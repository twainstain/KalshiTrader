"""Discord alert backend.

Sends messages via Discord Webhook. Requires:
  - DISCORD_WEBHOOK_URL: Webhook URL from Discord channel settings

Setup:
  1. In Discord, go to Channel Settings → Integrations → Webhooks
  2. Create a webhook, copy the URL
  3. Add to .env:
     DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

`ALLOWED_EVENTS` keeps Discord low-noise — risk_reject and paper_fill would
fire dozens of times an hour in shadow mode. Only live fills, system errors,
and daily summaries land on Discord by default.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

EVENT_COLORS = {
    "paper_fill":     0x3498DB,  # blue
    "live_fill":      0x2ECC71,  # green
    "paper_settle":   0x9B59B6,  # purple
    "risk_reject":    0xE67E22,  # dark orange
    "system_error":   0xE74C3C,  # red
    "daily_summary":  0x1ABC9C,  # teal
}


class DiscordAlert:
    """Send alerts to a Discord channel via webhook."""

    # Events too noisy for Discord (thousands/day in shadow) are filtered at
    # the backend so call sites stay uniform across backends.
    ALLOWED_EVENTS = frozenset({
        "live_fill", "system_error", "daily_summary", "paper_settle",
    })

    def __init__(
        self,
        webhook_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "discord"

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, event_type: str, message: str,
             details: dict | None = None) -> bool:
        if not self.configured:
            logger.debug("Discord not configured — skipping alert")
            return False
        if event_type not in self.ALLOWED_EVENTS:
            logger.debug("Discord: skipping event_type=%s (not in allowed list)",
                         event_type)
            return True  # True so the dispatcher doesn't log a failure

        color = EVENT_COLORS.get(event_type, 0x95A5A6)
        title = event_type.replace("_", " ").title()

        # Skip raw link keys from embed fields — they're rendered as dedicated
        # clickable fields at the bottom instead of duplicated as plain text.
        SKIP_FIELDS = {"market_link", "dashboard_link"}
        fields = []
        if details:
            for key, val in details.items():
                if key in SKIP_FIELDS:
                    continue
                fields.append({
                    "name": key.replace("_", " ").title(),
                    "value": str(val),
                    "inline": True,
                })
            market_link = details.get("market_link")
            dash_link = details.get("dashboard_link")
            if market_link:
                fields.append({"name": "Kalshi Market",
                               "value": f"[Open]({market_link})",
                               "inline": True})
            if dash_link:
                fields.append({"name": "Dashboard",
                               "value": f"[Open]({dash_link})",
                               "inline": True})

        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": message,
                    "color": color,
                    "fields": fields[:25],  # Discord max 25 fields
                }
            ],
        }

        try:
            resp = requests.post(
                self.webhook_url, json=payload, timeout=self.timeout,
            )
            if resp.status_code in (200, 204):
                return True
            logger.warning("Discord webhook returned %d: %s",
                           resp.status_code, resp.text)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Discord send failed: %s", exc)
            return False
