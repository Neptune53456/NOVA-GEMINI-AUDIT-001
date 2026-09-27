"""Deterministic, network-free drivers for V1 hardening benchmarks.

This module deliberately stays outside the production composition root.  It may
seed historical turns, but always drives the final user turn through
``ConversationService`` so routing, state changes and journal behavior remain
the production behavior under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
from typing import Any, Literal
from uuid import uuid4

from .conversation_service import ConversationService
from .journal import EventJournal, JournalEvent
from .schemas import ConversationMessage

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ConversationTurn:
    role: Role
    content: str


@dataclass(frozen=True)
class ScriptedReply:
    """One explicit provider reply, optionally expressed as a tool-call message."""

    content: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()

    def message(self) -> dict[str, Any]:
        return {"role": "assistant", "content": self.content, "tool_calls": list(self.tool_calls)}


@dataclass(frozen=True)
class ProviderCall:
    method: str
    history_roles: tuple[str, ...]
    tool_names: tuple[str, ...] = ()


class ScriptedProvider:
    """FIFO provider double with explicit capabilities and zero network access."""

    def __init__(self, replies: Sequence[ScriptedReply | str], *,
                 capabilities: Sequence[str] = ("chat", "tools")) -> None:
        self.capabilities = frozenset(capabilities)
        self._replies = [reply if isinstance(reply, ScriptedReply) else ScriptedReply(reply)
                         for reply in replies]
        self.calls: list[ProviderCall] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _next(self) -> ScriptedReply:
        if not self._replies:
            raise RuntimeError("scripted_provider_exhausted")
        return self._replies.pop(0)

    def respond(self, content: str, history: list[dict[str, str]], mode: str) -> str:
        del content, mode
        if "chat" not in self.capabilities:
            raise RuntimeError("scripted_provider_chat_unsupported")
        self.calls.append(ProviderCall("respond", tuple(str(item.get("role")) for item in history)))
        reply = self._next()
        if reply.tool_calls:
            raise RuntimeError("scripted_provider_tool_reply_on_chat_path")
        return reply.content

    def stream(self, content: str, history: list[dict[str, str]], mode: str,
               cancelled: Event):
        reply = self.respond(content, history, mode)
        if not cancelled.is_set():
            yield reply

    def agent_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                   timeout_seconds: float) -> dict[str, Any]:
        del timeout_seconds
        if "tools" not in self.capabilities:
            raise RuntimeError("scripted_provider_tools_unsupported")
        names = tuple(str(item.get("function", {}).get("name")) for item in tools)
        self.calls.append(ProviderCall(
            "agent_turn", tuple(str(item.get("role")) for item in messages), names,
        ))
        return {"message": self._next().message()}


@dataclass(frozen=True)
class ConversationRun:
    scenario_id: str
    conversation_id: str
    generation_id: str
    assistant_output: str | None
    terminal_event: str
    events: tuple[dict[str, Any], ...]
    journal_events: tuple[JournalEvent, ...]
    goal_ids: tuple[str, ...]
    action_ids: tuple[str, ...]
    provider_calls: int | None


class MultiTurnConversationRunner:
    """Run one isolated conversation and remove its in-memory session afterwards."""

    def __init__(self, service_factory: Callable[[], ConversationService], *,
                 journal: EventJournal | None = None) -> None:
        self._service_factory = service_factory
        self._journal = journal

    async def arun(self, scenario_id: str, turns: Sequence[ConversationTurn | dict[str, str]]) -> ConversationRun:
        normalized = [self._turn(item) for item in turns]
        if not scenario_id.strip() or not normalized or normalized[-1].role != "user":
            raise ValueError("scenario requires an id and a final user turn")
        service = self._service_factory()
        summary = service.create()
        conversation_id = summary.conversation_id
        self._seed_history(service, conversation_id, normalized[:-1])
        generation_id = service.start_generation(conversation_id, normalized[-1].content)
        events: list[dict[str, Any]] = []
        try:
            async for event in service.stream(generation_id):
                events.append(event)
            detail = service.get(conversation_id)
            assistant = next((item.content for item in reversed(detail.messages)
                              if item.role == "assistant"), None)
            journal_events = tuple(self._journal.for_conversation(conversation_id)) if self._journal else ()
            goal_ids = tuple(dict.fromkeys(
                str(item["goal_id"]) for item in events if item.get("goal_id")
            ))
            action_ids = tuple(dict.fromkeys(
                item.action_id for item in journal_events if item.action_id
            ))
            engine = getattr(service, "_engine", None)
            count = getattr(engine, "call_count", None)
            return ConversationRun(
                scenario_id.strip(), conversation_id, generation_id, assistant,
                events[-1]["event"] if events else "generation.missing", tuple(events),
                journal_events, goal_ids, action_ids, count if isinstance(count, int) else None,
            )
        finally:
            try:
                service.delete(conversation_id)
            except Exception:
                pass

    def run(self, scenario_id: str, turns: Sequence[ConversationTurn | dict[str, str]]) -> ConversationRun:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(scenario_id, turns))
        raise RuntimeError("use arun() inside an active event loop")

    @staticmethod
    def _turn(value: ConversationTurn | dict[str, str]) -> ConversationTurn:
        turn = value if isinstance(value, ConversationTurn) else ConversationTurn(
            role=value.get("role", ""), content=value.get("content", ""),  # type: ignore[arg-type]
        )
        if turn.role not in {"user", "assistant"} or not turn.content.strip():
            raise ValueError("invalid conversation turn")
        return ConversationTurn(turn.role, turn.content.strip())

    @staticmethod
    def _seed_history(service: ConversationService, conversation_id: str,
                      turns: Sequence[ConversationTurn]) -> None:
        """Benchmark adapter: seed prior turns without executing or scoring them."""
        now = datetime.now(timezone.utc)
        messages = [ConversationMessage(
            message_id=f"benchmark-{uuid4().hex}", role=turn.role,
            content=turn.content, created_at=now,
        ) for turn in turns]
        with service._lock:  # Deliberate benchmark-only adapter boundary.
            service._sessions[conversation_id]["messages"] = messages
