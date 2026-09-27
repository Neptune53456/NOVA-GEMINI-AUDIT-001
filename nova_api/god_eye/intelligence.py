"""Deterministic Event Intelligence V1 and inactive V2 enrichment contract."""
from __future__ import annotations

from hashlib import sha256
import re
from typing import Protocol

from .models import GodEyeEvent, MarketInstrument, NewsItem, NewsSourceConfig, utc_now
from .social.models import SocialSignal


EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security_incident", ("cyberattack", "data breach", "ransomware", "security incident", "hack")),
    ("central_bank", ("central bank", "federal reserve", "ecb", "interest rate", "monetary policy", "fomc")),
    ("crypto/regulatory", ("crypto regulation", "bitcoin regulation", "digital asset rule", "token regulation")),
    ("earnings/results", ("earnings", "results", "quarterly results", "annual results", "revenue", "net income")),
    ("guidance", ("guidance", "outlook", "forecast update")),
    ("acquisition", ("acquisition", "acquire", "merger", "takeover")),
    ("partnership", ("partnership", "partners with", "collaboration", "joint venture")),
    ("litigation", ("lawsuit", "litigation", "court", "sued", "settlement")),
    ("regulation", ("regulation", "regulator", "antitrust", "rulemaking", "compliance")),
    ("management", ("chief executive", "ceo", "cfo", "resigns", "appointed")),
    ("analyst_rating", ("rating", "price target", "upgrade", "downgrade", "analyst")),
    ("product", ("launches", "product", "release", "unveils")),
    ("macro", ("inflation", "gdp", "unemployment", "payroll", "consumer prices", "pmi")),
)


class EventEnricher(Protocol):
    """Phase-2 boundary for model_router/OmniRoute; no implementation is invoked in V1."""
    def enrich(self, event: GodEyeEvent) -> GodEyeEvent: ...


class EventIntelligence:
    def __init__(self, instruments: list[MarketInstrument], sources: dict[str, NewsSourceConfig]) -> None:
        self.instruments, self.sources = instruments, sources

    def build(self, items: list[NewsItem], existing: list[dict[str, object]] | None = None,
              social_signals: list[SocialSignal] | None = None) -> list[GodEyeEvent]:
        groups: dict[str, list[NewsItem]] = {}
        for item in items:
            groups.setdefault(self._cluster_key(item), []).append(item)
        known = {str(event.get("event_id")) for event in existing or []}
        events = [self._event(key, group, key not in known) for key, group in groups.items()]
        for signal in social_signals or []:
            match = next((i for i, event in enumerate(events) if set(event.instruments) & set(signal.instruments)), None)
            evidence = {"type": "social", "source": ",".join(signal.source_ids), "ref": signal.signal_id}
            if match is not None:
                event = events[match]
                events[match] = GodEyeEvent(**{**event.__dict__,
                    "source_ids": tuple(dict.fromkeys((*event.source_ids, *signal.source_ids))),
                    "evidence_refs": tuple(dict.fromkeys((*event.evidence_refs, *signal.evidence_refs))),
                    "evidence": (*event.evidence, evidence)})
            else:
                eid = sha256(f"social|{signal.signal_id}".encode()).hexdigest()
                events.append(GodEyeEvent(eid, f"Social {signal.signal_type}", "Deterministic social signal",
                    "social", signal.entities, signal.instruments, signal.source_ids, signal.detected_at,
                    signal.detected_at, float(signal.metrics.get("novelty", 0)), signal.score,
                    {"label": "deterministic_evidence_strength", "score": signal.score},
                    signal.evidence_refs, evidence=(evidence,)))
        return events

    def _cluster_key(self, item: NewsItem) -> str:
        event_type = classify_event(item)
        entities, instruments = match_entities(item, self.instruments)
        day = item.published_at.date().isoformat()
        topic = entities[0] if entities else (instruments[0] if instruments else _keywords(item.title)[:40])
        return sha256(f"{event_type}|{topic.casefold()}|{day}".encode()).hexdigest()

    def _event(self, event_id: str, items: list[NewsItem], novel: bool) -> GodEyeEvent:
        ranked = sorted(items, key=lambda item: self._source(item).reliability_prior, reverse=True)
        lead = ranked[0]
        entities = sorted({value for item in items for value in match_entities(item, self.instruments)[0]})
        instruments = sorted({value for item in items for value in match_entities(item, self.instruments)[1]})
        source_ids = tuple(dict.fromkeys(item.source.provider for item in ranked))
        source_quality = sum(self._source(item).reliability_prior for item in ranked) / len(ranked)
        first_seen = min(item.source.retrieved_at for item in items)
        published = min(item.published_at for item in items)
        evidence = tuple(dict.fromkeys(item.item_id for item in ranked))
        return GodEyeEvent(event_id, lead.title[:240], lead.summary[:500], classify_event(lead), tuple(entities),
                           tuple(instruments), source_ids, published, first_seen, 1.0 if novel else 0.0,
                           round(source_quality, 3),
                           {"label": "deterministic_evidence_strength",
                            "score": round(min(1.0, 0.45 + 0.1 * len(evidence) + 0.25 * source_quality), 3)},
                           evidence, evidence=tuple({"type": "news", "source": item.source.provider,
                                                     "ref": item.item_id} for item in ranked))

    def _source(self, item: NewsItem) -> NewsSourceConfig:
        return self.sources.get(item.source.provider, NewsSourceConfig(
            item.source.provider, item.source.provider, item.source.source_url, "unknown", 0.5,
            (), official=False))


def classify_event(item: NewsItem) -> str:
    text = f"{item.title} {item.summary}".casefold()
    for event_type, terms in EVENT_RULES:
        if any(term in text for term in terms):
            return event_type
    return "other"


def match_entities(item: NewsItem, instruments: list[MarketInstrument]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    text = f"{item.title} {item.summary}".casefold()
    entities = set(item.entities)
    symbols = set(item.tickers)
    for instrument in instruments:
        aliases = (instrument.name, instrument.symbol, *instrument.entities)
        if any(_term(alias, text) for alias in aliases if len(alias) > 1):
            symbols.add(instrument.symbol)
            entities.update(instrument.entities or (instrument.name,))
    return tuple(sorted(entities)), tuple(sorted(symbols))


def _term(value: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", text) is not None


def _keywords(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold())[:6])
