"""CLI de benchmark et boucle autonome bornée d'amélioration.

Exemples :
    python -m self_improvement.improvement_loop --benchmark-only
    python -m self_improvement.improvement_loop --cycles 3 --dry-run --report
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .benchmark_runner import BenchmarkRunner
from .codex_runner import (
    CodexRunner,
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    DEFAULT_SILENCE_WARNING_SECONDS,
)
from .evaluator import aggregate_scores, decide_acceptance
from .failure_analyzer import generate_improvement_task
from .git_manager import GitError, GitWorktreeManager
from .git_preflight import GitPreflight, GitPreflightError
from .models import BenchmarkReport, CriterionResult, ScenarioResult
from .reporting import write_baseline, write_cycle_report
from .repair_engine import SelfRepairEngine
from .scenario_lab import ScenarioLab, load_discovered
from .scenario_loader import load_scenarios


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_IMPROVEMENT = 0.5
DEFAULT_TARGET_SCORE = 98.0


class _LocalCycleComplete(Exception):
    """Signal interne : le cycle local est terminé sans lancer Codex."""


def _report_from_dict(data: dict) -> BenchmarkReport:
    results = []
    for item in data["results"]:
        criteria = [CriterionResult(**criterion) for criterion in item.get("criteria", [])]
        allowed = {field.name for field in fields(ScenarioResult)}
        values = {key: value for key, value in item.items() if key in allowed}
        values["criteria"] = criteria
        results.append(ScenarioResult(**values))
    allowed = {field.name for field in fields(BenchmarkReport)}
    values = {key: value for key, value in data.items() if key in allowed}
    values["results"] = results
    return BenchmarkReport(**values)


def _coverage_threshold(root: Path) -> float:
    match = re.search(r"--cov-fail-under=(\d+(?:\.\d+)?)", (root / "pyproject.toml").read_text(encoding="utf-8"))
    return float(match.group(1)) if match else 0.0


def _parse_coverage(output: str) -> float | None:
    matches = re.findall(r"^TOTAL\s+.*?([0-9]+(?:\.[0-9]+)?)%\s*$", output, flags=re.MULTILINE)
    return float(matches[-1]) if matches else None


def _run_tests(root: Path, timeout_seconds: int):
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "pytest"]
    started = time.monotonic()
    completed = subprocess.run(
        command, cwd=root, capture_output=True, text=True,
        timeout=timeout_seconds, env=environment,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": command,
        "coverage": _parse_coverage(output),
        "failed_tests": re.findall(r"(?m)^FAILED\s+(.+?)(?:\s+-\s+.*)?$", output),
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": output[-8000:],
    }


def _run_targeted_tests(root: Path, changed_files: list[str], timeout_seconds: int):
    mapping = {
        "system_action_controller.py": ["test_controllers_unit.py", "test_action_planner.py"],
        "request_interpreter.py": ["test_contextual_requests.py"],
        "document_command_router.py": ["test_document_command_router.py"],
    }
    selected = sorted({test for path in changed_files for test in mapping.get(path, [])})
    if not selected:
        return {"passed": False, "returncode": 2, "output_tail": "Aucun test ciblé autorisé."}
    command = [sys.executable, "-m", "pytest", *selected, "-q", "--no-cov"]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=root, capture_output=True, text=True, timeout=timeout_seconds,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return {
        "passed": completed.returncode == 0, "returncode": completed.returncode,
        "command": command,
        "failed_tests": re.findall(r"(?m)^FAILED\s+(.+?)(?:\s+-\s+.*)?$", output),
        "stdout": (completed.stdout or "")[-4000:], "stderr": (completed.stderr or "")[-4000:],
        "duration_seconds": round(time.monotonic() - started, 3), "output_tail": output[-4000:],
    }


def _compile_candidate(root: Path, timeout_seconds: int, changed_files=None):
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    selected = [str(root / path) for path in (changed_files or []) if str(path).endswith(".py")]
    command = [sys.executable, "-m", "py_compile", *selected] if selected else [
        sys.executable, "-c", "from run_tests import compile_project; compile_project()",
    ]
    completed = subprocess.run(
        command,
        cwd=root, capture_output=True, text=True, timeout=timeout_seconds, env=environment,
    )
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "output_tail": ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-4000:],
    }


def _run_representative_benchmark(worktree: Path, scenario_ids, timeout_seconds: int, *, scenarios=None):
    """Exécute exclusivement les scénarios TRAIN publics sélectionnés."""
    if scenarios is None:
        _version, scenarios = load_scenarios(splits=["train"])
    identifiers = set(scenario_ids)
    selected = [item for item in scenarios if item.split == "train" and item.id in identifiers]
    results = _run_external_scenarios(worktree, selected, timeout_seconds) if selected else []
    score, categories, dimensions, security = aggregate_scores(results)
    return BenchmarkReport(
        "representative-public-train", "", "candidate", ["train"], score,
        categories, dimensions, security, results,
        sum(item.duration_seconds for item in results),
        {"scenario_count": len(results), "failure_count": sum(not item.passed for item in results)},
    )


def _run_candidate_benchmark(
    worktree: Path, timeout_seconds: int, *, progress=lambda _split: None,
    dynamic_scenarios=None,
) -> BenchmarkReport:
    descriptor, name = tempfile.mkstemp(prefix="self_improvement_candidate_", suffix=".json")
    os.close(descriptor)
    output = Path(name)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(worktree)
    try:
        reports = []
        for split in ("train", "validation", "holdout"):
            progress(split)
            completed = subprocess.run(
                [
                    sys.executable, "-m", "self_improvement.benchmark_runner",
                    "--split", split, "--output", str(output),
                ],
                cwd=worktree, capture_output=True, text=True,
                timeout=timeout_seconds, env=environment,
            )
            if completed.returncode:
                raise RuntimeError((completed.stderr or completed.stdout)[-4000:])
            reports.append(_report_from_dict(json.loads(output.read_text(encoding="utf-8"))))
        results = [result for report in reports for result in report.results]
        if dynamic_scenarios:
            results.extend(_run_external_scenarios(worktree, dynamic_scenarios, timeout_seconds))
        score, categories, dimensions, security = aggregate_scores(results)
        dimensions["performance"] = 100.0
        duration = sum(report.duration_seconds for report in reports)
        return BenchmarkReport(
            dataset_version=reports[0].dataset_version,
            timestamp=reports[-1].timestamp,
            commit=reports[0].commit,
            splits=["holdout", "train", "validation"],
            score=score, category_scores=categories, dimension_scores=dimensions,
            security_score=security, results=results, duration_seconds=round(duration, 6),
            metrics={
                "scenario_count": len(results),
                "failure_count": sum(not result.passed for result in results),
                "model_calls": sum(report.metrics.get("model_calls", 0) for report in reports),
                "model_3b_calls": sum(report.metrics.get("model_3b_calls", 0) for report in reports),
                "embedding_calls": sum(report.metrics.get("embedding_calls", 0) for report in reports),
                "network_calls": sum(report.metrics.get("network_calls", 0) for report in reports),
                "average_seconds": round(duration / len(results), 6) if results else 0.0,
            },
        )
    finally:
        output.unlink(missing_ok=True)


def _scenario_result_from_dict(item: dict) -> ScenarioResult:
    criteria = [CriterionResult(**criterion) for criterion in item.get("criteria", [])]
    allowed = {field.name for field in fields(ScenarioResult)}
    values = {key: value for key, value in item.items() if key in allowed}
    values["criteria"] = criteria
    return ScenarioResult(**values)


def _run_external_scenarios(worktree: Path, scenarios, timeout_seconds: int):
    """Évalue les cas privés avec le code candidat, sans les copier dans son worktree."""
    input_descriptor, input_name = tempfile.mkstemp(prefix="scenario_lab_input_", suffix=".json")
    output_descriptor, output_name = tempfile.mkstemp(prefix="scenario_lab_output_", suffix=".json")
    os.close(input_descriptor)
    os.close(output_descriptor)
    input_path, output_path = Path(input_name), Path(output_name)
    input_path.write_text(json.dumps([asdict(item) for item in scenarios], ensure_ascii=False), encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(worktree)
    script = (
        "import json,sys; from dataclasses import asdict; "
        "from self_improvement.models import Scenario; "
        "from self_improvement.benchmark_runner import execute_scenario; "
        "items=json.load(open(sys.argv[1],encoding='utf-8')); "
        "results=[asdict(execute_scenario(Scenario(**item))) for item in items]; "
        "json.dump(results,open(sys.argv[2],'w',encoding='utf-8'),ensure_ascii=False)"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(input_path), str(output_path)],
            cwd=worktree, capture_output=True, text=True, timeout=timeout_seconds, env=environment,
        )
        if completed.returncode:
            raise RuntimeError((completed.stderr or completed.stdout)[-4000:])
        return [_scenario_result_from_dict(item) for item in json.loads(output_path.read_text(encoding="utf-8"))]
    finally:
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def _extend_report(report: BenchmarkReport, extra_results: list[ScenarioResult]) -> BenchmarkReport:
    if not extra_results:
        return report
    report.results.extend(extra_results)
    report.score, report.category_scores, report.dimension_scores, report.security_score = aggregate_scores(report.results)
    report.dimension_scores["performance"] = 100.0
    report.metrics["scenario_count"] = len(report.results)
    report.metrics["failure_count"] = len(report.failures)
    return report


def _summary(report: BenchmarkReport) -> dict:
    return {
        "score": report.score, "security_score": report.security_score,
        "category_scores": report.category_scores, "dimension_scores": report.dimension_scores,
        "failure_count": len(report.failures), "duration_seconds": report.duration_seconds,
        "commit": report.commit,
    }


class ImprovementLoop:
    def __init__(
        self, root=PROJECT_ROOT, *, benchmark_runner=None, codex_runner=None,
        git_manager=None, compile_runner=None, test_runner=None,
        candidate_benchmark_runner=None, scenario_lab=None, repair_engine=None, logger=print,
    ):
        self.root = Path(root).resolve()
        self.logger = logger
        self.benchmark_runner = benchmark_runner or BenchmarkRunner(project_root=self.root)
        self.codex_runner = codex_runner or CodexRunner()
        self.git = git_manager or GitWorktreeManager(self.root)
        self.scenario_lab = scenario_lab
        self.repair_engine = repair_engine
        self._dynamic_scenarios = []
        self.compile_runner = compile_runner or _compile_candidate
        self.test_runner = test_runner or _run_tests
        self.candidate_benchmark_runner = candidate_benchmark_runner or (
            lambda path, timeout: _run_candidate_benchmark(
                path, timeout,
                progress=lambda split: self.logger(f"[SelfImprove] Benchmark {split}"),
                dynamic_scenarios=self._dynamic_scenarios,
            )
        )

    def run(
        self, *, cycles=1, max_minutes=60, dry_run=False, benchmark_only=False,
        no_codex=False, report=False, minimum_improvement=MIN_IMPROVEMENT,
        target_score=DEFAULT_TARGET_SCORE, codex_timeout=900,
        codex_silence_warning=DEFAULT_SILENCE_WARNING_SECONDS,
        autonomous=False, max_scenarios=500, seed=42, scenario_lab_timeout=60,
        no_auto_git_preflight=False, no_self_repair=False, local_repair_only=False,
        repair_verbose=False, max_local_candidates=12, repair_timeout=None,
        no_auto_session_commit=False,
    ) -> list[dict]:
        started = time.monotonic()
        lab_report = None
        lab = self.scenario_lab
        if autonomous:
            lab = lab or ScenarioLab(logger=self.logger)
            self.logger("[SelfImprove] Campagne Autonomous Scenario Lab")
            lab_report = lab.run(
                max_scenarios=max_scenarios, seed=seed,
                max_seconds=scenario_lab_timeout, save=not dry_run,
            )
            if report:
                lab.write_report(lab_report)
            if lab_report.get("interrupted") or lab_report.get("timed_out"):
                return [{
                    "cycle": 0,
                    "decision": "INTERRUPTED" if lab_report.get("interrupted") else "STOPPED",
                    "reason": "Campagne Scenario Lab interrompue." if lab_report.get("interrupted") else "Timeout Scenario Lab atteint.",
                    "scenario_lab": lab_report,
                }]
            public_discoveries = load_discovered(lab.public_cases_path)
            private_discoveries = load_discovered(lab.private_holdout_path)
            legacy_unpartitioned = [
                item for item in public_discoveries
                if "red-team" in item.tags
                and not any(tag.startswith("discovery-family:") for tag in item.tags)
                and not any(tag.startswith("legacy-status:") for tag in item.tags)
            ]
            if legacy_unpartitioned:
                return [{
                    "cycle": 0, "decision": "STOPPED",
                    "reason": (
                        "Des découvertes Red Team publiques utilisent l'ancien format sans "
                        "partition familiale vérifiable; elles ne seront pas envoyées à Codex. "
                        "Régénérer ou migrer explicitement ce corpus avant le mode autonome."
                    ),
                    "scenario_lab": lab_report,
                    "legacy_unpartitioned_count": len(legacy_unpartitioned),
                }]
            self._dynamic_scenarios = [*public_discoveries, *private_discoveries]
        self.logger("[SelfImprove] Benchmark de référence")
        baseline = self.benchmark_runner.run()
        baseline = _extend_report(
            baseline,
            [lab.executor(item) for item in self._dynamic_scenarios] if lab is not None else [],
        )
        baseline_tests = {"executed_by_loop": False}
        if not benchmark_only:
            self.logger("[SelfImprove] Exécution pytest baseline")
            baseline_tests = self.test_runner(self.root, max(60, int(max_minutes * 60)))
            baseline_tests["executed_by_loop"] = True
        baseline.tests = baseline_tests
        write_baseline(baseline, tests=baseline_tests)
        self.logger(f"Baseline : {baseline.score:.2f}/100 ({len(baseline.failures)} échecs).")
        if benchmark_only:
            return [{"decision": "BENCHMARK_ONLY", "baseline": _summary(baseline)}]
        if not baseline_tests.get("passed"):
            return [{
                "decision": "STOPPED", "baseline": _summary(baseline),
                "reason": "La suite de tests baseline échoue; aucun candidat n'est autorisé.",
                "tests": baseline_tests,
            }]
        records = []
        no_gain = codex_failures = 0
        base_ref = "HEAD"
        requested_cycle = 1
        for _iteration in range(cycles):
            cycle = (
                self.git.next_available_cycle(requested_cycle)
                if not dry_run and not no_codex else requested_cycle
            )
            requested_cycle = cycle + 1
            self.logger(f"[SelfImprove] Préparation du cycle {cycle:03d}")
            if (time.monotonic() - started) / 60 >= max_minutes:
                records.append({"cycle": cycle, "decision": "STOPPED", "reason": "Temps maximal atteint."})
                break
            if baseline.score >= target_score:
                records.append({"cycle": cycle, "decision": "STOPPED", "reason": "Score cible atteint."})
                break
            train_failures = [failure for failure in baseline.failures if failure.split != "holdout"]
            if not train_failures:
                records.append({"cycle": cycle, "decision": "STOPPED", "reason": "Aucun échec train/validation prioritaire."})
                break
            self.logger("[SelfImprove] Génération de la tâche Codex")
            task = generate_improvement_task(baseline)
            if autonomous and lab is not None:
                visible = [
                    item for item in load_discovered(lab.public_cases_path)
                    if item.split == "train"
                ]
                if visible:
                    task += (
                        "\n\nÉchecs train découverts par le Scenario Lab (le holdout privé "
                        "n'est pas exposé) :\n"
                        + "\n".join(
                            f"- {item.id}: messages={item.messages!r}; critères={item.success_criteria!r}"
                            for item in visible[:50]
                        )
                    )
            record = {
                "cycle": cycle, "baseline": _summary(baseline), "candidate": None,
                "changed_files": [], "fixed_failures": [], "new_failures": [],
                "tests": {}, "duration_seconds": 0,
            }
            if lab_report is not None:
                record["scenario_lab"] = lab_report
            cycle_started = time.monotonic()
            if dry_run or (no_codex and not local_repair_only):
                record.update(
                    decision="DRY_RUN" if dry_run else "NO_CODEX",
                    reason="Tâche générée sans mutation du dépôt.",
                    task_preview=task,
                )
            else:
                worktree = None
                try:
                    self.logger("[SelfImprove] Vérification Git")
                    preflight = None
                    if autonomous and not no_auto_git_preflight:
                        preflight_checker = GitPreflight(self.root)
                        preflight_checker.auto_commit_validated_session = not no_auto_session_commit
                        preflight = preflight_checker.run()
                        record["git_preflight"] = preflight.to_dict()
                        if preflight.committed:
                            self.logger(
                                f"[SelfImprove] Artefacts générés commités automatiquement ({preflight.commit})"
                            )
                    else:
                        self.git.require_clean()
                    if not no_self_repair:
                        engine = self.repair_engine or SelfRepairEngine(
                            self.root, git_manager=self.git,
                            compile_runner=self.compile_runner,
                            targeted_test_runner=_run_targeted_tests,
                            representative_runner=lambda path, ids, timeout: _run_representative_benchmark(
                                path, ids, timeout,
                                scenarios=[
                                    *load_scenarios(splits=["train"])[1],
                                    *(item for item in self._dynamic_scenarios if item.split == "train"),
                                ],
                            ),
                            test_runner=self.test_runner,
                            benchmark_runner=self.candidate_benchmark_runner,
                            coverage_threshold=(
                                _coverage_threshold(self.root)
                                if (self.root / "pyproject.toml").exists() else 0.0
                            ),
                            minimum_improvement=minimum_improvement,
                            timeout_seconds=(repair_timeout or max(30, min(codex_timeout, int(max_minutes * 60)))),
                            max_total_candidates=max_local_candidates,
                            logger=self.logger, verbose=repair_verbose,
                        )
                        self.logger("[SelfImprove] Tentative Self-Repair locale")
                        repair = engine.run(
                            baseline, cycle=cycle, base_ref=base_ref, safety=preflight,
                        )
                        record["self_repair"] = repair.to_dict()
                        if repair.accepted:
                            candidate = repair.candidate_report
                            candidate.commit = repair.commit
                            record["candidate"] = _summary(candidate)
                            record["changed_files"] = repair.changed_files
                            record["security_changes"] = repair.security_changes
                            record.update(
                                decision="ACCEPTED",
                                reason=(
                                    f"Réparation locale {repair.operator} acceptée; "
                                    f"commit {repair.commit}. Codex évité."
                                ),
                            )
                            baseline = candidate
                            base_ref = repair.commit
                            no_gain = 0
                            raise _LocalCycleComplete
                    if local_repair_only:
                        record.update(
                            decision="REJECTED",
                            reason=(
                                "Aucune réparation locale acceptée; fallback Codex "
                                "désactivé par --local-repair-only."
                            ),
                        )
                        no_gain += 1
                        raise _LocalCycleComplete
                    self.logger("[SelfImprove] Création du worktree")
                    if preflight is None:
                        worktree = self.git.create(cycle, base_ref=base_ref)
                    else:
                        worktree = self.git.create(cycle, base_ref=base_ref, safety=preflight)
                    self.logger("[SelfImprove] Lancement de Codex CLI")
                    codex = self.codex_runner.run(
                        task, worktree.path, timeout_seconds=codex_timeout,
                        silence_warning_seconds=codex_silence_warning, logger=self.logger,
                    )
                    record["codex"] = {
                        "success": codex.success, "returncode": codex.returncode,
                        "error": codex.error, "stdout_tail": codex.stdout[-4000:],
                        "stderr_tail": codex.stderr[-4000:], "timed_out": codex.timed_out,
                        "interrupted": codex.interrupted, "duration_seconds": codex.duration_seconds,
                    }
                    if codex.interrupted:
                        record.update(
                            decision="INTERRUPTED",
                            reason="Cycle interrompu par l'utilisateur pendant Codex CLI.",
                        )
                    elif not codex.success:
                        codex_failures += 1
                        record.update(decision="REJECTED", reason=codex.error or "Échec Codex.")
                    else:
                        record["changed_files"] = self.git.changed_files(worktree)
                        operation_timeout = max(60, int(max_minutes * 60))
                        self.logger("[SelfImprove] Compilation du candidat")
                        compilation = self.compile_runner(worktree.path, operation_timeout)
                        if compilation["passed"]:
                            self.logger("[SelfImprove] Exécution pytest")
                            tests = self.test_runner(worktree.path, operation_timeout)
                        else:
                            self.logger("[SelfImprove] Pytest ignoré : compilation en échec")
                            tests = {
                                "passed": False, "returncode": compilation["returncode"],
                                "coverage": None, "output_tail": compilation["output_tail"],
                            }
                        tests["compilation"] = compilation
                        record["tests"] = tests
                        diff_check = subprocess.run(
                            ["git", "diff", "--check"], cwd=worktree.path,
                            capture_output=True, text=True, timeout=60,
                        )
                        tests["diff_check_passed"] = diff_check.returncode == 0
                        if tests["passed"] and tests["diff_check_passed"]:
                            candidate = self.candidate_benchmark_runner(
                                worktree.path, max(60, int(max_minutes * 60))
                            )
                            record["candidate"] = _summary(candidate)
                            decision = decide_acceptance(
                                baseline, candidate, tests_passed=True,
                                coverage=tests["coverage"], coverage_threshold=_coverage_threshold(worktree.path),
                                minimum_improvement=minimum_improvement,
                            )
                            # Diagnostic agrégé uniquement : aucun identifiant ou
                            # détail de scénario validation/holdout n'est publié.
                            record["security_changes"] = decision.security_changes
                        else:
                            candidate = None
                            decision = None
                        if decision and decision.accepted:
                            commit = self.git.commit(worktree, f"self-improvement: cycle {cycle:03d}")
                            self.logger("[SelfImprove] Nettoyage worktree")
                            try:
                                self.git.close_accepted(worktree)
                            except GitError as cleanup_error:
                                record["cleanup_error"] = str(cleanup_error)
                            worktree = None
                            candidate.commit = commit
                            record["candidate"] = _summary(candidate)
                            record.update(decision="ACCEPTED", reason=f"Gain {decision.improvement:.2f}; commit {commit}.")
                            # Les rapports de cycle sont publics : aucun identifiant
                            # de holdout dynamique ou statique ne doit y apparaître.
                            before = {
                                item.scenario_id for item in baseline.failures
                                if item.split != "holdout"
                            }
                            after = {
                                item.scenario_id for item in candidate.failures
                                if item.split != "holdout"
                            }
                            record["fixed_failures"] = sorted(before - after)
                            record["new_failures"] = sorted(after - before)
                            baseline = candidate
                            base_ref = commit
                            no_gain = 0
                        else:
                            reasons = decision.reasons if decision else ["Tests ou git diff --check en échec."]
                            record.update(decision="REJECTED", reason=" ".join(reasons))
                            no_gain += 1
                except _LocalCycleComplete:
                    pass
                except GitPreflightError as error:
                    for path in error.result.human_or_unknown:
                        self.logger(f"[SelfImprove] Git preflight bloqué : {path}")
                    record.update(
                        decision="STOPPED", reason=str(error),
                        git_preflight=error.result.to_dict(),
                        blocking_paths=error.result.blocking,
                    )
                except KeyboardInterrupt:
                    record.update(
                        decision="INTERRUPTED",
                        reason="Cycle interrompu par l'utilisateur.",
                    )
                except (GitError, OSError, subprocess.SubprocessError, RuntimeError) as error:
                    record.update(decision="REJECTED", reason=f"Cycle interrompu proprement : {error}")
                    no_gain += 1
                finally:
                    if worktree is not None:
                        self.logger("[SelfImprove] Nettoyage worktree")
                        try:
                            self.git.abandon(worktree)
                        except GitError as cleanup_error:
                            record["cleanup_error"] = str(cleanup_error)
                        worktree = None
            self.logger(f"[SelfImprove] Décision {record['decision']}")
            record["duration_seconds"] = round(time.monotonic() - cycle_started, 3)
            records.append(record)
            if report:
                write_cycle_report(cycle, record)
            if record["decision"] == "INTERRUPTED":
                break
            if no_gain >= 2 or codex_failures >= 2:
                break
        return records


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--max-minutes", type=float, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--benchmark-only", action="store_true")
    parser.add_argument("--no-codex", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument(
        "--codex-timeout", type=float, default=DEFAULT_CODEX_TIMEOUT_SECONDS,
    )
    parser.add_argument("--codex-silence-warning", type=float, default=DEFAULT_SILENCE_WARNING_SECONDS)
    parser.add_argument("--minimum-improvement", type=float, default=MIN_IMPROVEMENT)
    parser.add_argument("--target-score", type=float, default=DEFAULT_TARGET_SCORE)
    parser.add_argument("--autonomous", action="store_true")
    parser.add_argument("--max-scenarios", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scenario-lab-timeout", type=float, default=60)
    parser.add_argument("--no-auto-git-preflight", action="store_true")
    parser.add_argument(
        "--no-auto-session-commit", action="store_true",
        help="Désactive l’auto-commit des sessions Codex dont la provenance est validée.",
    )
    parser.add_argument("--no-self-repair", action="store_true")
    parser.add_argument(
        "--local-repair-only", action="store_true",
        help="Tente un cycle Self-Repair réel sans autoriser le fallback Codex.",
    )
    parser.add_argument("--repair-verbose", action="store_true")
    parser.add_argument("--max-local-candidates", type=int, default=12)
    parser.add_argument("--repair-timeout", type=float)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if (
        not 1 <= args.cycles <= 100 or args.max_minutes <= 0
        or args.codex_timeout <= 0 or args.codex_silence_warning <= 0
        or not 1 <= args.max_scenarios <= 10_000 or args.scenario_lab_timeout <= 0
        or args.max_local_candidates < 1
        or (args.repair_timeout is not None and args.repair_timeout <= 0)
    ):
        raise SystemExit(
            "--cycles doit être entre 1 et 100 et tous les délais doivent être positifs."
        )
    try:
        records = ImprovementLoop().run(**vars(args))
    except GitError as error:
        print(f"Cycle réel refusé : {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        interrupted = {
            "cycle": 0, "decision": "INTERRUPTED",
            "reason": "Interruption avant la création du cycle.",
            "baseline": {}, "candidate": None, "changed_files": [], "tests": {},
            "fixed_failures": [], "new_failures": [], "duration_seconds": 0,
        }
        if args.report:
            write_cycle_report(0, interrupted)
        print("Cycle 000: INTERRUPTED — interruption utilisateur.", file=sys.stderr)
        return 130
    for record in records:
        print(f"Cycle {record.get('cycle', 0):03d}: {record['decision']} — {record.get('reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
