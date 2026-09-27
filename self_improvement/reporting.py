"""Rapports JSON et Markdown atomiques et lisibles."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import tempfile
from collections import Counter
from datetime import datetime, timezone


REPORTS_DIR = Path(__file__).with_name("reports")


def _jsonable(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    return value


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(_jsonable(payload), ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def write_text_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)
    return path


def write_baseline(report, *, path=None, tests=None) -> Path:
    path = Path(path or REPORTS_DIR / "baseline.json")
    payload = report.to_dict()
    payload["tests"] = tests or report.tests
    write_json(path, payload)
    return path


def write_cycle_report(cycle: int, payload: dict, *, directory=None) -> tuple[Path, Path]:
    directory = Path(directory or REPORTS_DIR)
    json_path = directory / f"cycle_{cycle:03d}.json"
    markdown_path = directory / f"cycle_{cycle:03d}.md"
    write_json(json_path, payload)
    baseline = payload.get("baseline", {})
    candidate = payload.get("candidate") or {}
    repair = payload.get("self_repair") or {}
    repair_metrics = repair.get("metrics") or {}
    lines = [
        f"# Cycle {cycle:03d}", "",
        f"- Décision : **{payload.get('decision', 'UNKNOWN')}**",
        f"- Motif : {payload.get('reason', '')}",
        f"- Baseline : {baseline.get('score', 'n/a')}/100",
        f"- Candidat : {candidate.get('score', 'n/a')}/100",
        f"- Durée : {payload.get('duration_seconds', 0)} s",
        f"- Fichiers modifiés : {', '.join(payload.get('changed_files', [])) or 'aucun'}",
        "", "## Tests", "", f"`{payload.get('tests', {})}`", "",
        "## Échecs corrigés", "", *[f"- {item}" for item in payload.get("fixed_failures", [])],
        "", "## Nouveaux échecs", "", *[f"- {item}" for item in payload.get("new_failures", [])], "",
    ]
    if repair:
        lines.extend([
            "## SelfRepair", "",
            f"- Échecs analysés : {repair_metrics.get('failures_analyzed', 0)}",
            f"- Causes racines : {repair_metrics.get('root_causes_detected', 0)}",
            f"- Cas compatibles : {repair_metrics.get('compatible_failures', 0)}",
            f"- Candidats testés : {repair_metrics.get('candidates_evaluated', 0)}",
            f"- Réparations acceptées : {repair_metrics.get('local_repairs_accepted', 0)}",
            f"- Codex évité : {'oui' if repair_metrics.get('codex_avoided') else 'non'}", "",
        ])
    git_session = payload.get("git_preflight") or {}
    if git_session:
        validated = git_session.get("validated_session_changes", [])
        blocked = git_session.get("human_or_unknown", [])
        lines.extend([
            "## Git session", "",
            f"- Provenance vérifiée : {'oui' if git_session.get('session_provenance_verified') else 'non'}",
            f"- Fichiers validés : {len(validated)}",
            f"- Fichiers bloqués : {len(blocked)}",
            f"- Auto-commit : {'oui' if git_session.get('session_auto_commit') else 'non'}",
            f"- Commit : {git_session.get('commit') or 'null'}",
            f"- Raison si refus : {git_session.get('session_reason') or git_session.get('error') or 'aucune'}", "",
        ])
    write_text_atomic(markdown_path, "\n".join(lines))
    return json_path, markdown_path


def _recommend(metrics: dict, causes: list[dict], candidates: list[dict]) -> str:
    weak = [cause for cause in causes if float(cause.get("confidence", 0)) < 0.7]
    if weak:
        return f"Améliorer l’instrumentation de {weak[0].get('root_cause_id')} : la preuve est insuffisante."
    unsupported = [cause for cause in causes if not cause.get("compatible_repair_types")]
    if unsupported:
        return f"Étudier un support borné pour {unsupported[0].get('root_cause_id')}, sans élargir les permissions."
    stages = Counter(item.get("rejection_stage") for item in candidates if item.get("rejection_stage"))
    if stages:
        stage, count = stages.most_common(1)[0]
        return f"Analyser les {count} rejets au stage {stage} avant de générer de nouveaux candidats."
    if not causes:
        return "Aucun travail nécessaire : aucun échec TRAIN public n’a été détecté."
    return "Conserver les seuils actuels et enrichir les preuves comportementales publiques."


def _aggregate_top_causes(causes: list[dict]) -> list[tuple[str, int, int, int]]:
    """Dédoublonne les sous-types pour ne pas confondre variantes et causes."""
    totals: dict[str, list[int]] = {}
    for cause in causes:
        subtype = str(cause.get("subtype") or "cause_inconnue")
        current = totals.setdefault(subtype, [0, 0, 0])
        current[0] += int(cause.get("affected_failure_count", 0))
        variants = cause.get("scenario_variant_ids")
        current[1] += len(variants) if isinstance(variants, list) else int(cause.get("affected_failure_count", 0))
        current[2] += int(cause.get("raw_group_count", 1))
    return sorted(
        ((subtype, values[0], values[1], values[2]) for subtype, values in totals.items()),
        key=lambda item: (-item[1], -item[2], -item[3], item[0]),
    )


def _previous_metrics(directory: Path, cycle: int) -> dict | None:
    previous = directory / f"self_repair_cycle_{cycle - 1:03d}.json"
    if cycle <= 1 or not previous.exists():
        return None
    try:
        return json.loads(previous.read_text(encoding="utf-8")).get("metrics", {})
    except (OSError, json.JSONDecodeError):
        return None


def _longitudinal_kpis(directory: Path, current: dict) -> dict:
    history = []
    for path in sorted(directory.glob("self_repair_cycle_*.json")):
        try:
            metrics = json.loads(path.read_text(encoding="utf-8")).get("metrics")
        except (OSError, json.JSONDecodeError):
            metrics = None
        if isinstance(metrics, dict):
            history.append(metrics)
    history.append(current)
    candidates = sum(int(item.get("candidates_generated", 0)) for item in history)
    accepted = sum(int(item.get("local_repairs_accepted", 0)) for item in history)
    duration = sum(float(item.get("repair_duration_seconds", 0)) for item in history)
    return {
        "real_bug_resolution_rate": "not_available",
        "root_cause_resolution_rate": "not_available",
        "local_repair_success_rate": round(accepted / candidates, 4) if candidates else "not_available",
        "percent_cycles_without_codex": round(100 * sum(not item.get("codex_fallbacks") for item in history) / len(history), 2) if history else "not_available",
        "average_candidates_per_success": round(candidates / accepted, 3) if accepted else "not_available",
        "average_time_per_success": round(duration / accepted, 3) if accepted else "not_available",
        "true_security_regression_rate": "not_available",
        "holdout_generalization_rate": "not_available",
        "repeated_bug_rate": "not_available",
        "repeated_failed_patch_rate": current.get("repeated_failed_patch_rate", "not_available"),
        "operator_acceptance_rate": "not_available",
    }


def write_self_repair_report(cycle: int, payload: dict, *, directory=None) -> tuple[Path, Path, Path]:
    """Écrit le rapport détaillé et le résumé copiable, sans détail holdout."""
    directory = Path(directory or REPORTS_DIR)
    metrics = payload.get("metrics", {})
    causes = payload.get("root_causes", [])
    candidates = payload.get("candidate_diagnostics", [])
    operator_decisions = payload.get("operator_decisions", [])
    recommendation = _recommend(metrics, causes, candidates)
    kpis = _longitudinal_kpis(directory, metrics)
    status = "ACCEPTED" if payload.get("accepted") else "REJECTED"
    now = datetime.now(timezone.utc).isoformat()
    json_payload = {
        "cycle": cycle, "generated_at": now, "result": status,
        "reason": payload.get("reason", ""), "baseline_score": payload.get("baseline_score"),
        "metrics": metrics,
        "root_causes": causes, "candidate_diagnostics": candidates,
        "operator_decisions": operator_decisions,
        "recommendation": recommendation, "kpis": kpis,
    }
    json_path = directory / f"self_repair_cycle_{cycle:03d}.json"
    markdown_path = directory / f"self_repair_cycle_{cycle:03d}.md"
    previous = _previous_metrics(directory, cycle)
    write_json(json_path, json_payload)
    lines = [
        f"# Self-Repair Cycle {cycle:03d}", "", "## Résumé", "",
        f"- Date : {now}", "- Mode : local déterministe",
        f"- Durée : {metrics.get('repair_duration_seconds', 0)} s",
        f"- Baseline : {payload.get('baseline_score', 'not_available')}",
        f"- Échecs analysés : {metrics.get('failures_analyzed', 0)}",
        f"- Groupes bruts : {metrics.get('raw_failure_groups', metrics.get('root_causes_detected', 0))}",
        f"- Causes racines canoniques : {metrics.get('canonical_root_causes', metrics.get('root_causes_detected', 0))}",
        f"- Groupes fusionnés : {metrics.get('merged_root_cause_groups', 0)}",
        f"- Causes localisées : {metrics.get('localized_root_causes', 0)}",
        f"- Cas compatibles : {metrics.get('compatible_failures', 0)}",
        f"- Candidats générés : {metrics.get('candidates_generated', 0)}",
        f"- Candidats évalués : {metrics.get('candidates_reaching_full_evaluation', metrics.get('candidates_evaluated', 0))}",
        f"- Réparations acceptées : {metrics.get('local_repairs_accepted', 0)}",
        "- Codex utilisé : non (le moteur local n’exécute jamais Codex)",
        f"- Codex évité : {'oui' if metrics.get('codex_avoided') else 'non'}", "",
        "## Causes racines", "",
    ]
    if not causes:
        lines.append("Aucune cause racine publique.")
    for cause in causes:
        lines.extend([
            f"### {cause.get('root_cause_id')}", "",
            f"- Catégorie / sous-type : {cause.get('category')} / {cause.get('subtype')}",
            f"- Confiance : {cause.get('confidence')} — {cause.get('confidence_reason', '')}",
            f"- Nombre de scénarios : {cause.get('affected_failure_count', 0)}",
            f"- Groupes bruts fusionnés : {cause.get('raw_group_count', 1)}",
            f"- Familles publiques : {', '.join(cause.get('public_families', [])) or 'aucune'}",
            f"- Symptômes : {', '.join(cause.get('symptoms', [])) or 'non disponibles'}",
            f"- Preuve : {'; '.join(cause.get('evidence', [])) or 'insuffisante'}",
            f"- Fichiers suspects : {', '.join(cause.get('suspected_files', [])) or 'aucun (preuve insuffisante)'}",
            f"- Symboles suspects : {', '.join(cause.get('suspected_symbols', [])) or 'aucun (preuve insuffisante)'}",
            f"- Opérateurs compatibles : {', '.join(cause.get('compatible_repair_types', [])) or 'aucun'}", "",
        ])
    lines.extend(["## Décisions des opérateurs", ""])
    if not operator_decisions:
        lines.append("Aucune décision d'opérateur enregistrée.")
    for decision in operator_decisions:
        lines.append(
            f"- {decision.get('operator')} / {decision.get('canonical_root_cause')} — "
            f"{decision.get('status')} : {decision.get('reason')}"
        )
    lines.extend(["", "## Candidats", ""])
    if not candidates:
        lines.append("Aucun candidat généré.")
    for item in candidates:
        lines.append(
            f"- {item.get('candidate_id')} — {item.get('operator')} / {item.get('root_cause_id')} — "
            f"cible={','.join(item.get('localized_target', [])) or 'aucune'}, confiance={item.get('localization_confidence', 0)}, "
            f"fichiers={','.join(item.get('changed_files', [])) or 'aucun'}, lignes={item.get('lines_changed', 0)}, "
            f"résultat={item.get('outcome')}, rejet={item.get('rejection_stage') or 'aucun'} "
            f"({item.get('rejection_reason', '')}), durée={item.get('total_duration_seconds', 0)} s"
        )
        if item.get("rejection_stage") in {"no_behavior_change", "behavior_changed_but_not_fixed"}:
            lines.extend([
                f"  - branche pertinente atteinte : {item.get('relevant_branch_reached', False)}",
                f"  - condition avant : {'; '.join(item.get('relevant_condition_before', [])) or 'indisponible'}",
                f"  - condition après : {'; '.join(item.get('relevant_condition_after', [])) or 'indisponible'}",
                f"  - branche attendue : {'; '.join(item.get('expected_branch', [])) or 'indisponible'}",
                f"  - branche réelle avant : {'; '.join(item.get('actual_branch_before', [])) or 'indisponible'}",
                f"  - branche réelle après : {'; '.join(item.get('actual_branch_after', [])) or 'indisponible'}",
                f"  - contrat avant : {'; '.join(item.get('contract_before', [])) or 'indisponible'}",
                f"  - contrat après : {'; '.join(item.get('contract_after', [])) or 'indisponible'}",
            ])
        if item.get("rejection_stage") in {"targeted_tests", "local_tests", "full_tests"}:
            suite = item["rejection_stage"]
            lines.extend([
                f"  - commande pytest : {' '.join(item.get(f'{suite}_command', [])) or 'indisponible'}",
                f"  - code retour : {item.get(f'{suite}_returncode')}",
                f"  - tests échoués : {'; '.join(item.get(f'{suite}_failed_tests', [])) or 'indisponibles'}",
                f"  - durée pytest : {item.get(f'{suite}_duration_seconds')} s",
                f"  - stdout (borné) : {item.get(f'{suite}_stdout', '') or 'vide'}",
                f"  - stderr (borné) : {item.get(f'{suite}_stderr', '') or 'vide'}",
            ])
    lines.extend([
        "", "## Fast Gate", "",
        f"- patch_invalid : {metrics.get('rejected_patch_invalid', 0)}",
        f"- patch_bounds : {metrics.get('rejected_patch_bounds', 0)}",
        f"- compile : {metrics.get('rejected_compile', 0)}",
        f"- targeted_tests : {metrics.get('rejected_targeted_tests', 0)}",
        f"- local_tests : {metrics.get('rejected_local_tests', 0)}",
        f"- full_tests : {metrics.get('rejected_full_tests', 0)}",
        f"- representative_scenarios : {metrics.get('rejected_representative_scenarios', 0)}",
        f"- wrong_localization : {metrics.get('rejected_wrong_localization', 0)}",
        f"- no_behavior_change : {metrics.get('rejected_no_behavior_change', 0)}",
        f"- behavior_changed_but_not_fixed : {metrics.get('rejected_behavior_changed_but_not_fixed', 0)}",
        f"- Succès de localisation candidat : {metrics.get('localization_success_rate', 'not_available')}", "",
        "## Évaluation complète", "",
    ])
    evaluated = [item for item in candidates if item.get("train_gain") is not None]
    lines.extend([
        f"- {item.get('candidate_id')} : train={item.get('train_gain')}, validation={item.get('validation_gain')}, "
        f"holdout agrégé={item.get('holdout_gain')}, sécurité agrégée={item.get('true_security_regressions_count', 0)} régression(s)"
        for item in evaluated
    ] or ["Aucun candidat n’a atteint l’évaluation complète."])
    lines.extend(["", "## Résultat final", "", f"Self-Repair : **{status}**", "", payload.get("reason", "")])
    if previous is not None:
        lines.extend([
            "", "## Comparaison avec le cycle précédent", "",
            f"- Échecs analysés : {previous.get('failures_analyzed', 0)} → {metrics.get('failures_analyzed', 0)}",
            f"- Causes racines : {previous.get('root_causes_detected', 0)} → {metrics.get('root_causes_detected', 0)}",
            f"- Acceptées : {previous.get('local_repairs_accepted', 0)} → {metrics.get('local_repairs_accepted', 0)}",
            f"- Temps moyen Fast Gate : {previous.get('average_fast_gate_seconds', 0)} → {metrics.get('average_fast_gate_seconds', 0)} s",
        ])
    lines.extend(["", "## KPIs longitudinaux", ""])
    lines.extend(f"- {name} : {value}" for name, value in kpis.items())
    lines.extend(["", "## Prochaine action recommandée", "", recommendation, ""])
    markdown = "\n".join(lines)
    write_text_atomic(markdown_path, markdown)
    write_text_atomic(directory / "self_repair_latest.md", markdown)

    top_causes = [
        f"{index}. {subtype} ({variants} variante(s), {failures} échec(s), {raw_groups} groupe(s) brut(s))"
        for index, (subtype, failures, variants, raw_groups) in enumerate(_aggregate_top_causes(causes)[:3], 1)
    ]
    top_rejections = [f"{index}. {stage}: {count}" for index, (stage, count) in enumerate(
        Counter(item.get("rejection_stage") for item in candidates if item.get("rejection_stage")).most_common(3), 1
    )]
    files = sorted({path for item in candidates for path in item.get("changed_files", [])})
    summary = "\n".join([
        "=== SELF-REPAIR SUMMARY ===", "", f"Cycle: {cycle:03d}", "Mode: local déterministe",
        f"Baseline: {payload.get('baseline_score', 'not_available')}", f"Résultat: {status}", f"Durée: {metrics.get('repair_duration_seconds', 0)} s", "",
        f"Échecs analysés: {metrics.get('failures_analyzed', 0)}",
        f"Groupes bruts: {metrics.get('raw_failure_groups', metrics.get('root_causes_detected', 0))}",
        f"Causes racines canoniques: {metrics.get('canonical_root_causes', metrics.get('root_causes_detected', 0))}",
        f"Groupes fusionnés: {metrics.get('merged_root_cause_groups', 0)}",
        f"Causes localisées: {metrics.get('localized_root_causes', 0)}",
        f"Opérateurs refusés already_supported: {metrics.get('operators_refused_already_supported', 0)}",
        f"Opérateurs refusés unsupported_repair_hypothesis: {metrics.get('operators_refused_unsupported_repair_hypothesis', 0)}",
        f"Cas compatibles: {metrics.get('compatible_failures', 0)}",
        f"Candidats générés: {metrics.get('candidates_generated', 0)}",
        f"Rejets wrong_localization: {metrics.get('rejected_wrong_localization', 0)}",
        f"Rejets no_behavior_change: {metrics.get('rejected_no_behavior_change', 0)}",
        f"Rejets behavior_changed_but_not_fixed: {metrics.get('rejected_behavior_changed_but_not_fixed', 0)}",
        f"Candidats full evaluation: {metrics.get('candidates_reaching_full_evaluation', 0)}",
        f"Acceptés: {metrics.get('local_repairs_accepted', 0)}", "", "Top causes racines:",
        *(top_causes or ["1. aucune"]), "", "Top raisons de rejet:", *(top_rejections or ["1. aucune"]), "",
        "Codex utilisé: non (moteur local)",
        f"Codex évité: {'oui' if metrics.get('codex_avoided') else 'non'}", "",
        f"Fichiers principaux impliqués: {', '.join(files) or 'aucun'}", "",
        f"Prochaine action recommandée: {recommendation}", "",
    ])
    summary_path = write_text_atomic(directory / "assistant_summary_latest.md", summary)
    return json_path, markdown_path, summary_path
