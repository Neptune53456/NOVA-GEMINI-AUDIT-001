"""Deterministic repository grounding for the minimal Planner contract.

Facts are produced locally from :class:`RepoIntelligence`; the model only
chooses ephemeral references and describes the intended change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from self_improvement.agent_path_policy import is_agent_editable_path
from self_improvement.repo_intelligence import RepoIntelligence


@dataclass(frozen=True)
class GroundedFile:
    reference: str
    path: str
    score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroundedSymbol:
    reference: str
    qualified_name: str
    file_path: str
    symbol_type: str
    line: int
    score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryGrounding:
    version: str
    candidate_files: tuple[GroundedFile, ...]
    candidate_symbols: tuple[GroundedSymbol, ...]
    related_tests: tuple[str, ...]
    dependency_neighbors: dict[str, tuple[str, ...]]
    forbidden_targets: tuple[str, ...]
    evidence: tuple[str, ...]
    confidence: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def resolve(self, reference: str) -> tuple[str, str | None]:
        for item in self.candidate_files:
            if item.reference == reference:
                return item.path, None
        for item in self.candidate_symbols:
            if item.reference == reference:
                return item.file_path, item.qualified_name
        raise ValueError(f"UNKNOWN_TARGET_REFERENCE: {reference}")

    def render_for_planner(self) -> str:
        lines = ["REPOSITORY GROUNDING (deterministic; references valid for this run only):"]
        for item in self.candidate_files:
            lines.append(f"- {item.reference}: file={item.path} score={item.score:.1f}")
        for item in self.candidate_symbols:
            lines.append(
                f"- {item.reference}: symbol={item.qualified_name} type={item.symbol_type} "
                f"file={item.file_path} line={item.line} score={item.score:.1f}"
            )
        if self.related_tests:
            lines.append("Related tests (added by the system): " + ", ".join(self.related_tests))
        behavior_evidence = [item for item in self.evidence if item.startswith("required_behavior:")]
        if behavior_evidence:
            lines.append("Required behavior grounding (deterministic source evidence):")
            lines.extend(f"- {item.removeprefix('required_behavior:')}" for item in behavior_evidence)
        lines.append("Choose only F*/S* references above; never return a free-form path.")
        return "\n".join(lines)


@dataclass(frozen=True)
class PlannerAction:
    action_type: str
    target_reference: str
    intent: str
    dependencies: tuple[str, ...] = ()
    test_intent: str | None = None
    covers_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannerDecision:
    version: str
    summary: str
    actions: tuple[PlannerAction, ...]


def build_repository_grounding(
    intelligence: RepoIntelligence,
    objective: str,
    *,
    max_files: int = 8,
    max_symbols: int = 15,
    required_files: Iterable[str] = (),
    forbidden_targets: Iterable[str] = (),
    exclude_existing_tests: bool = False,
    project_targeter: Callable[[str], Any] | None = None,
) -> RepositoryGrounding:
    """Build a bounded, reproducible shortlist without executing repository code."""
    file_limit = max(1, min(int(max_files), 20))
    symbol_limit = max(1, min(int(max_symbols), 40))
    ranked = intelligence.relevant_files(objective, limit=file_limit * 2)
    # ProjectBrain is advisory: every suggestion is independently verified by
    # RepoIntelligence and the editable-path policy before it becomes a reference.
    brain_paths: list[str] = []
    brain_symbols: set[str] = set()
    if project_targeter is not None:
        try:
            target = project_targeter(objective)
            paths = getattr(target, "relevant_files", []) if target is not None else []
            symbols = getattr(target, "relevant_symbols", []) if target is not None else []
            for raw in paths if isinstance(paths, (list, tuple)) else ():
                path = str(raw).replace("\\", "/")
                if not is_agent_editable_path(path) or path in brain_paths:
                    continue
                intelligence.inspect_file(path)
                brain_paths.append(path)
            brain_symbols = {str(item) for item in symbols if isinstance(item, str)}
        except Exception:
            pass
    if brain_paths:
        promoted = [intelligence.inspect_file(path) for path in brain_paths]
        ranked = [*promoted, *(item for item in ranked if item.path not in brain_paths)]
    explicit_contract_values = [
        *re.findall(r"(?:attendu|expected)\s+equals\s+['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]", objective, re.IGNORECASE),
        *re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", objective),
    ]
    technical_requirements = tuple(dict.fromkeys(
        token for token in explicit_contract_values
        if "_" in token or token.isupper()
    ))[:16]
    # A failed public contract often names the exact action/error value while
    # the surrounding objective contains much more narrative text.  Promote
    # source files that actually reference those technical requirements.  The
    # search is read-only and still passes through the editable-path policy.
    semantic_paths: list[str] = []
    for requirement in technical_requirements:
        for hit in intelligence.references_to(requirement, limit=20):
            if Path(hit.path).name.casefold().startswith("test_"):
                continue
            if is_agent_editable_path(hit.path) and hit.path not in semantic_paths:
                semantic_paths.append(hit.path)
    if semantic_paths:
        promoted = []
        for path in semantic_paths:
            try:
                promoted.append(intelligence.inspect_file(path))
            except ValueError:
                continue
        ranked = [*promoted, *(item for item in ranked if item.path not in semantic_paths)]
    by_path = {item.path: item for item in ranked}
    for raw in required_files:
        path = str(raw).replace("\\", "/")
        try:
            by_path.setdefault(path, intelligence.inspect_file(path))
        except ValueError:
            continue

    def is_test_path(path: str) -> bool:
        candidate = Path(path)
        return candidate.name.casefold().startswith("test_") or "tests" in {
            part.casefold() for part in candidate.parts[:-1]
        }

    editable = [
        item for item in by_path.values()
        if is_agent_editable_path(item.path)
        and not (exclude_existing_tests and is_test_path(item.path))
    ]
    editable.sort(key=lambda item: (ranked.index(item) if item in ranked else -1, item.path))
    selected = editable[:file_limit]
    files = tuple(
        GroundedFile(f"F{index}", info.path, float(file_limit - index + 1),
                     ("project_brain_target", "objective_relevance") if info.path in brain_paths else ("objective_relevance",))
        for index, info in enumerate(selected, start=1)
    )

    symbols_raw = []
    objective_tokens = intelligence._tokens(objective)
    for file_rank, info in enumerate(selected):
        for symbol in info.symbols:
            name = (symbol.qualname or symbol.name).casefold()
            hits = sum(token in name for token in objective_tokens)
            semantic_hits = 0
            try:
                source = intelligence.read_symbol(info.path, symbol.qualname or symbol.name)
                if symbol.kind != "class":
                    semantic_hits = sum(
                        bool(re.search(rf"\b{re.escape(requirement)}\b", source))
                        for requirement in technical_requirements
                    )
            except ValueError:
                pass
            score = float(semantic_hits * 40 + hits * 10 + file_limit - file_rank)
            symbols_raw.append((score, info.path, symbol))
    symbols_raw.sort(key=lambda row: (-row[0], row[1], row[2].line, row[2].qualname))
    symbols = tuple(
        GroundedSymbol(
            f"S{index}", symbol.qualname or symbol.name, path, symbol.kind,
            symbol.line, score, (
                "ast_symbol",
                "required_behavior_reference" if score >= 40 else (
                    "objective_token" if score >= 10 else "file_relevance"
                ),
                *( ("project_brain_symbol",) if (symbol.qualname or symbol.name) in brain_symbols else () ),
            ),
        )
        for index, (score, path, symbol) in enumerate(symbols_raw[:symbol_limit], start=1)
    )
    paths = [item.path for item in files]
    tests = tuple(intelligence.find_tests_for(paths, limit=12))
    neighbors: dict[str, tuple[str, ...]] = {}
    for item in files:
        raw = intelligence.dependency_neighbors(item.path, limit=8)
        neighbors[item.path] = tuple(dict.fromkeys(raw["imports"] + raw["imported_by"]))
    confidence = 0.0 if not files else min(1.0, 0.45 + 0.05 * len(files) + (0.15 if symbols else 0.0))
    behavior_evidence = []
    for requirement in technical_requirements:
        related = [
            f"{item.reference}={item.qualified_name}@{item.file_path}"
            for item in symbols
            if "required_behavior_reference" in item.reasons
            and re.search(
                rf"\b{re.escape(requirement)}\b",
                intelligence.read_symbol(item.file_path, item.qualified_name),
            )
        ]
        if related:
            behavior_evidence.append(
                f"required_behavior:{requirement} -> {', '.join(related[:6])}"
            )
    return RepositoryGrounding(
        version="repository-grounding/v1",
        candidate_files=files,
        candidate_symbols=symbols,
        related_tests=tests,
        dependency_neighbors=neighbors,
        forbidden_targets=tuple(dict.fromkeys(str(item) for item in forbidden_targets)),
        evidence=tuple([
            *(f"objective_relevance:{item.path}" for item in files),
            *(f"project_brain_target:{item.path}" for item in files if item.path in brain_paths),
            *behavior_evidence,
        ]),
        confidence=round(confidence, 3),
        reasons=(
            "bounded_static_ast", "deterministic_test_mapping", "editable_path_filter",
            *(("project_brain_advisory",) if brain_paths else ()),
        ),
    )
