from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock

from scripts.run_private_release_gate import run_private_release_gate


def _manifest(*, dirty: bool) -> dict[str, object]:
    return {
        "manifest_type": "private_release_handoff",
        "manifest_version": 1,
        "repo_commit": "a" * 40,
        "processing_contract": {
            "required_export_formats": {
                "value": ["jsonl", "csv", "markdown", "tables_jsonl", "tables_csv", "quality_json", "quality_md"]
            }
        },
        "repo_status": {
            "dirty": dirty,
            "changed_path_count": 2 if dirty else 0,
            "changed_paths_preview": [" M README.md"] if dirty else [],
        },
        "release_hygiene": {
            "observed_result": {
                "exit_code": 0,
                "finding_count": 0,
            }
        },
    }


def _smoke(*, passed: bool = True) -> dict[str, object]:
    return {
        "report_type": "private_release_smoke",
        "passed": passed,
        "data_dir_mode": "explicit",
        "handoff_evidence": True,
        "http": {
            "unauthorized_upload_status_code": 401,
            "missing_tenant_upload_status_code": 400,
            "authorized_upload_status_code": 200,
        },
        "audit": {
            "passed": passed,
            "auth_denial_passed": passed,
            "tenant_header_required_passed": passed,
            "record_count": 7,
        },
        "exports": [
            {"format": "jsonl", "status_code": 200, "exists": True},
            {"format": "csv", "status_code": 200, "exists": True},
            {"format": "markdown", "status_code": 200, "exists": True},
            {"format": "tables_jsonl", "status_code": 200, "exists": True},
            {"format": "tables_csv", "status_code": 200, "exists": True},
            {"format": "quality_json", "status_code": 200, "exists": True},
            {"format": "quality_md", "status_code": 200, "exists": True},
        ],
    }


def _secure_rag_smoke() -> dict[str, object]:
    return {
        "report_type": "secure_rag_smoke",
        "passed": True,
        "tenant_id": "tenant-a",
        "synthetic_runtime": True,
        "handoff_evidence": False,
        "indexing_status": "indexed",
        "search_result_count": 1,
        "runtime_ok": True,
        "evidence_summary": {
            "passed": True,
            "approval_chain_failure_count": 0,
            "metadata_failure_count": 0,
            "stale_vector_record_count": 0,
            "indexing_job_failure_count": 0,
            "audit_control_failure_count": 0,
        },
    }


def _mcp_smoke() -> dict[str, object]:
    return {
        "report_type": "local_mcp_smoke",
        "passed": True,
        "tenant_id": "tenant-a",
        "synthetic_runtime": True,
        "handoff_evidence": False,
        "document_count": 1,
        "search_result_count": 2,
        "fetch_has_text": True,
        "citation_has_approved_hash": True,
        "evidence_summary": {"passed": True},
    }


def _mcp_transport_smoke() -> dict[str, object]:
    return {
        "report_type": "mcp_transport_smoke",
        "passed": True,
        "tenant_id": "tenant-a",
        "transport": "stdio",
        "preparation": {"passed": True, "skipped": True, "evidence_passed": None},
        "full_profile": {"passed": True, "search_result_count": 2, "fetch_has_text": True},
    }


def _mcp_index_visibility() -> dict[str, object]:
    return {
        "report_type": "mcp_index_visibility_audit",
        "passed": True,
        "tenant_id": "tenant-a",
        "document_count": 1,
        "total_approved_chunks": 2,
        "total_mcp_visible_records": 2,
        "smoke_like_document_count": 0,
        "preapproval_visibility_guard": {"passed": True},
        "approval_journal_coverage": {"missing_record_count": 0},
        "approval_provenance_coverage": {"record_count": 2, "complete_record_count": 2},
    }


def _mcp_connection_readiness(*, deploy_ready: bool = False) -> dict[str, object]:
    return {
        "report_type": "mcp_connection_readiness",
        "passed": True,
        "deploy_ready": deploy_ready,
        "readiness_scope": "configuration",
        "client_profile": "claude-desktop",
        "connection_mode": "direct",
        "transport": "stdio",
        "high_count": 0,
        "medium_count": 0,
        "mcp_index_visibility_summary": {
            "passed": True,
            "tenant_id": "tenant-a",
            "total_indexable_record_count": 2,
            "total_mcp_visible_records": 2,
        },
    }


def _release_evidence_verification() -> dict[str, object]:
    return {
        "report_type": "release_evidence_bundle_verification",
        "evidence_profile": "mcp-product-readiness",
        "passed": True,
        "artifact_count": 9,
        "failure_count": 0,
        "warning_count": 0,
    }


def _mcp_handoff() -> dict[str, object]:
    return {
        "report_type": "mcp_handoff_report",
        "handoff_schema_version": 2,
        "passed": True,
        "handoff_ready": True,
        "decision": "ready_for_local_claude_desktop_mvp",
        "blocking_count": 0,
        "warning_count": 0,
        "server_name": "regulation-mcp",
        "product_summary": {
            "approval_journal_review_event_coverage": _approval_review_event_coverage(),
        },
    }


def _approval_review_event_coverage() -> dict[str, object]:
    return {
        "journal_record_count": 2,
        "applicable_record_count": 2,
        "chunk_reference_count": 2,
        "review_decision_event_count": 6,
        "expected_event_chunk_counts": {
            "ai_review_confirmed": 2,
            "approved": 2,
            "human_review_confirmed": 2,
        },
        "event_chunk_counts": {
            "ai_review_confirmed": 2,
            "approved": 2,
            "human_review_confirmed": 2,
        },
        "missing_event_chunk_counts": {
            "ai_review_confirmed": 0,
            "approved": 0,
            "human_review_confirmed": 0,
        },
        "incomplete_record_count": 0,
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_official_evidence_reports(directory: Path) -> dict[str, Path]:
    return {
        "mcp_transport_smoke_report_path": _write_json(directory / "mcp_transport.json", _mcp_transport_smoke()),
        "mcp_index_visibility_report_path": _write_json(directory / "mcp_visibility.json", _mcp_index_visibility()),
        "mcp_connection_readiness_report_path": _write_json(
            directory / "mcp_connection.json",
            _mcp_connection_readiness(),
        ),
        "mcp_handoff_report_path": _write_json(directory / "mcp_handoff.json", _mcp_handoff()),
        "release_evidence_verification_report_path": _write_json(
            directory / "release_evidence.json",
            _release_evidence_verification(),
        ),
    }


def _pass_private_release_base_mocks() -> object:
    return mock.patch.multiple(
        "scripts.run_private_release_gate",
        build_readiness_report=mock.DEFAULT,
        load_private_release_smoke_report=mock.DEFAULT,
        build_visibility_report=mock.DEFAULT,
        run_regression_gate=mock.DEFAULT,
        build_manifest=mock.DEFAULT,
    )


class PrivateReleaseGateTests(unittest.TestCase):
    def test_private_release_gate_requires_shared_deployment_by_default(self):
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ) as readiness, mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=_smoke(),
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": True, "checks": []},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True, "release_hygiene": {"include_source_path_scan": True}},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=False),
        ) as manifest:
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["require_shared_deployment"])
        self.assertEqual([], report["failed_check_names"])
        self.assertEqual("private_release_gate", report["report_type"])
        self.assertEqual("a" * 40, report["repo_commit"])
        self.assertEqual(6, report["check_count"])
        readiness.assert_called_once_with(require_shared_deployment=True)
        manifest.assert_called_once()
        self.assertTrue(manifest.call_args.kwargs["include_untracked"])

    def test_private_release_gate_fails_dirty_worktree_without_approval(self):
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=_smoke(),
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=True),
        ):
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
            )

        self.assertFalse(report["passed"])
        self.assertIn("clean_worktree_or_approved_dirty_release", report["failed_check_names"])

    def test_private_release_gate_allows_dirty_worktree_with_explicit_approval(self):
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=_smoke(),
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=True),
        ):
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                allow_dirty_worktree=True,
                dirty_worktree_approval="REL-123",
            )

        self.assertTrue(report["passed"])
        dirty_check = next(
            check for check in report["checks"] if check["name"] == "clean_worktree_or_approved_dirty_release"
        )
        self.assertEqual("REL-123", dirty_check["details"]["approval_reference"])

    def test_private_release_gate_fails_when_smoke_report_does_not_pass(self):
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=_smoke(passed=False),
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=False),
        ):
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
            )

        self.assertFalse(report["passed"])
        self.assertIn("private_release_smoke", report["failed_check_names"])

    def test_private_release_gate_rejects_temporary_smoke_evidence(self):
        smoke = _smoke()
        smoke["data_dir_mode"] = "temporary"
        smoke["handoff_evidence"] = False
        smoke["data_dir_name"] = "reg-rag-private-smoke-abc123"
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=smoke,
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=False),
        ):
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
            )

        self.assertFalse(report["passed"])
        self.assertIn("private_release_smoke", report["failed_check_names"])
        smoke_check = next(check for check in report["checks"] if check["name"] == "private_release_smoke")
        self.assertEqual("temporary", smoke_check["details"]["data_dir_mode"])
        self.assertFalse(smoke_check["details"]["handoff_evidence"])

    def test_private_release_gate_requires_all_manifest_export_formats_in_smoke(self):
        smoke = _smoke()
        smoke["exports"] = [item for item in smoke["exports"] if item["format"] != "quality_md"]
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=smoke,
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=False),
        ):
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
            )

        self.assertFalse(report["passed"])
        self.assertIn("private_release_smoke", report["failed_check_names"])
        smoke_check = next(check for check in report["checks"] if check["name"] == "private_release_smoke")
        self.assertEqual(["quality_md"], smoke_check["details"]["missing_export_formats"])

    def test_private_release_gate_fails_when_github_repository_is_not_private(self):
        with mock.patch(
            "scripts.run_private_release_gate.build_readiness_report",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.load_private_release_smoke_report",
            return_value=_smoke(),
        ), mock.patch(
            "scripts.run_private_release_gate.build_visibility_report",
            return_value={"passed": False, "failed_check_names": ["github_repository_private"]},
        ), mock.patch(
            "scripts.run_private_release_gate.run_regression_gate",
            return_value={"passed": True},
        ), mock.patch(
            "scripts.run_private_release_gate.build_manifest",
            return_value=_manifest(dirty=False),
        ):
            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
            )

        self.assertFalse(report["passed"])
        self.assertIn("github_repository_private", report["failed_check_names"])

    def test_private_release_gate_requires_official_rag_mcp_evidence_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["require_official_rag_mcp_evidence"])
        self.assertEqual(7, report["check_count"])
        self.assertEqual([], report["failed_check_names"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertTrue(official_check["passed"])
        self.assertEqual(5, official_check["details"]["report_count"])
        self.assertEqual([], official_check["details"]["failed_report_names"])

    def test_private_release_gate_rejects_mcp_transport_tenant_mismatch_with_visibility(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            transport = _mcp_transport_smoke()
            transport["tenant_id"] = "tenant-b"
            _write_json(evidence_paths["mcp_transport_smoke_report_path"], transport)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["mcp_transport_smoke"], official_check["details"]["failed_report_names"])
        self.assertIn(
            "tenant_id_mismatch_with_mcp_index_visibility",
            official_check["details"]["reports"]["mcp_transport_smoke"]["failed_fields"],
        )

    def test_private_release_gate_requires_connection_readiness_index_visibility_summary(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            connection = _mcp_connection_readiness()
            connection.pop("mcp_index_visibility_summary")
            _write_json(evidence_paths["mcp_connection_readiness_report_path"], connection)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["mcp_connection_readiness"], official_check["details"]["failed_report_names"])
        self.assertIn(
            "mcp_index_visibility_summary",
            official_check["details"]["reports"]["mcp_connection_readiness"]["failed_fields"],
        )

    def test_private_release_gate_uses_indexable_not_visible_count_for_mcp_lineage(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            connection = _mcp_connection_readiness()
            connection["mcp_index_visibility_summary"]["total_mcp_visible_records"] = 1
            _write_json(evidence_paths["mcp_connection_readiness_report_path"], connection)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertTrue(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual([], official_check["details"]["failed_report_names"])

    def test_private_release_gate_auto_requires_official_mcp_evidence_for_runtime_artifact(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                mcp_runtime_data_dir=Path("data") / "aks_mcp_publish_runtime",
                **evidence_paths,
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["require_official_rag_mcp_evidence"])
        self.assertEqual("mcp_runtime_or_bundle", report["official_rag_mcp_evidence_trigger"])
        self.assertEqual("data\\aks_mcp_publish_runtime", report["mcp_runtime_data_dir"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertTrue(official_check["passed"])

    def test_private_release_gate_fails_missing_official_evidence_for_mcp_bundle_artifact(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)

            report = run_private_release_gate(
                project_root=Path(tmp),
                workflow_scope="available",
                mcp_bundle_dir=Path("reports") / "mcp_connection_bundle",
            )

        self.assertFalse(report["passed"])
        self.assertTrue(report["require_official_rag_mcp_evidence"])
        self.assertEqual("mcp_runtime_or_bundle", report["official_rag_mcp_evidence_trigger"])
        self.assertIn("official_rag_mcp_evidence", report["failed_check_names"])

    def test_private_release_gate_fails_when_required_official_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            missing = Path(tmp) / "missing.json"

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                mcp_transport_smoke_report_path=missing,
                mcp_index_visibility_report_path=missing,
                mcp_connection_readiness_report_path=missing,
                mcp_handoff_report_path=missing,
                release_evidence_verification_report_path=missing,
            )

        self.assertFalse(report["passed"])
        self.assertIn("official_rag_mcp_evidence", report["failed_check_names"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(
            [
                "mcp_transport_smoke",
                "mcp_index_visibility",
                "mcp_connection_readiness",
                "mcp_handoff",
                "mcp_release_evidence_verification",
            ],
            official_check["details"]["failed_report_names"],
        )
        self.assertIn("load_error", official_check["details"]["reports"]["mcp_transport_smoke"]["failed_fields"])

    def test_private_release_gate_rejects_mcp_visibility_with_smoke_or_missing_approval_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            visibility = _mcp_index_visibility()
            visibility["smoke_like_document_count"] = 1
            visibility["approval_journal_coverage"] = {"missing_record_count": 1}
            _write_json(evidence_paths["mcp_index_visibility_report_path"], visibility)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        self.assertIn("official_rag_mcp_evidence", report["failed_check_names"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["mcp_index_visibility"], official_check["details"]["failed_report_names"])
        failed_fields = official_check["details"]["reports"]["mcp_index_visibility"]["failed_fields"]
        self.assertIn("smoke_like_document_count", failed_fields)
        self.assertIn("approval_journal_coverage.missing_record_count", failed_fields)

    def test_private_release_gate_rejects_mcp_handoff_with_missing_review_events(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            handoff = _mcp_handoff()
            coverage = handoff["product_summary"]["approval_journal_review_event_coverage"]
            coverage["event_chunk_counts"]["approved"] = 1
            coverage["missing_event_chunk_counts"]["approved"] = 1
            coverage["incomplete_record_count"] = 1
            _write_json(evidence_paths["mcp_handoff_report_path"], handoff)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        self.assertIn("official_rag_mcp_evidence", report["failed_check_names"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["mcp_handoff"], official_check["details"]["failed_report_names"])
        failed_fields = official_check["details"]["reports"]["mcp_handoff"]["failed_fields"]
        self.assertIn(
            "product_summary.approval_journal_review_event_coverage.missing_event_chunk_counts",
            failed_fields,
        )

    def test_private_release_gate_rejects_mcp_handoff_with_partial_review_event_keys(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            handoff = _mcp_handoff()
            coverage = handoff["product_summary"]["approval_journal_review_event_coverage"]
            coverage["event_chunk_counts"] = {"approved": 2}
            coverage["missing_event_chunk_counts"] = {"approved": 0}
            coverage["incomplete_record_count"] = 0
            _write_json(evidence_paths["mcp_handoff_report_path"], handoff)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        failed_fields = official_check["details"]["reports"]["mcp_handoff"]["failed_fields"]
        self.assertIn(
            "product_summary.approval_journal_review_event_coverage.required_event_types",
            failed_fields,
        )

    def test_private_release_gate_rejects_mcp_handoff_with_malformed_review_event_counts(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            handoff = _mcp_handoff()
            coverage = handoff["product_summary"]["approval_journal_review_event_coverage"]
            coverage["incomplete_record_count"] = "not-a-count"
            coverage["missing_event_chunk_counts"]["approved"] = "bad"
            _write_json(evidence_paths["mcp_handoff_report_path"], handoff)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        failed_fields = official_check["details"]["reports"]["mcp_handoff"]["failed_fields"]
        self.assertIn("product_summary.approval_journal_review_event_coverage.counts", failed_fields)

    def test_private_release_gate_rejects_legacy_mcp_handoff_schema(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            handoff = _mcp_handoff()
            handoff.pop("handoff_schema_version")
            _write_json(evidence_paths["mcp_handoff_report_path"], handoff)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        failed_fields = official_check["details"]["reports"]["mcp_handoff"]["failed_fields"]
        self.assertIn("handoff_schema_version", failed_fields)

    def test_private_release_gate_can_require_deploy_ready_mcp_connection(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            _write_json(
                evidence_paths["mcp_connection_readiness_report_path"],
                _mcp_connection_readiness(deploy_ready=False),
            )

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                require_deploy_ready_mcp_connection=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["mcp_connection_readiness"], official_check["details"]["failed_report_names"])
        self.assertIn("deploy_ready", official_check["details"]["reports"]["mcp_connection_readiness"]["failed_fields"])

    def test_private_release_gate_requires_mcp_product_readiness_evidence_profile(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            verification = _release_evidence_verification()
            verification["evidence_profile"] = "private-release"
            _write_json(evidence_paths["release_evidence_verification_report_path"], verification)

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["mcp_release_evidence_verification"], official_check["details"]["failed_report_names"])
        self.assertIn(
            "evidence_profile",
            official_check["details"]["reports"]["mcp_release_evidence_verification"]["failed_fields"],
        )

    def test_private_release_gate_rejects_synthetic_smoke_when_supplied_as_official_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, _pass_private_release_base_mocks() as patched:
            patched["build_readiness_report"].return_value = {"passed": True}
            patched["load_private_release_smoke_report"].return_value = _smoke()
            patched["build_visibility_report"].return_value = {"passed": True}
            patched["run_regression_gate"].return_value = {"passed": True}
            patched["build_manifest"].return_value = _manifest(dirty=False)
            evidence_paths = _write_official_evidence_reports(Path(tmp))
            evidence_paths["secure_rag_smoke_report_path"] = _write_json(
                Path(tmp) / "secure_rag_synthetic.json",
                _secure_rag_smoke(),
            )
            evidence_paths["mcp_smoke_report_path"] = _write_json(
                Path(tmp) / "mcp_smoke_synthetic.json",
                _mcp_smoke(),
            )

            report = run_private_release_gate(
                project_root=Path(__file__).resolve().parents[1],
                workflow_scope="available",
                require_official_rag_mcp_evidence=True,
                **evidence_paths,
            )

        self.assertFalse(report["passed"])
        official_check = next(check for check in report["checks"] if check["name"] == "official_rag_mcp_evidence")
        self.assertEqual(["secure_rag_smoke", "mcp_smoke"], official_check["details"]["failed_report_names"])
        self.assertIn("synthetic_runtime", official_check["details"]["reports"]["secure_rag_smoke"]["failed_fields"])
        self.assertIn("handoff_evidence", official_check["details"]["reports"]["mcp_smoke"]["failed_fields"])


if __name__ == "__main__":
    unittest.main()
