"""Point d'entrée unifié du Software Agent autonome V5.

Exemples :
    python -m self_improvement.agent_runtime doctor
    python -m self_improvement.agent_runtime inspect "Ajoute OpenRouter Provider V1"
    python -m self_improvement.agent_runtime plan "Ajoute OpenRouter Provider V1"
    python -m self_improvement.agent_runtime objective "Ajoute OpenRouter Provider V1"
    python -m self_improvement.agent_runtime self-improve --cycles 1 --dry-run
    python -m self_improvement.agent_runtime recover
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, TYPE_CHECKING

from self_improvement.agent_doctor import run_doctor

if TYPE_CHECKING:  # imports lourds seulement pour le typage
    from self_improvement.engineering_orchestrator import EngineeringOrchestrator
    from self_improvement.trusted_supervisor import TrustedSelfImprovementSupervisor


class AutonomousSoftwareAgent:
    """Façade publique : objectif humain et auto-amélioration partagent le même moteur."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        engineering_orchestrator: "EngineeringOrchestrator | None" = None,
        self_improvement_agent: "TrustedSelfImprovementSupervisor | None" = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        if engineering_orchestrator is None:
            from self_improvement.engineering_orchestrator import EngineeringOrchestrator
            engineering_orchestrator = EngineeringOrchestrator(self.repo_root)
        self.engineering = engineering_orchestrator

        if self_improvement_agent is None:
            # L'auto-amélioration active passe toujours par le superviseur externe
            # de confiance. L'ancien AutonomousSelfImprovementAgent reste une brique
            # cognitive/compatibilité, mais ne décide plus seul de conserver son code.
            from self_improvement.trusted_supervisor import TrustedSelfImprovementSupervisor
            self.self_improvement = TrustedSelfImprovementSupervisor(self.repo_root)
        else:
            self.self_improvement = self_improvement_agent

    def plan_objective(self, goal: str) -> dict[str, Any]:
        """Prévisualise et valide un plan sans exécuter de modification.

        Cette entrée est volontairement read-only : elle permet de vérifier ce que
        l'agent compte faire avant un chantier réel, tout en utilisant exactement le
        même Planner que ``run_objective``.
        """
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("L'objectif ne peut pas être vide.")
        from self_improvement.engineering_planner import EngineeringObjective

        objective = EngineeringObjective(goal=goal.strip())
        plan = self.engineering.planner.plan(objective)
        self.engineering.planner.validate_plan(plan)
        return {"mode": "plan", "result": plan.to_dict()}

    def run_objective(
        self,
        goal: str,
        *,
        constraints: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        self_improvement_mode: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("L'objectif ne peut pas être vide.")
        if constraints or metadata or self_improvement_mode:
            from self_improvement.engineering_planner import EngineeringObjective
            objective = EngineeringObjective(
                goal=goal.strip(),
                constraints=list(constraints or []),
                metadata=dict(metadata or {}),
            )
            if self_improvement_mode:
                from self_improvement.engineering_orchestrator import GlobalValidationResult
                from self_improvement.campaign_manager import CampaignBudget
                from self_improvement.execution_limits import ExecutionLimits
                limits = ExecutionLimits.from_dict(objective.metadata.get("execution_limits"))
                if objective.metadata.get("max_model_calls") != limits.max_model_calls:
                    raise ValueError("unsupported_execution_limits: inconsistent model cap")
                worker_seconds = limits.phase_timeout(1200.0, minimum=480.0, future_seconds=300.0)
                worker_model_calls = limits.max_model_calls
                result = self.engineering.run(
                    objective,
                    campaign_budget=CampaignBudget(
                        max_tasks=limits.max_tasks, max_model_calls=worker_model_calls,
                        max_source_files=limits.max_source_files, max_total_diff_lines=limits.max_diff_lines,
                        max_duration_seconds=worker_seconds, execution_limits=limits.to_dict()),
                    protect_existing_tests=True,
                    # Le worker d'auto-amélioration travaille dans un workspace
                    # assaini. La validation globale autoritative est volontairement
                    # différée au TrustedSupervisor sur le vrai repository.
                    global_validator=lambda: GlobalValidationResult(
                        True, "deferred_to_trusted_supervisor", tests_run=0, tests_failed=0
                    ),
                )
            else:
                result = self.engineering.run(objective)
        else:
            result = self.engineering.run(goal.strip())
        return {"mode": "objective", "result": result.to_dict()}

    def run_self_improvement(
        self,
        *,
        budget: Any = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        result = self.self_improvement.run(budget=budget, dry_run=dry_run)
        return {"mode": "self-improve", "result": result.to_dict()}

    def run_mission(self, goal: str, *, max_rounds: int = 4) -> dict[str, Any]:
        from self_improvement.mission_manager import MissionManager
        manager = MissionManager(self.repo_root)
        result = manager.run(goal, max_rounds=max_rounds)
        return {"mode": "mission", "result": result.to_dict()}

    def resume_mission(self, *, max_rounds: int = 4) -> dict[str, Any]:
        from self_improvement.mission_manager import MissionManager
        manager = MissionManager(self.repo_root)
        result = manager.resume(max_rounds=max_rounds)
        return {"mode": "mission-resume", "result": result.to_dict()}

    def mission_status(self) -> dict[str, Any]:
        from self_improvement.mission_manager import MissionManager
        manager = MissionManager(self.repo_root)
        return {"mode": "mission-status", "result": manager.status()}

    def meta_learning_status(self) -> dict[str, Any]:
        from self_improvement.meta_learning import MetaLearningAnalyzer
        report = MetaLearningAnalyzer(self.repo_root).analyze()
        return {"mode": "learn-status", "result": report.to_dict()}

    def route_goal(self, goal: str, *, execute: bool = False) -> dict[str, Any]:
        from self_improvement.goal_orchestrator import GoalOrchestrator
        router = GoalOrchestrator()
        if execute:
            return {"mode": "agent", "result": router.run(goal, software_agent=self)}
        return {"mode": "route", "result": router.classify(goal).to_dict()}

    def observability_status(self) -> dict[str, Any]:
        from self_improvement.observability import AgentTelemetry
        return {"mode": "metrics", "result": AgentTelemetry(self.repo_root).summary()}

    def brain_readiness(self, goal: str = "Ajoute OpenRouter Provider V1") -> dict[str, Any]:
        from self_improvement.brain_readiness import run_brain_readiness
        return {"mode": "brain-check", "result": run_brain_readiness(self.repo_root, goal).to_dict()}

    def provider_status(self) -> dict[str, Any]:
        from model_router import provider_health_snapshot
        return {"mode": "providers", "result": provider_health_snapshot()}

    def provider_smoke(self) -> dict[str, Any]:
        from model_router import smoke_test_providers
        return {"mode": "provider-smoke", "result": smoke_test_providers()}

    def omniroute_status(self) -> dict[str, Any]:
        from model_router import omniroute_status
        return {"mode": "omniroute-status", "result": omniroute_status(force=True)}

    def models(self, *, pool: str | None = None) -> dict[str, Any]:
        from model_router import model_catalog_snapshot
        return {"mode": "models", "result": model_catalog_snapshot(pool=pool, force=True)}

    def routing_test(self, task: str) -> dict[str, Any]:
        from model_router import routing_diagnostic
        return {"mode": "routing-test", "result": routing_diagnostic(task)}

    def sandbox_status(self) -> dict[str, Any]:
        from self_improvement.sandbox_executor import SandboxExecutor
        executor = SandboxExecutor(self.repo_root)
        try:
            backend = executor.backend
            error = None
        except RuntimeError as exc:
            backend, error = "unavailable", str(exc)
        return {"mode": "sandbox-status", "result": {"configured_mode": executor.mode, "backend": backend, "docker_available": executor.docker_available(), "image": executor.image, "image_ready": executor.docker_image_available(), "error": error}}

    def inspect_objective(self, goal: str) -> dict[str, Any]:
        """Montre les faits repo visibles par l'agent sans modèle ni écriture."""
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("L'objectif ne peut pas être vide.")
        from self_improvement.repo_intelligence import RepoIntelligence

        intelligence = RepoIntelligence(self.repo_root)
        relevant = intelligence.relevant_files(goal.strip(), limit=12)
        return {
            "mode": "inspect",
            "goal": goal.strip(),
            "relevant_files": [item.path for item in relevant],
            "context": intelligence.context_for_objective(goal.strip(), max_files=10, include_inventory=60),
            "read_only": True,
        }

    def recovery_status(self) -> dict[str, Any]:
        """Expose d'abord le checkpoint externe d'auto-amélioration, puis celui d'ingénierie."""
        trusted_status = getattr(self.self_improvement, "recovery_status", None)
        if callable(trusted_status):
            payload = dict(trusted_status())
            if payload.get("pending"):
                return payload
        status_fn = getattr(self.engineering, "recovery_status", None)
        if callable(status_fn):
            return dict(status_fn())
        return {"pending": False, "supported": callable(trusted_status)}

    def recover(self) -> dict[str, Any]:
        """Restaure automatiquement le checkpoint le plus externe disponible."""
        trusted_status = getattr(self.self_improvement, "recovery_status", None)
        trusted_recover = getattr(self.self_improvement, "recover_repository", None)
        if callable(trusted_status) and callable(trusted_recover):
            status = dict(trusted_status())
            if status.get("pending"):
                result = trusted_recover()
                to_dict = getattr(result, "to_dict", None)
                payload = to_dict() if callable(to_dict) else {
                    "success": bool(getattr(result, "success", False)),
                    "reason": str(getattr(result, "reason", result)),
                    "restored_files": int(getattr(result, "restored_files", 0) or 0),
                }
                payload.setdefault("scope", "trusted_self_improvement")
                return {"mode": "recover", "result": payload}

        recovery_fn = getattr(self.engineering, "recover_repository", None)
        if not callable(recovery_fn):
            raise RuntimeError("recovery_not_supported")
        result = recovery_fn()
        to_dict = getattr(result, "to_dict", None)
        payload = to_dict() if callable(to_dict) else {"success": False, "reason": str(result)}
        return {"mode": "recover", "result": payload}


def _print_or_save(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        printable = text.encode(encoding, errors="backslashreplace").decode(encoding)
        print(printable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Écrit aussi le rapport JSON dans ce fichier.")
    sub = parser.add_subparsers(dest="mode", required=True)

    sub.add_parser("doctor", help="Diagnostic local non destructif, sans réseau ni secrets.")

    inspect_cmd = sub.add_parser("inspect", help="Affiche la compréhension read-only du repo pour un objectif.")
    inspect_cmd.add_argument("goal")

    plan_cmd = sub.add_parser("plan", help="Génère et valide un plan sans exécuter de modifications.")
    plan_cmd.add_argument("goal")

    objective = sub.add_parser("objective", help="Exécute un objectif logiciel haut niveau.")
    objective.add_argument("goal", nargs="?")
    objective.add_argument("--objective-file", type=Path, help=argparse.SUPPRESS)
    objective.add_argument("--self-improvement", action="store_true", help=argparse.SUPPRESS)

    improve = sub.add_parser("self-improve", help="Lance une boucle d'auto-amélioration publique et vérifiée.")
    improve.add_argument("--cycles", type=int, default=1)
    improve.add_argument("--max-minutes", type=float, default=20.0)
    improve.add_argument("--max-tasks", type=int, default=1)
    improve.add_argument("--max-source-files", type=int, default=2)
    improve.add_argument("--max-diff-lines", type=int, default=120)
    improve.add_argument("--max-model-calls", type=int, default=15)
    improve.add_argument("--minimum-improvement", type=float, default=0.5)
    improve.add_argument("--target-score", type=float, default=98.0)
    improve.add_argument("--dry-run", action="store_true")
    improve.add_argument("--preflight-only", action="store_true", help="Valide seulement les plafonds, sans benchmark, modèle ni cycle.")
    improve.add_argument("--git-checkpoint", action="store_true", help="Crée un commit Git minimal après chaque cycle ACCEPT si le repo est propre.")
    improve.add_argument("--continuous", action="store_true", help="Mode longue durée borné : jusqu'à 10 cycles, toujours limité par --max-minutes et --target-score.")

    mission = sub.add_parser("mission", help="Exécute une mission longue en plusieurs objectifs vérifiés.")
    mission.add_argument("goal")
    mission.add_argument("--rounds", type=int, default=4)

    mission_resume = sub.add_parser("mission-resume", help="Reprend une mission persistée interrompue ou arrivée à son budget de tours.")
    mission_resume.add_argument("--rounds", type=int, default=4)

    sub.add_parser("mission-status", help="Affiche l'état persistant de la dernière mission.")
    sub.add_parser("learn-status", help="Analyse la mémoire d'expériences et les stratégies récurrentes.")

    route = sub.add_parser("route", help="Prévisualise quel spécialiste recevrait un objectif général.")
    route.add_argument("goal")
    general = sub.add_parser("agent", help="Route et exécute un objectif via le spécialiste approprié.")
    general.add_argument("goal")
    sub.add_parser("sandbox-status", help="Affiche le backend d'isolation disponible (Docker/local).")
    sub.add_parser("providers", help="Affiche les providers, budgets et états de cooldown sans exposer les clés.")
    sub.add_parser("provider-smoke", help="Teste en live les providers configurés avec un prompt minuscule (consomme un petit quota).")
    sub.add_parser("omniroute-status", help="Teste /models et résume les pools OmniRoute sans afficher de clé.")
    models_cmd = sub.add_parser("models", help="Liste le catalogue dynamique OmniRoute.")
    models_cmd.add_argument("--pool", choices=["coding", "reasoning", "fast", "general", "vision", "free", "cheap", "high_context", "tool_calling", "reliable"])
    routing_cmd = sub.add_parser("routing-test", help="Sélectionne et explique un candidat sans appel de génération payant.")
    routing_cmd.add_argument("--task", default="general")
    brain = sub.add_parser("brain-check", help="Vérifie le chemin critique Planner/Router/Edit avant un test autonome live.")
    brain.add_argument("goal", nargs="?", default="Ajoute OpenRouter Provider V1")
    sub.add_parser("metrics", help="Résumé local de l'observabilité V6.")

    sub.add_parser("recover", help="Restaure le dernier checkpoint si un chantier a été interrompu brutalement.")

    args = parser.parse_args(argv)

    if args.mode == "doctor":
        report = run_doctor(Path.cwd())
        payload = {"mode": "doctor", "result": report.to_dict()}
        _print_or_save(payload, args.output)
        return 0 if report.ready else 2

    agent = AutonomousSoftwareAgent(Path.cwd())

    if args.mode == "inspect":
        payload = agent.inspect_objective(args.goal)
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "plan":
        payload = agent.plan_objective(args.goal)
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "mission":
        payload = agent.run_mission(args.goal, max_rounds=args.rounds)
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("success") else 2

    if args.mode == "mission-resume":
        payload = agent.resume_mission(max_rounds=args.rounds)
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("success") else 2

    if args.mode == "mission-status":
        payload = agent.mission_status()
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "learn-status":
        payload = agent.meta_learning_status()
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "route":
        payload = agent.route_goal(args.goal, execute=False)
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "agent":
        payload = agent.route_goal(args.goal, execute=True)
        _print_or_save(payload, args.output)
        result = payload.get("result", {}).get("result")
        return 0 if result is not None else 2

    if args.mode == "sandbox-status":
        payload = agent.sandbox_status()
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "providers":
        payload = agent.provider_status()
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "provider-smoke":
        payload = agent.provider_smoke()
        _print_or_save(payload, args.output)
        summary = payload["result"].get("summary", {})
        return 0 if summary.get("routes_healthy", summary.get("healthy", 0)) > 0 else 2

    if args.mode == "omniroute-status":
        payload = agent.omniroute_status()
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("reachable") else 2

    if args.mode == "models":
        payload = agent.models(pool=args.pool)
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("models_count", 0) else 2

    if args.mode == "routing-test":
        payload = agent.routing_test(args.task)
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("selected") else 2

    if args.mode == "brain-check":
        payload = agent.brain_readiness(args.goal)
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("status") in {"READY", "DEGRADED"} else 2

    if args.mode == "metrics":
        payload = agent.observability_status()
        _print_or_save(payload, args.output)
        return 0

    if args.mode == "recover":
        payload = agent.recover()
        _print_or_save(payload, args.output)
        return 0 if payload["result"].get("success") else 2

    if args.mode == "objective":
        constraints: list[str] = []
        metadata: dict[str, Any] = {}
        goal = args.goal
        if args.objective_file:
            raw = json.loads(args.objective_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("objective_file_invalid")
            goal = str(raw.get("goal") or "")
            constraints = [str(item) for item in raw.get("constraints", []) if isinstance(item, str)]
            metadata = dict(raw.get("metadata") or {})
        payload = agent.run_objective(
            goal or "",
            constraints=constraints,
            metadata=metadata,
            self_improvement_mode=bool(args.self_improvement),
        )
        _print_or_save(payload, args.output)
        decision = payload["result"].get("final_decision")
        return 0 if decision == "ACCEPT" else 2

    from self_improvement.trusted_supervisor import TrustedSelfImprovementBudget

    budget = TrustedSelfImprovementBudget(
        max_cycles=10 if args.continuous else args.cycles,
        max_minutes=args.max_minutes,
        max_tasks_per_cycle=args.max_tasks,
        max_source_files=args.max_source_files,
        max_diff_lines=args.max_diff_lines,
        max_model_calls_per_cycle=args.max_model_calls,
        minimum_improvement=args.minimum_improvement,
        target_score=args.target_score,
        git_checkpoint=bool(args.git_checkpoint),
    )
    if args.preflight_only:
        from self_improvement.trusted_supervisor import supervised_preflight
        result = supervised_preflight(budget)
        _print_or_save({"mode": "preflight", "result": result}, args.output)
        return 0 if result["status"] == "CONFIGURATION_READY" else 2
    payload = agent.run_self_improvement(budget=budget, dry_run=args.dry_run)
    _print_or_save(payload, args.output)
    decision = payload["result"].get("final_decision")
    return 0 if decision in {"ACCEPT", "TARGET_REACHED", "NO_ACTION", "DRY_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
