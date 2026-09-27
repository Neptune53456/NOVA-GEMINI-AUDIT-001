"""Auditable Phase-6 research primitives; all fitting is past-only."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from hashlib import sha256
from math import sqrt
from statistics import mean, median
from typing import Any

@dataclass(frozen=True)
class ComponentScore:
    name: str; raw_score: float | None; availability: bool; quality: float
    feature_refs: tuple[str, ...]; model_version: str

class ForecastEnsembleV3:
    version = "ensemble-v3.0"
    components = ("momentum", "mean_reversion", "event_driven", "similar_events", "regime_aware", "calibrated_baseline")
    def score(self, features: dict[str, Any]) -> list[ComponentScore]:
        momentum=float(features.get("market_momentum",0)); ma=float(features.get("moving_average_short",0))-float(features.get("moving_average_long",0))
        values={"momentum":momentum,"mean_reversion":-momentum,"event_driven":{"positive":.5,"negative":-.5}.get(str(features.get("llm_impact_direction")),0),
          "similar_events":features.get("similar_return_median"),"regime_aware": momentum if "trend" in str(features.get("market_regime")) else -momentum,
          "calibrated_baseline":features.get("baseline_score")}
        return [ComponentScore(n, None if values[n] is None else float(values[n]), values[n] is not None,
          float(features.get("source_quality",.5)), tuple(sorted(str(k) for k in features if n.split('_')[0] in k or k in {"market_momentum","market_regime"})), "v1") for n in self.components]
    def combine(self, parts:list[ComponentScore], history:list[dict[str,Any]], as_of:datetime, regime:str|None=None)->dict[str,Any]:
        past=[r for r in history if datetime.fromisoformat(str(r["created_at"]).replace("Z","+00:00"))<as_of and (not regime or r.get("regime")==regime)]
        perf={n:[float(r["correct"]) for r in past if r.get("component")==n] for n in self.components}
        weights={p.name:(.5+max(.1,mean(perf[p.name])) if len(perf[p.name])>=20 else 1.0) for p in parts if p.availability}
        total=sum(weights.values()) or 1; weights={k:v/total for k,v in weights.items()}
        return {"raw_score":sum((p.raw_score or 0)*weights.get(p.name,0)*p.quality for p in parts),"weights":weights,
          "weight_source":"past_performance" if any(len(v)>=20 for v in perf.values()) else "fixed_fallback","model_version":self.version}

def expected_return_v2(rows:list[dict[str,Any]], *, as_of:datetime, horizon:str, score:float, asset_class:str, regime:str, minimum:int=20)->dict[str,Any]|None:
    eligible=[r for r in rows if datetime.fromisoformat(str(r["created_at"]).replace("Z","+00:00"))<as_of and r.get("horizon")==horizon and
      r.get("asset_class")==asset_class and r.get("regime")==regime and abs(float(r.get("raw_score",0))-score)<=.25]
    if len(eligible)<minimum:return None
    values=sorted(float(r["actual_return"]) for r in eligible); q=lambda p:values[min(len(values)-1,int((len(values)-1)*p))]
    return {"mean":mean(values),"median":median(values),"downside_quantile":q(.1),"upside_quantile":q(.9),"sample_count":len(values)}

def temporal_split(rows:list[dict[str,Any]], train=.6, validation=.2)->dict[str,list[dict[str,Any]]]:
    ordered=sorted(rows,key=lambda r:str(r["created_at"])); a=int(len(ordered)*train); b=int(len(ordered)*(train+validation))
    return {"train":ordered[:a],"validation":ordered[a:b],"holdout":ordered[b:]}

@dataclass(frozen=True)
class StrategyParameter:
    name:str; value:Any; version:str; source:str; created_at:str; evaluation_window:str; status:str="candidate"

class ParameterRegistry:
    def __init__(self): self._items:dict[tuple[str,str],StrategyParameter]={}
    def register(self,item:StrategyParameter)->None:
        key=(item.name,item.version)
        if key in self._items and self._items[key]!=item: raise ValueError("immutable_parameter_version")
        if "holdout" in item.source.casefold(): raise ValueError("holdout_cannot_select_parameters")
        self._items[key]=item
    def values(self): return [asdict(v) for v in self._items.values()]

def promote_candidate(candidate:dict[str,Any], incumbent:dict[str,Any], criteria:dict[str,float])->dict[str,Any]:
    checks={"samples":candidate.get("sample_count",0)>=criteria.get("minimum_samples",30),"drawdown":candidate.get("max_drawdown",-1)>=-criteria.get("max_drawdown",.2),
      "calibration":candidate.get("calibration_error",1)<=criteria.get("max_calibration_error",.15),"baseline":candidate.get("excess_vs_baseline",-1)>0,
      "regimes":candidate.get("regime_count",0)>=criteria.get("minimum_regimes",2),"net":candidate.get("net_return",-1)>incumbent.get("net_return",-1)}
    return {"promoted":all(checks.values()),"checks":checks,"reason":"all_criteria_passed" if all(checks.values()) else "criteria_failed"}

def allocate(policy:str, opportunities:list[dict[str,Any]], *, max_position=.1,max_exposure=.75,cash_reserve=.2)->dict[str,float]:
    cap=min(max_exposure,1-cash_reserve); eligible=[o for o in opportunities if o.get("status") in {"eligible","accepted"}]
    if policy=="equal_weight": raw=[1.0 for _ in eligible]
    elif policy=="volatility_adjusted": raw=[1/max(.01,float(o.get("volatility",1))) for o in eligible]
    elif policy=="capped_kelly": raw=[max(0,min(.25,float(o.get("kelly_fraction",0)))) if int(o.get("sample_count",0))>=50 else 0 for o in eligible]
    else: raw=[max(0,float(o.get("opportunity_score",0))) for o in eligible]
    total=sum(raw) or 1
    return {str(o["instrument"]):min(max_position,cap*w/total) for o,w in zip(eligible,raw)}

def rolling_correlation(a:list[float],b:list[float])->float|None:
    n=min(len(a),len(b));
    if n<3:return None
    x,y=a[-n:],b[-n:]; mx,my=mean(x),mean(y); den=sqrt(sum((v-mx)**2 for v in x)*sum((v-my)**2 for v in y))
    return None if not den else sum((x[i]-mx)*(y[i]-my) for i in range(n))/den

def risk_assessment(weights:dict[str,float], metadata:dict[str,dict[str,Any]], *, asset_class_cap=.4, drawdown=0.0, drawdown_limit=.2)->dict[str,Any]:
    classes:dict[str,float]={}
    for symbol,weight in weights.items(): classes[str(metadata.get(symbol,{}).get("asset_class","unknown"))]=classes.get(str(metadata.get(symbol,{}).get("asset_class","unknown")),0)+weight
    concentration=sum(v*v for v in weights.values()); reasons=[]
    if any(v>asset_class_cap for v in classes.values()): reasons.append("asset_class_cap")
    if drawdown<=-drawdown_limit: reasons.append("drawdown_guard")
    return {"accepted":not reasons,"reasons":reasons,"concentration":concentration,"asset_class_exposure":classes,
      "portfolio_volatility_approx":sqrt(sum((weights[s]*float(metadata.get(s,{}).get("volatility",0)))**2 for s in weights))}

def robustness_report(results:list[dict[str,Any]], tolerance=.25)->dict[str,Any]:
    returns=[float(r["net_return"]) for r in results]
    fragile=not returns or min(returns)<0 or (max(returns)-min(returns)>tolerance)
    return {"fragile":fragile,"scenario_count":len(results),"worst_net_return":min(returns) if returns else None,
      "dimensions":sorted({str(r.get("dimension","unknown")) for r in results})}
