import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().with_name("memory.db")


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL
                )
            """)


def save_message(role, content):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                (role, content)
            )


def load_messages():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY id"
        ).fetchall()

    return [
        {
            "role": role,
            "content": content
        }
        for role, content in rows
    ]
