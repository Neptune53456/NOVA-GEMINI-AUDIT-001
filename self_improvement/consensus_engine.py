"""Sélection de variantes V6 : plusieurs candidats, une décision déterministe et testable."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Callable, Sequence
import difflib

@dataclass(frozen=True)
class CandidateVerdict:
    index:int; passed:bool; score:float; changed_lines:int; reason:str
    def to_dict(self): return asdict(self)

class ConsensusSelector:
    """Évalue N candidats avec un callback indépendant; préfère le plus petit patch sûr."""
    def select(self, baseline: str, candidates: Sequence[str], evaluator: Callable[[str], tuple[bool,float,str]]):
        verdicts=[]
        for i,candidate in enumerate(candidates):
            passed, quality, reason = evaluator(candidate)
            changed=sum(1 for line in difflib.ndiff(baseline.splitlines(), candidate.splitlines()) if line[:1] in {"+","-"})
            # qualité domine; à qualité égale, patch plus petit.
            score=float(quality) - min(changed,1000)*0.0001
            verdicts.append(CandidateVerdict(i,bool(passed),round(score,4),changed,str(reason)))
        viable=[v for v in verdicts if v.passed]
        if not viable: return None, verdicts
        winner=max(viable,key=lambda v:(v.score,-v.changed_lines,-v.index))
        return winner.index, verdicts
