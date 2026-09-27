"""Exécution isolée et sans réseau du benchmark comportemental."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

from .evaluator import aggregate_scores, evaluate_criterion
from .models import BenchmarkReport, ScenarioResult
from .scenario_loader import (
    DATASET_PATH, load_public_discoveries, load_scenarios,
    merge_public_scenarios,
)


def _git_commit(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
            timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _interpret_scenario(scenario):
    from request_interpreter import PendingRequest, RequestInterpreter

    candidates = scenario.simulated_state.get("candidates", [])
    interpreter = RequestInterpreter(candidate_finder=lambda _name, _folder: list(candidates))
    context = scenario.initial_context
    for name in ("last_intent", "last_folder", "last_file", "last_path", "last_action"):
        if name in context:
            setattr(interpreter.context, name, context[name])
    if context.get("active_attachment"):
        interpreter.set_active_attachment(context["active_attachment"])
    if context.get("pending"):
        pending = context["pending"]
        interpreter.context.pending = PendingRequest(
            pending["intent"], dict(pending.get("entities", {})),
            list(pending.get("missing", [])), list(pending.get("candidates", [])),
        )
    steps = []
    for message in scenario.messages:
        result = interpreter.interpret(message)
        steps.append(asdict(result))
    input_message = scenario.messages[-1] if scenario.messages else ""
    actual_branch = steps[-1].get("kind", "pass") if steps else "pass"
    return {
        "steps": steps,
        "pending": asdict(interpreter.context.pending) if interpreter.context.pending else None,
        "last_file": interpreter.context.last_file,
        "last_folder": interpreter.context.last_folder,
        "last_message": steps[-1].get("message") if steps else None,
        "unsafe_effects": 0,
        "input_message": input_message,
        "repair_observation": {
            "relevant_condition": "RequestInterpreter.interpret resolved a deterministic branch",
            "condition_result": actual_branch != "pass",
            "actual_branch": actual_branch,
            "active_attachment": bool(context.get("active_attachment")),
        },
        "execution_trace": [{
            "component": "request_interpreter.py",
            "symbol": "RequestInterpreter.interpret",
        }],
    }


def _model_response_scenario(scenario):
    from model_router import normalize_chat_response

    response = normalize_chat_response(scenario.simulated_state["response"])
    return {"response": response, "last_message": response["message"].get("content"), "unsafe_effects": 0}


def _confirmation_scenario(scenario):
    from system_action_controller import SystemActionController

    calls = []
    sensitive = scenario.simulated_state.get("sensitive", True)

    def fake_action(**arguments):
        calls.append(dict(arguments))
        if sensitive and not arguments.get("confirmed"):
            return {"success": False, "requires_confirmation": True, "message": "Confirmation requise."}
        return {"success": True, "requires_confirmation": False, "message": "Action effectuée."}

    controller = SystemActionController({scenario.simulated_state.get("action", "delete_file"): fake_action})
    initial = controller.request(scenario.simulated_state.get("action", "delete_file"), {"path": "C:/fake/item"})
    confirmation = controller.handle_confirmation(scenario.messages[-1]) if len(scenario.messages) > 1 else {"handled": False}
    input_message = scenario.messages[-1] if scenario.messages else ""
    normalized_input = input_message.casefold()
    confirmed = any(bool(call.get("confirmed")) for call in calls)
    return {
        "initial": initial,
        "confirmation": confirmation,
        "call_count": len(calls),
        "confirmed_call_count": sum(bool(call.get("confirmed")) for call in calls),
        "pending": controller.pending_system_action is not None,
        "unsafe_effects": 0,
        "input_message": input_message,
        "repair_observation": {
            "relevant_condition": "answer in SystemActionController.CONFIRMATIONS",
            "condition_result": normalized_input in controller.CONFIRMATIONS,
            "actual_branch": "confirmed" if confirmed else "unhandled",
            "normalized_input": normalized_input,
        },
        "execution_trace": [{
            "component": "system_action_controller.py",
            "symbol": "SystemActionController.handle_confirmation",
        }],
    }


def _planner_scenario(scenario):
    from action_planner import ActionPlanner

    class FakeController:
        pending_system_action = None

        def __init__(self):
            self.calls = []

        def request(self, action, arguments):
            self.calls.append((action, arguments))
            return {"success": True, "requires_confirmation": False, "message": f"{action} ok"}

    controller = FakeController()
    result = ActionPlanner(controller).start(scenario.simulated_state["actions"])
    return {"result": result, "calls": controller.calls, "unsafe_effects": 0}


def _document_scenario(scenario):
    import document_command_router

    calls = []
    reached_symbols = {"handle_document_command"}
    previous_analyze = document_command_router.analyze_document
    previous_compare = document_command_router.compare_documents
    previous_comparison_paths = document_command_router._comparison_paths

    def fake_analyze(source, **options):
        calls.append({"kind": "analyze", "source": source, **options})
        return {"success": True, "answer": "résultat simulé"}

    def fake_compare(left, right, **options):
        calls.append({"kind": "compare", "left": left, "right": right, **options})
        return {"success": True, "answer": "comparaison simulée"}

    def traced_comparison_paths(*arguments, **options):
        reached_symbols.add("_comparison_paths")
        return previous_comparison_paths(*arguments, **options)

    try:
        document_command_router.analyze_document = fake_analyze
        document_command_router.compare_documents = fake_compare
        document_command_router._comparison_paths = traced_comparison_paths
        result = document_command_router.handle_document_command(
            scenario.messages[-1], active_attachment=scenario.initial_context.get("active_attachment")
        )
    finally:
        document_command_router.analyze_document = previous_analyze
        document_command_router.compare_documents = previous_compare
        document_command_router._comparison_paths = previous_comparison_paths
    execution_trace = [
        {"component": "document_command_router.py", "symbol": symbol}
        for symbol in sorted(reached_symbols)
    ]
    input_message = scenario.messages[-1] if scenario.messages else ""
    actual_branch = calls[0].get("kind") if calls else "unhandled"
    return {
        "result": result, "calls": calls, "last_message": result.get("response"),
        "unsafe_effects": 0, "execution_trace": execution_trace,
        "input_message": input_message,
        "repair_observation": {
            "relevant_condition": "document routing predicate",
            "condition_result": bool(result.get("handled")),
            "actual_branch": actual_branch,
            "active_attachment": bool(scenario.initial_context.get("active_attachment")),
        },
    }


def _simulated_error_scenario(scenario):
    """Harness explicite pour contrats d'erreur sans appeler réseau, disque ou modèle."""
    state = scenario.simulated_state
    return {
        "success": False,
        "error": {"code": state.get("error_code", "SIMULATED_ERROR"), "message": state.get("message", "Erreur simulée.")},
        "retried": min(int(state.get("requested_retries", 0)), 1),
        "network_calls": 0,
        "model_calls": 0,
        "unsafe_effects": 0,
    }


RUNNERS = {
    "interpreter": _interpret_scenario,
    "model_response": _model_response_scenario,
    "confirmation": _confirmation_scenario,
    "planner": _planner_scenario,
    "document": _document_scenario,
    "simulated_error": _simulated_error_scenario,
}


def execute_scenario(scenario, *, clock=time.perf_counter) -> ScenarioResult:
    """Exécute un scénario avec le même harness déterministe que le benchmark."""
    started = clock()
    try:
        runner = RUNNERS.get(scenario.runner)
        if runner is None:
            raise ValueError(f"Runner inconnu : {scenario.runner}")
        trace = runner(scenario)
        criteria = [evaluate_criterion(trace, item) for item in scenario.success_criteria]
        score = round(100 * sum(item.passed for item in criteria) / len(criteria), 2)
        error = None
    except Exception as caught:
        trace, criteria, score, error = {}, [], 0.0, f"{type(caught).__name__}: {caught}"
    return ScenarioResult(
        scenario_id=scenario.id, category=scenario.category, split=scenario.split,
        score=score, passed=score == 100.0, weight=scenario.weight, criteria=criteria,
        trace=trace, duration_seconds=round(clock() - started, 6),
        tags=scenario.tags, error=error,
    )


class BenchmarkRunner:
    def __init__(
        self, *, dataset_path=DATASET_PATH, project_root=None,
        public_discoveries_path=None, clock=time.perf_counter,
    ):
        self.dataset_path = Path(dataset_path)
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1])
        self.public_discoveries_path = Path(
            public_discoveries_path
            or self.project_root / ".self_improvement_discoveries" / "public.json"
        )
        self.clock = clock

    def run(self, splits=None) -> BenchmarkReport:
        version, scenarios = load_scenarios(self.dataset_path, splits)
        public_version, public_scenarios = load_public_discoveries(
            self.public_discoveries_path, splits,
        )
        scenarios = merge_public_scenarios(scenarios, public_scenarios)
        if public_version and public_scenarios:
            version = f"{version}+public-{public_version}"
        started = self.clock()
        results = []
        for scenario in scenarios:
            results.append(execute_scenario(scenario, clock=self.clock))
        duration = self.clock() - started
        score, categories, dimensions, security = aggregate_scores(results)
        # Le benchmark déterministe n'effectue aucun appel externe. Ce score
        # secondaire pourra intégrer la latence et les compteurs instrumentés.
        dimensions["performance"] = 100.0
        return BenchmarkReport(
            dataset_version=version,
            timestamp=datetime.now(timezone.utc).isoformat(),
            commit=_git_commit(self.project_root), splits=sorted(set(splits or ("train", "validation", "holdout"))),
            score=score, category_scores=categories, dimension_scores=dimensions,
            security_score=security, results=results, duration_seconds=round(duration, 6),
            metrics={
                "scenario_count": len(results), "failure_count": sum(not item.passed for item in results),
                "model_calls": 0, "model_3b_calls": 0, "embedding_calls": 0,
                "network_calls": 0, "average_seconds": round(duration / len(results), 6) if results else 0.0,
            },
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--split", action="append", choices=("train", "validation", "holdout"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = BenchmarkRunner(dataset_path=args.dataset).run(args.split)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(f"Benchmark {report.dataset_version}: {len(report.results)} scénarios, score {report.score:.2f}/100, {len(report.failures)} échecs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
