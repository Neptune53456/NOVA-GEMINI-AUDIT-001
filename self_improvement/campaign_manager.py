"""Autonomous Campaign Manager V1 — Boucle multi-cycles autonome et bornée.

Orchestre une campagne d'auto-amélioration complète sur un backlog de tâches :
1. Validation de sécurité et détection d'invariants (rejet strict du holdout).
2. Résolution et validation des dépendances (détection de cycles, dépendances inconnues/auto-dépendances).
3. Priorisation déterministe et explicable basée sur l'impact, le risque, le coût et l'historique ExperimentMemory.
4. Exécution séquentielle de chaque tâche via l'ImprovementOrchestrator.
5. Gestion des statuts (PENDING, RUNNING, ACCEPTED, REJECTED, UNCERTAIN, BLOCKED, SKIPPED).
6. Déblocage automatique des dépendances en cascade après chaque ACCEPT.
7. Re-ranking dynamique du backlog après chaque tentative.
8. Arrêt automatique contrôlé selon les plafonds de budget et conditions d'arrêt strictes.
9. Journalisation structurée des événements et rapport d'audit consolidé (CampaignOutcome).
"""

from __future__ import annotations

import json
import inspect
import os
from pathlib import Path
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Sequence

from model_router import ModelCallBudget, RoleBudgetState

from self_improvement.experiment_memory import ExperimentMemory
from self_improvement.improvement_orchestrator import (
    HOLDOUT_NAME,
    ImprovementBudget,
    ImprovementOrchestrator,
    ImprovementOutcome,
    ImprovementRequest,
)


# ===========================================================================
# 1. ÉNUMÉRATIONS ET STRUCTURES DE DONNÉES
# ===========================================================================

class TaskStatus(str, Enum):
    """Statut du cycle de vie d'une tâche dans la campagne."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class CampaignEventType(str, Enum):
    """Types d'événements observés et journalisés durant la campagne."""
    CAMPAIGN_STARTED = "CAMPAIGN_STARTED"
    TASK_SELECTED = "TASK_SELECTED"
    TASK_STARTED = "TASK_STARTED"
    TASK_ACCEPTED = "TASK_ACCEPTED"
    TASK_REJECTED = "TASK_REJECTED"
    TASK_UNCERTAIN = "TASK_UNCERTAIN"
    TASK_BLOCKED = "TASK_BLOCKED"
    BUDGET_WARNING = "BUDGET_WARNING"
    CAMPAIGN_STOPPED = "CAMPAIGN_STOPPED"


class StopReason(str, Enum):
    """Motif d'interruption formel de la campagne autonome."""
    BACKLOG_COMPLETED = "BACKLOG_COMPLETED"
    ALL_TASKS_BLOCKED = "ALL_TASKS_BLOCKED"
    MAX_TASKS_REACHED = "MAX_TASKS_REACHED"
    MAX_ACCEPTED_REACHED = "MAX_ACCEPTED_REACHED"
    MAX_REJECTED_REACHED = "MAX_REJECTED_REACHED"
    MAX_UNCERTAIN_REACHED = "MAX_UNCERTAIN_REACHED"
    MAX_CONSECUTIVE_FAILURES = "MAX_CONSECUTIVE_FAILURES"
    MAX_DURATION_EXCEEDED = "MAX_DURATION_EXCEEDED"
    MAX_MODEL_CALLS_EXCEEDED = "MAX_MODEL_CALLS_EXCEEDED"
    MODEL_BUDGET_EXHAUSTED = "MODEL_BUDGET_EXHAUSTED"
    MAX_TOTAL_DIFF_EXCEEDED = "MAX_TOTAL_DIFF_EXCEEDED"
    SAFETY_VIOLATION = "SAFETY_VIOLATION"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    NO_VIABLE_TASKS = "NO_VIABLE_TASKS"
    ZERO_PROGRESS = "ZERO_PROGRESS"
    CANDIDATE_READY = "CANDIDATE_READY"
    INSUFFICIENT_CAMPAIGN_TIME = "INSUFFICIENT_CAMPAIGN_TIME"


@dataclass
class CampaignTask:
    """Description unitaire d'un problème ou d'un objectif dans le backlog."""
    task_id: str
    title: str
    task: str
    problem_type: str = ""
    root_cause: str = ""
    target_files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    priority: int = 50  # 1 à 100
    estimated_impact: float = 5.0  # 1.0 à 10.0
    estimated_risk: float = 3.0  # 1.0 à 10.0
    estimated_cost: float = 2.0  # 1.0 à 10.0
    dependencies: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    runs_count: int = 0
    priority_score: float = 0.0
    priority_explanation: list[str] = field(default_factory=list)
    last_outcome: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value if isinstance(self.status, TaskStatus) else str(self.status)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CampaignTask:
        d = dict(data)
        if "status" in d and isinstance(d["status"], str):
            try:
                d["status"] = TaskStatus(d["status"])
            except ValueError:
                d["status"] = TaskStatus.PENDING
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class CampaignBudget:
    """Budgets et plafonds maximaux alloués pour l'ensemble d'une campagne."""
    max_tasks: int = 10
    max_accepted_changes: int = 10
    max_rejected_tasks: int = 5
    max_uncertain_tasks: int = 3
    max_consecutive_failures: int = 3
    max_model_calls: int = 50
    max_total_attempts: int = 20
    max_duration_seconds: float = 600.0
    max_total_diff_lines: int = 1000
    max_source_files: int = 5
    execution_limits: dict | None = None
    max_runs_per_task: int = 2
    max_tokens: int = 500_000
    max_cost: float | None = None


def required_task_seconds(
    task: CampaignTask, *, test_window_seconds: float = 120.0,
    developer_test_window_seconds: float = 120.0,
) -> float:
    """Minimum for one productive attempt, not a guarantee for every retry.

    ImprovementOrchestrator measures BEFORE and AFTER; DeveloperAgent runs
    public tests between them. Reserve 60 s for exploration, generation and
    orchestration. Further attempts/repair consume the same bounded envelope.
    Tasks without tests retain the historical 120 s allowance.
    """
    test_seconds = 2 * test_window_seconds + developer_test_window_seconds if task.tests else 0.0
    return max(120.0, test_seconds + 60.0)


def required_campaign_seconds(task_seconds: list[float], budget: CampaignBudget) -> float:
    """Reserve task windows plus 60 s for ranking, snapshots and bookkeeping.

    Only tasks permitted by task/attempt caps count. Use the largest windows
    because priority ordering is recomputed at runtime. Hard caps still win
    when the full plan cannot fit.
    """
    count = max(0, min(len(task_seconds), budget.max_tasks, budget.max_total_attempts))
    return sum(sorted(task_seconds, reverse=True)[:count]) + (60.0 if count else 0.0)


@dataclass
class CampaignEvent:
    """Événement structuré et horodaté du journal de campagne."""
    event_id: str
    timestamp: str  # ISO 8601 UTC
    event_type: str
    task_id: str | None = None
    priority_score: float | None = None
    result: str | None = None
    budget_usage: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CampaignOutcome:
    """Rapport consolidé et auditable de fin de campagne."""
    campaign_id: str
    success: bool
    stop_reason: str
    tasks_total: int
    tasks_run: int
    tasks_accepted: int
    tasks_rejected: int
    tasks_uncertain: int
    tasks_blocked: int
    accepted_task_ids: list[str] = field(default_factory=list)
    rejected_task_ids: list[str] = field(default_factory=list)
    uncertain_task_ids: list[str] = field(default_factory=list)
    remaining_tasks: list[dict[str, Any]] = field(default_factory=list)
    budget_usage: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ===========================================================================
# 2. VALIDATION DES DÉPENDANCES ET DÉTECTION DE CYCLES
# ===========================================================================

def validate_and_resolve_dependencies(tasks: list[CampaignTask]) -> None:
    """Valide l'arbre de dépendances, détecte les anomalies et met à jour les statuts."""
    task_map = {t.task_id: t for t in tasks}

    # 1. Vérification des dépendances inconnues et auto-dépendances
    for t in tasks:
        for dep in t.dependencies:
            if dep == t.task_id:
                t.status = TaskStatus.BLOCKED
                t.metadata["dependency_error"] = f"self_dependency: {t.task_id} dépend de lui-même."
            elif dep not in task_map:
                t.status = TaskStatus.BLOCKED
                t.metadata["dependency_error"] = f"unknown_dependency: {dep} non présent dans le backlog."

    # 2. Détection des cycles via parcours en profondeur (DFS)
    visited: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=visited

    def dfs(t_id: str, path: list[str]) -> bool:
        visited[t_id] = 1
        curr_task = task_map.get(t_id)
        if curr_task and "dependency_error" not in curr_task.metadata:
            for dep in curr_task.dependencies:
                if dep == t_id:
                    continue  # Déjà traité comme self_dependency
                if dep in task_map:
                    if visited.get(dep, 0) == 1:
                        # Cycle détecté
                        cycle_str = " -> ".join(path + [dep])
                        curr_task.status = TaskStatus.BLOCKED
                        curr_task.metadata["dependency_error"] = f"cyclic_dependency: {cycle_str}"
                        return True
                    elif visited.get(dep, 0) == 0:
                        if dfs(dep, path + [dep]):
                            return True
        visited[t_id] = 2
        return False

    for t in tasks:
        if visited.get(t.task_id, 0) == 0:
            dfs(t.task_id, [t.task_id])

    # 3. Mise à jour des statuts BLOCKED / PENDING selon l'état des parents
    for t in tasks:
        if t.status in (TaskStatus.ACCEPTED, TaskStatus.REJECTED, TaskStatus.UNCERTAIN, TaskStatus.SKIPPED):
            continue

        if "dependency_error" in t.metadata:
            t.status = TaskStatus.BLOCKED
            continue

        if not t.dependencies:
            if t.status == TaskStatus.BLOCKED:
                t.status = TaskStatus.PENDING
            continue

        # Si toutes les dépendances sont ACCEPTED -> PENDING, sinon BLOCKED
        all_satisfied = all(
            task_map.get(dep) and task_map[dep].status == TaskStatus.ACCEPTED
            for dep in t.dependencies
        )

        if all_satisfied:
            if t.status == TaskStatus.BLOCKED:
                t.status = TaskStatus.PENDING
        else:
            t.status = TaskStatus.BLOCKED


# ===========================================================================
# 3. CALCUL DU SCORE DE PRIORISATION DÉTERMINISTE
# ===========================================================================

def compute_priority_score(
    task: CampaignTask,
    memory: ExperimentMemory | None = None,
) -> tuple[float, list[str]]:
    """Calcule un score de priorité explicable pour ordonnancer le backlog."""
    explanation: list[str] = []

    # 1. Base et Impact (+10 par point d'impact estimé)
    impact_score = round(task.estimated_impact * 10.0, 2)
    priority_base = round(task.priority * 0.20, 2)
    explanation.append(f"base_priority({priority_base})")
    explanation.append(f"impact({impact_score})")

    # 2. Pénalités de Risque et de Coût
    risk_penalty = round(task.estimated_risk * 5.0, 2)
    cost_penalty = round(task.estimated_cost * 3.0, 2)
    explanation.append(f"risk_penalty(-{risk_penalty})")
    explanation.append(f"cost_penalty(-{cost_penalty})")

    # 3. Pénalité pour échecs répétés dans cette campagne (-25.0 par run précédent)
    runs_penalty = round(task.runs_count * 25.0, 2)
    if runs_penalty > 0:
        explanation.append(f"repeated_runs_penalty(-{runs_penalty})")

    # 4. Signal d'expérience historique depuis ExperimentMemory
    memory_bonus = 0.0
    if memory:
        # Recherche de succès passés
        successful = memory.find_successful_strategies(
            problem_type=task.problem_type,
            task=task.task,
            tags=task.tags,
            limit=3,
        )
        if successful:
            memory_bonus += 15.0
            explanation.append(f"memory_success_bonus(+15.0)")

        # Recherche d'échecs passés pour appliquer un malus modéré
        failed = memory.find_failed_strategies(
            problem_type=task.problem_type,
            task=task.task,
            tags=task.tags,
            limit=3,
        )
        if failed and not successful:
            memory_bonus -= 10.0
            explanation.append(f"memory_failure_penalty(-10.0)")

    total_score = round(priority_base + impact_score - risk_penalty - cost_penalty - runs_penalty + memory_bonus, 2)
    return total_score, explanation


# ===========================================================================
# 4. GESTIONNAIRE DE CAMPAGNE AUTONOME (AUTONOMOUS CAMPAIGN MANAGER V1)
# ===========================================================================

class AutonomousCampaignManager:
    """Moteur d'exécution multi-cycles borné pour campagnes d'auto-amélioration."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        orchestrator: ImprovementOrchestrator | None = None,
        memory: ExperimentMemory | None = None,
        default_budget: CampaignBudget | None = None,
    ):
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self.memory = memory or ExperimentMemory()
        self.orchestrator = orchestrator or ImprovementOrchestrator(
            repo_root=self.repo_root,
            memory=self.memory,
        )
        self.default_budget = default_budget or CampaignBudget()

    def required_task_seconds(self, task: CampaignTask) -> float:
        return required_task_seconds(
            task,
            test_window_seconds=getattr(self.orchestrator, "test_timeout_seconds", 120.0),
            developer_test_window_seconds=getattr(
                getattr(self.orchestrator, "developer_agent", None), "test_timeout_seconds", 120.0,
            ),
        )

    def _is_holdout_violation(self, task: CampaignTask) -> bool:
        """Vérifie si une tâche tente d'accéder au holdout."""
        if HOLDOUT_NAME in task.task:
            return True
        for f in task.target_files:
            if HOLDOUT_NAME in str(f).replace("\\", "/"):
                return True
        return False

    def _rerank_backlog(self, tasks: list[CampaignTask]) -> list[CampaignTask]:
        """Recalcule les dépendances et trie les tâches PENDING par score de priorité décroissant."""
        validate_and_resolve_dependencies(tasks)

        for t in tasks:
            score, explanation = compute_priority_score(t, self.memory)
            t.priority_score = score
            t.priority_explanation = explanation

        # Tri : PENDING d'abord par score décroissant, puis BLOCKED, puis les terminées
        def sort_key(t: CampaignTask):
            status_order = {
                TaskStatus.PENDING: 0,
                TaskStatus.BLOCKED: 1,
                TaskStatus.UNCERTAIN: 2,
                TaskStatus.REJECTED: 3,
                TaskStatus.ACCEPTED: 4,
                TaskStatus.SKIPPED: 5,
            }
            return (status_order.get(t.status, 9), -t.priority_score)

        return sorted(tasks, key=sort_key)

    def run(
        self,
        tasks: list[CampaignTask],
        budget: CampaignBudget | None = None,
        *,
        model_budget: ModelCallBudget | None = None,
    ) -> CampaignOutcome:
        """Exécute une campagne autonome bornée sur un ensemble de tâches."""
        b = budget or self.default_budget
        if b.execution_limits is not None:
            from self_improvement.execution_limits import ExecutionLimits
            limits = ExecutionLimits.from_dict(b.execution_limits)
            if (b.max_tasks, b.max_source_files, b.max_total_diff_lines) != (limits.max_tasks, limits.max_source_files, limits.max_diff_lines):
                raise ValueError("unsupported_execution_limits: campaign caps differ")
            if b.max_model_calls > limits.max_model_calls:
                raise ValueError("unsupported_execution_limits: campaign model cap")
        shared_model_budget = (
            model_budget.child(b.max_model_calls)
            if model_budget is not None
            else ModelCallBudget(b.max_model_calls)
        )
        initial_model_calls = shared_model_budget.used_calls
        root_model_budget = shared_model_budget
        while root_model_budget.parent is not None:
            root_model_budget = root_model_budget.parent
        campaign_id = f"cmp_mgr_{int(time.time() * 1000)}"
        campaign_start_time = time.perf_counter()

        events: list[CampaignEvent] = []
        accepted_ids: list[str] = []
        rejected_ids: list[str] = []
        uncertain_ids: list[str] = []
        consecutive_failures = 0
        tasks_run_count = 0
        total_model_calls = 0
        total_attempts = 0
        total_diff_lines = 0
        candidate_first_mode = any(bool(task.metadata.get("candidate_first")) for task in tasks)
        initial_task_fingerprints: set[tuple] = set()

        def log_event(event_type: CampaignEventType, task_id: str | None = None, score: float | None = None, res: str | None = None, details: dict | None = None):
            ev = CampaignEvent(
                event_id=f"ev_{int(time.time() * 1000)}_{len(events)+1}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type.value,
                task_id=task_id,
                priority_score=score,
                result=res,
                budget_usage={
                    "tasks_run": tasks_run_count,
                    "tasks_accepted": len(accepted_ids),
                    "tasks_rejected": len(rejected_ids),
                    "tasks_uncertain": len(uncertain_ids),
                    "total_model_calls": root_model_budget.used_calls if model_budget is not None else total_model_calls,
                    "campaign_provider_attempts": total_model_calls,
                    "logical_model_requests": root_model_budget.logical_requests,
                    "model_call_counting": "provider_attempts_including_planner_and_fallbacks",
                    "total_attempts": total_attempts,
                    "total_diff_lines": total_diff_lines,
                },
                duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                details=dict(details or {}),
            )
            events.append(ev)

        # -------------------------------------------------------------------
        # 1. INITIALISATION ET CONTRÔLE PRÉALABLE
        # -------------------------------------------------------------------
        log_event(CampaignEventType.CAMPAIGN_STARTED, details={
            "total_tasks": len(tasks),
            "max_duration_seconds": b.max_duration_seconds,
            "structural_floor_seconds": required_campaign_seconds(
                [self.required_task_seconds(task) for task in tasks], b,
            ),
        })

        if not tasks:
            log_event(CampaignEventType.CAMPAIGN_STOPPED, res="EMPTY_BACKLOG")
            return CampaignOutcome(
                campaign_id=campaign_id,
                success=True,
                stop_reason=StopReason.BACKLOG_COMPLETED.value,
                tasks_total=0,
                tasks_run=0,
                tasks_accepted=0,
                tasks_rejected=0,
                tasks_uncertain=0,
                tasks_blocked=0,
                budget_usage={},
                duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                events=[e.to_dict() for e in events],
            )

        # Copie locale du backlog
        backlog = list(tasks)

        # -------------------------------------------------------------------
        # 2. VÉRIFICATION DE SÉCURITÉ HOLDOUT
        # -------------------------------------------------------------------
        for t in backlog:
            if self._is_holdout_violation(t):
                t.status = TaskStatus.REJECTED
                t.last_outcome = "SAFETY_VIOLATION: mention du holdout interdite."
                log_event(CampaignEventType.CAMPAIGN_STOPPED, task_id=t.task_id, res="SAFETY_VIOLATION")
                return CampaignOutcome(
                    campaign_id=campaign_id,
                    success=False,
                    stop_reason=StopReason.SAFETY_VIOLATION.value,
                    tasks_total=len(backlog),
                    tasks_run=0,
                    tasks_accepted=0,
                    tasks_rejected=1,
                    tasks_uncertain=0,
                    tasks_blocked=0,
                    rejected_task_ids=[t.task_id],
                    remaining_tasks=[tsk.to_dict() for tsk in backlog],
                    duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
                    events=[e.to_dict() for e in events],
                )

        stop_reason = StopReason.BACKLOG_COMPLETED

        # -------------------------------------------------------------------
        # 3. BOUCLE DE LA CAMPAGNE
        # -------------------------------------------------------------------
        while True:
            # Re-ranking du backlog et résolution des dépendances
            backlog = self._rerank_backlog(backlog)

            # Vérification des conditions d'arrêt sur budgets
            elapsed = time.perf_counter() - campaign_start_time
            if elapsed >= b.max_duration_seconds:
                stop_reason = StopReason.MAX_DURATION_EXCEEDED
                log_event(CampaignEventType.BUDGET_WARNING, details={"reason": "max_duration_seconds"})
                break

            if tasks_run_count >= b.max_tasks:
                stop_reason = StopReason.MAX_TASKS_REACHED
                break

            if len(accepted_ids) >= b.max_accepted_changes:
                stop_reason = StopReason.MAX_ACCEPTED_REACHED
                break

            if len(rejected_ids) >= b.max_rejected_tasks:
                stop_reason = StopReason.MAX_REJECTED_REACHED
                break

            if len(uncertain_ids) >= b.max_uncertain_tasks:
                stop_reason = StopReason.MAX_UNCERTAIN_REACHED
                break

            if consecutive_failures >= b.max_consecutive_failures:
                stop_reason = StopReason.MAX_CONSECUTIVE_FAILURES
                break

            if total_attempts >= b.max_total_attempts:
                stop_reason = StopReason.MAX_TASKS_REACHED
                break

            if total_diff_lines >= b.max_total_diff_lines:
                stop_reason = StopReason.MAX_TOTAL_DIFF_EXCEEDED
                break

            if candidate_first_mode and tasks_run_count >= 2:
                stop_reason = StopReason.ZERO_PROGRESS
                break

            # Sélection de la prochaine tâche éligible
            eligible_tasks = [
                t for t in backlog
                if t.status == TaskStatus.PENDING and t.runs_count < b.max_runs_per_task
            ]

            if not eligible_tasks:
                # Vérifier si des tâches restent bloquées
                blocked_tasks = [t for t in backlog if t.status == TaskStatus.BLOCKED]
                if blocked_tasks and not any(t.status == TaskStatus.PENDING for t in backlog):
                    stop_reason = StopReason.ALL_TASKS_BLOCKED
                else:
                    stop_reason = StopReason.NO_VIABLE_TASKS if any(t.runs_count >= b.max_runs_per_task for t in backlog) else StopReason.BACKLOG_COMPLETED
                break

            # Le budget interdit de commencer une nouvelle tâche, mais ne doit pas
            # remplacer BACKLOG_COMPLETED lorsque la dernière tâche a consommé
            # exactement le nombre d'appels alloué.
            if shared_model_budget.remaining_calls <= 0 or total_model_calls >= b.max_model_calls:
                stop_reason = StopReason.MODEL_BUDGET_EXHAUSTED
                break

            current_task = eligible_tasks[0]
            task_fingerprint = (
                tuple(sorted(str(path) for path in current_task.target_files)),
                tuple(sorted(str(test) for test in current_task.tests)),
                str(current_task.problem_type or "").casefold(),
                str(current_task.root_cause or "").strip().casefold(),
            )
            comparable_task = bool(current_task.target_files and current_task.tests)
            if (not candidate_first_mode and comparable_task
                    and task_fingerprint in initial_task_fingerprints):
                stop_reason = StopReason.ZERO_PROGRESS
                log_event(CampaignEventType.CAMPAIGN_STOPPED, task_id=current_task.task_id,
                          res="EQUIVALENT_TASK_SUPPRESSED",
                          details={"reason": "same_targets_tests_problem_and_root_cause"})
                break
            if comparable_task:
                initial_task_fingerprints.add(task_fingerprint)
            structural_floor = self.required_task_seconds(current_task)
            remaining_seconds = max(0.0, b.max_duration_seconds - (time.perf_counter() - campaign_start_time))
            if b.execution_limits is not None:
                from self_improvement.execution_limits import ExecutionLimits
                limits = ExecutionLimits.from_dict(b.execution_limits)
                remaining_seconds = min(remaining_seconds, max(0.0, limits.remaining() - limits.rollback_reserve_seconds - 300.0))
            if remaining_seconds < structural_floor:
                stop_reason = StopReason.INSUFFICIENT_CAMPAIGN_TIME
                break
            current_task.status = TaskStatus.RUNNING
            current_task.runs_count += 1
            tasks_run_count += 1

            log_event(
                CampaignEventType.TASK_SELECTED,
                task_id=current_task.task_id,
                score=current_task.priority_score,
                details={"title": current_task.title, "runs": current_task.runs_count},
            )

            # ---------------------------------------------------------------
            # 4. EXÉCUTION DE LA TÂCHE VIA IMPROVEMENT ORCHESTRATOR
            # ---------------------------------------------------------------
            orch_req = ImprovementRequest(
                task=current_task.task,
                problem_type=current_task.problem_type,
                root_cause=current_task.root_cause,
                target_files=current_task.target_files,
                tests=current_task.tests,
                tags=current_task.tags,
                metadata=current_task.metadata,
            )

            candidate_first = bool(current_task.metadata.get("candidate_first"))
            orch_budget = ImprovementBudget(
                max_attempts=min(1 if candidate_first else 2, b.max_total_attempts - total_attempts or 1),
                max_model_calls=shared_model_budget.remaining_calls,
                max_files_changed=b.max_source_files,
                max_diff_lines=b.max_total_diff_lines if b.execution_limits is not None else min(300, b.max_total_diff_lines),
                execution_limits=b.execution_limits,
                max_duration_seconds=min(structural_floor, remaining_seconds),
            )
            log_event(CampaignEventType.TASK_STARTED, task_id=current_task.task_id, details={
                "structural_floor_seconds": structural_floor,
                "max_duration_seconds": orch_budget.max_duration_seconds,
                "remaining_campaign_seconds": remaining_seconds,
                "structural_floor_limited_by_remaining_budget": remaining_seconds < structural_floor,
            })

            # Reviewer is nested inside DeveloperAgent but owns a distinct call
            # envelope. Both remain children of the same authoritative counter.
            role_budget = RoleBudgetState(shared_model_budget, {"reviewer": 1})
            developer_model_budget = role_budget.child_for("developer")
            orch_budget.reviewer_model_budget = role_budget.child_for("reviewer", maximum=1)

            run_parameters = inspect.signature(self.orchestrator.run).parameters
            budget_kwargs = (
                {"model_budget": developer_model_budget}
                if "model_budget" in run_parameters
                or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in run_parameters.values())
                else {}
            )
            outcome: ImprovementOutcome = self.orchestrator.run(
                orch_req, budget=orch_budget, **budget_kwargs
            )

            total_attempts += len(outcome.attempts)
            if budget_kwargs:
                total_model_calls = shared_model_budget.used_calls - initial_model_calls
            else:
                total_model_calls += sum(
                    (att.developer_result.model_attempts or 1)
                    for att in outcome.attempts
                    if att.developer_result
                )

            if shared_model_budget.remaining_calls == 0 and not outcome.success:
                stop_reason = StopReason.MODEL_BUDGET_EXHAUSTED

            # Calcul du diff cumulé
            if outcome.after_metrics and "change_risk" in outcome.after_metrics:
                cr = outcome.after_metrics["change_risk"]
                total_diff_lines += (cr.get("lines_added", 0) + cr.get("lines_removed", 0))

            # ---------------------------------------------------------------
            # 5. TRAITEMENT DU RÉSULTAT ET GESTION DES STATUTS
            # ---------------------------------------------------------------
            feedback_details = {"reason": outcome.reason}
            current_task.metadata["last_improvement_reason"] = outcome.reason
            if outcome.attempts:
                last_dev = outcome.attempts[-1].developer_result
                last_judge = outcome.attempts[-1].judge_result
                if last_dev:
                    feedback_details["developer_result"] = {
                        "status": getattr(last_dev, "status", "unknown"),
                        "summary": getattr(last_dev, "summary", "")[:3000],
                        "failure_reason": last_dev.failure_reason,
                        "files_examined": list(getattr(last_dev, "files_examined", []) or [])[:20],
                        "files_modified": list(getattr(last_dev, "files_modified", []) or last_dev.files_changed)[:20],
                        "files_created": list(last_dev.files_created)[:20],
                        "needs_replan": bool(last_dev.replan_requested),
                        "replan_reason": getattr(last_dev, "replan_reason", None),
                        "review_decision": getattr(last_dev, "review_decision", None),
                        "reviewer_confidence": getattr(last_dev, "reviewer_confidence", 0.0),
                        "reviewer_summary": getattr(last_dev, "reviewer_summary", "")[:1000],
                        "reviewer_concerns": list(getattr(last_dev, "reviewer_concerns", []) or [])[:6],
                        "model_used": getattr(last_dev, "model_used", None),
                        "model_attempts": getattr(last_dev, "model_attempts", None),
                        "logical_model_requests": getattr(last_dev, "logical_model_requests", None),
                        "attempt_history": list(getattr(last_dev, "attempt_history", []) or [])[:8],
                        "pipeline_trace": dict(getattr(last_dev, "pipeline_trace", {}) or {}),
                    }
                if last_judge:
                    feedback_details["judge_feedback"] = last_judge.to_dict()
                if last_dev and getattr(last_dev, "replan_requested", False):
                    recommended_files = list(getattr(last_dev, "recommended_files", []) or [])
                    recommended_tests = list(getattr(last_dev, "recommended_tests", []) or [])
                    current_task.metadata["developer_replan"] = {
                        "recommended_files": recommended_files[:12],
                        "recommended_tests": recommended_tests[:12],
                        "exploration_summary": getattr(last_dev, "exploration_summary", "")[:4000],
                    }
                    feedback_details["developer_replan"] = current_task.metadata["developer_replan"]

            if outcome.final_decision == "ACCEPT":
                current_task.status = TaskStatus.ACCEPTED
                current_task.last_outcome = "ACCEPTED"
                accepted_ids.append(current_task.task_id)
                consecutive_failures = 0
                log_event(CampaignEventType.TASK_ACCEPTED, task_id=current_task.task_id, res="ACCEPT", details=feedback_details)
                if candidate_first:
                    stop_reason = StopReason.CANDIDATE_READY
                    break

            elif outcome.final_decision == "REJECT":
                current_task.last_outcome = "REJECTED"
                consecutive_failures += 1

                if current_task.task_id not in rejected_ids:
                    rejected_ids.append(current_task.task_id)

                log_event(
                    CampaignEventType.TASK_REJECTED,
                    task_id=current_task.task_id,
                    res="REJECT",
                    details=feedback_details,
                )

                if current_task.runs_count < b.max_runs_per_task:
                    current_task.status = TaskStatus.PENDING
                else:
                    current_task.status = TaskStatus.REJECTED

            else:  # UNCERTAIN
                current_task.status = TaskStatus.UNCERTAIN
                current_task.last_outcome = "UNCERTAIN"
                uncertain_ids.append(current_task.task_id)
                consecutive_failures += 1
                log_event(CampaignEventType.TASK_UNCERTAIN, task_id=current_task.task_id, res="UNCERTAIN", details=feedback_details)

        # -------------------------------------------------------------------
        # 6. CLÔTURE DE LA CAMPAGNE ET BILAN
        # -------------------------------------------------------------------
        log_event(CampaignEventType.CAMPAIGN_STOPPED, res=stop_reason.value)

        blocked_count = sum(1 for t in backlog if t.status == TaskStatus.BLOCKED)

        return CampaignOutcome(
            campaign_id=campaign_id,
            success=len(accepted_ids) > 0 and stop_reason not in (StopReason.SAFETY_VIOLATION, StopReason.ROLLBACK_FAILURE),
            stop_reason=stop_reason.value,
            tasks_total=len(backlog),
            tasks_run=tasks_run_count,
            tasks_accepted=len(accepted_ids),
            tasks_rejected=len(rejected_ids),
            tasks_uncertain=len(uncertain_ids),
            tasks_blocked=blocked_count,
            accepted_task_ids=accepted_ids,
            rejected_task_ids=rejected_ids,
            uncertain_task_ids=uncertain_ids,
            remaining_tasks=[t.to_dict() for t in backlog],
            budget_usage={
                "tasks_run": tasks_run_count,
                "tasks_accepted": len(accepted_ids),
                "tasks_rejected": len(rejected_ids),
                "tasks_uncertain": len(uncertain_ids),
                "total_model_calls": root_model_budget.used_calls if model_budget is not None else total_model_calls,
                "campaign_provider_attempts": total_model_calls,
                "logical_model_requests": root_model_budget.logical_requests,
                "model_call_counting": "provider_attempts_including_planner_and_fallbacks",
                "total_attempts": total_attempts,
                "total_diff_lines": total_diff_lines,
            },
            duration_seconds=round(time.perf_counter() - campaign_start_time, 2),
            events=[e.to_dict() for e in events],
        )
