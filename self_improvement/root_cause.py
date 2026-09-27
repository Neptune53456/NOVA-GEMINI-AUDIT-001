"""Diagnostic déterministe des causes racines à partir des seuls cas publics TRAIN."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Protocol

from .models import BenchmarkReport, ScenarioResult


PUBLIC_SPLITS = frozenset({"train"})


@dataclass(frozen=True)
class BehavioralDiagnosis:
    expected_behavior: list[str]
    observed_behavior: list[str]
    divergence_type: str
    first_divergence_stage: str
    suspected_component: str | None
    confidence: float
    evidence: list[str]


@dataclass(frozen=True)
class RootCause:
    root_cause_id: str
    category: str
    subtype: str
    confidence: float
    affected_failure_count: int
    representative_public_scenario_ids: list[str]
    symptoms: list[str]
    evidence: list[str]
    suspected_files: list[str]
    suspected_symbols: list[str]
    compatible_repair_types: list[str]
    likely_behavioral_layer: str
    reason: str
    uncertainty_reason: str
    generated_at: str
    confidence_reason: str = ""
    diagnosis: BehavioralDiagnosis | None = None
    public_families: list[str] = field(default_factory=list)
    scenario_variant_ids: list[str] = field(default_factory=list)
    raw_group_count: int = 1
    localization: Any | None = None
    cases: list[ScenarioResult] = field(default_factory=list, repr=False, compare=False)


class RootCauseAdvisor(Protocol):
    """Extension future en lecture seule; la décision déterministe reste autoritaire."""

    def advise(self, public_evidence: dict[str, Any]) -> dict[str, Any]: ...


_CATEGORY_DEFAULTS = {
    "confirmations": ("confirmation_detection", "confirmation.ambiguous_positive_marker", "system_action_controller", ["confirmation", "text_normalization"]),
    "suppression": ("intent_detection", "deletion.confirmation_detection_gap", "request_interpreter", ["intent_alias"]),
    "documents": ("document_routing", "document.extensionless_routing", "document_command_router", ["document_routing", "regex_repair"]),
    "pièces jointes": ("attachment_resolution", "document.attachment_correction_priority", "document_command_router", ["document_routing", "context_priority", "regex_repair"]),
    "compréhension contextuelle": ("context_resolution", "context.last_file_vs_attachment", "request_interpreter", ["context_priority"]),
    "ambiguïtés": ("context_resolution", "context.stale_reference", "request_interpreter", ["context_priority", "text_normalization"]),
    "réponses utilisateur très courtes": ("normalization", "text.normalization_gap", "request_interpreter", ["text_normalization", "intent_alias"]),
    "création fichiers/dossiers": ("intent_detection", "intent.alias_missing", "request_interpreter", ["intent_alias"]),
    "OCR simulé": ("document_routing", "document.extensionless_routing", "document_command_router", ["document_routing"]),
}


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return re.sub(r"\s+", " ", "".join(ch for ch in text if not unicodedata.combining(ch))).strip()


def _failed_contracts(case: ScenarioResult) -> list[str]:
    return sorted({_normalized(item.name) for item in case.criteria if not item.passed and item.name})


def _trace_signal(case: ScenarioResult, *keys: str) -> str:
    for key in keys:
        value = case.trace.get(key)
        if value not in (None, "", [], {}):
            return _normalized(value)
    steps = case.trace.get("steps")
    if isinstance(steps, list) and steps and isinstance(steps[-1], dict):
        for key in keys:
            value = steps[-1].get(key)
            if value not in (None, "", [], {}):
                return _normalized(value)
    return ""


def _classify(case: ScenarioResult) -> tuple[str, str, str | None, list[str]]:
    stage, subtype, component, repairs = _CATEGORY_DEFAULTS.get(
        case.category, ("response_generation", f"{_normalized(case.category).replace(' ', '_')}.behavior_gap", None, [])
    )
    tags = {_normalized(tag) for tag in case.tags}
    contracts = " ".join(_failed_contracts(case))
    error_code = _trace_signal(case, "error_code", "code")
    if "active-attachment" in tags or "attachment" in contracts:
        stage, subtype = "attachment_resolution", "context.last_file_vs_attachment"
    if "last-file" in tags and "active-attachment" not in tags:
        stage, subtype = "context_resolution", "context.stale_reference"
    if "mandatory-confirmation" in tags:
        stage, subtype = "confirmation_detection", "confirmation.ambiguous_positive_marker"
    if "extensionless" in tags:
        stage, subtype = "document_routing", "document.extensionless_routing"
    if error_code:
        subtype = f"{subtype}.error_{re.sub(r'[^a-z0-9]+', '_', error_code).strip('_')}"
    return stage, subtype, component, repairs


def _contract_signature(case: ScenarioResult) -> list[tuple[str, str, str]]:
    return sorted(
        (_normalized(item.name), _normalized(item.expected), _normalized(item.actual))
        for item in case.criteria if not item.passed
    )


def raw_cause_signature(case: ScenarioResult) -> str:
    """Groupe brut conservant la provenance de famille pour mesurer la fragmentation."""
    stage, subtype, _component, _repairs = _classify(case)
    family = next((tag for tag in case.tags if tag.startswith(("family:", "discovery-family:"))), "")
    payload = {
        "subtype": subtype,
        "stage": stage,
        "contracts": _contract_signature(case),
        "error": _trace_signal(case, "error_code", "code"),
        "intent": _trace_signal(case, "intent", "action", "kind"),
        "router": _trace_signal(case, "router", "route"),
        "family": _normalized(family),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def cause_signature(case: ScenarioResult) -> str:
    """Clé canonique comportementale; la famille reste une provenance, pas une cause."""
    stage, subtype, _component, _repairs = _classify(case)
    payload = {
        "subtype": subtype,
        "stage": stage,
        "contracts": _contract_signature(case),
        "observed": sorted(
            f"{item.name}={item.actual!r}" for item in case.criteria if not item.passed
        ),
        "expected": sorted(
            f"{item.name}={item.expected!r}" for item in case.criteria if not item.passed
        ),
        "component": _trace_signal(case, "component", "module", "router"),
        "error": _trace_signal(case, "error_code", "code"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def diagnose(cases: list[ScenarioResult]) -> BehavioralDiagnosis:
    expected, observed, evidence = [], [], []
    stages = []
    components = []
    for case in cases:
        stage, _subtype, component, _repairs = _classify(case)
        stages.append(stage)
        if component:
            components.append(component)
        for criterion in case.criteria:
            if criterion.passed:
                continue
            expected.append(f"{criterion.name}={criterion.expected!r}")
            observed.append(f"{criterion.name}={criterion.actual!r}")
            evidence.append(f"{case.scenario_id}: contrat {criterion.name} non respecté")
        if case.error:
            observed.append(f"error={case.error}")
            evidence.append(f"{case.scenario_id}: erreur d'exécution observée")
    stage = max(set(stages), key=lambda value: (stages.count(value), value)) if stages else "response_generation"
    component = max(set(components), key=lambda value: (components.count(value), value)) if components else None
    agreement = stages.count(stage) / len(stages) if stages else 0.0
    confidence = round(min(1.0, 0.35 + 0.25 * agreement + 0.1 * min(len(cases), 3) + (0.1 if evidence else 0)), 3)
    return BehavioralDiagnosis(
        sorted(set(expected)), sorted(set(observed)), "contract_mismatch", stage,
        component, confidence, evidence[:20],
    )


def _representatives(cases: list[ScenarioResult], maximum: int = 4) -> list[str]:
    chosen, seen_shapes = [], set()
    ordered = sorted(cases, key=lambda case: (-case.weight, case.scenario_id))
    for case in ordered:
        shape = (len(case.trace.get("steps", [])), tuple(sorted(case.tags)), bool(case.error))
        if shape not in seen_shapes:
            chosen.append(case.scenario_id)
            seen_shapes.add(shape)
        if len(chosen) >= maximum:
            break
    for case in ordered:
        if len(chosen) >= min(maximum, len(ordered)):
            break
        if case.scenario_id not in chosen:
            chosen.append(case.scenario_id)
    return chosen


class RootCauseEngine:
    def __init__(self):
        self.last_metrics = {
            "raw_failure_groups": 0, "canonical_root_causes": 0,
            "merged_root_cause_groups": 0,
        }

    def analyze(self, report: BenchmarkReport) -> list[RootCause]:
        grouped: dict[str, list[ScenarioResult]] = {}
        raw_groups = set()
        for failure in report.failures:
            if failure.split not in PUBLIC_SPLITS:
                continue
            raw_groups.add(raw_cause_signature(failure))
            grouped.setdefault(cause_signature(failure), []).append(failure)
        self.last_metrics = {
            "raw_failure_groups": len(raw_groups),
            "canonical_root_causes": len(grouped),
            "merged_root_cause_groups": max(0, len(raw_groups) - len(grouped)),
        }
        causes = []
        now = datetime.now(timezone.utc).isoformat()
        for signature, cases in grouped.items():
            diagnosis = diagnose(cases)
            stage, subtype, _component, _repairs = _classify(cases[0])
            repair_sets = [set(_classify(case)[3]) for case in cases]
            repairs = sorted(set.intersection(*repair_sets)) if repair_sets else []
            same_stage = sum(_classify(case)[0] == stage for case in cases)
            same_contract = len({tuple(_failed_contracts(case)) for case in cases}) == 1
            trace_evidence = any(case.trace for case in cases)
            score = 0.25 + 0.2 * (same_stage / len(cases)) + 0.15 * min(len(cases), 3) / 3
            score += 0.15 if same_contract else 0.0
            score += 0.15 if trace_evidence else 0.0
            score = round(min(score, 0.95), 3)
            evidence = list(diagnosis.evidence)
            evidence.append(f"{same_stage}/{len(cases)} cas divergent à l'étape {stage}")
            families = sorted({
                tag for case in cases for tag in case.tags
                if tag.startswith(("family:", "discovery-family:"))
            })
            raw_count = len({raw_cause_signature(case) for case in cases})
            causes.append(RootCause(
                root_cause_id=f"rc-{signature}", category=cases[0].category, subtype=subtype,
                confidence=score, affected_failure_count=len(cases),
                representative_public_scenario_ids=_representatives(cases),
                symptoms=sorted(set(_failed_contracts(case)[0] if _failed_contracts(case) else (case.error or "échec comportemental") for case in cases)),
                evidence=evidence, suspected_files=[], suspected_symbols=[],
                compatible_repair_types=repairs, likely_behavioral_layer=stage,
                reason=(
                    "Signature canonique fondée sur sous-type, étape, contrat attendu/observé "
                    f"et composant public lorsqu'il est connu ({signature})."
                ),
                uncertainty_reason="" if score >= 0.7 else "Preuves ou répétitions insuffisantes pour localiser un symbole avec sûreté.",
                generated_at=now,
                confidence_reason=f"étape cohérente {same_stage}/{len(cases)}; contrat cohérent={same_contract}; trace disponible={trace_evidence}",
                diagnosis=diagnosis, public_families=families,
                scenario_variant_ids=sorted(case.scenario_id for case in cases),
                raw_group_count=raw_count, cases=list(cases),
            ))
        def priority(cause: RootCause) -> tuple:
            tags = {_normalized(tag) for case in cause.cases for tag in case.tags}
            source_rank = (
                0 if any("real-bug" in tag or "bug-reel" in tag for tag in tags) else
                1 if any("confirmed" in tag or "red-team" in tag for tag in tags) else
                3 if any("synthetic" in tag or "variant" in tag for tag in tags) else 2
            )
            weighted_impact = sum(case.weight * (100 - case.score) / 100 for case in cause.cases)
            return source_rank, -weighted_impact, -cause.confidence, cause.root_cause_id
        return sorted(causes, key=priority)
