"""Small persistence invariants shared by Nova's SQLite state stores."""
from __future__ import annotations

import sqlite3


def ensure_schema_version(connection: sqlite3.Connection, *, expected: int, component: str) -> int:
    """Fail closed on a database written by a newer incompatible Nova.

    Existing pre-versioned databases use SQLite's default user_version=0 and are
    adopted as the current schema without rewriting their data.  Future schema
    migrations can increment ``expected`` and migrate explicitly before setting
    the new version.
    """
    row = connection.execute("PRAGMA user_version").fetchone()
    current = int(row[0]) if row else 0
    if current > expected:
        raise RuntimeError(f"unsupported_{component}_schema_version:{current}")
    if current == 0:
        connection.execute(f"PRAGMA user_version={int(expected)}")
        return expected
    return current
