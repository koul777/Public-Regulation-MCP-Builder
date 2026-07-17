from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import routes_documents, routes_exports, routes_jobs
from app.core.api_audit import api_audit_path
from app.core.config import Settings, get_settings
from app.core.tenant_access import settings_for_tenant
from app.main import app
from app.schemas.document import Document, ProcessingJob
from app.storage.repository import JsonRepository


class ApiTenantIsolationTests(unittest.TestCase):
    def test_document_list_is_filtered_by_tenant(self) -> None:
        with tenant_client() as context:
            response = context.client.get("/api/documents", headers=context.headers("tenant-a"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["document_id"] for row in response.json()}, {"doc_a"})

    def test_document_results_are_not_visible_cross_tenant(self) -> None:
        with tenant_client() as context:
            chunks = context.client.get("/api/documents/doc_b/chunks", headers=context.headers("tenant-a"))
            quality = context.client.get("/api/documents/doc_b/quality", headers=context.headers("tenant-a"))

        self.assertEqual(chunks.status_code, 404)
        self.assertEqual(quality.status_code, 404)

    def test_export_file_is_not_served_cross_tenant(self) -> None:
        with tenant_client() as context:
            response = context.client.get("/api/documents/doc_b/export?format=jsonl", headers=context.headers("tenant-a"))

        self.assertEqual(response.status_code, 404)

    def test_job_is_not_visible_cross_tenant(self) -> None:
        with tenant_client() as context:
            denied = context.client.get("/api/jobs/job_b", headers=context.headers("tenant-a"))
            allowed = context.client.get("/api/jobs/job_b", headers=context.headers("tenant-b"))

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["job_id"], "job_b")

    def test_tenant_storage_isolation_uses_separate_data_directories(self) -> None:
        with tenant_client(tenant_storage_isolation=True) as context:
            tenant_a_settings = settings_for_tenant(context.settings, "tenant-a")
            tenant_b_settings = settings_for_tenant(context.settings, "tenant-b")

            response = context.client.post(
                "/api/documents",
                headers=context.headers("tenant-a"),
                files={"file": ("tenant-a.pdf", b"%PDF-1.4 tenant-a", "application/pdf")},
            )
            tenant_a_docs = context.client.get("/api/documents", headers=context.headers("tenant-a"))
            tenant_b_docs = context.client.get("/api/documents", headers=context.headers("tenant-b"))
            tenant_a_uploads_exists = tenant_a_settings.uploads_dir.is_dir()
            tenant_a_uploaded_pdf_exists = any(
                path.name.endswith(".pdf")
                for path in tenant_a_settings.uploads_dir.rglob("*.pdf")
            )
            tenant_b_manifest_exists = (tenant_b_settings.data_dir / "repository" / "manifest.json").is_file()
            base_uploads_exists = (context.settings.data_dir / "uploads").exists()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tenant_id"], "tenant-a")
        self.assertTrue(tenant_a_uploads_exists)
        self.assertTrue(tenant_a_uploaded_pdf_exists)
        self.assertTrue(tenant_b_manifest_exists)
        self.assertEqual(
            {row["document_id"] for row in tenant_a_docs.json()},
            {"doc_a", response.json()["document_id"]},
        )
        self.assertEqual({row["document_id"] for row in tenant_b_docs.json()}, {"doc_b"})
        self.assertFalse(base_uploads_exists)

    def test_auth_denial_audit_uses_central_pre_auth_storage(self) -> None:
        with tenant_client(tenant_storage_isolation=True) as context:
            tenant_a_settings = settings_for_tenant(context.settings, "tenant-a")
            response = context.client.get(
                "/api/documents",
                headers={"Authorization": "Bearer wrong", "X-Tenant-Id": "tenant-a"},
            )
            tenant_a_audit_exists = api_audit_path(tenant_a_settings).is_file()
            base_audit_exists = api_audit_path(context.settings).is_file()
            rows = [
                json.loads(line)
                for line in api_audit_path(context.settings).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(401, response.status_code)
        self.assertFalse(tenant_a_audit_exists)
        self.assertTrue(base_audit_exists)
        self.assertEqual("default", rows[0]["tenant_id"])
        self.assertEqual("tenant-a", rows[0]["claimed_tenant_id"])


class tenant_client:
    def __init__(self, *, tenant_storage_isolation: bool = False):
        self.tenant_storage_isolation = tenant_storage_isolation

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.tmp.name),
            api_auth_required=True,
            api_auth_token="secret",
            api_default_tenant_id="default",
            tenant_storage_isolation=self.tenant_storage_isolation,
        )
        self.repo = JsonRepository(settings_for_tenant(self.settings, "tenant-a"))
        self.repo.upsert_document(_document("doc_a", tenant_id="tenant-a"))
        self.repo.upsert_document(_document("doc_shared", tenant_id=None))
        self.tenant_b_repo = JsonRepository(settings_for_tenant(self.settings, "tenant-b"))
        self.tenant_b_repo.upsert_document(_document("doc_b", tenant_id="tenant-b"))
        self.tenant_b_repo.upsert_job(ProcessingJob(job_id="job_b", document_id="doc_b", tenant_id="tenant-b"))
        tenant_b_settings = settings_for_tenant(self.settings, "tenant-b")
        tenant_b_settings.exports_dir.mkdir(parents=True, exist_ok=True)
        (tenant_b_settings.exports_dir / "doc_b.jsonl").write_text('{"chunk_id":"secret"}\n', encoding="utf-8")

        app.dependency_overrides[get_settings] = lambda: self.settings
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(routes_documents, "get_settings", return_value=self.settings))
        self.stack.enter_context(patch.object(routes_exports, "get_settings", return_value=self.settings))
        self.stack.enter_context(patch.object(routes_jobs, "get_settings", return_value=self.settings))
        self.client = TestClient(app)
        return self

    def __exit__(self, exc_type, exc, tb):
        app.dependency_overrides.clear()
        self.stack.close()
        self.tmp.cleanup()

    def headers(self, tenant_id: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer secret",
            "X-Actor": "tester",
            "X-Tenant-Id": tenant_id,
        }


def _document(document_id: str, *, tenant_id: str | None) -> Document:
    return Document(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        document_name=document_id,
        file_type="pdf",
        file_hash=f"hash-{document_id}",
        tenant_id=tenant_id,
        status="uploaded",
    )


if __name__ == "__main__":
    unittest.main()
