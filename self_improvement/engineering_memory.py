"""Mémoire d'ingénierie persistante et compacte pour l'agent V5.

Cette mémoire ne décide jamais ACCEPT/REJECT. Elle conserve uniquement des leçons
sanitisées issues de tentatives réelles afin d'éviter de répéter les mêmes erreurs
et d'aider le Planner/Developer Agent à réutiliser les approches qui ont marché.
Le fichier de données vit sous ``.runtime`` et reste donc hors du
contexte brut du LLM ; seules des leçons bornées peuvent être réinjectées.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from self_improvement.experiment_memory import sanitize_text


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_\-]{3,}")


@dataclass(frozen=True)
class EngineeringMemoryEntry:
    entry_id: str
    timestamp: str
    task: str
    outcome: str
    lesson: str
    failure_type: str = ""
    strategy: str = ""
    files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    repo_fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngineeringMemoryEntry":
        known = cls.__dataclass_fields__
        clean = {key: value for key, value in data.items() if key in known}
        return cls(**clean)


@dataclass(frozen=True)
class MemoryHint:
    lesson: str
    outcome: str
    similarity: float
    failure_type: str = ""
    strategy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EngineeringMemory:
    """JSONL local, atomique et borné, sans donnée de validation/holdout."""

    def __init__(self, repo_root: str | Path | None = None, *, storage_path: str | Path | None = None, max_entries: int = 600):
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.storage_path = Path(storage_path).resolve() if storage_path else (
            self.repo_root / ".runtime" / "engineering_memory.jsonl"
        )
        self.max_entries = max(20, min(int(max_entries), 5000))
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token.casefold() for token in _TOKEN_RE.findall(text or "")}

    @staticmethod
    def _safe_text(text: Any, *, limit: int) -> str:
        return sanitize_text(str(text or ""))[:limit]

    def _load(self) -> list[EngineeringMemoryEntry]:
        if not self.storage_path.is_file():
            return []
        records: list[EngineeringMemoryEntry] = []
        try:
            lines = self.storage_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines[-self.max_entries :]:
            try:
                raw = json.loads(line)
                if isinstance(raw, dict) and raw.get("entry_id"):
                    records.append(EngineeringMemoryEntry.from_dict(raw))
            except Exception:
                continue
        return records[-self.max_entries :]

    def _save(self, entries: list[EngineeringMemoryEntry]) -> None:
        entries = entries[-self.max_entries :]
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.storage_path.parent,
            prefix=".engineering_memory_", suffix=".tmp", delete=False, newline="\n",
        ) as handle:
            for item in entries:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            tmp = Path(handle.name)
        try:
            os.replace(tmp, self.storage_path)
        finally:
            tmp.unlink(missing_ok=True)

    def record(
        self,
        *,
        task: str,
        outcome: str,
        lesson: str,
        failure_type: str = "",
        strategy: str = "",
        files: Iterable[str] = (),
        tests: Iterable[str] = (),
        tags: Iterable[str] = (),
        repo_fingerprint: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EngineeringMemoryEntry:
        safe_task = self._safe_text(task, limit=1800)
        safe_lesson = self._safe_text(lesson, limit=1200)
        timestamp = datetime.now(timezone.utc).isoformat()
        seed = f"{timestamp}|{safe_task}|{safe_lesson}|{outcome}"
        entry = EngineeringMemoryEntry(
            entry_id="mem_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
            timestamp=timestamp,
            task=safe_task,
            outcome=str(outcome or "UNCERTAIN").upper()[:16],
            lesson=safe_lesson,
            failure_type=self._safe_text(failure_type, limit=160),
            strategy=self._safe_text(strategy, limit=160),
            files=[self._safe_text(item, limit=260) for item in list(files)[:20]],
            tests=[self._safe_text(item, limit=260) for item in list(tests)[:20]],
            tags=[self._safe_text(item, limit=80) for item in list(tags)[:20]],
            repo_fingerprint=self._safe_text(repo_fingerprint, limit=128),
            metadata=self._sanitize_metadata(metadata or {}),
        )
        entries = self._load()
        # Dédupliquer les répétitions exactes récentes pour ne pas polluer la mémoire.
        duplicate = next((item for item in reversed(entries[-80:]) if (
            item.task == entry.task and item.lesson == entry.lesson and item.outcome == entry.outcome
        )), None)
        if duplicate is not None:
            return duplicate
        entries.append(entry)
        self._save(entries)
        return entry

    @classmethod
    def _sanitize_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:30]:
            safe_key = cls._safe_text(key, limit=80)
            if isinstance(item, (str, int, float, bool)) or item is None:
                result[safe_key] = cls._safe_text(item, limit=600) if isinstance(item, str) else item
            elif isinstance(item, list):
                result[safe_key] = [cls._safe_text(v, limit=220) for v in item[:20] if isinstance(v, (str, int, float, bool))]
        return result

    def relevant_hints(
        self,
        task: str,
        *,
        limit: int = 4,
        repo_fingerprint: str = "",
        include_rejected: bool = True,
    ) -> list[MemoryHint]:
        query = self._tokens(task)
        if not query:
            return []
        candidates: list[MemoryHint] = []
        for record in self._load():
            if not record.lesson:
                continue
            if not include_rejected and record.outcome != "ACCEPT":
                continue
            target = self._tokens(" ".join([record.task, record.lesson, record.failure_type, record.strategy, *record.tags]))
            if not target:
                continue
            overlap = len(query & target)
            union = len(query | target)
            lexical = overlap / union if union else 0.0
            exact_repo_bonus = 0.12 if repo_fingerprint and record.repo_fingerprint == repo_fingerprint else 0.0
            accept_bonus = 0.06 if record.outcome == "ACCEPT" else 0.0
            score = min(1.0, lexical + exact_repo_bonus + accept_bonus)
            if score < 0.05:
                continue
            candidates.append(MemoryHint(
                lesson=record.lesson,
                outcome=record.outcome,
                similarity=round(score, 3),
                failure_type=record.failure_type,
                strategy=record.strategy,
            ))
        candidates.sort(key=lambda item: (item.similarity, item.outcome == "ACCEPT"), reverse=True)
        return candidates[: max(1, min(int(limit), 8))]

    def stats(self) -> dict[str, Any]:
        entries = self._load()
        outcomes: dict[str, int] = {}
        for item in entries:
            outcomes[item.outcome] = outcomes.get(item.outcome, 0) + 1
        return {
            "entries": len(entries),
            "outcomes": outcomes,
            "storage": str(self.storage_path),
        }
