from __future__ import annotations

from pathlib import Path

import pytest

import model_router
from nova_api.context_builder import ContextBuilder
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore
from nova_api.missions import MissionManager, MissionStateError, MissionStore
from nova_api.capabilities import build_default_registry


def _manager(tmp_path: Path) -> MissionManager:
    journal = EventJournal(tmp_path / "events.sqlite3")
    return MissionManager(
        build_default_registry(journal, project_root=tmp_path),
        journal,
        store=MissionStore(tmp_path / "missions.sqlite3"),
    )


def test_mission_store_filters_by_conversation_and_state(tmp_path: Path) -> None:
    store = MissionStore(tmp_path / "missions.sqlite3")
    a = store.create("a", "active", [{"step_id": "step-1", "capability_id": "filesystem.list", "arguments": {}, "status": "pending"}])
    b = store.create("b", "other", [{"step_id": "step-1", "capability_id": "filesystem.list", "arguments": {}, "status": "pending"}])
    assert [m.mission_id for m in store.list(conversation_id="a", states={"pending"})] == [a.mission_id]
    assert b.mission_id not in {m.mission_id for m in store.list(conversation_id="a")}


def test_corrupt_mission_row_fails_safe_but_does_not_poison_listing(tmp_path: Path) -> None:
    store = MissionStore(tmp_path / "missions.sqlite3")
    mission = store.create("c", "inspect", [{"step_id": "step-1", "capability_id": "filesystem.list", "arguments": {}, "status": "pending"}])
    with store._connect() as db:
        db.execute("UPDATE missions SET checkpoint_json='not-json' WHERE mission_id=?", (mission.mission_id,))
    with pytest.raises(MissionStateError, match="corrupt_mission_state"):
        store.get(mission.mission_id)
    assert store.list() == []


def test_context_prefers_active_same_conversation_mission(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    completed = manager.create("c", "old completed", [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    completed = manager._save(completed, state="completed", error=None, current_step=1)
    active = manager.create("c", "active mission", [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    package = ContextBuilder(MemoryStore(tmp_path / "memory.sqlite3"), missions=manager).build(
        "hello", conversation_id="c"
    )
    assert "active mission" in package.content
    assert "old completed" not in package.content
    refs = [d["reference"] for d in package.diagnostics if d["source"] == "mission"]
    assert refs == [active.mission_id]


def test_memory_equal_score_prefers_newer_evidence(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    older = store.remember(memory_type="FACT", source_type="test", provenance="USER_STATED", subject="alpha one", content="shared detail")
    newer = store.remember(memory_type="FACT", source_type="test", provenance="USER_STATED", subject="alpha two", content="shared detail")
    with store._connect() as db:
        db.execute("UPDATE memories SET updated_at='2026-01-01T00:00:00+00:00' WHERE memory_id=?", (older.memory_id,))
        db.execute("UPDATE memories SET updated_at='2026-02-01T00:00:00+00:00' WHERE memory_id=?", (newer.memory_id,))
    results = store.search("alpha shared", limit=2, touch=False)
    assert [item.item.memory_id for item in results] == [newer.memory_id, older.memory_id]


def test_missing_ollama_client_degrades_to_explicit_local_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(model_router, "ollama", None)
    with pytest.raises(model_router.ModelRouterError) as exc:
        model_router.embed("hello")
    assert exc.value.kind == "local_model_unavailable"

from nova_api.autonomy import GoalExecution, GoalStateError, GoalStore


def _goal(goal_id: str = "g") -> GoalExecution:
    return GoalExecution(
        goal_id=goal_id, mission_id=None, conversation_id="c", objective="inspect",
        success_criteria="verified", status="pending", phase="plan", plan_version=1,
        current_step=0, replans=0, failed_steps=0, mutating_actions=0, discovery_actions=0,
        model_calls=0, created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        plan=[], evidence=[], blockers=[], checkpoint={}, metrics={},
    )


def test_corrupt_goal_row_does_not_prevent_store_restart_or_listing(tmp_path: Path) -> None:
    store = GoalStore(tmp_path / "goals.sqlite3")
    store.save(_goal())
    with store._connect() as db:
        db.execute("UPDATE goals SET payload_json='not-json' WHERE goal_id='g'")
    restarted = GoalStore(store.path)
    with pytest.raises(GoalStateError, match="corrupt_goal_state"):
        restarted.get("g")
    assert restarted.list() == []


def test_memory_store_restart_survives_corrupt_legacy_tags(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory-tags.sqlite3")
    item = store.remember(
        memory_type="FACT", source_type="test", provenance="USER_STATED",
        subject="alpha", content="useful detail", tags=["one"],
    )
    with store._connect() as db:
        db.execute("UPDATE memories SET tags_json='not-json' WHERE memory_id=?", (item.memory_id,))
        db.execute("DELETE FROM memory_terms WHERE memory_id=?", (item.memory_id,))
    restarted = MemoryStore(store.path)
    restored = restarted.get(item.memory_id)
    assert restored is not None
    assert restored.tags == ()
