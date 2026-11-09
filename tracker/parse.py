"""Pure HTML/JSON parsing helpers. No network, no third-party dependencies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterator

IN_AVAILABILITY = {
    "instock",
    "limitedavailability",
    "onlineonly",
    "instoreonly",
    "preorder",
    "presale",
    "backorder",
}
OUT_AVAILABILITY = {"outofstock", "soldout", "discontinued", "oos"}

# JSON-LD keys that point at *other* products; their offers must not count.
_RELATED_KEYS = {"isRelatedTo", "isSimilarTo", "isAccessoryOrSparePartFor", "isConsumableFor"}


@dataclass
class Extract:
    jsonld: list[Any] = field(default_factory=list)
    next_data: Any = None
    meta: dict[str, str] = field(default_factory=dict)
    title: str = ""


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = Extract()
        self._mode: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            if a.get("type", "").lower().strip() == "application/ld+json":
                self._mode, self._buf = "jsonld", []
            elif a.get("id") == "__NEXT_DATA__":
                self._mode, self._buf = "next", []
        elif tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and "content" in a:
                self.out.meta.setdefault(key.lower(), a["content"])
        elif tag == "link":
            # Microdata form: <link itemprop="availability" href="https://schema.org/InStock">
            if a.get("itemprop") and a.get("href"):
                self.out.meta.setdefault(a["itemprop"].lower(), a["href"])
        elif tag == "title" and not self.out.title and self._mode is None:
            self._mode, self._buf = "title", []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._mode in ("jsonld", "next") and tag == "script":
            doc = _loads("".join(self._buf))
            if doc is not None:
                if self._mode == "jsonld":
                    self.out.jsonld.append(doc)
                else:
                    self.out.next_data = doc
            self._mode = None
        elif self._mode == "title" and tag == "title":
            self.out.title = " ".join("".join(self._buf).split())
            self._mode = None


def _loads(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text, strict=False)
    except ValueError:
        return None


def extract(html: str) -> Extract:
    parser = _Extractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # malformed markup: keep whatever was collected
        pass
    return parser.out


def normalize_availability(value: Any) -> str:
    """'https://schema.org/InStock' / 'in stock' / 'IN_STOCK' -> 'instock'."""
    if not isinstance(value, str):
        return ""
    value = value.strip().rsplit("/", 1)[-1]
    return re.sub(r"[^a-z]", "", value.lower())


def classify_availability(value: Any) -> bool | None:
    """True = buyable, False = not buyable, None = unrecognised."""
    norm = normalize_availability(value)
    if norm in IN_AVAILABILITY:
        return True
    if norm in OUT_AVAILABILITY:
        return False
    return None


def to_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        if m:
            try:
                price = float(m.group(0).replace(",", ""))
            except ValueError:
                return None
            return price if price > 0 else None
    return None


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _products(node: Any) -> Iterator[dict]:
    """Yield every schema.org Product-like node, not descending into related products."""
    if isinstance(node, list):
        for item in node:
            yield from _products(item)
    elif isinstance(node, dict):
        if any("Product" in t for t in _types(node)):
            yield node
            return
        for key, value in node.items():
            if key not in _RELATED_KEYS:
                yield from _products(value)


def _offers(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _offers(item)
    elif isinstance(node, dict):
        if "availability" in node:
            yield node
        for key, value in node.items():
            if key not in _RELATED_KEYS:
                yield from _offers(value)


def _offer_price(offer: dict) -> float | None:
    for key in ("price", "lowPrice"):
        price = to_price(offer.get(key))
        if price is not None:
            return price
    spec = offer.get("priceSpecification")
    for item in spec if isinstance(spec, list) else [spec]:
        if isinstance(item, dict):
            price = to_price(item.get("price"))
            if price is not None:
                return price
    return None


def jsonld_offers(docs: list[Any]) -> list[tuple[bool | None, float | None, str]]:
    """(buyable, price, raw availability) for each offer of each Product in the JSON-LD."""
    found = []
    for product in _products(docs):
        for offer in _offers(product):
            raw = offer.get("availability")
            found.append((classify_availability(raw), _offer_price(offer), str(raw)))
    return found


def dig(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def find_dict_with(node: Any, key: str, depth: int = 12) -> dict | None:
    """First dict (breadth-ish, depth-limited) that has `key`."""
    if depth < 0:
        return None
    if isinstance(node, dict):
        if key in node:
            return node
        for value in node.values():
            hit = find_dict_with(value, key, depth - 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find_dict_with(value, key, depth - 1)
            if hit is not None:
                return hit
    return None
