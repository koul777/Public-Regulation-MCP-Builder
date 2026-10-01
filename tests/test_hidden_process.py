from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core import hidden_process
from app.core.hidden_process import hidden_window_options


CREATE_NO_WINDOW = 0x08000000


class HiddenProcessTests(unittest.TestCase):
    def test_windows_returns_create_no_window_creationflags(self) -> None:
        with patch.object(hidden_process, "_is_windows", return_value=True):
            options = hidden_window_options()

        self.assertEqual({"creationflags": CREATE_NO_WINDOW}, options)

    def test_non_windows_returns_no_options(self) -> None:
        with patch.object(hidden_process, "_is_windows", return_value=False):
            options = hidden_window_options()

        self.assertEqual({}, options)

    def test_platform_detection_follows_os_name(self) -> None:
        with patch.object(hidden_process.os, "name", "nt"):
            self.assertTrue(hidden_process._is_windows())
            self.assertEqual({"creationflags": CREATE_NO_WINDOW}, hidden_window_options())
        with patch.object(hidden_process.os, "name", "posix"):
            self.assertFalse(hidden_process._is_windows())
            self.assertEqual({}, hidden_window_options())

    def test_falls_back_to_documented_constant_when_subprocess_lacks_the_flag(self) -> None:
        # A non-Windows interpreter has no subprocess.CREATE_NO_WINDOW; the helper
        # must stay importable and callable there instead of raising AttributeError.
        with patch.object(hidden_process, "_is_windows", return_value=True), patch.object(
            hidden_process, "subprocess", SimpleNamespace()
        ):
            options = hidden_window_options()

        self.assertEqual({"creationflags": CREATE_NO_WINDOW}, options)

    def test_each_call_returns_an_independent_dict(self) -> None:
        with patch.object(hidden_process, "_is_windows", return_value=True):
            first = hidden_window_options()
            first["creationflags"] = 0
            second = hidden_window_options()

        self.assertEqual({"creationflags": CREATE_NO_WINDOW}, second)


if __name__ == "__main__":
    unittest.main()
