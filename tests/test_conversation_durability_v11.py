from pathlib import Path
import asyncio

from nova_api.conversation_service import ConversationService
from nova_api.conversation_store import ConversationStore
from nova_api.state import ApiStateStore


class Engine:
    def respond(self, content: str, history: list[dict[str, str]], mode: str) -> str:
        del history, mode
        return f"echo:{content}"


def test_conversation_messages_survive_restart_with_bounded_store(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    service = ConversationService(Engine(), ApiStateStore(), conversation_store=store,
                                  id_factory=iter(["c", "u1", "a1"]).__next__)
    conversation_id = service.create().conversation_id
    asyncio.run(service.send(conversation_id, "bonjour"))

    restarted = ConversationService(Engine(), ApiStateStore(), conversation_store=ConversationStore(store.path))
    restored = restarted.get(conversation_id)
    assert [message.content for message in restored.messages] == ["bonjour", "echo:bonjour"]
    assert restored.status == "success"


def test_inflight_state_is_not_revived_as_busy_after_restart(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    service = ConversationService(Engine(), ApiStateStore(), conversation_store=store, id_factory=lambda: "c")
    cid = service.create().conversation_id
    session = service._sessions[cid]
    session["busy"] = True
    session["status"] = "thinking"
    session["generation_id"] = "old-generation"
    service._persist_session(cid)

    restarted = ConversationService(Engine(), ApiStateStore(), conversation_store=ConversationStore(store.path))
    assert restarted._sessions[cid]["busy"] is False
    assert restarted._sessions[cid]["generation_id"] is None
    assert restarted.get(cid).status == "idle"


def test_goal_and_mission_associations_are_durable(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    service = ConversationService(Engine(), ApiStateStore(), conversation_store=store, id_factory=lambda: "c")
    cid = service.create().conversation_id
    service._sessions[cid]["active_goal_ids"] = ["g1"]
    service._sessions[cid]["active_mission_ids"] = ["m1"]
    service._persist_session(cid)

    restarted = ConversationService(Engine(), ApiStateStore(), conversation_store=ConversationStore(store.path))
    assert restarted._sessions[cid]["active_goal_ids"] == ["g1"]
    assert restarted._sessions[cid]["active_mission_ids"] == ["m1"]
