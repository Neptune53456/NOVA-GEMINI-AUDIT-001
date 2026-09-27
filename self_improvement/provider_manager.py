"""Provider health, leases, capabilities and routing scores.

This module intentionally stores no credentials.  It only keeps short-lived
runtime observations that help the router avoid repeating calls that are known
in advance to fail (quota, payment, token limit, cooldown, etc.).
"""

from __future__ import annotations

import time
import json
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field, asdict
from threading import RLock
from typing import Any, Callable


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ErrorKind(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTH_ERROR = "AUTH_ERROR"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    RATE_LIMIT = "RATE_LIMIT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    MISSING_API_KEY = "MISSING_API_KEY"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    TRANSIENT_SERVER_ERROR = "TRANSIENT_SERVER_ERROR"
    PERMANENT_PROVIDER_ERROR = "PERMANENT_PROVIDER_ERROR"
    LOCAL_MODEL_UNAVAILABLE = "LOCAL_MODEL_UNAVAILABLE"
    ROUTES_NOT_AVAILABLE = "ROUTES_NOT_AVAILABLE"
    ROUTES_FAILED = "ROUTES_FAILED"
    ROUTES_SKIPPED = "ROUTES_SKIPPED"
    DEADLINE_EXHAUSTED = "DEADLINE_EXHAUSTED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    ALL_ROUTES_EXHAUSTED = "ALL_ROUTES_EXHAUSTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ErrorPolicy:
    retryable: bool
    severity: str
    cooldown_seconds: float
    fallback_behavior: str

    @property
    def retry_same_model(self) -> bool:
        return self.retryable and self.fallback_behavior in {"respect_retry_after", "compress_or_larger_context"}

    @property
    def retry_other_model(self) -> bool:
        return self.fallback_behavior in {"different_model", "compress_or_larger_context"}

    @property
    def fallback_provider(self) -> bool:
        return self.fallback_behavior in {"different_provider", "respect_retry_after", "different_model"}

    @property
    def permanent_for_campaign(self) -> bool:
        return not self.retryable and self.severity in {"provider", "configuration", "permanent"}


ERROR_POLICIES: dict[ErrorKind, ErrorPolicy] = {
    ErrorKind.INVALID_REQUEST: ErrorPolicy(False, "model", 900, "different_model"),
    ErrorKind.AUTH_ERROR: ErrorPolicy(False, "provider", 3600, "different_provider"),
    ErrorKind.PAYMENT_REQUIRED: ErrorPolicy(False, "provider", 6 * 3600, "different_provider"),
    ErrorKind.AUTHORIZATION_ERROR: ErrorPolicy(False, "provider", 3600, "different_provider"),
    ErrorKind.MODEL_NOT_FOUND: ErrorPolicy(False, "model", 3600, "different_model"),
    ErrorKind.TIMEOUT: ErrorPolicy(True, "transient", 20, "different_provider"),
    ErrorKind.CONTEXT_TOO_LARGE: ErrorPolicy(True, "request", 0, "compress_or_larger_context"),
    ErrorKind.RATE_LIMIT: ErrorPolicy(True, "transient", 60, "respect_retry_after"),
    ErrorKind.UPSTREAM_ERROR: ErrorPolicy(True, "transient", 15, "different_provider"),
    ErrorKind.MISSING_API_KEY: ErrorPolicy(False, "configuration", 0, "different_provider"),
    ErrorKind.RUNTIME_ERROR: ErrorPolicy(True, "runtime", 15, "different_provider"),
    ErrorKind.NETWORK_ERROR: ErrorPolicy(True, "transient", 30, "different_provider"),
    ErrorKind.CONNECTION_ERROR: ErrorPolicy(True, "transient", 30, "different_provider"),
    ErrorKind.PROVIDER_UNAVAILABLE: ErrorPolicy(True, "provider", 30, "different_provider"),
    ErrorKind.MODEL_UNAVAILABLE: ErrorPolicy(True, "model", 60, "different_model"),
    ErrorKind.INVALID_RESPONSE: ErrorPolicy(True, "model", 15, "different_model"),
    ErrorKind.CONTEXT_LIMIT: ErrorPolicy(True, "request", 0, "compress_or_larger_context"),
    ErrorKind.QUOTA_EXCEEDED: ErrorPolicy(True, "provider", 3600, "different_provider"),
    ErrorKind.TRANSIENT_SERVER_ERROR: ErrorPolicy(True, "transient", 15, "different_provider"),
    ErrorKind.PERMANENT_PROVIDER_ERROR: ErrorPolicy(False, "permanent", 3600, "different_provider"),
    ErrorKind.LOCAL_MODEL_UNAVAILABLE: ErrorPolicy(False, "model", 30, "different_model"),
    ErrorKind.ROUTES_NOT_AVAILABLE: ErrorPolicy(False, "configuration", 0, "stop"),
    ErrorKind.ROUTES_FAILED: ErrorPolicy(False, "transient", 0, "stop"),
    ErrorKind.ROUTES_SKIPPED: ErrorPolicy(False, "transient", 0, "stop"),
    ErrorKind.DEADLINE_EXHAUSTED: ErrorPolicy(False, "transient", 0, "stop"),
    ErrorKind.UNSUPPORTED_CAPABILITY: ErrorPolicy(False, "configuration", 0, "different_provider"),
    ErrorKind.ALL_ROUTES_EXHAUSTED: ErrorPolicy(False, "permanent", 0, "stop"),
    ErrorKind.UNKNOWN: ErrorPolicy(True, "unknown", 15, "different_provider"),
}


def classify_provider_error(error: Any = None, *, status_code: int | None = None) -> tuple[ErrorKind, ErrorPolicy]:
    """Classe une erreur sans inclure son message dans les diagnostics persistants."""
    status = status_code if status_code is not None else getattr(error, "status_code", None)
    if status == 400:
        kind = ErrorKind.INVALID_REQUEST
    elif status == 401:
        kind = ErrorKind.AUTH_ERROR
    elif status == 402:
        kind = ErrorKind.PAYMENT_REQUIRED
    elif status == 403:
        kind = ErrorKind.AUTHORIZATION_ERROR
    elif status == 404:
        kind = ErrorKind.MODEL_NOT_FOUND
    elif status == 408:
        kind = ErrorKind.TIMEOUT
    elif status == 413:
        kind = ErrorKind.CONTEXT_TOO_LARGE
    elif status == 429:
        kind = ErrorKind.RATE_LIMIT
    elif isinstance(status, int) and 500 <= status <= 599:
        kind = ErrorKind.UPSTREAM_ERROR
    else:
        name = type(error).__name__.casefold() if error is not None else ""
        text = str(error or "").casefold()
        if "missing" in text and ("key" in text or "credential" in text):
            kind = ErrorKind.MISSING_API_KEY
        elif "timeout" in name or "timeout" in text:
            kind = ErrorKind.TIMEOUT
        elif "quota" in text:
            kind = ErrorKind.QUOTA_EXCEEDED
        elif "network" in name or "connect" in name:
            kind = ErrorKind.CONNECTION_ERROR
        elif error is not None:
            kind = ErrorKind.RUNTIME_ERROR
        else:
            kind = ErrorKind.UNKNOWN
    return kind, ERROR_POLICIES[kind]


@dataclass(frozen=True)
class ProviderCapability:
    name: str
    max_input_tokens: int | None = None
    # Text generation alone never implies either structured-output contract.
    strict_json_schema: bool = False
    json_object: bool = False
    tools: bool = False
    # Some OpenAI-compatible backends expose both features, but reject a
    # request that combines them. Keep the interaction explicit.
    structured_output_with_tools: bool = True
    coding: bool = False
    reasoning: bool = False
    vision: bool = False
    local: bool = False
    cost_rank: int = 5      # 1=cheap/free-preferred, larger=more expensive/unknown
    latency_rank: int = 5   # 1=fast-preferred


DEFAULT_CAPABILITIES: dict[str, ProviderCapability] = {
    "cerebras": ProviderCapability("cerebras", max_input_tokens=120_000, strict_json_schema=True, json_object=True, coding=True, reasoning=True, cost_rank=2, latency_rank=1),
    # The user's on-demand Groq tier currently reports 8k TPM.  The env override
    # lets paid tiers raise this without code changes.
    "groq": ProviderCapability("groq", max_input_tokens=6_800, strict_json_schema=True, json_object=True, tools=True, structured_output_with_tools=False, coding=True, reasoning=True, cost_rank=2, latency_rank=1),
    "gemini": ProviderCapability("gemini", max_input_tokens=1_000_000, strict_json_schema=True, json_object=True, coding=True, reasoning=True, cost_rank=3, latency_rank=2),
    "omniroute": ProviderCapability("omniroute", max_input_tokens=None, tools=True, structured_output_with_tools=False, coding=True, reasoning=True, cost_rank=1, latency_rank=2),
    "openrouter": ProviderCapability("openrouter", max_input_tokens=None, coding=True, reasoning=True, cost_rank=4, latency_rank=3),
    "local": ProviderCapability("local", max_input_tokens=None, local=True, cost_rank=1, latency_rank=5),
}


def supports_structured_output_with_tools(provider: str) -> bool:
    """Return whether one request may safely combine both protocol features."""
    capability = DEFAULT_CAPABILITIES.get(str(provider).split(":", 1)[0])
    return capability.structured_output_with_tools if capability is not None else True


@dataclass
class ProviderState:
    status: str = "available"
    cooldown_until: float = 0.0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    active_leases: int = 0
    last_error_kind: str | None = None
    last_status_code: int | None = None
    last_latency_ms: int | None = None
    circuit_state: str = CircuitState.CLOSED.value
    half_open_probe_active: bool = False


class ProviderLeaseManager:
    """Small in-memory circuit breaker + concurrency lease manager."""

    COOLDOWNS = {
        "rate_limit": 60.0,
        "payment_required": 6 * 3600.0,
        "auth_error": 3600.0,
        "network_error": 30.0,
        "timeout": 300.0,
        "inference_error": 15.0,
        "token_limit": 0.0,
        "invalid_request": 0.0,
        "model_not_found": 3600.0,
        "missing_api_key": 0.0,
        "unknown_provider_error": 15.0,
    }

    def __init__(
        self,
        *,
        max_concurrent_per_provider: int = 2,
        failure_threshold: int = 3,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent_per_provider))
        self.failure_threshold = max(1, int(failure_threshold))
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock
        self._states: dict[str, ProviderState] = {}
        self._lock = RLock()
        self._load()

    @staticmethod
    def _legacy_kind(kind: str | ErrorKind) -> str:
        value = kind.value if isinstance(kind, ErrorKind) else str(kind)
        mapping = {
            "INVALID_REQUEST": "invalid_request", "AUTH_ERROR": "auth_error",
            "PAYMENT_REQUIRED": "payment_required", "AUTHORIZATION_ERROR": "auth_error",
            "MODEL_NOT_FOUND": "model_not_found", "TIMEOUT": "timeout",
            "CONTEXT_TOO_LARGE": "token_limit", "RATE_LIMIT": "rate_limit",
            "UPSTREAM_ERROR": "inference_error", "MISSING_API_KEY": "missing_api_key",
            "RUNTIME_ERROR": "inference_error", "NETWORK_ERROR": "network_error",
            "CONNECTION_ERROR": "network_error", "PROVIDER_UNAVAILABLE": "network_error",
            "MODEL_UNAVAILABLE": "model_not_found", "INVALID_RESPONSE": "inference_error",
            "CONTEXT_LIMIT": "token_limit", "QUOTA_EXCEEDED": "rate_limit",
            "TRANSIENT_SERVER_ERROR": "inference_error", "PERMANENT_PROVIDER_ERROR": "auth_error",
            "LOCAL_MODEL_UNAVAILABLE": "inference_error", "ALL_ROUTES_EXHAUSTED": "unknown_provider_error",
            "UNSUPPORTED_CAPABILITY": "invalid_request",
            "UNKNOWN": "unknown_provider_error",
        }
        return mapping.get(value.upper(), value.casefold())

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            for name, value in raw.items():
                if isinstance(name, str) and isinstance(value, dict):
                    allowed = {key: item for key, item in value.items() if key in ProviderState.__dataclass_fields__}
                    allowed["active_leases"] = 0
                    allowed["half_open_probe_active"] = False
                    self._states[name] = ProviderState(**allowed)
        except (OSError, ValueError, TypeError):
            self._states = {}

    def _persist(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temp.write_text(json.dumps(self.snapshot(), sort_keys=True), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError:
            pass

    def state(self, provider: str) -> ProviderState:
        with self._lock:
            return self._states.setdefault(provider, ProviderState())

    def available(self, provider: str) -> bool:
        now = self.clock()
        with self._lock:
            state = self.state(provider)
            if state.cooldown_until and now >= state.cooldown_until:
                state.cooldown_until = 0.0
                if state.circuit_state == CircuitState.OPEN.value:
                    state.circuit_state = CircuitState.HALF_OPEN.value
                    state.status = "degraded"
            if state.circuit_state == CircuitState.OPEN.value:
                return False
            if state.circuit_state == CircuitState.HALF_OPEN.value and state.half_open_probe_active:
                return False
            return state.active_leases < self.max_concurrent

    def acquire(self, provider: str) -> bool:
        with self._lock:
            if not self.available(provider):
                return False
            state = self.state(provider)
            state.active_leases += 1
            if state.circuit_state == CircuitState.HALF_OPEN.value:
                state.half_open_probe_active = True
            return True

    def release(self, provider: str) -> None:
        with self._lock:
            state = self.state(provider)
            state.active_leases = max(0, state.active_leases - 1)
            if state.active_leases == 0 and state.circuit_state != CircuitState.HALF_OPEN.value:
                state.half_open_probe_active = False

    def mark_success(self, provider: str, *, latency_ms: int | None = None) -> None:
        with self._lock:
            state = self.state(provider)
            state.status = "available"
            state.cooldown_until = 0.0
            state.successes += 1
            state.consecutive_failures = 0
            state.last_error_kind = None
            state.last_status_code = None
            state.circuit_state = CircuitState.CLOSED.value
            state.half_open_probe_active = False
            if latency_ms is not None:
                state.last_latency_ms = int(latency_ms)

            self._persist()

    def mark_error(
        self,
        provider: str,
        kind: str | ErrorKind,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        with self._lock:
            legacy_kind = self._legacy_kind(kind)
            state = self.state(provider)
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error_kind = legacy_kind
            state.last_status_code = status_code
            duration = float(self.COOLDOWNS.get(legacy_kind, 15.0))
            if retry_after is not None and legacy_kind == "rate_limit":
                duration = max(duration, min(float(retry_after), 24 * 3600))
            if legacy_kind == "rate_limit":
                state.status = "rate_limited"
            elif legacy_kind in {"payment_required", "auth_error", "model_not_found"}:
                state.status = "offline"
            elif legacy_kind == "token_limit":
                state.status = "available"  # provider is healthy; this request was too large
            else:
                state.status = "degraded"
            should_open = legacy_kind in {
                "payment_required", "auth_error", "model_not_found", "rate_limit",
            } or state.consecutive_failures >= self.failure_threshold
            # Request-local protocol/schema failures must never contribute to
            # opening the shared provider circuit.
            if legacy_kind in {"invalid_request", "token_limit", "missing_api_key"}:
                should_open = False
            state.circuit_state = CircuitState.OPEN.value if should_open and duration > 0 else CircuitState.CLOSED.value
            state.half_open_probe_active = False
            state.cooldown_until = self.clock() + duration if duration > 0 else 0.0
            self._persist()

    def score(self, provider: str, capability: ProviderCapability, *, estimated_tokens: int) -> float:
        state = self.state(provider)
        if capability.max_input_tokens and estimated_tokens > capability.max_input_tokens:
            return float("-inf")
        if not self.available(provider):
            return float("-inf")
        reliability = (state.successes + 2.0) / (state.successes + state.failures + 2.0)
        failure_penalty = min(state.consecutive_failures * 12.0, 36.0)
        latency_penalty = capability.latency_rank * 2.0
        cost_penalty = capability.cost_rank * 1.5
        return reliability * 100.0 - failure_penalty - latency_penalty - cost_penalty

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
            self._persist()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {name: asdict(state) for name, state in sorted(self._states.items())}
