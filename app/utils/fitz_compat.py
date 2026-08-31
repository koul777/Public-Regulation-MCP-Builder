from __future__ import annotations

from types import ModuleType


def _has_required_api(backend: ModuleType) -> bool:
    """Return whether a candidate PyMuPDF module exposes the API we rely on."""

    return callable(getattr(backend, "open", None)) and hasattr(backend, "Matrix")


def import_fitz() -> ModuleType:
    """Return PyMuPDF through its current name, with the legacy alias as fallback."""

    try:
        import pymupdf as backend  # type: ignore

        if _has_required_api(backend):
            return backend
    except (ImportError, OSError):
        backend = None

    try:
        import fitz as backend  # type: ignore

        if _has_required_api(backend):
            return backend
    except (ImportError, OSError) as fitz_error:
        raise ImportError(
            "PyMuPDF is not installed. Install package 'pymupdf'."
        ) from fitz_error

    raise ImportError("PyMuPDF is installed but does not expose the required API.")


fitz = import_fitz()


__all__ = ["fitz", "import_fitz"]
