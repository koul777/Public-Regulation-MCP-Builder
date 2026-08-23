from __future__ import annotations

import unittest

from app.agents.citation_verifier import CitationVerifierAgent


class CitationVerifierTests(unittest.TestCase):
    def _evidence(self) -> list[dict[str, object]]:
        return [
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "regulation_title": "정보보안업무규정",
                "regulation_version": "3.2",
                "article_no": "제12조",
                "article_title": "접근권한 관리",
                "source_page_start": 14,
                "source_page_end": 14,
                "approval_id": "approval-1",
            }
        ]

    def test_builds_citation_only_from_retrieved_evidence(self) -> None:
        result = CitationVerifierAgent().run(
            {
                "answer": "접근권한은 관리되어야 합니다.",
                "evidence": self._evidence(),
                "evidence_ids": ["chunk-1"],
            }
        )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verified_evidence_ids"], ["chunk-1"])
        self.assertEqual(result["citations"][0]["article_no"], "제12조")
        self.assertEqual(result["citations"][0]["source_page_start"], 14)

    def test_rejects_answer_id_not_in_retrieved_evidence(self) -> None:
        result = CitationVerifierAgent().run(
            {
                "answer": "답변",
                "evidence": self._evidence(),
                "evidence_ids": ["chunk-not-found"],
            }
        )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("answer_evidence_id_not_in_retrieval", result["findings"])

    def test_no_evidence_answer_is_abstained(self) -> None:
        result = CitationVerifierAgent().run(
            {
                "answer": "승인된 규정 근거에서 확인할 수 없습니다.",
                "evidence": [],
                "evidence_ids": [],
            }
        )

        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["citations"], [])

    def test_redacts_answer_before_returning_verified_answer(self) -> None:
        result = CitationVerifierAgent().run(
            {
                "answer": "확인 C:\\private\\secret.txt",
                "evidence": self._evidence(),
            }
        )

        self.assertEqual(result["status"], "verified")
        self.assertNotIn("C:\\private", result["verified_answer"])


if __name__ == "__main__":
    unittest.main()
