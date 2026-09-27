"""Canonical, secret-free provenance for Planner model routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class PlannerRouteAttempt:
    provider: str = "unknown"
    model: str = "unknown"
    role: str = "planner"
    task_profile: str = "planning"
    attempt_index: int = 0
    duration_ms: int = 0
    timeout_ms: int = 0
    outcome: str = "unknown"
    error_category: str | None = None
    retryable_same_model: bool = False
    retryable_other_model: bool = False
    retryable_other_provider: bool = False
    cooldown_state: str | None = None
    budget_before: int | None = None
    budget_after: int | None = None
    fallback_reason: str | None = None


@dataclass
class PlannerRuntimeTrace:
    task_run_id: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    elapsed_ms: int = 0
    total_deadline_ms: int = 0
    model_call_budget_initial: int = 0
    model_call_budget_used: int = 0
    routes_attempted: list[PlannerRouteAttempt] = field(default_factory=list)
    selected_route: str | None = None
    final_status: str = "running"
    final_error_category: str | None = None
    plan_validation_status: str = "not_run"
    repair_attempted: bool = False
    repair_success: bool = False
    fallback_count: int = 0
    plan_produced: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: str, **data: Any) -> None:
        safe = {str(key)[:80]: value for key, value in data.items() if key not in {"prompt", "response", "messages"}}
        self.events.append({"event": str(event)[:80], **safe})
        self.events[:] = self.events[-64:]

    def absorb_meta(self, meta: Mapping[str, Any], *, budget_initial: int, budget_used: int) -> None:
        history = meta.get("attempt_history", []) if isinstance(meta, Mapping) else []
        offset = len(self.routes_attempted)
        for index, raw in enumerate(history, start=1):
            if not isinstance(raw, Mapping):
                continue
            reason = str(raw.get("reason") or "")
            error = reason if str(raw.get("result")) == "error" else None
            self.routes_attempted.append(PlannerRouteAttempt(
                provider=str(raw.get("provider") or "unknown")[:120],
                model=str(raw.get("model") or "unknown")[:200],
                attempt_index=offset + index,
                duration_ms=max(0, int(raw.get("duration_ms") or 0)),
                timeout_ms=max(0, int(raw.get("timeout_ms") or 0)),
                outcome=str(raw.get("result") or "unknown")[:40],
                error_category=error[:80] if error else None,
                retryable_same_model=bool(raw.get("retry_same_model")),
                retryable_other_model=bool(raw.get("retry_other_model")),
                retryable_other_provider=bool(raw.get("fallback_provider")),
                cooldown_state=str(raw.get("cooldown_state"))[:80] if raw.get("cooldown_state") else None,
                budget_before=raw.get("budget_before"), budget_after=raw.get("budget_after"),
                fallback_reason=reason[:120] if reason and index > 1 else None,
            ))
            event = "PLANNER_ROUTE_FAILED" if str(raw.get("result")) == "error" else "PLANNER_ROUTE_ATTEMPT"
            self.emit(event, provider=str(raw.get("provider") or "unknown")[:120], model=str(raw.get("model") or "unknown")[:200], outcome=str(raw.get("result") or "unknown")[:40], error_category=error[:80] if error else None)
            if index > 1:
                self.emit("PLANNER_FALLBACK", reason=reason[:120] or "next_route")
        self.model_call_budget_initial = max(0, int(budget_initial))
        self.model_call_budget_used = max(0, int(budget_used))
        provider, model = meta.get("provider"), meta.get("model")
        self.selected_route = f"{provider}:{model}" if provider or model else None
        self.fallback_count = max(0, sum(item.outcome in {"success", "error"} for item in self.routes_attempted) - 1)
        self.elapsed_ms = max(self.elapsed_ms, int(meta.get("duration_ms") or 0))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def planner_error_category(error_kind: str | None, *, validation: bool = False, repair: bool = False) -> str:
    if validation:
        return "planner_plan_repair_failure" if repair else "planner_plan_validation_failure"
    normalized = str(error_kind or "").upper()
    if "BUDGET" in normalized:
        return "planner_budget_exhausted"
    if "TIMEOUT" in normalized:
        return "planner_timeout"
    if normalized in {"INVALID_RESPONSE", "RESPONSE_NORMALIZATION_ERROR"}:
        return "planner_invalid_response"
    if normalized in {"ALL_ROUTES_EXHAUSTED", "ROUTES_NOT_AVAILABLE", "ROUTES_FAILED", "ROUTES_SKIPPED", "DEADLINE_EXHAUSTED", "AUTH_ERROR", "CONNECTION_ERROR", "PROVIDER_UNAVAILABLE", "UPSTREAM_ERROR", "TRANSIENT_SERVER_ERROR", "RATE_LIMIT", "LOCAL_MODEL_UNAVAILABLE"}:
        return "planner_routing_failure"
    return "planner_runtime_failure"
