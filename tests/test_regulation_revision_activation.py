from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from app.api.routes_documents import _automatically_supersede_prior_version
from app.core.config import Settings
from app.core.security import AuthContext
from app.schemas.document import Document
from app.storage.repository import JsonRepository


class RegulationRevisionActivationTests(unittest.TestCase):
    def test_approved_revision_supersedes_legacy_prior_version_with_same_effective_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            repository = JsonRepository(settings)
            created_at = datetime(2025, 7, 1, tzinfo=timezone.utc)
            prior = Document(
                document_id="doc-prior",
                filename="prior.pdf",
                document_name="인사규정",
                file_type="pdf",
                file_hash="hash-prior",
                profile_id="institution-a",
                regulation_id="reg-인사규정",
                regulation_version="v1",
                revision_date="2025-07-01",
                effective_from=None,
                regulation_status="approved",
                tenant_id="tenant-a",
                status="completed",
                created_at=created_at,
            )
            revision = Document(
                document_id="doc-revision",
                filename="revision.pdf",
                document_name="인사규정",
                file_type="pdf",
                file_hash="hash-revision",
                profile_id="institution-a",
                regulation_id="reg-인사규정",
                regulation_version="v2",
                revision_date="2025-07-01",
                effective_from="2025-07-01",
                regulation_status="approved",
                supersedes_document_id=prior.document_id,
                tenant_id="tenant-a",
                status="completed",
                created_at=created_at,
            )
            repository.upsert_document(prior)
            repository.upsert_document(revision)

            event = _automatically_supersede_prior_version(
                settings=settings,
                repository=repository,
                document=revision,
                auth=AuthContext(
                    actor="tester",
                    tenant_id="tenant-a",
                    auth_mode="local",
                    role="operator",
                ),
            )
            updated_prior = repository.get_document(prior.document_id)

        self.assertIsNotNone(event)
        self.assertEqual("completed", event["outcome"])
        self.assertEqual("superseded", updated_prior.regulation_status)
        self.assertEqual("2025-07-01", updated_prior.effective_from)
        self.assertEqual("2025-07-01", updated_prior.effective_to)


if __name__ == "__main__":
    unittest.main()
