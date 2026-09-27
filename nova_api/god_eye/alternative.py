"""Bounded live alternative-data adapters and event conversion."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable, Iterable
import time
from .models import GodEyeEvent, as_utc, utc_now

@dataclass(frozen=True)
class AlternativeDataItem:
    item_id: str; kind: str; source: str; published_at: datetime; entities: tuple[str, ...]
    payload: dict[str, Any]; url: str; ingested_at: datetime = field(default_factory=utc_now)

@dataclass(frozen=True)
class SecEdgarConfig:
    companies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    max_filings_per_company: int = 20
    minimum_request_interval: float = 0.11
    @property
    def active(self) -> bool:
        return bool(self.companies and "@" in self.user_agent and len(self.user_agent) >= 8)

@dataclass(frozen=True)
class MacroSourceConfig:
    source_id: str; url: str; official: bool = True; enabled: bool = True
    @property
    def active(self) -> bool:
        return self.enabled and self.official and self.url.startswith("https://")

def normalize_sec_filing(raw: dict[str, Any], ticker: str) -> AlternativeDataItem:
    accession, form = str(raw["accessionNumber"]), str(raw["form"])
    filed = as_utc(str(raw["filingDate"]) + "T00:00:00+00:00")
    cik = str(raw.get("cik", "")).lstrip("0")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.html"
    payload = {"form": form, "accession": accession, "primary_document": raw.get("primaryDocument"),
               "cik": cik, "filing_date": filed.isoformat()}
    text = raw.get("text")
    if isinstance(text, str) and text.strip(): payload["text"] = text.strip()[:20_000]
    return AlternativeDataItem(sha256(f"sec|{accession}".encode()).hexdigest(), "corporate_filing",
                               "sec_edgar", filed, (ticker,), payload, url)

def normalize_macro_event(raw: dict[str, Any]) -> AlternativeDataItem:
    scheduled = as_utc(raw["scheduled_at"]); actual, consensus = raw.get("actual"), raw.get("consensus")
    surprise = (float(actual) - float(consensus)) if actual is not None and consensus is not None else None
    payload = {"category": raw["category"], "scheduled_at": scheduled.isoformat(), "actual": actual,
               "previous": raw.get("previous"), "consensus": consensus, "surprise": surprise,
               "affected_markets": raw.get("affected_markets", [])}
    source = str(raw.get("source", "official"))
    key = sha256(f"macro|{source}|{raw['category']}|{scheduled.isoformat()}".encode()).hexdigest()
    return AlternativeDataItem(key, "macro_release", source, scheduled, tuple(raw.get("entities", ())),
                               payload, str(raw.get("url", "")))

class SecEdgarCollector:
    """SEC submissions collector. HTTP is injected for policy enforcement and tests."""
    def __init__(self, config: SecEdgarConfig, get_json: Callable[[str, dict[str, str]], Any], *,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config, self.get_json, self.monotonic, self.sleep = config, get_json, monotonic, sleep
        self._last_request: float | None = None
    def collect(self, checkpoint: str | None = None) -> tuple[list[AlternativeDataItem], str | None]:
        if not self.config.active: return [], checkpoint
        items: list[AlternativeDataItem] = []
        for ticker, cik in sorted(self.config.companies.items()):
            if self._last_request is not None:
                remaining = self.config.minimum_request_interval - (self.monotonic() - self._last_request)
                if remaining > 0: self.sleep(remaining)
            url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
            doc = self.get_json(url, {"User-Agent": self.config.user_agent, "Accept-Encoding": "gzip, deflate"})
            self._last_request = self.monotonic()
            recent = (doc or {}).get("filings", {}).get("recent", {})
            keys = ("accessionNumber", "form", "filingDate", "primaryDocument")
            rows = [dict(zip(keys, values)) for values in zip(*(recent.get(key, []) for key in keys))]
            for row in rows[:max(1, min(self.config.max_filings_per_company, 100))]:
                row["cik"] = cik; item = normalize_sec_filing(row, ticker)
                if checkpoint is None or item.published_at.isoformat() > checkpoint: items.append(item)
        return items, max((item.published_at.isoformat() for item in items), default=checkpoint)

class MacroReleaseCollector:
    def __init__(self, sources: Iterable[MacroSourceConfig], get_json: Callable[[str, dict[str, str]], Any]) -> None:
        self.sources, self.get_json = tuple(sources), get_json
    def collect(self, checkpoint: str | None = None) -> tuple[list[AlternativeDataItem], str | None]:
        items: list[AlternativeDataItem] = []
        for source in self.sources:
            if not source.active: continue
            rows = self.get_json(source.url, {"Accept": "application/json"})
            if isinstance(rows, dict): rows = rows.get("releases", [])
            for raw in list(rows or [])[:200]:
                value = dict(raw); value.update(source=source.source_id, url=value.get("url") or source.url)
                item = normalize_macro_event(value)
                if checkpoint is None or item.published_at.isoformat() > checkpoint: items.append(item)
        return items, max((item.published_at.isoformat() for item in items), default=checkpoint)

def alternative_to_event(item: AlternativeDataItem, instruments: Iterable[str] = ()) -> GodEyeEvent:
    form, category = str(item.payload.get("form", "")).upper(), str(item.payload.get("category", "")).casefold()
    if item.kind == "corporate_filing":
        event_type = "earnings/financial_update" if form in {"10-K", "10-Q", "20-F", "6-K"} else "filing"
    elif any(term in category for term in ("central bank", "rate decision", "monetary policy", "fomc", "ecb")): event_type = "central_bank"
    elif any(term in category for term in ("regulatory", "regulation", "enforcement")): event_type = "regulatory"
    else: event_type = "macro_release"
    title = str(item.payload.get("category") or f"{form} filing").strip()
    evidence = ({"ref": item.item_id, "source": item.source, "url": item.url, "kind": item.kind},)
    return GodEyeEvent(item.item_id, title[:240], str(item.payload.get("text", ""))[:500], event_type,
        item.entities, tuple(dict.fromkeys(instruments)), (item.source,), item.published_at, item.ingested_at,
        1.0, 1.0, {"method": "deterministic", "score": 1.0}, (item.item_id,), evidence=evidence)

def capability_status(sec: SecEdgarConfig | None, macro: Iterable[MacroSourceConfig]) -> dict[str, dict[str, Any]]:
    sources = tuple(macro)
    return {"sec_edgar": {"active": bool(sec and sec.active), "mode": "metadata_bounded", "configured": bool(sec)},
        "macro_calendar": {"active": any(item.active for item in sources), "mode": "configured_official",
                           "configured_sources": sum(item.active for item in sources)},
        "earnings_calendar": {"active": False}, "insider_filings": {"active": False},
        "search_trends": {"active": False}, "developer_activity": {"active": False}, "on_chain": {"active": False}}

ALTERNATIVE_CAPABILITIES = capability_status(None, ())
