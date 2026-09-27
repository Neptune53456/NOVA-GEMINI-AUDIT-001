"""Point d'entrée unique de la suite automatisée du projet."""

import os
from pathlib import Path
import py_compile
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parent
IGNORED_DIRECTORIES = {
    ".git",
    ".temp_tests",
    ".venv",
    "__pycache__",
    "backups",
    "env",
    "htmlcov",
    "venv",
}


def compile_project():
    """Compile tous les fichiers Python sans écrire de bytecode dans le dépôt."""
    python_files = sorted(
        path
        for path in PROJECT_ROOT.rglob("*.py")
        if not any(part in IGNORED_DIRECTORIES for part in path.relative_to(PROJECT_ROOT).parts)
    )

    with tempfile.TemporaryDirectory(prefix="project_compile_") as temp_directory:
        output_directory = Path(temp_directory)
        for index, path in enumerate(python_files):
            py_compile.compile(
                str(path),
                cfile=str(output_directory / f"{index}.pyc"),
                doraise=True,
            )

    print(f"Compilation globale : {len(python_files)} fichiers Python valides.")


def main(arguments=None):
    previous_setting = os.environ.get("PYTHONDONTWRITEBYTECODE")
    previous_runtime_setting = sys.dont_write_bytecode
    try:
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.dont_write_bytecode = True

        compile_project()

        import pytest

        return pytest.main(list(sys.argv[1:] if arguments is None else arguments))
    finally:
        sys.dont_write_bytecode = previous_runtime_setting
        if previous_setting is None:
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        else:
            os.environ["PYTHONDONTWRITEBYTECODE"] = previous_setting


if __name__ == "__main__":
    raise SystemExit(main())
