from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.operator_setup_service import kordoc_installer_guidance, run_kordoc_installer


class OperatorSetupServiceTests(unittest.TestCase):
    @patch("app.services.operator_setup_service.sys.platform", "win32")
    def test_installer_classifies_errors_and_discards_all_output(self) -> None:
        for outcome, expected in (
            (subprocess.TimeoutExpired("installer", 300), "installer_timeout"),
            (OSError("synthetic-secret"), "installer_unavailable"),
            (SimpleNamespace(returncode=1, stdout="synthetic-secret", stderr="synthetic-secret"), "installer_failed"),
            (SimpleNamespace(returncode=10, stdout="synthetic-secret", stderr="synthetic-secret"), "installer_command_mismatch"),
            (SimpleNamespace(returncode=11, stdout="synthetic-secret", stderr="synthetic-secret"), "installer_version_mismatch"),
            (SimpleNamespace(returncode=0, stdout="synthetic-secret", stderr="synthetic-secret"), ""),
        ):
            with self.subTest(expected=expected):
                runner = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                result = run_kordoc_installer([Path("synthetic/INSTALL_KORDOC_KO.ps1")], runner=runner)
                self.assertEqual(expected, result["error"])
                self.assertEqual(expected == "", result["ok"])
                self.assertEqual("", result["output"])
                self.assertNotIn("synthetic-secret", str(result))
                self.assertIn("-PersistUserPath", runner.call_args.args[0])
                self.assertFalse(runner.call_args.kwargs.get("shell", False))

    def test_missing_or_unsupported_setup_does_not_launch_process(self) -> None:
        runner = Mock()
        with patch("app.services.operator_setup_service.sys.platform", "linux"):
            self.assertEqual("windows_only", run_kordoc_installer([Path("install.ps1")], runner=runner)["error"])
        with patch("app.services.operator_setup_service.sys.platform", "win32"):
            self.assertEqual("installer_missing", run_kordoc_installer([], runner=runner)["error"])
        runner.assert_not_called()

    def test_guidance_is_specific_and_never_echoes_unknown_error(self) -> None:
        timeout = kordoc_installer_guidance("installer_timeout")
        missing = kordoc_installer_guidance("installer_missing")
        self.assertIn("시간", timeout[0])
        self.assertIn("배포본", missing[1])
        self.assertNotEqual(timeout, missing)
        self.assertNotIn("synthetic-secret", str(kordoc_installer_guidance("synthetic-secret")))
        self.assertIn("PATH", kordoc_installer_guidance("installer_command_mismatch")[1])
        self.assertIn("4.16.0", kordoc_installer_guidance("installer_version_mismatch")[0])
