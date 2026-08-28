from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents, routes_exports
from app.core.config import Settings
from app.core.security import AuthContext
from app.services.synthetic_sample_service import build_synthetic_regulation_docx

class _UploadFile:
    def __init__(self, path: Path) -> None:
        self.filename = path.name
        self.file = path.open("rb")

    async def seek(self, offset: int) -> None:
        self.file.seek(offset)

    def close(self) -> None:
        self.file.close()


class ApiSampleEndToEndTests(unittest.TestCase):
    def test_synthetic_docx_upload_process_quality_and_export_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample = _write_synthetic_regulation_docx(tmp_path / "synthetic_regulation.docx")
            settings = Settings(data_dir=tmp_path / "data")
            auth = AuthContext(
                actor="api-smoke-test",
                tenant_id="tenant-smoke",
                auth_mode="api_token",
                role="admin",
            )
            upload = _UploadFile(sample)
            try:
                with patch.object(routes_documents, "get_settings", return_value=settings):
                    document = asyncio.run(
                        routes_documents.upload_document(
                            upload,
                            institution_name="Smoke Institution",
                            source_system="LOCAL",
                            source_record_id="sample-board",
                            source_file_id="sample-file",
                            profile_id="default-public-institution",
                            auth_context=auth,
                        )
                    )
                    job = routes_documents.process_document(document["document_id"], None, auth)
                    quality = routes_documents.get_quality(document["document_id"], auth)
            finally:
                upload.close()

            with patch.object(routes_exports, "get_settings", return_value=settings):
                exported = routes_exports.export_document(document["document_id"], "jsonl", auth)

            self.assertEqual("completed", job["status"])
            self.assertTrue(quality["passed"])
            self.assertGreater(quality["chunk_count"], 0)
            self.assertTrue(Path(exported.path).is_file())
            self.assertEqual(f"{document['document_id']}.jsonl", exported.filename)


def _write_synthetic_regulation_docx(path: Path) -> Path:
    path.write_bytes(build_synthetic_regulation_docx())
    return path


if __name__ == "__main__":
    unittest.main()
