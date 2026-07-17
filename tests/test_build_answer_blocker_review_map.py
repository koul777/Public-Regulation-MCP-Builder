from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_answer_blocker_review_map import build_answer_blocker_review_map, main


class BuildAnswerBlockerReviewMapTests(unittest.TestCase):
    def test_maps_failed_demo_answers_to_review_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            demo_json = _seed_demo_answers(root)

            report = build_answer_blocker_review_map(
                demo_answers_json=demo_json,
                out_json=root / "reports" / "map.json",
                out_csv=root / "reports" / "map.csv",
                out_md=root / "reports" / "map.md",
                table_unit_review_csv=Path("reports/table_units.csv"),
                table_source_traceability_report=Path("reports/table_traceability.json"),
                table_risk_csv=Path("reports/table_risk.csv"),
                review_triage_csv=Path("reports/triage.csv"),
                query_spec_path=Path("config/query_specs.json"),
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "map.json").read_text(encoding="utf-8"))
            with (root / "reports" / "map.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "map.md").read_text(encoding="utf-8")

        self.assertEqual(4, report["query_count"])
        self.assertEqual(3, report["failed_query_count"])
        self.assertEqual(3, report["quality_issue_count"])
        self.assertEqual(1, report["blocker_category_counts"]["table_parentage_or_structure_review"])
        self.assertEqual(1, report["blocker_category_counts"]["form_parentage_review"])
        self.assertEqual(1, report["blocker_category_counts"]["query_goldset_or_citation_metadata_review"])
        self.assertEqual(3, len(payload["rows"]))
        self.assertEqual("table_parentage_or_structure_review", rows[0]["blocker_category"])
        self.assertIn("table_units.csv", rows[0]["recommended_artifacts"])
        self.assertIn("table_traceability.json", rows[0]["recommended_artifacts"])
        self.assertEqual("", rows[0]["human_resolution"])
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertIn("Answer Blocker Review Map", markdown)

    def test_cli_writes_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            demo_json = _seed_demo_answers(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--demo-answers-json",
                        str(demo_json),
                        "--out-json",
                        str(root / "reports" / "map.json"),
                        "--out-csv",
                        str(root / "reports" / "map.csv"),
                        "--out-md",
                        str(root / "reports" / "map.md"),
                        "--review-triage-csv",
                        "reports/triage.csv",
                    ]
                )

            payload = json.loads((root / "reports" / "map.json").read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn('"ok": true', stdout.getvalue())
        self.assertEqual(3, payload["failed_query_count"])


def _seed_demo_answers(root: Path) -> Path:
    payload = {
        "report_type": "mcp_demo_answers",
        "items": [
            {
                "query": "연구직 임용자격기준표는 별표 2-1로 정하나요?",
                "passed": False,
                "quality_issue_count": 1,
                "quality_issues": [
                    {
                        "code": "expected-term-coverage-low",
                        "missing_terms": ["별표 2-1", "연구직 임용자격기준표"],
                    }
                ],
                "citations": [{"chunk_id": "doc_appendix_별표2_001", "article_no": "", "article_title": ""}],
            },
            {
                "query": "휴직자 국외 출국 신고서는 언제 제출하나요?",
                "passed": False,
                "quality_issue_count": 1,
                "quality_issues": [
                    {
                        "code": "expected-article-title-missing",
                        "missing_values": ["휴직자의 복무실태 점검"],
                    }
                ],
                "citations": [{"chunk_id": "doc_form_별지제6호_001", "article_no": "제12조", "article_title": "다른 조항"}],
            },
            {
                "query": "강사 신규임용의 원칙은 무엇인가요?",
                "passed": False,
                "quality_issue_count": 1,
                "quality_issues": [
                    {
                        "code": "expected-article-no-missing",
                        "missing_values": ["제3조"],
                    }
                ],
                "citations": [{"chunk_id": "doc_article_제6조_001", "article_no": "제6조", "article_title": "임용원칙"}],
            },
            {
                "query": "근거 없는 질문",
                "passed": True,
                "quality_issue_count": 0,
                "quality_issues": [],
                "citations": [],
            },
        ],
    }
    path = root / "demo_answers.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
