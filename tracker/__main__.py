from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from .config import Config, ConfigError, load_config
from .fetch.browser import BrowserFetcher
from .fetch.http import HttpFetcher
from .models import Page
from .notify import BLUE, Notifier
from .retailers import REGISTRY
from .scheduler import Scheduler
from .state import State, load_state

log = logging.getLogger("tracker")
HEALTH_MAX_AGE = 300  # seconds without a loop iteration before the pod counts as stuck


def build_fetchers(cfg: Config) -> dict:
    s = cfg.settings
    return {
        "http": HttpFetcher(s.user_agent),
        "browser": BrowserFetcher(s.profile_dir, headless=s.headless),
    }


def build_notifier(cfg: Config) -> Notifier:
    s = cfg.settings
    return Notifier(cfg.webhook_url, mention=s.mention, username=s.username, user_agent=s.user_agent)


def cmd_run(cfg: Config, args) -> int:
    if not cfg.webhook_url:
        log.warning("DISCORD_WEBHOOK_URL is not set: alerts will only be logged")
    cfg.settings.data_dir.mkdir(parents=True, exist_ok=True)
    fetchers = build_fetchers(cfg)
    scheduler = Scheduler(cfg, fetchers, build_notifier(cfg), load_state(cfg.settings.state_path))
    stopping = []

    def stop(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        stopping.append(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        scheduler.run_forever(lambda: bool(stopping))
    finally:
        for fetcher in fetchers.values():
            fetcher.close()
    return 0


def cmd_check_once(cfg: Config, args) -> int:
    """Check everything once (or a subset) and print the result. Sends no alerts
    unless --notify is given, and does not touch the saved state."""
    # A throwaway browser profile, so this never touches the profile a running
    # tracker has open (safe to use with kubectl exec).
    s = cfg.settings
    scratch = Path(tempfile.mkdtemp(prefix="tracker-check-"))
    fetchers = {
        "http": HttpFetcher(s.user_agent),
        "browser": BrowserFetcher(scratch, headless=s.headless),
    }
    notifier = build_notifier(cfg) if args.notify else Notifier("")
    scheduler = Scheduler(cfg, fetchers, notifier, State(), persist=False)
    code = 0
    try:
        for task in scheduler.tasks:
            if args.retailer and task.rc.key != args.retailer:
                continue
            if args.product and task.product.id != args.product:
                continue
            verdict = scheduler.run_task(task) if args.notify else scheduler.check(task)
            price = f"${verdict.price:.2f}" if verdict.price is not None else "-"
            print(f"{task.rc.key:<9} {task.product.id:<24} {verdict.status.value:<13} {price:<9} {verdict.detail}")
            if verdict.status.value in ("unknown", "blocked"):
                code = 2
    finally:
        for fetcher in fetchers.values():
            fetcher.close()
        shutil.rmtree(scratch, ignore_errors=True)
    return code


def cmd_evaluate(cfg: Config | None, args) -> int:
    """Run one retailer's parser against a saved HTML file (no network)."""
    retailer = REGISTRY[args.retailer]
    if cfg is not None and args.retailer in cfg.retailers:
        retailer = cfg.retailers[args.retailer].retailer
    html = Path(args.html_file).read_text(errors="replace")
    verdict = retailer.evaluate(Page(url="file", status=200, html=html))
    print(f"{verdict.status.value}  price={verdict.price}  {verdict.detail}")
    return 0


def cmd_test_notify(cfg: Config, args) -> int:
    ok = build_notifier(cfg).send(
        "Test alert", "If you can read this, the webhook works. A real restock alert pings "
        f"{cfg.settings.mention or 'nobody (no mention configured)'}.", color=BLUE, ping=args.ping)
    print("sent" if ok else "FAILED (see log above)")
    return 0 if ok else 1


def cmd_pause(cfg: Config, args) -> int:
    cfg.settings.pause_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.settings.pause_path.write_text("paused\n")
    print("browser checks paused; run 'resume' when you are done")
    return 0


def cmd_resume(cfg: Config, args) -> int:
    cfg.settings.pause_path.unlink(missing_ok=True)
    print("browser checks resumed")
    return 0


def cmd_healthcheck(cfg: Config, args) -> int:
    try:
        age = time.time() - cfg.settings.heartbeat_path.stat().st_mtime
    except OSError:
        print("no heartbeat file yet")
        return 1
    print(f"last loop iteration {age:.0f}s ago")
    return 0 if age < HEALTH_MAX_AGE else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracker", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("TRACKER_CONFIG", "/config/config.toml"))
    parser.add_argument("--data-dir", default=None, help="overrides TRACKER_DATA_DIR and the config")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="poll forever (default)").set_defaults(func=cmd_run)
    once = sub.add_parser("check-once", help="check each page once and print the results")
    once.add_argument("--retailer", choices=sorted(REGISTRY))
    once.add_argument("--product")
    once.add_argument("--notify", action="store_true", help="also send Discord alerts")
    once.set_defaults(func=cmd_check_once)
    ev = sub.add_parser("evaluate", help="run a retailer parser on a saved HTML file")
    ev.add_argument("retailer", choices=sorted(REGISTRY))
    ev.add_argument("html_file")
    ev.set_defaults(func=cmd_evaluate)
    tn = sub.add_parser("test-notify", help="send a test message to Discord")
    tn.add_argument("--ping", action="store_true", help="include the configured mention")
    tn.set_defaults(func=cmd_test_notify)
    sub.add_parser("pause", help="pause browser checks while you use noVNC").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume browser checks").set_defaults(func=cmd_resume)
    sub.add_parser("healthcheck", help="exit 0 if the loop is alive").set_defaults(func=cmd_healthcheck)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    func = getattr(args, "func", cmd_run)
    try:
        cfg = load_config(args.config, args.data_dir)
    except (ConfigError, OSError) as exc:
        if func is cmd_evaluate:
            return cmd_evaluate(None, args)
        print(f"config error: {exc}", file=sys.stderr)
        return 64
    return func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
