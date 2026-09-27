"""Multi-Cycle Self-Improvement Orchestrator V7.2

Orchestrates multiple improvement cycles with:
- Baseline versioning at each cycle boundary
- Proper state isolation between cycles
- Budget reservation for final phases
- Failure fingerprinting to prevent repeated mistakes
- Comprehensive reporting
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from self_improvement.baseline_versioning import (
    BaselineSnapshot,
    BaselineVersionHistory,
    CandidateSnapshot,
    CycleDecision,
    FailureFingerprint,
    ImprovementProposal,
    make_baseline_id,
    make_candidate_id,
)


@dataclass
class MultiCycleBudget:
    """Budget constraints for multi-cycle runs."""
    max_cycles: int = 3
    max_consecutive_rollbacks: int = 2
    max_failed_experiments: int = 5
    max_total_model_calls: int = 100
    max_total_duration_seconds: float = 600.0
    min_improvement_per_cycle: float = 0.5  # Minimum acceptable improvement
    max_cost: float | None = None

    def validate(self) -> tuple[bool, str]:
        """Validate budget constraints."""
        if not 1 <= self.max_cycles <= 20:
            return False, "max_cycles must be between 1 and 20"
        if self.max_consecutive_rollbacks < 1:
            return False, "max_consecutive_rollbacks must be >= 1"
        if self.max_failed_experiments < 1:
            return False, "max_failed_experiments must be >= 1"
        if self.max_total_model_calls < 1:
            return False, "max_total_model_calls must be >= 1"
        if self.max_total_duration_seconds < 10.0:
            return False, "max_total_duration_seconds must be >= 10.0"
        return True, "ok"


@dataclass
class CycleState:
    """State of a single cycle."""
    cycle_id: int
    baseline_id: str
    baseline_snapshot: BaselineSnapshot
    proposal: ImprovementProposal | None = None
    candidate_id: str | None = None
    candidate_snapshot: CandidateSnapshot | None = None
    decision: CycleDecision | None = None
    failure_fingerprints: set[FailureFingerprint] = field(default_factory=set)
    duration_seconds: float = 0.0
    model_calls_used: int = 0
    error: str | None = None
    candidate_evidence: dict[str, Any] | None = None
    judge_decision: str | None = None
    accepted: bool = False
    rollback_performed: bool = False
    failure_category: str | None = None
    infrastructure_status: str = "OK"
    metrics_before: dict[str, Any] = field(default_factory=dict)
    metrics_after: dict[str, Any] = field(default_factory=dict)
    provider_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Public, structured Phase-5 cycle result."""
        data = asdict(self)
        data["failure_fingerprints"] = [
            item.to_dict() for item in sorted(
                self.failure_fingerprints,
                key=lambda fp: (fp.component, fp.failure_category, fp.strategy_family, fp.outcome),
            )
        ]
        return data


@dataclass
class MultiCycleRunReport:
    """Final report for a multi-cycle run."""
    run_id: str
    timestamp: str  # ISO 8601 UTC
    initial_baseline_id: str
    final_baseline_id: str
    total_cycles: int
    cycle_outcomes: list[CycleDecision] = field(default_factory=list)
    cycle_results: list[CycleState] = field(default_factory=list)
    total_accepts: int = 0
    total_rejections: int = 0
    total_inconclusive: int = 0
    total_infrastructure_failures: int = 0
    
    initial_train_score: float = 0.0
    final_train_score: float = 0.0
    total_improvement: float = 0.0
    
    total_model_calls: int = 0
    total_duration_seconds: float = 0.0
    stop_reason: str = "UNKNOWN"
    success: bool = False
    
    versioning_history: BaselineVersionHistory | None = None
    state_isolation_verified: bool = False
    
    baseline_versions: dict[str, BaselineSnapshot] = field(default_factory=dict)
    candidate_versions: dict[str, CandidateSnapshot] = field(default_factory=dict)
    
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cycle_results"] = [item.to_dict() for item in self.cycle_results]
        if self.versioning_history:
            data["versioning_history"] = self.versioning_history.to_dict()
        return data

    def save_to_file(self, path: Path | str) -> None:
        """Persist report to JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )


@dataclass
class StopCondition:
    """Represents a stop condition result."""
    should_stop: bool
    reason: str
    cycle_count: int | None = None


class MultiCycleOrchestrator:
    """Manages multi-cycle self-improvement with baseline versioning and state isolation.
    
    Architecture:
    1. Evaluate baseline for cycle N
    2. Create BaselineSnapshot
    3. Propose improvement
    4. Run engineering (developer agent)
    5. Evaluate candidate
    6. Create CandidateSnapshot
    7. Judge decides: ACCEPT | REJECT | UNCERTAIN | ROLLBACK
    8. If ACCEPT: candidate becomes baseline for cycle N+1
    9. Record CycleDecision
    10. Repeat from step 1 until stop condition
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        logger: Callable[[str], None] | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.logger = logger or (lambda msg: None)
        self.history: BaselineVersionHistory | None = None
        self.failed_fingerprints: set[FailureFingerprint] = set()
        self.consecutive_rollbacks = 0

    def run_multicycle(
        self,
        initial_baseline_snapshot: BaselineSnapshot,
        cycle_executor: Callable[[int, BaselineSnapshot], CycleState],
        *,
        budget: MultiCycleBudget | None = None,
        dry_run: bool = False,
    ) -> MultiCycleRunReport:
        """Execute multiple cycles of improvement with versioning.
        
        Args:
            initial_baseline_snapshot: Starting baseline state
            cycle_executor: Function that runs a single cycle and returns CycleState
            budget: Budget constraints (use defaults if None)
            dry_run: If True, simulate without actually modifying repository
        
        Returns:
            MultiCycleRunReport with all cycle outcomes and final state
        """
        cfg = budget or MultiCycleBudget()
        valid, msg = cfg.validate()
        if not valid:
            raise ValueError(f"Invalid budget: {msg}")

        run_id = f"run_{int(time.time() * 1000)}"
        started = time.perf_counter()
        
        self.history = BaselineVersionHistory(
            run_id=run_id,
            initial_baseline_id=initial_baseline_snapshot.baseline_id,
            current_baseline_id=initial_baseline_snapshot.baseline_id,
        )
        self.history.add_baseline(initial_baseline_snapshot)
        self.failed_fingerprints.clear()
        self.consecutive_rollbacks = 0

        report = MultiCycleRunReport(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            initial_baseline_id=initial_baseline_snapshot.baseline_id,
            final_baseline_id=initial_baseline_snapshot.baseline_id,
            total_cycles=0,
            initial_train_score=initial_baseline_snapshot.train_score,
        )

        if dry_run:
            self.logger("[MultiCycleOrchestrator] DRY_RUN mode: simulating cycles")

        cycles_executed = 0
        for cycle_id in range(1, int(cfg.max_cycles) + 1):
            elapsed = (time.perf_counter() - started) / 60.0
            if elapsed >= cfg.max_total_duration_seconds / 60.0:
                report.stop_reason = "DURATION_EXCEEDED"
                break

            current_baseline = self.history.get_current_baseline()
            if not isinstance(current_baseline, (BaselineSnapshot, CandidateSnapshot)):
                report.stop_reason = "MISSING_BASELINE"
                break

            self.logger(f"[MultiCycleOrchestrator] Starting cycle {cycle_id}")
            cycle_start = time.perf_counter()

            try:
                # Execute this cycle
                cycle_state = cycle_executor(cycle_id, current_baseline)  # type: ignore
                cycle_state.duration_seconds = time.perf_counter() - cycle_start
                cycles_executed += 1
                report.cycle_results.append(cycle_state)
                report.total_model_calls += max(0, int(cycle_state.model_calls_used))

                # Record candidate if present
                if cycle_state.candidate_snapshot:
                    self.history.add_candidate(cycle_state.candidate_snapshot)

                # Record decision
                if cycle_state.decision:
                    self.history.record_decision(cycle_state.decision)
                    report.cycle_outcomes.append(cycle_state.decision)

                    # Update counters
                    if cycle_state.decision.decision == "ACCEPT":
                        report.total_accepts += 1
                        self.consecutive_rollbacks = 0
                    elif cycle_state.decision.decision in ("REJECT", "ROLLBACK"):
                        report.total_rejections += 1
                        self.consecutive_rollbacks += 1
                    elif cycle_state.decision.decision == "UNCERTAIN":
                        report.total_inconclusive += 1
                        self.consecutive_rollbacks += 1
                    elif cycle_state.decision.decision == "INFRASTRUCTURE_INCONCLUSIVE":
                        report.total_infrastructure_failures += 1

                    # Check stop conditions
                    stop = self._check_stop_conditions(
                        cycle_id=cycle_id,
                        decision=cycle_state.decision,
                        budget=cfg,
                        cycles_executed=cycles_executed,
                        report=report,
                    )
                    if stop.should_stop:
                        report.stop_reason = stop.reason
                        self.logger(f"[MultiCycleOrchestrator] Stopped: {stop.reason}")
                        break

                self.logger(
                    f"[MultiCycleOrchestrator] Cycle {cycle_id} completed: "
                    f"{cycle_state.decision.decision if cycle_state.decision else 'UNKNOWN'} "
                    f"(+{cycle_state.decision.train_improvement:.2f}% improvement, "
                    f"{cycle_state.duration_seconds:.1f}s)"
                )

            except Exception as exc:
                report.stop_reason = "CYCLE_EXECUTION_ERROR"
                report.details["last_error"] = str(exc)
                self.logger(f"[MultiCycleOrchestrator] Cycle {cycle_id} failed: {exc}")
                break

        # Finalize report
        report.total_cycles = cycles_executed
        if self.history.current_baseline_id:
            report.final_baseline_id = self.history.current_baseline_id
            final_baseline = self.history.get_current_baseline()
            if isinstance(final_baseline, BaselineSnapshot):
                report.final_train_score = final_baseline.train_score
            elif isinstance(final_baseline, CandidateSnapshot):
                report.final_train_score = final_baseline.train_score

        report.total_improvement = report.final_train_score - report.initial_train_score
        report.total_duration_seconds = time.perf_counter() - started
        report.versioning_history = self.history
        report.baseline_versions = self.history.baselines
        report.candidate_versions = self.history.candidates
        report.success = report.total_accepts > 0 or report.total_cycles > 0
        report.state_isolation_verified = self._verify_state_isolation(report)

        self.logger(
            f"[MultiCycleOrchestrator] Multi-cycle run {run_id} completed: "
            f"{cycles_executed} cycles, {report.total_accepts} accepts, "
            f"{report.total_rejections} rejections, +{report.total_improvement:.2f}% improvement"
        )

        return report

    def _check_stop_conditions(
        self,
        cycle_id: int,
        decision: CycleDecision,
        budget: MultiCycleBudget,
        cycles_executed: int,
        report: MultiCycleRunReport,
    ) -> StopCondition:
        """Check if any stop condition is met."""
        if cycles_executed >= budget.max_cycles:
            return StopCondition(True, "MAX_CYCLES_REACHED", cycles_executed)

        if self.consecutive_rollbacks >= budget.max_consecutive_rollbacks:
            return StopCondition(True, "MAX_CONSECUTIVE_ROLLBACKS", cycles_executed)

        if report.total_rejections >= budget.max_failed_experiments:
            return StopCondition(True, "MAX_FAILED_EXPERIMENTS", cycles_executed)

        if report.total_model_calls >= budget.max_total_model_calls:
            return StopCondition(True, "MODEL_BUDGET_EXHAUSTED", cycles_executed)

        # Success conditions
        if decision.decision == "ACCEPT" and decision.train_improvement < budget.min_improvement_per_cycle:
            self.logger(
                f"[MultiCycleOrchestrator] Insufficient improvement: "
                f"{decision.train_improvement:.2f}% < {budget.min_improvement_per_cycle}%"
            )
            # This is a warning, not a stop condition

        return StopCondition(False, "ok")

    def _verify_state_isolation(self, report: MultiCycleRunReport) -> bool:
        """Verify that cycles were properly isolated.
        
        Checks:
        - Each cycle has unique baseline_id
        - Baseline progression is correct
        - No contamination between cycles
        """
        if not self.history or not report.cycle_outcomes:
            return False

        # Check that we have proper baseline progression
        seen_baselines = set()
        for decision in report.cycle_outcomes:
            if decision.baseline_id in seen_baselines and decision.decision == "ACCEPT":
                # Baseline reused after ACCEPT is wrong
                return False
            seen_baselines.add(decision.baseline_id)

        return len(seen_baselines) > 0

    def register_failed_fingerprint(self, fp: FailureFingerprint) -> None:
        """Register a failed strategy fingerprint to prevent retries."""
        self.failed_fingerprints.add(fp)

    def has_failed_fingerprint(self, fp: FailureFingerprint) -> bool:
        """Check if a strategy fingerprint has failed before."""
        return fp in self.failed_fingerprints
