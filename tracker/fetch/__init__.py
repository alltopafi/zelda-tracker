from __future__ import annotations

from typing import Protocol, Sequence

from ..models import Page


class Fetcher(Protocol):
    def fetch(self, url: str, selectors: Sequence[str] = ()) -> Page: ...

    def close(self) -> None: ...
