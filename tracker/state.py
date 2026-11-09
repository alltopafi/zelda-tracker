from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .models import Status, Verdict


@dataclass
class Entry:
    status: str = Status.UNKNOWN.value
    definite: str = ""  # last in_stock / out_of_stock reading; survives unknown and blocked polls
    since: float = 0.0
    last_checked: float = 0.0
    last_alert: float = 0.0
    unknown_streak: int = 0
    unknown_notified: bool = False
    price: float | None = None
    detail: str = ""


@dataclass
class RetailerState:
    blocked_streak: int = 0
    blocked_notified_at: float = 0.0


@dataclass
class State:
    entries: dict[str, Entry] = field(default_factory=dict)
    retailers: dict[str, RetailerState] = field(default_factory=dict)
    last_heartbeat: float = 0.0

    def entry(self, key: str) -> Entry:
        return self.entries.setdefault(key, Entry())

    def retailer(self, key: str) -> RetailerState:
        return self.retailers.setdefault(key, RetailerState())


def _build(cls, raw: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in names})


def load_state(path: Path) -> State:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return State()
    state = State(last_heartbeat=float(raw.get("last_heartbeat", 0.0)))
    for key, value in (raw.get("entries") or {}).items():
        state.entries[key] = _build(Entry, value)
    for key, value in (raw.get("retailers") or {}).items():
        state.retailers[key] = _build(RetailerState, value)
    return state


def save_state(state: State, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=1, sort_keys=True))
    os.replace(tmp, path)


def apply_verdict(
    entry: Entry, verdict: Verdict, now: float, realert_seconds: float, unknown_alert_after: int
) -> list[str]:
    """Fold one poll result into the entry. Returns events: 'restock', 'parser'."""
    events: list[str] = []
    if verdict.status.value != entry.status:
        entry.since = now
    entry.status = verdict.status.value
    entry.last_checked = now
    entry.detail = verdict.detail
    if verdict.price is not None:
        entry.price = verdict.price

    if verdict.status in (Status.IN_STOCK, Status.OUT_OF_STOCK):
        entry.unknown_streak = 0
        entry.unknown_notified = False
        if verdict.status is Status.IN_STOCK:
            newly = entry.definite != Status.IN_STOCK.value
            if newly and now - entry.last_alert >= realert_seconds:
                events.append("restock")
                entry.last_alert = now
        entry.definite = verdict.status.value
    elif verdict.status is Status.UNKNOWN:
        entry.unknown_streak += 1
        if entry.unknown_streak >= unknown_alert_after and not entry.unknown_notified:
            entry.unknown_notified = True
            events.append("parser")
    return events
