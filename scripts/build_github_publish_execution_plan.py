from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


HASH_CHUNK_BYTES = 1024 * 1024
SOURCE_ONLY_DECISION_IDS = {
    "license_selection",
    "sample_redistribution_policy",
    "nonpublic_doc_policy",
    "identifier_fixture_policy",
}
PRODUCT_DECISION_IDS = {
    "product_remediation_parser_release_evidence",
    "product_remediation_temporal_metadata_review",
    "product_remediation_runtime_reapproval_and_reindex",
}


def build_github_publish_execution_plan(
    *,
    readiness_summary_report: Path,
    owner_decision_gate_report: Path | None = None,
    public_release_gate_report: Path | None = None,
    evidence_verification_report: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    readiness = _load_json(readiness_summary_report)
    owner_gate = _load_json(owner_decision_gate_report) if owner_decision_gate_report else {}
    public_gate = _load_json(public_release_gate_report) if public_release_gate_report else {}
    evidence_verification = _load_json(evidence_verification_report) if evidence_verification_report else {}

    owner_decisions = _list_of_dicts(readiness.get("owner_decisions_required"))
    incomplete_decision_ids = _incomplete_decision_ids(owner_gate, owner_decisions)
    source_incomplete = [decision_id for decision_id in incomplete_decision_ids if decision_id in SOURCE_ONLY_DECISION_IDS]
    product_incomplete = [decision_id for decision_id in incomplete_decision_ids if decision_id in PRODUCT_DECISION_IDS]
    public_status = _public_status(readiness, public_gate)
    product_status = _dict(readiness.get("product_readiness_status"))
    strict_gap = _dict(readiness.get("strict_parser_gap_summary"))
    cleanup_breakdown = _dict(readiness.get("cleanup_breakdown"))
    machine_cleanup_actions = _list_of_dicts(readiness.get("machine_cleanup_actions"))

    source_only_blockers = _source_only_blockers(
        public_status=public_status,
        incomplete_decision_ids=source_incomplete,
        cleanup_breakdown=cleanup_breakdown,
    )
    product_blockers = _product_blockers(
        product_status=product_status,
        strict_gap=strict_gap,
        incomplete_decision_ids=product_incomplete,
        readiness=readiness,
        evidence_verification=evidence_verification,
        evidence_verification_report=evidence_verification_report,
    )
    phases = _phases(
        readiness=readiness,
        owner_gate=owner_gate,
        owner_decisions=owner_decisions,
        source_only_blockers=source_only_blockers,
        product_blockers=product_blockers,
        machine_cleanup_actions=machine_cleanup_actions,
    )
    report = {
        "report_type": "github_publish_execution_plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "overall_status": _overall_status(source_only_blockers, product_blockers),
        "publish_modes": {
            "source_only_public_github": {
                "status": "blocked" if source_only_blockers else "ready_for_cleanup_branch_validation",
                "blocker_count": len(source_only_blockers),
                "blockers": source_only_blockers,
                "target_time_after_owner_decisions": _dict(readiness.get("time_estimates")).get(
                    "source_only_public_branch"
                ),
            },
            "product_public_release": {
                "status": "blocked" if product_blockers else "ready_for_product_release_validation",
                "blocker_count": len(product_blockers),
                "blockers": product_blockers,
                "target_time_after_owner_decisions": _dict(readiness.get("time_estimates")).get(
                    "product_public_release"
                ),
            },
        },
        "owner_decision_gate": {
            "supplied": bool(owner_gate),
            "passed": bool(owner_gate.get("passed")) if owner_gate else False,
            "status": owner_gate.get("status") if owner_gate else "owner_decision_gate_not_supplied",
            "decision_count": owner_gate.get("decision_count") if owner_gate else len(owner_decisions),
            "complete_decision_count": owner_gate.get("complete_decision_count") if owner_gate else 0,
            "incomplete_decision_count": len(incomplete_decision_ids),
            "incomplete_decision_ids": incomplete_decision_ids,
        },
        "decision_guidance": [_decision_guidance(decision, readiness) for decision in owner_decisions],
        "execution_phases": phases,
        "validation_commands": _validation_commands(),
        "source_reports": {
            "readiness_summary_report": str(readiness_summary_report),
            "owner_decision_gate_report": str(owner_decision_gate_report) if owner_decision_gate_report else None,
            "public_release_gate_report": str(public_release_gate_report) if public_release_gate_report else None,
            "evidence_verification_report": str(evidence_verification_report) if evidence_verification_report else None,
        },
        "source_report_artifacts": [
            _source_artifact("readiness_summary_report", readiness_summary_report, readiness),
            *(
                [_source_artifact("owner_decision_gate_report", owner_decision_gate_report, owner_gate)]
                if owner_decision_gate_report
                else []
            ),
            *(
                [_source_artifact("public_release_gate_report", public_release_gate_report, public_gate)]
                if public_release_gate_report
                else []
            ),
            *(
                [_source_artifact("evidence_verification_report", evidence_verification_report, evidence_verification)]
                if evidence_verification_report
                else []
            ),
        ],
        "safety_note": (
            "This plan is read-only. It does not remove files, add a license, fill owner decisions, "
            "approve chunks, or write Vector DB records."
        ),
        "api_call_count": 0,
    }

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _source_only_blockers(
    *,
    public_status: dict[str, Any],
    incomplete_decision_ids: list[str],
    cleanup_breakdown: dict[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if not bool(public_status.get("passed")):
        blockers.append(
            {
                "code": "public_release_gate_blocked",
                "detail": "Public release gate is not passing on the current source tree.",
                "finding_count": _int(public_status.get("finding_count")),
                "action_count": _int(public_status.get("action_count")),
            }
        )
    if incomplete_decision_ids:
        blockers.append(
            {
                "code": "source_only_owner_decisions_incomplete",
                "detail": "Source-only public GitHub publication needs legal/policy owner decisions.",
                "decision_ids": incomplete_decision_ids,
            }
        )
    owner_action_count = _int(cleanup_breakdown.get("owner_decision_action_count"))
    if owner_action_count:
        blockers.append(
            {
                "code": "owner_gated_cleanup_actions",
                "detail": "Cleanup plan includes owner-gated destructive or policy actions.",
                "owner_decision_action_count": owner_action_count,
            }
        )
    return blockers


def _product_blockers(
    *,
    product_status: dict[str, Any],
    strict_gap: dict[str, Any],
    incomplete_decision_ids: list[str],
    readiness: dict[str, Any],
    evidence_verification: dict[str, Any],
    evidence_verification_report: Path | None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    blocking_codes = [str(code) for code in product_status.get("blocking_codes") or []]
    if blocking_codes:
        blockers.append(
            {
                "code": "product_readiness_blockers_open",
                "detail": "Product readiness has blockers that must be resolved before product public release.",
                "blocking_codes": blocking_codes,
            }
        )
    warning_codes = [str(code) for code in product_status.get("warning_codes") or []]
    if warning_codes:
        blockers.append(
            {
                "code": "product_readiness_warnings_open",
                "detail": "Product readiness has warnings that require release evidence or owner sign-off.",
                "warning_codes": warning_codes,
            }
        )
    if strict_gap and not bool(strict_gap.get("passed")):
        gap_counts = _dict(strict_gap.get("gap_counts"))
        blockers.append(
            {
                "code": "strict_parser_release_evidence_missing",
                "detail": "Strict parser readiness is not yet acceptable for product public release.",
                "failed_info_check_total": _int(gap_counts.get("failed_info_check_total")),
                "recommendation_total": _int(gap_counts.get("recommendation_total")),
                "missing_required_field_total": _int(gap_counts.get("missing_required_field_total")),
            }
        )
    if incomplete_decision_ids:
        blockers.append(
            {
                "code": "product_owner_decisions_incomplete",
                "detail": "Product public release decisions and evidence references are incomplete.",
                "decision_ids": incomplete_decision_ids,
            }
        )
    blockers.extend(
        _product_evidence_verification_blockers(
            readiness=readiness,
            evidence_verification=evidence_verification,
            evidence_verification_report=evidence_verification_report,
        )
    )
    return blockers


def _product_evidence_verification_blockers(
    *,
    readiness: dict[str, Any],
    evidence_verification: dict[str, Any],
    evidence_verification_report: Path | None,
) -> list[dict[str, Any]]:
    if not evidence_verification_report:
        return [
            {
                "code": "product_release_evidence_verification_missing",
                "detail": (
                    "Product public release requires a current mcp-product-readiness release evidence "
                    "verification report, not only validation commands in the plan."
                ),
                "required_report": "reports/mcp_product_readiness_release_evidence_verification_current.json",
            }
        ]
    blockers: list[dict[str, Any]] = []
    if evidence_verification.get("report_type") != "release_evidence_bundle_verification":
        blockers.append(
            {
                "code": "product_release_evidence_verification_report_type",
                "detail": "Evidence verification report has an unexpected report_type.",
                "report_type": evidence_verification.get("report_type"),
                "path": str(evidence_verification_report),
            }
        )
    if evidence_verification.get("evidence_profile") != "mcp-product-readiness":
        blockers.append(
            {
                "code": "product_release_evidence_verification_profile",
                "detail": "Product public release requires the mcp-product-readiness evidence profile.",
                "evidence_profile": evidence_verification.get("evidence_profile"),
                "path": str(evidence_verification_report),
            }
        )
    if evidence_verification.get("passed") is not True or _int(evidence_verification.get("failure_count")) > 0:
        failures = _list_of_dicts(evidence_verification.get("failures"))
        blockers.append(
            {
                "code": "product_release_evidence_verification_failed",
                "detail": "Product public release evidence verification must pass before product release validation.",
                "failure_count": _int(evidence_verification.get("failure_count")),
                "failure_checks": sorted({str(item.get("check")) for item in failures if item.get("check")}),
                "path": str(evidence_verification_report),
            }
        )
    readiness_commit = readiness.get("repo_commit")
    verification_commit = evidence_verification.get("repo_commit")
    if readiness_commit and verification_commit and readiness_commit != verification_commit:
        blockers.append(
            {
                "code": "product_release_evidence_verification_stale_commit",
                "detail": "Readiness summary and evidence verification report were generated from different commits.",
                "readiness_repo_commit": readiness_commit,
                "verification_repo_commit": verification_commit,
            }
        )
    return blockers


def _phases(
    *,
    readiness: dict[str, Any],
    owner_gate: dict[str, Any],
    owner_decisions: list[dict[str, Any]],
    source_only_blockers: list[dict[str, Any]],
    product_blockers: list[dict[str, Any]],
    machine_cleanup_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "order": 1,
            "phase_id": "owner_decision_lock",
            "status": "ready" if bool(owner_gate.get("passed")) else "blocked_pending_owner_decisions",
            "goal": "Capture legal/policy/product decisions before public-branch cleanup.",
            "blocks_source_only_publish": True,
            "blocks_product_release": True,
            "actions": [_decision_action(decision, readiness) for decision in owner_decisions],
        },
        {
            "order": 2,
            "phase_id": "source_only_cleanup_branch",
            "status": "blocked" if source_only_blockers else "ready",
            "goal": "Apply cleanup only on a dedicated public-release branch.",
            "blocks_source_only_publish": True,
            "blocks_product_release": True,
            "actions": [
                {
                    "action_id": "create_public_release_branch",
                    "action": "Create a dedicated public-release branch before any destructive cleanup.",
                    "command": "git switch -c public-release",
                    "requires_owner_decision_gate": True,
                    "destructive": False,
                },
                *[
                    {
                        "action_id": str(action.get("action")),
                        "action": str(action.get("reason") or action.get("action")),
                        "path": action.get("path"),
                        "command": action.get("command"),
                        "requires_owner_decision_gate": False,
                        "destructive": bool(action.get("destructive")),
                    }
                    for action in machine_cleanup_actions
                ],
            ],
        },
        {
            "order": 3,
            "phase_id": "product_evidence_closure",
            "status": "blocked" if product_blockers else "ready",
            "goal": "Close product-public warnings without weakening approval or citation gates.",
            "blocks_source_only_publish": False,
            "blocks_product_release": True,
            "actions": _product_evidence_actions(readiness),
        },
        {
            "order": 4,
            "phase_id": "fresh_clone_validation",
            "status": "waiting_for_clean_public_branch" if source_only_blockers else "ready",
            "goal": "Prove a clean branch installs, tests, and passes release gates from a fresh clone.",
            "blocks_source_only_publish": True,
            "blocks_product_release": True,
            "actions": [
                {
                    "action_id": "run_validation_command",
                    "action": command["purpose"],
                    "command": command["command"],
                    "required_for": command["required_for"],
                }
                for command in _validation_commands()
            ],
        },
    ]


def _decision_action(decision: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    guidance = _decision_guidance(decision, readiness)
    return {
        "action_id": guidance["decision_id"],
        "action": guidance["required_decision"],
        "recommended_publish_safe_default": guidance["recommended_publish_safe_default"],
        "required_evidence": guidance["required_evidence"],
        "inputs": guidance["inputs"],
    }


def _decision_guidance(decision: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    decision_id = str(decision.get("decision_id") or "")
    inputs = [str(value) for value in decision.get("inputs") or []]
    base = {
        "decision_id": decision_id,
        "workstream": "product_public_release" if decision_id in PRODUCT_DECISION_IDS else "source_only_github_publish",
        "summary": str(decision.get("summary") or ""),
        "inputs": inputs,
        "required_evidence": ["decision", "decision_owner", "decision_reference"],
        "required_decision": "Fill decision, decision_owner, and decision_reference in the owner decision CSV.",
        "recommended_publish_safe_default": "Keep publication blocked until the owner decision is recorded.",
    }
    strict_gap = _dict(readiness.get("strict_parser_gap_summary"))
    product_status = _dict(readiness.get("product_readiness_status"))
    temporal = _dict(product_status.get("temporal_backfill_summary"))
    reapproval = _dict(product_status.get("reapproval_summary"))
    guidance_by_id: dict[str, dict[str, Any]] = {
        "license_selection": {
            "required_decision": "Choose the repository license and add a matching LICENSE file.",
            "recommended_publish_safe_default": "Do not publish a public repository until LICENSE exists.",
            "required_evidence": ["LICENSE file", "owner/legal approval reference", "decision owner"],
        },
        "sample_redistribution_policy": {
            "required_decision": "Remove the tracked HWP sample or document redistribution approval.",
            "recommended_publish_safe_default": "Remove the sample from the public branch unless redistribution evidence exists.",
            "required_evidence": ["sample origin", "redistribution permission or removal commit", "public sample manifest update"],
        },
        "nonpublic_doc_policy": {
            "required_decision": "Remove private/internal docs or rewrite selected content as public-safe documentation.",
            "recommended_publish_safe_default": "Remove private/internal docs from the public branch first.",
            "required_evidence": ["public-safe replacement decision", "review owner", "removal or rewrite reference"],
        },
        "identifier_fixture_policy": {
            "required_decision": "Replace institution-derived identifiers with synthetic fixtures or remove the affected artifacts.",
            "recommended_publish_safe_default": "Remove generated reports and synthesize test fixtures before public release.",
            "required_evidence": ["synthetic fixture provenance", "identifier scan result", "fixture update reference"],
        },
        "product_remediation_parser_release_evidence": {
            "required_decision": "Replace review-tolerance parser evidence with strict parser release evidence.",
            "recommended_publish_safe_default": "Treat product public release as blocked until strict parser gaps are closed.",
            "required_evidence": [
                f"failed_info_check_total={_int(_dict(strict_gap.get('gap_counts')).get('failed_info_check_total'))}",
                f"recommendation_total={_int(_dict(strict_gap.get('gap_counts')).get('recommendation_total'))}",
                "strict parser rerun or accepted release exception",
            ],
        },
        "product_remediation_temporal_metadata_review": {
            "required_decision": "Decide how ambiguous temporal metadata is carried into approved runtime evidence.",
            "recommended_publish_safe_default": "Carry ambiguity as explicit metadata and block silent date inference.",
            "required_evidence": [
                f"conflict_chunk_count={_int(temporal.get('conflict_chunk_count'))}",
                f"ambiguous_chunk_count={_int(temporal.get('ambiguous_chunk_count'))}",
                "temporal ambiguity policy reference",
            ],
        },
        "product_remediation_runtime_reapproval_and_reindex": {
            "required_decision": (
                "Complete reapproval batches, reprocess stale chunks, and reindex with approval provenance "
                "and approval-journal coverage."
            ),
            "recommended_publish_safe_default": (
                "Keep product release blocked until batch decisions are filled, reindex evidence is regenerated, "
                "and approval journal coverage is complete."
            ),
            "required_evidence": [
                f"batch_count={_int(reapproval.get('batch_count'))}",
                "completed reapproval decision CSV",
                "approval_journal_coverage.missing_record_count=0",
                "approval_journal_coverage.matched_record_count>=eligible_record_count",
                "post-reindex product readiness report",
                "MCP handoff/release verification with approval_journal_coverage",
            ],
        },
    }
    base.update(guidance_by_id.get(decision_id, {}))
    return base


def _product_evidence_actions(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    product_status = _dict(readiness.get("product_readiness_status"))
    strict_gap = _dict(readiness.get("strict_parser_gap_summary"))
    temporal = _dict(product_status.get("temporal_backfill_summary"))
    reapproval = _dict(product_status.get("reapproval_summary"))
    actions = [
        {
            "action_id": "strict_parser_gap_triage",
            "action": "Triage strict parser failed checks and missing required metadata before citation-grade release.",
            "evidence": {
                "failed_checks": _dict(readiness.get("strict_parser_candidate_status")).get("failed_checks"),
                "gap_counts": strict_gap.get("gap_counts"),
                "missing_required_field_counts": strict_gap.get("missing_required_field_counts"),
            },
        },
        {
            "action_id": "temporal_ambiguity_policy",
            "action": "Review temporal ambiguity scope and decide answer/indexing behavior for ambiguous dates.",
            "evidence": temporal,
        },
        {
            "action_id": "runtime_reapproval_reindex",
            "action": (
                "Complete reapproval review batches, reindex, and regenerate product readiness, MCP visibility, "
                "handoff, and release evidence with approval-journal coverage."
            ),
            "evidence": {
                **reapproval,
                "required_post_reindex_evidence": [
                    "approval_provenance_coverage.complete_record_count",
                    "approval_journal_coverage.eligible_record_count",
                    "approval_journal_coverage.matched_record_count",
                    "approval_journal_coverage.missing_record_count=0",
                    "mcp_handoff_current.json includes mcp_index_visibility_summary.approval_journal_coverage",
                    "mcp-product-readiness release evidence verification passes",
                ],
            },
        },
    ]
    warning_codes = set(str(code) for code in product_status.get("warning_codes") or [])
    if "runtime-version-drift-evidence" in warning_codes:
        actions.append(
            {
                "action_id": "runtime_version_drift_closure",
                "action": "Review runtime version drift and either reprocess stale chunks or record release-owner acceptance.",
                "evidence": {"warning_code": "runtime-version-drift-evidence"},
            }
        )
    return actions


def _validation_commands() -> list[dict[str, str]]:
    return [
        {
            "command": "python -m unittest discover -s tests -q",
            "purpose": "Run the full unit test suite.",
            "required_for": "source_only_public_github,product_public_release",
        },
        {
            "command": "python -m build --sdist --wheel",
            "purpose": "Build source and wheel distributions.",
            "required_for": "source_only_public_github,product_public_release",
        },
        {
            "command": "reg-rag-public-release-gate --include-untracked --out-json reports/public_release_gate_current.json --out-md reports/public_release_gate_current.md",
            "purpose": "Re-run the public release gate on the cleaned branch.",
            "required_for": "source_only_public_github,product_public_release",
        },
        {
            "command": "reg-rag-github-publish-owner-decisions --decisions-csv reports/github_publish_owner_decisions_current.csv --readiness-summary-report reports/github_publish_readiness_current.json --out-json reports/github_publish_owner_decision_gate_current.json --out-md reports/github_publish_owner_decision_gate_current.md --fail-on-blocker",
            "purpose": "Fail closed until owner decision, owner, and reference fields are complete.",
            "required_for": "source_only_public_github,product_public_release",
        },
        {
            "command": "reg-rag-fresh-clone-rehearsal --mode public --dry-run --out-json reports/fresh_clone_rehearsal_plan_current.json --fail-on-issue",
            "purpose": "Validate the public fresh-clone rehearsal plan.",
            "required_for": "source_only_public_github",
        },
        {
            "command": "reg-rag-mcp-product-readiness --out-json reports/mcp_product_readiness_current.json --out-md reports/mcp_product_readiness_current.md",
            "purpose": "Regenerate product readiness evidence after parser, temporal, or reapproval changes.",
            "required_for": "product_public_release",
        },
        {
            "command": "reg-rag-mcp-doctor --audit-index-visibility --require-indexed --forbid-smoke-docs --out-json reports/mcp_connection_readiness_current.json --json",
            "purpose": "Regenerate MCP connection readiness and approval-journal visibility evidence.",
            "required_for": "product_public_release",
        },
        {
            "command": "reg-rag-mcp-authority --authoritative-artifact product_readiness=reports/mcp_product_readiness_current.json --authoritative-artifact mcp_demo_answers=reports/mcp_demo_answers_current.json --authoritative-artifact mcp_transport_smoke=reports/mcp_transport_smoke_current.json --authoritative-artifact mcp_index_visibility=reports/mcp_index_visibility_current.json --authoritative-artifact mcp_connection_readiness=reports/mcp_connection_readiness_current.json --out-json reports/mcp_readiness_authority_current.json --out-md reports/mcp_readiness_authority_current.md --fail-on-issue",
            "purpose": "Fingerprint the current product-readiness, connection, and MCP visibility artifacts before handoff.",
            "required_for": "product_public_release",
        },
        {
            "command": "reg-rag-mcp-handoff-report --product-readiness-report reports/mcp_product_readiness_current.json --mcp-demo-answer-report reports/mcp_demo_answers_current.json --mcp-readiness-report reports/mcp_connection_readiness_current.json --mcp-index-visibility-report reports/mcp_index_visibility_current.json --authority-manifest reports/mcp_readiness_authority_current.json --out-json reports/mcp_handoff_current.json --out-md reports/mcp_handoff_current.md --fail-on-issue",
            "purpose": "Regenerate authority-backed handoff evidence and fail if approval-journal coverage is missing or incomplete.",
            "required_for": "product_public_release",
        },
        {
            "command": "reg-rag-release-evidence-index --profile mcp-product-readiness --repo-root . --out-json reports/mcp_product_readiness_release_evidence_index_current.json",
            "purpose": "Index product-readiness release artifacts including MCP handoff and approval-journal coverage summaries.",
            "required_for": "product_public_release",
        },
        {
            "command": "reg-rag-verify-release-evidence --index-json reports/mcp_product_readiness_release_evidence_index_current.json --repo-root . --out-json reports/mcp_product_readiness_release_evidence_verification_current.json",
            "purpose": "Verify the product-readiness release evidence bundle before publication.",
            "required_for": "product_public_release",
        },
    ]


def _overall_status(source_only_blockers: list[dict[str, Any]], product_blockers: list[dict[str, Any]]) -> str:
    if not source_only_blockers and not product_blockers:
        return "ready_for_public_github_and_product_release_validation"
    if not source_only_blockers:
        return "source_only_public_github_ready_product_release_blocked"
    return "public_github_blocked"


def _public_status(readiness: dict[str, Any], public_gate: dict[str, Any]) -> dict[str, Any]:
    if public_gate:
        return {
            "passed": bool(public_gate.get("passed")),
            "status": public_gate.get("status"),
            "finding_count": _int(public_gate.get("finding_count")),
            "action_count": _int(public_gate.get("action_count")),
        }
    return _dict(readiness.get("public_release_gate_status"))


def _incomplete_decision_ids(owner_gate: dict[str, Any], owner_decisions: list[dict[str, Any]]) -> list[str]:
    if owner_gate:
        return [str(value) for value in owner_gate.get("incomplete_decision_ids") or []]
    return [str(decision.get("decision_id")) for decision in owner_decisions if decision.get("decision_id")]


def _source_artifact(role: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    exists = path.exists()
    return {
        "role": role,
        "path": str(path),
        "exists": exists,
        "sha256": _sha256_file(path) if exists else None,
        "byte_count": path.stat().st_size if exists else None,
        "report_type": payload.get("report_type"),
        "generated_at": payload.get("generated_at"),
        "repo_commit": payload.get("repo_commit"),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_markdown(report: dict[str, Any]) -> str:
    modes = _dict(report.get("publish_modes"))
    source_mode = _dict(modes.get("source_only_public_github"))
    product_mode = _dict(modes.get("product_public_release"))
    owner_gate = _dict(report.get("owner_decision_gate"))
    lines = [
        "# GitHub Publish Execution Plan",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Overall status: `{report.get('overall_status')}`",
        f"- Source-only public GitHub: `{source_mode.get('status')}`, blockers {source_mode.get('blocker_count')}",
        f"- Product public release: `{product_mode.get('status')}`, blockers {product_mode.get('blocker_count')}",
        f"- Owner decision gate: `{owner_gate.get('status')}`, complete {owner_gate.get('complete_decision_count')} / {owner_gate.get('decision_count')}",
        "",
        "## Publish Modes",
        "",
        "| Mode | Status | Blockers | Target Time |",
        "| --- | --- | ---: | --- |",
        "| Source-only public GitHub | `{}` | {} | {} |".format(
            _md_cell(source_mode.get("status")),
            source_mode.get("blocker_count"),
            _md_cell(source_mode.get("target_time_after_owner_decisions")),
        ),
        "| Product public release | `{}` | {} | {} |".format(
            _md_cell(product_mode.get("status")),
            product_mode.get("blocker_count"),
            _md_cell(product_mode.get("target_time_after_owner_decisions")),
        ),
        "",
    ]
    blocker_rows: list[tuple[str, dict[str, Any]]] = []
    for mode_id, mode in (
        ("source_only_public_github", source_mode),
        ("product_public_release", product_mode),
    ):
        for blocker in _list_of_dicts(mode.get("blockers")):
            blocker_rows.append((mode_id, blocker))
    if blocker_rows:
        lines.extend(["## Mode Blockers", "", "| Mode | Code | Detail |", "| --- | --- | --- |"])
        for mode_id, blocker in blocker_rows:
            lines.append(
                "| {mode} | `{code}` | {detail} |".format(
                    mode=_md_cell(mode_id),
                    code=_md_cell(blocker.get("code")),
                    detail=_md_cell(blocker.get("detail")),
                )
            )
        lines.append("")

    lines.extend(
        [
            "## Decision Guidance",
            "",
            "| Decision ID | Workstream | Required Decision | Publish-Safe Default | Required Evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for guidance in _list_of_dicts(report.get("decision_guidance")):
        lines.append(
            "| {decision_id} | {workstream} | {required_decision} | {default} | {evidence} |".format(
                decision_id=_md_cell(guidance.get("decision_id")),
                workstream=_md_cell(guidance.get("workstream")),
                required_decision=_md_cell(guidance.get("required_decision")),
                default=_md_cell(guidance.get("recommended_publish_safe_default")),
                evidence=_md_cell(_compact_list(guidance.get("required_evidence"), limit=4)),
            )
        )

    lines.extend(["", "## Execution Phases", ""])
    for phase in _list_of_dicts(report.get("execution_phases")):
        lines.extend(
            [
                f"### {phase.get('order')}. {phase.get('phase_id')}",
                "",
                f"- Status: `{phase.get('status')}`",
                f"- Goal: {phase.get('goal')}",
                f"- Blocks source-only publish: `{str(bool(phase.get('blocks_source_only_publish'))).lower()}`",
                f"- Blocks product release: `{str(bool(phase.get('blocks_product_release'))).lower()}`",
                "",
            ]
        )
        actions = _list_of_dicts(phase.get("actions"))
        if actions:
            lines.extend(["| Action ID | Action | Command |", "| --- | --- | --- |"])
            for action in actions:
                lines.append(
                    "| {action_id} | {action} | {command} |".format(
                        action_id=_md_cell(action.get("action_id")),
                        action=_md_cell(action.get("action")),
                        command=f"`{_md_cell(action.get('command'))}`" if action.get("command") else "",
                    )
                )
            lines.append("")

    lines.extend(["## Validation Commands", "", "| Required For | Purpose | Command |", "| --- | --- | --- |"])
    for command in _list_of_dicts(report.get("validation_commands")):
        lines.append(
            "| {required_for} | {purpose} | `{command}` |".format(
                required_for=_md_cell(command.get("required_for")),
                purpose=_md_cell(command.get("purpose")),
                command=_md_cell(command.get("command")),
            )
        )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def _compact_list(value: Any, *, limit: int = 5) -> str:
    if not isinstance(value, list):
        return ""
    values = [str(item) for item in value]
    if len(values) <= limit:
        return ", ".join(values) or "-"
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit})"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only GitHub publish execution plan from readiness and owner-decision evidence."
    )
    parser.add_argument("--readiness-summary-report", type=Path, required=True)
    parser.add_argument("--owner-decision-gate-report", type=Path)
    parser.add_argument("--public-release-gate-report", type=Path)
    parser.add_argument("--evidence-verification-report", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    report = build_github_publish_execution_plan(
        readiness_summary_report=args.readiness_summary_report,
        owner_decision_gate_report=args.owner_decision_gate_report,
        public_release_gate_report=args.public_release_gate_report,
        evidence_verification_report=args.evidence_verification_report,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
