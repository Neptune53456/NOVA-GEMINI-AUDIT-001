"""Privacy-safe normalized model usage records for Nova 1.1 observability."""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .journal import EventJournal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _first_token_value(usage: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _int_or_none(usage.get(key))
        if value is not None:
            return value
    return None


def normalize_provider_usage(usage: Any) -> tuple[int | None, int | None, int | None, str, bool]:
    """Normalize only provider-reported usage; never promote estimates to facts."""
    if not isinstance(usage, dict) or not usage:
        return None, None, None, "unavailable", False
    input_tokens = _first_token_value(usage, (
        "prompt_tokens", "input_tokens", "promptTokenCount", "inputTokenCount"
    ))
    output_tokens = _first_token_value(usage, (
        "completion_tokens", "output_tokens", "candidatesTokenCount", "outputTokenCount"
    ))
    total_tokens = _first_token_value(usage, ("total_tokens", "totalTokenCount"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    authoritative = any(value is not None for value in (input_tokens, output_tokens, total_tokens))
    return input_tokens, output_tokens, total_tokens, "provider_reported" if authoritative else "unavailable", authoritative


@dataclass(frozen=True)
class ModelUsageRecord:
    usage_id: str
    timestamp: str
    conversation_id: str | None
    mission_id: str | None
    goal_id: str | None
    plan_version: int | None
    model_call_index: int
    provider: str | None
    model: str | None
    purpose: str
    elapsed_ms: int | None
    success: bool
    failure_category: str | None
    fallback_from: str | None
    fallback_reason: str | None
    tokens_input: int | None
    tokens_output: int | None
    tokens_total: int | None
    usage_source: str
    cost_estimate: float | None
    cost_currency: str | None
    authoritative_usage: bool


class ModelUsageStore:
    """SQLite-backed structural usage ledger.

    No prompts, response bodies, tool arguments, API keys or raw provider
    payloads are stored.
    """

    def __init__(self, path: str | Path, *, max_records: int = 50_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_records = max(1_000, int(max_records))
        self._lock = RLock()
        self._initialize()

    @classmethod
    def for_journal(cls, journal: EventJournal) -> "ModelUsageStore":
        return cls(journal.path.with_name("nova_model_usage.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS model_usage (
                    usage_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    conversation_id TEXT,
                    mission_id TEXT,
                    goal_id TEXT,
                    plan_version INTEGER,
                    model_call_index INTEGER NOT NULL,
                    provider TEXT,
                    model TEXT,
                    purpose TEXT NOT NULL,
                    elapsed_ms INTEGER,
                    success INTEGER NOT NULL,
                    failure_category TEXT,
                    fallback_from TEXT,
                    fallback_reason TEXT,
                    tokens_input INTEGER,
                    tokens_output INTEGER,
                    tokens_total INTEGER,
                    usage_source TEXT NOT NULL,
                    cost_estimate REAL,
                    cost_currency TEXT,
                    authoritative_usage INTEGER NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS model_usage_goal_idx ON model_usage(goal_id, model_call_index)")
            db.execute("CREATE INDEX IF NOT EXISTS model_usage_goal_time_idx ON model_usage(goal_id, timestamp, model_call_index)")
            db.execute("CREATE INDEX IF NOT EXISTS model_usage_provider_idx ON model_usage(provider, model)")

    def record_response(self, response: dict[str, Any] | None, *, purpose: str,
                        conversation_id: str | None = None, mission_id: str | None = None,
                        goal_id: str | None = None, plan_version: int | None = None) -> list[ModelUsageRecord]:
        response = response if isinstance(response, dict) else {}
        meta = response.get("_meta") if isinstance(response.get("_meta"), dict) else {}
        history = meta.get("attempt_history") if isinstance(meta.get("attempt_history"), list) else []
        usage = response.get("usage")
        input_tokens, output_tokens, total_tokens, usage_source, authoritative = normalize_provider_usage(usage)
        attempts = [item for item in history if isinstance(item, dict)]
        if not attempts:
            attempts = [{
                "provider": meta.get("provider"), "model": meta.get("model"),
                "result": "success" if response else "error", "reason": None,
                "duration_ms": meta.get("duration_ms"),
            }]
        records: list[ModelUsageRecord] = []
        previous_provider: str | None = None
        for index, attempt in enumerate(attempts, start=1):
            success = attempt.get("result") == "success"
            is_final_success = success and index == len(attempts)
            record = ModelUsageRecord(
                usage_id=uuid4().hex,
                timestamp=_now(),
                conversation_id=conversation_id,
                mission_id=mission_id,
                goal_id=goal_id,
                plan_version=plan_version,
                model_call_index=int(attempt.get("attempt_index") or index),
                provider=str(attempt.get("provider")) if attempt.get("provider") is not None else None,
                model=str(attempt.get("model")) if attempt.get("model") is not None else None,
                purpose=purpose[:80],
                elapsed_ms=_int_or_none(attempt.get("duration_ms")),
                success=success,
                failure_category=(None if success else str(attempt.get("reason") or "provider_error")),
                fallback_from=(previous_provider if success and index > 1 else None),
                fallback_reason=(str(meta.get("fallback_reason")) if success and index > 1 and meta.get("fallback_reason") else None),
                tokens_input=(input_tokens if is_final_success else None),
                tokens_output=(output_tokens if is_final_success else None),
                tokens_total=(total_tokens if is_final_success else None),
                usage_source=(usage_source if is_final_success else "unavailable"),
                cost_estimate=None,
                cost_currency=None,
                authoritative_usage=(authoritative if is_final_success else False),
            )
            records.append(record)
            if attempt.get("provider") is not None:
                previous_provider = str(attempt.get("provider"))
        self._insert_many(records)
        return records

    def record_failure(self, *, purpose: str, error: BaseException,
                       conversation_id: str | None = None, mission_id: str | None = None,
                       goal_id: str | None = None, plan_version: int | None = None) -> list[ModelUsageRecord]:
        details = getattr(error, "details", {})
        history = details.get("attempt_history") if isinstance(details, dict) else None
        if history:
            pseudo = {"_meta": {"attempt_history": history}}
            return self.record_response(
                pseudo, purpose=purpose, conversation_id=conversation_id,
                mission_id=mission_id, goal_id=goal_id, plan_version=plan_version,
            )
        category = str(getattr(error, "kind", None) or getattr(error, "category", None) or type(error).__name__)
        record = ModelUsageRecord(
            usage_id=uuid4().hex, timestamp=_now(), conversation_id=conversation_id,
            mission_id=mission_id, goal_id=goal_id, plan_version=plan_version,
            model_call_index=1, provider=None, model=None, purpose=purpose[:80], elapsed_ms=None,
            success=False, failure_category=category, fallback_from=None, fallback_reason=None,
            tokens_input=None, tokens_output=None, tokens_total=None, usage_source="unavailable",
            cost_estimate=None, cost_currency=None, authoritative_usage=False,
        )
        self._insert(record)
        return [record]

    @staticmethod
    def _values(record: ModelUsageRecord) -> dict[str, Any]:
        values = asdict(record)
        values["success"] = int(record.success)
        values["authoritative_usage"] = int(record.authoritative_usage)
        return values

    def _insert(self, record: ModelUsageRecord) -> None:
        self._insert_many([record])

    def _insert_many(self, records: list[ModelUsageRecord]) -> None:
        if not records:
            return
        values = [self._values(record) for record in records]
        with self._lock, self._connect() as db:
            db.executemany("""INSERT INTO model_usage (
                usage_id,timestamp,conversation_id,mission_id,goal_id,plan_version,model_call_index,
                provider,model,purpose,elapsed_ms,success,failure_category,fallback_from,fallback_reason,
                tokens_input,tokens_output,tokens_total,usage_source,cost_estimate,cost_currency,authoritative_usage
            ) VALUES (
                :usage_id,:timestamp,:conversation_id,:mission_id,:goal_id,:plan_version,:model_call_index,
                :provider,:model,:purpose,:elapsed_ms,:success,:failure_category,:fallback_from,:fallback_reason,
                :tokens_input,:tokens_output,:tokens_total,:usage_source,:cost_estimate,:cost_currency,:authoritative_usage
            )""", values)
            count = int(db.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0])
            overflow = count - self.max_records
            if overflow > 0:
                db.execute(
                    "DELETE FROM model_usage WHERE usage_id IN ("
                    "SELECT usage_id FROM model_usage ORDER BY timestamp ASC LIMIT ?)",
                    (overflow,),
                )

    def for_goal(self, goal_id: str) -> list[ModelUsageRecord]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM model_usage WHERE goal_id=? ORDER BY timestamp, model_call_index", (goal_id,)).fetchall()
        return [self._decode(row) for row in rows]

    def summary(self, *, goal_id: str | None = None) -> dict[str, Any]:
        clause = " WHERE goal_id=?" if goal_id is not None else ""
        params = (goal_id,) if goal_id is not None else ()
        with self._lock, self._connect() as db:
            rows = db.execute(f"SELECT * FROM model_usage{clause}", params).fetchall()
        records = [self._decode(row) for row in rows]
        authoritative = [item for item in records if item.authoritative_usage]
        tokens = [item.tokens_total for item in authoritative if item.tokens_total is not None]
        provider_records: dict[str, list[ModelUsageRecord]] = {}
        for item in records:
            key = f"{item.provider or 'unknown'}::{item.model or 'unknown'}"
            provider_records.setdefault(key, []).append(item)
        providers: dict[str, dict[str, Any]] = {}
        for key, items in provider_records.items():
            latencies = sorted(item.elapsed_ms for item in items if item.elapsed_ms is not None)
            authoritative_tokens = [item.tokens_total for item in items if item.authoritative_usage and item.tokens_total is not None]
            bucket: dict[str, Any] = {
                "attempts": len(items),
                "successes": sum(item.success for item in items),
                "failures": sum(not item.success for item in items),
                "fallback_successes": sum(item.fallback_from is not None for item in items),
                "authoritative_tokens_total": sum(authoritative_tokens) if authoritative_tokens else None,
            }
            if len(latencies) >= 2:
                bucket["latency_median_ms"] = statistics.median(latencies)
            if len(latencies) >= 20:
                index = max(0, min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1)))))
                bucket["latency_p95_ms"] = latencies[index]
            providers[key] = bucket
        successful = [item for item in records if item.success]
        return {
            "model_calls": len(records),
            "successful_attempts": len(successful),
            "fallbacks": sum(item.fallback_from is not None for item in records),
            "authoritative_usage_records": len(authoritative),
            "authoritative_usage_coverage": (len(authoritative) / len(records) if records else 0.0),
            "authoritative_usage_coverage_successful": (len(authoritative) / len(successful) if successful else 0.0),
            "authoritative_tokens_total": (sum(tokens) if tokens else None),
            "providers": providers,
        }

    @staticmethod
    def _decode(row: sqlite3.Row) -> ModelUsageRecord:
        values = dict(row)
        values["success"] = bool(values["success"])
        values["authoritative_usage"] = bool(values["authoritative_usage"])
        return ModelUsageRecord(**values)
