"""Bounded model/capability loop with opaque, single-use write confirmations."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from threading import Event, RLock
from time import monotonic
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

from .capabilities import CapabilityRegistry
from .context_builder import ContextBuilder
from .engine_adapter import EngineUnavailableError
from .journal import EventJournal
from .model_usage import ModelUsageStore
from .memory_store import MemoryRejected, explicit_memory_content
from .project_brain import ProjectBrain
from .schemas import GenerationErrorCode

EXPOSED_CAPABILITIES = frozenset({
    "git.status", "project.basic_info", "filesystem.list",
    "filesystem.read", "filesystem.write",
    "computer.observe", "computer.windows", "computer.active_window",
    "computer.window.focus", "computer.window.minimize", "computer.window.restore",
    "computer.ui.inspect", "computer.ui.elements", "computer.ui.invoke", "computer.ui.focus",
    "computer.ui.set_value", "computer.ui.toggle", "computer.ui.select",
    "computer.visual.displays", "computer.visual.capture", "computer.visual.inspect",
    "computer.visual.ocr", "computer.visual.analyze", "computer.perception.ground",
})
PROJECT_CAPABILITIES = frozenset({"git.status", "project.basic_info"})
FILESYSTEM_CAPABILITIES = frozenset({"filesystem.list", "filesystem.read", "filesystem.write"})
COMPUTER_OBSERVATION_CAPABILITIES = frozenset({
    "computer.observe", "computer.windows", "computer.active_window",
})
COMPUTER_WINDOW_CAPABILITIES = frozenset({
    "computer.window.focus", "computer.window.minimize", "computer.window.restore",
})
COMPUTER_UI_CAPABILITIES = frozenset({
    "computer.ui.inspect", "computer.ui.elements", "computer.ui.invoke", "computer.ui.focus",
    "computer.ui.set_value", "computer.ui.toggle", "computer.ui.select",
})
DETERMINISTICALLY_VERIFIED_MUTATIONS = frozenset({
    "computer.ui.set_value", "computer.ui.toggle", "computer.ui.select",
})
COMPUTER_VISUAL_CAPABILITIES = frozenset({
    "computer.visual.displays", "computer.visual.capture", "computer.visual.inspect",
    "computer.visual.ocr", "computer.visual.analyze", "computer.perception.ground",
})
DISCOVERY_CAPABILITIES = frozenset({
    "git.status", "project.basic_info", "filesystem.list", "filesystem.read",
    "computer.observe", "computer.windows", "computer.active_window",
    "computer.ui.inspect", "computer.ui.elements",
    "computer.visual.displays", "computer.visual.capture", "computer.visual.inspect",
    "computer.visual.ocr", "computer.visual.analyze", "computer.perception.ground",
})
MAX_MODEL_TURNS = 7
MAX_CAPABILITY_EXECUTIONS = 3
MAX_DISCOVERY_EXECUTIONS = 3
AGENT_TIMEOUT_SECONDS = 45.0
CONFIRMATION_TTL_SECONDS = 300.0
MAX_OBSERVATION_CONTENT_CHARS = 16_000
MAX_OBSERVATION_ENTRIES = 100
MAX_DIFF_SUMMARY_CHARS = 1_000
MAX_MODEL_OBSERVATION_ITEMS = 40
MAX_MODEL_OBSERVATION_STRING_CHARS = 2_000


class ToolCallingEngine(Protocol):
    def agent_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                   timeout_seconds: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ConfirmationRequest:
    token: str
    capability_id: str
    arguments: dict[str, Any]
    path: str
    effect: str
    expires_at: float
    conversation_id: str
    generation_id: str
    messages: list[dict[str, Any]]
    model_turns: int
    action_count: int
    discovery_count: int

    def public(self) -> dict[str, Any]:
        return {"token": self.token, "capability_id": self.capability_id,
                "path": self.path, "effect": self.effect,
                "expires_in_seconds": max(0, int(self.expires_at - monotonic()))}


@dataclass(frozen=True)
class AgentOutcome:
    status: Literal["success", "awaiting-confirmation", "error", "cancelled"]
    answer: str | None = None
    confirmation: ConfirmationRequest | None = None
    error_category: GenerationErrorCode | None = None
    completion_state: Literal["completed_verified", "completed_unverified"] | None = None


class ConfirmationStore:
    def __init__(self, *, ttl: float = CONFIRMATION_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._secret = uuid4().bytes + uuid4().bytes
        self._items: dict[str, ConfirmationRequest] = {}
        self._lock = RLock()

    @staticmethod
    def _normalized(capability_id: str, arguments: dict[str, Any]) -> bytes:
        return json.dumps([capability_id, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()

    def create(self, capability_id: str, arguments: dict[str, Any], *, conversation_id: str,
               generation_id: str, messages: list[dict[str, Any]], model_turns: int,
               action_count: int, discovery_count: int = 0) -> ConfirmationRequest:
        nonce = uuid4().hex
        signature = hmac.new(self._secret, nonce.encode() + self._normalized(capability_id, arguments), hashlib.sha256).hexdigest()
        token = f"{nonce}.{signature}"
        request = ConfirmationRequest(token, capability_id, dict(arguments), str(arguments.get("path", "")),
                                      "Écrire le contenu proposé dans ce fichier.", monotonic() + self._ttl,
                                      conversation_id, generation_id, list(messages), model_turns, action_count,
                                      discovery_count)
        with self._lock: self._items[token] = request
        return request

    def consume(self, token: str, *, conversation_id: str) -> ConfirmationRequest:
        with self._lock: request = self._items.get(token)
        if request is None or request.conversation_id != conversation_id or request.expires_at < monotonic():
            raise ValueError("invalid_confirmation")
        expected = hmac.new(self._secret, token.split(".", 1)[0].encode() + self._normalized(request.capability_id, request.arguments), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(token.split(".", 1)[-1], expected): raise ValueError("invalid_confirmation")
        with self._lock: self._items.pop(token, None)
        return request

    def refuse(self, token: str, *, conversation_id: str) -> ConfirmationRequest:
        return self.consume(token, conversation_id=conversation_id)


def select_capabilities(content: str) -> frozenset[str]:
    """Select a registered-tool subset deterministically; this never invokes a model."""
    normalized = "".join(
        character for character in unicodedata.normalize("NFKD", content.casefold())
        if not unicodedata.combining(character)
    )
    project_intent = any(word in normalized for word in (
        "fichier", "dossier", "repertoire", "projet", "code", "git", "file", "folder", "directory", "repository",
    ))
    base = set(PROJECT_CAPABILITIES)
    if project_intent:
        base.update(FILESYSTEM_CAPABILITIES)
    computer_intent = any(word in normalized for word in (
        "ordinateur", "computer", "pc", "bureau", "desktop", "ecran", "screen", "fenetre", "window",
        "application", " app ", "bloc-notes", "notepad",
    ))
    window_action = any(word in normalized for word in (
        "premier plan", "foreground", "focus", "reduis", "minimis", "restore", "restaur",
    ))
    ui_intent = any(word in normalized for word in (
        "bouton", "button", "onglet", "tab", "champ", "field", "element", "controle", "control",
        "cliqu", "click", "saisi", "type", "ecris", "write", "toggle", "select",
    ))
    visual_intent = any(phrase in normalized for phrase in (
        "que vois", "qu'est-ce qui est affiche", "regarde mon ecran", "regarde l'ecran",
        "regarde la fenetre", "look at", "what do you see", "visuellement", "visually", "screenshot",
    ))
    active_window_intent = any(phrase in normalized for phrase in (
        "fenetre active", "fenetre au premier plan", "active window", "foreground window",
        "actuellement active", "currently active",
    ))
    named_window_visual = visual_intent and any(word in normalized for word in ("fenetre", "window")) \
        and not active_window_intent
    display_selection_intent = visual_intent and any(word in normalized for word in (
        "moniteur", "monitor", "deuxieme ecran", "second screen", "display topology", "topologie",
    ))
    if (computer_intent or window_action or ui_intent or visual_intent) and not project_intent:
        base.clear()
    if (computer_intent or window_action or ui_intent) and not visual_intent:
        base.update(COMPUTER_OBSERVATION_CAPABILITIES)
    if (window_action or ui_intent) and not visual_intent:
        base.update(COMPUTER_WINDOW_CAPABILITIES)
    if ui_intent:
        base.update(COMPUTER_UI_CAPABILITIES)
    if visual_intent:
        base.update({"computer.visual.capture", "computer.visual.analyze"})
        if ui_intent:
            base.add("computer.perception.ground")
        if named_window_visual: base.add("computer.windows")
        if active_window_intent: base.add("computer.active_window")
        if display_selection_intent: base.add("computer.visual.displays")
    return frozenset(base)


def tool_definitions(registry: CapabilityRegistry,
                     capability_ids: frozenset[str] | None = None) -> list[dict[str, Any]]:
    definitions = []
    legacy_schemas = {
        "git.status": {"type": "object", "properties": {}, "additionalProperties": False},
        "project.basic_info": {"type": "object", "properties": {}, "additionalProperties": False},
        "filesystem.list": {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
        "filesystem.read": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
        "filesystem.write": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False},
    }
    for capability_id in sorted(capability_ids or EXPOSED_CAPABILITIES):
        capability = registry.lookup(capability_id)
        definitions.append({"type": "function", "function": {"name": capability.id,
                            "description": capability.description,
                            "parameters": capability.argument_schema or legacy_schemas[capability.id]}})
    return definitions


def _discovery_evidence_fingerprint(capability_id: str, result: object) -> str:
    """Fingerprint stable evidence, not the repeated request or volatile timestamps."""
    volatile = {"observation_id", "ui_observation_id", "observed_at"}

    def stable(value: object) -> object:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in sorted(value.items()) if key not in volatile}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return json.dumps([capability_id, stable(result)], sort_keys=True, separators=(",", ":"), default=str)


def compact_observation(capability_id: str, result: Any) -> dict[str, Any]:
    base = {"capability_id": capability_id, "status": result.status, "verified": result.verified,
            "action_id": result.action_id, "error_category": result.error_category}
    value = result.result or {}
    if result.status != "success":
        if capability_id == "computer.visual.analyze" and result.error_category == "VISION_PROVIDER_UNAVAILABLE":
            base.update({
                "visual_analysis_attempted": True,
                "provider_fallback_exhausted": True,
                "retry_same_image_ref": False,
            })
        return base
    if capability_id == "filesystem.read":
        content = str(value.get("content", "")); base.update(path=value.get("path"), size=value.get("size"),
            content=content[:MAX_OBSERVATION_CONTENT_CHARS], truncated=len(content) > MAX_OBSERVATION_CONTENT_CHARS)
    elif capability_id == "filesystem.list":
        entries = value.get("entries", []); base.update(path=value.get("path"), entries=entries[:MAX_OBSERVATION_ENTRIES],
            truncated=bool(value.get("truncated")) or len(entries) > MAX_OBSERVATION_ENTRIES)
    elif capability_id == "filesystem.write":
        diff = str(value.get("diff", "")); base.update(path=value.get("path"), transaction_id=value.get("transaction_id"),
            transaction_status=value.get("status"), diff_summary=diff[:MAX_DIFF_SUMMARY_CHARS],
            truncated=bool(value.get("diff_truncated")) or len(diff) > MAX_DIFF_SUMMARY_CHARS)
    else:
        base.update({
            key: _bounded_observation_value(item)
            for key, item in value.items() if key not in {"content", "diff"}
        })
    return base


def _bounded_observation_value(value: Any, *, depth: int = 0) -> Any:
    """Bound model-facing evidence while retaining identifiers and state."""
    if depth >= 4:
        return "[nested value omitted]"
    if isinstance(value, str):
        return value[:MAX_MODEL_OBSERVATION_STRING_CHARS]
    if isinstance(value, list):
        return [
            _bounded_observation_value(item, depth=depth + 1)
            for item in value[:MAX_MODEL_OBSERVATION_ITEMS]
        ]
    if isinstance(value, dict):
        return {
            str(key): _bounded_observation_value(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_MODEL_OBSERVATION_ITEMS]
        }
    return value


def canonical_assistant_message(message: Any) -> dict[str, Any]:
    """Normalize provider tool calls before they become cross-turn state."""
    if not isinstance(message, dict):
        raise ValueError("invalid_assistant_message")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, (list, tuple)):
        raise ValueError("invalid_tool_calls")
    normalized_calls = []
    for raw_call in calls:
        if not isinstance(raw_call, dict) or not isinstance(raw_call.get("function"), dict):
            raise ValueError("invalid_tool_call")
        function = raw_call["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invalid_tool_name")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError as error:
                raise ValueError("invalid_tool_arguments") from error
        else:
            parsed_arguments = arguments
        if not isinstance(parsed_arguments, dict):
            raise ValueError("invalid_tool_arguments")
        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = "call_" + uuid4().hex
        normalized_calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(parsed_arguments, ensure_ascii=False, separators=(",", ":")),
            },
        })
    return {
        "role": "assistant",
        "content": (None if normalized_calls and not message.get("content")
                    else str(message.get("content") or "")),
        "tool_calls": normalized_calls,
    }


def tool_result_message(call_id: str, capability_id: str, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": capability_id,
        "content": json.dumps(content, ensure_ascii=False),
    }


class AgentLoop:
    def __init__(self, engine: ToolCallingEngine, registry: CapabilityRegistry, journal: EventJournal,
                 confirmations: ConfirmationStore | None = None, *, max_model_turns: int = MAX_MODEL_TURNS,
                 max_actions: int = MAX_CAPABILITY_EXECUTIONS, timeout: float = AGENT_TIMEOUT_SECONDS,
                 project_brain: ProjectBrain | None = None,
                 context_builder: ContextBuilder | None = None,
                 model_usage: ModelUsageStore | None = None,
                 max_discoveries: int = MAX_DISCOVERY_EXECUTIONS) -> None:
        self.engine, self.registry, self.journal = engine, registry, journal
        self.confirmations = confirmations or ConfirmationStore()
        self.max_model_turns, self.max_actions, self.timeout = max_model_turns, max_actions, timeout
        self.max_discoveries = max_discoveries
        self.tools = tool_definitions(registry)
        self.project_brain = project_brain
        self.context_builder = context_builder
        self.model_usage = model_usage or ModelUsageStore.for_journal(journal)

    def run(self, content: str, history: list[dict[str, str]], mode: str, cancelled: Event, *,
            conversation_id: str, generation_id: str, notify: Callable[[str, dict[str, Any]], None]) -> AgentOutcome:
        del mode
        messages: list[dict[str, Any]] = [{"role": "system", "content": (
            "Tu es Nova. Utilise seulement les capabilities fournies quand une observation locale est "
            "nécessaire. N'invente jamais un résultat d'outil. Pour une tâche UI, converge vers l'action "
            "sémantique demandée: computer.windows fournit déjà state et active; ne restaure qu'une "
            "fenêtre minimized. computer.ui.inspect fournit editable, read_only et supported_actions. "
            "computer.ui.set_value ne requiert pas que la fenêtre soit au premier plan; utilise focus "
            "seulement comme récupération lorsqu'un contrôle l'exige. Pour une demande visuelle visant "
            "une fenêtre nommée: computer.windows, puis computer.visual.capture avec target_type=window "
            "et son window_ref, puis computer.visual.analyze. N'observe ni la fenêtre active ni les "
            "moniteurs sauf si la demande porte explicitement sur eux. Pour l'écran principal, capture "
            "directement target_type=primary_display."
        )}]
        if self.context_builder is not None:
            try:
                explicit = explicit_memory_content(content)
                if explicit:
                    memory_type = "PREFERENCE" if any(word in explicit.casefold() for word in ("préfère", "prefere", "prefer")) else "DECISION"
                    self.context_builder.memory.remember(memory_type=memory_type, source_type="explicit_user_statement",
                        provenance="USER_STATED", subject=explicit[:160], content=explicit, importance=8,
                        source_reference=conversation_id, tags=["explicit"])
                    self.journal.append("memory.remembered", generation_id=generation_id,
                                        conversation_id=conversation_id, status="success")
                package = self.context_builder.build(content, conversation_id=conversation_id)
                if package.content:
                    messages.append({"role": "system", "content": (
                        "Contexte de confiance ciblé et borné. Traite MODEL_INFERRED comme une hypothèse; "
                        "les états récents vérifiés priment sur les anciens.\n" + package.content)})
                if package.diagnostics:
                    self.journal.append("context.assembled", generation_id=generation_id,
                                        conversation_id=conversation_id, status="success", duration_ms=0)
            except MemoryRejected:
                self.journal.append("memory.rejected", generation_id=generation_id,
                                    conversation_id=conversation_id, status="error", error_category="memory_policy_rejected")
            except (OSError, ValueError, sqlite3.Error):
                self.journal.append("context.unavailable", generation_id=generation_id,
                                    conversation_id=conversation_id, status="error", error_category="context_unavailable")
        elif self.project_brain is not None:
            try:
                if self.project_brain.should_target(content):
                    if self.project_brain.status()["status"] != "ready": self.project_brain.refresh()
                    target = self.project_brain.target(content)
                    if target.context:
                        messages.append({"role": "system", "content": "Contexte projet ciblé (lecture seule, borné):" + target.context})
                    self.journal.append("project_brain.targeted", generation_id=generation_id, conversation_id=conversation_id,
                                        status="success", duration_ms=0)
            except (OSError, ValueError, sqlite3.Error):
                self.journal.append("project_brain.unavailable", generation_id=generation_id, conversation_id=conversation_id,
                                    status="error", error_category="project_brain_unavailable")
        messages.extend(history); messages.append({"role": "user", "content": content})
        self.journal.append("agent.started", generation_id=generation_id, conversation_id=conversation_id, status="running")
        selected = select_capabilities(content)
        return self._continue(messages, cancelled, conversation_id, generation_id, notify, 0, 0, 0,
                              tools=tool_definitions(self.registry, selected), allowed=selected)

    def resume(self, request: ConfirmationRequest, cancelled: Event,
               notify: Callable[[str, dict[str, Any]], None]) -> AgentOutcome:
        return self._continue(request.messages, cancelled, request.conversation_id, request.generation_id,
                              notify, request.model_turns, request.action_count, request.discovery_count,
                              confirmed=request)

    def _continue(self, messages: list[dict[str, Any]], cancelled: Event, conversation_id: str,
                  generation_id: str, notify: Callable[[str, dict[str, Any]], None], model_turns: int,
                  action_count: int, discovery_count: int,
                  confirmed: ConfirmationRequest | None = None,
                  tools: list[dict[str, Any]] | None = None,
                  allowed: frozenset[str] | None = None) -> AgentOutcome:
        started = monotonic()
        active_tools = tools or self.tools
        allowed_capabilities = allowed or EXPOSED_CAPABILITIES
        seen_discoveries: set[str] = set()
        if confirmed is not None:
            if cancelled.is_set(): return AgentOutcome("cancelled")
            notify("acting", {"capability_id": confirmed.capability_id})
            result = self.registry.execute(confirmed.capability_id, confirmed.arguments, confirmed=True)
            action_count += 1
            self.journal.append("agent.tool_completed", generation_id=generation_id, action_id=result.action_id,
                transaction_id=(result.result or {}).get("transaction_id"), conversation_id=conversation_id,
                capability_id=confirmed.capability_id, status=result.status, duration_ms=result.duration_ms,
                error_category=result.error_category)
            call_id = messages[-1]["tool_calls"][-1]["id"]
            messages.append(tool_result_message(
                call_id, confirmed.capability_id, compact_observation(confirmed.capability_id, result),
            ))
        while (model_turns < self.max_model_turns and action_count <= self.max_actions and
               discovery_count <= self.max_discoveries and monotonic() - started < self.timeout):
            if cancelled.is_set(): return AgentOutcome("cancelled")
            remaining = self.timeout - (monotonic() - started)
            try:
                response = self.engine.agent_turn(messages, active_tools, timeout_seconds=remaining)
                usage_records = self.model_usage.record_response(
                    response, purpose="agent_turn", conversation_id=conversation_id,
                )
                for usage in usage_records:
                    self.journal.append(
                        "model.attempt", generation_id=generation_id, conversation_id=conversation_id,
                        status="success" if usage.success else "error", duration_ms=usage.elapsed_ms,
                        model=usage.model, provider=usage.provider, input_tokens=usage.tokens_input,
                        output_tokens=usage.tokens_output, error_category=usage.failure_category,
                        structural_fingerprint={
                            "usage_source": usage.usage_source,
                            "authoritative_usage": usage.authoritative_usage,
                            "fallback_from": usage.fallback_from,
                            "fallback_reason": usage.fallback_reason,
                            "purpose": usage.purpose,
                        },
                    )
            except EngineUnavailableError as error:
                self.model_usage.record_failure(
                    purpose="agent_turn", error=error, conversation_id=conversation_id,
                )
                category = getattr(error, "category", "provider_unavailable")
                self.journal.append("agent.error", generation_id=generation_id,
                                    conversation_id=conversation_id, status="error",
                                    error_category=category)
                return AgentOutcome("error", error_category=category)
            except (TypeError, ValueError):
                self.journal.append("agent.error", generation_id=generation_id,
                                    conversation_id=conversation_id, status="error",
                                    error_category="tool_protocol_error")
                return AgentOutcome("error", error_category="tool_protocol_error")
            model_turns += 1
            try:
                assistant = canonical_assistant_message(response.get("message", response))
            except (TypeError, ValueError):
                self.journal.append("agent.error", generation_id=generation_id,
                                    conversation_id=conversation_id, status="error",
                                    error_category="tool_protocol_error")
                return AgentOutcome("error", error_category="tool_protocol_error")
            calls = assistant.get("tool_calls") or []
            if not calls:
                answer = str(assistant.get("content") or "").strip()
                if not answer: return AgentOutcome("error", error_category="empty_response")
                self.journal.append("agent.completed", generation_id=generation_id, conversation_id=conversation_id, status="success")
                return AgentOutcome("success", answer=answer)
            messages.append(assistant)
            for call in calls:
                if cancelled.is_set(): return AgentOutcome("cancelled")
                function = call["function"]; capability_id = function["name"]
                arguments = json.loads(function["arguments"])
                if capability_id not in allowed_capabilities or not isinstance(arguments, dict):
                    return AgentOutcome("error", error_category="capability_not_allowed")
                try: self.registry.validate_arguments(capability_id, arguments)
                except (ValueError, KeyError): return AgentOutcome("error", error_category="invalid_arguments")
                self.journal.append("agent.tool_requested", generation_id=generation_id, conversation_id=conversation_id,
                                    capability_id=capability_id, status="requested")
                is_discovery = capability_id in DISCOVERY_CAPABILITIES
                if is_discovery:
                    if discovery_count >= self.max_discoveries:
                        return AgentOutcome("error", error_category="discovery_budget_exceeded")
                    discovery_count += 1
                elif action_count >= self.max_actions:
                    return AgentOutcome("error", error_category="action_budget_exceeded")
                if capability_id == "filesystem.write":
                    pending = self.confirmations.create(capability_id, arguments, conversation_id=conversation_id,
                        generation_id=generation_id, messages=messages, model_turns=model_turns,
                        action_count=action_count, discovery_count=discovery_count)
                    self.journal.append("agent.awaiting_confirmation", generation_id=generation_id, conversation_id=conversation_id,
                                        capability_id=capability_id, status="awaiting-confirmation")
                    return AgentOutcome("awaiting-confirmation", confirmation=pending)
                notify("acting", {"capability_id": capability_id})
                result = self.registry.execute(capability_id, arguments)
                if not is_discovery: action_count += 1
                if is_discovery and result.status == "success":
                    fingerprint = _discovery_evidence_fingerprint(capability_id, result.result)
                    if fingerprint in seen_discoveries:
                        self.journal.append("agent.tool_completed", generation_id=generation_id,
                            conversation_id=conversation_id, capability_id=capability_id, status="error",
                            duration_ms=result.duration_ms, error_category="repeated_observation")
                        return AgentOutcome("error", error_category="repeated_observation")
                    seen_discoveries.add(fingerprint)
                self.journal.append("agent.tool_completed", generation_id=generation_id, action_id=result.action_id,
                    conversation_id=conversation_id, capability_id=capability_id, status=result.status,
                    duration_ms=result.duration_ms, error_category=result.error_category)
                messages.append(tool_result_message(
                    call["id"], capability_id, compact_observation(capability_id, result),
                ))
                if capability_id in DETERMINISTICALLY_VERIFIED_MUTATIONS and result.status == "success" and result.verified:
                    answer = _verified_action_answer(capability_id)
                    self.journal.append("agent.completed", generation_id=generation_id,
                                        conversation_id=conversation_id, status="success")
                    return AgentOutcome("success", answer=answer, completion_state="completed_verified")
                if (capability_id == "computer.visual.analyze" and result.status != "success"
                        and result.error_category == "VISION_PROVIDER_UNAVAILABLE"):
                    answer = (
                        "L’analyse visuelle a bien été tentée, mais les fournisseurs vision disponibles "
                        "sont temporairement indisponibles. Je ne peux pas décrire fidèlement cette image pour le moment."
                    )
                    self.journal.append("agent.completed", generation_id=generation_id,
                                        conversation_id=conversation_id, status="success")
                    return AgentOutcome("success", answer=answer)
        category = "timeout" if monotonic() - started >= self.timeout else "model_turn_budget_exceeded"
        self.journal.append("agent.error", generation_id=generation_id, conversation_id=conversation_id, status="error", error_category=category)
        return AgentOutcome("error", error_category=category)


def _verified_action_answer(capability_id: str) -> str:
    if capability_id == "computer.ui.set_value":
        return "Texte saisi et vérifié dans le contrôle ciblé."
    return "Action exécutée et vérifiée sur le contrôle ciblé."
