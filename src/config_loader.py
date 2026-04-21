"""Config-file loader for runtime knobs.

Loads `config/kalshi_fair_value_config.json` (or an override path) and
hands out typed fragments the runtime asks for. Decimal fields are
coerced at read-time so downstream code never sees strings.

Env-var expansion is supported only for `${VAR_NAME}`-format values in
string positions (database_url, secrets) — simpler than a full template
engine and avoids accidental surprises.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class LoadedConfig:
    raw: dict[str, Any]
    mode: str = "paper"
    dry_run: bool = True
    database_url: str = "sqlite:///data/kalshi.db"

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name) or {})

    def get_decimal(self, dotted_path: str, default: Decimal | None = None) -> Decimal | None:
        """Read a nested key (`strategy.pure_lag.move_threshold_bps`) as Decimal."""
        node: Any = self.raw
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        if node is None or node == "":
            return default
        return Decimal(str(node))

    @property
    def three_opt_in_config_mode_live(self) -> bool:
        """True iff the config file itself declares live mode + no dry_run."""
        return self.mode == "live" and not self.dry_run


def load_config(path: str | Path | None = None) -> LoadedConfig:
    """Load the paper-default config (or an override path)."""
    if path is None:
        path = os.environ.get(
            "KALSHI_CONFIG_PATH", "config/kalshi_fair_value_config.json"
        )
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config not found: {p}")
    raw = _expand_env(json.loads(p.read_text()))
    return LoadedConfig(
        raw=raw,
        mode=str(raw.get("mode", "paper")),
        dry_run=bool(raw.get("dry_run", True)),
        database_url=str(raw.get("database_url",
                                 "sqlite:///data/kalshi.db")),
    )


def build_risk_rules(cfg: LoadedConfig):
    """Instantiate `default_rules()` with the config's thresholds applied."""
    from risk.kalshi_rules import (
        BookDepthRule,
        CIWidthRule,
        DailyLossRule,
        MinEdgeAfterFeesRule,
        NoDataResolveNoRule,
        OpenPositionsRule,
        PositionAccountabilityRule,
        ReferenceFeedStaleRule,
        StrikeProximityRule,
        TimeWindowRule,
    )
    r = cfg.section("risk")
    time_window = r.get("time_window_s") or [5, 60]
    return [
        MinEdgeAfterFeesRule(
            min_bps=Decimal(str(r.get("min_edge_after_fees_bps", "100"))),
        ),
        TimeWindowRule(
            min_s=Decimal(str(time_window[0])),
            max_s=Decimal(str(time_window[1])),
        ),
        CIWidthRule(
            max_width=Decimal(str(r.get("max_ci_width", "0.15"))),
        ),
        OpenPositionsRule(
            max_concurrent=int(r.get("max_concurrent_positions", 3)),
        ),
        DailyLossRule(
            stop_usd=Decimal(str(r.get("daily_loss_stop_usd", "250"))),
        ),
        ReferenceFeedStaleRule(
            max_stale_s=Decimal(str(r.get("reference_feed_max_stale_s", "3"))),
        ),
        BookDepthRule(
            min_top_usd=Decimal(str(r.get("book_depth_min_usd", "200"))),
        ),
        NoDataResolveNoRule(),
        PositionAccountabilityRule(
            per_strike_cap_usd=Decimal(
                str(r.get("position_accountability_usd_per_strike", "2500"))
            ),
        ),
        StrikeProximityRule(
            min_bps=Decimal(str(r.get("strike_proximity_min_bps", "10"))),
        ),
    ]
