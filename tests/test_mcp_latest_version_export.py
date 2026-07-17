from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.mcp_server.regulation_tools import mcp_auth_context
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.generate_mcp_client_config import _runtime_visible_records_for_export


def _document(document_id: str, *, version: str, effective_from: str, status: str = "approved") -> Document:
    return Document(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        document_name="인사규정",
        file_type="pdf",
        file_hash=f"hash-{document_id}",
        institution_name="테스트 기관",
        source_system="LOCAL",
        source_url="https://example.test/regulation",
        profile_id="institution-a",
        regulation_id="reg-인사규정",
        regulation_version=version,
        revision_date=effective_from,
        effective_from=effective_from,
        regulation_status=status,
        tenant_id="tenant-a",
        status="completed",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _record(document_id: str, chunk_id: str) -> dict:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": "승인된 규정 본문",
        "metadata": {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "approval_status": "approved",
            "approval_id": f"approval-{chunk_id}",
            "approved_content_hash": f"approved-{chunk_id}",
            "approval_worklist_report_path": "reports/worklist.json",
            "approval_worklist_report_sha256": "a" * 64,
            "approval_review_batch_manifest_path": "reports/batch.json",
            "approval_review_batch_manifest_sha256": "b" * 64,
            "approval_review_batch_id": "batch-1",
            "approval_review_batch_chunk_fingerprint": "c" * 64,
            "approval_review_strategy": "human_review",
            "tenant_id": "tenant-a",
            "security_level": "internal",
            "department_acl": [],
        },
    }


class McpLatestVersionExportTests(unittest.TestCase):
    def test_institution_export_retains_approved_regulation_history_for_hierarchical_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(_document("doc-old", version="v1", effective_from="2024-01-01"))
            repository.upsert_document(_document("doc-new", version="v2", effective_from="2025-01-01"))
            records = [
                _record("doc-old", "old-1"),
                _record("doc-new", "new-1"),
                _record("doc-new", "new-2"),
            ]
            auth = mcp_auth_context(tenant_id="tenant-a", actor="tester", role="operator")

            with (
                patch("scripts.generate_mcp_client_config.routes_rag._load_local_vector_records", return_value=records),
                patch("scripts.generate_mcp_client_config.routes_rag._load_cached_approval_snapshot", return_value={}),
                patch("scripts.generate_mcp_client_config.routes_rag._record_visible_to_request", return_value=True),
            ):
                visible = _runtime_visible_records_for_export(
                    settings=settings,
                    auth=auth,
                    profile_id="institution-a",
                    document_id=None,
                )

        self.assertEqual({"doc-old", "doc-new"}, {record["document_id"] for record in visible})
        self.assertEqual({"old-1", "new-1", "new-2"}, {record["chunk_id"] for record in visible})

    def test_superseded_document_cannot_be_exported_even_when_stale_vector_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                _document("doc-old", version="v1", effective_from="2024-01-01", status="superseded").model_copy(
                    update={"effective_to": "2025-01-01"}
                )
            )
            records = [_record("doc-old", "old-1")]
            auth = mcp_auth_context(tenant_id="tenant-a", actor="tester", role="operator")

            with (
                patch("scripts.generate_mcp_client_config.routes_rag._load_local_vector_records", return_value=records),
                patch("scripts.generate_mcp_client_config.routes_rag._load_cached_approval_snapshot", return_value={}),
                patch("scripts.generate_mcp_client_config.routes_rag._record_visible_to_request", return_value=True),
            ):
                visible = _runtime_visible_records_for_export(
                    settings=settings,
                    auth=auth,
                    profile_id=None,
                    document_id="doc-old",
                )

        self.assertEqual([], visible)


if __name__ == "__main__":
    unittest.main()
