"""Ingest Kalshi Ideas leaderboard, profile, and trade payloads into the DB.

This script is meant for the social/Ideas surface rather than the exchange
orderbook. In practice, Kalshi's profile pages are client-rendered and often
fronted by a browser checkpoint, so the most reliable workflow is:

1. Capture the underlying leaderboard/profile/trades JSON from the browser's
   Network tab, or
2. Supply the real endpoint templates once they're known.

The normalizers are intentionally tolerant because Kalshi's internal payload
shape may change. We score/scan for likely row collections instead of binding
to one exact schema.

Examples:
    python3.11 scripts/kalshi_ideas_pull.py \
      --leaderboard-json profits=/tmp/profits.json \
      --leaderboard-json winning_streak=/tmp/winning_streak.json \
      --profile-json-dir /tmp/profiles \
      --trades-json-dir /tmp/trades

    python3.11 scripts/kalshi_ideas_pull.py \
      --leaderboard-url-template 'https://host/api/leaderboard?category={category}' \
      --profile-url-template 'https://host/api/profiles/{slug}' \
      --trades-url-template 'https://host/api/profiles/{slug}/trades'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from requests import RequestException


logger = logging.getLogger(__name__)


DEFAULT_CATEGORIES: tuple[str, ...] = ("profits", "num_markets_traded")
DEFAULT_PROFILE_URL_TEMPLATE = "https://api.elections.kalshi.com/v1/social/profile?nickname={slug}"
DEFAULT_PROFILE_METRICS_URL_TEMPLATE = "https://api.elections.kalshi.com/v1/social/profile/metrics?nickname={slug}"
DEFAULT_TRADES_URL_TEMPLATE = "https://api.elections.kalshi.com/v1/social/trades?nickname={slug}&page_size={page_size}"
DEFAULT_LEADERBOARD_LIMIT = 20
DEFAULT_LEADERBOARD_TIME_PERIOD = "monthly"
SUPPORTED_LEADERBOARD_TIME_PERIODS: tuple[str, ...] = (
    "daily", "weekly", "monthly", "all_time",
)
CATEGORY_ALIASES = {
    "profit": "profits",
    "profits": "profits",
    "pnl": "profits",
    "projected_pnl": "projected_pnl",
    "monthly_pnl": "projected_pnl",
    "num_markets_traded": "num_markets_traded",
    "markets_traded": "num_markets_traded",
    "trade_count": "num_markets_traded",
    "winning strike": "winning_streak",
    "winning-streak": "winning_streak",
    "winning streak": "winning_streak",
    "winning_strike": "winning_streak",
    "winningstreak": "winning_streak",
    "winning_streak": "winning_streak",
    "streak": "winning_streak",
}

LEADERBOARD_METRIC_BY_CATEGORY = {
    "profits": "projected_pnl",
    "projected_pnl": "projected_pnl",
    "num_markets_traded": "num_markets_traded",
}


def normalize_category(value: str) -> str:
    out = CATEGORY_ALIASES.get(value.strip().lower(), value.strip().lower())
    if not out:
        raise ValueError("category cannot be empty")
    return out


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, default=str, sort_keys=True)


def _normalize_slug(value: Any) -> str:
    slug = _stringify(value).strip()
    if slug.startswith("@"):
        slug = slug[1:]
    return slug


def _to_us(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        numeric = int(value)
        if numeric > 1_000_000_000_000_000:
            return numeric
        if numeric > 1_000_000_000_000:
            return numeric * 1_000
        if numeric > 1_000_000_000:
            return numeric * 1_000_000
        return numeric * 1_000_000
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            if text.isdigit():
                return _to_us(int(text))
            return _to_us(float(text))
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return 0
            return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)
    return 0


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def _walk(payload: Any, *, max_depth: int = 5, _depth: int = 0):
    if _depth > max_depth:
        return
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk(value, max_depth=max_depth, _depth=_depth + 1)
        return
    if isinstance(payload, list):
        for item in payload:
            yield from _walk(item, max_depth=max_depth, _depth=_depth + 1)


def _list_candidates(payload: Any) -> list[list[dict[str, Any]]]:
    candidates: list[list[dict[str, Any]]] = []
    if isinstance(payload, list) and payload and all(isinstance(x, dict) for x in payload):
        candidates.append(payload)
    for mapping in _walk(payload):
        for value in mapping.values():
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                candidates.append(value)
    return candidates


def _row_score(rows: list[dict[str, Any]], keywords: set[str]) -> int:
    if not rows:
        return 0
    score = 0
    sample = rows[:3]
    for row in sample:
        flat_keys = set(row)
        for nested_key in ("user", "profile", "stats", "metadata"):
            nested = row.get(nested_key)
            if isinstance(nested, dict):
                flat_keys |= {f"{nested_key}.{key}" for key in nested}
                flat_keys |= set(nested)
        score += len(flat_keys & keywords)
    return score


def _best_rows(payload: Any, *, keywords: set[str]) -> list[dict[str, Any]]:
    candidates = _list_candidates(payload)
    if not candidates:
        return []
    ranked = sorted(
        candidates,
        key=lambda rows: _row_score(rows, keywords),
        reverse=True,
    )
    return ranked[0]


def _best_mapping(payload: Any, *, keywords: set[str]) -> dict[str, Any]:
    candidates = list(_walk(payload))
    if not candidates:
        return {}
    ranked = sorted(
        candidates,
        key=lambda mapping: len(set(mapping) & keywords),
        reverse=True,
    )
    return ranked[0]


def _parse_concatenated_json(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    idx = 0
    docs: list[Any] = []
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        docs.append(obj)
        idx = end
    return docs


def _parse_next_rsc_stream(text: str) -> dict[str, Any]:
    records: dict[str, Any] = {}
    parsed_values: list[Any] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, payload = line.split(":", 1)
        key = key.strip()
        payload = payload.strip()
        if not key or not payload:
            continue
        candidate = payload.rstrip(",")
        if not candidate.startswith(("{", "[", '"')):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        records[key] = value
        parsed_values.append(value)
    if not records:
        raise ValueError("text did not contain parseable Next.js RSC records")
    return {
        "__format__": "next_rsc",
        "records": records,
        "values": parsed_values,
    }


def load_json_source(source: str, *, session: requests.Session | None = None) -> Any:
    def parse_text(text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            # Browser "copy all responses" workflows sometimes concatenate
            # multiple JSON pages into one file. Support that directly so the
            # trade ingester can replay paginated captures without cleanup.
            if "Extra data" in str(exc):
                try:
                    return _parse_concatenated_json(text)
                except json.JSONDecodeError:
                    pass
            # Next.js app-router endpoints often return RSC streams shaped like
            # `0:{...}\n1:"$Sreact.fragment"\n...` rather than plain JSON.
            try:
                return _parse_next_rsc_stream(text)
            except ValueError:
                raise exc

    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        session = session or requests.Session()
        resp = session.get(source, timeout=30.0)
        resp.raise_for_status()
        return parse_text(resp.text)
    return parse_text(Path(source).read_text())


def parse_keyed_sources(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"expected KEY=SOURCE, got {value!r}")
        key, source = value.split("=", 1)
        out[key.strip()] = source.strip()
    return out


def render_source(
    explicit: dict[str, str],
    key: str,
    *,
    template: str | None = None,
    dir_path: str | None = None,
) -> str | None:
    if key in explicit:
        return explicit[key]
    if dir_path:
        for suffix in (".json", ".txt", ".rsc"):
            candidate = Path(dir_path) / f"{key}{suffix}"
            if candidate.is_file():
                return str(candidate)
    if template:
        return template.format(category=key, slug=key)
    return None


def with_query_param(url: str, key: str, value: str | int | None) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if value is None or value == "":
        query.pop(key, None)
    else:
        query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_default_leaderboard_url(
    category: str,
    *,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    time_period: str = DEFAULT_LEADERBOARD_TIME_PERIOD,
) -> str | None:
    metric_name = LEADERBOARD_METRIC_BY_CATEGORY.get(category)
    if not metric_name:
        return None
    return (
        "https://api.elections.kalshi.com/v1/social/leaderboard?"
        + urlencode(
            {
                "metric_name": metric_name,
                "limit": limit,
                "time_period": time_period,
            },
        )
    )


def normalize_leaderboard_entries(
    payload: Any,
    *,
    category: str,
    time_period: str = DEFAULT_LEADERBOARD_TIME_PERIOD,
) -> list[dict[str, Any]]:
    if isinstance(payload, list) and payload and all(isinstance(x, dict) for x in payload):
        entries: list[dict[str, Any]] = []
        for item in payload:
            entries.extend(
                normalize_leaderboard_entries(
                    item,
                    category=category,
                    time_period=time_period,
                ),
            )
        return entries

    rows = _best_rows(
        payload,
        keywords={
            "rank", "position", "username", "nickname", "display_name",
            "social_id", "profit", "profits", "pnl", "winning_streak",
            "streak", "predictions", "wins", "value", "profile_image_path",
        },
    )
    entries: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        user = _as_dict(row.get("user"))
        profile = _as_dict(row.get("profile"))
        stats = _as_dict(row.get("stats"))
        metric_value = _stringify(_coalesce(row.get("value"), stats.get("value")))
        slug = _normalize_slug(_coalesce(
            row.get("profile_slug"),
            row.get("slug"),
            user.get("profile_slug"),
            user.get("slug"),
            profile.get("profile_slug"),
            profile.get("slug"),
            row.get("nickname"),
            user.get("nickname"),
            row.get("username"),
            user.get("username"),
        ))
        if not slug:
            continue
        entries.append({
            "category": category,
            "time_period": time_period,
            "rank": int(_coalesce(row.get("rank"), row.get("position"), idx) or idx),
            "profile_slug": slug,
            "username": _stringify(_coalesce(
                row.get("username"), user.get("username"), row.get("nickname"), user.get("nickname"),
            )),
            "display_name": _stringify(_coalesce(
                row.get("display_name"),
                user.get("display_name"),
                profile.get("display_name"),
                row.get("name"),
                row.get("nickname"),
            )),
            "social_id": _stringify(_coalesce(
                row.get("social_id"), user.get("social_id"), profile.get("social_id"), user.get("id"),
            )),
            "profile_image_path": _stringify(_coalesce(
                row.get("profile_image_path"),
                profile.get("profile_image_path"),
                user.get("profile_image_path"),
            )),
            "metric_value": metric_value,
            "profit_usd": _stringify(_coalesce(
                row.get("profit_usd"),
                row.get("profit"),
                row.get("profits"),
                row.get("pnl"),
                metric_value if category in {"profits", "projected_pnl"} else None,
                stats.get("profit_usd"), stats.get("profit"), stats.get("pnl"),
            )),
            "winning_streak": _stringify(_coalesce(
                row.get("winning_streak"), row.get("streak"), stats.get("winning_streak"), stats.get("streak"),
            )),
            "total_predictions": _stringify(_coalesce(
                row.get("total_predictions"),
                row.get("predictions"),
                row.get("trade_count"),
                metric_value if category == "num_markets_traded" else None,
                stats.get("total_predictions"), stats.get("predictions"),
            )),
            "correct_predictions": _stringify(_coalesce(
                row.get("correct_predictions"), row.get("wins"), stats.get("correct_predictions"), stats.get("wins"),
            )),
            "is_anonymous": _to_int(row.get("is_anonymous")),
            "raw_json": json.dumps(row, default=str, sort_keys=True),
        })
    return entries


def normalize_profile(payload: Any, *, slug_hint: str) -> dict[str, Any]:
    if isinstance(payload, list) and payload and all(isinstance(x, dict) for x in payload):
        payload = payload[0]

    root = _as_dict(payload)
    mapping = _best_mapping(
        payload,
        keywords={
            "profile_slug", "slug", "username", "nickname", "display_name",
            "description", "bio", "social_id", "profile_image_url",
            "avatar_url", "profit", "pnl", "win_rate", "predictions",
        },
    )
    if not mapping:
        mapping = root
    social_profile = _as_dict(root.get("social_profile"))
    if not social_profile and {"nickname", "social_id"} & set(mapping):
        social_profile = mapping
    inner_circle = _as_dict(root.get("inner_circle"))
    stats = _as_dict(mapping.get("stats"))
    user = _as_dict(mapping.get("user"))
    slug = _normalize_slug(_coalesce(
        social_profile.get("nickname"),
        mapping.get("profile_slug"),
        mapping.get("slug"),
        user.get("profile_slug"),
        user.get("slug"),
        mapping.get("nickname"),
        mapping.get("username"),
        slug_hint,
    ))
    return {
        "profile_slug": slug or slug_hint,
        "username": _stringify(_coalesce(
            mapping.get("username"),
            user.get("username"),
            social_profile.get("nickname"),
            mapping.get("nickname"),
            user.get("nickname"),
            slug_hint,
        )),
        "display_name": _stringify(_coalesce(
            mapping.get("display_name"),
            user.get("display_name"),
            mapping.get("name"),
            user.get("name"),
            social_profile.get("nickname"),
            slug_hint,
        )),
        "social_id": _stringify(_coalesce(
            social_profile.get("social_id"),
            mapping.get("social_id"),
            user.get("social_id"),
            mapping.get("id"),
            user.get("id"),
        )),
        "bio": _stringify(_coalesce(
            social_profile.get("description"),
            mapping.get("bio"),
            mapping.get("description"),
            user.get("bio"),
            user.get("description"),
        )),
        "profile_image_url": _stringify(_coalesce(
            mapping.get("profile_image_url"), mapping.get("avatar_url"), mapping.get("image_url"),
            user.get("profile_image_url"), user.get("avatar_url"),
        )),
        "profile_image_path": _stringify(social_profile.get("profile_image_path")),
        "pending_profile_image_path": _stringify(social_profile.get("pending_profile_image_path")),
        "total_trades": _stringify(_coalesce(
            mapping.get("total_trades"), stats.get("total_trades"), stats.get("trade_count"),
        )),
        "total_predictions": _stringify(_coalesce(
            mapping.get("total_predictions"), mapping.get("predictions"), stats.get("total_predictions"),
            stats.get("predictions"),
        )),
        "correct_predictions": _stringify(_coalesce(
            mapping.get("correct_predictions"), mapping.get("wins"), stats.get("correct_predictions"), stats.get("wins"),
        )),
        "win_rate": _stringify(_coalesce(
            mapping.get("win_rate"), stats.get("win_rate"),
        )),
        "profit_usd": _stringify(_coalesce(
            mapping.get("profit_usd"), mapping.get("profit"), mapping.get("pnl"),
            stats.get("profit_usd"), stats.get("profit"), stats.get("pnl"),
        )),
        "follower_count": _to_int(social_profile.get("follower_count")),
        "following_count": _to_int(social_profile.get("following_count")),
        "posts_count": _to_int(social_profile.get("posts_count")),
        "profile_view_count": _to_int(social_profile.get("profile_view_count")),
        "joined_at": _stringify(social_profile.get("joined_at")),
        "top_categories_json": json.dumps(social_profile.get("top_categories") or [], default=str),
        "blocked": _to_int(social_profile.get("blocked")),
        "inner_circle_enabled": _to_int(inner_circle.get("enabled")),
        "inner_circle_viewer_status": _stringify(inner_circle.get("viewer_status")),
        "metrics_volume": 0,
        "metrics_volume_fp": "",
        "metrics_pnl": 0,
        "metrics_num_markets_traded": 0,
        "metrics_raw_json": "",
        "raw_json": json.dumps(payload, default=str, sort_keys=True),
    }


def merge_profile_metrics(profile: dict[str, Any], payload: Any) -> dict[str, Any]:
    root = _as_dict(payload)
    metrics = _as_dict(root.get("metrics"))
    if not metrics:
        metrics = root
    merged = dict(profile)
    merged["metrics_volume"] = _to_int(metrics.get("volume"))
    merged["metrics_volume_fp"] = _stringify(metrics.get("volume_fp"))
    merged["metrics_pnl"] = _to_int(metrics.get("pnl"))
    merged["metrics_num_markets_traded"] = _to_int(metrics.get("num_markets_traded"))
    merged["metrics_raw_json"] = json.dumps(payload, default=str, sort_keys=True)
    if not merged.get("total_trades"):
        merged["total_trades"] = _stringify(metrics.get("num_markets_traded"))
    return merged


def _related_role(trade: dict[str, Any], *, profile_slug: str, social_id: str) -> str:
    lower_slug = profile_slug.lower()
    if social_id and _stringify(trade.get("taker_social_id")) == social_id:
        return "taker"
    if social_id and _stringify(trade.get("maker_social_id")) == social_id:
        return "maker"
    if _stringify(trade.get("taker_nickname")).lower() == lower_slug:
        return "taker"
    if _stringify(trade.get("maker_nickname")).lower() == lower_slug:
        return "maker"
    return ""


def _fallback_trade_id(trade: dict[str, Any], *, profile_slug: str) -> str:
    pieces = [
        profile_slug,
        _stringify(trade.get("market_id")),
        _stringify(trade.get("ticker")),
        _stringify(trade.get("price_dollars")),
        _stringify(trade.get("count_fp")),
        _stringify(trade.get("create_date") or trade.get("created_time")),
        _stringify(trade.get("maker_nickname")),
        _stringify(trade.get("taker_nickname")),
    ]
    return hashlib.sha256("|".join(pieces).encode("utf-8")).hexdigest()


def normalize_trades(payload: Any, *, profile_slug: str, social_id: str = "") -> list[dict[str, Any]]:
    if isinstance(payload, list) and payload and all(isinstance(x, dict) for x in payload):
        deduped: dict[str, dict[str, Any]] = {}
        for item in payload:
            for trade in normalize_trades(item, profile_slug=profile_slug, social_id=social_id):
                deduped[trade["trade_id"]] = trade
        return list(deduped.values())

    rows = _best_rows(
        payload,
        keywords={
            "trade_id", "market_id", "ticker", "price_dollars", "count_fp",
            "maker_nickname", "taker_nickname", "maker_social_id",
            "taker_social_id", "create_date", "created_time",
        },
    )
    trades: list[dict[str, Any]] = []
    for row in rows:
        trade_id = _stringify(_coalesce(row.get("trade_id"), row.get("id")))
        price = _stringify(_coalesce(row.get("price_dollars"), row.get("yes_price_dollars"), row.get("price")))
        count = _stringify(_coalesce(row.get("count_fp"), row.get("qty"), row.get("count")))
        normalized = {
            "trade_id": trade_id or _fallback_trade_id(row, profile_slug=profile_slug),
            "profile_slug": profile_slug,
            "social_id": social_id,
            "market_id": _stringify(row.get("market_id")),
            "ticker": _stringify(row.get("ticker")),
            "price_dollars": price,
            "count_fp": count,
            "taker_side": _stringify(row.get("taker_side")).lower(),
            "maker_action": _stringify(row.get("maker_action")).lower(),
            "taker_action": _stringify(row.get("taker_action")).lower(),
            "maker_nickname": _stringify(row.get("maker_nickname")),
            "taker_nickname": _stringify(row.get("taker_nickname")),
            "maker_social_id": _stringify(row.get("maker_social_id")),
            "taker_social_id": _stringify(row.get("taker_social_id")),
            "related_role": _related_role(row, profile_slug=profile_slug, social_id=social_id),
            "created_ts_us": _to_us(_coalesce(row.get("created_time"), row.get("create_date"), row.get("ts_us"))),
            "raw_json": json.dumps(row, default=str, sort_keys=True),
        }
        trades.append(normalized)
    return trades


def fetch_paginated_trades(
    nickname: str,
    *,
    session: requests.Session,
    trades_url_template: str,
    page_size: int = 50,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    base_url = trades_url_template.format(slug=nickname, page_size=page_size)
    cursor: str | None = None
    pages: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()

    for _ in range(max_pages):
        url = with_query_param(base_url, "cursor", cursor)
        try:
            payload = load_json_source(url, session=session)
        except RequestException as exc:
            logger.warning(
                "stopping trades pagination for %s after request failure at cursor=%s: %s",
                nickname,
                cursor or "<initial>",
                exc,
            )
            break
        if not isinstance(payload, dict):
            break
        pages.append(payload)
        next_cursor = _stringify(payload.get("cursor")).strip()
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return pages


def _open_connection(url: str) -> tuple[Any, bool]:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(path)), False
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2

        return psycopg2.connect(url), True
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


def upsert_profile(conn: Any, is_postgres: bool, profile: dict[str, Any], *, fetched_at_us: int) -> None:
    row = (
        profile["profile_slug"],
        profile["username"],
        profile["display_name"],
        profile["social_id"],
        profile["bio"],
        profile["profile_image_url"],
        profile["profile_image_path"],
        profile["pending_profile_image_path"],
        profile["total_trades"],
        profile["total_predictions"],
        profile["correct_predictions"],
        profile["win_rate"],
        profile["profit_usd"],
        profile["follower_count"],
        profile["following_count"],
        profile["posts_count"],
        profile["profile_view_count"],
        profile["joined_at"],
        profile["top_categories_json"],
        profile["blocked"],
        profile["inner_circle_enabled"],
        profile["inner_circle_viewer_status"],
        profile["metrics_volume"],
        profile["metrics_volume_fp"],
        profile["metrics_pnl"],
        profile["metrics_num_markets_traded"],
        profile["metrics_raw_json"],
        profile["raw_json"],
        fetched_at_us,
    )
    sql = (
        "INSERT OR REPLACE INTO kalshi_ideas_profiles "
        "(profile_slug, username, display_name, social_id, bio, profile_image_url, "
        " profile_image_path, pending_profile_image_path, "
        " total_trades, total_predictions, correct_predictions, win_rate, "
        " profit_usd, follower_count, following_count, posts_count, "
        " profile_view_count, joined_at, top_categories_json, blocked, "
        " inner_circle_enabled, inner_circle_viewer_status, metrics_volume, "
        " metrics_volume_fp, metrics_pnl, metrics_num_markets_traded, metrics_raw_json, "
        " raw_json, fetched_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        sql = (
            "INSERT INTO kalshi_ideas_profiles "
            "(profile_slug, username, display_name, social_id, bio, profile_image_url, "
            " profile_image_path, pending_profile_image_path, "
            " total_trades, total_predictions, correct_predictions, win_rate, "
            " profit_usd, follower_count, following_count, posts_count, "
            " profile_view_count, joined_at, top_categories_json, blocked, "
            " inner_circle_enabled, inner_circle_viewer_status, metrics_volume, "
            " metrics_volume_fp, metrics_pnl, metrics_num_markets_traded, metrics_raw_json, "
            " raw_json, fetched_at_us) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (profile_slug) DO UPDATE SET "
            "username = EXCLUDED.username, "
            "display_name = EXCLUDED.display_name, "
            "social_id = EXCLUDED.social_id, "
            "bio = EXCLUDED.bio, "
            "profile_image_url = EXCLUDED.profile_image_url, "
            "profile_image_path = EXCLUDED.profile_image_path, "
            "pending_profile_image_path = EXCLUDED.pending_profile_image_path, "
            "total_trades = EXCLUDED.total_trades, "
            "total_predictions = EXCLUDED.total_predictions, "
            "correct_predictions = EXCLUDED.correct_predictions, "
            "win_rate = EXCLUDED.win_rate, "
            "profit_usd = EXCLUDED.profit_usd, "
            "follower_count = EXCLUDED.follower_count, "
            "following_count = EXCLUDED.following_count, "
            "posts_count = EXCLUDED.posts_count, "
            "profile_view_count = EXCLUDED.profile_view_count, "
            "joined_at = EXCLUDED.joined_at, "
            "top_categories_json = EXCLUDED.top_categories_json, "
            "blocked = EXCLUDED.blocked, "
            "inner_circle_enabled = EXCLUDED.inner_circle_enabled, "
            "inner_circle_viewer_status = EXCLUDED.inner_circle_viewer_status, "
            "metrics_volume = EXCLUDED.metrics_volume, "
            "metrics_volume_fp = EXCLUDED.metrics_volume_fp, "
            "metrics_pnl = EXCLUDED.metrics_pnl, "
            "metrics_num_markets_traded = EXCLUDED.metrics_num_markets_traded, "
            "metrics_raw_json = EXCLUDED.metrics_raw_json, "
            "raw_json = EXCLUDED.raw_json, "
            "fetched_at_us = EXCLUDED.fetched_at_us"
        )
        with conn.cursor() as cur:
            cur.execute(sql, row)
        return
    conn.execute(sql, row)


def insert_leaderboard_entry(
    conn: Any, is_postgres: bool, entry: dict[str, Any], *, fetched_at_us: int,
) -> None:
    row = (
        entry["category"],
        entry["time_period"],
        entry["rank"],
        entry["profile_slug"],
        entry["username"],
        entry["display_name"],
        entry["social_id"],
        entry["profile_image_path"],
        entry["metric_value"],
        entry["profit_usd"],
        entry["winning_streak"],
        entry["total_predictions"],
        entry["correct_predictions"],
        entry["is_anonymous"],
        entry["raw_json"],
        fetched_at_us,
    )
    sql = (
        "INSERT INTO kalshi_ideas_leaderboard_entries "
        "(category, time_period, rank, profile_slug, username, display_name, social_id, "
        " profile_image_path, metric_value, profit_usd, winning_streak, "
        " total_predictions, correct_predictions, is_anonymous, raw_json, fetched_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), row)
        return
    conn.execute(sql, row)


def upsert_trade(conn: Any, is_postgres: bool, trade: dict[str, Any]) -> None:
    row = (
        trade["trade_id"],
        trade["profile_slug"],
        trade["social_id"],
        trade["market_id"],
        trade["ticker"],
        trade["price_dollars"],
        trade["count_fp"],
        trade["taker_side"],
        trade["maker_action"],
        trade["taker_action"],
        trade["maker_nickname"],
        trade["taker_nickname"],
        trade["maker_social_id"],
        trade["taker_social_id"],
        trade["related_role"],
        trade["created_ts_us"],
        trade["raw_json"],
    )
    sql = (
        "INSERT OR REPLACE INTO kalshi_ideas_trades "
        "(trade_id, profile_slug, social_id, market_id, ticker, price_dollars, "
        " count_fp, taker_side, maker_action, taker_action, maker_nickname, "
        " taker_nickname, maker_social_id, taker_social_id, related_role, "
        " created_ts_us, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        sql = (
            "INSERT INTO kalshi_ideas_trades "
            "(trade_id, profile_slug, social_id, market_id, ticker, price_dollars, "
            " count_fp, taker_side, maker_action, taker_action, maker_nickname, "
            " taker_nickname, maker_social_id, taker_social_id, related_role, "
            " created_ts_us, raw_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (trade_id) DO UPDATE SET "
            "profile_slug = EXCLUDED.profile_slug, "
            "social_id = EXCLUDED.social_id, "
            "market_id = EXCLUDED.market_id, "
            "ticker = EXCLUDED.ticker, "
            "price_dollars = EXCLUDED.price_dollars, "
            "count_fp = EXCLUDED.count_fp, "
            "taker_side = EXCLUDED.taker_side, "
            "maker_action = EXCLUDED.maker_action, "
            "taker_action = EXCLUDED.taker_action, "
            "maker_nickname = EXCLUDED.maker_nickname, "
            "taker_nickname = EXCLUDED.taker_nickname, "
            "maker_social_id = EXCLUDED.maker_social_id, "
            "taker_social_id = EXCLUDED.taker_social_id, "
            "related_role = EXCLUDED.related_role, "
            "created_ts_us = EXCLUDED.created_ts_us, "
            "raw_json = EXCLUDED.raw_json"
        )
        with conn.cursor() as cur:
            cur.execute(sql, row)
        return
    conn.execute(sql, row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull Kalshi Ideas social payloads into the DB.")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Leaderboard category; defaults to profits + winning_streak.",
    )
    parser.add_argument(
        "--user",
        action="append",
        default=[],
        help="Profile slug(s) to fetch directly; can repeat.",
    )
    parser.add_argument(
        "--leaderboard-json",
        action="append",
        default=[],
        help="CATEGORY=path_or_url for a leaderboard payload.",
    )
    parser.add_argument(
        "--leaderboard-url-template",
        default=None,
        help="URL template for leaderboard payloads. Use {category}.",
    )
    parser.add_argument(
        "--leaderboard-limit",
        type=int,
        default=DEFAULT_LEADERBOARD_LIMIT,
        help="Limit for live leaderboard API pulls.",
    )
    parser.add_argument(
        "--leaderboard-time-period",
        action="append",
        default=[],
        help=(
            "Time period for live leaderboard API pulls; can repeat to fan out "
            "across multiple windows (e.g. --leaderboard-time-period weekly "
            "--leaderboard-time-period monthly). Default: monthly. "
            f"Supported: {', '.join(SUPPORTED_LEADERBOARD_TIME_PERIODS)}."
        ),
    )
    parser.add_argument(
        "--profile-url-template",
        default=DEFAULT_PROFILE_URL_TEMPLATE,
        help="URL template for profile payloads. Use {slug}.",
    )
    parser.add_argument(
        "--trades-url-template",
        default=DEFAULT_TRADES_URL_TEMPLATE,
        help="URL template for trades payloads. Use {slug} and optionally {page_size}.",
    )
    parser.add_argument(
        "--trades-page-size",
        type=int,
        default=50,
        help="Page size for live trades pagination.",
    )
    parser.add_argument(
        "--trades-max-pages",
        type=int,
        default=100,
        help="Safety cap for live trades pagination.",
    )
    parser.add_argument(
        "--profile-metrics-url-template",
        default=DEFAULT_PROFILE_METRICS_URL_TEMPLATE,
        help="URL template for profile metrics payloads. Use {slug}.",
    )
    parser.add_argument(
        "--profile-json-dir",
        default=None,
        help="Directory of captured profile payloads named <slug>.json.",
    )
    parser.add_argument(
        "--profile-metrics-json-dir",
        default=None,
        help="Directory of captured profile metrics payloads named <slug>.json.",
    )
    parser.add_argument(
        "--trades-json-dir",
        default=None,
        help="Directory of captured trades payloads named <slug>.json.",
    )
    parser.add_argument(
        "--max-users-per-category",
        type=int,
        default=10,
        help="How many leaderboard users to fan out into profile/trade pulls.",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    categories = [normalize_category(c) for c in (args.category or list(DEFAULT_CATEGORIES))]
    time_periods = list(args.leaderboard_time_period) or [DEFAULT_LEADERBOARD_TIME_PERIOD]
    leaderboard_sources = parse_keyed_sources(args.leaderboard_json)
    session = requests.Session()
    fetched_at_us = int(time.time() * 1_000_000)

    url = args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db"
    conn, is_postgres = _open_connection(url)
    discovered_users: list[str] = []
    seen_users: set[str] = set()

    try:
        for category in categories:
            for time_period in time_periods:
                source = render_source(
                    leaderboard_sources,
                    category,
                    template=args.leaderboard_url_template,
                )
                if not source:
                    source = build_default_leaderboard_url(
                        category,
                        limit=args.leaderboard_limit,
                        time_period=time_period,
                    )
                if not source:
                    logger.info(
                        "no source for category=%s time_period=%s; skipping leaderboard fetch",
                        category, time_period,
                    )
                    continue
                payload = load_json_source(source, session=session)
                entries = normalize_leaderboard_entries(
                    payload,
                    category=category,
                    time_period=time_period,
                )
                logger.info(
                    "category=%s time_period=%s yielded %d leaderboard rows",
                    category, time_period, len(entries),
                )
                for entry in entries[: args.max_users_per_category]:
                    if entry["profile_slug"] not in seen_users:
                        seen_users.add(entry["profile_slug"])
                        discovered_users.append(entry["profile_slug"])
                    if not args.dry_run:
                        insert_leaderboard_entry(conn, is_postgres, entry, fetched_at_us=fetched_at_us)
                if not args.dry_run:
                    conn.commit()

        for user in args.user:
            slug = _normalize_slug(user)
            if slug and slug not in seen_users:
                seen_users.add(slug)
                discovered_users.append(slug)

        for slug in discovered_users:
            profile_source = render_source(
                {},
                slug,
                template=args.profile_url_template,
                dir_path=args.profile_json_dir,
            )
            if profile_source:
                profile_payload = load_json_source(profile_source, session=session)
                profile = normalize_profile(profile_payload, slug_hint=slug)
            else:
                profile = normalize_profile({"slug": slug}, slug_hint=slug)

            metrics_source = render_source(
                {},
                slug,
                template=args.profile_metrics_url_template,
                dir_path=args.profile_metrics_json_dir,
            )
            if metrics_source:
                metrics_payload = load_json_source(metrics_source, session=session)
                profile = merge_profile_metrics(profile, metrics_payload)

            if not args.dry_run:
                upsert_profile(conn, is_postgres, profile, fetched_at_us=fetched_at_us)

            trades_source = render_source(
                {},
                slug,
                template=None,
                dir_path=args.trades_json_dir,
            )
            if trades_source:
                trades_payload = load_json_source(trades_source, session=session)
            elif args.trades_url_template:
                trades_payload = fetch_paginated_trades(
                    slug,
                    session=session,
                    trades_url_template=args.trades_url_template,
                    page_size=args.trades_page_size,
                    max_pages=args.trades_max_pages,
                )
            else:
                logger.info("no trades source for %s; profile only", slug)
                continue
            trades = normalize_trades(
                trades_payload,
                profile_slug=profile["profile_slug"],
                social_id=profile["social_id"],
            )
            logger.info("%s yielded %d trades", slug, len(trades))
            for trade in trades:
                if not args.dry_run:
                    upsert_trade(conn, is_postgres, trade)
            if not args.dry_run:
                conn.commit()

        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    logger.info("done: %d users processed", len(discovered_users))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
