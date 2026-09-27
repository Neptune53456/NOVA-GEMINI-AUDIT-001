from datetime import datetime, timezone
from threading import Event
from time import monotonic, sleep

from nova_api.god_eye.autonomous_research import watchdog
from nova_api.god_eye.scheduler import GodEyeScheduler
from nova_api.god_eye.star_finder import StarFinderConfig, rejection_gates
from nova_api.god_eye.trader import PaperLedger, TraderConfig


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_scheduler_huge_persisted_failure_count_is_bounded_and_observable():
    scheduler = GodEyeScheduler(
        lambda: (_ for _ in ()).throw(RuntimeError("provider down")),
        lambda: None,
        market_interval=10,
        max_backoff=300,
        clock=lambda: NOW,
        initial_state={"market": {"failures": 10**6}},
    )
    assert scheduler.run_due() is True
    state = scheduler.status()["tasks"]["market"]
    assert state["failures"] == 10**6 + 1
    assert state["next_run_at"] == "2026-01-01T00:05:00+00:00"
    assert "provider down" in state["last_error"]


def test_scheduler_isolates_critical_work_and_prevents_overlap():
    release, critical_ran = Event(), Event()
    calls = {"news": 0}

    def blocked_news():
        calls["news"] += 1
        release.wait(2)

    scheduler = GodEyeScheduler(lambda: None, blocked_news,
        extra_tasks={"paper_positions": (critical_ran.set, 10)})
    scheduler.start()
    try:
        deadline = monotonic() + 1
        while not critical_ran.is_set() and monotonic() < deadline: sleep(.01)
        assert critical_ran.is_set()
        for _ in range(5): scheduler.run_due()
        assert calls["news"] == 1
        assert scheduler.status()["tasks"]["paper_positions"]["lane"] == "critical"
    finally:
        release.set(); scheduler.stop()


def test_scheduler_stop_and_restart_do_not_duplicate_workers():
    scheduler = GodEyeScheduler(lambda: None, lambda: None)
    scheduler.start(); scheduler.start()
    first = tuple(scheduler._workers.values())
    scheduler.stop(); scheduler.start()
    try:
        assert all(not worker.is_alive() for worker in first)
        assert len(scheduler._workers) == 3
    finally:
        scheduler.stop()


def test_watchdog_reports_unexpected_scheduler_death():
    status = {"started_at": NOW.isoformat(), "scheduler_alive": False, "tasks": {}}
    report = watchdog(status, now_timestamp=NOW.timestamp())
    assert report["healthy"] is False
    assert {"task": "scheduler", "reason": "scheduler_dead"} in report["alerts"]


def test_ledger_recent_history_and_snapshot_payload_are_bounded():
    ledger = PaperLedger(TraderConfig(starting_capital=1_000_000))
    for index in range(PaperLedger.MAX_RECENT_FILLS + 25):
        fill = {"fill_id": f"f{index}", "order_id": f"o{index}", "instrument": "BTC-EUR",
                "side": "buy", "quantity": .001, "execution_price": 100.0, "fees": 0.0,
                "slippage": 0.0, "timestamp": NOW.isoformat(), "strategy": "NOVA_COMPOSITE",
                "opportunity_id": "x", "venue": "paper"}
        ledger.apply_fill(fill)
    assert len(ledger.fills) == PaperLedger.MAX_RECENT_FILLS
    duplicate = dict(ledger.fills[-1])
    cash = ledger.cash
    assert ledger.apply_fill(duplicate) is None
    assert ledger.cash == cash
    assert len(ledger.state()["fills"]) == PaperLedger.MAX_RECENT_FILLS


def test_cost_degrade_requires_margin_and_risk_gate_is_fail_closed():
    opportunity = {"expected_net_return": .0011, "cost_estimate": {"known": False,
        "cost_uncertainty": .8, "config": {"unknown_policy": "degrade"}},
        "intelligence_quality": 1, "calibrated_probability": .6, "sample_count": 30,
        "liquidity_quality": 1, "uncertainty": .1, "expected_gross_return": .01,
        "risk_approved": False}
    reasons = rejection_gates(opportunity, StarFinderConfig())
    assert "uncertain_cost_margin_insufficient" in reasons
    assert "risk_engine_rejected" in reasons
