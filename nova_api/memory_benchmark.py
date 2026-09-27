"""Small, deterministic benchmark helpers for Memory V2 retrieval quality."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .memory_store import MemoryStore


@dataclass(frozen=True)
class MemoryRecallCase:
    query: str
    expected_memory_id: str


@dataclass(frozen=True)
class MemoryRecallReport:
    total: int
    top1_hits: int
    topk_hits: int
    top1_rate: float
    topk_rate: float

    def public(self) -> dict[str, int | float]:
        return {
            "total": self.total,
            "top1_hits": self.top1_hits,
            "topk_hits": self.topk_hits,
            "top1_rate": self.top1_rate,
            "topk_rate": self.topk_rate,
        }


def evaluate_recall(store: MemoryStore, cases: Iterable[MemoryRecallCase], *, top_k: int = 5) -> MemoryRecallReport:
    values = list(cases)
    if top_k < 1:
        raise ValueError("invalid_top_k")
    top1 = topk = 0
    for case in values:
        results = store.search(case.query, limit=top_k, touch=False)
        ids = [result.item.memory_id for result in results]
        top1 += int(bool(ids) and ids[0] == case.expected_memory_id)
        topk += int(case.expected_memory_id in ids)
    total = len(values)
    return MemoryRecallReport(
        total=total,
        top1_hits=top1,
        topk_hits=topk,
        top1_rate=(top1 / total if total else 0.0),
        topk_rate=(topk / total if total else 0.0),
    )
