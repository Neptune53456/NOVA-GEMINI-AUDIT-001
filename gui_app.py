"""Interface graphique Tkinter de l'assistant local.

Ce module ne crée aucune fenêtre à l'import. Toutes les opérations métier passent
par :class:`app_controller.AppController` et s'exécutent hors du thread Tkinter.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from app_controller import AppController, ControllerResult


BACKGROUND = "#15181d"
PANEL = "#20242b"
INPUT_BACKGROUND = "#292e37"
TEXT = "#f2f3f5"
MUTED = "#a9b0bc"
ACCENT = "#4f8cff"
USER = "#7fb3ff"
ASSISTANT = "#d8dde7"
ERROR = "#ff8b8b"


class AssistantGUI:
    """Fenêtre de conversation, sans logique propre au moteur de l'assistant."""

    POLL_INTERVAL_MS = 100

    def __init__(self, root: tk.Tk, controller: AppController):
        self.root = root
        self.controller = controller
        self._results: queue.Queue[tuple[str, ControllerResult]] = queue.Queue()
        self._ui_busy = False
        self._confirmation_window: tk.Toplevel | None = None
        self._report_window: tk.Toplevel | None = None
        self._review_window: tk.Toplevel | None = None
        self._pending_user_message: str | None = None

        self._build_window()
        self._build_conversation()
        self._build_attachment_bar()
        self._build_composer()
        self._refresh_attachment()
        self._refresh_review_count()

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(self.POLL_INTERVAL_MS, self._poll_results)

    def _build_window(self):
        self.root.title("Assistant IA local")
        self.root.geometry("900x680")
        self.root.minsize(620, 460)
        self.root.configure(background=BACKGROUND)

        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

    def _build_conversation(self):
        frame = tk.Frame(self.root, background=BACKGROUND)
        frame.grid(row=0, column=0, sticky="nsew", padx=18, pady=(18, 8))
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        scrollbar = tk.Scrollbar(frame)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.conversation = tk.Text(
            frame,
            wrap="word",
            state="disabled",
            background=PANEL,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            padx=16,
            pady=14,
            font=("Segoe UI", 10),
            yscrollcommand=scrollbar.set,
        )
        self.conversation.grid(row=0, column=0, sticky="nsew")
        scrollbar.configure(command=self.conversation.yview)

        self.conversation.tag_configure("user_label", foreground=USER, font=("Segoe UI", 10, "bold"))
        self.conversation.tag_configure("assistant_label", foreground=ASSISTANT, font=("Segoe UI", 10, "bold"))
        self.conversation.tag_configure("error_label", foreground=ERROR, font=("Segoe UI", 10, "bold"))
        self.conversation.tag_configure("user", foreground=TEXT, spacing3=12)
        self.conversation.tag_configure("assistant", foreground=ASSISTANT, spacing3=12)
        self.conversation.tag_configure("error", foreground=ERROR, spacing3=12)

    def _build_attachment_bar(self):
        self.attachment_frame = tk.Frame(self.root, background=PANEL, padx=10, pady=7)
        self.attachment_frame.grid(row=1, column=0, sticky="ew", padx=18, pady=4)
        self.attachment_frame.grid_columnconfigure(0, weight=1)

        self.attachment_label = tk.Label(
            self.attachment_frame,
            text="Aucune pièce jointe",
            anchor="w",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        self.attachment_label.grid(row=0, column=0, sticky="ew")
        self.remove_attachment_button = tk.Button(
            self.attachment_frame,
            text="Retirer",
            command=self._remove_attachment,
            background="#343a45",
            foreground=TEXT,
            activebackground="#454d5b",
            activeforeground=TEXT,
            relief="flat",
            padx=10,
        )
        self.remove_attachment_button.grid(row=0, column=1, padx=(8, 0))
        self.review_button = tk.Button(
            self.attachment_frame,
            text="Signalements à revoir",
            command=self._show_bug_reviews,
            background="#343a45", foreground=TEXT,
            activebackground="#454d5b", activeforeground=TEXT,
            relief="flat", padx=10,
        )
        self.review_button.grid(row=0, column=2, padx=(8, 0))

    def _build_composer(self):
        frame = tk.Frame(self.root, background=BACKGROUND)
        frame.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 8))
        frame.grid_columnconfigure(0, weight=1)

        self.message_input = tk.Text(
            frame,
            height=4,
            wrap="word",
            background=INPUT_BACKGROUND,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            padx=10,
            pady=8,
            font=("Segoe UI", 10),
        )
        self.message_input.grid(row=0, column=0, rowspan=2, sticky="ew")
        self.message_input.bind("<Return>", self._on_return)

        self.send_button = tk.Button(
            frame,
            text="Envoyer",
            command=self._send_message,
            background=ACCENT,
            foreground="white",
            activebackground="#6a9dff",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=7,
        )
        self.send_button.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=(0, 5))

        self.attach_button = tk.Button(
            frame,
            text="Joindre…",
            command=self._choose_attachment,
            background="#343a45",
            foreground=TEXT,
            activebackground="#454d5b",
            activeforeground=TEXT,
            relief="flat",
            padx=18,
            pady=7,
        )
        self.attach_button.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(5, 0))

        self.status_label = tk.Label(
            self.root,
            text="Prêt",
            anchor="w",
            background=BACKGROUND,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        self.status_label.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))
        self.message_input.focus_set()

    def _on_return(self, event):
        if event.state & 0x0001:  # Maj+Entrée conserve un saut de ligne.
            return None
        self._send_message()
        return "break"

    def _append_message(self, role, message):
        labels = {
            "user": ("Vous", "user_label", "user"),
            "assistant": ("Assistant", "assistant_label", "assistant"),
            "error": ("Erreur", "error_label", "error"),
        }
        label, label_tag, body_tag = labels[role]
        self.conversation.configure(state="normal")
        self.conversation.insert("end", f"{label}\n", label_tag)
        self.conversation.insert("end", f"{message}\n\n", body_tag)
        self.conversation.configure(state="disabled")
        self.conversation.see("end")

    def _append_report_button(self, user_message, assistant_response):
        """Ajoute une action liée au couple exact question/réponse affiché."""
        try:
            draft = self.controller.prepare_bug_report(user_message, assistant_response)
        except Exception as error:
            self.status_label.configure(text=f"Signalement indisponible : {error}")
            return
        self.conversation.configure(state="normal")
        button = tk.Button(
            self.conversation,
            text="👎 Signaler cette réponse",
            command=lambda captured=draft: self._show_bug_report(captured),
            background="#343a45",
            foreground=MUTED,
            activebackground="#454d5b",
            activeforeground=TEXT,
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        )
        self.conversation.window_create("end", window=button)
        self.conversation.insert("end", "\n\n")
        self.conversation.configure(state="disabled")
        self.conversation.see("end")

    def _set_busy(self, busy, status=""):
        self._ui_busy = busy
        state = "disabled" if busy else "normal"
        self.send_button.configure(state=state)
        self.attach_button.configure(state=state)
        self.message_input.configure(state=state)
        self._refresh_attachment()
        self.status_label.configure(text=status or ("Traitement en cours…" if busy else "Prêt"))
        if not busy:
            self.message_input.focus_set()

    def _start_operation(self, operation, status, callback):
        if self._ui_busy:
            self.status_label.configure(text="Une requête est déjà en cours.")
            return False
        self._set_busy(True, status)

        def run():
            try:
                result = callback()
            except Exception as error:  # Dernier rempart : aucune erreur moteur ne ferme la GUI.
                result = ControllerResult(
                    False,
                    f"Une erreur inattendue a été interceptée : {error}",
                    kind="error",
                    error_code="GUI_WORKER_ERROR",
                )
            self._results.put((operation, result))

        threading.Thread(target=run, name=f"assistant-{operation}", daemon=True).start()
        return True

    def _send_message(self):
        text = self.message_input.get("1.0", "end-1c").strip()
        if not text:
            self.status_label.configure(text="Saisissez un message.")
            return
        if self._start_operation("message", "Assistant en cours de traitement…", lambda: self.controller.send_message(text)):
            self._pending_user_message = text
            self.message_input.configure(state="normal")
            self.message_input.delete("1.0", "end")
            self.message_input.configure(state="disabled")
            self._append_message("user", text)

    def _choose_attachment(self):
        if self._ui_busy:
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Joindre un document",
            filetypes=(
                ("Documents pris en charge", "*.pdf *.txt *.md *.docx"),
                ("PDF", "*.pdf"),
                ("Texte et Markdown", "*.txt *.md"),
                ("Word", "*.docx"),
            ),
        )
        if path:
            self._start_operation(
                "attachment",
                "Lecture de la pièce jointe…",
                lambda: self.controller.attach_document(path),
            )

    def _remove_attachment(self):
        self._start_operation(
            "attachment",
            "Retrait de la pièce jointe…",
            self.controller.clear_active_attachment,
        )

    def _refresh_attachment(self):
        attachment = self.controller.get_active_attachment()
        if attachment:
            size_kb = max(1, (attachment["size"] + 1023) // 1024)
            self.attachment_label.configure(
                text=f"Pièce jointe : {attachment['name']} · {attachment['format'].upper()} · {size_kb} Ko",
                foreground=TEXT,
            )
        else:
            self.attachment_label.configure(text="Aucune pièce jointe", foreground=MUTED)
        self.remove_attachment_button.configure(
            state="normal" if attachment and not self._ui_busy else "disabled"
        )

    def _poll_results(self):
        if self._ui_busy:
            status = self.controller.get_status()
            if status.get("message"):
                self.status_label.configure(text=status["message"])

        try:
            while True:
                operation, result = self._results.get_nowait()
                self._handle_result(operation, result)
        except queue.Empty:
            pass

        try:
            self.root.after(self.POLL_INTERVAL_MS, self._poll_results)
        except tk.TclError:
            pass

    def _handle_result(self, operation, result):
        self._set_busy(False)
        self._refresh_attachment()

        if operation == "attachment":
            self.status_label.configure(text=result.message if result.success else "Échec de la pièce jointe.")
            if not result.success:
                self._append_message("error", result.message)
        elif operation == "bug_report":
            self.status_label.configure(text=result.message)
            if result.success:
                self._refresh_review_count()
        elif operation.startswith("bug_review"):
            self.status_label.configure(text=result.message)
            self._reload_bug_reviews()
        else:
            self._append_message("assistant" if result.success else "error", result.message)
            if operation == "message":
                user_message = self._pending_user_message
                self._pending_user_message = None
                if user_message:
                    self._append_report_button(user_message, result.message)

        if result.confirmation is not None:
            self._show_confirmation(result.confirmation)

    def _show_bug_report(self, draft):
        if self._ui_busy:
            self.status_label.configure(text="Attendez la fin de la requête en cours avant de signaler.")
            return
        if self._report_window is not None:
            self._report_window.focus_set()
            return
        window = tk.Toplevel(self.root)
        self._report_window = window
        window.title("Signaler cette réponse")
        window.configure(background=PANEL)
        window.geometry("680x650")
        window.minsize(520, 520)
        window.transient(self.root)
        window.grab_set()
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)

        title = tk.Label(
            window, text="Signaler cette réponse", background=PANEL, foreground=TEXT,
            font=("Segoe UI", 13, "bold"), anchor="w", padx=18, pady=14,
        )
        title.grid(row=0, column=0, sticky="ew")

        preview = tk.Text(
            window, height=12, wrap="word", background=INPUT_BACKGROUND,
            foreground=TEXT, relief="flat", padx=10, pady=8, font=("Segoe UI", 9),
        )
        preview.grid(row=1, column=0, sticky="nsew", padx=18)
        attachment = draft.active_attachment or "Aucune"
        recent = "\n".join(
            f"{item['role']}: {item['content']}" for item in draft.conversation_history
        ) or "Aucun historique antérieur nécessaire."
        preview.insert(
            "1.0",
            f"Message utilisateur\n{draft.user_message}\n\n"
            f"Réponse assistant\n{draft.assistant_response}\n\n"
            f"Contexte récent\n{recent}\n\n"
            f"Pièce jointe active\n{attachment}\n\n"
            f"Contexte de session\n{draft.session_context}",
        )
        preview.configure(state="disabled")

        form = tk.Frame(window, background=PANEL)
        form.grid(row=2, column=0, sticky="ew", padx=18, pady=(12, 0))
        form.grid_columnconfigure(0, weight=1)
        tk.Label(
            form, text="Qu'est-ce qui était incorrect ? *", background=PANEL,
            foreground=TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        incorrect_input = tk.Text(
            form, height=3, wrap="word", background=INPUT_BACKGROUND,
            foreground=TEXT, insertbackground=TEXT, relief="flat", padx=8, pady=6,
        )
        incorrect_input.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        tk.Label(
            form, text="Quel comportement attendais-tu ? (facultatif)",
            background=PANEL, foreground=TEXT, anchor="w",
        ).grid(row=2, column=0, sticky="ew")
        expected_input = tk.Text(
            form, height=3, wrap="word", background=INPUT_BACKGROUND,
            foreground=TEXT, insertbackground=TEXT, relief="flat", padx=8, pady=6,
        )
        expected_input.grid(row=3, column=0, sticky="ew", pady=(4, 6))
        validation_label = tk.Label(
            form, text="", background=PANEL, foreground=ERROR, anchor="w",
        )
        validation_label.grid(row=4, column=0, sticky="ew")

        buttons = tk.Frame(window, background=PANEL)
        buttons.grid(row=3, column=0, sticky="e", padx=18, pady=16)

        def save():
            comment = incorrect_input.get("1.0", "end-1c").strip()
            expected = expected_input.get("1.0", "end-1c").strip()
            if not comment:
                validation_label.configure(text="Expliquez ce qui était incorrect.")
                incorrect_input.focus_set()
                return
            self._close_bug_report()
            self._start_operation(
                "bug_report",
                "Enregistrement du signalement…",
                lambda: self.controller.submit_bug_report(
                    draft, incorrect_comment=comment, expected_behavior=expected
                ),
            )

        tk.Button(
            buttons, text="Annuler", command=self._close_bug_report,
            background="#343a45", foreground=TEXT, relief="flat", padx=14, pady=6,
        ).grid(row=0, column=0, padx=(0, 8))
        tk.Button(
            buttons, text="Enregistrer", command=save,
            background=ACCENT, foreground="white", relief="flat", padx=14, pady=6,
        ).grid(row=0, column=1)
        window.protocol("WM_DELETE_WINDOW", self._close_bug_report)
        window.bind("<Escape>", lambda _event: self._close_bug_report())
        incorrect_input.focus_set()

    def _close_bug_report(self):
        if self._report_window is not None:
            try:
                self._report_window.grab_release()
            except tk.TclError:
                pass
            self._report_window.destroy()
            self._report_window = None

    @staticmethod
    def _bug_review_detail(case):
        history = "\n".join(
            f"{item['role']}: {item['content']}" for item in case.conversation_history
        ) or "Aucun contexte récent enregistré."
        context = case.session_context
        return (
            f"Identifiant : {case.id}\nDate : {case.created_at}\n"
            f"Catégorie : {case.category}\nStatut : {case.status}\n\n"
            f"Message utilisateur\n{case.user_message}\n\n"
            f"Réponse incorrecte\n{case.incorrect_response}\n\n"
            f"Commentaire\n{case.expected_behavior.get('user_comment', '')}\n\n"
            f"Comportement attendu\n{case.expected_behavior.get('description', '')}\n\n"
            f"Conversation récente\n{history}\n\n"
            f"Pièce jointe\n{context.get('active_attachment', 'Aucune')}\n\n"
            f"last_file : {context.get('last_file', '')}\n"
            f"last_folder : {context.get('last_folder', '')}\n"
            f"intent : {context.get('last_intent', '')}"
        )

    def _refresh_review_count(self):
        try:
            count = len(self.controller.list_new_bug_reports())
            self._set_review_count(count)
        except Exception:
            self.review_button.configure(text="Signalements à revoir (?)")

    def _set_review_count(self, count):
        text = f"Signalements à revoir ({count})" if count else "Signalements à revoir"
        self.review_button.configure(text=text)

    def _show_bug_reviews(self):
        if self._review_window is not None:
            self._review_window.focus_set()
            return
        window = tk.Toplevel(self.root)
        self._review_window = window
        window.title("Signalements à revoir")
        window.geometry("980x680")
        window.minsize(760, 520)
        window.configure(background=PANEL)
        window.transient(self.root)
        window.grid_rowconfigure(0, weight=1)
        window.grid_columnconfigure(1, weight=1)

        list_frame = tk.Frame(window, background=PANEL)
        list_frame.grid(row=0, column=0, sticky="ns", padx=(14, 7), pady=14)
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._review_list = tk.Listbox(
            list_frame, width=34, background=INPUT_BACKGROUND, foreground=TEXT,
            selectbackground=ACCENT, relief="flat", yscrollcommand=scrollbar.set,
        )
        self._review_list.grid(row=0, column=0, sticky="ns")
        scrollbar.configure(command=self._review_list.yview)
        self._review_list.bind("<<ListboxSelect>>", self._select_bug_review)

        detail_frame = tk.Frame(window, background=PANEL)
        detail_frame.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=14)
        detail_frame.grid_columnconfigure(0, weight=1)
        detail_frame.grid_rowconfigure(0, weight=1)
        self._review_detail = tk.Text(
            detail_frame, wrap="word", state="disabled", background=INPUT_BACKGROUND,
            foreground=TEXT, relief="flat", padx=10, pady=8,
        )
        self._review_detail.grid(row=0, column=0, sticky="nsew")
        tk.Label(
            detail_frame, text="Critères attendus (un par ligne)",
            background=PANEL, foreground=TEXT, anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self._review_criteria = tk.Text(
            detail_frame, height=6, wrap="word", background=INPUT_BACKGROUND,
            foreground=TEXT, insertbackground=TEXT, relief="flat", padx=8, pady=6,
        )
        self._review_criteria.grid(row=2, column=0, sticky="ew")
        self._review_status = tk.Label(
            detail_frame, text="", background=PANEL, foreground=MUTED, anchor="w",
        )
        self._review_status.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        actions = tk.Frame(detail_frame, background=PANEL)
        actions.grid(row=4, column=0, sticky="e", pady=(10, 0))
        tk.Button(
            actions, text="Rejeter", command=self._reject_selected_bug,
            background="#63343a", foreground="white", relief="flat", padx=12, pady=6,
        ).grid(row=0, column=0, padx=(0, 8))
        tk.Button(
            actions, text="Valider", command=self._validate_selected_bug,
            background=ACCENT, foreground="white", relief="flat", padx=12, pady=6,
        ).grid(row=0, column=1, padx=(0, 8))
        tk.Button(
            actions, text="Fermer", command=self._close_bug_reviews,
            background="#343a45", foreground=TEXT, relief="flat", padx=12, pady=6,
        ).grid(row=0, column=2)
        window.protocol("WM_DELETE_WINDOW", self._close_bug_reviews)
        self._reload_bug_reviews()

    def _reload_bug_reviews(self):
        if self._review_window is None:
            self._refresh_review_count()
            return
        try:
            self._review_cases = self.controller.list_new_bug_reports()
            self._review_list.delete(0, "end")
            for case in self._review_cases:
                date = case.created_at[:10]
                comment = str(case.expected_behavior.get("user_comment", ""))
                expected = str(case.expected_behavior.get("description", ""))
                self._review_list.insert(
                    "end",
                    f"{case.id[-12:]} · {date} · {case.category} | "
                    f"Demande: {case.user_message[:45]} | Réponse: {case.incorrect_response[:45]} | "
                    f"Commentaire: {comment[:45]} | Attendu: {expected[:45]}",
                )
            message = f"{len(self._review_cases)} signalement(s) à revoir."
        except Exception as error:
            self._review_cases = []
            message = f"Impossible de charger le corpus : {error}"
        self._review_status.configure(text=message)
        self._set_review_count(len(self._review_cases))

    def _selected_review_case(self):
        selection = self._review_list.curselection()
        return self._review_cases[selection[0]] if selection else None

    def _select_bug_review(self, _event=None):
        case = self._selected_review_case()
        if case is None:
            return
        self._review_detail.configure(state="normal")
        self._review_detail.delete("1.0", "end")
        self._review_detail.insert("1.0", self._bug_review_detail(case))
        self._review_detail.configure(state="disabled")

    def _validate_selected_bug(self):
        case = self._selected_review_case()
        if case is None:
            self._review_status.configure(text="Sélectionnez un signalement.")
            return
        criteria = self._review_criteria.get("1.0", "end-1c")
        self._start_operation(
            "bug_review_validate", "Validation du signalement…",
            lambda: self.controller.validate_review_bug_report(case.id, criteria),
        )

    def _reject_selected_bug(self):
        case = self._selected_review_case()
        if case is None:
            self._review_status.configure(text="Sélectionnez un signalement.")
            return
        if not messagebox.askyesno(
            "Rejeter le signalement",
            "Conserver ce signalement avec le statut rejected ?",
            parent=self._review_window,
        ):
            return
        self._start_operation(
            "bug_review_reject", "Rejet du signalement…",
            lambda: self.controller.reject_bug_report(case.id),
        )

    def _close_bug_reviews(self):
        if self._review_window is not None:
            self._review_window.destroy()
            self._review_window = None

    def _show_confirmation(self, confirmation):
        if self._confirmation_window is not None:
            return
        window = tk.Toplevel(self.root)
        self._confirmation_window = window
        window.title(confirmation.title)
        window.configure(background=PANEL)
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        label = tk.Label(
            window,
            text=confirmation.description,
            justify="left",
            wraplength=520,
            background=PANEL,
            foreground=TEXT,
            padx=20,
            pady=20,
            font=("Segoe UI", 10),
        )
        label.grid(row=0, column=0, columnspan=2, sticky="ew")

        confirm_button = tk.Button(
            window,
            text="Confirmer",
            command=lambda: self._answer_confirmation(True),
            background=ACCENT,
            foreground="white",
            relief="flat",
            padx=16,
            pady=7,
        )
        confirm_button.grid(row=1, column=0, padx=(20, 6), pady=(0, 18), sticky="e")
        cancel_button = tk.Button(
            window,
            text="Annuler",
            command=lambda: self._answer_confirmation(False),
            background="#343a45",
            foreground=TEXT,
            relief="flat",
            padx=16,
            pady=7,
        )
        cancel_button.grid(row=1, column=1, padx=(6, 20), pady=(0, 18), sticky="w")
        window.protocol("WM_DELETE_WINDOW", lambda: self._answer_confirmation(False))
        window.bind("<Escape>", lambda _event: self._answer_confirmation(False))
        window.wait_visibility()
        window.focus_set()

    def _answer_confirmation(self, confirmed):
        if self._confirmation_window is not None:
            self._confirmation_window.grab_release()
            self._confirmation_window.destroy()
            self._confirmation_window = None
        callback = (
            self.controller.confirm_pending_action
            if confirmed
            else self.controller.cancel_pending_action
        )
        self._start_operation(
            "confirmation",
            "Traitement de la confirmation…",
            callback,
        )

    def _close(self):
        if self._confirmation_window is not None:
            self._confirmation_window.destroy()
            self._confirmation_window = None
        self._close_bug_report()
        self._close_bug_reviews()
        self.root.destroy()


def run_gui(controller=None):
    """Crée et lance explicitement l'interface graphique."""
    root = tk.Tk()
    AssistantGUI(root, controller or AppController())
    root.mainloop()
