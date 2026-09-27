"""Strict, budgeted model-router enrichment for eligible fused events."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


class EventAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    financial_summary: str = Field(max_length=600)
    entities: list[str] = Field(max_length=20)
    instruments: list[str] = Field(max_length=20)
    impact_type: str = Field(max_length=80)
    potential_direction: str
    horizons: list[str] = Field(max_length=5)
    importance: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    interpretation_confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(max_length=10)
    reasoning_summary: str = Field(max_length=300)
    contradictions: list[str] = Field(max_length=10)


ALLOWED_DIRECTIONS = {"positive", "negative", "mixed", "unclear"}
ALLOWED_HORIZONS = {"minutes", "hours", "1d", "7d", "longer"}


class EnrichmentGate:
    IMPORTANT_TYPES = {"earnings/results", "guidance", "regulation", "central_bank", "macro",
                       "acquisition", "litigation", "security_incident", "crypto/regulatory"}

    def eligible(self, event: dict[str, Any], *, remaining_budget: int) -> tuple[bool, str]:
        if remaining_budget < 1:
            return False, "budget_exhausted"
        if float(event.get("novelty_score", 0)) < 0.5:
            return False, "not_novel"
        if float(event.get("source_quality_score", 0)) < 0.65:
            return False, "low_source_quality"
        if not event.get("instruments"):
            return False, "no_instrument"
        if event.get("event_type") not in self.IMPORTANT_TYPES and float(event.get("confidence", {}).get("score", 0)) >= 0.75:
            return False, "trivial_or_unambiguous"
        return True, "eligible"


class ModelRouterEventEnricher:
    def __init__(self, call: Callable[..., dict[str, Any]] | None = None, *, timeout_seconds: float = 12.0,
                 max_output_tokens: int = 350) -> None:
        self.call, self.timeout_seconds, self.max_output_tokens = call, timeout_seconds, max_output_tokens

    def enrich(self, event: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        call = self.call
        if call is None:
            from model_router import chat
            call = chat
        compact = [{"item_id": item["item_id"], "title": str(item["title"])[:240],
                    "summary": str(item.get("summary", ""))[:500],
                    "source_id": item["source"]["provider"]} for item in evidence[:5]]
        messages = [
            {"role": "system", "content": "Analyze the financial event. Return only schema-valid JSON. Never provide BUY/SELL advice or market probabilities."},
            {"role": "user", "content": json.dumps({"event": {key: event.get(key) for key in
                ("event_id", "event_type", "entities", "instruments", "novelty_score", "source_quality_score")},
                "evidence": compact}, separators=(",", ":"))},
        ]
        schema = EventAnalysis.model_json_schema()
        response = call(messages, task_type="classification", format=schema,
                        options={"temperature": 0, "max_tokens": self.max_output_tokens,
                                 "num_predict": self.max_output_tokens},
                        timeout_seconds=self.timeout_seconds, required_capabilities={"structured_output"})
        content = response.get("message", {}).get("content")
        if isinstance(content, str):
            content = json.loads(content)
        analysis = EventAnalysis.model_validate(content).model_dump()
        if analysis["potential_direction"] not in ALLOWED_DIRECTIONS:
            raise ValueError("invalid_impact_direction")
        if not set(analysis["horizons"]).issubset(ALLOWED_HORIZONS):
            raise ValueError("invalid_horizon")
        if not set(analysis["evidence_refs"]).issubset({item["item_id"] for item in compact}):
            raise ValueError("unknown_evidence_ref")
        meta = dict(response.get("_meta", {})) if isinstance(response.get("_meta"), dict) else {}
        meta["usage"] = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return analysis, meta


def event_fingerprint(event: dict[str, Any]) -> str:
    stable = {key: event.get(key) for key in ("event_id", "event_type", "entities", "instruments",
                                               "source_ids", "evidence_refs", "novelty_score")}
    return sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
