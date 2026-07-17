from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_github_publish_readiness_summary import (
    build_github_publish_readiness_summary,
    main,
)


class GithubPublishReadinessSummaryTests(unittest.TestCase):
    def test_builds_progress_tracks_and_decision_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_gate = root / "public_gate.json"
            product = root / "product.json"
            remediation = root / "remediation.json"
            verification = root / "verification.json"
            strict_candidate = root / "strict_candidate.json"
            strict_gap = root / "strict_gap.json"
            _write_json(public_gate, _public_gate_payload())
            _write_json(product, _product_payload())
            _write_json(remediation, _remediation_payload())
            _write_json(verification, _verification_payload())
            _write_json(strict_candidate, _strict_candidate_payload())
            _write_json(strict_gap, _strict_gap_payload())

            report = build_github_publish_readiness_summary(
                public_release_gate_report=public_gate,
                product_readiness_report=product,
                remediation_plan_report=remediation,
                evidence_verification_report=verification,
                strict_parser_candidate_report=strict_candidate,
                strict_gap_summary_report=strict_gap,
            )

        self.assertEqual("github_publish_readiness_summary", report["report_type"])
        self.assertEqual("internal_pilot_ready_public_github_blocked", report["overall_status"])
        self.assertEqual("warning", report["source_lineage_status"]["status"])
        self.assertEqual(
            ["remediation-plan-product-readiness-lineage-missing"],
            [finding["code"] for finding in report["source_lineage_status"]["findings"]],
        )
        tracks = {track["track"]: track for track in report["progress_tracks"]}
        self.assertEqual("75-80%", tracks["core_pipeline"]["progress_band"])
        self.assertEqual("65-70%", tracks["human_intervention_minimization"]["progress_band"])
        self.assertEqual("45-55%", tracks["source_only_github_publish"]["progress_band"])
        self.assertEqual("40-50%", tracks["product_public_release"]["progress_band"])
        self.assertEqual(2, report["public_release_gate_status"]["finding_count"])
        self.assertEqual(3, report["owner_decision_count"])
        self.assertEqual(
            {"owner_legal_decision": 1, "owner_policy_decision": 1, "safe_machine_action": 1},
            report["cleanup_breakdown"]["action_class_counts"],
        )
        self.assertEqual(2, report["cleanup_breakdown"]["owner_decision_action_count"])
        self.assertEqual(1, report["cleanup_breakdown"]["safe_machine_action_count"])
        decision_ids = {item["decision_id"] for item in report["owner_decisions_required"]}
        self.assertIn("license_selection", decision_ids)
        self.assertIn("public_doc_rewrite_policy", decision_ids)
        self.assertIn("product_remediation_runtime_reapproval_and_reindex", decision_ids)
        decisions = {item["decision_id"]: item for item in report["owner_decisions_required"]}
        runtime_decision = decisions["product_remediation_runtime_reapproval_and_reindex"]
        self.assertIn("approval journal coverage", runtime_decision["summary"].lower())
        self.assertIn("approval_journal_coverage.missing_record_count=0", runtime_decision["inputs"])
        self.assertIn("mcp_connection_readiness_current.json", runtime_decision["inputs"])
        self.assertIn("mcp_readiness_authority_current.json", runtime_decision["inputs"])
        self.assertEqual(
            ["public-batch-readiness-failed"],
            report["strict_parser_candidate_status"]["blocking_codes"],
        )
        self.assertIn(
            "recommendations_within_limit",
            report["strict_parser_candidate_status"]["failed_checks"],
        )
        self.assertEqual(
            "strict_public_readiness_blocked",
            report["strict_parser_gap_summary"]["status"],
        )
        self.assertEqual(86, report["strict_parser_gap_summary"]["gap_counts"]["recommendation_total"])
        self.assertEqual({"apba_id": 20}, report["strict_parser_gap_summary"]["missing_required_field_counts"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_gate = root / "public_gate.json"
            product = root / "product.json"
            remediation = root / "remediation.json"
            verification = root / "verification.json"
            strict_candidate = root / "strict_candidate.json"
            strict_gap = root / "strict_gap.json"
            out_json = root / "summary.json"
            out_md = root / "summary.md"
            out_decisions_csv = root / "decisions.csv"
            out_decisions_md = root / "decisions.md"
            _write_json(public_gate, _public_gate_payload())
            _write_json(product, _product_payload())
            _write_json(remediation, _remediation_payload())
            _write_json(verification, _verification_payload())
            _write_json(strict_candidate, _strict_candidate_payload())
            _write_json(strict_gap, _strict_gap_payload())
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--public-release-gate-report",
                    str(public_gate),
                    "--product-readiness-report",
                    str(product),
                    "--remediation-plan-report",
                    str(remediation),
                    "--evidence-verification-report",
                    str(verification),
                    "--strict-parser-candidate-report",
                    str(strict_candidate),
                    "--strict-gap-summary-report",
                    str(strict_gap),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--out-decisions-csv",
                    str(out_decisions_csv),
                    "--out-decisions-md",
                    str(out_decisions_md),
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")
            decisions_csv = out_decisions_csv.read_text(encoding="utf-8-sig")
            decisions_md = out_decisions_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("github_publish_readiness_summary", payload["report_type"])
        self.assertIn("GitHub Publish Readiness Summary", markdown)
        self.assertIn("Evidence Verification", markdown)
        self.assertIn("Release blockers: 1", markdown)
        self.assertIn("Strict Parser Candidate", markdown)
        self.assertIn("Strict Parser Gaps", markdown)
        self.assertIn("Missing field counts: apba_id=20", markdown)
        self.assertIn("Owner-decision actions: 2", markdown)
        self.assertIn("Safe machine actions: 1", markdown)
        self.assertIn("decision_id,workstream,summary,input_count,inputs", decisions_csv)
        self.assertIn("license_selection,source_only_github_publish", decisions_csv)
        self.assertIn("product_remediation_runtime_reapproval_and_reindex,product_public_release", decisions_csv)
        self.assertIn("approval_journal_coverage.missing_record_count=0", decisions_csv)
        self.assertIn("mcp_readiness_authority_current.json", decisions_csv)
        self.assertIn("GitHub Publish Owner Decision Template", decisions_md)
        self.assertIn("input(s):", decisions_md)
        self.assertIn("5 input(s):", decisions_md)
        self.assertIn('"source_only_github_publish"', stdout.getvalue())

    def test_progress_tracks_include_product_blocking_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_gate = root / "public_gate.json"
            product = root / "product.json"
            product_payload = _product_payload()
            product_payload["passed"] = False
            product_payload["blocking_count"] = 2
            product_payload["blocking_codes"] = [
                "parser-goldset-f1-missing",
                "table-preprocessing-claim-not-ready",
            ]
            _write_json(public_gate, _public_gate_payload())
            _write_json(product, product_payload)

            report = build_github_publish_readiness_summary(
                public_release_gate_report=public_gate,
                product_readiness_report=product,
            )

        tracks = {track["track"]: track for track in report["progress_tracks"]}
        self.assertEqual(
            [
                "parser-goldset-f1-missing",
                "table-preprocessing-claim-not-ready",
                "runtime-version-drift-evidence",
                "reapproval-worklist-review-evidence",
            ],
            tracks["core_pipeline"]["remaining"],
        )
        self.assertIn(
            "parser-goldset-f1-missing",
            tracks["product_public_release"]["remaining"],
        )
        self.assertIn(
            "public-release-gate-blocked",
            tracks["product_public_release"]["remaining"],
        )

    def test_remediation_product_lineage_passes_when_source_fingerprint_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_gate = root / "public_gate.json"
            product = root / "product.json"
            remediation = root / "remediation.json"
            product_payload = _product_payload()
            product_payload["generated_at"] = "2026-07-09T12:00:00+00:00"
            product_payload["repo_commit"] = "a" * 40
            _write_json(public_gate, _public_gate_payload())
            _write_json(product, product_payload)
            _write_json(
                remediation,
                _remediation_payload(
                    generated_at="2026-07-09T12:10:00+00:00",
                    product_sha256=_sha256_file(product),
                    product_generated_at=str(product_payload["generated_at"]),
                    product_repo_commit=str(product_payload["repo_commit"]),
                ),
            )

            report = build_github_publish_readiness_summary(
                public_release_gate_report=public_gate,
                product_readiness_report=product,
                remediation_plan_report=remediation,
            )

        lineage = report["source_lineage_status"]
        self.assertTrue(lineage["passed"])
        self.assertEqual("passed", lineage["status"])
        self.assertEqual([], lineage["findings"])
        self.assertEqual(
            "passed",
            lineage["relationships"][0]["status"],
        )

    def test_blocks_stale_remediation_plan_product_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_gate = root / "public_gate.json"
            product = root / "product.json"
            remediation = root / "remediation.json"
            product_payload = _product_payload()
            product_payload["generated_at"] = "2026-07-09T12:00:00+00:00"
            product_payload["repo_commit"] = "a" * 40
            _write_json(public_gate, _public_gate_payload())
            _write_json(product, product_payload)
            _write_json(
                remediation,
                _remediation_payload(
                    generated_at="2026-07-09T11:59:00+00:00",
                    product_sha256="0" * 64,
                    product_generated_at="2026-07-09T11:58:00+00:00",
                    product_repo_commit=str(product_payload["repo_commit"]),
                ),
            )

            report = build_github_publish_readiness_summary(
                public_release_gate_report=public_gate,
                product_readiness_report=product,
                remediation_plan_report=remediation,
            )

        lineage = report["source_lineage_status"]
        self.assertFalse(lineage["passed"])
        self.assertEqual("blocked", lineage["status"])
        self.assertEqual("source_report_lineage_blocked", report["overall_status"])
        self.assertEqual(2, lineage["blocking_count"])
        self.assertEqual(
            {
                "remediation-plan-product-readiness-sha-mismatch",
                "remediation-plan-older-than-product-readiness",
            },
            {finding["code"] for finding in lineage["findings"]},
        )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_gate_payload() -> dict[str, object]:
    return {
        "report_type": "public_release_gate",
        "passed": False,
        "status": "blocked_by_public_audit",
        "finding_count": 2,
        "action_count": 2,
        "severity_counts": {"high": 2},
        "findings": [
            {"severity": "high", "code": "missing-license", "path": ".", "detail": "missing"},
            {"severity": "high", "code": "tracked-report-artifact", "path": "reports/a.json", "detail": "report"},
        ],
        "cleanup_plan": {
            "actions": [
                {
                    "action": "choose_and_add_license",
                    "path": "LICENSE",
                    "reason": "license",
                    "command": None,
                    "action_class": "owner_legal_decision",
                    "requires_owner_decision": True,
                    "destructive": False,
                    "apply_scope": "repository_policy",
                },
                {
                    "action": "remove_generated_report",
                    "path": "reports/a.json",
                    "reason": "report",
                    "command": "git rm -- reports/a.json",
                    "action_class": "safe_machine_action",
                    "requires_owner_decision": False,
                    "destructive": True,
                    "apply_scope": "dedicated_public_release_branch",
                },
                {
                    "action": "rewrite_public_doc_for_public_release",
                    "path": "README.md",
                    "reason": "README links private material",
                    "command": None,
                    "action_class": "owner_policy_decision",
                    "requires_owner_decision": True,
                    "destructive": False,
                    "apply_scope": "public_release_branch",
                },
            ]
        },
        "next_actions": ["Decide the open-source license and add LICENSE before public release."],
    }


def _product_payload() -> dict[str, object]:
    return {
        "report_type": "mcp_product_readiness",
        "passed": True,
        "blocking_count": 0,
        "warning_count": 2,
        "blocking_codes": [],
        "warning_codes": ["runtime-version-drift-evidence", "reapproval-worklist-review-evidence"],
        "runtime_summary": {
            "repository_chunk_count": 5997,
            "approved_repository_chunk_count": 5997,
            "unapproved_repository_chunk_count": 0,
            "vector_record_count": 5997,
            "full_index_match": True,
            "approval_metadata_complete_ratio": 1.0,
        },
        "mcp_readiness_summary": {"passed": True, "deploy_ready": True},
        "mcp_transport_smoke_summary": {"passed": True},
        "temporal_backfill_shadow_summary": {
            "passed": True,
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": 4451,
            "shadow_runtime_written": True,
            "write_blocked": False,
            "delta_temporal_metadata_count": 410,
        },
        "reapproval_workload_summary": {
            "reapproval_candidate_chunks": 5997,
            "recommended_initial_review_chunks": 419,
            "estimated_initial_review_minutes": 140,
            "initial_review_reduction_ratio": 0.9301,
        },
        "reapproval_review_batch_summary": {
            "batch_count": 61,
            "selected_candidate_count": 5997,
            "risk_tier_chunk_counts": {"low": 4823, "medium": 1174},
        },
    }


def _remediation_payload(
    *,
    generated_at: str | None = None,
    product_sha256: str | None = None,
    product_generated_at: str | None = None,
    product_repo_commit: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "report_type": "mcp_readiness_remediation_plan",
        "remediation_items": [
            {
                "item_id": "runtime_reapproval_and_reindex",
                "summary": "Complete reapproval batches.",
                "operator_inputs_required": ["reapproval batch decisions"],
            }
        ],
    }
    if generated_at:
        payload["generated_at"] = generated_at
    if product_sha256:
        payload["source_report_artifacts"] = [
            {
                "role": "product_readiness_report",
                "path": "product.json",
                "sha256": product_sha256,
                "byte_count": 1,
                "report_type": "mcp_product_readiness",
                "generated_at": product_generated_at,
                "repo_commit": product_repo_commit,
            }
        ]
    return payload


def _verification_payload() -> dict[str, object]:
    return {
        "report_type": "release_evidence_bundle_verification",
        "passed": True,
        "artifact_count": 26,
        "failure_count": 0,
        "warning_count": 1,
        "warnings": [
            {
                "check": "json_artifact_release_blocker_count",
                "release_blocker_count": 1,
            }
        ],
    }


def _strict_candidate_payload() -> dict[str, object]:
    return {
        "report_type": "mcp_product_readiness",
        "passed": False,
        "blocking_count": 1,
        "warning_count": 3,
        "blocking_codes": ["public-batch-readiness-failed"],
        "warning_codes": [
            "temporal-metadata-coverage-partial",
            "runtime-version-drift-evidence",
            "reapproval-worklist-review-evidence",
        ],
        "public_readiness_summary": {
            "passed": False,
            "readiness_profile": "strict",
            "strict_release_evidence": False,
            "failed_check_count": 2,
            "failed_checks": ["failed_info_checks_within_limit", "recommendations_within_limit"],
            "recommendation_total": 86,
            "input_count": 64,
        },
    }


def _strict_gap_payload() -> dict[str, object]:
    return {
        "report_type": "strict_public_readiness_gap_summary",
        "passed": False,
        "source_passed": False,
        "status": "strict_public_readiness_blocked",
        "failed_check_count": 3,
        "gap_counts": {
            "failed_info_check_total": 7,
            "recommendation_total": 86,
            "recommendation_row_count": 20,
            "missing_required_field_total": 20,
        },
        "missing_required_field_counts": {"apba_id": 20},
        "top_recommendations": [
            {
                "value": "Table/appendix rows marked review_required should be checked before citation-grade RAG use.",
                "count": 11,
            }
        ],
        "remediation_work_items": [
            {"item_id": "failed_info_check_triage"},
            {"item_id": "recommendation_triage"},
            {"item_id": "required_metadata_backfill"},
        ],
    }


if __name__ == "__main__":
    unittest.main()
