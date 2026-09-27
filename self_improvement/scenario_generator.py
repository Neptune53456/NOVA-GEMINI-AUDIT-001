"""Création bornée de variantes provisoires et promotion en régressions."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import re

from .models import Scenario


REGRESSION_DIR = Path(__file__).with_name("scenarios") / "regressions"


class ScenarioGenerator:
    def __init__(self, *, maximum_variants=8):
        if not 1 <= maximum_variants <= 20:
            raise ValueError("La génération doit rester bornée entre 1 et 20 variantes.")
        self.maximum_variants = maximum_variants

    def mutate_short_reply(self, source: Scenario, replacements=None) -> list[Scenario]:
        replacements = list(replacements or ("tomate", "appelle-le tomate", "le nom sera tomate"))
        generated = []
        for index, replacement in enumerate(replacements[: self.maximum_variants], start=1):
            messages = [*source.messages[:-1], replacement]
            generated.append(replace(
                source, id=f"generated-{source.id}-{index:02d}", messages=messages,
                split="train", weight=min(source.weight, 0.25),
                tags=[*source.tags, "provisional", "generated"],
            ))
        return generated

    def promote_regression(self, scenario: Scenario, *, directory=None) -> Path:
        directory = Path(directory or REGRESSION_DIR)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,120}", scenario.id):
            raise ValueError("Identifiant de régression invalide.")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{scenario.id}.json"
        if target.exists():
            raise FileExistsError(target)
        payload = asdict(replace(scenario, weight=max(1.0, scenario.weight), tags=[tag for tag in scenario.tags if tag != "provisional"] + ["regression"]))
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return target
