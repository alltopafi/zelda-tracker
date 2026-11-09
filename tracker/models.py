from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Status(str, enum.Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"  # page loaded but no stock signal could be read
    BLOCKED = "blocked"  # the retailer served a challenge or refused the request

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


@dataclass(frozen=True)
class Element:
    """One DOM node matched by a selector in the rendered page (browser fetch only)."""

    text: str = ""
    state: str = ""  # e.g. Best Buy's data-button-state
    disabled: bool = False
    visible: bool = True


@dataclass
class Page:
    """Result of fetching one product page, by either fetcher."""

    url: str
    status: int = 0  # HTTP status; 0 means the request itself failed
    html: str = ""
    title: str = ""
    final_url: str = ""
    elements: dict[str, list[Element]] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class Verdict:
    status: Status
    price: float | None = None
    detail: str = ""
