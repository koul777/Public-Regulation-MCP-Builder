from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class LocalVerificationScriptTests(unittest.TestCase):
    ROOT = Path(__file__).parents[1]

    def test_source_checkout_verification_clis_show_help(self) -> None:
        for script_name in (
            "verify_local_model_roles.py",
            "verify_local_semantic_models.py",
            "verify_paddle_ocr_runtime.py",
            "verify_local_structure_review.py",
            "verify_local_table_review.py",
        ):
            with self.subTest(script=script_name):
                completed = subprocess.run(
                    [sys.executable, str(self.ROOT / "scripts" / script_name), "--help"],
                    cwd=self.ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn("usage:", completed.stdout.lower())

    def test_model_role_timeout_writes_structured_fail_closed_report(self) -> None:
        script_path = self.ROOT / "scripts" / "verify_local_model_roles.py"
        spec = importlib.util.spec_from_file_location("verify_local_model_roles_under_test", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "timeout.json"
            with patch.object(module, "verify", side_effect=TimeoutError("local model timed out")):
                with patch.object(sys, "argv", [str(script_path), "--output", str(output)]):
                    self.assertEqual(1, module.main())
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        self.assertEqual("TimeoutError", report["error_type"])
        self.assertIn("local model timed out", report["error"])


if __name__ == "__main__":
    unittest.main()
