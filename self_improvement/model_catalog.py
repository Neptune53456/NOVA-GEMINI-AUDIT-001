"""Catalogue dynamique des modeles exposes par OmniRoute."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import os
import time
from typing import Any, Callable, Iterable, Mapping

from self_improvement.omniroute_provider import OmniRouteProvider


class ModelPool(str, Enum):
    CODING = "coding"
    REASONING = "reasoning"
    FAST = "fast"
    GENERAL = "general"
    VISION = "vision"
    FREE = "free"
    CHEAP = "cheap"
    HIGH_CONTEXT = "high_context"
    TOOL_CALLING = "tool_calling"
    RELIABLE = "reliable"


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "yes", "1", "supported"}:
            return True
        if lowered in {"false", "no", "0", "unsupported"}:
            return False
    return None


def _positive_int(*values: Any) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


@dataclass
class ModelDescriptor:
    id: str
    provider: str = "unknown"
    context_length: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tool_calling: bool | None = None
    supports_reasoning: bool | None = None
    supports_thinking: bool | None = None
    supports_vision: bool | None = None
    supports_strict_json_schema: bool | None = None
    supports_json_object: bool | None = None
    free_status: bool | None = None
    health: str = "unknown"
    latency_ms: float | None = None
    success_rate: float | None = None
    failure_rate: float | None = None
    cooldown_until: float = 0.0
    quality_score: float = 0.5
    reliability_score: float = 0.5
    pools: set[ModelPool] = field(default_factory=set)
    raw_metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["pools"] = sorted(pool.value for pool in self.pools)
        result.pop("raw_metadata", None)
        return result


def _provider_from(raw: Mapping[str, Any], model_id: str) -> str:
    for key in ("provider", "owned_by", "vendor", "source"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip() and value.casefold() not in {"system", "openai"}:
            return value.strip().casefold()
    if "/" in model_id:
        return model_id.split("/", 1)[0].casefold()
    return "omniroute"


def descriptor_from_payload(raw: Mapping[str, Any]) -> ModelDescriptor:
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        raise ValueError("model_id_missing")
    caps = raw.get("capabilities") if isinstance(raw.get("capabilities"), Mapping) else {}
    limits = raw.get("limits") if isinstance(raw.get("limits"), Mapping) else {}
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), Mapping) else {}
    free = _bool(raw.get("free"))
    if free is None:
        free = _bool(raw.get("is_free"))
    if free is None and pricing:
        numeric = []
        for value in pricing.values():
            try:
                numeric.append(float(value))
            except (TypeError, ValueError):
                pass
        free = bool(numeric) and max(numeric) == 0.0
    descriptor = ModelDescriptor(
        id=model_id,
        provider=_provider_from(raw, model_id),
        context_length=_positive_int(raw.get("context_length"), raw.get("context_window"), limits.get("context")),
        max_input_tokens=_positive_int(raw.get("max_input_tokens"), limits.get("input")),
        max_output_tokens=_positive_int(raw.get("max_output_tokens"), limits.get("output")),
        supports_tool_calling=_bool(caps.get("tool_calling", raw.get("supports_tool_calling"))),
        supports_reasoning=_bool(caps.get("reasoning", raw.get("supports_reasoning"))),
        supports_thinking=_bool(caps.get("thinking", raw.get("supports_thinking"))),
        supports_vision=_bool(caps.get("vision", raw.get("supports_vision"))),
        supports_strict_json_schema=_bool(caps.get(
            "json_schema", caps.get("strict_json_schema", raw.get("supports_json_schema"))
        )),
        supports_json_object=_bool(caps.get(
            "json_object", raw.get("supports_json_object")
        )),
        free_status=free,
        raw_metadata=dict(raw),
    )
    descriptor.pools = classify_model(descriptor)
    return descriptor


def classify_model(model: ModelDescriptor) -> set[ModelPool]:
    """Classification defensive: metadata d'abord, famille seulement en indice."""
    text = f"{model.id} {model.provider}".casefold()
    pools = {ModelPool.GENERAL}
    if model.supports_tool_calling:
        pools.add(ModelPool.TOOL_CALLING)
    if model.supports_vision or any(word in text for word in ("vision", "vl", "multimodal")):
        pools.add(ModelPool.VISION)
    if model.supports_reasoning or model.supports_thinking or any(word in text for word in ("reason", "thinking", "r1", "o1", "o3")):
        pools.add(ModelPool.REASONING)
    if any(word in text for word in ("code", "coding", "coder", "codestral", "devstral", "starcoder", "deepseek-coder")):
        pools.add(ModelPool.CODING)
    if any(word in text for word in ("flash", "mini", "small", "fast", "instant", "1.7b", "3b")):
        pools.add(ModelPool.FAST)
    limit = model.max_input_tokens or model.context_length
    if limit and limit >= 100_000:
        pools.add(ModelPool.HIGH_CONTEXT)
    if model.free_status is True:
        pools.update({ModelPool.FREE, ModelPool.CHEAP})
    if model.reliability_score >= 0.8 or (model.health == "healthy" and (model.success_rate or 0) >= 0.8):
        pools.add(ModelPool.RELIABLE)
    return pools


class ModelCatalog:
    def __init__(
        self,
        provider: OmniRouteProvider | None = None,
        *,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider or OmniRouteProvider()
        raw_ttl = ttl_seconds if ttl_seconds is not None else os.environ.get("MODEL_CATALOG_TTL_SECONDS", "300")
        try:
            self.ttl_seconds = max(1.0, float(raw_ttl))
        except (TypeError, ValueError):
            self.ttl_seconds = 300.0
        self.clock = clock
        self._models: dict[str, ModelDescriptor] = {}
        self._refreshed_at = 0.0
        self.last_error: str | None = None

    def refresh(self, *, force: bool = False) -> list[ModelDescriptor]:
        now = self.clock()
        if not force and self._models and now - self._refreshed_at < self.ttl_seconds:
            return list(self._models.values())
        try:
            parsed = []
            for item in self.provider.list_models():
                try:
                    parsed.append(descriptor_from_payload(item))
                except (TypeError, ValueError):
                    # Un enregistrement mal forme ne doit pas rendre les autres
                    # modeles indisponibles.
                    continue
        except Exception as exc:
            self.last_error = type(exc).__name__
            if self._models:
                return list(self._models.values())
            raise
        self._models = {item.id: item for item in parsed}
        self._refreshed_at = now
        self.last_error = None
        return parsed

    def models(self, *, pool: ModelPool | str | None = None, refresh: bool = True) -> list[ModelDescriptor]:
        values = self.refresh() if refresh else list(self._models.values())
        if pool is None:
            return values
        wanted = pool if isinstance(pool, ModelPool) else ModelPool(str(pool).casefold())
        return [model for model in values if wanted in model.pools]

    def pool_counts(self) -> dict[str, int]:
        return {pool.value: len(self.models(pool=pool, refresh=False)) for pool in ModelPool}
