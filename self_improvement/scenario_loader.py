"""Chargement et validation du corpus de scénarios versionné."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Scenario
from .variant_generator import scenario_fingerprint


DATASET_PATH = Path(__file__).with_name("scenarios") / "v1.json"
VALID_SPLITS = {"train", "validation", "holdout"}
PUBLIC_DISCOVERY_SPLITS = {"train", "validation"}
DISCOVERED_SCENARIO_FIELDS = {
    "id", "category", "split", "initial_context", "messages",
    "simulated_state", "expectations", "success_criteria", "weight",
    "tags", "runner",
}


class ScenarioValidationError(ValueError):
    pass


def _expand_family(family: dict, version: str) -> list[Scenario]:
    required = {"id_prefix", "category", "runner", "criteria", "cases"}
    missing = required - set(family)
    if missing:
        raise ScenarioValidationError(f"Famille incomplète ({sorted(missing)}), dataset {version}.")
    scenarios = []
    for index, case in enumerate(family["cases"], start=1):
        split = case.get("split")
        if split not in VALID_SPLITS:
            raise ScenarioValidationError(f"Split invalide pour {family['id_prefix']}-{index:03d}.")
        criteria = case.get("criteria", family["criteria"])
        scenario = Scenario(
            id=case.get("id", f"{family['id_prefix']}-{index:03d}"),
            category=family["category"],
            split=split,
            initial_context=dict(case.get("initial_context", family.get("initial_context", {}))),
            messages=list(case.get("messages", [])),
            simulated_state=dict(case.get("simulated_state", family.get("simulated_state", {}))),
            expectations=dict(case.get("expectations", family.get("expectations", {}))),
            success_criteria=[dict(item) for item in criteria],
            weight=float(case.get("weight", family.get("weight", 1.0))),
            tags=list(dict.fromkeys([*family.get("tags", []), *case.get("tags", [])])),
            runner=case.get("runner", family["runner"]),
        )
        if not scenario.messages or not scenario.success_criteria or scenario.weight <= 0:
            raise ScenarioValidationError(f"Scénario incomplet : {scenario.id}.")
        scenarios.append(scenario)
    return scenarios


def load_scenarios(path: Path | str = DATASET_PATH, splits=None) -> tuple[str, list[Scenario]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = str(data.get("version", ""))
    if not version or not isinstance(data.get("families"), list):
        raise ScenarioValidationError("Dataset invalide : version et familles sont obligatoires.")
    scenarios = [scenario for family in data["families"] for scenario in _expand_family(family, version)]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ScenarioValidationError("Les identifiants de scénarios doivent être uniques.")
    selected = set(splits or VALID_SPLITS)
    if not selected <= VALID_SPLITS:
        raise ScenarioValidationError("Un split demandé est inconnu.")
    return version, [scenario for scenario in scenarios if scenario.split in selected]


def load_public_discoveries(path: Path | str, splits=None) -> tuple[str | None, list[Scenario]]:
    """Charge uniquement les découvertes publiques TRAIN/validation."""
    source = Path(path)
    if not source.exists():
        return None, []
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("version") != "1.0" or not isinstance(data.get("scenarios"), list):
        raise ScenarioValidationError("Corpus de découvertes publiques invalide.")
    requested = set(splits or PUBLIC_DISCOVERY_SPLITS)
    if not requested <= VALID_SPLITS:
        raise ScenarioValidationError("Un split demandé est inconnu.")
    selected = requested & PUBLIC_DISCOVERY_SPLITS
    scenarios = []
    for payload in data["scenarios"]:
        if not isinstance(payload, dict) or set(payload) != DISCOVERED_SCENARIO_FIELDS:
            raise ScenarioValidationError("Schéma de scénario public découvert invalide.")
        if payload.get("split") not in PUBLIC_DISCOVERY_SPLITS:
            continue
        scenario = Scenario(**payload)
        if (
            not scenario.id or not scenario.messages or not scenario.success_criteria
            or scenario.weight <= 0 or scenario.split not in PUBLIC_DISCOVERY_SPLITS
        ):
            raise ScenarioValidationError(f"Scénario public découvert incomplet : {scenario.id!r}.")
        if scenario.split in selected:
            scenarios.append(scenario)
    return "1.0", scenarios


def merge_public_scenarios(primary: list[Scenario], discovered: list[Scenario]) -> list[Scenario]:
    """Fusion déterministe : le dataset principal garde toujours la priorité."""
    merged = []
    fingerprints = set()
    identifiers = set()
    for scenario in [*primary, *discovered]:
        fingerprint = scenario_fingerprint(scenario)
        if scenario.id in identifiers or fingerprint in fingerprints:
            continue
        merged.append(scenario)
        identifiers.add(scenario.id)
        fingerprints.add(fingerprint)
    return merged


def public_summary(scenarios: list[Scenario]) -> dict:
    """Expose uniquement des comptes, jamais le contenu du holdout."""
    counts = {split: 0 for split in sorted(VALID_SPLITS)}
    for scenario in scenarios:
        counts[scenario.split] += 1
    return {"total": len(scenarios), "splits": counts}
