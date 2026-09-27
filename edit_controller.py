"""Contrôleur des propositions et confirmations de modification de fichiers."""

import re

from file_editor import (
    apply_edit,
    create_diff,
    generate_patch_edit,
    repair_until_valid,
)


class EditController:
    """Conserve et traite l'état d'une proposition de modification."""

    CONFIRMATIONS = {
        "oui",
        "applique",
        "appliquer",
        "confirme",
        "vas-y",
        "vas y",
    }
    CANCELLATIONS = {
        "non",
        "annule",
        "annuler",
        "refuse",
    }

    def __init__(self):
        self.pending_edit = None

    def _result(self, handled, response=None):
        return {
            "handled": handled,
            "response": response,
            "continue": handled,
        }

    def _handle_confirmation(self, user_input):
        answer = user_input.lower()

        if answer in self.CONFIRMATIONS:
            try:
                print("\nIA : Vérification de la modification...\n")

                valid, repaired_content, error, attempts = repair_until_valid(
                    self.pending_edit["path"],
                    self.pending_edit["instruction"],
                    self.pending_edit["new_content"],
                    max_attempts=2,
                )

                if not valid:
                    print(
                        "\nIA : Je n'ai pas réussi à produire "
                        "une version fonctionnelle après plusieurs essais.\n"
                    )
                    print("Dernière erreur :\n")
                    print(error)
                    print()
                    self.pending_edit = None
                    return self._result(True, str(error))

                if repaired_content != self.pending_edit["new_content"]:
                    print(
                        f"\nIA : La première proposition avait un problème. "
                        f"Je l'ai corrigée automatiquement "
                        f"en {attempts} tentative(s).\n"
                    )
                    new_diff = create_diff(
                        self.pending_edit["original_content"],
                        repaired_content,
                        self.pending_edit["path"],
                    )
                    print("===== NOUVELLE VERSION PROPOSÉE =====\n")
                    print(new_diff)
                    print()
                    self.pending_edit["new_content"] = repaired_content
                    print(
                        "\nIA : La nouvelle version passe la validation syntaxique. "
                        "Veux-tu l'appliquer ? (oui / non)\n"
                    )
                    return self._result(True, new_diff)

                backup_path = apply_edit(
                    self.pending_edit["path"],
                    self.pending_edit["new_content"],
                    expected_content=self.pending_edit["original_content"],
                )
                print(
                    f"\nIA : Modification appliquée.\n"
                    f"Sauvegarde créée : {backup_path}\n"
                )
                self.pending_edit = None
                return self._result(True, backup_path)

            except Exception as error:
                print(
                    f"\nIA : Impossible d'appliquer "
                    f"la modification : {error}\n"
                )
                self.pending_edit = None
                return self._result(True, str(error))

        if answer in self.CANCELLATIONS:
            self.pending_edit = None
            print(
                "\nIA : Modification annulée. "
                "Le fichier n'a pas été touché.\n"
            )
            return self._result(True, "Modification annulée.")

        return self._result(False)

    def _handle_new_request(self, user_input):
        edit_match = re.search(
            r'(?:modifie|corrige|répare|repare)\s+'
            r'(?:le\s+)?(?:fichier\s+)?'
            r'["\']?([^"\']+?\.(?:py|txt|json|md|ini|yaml|yml))["\']?'
            r'(?:\s+(?:pour|afin de|:)\s*(.*))?',
            user_input,
            re.IGNORECASE,
        )
        if not edit_match:
            return self._result(False)

        file_path = edit_match.group(1).strip()
        instruction = edit_match.group(2)
        if not instruction:
            instruction = (
                "Analyse le fichier et corrige les problèmes "
                "évidents sans supprimer de fonctionnalités."
            )

        print(f"\nIA : Je prépare une modification de {file_path}...\n")

        try:
            original_content, new_content, summary = generate_patch_edit(
                file_path,
                instruction,
            )
            diff = create_diff(original_content, new_content, file_path)

            if not diff:
                print("IA : Je ne trouve aucune modification nécessaire.\n")
                return self._result(True, "Aucune modification nécessaire.")

            if summary:
                print(f"IA : {summary}\n")

            print("===== MODIFICATIONS PROPOSÉES =====\n")
            print(diff)
            print()

            self.pending_edit = {
                "path": file_path,
                "original_content": original_content,
                "new_content": new_content,
                "instruction": instruction,
            }
            print(
                "\nIA : Veux-tu appliquer cette modification ? "
                "(oui / non)\n"
            )
            return self._result(True, diff)

        except Exception as error:
            print(
                f"\nIA : Impossible de préparer "
                f"la modification : {error}\n"
            )
            return self._result(True, str(error))

    def handle(self, user_input):
        """Traite une confirmation en attente ou une nouvelle demande d'édition."""
        if self.pending_edit is not None:
            result = self.handle_confirmation(user_input)
            if result["handled"]:
                return result

        return self._handle_new_request(user_input)

    def handle_confirmation(self, user_input):
        """Traite uniquement une réponse à la proposition en attente."""
        if self.pending_edit is None:
            return self._result(False)
        return self._handle_confirmation(user_input)
