"""PC virtuel strictement confiné dans un répertoire temporaire."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile


class SimulatedPathError(ValueError):
    pass


class SimulatedPC:
    def __init__(self, root=None):
        self._temporary = None if root is not None else tempfile.TemporaryDirectory(prefix="scenario_lab_")
        self.root = Path(root if root is not None else self._temporary.name).resolve()

    def __enter__(self):
        self.seed()
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _safe(self, relative) -> Path:
        text = str(relative)
        windows = PureWindowsPath(text)
        posix = PurePosixPath(text.replace("\\", "/"))
        if windows.anchor or posix.is_absolute():
            raise SimulatedPathError("Les chemins absolus sont interdits dans le PC simulé.")
        if any(":" in part for part in windows.parts):
            raise SimulatedPathError("Les flux Windows sont interdits dans le PC simulé.")
        if any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?",
                            part.rstrip(" ."), flags=re.IGNORECASE) for part in windows.parts):
            raise SimulatedPathError("Les noms de périphériques Windows sont interdits.")
        raw = Path(*posix.parts)
        target = (self.root / raw).resolve()
        if target != self.root and self.root not in target.parents:
            raise SimulatedPathError("Le chemin sort du PC simulé.")
        return target

    def seed(self):
        for folder in ("Desktop/Travail", "Documents", "Downloads", "Attachments"):
            self._safe(folder).mkdir(parents=True, exist_ok=True)
        for name, content in {
            "Desktop/Travail/notes.txt": "notes simulées\n",
            "Desktop/photo.jpg": "image simulée\n",
            "Documents/cours.pdf": "pdf simulé\n",
            "Attachments/facture.pdf": "pièce jointe simulée\n",
        }.items():
            path = self._safe(name)
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def create_file(self, relative, content=""):
        target = self._safe(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return target

    def copy(self, source, destination):
        return Path(shutil.copy2(self._safe(source), self._safe(destination)))

    def move(self, source, destination):
        return Path(shutil.move(self._safe(source), self._safe(destination)))

    def rename(self, source, destination):
        return self.move(source, destination)

    def delete(self, relative, *, confirmed=False):
        if not confirmed:
            return {"success": False, "requires_confirmation": True}
        target = self._safe(relative)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"success": True, "requires_confirmation": False}
