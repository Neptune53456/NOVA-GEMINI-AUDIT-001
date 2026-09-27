"""Disabled-by-default deterministic sandbox execution; no live transport exists."""
from __future__ import annotations
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from hashlib import sha256
from typing import Protocol

class SandboxGuardError(RuntimeError): pass
class SandboxBroker(Protocol):
    def account_info(self)->dict: ...
    def submit_order(self,request:dict)->dict: ...
    def cancel_order(self,order_id:str)->dict: ...
    def order_status(self,order_id:str)->dict: ...

class FakeSandboxBroker:
    def __init__(self,*,sandbox:bool=True,enabled:bool=False,kill_switch:bool=True,max_notional:float=1000):
        self.sandbox,self.enabled,self.kill_switch,self.max_notional=sandbox,enabled,kill_switch,max_notional; self.orders={}; self.keys={}
    def _guard(self,request):
        if not self.sandbox: raise SandboxGuardError("live_environment_refused")
        if not self.enabled: raise SandboxGuardError("sandbox_disabled")
        if self.kill_switch: raise SandboxGuardError("kill_switch_active")
        if float(request.get("notional",0))<=0 or float(request.get("notional",0))>self.max_notional: raise SandboxGuardError("notional_limit")
        if not request.get("idempotency_key"): raise SandboxGuardError("idempotency_key_required")
    def account_info(self): return {"environment":"sandbox","currency":"USD","paper":True}
    def submit_order(self,request):
        self._guard(request); key=str(request["idempotency_key"])
        if key in self.keys:return self.orders[self.keys[key]]
        oid=sha256(key.encode()).hexdigest(); order={**request,"order_id":oid,"status":"filled","environment":"sandbox","filled_notional":float(request["notional"])}
        self.orders[oid]=order; self.keys[key]=oid; return order
    def cancel_order(self,order_id):
        if order_id not in self.orders: raise KeyError("order_not_found")
        if self.orders[order_id]["status"]!="filled":self.orders[order_id]["status"]="cancelled"
        return self.orders[order_id]
    def order_status(self,order_id): return self.orders[order_id]
    def paper_positions(self): return []

class PaperExecutionController:
    def __init__(self,broker:FakeSandboxBroker,store=None): self.broker,self.store=broker,store
    def execute(self,opportunity:dict,risk:dict,*,price_fresh:bool,notional:float,idempotency_key:str)->dict:
        reasons=[]
        if opportunity.get("status") not in {"eligible","accepted"}:reasons.append("opportunity_rejected")
        if int(opportunity.get("calibration_sample_count",0))<20:reasons.append("calibration_insufficient")
        if not risk.get("accepted"):reasons.append("risk_rejected")
        if not price_fresh:reasons.append("stale_data")
        if reasons:return {"status":"rejected","reasons":reasons}
        order=self.broker.submit_order({"symbol":opportunity["instrument"],"side":opportunity.get("direction"),"notional":notional,"idempotency_key":idempotency_key})
        if self.store:self.store.save_audit("sandbox_order",order["order_id"],order)
        return order
    def reconcile(self,order_id:str)->dict:return self.broker.order_status(order_id)
