from pathlib import Path
import asyncio

import pytest

from nova_api.capabilities import build_default_registry
from nova_api.journal import EventJournal
from nova_api.missions import MissionManager, MissionStateError, MissionStore
from nova_api.conversation_service import ConversationService
from nova_api.state import ApiStateStore


class _PlainEngine:
    def respond(self, content: str, history: list[dict[str, str]], mode: str) -> str:
        del content, history, mode
        return "réponse simple"


def manager(tmp_path: Path) -> MissionManager:
    journal = EventJournal(tmp_path / "events.sqlite3")
    return MissionManager(build_default_registry(journal, project_root=tmp_path), journal,
                          store=MissionStore(tmp_path / "missions.sqlite3"))


def test_multistep_mission_checkpoints_each_verified_step(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    service = manager(tmp_path)
    mission = service.create("conversation", "inspect", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
        {"capability_id": "filesystem.read", "arguments": {"path": "README.md"}},
    ])
    completed = service.run(mission.mission_id)
    assert completed.state == "completed"
    assert completed.checkpoint["completed_steps"] == [0, 1]
    assert completed.checkpoint["last_result"]["capability_id"] == "filesystem.read"
    assert "content" not in completed.checkpoint["last_result"]


def test_write_waits_for_hmac_confirmation_then_checkpoints(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("conversation", "write", [
        {"capability_id": "filesystem.write", "arguments": {"path": "note.txt", "content": "safe"}},
    ])
    waiting = service.run(mission.mission_id)
    token = waiting.steps[0]["confirmation_token"]
    assert waiting.state == "awaiting_confirmation"
    completed = service.decide_confirmation(mission.mission_id, token, True)
    assert completed.state == "completed"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "safe"
    assert completed.checkpoint["transaction_id"]


def test_restart_pauses_running_mission_without_automatic_resume(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("conversation", "inspect", [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    with service.store._connect() as connection:
        connection.execute("UPDATE missions SET state='running' WHERE mission_id=?", (mission.mission_id,))
    restarted = MissionManager(service.registry, service.journal, store=MissionStore(service.store.path))
    recovered = restarted.store.get(mission.mission_id)
    assert recovered.state == "paused"
    assert recovered.last_error_category == "interrupted_uncertain"
    completed = restarted.run(mission.mission_id)
    assert completed.state == "completed"
    assert completed.checkpoint["recovery_generation"] == 1


def test_cancelled_mission_does_not_execute_steps(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("conversation", "inspect", [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    assert service.cancel(mission.mission_id).state == "cancelled"
    assert service.store.get(mission.mission_id).current_step == 0


def test_refused_confirmation_pauses_without_writing(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("conversation", "write", [
        {"capability_id": "filesystem.write", "arguments": {"path": "note.txt", "content": "never"}},
    ])
    waiting = service.run(mission.mission_id)
    refused = service.decide_confirmation(mission.mission_id, waiting.steps[0]["confirmation_token"], False)
    assert refused.state == "paused"
    assert refused.last_error_category == "confirmation_refused"
    assert not (tmp_path / "note.txt").exists()


def test_explicit_conversational_mission_streams_compact_progress(tmp_path: Path) -> None:
    missions = manager(tmp_path)
    service = ConversationService(_PlainEngine(), ApiStateStore(), mission_manager=missions)
    conversation_id = service.create().conversation_id

    async def collect() -> list[dict[str, object]]:
        generation_id = service.start_generation(conversation_id, "Mission: inspecte le projet en plusieurs étapes")
        return [event async for event in service.stream(generation_id)]

    events = asyncio.run(collect())
    mission_events = [event for event in events if str(event["event"]).startswith("mission.")]
    assert [event["event"] for event in mission_events] == [
        "mission.created", "mission.started", "mission.step.started", "mission.step.completed",
        "mission.step.started", "mission.step.completed", "mission.completed",
    ]
    assert all("objective" not in event and "arguments" not in event for event in mission_events)
    assert service.get(conversation_id).messages[-1].content == "Mission terminée."


def test_simple_conversation_keeps_mvp5_fallback(tmp_path: Path) -> None:
    missions = manager(tmp_path)
    service = ConversationService(_PlainEngine(), ApiStateStore(), mission_manager=missions)
    conversation_id = service.create().conversation_id

    async def collect() -> list[dict[str, object]]:
        generation_id = service.start_generation(conversation_id, "Bonjour Nova")
        return [event async for event in service.stream(generation_id)]

    events = asyncio.run(collect())
    assert events[0]["event"] == "generation.started"
    assert events[-1]["event"] == "generation.completed"
    assert not any(str(event["event"]).startswith("mission.") for event in events)
    assert missions.store.list() == []
