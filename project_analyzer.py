import os
import ast
import py_compile

from model_router import chat


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

IGNORED_DIRS = {
    ".venv",
    "__pycache__",
    "backups",
    ".temp_tests",
    ".git",
}


def get_python_files():
    python_files = []

    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in IGNORED_DIRS
        ]

        for filename in files:
            if filename.endswith(".py"):
                full_path = os.path.join(root, filename)

                relative_path = os.path.relpath(
                    full_path,
                    PROJECT_ROOT
                )

                python_files.append(relative_path)

    return sorted(python_files)


def check_syntax(path):
    try:
        py_compile.compile(
            path,
            doraise=True
        )

        return True, None

    except Exception as error:
        return False, str(error)


def analyze_python_structure(path):
    try:
        with open(
            path,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as file:
            content = file.read()

        tree = ast.parse(content)

    except Exception as error:
        return {
            "path": path,
            "error": str(error)
        }

    imports = []
    functions = []
    classes = []

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""

            imports.append(module)

        elif isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            functions.append(node.name)

        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)

    line_count = len(content.splitlines())

    syntax_ok, syntax_error = check_syntax(path)

    return {
        "path": path,
        "lines": line_count,
        "imports": sorted(set(imports)),
        "functions": sorted(set(functions)),
        "classes": sorted(set(classes)),
        "syntax_ok": syntax_ok,
        "syntax_error": syntax_error,
    }


def build_project_report():
    files = get_python_files()

    analyses = []

    for path in files:
        analyses.append(
            analyze_python_structure(path)
        )

    report_lines = []

    report_lines.append(
        f"Nombre de fichiers Python : {len(files)}"
    )

    report_lines.append("")

    for analysis in analyses:

        path = analysis["path"]

        report_lines.append(
            f"===== {path} ====="
        )

        if "error" in analysis:
            report_lines.append(
                f"ERREUR : {analysis['error']}"
            )

            report_lines.append("")
            continue

        report_lines.append(
            f"Lignes : {analysis['lines']}"
        )

        report_lines.append(
            "Syntaxe : "
            + (
                "OK"
                if analysis["syntax_ok"]
                else f"ERREUR : {analysis['syntax_error']}"
            )
        )

        if analysis["imports"]:
            report_lines.append(
                "Imports : "
                + ", ".join(analysis["imports"])
            )

        if analysis["functions"]:
            report_lines.append(
                "Fonctions : "
                + ", ".join(analysis["functions"])
            )

        if analysis["classes"]:
            report_lines.append(
                "Classes : "
                + ", ".join(analysis["classes"])
            )

        report_lines.append("")

    return "\n".join(report_lines)


def analyze_project():
    report = build_project_report()

    prompt = f"""
Tu es chargé d'analyser l'architecture d'un projet Python.

Voici un rapport généré automatiquement à partir du projet :

{report}

Analyse :

1. L'organisation générale du projet.
2. Les relations probables entre les fichiers.
3. Les éventuels problèmes de structure.
4. Les fichiers qui semblent devenir trop volumineux.
5. Les responsabilités qui devraient être séparées.
6. Les erreurs de syntaxe éventuelles.
7. Les risques de dépendances circulaires.
8. Les fichiers qu'il faudrait analyser en profondeur en priorité.
9. Les améliorations les plus importantes à faire ensuite.

Ne prétends pas avoir vu le contenu complet des fichiers.
Base-toi uniquement sur le rapport fourni.

Réponds en français, clairement et de façon structurée.
"""

    response = chat(
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        task_type="analysis",
        think=False
    )

    return report, response["message"]["content"]
