"""Thread-safe authoritative state shared by conversation and cockpit routes."""

from threading import Lock

from .schemas import NovaState, StateResponse


_GOAL_PRESENTATION: dict[str, StateResponse] = {
    "pending": StateResponse(state="thinking", label="Préparation", busy=True, message="Nova prépare l’objectif."),
    "running": StateResponse(state="acting", label="Action en cours", busy=True, message="Nova exécute l’objectif."),
    "acting": StateResponse(state="acting", label="Action en cours", busy=True, message="Nova exécute l’objectif."),
    "verifying": StateResponse(state="acting", label="Vérification", busy=True, message="Nova vérifie le résultat."),
    "awaiting_confirmation": StateResponse(state="awaiting-confirmation", label="Confirmation requise", busy=False, message="Nova attend votre confirmation."),
    "paused": StateResponse(state="idle", label="En pause", busy=False, message="L’objectif est en pause."),
    "completed_verified": StateResponse(state="success", label="Objectif vérifié", busy=False, message="L’objectif est terminé et vérifié."),
    "completed_unverified": StateResponse(state="error", label="Vérification incomplète", busy=False, message="L’objectif est terminé sans vérification complète."),
    "blocked": StateResponse(state="error", label="Objectif bloqué", busy=False, message="L’objectif nécessite une intervention."),
    "failed": StateResponse(state="error", label="Échec de l’objectif", busy=False, message="L’objectif a échoué."),
    "cancelled": StateResponse(state="idle", label="Objectif annulé", busy=False, message="L’objectif a été annulé."),
}


class ApiStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._value = StateResponse(state="idle", label="Disponible", busy=False, message="Nova est disponible.")

    def set(self, state: NovaState, label: str, message: str, *, busy: bool) -> None:
        with self._lock:
            self._value = StateResponse(state=state, label=label, busy=busy, message=message)

    def snapshot(self) -> StateResponse:
        with self._lock:
            return self._value.model_copy()

    def set_goal_status(self, status: str) -> None:
        """Project an authoritative goal status onto the shared presentation state."""
        presentation = _GOAL_PRESENTATION.get(status)
        if presentation is None:
            return
        with self._lock:
            self._value = presentation.model_copy()
