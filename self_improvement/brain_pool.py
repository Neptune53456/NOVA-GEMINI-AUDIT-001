"""Smart Model Router V7: profils de tache, scores explicables et failover borne."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import os
import random
import time
from typing import Any, Callable, Iterable

from self_improvement.context_budget import estimate_message_tokens, trim_text_to_token_budget
from self_improvement.model_catalog import ModelCatalog, ModelDescriptor, ModelPool
from self_improvement.model_performance import ModelPerformanceMemory
from self_improvement.provider_manager import ErrorKind, classify_provider_error


@dataclass(frozen=True)
class TaskProfile:
    task_type: str = "general"
    estimated_input_tokens: int = 0
    required_output_tokens: int = 1024
    tool_calling_required: bool = False
    reasoning_required: bool = False
    vision_required: bool = False
    latency_priority: float = 0.5
    reliability_priority: float = 0.8
    cost_priority: float = 0.5
    preferred_provider: str | None = None
    excluded_models: frozenset[str] = frozenset()
    excluded_providers: frozenset[str] = frozenset()
    critical: bool = False
    agent_role: str | None = None
    different_from_model: str | None = None
    different_from_provider: str | None = None

    @classmethod
    def from_messages(cls, task_type: str, messages: Iterable[dict[str, Any]], **kwargs: Any) -> "TaskProfile":
        return cls(task_type=task_type, estimated_input_tokens=estimate_message_tokens(messages), **kwargs)


@dataclass(frozen=True)
class RoutingWeights:
    capability: float = 30.0
    reliability: float = 22.0
    historical: float = 18.0
    context: float = 12.0
    latency: float = 8.0
    cost: float = 6.0
    provider_preference: float = 4.0
    independence: float = 8.0
    cooldown_penalty: float = 1000.0
    recent_failure_penalty: float = 20.0

    @classmethod
    def from_environment(cls) -> "RoutingWeights":
        values: dict[str, float] = {}
        for name, item in cls.__dataclass_fields__.items():
            raw = os.environ.get(f"ROUTING_WEIGHT_{name.upper()}")
            if raw is None:
                continue
            try:
                values[name] = max(0.0, float(raw))
            except ValueError:
                pass
        return cls(**values)


@dataclass
class ScoredCandidate:
    model: ModelDescriptor
    score: float
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.id,
            "provider": self.model.provider,
            "score": round(self.score, 3),
            "reason": "; ".join(self.reasons),
            "components": {key: round(value, 3) for key, value in self.components.items()},
        }


@dataclass
class RoutingDecision:
    profile: TaskProfile
    selected: ScoredCandidate | None
    alternatives: list[ScoredCandidate]
    exploration: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.profile.task_type,
            "selected": self.selected.to_dict() if self.selected else None,
            "alternatives": [item.to_dict() for item in self.alternatives[:5]],
            "exploration": self.exploration,
        }


ROLE_POOLS = {
    "planner": {ModelPool.REASONING, ModelPool.HIGH_CONTEXT},
    "developer": {ModelPool.CODING, ModelPool.TOOL_CALLING},
    "reviewer": {ModelPool.REASONING, ModelPool.RELIABLE},
}


class SmartModelRouter:
    def __init__(
        self,
        catalog: ModelCatalog,
        *,
        memory: ModelPerformanceMemory | None = None,
        weights: RoutingWeights | None = None,
        exploration_rate: float | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.catalog = catalog
        self.memory = memory or ModelPerformanceMemory()
        self.weights = weights or RoutingWeights.from_environment()
        raw_rate = exploration_rate if exploration_rate is not None else os.environ.get("MODEL_EXPLORATION_RATE", "0.05")
        try:
            self.exploration_rate = max(0.0, min(float(raw_rate), 0.25))
        except (TypeError, ValueError):
            self.exploration_rate = 0.05
        self.random = random_source or random.Random()

    @staticmethod
    def _desired_pools(profile: TaskProfile) -> set[ModelPool]:
        role = (profile.agent_role or "").casefold()
        desired = set(ROLE_POOLS.get(role, set()))
        task = profile.task_type.casefold()
        if "cod" in task or "develop" in task or "patch" in task:
            desired.add(ModelPool.CODING)
        if profile.reasoning_required or any(word in task for word in ("plan", "reason", "review", "complex")):
            desired.add(ModelPool.REASONING)
        if profile.tool_calling_required:
            desired.add(ModelPool.TOOL_CALLING)
        if profile.vision_required:
            desired.add(ModelPool.VISION)
        if profile.estimated_input_tokens >= 50_000:
            desired.add(ModelPool.HIGH_CONTEXT)
        if profile.latency_priority >= 0.75:
            desired.add(ModelPool.FAST)
        if profile.cost_priority >= 0.75:
            desired.add(ModelPool.CHEAP)
        return desired

    def score(self, model: ModelDescriptor, profile: TaskProfile) -> ScoredCandidate | None:
        if model.id in profile.excluded_models or model.provider in profile.excluded_providers:
            return None
        if profile.vision_required and model.supports_vision is False:
            return None
        if profile.tool_calling_required and model.supports_tool_calling is False:
            return None
        input_needed = max(0, profile.estimated_input_tokens)
        needed = input_needed + max(0, profile.required_output_tokens)
        context_limit = model.context_length or ((model.max_input_tokens or 0) + (model.max_output_tokens or 0)) or None
        if model.max_input_tokens and input_needed > model.max_input_tokens:
            return None
        if model.max_output_tokens and profile.required_output_tokens > model.max_output_tokens:
            return None
        if model.context_length and needed > model.context_length:
            return None

        desired = self._desired_pools(profile)
        pool_ratio = len(desired & model.pools) / len(desired) if desired else 0.6
        semantic_route = model.id.casefold()
        if any(f"best-{pool.value.replace('_', '-')}" in semantic_route for pool in desired):
            pool_ratio = min(1.15, pool_ratio + 0.15)
        if profile.reasoning_required and model.supports_reasoning is None and ModelPool.REASONING not in model.pools:
            pool_ratio *= 0.75
        reliability = max(0.0, min(model.reliability_score, 1.0))
        stats = self.memory.stats(model.id, profile.task_type)
        historical = (stats.accept_rate + stats.test_pass_rate) / 2.0
        context = 0.6 if context_limit is None else min(1.0, context_limit / max(needed * 2, 1))
        latency_ms = stats.latency_ema_ms if stats.latency_ema_ms is not None else model.latency_ms
        latency = 0.5 if latency_ms is None else 1.0 / (1.0 + max(0.0, latency_ms) / 3000.0)
        cost = 1.0 if model.free_status is True else (0.3 if model.free_status is False else 0.5)
        provider_pref = 1.0 if profile.preferred_provider and model.provider == profile.preferred_provider else 0.0
        independence = 0.0
        reasons = []
        if profile.different_from_model and model.id != profile.different_from_model:
            independence += 0.45
            reasons.append("different model from developer")
        if profile.different_from_provider and model.provider != profile.different_from_provider:
            independence += 0.55
            reasons.append("different provider from developer")
        cooldown = 1.0 if model.cooldown_until > time.time() else 0.0
        components = {
            "capability": self.weights.capability * pool_ratio,
            "reliability": self.weights.reliability * reliability * profile.reliability_priority,
            "historical": self.weights.historical * historical,
            "context": self.weights.context * context,
            "latency": self.weights.latency * latency * profile.latency_priority,
            "cost": self.weights.cost * cost * profile.cost_priority,
            "provider_preference": self.weights.provider_preference * provider_pref,
            "independence": self.weights.independence * independence,
            "cooldown_penalty": -self.weights.cooldown_penalty * cooldown,
            "recent_failure_penalty": -self.weights.recent_failure_penalty * stats.recent_error_rate,
        }
        matched = sorted(pool.value for pool in desired & model.pools)
        if matched:
            reasons.append("capabilities: " + ", ".join(matched))
        reasons.append(f"historical accept={stats.accept_rate:.2f}")
        if cooldown:
            reasons.append("cooldown active")
        return ScoredCandidate(model, sum(components.values()), reasons, components)

    def route(self, profile: TaskProfile, *, models: Iterable[ModelDescriptor] | None = None) -> RoutingDecision:
        available = list(models) if models is not None else self.catalog.models()
        ranked = [candidate for model in available if (candidate := self.score(model, profile)) is not None]
        ranked.sort(key=lambda item: (-item.score, item.model.provider, item.model.id))
        exploring = False
        if len(ranked) > 1 and not profile.critical and self.random.random() < self.exploration_rate:
            exploring = True
            # UCB simplifie: explorer d'abord un candidat peu essaye, sans ignorer le score.
            window = ranked[: min(5, len(ranked))]
            selected = max(window, key=lambda item: math.sqrt(math.log(sum(self.memory.stats(x.model.id, profile.task_type).attempts for x in window) + 2) / (self.memory.stats(item.model.id, profile.task_type).attempts + 1)))
            ranked.remove(selected)
            ranked.insert(0, selected)
        return RoutingDecision(profile, ranked[0] if ranked else None, ranked[1:], exploring)


@dataclass(frozen=True)
class FallbackLimits:
    max_model_attempts: int = 6
    max_provider_attempts: int = 3
    max_task_model_time: float = 180.0


class FallbackExecutor:
    """Execute un graphe de candidats et traite 413 par compression + retry unique."""

    def __init__(self, *, limits: FallbackLimits | None = None, memory: ModelPerformanceMemory | None = None) -> None:
        self.limits = limits or FallbackLimits()
        self.memory = memory or ModelPerformanceMemory()

    def execute(
        self,
        profile: TaskProfile,
        candidates: Iterable[ScoredCandidate],
        messages: list[dict[str, Any]],
        call: Callable[[ModelDescriptor, list[dict[str, Any]]], Any],
    ) -> tuple[Any, list[dict[str, Any]]]:
        started = time.monotonic()
        attempts: list[dict[str, Any]] = []
        provider_counts: dict[str, int] = {}
        for candidate in candidates:
            if len(attempts) >= self.limits.max_model_attempts or time.monotonic() - started > self.limits.max_task_model_time:
                break
            model = candidate.model
            if provider_counts.get(model.provider, 0) >= self.limits.max_provider_attempts:
                continue
            provider_counts[model.provider] = provider_counts.get(model.provider, 0) + 1
            call_started = time.monotonic()
            try:
                result = call(model, messages)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                response = getattr(exc, "response", None)
                if status is None and response is not None:
                    status = getattr(response, "status_code", None)
                kind, policy = classify_provider_error(exc, status_code=status)
                attempts.append({"provider": model.provider, "model": model.id, "result": "error", "error_kind": kind.value, "status_code": status, "retryable": policy.retryable})
                self.memory.record(model.id, profile.task_type, success=False, latency_ms=(time.monotonic() - call_started) * 1000)
                if kind == ErrorKind.CONTEXT_TOO_LARGE:
                    compacted = []
                    budget = max(256, int((model.max_input_tokens or model.context_length or profile.estimated_input_tokens or 2048) * 0.72))
                    for message in messages:
                        item = dict(message)
                        item["content"] = trim_text_to_token_budget(str(item.get("content", "")), budget)
                        compacted.append(item)
                    try:
                        result = call(model, compacted)
                    except Exception:
                        continue
                    attempts.append({"provider": model.provider, "model": model.id, "result": "success_after_compression"})
                    self.memory.record(model.id, profile.task_type, success=True, latency_ms=(time.monotonic() - call_started) * 1000)
                    return result, attempts
                continue
            attempts.append({"provider": model.provider, "model": model.id, "result": "success"})
            self.memory.record(model.id, profile.task_type, success=True, latency_ms=(time.monotonic() - call_started) * 1000)
            return result, attempts
        raise RuntimeError(f"model_pool_exhausted:{attempts}")
