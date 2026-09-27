"""Repo Intelligence V3 — cartographie statique sûre et contexte ciblé pour les agents.

Ce module est volontairement déterministe : il n'appelle aucun modèle et n'exécute
aucun code du repository. Il construit une carte AST bornée, recherche les symboles
et sélectionne les fichiers pertinents pour un objectif logiciel.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Iterable

from self_improvement.experiment_memory import sanitize_text
from self_improvement.agent_path_policy import is_agent_readable_path, is_model_private_path

HOLDOUT_NAME = ".self_improvement_holdout"


DEFAULT_IGNORED_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    ".temp_tests",
    ".self_improvement_worktrees",
    ".self_improvement_discoveries",
    "benchmark_results",
    HOLDOUT_NAME,
})

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ_][A-Za-zÀ-ÖØ-öø-ÿ0-9_-]{2,}")
_STOPWORDS = frozenset({
    "ajoute", "ajouter", "add", "avec", "dans", "pour", "from", "into", "that",
    "this", "une", "des", "les", "the", "version", "v1",
    "feature", "implement", "implémenter", "créer", "create", "nouveau", "new",
})


@dataclass(frozen=True)
class RepoSymbol:
    name: str
    kind: str
    line: int
    end_line: int
    qualname: str = ""
    parent: str | None = None


@dataclass(frozen=True)
class CodeSearchHit:
    path: str
    line: int
    excerpt: str
    score: float


@dataclass(frozen=True)
class SymbolSearchHit:
    path: str
    name: str
    qualname: str
    kind: str
    line: int
    score: float


@dataclass
class RepoFileInfo:
    path: str
    language: str
    lines: int = 0
    imports: list[str] = field(default_factory=list)
    symbols: list[RepoSymbol] = field(default_factory=list)
    parse_error: str | None = None
    text_preview: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["symbols"] = [asdict(item) for item in self.symbols]
        return payload


@dataclass
class RepoMap:
    packages: list[str]
    files: list[RepoFileInfo]
    source_tests: dict[str, list[str]]
    recently_modified: list[str]

    def to_dict(self) -> dict:
        return {
            "packages": self.packages,
            "files": [item.to_dict() for item in self.files],
            "source_tests": self.source_tests,
            "recently_modified": self.recently_modified,
        }


class RepoIntelligence:
    """Index statique borné d'un repository, sans accès au holdout."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        max_file_bytes: int = 300_000,
        ignored_dirs: Iterable[str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.max_file_bytes = max(1_000, int(max_file_bytes))
        self.ignored_dirs = frozenset(ignored_dirs or DEFAULT_IGNORED_DIRS)
        # Cache auto-invalidé par (mtime_ns, taille). Les agents appellent souvent
        # inspect/search/dependencies plusieurs fois sur les mêmes fichiers.
        self._analysis_cache: dict[str, tuple[tuple[int, int], RepoFileInfo, str]] = {}

    # ------------------------------------------------------------------
    # Paths / inventory
    # ------------------------------------------------------------------
    def _relative_safe(self, path: Path) -> str | None:
        try:
            rel = path.resolve(strict=False).relative_to(self.repo_root)
        except ValueError:
            return None
        if any(part in self.ignored_dirs or HOLDOUT_NAME.casefold() in part.casefold() for part in rel.parts):
            return None
        rel_text = rel.as_posix()
        if is_model_private_path(rel_text) or not is_agent_readable_path(rel_text):
            return None
        return rel_text

    def iter_files(self, *, suffixes: set[str] | None = None) -> list[str]:
        suffixes = suffixes or {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".ini", ".toml"}
        results: list[str] = []
        for path in self.repo_root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in suffixes:
                continue
            rel = self._relative_safe(path)
            if rel is not None:
                results.append(rel)
        return sorted(set(results))

    # ------------------------------------------------------------------
    # AST map
    # ------------------------------------------------------------------
    def inspect_file(self, relative_path: str) -> RepoFileInfo:
        path = (self.repo_root / relative_path).resolve(strict=False)
        rel = self._relative_safe(path)
        if rel is None or not path.is_file():
            raise ValueError(f"Fichier non autorisé ou introuvable : {relative_path}")
        try:
            stat = path.stat()
            if stat.st_size > self.max_file_bytes:
                return RepoFileInfo(path=rel, language=path.suffix.lstrip("."), parse_error="file_too_large")
            signature = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError as exc:
            return RepoFileInfo(path=rel, language=path.suffix.lstrip("."), parse_error=str(exc))

        cached = self._analysis_cache.get(rel)
        if cached and cached[0] == signature:
            return cached[1]

        content = path.read_text(encoding="utf-8", errors="replace")
        safe_content = sanitize_text(content)
        lines = safe_content.splitlines()
        preview = "\n".join(lines[:18])[:2400]
        if path.suffix.casefold() != ".py":
            info = RepoFileInfo(path=rel, language=path.suffix.lstrip("."), lines=len(lines), text_preview=preview)
            self._analysis_cache[rel] = (signature, info, safe_content)
            return info

        try:
            # L'AST est construit sur le contenu original pour ne pas fausser la
            # syntaxe si un secret est remplacé. Seules les sorties sont sanitizées.
            tree = ast.parse(content, filename=rel)
        except SyntaxError as exc:
            info = RepoFileInfo(
                path=rel,
                language="python",
                lines=len(lines),
                parse_error=f"{exc.msg} (line {exc.lineno})",
                text_preview=preview,
            )
            self._analysis_cache[rel] = (signature, info, safe_content)
            return info

        imports: list[str] = []
        symbols: list[RepoSymbol] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.append("." * int(node.level or 0) + module)

        def add_symbols(nodes, parent: str | None = None) -> None:
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = f"{parent}.{node.name}" if parent else node.name
                    symbols.append(RepoSymbol(
                        node.name,
                        "method" if parent else "function",
                        node.lineno,
                        getattr(node, "end_lineno", node.lineno),
                        qualname=qualname,
                        parent=parent,
                    ))
                    # Les fonctions imbriquées ne sont pas exposées comme API du
                    # module : elles restent lisibles via la fonction parente.
                elif isinstance(node, ast.ClassDef):
                    qualname = f"{parent}.{node.name}" if parent else node.name
                    symbols.append(RepoSymbol(
                        node.name, "class", node.lineno,
                        getattr(node, "end_lineno", node.lineno),
                        qualname=qualname, parent=parent,
                    ))
                    add_symbols(node.body, qualname)

        add_symbols(tree.body)
        info = RepoFileInfo(
            path=rel,
            language="python",
            lines=len(lines),
            imports=sorted(set(imports)),
            symbols=symbols,
            text_preview=preview,
        )
        self._analysis_cache[rel] = (signature, info, safe_content)
        return info

    def _safe_content(self, relative_path: str) -> str:
        """Retourne le contenu sanitizé en réutilisant le cache d'analyse."""
        info = self.inspect_file(relative_path)
        path = (self.repo_root / info.path).resolve(strict=False)
        try:
            stat = path.stat()
            signature = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            return ""
        cached = self._analysis_cache.get(info.path)
        if cached and cached[0] == signature:
            return cached[2]
        # Défensif : inspect_file ci-dessus devrait avoir peuplé le cache.
        return sanitize_text(path.read_text(encoding="utf-8", errors="replace"))

    def build_map(self, *, max_files: int = 160) -> list[RepoFileInfo]:
        paths = self.iter_files(suffixes={".py"})[: max(1, int(max_files))]
        return [self.inspect_file(path) for path in paths]

    def repo_map(self, *, max_files: int = 160, recent_limit: int = 20) -> RepoMap:
        """Carte structuree bornee, sans lecture des surfaces privees."""
        infos = self.build_map(max_files=max_files)
        packages = sorted({
            Path(info.path).parent.as_posix()
            for info in infos
            if info.language == "python" and Path(info.path).parent.as_posix() not in {".", ""}
        })
        source_tests: dict[str, list[str]] = {}
        for info in infos:
            path = Path(info.path)
            if info.language != "python" or path.name.startswith("test_") or "tests" in {part.casefold() for part in path.parts}:
                continue
            tests = self.find_tests_for([info.path], limit=6)
            if tests:
                source_tests[info.path] = tests
        dated: list[tuple[int, str]] = []
        for info in infos:
            try:
                dated.append(((self.repo_root / info.path).stat().st_mtime_ns, info.path))
            except OSError:
                continue
        dated.sort(reverse=True)
        return RepoMap(
            packages=packages,
            files=infos,
            source_tests=source_tests,
            recently_modified=[path for _, path in dated[: max(0, int(recent_limit))]],
        )

    # ------------------------------------------------------------------
    # Relevance / snippets
    # ------------------------------------------------------------------
    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token.casefold().replace("-", "_")
            for token in _TOKEN_RE.findall(text or "")
            if token.casefold() not in _STOPWORDS
        }

    def _score_file(self, info: RepoFileInfo, objective: str, content: str) -> float:
        tokens = self._tokens(objective)
        if not tokens:
            return 0.0
        path_text = info.path.casefold().replace("-", "_")
        symbol_text = " ".join(symbol.name for symbol in info.symbols).casefold()
        import_text = " ".join(info.imports).casefold()
        lower_content = content.casefold()
        score = 0.0
        for token in tokens:
            score += 14.0 if token in path_text else 0.0
            score += 9.0 if token in symbol_text else 0.0
            score += 4.0 if token in import_text else 0.0
            occurrences = lower_content.count(token)
            score += min(occurrences, 8) * 1.5
        if info.path.startswith("self_improvement/") and any(k in objective.casefold() for k in ("agent", "amélior", "improv", "planner", "orchestr")):
            score += 5.0
        if info.path.startswith("test_") or "/test_" in info.path:
            # Les tests restent utiles mais ne doivent pas masquer l'implémentation
            # lorsqu'un mot précis n'existe encore que dans un scénario de test.
            score -= 4.0
        return score

    def relevant_files(self, objective: str, *, limit: int = 12) -> list[RepoFileInfo]:
        ranked: list[tuple[float, RepoFileInfo]] = []
        for path in self.iter_files(suffixes={".py", ".md", ".txt", ".json", ".yaml", ".yml", ".ini", ".toml"}):
            info = self.inspect_file(path)
            try:
                content = self._safe_content(path)
            except OSError:
                content = ""
            score = self._score_file(info, objective, content)
            if score > 0:
                ranked.append((score, info))
        ranked.sort(key=lambda item: (-item[0], item[1].path))
        return [info for _score, info in ranked[: max(1, int(limit))]]

    def search_code(self, query: str, *, limit: int = 20) -> list[CodeSearchHit]:
        """Recherche textuelle bornée dans les fichiers publics du repository.

        Cet outil ne résout que des chemins déjà autorisés par l'inventaire et ne
        suit aucun symlink hors dépôt. Il retourne de courts extraits avec numéro
        de ligne, adaptés à une boucle d'agent.
        """
        if not isinstance(query, str) or not query.strip():
            return []
        tokens = self._tokens(query)
        literal = query.strip().casefold()
        hits: list[CodeSearchHit] = []
        for rel in self.iter_files(suffixes={".py", ".md", ".txt", ".json", ".yaml", ".yml", ".ini", ".toml"}):
            path = self.repo_root / rel
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
                lines = self._safe_content(rel).splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                lower = line.casefold()
                token_hits = sum(token in lower for token in tokens)
                literal_hit = bool(literal and literal in lower)
                if not literal_hit and not token_hits:
                    continue
                score = float(token_hits * 2 + (6 if literal_hit else 0))
                if rel.endswith('.py') and not Path(rel).name.startswith('test_'):
                    score += 0.5
                start = max(0, index - 2)
                end = min(len(lines), index + 1)
                excerpt = "\n".join(
                    f"{line_no + 1:>4}: {lines[line_no]}"
                    for line_no in range(start, end)
                )[:1200]
                hits.append(CodeSearchHit(rel, index, excerpt, score))
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.line))
        return hits[: max(1, min(int(limit), 50))]

    def find_symbols(self, query: str, *, limit: int = 24) -> list[SymbolSearchHit]:
        """Recherche AST de symboles sans exécuter le repository."""
        if not isinstance(query, str) or not query.strip():
            return []
        needle = query.strip().casefold()[:160]
        tokens = self._tokens(query)
        hits: list[SymbolSearchHit] = []
        for rel in self.iter_files(suffixes={".py"}):
            info = self.inspect_file(rel)
            for symbol in info.symbols:
                name = symbol.name.casefold()
                qualname = (symbol.qualname or symbol.name).casefold()
                exact = needle in {name, qualname}
                substring = needle in name or needle in qualname
                token_hits = sum(token in name or token in qualname for token in tokens)
                if not exact and not substring and not token_hits:
                    continue
                score = (12.0 if exact else 0.0) + (5.0 if substring else 0.0) + token_hits * 2.0
                if not Path(rel).name.startswith("test_"):
                    score += 0.5
                hits.append(SymbolSearchHit(
                    path=rel,
                    name=symbol.name,
                    qualname=symbol.qualname or symbol.name,
                    kind=symbol.kind,
                    line=symbol.line,
                    score=score,
                ))
        hits.sort(key=lambda item: (-item.score, item.path, item.line, item.qualname))
        return hits[: max(1, min(int(limit), 60))]

    def read_symbol(self, relative_path: str, symbol: str, *, max_chars: int = 12_000) -> str:
        """Lit exactement un symbole top-level Python identifié par l'AST."""
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Nom de symbole vide.")
        info = self.inspect_file(relative_path)
        matches = [item for item in info.symbols if item.name == symbol or item.qualname == symbol]
        if not matches:
            raise ValueError(f"Symbole introuvable : {relative_path}::{symbol}")
        if len(matches) != 1:
            raise ValueError(f"Symbole ambigu : {relative_path}::{symbol}")
        item = matches[0]
        lines = self._safe_content(info.path).splitlines()
        source = "\n".join(lines[item.line - 1:item.end_line])
        return source[: max(200, min(int(max_chars), 50_000))]


    def read_file(
        self,
        relative_path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 12_000,
    ) -> str:
        """Lit une plage bornée d'un fichier public sans exécuter son contenu."""
        path = (self.repo_root / relative_path).resolve(strict=False)
        rel = self._relative_safe(path)
        if rel is None or not path.is_file():
            raise ValueError(f"Fichier non autorisé ou introuvable : {relative_path}")
        try:
            if path.stat().st_size > self.max_file_bytes:
                raise ValueError(f"Fichier trop volumineux : {relative_path}")
        except OSError as exc:
            raise ValueError(f"Impossible de lire le fichier : {relative_path}: {exc}") from exc

        lines = self._safe_content(rel).splitlines()
        start = max(1, int(start_line or 1))
        requested_end = len(lines) if end_line is None else max(start, int(end_line))
        # Une lecture agent ne peut jamais aspirer un fichier entier gigantesque.
        stop = min(len(lines), requested_end, start + 199)
        rendered = "\n".join(
            f"{index:>4}: {lines[index - 1]}"
            for index in range(start, stop + 1)
        )
        return rendered[: max(200, min(int(max_chars), 50_000))]

    def references_to(self, symbol: str, *, limit: int = 30) -> list[CodeSearchHit]:
        """Recherche les références textuelles exactes à un symbole Python."""
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", symbol.strip()):
            raise ValueError("Nom de symbole invalide.")
        pattern = re.compile(rf"\b{re.escape(symbol.strip())}\b")
        hits: list[CodeSearchHit] = []
        for rel in self.iter_files(suffixes={".py"}):
            path = self.repo_root / rel
            try:
                lines = self._safe_content(rel).splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                if not pattern.search(line):
                    continue
                start = max(0, index - 2)
                end = min(len(lines), index + 1)
                excerpt = "\n".join(
                    f"{line_no + 1:>4}: {lines[line_no]}"
                    for line_no in range(start, end)
                )[:1200]
                score = 2.0 + (1.0 if not Path(rel).name.startswith("test_") else 0.0)
                hits.append(CodeSearchHit(rel, index, excerpt, score))
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.line))
        return hits[: max(1, min(int(limit), 60))]

    def dependency_neighbors(self, relative_path: str, *, limit: int = 24) -> dict[str, list[str]]:
        """Retourne les imports internes et dépendants directs d'un module Python.

        La résolution est statique et conservatrice : aucune importation Python n'est
        exécutée. Le résultat sert uniquement de contexte aux agents.
        """
        info = self.inspect_file(relative_path)
        if info.language != "python":
            return {"imports": [], "imported_by": []}

        python_files = self.iter_files(suffixes={".py"})
        module_to_path: dict[str, str] = {}
        path_to_module: dict[str, str] = {}
        for rel in python_files:
            path = Path(rel)
            parts = list(path.with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            module = ".".join(parts)
            if module:
                module_to_path[module] = rel
                path_to_module[rel] = module

        current_module = path_to_module.get(info.path, "")
        package = current_module.rsplit(".", 1)[0] if "." in current_module else ""

        def resolve_import(raw: str) -> str | None:
            name = raw.strip()
            if not name:
                return None
            if name.startswith("."):
                dots = len(name) - len(name.lstrip("."))
                tail = name[dots:]
                base_parts = package.split(".") if package else []
                if dots > 1:
                    base_parts = base_parts[: max(0, len(base_parts) - (dots - 1))]
                name = ".".join([part for part in [*base_parts, tail] if part])
            candidates = [name]
            pieces = name.split(".")
            candidates.extend(".".join(pieces[:i]) for i in range(len(pieces) - 1, 0, -1))
            for candidate in candidates:
                if candidate in module_to_path:
                    return module_to_path[candidate]
            return None

        imported: list[str] = []
        for raw in info.imports:
            resolved = resolve_import(raw)
            if resolved and resolved != info.path:
                imported.append(resolved)

        imported_by: list[str] = []
        current_names = {current_module}
        if current_module:
            current_names.add(current_module.split(".")[-1])
        for rel in python_files:
            if rel == info.path:
                continue
            other = self.inspect_file(rel)
            for raw in other.imports:
                normalized = raw.lstrip(".")
                if current_module and (
                    normalized == current_module
                    or normalized.startswith(current_module + ".")
                    or normalized in current_names
                ):
                    imported_by.append(rel)
                    break

        return {
            "imports": list(dict.fromkeys(imported))[: max(1, int(limit))],
            "imported_by": list(dict.fromkeys(imported_by))[: max(1, int(limit))],
        }

    def find_tests_for(self, paths: Iterable[str], *, limit: int = 12) -> list[str]:
        stems = {Path(path).stem.casefold() for path in paths if path}
        if not stems:
            return []
        candidates: list[tuple[int, str]] = []
        for test_path in self.iter_files(suffixes={".py"}):
            name = Path(test_path).name.casefold()
            if not (name.startswith("test_") or "/test_" in f"/{test_path.casefold()}"):
                continue
            try:
                text = self._safe_content(test_path).casefold()
            except OSError:
                continue
            score = sum(5 for stem in stems if stem in name) + sum(text.count(stem) for stem in stems)
            if score:
                candidates.append((score, test_path))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return [path for _score, path in candidates[:limit]]

    def context_for_objective(
        self,
        objective: str,
        *,
        max_files: int = 14,
        max_chars_per_file: int = 4200,
        include_inventory: int = 80,
    ) -> str:
        """Produit un contexte compact : inventaire + symboles + extraits pertinents."""
        all_files = self.iter_files(suffixes={".py", ".md", ".txt", ".json", ".yaml", ".yml", ".ini", ".toml"})
        relevant = self.relevant_files(objective, limit=max_files)
        selected_paths = {info.path for info in relevant}

        lines: list[str] = [
            f"REPOSITORY FACTS: {len(all_files)} Python files. Paths below are real; do not invent existing symbols.",
            "Inventory (bounded):",
        ]
        lines.extend(f"- {path}" for path in all_files[:include_inventory])
        if len(all_files) > include_inventory:
            lines.append(f"- ... {len(all_files) - include_inventory} additional Python files omitted")

        lines.append("\nRelevant modules (AST + bounded excerpts):")
        for info in relevant:
            path = self.repo_root / info.path
            try:
                content = self._safe_content(info.path)
            except OSError:
                content = ""
            symbol_desc = ", ".join(f"{s.kind} {s.qualname or s.name}@{s.line}" for s in info.symbols[:30]) or "none"
            imports = ", ".join(info.imports[:16]) or "none"
            lines.append(f"\n### {info.path} ({info.lines} lines)")
            lines.append(f"Symbols: {symbol_desc}")
            lines.append(f"Imports: {imports}")
            if info.parse_error:
                lines.append(f"Parse note: {info.parse_error}")
            if info.language == "python":
                neighbors = self.dependency_neighbors(info.path, limit=8)
                if neighbors["imports"]:
                    lines.append("Internal imports: " + ", ".join(neighbors["imports"]))
                if neighbors["imported_by"]:
                    lines.append("Imported by: " + ", ".join(neighbors["imported_by"]))
            excerpt = self._focused_excerpt(content, objective, max_chars=max_chars_per_file)
            if excerpt:
                lines.append("Excerpt:\n" + excerpt)

        # Helpful explicit fact for the model: relevant tests that already exist.
        existing_tests = self.find_tests_for(selected_paths, limit=12)
        if existing_tests:
            lines.append("\nExisting related tests:")
            lines.extend(f"- {path}" for path in existing_tests)
        return "\n".join(lines)

    def context_for_paths(self, paths: Iterable[str], *, max_chars_per_file: int = 5000) -> str:
        """Contexte factuel pour des chemins explicitement recommandés par un agent.

        Les chemins inexistants sont signalés comme tels (utile pour proposer un
        nouveau module) mais aucun contenu hors dépôt/zone publique n'est lu.
        """
        chunks: list[str] = []
        seen: set[str] = set()
        allowed_suffixes = {".py", ".md", ".json", ".yaml", ".yml", ".ini", ".toml"}
        for raw in list(paths)[:16]:
            candidate = Path(raw)
            resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (self.repo_root / candidate).resolve(strict=False)
            rel = self._relative_safe(resolved)
            if rel is None or rel in seen or resolved.suffix.casefold() not in allowed_suffixes:
                continue
            seen.add(rel)
            if not resolved.is_file():
                chunks.append(f"### {rel}\nStatus: MISSING (peut être un nouveau fichier proposé)")
                continue
            info = self.inspect_file(rel)
            symbol_desc = ", ".join(f"{s.kind} {s.qualname or s.name}@{s.line}" for s in info.symbols[:30]) or "none"
            imports = ", ".join(info.imports[:20]) or "none"
            excerpt = self.read_file(rel, start_line=1, end_line=min(info.lines or 160, 160), max_chars=max_chars_per_file)
            chunks.append(
                f"### {rel}\nStatus: EXISTS\nSymbols: {symbol_desc}\nImports: {imports}\nExcerpt:\n{excerpt}"
            )
        return "\n\n".join(chunks)

    @classmethod
    def _focused_excerpt(cls, content: str, objective: str, *, max_chars: int) -> str:
        if not content:
            return ""
        lines = content.splitlines()
        tokens = cls._tokens(objective)
        hit_lines: list[int] = []
        for idx, line in enumerate(lines):
            lower = line.casefold()
            if any(token in lower for token in tokens):
                hit_lines.append(idx)
        if not hit_lines:
            return "\n".join(f"{i+1:>4}: {line}" for i, line in enumerate(lines[:35]))[:max_chars]

        chosen: set[int] = set()
        for idx in hit_lines[:12]:
            chosen.update(range(max(0, idx - 3), min(len(lines), idx + 5)))
        rendered = [f"{idx+1:>4}: {lines[idx]}" for idx in sorted(chosen)]
        return sanitize_text("\n".join(rendered))[:max_chars]
