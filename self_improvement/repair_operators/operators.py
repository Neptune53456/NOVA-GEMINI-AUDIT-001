"""Transformations bornées dérivées des contrats et entrées TRAIN représentatives."""

from __future__ import annotations

import ast
import json
import re
import unicodedata

from .base import RepairOperator, RepairProposal, TextEdit


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value).casefold())
    return "".join(character for character in text if not unicodedata.combining(character)).strip()


def _class_string_set(source: str, class_name: str, attribute: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if not isinstance(child, (ast.Assign, ast.AnnAssign)):
                continue
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            if not any(isinstance(target, ast.Name) and target.id == attribute for target in targets):
                continue
            try:
                value = ast.literal_eval(child.value)
            except (ValueError, TypeError):
                return set()
            return {str(item).casefold() for item in value} if isinstance(value, (set, list, tuple)) else set()
    return set()


def _contract(root_cause, expected: str, observed: str) -> bool:
    diagnosis = root_cause.diagnosis
    return expected in diagnosis.expected_behavior and observed in diagnosis.observed_behavior


class TextNormalizationRepairOperator(RepairOperator):
    name = "text_normalization"
    categories = frozenset({"réponses utilisateur très courtes", "ambiguïtés", "confirmations"})
    allowed_files = frozenset({"system_action_controller.py"})
    allowed_symbols = frozenset({"SystemActionController.handle_confirmation"})

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        if not _contract(root_cause, "confirmed_call_count=1", "confirmed_call_count=0"):
            return self.refusal(
                "unsupported_repair_hypothesis",
                "Le contrat échoué ne porte pas sur la normalisation d'une confirmation.",
            )
        inputs = self.public_train_inputs(root_cause)
        source = self.source_text("system_action_controller.py", source_root)
        aliases = _class_string_set(source, "SystemActionController", "CONFIRMATIONS")
        if not inputs or not aliases:
            return self.refusal("unsupported_repair_hypothesis", "Entrées ou règle de confirmation absentes.")
        if "unicodedata.normalize" in self.localized_symbol_source(
            root_cause, "system_action_controller.py", source_root
        ):
            return self.refusal("already_supported", "Le symbole normalise déjà casse, espaces et accents.")
        normalized_inputs = {_normalized(value) for value in inputs}
        raw_inputs = {value.casefold() for value in inputs}
        if not normalized_inputs <= aliases or normalized_inputs == raw_inputs:
            return self.refusal(
                "unsupported_repair_hypothesis",
                "La normalisation seule ne transforme pas les entrées représentatives en confirmations connues.",
                *(f"entrée={value!r}, normalisée={_normalized(value)!r}" for value in inputs),
            )
        edits = (
            TextEdit(
                "system_action_controller.py",
                '"""Confirmation différée des actions système sensibles."""\n\n',
                '"""Confirmation différée des actions système sensibles."""\n\nimport unicodedata\n\n',
            ),
            TextEdit(
                "system_action_controller.py", "        answer = user_input.lower()\n",
                "        answer = ''.join(character for character in "
                "unicodedata.normalize('NFKD', user_input.strip().casefold()) "
                "if not unicodedata.combining(character))\n",
            ),
        )
        return self.checked_candidate(
            root_cause, source_root, "Normaliser une confirmation vers un alias déjà supporté", edits,
            required_symbol_markers=("answer = user_input.lower()", "self.CONFIRMATIONS"),
            evidence=tuple(f"{value!r} -> {_normalized(value)!r} présent dans CONFIRMATIONS" for value in inputs),
        )

    def propose(self, root_cause, source_root=None):
        return list(self.propose_evidence_driven(root_cause, source_root).candidates)


class RegexRepairOperator(RepairOperator):
    name = "regex_repair"
    categories = frozenset({"documents", "pièces jointes"})
    allowed_files = frozenset({"document_command_router.py"})
    allowed_symbols = frozenset({"_comparison_paths"})

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        inputs = self.representative_inputs(root_cause)
        if "_comparison_paths" not in root_cause.localization.candidate_symbols:
            return self.refusal("unsupported_repair_hypothesis", "La branche de comparaison n'est pas localisée.")
        if not inputs or not all(re.search(r"\bcomparaison\b", _normalized(value)) for value in inputs):
            return self.refusal(
                "unsupported_repair_hypothesis",
                "Les entrées représentatives ne prouvent pas l'alias nominal « comparaison ».",
            )
        edit = TextEdit(
            "document_command_router.py", r'r"^.*?\bcompare(?:r)?\b"',
            r'r"^.*?\bcompar(?:e|er|aison)\b"',
        )
        return self.checked_candidate(
            root_cause, source_root, "Reconnaître l'alias nominal observé « comparaison »", (edit,),
            required_symbol_markers=(r"compare(?:r)?",),
            evidence=tuple(f"entrée TRAIN contenant comparaison: {value!r}" for value in inputs),
        )

    def propose(self, root_cause, source_root=None):
        return list(self.propose_evidence_driven(root_cause, source_root).candidates)


class IntentAliasRepairOperator(RepairOperator):
    name = "intent_alias"
    categories = frozenset({"suppression", "création fichiers/dossiers", "réponses utilisateur très courtes"})
    allowed_files = frozenset({"request_interpreter.py"})
    allowed_symbols = frozenset({"RequestInterpreter.interpret", "VAGUE_SIMPLE_NAMES"})

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        inputs = self.representative_inputs(root_cause)
        if "VAGUE_SIMPLE_NAMES" not in root_cause.localization.candidate_symbols:
            return self.refusal(
                "unsupported_repair_hypothesis",
                "L'alias VAGUE_SIMPLE_NAMES n'est pas dans la cible localisée; le modifier ne peut pas affecter la branche observée.",
            )
        if not inputs or not all(re.search(r"\bun\s+doc\b", _normalized(value)) for value in inputs):
            return self.refusal(
                "unsupported_repair_hypothesis",
                "Aucune entrée représentative ne contient l'alias « un doc ».",
            )
        edit = TextEdit(
            "request_interpreter.py", '    "un fichier",\n',
            '    "un fichier",\n    "un doc",\n',
        )
        return self.checked_candidate(
            root_cause, source_root, "Ajouter l'alias exact observé « un doc »", (edit,),
            required_symbol_markers=('"un fichier"',),
            evidence=tuple(f"alias observé dans {value!r}" for value in inputs),
        )

    def propose(self, root_cause, source_root=None):
        return list(self.propose_evidence_driven(root_cause, source_root).candidates)


class ConfirmationRepairOperator(RepairOperator):
    name = "confirmation"
    categories = frozenset({"confirmations"})
    allowed_files = frozenset({"system_action_controller.py"})
    allowed_symbols = frozenset({"SystemActionController.handle_confirmation"})

    @staticmethod
    def _safe_positive_phrase(value: str) -> bool:
        words = re.findall(r"[a-z]+", _normalized(value))
        allowed = {"euh", "stp", "s", "il", "te", "plait", "la", "oui", "confirme", "confirmer", "vas", "y"}
        positive = "oui" in words or "confirme" in words or "confirmer" in words or {"vas", "y"} <= set(words)
        return bool(words and positive and set(words) <= allowed)

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        if not _contract(root_cause, "confirmed_call_count=1", "confirmed_call_count=0"):
            return self.refusal("unsupported_repair_hypothesis", "Le contrat ne demande pas une confirmation reconnue.")
        inputs = self.public_train_inputs(root_cause)
        if not inputs or not all(self._safe_positive_phrase(value) for value in inputs):
            return self.refusal(
                "unsupported_repair_hypothesis",
                "Les entrées ne sont pas toutes des confirmations positives bornées.",
                *(f"entrée refusée: {value!r}" for value in inputs if not self._safe_positive_phrase(value)),
            )
        source = self.source_text("system_action_controller.py", source_root)
        aliases = _class_string_set(source, "SystemActionController", "CONFIRMATIONS")
        missing = sorted({value.casefold() for value in inputs} - aliases)
        if not missing:
            return self.refusal(
                "already_supported", "Toutes les confirmations représentatives sont déjà présentes dans CONFIRMATIONS.",
            )
        old = '        "vas y",\n    }'
        inserted = "".join(f"        {json.dumps(value, ensure_ascii=False)},\n" for value in missing)
        edit = TextEdit("system_action_controller.py", old, f'        "vas y",\n{inserted}    }}')
        return self.checked_candidate(
            root_cause, source_root, "Ajouter uniquement les confirmations positives observées", (edit,),
            required_symbol_markers=("answer = user_input.lower()", "self.CONFIRMATIONS"),
            evidence=tuple(f"alias positif absent de CONFIRMATIONS: {value!r}" for value in missing),
        )

    def propose(self, root_cause, source_root=None):
        return list(self.propose_evidence_driven(root_cause, source_root).candidates)


class DocumentRoutingRepairOperator(RepairOperator):
    name = "document_routing"
    categories = frozenset({"documents", "pièces jointes", "OCR simulé"})
    allowed_files = frozenset({"document_command_router.py"})
    allowed_symbols = frozenset({"handle_document_command"})

    @staticmethod
    def _explicit_attachment_correction(value: str) -> bool:
        normalized = _normalized(value)
        return bool(
            re.search(r"\b(?:piece jointe|fichier joint|document joint)\b", normalized)
            and re.search(r"\b(?:non|plutot|je parle de)\b", normalized)
        )

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        inputs = self.representative_inputs(root_cause)
        source = self.source_text("document_command_router.py", source_root)
        if not inputs or "calls.0.kind='analyze'" not in root_cause.diagnosis.expected_behavior:
            return self.refusal("unsupported_repair_hypothesis", "Le contrat ne prouve pas un routage vers l'analyse.")
        observations = [case.trace.get("repair_observation", {}) for case in self.representative_cases(root_cause)]
        attachment_inputs = all(
            observation.get("active_attachment")
            and self._explicit_attachment_correction(value)
            for value, observation in zip(inputs, observations)
        ) and len(observations) == len(inputs)
        if attachment_inputs:
            old = (
                "    document_word = bool(\n"
                "        re.search(r\"\\b(?:pdf|document|fichier)\\b\", normalized)\n"
                "        or attachment_reference\n"
                "    )\n\n"
                "    if not any((comparison, summary, key_points, question_request, analysis)):\n"
            )
            new = (
                "    document_word = bool(\n"
                "        re.search(r\"\\b(?:pdf|document|fichier)\\b\", normalized)\n"
                "        or attachment_reference\n"
                "    )\n"
                "    attachment_correction = bool(\n"
                "        active_attachment is not None\n"
                "        and attachment_reference\n"
                "        and re.search(r\"\\b(?:non|plutot|je parle de)\\b\", normalized)\n"
                "    )\n\n"
                "    if not any((comparison, summary, key_points, question_request, analysis, attachment_correction)):\n"
            )
            if "analysis, attachment_correction" in source:
                return self.refusal("already_supported", "La référence explicite à une pièce jointe active déjà la branche d'analyse.")
            return self.checked_candidate(
                root_cause, source_root,
                "Router une correction explicite vers la pièce jointe active",
                (TextEdit("document_command_router.py", old, new),),
                required_symbol_markers=("attachment_reference", "if not any((comparison"),
                evidence=tuple(f"pièce jointe active et référence explicite: {value!r}" for value in inputs),
            )
        if all(re.search(r"\bdoc\b", _normalized(value)) for value in inputs):
            old = r'r"\b(?:pdf|document|fichier)\b"'
            new = r'r"\b(?:pdf|document|fichier|doc)\b"'
            if new in source:
                return self.refusal("already_supported", "L'alias document « doc » est déjà reconnu.")
            return self.checked_candidate(
                root_cause, source_root, "Ajouter l'alias document exact observé « doc »",
                (TextEdit("document_command_router.py", old, new),),
                required_symbol_markers=(r"(?:pdf|document|fichier)",),
                evidence=tuple(f"alias doc observé: {value!r}" for value in inputs),
            )
        return self.refusal(
            "unsupported_repair_hypothesis",
            "Le patch générique « doc » ne peut pas affecter les requêtes extensionless représentatives.",
            *(f"entrée sans alias doc: {value!r}" for value in inputs),
        )

    def propose(self, root_cause, source_root=None):
        return list(self.propose_evidence_driven(root_cause, source_root).candidates)


class ContextPriorityRepairOperator(RepairOperator):
    name = "context_priority"
    categories = frozenset({"ambiguïtés", "pièces jointes", "compréhension contextuelle"})
    allowed_files = frozenset({"request_interpreter.py"})
    allowed_symbols = frozenset({"RequestInterpreter.interpret", "ATTACHMENT_REFERENCE_PATTERN"})

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        inputs = self.representative_inputs(root_cause)
        observations = [case.trace.get("repair_observation", {}) for case in self.representative_cases(root_cause)]
        if "ATTACHMENT_REFERENCE_PATTERN" not in root_cause.localization.candidate_symbols:
            return self.refusal(
                "unsupported_repair_hypothesis",
                "Le motif de pièce jointe n'est pas localisé; l'étendre ne peut pas corriger la branche ambiguë observée.",
            )
        if not inputs or not all(
            "le joint" in _normalized(value) and observation.get("active_attachment")
            for value, observation in zip(inputs, observations)
        ):
            return self.refusal(
                "unsupported_repair_hypothesis",
                "Les entrées ne prouvent pas l'alias contextuel « le joint » avec pièce jointe active.",
            )
        edit = TextEdit(
            "request_interpreter.py", r'r"fichier\s+que\s+j(?:e|\s+ai)\s+joint)"',
            r'r"fichier\s+que\s+j(?:e|\s+ai)\s+joint|le\s+joint)"',
        )
        return self.checked_candidate(
            root_cause, source_root, "Reconnaître l'alias contextuel observé « le joint »", (edit,),
            required_symbol_markers=("ATTACHMENT_REFERENCE_PATTERN",),
            evidence=tuple(f"alias avec pièce jointe active: {value!r}" for value in inputs),
        )

    def propose(self, root_cause, source_root=None):
        return list(self.propose_evidence_driven(root_cause, source_root).candidates)


DEFAULT_OPERATORS = (
    TextNormalizationRepairOperator(), RegexRepairOperator(), IntentAliasRepairOperator(),
    ConfirmationRepairOperator(), DocumentRoutingRepairOperator(), ContextPriorityRepairOperator(),
)
