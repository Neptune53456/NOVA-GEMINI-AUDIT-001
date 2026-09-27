"""Analyse statique et LLM des relations entre plusieurs modules Python."""

import ast
import builtins
import importlib.util
from pathlib import Path

from model_router import chat


PROJECT_ROOT = Path(__file__).resolve().parent
IGNORED_DIRS = {".venv", "__pycache__", "backups", ".temp_tests", ".git"}


def _resolve_python_path(path):
    if not isinstance(path, (str, Path)):
        raise TypeError("Chaque chemin doit être une chaîne ou un objet Path.")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise ValueError("Tous les fichiers doivent appartenir au projet.") from error
    if candidate.suffix.lower() != ".py":
        raise ValueError(f"Seuls les fichiers .py sont acceptés : {candidate}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Fichier Python introuvable : {candidate}")
    return candidate


def _module_name(path):
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _project_python_files(include_ignored=False):
    files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        relative_parts = path.relative_to(PROJECT_ROOT).parts
        if not include_ignored and any(part in IGNORED_DIRS for part in relative_parts):
            continue
        if path.is_file():
            files.append(path.resolve())
    return sorted(files)


def _resolve_relative_module(current_module, module, level):
    package_parts = current_module.split(".")[:-1]
    remove_count = max(level - 1, 0)
    if remove_count > len(package_parts):
        return None
    base = package_parts[:len(package_parts) - remove_count]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _collect_module(path):
    content = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(content, filename=str(path))
    module = _module_name(path)
    imports = []
    imported_names = set()
    imported_aliases = {}
    functions = []
    classes = []
    exports = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
            exports.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
            exports.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    exports.add(target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".")[0]
                imported_names.add(bound_name)
                imported_aliases[bound_name] = alias.name
                exports.add(bound_name)
                imports.append({
                    "kind": "import",
                    "module": alias.name,
                    "symbol": None,
                    "level": 0,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            target_module = _resolve_relative_module(module, node.module, node.level)
            for alias in node.names:
                bound_name = alias.asname or alias.name
                imported_names.add(bound_name)
                imported_aliases[bound_name] = target_module or node.module or ""
                exports.add(bound_name)
                imports.append({
                    "kind": "from",
                    "module": target_module if node.level else (node.module or ""),
                    "symbol": alias.name,
                    "level": node.level,
                    "line": node.lineno,
                })

    calls = sorted({
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    })
    attribute_references = sorted({
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in imported_aliases
    })
    function_fingerprints = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            normalized = ast.dump(
                ast.Module(body=node.body, type_ignores=[]),
                include_attributes=False,
            )
            function_fingerprints[node.name] = normalized

    return {
        "path": path,
        "relative_path": str(path.relative_to(PROJECT_ROOT)),
        "module": module,
        "size": len(content),
        "lines": len(content.splitlines()),
        "tree": tree,
        "imports": imports,
        "imported_names": imported_names,
        "imported_aliases": imported_aliases,
        "functions": sorted(functions),
        "classes": sorted(classes),
        "calls": calls,
        "attribute_references": attribute_references,
        "exports": exports,
        "function_fingerprints": function_fingerprints,
        "has_dynamic_getattr": "__getattr__" in functions,
    }


def _module_is_external(module):
    if not module:
        return False
    root_name = module.split(".")[0]
    try:
        return importlib.util.find_spec(root_name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _strongly_connected_components(graph):
    index = 0
    indices = {}
    lowlinks = {}
    stack = []
    on_stack = set()
    components = []

    def visit(node):
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph.get(node, set()):
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in graph:
        if node not in indices:
            visit(node)
    return components


def _build_static_analysis(paths):
    selected_paths = [_resolve_python_path(path) for path in paths]
    if not selected_paths:
        raise ValueError("La liste de fichiers est vide.")
    selected_paths = list(dict.fromkeys(selected_paths))

    discovery_paths = set(_project_python_files())
    discovery_paths.update(selected_paths)
    for selected_path in selected_paths:
        discovery_paths.update(
            sibling.resolve() for sibling in selected_path.parent.glob("*.py")
        )

    project_modules = {}
    for project_path in sorted(discovery_paths):
        try:
            project_modules[_module_name(project_path)] = _collect_module(project_path)
        except (OSError, SyntaxError):
            continue

    selected = {}
    for path in selected_paths:
        data = project_modules.get(_module_name(path)) or _collect_module(path)
        selected[data["module"]] = data
        project_modules[data["module"]] = data

    problems = []
    graph = {module: set() for module in selected}
    relation_counts = {}

    for module, data in selected.items():
        local_dependencies = set()
        for imported in data["imports"]:
            target = imported["module"]
            target_data = project_modules.get(target)
            symbol = imported["symbol"]
            imported_submodule = (
                f"{target}.{symbol}"
                if target and symbol and symbol != "*"
                else None
            )
            submodule_data = project_modules.get(imported_submodule)

            if target_data is not None or submodule_data is not None:
                dependency = imported_submodule if submodule_data is not None else target
                local_dependencies.add(dependency)
                relation_counts[(module, dependency)] = (
                    relation_counts.get((module, dependency), 0) + 1
                )
                if symbol and symbol != "*":
                    symbol_exists = (
                        submodule_data is not None
                        or (target_data is not None and symbol in target_data["exports"])
                    )
                    has_dynamic_getattr = (
                        target_data is not None and target_data["has_dynamic_getattr"]
                    )
                    if not symbol_exists and not has_dynamic_getattr:
                        problems.append({
                            "level": "CONFIRME",
                            "category": "symbole importé absent",
                            "message": (
                                f"{data['relative_path']}:{imported['line']} importe "
                                f"{symbol} depuis {target}, mais ce nom n'y est pas défini."
                            ),
                        })
            elif imported["level"]:
                problems.append({
                    "level": "CONFIRME",
                    "category": "module local inexistant",
                    "message": (
                        f"{data['relative_path']}:{imported['line']} référence le "
                        f"module relatif introuvable {target or '(invalide)'}."
                    ),
                })
            elif target and not _module_is_external(target):
                problems.append({
                    "level": "PROBABLE",
                    "category": "module importé introuvable",
                    "message": (
                        f"{data['relative_path']}:{imported['line']} importe {target}, "
                        "absent du projet et introuvable dans l'environnement courant."
                    ),
                })

        data["local_dependencies"] = sorted(local_dependencies)
        graph[module] = {dependency for dependency in local_dependencies if dependency in selected}

        for reference in data["attribute_references"]:
            alias_name, attribute_name = reference.split(".", 1)
            target = data["imported_aliases"].get(alias_name)
            target_data = project_modules.get(target)
            if (
                target_data is not None
                and attribute_name not in target_data["exports"]
                and not target_data["has_dynamic_getattr"]
            ):
                problems.append({
                    "level": "CONFIRME",
                    "category": "attribut importé absent",
                    "message": (
                        f"{data['relative_path']} référence {reference}, mais "
                        f"{attribute_name} n'est pas défini dans {target}."
                    ),
                })

        global_known = (
            set(data["functions"])
            | set(data["classes"])
            | data["imported_names"]
            | set(dir(builtins))
        )
        for node in data["tree"].body:
            candidates = []
            if isinstance(node, ast.Expr):
                candidates = [node.value]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                candidates = [node.value] if node.value is not None else []
            for candidate in candidates:
                for child in ast.walk(candidate):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id not in global_known
                    ):
                        problems.append({
                            "level": "PROBABLE",
                            "category": "appel global non résolu",
                            "message": (
                                f"{data['relative_path']}:{child.lineno} appelle "
                                f"{child.func.id}, non défini ni importé statiquement."
                            ),
                        })

    for component in _strongly_connected_components(graph):
        if len(component) > 1:
            cycle = " ↔ ".join(sorted(component))
            problems.append({
                "level": "PROBABLE",
                "category": "import circulaire",
                "message": f"Cycle d'imports locaux détecté : {cycle}.",
            })

    fingerprints = {}
    for module, data in selected.items():
        for function_name, fingerprint in data["function_fingerprints"].items():
            fingerprints.setdefault(fingerprint, []).append(f"{module}.{function_name}")
    for occurrences in fingerprints.values():
        if len(occurrences) > 1:
            problems.append({
                "level": "INDICATEUR",
                "category": "fonction dupliquée",
                "message": "Implémentations identiques : " + ", ".join(sorted(occurrences)) + ".",
            })

    for (source, target), count in relation_counts.items():
        if count >= 5:
            problems.append({
                "level": "INDICATEUR",
                "category": "dépendance concentrée",
                "message": f"{source} importe {count} éléments ou fois depuis {target}.",
            })

    return selected, graph, problems


def _format_static_report(selected, graph, problems):
    lines = ["=== RAPPORT STATIQUE INTER-FICHIERS ===", ""]
    for module, data in sorted(selected.items()):
        import_labels = []
        for item in data["imports"]:
            label = item["module"]
            if item["symbol"]:
                label += f".{item['symbol']}"
            import_labels.append(label)
        lines.extend([
            f"FICHIER : {data['relative_path']}",
            f"Module : {module}",
            f"Taille : {data['size']} caractères, {data['lines']} lignes",
            "Imports : " + (", ".join(import_labels) or "aucun"),
            "Fonctions : " + (", ".join(data["functions"]) or "aucune"),
            "Classes : " + (", ".join(data["classes"]) or "aucune"),
            "Appels : " + (", ".join(data["calls"]) or "aucun"),
            "Attributs importés référencés : "
            + (", ".join(data["attribute_references"]) or "aucun"),
            "Dépendances locales : "
            + (", ".join(data["local_dependencies"]) or "aucune"),
            "Exports approximatifs : " + (", ".join(sorted(data["exports"])) or "aucun"),
            "",
        ])

    lines.extend(["RELATIONS ENTRE MODULES :"])
    for module in sorted(graph):
        dependencies = sorted(graph[module])
        lines.append(f"- {module} → {', '.join(dependencies) if dependencies else 'aucune'}")

    lines.extend(["", "DÉTECTIONS STATIQUES :"])
    if problems:
        for problem in problems:
            lines.append(
                f"- [{problem['level']}] {problem['category']} : {problem['message']}"
            )
    else:
        lines.append("- Aucun problème inter-fichiers détecté statiquement.")
    return "\n".join(lines)


def _response_content(response):
    try:
        return response["message"]["content"]
    except (KeyError, TypeError):
        message = getattr(response, "message", None)
        content = getattr(message, "content", None)
        if content is None:
            raise RuntimeError("Le modèle a renvoyé une réponse inattendue.")
        return content


def analyze_related_files(paths):
    """Analyse les relations d'une liste de fichiers Python du projet."""
    try:
        if isinstance(paths, (str, Path)):
            raise TypeError("paths doit être une liste de fichiers Python.")
        selected, graph, problems = _build_static_analysis(list(paths))
        static_report = _format_static_report(selected, graph, problems)

        prompt = f"""Analyse l'architecture inter-fichiers à partir du rapport AST
compact ci-dessous. Aucun code source complet ne t'est fourni.

Évalue le couplage, la répartition des responsabilités, les incohérences, les risques
de bugs d'intégration et les fichiers à examiner en profondeur. Respecte les niveaux
CONFIRME, PROBABLE et INDICATEUR du rapport statique. Distingue explicitement bug
concret, risque probable, problème d'architecture et simple recommandation. Ne
transforme jamais une incertitude statique en bug certain.

Retourne obligatoirement ces sections :
=== CARTE DES DEPENDANCES ===
=== PROBLEMES CONFIRMES ===
=== RISQUES PROBABLES ===
=== COUPLAGE / ARCHITECTURE ===
=== FICHIERS A ANALYSER EN PROFONDEUR ===
=== PRIORITES ===

{static_report}
"""
        response = chat(
            messages=[{"role": "user", "content": prompt}],
            task_type="analysis",
            think=False,
        )
        return _response_content(response).strip()
    except (FileNotFoundError, OSError, SyntaxError, TypeError, ValueError) as error:
        return f"Erreur Multi File Analyzer : {error}"
    except Exception as error:
        return f"Erreur Multi File Analyzer lors de l'analyse : {error}"


def analyze_project_relationships():
    """Analyse automatiquement tous les fichiers Python utiles du projet."""
    return analyze_related_files(_project_python_files())
