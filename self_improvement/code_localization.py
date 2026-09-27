"""Localisation déterministe et vérifiée d'une cause avant toute génération de patch."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodeLocalization:
    candidate_files: list[str]
    candidate_symbols: list[str]
    evidence: list[str]
    confidence: float
    localization_method: str

    @property
    def reliable(self) -> bool:
        return bool(self.candidate_files and self.candidate_symbols and self.confidence >= 0.75)


_EXECUTION_TARGETS = {
    "system_action_controller.py": {
        "SystemActionController.handle_confirmation",
    },
    "document_command_router.py": {
        "handle_document_command", "_comparison_paths",
    },
    "request_interpreter.py": {
        "RequestInterpreter.interpret", "RequestInterpreter._explicit_attachment_reference",
        "ATTACHMENT_REFERENCE_PATTERN", "VAGUE_SIMPLE_NAMES",
    },
}


def _execution_entries(trace: dict[str, Any]) -> list[dict[str, str]]:
    entries = trace.get("execution_trace", [])
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


class CodeLocalizer:
    """N'émet que des fichiers présents et des symboles confirmés par l'AST."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def _symbols(self, relative: str) -> set[str]:
        target = (self.root / relative).resolve()
        if self.root not in target.parents or not target.is_file():
            return set()
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"), filename=relative)
        except (OSError, UnicodeDecodeError, SyntaxError):
            return set()
        symbols = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                symbols.update(target.id for target in targets if isinstance(target, ast.Name))
            if isinstance(node, ast.ClassDef):
                symbols.update(
                    f"{node.name}.{child.name}" for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        return symbols

    @staticmethod
    def _failed_contracts(root_cause) -> set[str]:
        return {
            item.name for case in root_cause.cases for item in case.criteria if not item.passed
        }

    def _trace_targets(self, root_cause) -> tuple[dict[str, set[str]], list[str]]:
        targets: dict[str, set[str]] = {}
        evidence = []
        representative = set(root_cause.representative_public_scenario_ids)
        for case in root_cause.cases:
            if case.scenario_id not in representative:
                continue
            for entry in _execution_entries(case.trace):
                component = str(entry.get("component", "")).replace("\\", "/")
                symbol = str(entry.get("symbol", ""))
                if component in _EXECUTION_TARGETS and symbol in _EXECUTION_TARGETS[component]:
                    targets.setdefault(component, set()).add(symbol)
                    evidence.append(
                        f"trace publique {case.scenario_id}: {component}:{symbol} exécuté"
                    )
        return targets, evidence

    def _contract_targets(self, root_cause) -> tuple[dict[str, set[str]], list[str]]:
        contracts = self._failed_contracts(root_cause)
        stage = root_cause.likely_behavioral_layer
        targets: dict[str, set[str]] = {}
        evidence = []
        if stage == "confirmation_detection" and any(
            name == "confirmed_call_count" or name.startswith(("confirmation.", "initial."))
            for name in contracts
        ):
            targets["system_action_controller.py"] = {"SystemActionController.handle_confirmation"}
        elif stage == "document_routing" and any(
            name.startswith(("calls.", "result.")) for name in contracts
        ):
            targets["document_command_router.py"] = {"handle_document_command", "_comparison_paths"}
        elif stage in {"attachment_resolution", "context_resolution", "intent_detection", "normalization"} and any(
            name.startswith("steps.") for name in contracts
        ):
            symbols = {"RequestInterpreter.interpret"}
            if stage in {"attachment_resolution", "context_resolution"}:
                symbols.update({"RequestInterpreter._explicit_attachment_reference", "ATTACHMENT_REFERENCE_PATTERN"})
            if stage in {"intent_detection", "normalization"}:
                symbols.add("VAGUE_SIMPLE_NAMES")
            targets["request_interpreter.py"] = symbols
        if targets:
            evidence.append(
                f"contrat public {sorted(contracts)!r} cohérent avec l'étape {stage}"
            )
        return targets, evidence

    def localize(self, root_cause) -> CodeLocalization:
        targets, evidence = self._trace_targets(root_cause)
        method, confidence = "traces_internal", 0.95
        if not targets:
            targets, evidence = self._contract_targets(root_cause)
            method, confidence = "failed_contract_static_mapping", 0.82
        verified: dict[str, list[str]] = {}
        for relative, proposed_symbols in targets.items():
            available = self._symbols(relative)
            existing = sorted(proposed_symbols & available)
            if existing:
                verified[relative] = existing
                evidence.append(
                    f"AST: {relative} contient {', '.join(existing)}"
                )
        if not verified:
            return CodeLocalization([], [], evidence, 0.0, "localization_insufficient")
        files = sorted(verified)
        symbols = sorted({symbol for values in verified.values() for symbol in values})
        return CodeLocalization(files, symbols, evidence, confidence, method)

    @staticmethod
    def reached(localization: CodeLocalization, report) -> tuple[bool, bool, list[str]]:
        reached_files, reached_symbols = set(), set()
        for result in report.results:
            for entry in _execution_entries(result.trace):
                reached_files.add(str(entry.get("component", "")).replace("\\", "/"))
                reached_symbols.add(str(entry.get("symbol", "")))
        component_reached = bool(set(localization.candidate_files) & reached_files)
        symbol_reached = bool(set(localization.candidate_symbols) & reached_symbols)
        evidence = [
            f"component_reached={component_reached}", f"symbol_reached={symbol_reached}",
        ]
        return component_reached, symbol_reached, evidence

