from __future__ import annotations

import unittest
from pathlib import Path
import subprocess
import sys

from scripts.run_image_pipeline_acceptance import QWEN3_EMBEDDING_DIMENSIONS, _is_loopback_address


class ImagePipelineAcceptanceGuardTests(unittest.TestCase):
    def test_loopback_addresses_are_allowed(self) -> None:
        for address in (("127.0.0.1", 11434), ("::1", 11434), ("localhost", 11434)):
            with self.subTest(address=address):
                self.assertTrue(_is_loopback_address(address))

    def test_external_addresses_are_rejected(self) -> None:
        for address in (("203.0.113.10", 443), ("models.example", 443)):
            with self.subTest(address=address):
                self.assertFalse(_is_loopback_address(address))

    def test_non_ip_local_transport_is_allowed(self) -> None:
        self.assertTrue(_is_loopback_address("/tmp/local.sock"))

    def test_source_checkout_cli_shows_help(self) -> None:
        root = Path(__file__).parents[1]
        completed = subprocess.run(
            [sys.executable, str(root / "scripts" / "run_image_pipeline_acceptance.py"), "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("usage:", completed.stdout.lower())

    def test_semantic_acceptance_uses_qwen_default_dimensions(self) -> None:
        self.assertEqual(1024, QWEN3_EMBEDDING_DIMENSIONS)


if __name__ == "__main__":
    unittest.main()
