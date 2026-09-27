"""Small local OCR boundary for desktop screenshots.

The backend is optional and dependency-light: if the ``tesseract`` executable is
unavailable, callers receive a structured unavailable result and perception can
continue with UIA / vision. No shell is used and OCR output is bounded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import shutil
import subprocess
import tempfile
from typing import Protocol

MAX_OCR_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OCR_SPANS = 500
MAX_OCR_TEXT = 240
DEFAULT_OCR_TIMEOUT = 12.0


@dataclass(frozen=True)
class OcrSpan:
    text: str
    confidence: float
    bounds: tuple[int, int, int, int]

    def public(self) -> dict[str, object]:
        x, y, w, h = self.bounds
        return {
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "bounds": {"x": x, "y": y, "width": w, "height": h},
        }


class ImageOcrBackend(Protocol):
    def availability(self) -> dict[str, object]: ...
    def extract(self, png_bytes: bytes, *, timeout_seconds: float = DEFAULT_OCR_TIMEOUT) -> list[OcrSpan]: ...


class TesseractImageOcrBackend:
    name = "tesseract-local"

    def __init__(self, executable: str | None = None, language: str | None = None) -> None:
        self.executable = executable
        self.language = language

    def availability(self) -> dict[str, object]:
        executable = self.executable or shutil.which("tesseract")
        return {
            "available": bool(executable),
            "backend": self.name,
            "executable": executable,
            "reason": None if executable else "tesseract_not_found",
        }

    def extract(self, png_bytes: bytes, *, timeout_seconds: float = DEFAULT_OCR_TIMEOUT) -> list[OcrSpan]:
        info = self.availability()
        executable = info.get("executable")
        if not executable:
            return []
        if not png_bytes or len(png_bytes) > MAX_OCR_IMAGE_BYTES:
            return []
        timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        with tempfile.TemporaryDirectory(prefix="nova_ocr_") as directory:
            source = Path(directory) / "capture.png"
            source.write_bytes(png_bytes)
            command = [str(executable), str(source), "stdout", "tsv"]
            if self.language:
                command[3:3] = ["-l", self.language]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return []
        if result.returncode != 0 or not result.stdout:
            return []
        spans: list[OcrSpan] = []
        try:
            reader = csv.DictReader(result.stdout.splitlines(), delimiter="\t")
            for row in reader:
                text = " ".join(str(row.get("text") or "").split())[:MAX_OCR_TEXT]
                if not text:
                    continue
                try:
                    raw_conf = float(row.get("conf") or -1)
                    left = int(row.get("left") or 0); top = int(row.get("top") or 0)
                    width = int(row.get("width") or 0); height = int(row.get("height") or 0)
                except (TypeError, ValueError):
                    continue
                if raw_conf < 0 or width <= 0 or height <= 0:
                    continue
                spans.append(OcrSpan(text, max(0.0, min(1.0, raw_conf / 100.0)), (left, top, width, height)))
                if len(spans) >= MAX_OCR_SPANS:
                    break
        except (csv.Error, UnicodeError):
            return []
        return spans


def get_default_image_ocr_backend() -> ImageOcrBackend:
    return TesseractImageOcrBackend()
