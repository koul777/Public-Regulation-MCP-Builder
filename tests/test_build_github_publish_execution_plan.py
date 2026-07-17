from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_github_publish_execution_plan import build_github_publish_execution_plan, main


class GithubPublishExecutionPlanTests(unittest.TestCase):
    def test_builds_separate_source_and_product_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            owner_gate = root / "owner_gate.json"
            public_gate = root / "public_gate.json"
            _write_json(readiness, _readiness_payload())
            _write_json(owner_gate, _owner_gate_payload())
            _write_json(public_gate, _public_gate_payload())

            report = build_github_publish_execution_plan(
                readiness_summary_report=readiness,
                owner_decision_gate_report=owner_gate,
                public_release_gate_report=public_gate,
            )

        self.assertEqual("github_publish_execution_plan", report["report_type"])
        self.assertEqual("public_github_blocked", report["overall_status"])
        source_mode = report["publish_modes"]["source_only_public_github"]
        product_mode = report["publish_modes"]["product_public_release"]
        self.assertEqual("blocked", source_mode["status"])
        self.assertEqual("blocked", product_mode["status"])
        source_codes = {blocker["code"] for blocker in source_mode["blockers"]}
        product_codes = {blocker["code"] for blocker in product_mode["blockers"]}
        self.assertIn("public_release_gate_blocked", source_codes)
        self.assertIn("source_only_owner_decisions_incomplete", source_codes)
        self.assertIn("strict_parser_release_evidence_missing", product_codes)
        self.assertIn("product_owner_decisions_incomplete", product_codes)
        self.assertEqual(7, report["owner_decision_gate"]["incomplete_decision_count"])
        guidance = {item["decision_id"]: item for item in report["decision_guidance"]}
        self.assertIn("LICENSE file", guidance["license_selection"]["required_evidence"])
        self.assertIn(
            "ambiguous_chunk_count=4451",
            guidance["product_remediation_temporal_metadata_review"]["required_evidence"],
        )
        self.assertIn(
            "approval_journal_coverage.missing_record_count=0",
            guidance["product_remediation_runtime_reapproval_and_reindex"]["required_evidence"],
        )
        self.assertEqual(4, len(report["execution_phases"]))
        self.assertTrue(any("public-release" in action.get("command", "") for action in report["execution_phases"][1]["actions"]))
        product_evidence_phase = report["execution_phases"][2]
        reapproval_action = {
            action["action_id"]: action for action in product_evidence_phase["actions"]
        }["runtime_reapproval_reindex"]
        self.assertIn(
            "approval_journal_coverage.missing_record_count=0",
            reapproval_action["evidence"]["required_post_reindex_evidence"],
        )
        validation_commands = [item["command"] for item in report["validation_commands"]]
        self.assertTrue(any("reg-rag-mcp-doctor --audit-index-visibility" in command for command in validation_commands))
        self.assertTrue(any("reg-rag-mcp-authority" in command for command in validation_commands))
        self.assertTrue(any("mcp_connection_readiness=reports/mcp_connection_readiness_current.json" in command for command in validation_commands))
        self.assertTrue(any("reg-rag-mcp-handoff-report" in command for command in validation_commands))
        self.assertTrue(any("--authority-manifest reports/mcp_readiness_authority_current.json" in command for command in validation_commands))
        self.assertTrue(any("reg-rag-release-evidence-index --profile mcp-product-readiness" in command for command in validation_commands))
        self.assertTrue(any("reg-rag-verify-release-evidence" in command for command in validation_commands))

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            owner_gate = root / "owner_gate.json"
            out_json = root / "plan.json"
            out_md = root / "plan.md"
            _write_json(readiness, _readiness_payload())
            _write_json(owner_gate, _owner_gate_payload())
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--readiness-summary-report",
                    str(readiness),
                    "--owner-decision-gate-report",
                    str(owner_gate),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("github_publish_execution_plan", payload["report_type"])
        self.assertIn("GitHub Publish Execution Plan", markdown)
        self.assertIn("Source-only public GitHub", markdown)
        self.assertIn("Mode Blockers", markdown)
        self.assertIn("product_release_evidence_verification_missing", markdown)
        self.assertIn("Decision Guidance", markdown)
        self.assertIn("reg-rag-public-release-gate", markdown)
        self.assertIn("approval_journal_coverage.missing_record_count=0", markdown)
        self.assertIn("reg-rag-verify-release-evidence", markdown)
        self.assertIn('"publish_modes"', stdout.getvalue())

    def test_product_readiness_blocking_codes_block_product_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            payload = _readiness_payload()
            payload["product_readiness_status"] = {
                "passed": False,
                "blocking_count": 1,
                "blocking_codes": ["temporal-ambiguity-policy-required"],
                "warning_count": 0,
                "warning_codes": [],
                "temporal_backfill_summary": {
                    "conflict_chunk_count": 0,
                    "ambiguous_chunk_count": 8,
                },
            }
            payload["strict_parser_gap_summary"] = {"passed": True, "status": "passed"}
            payload["owner_decisions_required"] = [
                decision
                for decision in payload["owner_decisions_required"]
                if not str(decision.get("decision_id", "")).startswith("product_remediation_")
            ]
            _write_json(readiness, payload)

            report = build_github_publish_execution_plan(readiness_summary_report=readiness)

        product_mode = report["publish_modes"]["product_public_release"]
        self.assertEqual("blocked", product_mode["status"])
        blockers = {blocker["code"]: blocker for blocker in product_mode["blockers"]}
        self.assertIn("product_readiness_blockers_open", blockers)
        self.assertEqual(
            ["temporal-ambiguity-policy-required"],
            blockers["product_readiness_blockers_open"]["blocking_codes"],
        )
        self.assertNotIn("product_readiness_warnings_open", blockers)

    def test_product_release_requires_current_evidence_verification_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            owner_gate = root / "owner_gate.json"
            public_gate = root / "public_gate.json"
            verification = root / "verification.json"
            payload = _release_ready_readiness_payload()
            _write_json(readiness, payload)
            _write_json(owner_gate, _passing_owner_gate_payload())
            _write_json(public_gate, _passing_public_gate_payload())

            missing_report = build_github_publish_execution_plan(
                readiness_summary_report=readiness,
                owner_decision_gate_report=owner_gate,
                public_release_gate_report=public_gate,
            )
            _write_json(verification, _verification_payload())
            passing_report = build_github_publish_execution_plan(
                readiness_summary_report=readiness,
                owner_decision_gate_report=owner_gate,
                public_release_gate_report=public_gate,
                evidence_verification_report=verification,
            )

        missing_product_mode = missing_report["publish_modes"]["product_public_release"]
        self.assertEqual("blocked", missing_product_mode["status"])
        self.assertIn(
            "product_release_evidence_verification_missing",
            {blocker["code"] for blocker in missing_product_mode["blockers"]},
        )
        self.assertEqual(
            "ready_for_public_github_and_product_release_validation",
            passing_report["overall_status"],
        )
        self.assertEqual(
            "ready_for_product_release_validation",
            passing_report["publish_modes"]["product_public_release"]["status"],
        )
        self.assertEqual(
            str(verification),
            passing_report["source_reports"]["evidence_verification_report"],
        )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _readiness_payload() -> dict[str, object]:
    return {
        "report_type": "github_publish_readiness_summary",
        "overall_status": "internal_pilot_ready_public_github_blocked",
        "public_release_gate_status": {
            "passed": False,
            "status": "blocked_by_public_audit",
            "finding_count": 19,
            "action_count": 14,
        },
        "product_readiness_status": {
            "passed": True,
            "warning_count": 4,
            "warning_codes": [
                "public-readiness-review-tolerance-evidence",
                "temporal-metadata-coverage-partial",
                "runtime-version-drift-evidence",
                "reapproval-worklist-review-evidence",
            ],
            "temporal_backfill_summary": {
                "conflict_chunk_count": 0,
                "ambiguous_chunk_count": 4451,
            },
            "reapproval_summary": {
                "batch_count": 61,
                "selected_candidate_count": 5997,
            },
        },
        "strict_parser_candidate_status": {
            "failed_checks": [
                "failed_info_checks_within_limit",
                "recommendations_within_limit",
                "required_row_fields_present",
            ]
        },
        "strict_parser_gap_summary": {
            "passed": False,
            "status": "strict_public_readiness_blocked",
            "gap_counts": {
                "failed_info_check_total": 7,
                "recommendation_total": 86,
                "missing_required_field_total": 20,
            },
            "missing_required_field_counts": {"apba_id": 20},
        },
        "cleanup_breakdown": {
            "owner_decision_action_count": 12,
            "safe_machine_action_count": 2,
        },
        "machine_cleanup_actions": [
            {
                "action": "remove_generated_report",
                "path": "reports/a.json",
                "reason": "Generated reports should not be committed.",
                "command": "git rm -- reports/a.json",
                "destructive": True,
            }
        ],
        "owner_decisions_required": [
            {
                "decision_id": "license_selection",
                "summary": "Choose the repository license before public GitHub publication.",
                "inputs": ["LICENSE"],
            },
            {
                "decision_id": "sample_redistribution_policy",
                "summary": "Decide sample redistribution policy.",
                "inputs": ["data/public_portal_samples/public_portal_3050658.hwp"],
            },
            {
                "decision_id": "nonpublic_doc_policy",
                "summary": "Decide nonpublic doc policy.",
                "inputs": ["docs/private_release_runbook.md"],
            },
            {
                "decision_id": "identifier_fixture_policy",
                "summary": "Decide identifier fixture policy.",
                "inputs": ["tests/fixtures/regression/public_portal_quality_expectations_20260703.json"],
            },
            {
                "decision_id": "product_remediation_parser_release_evidence",
                "summary": "Replace parser release evidence.",
                "inputs": ["strict report"],
            },
            {
                "decision_id": "product_remediation_temporal_metadata_review",
                "summary": "Review temporal metadata gaps.",
                "inputs": ["temporal report"],
            },
            {
                "decision_id": "product_remediation_runtime_reapproval_and_reindex",
                "summary": "Complete reapproval and reindex.",
                "inputs": ["reapproval decisions"],
            },
        ],
        "time_estimates": {
            "source_only_public_branch": "0.5-1 day after decisions",
            "product_public_release": "1-2 weeks",
        },
    }


def _owner_gate_payload() -> dict[str, object]:
    return {
        "report_type": "github_publish_owner_decision_gate",
        "passed": False,
        "status": "blocked_pending_owner_decisions",
        "decision_count": 7,
        "complete_decision_count": 0,
        "incomplete_decision_ids": [
            "license_selection",
            "sample_redistribution_policy",
            "nonpublic_doc_policy",
            "identifier_fixture_policy",
            "product_remediation_parser_release_evidence",
            "product_remediation_temporal_metadata_review",
            "product_remediation_runtime_reapproval_and_reindex",
        ],
    }


def _public_gate_payload() -> dict[str, object]:
    return {
        "report_type": "public_release_gate",
        "passed": False,
        "status": "blocked_by_public_audit",
        "finding_count": 19,
        "action_count": 14,
    }


def _release_ready_readiness_payload() -> dict[str, object]:
    payload = _readiness_payload()
    payload["repo_commit"] = "a" * 40
    payload["public_release_gate_status"] = {
        "passed": True,
        "status": "passed",
        "finding_count": 0,
        "action_count": 0,
    }
    payload["product_readiness_status"] = {
        "passed": True,
        "warning_count": 0,
        "warning_codes": [],
        "blocking_count": 0,
        "blocking_codes": [],
        "temporal_backfill_summary": {
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": 0,
        },
        "reapproval_summary": {
            "batch_count": 0,
            "selected_candidate_count": 0,
        },
    }
    payload["strict_parser_gap_summary"] = {"passed": True, "status": "passed", "gap_counts": {}}
    payload["cleanup_breakdown"] = {"owner_decision_action_count": 0, "safe_machine_action_count": 0}
    payload["machine_cleanup_actions"] = []
    payload["owner_decisions_required"] = []
    return payload


def _passing_owner_gate_payload() -> dict[str, object]:
    return {
        "report_type": "github_publish_owner_decision_gate",
        "passed": True,
        "status": "passed",
        "decision_count": 0,
        "complete_decision_count": 0,
        "incomplete_decision_ids": [],
    }


def _passing_public_gate_payload() -> dict[str, object]:
    return {
        "report_type": "public_release_gate",
        "passed": True,
        "status": "passed",
        "finding_count": 0,
        "action_count": 0,
    }


def _verification_payload() -> dict[str, object]:
    return {
        "report_type": "release_evidence_bundle_verification",
        "evidence_profile": "mcp-product-readiness",
        "repo_commit": "a" * 40,
        "passed": True,
        "artifact_count": 12,
        "failure_count": 0,
        "warning_count": 0,
        "failures": [],
        "warnings": [],
    }


if __name__ == "__main__":
    unittest.main()
