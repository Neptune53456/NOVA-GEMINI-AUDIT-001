"""Bounded deterministic evidence for root-cause reasoning before patching."""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import textwrap
from typing import Iterable

from self_improvement.experiment_memory import sanitize_text
from self_improvement.repo_intelligence import RepoIntelligence, RepoSymbol


@dataclass(frozen=True)
class SymbolEvidence:
    path: str
    symbol: str
    relation: str
    source: str


@dataclass(frozen=True)
class RootCauseEvidencePack:
    expected_behavior: str
    observed_behavior: str
    suspected_target: str
    target_contract: str
    symbols: tuple[SymbolEvidence, ...] = ()
    relevant_tests: tuple[str, ...] = ()
    prior_failures: tuple[str, ...] = ()
    target_appears_satisfied: bool = False
    escalation_reason: str = ""
    total_chars: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        lines = [
            "ROOT-CAUSE EVIDENCE (bounded, deterministic, non-authoritative)",
            f"EXPECTED BEHAVIOR: {self.expected_behavior or 'not explicitly established'}",
            f"OBSERVED BEHAVIOR: {self.observed_behavior or 'not supplied'}",
            f"SUSPECTED TARGET (not necessarily root cause): {self.suspected_target}",
            f"SYMBOL CONTRACT TO PRESERVE: {self.target_contract or 'not available'}",
        ]
        for item in self.symbols:
            lines.append(f"[{item.relation}] {item.path}::{item.symbol}\n{item.source}")
        if self.relevant_tests:
            lines.append("RELEVANT PUBLIC TESTS: " + ", ".join(self.relevant_tests))
        if self.prior_failures:
            lines.append("KNOWN INVALIDATED ATTEMPTS: " + " | ".join(self.prior_failures))
        lines.extend([
            "ROOT-CAUSE CHECK REQUIRED: compare expected, observed, current implementation, causal path, and patch hypothesis.",
            "If the suspected target already implements the behavior, do not rewrite it; request replanning toward a caller/adapter/state boundary.",
            "Preserve symbol identity, class membership, decorators, async/sync kind, and parameters unless an API change is explicitly required.",
        ])
        return "\n\n".join(lines)


def _contract(source: str, symbol: RepoSymbol) -> str:
    try:
        node = ast.parse(textwrap.dedent(source)).body[0]
    except (SyntaxError, IndexError):
        return f"{symbol.kind} {symbol.qualname or symbol.name}"
    decorators = [ast.unparse(item) for item in getattr(node, "decorator_list", [])]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        owner = f"class={symbol.parent}" if symbol.parent else "module-level"
        return f"{prefix} {node.name}({ast.unparse(node.args)}); {owner}; decorators={decorators}"
    return f"class {getattr(node, 'name', symbol.name)}; decorators={decorators}"


def _callee_names(source: str) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name and name not in names:
                names.append(name)
    return names[:4]


def _explicit_literals(text: str) -> set[str]:
    patterns = (
        r"(?:expected|attendu|error_code|required_behavior)[^\n]{0,50}?[=:]\s*['\"]([^'\"]{2,80})['\"]",
        r"(?:expected|attendu)[^\n]{0,40}?\b([A-Z][A-Z0-9_]{3,})\b",
        r"(?:error[_ -]?code|code d.erreur|return|retourne|produce|use)[^\n]{0,40}?\b([A-Z][A-Z0-9_]{3,})\b",
    )
    return {match.group(1) for pattern in patterns for match in re.finditer(pattern, text, re.IGNORECASE)}


def build_root_cause_evidence(
    intelligence: RepoIntelligence, *, target_path: str, target_symbol: str = "",
    expected_behavior: str = "", observed_behavior: str = "", tests: Iterable[str] = (),
    prior_failures: Iterable[str] = (), max_symbols: int = 5, max_chars: int = 12_000,
) -> RootCauseEvidencePack:
    """Build target and direct relations through the existing repository index."""
    path = Path(target_path)
    rel = path.resolve().relative_to(intelligence.repo_root).as_posix() if path.is_absolute() else path.as_posix()
    info = intelligence.inspect_file(rel)
    chosen = next((item for item in info.symbols if target_symbol in {item.name, item.qualname}), None)
    if chosen is None and len(info.symbols) == 1:
        chosen = info.symbols[0]
    if chosen is None:
        return RootCauseEvidencePack(sanitize_text(expected_behavior)[:1200], sanitize_text(observed_behavior)[:1200], f"{rel}::{target_symbol or '<unresolved>'}", "unresolved")

    source = intelligence.read_symbol(rel, chosen.qualname or chosen.name, max_chars=min(6000, max_chars // 2))
    evidence = [SymbolEvidence(rel, chosen.qualname or chosen.name, "suspected_target", source)]
    budget = max_chars - len(source)
    for hit in intelligence.references_to(chosen.name, limit=12):
        if len(evidence) >= max_symbols or budget <= 300:
            break
        if hit.path == rel and chosen.line <= hit.line <= chosen.end_line:
            continue
        snippet = hit.excerpt[:min(1200, budget)]
        evidence.append(SymbolEvidence(hit.path, chosen.name, "direct_reference_or_caller", snippet))
        budget -= len(snippet)
    for callee in _callee_names(source):
        if len(evidence) >= max_symbols or budget <= 300:
            break
        hits = intelligence.find_symbols(callee, limit=3)
        hit = next((item for item in hits if item.path == rel), hits[0] if hits else None)
        if hit is None or any(item.path == hit.path and item.symbol == hit.qualname for item in evidence):
            continue
        snippet = intelligence.read_symbol(hit.path, hit.qualname, max_chars=min(1600, budget))
        evidence.append(SymbolEvidence(hit.path, hit.qualname, "direct_callee", snippet))
        budget -= len(snippet)
    expected, observed = sanitize_text(expected_behavior)[:1200], sanitize_text(observed_behavior)[:1200]
    literals = _explicit_literals(expected + "\n" + observed)
    satisfied = bool(literals) and all(item in source for item in literals)
    pack = RootCauseEvidencePack(
        expected, observed, f"{rel}::{chosen.qualname or chosen.name}", _contract(source, chosen), tuple(evidence),
        tuple(dict.fromkeys(str(item) for item in tests))[:8], tuple(sanitize_text(str(item))[:400] for item in prior_failures)[:4],
        satisfied, "suspected_target_already_contains_explicit_expected_contract" if satisfied else "",
    )
    return RootCauseEvidencePack(**{**pack.to_dict(), "symbols": pack.symbols, "total_chars": len(pack.render())})
