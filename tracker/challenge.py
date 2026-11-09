"""Recognise when a retailer refused the request or served a bot challenge.

Detection only. The tracker never tries to get past a challenge: it backs off,
tells you on Discord, and waits for you to clear it by hand (or for it to expire).
"""

from __future__ import annotations

from .models import Page

TITLE_MARKERS = (
    "robot or human",
    "access denied",
    "just a moment",
    "attention required",
    "pardon our interruption",
    "verify you are human",
    "are you a human",
    "security check",
    "request blocked",
)
# Markers that only appear on challenge pages, not on normal protected pages.
HTML_MARKERS = ('id="px-captcha"', "geo.captcha-delivery.com")
URL_MARKERS = ("/blocked", "/captcha")


def blocked_reason(page: Page, title: str = "") -> str | None:
    if page.status in (403, 429):
        return f"HTTP {page.status}"
    title_l = (page.title or title or "").lower()
    for marker in TITLE_MARKERS:
        if marker in title_l:
            return f"challenge page ({marker!r} in title)"
    final = (page.final_url or "").lower().split("?", 1)[0]
    for marker in URL_MARKERS:
        if final.endswith(marker) or f"{marker}/" in final:
            return f"redirected to {marker}"
    html = page.html or ""
    for marker in HTML_MARKERS:
        if marker in html:
            return "challenge page (captcha widget present)"
    return None
