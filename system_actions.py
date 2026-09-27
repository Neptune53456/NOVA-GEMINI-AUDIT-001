"""Actions Windows locales avec validation stricte et résultats structurés."""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import unicodedata
import uuid

import ctypes
from ctypes import wintypes


SAFE = "SAFE"
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
FORBIDDEN = "FORBIDDEN"

USER_ROOT = Path.home().resolve()
SENSITIVE_ROOTS = tuple(
    Path(path).resolve()
    for path in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")),
    )
)

ALLOWED_APPS = {
    "notepad": ("notepad.exe",),
    "bloc-notes": ("notepad.exe",),
    "bloc notes": ("notepad.exe",),
    "calculatrice": ("calc.exe",),
    "calculator": ("calc.exe",),
    "vscode": (
        "code.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Microsoft VS Code/Code.exe"),
    ),
    "vs code": (
        "code.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Microsoft VS Code/Code.exe"),
    ),
    "explorer": ("explorer.exe",),
    "explorateur": ("explorer.exe",),
}

SAFE_SYSTEM_ACTIONS = {
    "network_configuration": ("ipconfig",),
    "running_processes": ("tasklist",),
}

TEXT_FILE_EXTENSIONS = {".txt", ".md", ".log", ".csv"}

KNOWN_FOLDER_IDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
}

USER_FOLDER_ALIASES = {
    "bureau": "desktop",
    "desktop": "desktop",
    "documents": "documents",
    "document": "documents",
    "telechargements": "downloads",
    "telechargement": "downloads",
    "downloads": "downloads",
    "download": "downloads",
    "images": "pictures",
    "image": "pictures",
    "pictures": "pictures",
    "picture": "pictures",
    "videos": "videos",
    "video": "videos",
    "musique": "music",
    "music": "music",
}

FALLBACK_FOLDER_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "videos": "Videos",
    "music": "Music",
}


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid_from_string(value):
    raw = uuid.UUID(value).bytes_le
    return _Guid.from_buffer_copy(raw)


def _windows_known_folder_path(folder_key):
    """Adaptateur Known Folders ; None si l'API Windows est indisponible."""
    output_path = ctypes.c_wchar_p()
    try:
        shell32 = ctypes.windll.shell32
        folder_id = _guid_from_string(KNOWN_FOLDER_IDS[folder_key])
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_Guid), ctypes.c_uint32, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_int32
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(output_path),
        )
        if result == 0 and output_path.value:
            return Path(output_path.value)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    finally:
        if output_path.value:
            try:
                ctypes.windll.ole32.CoTaskMemFree(output_path)
            except (AttributeError, OSError):
                pass
    return None


def _known_folder_path(folder_key):
    """Retourne un Known Folder Windows, avec repli profil/OneDrive."""
    windows_path = _windows_known_folder_path(folder_key)
    if windows_path is not None:
        return windows_path

    folder_name = FALLBACK_FOLDER_NAMES[folder_key]
    one_drive = os.environ.get("OneDrive")
    if one_drive:
        one_drive_candidate = Path(one_drive) / folder_name
        if one_drive_candidate.exists():
            return one_drive_candidate
    return Path.home() / folder_name


def _normalize_alias(value):
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _strip_possessive(value):
    words = value.strip().split()
    if words and _normalize_alias(words[0]) in {"mon", "mes", "le", "la", "les"}:
        words.pop(0)
    return " ".join(words)


def _folder_key_from_expression(value):
    cleaned = _strip_possessive(value)
    normalized = _normalize_alias(cleaned).strip()
    return USER_FOLDER_ALIASES.get(normalized)


def _natural_relative_parts(value):
    """Transforme « fichier dans dossier » en dossier/fichier."""
    normalized = _normalize_alias(value)
    marker = " dans "
    marker_index = normalized.find(marker)
    if marker_index != -1:
        item = value[:marker_index].strip()
        container = value[marker_index + len(marker):].strip()
        return _natural_relative_parts(container) + [item]
    return [value.strip()] if value.strip() else []


def resolve_user_path(path):
    """Résout les alias naturels des dossiers utilisateur Windows."""
    if not isinstance(path, (str, os.PathLike)):
        return path

    original = str(path).strip().strip('"\'')
    if not original:
        return original

    direct_expression = original
    direct_normalized = _normalize_alias(direct_expression)
    for prefix in ("sur ", "dans "):
        if direct_normalized.startswith(prefix):
            direct_expression = direct_expression[len(prefix):].strip()
            direct_normalized = _normalize_alias(direct_expression)
            break

    folder_key = _folder_key_from_expression(direct_expression)
    if folder_key:
        return str(_known_folder_path(folder_key))

    natural_match = re.fullmatch(
        r"(?P<relative>.+)\s+(?:sur|dans)\s+"
        r"(?:(?:mon|mes|le|la|les)\s+)?"
        r"(?P<folder>[^\\/]+?)"
        r"(?P<trailing>(?:[\\/].*)?)",
        original,
        flags=re.IGNORECASE,
    )
    if natural_match:
        folder_key = _folder_key_from_expression(natural_match.group("folder"))
        if folder_key:
            resolved = _known_folder_path(folder_key)
            relative_parts = _natural_relative_parts(
                natural_match.group("relative")
            )
            trailing = natural_match.group("trailing").strip("\\/")
            if trailing:
                relative_parts.extend(
                    part for part in re.split(r"[\\/]+", trailing) if part
                )
            for part in relative_parts:
                resolved /= part
            return str(resolved)

    return original


def _result(success, message, requires_confirmation=False, action_level=SAFE):
    return {
        "success": success,
        "message": message,
        "requires_confirmation": requires_confirmation,
        "action_level": action_level,
    }


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_path(path, must_exist=False, destructive=False):
    """Valide un chemin local et retourne son chemin absolu résolu."""
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        return _result(False, "Chemin invalide.", action_level=FORBIDDEN), None

    try:
        resolved_text = str(resolve_user_path(path))
        windows = PureWindowsPath(resolved_text)
        if os.name != "nt" and (windows.drive or resolved_text.startswith("\\")):
            return _result(
                False, "Accès refusé : chemin Windows non local sur ce système.",
                action_level=FORBIDDEN,
            ), None
        unresolved_candidate = Path(resolved_text).expanduser()
        native = PureWindowsPath(unresolved_candidate) if os.name == "nt" else PurePosixPath(unresolved_candidate)
        if not native.is_absolute():
            return _result(
                False,
                f"Chemin relatif ambigu refusé : {unresolved_candidate}",
                action_level=FORBIDDEN,
            ), None
        candidate = unresolved_candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        return _result(False, f"Chemin invalide : {error}", action_level=FORBIDDEN), None

    if candidate == Path(candidate.anchor):
        return _result(
            False,
            f"Accès refusé à la racine du disque : {candidate}",
            action_level=FORBIDDEN,
        ), None

    if any(candidate == root or _is_relative_to(candidate, root) for root in SENSITIVE_ROOTS):
        return _result(
            False,
            f"Accès refusé au chemin système sensible : {candidate}",
            action_level=FORBIDDEN,
        ), None

    if destructive and not _is_relative_to(candidate, USER_ROOT):
        return _result(
            False,
            "Action destructive refusée hors du dossier utilisateur.",
            action_level=FORBIDDEN,
        ), None

    if must_exist and not candidate.exists():
        return _result(False, f"Chemin introuvable : {candidate}"), None

    return _result(True, "Chemin valide."), candidate


def _find_application(name):
    candidates = ALLOWED_APPS.get(name.strip().lower())
    if candidates is None:
        return None

    for candidate in candidates:
        candidate_path = Path(candidate)
        if candidate_path.is_absolute() and candidate_path.is_file():
            return str(candidate_path)
        located = shutil.which(candidate)
        if located:
            return located
    return None


def open_application(name):
    """Ouvre une application appartenant à la liste blanche."""
    if not isinstance(name, str) or name.strip().lower() not in ALLOWED_APPS:
        return _result(
            False,
            f"Application non autorisée : {name}",
            action_level=FORBIDDEN,
        )

    executable = _find_application(name)
    if executable is None:
        return _result(False, f"Application autorisée mais introuvable : {name}")

    try:
        subprocess.Popen([executable])
        return _result(True, f"Application ouverte : {name}")
    except OSError as error:
        return _result(False, f"Impossible d'ouvrir {name} : {error}")


def run_safe_system_action(action):
    """Exécute une action système prédéfinie, sans accepter de commande libre."""
    command = SAFE_SYSTEM_ACTIONS.get(action)
    if command is None:
        return _result(
            False,
            f"Action système non autorisée : {action}",
            action_level=FORBIDDEN,
        )
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _result(False, f"Impossible d'exécuter l'action sûre : {error}")

    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        return _result(False, output or "L'action système a échoué.")
    return _result(True, output or "Action système exécutée.")


def _windows_open_path(path):
    """Adaptateur de lancement Windows, remplaçable sans changer os.name."""
    os.startfile(str(path))


def open_path(path):
    """Ouvre un fichier ou dossier existant avec Windows."""
    validation, candidate = validate_path(path, must_exist=True)
    if candidate is None:
        return validation
    try:
        _windows_open_path(candidate)
        return _result(True, f"Chemin ouvert : {candidate}")
    except (AttributeError, OSError) as error:
        return _result(False, f"Impossible d'ouvrir le chemin : {error}")


def create_folder(path):
    validation, candidate = validate_path(path)
    if candidate is None:
        return validation
    if candidate.exists():
        return _result(False, f"La cible existe déjà : {candidate}")
    try:
        candidate.mkdir(parents=False)
        return _result(True, f"Dossier créé : {candidate}")
    except OSError as error:
        return _result(False, f"Impossible de créer le dossier : {error}")


def create_text_file(path, confirmed=False):
    validation, candidate = validate_path(path)
    if candidate is None:
        return validation
    if candidate.suffix.lower() not in TEXT_FILE_EXTENSIONS:
        return _result(False, "Extension de fichier texte non autorisée.", action_level=FORBIDDEN)
    if candidate.exists() and not confirmed:
        return _result(
            False,
            f"Le fichier existe déjà et serait remplacé : {candidate}. Confirmer ? (oui/non)",
            requires_confirmation=True,
            action_level=CONFIRMATION_REQUIRED,
        )
    if not candidate.parent.is_dir():
        return _result(False, f"Dossier parent introuvable : {candidate.parent}")
    try:
        candidate.write_text("", encoding="utf-8")
        return _result(True, f"Fichier texte créé : {candidate}")
    except OSError as error:
        return _result(False, f"Impossible de créer le fichier : {error}")


def _same_file(source_path, destination_path):
    """Détecte une destination identique, y compris via lien ou hardlink."""
    if source_path == destination_path:
        return True
    try:
        return destination_path.exists() and os.path.samefile(
            source_path,
            destination_path,
        )
    except OSError:
        return False


def copy_file(source, destination, confirmed=False):
    source_validation, source_path = validate_path(source, must_exist=True)
    if source_path is None:
        return source_validation
    destination_validation, destination_path = validate_path(destination)
    if destination_path is None:
        return destination_validation
    if not source_path.is_file():
        return _result(False, f"La source n'est pas un fichier : {source_path}")
    if _same_file(source_path, destination_path):
        return _result(False, "La source et la destination désignent le même fichier.")
    if destination_path.exists() and not confirmed:
        return _result(
            False,
            f"La destination serait remplacée : {destination_path}. Confirmer ? (oui/non)",
            requires_confirmation=True,
            action_level=CONFIRMATION_REQUIRED,
        )
    try:
        shutil.copy2(source_path, destination_path)
        return _result(True, f"Fichier copié vers : {destination_path}")
    except OSError as error:
        return _result(False, f"Impossible de copier le fichier : {error}")


def move_file(source, destination, confirmed=False):
    source_validation, source_path = validate_path(source, must_exist=True, destructive=True)
    if source_path is None:
        return source_validation
    destination_validation, destination_path = validate_path(destination)
    if destination_path is None:
        return destination_validation
    if not source_path.is_file():
        return _result(False, f"La source n'est pas un fichier : {source_path}")
    if _same_file(source_path, destination_path):
        return _result(False, "La source et la destination désignent le même fichier.")
    if destination_path.exists() and not confirmed:
        return _result(
            False,
            f"La destination serait remplacée : {destination_path}. Confirmer ? (oui/non)",
            requires_confirmation=True,
            action_level=CONFIRMATION_REQUIRED,
        )
    try:
        if destination_path.exists():
            os.replace(source_path, destination_path)
        else:
            shutil.move(str(source_path), str(destination_path))
        return _result(True, f"Fichier déplacé vers : {destination_path}")
    except OSError as error:
        return _result(False, f"Impossible de déplacer le fichier : {error}")


def delete_file(path, confirmed=False):
    validation, candidate = validate_path(path, must_exist=True, destructive=True)
    if candidate is None:
        return validation
    if not candidate.is_file():
        return _result(False, f"La cible n'est pas un fichier : {candidate}")
    if not confirmed:
        return _result(
            False,
            f"Cette action va supprimer {candidate}. Confirmer ? (oui/non)",
            requires_confirmation=True,
            action_level=CONFIRMATION_REQUIRED,
        )
    try:
        candidate.unlink()
        return _result(True, f"Fichier supprimé : {candidate}")
    except OSError as error:
        return _result(False, f"Impossible de supprimer le fichier : {error}")


def delete_folder(path, confirmed=False):
    validation, candidate = validate_path(path, must_exist=True, destructive=True)
    if candidate is None:
        return validation
    if not candidate.is_dir():
        return _result(False, f"La cible n'est pas un dossier : {candidate}")
    if candidate == USER_ROOT:
        return _result(False, "Suppression du dossier utilisateur refusée.", action_level=FORBIDDEN)
    if not confirmed:
        return _result(
            False,
            f"Cette action va supprimer le dossier {candidate}. Confirmer ? (oui/non)",
            requires_confirmation=True,
            action_level=CONFIRMATION_REQUIRED,
        )
    try:
        shutil.rmtree(candidate)
        return _result(True, f"Dossier supprimé : {candidate}")
    except OSError as error:
        return _result(False, f"Impossible de supprimer le dossier : {error}")
