from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.utils.fitz_compat import import_fitz


class FitzCompatTests(unittest.TestCase):
    def test_current_pymupdf_import_path_is_preferred(self) -> None:
        current_backend = SimpleNamespace(open=object())
        original_import = __import__
        imported_names: list[str] = []

        def import_current_first(name: str, *args, **kwargs):
            imported_names.append(name)
            if name == "pymupdf":
                return current_backend
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_current_first):
            self.assertIs(import_fitz(), current_backend)

        self.assertNotIn("fitz", imported_names)

    def test_legacy_fitz_alias_is_used_when_pymupdf_name_is_unavailable(self) -> None:
        fallback_backend = SimpleNamespace(open=object())
        original_import = __import__

        def import_with_missing_pymupdf(name: str, *args, **kwargs):
            if name == "pymupdf":
                raise ImportError("pymupdf name unavailable")
            if name == "fitz":
                return fallback_backend
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_with_missing_pymupdf):
            self.assertIs(import_fitz(), fallback_backend)

    def test_missing_current_and_legacy_modules_raise_actionable_error(self) -> None:
        original_import = __import__

        def import_without_pymupdf(name: str, *args, **kwargs):
            if name in {"pymupdf", "fitz"}:
                raise ImportError(f"{name} unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_pymupdf):
            with self.assertRaisesRegex(ImportError, "Install package 'pymupdf'"):
                import_fitz()


if __name__ == "__main__":
    unittest.main()
