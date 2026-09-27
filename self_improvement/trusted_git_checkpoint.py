"""Checkpoint Git optionnel du Trusted Control Plane V5.

Ce module n'est jamais requis pour accepter une amélioration. Il intervient *après*
que tests, benchmark et audit externe ont déjà accepté le candidat. Son seul rôle est
de créer un commit traçable contenant exactement les chemins audités.

La politique est conservatrice :
- aucun commit si le dossier n'est pas un repository Git ;
- refus si l'index contient déjà des changements stagés ;
- refus si le working tree contient des changements non liés au candidat ;
- seuls les chemins explicitement fournis et toujours éditables par l'agent peuvent
  être ajoutés ;
- un échec Git ne transforme jamais une bonne amélioration en mauvaise : le
  superviseur conserve le code accepté et expose simplement l'avertissement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
from typing import Callable, Iterable

from self_improvement.agent_path_policy import (
    is_agent_editable_path, is_model_private_path, normalize_relative_path,
)


@dataclass(frozen=True)
class GitCheckpointResult:
    success: bool
    reason: str
    commit: str | None = None
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["paths"] = list(self.paths)
        return payload


class TrustedGitCheckpointManager:
    """Crée un commit minimal après ACCEPT du superviseur de confiance."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        runner: Callable = subprocess.run,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.runner = runner
        self.timeout_seconds = max(2.0, min(float(timeout_seconds), 120.0))

    def _git(self, *args: str, check: bool = False):
        result = self.runner(
            ["git", *args], cwd=str(self.repo_root), capture_output=True, text=True,
            timeout=self.timeout_seconds,
        )
        if check and result.returncode:
            message = (result.stderr or result.stdout or "git command failed").strip()
            raise RuntimeError(message[:2000])
        return result

    def _is_repo(self) -> bool:
        try:
            result = self._git("rev-parse", "--is-inside-work-tree")
        except Exception:
            return False
        return result.returncode == 0 and result.stdout.strip().casefold() == "true"

    def _normalize_paths(self, changed_paths: Iterable[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in changed_paths:
            rel = normalize_relative_path(str(raw or "").strip())
            if not rel or rel in normalized:
                continue
            candidate = (self.repo_root / rel).resolve(strict=False)
            try:
                candidate.relative_to(self.repo_root)
            except ValueError as exc:
                raise ValueError(f"git_checkpoint_path_escape: {rel}") from exc
            if not is_agent_editable_path(rel):
                raise ValueError(f"git_checkpoint_path_not_agent_editable: {rel}")
            if candidate.is_dir():
                raise ValueError(f"git_checkpoint_directory_not_allowed: {rel}")
            normalized.append(rel)
        return tuple(normalized[:100])

    def checkpoint(self, changed_paths: Iterable[str], *, message: str) -> GitCheckpointResult:
        if not self._is_repo():
            return GitCheckpointResult(False, "git_repository_unavailable")
        try:
            paths = self._normalize_paths(changed_paths)
        except Exception as exc:
            return GitCheckpointResult(False, str(exc)[:2000])
        if not paths:
            return GitCheckpointResult(False, "git_checkpoint_no_paths")

        # L'index humain est sacré : ne jamais l'utiliser/écraser implicitement.
        staged = self._git("diff", "--cached", "--name-only", "--")
        staged_paths = [line.strip().replace("\\", "/") for line in staged.stdout.splitlines() if line.strip()]
        if staged.returncode or staged_paths:
            return GitCheckpointResult(False, "git_checkpoint_refused_prestaged_changes", paths=paths)

        status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        if status.returncode:
            return GitCheckpointResult(False, "git_checkpoint_status_failed", paths=paths)
        visible_changes: set[str] = set()
        for line in status.stdout.splitlines():
            if len(line) < 4:
                continue
            rel = line[3:].strip().replace("\\", "/")
            # rename syntax is deliberately unsupported by the self-improvement
            # workspace, but be conservative if Git ever reports one.
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            # Rapports/mémoires/recovery/caches du superviseur sont volontairement
            # hors commit ; ils ne doivent pas empêcher la traçabilité du code ACCEPT.
            if is_model_private_path(rel) or Path(rel).name.casefold() in {
                ".coverage", "coverage.xml", "thumbs.db", ".ds_store"
            }:
                continue
            visible_changes.add(rel)
        unexpected = sorted(visible_changes - set(paths))
        if unexpected:
            return GitCheckpointResult(
                False,
                "git_checkpoint_refused_unrelated_changes: " + ", ".join(unexpected[:12]),
                paths=paths,
            )

        clean_message = " ".join(str(message or "").split())[:240] or "chore(self-improvement): accepted autonomous improvement"
        try:
            added = self._git("add", "--", *paths)
            if added.returncode:
                raise RuntimeError((added.stderr or added.stdout or "git add failed").strip())
            staged_after = self._git("diff", "--cached", "--name-only", "--")
            actual_staged = {
                line.strip().replace("\\", "/") for line in staged_after.stdout.splitlines() if line.strip()
            }
            if not actual_staged:
                self._git("reset", "--", *paths)
                return GitCheckpointResult(False, "git_checkpoint_nothing_to_commit", paths=paths)
            if not actual_staged.issubset(set(paths)):
                self._git("reset", "--", *paths)
                return GitCheckpointResult(False, "git_checkpoint_staged_scope_mismatch", paths=paths)
            commit = self._git("commit", "-m", clean_message, "--", *paths)
            if commit.returncode:
                raise RuntimeError((commit.stderr or commit.stdout or "git commit failed").strip())
            head = self._git("rev-parse", "HEAD")
            sha = head.stdout.strip() if head.returncode == 0 else ""
            return GitCheckpointResult(True, "git_checkpoint_created", sha or None, paths)
        except Exception as exc:
            # Nettoyer uniquement nos chemins ; ne jamais toucher aux autres états Git.
            try:
                self._git("reset", "--", *paths)
            except Exception:
                pass
            return GitCheckpointResult(False, f"git_checkpoint_failed: {str(exc)[:1600]}", paths=paths)
