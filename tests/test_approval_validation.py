from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.ingestion.vector_adapter import vector_record_from_chunk, with_vector_record_verification
from app.processors.exporter import Exporter
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.services.approval_validation import (
    validate_export_chunks_against_repository, validate_vector_records_against_repository,
)
from app.storage.repository import JsonRepository
from tests.test_routes_documents import _write_approval_evidence


def approved_ingestion_fixture(root: Path, *, chunk_count: int = 1) -> tuple[Settings, JsonRepository, AuthContext, Document, Chunk, dict, dict]:
    """Create synthetic input through the real approval route and journal."""
    settings = Settings(data_dir=root / "data", artifact_root=root, tenant_storage_isolation=False, app_env="test")
    repository = JsonRepository(settings)
    auth = AuthContext(actor="synthetic-reviewer", tenant_id="tenant-a", auth_mode="local", role="admin")
    document = Document(
        document_id="approved-doc", filename="synthetic.pdf", document_name="Synthetic regulation",
        file_type="pdf", file_hash="synthetic", tenant_id="tenant-a", profile_id="profile-a",
        regulation_id="synthetic-regulation", regulation_version="v1",
        effective_from="2025-01-01", regulation_status="pending_approval", status="completed",
    )
    repository.upsert_document(document)
    initial_chunk = Chunk(
        chunk_id="approved-article", document_id=document.document_id, chunk_type="article",
        text="Synthetic original article.", normalized_text="Synthetic normalized article.",
        retrieval_text="Synthetic approved retrieval article.", security_level="internal",
        department_acl=["synthetic-department"],
        metadata={"tenant_id": "tenant-a", "profile_id": "profile-a", "regulation_status": "pending_approval"},
    )
    chunks = [initial_chunk.model_copy(update={"chunk_id": "approved-article" if index == 0 else f"approved-article-{index}"}, deep=True)
              for index in range(chunk_count)]
    repository.save_processing_result(document.document_id, [], chunks, [])
    evidence = _write_approval_evidence(root, settings=settings, document_id=document.document_id,
                                        chunks=repository.get_chunks(document.document_id))
    with patch.object(routes_documents, "get_settings", return_value=settings):
        routes_documents.approve_review_chunks(document.document_id, routes_documents.ApprovalRequest(
            chunk_ids=[chunk.chunk_id for chunk in chunks], approval_id="synthetic-approval", security_level="internal", **evidence,
        ), auth)
    document = repository.get_document(document.document_id)
    chunk = repository.get_chunks(document.document_id)[0]
    exported = routes_documents._chunks_for_indexing([chunk], document, auth)[0]
    record = vector_record_from_chunk(exported)
    assert record is not None
    return settings, repository, auth, document, chunk, exported, record


class ApprovalValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.settings, self.repository, self.auth, self.document,
         self.chunk, self.exported, self.record) = approved_ingestion_fixture(self.root)
        self.options = dict(data_dir=self.settings.data_dir, tenant_storage_isolation=False, tenant_id="tenant-a")

    def test_accepts_genuine_repository_model_flat_and_api_exports(self) -> None:
        for payload in (self.chunk.model_dump(mode="json"), Exporter()._flat_chunk(self.chunk), self.exported):
            with self.subTest(serializer=sorted(payload)):
                result = validate_export_chunks_against_repository([payload], **self.options)
                self.assertEqual(1, result["validated_chunk_count"])
        self.assertEqual(1, validate_vector_records_against_repository([self.record], **self.options)["validated_record_count"])

    def test_export_rejects_tampered_content_and_scope_with_copied_approval(self) -> None:
        changes = {"text": "changed original", "normalized_text": "changed normalized",
                   "retrieval_text": "changed retrieval", "ai_preprocessed_text": "changed AI text",
                   "profile_id": "profile-b", "department_acl": ["other-department"],
                   "security_level": "public", "tenant_id": "tenant-b"}
        for field, value in changes.items():
            with self.subTest(field=field):
                payload = copy.deepcopy(self.exported)
                if field == "profile_id":
                    payload["metadata"][field] = value
                else:
                    payload[field] = value
                with self.assertRaises(ValueError):
                    validate_export_chunks_against_repository([payload], **self.options)

    def test_vector_rejects_restamped_tampered_content_and_scope(self) -> None:
        for field, value in {"text": "changed text", "retrieval_text": "changed retrieval",
                             "profile_id": "profile-b", "department_acl": ["other-department"],
                             "security_level": "public", "tenant_id": "tenant-b"}.items():
            with self.subTest(field=field):
                record = copy.deepcopy(self.record)
                if field == "text":
                    record[field] = value
                else:
                    record["metadata"][field] = value
                record = with_vector_record_verification(record)
                with self.assertRaises(ValueError):
                    validate_vector_records_against_repository([record], **self.options)

    def test_rejects_repository_content_changed_with_copied_approval_hash(self) -> None:
        for field, value in {"text": "changed original", "retrieval_text": "changed retrieval",
                             "security_level": "public", "department_acl": ["other-department"]}.items():
            with self.subTest(field=field):
                changed = self.chunk.model_copy(update={field: value}, deep=True)
                self.repository.save_chunks(self.document.document_id, [changed])
                payload = routes_documents._chunks_for_indexing([changed], self.document, self.auth)[0]
                record = vector_record_from_chunk(payload)
                with self.assertRaises(ValueError):
                    validate_export_chunks_against_repository([payload], **self.options)
                with self.assertRaises(ValueError):
                    validate_vector_records_against_repository([record], **self.options)
                self.repository.save_chunks(self.document.document_id, [self.chunk])

    def test_rejects_newer_review_revocation_despite_matching_old_approval(self) -> None:
        self.repository.append_review_record({
            "review_id": "newer-revocation", "document_id": self.document.document_id,
            "tenant_id": "tenant-a", "chunk_ids": [self.chunk.chunk_id], "action": "reject",
            "reviewed_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        })
        for validate, payload in ((validate_export_chunks_against_repository, self.exported),
                                  (validate_vector_records_against_repository, self.record)):
            with self.assertRaisesRegex(ValueError, "latest"):
                validate([payload], **self.options)

    def test_rejects_ambiguous_latest_approval(self) -> None:
        original = self.repository.list_approval_journal_records(self.document.document_id)[0]
        duplicate = {**original, "approval_record_id": "ambiguous-approval"}
        self.repository.append_approval_record(duplicate)
        with self.assertRaisesRegex(ValueError, "latest"):
            validate_vector_records_against_repository([self.record], **self.options)

    def test_rejects_latest_approval_without_complete_worklist_provenance(self) -> None:
        original = self.repository.list_approval_journal_records(self.document.document_id)[0]
        incomplete = {**original, "approval_record_id": "incomplete-latest-approval",
                      "approved_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                      "worklist_evidence": {}}
        self.repository.append_approval_record(incomplete)
        with self.assertRaisesRegex(ValueError, "latest"):
            validate_vector_records_against_repository([self.record], **self.options)

    def test_rejects_restamped_profile_and_acl_in_top_level_and_nested_export(self) -> None:
        for container in ("top", "metadata"):
            for field, value in {"profile_id": "profile-b", "department_acl": ["other-department"],
                                 "security_level": "public", "tenant_id": "tenant-b"}.items():
                with self.subTest(container=container, field=field):
                    payload = copy.deepcopy(self.exported)
                    target = payload if container == "top" else payload["metadata"]
                    target[field] = value
                    with self.assertRaises(ValueError):
                        validate_export_chunks_against_repository([payload], **self.options)

    def test_rejects_missing_journal_unapproved_stale_and_foreign_tenant(self) -> None:
        with patch.object(JsonRepository, "list_approval_journal_records", return_value=[]):
            with self.assertRaises(ValueError):
                validate_export_chunks_against_repository([self.exported], **self.options)
        for changes in ({"approval_status": "draft"}, {"approval_id": "newer-approval"}):
            self.repository.save_chunks(self.document.document_id, [self.chunk.model_copy(update=changes)])
            with self.assertRaises(ValueError):
                validate_vector_records_against_repository([self.record], **self.options)
        self.repository.save_chunks(self.document.document_id, [self.chunk])
        with self.assertRaises(ValueError):
            validate_vector_records_against_repository([self.record], **{**self.options, "tenant_id": "tenant-b"})

    def test_multi_chunk_invocation_reads_snapshot_once_and_checks_every_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repository, auth, document, _chunk, _exported, _record = approved_ingestion_fixture(Path(tmp), chunk_count=40)
            chunks = repository.get_chunks(document.document_id)
            exports = routes_documents._chunks_for_indexing(chunks, document, auth)
            records = [vector_record_from_chunk(chunk) for chunk in exports]
            snapshot = (settings.data_dir / repository.list_approval_journal_records(document.document_id)[0]["snapshot"]).resolve()
            original_open = Path.open
            loads = []

            def counted_open(path, *args, **kwargs):
                if path.resolve() == snapshot:
                    loads.append(path)
                return original_open(path, *args, **kwargs)

            options = dict(data_dir=settings.data_dir, tenant_storage_isolation=False, tenant_id="tenant-a")
            with patch.object(Path, "open", counted_open):
                validate_export_chunks_against_repository(exports, **options)
                self.assertEqual(1, len(loads))
                loads.clear()
                validate_vector_records_against_repository(records, **options)
                self.assertEqual(1, len(loads))
                records[-1]["text"] = "Changed final row with copied approval."
                records[-1] = with_vector_record_verification(records[-1])
                loads.clear()
                with self.assertRaises(ValueError):
                    validate_vector_records_against_repository(records, **options)
                self.assertEqual(1, len(loads))

    def test_duplicate_or_tampered_snapshot_is_rejected_on_each_invocation(self) -> None:
        record = self.repository.list_approval_journal_records(self.document.document_id)[0]
        snapshot = self.settings.data_dir / record["snapshot"]
        original = snapshot.read_text(encoding="utf-8")
        for contents in (original + original, original.replace("Synthetic original article.", "Tampered snapshot article.")):
            with self.subTest(contents=contents[:50]):
                snapshot.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    validate_export_chunks_against_repository([self.exported], **self.options)
                with self.assertRaises(ValueError):
                    validate_vector_records_against_repository([self.record], **self.options)
        snapshot.write_text(original, encoding="utf-8")
        self.assertEqual(1, validate_vector_records_against_repository([self.record], **self.options)["validated_record_count"])

    def test_rejects_document_profile_changed_after_approval(self) -> None:
        self.repository.upsert_document(self.document.model_copy(update={"profile_id": "profile-b"}))
        with self.assertRaises(ValueError):
            validate_export_chunks_against_repository([self.exported], **self.options)


if __name__ == "__main__":
    unittest.main()
