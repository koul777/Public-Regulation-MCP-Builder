from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import routes_documents
from app.core.api_audit import api_audit_path
from app.core.config import Settings, get_settings
from app.main import app


class APIUploadAdmissionTests(unittest.TestCase):
    def test_upload_rejects_pdf_signature_mismatch_and_writes_failure_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), api_auth_required=False)
            app.dependency_overrides[get_settings] = lambda: settings
            try:
                with patch.object(routes_documents, "get_settings", return_value=settings):
                    response = TestClient(app).post(
                        "/api/documents",
                        files={"file": ("bad.pdf", b"not a pdf", "application/pdf")},
                    )
                rows = [json.loads(line) for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()]
            finally:
                app.dependency_overrides.clear()

        self.assertEqual(400, response.status_code)
        self.assertIn(".pdf signature", response.json()["detail"])
        self.assertEqual("document.upload", rows[0]["action"])
        self.assertEqual("failure", rows[0]["outcome"])
        self.assertEqual(400, rows[0]["status_code"])
        self.assertEqual("bad.pdf", rows[0]["filename"])


if __name__ == "__main__":
    unittest.main()
