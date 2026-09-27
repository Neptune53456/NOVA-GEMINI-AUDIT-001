"""Abstraction sûre d'une source documentaire locale ou jointe en mémoire."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from system_actions import resolve_user_path, validate_path


class DocumentSourceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DocumentSource:
    """Référence stable fournie par le CLI ou, plus tard, par une interface."""

    kind: str
    name: str
    path: str | Path | None = None
    content: bytes | None = None
    reference: str | None = None

    @classmethod
    def local_path(cls, path):
        return cls(kind="local_path", name=Path(str(path)).name, path=path)

    @classmethod
    def attachment(cls, name, content=None, reference=None):
        return cls(
            kind="attachment",
            name=str(name),
            content=bytes(content) if isinstance(content, (bytes, bytearray, memoryview)) else content,
            reference=reference,
        )


@dataclass(frozen=True)
class ResolvedDocumentSource:
    kind: str
    name: str
    extension: str
    size: int
    path: Path | None = None
    content: bytes | None = None
    reference: str | None = None

    def open_binary(self):
        if self.path is not None:
            return self.path.open("rb")
        return BytesIO(self.content or b"")

    def metadata(self):
        return {
            "source_kind": self.kind,
            "name": self.name,
            "path": str(self.path) if self.path is not None else None,
            "attachment_reference": self.reference,
            "format": self.extension.lstrip("."),
            "source_bytes": self.size,
        }


def resolve_document_source(
    source,
    *,
    project_root,
    supported_extensions,
    max_source_bytes,
):
    """Valide et matérialise une source sans interpréter son contenu."""
    if isinstance(source, (str, Path)):
        source = DocumentSource.local_path(source)
    if not isinstance(source, DocumentSource):
        raise DocumentSourceError("INVALID_SOURCE", "Source documentaire invalide.")

    if source.kind == "attachment":
        clean_name = Path(source.name).name if source.name else ""
        if not clean_name or "\x00" in clean_name:
            raise DocumentSourceError("INVALID_SOURCE_NAME", "Nom de pièce jointe invalide.")
        extension = Path(clean_name).suffix.casefold()
        if extension not in supported_extensions:
            raise DocumentSourceError(
                "UNSUPPORTED_FORMAT",
                "Format non pris en charge. Formats acceptés : PDF, TXT, Markdown et DOCX.",
            )
        if source.content is None:
            raise DocumentSourceError(
                "ATTACHMENT_NOT_RESOLVED",
                "La pièce jointe est référencée mais son contenu n'a pas été fourni.",
            )
        if not isinstance(source.content, bytes):
            raise DocumentSourceError("INVALID_SOURCE", "Le contenu joint doit être binaire.")
        if len(source.content) > max_source_bytes:
            raise DocumentSourceError(
                "FILE_TOO_LARGE",
                f"Le document dépasse la limite de {max_source_bytes // (1024 * 1024)} Mo.",
            )
        return ResolvedDocumentSource(
            kind="attachment",
            name=clean_name,
            extension=extension,
            size=len(source.content),
            content=source.content,
            reference=source.reference,
        )

    if source.kind != "local_path" or source.path is None or not str(source.path).strip():
        raise DocumentSourceError("INVALID_PATH", "Un chemin de document non vide est requis.")

    resolved_expression = resolve_user_path(source.path)
    candidate = Path(resolved_expression).expanduser()
    if not candidate.is_absolute():
        candidate = Path(project_root) / candidate
    validation, resolved = validate_path(str(candidate), must_exist=True)
    if resolved is None:
        raise DocumentSourceError("UNSAFE_PATH", validation["message"])
    extension = resolved.suffix.casefold()
    if extension not in supported_extensions:
        raise DocumentSourceError(
            "UNSUPPORTED_FORMAT",
            "Format non pris en charge. Formats acceptés : PDF, TXT, Markdown et DOCX.",
        )
    if not resolved.is_file():
        raise DocumentSourceError("NOT_A_FILE", "Le chemin ne désigne pas un fichier.")
    try:
        source_size = resolved.stat().st_size
    except OSError as error:
        raise DocumentSourceError(
            "READ_ERROR", f"Impossible de lire les métadonnées : {error}"
        ) from error
    if source_size > max_source_bytes:
        raise DocumentSourceError(
            "FILE_TOO_LARGE",
            f"Le document dépasse la limite de {max_source_bytes // (1024 * 1024)} Mo.",
        )
    return ResolvedDocumentSource(
        kind="local_path",
        name=resolved.name,
        extension=extension,
        size=source_size,
        path=resolved,
    )

