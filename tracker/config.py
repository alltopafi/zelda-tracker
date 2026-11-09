from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .retailers import REGISTRY, Retailer

MIN_INTERVAL_FLOOR = 15  # seconds; nothing polls faster than this


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mention: str = ""
    username: str = "Restock Tracker"
    user_agent: str = "restock-tracker/0.1 (personal stock checker)"
    heartbeat_hours: float = 24.0
    unknown_alert_after: int = 5
    realert_minutes: float = 10.0
    block_notify_minutes: float = 60.0
    max_backoff_seconds: float = 1800.0
    headless: bool = False

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def pause_path(self) -> Path:
        return self.data_dir / "PAUSE"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"


@dataclass(frozen=True)
class RetailerConfig:
    retailer: Retailer
    fetch: str
    min_interval: float
    max_interval: float

    @property
    def key(self) -> str:
        return self.retailer.key


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    urls: dict[str, str]
    max_price: float | None = None


@dataclass(frozen=True)
class Config:
    settings: Settings
    retailers: dict[str, RetailerConfig]
    products: list[Product] = field(default_factory=list)
    webhook_url: str = ""


DEFAULT_INTERVALS = {"http": (45.0, 90.0), "browser": (75.0, 150.0)}


def _retailer_config(key: str, raw: dict) -> RetailerConfig:
    if key not in REGISTRY:
        raise ConfigError(f"unknown retailer {key!r}; known: {', '.join(sorted(REGISTRY))}")
    retailer = REGISTRY[key]
    fetch = raw.get("fetch", retailer.default_fetch)
    if fetch not in ("http", "browser"):
        raise ConfigError(f"retailers.{key}.fetch must be 'http' or 'browser', got {fetch!r}")
    lo_default, hi_default = DEFAULT_INTERVALS[fetch]
    lo = float(raw.get("min_interval", lo_default))
    hi = float(raw.get("max_interval", max(hi_default, lo)))
    if lo < MIN_INTERVAL_FLOOR:
        raise ConfigError(f"retailers.{key}.min_interval must be at least {MIN_INTERVAL_FLOOR}s")
    if hi < lo:
        raise ConfigError(f"retailers.{key}.max_interval must be >= min_interval")
    buy = raw.get("buy_selectors")
    out = raw.get("out_selectors")
    retailer = retailer.with_selectors(
        tuple(buy) if buy is not None else None, tuple(out) if out is not None else None
    )
    return RetailerConfig(retailer, fetch, lo, hi)


def parse_config(raw: dict, data_dir: str | os.PathLike | None = None, env=os.environ) -> Config:
    s = dict(raw.get("settings", {}))
    data = Path(data_dir or env.get("TRACKER_DATA_DIR") or s.pop("data_dir", "/data"))
    s.pop("data_dir", None)
    headless = str(env.get("TRACKER_HEADLESS", "")).lower() in ("1", "true", "yes")
    try:
        settings = Settings(data_dir=data, headless=headless or bool(s.pop("headless", False)), **s)
    except TypeError as exc:
        raise ConfigError(f"[settings]: {exc}") from None

    retailers_raw = raw.get("retailers", {})
    products: list[Product] = []
    seen: set[str] = set()
    for p in raw.get("products", []):
        pid = p.get("id")
        if not pid or pid in seen:
            raise ConfigError(f"each [[products]] needs a unique id (got {pid!r})")
        seen.add(pid)
        urls = {}
        for key, url in (p.get("urls") or {}).items():
            if not url:
                continue  # blank URL = not tracked at this retailer
            if key not in REGISTRY:
                raise ConfigError(f"product {pid!r}: unknown retailer {key!r}")
            if not str(url).startswith("https://"):
                raise ConfigError(f"product {pid!r}: {key} url must start with https://")
            urls[key] = str(url)
        if not urls:
            raise ConfigError(f"product {pid!r} has no urls")
        max_price = p.get("max_price")
        products.append(
            Product(pid, p.get("name", pid), urls, float(max_price) if max_price is not None else None)
        )
    if not products:
        raise ConfigError("no [[products]] configured")

    used = {key for p in products for key in p.urls}
    for key in retailers_raw:
        if key not in REGISTRY:
            raise ConfigError(f"unknown retailer {key!r}; known: {', '.join(sorted(REGISTRY))}")
    retailers = {key: _retailer_config(key, retailers_raw.get(key, {})) for key in sorted(used)}
    return Config(settings, retailers, products, env.get("DISCORD_WEBHOOK_URL", "").strip())


def load_config(path: str | os.PathLike, data_dir: str | os.PathLike | None = None) -> Config:
    with open(path, "rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
    return parse_config(raw, data_dir)
