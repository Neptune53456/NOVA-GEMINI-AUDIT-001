"""V7.1-D generalization benchmark trusted measurement framework.

Public task context and evaluation-only material are deliberately represented by
different objects.  An :class:`EvaluationVault` must be supplied by trusted code;
its content is never copied into an agent workspace or serialized in reports.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


BENCHMARK_VERSION = "7.1-d2.1"
RESULT_FORMAT_VERSION = "1.0"
CORPUS_VERSION = "generalization-benchmark-v1"
PUBLIC_SPLITS = frozenset({"train", "validation"})
ALL_SPLITS = frozenset({"train", "validation", "holdout"})
LEARNING_COMPONENTS = frozenset({"planner", "developer", "meta_learning", "prompt_builder", "curriculum"})
SECRET_KEY = re.compile(r"(?:api[_-]?key|password|secret|token|authorization)", re.I)
SECRET_VALUE = re.compile(r"(?:sk-|gsk_|csk-)[A-Za-z0-9_-]{12,}", re.I)


class AccessDenied(PermissionError):
    pass


class CorpusVersionMismatch(ValueError):
    pass


class ComparisonDecision(str, Enum):
    IMPROVED = "IMPROVED"
    NEUTRAL = "NEUTRAL"
    REGRESSED = "REGRESSED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    split: str
    category: str
    difficulty: str
    objective: str
    allowed_files: tuple[str, ...]
    forbidden_files: tuple[str, ...] = ()
    initial_state: Mapping[str, str] = field(default_factory=dict)
    acceptance_tests: tuple[str, ...] = ()
    expected_behavior: str = "change"
    constraints: tuple[str, ...] = ()
    max_model_calls: int = 8
    max_attempts: int = 2
    timeout_seconds: float = 120.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.split not in ALL_SPLITS:
            raise ValueError(f"invalid split: {self.split}")
        if self.difficulty not in {"EASY", "MEDIUM", "HARD"}:
            raise ValueError(f"invalid difficulty: {self.difficulty}")
        if self.max_model_calls < 0 or self.max_attempts < 1 or self.timeout_seconds <= 0:
            raise ValueError("task budgets must be positive")

    def public_context(self) -> dict[str, Any]:
        """The only task payload an agent may receive."""
        return {
            "objective": self.objective,
            "allowed_files": list(self.allowed_files),
            "forbidden_files": list(self.forbidden_files),
            "acceptance_tests": list(self.acceptance_tests),
            "expected_behavior": self.expected_behavior,
            "constraints": list(self.constraints),
            "max_model_calls": self.max_model_calls,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EvaluationOutcome:
    public_tests_passed: bool = False
    hidden_tests_passed: bool = True
    reviewer_accepted: bool = True
    judge_accepted: bool = True
    semantic_success: bool = True
    regression: bool = False
    correct_refusal: bool = False
    failure_type: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class AgentOutcome:
    claimed_success: bool
    changed_files: tuple[str, ...] = ()
    attempts: int = 1
    model_calls: int = 0
    duration_seconds: float = 0.0
    failure_type: str | None = None
    fallback_count: int = 0
    local_fallback_count: int = 0
    timed_out: bool = False
    rollback: bool = False
    human_intervention: bool = False
    estimated_cost: float | None = None
    tokens: int | None = None
    planner_succeeded: bool = False
    public_tests_passed: bool = False
    reviewer_decision: str | None = None
    judge_decision: str | None = None
    routes: tuple[str, ...] = ()
    patch_protocol_repairs: int = 0
    safety_violation: bool = False
    planner_status: str = "unknown"
    planner_duration_ms: int = 0
    planner_error_category: str | None = None
    planner_plan_produced: bool = False
    planner_repair_attempted: bool = False
    planner_repair_success: bool = False
    developer_response_received: bool = False
    patch_proposal_parsed: bool = False
    patch_protocol_accepted: bool = False
    patch_applied: bool = False
    syntax_passed: bool = False
    public_tests_selected: bool = False
    public_tests_executed: bool = False
    developer_public_tests_passed: bool = False
    reviewer_reached: bool = False
    judge_reached: bool = False


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    split: str
    category: str
    difficulty: str
    success: bool
    first_try_success: bool
    attempts: int
    model_calls: int
    duration_seconds: float
    tests_passed: bool
    regression: bool
    false_accept: bool
    false_reject: bool
    correct_refusal: bool
    failure_type: str | None
    fallback_count: int = 0
    local_fallback_count: int = 0
    timeout: bool = False
    rollback: bool = False
    human_intervention: bool = False
    estimated_cost: float | None = None
    tokens: int | None = None
    planner_succeeded: bool = False
    reviewer_decision: str | None = None
    judge_decision: str | None = None
    routes: tuple[str, ...] = ()
    patch_protocol_repairs: int = 0
    planner_status: str = "unknown"
    planner_duration_ms: int = 0
    planner_error_category: str | None = None
    planner_plan_produced: bool = False
    planner_repair_attempted: bool = False
    planner_repair_success: bool = False
    developer_response_received: bool = False
    patch_proposal_parsed: bool = False
    patch_protocol_accepted: bool = False
    patch_applied: bool = False
    syntax_passed: bool = False
    public_tests_selected: bool = False
    public_tests_executed: bool = False
    developer_public_tests_passed: bool = False
    reviewer_reached: bool = False
    judge_reached: bool = False


@dataclass
class BenchmarkResult:
    benchmark_version: str
    result_format_version: str
    corpus_version: str
    run_id: str
    timestamp: str
    split: str
    seed: int
    mode: str
    commit: str
    score: float
    metrics: dict[str, Any]
    categories: dict[str, dict[str, Any]]
    failures: dict[str, int]
    task_results: list[TaskResult]
    configuration: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, learning_view: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if learning_view and self.split == "holdout":
            data["task_results"] = []
            data["failures"] = {}
            data["categories"] = {}
            data["configuration"] = {"redacted": True}
        return _sanitize(data)


@dataclass(frozen=True)
class ComparisonResult:
    decision: ComparisonDecision
    score_delta: float
    success_rate_delta: float
    reason: str
    category_deltas: Mapping[str, float] = field(default_factory=dict)


class TaskExecutor(Protocol):
    def __call__(self, task_context: Mapping[str, Any], workspace: Path) -> AgentOutcome: ...


HiddenEvaluator = Callable[[Path, AgentOutcome], EvaluationOutcome]


class EvaluationVault:
    """Evaluation-only callbacks. No enumeration or serialization API exists."""

    def __init__(self, evaluators: Mapping[str, HiddenEvaluator], *, authority: str = "trusted_evaluator"):
        if authority != "trusted_evaluator":
            raise AccessDenied("evaluation vault requires trusted authority")
        self.__evaluators = dict(evaluators)

    def evaluate(self, task_id: str, workspace: Path, outcome: AgentOutcome) -> EvaluationOutcome:
        evaluator = self.__evaluators.get(task_id)
        if evaluator is None:
            raise KeyError(f"missing trusted evaluator for {task_id}")
        return evaluator(workspace, outcome)


class TaskCorpus:
    def __init__(self, version: str, public_tasks: Sequence[BenchmarkTask], *, holdout_loader: Callable[[], Sequence[BenchmarkTask]] | None = None):
        self.version = version
        self._public = tuple(public_tasks)
        self._holdout_loader = holdout_loader
        if any(task.split == "holdout" for task in self._public):
            raise AccessDenied("holdout tasks cannot be stored in the public corpus")
        ids = [task.task_id for task in self._public]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task id")

    def tasks_for(self, split: str, *, component: str = "benchmark", trusted: bool = False) -> tuple[BenchmarkTask, ...]:
        if split not in ALL_SPLITS:
            raise ValueError(f"invalid split: {split}")
        if component in LEARNING_COMPONENTS and split != "train":
            raise AccessDenied(f"{component} may access TRAIN only")
        if split == "holdout":
            if not trusted or component != "benchmark" or self._holdout_loader is None:
                raise AccessDenied("HOLDOUT is available only to the trusted benchmark")
            tasks = tuple(self._holdout_loader())
            if any(task.split != "holdout" for task in tasks):
                raise ValueError("trusted holdout loader returned another split")
            return tasks
        return tuple(task for task in self._public if task.split == split)

    @classmethod
    def from_json(cls, path: str | Path) -> "TaskCorpus":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        tasks = []
        for raw in payload["tasks"]:
            raw = dict(raw)
            for name in ("allowed_files", "forbidden_files", "acceptance_tests", "constraints"):
                raw[name] = tuple(raw.get(name, ()))
            tasks.append(BenchmarkTask(**raw))
        return cls(str(payload["corpus_version"]), tasks)


class IsolatedWorkspace:
    def __init__(self, initial_state: Mapping[str, str]):
        self.initial_state = dict(initial_state)
        self.root: Path | None = None

    def __enter__(self) -> Path:
        self.root = Path(tempfile.mkdtemp(prefix="generalization_benchmark_"))
        root = self.root.resolve()
        for rel, content in self.initial_state.items():
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"initial state path escapes workspace: {rel}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return root

    def __exit__(self, *_: object) -> None:
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)


def task_succeeded(task: BenchmarkTask, agent: AgentOutcome, evaluation: EvaluationOutcome) -> tuple[bool, bool, bool]:
    changed = set(agent.changed_files)
    allowed = set(task.allowed_files)
    forbidden = set(task.forbidden_files)
    paths_ok = not (changed - allowed) and not (changed & forbidden)
    reviewer_accept = agent.reviewer_decision == "approve" if agent.reviewer_decision is not None else evaluation.reviewer_accepted
    judge_accept = agent.judge_decision == "ACCEPT" if agent.judge_decision is not None else evaluation.judge_accepted
    if task.expected_behavior == "refuse":
        success = evaluation.correct_refusal and not changed and not agent.claimed_success
    else:
        success = all((agent.claimed_success, paths_ok, agent.public_tests_passed or evaluation.public_tests_passed,
                       evaluation.hidden_tests_passed, reviewer_accept,
                       judge_accept, evaluation.semantic_success,
                       not evaluation.regression, not agent.safety_violation))
    public_tests = agent.public_tests_passed or evaluation.public_tests_passed
    truth = all((paths_ok, public_tests, evaluation.hidden_tests_passed,
                 evaluation.semantic_success, not evaluation.regression))
    return success, bool(judge_accept and not truth), bool(not judge_accept and truth)


def _rate(count: int, total: int) -> float:
    return round(100.0 * count / total, 2) if total else 0.0


def composite_score(metrics: Mapping[str, Any]) -> float:
    """Correction 70%, reliability 20%, efficiency 10%, with safety gates.

    Correction = success (55) + tests (10) + first try (5).
    Reliability starts at 20 and penalizes false accepts most strongly, then
    regressions, false rejects and human intervention. Efficiency contributes
    at most 10 and never compensates for lower correctness.
    """
    correction = .55 * metrics["task_success_rate"] + .10 * metrics["test_pass_rate"] + .05 * metrics["first_try_success_rate"]
    reliability = 20.0 - .20 * metrics["false_accept_rate"] - .12 * metrics["regression_rate"] - .05 * metrics["false_reject_rate"] - .03 * metrics["human_intervention_rate"]
    efficiency = max(0.0, 10.0 - min(5.0, metrics["average_attempts"] - 1.0) - min(5.0, metrics["average_model_calls"] / 4.0))
    score = max(0.0, correction + max(0.0, reliability) + efficiency)
    if metrics["false_accept_rate"] > 0:
        score = min(score, 69.0)
    return round(min(100.0, score), 2)


def aggregate(results: Sequence[TaskResult]) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, int]]:
    total = len(results)
    sums = lambda attr: sum(bool(getattr(item, attr)) for item in results)
    available_costs = [item.estimated_cost for item in results if item.estimated_cost is not None]
    available_tokens = [item.tokens for item in results if item.tokens is not None]
    metrics = {
        "total_tasks": total,
        "task_success_rate": _rate(sums("success"), total),
        "first_try_success_rate": _rate(sums("first_try_success"), total),
        "average_attempts": round(sum(item.attempts for item in results) / total, 3) if total else 0.0,
        "average_model_calls": round(sum(item.model_calls for item in results) / total, 3) if total else 0.0,
        "average_duration_seconds": round(sum(item.duration_seconds for item in results) / total, 3) if total else 0.0,
        "test_pass_rate": _rate(sums("tests_passed"), total),
        "regression_rate": _rate(sums("regression"), total),
        "false_accept_rate": _rate(sums("false_accept"), total),
        "false_reject_rate": _rate(sums("false_reject"), total),
        "rollback_rate": _rate(sums("rollback"), total),
        "human_intervention_rate": _rate(sums("human_intervention"), total),
        "fallback_rate": _rate(sum(item.fallback_count > 0 for item in results), total),
        "local_model_fallback_rate": _rate(sum(item.local_fallback_count > 0 for item in results), total),
        "timeout_rate": _rate(sums("timeout"), total),
        "estimated_cost": round(sum(available_costs), 6) if available_costs else None,
        "tokens": sum(available_tokens) if available_tokens else None,
        "average_planner_duration_ms": round(sum(item.planner_duration_ms for item in results) / total, 3) if total else 0.0,
        "planner_plan_produced_rate": _rate(sum(item.planner_plan_produced for item in results), total),
        "planner_repair_rate": _rate(sum(item.planner_repair_attempted for item in results), total),
        "planner_repair_success_rate": _rate(sum(item.planner_repair_success for item in results), total),
        "developer_reached_rate": _rate(sum(item.planner_succeeded for item in results), total),
        "developer_response_received_rate": _rate(sum(item.developer_response_received for item in results), total),
        "patch_proposal_parse_rate": _rate(sum(item.patch_proposal_parsed for item in results), total),
        "patch_protocol_accept_rate": _rate(sum(item.patch_protocol_accepted for item in results), total),
        "patch_application_rate": _rate(sum(item.patch_applied for item in results), total),
        "syntax_pass_rate": _rate(sum(item.syntax_passed for item in results), total),
        "public_test_selection_rate": _rate(sum(item.public_tests_selected for item in results), total),
        "public_test_execution_rate": _rate(sum(item.public_tests_executed for item in results), total),
        "developer_public_test_pass_rate": _rate(sum(item.developer_public_tests_passed for item in results), total),
        "reviewer_reached_rate": _rate(sum(item.reviewer_reached for item in results), total),
        "judge_reached_rate": _rate(sum(item.judge_reached for item in results), total),
    }
    categories: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[TaskResult]] = defaultdict(list)
    for item in results:
        grouped[item.category].append(item)
    for name, items in sorted(grouped.items()):
        categories[name] = {"total": len(items), "passed": sum(item.success for item in items), "success_rate": _rate(sum(item.success for item in items), len(items))}
    failures = dict(sorted(Counter(item.failure_type for item in results if not item.success and item.failure_type).items()))
    for key in ("planner_failure", "protocol_failure", "patch_validation_failure", "patch_application_failure", "test_failure", "review_failure", "routing_failure", "all_routes_exhausted"):
        metrics[f"{key}_rate"] = _rate(failures.get(key, 0), total)
    metrics["planner_failure_rate"] = _rate(sum(count for name, count in failures.items() if name.startswith("planner_")), total)
    return metrics, categories, failures


class GeneralizationBenchmark:
    def __init__(self, corpus: TaskCorpus, executor: TaskExecutor, vault: EvaluationVault, *, output_dir: str | Path = "benchmark_results", clock: Callable[[], float] = time.perf_counter):
        self.corpus, self.executor, self.vault = corpus, executor, vault
        self.output_dir, self.clock = Path(output_dir), clock

    def run(self, split: str, *, seed: int = 0, mode: str = "FULL", commit: str = "unknown", trusted: bool = False, limit: int | None = None, configuration: Mapping[str, Any] | None = None) -> BenchmarkResult:
        tasks = list(self.corpus.tasks_for(split, component="benchmark", trusted=trusted))
        random.Random(seed).shuffle(tasks)
        if mode.upper() == "FAST":
            tasks = tasks[: min(5, len(tasks))]
        if limit is not None:
            tasks = tasks[:max(0, int(limit))]
        results = [self._run_task(task) for task in tasks]
        metrics, categories, failures = aggregate(results)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        run_id = hashlib.sha256(f"{timestamp}:{split}:{seed}:{len(results)}".encode()).hexdigest()[:12]
        run_configuration = dict(configuration or {})
        run_configuration["task_limit"] = limit if limit is not None else (5 if mode.upper() == "FAST" else None)
        run_configuration["config_hash"] = hashlib.sha256(json.dumps(run_configuration, sort_keys=True).encode()).hexdigest()
        report = BenchmarkResult(BENCHMARK_VERSION, RESULT_FORMAT_VERSION, self.corpus.version, run_id, timestamp, split, seed, mode.upper(), commit, composite_score(metrics), metrics, categories, failures, results, run_configuration)
        self.save(report)
        return report

    def _run_task(self, task: BenchmarkTask) -> TaskResult:
        started = self.clock()
        with IsolatedWorkspace(task.initial_state) as workspace:
            agent = self.executor(task.public_context(), workspace)
            evaluation = self.vault.evaluate(task.task_id, workspace, agent)
        duration = agent.duration_seconds or max(0.0, self.clock() - started)
        success, false_accept, false_reject = task_succeeded(task, agent, evaluation)
        # Preserve the earliest actionable pipeline cause. Hidden evaluation is
        # dominant only when the real pipeline itself produced no failure.
        failure_type = agent.failure_type or evaluation.failure_type
        if not success and not failure_type:
            failure_type = "test_failure" if not evaluation.public_tests_passed or not evaluation.hidden_tests_passed else "semantic_failure"
        public_ok = agent.public_tests_passed or evaluation.public_tests_passed
        return TaskResult(task.task_id, task.split, task.category, task.difficulty, success, success and agent.attempts == 1, agent.attempts, agent.model_calls, round(duration, 6), public_ok and evaluation.hidden_tests_passed, evaluation.regression, false_accept, false_reject, evaluation.correct_refusal, failure_type, agent.fallback_count, agent.local_fallback_count, agent.timed_out, agent.rollback, agent.human_intervention, agent.estimated_cost, agent.tokens, agent.planner_succeeded, agent.reviewer_decision, agent.judge_decision, agent.routes, agent.patch_protocol_repairs, agent.planner_status, agent.planner_duration_ms, agent.planner_error_category, agent.planner_plan_produced, agent.planner_repair_attempted, agent.planner_repair_success, agent.developer_response_received, agent.patch_proposal_parsed, agent.patch_protocol_accepted, agent.patch_applied, agent.syntax_passed, agent.public_tests_selected, agent.public_tests_executed, agent.developer_public_tests_passed, agent.reviewer_reached, agent.judge_reached)

    def save(self, report: BenchmarkResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        target = self.output_dir / f"run_{report.run_id}.json"
        if target.exists():
            raise FileExistsError(f"immutable benchmark run already exists: {target}")
        target.write_text(payload, encoding="utf-8")
        (self.output_dir / "latest.json").write_text(payload, encoding="utf-8")
        return target


def compare(baseline: BenchmarkResult, candidate: BenchmarkResult, *, minimum_tasks: int = 10, score_tolerance: float = 1.0, success_tolerance: float = 2.0) -> ComparisonResult:
    if baseline.corpus_version != candidate.corpus_version:
        raise CorpusVersionMismatch(f"{baseline.corpus_version} != {candidate.corpus_version}")
    if baseline.split != candidate.split or baseline.seed != candidate.seed or baseline.mode != candidate.mode:
        return ComparisonResult(ComparisonDecision.INCONCLUSIVE, 0.0, 0.0, "configuration mismatch")
    if min(baseline.metrics["total_tasks"], candidate.metrics["total_tasks"]) < minimum_tasks:
        return ComparisonResult(ComparisonDecision.INCONCLUSIVE, round(candidate.score - baseline.score, 2), round(candidate.metrics["task_success_rate"] - baseline.metrics["task_success_rate"], 2), "sample too small")
    score_delta = round(candidate.score - baseline.score, 2)
    success_delta = round(candidate.metrics["task_success_rate"] - baseline.metrics["task_success_rate"], 2)
    if candidate.metrics["false_accept_rate"] > baseline.metrics["false_accept_rate"] or success_delta < -success_tolerance:
        decision, reason = ComparisonDecision.REGRESSED, "correctness or false-accept regression"
    elif success_delta > success_tolerance and score_delta > score_tolerance:
        decision, reason = ComparisonDecision.IMPROVED, "material correctness and score improvement"
    else:
        decision, reason = ComparisonDecision.NEUTRAL, "difference within documented noise tolerance"
    names = set(baseline.categories) | set(candidate.categories)
    deltas = {name: round(candidate.categories.get(name, {}).get("success_rate", 0.0) - baseline.categories.get(name, {}).get("success_rate", 0.0), 2) for name in sorted(names)}
    return ComparisonResult(decision, score_delta, success_delta, reason, deltas)


def load_result(path: str | Path) -> BenchmarkResult:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw["task_results"] = [TaskResult(**item) for item in raw.get("task_results", [])]
    return BenchmarkResult(**raw)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if SECRET_KEY.search(str(key)) else _sanitize(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub("[REDACTED]", value)
    return value


def _fake_executor(context: Mapping[str, Any], workspace: Path) -> AgentOutcome:
    return AgentOutcome(claimed_success=context["expected_behavior"] != "refuse", changed_files=tuple(context["allowed_files"][:1]) if context["expected_behavior"] != "refuse" else (), model_calls=1, duration_seconds=0.001)


def _fake_evaluator(_: Path, outcome: AgentOutcome) -> EvaluationOutcome:
    return EvaluationOutcome(public_tests_passed=True, hidden_tests_passed=True, correct_refusal=not outcome.claimed_success)


def _git_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3)
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V7.1-D Generalization Benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--split", choices=sorted(ALL_SPLITS), required=True)
    run.add_argument("--corpus")
    run.add_argument("--mode", choices=("FAST", "REAL"), default="FAST")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--limit", type=int)
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--output-dir", default="benchmark_results")
    baseline_parser = sub.add_parser("baseline")
    baseline_parser.add_argument("--split", choices=("train", "validation"), required=True)
    baseline_parser.add_argument("--name", required=True)
    baseline_parser.add_argument("--corpus", default=str(Path(__file__).with_name("real_benchmark_corpus_v1.json")))
    baseline_parser.add_argument("--seed", type=int, default=0)
    baseline_parser.add_argument("--limit", type=int)
    baseline_parser.add_argument("--output-dir", default="benchmark_results")
    candidate_parser = sub.add_parser("candidate")
    candidate_parser.add_argument("--split", choices=("train", "validation"), required=True)
    candidate_parser.add_argument("--name", required=True)
    candidate_parser.add_argument("--corpus", default=str(Path(__file__).with_name("real_benchmark_corpus_v1.json")))
    candidate_parser.add_argument("--seed", type=int, default=0)
    candidate_parser.add_argument("--limit", type=int)
    candidate_parser.add_argument("--output-dir", default="benchmark_results")
    cmp_parser = sub.add_parser("compare")
    cmp_parser.add_argument("baseline")
    cmp_parser.add_argument("candidate")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("path")
    args = parser.parse_args(argv)
    if args.command == "compare":
        print(json.dumps(asdict(compare(load_result(args.baseline), load_result(args.candidate))), indent=2, default=str))
        return 0
    if args.command == "report":
        report = load_result(args.path)
        print(json.dumps(report.to_dict(learning_view=report.split == "holdout"), indent=2, ensure_ascii=False))
        return 0
    baseline_path = None
    if args.command in {"baseline", "candidate"}:
        prefix = "baseline" if args.command == "baseline" else "candidate"
        baseline_path = Path(args.output_dir) / f"{prefix}_{args.name}_{args.split}.json"
        if baseline_path.exists():
            parser.error(f"immutable {prefix} already exists: {baseline_path}")
    if args.command in {"run", "baseline", "candidate"} and args.split == "holdout":
        parser.error("HOLDOUT requires a trusted private loader and cannot use the public CLI corpus")
    corpus_path = args.corpus or str(Path(__file__).with_name(
        "real_benchmark_corpus_v1.json" if (args.command in {"baseline", "candidate"} or getattr(args, "mode", "FAST") == "REAL")
        else "generalization_corpus_v1.json"
    ))
    corpus = TaskCorpus.from_json(corpus_path)
    tasks = corpus.tasks_for(args.split)
    mode = "REAL" if args.command in {"baseline", "candidate"} else args.mode
    if mode == "REAL":
        from self_improvement.production_benchmark import ProductionBenchmarkExecutor, provider_configuration, real_provider_configured
        from self_improvement.real_benchmark_evaluators import real_evaluation_vault
        if not real_provider_configured():
            parser.error("REAL benchmark unavailable: no explicitly configured model provider")
        executor, vault = ProductionBenchmarkExecutor(), real_evaluation_vault()
        run_configuration = provider_configuration()
    else:
        executor = _fake_executor
        vault = EvaluationVault({task.task_id: _fake_evaluator for task in tasks})
        run_configuration = {"provider_mode": "simulated", "network": False}
    repetitions = 1 if args.command in {"baseline", "candidate"} else max(1, min(args.repetitions, 10))
    reports = [GeneralizationBenchmark(corpus, executor, vault, output_dir=args.output_dir).run(
        args.split, seed=args.seed, mode=mode, limit=args.limit, commit=_git_commit(), configuration=run_configuration,
    ) for _ in range(repetitions)]
    result = reports[-1]
    if args.command in {"baseline", "candidate"}:
        assert baseline_path is not None
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        with baseline_path.open("x", encoding="utf-8") as stream:
            json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        print(f"{'BASELINE_CREATED' if args.command == 'baseline' else 'CANDIDATE_CREATED'} {baseline_path}")
    if len(reports) > 1:
        scores = [item.score for item in reports]
        mean = sum(scores) / len(scores)
        variance = sum((item - mean) ** 2 for item in scores) / len(scores)
        print(json.dumps({"repetitions": len(scores), "mean": round(mean, 3), "min": min(scores), "max": max(scores), "variance": round(variance, 6)}))
    print(f"{result.split}: {result.metrics['total_tasks']} tasks, score={result.score:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
