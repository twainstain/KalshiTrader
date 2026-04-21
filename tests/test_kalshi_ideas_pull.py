"""Coverage for `scripts/kalshi_ideas_pull.py`."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest
from requests import RequestException


@pytest.fixture(scope="module")
def ip():
    return importlib.import_module("kalshi_ideas_pull")


@pytest.fixture
def seeded_db(tmp_path):
    import migrate_db as m

    url = f"sqlite:///{tmp_path}/ideas.db"
    m.migrate(url)
    return url, sqlite3.connect(url.removeprefix("sqlite:///"))


def test_normalize_category_aliases(ip):
    assert ip.normalize_category("profits") == "profits"
    assert ip.normalize_category("projected_pnl") == "projected_pnl"
    assert ip.normalize_category("num_markets_traded") == "num_markets_traded"
    assert ip.normalize_category("winning strike") == "winning_streak"
    assert ip.normalize_category("streak") == "winning_streak"


def test_normalize_leaderboard_entries_handles_nested_user_payload(ip):
    payload = {
        "data": {
            "rows": [
                {
                    "rank": 1,
                    "profit": "1200.55",
                    "winning_streak": 8,
                    "predictions": 91,
                    "wins": 54,
                    "user": {
                        "slug": "best.gun2",
                        "username": "best.gun2",
                        "display_name": "Best Gun",
                        "social_id": "social-1",
                    },
                },
            ],
        },
    }
    rows = ip.normalize_leaderboard_entries(payload, category="profits")
    assert rows == [{
        "category": "profits",
        "time_period": "monthly",
        "rank": 1,
        "profile_slug": "best.gun2",
        "username": "best.gun2",
        "display_name": "Best Gun",
        "social_id": "social-1",
        "profile_image_path": "",
        "metric_value": "",
        "profit_usd": "1200.55",
        "winning_streak": "8",
        "total_predictions": "91",
        "correct_predictions": "54",
        "is_anonymous": 0,
        "raw_json": json.dumps(payload["data"]["rows"][0], default=str, sort_keys=True),
    }]


def test_normalize_leaderboard_entries_handles_live_rank_list(ip):
    payload = {
        "rank_list": [
            {
                "nickname": "best.gun2",
                "social_id": "",
                "profile_image_path": "crypto",
                "value": 692879.94,
                "rank": 30,
                "is_anonymous": False,
            },
        ],
    }
    rows = ip.normalize_leaderboard_entries(
        payload,
        category="projected_pnl",
        time_period="monthly",
    )
    assert rows[0]["profile_slug"] == "best.gun2"
    assert rows[0]["profile_image_path"] == "crypto"
    assert rows[0]["metric_value"] == "692879.94"
    assert rows[0]["profit_usd"] == "692879.94"
    assert rows[0]["time_period"] == "monthly"
    assert rows[0]["is_anonymous"] == 0


def test_build_default_leaderboard_url(ip):
    assert ip.build_default_leaderboard_url("profits", limit=20, time_period="monthly") == (
        "https://api.elections.kalshi.com/v1/social/leaderboard?metric_name=projected_pnl&limit=20&time_period=monthly"
    )
    assert ip.build_default_leaderboard_url("num_markets_traded", limit=20, time_period="monthly") == (
        "https://api.elections.kalshi.com/v1/social/leaderboard?metric_name=num_markets_traded&limit=20&time_period=monthly"
    )
    assert ip.build_default_leaderboard_url("profits", limit=20, time_period="weekly") == (
        "https://api.elections.kalshi.com/v1/social/leaderboard?metric_name=projected_pnl&limit=20&time_period=weekly"
    )


def test_main_fans_out_across_time_periods(ip, tmp_path, monkeypatch):
    import migrate_db as m

    db_url = f"sqlite:///{tmp_path}/ideas.db"
    m.migrate(db_url)

    weekly_payload = {
        "rank_list": [
            {"nickname": "vica", "rank": 8, "value": 47906.02,
             "profile_image_path": "elements", "is_anonymous": False},
        ],
    }
    monthly_payload = {
        "rank_list": [
            {"nickname": "REAKT", "rank": 24, "value": 856951.56,
             "profile_image_path": "REAKT-2026-02-15", "is_anonymous": False},
        ],
    }
    captured_urls: list[str] = []

    def fake_load(source, *, session=None):
        captured_urls.append(source)
        if "time_period=weekly" in source:
            return weekly_payload
        if "time_period=monthly" in source:
            return monthly_payload
        # profile / metrics / trades calls — return shape the script tolerates
        return {"social_profile": {"nickname": source.split("nickname=")[-1]}}

    monkeypatch.setattr(ip, "load_json_source", fake_load)

    rc = ip.main([
        "--category", "profits",
        "--leaderboard-time-period", "weekly",
        "--leaderboard-time-period", "monthly",
        "--max-users-per-category", "1",
        "--database-url", db_url,
    ])
    assert rc == 0

    weekly_lb_urls = [u for u in captured_urls if "/social/leaderboard" in u and "weekly" in u]
    monthly_lb_urls = [u for u in captured_urls if "/social/leaderboard" in u and "monthly" in u]
    assert weekly_lb_urls and monthly_lb_urls

    conn = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    try:
        rows = conn.execute(
            "SELECT time_period, profile_slug, metric_value FROM kalshi_ideas_leaderboard_entries "
            "ORDER BY time_period, rank"
        ).fetchall()
    finally:
        conn.close()
    periods = {r[0] for r in rows}
    assert periods == {"weekly", "monthly"}
    by_period = {r[0]: (r[1], r[2]) for r in rows}
    assert by_period["weekly"] == ("vica", "47906.02")
    assert by_period["monthly"] == ("REAKT", "856951.56")


def test_normalize_profile_prefers_profile_fields(ip):
    payload = {
        "profile": {
            "slug": "moonmoon",
            "username": "moonmoon",
            "display_name": "Moon Moon",
            "bio": "Macro trader",
            "social_id": "social-2",
            "profile_image_url": "https://img/moonmoon.png",
            "profit_usd": "99.25",
            "stats": {
                "total_predictions": 42,
                "correct_predictions": 31,
                "win_rate": "73.8%",
            },
        },
    }
    profile = ip.normalize_profile(payload, slug_hint="fallback")
    assert profile["profile_slug"] == "moonmoon"
    assert profile["username"] == "moonmoon"
    assert profile["display_name"] == "Moon Moon"
    assert profile["bio"] == "Macro trader"
    assert profile["social_id"] == "social-2"
    assert profile["profile_image_url"] == "https://img/moonmoon.png"
    assert profile["profit_usd"] == "99.25"
    assert profile["total_predictions"] == "42"
    assert profile["correct_predictions"] == "31"
    assert profile["win_rate"] == "73.8%"


def test_normalize_profile_handles_live_social_profile_shape(ip):
    payload = {
        "social_profile": {
            "social_id": "7ae31497-99f9-4960-9992-6dac97036bf2",
            "nickname": "boundary",
            "follower_count": 0,
            "following_count": 0,
            "posts_count": 0,
            "profile_image_path": "boundary-2026-03-28 00:48:46",
            "description": "",
            "pending_profile_image_path": "",
            "blocked": False,
            "joined_at": "2026-03-25",
            "profile_view_count": 20521,
            "top_categories": ["Sports", "Climate And Weather"],
        },
        "inner_circle": {
            "enabled": False,
            "viewer_status": "none",
        },
    }
    profile = ip.normalize_profile(payload, slug_hint="fallback")
    assert profile["profile_slug"] == "boundary"
    assert profile["username"] == "boundary"
    assert profile["display_name"] == "boundary"
    assert profile["social_id"] == "7ae31497-99f9-4960-9992-6dac97036bf2"
    assert profile["profile_image_path"] == "boundary-2026-03-28 00:48:46"
    assert profile["pending_profile_image_path"] == ""
    assert profile["follower_count"] == 0
    assert profile["following_count"] == 0
    assert profile["posts_count"] == 0
    assert profile["profile_view_count"] == 20521
    assert profile["joined_at"] == "2026-03-25"
    assert json.loads(profile["top_categories_json"]) == ["Sports", "Climate And Weather"]
    assert profile["blocked"] == 0
    assert profile["inner_circle_enabled"] == 0
    assert profile["inner_circle_viewer_status"] == "none"


def test_merge_profile_metrics_uses_live_metrics_shape(ip):
    profile = ip.normalize_profile({"social_profile": {"nickname": "boundary"}}, slug_hint="boundary")
    merged = ip.merge_profile_metrics(profile, {
        "metrics": {
            "volume": 840110,
            "volume_fp": "840110.00",
            "pnl": 59422371,
            "num_markets_traded": 11086,
        },
        "social_id": "7ae31497-99f9-4960-9992-6dac97036bf2",
    })
    assert merged["metrics_volume"] == 840110
    assert merged["metrics_volume_fp"] == "840110.00"
    assert merged["metrics_pnl"] == 59422371
    assert merged["metrics_num_markets_traded"] == 11086
    assert merged["total_trades"] == "11086"


def test_fetch_paginated_trades_follows_cursor(ip):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self):
            self.urls = []

        def get(self, url, timeout=30.0):
            self.urls.append(url)
            if "cursor=cursor-1" in url:
                return FakeResponse({"trades": [{"trade_id": "b"}], "cursor": ""})
            return FakeResponse({"trades": [{"trade_id": "a"}], "cursor": "cursor-1"})

    session = FakeSession()
    pages = ip.fetch_paginated_trades(
        "best.gun2",
        session=session,
        trades_url_template="https://api.elections.kalshi.com/v1/social/trades?nickname={slug}&page_size={page_size}",
        page_size=50,
        max_pages=5,
    )
    assert len(pages) == 2
    assert pages[0]["trades"][0]["trade_id"] == "a"
    assert pages[1]["trades"][0]["trade_id"] == "b"
    assert "page_size=50" in session.urls[0]
    assert "cursor=cursor-1" in session.urls[1]


def test_fetch_paginated_trades_stops_on_request_error(ip):
    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, timeout=30.0):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse({"trades": [{"trade_id": "a"}], "cursor": "cursor-1"})
            raise RequestException("boom")

    pages = ip.fetch_paginated_trades(
        "best.gun2",
        session=FakeSession(),
        trades_url_template="https://api.elections.kalshi.com/v1/social/trades?nickname={slug}&page_size={page_size}",
        page_size=50,
        max_pages=5,
    )
    assert len(pages) == 1
    assert pages[0]["trades"][0]["trade_id"] == "a"


def test_normalize_trades_uses_sample_repo_fixture(ip):
    payload = ip.load_json_source("trades.best.json")
    trades = ip.normalize_trades(
        payload,
        profile_slug="best.gun2",
        social_id="40e588ef-ce61-454b-8c1c-27770fc4dc92",
    )
    assert trades
    first = trades[0]
    assert first["profile_slug"] == "best.gun2"
    assert first["social_id"] == "40e588ef-ce61-454b-8c1c-27770fc4dc92"
    assert first["trade_id"] == "e463730c-ffd5-43b0-246a-90a04b2a2ab3"
    assert first["ticker"] == "KXNBAGAME-26APR07CHABOS-CHA"
    assert first["price_dollars"] == "0.3600"
    assert first["count_fp"] == "13744.00"
    assert first["related_role"] == "taker"
    assert first["created_ts_us"] > 0


def test_load_json_source_parses_next_rsc_stream(ip, tmp_path):
    rsc_path = tmp_path / "profits.rsc"
    rsc_path.write_text(
        '\n'.join([
            '1:"$Sreact.fragment"',
            '0:{"leaderboard":[{"rank":1,"profit":"50.25","user":{"slug":"best.gun2","username":"best.gun2","social_id":"social-1"}}]}',
        ]),
    )
    payload = ip.load_json_source(str(rsc_path))
    assert payload["__format__"] == "next_rsc"
    rows = ip.normalize_leaderboard_entries(payload, category="profits")
    assert len(rows) == 1
    assert rows[0]["profile_slug"] == "best.gun2"
    assert rows[0]["profit_usd"] == "50.25"


def test_main_local_payloads_persist_profiles_leaderboard_and_trades(ip, tmp_path):
    leaderboard_dir = tmp_path / "leaderboards"
    profile_dir = tmp_path / "profiles"
    metrics_dir = tmp_path / "profile_metrics"
    trades_dir = tmp_path / "trades"
    leaderboard_dir.mkdir()
    profile_dir.mkdir()
    metrics_dir.mkdir()
    trades_dir.mkdir()

    profits_payload = {
        "leaderboard": [
            {
                "rank": 1,
                "profit": "125.50",
                "winning_streak": 3,
                "predictions": 11,
                "wins": 8,
                "user": {
                    "slug": "best.gun2",
                    "username": "best.gun2",
                    "display_name": "Best Gun",
                    "social_id": "social-1",
                },
            },
        ],
    }
    (leaderboard_dir / "profits.json").write_text(json.dumps(profits_payload))
    (leaderboard_dir / "winning_streak.json").write_text(json.dumps({
        "entries": [
            {
                "position": 1,
                "streak": 9,
                "user": {
                    "slug": "moonmoon",
                    "username": "moonmoon",
                    "display_name": "Moon Moon",
                    "social_id": "social-2",
                },
            },
        ],
    }))
    (profile_dir / "best.gun2.json").write_text(json.dumps({
        "social_profile": {
            "social_id": "social-1",
            "nickname": "best.gun2",
            "description": "Sports trader",
            "follower_count": 1,
            "following_count": 2,
            "posts_count": 3,
            "profile_view_count": 4,
            "joined_at": "2026-04-01",
            "top_categories": ["Sports"],
        },
    }))
    (profile_dir / "moonmoon.json").write_text(json.dumps({
        "social_profile": {
            "social_id": "social-2",
            "nickname": "moonmoon",
            "description": "Macro trader",
            "follower_count": 4,
            "following_count": 5,
            "posts_count": 6,
            "profile_view_count": 7,
            "joined_at": "2026-04-02",
            "top_categories": ["Politics"],
        },
    }))
    (metrics_dir / "best.gun2.json").write_text(json.dumps({
        "metrics": {
            "volume": 100,
            "volume_fp": "100.00",
            "pnl": 50,
            "num_markets_traded": 5,
        },
        "social_id": "social-1",
    }))
    (metrics_dir / "moonmoon.json").write_text(json.dumps({
        "metrics": {
            "volume": 200,
            "volume_fp": "200.00",
            "pnl": 25,
            "num_markets_traded": 9,
        },
        "social_id": "social-2",
    }))
    (trades_dir / "best.gun2.json").write_text(Path("trades.best.json").read_text())
    (trades_dir / "moonmoon.json").write_text(json.dumps({"trades": []}))

    db_url = f"sqlite:///{tmp_path}/ideas.db"
    import migrate_db as m

    m.migrate(db_url)
    rc = ip.main([
        "--category", "profits",
        "--category", "winning_streak",
        "--leaderboard-json", f"profits={leaderboard_dir / 'profits.json'}",
        "--leaderboard-json", f"winning_streak={leaderboard_dir / 'winning_streak.json'}",
        "--profile-json-dir", str(profile_dir),
        "--profile-metrics-json-dir", str(metrics_dir),
        "--trades-json-dir", str(trades_dir),
        "--database-url", db_url,
    ])
    assert rc == 0

    conn = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM kalshi_ideas_profiles"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT follower_count FROM kalshi_ideas_profiles WHERE profile_slug = ?",
            ("best.gun2",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT metrics_num_markets_traded FROM kalshi_ideas_profiles WHERE profile_slug = ?",
            ("best.gun2",),
        ).fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM kalshi_ideas_leaderboard_entries"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT time_period FROM kalshi_ideas_leaderboard_entries WHERE profile_slug = ?",
            ("best.gun2",),
        ).fetchone()[0] == "monthly"
        assert conn.execute(
            "SELECT COUNT(*) FROM kalshi_ideas_trades"
        ).fetchone()[0] > 0
    finally:
        conn.close()
