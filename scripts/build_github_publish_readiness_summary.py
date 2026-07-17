from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


HASH_CHUNK_BYTES = 1024 * 1024
DECISION_TEMPLATE_FIELDS = (
    "decision_id",
    "workstream",
    "summary",
    "input_count",
    "inputs",
    "decision",
    "decision_owner",
    "decision_reference",
    "notes",
)


def build_github_publish_readiness_summary(
    *,
    public_release_gate_report: Path,
    product_readiness_report: Path,
    remediation_plan_report: Path | None = None,
    evidence_verification_report: Path | None = None,
    strict_parser_candidate_report: Path | None = None,
    strict_gap_summary_report: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
    out_decisions_csv: Path | None = None,
    out_decisions_md: Path | None = None,
) -> dict[str, Any]:
    public_gate = _load_json(public_release_gate_report)
    product = _load_json(product_readiness_report)
    remediation = _load_json(remediation_plan_report) if remediation_plan_report else {}
    verification = _load_json(evidence_verification_report) if evidence_verification_report else {}
    strict_candidate = _load_json(strict_parser_candidate_report) if strict_parser_candidate_report else {}
    strict_gap = _load_json(strict_gap_summary_report) if strict_gap_summary_report else {}

    cleanup_actions = _list_of_dicts(_dict(public_gate.get("cleanup_plan")).get("actions"))
    findings = _list_of_dicts(public_gate.get("findings"))
    warning_codes = [str(code) for code in product.get("warning_codes") or []]
    blocking_codes = [str(code) for code in product.get("blocking_codes") or []]
    tracks = _progress_tracks(
        public_gate=public_gate,
        product=product,
        remediation=remediation,
        verification=verification,
    )
    owner_decisions = _owner_decisions(cleanup_actions=cleanup_actions, remediation=remediation)
    machine_cleanup = _machine_cleanup_actions(cleanup_actions)
    cleanup_breakdown = _cleanup_breakdown(cleanup_actions)
    source_report_artifacts = [
        _source_artifact("public_release_gate_report", public_release_gate_report, public_gate),
        _source_artifact("product_readiness_report", product_readiness_report, product),
        *(
            [_source_artifact("remediation_plan_report", remediation_plan_report, remediation)]
            if remediation_plan_report
            else []
        ),
        *(
            [_source_artifact("evidence_verification_report", evidence_verification_report, verification)]
            if evidence_verification_report
            else []
        ),
        *(
            [_source_artifact("strict_parser_candidate_report", strict_parser_candidate_report, strict_candidate)]
            if strict_parser_candidate_report
            else []
        ),
        *(
            [_source_artifact("strict_gap_summary_report", strict_gap_summary_report, strict_gap)]
            if strict_gap_summary_report
            else []
        ),
    ]
    source_lineage_status = _source_lineage_status(
        source_report_artifacts=source_report_artifacts,
        remediation=remediation,
    )

    report = {
        "report_type": "github_publish_readiness_summary",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "overall_status": _overall_status(
            public_gate=public_gate,
            product=product,
            source_lineage_status=source_lineage_status,
        ),
        "progress_tracks": tracks,
        "public_release_gate_status": {
            "passed": bool(public_gate.get("passed")),
            "status": public_gate.get("status"),
            "finding_count": _int(public_gate.get("finding_count")),
            "action_count": _int(public_gate.get("action_count")),
            "severity_counts": _dict(public_gate.get("severity_counts")),
            "finding_code_counts": dict(Counter(str(finding.get("code")) for finding in findings)),
            "next_actions": list(public_gate.get("next_actions") or []),
        },
        "product_readiness_status": {
            "passed": bool(product.get("passed")),
            "blocking_count": _int(product.get("blocking_count")),
            "warning_count": _int(product.get("warning_count")),
            "blocking_codes": blocking_codes,
            "warning_codes": warning_codes,
            "runtime_summary": _runtime_summary(product),
            "temporal_backfill_summary": _temporal_summary(product),
            "reapproval_summary": _reapproval_summary(product),
        },
        "evidence_verification_status": _verification_status(verification),
        "strict_parser_candidate_status": _strict_parser_candidate_status(strict_candidate),
        "strict_parser_gap_summary": _strict_parser_gap_summary(strict_gap),
        "cleanup_breakdown": cleanup_breakdown,
        "owner_decision_count": len(owner_decisions),
        "owner_decisions_required": owner_decisions,
        "machine_cleanup_action_count": len(machine_cleanup),
        "machine_cleanup_actions": machine_cleanup,
        "recommended_sequence": _recommended_sequence(public_gate=public_gate, product=product),
        "time_estimates": {
            "source_only_public_branch": "0.5-1 day after license/sample/docs decisions",
            "minimum_public_github_publish": "3-5 business days",
            "product_public_release": "1-2 weeks",
        },
        "source_reports": {
            "public_release_gate_report": str(public_release_gate_report),
            "product_readiness_report": str(product_readiness_report),
            "remediation_plan_report": str(remediation_plan_report) if remediation_plan_report else None,
            "evidence_verification_report": str(evidence_verification_report) if evidence_verification_report else None,
            "strict_parser_candidate_report": (
                str(strict_parser_candidate_report) if strict_parser_candidate_report else None
            ),
            "strict_gap_summary_report": str(strict_gap_summary_report) if strict_gap_summary_report else None,
        },
        "source_report_artifacts": source_report_artifacts,
        "source_lineage_status": source_lineage_status,
        "safety_note": (
            "This summary is read-only. It does not remove files, add a license, approve chunks, "
            "or write Vector DB records."
        ),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    if out_decisions_csv:
        _write_decision_template_csv(out_decisions_csv, report)
    if out_decisions_md:
        _write_decision_template_md(out_decisions_md, report)
    return report


def _progress_tracks(
    *,
    public_gate: dict[str, Any],
    product: dict[str, Any],
    remediation: dict[str, Any],
    verification: dict[str, Any],
) -> list[dict[str, Any]]:
    runtime = _runtime_summary(product)
    reapproval = _reapproval_summary(product)
    remediation_items = _list_of_dicts(remediation.get("remediation_items"))
    public_gate_passed = bool(public_gate.get("passed"))
    product_passed = bool(product.get("passed"))
    verification_passed = bool(verification.get("passed")) if verification else None
    product_remaining = _unique_strings(
        [
            *[str(code) for code in product.get("blocking_codes") or []],
            *[str(code) for code in product.get("warning_codes") or []],
        ]
    )

    return [
        {
            "track": "core_pipeline",
            "progress_band": "75-80%" if product_passed else "55-65%",
            "status": "pilot_runtime_ready_with_warnings" if product_passed else "blocked",
            "evidence": {
                "product_readiness_passed": product_passed,
                "repository_chunk_count": runtime.get("repository_chunk_count"),
                "approved_repository_chunk_count": runtime.get("approved_repository_chunk_count"),
                "vector_record_count": runtime.get("vector_record_count"),
                "approval_metadata_complete_ratio": runtime.get("approval_metadata_complete_ratio"),
                "mcp_transport_passed": _dict(product.get("mcp_transport_smoke_summary")).get("passed"),
            },
            "remaining": product_remaining,
        },
        {
            "track": "human_intervention_minimization",
            "progress_band": "65-70%" if _int(reapproval.get("batch_count")) else "70-75%",
            "status": "reapproval_decisions_required" if _int(reapproval.get("batch_count")) else "approval_queue_clear",
            "evidence": reapproval,
            "remaining": [
                item.get("item_id")
                for item in remediation_items
                if item.get("item_id") in {"temporal_metadata_review", "runtime_reapproval_and_reindex"}
            ],
        },
        {
            "track": "source_only_github_publish",
            "progress_band": "45-55%" if not public_gate_passed else "90-95%",
            "status": str(public_gate.get("status") or "unknown"),
            "evidence": {
                "public_gate_passed": public_gate_passed,
                "finding_count": _int(public_gate.get("finding_count")),
                "cleanup_action_count": _int(public_gate.get("action_count")),
                "severity_counts": _dict(public_gate.get("severity_counts")),
            },
            "remaining": list(public_gate.get("next_actions") or []),
        },
        {
            "track": "product_public_release",
            "progress_band": "40-50%" if not public_gate_passed else "55-65%",
            "status": "public_gate_blocked" if not public_gate_passed else "public_source_ready_product_warnings_remain",
            "evidence": {
                "product_readiness_passed": product_passed,
                "product_warning_count": _int(product.get("warning_count")),
                "evidence_verification_passed": verification_passed,
                "public_gate_passed": public_gate_passed,
            },
            "remaining": [
                *product_remaining,
                *(["public-release-gate-blocked"] if not public_gate_passed else []),
            ],
        },
    ]


def _unique_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _overall_status(
    *,
    public_gate: dict[str, Any],
    product: dict[str, Any],
    source_lineage_status: dict[str, Any] | None = None,
) -> str:
    if source_lineage_status and _int(source_lineage_status.get("blocking_count")) > 0:
        return "source_report_lineage_blocked"
    if public_gate.get("passed") and product.get("passed") and _int(product.get("warning_count")) == 0:
        return "ready_for_public_github_publish"
    if product.get("passed") and not public_gate.get("passed"):
        return "internal_pilot_ready_public_github_blocked"
    if product.get("passed"):
        return "product_ready_with_release_warnings"
    return "product_readiness_blocked"


def _runtime_summary(product: dict[str, Any]) -> dict[str, Any]:
    runtime = _dict(product.get("runtime_summary"))
    mcp_readiness = _dict(product.get("mcp_readiness_summary"))
    return {
        "repository_chunk_count": _int(runtime.get("repository_chunk_count")),
        "approved_repository_chunk_count": _int(runtime.get("approved_repository_chunk_count")),
        "unapproved_repository_chunk_count": _int(runtime.get("unapproved_repository_chunk_count")),
        "vector_record_count": _int(runtime.get("vector_record_count")),
        "full_index_match": bool(runtime.get("full_index_match")),
        "approval_metadata_complete_ratio": runtime.get("approval_metadata_complete_ratio"),
        "mcp_readiness_passed": bool(mcp_readiness.get("passed")),
        "mcp_deploy_ready": bool(mcp_readiness.get("deploy_ready")),
    }


def _temporal_summary(product: dict[str, Any]) -> dict[str, Any]:
    temporal = _dict(product.get("temporal_backfill_shadow_summary"))
    return {
        "passed": bool(temporal.get("passed")),
        "conflict_chunk_count": _int(temporal.get("conflict_chunk_count")),
        "ambiguous_chunk_count": _int(temporal.get("ambiguous_chunk_count")),
        "shadow_runtime_written": bool(temporal.get("shadow_runtime_written")),
        "write_blocked": bool(temporal.get("write_blocked")),
        "delta_temporal_metadata_count": _int(temporal.get("delta_temporal_metadata_count")),
    }


def _reapproval_summary(product: dict[str, Any]) -> dict[str, Any]:
    workload = _dict(product.get("reapproval_workload_summary"))
    batches = _dict(product.get("reapproval_review_batch_summary"))
    return {
        "reapproval_candidate_chunks": _int(workload.get("reapproval_candidate_chunks")),
        "recommended_initial_review_chunks": _int(workload.get("recommended_initial_review_chunks")),
        "estimated_initial_review_minutes": _int(workload.get("estimated_initial_review_minutes")),
        "initial_review_reduction_ratio": workload.get("initial_review_reduction_ratio"),
        "batch_count": _int(batches.get("batch_count")),
        "selected_candidate_count": _int(batches.get("selected_candidate_count")),
        "risk_tier_chunk_counts": _dict(batches.get("risk_tier_chunk_counts")),
    }


def _verification_status(verification: dict[str, Any]) -> dict[str, Any] | None:
    if not verification:
        return None
    release_blocker_warnings = [
        item
        for item in verification.get("warnings") or []
        if isinstance(item, dict) and item.get("check") == "json_artifact_release_blocker_count"
    ]
    dirty_worktree_warnings = [
        item
        for item in verification.get("warnings") or []
        if isinstance(item, dict) and item.get("check") == "index_repo_worktree_dirty"
    ]
    return {
        "passed": bool(verification.get("passed")),
        "artifact_count": _int(verification.get("artifact_count")),
        "failure_count": _int(verification.get("failure_count")),
        "warning_count": _int(verification.get("warning_count")),
        "release_blocker_count": sum(_int(item.get("release_blocker_count")) for item in release_blocker_warnings),
        "dirty_worktree": bool(dirty_worktree_warnings),
    }


def _strict_parser_candidate_status(candidate: dict[str, Any]) -> dict[str, Any] | None:
    if not candidate:
        return None
    public = _dict(candidate.get("public_readiness_summary"))
    return {
        "passed": bool(candidate.get("passed")),
        "blocking_count": _int(candidate.get("blocking_count")),
        "warning_count": _int(candidate.get("warning_count")),
        "blocking_codes": list(candidate.get("blocking_codes") or []),
        "warning_codes": list(candidate.get("warning_codes") or []),
        "public_readiness_passed": bool(public.get("passed")),
        "readiness_profile": public.get("readiness_profile"),
        "strict_release_evidence": bool(public.get("strict_release_evidence")),
        "failed_check_count": _int(public.get("failed_check_count")),
        "failed_checks": list(public.get("failed_checks") or []),
        "recommendation_total": _int(public.get("recommendation_total")),
        "input_count": _int(public.get("input_count")),
    }


def _strict_parser_gap_summary(gap: dict[str, Any]) -> dict[str, Any] | None:
    if not gap:
        return None
    gap_counts = _dict(gap.get("gap_counts"))
    return {
        "status": gap.get("status"),
        "passed": bool(gap.get("passed")),
        "source_passed": bool(gap.get("source_passed")),
        "failed_check_count": _int(gap.get("failed_check_count")),
        "gap_counts": {
            "failed_info_check_total": _int(gap_counts.get("failed_info_check_total")),
            "recommendation_total": _int(gap_counts.get("recommendation_total")),
            "recommendation_row_count": _int(gap_counts.get("recommendation_row_count")),
            "missing_required_field_total": _int(gap_counts.get("missing_required_field_total")),
        },
        "missing_required_field_counts": _dict(gap.get("missing_required_field_counts")),
        "top_recommendations": _list_of_dicts(gap.get("top_recommendations"))[:5],
        "remediation_item_ids": [
            str(item.get("item_id"))
            for item in _list_of_dicts(gap.get("remediation_work_items"))
            if item.get("item_id")
        ],
    }


def _owner_decisions(*, cleanup_actions: list[dict[str, Any]], remediation: dict[str, Any]) -> list[dict[str, Any]]:
    action_paths: dict[str, list[str]] = {}
    for action in cleanup_actions:
        action_name = str(action.get("action") or "")
        action_paths.setdefault(action_name, []).append(str(action.get("path") or ""))

    decisions: list[dict[str, Any]] = []
    if "choose_and_add_license" in action_paths:
        decisions.append(
            _decision(
                "license_selection",
                "Choose the repository license before public GitHub publication.",
                action_paths["choose_and_add_license"],
            )
        )
    if "remove_or_document_sample" in action_paths:
        decisions.append(
            _decision(
                "sample_redistribution_policy",
                "Decide whether tracked source documents are removed from the public branch or kept with redistribution evidence.",
                action_paths["remove_or_document_sample"],
            )
        )
    if "remove_nonpublic_doc" in action_paths:
        decisions.append(
            _decision(
                "nonpublic_doc_policy",
                "Decide whether private/internal docs are removed or rewritten as public-safe documentation.",
                action_paths["remove_nonpublic_doc"],
            )
        )
    if "rewrite_public_doc_for_public_release" in action_paths:
        decisions.append(
            _decision(
                "public_doc_rewrite_policy",
                "Decide how README and other public docs should replace private/internal handoff references.",
                action_paths["rewrite_public_doc_for_public_release"],
            )
        )
    if "synthesize_or_remove_identifier_fixture" in action_paths:
        decisions.append(
            _decision(
                "identifier_fixture_policy",
                "Decide whether institution-derived fixtures and reports are synthesized, redacted, or removed.",
                action_paths["synthesize_or_remove_identifier_fixture"],
            )
        )

    for item in _list_of_dicts(remediation.get("remediation_items")):
        item_id = str(item.get("item_id") or "")
        inputs = [str(value) for value in item.get("operator_inputs_required") or []]
        summary = str(item.get("summary") or "Product readiness remediation requires operator input.")
        if item_id == "runtime_reapproval_and_reindex":
            summary = (
                f"{summary} Approval journal coverage must be regenerated and verified before product public release."
            )
            inputs.extend(
                [
                    "approval_journal_coverage.missing_record_count=0",
                    "mcp_connection_readiness_current.json",
                    "mcp_readiness_authority_current.json",
                    "mcp_product_readiness_release_evidence_verification_current.json",
                ]
            )
        if inputs:
            decisions.append(
                _decision(
                    f"product_remediation_{item_id}",
                    summary,
                    inputs,
                )
            )
    return decisions


def _decision(decision_id: str, summary: str, inputs: list[str]) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "summary": summary,
        "inputs": sorted(set(inputs)),
    }


def _machine_cleanup_actions(cleanup_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    machine_ready = {"remove_generated_report"}
    return [
        {
            "action": action.get("action"),
            "path": action.get("path"),
            "command": action.get("command"),
            "reason": action.get("reason"),
            "action_class": action.get("action_class"),
            "apply_scope": action.get("apply_scope"),
            "destructive": action.get("destructive"),
        }
        for action in cleanup_actions
        if action.get("action") in machine_ready
    ]


def _cleanup_breakdown(cleanup_actions: list[dict[str, Any]]) -> dict[str, Any]:
    action_counts = Counter(str(action.get("action")) for action in cleanup_actions)
    action_class_counts = Counter(
        str(action.get("action_class") or "unclassified") for action in cleanup_actions
    )
    paths_by_action: dict[str, list[str]] = {}
    for action in cleanup_actions:
        paths_by_action.setdefault(str(action.get("action")), []).append(str(action.get("path")))
    return {
        "action_counts": dict(sorted(action_counts.items())),
        "action_class_counts": dict(sorted(action_class_counts.items())),
        "owner_decision_action_count": sum(1 for action in cleanup_actions if action.get("requires_owner_decision")),
        "destructive_action_count": sum(1 for action in cleanup_actions if action.get("destructive")),
        "safe_machine_action_count": action_class_counts.get("safe_machine_action", 0),
        "paths_by_action": {key: sorted(values) for key, values in sorted(paths_by_action.items())},
    }


def _recommended_sequence(*, public_gate: dict[str, Any], product: dict[str, Any]) -> list[dict[str, Any]]:
    sequence = [
        {
            "order": 1,
            "workstream": "public_branch_policy",
            "action": "Resolve license, sample redistribution, nonpublic docs, and identifier fixture policy.",
            "blocks_public_publish": not bool(public_gate.get("passed")),
        },
        {
            "order": 2,
            "workstream": "source_only_branch_cleanup",
            "action": "Apply the cleanup plan on a dedicated public-release branch and rerun the public release gate.",
            "blocks_public_publish": not bool(public_gate.get("passed")),
        },
        {
            "order": 3,
            "workstream": "product_evidence",
            "action": "Replace review-tolerance parser evidence with strict parser evidence and complete reapproval decisions.",
            "blocks_public_publish": _int(product.get("warning_count")) > 0,
        },
        {
            "order": 4,
            "workstream": "fresh_clone_ci",
            "action": "Run fresh clone, public harness, unit tests, and release hygiene on the cleaned branch.",
            "blocks_public_publish": True,
        },
    ]
    return sequence


def _source_artifact(role: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "path": str(path),
        "sha256": _sha256_file(path),
        "byte_count": path.stat().st_size,
        "report_type": payload.get("report_type"),
        "generated_at": payload.get("generated_at"),
        "repo_commit": payload.get("repo_commit"),
    }


def _source_lineage_status(
    *,
    source_report_artifacts: list[dict[str, Any]],
    remediation: dict[str, Any],
) -> dict[str, Any]:
    if not remediation:
        return {
            "passed": True,
            "status": "not_applicable",
            "relationship_count": 0,
            "finding_count": 0,
            "blocking_count": 0,
            "warning_count": 0,
            "relationships": [],
            "findings": [],
        }

    current_artifacts = {
        str(item.get("role") or ""): item for item in source_report_artifacts if isinstance(item, dict)
    }
    current_product = current_artifacts.get("product_readiness_report") or {}
    remediation_sources = {
        str(item.get("role") or ""): item
        for item in _list_of_dicts(remediation.get("source_report_artifacts"))
    }
    claimed_product = remediation_sources.get("product_readiness_report")
    findings: list[dict[str, Any]] = []
    relationship = {
        "relationship": "remediation_plan_uses_product_readiness_report",
        "status": "passed",
        "current_sha256": current_product.get("sha256"),
        "claimed_sha256": claimed_product.get("sha256") if claimed_product else None,
        "current_generated_at": current_product.get("generated_at"),
        "claimed_generated_at": claimed_product.get("generated_at") if claimed_product else None,
        "remediation_generated_at": remediation.get("generated_at"),
    }

    if not claimed_product:
        relationship["status"] = "unverified"
        findings.append(
            {
                "severity": "warning",
                "blocking": False,
                "code": "remediation-plan-product-readiness-lineage-missing",
                "detail": "remediation_plan_report does not expose a product_readiness_report source fingerprint",
            }
        )
    else:
        current_sha = str(current_product.get("sha256") or "")
        claimed_sha = str(claimed_product.get("sha256") or "")
        if not claimed_sha:
            relationship["status"] = "unverified"
            findings.append(
                {
                    "severity": "warning",
                    "blocking": False,
                    "code": "remediation-plan-product-readiness-sha-missing",
                    "detail": "remediation_plan_report product_readiness_report source is missing sha256",
                }
            )
        elif current_sha and current_sha != claimed_sha:
            relationship["status"] = "failed"
            findings.append(
                {
                    "severity": "high",
                    "blocking": True,
                    "code": "remediation-plan-product-readiness-sha-mismatch",
                    "detail": "remediation_plan_report was generated from a different product_readiness_report",
                    "current_sha256": current_sha,
                    "claimed_sha256": claimed_sha,
                }
            )

        current_generated_at = _parse_datetime(current_product.get("generated_at"))
        remediation_generated_at = _parse_datetime(remediation.get("generated_at"))
        if current_generated_at and remediation_generated_at and remediation_generated_at < current_generated_at:
            relationship["status"] = "failed"
            findings.append(
                {
                    "severity": "high",
                    "blocking": True,
                    "code": "remediation-plan-older-than-product-readiness",
                    "detail": "remediation_plan_report generated_at predates the selected product_readiness_report",
                    "product_readiness_generated_at": current_product.get("generated_at"),
                    "remediation_generated_at": remediation.get("generated_at"),
                }
            )

    blocking_count = sum(1 for item in findings if item.get("blocking") is True)
    warning_count = sum(1 for item in findings if item.get("severity") == "warning")
    status = "passed"
    if blocking_count:
        status = "blocked"
    elif findings:
        status = "warning"
    return {
        "passed": blocking_count == 0,
        "status": status,
        "relationship_count": 1,
        "finding_count": len(findings),
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "relationships": [relationship],
        "findings": findings,
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _to_markdown(report: dict[str, Any]) -> str:
    public_status = _dict(report.get("public_release_gate_status"))
    product_status = _dict(report.get("product_readiness_status"))
    strict_candidate = _dict(report.get("strict_parser_candidate_status"))
    strict_gap = _dict(report.get("strict_parser_gap_summary"))
    lines = [
        "# GitHub Publish Readiness Summary",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Overall status: `{report.get('overall_status')}`",
        f"- Public gate: `{public_status.get('status')}` / passed `{str(public_status.get('passed')).lower()}`",
        f"- Public findings/actions: {public_status.get('finding_count')} / {public_status.get('action_count')}",
        f"- Product readiness: passed `{str(product_status.get('passed')).lower()}`, warnings {product_status.get('warning_count')}",
        f"- Owner decisions required: {report.get('owner_decision_count')}",
        f"- Machine cleanup actions: {report.get('machine_cleanup_action_count')}",
        "",
        "## Progress Tracks",
        "",
        "| Track | Progress | Status | Remaining |",
        "| --- | --- | --- | --- |",
    ]
    for track in report.get("progress_tracks") or []:
        if not isinstance(track, dict):
            continue
        lines.append(
            "| {track} | {progress} | `{status}` | {remaining} |".format(
                track=_md_cell(track.get("track")),
                progress=_md_cell(track.get("progress_band")),
                status=_md_cell(track.get("status")),
                remaining=_md_cell(_compact_list(track.get("remaining"))),
            )
        )

    lineage = _dict(report.get("source_lineage_status"))
    if lineage:
        lines.extend(
            [
                "",
                "## Source Report Lineage",
                "",
                f"- Status: `{lineage.get('status')}`",
                f"- Findings: {lineage.get('finding_count')}",
                f"- Blocking findings: {lineage.get('blocking_count')}",
                "",
            ]
        )
        findings = _list_of_dicts(lineage.get("findings"))
        if findings:
            lines.extend(["| Severity | Code | Detail |", "| --- | --- | --- |"])
            for finding in findings:
                lines.append(
                    "| {severity} | `{code}` | {detail} |".format(
                        severity=_md_cell(finding.get("severity")),
                        code=_md_cell(finding.get("code")),
                        detail=_md_cell(finding.get("detail")),
                    )
                )

    verification = _dict(report.get("evidence_verification_status"))
    if verification:
        lines.extend(
            [
                "",
                "## Evidence Verification",
                "",
                f"- Passed: `{str(verification.get('passed')).lower()}`",
                f"- Artifacts: {verification.get('artifact_count')}",
                f"- Failures: {verification.get('failure_count')}",
                f"- Warnings: {verification.get('warning_count')}",
                f"- Release blockers: {verification.get('release_blocker_count')}",
                f"- Dirty worktree warning: `{str(verification.get('dirty_worktree')).lower()}`",
            ]
        )

    lines.extend(["", "## Owner Decisions", ""])
    decisions = _list_of_dicts(report.get("owner_decisions_required"))
    if not decisions:
        lines.append("- None.")
    else:
        for decision in decisions:
            lines.append(
                f"- `{decision.get('decision_id')}`: {decision.get('summary')} "
                f"Inputs: {_compact_list(decision.get('inputs'))}"
            )

    lines.extend(["", "## Cleanup Breakdown", ""])
    breakdown = _dict(report.get("cleanup_breakdown"))
    class_counts = _dict(breakdown.get("action_class_counts"))
    if class_counts:
        lines.append(
            "- Action classes: "
            + ", ".join(f"`{action_class}`={count}" for action_class, count in class_counts.items())
        )
    if "owner_decision_action_count" in breakdown:
        lines.append(f"- Owner-decision actions: {breakdown.get('owner_decision_action_count')}")
    if "safe_machine_action_count" in breakdown:
        lines.append(f"- Safe machine actions: {breakdown.get('safe_machine_action_count')}")
    if "destructive_action_count" in breakdown:
        lines.append(f"- Destructive branch actions: {breakdown.get('destructive_action_count')}")
    if class_counts:
        lines.append("")
    for action, count in _dict(breakdown.get("action_counts")).items():
        paths = _dict(breakdown.get("paths_by_action")).get(action) or []
        lines.append(f"- `{action}`: {count} path(s). {_compact_list(paths)}")

    if strict_candidate:
        lines.extend(
            [
                "",
                "## Strict Parser Candidate",
                "",
                f"- Candidate passed: `{str(strict_candidate.get('passed')).lower()}`",
                f"- Blocking codes: {_compact_list(strict_candidate.get('blocking_codes'))}",
                f"- Failed checks: {_compact_list(strict_candidate.get('failed_checks'))}",
                f"- Recommendation total: {strict_candidate.get('recommendation_total')}",
                f"- Input count: {strict_candidate.get('input_count')}",
            ]
        )

    if strict_gap:
        gap_counts = _dict(strict_gap.get("gap_counts"))
        lines.extend(
            [
                "",
                "## Strict Parser Gaps",
                "",
                f"- Gap status: `{strict_gap.get('status')}`",
                f"- Failed info checks: {gap_counts.get('failed_info_check_total')}",
                f"- Recommendation total: {gap_counts.get('recommendation_total')}",
                f"- Missing required fields: {gap_counts.get('missing_required_field_total')}",
                f"- Missing field counts: {_compact_mapping(strict_gap.get('missing_required_field_counts'))}",
            ]
        )
        top_recommendations = _list_of_dicts(strict_gap.get("top_recommendations"))
        if top_recommendations:
            lines.append("- Top recommendations: " + _compact_recommendations(top_recommendations))

    lines.extend(["", "## Recommended Sequence", ""])
    for item in _list_of_dicts(report.get("recommended_sequence")):
        lines.append(
            f"{item.get('order')}. `{item.get('workstream')}`: {item.get('action')} "
            f"Blocks public publish: `{str(item.get('blocks_public_publish')).lower()}`"
        )

    estimates = _dict(report.get("time_estimates"))
    lines.extend(
        [
            "",
            "## Time Estimates",
            "",
            f"- Source-only public branch: {estimates.get('source_only_public_branch')}",
            f"- Minimum public GitHub publish: {estimates.get('minimum_public_github_publish')}",
            f"- Product public release: {estimates.get('product_public_release')}",
            "",
            f"> {report.get('safety_note')}",
            "",
        ]
    )
    return "\n".join(lines)


def _decision_template_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for decision in _list_of_dicts(report.get("owner_decisions_required")):
        inputs = [str(value) for value in decision.get("inputs") or []]
        decision_id = str(decision.get("decision_id") or "")
        rows.append(
            {
                "decision_id": decision_id,
                "workstream": _decision_workstream(decision_id),
                "summary": str(decision.get("summary") or ""),
                "input_count": str(len(inputs)),
                "inputs": "; ".join(inputs),
                "input_sample": _compact_list(inputs),
                "decision": "",
                "decision_owner": "",
                "decision_reference": "",
                "notes": "",
            }
        )
    return rows


def _decision_workstream(decision_id: str) -> str:
    if decision_id.startswith("product_remediation_"):
        return "product_public_release"
    return "source_only_github_publish"


def _write_decision_template_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_TEMPLATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_decision_template_rows(report))


def _decision_template_markdown(report: dict[str, Any]) -> str:
    rows = _decision_template_rows(report)
    lines = [
        "# GitHub Publish Owner Decision Template",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Overall status: `{report.get('overall_status')}`",
        f"- Decision count: {len(rows)}",
        "",
        "| Decision ID | Workstream | Summary | Inputs | Decision | Owner | Reference | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        input_count = row["input_count"]
        inputs = row.get("input_sample") or "-"
        input_summary = f"{input_count} input(s): {inputs}"
        lines.append(
            "| {decision_id} | {workstream} | {summary} | {inputs} |  |  |  |  |".format(
                decision_id=_md_cell(row["decision_id"]),
                workstream=_md_cell(row["workstream"]),
                summary=_md_cell(row["summary"]),
                inputs=_md_cell(input_summary),
            )
        )
    lines.extend(
        [
            "",
            "> Fill Decision, Owner, Reference, and Notes before applying destructive public-branch cleanup actions.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_decision_template_md(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_decision_template_markdown(report), encoding="utf-8")


def _compact_list(value: Any, *, limit: int = 5) -> str:
    if not isinstance(value, list):
        return ""
    values = [str(item) for item in value]
    if len(values) <= limit:
        return ", ".join(values) or "-"
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit})"


def _compact_mapping(value: Any, *, limit: int = 5) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    items = [f"{key}={count}" for key, count in sorted(value.items())]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... (+{len(items) - limit})"


def _compact_recommendations(items: list[dict[str, Any]], *, limit: int = 3) -> str:
    values = []
    for item in items[:limit]:
        values.append(f"{item.get('count')}: {item.get('value')}")
    if len(items) > limit:
        values.append(f"... (+{len(items) - limit})")
    return "; ".join(values) or "-"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only GitHub publish readiness summary from release/product evidence."
    )
    parser.add_argument("--public-release-gate-report", type=Path, required=True)
    parser.add_argument("--product-readiness-report", type=Path, required=True)
    parser.add_argument("--remediation-plan-report", type=Path)
    parser.add_argument("--evidence-verification-report", type=Path)
    parser.add_argument("--strict-parser-candidate-report", type=Path)
    parser.add_argument("--strict-gap-summary-report", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--out-decisions-csv", type=Path)
    parser.add_argument("--out-decisions-md", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    report = build_github_publish_readiness_summary(
        public_release_gate_report=args.public_release_gate_report,
        product_readiness_report=args.product_readiness_report,
        remediation_plan_report=args.remediation_plan_report,
        evidence_verification_report=args.evidence_verification_report,
        strict_parser_candidate_report=args.strict_parser_candidate_report,
        strict_gap_summary_report=args.strict_gap_summary_report,
        out_json=args.out_json,
        out_md=args.out_md,
        out_decisions_csv=args.out_decisions_csv,
        out_decisions_md=args.out_decisions_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
