from pathlib import Path

import pytest

from nova_api.journal import EventJournal
from nova_api.transactions import TransactionError, TransactionStore
from nova_api.workspace import Workspace


def _store(tmp_path: Path) -> TransactionStore:
    journal = EventJournal(tmp_path / "events.sqlite3")
    return TransactionStore(Workspace(tmp_path), journal)


def test_pending_transaction_survives_restart_and_rolls_back_once(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    store = _store(tmp_path)
    tx = store.write("note.txt", "after")
    assert target.read_text(encoding="utf-8") == "after"

    restarted = _store(tmp_path)
    reopened = restarted.get(tx.transaction_id)
    assert reopened.status == "pending"
    assert reopened.before == b"before"

    rolled = restarted.rollback(tx.transaction_id)
    assert rolled.status == "rolled_back"
    assert target.read_text(encoding="utf-8") == "before"
    with pytest.raises(TransactionError, match="transaction_not_pending"):
        restarted.rollback(tx.transaction_id)


def test_created_file_transaction_survives_restart_and_rolls_back(tmp_path):
    store = _store(tmp_path)
    tx = store.write("created.txt", "new")
    assert (tmp_path / "created.txt").exists()

    restarted = _store(tmp_path)
    restarted.rollback(tx.transaction_id)
    assert not (tmp_path / "created.txt").exists()


def test_committed_transaction_survives_restart_without_preimage(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("old", encoding="utf-8")
    store = _store(tmp_path)
    tx = store.write("keep.txt", "new")
    store.commit(tx.transaction_id)

    restarted = _store(tmp_path)
    persisted = restarted.get(tx.transaction_id)
    assert persisted.status == "committed"
    assert target.read_text(encoding="utf-8") == "new"
    with pytest.raises(TransactionError, match="transaction_not_pending"):
        restarted.rollback(tx.transaction_id)


def test_transaction_conflict_is_detected_after_restart(tmp_path):
    store = _store(tmp_path)
    tx = store.write("conflict.txt", "nova")
    (tmp_path / "conflict.txt").write_text("human-change", encoding="utf-8")

    restarted = _store(tmp_path)
    with pytest.raises(TransactionError, match="transaction_conflict"):
        restarted.rollback(tx.transaction_id)
    assert (tmp_path / "conflict.txt").read_text(encoding="utf-8") == "human-change"
