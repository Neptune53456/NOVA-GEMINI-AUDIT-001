"""Mutations linguistiques déterministes qui conservent l'oracle du scénario."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import random
import re
import unicodedata

from .models import Scenario


def normalize_utterance(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def scenario_fingerprint(scenario: Scenario) -> str:
    stable = {
        "runner": scenario.runner,
        "messages": [message.strip() for message in scenario.messages],
        "context": scenario.initial_context,
        "criteria": scenario.success_criteria,
    }
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class LinguisticVariantGenerator:
    """Produit un ensemble borné et reproductible, sans modifier les critères."""

    def __init__(self, *, seed=0, maximum_variants=100):
        if not 1 <= maximum_variants <= 10_000:
            raise ValueError("maximum_variants doit être compris entre 1 et 10000.")
        self.seed = int(seed)
        self.maximum_variants = maximum_variants

    @staticmethod
    def _accentless(text):
        value = unicodedata.normalize("NFKD", text)
        return "".join(char for char in value if not unicodedata.combining(char))

    def _candidates(self, text: str, controlled_variants=()):
        stripped = text.strip()
        variants = [
            *controlled_variants,
            self._accentless(stripped),
            stripped.casefold(),
            stripped.upper(),
            stripped.rstrip(" .?!,;:"),
            stripped.replace("'", "’"),
            stripped.replace("’", "'"),
            re.sub(r"\s+", "  ", stripped),
            f"non, {stripped}",
            f"euh {stripped}",
            f"{stripped} stp",
        ]
        replacements = (
            ("pièce jointe", "fichier joint"),
            ("pièce jointe", "document joint"),
            ("je parle de", "non je parle de"),
            ("ce fichier", "celui-là"),
            ("ce fichier", "ça"),
        )
        for old, new in replacements:
            if old in stripped.casefold():
                variants.append(re.sub(re.escape(old), new, stripped, flags=re.IGNORECASE))
        words = stripped.split()
        if len(words) > 3:
            variants.append(" ".join(words[-3:]))
        return variants

    def generate(self, source: Scenario, *, count=None, controlled_variants=()) -> list[Scenario]:
        limit = min(self.maximum_variants, self.maximum_variants if count is None else max(0, int(count)))
        if not source.messages or limit == 0:
            return []
        candidates = self._candidates(source.messages[-1], controlled_variants)
        rng = random.Random(f"{self.seed}:{source.id}")
        rng.shuffle(candidates)
        seen = {source.messages[-1].strip()}
        generated = []
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            digest = hashlib.sha1(f"{source.id}|{candidate}".encode("utf-8")).hexdigest()[:10]
            generated.append(replace(
                source,
                id=f"lab-{source.id}-{digest}",
                messages=[*source.messages[:-1], candidate],
                weight=min(source.weight, 0.25),
                tags=list(dict.fromkeys([*source.tags, "generated", "provisional", f"family:{self.family(source)}"])),
            ))
            if len(generated) >= limit:
                break
        return generated

    @staticmethod
    def family(scenario: Scenario) -> str:
        for tag in scenario.tags:
            if tag.startswith("discovery-family:"):
                return tag.split(":", 1)[1]
        for tag in scenario.tags:
            if tag.startswith("family:"):
                return tag.split(":", 1)[1]
        return re.sub(r"-(?:\d{2,3}|[0-9a-f]{10})$", "", scenario.id)


def assign_family_splits(scenarios: list[Scenario], *, seed=0) -> list[Scenario]:
    """Affecte une famille entière à un split afin d'éviter les quasi-doublons."""
    families = sorted(
        {LinguisticVariantGenerator.family(item) for item in scenarios},
        key=lambda family: hashlib.sha256(f"{seed}:{family}".encode()).hexdigest(),
    )
    count = len(families)
    if count >= 3:
        holdout_count = max(1, round(count * 0.15))
        validation_count = max(1, round(count * 0.20))
        if holdout_count + validation_count >= count:
            holdout_count = validation_count = 1
    else:
        holdout_count = 1 if count == 2 else 0
        validation_count = 0
    mapping = {}
    for index, family in enumerate(families):
        if index < holdout_count:
            mapping[family] = "holdout"
        elif index < holdout_count + validation_count:
            mapping[family] = "validation"
        else:
            mapping[family] = "train"
    result = []
    for scenario in scenarios:
        family = LinguisticVariantGenerator.family(scenario)
        result.append(replace(scenario, split=mapping[family]))
    return result


def assign_discovery_splits(
    scenarios: list[Scenario], *, existing_public=(), existing_holdout=(), seed=0,
) -> tuple[list[Scenario], dict]:
    """Répartit les découvertes, sans déplacer ni dévoiler les familles existantes.

    Une famille déjà publique reste publique. Une famille exclusivement privée reste
    privée. Seules les familles réellement nouvelles sont réparties avec la seed.
    """
    public_splits: dict[str, set[str]] = {}
    for item in existing_public:
        public_splits.setdefault(LinguisticVariantGenerator.family(item), set()).add(item.split)
    public_families = set(public_splits)
    private_families = {LinguisticVariantGenerator.family(item) for item in existing_holdout}
    contaminated = public_families & private_families

    candidate_families = {LinguisticVariantGenerator.family(item) for item in scenarios}
    new_families = candidate_families - public_families - private_families
    ordered = sorted(
        new_families,
        key=lambda family: hashlib.sha256(f"{seed}:discovery:{family}".encode()).hexdigest(),
    )
    new_mapping: dict[str, str] = {}
    if len(ordered) >= 3:
        holdout_count = max(1, round(len(ordered) * 0.15))
        validation_count = max(1, round(len(ordered) * 0.20))
        if holdout_count + validation_count >= len(ordered):
            holdout_count = validation_count = 1
    elif len(ordered) == 2:
        holdout_count, validation_count = 1, 0
    else:
        holdout_count = validation_count = 0
    for index, family in enumerate(ordered):
        if index < holdout_count:
            new_mapping[family] = "holdout"
        elif index < holdout_count + validation_count:
            new_mapping[family] = "validation"
        else:
            new_mapping[family] = "train"

    result = []
    for item in scenarios:
        family = LinguisticVariantGenerator.family(item)
        if family in public_families:
            # Un éventuel conflit historique signifie que la famille n'est plus secrète.
            known = public_splits[family]
            split = "train" if "train" in known else "validation"
        elif family in private_families:
            split = "holdout"
        else:
            split = new_mapping[family]
        result.append(replace(item, split=split))

    hidden_count = sum(item.split == "holdout" for item in result)
    if hidden_count:
        reason = "holdout attribué par famille sans chevauchement avec le corpus public"
    elif len(new_families) == 1:
        reason = "holdout impossible sans scinder l'unique nouvelle famille"
    elif not new_families and candidate_families:
        reason = "aucune famille nouvelle éligible au holdout; toutes sont déjà publiques"
    elif not candidate_families:
        reason = "aucune nouvelle découverte à répartir"
    else:
        reason = "corpus insuffisant pour créer un holdout sans rompre les familles"
    return result, {
        "new_family_count": len(new_families),
        "hidden_family_count": len({
            LinguisticVariantGenerator.family(item) for item in result if item.split == "holdout"
        }),
        "contaminated_family_count": len(contaminated),
        "reason": reason,
    }
