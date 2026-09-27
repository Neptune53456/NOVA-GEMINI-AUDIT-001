"""Gestion de la conversation normale avec le modèle et ses outils."""

import json
import re
import unicodedata

from action_planner import ActionPlanner
from model_router import chat
from personality import SYSTEM_PROMPT
from request_interpreter import RequestInterpreter
from smart_memory import auto_save_memory, search_memories
from system_action_controller import SystemActionController
from tools import (
    analyze_text_file,
    calculate,
    get_battery_info,
    get_cpu_info,
    get_datetime,
    get_disk_space,
    get_memory_info,
    get_system_info,
    list_files,
    read_text_file,
)
from web_tools import (
    build_high_confidence_version_fact,
    extract_version_candidates,
    rank_version_candidates,
    read_web_page,
    search_web,
)


USER_PATH_DESCRIPTION = (
    "Chemin Windows ou chemin naturel utilisant Bureau/Desktop, Documents, "
    "Téléchargements/Downloads, Images/Pictures, Vidéos/Videos ou Musique/Music. "
    "Exemples : 'TestIA sur mon Bureau', 'test.txt dans TestIA sur mon Bureau'."
)

CREATE_TEXT_PATH_DESCRIPTION = (
    USER_PATH_DESCRIPTION
    + " Fournis un unique path complet ou naturel au fichier, par exemple "
    "'test2.txt dans TestIA2 sur mon Bureau'. Ne préfixe pas cette expression "
    "avec le dossier courant du projet."
)

SYSTEM_ACTION_TOOL_NAMES = {
    "open_application",
    "open_path",
    "create_folder",
    "create_text_file",
    "copy_file",
    "move_file",
    "delete_file",
    "delete_folder",
}

PLANNING_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "action": {"const": action_name},
                            "arguments": {
                                "type": "object",
                                "properties": {
                                    argument_name: {"type": "string"}
                                    for argument_name in argument_names
                                },
                                "required": list(argument_names),
                                "additionalProperties": False,
                            },
                        },
                        "required": ["action", "arguments"],
                        "additionalProperties": False,
                    }
                    for action_name, argument_names in (
                        ("open_application", ("name",)),
                        ("open_path", ("path",)),
                        ("create_folder", ("path",)),
                        ("create_text_file", ("path",)),
                        ("copy_file", ("source", "destination")),
                        ("move_file", ("source", "destination")),
                        ("delete_file", ("path",)),
                        ("delete_folder", ("path",)),
                    )
                ]
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}

SYSTEM_ACTION_RULE = (
    "Ne dis jamais qu'une action sur le PC a été effectuée sans avoir reçu un "
    "résultat positif de l'outil correspondant. Si l'outil échoue, rapporte son "
    "message réel. S'il exige une confirmation, demande la confirmation sans "
    "prétendre que l'action est déjà effectuée."
)

SYSTEM_ACTION_RETRY_INSTRUCTION = (
    "La demande utilisateur nécessite une action réelle sur le PC. Tu dois utiliser "
    "l'outil approprié. Ne prétends jamais qu'une action a été effectuée sans "
    "résultat d'outil."
)

ACTION_PLAN_RETRY_INSTRUCTION = (
    "La demande nécessite des actions réelles sur le PC. Utilise "
    "execute_action_plan et ne prétends pas que les actions sont effectuées sans "
    "résultats d'outils."
)

WEB_RETRY_INSTRUCTION = (
    "La demande nécessite une vraie recherche sur Internet. Utilise search_web ou "
    "read_web_page. Ne prétends pas avoir recherché ou lu une source sans résultat "
    "réel d'outil web."
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "Donne la date et l'heure actuelles du PC.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Donne les informations principales du PC et du système.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_disk_space",
            "description": "Donne l'espace total, utilisé et libre du disque C.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Effectue un calcul mathématique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Expression mathématique, par exemple 25 * 4 + 10"
                    }
                },
                "required": ["expression"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_info",
            "description": "Donne l'utilisation actuelle de la RAM du PC.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cpu_info",
            "description": "Donne l'utilisation actuelle du processeur et le nombre de cœurs.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_battery_info",
            "description": "Donne le niveau actuel de la batterie du PC.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Liste les fichiers et dossiers présents dans un dossier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Chemin du dossier. Utilise '.' pour le dossier actuel."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "Lit le contenu d'un fichier texte ou de code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Chemin du fichier à lire, par exemple main.py."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Ouvre une application Windows autorisée par la liste blanche.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_path",
            "description": "Ouvre un fichier ou dossier local existant et non sensible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": USER_PATH_DESCRIPTION}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": "Crée un dossier local dans un chemin autorisé.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": USER_PATH_DESCRIPTION}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_text_file",
            "description": "Crée un fichier texte vide. Le remplacement exige une confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": CREATE_TEXT_PATH_DESCRIPTION}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": "Copie un fichier. Le remplacement exige une confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": USER_PATH_DESCRIPTION},
                    "destination": {"type": "string", "description": USER_PATH_DESCRIPTION}
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Déplace ou renomme un fichier. Un écrasement exige une confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": USER_PATH_DESCRIPTION},
                    "destination": {"type": "string", "description": USER_PATH_DESCRIPTION}
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Demande la suppression d'un fichier. Confirmation utilisateur obligatoire.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": USER_PATH_DESCRIPTION}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_folder",
            "description": "Demande la suppression d'un dossier utilisateur. Confirmation obligatoire.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": USER_PATH_DESCRIPTION}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_action_plan",
            "description": (
                "Valide et exécute dans l'ordre plusieurs actions PC. Utilise cet "
                "outil pour toute demande comportant plusieurs actions système."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": sorted(SYSTEM_ACTION_TOOL_NAMES),
                                },
                                "arguments": {"type": "object"},
                            },
                            "required": ["action", "arguments"],
                        },
                    }
                },
                "required": ["actions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Recherche des informations publiques et récentes sur Internet. "
                "À utiliser lorsque la réponse dépend d'informations actuelles "
                "ou inconnues localement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_web_page",
            "description": (
                "Lit le contenu textuel d'une page web publique à partir d'une "
                "URL HTTP ou HTTPS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    }
]


AVAILABLE_TOOLS = {
    "get_datetime": get_datetime,
    "get_system_info": get_system_info,
    "get_disk_space": get_disk_space,
    "calculate": calculate,
    "get_memory_info": get_memory_info,
    "get_cpu_info": get_cpu_info,
    "get_battery_info": get_battery_info,
    "list_files": list_files,
    "read_text_file": read_text_file,
    "analyze_text_file": analyze_text_file,
    "search_web": search_web,
    "read_web_page": read_web_page,
}

WEB_TOOL_NAMES = {"search_web", "read_web_page"}

class ConversationManager:
    """Conserve l'historique court et exécute le flux conversationnel LLM."""

    MAX_CONVERSATION_MESSAGES = 20

    def __init__(
        self,
        chat_function=chat,
        memory_search=search_memories,
        memory_save=auto_save_memory,
        system_prompt=SYSTEM_PROMPT,
        available_tools=None,
        tool_definitions=None,
        system_action_controller=None,
        action_planner=None,
        request_interpreter=None,
    ):
        self.conversation = []
        self._chat = chat_function
        self._search_memories = memory_search
        self._auto_save_memory = memory_save
        self._system_prompt = system_prompt
        self._available_tools = available_tools or AVAILABLE_TOOLS
        self._tool_definitions = TOOLS if tool_definitions is None else tool_definitions
        self._system_action_controller = (
            system_action_controller or SystemActionController()
        )
        self._action_planner = (
            action_planner or ActionPlanner(self._system_action_controller)
        )
        self._request_interpreter = request_interpreter or RequestInterpreter(
            context_chat=chat_function
        )
        self._last_context_error_code = None

    def set_active_attachment(self, metadata):
        """Expose uniquement les métadonnées de la pièce jointe au contexte de session."""
        self._request_interpreter.set_active_attachment(metadata)

    def get_last_context_error_code(self):
        """Expose l'échec déterministe du dernier tour sans changer l'API texte."""
        return self._last_context_error_code

    def get_session_context(self):
        """Retourne un instantané lisible sans exposer l'état mutable interne."""
        context = self._request_interpreter.context
        pending = context.pending
        return {
            "last_intent": context.last_intent,
            "last_folder": context.last_folder,
            "last_file": context.last_file,
            "last_path": context.last_path,
            "last_action": context.last_action,
            "active_attachment": (
                dict(context.active_attachment) if context.active_attachment else None
            ),
            "pending": (
                {
                    "intent": pending.intent,
                    "entities": dict(pending.entities),
                    "missing": list(pending.missing),
                    "candidates": list(pending.candidates),
                }
                if pending
                else None
            ),
        }

    def _handle_context_interpretation(self, user_input, interpretation):
        if interpretation.kind in {"clarification", "information"}:
            return self._store_answer(user_input, interpretation.message)

        if interpretation.kind == "action":
            result = self._system_action_controller.request(
                interpretation.action,
                interpretation.arguments,
            )
            if result.get("success"):
                self._request_interpreter.context.last_action = interpretation.action
            return self._store_answer(user_input, result.get("message", str(result)))

        if interpretation.kind == "plan":
            result = self._action_planner.start(interpretation.actions)
            if result.get("success"):
                self._request_interpreter.context.last_action = "action_plan"
            return self._store_answer(user_input, result.get("message", str(result)))

        return None

    def _trim_history(self):
        if len(self.conversation) > self.MAX_CONVERSATION_MESSAGES:
            self.conversation = self.conversation[-self.MAX_CONVERSATION_MESSAGES:]

    @staticmethod
    def _requires_system_action(user_input):
        normalized = unicodedata.normalize("NFKD", user_input.casefold())
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        patterns = (
            r"\b(?:ouvre|ouvrir|lance|lancer|demarre|demarrer)\b",
            r"\b(?:cree|creer)\b.*\b(?:dossier|fichier)\b",
            r"\b(?:copie|copier)\b.*\bfichier\b",
            r"\b(?:deplace|deplacer|renomme|renommer)\b.*\bfichier\b",
            r"\b(?:supprime|supprimer|efface|effacer)\b.*\b(?:fichier|dossier)\b",
        )
        return any(re.search(pattern, normalized) for pattern in patterns)

    @staticmethod
    def _detect_sensitive_system_action(user_input):
        """Reconnaît seulement les demandes de suppression non ambiguës."""
        verb = r"(?:supprime(?:r)?|efface(?:r)?)"
        explicit_patterns = (
            ("delete_file", rf"^\s*{verb}\s+(?:(?:le|un)\s+)?fichier\s+(.+?)\s*$"),
            ("delete_folder", rf"^\s*{verb}\s+(?:(?:le|un)\s+)?dossier\s+(.+?)\s*$"),
        )
        for action_name, pattern in explicit_patterns:
            match = re.match(pattern, user_input, flags=re.IGNORECASE)
            if match:
                target = match.group(1).strip().strip('"\'')
                if target:
                    return action_name, target

        implicit_file = re.match(
            rf"^\s*{verb}\s+(?:(?:le|un)\s+)?"
            rf"(.+\.[A-Za-z0-9]{{1,10}}(?:\s+(?:dans|sur)\s+.+)?)\s*$",
            user_input,
            flags=re.IGNORECASE,
        )
        if implicit_file:
            return "delete_file", implicit_file.group(1).strip().strip('"\'')

        absolute_folder = re.match(
            rf"^\s*{verb}\s+([A-Za-z]:[\\/].+?)\s*$",
            user_input,
            flags=re.IGNORECASE,
        )
        if absolute_folder:
            return "delete_folder", absolute_folder.group(1).strip().strip('"\'')

        return None

    @staticmethod
    def _requires_action_plan(user_input):
        normalized = unicodedata.normalize("NFKD", user_input.casefold())
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        action_verbs = re.findall(
            r"\b(?:ouvre|ouvrir|lance|lancer|cree|creer|copie|copier|"
            r"deplace|deplacer|renomme|renommer|supprime|supprimer|"
            r"efface|effacer)\b",
            normalized,
        )
        return len(action_verbs) >= 2

    @staticmethod
    def _requires_web_tool(user_input):
        normalized = unicodedata.normalize("NFKD", user_input.casefold())
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        patterns = (
            r"\b(?:cherche|chercher|recherche|rechercher)\b(?:.*\b(?:internet|web)\b)?",
            r"\b(?:dernieres nouvelles|actualites recentes|derniere actualite)\b",
            r"\bversion actuelle\b",
            r"\b(?:regarde|verifie)\s+(?:sur|dans)\s+(?:le\s+)?(?:web|internet)\b",
        )
        return any(re.search(pattern, normalized) for pattern in patterns)

    @staticmethod
    def _detect_explicit_web_request(user_input):
        search_patterns = (
            r"^\s*(?:cherche|recherche|regarde|trouve)\s+"
            r"(?:sur|dans)\s+(?:le\s+)?(?:web|internet)\s*[:\-]?\s*(.+?)\s*$",
            r"^\s*fais\s+une\s+recherche\s+sur\s+(.+?)\s*$",
        )
        for pattern in search_patterns:
            match = re.match(pattern, user_input, flags=re.IGNORECASE)
            if match and match.group(1).strip():
                query = re.sub(
                    r"^(?:le|la|les|des|l['’])\s*",
                    "",
                    match.group(1).strip(),
                    flags=re.IGNORECASE,
                )
                return "search_web", {"query": query}

        page_match = re.match(
            r"^\s*(?:lis|résume|resume|ouvre\s+et\s+lis)\s+"
            r"(https?://\S+)\s*$",
            user_input,
            flags=re.IGNORECASE,
        )
        if page_match:
            url = page_match.group(1).strip().strip('"\'')
            return "read_web_page", {"url": url}
        return None

    def _store_answer(self, user_input, answer):
        self.add_exchange(user_input, answer)
        return answer

    @staticmethod
    def _assistant_message(response):
        """Valide la structure minimale d'une réponse du modèle."""
        if not isinstance(response, dict):
            raise ValueError("Réponse du modèle invalide.")
        message = response.get("message")
        if not isinstance(message, dict):
            raise ValueError("Message du modèle absent ou invalide.")
        return message

    def _generate_and_execute_plan(self, user_input):
        prompt = f"""Transforme la demande utilisateur en plan d'actions système JSON.

DEMANDE :
{user_input}

Règles strictes :
- Produis uniquement l'objet JSON conforme au schéma demandé.
- Ne réponds pas à l'utilisateur et ne prétends avoir exécuté aucune action.
- Utilise uniquement les actions autorisées par le schéma.
- Utilise exactement ces arguments : path pour ouvrir/créer/supprimer, name pour
  open_application, source et destination pour copy_file ou move_file.
- create_folder sert uniquement à créer un dossier.
- create_text_file sert uniquement à créer un fichier texte.
- open_path sert à ouvrir un fichier ou dossier déjà identifié, notamment pour
  « ouvre-le », « ouvre-la » ou « ouvre ce fichier ».
- open_application sert uniquement à lancer une application installée par son nom.
- Respecte exactement l'ordre demandé.
- Conserve les chemins naturels comme « ProjetAgent sur mon Bureau ».
- Résous « dedans », « ce fichier », « ce dossier », « ouvre-le » et « ouvre-la »
  à partir des étapes précédentes.
- Pour un fichier créé dans un dossier précédent, utilise par exemple
  « notes.txt dans ProjetAgent sur mon Bureau ».

Exemple obligatoire de raisonnement structurel, sans texte autour du JSON :
Demande : « crée un dossier ProjetAgent sur mon Bureau, crée un fichier notes.txt
dedans et ouvre-le »
Plan :
{{
  "actions": [
    {{"action": "create_folder", "arguments": {{"path": "ProjetAgent sur mon Bureau"}}}},
    {{"action": "create_text_file", "arguments": {{"path": "notes.txt dans ProjetAgent sur mon Bureau"}}}},
    {{"action": "open_path", "arguments": {{"path": "notes.txt dans ProjetAgent sur mon Bureau"}}}}
  ]
}}
"""
        try:
            response = self._chat(
                messages=[{"role": "user", "content": prompt}],
                task_type="planning",
                think=False,
                format=PLANNING_SCHEMA,
            )
            content = self._assistant_message(response).get("content")
            plan_data = json.loads(content) if isinstance(content, str) else content
            if not isinstance(plan_data, dict):
                raise ValueError("Le plan JSON doit être un objet.")
            actions = plan_data.get("actions")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"[Planner] Plan JSON invalide : {error}")
            return self._store_answer(
                user_input,
                "Je n'ai pas pu exécuter réellement ce plan.",
            )
        except Exception as error:
            print(f"[Planner] Erreur pendant la génération du plan : {error}")
            return self._store_answer(
                user_input,
                "Je n'ai pas pu exécuter réellement ce plan.",
            )

        step_count = len(actions) if isinstance(actions, list) else 0
        print(f"[Planner] Plan généré : {step_count} étapes")
        result = self._action_planner.start(actions)
        return self._store_answer(user_input, result["message"])

    def _execute_explicit_web_request(self, user_input, tool_name, arguments):
        tool = self._available_tools.get(tool_name)
        if tool is None:
            return self._store_answer(
                user_input,
                "L'outil web demandé n'est pas disponible.",
            )
        try:
            result = tool(**arguments)
        except Exception as error:
            return self._store_answer(user_input, f"Erreur web réelle : {error}")

        if not isinstance(result, dict) or not result.get("success"):
            error = (
                result.get("error", "La requête web a échoué.")
                if isinstance(result, dict)
                else "L'outil web a renvoyé un résultat invalide."
            )
            return self._store_answer(user_input, error)

        if (
            tool_name == "search_web"
            and result.get("freshness_sensitive")
            and result.get("results")
            and result["results"][0].get("official")
        ):
            page_reader = self._available_tools.get("read_web_page")
            if page_reader is not None:
                verified_pages = []
                official_result = next(
                    item for item in result["results"] if item.get("official")
                )
                print("[Web] Lecture source officielle...")
                for search_result in [official_result]:
                    try:
                        page_result = page_reader(search_result["url"])
                    except Exception as error:
                        page_result = {
                            "success": False,
                            "error": f"Lecture de vérification impossible : {error}",
                        }
                    verified_pages.append({
                        "search_result": search_result,
                        "page_result": page_result,
                    })

                factual_items = list(result["results"])
                for verified in verified_pages:
                    page = verified["page_result"]
                    if page.get("success"):
                        factual_items.append({
                            **page,
                            "official": verified["search_result"].get("official", False),
                        })
                raw_candidates = (
                    extract_version_candidates(factual_items)
                    if result.get("version_intent")
                    else []
                )
                candidates = rank_version_candidates(raw_candidates)

                # Une page officielle générique peut ne contenir aucune réponse
                # exploitable. Une seule recherche raffinée est alors permise.
                if result.get("version_intent") and not candidates:
                    official_domain = result.get("official_domain")
                    refined_query = (
                        f"site:{official_domain} latest stable release"
                        if official_domain
                        else arguments["query"] + " latest stable release"
                    )
                    refined_result = tool(query=refined_query, max_results=3)
                    result = dict(result)
                    result["refined_search"] = refined_result
                    if isinstance(refined_result, dict) and refined_result.get("success"):
                        refined_results = refined_result.get("results", [])
                        factual_items.extend(refined_results)
                        refined_official = next(
                            (item for item in refined_results if item.get("official")),
                            None,
                        )
                        if refined_official is not None and len(verified_pages) < 2:
                            try:
                                refined_page = page_reader(refined_official["url"])
                            except Exception as error:
                                refined_page = {
                                    "success": False,
                                    "error": f"Lecture de vérification impossible : {error}",
                                }
                            verified_pages.append({
                                "search_result": refined_official,
                                "page_result": refined_page,
                            })
                            if refined_page.get("success"):
                                factual_items.append({
                                    **refined_page,
                                    "official": True,
                                })
                        raw_candidates = extract_version_candidates(factual_items)
                        candidates = rank_version_candidates(raw_candidates)

                result = dict(result)
                result["verified_pages"] = verified_pages
                result["factual_candidates"] = candidates
                print(f"[Web] Candidats bruts : {len(raw_candidates)}")
                print("[Web] Candidats retenus :")
                for candidate in candidates:
                    source_kind = "official" if candidate["official"] else "third-party"
                    print(
                        f"- {candidate['version']} | {candidate['status']} | "
                        f"{source_kind} | {candidate['evidence_type']}"
                    )
                if candidates:
                    print(f"[Web] Candidat principal : {candidates[0]['version']}")

                subject = None
                if result.get("official_domain"):
                    subject = result["official_domain"].split(".")[0].title()
                high_confidence_fact = build_high_confidence_version_fact(
                    candidates,
                    subject=subject,
                )
                if result.get("version_intent") and high_confidence_fact is not None:
                    result["high_confidence_fact"] = high_confidence_fact
                    print("[Web] Confiance : HIGH")
                    print("[Web] Réponse factuelle directe - LLM non utilisé")
                    date_part = (
                        f", publiée le {high_confidence_fact['date']}"
                        if high_confidence_fact.get("date") else ""
                    )
                    answer = (
                        "La dernière version stable trouvée est "
                        f"{high_confidence_fact['subject']} "
                        f"{high_confidence_fact['value']}{date_part}, selon le site "
                        f"officiel {high_confidence_fact['source_domain']}."
                    )
                    return self._store_answer(user_input, answer)

                if result.get("version_intent") and not candidates:
                    return self._store_answer(
                        user_input,
                        "Les sources consultées ne permettent pas de déterminer "
                        "avec certitude la version actuelle.",
                    )

        messages = [
            {
                "role": "system",
                "content": (
                    self._system_prompt
                    + "\n\nRéponds en français, directement à la question, en restant "
                    "concis. Ne dis pas « The provided text » et ne crée pas de "
                    "tableau sauf nécessité réelle. Réponds d'abord en une ou deux "
                    "phrases. Ensuite, seulement si utile, ajoute une courte "
                    "explication. N'analyse pas chaque résultat séparément. "
                    "Ignore les résultats hors sujet. Synthétise uniquement les "
                    "sources, pages vérifiées et candidats factuels fournis. Les "
                    "résultats de recherche peuvent être anciens ou "
                    "mal classés. Pour une question sur une information actuelle, "
                    "privilégie : 1. la source officielle, 2. la date la plus "
                    "récente, 3. la concordance entre sources. Ne présente jamais "
                    "une ancienne information comme actuelle si un résultat plus "
                    "récent est disponible. N'utilise que les dates réellement "
                    "présentes dans les résultats ou pages, distingue une date de "
                    "publication d'une date simplement mentionnée dans le texte, "
                    "et n'invente aucune date, source, URL ou information absente. "
                    "Réponds uniquement à partir des candidats factuels classés. "
                    "Le candidat n°1 est celui considéré comme le plus fiable par "
                    "l'analyse statique. Ne choisis pas une version absente des "
                    "candidats et ne réanalyse pas toute l'histoire des versions. "
                    "Si les données sont insuffisantes, dis-le. "
                    "Si les sources se contredisent, indique-le clairement."
                ),
            }
        ]
        messages.extend(self.conversation)
        messages.append({"role": "user", "content": user_input})
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": tool_name,
                    "arguments": arguments,
                }
            }],
        })
        if tool_name == "search_web":
            combined_sources = list(result.get("results", []))
            refined = result.get("refined_search")
            if isinstance(refined, dict):
                combined_sources.extend(refined.get("results", []))
            compact_sources = []
            seen_urls = set()
            for source in combined_sources:
                if source.get("url") not in seen_urls:
                    seen_urls.add(source.get("url"))
                    compact_sources.append(source)
                if len(compact_sources) == 3:
                    break
            facts = result.get("factual_candidates", [])[:5]
            web_payload = {
                "QUESTION": user_input,
                "SOURCES": compact_sources,
                "EXTRAITS PERTINENTS": [fact["context"] for fact in facts],
                "FAITS EXTRAITS": facts,
                "DATES FIABLES": [
                    fact["date"] for fact in facts if fact.get("date")
                ],
            }
        else:
            web_payload = {
                "QUESTION UTILISATEUR": user_input,
                "PAGE WEB": result,
            }
        messages.append({
            "role": "tool",
            "tool_name": tool_name,
            "content": json.dumps(web_payload, ensure_ascii=False),
        })
        if result.get("freshness_sensitive"):
            print("[Web] Synthèse complexe")
        try:
            response = self._chat(
                messages=messages,
                task_type="web_synthesis",
                tools=self._tool_definitions,
                think=False,
            )
            answer = response["message"]["content"].strip()
        except Exception as error:
            return self._store_answer(
                user_input,
                f"La requête web a réussi, mais la synthèse a échoué : {error}",
            )

        if not answer:
            answer = "La requête web a réussi, mais aucune synthèse n'a été générée."
        return self._store_answer(user_input, answer)

    def add_exchange(self, user_input, assistant_response):
        """Ajoute à l'historique une réponse produite par une route directe."""
        self.conversation.append({
            "role": "user",
            "content": user_input,
        })
        self.conversation.append({
            "role": "assistant",
            "content": assistant_response,
        })
        self._trim_history()

    def handle_system_confirmation(self, user_input):
        """Confirme d'abord un plan suspendu, sinon une action système simple."""
        if self._action_planner.waiting_confirmation:
            pending = getattr(self._action_planner, "pending_step", None) or {}
            result = self._action_planner.handle_confirmation(user_input)
            plan_result = result.get("plan_result") if isinstance(result, dict) else None
            if (
                isinstance(plan_result, dict)
                and plan_result.get("success")
                and pending.get("action")
            ):
                self._request_interpreter.context.last_action = pending["action"]
            return result

        pending = (
            getattr(self._system_action_controller, "pending_system_action", None) or {}
        )
        result = self._system_action_controller.handle_confirmation(user_input)
        action_result = result.get("result") if isinstance(result, dict) else None
        if (
            isinstance(action_result, dict)
            and action_result.get("success")
            and pending.get("action_name")
        ):
            self._request_interpreter.context.last_action = pending["action_name"]
        return result

    def handle(self, user_input):
        """Traite une interaction LLM normale et retourne le texte final."""
        self._last_context_error_code = None
        interpretation = self._request_interpreter.interpret(user_input)
        if interpretation.handled:
            self._last_context_error_code = interpretation.error_code
            return self._handle_context_interpretation(user_input, interpretation)

        explicit_web_request = self._detect_explicit_web_request(user_input)
        if explicit_web_request is not None:
            tool_name, arguments = explicit_web_request
            return self._execute_explicit_web_request(
                user_input,
                tool_name,
                arguments,
            )

        try:
            remembered = self._auto_save_memory(user_input)
        except Exception as error:
            remembered = False
            print(f"[Mémoire] Enregistrement indisponible : {error}")
        if remembered:
            print("[Mémoire] Information enregistrée.")

        is_multi_action_request = self._requires_action_plan(user_input)
        sensitive_action = self._detect_sensitive_system_action(user_input)
        if sensitive_action is not None and not is_multi_action_request:
            action_name, target = sensitive_action
            result = self._system_action_controller.request(
                action_name,
                {"path": target},
            )
            return self._store_answer(user_input, result["message"])

        if is_multi_action_request:
            print("[Planner] Demande multi-actions détectée")
            return self._generate_and_execute_plan(user_input)

        try:
            memories = self._search_memories(user_input, limit=3)
        except Exception as error:
            memories = []
            print(f"[Mémoire] Recherche indisponible : {error}")
        memory_context = ""
        if memories:
            memory_context = "\nSouvenirs potentiellement utiles :\n"
            for memory in memories:
                memory_context += f"- {memory}\n"

        messages = [
            {
                "role": "system",
                "content": (
                    self._system_prompt
                    + memory_context
                    + "\n\nRègle impérative pour les actions PC : "
                    + SYSTEM_ACTION_RULE
                    + "\n\nRègle impérative pour le web : ne dis jamais avoir "
                    "recherché Internet ou lu une URL sans résultat positif de "
                    "search_web ou read_web_page. Ne cite aucune source absente "
                    "des résultats d'outils."
                ),
            }
        ]
        messages.extend(self.conversation)
        messages.append({
            "role": "user",
            "content": user_input,
        })

        try:
            response = self._chat(
                messages=messages,
                task_type="chat",
                tools=self._tool_definitions,
                think=False,
            )
            assistant_message = self._assistant_message(response)
        except Exception as error:
            print(
                f"\nIA : Erreur pendant la communication avec Ollama : {error}\n"
            )
            return None

        tool_calls = assistant_message.get("tool_calls") or []
        requires_system_action = self._requires_system_action(user_input)
        requires_action_plan = is_multi_action_request
        requires_web_tool = self._requires_web_tool(user_input)

        has_action_plan = any(
            call.get("function", {}).get("name") == "execute_action_plan"
            for call in tool_calls
            if isinstance(call, dict)
        )

        if requires_action_plan and not has_action_plan:
            retry_messages = list(messages)
            retry_messages.insert(-1, {
                "role": "system",
                "content": ACTION_PLAN_RETRY_INSTRUCTION,
            })
            try:
                response = self._chat(
                    messages=retry_messages,
                    task_type="chat",
                    tools=self._tool_definitions,
                    think=False,
                )
                assistant_message = self._assistant_message(response)
            except Exception as error:
                print(f"\nIA : Erreur pendant la nouvelle tentative de plan : {error}\n")
                return None

            messages = retry_messages
            tool_calls = assistant_message.get("tool_calls") or []
            has_action_plan = any(
                call.get("function", {}).get("name") == "execute_action_plan"
                for call in tool_calls
                if isinstance(call, dict)
            )
            if not has_action_plan:
                return self._store_answer(
                    user_input,
                    "Je n'ai pas pu exécuter réellement ce plan.",
                )

        if requires_system_action and not requires_action_plan and not tool_calls:
            retry_messages = list(messages)
            retry_messages.insert(-1, {
                "role": "system",
                "content": SYSTEM_ACTION_RETRY_INSTRUCTION,
            })
            try:
                response = self._chat(
                    messages=retry_messages,
                    task_type="chat",
                    tools=self._tool_definitions,
                    think=False,
                )
                assistant_message = self._assistant_message(response)
            except Exception as error:
                print(
                    f"\nIA : Erreur pendant la nouvelle tentative d'action : {error}\n"
                )
                return None

            messages = retry_messages
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                return self._store_answer(
                    user_input,
                    "Je n'ai pas pu exécuter réellement cette action.",
                )

        has_web_tool = any(
            call.get("function", {}).get("name") in WEB_TOOL_NAMES
            for call in tool_calls
            if isinstance(call, dict)
        )
        if requires_web_tool and not has_web_tool:
            retry_messages = list(messages)
            retry_messages.insert(-1, {
                "role": "system",
                "content": WEB_RETRY_INSTRUCTION,
            })
            try:
                response = self._chat(
                    messages=retry_messages,
                    task_type="chat",
                    tools=self._tool_definitions,
                    think=False,
                )
                assistant_message = self._assistant_message(response)
            except Exception as error:
                print(f"\nIA : Erreur pendant la nouvelle tentative web : {error}\n")
                return None
            messages = retry_messages
            tool_calls = assistant_message.get("tool_calls") or []
            has_web_tool = any(
                call.get("function", {}).get("name") in WEB_TOOL_NAMES
                for call in tool_calls
                if isinstance(call, dict)
            )
            if not has_web_tool:
                return self._store_answer(
                    user_input,
                    "Je n'ai pas pu effectuer une recherche web réelle.",
                )

        system_action_results = []
        action_plan_results = []
        web_results = []
        if tool_calls:
            if has_action_plan:
                tool_calls = [
                    call for call in tool_calls
                    if call.get("function", {}).get("name") == "execute_action_plan"
                ][:1]
            messages.append(assistant_message)

            for call in tool_calls:
                try:
                    function_name = call["function"]["name"]
                    arguments = call["function"].get("arguments", {})
                    if function_name == "execute_action_plan":
                        actions = (
                            arguments.get("actions")
                            if isinstance(arguments, dict)
                            else None
                        )
                        result = self._action_planner.start(actions)
                        action_plan_results.append(result)
                    elif function_name in SYSTEM_ACTION_TOOL_NAMES:
                        result = self._system_action_controller.request(
                            function_name,
                            arguments,
                        )
                        system_action_results.append(result)
                    else:
                        function_to_call = self._available_tools.get(function_name)

                        if function_to_call is None:
                            result = f"Outil inconnu : {function_name}"
                        else:
                            try:
                                result = function_to_call(**arguments)
                            except TypeError as error:
                                result = (
                                    f"Erreur d'arguments pour "
                                    f"{function_name} : {error}"
                                )
                            except Exception as error:
                                result = (
                                    f"Erreur pendant l'utilisation "
                                    f"de {function_name} : {error}"
                                )
                        if function_name in WEB_TOOL_NAMES and isinstance(result, dict):
                            web_results.append(result)

                    messages.append({
                        "role": "tool",
                        "tool_name": function_name,
                        "content": str(result),
                    })
                except Exception as error:
                    print(
                        f"\nIA : Erreur pendant l'appel d'un outil : {error}\n"
                    )

            try:
                response = self._chat(
                    messages=messages,
                    task_type="chat",
                    tools=self._tool_definitions,
                    think=False,
                )
                assistant_message = self._assistant_message(response)
            except Exception as error:
                print(
                    f"\nIA : Erreur après utilisation de l'outil : {error}\n"
                )
                return None

        if requires_action_plan and not action_plan_results:
            return self._store_answer(
                user_input,
                "Je n'ai pas pu exécuter réellement ce plan.",
            )

        if requires_system_action and not requires_action_plan and not system_action_results:
            return self._store_answer(
                user_input,
                "Je n'ai pas pu exécuter réellement cette action.",
            )

        if requires_web_tool and not web_results:
            return self._store_answer(
                user_input,
                "Je n'ai pas pu effectuer une recherche web réelle.",
            )

        content = assistant_message.get("content", "")
        answer = content.strip() if isinstance(content, str) else ""

        if action_plan_results:
            answer = action_plan_results[-1]["message"]

        if system_action_results:
            confirmations = [
                result for result in system_action_results
                if result.get("requires_confirmation")
            ]
            failures = [
                result for result in system_action_results
                if not result.get("success")
                and not result.get("requires_confirmation")
            ]
            if confirmations:
                answer = "\n".join(result["message"] for result in confirmations)
            elif failures:
                answer = "\n".join(result["message"] for result in failures)
            else:
                answer = "\n".join(
                    result["message"] for result in system_action_results
                )

        if web_results and not any(result.get("success") for result in web_results):
            answer = "\n".join(
                result.get("error", "La requête web a échoué.")
                for result in web_results
            )

        if not answer:
            answer = "J'ai exécuté l'action, mais je n'ai pas généré de réponse."

        return self._store_answer(user_input, answer)
