from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional

from ..challenge import blocked_reason
from ..models import Page, Status, Verdict
from ..parse import Extract, classify_availability, extract, jsonld_offers, to_price

BUY_WORDS = (
    "add to cart",
    "add to bag",
    "pre-order",
    "preorder",
    "pre order",
    "ship it",
    "pick it up",
    "buy now",
)
OUT_WORDS = (
    "sold out",
    "out of stock",
    "coming soon",
    "unavailable",
    "not available",
    "notify me",
    "temporarily out",
)

Signal = Callable[["Retailer", Page, Extract], Optional[Verdict]]


def sig_jsonld(retailer: "Retailer", page: Page, ext: Extract) -> Verdict | None:
    offers = jsonld_offers(ext.jsonld)
    buyable = [o for o in offers if o[0] is True]
    if buyable:
        prices = [o[1] for o in buyable if o[1] is not None]
        raw = buyable[0][2].rsplit("/", 1)[-1]
        return Verdict(Status.IN_STOCK, min(prices) if prices else None, f"json-ld: {raw}")
    gone = [o for o in offers if o[0] is False]
    if gone:
        raw = gone[0][2].rsplit("/", 1)[-1]
        return Verdict(Status.OUT_OF_STOCK, gone[0][1], f"json-ld: {raw}")
    return None


def sig_meta(retailer: "Retailer", page: Page, ext: Extract) -> Verdict | None:
    for key in ("product:availability", "og:availability", "availability"):
        buyable = classify_availability(ext.meta.get(key))
        if buyable is None:
            continue
        price = None
        for pkey in ("product:price:amount", "og:price:amount", "price"):
            price = to_price(ext.meta.get(pkey))
            if price is not None:
                break
        status = Status.IN_STOCK if buyable else Status.OUT_OF_STOCK
        return Verdict(status, price, f"meta {key}: {ext.meta[key]}")
    return None


def sig_elements(retailer: "Retailer", page: Page, ext: Extract) -> Verdict | None:
    """Read the rendered buy button. Only available on browser fetches."""
    buy_hit = out_hit = None
    for sel in retailer.buy_selectors:
        # Only the first visible match per selector: the product's own button comes
        # before any "add to cart" buttons in recommendation carousels further down.
        for el in [e for e in page.elements.get(sel, []) if e.visible][:1]:
            label = f"{el.text} {el.state.replace('_', ' ')}".lower()
            if any(w in label for w in OUT_WORDS):
                out_hit = out_hit or el
            elif any(w in label for w in BUY_WORDS):
                if el.disabled:
                    out_hit = out_hit or el
                else:
                    buy_hit = buy_hit or el
    if buy_hit is not None:
        return Verdict(Status.IN_STOCK, None, f"button: {_short(buy_hit.text or buy_hit.state)}")
    for sel in retailer.out_selectors:
        if any(el.visible for el in page.elements.get(sel, [])):
            return Verdict(Status.OUT_OF_STOCK, None, f"sold-out block: {sel}")
    if out_hit is not None:
        return Verdict(Status.OUT_OF_STOCK, None, f"button: {_short(out_hit.text or out_hit.state)}")
    return None


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 60 else text[:57] + "..."


def any_price(ext: Extract) -> float | None:
    prices = [o[1] for o in jsonld_offers(ext.jsonld) if o[1] is not None]
    if prices:
        return min(prices)
    for key in ("product:price:amount", "og:price:amount"):
        price = to_price(ext.meta.get(key))
        if price is not None:
            return price
    return None


@dataclass(frozen=True)
class Retailer:
    key: str
    display: str
    default_fetch: str  # "http" or "browser"
    signals: tuple[Signal, ...]  # tried in order; the first definite answer wins
    buy_selectors: tuple[str, ...] = ()
    out_selectors: tuple[str, ...] = ()

    @property
    def selectors(self) -> tuple[str, ...]:
        return self.buy_selectors + self.out_selectors

    def with_selectors(self, buy: tuple[str, ...] | None, out: tuple[str, ...] | None) -> "Retailer":
        return replace(
            self,
            buy_selectors=self.buy_selectors if buy is None else buy,
            out_selectors=self.out_selectors if out is None else out,
        )

    def evaluate(self, page: Page) -> Verdict:
        if page.status == 0:
            return Verdict(Status.UNKNOWN, None, f"fetch failed: {page.error or 'no response'}")
        ext = extract(page.html)
        reason = blocked_reason(page, ext.title)
        if reason:
            return Verdict(Status.BLOCKED, None, reason)
        if page.status in (404, 410):
            return Verdict(Status.UNKNOWN, None, f"HTTP {page.status}: product page not found")
        if page.status >= 500:
            return Verdict(Status.UNKNOWN, None, f"HTTP {page.status}")
        for signal in self.signals:
            verdict = signal(self, page, ext)
            if verdict is not None:
                if verdict.price is None:
                    verdict = replace(verdict, price=any_price(ext))
                return verdict
        return Verdict(Status.UNKNOWN, None, "no stock signal found on page")
