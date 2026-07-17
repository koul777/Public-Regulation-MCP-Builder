import json
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.storage.repository import JsonRepository
from scripts.backfill_approval_review_events import build_approval_review_event_backfill_report


class BackfillApprovalReviewEventsTests(unittest.TestCase):
    def test_appends_superseding_correction_record_without_rewriting_source_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            repository = JsonRepository(settings)
            source = {
                "approval_record_id": "approval-record-source",
                "approval_id": "approval-source",
                "document_id": "doc-a",
                "tenant_id": "default",
                "chunk_ids": ["chunk-a", "chunk-b"],
                "approved_content_hashes": {"chunk-a": "hash-a", "chunk-b": "hash-b"},
                "approved_chunks": [
                    {"chunk_id": "chunk-a", "approved_content_hash": "hash-a"},
                    {"chunk_id": "chunk-b", "approved_content_hash": "hash-b"},
                ],
                "approved_by": "operator",
                "approved_at": "2026-07-12T00:00:00+00:00",
                "human_review_confirmed": True,
                "ai_review_confirmed": True,
                "review_decision_events": [
                    {
                        "event": "approved",
                        "timestamp": "2026-07-12T00:00:00+00:00",
                        "actor": "operator",
                        "chunk_id": "chunk-a",
                    }
                ],
            }
            repository.append_approval_record(source)

            report = build_approval_review_event_backfill_report(
                data_dir=settings.data_dir,
                actor="operator",
                approval_reference="unit-test",
                apply=True,
            )
            journal = repository.list_approval_journal_records("doc-a")

        self.assertTrue(report["applied"])
        self.assertEqual(1, report["candidate_count"])
        self.assertEqual(1, report["correction_count"])
        self.assertEqual(2, len(journal))
        self.assertEqual("approval-record-source", journal[0]["approval_record_id"])
        correction = journal[1]
        self.assertEqual(["approval-record-source"], correction["supersedes_approval_record_ids"])
        self.assertEqual(6, len(correction["review_decision_events"]))
        self.assertEqual(
            {"ai_review_confirmed": 2, "human_review_confirmed": 2, "approved": 2},
            correction["review_decision_event_counts"],
        )


if __name__ == "__main__":
    unittest.main()
