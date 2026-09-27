"""Provider-neutral God Eyes domain models and normalization helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | str | int | float) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class InstrumentType(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    INDEX = "index"
    ETF = "etf"
    FX = "fx"
    COMMODITY = "commodity"


class SourceKind(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    SOCIAL = "social"
    AGGREGATOR = "aggregator"


@dataclass(frozen=True)
class NewsSourceConfig:
    source_id: str
    name: str
    url: str
    feed_type: str
    reliability_prior: float
    allowed_domains: tuple[str, ...]
    kind: SourceKind = SourceKind.SECONDARY
    official: bool = False


@dataclass(frozen=True)
class MarketInstrument:
    symbol: str
    name: str
    kind: InstrumentType
    currency: str = "USD"
    provider_symbols: dict[str, str] = field(default_factory=dict)
    entities: tuple[str, ...] = ()
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    active: bool = True
    liquidity_tier: str = "unknown"
    data_quality_tier: str = "standard"
    instrument_id: str | None = None
    session: dict[str, Any] = field(default_factory=dict)
    universe_tier: str = "core"

    def provider_symbol(self, provider: str) -> str:
        return self.provider_symbols.get(provider, self.symbol)

    @property
    def stable_instrument_id(self) -> str:
        return self.instrument_id or f"{self.kind.value}:{self.exchange or 'global'}:{self.symbol}"


@dataclass(frozen=True)
class SourceMetadata:
    provider: str
    source_url: str
    retrieved_at: datetime
    provenance: str = "public_api"


@dataclass(frozen=True)
class DataQuality:
    observed_at: datetime
    retrieved_at: datetime
    max_age_seconds: int
    complete: bool = True
    issues: tuple[str, ...] = ()

    @property
    def age_seconds(self) -> float:
        return max(0.0, (utc_now() - self.observed_at).total_seconds())

    @property
    def stale(self) -> bool:
        return self.age_seconds > self.max_age_seconds


@dataclass(frozen=True)
class MarketQuote:
    instrument: MarketInstrument
    price: float
    observed_at: datetime
    source: SourceMetadata
    quality: DataQuality
    change_percent: float | None = None


@dataclass(frozen=True)
class MarketCandle:
    instrument: MarketInstrument
    interval: str
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: SourceMetadata
    quality: DataQuality


@dataclass(frozen=True)
class NewsItem:
    item_id: str
    title: str
    url: str
    published_at: datetime
    summary: str
    source: SourceMetadata
    tickers: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()

    @staticmethod
    def stable_id(url: str, title: str) -> str:
        canonical = url.strip().lower().rstrip("/") or " ".join(title.lower().split())
        return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GodEyeEvent:
    event_id: str
    title: str
    summary: str
    event_type: str
    entities: tuple[str, ...]
    instruments: tuple[str, ...]
    source_ids: tuple[str, ...]
    published_at: datetime
    first_seen_at: datetime
    novelty_score: float
    source_quality_score: float
    confidence: dict[str, float | str]
    evidence_refs: tuple[str, ...]
    status: str = "observed"
    evidence: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class ForecastRecord:
    forecast_id: str
    instrument: str
    horizon: str
    model_id: str
    model_version: str
    feature_snapshot_hash: str
    raw_score: float
    direction: str
    feature_snapshot: dict[str, Any]
    created_at: datetime
    due_at: datetime
    status: str = "pending"
    outcome_ref: str | None = None
    probability_up: float | None = None
    probability_down: float | None = None
    calibration_quality: float | None = None
    calibration_version: str | None = None
    calibration_sample_count: int = 0
    expected_return_estimate: float | None = None
    uncertainty: float | None = None


@dataclass(frozen=True)
class EventOutcome:
    outcome_id: str
    event_id: str
    instrument: str
    calculated_at: datetime
    reactions: dict[str, dict[str, float | None]]


@dataclass(frozen=True)
class ForecastFeatureSet:
    instrument: str
    as_of: datetime
    market_momentum: float
    moving_average_short: float
    moving_average_long: float
    volatility: float
    recent_returns: tuple[float, ...]
    relative_volume: float | None
    event_type: str
    source_quality: float
    novelty: float
    llm_impact_direction: str
    llm_interpretation_confidence: float
    market_regime: str = "unclassified"
    similar_historical_reactions: tuple[str, ...] = ()
    local_regime: str = "unknown"
    global_regime: str = "unknown"
    similar_event_count: int = 0
    similar_return_mean: float | None = None
    similar_return_median: float | None = None
    similar_positive_ratio: float | None = None
    similar_downside: float | None = None
    similarity_confidence: float = 0.0
    quantitative_features: dict[str, Any] = field(default_factory=dict)
    patterns: tuple[dict[str, Any], ...] = ()
    multi_timeframe_context: dict[str, Any] = field(default_factory=dict)
    microstructure: dict[str, Any] = field(default_factory=dict)
    intelligence_quality: float | None = None


def public(value: Any) -> Any:
    """Convert domain values to JSON-safe structures without losing UTC offsets."""
    if isinstance(value, DataQuality):
        result = asdict(value)
        result.update(age_seconds=value.age_seconds, stale=value.stale)
        return public(result)
    if hasattr(value, "__dataclass_fields__"):
        return {item.name: public(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public(item) for item in value]
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value
