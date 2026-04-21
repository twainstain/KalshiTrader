"""Alerting — multi-backend fan-out for operational and trading events."""

from alerting.dispatcher import (
    AlertDispatcher,
    DEFAULT_DASHBOARD_URL,
    KALSHI_MARKET_URL_BASE,
    build_dispatcher_from_env,
    dashboard_market_url,
    kalshi_market_url,
)
from alerting.discord import DiscordAlert
from alerting.gmail import GmailAlert
from alerting.telegram import TelegramAlert

__all__ = [
    "AlertDispatcher",
    "DEFAULT_DASHBOARD_URL",
    "KALSHI_MARKET_URL_BASE",
    "build_dispatcher_from_env",
    "dashboard_market_url",
    "kalshi_market_url",
    "DiscordAlert",
    "GmailAlert",
    "TelegramAlert",
]
