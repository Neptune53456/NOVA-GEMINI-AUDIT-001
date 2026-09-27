"""Protections communes à toute la suite pytest."""

from pathlib import Path
import tempfile

import pytest

import memory
import smart_memory


# Local/CI command captures may be named ``test_output.txt`` or
# ``test_results.txt``.  Some environments enable the doctest plugin globally;
# never let binary/UTF-16 reports become test modules.
collect_ignore_glob = ["test_output.txt", "test_results.txt", "validation_output.txt"]


@pytest.fixture(autouse=True)
def isolate_user_database(monkeypatch):
    """Interdit à chaque test d'accéder à la véritable base utilisateur."""
    with tempfile.TemporaryDirectory(prefix="local_assistant_test_") as directory:
        test_database = Path(directory) / "memory.db"
        monkeypatch.setattr(memory, "DB_PATH", test_database)
        monkeypatch.setattr(smart_memory, "DB_PATH", test_database)
        # Les tests unitaires sont strictement hors reseau. Les tests V7 du
        # catalogue injectent leur propre transport fake.
        monkeypatch.setenv("OMNIROUTE_ENABLED", "0")
        yield test_database
