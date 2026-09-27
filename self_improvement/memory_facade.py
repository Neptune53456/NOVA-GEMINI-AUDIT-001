"""Façade mémoire V6.

Sépare explicitement mémoire utilisateur/applicative et mémoire d'ingénierie. Le
self-improvement consomme uniquement la seconde; aucune donnée utilisateur n'est
injectée dans les benchmarks ou objectifs autonomes.
"""
from __future__ import annotations
from pathlib import Path
from self_improvement.engineering_memory import EngineeringMemory
from self_improvement.semantic_memory import SemanticEngineeringMemory

class MemoryFacade:
    def __init__(self, repo_root: str|Path):
        self.repo_root=Path(repo_root).resolve()
        self.engineering=EngineeringMemory(self.repo_root)
        self.semantic=SemanticEngineeringMemory(self.engineering)
    def engineering_hints(self, query:str, *, limit:int=5):
        return [h.to_dict() for h in self.semantic.search(query,limit=limit)]
    def stats(self):
        return {"engineering": self.engineering.stats(), "semantic_backend":"local_tfidf_v1", "user_memory_isolated":True}
