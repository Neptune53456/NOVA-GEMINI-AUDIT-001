"""Engineering Orchestrator V3 — chantier autonome transactionnel et replanifiable."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from model_router import ModelCallBudget, RoleBudgetState

from self_improvement.campaign_manager import (
    AutonomousCampaignManager,
    CampaignBudget,
    CampaignOutcome,
    StopReason,
)
from self_improvement.engineering_planner import (
    EngineeringObjective,
    EngineeringPlan,
    EngineeringPlanner,
)
from self_improvement.adaptive_budget import AdaptiveBudgetPolicy, BudgetDecision
from self_improvement.improvement_orchestrator import HOLDOUT_NAME
from self_improvement.agent_path_policy import is_model_private_path, is_non_executable_audit_artifact
from self_improvement.process_safety import sanitized_child_environment


@dataclass
class GlobalValidationResult:
    """Résultat borné de la vérification globale d'un chantier."""

    success: bool
    reason: str = ""
    tests_run: int = 0
    tests_failed: int = 0
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EngineeringOutcome:
    """Rapport final d'un chantier d'ingénierie autonome."""

    objective: EngineeringObjective
    final_decision: str
    reason: str
    success: bool = False
    plan: EngineeringPlan | None = None
    campaign_outcome: CampaignOutcome | None = None
    global_validation: GlobalValidationResult | None = None
    tasks_accepted: int = 0
    tasks_rejected: int = 0
    tasks_uncertain: int = 0
    tasks_blocked: int = 0
    duration_seconds: float = 0.0
    rollback_performed: bool = False
    replans: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": asdict(self.objective),
            "final_decision": self.final_decision,
            "reason": self.reason,
            "success": self.success,
            "plan": self.plan.to_dict() if self.plan else None,
            "campaign_outcome": self.campaign_outcome.to_dict() if self.campaign_outcome else None,
            "global_validation": self.global_validation.to_dict() if self.global_validation else None,
            "tasks_accepted": self.tasks_accepted,
            "tasks_rejected": self.tasks_rejected,
            "tasks_uncertain": self.tasks_uncertain,
            "tasks_blocked": self.tasks_blocked,
            "duration_seconds": self.duration_seconds,
            "rollback_performed": self.rollback_performed,
            "replans": self.replans,
            "details": self.details,
        }


GlobalValidator = Callable[[], GlobalValidationResult]


class EngineeringTransaction:
    """Snapshot global borné du repository pour un chantier complet.

    V3 ne se limite plus aux chemins annoncés par le Planner : tous les fichiers
    textuels publics pertinents sont photographiés avant la campagne. Ainsi, une
    écriture inattendue causée par un bug d'agent reste réversible et détectable.
    Les rapports d'audit sont volontairement exclus pour conserver la traçabilité.
    """

    ALLOWED_SUFFIXES = frozenset({".py", ".txt", ".json", ".md", ".ini", ".yaml", ".yml", ".toml"})
    IGNORED_PARTS = frozenset({
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".hypothesis",
        ".self_improvement_worktrees", ".temp_tests",
        ".self_improvement_discoveries", ".self_improvement_recovery",
        ".self_improvement_supervisor_recovery", "benchmark_results",
    })

    def __init__(
        self,
        repo_root: str | Path,
        *,
        max_snapshot_files: int = 1500,
        max_snapshot_bytes: int = 30_000_000,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.snapshot: dict[Path, str | None] = {}
        self.max_snapshot_files = max(10, int(max_snapshot_files))
        self.max_snapshot_bytes = max(1_000_000, int(max_snapshot_bytes))
        self._baseline_inventory: set[Path] = set()
        self.snapshot_bytes = 0

    def _ignored_relative(self, rel: Path) -> bool:
        lowered = [part.casefold() for part in rel.parts]
        if any(part in self.IGNORED_PARTS for part in lowered):
            return True
        if any(HOLDOUT_NAME.casefold() in part for part in lowered):
            return True
        # Les rapports texte/JSON sont des artefacts d'audit et peuvent survivre
        # au rollback. En revanche un .py placé dans reports reste transactionnel :
        # aucun code exécutable ne peut utiliser ce répertoire comme échappatoire.
        if is_non_executable_audit_artifact(rel):
            return True
        rel_text = rel.as_posix().casefold()
        if is_model_private_path(rel) and not rel_text.startswith("self_improvement/reports/"):
            return True
        return False

    def _resolve(self, raw: str | Path) -> Path:
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (self.repo_root / candidate).resolve(strict=False)
        rel = resolved.relative_to(self.repo_root)
        if self._ignored_relative(rel):
            raise ValueError(f"safety_violation: chemin interne/holdout interdit dans transaction: {raw}")
        if resolved.suffix.casefold() not in self.ALLOWED_SUFFIXES:
            raise ValueError(f"type de fichier non transactionnel interdit: {raw}")
        return resolved

    def _iter_transactional_files(self) -> list[Path]:
        results: list[Path] = []
        for path in self.repo_root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in self.ALLOWED_SUFFIXES:
                continue
            try:
                rel = path.resolve(strict=False).relative_to(self.repo_root)
            except ValueError:
                continue
            if self._ignored_relative(rel):
                continue
            results.append(path.resolve(strict=False))
        return sorted(set(results), key=lambda item: item.as_posix())

    def capture_repository(self, extra_paths: Iterable[str] = ()) -> None:
        """Capture l'état complet autorisé + les nouveaux chemins prévus."""
        files = self._iter_transactional_files()
        if len(files) > self.max_snapshot_files:
            raise ValueError(
                f"transaction_snapshot_too_many_files: {len(files)} > {self.max_snapshot_files}"
            )
        self._baseline_inventory = set(files)
        for path in files:
            self._capture_path(path)
        # Les fichiers futurs prévus par le plan n'existent pas encore : mémoriser None.
        self.capture(extra_paths)

    def _capture_path(self, path: Path) -> None:
        if path in self.snapshot:
            return
        content = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        if content is not None:
            encoded_size = len(content.encode("utf-8", errors="replace"))
            if self.snapshot_bytes + encoded_size > self.max_snapshot_bytes:
                raise ValueError(
                    f"transaction_snapshot_too_large: > {self.max_snapshot_bytes} bytes"
                )
            self.snapshot_bytes += encoded_size
        self.snapshot[path] = content

    def capture(self, paths: Iterable[str]) -> None:
        for raw in paths:
            path = self._resolve(raw)
            self._capture_path(path)

    def changed_paths(self) -> list[str]:
        """Liste les changements textuels depuis la baseline, y compris fichiers imprévus."""
        changed: set[Path] = set()
        current = set(self._iter_transactional_files())
        all_paths = set(self.snapshot) | current
        for path in all_paths:
            before = self.snapshot.get(path, None)
            if path.is_file():
                try:
                    after = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    after = None
            else:
                after = None
            if before != after:
                changed.add(path)
        return sorted(
            path.relative_to(self.repo_root).as_posix() for path in changed
        )

    def current_state(self) -> dict[str, str]:
        """Empreinte déterministe de l'état transactionnel courant.

        Sert notamment à garantir qu'un validateur global reste réellement
        read-only : même un fichier planifié ne doit pas être réécrit par les tests.
        """
        state: dict[str, str] = {}
        for path in self._iter_transactional_files():
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            rel = path.relative_to(self.repo_root).as_posix()
            state[rel] = hashlib.sha256(raw).hexdigest()
        return state

    @staticmethod
    def state_diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
        keys = set(before) | set(after)
        return sorted(path for path in keys if before.get(path) != after.get(path))

    def unplanned_changed_paths(self, planned_paths: Iterable[str]) -> list[str]:
        planned: set[str] = set()
        for raw in planned_paths:
            try:
                planned.add(self._resolve(raw).relative_to(self.repo_root).as_posix())
            except ValueError:
                continue
        return [path for path in self.changed_paths() if path not in planned]

    def unplanned_new_files(self, planned_paths: Iterable[str]) -> list[str]:
        planned: set[Path] = set()
        for raw in planned_paths:
            try:
                planned.add(self._resolve(raw))
            except ValueError:
                continue
        current = set(self._iter_transactional_files())
        unexpected = [
            path for path in current
            if path not in self._baseline_inventory and path not in planned
        ]
        return sorted(path.relative_to(self.repo_root).as_posix() for path in unexpected)

    def restore(self) -> tuple[bool, str | None]:
        errors: list[str] = []
        # D'abord supprimer tout fichier transactionnel créé après la baseline, même
        # s'il n'était pas annoncé par le Planner.
        current = set(self._iter_transactional_files())
        baseline_known = {path for path, content in self.snapshot.items() if content is not None}
        for path in sorted(current - baseline_known, key=lambda item: len(item.parts), reverse=True):
            try:
                path.unlink(missing_ok=True)
                self._remove_empty_parents(path.parent)
            except Exception as exc:  # pragma: no cover
                errors.append(f"{path}: {exc}")

        for path, original in sorted(self.snapshot.items(), key=lambda item: len(item[0].parts), reverse=True):
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                    self._remove_empty_parents(path.parent)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(original, encoding="utf-8")
            except Exception as exc:  # pragma: no cover
                errors.append(f"{path}: {exc}")
        return (not errors, "; ".join(errors) if errors else None)

    def _remove_empty_parents(self, parent: Path) -> None:
        current = parent
        while current != self.repo_root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


@dataclass
class RecoveryResult:
    """Résultat d'une récupération persistante après interruption brutale."""

    success: bool
    reason: str
    restored_files: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EngineeringRecoveryJournal:
    """Checkpoint persistant privé pour survivre à un crash de processus.

    L'EngineeringTransaction protège les erreurs contrôlées. Ce journal couvre le
    cas différent où Python, VS Code ou la machine disparaît avant que le bloc de
    rollback ne puisse s'exécuter. Aucune donnée du journal n'est exposée au LLM.
    """

    VERSION = 1

    def __init__(self, repo_root: str | Path, *, root_name: str = ".self_improvement_recovery") -> None:
        self.repo_root = Path(repo_root).resolve()
        if not root_name.startswith(".self_improvement_") or "/" in root_name or "\\" in root_name:
            raise ValueError("invalid_recovery_root_name")
        self.root = self.repo_root / root_name
        self.archive = self.root / "active.zip"
        self.metadata = self.root / "active.json"

    def has_pending(self) -> bool:
        return self.archive.is_file() or self.metadata.is_file()

    def create(self, transaction: EngineeringTransaction, objective: EngineeringObjective) -> None:
        if self.has_pending():
            raise RuntimeError("recovery_checkpoint_already_active")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_archive = self.root / "active.zip.tmp"
        tmp_metadata = self.root / "active.json.tmp"
        try:
            entries = []
            with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, content in sorted(transaction.snapshot.items(), key=lambda item: item[0].as_posix()):
                    if content is None:
                        continue
                    rel = path.relative_to(self.repo_root).as_posix()
                    raw_content = content if isinstance(content, bytes) else content.encode("utf-8")
                    archive.writestr(rel, raw_content)
                    entries.append(rel)
            archive_digest = hashlib.sha256(tmp_archive.read_bytes()).hexdigest()
            payload = {
                "version": self.VERSION,
                "created_unix": time.time(),
                "objective": objective.goal,
                "files": len(entries),
                "snapshot_bytes": transaction.snapshot_bytes,
                "archive_sha256": archive_digest,
            }
            tmp_metadata.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_archive.replace(self.archive)
            tmp_metadata.replace(self.metadata)
        except Exception:
            tmp_archive.unlink(missing_ok=True)
            tmp_metadata.unlink(missing_ok=True)
            # Ne pas laisser un demi-checkpoint bloquer tout le runtime.
            if not self.archive.exists():
                self.root.rmdir() if self.root.exists() and not any(self.root.iterdir()) else None
            raise

    def inspect(self) -> dict[str, Any]:
        if not self.has_pending():
            return {"pending": False}
        data: dict[str, Any] = {"pending": True}
        if self.metadata.is_file():
            try:
                raw = json.loads(self.metadata.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception as exc:
                data["metadata_error"] = str(exc)
        data["archive_present"] = self.archive.is_file()
        return data

    def restore(self) -> RecoveryResult:
        if not self.archive.is_file():
            return RecoveryResult(False, "recovery_archive_missing", details=self.inspect())
        try:
            if self.metadata.is_file():
                meta = json.loads(self.metadata.read_text(encoding="utf-8"))
                expected = str(meta.get("archive_sha256", "")) if isinstance(meta, dict) else ""
                actual = hashlib.sha256(self.archive.read_bytes()).hexdigest()
                if expected and actual != expected:
                    return RecoveryResult(False, "recovery_archive_integrity_failed")

            transaction = EngineeringTransaction(self.repo_root)
            with zipfile.ZipFile(self.archive, "r") as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if len(infos) > transaction.max_snapshot_files:
                    return RecoveryResult(False, "recovery_archive_too_many_files")
                if sum(max(0, int(info.file_size)) for info in infos) > transaction.max_snapshot_bytes:
                    return RecoveryResult(False, "recovery_archive_too_large")
                baseline: dict[Path, bytes] = {}
                for info in infos:
                    raw_name = info.filename.replace("\\", "/")
                    candidate = (self.repo_root / raw_name).resolve(strict=False)
                    try:
                        rel = candidate.relative_to(self.repo_root)
                    except ValueError:
                        return RecoveryResult(False, "recovery_archive_unsafe_member")
                    if transaction._ignored_relative(rel) or candidate.suffix.casefold() not in transaction.ALLOWED_SUFFIXES:
                        return RecoveryResult(False, "recovery_archive_unsafe_member")
                    if candidate in baseline:
                        return RecoveryResult(False, "recovery_archive_duplicate_member")
                    baseline[candidate] = archive.read(info)

            current = set(transaction._iter_transactional_files())
            baseline_paths = set(baseline)
            for path in sorted(current - baseline_paths, key=lambda item: len(item.parts), reverse=True):
                path.unlink(missing_ok=True)
                transaction._remove_empty_parents(path.parent)
            for path, raw in baseline.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)

            restored = len(baseline)
            self.clear()
            return RecoveryResult(True, "recovery_restored", restored_files=restored)
        except Exception as exc:
            return RecoveryResult(False, f"recovery_restore_failed: {exc}")

    def clear(self) -> None:
        self.archive.unlink(missing_ok=True)
        self.metadata.unlink(missing_ok=True)
        (self.root / "active.zip.tmp").unlink(missing_ok=True)
        (self.root / "active.json.tmp").unlink(missing_ok=True)
        try:
            self.root.rmdir()
        except OSError:
            pass


class EngineeringOrchestrator:
    """Supervise planning, exécution, replanification et validation globale."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        planner: EngineeringPlanner | None = None,
        campaign_manager: AutonomousCampaignManager | None = None,
        campaign_budget: CampaignBudget | None = None,
        global_validator: GlobalValidator | None = None,
        global_test_timeout_seconds: float = 600.0,
        minimum_coverage: float = 84.0,
        max_replans: int = 2,
        budget_policy: AdaptiveBudgetPolicy | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.planner = planner or EngineeringPlanner(repo_root=self.repo_root)
        self.campaign_manager = campaign_manager or AutonomousCampaignManager(repo_root=self.repo_root)
        self.campaign_budget = campaign_budget or CampaignBudget()
        self.global_validator = global_validator or self._default_global_validator
        self.global_test_timeout_seconds = float(global_test_timeout_seconds)
        self.minimum_coverage = float(minimum_coverage)
        if not 0.0 <= self.minimum_coverage <= 100.0:
            raise ValueError("minimum_coverage doit être compris entre 0 et 100.")
        self.max_replans = max(0, min(int(max_replans), 3))
        self.budget_policy = budget_policy or AdaptiveBudgetPolicy(
            self.campaign_budget,
            **({"task_duration": self.campaign_manager.required_task_seconds}
               if hasattr(self.campaign_manager, "required_task_seconds") else {}),
        )
        self.recovery = EngineeringRecoveryJournal(self.repo_root)

    @staticmethod
    def _downstream_minimum() -> dict[str, int]:
        """Minimum derived from the existing post-replan call envelopes."""
        return {
            "replan_min": 1,
            "candidate_first_min": 1,
            "semantic_repair_min": 1,
            "provider_fallback_min": 1,
            "reviewer_reserve": 1,
        }

    def run(
        self,
        objective: EngineeringObjective | str,
        *,
        campaign_budget: CampaignBudget | None = None,
        global_validator: GlobalValidator | None = None,
        protect_existing_tests: bool = False,
    ) -> EngineeringOutcome:
        """Exécute un objectif comme une transaction globale avec replanification bornée."""
        started = time.perf_counter()
        normalized = objective if isinstance(objective, EngineeringObjective) else EngineeringObjective(goal=str(objective))
        requested_model_limit = (campaign_budget or self.campaign_budget).max_model_calls
        model_budget = ModelCallBudget(requested_model_limit)
        if campaign_budget is not None and campaign_budget.execution_limits is not None:
            from self_improvement.execution_limits import ExecutionLimits
            limits = ExecutionLimits.from_dict(campaign_budget.execution_limits)
            if (campaign_budget.max_tasks, campaign_budget.max_source_files,
                campaign_budget.max_total_diff_lines, requested_model_limit) != (
                limits.max_tasks, limits.max_source_files, limits.max_diff_lines, limits.max_model_calls):
                raise ValueError("unsupported_execution_limits: worker campaign caps differ")
            limits.phase_timeout(480, minimum=480, future_seconds=300)
            model_budget.deadline_monotonic = limits.deadline_monotonic - limits.rollback_reserve_seconds - 300.0
        # Reservations are views over the single root counter. They are not
        # subtracted again by worker/parent accounting.
        if requested_model_limit >= 6:
            reserved_roles = {
                "replan": 1,
                "developer": min(3, requested_model_limit - 3),
                "reviewer": 1,
            }
        else:
            # Legacy/micro budgets still permit one initial Planner call. Replan
            # and cognitive review are denied explicitly when they cannot coexist
            # with the minimum Developer envelope.
            reserved_roles = {"developer": max(1, requested_model_limit - 1)}
        reservations = RoleBudgetState(model_budget, reserved_roles)
        planner_budget = reservations.child_for("planner")
        budget_snapshots = [reservations.snapshot("planner")]

        if self.recovery.has_pending():
            return self._outcome(
                normalized, started,
                final_decision="REJECT",
                reason="recovery_required_before_new_engineering_run",
                details={"phase": "recovery_guard", "recovery": self.recovery.inspect()},
            )

        try:
            plan_parameters = inspect.signature(self.planner.plan).parameters
            plan_budget_kwargs = (
                {"model_budget": planner_budget}
                if "model_budget" in plan_parameters
                or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in plan_parameters.values())
                else {}
            )
            plan = self.planner.plan(normalized, **plan_budget_kwargs)
            reservations.release("planner")
            self.planner.validate_plan(plan)
            if protect_existing_tests:
                protected_test_edits = self._existing_test_edits(plan)
                if protected_test_edits:
                    raise ValueError(
                        "autonomous_test_integrity_violation: modification de tests existants interdite: "
                        + ", ".join(protected_test_edits[:8])
                    )
        except Exception as exc:
            planner_trace = getattr(self.planner, "last_runtime_trace", None)
            return self._outcome(
                normalized, started,
                final_decision="REJECT",
                reason=f"planning_failed: {exc}",
                details={
                    "phase": "planning",
                    "planner_trace": planner_trace.to_dict() if callable(getattr(planner_trace, "to_dict", None)) else None,
                    "model_budget": model_budget.snapshot(),
                    "budget_snapshots": budget_snapshots,
                },
            )

        transaction = EngineeringTransaction(self.repo_root)
        try:
            transaction.capture_repository(self._plan_paths(plan))
            self.recovery.create(transaction, normalized)
        except Exception as exc:
            self.recovery.clear()
            return self._outcome(
                normalized, started, plan=plan,
                final_decision="REJECT",
                reason=f"transaction_setup_failed: {exc}",
                details={"phase": "transaction_setup"},
            )

        current_plan = plan
        replans_done = 0
        campaign: CampaignOutcome | None = None
        budget_decision: BudgetDecision | None = None
        downstream_minimum = self._downstream_minimum()
        downstream_total = sum(downstream_minimum.values())

        while True:
            try:
                if campaign_budget is not None:
                    effective_budget = copy.deepcopy(campaign_budget)
                    budget_decision = None
                else:
                    budget_decision = self.budget_policy.decide(current_plan, requested=self.campaign_budget)
                    effective_budget = budget_decision.budget
                developer_available = reservations.available_for("developer")
                # CampaignManager owns the nested Reviewer split. Include that
                # single reserved unit in its envelope so it is not subtracted at
                # both orchestration levels.
                campaign_available = developer_available + reservations.reserved_by_role.get("reviewer", 0)
                effective_budget.max_model_calls = min(effective_budget.max_model_calls, campaign_available)
                budget_snapshots.append(reservations.snapshot("developer"))
                campaign_parameters = inspect.signature(self.campaign_manager.run).parameters
                campaign_budget_kwargs = (
                    {"model_budget": model_budget}
                    if "model_budget" in campaign_parameters
                    or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in campaign_parameters.values())
                    else {}
                )
                campaign = self.campaign_manager.run(
                    current_plan.tasks,
                    budget=effective_budget,
                    **({"model_budget": model_budget} if campaign_budget_kwargs else {}),
                )
            except Exception as exc:
                return self._rollback_outcome(
                    transaction, normalized, started,
                    plan=current_plan,
                    final_decision="REJECT",
                    reason=f"campaign_failed: {exc}",
                    replans=replans_done,
                    details={"phase": "campaign"},
                )

            pre_decision, pre_reason = self._decision_from_campaign(campaign)
            preflight_reason = str(self._campaign_feedback(campaign).get("developer_feedback", {}).get("failure_reason") or "")
            if preflight_reason.startswith("TEST_INFRA_FAILURE"):
                return self._rollback_outcome(
                    transaction, normalized, started, plan=current_plan, campaign=campaign,
                    final_decision="REJECT", reason=preflight_reason, replans=replans_done,
                    details={"phase": "baseline_preflight", "model_budget": model_budget.snapshot()},
                )
            stale_objective = preflight_reason.startswith("ALREADY_SATISFIED")
            if pre_decision == "ACCEPT":
                break

            # Un UNCERTAIN est une occasion de corriger le plan, pas d'empiler des
            # changements sur une base partielle. On revient d'abord à la baseline.
            replan_fn = getattr(self.planner, "replan", None)
            # A post-replan campaign is already the bounded corrective pass. If it
            # remains uncertain, another LLM replan mostly repeats the same work
            # and consumes the Reviewer/Judge reserve without a validated
            # candidate. Return the measured failure from the clean baseline.
            can_replan = not bool(current_plan.metadata.get("post_replan"))
            if campaign_budget is not None and campaign_budget.execution_limits is not None:
                can_replan = can_replan and (campaign.tasks_run == 0 or stale_objective)
            if (
                pre_decision == "UNCERTAIN"
                and can_replan
                and replans_done < self.max_replans
                and callable(replan_fn)
            ):
                rb_ok, rb_err = transaction.restore()
                if not rb_ok:
                    return self._outcome(
                        normalized, started, plan=current_plan, campaign=campaign,
                        final_decision="REJECT",
                        reason=f"engineering_rollback_failed: {rb_err}",
                        rollback_performed=True, replans=replans_done,
                        details={"phase": "replan_rollback"},
                    )
                try:
                    feedback = self._campaign_feedback(campaign)
                    feedback["previous_plan"] = current_plan.to_dict()
                    feedback["previous_target_files"] = self._plan_paths(current_plan)
                    replan_parameters = inspect.signature(replan_fn).parameters
                    # The reservation guarantees that replan can start; its child
                    # may still use the globally available envelope so the bounded
                    # router can fall back after one provider failure.
                    replan_budget = reservations.child_for("replan")
                    budget_snapshots.append(reservations.snapshot("replan"))
                    replan_budget_kwargs = (
                        {"model_budget": replan_budget}
                        if "model_budget" in replan_parameters
                        or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in replan_parameters.values())
                        else {}
                    )
                    new_plan = replan_fn(normalized, current_plan, feedback, **replan_budget_kwargs)
                    reservations.release("replan")
                    self.planner.validate_plan(new_plan)
                    if protect_existing_tests:
                        protected_test_edits = self._existing_test_edits(new_plan)
                        if protected_test_edits:
                            raise ValueError(
                                "autonomous_test_integrity_violation: modification de tests existants interdite: "
                                + ", ".join(protected_test_edits[:8])
                            )
                    if self._plan_fingerprint(new_plan) == self._plan_fingerprint(current_plan):
                        if not current_plan.metadata.get("deterministic_provider_contract"):
                            fallback_fn = getattr(self.planner, "deterministic_replan", None)
                            if not callable(fallback_fn):
                                raise ValueError("replan_identical_to_previous_plan")
                            new_plan = fallback_fn(normalized, current_plan, feedback)
                            self.planner.validate_plan(new_plan)
                            if self._plan_fingerprint(new_plan) == self._plan_fingerprint(current_plan):
                                raise ValueError("replan_identical_after_deterministic_fallback")
                    transaction.capture(self._plan_paths(new_plan))
                    new_plan.metadata["post_replan"] = True
                    new_plan.metadata["replan_source_fingerprint"] = self._plan_fingerprint(current_plan)
                    for task in new_plan.tasks:
                        task.metadata["post_replan"] = True
                        task.metadata["candidate_first"] = True
                    current_plan = new_plan
                    replans_done += 1
                    continue
                except Exception as exc:
                    replan_error = str(exc)
                    if campaign_budget_kwargs:
                        campaign.budget_usage["total_model_calls"] = model_budget.used_calls
                        campaign.budget_usage["logical_model_requests"] = model_budget.logical_requests
                    if "MODEL_BUDGET_EXHAUSTED" in replan_error or "budget" in replan_error.casefold():
                        replan_error = f"REPLAN_BUDGET_FAILURE: {replan_error}"
                    elif any(kind in replan_error for kind in (
                        "ALL_ROUTES_EXHAUSTED", "ROUTES_NOT_AVAILABLE", "ROUTES_FAILED",
                        "ROUTES_SKIPPED", "DEADLINE_EXHAUSTED",
                    )):
                        replan_error = f"REPLAN_PROVIDER_FAILURE: {replan_error}"
                    elif "UNKNOWN_TARGET_REFERENCE" in replan_error:
                        replan_error = f"REPLAN_TARGET_FAILURE: {replan_error}"
                    elif "REPLAN_" not in replan_error:
                        replan_error = f"REPLAN_INVALID_RESPONSE: {replan_error}"
                    return self._rollback_outcome(
                        transaction, normalized, started, plan=current_plan, campaign=campaign,
                        final_decision="UNCERTAIN",
                        reason=f"replanning_failed: {replan_error}",
                        replans=replans_done,
                        details={"phase": "replanning", "campaign_reason": pre_reason,
                                 "budget_snapshots": budget_snapshots},
                    )

            return self._rollback_outcome(
                transaction, normalized, started,
                plan=current_plan,
                campaign=campaign,
                final_decision=pre_decision,
                reason=pre_reason,
                replans=replans_done,
                details={"phase": "campaign_review", "budget_snapshots": budget_snapshots},
            )

        # Audit du blast radius avant même la suite globale. Un nouveau fichier
        # non annoncé est considéré comme une violation de contrat et entraîne un
        # rollback global. Les modifications de fichiers existants restent permises
        # uniquement si elles proviennent de la pile transactionnelle connue.
        planned_paths = self._plan_paths(current_plan)
        unexpected_new = transaction.unplanned_new_files(planned_paths)
        changed_paths = transaction.changed_paths()
        unplanned_changes = transaction.unplanned_changed_paths(planned_paths)
        if unexpected_new or unplanned_changes:
            return self._rollback_outcome(
                transaction, normalized, started,
                plan=current_plan, campaign=campaign,
                final_decision="REJECT",
                reason="unplanned_repository_changes_detected",
                replans=replans_done,
                details={
                    "phase": "change_audit",
                    "unexpected_new_files": unexpected_new[:20],
                    "unplanned_changes": unplanned_changes[:20],
                    "changed_paths": changed_paths[:40],
                },
            )

        validator = global_validator or self.global_validator
        # Le validateur est une sonde, jamais un mécanisme de réparation. On prend
        # une empreinte juste avant et juste après pour refuser toute mutation de
        # code/config, même si elle touche un chemin pourtant prévu par le plan.
        pre_validation_state = transaction.current_state()
        try:
            global_result = validator()
        except Exception as exc:
            global_result = GlobalValidationResult(
                success=False,
                reason=f"global_validator_crash: {exc}",
                tests_run=0,
                tests_failed=1,
            )
        post_validation_state = transaction.current_state()
        validator_mutations = transaction.state_diff(pre_validation_state, post_validation_state)
        if validator_mutations:
            return self._rollback_outcome(
                transaction, normalized, started,
                plan=current_plan, campaign=campaign,
                global_validation=global_result if isinstance(global_result, GlobalValidationResult) else None,
                final_decision="REJECT",
                reason="global_validator_mutated_repository",
                replans=replans_done,
                details={
                    "phase": "global_validation_audit",
                    "validator_mutations": validator_mutations[:40],
                },
            )

        if not isinstance(global_result, GlobalValidationResult):
            return self._rollback_outcome(
                transaction, normalized, started,
                plan=current_plan, campaign=campaign,
                final_decision="UNCERTAIN",
                reason="global_validation_invalid_result",
                replans=replans_done,
                details={"phase": "global_validation"},
            )

        if not global_result.success:
            return self._rollback_outcome(
                transaction, normalized, started,
                plan=current_plan, campaign=campaign, global_validation=global_result,
                final_decision="REJECT",
                reason=global_result.reason or "global_regression_detected",
                replans=replans_done,
                details={"phase": "global_validation"},
            )

        try:
            self.recovery.clear()
        except Exception as exc:
            return self._rollback_outcome(
                transaction, normalized, started,
                plan=current_plan, campaign=campaign, global_validation=global_result,
                final_decision="REJECT",
                reason=f"recovery_checkpoint_cleanup_failed: {exc}",
                replans=replans_done,
                details={"phase": "recovery_cleanup"},
            )

        return self._outcome(
            normalized, started,
            plan=current_plan,
            campaign=campaign,
            global_validation=global_result,
            final_decision="ACCEPT",
            reason="engineering_objective_completed_and_globally_validated",
            replans=replans_done,
            details={"phase": "completed", "transaction_paths": len(transaction.snapshot),
                     "changed_paths": changed_paths[:40], "snapshot_bytes": transaction.snapshot_bytes,
                     "budget_snapshots": budget_snapshots, "model_budget": model_budget.snapshot(),
                     "downstream_minimum": dict(downstream_minimum),
                     "downstream_total": downstream_total},
        )

    @staticmethod
    def _looks_like_test_path(path: str) -> bool:
        candidate = Path(path)
        name = candidate.name.casefold()
        parents = {part.casefold() for part in candidate.parts[:-1]}
        return candidate.suffix.casefold() == ".py" and (
            name.startswith("test_") or bool(parents & {"test", "tests"})
        )

    def _existing_test_edits(self, plan: EngineeringPlan) -> list[str]:
        """Tests déjà présents que le plan tente de modifier.

        En auto-amélioration, l'agent peut créer de nouveaux tests de régression et
        exécuter les tests existants, mais pas réécrire son examen après avoir vu un
        échec. Les objectifs humains normaux gardent leur flexibilité historique.
        """
        blocked: list[str] = []
        for task in plan.tasks:
            for raw in task.target_files:
                if not self._looks_like_test_path(raw):
                    continue
                candidate = (self.repo_root / raw).resolve(strict=False)
                try:
                    candidate.relative_to(self.repo_root)
                except ValueError:
                    continue
                if candidate.is_file() and raw not in blocked:
                    blocked.append(raw)
        return blocked

    @staticmethod
    def _plan_paths(plan: EngineeringPlan) -> list[str]:
        return list(dict.fromkeys(
            path
            for task in plan.tasks
            for path in [*task.target_files, *task.tests]
            if path
        ))

    @staticmethod
    def _plan_fingerprint(plan: EngineeringPlan) -> str:
        """Empreinte sémantique du plan, indépendante des statuts d'exécution."""
        payload = []
        for task in plan.tasks:
            payload.append({
                "title": task.title.strip(),
                "task": task.task.strip(),
                "problem_type": (task.problem_type or "").strip(),
                "target_files": list(task.target_files),
                "tests": list(task.tests),
                "dependencies": list(task.dependencies),
            })
        raw = repr(payload).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _campaign_feedback(campaign: CampaignOutcome) -> dict[str, Any]:
        """Feedback borné, suffisant pour replanifier sans exposer de données cachées."""
        events = campaign.events[-12:]
        failed_event = next((event for event in reversed(events) if event.get("task_id") and event.get("result") in {"REJECT", "UNCERTAIN"}), {})
        details = failed_event.get("details", {}) if isinstance(failed_event, dict) else {}
        developer = details.get("developer_result", {}) if isinstance(details, dict) else {}
        return {
            "campaign_id": campaign.campaign_id,
            "stop_reason": campaign.stop_reason,
            "tasks_total": campaign.tasks_total,
            "tasks_accepted": campaign.tasks_accepted,
            "tasks_rejected": campaign.tasks_rejected,
            "tasks_uncertain": campaign.tasks_uncertain,
            "tasks_blocked": campaign.tasks_blocked,
            "accepted_task_ids": campaign.accepted_task_ids,
            "rejected_task_ids": campaign.rejected_task_ids,
            "uncertain_task_ids": campaign.uncertain_task_ids,
            "remaining_tasks": campaign.remaining_tasks[:15],
            "events_tail": events,
            "failed_task": failed_event.get("task_id") if isinstance(failed_event, dict) else None,
            "failure_reason": details.get("reason") if isinstance(details, dict) else None,
            "developer_feedback": developer,
            "judge_feedback": details.get("judge_feedback") if isinstance(details, dict) else None,
            "files_examined": developer.get("files_examined", []) if isinstance(developer, dict) else [],
            "previous_attempt_summary": developer.get("summary", "") if isinstance(developer, dict) else "",
        }

    @staticmethod
    def _decision_from_campaign(campaign: CampaignOutcome) -> tuple[str, str]:
        if campaign.stop_reason in {
            StopReason.SAFETY_VIOLATION.value,
            StopReason.ROLLBACK_FAILURE.value,
        }:
            return "REJECT", f"campaign_stopped: {campaign.stop_reason}"
        if campaign.tasks_rejected > 0:
            return "REJECT", "campaign_contains_rejected_tasks"
        if campaign.tasks_uncertain > 0:
            return "UNCERTAIN", "campaign_contains_uncertain_tasks"
        if campaign.tasks_blocked > 0:
            return "REJECT", "campaign_contains_blocked_tasks"
        if campaign.tasks_total <= 0:
            return "REJECT", "campaign_contains_no_tasks"
        if campaign.tasks_accepted != campaign.tasks_total:
            return "UNCERTAIN", "campaign_incomplete"
        if not campaign.success:
            return "REJECT", f"campaign_unsuccessful: {campaign.stop_reason}"
        return "ACCEPT", "campaign_completed"

    def recovery_status(self) -> dict[str, Any]:
        """Expose uniquement les métadonnées locales du checkpoint, jamais son contenu."""
        return self.recovery.inspect()

    def recover_repository(self) -> RecoveryResult:
        """Restaure une baseline persistante après un run interrompu brutalement."""
        return self.recovery.restore()

    def validate_repository(self, *, timeout_seconds: float | None = None) -> GlobalValidationResult:
        """API publique pour la validation globale utilisée par les agents supérieurs."""
        return self._default_global_validator(timeout_seconds=timeout_seconds)

    def _default_global_validator(self, *, timeout_seconds: float | None = None) -> GlobalValidationResult:
        """Valide tests + couverture avec une configuration indépendante du candidat.

        Le seuil et la configuration coverage ne sont pas lus depuis ``pyproject.toml``
        ou ``.coveragerc`` : même si une future régression de la politique de chemins
        autorisait leur modification, le candidat ne pourrait pas s'auto-noter plus
        généreusement. Les artefacts coverage sont écrits hors du repository.
        """
        started = time.perf_counter()
        coverage_config = """[run]
branch = True
source = .
omit =
    .venv/*
    venv/*
    backups/*
    conftest.py
    run_tests.py
    test_*.py
    .self_improvement_holdout/*
    .self_improvement_worktrees/*
    .self_improvement_discoveries/*
    .self_improvement_recovery/*

[report]
show_missing = True
skip_covered = False
precision = 1
"""

        try:
            with tempfile.TemporaryDirectory(prefix="projet_ia_global_validation_") as temp_dir:
                temp_root = Path(temp_dir)
                config_path = temp_root / "coverage.ini"
                json_path = temp_root / "coverage.json"
                data_path = temp_root / "coverage.data"
                config_path.write_text(coverage_config, encoding="utf-8")

                cmd = [
                    sys.executable,
                    "-m",
                    "pytest",
                    ".",
                    "-q",
                    "-o",
                    "addopts=",
                    "--strict-config",
                    "--strict-markers",
                    "-p",
                    "no:cacheprovider",
                    "--ignore=.self_improvement_holdout",
                    "--ignore=.self_improvement_worktrees",
                    "--cov=.",
                    f"--cov-config={config_path}",
                    "--cov-report=term-missing",
                    f"--cov-report=json:{json_path}",
                    f"--cov-fail-under={self.minimum_coverage:g}",
                ]
                env = sanitized_child_environment(extra={"COVERAGE_FILE": str(data_path)})
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    timeout=self.global_test_timeout_seconds if timeout_seconds is None else min(self.global_test_timeout_seconds, timeout_seconds),
                    env=env,
                )

                coverage_percent: float | None = None
                if json_path.is_file():
                    try:
                        coverage_payload = json.loads(json_path.read_text(encoding="utf-8"))
                        coverage_percent = float(coverage_payload.get("totals", {}).get("percent_covered"))
                    except (TypeError, ValueError, json.JSONDecodeError, OSError):
                        coverage_percent = None

                output = f"{proc.stdout}\n{proc.stderr}".strip()
                passed, failed = self._parse_pytest_counts(output)
                coverage_ok = coverage_percent is not None and coverage_percent + 1e-9 >= self.minimum_coverage
                success = proc.returncode == 0 and coverage_ok
                if proc.returncode != 0:
                    reason = "global_tests_or_coverage_failed"
                elif coverage_percent is None:
                    reason = "global_coverage_result_missing"
                elif not coverage_ok:
                    reason = "global_coverage_below_threshold"
                else:
                    reason = "global_tests_and_coverage_passed"
                return GlobalValidationResult(
                    success=success,
                    reason=reason,
                    tests_run=passed + failed,
                    tests_failed=failed,
                    duration_seconds=round(time.perf_counter() - started, 2),
                    details={
                        "returncode": proc.returncode,
                        "coverage_percent": coverage_percent,
                        "minimum_coverage": self.minimum_coverage,
                        "output_tail": output[-4000:],
                    },
                )
        except subprocess.TimeoutExpired:
            return GlobalValidationResult(
                success=False,
                reason="global_tests_timeout",
                duration_seconds=round(time.perf_counter() - started, 2),
            )
        except Exception as exc:
            return GlobalValidationResult(
                success=False,
                reason=f"global_tests_crash: {exc}",
                duration_seconds=round(time.perf_counter() - started, 2),
            )

    @staticmethod
    def _parse_pytest_counts(output: str) -> tuple[int, int]:
        import re

        passed_match = re.search(r"(\d+)\s+passed", output)
        failed_match = re.search(r"(\d+)\s+failed", output)
        passed = int(passed_match.group(1)) if passed_match else 0
        failed = int(failed_match.group(1)) if failed_match else 0
        return passed, failed

    def _rollback_outcome(
        self,
        transaction: EngineeringTransaction,
        objective: EngineeringObjective,
        started: float,
        *,
        final_decision: str,
        reason: str,
        plan: EngineeringPlan | None = None,
        campaign: CampaignOutcome | None = None,
        global_validation: GlobalValidationResult | None = None,
        replans: int = 0,
        details: dict[str, Any] | None = None,
    ) -> EngineeringOutcome:
        ok, error = transaction.restore()
        merged = dict(details or {})
        merged["transaction_rollback"] = "ok" if ok else "failed"
        if ok:
            try:
                self.recovery.clear()
                merged["recovery_checkpoint"] = "cleared"
            except Exception as cleanup_exc:
                ok = False
                error = f"recovery_cleanup_failed: {cleanup_exc}"
        if error:
            merged["rollback_error"] = error
            final_decision = "REJECT"
            reason = f"engineering_rollback_failed: {error} | original_reason: {reason}"
        return self._outcome(
            objective, started,
            final_decision=final_decision,
            reason=reason,
            plan=plan,
            campaign=campaign,
            global_validation=global_validation,
            rollback_performed=True,
            replans=replans,
            details=merged,
        )

    @staticmethod
    def _outcome(
        objective: EngineeringObjective,
        started: float,
        *,
        final_decision: str,
        reason: str,
        plan: EngineeringPlan | None = None,
        campaign: CampaignOutcome | None = None,
        global_validation: GlobalValidationResult | None = None,
        rollback_performed: bool = False,
        replans: int = 0,
        details: dict[str, Any] | None = None,
    ) -> EngineeringOutcome:
        return EngineeringOutcome(
            objective=objective,
            final_decision=final_decision,
            reason=reason,
            success=final_decision == "ACCEPT",
            plan=plan,
            campaign_outcome=campaign,
            global_validation=global_validation,
            tasks_accepted=campaign.tasks_accepted if campaign else 0,
            tasks_rejected=campaign.tasks_rejected if campaign else 0,
            tasks_uncertain=campaign.tasks_uncertain if campaign else 0,
            tasks_blocked=campaign.tasks_blocked if campaign else 0,
            duration_seconds=round(time.perf_counter() - started, 2),
            rollback_performed=rollback_performed,
            replans=replans,
            details=dict(details or {}),
        )
