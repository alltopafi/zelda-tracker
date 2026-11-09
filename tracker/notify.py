from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger("tracker.notify")

GREEN, ORANGE, GREY, BLUE = 0x2ECC71, 0xE67E22, 0x95A5A6, 0x3498DB


def allowed_mentions(mention: str) -> dict:
    mention = mention.strip()
    if mention in ("@everyone", "@here"):
        return {"parse": ["everyone"]}
    m = re.fullmatch(r"<@&(\d+)>", mention)
    if m:
        return {"roles": [m.group(1)]}
    m = re.fullmatch(r"<@!?(\d+)>", mention)
    if m:
        return {"users": [m.group(1)]}
    return {"parse": []}


class Notifier:
    """Posts to a Discord webhook. Never raises; returns False if delivery failed."""

    def __init__(
        self,
        webhook_url: str,
        mention: str = "",
        username: str = "Restock Tracker",
        user_agent: str = "restock-tracker/0.1",
        timeout: float = 10.0,
        retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self.webhook_url = webhook_url
        self.mention = mention.strip()
        self.username = username
        # Discord rejects the default urllib user agent, so always send our own.
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep

    def send(self, title: str, description: str = "", url: str = "", color: int = GREY,
             fields: list[tuple[str, str]] | None = None, ping: bool = False) -> bool:
        embed: dict = {"title": title[:256], "color": color}
        if description:
            embed["description"] = description[:4000]
        if url:
            embed["url"] = url
        if fields:
            embed["fields"] = [
                {"name": n[:256], "value": (v or "-")[:1024], "inline": True} for n, v in fields
            ]
        payload = {
            "username": self.username,
            "content": self.mention if (ping and self.mention) else "",
            "embeds": [embed],
            "allowed_mentions": allowed_mentions(self.mention) if ping else {"parse": []},
        }
        if not self.webhook_url:
            log.warning("no DISCORD_WEBHOOK_URL set; would have sent: %s | %s", title, description)
            return False
        return self._post(payload)

    def _post(self, payload: dict) -> bool:
        body = json.dumps(payload).encode()
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                self.webhook_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if 200 <= resp.status < 300:
                        return True
                    log.warning("discord returned HTTP %s", resp.status)
            except urllib.error.HTTPError as exc:
                wait = 2.0 * attempt
                if exc.code == 429:
                    try:
                        wait = float(json.loads(exc.read() or b"{}").get("retry_after", wait))
                    except (ValueError, AttributeError):
                        pass
                    wait = min(wait + 0.25, 30.0)
                elif exc.code < 500:
                    log.error("discord rejected the message: HTTP %s", exc.code)
                    return False
                log.warning("discord HTTP %s, retrying in %.1fs", exc.code, wait)
                if attempt < self.retries:
                    self._sleep(wait)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.warning("discord post failed (%s), attempt %d/%d", exc, attempt, self.retries)
            if attempt < self.retries:
                self._sleep(2.0 * attempt)
        return False
