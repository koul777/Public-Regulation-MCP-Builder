from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_temporal_metadata_coverage import build_temporal_metadata_coverage_report


class AuditTemporalMetadataCoverageTests(unittest.TestCase):
    def test_reports_partial_temporal_coverage_and_inheritance_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_dir = root / "vector_db" / "default"
            vector_dir.mkdir(parents=True)
            records = [
                _record(
                    "chunk-article",
                    chunk_type="article",
                    regulation_title="Personnel Rule",
                    article_no="제1조",
                    effective_date="2026-01-01",
                ),
                _record(
                    "chunk-table",
                    chunk_type="appendix",
                    regulation_title="Personnel Rule",
                    article_no="",
                ),
                _record(
                    "chunk-other",
                    chunk_type="article",
                    regulation_title="Other Rule",
                    article_no="제2조",
                ),
            ]
            (vector_dir / "approved_vectors.jsonl").write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )
            out_json = root / "temporal.json"
            out_md = root / "temporal.md"

            report = build_temporal_metadata_coverage_report(
                data_dir=root,
                tenant_storage_isolation=False,
                out_json=out_json,
                out_md=out_md,
            )
            self.assertTrue(out_json.exists())
            markdown = out_md.read_text(encoding="utf-8")

        self.assertFalse(report["passed"])
        self.assertEqual(3, report["record_count"])
        self.assertEqual(1, report["with_temporal_metadata_count"])
        self.assertEqual(0.3333, report["temporal_metadata_ratio"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual(1, report["blocker_count"])
        self.assertIn("regulation-lifecycle-incomplete", [item["code"] for item in report["findings"]])
        self.assertIn("temporal-metadata-partial", [item["code"] for item in report["findings"]])
        self.assertEqual(1, report["inheritance_opportunities"]["candidate_scope_count"])
        self.assertEqual(1, report["inheritance_opportunities"]["candidate_missing_record_count"])
        self.assertIn("Temporal Metadata Coverage", markdown)

    def test_reports_complete_lifecycle_for_legacy_alias_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_dir = root / "vector_db" / "default"
            vector_dir.mkdir(parents=True)
            records = [
                _record(
                    "chunk-legacy-1",
                    chunk_type="article",
                    regulation_title="Personnel Rule",
                    article_no="제1조",
                    regulation_no="4-4-1",
                    revision_date="2025-01-02",
                    regulation_status="approved",
                ),
                _record(
                    "chunk-legacy-2",
                    chunk_type="article",
                    regulation_title="Personnel Rule",
                    article_no="제2조",
                    regulation_no="4-4-1",
                    revision_date="2025-01-02",
                    regulation_status="approved",
                ),
            ]
            (vector_dir / "approved_vectors.jsonl").write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_temporal_metadata_coverage_report(
                data_dir=root,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(2, report["lifecycle_complete_count"])
        self.assertEqual(1, report["regulation_group_count"])
        self.assertTrue(report["latest_only_passed"])

    def test_applies_document_defaults_to_title_like_rows_before_lifecycle_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_dir = root / "vector_db" / "default"
            vector_dir.mkdir(parents=True)
            records = [
                _record(
                    "chunk-stable",
                    chunk_type="article",
                    regulation_title="Personnel Rule",
                    article_no="제1조",
                    regulation_no="4-4-1",
                    revision_date="2025-01-02",
                    regulation_status="approved",
                ),
                _record(
                    "chunk-title",
                    chunk_type="paragraph",
                    regulation_title="Personnel Rule",
                    article_no="",
                ),
            ]
            (vector_dir / "approved_vectors.jsonl").write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_temporal_metadata_coverage_report(
                data_dir=root,
                tenant_storage_isolation=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(2, report["lifecycle_complete_count"])
        self.assertTrue(report["latest_only_passed"])
        self.assertEqual(1, report["regulation_group_count"])
        self.assertEqual(1, report["with_temporal_metadata_count"])
        self.assertEqual(1, report["without_temporal_metadata_count"])


def _record(
    chunk_id: str,
    *,
    chunk_type: str,
    regulation_title: str,
    article_no: str,
    effective_date: str | None = None,
    regulation_no: str | None = None,
    revision_date: str | None = None,
    regulation_status: str | None = None,
) -> dict:
    metadata = {
        "document_id": "doc-demo",
        "chunk_id": chunk_id,
        "chunk_type": chunk_type,
        "institution_name": "Demo Institution",
        "regulation_title": regulation_title,
        "article_no": article_no,
        "article_title": "Title",
        "source_page_start": 1,
    }
    if effective_date:
        metadata["effective_date"] = effective_date
        metadata["valid_from"] = effective_date
    if regulation_no:
        metadata["regulation_no"] = regulation_no
    if revision_date:
        metadata["revision_date"] = revision_date
    if regulation_status:
        metadata["regulation_status"] = regulation_status
    return {
        "id": f"doc-demo:{chunk_id}",
        "document_id": "doc-demo",
        "chunk_id": chunk_id,
        "text": "Demo text",
        "metadata": metadata,
        "content_hash": f"hash-{chunk_id}",
    }


if __name__ == "__main__":
    unittest.main()
