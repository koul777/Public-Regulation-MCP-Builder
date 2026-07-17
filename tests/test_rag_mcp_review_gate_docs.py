from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "public_institution_pilot_plan.md",
    REPO_ROOT / "docs" / "pilot_acceptance_and_evidence_ko.md",
    REPO_ROOT / "docs" / "ui_ux_release_scope_ko.md",
    REPO_ROOT / "docs" / "public-institution-operations-runbook.md",
    REPO_ROOT / "docs" / "pilot_overview_ko.md",
]


class RagMcpReviewGateDocsTests(unittest.TestCase):
    def test_docs_do_not_describe_preprocessing_outputs_as_direct_rag_inputs(self) -> None:
        shortcuts = (
            "RAG-ready",
            "ready for downstream RAG ingestion",
            "ready for RAG ingestion",
            "downstream RAG ingestion",
        )
        for path in DOC_PATHS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                for phrase in shortcuts:
                    self.assertNotIn(phrase, text)

    def test_docs_keep_official_review_approval_indexing_chain(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        pilot = (REPO_ROOT / "docs" / "public_institution_pilot_plan.md").read_text(encoding="utf-8")
        matrix = (REPO_ROOT / "docs" / "pilot_acceptance_and_evidence_ko.md").read_text(encoding="utf-8")
        ui_scope = (REPO_ROOT / "docs" / "ui_ux_release_scope_ko.md").read_text(encoding="utf-8")
        runbook = (REPO_ROOT / "docs" / "public-institution-operations-runbook.md").read_text(encoding="utf-8")
        overview = (REPO_ROOT / "docs" / "pilot_overview_ko.md").read_text(encoding="utf-8")

        self.assertIn("사람 검수", readme)
        self.assertIn("승인된 규정만 MCP 데이터로 생성", readme)
        self.assertIn("Official indexing starts only after human review", pilot)
        self.assertIn("Preprocessing output is preview/schema validation only", matrix)
        self.assertIn("preview-only", ui_scope)
        self.assertIn("Review handoff", runbook)
        self.assertIn("approved vector DB", overview)


if __name__ == "__main__":
    unittest.main()
