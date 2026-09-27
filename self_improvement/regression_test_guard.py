"""Preuve bornée qu'un nouveau test de régression échoue sur la baseline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from self_improvement.agent_path_policy import is_model_private_path
from self_improvement.process_safety import sanitized_child_environment


@dataclass(frozen=True)
class RegressionProbeResult:
    checked: bool
    baseline_failed: bool
    tests: list[str]
    reason: str
    output_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RegressionTestGuard:
    def __init__(self, repo_root: str | Path, *, timeout_seconds: float = 90.0) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.timeout_seconds = max(5.0, min(float(timeout_seconds), 300.0))

    @staticmethod
    def _is_test_path(rel: str) -> bool:
        path = Path(rel)
        return path.name.casefold().startswith("test_") or "tests" in {part.casefold() for part in path.parts[:-1]}

    def verify_new_tests_fail_on_baseline(self, candidates: Iterable[tuple[str, str, str]]) -> RegressionProbeResult:
        candidate_list = list(candidates)
        new_tests: list[tuple[str, str]] = []
        for raw_path, before, after in candidate_list:
            path = Path(raw_path).resolve(strict=False)
            try:
                rel = path.relative_to(self.repo_root).as_posix()
            except ValueError:
                continue
            if before == "" and after.strip() and self._is_test_path(rel):
                new_tests.append((rel, after))
        if not new_tests:
            return RegressionProbeResult(False, False, [], "no_new_regression_test")

        with tempfile.TemporaryDirectory(prefix="regression_baseline_") as temp:
            workspace = Path(temp) / "repo"
            self._copy_public_repo(workspace)
            tests: list[str] = []
            for rel, content in new_tests[:6]:
                destination = workspace / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8", newline="")
                tests.append(rel)
            command = [sys.executable, "-m", "pytest", *tests, "--no-cov", "-q", "-p", "no:cacheprovider"]
            try:
                proc = subprocess.run(
                    command, cwd=str(workspace), capture_output=True, text=True,
                    timeout=self.timeout_seconds, env=sanitized_child_environment(extra={"PYTHONPATH": ""}),
                )
            except subprocess.TimeoutExpired:
                return RegressionProbeResult(True, True, tests, "baseline_test_timeout_counts_as_failure")
            output = f"{proc.stdout}\n{proc.stderr}".strip()
            if proc.returncode == 0:
                return RegressionProbeResult(True, False, tests, "new_test_already_passes_on_baseline", output[-2500:])
            return RegressionProbeResult(True, True, tests, "new_test_fails_on_baseline", output[-2500:])

    def _copy_public_repo(self, workspace: Path) -> None:
        ignored = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".hypothesis", ".temp_tests",
                   ".self_improvement_worktrees", ".self_improvement_recovery", ".self_improvement_supervisor_recovery"}
        workspace.mkdir(parents=True, exist_ok=True)
        for source in self.repo_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            try:
                rel = source.relative_to(self.repo_root).as_posix()
            except ValueError:
                continue
            if any(part.casefold() in {item.casefold() for item in ignored} for part in Path(rel).parts):
                continue
            if is_model_private_path(rel):
                continue
            try:
                if source.stat().st_size > 5_000_000:
                    continue
                destination = workspace / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            except OSError:
                continue
