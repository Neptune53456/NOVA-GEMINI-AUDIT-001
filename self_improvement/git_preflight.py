"""Préflight Git conservateur pour les artefacts générés de self_improvement."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess

from .real_bug_corpus import BUG_CORPUS_PATH, RealBugCorpus
from .red_team import validate_attack
from .scenario_loader import DATASET_PATH, load_scenarios
from .work_session import WorkSessionError, WorkSessionManager


AUTO_COMMIT_MESSAGE = "chore(self-improvement): persist generated corpus artifacts"
SAFE_CONFIRMED_PREFIX = "self_improvement/red_team_corpus/confirmed_failure/"
IGNORED_PREFIXES = (
    ".self_improvement_holdout/",
    ".self_improvement_discoveries/",
    ".self_improvement_worktrees/",
    ".self_improvement_sessions/",
    "self_improvement/red_team_corpus/backups/",
    "self_improvement/repair_history/",
    "self_improvement/reports/",
    ".pytest_cache/",
    ".hypothesis/",
    "htmlcov/",
)
IGNORED_NAMES = {".coverage", "Thumbs.db", ".DS_Store"}
IGNORED_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class GitChange:
    status: str
    path: str


@dataclass
class GitPreflightResult:
    success: bool
    clean: bool
    ready_for_cycle: bool = False
    committed: bool = False
    commit: str | None = None
    safe_generated: list[str] = field(default_factory=list)
    validated_session_changes: list[str] = field(default_factory=list)
    ignored_private: list[str] = field(default_factory=list)
    human_or_unknown: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    error: str | None = None
    session_id: str | None = None
    session_provenance_verified: bool = False
    session_auto_commit: bool = False
    session_reason: str = ""

    def to_dict(self):
        return asdict(self)


class GitPreflightError(RuntimeError):
    def __init__(self, message, *, result: GitPreflightResult):
        super().__init__(message)
        self.result = result


def parse_porcelain_z(output: str) -> list[GitChange]:
    """Parse `git status --porcelain=v1 -z` sans casser espaces ou Unicode."""
    fields = output.split("\0")
    changes = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise ValueError("Sortie git status --porcelain invalide.")
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            # Le chemin source suivant est informatif; toute opération de renommage
            # sera de toute façon classée HUMAN_OR_UNKNOWN.
            index += 1
        changes.append(GitChange(status, path.replace("\\", "/")))
    return changes


class GitPreflight:
    def __init__(
        self, root, *, runner=subprocess.run, dataset_path=DATASET_PATH,
        bug_corpus_path=BUG_CORPUS_PATH, commit_message=AUTO_COMMIT_MESSAGE,
        auto_commit_validated_session=False, session_id=None, session_manager=None,
    ):
        self.root = Path(root).resolve()
        self.runner = runner
        self.dataset_path = Path(dataset_path)
        self.bug_corpus_path = Path(bug_corpus_path)
        self.commit_message = commit_message
        self.auto_commit_validated_session = auto_commit_validated_session
        self.session_id = session_id
        self.session_manager = session_manager or WorkSessionManager(self.root, runner=runner)

    def _git(self, arguments, *, check=False):
        return self.runner(
            ["git", *arguments], cwd=self.root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=check,
        )

    def status(self) -> list[GitChange]:
        completed = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if completed.returncode:
            raise GitPreflightError(
                "Impossible d'inspecter le working tree Git.",
                result=GitPreflightResult(False, False, error=(completed.stderr or completed.stdout).strip()),
            )
        try:
            return parse_porcelain_z(completed.stdout or "")
        except ValueError as error:
            raise GitPreflightError(
                str(error), result=GitPreflightResult(False, False, error=str(error)),
            ) from error

    @staticmethod
    def _ignored(path: str) -> bool:
        normalized = path[2:] if path.startswith("./") else path
        parts = set(PurePosixPath(normalized).parts)
        return (
            normalized in IGNORED_NAMES
            or any(normalized.startswith(prefix) for prefix in IGNORED_PREFIXES)
            or bool(parts & IGNORED_PARTS)
            or normalized.endswith((".pyc", ".pyo", ".log", ".tmp"))
        )

    def _known_source_ids(self) -> set[str]:
        _version, scenarios = load_scenarios(self.dataset_path)
        corpus = RealBugCorpus(self.bug_corpus_path)
        bugs = [
            corpus.to_scenario(case)
            for case in corpus.load(statuses={"validated", "integrated", "resolved"})
        ]
        return {item.id for item in [*scenarios, *bugs]}

    def _verified_confirmed_failure(self, change: GitChange, source_ids: set[str]) -> bool:
        if change.status.strip() in {"D", "R", "C"} or "D" in change.status or "R" in change.status or "C" in change.status:
            return False
        path = change.path
        if not path.startswith(SAFE_CONFIRMED_PREFIX) or not path.endswith(".json"):
            return False
        target = (self.root / Path(path)).resolve()
        expected_root = (self.root / "self_improvement" / "red_team_corpus" / "confirmed_failure").resolve()
        if expected_root not in target.parents:
            return False
        try:
            wrapper = json.loads(target.read_text(encoding="utf-8"))
            attack = validate_attack(wrapper.get("attack"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
            return False
        if target.name != f"{attack['normalized_signature']}.json":
            return False
        if attack["base_scenario_id"] not in source_ids:
            return False
        migration = wrapper.get("migration")
        if migration is not None:
            if set(migration) != {"version", "status", "discovery_family", "exposure", "previous_version"}:
                return False
            if migration.get("status") not in {"family_reconstructed", "legacy_public"}:
                return False
            if migration.get("exposure") != "public" or not isinstance(migration.get("discovery_family"), str):
                return False
        return wrapper.get("version") in {"1.0", "1.1"}

    def classify(self, changes: list[GitChange], *, validated_manifest=None) -> GitPreflightResult:
        source_ids = self._known_source_ids()
        safe, validated, ignored, unknown = [], [], [], []
        validated_paths = set(validated_manifest.changed_files) if validated_manifest else set()
        for change in changes:
            if self._ignored(change.path):
                # Un artefact privé déjà présent dans l'index est un état dangereux :
                # ne pas le désindexer automatiquement, mais bloquer avec son chemin.
                if change.status[0] not in {" ", "?"}:
                    unknown.append(change.path)
                else:
                    ignored.append(change.path)
            elif change.path in validated_paths:
                validated.append(change.path)
            elif self._verified_confirmed_failure(change, source_ids):
                safe.append(change.path)
            else:
                unknown.append(change.path)
        return GitPreflightResult(
            success=not unknown, clean=not safe and not validated and not unknown,
            ready_for_cycle=not safe and not validated and not unknown,
            safe_generated=sorted(set(safe)),
            validated_session_changes=sorted(set(validated)),
            ignored_private=sorted(set(ignored)),
            human_or_unknown=sorted(set(unknown)),
            blocking=sorted(set([*safe, *validated, *unknown])),
            session_id=validated_manifest.session_id if validated_manifest else None,
            session_provenance_verified=bool(validated_manifest),
        )

    def _validated_manifest(self):
        if not self.auto_commit_validated_session:
            return None
        candidate = (
            self.session_manager.load(self.session_id)
            if self.session_id else self.session_manager.latest_validated()
        )
        if candidate is None:
            return None
        if candidate.status == "COMMITTED":
            return None
        return self.session_manager.verify(candidate.session_id)

    def run(self) -> GitPreflightResult:
        changes = self.status()
        try:
            manifest = self._validated_manifest()
        except WorkSessionError as error:
            result = self.classify(changes)
            result.error = f"Provenance de session refusée : {error}"
            result.session_reason = str(error)
            raise GitPreflightError(result.error, result=result) from error
        result = self.classify(changes, validated_manifest=manifest)
        if result.human_or_unknown:
            result.error = "Changements humains ou inconnus détectés."
            raise GitPreflightError(result.error, result=result)
        commit_paths = sorted(set([*result.safe_generated, *result.validated_session_changes]))
        if not commit_paths:
            result.success = True
            result.clean = True
            result.ready_for_cycle = True
            result.blocking = []
            return result
        add = self._git(["add", "--", *commit_paths])
        if add.returncode:
            result.success = False
            result.error = (add.stderr or add.stdout).strip() or "git add a échoué."
            raise GitPreflightError(result.error, result=result)
        message = self.commit_message
        if manifest:
            message = (
                f"feat(self-improvement): {manifest.summary}"
                if manifest.summary else
                "chore(self-improvement): persist validated Codex session"
            )
        commit = self._git(["commit", "-m", message, "--", *commit_paths])
        if commit.returncode:
            result.success = False
            result.error = (commit.stderr or commit.stdout).strip() or "git commit a échoué."
            raise GitPreflightError(result.error, result=result)
        head = self._git(["rev-parse", "HEAD"])
        result.committed = True
        result.commit = (head.stdout or "").strip() or None
        result.session_auto_commit = bool(manifest)
        if manifest and result.commit:
            self.session_manager.mark_committed(manifest.session_id, result.commit)
        remaining = self.classify(self.status())
        if remaining.safe_generated or remaining.validated_session_changes or remaining.human_or_unknown:
            result.success = False
            result.clean = False
            result.human_or_unknown = remaining.human_or_unknown
            result.blocking = remaining.blocking
            result.error = "Le working tree contient encore des changements versionnables requis."
            raise GitPreflightError(result.error, result=result)
        result.success = True
        result.clean = True
        result.ready_for_cycle = True
        result.blocking = []
        result.ignored_private = sorted(set([*result.ignored_private, *remaining.ignored_private]))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Ajoute et commit uniquement les artefacts vérifiés.")
    parser.add_argument(
        "--auto-commit-validated-session", action="store_true",
        help="Valide la provenance de la dernière session Codex puis commit ses seuls fichiers.",
    )
    parser.add_argument("--session-id", help="Session Codex précise à vérifier.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    preflight = GitPreflight(
        args.root, auto_commit_validated_session=args.auto_commit_validated_session,
        session_id=args.session_id,
    )
    try:
        result = preflight.run() if args.apply or args.auto_commit_validated_session else preflight.classify(preflight.status())
    except GitPreflightError as error:
        result = error.result
    print(f"[GitPreflight] safe_generated={len(result.safe_generated)}")
    print(f"[GitPreflight] validated_session_change={len(result.validated_session_changes)}")
    print(f"[GitPreflight] ignored_private={len(result.ignored_private)}")
    print(f"[GitPreflight] human_or_unknown={len(result.human_or_unknown)}")
    for path in result.human_or_unknown:
        print(f"[GitPreflight] BLOCKING {path}")
    if result.committed:
        print(f"[GitPreflight] commit={result.commit}")
    if result.session_id:
        print(f"[GitPreflight] session={result.session_id} provenance={'oui' if result.session_provenance_verified else 'non'}")
    return 2 if result.human_or_unknown or result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
