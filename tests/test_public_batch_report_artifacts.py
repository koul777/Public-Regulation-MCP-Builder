from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_BATCH_REPORTS = (
    REPO_ROOT / "reports" / "public_batch_quality_20260703-221530.json",
    REPO_ROOT / "reports" / "public_batch_quality_20260703-221536.json",
)


class PublicBatchReportArtifactTests(unittest.TestCase):
    def test_generated_public_batch_reports_are_not_committed(self) -> None:
        for report_path in PUBLIC_BATCH_REPORTS:
            with self.subTest(report=report_path.name):
                self.assertFalse(report_path.exists())

    def test_gitignore_does_not_reinclude_generated_reports(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        report_lines = [
            line.strip()
            for line in gitignore.splitlines()
            if line.strip().startswith("reports/") or line.strip().startswith("!reports/")
        ]

        self.assertIn("reports/*", report_lines)
        self.assertEqual([], [line for line in report_lines if line.startswith("!reports/")])


if __name__ == "__main__":
    unittest.main()
