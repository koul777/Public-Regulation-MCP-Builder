from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_approval_evidence import build_approval_evidence, main


class BuildApprovalEvidenceTests(unittest.TestCase):
    def test_builds_worklist_and_review_batch_manifest_in_one_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                        metadata={"article_no": "1", "article_title": "Purpose"},
                    ),
                    Chunk(
                        chunk_id="attention-1",
                        document_id="doc-a",
                        chunk_type="table",
                        text="table",
                        metadata={"table_review_required": True, "table_review_flags": ["row_review_required"]},
                    ),
                ],
            )

            report = build_approval_evidence(
                data_dir=settings.data_dir,
                source_system="PUBLIC_PORTAL",
                apba_id="C0001",
                out_prefix=root / "reports" / "approval_evidence_current",
                max_chunks_per_batch=1,
            )
            worklist_json_exists = Path(report["artifacts"]["worklist_json"]).is_file()
            worklist_csv_exists = Path(report["artifacts"]["worklist_csv"]).is_file()
            review_batches_json_exists = Path(report["artifacts"]["review_batches_json"]).is_file()
            review_batches_csv_exists = Path(report["artifacts"]["review_batches_csv"]).is_file()

        self.assertTrue(report["passed"])
        self.assertIn("does not approve chunks", report["safety_note"])
        self.assertEqual(1, report["worklist_summary"]["document_count"])
        self.assertEqual(2, report["review_batch_summary"]["batch_count"])
        self.assertEqual(2, report["review_batch_summary"]["approval_chunk_count"])
        self.assertTrue(worklist_json_exists)
        self.assertTrue(worklist_csv_exists)
        self.assertTrue(review_batches_json_exists)
        self.assertTrue(review_batches_csv_exists)
        self.assertEqual(64, len(report["artifacts"]["worklist_sha256"]))
        self.assertEqual(64, len(report["artifacts"]["review_batches_sha256"]))
        self.assertEqual(2, len(report["next_steps"]))
        manual_step = next(step for step in report["next_steps"] if step["review_type"] == "manual_attention")
        self.assertTrue(manual_step["review_flags_acknowledged_required"])
        self.assertEqual(report["artifacts"]["worklist_sha256"], manual_step["worklist_report_sha256"])
        self.assertEqual(report["artifacts"]["review_batches_sha256"], manual_step["review_batch_manifest_sha256"])
        self.assertIn("manually acknowledge parser/table flags", manual_step["streamlit_action"])

    def test_cli_writes_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _seed_document(
                settings,
                [
                    Chunk(
                        chunk_id="clean-1",
                        document_id="doc-a",
                        chunk_type="article",
                        text="clean one",
                    )
                ],
            )
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--data-dir",
                    str(settings.data_dir),
                    "--source-system",
                    "PUBLIC_PORTAL",
                    "--out-prefix",
                    str(root / "reports" / "approval_evidence_cli"),
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )

        self.assertEqual(0, exit_code)
        self.assertIn('"approval_evidence_bundle"', stdout.getvalue())
        self.assertIn('"review_batch_manifest_path"', stdout.getvalue())


def _seed_document(settings: Settings, chunks: list[Chunk]) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc-a",
            filename="a.pdf",
            document_name="A",
            file_type="pdf",
            file_hash="hash-a",
            institution_name="Institution A",
            apba_id="C0001",
            source_system="PUBLIC_PORTAL",
            source_record_id="record-a",
            profile_id="public_portal-c0001",
        )
    )
    repository.save_processing_result("doc-a", [], chunks, [])


if __name__ == "__main__":
    unittest.main()
