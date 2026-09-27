"""Confirmation différée des actions système sensibles."""

from system_actions import (
    copy_file,
    create_folder,
    create_text_file,
    delete_file,
    delete_folder,
    move_file,
    open_application,
    open_path,
)


SYSTEM_ACTIONS = {
    "open_application": open_application,
    "open_path": open_path,
    "create_folder": create_folder,
    "create_text_file": create_text_file,
    "copy_file": copy_file,
    "move_file": move_file,
    "delete_file": delete_file,
    "delete_folder": delete_folder,
}


class SystemActionController:
    """Exécute les actions sûres et mémorise celles qui exigent confirmation."""

    CONFIRMATIONS = {
        "oui",
        "confirme",
        "confirmer",
        "vas-y",
        "vas y",
        "euh confirme",
        "euh oui",
        "euh stp confirme là",
        "euh stp oui là",
        "euh vas y",
    }
    CANCELLATIONS = {
        "non",
        "annule",
        "annuler",
        "refuse",
    }

    def __init__(self, actions=None):
        self.pending_system_action = None
        self._actions = actions or SYSTEM_ACTIONS

    def request(self, action_name, arguments=None):
        """Exécute une action ou la place en attente si elle est sensible."""
        action = self._actions.get(action_name)
        if action is None:
            return {
                "success": False,
                "message": f"Action système inconnue : {action_name}",
                "requires_confirmation": False,
                "action_level": "FORBIDDEN",
            }

        arguments = dict(arguments or {})
        arguments.pop("confirmed", None)
        try:
            result = action(**arguments)
        except TypeError as error:
            return {
                "success": False,
                "message": f"Arguments invalides pour {action_name} : {error}",
                "requires_confirmation": False,
                "action_level": "SAFE",
            }
        except Exception as error:
            return {
                "success": False,
                "message": f"Erreur pendant l'action {action_name} : {error}",
                "requires_confirmation": False,
                "action_level": "SAFE",
            }

        if result.get("requires_confirmation"):
            self.pending_system_action = {
                "action_name": action_name,
                "arguments": arguments,
            }
        return result

    def handle_confirmation(self, user_input):
        """Confirme ou annule l'action sensible actuellement en attente."""
        if self.pending_system_action is None:
            return {
                "handled": False,
                "response": None,
            }

        answer = user_input.lower()
        if answer in self.CANCELLATIONS:
            self.pending_system_action = None
            return {
                "handled": True,
                "response": "Action système annulée. Aucune modification effectuée.",
            }

        if answer not in self.CONFIRMATIONS:
            return {
                "handled": False,
                "response": None,
            }

        pending = self.pending_system_action
        self.pending_system_action = None
        action = self._actions[pending["action_name"]]
        arguments = dict(pending["arguments"])
        arguments["confirmed"] = True

        try:
            result = action(**arguments)
        except Exception as error:
            result = {
                "success": False,
                "message": f"Impossible d'exécuter l'action confirmée : {error}",
            }

        return {
            "handled": True,
            "response": result.get("message", str(result)),
            "result": result,
        }
