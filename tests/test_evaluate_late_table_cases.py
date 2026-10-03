from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import evaluate_late_table_cases as late_tables


SCRIPT_PATH = Path(late_tables.__file__)


def _chunk(chunk_id: str, rows: list[str]) -> dict:
    return {
        "chunk_id": chunk_id,
        "chunk_type": "appendix",
        "metadata": {"table_rows": rows, "source_page_start": 3, "source_page_end": 3},
    }


class LateTableCaseEvaluationTests(unittest.TestCase):
    """비공개 코퍼스의 경로·기관·문서 식별자를 공개 소스에 두지 않는다."""

    def test_source_has_no_built_in_runtime_paths_or_institution_identifiers(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIsNone(re.search(r"tenant-[a-z0-9-]+", source))
        self.assertIsNone(re.search(r"doc_[0-9a-f]{12}", source))
        self.assertNotIn("overnight_runs", source)
        self.assertIsNone(re.search(r"(?i)\baks\b|aks_", source))
        self.assertFalse(hasattr(late_tables, "CASES"))

    def test_all_inputs_and_outputs_are_required(self) -> None:
        with self.assertRaises(SystemExit):
            late_tables.parse_args(["--chunks-json", "chunks.json"])

    def test_cases_come_from_the_operator_file(self) -> None:
        rows = ["가상 수당 지급 기준표", "구 분 | 대 상 | 금 액", "가족수당 | 배우자 | 40,000원", "가족수당 | 자녀 | 20,000원"]
        chunks = [_chunk("doc_x_appendix_p0003_001", rows), _chunk("doc_x_appendix_p0004_001", ["다른 표"])]
        cases = [
            {
                "case_id": "allowance",
                "case_label": "가상 수당 지급 기준표",
                "chunk_id_fragment": "p0003_001",
                "required_row_tokens": ["가상 수당 지급 기준표", "구 분"],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chunks_json = tmp_path / "corpus_chunks.json"
            cases_json = tmp_path / "late_table_cases.json"
            chunks_json.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            cases_json.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

            exit_code = late_tables.main(
                [
                    "--chunks-json", str(chunks_json),
                    "--cases-json", str(cases_json),
                    "--out-json", str(tmp_path / "out" / "report.json"),
                    "--out-md", str(tmp_path / "out" / "report.md"),
                ]
            )
            report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
            markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("late_table_cases_eval", report["report_type"])
        self.assertEqual(1, report["case_count"])
        self.assertEqual("doc_x_appendix_p0003_001", report["cases"][0]["chunk_id"])
        self.assertEqual("corpus_chunks.json", report["source_chunks_json"])
        self.assertNotIn(tmp, json.dumps(report, ensure_ascii=False))
        self.assertNotIn(tmp, markdown)
        self.assertIn("# Late Table Case Evaluation", markdown)

    def test_invalid_case_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            for payload in ([], [{"case_id": "x"}], [{"case_id": "x", "case_label": "x", "chunk_id_fragment": "x", "required_row_tokens": "x"}]):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        late_tables.load_cases(path)


if __name__ == "__main__":
    unittest.main()
