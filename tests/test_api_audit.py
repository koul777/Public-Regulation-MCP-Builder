from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents
from app.core.api_audit import append_api_audit_record, api_audit_path, audit_api_event, redact_sensitive_paths
from app.core.config import Settings
from app.core.security import AuthContext, audit_auth_denial
from app.schemas.document import Document, ProcessingJob


class ApiAuditTests(unittest.TestCase):
    def test_append_api_audit_record_writes_locked_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))

            record = append_api_audit_record(
                settings,
                {
                    "actor": "tester",
                    "tenant_id": "tenant-a",
                    "auth_mode": "api_token",
                    "action": "document.upload",
                    "resource_type": "document",
                    "document_id": "doc_1",
                    "outcome": "success",
                    "status_code": 200,
                },
            )

            rows = [json.loads(line) for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()]

        self.assertTrue(record["record_id"].startswith("api_"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "tester")
        self.assertEqual(rows[0]["action"], "document.upload")

    def test_audit_api_event_records_only_filename_not_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token")

            audit_api_event(
                settings,
                auth,
                action="document.export",
                outcome="success",
                status_code=200,
                resource_type="document",
                document_id="doc_1",
                filename=r"C:\Users\example\Desktop\secret.pdf",
                export_format="jsonl",
            )
            raw = api_audit_path(settings).read_text(encoding="utf-8")

        self.assertIn("secret.pdf", raw)
        self.assertNotIn(r"C:\Users", raw)

    def test_audit_api_event_records_only_filename_for_posix_style_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token")

            record = audit_api_event(
                settings,
                auth,
                action="document.export",
                outcome="success",
                status_code=200,
                resource_type="document",
                document_id="doc_1",
                filename="/home/example/Desktop/secret.pdf",
                export_format="jsonl",
            )

        self.assertEqual(record["filename"], "secret.pdf")

    def test_append_api_audit_record_rejects_unexpected_local_path_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))

            with self.assertRaises(ValueError):
                append_api_audit_record(
                    settings,
                    {
                        "actor": "tester",
                        "tenant_id": "tenant-a",
                        "auth_mode": "api_token",
                        "action": "document.upload",
                        "outcome": "failure",
                        "status_code": 400,
                        "detail": r"C:\Users\example\Desktop\secret.pdf",
                    },
                )

    def test_audit_api_event_redacts_local_path_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token")

            audit_api_event(
                settings,
                auth,
                action="document.process",
                outcome="failure",
                status_code=500,
                detail=r"parser failed at C:\Users\example\Desktop\secret.pdf",
            )
            raw = api_audit_path(settings).read_text(encoding="utf-8")

        self.assertIn("[local-path-redacted]", raw)
        self.assertNotIn(r"C:\Users", raw)

    def test_audit_api_event_redacts_container_path_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token")

            audit_api_event(
                settings,
                auth,
                action="document.process",
                outcome="failure",
                status_code=500,
                detail="/app/data/input secret.pdf failed",
            )
            raw = api_audit_path(settings).read_text(encoding="utf-8")

        self.assertIn("[local-path-redacted]", raw)
        self.assertNotIn("/app/data", raw)

    def test_redacts_paths_with_spaces(self) -> None:
        detail = (
            r'windows "C:\Users\example\My Documents\secret file.pdf"; '
            r"unc \\server\share\Team Folder\secret file.docx; "
            "/home/example/My Documents/secret file.pdf"
        )

        redacted = redact_sensitive_paths(detail)

        self.assertIn("[local-path-redacted]", redacted)
        self.assertNotIn("C:\\Users", redacted)
        self.assertNotIn("My Documents", redacted)
        self.assertNotIn("\\\\server\\share", redacted)
        self.assertNotIn("/home/example", redacted)

    def test_auth_denial_writes_denied_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), api_default_tenant_id="tenant-default")

            audit_auth_denial(
                settings,
                exc=ExceptionWithStatus(401, "Missing or invalid API credentials."),
                actor=None,
                tenant_id=None,
            )
            rows = [json.loads(line) for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()]

        self.assertEqual("auth.denied", rows[0]["action"])
        self.assertEqual("denied", rows[0]["outcome"])
        self.assertEqual(401, rows[0]["status_code"])
        self.assertEqual("tenant-default", rows[0]["tenant_id"])
        self.assertEqual("tenant-default", rows[0]["claimed_tenant_id"])

    def test_auth_denial_redacts_path_shaped_claimed_tenant_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), api_default_tenant_id="tenant-default")

            audit_auth_denial(
                settings,
                exc=ExceptionWithStatus(401, "Missing or invalid API credentials."),
                actor=None,
                tenant_id=r"C:\Users\attacker\secret.pdf",
            )
            raw = api_audit_path(settings).read_text(encoding="utf-8")
            rows = [json.loads(line) for line in raw.splitlines()]

        self.assertEqual("auth.denied", rows[0]["action"])
        self.assertEqual("[local-path-redacted]", rows[0]["claimed_tenant_id"])
        self.assertNotIn(r"C:\Users", raw)

    def test_auth_denial_redacts_control_characters_in_identity_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), api_auth_required=True)
            audit_auth_denial(
                settings,
                ExceptionWithStatus(400, "invalid identity header"),
                actor="bad\nactor",
                tenant_id="bad\ttenant",
            )
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual("[untrusted-header-redacted]", rows[0]["actor"])
        self.assertEqual("[untrusted-header-redacted]", rows[0]["claimed_tenant_id"])

    def test_process_route_writes_success_audit_record(self) -> None:
        class FakeProcessingService:
            def __init__(self, settings=None, repository=None):
                pass

            def process(self, document_id, options=None):
                return ProcessingJob(
                    job_id="job_test",
                    document_id=document_id,
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token")
            with patch.object(routes_documents, "ProcessingService", FakeProcessingService), patch.object(
                routes_documents, "_repository", return_value=_repository_with_document()
            ), patch.object(routes_documents, "get_settings", return_value=settings):
                response = routes_documents.process_document("doc_test", None, auth)

            rows = [json.loads(line) for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(response["job_id"], "job_test")
        self.assertEqual(rows[0]["actor"], "tester")
        self.assertEqual(rows[0]["action"], "document.process")
        self.assertEqual(rows[0]["document_id"], "doc_test")
        self.assertEqual(rows[0]["job_id"], "job_test")
        self.assertEqual(rows[0]["outcome"], "success")


class _RepositoryWithDocument:
    def get_document(self, document_id: str):
        return Document(
            document_id=document_id,
            filename="doc_test.pdf",
            document_name="doc_test",
            file_type="pdf",
            file_hash="hash",
            tenant_id="tenant-a",
            status="uploaded",
        )


def _repository_with_document() -> _RepositoryWithDocument:
    return _RepositoryWithDocument()


class ExceptionWithStatus(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


if __name__ == "__main__":
    unittest.main()
