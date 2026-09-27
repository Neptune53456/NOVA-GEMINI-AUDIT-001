"""Deterministic research analytics; PAPER data only and no access to sealed holdout rows."""
from __future__ import annotations
from hashlib import sha256
from random import Random
from statistics import mean
from typing import Any, Callable, Iterable

ALLOWED_DECISIONS={"WAIT","ENTER","HOLD","ADD","REDUCE","EXIT","IGNORE","REJECTED"}
ERRORS={"bad_forecast","bad_timing","bad_sizing","excessive_fees","slippage","wrong_regime",
        "false_event_interpretation","late_entry","premature_exit","late_exit","overtrading",
        "missed_opportunity","data_quality_failure"}
DENYLIST=("live_broker","fake_sandbox_broker","kill_switch","risk_engine","secret","credential",
          "private_holdout","holdout_access","supervisor","judge","promotion_policy","financial_safety")

def decision_record(state:dict[str,Any],decision:str,outcome:dict[str,Any],*,decided_at:str,resolved_at:str)->dict[str,Any]:
    if decision not in ALLOWED_DECISIONS:raise ValueError("invalid_decision")
    future=set(state).intersection({"forward_return","net_result","mfe","mae","resolved_at","future_price"})
    if future:raise ValueError("look_ahead_state_forbidden")
    return {"record_id":sha256(f"{decided_at}|{decision}|{state}".encode()).hexdigest(),"state":state,
            "decision":decision,"outcome":outcome,"decided_at":decided_at,"resolved_at":resolved_at,
            "look_ahead":False,"paper_only":True,"version":"decision-dataset-v2"}

def learning_summary(records:Iterable[dict[str,Any]])->dict[str,Any]:
    groups:dict[str,list[dict[str,Any]]]={}
    for row in records:groups.setdefault(str(row["decision"]),[]).append(row)
    quality={}
    errors={name:{"count":0,"financial_impact":0.0} for name in ERRORS}
    for action,rows in groups.items():
        nets=[float(v.get("outcome",{}).get("net_result",0)) for v in rows]
        quality[action]={"sample_count":len(rows),"mean_net_result":mean(nets) if nets else None,
                         "reliable":len(rows)>=20}
        for row in rows:
            name=row.get("outcome",{}).get("error_type")
            if name in errors:errors[name]["count"]+=1;errors[name]["financial_impact"]+=float(row["outcome"].get("net_result",0))
    return {"decision_quality":quality,"error_taxonomy":errors,"paper_only":True}

def bounded_search(search_space:dict[str,list[Any]],evaluate:Callable[[dict[str,Any]],dict[str,Any]],*,
                   method:str="grid",max_candidates:int=10,seed:int=0)->list[dict[str,Any]]:
    if max_candidates<1 or max_candidates>100:raise ValueError("candidate_budget_out_of_bounds")
    if any(any(word in key.casefold() for word in DENYLIST) for key in search_space):raise ValueError("immutable_boundary")
    keys=sorted(search_space); candidates=[]
    if not keys:return candidates
    total=max(len(search_space[k]) for k in keys); indexes=list(range(total))
    if method=="random":Random(seed).shuffle(indexes)
    elif method!="grid":raise ValueError("unsupported_search_method")
    for index in indexes[:max_candidates]:
        params={key:search_space[key][index%len(search_space[key])] for key in keys}
        candidates.append({"params":params,"metrics":evaluate(params),"evaluation_index":index,"seed":seed})
    return candidates

def feature_ablation(features:dict[str,Any],evaluate:Callable[[dict[str,Any]],dict[str,Any]])->dict[str,Any]:
    baseline=evaluate(dict(features)); variants={}
    for key in sorted(features):variants[f"minus:{key}"]=evaluate({k:v for k,v in features.items() if k!=key})
    return {"baseline":baseline,"ablations":variants,"out_of_sample_required":True}

def evidence_maturity(*,resolved_forecasts:int,resolved_trades:int,duration_days:int,regimes:int,assets:int)->dict[str,Any]:
    score=sum((resolved_forecasts>=20,resolved_trades>=10,duration_days>=14,regimes>=2,assets>=2))
    tier="MATURE" if score==5 else "DEVELOPING" if score>=3 else "EARLY" if score>=1 else "INSUFFICIENT"
    return {"tier":tier,"criteria":{"resolved_forecasts":resolved_forecasts,"resolved_trades":resolved_trades,
            "duration_days":duration_days,"regime_diversity":regimes,"asset_diversity":assets}}

def drift_report(reference:dict[str,float],current:dict[str,float],*,threshold:float=.20)->dict[str,Any]:
    deltas={key:current[key]-value for key,value in reference.items() if key in current}
    flags={key:abs(delta)>threshold*max(abs(reference[key]),1e-9) for key,delta in deltas.items()}
    return {"deltas":deltas,"flags":flags,"drift_detected":any(flags.values()),"automatic_promotion":False}

def watchdog(scheduler:dict[str,Any],*,now_timestamp:float,maximum_lag_seconds:float=900,
             backlogs:dict[str,int]|None=None)->dict[str,Any]:
    alerts=[]
    from datetime import datetime
    if scheduler.get("started_at") and not scheduler.get("scheduler_alive", scheduler.get("running", False)):
        alerts.append({"task":"scheduler","reason":"scheduler_dead"})
    for name,state in scheduler.get("tasks",{}).items():
        finished=state.get("last_finished_at")
        if state.get("failures",0)>=3:alerts.append({"task":name,"reason":"repeated_failure"})
        elif finished and now_timestamp-datetime.fromisoformat(str(finished).replace("Z","+00:00")).timestamp()>maximum_lag_seconds:
            alerts.append({"task":name,"reason":"stalled"})
    for name,count in (backlogs or {}).items():
        if count>0: alerts.append({"task":name,"reason":"backlog","count":count})
    return {"healthy":not alerts,"alerts":alerts,"bounded_recovery":True,"maximum_recovery_attempts":3}

class SealedHoldoutGateway:
    """Accepts only aggregate metrics and returns a bounded public verdict."""
    def __init__(self,*,maximum_evaluations:int=5,store=None):
        self.maximum=max(1,maximum_evaluations);self.store=store
        self.counts={}
        if store:
            for row in store.governance("holdout_evaluation"):
                version=str(row.get("candidate_version"));self.counts[version]=max(self.counts.get(version,0),int(row.get("evaluation_count",0)))
    def evaluate(self,version:str,aggregate_metrics:dict[str,float])->dict[str,Any]:
        self.counts[version]=self.counts.get(version,0)+1
        if self.counts[version]>self.maximum:raise RuntimeError("holdout_evaluation_limit")
        allowed={k:aggregate_metrics[k] for k in ("net_return","calibration","drawdown","robustness","baseline_delta","integrity") if k in aggregate_metrics}
        result={"candidate_version":version,"evaluation_count":self.counts[version],"aggregate_metrics":allowed,
                "raw_rows_exposed":False,"timestamps_exposed":False}
        if self.store:
            key=f"{version}|{self.counts[version]}";self.store.save_governance("holdout_evaluation",key,"1",result,key)
        return result
