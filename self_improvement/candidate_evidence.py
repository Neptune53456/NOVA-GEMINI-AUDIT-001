"""Preuve causale structurée pour une candidate d'amélioration.

Ce module ne décide jamais de l'acceptation. Il synthétise les mesures prises
dans les mêmes conditions avant et après le patch; le Judge reste autoritaire.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from self_improvement.judge_engine import JudgeMetrics, JudgeResult


class EvidenceOutcome(str, Enum):
    IMPROVED = "IMPROVED"
    NEUTRAL = "NEUTRAL"
    REGRESSED = "REGRESSED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class CandidateEvidence:
    baseline_test_results: dict[str, Any]
    candidate_test_results: dict[str, Any]
    baseline_behavior_metrics: dict[str, Any]
    candidate_behavior_metrics: dict[str, Any]
    changed_files: list[str] = field(default_factory=list)
    changed_symbols: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    unchanged_checks: list[str] = field(default_factory=list)
    evidence_strength: str = "insufficient"
    outcome: str = EvidenceOutcome.UNCERTAIN.value
    repair_attempted: bool = False
    repair_count: int = 0
    failure_before_repair: dict[str, Any] | None = None
    result_after_repair: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _test_snapshot(metrics: JudgeMetrics) -> dict[str, Any]:
    tests = metrics.tests
    return {
        "passed": tests.passed,
        "failed": tests.failed,
        "total": tests.total,
        "pass_rate": tests.pass_rate,
        "failed_test_names": list(tests.failed_test_names),
        "duration_seconds": tests.duration_seconds,
    }


def _behavior_snapshot(metrics: JudgeMetrics) -> dict[str, Any]:
    return {
        "compilation_ok": metrics.static_quality.compilation_ok,
        "syntax_errors": list(metrics.static_quality.syntax_errors),
        "benchmark_score": metrics.benchmark.score,
        "coverage_percent": metrics.coverage.percent,
        "safety_violations": metrics.benchmark.safety_violations,
        "false_successes": metrics.benchmark.false_successes,
    }


def changed_python_symbols(changes: Iterable[tuple[str, str, str]]) -> list[str]:
    """Retourne les symboles Python modifiés au niveau le plus précis disponible.

    Une méthode modifiée ne doit pas être réduite à sa classe parente. Ce détail
    est réutilisé par la réparation sémantique pour reconstruire une target
    canonique unique et éviter qu'un modèle local doive réinventer le symbole.
    """
    changed: list[str] = []

    def symbols(source: str) -> dict[str, str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {}

        result: dict[str, str] = {}

        def visit(body, prefix: str = "") -> None:
            for node in body:
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                result[qualified] = ast.dump(node, include_attributes=False)
                # Les méthodes/classes imbriquées portent l'information de cible
                # la plus utile pour un repair. Les fonctions imbriquées sont
                # également conservées avec leur nom qualifié.
                visit(getattr(node, "body", ()), qualified)

        visit(tree.body)
        return result

    for raw_path, before, after in changes:
        if Path(raw_path).suffix.casefold() != ".py":
            continue
        old, new = symbols(before), symbols(after)
        differing = {name for name in set(old) | set(new) if old.get(name) != new.get(name)}
        # Si un descendant précis diffère, supprimer le parent dont l'AST ne
        # diffère que parce qu'il contient ce descendant. Une modification de
        # classe propre (bases/décorateurs/attributs) reste reportée lorsqu'aucun
        # descendant ne suffit à l'expliquer.
        precise = {
            name for name in differing
            if not any(other.startswith(name + ".") for other in differing)
        }
        relative = Path(raw_path).as_posix()
        changed.extend(f"{relative}:{name}" for name in sorted(precise))
    return changed


def build_candidate_evidence(
    before: JudgeMetrics,
    after: JudgeMetrics,
    judge_result: JudgeResult,
    *,
    changes: Iterable[tuple[str, str, str]] = (),
    repair_attempted: bool = False,
    repair_count: int = 0,
    failure_before_repair: dict[str, Any] | None = None,
    result_after_repair: dict[str, Any] | None = None,
) -> CandidateEvidence:
    before_tests = _test_snapshot(before)
    after_tests = _test_snapshot(after)
    changed_rows = list(changes)
    changed_files = list(dict.fromkeys(Path(path).as_posix() for path, old, new in changed_rows if old != new))

    infra_markers = ("test_runner_crash", "timed out", "timeout", "test_infra_failure")
    infra_evidence = (
        isinstance(failure_before_repair, dict)
        and str(failure_before_repair.get("failure_type", "")).upper() == "TEST_INFRA_FAILURE"
    ) or any(
        any(marker in str(name).casefold() for marker in infra_markers)
        for name in [*before_tests.get("failed_test_names", []), *after_tests.get("failed_test_names", [])]
    )

    # Des métriques identiques produites par un timeout ou un crash du runner ne
    # constituent jamais une preuve forte de neutralité. Le Judge reste
    # autoritaire; CandidateEvidence marque seulement la preuve comme insuffisante.
    if infra_evidence:
        outcome = EvidenceOutcome.UNCERTAIN
        strength = "insufficient"
    elif judge_result.regressions:
        outcome = EvidenceOutcome.REGRESSED
        strength = "strong" if before.tests.failed < after.tests.failed else "moderate"
    elif judge_result.improvements:
        outcome = EvidenceOutcome.IMPROVED
        strength = "strong" if before.tests.failed > after.tests.failed else "moderate"
    elif before_tests == after_tests and _behavior_snapshot(before) == _behavior_snapshot(after):
        outcome = EvidenceOutcome.NEUTRAL
        strength = "strong" if before.tests.total > 0 else "insufficient"
    else:
        outcome = EvidenceOutcome.UNCERTAIN
        strength = "insufficient"

    unchanged: list[str] = []
    if before.tests.total == after.tests.total:
        unchanged.append("test_count")
    if before.static_quality.compilation_ok == after.static_quality.compilation_ok:
        unchanged.append("compilation_status")
    if before.benchmark.safety_violations == after.benchmark.safety_violations:
        unchanged.append("safety_violations")

    return CandidateEvidence(
        baseline_test_results=before_tests,
        candidate_test_results=after_tests,
        baseline_behavior_metrics=_behavior_snapshot(before),
        candidate_behavior_metrics=_behavior_snapshot(after),
        changed_files=changed_files,
        changed_symbols=changed_python_symbols(changed_rows),
        regressions=list(judge_result.regressions),
        improvements=list(judge_result.improvements),
        unchanged_checks=unchanged,
        evidence_strength=strength,
        outcome=outcome.value,
        repair_attempted=repair_attempted,
        repair_count=repair_count,
        failure_before_repair=failure_before_repair,
        result_after_repair=result_after_repair,
    )
