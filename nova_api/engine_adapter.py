"""Lazy, bounded adapter around Nova's existing conversational entry point."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from threading import Event
from typing import Any, Protocol

from .schemas import GenerationErrorCode


class EngineUnavailableError(RuntimeError):
    def __init__(self, message: str, *, category: GenerationErrorCode = GenerationErrorCode.PROVIDER_UNAVAILABLE) -> None:
        super().__init__(message)
        self.category = category


class EngineBusyError(RuntimeError):
    pass


def _provider_failure_category(error: Exception) -> str | None:
    """Project router diagnostics onto a small, secret-free API category."""
    kind = str(getattr(error, "kind", "") or "")
    provider = str(getattr(error, "provider", "") or "").split(":", 1)[0].casefold()
    details = getattr(error, "details", {})
    attempts = details.get("attempt_history", []) if isinstance(details, dict) else []
    if kind in {"timeout", "deadline_exhausted"}:
        return "omniroute_timeout" if provider == "omniroute" else "timeout"
    if kind == "invalid_request":
        return "provider_invalid_request"
    if kind == "routes_failed" and isinstance(attempts, list):
        failed = [item for item in attempts if isinstance(item, dict) and item.get("result") == "error"]
        if failed and all(str(item.get("reason", "")) in {"timeout", "deadline_exhausted"} for item in failed):
            if any(str(item.get("provider", "")).startswith("omniroute:") for item in failed):
                return "omniroute_timeout"
            return "timeout"
        if any(str(item.get("reason", "")) == "invalid_request" for item in failed):
            return "provider_invalid_request"
    return None


class ConversationEngine(Protocol):
    def respond(self, content: str, history: list[dict[str, str]], mode: str) -> str: ...


class _DisabledSystemActions:
    pending_system_action = None

    def request(self, *_args, **_kwargs):
        return {"success": False, "message": "Les actions système ne sont pas disponibles dans cette interface."}

    def handle_confirmation(self, *_args, **_kwargs):
        return {"success": False, "message": "Aucune action système n’est disponible."}


class _DisabledActionPlanner:
    waiting_confirmation = False
    pending_step = None

    def start(self, *_args, **_kwargs):
        return {"success": False, "message": "Les plans d’actions ne sont pas disponibles dans cette interface."}

    def handle_confirmation(self, *_args, **_kwargs):
        return {"success": False, "message": "Aucun plan d’action n’est disponible."}


class NovaEngineAdapter:
    """Creates Nova only on first use and serializes access to its mutable history."""

    def __init__(self, history_limit: int = 20, *, tool_definitions: list[dict[str, Any]] | None = None) -> None:
        self._history_limit = history_limit
        self._controlled_tool_definitions = list(tool_definitions or [])
        self._controlled_tools_by_name = {
            item.get("function", {}).get("name"): item for item in self._controlled_tool_definitions
        }
        self._manager = None
        self._lock = threading.Lock()

    def _get_manager(self):
        if self._manager is None:
            from conversation_manager import ConversationManager

            actions = _DisabledSystemActions()
            self._manager = ConversationManager(
                memory_search=lambda *_args, **_kwargs: [],
                memory_save=lambda *_args, **_kwargs: False,
                available_tools={"web-interface-tools-disabled": None},
                tool_definitions=self._controlled_tool_definitions,
                system_action_controller=actions,
                action_planner=_DisabledActionPlanner(),
            )
        return self._manager

    def agent_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                   timeout_seconds: float) -> dict[str, Any]:
        """Perform one model turn; execution remains exclusively in AgentLoop."""
        names = [item.get("function", {}).get("name") for item in tools]
        if (len(names) != len(set(names)) or any(
                not isinstance(name, str) or self._controlled_tools_by_name.get(name) != item
                for name, item in zip(names, tools))):
            raise EngineUnavailableError("Untrusted tool definitions")
        if not self._lock.acquire(blocking=False):
            raise EngineBusyError("Nova engine busy")
        try:
            manager = self._get_manager()
            try:
                response = manager._chat(
                    messages=messages, task_type="chat", tools=tools, think=False,
                    required_capabilities={"tools"},
                    timeout_seconds=timeout_seconds,
                )
                message = manager._assistant_message(response)
            except EngineUnavailableError:
                raise
            except (TypeError, ValueError) as error:
                raise EngineUnavailableError(
                    "Invalid tool response", category="tool_protocol_error",
                ) from error
            except Exception as error:
                details = getattr(error, "details", {})
                kind = getattr(error, "kind", "")
                required = details.get("required_capabilities", []) if isinstance(details, dict) else []
                if kind == "no_capable_provider" and "tools" in required:
                    raise EngineUnavailableError(
                        "No tool-capable provider", category="no_tool_capable_provider",
                    ) from error
                category = _provider_failure_category(error)
                if category is not None:
                    raise EngineUnavailableError("Provider request failed", category=category) from error
                raise EngineUnavailableError(
                    "Tool-capable provider unavailable", category="provider_unavailable",
                ) from error
            return {"message": message, "_meta": (response.get("_meta", {}) if isinstance(response, dict) else {}),
                    "usage": (response.get("usage", {}) if isinstance(response, dict) else {})}
        finally:
            self._lock.release()

    def respond(self, content: str, history: list[dict[str, str]], mode: str) -> str:
        del mode  # Session preference only; it grants no capability in MVP-1.
        if not self._lock.acquire(blocking=False):
            raise EngineBusyError("Nova engine busy")
        try:
            manager = self._get_manager()
            manager.conversation = [dict(item) for item in history[-self._history_limit :]]
            answer = manager.handle(content)
        finally:
            self._lock.release()
        if answer is None or not str(answer).strip():
            raise EngineUnavailableError("Nova engine unavailable")
        return str(answer).strip()

    def stream(
        self,
        content: str,
        history: list[dict[str, str]],
        mode: str,
        cancelled: Event,
    ) -> Iterator[str]:
        """Expose a delta contract while the historical engine remains synchronous."""
        answer = self.respond(content, history, mode)
        if not cancelled.is_set():
            yield answer
