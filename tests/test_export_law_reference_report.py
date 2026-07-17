from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_law_reference_report import export_law_reference_report, load_chunks_jsonl, summarize_law_references


class ExportLawReferenceReportTests(unittest.TestCase):
    def test_summarizes_internal_and_external_refs(self) -> None:
        summary = summarize_law_references(
            [
                {
                    "internal_regulation_refs": ["인사규정"],
                    "external_law_refs": ["국가재정법"],
                    "article_refs": ["제2조"],
                    "revision_history_spans": [{"event_count": 2}],
                    "article_validity_windows": [{"source": "article_effective_override"}],
                }
            ]
        )

        self.assertEqual(summary["chunks_with_internal_regulation_refs"], 1)
        self.assertEqual(summary["chunks_with_external_law_refs"], 1)
        self.assertEqual(summary["revision_history_span_count"], 1)
        self.assertEqual(summary["article_override_window_count"], 1)

    def test_exports_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks_path = root / "chunks.jsonl"
            chunks_path.write_text(
                json.dumps(
                    {
                        "chunk_id": "c1",
                        "external_law_refs": ["국가재정법"],
                        "internal_regulation_refs": ["인사규정"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            out_json = root / "report.json"
            report = export_law_reference_report(chunks_jsonl=chunks_path, out_json=out_json)

            self.assertTrue(out_json.is_file())
            self.assertEqual(report["summary"]["unique_external_law_ref_count"], 1)

    def test_loads_repository_json_array_chunk_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chunks.json"
            path.write_text(
                json.dumps([{"chunk_id": "c1"}, {"chunk_id": "c2"}], ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertEqual(["c1", "c2"], [item["chunk_id"] for item in load_chunks_jsonl(path)])

    def test_exports_batch_report_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_path = root / "batch.json"
            chunk_path = root / "data" / "exports" / "doc_a.jsonl"
            chunk_path.parent.mkdir(parents=True)
            chunk_path.write_text(
                json.dumps(
                    {
                        "document_id": "doc_a",
                        "internal_regulation_refs": ["인사규정"],
                        "external_law_refs": ["국가재정법"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            batch_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-04T00:00:00+00:00",
                        "input_count": 1,
                        "successful_count": 1,
                        "rows": [
                            {
                                "document_id": "doc_a",
                                "filename": "sample.hwp",
                                "status": "completed",
                                "quality_json": str(chunk_path.with_suffix(".quality.json")),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            out_json = root / "report.json"
            report = export_law_reference_report(batch_report_path=batch_path, out_json=out_json)

            self.assertEqual(report["document_count"], 1)
            self.assertEqual(report["summary"]["unique_internal_regulation_ref_count"], 1)
