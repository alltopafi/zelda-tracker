from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from ..models import Element, Page

log = logging.getLogger("tracker.browser")

# Runs in the page: for each selector, describe the first few matching nodes.
PROBE_JS = """
(selectors) => {
  const out = {};
  for (const sel of selectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { nodes = []; }
    out[sel] = nodes.slice(0, 8).map((n) => {
      const r = n.getBoundingClientRect();
      const st = window.getComputedStyle(n);
      const text = (n.innerText || n.getAttribute('aria-label') || n.value || '').trim();
      return {
        text: text.slice(0, 200),
        state: n.getAttribute('data-button-state') || '',
        disabled: !!(n.disabled || n.getAttribute('aria-disabled') === 'true'),
        visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none',
      };
    });
  }
  return out;
}
"""


class BrowserFetcher:
    """One real Chromium window with a profile on disk and one tab per product URL.

    Each fetch reloads that URL's tab and reads the rendered page. The window is
    visible on the container's virtual display, so you can look at it over noVNC.
    """

    def __init__(self, profile_dir: Path, headless: bool = False,
                 nav_timeout_ms: int = 45_000, settle_ms: int = 2_500) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.settle_ms = settle_ms
        self._pw = None
        self._ctx = None
        self._pages: dict[str, object] = {}

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # A killed pod leaves Chromium's profile lock behind, and the next pod has a
        # different hostname, so Chromium would refuse to open the profile.
        for stale in self.profile_dir.glob("Singleton*"):
            try:
                stale.unlink()
            except OSError:
                pass
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            no_viewport=True,
            args=[
                "--window-size=1366,850",
                "--window-position=0,0",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-crash-restore-bubble",
            ],
        )
        self._ctx.set_default_timeout(self.nav_timeout_ms)
        self._pages = {}
        log.info("browser started (profile %s, headless=%s)", self.profile_dir, self.headless)

    def _page_for(self, url: str):
        page = self._pages.get(url)
        if page is not None and not page.is_closed():
            return page
        spare = [p for p in self._ctx.pages if p not in self._pages.values() and not p.is_closed()]
        page = spare[0] if spare else self._ctx.new_page()
        self._pages[url] = page
        return page

    def fetch(self, url: str, selectors: Sequence[str] = ()) -> Page:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            if self._ctx is None:
                self._start()
            page = self._page_for(url)
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            status = response.status if response is not None else 200
            try:
                page.wait_for_load_state("networkidle", timeout=self.settle_ms * 3)
            except PlaywrightTimeout:
                pass  # busy retail pages never go fully idle
            page.wait_for_timeout(self.settle_ms)
            raw = page.evaluate(PROBE_JS, list(selectors)) if selectors else {}
            elements = {
                sel: [
                    Element(
                        text=str(n.get("text", "")),
                        state=str(n.get("state", "")),
                        disabled=bool(n.get("disabled")),
                        visible=bool(n.get("visible")),
                    )
                    for n in nodes
                ]
                for sel, nodes in raw.items()
            }
            return Page(url=url, status=status, html=page.content(), title=page.title(),
                        final_url=page.url, elements=elements)
        except PlaywrightTimeout as exc:
            return Page(url=url, status=0, error=f"timeout: {str(exc).splitlines()[0]}")
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            if "closed" in message.lower() or "crash" in message.lower():
                log.warning("browser went away (%s); it will be restarted on the next check", message)
                self.close()
            return Page(url=url, status=0, error=f"browser error: {message}")

    def close(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._ctx = self._pw = None
        self._pages = {}
        for closer in (getattr(ctx, "close", None), getattr(pw, "stop", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
