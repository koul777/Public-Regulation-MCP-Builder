from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_release_evidence_index as evidence_index
from scripts import verify_release_evidence_bundle as verify


REQUIRED_REPORTS = (
    "reports/private_release_gate_current.json",
    "reports/private_release_readiness_current.json",
    "reports/private_release_manifest_current.json",
    "reports/github_private_visibility_current.json",
    "reports/release_hygiene_current.json",
    "reports/private_release_smoke_current.json",
)
COMMIT = "a" * 40
REQUIRED_REPORT_PAYLOADS = {
    "reports/private_release_gate_current.json": {
        "report_type": "private_release_gate",
        "repo_commit": COMMIT,
        "passed": True,
        "failed_check_names": [],
    },
    "reports/private_release_readiness_current.json": {
        "report_type": "private_release_readiness",
        "repo_commit": COMMIT,
        "passed": True,
        "failed_check_names": [],
    },
    "reports/private_release_manifest_current.json": {
        "manifest_type": "private_release_handoff",
        "repo_commit": COMMIT,
    },
    "reports/github_private_visibility_current.json": {
        "report_type": "github_private_visibility",
        "repo_commit": COMMIT,
        "passed": True,
        "failed_check_names": [],
    },
    "reports/release_hygiene_current.json": {
        "report_type": "release_hygiene",
        "repo_commit": COMMIT,
        "passed": True,
    },
    "reports/private_release_smoke_current.json": {
        "report_type": "private_release_smoke",
        "repo_commit": COMMIT,
        "passed": True,
        "data_dir_mode": "explicit",
        "handoff_evidence": True,
    },
}


class VerifyReleaseEvidenceBundleTests(unittest.TestCase):
    def test_verifies_passing_index_against_real_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index = _write_valid_bundle(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["failure_count"])
        self.assertEqual(COMMIT, report["repo_commit"])

    def test_fails_when_index_claims_deleted_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index = _write_valid_bundle(root)
            (root / "reports/private_release_gate_current.json").unlink()

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("artifact_exists_mismatch", checks)
        self.assertIn("artifact_file_present", checks)

    def test_fails_when_index_hash_is_stale_after_artifact_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index = _write_valid_bundle(root)
            (root / "reports/private_release_gate_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "private_release_gate",
                        "repo_commit": COMMIT,
                        "passed": False,
                        "failed_check_names": ["dirty_worktree"],
                    }
                ),
                encoding="utf-8",
            )

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("artifact_sha256_mismatch", checks)
        self.assertIn("json_artifact_passed", checks)
        self.assertIn("json_artifact_failed_check_names", checks)

    def test_fails_incomplete_private_release_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "reports/private_release_gate_current.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text('{"report_type": "private_release_gate", "passed": true}\n', encoding="utf-8")
            index = evidence_index.build_release_evidence_index(
                root,
                artifact_paths=["reports/private_release_gate_current.json"],
            )

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("required_report_artifacts_present", checks)
        self.assertIn("required_wheel_artifact_present", checks)
        self.assertIn("required_sdist_artifact_present", checks)

    def test_fails_unknown_index_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index = _write_valid_bundle(root)
            index["index_version"] = 2

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("index_version", checks)

    def test_fails_wrong_required_report_marker_even_when_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            (root / "reports/private_release_manifest_current.json").write_text(
                json.dumps({"report_type": "private_release_manifest", "repo_commit": COMMIT, "passed": True}) + "\n",
                encoding="utf-8",
            )
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_expected_marker", checks)

    def test_surfaces_release_hygiene_suppressions_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            (root / "reports/release_hygiene_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "release_hygiene",
                        "repo_commit": COMMIT,
                        "passed": True,
                        "raw_finding_count": 2,
                        "finding_count": 0,
                        "suppressed_finding_count": 2,
                        "allowlist": {
                            "missing_approval_metadata_count": 0,
                            "non_attributable_approval_count": 0,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _write_allowlist(root)
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual("json_artifact_suppressed_finding_count", report["warnings"][0]["check"])
        self.assertIn(".release-hygiene-allowlist.json", {artifact["artifact_path"] for artifact in index["artifacts"]})

    def test_fails_release_hygiene_suppressions_without_approval_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            (root / "reports/release_hygiene_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "release_hygiene",
                        "repo_commit": COMMIT,
                        "passed": True,
                        "raw_finding_count": 2,
                        "finding_count": 0,
                        "suppressed_finding_count": 2,
                        "allowlist": {
                            "missing_approval_metadata_count": 1,
                            "non_attributable_approval_count": 0,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _write_allowlist(root)
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["warning_count"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_suppression_approval_metadata", checks)

    def test_fails_release_hygiene_suppressions_without_indexed_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            (root / "reports/release_hygiene_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "release_hygiene",
                        "repo_commit": COMMIT,
                        "passed": True,
                        "raw_finding_count": 2,
                        "finding_count": 0,
                        "suppressed_finding_count": 2,
                        "allowlist": {"missing_approval_metadata_count": 0},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("required_release_hygiene_allowlist_artifact_present", checks)

    def test_fails_mismatched_required_report_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            (root / "reports/private_release_smoke_current.json").write_text(
                json.dumps(
                    {
                        "report_type": "private_release_smoke",
                        "repo_commit": "b" * 40,
                        "passed": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_repo_commit_consistency", checks)

    def test_fails_private_smoke_without_handoff_data_dir_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            smoke_path = root / "reports/private_release_smoke_current.json"
            smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
            smoke["data_dir_mode"] = "temporary"
            smoke["handoff_evidence"] = False
            smoke_path.write_text(json.dumps(smoke) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_private_smoke_handoff_evidence", checks)

    def test_fails_malformed_required_report_commit_even_when_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            for report in REQUIRED_REPORTS:
                path = root / report
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["repo_commit"] = "abc123"
                path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root)

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_repo_commit", checks)

    def test_fails_unsafe_artifact_path(self) -> None:
        report = verify.verify_release_evidence_index(
            {
                "index_type": "release_evidence_index",
                "index_version": 1,
                "artifacts": [
                    {
                        "artifact_path": "../outside.json",
                        "exists": True,
                        "size_bytes": 10,
                        "sha256": "c" * 64,
                    }
                ],
            },
            repo_root=Path.cwd(),
        )

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("artifact_path_safe", checks)

    def test_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index = _write_valid_bundle(root)
            index_json = root / "reports" / "release_evidence_index.json"
            out_json = root / "reports" / "release_evidence_verification.json"
            index_json.write_text(json.dumps(index), encoding="utf-8")
            stdout = io.StringIO()

            exit_code = verify.main(
                [
                    "--index-json",
                    str(index_json),
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(out_json),
                ],
                stdout=stdout,
            )
            written = json.loads(out_json.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(written, printed)
        self.assertTrue(written["passed"])

    def test_index_cli_output_verifies_as_private_release_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_required_reports(root)
            _write_dist_artifacts(root)
            index_json = root / "reports" / "release_evidence_index.json"
            verification_json = root / "reports" / "release_evidence_verification.json"

            index_exit_code = evidence_index.main(
                [
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(index_json),
                ],
                stdout=io.StringIO(),
            )
            verify_exit_code = verify.main(
                [
                    "--index-json",
                    str(index_json),
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(verification_json),
                ],
                stdout=io.StringIO(),
            )
            verification = json.loads(verification_json.read_text(encoding="utf-8"))

        self.assertEqual(0, index_exit_code)
        self.assertEqual(0, verify_exit_code)
        self.assertTrue(verification["passed"])
        self.assertEqual(0, verification["failure_count"])

    def test_hermes_mcp_profile_cli_output_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_hermes_mcp_artifacts(root)
            index_json = root / "reports" / "hermes_release_evidence_index.json"
            verification_json = root / "reports" / "hermes_release_evidence_verification.json"

            index_exit_code = evidence_index.main(
                [
                    "--repo-root",
                    str(root),
                    "--profile",
                    "hermes-mcp",
                    "--out-json",
                    str(index_json),
                ],
                stdout=io.StringIO(),
            )
            verify_exit_code = verify.main(
                [
                    "--index-json",
                    str(index_json),
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(verification_json),
                ],
                stdout=io.StringIO(),
            )
            index_payload = json.loads(index_json.read_text(encoding="utf-8"))
            verification = json.loads(verification_json.read_text(encoding="utf-8"))

        self.assertEqual(0, index_exit_code)
        self.assertEqual(0, verify_exit_code)
        self.assertEqual("hermes-mcp", index_payload["evidence_profile"])
        self.assertEqual("hermes-mcp", verification["evidence_profile"])
        self.assertTrue(verification["passed"])
        self.assertEqual(0, verification["failure_count"])
        self.assertNotIn(
            "reports/private_release_gate_current.json",
            {artifact["artifact_path"] for artifact in index_payload["artifacts"]},
        )

    def test_mcp_product_readiness_profile_cli_output_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            index_json = root / "reports" / "mcp_release_evidence_index.json"
            verification_json = root / "reports" / "mcp_release_evidence_verification.json"

            index_exit_code = evidence_index.main(
                [
                    "--repo-root",
                    str(root),
                    "--profile",
                    "mcp-product-readiness",
                    "--out-json",
                    str(index_json),
                ],
                stdout=io.StringIO(),
            )
            verify_exit_code = verify.main(
                [
                    "--index-json",
                    str(index_json),
                    "--repo-root",
                    str(root),
                    "--out-json",
                    str(verification_json),
                ],
                stdout=io.StringIO(),
            )
            index_payload = json.loads(index_json.read_text(encoding="utf-8"))
            verification = json.loads(verification_json.read_text(encoding="utf-8"))

        self.assertEqual(0, index_exit_code)
        self.assertEqual(0, verify_exit_code)
        self.assertEqual("mcp-product-readiness", index_payload["evidence_profile"])
        self.assertEqual("mcp-product-readiness", verification["evidence_profile"])
        self.assertTrue(verification["passed"])
        self.assertEqual(0, verification["failure_count"])
        self.assertEqual(2, verification["warning_count"])
        self.assertEqual(
            {"json_artifact_diagnostic_not_passed"},
            {warning["check"] for warning in verification["warnings"]},
        )
        self.assertIn(
            "reports/mcp_readiness_authority_current.json",
            {artifact["artifact_path"] for artifact in index_payload["artifacts"]},
        )

    def test_fails_mcp_product_readiness_profile_with_runtime_vector_approval_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            runtime_report = root / "reports" / "aks_mcp_publish_runtime_report.json"
            runtime_payload = json.loads(runtime_report.read_text(encoding="utf-8"))
            runtime_payload["target_data_dir"] = "data/aks_mcp_publish_runtime"
            runtime_payload["tenant_data_dir"] = "data/aks_mcp_publish_runtime/tenants/tenant-aks-publish"
            runtime_report.write_text(json.dumps(runtime_payload) + "\n", encoding="utf-8")

            approval_evidence = runtime_payload["approval_evidence"]
            tenant_dir = root / "data" / "aks_mcp_publish_runtime" / "tenants" / "tenant-aks-publish"
            vector_path = tenant_dir / "vector_db" / "tenant-aks-publish" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True, exist_ok=True)
            vector_path.write_text(
                json.dumps(
                    {
                        "id": "doc:chunk-1",
                        "metadata": {
                            "approval_worklist_report_sha256": "0" * 64,
                            "approval_review_batch_manifest_sha256": approval_evidence[
                                "review_batch_manifest_sha256"
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            journal_path = tenant_dir / "repository" / "journals" / "approvals.jsonl"
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.write_text(
                json.dumps(
                    {
                        "approval_id": "approval-1",
                        "worklist_evidence": {
                            "worklist_report_sha256": approval_evidence["worklist_report_sha256"],
                            "review_batch_manifest_sha256": "1" * 64,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")
            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failure_checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_publish_runtime_vector_approval_sha256", failure_checks)
        self.assertIn("json_artifact_publish_runtime_journal_approval_sha256", failure_checks)

    def test_fails_mcp_product_readiness_profile_without_reapproval_burden_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")
            index["artifacts"] = [
                artifact
                for artifact in index["artifacts"]
                if artifact.get("artifact_path")
                not in {
                    "reports/reapproval_review_batch_decisions_current.csv",
                    "reports/reapproval_decision_validation_current.json",
                    "reports/reapproval_decision_validation_current.md",
                    "reports/reapproval_review_burden_current.json",
                    "reports/reapproval_review_burden_current.md",
                }
            ]

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "required_mcp_product_readiness_artifacts_present"
        ]
        self.assertTrue(failures)
        self.assertEqual(
            [
                "reports/reapproval_decision_validation_current.json",
                "reports/reapproval_decision_validation_current.md",
                "reports/reapproval_review_batch_decisions_current.csv",
                "reports/reapproval_review_burden_current.json",
                "reports/reapproval_review_burden_current.md",
            ],
            failures[0]["missing_artifacts"],
        )

    def test_fails_mcp_product_readiness_profile_with_mismatched_report_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            demo = root / "reports/mcp_demo_answers_current.json"
            payload = json.loads(demo.read_text(encoding="utf-8"))
            payload["repo_commit"] = "b" * 40
            demo.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_repo_commit_consistency", checks)

    def test_fails_mcp_product_readiness_profile_without_required_authority_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            authority = root / "reports/mcp_readiness_authority_current.json"
            payload = json.loads(authority.read_text(encoding="utf-8"))
            payload["authoritative_artifacts"] = [
                item
                for item in payload["authoritative_artifacts"]
                if item.get("role") in {"product_readiness", "mcp_demo_answers"}
            ]
            authority.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_authority_required_roles"
        ]
        self.assertTrue(failures)
        self.assertEqual(
            ["mcp_connection_readiness", "mcp_index_visibility", "mcp_transport_smoke"],
            failures[0]["missing_roles"],
        )

    def test_fails_mcp_product_readiness_profile_with_wrong_authority_role_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            authority = root / "reports/mcp_readiness_authority_current.json"
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(authority.read_text(encoding="utf-8"))
            for item in payload["authoritative_artifacts"]:
                if item.get("role") == "mcp_demo_answers":
                    item["path"] = "reports/mcp_product_readiness_current.json"
                    item["byte_count"] = product.stat().st_size
                    item["sha256"] = _sha256_file(product)
            authority.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_authority_role_path"
        ]
        self.assertTrue(failures)
        self.assertEqual("mcp_demo_answers", failures[0]["role"])
        self.assertEqual("reports/mcp_demo_answers_current.json", failures[0]["expected_path"])

    def test_fails_mcp_product_readiness_profile_with_wrong_authority_role_report_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            demo = root / "reports/mcp_demo_answers_current.json"
            demo.write_text(
                json.dumps({"report_type": "mcp_product_readiness", "repo_commit": COMMIT, "passed": True})
                + "\n",
                encoding="utf-8",
            )
            authority = root / "reports/mcp_readiness_authority_current.json"
            payload = json.loads(authority.read_text(encoding="utf-8"))
            for item in payload["authoritative_artifacts"]:
                if item.get("role") == "mcp_demo_answers":
                    item["byte_count"] = demo.stat().st_size
                    item["sha256"] = _sha256_file(demo)
            authority.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_authority_role_json"
            and failure["role"] == "mcp_demo_answers"
        ]
        self.assertTrue(failures)
        self.assertEqual("mcp_demo_answers", failures[0]["expected_report_type"])

    def test_fails_mcp_product_readiness_profile_without_source_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload.pop("source_report_artifacts")
            payload.pop("source_report_artifact_summary")
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_source_fingerprints", checks)

    def test_fails_mcp_product_readiness_profile_without_table_claim_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload.pop("table_preprocessing_claim_gate_summary")
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_table_claim_summary", checks)

    def test_fails_mcp_product_readiness_profile_without_table_claim_source_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["source_report_artifacts"] = [
                item
                for item in payload["source_report_artifacts"]
                if item.get("role") != "table_preprocessing_claim_gate_report"
            ]
            source_count = len(payload["source_report_artifacts"])
            payload["source_report_artifact_summary"]["provided_count"] = source_count
            payload["source_report_artifact_summary"]["sha256_count"] = source_count
            payload["source_report_artifact_summary"]["payload_generated_at_count"] = source_count
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_table_claim_source", checks)

    def test_fails_mcp_product_readiness_profile_with_unready_table_claim_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            summary = payload["table_preprocessing_claim_gate_summary"]
            summary["passed"] = False
            summary["status"] = "blocked_evidence_drift"
            summary["pending_unit_count"] = 255
            summary["source_traceability_issue_count"] = 10
            summary["drift_check_passed"] = False
            summary["drift_check_blocker_count"] = 1
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_table_claim_ready", checks)
        self.assertIn("json_artifact_mcp_product_table_claim_blockers", checks)
        self.assertIn("json_artifact_mcp_product_table_claim_drift_check", checks)

    def test_fails_mcp_product_readiness_profile_without_table_source_page_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["table_preprocessing_claim_gate_summary"][
                "source_traceability_require_page_count_verification"
            ] = False
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_table_claim_source_page_count_verification", checks)

    def test_fails_mcp_product_readiness_profile_with_stale_source_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            source = root / "reports/batch_quality_current.json"
            source.write_text('{"report_type": "batch_quality", "passed": false}\n', encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_source_sha256", checks)

    def test_fails_mcp_product_readiness_profile_with_malformed_source_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["source_report_artifacts"][0]["sha256"] = "not-a-sha"
            payload["source_report_artifacts"][0]["byte_count"] = "123"
            payload["source_report_artifact_summary"]["sha256_count"] = 1
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_source_fingerprints_complete", checks)
        self.assertIn("json_artifact_mcp_product_source_fingerprint_complete", checks)

    def test_fails_mcp_product_readiness_profile_without_worklist_source_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["source_report_artifacts"] = [
                item
                for item in payload["source_report_artifacts"]
                if item.get("role")
                not in {
                    "approval_worklist_report",
                    "approval_review_batch_manifest_report",
                    "reapproval_worklist_report",
                    "reapproval_review_batch_manifest_report",
                    "reapproval_decision_validation_report",
                    "reapproval_apply_plan_report",
                }
            ]
            payload["source_report_artifact_summary"]["sha256_count"] = len(payload["source_report_artifacts"])
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_approval_worklist_source", checks)
        self.assertIn("json_artifact_mcp_product_approval_review_batch_source", checks)
        self.assertIn("json_artifact_mcp_product_reapproval_worklist_source", checks)
        self.assertIn("json_artifact_mcp_product_reapproval_review_batch_source", checks)
        self.assertIn("json_artifact_mcp_product_reapproval_decision_validation_source", checks)
        self.assertIn("json_artifact_mcp_product_reapproval_apply_plan_source", checks)

    def test_fails_mcp_product_readiness_profile_without_reapproval_batch_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload.pop("reapproval_review_batch_summary")
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_reapproval_review_batch_summary", checks)

    def test_fails_mcp_product_readiness_profile_with_incomplete_reapproval_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["reapproval_decision_validation_summary"]["blocking_count"] = 1
            payload["reapproval_decision_validation_summary"]["blank_or_incomplete_row_count"] = 1
            payload["reapproval_decision_validation_summary"]["complete_row_count"] = 59
            payload["reapproval_decision_validation_summary"]["release_gate_status_counts"] = {
                "blocked_pending_operator_decisions": 1
            }
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_reapproval_decision_validation_complete", checks)

    def test_fails_mcp_product_readiness_profile_without_reapproval_apply_plan_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload.pop("reapproval_apply_plan_summary")
            payload["source_report_artifacts"] = [
                item
                for item in payload["source_report_artifacts"]
                if item.get("role") != "reapproval_apply_plan_report"
            ]
            payload["source_report_artifact_summary"]["sha256_count"] = len(payload["source_report_artifacts"])
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_reapproval_apply_plan_summary", checks)
        self.assertIn("json_artifact_mcp_product_reapproval_apply_plan_source", checks)

    def test_fails_mcp_product_readiness_profile_with_unsafe_reapproval_apply_plan_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["reapproval_apply_plan_summary"]["unsafe_contract_violation_count"] = 1
            payload["reapproval_apply_plan_summary"]["required_execution_steps"] = [
                step
                for step in payload["reapproval_apply_plan_summary"]["required_execution_steps"]
                if step != "validate_approval_preconditions"
            ]
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_product_reapproval_apply_plan_safe"
        ]
        self.assertTrue(failures)
        self.assertIn("validate_approval_preconditions", failures[0]["missing_required_execution_steps"])

    def test_fails_mcp_product_readiness_profile_with_unobserved_reapproval_apply_plan_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["reapproval_apply_plan_summary"]["observed_execution_step_counts"].pop(
                "record_apply_audit_event"
            )
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_product_reapproval_apply_plan_safe"
        ]
        self.assertTrue(failures)
        self.assertIn("record_apply_audit_event", failures[0]["missing_observed_execution_steps"])

    def test_fails_mcp_product_readiness_profile_with_revision_summary_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["revision_impact_summary"] = {
                "report_count": 1,
                "changed_count": 12,
                "added_count": 9,
                "removed_count": 4,
                "metadata_only_changed_count": 3,
                "approval_required_count": 25,
                "approval_reuse_candidate_count": 5980,
                "deindex_required_count": 4,
            }
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_product_revision_impact_source", checks)

    def test_fails_mcp_product_readiness_profile_without_reapproval_provenance_summary_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            for key in (
                "approval_provenance_missing_chunks",
                "approval_provenance_only_chunks",
                "approval_provenance_missing_field_counts",
            ):
                payload["reapproval_workload_summary"].pop(key)
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_product_reapproval_workload_summary"
        ]
        self.assertTrue(failures)
        self.assertIn("approval_provenance_missing_chunks", failures[0]["missing_fields"])
        self.assertIn("approval_provenance_missing_field_counts", failures[0]["missing_fields"])

    def test_fails_mcp_product_readiness_profile_with_reapproval_batch_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["reapproval_review_batch_summary"]["selected_candidate_count"] = 5900
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_product_reapproval_review_batch_coverage"
        ]
        self.assertTrue(failures)
        self.assertEqual(5997, failures[0]["reapproval_candidate_chunks"])
        self.assertEqual(5900, failures[0]["mismatched_fields"]["selected_candidate_count"])

    def test_accepts_repo_root_absolute_product_source_fingerprint_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["source_report_artifacts"][0]["path"] = str(root / "reports/batch_quality_current.json")
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            product_sha = _sha256_file(product)
            authority = root / "reports/mcp_readiness_authority_current.json"
            authority_payload = json.loads(authority.read_text(encoding="utf-8"))
            authority_payload["authoritative_artifacts"][0]["byte_count"] = product.stat().st_size
            authority_payload["authoritative_artifacts"][0]["sha256"] = product_sha
            authority.write_text(json.dumps(authority_payload) + "\n", encoding="utf-8")
            authority_sha = _sha256_file(authority)
            handoff = root / "reports/mcp_handoff_current.json"
            handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
            for item in handoff_payload["source_report_artifacts"]:
                if item["role"] == "product_readiness_report":
                    item["byte_count"] = product.stat().st_size
                    item["sha256"] = product_sha
                if item["role"] == "authority_manifest":
                    item["byte_count"] = authority.stat().st_size
                    item["sha256"] = authority_sha
            handoff.write_text(json.dumps(handoff_payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertTrue(report["passed"])

    def test_fails_mcp_product_readiness_profile_with_stale_authority_product_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            authority = root / "reports/mcp_readiness_authority_current.json"
            payload = json.loads(authority.read_text(encoding="utf-8"))
            payload["authoritative_artifacts"][0]["sha256"] = "0" * 64
            authority.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_authority_product_sha256", checks)

    def test_fails_mcp_product_readiness_profile_without_handoff_authority_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["source_report_artifacts"] = [
                item
                for item in payload["source_report_artifacts"]
                if item.get("role") != "authority_manifest"
            ]
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_handoff_source_fingerprints", checks)

    def test_fails_mcp_product_readiness_profile_with_nonpassing_handoff_source_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            for item in payload["source_report_artifacts"]:
                if item.get("role") == "product_readiness_report":
                    item["passed"] = False
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_handoff_source_report_passed", checks)
        self.assertNotIn("json_artifact_mcp_handoff_source_fingerprint_complete", checks)

    def test_fails_mcp_product_readiness_profile_without_handoff_approval_journal_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["mcp_index_visibility_summary"].pop("approval_journal_coverage")
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_handoff_approval_journal_coverage", checks)

    def test_fails_mcp_product_readiness_profile_with_handoff_approval_journal_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            journal = payload["mcp_index_visibility_summary"]["approval_journal_coverage"]
            journal["matched_record_count"] = 5996
            journal["missing_record_count"] = 1
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_handoff_approval_journal_coverage"
        ]
        self.assertTrue(failures)
        self.assertEqual(1, failures[0]["approval_journal_coverage"]["missing_record_count"])

    def test_fails_mcp_product_readiness_profile_with_handoff_approval_review_event_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            coverage = payload["product_summary"]["approval_journal_review_event_coverage"]
            coverage["event_chunk_counts"]["approved"] = 5996
            coverage["missing_event_chunk_counts"]["approved"] = 1
            coverage["incomplete_record_count"] = 1
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_handoff_approval_journal_review_event_coverage"
        ]
        self.assertTrue(failures)
        self.assertEqual(
            1,
            failures[0]["approval_journal_review_event_coverage"]["missing_event_chunk_counts"]["approved"],
        )

    def test_fails_mcp_product_readiness_profile_with_partial_handoff_approval_review_event_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            coverage = payload["product_summary"]["approval_journal_review_event_coverage"]
            coverage["event_chunk_counts"] = {"approved": 5997}
            coverage["missing_event_chunk_counts"] = {"approved": 0}
            coverage["incomplete_record_count"] = 0
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_handoff_approval_journal_review_event_coverage"
        ]
        self.assertTrue(failures)
        self.assertEqual(
            "approval_journal_review_event_coverage is missing required event types",
            failures[0]["reason"],
        )

    def test_fails_mcp_product_readiness_profile_with_malformed_handoff_approval_review_event_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            coverage = payload["product_summary"]["approval_journal_review_event_coverage"]
            coverage["incomplete_record_count"] = "not-a-count"
            coverage["missing_event_chunk_counts"]["approved"] = "bad"
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_handoff_approval_journal_review_event_coverage"
        ]
        self.assertTrue(failures)
        self.assertIn("incomplete_record_count", failures[0]["malformed_count_fields"])
        self.assertIn("missing_event_chunk_counts.approved", failures[0]["malformed_count_fields"])

    def test_fails_mcp_product_readiness_profile_with_malformed_handoff_approval_journal_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            handoff = root / "reports/mcp_handoff_current.json"
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            journal = payload["mcp_index_visibility_summary"]["approval_journal_coverage"]
            journal["eligible_record_count"] = "not-a-count"
            handoff.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        failures = [
            failure
            for failure in report["failures"]
            if failure["check"] == "json_artifact_mcp_handoff_approval_journal_coverage"
        ]
        self.assertTrue(failures)
        self.assertEqual(
            "approval_journal_coverage counts must be non-negative integers",
            failures[0]["reason"],
        )

    def test_fails_mcp_product_readiness_profile_with_stale_handoff_product_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_mcp_product_readiness_artifacts(root)
            product = root / "reports/mcp_product_readiness_current.json"
            payload = json.loads(product.read_text(encoding="utf-8"))
            payload["generated_at"] = "2026-07-09T11:00:00+00:00"
            product.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="mcp-product-readiness")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_mcp_handoff_source_sha256", checks)

    def test_fails_hermes_mcp_profile_when_hermes_status_needs_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_hermes_mcp_artifacts(root, hermes_status="needs_attention")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="hermes-mcp")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_hermes_status", checks)

    def test_fails_hermes_mcp_profile_when_bundle_json_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_hermes_mcp_artifacts(root)
            (root / "reports/mcp_client_bundle_hermes.json").write_text('{"quickstart": {}}\n', encoding="utf-8")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="hermes-mcp")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("json_artifact_hermes_bundle_shape", checks)

    def test_fails_hermes_mcp_profile_when_bundle_zip_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_hermes_mcp_artifacts(root)
            (root / "reports/mcp_connection_bundle_hermes.zip").write_bytes(b"not a zip")
            index = evidence_index.build_release_evidence_index(root, evidence_profile="hermes-mcp")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("zip_artifact_readable", checks)

    def test_fails_hermes_mcp_profile_when_index_has_no_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_hermes_mcp_artifacts(root, repo_commit=None)
            index = evidence_index.build_release_evidence_index(root, evidence_profile="hermes-mcp")

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertFalse(report["passed"])
        checks = {failure["check"] for failure in report["failures"]}
        self.assertIn("index_repo_commit", checks)

    def test_warns_when_index_records_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index = _write_valid_bundle(root)
            index["repo_worktree"] = {
                "state": "dirty",
                "dirty": True,
                "tracked_change_count": 2,
                "untracked_change_count": 1,
            }

            report = verify.verify_release_evidence_index(index, repo_root=root)

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual("index_repo_worktree_dirty", report["warnings"][0]["check"])
        self.assertEqual(2, report["warnings"][0]["tracked_change_count"])
        self.assertEqual(1, report["warnings"][0]["untracked_change_count"])


def _write_valid_bundle(root: Path) -> dict[str, object]:
    _write_required_reports(root)
    _write_dist_artifacts(root)
    return evidence_index.build_release_evidence_index(root)


def _write_required_reports(root: Path) -> None:
    for report in REQUIRED_REPORTS:
        path = root / report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(REQUIRED_REPORT_PAYLOADS[report]) + "\n", encoding="utf-8")


def _write_allowlist(root: Path) -> None:
    (root / ".release-hygiene-allowlist.json").write_text(
        json.dumps(
            {
                "allowed_findings": [
                    {
                        "code": "local-path-leak",
                        "path": "tests/test_fixture.py",
                        "reason": "unit test suppression fixture",
                        "approved_by": "unit-test",
                        "approved_at": "2026-07-07",
                        "approval_reference": "TEST",
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_dist_artifacts(root: Path) -> None:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "reg_rag_preprocessor-0.2.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "reg_rag_preprocessor-0.2.0.tar.gz").write_bytes(b"sdist")


def _write_hermes_mcp_artifacts(root: Path, *, hermes_status: str = "ready", repo_commit: str | None = COMMIT) -> None:
    commit_fields = {"repo_commit": repo_commit} if repo_commit else {}
    payloads: dict[str, object] = {
        "reports/hermes_mcp_check_current.json": {
            "report_type": "hermes_agent_run",
            "status": hermes_status,
            "mode": "mcp-check",
            **commit_fields,
        },
        "reports/installed_console_scripts_hermes.json": {
            "report_type": "installed_console_scripts",
            "passed": True,
        },
        "reports/mcp_smoke_hermes.json": {
            "report_type": "local_mcp_smoke",
            "passed": True,
        },
        "reports/mcp_transport_smoke_hermes.json": {
            "report_type": "mcp_transport_smoke",
            "passed": True,
        },
        "reports/mcp_client_bundle_hermes.json": _hermes_bundle_json(),
        "reports/mcp_connection_readiness_bundle_hermes.json": {
            "report_type": "mcp_connection_readiness",
            "passed": True,
        },
        "reports/mcp_connection_readiness_chatgpt_https_hermes.json": {
            "report_type": "mcp_connection_readiness",
            "passed": True,
        },
        "reports/mcp_connection_readiness_chatgpt_tunnel_hermes.json": {
            "report_type": "mcp_connection_readiness",
            "passed": True,
        },
    }
    for artifact, payload in payloads.items():
        path = root / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    (root / "reports/hermes_mcp_check_current.md").write_text("# Hermes\n", encoding="utf-8")
    _write_hermes_bundle_zip(root / "reports/mcp_connection_bundle_hermes.zip")


def _write_mcp_product_readiness_artifacts(root: Path) -> None:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    batch_path = _write_json_artifact(
        root,
        "reports/batch_quality_current.json",
        {
            "report_type": "batch_quality",
            "passed": True,
            "generated_at": "2026-07-09T09:00:00+00:00",
        },
    )
    source_artifact = {
        "role": "batch_report",
        "path": "reports/batch_quality_current.json",
        "byte_count": batch_path.stat().st_size,
        "sha256": _sha256_file(batch_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:00:00+00:00",
    }
    table_claim_path = _write_json_artifact(
        root,
        "reports/table_preprocessing_claim_gate_current.json",
        {
            "report_type": "table_preprocessing_claim_gate",
            "generated_at": "2026-07-09T09:05:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "status": "ready_for_table_quality_claim",
            "claim_level": "quality_claim_ready",
            "feasibility_status": "feasible_with_human_review",
            "blocker_count": 0,
            "warning_count": 0,
        },
    )
    (reports / "table_preprocessing_claim_gate_current.md").write_text(
        "# Table Preprocessing Claim Gate\n\nReady.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/table_review_source_traceability_current.json",
        {
            "report_type": "table_review_source_traceability",
            "generated_at": "2026-07-09T09:03:00+00:00",
            "repo_commit": COMMIT,
            "traceability_passed": True,
            "record_count": 20,
            "blocked_batch_count": 0,
            "issue_count": 0,
            "require_page_count_verification": True,
        },
    )
    (reports / "table_review_source_traceability_current.md").write_text(
        "# Table Review Source Traceability\n\nReady.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/parsing_goldset_table_drift_check_current.json",
        {
            "report_type": "parsing_goldset_table_drift_check",
            "generated_at": "2026-07-09T09:04:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "blocker_count": 0,
            "warning_count": 0,
        },
    )
    (reports / "parsing_goldset_table_drift_check_current.md").write_text(
        "# Parsing Goldset Table Drift Check\n\nReady.\n",
        encoding="utf-8",
    )
    table_claim_source_artifact = {
        "role": "table_preprocessing_claim_gate_report",
        "path": "reports/table_preprocessing_claim_gate_current.json",
        "byte_count": table_claim_path.stat().st_size,
        "sha256": _sha256_file(table_claim_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:05:00+00:00",
    }
    approval_path = _write_json_artifact(
        root,
        "reports/approval_worklist_current.json",
        {
            "report_type": "approval_worklist",
            "generated_at": "2026-07-09T09:10:00+00:00",
            "repo_commit": COMMIT,
            "document_count": 5,
            "total_chunks": 1222,
            "manual_attention_chunks": 69,
            "low_risk_batch_review_candidate_chunks": 1153,
            "blocking_review_chunks": 47,
            "domain_attention_chunks": 22,
            "safety_note": "This worklist does not approve chunks or write VectorDB records.",
        },
    )
    approval_batches_path = _write_json_artifact(
        root,
        "reports/approval_review_batches_current.json",
        {
            "report_type": "approval_review_batch_manifest",
            "generated_at": "2026-07-09T09:12:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "batch_count": 18,
            "approval_chunk_count": 1222,
            "manual_attention_chunks": 69,
            "low_risk_batch_review_candidate_chunks": 1153,
            "blocker_count": 0,
            "warning_count": 0,
            "safety_note": "This manifest does not approve chunks or write Vector DB records.",
        },
    )
    publish_runtime_path = _write_json_artifact(
        root,
        "reports/aks_mcp_publish_runtime_report.json",
        {
            "report_type": "aks_mcp_publish_runtime",
            "generated_at": "2026-07-09T09:13:00+00:00",
            "repo_commit": COMMIT,
            "tenant_id": "tenant-aks-publish",
            "passed": True,
            "source_chunk_count": 5997,
            "selected_chunk_count": 5997,
            "approved_chunk_count": 5997,
            "approval_record_count": 2,
            "indexed_record_count": 5997,
            "approval_evidence": {
                "worklist_report_path": "reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json",
                "worklist_report_sha256": "b" * 64,
                "review_batch_manifest_path": "reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json",
                "review_batch_manifest_sha256": "c" * 64,
                "approval_request_count": 2,
                "approval_chunk_count": 5997,
                "manual_attention_batch_count": 1,
                "manual_attention_chunk_count": 1993,
                "review_type_batch_counts": {
                    "low_risk_batch": 1,
                    "manual_attention": 1,
                },
                "artifacts": {
                    "worklist_json": (
                        "data\\aks_mcp_publish_runtime\\reports\\"
                        "aks_mcp_publish_tenant-aks-publish_approval_worklist.json"
                    ),
                    "review_batch_manifest_json": (
                        "data\\aks_mcp_publish_runtime\\reports\\"
                        "aks_mcp_publish_tenant-aks-publish_approval_review_batches.json"
                    ),
                },
            },
        },
    )
    publish_worklist_path = _write_json_artifact(
        root,
        "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json",
        {
            "report_type": "approval_worklist",
            "generated_at": "2026-07-09T09:11:00+00:00",
            "repo_commit": COMMIT,
            "tenant_id": "tenant-aks-publish",
            "document_count": 1,
            "total_chunks": 5997,
            "manual_attention_chunks": 1993,
            "low_risk_batch_review_candidate_chunks": 4004,
            "safety_note": "This worklist records the publish approval input.",
        },
    )
    publish_review_batch_path = _write_json_artifact(
        root,
        "data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json",
        {
            "report_type": "approval_review_batch_manifest",
            "generated_at": "2026-07-09T09:12:00+00:00",
            "repo_commit": COMMIT,
            "tenant_id": "tenant-aks-publish",
            "passed": True,
            "batch_count": 2,
            "approval_chunk_count": 5997,
            "manual_attention_chunks": 1993,
            "low_risk_batch_review_candidate_chunks": 4004,
            "blocker_count": 0,
            "warning_count": 0,
            "safety_note": "This manifest records the publish approval input.",
        },
    )
    publish_runtime_payload = json.loads(publish_runtime_path.read_text(encoding="utf-8"))
    publish_runtime_payload["approval_evidence"]["worklist_report_sha256"] = _sha256_file(publish_worklist_path)
    publish_runtime_payload["approval_evidence"]["review_batch_manifest_sha256"] = _sha256_file(
        publish_review_batch_path
    )
    publish_runtime_path.write_text(json.dumps(publish_runtime_payload) + "\n", encoding="utf-8")
    reapproval_path = _write_json_artifact(
        root,
        "reports/reapproval_worklist_current.json",
        {
            "report_type": "reapproval_worklist",
            "generated_at": "2026-07-09T09:15:00+00:00",
            "repo_commit": COMMIT,
            "document_count": 199,
            "reapproval_candidate_chunks": 5997,
            "recommended_initial_review_chunks": 419,
            "estimated_initial_review_minutes": 140,
            "source_vector_integrity_failure_count": 0,
            "pre_reapproval_blockers": [],
            "initial_review_reduction_ratio": 0.9301,
            "safety_note": "This worklist is read-only and does not approve chunks or write Vector DB records.",
        },
    )
    (reports / "reapproval_worklist_current_chunks.csv").write_text(
        "document_rank,chunk_id,reapproval_reasons\n1,chunk-a,approval_provenance_missing\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/reapproval_worklist_current_chunks.json",
        {
            "report_type": "reapproval_worklist_chunk_candidates",
            "generated_at": "2026-07-09T09:15:00+00:00",
            "repo_commit": COMMIT,
            "candidate_count": 5997,
            "fields": ["document_rank", "chunk_id", "reapproval_reasons"],
            "candidates": [{"document_rank": 1, "chunk_id": "chunk-a"}],
            "safety_note": "This file is read-only and does not approve chunks or write Vector DB records.",
        },
    )
    reapproval_batches_path = _write_json_artifact(
        root,
        "reports/reapproval_review_batches_current.json",
        {
            "report_type": "reapproval_review_batch_manifest",
            "generated_at": "2026-07-09T09:18:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "candidate_count": 5997,
            "selected_candidate_count": 5997,
            "batch_count": 60,
            "reapproval_chunk_count": 5997,
            "blocker_count": 0,
            "warning_count": 0,
            "safety_note": "This manifest is read-only and does not approve chunks or write Vector DB records.",
        },
    )
    (reports / "reapproval_review_batches_current.csv").write_text(
        "batch_rank,reapproval_batch_id,chunk_count\n1,batch-a,100\n",
        encoding="utf-8",
    )
    (reports / "reapproval_review_batches_current.md").write_text(
        "# Reapproval Review Batch Manifest\n\nSafety: not reapproved.\n",
        encoding="utf-8",
    )
    (reports / "reapproval_review_batch_decisions_current.csv").write_text(
        (
            "batch_rank,reapproval_batch_id,operator_decision,reviewer_id,reviewed_at,"
            "chunk_decision_overrides_json,approval_scope_confirmation\n"
            "1,batch-a,approve_all_reviewed,reviewer-a,2026-07-10T09:00:00+09:00,[],confirmed\n"
        ),
        encoding="utf-8",
    )
    reapproval_validation_path = _write_json_artifact(
        root,
        "reports/reapproval_decision_validation_current.json",
        {
            "report_type": "reapproval_decision_validation",
            "generated_at": "2026-07-09T09:19:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "release_gate_status": "ready_for_reapproval_apply",
            "blocking_count": 0,
            "warning_count": 0,
            "expected_batch_count": 60,
            "decision_row_count": 60,
            "complete_row_count": 60,
            "blank_or_incomplete_row_count": 0,
        },
    )
    (reports / "reapproval_decision_validation_current.md").write_text(
        "# Reapproval Decision Validation\n\nReady for apply preflight.\n",
        encoding="utf-8",
    )
    reapproval_apply_plan_path = _write_json_artifact(
        root,
        "reports/reapproval_apply_plan_current.json",
        {
            "report_type": "reapproval_apply_plan",
            "generated_at": "2026-07-09T09:20:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "release_gate_status": "ready_for_apply_execution",
            "blocker_count": 0,
            "summary": {
                "batch_count": 60,
                "approve_chunk_count": 5997,
                "reject_chunk_count": 0,
                "reprocess_chunk_count": 0,
                "defer_chunk_count": 0,
            },
            "operator_controls": {
                "auto_approval": False,
                "auto_reindex": False,
                "applies_reapproval_decisions": False,
                "requires_dedicated_apply_step": True,
                "direct_approval_metadata_write_allowed": False,
                "requires_tenant_and_operator_access_control": True,
                "requires_shared_review_workflow_contract": True,
                "requires_approval_precondition_validation": True,
                "requires_rejection_decision_validation": True,
                "requires_preapproval_security_scan": True,
                "requires_review_flag_acknowledgement": True,
                "requires_approved_content_hash_recalculation": True,
                "requires_apply_audit_event": True,
                "requires_vector_sync_or_explicit_reindex": True,
                "requires_explicit_reindex_phase_by_default": True,
                "conditional_vector_sync_requires_existing_successful_index": True,
                "official_mcp_publish_allowed_by_this_plan": False,
            },
            "execution_requirements": [
                {"step": step, "required": True}
                for step in (
                    "load_current_review_chunks",
                    "enforce_tenant_and_operator_access",
                    "use_shared_review_workflow_contract",
                    "validate_approval_preconditions",
                    "validate_rejection_decision_contract",
                    "run_preapproval_security_scan",
                    "acknowledge_review_attention_flags",
                    "recalculate_approval_hashes",
                    "append_review_journals_and_snapshots",
                    "record_apply_audit_event",
                    "refresh_exports_and_vector_state",
                    "keep_reindex_as_explicit_phase",
                    "rerun_mcp_visibility_gate",
                )
            ],
            "batch_plans": [
                {
                    "reapproval_batch_id": "batch-a",
                    "approve_chunk_ids": ["chunk-a"],
                    "reject_chunk_ids": [],
                    "requires_reindex": True,
                    "apply_controls": {
                        "direct_metadata_write_allowed": False,
                        "requires_tenant_and_operator_access_control": True,
                        "requires_shared_review_workflow_contract": True,
                        "approval_requires_precondition_validation": True,
                        "approval_requires_preapproval_security_scan": True,
                        "approval_requires_review_flag_acknowledgement_if_attention_present": True,
                        "approval_recalculates_approved_content_hash": True,
                        "rejection_requires_reason_validation": False,
                        "requires_apply_audit_event": True,
                        "requires_vector_sync_or_explicit_reindex": True,
                        "requires_explicit_reindex_phase": True,
                        "conditional_vector_sync_allowed_only_after_successful_index": True,
                        "official_mcp_publish_allowed_by_batch_plan": False,
                    },
                }
            ],
            "safety_note": "This plan is read-only.",
        },
    )
    (reports / "reapproval_apply_plan_current.md").write_text(
        "# Reapproval Apply Plan\n\nRead-only apply plan.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/reapproval_review_burden_current.json",
        {
            "report_type": "reapproval_review_burden",
            "generated_at": "2026-07-09T09:20:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "status": "ready_for_operator_review",
            "blocking_count": 0,
            "warning_count": 0,
            "release_gate_status": "ready_for_release_gate",
            "release_blocker_count": 0,
            "reapproval_candidate_chunks": 5997,
            "baseline_full_review_minutes": 1999,
            "recommended_initial_review_chunks": 419,
            "estimated_initial_review_minutes": 140,
            "initial_review_reduction_ratio": 0.9301,
            "decision_template_row_count": 1,
            "decision_template_operator_decision_complete_count": 1,
            "decision_template_operator_decision_blank_count": 0,
        },
    )
    (reports / "reapproval_review_burden_current.md").write_text(
        "# Reapproval Review Burden\n\nSafety: not reapproved.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/mcp_readiness_remediation_plan_current.json",
        {
            "report_type": "mcp_readiness_remediation_plan",
            "generated_at": "2026-07-09T09:22:00+00:00",
            "repo_commit": COMMIT,
            "passed": True,
            "plan_status": "ready_for_human_remediation",
            "plan_blockers": [],
            "release_gate_status": "blocked",
            "release_gate_blocker_count": 3,
            "remediation_item_count": 3,
            "source_report_artifacts": [],
        },
    )
    (reports / "mcp_readiness_remediation_plan_current.md").write_text(
        "# MCP Readiness Remediation Plan\n\nPublic release gate remains blocked.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/github_publish_readiness_current.json",
        {
            "report_type": "github_publish_readiness_summary",
            "generated_at": "2026-07-09T09:24:00+00:00",
            "repo_commit": COMMIT,
            "overall_status": "internal_pilot_ready_public_github_blocked",
            "owner_decision_count": 8,
            "machine_cleanup_action_count": 2,
            "source_report_artifacts": [],
        },
    )
    (reports / "github_publish_readiness_current.md").write_text(
        "# GitHub Publish Readiness\n\nInternal pilot ready; public GitHub publish remains blocked.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/github_publish_execution_plan_current.json",
        {
            "report_type": "github_publish_execution_plan",
            "generated_at": "2026-07-09T09:25:00+00:00",
            "repo_commit": COMMIT,
            "overall_status": "public_github_blocked",
            "source_report_artifacts": [],
        },
    )
    (reports / "github_publish_execution_plan_current.md").write_text(
        "# GitHub Publish Execution Plan\n\nSource-only and product public release paths are separated.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/strict_public_readiness_gap_summary_current.json",
        {
            "report_type": "strict_public_readiness_gap_summary",
            "generated_at": "2026-07-09T09:26:00+00:00",
            "repo_commit": COMMIT,
            "passed": False,
            "status": "strict_public_readiness_blocked",
            "source_report_artifacts": [],
        },
    )
    (reports / "strict_public_readiness_gap_summary_current.md").write_text(
        "# Strict Public Readiness Gaps\n\nStrict parser evidence remains blocked.\n",
        encoding="utf-8",
    )
    (reports / "strict_public_readiness_gap_worklist_current.csv").write_text(
        "issue_id,issue_type,severity,operator_action,filename,file_format,document_id,"
        "institution_name,profile_id,source_record_id,source_file_id,apba_id,"
        "failed_info_check_count,recommendation_count,top_recommendation,missing_fields\n"
        "strict-0001,failed_info_check,high,review_parser_evidence,rule.pdf,pdf,doc-1,"
        "기관,profile-1,record-1,file-1,apba-1,1,0,,\n",
        encoding="utf-8",
    )
    (reports / "strict_public_readiness_gap_worklist_current.md").write_text(
        "# Strict Public Readiness Gap Worklist\n\nOperator-facing strict parser review rows.\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/temporal_ambiguity_review_scope_current.json",
        {
            "report_type": "temporal_ambiguity_review_scope",
            "generated_at": "2026-07-09T09:27:00+00:00",
            "repo_commit": COMMIT,
            "status": "temporal_ambiguity_policy_required",
            "passed": False,
            "summary": {
                "chunk_count": 5997,
                "before_temporal_metadata_count": 1174,
                "after_temporal_metadata_count": 1584,
                "delta_temporal_metadata_count": 410,
                "conflict_chunk_count": 0,
                "ambiguous_chunk_count": 4451,
                "ambiguous_chunk_ratio": 0.7422,
                "shadow_runtime_written": True,
                "write_blocked": False,
            },
            "record_analysis": {
                "vector_record_count": 5997,
                "ambiguous_record_count": 4451,
                "review_slice_count": 8,
                "ambiguous_by_chunk_type": {"article": 2406, "form": 867},
                "ambiguous_by_field_from_records": {"effective_date": 4227, "revision_date": 2203},
            },
            "source_report_artifacts": [],
        },
    )
    (reports / "temporal_ambiguity_review_scope_current.md").write_text(
        "# Temporal Ambiguity Review Scope\n\nTemporal ambiguity requires an owner policy decision.\n",
        encoding="utf-8",
    )
    (reports / "approval_worklist_current.md").write_text("# Approval Worklist\n\nSafety: not approved.\n", encoding="utf-8")
    (reports / "approval_review_batches_current.md").write_text(
        "# Approval Review Batch Manifest\n\nSafety: not approved.\n",
        encoding="utf-8",
    )
    (reports / "reapproval_worklist_current.md").write_text("# Reapproval Worklist\n\nSafety: not reapproved.\n", encoding="utf-8")
    approval_source_artifact = {
        "role": "approval_worklist_report",
        "path": "reports/approval_worklist_current.json",
        "byte_count": approval_path.stat().st_size,
        "sha256": _sha256_file(approval_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:10:00+00:00",
    }
    approval_batch_source_artifact = {
        "role": "approval_review_batch_manifest_report",
        "path": "reports/approval_review_batches_current.json",
        "byte_count": approval_batches_path.stat().st_size,
        "sha256": _sha256_file(approval_batches_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:12:00+00:00",
    }
    reapproval_source_artifact = {
        "role": "reapproval_worklist_report",
        "path": "reports/reapproval_worklist_current.json",
        "byte_count": reapproval_path.stat().st_size,
        "sha256": _sha256_file(reapproval_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:15:00+00:00",
    }
    reapproval_batch_source_artifact = {
        "role": "reapproval_review_batch_manifest_report",
        "path": "reports/reapproval_review_batches_current.json",
        "byte_count": reapproval_batches_path.stat().st_size,
        "sha256": _sha256_file(reapproval_batches_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:18:00+00:00",
    }
    reapproval_validation_source_artifact = {
        "role": "reapproval_decision_validation_report",
        "path": "reports/reapproval_decision_validation_current.json",
        "byte_count": reapproval_validation_path.stat().st_size,
        "sha256": _sha256_file(reapproval_validation_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:19:00+00:00",
    }
    reapproval_apply_plan_source_artifact = {
        "role": "reapproval_apply_plan_report",
        "path": "reports/reapproval_apply_plan_current.json",
        "byte_count": reapproval_apply_plan_path.stat().st_size,
        "sha256": _sha256_file(reapproval_apply_plan_path),
        "modified_at": "2026-07-09T10:00:00+00:00",
        "payload_generated_at": "2026-07-09T09:20:00+00:00",
    }
    product_contract = {
        "source_report_artifact_count": 8,
        "source_report_artifact_complete_count": 8,
        "missing_source_report_artifact_count": 0,
    }
    product_path = _write_json_artifact(
        root,
        "reports/mcp_product_readiness_current.json",
        {
            "report_type": "mcp_product_readiness",
            "repo_commit": COMMIT,
            "passed": True,
            "blocking_count": 0,
            "warning_count": 1,
            "source_report_artifacts": [
                source_artifact,
                table_claim_source_artifact,
                approval_source_artifact,
                approval_batch_source_artifact,
                reapproval_source_artifact,
                reapproval_batch_source_artifact,
                reapproval_validation_source_artifact,
                reapproval_apply_plan_source_artifact,
            ],
            "source_report_artifact_summary": {
                "provided_count": 8,
                "sha256_count": 8,
                "payload_generated_at_count": 8,
            },
            "table_preprocessing_claim_gate_summary": {
                "passed": True,
                "status": "ready_for_table_quality_claim",
                "pending_unit_count": 0,
                "invalid_unit_count": 0,
                "transfer_blocker_count": 0,
                "source_traceability_issue_count": 0,
                "source_traceability_require_page_count_verification": True,
                "drift_check_present": True,
                "drift_check_passed": True,
                "drift_check_blocker_count": 0,
                "table_answer_blocker_count": 0,
            },
            "approval_workload_summary": {
                "manual_attention_chunks": 69,
                "low_risk_batch_review_candidate_chunks": 1153,
                "blocking_review_chunks": 47,
                "domain_attention_chunks": 22,
            },
            "approval_review_batch_summary": {
                "batch_count": 18,
                "approval_chunk_count": 1222,
                "blocker_count": 0,
                "warning_count": 0,
            },
            "reapproval_workload_summary": {
                "reapproval_candidate_chunks": 5997,
                "recommended_initial_review_chunks": 419,
                "approval_provenance_missing_chunks": 197,
                "approval_provenance_only_chunks": 50,
                "approval_provenance_missing_field_counts": {
                    "approval_worklist_report_path": 197,
                    "approval_review_batch_manifest_path": 197,
                },
                "pre_reapproval_blocker_count": 0,
                "initial_review_reduction_ratio": 0.9301,
                "source_vector_integrity_failure_count": 0,
            },
            "reapproval_review_batch_summary": {
                "candidate_count": 5997,
                "selected_candidate_count": 5997,
                "batch_count": 60,
                "reapproval_chunk_count": 5997,
                "blocker_count": 0,
                "warning_count": 0,
            },
            "reapproval_decision_validation_summary": {
                "expected_batch_count": 60,
                "decision_row_count": 60,
                "complete_row_count": 60,
                "blank_or_incomplete_row_count": 0,
                "blocking_count": 0,
                "warning_count": 0,
                "passed": True,
                "release_gate_status_counts": {"ready_for_reapproval_apply": 1},
                "operator_decision_counts": {"approve_all_reviewed": 60},
            },
            "reapproval_apply_plan_summary": {
                "report_count": 1,
                "passed": True,
                "blocker_count": 0,
                "ready_plan_count": 1,
                "batch_count": 60,
                "approve_chunk_count": 5997,
                "reject_chunk_count": 0,
                "batch_apply_control_count": 60,
                "batch_requires_explicit_reindex_phase_count": 60,
                "batch_conditional_vector_sync_guard_count": 60,
                "unsafe_contract_violation_count": 0,
                "required_execution_steps": [
                    "acknowledge_review_attention_flags",
                    "append_review_journals_and_snapshots",
                    "enforce_tenant_and_operator_access",
                    "keep_reindex_as_explicit_phase",
                    "record_apply_audit_event",
                    "recalculate_approval_hashes",
                    "refresh_exports_and_vector_state",
                    "rerun_mcp_visibility_gate",
                    "run_preapproval_security_scan",
                    "use_shared_review_workflow_contract",
                    "validate_approval_preconditions",
                    "validate_rejection_decision_contract",
                ],
                "observed_execution_step_counts": {
                    "acknowledge_review_attention_flags": 1,
                    "append_review_journals_and_snapshots": 1,
                    "enforce_tenant_and_operator_access": 1,
                    "keep_reindex_as_explicit_phase": 1,
                    "record_apply_audit_event": 1,
                    "recalculate_approval_hashes": 1,
                    "refresh_exports_and_vector_state": 1,
                    "rerun_mcp_visibility_gate": 1,
                    "run_preapproval_security_scan": 1,
                    "use_shared_review_workflow_contract": 1,
                    "validate_approval_preconditions": 1,
                    "validate_rejection_decision_contract": 1,
                },
            },
        },
    )
    product_sha = _sha256_file(product_path)
    authority_path = _write_json_artifact(
        root,
        "reports/mcp_readiness_authority_current.json",
        {
            "report_type": "mcp_readiness_authority",
            "authority_version": 1,
            "repo_commit": COMMIT,
            "passed": True,
            "blocking_count": 0,
            "warning_count": 0,
            "finding_count": 0,
            "authoritative_artifacts": [
                {
                    "role": "product_readiness",
                    "path": "reports/mcp_product_readiness_current.json",
                    "exists": True,
                    "byte_count": product_path.stat().st_size,
                    "sha256": product_sha,
                    "product_readiness_contract": product_contract,
                },
                {"role": "mcp_demo_answers", "path": "reports/mcp_demo_answers_current.json"},
                {"role": "mcp_transport_smoke", "path": "reports/mcp_transport_smoke_current.json"},
                {"role": "mcp_index_visibility", "path": "reports/mcp_index_visibility_current.json"},
                {"role": "mcp_connection_readiness", "path": "reports/mcp_connection_readiness_current.json"},
            ],
            "supersedes": [
                {
                    "path": "reports/mcp_product_readiness_old.json",
                    "sha256": "d" * 64,
                    "reason": "replaced by current authority",
                }
            ],
        },
    )
    payloads: dict[str, object] = {
        "reports/mcp_demo_answers_current.json": {
            "report_type": "mcp_demo_answers",
            "repo_commit": COMMIT,
            "passed": True,
            "query_count": 2,
            "quality_issue_count": 0,
        },
        "reports/simple_rag_vs_mcp_accuracy_current.json": {
            "report_type": "simple_rag_vs_mcp_accuracy",
            "repo_commit": COMMIT,
            "passed": True,
            "query_count": 2,
        },
        "reports/mcp_transport_smoke_current.json": {
            "report_type": "mcp_transport_smoke",
            "repo_commit": COMMIT,
            "passed": True,
            "transport": "stdio",
        },
        "reports/mcp_index_visibility_current.json": {
            "report_type": "mcp_index_visibility_audit",
            "repo_commit": COMMIT,
            "passed": True,
            "finding_count": 0,
            "approval_journal_coverage": {
                "journal_record_count": 5997,
                "record_count": 5997,
                "eligible_record_count": 5997,
                "matched_record_count": 5997,
                "missing_record_count": 0,
            },
        },
        "reports/mcp_connection_readiness_current.json": {
            "report_type": "mcp_connection_readiness",
            "repo_commit": COMMIT,
            "passed": True,
            "finding_count": 0,
            "mcp_index_visibility_summary": {
                "passed": True,
                "document_count": 1,
                "total_approved_chunks": 5997,
                "total_mcp_visible_records": 5997,
                "approval_journal_coverage": {
                    "journal_record_count": 5997,
                    "record_count": 5997,
                    "eligible_record_count": 5997,
                    "matched_record_count": 5997,
                    "missing_record_count": 0,
                },
            },
        },
    }
    for artifact, payload in payloads.items():
        _write_json_artifact(root, artifact, payload)
    authority_payload = json.loads(authority_path.read_text(encoding="utf-8"))
    authority_artifact_paths = {
        "mcp_demo_answers": "reports/mcp_demo_answers_current.json",
        "mcp_transport_smoke": "reports/mcp_transport_smoke_current.json",
        "mcp_index_visibility": "reports/mcp_index_visibility_current.json",
        "mcp_connection_readiness": "reports/mcp_connection_readiness_current.json",
    }
    for item in authority_payload["authoritative_artifacts"]:
        artifact = authority_artifact_paths.get(item.get("role"))
        if not artifact:
            continue
        path = root / artifact
        item.update(
            {
                "path": artifact,
                "exists": True,
                "byte_count": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    authority_path.write_text(json.dumps(authority_payload) + "\n", encoding="utf-8")
    _write_json_artifact(
        root,
        "reports/mcp_handoff_current.json",
        {
            "report_type": "mcp_handoff_report",
            "handoff_schema_version": 2,
            "repo_commit": COMMIT,
            "passed": True,
            "handoff_ready": True,
            "blocking_count": 0,
            "warning_count": 0,
            "source_reports": {
                "product_readiness_report": "reports/mcp_product_readiness_current.json",
                "authority_manifest": "reports/mcp_readiness_authority_current.json",
            },
            "source_report_artifacts": [
                _source_artifact_for_test(
                    root,
                    "reports/mcp_product_readiness_current.json",
                    role="product_readiness_report",
                    report_type="mcp_product_readiness",
                ),
                _source_artifact_for_test(
                    root,
                    "reports/mcp_readiness_authority_current.json",
                    role="authority_manifest",
                    report_type="mcp_readiness_authority",
                ),
            ],
            "product_summary": {
                "approval_journal_review_event_coverage": _approval_review_event_coverage_for_test(),
            },
            "mcp_index_visibility_summary": {
                "passed": True,
                "document_count": 1,
                "total_approved_chunks": 5997,
                "total_mcp_visible_records": 5997,
                "approval_journal_coverage": {
                    "journal_record_count": 5997,
                    "record_count": 5997,
                    "eligible_record_count": 5997,
                    "matched_record_count": 5997,
                    "missing_record_count": 0,
                },
            },
            "authority_summary": {
                "report_type": "mcp_readiness_authority",
                "passed": True,
                "blocking_count": 0,
                "warning_count": 0,
                "finding_count": 0,
                "authoritative_artifact_count": 1,
                "supersedes_count": 1,
                "repo_commit": COMMIT,
            },
        },
    )
    _write_json_artifact(
        root,
        "reports/mcp_query_benchmark_current.json",
        {
            "report_type": "mcp_query_benchmark",
            "repo_commit": COMMIT,
            "passed": True,
            "query_count": 5,
            "min_warm_records": 5000,
            "min_record_count": 5997,
            "finding_count": 0,
        },
    )
    (reports / "mcp_query_benchmark_current.md").write_text("# MCP Query Benchmark\n", encoding="utf-8")
    _write_json_artifact(
        root,
        "reports/mcp_answer_evidence_bundle_current.json",
        {
            "report_type": "mcp_answer_evidence_bundle",
            "repo_commit": COMMIT,
            "passed": True,
            "query_count": 5,
            "finding_count": 0,
        },
    )
    (reports / "mcp_answer_evidence_bundle_current.md").write_text(
        "# MCP Answer Evidence Bundle\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/mcp_performance_load_evidence_current.json",
        {
            "report_type": "mcp_performance_load_evidence",
            "repo_commit": COMMIT,
            "passed": True,
            "evidence_ready": True,
            "min_warm_records": 5000,
            "min_record_count": 5997,
            "finding_count": 0,
        },
    )
    (reports / "mcp_performance_load_evidence_current.md").write_text(
        "# MCP Performance Load Evidence\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/mcp_cold_start_benchmark_current.json",
        {
            "report_type": "mcp_cold_start_benchmark",
            "repo_commit": COMMIT,
            "passed": True,
            "iterations": 3,
            "min_record_count": 5997,
            "max_process_elapsed_ms": 1000,
            "finding_count": 0,
        },
    )
    (reports / "mcp_cold_start_benchmark_current.md").write_text(
        "# MCP Cold Start Benchmark\n",
        encoding="utf-8",
    )
    _write_json_artifact(
        root,
        "reports/mcp_concurrent_benchmark_current.json",
        {
            "report_type": "mcp_concurrent_query_benchmark",
            "repo_commit": COMMIT,
            "passed": True,
            "rounds": 2,
            "concurrency": 3,
            "task_count": 10,
            "max_task_total_ms": 400,
            "max_batch_elapsed_ms": 1000,
            "finding_count": 0,
        },
    )
    (reports / "mcp_concurrent_benchmark_current.md").write_text(
        "# MCP Concurrent Benchmark\n",
        encoding="utf-8",
    )
    (reports / "mcp_readiness_authority_current.md").write_text("# Authority\n", encoding="utf-8")
    (reports / "mcp_product_readiness_current.md").write_text("# Product\n", encoding="utf-8")


def _approval_review_event_coverage_for_test() -> dict[str, object]:
    return {
        "journal_record_count": 5997,
        "applicable_record_count": 5997,
        "chunk_reference_count": 5997,
        "review_decision_event_count": 17991,
        "expected_event_chunk_counts": {
            "ai_review_confirmed": 5997,
            "approved": 5997,
            "human_review_confirmed": 5997,
        },
        "event_chunk_counts": {
            "ai_review_confirmed": 5997,
            "approved": 5997,
            "human_review_confirmed": 5997,
        },
        "missing_event_chunk_counts": {
            "ai_review_confirmed": 0,
            "approved": 0,
            "human_review_confirmed": 0,
        },
        "incomplete_record_count": 0,
    }


def _write_json_artifact(root: Path, artifact: str, payload: object) -> Path:
    path = root / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _source_artifact_for_test(root: Path, artifact: str, *, role: str, report_type: str) -> dict[str, object]:
    path = root / artifact
    return {
        "role": role,
        "path": artifact,
        "exists": True,
        "byte_count": path.stat().st_size,
        "sha256": _sha256_file(path),
        "report_type": report_type,
        "passed": True,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hermes_bundle_json() -> dict[str, object]:
    return {
        "quickstart": {
            "validate_synthetic_chain": {"command": "reg-rag-mcp-smoke"},
            "run_local_stdio_server": {"command": "reg-rag-mcp-server"},
            "run_http_server": {"command": "reg-rag-mcp-server"},
            "run_chatgpt_data_server": {"command": "reg-rag-mcp-server"},
            "openai_secure_tunnel": {"command": "tunnel-client"},
        },
        "claude_desktop": {"ready": True},
        "claude_code": {"ready": True},
        "chatgpt": {"ready": True},
        "claude_api": {"ready": True},
    }


def _write_hermes_bundle_zip(path: Path) -> None:
    entries = [
        "README.md",
        "README.ko.md",
        "manifest.json",
        "mcp_config.bundle.json",
        "connect_mcp_client.ps1",
        "CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md",
        "CODEX_AGENT_CONNECT_PROMPT.md",
        "CLAUDE_CODE_AGENT_CONNECT_PROMPT.md",
        "MCP 사용 시작하기.txt",
        "설치 후 MCP 사용 방법 보기.bat",
        "Codex 플러그인 MCP 입력값.txt",
        "ChatGPT Desktop에 연결하기.bat",
        "Codex에 연결하기.bat",
        "Claude Desktop에 연결하기.bat",
        "Claude Code에 연결하기.bat",
        "ChatGPT HTTPS에 연결하기.bat",
        "ChatGPT 보안 Tunnel에 연결하기.bat",
        "Claude HTTPS에 연결하기.bat",
        "install_local_package.ps1",
        "doctor_mcp_connection.ps1",
        "연결 상태 확인하기.bat",
        "validate_mcp_smoke.ps1",
        "run_local_stdio_server.ps1",
        "run_http_server.ps1",
        "run_chatgpt_data_server.ps1",
        "run_openai_secure_tunnel.ps1",
        "claude_desktop_config.json",
        "chatgpt_desktop_local_mcp.json",
        "chatgpt_connector.json",
        "claude_api_fragment.json",
        "wheelhouse/reg_rag_preprocessor-0.1.0-py3-none-any.whl",
    ]
    with zipfile.ZipFile(path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, "ok")


if __name__ == "__main__":
    unittest.main()
