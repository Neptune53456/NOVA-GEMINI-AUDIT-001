"""Improvement Orchestrator V1 — Boucle bornée d'auto-amélioration transactionnelle.

Orchestre de manière déterministe et sécurisée :
1. Réception et validation d'une requête d'amélioration (ImprovementRequest).
2. Vérification des invariants de sécurité (interdiction formelle du holdout).
3. Snapshot transactionnel baseline des fichiers ciblés.
4. Consultation d'Experiment Memory pour réutiliser les succès et proscrire les échecs.
5. Sélection de stratégie déterministe et formulation de consignes enrichies.
6. Exécution bornée du Developer Agent sous contrôle de budget strict.
7. Collecte des métriques post-modification et arbitrage par le Judge Engine.
8. Décision finale :
   - ACCEPT : conservation des modifications + persistance mémoire positive.
   - REJECT : rollback transactionnel immédiat + persistance mémoire négative.
   - UNCERTAIN : rollback par défaut + arrêt immédiat + persistance mémoire incertaine.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from model_router import ModelCallBudget

from self_improvement.developer_agent import DeveloperAgent, DeveloperResult, DeveloperTask
from self_improvement.candidate_evidence import CandidateEvidence, build_candidate_evidence
from self_improvement.experiment_memory import (
    ExperimentMatch,
    ExperimentMemory,
    ExperimentRecord,
    create_experiment_record,
    sanitize_text,
)
from self_improvement.process_safety import sanitized_child_environment
from self_improvement.public_workspace_resources import select_worker_safe_tests
from self_improvement.engineering_memory import EngineeringMemory
from self_improvement.judge_engine import (
    BenchmarkMetrics,
    ChangeRiskMetrics,
    CoverageMetrics,
    JudgeDecision,
    JudgeEngine,
    JudgeMetrics,
    JudgeResult,
    StaticQualityMetrics,
    TestMetrics,
    judge,
)


HOLDOUT_NAME = ".self_improvement_holdout"


# ===========================================================================
# 1. STRUCTURES DE DONNÉES DE L'ORCHESTRATEUR
# ===========================================================================

@dataclass
class ImprovementRequest:
    """Requête d'amélioration soumise à l'orchestrateur."""
    task: str
    problem_type: str = ""
    root_cause: str = ""
    target_files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImprovementBudget:
    """Budgets et plafonds maximaux alloués pour une campagne d'amélioration."""
    max_attempts: int = 2
    max_files_changed: int = 5
    max_diff_lines: int = 300
    execution_limits: dict | None = None
    max_model_calls: int = 10
    max_duration_seconds: float = 300.0
    reviewer_model_budget: Any = None


@dataclass
class ImprovementAttempt:
    """Enregistrement d'une tentative au sein d'une campagne."""
    attempt_id: str
    attempt_number: int
    strategy: str
    instruction: str
    files_targeted: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    developer_result: DeveloperResult | None = None
    judge_result: JudgeResult | None = None
    candidate_evidence: CandidateEvidence | None = None
    experiment_id: str | None = None
    duration_seconds: float = 0.0
    budget_used: dict[str, Any] = field(default_factory=dict)
    rollback_performed: bool = False
    error: str | None = None


@dataclass
class ImprovementOutcome:
    """Résultat consolidé et auditable de la campagne d'amélioration."""
    campaign_id: str
    success: bool
    final_decision: str  # ACCEPT | REJECT | UNCERTAIN
    attempts: list[ImprovementAttempt] = field(default_factory=list)
    reason: str = ""
    rollback_performed: bool = False
    experiment_ids: list[str] = field(default_factory=list)
    before_metrics: dict[str, Any] = field(default_factory=dict)
    after_metrics: dict[str, Any] = field(default_factory=dict)
    strategy_used: str | None = None
    duration_seconds: float = 0.0
    budget_exceeded: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.attempts:
            data["attempts"] = [
                {
                    **asdict(att),
                    "developer_result": asdict(att.developer_result) if att.developer_result else None,
                    "judge_result": att.judge_result.to_dict() if att.judge_result else None,
                    "candidate_evidence": att.candidate_evidence.to_dict() if att.candidate_evidence else None,
                }
                for att in self.attempts
            ]
        return data


# ===========================================================================
# 2. ORCHESTRATEUR TRANSACTIONNEL D'AMÉLIORATION (IMPROVEMENT ORCHESTRATOR V1)
# ===========================================================================

class ImprovementOrchestrator:
    """Moteur d'orchestration pour campagnes d'auto-amélioration déterministes."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        memory: ExperimentMemory | None = None,
        judge_engine: JudgeEngine | None = None,
        developer_agent: DeveloperAgent | None = None,
        developer_agent_factory: Callable[[], DeveloperAgent] | None = None,
        test_runner: Callable[[list[str]], TestMetrics] | None = None,
        default_budget: ImprovementBudget | None = None,
        engineering_memory: EngineeringMemory | None = None,
        test_timeout_seconds: float = 120.0,
    ):
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.memory = memory or ExperimentMemory()
        self.judge_engine = judge_engine or JudgeEngine()
        self.developer_agent = developer_agent
        self.developer_agent_factory = developer_agent_factory
        self.test_runner = test_runner or self._default_test_runner
        self.default_budget = default_budget or ImprovementBudget()
        self.engineering_memory = engineering_memory or EngineeringMemory(self.repo_root)
        # The REAL Developer already uses a 120 s public-test budget. Keep the
        # orchestrator's before/after measurement on the same bounded scale so
        # a healthy targeted slice is not misclassified as a runner crash.
        self.test_timeout_seconds = max(5.0, min(float(test_timeout_seconds), 900.0))

    # -----------------------------------------------------------------------
    # Sécurité & Protections
    # -----------------------------------------------------------------------
    def _is_holdout_path(self, path_str: str) -> bool:
        """Vérifie si un chemin cible ou une mention implique le holdout."""
        if not path_str:
            return False
        normalized = str(path_str).replace("\\", "/")
        return HOLDOUT_NAME in normalized

    def _resolve_repo_path(self, rel_or_abs: str | Path) -> Path:
        """Résout un chemin de façon sécurisée à l'intérieur du dépôt."""
        p = Path(rel_or_abs)
        resolved = (self.repo_root / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError:
            raise ValueError(f"safety_violation: chemin hors du dépôt : {rel_or_abs}")
        if HOLDOUT_NAME in resolved.parts:
            raise ValueError(f"safety_violation: accès interdit au holdout : {rel_or_abs}")
        return resolved

    # -----------------------------------------------------------------------
    # Transactional Snapshot & Rollback
    # -----------------------------------------------------------------------
    def _create_snapshot(self, target_files: list[str]) -> dict[Path, str | None]:
        """Prend une empreinte exacte en mémoire du contenu des fichiers ciblés."""
        snapshot: dict[Path, str | None] = {}
        for f in target_files:
            try:
                resolved = self._resolve_repo_path(f)
                if resolved.is_file():
                    snapshot[resolved] = resolved.read_text(encoding="utf-8", errors="replace")
                else:
                    snapshot[resolved] = None
            except Exception:
                continue
        return snapshot

    def _restore_snapshot(
        self,
        snapshot: dict[Path, str | None],
        touched_files: list[str] | None = None,
    ) -> tuple[bool, str | None]:
        """Restaure fidèlement l'état initial des fichiers ciblés sans toucher à git."""
        errors: list[str] = []
        all_paths = set(snapshot.keys())

        if touched_files:
            for tf in touched_files:
                try:
                    all_paths.add(self._resolve_repo_path(tf))
                except Exception:
                    pass

        for path in all_paths:
            try:
                original_content = snapshot.get(path)
                if original_content is None:
                    # Le fichier n'existait pas avant la tentative : le supprimer s'il a été créé
                    if path.is_file():
                        path.unlink(missing_ok=True)
                else:
                    # Restaurer le contenu textuel d'origine
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(original_content, encoding="utf-8")
            except Exception as exc:
                errors.append(f"Échec restauration {path.name}: {exc}")

        if errors:
            return False, "; ".join(errors)
        return True, None

    # -----------------------------------------------------------------------
    # Exécution des tests et métriques
    # -----------------------------------------------------------------------
    def _default_test_runner(self, test_files: list[str]) -> TestMetrics:
        """Exécute les tests ciblés via pytest sous forme de sous-processus sécurisé."""
        if not test_files:
            return TestMetrics(passed=0, failed=0, total=0)

        cmd = [sys.executable, "-m", "pytest", *test_files, "--no-cov", "-q"]
        start_time = time.perf_counter()
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=self.test_timeout_seconds,
                env=sanitized_child_environment(),
            )
            duration = round(time.perf_counter() - start_time, 2)
            stdout = res.stdout + "\n" + res.stderr

            # Analyse simplifiée de la sortie pytest
            passed_match = re.search(r"(\d+)\s+passed", stdout)
            failed_match = re.search(r"(\d+)\s+failed", stdout)
            passed = int(passed_match.group(1)) if passed_match else 0
            failed = int(failed_match.group(1)) if failed_match else 0
            total = passed + failed

            failed_names: list[str] = []
            if failed > 0:
                for line in stdout.splitlines():
                    if line.startswith("FAILED "):
                        parts = line.split()
                        if len(parts) >= 2:
                            failed_names.append(parts[1])

            return TestMetrics(
                passed=passed,
                failed=failed,
                total=total,
                duration_seconds=duration,
                failed_test_names=failed_names,
            )
        except Exception as exc:
            return TestMetrics(
                passed=0,
                failed=1,
                total=1,
                failed_test_names=[f"test_runner_crash: {exc}"],
            )

    def _collect_static_quality(self, files: list[str]) -> StaticQualityMetrics:
        """Vérifie la syntaxe Python des fichiers ciblés."""
        syntax_errors: list[str] = []
        for f in files:
            try:
                resolved = self._resolve_repo_path(f)
                if resolved.is_file() and resolved.suffix == ".py":
                    source = resolved.read_text(encoding="utf-8", errors="replace")
                    ast.parse(source, filename=str(resolved))
            except SyntaxError as syn_err:
                syntax_errors.append(f"{Path(f).name}: {syn_err.msg} (line {syn_err.lineno})")
            except Exception as exc:
                syntax_errors.append(f"{Path(f).name}: {exc}")

        return StaticQualityMetrics(
            compilation_ok=len(syntax_errors) == 0,
            syntax_errors=syntax_errors,
            git_diff_clean=True,
        )

    def _calculate_diff_metrics(
        self,
        snapshot: dict[Path, str | None],
        target_files: list[str],
    ) -> tuple[int, int, list[str]]:
        """Calcule les lignes ajoutées, supprimées et fichiers réellement modifiés."""
        added = 0
        removed = 0
        modified_files: list[str] = []

        for f in target_files:
            try:
                resolved = self._resolve_repo_path(f)
                orig_content = snapshot.get(resolved) or ""
                new_content = resolved.read_text(encoding="utf-8", errors="replace") if resolved.is_file() else ""

                if orig_content != new_content:
                    modified_files.append(str(resolved.relative_to(self.repo_root)))
                    diff = list(difflib.unified_diff(
                        orig_content.splitlines(),
                        new_content.splitlines(),
                    ))
                    for line in diff:
                        if line.startswith("+") and not line.startswith("+++"):
                            added += 1
                        elif line.startswith("-") and not line.startswith("---"):
                            removed += 1
            except Exception:
                continue

        return added, removed, modified_files

    def _collect_snapshot_metrics(
        self,
        request: ImprovementRequest,
        snapshot: dict[Path, str | None],
        target_files: list[str],
    ) -> JudgeMetrics:
        """Construit un objet JudgeMetrics complet pour l'état courant."""
        limits = getattr(self, "_execution_limits", None)
        metric_tests = request.tests
        if limits is not None and request.tests:
            limits.phase_timeout(self.test_timeout_seconds, minimum=self.test_timeout_seconds, future_seconds=300.0)
            metric_tests, _ = select_worker_safe_tests(self.repo_root, request.tests)
        test_metrics = self.test_runner(metric_tests) if self.test_runner else TestMetrics()
        static_quality = self._collect_static_quality(target_files)
        added, removed, modified = self._calculate_diff_metrics(snapshot, target_files)

        change_risk = ChangeRiskMetrics(
            files_modified=modified,
            lines_added=added,
            lines_removed=removed,
        )

        return JudgeMetrics(
            tests=test_metrics,
            static_quality=static_quality,
            change_risk=change_risk,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # -----------------------------------------------------------------------
    # Sélection de Stratégie Déterministe V1
    # -----------------------------------------------------------------------
    def _select_strategy(
        self,
        request: ImprovementRequest,
        attempt_idx: int,
        attempted_strategies: set[str],
    ) -> tuple[str, str, list[str]]:
        """Sélectionne une stratégie déterministe en exploitant l'ExperimentMemory.

        Retourne : (strategy_name, enhanced_instruction, reusable_lessons)
        """
        # 1. Rechercher les stratégies ayant réussi dans le passé sur un problème similaire
        successful_matches = self.memory.find_successful_strategies(
            problem_type=request.problem_type,
            task=request.task,
            tags=request.tags,
            limit=5,
        )

        # 2. Rechercher les stratégies ayant échoué à éviter
        failed_matches = self.memory.find_failed_strategies(
            problem_type=request.problem_type,
            task=request.task,
            tags=request.tags,
            limit=5,
        )

        avoid_strategies = {m.strategy for m in failed_matches if m.strategy}
        reusable_lessons: list[str] = [m.reusable_lesson for m in successful_matches if m.reusable_lesson]
        # Mémoire V5 : leçons plus fines de tentatives précédentes. Elle ne décide
        # jamais du résultat, elle enrichit uniquement l'instruction cognitive.
        try:
            contextual_hints = self.engineering_memory.relevant_hints(request.task, limit=3)
        except Exception:
            contextual_hints = []
        for hint in contextual_hints:
            rendered = f"[{hint.outcome}/{hint.similarity:.2f}] {hint.lesson}"
            if rendered not in reusable_lessons:
                reusable_lessons.append(rendered)

        # Ordre de fallback déterministe des stratégies
        default_ladder = [
            "targeted_fix",
            "symbol_edit",
            "anchor_edit",
            "relevance_guided_fix",
            "test_guided_fix",
        ]

        selected_strat: str | None = None

        # Priorité 1 : Stratégie ACCEPT passée non encore tentée dans cette campagne
        for match in successful_matches:
            if match.strategy and match.strategy not in attempted_strategies and match.strategy not in avoid_strategies:
                selected_strat = match.strategy
                break

        # Priorité 2 : Première stratégie par défaut disponible
        if not selected_strat:
            for strat in default_ladder:
                if strat not in attempted_strategies and strat not in avoid_strategies:
                    selected_strat = strat
                    break

        if not selected_strat:
            # Si toutes les stratégies sont épuisées, prendre la première non tentée dans cette campagne
            for strat in default_ladder:
                if strat not in attempted_strategies:
                    selected_strat = strat
                    break

        selected_strat = selected_strat or "targeted_fix"

        # Construction de l'instruction enrichie
        instruction_lines = [request.task]
        if request.root_cause:
            instruction_lines.append(f"Cause racine identifiée: {request.root_cause}")
        if reusable_lessons:
            instruction_lines.append(f"Leçon d'expérience utile: {reusable_lessons[0]}")
        if avoid_strategies:
            instruction_lines.append(f"Stratégies à proscrire: {', '.join(sorted(avoid_strategies))}")

        enhanced_instruction = "\n".join(instruction_lines)
        return selected_strat, enhanced_instruction, reusable_lessons

    def _remember_attempt(
        self,
        request: ImprovementRequest,
        *,
        outcome: str,
        lesson: str,
        failure_type: str = "",
        strategy: str = "",
        files: list[str] | None = None,
        tests: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.engineering_memory.record(
                task=request.task, outcome=outcome, lesson=lesson, failure_type=failure_type,
                strategy=strategy, files=files or request.target_files, tests=tests or request.tests,
                tags=request.tags, metadata=metadata or {},
            )
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Exécution Principale
    # -----------------------------------------------------------------------
    def run(
        self,
        request: ImprovementRequest,
        budget: ImprovementBudget | None = None,
        *,
        model_budget: ModelCallBudget | None = None,
    ) -> ImprovementOutcome:
        """Lance la campagne bornée d'auto-amélioration."""
        b = budget or self.default_budget
        self._execution_limits = None
        if b.execution_limits is not None:
            from self_improvement.execution_limits import ExecutionLimits
            self._execution_limits = ExecutionLimits.from_dict(b.execution_limits)
            if (b.max_files_changed, b.max_diff_lines) != (self._execution_limits.max_source_files, self._execution_limits.max_diff_lines):
                raise ValueError("unsupported_execution_limits: improvement caps differ")
        external_model_budget = model_budget is not None
        shared_model_budget = model_budget or ModelCallBudget(b.max_model_calls)
        initial_model_calls = shared_model_budget.used_calls
        campaign_id = f"cmp_{int(time.time() * 1000)}"
        campaign_start_time = time.perf_counter()

        # -------------------------------------------------------------------
        # 1. CONTRÔLE DE SÉCURITÉ HOLDOUT
        # -------------------------------------------------------------------
        if self._is_holdout_path(request.task) or any(self._is_holdout_path(f) for f in request.target_files):
            return ImprovementOutcome(
                campaign_id=campaign_id,
                success=False,
                final_decision="REJECT",
                reason="safety_rejection: mention ou chemin holdout strictement interdit.",
                rollback_performed=False,
            )

        # Cibles effectives
        target_files = list(request.target_files)
        if not target_files:
            return ImprovementOutcome(
                campaign_id=campaign_id,
                success=False,
                final_decision="UNCERTAIN",
                reason="missing_targets: aucun fichier cible spécifié.",
                rollback_performed=False,
            )

        # -------------------------------------------------------------------
        # 2. SNAPSHOT BASELINE TRANSACTIONNEL & MÉTRIQUES INITIALES
        # -------------------------------------------------------------------
        baseline_snapshot = self._create_snapshot(target_files)
        before_metrics = self._collect_snapshot_metrics(request, baseline_snapshot, target_files)

        attempts_record: list[ImprovementAttempt] = []
        experiment_ids: list[str] = []
        attempted_strategies: set[str] = set()

        total_model_calls = 0
        overall_rollback_performed = False

        # -------------------------------------------------------------------
        # 3. BOUCLE BORNÉE DES TENTATIVES
        # -------------------------------------------------------------------
        for attempt_num in range(1, b.max_attempts + 1):
            attempt_id = f"att_{int(time.time() * 1000)}_{attempt_num}"
            attempt_start_time = time.perf_counter()

            # Vérification du budget temps
            elapsed_campaign = time.perf_counter() - campaign_start_time
            if elapsed_campaign >= b.max_duration_seconds:
                self._restore_snapshot(baseline_snapshot, target_files)
                return ImprovementOutcome(
                    campaign_id=campaign_id,
                    success=False,
                    final_decision="REJECT",
                    attempts=attempts_record,
                    reason=f"budget_exceeded: durée maximale dépassée ({elapsed_campaign:.1f}s >= {b.max_duration_seconds}s).",
                    rollback_performed=True,
                    experiment_ids=experiment_ids,
                    before_metrics=before_metrics.to_dict(),
                    budget_exceeded=True,
                    duration_seconds=round(elapsed_campaign, 2),
                )

            # Sélection de stratégie
            strategy_name, instruction, lessons = self._select_strategy(
                request, attempt_num, attempted_strategies
            )
            attempted_strategies.add(strategy_name)

            dev_result: DeveloperResult | None = None
            judge_res: JudgeResult | None = None
            candidate_evidence: CandidateEvidence | None = None
            attempt_error: str | None = None
            attempt_rollback = False

            # Snapshot pré-tentative
            pre_attempt_snapshot = self._create_snapshot(target_files)

            try:
                # -----------------------------------------------------------
                # A. Exécution du DeveloperAgent
                # -----------------------------------------------------------
                dev_agent = (
                    self.developer_agent_factory()
                    if self.developer_agent_factory
                    else (self.developer_agent or DeveloperAgent(repo_root=self.repo_root))
                )

                dev_task = DeveloperTask(
                    task=instruction,
                    relevance_task=request.task,
                    constraints=[
                        str(request.metadata.get("required_behavior", "")),
                        *(f"Target symbol: {item}" for item in [request.metadata.get("grounded_symbol")] if item),
                    ],
                    target_files=target_files,
                    tests=request.tests,
                    max_iterations=2,
                    require_regression_proof=(request.problem_type or "").casefold() in {"fix", "bug"},
                    docs_domains=[
                        str(item) for item in (request.metadata.get("docs_domains", []) if isinstance(request.metadata, dict) else [])
                        if isinstance(item, str)
                    ][:8],
                    max_source_files=b.max_files_changed,
                    max_diff_lines=b.max_diff_lines,
                    execution_limits=b.execution_limits,
                    model_budget=shared_model_budget,
                    reviewer_model_budget=b.reviewer_model_budget,
                    candidate_first=bool(request.metadata.get("candidate_first")),
                    deterministic_handoff=bool(
                        target_files and request.tests and (request.task or "").strip()
                    ),
                    grounded_symbol=str(request.metadata.get("grounded_symbol", "")),
                    expected_behavior=str(request.metadata.get("required_behavior", "")),
                    observed_behavior=str(request.metadata.get("observed_behavior", "")),
                    prior_failures=[hint.lesson for hint in self.engineering_memory.relevant_hints(request.task, limit=3) if hint.outcome != "ACCEPT"],
                )

                dev_result = dev_agent.run(dev_task)
                total_model_calls = shared_model_budget.used_calls - initial_model_calls
                if any(marker in (dev_result.failure_reason or "") for marker in (
                    "candidate_source_file_limit_exceeded", "candidate_diff_limit_exceeded")):
                    raise ValueError(f"budget_exceeded: {dev_result.failure_reason}")

                reported_model_calls = int(dev_result.model_attempts or 0)
                if not external_model_budget and reported_model_calls > b.max_model_calls:
                    raise ValueError(
                        "budget_exceeded: nombre d'appels modele rapporte depasse "
                        f"le plafond ({reported_model_calls} > {b.max_model_calls})."
                    )

                # Le Developer Agent V3 peut conclure, après exploration read-only,
                # que les cibles du Planner sont incorrectes. Ce n'est pas un patch
                # à juger : on rollback la tâche et on remonte UNCERTAIN pour que
                # l'EngineeringOrchestrator puisse replanifier avec les preuves.
                if dev_result.replan_requested or str(dev_result.failure_reason or "").startswith("TEST_INFRA_FAILURE"):
                    rb_ok, rb_err = self._restore_snapshot(baseline_snapshot, target_files)
                    overall_rollback_performed = True
                    reason = dev_result.failure_reason or "developer_requested_replan"
                    if not rb_ok:
                        reason = f"{reason} | rollback_failure: {rb_err}"
                    uncertain_judge = JudgeResult(
                        decision=JudgeDecision.UNCERTAIN,
                        score=0.0,
                        confidence=0.95,
                        reasons=[reason],
                        regressions=[],
                        improvements=[],
                        before_metrics=before_metrics,
                        after_metrics=before_metrics,
                    )
                    exp_record = create_experiment_record(
                        task=request.task,
                        problem_type=request.problem_type,
                        root_cause=request.root_cause,
                        strategy="repo_exploration_replan",
                        judge_result=uncertain_judge,
                        files_changed=[],
                        failure_type="planner_target_mismatch",
                        reusable_lesson=(
                            "Replanifier avec les fichiers recommandés par l'exploration read-only : "
                            + ", ".join(dev_result.recommended_files[:8])
                        ),
                        tags=[*request.tags, "replan_requested"],
                        metadata={
                            "recommended_files": dev_result.recommended_files[:12],
                            "recommended_tests": dev_result.recommended_tests[:12],
                            "tool_calls": dev_result.tool_calls,
                            "strategy_escalation": dev_result.pipeline_trace.get("strategy_escalation"),
                        },
                    )
                    escalation = dev_result.pipeline_trace.get("strategy_escalation") or {}
                    self._remember_attempt(
                        request, outcome="UNCERTAIN",
                        lesson=(
                            f"Invalidated local strategy for {escalation.get('invalidated_target', target_files[0])}: "
                            f"{escalation.get('reason', reason)}"
                        ),
                        failure_type=str(escalation.get("failure_category") or "planner_target_mismatch"),
                        strategy="replan_causal_path", files=target_files,
                        metadata={"escalation_level": escalation.get("level", "REPLAN_TARGET")},
                    )
                    saved_exp = self.memory.record_experiment(exp_record)
                    experiment_ids.append(saved_exp.experiment_id)
                    attempts_record.append(ImprovementAttempt(
                        attempt_id=attempt_id,
                        attempt_number=attempt_num,
                        strategy="repo_exploration_replan",
                        instruction=instruction,
                        files_targeted=target_files,
                        files_modified=[],
                        developer_result=dev_result,
                        judge_result=uncertain_judge,
                        experiment_id=saved_exp.experiment_id,
                        duration_seconds=round(time.perf_counter() - attempt_start_time, 2),
                        rollback_performed=True,
                    ))
                    return ImprovementOutcome(
                        campaign_id=campaign_id,
                        success=False,
                        final_decision="UNCERTAIN",
                        attempts=attempts_record,
                        reason=reason,
                        rollback_performed=True,
                        experiment_ids=experiment_ids,
                        before_metrics=before_metrics.to_dict(),
                        after_metrics=before_metrics.to_dict(),
                        strategy_used="repo_exploration_replan",
                        duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                    )

                # Vérification budget model calls
                if total_model_calls > b.max_model_calls:
                    raise ValueError(f"budget_exceeded: nombre d'appels modèle dépassé ({total_model_calls} > {b.max_model_calls}).")

                # Vérification budget fichiers modifiés
                files_written = dev_result.files_written or dev_result.files_changed
                from self_improvement.execution_limits import is_test_path
                counted_files = (sum(not is_test_path(path) for path in set(files_written))
                                 if b.execution_limits is not None else len(files_written))
                if counted_files > b.max_files_changed:
                    raise ValueError(f"budget_exceeded: trop de fichiers modifiés ({counted_files} > {b.max_files_changed}).")

                # Vérification budget taille de diff
                added_lines, removed_lines, modified_files = self._calculate_diff_metrics(baseline_snapshot, target_files)
                total_diff_lines = added_lines + removed_lines
                if total_diff_lines > b.max_diff_lines:
                    raise ValueError(f"budget_exceeded: diff trop volumineux ({total_diff_lines} > {b.max_diff_lines} lignes).")

                # -----------------------------------------------------------
                # B. Collecte des métriques post-modification & Jugement
                # -----------------------------------------------------------
                after_metrics = self._collect_snapshot_metrics(request, baseline_snapshot, target_files)

                # Évaluation par le Judge Engine
                judge_res = self.judge_engine.judge(before_metrics, after_metrics)
                evidence_changes = []
                for raw_path, original in baseline_snapshot.items():
                    current = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.is_file() else ""
                    evidence_changes.append((str(raw_path.relative_to(self.repo_root)), original or "", current))
                candidate_evidence = build_candidate_evidence(
                    before_metrics, after_metrics, judge_res, changes=evidence_changes,
                    repair_attempted=dev_result.repair_attempted,
                    repair_count=dev_result.repair_count,
                    failure_before_repair=dev_result.failure_before_repair,
                    result_after_repair=dev_result.result_after_repair,
                )
                judge_res.details["candidate_evidence"] = candidate_evidence.to_dict()

                # -----------------------------------------------------------
                # C. Arbitrage & Gestion Transactionnelle
                # -----------------------------------------------------------
                if judge_res.decision == JudgeDecision.ACCEPT:
                    # SUCCÈS : Conservation des modifications et enregistrement
                    exp_record = create_experiment_record(
                        task=request.task,
                        problem_type=request.problem_type,
                        root_cause=request.root_cause,
                        strategy=strategy_name,
                        judge_result=judge_res,
                        files_changed=modified_files,
                        reusable_lesson=lessons[0] if lessons else f"Stratégie {strategy_name} validée",
                        tags=request.tags,
                        metadata={"candidate_evidence": candidate_evidence.to_dict()},
                    )
                    saved_exp = self.memory.record_experiment(exp_record)
                    experiment_ids.append(saved_exp.experiment_id)
                    self._remember_attempt(
                        request, outcome="ACCEPT",
                        lesson=(lessons[0] if lessons else f"La stratégie {strategy_name} a été validée par tests + Judge."),
                        strategy=strategy_name, files=modified_files, tests=request.tests,
                        metadata={"review_decision": getattr(dev_result, "review_decision", None),
                                  "confidence": getattr(dev_result, "confidence_score", None)},
                    )

                    attempt_rec = ImprovementAttempt(
                        attempt_id=attempt_id,
                        attempt_number=attempt_num,
                        strategy=strategy_name,
                        instruction=instruction,
                        files_targeted=target_files,
                        files_modified=modified_files,
                        developer_result=dev_result,
                        judge_result=judge_res,
                        candidate_evidence=candidate_evidence,
                        experiment_id=saved_exp.experiment_id,
                        duration_seconds=round(time.perf_counter() - attempt_start_time, 2),
                        rollback_performed=False,
                    )
                    attempts_record.append(attempt_rec)

                    return ImprovementOutcome(
                        campaign_id=campaign_id,
                        success=True,
                        final_decision="ACCEPT",
                        attempts=attempts_record,
                        reason="Amélioration validée par le Judge Engine sans régression.",
                        rollback_performed=False,
                        experiment_ids=experiment_ids,
                        before_metrics=before_metrics.to_dict(),
                        after_metrics=after_metrics.to_dict(),
                        strategy_used=strategy_name,
                        duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                    )

                elif judge_res.decision == JudgeDecision.REJECT:
                    # REJET : Rollback transactionnel vers le snapshot baseline
                    rb_ok, rb_err = self._restore_snapshot(baseline_snapshot, target_files)
                    attempt_rollback = True
                    overall_rollback_performed = True
                    if not rb_ok:
                        attempt_error = f"rollback_failure: {rb_err}"

                    exp_record = create_experiment_record(
                        task=request.task,
                        problem_type=request.problem_type,
                        root_cause=request.root_cause,
                        strategy=strategy_name,
                        judge_result=judge_res,
                        files_changed=modified_files,
                        failure_type=judge_res.regressions[0] if judge_res.regressions else "rejected_by_judge",
                        reusable_lesson=f"Éviter {strategy_name} pour ce type de problème",
                        tags=request.tags,
                        metadata={"candidate_evidence": candidate_evidence.to_dict()},
                    )
                    saved_exp = self.memory.record_experiment(exp_record)
                    experiment_ids.append(saved_exp.experiment_id)
                    self._remember_attempt(
                        request, outcome="REJECT",
                        lesson=f"Éviter ou modifier la stratégie {strategy_name}; le Judge a rejeté le candidat.",
                        failure_type=(judge_res.regressions[0] if judge_res.regressions else "rejected_by_judge"),
                        strategy=strategy_name, files=modified_files, tests=request.tests,
                    )

                else:  # UNCERTAIN
                    # INCERTAIN : Rollback par défaut et arrêt immédiat de la campagne
                    rb_ok, rb_err = self._restore_snapshot(baseline_snapshot, target_files)
                    attempt_rollback = True
                    overall_rollback_performed = True

                    exp_record = create_experiment_record(
                        task=request.task,
                        problem_type=request.problem_type,
                        root_cause=request.root_cause,
                        strategy=strategy_name,
                        judge_result=judge_res,
                        files_changed=modified_files,
                        reusable_lesson="Résultat incertain : vérifier les métriques de test",
                        tags=request.tags,
                        metadata={"candidate_evidence": candidate_evidence.to_dict()},
                    )
                    saved_exp = self.memory.record_experiment(exp_record)
                    experiment_ids.append(saved_exp.experiment_id)
                    self._remember_attempt(
                        request, outcome="UNCERTAIN",
                        lesson="Le résultat n'était pas suffisamment démontré; renforcer tests/mesures avant de retenter.",
                        failure_type="uncertain_evidence", strategy=strategy_name,
                        files=modified_files, tests=request.tests,
                    )

                    attempt_rec = ImprovementAttempt(
                        attempt_id=attempt_id,
                        attempt_number=attempt_num,
                        strategy=strategy_name,
                        instruction=instruction,
                        files_targeted=target_files,
                        files_modified=modified_files,
                        developer_result=dev_result,
                        judge_result=judge_res,
                        candidate_evidence=candidate_evidence,
                        experiment_id=saved_exp.experiment_id,
                        duration_seconds=round(time.perf_counter() - attempt_start_time, 2),
                        rollback_performed=True,
                    )
                    attempts_record.append(attempt_rec)

                    return ImprovementOutcome(
                        campaign_id=campaign_id,
                        success=False,
                        final_decision="UNCERTAIN",
                        attempts=attempts_record,
                        reason="Décision incertaine rendue par le Judge Engine (changement annulé).",
                        rollback_performed=True,
                        experiment_ids=experiment_ids,
                        before_metrics=before_metrics.to_dict(),
                        after_metrics=after_metrics.to_dict(),
                        strategy_used=strategy_name,
                        duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                    )

            except Exception as exc:
                # Erreur durant l'exécution (Dev agent, test runner crash, budget, etc.)
                attempt_error = str(exc)
                rb_ok, rb_err = self._restore_snapshot(baseline_snapshot, target_files)
                attempt_rollback = True
                overall_rollback_performed = True

                if not rb_ok:
                    attempt_error = f"{attempt_error} | rollback_failure: {rb_err}"

                # Enregistrement de l'échec d'exécution dans la mémoire
                try:
                    fallback_judge_res = JudgeResult(
                        decision=JudgeDecision.REJECT,
                        score=-100.0,
                        confidence=0.9,
                        reasons=[f"Exception lors de la tentative : {attempt_error}"],
                        regressions=[f"execution_error: {attempt_error}"],
                        improvements=[],
                        before_metrics=before_metrics,
                        after_metrics=before_metrics,
                    )
                    exp_record = create_experiment_record(
                        task=request.task,
                        problem_type=request.problem_type,
                        root_cause=request.root_cause,
                        strategy=strategy_name,
                        judge_result=fallback_judge_res,
                        failure_type="execution_exception",
                        tags=request.tags,
                    )
                    saved_exp = self.memory.record_experiment(exp_record)
                    experiment_ids.append(saved_exp.experiment_id)
                except Exception:
                    pass
                self._remember_attempt(
                    request, outcome="REJECT",
                    lesson=f"Tentative d'exécution échouée: {str(attempt_error)[:700]}",
                    failure_type="execution_exception", strategy=strategy_name, tests=request.tests,
                )

                # Arrêt immédiat si le budget a été dépassé
                if "budget_exceeded" in str(attempt_error):
                    attempt_rec = ImprovementAttempt(
                        attempt_id=attempt_id,
                        attempt_number=attempt_num,
                        strategy=strategy_name,
                        instruction=instruction,
                        files_targeted=target_files,
                        developer_result=dev_result,
                        judge_result=judge_res,
                        duration_seconds=round(time.perf_counter() - attempt_start_time, 2),
                        rollback_performed=True,
                        error=attempt_error,
                    )
                    attempts_record.append(attempt_rec)

                    return ImprovementOutcome(
                        campaign_id=campaign_id,
                        success=False,
                        final_decision="REJECT",
                        attempts=attempts_record,
                        reason=f"Campagne stoppée : {attempt_error}",
                        rollback_performed=True,
                        experiment_ids=experiment_ids,
                        before_metrics=before_metrics.to_dict(),
                        budget_exceeded=True,
                        duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                    )

            attempt_rec = ImprovementAttempt(
                attempt_id=attempt_id,
                attempt_number=attempt_num,
                strategy=strategy_name,
                instruction=instruction,
                files_targeted=target_files,
                developer_result=dev_result,
                judge_result=judge_res,
                candidate_evidence=candidate_evidence,
                experiment_id=experiment_ids[-1] if experiment_ids else None,
                duration_seconds=round(time.perf_counter() - attempt_start_time, 2),
                rollback_performed=attempt_rollback,
                error=attempt_error,
            )
            attempts_record.append(attempt_rec)

        # -------------------------------------------------------------------
        # 4. ÉCHEC FINAL APRÈS ÉPUISEMENT DES TENTATIVES
        # -------------------------------------------------------------------
        self._restore_snapshot(baseline_snapshot, target_files)
        return ImprovementOutcome(
            campaign_id=campaign_id,
            success=False,
            final_decision="REJECT",
            attempts=attempts_record,
            reason=f"Campagne terminée sans amélioration validée après {b.max_attempts} tentative(s).",
            rollback_performed=True,
            experiment_ids=experiment_ids,
            before_metrics=before_metrics.to_dict(),
            duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
        )
