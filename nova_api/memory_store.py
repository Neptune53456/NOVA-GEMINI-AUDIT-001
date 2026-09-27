"""Persistent, selective and bounded memory for the local Nova runtime."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Literal, Sequence
from uuid import uuid4
from math import isfinite, sqrt

MemoryType = Literal["FACT", "PREFERENCE", "DECISION", "PROCEDURE", "PROJECT_STATE", "TASK_STATE", "OUTCOME", "ERROR_LESSON"]
Provenance = Literal["USER_STATED", "DETERMINISTIC", "MODEL_INFERRED"]
MEMORY_TYPES = frozenset({"FACT", "PREFERENCE", "DECISION", "PROCEDURE", "PROJECT_STATE", "TASK_STATE", "OUTCOME", "ERROR_LESSON"})
PROVENANCES = frozenset({"USER_STATED", "DETERMINISTIC", "MODEL_INFERRED"})
DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "nova_memory.sqlite3"
MAX_CONTENT_CHARS = 2_000
MAX_SUBJECT_CHARS = 160
MAX_TAGS = 8
MAX_CANDIDATES = 200
TOKEN = re.compile(r"[A-Za-zÀ-ÿ0-9_][A-Za-zÀ-ÿ0-9_-]{1,}")
SENSITIVE_KEY = r"(?:password|passwd|pwd|mot\s+de\s+passe|api[_ -]?key|apikey|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|authorization|bearer|client[_ -]?secret|private[_ -]?key|secret|token)"
SECRET = re.compile(
    rf"(?:[\"']?{SENSITIVE_KEY}[\"']?)\s*[:=]\s*(?![\"']?\s*(?:null|none|false|empty)?[\"']?(?:\s*[,}}]|\s*$))[^,}}\n]{{2,}}"
    r"|\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{6,}"
    r"|\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b",
    re.I | re.S,
)

def contains_obvious_secret(value: object) -> bool:
    """Fail closed for obvious credential assignments without flagging normal discussion.

    Structured JSON strings are inspected recursively before the bounded textual
    detector runs. This keeps the persistence boundary consistent for callers that
    serialize dictionaries before handing them to MemoryStore.
    """
    if value is None:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if re.fullmatch(SENSITIVE_KEY, key_text.strip(), re.I) and item not in (None, "", False):
                return True
            if contains_obvious_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_obvious_secret(item) for item in value)
    text = str(value)
    stripped = text.strip()
    if stripped[:1] in {"{", "["} and len(stripped) <= 20_000:
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed is not value and contains_obvious_secret(parsed):
            return True
    return bool(SECRET.search(text))
EXPLICIT_PATTERNS = (
    re.compile(r"^\s*souviens-toi\s+que\s+(.+)$", re.I | re.S),
    re.compile(r"^\s*retiens\s+que\s+(.+)$", re.I | re.S),
    re.compile(r"^\s*remember\s+that\s+(.+)$", re.I | re.S),
    re.compile(r"^\s*pour\s+la\s+suite,?\s+garde\s+en\s+tête\s+que\s+(.+)$", re.I | re.S),
)
CONTEXT_MARKERS = (
    "continue", "comme hier", "reprends", "precedent", "precedemment", "avant", "la derniere fois",
    "avait on decide", "avait on choisi", "on avait decide", "on avait choisi", "quelle regle",
    "quelle decision", "qu est ce qu on avait", "qu avions nous decide", "tu te rappelles",
    "tu te souviens", "que sais tu", "what do you know", "what did we decide",
    "what had we decided", "what was our decision", "what was our rule", "what did we choose",
    "do you remember", "last time", "previously",
)
TOKEN_CANONICAL = {
    "commits": "commit",
    "providers": "provider",
    "missions": "mission",
    "decisions": "decision",
    "regles": "regle",
}
DECISION_QUERY_TERMS = frozenset({"regle", "rule", "decision", "choisi", "chosen", "chose", "decide", "decided"})


class MemoryRejected(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(char for char in value if not unicodedata.combining(char)).split())


def _terms(value: str) -> list[str]:
    ignored = {"avec", "dans", "pour", "that", "this", "what", "nous", "vous", "nova", "avait", "faire", "the", "une", "des"}
    words: list[str] = []
    for token in TOKEN.findall(_normalized(value)):
        words.append(token)
        words.extend(part for part in re.split(r"[_-]+", token) if part != token)
    return list(dict.fromkeys(TOKEN_CANONICAL.get(word, word) for word in words
                              if len(word) >= 3 and word not in ignored))[:30]


def _intent_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _normalized(value)))


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    memory_type: str
    created_at: str
    updated_at: str
    source_type: str
    source_reference: str | None
    provenance: str
    subject: str
    content: str
    importance: int
    confidence: float
    status: str
    last_accessed: str | None
    access_count: int
    expires_at: str | None
    tags: tuple[str, ...]
    supersedes: str | None
    superseded_by: str | None

    def public(self, *, include_content: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "memory_id": self.memory_id, "memory_type": self.memory_type, "created_at": self.created_at,
            "updated_at": self.updated_at, "source_type": self.source_type, "provenance": self.provenance,
            "subject": self.subject, "importance": self.importance, "confidence": self.confidence,
            "status": self.status, "last_accessed": self.last_accessed, "access_count": self.access_count,
            "expires_at": self.expires_at, "tags": list(self.tags),
        }
        if include_content:
            value["content"] = self.content
        return value


@dataclass(frozen=True)
class RetrievedMemory:
    item: MemoryItem
    score: float
    reasons: tuple[str, ...]


class MemoryStore:
    def __init__(self, path: str | Path = DEFAULT_MEMORY_PATH, *,
                 embedder: Callable[[str], Sequence[float]] | None = None) -> None:
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True); self._lock = RLock()
        self._embedder = embedder
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                  memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, source_type TEXT NOT NULL, source_reference TEXT,
                  provenance TEXT NOT NULL, subject TEXT NOT NULL, subject_key TEXT NOT NULL,
                  content TEXT NOT NULL, content_key TEXT NOT NULL, importance INTEGER NOT NULL,
                  confidence REAL NOT NULL, status TEXT NOT NULL, last_accessed TEXT,
                  access_count INTEGER NOT NULL DEFAULT 0, expires_at TEXT, tags_json TEXT NOT NULL,
                  supersedes TEXT, superseded_by TEXT);
                CREATE INDEX IF NOT EXISTS idx_memory_active_subject ON memories(status, subject_key, memory_type);
                CREATE INDEX IF NOT EXISTS idx_memory_updated ON memories(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS memory_terms (
                  memory_id TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY(memory_id, term),
                  FOREIGN KEY(memory_id) REFERENCES memories(memory_id));
                CREATE INDEX IF NOT EXISTS idx_memory_terms_term ON memory_terms(term, memory_id);
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                  memory_id TEXT PRIMARY KEY, embedding_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                  FOREIGN KEY(memory_id) REFERENCES memories(memory_id));
            """)
            missing = db.execute("""SELECT memory_id, subject, content, tags_json FROM memories
                WHERE memory_id NOT IN (SELECT DISTINCT memory_id FROM memory_terms)""").fetchall()
            for row in missing:
                try:
                    decoded_tags = json.loads(row["tags_json"])
                    tags = " ".join(str(tag) for tag in decoded_tags if isinstance(tag, str)) if isinstance(decoded_tags, list) else ""
                except (json.JSONDecodeError, TypeError, ValueError):
                    tags = ""
                db.executemany("INSERT OR IGNORE INTO memory_terms(memory_id, term) VALUES (?, ?)",
                               ((row["memory_id"], term) for term in _terms(f"{row['subject']} {row['content']} {tags}")))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @property
    def semantic_enabled(self) -> bool:
        return self._embedder is not None

    @staticmethod
    def _row(row: sqlite3.Row) -> MemoryItem:
        value = dict(row)
        try:
            decoded_tags = json.loads(value["tags_json"])
            tags = tuple(str(item) for item in decoded_tags if isinstance(item, str)) if isinstance(decoded_tags, list) else ()
        except (json.JSONDecodeError, TypeError, ValueError):
            tags = ()
        return MemoryItem(value["memory_id"], value["memory_type"], value["created_at"], value["updated_at"],
            value["source_type"], value["source_reference"], value["provenance"], value["subject"], value["content"],
            value["importance"], value["confidence"], value["status"], value["last_accessed"], value["access_count"],
            value["expires_at"], tags, value["supersedes"], value["superseded_by"])

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        ln = sqrt(sum(a * a for a in left)); rn = sqrt(sum(b * b for b in right))
        return dot / (ln * rn) if ln and rn else 0.0

    def _encode(self, text: str) -> list[float] | None:
        if self._embedder is None:
            return None
        try:
            vector = [float(value) for value in self._embedder(text)]
        except Exception:
            return None
        return vector if vector and all(isfinite(value) for value in vector) else None

    def _store_embedding(self, db: sqlite3.Connection, memory_id: str, text: str) -> None:
        vector = self._encode(text)
        if vector is None:
            return
        db.execute("""INSERT INTO memory_embeddings(memory_id, embedding_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET embedding_json=excluded.embedding_json, updated_at=excluded.updated_at""",
            (memory_id, json.dumps(vector, separators=(",", ":")), _now()))

    @staticmethod
    def _normalize_expiry(expires_at: str | None) -> str | None:
        if expires_at is None:
            return None
        try:
            value = datetime.fromisoformat(expires_at)
        except (TypeError, ValueError) as error:
            raise MemoryRejected("invalid_expiry") from error
        if value.tzinfo is None:
            raise MemoryRejected("invalid_expiry")
        return value.astimezone(timezone.utc).isoformat()

    def remember(self, *, memory_type: str, source_type: str, provenance: str, subject: str, content: str,
                 importance: int = 5, confidence: float = 1.0, source_reference: str | None = None,
                 tags: list[str] | tuple[str, ...] = (), expires_at: str | None = None) -> MemoryItem:
        memory_type, provenance = memory_type.upper(), provenance.upper()
        subject, content = " ".join(subject.split()), " ".join(content.split())
        if memory_type not in MEMORY_TYPES or provenance not in PROVENANCES: raise MemoryRejected("invalid_taxonomy")
        if not subject or len(subject) > MAX_SUBJECT_CHARS or not content or len(content) > MAX_CONTENT_CHARS:
            raise MemoryRejected("invalid_memory_size")
        if contains_obvious_secret({
            "subject_text": subject, "content_text": content,
            "source_reference_text": source_reference or "", "tags_text": list(tags),
        }):
            raise MemoryRejected("secret_like_content")
        if not 1 <= importance <= 10 or not 0 <= confidence <= 1: raise MemoryRejected("invalid_weight")
        expires_at = self._normalize_expiry(expires_at)
        self.prune_expired()
        clean_tags = tuple(dict.fromkeys(_normalized(tag)[:40] for tag in tags if str(tag).strip()))[:MAX_TAGS]
        subject_key, content_key, now = _normalized(subject), _normalized(content), _now()
        with self._lock, self._connect() as db:
            duplicate = db.execute("SELECT * FROM memories WHERE status='active' AND memory_type=? AND subject_key=? AND content_key=?",
                                   (memory_type, subject_key, content_key)).fetchone()
            if duplicate:
                db.execute("UPDATE memories SET updated_at=?, importance=MAX(importance, ?), confidence=MAX(confidence, ?) WHERE memory_id=?",
                           (now, importance, confidence, duplicate["memory_id"]))
                self._store_embedding(db, duplicate["memory_id"], f"{subject} {content} {' '.join(clean_tags)}")
                return self._row(db.execute("SELECT * FROM memories WHERE memory_id=?", (duplicate["memory_id"],)).fetchone())
            previous = db.execute("SELECT * FROM memories WHERE status='active' AND memory_type=? AND subject_key=? ORDER BY updated_at DESC LIMIT 1",
                                  (memory_type, subject_key)).fetchone()
            memory_id = uuid4().hex
            supersedes = previous["memory_id"] if previous else None
            db.execute("""INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, 0, ?, ?, ?, NULL)""",
                (memory_id, memory_type, now, now, source_type[:80], source_reference[:160] if source_reference else None,
                 provenance, subject, subject_key, content, content_key, importance, confidence, expires_at,
                 json.dumps(clean_tags, ensure_ascii=False), supersedes))
            db.executemany("INSERT OR IGNORE INTO memory_terms(memory_id, term) VALUES (?, ?)",
                           ((memory_id, term) for term in _terms(f"{subject} {content} {' '.join(clean_tags)}")))
            self._store_embedding(db, memory_id, f"{subject} {content} {' '.join(clean_tags)}")
            if previous:
                db.execute("UPDATE memories SET status='superseded', superseded_by=? WHERE memory_id=?", (memory_id, previous["memory_id"]))
            return self._row(db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone())

    def prune_expired(self, *, now: str | None = None) -> int:
        """Expire active memories whose explicit retention deadline has passed."""
        cutoff = now or _now()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE memories SET status='expired', updated_at=? "
                "WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?",
                (cutoff, cutoff),
            )
            return max(0, int(cursor.rowcount or 0))

    def get(self, memory_id: str) -> MemoryItem | None:
        self.prune_expired()
        with self._connect() as db:
            row = db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None or row["status"] == "expired":
            return None
        return self._row(row)

    def touch(self, memory_ids: Sequence[str]) -> None:
        ids = tuple(dict.fromkeys(str(value) for value in memory_ids if value))[:20]
        if not ids:
            return
        now = _now()
        with self._lock, self._connect() as db:
            db.executemany(
                "UPDATE memories SET last_accessed=?, access_count=access_count+1 "
                "WHERE memory_id=? AND status='active'",
                ((now, memory_id) for memory_id in ids),
            )

    def list(self, *, limit: int = 50, include_superseded: bool = False) -> list[MemoryItem]:
        self.prune_expired()
        where = "" if include_superseded else "WHERE status='active'"
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM memories {where} ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [self._row(row) for row in rows]

    def search(self, query: str, *, limit: int = 8, touch: bool = True) -> list[RetrievedMemory]:
        self.prune_expired()
        terms = _terms(query)
        query_vector = self._encode(query)
        if not terms and query_vector is None:
            return []
        decision_query = bool(set(terms) & DECISION_QUERY_TERMS)
        with self._lock, self._connect() as db:
            rows_by_id: dict[str, sqlite3.Row] = {}
            if terms:
                placeholders = ",".join("?" for _ in terms)
                rows = db.execute(f"""SELECT memories.* FROM memories JOIN memory_terms
                    ON memory_terms.memory_id=memories.memory_id WHERE memories.status='active'
                    AND memory_terms.term IN ({placeholders}) GROUP BY memories.memory_id
                    ORDER BY COUNT(*) DESC, memories.updated_at DESC LIMIT ?""", (*terms, MAX_CANDIDATES)).fetchall()
                rows_by_id.update((row["memory_id"], row) for row in rows)
            if query_vector is not None:
                semantic_rows = db.execute("""SELECT memories.* FROM memories JOIN memory_embeddings
                    ON memory_embeddings.memory_id=memories.memory_id WHERE memories.status='active'
                    ORDER BY memories.updated_at DESC LIMIT ?""", (MAX_CANDIDATES,)).fetchall()
                rows_by_id.update((row["memory_id"], row) for row in semantic_rows)
            embedding_by_id: dict[str, Sequence[float]] = {}
            if query_vector is not None and rows_by_id:
                ids = tuple(rows_by_id)
                placeholders = ",".join("?" for _ in ids)
                embedding_rows = db.execute(
                    f"SELECT memory_id, embedding_json FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                    ids,
                ).fetchall()
                for emb_row in embedding_rows:
                    try:
                        decoded = json.loads(emb_row["embedding_json"])
                        if isinstance(decoded, list) and decoded and all(isinstance(value, (int, float)) for value in decoded):
                            embedding_by_id[emb_row["memory_id"]] = decoded
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue

            ranked: list[RetrievedMemory] = []
            for row in rows_by_id.values():
                hay_subject, hay_content = row["subject_key"], row["content_key"]
                exact_subject = [term for term in terms if term in hay_subject]
                content_matches = [term for term in terms if term in hay_content]
                reasons: list[str] = []
                score = row["importance"] * 1.5 + row["confidence"] * 4
                if exact_subject: score += len(exact_subject) * 8; reasons.append("entity/subject match")
                if content_matches: score += len(content_matches) * 3; reasons.append("lexical match")
                if row["provenance"] == "DETERMINISTIC": score += 4; reasons.append("verified source")
                elif row["provenance"] == "USER_STATED": score += 3; reasons.append("explicit user statement")
                if row["memory_type"] in {"DECISION", "TASK_STATE", "PROJECT_STATE", "ERROR_LESSON"}: score += 2
                if decision_query and row["memory_type"] in {"DECISION", "PROCEDURE"}:
                    score += 6; reasons.append("decision/rule type match")
                if row["memory_type"] in {"OUTCOME", "ERROR_LESSON"} and row["access_count"] > 0:
                    score += min(3.0, row["access_count"] * 0.25); reasons.append("experience history")
                if query_vector is not None:
                    stored_vector = embedding_by_id.get(row["memory_id"])
                    if stored_vector is not None:
                        similarity = max(0.0, self._cosine(query_vector, stored_vector))
                        if similarity > 0:
                            score += similarity * 12
                            reasons.append("semantic match")
                ranked.append(RetrievedMemory(self._row(row), score, tuple(reasons)))
            # Stable two-pass sort: score is authoritative, recency breaks ties in favour
            # of the newest durable evidence instead of accidentally preferring old rows.
            ranked.sort(key=lambda value: (value.item.updated_at, value.item.memory_id), reverse=True)
            ranked.sort(key=lambda value: value.score, reverse=True)
            selected = ranked[:max(1, min(limit, 20))]
            if touch and selected:
                now = _now(); db.executemany("UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE memory_id=?",
                                             ((now, result.item.memory_id) for result in selected))
        return selected


def explicit_memory_content(message: str) -> str | None:
    for pattern in EXPLICIT_PATTERNS:
        match = pattern.match(message)
        if match: return " ".join(match.group(1).split())
    return None


def should_retrieve_memory(message: str) -> bool:
    normalized = _intent_text(message)
    if explicit_memory_content(message): return True
    if any(marker in normalized for marker in CONTEXT_MARKERS): return True
    technical = {"architecture", "bug", "erreur", "error", "routeur", "router", "mvp", "mission", "decision", "décision", "projet", "project"}
    return bool(set(_terms(normalized)) & technical)
