"""Cover `src/kalshi_rest.py` — signing, request plumbing, cursor pagination."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_rest import (
    KalshiRestClient,
    REST_HOSTS,
    load_private_key,
    sign_message,
)
from platform_adapters import KalshiAPIError


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def pem_file(tmp_path, rsa_key):
    pem = tmp_path / "test.pem"
    pem.write_bytes(rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return pem


# --------- signing ---------

def test_sign_message_is_base64_and_verifies_against_pubkey(rsa_key):
    msg = "1699564800000GET/trade-api/v2/portfolio/balance"
    sig_b64 = sign_message(rsa_key, msg)
    # base64 round-trip.
    sig_bytes = base64.b64decode(sig_b64)
    # Verify with the public key using the same PSS params.
    rsa_key.public_key().verify(
        sig_bytes, msg.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256.digest_size,
        ),
        hashes.SHA256(),
    )


def test_sign_message_returns_ascii_string(rsa_key):
    sig = sign_message(rsa_key, "hello")
    assert isinstance(sig, str)
    assert sig == sig.encode("ascii").decode("ascii")


# --------- load_private_key ---------

def test_load_private_key_valid_pem(pem_file):
    key = load_private_key(pem_file)
    assert key.key_size == 2048


def test_load_private_key_non_rsa_rejected(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    pem = tmp_path / "ed.pem"
    k = ed25519.Ed25519PrivateKey.generate()
    pem.write_bytes(k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    with pytest.raises(ValueError, match="not an RSA"):
        load_private_key(pem)


# --------- client construction + headers ---------

def test_client_rejects_unknown_env(rsa_key):
    with pytest.raises(ValueError, match="demo|prod"):
        KalshiRestClient(api_key_id="x", private_key=rsa_key, env="staging")


def test_from_env_requires_key_id_and_pem(monkeypatch, pem_file):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(pem_file))
    with pytest.raises(RuntimeError, match="KALSHI_API_KEY_ID"):
        KalshiRestClient.from_env()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id-x")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/nope/missing.pem")
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY_PATH"):
        KalshiRestClient.from_env()


def test_auth_headers_populate_three_required_headers(rsa_key):
    c = KalshiRestClient(api_key_id="id-abc", private_key=rsa_key, env="demo")
    h = c._auth_headers("GET", "/trade-api/v2/portfolio/balance")
    assert h["KALSHI-ACCESS-KEY"] == "id-abc"
    assert h["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    assert h["KALSHI-ACCESS-SIGNATURE"]
    assert base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])  # round-trip OK


# --------- request plumbing (mocked session) ---------

def _mock_response(status: int, body: dict | None = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"x" if (body or text) else b""
    resp.json.return_value = body
    resp.text = text
    return resp


def test_request_authenticates_by_default(rsa_key):
    session = MagicMock()
    session.request.return_value = _mock_response(200, {"ok": True})
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    c.request("GET", "/portfolio/balance")
    args, kwargs = session.request.call_args
    assert "KALSHI-ACCESS-KEY" in kwargs["headers"]


def test_request_skips_auth_when_public(rsa_key):
    session = MagicMock()
    session.request.return_value = _mock_response(200, {})
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    c.request("GET", "/exchange/schedule", authenticated=False)
    _, kwargs = session.request.call_args
    assert "KALSHI-ACCESS-KEY" not in (kwargs["headers"] or {})


def test_request_raises_kalshi_api_error_on_4xx(rsa_key):
    session = MagicMock()
    session.request.return_value = _mock_response(429, text="rate limited")
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    with pytest.raises(KalshiAPIError) as exc:
        c.request("GET", "/portfolio/balance")
    assert exc.value.status == 429
    assert "rate limited" in exc.value.response_body


def test_request_raises_on_transport_error(rsa_key):
    import requests
    session = MagicMock()
    session.request.side_effect = requests.ConnectionError("boom")
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    with pytest.raises(KalshiAPIError) as exc:
        c.request("GET", "/portfolio/balance")
    assert exc.value.status is None


# --------- paginate() ---------

def test_paginate_follows_cursor_and_yields_items(rsa_key):
    session = MagicMock()
    session.request.side_effect = [
        _mock_response(200, {"markets": [{"t": 1}], "cursor": "pg2"}),
        _mock_response(200, {"markets": [{"t": 2}, {"t": 3}], "cursor": ""}),
    ]
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    out = list(c.paginate("GET", "/historical/markets", collection_key="markets"))
    assert [m["t"] for m in out] == [1, 2, 3]
    # Second request should have cursor param set.
    _, kwargs2 = session.request.call_args_list[1]
    assert kwargs2["params"]["cursor"] == "pg2"


def test_paginate_stops_when_max_pages_reached(rsa_key):
    session = MagicMock()
    session.request.return_value = _mock_response(200, {"markets": [{"t": 1}], "cursor": "x"})
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    out = list(c.paginate(
        "GET", "/historical/markets",
        collection_key="markets", max_pages=3,
    ))
    assert len(out) == 3  # 3 pages × 1 item each


def test_historical_markets_passes_series_filter(rsa_key):
    session = MagicMock()
    session.request.return_value = _mock_response(200, {"markets": [], "cursor": ""})
    c = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo",
                         session=session)
    list(c.historical_markets(series_ticker="KXBTC15M", min_close_ts=1, max_close_ts=2))
    _, kwargs = session.request.call_args
    p = kwargs["params"]
    assert p["series_ticker"] == "KXBTC15M"
    assert p["min_close_ts"] == 1
    assert p["max_close_ts"] == 2


def test_host_mapping_for_demo_and_prod(rsa_key):
    c_demo = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="demo")
    c_prod = KalshiRestClient(api_key_id="k", private_key=rsa_key, env="prod")
    assert c_demo.host == REST_HOSTS["demo"]
    assert c_prod.host == REST_HOSTS["prod"]
