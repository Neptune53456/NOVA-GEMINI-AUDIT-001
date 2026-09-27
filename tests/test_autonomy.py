from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.autonomy import GoalBudgets, GoalRunner, GoalStateError, GoalStore
from nova_api.agent_loop import ConfirmationStore
from nova_api.capabilities import build_default_registry
from nova_api.context_builder import ContextBuilder
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore
from nova_api.state import ApiStateStore


class _Replanner:
    def __init__(self, plan):
        self.plan = plan
        self.calls = 0

    def replan(self, objective, context, state):
        del objective, context, state
        self.calls += 1
        return self.plan


def _runner(tmp_path: Path, *, planner=None, budgets=GoalBudgets()):
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    return GoalRunner(registry, journal, ContextBuilder(memory), memory,
                      store=GoalStore(tmp_path / "goals.sqlite3"), planner=planner, budgets=budgets)


def test_verified_goal_waits_for_confirmation_then_verifies_and_remembers(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("conversation", "Create the expected note", "note content equals ready", [
        {"capability_id": "filesystem.write", "arguments": {"path": "note.txt", "content": "ready"}},
        {"capability_id": "filesystem.read", "arguments": {"path": "note.txt"},
         "verification": {"content": "ready"}},
    ])
    waiting = runner.run(goal.goal_id)
    assert waiting.status == "awaiting_confirmation"
    token = waiting.plan[0]["confirmation_token"]
    completed = runner.run(goal.goal_id, confirmed_token=token)
    assert completed.status == "completed_verified"
    assert completed.checkpoint["completed_step_ids"] == ["step-1", "step-2"]
    assert len(completed.evidence) == 2
    assert MemoryStore(tmp_path / "memory.sqlite3").search("expected note")[0].item.memory_type == "OUTCOME"


def test_failure_replans_once_without_replaying_failed_step(tmp_path):
    planner = _Replanner([{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    runner = _runner(tmp_path, planner=planner)
    goal = runner.create("c", "Inspect available files", "directory listing verified", [
        {"capability_id": "filesystem.read", "arguments": {"path": "missing.txt"}},
    ])
    completed = runner.run(goal.goal_id)
    assert completed.status == "completed_verified"
    assert completed.replans == 1 and planner.calls == 1
    assert [item["capability_id"] for item in completed.evidence] == ["filesystem.read", "filesystem.list"]


def test_no_progress_is_bounded_by_replans_and_failed_steps(tmp_path):
    failed = [{"capability_id": "filesystem.read", "arguments": {"path": "still-missing.txt"}}]
    planner = _Replanner(failed)
    runner = _runner(tmp_path, planner=planner, budgets=GoalBudgets(max_replans=2, max_failed_steps=3))
    goal = runner.create("c", "Find missing", "file read verified", failed)
    blocked = runner.run(goal.goal_id)
    assert blocked.status == "blocked"
    assert blocked.replans == 2 and blocked.failed_steps == 3
    assert planner.calls == 2


def test_restart_preserves_completed_steps_and_does_not_replay_write(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "Resume safely", "both files observed", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
        {"capability_id": "filesystem.read", "arguments": {"path": "later.txt"}},
    ])
    # Represent a process dying immediately after the first verified checkpoint.
    first = dict(goal.plan[0]); first["status"] = "completed"
    running = replace(goal, status="running", phase="execute", current_step=1,
                      plan=[first, goal.plan[1]], evidence=[{"evidence_id": "e1", "source": "DETERMINISTIC",
                      "provenance": "DETERMINISTIC", "step_id": "step-1", "summary": "listed",
                      "verification_state": "VERIFIED", "timestamp": goal.updated_at,
                      "capability_id": "filesystem.list", "action_id": "a1"}],
                      checkpoint={"completed_step_ids": ["step-1"], "plan_version": 1, "next_phase": "execute"})
    runner.store.save(running)
    (tmp_path / "later.txt").write_text("ok", encoding="utf-8")
    restarted = _runner(tmp_path)
    recovered = restarted.store.get(goal.goal_id)
    assert recovered.status == "paused" and recovered.current_step == 1
    completed = restarted.run(goal.goal_id)
    assert completed.status == "completed_verified"
    assert completed.checkpoint["completed_step_ids"] == ["step-1", "step-2"]


def test_failed_goal_verification_rolls_back_reversible_write(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "Try bounded edit", "write has expected impossible field", [
        {"capability_id": "filesystem.write", "arguments": {"path": "temp.txt", "content": "changed"},
         "verification": {"impossible": True}},
    ])
    waiting = runner.run(goal.goal_id)
    blocked = runner.run(goal.goal_id, confirmed_token=waiting.plan[0]["confirmation_token"])
    assert blocked.status == "blocked"
    assert not (tmp_path / "temp.txt").exists()


def test_refused_goal_confirmation_pauses_without_mutation(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "Do not write without approval", "file exists", [
        {"capability_id": "filesystem.write", "arguments": {"path": "refused.txt", "content": "no"}},
    ])
    waiting = runner.run(goal.goal_id)
    paused = runner.run(goal.goal_id, confirmed_token=waiting.plan[0]["confirmation_token"], approved=False)
    assert paused.status == "paused" and paused.blockers == ["confirmation_refused"]
    assert not (tmp_path / "refused.txt").exists()


def test_expired_goal_confirmation_is_rejected_without_mutation(tmp_path):
    runner = _runner(tmp_path)
    runner.confirmations = ConfirmationStore(ttl=-1)
    goal = runner.create("c", "Do not accept stale approval", "file exists", [
        {"capability_id": "filesystem.write", "arguments": {"path": "expired.txt", "content": "no"}},
    ])
    waiting = runner.run(goal.goal_id)

    with pytest.raises(GoalStateError, match="invalid_confirmation"):
        runner.run(goal.goal_id, confirmed_token=waiting.plan[0]["confirmation_token"])

    assert not (tmp_path / "expired.txt").exists()


def test_goal_api_create_get_pause_cancel(tmp_path):
    runner = _runner(tmp_path)
    client = TestClient(create_app(goal_runner=runner, memory_store=runner.memory,
                                   capability_registry=runner.registry, journal=runner.journal))
    response = client.post("/api/v1/goals", json={"conversation_id": "c", "objective": "Inspect",
        "success_criteria": "listing verified", "steps": [{"capability_id": "filesystem.list",
        "arguments": {"path": "."}}]})
    assert response.status_code == 201
    goal_id = response.json()["goal_id"]
    assert client.get(f"/api/v1/goals/{goal_id}").status_code == 200
    assert client.post(f"/api/v1/goals/{goal_id}/pause").json()["status"] == "paused"
    assert client.post(f"/api/v1/goals/{goal_id}/cancel").json()["status"] == "cancelled"


def test_goal_resume_clears_confirmation_presence_after_verified_completion(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "Write and verify", "content verified", [
        {"capability_id": "filesystem.write", "arguments": {"path": "state.txt", "content": "ready"}},
        {"capability_id": "filesystem.read", "arguments": {"path": "state.txt"},
         "verification": {"content": "ready"}},
    ])
    client = TestClient(create_app(goal_runner=runner, memory_store=runner.memory,
                                   capability_registry=runner.registry, journal=runner.journal))
    waiting = client.post(f"/api/v1/goals/{goal.goal_id}/resume").json()
    assert waiting["status"] == "awaiting_confirmation"
    assert client.get("/api/v1/state").json()["state"] == "awaiting-confirmation"

    response = client.post(f"/api/v1/goals/{goal.goal_id}/resume", json={
        "token": waiting["plan"][0]["confirmation"]["token"], "approved": True,
    })

    assert response.status_code == 200
    assert response.json()["status"] == "completed_verified"
    assert client.get("/api/v1/state").json() == {
        "state": "success", "label": "Objectif vérifié", "busy": False,
        "message": "L’objectif est terminé et vérifié.",
    }


@pytest.mark.parametrize(("goal_status", "state", "busy"), [
    ("awaiting_confirmation", "awaiting-confirmation", False),
    ("running", "acting", True),
    ("acting", "acting", True),
    ("verifying", "acting", True),
    ("completed_verified", "success", False),
    ("completed_unverified", "error", False),
    ("blocked", "error", False),
    ("failed", "error", False),
    ("cancelled", "idle", False),
])
def test_goal_status_has_one_authoritative_presentation(goal_status, state, busy):
    store = ApiStateStore()
    store.set_goal_status(goal_status)
    snapshot = store.snapshot()
    assert snapshot.state == state
    assert snapshot.busy is busy


def test_goal_budget_exhaustion_is_structured(tmp_path):
    runner = _runner(tmp_path, budgets=GoalBudgets(max_discovery_actions=0))
    goal = runner.create("c", "Inspect", "listing verified", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ])
    blocked = runner.run(goal.goal_id)
    assert blocked.status == "blocked"
    assert blocked.blockers == ["GOAL_BUDGET_EXCEEDED"]
    assert blocked.current_step == 0


def test_user_pause_resume_continues_from_checkpoint(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "Inspect after pause", "listing verified", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ])
    assert runner.pause(goal.goal_id).status == "paused"
    completed = runner.run(goal.goal_id)
    assert completed.status == "completed_verified"
    assert completed.current_step == 1
    events = runner.journal.recent(limit=30)
    assert any(event.type == "goal.completed" and event.goal_id == goal.goal_id for event in events)


def test_realistic_demo_startup_fix_survives_restart_in_memory(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "Diagnose why the demo app does not start", "config is fixed and readable", [
        {"objective": "apply bounded config fix", "expected_evidence": "transaction hash verified",
         "capability_id": "filesystem.write", "arguments": {"path": "demo.conf", "content": "enabled=true"}},
        {"objective": "verify startup config", "expected_evidence": "exact expected config",
         "capability_id": "filesystem.read", "arguments": {"path": "demo.conf"},
         "verification": {"content": "enabled=true"}},
    ])
    waiting = runner.run(goal.goal_id)
    completed = runner.run(goal.goal_id, confirmed_token=waiting.plan[0]["confirmation_token"])
    assert completed.status == "completed_verified"

    reopened = MemoryStore(tmp_path / "memory.sqlite3")
    result = reopened.search("What fixed the demo startup issue?")
    assert result and result[0].item.memory_type == "OUTCOME"
    assert result[0].item.provenance == "DETERMINISTIC"
