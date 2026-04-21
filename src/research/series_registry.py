"""Series-registry heuristics for lag-opportunity discovery.

The research plan in `docs/kalshi_multi_category_lag_research.md` is much
larger than one coding pass. This module implements the first useful slice:

1. normalize Kalshi series metadata plus optional contract-terms metadata
2. infer a plausible source type / source agency / strategy hypothesis
3. assign a heuristic lag-priority score before any live measurements exist

These scores are explicitly *pre-measurement*. They are meant to rank where we
should spend R3 notebook effort first, not to masquerade as measured edge.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SOURCE_TYPE_SCORES: dict[str, int] = {
    "scheduled_release": 72,
    "continuous_index": 46,
    "daily_report": 34,
    "event_driven_scored": 22,
    "event_driven_news": 18,
    "unknown": 4,
}

CATEGORY_BONUSES: dict[str, int] = {
    "economics": 16,
    "macro": 16,
    "finance": 12,
    "rates": 12,
    "crypto": 10,
    "commodities": 9,
    "companies": 8,
    "technology": 8,
    "weather": 2,
    "climate and weather": 2,
    "sports": -8,
    "entertainment": -4,
    "politics": -2,
    "elections": -2,
}

_SCHEDULED_RELEASE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcpi\b",
        r"\binflation\b",
        r"\bnfp\b",
        r"\bnonfarm\b",
        r"\bemployment\b",
        r"\bjobless\b",
        r"\bunemployment\b",
        r"\bfomc\b",
        r"\bfed(?:eral reserve)?\b",
        r"\brate decision\b",
        r"\bpce\b",
        r"\bgdp\b",
        r"\beia\b",
        r"\busda\b",
        r"\binventory\b",
        r"\bearnings\b",
        r"\blaunch\b",
        r"\bceo change\b",
        r"\bapprove\b",
        r"\bapproval\b",
    )
)

_CONTINUOUS_INDEX_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcrypto\b",
        r"\bbitcoin\b|\bbtc\b",
        r"\beth(?:ereum)?\b",
        r"\bsol(?:ana)?\b",
        r"\bxrp\b",
        r"\bdoge\b",
        r"\bbnb\b",
        r"\bprice\b",
        r"\bindex\b",
        r"\bnasdaq\b",
        r"\bs&p\b",
        r"\bdow\b",
        r"\bgold\b",
        r"\boil\b",
        r"\byield\b",
        r"\b10y2y\b",
        r"\b10y3m\b",
        r"\b15m\b",
    )
)

_SPORTS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bnba\b",
        r"\bnfl\b",
        r"\bmlb\b",
        r"\bnhl\b",
        r"\batp\b",
        r"\bwta\b",
        r"\bsoccer\b",
        r"\bgame\b",
        r"\bmatch\b",
        r"\bspread\b",
        r"\btotal\b",
        r"\bset winner\b",
        r"\bteam total\b",
        r"\bhome run\b",
        r"\btouchdown\b",
    )
)

_WEATHER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brain\b",
        r"\bsnow\b",
        r"\btemperature\b",
        r"\bweather\b",
        r"\bclimate\b",
        r"\bmetar\b",
        r"\bhurricane\b",
        r"\bwind\b",
    )
)


@dataclass(frozen=True)
class SeriesRegistryEntry:
    series_ticker: str
    category: str
    title: str
    frequency: str
    contract_terms_url: str
    matched_contract_terms_url: str
    matched_contract_terms_path: str
    source_type: str
    source_agency: str
    source_url: str
    publish_schedule_utc: str
    ltt_to_expiry_s: int
    strategy_hypothesis: str
    lag_priority_score: int
    priority_band: str
    notes: str
    raw_series_json: dict[str, Any]

    def to_registry_value(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "title": self.title,
            "frequency": self.frequency,
            "contract_terms_url": self.contract_terms_url,
            "matched_contract_terms_url": self.matched_contract_terms_url,
            "matched_contract_terms_path": self.matched_contract_terms_path,
            "source_type": self.source_type,
            "source_agency": self.source_agency,
            "source_url": self.source_url,
            "publish_schedule_utc": self.publish_schedule_utc,
            "ltt_to_expiry_s": self.ltt_to_expiry_s,
            "strategy_hypothesis": self.strategy_hypothesis,
            "lag_priority_score": self.lag_priority_score,
            "priority_band": self.priority_band,
            "notes": self.notes,
        }

    def to_db_row(self, *, built_ts: int) -> tuple[Any, ...]:
        return (
            self.series_ticker,
            self.category,
            self.title,
            self.source_type,
            self.source_agency,
            self.source_url,
            self.publish_schedule_utc,
            self.ltt_to_expiry_s,
            self.strategy_hypothesis,
            self.lag_priority_score,
            self.priority_band,
            self.notes,
            json.dumps(self.to_registry_value(), sort_keys=True),
            built_ts,
        )


def to_registry_json(entries: Iterable[SeriesRegistryEntry]) -> dict[str, dict[str, Any]]:
    return {
        entry.series_ticker: entry.to_registry_value()
        for entry in entries
    }


def render_opportunity_markdown(
    entries: Iterable[SeriesRegistryEntry],
    *,
    research_date: str,
    limit: int = 50,
) -> str:
    rows = list(entries)[:limit]
    lines = [
        "# Kalshi Lag Opportunity Ranking",
        "",
        f"**Research date:** {research_date}",
        "",
        "> Heuristic pre-measurement ranking. This is a triage artifact for R3/R4, not measured lag.",
        "",
        "| Rank | Series | Category | Source Type | Source Agency | Score | Band | Strategy Hypothesis | Publish Schedule UTC |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for rank, entry in enumerate(rows, start=1):
        lines.append(
            f"| {rank} | {entry.series_ticker} | {entry.category or 'unknown'} | "
            f"{entry.source_type} | {entry.source_agency or 'unknown'} | "
            f"{entry.lag_priority_score} | {entry.priority_band} | "
            f"{entry.strategy_hypothesis or 'manual_review'} | "
            f"{entry.publish_schedule_utc or 'unknown'} |"
        )

    if rows:
        lines.extend(
            [
                "",
                "## Notes",
                "",
            ]
        )
        for entry in rows[:10]:
            lines.append(f"- `{entry.series_ticker}`: {entry.notes}")
    return "\n".join(lines) + "\n"


def build_registry(
    series_rows: Iterable[Mapping[str, Any]],
    contract_rows: Iterable[Mapping[str, Any]] = (),
) -> list[SeriesRegistryEntry]:
    contract_rows_list = [dict(row) for row in contract_rows]
    entries: list[SeriesRegistryEntry] = []
    for raw_row in series_rows:
        row = dict(raw_row)
        series_ticker = str(
            row.get("series_ticker") or row.get("ticker") or ""
        ).strip()
        if not series_ticker:
            continue
        category = _clean_text(row.get("category"))
        title = _clean_text(
            row.get("title") or row.get("name") or row.get("subtitle")
        )
        frequency = _clean_text(row.get("frequency") or row.get("interval"))
        contract_terms_url = _clean_text(
            row.get("contract_terms_url") or row.get("rulebook_url")
        )
        matched_contract = _match_contract_terms(
            series_ticker=series_ticker,
            title=title,
            contract_terms_url=contract_terms_url,
            contract_rows=contract_rows_list,
        )
        source_type = infer_source_type(
            series_ticker=series_ticker,
            category=category,
            title=title,
            frequency=frequency,
            contract_terms_url=contract_terms_url,
            matched_contract=matched_contract,
        )
        source_agency = infer_source_agency(
            source_type=source_type,
            category=category,
            title=title,
            series_ticker=series_ticker,
        )
        source_url = infer_source_url(
            source_agency=source_agency,
            source_type=source_type,
            category=category,
        )
        publish_schedule = infer_publish_schedule_utc(
            source_type=source_type,
            category=category,
            title=title,
            frequency=frequency,
        )
        ltt_to_expiry_s = infer_ltt_to_expiry_s(
            source_type=source_type,
            title=title,
            frequency=frequency,
            series_ticker=series_ticker,
        )
        strategy_hypothesis = infer_strategy_hypothesis(
            source_type=source_type,
            category=category,
            title=title,
        )
        score, notes = score_lag_candidate(
            source_type=source_type,
            category=category,
            title=title,
            frequency=frequency,
            ltt_to_expiry_s=ltt_to_expiry_s,
            matched_contract=matched_contract,
        )
        entries.append(
            SeriesRegistryEntry(
                series_ticker=series_ticker,
                category=category,
                title=title,
                frequency=frequency,
                contract_terms_url=contract_terms_url,
                matched_contract_terms_url=_clean_text(
                    matched_contract.get("pdf_url")
                ),
                matched_contract_terms_path=_clean_text(
                    matched_contract.get("local_path")
                ),
                source_type=source_type,
                source_agency=source_agency,
                source_url=source_url,
                publish_schedule_utc=publish_schedule,
                ltt_to_expiry_s=ltt_to_expiry_s,
                strategy_hypothesis=strategy_hypothesis,
                lag_priority_score=score,
                priority_band=priority_band_for_score(score),
                notes=notes,
                raw_series_json=_normalize_raw_series_json(row),
            )
        )
    return sorted(
        entries,
        key=lambda entry: (-entry.lag_priority_score, entry.series_ticker),
    )


def infer_source_type(
    *,
    series_ticker: str,
    category: str,
    title: str,
    frequency: str,
    contract_terms_url: str,
    matched_contract: Mapping[str, Any] | None = None,
) -> str:
    haystack = _series_text(
        series_ticker,
        category,
        title,
        frequency,
        contract_terms_url,
        _clean_text((matched_contract or {}).get("pdf_url")),
    )
    category_lc = category.lower()
    if any(p.search(haystack) for p in _SCHEDULED_RELEASE_PATTERNS):
        return "scheduled_release"
    if "sports" in category_lc or any(p.search(haystack) for p in _SPORTS_PATTERNS):
        return "event_driven_scored"
    if "weather" in category_lc or any(p.search(haystack) for p in _WEATHER_PATTERNS):
        return "daily_report"
    if "crypto" in category_lc or any(p.search(haystack) for p in _CONTINUOUS_INDEX_PATTERNS):
        return "continuous_index"
    if category_lc in {"politics", "elections", "social", "mentions"}:
        return "event_driven_news"
    if category_lc in {"companies", "technology", "business"}:
        return "event_driven_news"
    return "unknown"


def infer_source_agency(
    *,
    source_type: str,
    category: str,
    title: str,
    series_ticker: str,
) -> str:
    haystack = _series_text(category, title, series_ticker)
    if source_type == "scheduled_release":
        if re.search(r"\bcpi\b|\binflation\b|\bnfp\b|\bemployment\b|\bjobs?\b", haystack):
            return "BLS"
        if re.search(r"\bfomc\b|\bfed\b|\brate decision\b", haystack):
            return "Federal Reserve"
        if re.search(r"\beia\b|\binventory\b|\boil\b", haystack):
            return "EIA"
        if re.search(r"\busda\b", haystack):
            return "USDA"
        if re.search(r"\bearnings\b|\bceo\b|\blaunch\b", haystack):
            return "Issuer IR / SEC"
        return "Scheduled public release"
    if source_type == "continuous_index":
        if re.search(r"\bcrypto\b|\bbtc\b|\bbitcoin\b|\beth\b|\bsol\b|\bxrp\b|\bdoge\b|\bbnb\b", haystack):
            return "CF Benchmarks / market data"
        return "Continuous market data"
    if source_type == "daily_report":
        return "NOAA / NWS"
    if source_type == "event_driven_scored":
        return "STATSCORE / official scoring"
    if source_type == "event_driven_news":
        return "News / official source"
    return ""


def infer_source_url(
    *,
    source_agency: str,
    source_type: str,
    category: str,
) -> str:
    agency_lc = source_agency.lower()
    category_lc = category.lower()
    if agency_lc == "bls":
        return "https://www.bls.gov"
    if agency_lc == "federal reserve":
        return "https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm"
    if agency_lc == "eia":
        return "https://www.eia.gov"
    if agency_lc == "usda":
        return "https://www.usda.gov"
    if "cf benchmarks" in agency_lc:
        return "https://www.cfbenchmarks.com"
    if agency_lc == "noaa / nws":
        return "https://www.weather.gov"
    if source_type == "event_driven_scored":
        return "https://www.statscore.com"
    if category_lc in {"politics", "elections"}:
        return "https://www.fec.gov"
    return ""


def infer_publish_schedule_utc(
    *,
    source_type: str,
    category: str,
    title: str,
    frequency: str,
) -> str:
    haystack = _series_text(category, title, frequency)
    if source_type == "scheduled_release":
        if re.search(r"\bcpi\b|\binflation\b", haystack):
            return "13:30 UTC monthly BLS release"
        if re.search(r"\bnfp\b|\bnonfarm\b|\bemployment\b|\bjobs?\b", haystack):
            return "13:30 UTC first-Friday BLS release"
        if re.search(r"\bfomc\b|\bfed\b|\brate decision\b", haystack):
            return "18:00 UTC on FOMC statement days"
        if re.search(r"\bearnings\b", haystack):
            return "Company-scheduled release time (varies)"
        return "Scheduled public release (exact UTC varies)"
    if source_type == "continuous_index":
        return "Continuous"
    if source_type == "daily_report":
        return "Daily / station-dependent"
    if source_type in {"event_driven_scored", "event_driven_news"}:
        return "Event-driven / non-fixed"
    return ""


def infer_ltt_to_expiry_s(
    *,
    source_type: str,
    title: str,
    frequency: str,
    series_ticker: str,
) -> int:
    haystack = _series_text(title, frequency, series_ticker)
    if re.search(r"\b15m\b|\b15 min\b|\b15 minute\b", haystack):
        return 900
    if re.search(r"\bhourly\b|\b1h\b|\b60 min\b", haystack):
        return 3600
    if re.search(r"\bdaily\b|\bday\b", haystack):
        return 86400
    if re.search(r"\bweekly\b|\bweek\b", haystack):
        return 604800
    if re.search(r"\bmonthly\b|\bmonth\b", haystack):
        return 2592000
    if source_type == "scheduled_release":
        return 300
    return 0


def infer_strategy_hypothesis(
    *,
    source_type: str,
    category: str,
    title: str,
) -> str:
    category_lc = category.lower()
    title_lc = title.lower()
    if source_type == "scheduled_release":
        return "scheduled_release_lag"
    if source_type == "continuous_index":
        if "crypto" in category_lc or any(k in title_lc for k in ("bitcoin", "ethereum", "solana", "15m")):
            return "continuous_index_reprice"
        return "index_move_reprice"
    if source_type == "daily_report":
        return "daily_report_publication_lag"
    if source_type == "event_driven_scored":
        return "score_update_lag"
    if source_type == "event_driven_news":
        return "headline_reprice_watch"
    return "manual_review"


def score_lag_candidate(
    *,
    source_type: str,
    category: str,
    title: str,
    frequency: str,
    ltt_to_expiry_s: int,
    matched_contract: Mapping[str, Any] | None = None,
) -> tuple[int, str]:
    score = SOURCE_TYPE_SCORES.get(source_type, SOURCE_TYPE_SCORES["unknown"])
    reasons = [f"base={source_type}"]

    category_bonus = CATEGORY_BONUSES.get(category.lower(), 0)
    if category_bonus:
        score += category_bonus
        reasons.append(f"category={category.lower()} ({category_bonus:+d})")

    if source_type in {"scheduled_release", "continuous_index", "daily_report"}:
        score += 10
        reasons.append("public-ish source access (+10)")
    if source_type == "event_driven_scored":
        score -= 8
        reasons.append("scoring-feed / event-driven penalty (-8)")

    freq_lc = frequency.lower()
    if freq_lc in {"hourly", "daily", "weekly"} or "15m" in freq_lc:
        score += 4
        reasons.append("repeatable cadence (+4)")

    if 0 < ltt_to_expiry_s <= 300:
        score += 12
        reasons.append("tight release-to-expiry window (+12)")
    elif 300 < ltt_to_expiry_s <= 3600:
        score += 6
        reasons.append("sub-hour reaction window (+6)")
    elif ltt_to_expiry_s >= 86400:
        score -= 4
        reasons.append("long window penalty (-4)")

    if matched_contract and matched_contract.get("pdf_url"):
        score += 3
        reasons.append("contract terms matched (+3)")

    if not title.strip():
        score -= 6
        reasons.append("missing title metadata (-6)")

    # Clamp into a human-scale 0..100 band so reports are easier to scan.
    score = max(0, min(100, score))
    return score, "; ".join(reasons)


def priority_band_for_score(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _match_contract_terms(
    *,
    series_ticker: str,
    title: str,
    contract_terms_url: str,
    contract_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    contract_terms_url = contract_terms_url.strip()
    if contract_terms_url:
        for row in contract_rows:
            if _clean_text(row.get("pdf_url")) == contract_terms_url:
                return dict(row)

    series_tokens = {
        _stem_token(series_ticker),
        _stem_token(title),
    }
    for row in contract_rows:
        row_url = _clean_text(row.get("pdf_url"))
        row_guess = _stem_token(row.get("series_ticker_guess"))
        row_stem = _stem_token(Path(row_url).stem)
        if row_guess in series_tokens or row_stem in series_tokens:
            return dict(row)
    return {}


def _stem_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "", str(value or "").upper())
    return text


def _series_text(*parts: Any) -> str:
    return " ".join(_clean_text(part) for part in parts if _clean_text(part)).lower()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_raw_series_json(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    raw_json = normalized.get("raw_json")
    if isinstance(raw_json, str):
        try:
            normalized["raw_json"] = json.loads(raw_json)
        except json.JSONDecodeError:
            pass
    return normalized


def as_public_dict(entry: SeriesRegistryEntry) -> dict[str, Any]:
    out = asdict(entry)
    out["raw_series_json"] = entry.raw_series_json
    return out
