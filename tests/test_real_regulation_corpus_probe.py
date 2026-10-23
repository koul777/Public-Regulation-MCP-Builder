from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils.fitz_compat import fitz
from scripts.real_regulation_corpus_probe import PROJECT_ROOT, run_corpus_probe


class RealRegulationCorpusProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="corpus-probe-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_dir = self.root / "sources"
        self.source_dir.mkdir()
        self.source = self.source_dir / "sample.pdf"
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text(
            (50, 70),
            "표본 운영 규정\n"
            "제1조(목적) 이 규정은 표본 업무의 운영 기준을 정한다.\n"
            "제2조(보고) 담당자는 업무 종료 후 7일 이내에 보고한다.\n"
            "보고서는 담당 부서장에게 제출하여야 한다.\n"
            "제3조(보관) 담당자는 보고서를 3년간 보관하여야 한다.\n"
            "보고서에는 업무 기간 및 결과를 기록한다.",
            fontname="korea", fontsize=11,
        )
        pdf.save(self.source)
        pdf.close()
        self.fingerprint = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.row = {
            "document_id": "source-one", "file_type": "pdf", "page_count": 1,
            "source_sha256": self.fingerprint, "snapshot_pdf": str(self.source),
            "filename": "sample.pdf", "profile_id": "sample-profile", "tenant_id": "sample-tenant",
        }
        self.write_manifest([self.row])

    def write_manifest(self, rows: list[dict], **extra: object) -> None:
        self.manifest.write_text(json.dumps({"documents": rows, **extra}), encoding="utf-8")

    def test_real_pdf_pipeline_keeps_every_chunk_unapproved_and_unindexed(self) -> None:
        with patch("app.agents.review_executor._post_json", side_effect=AssertionError("No external review")) as provider:
            report = run_corpus_probe(corpus_manifest=self.manifest, output_dir=self.root / "probe")
        provider.assert_not_called()
        self.assertTrue(report["passed"])
        self.assertEqual(report["parsed_count"], 1)
        self.assertEqual(report["new_approvals"], 0)
        self.assertEqual(report["index_file_count"], 0)
        result = report["results"][0]
        self.assertGreater(result["chunk_count"], 0)
        self.assertEqual(result["review_required_count"], result["chunk_count"])
        self.assertEqual(result["index_admission_record_count"], 0)
        self.assertNotIn("approved", result["approval_status_counts"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.fingerprint)
        self.assertFalse(report["product_release_ready"])
        self.assertTrue((self.root / "probe/corpus-probe-report.json").is_file())

    def test_source_mismatch_is_rejected_before_creating_output(self) -> None:
        self.write_manifest([{**self.row, "source_sha256": "0" * 64}])
        destination = self.root / "mismatch"
        with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=destination)
        self.assertFalse(destination.exists())

    def test_existing_output_and_source_runtime_cannot_be_overwritten(self) -> None:
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "operator-data.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fresh directory"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.write_manifest([self.row], source_data_dir=str(self.source_dir))
        with self.assertRaisesRegex(ValueError, "source runtime"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=self.source_dir / "probe")

    def test_public_checkout_and_source_sibling_output_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the source checkout"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=PROJECT_ROOT / "probe-test-output")
        with self.assertRaisesRegex(ValueError, "separate from original PDF"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=self.source_dir / "nested")

    def test_unknown_selection_and_large_only_default_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "present in the corpus"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=self.root / "missing", selected_document_ids=["missing"])
        self.write_manifest([{**self.row, "page_count": 100}])
        with self.assertRaisesRegex(ValueError, "explicitly select"):
            run_corpus_probe(corpus_manifest=self.manifest, output_dir=self.root / "large")
        self.assertFalse((self.root / "large").exists())

    def test_default_reports_deferred_large_and_duplicate_sources(self) -> None:
        self.write_manifest([
            self.row,
            {**self.row, "document_id": "duplicate"},
            {**self.row, "document_id": "large", "page_count": 100},
        ])
        report = run_corpus_probe(corpus_manifest=self.manifest, output_dir=self.root / "selected")
        self.assertTrue(report["passed"])
        self.assertEqual(report["selected_document_count"], 1)
        self.assertEqual({row["reason"] for row in report["deferred_documents"]}, {
            "duplicate_source_sha256", "large_source_requires_explicit_selection",
        })

    def test_requested_kordoc_timeout_is_not_reported_as_success(self) -> None:
        with patch("app.processors.kordoc_table_parser.KordocTableParser.parse_file", return_value={
            "status": "timeout", "parser": "kordoc", "table_count": 0, "tables": [],
        }):
            report = run_corpus_probe(
                corpus_manifest=self.manifest, output_dir=self.root / "kordoc-timeout",
                enable_kordoc_table_parser=True,
            )
        self.assertFalse(report["passed"])
        self.assertEqual(report["parsed_count"], 1)
        self.assertEqual(report["failed_count"], 1)
        self.assertFalse(report["results"][0]["requested_table_parser_completed"])
        self.assertEqual(report["results"][0]["kordoc_table_summary"]["status"], "timeout")
        self.assertEqual(report["new_approvals"], 0)


if __name__ == "__main__":
    unittest.main()
