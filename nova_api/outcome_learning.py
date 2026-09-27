"""Bounded experience capture and retrieval on top of the authoritative MemoryStore."""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from typing import Any, Iterable
from .memory_store import MemoryRejected, MemoryStore, contains_obvious_secret

@dataclass(frozen=True)
class ExecutionOutcome:
    objective: str
    capability_id: str
    strategy: str
    success: bool
    verification: str
    failure_category: str | None = None
    recovery: str | None = None
    attempts: int = 1
    provider: str | None = None
    model: str | None = None
    lesson: str | None = None
    app_context: str | None = None

    def compact(self) -> str:
        payload = {k:v for k,v in asdict(self).items() if v not in (None, "", [], {})}
        # This is intentionally checked before serialization and again by MemoryStore.
        if contains_obvious_secret(payload):
            raise MemoryRejected("secret_like_content")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:1900]

class OutcomeLearner:
    def __init__(self, memory: MemoryStore): self.memory=memory
    def record(self, outcome: ExecutionOutcome, *, reference: str | None=None) -> bool:
        memory_type="OUTCOME" if outcome.success else "ERROR_LESSON"
        importance=7 if outcome.success else 8
        try:
            self.memory.remember(memory_type=memory_type, source_type="execution_outcome",
                provenance="DETERMINISTIC", subject=outcome.objective[:160], content=outcome.compact(),
                importance=importance, confidence=.95 if outcome.success else .9,
                source_reference=reference, tags=["experience", "verified" if outcome.success else "error-lesson"])
            return True
        except MemoryRejected:
            return False
    def relevant(self, query: str, *, limit: int=4) -> list[dict[str, Any]]:
        out=[]
        for hit in self.memory.search(query, limit=max(limit*2, limit), touch=False):
            if hit.item.memory_type not in {"OUTCOME","ERROR_LESSON"}: continue
            out.append({"type":hit.item.memory_type,"subject":hit.item.subject,
                        "content":hit.item.content[:600],"score":round(hit.score,3),
                        "provenance":hit.item.provenance})
            if len(out)>=limit: break
        return out
    def compact_context(self, query: str, *, limit: int=4, max_chars: int=1800) -> str:
        rows=self.relevant(query, limit=limit)
        text="\n".join(f"- {r['type']}: {r['content']}" for r in rows)
        return text[:max_chars]
