"""Baseline Versioning for Multi-Cycle Self-Improvement V7.2

This module manages versioned snapshots of baseline states across multiple improvement cycles.

Key responsibility:
- Maintain immutable baseline versions
- Track cycle dependencies (baseline -> candidate -> new baseline)
- Enable rollback to any previous baseline
- Prevent confusion between cycle baselines
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BaselineSnapshot:
    """Immutable snapshot of repository state at a baseline."""
    baseline_id: str  # UUID or deterministic hash
    cycle_id: int
    timestamp: str  # ISO 8601 UTC
    repository_digest: str  # SHA256 of relevant files
    train_score: float
    validation_score: float
    security_score: float
    test_count: int
    test_failed_count: int
    dataset_version: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateSnapshot:
    """Immutable snapshot of a candidate improvement attempt."""
    candidate_id: str
    baseline_id: str  # Points to the baseline it was derived from
    cycle_id: int
    timestamp: str  # ISO 8601 UTC
    repository_digest: str  # SHA256 of relevant files post-patch
    train_score: float
    validation_score: float
    security_score: float
    test_count: int
    test_failed_count: int
    changed_files: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CycleDecision:
    """Immutable record of a cycle's decision."""
    cycle_id: int
    decision: str  # ACCEPT | REJECT | ROLLBACK | UNCERTAIN | INCONCLUSIVE
    baseline_id: str
    candidate_id: str | None
    reason: str
    train_improvement: float
    timestamp: str  # ISO 8601 UTC
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineVersionHistory:
    """Tracks progression of baselines across cycles.
    
    Invariant:
    - Each ACCEPT transition creates: baseline[N] -> candidate[N] -> baseline[N+1]
    - Each REJECT keeps: baseline[N] active, no new baseline
    - Rollback can restore any previous baseline_id
    """
    run_id: str
    initial_baseline_id: str
    baselines: dict[str, BaselineSnapshot] = field(default_factory=dict)
    candidates: dict[str, CandidateSnapshot] = field(default_factory=dict)
    decisions: list[CycleDecision] = field(default_factory=list)
    current_baseline_id: str | None = None

    def add_baseline(self, snapshot: BaselineSnapshot) -> None:
        """Record a baseline snapshot. Should be called at cycle start."""
        self.baselines[snapshot.baseline_id] = snapshot
        self.current_baseline_id = snapshot.baseline_id

    def add_candidate(self, snapshot: CandidateSnapshot) -> None:
        """Record a candidate snapshot. Should be called after engineering attempt."""
        self.candidates[snapshot.candidate_id] = snapshot

    def record_decision(self, decision: CycleDecision) -> None:
        """Record the cycle decision. Immutable once appended."""
        self.decisions.append(decision)
        # After ACCEPT, the candidate becomes the new baseline for next cycle
        if decision.decision == "ACCEPT" and decision.candidate_id:
            self.current_baseline_id = decision.candidate_id

    def get_current_baseline(self) -> BaselineSnapshot | CandidateSnapshot | None:
        """Get the currently active baseline for the next cycle."""
        if not self.current_baseline_id:
            return None
        # Check if it's a baseline or a candidate (ACCEPT'd candidate)
        return (
            self.baselines.get(self.current_baseline_id) or
            self.candidates.get(self.current_baseline_id)
        )

    def get_baseline_at_cycle(self, cycle_id: int) -> BaselineSnapshot | None:
        """Retrieve the baseline that was active at a specific cycle."""
        for baseline in self.baselines.values():
            if baseline.cycle_id == cycle_id:
                return baseline
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "initial_baseline_id": self.initial_baseline_id,
            "current_baseline_id": self.current_baseline_id,
            "baselines": {k: v.to_dict() for k, v in self.baselines.items()},
            "candidates": {k: v.to_dict() for k, v in self.candidates.items()},
            "decisions": [d.to_dict() for d in self.decisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaselineVersionHistory:
        """Deserialize from dict, reconstructing immutable snapshots."""
        baselines = {
            k: BaselineSnapshot(**v)
            for k, v in data.get("baselines", {}).items()
        }
        candidates = {
            k: CandidateSnapshot(**v)
            for k, v in data.get("candidates", {}).items()
        }
        decisions = [
            CycleDecision(**d)
            for d in data.get("decisions", [])
        ]
        return cls(
            run_id=data.get("run_id", "unknown"),
            initial_baseline_id=data.get("initial_baseline_id", ""),
            baselines=baselines,
            candidates=candidates,
            decisions=decisions,
            current_baseline_id=data.get("current_baseline_id"),
        )


@dataclass(frozen=True)
class FailureFingerprint:
    """Compact fingerprint to detect repeated failed experiments.
    
    Used to prevent retry of:
    - same component + same strategy
    - same failure category
    - same touched symbols
    """
    component: str  # e.g. "test_agent.py"
    failure_category: str  # e.g. "performance", "regression", "compilation"
    strategy_family: str  # e.g. "caching", "memoization", "refactoring"
    touched_symbols: frozenset[str]  # e.g. frozenset({"foo()", "bar.baz"})
    outcome: str  # "REGRESSED", "FAILED_TESTS", "SYNTAX_ERROR"

    def __hash__(self) -> int:
        return hash((self.component, self.failure_category, self.strategy_family, self.touched_symbols, self.outcome))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FailureFingerprint):
            return False
        return (
            self.component == other.component and
            self.failure_category == other.failure_category and
            self.strategy_family == other.strategy_family and
            self.touched_symbols == other.touched_symbols and
            self.outcome == other.outcome
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "failure_category": self.failure_category,
            "strategy_family": self.strategy_family,
            "touched_symbols": sorted(self.touched_symbols),
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailureFingerprint:
        return cls(
            component=data.get("component", ""),
            failure_category=data.get("failure_category", ""),
            strategy_family=data.get("strategy_family", ""),
            touched_symbols=frozenset(data.get("touched_symbols", [])),
            outcome=data.get("outcome", ""),
        )


@dataclass
class ImprovementProposal:
    """Versioned proposal for an improvement, with rollback plan.
    
    Ensures:
    - Proposal is derived from measured TRAIN evidence
    - Scope is bounded
    - Not a repeat of a known failure
    - Includes concrete rollback steps
    """
    proposal_id: str
    cycle_id: int
    timestamp: str  # ISO 8601 UTC
    
    # What problem are we solving?
    target_component: str  # e.g. "test_agent.py"
    observed_problem: str  # Why we think this needs fixing
    public_train_evidence: list[str]  # Explicit TRAIN failures or metrics
    
    # How do we solve it?
    proposed_strategy: str  # e.g. "add caching to reduce overhead"
    expected_gain: float  # Expected improvement (if measurable)
    risk_level: str  # LOW | MEDIUM | HIGH
    expected_files: list[str]  # Predicted modified files
    
    # Safety & validation
    benchmark_plan: str  # How we'll measure success
    rollback_plan: str  # How to undo if it fails
    
    # Gating
    rejected_by_gate: bool = False
    gate_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImprovementProposal:
        known_fields = cls.__dataclass_fields__
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


def make_digest(content: str | bytes) -> str:
    """Create a deterministic SHA256 digest."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def make_baseline_id(cycle_id: int, repository_digest: str) -> str:
    """Generate a baseline ID from cycle and repo state."""
    combined = f"baseline_cycle{cycle_id}_{repository_digest[:12]}"
    return make_digest(combined)


def make_candidate_id(cycle_id: int, baseline_id: str, repository_digest: str) -> str:
    """Generate a candidate ID from cycle, baseline and repo state."""
    combined = f"candidate_cycle{cycle_id}_{baseline_id[:12]}_{repository_digest[:12]}"
    return make_digest(combined)
