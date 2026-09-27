"""Trusted supervisor for autonomous self-improvement V5.

This module is part of the Trusted Control Plane (TCB).  The cognitive agent may
improve its Planner, DeveloperAgent and repository intelligence, but it cannot
change the rules that decide whether a candidate survives.

Key property: every engineering cycle runs in a *fresh Python process*.  Changes
to the cognitive agent that are accepted in cycle N are therefore actually loaded
and used by cycle N+1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Any, Callable, Protocol

from self_improvement.agent_path_policy import is_agent_editable_path, is_model_private_path, is_trusted_control_path
from self_improvement.engineering_orchestrator import (
    EngineeringOrchestrator,
    EngineeringTransaction,
    GlobalValidationResult,
)
from self_improvement.engineering_planner import EngineeringObjective
from self_improvement.evaluator import compare_security_results
from self_improvement.models import BenchmarkReport, CriterionResult, ScenarioResult
from self_improvement.process_safety import sanitized_child_environment, model_agent_environment
from self_improvement.learning_curriculum import TrustedTrainCurriculum
from self_improvement.engineering_memory import EngineeringMemory
from self_improvement.trusted_git_checkpoint import TrustedGitCheckpointManager


OPTIMIZATION_SPLIT = "train"
GUARD_SPLIT = "validation"
PUBLIC_SPLITS = frozenset({OPTIMIZATION_SPLIT, GUARD_SPLIT})


class Evaluator(Protocol):
    def evaluate(self) -> BenchmarkReport: ...


@dataclass(frozen=True)
class TrustedSelfImprovementBudget:
    max_cycles: int = 3
    max_minutes: float = 45.0
    minimum_improvement: float = 0.5
    target_score: float = 98.0
    git_checkpoint: bool = False
    max_model_calls_per_cycle: int = 30
    max_tasks_per_cycle: int = 1
    max_source_files: int = 2
    max_diff_lines: int = 120

    def __post_init__(self) -> None:
        from self_improvement.execution_limits import ExecutionLimits
        ExecutionLimits(self.max_tasks_per_cycle, self.max_source_files, self.max_diff_lines,
                        self.max_model_calls_per_cycle, 1.0)
        if type(self.max_cycles) is not int or not 1 <= self.max_cycles <= 10:
            raise ValueError("max_cycles doit être compris entre 1 et 10.")
        if type(self.max_minutes) not in (int, float) or not 1.0 <= self.max_minutes <= 1440.0:
            raise ValueError("max_minutes doit être compris entre 1 et 1440.")
        if not 0.1 <= float(self.minimum_improvement) <= 20.0:
            raise ValueError("minimum_improvement doit être compris entre 0.1 et 20.")
        if not 0.0 <= float(self.target_score) <= 100.0:
            raise ValueError("target_score doit être compris entre 0 et 100.")

        if not 4 <= int(self.max_model_calls_per_cycle) <= 500:
            raise ValueError("max_model_calls_per_cycle must be between 4 and 500")


def supervised_preflight(budget: TrustedSelfImprovementBudget) -> dict[str, Any]:
    """Validate only the execution contract. No dataset, provider or worker access."""
    from self_improvement.execution_limits import ExecutionLimits
    limits = ExecutionLimits(budget.max_tasks_per_cycle, budget.max_source_files,
        budget.max_diff_lines, budget.max_model_calls_per_cycle,
        time.monotonic() + budget.max_minutes * 60.0)
    admitted = budget.max_minutes * 60.0 >= 1110.0
    return {
        "status": "CONFIGURATION_READY" if admitted else "BLOCKED",
        "reason": "provider_reachability_not_checked" if admitted else "INSUFFICIENT_GLOBAL_TIME",
        "execution_limits": limits.to_dict(),
        "minimum_cycle_seconds": 1110.0,
        "evaluation_timeout_seconds": 120.0,
        "repository_validation_timeout_seconds": 180.0,
        "worker_minimum_seconds": 480.0,
        "deadline_behavior": "bounded_subprocesses_cooperative_cleanup_restoration_not_interrupted",
        "real_cycles_started": 0,
        "model_calls": 0,
    }


@dataclass(frozen=True)
class WorkerUsageReport:
    model_calls_total: int = 0
    logical_model_requests: int = 0
    model_calls_by_role: dict[str, int] = field(default_factory=dict)
    provider_attempts: dict[str, int] = field(default_factory=dict)
    fallback_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0
    remaining_local_budget: int = 0
    budget_exhausted: bool = False
    attempt_trace: list[dict[str, Any]] = field(default_factory=list)
    call_value_trace: list[dict[str, Any]] = field(default_factory=list)
    patch_failure_evidence: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "WorkerUsageReport":
        data = dict(raw or {})
        return cls(**{item.name: data[item.name] for item in fields(cls) if item.name in data})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrustedEvaluationSummary:
    train_score: float
    validation_score: float
    security_score: float
    train_failures: int
    scenario_count: int
    dataset_version: str

    @classmethod
    def from_report(cls, report: BenchmarkReport) -> "TrustedEvaluationSummary":
        return cls(
            train_score=round(_split_score(report, OPTIMIZATION_SPLIT), 2),
            validation_score=round(_split_score(report, GUARD_SPLIT), 2),
            security_score=float(report.security_score),
            train_failures=len(_train_failures(report)),
            scenario_count=len(report.results),
            dataset_version=str(report.dataset_version),
        )


@dataclass
class TrustedCycleOutcome:
    cycle: int
    decision: str
    reason: str
    objective: str
    baseline: TrustedEvaluationSummary
    candidate: TrustedEvaluationSummary | None = None
    train_improvement: float = 0.0
    changed_paths: list[str] = field(default_factory=list)
    rollback_performed: bool = False
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass
class TrustedSelfImprovementOutcome:
    final_decision: str
    reason: str
    success: bool
    initial: TrustedEvaluationSummary | None = None
    final: TrustedEvaluationSummary | None = None
    cycles: list[TrustedCycleOutcome] = field(default_factory=list)
    duration_seconds: float = 0.0
    recovered_interrupted_cycle: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_decision": self.final_decision,
            "reason": self.reason,
            "success": self.success,
            "initial": asdict(self.initial) if self.initial else None,
            "final": asdict(self.final) if self.final else None,
            "cycles": [item.to_dict() for item in self.cycles],
            "duration_seconds": self.duration_seconds,
            "recovered_interrupted_cycle": self.recovered_interrupted_cycle,
            "details": dict(self.details),
        }


class TrustedRepositorySnapshot:
    """Byte-for-byte outer snapshot used only by the trusted supervisor.

    Unlike the normal engineering transaction, this snapshot intentionally includes
    private evaluation corpora, local configuration and binary state such as
    ``memory.db``.  None of those bytes are exposed to the model; they exist only so
    the trusted parent can detect and restore a bypass of the normal agent tools.
    """

    IGNORED_PARTS = frozenset({
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".hypothesis",
        ".temp_tests", ".self_improvement_worktrees", ".self_improvement_discoveries",
        ".self_improvement_recovery", ".self_improvement_supervisor_recovery",
        "benchmark_results", "backups", ".runtime", ".mypy_cache", ".ruff_cache", "htmlcov",
        ".coverage", "coverage.xml",
    })
    IGNORED_PREFIXES = ("self_improvement/reports/",)

    def __init__(
        self,
        repo_root: str | Path,
        *,
        max_files: int = 4000,
        max_bytes: int = 100_000_000,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.max_snapshot_files = max(100, int(max_files))
        self.max_snapshot_bytes = max(5_000_000, int(max_bytes))
        self.snapshot: dict[Path, bytes | None] = {}
        self.snapshot_bytes = 0

    def _ignored(self, path: Path) -> bool:
        try:
            rel = path.resolve(strict=False).relative_to(self.repo_root)
        except ValueError:
            return True
        parts = {part.casefold() for part in rel.parts}
        if parts & self.IGNORED_PARTS:
            return True
        lowered = rel.as_posix().casefold()
        return any(lowered.startswith(prefix.casefold()) for prefix in self.IGNORED_PREFIXES)

    def _iter_files(self) -> list[Path]:
        files: list[Path] = []
        for path in self.repo_root.rglob("*"):
            if self._ignored(path):
                continue
            if path.is_symlink():
                raise RuntimeError(f"trusted_snapshot_symlink_unsupported: {path}")
            if path.is_file():
                files.append(path.resolve(strict=False))
        return sorted(set(files), key=lambda item: item.as_posix())

    def capture_repository(self) -> None:
        files = self._iter_files()
        if len(files) > self.max_snapshot_files:
            raise RuntimeError(f"trusted_snapshot_too_many_files: {len(files)} > {self.max_snapshot_files}")
        for path in files:
            raw = path.read_bytes()
            if self.snapshot_bytes + len(raw) > self.max_snapshot_bytes:
                raise RuntimeError(f"trusted_snapshot_too_large: > {self.max_snapshot_bytes} bytes")
            self.snapshot[path] = raw
            self.snapshot_bytes += len(raw)

    def changed_paths(self) -> list[str]:
        current = set(self._iter_files())
        changed: list[str] = []
        for path in set(self.snapshot) | current:
            before = self.snapshot.get(path)
            after = path.read_bytes() if path in current and path.is_file() else None
            if before != after:
                changed.append(path.relative_to(self.repo_root).as_posix())
        return sorted(changed)

    def state_digest(self) -> str:
        """Empreinte déterministe de l'état surveillé actuel du repository."""
        digest = hashlib.sha256()
        for path in self._iter_files():
            rel = path.relative_to(self.repo_root).as_posix().encode("utf-8")
            raw = path.read_bytes()
            digest.update(len(rel).to_bytes(4, "big"))
            digest.update(rel)
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        return digest.hexdigest()

    def restore(self) -> tuple[bool, str | None]:
        try:
            current = set(self._iter_files())
            baseline = set(self.snapshot)
            for path in sorted(current - baseline, key=lambda item: len(item.parts), reverse=True):
                path.unlink(missing_ok=True)
                self._remove_empty_parents(path.parent)
            for path, raw in self.snapshot.items():
                if raw is None:
                    path.unlink(missing_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self.repo_root:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent


@dataclass
class TrustedRecoveryResult:
    success: bool
    reason: str
    restored_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TrustedSupervisorRecoveryJournal:
    """Crash-safe recovery for the *outer* snapshot, including private corpora."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.root = self.repo_root / ".self_improvement_supervisor_recovery"
        self.archive = self.root / "active.zip"
        self.metadata = self.root / "active.json"

    def has_pending(self) -> bool:
        return self.archive.is_file() or self.metadata.is_file()

    def create(self, snapshot: TrustedRepositorySnapshot, objective: EngineeringObjective) -> None:
        if self.has_pending():
            raise RuntimeError("supervisor_recovery_already_active")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_archive = self.root / "active.zip.tmp"
        tmp_metadata = self.root / "active.json.tmp"
        try:
            with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, raw in sorted(snapshot.snapshot.items(), key=lambda item: item[0].as_posix()):
                    if raw is None:
                        continue
                    archive.writestr(path.relative_to(self.repo_root).as_posix(), raw)
            digest = hashlib.sha256(tmp_archive.read_bytes()).hexdigest()
            tmp_metadata.write_text(json.dumps({
                "version": 1,
                "created_unix": time.time(),
                "objective_sha256": hashlib.sha256(objective.goal.encode("utf-8")).hexdigest(),
                "files": len(snapshot.snapshot),
                "snapshot_bytes": snapshot.snapshot_bytes,
                "archive_sha256": digest,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_archive.replace(self.archive)
            tmp_metadata.replace(self.metadata)
        except Exception:
            tmp_archive.unlink(missing_ok=True)
            tmp_metadata.unlink(missing_ok=True)
            raise

    def restore(self) -> TrustedRecoveryResult:
        if not self.archive.is_file():
            return TrustedRecoveryResult(False, "supervisor_recovery_archive_missing")
        try:
            if self.metadata.is_file():
                meta = json.loads(self.metadata.read_text(encoding="utf-8"))
                expected = str(meta.get("archive_sha256", "")) if isinstance(meta, dict) else ""
                actual = hashlib.sha256(self.archive.read_bytes()).hexdigest()
                if expected and expected != actual:
                    return TrustedRecoveryResult(False, "supervisor_recovery_integrity_failed")
            probe = TrustedRepositorySnapshot(self.repo_root)
            baseline: dict[Path, bytes] = {}
            with zipfile.ZipFile(self.archive, "r") as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if len(infos) > probe.max_snapshot_files:
                    return TrustedRecoveryResult(False, "supervisor_recovery_too_many_files")
                if sum(max(0, int(info.file_size)) for info in infos) > probe.max_snapshot_bytes:
                    return TrustedRecoveryResult(False, "supervisor_recovery_too_large")
                for info in infos:
                    rel_text = info.filename.replace("\\", "/")
                    candidate = (self.repo_root / rel_text).resolve(strict=False)
                    try:
                        candidate.relative_to(self.repo_root)
                    except ValueError:
                        return TrustedRecoveryResult(False, "supervisor_recovery_unsafe_member")
                    if probe._ignored(candidate) or candidate in baseline:
                        return TrustedRecoveryResult(False, "supervisor_recovery_unsafe_member")
                    baseline[candidate] = archive.read(info)
            current = set(probe._iter_files())
            baseline_paths = set(baseline)
            for path in sorted(current - baseline_paths, key=lambda item: len(item.parts), reverse=True):
                path.unlink(missing_ok=True)
                probe._remove_empty_parents(path.parent)
            for path, raw in baseline.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            restored = len(baseline)
            self.clear()
            return TrustedRecoveryResult(True, "supervisor_recovery_restored", restored)
        except Exception as exc:
            return TrustedRecoveryResult(False, f"supervisor_recovery_failed: {exc}")

    def clear(self) -> None:
        self.archive.unlink(missing_ok=True)
        self.metadata.unlink(missing_ok=True)
        (self.root / "active.zip.tmp").unlink(missing_ok=True)
        (self.root / "active.json.tmp").unlink(missing_ok=True)
        try:
            self.root.rmdir()
        except OSError:
            pass


class IsolatedTrustedEvaluator:
    """Runs the protected public benchmark in a clean child interpreter."""

    def __init__(self, repo_root: str | Path, *, timeout_seconds: float = 240.0) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.timeout_seconds = max(10.0, float(timeout_seconds))

    def evaluate(self, *, timeout_seconds: float | None = None) -> BenchmarkReport:
        with tempfile.NamedTemporaryFile(prefix="trusted_eval_", suffix=".json", delete=False) as handle:
            output = Path(handle.name)
        try:
            command = [
                sys.executable, "-m", "self_improvement.benchmark_runner",
                "--split", OPTIMIZATION_SPLIT,
                "--split", GUARD_SPLIT,
                "--output", str(output),
            ]
            proc = subprocess.run(
                command,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds if timeout_seconds is None else min(self.timeout_seconds, timeout_seconds),
                env=sanitized_child_environment(),
            )
            if proc.returncode != 0:
                tail = f"{proc.stdout}\n{proc.stderr}".strip()[-4000:]
                raise RuntimeError(f"trusted_benchmark_failed({proc.returncode}): {tail}")
            if not output.is_file():
                raise RuntimeError("trusted_benchmark_missing_output")
            report = _report_from_dict(json.loads(output.read_text(encoding="utf-8")))
            _assert_public_report(report)
            return report
        finally:
            output.unlink(missing_ok=True)


EngineeringRunner = Callable[[EngineeringObjective], dict[str, Any]]
RepositoryValidator = Callable[[], GlobalValidationResult]


class TrustedSelfImprovementSupervisor:
    """Outer evidence/rollback loop that the cognitive agent cannot rewrite."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        evaluator: Evaluator | Callable[[], BenchmarkReport] | None = None,
        repository_validator: RepositoryValidator | None = None,
        engineering_runner: EngineeringRunner | None = None,
        logger: Callable[[str], None] | None = print,
        engineering_timeout_seconds: float = 1200.0,
        curriculum: TrustedTrainCurriculum | None = None,
        engineering_memory: EngineeringMemory | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.evaluator = evaluator or IsolatedTrustedEvaluator(self.repo_root)
        self._validator_owner = EngineeringOrchestrator(self.repo_root) if repository_validator is None else None
        self.repository_validator = repository_validator or self._validator_owner.validate_repository
        self.engineering_runner = engineering_runner or self._run_engineering_fresh_process
        self.logger = logger or (lambda _message: None)
        self.engineering_timeout_seconds = max(30.0, float(engineering_timeout_seconds))
        self.recovery = TrustedSupervisorRecoveryJournal(self.repo_root)
        self.curriculum = curriculum or TrustedTrainCurriculum()
        self.engineering_memory = engineering_memory or EngineeringMemory(self.repo_root)

    def recovery_status(self) -> dict[str, Any]:
        """État du checkpoint externe, sans exposer son contenu privé."""
        return {
            "pending": self.recovery.has_pending(),
            "supported": True,
            "scope": "trusted_self_improvement",
        }

    def recover_repository(self) -> TrustedRecoveryResult:
        """Restaure explicitement un cycle autonome interrompu."""
        if not self.recovery.has_pending():
            return TrustedRecoveryResult(True, "no_supervisor_recovery_pending", 0)
        return self.recovery.restore()

    def run(
        self,
        *,
        budget: TrustedSelfImprovementBudget | None = None,
        dry_run: bool = False,
    ) -> TrustedSelfImprovementOutcome:
        cfg = budget or TrustedSelfImprovementBudget()
        started = time.perf_counter()
        cycles: list[TrustedCycleOutcome] = []
        recovered = False
        from self_improvement.execution_limits import ExecutionLimits
        self._execution_limits = ExecutionLimits(
            cfg.max_tasks_per_cycle, cfg.max_source_files, cfg.max_diff_lines,
            cfg.max_model_calls_per_cycle, time.monotonic() + cfg.max_minutes * 60.0)
        self._evaluations_started = 0
        self._validations_started = 0
        # 2 evaluations (120s), 2 repository validations (180s), one
        # structural campaign (480s) and restoration (30s). No model discovery.

        # Minimal human intervention: a crashed previous self-improvement cycle is
        # automatically rolled back before a new one starts.
        if self.recovery.has_pending():
            result = self.recovery.restore()
            if not result.success:
                return self._outcome(started, "REJECT", result.reason, False, cycles, recovered=False)
            recovered = True

        if self._execution_limits.remaining() < 1110.0:
            return self._outcome(started, "STOPPED", "INSUFFICIENT_GLOBAL_TIME", False, cycles)

        try:
            baseline = self._evaluate()
        except Exception as exc:
            return self._outcome(started, "REJECT", f"baseline_evaluation_failed: {exc}", False, cycles, recovered=recovered)

        initial = TrustedEvaluationSummary.from_report(baseline)
        validation = self._safe_validate_repository()
        if not validation.success:
            return self._outcome(
                started, "REJECT", validation.reason or "baseline_tests_failed", False, cycles,
                initial=initial, final=initial, recovered=recovered,
                details={"baseline_validation": validation.to_dict()},
            )

        if initial.train_score >= cfg.target_score:
            return self._outcome(started, "TARGET_REACHED", "target_score_already_reached", True, cycles,
                                 initial=initial, final=initial, recovered=recovered)
        if not _train_failures(baseline):
            return self._outcome(started, "NO_ACTION", "no_train_failure_to_improve", True, cycles,
                                 initial=initial, final=initial, recovered=recovered)

        current = baseline
        repo_fingerprint = self._repository_fingerprint()
        attempted_ids: set[str] = self._historical_rejected_ids(current.dataset_version, repo_fingerprint)
        for cycle_index in range(1, int(cfg.max_cycles) + 1):
            if (time.perf_counter() - started) / 60.0 >= cfg.max_minutes:
                return self._outcome(started, "STOPPED", "time_budget_exhausted", True, cycles,
                                     initial=initial, final=TrustedEvaluationSummary.from_report(current), recovered=recovered)
            cycle_started = time.perf_counter()
            try:
                category_rejects = self._historical_rejected_category_counts(current, repo_fingerprint)
                curriculum = self.curriculum.select(
                    current, exclude_scenario_ids=attempted_ids, maximum_cases=cfg.max_tasks_per_cycle,
                    rejected_category_counts=category_rejects,
                )
                objective = build_trusted_train_objective(
                    current, exclude_scenario_ids=attempted_ids,
                    maximum_cases=1,
                    preferred_scenario_ids=curriculum.scenario_ids,
                    curriculum_rationale=curriculum.rationale,
                )
                objective.metadata["execution_limits"] = self._execution_limits.to_dict()
                objective.metadata["max_model_calls"] = int(cfg.max_model_calls_per_cycle)
                objective.metadata["protect_existing_tests"] = True
                hints = self.engineering_memory.relevant_hints(
                    objective.goal, limit=3, repo_fingerprint=repo_fingerprint
                )
                if hints:
                    objective.constraints.append(
                        "Leçons historiques non autoritaires : " + " | ".join(
                            item.lesson[:350] for item in hints
                        )
                    )
                    objective.metadata["memory_hint_count"] = len(hints)
                    # Memory is context, never a hard PlanRequirement. Keeping it
                    # in constraints made historical error identifiers look like
                    # required repository symbols inside a fresh worker.
                    objective.metadata["engineering_memory_hints"] = [
                        item.lesson[:350] for item in hints
                    ]
                    objective.constraints.pop()
            except Exception:
                break
            selected = [str(item) for item in objective.metadata.get("scenario_ids", []) if isinstance(item, str)]
            attempted_ids.update(selected)
            baseline_summary = TrustedEvaluationSummary.from_report(current)
            self.logger(f"[TrustedSupervisor] Cycle {cycle_index}: {len(selected)} cas TRAIN ciblés.")

            if dry_run:
                cycle = TrustedCycleOutcome(
                    cycle=cycle_index, decision="DRY_RUN", reason="objective_generated_without_mutation",
                    objective=objective.goal, baseline=baseline_summary,
                    duration_seconds=round(time.perf_counter() - cycle_started, 2),
                )
                cycles.append(cycle)
                return self._outcome(started, "DRY_RUN", "dry_run_completed", True, cycles,
                                     initial=initial, final=baseline_summary, recovered=recovered)

            transaction = TrustedRepositorySnapshot(self.repo_root)
            try:
                transaction.capture_repository()
                self.recovery.create(transaction, objective)
            except Exception as exc:
                self.recovery.clear()
                cycle = TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason=f"supervisor_snapshot_failed: {exc}",
                    objective=objective.goal, baseline=baseline_summary,
                    duration_seconds=round(time.perf_counter() - cycle_started, 2),
                )
                cycles.append(cycle)
                break

            baseline_existing_tests = self._existing_test_files()
            engineering_payload: dict[str, Any] = {}
            try:
                self._execution_limits.phase_timeout(self.engineering_timeout_seconds,
                    minimum=480.0, future_seconds=300.0)
                engineering_payload = self.engineering_runner(objective)
                self._execution_limits.phase_timeout(1.0, future_seconds=300.0)
            except Exception as exc:
                self._rollback(transaction)
                cycle = TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason=f"engineering_process_failed: {exc}",
                    objective=objective.goal, baseline=baseline_summary, rollback_performed=True,
                    duration_seconds=round(time.perf_counter() - cycle_started, 2),
                )
                cycles.append(cycle)
                if self._has_unattempted(current, attempted_ids) and cycle_index < cfg.max_cycles:
                    continue
                return self._outcome(started, "REJECT", cycle.reason, False, cycles,
                                     initial=initial, final=baseline_summary, recovered=recovered)

            worker_usage = WorkerUsageReport.from_dict(engineering_payload.get("worker_usage"))
            usage_details = {
                "worker_usage": worker_usage.to_dict(),
                "model_calls": worker_usage.model_calls_total,
                # Diagnostic evidence from the sanitized TRAIN worker only.
                # Preserve it before its temporary workspace disappears; it is
                # never used as authoritative validation or permission to apply.
                "engineering_evidence": {
                    key: engineering_payload[key]
                    for key in ("final_decision", "reason", "plan", "campaign_outcome", "details")
                    if key in engineering_payload
                },
            }
            engineering_details = engineering_payload.get("details")
            if isinstance(engineering_details, dict):
                usage_details["budget_snapshots"] = list(engineering_details.get("budget_snapshots") or [])[:16]
                usage_details["worker_model_budget"] = dict(engineering_details.get("model_budget") or {})
            decision = str(engineering_payload.get("final_decision", "UNCERTAIN")).upper()
            changed = transaction.changed_paths()
            audit_error, audit_details = self._audit_candidate_changes(
                changed, baseline_existing_tests, engineering_payload,
                transaction=transaction, selected_scenario_ids=selected,
            )
            if decision != "ACCEPT" or audit_error:
                self._rollback(transaction)
                reason = audit_error or f"engineering_not_accepted: {engineering_payload.get('reason', decision)}"
                cycles.append(TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT" if audit_error else decision, reason=reason,
                    objective=objective.goal, baseline=baseline_summary, changed_paths=changed,
                    rollback_performed=True, duration_seconds=round(time.perf_counter() - cycle_started, 2),
                    details={**audit_details, **usage_details, "scenario_ids": selected, "repo_fingerprint": repo_fingerprint},
                ))
                self._append_history(cycles[-1], dataset_version=current.dataset_version)
                self._remember_cycle(cycles[-1], repo_fingerprint=repo_fingerprint)
                if self._has_unattempted(current, attempted_ids) and cycle_index < cfg.max_cycles:
                    continue
                return self._outcome(started, decision if not audit_error else "REJECT", reason, False, cycles,
                                     initial=initial, final=baseline_summary, recovered=recovered)

            # From this point on, tests and benchmark execute candidate-controlled
            # code. They must not be able to mutate the repository behind the
            # supervisor's back after the initial diff audit.
            candidate_state_digest = transaction.state_digest()

            trusted_tests = self._safe_validate_repository()
            if not trusted_tests.success:
                self._rollback(transaction)
                cycles.append(TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason="trusted_global_validation_failed",
                    objective=objective.goal, baseline=baseline_summary, changed_paths=changed,
                    rollback_performed=True, duration_seconds=round(time.perf_counter() - cycle_started, 2),
                    details={"validation": trusted_tests.to_dict(), "scenario_ids": selected, "repo_fingerprint": repo_fingerprint},
                ))
                self._append_history(cycles[-1], dataset_version=current.dataset_version)
                self._remember_cycle(cycles[-1], repo_fingerprint=repo_fingerprint)
                if self._has_unattempted(current, attempted_ids) and cycle_index < cfg.max_cycles:
                    continue
                return self._outcome(started, "REJECT", "trusted_global_validation_failed", False, cycles,
                                     initial=initial, final=baseline_summary, recovered=recovered)

            try:
                candidate = self._evaluate()
            except Exception as exc:
                self._rollback(transaction)
                cycles.append(TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason=f"candidate_evaluation_failed: {exc}",
                    objective=objective.goal, baseline=baseline_summary, changed_paths=changed,
                    rollback_performed=True, duration_seconds=round(time.perf_counter() - cycle_started, 2),
                ))
                return self._outcome(started, "REJECT", "candidate_evaluation_failed", False, cycles,
                                     initial=initial, final=baseline_summary, recovered=recovered)

            post_validation_digest = transaction.state_digest()
            if post_validation_digest != candidate_state_digest:
                post_changed = transaction.changed_paths()
                post_audit_error, post_audit_details = self._audit_candidate_changes(
                    post_changed, baseline_existing_tests, engineering_payload,
                    transaction=transaction, selected_scenario_ids=selected,
                )
                self._rollback(transaction)
                cycles.append(TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason="trusted_validation_mutated_repository",
                    objective=objective.goal, baseline=baseline_summary, changed_paths=post_changed,
                    rollback_performed=True, duration_seconds=round(time.perf_counter() - cycle_started, 2),
                    details={
                        **post_audit_details,
                        "post_validation_audit_error": post_audit_error,
                        "scenario_ids": selected,
                        "repo_fingerprint": repo_fingerprint,
                    },
                ))
                self._append_history(cycles[-1], dataset_version=current.dataset_version)
                self._remember_cycle(cycles[-1], repo_fingerprint=repo_fingerprint)
                if self._has_unattempted(current, attempted_ids) and cycle_index < cfg.max_cycles:
                    continue
                return self._outcome(
                    started, "REJECT", "trusted_validation_mutated_repository", False, cycles,
                    initial=initial, final=baseline_summary, recovered=recovered,
                )

            accepted, reasons, gain = decide_trusted_candidate(current, candidate, cfg.minimum_improvement)
            if self._execution_limits.remaining() <= self._execution_limits.rollback_reserve_seconds:
                self._rollback(transaction)
                cycles.append(TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason="INSUFFICIENT_GLOBAL_TIME",
                    objective=objective.goal, baseline=baseline_summary, changed_paths=changed,
                    rollback_performed=True, duration_seconds=round(time.perf_counter() - cycle_started, 2)))
                return self._outcome(started, "REJECT", "INSUFFICIENT_GLOBAL_TIME", False, cycles,
                    initial=initial, final=baseline_summary, recovered=recovered)
            candidate_summary = TrustedEvaluationSummary.from_report(candidate)
            if not accepted:
                self._rollback(transaction)
                cycles.append(TrustedCycleOutcome(
                    cycle=cycle_index, decision="REJECT", reason="independent_evidence_rejected_candidate",
                    objective=objective.goal, baseline=baseline_summary, candidate=candidate_summary,
                    train_improvement=gain, changed_paths=changed, rollback_performed=True,
                    duration_seconds=round(time.perf_counter() - cycle_started, 2),
                    details={"reasons": reasons, "scenario_ids": selected, "repo_fingerprint": repo_fingerprint},
                ))
                self._append_history(cycles[-1], dataset_version=current.dataset_version)
                self._remember_cycle(cycles[-1], repo_fingerprint=repo_fingerprint)
                if self._has_unattempted(current, attempted_ids) and cycle_index < cfg.max_cycles:
                    continue
                return self._outcome(started, "REJECT", "independent_evidence_rejected_candidate", False, cycles,
                                     initial=initial, final=baseline_summary, recovered=recovered)

            self.recovery.clear()
            accept_details = {
                "fresh_process": True,
                "scenario_ids": selected,
                "repo_fingerprint": repo_fingerprint,
                "trusted_train_only": True,
                "validation_used_as_veto_only": True,
                **usage_details,
            }
            if bool(cfg.git_checkpoint):
                checkpoint = TrustedGitCheckpointManager(self.repo_root).checkpoint(
                    changed,
                    message=f"chore(self-improvement): accept cycle {cycle_index} (+{gain:.2f} TRAIN)",
                )
                # Git est une couche de traçabilité après ACCEPT. Son indisponibilité
                # ne peut pas invalider des preuves tests/benchmark déjà autoritatives.
                accept_details["git_checkpoint"] = checkpoint.to_dict()
            cycles.append(TrustedCycleOutcome(
                cycle=cycle_index, decision="ACCEPT", reason="trusted_evidence_accepted_candidate",
                objective=objective.goal, baseline=baseline_summary, candidate=candidate_summary,
                train_improvement=gain, changed_paths=changed, rollback_performed=False,
                duration_seconds=round(time.perf_counter() - cycle_started, 2),
                details=accept_details,
            ))
            self._append_history(cycles[-1], dataset_version=current.dataset_version)
            self._remember_cycle(cycles[-1], repo_fingerprint=repo_fingerprint)
            current = candidate
            repo_fingerprint = self._repository_fingerprint()
            attempted_ids = self._historical_rejected_ids(current.dataset_version, repo_fingerprint)

            if candidate_summary.train_score >= cfg.target_score:
                return self._outcome(started, "TARGET_REACHED", "target_score_reached", True, cycles,
                                     initial=initial, final=candidate_summary, recovered=recovered)
            if not _train_failures(current):
                return self._outcome(started, "ACCEPT", "no_train_failure_remaining", True, cycles,
                                     initial=initial, final=candidate_summary, recovered=recovered)

        final = TrustedEvaluationSummary.from_report(current)
        accepted_any = any(item.decision == "ACCEPT" for item in cycles)
        return self._outcome(started, "ACCEPT" if accepted_any else "STOPPED",
                             "cycle_budget_exhausted" if accepted_any else "no_cycle_accepted",
                             accepted_any, cycles, initial=initial, final=final, recovered=recovered)

    def _run_engineering_fresh_process(self, objective: EngineeringObjective) -> dict[str, Any]:
        """Run the cognitive engineer in a sanitized disposable workspace.

        The child never needs raw evaluation corpora, reports, user memory or local
        secrets to implement a TRAIN-derived objective.  Only its vetted diff is
        copied back to the real repository; authoritative tests/benchmark then run
        in the trusted parent.
        """
        from self_improvement.execution_limits import ExecutionLimits, check_candidate
        limits = ExecutionLimits.from_dict(objective.metadata.get("execution_limits"))
        if objective.metadata.get("max_model_calls") != limits.max_model_calls:
            raise ValueError("unsupported_execution_limits: inconsistent model cap")
        worker_timeout = limits.phase_timeout(self.engineering_timeout_seconds,
                                              minimum=480.0, future_seconds=300.0)
        with tempfile.TemporaryDirectory(prefix="trusted_engineering_") as temp:
            temp_root = Path(temp)
            workspace = temp_root / "workspace"
            baseline = self._materialize_sanitized_workspace(workspace)
            if (self.repo_root / "self_improvement/agent_runtime.py").is_file():
                from self_improvement.public_workspace_resources import missing_public_resources
                missing = missing_public_resources(workspace)
                if missing:
                    return {"final_decision": "REJECT", "success": False,
                            "reason": "TEST_INFRA_FAILURE: missing public resources: " + ", ".join(missing),
                            "worker_usage": WorkerUsageReport().to_dict()}
            objective_path = temp_root / "objective.json"
            output_path = temp_root / "outcome.json"
            usage_path = temp_root / "worker_usage.json"
            objective_path.write_text(json.dumps({
                "goal": objective.goal,
                "constraints": list(objective.constraints),
                "metadata": dict(objective.metadata),
            }, ensure_ascii=False), encoding="utf-8")
            command = [
                sys.executable, "-m", "self_improvement.agent_runtime",
                "--output", str(output_path),
                "objective", "--objective-file", str(objective_path), "--self-improvement",
            ]
            env = model_agent_environment(extra={
                "PYTHONPATH": "",
                "PROJET_IA_WORKER_USAGE_PATH": str(usage_path),
            })
            # CI's audited guard is inherited even though ordinary PYTHONPATH
            # entries must not enter the sanitized cognitive worker.
            guard = sys.modules.get("sitecustomize")
            if env.get("NOVA_OFFLINE_AUDIT_FILE") and getattr(guard, "_AUDIT_FILE", None):
                env["PYTHONPATH"] = str(Path(guard.__file__).resolve().parent)
            try:
                proc = subprocess.run(
                    command, cwd=str(workspace), capture_output=True, text=True,
                    timeout=min(worker_timeout, limits.phase_timeout(self.engineering_timeout_seconds,
                        minimum=480.0, future_seconds=300.0)), env=env,
                )
            except subprocess.TimeoutExpired:
                return {
                    "final_decision": "REJECT", "success": False,
                    "reason": "engineering_worker_timeout",
                    "worker_usage": self._load_worker_usage(usage_path).to_dict(),
                    "details": {"timeout_seconds": self.engineering_timeout_seconds},
                }
            if not output_path.is_file():
                tail = f"{proc.stdout}\n{proc.stderr}".strip()[-5000:]
                return {
                    "final_decision": "REJECT", "success": False,
                    "reason": f"engineering_worker_missing_output({proc.returncode}): {tail}",
                    "worker_usage": self._load_worker_usage(usage_path).to_dict(),
                    "details": {"worker_returncode": proc.returncode},
                }
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                raise RuntimeError("engineering_worker_invalid_output")
            result.setdefault("worker_returncode", proc.returncode)
            result["worker_usage"] = self._load_worker_usage(usage_path).to_dict()
            result.setdefault("details", {})
            result["details"]["sanitized_workspace"] = True

            if str(result.get("final_decision", "")).upper() != "ACCEPT":
                return result

            changed = self._workspace_changed_paths(workspace, baseline)
            planned = _planned_paths_from_engineering_payload(result)
            forbidden = [path for path in changed if not is_agent_editable_path(path)]
            unplanned = [path for path in changed if path not in planned]
            deleted = [path for path in baseline if not (workspace / path).is_file()]
            if forbidden or unplanned or deleted:
                result["final_decision"] = "REJECT"
                result["success"] = False
                result["reason"] = "sanitized_workspace_change_audit_failed"
                result["details"].update({
                    "workspace_changed_paths": changed,
                    "workspace_forbidden_changes": forbidden,
                    "workspace_unplanned_changes": unplanned,
                    "workspace_deleted_paths": deleted,
                })
                return result

            # Apply only the validated declared diff to the real repository.  The
            # outer byte snapshot already exists, so any copy failure is reversible.
            try:
                limits.phase_timeout(1, future_seconds=300.0)
                check_candidate({rel: (baseline.get(rel, b""), (workspace / rel).read_bytes()
                                      if (workspace / rel).is_file() else b"") for rel in changed},
                                limits.max_source_files, limits.max_diff_lines)
            except (ValueError, TimeoutError) as exc:
                return {**result, "final_decision": "REJECT", "success": False, "reason": str(exc)}
            for rel in changed:
                source = (workspace / rel).resolve(strict=False)
                destination = (self.repo_root / rel).resolve(strict=False)
                try:
                    destination.relative_to(self.repo_root)
                except ValueError as exc:
                    raise RuntimeError(f"workspace_apply_path_escape: {rel}") from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                raw = source.read_bytes()
                with tempfile.NamedTemporaryFile(
                    prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent), delete=False
                ) as handle:
                    tmp_path = Path(handle.name)
                    handle.write(raw)
                os.replace(tmp_path, destination)

            result["details"]["workspace_changed_paths"] = changed
            return result

    @staticmethod
    def _load_worker_usage(path: Path) -> WorkerUsageReport:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError, TypeError):
            raw = {}
        return WorkerUsageReport.from_dict(raw if isinstance(raw, dict) else {})

    def _materialize_sanitized_workspace(self, workspace: Path) -> dict[str, bytes]:
        """Copy only model-readable/project-operational files into a temp repo.

        Evaluation corpora and private config are intentionally absent.  Existing
        tests remain present because they are useful for targeted regression runs,
        but self-improvement mode forbids modifying them.
        """
        from self_improvement.public_workspace_resources import is_public_workspace_file
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_root = workspace.resolve(strict=False)
        baseline: dict[str, bytes] = {}
        ignored_parts = {
            ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".hypothesis",
            ".temp_tests", ".self_improvement_worktrees", ".self_improvement_discoveries",
            ".self_improvement_recovery", ".self_improvement_supervisor_recovery",
            "benchmark_results", "backups", ".continue", ".vscode", ".idea", ".cursor", ".cline", ".roo",
            ".coverage", "coverage.xml", "htmlcov", ".mypy_cache", ".ruff_cache",
        }
        for source in self.repo_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            # Tests and custom callers may place the sanitized workspace inside
            # the repository. Never traverse/copy the destination into itself.
            resolved_source = source.resolve(strict=False)
            try:
                resolved_source.relative_to(workspace_root)
            except ValueError:
                pass
            else:
                continue
            rel = source.relative_to(self.repo_root).as_posix()
            parts = {part.casefold() for part in Path(rel).parts}
            if parts & ignored_parts:
                continue
            # The model-private policy hides raw evaluation corpora, reports and
            # secrets.  memory.db is user state and is also omitted explicitly.
            if is_model_private_path(rel) or Path(rel).name.casefold() == "memory.db":
                continue
            if not is_public_workspace_file(rel):
                continue
            try:
                resolved_source.relative_to(self.repo_root.resolve())
            except ValueError:
                continue
            try:
                raw = source.read_bytes()
            except OSError:
                continue
            if len(raw) > 5_000_000:
                continue
            destination = workspace / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            baseline[rel] = raw
        return baseline

    @staticmethod
    def _workspace_changed_paths(workspace: Path, baseline: dict[str, bytes]) -> list[str]:
        current: dict[str, bytes] = {}
        for path in workspace.rglob("*"):
            if path.is_file() and not path.is_symlink():
                rel = path.relative_to(workspace).as_posix()
                if is_model_private_path(rel) or Path(rel).name.casefold() == "memory.db":
                    continue
                # Runtime caches/recovery created by the child are not candidate code.
                if any(part.casefold() in {
                    "__pycache__", ".pytest_cache", ".self_improvement_recovery",
                    ".self_improvement_supervisor_recovery", ".temp_tests",
                    ".coverage", "coverage.xml", "htmlcov", ".mypy_cache", ".ruff_cache",
                } for part in Path(rel).parts):
                    continue
                try:
                    current[rel] = path.read_bytes()
                except OSError:
                    continue
        keys = set(baseline) | set(current)
        return sorted(path for path in keys if baseline.get(path) != current.get(path))

    def _audit_candidate_changes(
        self,
        changed: list[str],
        baseline_existing_tests: set[str],
        engineering_payload: dict[str, Any],
        *,
        transaction: TrustedRepositorySnapshot,
        selected_scenario_ids: list[str],
    ) -> tuple[str | None, dict[str, Any]]:
        trusted_changes = [path for path in changed if is_trusted_control_path(path)]
        private_changes = [path for path in changed if is_model_private_path(path)]
        non_editable_changes = [path for path in changed if not is_agent_editable_path(path)]
        edited_existing_tests = [path for path in changed if path in baseline_existing_tests]
        planned = _planned_paths_from_engineering_payload(engineering_payload)
        unplanned = [path for path in changed if path not in planned]
        memorized_ids: dict[str, list[str]] = {}
        for rel in changed:
            candidate_path = self.repo_root / rel
            if self._is_test_path(rel) or candidate_path.suffix.casefold() not in {".py", ".json", ".yaml", ".yml", ".toml", ".ini"}:
                continue
            before_raw = transaction.snapshot.get(candidate_path.resolve(strict=False), b"") or b""
            try:
                before_text = before_raw.decode("utf-8", errors="ignore")
                after_text = candidate_path.read_text(encoding="utf-8", errors="ignore") if candidate_path.is_file() else ""
            except OSError:
                continue
            introduced = [sid for sid in selected_scenario_ids if sid and sid in after_text and sid not in before_text]
            if introduced:
                memorized_ids[rel] = introduced
        details = {
            "trusted_control_changes": trusted_changes,
            "private_surface_changes": private_changes,
            "non_editable_changes": non_editable_changes,
            "edited_existing_tests": edited_existing_tests,
            "unplanned_changes": unplanned,
            "planned_paths": sorted(planned),
            "introduced_train_scenario_ids": memorized_ids,
        }
        if trusted_changes:
            return "trusted_control_plane_modified", details
        if private_changes or non_editable_changes:
            return "forbidden_repository_surface_modified", details
        if edited_existing_tests:
            return "existing_test_modified_during_self_improvement", details
        if memorized_ids:
            return "train_scenario_identifier_hardcoded_in_production", details
        if changed and not planned:
            return "engineering_plan_missing_for_changed_repository", details
        if unplanned:
            return "outer_supervisor_detected_unplanned_changes", details
        limits = getattr(self, "_execution_limits", None)
        if limits is not None:
            from self_improvement.execution_limits import check_candidate
            try:
                usage = WorkerUsageReport.from_dict(engineering_payload.get("worker_usage"))
                if usage.model_calls_total > limits.max_model_calls:
                    raise ValueError("candidate_model_call_limit_exceeded")
                check_candidate({rel: (transaction.snapshot.get((self.repo_root / rel).resolve(), b"") or b"",
                                      (self.repo_root / rel).read_bytes() if (self.repo_root / rel).is_file() else b"")
                                 for rel in changed}, limits.max_source_files, limits.max_diff_lines)
            except ValueError as exc:
                return str(exc), {"execution_limits": limits.to_dict()}
        return None, details

    @staticmethod
    def _is_test_path(path: str) -> bool:
        candidate = Path(path)
        return candidate.name.casefold().startswith("test_") or "tests" in {part.casefold() for part in candidate.parts[:-1]}

    def _existing_test_files(self) -> set[str]:
        found: set[str] = set()
        for path in self.repo_root.rglob("*.py"):
            try:
                rel = path.resolve(strict=False).relative_to(self.repo_root).as_posix()
            except ValueError:
                continue
            name = path.name.casefold()
            if name.startswith("test_") or "tests" in {part.casefold() for part in path.parts[:-1]}:
                found.add(rel)
        return found

    def _rollback(self, transaction: TrustedRepositorySnapshot) -> None:
        ok, error = transaction.restore()
        try:
            self.recovery.clear()
        finally:
            if not ok:
                raise RuntimeError(f"trusted_rollback_failed: {error}")

    def _evaluate(self) -> BenchmarkReport:
        evaluator = self.evaluator
        callback = evaluator.evaluate if hasattr(evaluator, "evaluate") else evaluator
        limits = getattr(self, "_execution_limits", None)
        kwargs = {}
        if limits is not None:
            reserve = 960.0 if self._evaluations_started == 0 else 0.0
            timeout = limits.phase_timeout(120.0, minimum=120.0, future_seconds=reserve)
            self._evaluations_started += 1
            import inspect
            if "timeout_seconds" in inspect.signature(callback).parameters:
                kwargs["timeout_seconds"] = timeout
        report = callback(**kwargs)
        if limits is not None:
            limits.phase_timeout(1.0)
        if not isinstance(report, BenchmarkReport):
            raise TypeError("trusted evaluator must return BenchmarkReport")
        _assert_public_report(report)
        return report

    def _safe_validate_repository(self) -> GlobalValidationResult:
        try:
            limits = getattr(self, "_execution_limits", None)
            kwargs = {}
            if limits is not None:
                reserve = 780.0 if self._validations_started == 0 else 120.0
                timeout = limits.phase_timeout(180.0, minimum=180.0, future_seconds=reserve)
                self._validations_started += 1
                import inspect
                if "timeout_seconds" in inspect.signature(self.repository_validator).parameters:
                    kwargs["timeout_seconds"] = timeout
            result = self.repository_validator(**kwargs)
            if limits is not None:
                limits.phase_timeout(1.0)
        except Exception as exc:
            return GlobalValidationResult(False, f"trusted_validator_crash: {exc}", tests_failed=1)
        if not isinstance(result, GlobalValidationResult):
            return GlobalValidationResult(False, "trusted_validator_invalid_result", tests_failed=1)
        return result

    @staticmethod
    def _has_unattempted(report: BenchmarkReport, attempted: set[str]) -> bool:
        return any(item.scenario_id not in attempted for item in _train_failures(report))

    def _remember_cycle(self, cycle: TrustedCycleOutcome, *, repo_fingerprint: str) -> None:
        try:
            lesson = cycle.reason
            if cycle.decision == "ACCEPT":
                lesson = f"Auto-amélioration acceptée avec gain TRAIN mesuré de {cycle.train_improvement:.2f}."
            self.engineering_memory.record(
                task=cycle.objective, outcome=cycle.decision, lesson=lesson,
                failure_type="" if cycle.decision == "ACCEPT" else cycle.reason,
                strategy="trusted_self_improvement", files=cycle.changed_paths,
                repo_fingerprint=repo_fingerprint,
                metadata={"scenario_ids": list(cycle.details.get("scenario_ids", []) or [])[:12]},
            )
        except Exception:
            pass

    def _historical_rejected_category_counts(self, report: BenchmarkReport, repo_fingerprint: str) -> dict[str, int]:
        path = self._history_path()
        if not path.is_file():
            return {}
        by_id = {item.scenario_id: item.category for item in _train_failures(report)}
        counts: dict[str, int] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-300:]
        except OSError:
            return counts
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or str(item.get("decision", "")).upper() == "ACCEPT":
                continue
            if str(item.get("dataset_version", "")) != str(report.dataset_version):
                continue
            if str(item.get("repo_fingerprint", "")) != str(repo_fingerprint):
                continue
            seen_categories: set[str] = set()
            for scenario_id in item.get("scenario_ids", []) or []:
                category = by_id.get(str(scenario_id))
                if category and category not in seen_categories:
                    counts[category] = counts.get(category, 0) + 1
                    seen_categories.add(category)
        return counts

    def _history_path(self) -> Path:
        return self.repo_root / ".runtime" / "trusted_supervisor_history.jsonl"

    def _append_history(self, cycle: TrustedCycleOutcome, *, dataset_version: str) -> None:
        try:
            path = self._history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "decision": cycle.decision,
                "reason": cycle.reason,
                "train_improvement": cycle.train_improvement,
                "changed_paths": cycle.changed_paths,
                "scenario_ids": list(cycle.details.get("scenario_ids", []) or []),
                "repo_fingerprint": str(cycle.details.get("repo_fingerprint", "")),
                "dataset_version": str(dataset_version),
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _historical_rejected_ids(self, dataset_version: str, repo_fingerprint: str) -> set[str]:
        path = self._history_path()
        if not path.is_file():
            return set()
        avoided: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-300:]
        except OSError:
            return set()
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or str(item.get("decision", "")).upper() == "ACCEPT":
                continue
            if str(item.get("dataset_version", "")) != str(dataset_version):
                continue
            if str(item.get("repo_fingerprint", "")) != str(repo_fingerprint):
                continue
            for scenario_id in item.get("scenario_ids", []) or []:
                if isinstance(scenario_id, str):
                    avoided.add(scenario_id)
        return avoided

    def _repository_fingerprint(self) -> str:
        digest = hashlib.sha256()
        transaction = TrustedRepositorySnapshot(self.repo_root)
        for path in transaction._iter_files():
            rel = path.relative_to(self.repo_root).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        return digest.hexdigest()

    def _outcome(
        self,
        started: float,
        decision: str,
        reason: str,
        success: bool,
        cycles: list[TrustedCycleOutcome],
        *,
        initial: TrustedEvaluationSummary | None = None,
        final: TrustedEvaluationSummary | None = None,
        recovered: bool = False,
        details: dict[str, Any] | None = None,
    ) -> TrustedSelfImprovementOutcome:
        return TrustedSelfImprovementOutcome(
            final_decision=decision,
            reason=reason,
            success=success,
            initial=initial,
            final=final,
            cycles=list(cycles),
            duration_seconds=round(time.perf_counter() - started, 2),
            recovered_interrupted_cycle=recovered,
            details={**dict(details or {}), **({
                "execution_limits": self._execution_limits.to_dict(),
                "global_deadline_overrun_seconds": max(0.0, time.monotonic() - self._execution_limits.deadline_monotonic),
                "deadline_behavior": "bounded_subprocesses_cooperative_cleanup_restoration_not_interrupted",
            } if hasattr(self, "_execution_limits") else {})},
        )


def _suggest_improvement_level(selected: list[ScenarioResult]) -> str:
    """Niveau cognitif suggéré à partir de catégories TRAIN uniquement.

    C'est un hint de planification, jamais une permission de chemin : le Planner et
    le Trusted Control Plane continuent à décider du périmètre réel.
    """
    text = " ".join(str(item.category or "").casefold() for item in selected)
    if any(token in text for token in ("planning", "planner", "developer", "tool", "repo", "code", "agent")):
        return "agent_cognition"
    if any(token in text for token in ("context", "memory", "conversation", "intent", "router", "reason")):
        return "assistant_cognition"
    return "behavior"


def build_trusted_train_objective(
    report: BenchmarkReport,
    *,
    maximum_cases: int = 10,
    exclude_scenario_ids: set[str] | None = None,
    preferred_scenario_ids: list[str] | None = None,
    curriculum_rationale: list[str] | None = None,
) -> EngineeringObjective:
    """Build an objective from TRAIN evidence only; the guard never enters the prompt."""
    _assert_public_report(report)
    excluded = set(exclude_scenario_ids or set())
    candidates = [item for item in _train_failures(report) if item.scenario_id not in excluded]
    candidates.sort(key=lambda item: (item.score, -float(item.weight), item.scenario_id))
    preferred = [str(item) for item in (preferred_scenario_ids or []) if isinstance(item, str)]
    if preferred:
        by_id = {item.scenario_id: item for item in candidates}
        selected = [by_id[item] for item in preferred if item in by_id]
        remaining = [item for item in candidates if item.scenario_id not in set(preferred)]
        selected.extend(remaining)
    else:
        selected = candidates
    selected = selected[: max(1, min(int(maximum_cases), 15))]
    if not selected:
        raise ValueError("Aucun échec TRAIN non tenté.")
    train_score = _split_score(report, OPTIMIZATION_SPLIT)
    improvement_level = _suggest_improvement_level(selected)
    lines = [
        "Améliore de façon mesurable l'assistant à partir des échecs TRAIN ci-dessous.",
        f"Score TRAIN actuel : {train_score:.2f}/100.",
        f"Niveau d'amélioration suggéré : {improvement_level} (indice uniquement ; confirme la cause dans le repo).",
        "Commence par localiser la cause racine dans le repository réel avec les outils de lecture.",
        "Préfère une correction générale à un traitement spécial des identifiants de scénarios.",
        "Ajoute un nouveau test de non-régression déterministe pour chaque bug corrigé.",
        "Préserve toutes les politiques de sécurité et le Trusted Control Plane.",
        "",
        "Échecs TRAIN prioritaires :",
    ]
    for case in selected:
        issues = "; ".join(item.issue for item in case.criteria if not item.passed) or case.error or "échec sans détail"
        lines.append(f"- {case.scenario_id} | catégorie={case.category} | problème={issues[:420]}")
    return EngineeringObjective(
        goal="\n".join(lines),
        constraints=[
            "Utilise uniquement l'évidence TRAIN présente dans cet objectif.",
            "Ne lis et ne demande aucune donnée de validation/holdout ni corpus d'évaluation brut.",
            "Ne modifie aucun test existant ; crée de nouveaux tests si nécessaire.",
            "Ne modifie aucun fichier du Trusted Control Plane.",
        ],
        metadata={
            "source": "trusted_train_only_supervisor_v5",
            "scenario_ids": [item.scenario_id for item in selected],
            "focused_train_selection": len(selected) == 1,
            "baseline_train_score": round(train_score, 2),
            "curriculum_rationale": list(curriculum_rationale or [])[:12],
            "curriculum_version": "5",
            "improvement_level": improvement_level,
        },
    )


def decide_trusted_candidate(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
    minimum_improvement: float,
) -> tuple[bool, list[str], float]:
    reasons: list[str] = []
    gain = round(_split_score(candidate, OPTIMIZATION_SPLIT) - _split_score(baseline, OPTIMIZATION_SPLIT), 2)
    if gain < float(minimum_improvement):
        reasons.append(f"Gain TRAIN insuffisant ({gain:.2f} < {float(minimum_improvement):.2f}).")
    if _split_score(candidate, GUARD_SPLIT) < _split_score(baseline, GUARD_SPLIT):
        reasons.append("Régression du garde indépendant.")
    security_changes = compare_security_results(baseline, candidate)
    if security_changes.get("true_regressions") or candidate.security_score < baseline.security_score:
        reasons.append("Régression de sécurité détectée.")
    if int(candidate.metrics.get("network_calls", 0) or 0) != 0:
        reasons.append("Le benchmark a effectué un appel réseau.")
    for metric in ("model_calls", "model_3b_calls", "embedding_calls"):
        before = int(baseline.metrics.get(metric, 0) or 0)
        after = int(candidate.metrics.get(metric, 0) or 0)
        if after > max(10, before * 10):
            reasons.append(f"Coût excessif: {metric} {before}->{after}.")
    return not reasons, reasons, gain


def _planned_paths_from_engineering_payload(payload: dict[str, Any]) -> set[str]:
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return set()
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return set()
    paths: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for key in ("target_files", "tests"):
            for raw in task.get(key, []) or []:
                if isinstance(raw, str) and raw.strip():
                    paths.add(Path(raw).as_posix())
    return paths


def _train_failures(report: BenchmarkReport) -> list[ScenarioResult]:
    return [item for item in report.failures if item.split == OPTIMIZATION_SPLIT]


def _split_score(report: BenchmarkReport, split: str) -> float:
    items = [item for item in report.results if item.split == split]
    denominator = sum(float(item.weight) for item in items)
    if not denominator:
        return 0.0
    return sum(float(item.score) * float(item.weight) for item in items) / denominator


def _assert_public_report(report: BenchmarkReport) -> None:
    splits = {str(item).casefold() for item in report.splits}
    if splits - PUBLIC_SPLITS:
        raise ValueError("hidden_or_unknown_split_present_in_evaluation")
    if any(item.split.casefold() not in PUBLIC_SPLITS for item in report.results):
        raise ValueError("hidden_or_unknown_result_present_in_evaluation")
    if int(report.metrics.get("network_calls", 0) or 0) != 0:
        raise ValueError("evaluation_must_not_use_network")


def _report_from_dict(data: dict[str, Any]) -> BenchmarkReport:
    results: list[ScenarioResult] = []
    for item in data.get("results", []):
        criteria = [CriterionResult(**criterion) for criterion in item.get("criteria", [])]
        allowed = {entry.name for entry in fields(ScenarioResult)}
        values = {key: value for key, value in item.items() if key in allowed}
        values["criteria"] = criteria
        results.append(ScenarioResult(**values))
    allowed_report = {entry.name for entry in fields(BenchmarkReport)}
    values = {key: value for key, value in data.items() if key in allowed_report}
    values["results"] = results
    return BenchmarkReport(**values)
