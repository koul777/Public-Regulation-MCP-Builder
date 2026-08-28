from __future__ import annotations

from types import ModuleType


def import_fitz() -> ModuleType:
    """Return PyMuPDF through its current name, with the legacy alias as fallback."""

    try:
        import pymupdf as backend  # type: ignore

        return backend
    except (ImportError, OSError):
        try:
            import fitz as backend  # type: ignore

            return backend
        except (ImportError, OSError) as fitz_error:
            raise ImportError(
                "PyMuPDF is not installed. Install package 'pymupdf'."
            ) from fitz_error


fitz = import_fitz()


__all__ = ["fitz", "import_fitz"]
