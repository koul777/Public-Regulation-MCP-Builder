from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PilotKoreanDocsTests(unittest.TestCase):
    def test_korean_pilot_overview_answers_ai_only_question(self) -> None:
        overview = (REPO_ROOT / "docs" / "pilot_overview_ko.md").read_text(encoding="utf-8")

        for phrase in [
            "private preprocessing/review gateway",
            "provider API",
            "approved vector DB",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, overview)

    def test_public_acceptance_matrix_names_release_evidence(self) -> None:
        matrix = (REPO_ROOT / "docs" / "pilot_acceptance_and_evidence_ko.md").read_text(encoding="utf-8")

        for phrase in [
            "public_batch_readiness_*.json/.md",
            "public_batch_quality_*.json/.md",
            "approval journal",
            "X-Tenant-Id",
            "source-only",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, matrix)

        self.assertNotIn("reports/private_release_gate_current.json", matrix)
        self.assertNotIn("reports/github_private_visibility_current.json", matrix)


if __name__ == "__main__":
    unittest.main()
