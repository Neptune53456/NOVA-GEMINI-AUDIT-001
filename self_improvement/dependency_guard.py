"""Détection déterministe de dépendances Python nouvellement introduites."""

from __future__ import annotations

import ast
from dataclasses import dataclass, asdict
from pathlib import Path
import re
import sys
from typing import Any, Iterable


@dataclass(frozen=True)
class DependencyFinding:
    imports: list[str]
    missing_from_requirements: list[str]
    requirements_file: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DependencyGuard:
    """Signale les imports tiers nouveaux qui ne sont ni locaux ni déclarés."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def inspect_candidates(self, candidates: Iterable[tuple[str, str, str]]) -> DependencyFinding:
        candidate_list = list(candidates)
        introduced: set[str] = set()
        candidate_local_modules: set[str] = set()
        for raw_path, _before, _after in candidate_list:
            path = Path(raw_path)
            if path.suffix.casefold() == ".py":
                candidate_local_modules.add(path.stem)
                try:
                    rel = path.resolve(strict=False).relative_to(self.repo_root)
                    if len(rel.parts) > 1:
                        candidate_local_modules.add(rel.parts[0])
                except ValueError:
                    pass
        for raw_path, before, after in candidate_list:
            if Path(raw_path).suffix.casefold() != ".py":
                continue
            before_imports = self._imports(before)
            after_imports = self._imports(after)
            introduced.update(after_imports - before_imports)
        third_party = sorted(name for name in introduced if name not in candidate_local_modules and self._is_third_party(name))
        req_path = self._requirements_path()
        declared = self._declared(req_path) if req_path else set()
        missing = sorted(name for name in third_party if self._normalize(name) not in declared)
        return DependencyFinding(third_party, missing, req_path.relative_to(self.repo_root).as_posix() if req_path else None)

    @staticmethod
    def _imports(content: str) -> set[str]:
        try:
            tree = ast.parse(content or "")
        except SyntaxError:
            return set()
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".", 1)[0])
        return found

    def _is_third_party(self, name: str) -> bool:
        if name in sys.stdlib_module_names:
            return False
        if (self.repo_root / f"{name}.py").is_file() or (self.repo_root / name / "__init__.py").is_file():
            return False
        return True

    def _requirements_path(self) -> Path | None:
        for name in ("requirements.txt", "requirements-dev.txt"):
            path = self.repo_root / name
            if path.is_file():
                return path
        return None

    @classmethod
    def _normalize(cls, value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).casefold()

    def _declared(self, path: Path) -> set[str]:
        result: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return result
        for line in lines:
            clean = line.split("#", 1)[0].strip()
            if not clean or clean.startswith(("-r", "--", "git+", "http:" , "https:")):
                continue
            package = re.split(r"[<>=!~\[;\s]", clean, maxsplit=1)[0].strip()
            if package:
                result.add(self._normalize(package))
        return result
