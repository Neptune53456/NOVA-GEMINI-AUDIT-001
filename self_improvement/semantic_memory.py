"""Index sémantique local léger au-dessus d'EngineeringMemory, sans dépendance externe.

V6 utilise TF-IDF/cosinus déterministe pour retrouver des expériences proches. Une
implémentation embeddings pourra remplacer ce backend derrière la même façade.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import math, re
from collections import Counter
from typing import Iterable
from self_improvement.engineering_memory import EngineeringMemory, EngineeringMemoryEntry

TOKENS = re.compile(r"[A-Za-zÀ-ÿ0-9_\-]{3,}")
@dataclass(frozen=True)
class SemanticHit:
    entry_id: str; lesson: str; outcome: str; score: float; strategy: str = ""
    def to_dict(self): return asdict(self)

class SemanticEngineeringMemory:
    def __init__(self, memory: EngineeringMemory): self.memory = memory
    @staticmethod
    def _tf(text): return Counter(t.casefold() for t in TOKENS.findall(text or ""))
    def search(self, query: str, *, limit: int = 5, include_rejected: bool = True) -> list[SemanticHit]:
        entries = self.memory._load()  # façade trusted autour du stockage borné existant
        if not entries: return []
        docs = [self._tf(" ".join([e.task,e.lesson,e.failure_type,e.strategy,*e.tags])) for e in entries]
        q = self._tf(query)
        if not q: return []
        df = Counter()
        for d in docs:
            for t in d: df[t] += 1
        n = len(docs)
        def vec(tf): return {t: c*(math.log((n+1)/(df[t]+1))+1.0) for t,c in tf.items()}
        qv=vec(q); qn=math.sqrt(sum(v*v for v in qv.values())) or 1.0
        hits=[]
        for e,d in zip(entries,docs):
            if not include_rejected and e.outcome != "ACCEPT": continue
            dv=vec(d); dn=math.sqrt(sum(v*v for v in dv.values())) or 1.0
            score=sum(qv.get(t,0)*v for t,v in dv.items())/(qn*dn)
            if score > 0:
                hits.append(SemanticHit(e.entry_id,e.lesson,e.outcome,round(score,4),e.strategy))
        return sorted(hits,key=lambda h:h.score,reverse=True)[:max(1,min(limit,10))]
