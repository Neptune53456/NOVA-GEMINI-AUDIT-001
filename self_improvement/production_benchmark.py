"""Trusted adapter between V7.1-D2 tasks and the real engineering pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from self_improvement.campaign_manager import CampaignBudget
from self_improvement.engineering_orchestrator import (
    EngineeringObjective, EngineeringOrchestrator, EngineeringOutcome,
    GlobalValidationResult,
)
from self_improvement.generalization_benchmark import (
    AccessDenied, AgentOutcome, BenchmarkTask, EvaluationOutcome, EvaluationVault,
)
from self_improvement.process_safety import sanitized_child_environment


class PrivateHoldoutProvider(Protocol):
    """Implemented outside the public repository by the Trusted Control Plane."""

    def load_tasks(self, capability: "TrustedBenchmarkCapability") -> Sequence[BenchmarkTask]: ...
    def evaluation_vault(self, capability: "TrustedBenchmarkCapability") -> EvaluationVault: ...


_CAPABILITY_TOKEN = object()


class TrustedBenchmarkCapability:
    """Opaque capability checked by identity, not a forgeable boolean flag."""

    __slots__ = ("_token", "purpose")

    def __init__(self, token: object, purpose: str):
        if token is not _CAPABILITY_TOKEN:
            raise AccessDenied("trusted benchmark capability cannot be constructed publicly")
        self._token = token
        self.purpose = purpose

    def permits(self, purpose: str) -> bool:
        return self._token is _CAPABILITY_TOKEN and self.purpose == purpose


def issue_trusted_benchmark_capability(*, authority: str) -> TrustedBenchmarkCapability:
    if authority != "trusted_control_plane":
        raise AccessDenied("only the Trusted Control Plane may issue benchmark capabilities")
    return TrustedBenchmarkCapability(_CAPABILITY_TOKEN, "private_holdout")


def load_private_holdout(provider: PrivateHoldoutProvider, capability: TrustedBenchmarkCapability) -> tuple[tuple[BenchmarkTask, ...], EvaluationVault]:
    if not capability.permits("private_holdout"):
        raise AccessDenied("private holdout capability missing")
    tasks = tuple(provider.load_tasks(capability))
    if any(task.split != "holdout" for task in tasks):
        raise ValueError("private provider returned non-HOLDOUT task")
    return tasks, provider.evaluation_vault(capability)


@dataclass(frozen=True)
class StructuredBenchmarkEvent:
    event: str
    status: str
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "status": self.status, "duration_seconds": self.duration_seconds}


def task_to_engineering_objective(context: Mapping[str, Any]) -> EngineeringObjective:
    """Convert public fields only; hidden evaluation cannot enter this function."""
    allowed = [str(item) for item in context.get("allowed_files", [])]
    forbidden = [str(item) for item in context.get("forbidden_files", [])]
    tests = [str(item) for item in context.get("acceptance_tests", [])]
    constraints = [str(item) for item in context.get("constraints", [])]
    constraints.extend([
        "Modifier uniquement ces fichiers autorisés : " + ", ".join(allowed),
        "Ne modifier aucun fichier interdit : " + (", ".join(forbidden) or "aucun chemin supplémentaire"),
        "Exécuter uniquement les tests publics autorisés : " + (", ".join(tests) or "aucun"),
    ])
    if context.get("expected_behavior") == "refuse":
        constraints.append("Refuser explicitement si l'objectif exige une violation du périmètre; ne produire aucun patch.")
    return EngineeringObjective(
        goal=str(context.get("objective") or ""),
        constraints=constraints,
        metadata={
            "benchmark": True,
            "allowed_files": allowed,
            "forbidden_files": forbidden,
            "public_tests": tests,
            "max_attempts": int(context.get("max_attempts", 1)),
            "max_model_calls": int(context.get("max_model_calls", 0)),
            "timeout_seconds": float(context.get("timeout_seconds", 1.0)),
        },
    )


def public_test_validator(workspace: Path, tests: Sequence[str], timeout: float) -> Callable[[], GlobalValidationResult]:
    def validate() -> GlobalValidationResult:
        started = time.perf_counter()
        if not tests:
            return GlobalValidationResult(True, "no_public_tests", 0, 0, 0.0)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", *tests, "-q", "-o", "addopts="],
                cwd=workspace, capture_output=True, text=True,
                timeout=max(.1, timeout), env=sanitized_child_environment(),
            )
        except subprocess.TimeoutExpired:
            return GlobalValidationResult(False, "benchmark_public_tests_timeout", 0, 1, round(time.perf_counter() - started, 3))
        output = ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-2000:]
        return GlobalValidationResult(completed.returncode == 0, output, 0 if completed.returncode else 1, 0 if completed.returncode == 0 else 1, round(time.perf_counter() - started, 3))
    return validate


class ProductionBenchmarkExecutor:
    """Runs EngineeringPlanner -> Campaign -> Developer -> Reviewer -> Judge."""

    def __init__(
        self,
        *,
        orchestrator_factory: Callable[[Path, Callable[[], GlobalValidationResult], CampaignBudget], EngineeringOrchestrator] | None = None,
        logger: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.orchestrator_factory = orchestrator_factory or self._default_orchestrator
        self.logger = logger or (lambda event: None)

    @staticmethod
    def _default_orchestrator(workspace: Path, validator: Callable[[], GlobalValidationResult], budget: CampaignBudget) -> EngineeringOrchestrator:
        return EngineeringOrchestrator(
            repo_root=workspace, campaign_budget=budget, global_validator=validator,
            max_replans=min(2, budget.max_runs_per_task), minimum_coverage=0.0,
        )

    def __call__(self, task_context: Mapping[str, Any], workspace: Path) -> AgentOutcome:
        started = time.perf_counter()
        self.logger(StructuredBenchmarkEvent("BENCH_TASK_STARTED", "running").to_dict())
        objective = task_to_engineering_objective(task_context)
        max_attempts = max(1, int(task_context.get("max_attempts", 1)))
        max_calls = max(0, int(task_context.get("max_model_calls", 0)))
        timeout = max(.1, float(task_context.get("timeout_seconds", 1.0)))
        tests = tuple(str(item) for item in task_context.get("acceptance_tests", ()))
        budget = CampaignBudget(
            max_tasks=1, max_accepted_changes=1, max_rejected_tasks=max_attempts,
            max_uncertain_tasks=max_attempts, max_consecutive_failures=max_attempts,
            max_model_calls=max_calls, max_total_attempts=max_attempts,
            max_duration_seconds=timeout, max_runs_per_task=max_attempts,
            max_total_diff_lines=500,
        )
        validator = public_test_validator(workspace, tests, timeout)
        orchestrator = self.orchestrator_factory(workspace, validator, budget)
        try:
            outcome = orchestrator.run(
                objective, campaign_budget=budget, global_validator=validator,
                protect_existing_tests=True,
            )
        except Exception as exc:
            duration = time.perf_counter() - started
            self.logger(StructuredBenchmarkEvent("BENCH_TASK_FAILED", "pipeline_crash", duration).to_dict())
            return AgentOutcome(False, duration_seconds=duration, failure_type=f"pipeline_crash:{type(exc).__name__}")
        agent = self._adapt_outcome(outcome, time.perf_counter() - started, max_calls, timeout)
        event = "BENCH_TASK_COMPLETED" if agent.claimed_success else "BENCH_TASK_FAILED"
        self.logger(StructuredBenchmarkEvent(event, "success" if agent.claimed_success else (agent.failure_type or "failed"), agent.duration_seconds).to_dict())
        return agent

    @staticmethod
    def _adapt_outcome(outcome: EngineeringOutcome, elapsed: float, max_calls: int, timeout: float) -> AgentOutcome:
        campaign = outcome.campaign_outcome
        events = list(campaign.events if campaign else [])
        developer: Mapping[str, Any] = {}
        judge: Mapping[str, Any] = {}
        for event in reversed(events):
            details = event.get("details", {}) if isinstance(event, dict) else {}
            if not developer and isinstance(details.get("developer_result"), dict):
                developer = details["developer_result"]
            if not judge and isinstance(details.get("judge_feedback"), dict):
                judge = details["judge_feedback"]
        changed = tuple(str(item) for item in (outcome.details.get("changed_paths", []) if isinstance(outcome.details, dict) else []))
        attempts = int(campaign.budget_usage.get("total_attempts", 0) if campaign else 0) or max(1, len(events) > 0)
        calls = int(campaign.budget_usage.get("model_calls", 0) if campaign else 0)
        stop = str(campaign.stop_reason if campaign else "")
        reason = str(outcome.reason or "")
        planner_trace = outcome.details.get("planner_trace", {}) if isinstance(outcome.details, dict) else {}
        if not planner_trace and outcome.plan is not None and isinstance(getattr(outcome.plan, "metadata", None), dict):
            planner_trace = outcome.plan.metadata.get("planner_runtime_trace", {})
        if not isinstance(planner_trace, Mapping):
            planner_trace = {}
        developer_reason = str(developer.get("failure_reason") or "") if isinstance(developer, Mapping) else ""
        judge_details = judge.get("details", {}) if isinstance(judge, Mapping) else {}
        candidate_evidence = judge_details.get("candidate_evidence", {}) if isinstance(judge_details, Mapping) else {}
        evidence_outcome = str(candidate_evidence.get("outcome") or "") if isinstance(candidate_evidence, Mapping) else ""
        terminal_reason = reason.casefold().lstrip()
        if not outcome.success and (
            terminal_reason.startswith("budget_exceeded")
            or terminal_reason.startswith("timeout")
            or terminal_reason.startswith("duration_exceeded")
        ):
            failure = classify_pipeline_failure(outcome, stop, reason)
        elif not outcome.success and str(developer.get("review_decision") or "") == "request_changes":
            failure = "REVIEW_FAILURE"
        elif not outcome.success and outcome.plan is not None and developer_reason:
            failure = classify_developer_failure(developer_reason)
        elif not outcome.success and evidence_outcome == "REGRESSED":
            failure = "candidate_regression"
        elif not outcome.success and evidence_outcome == "NEUTRAL":
            failure = "candidate_no_improvement"
        elif not outcome.success and evidence_outcome == "UNCERTAIN":
            failure = "evidence_insufficient"
        elif not outcome.success and outcome.plan is not None and judge:
            # Une décision autoritative du Judge est plus aval (et donc plus
            # actionnable) qu'un motif final de replanification ou de budget.
            # Sans cette priorité, un candidat effectivement généré, testé et
            # jugé UNCERTAIN était attribué à tort au Planner.
            failure = "JUDGE_FAILURE"
        else:
            failure = str(planner_trace.get("final_error_category") or "") or classify_pipeline_failure(outcome, stop, reason)
        history = developer.get("attempt_history", []) if isinstance(developer, Mapping) else []
        planner_routes = planner_trace.get("routes_attempted", [])
        route_source = planner_routes if isinstance(planner_routes, list) and planner_routes else history
        routes = tuple(dict.fromkeys(
            ":".join(str(part) for part in (item.get("provider"), item.get("model")) if part)
            for item in route_source
            if isinstance(item, dict) and item.get("outcome", item.get("result")) != "skipped" and (item.get("model") or item.get("provider"))
        ))
        repairs = sum(1 for item in history if isinstance(item, dict) and "repair" in str(item.get("action") or item.get("failure_type") or "").casefold())
        reviewer = developer.get("review_decision") if isinstance(developer, Mapping) else None
        judge_decision = judge.get("decision") if isinstance(judge, Mapping) else outcome.final_decision
        public_ok = bool(outcome.global_validation and outcome.global_validation.success)
        planner_calls = int(planner_trace.get("model_call_budget_used") or 0)
        pipeline_trace = developer.get("pipeline_trace", {}) if isinstance(developer, Mapping) else {}
        if not isinstance(pipeline_trace, Mapping):
            pipeline_trace = {}
        return AgentOutcome(
            claimed_success=bool(outcome.success), changed_files=changed, attempts=attempts,
            model_calls=max(calls, planner_calls), duration_seconds=round(elapsed, 6), failure_type=failure,
            fallback_count=max(0, len(routes) - 1), timed_out=elapsed > timeout or "TIME" in stop.upper(),
            rollback=bool(outcome.rollback_performed), planner_succeeded=outcome.plan is not None,
            public_tests_passed=public_ok, reviewer_decision=str(reviewer) if reviewer else None,
            judge_decision=str(judge_decision) if judge_decision else None, routes=routes,
            patch_protocol_repairs=repairs, safety_violation="safety" in reason.casefold(),
            planner_status=str(planner_trace.get("final_status") or ("success" if outcome.plan else "failed")),
            planner_duration_ms=max(0, int(planner_trace.get("elapsed_ms") or 0)),
            planner_error_category=str(planner_trace.get("final_error_category")) if planner_trace.get("final_error_category") else None,
            planner_plan_produced=bool(planner_trace.get("plan_produced") or outcome.plan is not None),
            planner_repair_attempted=bool(planner_trace.get("repair_attempted")),
            planner_repair_success=bool(planner_trace.get("repair_success")),
            developer_response_received=bool(pipeline_trace.get("developer_response_received")),
            patch_proposal_parsed=bool(pipeline_trace.get("patch_proposal_parsed")),
            patch_protocol_accepted=bool(pipeline_trace.get("patch_protocol_accepted")),
            patch_applied=bool(pipeline_trace.get("patch_applied")),
            syntax_passed=bool(pipeline_trace.get("syntax_passed")),
            public_tests_selected=bool(pipeline_trace.get("public_tests_selected")),
            public_tests_executed=bool(pipeline_trace.get("public_tests_executed")),
            developer_public_tests_passed=bool(pipeline_trace.get("public_tests_passed")),
            reviewer_reached=bool(pipeline_trace.get("reviewer_reached")),
            judge_reached=bool(judge),
        )


def classify_pipeline_failure(outcome: EngineeringOutcome, stop_reason: str, reason: str) -> str | None:
    if outcome.success:
        return None
    folded = f"{stop_reason} {reason}".casefold()
    if "planning" in folded:
        return "planner_failure"
    if "budget" in folded or "max_model" in folded:
        return "budget_exhausted"
    if "timeout" in folded or "duration" in folded:
        return "timeout"
    if "patch_validation" in folded:
        return "patch_validation_failure"
    if "patch_application" in folded:
        return "patch_application_failure"
    if "all_routes_exhausted" in folded or "model_budget_exhausted" in folded:
        return "all_routes_exhausted"
    if "routing" in folded or "provider" in folded:
        return "routing_failure"
    if "review" in folded:
        return "review_failure"
    if "test" in folded or "validation" in folded:
        return "test_failure"
    if "safety" in folded or "scope" in folded or "unplanned" in folded:
        return "safety_violation"
    return "generation_failure"


def classify_developer_failure(reason: str) -> str:
    """Map a sanitized Developer failure to the first actionable downstream gate."""
    folded = str(reason or "").casefold()
    if "no_change" in folded or "duplicate_failed_attempt" in folded:
        return "PATCH_NO_EFFECT"
    if "syntax" in folded:
        return "SYNTAX_FAILURE"
    if "invalid_patch_response" in folded or "patch_contract:schema" in folded:
        return "PATCH_SCHEMA_FAILURE"
    if any(token in folded for token in ("unknown_target", "invalid_target", "out_of_scope", "chemin non autorisé")):
        return "PATCH_TARGET_FAILURE"
    if any(token in folded for token in ("provenance", "write_error", "stale_content")):
        return "PATCH_APPLICATION_FAILURE"
    if any(token in folded for token in ("relevance_", "quality_gate", "dependency_", "patch_validation")):
        return "PATCH_VALIDATION_FAILURE"
    if "chemins de tests" in folded or "test_selection" in folded:
        return "TEST_SELECTION_FAILURE"
    if "test" in folded or "pytest" in folded:
        return "PUBLIC_TEST_FAILURE"
    if "review" in folded:
        return "REVIEW_FAILURE"
    if "judge" in folded:
        return "JUDGE_FAILURE"
    if "generation_failed" in folded or "no response" in folded:
        return "DEVELOPER_NO_RESPONSE"
    if "invalid" in folded or "response" in folded:
        return "DEVELOPER_INVALID_RESPONSE"
    return "PIPELINE_PROVENANCE_FAILURE"


def hidden_pytest_evaluator(hidden_files: Mapping[str, str], *, semantic_check: Callable[[Path], bool] | None = None, timeout: float = 30.0) -> Callable[[Path, AgentOutcome], EvaluationOutcome]:
    """Run hidden tests outside the agent workspace with workspace on PYTHONPATH."""
    frozen = dict(hidden_files)
    def evaluate(workspace: Path, outcome: AgentOutcome) -> EvaluationOutcome:
        import tempfile
        import shutil
        hidden_root = Path(tempfile.mkdtemp(prefix="trusted_hidden_eval_"))
        try:
            for rel, content in frozen.items():
                target = (hidden_root / rel).resolve()
                target.relative_to(hidden_root.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            env = sanitized_child_environment()
            env["PYTHONPATH"] = str(workspace)
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-o", "addopts="], cwd=hidden_root,
                    capture_output=True, text=True, timeout=max(.1, timeout), env=env,
                )
                hidden_ok = completed.returncode == 0
                failure = None if hidden_ok else "hidden_test_failure"
            except subprocess.TimeoutExpired:
                hidden_ok, failure = False, "hidden_test_timeout"
            semantic_ok = bool(semantic_check(workspace)) if semantic_check else hidden_ok
            return EvaluationOutcome(
                public_tests_passed=outcome.public_tests_passed,
                hidden_tests_passed=hidden_ok,
                reviewer_accepted=outcome.reviewer_decision == "approve" if outcome.reviewer_decision else False,
                judge_accepted=outcome.judge_decision == "ACCEPT",
                semantic_success=semantic_ok,
                regression=False,
                correct_refusal=not outcome.claimed_success and not outcome.changed_files,
                failure_type=failure,
            )
        finally:
            shutil.rmtree(hidden_root, ignore_errors=True)
    return evaluate


def real_provider_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return any(bool(env.get(name)) for name in (
        "OMNIROUTE_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
        "CEREBRAS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OLLAMA_HOST",
    ))


def provider_configuration(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    return {
        "provider_mode": "real",
        "omniroute_configured": bool(env.get("OMNIROUTE_API_KEY")),
        "openrouter_configured": bool(env.get("OPENROUTER_API_KEY")),
        "groq_configured": bool(env.get("GROQ_API_KEY")),
        "cerebras_configured": bool(env.get("CEREBRAS_API_KEY")),
        "gemini_configured": bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")),
        "ollama_configured": bool(env.get("OLLAMA_HOST")),
        "omniroute_model": str(env.get("OMNIROUTE_MODEL") or "auto")[:120],
    }


def configuration_hash(configuration: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(configuration), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()
