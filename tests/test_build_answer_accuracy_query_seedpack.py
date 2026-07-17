import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.build_answer_accuracy_query_seedpack import (
    build_answer_accuracy_query_seedpack,
    main,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _record(chunk_id: str, chunk_type: str, **metadata: object) -> dict:
    return {
        "document_id": "doc-a",
        "chunk_id": chunk_id,
        "text": "sample",
        "metadata": {
            "chunk_type": chunk_type,
            "regulation_title": "계약업무규정",
            **metadata,
        },
    }


class BuildAnswerAccuracyQuerySeedpackTests(unittest.TestCase):
    def test_builds_answerable_and_no_evidence_query_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "article-1",
                        "article",
                        article_no="제1조",
                        article_title="목적",
                        article_refs=["제2조"],
                    ),
                    _record(
                        "appendix-1",
                        "appendix",
                        table_citation_label="별표1 평가기준",
                        table_appendix_no="별표1",
                        article_refs=["제1조"],
                    ),
                    _record(
                        "appendix-2",
                        "appendix",
                    ),
                    _record(
                        "form-1",
                        "form",
                        table_citation_label="별지 제1호 서식",
                        form_refs=["별지 제1호"],
                    ),
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=3,
                no_evidence_control_count=2,
            )

        self.assertEqual(5, report["query_spec_count"])
        self.assertEqual(3, report["answerable_query_count"])
        self.assertEqual(2, report["no_evidence_control_count"])
        self.assertEqual(2, sum(1 for item in report["query_specs"] if item["expect_no_evidence"]))
        answerable_types = [
            item["target_chunk_type"] for item in report["query_specs"] if not item["expect_no_evidence"]
        ]
        self.assertEqual(["article", "appendix", "form"], answerable_types)
        self.assertEqual(["제1조", "목적"], report["query_specs"][0]["expected_terms"])
        self.assertEqual(["제1조"], report["query_specs"][0]["expected_article_nos"])
        self.assertEqual(["제1조"], report["query_specs"][1]["expected_article_nos"])
        self.assertIn("별표1 평가기준", report["query_specs"][1]["query"])

    def test_supplementary_provision_query_includes_target_article_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "supp-1",
                        "supplementary_provision",
                        article_refs=["제44조제2항", "제1조"],
                    )
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=1,
                no_evidence_control_count=0,
            )

        spec = report["query_specs"][0]
        self.assertIn("제44조제2항", spec["query"])
        self.assertEqual(["제44조제2항"], spec["expected_article_nos"])

    def test_deduplicates_split_form_chunks_and_keeps_governing_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "form_0430_p1_001",
                        "form",
                        table_citation_label="별지 제27호서식] (제41조의3제2항 관련)",
                        table_appendix_no="별지",
                        form_refs=["별지제27호서식"],
                        article_refs=["제41조의3제2항", "제1조"],
                    ),
                    _record(
                        "form_0430_p1_014",
                        "form",
                        table_citation_label="별지제27호서식 (제41조의3제2항 관련)",
                        table_appendix_no="별지제27호서식",
                        article_refs=["제4조", "제6조", "제7조제1항"],
                    ),
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=2,
                no_evidence_control_count=0,
            )

        self.assertEqual(1, report["answerable_query_count"])
        spec = report["query_specs"][0]
        self.assertEqual("form_0430_p1_001", spec["target_chunk_id"])
        self.assertEqual(["제41조의3제2항"], spec["expected_article_nos"])
        self.assertNotIn("제4조", spec["expected_terms"])

    def test_form_label_governing_ref_precedes_internal_body_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "form_0430_p1_014",
                        "form",
                        table_citation_label="별지제27호서식 (제41조의3제2항 관련)",
                        table_appendix_no="별지제27호서식",
                        article_refs=["제4조", "제6조"],
                    )
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=1,
                no_evidence_control_count=0,
            )

        spec = report["query_specs"][0]
        self.assertEqual(["제41조의3제2항"], spec["expected_article_nos"])
        self.assertIn("제41조의3제2항", spec["expected_terms"])
        self.assertNotIn("제4조", spec["expected_terms"])

    def test_form_ref_builds_specific_query_when_citation_label_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "form_0405_p1_001",
                        "form",
                        form_refs=["별지제2호서식"],
                        article_refs=["제5조제1항"],
                    )
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=1,
                no_evidence_control_count=0,
            )

        query = report["query_specs"][0]["query"]
        self.assertIn("별지제2호서식", query)
        self.assertIn("제5조제1항", query)

    def test_appendix_query_includes_appendix_title_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "appendix_0500_p1_001",
                        "appendix",
                        table_appendix_no="별지제7호서식",
                        table_appendix_title="연구과제 선정",
                        table_citation_label="별지제7호서식 (제3조 관련)",
                        article_refs=["제3조"],
                    )
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=1,
                no_evidence_control_count=0,
            )

        query = report["query_specs"][0]["query"]
        self.assertIn("별지제7호서식", query)
        self.assertIn("연구과제 선정", query)

    def test_prefers_appendix_header_over_metadata_empty_body_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "appendix_0403_p1_002",
                        "appendix",
                        hierarchy_path="제8장 > 별표3 제안서 평가항목 (제26조제2항 관련)",
                    ),
                    _record(
                        "appendix_0403_p1_001",
                        "appendix",
                        hierarchy_path="제8장 > 별표3 제안서 평가항목 (제26조제2항 관련)",
                        table_citation_label="별표3 제안서 평가항목 (제26조제2항 관련)",
                        table_appendix_no="별표3",
                        appendix_refs=["별표3"],
                        article_refs=["제26조제2항"],
                    ),
                ],
            )

            report = build_answer_accuracy_query_seedpack(
                approved_vectors_jsonl=vectors,
                target_answerable_count=2,
                no_evidence_control_count=0,
            )

        self.assertEqual(1, report["answerable_query_count"])
        self.assertEqual("appendix_0403_p1_001", report["query_specs"][0]["target_chunk_id"])

    def test_cli_writes_report_and_query_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            out_report = root / "report.json"
            out_md = root / "report.md"
            out_json = root / "queries.json"
            out_csv = root / "queries.csv"
            _write_jsonl(
                vectors,
                [
                    _record(
                        "article-1",
                        "article",
                        article_no="제2조",
                        article_title="정의",
                    )
                ],
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--approved-vectors-jsonl",
                        str(vectors),
                        "--target-answerable-count",
                        "1",
                        "--no-evidence-control-count",
                        "1",
                        "--out-report-json",
                        str(out_report),
                        "--out-md",
                        str(out_md),
                        "--out-query-spec-json",
                        str(out_json),
                        "--out-query-spec-csv",
                        str(out_csv),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(2, len(json.loads(out_json.read_text(encoding="utf-8"))))
            self.assertIn("Answer Accuracy Query Seedpack", out_md.read_text(encoding="utf-8"))
            self.assertIn("expect_no_evidence", out_csv.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
