from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest

from nova_api.god_eye.alternative import (MacroReleaseCollector, MacroSourceConfig, SecEdgarCollector,
    SecEdgarConfig, alternative_to_event)
from nova_api.god_eye.live_forward import LiveForwardValidator
from nova_api.god_eye.scheduler import GodEyeScheduler
from nova_api.god_eye.storage import GodEyeStore
from self_improvement.god_eye_benchmark import GodEyeImprovementBudget, judge_god_eye_candidate, validate_change_scope
from self_improvement.god_eye_supervisor import GodEyeSupervisorWorkflow

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

def test_live_adapters_are_config_gated_checkpointed_and_preserve_provenance():
    calls = []
    def get(url, headers):
        calls.append((url, headers))
        return {"filings": {"recent": {"accessionNumber": ["1-2"], "form": ["10-Q"],
            "filingDate": ["2026-01-02"], "primaryDocument": ["x.htm"]}}}
    assert SecEdgarCollector(SecEdgarConfig({"X": "1"}, "invalid"), get).collect() == ([], None)
    items, cursor = SecEdgarCollector(SecEdgarConfig({"X": "1"}, "Nova ops@example.com"), get).collect()
    assert len(items) == 1 and cursor and calls[0][1]["User-Agent"] == "Nova ops@example.com"
    event = alternative_to_event(items[0], ["X"])
    assert event.event_type == "earnings/financial_update" and event.evidence[0]["source"] == "sec_edgar"

def test_macro_does_not_invent_consensus_and_deduplicates_in_store(tmp_path):
    collector = MacroReleaseCollector([MacroSourceConfig("ecb", "https://ecb.example/releases")],
        lambda *_: [{"scheduled_at": NOW.isoformat(), "category": "Central bank rate decision", "actual": 4.0}])
    items, _ = collector.collect(); assert items[0].payload["surprise"] is None
    store = GodEyeStore(tmp_path / "g.db")
    assert store.save_alternative_items(items) == 1 and store.save_alternative_items(items) == 0

def test_scheduler_recovers_state_and_backs_off():
    state = {"sec": {"failures": 2, "last_status": "error", "next_run_at": (NOW-timedelta(seconds=1)).isoformat()}}
    scheduler = GodEyeScheduler(lambda: None, lambda: None, clock=lambda: NOW,
        extra_tasks={"sec": (lambda: {"status": "error"}, 10)}, initial_state=state)
    scheduler.run_due(); value = scheduler.status()["tasks"]["sec"]
    assert value["failures"] == 3 and value["next_run_at"] == (NOW+timedelta(seconds=80)).isoformat()

def test_live_forward_freezes_identity_and_resumes(tmp_path):
    store = GodEyeStore(tmp_path / "g.db"); manager = LiveForwardValidator(store)
    run = manager.start("v1", ["X"], now=NOW, validation_id="lf")
    assert manager.resume()["validation_id"] == "lf" and run["model_version"] == "v1"
    changed = dict(run, model_version="v2")
    with pytest.raises(ValueError): store.save_live_forward(changed)

def test_policy_budget_and_deterministic_judge_fail_closed():
    assert validate_change_scope(["nova_api/god_eye/forecasting.py"])["accepted"]
    assert not validate_change_scope(["nova_api/god_eye/sandbox.py"])["accepted"]
    assert not GodEyeImprovementBudget(max_files_changed=1).check(paths=["nova_api/god_eye/forecasting.py", "nova_api/god_eye/calibration.py"])["accepted"]
    incumbent = {"benchmark_score": 90, "calibration_error": .1, "max_drawdown": -.1,
                 "latency_ms": 10, "ingestion_reliability": .99}
    good = {**incumbent, "benchmark_score": 91, "tests_passed": True, "validation_passed": True,
            "holdout_passed": True, "robustness_passed": True}
    assert judge_god_eye_candidate(good, incumbent)["decision"] == "ACCEPT"
    assert judge_god_eye_candidate({**good, "holdout_passed": False}, incumbent)["decision"] == "REJECT"

def test_supervisor_workflow_blocks_forbidden_change_before_runner(tmp_path):
    store = GodEyeStore(tmp_path / "g.db")
    supervisor = SimpleNamespace(repo_root=tmp_path, engineering_runner=lambda _: (_ for _ in ()).throw(AssertionError()))
    result = GodEyeSupervisorWorkflow(supervisor, store).run("unsafe", planned_paths=["nova_api/god_eye/sandbox.py"],
        tests=lambda: {}, benchmark=lambda: {}, validation=lambda: {}, locked_holdout=lambda: {}, incumbent={})
    assert result["status"] == "rejected" and result["blocked_unsafe_modification"]

def test_supervisor_workflow_runs_authoritative_stages_and_accepts(tmp_path, monkeypatch):
    store = GodEyeStore(tmp_path / "g.db"); calls = []
    class Snapshot:
        def __init__(self, root): pass
        def capture_repository(self): calls.append("snapshot")
        def changed_paths(self): return ["nova_api/god_eye/forecasting.py"]
    class Recovery:
        def create(self, *_): calls.append("recovery")
        def clear(self): calls.append("clear")
    supervisor = SimpleNamespace(repo_root=tmp_path, recovery=Recovery(),
        engineering_runner=lambda _: {"worker_usage": {}}, _rollback=lambda _: calls.append("rollback"))
    monkeypatch.setattr("self_improvement.god_eye_supervisor.TrustedRepositorySnapshot", Snapshot)
    incumbent = {"benchmark_score": 90, "calibration_error": .1, "max_drawdown": -.1,
                 "latency_ms": 10, "ingestion_reliability": .99}
    result = GodEyeSupervisorWorkflow(supervisor, store).run("safe", planned_paths=["nova_api/god_eye/forecasting.py"],
        tests=lambda: {"passed": True}, benchmark=lambda: {"benchmark_score": 91},
        validation=lambda: {"passed": True, "robustness_passed": True, "calibration_error": .1,
                            "max_drawdown": -.1, "latency_ms": 10, "ingestion_reliability": .99},
        locked_holdout=lambda: {"passed": True}, incumbent=incumbent)
    assert result["status"] == "accepted" and calls == ["snapshot", "recovery", "clear"]
