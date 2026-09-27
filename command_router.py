"""Routage des commandes directes qui ne nécessitent pas le chat LLM."""

import re

from document_command_router import handle_document_command
from multi_file_analyzer import analyze_project_relationships
from project_analyzer import analyze_project
from tools import (
    analyze_text_file,
    get_cpu_info,
    get_datetime,
    get_disk_space,
    get_memory_info,
    get_system_info,
    read_text_file,
)


def _result(response, conversation_response=None):
    return {
        "handled": True,
        "response": response,
        "conversation_response": conversation_response,
    }


def handle_direct_command(user_input):
    """Exécute une commande déterministe reconnue, sinon indique qu'elle ne l'est pas."""
    text_lower = user_input.lower()

    document_result = handle_document_command(user_input)
    if document_result["handled"]:
        print("\nIA :", document_result["response"], "\n")
        return document_result

    if any(expression in text_lower for expression in [
        "quelle heure",
        "quel heure",
        "heure est-il",
        "heure est il",
        "l'heure",
        "la date",
        "quel jour",
        "quelle date",
    ]):
        result = get_datetime()
        print("\nIA :", result, "\n")
        return _result(result, result)

    if any(expression in text_lower for expression in [
        "espace disque",
        "espace libre",
        "stockage libre",
        "place sur mon disque",
        "place sur le disque",
        "combien de go",
    ]):
        result = get_disk_space("C:\\")
        print("\nIA :", result, "\n")
        return _result(result, result)

    if any(expression in text_lower for expression in [
        "quel système",
        "quelle version de windows",
        "infos pc",
        "informations pc",
        "informations système",
        "mon processeur",
        "nom de mon pc",
    ]):
        result = get_system_info()
        print("\nIA :", result, "\n")
        return _result(result, result)

    if any(expression in text_lower for expression in [
        "combien de ram",
        "utilisation ram",
        "ram utilisée",
        "ram disponible",
        "mémoire vive",
    ]):
        result = get_memory_info()
        print("\nIA :", result, "\n")
        return _result(result, result)

    if any(expression in text_lower for expression in [
        "utilisation cpu",
        "cpu utilisé",
        "processeur utilisé",
        "mon cpu",
    ]):
        result = get_cpu_info()
        print("\nIA :", result, "\n")
        return _result(result, result)

    file_match = re.search(
        r'(?:lis|lire|ouvre|affiche)\s+(?:le\s+)?fichier\s+["\']?([^"\']+?\.(?:py|txt|json|md|csv|log|ini|yaml|yml))["\']?',
        user_input,
        re.IGNORECASE,
    )
    if file_match:
        file_path = file_match.group(1).strip()
        result = read_text_file(file_path)
        print(f"\nIA : Contenu de {file_path} :\n")
        print(result)
        print()
        return _result(result, f"J'ai lu le fichier {file_path}.")

    analyze_match = re.search(
        r'(?:analyse|analyser|vérifie|verifie|inspecte)\s+(?:le\s+)?(?:fichier\s+)?["\']?([^"\']+?\.(?:py|txt|json|md|csv|log|ini|yaml|yml))["\']?',
        user_input,
        re.IGNORECASE,
    )
    if analyze_match:
        file_path = analyze_match.group(1).strip()
        print(f"\nIA : Analyse de {file_path} en cours...\n")
        result = analyze_text_file(file_path)
        print(result)
        print()
        return _result(result, result)

    # Dans main.py, l'édition précédait historiquement l'analyse du projet.
    edit_match = re.search(
        r'(?:modifie|corrige|répare|repare)\s+'
        r'(?:le\s+)?(?:fichier\s+)?'
        r'["\']?([^"\']+?\.(?:py|txt|json|md|ini|yaml|yml))["\']?'
        r'(?:\s+(?:pour|afin de|:)\s*(.*))?',
        user_input,
        re.IGNORECASE,
    )
    if edit_match:
        return {
            "handled": False,
            "response": None,
            "conversation_response": None,
        }

    if any(expression in text_lower for expression in [
        "analyse les relations du projet",
        "analyse les dépendances du projet",
        "analyse l'architecture du projet",
        "analyse les fichiers ensemble",
    ]):
        print("\nIA : Analyse des relations entre fichiers en cours...\n")
        result = analyze_project_relationships()
        print(result)
        print()
        return _result(result)

    if any(expression in text_lower for expression in [
        "analyse le projet",
        "analyse tout le projet",
        "analyse mon projet",
        "vérifie le projet",
        "verifie le projet",
    ]):
        print("\nIA : Analyse de l'ensemble du projet en cours...\n")
        try:
            report, analysis = analyze_project()
            print("===== STRUCTURE DU PROJET =====\n")
            print(report)
            print("\n===== ANALYSE IA =====\n")
            print(analysis)
            print()
            return _result(analysis)
        except Exception as error:
            message = f"Erreur pendant l'analyse du projet : {error}"
            print(f"\nIA : {message}\n")
            return _result(message)

    return {
        "handled": False,
        "response": None,
        "conversation_response": None,
    }
