from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class OperatorQuickstartKoTests(unittest.TestCase):
    def test_public_operator_quickstart_covers_safe_validation(self) -> None:
        quickstart = (REPO_ROOT / "docs" / "operator_quickstart_ko.md").read_text(encoding="utf-8")

        for phrase in [
            "# Public Operator Quickstart",
            "APP_ENV=",
            "API_AUTH_TOKEN",
            "DATA_DIR",
            "X-Tenant-Id",
            "Authorization: Bearer",
            "status=completed",
            "quality.passed=true",
            "audit_release_hygiene.py",
            "run_fresh_clone_rehearsal.py",
            "run_release_harness.py",
            "UNREVIEWED_PREVIEW",
            "UNREVIEWED_POC_REVIEW",
            "approved local regulation DB/vector index",
            "release evidence",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, quickstart)

        self.assertNotIn("private_release_runtime", quickstart)
        self.assertNotIn("data/public_portal_", quickstart)


if __name__ == "__main__":
    unittest.main()
