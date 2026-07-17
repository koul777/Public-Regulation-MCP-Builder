from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_rag
from app.core.config import Settings
from app.mcp_server import regulation_tools
from app.mcp_server.regulation_server import _resolve_profile_scope
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.prepare_mcp_publish_runtime import prepare_mcp_publish_runtime


class InstitutionMcpContractTests(unittest.TestCase):
    def test_bound_mcp_profile_rejects_conflicting_requested_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "bound to a different institution profile"):
            _resolve_profile_scope("institution-b", "institution-a")

    def test_search_and_fetch_accept_same_as_of_date(self) -> None:
        as_of_date = "2025-01-02"
        settings = Settings(data_dir=Path(tempfile.mkdtemp()))
        auth = regulation_tools.mcp_auth_context(tenant_id="tenant-contract")
        record = {
            "document_id": "doc-contract",
            "chunk_id": "chunk-contract",
            "text": "approved regulation text",
            "document_name": "Contract Rules",
        }

        with patch.object(
            regulation_tools.routes_rag,
            "search_records",
            return_value=([], {"trace_id": "trace-contract", "timing_ms": {}}),
        ) as search_records, patch.object(
            regulation_tools,
            "_visible_record_by_chunk",
            return_value=record,
        ) as visible_record, patch.object(
            regulation_tools.routes_rag,
            "public_search_result",
            return_value=record,
        ):
            regulation_tools.search_regulations(
                settings=settings,
                auth=auth,
                query="contract query",
                as_of_date=as_of_date,
            )
            result_id = regulation_tools._encode_result_id(
                document_id=record["document_id"],
                chunk_id=record["chunk_id"],
            )
            regulation_tools.fetch_regulation(
                settings=settings,
                auth=auth,
                result_id=result_id,
                as_of_date=as_of_date,
            )

        self.assertEqual(as_of_date, search_records.call_args.kwargs["query"].as_of_date)
        self.assertEqual(as_of_date, visible_record.call_args.kwargs["as_of_date"])

    def test_unbound_mcp_requires_profile_when_tenant_has_multiple_institutions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            for profile_id in ("institution-a", "institution-b"):
                repository.upsert_document(
                    Document(
                        document_id=f"doc-{profile_id}",
                        filename=f"{profile_id}.pdf",
                        document_name=profile_id,
                        file_type="pdf",
                        file_hash=f"hash-{profile_id}",
                        tenant_id="tenant-contract",
                        profile_id=profile_id,
                        status="completed",
                    )
                )
            auth = regulation_tools.mcp_auth_context(tenant_id="tenant-contract")

            with self.assertRaisesRegex(ValueError, "profile_id is required"):
                regulation_tools.list_documents(settings=settings, auth=auth)

            scoped = regulation_tools.list_documents(
                settings=settings,
                auth=auth,
                profile_id="institution-a",
            )

        self.assertEqual([], scoped["documents"])

    def test_unbound_mcp_requires_profile_when_only_vectors_have_multiple_profiles(self) -> None:
        settings = Settings(data_dir=Path(tempfile.mkdtemp()))
        auth = regulation_tools.mcp_auth_context(tenant_id="tenant-vector-only")
        with patch.object(
            regulation_tools.routes_rag,
            "load_local_vector_records",
            return_value=[
                {"metadata": {"tenant_id": "tenant-vector-only", "profile_id": "institution-a"}},
                {"metadata": {"tenant_id": "tenant-vector-only", "profile_id": "institution-b"}},
            ],
        ):
            with self.assertRaisesRegex(ValueError, "profile_id is required"):
                regulation_tools._require_unambiguous_profile_scope(
                    settings=settings,
                    auth=auth,
                    profile_id=None,
                    inspect_vector_records=True,
                )

    def test_official_publish_rejects_missing_source_profile_without_explicit_profile_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_data_dir = root / "source"
            repository = JsonRepository(Settings(data_dir=source_data_dir))
            repository.upsert_document(
                Document(
                    document_id="doc-without-profile",
                    filename="rules.pdf",
                    document_name="Rules",
                    file_type="pdf",
                    file_hash="source-hash",
                    status="completed",
                )
            )

            with self.assertRaisesRegex(ValueError, "concrete profile_id is required"):
                prepare_mcp_publish_runtime(
                    source_data_dir=source_data_dir,
                    target_data_dir=root / "target",
                    source_document_id="doc-without-profile",
                )

    def test_malformed_approval_journal_row_is_treated_as_missing_provenance(self) -> None:
        chunk = Chunk(
            chunk_id="chunk-contract",
            document_id="doc-contract",
            chunk_type="article",
            text="approved regulation text",
            approval_id="approval-contract",
            approved_content_hash="approved-hash",
        )
        malformed_row = {
            "document_id": "doc-contract",
            "tenant_id": "tenant-contract",
            "approval_id": "approval-contract",
            "chunk_ids": ["chunk-contract"],
            "approved_content_hashes": {"chunk-contract": "approved-hash"},
            "worklist_evidence": [],
        }

        self.assertFalse(
            routes_rag._has_matching_approval_journal_record(
                [malformed_row],
                chunk=chunk,
                chunk_id="chunk-contract",
                document_id="doc-contract",
                tenant_id="tenant-contract",
                expected_metadata={},
            )
        )


if __name__ == "__main__":
    unittest.main()
