"""Thin Kalshi REST client — direct `requests` + RSA-PSS signing.

Exists to sidestep `kalshi_python_sync` 3.2.0 schema bugs where pydantic
rejects null-in-int fields on some market responses. For Phase 1 we want
JSON dicts, not validated models, so this client returns `dict`/`list`
shapes verbatim.

Signing follows Kalshi docs §3.2:
    sig_msg = f"{timestamp_ms}{METHOD}{path_without_query}"
    signature = base64(RSA-PSS(SHA-256, MGF1(SHA-256), salt=DIGEST_LEN)(sig_msg))

Headers per request:
    KALSHI-ACCESS-KEY: <api_key_id>
    KALSHI-ACCESS-TIMESTAMP: <ms>
    KALSHI-ACCESS-SIGNATURE: <base64 sig>

Only used for **authenticated** reads in Phase 1 (historical markets/trades).
Public endpoints (`/series`, `/markets`, `/markets/{t}/orderbook`) also work
through this client — unauthenticated calls skip the signing step.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from platform_adapters import KalshiAPIError


logger = logging.getLogger(__name__)


REST_HOSTS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}


def sign_message(private_key: RSAPrivateKey, message: str) -> str:
    """RSA-PSS (SHA-256, MGF1-SHA256, salt=digest-length) → base64 string."""
    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256.digest_size,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def load_private_key(path: str | Path) -> RSAPrivateKey:
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(
        data, password=None, backend=default_backend(),
    )
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("PEM is not an RSA private key")
    return key


@dataclass
class KalshiRestClient:
    """Authenticated Kalshi REST client."""
    api_key_id: str
    private_key: RSAPrivateKey
    env: str = "demo"
    timeout_s: float = 30.0
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.env not in REST_HOSTS:
            raise ValueError(f"env must be demo|prod, got {self.env!r}")
        if self.session is None:
            self.session = requests.Session()

    @classmethod
    def from_env(cls, *, env: str | None = None,
                 api_key_id: str | None = None,
                 private_key_path: str | Path | None = None) -> "KalshiRestClient":
        import os
        env = env or os.environ.get("KALSHI_ENV", "demo")
        key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID", "")
        pem_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not key_id:
            raise RuntimeError("KALSHI_API_KEY_ID is unset")
        if not pem_path or not Path(pem_path).is_file():
            raise RuntimeError(f"KALSHI_PRIVATE_KEY_PATH invalid: {pem_path!r}")
        return cls(
            api_key_id=key_id,
            private_key=load_private_key(pem_path),
            env=env,
        )

    # ---- low-level ----

    @property
    def host(self) -> str:
        return REST_HOSTS[self.env]

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build signed headers. `path` is the full URL path without query."""
        ts_ms = str(int(time.time() * 1000))
        msg = f"{ts_ms}{method.upper()}{path}"
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sign_message(self.private_key, msg),
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        authenticated: bool = True,
    ) -> Any:
        """Issue a single request. `path` is relative to `/trade-api/v2`."""
        # Path used for signing must NOT include query string and must include
        # the `/trade-api/v2` prefix — the signature spec is based on the
        # full server-side path.
        full_path = f"/trade-api/v2{path}" if not path.startswith("/trade-api/v2") else path
        url = f"{REST_HOSTS[self.env].rstrip('/trade-api/v2')}{full_path}"
        headers = {}
        if authenticated:
            headers.update(self._auth_headers(method, full_path))
        try:
            resp = self.session.request(  # type: ignore[union-attr]
                method=method, url=url, headers=headers, params=params,
                json=json_body, timeout=self.timeout_s,
            )
        except requests.RequestException as e:
            raise KalshiAPIError(f"transport error: {e}", status=None) from e

        if resp.status_code >= 400:
            raise KalshiAPIError(
                f"{method} {path} → {resp.status_code}",
                status=resp.status_code, response_body=resp.text[:2000],
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---- higher-level helpers ----

    def paginate(
        self, method: str, path: str, *,
        params: dict[str, Any] | None = None,
        collection_key: str,
        authenticated: bool = True,
        page_size: int = 200,
        max_pages: int = 10_000,
    ) -> Iterable[dict]:
        """Yield items from a cursor-paginated endpoint.

        Kalshi cursor semantics: each response includes a `cursor` field;
        pass it back as a `cursor` query param to get the next page. Empty
        string or missing cursor = done.
        """
        call_params = dict(params or {})
        call_params.setdefault("limit", page_size)
        for _ in range(max_pages):
            resp = self.request(
                method, path, params=call_params, authenticated=authenticated,
            )
            items = resp.get(collection_key, []) if isinstance(resp, dict) else []
            for item in items:
                yield item
            cursor = (resp or {}).get("cursor", "") if isinstance(resp, dict) else ""
            if not cursor:
                return
            call_params["cursor"] = cursor
        logger.warning("paginate(%s) hit max_pages=%d — stopping early",
                       path, max_pages)

    def get_exchange_schedule(self) -> dict:
        return self.request("GET", "/exchange/schedule", authenticated=False)

    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def historical_markets(
        self, *, series_ticker: str | None = None,
        min_close_ts: int | None = None, max_close_ts: int | None = None,
    ) -> Iterable[dict]:
        params: dict[str, Any] = {}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        yield from self.paginate(
            "GET", "/historical/markets", params=params,
            collection_key="markets",
        )

    def historical_trades(
        self, *, ticker: str,
        min_ts: int | None = None, max_ts: int | None = None,
    ) -> Iterable[dict]:
        params: dict[str, Any] = {"ticker": ticker}
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        yield from self.paginate(
            "GET", "/historical/trades", params=params,
            collection_key="trades",
        )
