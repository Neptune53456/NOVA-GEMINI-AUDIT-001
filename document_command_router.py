"""Reconnaissance déterministe des demandes portant sur des documents locaux."""

import re
import unicodedata

from document_tools import analyze_document, compare_documents


EXTENSIONS = r"(?:pdf|txt|md|docx)"


def _normalize(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in value if not unicodedata.combining(character))


def _path_from_fragment(fragment):
    quoted = re.search(rf'["\']([^"\']+\.{EXTENSIONS})["\']', fragment, re.IGNORECASE)
    if quoted:
        return quoted.group(1).strip()
    windows = re.search(rf"([A-Za-z]:[\\/].*?\.{EXTENSIONS})\b", fragment, re.IGNORECASE)
    if windows:
        return windows.group(1).strip()
    simple = re.search(rf"([^\s,;]+\.{EXTENSIONS})\b", fragment, re.IGNORECASE)
    return simple.group(1).strip() if simple else None


def _comparison_paths(user_input):
    body = re.sub(r"^.*?\bcompare(?:r)?\b", "", user_input, count=1, flags=re.IGNORECASE)
    for separator in (r"\s+avec\s+", r"\s+et\s+"):
        parts = re.split(separator, body, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            left = _path_from_fragment(parts[0])
            right = _path_from_fragment(parts[1])
            if left and right:
                return left, right
    quoted = re.findall(rf'["\']([^"\']+\.{EXTENSIONS})["\']', body, re.IGNORECASE)
    return tuple(quoted[:2]) if len(quoted) >= 2 else None


def _question_from_request(user_input, path):
    question = user_input.replace(path, " ") if path else user_input
    question = re.sub(
        r"\b(?:reponds?|réponds?)\b(?:\s+a|\s+à)?(?:\s+(?:cette|la))?\s+question\s*[:\-]?",
        " ",
        question,
        flags=re.IGNORECASE,
    )
    question = re.sub(
        r"\b(?:a|à)\s+partir\s+(?:de|du)\s+(?:ce|le|la)?\s*(?:pdf|document|fichier)?",
        " ",
        question,
        flags=re.IGNORECASE,
    )
    return question.strip(" \t\r\n:,-?")


def _format_result(result):
    if result.get("success"):
        return result["answer"]
    error = result.get("error") or {}
    return f"Erreur document ({error.get('code', 'UNKNOWN')}) : {error.get('message', 'Échec inconnu.')}"


def handle_document_command(user_input, active_attachment=None, status_callback=None):
    """Route une demande documentaire explicite ou indique qu'elle ne correspond pas."""
    normalized = _normalize(user_input)
    comparison = bool(re.search(r"\bcompar(?:e|er)\b", normalized))
    summary = bool(re.search(r"\b(?:resume|resumer|synthese|synthetise)\b", normalized))
    key_points = "information" in normalized and "important" in normalized
    question_request = bool(re.search(r"\b(?:reponds?|question)\b", normalized)) and (
        "a partir" in normalized or "selon" in normalized
    )
    analysis = bool(re.search(r"\b(?:analyse|analyser|inspecte)\b", normalized))
    attachment_reference = bool(
        re.search(r"\b(?:piece jointe|fichier joint|document joint)\b", normalized)
    )
    document_word = bool(
        re.search(r"\b(?:pdf|document|fichier)\b", normalized)
        or attachment_reference
    )
    attachment_correction = bool(
        active_attachment is not None
        and attachment_reference
        and re.search(r"\b(?:non|plutot|je parle de)\b", normalized)
    )

    if not any((comparison, summary, key_points, question_request, analysis, attachment_correction)):
        return {"handled": False, "response": None, "conversation_response": None}
    if not document_word and not re.search(rf"\.{EXTENSIONS}\b", normalized):
        return {"handled": False, "response": None, "conversation_response": None}

    if comparison:
        paths = _comparison_paths(user_input)
        comparison_options = (
            {"status_callback": status_callback} if status_callback is not None else {}
        )
        result = (
            compare_documents(*paths, **comparison_options)
            if paths
            else {
                "success": False,
                "error": {
                    "code": "MISSING_PATHS",
                    "message": "Deux chemins de documents explicites sont requis.",
                },
            }
        )
    else:
        path = _path_from_fragment(user_input)
        source = path or active_attachment
        if source is None:
            result = {
                "success": False,
                "error": {
                    "code": (
                        "NO_ACTIVE_ATTACHMENT" if attachment_reference else "MISSING_PATH"
                    ),
                    "message": (
                        "Aucune pièce jointe n'est active. Joins d'abord un document."
                        if attachment_reference
                        else "Indique le chemin explicite du document à utiliser."
                    ),
                },
            }
        elif question_request:
            question = _question_from_request(user_input, path or "")
            analysis_options = (
                {"status_callback": status_callback} if status_callback is not None else {}
            )
            result = analyze_document(
                source,
                operation="question",
                question=question,
                **analysis_options,
            )
        else:
            operation = "summary" if summary else "key_points" if key_points else "analysis"
            analysis_options = (
                {"status_callback": status_callback} if status_callback is not None else {}
            )
            result = analyze_document(
                source,
                operation=operation,
                **analysis_options,
            )

    response = _format_result(result)
    return {
        "handled": True,
        "response": response,
        "conversation_response": response,
        "document_result": result,
    }
