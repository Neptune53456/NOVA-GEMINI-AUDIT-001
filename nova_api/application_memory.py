"""Bounded application interaction memory.

This store keeps semantic/structural hints that can improve future target resolution.
It never authorizes an action and never replays coordinates blindly.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from pathlib import Path


@dataclass(frozen=True)
class InteractionHint:
    app_identity: str
    intent_fingerprint: str
    target_label: str
    control_type: str
    structural_fingerprint: str
    geometry: tuple[float, float, float, float] | None
    action_type: str
    success_count: int
    failure_count: int
    confidence: float
    last_success_at: str | None
    intent_similarity: float = 1.0


class ApplicationMemory:
    def __init__(self, path: str | Path, *, max_records: int = 5_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_records = max(100, int(max_records))
        self._lock = RLock()
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS interactions(
              app_identity TEXT NOT NULL,intent_fingerprint TEXT NOT NULL,target_label TEXT NOT NULL,
              control_type TEXT NOT NULL,structural_fingerprint TEXT NOT NULL,geometry_json TEXT,
              action_type TEXT NOT NULL,success_count INTEGER NOT NULL DEFAULT 0,failure_count INTEGER NOT NULL DEFAULT 0,
              confidence REAL NOT NULL DEFAULT .5,last_success_at TEXT,intent_text TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(app_identity,intent_fingerprint,structural_fingerprint,action_type))''')
            columns = {row[1] for row in db.execute("PRAGMA table_info(interactions)")}
            if "intent_text" not in columns:
                db.execute("ALTER TABLE interactions ADD COLUMN intent_text TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS interactions_app_idx ON interactions(app_identity, confidence DESC)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @staticmethod
    def app_identity(*, executable: str = "", app_name: str = "", window_class: str = "", title_family: str = "") -> str:
        parts = [x.strip().casefold()[:120] for x in (executable, app_name, window_class, title_family)]
        if not any(parts):
            return ""
        raw = "|".join(parts)
        return "app_" + hashlib.sha256(raw.encode()).hexdigest()[:20]

    @staticmethod
    def _normalize_intent(intent: str) -> str:
        normalized = "".join(
            char for char in unicodedata.normalize("NFKD", intent.casefold())
            if not unicodedata.combining(char)
        )
        return " ".join(re.findall(r"[a-z0-9_-]+", normalized))[:500]

    @classmethod
    def _terms(cls, intent: str) -> set[str]:
        ignored = {"le", "la", "les", "un", "une", "des", "de", "du", "the", "a", "an", "to", "dans", "sur"}
        return {term for term in cls._normalize_intent(intent).split() if len(term) > 1 and term not in ignored}

    @classmethod
    def intent_fingerprint(cls, intent: str) -> str:
        return hashlib.sha256(cls._normalize_intent(intent).encode()).hexdigest()[:20]

    @staticmethod
    def _recency_factor(last_success_at: str | None) -> float:
        if not last_success_at:
            return 0.75
        try:
            then = datetime.fromisoformat(last_success_at)
            age_days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            return 0.75
        # Old hints remain useful but gradually lose authority.
        return max(0.45, 1.0 / (1.0 + age_days / 45.0))

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        a, b = cls._terms(left), cls._terms(right)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def record(self, *, app_identity: str, intent: str, target_label: str, control_type: str,
               structural_fingerprint: str, action_type: str, success: bool,
               geometry: tuple[float, float, float, float] | None = None) -> None:
        if not app_identity or not structural_fingerprint:
            return
        fp = self.intent_fingerprint(intent)
        normalized_intent = self._normalize_intent(intent)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as db:
            row = db.execute(
                '''SELECT success_count,failure_count,confidence,last_success_at FROM interactions
                   WHERE app_identity=? AND intent_fingerprint=? AND structural_fingerprint=? AND action_type=?''',
                (app_identity, fp, structural_fingerprint, action_type),
            ).fetchone()
            sc, fc, _conf, last = row or (0, 0, .5, None)
            sc += int(success)
            fc += int(not success)
            # Failures reduce confidence faster than successes increase it.
            conf = max(.05, min(.98, (1 + sc) / (2 + sc + 1.5 * fc)))
            if success:
                last = now
            db.execute(
                '''INSERT INTO interactions(
                     app_identity,intent_fingerprint,target_label,control_type,structural_fingerprint,
                     geometry_json,action_type,success_count,failure_count,confidence,last_success_at,intent_text
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(app_identity,intent_fingerprint,structural_fingerprint,action_type)
                   DO UPDATE SET target_label=excluded.target_label,control_type=excluded.control_type,
                     geometry_json=excluded.geometry_json,success_count=excluded.success_count,
                     failure_count=excluded.failure_count,confidence=excluded.confidence,
                     last_success_at=excluded.last_success_at,intent_text=excluded.intent_text''',
                (app_identity, fp, target_label[:160], control_type[:80], structural_fingerprint[:160],
                 json.dumps(geometry) if geometry else None, action_type[:80], sc, fc, conf, last, normalized_intent),
            )
            count = int(db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0])
            overflow = count - self.max_records
            if overflow > 0:
                db.execute(
                    "DELETE FROM interactions WHERE rowid IN ("
                    "SELECT rowid FROM interactions ORDER BY confidence ASC, "
                    "COALESCE(last_success_at, '') ASC LIMIT ?)",
                    (overflow,),
                )

    @staticmethod
    def _hint(row: sqlite3.Row | tuple, similarity: float) -> InteractionHint:
        try:
            geom = json.loads(row[5]) if row[5] else None
            if geom is not None and (not isinstance(geom, list) or len(geom) != 4):
                geom = None
        except (json.JSONDecodeError, TypeError, ValueError):
            geom = None
        return InteractionHint(
            row[0], row[1], row[2], row[3], row[4], tuple(geom) if geom else None,
            row[6], int(row[7]), int(row[8]), float(row[9]), row[10], similarity,
        )

    def hints(self, *, app_identity: str, intent: str, limit: int = 5) -> list[InteractionHint]:
        bounded = max(1, min(limit, 20))
        fp = self.intent_fingerprint(intent)
        if not app_identity:
            return []
        with self._lock, self._connect() as db:
            exact = db.execute(
                '''SELECT app_identity,intent_fingerprint,target_label,control_type,structural_fingerprint,
                          geometry_json,action_type,success_count,failure_count,confidence,last_success_at,intent_text
                   FROM interactions WHERE app_identity=? AND intent_fingerprint=?
                   ORDER BY confidence DESC, success_count DESC LIMIT ?''',
                (app_identity, fp, bounded),
            ).fetchall()
            if exact:
                return [self._hint(row, 1.0) for row in exact]
            rows = db.execute(
                '''SELECT app_identity,intent_fingerprint,target_label,control_type,structural_fingerprint,
                          geometry_json,action_type,success_count,failure_count,confidence,last_success_at,intent_text
                   FROM interactions WHERE app_identity=?
                   ORDER BY confidence DESC, success_count DESC LIMIT 80''',
                (app_identity,),
            ).fetchall()
        ranked: list[tuple[float, tuple]] = []
        for row in rows:
            similarity = self._similarity(intent, row[11] or "")
            if similarity >= .25:
                ranked.append((similarity * float(row[9]) * self._recency_factor(row[10]), row))
        ranked.sort(key=lambda pair: (-pair[0], -float(pair[1][9])))
        return [self._hint(row, self._similarity(intent, row[11] or "")) for _score, row in ranked[:bounded]]
