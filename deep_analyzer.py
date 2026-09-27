"""Analyse approfondie et progressive d'un fichier Python."""

import ast

from pathlib import Path

from model_router import chat


PROJECT_ROOT = Path(__file__).resolve().parent
CHUNK_SIZE = 5_000


class _StaticMetricsVisitor(ast.NodeVisitor):
    """Collecte les métriques qui nécessitent de suivre l'imbrication."""

    NESTING_NODES = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Match,
    )

    def __init__(self):
        self.depth = 0
        self.max_depth = 0
        self.function_stack = []
        self.functions = []
        self.heavy_while_loops = []

    def generic_visit(self, node):
        if isinstance(node, self.NESTING_NODES):
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)

            if isinstance(node, ast.While):
                start = node.lineno
                end = getattr(node, "end_lineno", start)
                statement_count = sum(
                    isinstance(child, ast.stmt) for child in ast.walk(node)
                )
                if end - start + 1 > 50 or statement_count >= 15:
                    self.heavy_while_loops.append(
                        {
                            "start_line": start,
                            "end_line": end,
                            "lines": end - start + 1,
                            "statements": statement_count,
                        }
                    )

            super().generic_visit(node)
            self.depth -= 1
            return

        super().generic_visit(node)

    def _visit_function(self, node):
        self.function_stack.append(node.name)
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        self.functions.append(
            {
                "name": ".".join(self.function_stack),
                "start_line": start,
                "end_line": end,
                "lines": end - start + 1,
            }
        )
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)


def _analyze_static(content):
    """Analyse globalement le code avec AST, indépendamment des chunks."""
    tree = ast.parse(content)
    all_nodes = list(ast.walk(tree))
    visitor = _StaticMetricsVisitor()
    visitor.visit(tree)

    lines = content.splitlines()
    functions = sorted(
        visitor.functions,
        key=lambda function: function["lines"],
        reverse=True,
    )
    if_count = sum(isinstance(node, ast.If) for node in all_nodes)
    match_branches = sum(
        len(node.cases) for node in all_nodes if isinstance(node, ast.Match)
    )
    conditional_branches = (
        if_count
        + sum(isinstance(node, ast.IfExp) for node in all_nodes)
        + match_branches
    )

    ignored_global_nodes = (ast.Import, ast.ImportFrom, ast.FunctionDef,
                            ast.AsyncFunctionDef, ast.ClassDef, ast.Pass)
    global_logic = []
    for node in tree.body:
        is_docstring = (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        if not isinstance(node, ignored_global_nodes) and not is_docstring:
            global_logic.append(node)

    global_logic_lines = sum(
        getattr(node, "end_lineno", node.lineno) - node.lineno + 1
        for node in global_logic
    )
    signals = []
    if len(lines) > 500:
        signals.append("fichier volumineux (> 500 lignes)")
    for function in functions:
        if function["lines"] > 100:
            signals.append(
                f"fonction très longue : {function['name']} "
                f"({function['lines']} lignes)"
            )
        elif function["lines"] > 50:
            signals.append(
                f"fonction longue : {function['name']} ({function['lines']} lignes)"
            )
    if conditional_branches >= 15:
        signals.append(
            f"routage conditionnel important ({conditional_branches} branches)"
        )
    if len(global_logic) >= 5 or global_logic_lines >= 30:
        signals.append(
            "responsabilités concentrées dans le module "
            f"({len(global_logic)} blocs globaux, {global_logic_lines} lignes)"
        )
    for loop in visitor.heavy_while_loops:
        signals.append(
            "boucle principale potentiellement trop chargée : "
            f"lignes {loop['start_line']}-{loop['end_line']} "
            f"({loop['lines']} lignes, {loop['statements']} instructions AST)"
        )

    return {
        "total_lines": len(lines),
        "non_empty_lines": sum(bool(line.strip()) for line in lines),
        "character_count": len(content),
        "function_count": sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in all_nodes
        ),
        "class_count": sum(isinstance(node, ast.ClassDef) for node in all_nodes),
        "import_count": sum(
            isinstance(node, (ast.Import, ast.ImportFrom)) for node in all_nodes
        ),
        "if_count": if_count,
        "for_count": sum(
            isinstance(node, (ast.For, ast.AsyncFor)) for node in all_nodes
        ),
        "while_count": sum(isinstance(node, ast.While) for node in all_nodes),
        "try_count": sum(isinstance(node, ast.Try) for node in all_nodes),
        "continue_count": sum(isinstance(node, ast.Continue) for node in all_nodes),
        "break_count": sum(isinstance(node, ast.Break) for node in all_nodes),
        "conditional_branches": conditional_branches,
        "longest_functions": functions[:10],
        "functions_over_50_lines": [
            function for function in functions if function["lines"] > 50
        ],
        "max_nesting": visitor.max_depth,
        "global_logic_present": bool(global_logic),
        "global_logic_blocks": len(global_logic),
        "global_logic_lines": global_logic_lines,
        "signals": signals,
    }


def _format_static_report(metrics):
    """Transforme les métriques AST en rapport destiné à la synthèse."""
    report = [
        "=== ANALYSE STATIQUE GLOBALE ===",
        "",
        f"Lignes : {metrics['total_lines']}",
        f"Lignes non vides : {metrics['non_empty_lines']}",
        f"Taille : {metrics['character_count']} caractères",
        f"Fonctions : {metrics['function_count']}",
        f"Classes : {metrics['class_count']}",
        f"Imports : {metrics['import_count']}",
        f"If/elif : {metrics['if_count']}",
        f"For : {metrics['for_count']}",
        f"While : {metrics['while_count']}",
        f"Try : {metrics['try_count']}",
        f"Continue : {metrics['continue_count']}",
        f"Break : {metrics['break_count']}",
        f"Branches conditionnelles approximatives : {metrics['conditional_branches']}",
        f"Imbrication maximale approximative : {metrics['max_nesting']}",
        "Logique au niveau global : "
        + (
            f"oui ({metrics['global_logic_blocks']} blocs, "
            f"{metrics['global_logic_lines']} lignes)"
            if metrics["global_logic_present"]
            else "non"
        ),
        "",
        "Fonctions les plus longues :",
    ]
    if metrics["longest_functions"]:
        report.extend(
            f"- {function['name']} : lignes {function['start_line']}-"
            f"{function['end_line']}, {function['lines']} lignes"
            for function in metrics["longest_functions"]
        )
    else:
        report.append("- Aucune fonction")

    report.extend(["", "Fonctions de plus de 50 lignes :"])
    if metrics["functions_over_50_lines"]:
        report.extend(
            f"- {function['name']} : {function['lines']} lignes"
            for function in metrics["functions_over_50_lines"]
        )
    else:
        report.append("- Aucune")

    report.extend(["", "Signaux architecturaux (indicateurs, pas bugs) :"])
    if metrics["signals"]:
        report.extend(f"- {signal}" for signal in metrics["signals"])
    else:
        report.append("- Aucun signal objectif selon les seuils configurés")

    return "\n".join(report)


def _resolve_python_path(path):
    """Retourne un chemin Python valide, contenu dans le projet."""
    if not isinstance(path, (str, Path)):
        raise TypeError("Le chemin doit être une chaîne ou un objet Path.")

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate = candidate.resolve()

    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise ValueError("Le fichier doit se trouver dans le dossier du projet.") from error

    if candidate.suffix.lower() != ".py":
        raise ValueError("Seuls les fichiers .py peuvent être analysés.")
    if not candidate.exists():
        raise FileNotFoundError(f"Fichier introuvable : {candidate}")
    if not candidate.is_file():
        raise ValueError(f"Le chemin ne désigne pas un fichier : {candidate}")

    return candidate


def _split_into_chunks(content, target_size=CHUNK_SIZE):
    """Découpe par lignes, en gardant leur numéro dans chaque morceau."""
    lines = content.splitlines(keepends=True)
    if not lines:
        return [(1, 1, "")]

    chunks = []
    current_lines = []
    current_size = 0
    start_line = 1

    for line_number, line in enumerate(lines, start=1):
        if current_lines and current_size + len(line) > target_size:
            chunks.append((start_line, line_number - 1, "".join(current_lines)))
            current_lines = []
            current_size = 0
            start_line = line_number

        current_lines.append(line)
        current_size += len(line)

    if current_lines:
        chunks.append((start_line, len(lines), "".join(current_lines)))

    return chunks


def _response_content(response):
    """Extrait le texte d'une réponse Ollama, objet ou dictionnaire."""
    try:
        message = response["message"]
        return message["content"]
    except (KeyError, TypeError):
        message = getattr(response, "message", None)
        content = getattr(message, "content", None)
        if content is None:
            raise RuntimeError("Le modèle a renvoyé une réponse inattendue.")
        return content


def _analyze_chunk(relative_path, index, total, start_line, end_line, code):
    numbered_code = "".join(
        f"{line_number:>6} | {line}"
        for line_number, line in enumerate(
            code.splitlines(keepends=True),
            start=start_line,
        )
    )

    prompt = f"""Tu analyses un morceau du fichier Python {relative_path}.
Morceau {index}/{total}, lignes {start_line} à {end_line}.

Analyse séparément chacune des catégories suivantes :
1. Bugs et erreurs potentielles
2. Sécurité
3. Performance
4. Code mort ou redondant
5. Couplage
6. Responsabilités multiples
7. Blocs qui devraient être extraits dans des fonctions ou modules
8. Fonctions ou blocs trop longs
9. Routage ou logique conditionnelle trop complexe
10. Architecture difficile à maintenir

Une absence de bug ne signifie pas que l'architecture est bonne. Signale donc les
problèmes concrets de maintenabilité visibles dans ce morceau, même s'ils ne causent
pas encore de panne. Pour chaque constat, indique la catégorie, une priorité parmi
CRITIQUE, ÉLEVÉE, MOYENNE ou FAIBLE, les lignes approximatives concernées, la raison
et une correction concrète. Pour une extraction, précise si possible le bloc à
extraire et la responsabilité cible.

Ne recommande pas d'ajouter des commentaires, de la documentation ou des changements
purement cosmétiques. Le code visible n'est qu'un morceau du fichier : n'invente pas
de problèmes qui exigeraient de voir le contexte absent et qualifie explicitement
toute incertitude due aux limites du morceau. Si aucune des dix catégories ne révèle
de problème concret, réponds exactement : Aucun problème concret.

```python
{numbered_code}
```
"""
    response = chat(
        messages=[{"role": "user", "content": prompt}],
        task_type="analysis_light",
        think=False,
    )
    return _response_content(response).strip()


def deep_analyze_file(path):
    """Analyse un fichier Python par morceaux et retourne un rapport final."""
    try:
        file_path = _resolve_python_path(path)
        content = file_path.read_text(encoding="utf-8", errors="replace")
        static_metrics = _analyze_static(content)
        static_report = _format_static_report(static_metrics)
        chunks = _split_into_chunks(content)
        relative_path = file_path.relative_to(PROJECT_ROOT)

        partial_analyses = []
        total = len(chunks)
        for index, (start_line, end_line, code) in enumerate(chunks, start=1):
            print(f"[Deep Analyzer] Analyse {index}/{total}...")
            analysis = _analyze_chunk(
                relative_path,
                index,
                total,
                start_line,
                end_line,
                code,
            )
            partial_analyses.append(
                f"=== Morceau {index}/{total} (lignes {start_line}-{end_line}) ===\n"
                f"{analysis}"
            )

        combined = "\n\n".join(partial_analyses)
        synthesis_prompt = f"""Produis le rapport final de l'analyse de {relative_path}
à partir des analyses partielles et de l'analyse statique globale ci-dessous.

Les analyses de chunks servent surtout à détecter les problèmes locaux. L'analyse
statique globale sert à évaluer l'architecture générale et la maintenabilité du
fichier entier. Ses signaux sont des indicateurs architecturaux objectifs, pas des
bugs automatiques. Croise ces deux sources, fusionne les doublons et écarte les
suppositions qu'aucune d'elles n'étaye.

Une absence de bugs dans les chunks ne signifie PAS que l'architecture est optimale.
Ne conclus jamais que le fichier est « optimal » uniquement parce qu'aucun bug n'a
été trouvé. Distingue clairement les bugs des problèmes de maintenabilité. Propose
des extractions de modules uniquement si les métriques globales ou les analyses de
chunks les justifient.

Commence le rapport final par une section « Métriques statiques principales » qui
reprend au minimum le nombre de lignes, les fonctions, les classes, les branches
conditionnelles approximatives, l'imbrication maximale et les fonctions longues.

Structure obligatoirement le rapport avec ces sections :
1. Bugs critiques
2. Erreurs potentielles
3. Sécurité
4. Performance
5. Problèmes d'architecture
6. Responsabilités à extraire
7. Redondances
8. Priorités de refactorisation
9. Verdict général

Pour chaque problème, indique une priorité parmi CRITIQUE, ÉLEVÉE, MOYENNE ou
FAIBLE, les lignes approximatives concernées, l'explication et une correction
concrète. Dans les sections d'architecture et d'extraction, précise si possible le
bloc ou la responsabilité à extraire, le nom d'un nouveau module approprié et la
raison de cette séparation. Ordonne les priorités de refactorisation par impact.

Ne recommande pas d'ajouter des commentaires, de la documentation ou des changements
purement cosmétiques. N'invente aucun problème absent des analyses partielles ou
non étayé par les métriques statiques. Si une section ne contient aucun constat
concret, écris « Aucun problème identifié » dans cette section au lieu de la
supprimer.

{static_report}

ANALYSES PARTIELLES :
{combined}
"""
        response = chat(
            messages=[{"role": "user", "content": synthesis_prompt}],
            task_type="analysis",
            think=False,
        )
        return _response_content(response).strip()

    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError) as error:
        return f"Erreur Deep Analyzer : {error}"
    except Exception as error:
        return f"Erreur Deep Analyzer lors de l'analyse : {error}"
