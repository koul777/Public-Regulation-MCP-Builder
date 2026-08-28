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

    def test_citations_are_limited_to_answer_requested_evidence_ids(self) -> None:
        evidence = [
            *self._evidence(),
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-2",
                "regulation_title": "정보보안업무규정",
                "article_no": "제13조",
                "article_title": "접근권한 점검",
                "approval_id": "approval-2",
            },
        ]

        result = CitationVerifierAgent().run(
            {
                "answer": "제12조를 근거로 답변합니다.",
                "evidence": evidence,
                "evidence_ids": ["chunk-1"],
            }
        )

        self.assertEqual("verified", result["status"])
        self.assertEqual(["chunk-1"], result["verified_evidence_ids"])
        self.assertEqual(["chunk-1"], [item["chunk_id"] for item in result["citations"]])

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

    def test_abstained_answer_never_cites_retrieved_but_unused_evidence(self) -> None:
        result = CitationVerifierAgent().run(
            {
                "answer": "승인된 규정 근거에서 확인할 수 없습니다.",
                "evidence": self._evidence(),
                "evidence_ids": ["chunk-1"],
            }
        )

        self.assertEqual("abstained", result["status"])
        self.assertEqual([], result["citations"])
        self.assertEqual([], result["verified_evidence_ids"])
        self.assertIn("answer_abstained", result["findings"])

    def test_redacts_answer_before_returning_verified_answer(self) -> None:
        result = CitationVerifierAgent().run(
            {
                "answer": "확인 C:\\private\\secret.txt",
                "evidence": self._evidence(),
            }
        )

        self.assertEqual(result["status"], "verified")
        self.assertNotIn("C:\\private", result["verified_answer"])

    def test_rejects_explicitly_non_approved_or_malformed_evidence(self) -> None:
        draft = self._evidence()
        draft[0]["approval_status"] = "pending_review"

        with self.assertRaisesRegex(ValueError, "non-approved"):
            CitationVerifierAgent().run(
                {
                    "answer": "답변",
                    "evidence": draft,
                    "evidence_ids": ["chunk-1"],
                }
            )
        with self.assertRaisesRegex(ValueError, "items must be objects"):
            CitationVerifierAgent().run(
                {
                    "answer": "답변",
                    "evidence": [*self._evidence(), "not-an-object"],
                    "evidence_ids": ["chunk-1"],
                }
            )


if __name__ == "__main__":
    unittest.main()
