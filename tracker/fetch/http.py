from __future__ import annotations

import gzip
import urllib.error
import urllib.request
import zlib
from typing import Sequence

from ..models import Page

MAX_BYTES = 8 * 1024 * 1024


def _decode(raw: bytes, headers) -> str:
    encoding = (headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            raw = zlib.decompress(raw)
    except (OSError, zlib.error):
        pass
    charset = headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class HttpFetcher:
    """Plain GET with the standard library. Identifies itself honestly; if a
    retailer refuses it (403/429), switch that retailer to fetch = "browser"."""

    def __init__(self, user_agent: str, timeout: float = 20.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def fetch(self, url: str, selectors: Sequence[str] = ()) -> Page:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                html = _decode(resp.read(MAX_BYTES), resp.headers)
                return Page(url=url, status=resp.status, html=html, final_url=resp.geturl())
        except urllib.error.HTTPError as exc:
            try:
                html = _decode(exc.read(MAX_BYTES), exc.headers)
            except Exception:
                html = ""
            return Page(url=url, status=exc.code, html=html, final_url=exc.geturl() or url)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return Page(url=url, status=0, error=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        pass
