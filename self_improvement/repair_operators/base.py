"""Contrats sûrs et déterministes des opérateurs de réparation locale."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..failure_analyzer import FailureCluster


class UnsafeRepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextEdit:
    path: str
    old: str
    new: str
    expected_occurrences: int = 1


@dataclass(frozen=True)
class RepairCandidate:
    operator: str
    description: str
    edits: tuple[TextEdit, ...]
    repair_hypothesis: str = ""
    evidence_used: tuple[str, ...] = ()
    expected_behavior_change: str = ""
    localized_target: tuple[str, ...] = ()
    localization_confidence: float = 0.0
    confidence: float = 0.0


@dataclass(frozen=True)
class RepairProposal:
    status: str
    reason: str
    candidates: tuple[RepairCandidate, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorApplicability:
    supported: bool
    score: float
    reason: str
    required_evidence: tuple[str, ...]
    allowed_files: tuple[str, ...]
    allowed_symbols: tuple[str, ...]
    estimated_risk: str


class RepairOperator:
    """Base sans LLM : recettes bornées, fichiers et catégories déclarés."""

    name = "base"
    categories: frozenset[str] = frozenset()
    allowed_files: frozenset[str] = frozenset()
    allowed_symbols: frozenset[str] = frozenset()
    supported_repair_types: frozenset[str] = frozenset()
    required_evidence: tuple[str, ...] = ("behavioral_diagnosis", "code_localization")
    estimated_risk = "low"
    max_candidates = 2

    @staticmethod
    def _project_root(source_root=None) -> Path:
        return Path(source_root or Path(__file__).resolve().parents[2]).resolve()

    def source_text(self, relative: str, source_root=None) -> str:
        root = self._project_root(source_root)
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file():
            return ""
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def localized_symbol_source(self, root_cause, relative: str, source_root=None) -> str:
        """Retourne uniquement les symboles AST localisés et autorisés dans ce fichier."""
        content = self.source_text(relative, source_root)
        try:
            tree = ast.parse(content, filename=relative)
        except SyntaxError:
            return ""
        wanted = set(root_cause.localization.candidate_symbols) & self.allowed_symbols
        segments = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
                segments.append(ast.get_source_segment(content, node) or "")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id in wanted for target in targets):
                    segments.append(ast.get_source_segment(content, node) or "")
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    qualified = f"{node.name}.{getattr(child, 'name', '')}"
                    if qualified in wanted:
                        segments.append(ast.get_source_segment(content, child) or "")
        return "\n".join(segments)

    @staticmethod
    def representative_cases(root_cause):
        public_ids = set(root_cause.representative_public_scenario_ids)
        return [
            case for case in root_cause.cases
            if case.split == "train" and case.scenario_id in public_ids
        ]

    def representative_inputs(self, root_cause) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            str(case.trace.get("input_message", "")).strip()
            for case in self.representative_cases(root_cause)
            if str(case.trace.get("input_message", "")).strip()
        ))

    @staticmethod
    def public_train_inputs(root_cause) -> tuple[str, ...]:
        """Toutes les variantes de la cause canonique, sans validation ni holdout."""
        return tuple(dict.fromkeys(
            str(case.trace.get("input_message", "")).strip()
            for case in root_cause.cases
            if case.split == "train" and str(case.trace.get("input_message", "")).strip()
        ))

    @staticmethod
    def refusal(status: str, reason: str, *evidence: str) -> RepairProposal:
        if status not in {"already_supported", "unsupported_repair_hypothesis"}:
            raise ValueError(f"Statut de proposition inconnu : {status}")
        return RepairProposal(status, reason, evidence=tuple(evidence))

    def checked_candidate(
        self, root_cause, source_root, description: str, edits: tuple[TextEdit, ...],
        *, required_symbol_markers: tuple[str, ...], evidence: tuple[str, ...] = (),
    ) -> RepairProposal:
        localized_files = set(root_cause.localization.candidate_files) & self.allowed_files
        if not edits or any(edit.path not in localized_files for edit in edits):
            return self.refusal(
                "unsupported_repair_hypothesis",
                "La transformation ne modifie pas le fichier localisé.",
            )
        symbol_source = "\n".join(
            self.localized_symbol_source(root_cause, path, source_root)
            for path in sorted(localized_files)
        )
        missing_markers = [marker for marker in required_symbol_markers if marker not in symbol_source]
        if missing_markers:
            return self.refusal(
                "unsupported_repair_hypothesis",
                "La règle visée n'est pas référencée par le symbole localisé.",
                *(f"marqueur absent du symbole: {marker}" for marker in missing_markers),
            )
        for edit in edits:
            content = self.source_text(edit.path, source_root)
            if content.count(edit.old) != edit.expected_occurrences:
                status = "already_supported" if edit.new in content else "unsupported_repair_hypothesis"
                return self.refusal(
                    status,
                    "La règle cible est déjà présente." if status == "already_supported" else
                    "Le motif exact à transformer est absent ou ambigu.",
                    f"occurrences du motif={content.count(edit.old)}",
                )
            patched = content.replace(edit.old, edit.new, 1)
            if patched == content:
                return self.refusal("already_supported", "La transformation est sémantiquement identique.")
        candidate = self.build_candidate(root_cause, description, edits, extra_evidence=evidence)
        return RepairProposal("candidate", description, (candidate,), evidence)

    def applicable(self, cluster: FailureCluster) -> bool:
        return any(case.category in self.categories for case in cluster.cases)

    def supports(self, root_cause) -> bool:
        localization = getattr(root_cause, "localization", None)
        return (
            self.name in getattr(root_cause, "compatible_repair_types", ())
            and getattr(root_cause, "diagnosis", None) is not None
            and localization is not None and localization.reliable
            and bool(set(localization.candidate_files) & self.allowed_files)
            and (
                not self.allowed_symbols
                or bool(set(localization.candidate_symbols) & self.allowed_symbols)
            )
        )

    def applicability(self, root_cause) -> OperatorApplicability:
        supported = self.supports(root_cause)
        confidence = float(getattr(root_cause, "confidence", 0.0))
        localization = getattr(root_cause, "localization", None)
        file_hints = set(localization.candidate_files if localization else ())
        file_compatible = bool(file_hints & self.allowed_files)
        symbol_hints = set(localization.candidate_symbols if localization else ())
        symbol_compatible = not self.allowed_symbols or bool(symbol_hints & self.allowed_symbols)
        supported = supported and file_compatible and symbol_compatible
        localization_confidence = float(localization.confidence if localization else 0.0)
        score = round(min(confidence, localization_confidence) * (1.0 if supported else 0.0), 3)
        reason = (
            f"cause {getattr(root_cause, 'subtype', 'inconnue')} supportée; "
            f"confiance={confidence:.3f}; zones compatibles"
            if supported else "type de cause, preuves ou zones de modification incompatibles"
        )
        return OperatorApplicability(
            supported, score, reason, self.required_evidence,
            tuple(sorted(self.allowed_files)), tuple(sorted(self.allowed_symbols)), self.estimated_risk,
        )

    def build_candidate(
        self, root_cause, description: str, edits: tuple[TextEdit, ...],
        *, extra_evidence: tuple[str, ...] = (),
    ) -> RepairCandidate:
        localization = getattr(root_cause, "localization", None)
        diagnosis = getattr(root_cause, "diagnosis", None)
        if localization is None or not localization.reliable or diagnosis is None:
            raise UnsafeRepairError("Une localisation fiable et un diagnostic sont requis avant tout patch.")
        target_files = sorted(set(localization.candidate_files) & self.allowed_files)
        target_symbols = sorted(set(localization.candidate_symbols) & self.allowed_symbols)
        if not target_files or (self.allowed_symbols and not target_symbols):
            raise UnsafeRepairError("La localisation ne recouvre pas la zone autorisee de l'operateur.")
        localized = ", ".join(
            f"{path}:{symbol}" for path in target_files for symbol in target_symbols
        )
        hypothesis = (
            f"{root_cause.subtype}: divergence {diagnosis.first_divergence_stage}; "
            f"zone vérifiée {localized}; transformation bornée: {description}"
        )
        evidence = tuple(dict.fromkeys([
            *diagnosis.evidence, *localization.evidence,
            *(f"attendu {value}" for value in diagnosis.expected_behavior),
            *(f"observé {value}" for value in diagnosis.observed_behavior),
            *(
                f"entrée TRAIN représentative {case.scenario_id}: "
                f"{case.trace.get('input_message')!r}"
                for case in self.representative_cases(root_cause)
                if case.trace.get("input_message")
            ),
            *extra_evidence,
        ]))
        expected = "; ".join(diagnosis.expected_behavior) or "respect du contrat public ciblé"
        return RepairCandidate(
            self.name, description, edits, repair_hypothesis=hypothesis,
            evidence_used=evidence, expected_behavior_change=expected,
            localized_target=tuple(
                f"{path}:{symbol}" for path in target_files for symbol in target_symbols
            ),
            localization_confidence=localization.confidence,
            confidence=round(min(root_cause.confidence, localization.confidence), 3),
        )

    def propose_evidence_driven(self, root_cause, source_root=None) -> RepairProposal:
        candidates = tuple(self.propose(root_cause))
        return RepairProposal(
            "candidate" if candidates else "unsupported_repair_hypothesis",
            "Proposition déterministe de compatibilité.", candidates,
        )

    def propose(self, cluster: FailureCluster) -> list[RepairCandidate]:
        raise NotImplementedError

    def validate(self, candidate: RepairCandidate) -> None:
        if candidate.operator != self.name or not candidate.edits:
            raise UnsafeRepairError("Candidat vide ou attribué au mauvais opérateur.")
        if len(candidate.edits) > self.max_candidates:
            raise UnsafeRepairError("Trop d'éditions dans un candidat.")
        for edit in candidate.edits:
            path = PurePosixPath(edit.path)
            if path.is_absolute() or ".." in path.parts or edit.path not in self.allowed_files:
                raise UnsafeRepairError(f"Zone de modification interdite : {edit.path}")
            if not edit.old or edit.old == edit.new or edit.expected_occurrences != 1:
                raise UnsafeRepairError("Édition textuelle non déterministe.")

    def apply(self, candidate: RepairCandidate, worktree: Path) -> list[str]:
        self.validate(candidate)
        changed = []
        root = worktree.resolve()
        for edit in candidate.edits:
            target = (root / edit.path).resolve()
            if root not in target.parents:
                raise UnsafeRepairError("Chemin de réparation hors worktree.")
            raw = target.read_bytes()
            content = raw.decode("utf-8")
            newline = "\r\n" if b"\r\n" in raw else "\n"
            old = edit.old.replace("\n", newline)
            new = edit.new.replace("\n", newline)
            if content.count(old) != edit.expected_occurrences:
                raise UnsafeRepairError(
                    f"Ancre absente ou ambiguë dans {edit.path}."
                )
            target.write_bytes(content.replace(old, new, 1).encode("utf-8"))
            changed.append(edit.path)
        return changed
