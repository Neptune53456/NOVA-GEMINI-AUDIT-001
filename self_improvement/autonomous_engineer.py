"""Agent d'auto-amélioration V3 de haut niveau, borné, mémorisé et vérifiable.

Cette couche ne considère jamais une auto-modification comme une amélioration par
simple intuition du modèle. Un cycle n'est conservé que si :

1. le dépôt public est sain avant le cycle ;
2. un objectif est dérivé exclusivement d'échecs TRAIN publics ;
3. l'EngineeringOrchestrator termine son chantier ;
4. la suite de tests publique passe ;
5. un benchmark public exécuté dans un processus frais démontre un gain minimal ;
6. aucun score de sécurité/validation ne régresse.

En cas d'échec de 3 à 6, la transaction globale de l'EngineeringOrchestrator
restaure automatiquement la baseline du cycle.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Protocol

from self_improvement.engineering_orchestrator import (
    EngineeringOrchestrator,
    EngineeringOutcome,
    GlobalValidationResult,
)
from self_improvement.engineering_planner import EngineeringObjective
from self_improvement.evaluator import AcceptanceDecision, compare_security_results
from self_improvement.failure_analyzer import cluster_failures
from self_improvement.experiment_memory import ExperimentMemory, ExperimentRecord
from self_improvement.agent_path_policy import is_model_private_path
from self_improvement.process_safety import sanitized_child_environment
from self_improvement.models import BenchmarkReport, CriterionResult, ScenarioResult


PUBLIC_SPLITS = frozenset({"train", "validation"})
OPTIMIZATION_SPLIT = "train"


class PublicEvaluator(Protocol):
    """Contrat minimal d'une évaluation publique isolée."""

    def evaluate(self) -> BenchmarkReport: ...


@dataclass(frozen=True)
class SelfImprovementBudget:
    """Budget dur pour empêcher les boucles d'auto-modification non bornées."""

    max_cycles: int = 3
    max_minutes: float = 45.0
    minimum_improvement: float = 0.5
    target_score: float = 98.0

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_cycles) <= 10:
            raise ValueError("max_cycles doit être compris entre 1 et 10.")
        if not 0.1 <= float(self.minimum_improvement) <= 20.0:
            raise ValueError("minimum_improvement doit être compris entre 0.1 et 20.")
        if not 1.0 <= float(self.max_minutes) <= 24 * 60:
            raise ValueError("max_minutes doit être compris entre 1 et 1440.")
        if not 0.0 <= float(self.target_score) <= 100.0:
            raise ValueError("target_score doit être compris entre 0 et 100.")


@dataclass
class PublicEvaluationSummary:
    score: float
    security_score: float
    validation_score: float
    failures: int
    scenario_count: int
    dataset_version: str
    train_score: float = 0.0

    @classmethod
    def from_report(cls, report: BenchmarkReport) -> "PublicEvaluationSummary":
        validation = _split_score(report, "validation")
        train = _split_score(report, OPTIMIZATION_SPLIT)
        return cls(
            score=float(report.score),
            security_score=float(report.security_score),
            validation_score=round(validation, 2),
            failures=len(report.failures),
            scenario_count=len(report.results),
            dataset_version=str(report.dataset_version),
            train_score=round(train, 2),
        )


@dataclass
class SelfImprovementCycle:
    cycle: int
    decision: str
    reason: str
    objective: str = ""
    baseline: PublicEvaluationSummary | None = None
    candidate: PublicEvaluationSummary | None = None
    improvement: float = 0.0
    engineering: EngineeringOutcome | None = None
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "decision": self.decision,
            "reason": self.reason,
            "objective": self.objective,
            "baseline": asdict(self.baseline) if self.baseline else None,
            "candidate": asdict(self.candidate) if self.candidate else None,
            "improvement": self.improvement,
            "engineering": self.engineering.to_dict() if self.engineering else None,
            "duration_seconds": self.duration_seconds,
            "details": self.details,
        }


@dataclass
class SelfImprovementOutcome:
    final_decision: str
    reason: str
    success: bool
    initial: PublicEvaluationSummary | None = None
    final: PublicEvaluationSummary | None = None
    cycles: list[SelfImprovementCycle] = field(default_factory=list)
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_decision": self.final_decision,
            "reason": self.reason,
            "success": self.success,
            "initial": asdict(self.initial) if self.initial else None,
            "final": asdict(self.final) if self.final else None,
            "cycles": [cycle.to_dict() for cycle in self.cycles],
            "duration_seconds": self.duration_seconds,
            "details": self.details,
        }


class IsolatedPublicBenchmarkEvaluator:
    """Exécute train+validation dans un nouveau processus Python.

    Un processus frais est volontaire : les modules modifiés par l'agent ne doivent
    pas rester masqués par le cache d'import du processus orchestrateur.
    """

    def __init__(self, repo_root: str | Path, *, timeout_seconds: float = 180.0) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    def evaluate(self) -> BenchmarkReport:
        with tempfile.NamedTemporaryFile(
            prefix="public_benchmark_", suffix=".json", delete=False,
        ) as descriptor:
            output = Path(descriptor.name)
        command = [
            sys.executable,
            "-m",
            "self_improvement.benchmark_runner",
            "--split",
            "train",
            "--split",
            "validation",
            "--output",
            str(output),
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=sanitized_child_environment(),
            )
            if proc.returncode != 0:
                tail = f"{proc.stdout}\n{proc.stderr}".strip()[-3000:]
                raise RuntimeError(f"public_benchmark_failed({proc.returncode}): {tail}")
            if not output.is_file():
                raise RuntimeError("public_benchmark_missing_output")
            raw = json.loads(output.read_text(encoding="utf-8"))
            report = _report_from_dict(raw)
            _assert_public_report(report)
            return report
        finally:
            output.unlink(missing_ok=True)


ObjectiveBuilder = Callable[[BenchmarkReport], EngineeringObjective]


class AutonomousSelfImprovementAgent:
    """Boucle de niveau supérieur : observe -> planifie -> agit -> mesure -> garde/rollback."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        engineering_orchestrator: EngineeringOrchestrator | None = None,
        evaluator: PublicEvaluator | Callable[[], BenchmarkReport] | None = None,
        objective_builder: ObjectiveBuilder | None = None,
        memory: ExperimentMemory | None = None,
        logger: Callable[[str], None] | None = print,
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.engineering = engineering_orchestrator or EngineeringOrchestrator(self.repo_root)
        self.evaluator = evaluator or IsolatedPublicBenchmarkEvaluator(self.repo_root)
        self.objective_builder = objective_builder or build_public_improvement_objective
        self.memory = memory or ExperimentMemory()
        self.logger = logger or (lambda _message: None)

    def run(
        self,
        *,
        budget: SelfImprovementBudget | None = None,
        dry_run: bool = False,
    ) -> SelfImprovementOutcome:
        cfg = budget or SelfImprovementBudget()
        started = time.perf_counter()
        cycles: list[SelfImprovementCycle] = []

        try:
            baseline = self._evaluate_public()
        except Exception as exc:
            return self._outcome(
                started, "REJECT", f"baseline_evaluation_failed: {exc}", False,
                cycles=cycles,
            )

        initial_summary = PublicEvaluationSummary.from_report(baseline)
        self.logger(
            f"[AutonomousEngineer] Baseline TRAIN {_split_score(baseline, OPTIMIZATION_SPLIT):.2f}/100 "
            f"({len(_train_failures(baseline))} échecs train)."
        )

        # Aucun auto-développement ne démarre depuis une baseline de tests cassée.
        try:
            baseline_tests = self.engineering.validate_repository()
        except Exception as exc:
            baseline_tests = GlobalValidationResult(False, f"baseline_tests_crash: {exc}")
        if not baseline_tests.success:
            return self._outcome(
                started,
                "REJECT",
                baseline_tests.reason or "baseline_tests_failed",
                False,
                initial=initial_summary,
                final=initial_summary,
                cycles=cycles,
                details={"baseline_validation": baseline_tests.to_dict()},
            )

        if _split_score(baseline, OPTIMIZATION_SPLIT) >= cfg.target_score:
            return self._outcome(
                started, "TARGET_REACHED", "target_score_already_reached", True,
                initial=initial_summary, final=initial_summary, cycles=cycles,
            )
        if not _train_failures(baseline):
            return self._outcome(
                started, "NO_ACTION", "no_train_failure_to_improve", True,
                initial=initial_summary, final=initial_summary, cycles=cycles,
            )

        current = baseline
        repo_fingerprint = self._repository_fingerprint()
        attempted_scenario_ids = self._historical_failed_scenario_ids(
            current.dataset_version, repo_fingerprint=repo_fingerprint
        )
        attempted_objectives: set[str] = set()
        for cycle_index in range(1, int(cfg.max_cycles) + 1):
            if (time.perf_counter() - started) / 60.0 >= cfg.max_minutes:
                return self._outcome(
                    started, "STOPPED", "time_budget_exhausted", True,
                    initial=initial_summary,
                    final=PublicEvaluationSummary.from_report(current),
                    cycles=cycles,
                )

            cycle_started = time.perf_counter()
            try:
                if self.objective_builder is build_public_improvement_objective:
                    objective = build_public_improvement_objective(
                        current,
                        exclude_scenario_ids=attempted_scenario_ids,
                    )
                else:
                    objective = self.objective_builder(current)
                if not isinstance(objective, EngineeringObjective) or not objective.goal.strip():
                    raise ValueError("objective_builder_invalid_result")
                model_visible_objective = "\n".join([objective.goal, *objective.constraints])
                if _mentions_guarded_evaluation(model_visible_objective):
                    raise ValueError("objective_builder_referenced_guarded_evaluation")
                objective_fingerprint = self._objective_fingerprint(objective)
                if objective_fingerprint in attempted_objectives:
                    raise ValueError("objective_builder_repeated_same_objective")
                attempted_objectives.add(objective_fingerprint)
            except Exception as exc:
                cycles.append(SelfImprovementCycle(
                    cycle=cycle_index,
                    decision="REJECT",
                    reason=f"objective_generation_failed: {exc}",
                    baseline=PublicEvaluationSummary.from_report(current),
                    duration_seconds=round(time.perf_counter() - cycle_started, 2),
                ))
                break

            selected_scenario_ids = [
                str(item) for item in objective.metadata.get("scenario_ids", [])
                if isinstance(item, str)
            ]
            attempted_scenario_ids.update(selected_scenario_ids)
            self.logger(f"[AutonomousEngineer] Cycle {cycle_index}: objectif train généré.")
            if dry_run:
                cycles.append(SelfImprovementCycle(
                    cycle=cycle_index,
                    decision="DRY_RUN",
                    reason="objective_generated_without_repository_mutation",
                    objective=objective.goal,
                    baseline=PublicEvaluationSummary.from_report(current),
                    duration_seconds=round(time.perf_counter() - cycle_started, 2),
                ))
                return self._outcome(
                    started, "DRY_RUN", "dry_run_completed", True,
                    initial=initial_summary,
                    final=PublicEvaluationSummary.from_report(current),
                    cycles=cycles,
                )

            candidate_box: dict[str, Any] = {}
            baseline_for_cycle = current

            def evidence_validator() -> GlobalValidationResult:
                tests = self.engineering.validate_repository()
                if not tests.success:
                    return tests
                try:
                    candidate = self._evaluate_public()
                except Exception as exc:
                    return GlobalValidationResult(
                        False,
                        f"candidate_public_evaluation_failed: {exc}",
                        tests_run=tests.tests_run,
                        tests_failed=max(1, tests.tests_failed),
                        details={"test_validation": tests.to_dict()},
                    )
                candidate_box["report"] = candidate
                decision = _decide_guarded_self_improvement(
                    baseline_for_cycle,
                    candidate,
                    minimum_train_improvement=float(cfg.minimum_improvement),
                )
                details = {
                    "baseline_train_score": round(_split_score(baseline_for_cycle, OPTIMIZATION_SPLIT), 2),
                    "candidate_train_score": round(_split_score(candidate, OPTIMIZATION_SPLIT), 2),
                    "train_improvement": decision.improvement,
                    # Ces métriques restent dans le rapport d'audit local du garde ;
                    # elles ne sont jamais réinjectées dans le générateur d'objectif.
                    "baseline_security_score": baseline_for_cycle.security_score,
                    "candidate_security_score": candidate.security_score,
                    "baseline_validation_score": round(_split_score(baseline_for_cycle, "validation"), 2),
                    "candidate_validation_score": round(_split_score(candidate, "validation"), 2),
                    "security_changes": decision.security_changes,
                    "acceptance_reasons": list(decision.reasons),
                    "test_validation": tests.to_dict(),
                }
                if int(candidate.metrics.get("network_calls", 0) or 0) != 0:
                    details["acceptance_reasons"].append("Le benchmark public a effectué un appel réseau.")
                    return GlobalValidationResult(
                        False,
                        "public_benchmark_network_activity_detected",
                        tests_run=tests.tests_run,
                        tests_failed=tests.tests_failed,
                        details=details,
                    )
                return GlobalValidationResult(
                    decision.accepted,
                    "public_benchmark_improved" if decision.accepted else "public_benchmark_not_improved",
                    tests_run=tests.tests_run,
                    tests_failed=tests.tests_failed,
                    details=details,
                )

            engineering_result = self.engineering.run(
                objective,
                global_validator=evidence_validator,
                protect_existing_tests=True,
            )
            candidate = candidate_box.get("report")
            baseline_summary = PublicEvaluationSummary.from_report(baseline_for_cycle)
            candidate_summary = PublicEvaluationSummary.from_report(candidate) if candidate else None
            improvement = (
                round(_split_score(candidate, OPTIMIZATION_SPLIT) - _split_score(baseline_for_cycle, OPTIMIZATION_SPLIT), 2)
                if candidate else 0.0
            )
            cycle = SelfImprovementCycle(
                cycle=cycle_index,
                decision=engineering_result.final_decision,
                reason=engineering_result.reason,
                objective=objective.goal,
                baseline=baseline_summary,
                candidate=candidate_summary,
                improvement=improvement,
                engineering=engineering_result,
                duration_seconds=round(time.perf_counter() - cycle_started, 2),
                details={
                    "repository_rollback": engineering_result.rollback_performed,
                    "evidence_collected": candidate is not None,
                },
            )
            cycles.append(cycle)
            self._record_cycle_memory(
                cycle,
                dataset_version=current.dataset_version,
                scenario_ids=selected_scenario_ids,
                repo_fingerprint=repo_fingerprint,
            )

            if engineering_result.final_decision != "ACCEPT" or candidate is None:
                # La transaction de l'EngineeringOrchestrator a déjà restauré le dépôt.
                # Contrairement à V2, un échec n'arrête pas forcément toute la boucle :
                # si le budget le permet, le cycle suivant cible un autre groupe train.
                if cycle_index < int(cfg.max_cycles) and self._has_unattempted_train_failures(
                    current, attempted_scenario_ids
                ):
                    self.logger(
                        f"[AutonomousEngineer] Cycle {cycle_index} non accepté; "
                        "rollback confirmé, tentative d'une autre faiblesse train."
                    )
                    continue
                return self._outcome(
                    started,
                    engineering_result.final_decision,
                    f"self_improvement_cycle_not_accepted: {engineering_result.reason}",
                    False,
                    initial=initial_summary,
                    final=PublicEvaluationSummary.from_report(current),
                    cycles=cycles,
                )

            current = candidate
            # Une amélioration acceptée change l'état du code : les échecs tentés
            # dans l'ancienne version peuvent redevenir pertinents. On repart donc
            # avec l'historique associé au NOUVEL état du repository au lieu de
            # bannir définitivement ces scénarios.
            repo_fingerprint = self._repository_fingerprint()
            attempted_scenario_ids = self._historical_failed_scenario_ids(
                current.dataset_version, repo_fingerprint=repo_fingerprint
            )
            attempted_objectives.clear()
            self.logger(
                f"[AutonomousEngineer] Cycle {cycle_index} ACCEPT TRAIN: "
                f"{_split_score(baseline_for_cycle, OPTIMIZATION_SPLIT):.2f} -> "
                f"{_split_score(current, OPTIMIZATION_SPLIT):.2f}."
            )
            if _split_score(current, OPTIMIZATION_SPLIT) >= cfg.target_score:
                return self._outcome(
                    started, "TARGET_REACHED", "target_score_reached", True,
                    initial=initial_summary,
                    final=PublicEvaluationSummary.from_report(current),
                    cycles=cycles,
                )
            if not _train_failures(current):
                return self._outcome(
                    started, "ACCEPT", "no_train_failure_remaining", True,
                    initial=initial_summary,
                    final=PublicEvaluationSummary.from_report(current),
                    cycles=cycles,
                )

        final = PublicEvaluationSummary.from_report(current)
        accepted = any(item.decision == "ACCEPT" for item in cycles)
        return self._outcome(
            started,
            "ACCEPT" if accepted else "STOPPED",
            "cycle_budget_exhausted" if accepted else "no_cycle_accepted",
            accepted,
            initial=initial_summary,
            final=final,
            cycles=cycles,
        )

    def _historical_failed_scenario_ids(
        self,
        dataset_version: str,
        *,
        repo_fingerprint: str | None = None,
    ) -> set[str]:
        """Évite les répétitions uniquement pour le même état de code.

        Un scénario rejeté sur une ancienne version ne doit pas être banni après
        qu'une autre amélioration a changé le repository : il peut alors devenir
        réparable avec une stratégie différente.
        """
        avoided: set[str] = set()
        try:
            records = self.memory.list_experiments()
        except Exception:
            return avoided
        for record in records[-200:]:
            if record.judge_decision.upper() not in {"REJECT", "UNCERTAIN"}:
                continue
            if "autonomous_self_improvement" not in {tag.casefold() for tag in record.tags}:
                continue
            if str(record.metadata.get("dataset_version", "")) != str(dataset_version):
                continue
            if repo_fingerprint is not None:
                recorded_fp = str(record.metadata.get("repo_fingerprint", ""))
                # Compatibilité avec l'historique V1/V2 : une ancienne entrée sans
                # empreinte reste considérée pertinente. Les nouvelles entrées V3,
                # elles, sont précisément liées à l'état du code qui a échoué.
                if recorded_fp and recorded_fp != repo_fingerprint:
                    continue
            for scenario_id in record.metadata.get("scenario_ids", []) or []:
                if isinstance(scenario_id, str):
                    avoided.add(scenario_id)
        return avoided

    @staticmethod
    def _has_unattempted_train_failures(
        report: BenchmarkReport,
        attempted: set[str],
    ) -> bool:
        return any(
            item.split == OPTIMIZATION_SPLIT and item.scenario_id not in attempted
            for item in report.failures
        )

    def _record_cycle_memory(
        self,
        cycle: SelfImprovementCycle,
        *,
        dataset_version: str,
        scenario_ids: list[str],
        repo_fingerprint: str,
    ) -> None:
        """Persiste une leçon compacte sans secret ni donnée de validation détaillée."""
        try:
            decision = cycle.decision.upper()
            normalized = "ACCEPT" if decision == "ACCEPT" else (
                "UNCERTAIN" if decision == "UNCERTAIN" else "REJECT"
            )
            before = asdict(cycle.baseline) if cycle.baseline else {}
            after = asdict(cycle.candidate) if cycle.candidate else before
            # La mémoire exploitable par les agents ne contient QUE l'évidence TRAIN.
            # Les métriques globales/sécurité/garde pourraient indirectement révéler
            # des informations sur le split indépendant et sont donc supprimées.
            for payload in (before, after):
                for key in ("score", "validation_score", "security_score", "failures", "scenario_count"):
                    payload.pop(key, None)
            changed_files: list[str] = []
            if cycle.engineering:
                changed_files = list(cycle.engineering.details.get("changed_paths", []) or [])
            lesson = (
                f"Piste acceptée avec gain TRAIN {cycle.improvement:.2f}."
                if normalized == "ACCEPT"
                else "Piste rejetée par l'évaluation indépendante ; réviser l'hypothèse avant de la retenter."
            )
            record = ExperimentRecord(
                experiment_id=f"auto_{int(time.time() * 1000)}_{cycle.cycle}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                task=cycle.objective[:12_000],
                problem_type="autonomous_self_improvement",
                root_cause="train_failure_cluster",
                strategy="engineering_orchestrator_v3",
                files_changed=changed_files,
                before_metrics=before,
                after_metrics=after,
                judge_decision=normalized,
                judge_score=cycle.improvement,
                judge_confidence=1.0 if normalized == "ACCEPT" else 0.8,
                regressions=[] if normalized == "ACCEPT" else ["independent_guard_rejected_candidate"],
                improvements=[f"train_gain={cycle.improvement:.2f}"] if normalized == "ACCEPT" else [],
                failure_type=None if normalized == "ACCEPT" else "independent_guard_rejection",
                result_summary=(
                    f"candidate accepted; train_gain={cycle.improvement:.2f}"
                    if normalized == "ACCEPT" else "candidate rejected by independent evidence guard"
                ),
                reusable_lesson=lesson,
                tags=["autonomous_self_improvement", "train_only_optimization"],
                metadata={
                    "dataset_version": str(dataset_version),
                    "scenario_ids": list(scenario_ids),
                    "cycle": cycle.cycle,
                    "repo_fingerprint": repo_fingerprint,
                },
            )
            self.memory.record_experiment(record)
        except Exception:
            # La mémoire ne doit jamais transformer une amélioration validée en échec.
            return

    @staticmethod
    def _objective_fingerprint(objective: EngineeringObjective) -> str:
        payload = {
            "goal": objective.goal.strip(),
            "constraints": [str(item).strip() for item in objective.constraints],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _repository_fingerprint(self) -> str:
        """Empreinte stable du code public exploitable par l'agent.

        Les rapports, caches, environnements et surfaces privées sont ignorés afin
        que la mémoire d'expériences soit liée à l'état logiciel réel et non à des
        artefacts d'exécution.
        """
        digest = hashlib.sha256()
        suffixes = {".py", ".toml", ".ini", ".yaml", ".yml", ".json", ".md"}
        entries: list[tuple[str, Path]] = []
        for path in self.repo_root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in suffixes:
                continue
            try:
                rel = path.resolve(strict=False).relative_to(self.repo_root).as_posix()
            except ValueError:
                continue
            if is_model_private_path(rel):
                continue
            entries.append((rel, path))
        for rel, path in sorted(entries):
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        return digest.hexdigest()

    def _evaluate_public(self) -> BenchmarkReport:
        evaluator = self.evaluator
        report = evaluator.evaluate() if hasattr(evaluator, "evaluate") else evaluator()
        if not isinstance(report, BenchmarkReport):
            raise TypeError("L'évaluateur doit retourner BenchmarkReport.")
        _assert_public_report(report)
        return report

    @staticmethod
    def _outcome(
        started: float,
        final_decision: str,
        reason: str,
        success: bool,
        *,
        initial: PublicEvaluationSummary | None = None,
        final: PublicEvaluationSummary | None = None,
        cycles: list[SelfImprovementCycle] | None = None,
        details: dict[str, Any] | None = None,
    ) -> SelfImprovementOutcome:
        return SelfImprovementOutcome(
            final_decision=final_decision,
            reason=reason,
            success=success,
            initial=initial,
            final=final,
            cycles=list(cycles or []),
            duration_seconds=round(time.perf_counter() - started, 2),
            details=dict(details or {}),
        )


def build_public_improvement_objective(
    report: BenchmarkReport,
    *,
    maximum_cases: int = 12,
    exclude_scenario_ids: set[str] | None = None,
) -> EngineeringObjective:
    """Transforme les échecs publics en objectif d'ingénierie sans donnée cachée."""
    _assert_public_report(report)
    if not 1 <= int(maximum_cases) <= 15:
        raise ValueError("maximum_cases doit être compris entre 1 et 15.")
    # Important : ne pas clusteriser le rapport public complet puis filtrer les cas.
    # Les noms/ordres de clusters pourraient sinon être influencés par l'évaluation
    # gardée. On construit d'abord une vue strictement TRAIN et toute la génération
    # d'objectif travaille ensuite uniquement sur cette vue.
    train_report = _train_only_report(report)
    clusters = cluster_failures(train_report, include_holdout=False)
    excluded = set(exclude_scenario_ids or set())
    selected: list[tuple[Any, ScenarioResult]] = []
    for cluster in clusters:
        for case in cluster.cases:
            # V3 : le modèle optimise uniquement TRAIN. VALIDATION reste un garde
            # d'acceptation et n'est jamais exposée dans l'objectif d'ingénierie.
            if case.split != OPTIMIZATION_SPLIT or case.scenario_id in excluded:
                continue
            selected.append((cluster, case))
            if len(selected) >= maximum_cases:
                break
        if len(selected) >= maximum_cases:
            break
    if not selected:
        raise ValueError("Aucun échec train exploitable non tenté.")

    lines = [
        "Améliore de façon mesurable les capacités de l'assistant à partir des échecs TRAIN suivants.",
        f"Score TRAIN actuel : {train_report.score:.2f}/100.",
        "Analyse la cause racine dans le repository réel avant de modifier le code.",
        "Chaque tâche de code doit inclure ses tests déterministes de non-régression.",
        "Préserve strictement les contrôles de chemins, confirmations, protections SSRF et autres invariants de sécurité.",
        "Évite les architectures parallèles inventées : étends les abstractions réellement présentes dans le repository.",
        "",
        "Échecs prioritaires :",
    ]
    for cluster, case in selected:
        issues = "; ".join(item.issue for item in case.criteria if not item.passed) or case.error or "échec sans détail"
        modules = ", ".join(cluster.modules) or "à localiser dans le repository"
        lines.append(
            f"- {case.scenario_id} | catégorie={case.category} | cluster={cluster.name} | "
            f"problème={issues[:350]} | pistes={modules}"
        )
    return EngineeringObjective(
        goal="\n".join(lines),
        constraints=[
            "Utiliser uniquement les cas TRAIN fournis dans cet objectif.",
            "Ne demander aucune donnée d'évaluation gardée ou cachée.",
            "Ne conserver une modification que si les tests et l'évaluation indépendante prouvent une amélioration.",
            "Ne modifie aucun test existant ; crée un nouveau test de non-régression si nécessaire.",
        ],
        metadata={
            "source": "train_benchmark_guarded",
            "scenario_ids": [case.scenario_id for _cluster, case in selected],
            "cluster_names": list(dict.fromkeys(cluster.name for cluster, _case in selected)),
            "baseline_train_score": train_report.score,
            "failure_count": len(selected),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )




def _decide_guarded_self_improvement(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
    *,
    minimum_train_improvement: float,
) -> AcceptanceDecision:
    """Décision V3 : optimiser TRAIN, utiliser le reste uniquement comme veto.

    Le score du split gardé ne contribue jamais positivement au gain demandé : il
    peut seulement empêcher l'acceptation s'il régresse. Cela évite d'orienter
    l'auto-développement vers l'examen de généralisation.
    """
    reasons: list[str] = []
    security_changes = compare_security_results(baseline, candidate)
    if security_changes["true_regressions"] or candidate.security_score < baseline.security_score:
        reasons.append("Régression de sécurité détectée.")

    baseline_guard = _split_score(baseline, "validation")
    candidate_guard = _split_score(candidate, "validation")
    if candidate_guard < baseline_guard:
        reasons.append("Le score validation (garde indépendant) baisse.")

    for metric in ("model_calls", "model_3b_calls", "embedding_calls"):
        before = int(baseline.metrics.get(metric, 0) or 0)
        after = int(candidate.metrics.get(metric, 0) or 0)
        tolerated = max(10, before * 10)
        if after > tolerated:
            reasons.append(f"Coût performance excessif : {metric} passe de {before} à {after}.")

    train_gain = round(
        _split_score(candidate, OPTIMIZATION_SPLIT) - _split_score(baseline, OPTIMIZATION_SPLIT),
        2,
    )
    if train_gain < float(minimum_train_improvement):
        reasons.append(
            f"Gain TRAIN insuffisant ({train_gain:.2f} < {float(minimum_train_improvement):.2f})."
        )
    return AcceptanceDecision(not reasons, reasons, train_gain, security_changes)


def _mentions_guarded_evaluation(text: str) -> bool:
    """Détecte une référence explicite au garde, sans bloquer « validation » métier.

    Le mot « validation » seul peut apparaître dans une tâche parfaitement légitime
    (validation de chemin, validation JSON, etc.). On refuse uniquement des formes
    qui désignent clairement le split/score d'évaluation gardé ou le holdout.
    """
    lowered = str(text or "").casefold()
    patterns = (
        r"\bholdout\b",
        r"\bvalidation[_ -]?score\b",
        r"\b(?:split|jeu|dataset|score|métrique|metrique)[ _-]?validation\b",
        r"\bvalidation[ _-]?(?:split|set|dataset|score|metric|métrique|metrique)\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)

def _train_only_report(report: BenchmarkReport) -> BenchmarkReport:
    """Construit une vue d'optimisation sans aucune donnée du split gardé.

    Cette vue est utilisée uniquement pour choisir *quoi* améliorer. Le rapport
    public complet reste utilisé séparément par le validateur d'évidence pour
    décider si une modification peut être conservée.
    """
    train_results = [item for item in report.results if item.split == OPTIMIZATION_SPLIT]
    if not train_results:
        raise ValueError("Aucun scénario TRAIN disponible.")
    train_failures = [item for item in train_results if not item.passed]
    return BenchmarkReport(
        dataset_version=report.dataset_version,
        timestamp=report.timestamp,
        commit=report.commit,
        splits=[OPTIMIZATION_SPLIT],
        score=round(_split_score(report, OPTIMIZATION_SPLIT), 2),
        category_scores={},
        dimension_scores={},
        security_score=100.0,
        results=train_results,
        duration_seconds=report.duration_seconds,
        metrics={
            "scenario_count": len(train_results),
            "failure_count": len(train_failures),
            "network_calls": 0,
        },
        tests={},
    )

def _public_failures(report: BenchmarkReport) -> list[ScenarioResult]:
    return [item for item in report.failures if item.split in PUBLIC_SPLITS]


def _train_failures(report: BenchmarkReport) -> list[ScenarioResult]:
    return [item for item in report.failures if item.split == OPTIMIZATION_SPLIT]


def _assert_public_report(report: BenchmarkReport) -> None:
    split_names = {str(item).casefold() for item in report.splits}
    if split_names - PUBLIC_SPLITS:
        raise ValueError("hidden_or_unknown_split_present_in_evaluation")
    for result in report.results:
        if result.split.casefold() not in PUBLIC_SPLITS:
            raise ValueError("hidden_or_unknown_result_present_in_evaluation")
    if int(report.metrics.get("network_calls", 0) or 0) != 0:
        raise ValueError("public_evaluation_must_not_use_network")


def _split_score(report: BenchmarkReport, split: str) -> float:
    items = [item for item in report.results if item.split == split]
    denominator = sum(item.weight for item in items)
    if not denominator:
        return 0.0
    return sum(item.score * item.weight for item in items) / denominator


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


def main(argv: list[str] | None = None) -> int:
    """Compatibilité CLI : délègue toujours au superviseur V5 de confiance.

    Les classes historiques de ce module restent utilisables comme briques
    cognitives/testables, mais l'entrée exécutable ne peut plus conserver seule
    une auto-modification sans l'arbitre externe.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--max-minutes", type=float, default=45.0)
    parser.add_argument("--minimum-improvement", type=float, default=0.5)
    parser.add_argument("--target-score", type=float, default=98.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    from self_improvement.trusted_supervisor import (
        TrustedSelfImprovementBudget,
        TrustedSelfImprovementSupervisor,
    )

    supervisor = TrustedSelfImprovementSupervisor(Path.cwd())
    result = supervisor.run(
        budget=TrustedSelfImprovementBudget(
            max_cycles=args.cycles,
            max_minutes=args.max_minutes,
            minimum_improvement=args.minimum_improvement,
            target_score=args.target_score,
        ),
        dry_run=args.dry_run,
    )
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result.success or result.final_decision in {"DRY_RUN", "NO_ACTION", "TARGET_REACHED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
