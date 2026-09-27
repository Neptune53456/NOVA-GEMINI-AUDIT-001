import re
import os
import time
import json
import logging
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from self_improvement.context_budget import estimate_message_tokens
from self_improvement.provider_manager import (
    DEFAULT_CAPABILITIES,
    ERROR_POLICIES,
    ErrorKind,
    ProviderLeaseManager,
    supports_structured_output_with_tools,
)
from self_improvement.brain_pool import SmartModelRouter, TaskProfile
from self_improvement.model_catalog import ModelCatalog, ModelPool
from self_improvement.model_performance import ModelPerformanceMemory
from self_improvement.omniroute_provider import OmniRouteProvider, DEFAULT_OMNIROUTE_BASE_URL
from self_improvement.multimodal import openai_chat_messages
from self_improvement.observability import AgentTelemetry

import httpx

try:
    import ollama
except ImportError:  # pragma: no cover - optional when only remote providers are used
    ollama = None

try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:  # pragma: no cover - local-only installations
    Cerebras = None

try:
    from groq import Groq
except ImportError:  # pragma: no cover - optional remote backend
    Groq = None


# =========================
# MODÈLES DISPONIBLES
# =========================

FAST_MODEL = "qwen3:1.7b"
LIGHT_CODE_MODEL = "qwen2.5-coder:1.5b"
CODE_MODEL = "qwen2.5-coder:3b"
FALLBACK_MODEL = "qwen3:4b"
EMBED_MODEL = "nomic-embed-text"
MODEL_RUNTIME_TIMEOUT_SECONDS = 60.0
REMOTE_PROVIDER_TIMEOUT_SECONDS = max(
    5.0, min(float(os.environ.get("REMOTE_PROVIDER_TIMEOUT_SECONDS", "20")), 60.0)
)
CEREBRAS_MODEL = "gpt-oss-120b"
GROQ_MODEL = "openai/gpt-oss-120b"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
OMNIROUTE_API_BASE = os.environ.get("OMNIROUTE_BASE_URL") or os.environ.get("OMNIROUTE_API_BASE", DEFAULT_OMNIROUTE_BASE_URL)
OMNIROUTE_MODEL = os.environ.get("OMNIROUTE_MODEL", "auto")
MAX_MODEL_ATTEMPTS = max(1, int(os.environ.get("MAX_MODEL_ATTEMPTS", "6")))
MAX_PROVIDER_ATTEMPTS = max(1, int(os.environ.get("MAX_PROVIDER_ATTEMPTS", "3")))
MAX_VISION_MODEL_ATTEMPTS = max(1, min(3, int(os.environ.get("MAX_VISION_MODEL_ATTEMPTS", "3"))))
MAX_STRUCTURED_MODEL_ATTEMPTS = 3
MAX_TASK_MODEL_TIME_SECONDS = max(1.0, float(os.environ.get("MAX_TASK_MODEL_TIME", "180")))
MAX_PARALLEL_MODEL_CALLS = max(1, int(os.environ.get("MAX_PARALLEL_MODEL_CALLS", "2")))

_V7_CATALOG: ModelCatalog | None = None
_V7_ROUTER: SmartModelRouter | None = None
_LAST_COGNITIVE_SELECTION: dict[str, dict[str, str]] = {}
_TELEMETRY: AgentTelemetry | None = None
_ROUTING_METRICS_LOCK = Lock()
_ROUTING_METRICS = Counter()
_LOGGER = logging.getLogger(__name__)

_MESSAGE_FIELDS_BY_ROLE = {
    "system": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {"role", "content", "tool_calls"},
    "tool": {"role", "content", "tool_call_id"},
}


def _openai_tool_transport_messages(messages):
    """Return the strict cross-provider tool transcript shape."""
    translated = openai_chat_messages(messages)
    result = []
    for message in translated:
        role = message.get("role")
        allowed = _MESSAGE_FIELDS_BY_ROLE.get(role, {"role", "content"})
        item = {key: value for key, value in message.items() if key in allowed}
        if role == "assistant" and item.get("tool_calls") and not item.get("content"):
            item["content"] = None
        result.append(item)
    return result


def _redacted_provider_error(exc):
    response = getattr(exc, "response", None)
    body = getattr(exc, "body", None)
    if body is None and response is not None:
        try:
            body = response.json()
        except Exception:
            body = getattr(response, "text", None)
    if body is None:
        body = str(exc)
    value = json.dumps(body, ensure_ascii=False, default=str) if not isinstance(body, str) else body
    value = re.sub(r"(?i)(authorization|api[_-]?key|token)\s*[:=]\s*[^\s,}\"]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [REDACTED]", value)
    return value[:1000]


def _provider_request_diagnostic(provider, model, messages, tools, payload, exc):
    response = getattr(exc, "response", None)
    body = getattr(exc, "body", None)
    if body is None and response is not None:
        try:
            body = response.json()
        except Exception:
            body = None
    error = body.get("error", body) if isinstance(body, dict) else {}
    summaries, extras = [], []
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, Mapping) else None
        if not isinstance(message, Mapping):
            continue
        extra = sorted(set(message) - _MESSAGE_FIELDS_BY_ROLE.get(role, {"role", "content"}))
        if extra:
            extras.append({"index": index, "role": role, "fields": extra})
        if role in {"assistant", "tool"} and (message.get("tool_calls") or role == "tool"):
            for call in message.get("tool_calls") or [None]:
                function = call.get("function", {}) if isinstance(call, Mapping) else {}
                content = message.get("content")
                summaries.append({
                    "role": role, "has_tool_calls": bool(message.get("tool_calls")),
                    "tool_call_id": call.get("id") if isinstance(call, Mapping) else message.get("tool_call_id"),
                    "tool_name": function.get("name") or message.get("name"),
                    "content_kind": "none" if content is None else ("empty" if content == "" else "string" if isinstance(content, str) else type(content).__name__),
                })
    diagnostic = {
        "provider": provider, "route_model": model,
        "http_status": getattr(response, "status_code", None) or getattr(exc, "status_code", None),
        "provider_error_code": error.get("code") if isinstance(error, dict) else None,
        "provider_error_type": error.get("type") if isinstance(error, dict) else type(exc).__name__,
        "response": _redacted_provider_error(exc),
        "message_roles": [item.get("role") for item in messages if isinstance(item, Mapping)],
        "message_count": len(messages), "tool_count": len(tools or []),
        "serialized_request_size": len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")),
        "tool_messages": summaries, "unsupported_extra_fields": extras,
    }
    _LOGGER.warning("provider_invalid_request %s", json.dumps(diagnostic, ensure_ascii=False))
    return diagnostic


def _routing_metric(event: str, provider: str | None = None, model: str | None = None) -> None:
    with _ROUTING_METRICS_LOCK:
        _ROUTING_METRICS[event] += 1
        if provider:
            _ROUTING_METRICS[f"provider:{provider}:{event}"] += 1
        if model:
            _ROUTING_METRICS[f"model:{model}:{event}"] += 1


def routing_metrics_snapshot() -> dict[str, int | float]:
    with _ROUTING_METRICS_LOCK:
        result = dict(_ROUTING_METRICS)
        successes = result.get("primary_success", 0) + result.get("fallback_success", 0)
        result["average_attempts_before_success"] = round(result.get("calls", 0) / successes, 3) if successes else 0.0
        return result


def _allocate_route_timeout(remaining_seconds: float, routes_left: int) -> float:
    """Provider-neutral allocation that preserves time for at least one fallback."""
    remaining = max(0.0, float(remaining_seconds))
    if routes_left <= 1:
        share = remaining
    else:
        share = remaining * 0.60
    return max(0.05, min(share, REMOTE_PROVIDER_TIMEOUT_SECONDS))


def _classify_route_exhaustion(*, candidate_count: int, attempted_count: int,
                               skipped_count: int, deadline_remaining_ms: int) -> ErrorKind:
    """Return the terminal routing cause without collapsing it into a generic error."""
    if deadline_remaining_ms <= 0:
        return ErrorKind.DEADLINE_EXHAUSTED
    if attempted_count > 0:
        return ErrorKind.ROUTES_FAILED
    if skipped_count > 0:
        return ErrorKind.ROUTES_SKIPPED
    if candidate_count <= 0:
        return ErrorKind.ROUTES_NOT_AVAILABLE
    return ErrorKind.ROUTES_NOT_AVAILABLE


_LEGACY_ERROR_KINDS = {
    "timeout": ErrorKind.TIMEOUT,
    "rate_limit": ErrorKind.RATE_LIMIT,
    "auth_error": ErrorKind.AUTH_ERROR,
    "missing_api_key": ErrorKind.MISSING_API_KEY,
    "payment_required": ErrorKind.PAYMENT_REQUIRED,
    "network_error": ErrorKind.CONNECTION_ERROR,
    "model_not_found": ErrorKind.MODEL_NOT_FOUND,
    "token_limit": ErrorKind.CONTEXT_LIMIT,
    "invalid_request": ErrorKind.INVALID_REQUEST,
    "inference_error": ErrorKind.TRANSIENT_SERVER_ERROR,
    "response_normalization_error": ErrorKind.INVALID_RESPONSE,
    "invalid_response": ErrorKind.INVALID_RESPONSE,
    "local_model_unavailable": ErrorKind.LOCAL_MODEL_UNAVAILABLE,
    "routes_not_available": ErrorKind.ROUTES_NOT_AVAILABLE,
    "routes_failed": ErrorKind.ROUTES_FAILED,
    "routes_skipped": ErrorKind.ROUTES_SKIPPED,
    "deadline_exhausted": ErrorKind.DEADLINE_EXHAUSTED,
    "unsupported_capability": ErrorKind.UNSUPPORTED_CAPABILITY,
    "all_routes_exhausted": ErrorKind.ALL_ROUTES_EXHAUSTED,
}


def _failure_health_scope(provider: str, model: str, task_type: str, error: "ModelRouterError") -> str:
    """Keep request/model incompatibilities from poisoning an entire provider."""
    if error.error_kind in {ErrorKind.INVALID_REQUEST, ErrorKind.CONTEXT_LIMIT, ErrorKind.CONTEXT_TOO_LARGE}:
        return f"{provider}:{model}:task:{task_type}"
    if error.policy.severity == "model":
        return f"{provider}:{model}"
    if task_type == "visual_observation" and provider.startswith("omniroute:") and error.error_kind in {
        ErrorKind.TIMEOUT, ErrorKind.UPSTREAM_ERROR, ErrorKind.TRANSIENT_SERVER_ERROR,
        ErrorKind.INVALID_RESPONSE, ErrorKind.MODEL_UNAVAILABLE,
    }:
        return f"{provider}:{model}"
    return provider


def _vision_candidate_is_eligible(model: Any, task_type: str) -> bool:
    """Apply explicit capability and known-health gates before consuming the vision bound."""
    if model.supports_vision is not True:
        return False
    if str(getattr(model, "health", "unknown")).casefold() in {"offline", "unavailable", "unhealthy"}:
        return False
    if float(getattr(model, "cooldown_until", 0.0) or 0.0) > time.time():
        return False
    provider = f"omniroute:{model.provider}"
    route = f"{provider}:{model.id}"
    return all(_is_provider_available(scope) for scope in (route, f"{route}:task:{task_type}"))


def _vision_diversity_key(model: Any) -> tuple[str, str]:
    """Identify the upstream vendor/family without trusting route aliases as diversity."""
    metadata = getattr(model, "raw_metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    model_id = str(getattr(model, "id", "unknown")).strip().casefold()
    path_parts = [part.lstrip("@").strip() for part in model_id.split("/") if part.strip()]

    vendor = next((
        str(metadata[key]).strip().casefold()
        for key in ("vendor", "upstream_provider", "publisher", "organization", "owned_by")
        if isinstance(metadata.get(key), str)
        and str(metadata[key]).strip().casefold() not in {"system", "openai", "omni", "omniroute"}
    ), "")
    if not vendor:
        vendor = path_parts[-2] if len(path_parts) >= 2 else str(getattr(model, "provider", "unknown")).casefold()

    model_name = path_parts[-1] if path_parts else model_id
    family = re.split(r"[-_.:]?(?=\d)|[-_.:]", model_name, maxsplit=1)[0] or model_name
    return vendor, family


def _select_diverse_vision_candidates(candidates: list[Any], limit: int) -> list[Any]:
    """Keep rank order while taking distinct upstream families before backfilling."""
    unique = []
    seen_models = set()
    for candidate in candidates:
        model_id = candidate.model.id
        if model_id not in seen_models:
            seen_models.add(model_id)
            unique.append(candidate)

    selected = []
    selected_models = set()
    seen_vendors = set()
    seen_model_families = set()
    for candidate in unique:
        vendor, model_family = _vision_diversity_key(candidate.model)
        if vendor in seen_vendors or model_family in seen_model_families:
            continue
        selected.append(candidate)
        selected_models.add(candidate.model.id)
        seen_vendors.add(vendor)
        seen_model_families.add(model_family)
        if len(selected) >= limit:
            return selected
    for candidate in unique:
        vendor, model_family = _vision_diversity_key(candidate.model)
        if candidate.model.id in selected_models or (
            vendor in seen_vendors and model_family in seen_model_families
        ):
            continue
        selected.append(candidate)
        selected_models.add(candidate.model.id)
        seen_vendors.add(vendor)
        seen_model_families.add(model_family)
        if len(selected) >= limit:
            return selected
    for candidate in unique:
        if candidate.model.id not in selected_models:
            selected.append(candidate)
            if len(selected) >= limit:
                break
    return selected


def _visual_attempt_diagnostic(*, attempt_index: int, provider: str, model: str,
                               family: tuple[str, str] | None, result: str,
                               status_code: int | None = None) -> None:
    status = str(status_code) if status_code is not None else "none"
    status_class = f"{status_code // 100}xx" if isinstance(status_code, int) else "none"
    print(
        f"[VisionFallback] phase=visual_analysis attempt_index={attempt_index} "
        f"provider={provider} model={model} family={'/'.join(family or ('unknown', 'unknown'))} result={result} "
        f"http_status={status} http_class={status_class}"
    )


def _canonical_error_kind(kind: str | ErrorKind) -> ErrorKind:
    if isinstance(kind, ErrorKind):
        return kind
    return _LEGACY_ERROR_KINDS.get(str(kind).casefold(), ErrorKind.UNKNOWN)


def _telemetry() -> AgentTelemetry:
    global _TELEMETRY
    if _TELEMETRY is None:
        _TELEMETRY = AgentTelemetry(Path.cwd())
    return _TELEMETRY


def _v7_components() -> tuple[ModelCatalog, SmartModelRouter]:
    """Construit paresseusement le brain pool; aucun appel reseau a l'import."""
    global _V7_CATALOG, _V7_ROUTER
    if _V7_CATALOG is None:
        provider = OmniRouteProvider(timeout_seconds=float(os.environ.get("OMNIROUTE_DISCOVERY_TIMEOUT_SECONDS", "5")))
        _V7_CATALOG = ModelCatalog(provider)
    if _V7_ROUTER is None:
        memory = ModelPerformanceMemory(Path.cwd() / ".runtime" / "model_performance.json")
        _V7_ROUTER = SmartModelRouter(_V7_CATALOG, memory=memory)
    return _V7_CATALOG, _V7_ROUTER


def reset_v7_brain_pool() -> None:
    global _V7_CATALOG, _V7_ROUTER
    _V7_CATALOG = None
    _V7_ROUTER = None


def discover_models(*, force: bool = False):
    catalog, _ = _v7_components()
    return catalog.refresh(force=force)


def routing_diagnostic(task_type: str = "general", *, pool: str | None = None) -> dict:
    catalog, router = _v7_components()
    models = catalog.models(pool=ModelPool(pool.casefold()) if pool else None)
    role = {"planning": "planner", "coding": "developer", "review": "reviewer"}.get(task_type.casefold())
    profile = TaskProfile(task_type=task_type, agent_role=role, reasoning_required=role in {"planner", "reviewer"})
    return router.route(profile, models=models).to_dict()


def omniroute_status(*, force: bool = True) -> dict:
    catalog, _ = _v7_components()
    started = time.perf_counter()
    try:
        models = catalog.refresh(force=force)
        reachable, error = True, None
    except Exception as exc:
        models, reachable, error = [], False, type(exc).__name__
    counts = catalog.pool_counts() if models else {pool.value: 0 for pool in ModelPool}
    return {
        "reachable": reachable,
        "base_url": catalog.provider.base_url,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "models_count": len(models),
        "coding_models": counts[ModelPool.CODING.value],
        "reasoning_models": counts[ModelPool.REASONING.value],
        "free_models": counts[ModelPool.FREE.value],
        "tool_calling_models": counts[ModelPool.TOOL_CALLING.value],
        "healthy": reachable and bool(models),
        "error_kind": error,
    }


def model_catalog_snapshot(*, pool: str | None = None, force: bool = False) -> dict:
    catalog, _ = _v7_components()
    models = catalog.refresh(force=force)
    if pool:
        wanted = ModelPool(pool.casefold())
        models = [model for model in models if wanted in model.pools]
    return {
        "models_count": len(models),
        "pool": pool,
        "models": [model.to_dict() for model in models],
        "pool_counts": catalog.pool_counts(),
    }


def ollama_status(*, timeout_seconds: float = 1.5) -> dict:
    base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    started = time.perf_counter()
    try:
        response = httpx.get(base + "/api/tags", timeout=max(0.1, min(float(timeout_seconds), 5.0)))
        response.raise_for_status()
        data = response.json()
        models = [str(item.get("name")) for item in data.get("models", []) if isinstance(item, dict) and item.get("name")]
        return {"configured": True, "available": True, "healthy": True, "models": models, "latency_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"configured": bool(os.environ.get("OLLAMA_HOST")), "available": False, "healthy": False, "models": [], "latency_ms": int((time.perf_counter() - started) * 1000), "error_kind": _http_error_kind(exc)}

# Conservative preflight limits. They can be overridden without code changes.
def _provider_input_limit(provider: str) -> int | None:
    env_name = f"{provider.upper()}_MAX_INPUT_TOKENS"
    raw = os.environ.get(env_name)
    if raw:
        try:
            return max(256, int(raw))
        except ValueError:
            pass
    capability = DEFAULT_CAPABILITIES.get(provider)
    return capability.max_input_tokens if capability else None


def _direct_provider_score(provider: str, estimated_tokens: int) -> float:
    scorer = getattr(_PROVIDER_LEASES, "score", None)
    if not callable(scorer):
        return 0.0
    capability = DEFAULT_CAPABILITIES.get(provider, DEFAULT_CAPABILITIES["openrouter"])
    return float(scorer(provider, capability, estimated_tokens=estimated_tokens))

_PROVIDER_LEASES = ProviderLeaseManager(state_path=Path.cwd() / ".runtime" / "provider_health.json")

# =========================
# ROUTEUR
# =========================

def choose_model(task_type="chat"):
    """
    Choisit le modèle adapté à la tâche.
    """

    routes = {
        "chat": FAST_MODEL,
        "memory": FAST_MODEL,
        "classification": FAST_MODEL,
        "simple": FAST_MODEL,
        # Génération adversariale structurée : privilégier le Qwen local rapide.
        "red_team_generation": FAST_MODEL,

        "web_synthesis": CODE_MODEL,
        "context_resolution": CODE_MODEL,
        "judge": CODE_MODEL,

        "code_light": LIGHT_CODE_MODEL,
        "analysis_light": LIGHT_CODE_MODEL,

        "code": CODE_MODEL,
        "analysis": CODE_MODEL,
        "repair": CODE_MODEL,
        "complex": CODE_MODEL,
        "planning": CODE_MODEL,
    }

    return routes.get(
        task_type,
        FAST_MODEL
    )


# =========================
# CHAT RAPIDE
# =========================

class EmptyModelResponseError(RuntimeError):
    """Signale deux réponses successives sans texte ni appel d'outil valide."""


class ModelRouterError(RuntimeError):
    """Erreur de backend structurée, sans exposer de secret dans le message."""

    def __init__(self, kind, message, *, provider=None, status_code=None, retryable=None, details=None):
        super().__init__(message)
        self.kind = kind
        self.error_kind = _canonical_error_kind(kind)
        self.policy = ERROR_POLICIES[self.error_kind]
        self.provider = provider
        self.status_code = status_code
        self.retryable = bool(retryable) if retryable is not None else self.policy.retryable
        self.details = dict(details or {})

    def diagnostic(self):
        return {
            "provider": self.provider,
            "kind": self.kind,
            "error_kind": self.error_kind.value,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_same_model": self.policy.retry_same_model,
            "retry_other_model": self.policy.retry_other_model,
            "fallback_provider": self.policy.fallback_provider,
            "permanent_for_campaign": self.policy.permanent_for_campaign,
            **self.details,
        }


class ModelBudgetExhaustedError(ModelRouterError):
    """Aucune nouvelle requete modele ne peut etre lancee."""

    def __init__(self, budget: "ModelCallBudget"):
        super().__init__(
            "model_budget_exhausted",
            "MODEL_BUDGET_EXHAUSTED",
            retryable=False,
            details={"status": "MODEL_BUDGET_EXHAUSTED", **budget.snapshot()},
        )


@dataclass
class ModelCallBudget:
    """Compteur partage qui reserve une unite avant chaque requete reelle."""

    max_calls: int
    used_calls: int = 0
    logical_requests: int = field(default=0, kw_only=True)
    deadline_monotonic: float | None = field(default=None, repr=False, compare=False, kw_only=True)
    parent: "ModelCallBudget | None" = field(default=None, repr=False, compare=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    role_counts: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    provider_attempts: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    attempt_trace: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    patch_failure_evidence: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self):
        self.max_calls = max(0, int(self.max_calls))
        self.used_calls = max(0, min(int(self.used_calls), self.max_calls))

    @property
    def remaining_calls(self):
        with self._lock:
            return self.max_calls - self.used_calls

    def remaining_seconds(self):
        if self.parent is not None:
            return self.parent.remaining_seconds()
        return float("inf") if self.deadline_monotonic is None else max(0.0, self.deadline_monotonic - time.monotonic())

    def consume(self, *, role: str = "unknown", provider: str = "unknown"):
        with self._lock:
            if self.remaining_seconds() <= 0:
                raise TimeoutError("GLOBAL_DEADLINE_EXHAUSTED")
            if self.used_calls >= self.max_calls:
                raise ModelBudgetExhaustedError(self)
            if self.parent is not None:
                self.parent.consume(role=role, provider=provider)
            self.used_calls += 1
            self.role_counts[str(role or "unknown")] = self.role_counts.get(str(role or "unknown"), 0) + 1
            self.provider_attempts[str(provider or "unknown")] = self.provider_attempts.get(str(provider or "unknown"), 0) + 1
            if self.parent is None:
                self.attempt_trace.append({
                    "role": str(role or "unknown"), "stage": str(role or "unknown").upper(),
                    "attempt": self.used_calls, "failure_in": None, "result": "started",
                    "retry_owner": "router", "consumed_call": True, "stop_reason": None,
                    "provider": str(provider or "unknown"), "duration_ms": None,
                    "information_gain": "pending", "changed_fields": [],
                    "reused_context": False, "retry_reason": None,
                    "useful": None, "output_disposition": "pending",
                })
                self.attempt_trace[:] = self.attempt_trace[-64:]
            if self.parent is None:
                self._write_worker_usage_report()
            return self.used_calls

    def begin_logical_request(self):
        """Count router entries separately; only consume() charges the hard cap."""
        if self.parent is not None:
            self.parent.begin_logical_request()
        with self._lock:
            self.logical_requests += 1

    def record_result(self, *, role: str, provider: str, result: str,
                      failure: str | None = None, duration_ms: int | None = None) -> None:
        if self.parent is not None:
            self.parent.record_result(role=role, provider=provider, result=result,
                                      failure=failure, duration_ms=duration_ms)
            return
        with self._lock:
            for item in reversed(self.attempt_trace):
                if (item.get("role") == str(role) and item.get("provider") == str(provider)
                        and item.get("result") == "started"):
                    item["result"] = str(result)[:80]
                    item["failure_in"] = str(failure)[:160] if failure else None
                    item["stop_reason"] = str(failure)[:160] if failure else None
                    item["duration_ms"] = max(0, int(duration_ms or 0))
                    succeeded = str(result).casefold() == "success"
                    changed_by_stage = {
                        "INITIAL_PLANNING": ["target", "strategy", "test_set"],
                        "PLAN_REPAIR": ["strategy"],
                        "DEVELOPER_EXPLORE": ["failure_understanding"],
                        "DEVELOPER_PLAN": ["strategy"],
                        "DEVELOPER_PATCH": ["patch", "candidate_state"],
                        "DEVELOPER_DIAGNOSE": ["failure_understanding"],
                        "REPLAN": ["target", "strategy", "test_set"],
                        "REVIEWER": ["candidate_state"], "REVIEW": ["candidate_state"],
                    }
                    stage = str(item.get("stage") or "OTHER")
                    item["changed_fields"] = changed_by_stage.get(stage, []) if succeeded else []
                    item["information_gain"] = "state_transition" if succeeded else "none"
                    item["useful"] = succeeded
                    item["retry_reason"] = str(failure)[:160] if failure else None
                    item["output_disposition"] = "reused" if succeeded else "discarded"
                    break
            self._write_worker_usage_report()

    def record_patch_failure(self, evidence: dict[str, Any]) -> None:
        if self.parent is not None:
            self.parent.record_patch_failure(evidence)
            return
        with self._lock:
            self.patch_failure_evidence.append(dict(evidence))
            self.patch_failure_evidence[:] = self.patch_failure_evidence[-16:]
            self._write_worker_usage_report()

    def _write_worker_usage_report(self):
        path_value = os.getenv("PROJET_IA_WORKER_USAGE_PATH", "").strip()
        if not path_value:
            return
        # Test subprocesses inherit the report path and may instantiate their
        # own root budgets.  Only the worker process which first claims the
        # journal may update it; descendants inherit the owner PID and cannot
        # overwrite authoritative usage with an unrelated test budget.
        owner_key = "PROJET_IA_WORKER_USAGE_OWNER_PID"
        owner_pid = os.getenv(owner_key, "").strip()
        current_pid = str(os.getpid())
        if owner_pid and owner_pid != current_pid:
            return
        if not owner_pid:
            os.environ[owner_key] = current_pid
        target = Path(path_value).resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "worker-usage/v1",
            "model_calls_total": self.used_calls,
            "logical_model_requests": self.logical_requests,
            "model_calls_by_role": dict(sorted(self.role_counts.items())),
            "provider_attempts": dict(sorted(self.provider_attempts.items())),
            "fallback_count": max(0, len([v for v in self.provider_attempts.values() if v]) - 1),
            "timeout_count": 0,
            "rate_limit_count": 0,
            "remaining_local_budget": self.max_calls - self.used_calls,
            "budget_exhausted": self.used_calls >= self.max_calls,
            "attempt_trace": list(self.attempt_trace[-64:]),
            "call_value_trace": [{
                "call_id": item.get("attempt"), "stage": item.get("stage", "OTHER"),
                "information_gain": item.get("information_gain", "pending"),
                "changed_fields": list(item.get("changed_fields") or []),
                "reused_context": bool(item.get("reused_context", False)),
                "retry_reason": item.get("retry_reason"), "useful": item.get("useful"),
                "output_disposition": item.get("output_disposition", "pending"),
            } for item in self.attempt_trace[-64:]],
            "patch_failure_evidence": list(self.patch_failure_evidence[-16:]),
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=target.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            temp_path = Path(handle.name)
        os.replace(temp_path, target)

    def child(self, max_calls):
        return ModelCallBudget(max_calls=max_calls, parent=self)

    def restrict(self, max_calls):
        """Resserre le plafond sans jamais oublier les appels deja envoyes."""
        with self._lock:
            self.max_calls = max(self.used_calls, min(self.max_calls, max(0, int(max_calls))))
            return self.max_calls

    def snapshot(self):
        return {
            "max_model_calls": self.max_calls,
            "model_calls_used": self.used_calls,
            "model_calls_remaining": self.max_calls - self.used_calls,
        }


@dataclass
class RoleBudgetState:
    """Read-only role allocation over one authoritative provider-call counter.

    Reservations are availability constraints, never additional counters.  Only
    ``ModelCallBudget.consume`` records a real provider call.
    """

    budget: ModelCallBudget
    reserved_by_role: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.reserved_by_role = {
            str(role): max(0, int(value))
            for role, value in self.reserved_by_role.items()
            if int(value) > 0
        }

    def available_for(self, role: str) -> int:
        reserved_for_others = sum(
            value for name, value in self.reserved_by_role.items() if name != role
        )
        return max(0, self.budget.remaining_calls - reserved_for_others)

    def release(self, role: str) -> None:
        self.reserved_by_role.pop(str(role), None)

    def child_for(self, role: str, *, maximum: int | None = None) -> ModelCallBudget:
        available = self.available_for(role)
        if maximum is not None:
            available = min(available, max(0, int(maximum)))
        return self.budget.child(available)

    def snapshot(self, role: str) -> dict[str, Any]:
        available = self.available_for(role)
        return {
            "role": str(role),
            "total": self.budget.max_calls,
            "consumed": self.budget.used_calls,
            "remaining": self.budget.remaining_calls,
            "reserved": dict(sorted(self.reserved_by_role.items())),
            "available": available,
            "decision": "allow" if available > 0 else "deny",
        }


def clean_model_output(text):
    """Retire le raisonnement balisé sans altérer une réponse texte normale."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    think_pattern = re.compile(
        r"<think\b[^>]*>.*?</think>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned_text, block_count = think_pattern.subn("", text)
    if block_count:
        return cleaned_text.strip()

    # Certains anciens modèles omettent la balise ouvrante mais terminent encore
    # leur raisonnement par </think>. Le suffixe constitue alors la réponse.
    if "</think>" in text.casefold():
        closing_index = text.casefold().rfind("</think>")
        return text[closing_index + len("</think>") :].strip()

    # Aucun marqueur : préserver intégralement le contenu fourni par le modèle.
    return text


def _as_plain_dict(value, label):
    """Convertit dict, objet Pydantic Ollama ou objet dict-like en dictionnaire."""
    if isinstance(value, Mapping):
        return dict(value)

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)

    items = getattr(value, "items", None)
    if callable(items):
        try:
            return dict(items())
        except (TypeError, ValueError):
            pass

    raise ValueError(f"{label} Ollama absent ou invalide.")


def _normalize_tool_calls(tool_calls):
    if not isinstance(tool_calls, (list, tuple)):
        return tool_calls

    normalized = []
    for call in tool_calls:
        try:
            call_data = _as_plain_dict(call, "Appel d'outil")
            function = call_data.get("function")
            if function is not None:
                call_data["function"] = _as_plain_dict(function, "Fonction d'outil")
            normalized.append(call_data)
        except ValueError:
            normalized.append(call)
    return normalized


def _has_valid_tool_calls(message):
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, (list, tuple)):
        return False
    for call in tool_calls:
        try:
            call_data = _as_plain_dict(call, "Appel d'outil")
            function = _as_plain_dict(call_data.get("function"), "Fonction d'outil")
        except ValueError:
            continue
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return True
    return False


def normalize_chat_response(response):
    """Retourne la forme dictionnaire stable attendue par tous les appelants."""
    response_data = _as_plain_dict(response, "Réponse")
    message = _as_plain_dict(response_data.get("message"), "Message")
    message["content"] = clean_model_output(message.get("content"))
    message["tool_calls"] = _normalize_tool_calls(message.get("tool_calls"))
    response_data["message"] = message
    return response_data


def _is_runtime_model_error(exc):
    if isinstance(exc, ModelRouterError):
        return exc.error_kind in {
            ErrorKind.TIMEOUT, ErrorKind.CONNECTION_ERROR, ErrorKind.NETWORK_ERROR,
            ErrorKind.PROVIDER_UNAVAILABLE, ErrorKind.UPSTREAM_ERROR,
            ErrorKind.TRANSIENT_SERVER_ERROR, ErrorKind.LOCAL_MODEL_UNAVAILABLE,
        }
    if isinstance(exc, (httpx.TimeoutException, httpx.HTTPError, TimeoutError, ConnectionError)):
        return True
    name = exc.__class__.__name__
    if name in {"ResponseError", "APIError", "ModelError", "ConnectionError", "TimeoutError"}:
        return True
    text = str(exc).lower()
    return any(keyword in text for keyword in ("timeout", "timed out", "connection", "unreachable", "inference", "ollama", "server"))


def _cerebras_error_kind(exc):
    """Classifie les erreurs Cerebras en kinds stables, sans exposer de secret."""
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return "timeout"
    if status_code == 402 or "payment_required" in text or "payment required" in text:
        return "payment_required"
    if status_code == 413 or "request too large" in text or "too many tokens" in text:
        return "token_limit"
    if status_code == 429 or "ratelimit" in name or "rate limit" in text or "quota" in text:
        return "rate_limit"
    if status_code in (401, 403) or "unauthorized" in text or "forbidden" in text or "authentication" in name or "auth" in name:
        return "auth_error"
    if status_code == 400 or "invalid_request" in name or "bad request" in text or "badrequest" in name:
        return "invalid_request"
    if status_code == 404 or "not_found" in name or "notfound" in name or "model not found" in text:
        return "model_not_found"
    if "connection" in name or "network" in text or "connect" in text:
        return "network_error"
    return "inference_error"


def _groq_error_kind(exc):
    """Classifie les erreurs Groq en kinds stables, sans exposer de secret.

    Groq SDK lève typiquement : RateLimitError, AuthenticationError,
    NotFoundError, BadRequestError, APITimeoutError, APIConnectionError.
    On préfère l'inspection par nom de classe pour éviter un import SDK.
    """
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return "timeout"
    if status_code == 402 or "payment_required" in text or "payment required" in text:
        return "payment_required"
    if status_code == 429 or "ratelimit" in name or "rate_limit" in name or "rate limit" in text or "quota" in text:
        return "rate_limit"
    if status_code == 413 or "request too large" in text or "too many tokens" in text or "requested" in text and "tokens" in text and "limit" in text:
        return "token_limit"
    if status_code in (401, 403) or "authentication" in name or "auth" in name or "unauthorized" in text or "forbidden" in text:
        return "auth_error"
    if status_code == 400 or "badrequest" in name or "bad_request" in name or "bad request" in text or "invalid_request" in name:
        return "invalid_request"
    if status_code == 404 or "notfound" in name or "not_found" in name or "model not found" in text:
        return "model_not_found"
    if "connection" in name or "apiconnection" in name or "network" in text or "connect" in text:
        return "network_error"
    return "inference_error"

def _gemini_error_kind(exc):
    """Classifie les erreurs Gemini API en kinds stables, sans exposer de secret."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"

    if isinstance(exc, httpx.RequestError):
        return "network_error"

    status_code = None

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
    else:
        status_code = getattr(exc, "status_code", None)

    text = str(exc).lower()

    if status_code == 402 or "payment required" in text:
        return "payment_required"

    if status_code == 413 or "request too large" in text or "too many tokens" in text:
        return "token_limit"

    if status_code == 429 or "quota" in text or "rate limit" in text:
        return "rate_limit"

    if status_code in (401, 403):
        return "auth_error"

    if status_code == 400:
        return "invalid_request"

    if status_code == 404:
        return "model_not_found"

    if status_code is not None and status_code >= 500:
        return "inference_error"

    return "inference_error"


# =========================
# PROVIDER HEALTH / COOLDOWN
# =========================

# Durées de cooldown par kind d'erreur (secondes).
_COOLDOWN_SECONDS: dict[str, float] = {
    "rate_limit": 60.0,
    "payment_required": 21600.0,
    "token_limit": 0.0,
    "auth_error": 3600.0,  # indisponible pour toute la session
    "network_error": 30.0,
    # Un timeout ne doit pas rappeler le même backend pendant la tâche suivante.
    "timeout": 300.0,
    "inference_error": 300.0,
    # Les 400 observés ici signalent typiquement une incompatibilité du modèle
    # OpenAI-compatible avec response_format/outils. Le retry se fait ailleurs.
    # A malformed/unsupported request says nothing about provider health.
    "invalid_request": 0.0,
    "model_not_found": 3600.0,
    "unknown_provider_error": 10.0,
}


@dataclass
class _ProviderState:
    status: str = "available"  # available | degraded | rate_limited | offline
    cooldown_until: float = field(default_factory=lambda: 0.0)
    consecutive_failures: int = 0
    last_error_kind: str | None = None


# État en mémoire — pas de thread, pas de base de données.
_PROVIDER_HEALTH: dict[str, _ProviderState] = {}
_PROVIDER_HEALTH_LOCK = RLock()


def _get_provider_state(provider: str) -> _ProviderState:
    with _PROVIDER_HEALTH_LOCK:
        if provider not in _PROVIDER_HEALTH:
            _PROVIDER_HEALTH[provider] = _ProviderState()
        return _PROVIDER_HEALTH[provider]


def _is_provider_available(provider: str) -> bool:
    """Retourne True si le provider n'est pas en cooldown."""
    with _PROVIDER_HEALTH_LOCK:
        state = _get_provider_state(provider)
        if state.cooldown_until > 0.0 and time.monotonic() < state.cooldown_until:
            return False
        if state.cooldown_until > 0.0 and time.monotonic() >= state.cooldown_until:
            # Le provider redevient testable, mais le compteur d'échecs reste
            # jusqu'au prochain succès afin d'éviter un retry storm périodique.
            state.status = "available"
            state.cooldown_until = 0.0
        return True


def _mark_provider_error(provider: str, kind: str, *, retry_after: float | None = None) -> None:
    """Met le provider en cooldown selon le kind d'erreur."""
    with _PROVIDER_HEALTH_LOCK:
        state = _get_provider_state(provider)
        state.consecutive_failures += 1
        state.last_error_kind = kind
        duration = _COOLDOWN_SECONDS.get(kind, 10.0)
        if kind == "rate_limit" and retry_after is not None:
            duration = max(duration, min(float(retry_after), 24 * 3600))
        # Retryable backend/network failures back off globally across all Nova
        # call sites.  First failure keeps historical timing; later failures
        # double deterministically up to a bounded ceiling.
        if kind in {"rate_limit", "network_error", "timeout", "inference_error", "unknown_provider_error"} and duration > 0:
            multiplier = 2 ** min(max(0, state.consecutive_failures - 1), 4)
            duration = min(duration * multiplier, 3600.0)
        if duration <= 0.0:
            state.status = "degraded"
            state.cooldown_until = 0.0
            return
        if kind == "rate_limit":
            state.status = "rate_limited"
        elif kind in ("auth_error", "model_not_found", "payment_required"):
            state.status = "offline"
        else:
            state.status = "degraded"
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + duration)


def _mark_provider_success(provider: str) -> None:
    """Réinitialise l'état du provider après un succès."""
    with _PROVIDER_HEALTH_LOCK:
        state = _get_provider_state(provider)
        state.status = "available"
        state.cooldown_until = 0.0
        state.consecutive_failures = 0
        state.last_error_kind = None


def reset_provider_health() -> None:
    """Réinitialise tout l'état de santé (utile pour les tests)."""
    with _PROVIDER_HEALTH_LOCK:
        _PROVIDER_HEALTH.clear()
    _PROVIDER_LEASES.reset()


def provider_health_snapshot():
    """Diagnostic public sans secrets pour doctor/CLI/observabilité."""
    result = {}
    names = ("cerebras", "groq", "gemini", "omniroute", "openrouter", "local")
    lease_snapshot = _PROVIDER_LEASES.snapshot()
    for name in names:
        state = _get_provider_state(name)
        cap = DEFAULT_CAPABILITIES.get(name)
        observed = lease_snapshot.get(name, {})
        result[name] = {
            "status": state.status,
            "cooldown_seconds": max(0, int(state.cooldown_until - time.monotonic())),
            "max_input_tokens": _provider_input_limit(name),
            "configured": _provider_is_configured(name),
            "local": bool(cap.local) if cap else False,
            "successes": int(observed.get("successes", 0) or 0),
            "failures": int(observed.get("failures", 0) or 0),
            "consecutive_failures": int(observed.get("consecutive_failures", 0) or 0),
            "last_error_kind": observed.get("last_error_kind"),
            "last_status_code": observed.get("last_status_code"),
            "last_latency_ms": observed.get("last_latency_ms"),
            "active_leases": int(observed.get("active_leases", 0) or 0),
            "global_consecutive_failures": state.consecutive_failures,
            "global_last_error_kind": state.last_error_kind,
        }
    return result


def _cerebras_value(value, key, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalize_openai_chat_response(response, provider):
    choices = _cerebras_value(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise ModelRouterError(
            "response_normalization_error",
            f"Réponse {provider} invalide : choices doit contenir un résultat.",
        )
    choice = choices[0]
    message = _cerebras_value(choice, "message")
    if message is None:
        raise ModelRouterError(
            "response_normalization_error",
            f"Réponse {provider} invalide : message absent.",
        )
    content = _cerebras_value(message, "content", "") or ""
    return {
        "message": {
            "role": _cerebras_value(message, "role", "assistant"),
            "content": content,
            "tool_calls": _cerebras_value(message, "tool_calls"),
        }
    }

def _messages_to_gemini_contents(messages):
    """Convertit les messages chat internes vers le format Gemini REST."""
    contents = []
    system_parts = []

    for message in messages:
        if not isinstance(message, Mapping):
            continue

        role = str(message.get("role", "user"))
        content = message.get("content", "")

        if content is None:
            content = ""

        if not isinstance(content, str):
            content = str(content)

        if role == "system":
            if content.strip():
                system_parts.append({"text": content})
            continue

        gemini_role = "model" if role == "assistant" else "user"

        contents.append(
            {
                "role": gemini_role,
                "parts": [{"text": content}],
            }
        )

    return contents, system_parts


def _provider_is_configured(provider: str) -> bool:
    if provider == "cerebras":
        return bool(os.environ.get("CEREBRAS_API_KEY"))
    if provider == "groq":
        return bool(os.environ.get("GROQ_API_KEY"))
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if provider == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    if provider == "omniroute":
        return os.environ.get("OMNIROUTE_ENABLED", "1") != "0"
    return provider == "local"


def _preflight_provider(provider: str, messages) -> int:
    estimated = estimate_message_tokens(messages)
    limit = _provider_input_limit(provider)
    if limit and estimated > limit:
        raise ModelRouterError(
            "token_limit",
            f"Requête trop volumineuse pour {provider} selon le budget configuré.",
            provider=provider,
            retryable=False,
            details={"estimated_input_tokens": estimated, "configured_limit": limit},
        )
    return estimated


def _http_error_kind(exc):
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "network_error"
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status == 402 or "payment required" in text:
        return "payment_required"
    if status == 413 or "request too large" in text or "too many tokens" in text:
        return "token_limit"
    if status == 429 or "rate limit" in text or "quota" in text:
        return "rate_limit"
    if status in (401, 403):
        return "auth_error"
    if status == 404:
        return "model_not_found"
    if status == 400:
        return "invalid_request"
    return "inference_error"


def _structured_response_format(format):
    """Translate Nova's parser schema to the OpenAI-compatible strict contract."""
    if not isinstance(format, dict):
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {
        "name": "nova_response", "strict": True, "schema": format,
    }}


def _compatible_response_format(provider, format, tools):
    """Build structured-output fields only when the request combination is supported."""
    if format is None:
        return None
    if tools and not supports_structured_output_with_tools(provider):
        return None
    return _structured_response_format(format)


def _strict_schema_compatible(schema) -> bool:
    """Check the common constrained-decoding subset without weakening backend validation."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
        return False
    properties = schema.get("properties", {})
    if isinstance(properties, dict) and any(
            not _strict_schema_compatible(value) for value in properties.values()
            if isinstance(value, dict)):
        return False
    items = schema.get("items")
    if isinstance(items, dict) and not _strict_schema_compatible(items):
        return False
    for keyword in ("anyOf", "oneOf"):
        variants = schema.get(keyword, [])
        if isinstance(variants, list) and any(
                not _strict_schema_compatible(value) for value in variants
                if isinstance(value, dict)):
            return False
    return True


def _call_openai_compatible_http(provider, base_url, model, api_key, messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS, tools=None):
    _preflight_provider(provider, messages)
    try:
        transport_messages = _openai_tool_transport_messages(messages)
    except (TypeError, ValueError) as exc:
        raise ModelRouterError(
            "invalid_request", "Contenu multimodal invalide.", provider=provider,
            retryable=False, details={"phase": "multimodal_translation"},
        ) from exc
    payload = {"model": model, "messages": transport_messages, "stream": False}
    if options and "temperature" in options:
        payload["temperature"] = options["temperature"]
    response_format = _compatible_response_format(provider, format, tools)
    if response_format is not None:
        payload["response_format"] = response_format
    if tools is not None:
        payload["tools"] = tools
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            headers=headers, json=payload, timeout=min(float(timeout_seconds), MODEL_RUNTIME_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        kind = _http_error_kind(exc)
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else getattr(exc, "status_code", None)
        retry_after = None
        if isinstance(exc, httpx.HTTPStatusError):
            raw_retry = exc.response.headers.get("Retry-After")
            try:
                retry_after = max(0.0, float(raw_retry)) if raw_retry is not None else None
            except ValueError:
                retry_after = None
        details = {"retry_after": retry_after} if retry_after is not None else {}
        if kind == "invalid_request" or status == 400:
            details["provider_diagnostic"] = _provider_request_diagnostic(
                provider, model, messages, tools, payload, exc,
            )
        raise ModelRouterError(
            kind, f"Échec du backend {provider}.", provider=provider,
            status_code=status, details=details,
        ) from exc
    return _normalize_openai_chat_response(data, provider)


def _call_openrouter_chat(messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS):
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ModelRouterError("missing_api_key", "OPENROUTER_API_KEY est absente.", provider="openrouter", retryable=False)
    model = os.environ.get("OPENROUTER_MODEL", OPENROUTER_MODEL)
    return _call_openai_compatible_http("openrouter", OPENROUTER_API_BASE, model, api_key, messages, format, options, timeout_seconds)


def _call_omniroute_chat(messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS):
    api_key = os.environ.get("OMNIROUTE_API_KEY", "")
    model = os.environ.get("OMNIROUTE_MODEL", OMNIROUTE_MODEL)
    base = os.environ.get("OMNIROUTE_API_BASE", OMNIROUTE_API_BASE)
    return _call_openai_compatible_http("omniroute", base, model, api_key, messages, format, options, timeout_seconds)


def _call_omniroute_model(model, messages, *, tools=None, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS):
    """Appelle un modele precis issu du catalogue dynamique."""
    api_key = os.environ.get("OMNIROUTE_API_KEY", "")
    base = os.environ.get("OMNIROUTE_BASE_URL") or os.environ.get("OMNIROUTE_API_BASE", OMNIROUTE_API_BASE)
    return _call_openai_compatible_http(
        "omniroute", base, model, api_key, messages, format, options,
        timeout_seconds, tools=tools,
    )


def _call_cerebras_chat(messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS):
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        raise ModelRouterError("missing_api_key", "CEREBRAS_API_KEY est absente.")
    if Cerebras is None:
        raise ModelRouterError("inference_error", "Le SDK Cerebras est indisponible.")

    _preflight_provider("cerebras", messages)
    kwargs = {"model": CEREBRAS_MODEL, "messages": messages}
    if options and "temperature" in options:
        kwargs["temperature"] = options["temperature"]
    if format is not None:
        kwargs["response_format"] = _structured_response_format(format)
    try:
        client = Cerebras(api_key=api_key, timeout=min(float(timeout_seconds), MODEL_RUNTIME_TIMEOUT_SECONDS))
        response = client.chat.completions.create(**kwargs)
    except ModelRouterError:
        raise
    except Exception as exc:
        kind = _cerebras_error_kind(exc)
        raise ModelRouterError(kind, "Échec du backend Cerebras.", provider="cerebras", status_code=getattr(exc, "status_code", None)) from exc
    return _normalize_openai_chat_response(response, "Cerebras")


def _call_groq_chat(messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS, tools=None):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ModelRouterError("missing_api_key", "GROQ_API_KEY est absente.")
    if Groq is None:
        raise ModelRouterError("inference_error", "Le SDK Groq est indisponible.")

    _preflight_provider("groq", messages)
    # Keep Nova's internal tool-result name for auditing/allowlisting, but do
    # not forward that legacy field: the modern tool message contract is
    # role + tool_call_id + content.  Sending `name` can make a valid first
    # tool call fail with HTTP 400 on the follow-up request.
    transport_messages = _openai_tool_transport_messages(messages)
    kwargs = {"model": GROQ_MODEL, "messages": transport_messages}

    if tools is not None:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    if options and "temperature" in options:
        kwargs["temperature"] = options["temperature"]

    response_format = _compatible_response_format("groq", format, tools)
    if response_format is not None:
        messages_for_groq = list(transport_messages)

        has_json_instruction = any(
            "json" in str(message.get("content", "")).lower()
            for message in messages_for_groq
            if isinstance(message, dict)
     )

        if not has_json_instruction:
            messages_for_groq = [
                {
                    "role": "system",
                    "content": "Return your response as a valid JSON object.",
                },
                *messages_for_groq,
            ]

        kwargs["messages"] = messages_for_groq
        kwargs["response_format"] = response_format

    try:
        client = Groq(api_key=api_key, timeout=min(float(timeout_seconds), MODEL_RUNTIME_TIMEOUT_SECONDS))
        response = client.chat.completions.create(**kwargs)
    except ModelRouterError:
        raise
    except Exception as exc:
        kind = _groq_error_kind(exc)
        details = {}
        if kind == "invalid_request" or getattr(exc, "status_code", None) == 400:
            details["provider_diagnostic"] = _provider_request_diagnostic(
                "groq", GROQ_MODEL, messages, tools, kwargs, exc,
            )
        raise ModelRouterError(kind, "Échec du backend Groq.", provider="groq",
                               status_code=getattr(exc, "status_code", None), details=details) from exc
    return _normalize_openai_chat_response(response, "Groq")

def _call_gemini_chat(
    messages,
    format=None,
    options=None,
    timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS,
):
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

    if not api_key:
        raise ModelRouterError(
            "missing_api_key",
            "GEMINI_API_KEY est absente.",
        )

    _preflight_provider("gemini", messages)
    contents, system_parts = _messages_to_gemini_contents(messages)

    if not contents:
        raise ModelRouterError(
            "invalid_request",
            "Gemini nécessite au moins un message utilisateur exploitable.",
        )

    payload = {
        "contents": contents,
    }

    if system_parts:
        payload["systemInstruction"] = {
            "parts": system_parts,
        }

    generation_config = {}

    if options and "temperature" in options:
        generation_config["temperature"] = options["temperature"]

    if format is not None:
        generation_config["responseMimeType"] = "application/json"
        if isinstance(format, dict):
            generation_config["responseJsonSchema"] = format

    if generation_config:
        payload["generationConfig"] = generation_config

    url = (
        f"{GEMINI_API_BASE}/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    try:
        response = httpx.post(
            url,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=min(
                float(timeout_seconds),
                MODEL_RUNTIME_TIMEOUT_SECONDS,
            ),
        )

        response.raise_for_status()
        data = response.json()

    except ModelRouterError:
        raise

    except Exception as exc:
        raise ModelRouterError(
            _gemini_error_kind(exc),
            "Échec du backend Gemini.",
        ) from exc

    candidates = data.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        raise ModelRouterError(
            "response_normalization_error",
            "Réponse Gemini invalide : candidates absent.",
        )

    candidate = candidates[0]
    content = candidate.get("content", {})
    parts = content.get("parts", [])

    text_parts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, Mapping) and part.get("text") is not None
    ]

    return {
        "message": {
            "role": "assistant",
            "content": "".join(text_parts),
            "tool_calls": None,
        }
    }


def _call_ollama_chat(model_name, messages, tools=None, think=False, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS):
    if ollama is None:
        raise ModelRouterError(
            "local_model_unavailable",
            "Le client Python Ollama n'est pas installé; les routes distantes restent disponibles.",
            provider="ollama",
            retryable=False,
        )
    kwargs = {
        "model": model_name,
        "messages": messages,
        "think": think,
    }

    if tools is not None:
        kwargs["tools"] = tools

    if format is not None:
        kwargs["format"] = format

    if options is not None:
        kwargs["options"] = options

    try:
        return ollama.chat(timeout=timeout_seconds, **kwargs)
    except TypeError:
        client = ollama.Client(timeout=timeout_seconds)
        return client.chat(**kwargs)


def _chat_once(model_name, messages, tools=None, think=False, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS):
    response = normalize_chat_response(_call_ollama_chat(
        model_name=model_name,
        messages=messages,
        tools=tools,
        think=think,
        format=format,
        options=options,
        timeout_seconds=timeout_seconds,
    ))
    message = response["message"]
    content = message.get("content") or ""
    if content.strip() or _has_valid_tool_calls(message):
        return response
    raise EmptyModelResponseError(
        "Réponse Ollama vide; nouvelle tentative pour obtenir un contenu exploitable."
    )


def _chat_local_fallback(
    messages, tools=None, think=False, format=None, options=None,
    timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS, model_name=FALLBACK_MODEL,
    model_budget=None, task_type="unknown",
):
    local_started = time.perf_counter()
    for attempt in range(2):
        try:
            remaining = float(timeout_seconds) - (time.perf_counter() - local_started)
            if remaining <= 0:
                raise ModelRouterError(
                    "timeout", "Budget temporel local épuisé.",
                    provider="local", retryable=False,
                )
            if model_budget is not None:
                model_budget.consume(role=task_type, provider="local")
            _routing_metric("calls", "local", model_name)
            return _chat_once(
                model_name=model_name,
                messages=messages,
                tools=tools,
                think=think,
                format=format,
                options=options,
                timeout_seconds=remaining,
            ), attempt + 1
        except EmptyModelResponseError:
            if model_budget is not None:
                model_budget.record_result(
                    role=task_type, provider="local", result="error",
                    failure="EMPTY_RESPONSE",
                    duration_ms=int((time.perf_counter() - local_started) * 1000),
                )
            _routing_metric("invalid_response", "local", model_name)
            if attempt == 1:
                raise
        except Exception as exc:
            if model_budget is not None:
                model_budget.record_result(
                    role=task_type, provider="local", result="error",
                    failure=type(exc).__name__,
                    duration_ms=int((time.perf_counter() - local_started) * 1000),
                )
            _routing_metric("error", "local", model_name)
            if not _is_runtime_model_error(exc):
                raise
            raise RuntimeError(f"{model_name} timeout ou erreur d'inférence après {timeout_seconds}s") from exc


def chat(
    messages,
    task_type="chat",
    tools=None,
    think=False,
    format=None,
    options=None,
    timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS,
    model_budget=None,
    required_capabilities=None,
):
    started = time.perf_counter()
    if model_budget is not None:
        model_budget.begin_logical_request()
    total_time_budget = max(0.0, min(float(timeout_seconds), MAX_TASK_MODEL_TIME_SECONDS))
    if model_budget is not None:
        total_time_budget = min(total_time_budget, model_budget.remaining_seconds())
    if total_time_budget <= 0:
        raise ModelRouterError(
            "deadline_exhausted", "Budget temporel global épuisé avant la découverte des routes.",
            retryable=False,
            details={
                "status": ErrorKind.DEADLINE_EXHAUSTED.value,
                "attempts": 0,
                "route_candidate_count": 0,
                "route_attempted_count": 0,
                "deadline_remaining_ms": 0,
                "routes_skipped": [{
                    "phase": "discovery", "result": "skipped",
                    "reason": "global_timeout_exhausted",
                }],
            },
        )
    deadline = started + total_time_budget
    explicit_capabilities = frozenset(required_capabilities or ())
    unknown_capabilities = explicit_capabilities - {"tools", "vision", "structured_output"}
    if unknown_capabilities:
        raise ValueError("unsupported required_capabilities")
    requested_capabilities = {
        "coding": task_type.startswith("developer_") or task_type in {"code", "code_light"},
        "reasoning": task_type in {
            "planning", "initial_planning", "plan_repair", "replan", "replan_repair", "review",
        },
        "tool_calling": tools is not None or "tools" in explicit_capabilities,
        "vision": "vision" in explicit_capabilities,
        "structured_output": "structured_output" in explicit_capabilities,
    }
    strict_schema_compatible = bool(
        requested_capabilities["structured_output"] and _strict_schema_compatible(format)
    )
    request_phase = "visual_analysis" if requested_capabilities["vision"] else (
        "agent_planner" if requested_capabilities["tool_calling"] else "conversation"
    )
    capability_class = "+".join(name for name, required in (
        ("tools", requested_capabilities["tool_calling"]),
        ("vision", requested_capabilities["vision"]),
        ("structured_output", requested_capabilities["structured_output"]),
    ) if required) or "text"
    cognitive_tasks = {
        "planning", "initial_planning", "plan_repair", "replan", "replan_repair",
        "developer_explore", "developer_plan", "developer_diagnose",
        "developer_patch", "review",
    }
    if tools is not None or task_type in cognitive_tasks or _provider_is_configured("omniroute"):
        remote_errors = []
        actual_model_attempts = 0
        providers = []
        attempt_history = []
        attempted_routes = set()
        omniroute_provider_dead = False
        performance_memory = None
        vision_identities = {}
        if _provider_is_configured("omniroute"):
            try:
                catalog, smart_router = _v7_components()
                role = ("planner" if task_type in {"planning", "initial_planning", "plan_repair", "replan", "replan_repair"}
                        else ("reviewer" if task_type == "review" else ("developer" if task_type.startswith("developer_") else "general")))
                previous_developer = _LAST_COGNITIVE_SELECTION.get("developer", {})
                simple_task = task_type in {"chat", "memory", "classification", "simple"}
                profile = TaskProfile.from_messages(
                    task_type,
                    messages,
                    agent_role=role,
                    reasoning_required=role in {"planner", "reviewer"},
                    tool_calling_required=requested_capabilities["tool_calling"],
                    vision_required=requested_capabilities["vision"],
                    critical=task_type == "developer_patch",
                    latency_priority=0.9 if simple_task else 0.5,
                    cost_priority=0.8 if simple_task else 0.5,
                    different_from_model=previous_developer.get("model") if role == "reviewer" else None,
                    different_from_provider=previous_developer.get("provider") if role == "reviewer" else None,
                )
                decision = smart_router.route(profile, models=catalog.models())
                performance_memory = smart_router.memory
                ranked = ([decision.selected] if decision.selected else []) + decision.alternatives
                if requested_capabilities["vision"]:
                    ranked = [candidate for candidate in ranked if _vision_candidate_is_eligible(candidate.model, task_type)]
                if requested_capabilities["tool_calling"]:
                    ranked = [candidate for candidate in ranked if candidate.model.supports_tool_calling is True]
                if requested_capabilities["structured_output"]:
                    ranked = [candidate for candidate in ranked if (
                        candidate.model.supports_strict_json_schema is True
                        if strict_schema_compatible
                        else candidate.model.supports_json_object is True
                    )]
                candidate_limit = MAX_VISION_MODEL_ATTEMPTS if requested_capabilities["vision"] else MAX_PROVIDER_ATTEMPTS
                if requested_capabilities["vision"]:
                    ranked = _select_diverse_vision_candidates(ranked, candidate_limit)
                seen_candidate_models = set()
                for candidate in ranked:
                    if candidate.model.id in seen_candidate_models:
                        continue
                    seen_candidate_models.add(candidate.model.id)
                    selected_model = candidate.model.id
                    if requested_capabilities["vision"]:
                        vision_identities[(f"omniroute:{candidate.model.provider}", selected_model)] = _vision_diversity_key(candidate.model)
                    providers.append((
                        f"omniroute:{candidate.model.provider}",
                        lambda *, messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS, _model=selected_model: _call_omniroute_model(
                            _model, messages, tools=tools, format=format, options=options, timeout_seconds=timeout_seconds,
                        ),
                        selected_model,
                        (("strict_schema" if strict_schema_compatible else "json_object")
                         if requested_capabilities["structured_output"] else None),
                    ))
                    if len(providers) >= candidate_limit:
                        break
            except Exception as exc:
                # La decouverte est une donnee externe faillible. Les providers
                # directs restent disponibles sans exposer le detail de l'erreur.
                remote_errors.append(ModelRouterError(
                    "network_error", "Decouverte OmniRoute indisponible.",
                    provider="omniroute", retryable=True,
                    details={"error_type": type(exc).__name__},
                ))

        # Groq's configured model and installed SDK implement OpenAI-compatible
        # local function calling. The other direct adapters remain text-only
        # until they forward and normalize their native tool protocols.
        direct_candidates = [
            ("cerebras", _call_cerebras_chat, CEREBRAS_MODEL, DEFAULT_CAPABILITIES["cerebras"]),
            (
                "groq",
                (lambda *, messages, format=None, options=None, timeout_seconds=MODEL_RUNTIME_TIMEOUT_SECONDS:
                 _call_groq_chat(messages, format, options, timeout_seconds, tools=tools))
                if tools is not None else _call_groq_chat,
                GROQ_MODEL, DEFAULT_CAPABILITIES["groq"],
            ),
            ("gemini", _call_gemini_chat, GEMINI_MODEL, DEFAULT_CAPABILITIES["gemini"]),
        ]
        for direct in direct_candidates:
            capability = direct[3]
            capability_fields = {"tool_calling": "tools", "coding": "coding", "reasoning": "reasoning", "vision": "vision"}
            unsupported = [
                name for name, required in requested_capabilities.items()
                if name in capability_fields and required and not getattr(capability, capability_fields[name])
            ]
            if unsupported:
                attempt_history.append({
                    "provider": direct[0], "model": direct[2], "result": "skipped",
                    "reason": ErrorKind.UNSUPPORTED_CAPABILITY.value,
                    "unsupported_capabilities": unsupported,
                })
            elif requested_capabilities["structured_output"] and not (
                    capability.strict_json_schema if strict_schema_compatible else capability.json_object):
                attempt_history.append({
                    "provider": direct[0], "model": direct[2], "result": "skipped",
                    "reason": ErrorKind.UNSUPPORTED_CAPABILITY.value,
                    "unsupported_capabilities": ["structured_output"],
                })
            elif _provider_is_configured(direct[0]):
                mode = ("strict_schema" if capability.strict_json_schema and strict_schema_compatible else
                        "json_object" if capability.json_object else None)
                providers.append((direct[0], direct[1], direct[2], mode))
            else:
                # Conserver un diagnostic de fallback, sans prétendre qu'un appel
                # modèle a eu lieu alors que le preflight sait la clé absente.
                remote_errors.append(ModelRouterError(
                    "missing_api_key", f"{direct[0]} non configuré.",
                    provider=direct[0], retryable=False,
                ))
        if tools is None and _provider_is_configured("openrouter"):
            if not requested_capabilities["vision"]:
                if not requested_capabilities["structured_output"]:
                    providers.append(("openrouter", _call_openrouter_chat,
                                      os.environ.get("OPENROUTER_MODEL", OPENROUTER_MODEL), None))

        required_route_capabilities = [
            name for name, required in (("tools", "tools" in explicit_capabilities),
                                        ("vision", requested_capabilities["vision"]),
                                        ("structured_output", requested_capabilities["structured_output"])) if required
        ]
        if required_route_capabilities and not providers:
            raise ModelRouterError("no_capable_provider", "Aucun fournisseur compatible n'est disponible.",
                                   retryable=False, details={"required_capabilities": required_route_capabilities,
                                   "route_diagnostics": attempt_history})

        if task_type == "review" and _LAST_COGNITIVE_SELECTION.get("developer"):
            previous_provider = _LAST_COGNITIVE_SELECTION["developer"].get("provider")
            providers.sort(key=lambda item: item[0] == previous_provider)

        estimated_tokens = estimate_message_tokens(messages)
        omni_entries = [item for item in providers if item[0].startswith("omniroute:")]
        direct_entries = [item for item in providers if not item[0].startswith("omniroute:")]
        direct_entries.sort(key=lambda item: -_direct_provider_score(item[0], estimated_tokens))
        providers = [*omni_entries, *direct_entries]

        route_limit = (MAX_VISION_MODEL_ATTEMPTS if requested_capabilities["vision"] else
                       MAX_STRUCTURED_MODEL_ATTEMPTS if requested_capabilities["structured_output"] else
                       MAX_MODEL_ATTEMPTS)
        bounded_providers = providers[:route_limit]
        route_candidate_count = len(bounded_providers) + 1  # local fallback
        semantic_budget_charged = False
        for route_index, (provider, provider_call, provider_model, structured_mode) in enumerate(bounded_providers):
            route_key = (provider, provider_model, structured_mode)
            health_route = f"{provider}:{provider_model}"
            provider_health = provider
            request_health = f"{health_route}:task:{task_type}"
            endpoint_health = "omniroute:endpoint" if provider.startswith("omniroute:") else None
            if route_key in attempted_routes:
                continue
            if provider.startswith("omniroute:") and omniroute_provider_dead:
                attempt_history.append({
                    "provider": provider, "model": provider_model,
                    "result": "skipped", "reason": "omniroute_provider_unavailable",
                    "deadline_remaining_ms": max(0, int((deadline - time.perf_counter()) * 1000)),
                })
                continue
            if endpoint_health and not _is_provider_available(endpoint_health):
                attempt_history.append({
                    "provider": provider, "model": provider_model,
                    "result": "skipped", "reason": "omniroute_endpoint_cooldown",
                    "cooldown_remaining": max(0.0, _get_provider_state(endpoint_health).cooldown_until - time.monotonic()),
                    "deadline_remaining_ms": max(0, int((deadline - time.perf_counter()) * 1000)),
                })
                continue
            remaining_time = deadline - time.perf_counter()
            if remaining_time <= 0:
                remote_errors.append(ModelRouterError(
                    "timeout", "Budget temporel global des modeles atteint.",
                    provider=provider, retryable=False,
                ))
                attempt_history.append({
                    "provider": provider, "model": provider_model,
                    "result": "skipped", "reason": "global_timeout_exhausted",
                    "deadline_remaining_ms": 0,
                })
                break
            attempted_routes.add(route_key)
            limit = _provider_input_limit(provider)
            if limit and estimated_tokens > limit:
                remote_errors.append(ModelRouterError(
                    "token_limit", f"{provider} ignoré: budget de contexte insuffisant.",
                    provider=provider, retryable=False,
                    details={"estimated_input_tokens": estimated_tokens, "configured_limit": limit},
                ))
                print(f"[Routeur] {provider.capitalize()} ignoré (token_limit: ~{estimated_tokens}>{limit}).")
                attempt_history.append({
                    "provider": provider, "model": provider_model,
                    "result": "skipped", "reason": "context_limit",
                    "deadline_remaining_ms": max(0, int((deadline - time.perf_counter()) * 1000)),
                })
                continue
            blocked_health = next(
                (scope for scope in (provider_health, health_route, request_health) if not _is_provider_available(scope)),
                None,
            )
            if blocked_health is not None:
                state = _get_provider_state(blocked_health)
                print(f"[Routeur] {provider.capitalize()} en cooldown ({state.status}); provider ignoré.")
                skip_error = ModelRouterError("cooldown", f"{provider} est en cooldown.")
                remote_errors.append(skip_error)
                attempt_history.append({
                    "provider": provider, "model": provider_model, "result": "skipped", "reason": "cooldown",
                    "scope": (
                        "request" if blocked_health == request_health
                        else ("model" if blocked_health == health_route else "provider")
                    ),
                    "cooldown_remaining": max(0.0, state.cooldown_until - time.monotonic()),
                    "deadline_remaining_ms": max(0, int((deadline - time.perf_counter()) * 1000)),
                })
                continue
            if not _PROVIDER_LEASES.acquire(health_route):
                remote_errors.append(ModelRouterError("cooldown", f"{provider} sans lease disponible.", provider=provider, retryable=True))
                attempt_history.append({"provider": provider, "model": provider_model, "result": "skipped", "reason": "lease_unavailable"})
                continue
            try:
                budget_before = model_budget.remaining_calls if model_budget is not None else None
                if model_budget is not None and not (
                        requested_capabilities["structured_output"] and semantic_budget_charged):
                    model_budget.consume(role=task_type, provider=provider)
                    semantic_budget_charged = True
                budget_after = model_budget.remaining_calls if model_budget is not None else None
                actual_model_attempts += 1
                _routing_metric("calls", provider, provider_model)
                call_started = time.perf_counter()
                route_timeout = _allocate_route_timeout(remaining_time, len(bounded_providers) - route_index)
                response = normalize_chat_response(provider_call(
                    messages=messages,
                    format=("json" if structured_mode == "json_object" else format),
                    options=options,
                    timeout_seconds=route_timeout,
                ))
                message = response["message"]
                if not (message.get("content") or "").strip() and not _has_valid_tool_calls(message):
                    raise ModelRouterError("invalid_response", f"Réponse {provider} vide.", provider=provider)
                _mark_provider_success(health_route)
                _mark_provider_success(provider_health)
                _mark_provider_success(request_health)
                if endpoint_health:
                    _mark_provider_success(endpoint_health)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                _PROVIDER_LEASES.mark_success(health_route, latency_ms=elapsed_ms)
                _PROVIDER_LEASES.release(health_route)
                response["_meta"] = {
                    "provider": provider,
                    "model": provider_model,
                    "attempts": actual_model_attempts,
                    "duration_ms": elapsed_ms,
                    "estimated_input_tokens": estimated_tokens,
                    "requested_capabilities": requested_capabilities,
                    "capability_class": capability_class,
                    "phase": request_phase,
                    "structured_mode": structured_mode,
                    "final_route": {"provider": provider, "model": provider_model},
                    "attempt_history": [*attempt_history, {
                        "provider": provider, "model": provider_model,
                        **({"family": "/".join(vision_identities.get(route_key[:2], ("unknown", "unknown")))}
                           if requested_capabilities["vision"] else {}),
                        "phase": request_phase, "attempt_index": actual_model_attempts,
                        "structured_mode": structured_mode,
                        "result": "success", "reason": "primary" if actual_model_attempts == 1 else "fallback",
                        "duration_ms": int((time.perf_counter() - call_started) * 1000),
                        "timeout_ms": int(route_timeout * 1000),
                        "budget_before": budget_before, "budget_after": budget_after,
                    }],
                }
                if requested_capabilities["vision"]:
                    _visual_attempt_diagnostic(
                        attempt_index=actual_model_attempts, provider=provider,
                        model=provider_model, family=vision_identities.get(route_key[:2]), result="success",
                    )
                _routing_metric("success", provider, provider_model)
                _routing_metric("primary_success" if actual_model_attempts == 1 else "fallback_success", provider, provider_model)
                if performance_memory is not None:
                    performance_memory.record(
                        provider_model, task_type, success=True,
                        latency_ms=(time.perf_counter() - call_started) * 1000,
                    )
                role_key = "reviewer" if task_type == "review" else ("developer" if task_type == "developer_patch" else ("planner" if task_type == "planning" else "general"))
                _LAST_COGNITIVE_SELECTION[role_key] = {"provider": provider, "model": provider_model}
                usage = response.get("usage", {}) if isinstance(response, dict) else {}
                _telemetry().model_call(
                    agent=role_key, provider=provider, model=provider_model,
                    attempt=actual_model_attempts, latency_ms=elapsed_ms,
                    estimated_input_tokens=estimated_tokens,
                    output_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
                    result="success",
                )
                if model_budget is not None:
                    model_budget.record_result(
                        role=task_type, provider=provider, result="success",
                        duration_ms=int((time.perf_counter() - call_started) * 1000),
                    )
                return response
            except ModelBudgetExhaustedError:
                _PROVIDER_LEASES.release(health_route)
                raise
            except ModelRouterError as error:
                _PROVIDER_LEASES.release(health_route)
                retry_after = error.details.get("retry_after") if isinstance(error.details, dict) else None
                failure_scope = _failure_health_scope(provider_health, provider_model, task_type, error)
                _mark_provider_error(failure_scope, error.kind, retry_after=retry_after)
                _PROVIDER_LEASES.mark_error(failure_scope, error.kind, status_code=error.status_code, retry_after=retry_after)
                remote_errors.append(error)
                attempt_history.append({
                    "provider": provider, "model": provider_model,
                    **({"family": "/".join(vision_identities.get(route_key[:2], ("unknown", "unknown")))}
                       if requested_capabilities["vision"] else {}),
                    "phase": request_phase, "attempt_index": actual_model_attempts,
                    "structured_mode": structured_mode,
                    "result": "error", "reason": error.error_kind.value,
                    "http_status": error.status_code,
                    "http_class": f"{error.status_code // 100}xx" if isinstance(error.status_code, int) else None,
                    "retryable": error.retryable,
                    "retry_same_model": error.policy.retry_same_model,
                    "retry_other_model": error.policy.retry_other_model,
                    "fallback_provider": error.policy.fallback_provider,
                    "failure_scope": (
                        "request" if failure_scope == request_health
                        else ("model" if failure_scope == health_route else "provider")
                    ),
                    "duration_ms": int((time.perf_counter() - call_started) * 1000),
                    "timeout_ms": int(route_timeout * 1000),
                    "budget_before": budget_before, "budget_after": budget_after,
                })
                if requested_capabilities["vision"]:
                    _visual_attempt_diagnostic(
                        attempt_index=actual_model_attempts, provider=provider,
                        model=provider_model, family=vision_identities.get(route_key[:2]), result=error.error_kind.value,
                        status_code=error.status_code,
                    )
                if model_budget is not None:
                    model_budget.record_result(
                        role=task_type, provider=provider, result="error",
                        failure=error.error_kind.value,
                        duration_ms=int((time.perf_counter() - call_started) * 1000),
                    )
                _routing_metric("error", provider, provider_model)
                _routing_metric(error.error_kind.value.casefold(), provider, provider_model)
                if performance_memory is not None:
                    performance_memory.record(
                        provider_model, task_type, success=False,
                        latency_ms=(time.perf_counter() - call_started) * 1000,
                        error_kind=error.error_kind.value,
                    )
                if provider.startswith("omniroute:") and error.error_kind in {
                    ErrorKind.CONNECTION_ERROR,
                    ErrorKind.PROVIDER_UNAVAILABLE, ErrorKind.AUTH_ERROR,
                    ErrorKind.PAYMENT_REQUIRED, ErrorKind.QUOTA_EXCEEDED,
                } | ({ErrorKind.TIMEOUT} if not requested_capabilities["vision"] else set()):
                    omniroute_provider_dead = True
                    _mark_provider_error(endpoint_health, error.kind, retry_after=retry_after)
                _telemetry().model_call(
                    agent=task_type, provider=provider, model=provider_model,
                    attempt=actual_model_attempts, latency_ms=int((time.perf_counter() - started) * 1000),
                    estimated_input_tokens=estimated_tokens, result="error",
                    error_kind=error.kind, status_code=error.status_code,
                )
                suffix = f" HTTP {error.status_code}" if error.status_code else ""
                print(f"[Routeur] {provider.capitalize()} indisponible ({error.kind}{suffix}); provider suivant.")
            except (TypeError, ValueError) as exc:
                _PROVIDER_LEASES.release(health_route)
                error = ModelRouterError(
                    "invalid_response", "Réponse modèle invalide.",
                    provider=provider, details={"error_type": type(exc).__name__},
                )
                _mark_provider_error(health_route, error.kind)
                _PROVIDER_LEASES.mark_error(health_route, error.kind)
                remote_errors.append(error)
                attempt_history.append({
                    "provider": provider, "model": provider_model,
                    "result": "error", "reason": error.error_kind.value,
                    "retryable": True,
                })
                _routing_metric("invalid_response", provider, provider_model)

        if requested_capabilities["vision"]:
            raise ModelRouterError("routes_failed", "Les routes vision compatibles n'ont produit aucune reponse.",
                                   retryable=False, details={"required_capabilities": ["vision"],
                                   "capability_class": capability_class, "phase": request_phase,
                                   "route_diagnostics": attempt_history})
        if "tools" in explicit_capabilities:
            diagnostics = [error.diagnostic() for error in remote_errors]
            raise ModelRouterError(
                "routes_failed", "Les routes compatibles outils n'ont produit aucune reponse.",
                retryable=False, details={"required_capabilities": ["tools"],
                "route_diagnostics": diagnostics, "attempt_history": attempt_history},
            )
        if requested_capabilities["structured_output"]:
            terminal = "no_capable_provider" if actual_model_attempts == 0 else "routes_failed"
            raise ModelRouterError(
                terminal,
                "Aucune route de planification structurée n'a produit de réponse.",
                retryable=False,
                details={
                    "required_capabilities": ["structured_output"],
                    "phase": "initial_goal_planning",
                    "attempt_history": attempt_history,
                    "route_diagnostics": [error.diagnostic() for error in remote_errors],
                },
            )
        local_errors = []
        try:
            remaining_time = deadline - time.perf_counter()
            if remaining_time <= 0:
                raise ModelRouterError(
                    "timeout", "Budget temporel global épuisé avant le fallback local.",
                    provider="local", retryable=False,
                )
            primary_local_model = CODE_MODEL if task_type == "developer_patch" else FALLBACK_MODEL
            local_models = [primary_local_model]
            if task_type == "developer_patch" and LIGHT_CODE_MODEL not in local_models:
                local_models.append(LIGHT_CODE_MODEL)
            local_attempts = 0
            response = None
            local_model = primary_local_model
            for local_model in local_models:
                try:
                    response, attempts = _chat_local_fallback(
                        messages=messages,
                        tools=tools,
                        think=think,
                        format=format,
                        options=options,
                        timeout_seconds=remaining_time,
                        model_name=local_model,
                        model_budget=model_budget,
                        task_type=task_type,
                    )
                    local_attempts += attempts
                    break
                except Exception as exc:
                    if not _is_runtime_model_error(exc):
                        raise
                    local_errors.append(exc)
                    local_attempts += 1
                    attempt_history.append({
                        "provider": "local", "model": local_model,
                        "result": "error", "reason": ErrorKind.LOCAL_MODEL_UNAVAILABLE.value,
                    })
            if response is None:
                raise local_errors[-1]
            response["_meta"] = {
                "provider": "local",
                "model": local_model,
                "attempts": actual_model_attempts + local_attempts,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "fallback_reason": next(
                    (
                        error.kind
                        for error in reversed(remote_errors)
                        if error.kind not in {"missing_api_key", "cooldown"}
                    ),
                    remote_errors[-1].kind,
                ),
                "fallback_reasons": [error.kind for error in remote_errors],
                "estimated_input_tokens": estimated_tokens,
                "requested_capabilities": requested_capabilities,
                "capability_class": capability_class,
                "phase": request_phase,
                "final_route": {"provider": "local", "model": local_model},
                "provider_diagnostics": [error.diagnostic() for error in remote_errors],
                "local_fallback_errors": [type(error).__name__ for error in local_errors],
                "attempt_history": [*attempt_history, {
                    "provider": "local", "model": local_model,
                    "result": "success", "reason": "local_fallback",
                }],
            }
            _routing_metric("fallback_success", "local", local_model)
            _telemetry().model_call(
                agent=task_type, provider="ollama", model=local_model,
                attempt=actual_model_attempts + local_attempts,
                latency_ms=int((time.perf_counter() - started) * 1000),
                estimated_input_tokens=estimated_tokens, result="success",
                fallback_reason=response["_meta"].get("fallback_reason"),
            )
            if model_budget is not None:
                model_budget.record_result(
                    role=task_type, provider="local", result="success",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            return response
        except Exception as local_error:
            if not _is_runtime_model_error(local_error):
                raise
            _routing_metric("all_routes_exhausted")
            diagnostics = [error.diagnostic() for error in remote_errors]
            diagnostics.append({
                "provider": "local", "kind": "local_model_unavailable",
                "error_kind": ErrorKind.LOCAL_MODEL_UNAVAILABLE.value,
                "retryable": False,
            })
            deadline_remaining_ms = max(0, int((deadline - time.perf_counter()) * 1000))
            attempted_count = actual_model_attempts + len(local_errors)
            skipped = [item for item in attempt_history if item.get("result") == "skipped"]
            terminal_kind = _classify_route_exhaustion(
                candidate_count=route_candidate_count,
                attempted_count=attempted_count,
                skipped_count=len(skipped),
                deadline_remaining_ms=deadline_remaining_ms,
            )
            raise ModelRouterError(
                terminal_kind.value.casefold(),
                f"{terminal_kind.value}: aucune route modèle n'a produit de réponse.",
                retryable=False,
                details={
                    "status": terminal_kind.value,
                    "legacy_status": ErrorKind.ALL_ROUTES_EXHAUSTED.value,
                    "attempts": attempted_count,
                    "attempt_history": attempt_history,
                    "route_diagnostics": diagnostics,
                    "route_candidate_count": route_candidate_count,
                    "route_attempted_count": attempted_count,
                    "routes_skipped": skipped,
                    "deadline_remaining_ms": deadline_remaining_ms,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    **(model_budget.snapshot() if model_budget is not None else {}),
                },
            ) from local_error

    model = choose_model(task_type)
    attempted_models = [model]
    if model != FALLBACK_MODEL and model in {CODE_MODEL, LIGHT_CODE_MODEL}:
        attempted_models.append(FALLBACK_MODEL)

    last_error = None

    for index, model_name in enumerate(attempted_models):
        empty_attempts = 0
        while True:
            print(f"[Routeur] Modèle utilisé : {model_name}")
            if index > 0:
                print(f"[Developer] {attempted_models[index - 1]} timeout après {timeout_seconds}s")
                print(f"[Developer] Escalade vers {model_name}")
            try:
                elapsed_total = time.perf_counter() - started
                remaining_time = total_time_budget if elapsed_total < 0.01 else total_time_budget - elapsed_total
                if remaining_time <= 0:
                    raise ModelRouterError(
                        "all_routes_exhausted",
                        "ALL_ROUTES_EXHAUSTED: budget temporel global épuisé.",
                        provider="local", retryable=False,
                    )
                if model_budget is not None:
                    model_budget.consume(role=task_type, provider="local")
                _routing_metric("calls", "local", model_name)
                response = _chat_once(
                    model_name=model_name,
                    messages=messages,
                    tools=tools,
                    think=think,
                    format=format,
                    options=options,
                    timeout_seconds=remaining_time,
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                response["_meta"] = {
                    "provider": "ollama",
                    "model": model_name,
                    "attempts": index + 1,
                    "duration_ms": elapsed_ms,
                }
                if model_budget is not None:
                    model_budget.record_result(
                        role=task_type, provider="local", result="success",
                        duration_ms=elapsed_ms,
                    )
                print(f"[Developer] Réponse reçue en {elapsed_ms / 1000:.1f}s")
                return response
            except EmptyModelResponseError:
                empty_attempts += 1
                if empty_attempts < 2:
                    print("[Developer] Réponse vide détectée; nouvelle tentative sur le même modèle.")
                    continue
                if model_name != attempted_models[-1]:
                    break
                raise
            except Exception as exc:
                if not _is_runtime_model_error(exc):
                    raise
                last_error = exc
                if model_name == attempted_models[-1]:
                    raise RuntimeError(f"{model_name} timeout ou erreur d'inférence après {timeout_seconds}s") from exc
                break

    if last_error is not None:
        raise RuntimeError(f"{model} timeout ou erreur d'inférence après {timeout_seconds}s") from last_error
    raise EmptyModelResponseError(
        "Réponse Ollama vide; nouvelle tentative pour obtenir un contenu exploitable."
    )


def smoke_test_providers(*, timeout_seconds: float = 20.0) -> dict[str, dict]:
    """Teste les providers configurés avec un prompt minuscule, sans exposer les secrets.

    Cette fonction n'est jamais appelée automatiquement : elle peut consommer un très
    petit quota distant. Elle sert de préflight live juste avant une campagne autonome.
    """
    prompt = [{"role": "user", "content": "Réponds uniquement OK"}]
    calls = {
        "cerebras": (_call_cerebras_chat, CEREBRAS_MODEL),
        "groq": (_call_groq_chat, GROQ_MODEL),
        "gemini": (_call_gemini_chat, GEMINI_MODEL),
        "omniroute": (_call_omniroute_chat, os.environ.get("OMNIROUTE_MODEL", OMNIROUTE_MODEL)),
        "openrouter": (_call_openrouter_chat, os.environ.get("OPENROUTER_MODEL", OPENROUTER_MODEL)),
    }
    results: dict[str, dict] = {}
    for provider, (call, model) in calls.items():
        if not _provider_is_configured(provider):
            results[provider] = {"configured": False, "reachable": False, "ok": False, "kind": "missing_api_key", "status_code": None, "retryable": False, "model": model, "latency_ms": None}
            continue
        started = time.perf_counter()
        try:
            response = normalize_chat_response(call(messages=prompt, timeout_seconds=timeout_seconds))
            content = (response.get("message", {}).get("content") or "").strip()
            ok = bool(content)
            results[provider] = {
                "configured": True,
                "reachable": True,
                "ok": ok,
                "kind": "ok" if ok else "empty_response",
                "model": model,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        except ModelRouterError as exc:
            results[provider] = {
                "configured": True,
                "reachable": exc.kind not in {"network_error", "timeout"},
                "ok": False,
                "kind": exc.kind,
                "status_code": exc.status_code,
                "retryable": exc.retryable,
                "model": model,
            }
        except Exception as exc:  # provider SDK/runtime mismatch; sanitized on purpose
            results[provider] = {
                "configured": True,
                "reachable": False,
                "ok": False,
                "kind": "unexpected_error",
                "error_type": type(exc).__name__,
                "model": model,
            }
    local = ollama_status(timeout_seconds=min(timeout_seconds, 2.0))
    results["ollama"] = {
        "configured": local.get("configured", False), "reachable": local.get("available", False),
        "ok": local.get("healthy", False), "kind": "ok" if local.get("healthy") else local.get("error_kind", "unavailable"),
        "status_code": None, "retryable": True, "model": (local.get("models") or [None])[0],
        "latency_ms": local.get("latency_ms"),
    }
    results["summary"] = {
        "configured": sum(1 for name, item in results.items() if name != "ollama" and isinstance(item, dict) and item.get("configured")),
        "healthy": sum(1 for name, item in results.items() if name != "ollama" and isinstance(item, dict) and item.get("ok")),
    }
    results["summary"]["routes_healthy"] = results["summary"]["healthy"] + int(bool(results["ollama"].get("ok")))
    results["summary"]["status"] = "READY" if results["summary"]["routes_healthy"] >= 2 else ("DEGRADED" if results["summary"]["routes_healthy"] else "NOT_READY")
    return results


# =========================
# EMBEDDINGS
# =========================

def embed(text):
    if ollama is None:
        raise ModelRouterError(
            "local_model_unavailable",
            "Le client Python Ollama n'est pas installé; embeddings locaux indisponibles.",
            provider="ollama",
            retryable=False,
        )
    response = ollama.embed(
        model=EMBED_MODEL,
        input=text,
        truncate=True
    )

    return response["embeddings"][0]
