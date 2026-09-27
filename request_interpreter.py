"""Compréhension déterministe et contexte de session pour les actions naturelles."""

from dataclasses import dataclass, field
import json
from pathlib import Path, PureWindowsPath
import re
import unicodedata

from system_actions import TEXT_FILE_EXTENSIONS, resolve_user_path, validate_path


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
VAGUE_SIMPLE_NAMES = {
    "ca",
    "cela",
    "celui la",
    "le fichier",
    "mon fichier",
    "un fichier",
    "le dossier",
    "mon dossier",
    "un dossier",
    "je ne sais pas",
}
LOCATION_NAMES = (
    "bureau",
    "desktop",
    "documents",
    "document",
    "téléchargements",
    "telechargements",
    "downloads",
    "images",
    "pictures",
    "vidéos",
    "videos",
    "musique",
    "music",
)
LOCATION_PATTERN = "|".join(re.escape(name) for name in LOCATION_NAMES)
ATTACHMENT_REFERENCE_PATTERN = (
    r"(?:piece\s+jointe|fichier\s+joint(?:e)?|document\s+joint(?:e)?|"
    r"fichier\s+que\s+j(?:e|\s+ai)\s+joint)"
)

CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "folder_name": {"type": "string"},
        "location": {"type": "string"},
        "file_name": {"type": "string"},
        "folder": {"type": "string"},
    },
    "required": ["folder_name", "location", "file_name", "folder"],
    "additionalProperties": False,
}


def _normalize(text):
    normalized = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    )


@dataclass
class PendingRequest:
    intent: str
    entities: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    candidates: list = field(default_factory=list)


@dataclass
class SessionContext:
    last_intent: str | None = None
    last_folder: str | None = None
    last_file: str | None = None
    last_path: str | None = None
    last_action: str | None = None
    active_attachment: dict | None = None
    pending: PendingRequest | None = None


@dataclass(frozen=True)
class Interpretation:
    handled: bool
    kind: str = "pass"
    message: str | None = None
    action: str | None = None
    arguments: dict | None = None
    actions: list | None = None
    error_code: str | None = None


class RequestInterpreter:
    """Résout les références sûres et remplit les paramètres sur plusieurs tours."""

    def __init__(self, *, candidate_finder=None, context_chat=None):
        self.context = SessionContext()
        self._candidate_finder = candidate_finder or self._find_file_candidates
        self._context_chat = context_chat

    def set_active_attachment(self, metadata):
        self.context.active_attachment = dict(metadata) if metadata else None

    def _model_slots(self, pending, user_input):
        """Extrait exceptionnellement des slots JSON, sans accepter de valeur inventée."""
        if self._context_chat is None:
            return {}
        prompt = (
            "Extrais uniquement les informations littéralement présentes dans la réponse "
            "utilisateur. N'invente aucun chemin ni nom. Utilise une chaîne vide pour toute "
            "information absente.\n"
            f"Intention en attente : {pending.intent}\n"
            f"Informations déjà connues : {pending.entities}\n"
            f"Réponse utilisateur : {user_input}"
        )
        try:
            response = self._context_chat(
                messages=[{"role": "user", "content": prompt}],
                task_type="context_resolution",
                think=False,
                format=CONTEXT_SCHEMA,
                options={"temperature": 0},
            )
            content = response.get("message", {}).get("content", "")
            parsed = json.loads(content)
        except Exception:
            return {}
        if not isinstance(parsed, dict) or set(parsed) - set(CONTEXT_SCHEMA["properties"]):
            return {}

        grounded = {}
        normalized_input = _normalize(user_input)
        for name, value in parsed.items():
            if not isinstance(value, str) or not value.strip():
                continue
            cleaned = value.strip()
            normalized_value = _normalize(cleaned)
            if normalized_value not in normalized_input:
                continue
            if name == "folder_name" and (
                len(cleaned) > 100
                or re.search(
                    r"[,;]|\b(?:appelle|nomme|mets|place|cree|dossier)\b",
                    normalized_value,
                )
            ):
                continue
            if (
                name == "file_name"
                and Path(cleaned).suffix.casefold() not in TEXT_FILE_EXTENSIONS
            ):
                continue
            if name == "location":
                location_match = re.fullmatch(
                    rf"(?:(?:sur|dans)\s+)?(?:(?:mon|mes|le|la|les)\s+)?"
                    rf"(?:{LOCATION_PATTERN})",
                    cleaned,
                    flags=re.IGNORECASE,
                )
                if not location_match:
                    continue
                if not re.match(r"^(?:sur|dans)\s+", cleaned, flags=re.IGNORECASE):
                    cleaned = f"sur {cleaned}"
            grounded[name] = cleaned
        return grounded

    @staticmethod
    def _find_file_candidates(name, folder_expression):
        validation, folder = validate_path(
            resolve_user_path(folder_expression),
            must_exist=True,
        )
        if folder is None or not validation.get("success") or not folder.is_dir():
            return []
        normalized_name = _normalize(Path(name).name)
        requested_suffix = Path(name).suffix.casefold()
        try:
            children = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return []
        matches = []
        for child in children:
            if not child.is_file():
                continue
            if requested_suffix:
                matches_name = _normalize(child.name) == normalized_name
            else:
                matches_name = (
                    _normalize(child.name) == normalized_name
                    or _normalize(child.stem) == normalized_name
                )
            if matches_name:
                matches.append(str(child))
        return matches

    @staticmethod
    def _all_files(folder_expression):
        validation, folder = validate_path(
            resolve_user_path(folder_expression),
            must_exist=True,
        )
        if folder is None or not validation.get("success") or not folder.is_dir():
            return []
        try:
            return sorted(
                (str(path) for path in folder.iterdir() if path.is_file()),
                key=str.casefold,
            )
        except OSError:
            return []

    @staticmethod
    def _split_named_location(text):
        match = re.fullmatch(
            rf"\s*(?P<name>.+?)\s+(?P<relation>sur|dans)\s+"
            rf"(?P<owner>mon|mes|le|la|les)?\s*(?P<location>{LOCATION_PATTERN})\s*",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, None
        name = match.group("name").strip(" ,.;:\"'")
        owner = (match.group("owner") or "").strip()
        location = " ".join(
            part
            for part in (match.group("relation"), owner, match.group("location"))
            if part
        )
        return name, location

    @classmethod
    def _folder_details_from_reply(cls, text):
        folder_name, location = cls._split_named_location(text)
        if folder_name and location and not re.search(
            r"[,;]|\b(?:appelle|nomme|mets|place|cree|dossier)\b",
            _normalize(folder_name),
        ):
            return folder_name, location

        name_match = re.search(
            r"\b(?:appelle(?:-le)?|nomme(?:-le)?|nom(?:me)?\s+sera)\s+"
            r"(.+?)(?=\s*(?:[,;]|\bet\b|$))",
            text,
            flags=re.IGNORECASE,
        )
        location_match = re.search(
            rf"\b(sur|dans)\s+((?:(?:mon|mes|le|la|les)\s+)?"
            rf"(?:{LOCATION_PATTERN}))\b",
            text,
            flags=re.IGNORECASE,
        )
        if not name_match or not location_match:
            return None, None
        return (
            name_match.group(1).strip(" ,.;:\"'"),
            f"{location_match.group(1)} {location_match.group(2)}",
        )

    @staticmethod
    def _file_name(text):
        match = re.search(
            r"(?<![\w.-])([^\s,;:\"']+\.(?:txt|md|log|csv))(?![\w.-])",
            text,
            flags=re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    @staticmethod
    def _unwrap_simple_name(text):
        value = str(text).strip(" \t\r\n,;:\"'")
        patterns = (
            r"^(?:appelle|nomme)(?:-le)?\s+(.+)$",
            r"^je\s+veux\s+l['’]appeler\s+(.+)$",
            r"^je\s+veux\s+qu['’]?il\s+(?:ait|est)\s+le\s+nom\s+(.+)$",
            r"^(?:le\s+)?nom\s+sera\s+(.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, value, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" \t\r\n,;:\"'")
        return value

    @classmethod
    def _safe_simple_name(cls, text, *, text_file=False):
        value = cls._unwrap_simple_name(text)
        normalized = _normalize(value).strip(" .?!")
        if (
            not value
            or normalized in VAGUE_SIMPLE_NAMES
            or len(value) > 100
            or len(value.split()) > 8
            or value in {".", ".."}
            or value.endswith((" ", "."))
            or re.search(r'[<>:"/\\|?*,;\x00-\x1f]', value)
            or Path(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
        ):
            return None

        suffix = Path(value).suffix.casefold()
        base_name = Path(value).stem if suffix else value
        if base_name.upper() in WINDOWS_RESERVED_NAMES:
            return None
        if text_file:
            if suffix and suffix not in TEXT_FILE_EXTENSIONS:
                return None
            if not suffix:
                value += ".txt"
        return value

    @staticmethod
    def _short_location(text):
        value = str(text).strip(" \t\r\n,;:\"'")
        match = re.fullmatch(
            rf"(?P<relation>sur|dans)?\s*"
            rf"(?P<owner>mon|mes|le|la|les)?\s*"
            rf"(?P<location>{LOCATION_PATTERN})",
            value,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        relation = match.group("relation") or "dans"
        owner = match.group("owner") or ""
        return " ".join(
            part for part in (relation, owner, match.group("location")) if part
        )

    @staticmethod
    def _attachment_question(normalized):
        """Reconnaît les questions d'identité explicites de la pièce jointe active."""
        definite = bool(
            re.search(
                r"\b(?:quelle est|quel est|c[ '\u2019]?est quoi|montre moi)\s+"
                r"(?:la|cette)\s+piece jointe\b",
                normalized,
            )
            or re.search(
                r"\b(?:quel est le\s+)?nom de\s+"
                r"(?:(?:la|cette)\s+)?piece jointe\b",
                normalized,
            )
        )
        return definite or normalized.strip(" ?") in {
            "quelle est la piece jointe",
            "c est quoi la piece jointe",
            "c'est quoi la piece jointe",
            "c\u2019est quoi la piece jointe",
        }

    @staticmethod
    def _explicit_attachment_reference(normalized):
        """Distingue une référence de session d'une question de définition générique."""
        words = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
        if not re.search(rf"\b{ATTACHMENT_REFERENCE_PATTERN}\b", words):
            return False
        generic_definition = re.fullmatch(
            rf"(?:c est quoi|qu est ce qu|definis moi|definition de)\s+"
            rf"(?:une|un)\s+{ATTACHMENT_REFERENCE_PATTERN}",
            words,
        )
        return generic_definition is None

    @staticmethod
    def _context_file_question(normalized):
        words = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
        return bool(re.fullmatch(
            r"(?:c est quoi|quel est|quelle est)\s+(?:ce|le)\s+fichier",
            words,
        ))

    def _attachment_identity(self):
        attachment = self.context.active_attachment
        if not attachment:
            return Interpretation(
                True,
                kind="information",
                message="Aucune pièce jointe n'est actuellement active.",
                error_code="NO_ACTIVE_ATTACHMENT",
            )
        name = str(attachment.get("name") or "").strip()
        source = str(
            attachment.get("reference") or attachment.get("path") or ""
        ).strip()
        if name and source:
            message = f"La pièce jointe active est {name} (source : {source})."
        elif name:
            message = f"La pièce jointe active est {name}."
        elif source:
            message = f"La pièce jointe active provient de {source}."
        else:
            message = "Une pièce jointe est active, mais son nom et sa source sont indisponibles."
        return Interpretation(True, kind="information", message=message)

    def _clarification(self, pending, message, error_code="CLARIFICATION_REQUIRED"):
        self.context.pending = pending
        self.context.last_intent = pending.intent
        return Interpretation(
            True,
            kind="clarification",
            message=message,
            error_code=error_code,
        )

    @staticmethod
    def _strip_file_article(value):
        return re.sub(
            r"^\s*(?:(?:le|ce|mon|un)\s+)?fichier(?:\s+|$)",
            "",
            value,
            count=1,
            flags=re.IGNORECASE,
        ).strip(" ,.;:\"'")

    @staticmethod
    def _file_basename(path_expression):
        value = str(path_expression).strip()
        if Path(value).is_absolute():
            return Path(value).name
        if PureWindowsPath(value).is_absolute():
            return PureWindowsPath(value).name
        return re.split(r"\s+dans\s+", value, maxsplit=1, flags=re.IGNORECASE)[0]

    @classmethod
    def _file_in_folder(cls, source, folder):
        file_name = cls._file_basename(source)
        cleaned_folder = re.sub(
            r"^\s*(?:dans|sur|vers)\s+",
            "",
            str(folder),
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if Path(cleaned_folder).is_absolute():
            return str(Path(cleaned_folder) / file_name)
        if PureWindowsPath(cleaned_folder).is_absolute():
            return str(PureWindowsPath(cleaned_folder) / file_name)
        return f"{file_name} dans {cleaned_folder}"

    @staticmethod
    def _renamed_path(source, new_name):
        value = str(source).strip()
        if Path(value).is_absolute():
            return str(Path(value).with_name(new_name))
        if PureWindowsPath(value).is_absolute():
            return str(PureWindowsPath(value).with_name(new_name))
        embedded = re.fullmatch(r".+?\s+dans\s+(.+)", value, flags=re.IGNORECASE)
        if embedded:
            return f"{new_name} dans {embedded.group(1).strip()}"
        return new_name

    def _file_operation_question(self, pending):
        missing = pending.missing
        action = pending.entities.get("action")
        if "source" in missing:
            message = "Quel fichier veux-tu utiliser ? Donne-moi son nom exact."
        elif "source_location" in missing:
            message = (
                f"Où se trouve le fichier {pending.entities.get('source')!r} ? "
                "Indique son dossier."
            )
        elif "new_name" in missing:
            message = "Quel nouveau nom veux-tu donner au fichier, extension comprise ?"
        elif "destination" in missing:
            verb = "copier" if action == "copy_file" else "déplacer"
            message = f"Dans quel dossier veux-tu {verb} ce fichier ?"
        else:
            message = "Il manque des informations pour cette opération sur le fichier."
        return self._clarification(pending, message)

    def _complete_file_operation(self, pending):
        entities = pending.entities
        source = entities["source"]
        action = entities["action"]
        if entities.get("operation") == "rename":
            destination = self._renamed_path(source, entities["new_name"])
        else:
            destination = self._file_in_folder(source, entities["destination"])

        self.context.pending = None
        self.context.last_intent = entities.get("operation", action)
        self.context.last_path = destination
        self.context.last_file = destination
        return Interpretation(
            True,
            kind="action",
            action=action,
            arguments={"source": source, "destination": destination},
        )

    def _file_operation_request(self, user_input, normalized):
        verb_match = re.match(
            r"^\s*(?:je\s+(?:veux|souhaite|voudrais)\s+)?"
            r"(?P<verb>copie(?:r)?|d[ée]place(?:r)?|renomme(?:r)?)\s+"
            r"(?P<body>.+?)\s*$",
            user_input,
            flags=re.IGNORECASE,
        )
        if not verb_match:
            return None

        verb = _normalize(verb_match.group("verb"))
        body = verb_match.group("body").strip()
        if verb.startswith("renomm"):
            parts = re.split(r"\s+en\s+", body, maxsplit=1, flags=re.IGNORECASE)
            source_text = self._strip_file_article(parts[0])
            generic_source = not source_text or _normalize(source_text) in {
                "le fichier", "ce fichier", "mon fichier", "un fichier"
            }
            source = self.context.last_file if generic_source else source_text
            new_name = self._safe_simple_name(parts[1]) if len(parts) == 2 else None
            entities = {
                "operation": "rename",
                "action": "move_file",
                "source": source,
                "new_name": new_name,
            }
            missing = []
            if not source:
                missing.append("source")
            elif (
                not generic_source
                and not Path(source).is_absolute()
                and not PureWindowsPath(source).is_absolute()
                and " dans " not in _normalize(source)
            ):
                if self.context.last_folder:
                    entities["source"] = self._file_in_folder(source, self.context.last_folder)
                else:
                    missing.append("source_location")
            if not new_name:
                missing.append("new_name")
        else:
            action = "copy_file" if verb.startswith("cop") else "move_file"
            separators = list(
                re.finditer(r"\s+(?:dans|sur|vers)\s+", body, flags=re.IGNORECASE)
            )
            if separators:
                separator = separators[-1]
                source_part = body[: separator.start()]
                destination = body[separator.end() :].strip(" ,.;:\"'")
            else:
                source_part, destination = body, None
            source_text = self._strip_file_article(source_part)
            generic_source = not source_text
            source = self.context.last_file if generic_source else source_text
            entities = {
                "operation": "copy" if action == "copy_file" else "move",
                "action": action,
                "source": source,
                "destination": destination,
            }
            missing = []
            if not source:
                missing.append("source")
            elif (
                not generic_source
                and not Path(source).is_absolute()
                and not PureWindowsPath(source).is_absolute()
                and " dans " not in _normalize(source)
            ):
                if self.context.last_folder:
                    entities["source"] = self._file_in_folder(source, self.context.last_folder)
                else:
                    missing.append("source_location")
            if not destination:
                missing.append("destination")

        pending = PendingRequest("file_operation", entities, missing)
        if missing:
            return self._file_operation_question(pending)
        return self._complete_file_operation(pending)

    def _continue_file_operation(self, pending, user_input):
        entities = pending.entities
        missing = list(pending.missing)
        expected = missing[0] if missing else None
        if expected == "source":
            source = self._file_name(user_input) or self._strip_file_article(user_input)
            if self._is_context_file_reference(source):
                source = self.context.last_file
            if source:
                entities["source"] = source
                missing.remove("source")
                if (
                    not Path(source).is_absolute()
                    and not PureWindowsPath(source).is_absolute()
                    and " dans " not in _normalize(source)
                ):
                    if self.context.last_folder:
                        entities["source"] = self._file_in_folder(
                            source, self.context.last_folder
                        )
                    else:
                        missing.insert(0, "source_location")
        elif expected == "source_location":
            location = re.sub(
                r"^\s*(?:dans|sur)\s+", "", user_input, flags=re.IGNORECASE
            ).strip()
            if location:
                entities["source"] = self._file_in_folder(entities["source"], location)
                missing.remove("source_location")
        elif expected == "new_name":
            new_name = self._safe_simple_name(user_input)
            if new_name:
                entities["new_name"] = new_name
                missing.remove("new_name")
        elif expected == "destination":
            destination = re.sub(
                r"^\s*(?:dans|sur|vers)\s+", "", user_input, flags=re.IGNORECASE
            ).strip()
            if destination:
                entities["destination"] = destination
                missing.remove("destination")

        pending.missing = missing
        if missing:
            return self._file_operation_question(pending)
        return self._complete_file_operation(pending)

    def _create_request(self, user_input, normalized):
        if not (
            re.search(r"\bcre(?:e|er)\b", normalized)
            and "dossier" in normalized
            and re.search(r"\b(?:fichier|document)\b", normalized)
            and re.search(r"\b(?:texte|textuel)\b", normalized)
            and re.search(r"\b(?:dedans|interieur)\b", normalized)
        ):
            return None

        folder_name = None
        location = None
        folder_match = re.search(
            rf"dossier\s+(?:nomme\s+|appele\s+)?(.+?\s+(?:sur|dans)\s+"
            rf"(?:(?:mon|mes|le|la|les)\s+)?(?:{LOCATION_PATTERN}))",
            user_input,
            flags=re.IGNORECASE,
        )
        if folder_match:
            folder_name, location = self._split_named_location(folder_match.group(1))
        file_name = self._file_name(user_input)
        entities = {
            "folder_name": folder_name,
            "location": location,
            "file_name": file_name,
        }
        missing = [name for name, value in entities.items() if not value]
        pending = PendingRequest("create_folder_with_file", entities, missing)
        if folder_name is None or location is None:
            return self._clarification(
                pending,
                "Bien sûr. Quel nom veux-tu donner au dossier et où veux-tu le créer ? "
                "Tu peux répondre par exemple « Travail sur mon Bureau ».",
            )
        if file_name is None:
            return self._clarification(
                pending,
                "Quel nom veux-tu donner au fichier texte, par exemple « notes.txt » ?",
            )
        return self._complete_create(pending)

    def _complete_create(self, pending):
        folder_path = f"{pending.entities['folder_name']} {pending.entities['location']}"
        file_path = f"{pending.entities['file_name']} dans {folder_path}"
        self.context.pending = None
        self.context.last_intent = pending.intent
        self.context.last_folder = folder_path
        self.context.last_file = file_path
        self.context.last_path = file_path
        return Interpretation(
            True,
            kind="plan",
            actions=[
                {"action": "create_folder", "arguments": {"path": folder_path}},
                {"action": "create_text_file", "arguments": {"path": file_path}},
            ],
        )

    @staticmethod
    def _refresh_create_missing(pending):
        pending.missing = [
            name
            for name in ("folder_name", "location", "file_name")
            if not pending.entities.get(name)
        ]
        return pending.missing

    def _remaining_create_question(self, pending):
        missing = self._refresh_create_missing(pending)
        if missing == ["folder_name"]:
            message = "Quel nom veux-tu donner au dossier ?"
        elif missing == ["location"]:
            message = (
                "Où veux-tu créer ce dossier ? Tu peux répondre « sur mon Bureau » "
                "ou simplement « Documents »."
            )
        elif missing == ["file_name"]:
            message = "Quel nom veux-tu donner au fichier texte, par exemple « notes.txt » ?"
        elif set(missing) == {"folder_name", "location"}:
            message = (
                "Quel nom veux-tu donner au dossier et où veux-tu le créer ? "
                "Par exemple : « Travail sur mon Bureau »."
            )
        elif "folder_name" in missing:
            message = "Quel nom veux-tu donner au dossier ?"
        elif "location" in missing:
            message = "Où veux-tu créer le dossier ?"
        else:
            message = "Quel nom veux-tu donner au fichier texte ?"
        return self._clarification(pending, message)

    def _continue_create(self, pending, user_input):
        entities = pending.entities
        missing_before = self._refresh_create_missing(pending)

        if len(missing_before) == 1:
            expected = missing_before[0]
            if expected == "file_name":
                entities[expected] = self._safe_simple_name(user_input, text_file=True)
            elif expected == "folder_name":
                entities[expected] = self._safe_simple_name(user_input)
            else:
                entities[expected] = self._short_location(user_input)
            if not entities.get(expected):
                return self._remaining_create_question(pending)
            return self._complete_create(pending)

        if not entities.get("folder_name") or not entities.get("location"):
            folder_name, location = self._folder_details_from_reply(user_input)
            if not folder_name or not location:
                short_location = self._short_location(user_input)
                if short_location and not entities.get("location"):
                    location = short_location
                elif not entities.get("folder_name"):
                    folder_name = self._safe_simple_name(user_input)

            if not folder_name and not location:
                slots = self._model_slots(pending, user_input)
                folder_name = folder_name or slots.get("folder_name")
                location = location or slots.get("location")
            if folder_name:
                entities["folder_name"] = folder_name
            if location:
                entities["location"] = location

        if self._refresh_create_missing(pending):
            return self._remaining_create_question(pending)
        return self._complete_create(pending)

    @staticmethod
    def _is_context_file_reference(target):
        return _normalize(target).strip(" .?!") in {
            "",
            "le",
            "celui la",
            "ce fichier",
            "le fichier",
            "dedans",
            "a l interieur",
            "a l'interieur",
        }

    def _ambiguous_candidates(self, pending, candidates):
        names = ", ".join(PureWindowsPath(candidate).name for candidate in candidates)
        pending.candidates = list(candidates)
        pending.missing = ["file_choice"]
        return self._clarification(
            pending,
            f"J'ai trouvé plusieurs fichiers correspondants : {names}. Lequel veux-tu utiliser ?",
            error_code="AMBIGUOUS_FILE",
        )

    def _resolve_file(self, pending, target, folder):
        if target and Path(target).is_absolute():
            return target, None

        if self._is_context_file_reference(target):
            if self.context.last_file:
                return self.context.last_file, None
            if folder:
                candidates = self._all_files(folder)
                if len(candidates) == 1:
                    return candidates[0], None
                if len(candidates) > 1:
                    return None, self._ambiguous_candidates(pending, candidates)
            return None, self._clarification(
                pending,
                "Quel fichier veux-tu supprimer ? Donne-moi son nom exact.",
            )

        embedded = re.fullmatch(r"(.+?)\s+dans\s+(.+)", target, flags=re.IGNORECASE)
        if embedded:
            target = embedded.group(1).strip()
            folder = embedded.group(2).strip()
        else:
            named_location, base_location = self._split_named_location(target)
            if named_location and base_location:
                target = named_location
                folder = base_location.removeprefix("sur ").removeprefix("dans ")

        folder = folder or self.context.last_folder
        if not folder:
            pending.entities["file_name"] = target
            pending.missing = ["location"]
            return None, self._clarification(
                pending,
                f"Où se trouve le fichier {target!r} ? Tu peux répondre par exemple "
                "« dans Travail sur mon Bureau ».",
            )

        candidates = self._candidate_finder(target, folder)
        if len(candidates) == 1:
            if Path(target).suffix:
                folder_path = Path(folder)
                if folder_path.is_absolute():
                    return str(folder_path / target), None
                return f"{target} dans {folder}", None
            return candidates[0], None
        if len(candidates) > 1:
            return None, self._ambiguous_candidates(pending, candidates)
        pending.entities.update(file_name=target, folder=folder)
        pending.missing = ["file_name"]
        return None, self._clarification(
            pending,
            f"Je ne trouve pas de fichier nommé {target!r} dans {folder}. "
            "Quel est son nom exact, extension comprise ?",
            error_code="FILE_NOT_FOUND",
        )

    def _complete_delete_file(self, pending, path):
        self.context.pending = None
        self.context.last_intent = "delete_file"
        self.context.last_file = path
        self.context.last_path = path
        self.context.last_action = "delete_file"
        return Interpretation(
            True,
            kind="action",
            action="delete_file",
            arguments={"path": path},
        )

    def _delete_file_request(self, user_input, normalized):
        match = re.match(
            r"^\s*(?:je\s+(?:veux|souhaite|voudrais)\s+)?"
            r"(?:supprime(?:r)?|efface(?:r)?)\s+"
            r"(?:(?:mon|le|un|ce)\s+)?fichier(?:\s+(.*?))?\s*$",
            user_input,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        target = (match.group(1) or "").strip(" ,.;:\"'")
        pending = PendingRequest("delete_file", {"file_name": target}, [])
        path, clarification = self._resolve_file(pending, target, None)
        return clarification or self._complete_delete_file(pending, path)

    def _continue_delete_file(self, pending, user_input):
        if pending.candidates:
            normalized_answer = _normalize(user_input).strip(" .?!\"'")
            selected = [
                candidate
                for candidate in pending.candidates
                if _normalize(PureWindowsPath(candidate).name) == normalized_answer
            ]
            if len(selected) == 1:
                return self._complete_delete_file(pending, selected[0])
            return self._ambiguous_candidates(pending, pending.candidates)

        target = pending.entities.get("file_name") or ""
        folder = pending.entities.get("folder")
        normalized = _normalize(user_input).strip()
        if "location" in pending.missing:
            simple_location = re.match(
                r"^\s*(?:dans|sur)\s+(.+?)\s*$",
                user_input,
                flags=re.IGNORECASE,
            )
            if simple_location:
                folder = simple_location.group(1)
            else:
                slots = self._model_slots(pending, user_input)
                folder = slots.get("folder") or slots.get("location")
                if not folder:
                    return self._clarification(
                        pending,
                        "Dans quel dossier se trouve ce fichier ?",
                    )
        elif "file_name" in pending.missing:
            target = self._file_name(user_input) or user_input.strip(" ,.;:\"'")
        elif normalized:
            target = user_input.strip(" ,.;:\"'")
        path, clarification = self._resolve_file(pending, target, folder)
        return clarification or self._complete_delete_file(pending, path)

    def _delete_multiple_request(self, user_input, normalized):
        generic = bool(
            re.search(r"\b(?:les\s+supprim(?:e|er)|les\s+effac(?:e|er))\b", normalized)
        )
        explicit_pair = (
            re.search(r"\b(?:supprime|supprimer|efface|effacer)\b", normalized)
            and "fichier" in normalized
            and "dossier" in normalized
        )
        if not generic and not explicit_pair:
            return None
        pending = PendingRequest("delete_file_and_folder", {}, ["targets"])
        return self._clarification(
            pending,
            "Quels fichier et dossier veux-tu supprimer, et où se trouvent-ils ?",
        )

    def _complete_delete_multiple(self, pending, file_path, folder):
        self.context.pending = None
        self.context.last_intent = pending.intent
        self.context.last_folder = folder
        self.context.last_file = file_path
        self.context.last_path = folder
        return Interpretation(
            True,
            kind="plan",
            actions=[
                {"action": "delete_file", "arguments": {"path": file_path}},
                {"action": "delete_folder", "arguments": {"path": folder}},
            ],
        )

    def _continue_delete_multiple(self, pending, user_input):
        if pending.candidates:
            normalized_answer = _normalize(user_input).strip(" .?!\"'")
            selected = [
                candidate
                for candidate in pending.candidates
                if _normalize(PureWindowsPath(candidate).name) == normalized_answer
            ]
            if len(selected) != 1:
                return self._ambiguous_candidates(pending, pending.candidates)
            return self._complete_delete_multiple(
                pending,
                selected[0],
                pending.entities["folder"],
            )

        folder_match = re.search(
            rf"dossier\s+(.+?\s+(?:sur|dans)\s+"
            rf"(?:(?:mon|mes|le|la|les)\s+)?(?:{LOCATION_PATTERN}))",
            user_input,
            flags=re.IGNORECASE,
        )
        file_match = re.search(
            r"fichier\s+(.+?)(?=\s+(?:à|a)\s+l['’]?intérieur|\s+dedans|[,;]|$)",
            user_input,
            flags=re.IGNORECASE,
        )
        if folder_match:
            pending.entities["folder"] = folder_match.group(1).strip()
        if file_match:
            pending.entities["file_name"] = file_match.group(1).strip(" ,.;:\"'")

        folder = pending.entities.get("folder") or self.context.last_folder
        target = pending.entities.get("file_name")
        if not folder or not target:
            return self._clarification(
                pending,
                "Indique-moi le nom du dossier, son emplacement et le nom exact du fichier.",
            )

        file_pending = PendingRequest(
            "delete_file_and_folder",
            {"file_name": target, "folder": folder},
            [],
        )
        file_path, clarification = self._resolve_file(file_pending, target, folder)
        if clarification:
            self.context.pending = file_pending
            return clarification
        return self._complete_delete_multiple(pending, file_path, folder)

    def _delete_folder_request(self, user_input, normalized):
        match = re.match(
            r"^\s*(?:je\s+(?:veux|souhaite|voudrais)\s+)?"
            r"(?:supprime(?:r)?|efface(?:r)?)\s+"
            r"(?:(?:mon|le|un|ce)\s+)?dossier(?:\s+(.*?))?\s*$",
            user_input,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        target = (match.group(1) or "").strip(" ,.;:\"'")
        if self._is_context_file_reference(target) and self.context.last_folder:
            target = self.context.last_folder
        if not target:
            return self._clarification(
                PendingRequest("delete_folder", {}, ["folder"]),
                "Quel dossier veux-tu supprimer et où se trouve-t-il ?",
            )
        if not Path(target).is_absolute() and not re.search(
            rf"\b(?:sur|dans)\s+(?:(?:mon|mes|le|la|les)\s+)?(?:{LOCATION_PATTERN})\b",
            target,
            flags=re.IGNORECASE,
        ):
            return self._clarification(
                PendingRequest("delete_folder", {"folder_name": target}, ["location"]),
                f"Où se trouve le dossier {target!r} ?",
            )
        self.context.last_folder = target
        self.context.last_path = target
        self.context.last_intent = "delete_folder"
        return Interpretation(
            True,
            kind="action",
            action="delete_folder",
            arguments={"path": target},
        )

    def _continue_delete_folder(self, pending, user_input):
        folder_name = pending.entities.get("folder_name")
        location = re.sub(r"^\s*(?:dans|sur)\s+", "", user_input, flags=re.IGNORECASE)
        target = f"{folder_name} dans {location}" if folder_name else location
        self.context.pending = None
        self.context.last_folder = target
        self.context.last_path = target
        self.context.last_intent = "delete_folder"
        return Interpretation(
            True,
            kind="action",
            action="delete_folder",
            arguments={"path": target},
        )

    def _continue_pending(self, user_input):
        pending = self.context.pending
        if _normalize(user_input).strip(" .?!") in {"annule", "annuler", "laisse tomber"}:
            self.context.pending = None
            return Interpretation(
                True,
                kind="information",
                message="D'accord, j'annule cette demande.",
            )
        if pending.intent == "create_folder_with_file":
            return self._continue_create(pending, user_input)
        if pending.intent == "delete_file":
            return self._continue_delete_file(pending, user_input)
        if pending.intent == "delete_file_and_folder":
            return self._continue_delete_multiple(pending, user_input)
        if pending.intent == "delete_folder":
            return self._continue_delete_folder(pending, user_input)
        if pending.intent == "file_operation":
            return self._continue_file_operation(pending, user_input)
        self.context.pending = None
        return Interpretation(False)

    def interpret(self, user_input):
        """Interprète un tour sans exécuter lui-même la moindre action système."""
        if not isinstance(user_input, str) or not user_input.strip():
            return Interpretation(False)
        normalized = _normalize(user_input)

        # Une référence explicite à une pièce jointe GUI prime toujours sur
        # last_file, y compris pendant une correction de sujet multi-tour.
        if (
            self._attachment_question(normalized)
            or self._explicit_attachment_reference(normalized)
        ):
            return self._attachment_identity()

        if self._context_file_question(normalized) and self.context.last_file:
            return Interpretation(
                True,
                kind="information",
                message=f"Le fichier auquel tu fais référence est {self.context.last_file}.",
            )

        if self.context.pending is not None:
            return self._continue_pending(user_input)

        if normalized.strip(" .?!") in {"supprime-le", "efface-le"}:
            if self.context.last_file:
                return self._complete_delete_file(
                    PendingRequest("delete_file", {"file_name": self.context.last_file}),
                    self.context.last_file,
                )
            if self.context.last_folder:
                return Interpretation(
                    True,
                    kind="action",
                    action="delete_folder",
                    arguments={"path": self.context.last_folder},
                )

        for resolver in (
            self._create_request,
            self._delete_multiple_request,
            self._delete_file_request,
            self._delete_folder_request,
            self._file_operation_request,
        ):
            result = resolver(user_input, normalized)
            if result is not None:
                return result
        return Interpretation(False)
