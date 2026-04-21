"""Coverage for `runtime_flags` — load/save, patch, three-opt-in gate."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import runtime_flags


# ---------------------------------------------------------------------------
# Defaults + coercion
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_missing_file_returns_defaults(self, tmp_path: Path):
        flags = runtime_flags.load(tmp_path / "nope.json")
        assert flags.execution_kill_switch is False
        # All assets enabled.
        assert all(flags.scan_enabled[a] for a in runtime_flags.ASSETS)
        assert all(flags.strategy_enabled[s] for s in runtime_flags.STRATEGIES)

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not valid { json")
        flags = runtime_flags.load(p)
        # Corrupt file MUST NOT crash — defaults instead.
        assert flags.execution_kill_switch is False
        assert flags.scan_enabled["btc"] is True

    def test_non_dict_root_falls_back(self, tmp_path: Path):
        p = tmp_path / "arr.json"
        p.write_text("[]")
        flags = runtime_flags.load(p)
        assert flags.execution_kill_switch is False

    def test_partial_file_merges_with_defaults(self, tmp_path: Path):
        p = tmp_path / "partial.json"
        p.write_text(json.dumps({
            "scan_enabled": {"btc": False},
        }))
        flags = runtime_flags.load(p)
        assert flags.scan_enabled["btc"] is False
        # Unspecified assets stay enabled.
        assert flags.scan_enabled["eth"] is True
        assert flags.strategy_enabled["pure_lag"] is True


# ---------------------------------------------------------------------------
# Save — atomic write + round-trip
# ---------------------------------------------------------------------------


class TestSave:
    def test_save_roundtrip(self, tmp_path: Path):
        p = tmp_path / "flags.json"
        flags = runtime_flags.RuntimeFlags()
        flags.scan_enabled["btc"] = False
        runtime_flags.save(flags, p, author="test")
        loaded = runtime_flags.load(p)
        assert loaded.scan_enabled["btc"] is False
        assert loaded.updated_by == "test"
        assert loaded.updated_at_us > 0

    def test_save_creates_parent_dir(self, tmp_path: Path):
        p = tmp_path / "nested/deep/flags.json"
        runtime_flags.save(runtime_flags.RuntimeFlags(), p)
        assert p.is_file()

    def test_save_uses_atomic_replace(self, tmp_path: Path):
        """No .tmp files left behind after a successful save."""
        p = tmp_path / "flags.json"
        runtime_flags.save(runtime_flags.RuntimeFlags(), p)
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_save_timestamp_updates(self, tmp_path: Path):
        p = tmp_path / "flags.json"
        runtime_flags.save(runtime_flags.RuntimeFlags(), p)
        first = runtime_flags.load(p).updated_at_us
        time.sleep(0.001)  # enough to tick microseconds
        runtime_flags.save(runtime_flags.RuntimeFlags(), p)
        second = runtime_flags.load(p).updated_at_us
        assert second >= first


# ---------------------------------------------------------------------------
# apply_dashboard_patch — the safety contract
# ---------------------------------------------------------------------------


class TestDashboardPatch:
    def test_scan_toggle_both_directions(self):
        base = runtime_flags.RuntimeFlags()
        off = runtime_flags.apply_dashboard_patch(
            base, {"scan_enabled": {"btc": False}},
        )
        assert off.scan_enabled["btc"] is False
        on = runtime_flags.apply_dashboard_patch(
            off, {"scan_enabled": {"btc": True}},
        )
        assert on.scan_enabled["btc"] is True

    def test_strategy_toggle_both_directions(self):
        base = runtime_flags.RuntimeFlags()
        off = runtime_flags.apply_dashboard_patch(
            base, {"strategy_enabled": {"pure_lag": False}},
        )
        assert off.strategy_enabled["pure_lag"] is False
        on = runtime_flags.apply_dashboard_patch(
            off, {"strategy_enabled": {"pure_lag": True}},
        )
        assert on.strategy_enabled["pure_lag"] is True

    def test_unknown_asset_ignored(self):
        base = runtime_flags.RuntimeFlags()
        out = runtime_flags.apply_dashboard_patch(
            base, {"scan_enabled": {"dogecoin_2": False}},
        )
        # No new keys added; existing ones unchanged.
        assert "dogecoin_2" not in out.scan_enabled
        assert out.scan_enabled["btc"] is True

    def test_unknown_strategy_ignored(self):
        base = runtime_flags.RuntimeFlags()
        out = runtime_flags.apply_dashboard_patch(
            base, {"strategy_enabled": {"quantum_ai": False}},
        )
        assert "quantum_ai" not in out.strategy_enabled

    def test_kill_switch_can_engage(self):
        base = runtime_flags.RuntimeFlags()
        killed = runtime_flags.apply_dashboard_patch(
            base, {"execution_kill_switch": True},
        )
        assert killed.execution_kill_switch is True

    def test_kill_switch_revive_via_dashboard(self):
        """Bi-directional (per 2026-04-20 spec change): dashboard can
        both ENGAGE and RELEASE the kill-switch. Re-enabling requires an
        explicit action (separate /unkill POST), and every transition
        lands in ops_events for audit."""
        killed = runtime_flags.RuntimeFlags()
        killed.execution_kill_switch = True
        revived = runtime_flags.apply_dashboard_patch(
            killed, {"execution_kill_switch": False},
        )
        assert revived.execution_kill_switch is False

    def test_execution_enabled_toggle_both_directions(self):
        base = runtime_flags.RuntimeFlags()
        off = runtime_flags.apply_dashboard_patch(
            base, {"execution_enabled": {"btc": False}},
        )
        assert off.execution_enabled["btc"] is False
        on = runtime_flags.apply_dashboard_patch(
            off, {"execution_enabled": {"btc": True}},
        )
        assert on.execution_enabled["btc"] is True

    def test_execution_enabled_unknown_asset_ignored(self):
        base = runtime_flags.RuntimeFlags()
        out = runtime_flags.apply_dashboard_patch(
            base, {"execution_enabled": {"notanasset": False}},
        )
        assert "notanasset" not in out.execution_enabled

    def test_patch_does_not_mutate_input(self):
        base = runtime_flags.RuntimeFlags()
        runtime_flags.apply_dashboard_patch(
            base, {"scan_enabled": {"btc": False}},
        )
        # Original object unchanged.
        assert base.scan_enabled["btc"] is True


# ---------------------------------------------------------------------------
# FlagsPoller — mtime-gated refresh
# ---------------------------------------------------------------------------


class TestFlagsPoller:
    def test_initial_load_uses_defaults_when_missing(self, tmp_path: Path):
        poller = runtime_flags.FlagsPoller(tmp_path / "nope.json")
        f = poller.get()
        assert f.execution_kill_switch is False

    def test_picks_up_writes(self, tmp_path: Path):
        p = tmp_path / "flags.json"
        runtime_flags.save(runtime_flags.RuntimeFlags(), p)
        # interval_s=0 so the refresh isn't gated during the test.
        poller = runtime_flags.FlagsPoller(p, interval_s=0.0)
        assert poller.get().scan_enabled["btc"] is True
        # Mutate and re-save.
        updated = runtime_flags.RuntimeFlags()
        updated.scan_enabled["btc"] = False
        # Ensure mtime actually changes across fast test runs.
        import os
        stat = p.stat()
        runtime_flags.save(updated, p)
        new_stat = p.stat()
        # If same mtime (cached filesystem), bump it.
        if new_stat.st_mtime == stat.st_mtime:
            os.utime(p, (stat.st_atime, stat.st_mtime + 1))
        assert poller.get().scan_enabled["btc"] is False

    def test_does_not_reread_when_mtime_unchanged(self, tmp_path: Path):
        p = tmp_path / "flags.json"
        runtime_flags.save(runtime_flags.RuntimeFlags(), p)
        poller = runtime_flags.FlagsPoller(p, interval_s=0.0)
        # Prime.
        first = poller.get()
        # Without mtime change, subsequent calls must return the same obj.
        second = poller.get()
        assert first is second


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


class TestQueryHelpers:
    def test_is_asset_scan_enabled_defaults_true(self):
        flags = runtime_flags.RuntimeFlags()
        for asset in runtime_flags.ASSETS:
            assert flags.is_asset_scan_enabled(asset)

    def test_is_asset_scan_enabled_case_insensitive(self):
        flags = runtime_flags.RuntimeFlags()
        assert flags.is_asset_scan_enabled("BTC") is True

    def test_is_strategy_enabled_defaults_true(self):
        flags = runtime_flags.RuntimeFlags()
        for strat in runtime_flags.STRATEGIES:
            assert flags.is_strategy_enabled(strat)

    def test_unknown_asset_defaults_to_enabled(self):
        """Future-proofing: a newly-added asset without a flag entry
        should still be scanned (permissive default)."""
        flags = runtime_flags.RuntimeFlags()
        assert flags.is_asset_scan_enabled("future_coin") is True


# ---------------------------------------------------------------------------
# Per-asset execution_enabled (separate from scan_enabled)
# ---------------------------------------------------------------------------


class TestExecutionEnabledDefaults:
    def test_default_all_enabled(self):
        flags = runtime_flags.RuntimeFlags()
        for a in runtime_flags.ASSETS:
            assert flags.is_asset_execution_enabled(a) is True

    def test_unknown_asset_defaults_to_enabled(self):
        flags = runtime_flags.RuntimeFlags()
        assert flags.is_asset_execution_enabled("future_coin") is True


class TestExecutionEnabledPatch:
    def test_patch_disables_single_asset(self):
        flags = runtime_flags.RuntimeFlags()
        patched = runtime_flags.apply_dashboard_patch(
            flags, {"execution_enabled": {"bnb": False}}
        )
        assert patched.is_asset_execution_enabled("bnb") is False
        # Other assets unaffected.
        assert patched.is_asset_execution_enabled("btc") is True

    def test_patch_unknown_asset_ignored(self):
        flags = runtime_flags.RuntimeFlags()
        patched = runtime_flags.apply_dashboard_patch(
            flags, {"execution_enabled": {"pepe": False}}
        )
        # All real assets still enabled.
        for a in runtime_flags.ASSETS:
            assert patched.is_asset_execution_enabled(a) is True

    def test_scan_and_execution_are_independent(self):
        """A disabled scan shouldn't auto-disable execution and vice versa."""
        flags = runtime_flags.RuntimeFlags()
        patched = runtime_flags.apply_dashboard_patch(
            flags, {"scan_enabled": {"btc": False}}
        )
        # Scan off, but execution still allowed.
        assert patched.is_asset_scan_enabled("btc") is False
        assert patched.is_asset_execution_enabled("btc") is True


class TestExecutionKillSwitchOverridesPerAsset:
    def test_kill_switch_disables_all_execution(self):
        flags = runtime_flags.RuntimeFlags()
        patched = runtime_flags.apply_dashboard_patch(
            flags, {"execution_kill_switch": True}
        )
        for a in runtime_flags.ASSETS:
            assert patched.is_asset_execution_enabled(a) is False

    def test_kill_switch_beats_explicit_per_asset_true(self):
        flags = runtime_flags.RuntimeFlags()
        patched = runtime_flags.apply_dashboard_patch(
            flags, {
                "execution_enabled": {"btc": True},
                "execution_kill_switch": True,
            }
        )
        assert patched.is_asset_execution_enabled("btc") is False


class TestExecutionEnabledPersistence:
    def test_save_and_load_round_trips(self, tmp_path):
        flags = runtime_flags.RuntimeFlags()
        flags.execution_enabled["btc"] = False
        flags.execution_enabled["eth"] = False
        path = tmp_path / "flags.json"
        runtime_flags.save(flags, path, author="test")
        loaded = runtime_flags.load(path)
        assert loaded.is_asset_execution_enabled("btc") is False
        assert loaded.is_asset_execution_enabled("eth") is False
        assert loaded.is_asset_execution_enabled("sol") is True
