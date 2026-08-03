from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import check_windows_venv_recreate_target as recreate_target


class WindowsVenvRecreateTargetTests(unittest.TestCase):
    def test_missing_target_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / ".venv"
            self.assertTrue(recreate_target.is_safe_recreate_target(target))

    def test_normal_directory_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / ".venv"
            target.mkdir()
            self.assertTrue(recreate_target.is_safe_recreate_target(target))

    def test_regular_file_is_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / ".venv"
            target.write_text("not a directory", encoding="utf-8")
            self.assertFalse(recreate_target.is_safe_recreate_target(target))

    def test_reparse_directory_is_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / ".venv"
            target.mkdir()
            reparse_point_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
            fake_stat = SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=reparse_point_flag,
            )
            with patch.object(recreate_target.os, "lstat", return_value=fake_stat):
                self.assertFalse(recreate_target.is_safe_recreate_target(target))

    def test_cli_returns_nonzero_for_unsafe_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / ".venv"
            target.write_text("not a directory", encoding="utf-8")
            exit_code = recreate_target.main(["--path", os.fspath(target)])

        self.assertEqual(1, exit_code)


if __name__ == "__main__":
    unittest.main()
