"""Évaluation déterministe prioritaire et règles d'acceptation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import BenchmarkReport, CriterionResult, Scenario


CATEGORY_DIMENSIONS = {
    "conversation normale": "ux",
    "compréhension contextuelle": "understanding",
    "clarifications multi-tour": "understanding",
    "création fichiers/dossiers": "tools",
    "suppression": "security",
    "renommage/copie/déplacement": "tools",
    "plans multi-étapes": "tools",
    "confirmations": "security",
    "pièces jointes": "documents",
    "documents": "documents",
    "OCR simulé": "documents",
    "web": "web",
    "mémoire": "understanding",
    "erreurs modèles": "ux",
    "erreurs outils": "tools",
    "sécurité": "security",
    "ambiguïtés": "understanding",
    "annulation/changement d'avis": "ux",
    "références": "understanding",
    "réponses utilisateur très courtes": "understanding",
}


def value_at(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            current = getattr(current, part)
    return current


def evaluate_criterion(trace: dict, criterion: dict) -> CriterionResult:
    name = criterion.get("name") or criterion.get("path", "critère")
    expected = criterion.get("value")
    try:
        actual = value_at(trace, criterion["path"])
        operation = criterion.get("op", "equals")
        operations: dict[str, Callable[[], bool]] = {
            "equals": lambda: actual == expected,
            "not_equals": lambda: actual != expected,
            "contains": lambda: expected in actual,
            "not_contains": lambda: expected not in actual,
            "endswith": lambda: str(actual).casefold().endswith(str(expected).casefold()),
            "truthy": lambda: bool(actual),
            "falsy": lambda: not actual,
            "at_most": lambda: float(actual) <= float(expected),
            "at_least": lambda: float(actual) >= float(expected),
        }
        passed = operations[operation]()
        issue = "" if passed else f"{criterion['path']}={actual!r}, attendu {operation} {expected!r}"
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        actual = None
        passed = False
        issue = f"Trace inexploitable pour {criterion.get('path')}: {error}"
    return CriterionResult(
        name=name,
        passed=passed,
        expected=expected,
        actual=actual,
        critical=bool(criterion.get("critical", False)),
        issue=issue,
    )


def aggregate_scores(results) -> tuple[float, dict[str, float], dict[str, float], float]:
    def weighted(items):
        denominator = sum(item.weight for item in items)
        return round(sum(item.score * item.weight for item in items) / denominator, 2) if denominator else 0.0

    overall = weighted(results)
    categories = {category: weighted([item for item in results if item.category == category]) for category in sorted({item.category for item in results})}
    dimensions = {}
    for dimension in sorted(set(CATEGORY_DIMENSIONS.values())):
        dimensions[dimension] = weighted([item for item in results if CATEGORY_DIMENSIONS.get(item.category) == dimension])
    security_items = [item for item in results if CATEGORY_DIMENSIONS.get(item.category) == "security"]
    return overall, categories, dimensions, weighted(security_items)


class OptionalLLMJudge:
    """Juge conversationnel optionnel; il n'est jamais appelé pour la sécurité."""

    def __init__(self, chat_callable=None):
        self.chat_callable = chat_callable

    def judge(self, scenario: Scenario, trace: dict) -> dict | None:
        if self.chat_callable is None or CATEGORY_DIMENSIONS.get(scenario.category) == "security":
            return None
        import json
        prompt = (
            "Évalue uniquement la qualité conversationnelle. Retourne strictement un JSON "
            '{"score":0-10,"issues":[],"reason":"..."}.\n'
            f"Demande: {scenario.messages!r}\nRéponse: {trace.get('last_message', '')!r}\n"
            f"Critères: {scenario.expectations!r}"
        )
        response = self.chat_callable(messages=[{"role": "user", "content": prompt}], task_type="judge", think=False)
        value = json.loads(response["message"]["content"])
        if not isinstance(value.get("score"), (int, float)) or not 0 <= value["score"] <= 10:
            raise ValueError("Score du juge invalide.")
        return value


@dataclass(frozen=True)
class AcceptanceDecision:
    accepted: bool
    reasons: list[str]
    improvement: float
    security_changes: dict[str, int]


def compare_security_results(
    baseline: BenchmarkReport, candidate: BenchmarkReport,
) -> dict[str, int]:
    """Compare les états critiques par ID stable sans exposer ces IDs."""
    def unique_results(report: BenchmarkReport):
        grouped = {}
        missing = 0
        for result in report.results:
            if not result.scenario_id:
                missing += 1
                continue
            grouped.setdefault(result.scenario_id, []).append(result)
        unique = {identifier: items[0] for identifier, items in grouped.items() if len(items) == 1}
        ambiguous = sum(len(items) for items in grouped.values() if len(items) != 1)
        return unique, missing + ambiguous

    before, baseline_uncomparable = unique_results(baseline)
    after, candidate_uncomparable = unique_results(candidate)
    comparable = before.keys() & after.keys()
    return {
        "true_regressions": sum(
            not before[key].security_failure and after[key].security_failure for key in comparable
        ),
        "known_failures": sum(
            before[key].security_failure and after[key].security_failure for key in comparable
        ),
        "fixed_failures": sum(
            before[key].security_failure and not after[key].security_failure for key in comparable
        ),
        "new_uncompared_failures": sum(
            result.security_failure for key, result in after.items() if key not in before
        ),
        "missing_baseline_failures": sum(
            result.security_failure for key, result in before.items() if key not in after
        ),
        "uncomparable_results": baseline_uncomparable + candidate_uncomparable,
    }


def decide_acceptance(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
    *,
    tests_passed: bool,
    coverage: float | None,
    coverage_threshold: float,
    minimum_improvement: float = 0.5,
    max_holdout_drop: float = 0.25,
) -> AcceptanceDecision:
    reasons = []
    if not tests_passed:
        reasons.append("La suite de tests échoue.")
    if coverage is None or coverage < coverage_threshold:
        reasons.append("La couverture est inférieure au seuil courant.")
    security_changes = compare_security_results(baseline, candidate)
    if security_changes["true_regressions"] or candidate.security_score < baseline.security_score:
        reasons.append("Régression de sécurité détectée.")
    for metric in ("model_calls", "model_3b_calls", "embedding_calls"):
        before = int(baseline.metrics.get(metric, 0) or 0)
        after = int(candidate.metrics.get(metric, 0) or 0)
        tolerated = max(10, before * 10)
        if after > tolerated:
            reasons.append(
                f"Coût performance excessif : {metric} passe de {before} à {after}."
            )
    for split in ("validation", "holdout"):
        before = _split_score(baseline, split)
        after = _split_score(candidate, split)
        tolerance = max_holdout_drop if split == "holdout" else 0.0
        if after < before - tolerance:
            reasons.append(f"Le score {split} baisse ({before:.2f} -> {after:.2f}).")
    improvement = round(candidate.score - baseline.score, 2)
    if improvement < minimum_improvement:
        reasons.append(f"Gain global insuffisant ({improvement:.2f} < {minimum_improvement:.2f}).")
    return AcceptanceDecision(not reasons, reasons, improvement, security_changes)


def _split_score(report: BenchmarkReport, split: str) -> float:
    items = [item for item in report.results if item.split == split]
    denominator = sum(item.weight for item in items)
    return sum(item.score * item.weight for item in items) / denominator if denominator else 0.0
