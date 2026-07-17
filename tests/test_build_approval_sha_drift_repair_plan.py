from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_approval_sha_drift_repair_plan import (
    build_approval_sha_drift_repair_plan,
    main,
)


class ApprovalShaDriftRepairPlanTests(unittest.TestCase):
    def test_passes_when_source_vector_and_journal_hashes_align(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_report = _write_runtime_fixture(root, vector_worklist_sha=None, journal_batch_sha=None)

            report = build_approval_sha_drift_repair_plan(
                publish_runtime_report=runtime_report,
                repo_root=root,
                out_json=root / "reports" / "repair.json",
                out_md=root / "reports" / "repair.md",
            )

        self.assertTrue(report["passed"])
        self.assertEqual("approval_sha_lineage_ready", report["release_gate_status"])
        self.assertEqual([], report["blockers"])
        self.assertTrue(report["vector_approval_sha_status"]["passed"])
        self.assertTrue(report["approval_journal_sha_status"]["passed"])
        self.assertIn("rerun_release_evidence_verification", {step["step"] for step in report["repair_sequence"]})

    def test_builds_repair_steps_for_source_vector_and_journal_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_report = _write_runtime_fixture(
                root,
                claimed_worklist_sha="0" * 64,
                vector_worklist_sha="1" * 64,
                journal_batch_sha="2" * 64,
            )

            report = build_approval_sha_drift_repair_plan(
                publish_runtime_report=runtime_report,
                repo_root=root,
            )

        self.assertFalse(report["passed"])
        codes = set(report["blocker_codes"])
        self.assertIn("approval-source-artifact-sha-drift", codes)
        self.assertIn("publish-runtime-vector-approval-sha-drift", codes)
        self.assertIn("publish-runtime-journal-approval-sha-drift", codes)
        steps = [step["step"] for step in report["repair_sequence"]]
        self.assertLess(
            steps.index("refresh_publish_runtime_approval_evidence"),
            steps.index("reindex_or_regenerate_approved_vectors"),
        )
        self.assertIn("repair_approval_journal_through_review_workflow", steps)
        self.assertFalse(report["operator_controls"]["direct_vector_metadata_patch_allowed"])
        self.assertFalse(report["operator_controls"]["direct_journal_edit_allowed"])

    def test_cli_writes_artifacts_and_can_fail_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_report = _write_runtime_fixture(root, vector_worklist_sha="1" * 64, journal_batch_sha=None)
            out_json = root / "reports" / "repair.json"
            out_md = root / "reports" / "repair.md"

            exit_code = main(
                [
                    "--publish-runtime-report",
                    str(runtime_report),
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-drift",
                ]
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(1, exit_code)
        self.assertIn("publish-runtime-vector-approval-sha-drift", payload["blocker_codes"])
        self.assertIn("Approval SHA Drift Repair Plan", markdown)

    def test_malformed_jsonl_blocks_without_silent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_report = _write_runtime_fixture(root, vector_worklist_sha=None, journal_batch_sha=None)
            journal_path = (
                root
                / "data"
                / "aks_mcp_publish_runtime"
                / "tenants"
                / "tenant-aks-publish"
                / "repository"
                / "journals"
                / "approvals.jsonl"
            )
            journal_path.write_text("not-json\n" + journal_path.read_text(encoding="utf-8"), encoding="utf-8")

            report = build_approval_sha_drift_repair_plan(
                publish_runtime_report=runtime_report,
                repo_root=root,
            )

        self.assertFalse(report["passed"])
        self.assertIn("publish-runtime-journal-malformed-jsonl", report["blocker_codes"])
        self.assertNotIn("publish-runtime-journal-approval-sha-drift", report["blocker_codes"])
        self.assertEqual(1, report["approval_journal_sha_status"]["malformed_line_count"])
        self.assertEqual(1, report["approval_journal_sha_status"]["record_count"])

    def test_missing_vector_file_reports_missing_without_sha_drift_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_report = _write_runtime_fixture(root, vector_worklist_sha=None, journal_batch_sha=None)
            vector_path = (
                root
                / "data"
                / "aks_mcp_publish_runtime"
                / "tenants"
                / "tenant-aks-publish"
                / "vector_db"
                / "tenant-aks-publish"
                / "approved_vectors.jsonl"
            )
            vector_path.unlink()

            report = build_approval_sha_drift_repair_plan(
                publish_runtime_report=runtime_report,
                repo_root=root,
            )

        self.assertFalse(report["passed"])
        self.assertIn("publish-runtime-vector-file-missing", report["blocker_codes"])
        self.assertNotIn("publish-runtime-vector-approval-sha-drift", report["blocker_codes"])
        self.assertEqual("file_missing", report["vector_approval_sha_status"]["status"])


def _write_runtime_fixture(
    root: Path,
    *,
    claimed_worklist_sha: str | None = None,
    claimed_batch_sha: str | None = None,
    vector_worklist_sha: str | None,
    journal_batch_sha: str | None,
) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist = root / "data" / "aks_mcp_publish_runtime" / "reports" / "worklist.json"
    review_batches = root / "data" / "aks_mcp_publish_runtime" / "reports" / "review_batches.json"
    worklist.parent.mkdir(parents=True, exist_ok=True)
    _write_json(worklist, {"report_type": "approval_worklist", "document_count": 1})
    _write_json(review_batches, {"report_type": "approval_review_batch_manifest", "batch_count": 1})
    worklist_sha = _sha256_file(worklist)
    batch_sha = _sha256_file(review_batches)

    tenant_dir = root / "data" / "aks_mcp_publish_runtime" / "tenants" / "tenant-aks-publish"
    vector_path = tenant_dir / "vector_db" / "tenant-aks-publish" / "approved_vectors.jsonl"
    journal_path = tenant_dir / "repository" / "journals" / "approvals.jsonl"
    vector_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    vector_path.write_text(
        json.dumps(
            {
                "id": "doc:chunk-1",
                "metadata": {
                    "approval_worklist_report_sha256": vector_worklist_sha or worklist_sha,
                    "approval_review_batch_manifest_sha256": batch_sha,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal_path.write_text(
        json.dumps(
            {
                "approval_id": "approval-1",
                "worklist_evidence": {
                    "worklist_report_sha256": worklist_sha,
                    "review_batch_manifest_sha256": journal_batch_sha or batch_sha,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runtime_report = reports / "aks_mcp_publish_runtime_report.json"
    _write_json(
        runtime_report,
        {
            "report_type": "aks_mcp_publish_runtime",
            "tenant_id": "tenant-aks-publish",
            "target_data_dir": "data/aks_mcp_publish_runtime",
            "tenant_data_dir": "data/aks_mcp_publish_runtime/tenants/tenant-aks-publish",
            "approval_evidence": {
                "worklist_report_path": "reports/worklist.json",
                "worklist_report_sha256": claimed_worklist_sha or worklist_sha,
                "review_batch_manifest_path": "reports/review_batches.json",
                "review_batch_manifest_sha256": claimed_batch_sha or batch_sha,
                "artifacts": {
                    "worklist_json": "data\\aks_mcp_publish_runtime\\reports\\worklist.json",
                    "review_batch_manifest_json": "data\\aks_mcp_publish_runtime\\reports\\review_batches.json",
                },
            },
        },
    )
    return runtime_report


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
