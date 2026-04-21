"""Alert dispatcher — fan-out wrapper around registered alert backends.

Every backend exposes:
    - `name: str`
    - `configured: bool` (optional; defaults True if missing)
    - `send(event_type, message, details) -> bool`

`alert()` calls every backend's `send()`, swallowing exceptions — a broken
telemetry backend must never take down the scanner or executor. Returns the
count of successful deliveries so callers can react (e.g. log a warning if
zero backends acked a critical system_error).

Kalshi-specific helpers (`paper_fill`, `risk_reject`, `paper_settle`,
`live_fill`, `system_error`, `daily_summary`) build a human-readable message
plus a structured `details` dict so Discord embeds / Gmail tables can render
the same payload without every call site reformatting.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

logger = logging.getLogger(__name__)


KALSHI_MARKET_URL_BASE = "https://kalshi.com/markets"
DEFAULT_DASHBOARD_URL = "http://localhost:8000"


def kalshi_market_url(ticker: str) -> str:
    """Return the Kalshi site URL for a market ticker."""
    return f"{KALSHI_MARKET_URL_BASE}/{ticker}"


def dashboard_market_url(ticker: str,
                         dashboard_url: str = DEFAULT_DASHBOARD_URL) -> str:
    """Return the local dashboard detail URL for a market ticker."""
    return f"{dashboard_url}/kalshi/decisions?ticker={ticker}"


def _fmt(val) -> str:
    """Stable string formatting for Decimals and floats in messages."""
    if isinstance(val, Decimal):
        return format(val, "f")
    return str(val)


class AlertDispatcher:
    """Fan-out dispatcher with Kalshi-specific convenience methods."""

    def __init__(self, backends: list | None = None) -> None:
        self._backends: list = []
        if backends:
            for b in backends:
                self.add_backend(b)

    def add_backend(self, backend) -> None:
        self._backends.append(backend)

    @property
    def backend_count(self) -> int:
        return len(self._backends)

    def alert(self, event_type: str, message: str,
              details: dict | None = None) -> int:
        """Send to every backend. Returns count of successful deliveries."""
        delivered = 0
        for backend in self._backends:
            try:
                ok = backend.send(event_type, message, details)
                if ok:
                    delivered += 1
                else:
                    logger.debug("Alert backend '%s' skipped or failed %s",
                                 getattr(backend, "name", "?"), event_type)
            except Exception as exc:  # noqa: BLE001 — telemetry must not crash caller
                logger.error("Alert backend '%s' raised on %s: %s",
                             getattr(backend, "name", "?"), event_type, exc)
        return delivered

    # Kalshi-specific helpers ------------------------------------------------

    def paper_fill(self, ticker: str, side: str, fill_price,
                   size_contracts, edge_bps=None,
                   strategy_label: str = "",
                   dashboard_url: str = DEFAULT_DASHBOARD_URL) -> int:
        lines = [
            f"Paper fill: {ticker}",
            f"Side: {side} @ {_fmt(fill_price)}",
            f"Size: {_fmt(size_contracts)} contracts",
        ]
        details: dict = {
            "ticker": ticker, "side": side,
            "fill_price": _fmt(fill_price),
            "size_contracts": _fmt(size_contracts),
        }
        if edge_bps is not None:
            lines.append(f"Edge: {_fmt(edge_bps)} bps")
            details["edge_bps"] = _fmt(edge_bps)
        if strategy_label:
            details["strategy_label"] = strategy_label
            lines.append(f"Strategy: {strategy_label}")
        dash = dashboard_market_url(ticker, dashboard_url)
        market = kalshi_market_url(ticker)
        lines.append(f"Market: {market}")
        lines.append(f"Dashboard: {dash}")
        details["market_link"] = market
        details["dashboard_link"] = dash
        return self.alert("paper_fill", "\n".join(lines), details)

    def live_fill(self, ticker: str, side: str, fill_price,
                  size_contracts, order_id: str = "",
                  strategy_label: str = "",
                  dashboard_url: str = DEFAULT_DASHBOARD_URL) -> int:
        lines = [
            f"LIVE fill: {ticker}",
            f"Side: {side} @ {_fmt(fill_price)}",
            f"Size: {_fmt(size_contracts)} contracts",
        ]
        details: dict = {
            "ticker": ticker, "side": side,
            "fill_price": _fmt(fill_price),
            "size_contracts": _fmt(size_contracts),
        }
        if order_id:
            lines.append(f"Order ID: {order_id}")
            details["order_id"] = order_id
        if strategy_label:
            details["strategy_label"] = strategy_label
        market = kalshi_market_url(ticker)
        dash = dashboard_market_url(ticker, dashboard_url)
        lines.append(f"Market: {market}")
        lines.append(f"Dashboard: {dash}")
        details["market_link"] = market
        details["dashboard_link"] = dash
        return self.alert("live_fill", "\n".join(lines), details)

    def risk_reject(self, ticker: str, side: str, reason: str,
                    strategy_label: str = "") -> int:
        lines = [
            f"Risk reject: {ticker}",
            f"Side: {side}",
            f"Reason: {reason}",
        ]
        details: dict = {
            "ticker": ticker, "side": side, "reason": reason,
        }
        if strategy_label:
            details["strategy_label"] = strategy_label
        return self.alert("risk_reject", "\n".join(lines), details)

    def paper_settle(self, ticker: str, outcome: str, realized_pnl_usd,
                     strategy_label: str = "") -> int:
        lines = [
            f"Paper settle: {ticker}",
            f"Outcome: {outcome}",
            f"Realized P/L: {_fmt(realized_pnl_usd)}",
        ]
        details: dict = {
            "ticker": ticker, "outcome": outcome,
            "realized_pnl_usd": _fmt(realized_pnl_usd),
        }
        if strategy_label:
            details["strategy_label"] = strategy_label
        return self.alert("paper_settle", "\n".join(lines), details)

    def system_error(self, component: str, error: str) -> int:
        msg = f"System error in {component}:\n{error}"
        return self.alert("system_error", msg, {
            "component": component, "error": error,
        })

    def daily_summary(self, *, ticks: int, decisions: int, fills: int,
                      settlements: int, realized_pnl_usd,
                      strategy_label: str = "") -> int:
        msg = (f"Daily summary\n"
               f"Ticks: {ticks}\n"
               f"Decisions: {decisions}\n"
               f"Paper fills: {fills}\n"
               f"Settlements: {settlements}\n"
               f"Realized P/L: {_fmt(realized_pnl_usd)}")
        details = {
            "ticks": ticks, "decisions": decisions, "fills": fills,
            "settlements": settlements,
            "realized_pnl_usd": _fmt(realized_pnl_usd),
        }
        if strategy_label:
            details["strategy_label"] = strategy_label
        return self.alert("daily_summary", msg, details)


def build_dispatcher_from_env(env: dict | None = None) -> AlertDispatcher:
    """Construct a dispatcher from env vars, attaching only configured backends.

    Reads standard env vars (TELEGRAM_BOT_TOKEN/CHAT_ID, DISCORD_WEBHOOK_URL,
    GMAIL_ADDRESS/APP_PASSWORD/RECIPIENT). Unconfigured backends are omitted
    rather than attached as no-ops so `backend_count` reflects reality.
    """
    from alerting.discord import DiscordAlert
    from alerting.gmail import GmailAlert
    from alerting.telegram import TelegramAlert

    e = env if env is not None else os.environ
    dispatcher = AlertDispatcher()
    # Pass string values (never None) so the backend uses the explicit env
    # dict rather than falling back to os.environ in its own __init__.
    tg = TelegramAlert(
        bot_token=e.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=e.get("TELEGRAM_CHAT_ID", ""),
    )
    if tg.configured:
        dispatcher.add_backend(tg)
    dc = DiscordAlert(webhook_url=e.get("DISCORD_WEBHOOK_URL", ""))
    if dc.configured:
        dispatcher.add_backend(dc)
    gm = GmailAlert(
        address=e.get("GMAIL_ADDRESS", ""),
        app_password=e.get("GMAIL_APP_PASSWORD", ""),
        recipient=e.get("GMAIL_RECIPIENT", ""),
    )
    if gm.configured:
        dispatcher.add_backend(gm)
    return dispatcher
