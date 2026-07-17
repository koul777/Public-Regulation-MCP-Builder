from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.api_audit import api_audit_path
from app.core.config import Settings, get_settings
from app.core.security import API_WRITE_ROLES, AuthContext, authenticate_request, require_api_role
from app.main import app
from app.api import routes_documents, routes_exports
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository


class ApiSecurityTests(unittest.TestCase):
    def test_local_mode_allows_anonymous_actor_without_token(self) -> None:
        context = authenticate_request(Settings(api_auth_required=False), actor=None, tenant_id=None)

        self.assertEqual(context.actor, "local-anonymous")
        self.assertEqual(context.tenant_id, "default")
        self.assertEqual(context.auth_mode, "local")
        self.assertEqual(context.role, "admin")

    def test_required_auth_rejects_missing_or_invalid_token(self) -> None:
        settings = Settings(api_auth_required=True, api_auth_token="secret")

        with self.assertRaises(HTTPException) as missing:
            authenticate_request(settings, actor="tester")
        with self.assertRaises(HTTPException) as invalid:
            authenticate_request(settings, authorization="Bearer wrong", actor="tester")

        self.assertEqual(missing.exception.status_code, 401)
        self.assertEqual(invalid.exception.status_code, 401)

    def test_required_auth_requires_actor_header(self) -> None:
        settings = Settings(api_auth_required=True, api_auth_token="secret")

        with self.assertRaises(HTTPException) as raised:
            authenticate_request(settings, authorization="Bearer secret")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("X-Actor", raised.exception.detail)

    def test_required_auth_requires_tenant_header_when_tenant_storage_isolated(self) -> None:
        settings = Settings(
            api_auth_required=True,
            api_auth_token="secret",
            tenant_storage_isolation=True,
        )

        with self.assertRaises(HTTPException) as raised:
            authenticate_request(settings, authorization="Bearer secret", actor="tester")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("X-Tenant-Id", raised.exception.detail)

    def test_required_auth_allows_default_tenant_only_without_tenant_storage_isolation(self) -> None:
        settings = Settings(
            api_auth_required=True,
            api_auth_token="secret",
            tenant_storage_isolation=False,
        )

        context = authenticate_request(settings, authorization="Bearer secret", actor="tester")

        self.assertEqual("default", context.tenant_id)
        self.assertEqual("admin", context.role)

    def test_required_auth_resolves_role_specific_token(self) -> None:
        settings = Settings(
            api_auth_required=True,
            api_auth_tokens=json.dumps({"view-secret": "viewer"}),
        )

        context = authenticate_request(settings, authorization="Bearer view-secret", actor="auditor")

        self.assertEqual("auditor", context.actor)
        self.assertEqual("viewer", context.role)
        self.assertEqual("api_token_rbac", context.auth_mode)

    def test_role_specific_token_can_bind_actor(self) -> None:
        settings = Settings(
            api_auth_required=True,
            api_auth_tokens=json.dumps({"ops-secret": {"role": "operator", "actor": "scheduler"}}),
        )

        context = authenticate_request(settings, authorization="Bearer ops-secret")

        self.assertEqual("scheduler", context.actor)
        self.assertEqual("operator", context.role)

    def test_role_specific_token_can_bind_departments(self) -> None:
        settings = Settings(
            api_auth_required=True,
            api_auth_tokens=json.dumps(
                {"ops-secret": {"role": "operator", "actor": "scheduler", "department_ids": ["hr", "finance"]}}
            ),
        )

        context = authenticate_request(settings, authorization="Bearer ops-secret")

        self.assertEqual(("hr", "finance"), context.department_ids)

    def test_role_specific_token_rejects_actor_mismatch(self) -> None:
        settings = Settings(
            api_auth_required=True,
            api_auth_tokens=json.dumps({"ops-secret": {"role": "operator", "actor": "scheduler"}}),
        )

        with self.assertRaises(HTTPException) as raised:
            authenticate_request(settings, authorization="Bearer ops-secret", actor="other")

        self.assertEqual(raised.exception.status_code, 403)

    def test_authentication_rejects_oversized_and_control_character_identity_headers(self) -> None:
        settings = Settings(api_auth_required=False)

        for field_name, kwargs in (
            ("X-Actor", {"actor": "a" * 201}),
            ("X-Tenant-Id", {"tenant_id": "t" * 129}),
            ("X-Actor", {"actor": "bad\nactor"}),
            ("X-Tenant-Id", {"tenant_id": "bad\ttenant"}),
        ):
            with self.subTest(field_name=field_name), self.assertRaises(HTTPException) as raised:
                authenticate_request(settings, **kwargs)
            self.assertEqual(400, raised.exception.status_code)
            self.assertIn(field_name, str(raised.exception.detail))

    def test_authentication_rejects_invalid_configured_identity_values_as_server_error(self) -> None:
        invalid_default = Settings(api_auth_required=False, api_default_tenant_id="t" * 129)
        invalid_actor = Settings(
            api_auth_required=True,
            api_auth_tokens=json.dumps(
                {"ops-secret": {"role": "operator", "actor": "a" * 201}}
            ),
        )

        with self.assertRaises(HTTPException) as tenant_error:
            authenticate_request(invalid_default)
        with self.assertRaises(HTTPException) as actor_error:
            authenticate_request(invalid_actor, authorization="Bearer ops-secret")

        self.assertEqual(500, tenant_error.exception.status_code)
        self.assertEqual(500, actor_error.exception.status_code)

    def test_write_role_guard_rejects_viewer(self) -> None:
        auth = AuthContext(actor="auditor", tenant_id="tenant-a", auth_mode="api_token_rbac", role="viewer")

        with self.assertRaises(HTTPException) as raised:
            require_api_role(auth, API_WRITE_ROLES)

        self.assertEqual(raised.exception.status_code, 403)

    def test_required_auth_fails_closed_when_token_is_not_configured(self) -> None:
        settings = Settings(api_auth_required=True, api_auth_token="")

        with self.assertRaises(HTTPException) as raised:
            authenticate_request(settings, authorization="Bearer secret", actor="tester")

        self.assertEqual(raised.exception.status_code, 500)

    def test_fastapi_dependency_protects_document_routes_when_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), api_auth_required=True, api_auth_token="secret")
            app.dependency_overrides[get_settings] = lambda: settings
            try:
                client = TestClient(app)

                with patch_routes_settings(settings):
                    denied = client.get("/api/documents")
                allowed = client.get(
                    "/api/documents",
                    headers={"Authorization": "Bearer secret", "X-Actor": "tester", "X-Tenant-Id": "tenant-a"},
                )
                rows = [json.loads(line) for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()]
            finally:
                app.dependency_overrides.clear()

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertIsInstance(allowed.json(), list)
        denied_rows = [row for row in rows if row["action"] == "auth.denied"]
        self.assertEqual(1, len(denied_rows))
        self.assertEqual("denied", denied_rows[0]["outcome"])
        self.assertEqual(401, denied_rows[0]["status_code"])

    def test_viewer_token_can_list_but_cannot_upload_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                api_auth_required=True,
                api_auth_tokens=json.dumps({"view-secret": {"role": "viewer", "actor": "auditor"}}),
            )
            app.dependency_overrides[get_settings] = lambda: settings
            try:
                client = TestClient(app)
                headers = {"Authorization": "Bearer view-secret"}
                with patch_routes_settings(settings):
                    listed = client.get("/api/documents", headers=headers)
                    denied = client.post(
                        "/api/documents",
                        headers=headers,
                        files={"file": ("sample.pdf", b"%PDF-1.4 sample", "application/pdf")},
                    )
                rows = [json.loads(line) for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()]
            finally:
                app.dependency_overrides.clear()

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual("document.upload", rows[0]["action"])
        self.assertEqual("denied", rows[0]["outcome"])
        self.assertEqual("viewer", rows[0]["api_role"])

    def test_viewer_cannot_read_raw_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            viewer = AuthContext(
                actor="auditor",
                tenant_id="tenant-a",
                auth_mode="api_token_rbac",
                role="viewer",
            )
            with patch_routes_settings(settings):
                for handler in (
                    routes_documents.get_issues,
                    routes_documents.get_quality,
                    routes_documents.get_runs,
                    routes_documents.get_security_review,
                ):
                    with self.subTest(handler=handler.__name__):
                        with self.assertRaises(HTTPException) as raised:
                            handler("doc-raw", viewer)
                        self.assertEqual(403, raised.exception.status_code)
                audit_rows = [
                    json.loads(line)
                    for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

        self.assertEqual(
            {
                "document.read.issues",
                "document.read.quality",
                "document.read.runs",
                "document.read.security_review",
            },
            {row["action"] for row in audit_rows},
        )
        self.assertTrue(all(row["outcome"] == "denied" and row["status_code"] == 403 for row in audit_rows))
    def test_viewer_chunks_endpoint_filters_unapproved_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            _seed_document_with_mixed_approval_chunks(settings)
            viewer = AuthContext(actor="auditor", tenant_id="tenant-a", auth_mode="api_token", role="viewer")

            with patch_routes_settings(settings):
                rows = routes_documents.get_chunks("doc_review", offset=0, limit=None, auth_context=viewer)

        self.assertEqual(["approved-1"], [row["chunk_id"] for row in rows])
        self.assertEqual("approved body", rows[0]["text"])

    def test_viewer_chunks_endpoint_hides_approved_chunk_without_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            _seed_document_with_mixed_approval_chunks(settings, include_approval_journal=False)
            viewer = AuthContext(actor="auditor", tenant_id="tenant-a", auth_mode="api_token", role="viewer")

            with patch_routes_settings(settings):
                rows = routes_documents.get_chunks("doc_review", offset=0, limit=None, auth_context=viewer)

        self.assertEqual([], rows)

    def test_viewer_export_generates_approved_only_content_instead_of_persisted_review_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            _seed_document_with_mixed_approval_chunks(settings)
            settings.exports_dir.mkdir(parents=True, exist_ok=True)
            (settings.exports_dir / "doc_review.jsonl").write_text(
                '{"chunk_id":"approved-1","text":"approved body"}\n'
                '{"chunk_id":"draft-1","text":"draft secret"}\n',
                encoding="utf-8",
            )
            viewer = AuthContext(actor="auditor", tenant_id="tenant-a", auth_mode="api_token", role="viewer")

            with patch_exports_settings(settings):
                response = routes_exports.export_document("doc_review", "jsonl", viewer)

        body = response.body.decode("utf-8")
        self.assertIn("approved body", body)
        self.assertNotIn("draft secret", body)
        self.assertNotIn("draft-1", body)

    def test_viewer_export_hides_approved_chunk_without_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            _seed_document_with_mixed_approval_chunks(settings, include_approval_journal=False)
            settings.exports_dir.mkdir(parents=True, exist_ok=True)
            (settings.exports_dir / "doc_review.jsonl").write_text(
                '{"chunk_id":"approved-1","text":"approved body"}\n',
                encoding="utf-8",
            )
            viewer = AuthContext(actor="auditor", tenant_id="tenant-a", auth_mode="api_token", role="viewer")

            with patch_exports_settings(settings), self.assertRaises(HTTPException) as raised:
                routes_exports.export_document("doc_review", "jsonl", viewer)

        self.assertEqual(404, raised.exception.status_code)
        self.assertIn("No approved chunks found", str(raised.exception.detail))


class patch_routes_settings:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._patcher = None

    def __enter__(self):
        from unittest.mock import patch

        self._patcher = patch.object(routes_documents, "get_settings", return_value=self.settings)
        return self._patcher.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._patcher.__exit__(exc_type, exc, tb)


class patch_exports_settings:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._patcher = None

    def __enter__(self):
        from unittest.mock import patch

        self._patcher = patch.object(routes_exports, "get_settings", return_value=self.settings)
        return self._patcher.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._patcher.__exit__(exc_type, exc, tb)


def _seed_document_with_mixed_approval_chunks(settings: Settings, *, include_approval_journal: bool = True) -> None:
    repository = JsonRepository(settings)
    approved_content_hash = "d" * 64
    approval_metadata = _approval_provenance_metadata()
    repository.upsert_document(
        Document(
            document_id="doc_review",
            filename="doc_review.pdf",
            document_name="doc_review",
            file_type="pdf",
            file_hash="hash",
            tenant_id="tenant-a",
            status="completed",
        )
    )
    repository.save_chunks(
        "doc_review",
        [
            Chunk(
                chunk_id="approved-1",
                document_id="doc_review",
                source_node_ids=["node-approved"],
                chunk_type="article",
                text="approved body",
                normalized_text="approved body",
                retrieval_text="approved body",
                metadata=approval_metadata,
                approval_status="approved",
                approval_id="approval-1",
                approved_by="reviewer",
                approved_content_hash=approved_content_hash,
                security_level="public",
            ),
            Chunk(
                chunk_id="draft-1",
                document_id="doc_review",
                source_node_ids=["node-draft"],
                chunk_type="article",
                text="draft secret",
                normalized_text="draft secret",
                retrieval_text="draft secret",
                approval_status="needs_review",
                security_level="public",
            ),
        ],
    )
    if include_approval_journal:
        repository.append_approval_record(
            {
                "approval_record_id": "approval_record_1",
                "approval_id": "approval-1",
                "document_id": "doc_review",
                "tenant_id": "tenant-a",
                "chunk_ids": ["approved-1"],
                "approved_content_hashes": {"approved-1": approved_content_hash},
                "approved_chunks": [
                    {
                        "chunk_id": "approved-1",
                        "approved_content_hash": approved_content_hash,
                    }
                ],
                "approved_by": "reviewer",
                "approved_at": "2026-07-10T00:00:00+00:00",
                "worklist_evidence": _approval_worklist_evidence(),
            }
        )


def _approval_provenance_metadata() -> dict[str, str]:
    return {
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }


def _approval_worklist_evidence() -> dict[str, str]:
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": "a" * 64,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": "b" * 64,
        "review_batch_id": "approval-batch-001",
        "review_batch_chunk_fingerprint": "c" * 64,
        "review_strategy": "human_bulk_review",
    }


if __name__ == "__main__":
    unittest.main()
