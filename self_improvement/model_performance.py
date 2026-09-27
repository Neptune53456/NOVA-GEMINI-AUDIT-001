"""Memoire locale bornee des performances de routage par modele et tache."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass
class ModelTaskStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rollbacks: int = 0
    tests_passed: int = 0
    latency_ema_ms: float | None = None
    recent_error_rate: float = 0.0
    timeouts: int = 0
    error_counts: dict[str, int] = field(default_factory=dict)
    quality_attempts: int = 0
    quality_successes: int = 0
    quality_failures: int = 0
    infrastructure_failures: int = 0

    @property
    def accept_rate(self) -> float:
        return self.quality_successes / self.quality_attempts if self.quality_attempts else 0.5

    @property
    def rollback_rate(self) -> float:
        return self.rollbacks / self.attempts if self.attempts else 0.0

    @property
    def test_pass_rate(self) -> float:
        return self.tests_passed / self.attempts if self.attempts else 0.5

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "accept_rate": self.accept_rate, "rollback_rate": self.rollback_rate, "test_pass_rate": self.test_pass_rate}


class ModelPerformanceMemory:
    """Statistiques non autoritatives: elles influencent le routeur, jamais le Judge."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._stats: dict[str, ModelTaskStats] = {}
        self._lock = RLock()
        self._load()

    @staticmethod
    def _key(model: str, task_type: str) -> str:
        return f"{model}\u241f{task_type.casefold()}"

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, dict):
                    allowed = {name: value[name] for name in ModelTaskStats.__dataclass_fields__ if name in value}
                    if "quality_attempts" not in value:
                        allowed["quality_attempts"] = int(value.get("attempts", 0))
                        allowed["quality_successes"] = int(value.get("successes", 0))
                        allowed["quality_failures"] = int(value.get("failures", 0))
                    self._stats[key] = ModelTaskStats(**allowed)
        except (OSError, TypeError, ValueError):
            self._stats = {}

    def _persist(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps({key: asdict(value) for key, value in self._stats.items()}, sort_keys=True), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass

    def stats(self, model: str, task_type: str) -> ModelTaskStats:
        with self._lock:
            return self._stats.setdefault(self._key(model, task_type), ModelTaskStats())

    def record(
        self,
        model: str,
        task_type: str,
        *,
        success: bool,
        latency_ms: float | None = None,
        rollback: bool = False,
        tests_passed: bool | None = None,
        error_kind: str | None = None,
    ) -> None:
        with self._lock:
            stats = self.stats(model, task_type)
            stats.attempts += 1
            normalized_error = str(error_kind or "").upper()
            infrastructure_error = normalized_error in {
                "AUTH_ERROR", "PAYMENT_REQUIRED", "AUTHORIZATION_ERROR", "TIMEOUT",
                "RATE_LIMIT", "UPSTREAM_ERROR", "TRANSIENT_SERVER_ERROR", "NETWORK_ERROR",
                "CONNECTION_ERROR", "PROVIDER_UNAVAILABLE", "QUOTA_EXCEEDED",
                "MISSING_API_KEY", "ALL_ROUTES_EXHAUSTED", "LOCAL_MODEL_UNAVAILABLE",
            }
            if success:
                stats.successes += 1
                stats.quality_attempts += 1
                stats.quality_successes += 1
            else:
                stats.failures += 1
                if infrastructure_error:
                    stats.infrastructure_failures += 1
                else:
                    stats.quality_attempts += 1
                    stats.quality_failures += 1
                if error_kind:
                    stats.error_counts[normalized_error] = stats.error_counts.get(normalized_error, 0) + 1
                    if normalized_error == "TIMEOUT":
                        stats.timeouts += 1
            if rollback:
                stats.rollbacks += 1
            if tests_passed is True:
                stats.tests_passed += 1
            if success or not infrastructure_error:
                error = 0.0 if success else 1.0
                stats.recent_error_rate = 0.8 * stats.recent_error_rate + 0.2 * error
            if latency_ms is not None:
                latency = max(0.0, float(latency_ms))
                stats.latency_ema_ms = latency if stats.latency_ema_ms is None else 0.8 * stats.latency_ema_ms + 0.2 * latency
            self._persist()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: stats.to_dict() for key, stats in self._stats.items()}
