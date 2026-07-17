from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app, readiness_checks


class ReadinessTests(unittest.TestCase):
    def test_readiness_checks_pass_for_local_temp_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                app_env="local",
                data_dir=Path(tmp) / "data",
                api_auth_required=False,
                tenant_storage_isolation=False,
            )

            checks = readiness_checks(settings)

        self.assertTrue(all(check["passed"] for check in checks))

    def test_readiness_endpoint_rejects_missing_protected_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="",
                tenant_storage_isolation=True,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(503, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertFalse(checks["api_auth_token_configured"]["passed"])
        self.assertTrue(all("path" not in check for check in response.json()["checks"]))
        self.assertTrue(all("error" not in check for check in response.json()["checks"]))

    def test_readiness_endpoint_accepts_role_specific_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="",
                api_auth_tokens=json.dumps({"operator-token": {"role": "operator", "actor": "batch-operator"}}),
                tenant_storage_isolation=True,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(200, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertTrue(checks["api_auth_token_configured"]["passed"])

    def test_readiness_endpoint_rejects_invalid_role_token_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="",
                api_auth_tokens="{invalid-json",
                tenant_storage_isolation=True,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(503, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertFalse(checks["api_auth_token_configured"]["passed"])

    def test_readiness_endpoint_rejects_auth_disabled_in_protected_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=False,
                api_auth_token="",
                tenant_storage_isolation=True,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(503, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertFalse(checks["api_auth_required_for_protected_env"]["passed"])

    def test_readiness_endpoint_rejects_unisolated_protected_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="secret",
                tenant_storage_isolation=False,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(503, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertFalse(checks["tenant_storage_isolation_enabled_for_protected_env"]["passed"])

    def test_readiness_endpoint_rejects_disabled_audit_in_protected_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(
                app_env="production",
                data_dir=data_dir,
                api_auth_required=True,
                api_auth_token="secret",
                api_audit_enabled=False,
                tenant_storage_isolation=True,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(503, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertFalse(checks["api_audit_enabled_for_protected_env"]["passed"])

    def test_readiness_rejects_nonpositive_json_request_body_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                app_env="local",
                data_dir=Path(tmp) / "data",
                max_json_request_body_mb=0,
            )

            checks = {check["name"]: check for check in readiness_checks(settings)}

        self.assertFalse(checks["json_request_body_limit_positive"]["passed"])

    def test_readiness_endpoint_rejects_missing_protected_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_data_dir = Path(tmp) / "missing-data"
            settings = Settings(
                app_env="production",
                data_dir=missing_data_dir,
                api_auth_required=True,
                api_auth_token="secret",
                tenant_storage_isolation=True,
            )

            with patch("app.main.get_settings", return_value=settings):
                response = TestClient(app).get("/ready")

        self.assertEqual(503, response.status_code)
        checks = {check["name"]: check for check in response.json()["checks"]}
        self.assertFalse(checks["data_dir_exists_for_protected_env"]["passed"])
        self.assertFalse(missing_data_dir.exists())


if __name__ == "__main__":
    unittest.main()
