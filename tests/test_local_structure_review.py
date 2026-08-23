from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agents.local_structure_review import LocalStructureReviewAgent, apply_structure_review
from app.schemas.structure import StructureNode


class _Runtime:
    def __init__(self, payload: dict, *, available: bool = True) -> None:
        self.payload = payload
        self.available = available

    def model_available(self, model: str) -> bool:
        return self.available

    def generate_json(self, **kwargs):
        return self.payload, SimpleNamespace(duration_ms=17.5)


def _node(*, confidence: float = 0.7, warnings: list[str] | None = None) -> StructureNode:
    return StructureNode(
        node_id="node-3",
        document_id="doc-1",
        node_type="article",
        number="제3조",
        title="권한",
        text="제3조(권한) 관리자는 권한을 부여한다. 제4조(기록) 변경 내역을 기록한다.",
        order_index=3,
        confidence=confidence,
        warnings=["possible_merged_boundary"] if warnings is None else warnings,
    )


class LocalStructureReviewTests(unittest.TestCase):
    def test_accepts_only_exact_source_bound_finding(self) -> None:
        runtime = _Runtime(
            {
                "findings": [
                    {
                        "node_id": "node-3",
                        "risk_level": "high",
                        "issue_type": "merged_boundary",
                        "source_quote": "제4조(기록) 변경 내역을 기록한다.",
                        "reason": "별도 조문이 앞 조문에 병합되어 있습니다.",
                        "recommended_human_check": "원문에서 제4조 경계를 확인하십시오.",
                    }
                ]
            }
        )
        report = LocalStructureReviewAgent(runtime=runtime).review([_node()])

        self.assertEqual("review_required", report.status)
        self.assertEqual("qwen3:4b", report.model)
        updated = apply_structure_review([_node()], report)[0]
        self.assertIn("local_structure_review:merged_boundary", updated.warnings)
        self.assertEqual("review_required", updated.metadata["local_structure_review"]["status"])

    def test_invalid_quote_degrades_and_marks_candidate_for_review(self) -> None:
        runtime = _Runtime(
            {
                "findings": [
                    {
                        "node_id": "node-3",
                        "risk_level": "high",
                        "issue_type": "merged_boundary",
                        "source_quote": "원문에 없는 문장",
                        "reason": "경계 오류",
                        "recommended_human_check": "원문 확인",
                    }
                ]
            }
        )
        report = LocalStructureReviewAgent(runtime=runtime).review([_node()])

        self.assertEqual("degraded", report.status)
        updated = apply_structure_review([_node()], report)[0]
        self.assertIn("local_structure_review_unavailable", updated.warnings)

    def test_clean_confident_node_is_not_sent_to_model(self) -> None:
        report = LocalStructureReviewAgent(runtime=_Runtime({})).review(
            [_node(confidence=1.0, warnings=[])]
        )

        self.assertEqual("skipped", report.status)
        self.assertEqual(0, report.candidate_count)


if __name__ == "__main__":
    unittest.main()
