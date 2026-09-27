"""Typed response contracts for the local Nova API."""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base for compact public contracts that reject undocumented fields."""

    model_config = ConfigDict(extra="forbid")


NovaState = Literal[
    "idle",
    "listening",
    "thinking",
    "responding",
    "acting",
    "awaiting-confirmation",
    "success",
    "error",
    "self-improving",
]


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    service: Literal["nova-api"] = "nova-api"
    version: Literal["1"] = "1"


class StateResponse(StrictModel):
    state: NovaState
    label: str
    busy: bool
    message: str


ComponentStatus = Literal["available", "degraded", "unavailable", "unknown"]
ValidationStatus = Literal["passed", "failed", "unknown"]
AlertLevel = Literal["info", "warning", "error"]


class CockpitApiStatus(StrictModel):
    """Status of the process serving this snapshot."""

    status: Literal["ok"] = "ok"
    uptime_seconds: int


class ProjectStatus(StrictModel):
    """Non-sensitive Git summary; file names and paths are never included."""

    git_available: bool
    branch: str | None
    clean: bool | None
    modified_count: int | None
    untracked_count: int | None


class ComponentStatusItem(StrictModel):
    """A cheap, side-effect-free component availability observation."""

    id: str
    label: str
    status: ComponentStatus
    message: str


class ValidationSummary(StrictModel):
    """Last safely known validation result, without triggering validation."""

    status: ValidationStatus
    generated_at: str | None = None
    passed: int | None = None
    failed: int | None = None
    warnings: int | None = None
    message: str


class CockpitAlert(StrictModel):
    """Sanitized, actionable cockpit alert."""

    level: AlertLevel
    message: str


class CockpitResponse(StrictModel):
    """Stable public snapshot for the local read-only cockpit."""

    generated_at: str
    api: CockpitApiStatus
    nova: StateResponse
    project: ProjectStatus
    components: list[ComponentStatusItem]
    validation: ValidationSummary
    alerts: list[CockpitAlert]


ConversationStatus = Literal["idle", "thinking", "acting", "awaiting-confirmation", "responding", "success", "error", "cancelled"]
ConversationMode = Literal["supervised", "autonomous-local"]
MessageRole = Literal["user", "assistant"]


class ConversationMessage(StrictModel):
    message_id: str
    role: MessageRole
    content: str
    created_at: datetime


class CreateConversationRequest(StrictModel):
    mode: ConversationMode = "supervised"


class ConversationSummary(StrictModel):
    conversation_id: str
    created_at: datetime
    status: ConversationStatus
    mode: ConversationMode


class ConversationDetail(ConversationSummary):
    messages: list[ConversationMessage]


class SendMessageRequest(StrictModel):
    content: str


class SendMessageResponse(StrictModel):
    user_message: ConversationMessage
    assistant_message: ConversationMessage
    status: Literal["success"] = "success"


class ConfirmationDecisionRequest(StrictModel):
    token: str
    approved: bool


class ConfirmationCard(StrictModel):
    token: str
    capability_id: str
    path: str
    effect: str
    expires_in_seconds: int


class ConfirmationDecisionResponse(StrictModel):
    status: Literal["success", "refused"]
    assistant_message: ConversationMessage


GenerationStatus = Literal[
    "connecting", "thinking", "acting", "awaiting-confirmation", "responding", "success", "error", "cancelled"
]


class GenerationErrorCode(str, Enum):
    """Bounded public categories emitted when conversational generation stops."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NO_TOOL_CAPABLE_PROVIDER = "no_tool_capable_provider"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    EMPTY_RESPONSE = "empty_response"
    CAPABILITY_NOT_ALLOWED = "capability_not_allowed"
    INVALID_ARGUMENTS = "invalid_arguments"
    ACTION_BUDGET_EXCEEDED = "action_budget_exceeded"
    DISCOVERY_BUDGET_EXCEEDED = "discovery_budget_exceeded"
    REPEATED_OBSERVATION = "repeated_observation"
    TIMEOUT = "timeout"
    OMNIROUTE_TIMEOUT = "omniroute_timeout"
    PROVIDER_INVALID_REQUEST = "provider_invalid_request"
    MODEL_TURN_BUDGET_EXCEEDED = "model_turn_budget_exceeded"
    PLAN_INVALID = "PLAN_INVALID"
    NO_STRUCTURED_PLANNER_PROVIDER = "NO_STRUCTURED_PLANNER_PROVIDER"
    PLANNER_PROVIDER_UNAVAILABLE = "PLANNER_PROVIDER_UNAVAILABLE"
    PLANNER_PROTOCOL_ERROR = "PLANNER_PROTOCOL_ERROR"
    PLANNER_SCHEMA_UNSUPPORTED = "PLANNER_SCHEMA_UNSUPPORTED"


class CancelGenerationResponse(StrictModel):
    generation_id: str
    status: Literal["cancelled"] = "cancelled"


class GenerationEvent(StrictModel):
    event: Literal[
        "generation.started", "generation.delta", "generation.completed",
        "generation.error", "generation.cancelled", "generation.state", "generation.awaiting_confirmation",
        "mission.created", "mission.started", "mission.step.started", "mission.step.completed",
        "mission.awaiting_confirmation", "mission.paused", "mission.completed", "mission.failed", "mission.cancelled",
        "goal.created", "goal.started", "goal.awaiting_confirmation", "goal.completed", "goal.blocked",
        "goal.clarification_required",
    ]
    generation_id: str
    status: GenerationStatus
    delta: str | None = None
    error: str | None = None
    code: GenerationErrorCode | None = None
    user_message: ConversationMessage | None = None
    assistant_message: ConversationMessage | None = None
    capability_id: str | None = None
    confirmation: ConfirmationCard | None = None
    mission_id: str | None = None
    mission_state: Literal["pending", "running", "awaiting_confirmation", "paused", "completed", "failed", "cancelled"] | None = None
    step_index: int | None = None
    step_count: int | None = None
    error_category: str | None = None
    goal_id: str | None = None
    goal_status: str | None = None
    completion_state: Literal["completed_verified", "completed_unverified"] | None = None
    warning: str | None = None


class CapabilityMetadata(StrictModel):
    id: str
    description: str
    risk_level: Literal["low", "medium", "high"]
    reversible: bool
    requires_confirmation: bool
    expected_effect: str
    category: str
    argument_schema: dict[str, Any]


class CapabilitiesResponse(StrictModel):
    count: int
    capabilities: list[CapabilityMetadata]


class JournalEventResponse(StrictModel):
    event_id: str
    timestamp: str
    type: str
    generation_id: str | None = None
    action_id: str | None = None
    transaction_id: str | None = None
    conversation_id: str | None = None
    mission_id: str | None = None
    goal_id: str | None = None
    capability_id: str | None = None
    status: str
    duration_ms: int | None = None
    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_category: str | None = None
    structural_fingerprint: dict[str, Any] | None = None


class JournalEventsResponse(StrictModel):
    events: list[JournalEventResponse]


class ComputerStateResponse(StrictModel):
    observation_id: str
    generation: int
    observed_at: str
    platform: dict[str, str]
    session: dict[str, bool]
    displays: list[dict[str, int | bool]]
    windows: list[dict[str, Any]]
    window_count: int
    active_window: dict[str, Any] | None = None


MissionStatus = Literal["pending", "running", "awaiting_confirmation", "paused", "completed", "failed", "cancelled"]


class MissionStepRequest(StrictModel):
    capability_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CreateMissionRequest(StrictModel):
    conversation_id: str
    objective: str
    steps: list[MissionStepRequest]


class MissionStep(StrictModel):
    capability_id: str
    status: str
    confirmation: ConfirmationCard | None = None


class MissionResponse(StrictModel):
    mission_id: str
    conversation_id: str
    objective: str
    state: MissionStatus
    current_step: int
    step_count: int
    created_at: str
    updated_at: str
    last_error_category: str | None = None
    checkpoint: dict[str, Any]
    steps: list[MissionStep]


class MissionListResponse(StrictModel):
    missions: list[MissionResponse]


class ExecuteCapabilityRequest(StrictModel):
    capability_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class CapabilityExecutionResponse(StrictModel):
    action_id: str
    capability_id: str
    status: Literal["success", "error"]
    verified: bool
    duration_ms: int
    result: dict[str, Any] | None = None
    error_category: str | None = None


class TransactionResponse(StrictModel):
    transaction_id: str
    path: str
    status: Literal["pending", "committed", "rolled_back"]
    created_file: bool
    before_hash: str | None
    after_hash: str
    diff: str
    diff_truncated: bool


class ProjectBrainStatusResponse(StrictModel):
    status: Literal["ready", "unavailable"]
    file_count: int
    symbol_count: int
    last_updated: str | None = None
    estimated_size: int


class ProjectBrainRefreshResponse(ProjectBrainStatusResponse):
    updated: int
    removed: int
    duration_ms: int


class MemoryResponse(StrictModel):
    memory_id: str
    memory_type: str
    created_at: str
    updated_at: str
    source_type: str
    provenance: str
    subject: str
    content: str
    importance: int
    confidence: float
    status: str
    last_accessed: str | None = None
    access_count: int
    expires_at: str | None = None
    tags: list[str]


class MemoryListResponse(StrictModel):
    memories: list[MemoryResponse]


class RememberRequest(StrictModel):
    content: str = Field(min_length=1, max_length=2000)
    subject: str | None = Field(default=None, max_length=160)
    memory_type: Literal["FACT", "PREFERENCE", "DECISION", "PROCEDURE", "PROJECT_STATE", "TASK_STATE", "OUTCOME", "ERROR_LESSON"] = "FACT"


class ContextPreviewResponse(StrictModel):
    estimated_chars: int
    memory_ids: list[str]
    diagnostics: list[dict[str, Any]]


GoalStatus = Literal["pending", "running", "awaiting_confirmation", "paused", "completed_verified",
                     "completed_unverified", "blocked", "failed", "cancelled"]


class GoalStepRequest(StrictModel):
    step_id: str | None = None
    objective: str | None = Field(default=None, max_length=240)
    expected_evidence: str | None = Field(default=None, max_length=240)
    capability_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)


class CreateGoalRequest(StrictModel):
    conversation_id: str
    objective: str = Field(min_length=1, max_length=2000)
    success_criteria: str | None = Field(default=None, min_length=1, max_length=1000)
    steps: list[GoalStepRequest] | None = Field(default=None, min_length=1, max_length=8)
    mission_id: str | None = None


class GoalConfirmationRequest(StrictModel):
    token: str
    approved: bool = True


class GoalResponse(StrictModel):
    goal_id: str
    mission_id: str | None
    conversation_id: str
    objective: str
    success_criteria: str
    status: GoalStatus
    phase: str
    plan_version: int
    current_step: int
    replans: int
    failed_steps: int
    mutating_actions: int
    discovery_actions: int
    model_calls: int
    created_at: str
    updated_at: str
    plan: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    blockers: list[str]
    checkpoint: dict[str, Any]
    metrics: dict[str, Any]
