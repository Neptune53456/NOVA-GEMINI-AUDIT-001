"""Bounded durable conversation metadata/history for Nova 1.1.

The store is intentionally small and local: at most the service-level message
budget is retained per conversation, generations are never persisted, and
confirmation secrets/tokens are excluded.
"""
from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .schemas import ConversationMessage


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_string_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item][:128]


class ConversationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    active_goal_ids_json TEXT NOT NULL,
                    active_mission_ids_json TEXT NOT NULL,
                    trimmed_message_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS conversation_messages (
                    conversation_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, ordinal),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
            if "trimmed_message_count" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN trimmed_message_count INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS conversation_message_idx ON conversation_messages(conversation_id, ordinal)")

    @classmethod
    def for_journal(cls, journal: Any) -> "ConversationStore":
        return cls(journal.path.with_name("nova_conversations.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def save(self, conversation_id: str, session: dict[str, Any], *, max_messages: int) -> None:
        messages = list(session.get("messages", []))[-max_messages:]
        goals = sorted({str(value) for value in session.get("active_goal_ids", []) if value})
        missions = sorted({str(value) for value in session.get("active_mission_ids", []) if value})
        trimmed = max(0, int(session.get("trimmed_message_count", 0) or 0))
        created_at = session["created_at"]
        last_active = session.get("last_active_at") or created_at
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO conversations (
                    conversation_id,created_at,last_active_at,status,mode,active_goal_ids_json,active_mission_ids_json,trimmed_message_count
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    last_active_at=excluded.last_active_at,
                    status=excluded.status,
                    mode=excluded.mode,
                    active_goal_ids_json=excluded.active_goal_ids_json,
                    active_mission_ids_json=excluded.active_mission_ids_json,
                    trimmed_message_count=excluded.trimmed_message_count""",
                (conversation_id, _iso(created_at), _iso(last_active), str(session.get("status", "idle")),
                 str(session.get("mode", "supervised")), json.dumps(goals), json.dumps(missions), trimmed),
            )
            db.execute("DELETE FROM conversation_messages WHERE conversation_id=?", (conversation_id,))
            for ordinal, message in enumerate(messages):
                db.execute(
                    "INSERT INTO conversation_messages VALUES (?,?,?,?,?,?)",
                    (conversation_id, ordinal, message.message_id, message.role, message.content, _iso(message.created_at)),
                )

    def load_all(self, *, limit: int, max_messages: int) -> "OrderedDict[str, dict[str, Any]]":
        sessions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM conversations ORDER BY last_active_at DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
            # Restore oldest first so OrderedDict eviction preserves LRU-ish order.
            for row in reversed(rows):
                messages = db.execute(
                    "SELECT * FROM conversation_messages WHERE conversation_id=? ORDER BY ordinal DESC LIMIT ?",
                    (row["conversation_id"], max_messages),
                ).fetchall()
                restored: list[ConversationMessage] = []
                for item in reversed(messages):
                    try:
                        restored.append(ConversationMessage(
                            message_id=item["message_id"], role=item["role"], content=item["content"],
                            created_at=_dt(item["created_at"]),
                        ))
                    except (TypeError, ValueError):
                        continue
                try:
                    created_at = _dt(row["created_at"])
                    last_active_at = _dt(row["last_active_at"])
                except (TypeError, ValueError):
                    continue
                sessions[row["conversation_id"]] = {
                    "created_at": created_at,
                    "last_active_at": last_active_at,
                    "status": "idle" if row["status"] in {"thinking", "acting", "responding", "awaiting-confirmation"} else row["status"],
                    "mode": row["mode"],
                    "messages": restored,
                    "busy": False,
                    "generation_id": None,
                    "active_goal_ids": _json_string_list(row["active_goal_ids_json"]),
                    "active_mission_ids": _json_string_list(row["active_mission_ids_json"]),
                    "trimmed_message_count": int(row["trimmed_message_count"] or 0),
                }
        return sessions

    def delete(self, conversation_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM conversation_messages WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE conversation_id=?", (conversation_id,))
