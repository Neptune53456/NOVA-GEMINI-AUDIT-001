"""Garde statique contre l'escalade de capacités lors d'une auto-modification.

Ce module ne cherche pas à prouver qu'un programme Python est sûr. Il applique une
règle plus étroite et testable : un patch autonome ne peut pas *introduire* davantage
d'appels à quelques primitives d'exécution particulièrement sensibles. Les usages
historiques restent modifiables, mais leur nombre ne peut pas croître sans intervention
humaine hors de la boucle autonome.
"""

from __future__ import annotations

import ast
from collections import Counter


_RESTRICTED_EXACT = frozenset({
    "eval",
    "exec",
    "__import__",
    "os.system",
    "os.popen",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "socket.socket",
    "socket.create_connection",
    "shutil.rmtree",
    "marshal.loads",
})
_RESTRICTED_PREFIXES = (
    "ctypes.windll.",
    "ctypes.oledll.",
    "ctypes.cdll.",
)


def _attribute_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".", 1)[0]
                aliases[local] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                if item.name == "*":
                    continue
                local = item.asname or item.name
                aliases[local] = f"{node.module}.{item.name}"
    return aliases


def _canonical_call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    raw = _attribute_name(node.func)
    if raw is None and isinstance(node.func, ast.Name):
        raw = node.func.id
    if not raw:
        return None
    head, dot, tail = raw.partition(".")
    mapped = aliases.get(head)
    if mapped:
        return mapped + (dot + tail if dot else "")
    return raw


def restricted_capability_counts(source: str) -> Counter[str]:
    """Compte les appels sensibles détectables statiquement dans une source Python."""
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, TypeError, ValueError):
        return Counter()
    aliases = _import_aliases(tree)
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _canonical_call_name(node, aliases)
        if not name:
            continue
        if name in _RESTRICTED_EXACT or any(name.startswith(prefix) for prefix in _RESTRICTED_PREFIXES):
            found[name] += 1
    return found


def introduced_restricted_capabilities(original: str, candidate: str) -> dict[str, int]:
    """Retourne uniquement les capacités sensibles dont le nombre augmente."""
    before = restricted_capability_counts(original)
    after = restricted_capability_counts(candidate)
    return {
        name: count - before.get(name, 0)
        for name, count in sorted(after.items())
        if count > before.get(name, 0)
    }
