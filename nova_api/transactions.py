"""Bounded, reversible and restart-durable single-file workspace transactions."""

from __future__ import annotations

import difflib
import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from .journal import EventJournal
from .persistence import ensure_schema_version
from .workspace import Workspace

MAX_FILE_BYTES = 1_000_000
MAX_DIFF_CHARS = 64_000


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".nova-write-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class FileTransaction:
    transaction_id: str
    relative_path: str
    path: Path
    existed_before: bool
    before: bytes
    before_hash: str | None
    after_hash: str
    diff: str
    diff_truncated: bool
    status: Literal["pending", "committed", "rolled_back"] = "pending"
    created_at: str = ""
    completed_at: str | None = None
    rolled_back_at: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "path": self.relative_path,
            "status": self.status,
            "created_file": not self.existed_before,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "diff": self.diff,
            "diff_truncated": self.diff_truncated,
            # A rolled_back record only exists after exact read-back
            # verification in rollback()/reconcile_pending().
            "rollback_verified": self.status == "rolled_back",
        }


class TransactionError(RuntimeError):
    pass


class TransactionStore:
    """Persistent transaction metadata + bounded protected pre-images.

    The database is colocated with the EventJournal by default.  Pre-images are
    stored under a private sibling directory and never embedded into journal
    events, keeping the journal structural and bounded.
    """

    def __init__(self, workspace: Workspace, journal: EventJournal, *, path: str | Path | None = None) -> None:
        self.workspace = workspace
        self._journal = journal
        self.path = Path(path) if path is not None else journal.path.with_name("nova_transactions.sqlite3")
        self.backup_dir = self.path.with_name(f"{self.path.stem}_backups")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            ensure_schema_version(db, expected=1, component="transaction_store")
            db.execute("""
                CREATE TABLE IF NOT EXISTS file_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL,
                    existed_before INTEGER NOT NULL,
                    before_hash TEXT,
                    after_hash TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    diff_truncated INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    rolled_back_at TEXT,
                    backup_name TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS tx_status_idx ON file_transactions(status)")

    def _backup_path(self, transaction_id: str) -> Path:
        return self.backup_dir / f"{transaction_id}.before"

    def _persist(self, tx: FileTransaction, *, backup_name: str | None) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO file_transactions (
                    transaction_id, relative_path, existed_before, before_hash, after_hash,
                    diff, diff_truncated, status, created_at, completed_at, rolled_back_at, backup_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    status=excluded.status, completed_at=excluded.completed_at,
                    rolled_back_at=excluded.rolled_back_at, backup_name=excluded.backup_name""",
                (tx.transaction_id, tx.relative_path, int(tx.existed_before), tx.before_hash, tx.after_hash,
                 tx.diff, int(tx.diff_truncated), tx.status, tx.created_at, tx.completed_at,
                 tx.rolled_back_at, backup_name),
            )

    def _from_row(self, row: sqlite3.Row) -> FileTransaction:
        backup_name = row["backup_name"]
        before = b""
        if bool(row["existed_before"]) and str(row["status"]) == "pending":
            if not backup_name:
                raise TransactionError("transaction_backup_missing")
            backup = self.backup_dir / str(backup_name)
            if not backup.is_file():
                raise TransactionError("transaction_backup_missing")
            before = backup.read_bytes()
            if len(before) > MAX_FILE_BYTES or _hash(before) != row["before_hash"]:
                raise TransactionError("transaction_backup_invalid")
        relative = str(row["relative_path"])
        return FileTransaction(
            transaction_id=str(row["transaction_id"]),
            relative_path=relative,
            path=self.workspace.resolve(relative),
            existed_before=bool(row["existed_before"]),
            before=before,
            before_hash=row["before_hash"],
            after_hash=str(row["after_hash"]),
            diff=str(row["diff"]),
            diff_truncated=bool(row["diff_truncated"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            completed_at=row["completed_at"],
            rolled_back_at=row["rolled_back_at"],
        )

    def write(self, relative_path: str, text: str) -> FileTransaction:
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise TransactionError("file_too_large")
        path = self.workspace.resolve(relative_path)
        if path.exists() and not path.is_file():
            raise TransactionError("not_a_file")
        before = path.read_bytes() if path.exists() else b""
        if len(before) > MAX_FILE_BYTES:
            raise TransactionError("file_too_large")
        before_text = before.decode("utf-8", errors="replace")
        diff = "".join(difflib.unified_diff(
            before_text.splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=f"a/{self.workspace.relative_name(path)}",
            tofile=f"b/{self.workspace.relative_name(path)}",
        ))
        truncated = len(diff) > MAX_DIFF_CHARS
        transaction_id = uuid4().hex
        transaction = FileTransaction(
            transaction_id=transaction_id,
            relative_path=self.workspace.relative_name(path), path=path,
            existed_before=path.exists(), before=before,
            before_hash=_hash(before) if path.exists() else None,
            after_hash=_hash(encoded), diff=diff[:MAX_DIFF_CHARS], diff_truncated=truncated,
            created_at=_now(),
        )
        backup_name = None
        if transaction.existed_before:
            backup = self._backup_path(transaction_id)
            _atomic_write(backup, before)
            backup_name = backup.name
        # Persist rollback material before the actual mutation.  A crash after
        # this point still leaves enough information for deterministic recovery.
        self._persist(transaction, backup_name=backup_name)
        try:
            _atomic_write(path, encoded)
            if not path.is_file() or _hash(path.read_bytes()) != transaction.after_hash:
                raise TransactionError("verification_failed")
        except Exception:
            if transaction.existed_before:
                _atomic_write(path, before)
            elif path.exists():
                path.unlink()
            self._delete_record(transaction_id)
            raise
        self._journal.append("transaction.created", transaction_id=transaction.transaction_id,
                             status="pending")
        return transaction

    def _delete_record(self, transaction_id: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT backup_name FROM file_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
            db.execute("DELETE FROM file_transactions WHERE transaction_id=?", (transaction_id,))
        if row and row["backup_name"]:
            (self.backup_dir / str(row["backup_name"])).unlink(missing_ok=True)

    def get(self, transaction_id: str) -> FileTransaction:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM file_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
        if row is None:
            raise TransactionError("transaction_not_found")
        return self._from_row(row)

    def list_pending(self) -> list[FileTransaction]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM file_transactions WHERE status='pending' ORDER BY created_at").fetchall()
        return [self._from_row(row) for row in rows]

    def _cleanup_backup(self, transaction_id: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT backup_name FROM file_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
        if row and row["backup_name"]:
            (self.backup_dir / str(row["backup_name"])).unlink(missing_ok=True)


    def reconcile_pending(self, relative_path: str, expected_after_hash: str) -> tuple[str, str | None]:
        """Classify a crash-window write without replaying it.

        Returns (state, transaction_id), where state is one of completed,
        not_applied, conflict, none.  A clearly not-applied transaction is
        closed as rolled_back without touching the file.
        """
        normalized = self.workspace.relative_name(self.workspace.resolve(relative_path))
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM file_transactions WHERE relative_path=? AND status='pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (normalized,),
            ).fetchone()
        if row is None:
            return "none", None
        tx = self._from_row(row)
        if tx.after_hash != expected_after_hash:
            return "conflict", tx.transaction_id
        if tx.path.is_file():
            current_hash = _hash(tx.path.read_bytes())
            if current_hash == tx.after_hash:
                return "completed", tx.transaction_id
            if tx.existed_before and current_hash == tx.before_hash:
                tx.status = "rolled_back"
                tx.rolled_back_at = _now()
                self._persist(tx, backup_name=self._existing_backup_name(tx.transaction_id))
                self._cleanup_backup(tx.transaction_id)
                return "not_applied", tx.transaction_id
        elif not tx.existed_before:
            tx.status = "rolled_back"
            tx.rolled_back_at = _now()
            self._persist(tx, backup_name=self._existing_backup_name(tx.transaction_id))
            self._cleanup_backup(tx.transaction_id)
            return "not_applied", tx.transaction_id
        return "conflict", tx.transaction_id

    def commit(self, transaction_id: str) -> FileTransaction:
        transaction = self.get(transaction_id)
        with self._lock:
            if transaction.status != "pending":
                raise TransactionError("transaction_not_pending")
            if not transaction.path.is_file() or _hash(transaction.path.read_bytes()) != transaction.after_hash:
                raise TransactionError("transaction_conflict")
            transaction.status = "committed"
            transaction.completed_at = _now()
            self._persist(transaction, backup_name=self._existing_backup_name(transaction_id))
            self._cleanup_backup(transaction_id)
        self._journal.append("transaction.committed", transaction_id=transaction_id, status="committed")
        return transaction

    def _existing_backup_name(self, transaction_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT backup_name FROM file_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
        return str(row["backup_name"]) if row and row["backup_name"] else None

    def rollback(self, transaction_id: str) -> FileTransaction:
        transaction = self.get(transaction_id)
        with self._lock:
            if transaction.status != "pending":
                raise TransactionError("transaction_not_pending")
            if not transaction.path.is_file() or _hash(transaction.path.read_bytes()) != transaction.after_hash:
                raise TransactionError("transaction_conflict")
            if transaction.existed_before:
                _atomic_write(transaction.path, transaction.before)
                verified = _hash(transaction.path.read_bytes()) == transaction.before_hash
            else:
                transaction.path.unlink()
                verified = not transaction.path.exists()
            if not verified:
                raise TransactionError("verification_failed")
            transaction.status = "rolled_back"
            transaction.rolled_back_at = _now()
            self._persist(transaction, backup_name=self._existing_backup_name(transaction_id))
            self._cleanup_backup(transaction_id)
        self._journal.append("transaction.rolled_back", transaction_id=transaction_id, status="rolled_back")
        return transaction
