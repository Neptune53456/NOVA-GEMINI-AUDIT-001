"""Behavioral probes through the real goal planner and execution runner."""
import json
import pytest

from nova_api.autonomy import GoalRunner, GoalStore
from nova_api.capabilities import build_default_registry
from nova_api.context_builder import ContextBuilder
from nova_api.initial_goal_planner import InitialGoalPlanner
from nova_api.initial_goal_planner import InitialPlanInvalid
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore


def _runner(tmp_path, replies):
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    prompts = []

    def chat_fn(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return {"message": {"content": json.dumps({"steps": replies.pop(0)})}}

    planner = InitialGoalPlanner(registry, chat_fn=chat_fn)
    runner = GoalRunner(registry, journal, ContextBuilder(memory), memory,
                        store=GoalStore(tmp_path / "goals.sqlite3"), initial_planner=planner)
    return runner, prompts


def test_production_goal_recovers_from_failed_read_with_fresh_plan(tmp_path):
    runner, prompts = _runner(tmp_path, [
        [{"capability_id": "filesystem.read", "arguments": {"path": "missing.txt"}}],
        [{"capability_id": "filesystem.list", "arguments": {"path": "."}}],
    ])
    goal = runner.create_from_objective("c", "Inspect available project files")
    completed = runner.run(goal.goal_id)
    assert completed.status == "completed_verified"
    assert completed.replans == 1
    assert [e["capability_id"] for e in completed.evidence] == ["filesystem.read", "filesystem.list"]
    assert "missing.txt" in prompts[1]


def test_identical_failed_replan_is_rejected_before_reexecution(tmp_path):
    runner, _ = _runner(tmp_path, [
        [{"capability_id": "filesystem.read", "arguments": {"path": "missing.txt"}}],
    ])
    class Repeating:
        def replan(self, objective, context, state):
            return [{"capability_id": "filesystem.read", "arguments": {"path": "missing.txt"}}]
    runner.planner = Repeating()
    goal = runner.create_from_objective("c", "Inspect missing project file")
    blocked = runner.run(goal.goal_id)
    assert blocked.status == "blocked"
    assert len(blocked.evidence) == 1
    assert blocked.discovery_actions == 1


def test_replan_cannot_downgrade_exact_readback_criterion(tmp_path):
    runner, _ = _runner(tmp_path, [])
    (tmp_path / "other.txt").write_text("other", encoding="utf-8")
    goal = runner.create("c", "Check requested content", "target.txt exists in the allowed workspace and its content equals ready", [
        {"capability_id": "filesystem.read", "arguments": {"path": "target.txt"},
         "verification": {"content": "ready"}},
    ])
    class Alternative:
        def replan(self, objective, context, state):
            return [{"capability_id": "filesystem.read", "arguments": {"path": "other.txt"}}]
    runner.planner = Alternative()
    completed = runner.run(goal.goal_id)
    assert completed.status != "completed_verified"


def test_simple_direct_objective_uses_one_planning_call_and_one_tool(tmp_path):
    runner, prompts = _runner(tmp_path, [[
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ]])
    goal = runner.create_from_objective("c", "List project files")
    result = runner.run(goal.goal_id)
    assert result.status == "completed_verified"
    assert result.model_calls == 1 and result.discovery_actions == 1
    assert len(prompts) == 1


def test_multistep_plan_preserves_observations_and_tool_order(tmp_path):
    runner, _ = _runner(tmp_path, [[
        {"capability_id": "project.basic_info", "arguments": {}},
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ]])
    goal = runner.create_from_objective("c", "Inspect project and list files")
    result = runner.run(goal.goal_id)
    assert result.status == "completed_verified"
    assert [e["capability_id"] for e in result.evidence] == ["project.basic_info", "filesystem.list"]
    assert result.current_step == 2


def test_unavailable_capability_is_rejected_before_execution(tmp_path):
    runner, _ = _runner(tmp_path, [[
        {"capability_id": "imaginary.tool", "arguments": {}},
    ]] * 2)
    with pytest.raises(InitialPlanInvalid):
        runner.create_from_objective("c", "Use unavailable tool")
    assert runner.store.list() == []


def test_verified_write_and_readback_across_confirmation(tmp_path):
    runner, _ = _runner(tmp_path, [[
        {"capability_id": "filesystem.write", "arguments": {"path": "note.txt", "content": "ready"}},
        {"capability_id": "filesystem.read", "arguments": {"path": "note.txt"}},
    ]])
    goal = runner.create_from_objective("c", "Create note then verify content")
    waiting = runner.run(goal.goal_id)
    assert waiting.status == "awaiting_confirmation"
    result = runner.run(goal.goal_id, confirmed_token=waiting.plan[0]["confirmation_token"])
    assert result.status == "completed_verified"
    assert result.current_step == 2
    assert result.plan[1]["verification"] == {"content": "ready"}


def test_unverifiable_claim_does_not_become_verified(tmp_path):
    runner, _ = _runner(tmp_path, [])
    runner.initial_planner = None
    goal = runner.create("c", "Check a value", "value must be ready", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."},
         "verification": {"content": "ready"}},
    ])
    result = runner.run(goal.goal_id)
    assert result.status == "blocked"
    assert result.evidence[0]["verification_state"] == "FAILED"
