from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PublicInstitutionAcceptanceDocsTests(unittest.TestCase):
    def test_pilot_plan_uses_public_source_only_evidence(self) -> None:
        plan = (REPO_ROOT / "docs" / "public_institution_pilot_plan.md").read_text(encoding="utf-8")

        for phrase in [
            "different from asking an AI directly",
            "deterministic preprocessing",
            "20 to 100",
            "public_batch_readiness_*.json/.md",
            "public_batch_quality_*.json/.md",
            "average_quality_score >= 98",
            "failed_count = 0",
            "ocr_required_count = 0",
            "api_call_count=0",
            "Streamlit local-only",
            "legacy repository records without `tenant_id`",
            "auditable preprocessing and review gateway",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, plan)

        self.assertNotIn("Private release", plan)
        self.assertNotIn("github_private", plan)

    def test_public_acceptance_matrix_names_review_fields_and_topology(self) -> None:
        matrix = (REPO_ROOT / "docs" / "pilot_acceptance_and_evidence_ko.md").read_text(encoding="utf-8")

        for phrase in [
            "public_batch_readiness_*.json/.md",
            "public_batch_quality_*.json/.md",
            "average_quality_score >= 98",
            "failed_count = 0",
            "ocr_required_count = 0",
            "reviewer",
            "단일 FastAPI container",
            "X-Tenant-Id",
            "Official Chain",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, matrix)

        self.assertNotIn("private visibility", matrix)
        self.assertNotIn("private_release", matrix)

    def test_operator_quickstart_has_public_authenticated_request_example(self) -> None:
        quickstart = (REPO_ROOT / "docs" / "operator_quickstart_ko.md").read_text(encoding="utf-8")

        for phrase in [
            "Authorization: Bearer",
            "X-Tenant-Id",
            "document_id",
            "job_id",
            "status=completed",
            "quality.passed=true",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, quickstart)


if __name__ == "__main__":
    unittest.main()
