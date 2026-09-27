"""Local, bounded project metadata index and deterministic context targeting."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterable

from .workspace import Workspace

DEFAULT_INDEX_PATH = ".runtime/project_brain.sqlite3"
MAX_INDEXED_FILES = 10_000
MAX_FILE_BYTES = 1_000_000
MAX_CONTEXT_FILES = 6
MAX_CONTEXT_BYTES = 24_000
MAX_CONTEXT_FILE_BYTES = 6_000
MAX_DEPENDENCY_DEPTH = 2
EXCLUDED_DIRS = frozenset({".git", ".venv", "node_modules", ".runtime", "__pycache__", ".pytest_cache", "dist", "build", "coverage", ".mypy_cache"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".zip", ".tar", ".gz", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".p12"})
SECRET_NAMES = re.compile(r"(^|[._-])(secret|credential|password|token|private)([._-]|$)|^\.env", re.I)
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
TS_IMPORT = re.compile(r"(?:import(?:[\s\S]*?from\s*)?|export\s+[^;]*?from\s*)[\"']([^\"']+)[\"']")
TS_SYMBOL = re.compile(r"\b(?:export\s+)?(?:class|function|interface|type|const)\s+([A-Za-z_$][\w$]*)")


@dataclass(frozen=True)
class ContextTarget:
    relevant_files: list[str]
    relevant_symbols: list[str]
    reasons: list[str]
    estimated_context_size: int
    context: str = ""

    def public(self) -> dict[str, object]:
        return {"relevant_files": self.relevant_files, "relevant_symbols": self.relevant_symbols,
                "reasons": self.reasons, "estimated_context_size": self.estimated_context_size}


class ProjectBrain:
    """A read-only index; source text is read only when a selected context is rendered."""
    def __init__(self, workspace: Workspace, *, path: str | Path | None = None) -> None:
        self.workspace = workspace
        self.path = Path(path) if path else workspace.root / DEFAULT_INDEX_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, kind TEXT NOT NULL, size INTEGER NOT NULL,
                  sha256 TEXT NOT NULL, modified_ns INTEGER NOT NULL, git_status TEXT, indexed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS symbols (path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                  line INTEGER NOT NULL, PRIMARY KEY(path, name, kind, line));
                CREATE TABLE IF NOT EXISTS imports (path TEXT NOT NULL, target TEXT NOT NULL,
                  PRIMARY KEY(path, target));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @staticmethod
    def _included(relative: Path, size: int) -> bool:
        parts = relative.parts
        return (not any(part in EXCLUDED_DIRS for part in parts) and not SECRET_NAMES.search(relative.name)
                and relative.suffix.lower() not in EXCLUDED_SUFFIXES and size <= MAX_FILE_BYTES)

    def refresh(self) -> dict[str, int | str | float]:
        """Incrementally update metadata, never retaining source content."""
        started = perf_counter(); current: dict[str, os.stat_result] = {}
        for base, dirs, names in os.walk(self.workspace.root):
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
            for name in sorted(names):
                candidate = Path(base) / name
                try:
                    relative, stat = candidate.relative_to(self.workspace.root), candidate.stat()
                except (OSError, ValueError):
                    continue
                if self._included(relative, stat.st_size):
                    current[relative.as_posix()] = stat
                    if len(current) >= MAX_INDEXED_FILES: break
            if len(current) >= MAX_INDEXED_FILES: break
        updated = removed = 0
        git_statuses = self._git_statuses()
        with self._connect() as db:
            existing = {row["path"]: row for row in db.execute("SELECT path, size, modified_ns FROM files")}
            db.executemany("UPDATE files SET git_status = ? WHERE path = ?", ((git_statuses.get(path), path) for path in current))
            for path, stat in current.items():
                old = existing.get(path)
                if old and old["size"] == stat.st_size and old["modified_ns"] == stat.st_mtime_ns: continue
                self._upsert_file(db, path, stat, git_statuses.get(path)); updated += 1
            stale = set(existing) - set(current)
            for path in stale:
                db.execute("DELETE FROM files WHERE path = ?", (path,)); db.execute("DELETE FROM symbols WHERE path = ?", (path,)); db.execute("DELETE FROM imports WHERE path = ?", (path,)); removed += 1
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('updated_at', ?)", (now,))
            db.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('last_refresh_ms', ?)", (str(int((perf_counter()-started)*1000)),))
        return {"updated": updated, "removed": removed, "duration_ms": int((perf_counter()-started)*1000), "updated_at": now}

    def _git_statuses(self) -> dict[str, str]:
        try:
            result = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=self.workspace.root,
                capture_output=True, timeout=2, check=False, shell=False)
            if result.returncode != 0: return {}
            return {entry[3:].replace("\\", "/"): entry[:2] for entry in result.stdout.decode("utf-8", "replace").split("\0") if len(entry) >= 4}
        except (OSError, subprocess.SubprocessError): return {}

    def _upsert_file(self, db: sqlite3.Connection, path: str, stat: os.stat_result, git_status: str | None) -> None:
        source = self.workspace.resolve(path).read_bytes()
        digest = hashlib.sha256(source).hexdigest(); kind = Path(path).suffix.lower().lstrip(".") or "unknown"
        db.execute("INSERT OR REPLACE INTO files(path, kind, size, sha256, modified_ns, git_status, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (path, kind, len(source), digest, stat.st_mtime_ns, git_status, datetime.now(timezone.utc).isoformat()))
        db.execute("DELETE FROM symbols WHERE path = ?", (path,)); db.execute("DELETE FROM imports WHERE path = ?", (path,))
        try: text = source.decode("utf-8")
        except UnicodeDecodeError: return
        symbols, imports = self._extract(path, text)
        db.executemany("INSERT OR IGNORE INTO symbols(path, name, kind, line) VALUES (?, ?, ?, ?)", ((path, *item) for item in symbols))
        db.executemany("INSERT OR IGNORE INTO imports(path, target) VALUES (?, ?)", ((path, item) for item in imports))

    @staticmethod
    def _extract(path: str, text: str) -> tuple[list[tuple[str, str, int]], list[str]]:
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            try: tree = ast.parse(text)
            except SyntaxError: return [], []
            symbols = [(node.name, "class" if isinstance(node, ast.ClassDef) else "function", node.lineno)
                       for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            imports += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
            return symbols, imports
        if suffix in {".ts", ".tsx", ".js", ".jsx"}:
            return [(match.group(1), "symbol", text.count("\n", 0, match.start()) + 1) for match in TS_SYMBOL.finditer(text)], TS_IMPORT.findall(text)
        return [], []

    def status(self) -> dict[str, object]:
        with self._connect() as db:
            files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]; symbols = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            row = db.execute("SELECT value FROM metadata WHERE key = 'updated_at'").fetchone()
            size = db.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0]
        return {"status": "ready" if row else "unavailable", "file_count": files, "symbol_count": symbols,
                "last_updated": row[0] if row else None, "estimated_size": size}

    def should_target(self, question: str) -> bool:
        words = {word.lower() for word in TOKEN.findall(question)}
        if not words: return False
        with self._connect() as db:
            names = {row[0].lower() for row in db.execute("SELECT name FROM symbols")}
        return bool(words & names) or bool(words & {"code", "fichier", "file", "import", "rollback", "transaction", "api", "test", "fonction", "class", "module", "dependency", "dépendance"})

    def target(self, question: str) -> ContextTarget:
        started = perf_counter(); words = {word.lower() for word in TOKEN.findall(question)}
        scores: dict[str, int] = {}; reasons: dict[str, list[str]] = {}; symbols: dict[str, list[str]] = {}
        with self._connect() as db:
            files = list(db.execute("SELECT path, size FROM files")); symbol_rows = list(db.execute("SELECT path, name FROM symbols")); imports = list(db.execute("SELECT path, target FROM imports"))
        for row in files:
            stem_words = {word.lower() for word in TOKEN.findall(Path(row["path"]).stem)}; matched = words & stem_words
            if matched: scores[row["path"]] = scores.get(row["path"], 0) + 8 * len(matched); reasons.setdefault(row["path"], []).append("nom de fichier correspondant")
        for row in symbol_rows:
            if row["name"].lower() in words:
                scores[row["path"]] = scores.get(row["path"], 0) + 12; reasons.setdefault(row["path"], []).append("symbole correspondant"); symbols.setdefault(row["path"], []).append(row["name"])
        for row in files:
            if "test" in Path(row["path"]).parts or Path(row["path"]).name.startswith("test_"):
                if any(word in row["path"].lower() for word in words): scores[row["path"]] = scores.get(row["path"], 0) + 3; reasons.setdefault(row["path"], []).append("test associé")
        selected = [path for path, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:MAX_CONTEXT_FILES]]
        # Add one direct importer of a selected module, bounded and deterministic.
        for row in sorted(imports, key=lambda item: (item["path"], item["target"])):
            if len(selected) >= MAX_CONTEXT_FILES: break
            imported_name = row["target"].strip("./").replace("/", ".").split(".")[-1]
            if any(Path(path).stem == imported_name for path in selected) and row["path"] not in selected:
                selected.append(row["path"]); reasons.setdefault(row["path"], []).append("dépendance directe")
        context, total = self._render(selected)
        target = ContextTarget(selected, sorted({name for values in symbols.values() for name in values}),
                               [f"{path}: {', '.join(reasons[path])}" for path in selected], total, context)
        _ = started  # Timing is intentionally available through refresh/status, not exposed in model context.
        return target

    def _render(self, paths: Iterable[str]) -> tuple[str, int]:
        chunks: list[str] = []; total = 0
        for path in paths:
            if total >= MAX_CONTEXT_BYTES: break
            try: raw = self.workspace.resolve(path).read_bytes()[:min(MAX_CONTEXT_FILE_BYTES, MAX_CONTEXT_BYTES-total)]
            except OSError: continue
            try: text = raw.decode("utf-8")
            except UnicodeDecodeError: continue
            chunk = f"\n--- {path} ---\n{text}"; encoded = len(chunk.encode("utf-8"))
            chunks.append(chunk); total += encoded
        return "".join(chunks), total
