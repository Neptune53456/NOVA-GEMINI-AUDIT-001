"""Bounded reproducible research isolated from production and locked holdout."""
from __future__ import annotations
from dataclasses import dataclass,asdict,field
from datetime import datetime,timezone
from hashlib import sha256
from random import Random
from statistics import mean
from typing import Any,Callable
from .research import promote_candidate

STATUSES={"draft","running","rejected","candidate","promoted"}
@dataclass
class ResearchExperiment:
 experiment_id:str; hypothesis:str; changes:dict[str,Any]; train_window:str; validation_window:str; locked_holdout:str
 params:dict[str,Any]; metrics:dict[str,Any]=field(default_factory=dict); status:str="draft"; artifacts:list[str]=field(default_factory=list)

class ResearchEngine:
 def __init__(self,store,*,budget=10,seed=0):self.store,self.budget,self.seed=store,budget,seed
 def run(self,experiment:ResearchExperiment,search_space:dict[str,list[Any]],evaluate:Callable[[dict[str,Any],str],dict[str,Any]])->list[dict]:
  if experiment.status!="draft":raise ValueError("experiment_not_draft")
  if any("holdout" in k.casefold() for k in search_space):raise ValueError("holdout_search_forbidden")
  keys=sorted(search_space); variants=[]
  for i in range(min(self.budget,max((len(search_space[k]) for k in keys),default=0))):
   params={k:search_space[k][i%len(search_space[k])] for k in keys}; metrics=evaluate(params,"validation")
   variants.append({"candidate_id":sha256(f"{experiment.experiment_id}|{i}|{self.seed}".encode()).hexdigest(),"params":params,"validation_metrics":metrics,"seed":self.seed})
  experiment.status="candidate" if variants else "rejected"; experiment.metrics={"variants":len(variants)}
  self.store.save_governance("experiment",experiment.experiment_id,"1",asdict(experiment),datetime.now(timezone.utc).isoformat())
  for value in variants:self.store.save_governance("candidate",value["candidate_id"],"1",value,datetime.now(timezone.utc).isoformat())
  return variants

def anti_overfit_gate(metrics:dict[str,Any],*,minimum_samples=30)->dict[str,Any]:
 checks={"sample":metrics.get("sample_count",0)>=minimum_samples,"divergence":abs(metrics.get("train_return",0)-metrics.get("validation_return",0))<=.15,
  "holdout":metrics.get("holdout_return",-1)>=metrics.get("validation_return",0)-.15,"sensitivity":metrics.get("parameter_sensitivity",1)<=.2,
  "regimes":metrics.get("worst_regime_return",-1)>=0,"assets":metrics.get("worst_asset_return",-1)>=0,"costs":metrics.get("high_cost_return",-1)>=0}
 return {"accepted":all(checks.values()),"checks":checks,"reason":"robust" if all(checks.values()) else "overfit_or_fragile"}

def bounded_bootstrap(values:list[float],*,samples=200,seed=0)->dict[str,Any]:
 if len(values)<30:return {"sample_count":len(values),"confidence_interval":None,"reason":"insufficient_sample"}
 rng=Random(seed); estimates=[mean(rng.choices(values,k=len(values))) for _ in range(min(500,max(20,samples)))];estimates.sort()
 return {"sample_count":len(values),"mean":mean(values),"confidence_interval":[estimates[int(.025*len(estimates))],estimates[min(len(estimates)-1,int(.975*len(estimates)))]]}

class PromotionRegistry:
 def __init__(self,store):self.store=store
 def promote(self,candidate:dict,incumbent:dict,criteria:dict,version:str)->dict:
  decision=promote_candidate(candidate,incumbent,criteria); value={"candidate":candidate.get("version"),"incumbent":incumbent.get("version"),"decision":decision,"rollback_target":incumbent.get("version"),"promoted_at":datetime.now(timezone.utc).isoformat() if decision["promoted"] else None}
  self.store.save_governance("promotion",str(candidate.get("version")),version,value,datetime.now(timezone.utc).isoformat());return value
 def rollback(self,current:str,target:str,reason:str)->dict:
  value={"current":current,"rollback_target":target,"reason":reason,"rolled_back_at":datetime.now(timezone.utc).isoformat()};self.store.save_governance("rollback",current,target,value,value["rolled_back_at"]);return value

def live_forward_record(version:str,started_at:str,ended_at:str|None=None)->dict:
 return {"version":version,"mode":"live_forward_paper","started_at":started_at,"ended_at":ended_at,"status":"completed" if ended_at else "running","historical_backtest":False}
