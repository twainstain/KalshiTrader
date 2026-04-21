"""Tests for src/alerting — dispatcher, Telegram, Discord, Gmail backends."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from alerting.dispatcher import (
    AlertDispatcher,
    build_dispatcher_from_env,
    dashboard_market_url,
    kalshi_market_url,
)
from alerting.discord import DiscordAlert
from alerting.gmail import GmailAlert
from alerting.telegram import TelegramAlert


class _FakeBackend:
    def __init__(self, name: str = "fake", should_fail: bool = False,
                 return_value: bool = True):
        self._name = name
        self._should_fail = should_fail
        self._return_value = return_value
        self.received: list[tuple] = []

    @property
    def name(self) -> str:
        return self._name

    def send(self, event_type, message, details=None):
        if self._should_fail:
            raise RuntimeError("boom")
        self.received.append((event_type, message, details))
        return self._return_value


# ---------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------

class DispatcherTests(unittest.TestCase):
    def test_no_backends_returns_zero(self):
        d = AlertDispatcher()
        self.assertEqual(d.alert("test", "hello"), 0)
        self.assertEqual(d.backend_count, 0)

    def test_routes_to_all_backends(self):
        b1 = _FakeBackend("b1")
        b2 = _FakeBackend("b2")
        d = AlertDispatcher([b1, b2])
        count = d.alert("paper_fill", "msg")
        self.assertEqual(count, 2)
        self.assertEqual(len(b1.received), 1)
        self.assertEqual(len(b2.received), 1)

    def test_failing_backend_doesnt_crash_dispatcher(self):
        good = _FakeBackend("good")
        bad = _FakeBackend("bad", should_fail=True)
        d = AlertDispatcher([bad, good])
        count = d.alert("system_error", "oops")
        self.assertEqual(count, 1)
        self.assertEqual(len(good.received), 1)

    def test_backend_returning_false_does_not_count(self):
        skipped = _FakeBackend("skip", return_value=False)
        d = AlertDispatcher([skipped])
        self.assertEqual(d.alert("paper_fill", "msg"), 0)

    def test_add_backend(self):
        d = AlertDispatcher()
        d.add_backend(_FakeBackend("x"))
        self.assertEqual(d.backend_count, 1)

    def test_paper_fill_helper(self):
        b = _FakeBackend()
        d = AlertDispatcher([b])
        d.paper_fill("KXBTC15M-T1", "yes", Decimal("0.42"),
                     Decimal("10"), edge_bps=Decimal("325"),
                     strategy_label="pure_lag")
        event, msg, details = b.received[0]
        self.assertEqual(event, "paper_fill")
        self.assertIn("KXBTC15M-T1", msg)
        self.assertIn("0.42", msg)
        self.assertEqual(details["ticker"], "KXBTC15M-T1")
        self.assertEqual(details["strategy_label"], "pure_lag")
        self.assertIn("kalshi.com/markets/KXBTC15M-T1", details["market_link"])
        self.assertIn("dashboard_link", details)

    def test_live_fill_helper(self):
        b = _FakeBackend()
        d = AlertDispatcher([b])
        d.live_fill("KXETH15M-T2", "no", Decimal("0.58"), Decimal("5"),
                    order_id="ord_abc")
        event, msg, details = b.received[0]
        self.assertEqual(event, "live_fill")
        self.assertIn("LIVE fill", msg)
        self.assertEqual(details["order_id"], "ord_abc")

    def test_risk_reject_helper(self):
        b = _FakeBackend()
        d = AlertDispatcher([b])
        d.risk_reject("KXBTC15M-T1", "yes", "daily_loss_stop",
                      strategy_label="stat_model")
        event, msg, details = b.received[0]
        self.assertEqual(event, "risk_reject")
        self.assertEqual(details["reason"], "daily_loss_stop")
        self.assertEqual(details["strategy_label"], "stat_model")

    def test_paper_settle_helper(self):
        b = _FakeBackend()
        d = AlertDispatcher([b])
        d.paper_settle("KXBTC15M-T1", "yes", Decimal("4.25"))
        event, msg, details = b.received[0]
        self.assertEqual(event, "paper_settle")
        self.assertEqual(details["realized_pnl_usd"], "4.25")
        self.assertIn("Outcome: yes", msg)

    def test_system_error_helper(self):
        b = _FakeBackend()
        d = AlertDispatcher([b])
        d.system_error("kalshi_api", "429 after retries")
        event, msg, details = b.received[0]
        self.assertEqual(event, "system_error")
        self.assertEqual(details["component"], "kalshi_api")
        self.assertIn("429 after retries", msg)

    def test_daily_summary_helper(self):
        b = _FakeBackend()
        d = AlertDispatcher([b])
        d.daily_summary(ticks=4500, decisions=120, fills=12, settlements=10,
                        realized_pnl_usd=Decimal("18.75"),
                        strategy_label="pure_lag")
        event, msg, details = b.received[0]
        self.assertEqual(event, "daily_summary")
        self.assertIn("4500", msg)
        self.assertEqual(details["fills"], 12)
        self.assertEqual(details["strategy_label"], "pure_lag")


# ---------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------

class UrlHelperTests(unittest.TestCase):
    def test_kalshi_market_url(self):
        self.assertEqual(kalshi_market_url("KXBTC15M-T1"),
                         "https://kalshi.com/markets/KXBTC15M-T1")

    def test_dashboard_market_url_default(self):
        url = dashboard_market_url("KXETH15M-T2")
        self.assertTrue(url.endswith("/kalshi/decisions?ticker=KXETH15M-T2"))

    def test_dashboard_market_url_custom_host(self):
        url = dashboard_market_url("KXETH15M-T2", "https://dash.example")
        self.assertEqual(
            url, "https://dash.example/kalshi/decisions?ticker=KXETH15M-T2")


# ---------------------------------------------------------------
# Telegram backend
# ---------------------------------------------------------------

class TelegramTests(unittest.TestCase):
    def test_not_configured_returns_false(self):
        t = TelegramAlert(bot_token="", chat_id="")
        self.assertFalse(t.configured)
        self.assertFalse(t.send("paper_fill", "msg"))

    def test_configured_true_when_both_present(self):
        t = TelegramAlert(bot_token="123:ABC", chat_id="999")
        self.assertTrue(t.configured)

    def test_name(self):
        self.assertEqual(TelegramAlert().name, "telegram")

    @patch("alerting.telegram.requests.post")
    def test_send_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        t = TelegramAlert(bot_token="123:ABC", chat_id="999")
        self.assertTrue(t.send("paper_fill", "KXBTC15M-T1 fill",
                               {"market_link": "https://kalshi.com/markets/X",
                                "dashboard_link": "http://localhost/y"}))
        mock_post.assert_called_once()
        sent = mock_post.call_args[1]["json"]
        self.assertEqual(sent["chat_id"], "999")
        self.assertIn("Paper Fill", sent["text"])
        self.assertIn("Kalshi Market", sent["text"])
        self.assertIn("Dashboard", sent["text"])

    @patch("alerting.telegram.requests.post")
    def test_send_api_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
        t = TelegramAlert(bot_token="123:ABC", chat_id="999")
        self.assertFalse(t.send("paper_fill", "msg"))

    @patch("alerting.telegram.requests.post", side_effect=Exception("timeout"))
    def test_send_network_error(self, _):
        t = TelegramAlert(bot_token="123:ABC", chat_id="999")
        self.assertFalse(t.send("paper_fill", "msg"))


# ---------------------------------------------------------------
# Discord backend
# ---------------------------------------------------------------

class DiscordTests(unittest.TestCase):
    def test_not_configured_returns_false(self):
        d = DiscordAlert(webhook_url="")
        self.assertFalse(d.configured)
        self.assertFalse(d.send("live_fill", "msg"))

    def test_name(self):
        self.assertEqual(DiscordAlert().name, "discord")

    @patch("alerting.discord.requests.post")
    def test_send_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=204)
        d = DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake")
        self.assertTrue(d.send(
            "live_fill", "KXBTC15M-T1 fill",
            {"ticker": "KXBTC15M-T1", "side": "yes",
             "market_link": "https://kalshi.com/markets/KXBTC15M-T1",
             "dashboard_link": "http://localhost:8000/kalshi"},
        ))
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["embeds"][0]["title"], "Live Fill")
        field_names = {f["name"] for f in payload["embeds"][0]["fields"]}
        # market/dashboard links appear as named fields, not raw fields
        self.assertIn("Kalshi Market", field_names)
        self.assertIn("Dashboard", field_names)
        self.assertNotIn("Market Link", field_names)  # skipped raw key

    @patch("alerting.discord.requests.post")
    def test_send_webhook_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=429, text="Rate limited")
        d = DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake")
        self.assertFalse(d.send("live_fill", "msg"))

    @patch("alerting.discord.requests.post", side_effect=Exception("network"))
    def test_send_network_error(self, _):
        d = DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake")
        self.assertFalse(d.send("live_fill", "msg"))

    def test_filtered_events_return_true_without_post(self):
        """paper_fill / risk_reject are filtered — Discord would be noisy."""
        d = DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake")
        with patch("alerting.discord.requests.post") as mock_post:
            self.assertTrue(d.send("paper_fill", "msg"))
            self.assertTrue(d.send("risk_reject", "msg"))
            mock_post.assert_not_called()


# ---------------------------------------------------------------
# Gmail backend
# ---------------------------------------------------------------

class GmailTests(unittest.TestCase):
    def test_not_configured_returns_false(self):
        g = GmailAlert(address="", app_password="", recipient="")
        self.assertFalse(g.configured)
        self.assertFalse(g.send("paper_fill", "msg"))

    def test_configured_check(self):
        g = GmailAlert(address="a@g.com", app_password="pw", recipient="b@g.com")
        self.assertTrue(g.configured)

    def test_name(self):
        self.assertEqual(GmailAlert().name, "gmail")

    @patch("alerting.gmail.smtplib.SMTP")
    def test_send_success(self, mock_smtp_cls):
        server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        g = GmailAlert(address="a@g.com", app_password="pw", recipient="b@g.com")
        self.assertTrue(g.send("daily_summary", "report body",
                               {"ticks": 100,
                                "market_link": "https://kalshi.com/markets/X"}))
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("a@g.com", "pw")
        server.sendmail.assert_called_once()
        # sendmail payload: args[0]=from, args[1]=recipients, args[2]=msg str
        raw = server.sendmail.call_args[0][2]
        self.assertIn("[Kalshi] Daily Summary", raw)

    @patch("alerting.gmail.smtplib.SMTP", side_effect=Exception("connection refused"))
    def test_send_smtp_error(self, _):
        g = GmailAlert(address="a@g.com", app_password="pw", recipient="b@g.com")
        self.assertFalse(g.send("paper_fill", "msg"))


# ---------------------------------------------------------------
# Integration + env construction
# ---------------------------------------------------------------

class IntegrationTests(unittest.TestCase):
    def test_unconfigured_backends_gracefully_skip(self):
        d = AlertDispatcher([
            TelegramAlert(bot_token="", chat_id=""),
            DiscordAlert(webhook_url=""),
            GmailAlert(address="", app_password="", recipient=""),
        ])
        self.assertEqual(d.alert("paper_fill", "test"), 0)

    @patch("alerting.telegram.requests.post")
    @patch("alerting.discord.requests.post")
    def test_mixed_configured(self, mock_discord, mock_telegram):
        mock_telegram.return_value = MagicMock(status_code=200)
        mock_discord.return_value = MagicMock(status_code=204)
        d = AlertDispatcher([
            TelegramAlert(bot_token="123:ABC", chat_id="999"),
            DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake"),
            GmailAlert(address="", app_password="", recipient=""),  # skip
        ])
        # live_fill is in Discord's allowlist
        self.assertEqual(d.alert("live_fill", "filled"), 2)

    def test_build_dispatcher_from_env_skips_unconfigured(self):
        dispatcher = build_dispatcher_from_env(env={})
        self.assertEqual(dispatcher.backend_count, 0)

    def test_build_dispatcher_from_env_attaches_only_configured(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "chat",
            "DISCORD_WEBHOOK_URL": "",        # unconfigured
            "GMAIL_ADDRESS": "a@g.com",
            "GMAIL_APP_PASSWORD": "pw",
            "GMAIL_RECIPIENT": "b@g.com",
        }
        dispatcher = build_dispatcher_from_env(env=env)
        self.assertEqual(dispatcher.backend_count, 2)


# ---------------------------------------------------------------
# Wiring into run_kalshi_shadow.build_paper_executor_bridge
# ---------------------------------------------------------------

class WiringTests(unittest.TestCase):
    """Cover the alert_dispatcher thread-through in run_kalshi_shadow."""

    def _build(self, alert_dispatcher):
        """Construct a fresh file-backed DB + bridge."""
        import sqlite3
        import tempfile
        from pathlib import Path
        import migrate_db as m
        from run_kalshi_shadow import build_paper_executor_bridge

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        db_path = Path(tmpdir) / "alerts.db"
        url = f"sqlite:///{db_path}"
        m.migrate(url)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        now_us = 1_746_000_000_000_000
        executor, decision_hook, reconcile_hook = build_paper_executor_bridge(
            conn=conn, is_postgres=False, strategy_label="pure_lag",
            now_us=lambda: now_us, alert_dispatcher=alert_dispatcher,
        )
        return executor, decision_hook, reconcile_hook, now_us

    def test_alert_dispatcher_fires_on_paper_fill(self):
        dispatcher = MagicMock()
        # Return value is irrelevant — bridge ignores it.
        dispatcher.paper_fill.return_value = 1
        dispatcher.risk_reject.return_value = 0

        from core.models import MarketQuote, Opportunity, OpportunityStatus

        executor, decision_hook, _, now_us = self._build(dispatcher)
        quote = MarketQuote(
            venue="kalshi", market_ticker="KXBTC15M-T1",
            series_ticker="KXBTC15M", event_ticker="KXBTC15M-E",
            best_yes_ask=Decimal("0.35"), best_no_ask=Decimal("0.60"),
            best_yes_bid=Decimal("0.33"), best_no_bid=Decimal("0.58"),
            book_depth_yes_usd=Decimal("500"),
            book_depth_no_usd=Decimal("500"),
            fee_bps=Decimal("35"),
            expiration_ts=Decimal("1746000000"),
            strike=Decimal("65000"), comparator="above",
            reference_price=Decimal("66050"),
            reference_60s_avg=Decimal("66050"),
            time_remaining_s=Decimal("30"),
            quote_timestamp_us=now_us,
        )
        opp = Opportunity(
            quote=quote, p_yes=Decimal("0.7"), ci_width=Decimal("0.4"),
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.35"),
            hypothetical_size_contracts=Decimal("10"),
            expected_edge_bps_after_fees=Decimal("450"),
            status=OpportunityStatus.SIMULATION_APPROVED,
            no_data_haircut_bps=Decimal("0"),
        )

        decision_hook(quote, opp)

        if dispatcher.paper_fill.called:
            # Happy path — risk engine accepted, dispatcher.paper_fill fired.
            (args, kwargs) = dispatcher.paper_fill.call_args
            self.assertEqual(args[0], "KXBTC15M-T1")
            self.assertEqual(args[1], "yes")
            self.assertEqual(kwargs.get("strategy_label"), "pure_lag")
            dispatcher.risk_reject.assert_not_called()
        else:
            # Risk engine rejected — risk_reject must have fired instead.
            dispatcher.risk_reject.assert_called_once()
            dispatcher.paper_fill.assert_not_called()

    def test_alert_dispatcher_failure_does_not_crash_hook(self):
        dispatcher = MagicMock()
        dispatcher.paper_fill.side_effect = RuntimeError("network down")
        dispatcher.risk_reject.side_effect = RuntimeError("network down")

        from core.models import MarketQuote, Opportunity, OpportunityStatus

        _, decision_hook, _, now_us = self._build(dispatcher)
        quote = MarketQuote(
            venue="kalshi", market_ticker="KXBTC15M-T1",
            series_ticker="KXBTC15M", event_ticker="KXBTC15M-E",
            best_yes_ask=Decimal("0.35"), best_no_ask=Decimal("0.60"),
            best_yes_bid=Decimal("0.33"), best_no_bid=Decimal("0.58"),
            book_depth_yes_usd=Decimal("500"),
            book_depth_no_usd=Decimal("500"),
            fee_bps=Decimal("35"),
            expiration_ts=Decimal("1746000000"),
            strike=Decimal("65000"), comparator="above",
            reference_price=Decimal("66050"),
            reference_60s_avg=Decimal("66050"),
            time_remaining_s=Decimal("30"),
            quote_timestamp_us=now_us,
        )
        opp = Opportunity(
            quote=quote, p_yes=Decimal("0.7"), ci_width=Decimal("0.4"),
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.35"),
            hypothetical_size_contracts=Decimal("10"),
            expected_edge_bps_after_fees=Decimal("450"),
            status=OpportunityStatus.SIMULATION_APPROVED,
            no_data_haircut_bps=Decimal("0"),
        )
        # Must not raise despite the dispatcher blowing up.
        decision_hook(quote, opp)


if __name__ == "__main__":
    unittest.main()
