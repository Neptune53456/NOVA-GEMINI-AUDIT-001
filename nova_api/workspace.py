"""Strict boundary for filesystem operations in an explicitly allowed workspace."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


class WorkspacePathError(ValueError):
    """Raised when a client path escapes the authorized workspace."""


class Workspace:
    def __init__(self, root: str | Path, *, workspace_id: str = "default") -> None:
        self.id = workspace_id
        self._root = Path(root).resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("workspace root must be a directory")

    @property
    def root(self) -> Path:
        """Authoritative root for trusted backend collaborators; never serialize it."""
        return self._root

    def resolve(self, client_path: str) -> Path:
        if not isinstance(client_path, str) or "\x00" in client_path:
            raise WorkspacePathError("invalid_path")
        # Reject Windows absolute/drive-relative syntax even when the backend is
        # currently running on POSIX (for example CI validating a Windows plan).
        windows_path = PureWindowsPath(client_path or ".")
        if windows_path.drive or windows_path.root:
            raise WorkspacePathError("path_outside_workspace")
        requested = Path(client_path or ".")
        if ".." in requested.parts:
            raise WorkspacePathError("invalid_path")
        candidate = requested if requested.is_absolute() else self._root / requested
        try:
            resolved = candidate.resolve(strict=False)
            common = os.path.commonpath((os.path.normcase(str(self._root)), os.path.normcase(str(resolved))))
        except (OSError, ValueError):
            raise WorkspacePathError("path_outside_workspace") from None
        if common != os.path.normcase(str(self._root)):
            raise WorkspacePathError("path_outside_workspace")
        return resolved

    def relative_name(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix() or "."
