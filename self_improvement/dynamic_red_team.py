"""Red Team dynamique V6 basé uniquement sur des scénarios TRAIN déjà publics.

Les variantes restent `provisional` et ne deviennent jamais automatiquement un
oracle autoritatif. Elles servent à découvrir des régressions; leur promotion vers
un corpus trusted nécessite toujours le pipeline de scénario existant.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
import json
from typing import Iterable
from self_improvement.models import Scenario, ScenarioResult
from self_improvement.variant_generator import LinguisticVariantGenerator

@dataclass(frozen=True)
class RedTeamBatch:
    source_count:int; generated_count:int; path:str
    def to_dict(self): return asdict(self)

class DynamicRedTeamGenerator:
    def __init__(self, repo_root: str|Path, *, seed:int=0):
        self.repo_root=Path(repo_root).resolve(); self.generator=LinguisticVariantGenerator(seed=seed, maximum_variants=100)
        self.output=self.repo_root/".runtime"/"red_team_candidates.json"
    def generate_from_train(self, scenarios: Iterable[Scenario], *, variants_per_scenario:int=3) -> RedTeamBatch:
        sources=[s for s in scenarios if str(s.split).casefold()=="train"]
        generated=[]
        for source in sources[:25]:
            generated.extend(self.generator.generate(source,count=max(1,min(variants_per_scenario,5))))
        payload=[]
        for s in generated:
            row=asdict(s); row["split"]="train"; row["tags"]=list(dict.fromkeys([*row.get("tags",[]),"dynamic-red-team","provisional"]))
            payload.append(row)
        self.output.parent.mkdir(parents=True,exist_ok=True)
        self.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
        return RedTeamBatch(len(sources),len(payload),str(self.output))
