from pathlib import Path

from nova_api.autonomy import GoalRunner, GoalStore
from nova_api.capabilities import build_default_registry
from nova_api.context_builder import ContextBuilder
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore


def test_repeated_restart_safe_read_goals_do_not_leak_state(tmp_path: Path) -> None:
    (tmp_path / "probe.txt").write_text("stable", encoding="utf-8")
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    store = GoalStore(tmp_path / "goals.sqlite3")

    completed_ids = []
    for index in range(40):
        runner = GoalRunner(registry, journal, ContextBuilder(memory), memory, store=store)
        goal = runner.create(f"c-{index}", "read probe", "read is verified", [
            {"capability_id": "filesystem.read", "arguments": {"path": "probe.txt"}},
        ])
        completed = runner.run(goal.goal_id)
        assert completed.status == "completed_verified"
        completed_ids.append(completed.goal_id)

    assert len(completed_ids) == len(set(completed_ids)) == 40
    assert all(store.get(goal_id).status == "completed_verified" for goal_id in completed_ids)
