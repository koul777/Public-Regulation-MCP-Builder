from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agents.local_table_review import LocalTableReviewAgent, apply_table_review
from app.schemas.structure import StructureNode


class _Runtime:
    def __init__(self, payload: dict, *, available: bool = True) -> None:
        self.payload = payload
        self.available = available

    def model_available(self, model: str) -> bool:
        return self.available

    def generate_json(self, **kwargs):
        return self.payload, SimpleNamespace(duration_ms=11.0)


def _table() -> StructureNode:
    return StructureNode(
        node_id="table-1",
        document_id="doc-1",
        node_type="table",
        title="접근권한 점검주기",
        text="등급 | 점검주기\n일반 | 분기\n중요 | 월 | 담당부서",
        page_start=3,
        page_end=3,
        order_index=7,
        confidence=0.8,
    )


class LocalTableReviewTests(unittest.TestCase):
    def test_accepts_exact_source_bound_finding_and_preserves_table(self) -> None:
        runtime = _Runtime(
            {
                "findings": [
                    {
                        "table_node_id": "table-1",
                        "risk_level": "high",
                        "issue_type": "inconsistent_columns",
                        "source_quote": "중요 | 월 | 담당부서",
                        "reason": "다른 행보다 열이 하나 많습니다.",
                        "recommended_human_check": "원문에서 병합 셀 여부를 확인하십시오.",
                    }
                ]
            }
        )
        source = _table()
        report = LocalTableReviewAgent(runtime=runtime).review([source])
        updated = apply_table_review([source], report)[0]

        self.assertEqual("review_required", report.status)
        self.assertEqual("qwen3:4b", report.model)
        self.assertEqual(source.text, updated.text)
        self.assertIn("local_table_review:inconsistent_columns", updated.warnings)

    def test_unknown_quote_degrades_fail_closed(self) -> None:
        runtime = _Runtime(
            {
                "findings": [
                    {
                        "table_node_id": "table-1",
                        "risk_level": "high",
                        "issue_type": "missing_header",
                        "source_quote": "원문에 없는 셀",
                        "reason": "헤더가 없습니다.",
                        "recommended_human_check": "원문을 확인하십시오.",
                    }
                ]
            }
        )
        report = LocalTableReviewAgent(runtime=runtime).review([_table()])
        updated = apply_table_review([_table()], report)[0]

        self.assertEqual("degraded", report.status)
        self.assertIn("local_table_review_unavailable", updated.warnings)

    def test_non_table_nodes_are_not_sent_to_model(self) -> None:
        node = _table().model_copy(update={"node_type": "article"})
        report = LocalTableReviewAgent(runtime=_Runtime({})).review([node])

        self.assertEqual("skipped", report.status)
        self.assertEqual(0, report.candidate_count)


if __name__ == "__main__":
    unittest.main()
