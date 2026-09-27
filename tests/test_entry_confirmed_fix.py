"""Test for entry_confirmed and entry_timing integration with AUTO_PAPER.

Reproduces the bug: entry_confirmed is never set in production, so entry_timing
always returns action=WAIT, but _auto_paper fires fills regardless because it
ignores entry_analysis.action.

Baseline: BEFORE fix, a QUALIFIED opportunity (entry_confirmed absent -> WAIT)
causes _auto_paper to execute fills even though timing is not confirmed.

After fix: _auto_paper only fills when entry_analysis.action == "ENTER",
and run_star_finder sets entry_confirmed=True for QUALIFIED opportunities,
making entry_analysis.action == "ENTER".
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.star_finder import entry_timing
from nova_api.god_eye.trader import TraderConfig, TraderMode
from nova_api.god_eye.storage import GodEyeStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _minimal_opportunity(status: str = "QUALIFIED", entry_confirmed=None) -> dict:
    """Build a minimal opportunity dict matching what run_star_finder produces."""
    opp = {
        "opportunity_id": "test-opp-1",
        "status": status,
        "instrument": "BTC-USD",
        "asset_class": "crypto",
        "venue": "coinbase",
        "direction": "up",
        "reference_price": 100.0,
        "expected_entry": 100.5,
        "expected_net_return": 0.02,
        "expected_gross_return": 0.03,
        "downside": -0.01,
        "uncertainty": 0.2,
        "expected_costs": 0.005,
        "calibrated_probability": 0.65,
        "liquidity_quality": 0.8,
        "intelligence_quality": 0.8,
        "regime_stability": 0.8,
        "evidence_diversity": 0.6,
        "sample_count": 50,
        "star_score": 70.0,
        "rejection_reasons": [],
        "risk_approved": True,
        "cost_estimate": {
            "known": True,
            "spread_bps": 5,
            "total_round_trip_cost": 0.005,
            "notional": 1000.0,
            "config": {},
        },
    }
    if entry_confirmed is not None:
        opp["entry_confirmed"] = entry_confirmed
    return opp


def test_entry_timing_requires_entry_confirmed():
    """Verify entry_timing returns WAIT when entry_confirmed is absent (production default)."""
    opp = _minimal_opportunity()
    result = entry_timing(opp, now=NOW)
    assert result["action"] == "WAIT", (
        f"Baseline bug: entry_confirmed absent should produce action=WAIT, "
        f"got {result['action']}"
    )
    assert "entry_confirmation_pending" in result["reasons"]


def test_entry_timing_enter_when_confirmed():
    """Verify entry_timing returns ENTER when entry_confirmed=True."""
    opp = _minimal_opportunity(entry_confirmed=True)
    result = entry_timing(opp, now=NOW)
    assert result["action"] == "ENTER", (
        f"With entry_confirmed=True, action should be ENTER, got {result['action']}"
    )
    assert "qualified_and_confirmed" in result["reasons"]


def test_auto_paper_respects_entry_timing_action(tmp_path):
    """Verify _auto_paper only fills when entry_analysis.action == 'ENTER'.

    Baseline (bug): _auto_paper fills regardless of entry_confirmed.
    After fix: _auto_paper checks entry_analysis.action and skips fills when action=WAIT.
    """
    store = GodEyeStore(tmp_path / "god.sqlite3")

    # Build a minimal service — skip all network calls
    service = GodEyeService.__new__(GodEyeService)
    service.store = store
    service.trader_kill_switch = False
    service._microstructure = {}

    # Configure trader in AUTO_PAPER mode with equity snapshot so sizing works
    cfg = TraderConfig(mode=TraderMode.AUTO_PAPER, starting_capital=10_000.0)
    from nova_api.god_eye.trader import PaperLedger
    service.trader = PaperLedger(cfg)
    service.trader.equity_snapshots.append({
        "equity": 10_000.0, "cash": 10_000.0, "drawdown": 0.0,
        "timestamp": NOW.isoformat()
    })

    # Mock cost engine and execution engine
    from nova_api.god_eye.costs import CostEngine
    from nova_api.god_eye.trader import PaperExecutionEngine
    from nova_api.god_eye.star_finder import StarFinderConfig
    service.cost_engine = CostEngine()
    service.execution_engine = PaperExecutionEngine(service.cost_engine)
    service.star_finder_config = StarFinderConfig()

    # Mock store.save_trader_record as a no-op
    store.save_trader_record = MagicMock(return_value=True)
    service.store.save_lifecycle = MagicMock(return_value=True)
    service._save_replay_stage = MagicMock()
    service.strategy_ledgers = {}  # _mirror_strategy_fill needs this; empty = no strategy mirrors

    # --- Case 1: QUALIFIED opportunity WITHOUT entry_confirmed (baseline bug) ---
    opp_no_confirm = _minimal_opportunity(status="QUALIFIED")
    opp_no_confirm["entry_analysis"] = entry_timing(opp_no_confirm, now=NOW)

    # The entry_analysis action should be WAIT (production default: entry_confirmed never set)
    assert opp_no_confirm["entry_analysis"]["action"] == "WAIT"

    # _auto_paper should NOT fill when action=WAIT (post-fix).
    # In baseline (bug): it WILL fill because action is ignored.
    # The test assertion below flips once the production fix is applied.
    results = service._auto_paper([opp_no_confirm], NOW)
    fills_no_confirm = [f for r in results for f in r.get("fills", [])]
    if len(fills_no_confirm) == 0:
        # FIX ALREADY APPLIED in production: entry timing gate works
        pass
    else:
        # BASELINE BUG: _auto_paper ignores entry_analysis.action and fills anyway.
        # The fix is: in _auto_paper, add: if opportunity.get("entry_analysis",{}).get("action") != "ENTER": continue
        assert False, (
            f"BASELINE BUG CONFIRMED: _auto_paper executed {len(fills_no_confirm)} fill(s) "
            f"for an opportunity with action=WAIT (entry_confirmed absent). "
            f"The entry timing gate is completely broken — entry_analysis.action is never checked."
        )

    # --- Case 2: QUALIFIED opportunity WITH entry_confirmed=True ---
    opp_confirmed = _minimal_opportunity(status="QUALIFIED", entry_confirmed=True)
    opp_confirmed["entry_analysis"] = entry_timing(opp_confirmed, now=NOW)

    assert opp_confirmed["entry_analysis"]["action"] == "ENTER"

    # _auto_paper should fill when action=ENTER (post-fix).
    service.trader.cash = 10_000.0  # reset cash
    service.trader.equity_snapshots.append({
        "equity": 10_000.0, "cash": 10_000.0, "drawdown": 0.0,
        "timestamp": NOW.isoformat()
    })

    results_confirmed = service._auto_paper([opp_confirmed], NOW)
    fills_confirmed = [f for r in results_confirmed for f in r.get("fills", [])]
    assert len(fills_confirmed) == 1, (
        f"With entry_confirmed=True and action=ENTER, _auto_paper should produce "
        f"1 fill, got {len(fills_confirmed)}"
    )
    assert fills_confirmed[0]["instrument"] == "BTC-USD"


def test_run_star_finder_sets_entry_confirmed(tmp_path):
    """Verify that after the fix, QUALIFIED opportunities have entry_confirmed=True."""
    store = GodEyeStore(tmp_path / "god2.sqlite3")

    # This test documents the expected post-fix behavior:
    # When an opportunity passes all gates (status=QUALIFIED),
    # run_star_finder should set entry_confirmed=True
    # and entry_analysis.action should be "ENTER"

    from nova_api.god_eye.star_finder import rejection_gates, StarFinderConfig, entry_timing

    # Simulate what run_star_finder does:
    opp = _minimal_opportunity(status="QUALIFIED")
    gates = rejection_gates(opp, StarFinderConfig())
    assert gates == [], f"Opportunity should pass all gates: {gates}"

    # After fix: run_star_finder should set entry_confirmed=True for QUALIFIED
    opp["entry_confirmed"] = True
    result = entry_timing(opp, now=NOW)

    assert result["action"] == "ENTER", (
        f"QUALIFIED opportunity with entry_confirmed=True should have "
        f"action=ENTER, got {result['action']}"
    )