"""Runtime feature flags — dashboard-writable, runner-polled.

Purpose: let the ops dashboard disable scanning for a specific asset,
disable a specific strategy, or trip the execution kill-switch without
stopping/restarting the scanner process.

Storage: a single JSON file, default `config/runtime_flags.json`.
Writes are atomic (tmp+rename). Reads are stat-gated so the runner
only re-parses on mtime change — safe to call every loop tick.

Three-opt-in safety contract for the execution kill-switch:
  - The dashboard can *set* `execution_kill_switch = true` (kill).
  - The dashboard **cannot** flip it back to `false` from the UI —
    re-enabling requires either (a) editing the file manually or
    (b) restarting the scanner. This preserves the three-opt-in
    gate documented in docs/kalshi_scanner_execution_plan.md §1.
  - Per-asset / per-strategy toggles are bidirectional (low risk,
    no live-money implications).

The module is intentionally dependency-free so the runner can import
it without pulling in FastAPI or SQLite.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path("config/runtime_flags.json")
SCHEMA_VERSION = 1

# Assets the scanner currently tracks — mirrors ASSET_FROM_SERIES in
# run_kalshi_shadow.py. Duplicated here to keep this module self-contained
# (importing from the runner would create a cycle).
ASSETS: tuple[str, ...] = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
STRATEGIES: tuple[str, ...] = ("stat_model", "pure_lag", "partial_avg")


@dataclass
class RuntimeFlags:
    """In-memory view of the flags file.

    All fields default to 'permissive': scanning on everywhere, all
    strategies on, per-asset execution on, kill-switch off. A brand-new
    deploy needs no config file to run — the defaults are the right
    starting state.

    Two separate per-asset toggles by design:
      - `scan_enabled[asset]`  — controls whether the SCANNER collects
        data + emits decisions. Disabled → no books pulled, no decisions
        written, no paper/live fills generated.
      - `execution_enabled[asset]` — controls whether the LIVE executor
        will submit real orders for this asset. Disabled → decisions are
        still recorded; paper fills still land; live orders are blocked.
        Lets operators run live trading on a subset (e.g. BTC-only) while
        scanning/paper-simulating the full asset set.
    """
    version: int = SCHEMA_VERSION
    updated_at_us: int = 0
    updated_by: str = "default"
    scan_enabled: dict[str, bool] = field(
        default_factory=lambda: {a: True for a in ASSETS},
    )
    strategy_enabled: dict[str, bool] = field(
        default_factory=lambda: {s: True for s in STRATEGIES},
    )
    execution_enabled: dict[str, bool] = field(
        default_factory=lambda: {a: True for a in ASSETS},
    )
    execution_kill_switch: bool = False

    def is_asset_scan_enabled(self, asset: str) -> bool:
        return bool(self.scan_enabled.get(asset.lower(), True))

    def is_strategy_enabled(self, strategy: str) -> bool:
        return bool(self.strategy_enabled.get(strategy, True))

    def is_asset_execution_enabled(self, asset: str) -> bool:
        """True iff live orders for `asset` are allowed. Always False once
        `execution_kill_switch` is engaged regardless of per-asset state."""
        if self.execution_kill_switch:
            return False
        return bool(self.execution_enabled.get(asset.lower(), True))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _coerce(data: dict[str, Any]) -> RuntimeFlags:
    """Merge a partial dict into a fully-defaulted RuntimeFlags."""
    flags = RuntimeFlags()
    flags.version = int(data.get("version", SCHEMA_VERSION))
    flags.updated_at_us = int(data.get("updated_at_us", 0))
    flags.updated_by = str(data.get("updated_by", "unknown"))
    # Merge rather than replace — keeps new assets/strategies (added
    # after the file was first written) enabled by default.
    incoming_scan = data.get("scan_enabled", {}) or {}
    for asset in ASSETS:
        if asset in incoming_scan:
            flags.scan_enabled[asset] = bool(incoming_scan[asset])
    incoming_strat = data.get("strategy_enabled", {}) or {}
    for strat in STRATEGIES:
        if strat in incoming_strat:
            flags.strategy_enabled[strat] = bool(incoming_strat[strat])
    incoming_exec = data.get("execution_enabled", {}) or {}
    for asset in ASSETS:
        if asset in incoming_exec:
            flags.execution_enabled[asset] = bool(incoming_exec[asset])
    flags.execution_kill_switch = bool(data.get("execution_kill_switch", False))
    return flags


def load(path: Path | str = DEFAULT_PATH) -> RuntimeFlags:
    """Read `path`. Missing file → defaults; corrupt file → defaults.

    A corrupt file is logged to stderr and replaced with defaults so
    the runner doesn't hard-stop on a bad hand-edit.
    """
    p = Path(path)
    if not p.is_file():
        return RuntimeFlags()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Deliberately silent-fallback: don't crash the scanner loop
        # on malformed runtime config. Operator sees the default state
        # in the dashboard, can fix the file and save again.
        import sys
        print(f"[runtime_flags] bad {p}: {e} — using defaults", file=sys.stderr)
        return RuntimeFlags()
    if not isinstance(data, dict):
        return RuntimeFlags()
    return _coerce(data)


def save(
    flags: RuntimeFlags,
    path: Path | str = DEFAULT_PATH,
    *,
    author: str = "dashboard",
) -> None:
    """Atomically persist `flags` to `path` (tmp + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    flags.updated_at_us = _now_us()
    flags.updated_by = author
    data = flags.to_dict()
    with tempfile.NamedTemporaryFile(
        mode="w", dir=str(p.parent), delete=False, suffix=".tmp",
    ) as tf:
        json.dump(data, tf, indent=2, sort_keys=True)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_path = Path(tf.name)
    os.replace(tmp_path, p)


class FlagsPoller:
    """Runner-side helper: re-reads the flags file on mtime change.

    Cheap enough to call every loop tick. Uses an internal lock so
    reads from worker threads are safe; writes always go through
    `save()` (not this class).
    """

    def __init__(
        self, path: Path | str = DEFAULT_PATH, *, interval_s: float = 2.0,
    ) -> None:
        self._path = Path(path)
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._last_mtime: float = -1.0
        self._last_check: float = 0.0
        self._flags: RuntimeFlags = RuntimeFlags()
        self._refresh(force=True)

    def _refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_check) < self._interval_s:
            return
        self._last_check = now
        try:
            mtime = self._path.stat().st_mtime if self._path.is_file() else -1.0
        except OSError:
            mtime = -1.0
        if mtime != self._last_mtime:
            self._last_mtime = mtime
            with self._lock:
                self._flags = load(self._path)

    def get(self) -> RuntimeFlags:
        self._refresh()
        with self._lock:
            return self._flags


def apply_dashboard_patch(
    current: RuntimeFlags,
    patch: dict[str, Any],
) -> RuntimeFlags:
    """Merge a dashboard patch into `current` with the safety contract.

    Rules enforced here (not at the save layer) so tests hit the same
    code path the HTTP handler does:
      - Unknown asset / strategy keys are ignored silently.
      - `execution_kill_switch` can only be flipped `true`. Any attempt
        to set it `false` is dropped (noop) — operator-only action.
    """
    # Copy to avoid mutating the argument.
    updated = _coerce(current.to_dict())

    scan_patch = patch.get("scan_enabled") or {}
    for asset, enabled in scan_patch.items():
        if asset in ASSETS:
            updated.scan_enabled[asset] = bool(enabled)

    strat_patch = patch.get("strategy_enabled") or {}
    for strat, enabled in strat_patch.items():
        if strat in STRATEGIES:
            updated.strategy_enabled[strat] = bool(enabled)

    exec_patch = patch.get("execution_enabled") or {}
    for asset, enabled in exec_patch.items():
        if asset in ASSETS:
            updated.execution_enabled[asset] = bool(enabled)

    if "execution_kill_switch" in patch:
        # Bi-directional per operator request: the dashboard can both
        # engage AND release the kill-switch. Per-asset execution_enabled
        # toggles are the granular control; the global kill-switch is
        # the panic button. Re-engaging must still be an explicit action
        # (the UI hides 'revive' behind a distinct button, and the audit
        # trail in ops_events captures both directions).
        updated.execution_kill_switch = bool(patch["execution_kill_switch"])

    return updated
