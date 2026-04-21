"""Coverage for `src/research/series_registry.py`."""

from __future__ import annotations

from research.series_registry import (
    build_registry,
    render_opportunity_markdown,
    to_registry_json,
)


def test_build_registry_prioritizes_scheduled_release_over_sports():
    series_rows = [
        {
            "series_ticker": "CPIYOY",
            "category": "Economics",
            "title": "Will CPI inflation come in above 3.0%?",
            "frequency": "monthly",
            "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CPI.pdf",
        },
        {
            "series_ticker": "NBATOTAL",
            "category": "Sports",
            "title": "NBA total points for tonight's game",
            "frequency": "event",
            "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NBATOTAL.pdf",
        },
    ]
    contract_rows = [
        {
            "pdf_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CPI.pdf",
            "series_ticker_guess": "CPI",
            "local_path": "data/contract_terms/CPI.pdf",
        },
        {
            "pdf_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NBATOTAL.pdf",
            "series_ticker_guess": "NBATOTAL",
            "local_path": "data/contract_terms/NBATOTAL.pdf",
        },
    ]

    entries = build_registry(series_rows, contract_rows)
    assert [entry.series_ticker for entry in entries] == ["CPIYOY", "NBATOTAL"]
    assert entries[0].source_type == "scheduled_release"
    assert entries[0].source_agency == "BLS"
    assert entries[0].publish_schedule_utc == "13:30 UTC monthly BLS release"
    assert entries[0].strategy_hypothesis == "scheduled_release_lag"
    assert entries[0].lag_priority_score > entries[1].lag_priority_score
    assert entries[1].source_type == "event_driven_scored"
    assert entries[1].source_agency == "STATSCORE / official scoring"


def test_build_registry_identifies_crypto_continuous_index():
    entries = build_registry(
        [
            {
                "series_ticker": "KXBTC15M",
                "category": "Crypto",
                "title": "Bitcoin 15m average price",
                "frequency": "15m",
                "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf",
            },
        ],
        [
            {
                "pdf_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf",
                "series_ticker_guess": "CRYPTO15M",
                "local_path": "data/contract_terms/CRYPTO15M.pdf",
            },
        ],
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source_type == "continuous_index"
    assert entry.source_agency == "CF Benchmarks / market data"
    assert entry.ltt_to_expiry_s == 900
    assert entry.strategy_hypothesis == "continuous_index_reprice"
    assert entry.priority_band in {"medium", "high"}


def test_registry_json_and_markdown_render():
    entries = build_registry(
        [
            {
                "series_ticker": "FEDDECISION",
                "category": "Economics",
                "title": "Fed decision in May",
                "frequency": "scheduled",
                "contract_terms_url": "",
            },
        ]
    )
    payload = to_registry_json(entries)
    assert payload["FEDDECISION"]["source_type"] == "scheduled_release"
    md = render_opportunity_markdown(entries, research_date="2026-04-21")
    assert "Kalshi Lag Opportunity Ranking" in md
    assert "FEDDECISION" in md
