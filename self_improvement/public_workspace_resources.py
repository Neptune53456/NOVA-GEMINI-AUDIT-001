"""Explicit non-code resources admitted to a cognitive engineering workspace.

The mixed evaluation dataset scenarios/v1.json is deliberately NOT an entry.
A separately approved public test fixture is required before admitting it.
No path supplied by a Planner or repository document can extend this manifest.
"""
import ast
from pathlib import Path
from typing import Iterable


PUBLIC_WORKSPACE_RESOURCES = (
    "pyproject.toml",
    ".coveragerc",
    "requirements.txt",
    "requirements-dev.txt",
)

# These modules load or coordinate the protected scenario corpus. Their test
# files can remain readable in a worker, but cannot be used for its baseline
# preflight because the corpus is deliberately not materialized there.
WORKER_PRIVATE_TEST_MODULE_PREFIXES = (
    "self_improvement.benchmark_runner",
    "self_improvement.scenario_loader",
    "self_improvement.scenario_lab",
    "self_improvement.real_bug_corpus",
)


def is_public_workspace_file(relative: str) -> bool:
    return relative in PUBLIC_WORKSPACE_RESOURCES or Path(relative).suffix.casefold() in {".py", ".md"}


def missing_public_resources(workspace: Path) -> list[str]:
    return [relative for relative in PUBLIC_WORKSPACE_RESOURCES
            if not (workspace / relative).is_file() or (workspace / relative).is_symlink()]


def is_worker_prevalidation_test(path: Path) -> bool:
    """Whether a readable test can run without protected evaluation resources."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        # Keep malformed or unreadable tests in the preflight: their failure is
        # actionable infrastructure evidence, not an evaluation-data exception.
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = (item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules = (node.module,) if node.module else ()
        else:
            continue
        if any(module and module.startswith(WORKER_PRIVATE_TEST_MODULE_PREFIXES) for module in modules):
            return False
    return True


def select_worker_safe_tests(workspace: Path, tests: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split test selectors by whether they can run in a sanitized worker."""
    included: list[str] = []
    excluded: list[str] = []
    for test in tests:
        test_path = workspace / test.split("::", 1)[0]
        target = included if is_worker_prevalidation_test(test_path) else excluded
        target.append(test)
    return included, excluded
