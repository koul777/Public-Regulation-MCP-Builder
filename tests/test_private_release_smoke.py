from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import Settings
from scripts.check_private_release_readiness import build_readiness_report
from scripts.run_private_release_smoke import (
    DEFAULT_EXPORT_FORMATS,
    _summarize_audit,
    main,
    run_smoke,
    write_synthetic_smoke_docx,
)

class PrivateReleaseSmokeTests(unittest.TestCase):
    def test_private_release_smoke_processes_synthetic_sample_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample = _write_synthetic_regulation_docx(tmp_path / "synthetic_private_smoke.docx")
            data_dir = tmp_path / "data"
            report = run_smoke(
                sample_path=sample,
                data_dir=data_dir,
            )
            readiness = build_readiness_report(
                Settings(
                    app_env="production",
                    data_dir=data_dir,
                    api_auth_required=True,
                    api_auth_token="smoke-token",
                    tenant_storage_isolation=True,
                    api_audit_enabled=True,
                ),
                require_shared_deployment=True,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(readiness["passed"])
        self.assertEqual("private_release_smoke", report["report_type"])
        self.assertEqual("completed", report["job"]["status"])
        self.assertTrue(report["quality"]["passed"])
        self.assertGreater(report["quality"]["chunk_count"], 0)
        self.assertEqual([], report["failed_exports"])
        self.assertEqual(list(DEFAULT_EXPORT_FORMATS), report["required_export_formats"])
        self.assertEqual(set(DEFAULT_EXPORT_FORMATS), {item["format"] for item in report["exports"]})
        self.assertTrue(all(item["exists"] for item in report["exports"]))
        self.assertEqual(401, report["http"]["unauthorized_upload_status_code"])
        self.assertEqual(400, report["http"]["missing_tenant_upload_status_code"])
        self.assertTrue(report["audit"]["passed"])
        self.assertTrue(report["audit"]["auth_denial_passed"])
        self.assertTrue(report["audit"]["tenant_header_required_passed"])
        for action in ["auth.denied", "document.upload", "document.process", "document.export"]:
            with self.subTest(action=action):
                self.assertIn(action, report["audit"]["actions"])

    def test_audit_summary_ignores_stale_records_in_reused_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            base_audit = data_dir / "repository" / "api_audit.jsonl"
            tenant_audit = data_dir / "tenants" / "tenant-smoke" / "repository" / "api_audit.jsonl"
            base_audit.parent.mkdir(parents=True)
            tenant_audit.parent.mkdir(parents=True)
            old_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            base_audit.write_text(
                '{"created_at": "%s", "actor": "unknown", "tenant_id": "default", "claimed_tenant_id": "tenant-smoke", "action": "auth.denied"}\n'
                % old_time,
                encoding="utf-8",
            )
            tenant_audit.write_text(
                "\n".join(
                    [
                        '{"created_at": "%s", "actor": "private-release-smoke", "tenant_id": "tenant-smoke", "action": "document.upload"}'
                        % old_time,
                        '{"created_at": "%s", "actor": "private-release-smoke", "tenant_id": "tenant-smoke", "document_id": "doc_old", "action": "document.process"}'
                        % old_time,
                        '{"created_at": "%s", "actor": "private-release-smoke", "tenant_id": "tenant-smoke", "document_id": "doc_old", "action": "document.export"}'
                        % old_time,
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = _summarize_audit(
                Settings(data_dir=data_dir, tenant_storage_isolation=True),
                "tenant-smoke",
                actor="private-release-smoke",
                document_id="doc_new",
                since=datetime.now(timezone.utc),
            )

        self.assertEqual([], summary["actions"])
        self.assertEqual(0, summary["record_count"])

    def test_cli_can_generate_synthetic_sample_for_source_only_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_json = tmp_path / "private_release_smoke.json"
            exit_code = main(
                [
                    "--synthetic-sample",
                    "--data-dir",
                    str(tmp_path / "data"),
                    "--out-json",
                    str(out_json),
                ]
            )
            report = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertTrue(report["passed"])
        self.assertTrue(report["synthetic_sample"])
        self.assertFalse(report["handoff_evidence"])
        self.assertEqual("explicit", report["data_dir_mode"])


def _write_synthetic_regulation_docx(path: Path) -> Path:
    try:
        return write_synthetic_smoke_docx(path)
    except RuntimeError as exc:
        raise unittest.SkipTest(str(exc)) from exc


if __name__ == "__main__":
    unittest.main()
