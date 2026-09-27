"""Durable, idempotent SQLite storage for normalized God Eyes records."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

from nova_api.persistence import ensure_schema_version

from .models import (EventOutcome, ForecastRecord, GodEyeEvent, MarketCandle, MarketQuote,
                     NewsItem, NewsSourceConfig, public)

DEFAULT_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "god_eye.sqlite3"


class GodEyeStore:
    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path, self._lock = Path(path), Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            ensure_schema_version(db, expected=1, component="god_eye")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    provider TEXT PRIMARY KEY, source_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, provenance TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quotes (
                    symbol TEXT NOT NULL, observed_at TEXT NOT NULL, provider TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(symbol, observed_at, provider)
                );
                CREATE INDEX IF NOT EXISTS quotes_recent_idx ON quotes(symbol, observed_at DESC);
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL, interval TEXT NOT NULL, opened_at TEXT NOT NULL,
                    provider TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(symbol, interval, opened_at, provider)
                );
                CREATE TABLE IF NOT EXISTS news (
                    item_id TEXT PRIMARY KEY, published_at TEXT NOT NULL, provider TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS news_recent_idx ON news(published_at DESC);
                CREATE TABLE IF NOT EXISTS god_eye_events (
                    event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecasts (
                    forecast_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_stats (
                    source_id TEXT PRIMARY KEY, successes INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0, last_success TEXT, last_error TEXT,
                    last_error_category TEXT
                );
                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL,
                    collected_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS event_enrichments (
                    event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL, model TEXT, provider TEXT
                );
                CREATE TABLE IF NOT EXISTS event_outcomes (
                    outcome_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, instrument TEXT NOT NULL,
                    calculated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_evaluations (
                    forecast_id TEXT PRIMARY KEY, evaluated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calibrations (
                    calibration_key TEXT PRIMARY KEY, built_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS regimes (
                    regime_key TEXT PRIMARY KEY, detected_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opportunities (
                    opportunity_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS social_posts (
                    post_id TEXT PRIMARY KEY, published_at TEXT NOT NULL, platform TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS social_signals (
                    signal_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    trade_id TEXT PRIMARY KEY, exit_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS governance (kind TEXT NOT NULL, item_key TEXT NOT NULL, version TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,item_key,version));
                CREATE TABLE IF NOT EXISTS alerts (alert_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, acknowledged_at TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_log (kind TEXT NOT NULL, item_key TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,item_key));
                CREATE TABLE IF NOT EXISTS backfill_checkpoints (symbol TEXT NOT NULL, interval TEXT NOT NULL, checked_at TEXT NOT NULL, cursor TEXT, PRIMARY KEY(symbol,interval));
                CREATE TABLE IF NOT EXISTS alternative_items (
                    item_id TEXT PRIMARY KEY, source TEXT NOT NULL, published_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alternative_recent_idx ON alternative_items(published_at DESC);
                CREATE TABLE IF NOT EXISTS alternative_checkpoints (
                    source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_forward_runs (
                    validation_id TEXT PRIMARY KEY, model_version TEXT NOT NULL, started_at TEXT NOT NULL,
                    ended_at TEXT, status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intelligence_snapshots (
                    snapshot_key TEXT PRIMARY KEY, instrument TEXT NOT NULL, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS intelligence_recent_idx
                    ON intelligence_snapshots(kind, instrument, created_at DESC);
                CREATE TABLE IF NOT EXISTS star_opportunities (
                    opportunity_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS star_opportunity_rank_idx ON star_opportunities(status, detected_at DESC);
                CREATE TABLE IF NOT EXISTS opportunity_lifecycle (
                    transition_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS opportunity_lifecycle_idx ON opportunity_lifecycle(opportunity_id, occurred_at);
                CREATE TABLE IF NOT EXISTS cost_snapshots (
                    snapshot_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ranking_snapshots (
                    snapshot_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS star_decisions (
                    decision_id TEXT PRIMARY KEY, opportunity_id TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcome_metrics (
                    metric_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, resolved_at TEXT NOT NULL, sampled_missed INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_checkpoints (
                    task_name TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trader_records (
                    kind TEXT NOT NULL, record_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind, record_id)
                );
                CREATE INDEX IF NOT EXISTS trader_records_recent_idx ON trader_records(kind, portfolio_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS trader_state (
                    portfolio_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retention_status (
                    status_key TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS forecasts_created_idx ON forecasts(created_at);
                CREATE INDEX IF NOT EXISTS events_occurred_idx ON god_eye_events(occurred_at);
                CREATE INDEX IF NOT EXISTS social_posts_published_idx ON social_posts(published_at);
                CREATE INDEX IF NOT EXISTS social_signals_detected_idx ON social_signals(detected_at);
            """)

    def cleanup_retention(self, *, now: datetime | None = None,
                          retention_days: dict[str, int] | None = None,
                          batch_size: int = 250) -> dict[str, object]:
        """Bounded pruning of high-volume history; authoritative state is never targeted."""
        started = monotonic(); at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        policy = {"quotes":30,"news":90,"social_posts":30,"social_signals":90,
                  "alternative_items":90,"intelligence_snapshots":180,"forecasts":365,
                  "god_eye_events":365}
        policy.update(retention_days or {})
        limit=max(1,min(int(batch_size),5000)); deleted: dict[str,int]={}; errors: list[str]=[]
        simple = {"quotes":"observed_at","news":"published_at","social_posts":"published_at",
                  "social_signals":"detected_at","alternative_items":"published_at",
                  "intelligence_snapshots":"created_at"}
        with self._lock, self._connect() as db:
            for table,column in simple.items():
                try:
                    cutoff=(at-timedelta(days=max(1,int(policy[table])))).isoformat()
                    cursor=db.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} "
                                      f"WHERE {column}<? ORDER BY {column} LIMIT ?)",(cutoff,limit))
                    deleted[table]=cursor.rowcount
                except Exception as error: errors.append(f"{table}:{type(error).__name__}")
            try:
                cutoff=(at-timedelta(days=max(1,int(policy["forecasts"])))).isoformat()
                ids=[str(row[0]) for row in db.execute("""SELECT f.forecast_id FROM forecasts f
                    JOIN forecast_evaluations e ON e.forecast_id=f.forecast_id
                    WHERE f.created_at<? AND NOT EXISTS (
                      SELECT 1 FROM trader_records r WHERE r.kind='replay' AND r.payload LIKE '%'||f.forecast_id||'%')
                    ORDER BY f.created_at LIMIT ?""",(cutoff,limit))]
                if ids:
                    marks=",".join("?" for _ in ids)
                    db.execute(f"DELETE FROM forecast_evaluations WHERE forecast_id IN ({marks})",ids)
                    db.execute(f"DELETE FROM forecasts WHERE forecast_id IN ({marks})",ids)
                deleted["forecasts"]=len(ids)
            except Exception as error: errors.append(f"forecasts:{type(error).__name__}")
            try:
                cutoff=(at-timedelta(days=max(1,int(policy["god_eye_events"])))).isoformat()
                ids=[str(row[0]) for row in db.execute("""SELECT e.event_id FROM god_eye_events e WHERE e.occurred_at<?
                    AND EXISTS (SELECT 1 FROM event_outcomes o WHERE o.event_id=e.event_id)
                    AND NOT EXISTS (SELECT 1 FROM trader_records r WHERE r.kind='replay' AND r.payload LIKE '%'||e.event_id||'%')
                    ORDER BY e.occurred_at LIMIT ?""",(cutoff,limit))]
                if ids:
                    marks=",".join("?" for _ in ids)
                    db.execute(f"DELETE FROM event_outcomes WHERE event_id IN ({marks})",ids)
                    db.execute(f"DELETE FROM event_enrichments WHERE event_id IN ({marks})",ids)
                    db.execute(f"DELETE FROM god_eye_events WHERE event_id IN ({marks})",ids)
                deleted["god_eye_events"]=len(ids)
            except Exception as error: errors.append(f"god_eye_events:{type(error).__name__}")
            result={"rows_deleted":sum(deleted.values()),"by_table":deleted,
                    "duration_seconds":round(monotonic()-started,6),"last_cleanup":at.isoformat(),
                    "errors":errors,"healthy":not errors,"bounded_batch_size":limit}
            db.execute("INSERT OR REPLACE INTO retention_status VALUES ('cleanup',?,?)",
                       (at.isoformat(),json.dumps(result,sort_keys=True)))
        return result

    def retention_health(self) -> dict[str, object]:
        with self._lock,self._connect() as db:
            row=db.execute("SELECT payload FROM retention_status WHERE status_key='cleanup'").fetchone()
            return json.loads(row[0]) if row else {"rows_deleted":0,"last_cleanup":None,"errors":[],"healthy":False}

    def wal_checkpoint(self) -> dict[str, object]:
        with self._lock,self._connect() as db:
            busy,log,checkpointed=db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return {"busy":busy,"log_frames":log,"checkpointed_frames":checkpointed}

    def save_trader_record(self, kind: str, record_id: str, occurred_at: str, portfolio_id: str,
                           value: dict[str, object], *, retention: int = 10000) -> bool:
        with self._lock, self._connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO trader_records VALUES (?,?,?,?,?)",
                (kind, record_id, occurred_at, portfolio_id, json.dumps(value, sort_keys=True))).rowcount == 1
            db.execute("DELETE FROM trader_records WHERE rowid IN (SELECT rowid FROM trader_records WHERE kind=? "
                       "AND portfolio_id=? ORDER BY occurred_at DESC LIMIT -1 OFFSET ?)",
                       (kind, portfolio_id, max(1, retention)))
            return inserted

    def trader_records(self, kind: str, *, portfolio_id: str | None = "NOVA_COMPOSITE", limit: int = 500) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            if portfolio_id is None:
                return [json.loads(row[0]) for row in db.execute(
                    "SELECT payload FROM trader_records WHERE kind=? ORDER BY occurred_at DESC LIMIT ?",
                    (kind,max(1,min(limit,10000))))]
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM trader_records WHERE kind=? AND portfolio_id=? ORDER BY occurred_at DESC LIMIT ?",
                (kind, portfolio_id, max(1, min(limit, 10000))))]

    def save_trader_state(self, portfolio_id: str, updated_at: str, value: dict[str, object]) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO trader_state VALUES (?,?,?)",
                       (portfolio_id, updated_at, json.dumps(value, sort_keys=True)))

    def persist_champion_application(self, portfolio_id: str, updated_at: str,
                                     state: dict[str, object], audit: dict[str, object]) -> bool:
        """Atomically persist the active paper configuration and its immutable audit."""
        version = updated_at
        item_key = f'{portfolio_id}|{audit["new_version"]}'
        payload = json.dumps(audit, sort_keys=True)
        with self._lock, self._connect() as db:
            existing = db.execute("SELECT payload FROM governance WHERE kind='trader_champion_applied' "
                                  "AND item_key=? AND version=?", (item_key, version)).fetchone()
            if existing and existing[0] != payload:
                raise ValueError("immutable_champion_application")
            db.execute("INSERT OR IGNORE INTO governance VALUES (?,?,?,?,?)",
                       ("trader_champion_applied", item_key, version, updated_at, payload))
            db.execute("INSERT OR REPLACE INTO trader_state VALUES (?,?,?)",
                       (portfolio_id, updated_at, json.dumps(state, sort_keys=True)))
            return existing is None

    def trader_state(self, portfolio_id: str = "NOVA_COMPOSITE") -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM trader_state WHERE portfolio_id=?", (portfolio_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_scheduler_state(self, value: dict[str, object]) -> None:
        with self._lock, self._connect() as db:
            for name, state in dict(value.get("tasks", {})).items():
                db.execute("INSERT OR REPLACE INTO scheduler_checkpoints VALUES (?,?)",
                           (name, json.dumps(state, sort_keys=True)))

    def scheduler_state(self) -> dict[str, dict[str, object]]:
        with self._lock, self._connect() as db:
            return {str(row[0]): json.loads(row[1]) for row in db.execute("SELECT task_name,payload FROM scheduler_checkpoints")}

    def save_star_opportunity(self, value: dict[str, object], *, retention: int = 5000) -> bool:
        payload=json.dumps(value,sort_keys=True,separators=(",",":")); oid=str(value["opportunity_id"])
        with self._lock,self._connect() as db:
            inserted=db.execute("INSERT OR IGNORE INTO star_opportunities VALUES (?,?,?,?)",
                (oid,value["detected_at"],value["status"],payload)).rowcount==1
            if not inserted:
                db.execute("UPDATE star_opportunities SET status=?,payload=? WHERE opportunity_id=?",(value["status"],payload,oid))
            cost=value.get("cost_estimate")
            if cost:
                sid=f"{oid}|{cost.get('config_version')}|{value['detected_at']}"
                db.execute("INSERT OR IGNORE INTO cost_snapshots VALUES (?,?,?,?)",
                    (sid,oid,value["detected_at"],json.dumps(cost,sort_keys=True)))
            db.execute("DELETE FROM star_opportunities WHERE rowid IN (SELECT rowid FROM star_opportunities ORDER BY detected_at DESC LIMIT -1 OFFSET ?)",(max(1,retention),))
            return inserted

    def star_opportunities(self,*,include_rejected:bool=False,limit:int=100)->list[dict[str,object]]:
        query="SELECT payload FROM star_opportunities";args=[]
        if not include_rejected:query+=" WHERE status='QUALIFIED'"
        query+=" ORDER BY json_extract(payload,'$.star_score') DESC LIMIT ?";args.append(max(1,min(limit,1000)))
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute(query,args)]

    def star_opportunity(self,opportunity_id:str)->dict[str,object]|None:
        with self._lock,self._connect() as db:
            row=db.execute("SELECT payload FROM star_opportunities WHERE opportunity_id=?",(opportunity_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_lifecycle(self,value:dict[str,object])->bool:
        with self._lock,self._connect() as db:return db.execute("INSERT OR IGNORE INTO opportunity_lifecycle VALUES (?,?,?,?)",
            (value["transition_id"],value["opportunity_id"],value["timestamp"],json.dumps(value,sort_keys=True))).rowcount==1

    def lifecycle(self,opportunity_id:str)->list[dict[str,object]]:
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM opportunity_lifecycle WHERE opportunity_id=? ORDER BY occurred_at",(opportunity_id,))]

    def save_ranking_snapshot(self,value:dict[str,object],retention:int=200)->bool:
        created=str(value["generated_at"]);sid=f"{created}|{value.get('version')}"
        with self._lock,self._connect() as db:
            inserted=db.execute("INSERT OR IGNORE INTO ranking_snapshots VALUES (?,?,?)",(sid,created,json.dumps(value,sort_keys=True))).rowcount==1
            db.execute("DELETE FROM ranking_snapshots WHERE rowid IN (SELECT rowid FROM ranking_snapshots ORDER BY created_at DESC LIMIT -1 OFFSET ?)",(max(1,retention),))
            return inserted

    def ranking_snapshots(self,limit:int=20)->list[dict[str,object]]:
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM ranking_snapshots ORDER BY created_at DESC LIMIT ?",(max(1,min(limit,200)),))]

    def cost_snapshots(self,limit:int=100)->list[dict[str,object]]:
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM cost_snapshots ORDER BY created_at DESC LIMIT ?",(max(1,min(limit,500)),))]

    def save_decision(self,value:dict[str,object],created_at:str)->bool:
        oid=str(value.get("opportunity_id") or value.get("position_id") or "unknown")
        did=f"{oid}|{value.get('action')}|{created_at}"
        with self._lock,self._connect() as db:return db.execute("INSERT OR IGNORE INTO star_decisions VALUES (?,?,?,?)",
            (did,value.get("opportunity_id"),created_at,json.dumps(value,sort_keys=True))).rowcount==1

    def decisions(self,limit:int=500)->list[dict[str,object]]:
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM star_decisions ORDER BY created_at DESC LIMIT ?",(max(1,min(limit,1000)),))]

    def save_intelligence(self, key: str, instrument: str, created_at: str, kind: str,
                          value: dict[str, object], *, retention: int = 5000) -> bool:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            old = db.execute("SELECT payload FROM intelligence_snapshots WHERE snapshot_key=?", (key,)).fetchone()
            if old and old[0] != payload:
                raise ValueError("immutable_point_in_time_snapshot")
            inserted = db.execute("INSERT OR IGNORE INTO intelligence_snapshots VALUES (?,?,?,?,?)",
                                  (key, instrument, created_at, kind, payload)).rowcount == 1
            db.execute("DELETE FROM intelligence_snapshots WHERE rowid IN (SELECT rowid FROM intelligence_snapshots "
                       "ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (max(1, retention),))
            return inserted

    def intelligence(self, kind: str, *, instrument: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        query, args = "SELECT payload FROM intelligence_snapshots WHERE kind=?", [kind]
        if instrument is not None:
            query += " AND instrument=?"; args.append(instrument)
        query += " ORDER BY created_at DESC LIMIT ?"; args.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, args)]

    def save_alternative_items(self, items: list[object]) -> int:
        inserted = 0
        with self._lock, self._connect() as db:
            for item in items:
                value = public(item)
                cursor = db.execute("INSERT OR IGNORE INTO alternative_items VALUES (?,?,?,?)", (
                    value["item_id"], value["source"], value["published_at"],
                    json.dumps(value, sort_keys=True, separators=(",", ":"))))
                inserted += cursor.rowcount
        return inserted

    def alternative_items(self, limit: int = 200) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM alternative_items ORDER BY published_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),))]

    def alternative_checkpoint(self, source: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT cursor FROM alternative_checkpoints WHERE source=?", (source,)).fetchone()
            return row[0] if row else None

    def save_alternative_checkpoint(self, source: str, cursor: str | None, updated_at: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO alternative_checkpoints VALUES (?,?,?)", (source, cursor, updated_at))

    def save_live_forward(self, value: dict[str, object]) -> None:
        validation_id = str(value["validation_id"])
        with self._lock, self._connect() as db:
            old = db.execute("SELECT model_version,started_at FROM live_forward_runs WHERE validation_id=?",
                             (validation_id,)).fetchone()
            if old and (old["model_version"] != value["model_version"] or old["started_at"] != value["start_at"]):
                raise ValueError("live_forward_identity_is_immutable")
            db.execute("INSERT OR REPLACE INTO live_forward_runs VALUES (?,?,?,?,?,?)", (
                validation_id, value["model_version"], value["start_at"], value.get("end_at"), value["status"],
                json.dumps(value, sort_keys=True, separators=(",", ":"))))

    def live_forward_runs(self, limit: int = 50) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM live_forward_runs ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 200)),))]

    def register_sources(self, configs: list[NewsSourceConfig]) -> None:
        with self._lock, self._connect() as db:
            for config in configs:
                db.execute("INSERT OR IGNORE INTO source_stats(source_id) VALUES (?)", (config.source_id,))

    def save_governance(self,kind:str,item_key:str,version:str,value:dict,created_at:str)->bool:
        with self._lock,self._connect() as db:
            existing=db.execute("SELECT payload FROM governance WHERE kind=? AND item_key=? AND version=?",(kind,item_key,version)).fetchone()
            payload=json.dumps(value,sort_keys=True)
            if existing and existing[0]!=payload: raise ValueError("immutable_governance_version")
            return db.execute("INSERT OR IGNORE INTO governance VALUES (?,?,?,?,?)",(kind,item_key,version,created_at,payload)).rowcount==1

    def governance(self,kind:str|None=None)->list[dict]:
        query="SELECT payload FROM governance"; args=()
        if kind:query+=" WHERE kind=?";args=(kind,)
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute(query,args)]

    def save_alert(self,value:dict)->bool:
        with self._lock,self._connect() as db:return db.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?)",(value["alert_id"],value["created_at"],value.get("acknowledged_at"),json.dumps(value,sort_keys=True))).rowcount==1

    def alerts(self,limit=100)->list[dict]:
        with self._lock,self._connect() as db:return [json.loads(r[0]) for r in db.execute("SELECT payload FROM alerts ORDER BY created_at DESC LIMIT ?",(limit,))]

    def acknowledge_alert(self,alert_id:str,at:str)->bool:
        with self._lock,self._connect() as db:
            row=db.execute("SELECT payload FROM alerts WHERE alert_id=?",(alert_id,)).fetchone()
            if not row:return False
            value=json.loads(row[0]);value["acknowledged_at"]=at
            db.execute("UPDATE alerts SET acknowledged_at=?,payload=? WHERE alert_id=?",(at,json.dumps(value,sort_keys=True),alert_id));return True

    def save_audit(self,kind:str,item_key:str,value:dict)->bool:
        created=str(value.get("created_at") or value.get("timestamp") or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        with self._lock,self._connect() as db:return db.execute("INSERT OR IGNORE INTO audit_log VALUES (?,?,?,?)",(kind,item_key,created,json.dumps(value,sort_keys=True))).rowcount==1

    def save_backfill_checkpoint(self,symbol:str,interval:str,checked_at:str,cursor:str|None)->None:
        with self._lock,self._connect() as db:db.execute("INSERT OR REPLACE INTO backfill_checkpoints VALUES (?,?,?,?)",(symbol,interval,checked_at,cursor))

    def backfill_checkpoints(self)->list[dict]:
        with self._lock,self._connect() as db:return [dict(r) for r in db.execute("SELECT * FROM backfill_checkpoints ORDER BY checked_at DESC")]

    def save_quote(self, quote: MarketQuote) -> None:
        payload = json.dumps(public(quote), sort_keys=True, separators=(",", ":"))
        source = quote.source
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?)",
                       (source.provider, source.source_url, source.retrieved_at.isoformat(), source.provenance))
            db.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?)",
                       (quote.instrument.symbol, quote.observed_at.isoformat(), source.provider, payload))

    def save_candles(self, candles: list[MarketCandle], *, retention: int = 2000) -> int:
        inserted = 0
        with self._lock, self._connect() as db:
            for candle in candles:
                cursor = db.execute("INSERT OR IGNORE INTO candles VALUES (?, ?, ?, ?, ?)",
                    (candle.instrument.symbol, candle.interval, candle.opened_at.isoformat(),
                     candle.source.provider, json.dumps(public(candle), sort_keys=True, separators=(",", ":"))))
                inserted += cursor.rowcount
            for symbol, interval in {(c.instrument.symbol, c.interval) for c in candles}:
                db.execute("""DELETE FROM candles WHERE symbol=? AND interval=? AND rowid NOT IN
                    (SELECT rowid FROM candles WHERE symbol=? AND interval=? ORDER BY opened_at DESC LIMIT ?)""",
                    (symbol, interval, symbol, interval, max(1, retention)))
        return inserted

    def history(self, symbol: str, *, interval: str | None = None, limit: int = 500,
                start: str | None = None, end: str | None = None) -> list[dict[str, object]]:
        query, params = "SELECT payload FROM (SELECT payload,opened_at,ROW_NUMBER() OVER (PARTITION BY opened_at ORDER BY provider) AS duplicate_rank FROM candles WHERE symbol=?", [symbol]
        if interval:
            query += " AND interval=?"
            params.append(interval)
        if start:
            query += " AND opened_at>=?"
            params.append(start)
        if end:
            query += " AND opened_at<=?"
            params.append(end)
        query += ") WHERE duplicate_rank=1 ORDER BY opened_at DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params).fetchall()]

    def candle_intervals(self, symbol: str) -> list[str]:
        with self._lock, self._connect() as db:
            return [str(row[0]) for row in db.execute(
                "SELECT DISTINCT interval FROM candles WHERE symbol=? ORDER BY interval", (symbol,))]

    def save_news(self, items: list[NewsItem]) -> int:
        with self._lock, self._connect() as db:
            inserted = 0
            for item in items:
                source = item.source
                db.execute("INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?)",
                           (source.provider, source.source_url, source.retrieved_at.isoformat(), source.provenance))
                cursor = db.execute("INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?)",
                                    (item.item_id, item.published_at.isoformat(), source.provider,
                                     json.dumps(public(item), sort_keys=True, separators=(",", ":"))))
                inserted += cursor.rowcount
            return inserted

    def recent_quotes(self, *, symbols: list[str] | None = None, limit: int = 200) -> list[dict[str, object]]:
        query, parameters = ("SELECT q.payload FROM quotes q JOIN "
                             "(SELECT symbol, MAX(observed_at) observed_at FROM quotes GROUP BY symbol) latest "
                             "ON q.symbol=latest.symbol AND q.observed_at=latest.observed_at"), []
        if symbols:
            query += f" WHERE q.symbol IN ({','.join('?' for _ in symbols)})"
            parameters.extend(symbols)
        query += " ORDER BY q.observed_at DESC LIMIT ?"
        parameters.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, parameters).fetchall()]

    def recent_news(self, *, limit: int = 50) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM news ORDER BY published_at DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()]

    def save_social_posts(self, posts: list[object]) -> int:
        inserted = 0
        with self._lock, self._connect() as db:
            for post in posts:
                value = public(post)
                cursor = db.execute("INSERT OR IGNORE INTO social_posts VALUES (?,?,?,?)",
                    (value["post_id"], value["published_at"], value["platform"], json.dumps(value, sort_keys=True)))
                inserted += cursor.rowcount
        return inserted

    def social_posts(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM social_posts ORDER BY published_at DESC LIMIT ?", (max(1, min(limit, 500)),))]

    def save_social_signals(self, signals: list[object]) -> int:
        inserted = 0
        with self._lock, self._connect() as db:
            for signal in signals:
                value = public(signal)
                cursor = db.execute("INSERT OR IGNORE INTO social_signals VALUES (?,?,?)",
                    (value["signal_id"], value["detected_at"], json.dumps(value, sort_keys=True)))
                inserted += cursor.rowcount
        return inserted

    def social_signals(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM social_signals ORDER BY detected_at DESC LIMIT ?", (max(1, min(limit, 500)),))]

    def save_paper_trade(self, trade: object) -> bool:
        value = public(trade)
        with self._lock, self._connect() as db:
            return db.execute("INSERT OR IGNORE INTO paper_trades VALUES (?,?,?)",
                (value["trade_id"], value["exit_at"], json.dumps(value, sort_keys=True))).rowcount == 1

    def paper_trades(self, limit: int = 500) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM paper_trades ORDER BY exit_at DESC LIMIT ?", (max(1, min(limit, 1000)),))]

    def news_by_ids(self, item_ids: list[str]) -> list[dict[str, object]]:
        if not item_ids:
            return []
        with self._lock, self._connect() as db:
            rows = db.execute(f"SELECT payload FROM news WHERE item_id IN ({','.join('?' for _ in item_ids)})",
                              item_ids).fetchall()
        values = {item["item_id"]: item for item in (json.loads(row[0]) for row in rows)}
        return [values[item_id] for item_id in item_ids if item_id in values]

    def save_events(self, events: list[GodEyeEvent]) -> int:
        inserted = 0
        with self._lock, self._connect() as db:
            for event in events:
                existing = db.execute("SELECT payload FROM god_eye_events WHERE event_id=?", (event.event_id,)).fetchone()
                payload = public(event)
                if existing:
                    previous = json.loads(existing[0])
                    for key in ("source_ids", "evidence_refs", "entities", "instruments"):
                        payload[key] = list(dict.fromkeys([*previous.get(key, []), *payload.get(key, [])]))
                    payload["first_seen_at"] = min(previous["first_seen_at"], payload["first_seen_at"])
                    payload["novelty_score"] = 0.0
                else:
                    inserted += 1
                db.execute("INSERT OR REPLACE INTO god_eye_events VALUES (?, ?, ?)",
                           (event.event_id, event.published_at.isoformat(),
                            json.dumps(payload, sort_keys=True, separators=(",", ":"))))
        return inserted

    def events(self, *, limit: int = 100, event_type: str | None = None,
               instrument: str | None = None, start: str | None = None,
               end: str | None = None) -> list[dict[str, object]]:
        query, params = "SELECT payload FROM god_eye_events", []
        clauses = []
        if event_type:
            clauses.append("json_extract(payload, '$.event_type')=?")
            params.append(event_type)
        if instrument:
            clauses.append("EXISTS (SELECT 1 FROM json_each(json_extract(payload, '$.instruments')) WHERE value=?)")
            params.append(instrument)
        if start:
            clauses.append("occurred_at>=?"); params.append(start)
        if end:
            clauses.append("occurred_at<=?"); params.append(end)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params).fetchall()]

    def event(self, event_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM god_eye_events WHERE event_id=?", (event_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def enrichment(self, event_id: str, fingerprint: str | None = None) -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT fingerprint,payload,model,provider,created_at FROM event_enrichments WHERE event_id=?",
                             (event_id,)).fetchone()
        if not row or (fingerprint is not None and row["fingerprint"] != fingerprint):
            return None
        return {"analysis": json.loads(row["payload"]), "fingerprint": row["fingerprint"],
                "model": row["model"], "provider": row["provider"], "created_at": row["created_at"]}

    def save_enrichment(self, event_id: str, fingerprint: str, created_at: str, analysis: dict[str, object],
                        *, model: str | None = None, provider: str | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO event_enrichments VALUES (?,?,?,?,?,?)",
                       (event_id, fingerprint, created_at, json.dumps(analysis, sort_keys=True), model, provider))

    def save_outcome(self, outcome: EventOutcome) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO event_outcomes VALUES (?,?,?,?,?)",
                       (outcome.outcome_id, outcome.event_id, outcome.instrument, outcome.calculated_at.isoformat(),
                        json.dumps(public(outcome), sort_keys=True, separators=(",", ":"))))

    def outcomes_for_event(self, event_id: str) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM event_outcomes WHERE event_id=? ORDER BY instrument", (event_id,)).fetchall()]

    def save_forecast(self, forecast: ForecastRecord) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO forecasts VALUES (?,?,?)",
                                (forecast.forecast_id, forecast.created_at.isoformat(),
                                 json.dumps(public(forecast), sort_keys=True, separators=(",", ":"))))
            return cursor.rowcount == 1

    def forecasts(self, *, limit: int = 100, instrument: str | None = None,
                  start: str | None = None, end: str | None = None) -> list[dict[str, object]]:
        query, params = "SELECT payload FROM forecasts", []
        clauses = []
        if instrument:
            clauses.append("json_extract(payload, '$.instrument')=?"); params.append(instrument)
        if start:
            clauses.append("created_at>=?"); params.append(start)
        if end:
            clauses.append("created_at<=?"); params.append(end)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"; params.append(max(1, min(limit, 500)))
        with self._lock, self._connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params).fetchall()]

    def compatible_forecast(self, *, instrument: str, horizon: str, model_id: str,
                            model_version: str, calibration_version: str | None,
                            created_after: str) -> dict[str, object] | None:
        """Return only an identity-compatible cache entry, independent of global row volume."""
        with self._lock, self._connect() as db:
            row = db.execute("""SELECT payload FROM forecasts
                WHERE created_at>=?
                  AND json_extract(payload,'$.instrument')=?
                  AND json_extract(payload,'$.horizon')=?
                  AND json_extract(payload,'$.model_id')=?
                  AND json_extract(payload,'$.model_version')=?
                  AND COALESCE(json_extract(payload,'$.calibration_version'),'')=COALESCE(?, '')
                ORDER BY created_at DESC LIMIT 1""",
                (created_after, instrument, horizon, model_id, model_version, calibration_version)).fetchone()
            return json.loads(row[0]) if row else None

    def resolved_forecasts(self, *, limit: int = 500) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            rows = db.execute("""SELECT f.payload,e.payload FROM forecasts f JOIN forecast_evaluations e
                ON e.forecast_id=f.forecast_id ORDER BY f.created_at LIMIT ?""", (max(1, min(limit, 5000)),)).fetchall()
        result = []
        for row in rows:
            value = json.loads(row[0]); value["evaluation"] = json.loads(row[1]); result.append(value)
        return result

    def save_calibration(self, value: dict[str, object], built_at: str) -> bool:
        key = f'{value["model_id"]}|{value["horizon"]}|{value["calibration_version"]}'
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT OR REPLACE INTO calibrations VALUES (?,?,?)",
                                (key, built_at, json.dumps(value, sort_keys=True)))
            return cursor.rowcount > 0

    def calibrations(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM calibrations ORDER BY built_at DESC")]

    def save_regime(self, key: str, value: dict[str, object], detected_at: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO regimes VALUES (?,?,?)", (key, detected_at, json.dumps(value, sort_keys=True)))

    def regimes(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM regimes ORDER BY detected_at DESC")]

    def save_opportunity(self, value: dict[str, object], created_at: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO opportunities VALUES (?,?,?)",
                                (value["opportunity_id"], created_at, json.dumps(value, sort_keys=True)))
            return cursor.rowcount == 1

    def opportunities(self, *, include_rejected: bool = False, limit: int = 100) -> list[dict[str, object]]:
        query, params = "SELECT payload FROM opportunities", []
        if not include_rejected:
            query += " WHERE json_extract(payload, '$.status')='eligible'"
        query += " ORDER BY json_extract(payload, '$.opportunity_score') DESC LIMIT ?"; params.append(limit)
        with self._lock, self._connect() as db:
            return [json.loads(r[0]) for r in db.execute(query, params)]

    def opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM opportunities WHERE opportunity_id=?", (opportunity_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def forecast(self, forecast_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
            if not row:
                return None
            value = json.loads(row[0])
            evaluation = db.execute("SELECT payload FROM forecast_evaluations WHERE forecast_id=?", (forecast_id,)).fetchone()
            if evaluation:
                value["evaluation"] = json.loads(evaluation[0])
            return value

    def save_evaluation(self, forecast_id: str, evaluation: dict[str, object]) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO forecast_evaluations VALUES (?,?,?)",
                (forecast_id, str(evaluation["evaluated_at"]), json.dumps(evaluation, sort_keys=True)))
            if cursor.rowcount:
                row = db.execute("SELECT payload FROM forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
                if row:
                    forecast = json.loads(row[0]); forecast["status"] = "evaluated"; forecast["outcome_ref"] = forecast_id
                    db.execute("UPDATE forecasts SET payload=? WHERE forecast_id=?",
                               (json.dumps(forecast, sort_keys=True, separators=(",", ":")), forecast_id))
            return cursor.rowcount == 1

    def performance(self) -> list[dict[str, object]]:
        forecasts = self.forecasts(limit=500)
        groups: dict[tuple[str, str, str, str, str, str], list[tuple[dict[str, object], dict[str, object] | None]]] = {}
        for forecast in forecasts:
            full = self.forecast(str(forecast["forecast_id"]))
            evaluation = full.get("evaluation") if full else None
            snapshot = forecast.get("feature_snapshot", {})
            key = (str(forecast["model_id"]), str(forecast["model_version"]), str(forecast["horizon"]),
                   str(snapshot.get("local_regime", snapshot.get("market_regime", "unknown"))),
                   str(snapshot.get("event_type", "unknown")), str(forecast.get("instrument", "unknown")))
            groups.setdefault(key, []).append((forecast, evaluation))
        result = []
        for (model_id, version, horizon, regime, event_type, instrument), values in groups.items():
            resolved = [(f, e) for f, e in values if e]
            sufficient = len(resolved) >= 5
            result.append({"model_id": model_id, "model_version": version, "horizon": horizon,
                "regime": regime, "event_type": event_type, "instrument": instrument,
                "count": len(values), "sample_sufficient": sufficient, "minimum_metric_samples": 5,
                "directional_accuracy": (sum(bool(e["directionally_correct"]) for _, e in resolved) / len(resolved)) if sufficient else None,
                "average_return_conditioned_by_signal": (sum(float(e["actual_return"]) for _, e in resolved) / len(resolved)) if sufficient else None,
                "average_forecast_score": sum(float(f["raw_score"]) for f, _ in values) / len(values),
                "unresolved_count": len(values) - len(resolved)})
        return result

    def record_source_result(self, source_id: str, *, success: bool, timestamp: str, error: str | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO source_stats(source_id) VALUES (?)", (source_id,))
            if success:
                db.execute("UPDATE source_stats SET successes=successes+1,last_success=? WHERE source_id=?",
                           (timestamp, source_id))
            else:
                db.execute("UPDATE source_stats SET errors=errors+1,last_error=?,last_error_category=? WHERE source_id=?",
                           (timestamp, error, source_id))

    def record_run(self, stream: str, status: str, timestamp: str, error: str | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO ingestion_runs(stream,collected_at,status,error) VALUES (?,?,?,?)",
                       (stream, timestamp, status, error))
            db.execute("DELETE FROM ingestion_runs WHERE sequence <= (SELECT MAX(sequence)-100 FROM ingestion_runs)")

    def operational_health(self) -> dict[str, object]:
        with self._lock, self._connect() as db:
            stats = [dict(row) for row in db.execute("SELECT * FROM source_stats ORDER BY source_id")]
            runs = [dict(row) for row in db.execute(
                "SELECT stream,collected_at,status,error FROM ingestion_runs ORDER BY sequence DESC LIMIT 20")]
            latest_market = db.execute("SELECT MAX(observed_at) FROM quotes").fetchone()[0]
            latest_news = db.execute("SELECT MAX(published_at) FROM news").fetchone()[0]
            ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return {"db_status": "ok" if ok else "error", "source_stats": stats, "recent_runs": runs,
                "latest_market_timestamp": latest_market, "latest_news_timestamp": latest_news}

    def source_health(self) -> list[dict[str, str]]:
        with self._lock, self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT provider, source_url, retrieved_at, provenance FROM sources ORDER BY provider"
            ).fetchall()]
