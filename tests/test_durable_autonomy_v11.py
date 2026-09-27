from pathlib import Path

from nova_api.autonomy import GoalRunner, GoalStore
from nova_api.capabilities import build_default_registry
from nova_api.context_builder import ContextBuilder
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore


def _runner(root: Path) -> GoalRunner:
    journal = EventJournal(root / "events.sqlite3")
    registry = build_default_registry(journal, project_root=root)
    memory = MemoryStore(root / "memory.sqlite3")
    return GoalRunner(registry, journal, ContextBuilder(memory), memory,
                      store=GoalStore(root / "goals.sqlite3"))


def test_restart_invalidates_raw_confirmation_and_issues_fresh_one(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "write safely", "file exists", [
        {"capability_id": "filesystem.write", "arguments": {"path": "safe.txt", "content": "ready"}},
    ])
    waiting = runner.run(goal.goal_id)
    old_token = waiting.plan[0]["confirmation_token"]

    restarted = _runner(tmp_path)
    paused = restarted.store.get(goal.goal_id)
    assert paused.status == "paused"
    assert "confirmation_restart_required" in paused.blockers
    assert "confirmation_token" not in paused.plan[0]
    assert not (tmp_path / "safe.txt").exists()

    fresh = restarted.run(goal.goal_id)
    assert fresh.status == "awaiting_confirmation"
    assert fresh.plan[0]["confirmation_token"] != old_token
    records = restarted.durable_confirmations.for_goal(goal.goal_id)
    assert records[-2].decision == "expired"
    assert records[-1].decision == "pending"


def test_completed_crash_window_write_is_recovered_without_replay(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "write once", "file written", [
        {"capability_id": "filesystem.write", "arguments": {"path": "once.txt", "content": "hello"}},
    ])
    step = goal.plan[0]
    goal = runner._mark_mutation_state(goal, step, "STARTED_UNCERTAIN")
    runner._save(goal, status="running", phase="execute")
    result = runner.registry.execute("filesystem.write", step["arguments"], confirmed=True)
    assert result.status == "success"
    transaction_id = result.result["transaction_id"]

    restarted = _runner(tmp_path)
    assert len(restarted.registry.transactions.list_pending()) == 1
    completed = restarted.run(goal.goal_id)

    assert completed.status == "completed_verified"
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "hello"
    assert len(restarted.registry.transactions.list_pending()) == 1
    assert restarted.registry.transactions.list_pending()[0].transaction_id == transaction_id
    mutation = next(iter(completed.checkpoint["mutation_states"].values()))
    assert mutation["state"] == "VERIFIED"
    assert mutation["recovered_after_restart"] is True


def test_confirmation_is_bound_to_exact_effect_fingerprint(tmp_path):
    runner = _runner(tmp_path)
    goal = runner.create("c", "write approved payload only", "file exists", [
        {"capability_id": "filesystem.write", "arguments": {"path": "bound.txt", "content": "approved"}},
    ])
    waiting = runner.run(goal.goal_id)
    token = waiting.plan[0]["confirmation_token"]

    changed = runner.store.get(goal.goal_id)
    changed.plan[0]["arguments"]["content"] = "changed-after-approval"
    runner.store.save(changed)

    from nova_api.autonomy import GoalStateError
    import pytest
    with pytest.raises(GoalStateError, match="confirmation_action_changed"):
        runner.run(goal.goal_id, confirmed_token=token)
    assert not (tmp_path / "bound.txt").exists()
