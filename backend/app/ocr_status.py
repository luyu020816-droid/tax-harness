"""OCR has its own runtime and local, data-only model installation."""

import json
import os
import subprocess
from pathlib import Path

from app.errors import AppError


def options(settings, *, validate=False):
    if not settings.knowledge_ocr_enabled:
        return None
    executable = settings.knowledge_ocr_runtime / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    installation = settings.knowledge_ocr_model_directory / "installation.json"
    try:
        lock = Path(__file__).resolve().parents[1] / "ocr-models.lock.json"
        if not executable.is_file() or json.loads(
            installation.read_text(encoding="utf-8")
        ) != json.loads(lock.read_text(encoding="utf-8")):
            return None
        if validate:
            from app.ocr_pdf import verify_models

            verify_models(settings.knowledge_ocr_model_directory)
            required = {
                "paddlepaddle": "3.2.2",
                "paddleocr": "3.4.0",
                "paddlex": "3.4.3",
                "PyMuPDF": "1.26.7",
                "opencc-python-reimplemented": "0.1.7",
            }
            probe = subprocess.run(
                [
                    str(executable),
                    "-c",
                    "import importlib.metadata as m,json;print(json.dumps({k:m.version(k) for k in "
                    + repr(required)
                    + "}))",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if probe.returncode or json.loads(probe.stdout) != required:
                return None
    except (OSError, ValueError, KeyError, AppError, subprocess.TimeoutExpired):
        return None
    return {
        "python": str(executable),
        "models": str(settings.knowledge_ocr_model_directory),
        "max_pages": settings.knowledge_ocr_max_pages,
        "max_pixels": settings.knowledge_ocr_max_pixels,
        "min_confidence": settings.knowledge_ocr_min_confidence,
    }
