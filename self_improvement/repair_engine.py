"""Self-Repair déterministe : diagnostic, sélection explicable et validation bornée."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, replace
import hashlib
import inspect
from pathlib import Path
import re
import subprocess
import time

from .evaluator import decide_acceptance
from .code_localization import CodeLocalizer
from .git_manager import GitError, GitWorktreeManager, Worktree
from .models import BenchmarkReport
from .repair_history import RepairHistory, patch_signature
from .repair_operators import DEFAULT_OPERATORS, UnsafeRepairError
from .reporting import write_self_repair_report
from .root_cause import RootCause, RootCauseEngine


REJECTION_STAGES = frozenset({
    "patch_invalid", "patch_bounds", "compilation", "targeted_tests",
    "local_tests", "full_tests",
    "representative_scenarios", "wrong_localization", "no_behavior_change",
    "behavior_changed_but_not_fixed", "train", "validation",
    "holdout", "security", "insufficient_gain", "timeout", "internal_error",
})


class CandidateRejected(RuntimeError):
    def __init__(self, stage: str, reason: str):
        if stage not in REJECTION_STAGES:
            raise ValueError(f"Stage de rejet inconnu : {stage}")
        super().__init__(reason)
        self.stage, self.reason = stage, reason


@dataclass
class CandidateDiagnostic:
    candidate_id: str
    root_cause_id: str
    operator: str
    applicability_score: float
    generation_reason: str
    canonical_cause: str = ""
    baseline_commit: str | None = None
    repair_hypothesis: str = ""
    evidence_used: list[str] = field(default_factory=list)
    expected_behavior_change: str = ""
    localized_target: list[str] = field(default_factory=list)
    localization_confidence: float = 0.0
    confidence: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    changed_symbols: list[str] = field(default_factory=list)
    lines_changed: int = 0
    patch_size_bytes: int = 0
    patch_signature: str = ""
    generation_duration_seconds: float = 0.0
    patch_valid: bool = False
    patch_bounds_passed: bool = False
    compilation_passed: bool = False
    targeted_tests_passed: bool = False
    targeted_tests_returncode: int | None = None
    targeted_tests_command: list[str] = field(default_factory=list)
    targeted_tests_failed_tests: list[str] = field(default_factory=list)
    targeted_tests_stdout: str = ""
    targeted_tests_stderr: str = ""
    targeted_tests_duration_seconds: float | None = None
    local_tests_passed: bool | None = None
    local_tests_returncode: int | None = None
    local_tests_command: list[str] = field(default_factory=list)
    local_tests_failed_tests: list[str] = field(default_factory=list)
    local_tests_stdout: str = ""
    local_tests_stderr: str = ""
    local_tests_duration_seconds: float | None = None
    full_tests_passed: bool | None = None
    full_tests_returncode: int | None = None
    full_tests_command: list[str] = field(default_factory=list)
    full_tests_failed_tests: list[str] = field(default_factory=list)
    full_tests_stdout: str = ""
    full_tests_stderr: str = ""
    full_tests_duration_seconds: float | None = None
    representative_scenarios_passed: bool = False
    behavior_changed: bool = False
    target_symbol_reached: bool = False
    target_component_reached: bool = False
    relevant_branch_reached: bool = False
    relevant_condition_before: list[str] = field(default_factory=list)
    relevant_condition_after: list[str] = field(default_factory=list)
    expected_branch: list[str] = field(default_factory=list)
    actual_branch_before: list[str] = field(default_factory=list)
    actual_branch_after: list[str] = field(default_factory=list)
    contract_before: list[str] = field(default_factory=list)
    contract_after: list[str] = field(default_factory=list)
    train_score_before: float | None = None
    train_score_after: float | None = None
    train_gain: float | None = None
    validation_score_before: float | None = None
    validation_score_after: float | None = None
    validation_gain: float | None = None
    holdout_score_before: float | None = None
    holdout_score_after: float | None = None
    holdout_gain: float | None = None
    security_score_before: float | None = None
    security_score_after: float | None = None
    true_security_regressions_count: int = 0
    total_duration_seconds: float = 0.0
    rejection_stage: str | None = None
    rejection_reason: str = ""
    bundle_composable: bool = False
    outcome: str = "pending"


@dataclass
class RepairMetrics:
    failures_analyzed: int = 0
    root_causes_detected: int = 0
    raw_failure_groups: int = 0
    canonical_root_causes: int = 0
    merged_root_cause_groups: int = 0
    localized_root_causes: int = 0
    compatible_failures: int = 0
    repair_operator_attempts: int = 0
    candidates_generated: int = 0
    candidates_evaluated: int = 0
    bundles_generated: int = 0
    bundles_evaluated: int = 0
    bundles_accepted: int = 0
    bundles_rejected: int = 0
    bundle_member_total: int = 0
    local_repairs_accepted: int = 0
    local_repairs_rejected: int = 0
    operators_considered: int = 0
    operators_rejected_as_inapplicable: int = 0
    operators_selected: int = 0
    operators_refused_already_supported: int = 0
    operators_refused_unsupported_repair_hypothesis: int = 0
    operator_selection_accuracy: float | str = "not_available"
    rejected_patch_invalid: int = 0
    rejected_patch_bounds: int = 0
    rejected_compile: int = 0
    rejected_targeted_tests: int = 0
    rejected_local_tests: int = 0
    rejected_full_tests: int = 0
    rejected_representative_scenarios: int = 0
    rejected_no_behavior_change: int = 0
    rejected_wrong_localization: int = 0
    rejected_behavior_changed_but_not_fixed: int = 0
    localized_candidates: int = 0
    localization_reached_candidates: int = 0
    localization_success_rate: float | str = "not_available"
    candidates_reaching_train: int = 0
    candidates_reaching_validation: int = 0
    candidates_reaching_holdout: int = 0
    candidates_reaching_full_evaluation: int = 0
    average_fast_gate_seconds: float = 0.0
    average_full_evaluation_seconds: float = 0.0
    codex_fallbacks: int = 0
    codex_fallback_reason: str | None = None
    codex_avoided: int = 0
    percent_cycles_without_codex: float = 0.0
    repair_duration_seconds: float = 0.0
    repeated_failed_patch_rate: float | str = "not_available"


@dataclass
class RepairRunResult:
    accepted: bool
    metrics: RepairMetrics
    reason: str
    baseline_score: float | None = None
    candidate_report: BenchmarkReport | None = None
    commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    operator: str | None = None
    security_changes: dict[str, int] = field(default_factory=dict)
    root_causes: list[RootCause] = field(default_factory=list)
    candidate_diagnostics: list[CandidateDiagnostic] = field(default_factory=list)
    operator_decisions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("candidate_report", None)
        for cause in payload.get("root_causes", []):
            cause.pop("cases", None)
        return payload


def _split_score(report: BenchmarkReport, split: str) -> float | None:
    cases = [item for item in report.results if item.split == split]
    if not cases:
        return None
    total = sum(item.weight for item in cases)
    return round(sum(item.score * item.weight for item in cases) / total, 3) if total else 0.0


class SelfRepairEngine:
    def __init__(
        self, root, *, git_manager=None, operators=DEFAULT_OPERATORS,
        root_cause_engine=None, code_localizer=None, history=None, compile_runner=None,
        targeted_test_runner=None, representative_runner=None, test_runner=None,
        benchmark_runner=None, coverage_threshold=84.0, minimum_improvement=0.5,
        minimum_root_cause_confidence=0.55, max_operators_per_cause=3,
        max_candidates_per_operator=2, max_attempts_per_root_cause=4,
        max_total_candidates=12, max_full_evaluations=3, max_files_modified=2,
        max_lines_modified=30, timeout_seconds=300, max_candidate_seconds=90,
        max_fast_gate_seconds=30, clock=time.monotonic, logger=lambda _message: None,
        verbose=False, report_directory=None, max_total_seconds=None,
        max_total_repair_seconds=None,
    ):
        self.root = Path(root).resolve()
        self.git = git_manager or GitWorktreeManager(self.root)
        self.operators = tuple(operators)
        self.root_cause_engine = root_cause_engine or RootCauseEngine()
        self.code_localizer = code_localizer or CodeLocalizer(self.root)
        self.history = history or RepairHistory(self.root / "self_improvement" / "repair_history")
        self.compile_runner, self.targeted_test_runner = compile_runner, targeted_test_runner
        self.representative_runner, self.test_runner = representative_runner, test_runner
        self.benchmark_runner = benchmark_runner
        self.coverage_threshold, self.minimum_improvement = coverage_threshold, minimum_improvement
        self.minimum_root_cause_confidence = minimum_root_cause_confidence
        self.max_operators_per_cause = max_operators_per_cause
        self.max_candidates_per_operator = max_candidates_per_operator
        self.max_attempts_per_root_cause, self.max_total_candidates = max_attempts_per_root_cause, max_total_candidates
        self.max_full_evaluations = max_full_evaluations
        self.max_files_modified, self.max_lines_modified = max_files_modified, max_lines_modified
        total_budget = max_total_repair_seconds or max_total_seconds or timeout_seconds
        self.timeout_seconds, self.max_candidate_seconds = total_budget, max_candidate_seconds
        self.max_fast_gate_seconds = max_fast_gate_seconds
        self.clock, self.logger, self.verbose = clock, logger, verbose
        self.report_directory = Path(report_directory or self.root / "self_improvement" / "reports")
        self._fast_gate_durations, self._full_durations = [], []

    @staticmethod
    def _train_only(report: BenchmarkReport) -> BenchmarkReport:
        return replace(report, splits=["train"], results=[item for item in report.results if item.split == "train"])

    def analyze(self, report: BenchmarkReport):
        train = self._train_only(report)
        causes = self.root_cause_engine.analyze(train)
        localized_causes = []
        for cause in causes:
            localization = self.code_localizer.localize(cause)
            uncertainty = cause.uncertainty_reason
            if not localization.reliable:
                uncertainty = "Localisation de code insuffisante; aucun patch local autorisé."
            localized_causes.append(replace(
                cause, suspected_files=list(localization.candidate_files),
                suspected_symbols=list(localization.candidate_symbols),
                localization=localization, uncertainty_reason=uncertainty,
            ))
        causes = localized_causes
        selected, compatible_ids = [], set()
        for cause in causes:
            ranked = []
            for operator in self.operators:
                category_supported = operator.applicable(cause)
                applicability = operator.applicability(cause)
                score = applicability.score * self.history.priority_multiplier(operator.name)
                if category_supported and applicability.supported and cause.confidence >= self.minimum_root_cause_confidence:
                    ranked.append((score, operator, applicability))
            ranked.sort(key=lambda item: (-item[0], item[1].name))
            ranked = ranked[:self.max_operators_per_cause]
            if ranked:
                compatible_ids.update(case.scenario_id for case in cause.cases)
            selected.append((cause, ranked))
        return train, causes, selected, compatible_ids

    def _diff_size(self, worktree: Worktree) -> tuple[int, int]:
        result = subprocess.run(["git", "diff", "--numstat"], cwd=worktree.path, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise CandidateRejected("patch_invalid", "Impossible de mesurer le patch local.")
        files = lines = 0
        for row in result.stdout.splitlines():
            added, removed, _path = row.split("\t", 2)
            if added == "-" or removed == "-":
                raise CandidateRejected("patch_bounds", "Les modifications binaires sont interdites.")
            files += 1
            lines += int(added) + int(removed)
        return files, lines

    def _validate_candidate(self, operator, candidate, worktree):
        try:
            changed = operator.apply(candidate, worktree.path)
        except UnsafeRepairError as error:
            raise CandidateRejected("patch_invalid", str(error)) from error
        files, lines = self._diff_size(worktree)
        if files == 0 or files > self.max_files_modified:
            raise CandidateRejected("patch_bounds", "Nombre de fichiers modifiés hors limite.")
        if lines > self.max_lines_modified:
            raise CandidateRejected("patch_bounds", "Nombre de lignes modifiées hors limite.")
        if set(self.git.changed_files(worktree)) != set(changed):
            raise CandidateRejected("patch_bounds", "Le patch a modifié une zone non déclarée.")
        return changed, lines

    def _validate_bundle(self, members, worktree):
        declared_changed = []

        for member in members:
            operator = member["operator"]
            candidate = member["candidate"]

            try:
                operator.validate(candidate)
            except UnsafeRepairError as error:
                raise CandidateRejected("patch_invalid", str(error)) from error

            try:
                changed = operator.apply(candidate, worktree.path)
            except UnsafeRepairError as error:
                raise CandidateRejected("patch_invalid", str(error)) from error

            declared_changed.extend(changed)

        files, lines = self._diff_size(worktree)

        if files == 0 or files > self.max_files_modified:
            raise CandidateRejected(
                "patch_bounds",
                "Nombre de fichiers modifiés hors limite.",
            )

        if lines > self.max_lines_modified:
            raise CandidateRejected(
                "patch_bounds",
                "Nombre de lignes modifiées hors limite.",
            )

        actual_changed = set(self.git.changed_files(worktree))
        declared_changed_set = set(declared_changed)

        if actual_changed != declared_changed_set:
            raise CandidateRejected(
                "patch_bounds",
                "Le bundle a modifié une zone non déclarée.",
            )

        return sorted(actual_changed), lines


    @staticmethod
    def _call_runner(runner, *arguments):
        signature = inspect.signature(runner)
        positional = [item for item in signature.parameters.values() if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)]
        if any(item.kind == item.VAR_POSITIONAL for item in signature.parameters.values()):
            return runner(*arguments)
        return runner(*arguments[:len(positional)])

    def _timed_out(self, started):
        return self.clock() - started >= self.timeout_seconds

    @staticmethod
    def _behavior_changed(baseline, candidate, identifiers):
        def fingerprint(item):
            criteria = tuple(
                (criterion.name, criterion.passed, repr(criterion.expected), repr(criterion.actual))
                for criterion in item.criteria
            )
            return item.trace, criteria, item.error

        before = {item.scenario_id: fingerprint(item) for item in baseline.results if item.scenario_id in identifiers}
        after = {item.scenario_id: fingerprint(item) for item in candidate.results if item.scenario_id in identifiers}
        return bool(before and after and any(before[key] != after.get(key) for key in before if key in after))

    @staticmethod
    def _populate_behavior_diagnostic(diagnostic, baseline, candidate, identifiers, cause):
        before = {item.scenario_id: item for item in baseline.results if item.scenario_id in identifiers}
        after = {item.scenario_id: item for item in candidate.results if item.scenario_id in identifiers}

        def observation(item):
            value = item.trace.get("repair_observation", {}) if item else {}
            return value if isinstance(value, dict) else {}

        def contracts(item):
            if item is None:
                return ["résultat absent"]
            return [
                f"{criterion.name}: expected={criterion.expected!r}, actual={criterion.actual!r}, passed={criterion.passed}"
                for criterion in item.criteria if not criterion.passed
            ] or ["tous les contrats représentatifs passent"]

        common = sorted(set(before) & set(after))
        diagnostic.relevant_condition_before = [
            f"{key}: {observation(before[key]).get('relevant_condition')}="
            f"{observation(before[key]).get('condition_result')}" for key in common
        ]
        diagnostic.relevant_condition_after = [
            f"{key}: {observation(after[key]).get('relevant_condition')}="
            f"{observation(after[key]).get('condition_result')}" for key in common
        ]
        diagnostic.actual_branch_before = [
            f"{key}: {observation(before[key]).get('actual_branch', 'unknown')}" for key in common
        ]
        diagnostic.actual_branch_after = [
            f"{key}: {observation(after[key]).get('actual_branch', 'unknown')}" for key in common
        ]
        diagnostic.expected_branch = list(cause.diagnosis.expected_behavior)
        diagnostic.contract_before = [
            f"{key}: {value}" for key in common for value in contracts(before[key])
        ]
        diagnostic.contract_after = [
            f"{key}: {value}" for key in common for value in contracts(after[key])
        ]
        diagnostic.relevant_branch_reached = bool(common) and all(
            observation(after[key]).get("relevant_condition") for key in common
        )

    def _reject(self, diagnostic, metrics, stage, reason, candidate_started):
        gate_field = {
            "targeted_tests": "targeted_tests_passed",
            "local_tests": "local_tests_passed",
            "full_tests": "full_tests_passed",
        }.get(stage)
        if gate_field and getattr(diagnostic, gate_field) is True:
            raise AssertionError(
                f"Invariant Fast Gate violé : {gate_field}=True avec rejection_stage={stage}."
            )
        diagnostic.rejection_stage, diagnostic.rejection_reason = stage, reason
        diagnostic.outcome = "rejected"
        diagnostic.total_duration_seconds = round(self.clock() - candidate_started, 3)
        metrics.local_repairs_rejected += 1
        field_name = {
            "patch_invalid": "rejected_patch_invalid", "patch_bounds": "rejected_patch_bounds",
            "compilation": "rejected_compile", "targeted_tests": "rejected_targeted_tests",
            "local_tests": "rejected_local_tests", "full_tests": "rejected_full_tests",
            "representative_scenarios": "rejected_representative_scenarios",
            "wrong_localization": "rejected_wrong_localization",
            "no_behavior_change": "rejected_no_behavior_change",
            "behavior_changed_but_not_fixed": "rejected_behavior_changed_but_not_fixed",
        }.get(stage)
        if field_name:
            setattr(metrics, field_name, getattr(metrics, field_name) + 1)
        self.history.record({
            "root_cause_id": diagnostic.root_cause_id, "operator": diagnostic.operator,
            "canonical_cause": diagnostic.canonical_cause,
            "baseline_commit": diagnostic.baseline_commit,
            "patch_signature": diagnostic.patch_signature, "outcome": "rejected",
            "rejection_stage": stage, "rejection_reason": reason,
            "files_changed": diagnostic.changed_files, "behavioral_effect": diagnostic.behavior_changed,
            "localized_target": diagnostic.localized_target,
            "localization_confidence": diagnostic.localization_confidence,
            "target_component_reached": diagnostic.target_component_reached,
            "target_symbol_reached": diagnostic.target_symbol_reached,
            "train_gain": diagnostic.train_gain, "duration_seconds": diagnostic.total_duration_seconds,
            "bundle_composable": diagnostic.bundle_composable,
        })
        self.logger(
            f"[SelfRepair][{diagnostic.candidate_id}] Cause: {diagnostic.root_cause_id} | "
            f"Operator: {diagnostic.operator} | FastGate: {stage} FAILED | "
            f"Duration: {diagnostic.total_duration_seconds:.1f}s | Action: rejected"
        )

    @staticmethod
    def _record_pytest_result(diagnostic, suite: str, result: dict, duration: float) -> bool:
        """Copie un résultat pytest sur le diagnostic, qui devient la source de vérité."""
        passed = bool(result.get("passed"))
        output = str(result.get("output_tail", ""))
        stdout = str(result.get("stdout", output))[-4000:]
        stderr = str(result.get("stderr", ""))[-4000:]
        failed = result.get("failed_tests")
        if failed is None:
            failed = re.findall(r"(?m)^FAILED\s+(.+?)(?:\s+-\s+.*)?$", f"{stdout}\n{stderr}")
        command = result.get("command", [])
        if isinstance(command, str):
            command = [command]
        setattr(diagnostic, f"{suite}_passed", passed)
        setattr(diagnostic, f"{suite}_returncode", result.get("returncode"))
        setattr(diagnostic, f"{suite}_command", [str(item) for item in command])
        setattr(diagnostic, f"{suite}_failed_tests", list(dict.fromkeys(map(str, failed))))
        setattr(diagnostic, f"{suite}_stdout", stdout)
        setattr(diagnostic, f"{suite}_stderr", stderr)
        setattr(diagnostic, f"{suite}_duration_seconds", round(duration, 3))
        return getattr(diagnostic, f"{suite}_passed")

    @staticmethod
    def _select_bundle_pair(composable_pool):
        if len(composable_pool) < 2:
            return None

        for index, first in enumerate(composable_pool):
            first_files = {
                edit.path
                for edit in first["candidate"].edits
            }
            first_symbols = set(first["diagnostic"].changed_symbols)

            for second in composable_pool[index + 1:]:
                if first["cause"].root_cause_id == second["cause"].root_cause_id:
                    continue

                if first["signature"] == second["signature"]:
                    continue

                second_files = {
                    edit.path
                    for edit in second["candidate"].edits
                }
                second_symbols = set(second["diagnostic"].changed_symbols)

                if first_symbols & second_symbols:
                    continue

                # V1 volontairement stricte :
                # deux fichiers distincts éliminent les conflits d'édition.
                if first_files & second_files:
                    continue

                return first, second

        return None

    @staticmethod
    def _bundle_identity(members):
        cause_ids = sorted(member["cause"].root_cause_id for member in members)
        signatures = sorted(member["signature"] for member in members)
        digest = hashlib.sha256("\n".join(signatures).encode("utf-8")).hexdigest()
        return f"bundle:{'+'.join(cause_ids)}", digest

    def _record_bundle_history(self, members, baseline, *, outcome, stage=None, reason="", changed=None):
        root_cause_id, signature = self._bundle_identity(members)
        self.history.record({
            "root_cause_id": root_cause_id,
            "canonical_cause": "bundle-v1",
            "baseline_commit": baseline.commit,
            "operator": "bundle:" + "+".join(sorted(member["operator"].name for member in members)),
            "patch_signature": signature,
            "outcome": outcome,
            "rejection_stage": stage,
            "rejection_reason": reason,
            "files_changed": list(changed or []),
            "bundle_size": len(members),
            "bundle_member_signatures": sorted(member["signature"] for member in members),
        })

    @staticmethod
    def _bundle_rejection_stage(decision, diagnostics):
        if decision.security_changes.get("true_regressions") or any(
            diagnostic.security_score_after is not None
            and diagnostic.security_score_before is not None
            and diagnostic.security_score_after < diagnostic.security_score_before
            for diagnostic in diagnostics
        ):
            return "security"
        if any(diagnostic.validation_gain is not None and diagnostic.validation_gain < 0 for diagnostic in diagnostics):
            return "validation"
        if any(diagnostic.holdout_gain is not None and diagnostic.holdout_gain < 0 for diagnostic in diagnostics):
            return "holdout"
        if any("couverture" in reason.casefold() or "tests" in reason.casefold() for reason in decision.reasons):
            return "full_tests"
        return "insufficient_gain"

    def run(self, baseline: BenchmarkReport, *, cycle=1, base_ref="HEAD", safety=None):
        started = self.clock()
        self._fast_gate_durations, self._full_durations = [], []
        self._baseline_score = baseline.score
        self._operator_decisions = []
        metrics, diagnostics = RepairMetrics(), []
        try:
            train, causes, selected, compatible_ids = self.analyze(baseline)
        except (OSError, ValueError, RuntimeError) as error:
            metrics.codex_fallbacks, metrics.codex_fallback_reason = 1, "unsupported_root_cause"
            return self._finish(
                metrics, started, f"Erreur de diagnostic : {error}", [], diagnostics, cycle=cycle,
            )
        metrics.failures_analyzed, metrics.root_causes_detected = len(train.failures), len(causes)
        root_metrics = getattr(self.root_cause_engine, "last_metrics", {})
        metrics.raw_failure_groups = int(root_metrics.get("raw_failure_groups", len(causes)))
        metrics.canonical_root_causes = int(root_metrics.get("canonical_root_causes", len(causes)))
        metrics.merged_root_cause_groups = int(root_metrics.get("merged_root_cause_groups", 0))
        metrics.localized_root_causes = sum(cause.localization.reliable for cause in causes)
        metrics.compatible_failures = len(compatible_ids)
        metrics.operators_considered = len(causes) * len(self.operators)
        metrics.operators_selected = sum(len(items) for _cause, items in selected)
        metrics.operators_rejected_as_inapplicable = metrics.operators_considered - metrics.operators_selected
        if not causes:
            return self._finish(metrics, started, "Aucun échec TRAIN public.", causes, diagnostics, cycle=cycle)
        if not compatible_ids:
            metrics.codex_fallbacks = 1
            if any(c.compatible_repair_types and not c.localization.reliable for c in causes):
                metrics.codex_fallback_reason = "localization_insufficient"
            else:
                metrics.codex_fallback_reason = "low_confidence" if any(c.confidence < self.minimum_root_cause_confidence for c in causes) else "no_operator"
            return self._finish(metrics, started, "Aucun opérateur local applicable.", causes, diagnostics, cycle=cycle)
        if not all((self.compile_runner, self.test_runner, self.benchmark_runner)):
            metrics.codex_fallbacks, metrics.codex_fallback_reason = 1, "unsupported_root_cause"
            return self._finish(metrics, started, "Validation locale non configurée.", causes, diagnostics, cycle=cycle)

        next_cycle, repeated, generated = cycle, 0, 0
        composable_pool = []
        for cause, ranked in selected:
            attempts_for_cause = 0
            for applicability_score, operator, applicability in ranked:
                if attempts_for_cause >= self.max_attempts_per_root_cause:
                    break
                metrics.repair_operator_attempts += 1
                proposal = operator.propose_evidence_driven(cause, self.root)
                self._operator_decisions.append({
                    "root_cause_id": cause.root_cause_id,
                    "canonical_root_cause": cause.subtype,
                    "operator": operator.name,
                    "status": proposal.status,
                    "reason": proposal.reason,
                    "evidence": list(proposal.evidence),
                    "expected_values": list(cause.diagnosis.expected_behavior),
                    "observed_values": list(cause.diagnosis.observed_behavior),
                    "representative_inputs": list(operator.representative_inputs(cause)),
                    "public_train_inputs": list(operator.public_train_inputs(cause)),
                    "localized_target": [
                        f"{path}:{symbol}" for path in cause.localization.candidate_files
                        for symbol in cause.localization.candidate_symbols
                    ],
                })
                if proposal.status == "already_supported":
                    metrics.operators_refused_already_supported += 1
                    continue
                if proposal.status != "candidate" or not proposal.candidates:
                    metrics.operators_refused_unsupported_repair_hypothesis += 1
                    continue
                candidates = list(proposal.candidates)[:min(self.max_candidates_per_operator, operator.max_candidates)]
                for candidate in candidates:
                    if attempts_for_cause >= self.max_attempts_per_root_cause:
                        break
                    if generated >= self.max_total_candidates or metrics.candidates_reaching_full_evaluation >= self.max_full_evaluations:
                        break
                    if self._timed_out(started):
                        metrics.codex_fallbacks, metrics.codex_fallback_reason = 1, "all_local_candidates_rejected"
                        return self._finish(metrics, started, "Timeout du Self-Repair Engine.", causes, diagnostics, cycle=cycle)
                    proposed_at = self.clock()
                    attempts_for_cause += 1
                    generated += 1
                    metrics.candidates_generated += 1
                    signature = patch_signature(candidate)
                    diagnostic = CandidateDiagnostic(
                        candidate_id=f"C{generated:03d}", root_cause_id=cause.root_cause_id,
                        canonical_cause=cause.subtype,
                        baseline_commit=baseline.commit,
                        operator=operator.name, applicability_score=round(applicability_score, 3),
                        generation_reason=f"{candidate.description}; {applicability.reason}",
                        repair_hypothesis=candidate.repair_hypothesis,
                        evidence_used=list(candidate.evidence_used),
                        expected_behavior_change=candidate.expected_behavior_change,
                        localized_target=list(candidate.localized_target),
                        localization_confidence=candidate.localization_confidence,
                        confidence=candidate.confidence,
                        changed_symbols=sorted(set(cause.localization.candidate_symbols) & set(operator.allowed_symbols)),
                        patch_signature=signature,
                        patch_size_bytes=sum(len(e.old.encode("utf-8")) + len(e.new.encode("utf-8")) for e in candidate.edits),
                        generation_duration_seconds=round(self.clock() - proposed_at, 3),
                    )
                    diagnostics.append(diagnostic)
                    metrics.localized_candidates += 1
                    target_files = sorted(set(cause.localization.candidate_files) & set(operator.allowed_files))
                    target_symbols = diagnostic.changed_symbols
                    target_localization = replace(
                        cause.localization, candidate_files=target_files,
                        candidate_symbols=target_symbols,
                    )
                    eligibility = self.history.bundle_eligibility(
                        cause.root_cause_id,
                        signature,
                    )

                    was_rejected = self.history.was_rejected(
                        cause.root_cause_id, signature, operator=operator.name,
                        canonical_cause=cause.subtype, baseline_commit=baseline.commit,
                    )
                    if eligibility == "composable" and was_rejected:
                        composable_pool.append({
                            "cause": cause,
                            "operator": operator,
                            "candidate": candidate,
                            "diagnostic": diagnostic,
                            "signature": signature,
                            "target_localization": target_localization,
                        })
                        diagnostic.outcome = "bundle_candidate"
                        continue

                    if eligibility == "revalidate":
                        try:
                            self.git.require_clean()
                        except GitError as error:
                            repeated += 1
                            self._reject(diagnostic, metrics, "internal_error", str(error), self.clock())
                            continue
                        if not any(
                            item.get("outcome") == "retry_authorized"
                            and item.get("root_cause_id") == cause.root_cause_id
                            and item.get("patch_signature") == signature
                            for item in self.history.load()
                        ):
                            self.history.authorize_retry(
                                cause.root_cause_id, signature, operator=operator.name,
                                canonical_cause=cause.subtype, baseline_commit=baseline.commit,
                                rejection_stage="insufficient_gain",
                                rejection_reason="Réévaluation autorisée pour historique insufficient_gain legacy ou pré-Bundle.",
                            )
                        repeated += 1
                    elif was_rejected:
                        repeated += 1
                        self._reject(
                            diagnostic,
                            metrics,
                            "patch_invalid",
                            "Patch identique déjà rejeté pour cette cause.",
                            self.clock(),
                        )
                        continue
                    candidate_started = fast_started = self.clock()
                    worktree = None
                    try:
                        try:
                            operator.validate(candidate)
                            diagnostic.patch_valid = True
                        except UnsafeRepairError as error:
                            raise CandidateRejected("patch_invalid", str(error)) from error
                        worktree = self.git.create(next_cycle, base_ref=base_ref, safety=safety)
                        next_cycle = (worktree.cycle or next_cycle) + 1
                        changed, lines = self._validate_candidate(operator, candidate, worktree)
                        if not set(changed) <= set(target_localization.candidate_files):
                            raise CandidateRejected("wrong_localization", "Le patch sort de la localisation prouvée.")
                        diagnostic.changed_files, diagnostic.lines_changed = changed, lines
                        diagnostic.patch_bounds_passed = True
                        compilation = self._call_runner(self.compile_runner, worktree.path, self.max_fast_gate_seconds, changed)
                        if not compilation.get("passed"):
                            raise CandidateRejected("compilation", "Compilation des fichiers modifiés échouée.")
                        diagnostic.compilation_passed = True
                        if self.targeted_test_runner:
                            test_started = self.clock()
                            targeted = self._call_runner(self.targeted_test_runner, worktree.path, changed, self.max_fast_gate_seconds)
                            if not self._record_pytest_result(
                                diagnostic, "targeted_tests", targeted, self.clock() - test_started,
                            ):
                                raise CandidateRejected("targeted_tests", "Tests ciblés locaux échoués.")
                        else:
                            diagnostic.targeted_tests_passed = True
                        if self.clock() - fast_started > self.max_fast_gate_seconds:
                            raise CandidateRejected("timeout", "Budget Fast Gate dépassé.")
                        if self.representative_runner:
                            representative = self._call_runner(self.representative_runner, worktree.path, cause.representative_public_scenario_ids, self.max_fast_gate_seconds)
                            after = {item.scenario_id: item for item in representative.results}
                            if not all(key in after for key in cause.representative_public_scenario_ids):
                                raise CandidateRejected("representative_scenarios", "Résultat représentatif incomplet.")
                            diagnostic.representative_scenarios_passed = all(after[key].passed for key in cause.representative_public_scenario_ids)
                            component_reached, symbol_reached, reach_evidence = self.code_localizer.reached(target_localization, representative)
                            diagnostic.target_component_reached = component_reached
                            diagnostic.target_symbol_reached = symbol_reached
                            diagnostic.evidence_used.extend(reach_evidence)
                            if not component_reached or not symbol_reached:
                                raise CandidateRejected("wrong_localization", "Le composant ou symbole localisé n'est pas exécuté par les scénarios représentatifs.")
                            metrics.localization_reached_candidates += 1
                            self._populate_behavior_diagnostic(
                                diagnostic, train, representative,
                                cause.representative_public_scenario_ids, cause,
                            )
                            diagnostic.behavior_changed = self._behavior_changed(train, representative, cause.representative_public_scenario_ids)
                            if not diagnostic.behavior_changed:
                                raise CandidateRejected("no_behavior_change", "Aucun scénario représentatif ciblé n'a changé.")
                            if not diagnostic.representative_scenarios_passed:
                                raise CandidateRejected("behavior_changed_but_not_fixed", "La trace change, mais le contrat représentatif reste en échec.")
                        else:
                            # Sans runner représentatif dédié, préserver l'ordre V1 :
                            # pytest élimine le candidat avant tout benchmark.
                            test_started = self.clock()
                            tests = self.test_runner(worktree.path, self.max_candidate_seconds)
                            if not self._record_pytest_result(
                                diagnostic, "full_tests", tests, self.clock() - test_started,
                            ):
                                raise CandidateRejected("full_tests", "Suite pytest complète échouée.")
                            representative = self.benchmark_runner(worktree.path, self.max_candidate_seconds)
                            component_reached, symbol_reached, reach_evidence = self.code_localizer.reached(target_localization, representative)
                            diagnostic.target_component_reached = component_reached
                            diagnostic.target_symbol_reached = symbol_reached
                            diagnostic.evidence_used.extend(reach_evidence)
                            if not component_reached or not symbol_reached:
                                raise CandidateRejected("wrong_localization", "Le composant ou symbole localisé n'est pas exécuté par les scénarios représentatifs.")
                            metrics.localization_reached_candidates += 1
                            self._populate_behavior_diagnostic(
                                diagnostic, train, representative,
                                cause.representative_public_scenario_ids, cause,
                            )
                            diagnostic.behavior_changed = self._behavior_changed(train, representative, cause.representative_public_scenario_ids)
                            after = {item.scenario_id: item for item in representative.results}
                            diagnostic.representative_scenarios_passed = all(
                                after.get(key) is not None and after[key].passed
                                for key in cause.representative_public_scenario_ids
                            )
                            if not diagnostic.behavior_changed:
                                raise CandidateRejected("no_behavior_change", "Aucun scénario représentatif ciblé n'a changé.")
                            if not diagnostic.representative_scenarios_passed:
                                raise CandidateRejected("behavior_changed_but_not_fixed", "La trace change, mais le contrat représentatif reste en échec.")
                        self._fast_gate_durations.append(self.clock() - fast_started)
                        if self.representative_runner:
                            test_started = self.clock()
                            tests = self.test_runner(worktree.path, self.max_candidate_seconds)
                            if not self._record_pytest_result(
                                diagnostic, "full_tests", tests, self.clock() - test_started,
                            ):
                                raise CandidateRejected("full_tests", "Suite pytest complète échouée.")
                        full_started = self.clock()
                        candidate_report = representative if not self.representative_runner else self.benchmark_runner(worktree.path, self.max_candidate_seconds)
                        self._full_durations.append(self.clock() - full_started)
                        metrics.candidates_evaluated += 1
                        metrics.candidates_reaching_train += 1
                        metrics.candidates_reaching_validation += int(any(i.split == "validation" for i in candidate_report.results))
                        metrics.candidates_reaching_holdout += int(any(i.split == "holdout" for i in candidate_report.results))
                        metrics.candidates_reaching_full_evaluation += 1
                        for split in ("train", "validation", "holdout"):
                            before, after_score = _split_score(baseline, split), _split_score(candidate_report, split)
                            setattr(diagnostic, f"{split}_score_before", before)
                            setattr(diagnostic, f"{split}_score_after", after_score)
                            setattr(diagnostic, f"{split}_gain", round(after_score - before, 3) if before is not None and after_score is not None else None)
                        diagnostic.security_score_before, diagnostic.security_score_after = baseline.security_score, candidate_report.security_score
                        decision = decide_acceptance(
                            baseline, candidate_report, tests_passed=True, coverage=tests.get("coverage"),
                            coverage_threshold=self.coverage_threshold, minimum_improvement=self.minimum_improvement,
                        )
                        diagnostic.true_security_regressions_count = decision.security_changes.get("true_regressions", 0)

                        if not decision.accepted:
                            diagnostic.bundle_composable = (
                                len(decision.reasons) == 1
                                and decision.reasons[0].startswith("Gain global insuffisant")
                                and diagnostic.full_tests_passed is True
                                and diagnostic.validation_gain is not None
                                and diagnostic.validation_gain >= 0
                                and diagnostic.holdout_gain is not None
                                and diagnostic.holdout_gain >= 0
                                and diagnostic.true_security_regressions_count == 0
                            )

                            reason = " ".join(decision.reasons)

                            stage = "security" if diagnostic.true_security_regressions_count else (
                                "validation" if diagnostic.validation_gain is not None and diagnostic.validation_gain < 0 else
                                "holdout" if diagnostic.holdout_gain is not None and diagnostic.holdout_gain < 0 else
                                "insufficient_gain"
                            )

                            raise CandidateRejected(stage, reason)
                        commit = self.git.commit(worktree, f"self-repair: {operator.name}")
                        self.git.close_accepted(worktree)
                        worktree = None
                        diagnostic.outcome, diagnostic.total_duration_seconds = "accepted", round(self.clock() - candidate_started, 3)
                        metrics.local_repairs_accepted, metrics.codex_avoided = 1, 1
                        self.history.record({
                            "root_cause_id": cause.root_cause_id, "operator": operator.name,
                            "canonical_cause": cause.subtype, "baseline_commit": baseline.commit,
                            "patch_signature": signature, "outcome": "accepted", "files_changed": changed,
                            "behavioral_effect": True, "train_gain": diagnostic.train_gain,
                            "localized_target": diagnostic.localized_target,
                            "localization_confidence": diagnostic.localization_confidence,
                            "target_component_reached": diagnostic.target_component_reached,
                            "target_symbol_reached": diagnostic.target_symbol_reached,
                            "duration_seconds": diagnostic.total_duration_seconds,
                        })
                        return self._finish(
                            metrics, started, "Réparation locale acceptée.", causes, diagnostics, cycle=cycle,
                            candidate_report=candidate_report, commit=commit, changed_files=changed,
                            operator=operator.name, security_changes=decision.security_changes,
                        )
                    except CandidateRejected as error:
                        self._reject(diagnostic, metrics, error.stage, error.reason, candidate_started)

                        if diagnostic.bundle_composable:
                            composable_pool.append({
                                "cause": cause,
                                "operator": operator,
                                "candidate": candidate,
                                "diagnostic": diagnostic,
                                "signature": signature,
                                "target_localization": target_localization,
                            })
                    except (GitError, OSError, subprocess.SubprocessError, RuntimeError) as error:
                        self._reject(diagnostic, metrics, "internal_error", str(error), candidate_started)
                    finally:
                        if worktree is not None:
                            self.git.abandon(worktree)
        bundle_pair = self._select_bundle_pair(composable_pool)

        if bundle_pair is not None:
            first, second = bundle_pair
            members = (first, second)
            metrics.bundles_generated += 1
            metrics.bundle_member_total += 2
            bundle_root_cause_id, bundle_signature = self._bundle_identity(members)
            if self.history.was_rejected(bundle_root_cause_id, bundle_signature):
                self.logger("[SelfRepair][Bundle] Bundle identique déjà rejeté; nouvelle évaluation bloquée.")
                bundle_pair = None

        if bundle_pair is not None:
            first, second = bundle_pair
            members = (first, second)
            bundle_worktree = None
            bundle_started = self.clock()

            try:
                bundle_worktree = self.git.create(
                    next_cycle,
                    base_ref=base_ref,
                    safety=safety,
                )
                next_cycle = (bundle_worktree.cycle or next_cycle) + 1

                bundle_changed, bundle_lines = self._validate_bundle(
                    (first, second),
                    bundle_worktree,
                )
                for member in members:
                    diagnostic = member["diagnostic"]
                    diagnostic.patch_valid = True
                    diagnostic.patch_bounds_passed = True
                    diagnostic.changed_files = sorted({edit.path for edit in member["candidate"].edits})
                bundle_compilation = self._call_runner(
                    self.compile_runner,
                    bundle_worktree.path,
                    self.max_fast_gate_seconds,
                    bundle_changed,
                )

                if not bundle_compilation.get("passed"):
                    raise CandidateRejected(
                        "compilation",
                        "Compilation du bundle échouée.",
                    )
                for member in members:
                    member["diagnostic"].compilation_passed = True

                if self.targeted_test_runner:
                    targeted_started = self.clock()
                    bundle_targeted = self._call_runner(
                        self.targeted_test_runner,
                        bundle_worktree.path,
                        bundle_changed,
                        self.max_fast_gate_seconds,
                    )
                    targeted_duration = self.clock() - targeted_started
                    targeted_results = [
                        self._record_pytest_result(
                            member["diagnostic"], "targeted_tests",
                            bundle_targeted, targeted_duration,
                        )
                        for member in members
                    ]
                    if not all(targeted_results):
                        raise CandidateRejected(
                            "targeted_tests",
                            "Tests ciblés du bundle échoués.",
                        )
                else:
                    for member in members:
                        member["diagnostic"].targeted_tests_passed = True

                representative_ids = sorted({
                    scenario_id
                    for member in members
                    for scenario_id in member["cause"].representative_public_scenario_ids
                })
                if not self.representative_runner:
                    raise CandidateRejected(
                        "representative_scenarios",
                        "Runner représentatif requis pour évaluer un bundle.",
                    )
                representative = self._call_runner(
                    self.representative_runner, bundle_worktree.path,
                    representative_ids, self.max_fast_gate_seconds,
                )
                representative_by_id = {item.scenario_id: item for item in representative.results}
                if not all(identifier in representative_by_id for identifier in representative_ids):
                    raise CandidateRejected("representative_scenarios", "Résultat représentatif du bundle incomplet.")

                for member in members:
                    cause = member["cause"]
                    diagnostic = member["diagnostic"]
                    identifiers = cause.representative_public_scenario_ids
                    diagnostic.representative_scenarios_passed = all(
                        representative_by_id[identifier].passed for identifier in identifiers
                    )
                    component_reached, symbol_reached, evidence = self.code_localizer.reached(
                        member["target_localization"], representative,
                    )
                    diagnostic.target_component_reached = component_reached
                    diagnostic.target_symbol_reached = symbol_reached
                    diagnostic.evidence_used.extend(evidence)
                    if not component_reached or not symbol_reached:
                        raise CandidateRejected("wrong_localization", f"Le membre {diagnostic.candidate_id} n'atteint pas sa cible localisée.")
                    self._populate_behavior_diagnostic(diagnostic, train, representative, identifiers, cause)
                    diagnostic.behavior_changed = self._behavior_changed(train, representative, identifiers)
                    if not diagnostic.behavior_changed:
                        raise CandidateRejected("no_behavior_change", f"Le membre {diagnostic.candidate_id} ne change pas le comportement ciblé.")
                    if not diagnostic.representative_scenarios_passed:
                        raise CandidateRejected("behavior_changed_but_not_fixed", f"Le membre {diagnostic.candidate_id} ne corrige pas ses scénarios représentatifs.")

                tests_started = self.clock()
                tests = self.test_runner(bundle_worktree.path, self.max_candidate_seconds)
                tests_duration = self.clock() - tests_started
                for member in members:
                    self._record_pytest_result(member["diagnostic"], "full_tests", tests, tests_duration)
                if not tests.get("passed"):
                    raise CandidateRejected("full_tests", "Suite pytest complète du bundle échouée.")

                full_started = self.clock()
                candidate_report = self.benchmark_runner(bundle_worktree.path, self.max_candidate_seconds)
                self._full_durations.append(self.clock() - full_started)
                metrics.bundles_evaluated += 1
                for member in members:
                    diagnostic = member["diagnostic"]
                    for split in ("train", "validation", "holdout"):
                        before = _split_score(baseline, split)
                        after = _split_score(candidate_report, split)
                        setattr(diagnostic, f"{split}_score_before", before)
                        setattr(diagnostic, f"{split}_score_after", after)
                        setattr(diagnostic, f"{split}_gain", round(after - before, 3) if before is not None and after is not None else None)
                    diagnostic.security_score_before = baseline.security_score
                    diagnostic.security_score_after = candidate_report.security_score

                decision = decide_acceptance(
                    baseline, candidate_report, tests_passed=True,
                    coverage=tests.get("coverage"), coverage_threshold=self.coverage_threshold,
                    minimum_improvement=self.minimum_improvement,
                )
                for member in members:
                    member["diagnostic"].true_security_regressions_count = decision.security_changes.get("true_regressions", 0)
                if not decision.accepted:
                    raise CandidateRejected(
                        self._bundle_rejection_stage(decision, [member["diagnostic"] for member in members]),
                        " ".join(decision.reasons),
                    )

                operator_name = "bundle:" + "+".join(sorted(member["operator"].name for member in members))
                commit = self.git.commit(bundle_worktree, f"self-repair: {operator_name}")
                self.git.close_accepted(bundle_worktree)
                bundle_worktree = None
                metrics.bundles_accepted += 1
                metrics.local_repairs_accepted = 1
                metrics.codex_avoided = 1
                for member in members:
                    member["diagnostic"].outcome = "accepted_bundle_member"
                    member["diagnostic"].rejection_stage = None
                    member["diagnostic"].rejection_reason = ""
                    member["diagnostic"].total_duration_seconds = round(self.clock() - bundle_started, 3)
                self._record_bundle_history(members, baseline, outcome="accepted", changed=bundle_changed)
                return self._finish(
                    metrics, started, "Bundle local de 2 réparations accepté.", causes, diagnostics,
                    cycle=cycle, candidate_report=candidate_report, commit=commit,
                    changed_files=bundle_changed, operator=operator_name,
                    security_changes=decision.security_changes,
                )

            except CandidateRejected as error:
                metrics.bundles_rejected += 1
                self._record_bundle_history(
                    members, baseline, outcome="rejected", stage=error.stage,
                    reason=error.reason, changed=locals().get("bundle_changed", []),
                )
                self.logger(
                    "[SelfRepair][Bundle] Bundle rejeté pendant l'application : "
                    f"{error.stage} | {error.reason}"
                )

            except (
                GitError,
                OSError,
                subprocess.SubprocessError,
                RuntimeError,
            ) as error:
                metrics.bundles_rejected += 1
                self._record_bundle_history(
                    members, baseline, outcome="rejected", stage="internal_error",
                    reason=str(error), changed=locals().get("bundle_changed", []),
                )
                self.logger(
                    f"[SelfRepair][Bundle] Erreur interne : {error}"
                )

            finally:
                if bundle_worktree is not None:
                    self.git.abandon(bundle_worktree)


        metrics.repeated_failed_patch_rate = round(repeated / generated, 4) if generated else "not_available"
        metrics.codex_fallbacks = 1
        metrics.codex_fallback_reason = (
            "no_evidence_driven_candidate" if generated == 0 else "all_local_candidates_rejected"
        )
        return self._finish(metrics, started, "Tous les candidats locaux ont été rejetés.", causes, diagnostics, cycle=cycle)

    def _finish(self, metrics, started, reason, causes, diagnostics, *, cycle, **kwargs):
        metrics.repair_duration_seconds = round(self.clock() - started, 3)
        metrics.average_fast_gate_seconds = round(sum(self._fast_gate_durations) / len(self._fast_gate_durations), 3) if self._fast_gate_durations else 0.0
        metrics.average_full_evaluation_seconds = round(sum(self._full_durations) / len(self._full_durations), 3) if self._full_durations else 0.0
        metrics.percent_cycles_without_codex = 100.0 if metrics.codex_avoided else 0.0
        metrics.localization_success_rate = (
            round(metrics.localization_reached_candidates / metrics.localized_candidates, 4)
            if metrics.localized_candidates else "not_available"
        )
        result = RepairRunResult(
            bool(kwargs.get("commit")), metrics, reason, baseline_score=getattr(self, "_baseline_score", None),
            root_causes=causes, candidate_diagnostics=diagnostics,
            operator_decisions=getattr(self, "_operator_decisions", []), **kwargs,
        )
        try:
            write_self_repair_report(cycle, result.to_dict(), directory=self.report_directory)
        except OSError as error:
            self.logger(f"[SelfRepair] Rapport non écrit : {error}")
        return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="Analyse TRAIN sans créer de worktree.")
    parser.add_argument("--diagnose", action="store_true", help="Affiche les diagnostics comportementaux.")
    parser.add_argument("--report", action="store_true", help="Génère le rapport d'analyse.")
    parser.add_argument("--repair-verbose", action="store_true")
    parser.add_argument("--max-local-candidates", type=int, default=12)
    parser.add_argument("--repair-timeout", type=float, default=300)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.max_local_candidates < 1 or args.repair_timeout <= 0:
        raise SystemExit("Les budgets doivent être strictement positifs.")
    from .benchmark_runner import BenchmarkRunner
    root = Path(__file__).resolve().parents[1]
    benchmark = BenchmarkRunner(project_root=root)
    if args.analyze:
        report = benchmark.run(["train"])
        engine = SelfRepairEngine(
            root, max_total_candidates=args.max_local_candidates,
            timeout_seconds=args.repair_timeout, verbose=args.repair_verbose,
        )
        _train, causes, selected, compatible = engine.analyze(report)
        print(f"SelfRepair: {len(report.failures)} échecs TRAIN analysés")
        print(f"{len(causes)} causes racines; {len(compatible)} cas compatibles")
        for cause, operators in selected:
            print(f"- {cause.root_cause_id} {cause.subtype} confiance={cause.confidence:.3f} opérateurs={len(operators)}")
            if args.diagnose and cause.diagnosis:
                print(f"  divergence={cause.diagnosis.first_divergence_stage}: {cause.diagnosis.observed_behavior[:2]}")
        if args.report:
            metrics = RepairMetrics(
                failures_analyzed=len(report.failures), root_causes_detected=len(causes),
                compatible_failures=len(compatible),
            )
            write_self_repair_report(
                0, RepairRunResult(False, metrics, "Analyse uniquement.", root_causes=causes).to_dict(),
                directory=engine.report_directory,
            )
        return 0

    from .improvement_loop import (
        _compile_candidate, _coverage_threshold, _run_candidate_benchmark,
        _run_representative_benchmark, _run_targeted_tests, _run_tests,
    )
    from .scenario_loader import load_public_discoveries, load_scenarios, merge_public_scenarios

    baseline = benchmark.run()
    _version, primary = load_scenarios(benchmark.dataset_path)
    _public_version, public = load_public_discoveries(benchmark.public_discoveries_path)
    primary_ids = {scenario.id for scenario in primary}
    merged = merge_public_scenarios(primary, public)
    public_unique = [scenario for scenario in merged if scenario.id not in primary_ids]
    representative_train = [scenario for scenario in merged if scenario.split == "train"]
    logger = print if args.repair_verbose else (lambda _message: None)
    engine = SelfRepairEngine(
        root,
        compile_runner=_compile_candidate,
        targeted_test_runner=_run_targeted_tests,
        representative_runner=lambda path, ids, timeout: _run_representative_benchmark(
            path, ids, timeout, scenarios=representative_train,
        ),
        test_runner=_run_tests,
        benchmark_runner=lambda path, timeout: _run_candidate_benchmark(
            path, timeout, dynamic_scenarios=public_unique,
        ),
        coverage_threshold=_coverage_threshold(root),
        max_total_candidates=args.max_local_candidates,
        timeout_seconds=args.repair_timeout,
        logger=logger,
        verbose=args.repair_verbose,
    )
    result = engine.run(baseline, cycle=1)
    status = "ACCEPTÉ" if result.accepted else "REJETÉ"
    print(f"SelfRepair cycle 1: {status}")
    print(
        f"Candidats: générés={result.metrics.candidates_generated}, "
        f"évalués={result.metrics.candidates_evaluated}"
    )
    print(
        f"Bundles: générés={result.metrics.bundles_generated}, "
        f"évalués={result.metrics.bundles_evaluated}, "
        f"acceptés={result.metrics.bundles_accepted}, "
        f"rejetés={result.metrics.bundles_rejected}"
    )
    print(f"Raison: {result.reason}")
    if result.accepted and result.commit:
        print(f"Commit: {result.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
