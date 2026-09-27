"""Small restart-safe state primitives for Nova 1.1 autonomy.

Only structural fingerprints and decisions are persisted here.  Raw confirmation
secrets/tokens and arbitrary user payloads are intentionally excluded.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

ConfirmationDecision = Literal["pending", "approved", "rejected", "expired"]


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def action_fingerprint(capability_id: str, arguments: dict[str, object]) -> str:
    payload = json.dumps([capability_id, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def target_fingerprint(capability_id: str, arguments: dict[str, object]) -> str:
    target = {
        key: arguments.get(key)
        for key in ("path", "window_ref", "element_ref", "image_ref", "display_ref", "target_type")
        if key in arguments
    }
    payload = json.dumps([capability_id, target], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DurableConfirmation:
    confirmation_request_id: str
    goal_id: str
    step_id: str
    capability_id: str
    target_fingerprint: str
    effect_fingerprint: str
    created_at: str
    expires_at: str
    decision: ConfirmationDecision
    decision_at: str | None
    process_generation: str


class DurableConfirmationStore:
    """Audit-safe durable confirmation ledger.

    The actual HMAC token stays process-local in ConfirmationStore.  Pending
    records from a previous process are expired on construction so a restart can
    never silently reuse an old capability grant.
    """

    def __init__(self, path: str | Path, *, process_generation: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.process_generation = process_generation or uuid4().hex
        self._lock = RLock()
        self._initialize()
        self._expire_previous_process_pending()

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
                CREATE TABLE IF NOT EXISTS durable_confirmations (
                    confirmation_request_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    target_fingerprint TEXT NOT NULL,
                    effect_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decision_at TEXT,
                    process_generation TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS durable_confirmation_goal_idx ON durable_confirmations(goal_id)")

    def _expire_previous_process_pending(self) -> None:
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE durable_confirmations SET decision='expired', decision_at=?
                   WHERE decision='pending' AND process_generation<>?""",
                (now, self.process_generation),
            )

    def create(self, *, goal_id: str, step_id: str, capability_id: str,
               arguments: dict[str, object], ttl_seconds: float) -> DurableConfirmation:
        created = _now_dt()
        expires = created + timedelta(seconds=max(0.0, ttl_seconds))
        record = DurableConfirmation(
            confirmation_request_id=uuid4().hex,
            goal_id=goal_id,
            step_id=step_id,
            capability_id=capability_id,
            target_fingerprint=target_fingerprint(capability_id, arguments),
            effect_fingerprint=action_fingerprint(capability_id, arguments),
            created_at=created.isoformat(),
            expires_at=expires.isoformat(),
            decision="pending",
            decision_at=None,
            process_generation=self.process_generation,
        )
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO durable_confirmations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.confirmation_request_id, record.goal_id, record.step_id, record.capability_id,
                 record.target_fingerprint, record.effect_fingerprint, record.created_at, record.expires_at,
                 record.decision, record.decision_at, record.process_generation),
            )
        return record

    def decide(self, confirmation_request_id: str, *, approved: bool,
               capability_id: str, arguments: dict[str, object]) -> DurableConfirmation:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM durable_confirmations WHERE confirmation_request_id=?",
                (confirmation_request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("invalid_confirmation")
            record = self._decode(row)
            if record.decision != "pending" or record.process_generation != self.process_generation:
                raise ValueError("invalid_confirmation")
            if datetime.fromisoformat(record.expires_at) < _now_dt():
                db.execute(
                    "UPDATE durable_confirmations SET decision='expired', decision_at=? WHERE confirmation_request_id=?",
                    (_now(), confirmation_request_id),
                )
                raise ValueError("invalid_confirmation")
            if record.capability_id != capability_id or record.effect_fingerprint != action_fingerprint(capability_id, arguments):
                raise ValueError("confirmation_action_changed")
            decision: ConfirmationDecision = "approved" if approved else "rejected"
            decided_at = _now()
            db.execute(
                "UPDATE durable_confirmations SET decision=?, decision_at=? WHERE confirmation_request_id=?",
                (decision, decided_at, confirmation_request_id),
            )
            return DurableConfirmation(**{**record.__dict__, "decision": decision, "decision_at": decided_at})

    def for_goal(self, goal_id: str) -> list[DurableConfirmation]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM durable_confirmations WHERE goal_id=? ORDER BY created_at",
                (goal_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> DurableConfirmation:
        return DurableConfirmation(**dict(row))
