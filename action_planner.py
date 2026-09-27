"""Validation et exécution séquentielle de plans d'actions système."""

import re

from system_action_controller import SystemActionController


RUNNING = "RUNNING"
WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

ALLOWED_PLAN_ACTIONS = {
    "open_application": {"required": {"name"}, "optional": set()},
    "open_path": {"required": {"path"}, "optional": set()},
    "create_folder": {"required": {"path"}, "optional": set()},
    "create_text_file": {"required": {"path"}, "optional": set()},
    "copy_file": {"required": {"source", "destination"}, "optional": set()},
    "move_file": {"required": {"source", "destination"}, "optional": set()},
    "delete_file": {"required": {"path"}, "optional": set()},
    "delete_folder": {"required": {"path"}, "optional": set()},
}


class ActionPlanner:
    """Exécute au plus dix actions autorisées, dans leur ordre exact."""

    MAX_STEPS = 10

    def __init__(self, system_action_controller=None):
        self.controller = system_action_controller or SystemActionController()
        self.actions = []
        self.current_index = 0
        self.results = []
        self.pending_step = None
        self.state = None

    @property
    def waiting_confirmation(self):
        return self.state == WAITING_CONFIRMATION

    def _response(self, message, success=None):
        if success is None:
            success = self.state == COMPLETED
        return {
            "success": success,
            "message": message,
            "requires_confirmation": self.state == WAITING_CONFIRMATION,
            "state": self.state,
            "current_index": self.current_index,
            "results": list(self.results),
        }

    def _fail(self, message):
        self.state = FAILED
        self.pending_step = None
        return self._response(message, success=False)

    def _validate_plan(self, actions):
        if not isinstance(actions, list):
            return "Le plan doit contenir une liste d'actions."
        if not actions:
            return "Le plan d'actions est vide."
        if len(actions) > self.MAX_STEPS:
            return f"Le plan dépasse la limite de {self.MAX_STEPS} étapes."

        for index, step in enumerate(actions, start=1):
            if not isinstance(step, dict):
                return f"Étape {index} invalide : un objet est attendu."
            if set(step) != {"action", "arguments"}:
                return f"Étape {index} invalide : champs attendus action et arguments."
            action_name = step["action"]
            arguments = step["arguments"]
            specification = ALLOWED_PLAN_ACTIONS.get(action_name)
            if specification is None:
                return f"Étape {index} refusée : action non autorisée {action_name}."
            if not isinstance(arguments, dict):
                return f"Étape {index} invalide : arguments doit être un dictionnaire."
            argument_names = set(arguments)
            allowed_names = specification["required"] | specification["optional"]
            missing = specification["required"] - argument_names
            unknown = argument_names - allowed_names
            if missing:
                return f"Étape {index} invalide : arguments manquants {sorted(missing)}."
            if unknown:
                return f"Étape {index} invalide : paramètres inconnus {sorted(unknown)}."
            for argument_name, argument_value in arguments.items():
                if not isinstance(argument_value, str) or not argument_value.strip():
                    return (
                        f"Étape {index} invalide : {argument_name} doit être "
                        "une chaîne non vide."
                    )
        return None

    @staticmethod
    def _resolve_reference(value, last_folder, last_file):
        cleaned = value.strip()
        lowered = cleaned.casefold()
        if lowered in {"ce fichier", "le fichier", "ouvre-le", "ouvre le", "le"}:
            return last_file
        if lowered in {"ce dossier", "le dossier", "ouvre-la", "ouvre la", "la"}:
            return last_folder
        if re.search(r"\bdedans\b", cleaned, flags=re.IGNORECASE):
            if not last_folder:
                return None
            return re.sub(
                r"\bdedans\b",
                f"dans {last_folder}",
                cleaned,
                flags=re.IGNORECASE,
            )
        return cleaned

    def _resolve_step_references(self, actions):
        resolved_actions = []
        last_folder = None
        last_file = None

        for index, step in enumerate(actions, start=1):
            arguments = {}
            for name, value in step["arguments"].items():
                resolved = self._resolve_reference(value, last_folder, last_file)
                if resolved is None:
                    return None, (
                        f"Étape {index} invalide : la référence {value!r} "
                        "ne peut pas être résolue."
                    )
                arguments[name] = resolved

            resolved_step = {
                "action": step["action"],
                "arguments": arguments,
            }
            resolved_actions.append(resolved_step)

            if step["action"] == "create_folder":
                last_folder = arguments["path"]
            elif step["action"] == "create_text_file":
                last_file = arguments["path"]
            elif step["action"] == "open_path":
                path = arguments["path"]
                if last_file and path == last_file:
                    last_file = path
                elif last_folder and path == last_folder:
                    last_folder = path

        return resolved_actions, None

    def start(self, actions):
        """Valide un nouveau plan puis l'exécute jusqu'à arrêt ou confirmation."""
        if self.controller.pending_system_action is not None:
            return self._fail(
                "Impossible de démarrer un plan : une action système attend déjà une confirmation."
            )

        validation_error = self._validate_plan(actions)
        if validation_error:
            return self._fail(validation_error)

        resolved_actions, reference_error = self._resolve_step_references(actions)
        if reference_error:
            return self._fail(reference_error)

        self.actions = resolved_actions
        self.current_index = 0
        self.results = []
        self.pending_step = None
        self.state = RUNNING
        return self._run()

    def _run(self):
        while self.current_index < len(self.actions):
            step = self.actions[self.current_index]
            print(
                f"[Planner] Étape {self.current_index + 1}/{len(self.actions)} : "
                f"{step['action']}"
            )
            result = self.controller.request(step["action"], step["arguments"])

            if result.get("requires_confirmation"):
                self.pending_step = {
                    "index": self.current_index,
                    "action": step["action"],
                    "arguments": dict(step["arguments"]),
                }
                self.state = WAITING_CONFIRMATION
                return self._response(result["message"], success=False)

            self.results.append({
                "index": self.current_index,
                "action": step["action"],
                "arguments": dict(step["arguments"]),
                "result": result,
            })
            if not result.get("success"):
                return self._fail(result.get("message", "Une étape du plan a échoué."))
            self.current_index += 1

        self.state = COMPLETED
        summary = "Plan terminé :\n" + "\n".join(
            f"- {entry['result']['message']}" for entry in self.results
        )
        return self._response(summary, success=True)

    def handle_confirmation(self, user_input):
        """Confirme l'étape suspendue puis reprend, ou annule tout le plan."""
        if not self.waiting_confirmation:
            return {"handled": False, "response": None}

        confirmation = self.controller.handle_confirmation(user_input)
        if not confirmation.get("handled"):
            return {"handled": False, "response": None}

        if "result" not in confirmation:
            self.state = CANCELLED
            self.pending_step = None
            return {
                "handled": True,
                "response": "Plan annulé. Aucune étape restante n'a été exécutée.",
                "plan_result": self._response("Plan annulé.", success=False),
            }

        result = confirmation["result"]
        step = self.pending_step
        self.pending_step = None
        self.results.append({
            "index": self.current_index,
            "action": step["action"],
            "arguments": dict(step["arguments"]),
            "result": result,
        })
        if not result.get("success"):
            self.state = FAILED
            return {
                "handled": True,
                "response": result.get("message", "L'action confirmée a échoué."),
                "plan_result": self._response(
                    result.get("message", "L'action confirmée a échoué."),
                    success=False,
                ),
            }

        self.current_index += 1
        self.state = RUNNING
        resumed = self._run()
        return {
            "handled": True,
            "response": resumed["message"],
            "plan_result": resumed,
        }
