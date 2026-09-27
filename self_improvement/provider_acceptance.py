"""Benchmark public generique pour l'ajout d'un provider, sans API reelle."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ProviderAcceptanceCase:
    case_id: str
    provider_name: str
    module_path: str
    test_path: str
    objective: str


@dataclass(frozen=True)
class ProviderAcceptanceResult:
    case_id: str
    decision: str
    checks: dict[str, bool]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generic_provider_cases() -> list[ProviderAcceptanceCase]:
    return [
        ProviderAcceptanceCase("provider_example", "ExampleProvider", "providers/example_provider.py", "tests/test_example_provider.py", "Ajoute le support ExampleProvider V1"),
        ProviderAcceptanceCase("provider_alpha", "FakeLLMAlpha", "providers/fake_alpha.py", "tests/test_fake_alpha.py", "Integre FakeLLMAlpha avec disponibilite et erreurs structurees"),
        ProviderAcceptanceCase("provider_beta", "FakeLLMBeta", "providers/fake_beta.py", "tests/test_fake_beta.py", "Ajoute FakeLLMBeta via l'abstraction provider existante"),
    ]


def create_provider_fixture(root: str | Path, case: ProviderAcceptanceCase) -> None:
    """Cree un depot synthetique public; aucune fixture validation/holdout."""
    base = Path(root)
    (base / "providers").mkdir(parents=True, exist_ok=True)
    (base / "tests").mkdir(parents=True, exist_ok=True)
    (base / "providers" / "base.py").write_text(
        "class ProviderError(RuntimeError):\n    pass\n\nclass BaseProvider:\n    def available(self):\n        raise NotImplementedError\n    def call(self, messages):\n        raise NotImplementedError\n",
        encoding="utf-8",
    )
    (base / "providers" / "__init__.py").write_text("from .base import BaseProvider, ProviderError\n", encoding="utf-8")
    (base / "README.md").write_text(f"Objective: {case.objective}\n", encoding="utf-8")


def evaluate_provider_case(root: str | Path, case: ProviderAcceptanceCase, developer_result: Any) -> ProviderAcceptanceResult:
    base = Path(root)
    source = base / case.module_path
    test = base / case.test_path
    source_text = source.read_text(encoding="utf-8", errors="replace") if source.is_file() else ""
    test_text = test.read_text(encoding="utf-8", errors="replace") if test.is_file() else ""
    modified = set(getattr(developer_result, "files_modified", []) or getattr(developer_result, "files_changed", []) or [])
    normalized_modified = {Path(item).as_posix() for item in modified}
    checks = {
        "source_created": source.is_file(),
        "test_created": test.is_file(),
        "provider_convention": "BaseProvider" in source_text,
        "availability": "available" in source_text,
        "call_implemented": "def call" in source_text,
        "errors_handled": "ProviderError" in source_text or "raise" in source_text,
        "tests_meaningful": case.provider_name in test_text and "assert" in test_text,
        "real_diff": bool(normalized_modified) and any(path.endswith(Path(case.module_path).as_posix()) for path in normalized_modified),
        "tests_passed": bool(getattr(developer_result, "tests_passed", False)),
    }
    accepted = all(checks.values())
    failed = [name for name, ok in checks.items() if not ok]
    return ProviderAcceptanceResult(case.case_id, "ACCEPT" if accepted else "REJECT", checks, "all_checks_passed" if accepted else "failed:" + ",".join(failed))


class GenericProviderAcceptanceBenchmark:
    def __init__(self, cases: list[ProviderAcceptanceCase] | None = None) -> None:
        self.cases = list(cases or generic_provider_cases())

    def run(self, workspace_factory: Callable[[ProviderAcceptanceCase], tuple[str | Path, Any]]) -> list[ProviderAcceptanceResult]:
        results = []
        for case in self.cases:
            root, developer_result = workspace_factory(case)
            results.append(evaluate_provider_case(root, case, developer_result))
        return results
