"""Backend OCR optionnel fondé sur les exécutables Tesseract et Poppler."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import time


DEFAULT_OCR_TIMEOUT_SECONDS = 45
MAX_OCR_TIMEOUT_SECONDS = 60
MAX_RENDERED_PAGE_BYTES = 20 * 1024 * 1024
MAX_BACKEND_PAGES = 20
MAX_OCR_OUTPUT_CHARS = 500_000


class OcrBackendError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class TesseractOcrBackend:
    """Rend les pages avec pdftoppm puis les lit avec Tesseract, sans shell."""

    name = "tesseract-poppler"

    def __init__(self, renderer=None, tesseract=None, dpi=200, language=None):
        self.renderer = renderer
        self.tesseract = tesseract
        self.dpi = dpi
        self.language = language

    def availability(self):
        renderer = self.renderer or shutil.which("pdftoppm")
        tesseract = self.tesseract or shutil.which("tesseract")
        missing = []
        if not renderer:
            missing.append("pdftoppm (Poppler)")
        if not tesseract:
            missing.append("tesseract")
        return {
            "available": not missing,
            "backend": self.name,
            "renderer": renderer,
            "tesseract": tesseract,
            "missing": missing,
            "message": (
                "Backend OCR disponible."
                if not missing
                else "OCR indisponible : installez manuellement " + " et ".join(missing) + "."
            ),
        }

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OcrBackendError("OCR_TIMEOUT", "Le délai total accordé à l'OCR est dépassé.")
        return remaining

    def extract_pdf_pages(
        self,
        *,
        pdf_path=None,
        pdf_content=None,
        page_numbers,
        timeout_seconds=DEFAULT_OCR_TIMEOUT_SECONDS,
        max_output_chars=500_000,
    ):
        availability = self.availability()
        if not availability["available"]:
            raise OcrBackendError("OCR_UNAVAILABLE", availability["message"])
        if not page_numbers:
            return {}
        if (
            timeout_seconds <= 0
            or timeout_seconds > MAX_OCR_TIMEOUT_SECONDS
            or max_output_chars <= 0
            or max_output_chars > MAX_OCR_OUTPUT_CHARS
            or not isinstance(self.dpi, int)
            or isinstance(self.dpi, bool)
            or not 72 <= self.dpi <= 300
        ):
            raise OcrBackendError("OCR_LIMIT_ERROR", "Limites OCR invalides.")
        invalid_page = any(
            not isinstance(page, int) or isinstance(page, bool) or page <= 0
            for page in page_numbers
        )
        if (
            len(page_numbers) > MAX_BACKEND_PAGES
            or invalid_page
            or len(set(page_numbers)) != len(page_numbers)
        ):
            raise OcrBackendError("OCR_PAGE_LIMIT", "Liste de pages OCR invalide ou trop longue.")

        deadline = time.monotonic() + timeout_seconds
        results = {}
        total_characters = 0
        with tempfile.TemporaryDirectory(prefix="assistant_ocr_") as directory:
            temp_root = Path(directory)
            if pdf_content is not None:
                input_pdf = temp_root / "attachment.pdf"
                try:
                    input_pdf.write_bytes(pdf_content)
                except (OSError, TypeError) as error:
                    raise OcrBackendError("OCR_SOURCE_ERROR", f"Source PDF invalide : {error}") from error
            elif pdf_path is not None:
                input_pdf = Path(pdf_path)
            else:
                raise OcrBackendError("OCR_SOURCE_ERROR", "Source PDF absente.")

            for page_number in page_numbers:
                output_prefix = temp_root / f"page_{page_number}"
                try:
                    rendered = subprocess.run(
                        [
                            availability["renderer"],
                            "-f", str(page_number),
                            "-l", str(page_number),
                            "-singlefile",
                            "-png",
                            "-r", str(self.dpi),
                            str(input_pdf),
                            str(output_prefix),
                        ],
                        capture_output=True,
                        timeout=self._remaining(deadline),
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    raise OcrBackendError("OCR_TIMEOUT", "Le rendu d'une page a expiré.") from error
                except OSError as error:
                    raise OcrBackendError("OCR_RENDER_ERROR", f"Rendu PDF impossible : {error}") from error
                if rendered.returncode != 0:
                    message = rendered.stderr.decode("utf-8", errors="replace").strip()
                    raise OcrBackendError(
                        "OCR_RENDER_ERROR",
                        f"Échec du rendu de la page {page_number} : {message or 'erreur inconnue'}",
                    )

                image_path = output_prefix.with_suffix(".png")
                if not image_path.is_file():
                    raise OcrBackendError(
                        "OCR_RENDER_ERROR", f"Image de la page {page_number} absente après rendu."
                    )
                if image_path.stat().st_size > MAX_RENDERED_PAGE_BYTES:
                    raise OcrBackendError(
                        "OCR_PAGE_TOO_LARGE", f"L'image de la page {page_number} est trop volumineuse."
                    )

                command = [availability["tesseract"], str(image_path), "stdout"]
                if self.language:
                    command.extend(["-l", self.language])
                try:
                    recognized = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self._remaining(deadline),
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    raise OcrBackendError("OCR_TIMEOUT", "L'OCR d'une page a expiré.") from error
                except OSError as error:
                    raise OcrBackendError("OCR_ENGINE_ERROR", f"Tesseract a échoué : {error}") from error
                if recognized.returncode != 0:
                    raise OcrBackendError(
                        "OCR_ENGINE_ERROR",
                        f"Échec OCR page {page_number} : {recognized.stderr.strip() or 'erreur inconnue'}",
                    )

                text = recognized.stdout.strip()
                total_characters += len(text)
                if total_characters > max_output_chars:
                    raise OcrBackendError("OCR_OUTPUT_TOO_LARGE", "La sortie OCR est trop volumineuse.")
                results[page_number] = text
        return results


def get_default_ocr_backend():
    return TesseractOcrBackend()
