"""Couche d'application commune entre une interface et le moteur de l'assistant."""

from dataclasses import dataclass
from pathlib import Path
import threading

from action_planner import ActionPlanner
from bug_report_controller import BugReportController, BugReportDraft
from command_router import handle_direct_command
from conversation_manager import ConversationManager
from document_command_router import handle_document_command
from document_source import (
    DocumentSource,
    DocumentSourceError,
    resolve_document_source,
)
from document_tools import MAX_SOURCE_BYTES, PROJECT_ROOT, SUPPORTED_EXTENSIONS
from edit_controller import EditController
from memory import init_db
from smart_memory import init_smart_memory
from system_action_controller import SystemActionController
from self_improvement.real_bug_corpus import BugCorpusError


@dataclass(frozen=True)
class ConfirmationRequest:
    kind: str
    title: str
    description: str


@dataclass(frozen=True)
class ControllerResult:
    success: bool
    message: str
    kind: str = "message"
    error_code: str | None = None
    confirmation: ConfirmationRequest | None = None
    attachment: dict | None = None


class AppController:
    """Orchestre les modules existants sans exposer leurs détails à la GUI."""

    def __init__(
        self,
        *,
        conversation_manager=None,
        edit_controller=None,
        system_action_controller=None,
        action_planner=None,
        direct_command_handler=handle_direct_command,
        document_command_handler=handle_document_command,
        bug_report_controller=None,
        initialize_databases=True,
    ):
        if initialize_databases:
            init_db()
            init_smart_memory()

        self._system_controller = system_action_controller or SystemActionController()
        self._action_planner = action_planner or ActionPlanner(self._system_controller)
        self._conversation_manager = conversation_manager or ConversationManager(
            system_action_controller=self._system_controller,
            action_planner=self._action_planner,
        )
        self._edit_controller = edit_controller or EditController()
        self._direct_command_handler = direct_command_handler
        self._document_command_handler = document_command_handler
        self._bug_reports = bug_report_controller or BugReportController()
        self._active_attachment = None
        self._attachment_metadata = None
        self._state_lock = threading.Lock()
        self._busy = False
        self._status_message = ""

    def _begin_operation(self, status_message):
        with self._state_lock:
            if self._busy:
                return False
            self._busy = True
            self._status_message = status_message
            return True

    def _finish_operation(self):
        with self._state_lock:
            self._busy = False
            self._status_message = ""

    def _document_status(self, status):
        messages = {
            "ocr_running": "OCR en cours...",
        }
        with self._state_lock:
            if self._busy:
                self._status_message = messages.get(status, "Analyse du document en cours...")

    @staticmethod
    def _busy_result():
        return ControllerResult(
            False,
            "Assistant en cours de traitement. Attendez la fin de la requête actuelle.",
            kind="error",
            error_code="BUSY",
        )

    def get_status(self):
        with self._state_lock:
            return {
                "busy": self._busy,
                "message": self._status_message,
                "has_attachment": self._active_attachment is not None,
            }

    def get_active_attachment(self):
        with self._state_lock:
            return dict(self._attachment_metadata) if self._attachment_metadata else None

    def prepare_bug_report(self, user_message, assistant_response) -> BugReportDraft:
        """Capture un snapshot borné correspondant exactement à une réponse affichée."""
        history = list(getattr(self._conversation_manager, "conversation", []) or [])
        if len(history) >= 2:
            tail = history[-2:]
            if (
                tail[0].get("role") == "user"
                and tail[0].get("content") == user_message
                and tail[1].get("role") == "assistant"
            ):
                history = history[:-2]
        context = (
            self._conversation_manager.get_session_context()
            if hasattr(self._conversation_manager, "get_session_context")
            else {}
        )
        with self._state_lock:
            attachment = dict(self._attachment_metadata) if self._attachment_metadata else None
        if attachment:
            context = {**context, "active_attachment": attachment}
        return self._bug_reports.prepare(
            user_message=user_message,
            assistant_response=assistant_response,
            history=history,
            session_context=context,
        )

    def submit_bug_report(self, draft, *, incorrect_comment, expected_behavior=""):
        """Enregistre uniquement après l'action explicite Enregistrer de la modale."""
        if not self._begin_operation("Enregistrement du signalement..."):
            return self._busy_result()
        try:
            case = self._bug_reports.submit(
                draft,
                incorrect_comment=incorrect_comment,
                expected_behavior=expected_behavior,
            )
            return ControllerResult(
                True,
                "Signalement enregistré. Il pourra être utilisé lors d'une future campagne d'amélioration.",
                kind="bug_report",
                attachment={"bug_id": case.id, "status": case.status},
            )
        except BugCorpusError as error:
            return ControllerResult(
                False, str(error), kind="error", error_code="BUG_REPORT_REJECTED"
            )
        except OSError as error:
            return ControllerResult(
                False,
                f"Impossible d'enregistrer le signalement : {error}",
                kind="error",
                error_code="BUG_REPORT_WRITE_ERROR",
            )
        finally:
            self._finish_operation()

    def list_new_bug_reports(self):
        return self._bug_reports.list_new()

    def get_bug_report_for_review(self, bug_id):
        return self._bug_reports.get_for_review(bug_id)

    def validate_review_bug_report(self, bug_id, criteria):
        if not self._begin_operation("Validation du signalement..."):
            return self._busy_result()
        try:
            case = self._bug_reports.validate_review(bug_id, criteria)
            return ControllerResult(
                True,
                "Signalement validé. Il pourra être utilisé lors d'une prochaine campagne.",
                kind="bug_review",
                attachment={"bug_id": case.id, "status": case.status},
            )
        except BugCorpusError as error:
            return ControllerResult(
                False, str(error), kind="error", error_code="BUG_REVIEW_REJECTED"
            )
        except OSError as error:
            return ControllerResult(
                False, f"Impossible de mettre à jour le corpus : {error}",
                kind="error", error_code="BUG_REVIEW_WRITE_ERROR",
            )
        finally:
            self._finish_operation()

    def reject_bug_report(self, bug_id):
        if not self._begin_operation("Rejet du signalement..."):
            return self._busy_result()
        try:
            case = self._bug_reports.reject(bug_id)
            return ControllerResult(
                True, "Signalement rejeté et conservé pour traçabilité.",
                kind="bug_review",
                attachment={"bug_id": case.id, "status": case.status},
            )
        except BugCorpusError as error:
            return ControllerResult(
                False, str(error), kind="error", error_code="BUG_REVIEW_REJECTED"
            )
        except OSError as error:
            return ControllerResult(
                False, f"Impossible de mettre à jour le corpus : {error}",
                kind="error", error_code="BUG_REVIEW_WRITE_ERROR",
            )
        finally:
            self._finish_operation()

    def validate_bug_report(self, bug_id, *, criteria, runner="interpreter"):
        return self._bug_reports.validate(bug_id, criteria=criteria, runner=runner)

    def advance_bug_report(self, bug_id, status):
        return self._bug_reports.advance(bug_id, status)

    def attach_document(self, path=None, *, name=None, content=None, reference=None):
        """Charge une pièce jointe bornée et remplace immédiatement l'ancienne."""
        if not self._begin_operation("Lecture de la pièce jointe..."):
            return self._busy_result()
        try:
            if content is not None or name is not None:
                source = DocumentSource.attachment(name or "", content, reference=reference)
                resolved = resolve_document_source(
                    source,
                    project_root=PROJECT_ROOT,
                    supported_extensions=SUPPORTED_EXTENSIONS,
                    max_source_bytes=MAX_SOURCE_BYTES,
                )
                attachment = source
            else:
                resolved = resolve_document_source(
                    path,
                    project_root=PROJECT_ROOT,
                    supported_extensions=SUPPORTED_EXTENSIONS,
                    max_source_bytes=MAX_SOURCE_BYTES,
                )
                try:
                    with resolved.open_binary() as stream:
                        document_bytes = stream.read(MAX_SOURCE_BYTES + 1)
                except OSError as error:
                    raise DocumentSourceError(
                        "READ_ERROR", f"Lecture de la pièce jointe impossible : {error}"
                    ) from error
                if len(document_bytes) > MAX_SOURCE_BYTES:
                    raise DocumentSourceError(
                        "FILE_TOO_LARGE",
                        f"Le document dépasse la limite de {MAX_SOURCE_BYTES // (1024 * 1024)} Mo.",
                    )
                attachment = DocumentSource.attachment(
                    resolved.name,
                    document_bytes,
                    reference=str(resolved.path),
                )

            metadata = {
                "name": resolved.name,
                "format": resolved.extension.lstrip("."),
                "size": resolved.size,
                "reference": attachment.reference,
            }
            with self._state_lock:
                self._active_attachment = attachment
                self._attachment_metadata = metadata
            if hasattr(self._conversation_manager, "set_active_attachment"):
                self._conversation_manager.set_active_attachment(metadata)
            return ControllerResult(
                True,
                f"Pièce jointe active : {resolved.name}",
                kind="attachment",
                attachment=dict(metadata),
            )
        except DocumentSourceError as error:
            return ControllerResult(
                False,
                error.message,
                kind="error",
                error_code=error.code,
            )
        except Exception as error:
            return ControllerResult(
                False,
                f"Impossible de joindre le document : {error}",
                kind="error",
                error_code="ATTACHMENT_ERROR",
            )
        finally:
            self._finish_operation()

    def clear_active_attachment(self):
        with self._state_lock:
            if self._busy:
                return self._busy_result()
            self._active_attachment = None
            self._attachment_metadata = None
        if hasattr(self._conversation_manager, "set_active_attachment"):
            self._conversation_manager.set_active_attachment(None)
        return ControllerResult(True, "Pièce jointe retirée.", kind="attachment")

    def _pending_confirmation(self):
        pending_edit = getattr(self._edit_controller, "pending_edit", None)
        if pending_edit is not None:
            return ConfirmationRequest(
                "edit",
                "Confirmer la modification ?",
                f"Appliquer la modification proposée à {pending_edit.get('path', 'ce fichier')} ?",
            )

        if getattr(self._action_planner, "waiting_confirmation", False):
            step = getattr(self._action_planner, "pending_step", None) or {}
            return ConfirmationRequest(
                "plan",
                "Confirmer cette étape ?",
                f"{step.get('action', 'Action système')} : {step.get('arguments', {})}",
            )

        pending_system = getattr(self._system_controller, "pending_system_action", None)
        if pending_system is not None:
            return ConfirmationRequest(
                "system",
                "Confirmer cette action ?",
                f"{pending_system.get('action_name', 'Action système')} : "
                f"{pending_system.get('arguments', {})}",
            )
        return None

    def get_pending_confirmation(self):
        return self._pending_confirmation()

    def _with_pending(self, result):
        pending = self._pending_confirmation()
        if pending is None:
            return result
        return ControllerResult(
            result.success,
            result.message,
            kind="confirmation",
            error_code=result.error_code,
            confirmation=pending,
            attachment=result.attachment,
        )

    def send_message(self, text):
        """Traite un message via les mêmes contrôleurs que le moteur CLI."""
        if not isinstance(text, str) or not text.strip():
            return ControllerResult(
                False,
                "Le message est vide.",
                kind="error",
                error_code="EMPTY_MESSAGE",
            )
        if not self._begin_operation("Assistant en cours de traitement..."):
            return self._busy_result()

        user_input = text.strip()
        try:
            pending = self._pending_confirmation()
            if pending is not None:
                return ControllerResult(
                    False,
                    "Une action attend déjà votre confirmation.",
                    kind="confirmation",
                    error_code="CONFIRMATION_REQUIRED",
                    confirmation=pending,
                )

            with self._state_lock:
                attachment = self._active_attachment
            document_result = self._document_command_handler(
                user_input,
                active_attachment=attachment,
                status_callback=self._document_status,
            )
            if document_result.get("handled"):
                message = document_result.get("response") or "Aucune réponse documentaire."
                structured = document_result.get("document_result") or {}
                success = bool(structured.get("success"))
                if hasattr(self._conversation_manager, "add_exchange"):
                    self._conversation_manager.add_exchange(user_input, message)
                return ControllerResult(
                    success,
                    message,
                    kind="message" if success else "error",
                    error_code=(structured.get("error") or {}).get("code"),
                )

            direct_result = self._direct_command_handler(user_input)
            if direct_result.get("handled"):
                message = direct_result.get("response") or "Commande terminée."
                conversation_response = direct_result.get("conversation_response")
                if conversation_response is not None and hasattr(
                    self._conversation_manager, "add_exchange"
                ):
                    self._conversation_manager.add_exchange(user_input, conversation_response)
                return self._with_pending(ControllerResult(True, message))

            edit_result = self._edit_controller.handle(user_input)
            if edit_result.get("handled"):
                message = edit_result.get("response") or "Demande de modification traitée."
                return self._with_pending(ControllerResult(True, str(message)))

            answer = self._conversation_manager.handle(user_input)
            if answer is None:
                return ControllerResult(
                    False,
                    "Le moteur de l'assistant est indisponible. Vérifiez Ollama et les modèles.",
                    kind="error",
                    error_code="BACKEND_UNAVAILABLE",
                )
            context_error = (
                self._conversation_manager.get_last_context_error_code()
                if hasattr(self._conversation_manager, "get_last_context_error_code")
                else None
            )
            missing_attachment = context_error == "NO_ACTIVE_ATTACHMENT"
            return self._with_pending(ControllerResult(
                not missing_attachment,
                str(answer),
                kind="error" if missing_attachment else "message",
                error_code=context_error if missing_attachment else None,
            ))
        except Exception as error:
            return ControllerResult(
                False,
                f"Une erreur du moteur a été interceptée : {error}",
                kind="error",
                error_code="BACKEND_ERROR",
            )
        finally:
            self._finish_operation()

    def _resolve_confirmation(self, answer):
        if not self._begin_operation("Traitement de la confirmation..."):
            return self._busy_result()
        try:
            pending = self._pending_confirmation()
            if pending is None:
                return ControllerResult(
                    False,
                    "Aucune action n'attend de confirmation.",
                    kind="error",
                    error_code="NO_PENDING_CONFIRMATION",
                )

            if pending.kind == "edit":
                result = self._edit_controller.handle_confirmation(answer)
                message = result.get("response") or "Modification traitée."
                success = bool(result.get("handled"))
            else:
                result = self._conversation_manager.handle_system_confirmation(answer)
                message = result.get("response") or "Action traitée."
                nested = result.get("result", result.get("plan_result"))
                success = bool(result.get("handled"))
                if answer == "oui" and isinstance(nested, dict):
                    success = success and bool(nested.get("success"))
            return self._with_pending(
                ControllerResult(
                    success,
                    str(message),
                    kind="message" if success else "error",
                    error_code=None if success else "ACTION_FAILED",
                )
            )
        except Exception as error:
            return ControllerResult(
                False,
                f"Impossible de traiter la confirmation : {error}",
                kind="error",
                error_code="CONFIRMATION_ERROR",
            )
        finally:
            self._finish_operation()

    def confirm_pending_action(self):
        return self._resolve_confirmation("oui")

    def cancel_pending_action(self):
        return self._resolve_confirmation("non")
