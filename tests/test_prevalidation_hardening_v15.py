from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from nova_api.application_memory import ApplicationMemory
from nova_api.conversation_service import ConversationService
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryRejected, MemoryStore
from nova_api.model_usage import ModelUsageStore
from nova_api.recovery import RecoveryPolicy
from nova_api.state import ApiStateStore


class _EchoEngine:
    def respond(self, content, history, mode):
        return f"ok:{content}"


def test_memory_structured_secret_boundary_and_expiry(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    with pytest.raises(MemoryRejected):
        store.remember(
            memory_type="FACT", source_type="test", provenance="USER_STATED",
            subject="credential", content='{"nested":{"api_key":"DEMO_ONLY_NOT_REAL"}}',
        )
    harmless = store.remember(
        memory_type="FACT", source_type="test", provenance="USER_STATED",
        subject="form", content="the password field is empty",
    )
    assert store.get(harmless.memory_id) is not None

    expired = store.remember(
        memory_type="TASK_STATE", source_type="test", provenance="DETERMINISTIC",
        subject="temporary", content="temporary state",
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert store.get(expired.memory_id) is None
    assert all(item.memory_id != expired.memory_id for item in store.list())


def test_model_usage_failure_is_not_double_counted(tmp_path):
    store = ModelUsageStore(tmp_path / "usage.sqlite3")
    error = RuntimeError("offline")
    rows = store.record_failure(purpose="test", error=error, goal_id="goal")
    assert len(rows) == 1
    assert len(store.for_goal("goal")) == 1


def test_recovery_normalizes_real_runtime_categories():
    policy = RecoveryPolicy()
    assert policy.decide(failure_category="STALE_ELEMENT_REFERENCE").action == "reresolve_target"
    assert policy.decide(failure_category="provider_unavailable").action == "retry_same"
    assert policy.decide(failure_category="repeated_action").repeated_strategy_blocked is True


def test_application_memory_does_not_create_unknown_app_bucket(tmp_path):
    memory = ApplicationMemory(tmp_path / "apps.sqlite3")
    assert memory.app_identity() == ""
    memory.record(
        app_identity="", intent="click save", target_label="Save", control_type="Button",
        structural_fingerprint="abc", action_type="invoke", success=True,
    )
    assert memory.hints(app_identity="", intent="click save") == []


def test_conversation_history_rolls_instead_of_dead_ending(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    service = ConversationService(_EchoEngine(), ApiStateStore(), max_messages=4, journal=journal)
    conversation = service.create()
    for value in ("one", "two", "three"):
        result = asyncio.run(service.send(conversation.conversation_id, value))
        assert result.assistant_message.content == f"ok:{value}"
    detail = service.get(conversation.conversation_id)
    assert len(detail.messages) == 4
    assert detail.messages[-1].content == "ok:three"


def test_journal_survives_corrupt_optional_structural_payload(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    event = journal.append("test", status="ok", structural_fingerprint={"safe": True})
    with journal._connect() as db:  # corruption simulation at persistence boundary
        db.execute(
            "UPDATE events SET structural_fingerprint_json='not-json' WHERE event_id=?",
            (event.event_id,),
        )
    restored = journal.recent(limit=1)[0]
    assert restored.structural_fingerprint is None
