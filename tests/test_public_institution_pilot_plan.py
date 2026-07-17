from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PublicInstitutionPilotPlanTests(unittest.TestCase):
    def test_pilot_plan_covers_public_value_and_acceptance(self) -> None:
        plan = (REPO_ROOT / "docs" / "public_institution_pilot_plan.md").read_text(encoding="utf-8")

        for phrase in [
            "different from asking an AI directly",
            "deterministic preprocessing",
            "20 to 100",
            "public_batch_readiness_*.json/.md",
            "public_batch_quality_*.json/.md",
            "Raw `batch_quality_*`",
            "Acceptance Criteria",
            "OCR-required files",
            "Streamlit local-only",
            "legacy repository records without `tenant_id`",
            "auditable preprocessing and review gateway",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, plan)

        self.assertNotIn("private visibility", plan)
        self.assertNotIn("data/private_release_runtime", plan)


if __name__ == "__main__":
    unittest.main()
