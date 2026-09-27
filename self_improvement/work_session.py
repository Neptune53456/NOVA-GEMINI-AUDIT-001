"""Provenance vérifiable des modifications d'une session Codex approuvée."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid
from typing import Any

from .reporting import write_json


SESSIONS_DIR_NAME = ".self_improvement_sessions"
SESSION_STATUSES = frozenset({"ACTIVE", "VALIDATED", "REJECTED", "COMMITTED"})
PRIVATE_PREFIXES = (
    ".self_improvement_holdout/", ".self_improvement_discoveries/",
    ".self_improvement_worktrees/", ".self_improvement_sessions/",
    "self_improvement/red_team_corpus/backups/", "self_improvement/repair_history/",
    "self_improvement/reports/", "backups/",
)
PRIVATE_NAMES = frozenset({".env", ".coverage", "memory.db", "credentials.json", "secrets.json"})
PRIVATE_PARTS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules"})
SECRET_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?i:(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"][^'\"]{8,}['\"])",
)


class WorkSessionError(RuntimeError):
    pass


@dataclass
class WorkSessionManifest:
    session_id: str
    started_at: str
    base_commit: str
    allowed_paths: list[str]
    task_type: str
    source: str
    repo_root: str
    branch: str
    status: str
    summary: str = ""
    coverage_threshold: float = 84.0
    preexisting_changes: list[str] = field(default_factory=list)
    finished_at: str | None = None
    changed_files: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    file_hashes: dict[str, str | None] = field(default_factory=dict)
    tests_passed: bool | None = None
    compilation_passed: bool | None = None
    coverage: float | None = None
    diff_check_passed: bool | None = None
    task_success: bool | None = None
    codex_returncode: int | None = None
    provenance_verified: bool = False
    blocked_files: list[str] = field(default_factory=list)
    refusal_reason: str = ""
    commit: str | None = None
    manifest_digest: str = ""

    def public_summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "provenance_verified": self.provenance_verified,
            "validated_files": len(self.changed_files) if self.provenance_verified else 0,
            "blocked_files": len(self.blocked_files),
            "auto_commit": bool(self.commit), "commit": self.commit,
            "reason": self.refusal_reason,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").removeprefix("./").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise WorkSessionError(f"Chemin de session invalide : {value}")
    return path.as_posix()


def _digest(payload: dict[str, Any]) -> str:
    clean = dict(payload)
    clean.pop("manifest_digest", None)
    raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _private_path(path: str) -> bool:
    normalized = _normalize_path(path)
    parts = set(PurePosixPath(normalized).parts)
    name = PurePosixPath(normalized).name.casefold()
    return (
        name in {item.casefold() for item in PRIVATE_NAMES}
        or any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in PRIVATE_PREFIXES)
        or bool(parts & PRIVATE_PARTS)
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"))
        or any(marker in name for marker in ("secret", "credential", "private_key"))
    )


class WorkSessionManager:
    def __init__(self, root: Path | str, *, runner=subprocess.run, directory=None):
        self.root = Path(root).resolve()
        self.runner = runner
        self.directory = Path(directory or self.root / SESSIONS_DIR_NAME).resolve()
        if self.root != self.directory and self.root not in self.directory.parents:
            raise WorkSessionError("Le stockage des sessions doit rester dans le dépôt.")

    def _git(self, arguments):
        return self.runner(
            ["git", *arguments], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def _git_value(self, arguments, label: str) -> str:
        completed = self._git(arguments)
        value = (completed.stdout or "").strip()
        if completed.returncode or not value:
            raise WorkSessionError(f"Impossible de déterminer {label}.")
        return value

    def _status(self):
        from .git_preflight import parse_porcelain_z
        completed = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if completed.returncode:
            raise WorkSessionError("Impossible de capturer git status.")
        return parse_porcelain_z(completed.stdout or "")

    @staticmethod
    def _allowed(path: str, scopes: list[str]) -> bool:
        return any(path == scope or path.startswith(f"{scope}/") for scope in scopes)

    def _hash(self, path: str) -> str | None:
        target = (self.root / Path(path)).resolve()
        if self.root not in target.parents or not target.exists() or not target.is_file():
            return None
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def _contains_secret(self, path: str) -> bool:
        target = self.root / Path(path)
        if not target.exists():
            return False
        if not target.is_file() or target.stat().st_size > 2_000_000:
            return True
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Une session code/tests ne peut pas prouver l'absence de secret dans
            # un contenu opaque : il reste HUMAN_OR_UNKNOWN.
            return True
        return bool(SECRET_PATTERN.search(content))

    def _write(self, manifest: WorkSessionManifest) -> Path:
        if manifest.status not in SESSION_STATUSES:
            raise WorkSessionError("Statut de session invalide.")
        payload = asdict(manifest)
        payload["manifest_digest"] = _digest(payload)
        manifest.manifest_digest = payload["manifest_digest"]
        path = self.directory / f"{manifest.session_id}.json"
        write_json(path, payload)
        return path

    def load(self, session_id: str) -> WorkSessionManifest:
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise WorkSessionError("Identifiant de session invalide.")
        path = self.directory / f"{session_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkSessionError("Manifeste de session absent ou illisible.") from error
        if payload.get("manifest_digest") != _digest(payload):
            raise WorkSessionError("Intégrité du manifeste de session invalide.")
        try:
            manifest = WorkSessionManifest(**payload)
        except TypeError as error:
            raise WorkSessionError("Schéma du manifeste de session invalide.") from error
        if Path(manifest.repo_root).resolve() != self.root or manifest.source != "codex":
            raise WorkSessionError("Le manifeste ne correspond pas à ce dépôt ou à Codex.")
        return manifest

    def start(
        self, *, allowed_paths: list[str], task_type: str,
        summary: str = "", coverage_threshold: float = 84.0,
    ) -> WorkSessionManifest:
        scopes = sorted(set(_normalize_path(path) for path in allowed_paths))
        if not scopes or coverage_threshold < 0 or coverage_threshold > 100:
            raise WorkSessionError("Scope vide ou seuil de couverture invalide.")
        if any(_private_path(path) for path in scopes):
            raise WorkSessionError("Un chemin privé ne peut pas appartenir au scope Codex.")
        status = self._status()
        preexisting = sorted(change.path for change in status if not _private_path(change.path))
        if preexisting:
            raise WorkSessionError(
                "La session ne peut pas démarrer avec des modifications versionnables préexistantes : "
                + ", ".join(preexisting)
            )
        branch = self._git_value(["branch", "--show-current"], "la branche Git")
        manifest = WorkSessionManifest(
            session_id=uuid.uuid4().hex, started_at=_now(),
            base_commit=self._git_value(["rev-parse", "HEAD"], "le commit de base"),
            allowed_paths=scopes, task_type=task_type.strip() or "maintenance",
            source="codex", repo_root=str(self.root), branch=branch, status="ACTIVE",
            summary=re.sub(r"\s+", " ", summary).strip()[:72],
            coverage_threshold=float(coverage_threshold),
            preexisting_changes=preexisting,
        )
        self._write(manifest)
        return manifest

    def finalize(
        self, session_id: str, *, tests_passed: bool, compilation_passed: bool,
        coverage: float | None, diff_check_passed: bool, task_success: bool,
        codex_returncode: int,
    ) -> WorkSessionManifest:
        manifest = self.load(session_id)
        if manifest.status != "ACTIVE":
            return manifest
        changes = self._status()
        paths = sorted(set(change.path for change in changes if not _private_path(change.path)))
        private_changed = sorted(set(change.path for change in changes if _private_path(change.path)))
        outside = [path for path in paths if not self._allowed(path, manifest.allowed_paths)]
        secrets = [path for path in paths if self._contains_secret(path)]
        ambiguous_git_changes = [
            change.path for change in changes
            if change.path in paths and ("R" in change.status or "C" in change.status)
        ]
        head = self._git_value(["rev-parse", "HEAD"], "le commit courant")
        branch = self._git_value(["branch", "--show-current"], "la branche Git")
        reasons = []
        if head != manifest.base_commit or branch != manifest.branch:
            reasons.append("HEAD ou branche modifié pendant la session")
        if manifest.preexisting_changes:
            reasons.append("modifications humaines préexistantes")
        if outside:
            reasons.append("fichiers hors scope")
        if secrets:
            reasons.append("secret détecté")
        if ambiguous_git_changes:
            reasons.append("renommage ou copie Git non attribuable avec certitude")
        quality = (
            tests_passed and compilation_passed and diff_check_passed and task_success
            and codex_returncode == 0 and coverage is not None
            and float(coverage) >= manifest.coverage_threshold
        )
        if not quality:
            reasons.append("gates de validation insuffisants")
        if not paths:
            reasons.append("aucun changement versionnable")
        manifest.finished_at = _now()
        manifest.changed_files = paths
        manifest.added_files = sorted(change.path for change in changes if change.path in paths and change.status == "??")
        manifest.deleted_files = sorted(change.path for change in changes if change.path in paths and "D" in change.status)
        manifest.file_hashes = {path: self._hash(path) for path in paths}
        manifest.tests_passed = bool(tests_passed)
        manifest.compilation_passed = bool(compilation_passed)
        manifest.coverage = coverage
        manifest.diff_check_passed = bool(diff_check_passed)
        manifest.task_success = bool(task_success)
        manifest.codex_returncode = int(codex_returncode)
        manifest.blocked_files = sorted(set([*outside, *secrets, *ambiguous_git_changes]))
        manifest.provenance_verified = not reasons
        manifest.refusal_reason = "; ".join(reasons)
        manifest.status = "VALIDATED" if manifest.provenance_verified else "REJECTED"
        self._write(manifest)
        return manifest

    def verify(self, session_id: str) -> WorkSessionManifest:
        manifest = self.load(session_id)
        if manifest.status != "VALIDATED" or not manifest.provenance_verified:
            raise WorkSessionError(manifest.refusal_reason or "La session n'est pas validée.")
        if self._git_value(["rev-parse", "HEAD"], "le commit courant") != manifest.base_commit:
            raise WorkSessionError("Le commit courant ne correspond plus à la session.")
        if self._git_value(["branch", "--show-current"], "la branche Git") != manifest.branch:
            raise WorkSessionError("La branche courante ne correspond plus à la session.")
        live = sorted(change.path for change in self._status() if not _private_path(change.path))
        if live != manifest.changed_files:
            raise WorkSessionError("Le working tree a changé depuis la validation de session.")
        if any(_private_path(path) or not self._allowed(path, manifest.allowed_paths) for path in live):
            raise WorkSessionError("Le périmètre de session n'est plus valide.")
        if {path: self._hash(path) for path in live} != manifest.file_hashes:
            raise WorkSessionError("Le contenu d'un fichier a changé depuis la validation.")
        if any(self._contains_secret(path) for path in live):
            raise WorkSessionError("Un secret est présent dans les modifications validées.")
        return manifest

    def latest_validated(self) -> WorkSessionManifest | None:
        if not self.directory.exists():
            return None
        candidates = []
        for path in self.directory.glob("*.json"):
            try:
                manifest = self.load(path.stem)
            except WorkSessionError:
                continue
            if manifest.status == "VALIDATED":
                candidates.append(manifest)
        return max(candidates, key=lambda item: item.finished_at or "", default=None)

    def mark_committed(self, session_id: str, commit: str) -> WorkSessionManifest:
        manifest = self.load(session_id)
        manifest.status, manifest.commit = "COMMITTED", commit
        self._write(manifest)
        return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--allowed-path", action="append", required=True)
    start.add_argument("--task-type", default="maintenance")
    start.add_argument("--summary", default="")
    start.add_argument("--coverage-threshold", type=float, default=84.0)
    finish = subparsers.add_parser("finish")
    finish.add_argument("session_id")
    finish.add_argument("--tests-passed", action="store_true")
    finish.add_argument("--compilation-passed", action="store_true")
    finish.add_argument("--coverage", type=float)
    finish.add_argument("--diff-check-passed", action="store_true")
    finish.add_argument("--task-success", action="store_true")
    finish.add_argument("--codex-returncode", type=int, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("session_id")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    manager = WorkSessionManager(args.root)
    try:
        if args.command == "start":
            result = manager.start(
                allowed_paths=args.allowed_path, task_type=args.task_type,
                summary=args.summary, coverage_threshold=args.coverage_threshold,
            )
        elif args.command == "finish":
            result = manager.finalize(
                args.session_id, tests_passed=args.tests_passed,
                compilation_passed=args.compilation_passed, coverage=args.coverage,
                diff_check_passed=args.diff_check_passed, task_success=args.task_success,
                codex_returncode=args.codex_returncode,
            )
        else:
            result = manager.verify(args.session_id)
    except WorkSessionError as error:
        print(f"[GitSession] refus : {error}")
        return 2
    print(json.dumps(result.public_summary(), ensure_ascii=False))
    return 0 if result.status in {"ACTIVE", "VALIDATED", "COMMITTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
