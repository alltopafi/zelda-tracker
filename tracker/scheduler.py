from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, replace
from typing import Callable

from .config import Config, Product, RetailerConfig
from .models import Page, Status, Verdict
from .notify import BLUE, GREEN, GREY, ORANGE, Notifier
from .state import State, apply_verdict, save_state

log = logging.getLogger("tracker")


@dataclass
class Task:
    product: Product
    rc: RetailerConfig
    url: str
    next_due: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.product.id}|{self.rc.key}"


def apply_price_cap(verdict: Verdict, max_price: float | None) -> Verdict:
    """An 'in stock' listing far above list price is a reseller, not a restock."""
    if (
        verdict.status is Status.IN_STOCK
        and max_price is not None
        and verdict.price is not None
        and verdict.price > max_price
    ):
        return replace(
            verdict,
            status=Status.OUT_OF_STOCK,
            detail=f"listed at ${verdict.price:.2f}, above the ${max_price:.2f} cap ({verdict.detail})",
        )
    return verdict


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


class Scheduler:
    def __init__(self, cfg: Config, fetchers: dict, notifier: Notifier, state: State,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, persist: bool = True) -> None:
        self.cfg = cfg
        self.fetchers = fetchers
        self.notifier = notifier
        self.state = state
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.persist = persist
        self.tasks: list[Task] = []
        self._last_touch = 0.0
        now = self.clock()
        for product in cfg.products:
            for key, url in product.urls.items():
                # Stagger the first round so nothing fires all at once.
                self.tasks.append(Task(product, cfg.retailers[key], url, now + 3.0 * len(self.tasks)))

    # ---- one check -------------------------------------------------------

    def check(self, task: Task) -> Verdict:
        retailer = task.rc.retailer
        try:
            page = self.fetchers[task.rc.fetch].fetch(task.url, retailer.selectors)
        except Exception as exc:  # a fetcher bug must not kill the loop
            log.exception("fetch crashed for %s", task.key)
            page = Page(url=task.url, status=0, error=f"{type(exc).__name__}: {exc}")
        verdict = apply_price_cap(retailer.evaluate(page), task.product.max_price)
        if verdict.status is Status.UNKNOWN and page.html:
            self._save_debug(task, page)
        return verdict

    def run_task(self, task: Task) -> Verdict:
        s = self.cfg.settings
        verdict = self.check(task)
        now = self.clock()
        entry = self.state.entry(task.key)
        before = (entry.definite, entry.last_alert)
        events = apply_verdict(entry, verdict, now, s.realert_minutes * 60, s.unknown_alert_after)
        log.info("%-9s %-22s %-12s %s", task.rc.key, task.product.id, verdict.status.value, verdict.detail)

        if "restock" in events and not self._alert_restock(task, verdict):
            # Delivery failed: forget the alert so the next poll tries again.
            entry.definite, entry.last_alert = before
        if "parser" in events:
            self.notifier.send(
                f"Can't read stock: {task.product.name} at {task.rc.retailer.display}",
                f"{entry.unknown_streak} checks in a row returned no stock signal "
                f"({verdict.detail}). The page layout may have changed; the last page is saved "
                f"under debug/ in the data volume.",
                url=task.url, color=ORANGE,
            )

        rstate = self.state.retailer(task.rc.key)
        if verdict.status is Status.BLOCKED:
            rstate.blocked_streak += 1
            delay = min(task.rc.max_interval * (2 ** rstate.blocked_streak), s.max_backoff_seconds)
            if now - rstate.blocked_notified_at >= s.block_notify_minutes * 60:
                rstate.blocked_notified_at = now
                how = (
                    "Open the browser over noVNC to clear it by hand, or leave it and it will keep retrying."
                    if task.rc.fetch == "browser"
                    else 'Consider setting fetch = "browser" for this retailer.'
                )
                self.notifier.send(
                    f"{task.rc.retailer.display} is blocking checks",
                    f"{verdict.detail}. Backing off for {_age(delay)}. {how}",
                    url=task.url, color=ORANGE,
                )
            # Hold every product at this retailer, not just this one.
            for other in self.tasks:
                if other.rc.key == task.rc.key:
                    other.next_due = max(other.next_due, now + delay)
            task.next_due = now + delay
        else:
            rstate.blocked_streak = 0
            task.next_due = now + self.rng.uniform(task.rc.min_interval, task.rc.max_interval)
        self._save()
        return verdict

    def _alert_restock(self, task: Task, verdict: Verdict) -> bool:
        fields = [("Retailer", task.rc.retailer.display)]
        fields.append(("Price", f"${verdict.price:.2f}" if verdict.price is not None else "not read"))
        fields.append(("Signal", verdict.detail))
        return self.notifier.send(
            f"IN STOCK: {task.product.name}",
            f"Available at {task.rc.retailer.display}. Tap the title to open the product page.\n{task.url}",
            url=task.url, color=GREEN, fields=fields, ping=True,
        )

    def _save_debug(self, task: Task, page: Page) -> None:
        try:
            d = self.cfg.settings.debug_dir
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{task.rc.key}-{task.product.id}.html").write_text(page.html, errors="replace")
        except OSError:
            pass

    def _save(self) -> None:
        if self.persist:
            try:
                save_state(self.state, self.cfg.settings.state_path)
            except OSError as exc:
                log.warning("could not save state: %s", exc)

    # ---- summaries -------------------------------------------------------

    def summary(self) -> str:
        now = self.clock()
        lines = []
        for task in self.tasks:
            entry = self.state.entries.get(task.key)
            if entry is None or not entry.last_checked:
                lines.append(f"**{task.rc.retailer.display}** - {task.product.name}: not checked yet")
            else:
                lines.append(
                    f"**{task.rc.retailer.display}** - {task.product.name}: "
                    f"{entry.status.replace('_', ' ')} (checked {_age(now - entry.last_checked)} ago)"
                )
        return "\n".join(lines)

    def announce_start(self) -> None:
        watching = "\n".join(
            f"**{t.rc.retailer.display}** - {t.product.name} ({t.rc.fetch}, every "
            f"{int(t.rc.min_interval)}-{int(t.rc.max_interval)}s)" for t in self.tasks
        )
        self.notifier.send("Restock tracker started", watching, color=BLUE)
        self.state.last_heartbeat = self.clock()

    def maybe_heartbeat(self) -> None:
        hours = self.cfg.settings.heartbeat_hours
        if hours <= 0:
            return
        now = self.clock()
        if now - self.state.last_heartbeat >= hours * 3600:
            self.state.last_heartbeat = now
            self.notifier.send("Still watching", self.summary(), color=GREY)
            self._save()

    # ---- loop ------------------------------------------------------------

    def _touch_liveness(self) -> None:
        now = self.clock()
        if now - self._last_touch < 20:
            return
        self._last_touch = now
        try:
            path = self.cfg.settings.heartbeat_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(int(self.clock())))
        except OSError:
            pass

    def step(self) -> bool:
        """Run at most one due task. Returns True if a task ran."""
        self._touch_liveness()
        self.maybe_heartbeat()
        now = self.clock()
        task = min(self.tasks, key=lambda t: t.next_due)
        wait = task.next_due - now
        if wait > 0:
            self.sleep(min(wait, 1.0))
            return False
        if task.rc.fetch == "browser" and self.cfg.settings.pause_path.exists():
            # You are using the browser over noVNC; leave its tabs alone.
            for other in self.tasks:
                if other.rc.fetch == "browser":
                    other.next_due = max(other.next_due, now + 15.0)
            return False
        self.run_task(task)
        return True

    def run_forever(self, should_stop: Callable[[], bool]) -> None:
        self.announce_start()
        while not should_stop():
            self.step()
