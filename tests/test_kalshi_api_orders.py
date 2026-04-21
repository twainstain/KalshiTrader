"""Cover `kalshi_api.py` order endpoints (P2-M2).

The `KalshiAPIClient.request()` method is already covered elsewhere; this
file asserts the order-lifecycle convenience methods build the correct
HTTP shape and validate their inputs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kalshi_api import KalshiAPIClient


def _client() -> KalshiAPIClient:
    """Client with a stubbed private_key (we never actually sign — request is mocked)."""
    pk = MagicMock()
    return KalshiAPIClient(api_key_id="k1", private_key=pk, env="demo")


class TestCreateOrder:
    def test_buy_yes_emits_yes_price(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={"order": {"order_id": "X"}}) as req:
            c.create_order(
                ticker="KXBTC-T1", action="buy", side="yes",
                count=5, client_order_id="c1", yes_price=55,
            )
        assert req.call_count == 1
        args, kwargs = req.call_args
        assert args == ("POST", "/portfolio/orders")
        body = kwargs["json_body"]
        assert body == {
            "ticker": "KXBTC-T1",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "client_order_id": "c1",
            "yes_price": 55,
        }
        assert kwargs["authenticated"] is True

    def test_buy_no_emits_no_price(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={}) as req:
            c.create_order(
                ticker="KXBTC-T1", action="buy", side="no",
                count=3, client_order_id="c2", no_price=42,
            )
        body = req.call_args.kwargs["json_body"]
        assert body["side"] == "no"
        assert body["no_price"] == 42
        assert "yes_price" not in body

    def test_rejects_bad_action(self) -> None:
        c = _client()
        with pytest.raises(ValueError, match="action"):
            c.create_order(
                ticker="T", action="pizza", side="yes", count=1,
                client_order_id="c", yes_price=50,
            )

    def test_rejects_bad_side(self) -> None:
        c = _client()
        with pytest.raises(ValueError, match="side"):
            c.create_order(
                ticker="T", action="buy", side="maybe", count=1,
                client_order_id="c", yes_price=50,
            )

    def test_rejects_zero_count(self) -> None:
        c = _client()
        with pytest.raises(ValueError, match="count"):
            c.create_order(
                ticker="T", action="buy", side="yes", count=0,
                client_order_id="c", yes_price=50,
            )

    def test_rejects_missing_price(self) -> None:
        c = _client()
        with pytest.raises(ValueError, match="yes_price/no_price"):
            c.create_order(
                ticker="T", action="buy", side="yes", count=1,
                client_order_id="c",
            )


class TestCancelAndQueryEndpoints:
    def test_cancel_order_hits_correct_path(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={"ok": True}) as req:
            c.cancel_order("ORD-42")
        assert req.call_args.args == ("DELETE", "/portfolio/orders/ORD-42")
        assert req.call_args.kwargs["authenticated"] is True

    def test_get_order_hits_correct_path(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={"order": {}}) as req:
            c.get_order("ORD-42")
        assert req.call_args.args == ("GET", "/portfolio/orders/ORD-42")

    def test_get_fills_forwards_filters(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={"fills": []}) as req:
            c.get_fills(ticker="KXBTC-T1", order_id="ORD-42", limit=50)
        kwargs = req.call_args.kwargs
        assert kwargs["params"] == {
            "limit": 50, "ticker": "KXBTC-T1", "order_id": "ORD-42",
        }

    def test_get_positions_forwards_ticker(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={"positions": []}) as req:
            c.get_positions(ticker="KXBTC-T1")
        assert req.call_args.kwargs["params"]["ticker"] == "KXBTC-T1"

    def test_get_settlements_defaults_no_ticker(self) -> None:
        c = _client()
        with patch.object(c, "request", return_value={"settlements": []}) as req:
            c.get_settlements()
        assert "ticker" not in req.call_args.kwargs["params"]
        assert req.call_args.kwargs["params"]["limit"] == 100
