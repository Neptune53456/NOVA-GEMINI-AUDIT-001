"""Persistent, sanitized event journal for Nova API generations and actions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .persistence import ensure_schema_version

DEFAULT_MAX_EVENTS = 10_000
DEFAULT_JOURNAL_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "nova_events.sqlite3"


def _safe_structural_json(value: str | None) -> dict[str, object] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class JournalEvent:
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
    status: str = "unknown"
    duration_ms: int | None = None
    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_category: str | None = None
    structural_fingerprint: dict[str, object] | None = None


class EventJournal:
    """Append-oriented SQLite journal with a fixed row-count retention policy."""

    def __init__(self, path: str | Path = DEFAULT_JOURNAL_PATH, *, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.path = Path(path)
        self.max_events = max_events
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            ensure_schema_version(connection, expected=1, component="event_journal")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    generation_id TEXT,
                    action_id TEXT,
                    transaction_id TEXT,
                    conversation_id TEXT,
                    mission_id TEXT,
                    goal_id TEXT,
                    capability_id TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER,
                    model TEXT,
                    provider TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    error_category TEXT,
                    structural_fingerprint_json TEXT
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS events_timestamp_idx ON events(sequence DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_goal_idx ON events(goal_id, sequence)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_conversation_idx ON events(conversation_id, sequence)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_mission_idx ON events(mission_id, sequence)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_action_idx ON events(action_id, sequence)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "transaction_id" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN transaction_id TEXT")
            if "mission_id" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN mission_id TEXT")
            if "goal_id" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN goal_id TEXT")
            if "structural_fingerprint_json" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN structural_fingerprint_json TEXT")

    def append(
        self,
        event_type: str,
        *,
        status: str,
        generation_id: str | None = None,
        action_id: str | None = None,
        transaction_id: str | None = None,
        conversation_id: str | None = None,
        mission_id: str | None = None,
        goal_id: str | None = None,
        capability_id: str | None = None,
        duration_ms: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_category: str | None = None,
        structural_fingerprint: dict[str, object] | None = None,
    ) -> JournalEvent:
        event = JournalEvent(
            event_id=uuid4().hex,
            timestamp=datetime.now(timezone.utc).isoformat(),
            type=event_type,
            generation_id=generation_id,
            action_id=action_id,
            transaction_id=transaction_id,
            conversation_id=conversation_id,
            mission_id=mission_id,
            goal_id=goal_id,
            capability_id=capability_id,
            status=status,
            duration_ms=duration_ms,
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_category=error_category,
            structural_fingerprint=structural_fingerprint,
        )
        values = asdict(event)
        values["structural_fingerprint_json"] = json.dumps(
            values.pop("structural_fingerprint"), sort_keys=True, separators=(",", ":")
        ) if structural_fingerprint is not None else None
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO events (
                    event_id, timestamp, type, generation_id, action_id, transaction_id, conversation_id, mission_id, goal_id,
                    capability_id, status, duration_ms, model, provider, input_tokens,
                    output_tokens, error_category, structural_fingerprint_json
                ) VALUES (
                    :event_id, :timestamp, :type, :generation_id, :action_id, :transaction_id, :conversation_id, :mission_id, :goal_id,
                    :capability_id, :status, :duration_ms, :model, :provider, :input_tokens,
                    :output_tokens, :error_category, :structural_fingerprint_json
                )""",
                values,
            )
            connection.execute(
                "DELETE FROM events WHERE sequence <= (SELECT MAX(sequence) - ? FROM events)",
                (self.max_events,),
            )
        return event

    def recent(self, *, limit: int = 50) -> list[JournalEvent]:
        bounded_limit = max(1, min(limit, 200))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT event_id, timestamp, type, generation_id, action_id, transaction_id,
                    conversation_id, mission_id, goal_id, capability_id, status, duration_ms, model, provider,
                    input_tokens, output_tokens, error_category, structural_fingerprint_json
                   FROM events ORDER BY sequence DESC LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        result = []
        for row in rows:
            values = dict(row)
            encoded = values.pop("structural_fingerprint_json", None)
            values["structural_fingerprint"] = _safe_structural_json(encoded)
            result.append(JournalEvent(**values))
        return result

    def for_goal(self, goal_id: str) -> list[JournalEvent]:
        """Return the sanitized event stream for one goal in chronological order."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT event_id, timestamp, type, generation_id, action_id, transaction_id,
                    conversation_id, mission_id, goal_id, capability_id, status, duration_ms, model, provider,
                    input_tokens, output_tokens, error_category, structural_fingerprint_json
                   FROM events WHERE goal_id = ? ORDER BY sequence ASC""",
                (goal_id,),
            ).fetchall()
        result = []
        for row in rows:
            values = dict(row)
            encoded = values.pop("structural_fingerprint_json", None)
            values["structural_fingerprint"] = _safe_structural_json(encoded)
            result.append(JournalEvent(**values))
        return result

    def for_conversation(self, conversation_id: str) -> list[JournalEvent]:
        """Return sanitized evidence for one conversation in chronological order."""
        return self._matching("conversation_id", conversation_id)

    def for_mission(self, mission_id: str) -> list[JournalEvent]:
        """Return the sanitized event stream for one mission in chronological order."""
        return self._matching("mission_id", mission_id)

    def _matching(self, column: str, value: str) -> list[JournalEvent]:
        if column not in {"conversation_id", "goal_id", "mission_id"}:
            raise ValueError("unsupported journal correlation")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""SELECT event_id, timestamp, type, generation_id, action_id, transaction_id,
                    conversation_id, mission_id, goal_id, capability_id, status, duration_ms, model, provider,
                    input_tokens, output_tokens, error_category, structural_fingerprint_json
                   FROM events WHERE {column} = ? ORDER BY sequence ASC""",
                (value,),
            ).fetchall()
        result = []
        for row in rows:
            values = dict(row)
            encoded = values.pop("structural_fingerprint_json", None)
            values["structural_fingerprint"] = _safe_structural_json(encoded)
            result.append(JournalEvent(**values))
        return result
