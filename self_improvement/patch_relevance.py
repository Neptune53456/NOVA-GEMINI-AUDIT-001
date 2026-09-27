"""Deterministic checks for whether a candidate patch addresses its task."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field


_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_PATTERN = re.compile(r"[a-z][A-Z]")
_ADD_PATTERN = re.compile(r"\b(?:add|ajoute|ajouter|ajout|introduit|introduire|create|crée|creer|implémente|implement)\b", re.IGNORECASE)
_FILENAME_PATTERN = re.compile(r"\b[\w./\\-]+\.(?:py|json|md|txt|ya?ml|ini|toml|cfg|csv)\b", re.IGNORECASE)
_OPTIONAL_ALTERNATIVE_PATTERN = re.compile(
    r"`?[A-Za-z_][A-Za-z0-9_]*`?\s*\(\s*(?:ou|or)\b[^)]*\)",
    re.IGNORECASE,
)
_WEAK_IDENTIFIERS = {
    "add", "ajoute", "ajouter", "ajout", "create", "crée", "creer", "implement",
    "implémente", "fonction", "function", "classe", "class", "champ", "field",
    "résultat", "result", "message", "fichier", "file", "code", "change", "modifie",
}
_BUILTIN_PROSE_TYPES = {
    "zerodivisionerror", "valueerror", "typeerror", "keyerror", "indexerror",
    "filenotfounderror", "runtimeerror", "attributeerror", "stopiteration",
    "notimplementederror", "exception", "baseexception",
    "none", "bool", "int", "float", "str", "dict", "list", "tuple", "set",
    "bytes", "any", "optional", "union",
}


@dataclass
class PatchRelevanceResult:
    passed: bool
    abstained: bool = False
    reason: str | None = None
    required_identifiers: list[str] = field(default_factory=list)
    matched_identifiers: list[str] = field(default_factory=list)
    added_symbols: list[str] = field(default_factory=list)
    suspicious_symbols: list[str] = field(default_factory=list)
    requirement_coverage: list[dict[str, str | list[str]]] = field(default_factory=list)


def _changed_symbol_names(original: str, new: str) -> set[str]:
    before = _symbol_dumps(original)
    after = _symbol_dumps(new)
    return {name for name in set(before) | set(after) if before.get(name) != after.get(name)}


def _qualified_nodes(source: str) -> dict[str, ast.AST]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    nodes: dict[str, ast.AST] = {}

    def visit(body: list[ast.stmt], parent: str = "") -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qualified = f"{parent}.{node.name}" if parent else node.name
            nodes[qualified] = node
            if isinstance(node, ast.ClassDef):
                visit(node.body, qualified)

    visit(tree.body)
    return nodes


def _changed_requirement_evidence(
    original: str,
    new: str,
    requirements: list[str],
) -> dict[str, list[str]]:
    """Accept behavior values only inside an AST symbol changed by the patch."""
    before = _qualified_nodes(original)
    after = _qualified_nodes(new)
    changed = {
        name for name in set(before) | set(after)
        if name not in before or name not in after
        or ast.dump(before[name], include_attributes=False) != ast.dump(after[name], include_attributes=False)
    }
    evidence: dict[str, list[str]] = {item: [] for item in requirements}
    for symbol in sorted(changed):
        node = after.get(symbol)
        if node is None or isinstance(node, ast.ClassDef):
            continue
        names = {
            child.id for child in ast.walk(node) if isinstance(child, ast.Name)
        } | {
            child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
        }
        values = {
            child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        for requirement in requirements:
            if requirement in names or requirement in values:
                if symbol not in before and requirement.casefold() not in symbol.casefold():
                    continue
                evidence[requirement].append(symbol)
    return evidence


def requirement_coverage_map(
    task: str,
    file_pairs: list[tuple[str, str]],
) -> list[dict[str, str | list[str]]]:
    """Build deterministic behavior evidence from AST names and changed symbols."""
    requirements = _task_identifiers(task)
    after_names: set[str] = set()
    changed_symbols: set[str] = set()
    behavior_evidence: dict[str, list[str]] = {item: [] for item in requirements}
    for original, new in file_pairs:
        after_names |= _ast_names(new)
        changed_symbols |= _changed_symbol_names(original, new)
        current = _changed_requirement_evidence(original, new, requirements)
        for requirement, symbols in current.items():
            behavior_evidence[requirement].extend(symbols)
    coverage = []
    for index, requirement in enumerate(requirements, start=1):
        evidence = []
        if requirement in after_names:
            evidence.append(f"ast_name:{requirement}")
        evidence.extend(
            f"changed_symbol_value:{symbol}"
            for symbol in sorted(set(behavior_evidence.get(requirement, [])))
        )
        related = sorted(
            symbol for symbol in changed_symbols
            if requirement.casefold() in symbol.casefold()
            or symbol.casefold() in requirement.casefold()
        )
        evidence.extend(f"changed_symbol:{symbol}" for symbol in related)
        status = "COVERED" if evidence else "MISSING"
        coverage.append({
            "requirement_id": f"behavior_{index:03d}",
            "expected_behavior": requirement,
            "target_symbol": related[0] if len(related) == 1 else "",
            "evidence_in_patch": evidence,
            "status": status,
        })
    return coverage


def required_behavior_contract(task: str) -> str:
    identifiers = _task_identifiers(task)
    if not identifiers:
        return ""
    lines = ["REQUIRED BEHAVIORS (authoritative task contract):"]
    lines.extend(f"- behavior_{index:03d}: {name}" for index, name in enumerate(identifiers, 1))
    lines.append("Implement these behaviors in the responsible source symbol; do not merely mention them.")
    return "\n".join(lines)


def _task_identifiers(task: str) -> list[str]:
    # Une API citée comme alternative explicite ("BaseProvider (ou object)")
    # n'est pas un identifiant obligatoire. Le garde doit vérifier le résultat
    # demandé, pas imposer l'une des branches optionnelles du Planner.
    required_task = _OPTIONAL_ALTERNATIVE_PATTERN.sub(" ", task)
    paths = _FILENAME_PATTERN.findall(required_task)
    path_names = {
        name.casefold()
        for raw in paths
        for name in [
            *re.split(r"[/\\]", raw),
            re.sub(r"\.[^.]+$", "", re.split(r"[/\\]", raw)[-1]),
        ]
        if name
    }
    explicit = [
        token for token in re.findall(r"`([^`]+)`", required_task)
        if token.casefold() not in path_names
    ]
    cleaned_task = _FILENAME_PATTERN.sub(" ", required_task)
    tokens = _IDENTIFIER_PATTERN.findall(cleaned_task)
    identifiers: list[str] = []
    for token in [*explicit, *tokens]:
        if not _IDENTIFIER_PATTERN.fullmatch(token):
            continue
        if token.casefold() in path_names and token not in explicit:
            continue
        if token.casefold() in _WEAK_IDENTIFIERS:
            continue
        if token.casefold() in _BUILTIN_PROSE_TYPES and token not in explicit:
            continue
        technical = "_" in token or bool(_CAMEL_PATTERN.search(token)) or token in explicit
        if technical and token not in identifiers:
            identifiers.append(token)
    return identifiers


def _ast_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and _IDENTIFIER_PATTERN.fullmatch(key.value):
                    names.add(key.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _top_level_symbols(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _symbol_dumps(source: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    return {
        node.name: ast.dump(node, include_attributes=False)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def check_patch_relevance(task: str, original_content: str, new_content: str) -> PatchRelevanceResult:
    """Evaluate strong task/code signals without using comments or string literals."""
    if original_content == new_content:
        return PatchRelevanceResult(False, reason="no_change")

    required = _task_identifiers(task)
    coverage = requirement_coverage_map(task, [(original_content, new_content)])
    before_names = _ast_names(original_content)
    after_names = _ast_names(new_content)
    covered = {
        str(item["expected_behavior"])
        for item in coverage if item["status"] == "COVERED"
    }
    matched = sorted(identifier for identifier in required if identifier in covered)
    added_symbols = sorted(_top_level_symbols(new_content) - _top_level_symbols(original_content))

    if not required:
        return PatchRelevanceResult(True, abstained=True, reason="no_strong_identifier", matched_identifiers=matched, added_symbols=added_symbols)

    adding_requested = bool(_ADD_PATTERN.search(task))
    missing = [identifier for identifier in required if identifier not in covered]
    if adding_requested:
        missing = [
            identifier for identifier in required
            if identifier not in before_names and identifier not in covered
        ]

    symbol_dumps_before = _symbol_dumps(original_content)
    symbol_dumps_after = _symbol_dumps(new_content)
    mentioned_existing_symbols = [identifier for identifier in required if identifier in symbol_dumps_before]
    unchanged_requested = [
        identifier for identifier in mentioned_existing_symbols
        if symbol_dumps_before.get(identifier) == symbol_dumps_after.get(identifier)
    ]

    if missing:
        suspicious = [
            symbol for symbol in added_symbols
            if symbol not in required
            and symbol.lstrip("_") not in required
            and not any(symbol.lstrip("_").startswith(identifier) for identifier in required)
        ]
        reason = "relevance_missing_required_identifier" if not suspicious else "irrelevant_patch"
        return PatchRelevanceResult(
            False,
            reason=reason,
            required_identifiers=required,
            matched_identifiers=matched,
            added_symbols=added_symbols,
            suspicious_symbols=suspicious,
            requirement_coverage=coverage,
        )

    if not adding_requested and mentioned_existing_symbols and len(unchanged_requested) == len(mentioned_existing_symbols):
        return PatchRelevanceResult(
            False,
            reason="relevance_target_symbol_unchanged",
            required_identifiers=required,
            matched_identifiers=matched,
            added_symbols=added_symbols,
            requirement_coverage=coverage,
        )

    return PatchRelevanceResult(
        True,
        reason="required_identifiers_matched",
        required_identifiers=required,
        matched_identifiers=matched,
        added_symbols=added_symbols,
        requirement_coverage=coverage,
    )


def check_patch_relevance_multi(
    task: str,
    file_pairs: list[tuple[str, str]],
) -> PatchRelevanceResult:
    """Evaluate relevance of a multi-file patch as a whole.

    Each element of *file_pairs* is ``(original_content, new_content)`` for one
    file in the batch.  Required identifiers extracted from the task are looked
    up in the *union* of all after-names across every file, which prevents false
    ``relevance_missing_required_identifier`` rejections when identifiers are
    legitimately spread across multiple files.

    The ``relevance_target_symbol_unchanged`` check is only triggered when every
    file that contained the symbol before still contains it unchanged after —
    i.e. no file in the batch actually modified that symbol.
    """
    # Require at least one pair with an actual change.
    changed_pairs = [(orig, new) for orig, new in file_pairs if orig != new]
    if not changed_pairs:
        return PatchRelevanceResult(False, reason="no_change")

    required = _task_identifiers(task)
    coverage = requirement_coverage_map(task, file_pairs)
    covered = {
        str(item["expected_behavior"])
        for item in coverage if item["status"] == "COVERED"
    }

    # Aggregate names and symbol-dumps across all files.
    combined_before_names: set[str] = set()
    combined_after_names: set[str] = set()
    combined_added_symbols: list[str] = []

    # Per-symbol: track whether it changed in at least one file.
    # symbol_changed_in_any[sym] = True if sym was changed/added in any file.
    symbol_changed_in_any: dict[str, bool] = {}

    for orig, new in file_pairs:
        combined_before_names |= _ast_names(orig)
        combined_after_names |= _ast_names(new)
        dumps_before = _symbol_dumps(orig)
        dumps_after = _symbol_dumps(new)
        # Newly added top-level symbols in this file.
        new_top = _top_level_symbols(new) - _top_level_symbols(orig)
        combined_added_symbols.extend(new_top)
        # Track per-symbol change status.
        all_syms = set(dumps_before) | set(dumps_after)
        for sym in all_syms:
            changed = dumps_before.get(sym) != dumps_after.get(sym)
            if sym not in symbol_changed_in_any:
                symbol_changed_in_any[sym] = changed
            elif changed:
                symbol_changed_in_any[sym] = True

    # Deduplicate added_symbols while preserving order.
    seen: set[str] = set()
    deduped_added: list[str] = []
    for s in combined_added_symbols:
        if s not in seen:
            seen.add(s)
            deduped_added.append(s)
    added_symbols = sorted(deduped_added)

    matched = sorted(identifier for identifier in required if identifier in covered)

    if not required:
        return PatchRelevanceResult(
            True, abstained=True, reason="no_strong_identifier",
            matched_identifiers=matched, added_symbols=added_symbols,
        )

    adding_requested = bool(_ADD_PATTERN.search(task))
    if adding_requested:
        missing = [
            identifier for identifier in required
            if identifier not in combined_before_names and identifier not in covered
        ]
    else:
        missing = [identifier for identifier in required if identifier not in covered]

    if missing:
        suspicious = [
            symbol for symbol in added_symbols
            if symbol not in required
            and symbol.lstrip("_") not in required
            and not any(symbol.lstrip("_").startswith(identifier) for identifier in required)
        ]
        reason = "relevance_missing_required_identifier" if not suspicious else "irrelevant_patch"
        return PatchRelevanceResult(
            False,
            reason=reason,
            required_identifiers=required,
            matched_identifiers=matched,
            added_symbols=added_symbols,
            suspicious_symbols=suspicious,
            requirement_coverage=coverage,
        )

    # Unchanged-symbol check: only apply for "modify"-type tasks (not "add/create").
    # For "add" tasks, the missing-identifiers check above already validates that
    # the required name exists in the after-names; requiring the symbol to also
    # *change* its AST is incorrect (the identifier may legitimately pre-exist).
    if not adding_requested:
        # Only reject when a required symbol that existed as a top-level definition
        # before is UNCHANGED in every file of the batch.
        mentioned_existing = [
            identifier for identifier in required
            if identifier in symbol_changed_in_any  # was a top-level def in some file
        ]
        truly_unchanged = [
            identifier for identifier in mentioned_existing
            if not symbol_changed_in_any.get(identifier, False)
        ]

        if mentioned_existing and len(truly_unchanged) == len(mentioned_existing):
            return PatchRelevanceResult(
                False,
                reason="relevance_target_symbol_unchanged",
                required_identifiers=required,
                matched_identifiers=matched,
                added_symbols=added_symbols,
                requirement_coverage=coverage,
            )

    return PatchRelevanceResult(
        True,
        reason="required_identifiers_matched",
        required_identifiers=required,
        matched_identifiers=matched,
        added_symbols=added_symbols,
        requirement_coverage=coverage,
    )
