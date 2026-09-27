"""Deterministic Target-ID binding before the canonical PatchProtocol gate."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any


class PatchTargetBindingError(ValueError):
    pass


@dataclass(frozen=True)
class CanonicalPatchTarget:
    target_id: str
    canonical_relative_path: str
    target_symbols: tuple[str, ...]
    allowed_operation_types: tuple[str, ...]
    requirement_ids: tuple[str, ...] = ()


def resolve_target_id(target_id: str, targets: tuple[CanonicalPatchTarget, ...]) -> CanonicalPatchTarget:
    matches = [item for item in targets if item.target_id == target_id]
    if len(matches) != 1:
        raise PatchTargetBindingError(f"PATCH_TARGET_FAILURE: unknown or ambiguous target_id={target_id!r}")
    return matches[0]


def _symbols(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def build_canonical_target(path: str | Path, source: str, *, project_root: str | Path | None = None,
                           instruction: str = "") -> CanonicalPatchTarget:
    resolved = Path(path).resolve(strict=False)
    root = Path(project_root).resolve(strict=False) if project_root else resolved.parent
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        relative = resolved.name
    available_symbols = sorted(_symbols(source))
    # Semantic repair carries an explicit machine-readable target marker. Use it
    # preferentially so symbols merely present inside embedded before/after code
    # cannot widen the canonical target set.
    marked_identifiers: set[str] = set()
    marker = re.search(r"(?m)^CANONICAL_TARGET_SYMBOLS:\s*(\[[^\n]*\])\s*$", instruction)
    if marker:
        try:
            raw_marked = json.loads(marker.group(1))
        except json.JSONDecodeError:
            raw_marked = []
        if isinstance(raw_marked, list):
            marked_identifiers = {
                str(item).rsplit(".", 1)[-1]
                for item in raw_marked
                if isinstance(item, str) and item.strip()
            }

    # Keep every explicitly named symbol, regardless of name length. Qualified
    # references select their final component, not the containing class; words
    # and filenames must not select symbols through substring matches.
    identifiers = marked_identifiers or {
        token.rsplit(".", 1)[-1]
        for token in re.findall(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", instruction)
    }
    mentioned = [symbol for symbol in available_symbols if symbol in identifiers]
    return CanonicalPatchTarget(
        "T1", relative, tuple(mentioned or available_symbols),
        ("replace", "append", "replace_symbol_block", "insert_after_anchor", "insert_before_anchor"),
    )


def _consistent(operation: dict[str, Any], target: CanonicalPatchTarget, source: str) -> bool:
    operation_type = operation.get("operation_type", operation.get("action"))
    if operation_type not in target.allowed_operation_types:
        return False
    symbol = operation.get("target_symbol", operation.get("symbol", operation.get("target")))
    if isinstance(symbol, str) and symbol:
        return symbol.rsplit(".", 1)[-1] in target.target_symbols
    old_content = operation.get("old_content", operation.get("old_text"))
    if isinstance(old_content, str) and old_content and source.count(old_content) == 1:
        return True
    anchor = operation.get("anchor")
    if isinstance(anchor, str) and anchor and source.count(anchor) == 1:
        return True
    new_content = operation.get("new_content", operation.get("new_text", operation.get("content")))
    return isinstance(new_content, str) and bool(_symbols(new_content) & set(target.target_symbols))


def bind_patch_target(payload: Any, *, target: CanonicalPatchTarget, source_content: str,
                      provider: str | None) -> tuple[Any, dict[str, Any]]:
    decoded = payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return payload, {"target_binding": "not_parsed"}
    if not isinstance(decoded, dict) or not isinstance(decoded.get("operations"), (list, dict)):
        return payload, {"target_binding": "not_applicable"}
    operations = ([decoded["operations"]]
                  if isinstance(decoded["operations"], dict) else decoded["operations"])
    if not all(isinstance(item, dict) for item in operations):
        return payload, {"target_binding": "not_applicable"}

    bound = json.loads(json.dumps(decoded))
    bound_operations = ([bound["operations"]]
                        if isinstance(bound["operations"], dict) else bound["operations"])
    trace = []
    canonical_basename = Path(target.canonical_relative_path).name.casefold()
    for index, operation in enumerate(bound_operations, 1):
        target_id = operation.pop("target_id", None)
        raw_path = operation.get("file_path", operation.get("path", operation.get("file")))
        raw_basename = Path(str(raw_path).replace("\\", "/")).name if raw_path else None
        if target_id is not None and target_id != target.target_id:
            raise PatchTargetBindingError(f"PATCH_TARGET_FAILURE: operation={index} unknown target_id={target_id!r}")
        wrong_local_path = (str(provider or "").casefold() == "local" and bool(raw_path)
                            and raw_basename.casefold() != canonical_basename)
        requests_binding = (target_id == target.target_id
                            or (str(provider or "").casefold() == "local" and not raw_path)
                            or wrong_local_path)
        if requests_binding:
            operation_type = operation.get("operation_type", operation.get("action"))
            if (operation_type == "replace_symbol_block"
                    and not operation.get("target_symbol", operation.get("symbol", operation.get("target")))
                    and len(target.target_symbols) == 1):
                operation["target_symbol"] = target.target_symbols[0]
            if not _consistent(operation, target, source_content):
                symbol = operation.get("target_symbol", operation.get("symbol"))
                raise PatchTargetBindingError(
                    f"PATCH_TARGET_FAILURE: operation={index} target_id={target.target_id} "
                    f"raw_basename={raw_basename!r} canonical_basename={canonical_basename!r} "
                    f"operation_type={operation.get('operation_type', operation.get('action'))!r} "
                    f"symbol={symbol!r} inconsistent with canonical target"
                )
            operation["file_path"] = target.canonical_relative_path
            operation.pop("path", None)
            operation.pop("file", None)
        trace.append({
            "operation_index": index, "raw_path_basename": raw_basename,
            "target_id": target_id, "canonical_path": target.canonical_relative_path,
            "symbol": operation.get("target_symbol", operation.get("symbol")),
            "operation_type": operation.get("operation_type", operation.get("action")),
            "binding": "bound" if requests_binding else "unchanged",
        })
    return bound, {"target_binding": "passed", "target_binding_operations": trace}
