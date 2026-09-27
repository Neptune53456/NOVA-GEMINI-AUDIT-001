"""Application factory for Nova's loopback-only, read-only HTTP API."""

from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .cockpit import CockpitSource, LocalCockpitSource
from .capabilities import CapabilityNotFound, CapabilityRegistry, ConfirmationRequired, build_default_registry
from .agent_loop import AgentLoop, tool_definitions
from .conversation_service import ConversationService
from .context_builder import ContextBuilder
from .autonomy import GoalNotFound, GoalRunner, GoalStateError
from .initial_goal_planner import InitialGoalPlanner, InitialPlanInvalid
from .conversations import build_router
from .engine_adapter import ConversationEngine, NovaEngineAdapter
from .journal import EventJournal
from .god_eye.api import build_router as build_god_eye_router
from .god_eye.service import GodEyeService
from .model_usage import ModelUsageStore
from .memory_store import MemoryRejected, MemoryStore
from .missions import MissionManager, MissionNotFound, MissionStateError
from .project_brain import ProjectBrain
from .schemas import (CapabilitiesResponse, CapabilityExecutionResponse, CockpitResponse,
                      ExecuteCapabilityRequest, HealthResponse, JournalEventsResponse,
                      StateResponse, TransactionResponse, CreateMissionRequest, MissionListResponse,
                      MissionResponse, ConfirmationDecisionRequest, ComputerStateResponse)
from .schemas import ProjectBrainRefreshResponse, ProjectBrainStatusResponse
from .schemas import ContextPreviewResponse, MemoryListResponse, MemoryResponse, RememberRequest
from .schemas import CreateGoalRequest, GoalConfirmationRequest, GoalResponse
from .transactions import TransactionError
from .state import ApiStateStore

StateSource = Callable[[], StateResponse | Mapping[str, Any]]

ALLOWED_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


def _default_state() -> StateResponse:
    return StateResponse(
        state="idle",
        label="Disponible",
        busy=False,
        message="Nova est disponible.",
    )


def create_app(
    state_source: StateSource | None = None,
    cockpit_source: CockpitSource | None = None,
    conversation_engine: ConversationEngine | None = None,
    conversation_service: ConversationService | None = None,
    journal: EventJournal | None = None,
    capability_registry: CapabilityRegistry | None = None,
    mission_manager: MissionManager | None = None,
    project_brain: ProjectBrain | None = None,
    memory_store: MemoryStore | None = None,
    goal_runner: GoalRunner | None = None,
    initial_goal_planner: InitialGoalPlanner | None = None,
    god_eye_service: GodEyeService | None = None,
) -> FastAPI:
    """Build the API without starting a server or consulting runtime files."""
    state_store = ApiStateStore()
    source = state_source or state_store.snapshot
    cockpit = cockpit_source or LocalCockpitSource(source)
    event_journal = journal or EventJournal()
    capabilities = capability_registry or build_default_registry(event_journal)
    missions = mission_manager or MissionManager(capabilities, event_journal)
    brain = project_brain or ProjectBrain(capabilities.transactions.workspace)
    memory = memory_store or MemoryStore()
    context_builder = ContextBuilder(memory, project_brain=brain, missions=missions)
    model_usage = ModelUsageStore.for_journal(event_journal)
    initial_planner = initial_goal_planner or InitialGoalPlanner(capabilities, model_usage=model_usage)
    goals = goal_runner or GoalRunner(capabilities, event_journal, context_builder, memory,
                                     initial_planner=initial_planner)
    if goals.initial_planner is None:
        goals.initial_planner = initial_planner
    app = FastAPI(title="Nova API", version="1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/v1/state", response_model=StateResponse)
    def state() -> StateResponse:
        value = source()
        if isinstance(value, StateResponse):
            return value
        return StateResponse(**dict(value))

    @app.get("/api/v1/cockpit", response_model=CockpitResponse)
    def cockpit_snapshot() -> CockpitResponse:
        value = cockpit.snapshot()
        if isinstance(value, CockpitResponse):
            return value
        return CockpitResponse(**dict(value))

    @app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
    def available_capabilities() -> CapabilitiesResponse:
        values = capabilities.available()
        return CapabilitiesResponse(count=len(values), capabilities=values)

    @app.get("/api/v1/computer/state", response_model=ComputerStateResponse)
    def computer_state() -> ComputerStateResponse:
        result = capabilities.execute("computer.observe")
        if result.status != "success" or result.result is None:
            raise HTTPException(status_code=503, detail=result.error_category or "COMPUTER_UNAVAILABLE")
        return ComputerStateResponse(**result.result)

    @app.get("/api/v1/computer/windows/{window_ref}/ui")
    def computer_window_ui(window_ref: str, depth: int = Query(default=4, ge=0, le=8),
                           max_elements: int = Query(default=80, ge=1, le=200)) -> dict[str, Any]:
        result = capabilities.execute("computer.ui.inspect", {"window_ref": window_ref,
                                                               "depth": depth, "max_elements": max_elements})
        if result.status != "success" or result.result is None:
            raise HTTPException(status_code=503, detail=result.error_category or "UI_AUTOMATION_UNAVAILABLE")
        return result.result

    @app.get("/api/v1/computer/displays")
    def computer_displays() -> dict[str, Any]:
        result = capabilities.execute("computer.visual.displays")
        if result.status != "success" or result.result is None:
            raise HTTPException(status_code=503, detail=result.error_category or "SCREEN_CAPTURE_UNAVAILABLE")
        return result.result

    @app.post("/api/v1/actions", response_model=CapabilityExecutionResponse)
    def execute_capability(request: ExecuteCapabilityRequest) -> CapabilityExecutionResponse:
        try:
            result = capabilities.execute(request.capability_id, request.arguments, confirmed=request.confirmed)
        except CapabilityNotFound:
            raise HTTPException(status_code=404, detail="unknown_capability") from None
        except ConfirmationRequired:
            raise HTTPException(status_code=409, detail="confirmation_required") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_arguments") from None
        return CapabilityExecutionResponse(**asdict(result))

    def transaction_value(transaction_id: str) -> TransactionResponse:
        if capabilities.transactions is None:
            raise HTTPException(status_code=503, detail="transactions_unavailable")
        try:
            return TransactionResponse(**capabilities.transactions.get(transaction_id).public())
        except TransactionError as error:
            status = 404 if str(error) == "transaction_not_found" else 409
            raise HTTPException(status_code=status, detail=str(error)) from None

    @app.get("/api/v1/transactions/{transaction_id}", response_model=TransactionResponse)
    def get_transaction(transaction_id: str) -> TransactionResponse:
        return transaction_value(transaction_id)

    @app.post("/api/v1/transactions/{transaction_id}/commit", response_model=TransactionResponse)
    def commit_transaction(transaction_id: str) -> TransactionResponse:
        if capabilities.transactions is None:
            raise HTTPException(status_code=503, detail="transactions_unavailable")
        try:
            capabilities.transactions.commit(transaction_id)
        except TransactionError as error:
            status = 404 if str(error) == "transaction_not_found" else 409
            raise HTTPException(status_code=status, detail=str(error)) from None
        return transaction_value(transaction_id)

    @app.post("/api/v1/transactions/{transaction_id}/rollback", response_model=TransactionResponse)
    def rollback_transaction(transaction_id: str) -> TransactionResponse:
        if capabilities.transactions is None:
            raise HTTPException(status_code=503, detail="transactions_unavailable")
        try:
            capabilities.transactions.rollback(transaction_id)
        except TransactionError as error:
            status = 404 if str(error) == "transaction_not_found" else 409
            raise HTTPException(status_code=status, detail=str(error)) from None
        return transaction_value(transaction_id)

    @app.get("/api/v1/journal/events", response_model=JournalEventsResponse)
    def recent_events(limit: int = Query(default=50, ge=1, le=200)) -> JournalEventsResponse:
        return JournalEventsResponse(events=[asdict(event) for event in event_journal.recent(limit=limit)])

    @app.get("/api/v1/project-brain", response_model=ProjectBrainStatusResponse)
    def project_brain_status() -> ProjectBrainStatusResponse:
        return ProjectBrainStatusResponse(**brain.status())

    @app.post("/api/v1/project-brain/refresh", response_model=ProjectBrainRefreshResponse)
    def refresh_project_brain() -> ProjectBrainRefreshResponse:
        result = brain.refresh()
        return ProjectBrainRefreshResponse(**brain.status(), updated=int(result["updated"]),
                                           removed=int(result["removed"]), duration_ms=int(result["duration_ms"]))

    @app.get("/api/v1/memory", response_model=MemoryListResponse)
    def list_memory(limit: int = Query(default=50, ge=1, le=200)) -> MemoryListResponse:
        return MemoryListResponse(memories=[MemoryResponse(**item.public()) for item in memory.list(limit=limit)])

    @app.get("/api/v1/memory/{memory_id}", response_model=MemoryResponse)
    def get_memory(memory_id: str) -> MemoryResponse:
        item = memory.get(memory_id)
        if item is None: raise HTTPException(status_code=404, detail="memory_not_found")
        return MemoryResponse(**item.public())

    @app.post("/api/v1/memory", response_model=MemoryResponse, status_code=201)
    def remember(body: RememberRequest) -> MemoryResponse:
        try:
            item = memory.remember(memory_type=body.memory_type, source_type="explicit_api",
                provenance="USER_STATED", subject=body.subject or body.content[:160], content=body.content,
                importance=8, tags=["explicit"])
        except MemoryRejected as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return MemoryResponse(**item.public())

    @app.get("/api/v1/context/preview", response_model=ContextPreviewResponse)
    def preview_context(query: str = Query(min_length=1, max_length=2000)) -> ContextPreviewResponse:
        package = context_builder.build(query)
        return ContextPreviewResponse(estimated_chars=package.estimated_chars,
            memory_ids=list(package.memory_ids), diagnostics=list(package.diagnostics))

    @app.get("/api/v1/missions", response_model=MissionListResponse)
    def list_missions() -> MissionListResponse:
        return MissionListResponse(missions=[mission.public() for mission in missions.store.list()])

    @app.post("/api/v1/missions", response_model=MissionResponse, status_code=201)
    def create_mission(request: CreateMissionRequest) -> MissionResponse:
        try:
            mission = missions.create(request.conversation_id, request.objective,
                                      [step.model_dump() for step in request.steps])
            return MissionResponse(**mission.public())
        except (ValueError, KeyError):
            raise HTTPException(status_code=422, detail="invalid_mission") from None

    @app.get("/api/v1/missions/{mission_id}", response_model=MissionResponse)
    def get_mission(mission_id: str) -> MissionResponse:
        try: return MissionResponse(**missions.store.get(mission_id).public())
        except MissionNotFound: raise HTTPException(status_code=404, detail="mission_not_found") from None

    @app.post("/api/v1/missions/{mission_id}/resume", response_model=MissionResponse)
    def resume_mission(mission_id: str) -> MissionResponse:
        try: return MissionResponse(**missions.run(mission_id).public())
        except MissionNotFound: raise HTTPException(status_code=404, detail="mission_not_found") from None
        except MissionStateError as error: raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/v1/missions/{mission_id}/cancel", response_model=MissionResponse)
    def cancel_mission(mission_id: str) -> MissionResponse:
        try: return MissionResponse(**missions.cancel(mission_id).public())
        except MissionNotFound: raise HTTPException(status_code=404, detail="mission_not_found") from None

    @app.post("/api/v1/missions/{mission_id}/confirmations", response_model=MissionResponse)
    def decide_mission_confirmation(mission_id: str, body: ConfirmationDecisionRequest) -> MissionResponse:
        try: return MissionResponse(**missions.decide_confirmation(mission_id, body.token, body.approved).public())
        except MissionNotFound: raise HTTPException(status_code=404, detail="mission_not_found") from None
        except MissionStateError as error: raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/v1/goals", response_model=GoalResponse, status_code=201)
    def create_goal(request: CreateGoalRequest) -> GoalResponse:
        try:
            if request.steps is None and request.success_criteria is None:
                goal = goals.create_from_objective(request.conversation_id, request.objective,
                                                   mission_id=request.mission_id)
            elif request.steps is not None and request.success_criteria is not None:
                goal = goals.create(request.conversation_id, request.objective, request.success_criteria,
                                    [step.model_dump() for step in request.steps], mission_id=request.mission_id)
            else:
                raise ValueError("incomplete_explicit_plan")
            return GoalResponse(**goal.public())
        except InitialPlanInvalid:
            raise HTTPException(status_code=422, detail="PLAN_INVALID") from None
        except (ValueError, KeyError):
            raise HTTPException(status_code=422, detail="invalid_goal") from None

    @app.get("/api/v1/goals/{goal_id}", response_model=GoalResponse)
    def get_goal(goal_id: str) -> GoalResponse:
        try: return GoalResponse(**goals.store.get(goal_id).public())
        except GoalNotFound: raise HTTPException(status_code=404, detail="goal_not_found") from None

    @app.post("/api/v1/goals/{goal_id}/resume", response_model=GoalResponse)
    def resume_goal(goal_id: str, body: GoalConfirmationRequest | None = None) -> GoalResponse:
        try:
            goal = goals.run(goal_id, confirmed_token=body.token if body else None,
                             approved=body.approved if body else True)
            state_store.set_goal_status(goal.status)
            return GoalResponse(**goal.public())
        except GoalNotFound: raise HTTPException(status_code=404, detail="goal_not_found") from None
        except GoalStateError as error: raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/v1/goals/{goal_id}/pause", response_model=GoalResponse)
    def pause_goal(goal_id: str) -> GoalResponse:
        try:
            goal = goals.pause(goal_id)
            state_store.set_goal_status(goal.status)
            return GoalResponse(**goal.public())
        except GoalNotFound: raise HTTPException(status_code=404, detail="goal_not_found") from None

    @app.post("/api/v1/goals/{goal_id}/cancel", response_model=GoalResponse)
    def cancel_goal(goal_id: str) -> GoalResponse:
        try:
            goal = goals.cancel(goal_id)
            state_store.set_goal_status(goal.status)
            return GoalResponse(**goal.public())
        except GoalNotFound: raise HTTPException(status_code=404, detail="goal_not_found") from None

    if conversation_service is None:
        engine = conversation_engine or NovaEngineAdapter(tool_definitions=tool_definitions(capabilities))
        agent_loop = AgentLoop(engine, capabilities, event_journal, project_brain=brain,
                               context_builder=context_builder) if callable(getattr(engine, "agent_turn", None)) else None
        conversations = ConversationService(engine, state_store, journal=event_journal, agent_loop=agent_loop,
                                           mission_manager=missions, goal_runner=goals)
    else:
        conversations = conversation_service
    app.include_router(build_router(conversations))
    active_god_eye = god_eye_service or GodEyeService.for_journal(event_journal)
    app.include_router(build_god_eye_router(active_god_eye))

    @app.on_event("startup")
    def start_god_eye_scheduler() -> None:
        active_god_eye.scheduler.start()

    @app.on_event("shutdown")
    def stop_god_eye_scheduler() -> None:
        active_god_eye.scheduler.stop()

    return app
