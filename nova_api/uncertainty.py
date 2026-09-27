"""Deterministic uncertainty signals for bounded Nova decisions."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal, Mapping

UncertaintyLevel = Literal["low", "medium", "high", "critical"]

@dataclass(frozen=True)
class UncertaintyAssessment:
    score: float
    level: UncertaintyLevel
    reasons: tuple[str, ...]
    evidence_quality: float
    action_policy: str

    def public(self) -> dict[str, Any]:
        return {"score": self.score, "level": self.level, "reasons": list(self.reasons),
                "evidence_quality": self.evidence_quality, "action_policy": self.action_policy}

class UncertaintyEngine:
    """Combine structural signals. Model self-confidence is deliberately non-authoritative."""
    def assess(self, signals: Mapping[str, Any] | None = None) -> UncertaintyAssessment:
        s = dict(signals or {})
        score = 0.0; reasons: list[str] = []
        def add(value: float, reason: str) -> None:
            nonlocal score
            score += value; reasons.append(reason)
        if s.get("ambiguous_target"): add(.28, "ambiguous_target")
        if s.get("visual_only_target"): add(.22, "visual_only_target")
        if s.get("stale_reference"): add(.16, "stale_reference")
        if s.get("verification_failed"): add(.30, "verification_failed")
        if int(s.get("replans") or 0) >= 2: add(.26, "repeated_replan")
        if int(s.get("same_strategy_failures") or 0) >= 2: add(.30, "repeated_strategy_failure")
        if s.get("memory_conflict"): add(.20, "memory_conflict")
        if s.get("provider_fallback"): add(.10, "provider_fallback")
        if int(s.get("provider_failures") or 0) >= 2: add(.18, "provider_instability")
        if s.get("missing_context"): add(.22, "missing_context")
        if s.get("irreversible"): add(.10, "irreversible_effect")
        if str(s.get("risk_level") or "low") in {"high", "critical"}: add(.10, "high_risk_context")
        evidence_quality = max(0.0, min(1.0, float(s.get("evidence_quality", 1.0))))
        if evidence_quality < .5: add((.5-evidence_quality) * .4, "weak_evidence")
        score = round(min(1.0, score), 3)
        if score >= .80: level: UncertaintyLevel = "critical"
        elif score >= .50: level = "high"
        elif score >= .22: level = "medium"
        else: level = "low"
        policy = {"low":"execute", "medium":"verify_more", "high":"reobserve_or_replan",
                  "critical":"deliberate_or_confirm"}[level]
        return UncertaintyAssessment(score, level, tuple(dict.fromkeys(reasons)), evidence_quality, policy)
