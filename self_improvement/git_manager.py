"""Gestion Git sûre : les candidats vivent uniquement dans des worktrees temporaires."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import stat
import subprocess

from .git_preflight import GitPreflightResult


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    baseline_commit: str
    cycle: int | None = None


class GitWorktreeManager:
    def __init__(self, repo: Path | str, *, runner=subprocess.run, worktree_root=None):
        self.repo = Path(repo).resolve()
        self.runner = runner
        self.worktree_root = Path(worktree_root).resolve() if worktree_root else self._default_worktree_root()

    def _default_worktree_root(self) -> Path:
        """Place toujours les candidats près du dépôt principal, jamais dans un worktree lié."""
        marker = self.repo / ".git"
        if marker.is_file():
            content = marker.read_text(encoding="utf-8", errors="replace").strip()
            if content.casefold().startswith("gitdir:"):
                git_directory = Path(content.split(":", 1)[1].strip())
                if not git_directory.is_absolute():
                    git_directory = (self.repo / git_directory).resolve()
                if git_directory.parent.name == "worktrees" and git_directory.parent.parent.name == ".git":
                    return git_directory.parent.parent.parent / ".self_improvement_worktrees"
        return self.repo / ".self_improvement_worktrees"

    def _run(self, *arguments, cwd=None, check=True):
        result = self.runner(
            ["git", *arguments], cwd=cwd or self.repo, capture_output=True,
            text=True, timeout=60,
        )
        if check and result.returncode:
            raise GitError((result.stderr or result.stdout or "Commande Git échouée.").strip())
        return result

    def status(self) -> str:
        return self._run("status", "--porcelain").stdout

    def require_clean(self):
        status = self.status()
        if status.strip():
            raise GitError("Le working tree principal doit être propre avant un cycle réel.")

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def _safe_branch(branch: str):
        if not re.fullmatch(r"self-improve/cycle-[0-9]{3,6}(?:-[a-z0-9-]+)?", branch):
            raise GitError("Nom de branche temporaire invalide.")

    def _branch_exists(self, branch: str) -> bool:
        result = self._run("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        return result.returncode == 0

    def next_available_cycle(self, first_cycle=1) -> int:
        cycle = max(1, int(first_cycle))
        while cycle <= 999_999:
            branch = f"self-improve/cycle-{cycle:03d}"
            path = self.worktree_root / f"cycle-{cycle:03d}"
            if not path.exists() and not self._branch_exists(branch):
                return cycle
            cycle += 1
        raise GitError("Aucun numéro de cycle temporaire disponible.")

    def create(
        self, cycle: int, *, base_ref="HEAD",
        safety: GitPreflightResult | None = None,
    ) -> Worktree:
        if safety is None:
            self.require_clean()
        elif not isinstance(safety, GitPreflightResult) or not safety.ready_for_cycle or safety.blocking:
            raise GitError("La décision GitPreflight n'autorise pas la création du worktree.")
        cycle = self.next_available_cycle(cycle)
        branch = f"self-improve/cycle-{cycle:03d}"
        self._safe_branch(branch)
        path = (self.worktree_root / f"cycle-{cycle:03d}").resolve()
        if path.parent != self.worktree_root.resolve() or path.exists():
            raise GitError("Chemin de worktree temporaire invalide ou déjà présent.")
        baseline = self._run("rev-parse", base_ref).stdout.strip()
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self._run("worktree", "add", "-b", branch, str(path), baseline)
        return Worktree(path, branch, baseline, cycle)

    def changed_files(self, worktree: Worktree) -> list[str]:
        output = self._run("status", "--porcelain", cwd=worktree.path).stdout
        return [line[3:] for line in output.splitlines() if len(line) > 3]

    def commit(self, worktree: Worktree, message: str) -> str:
        self._run("add", "-A", cwd=worktree.path)
        staged = self._run("diff", "--cached", "--quiet", cwd=worktree.path, check=False)
        if staged.returncode == 0:
            raise GitError("Codex n'a produit aucun changement à conserver.")
        self._run("commit", "-m", message, cwd=worktree.path)
        return self._run("rev-parse", "HEAD", cwd=worktree.path).stdout.strip()

    @staticmethod
    def _remove_residual_directory(resolved: Path):
        def make_writable_and_retry(function, path, _error):
            Path(path).chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            function(path)

        try:
            shutil.rmtree(resolved, onexc=make_writable_and_retry)
        except OSError as error:
            raise GitError(
                f"Nettoyage incomplet du worktree {resolved} : {error}"
            ) from error

    def abandon(self, worktree: Worktree, *, delete_branch=True):
        resolved = worktree.path.resolve()
        if resolved.parent != self.worktree_root.resolve():
            raise GitError("Refus d'abandonner un chemin hors du répertoire de worktrees.")
        self._safe_branch(worktree.branch)
        removed = self._run("worktree", "remove", "--force", str(resolved), check=False)
        if removed.returncode and resolved.exists():
            # Cible déjà validée comme enfant direct de worktree_root. Ce cas
            # couvre un registre Git incomplet après arrêt brutal.
            self._remove_residual_directory(resolved)
            self._run("worktree", "prune", check=False)
        if delete_branch:
            self._run("branch", "-D", worktree.branch, check=False)

    def close_accepted(self, worktree: Worktree):
        """Retire le dossier de travail; le commit accepté reste sur sa branche."""
        resolved = worktree.path.resolve()
        if resolved.parent != self.worktree_root.resolve():
            raise GitError("Worktree accepté hors périmètre.")
        removed = self._run("worktree", "remove", "--force", str(resolved), check=False)
        if removed.returncode and resolved.exists():
            self._remove_residual_directory(resolved)
            self._run("worktree", "prune", check=False)
