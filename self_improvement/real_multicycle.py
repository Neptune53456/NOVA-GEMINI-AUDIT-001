"""Phase 5 adapter: MultiCycleOrchestrator over the trusted real TRAIN cycle.

The adapter deliberately does not implement an engineering pipeline.  Each cycle
delegates to ``TrustedSelfImprovementSupervisor.run(max_cycles=1)`` so Planner,
Developer, PatchProtocol, tests, Reviewer, Judge and trusted rollback stay owned by
their existing components and execute in the supervisor's fresh process.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Callable

from model_router import ModelCallBudget
from self_improvement.baseline_versioning import (
    BaselineSnapshot, CandidateSnapshot, CycleDecision, ImprovementProposal,
    make_baseline_id, make_candidate_id,
)
from self_improvement.multicycle_orchestrator import (
    CycleState, MultiCycleBudget, MultiCycleOrchestrator, MultiCycleRunReport,
)
from self_improvement.trusted_supervisor import (
    TrustedEvaluationSummary, TrustedSelfImprovementBudget,
    TrustedSelfImprovementOutcome, TrustedSelfImprovementSupervisor,
)


def _repo_digest(root: Path) -> str:
    """Digest public source state without reading evaluation/recovery surfaces."""
    digest = hashlib.sha256()
    ignored = {".git", "benchmark_results", "backups", "__pycache__", ".pytest_cache",
               ".self_improvement_recovery", ".self_improvement_supervisor_recovery"}
    for path in sorted(root.rglob("*.py")):
        if ignored.intersection(part.casefold() for part in path.relative_to(root).parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _snapshot(summary: TrustedEvaluationSummary, root: Path, cycle_id: int) -> BaselineSnapshot:
    repository_digest = _repo_digest(root)
    return BaselineSnapshot(
        baseline_id=make_baseline_id(cycle_id, repository_digest), cycle_id=cycle_id,
        timestamp=datetime.now(timezone.utc).isoformat(), repository_digest=repository_digest,
        train_score=summary.train_score, validation_score=summary.validation_score,
        security_score=summary.security_score, test_count=summary.scenario_count,
        test_failed_count=summary.train_failures, dataset_version=summary.dataset_version,
        metrics=asdict(summary),
    )


class TrustedRealCycleExecutor:
    """Callable bridge carrying the required shared cycle inputs."""

    def __init__(
        self, repo_root: Path, supervisor: TrustedSelfImprovementSupervisor, *,
        global_model_budget: ModelCallBudget, maximum_minutes: float,
        minimum_improvement: float, target_score: float,
        experience_memory: Any = None, provider_state: Any = None,
        max_cycles: int = 3, minimum_safe_budget_for_cycle: int = 4,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.supervisor = supervisor
        self.global_model_budget = global_model_budget
        self.maximum_minutes = maximum_minutes
        self.minimum_improvement = minimum_improvement
        self.target_score = target_score
        self.experience_memory = experience_memory
        self.provider_state = provider_state
        self.max_cycles = max(1, int(max_cycles))
        self.minimum_safe_budget_for_cycle = max(4, int(minimum_safe_budget_for_cycle))

    def __call__(self, cycle_id: int, active_baseline: BaselineSnapshot | CandidateSnapshot) -> CycleState:
        # Reserve a fair share for later cycles.  The real inner pipeline still
        # owns its Planner/Developer/Reviewer/Judge allocation; this parent budget
        # accounts for the measured calls and prevents aggregate overspend.
        remaining = self.global_model_budget.remaining_calls
        if remaining < self.minimum_safe_budget_for_cycle:
            return self._rejected(
                cycle_id, active_baseline, "minimum_safe_cycle_budget_unavailable",
                decision="BUDGET_PROPAGATION_FAILURE",
            )
        future_cycles = max(0, self.max_cycles - cycle_id)
        reserved_for_future = future_cycles * self.minimum_safe_budget_for_cycle
        allocation = max(
            self.minimum_safe_budget_for_cycle,
            remaining - reserved_for_future,
        )
        outcome = self.supervisor.run(
            budget=TrustedSelfImprovementBudget(
                max_cycles=1, max_minutes=max(1.0, self.maximum_minutes),
                minimum_improvement=self.minimum_improvement, target_score=self.target_score,
                max_model_calls_per_cycle=allocation,
            ),
            dry_run=False,
        )
        return self._adapt(cycle_id, active_baseline, outcome)

    def _adapt(self, cycle_id: int, baseline: BaselineSnapshot | CandidateSnapshot,
               outcome: TrustedSelfImprovementOutcome) -> CycleState:
        if not outcome.cycles:
            reason = outcome.reason or "cycle_produced_no_result"
            infra = any(token in reason.casefold() for token in ("provider", "rate", "timeout", "network"))
            return self._infrastructure_result(cycle_id, baseline, reason) if infra else self._rejected(cycle_id, baseline, reason)
        raw = outcome.cycles[-1]
        accepted = raw.decision.upper() == "ACCEPT" and raw.candidate is not None
        candidate = None
        candidate_id = None
        if raw.candidate is not None:
            digest = _repo_digest(self.repo_root)
            candidate_id = make_candidate_id(cycle_id, baseline.baseline_id if isinstance(baseline, BaselineSnapshot) else baseline.candidate_id, digest)
            candidate = CandidateSnapshot(
                candidate_id=candidate_id, baseline_id=self._baseline_id(baseline), cycle_id=cycle_id,
                timestamp=datetime.now(timezone.utc).isoformat(), repository_digest=digest,
                train_score=raw.candidate.train_score, validation_score=raw.candidate.validation_score,
                security_score=raw.candidate.security_score, test_count=raw.candidate.scenario_count,
                test_failed_count=raw.candidate.train_failures, changed_files=list(raw.changed_paths),
                metrics=asdict(raw.candidate),
            )
        reason = raw.reason
        infrastructure = any(token in reason.casefold() for token in ("provider", "rate_limit", "timeout", "network", "all_routes"))
        failure_category = self._failure_category(reason, accepted=accepted)
        decision_name = "ACCEPT" if accepted else ("INFRASTRUCTURE_INCONCLUSIVE" if infrastructure else raw.decision.upper())
        calls = int(raw.details.get("model_calls", 0) or 0)
        if calls > self.global_model_budget.remaining_calls:
            state = self._rejected(
                cycle_id, baseline, "worker_usage_exceeds_global_budget",
                decision="BUDGET_PROPAGATION_FAILURE",
            )
            state.model_calls_used = calls
            return state
        for _ in range(min(calls, self.global_model_budget.remaining_calls)):
            self.global_model_budget.consume()
        proposal = ImprovementProposal(
            proposal_id=f"cycle-{cycle_id}-trusted-objective", cycle_id=cycle_id,
            timestamp=datetime.now(timezone.utc).isoformat(), target_component="TRAIN bottleneck",
            observed_problem=raw.objective, public_train_evidence=list(raw.details.get("scenario_ids", [])),
            proposed_strategy=raw.objective, expected_gain=max(0.0, raw.train_improvement), risk_level="MEDIUM",
            expected_files=list(raw.changed_paths), benchmark_plan="trusted TRAIN re-evaluation",
            rollback_plan="TrustedRepositorySnapshot.restore",
        )
        evidence = raw.details.get("candidate_evidence")
        if not isinstance(evidence, dict):
            evidence = {
                "outcome": "IMPROVED" if accepted else ("REGRESSED" if raw.candidate and raw.train_improvement < 0 else "UNCERTAIN"),
                "baseline_behavior_metrics": asdict(raw.baseline),
                "candidate_behavior_metrics": asdict(raw.candidate) if raw.candidate else {},
                "changed_files": list(raw.changed_paths),
                "evidence_strength": "strong" if accepted else "insufficient",
            }
        decision = CycleDecision(
            cycle_id=cycle_id, decision=decision_name, baseline_id=self._baseline_id(baseline),
            candidate_id=candidate_id, reason=reason, train_improvement=raw.train_improvement,
            timestamp=datetime.now(timezone.utc).isoformat(), details=dict(raw.details),
        )
        return CycleState(
            cycle_id=cycle_id, baseline_id=self._baseline_id(baseline), baseline_snapshot=baseline,
            proposal=proposal, candidate_id=candidate_id, candidate_snapshot=candidate, decision=decision,
            candidate_evidence=evidence, judge_decision=raw.decision.upper(), accepted=accepted,
            rollback_performed=raw.rollback_performed, model_calls_used=calls,
            failure_category=failure_category, infrastructure_status="INCONCLUSIVE" if infrastructure else "OK",
            metrics_before=asdict(raw.baseline), metrics_after=asdict(raw.candidate) if raw.candidate else {},
        )

    @staticmethod
    def _baseline_id(value: BaselineSnapshot | CandidateSnapshot) -> str:
        return value.baseline_id if isinstance(value, BaselineSnapshot) else value.candidate_id

    @staticmethod
    def _failure_category(reason: str, *, accepted: bool = False) -> str | None:
        if accepted:
            return None
        folded = str(reason or "").casefold()
        if "budget" in folded:
            return "BUDGET_PROPAGATION_FAILURE"
        if "worker_missing_output" in folded or "invalid_output" in folded or "serialization" in folded:
            return "MULTIPROCESS_CONTRACT_FAILURE"
        if any(token in folded for token in ("provider", "rate_limit", "timeout", "network", "all_routes")):
            return "INFRASTRUCTURE_FAILURE"
        if "planning" in folded or "planner" in folded or "repair_no_progress" in folded or "unknown_target_reference" in folded:
            return "PLANNER_COGNITIVE_FAILURE"
        return "CANDIDATE_FAILURE"

    def _infrastructure_result(self, cycle_id: int, baseline: BaselineSnapshot | CandidateSnapshot, reason: str) -> CycleState:
        state = self._rejected(cycle_id, baseline, reason, decision="INFRASTRUCTURE_INCONCLUSIVE")
        state.infrastructure_status = "INCONCLUSIVE"
        return state

    def _rejected(self, cycle_id: int, baseline: BaselineSnapshot | CandidateSnapshot, reason: str,
                  decision: str = "UNCERTAIN") -> CycleState:
        baseline_id = self._baseline_id(baseline)
        record = CycleDecision(cycle_id, decision, baseline_id, None, reason, 0.0,
                               datetime.now(timezone.utc).isoformat())
        return CycleState(cycle_id, baseline_id, baseline, decision=record, judge_decision=decision,
                          rollback_performed=True, failure_category=reason,
                          metrics_before=dict(baseline.metrics))


def run_real_multicycle(
    repo_root: Path | str, *, max_cycles: int = 3, max_minutes: float = 45.0,
    minimum_improvement: float = 0.5, target_score: float = 98.0,
    max_model_calls: int = 100, supervisor: TrustedSelfImprovementSupervisor | None = None,
    baseline_summary: TrustedEvaluationSummary | None = None,
) -> MultiCycleRunReport:
    """Run bounded, unattended real TRAIN cycles through the trusted pipeline."""
    root = Path(repo_root).resolve()
    owner = supervisor or TrustedSelfImprovementSupervisor(root)
    if baseline_summary is None:
        baseline_summary = TrustedEvaluationSummary.from_report(owner._evaluate())
    initial = _snapshot(baseline_summary, root, 0)
    global_budget = ModelCallBudget(max_model_calls)
    executor = TrustedRealCycleExecutor(
        root, owner, global_model_budget=global_budget, maximum_minutes=max_minutes,
        minimum_improvement=minimum_improvement, target_score=target_score,
        experience_memory=getattr(owner, "engineering_memory", None), provider_state="BrainPool/ProviderLeaseManager",
        max_cycles=max_cycles,
    )
    orchestrator = MultiCycleOrchestrator(root)
    return orchestrator.run_multicycle(
        initial, executor,
        budget=MultiCycleBudget(max_cycles=max_cycles, max_consecutive_rollbacks=max_cycles,
                                max_total_model_calls=max_model_calls,
                                max_total_duration_seconds=max(60.0, max_minutes * 60.0),
                                min_improvement_per_cycle=minimum_improvement),
    )
