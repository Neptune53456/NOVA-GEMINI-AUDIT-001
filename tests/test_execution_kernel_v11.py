from pathlib import Path

from nova_api.capabilities import build_default_registry
from nova_api.execution_kernel import ExecutionKernel, stable_mutation_id
from nova_api.journal import EventJournal


def test_mutation_identity_is_stable_and_effect_sensitive() -> None:
    first = stable_mutation_id("owner", "step-1", "filesystem.write", {"path": "x", "content": "a"})
    same = stable_mutation_id("owner", "step-1", "filesystem.write", {"content": "a", "path": "x"})
    changed = stable_mutation_id("owner", "step-1", "filesystem.write", {"path": "x", "content": "b"})
    assert first == same
    assert changed != first


def test_kernel_reconciles_completed_write_without_replay(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    result = registry.execute("filesystem.write", {"path": "x.txt", "content": "hello"}, confirmed=True)
    assert result.status == "success"
    kernel = ExecutionKernel(registry)
    decision = kernel.reconcile("filesystem.write", {"path": "x.txt", "content": "hello"})
    assert decision.state == "completed"
    assert decision.transaction_id == result.result["transaction_id"]
