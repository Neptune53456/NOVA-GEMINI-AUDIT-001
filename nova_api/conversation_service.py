"""Bounded, in-memory conversation sessions with cancellable generation streams."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime, timezone
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from .agent_loop import AgentLoop, AgentOutcome
from .autonomy import GoalRunner
from .conversation_store import ConversationStore
from .engine_adapter import ConversationEngine, EngineBusyError, EngineUnavailableError
from .journal import EventJournal
from .initial_goal_planner import InitialPlanInvalid, is_goal_intent, is_resume_intent
from .missions import MissionManager, MissionPlanError, plan_conversational_mission
from .schemas import (ConversationDetail, ConversationMessage, ConversationSummary, GenerationErrorCode,
                      GenerationEvent, SendMessageResponse)
from .state import ApiStateStore

MAX_MESSAGE_LENGTH = 8_000
MAX_CONVERSATIONS = 32
MAX_MESSAGES_PER_CONVERSATION = 40
ENGINE_TIMEOUT_SECONDS = 60.0
HISTORY_MESSAGES_SENT = 20


class ConversationNotFound(Exception): pass
class ConversationBusy(Exception): pass
class ConversationLimitReached(Exception): pass
class GenerationNotFound(Exception): pass
class EngineTimedOut(Exception): pass


class _MissionConfirmation:
    def __init__(self, value: dict[str, Any]) -> None:
        self.token = value["token"]


class ConversationService:
    def __init__(self, engine: ConversationEngine, state: ApiStateStore, *, max_conversations: int = MAX_CONVERSATIONS,
                 max_messages: int = MAX_MESSAGES_PER_CONVERSATION, timeout: float = ENGINE_TIMEOUT_SECONDS,
                 clock: Callable[[], datetime] | None = None, id_factory: Callable[[], str] | None = None,
                 generation_id_factory: Callable[[], str] | None = None,
                 journal: EventJournal | None = None, agent_loop: AgentLoop | None = None,
                 mission_manager: MissionManager | None = None,
                 goal_runner: GoalRunner | None = None,
                 conversation_store: ConversationStore | None = None) -> None:
        self._engine, self._state = engine, state
        self._max_conversations, self._max_messages, self._timeout = max_conversations, max_messages, timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._generation_id_factory = generation_id_factory or (lambda: uuid4().hex)
        self._journal = journal
        self._agent_loop = agent_loop
        self._missions = mission_manager
        self._goals = goal_runner
        self._conversation_store = conversation_store or (ConversationStore.for_journal(journal) if journal is not None else None)
        self._sessions: OrderedDict[str, dict[str, Any]] = (
            self._conversation_store.load_all(limit=max_conversations, max_messages=max_messages)
            if self._conversation_store is not None else OrderedDict()
        )
        self._generations: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._generation_lock = Lock()
        self._pending_confirmations: dict[str, Any] = {}
        self._mission_confirmations: dict[str, str] = {}
        self._generation_missions: dict[str, str] = {}
        self._goal_confirmations: dict[str, str] = {}
        self._generation_goals: dict[str, str] = {}

    def create(self, mode: str = "supervised") -> ConversationSummary:
        with self._lock:
            if len(self._sessions) >= self._max_conversations:
                removable = next((key for key, value in self._sessions.items() if not value["busy"]), None)
                if removable is None: raise ConversationLimitReached()
                del self._sessions[removable]
            conversation_id = self._id_factory()
            now = self._clock()
            self._sessions[conversation_id] = {"created_at": now, "last_active_at": now, "status": "idle", "mode": mode, "messages": [], "busy": False, "generation_id": None, "active_goal_ids": [], "active_mission_ids": [], "trimmed_message_count": 0}
            self._persist_session(conversation_id)
            return ConversationSummary(conversation_id=conversation_id, created_at=now, status="idle", mode=mode)

    def get(self, conversation_id: str) -> ConversationDetail:
        with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None: raise ConversationNotFound()
            return ConversationDetail(conversation_id=conversation_id, created_at=session["created_at"], status=session["status"], mode=session["mode"], messages=list(session["messages"]))

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None: raise ConversationNotFound()
            if session["busy"]: raise ConversationBusy()
            del self._sessions[conversation_id]
            if self._conversation_store is not None: self._conversation_store.delete(conversation_id)

    def start_generation(self, conversation_id: str, content: str) -> str:
        with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None: raise ConversationNotFound()
            if session["busy"]: raise ConversationBusy()
            if self._max_messages < 2:
                raise ConversationLimitReached()
            if not self._generation_lock.acquire(blocking=False): raise EngineBusyError("Nova engine busy")
            generation_id = self._generation_id_factory()
            session["busy"], session["status"], session["generation_id"] = True, "thinking", generation_id
            session["last_active_at"] = self._clock()
            self._persist_session(conversation_id)
            self._generations[generation_id] = {
                "conversation_id": conversation_id, "content": content,
                "history": [{"role": message.role, "content": message.content} for message in session["messages"][-HISTORY_MESSAGES_SENT:]],
                "mode": session["mode"], "cancelled": Event(), "started": False,
                "started_at": None,
            }
        self._set_state(generation_id, "thinking", "Réflexion", "Nova prépare une réponse.", busy=True)
        return generation_id

    async def stream(self, generation_id: str) -> AsyncIterator[dict[str, Any]]:
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation is None: raise GenerationNotFound()
            if generation["started"]: raise ConversationBusy()
            generation["started"] = True
            generation["started_at"] = monotonic()
        self._journal_generation("generation.started", generation_id, "thinking")
        yield self._event("generation.started", generation_id, "thinking")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        processed = Event()
        engine_finished = Event()

        def emit(kind: str, value: Any = None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

        def run_engine() -> None:
            try:
                if self._goals is not None and is_resume_intent(generation["content"]):
                    candidates = self._goals.resumable(generation["conversation_id"])
                    if len(candidates) != 1:
                        engine_finished.set()
                        emit("goal_clarification", len(candidates))
                        return
                    goal = self._goals.run(candidates[0].goal_id)
                    with self._lock:
                        self._generation_goals[generation_id] = goal.goal_id
                        session = self._sessions.get(generation["conversation_id"])
                        if session is not None:
                            goals = session.setdefault("active_goal_ids", [])
                            if goal.goal_id not in goals: goals.append(goal.goal_id)
                            self._persist_session(generation["conversation_id"])
                    engine_finished.set(); emit("goal_result", goal); return
                if self._goals is not None and is_goal_intent(generation["content"]):
                    try:
                        goal = self._goals.create_from_objective(
                            generation["conversation_id"], generation["content"])
                    except InitialPlanInvalid as error:
                        engine_finished.set(); emit("agent_error", str(error)); return
                    with self._lock:
                        self._generation_goals[generation_id] = goal.goal_id
                        session = self._sessions.get(generation["conversation_id"])
                        if session is not None:
                            goals = session.setdefault("active_goal_ids", [])
                            if goal.goal_id not in goals: goals.append(goal.goal_id)
                            self._persist_session(generation["conversation_id"])
                    emit("goal", ("goal.created", goal))
                    goal = self._goals.run(goal.goal_id)
                    engine_finished.set(); emit("goal_result", goal); return
                if self._missions is not None:
                    try:
                        plan = plan_conversational_mission(generation["content"])
                    except MissionPlanError as error:
                        engine_finished.set(); emit("agent_error", str(error)); return
                    if plan is not None:
                        mission = self._missions.create(generation["conversation_id"], generation["content"], plan)
                        with self._lock:
                            self._generation_missions[generation_id] = mission.mission_id
                            session = self._sessions.get(generation["conversation_id"])
                            if session is not None:
                                missions = session.setdefault("active_mission_ids", [])
                                if mission.mission_id not in missions: missions.append(mission.mission_id)
                                self._persist_session(generation["conversation_id"])
                        emit("mission", ("mission.created", mission, None))
                        def notify(event: str, current: Any, capability_id: str | None) -> None:
                            emit("mission", (event, current, capability_id))
                        mission = self._missions.run(mission.mission_id, notify=notify)
                        engine_finished.set()
                        if mission.state == "awaiting_confirmation":
                            request = mission.steps[mission.current_step]["confirmation"]
                            emit("mission_waiting", (mission, request))
                        elif mission.state == "completed":
                            emit("done", "Mission terminée.")
                        elif mission.state == "cancelled": emit("cancelled")
                        else: emit("agent_error", mission.last_error_category or "mission_failed")
                        return
                if self._agent_loop is not None and callable(getattr(self._engine, "agent_turn", None)):
                    outcome = self._agent_loop.run(
                        generation["content"], generation["history"], generation["mode"], generation["cancelled"],
                        conversation_id=generation["conversation_id"], generation_id=generation_id,
                        notify=lambda state, payload: emit("state", {"status": state, **payload}),
                    )
                    engine_finished.set()
                    if outcome.status == "success" and outcome.completion_state == "completed_verified":
                        emit("verified_result", outcome)
                    elif outcome.status == "success": emit("done", outcome)
                    elif outcome.status == "awaiting-confirmation": emit("awaiting", outcome.confirmation)
                    elif outcome.status == "cancelled": emit("cancelled")
                    else: emit("agent_error", outcome.error_category)
                    return
                stream_method = getattr(self._engine, "stream", None)
                chunks: Iterable[str]
                if callable(stream_method):
                    chunks = stream_method(generation["content"], generation["history"], generation["mode"], generation["cancelled"])
                else:
                    chunks = (self._engine.respond(generation["content"], generation["history"], generation["mode"]),)
                answer_parts: list[str] = []
                for chunk in chunks:
                    text = str(chunk)
                    if not text or generation["cancelled"].is_set(): continue
                    answer_parts.append(text); emit("delta", text)
                engine_finished.set(); emit("done", "".join(answer_parts))
            except Exception as error:
                engine_finished.set()
                try: emit("error", error)
                except RuntimeError: pass
            finally:
                processed.wait()
                self._release(generation_id)

        Thread(target=run_engine, name="nova-conversation-generation", daemon=True).start()
        deadline = monotonic() + self._timeout
        terminal = False
        try:
            while True:
                if generation["cancelled"].is_set():
                    terminal = True; self._mark_terminal(generation_id, "cancelled")
                    self._journal_generation("generation.cancelled", generation_id, "cancelled")
                    yield self._event("generation.cancelled", generation_id, "cancelled")
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    try:
                        if not engine_finished.is_set(): raise asyncio.QueueEmpty
                        kind, value = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        terminal = True; self._mark_terminal(generation_id, "error")
                        self._journal_generation("generation.error", generation_id, "error", error_category="timeout")
                        yield self._event("generation.error", generation_id, "error", error="Le moteur Nova n’a pas répondu à temps.", code="timeout")
                        break
                else:
                    try: kind, value = await asyncio.wait_for(queue.get(), timeout=min(.1, remaining))
                    except asyncio.TimeoutError: continue
                if kind == "delta":
                    self._mark_responding(generation_id)
                    yield self._event("generation.delta", generation_id, "responding", delta=value)
                elif kind == "verified_result":
                    terminal = True
                    user, assistant = self._complete(generation_id, str(value.answer))
                    self._journal_generation("generation.completed", generation_id, "success")
                    yield self._event(
                        "generation.completed", generation_id, "success",
                        completion_state=value.completion_state,
                        user_message=user.model_dump(mode="json"),
                        assistant_message=assistant.model_dump(mode="json"),
                    )
                    break
                elif kind == "state":
                    state = value["status"]
                    self._mark_agent_state(generation_id, state)
                    yield self._event("generation.state", generation_id, state, capability_id=value.get("capability_id"))
                elif kind == "mission":
                    event, mission, capability_id = value
                    status = {"pending": "thinking", "running": "acting", "awaiting_confirmation": "awaiting-confirmation",
                              "paused": "error", "completed": "success", "failed": "error", "cancelled": "cancelled"}[mission.state]
                    if mission.state in {"completed", "failed", "cancelled"}:
                        self._remove_active_work(generation["conversation_id"], mission_id=mission.mission_id)
                    yield self._event(event, generation_id, status,
                                      mission_id=mission.mission_id, mission_state=mission.state,
                                      step_index=mission.current_step, step_count=mission.step_count,
                                      capability_id=capability_id, error_category=mission.last_error_category)
                elif kind == "mission_waiting":
                    terminal = True
                    mission, confirmation = value
                    with self._lock:
                        self._mission_confirmations[confirmation["token"]] = mission.mission_id
                    user = self._await_confirmation(generation_id, _MissionConfirmation(confirmation))
                    yield self._event("mission.awaiting_confirmation", generation_id, "awaiting-confirmation",
                                      mission_id=mission.mission_id, mission_state=mission.state,
                                      step_index=mission.current_step, step_count=mission.step_count,
                                      capability_id=mission.steps[mission.current_step]["capability_id"],
                                      user_message=user.model_dump(mode="json"), confirmation=confirmation)
                elif kind == "goal":
                    event, goal = value
                    yield self._event(event, generation_id, "thinking", goal_id=goal.goal_id,
                                      goal_status=goal.status)
                elif kind == "goal_clarification":
                    terminal = True
                    answer = ("Je ne trouve aucune mission reprenable." if value == 0 else
                              "Plusieurs missions peuvent être reprises. Indiquez laquelle vous souhaitez continuer.")
                    user, assistant = self._complete(generation_id, answer)
                    yield self._event("goal.clarification_required", generation_id, "success",
                                      user_message=user.model_dump(mode="json"),
                                      assistant_message=assistant.model_dump(mode="json"))
                    break
                elif kind == "goal_result":
                    terminal = True
                    goal = value
                    if goal.status == "awaiting_confirmation":
                        confirmation = goal.plan[goal.current_step].get("confirmation")
                        if not isinstance(confirmation, dict):
                            self._mark_terminal(generation_id, "error")
                            yield self._event("generation.error", generation_id, "error",
                                              error="Confirmation indisponible.")
                            break
                        with self._lock: self._goal_confirmations[confirmation["token"]] = goal.goal_id
                        user = self._await_confirmation(generation_id, _MissionConfirmation(confirmation))
                        yield self._event("goal.awaiting_confirmation", generation_id, "awaiting-confirmation",
                                          goal_id=goal.goal_id, goal_status=goal.status,
                                          capability_id=goal.plan[goal.current_step]["capability_id"],
                                          user_message=user.model_dump(mode="json"), confirmation=confirmation)
                    else:
                        self._remove_active_work(generation["conversation_id"], goal_id=goal.goal_id)
                        answer = self._goal_answer(goal)
                        user, assistant = self._complete(generation_id, answer)
                        event = "goal.completed" if goal.status.startswith("completed") else "goal.blocked"
                        yield self._event(event, generation_id,
                                          "success" if goal.status == "completed_verified" else "error",
                                          goal_id=goal.goal_id, goal_status=goal.status,
                                          error_category=goal.blockers[-1] if goal.blockers else None,
                                          user_message=user.model_dump(mode="json"),
                                          assistant_message=assistant.model_dump(mode="json"))
                    break
                elif kind == "awaiting":
                    terminal = True
                    user = self._await_confirmation(generation_id, value)
                    yield self._event("generation.awaiting_confirmation", generation_id, "awaiting-confirmation",
                                      user_message=user.model_dump(mode="json"), confirmation=value.public())
                    break
                elif kind == "cancelled":
                    terminal = True; self._mark_terminal(generation_id, "cancelled")
                    yield self._event("generation.cancelled", generation_id, "cancelled")
                    break
                elif kind == "agent_error":
                    terminal = True; self._mark_terminal(generation_id, "error")
                    self._journal_generation("generation.error", generation_id, "error", error_category=str(value))
                    try: code = GenerationErrorCode(value)
                    except (TypeError, ValueError): code = None
                    yield self._event("generation.error", generation_id, "error",
                                      error="La boucle agentique s’est arrêtée proprement.", code=code)
                    break
                elif kind == "done":
                    terminal = True
                    answer = value.answer if isinstance(value, AgentOutcome) else value
                    completion_state = value.completion_state if isinstance(value, AgentOutcome) else None
                    if not answer or not str(answer).strip():
                        self._mark_terminal(generation_id, "error")
                        self._journal_generation("generation.error", generation_id, "error", error_category="empty_response")
                        yield self._event("generation.error", generation_id, "error", error="Moteur Nova indisponible.")
                    else:
                        user, assistant = self._complete(generation_id, str(answer).strip())
                        self._journal_generation("generation.completed", generation_id, "success")
                        yield self._event("generation.completed", generation_id, "success",
                                          completion_state=completion_state,
                                          user_message=user.model_dump(mode="json"), assistant_message=assistant.model_dump(mode="json"))
                    break
                else:
                    terminal = True; self._mark_terminal(generation_id, "error")
                    self._journal_generation("generation.error", generation_id, "error", error_category="engine_error")
                    yield self._event("generation.error", generation_id, "error", error="Moteur Nova indisponible.")
                    break
        finally:
            if not terminal:
                generation["cancelled"].set(); self._mark_terminal(generation_id, "cancelled")
                self._journal_generation("generation.cancelled", generation_id, "cancelled")
            processed.set()
            if engine_finished.is_set():
                while generation_id in self._generations:
                    await asyncio.sleep(0)

    def cancel(self, conversation_id: str, generation_id: str) -> None:
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation is None or generation["conversation_id"] != conversation_id: raise GenerationNotFound()
            generation["cancelled"].set()
            mission_id = self._generation_missions.get(generation_id)
        self._journal_generation("generation.cancel_requested", generation_id, "running")
        if mission_id and self._missions is not None:
            mission = self._missions.cancel(mission_id)
            if mission.state == "cancelled":
                self._remove_active_work(conversation_id, mission_id=mission_id)
        with self._lock:
            goal_id = self._generation_goals.get(generation_id)
        if goal_id and self._goals is not None:
            goal = self._goals.cancel(goal_id)
            if goal.status == "cancelled":
                self._remove_active_work(conversation_id, goal_id=goal_id)
        # Do not claim terminal cancellation until the underlying engine/tool
        # has actually observed the cancellation request.  stream() publishes
        # generation.cancelled when execution has reached that safe boundary.

    async def decide_confirmation(self, conversation_id: str, token: str, approved: bool) -> tuple[str, ConversationMessage]:
        with self._lock: goal_id = self._goal_confirmations.get(token)
        if goal_id is not None and self._goals is not None:
            try: goal = self._goals.run(goal_id, confirmed_token=token, approved=approved)
            except Exception as error: raise GenerationNotFound() from error
            with self._lock:
                self._goal_confirmations.pop(token, None)
                self._pending_confirmations.pop(token, None)
            if goal.status != "awaiting_confirmation":
                self._remove_active_work(conversation_id, goal_id=goal.goal_id)
            answer = self._goal_answer(goal)
            assistant = ConversationMessage(message_id=self._id_factory(), role="assistant",
                                            content=answer, created_at=self._clock())
            with self._lock:
                session = self._sessions.get(conversation_id)
                if session is None: raise GenerationNotFound()
                session["messages"].append(assistant)
                self._trim_session_messages(session)
                session["status"] = "success" if goal.status == "completed_verified" else "idle"
                session["last_active_at"] = self._clock(); self._persist_session(conversation_id)
            self._state.set_goal_status(goal.status)
            return ("success" if approved else "refused"), assistant
        with self._lock: mission_id = self._mission_confirmations.get(token)
        if mission_id is not None and self._missions is not None:
            try: mission = self._missions.decide_confirmation(mission_id, token, approved)
            except Exception as error: raise GenerationNotFound() from error
            with self._lock: self._mission_confirmations.pop(token, None)
            if mission.state in {"completed", "failed", "cancelled"}:
                self._remove_active_work(conversation_id, mission_id=mission.mission_id)
            answer = "Mission terminée." if mission.state == "completed" else "La mission a été mise en pause."
            assistant = ConversationMessage(message_id=self._id_factory(), role="assistant", content=answer, created_at=self._clock())
            with self._lock:
                session = self._sessions.get(conversation_id)
                if session is None: raise GenerationNotFound()
                session["messages"].append(assistant); self._trim_session_messages(session)
                session["status"] = "success" if mission.state == "completed" else "idle"
                session["last_active_at"] = self._clock(); self._persist_session(conversation_id)
            return ("success" if approved else "refused"), assistant
        if self._agent_loop is None:
            raise GenerationNotFound()
        with self._lock:
            pending = self._pending_confirmations.get(token)
            session = self._sessions.get(conversation_id)
            if pending is None or session is None or pending.conversation_id != conversation_id:
                raise GenerationNotFound()
        try:
            request = (self._agent_loop.confirmations.consume(token, conversation_id=conversation_id) if approved
                       else self._agent_loop.confirmations.refuse(token, conversation_id=conversation_id))
        except ValueError as error:
            raise GenerationNotFound() from error
        with self._lock:
            self._pending_confirmations.pop(token, None)
        if approved:
            outcome = await asyncio.to_thread(self._agent_loop.resume, request, Event(), lambda *_args: None)
            if outcome.status != "success" or not outcome.answer:
                raise EngineUnavailableError("Agent resume failed")
            answer = outcome.answer
        else:
            answer = "L’action a été refusée et aucun fichier n’a été modifié."
            if self._journal:
                self._journal.append("agent.completed", generation_id=request.generation_id,
                                     conversation_id=conversation_id, capability_id=request.capability_id, status="refused")
        assistant = ConversationMessage(message_id=self._id_factory(), role="assistant", content=answer, created_at=self._clock())
        with self._lock:
            session = self._sessions[conversation_id]
            session["messages"].append(assistant); self._trim_session_messages(session); session["status"] = "success"
            session["last_active_at"] = self._clock(); self._persist_session(conversation_id)
        self._state.set("success", "Réponse terminée", "Nova a répondu.", busy=False)
        return ("success" if approved else "refused"), assistant

    async def send(self, conversation_id: str, content: str) -> SendMessageResponse:
        generation_id = self.start_generation(conversation_id, content)
        async for event in self.stream(generation_id):
            if event["event"] in {"generation.completed", "goal.completed", "goal.clarification_required"}:
                return SendMessageResponse(user_message=event["user_message"], assistant_message=event["assistant_message"])
            if event["event"] == "generation.error":
                if event.get("code") == "timeout": raise EngineTimedOut()
                raise EngineUnavailableError("Nova engine failed")
        raise EngineUnavailableError("Nova generation cancelled")

    def _complete(self, generation_id: str, answer: str) -> tuple[ConversationMessage, ConversationMessage]:
        with self._lock:
            generation = self._generations[generation_id]; session = self._sessions[generation["conversation_id"]]
            if session["generation_id"] != generation_id: raise ConversationBusy()
            user = ConversationMessage(message_id=self._id_factory(), role="user", content=generation["content"], created_at=self._clock())
            assistant = ConversationMessage(message_id=self._id_factory(), role="assistant", content=answer, created_at=self._clock())
            session["messages"].extend((user, assistant)); self._trim_session_messages(session); session["status"] = "success"
            session["last_active_at"] = self._clock(); self._persist_session(generation["conversation_id"])
        self._set_state(generation_id, "success", "Réponse terminée", "Nova a répondu.", busy=False)
        return user, assistant

    @staticmethod
    def _goal_answer(goal: Any) -> str:
        if goal.status == "completed_verified":
            capabilities = ", ".join(item["capability_id"] for item in goal.evidence
                                     if item.get("verification_state") == "VERIFIED")
            return f"Objectif terminé et vérifié. Vérifications effectuées : {capabilities}."
        if goal.status == "awaiting_confirmation":
            return "J’ai préparé la prochaine étape vérifiée et j’ai besoin de votre confirmation avant d’écrire."
        reason = goal.blockers[-1] if goal.blockers else goal.status
        return f"L’objectif est bloqué : {reason}."

    def _mark_responding(self, generation_id: str) -> None:
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation:
                session = self._sessions.get(generation["conversation_id"])
                if session and session["generation_id"] == generation_id:
                    session["status"] = "responding"; self._persist_session(generation["conversation_id"])
        self._set_state(generation_id, "responding", "Réponse", "Nova répond.", busy=True)

    def _mark_terminal(self, generation_id: str, status: str) -> None:
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation:
                session = self._sessions.get(generation["conversation_id"])
                if session and session["generation_id"] == generation_id:
                    session["status"] = status; self._persist_session(generation["conversation_id"])

    def _mark_agent_state(self, generation_id: str, status: str) -> None:
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation:
                session = self._sessions.get(generation["conversation_id"])
                if session and session["generation_id"] == generation_id:
                    session["status"] = status
                    self._persist_session(generation["conversation_id"])
        label = "Nova agit…" if status == "acting" else "Réflexion"
        self._set_state(generation_id, status, label, label, busy=True)

    def _await_confirmation(self, generation_id: str, request: Any) -> ConversationMessage:
        with self._lock:
            generation = self._generations[generation_id]
            session = self._sessions[generation["conversation_id"]]
            user = ConversationMessage(message_id=self._id_factory(), role="user", content=generation["content"], created_at=self._clock())
            session["messages"].append(user); self._trim_session_messages(session); session["status"] = "awaiting-confirmation"
            session["last_active_at"] = self._clock(); self._persist_session(generation["conversation_id"])
            self._pending_confirmations[request.token] = request
        self._set_state(generation_id, "awaiting-confirmation", "Confirmation requise", "Nova attend votre confirmation.", busy=False)
        return user

    def _release(self, generation_id: str) -> None:
        with self._lock:
            generation = self._generations.pop(generation_id, None)
            if generation is None: return
            session = self._sessions.get(generation["conversation_id"])
            is_current = bool(session and session["generation_id"] == generation_id)
            if is_current:
                session["busy"], session["generation_id"] = False, None
                session["last_active_at"] = self._clock(); self._persist_session(generation["conversation_id"])
            self._generation_lock.release()
        if is_current and generation["cancelled"].is_set(): self._state.set("idle", "Disponible", "Génération annulée.", busy=False)
        elif is_current and session and session["status"] == "error": self._state.set("error", "Erreur", "L’opération en arrière-plan est terminée.", busy=False)

    def _trim_session_messages(self, session: dict[str, Any]) -> None:
        """Keep conversations usable indefinitely while bounding active prompt history."""
        messages = session.get("messages")
        if not isinstance(messages, list) or len(messages) <= self._max_messages:
            return
        dropped = len(messages) - self._max_messages
        del messages[:dropped]
        session["trimmed_message_count"] = max(0, int(session.get("trimmed_message_count", 0) or 0)) + dropped

    def _remove_active_work(self, conversation_id: str, *, goal_id: str | None = None,
                            mission_id: str | None = None) -> None:
        with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None:
                return
            if goal_id:
                session["active_goal_ids"] = [value for value in session.get("active_goal_ids", []) if value != goal_id]
            if mission_id:
                session["active_mission_ids"] = [value for value in session.get("active_mission_ids", []) if value != mission_id]
            self._persist_session(conversation_id)

    def _persist_session(self, conversation_id: str) -> None:
        if self._conversation_store is None:
            return
        session = self._sessions.get(conversation_id)
        if session is None:
            return
        self._conversation_store.save(conversation_id, session, max_messages=self._max_messages)

    def _set_state(self, generation_id: str, state: str, label: str, message: str, *, busy: bool) -> None:
        with self._lock:
            generation = self._generations.get(generation_id)
            current = generation and self._sessions.get(generation["conversation_id"], {}).get("generation_id") == generation_id
        if current: self._state.set(state, label, message, busy=busy)  # type: ignore[arg-type]

    def _journal_generation(self, event_type: str, generation_id: str, status: str, *, error_category: str | None = None) -> None:
        if self._journal is None:
            return
        with self._lock:
            generation = self._generations.get(generation_id)
            if generation is None:
                return
            started_at = generation.get("started_at")
            conversation_id = generation["conversation_id"]
        duration_ms = None if event_type == "generation.started" or started_at is None else int((monotonic() - started_at) * 1000)
        self._journal.append(
            event_type, generation_id=generation_id, conversation_id=conversation_id,
            status=status, duration_ms=duration_ms, error_category=error_category,
        )

    @staticmethod
    def _event(event: str, generation_id: str, status: str, **payload: Any) -> dict[str, Any]:
        return GenerationEvent(event=event, generation_id=generation_id, status=status, **payload).model_dump(mode="json", exclude_none=True)  # type: ignore[arg-type]
