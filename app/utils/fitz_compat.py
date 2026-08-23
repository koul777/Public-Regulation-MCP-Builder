from __future__ import annotations

from types import ModuleType


def import_fitz() -> ModuleType:
    """Return a PyMuPDF-compatible module from either import path."""

    try:
        import fitz as backend  # type: ignore

        return backend
    except (ImportError, OSError):
        try:
            import pymupdf as backend  # type: ignore

            return backend
        except (ImportError, OSError) as pymupdf_error:
            raise ImportError(
                "PyMuPDF is not installed. Install package 'pymupdf'."
            ) from pymupdf_error


fitz = import_fitz()


__all__ = ["fitz", "import_fitz"]
