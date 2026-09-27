"""Environnement minimal pour les sous-processus de validation autonomes.

Les tests/benchmarks exécutent du code candidat : ils n'ont pas besoin des clés API
utilisées par le processus parent pour appeler le Planner/Developer Agent. On retire
donc les secrets usuels avant chaque subprocess de validation et on stabilise quelques
variables d'exécution. Ce garde réduit le blast radius d'un test généré incorrect sans
prétendre remplacer un véritable sandbox OS.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_?KEY|ACCESS_?KEY|AUTH)(?:$|_)",
    re.IGNORECASE,
)
_EXPLICIT_SECRET_NAMES = frozenset({
    "CEREBRAS_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "OMNIROUTE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
})


def is_secret_environment_name(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return False
    return normalized.upper() in _EXPLICIT_SECRET_NAMES or bool(_SECRET_ENV_RE.search(normalized))


def sanitized_child_environment(
    source: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copie l'environnement en supprimant les secrets pour tests/benchmarks.

    Les variables système (PATH, TEMP, HOME, etc.) sont conservées pour ne pas casser
    Python/pytest. ``extra`` est appliqué après filtrage et doit être composé de valeurs
    de contrôle non secrètes choisies par le code de confiance.
    """
    raw = dict(os.environ if source is None else source)
    safe = {
        str(key): str(value)
        for key, value in raw.items()
        if not is_secret_environment_name(str(key))
    }
    safe["PYTHONHASHSEED"] = "0"
    safe["PYTHONDONTWRITEBYTECODE"] = "1"
    safe["PROJET_IA_AUTONOMOUS_VALIDATION"] = "1"
    if extra:
        safe.update({str(key): str(value) for key, value in extra.items()})
    return safe


def model_agent_environment(
    source: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for the *agent process*, not for candidate tests.

    It keeps only the explicitly supported LLM provider credentials on top of the
    sanitized base environment.  Candidate tests and benchmarks must continue to
    use :func:`sanitized_child_environment`, which receives no provider secret.
    """
    raw = dict(os.environ if source is None else source)
    safe = sanitized_child_environment(raw)
    for name in _EXPLICIT_SECRET_NAMES:
        # Only model providers are required by the engineering agent.  GitHub
        # credentials are deliberately not reintroduced.
        if name in {"GITHUB_TOKEN", "GH_TOKEN"}:
            continue
        value = raw.get(name)
        if value:
            safe[name] = str(value)
    if extra:
        safe.update({str(key): str(value) for key, value in extra.items()})
    return safe
