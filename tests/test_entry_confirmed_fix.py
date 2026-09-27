"""Test for entry_confirmed and entry_timing integration with AUTO_PAPER.

Campaign 002 authoritative decisions:
- BUG-001 FIXED: _auto_paper now checks entry_analysis.action == "ENTER" before filling.
- BUG-002 FIXED: tautological entry_confirmed=True assignment removed.
- BUG-003 UNRESOLVED: no evidence-backed confirmation producer exists.
  QUALIFIED + no confirmation -> action=WAIT -> zero new-entry fills (fail-closed).
- test_run_star_finder_sets_entry_confirmed is INVESTIGATION_TEST / DESIGN_PROPOSAL,
  not pre-existing production evidence. See EXP-012 in titan/EXPERIMENT_JOURNAL.md.

Expected post-fix behavior:
- QUALIFIED + action=WAIT -> _auto_paper returns zero fills (fail-closed)
- QUALIFIED + action=ENTER (via entry_confirmed=True) -> _auto_paper may fill
- test_god_eye_closure_gate.py uses explicit entry_analysis.action="ENTER" as TEST SETUP
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

    # --- Case 1: QUALIFIED opportunity WITHOUT entry_confirmed (fail-closed) ---
    opp_no_confirm = _minimal_opportunity(status="QUALIFIED")
    opp_no_confirm["entry_analysis"] = entry_timing(opp_no_confirm, now=NOW)

    # BUG-001 fix: _auto_paper now checks entry_analysis.action == "ENTER".
    # Since entry_confirmed is absent, action = WAIT and _auto_paper must skip this fill.
    assert opp_no_confirm["entry_analysis"]["action"] == "WAIT", (
        "Without entry_confirmed, entry_timing should return action=WAIT"
    )
    results = service._auto_paper([opp_no_confirm], NOW)
    fills_no_confirm = [f for r in results for f in r.get("fills", [])]
    # POST-FIX ASSERTION: zero fills when action=WAIT (fail-closed)
    assert len(fills_no_confirm) == 0, (
        f"FAIL-CLOSED VIOLATION: _auto_paper executed {len(fills_no_confirm)} fill(s) "
        f"for an opportunity with action=WAIT. entry_analysis.action gate is broken."
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


def test_entry_confirmed_produces_enter_action(tmp_path):
    """INVESTIGATION TEST / DESIGN PROPOSAL — not pre-existing production evidence.

    Validates the signal path: when entry_confirmed=True is set, entry_analysis
    action becomes ENTER, allowing _auto_paper to proceed (if other gates pass).

    This test was produced during Campaign 001 investigation. It validates what
    SHOULD happen with proper confirmation, not what production currently does.
    BUG-002 (tautological fix) was removed. No production mechanism produces
    entry_confirmed=True yet. See BUG-003 in titan/KNOWN_ISSUES.md.

    To make this test meaningful in production, a real Entry Confirmation Engine
    must be designed and implemented with evidence-backed semantics — not guessed
    duration values. Until then, this test documents the expected signal path.
    """
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