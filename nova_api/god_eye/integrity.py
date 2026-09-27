"""Versioned, non-destructive market-data integrity checks and bounded repair."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from statistics import median
from typing import Any
from .models import MarketCandle,as_utc,utc_now
from .providers import INTERVAL_SECONDS,detect_candle_gaps,normalize_candles

INTEGRITY_VERSION="integrity-v1"
def assess_candles(rows:list[dict[str,Any]],interval:str)->dict[str,Any]:
 ordered=sorted(rows,key=lambda r:str(r["opened_at"])); issues=[]; seen=set(); closes=[]
 for row in rows:
  stamp=str(row["opened_at"])
  try:
   if as_utc(stamp)>utc_now()+__import__("datetime").timedelta(minutes=5):issues.append({"timestamp":stamp,"issue":"future_timestamp"})
  except (TypeError,ValueError):issues.append({"timestamp":stamp,"issue":"invalid_timestamp"})
  if stamp in seen:issues.append({"timestamp":stamp,"issue":"duplicate"})
  seen.add(stamp)
  try:
   o,h,l,c=map(float,(row["open"],row["high"],row["low"],row["close"]))
   if min(o,h,l,c)<=0 or h<max(o,c) or l>min(o,c):issues.append({"timestamp":stamp,"issue":"invalid_ohlc"})
   if closes and abs(c/closes[-1]-1)>.5:issues.append({"timestamp":stamp,"issue":"impossible_jump"})
   closes.append(c)
  except (KeyError,TypeError,ValueError):issues.append({"timestamp":stamp,"issue":"malformed"})
 if [str(r["opened_at"]) for r in rows]!=[str(r["opened_at"]) for r in ordered]:issues.append({"issue":"out_of_order"})
 if ordered and (utc_now()-as_utc(ordered[-1]["opened_at"])).total_seconds()>INTERVAL_SECONDS[interval]*3:issues.append({"issue":"stale_feed"})
 gaps=detect_candle_gaps(ordered,interval)
 score=max(0,1-min(1,(len(issues)+len(gaps))/max(1,len(rows))))
 return {"version":INTEGRITY_VERSION,"data_quality_score":round(score,3),"issues":issues,"missing_intervals":len(gaps),"gaps":[(a.isoformat(),b.isoformat()) for a,b in gaps]}

class GapRepair:
 def __init__(self,store,*,max_gaps=5,max_calls=3):self.store,self.max_gaps,self.max_calls=store,max_gaps,max_calls
 def run(self,instrument,interval,provider)->dict[str,Any]:
  rows=self.store.history(instrument.symbol,interval=interval,limit=2000); gaps=detect_candle_gaps(rows,interval,max_gaps=self.max_gaps)
  repaired=failed=0
  for left,right in sorted(gaps,key=lambda g:g[1],reverse=True)[:self.max_calls]:
   try:
    candles=normalize_candles(provider.candles(instrument,interval,min(100,int((right-left).total_seconds()/INTERVAL_SECONDS[interval]))))
    repaired+=self.store.save_candles(candles)
   except Exception:failed+=1
  self.store.save_backfill_checkpoint(instrument.symbol,interval,utc_now().isoformat(),gaps[-1][1].isoformat() if gaps else None)
  return {"repaired":repaired,"failed":failed,"pending":max(0,len(gaps)-min(len(gaps),self.max_calls)),"calls":min(len(gaps),self.max_calls)}
