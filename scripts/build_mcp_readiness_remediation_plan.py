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


def build_mcp_readiness_remediation_plan(
    *,
    product_readiness_report: Path,
    reapproval_worklist_report: Path | None = None,
    evidence_verification_report: Path | None = None,
    strict_public_readiness_report: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    product = _load_json(product_readiness_report)
    reapproval = _load_json(reapproval_worklist_report) if reapproval_worklist_report else {}
    verification = _load_json(evidence_verification_report) if evidence_verification_report else {}
    strict_public_readiness = _load_json(strict_public_readiness_report) if strict_public_readiness_report else {}
    remediation_items, unmapped_warning_codes = _build_remediation_items(
        product=product,
        reapproval=reapproval,
        strict_public_readiness=strict_public_readiness,
    )
    evidence_verification_status = _verification_status(verification)
    source_report_artifacts = [
        _source_artifact("product_readiness_report", product_readiness_report, product),
        *(
            [_source_artifact("reapproval_worklist_report", reapproval_worklist_report, reapproval)]
            if reapproval_worklist_report
            else []
        ),
        *(
            [_source_artifact("evidence_verification_report", evidence_verification_report, verification)]
            if evidence_verification_report
            else []
        ),
        *(
            [_source_artifact("strict_public_readiness_report", strict_public_readiness_report, strict_public_readiness)]
            if strict_public_readiness_report
            else []
        ),
    ]
    source_consistency_status = _source_consistency_status(source_report_artifacts)
    priority_counts: dict[str, int] = {}
    for item in remediation_items:
        priority_counts[str(item["priority"])] = priority_counts.get(str(item["priority"]), 0) + 1
    readiness_status = {
        "passed": bool(product.get("passed")),
        "blocking_count": _int(product.get("blocking_count")),
        "warning_count": _int(product.get("warning_count")),
        "blocking_codes": list(product.get("blocking_codes") or []),
        "warning_codes": list(product.get("warning_codes") or []),
    }
    plan_blockers = _plan_blockers(
        readiness_status=readiness_status,
        evidence_verification_status=evidence_verification_status,
        source_consistency_status=source_consistency_status,
    )
    plan_status = _plan_status(plan_blockers, remediation_items)
    release_gate_blockers = _release_gate_blockers(
        readiness_status=readiness_status,
        evidence_verification_status=evidence_verification_status,
        source_consistency_status=source_consistency_status,
    )

    generated_at = datetime.now(timezone.utc).isoformat()
    report = {
        "report_type": "mcp_readiness_remediation_plan",
        "generated_at": generated_at,
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": plan_status != "blocked",
        "plan_status": plan_status,
        "plan_blockers": plan_blockers,
        "release_gate_status": "blocked" if release_gate_blockers else "ready_for_private_release",
        "release_gate_blocker_count": len(release_gate_blockers),
        "release_gate_blockers": release_gate_blockers,
        "remediation_item_count": len(remediation_items),
        "priority_counts": dict(sorted(priority_counts.items())),
        "readiness_status": readiness_status,
        "evidence_verification_status": evidence_verification_status,
        "source_consistency_status": source_consistency_status,
        "source_reports": {
            "product_readiness_report": str(product_readiness_report),
            "reapproval_worklist_report": str(reapproval_worklist_report) if reapproval_worklist_report else None,
            "evidence_verification_report": (
                str(evidence_verification_report) if evidence_verification_report else None
            ),
            "strict_public_readiness_report": (
                str(strict_public_readiness_report) if strict_public_readiness_report else None
            ),
        },
        "source_report_artifacts": source_report_artifacts,
        "remediation_items": remediation_items,
        "unmapped_warning_codes": unmapped_warning_codes,
        "safety_note": (
            "This remediation plan is read-only. It does not approve chunks, reprocess files, "
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
    return report


KNOWN_WARNING_CODES = frozenset(
    {
        "public-readiness-review-tolerance-evidence",
        "temporal-metadata-coverage-partial",
        "temporal-backfill-conflict",
        "temporal-ambiguity-scope-missing",
        "temporal-ambiguity-policy-required",
        "temporal-evidence-older-than-threshold",
        "temporal-evidence-runtime-lineage-mismatch",
        "runtime-version-drift-evidence",
        "approval-provenance-vector-evidence-incomplete",
        "approval-journal-vector-evidence-missing",
        "reapproval-worklist-review-evidence",
        "reapproval-apply-plan-blockers",
        "reapproval-apply-plan-safety-contract-missing",
        "parser-goldset-quality-claim-not-ready",
        "parser-goldset-score-issues",
        "parser-goldset-f1-missing",
        "parser-goldset-f1-low",
        "parser-goldset-scope-exclusions",
        "table-preprocessing-claim-not-ready",
        "institution-profile-generic-only",
        "profile-provenance-warnings",
    }
)
TEMPORAL_REMEDIATION_CODES = (
    "temporal-metadata-coverage-partial",
    "temporal-backfill-conflict",
    "temporal-ambiguity-scope-missing",
    "temporal-ambiguity-policy-required",
    "temporal-evidence-older-than-threshold",
    "temporal-evidence-runtime-lineage-mismatch",
)
REAPPROVAL_APPLY_PLAN_REMEDIATION_CODES = (
    "reapproval-apply-plan-blockers",
    "reapproval-apply-plan-safety-contract-missing",
)
PARSER_GOLDSET_REMEDIATION_CODES = (
    "parser-goldset-quality-claim-not-ready",
    "parser-goldset-score-issues",
    "parser-goldset-f1-missing",
    "parser-goldset-f1-low",
)
TABLE_PREPROCESSING_REMEDIATION_CODES = (
    "table-preprocessing-claim-not-ready",
)
PARSER_GOLDSET_SCOPE_REMEDIATION_CODES = (
    "parser-goldset-scope-exclusions",
)
PROFILE_PROVENANCE_REMEDIATION_CODES = (
    "institution-profile-generic-only",
    "profile-provenance-warnings",
)
REAPPROVAL_APPLY_REQUIRED_EXECUTION_STEPS = (
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


def _build_remediation_items(
    *,
    product: dict[str, Any],
    reapproval: dict[str, Any],
    strict_public_readiness: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    warning_codes = set(str(code) for code in product.get("warning_codes") or [])
    blocking_codes = set(str(code) for code in product.get("blocking_codes") or [])
    product_codes = warning_codes | blocking_codes
    items: list[dict[str, Any]] = []
    parser_goldset_codes = [code for code in PARSER_GOLDSET_REMEDIATION_CODES if code in product_codes]
    if parser_goldset_codes:
        score = _dict(product.get("parser_goldset_score_summary"))
        board = _dict(product.get("parser_goldset_completion_board_summary"))
        score_pending_document_count = _int(score.get("pending_document_count"))
        completion_missing_structure_score_rows = _int(
            board.get("missing_structure_score_rows")
        )
        completion_missing_matched_field_count = _int(
            board.get("missing_matched_field_count")
        )
        completion_gate_status = str(board.get("completion_gate_status") or "")
        labels_complete = (
            score_pending_document_count == 0
            and completion_missing_structure_score_rows == 0
            and completion_missing_matched_field_count == 0
            and completion_gate_status == "ready_for_quality_claim"
        )
        if labels_complete:
            parser_summary = (
                "Investigate parser structure/counting accuracy and regenerate the parser score "
                "before making a release-grade parsing claim; the current goldset labels are complete."
            )
            parser_action = (
                "Trace false positives and false negatives by structure, fix parser or counting "
                "behavior, rerun the parser goldset score, and do not alter labels or thresholds."
            )
            parser_inputs = [
                "structure-level parser error analysis",
                "refreshed parsing goldset score JSON/Markdown",
                "reproducible parser diagnostic output with repository-contained paths",
            ]
        else:
            parser_summary = (
                "Complete parser goldset matched-count labels and regenerate the parser score "
                "before making a release-grade parsing claim."
            )
            parser_action = (
                "Fill the remaining human matched-count fields from the 12-document goldset review "
                "packets, rerun the parser goldset score, and only then use the F1 as official publish evidence."
            )
            parser_inputs = [
                "completed parsing goldset label CSV",
                "refreshed parsing goldset score JSON/Markdown",
                "reviewer metadata for every scored goldset row",
            ]
        items.append(
            _item(
                "parser_goldset_label_completion",
                1,
                "Parsing Accuracy",
                parser_goldset_codes,
                parser_summary,
                {
                    "score_document_count": _int(score.get("document_count")),
                    "score_pending_document_count": score_pending_document_count,
                    "score_issue_count": _int(score.get("issue_count")),
                    "score_overall_f1": score.get("overall_f1"),
                    "completion_expected_structure_score_rows": _int(
                        board.get("expected_structure_score_rows")
                    ),
                    "completion_completed_structure_score_rows": _int(
                        board.get("completed_structure_score_rows")
                    ),
                    "completion_missing_structure_score_rows": completion_missing_structure_score_rows,
                    "completion_missing_matched_field_count": completion_missing_matched_field_count,
                    "completion_gate_status": completion_gate_status,
                    "labels_complete": labels_complete,
                },
                parser_action,
                parser_inputs,
                [
                    "parser goldset score ready_for_quality_claim is true",
                    "overall parser F1 is present and meets the release threshold",
                    "product readiness parsing gate is no longer blocked",
                ],
            )
        )
    table_preprocessing_codes = [
        code for code in TABLE_PREPROCESSING_REMEDIATION_CODES if code in product_codes
    ]
    if table_preprocessing_codes:
        table_claim = _dict(product.get("table_preprocessing_claim_gate_summary"))
        items.append(
            _item(
                "table_preprocessing_human_review",
                2,
                "Parsing Accuracy",
                table_preprocessing_codes,
                "Complete table-unit human review and transfer reviewed table counts before making a release-grade table parsing claim.",
                {
                    "selected_unit_count": _int(table_claim.get("selected_unit_count")),
                    "completed_unit_count": _int(table_claim.get("completed_unit_count")),
                    "pending_unit_count": _int(table_claim.get("pending_unit_count")),
                    "invalid_unit_count": _int(table_claim.get("invalid_unit_count")),
                    "ready_for_table_score_transfer": bool(
                        table_claim.get("ready_for_table_score_transfer")
                    ),
                    "transfer_passed": bool(table_claim.get("transfer_passed")),
                    "transfer_blocker_count": _int(table_claim.get("transfer_blocker_count")),
                    "required_field_missing_total": _int(
                        table_claim.get("required_field_missing_total")
                    ),
                    "required_field_missing_counts": _dict(
                        table_claim.get("required_field_missing_counts")
                    ),
                    "review_priority_counts": _dict(table_claim.get("review_priority_counts")),
                    "label_review_flag_counts": _dict(
                        table_claim.get("label_review_flag_counts")
                    ),
                    "source_traceability_passed": bool(
                        table_claim.get("source_traceability_passed")
                    ),
                    "source_traceability_issue_count": _int(
                        table_claim.get("source_traceability_issue_count")
                    ),
                    "source_traceability_issue_counts": _dict(
                        table_claim.get("source_traceability_issue_counts")
                    ),
                    "source_traceability_operator_next_action_counts": _dict(
                        table_claim.get("source_traceability_operator_next_action_counts")
                    ),
                    "source_traceability_require_page_count_verification": bool(
                        table_claim.get("source_traceability_require_page_count_verification")
                    ),
                    "drift_check_present": bool(table_claim.get("drift_check_present")),
                    "drift_check_passed": bool(table_claim.get("drift_check_passed")),
                    "drift_check_blocker_count": _int(table_claim.get("drift_check_blocker_count")),
                    "table_answer_blocker_count": _int(
                        table_claim.get("table_answer_blocker_count")
                    ),
                    "non_review_evidence_ready": bool(
                        table_claim.get("non_review_evidence_ready")
                    ),
                    "release_blocked_by_human_review": bool(
                        table_claim.get("release_blocked_by_human_review")
                    ),
                    "finding_code_counts": _dict(table_claim.get("finding_code_counts")),
                },
                "Fill reviewer status plus manual and matched table counts for every selected table unit, rerun table count transfer validation, regenerate the table preprocessing claim gate, and then rerun product readiness.",
                [
                    "completed table unit review CSV/JSON",
                    "manual and matched table counts for every selected table unit",
                    "reviewer identity and reviewed-at metadata",
                    "refreshed table count transfer validation report",
                ],
                [
                    "ready_for_table_score_transfer is true",
                    "transfer_passed is true",
                    "table preprocessing claim gate has no blockers",
                    "product readiness parsing gate has no table-preprocessing-claim-not-ready code",
                ],
            )
        )
    parser_scope_codes = [
        code for code in PARSER_GOLDSET_SCOPE_REMEDIATION_CODES if code in product_codes
    ]
    if parser_scope_codes:
        score = _dict(product.get("parser_goldset_score_summary"))
        board = _dict(product.get("parser_goldset_completion_board_summary"))
        items.append(
            _item(
                "parser_goldset_scope_decision",
                3,
                "Parsing Accuracy",
                parser_scope_codes,
                "Document the release-owner decision for goldset sources excluded from the quality-claim parser score.",
                {
                    "score_document_count": _int(score.get("document_count")),
                    "scored_document_count": _int(score.get("scored_document_count")),
                    "excluded_document_count": _int(score.get("excluded_document_count")),
                    "completion_excluded_document_count": _int(board.get("excluded_document_count")),
                    "completion_gate_status": str(board.get("completion_gate_status") or ""),
                },
                "Record whether each excluded source is out of scope for the release claim or must be converted into a scored goldset row; regenerate the parser score and product readiness after the decision is attached.",
                [
                    "release-owner goldset scope decision",
                    "excluded-source rationale for each non-article or non-regulation source",
                    "updated parser score report when scope changes",
                ],
                [
                    "parser-goldset-scope-exclusions accepted with fingerprinted rationale or removed",
                    "product readiness parsing gate no longer has unmapped scope warnings",
                ],
            )
        )
    profile_codes = [
        code for code in PROFILE_PROVENANCE_REMEDIATION_CODES if code in product_codes
    ]
    if profile_codes:
        profile = _dict(product.get("profile_provenance_summary"))
        items.append(
            _item(
                "institution_profile_provenance",
                5,
                "Institution Generality",
                profile_codes,
                "Backfill institution-specific profile IDs and APBA/source identity evidence instead of relying only on the generic public-institution profile.",
                {
                    "profile_provenance_passed": bool(profile.get("passed")),
                    "row_count": _int(profile.get("row_count")),
                    "institution_count": _int(profile.get("institution_count")),
                    "batch_profile_counts": _dict(profile.get("batch_profile_counts")),
                    "apba_id_count": _int(profile.get("apba_id_count")),
                    "apba_id_counts": _dict(profile.get("apba_id_counts")),
                    "registry_profile_count": _int(profile.get("registry_profile_count")),
                    "matched_profile_count": _int(profile.get("matched_profile_count")),
                    "unknown_profile_counts": _dict(profile.get("unknown_profile_counts")),
                    "warning_count": _int(profile.get("warning_count")),
                    "blocker_count": _int(profile.get("blocker_count")),
                    "file_type_counts": _dict(profile.get("file_type_counts")),
                },
                "Regenerate the PUBLIC_PORTAL/runtime manifest with institution-specific profile IDs, APBA IDs, source record IDs, and source file IDs; rebuild the profile provenance report and product readiness before claiming institution-general behavior.",
                [
                    "institution profile registry or generated PUBLIC_PORTAL profile manifest",
                    "runtime/batch rows with profile_id and apba_id populated",
                    "profile provenance report with matched institution-specific profiles",
                ],
                [
                    "batch_profile_counts is not only default-public-institution",
                    "apba_id_count matches the profiled runtime row count",
                    "profile provenance warnings are removed or explicitly accepted",
                ],
            )
        )
    if "public-readiness-review-tolerance-evidence" in warning_codes:
        summary = _dict(product.get("public_readiness_summary"))
        items.append(
            _item(
                "parser_release_evidence",
                1,
                "Parsing Accuracy",
                ["public-readiness-review-tolerance-evidence"],
                "Replace review-tolerance parsing evidence with strict parser release evidence.",
                {
                    "review_tolerance_evidence": True,
                    "readiness_profile": str(summary.get("readiness_profile") or ""),
                    "strict_release_evidence": bool(summary.get("strict_release_evidence")),
                    "thresholds": _dict(summary.get("thresholds")),
                    "failed_check_count": len(summary.get("failed_checks") or []),
                    "recommendation_total": _int(summary.get("recommendation_total")),
                    "input_count": _int(summary.get("input_count")),
                    "strict_candidate": _strict_public_readiness_counts(strict_public_readiness),
                },
                "Attach strict public readiness or parser goldset evidence to product readiness and regenerate the MCP evidence chain.",
                ["strict public readiness report or parser goldset score report"],
                ["mcp_product_readiness_current.json has no public-readiness-review-tolerance-evidence warning"],
            )
        )
    temporal_codes = [code for code in TEMPORAL_REMEDIATION_CODES if code in product_codes]
    if temporal_codes:
        summary = _dict(product.get("temporal_coverage_summary"))
        backfill = _dict(product.get("temporal_backfill_shadow_summary"))
        ambiguity = _dict(product.get("temporal_ambiguity_scope_summary"))
        guard = _dict(product.get("temporal_evidence_guard_summary"))
        revision = _dict(product.get("revision_impact_summary"))
        items.append(
            _item(
                "temporal_metadata_review",
                2,
                "Revision Response",
                temporal_codes,
                "Review temporal metadata gaps, evidence freshness, runtime lineage, ambiguity policy, and revision deltas before applying changes.",
                {
                    "record_count": _int(summary.get("record_count")),
                    "with_temporal_metadata_count": _int(summary.get("with_temporal_metadata_count")),
                    "without_temporal_metadata_count": _int(summary.get("without_temporal_metadata_count")),
                    "temporal_metadata_ratio": summary.get("temporal_metadata_ratio"),
                    "candidate_missing_record_count": _int(summary.get("candidate_missing_record_count")),
                    "shadow_delta_temporal_metadata_count": _int(backfill.get("delta_temporal_metadata_count")),
                    "shadow_conflict_chunk_count": _int(backfill.get("conflict_chunk_count")),
                    "shadow_ambiguous_chunk_count": _int(backfill.get("ambiguous_chunk_count")),
                    "shadow_write_blocked": bool(backfill.get("write_blocked")),
                    "temporal_ambiguity_status": str(ambiguity.get("status") or ""),
                    "temporal_ambiguity_blocking_decision_count": _int(ambiguity.get("blocking_decision_count")),
                    "temporal_ambiguity_review_slice_count": _int(ambiguity.get("review_slice_count")),
                    "temporal_evidence_stale_artifact_count": _int(guard.get("stale_artifact_count")),
                    "temporal_evidence_runtime_lineage_mismatch_count": _int(
                        guard.get("runtime_lineage_mismatch_count")
                    ),
                    "temporal_evidence_payload_generated_at_span_hours": guard.get(
                        "payload_generated_at_span_hours"
                    ),
                    "temporal_evidence_strict": bool(guard.get("strict_temporal_evidence")),
                    "revision_impact_approval_required_count": _int(revision.get("approval_required_count")),
                    "revision_impact_metadata_only_changed_count": _int(revision.get("metadata_only_changed_count")),
                    "revision_impact_deindex_required_count": _int(revision.get("deindex_required_count")),
                },
                "Refresh stale temporal evidence, fix runtime-lineage mismatches, resolve temporal conflicts, attach ambiguity index/answer policy decisions, review revision-impact units, apply only reviewed metadata, then rerun temporal coverage and product readiness.",
                [
                    "temporal coverage report",
                    "temporal backfill shadow report",
                    "temporal ambiguity scope report and policy decisions",
                    "revision impact report",
                ],
                ["temporal blocker or warning removed", "accepted residual scope documented with source report fingerprints"],
            )
        )
    runtime_codes = [
        code
        for code in (
            "runtime-version-drift-evidence",
            "approval-provenance-vector-evidence-incomplete",
            "approval-journal-vector-evidence-missing",
            "approval-journal-review-events-incomplete",
            "reapproval-worklist-review-evidence",
        )
        if code in product_codes
    ]
    if runtime_codes:
        drift = _dict(product.get("runtime_version_drift_summary"))
        runtime = _dict(product.get("runtime_summary"))
        coverage = _dict(runtime.get("approval_provenance_coverage"))
        journal_coverage = _dict(runtime.get("approval_journal_coverage"))
        review_event_coverage = _dict(runtime.get("approval_journal_review_event_coverage"))
        missing_counts = {
            str(field): _int(count)
            for field, count in _dict(coverage.get("missing_field_counts")).items()
            if _int(count) > 0
        }
        missing_event_counts = {
            str(event_type): _int(count)
            for event_type, count in _dict(review_event_coverage.get("missing_event_chunk_counts")).items()
            if _int(count) > 0
        }
        workload = _dict(product.get("reapproval_workload_summary"))
        batch = _dict(product.get("reapproval_review_batch_summary"))
        reapproval_counts = _dict(reapproval.get("review_triage_counts"))
        items.append(
            _item(
                "runtime_reapproval_and_reindex",
                3,
                "Operations",
                runtime_codes,
                "Reprocess stale chunks, complete reapproval batches, and reindex with approval provenance and approval journal evidence.",
                {
                    "approved_repository_stale_chunker_count": _int(
                        drift.get("approved_repository_stale_chunker_count")
                    ),
                    "vector_stale_chunker_count": _int(drift.get("vector_stale_chunker_count")),
                    "reprocess_requires_reapproval": bool(drift.get("reprocess_requires_reapproval")),
                    "approval_provenance_complete_record_count": _int(coverage.get("complete_record_count")),
                    "approval_provenance_record_count": _int(coverage.get("record_count")),
                    "approval_provenance_missing_field_counts": missing_counts,
                    "approval_journal_journal_record_count": _int(journal_coverage.get("journal_record_count")),
                    "approval_journal_eligible_record_count": _int(journal_coverage.get("eligible_record_count")),
                    "approval_journal_matched_record_count": _int(journal_coverage.get("matched_record_count")),
                    "approval_journal_missing_record_count": _int(journal_coverage.get("missing_record_count")),
                    "approval_journal_review_event_count": _int(
                        review_event_coverage.get("review_decision_event_count")
                    ),
                    "approval_journal_review_event_expected_chunk_counts": _dict(
                        review_event_coverage.get("expected_event_chunk_counts")
                    ),
                    "approval_journal_review_event_observed_chunk_counts": _dict(
                        review_event_coverage.get("event_chunk_counts")
                    ),
                    "approval_journal_review_event_missing_chunk_counts": missing_event_counts,
                    "approval_journal_review_event_incomplete_record_count": _int(
                        review_event_coverage.get("incomplete_record_count")
                    ),
                    "reapproval_candidate_chunks": _int(workload.get("reapproval_candidate_chunks")),
                    "recommended_initial_review_chunks": _int(workload.get("recommended_initial_review_chunks")),
                    "estimated_initial_review_minutes": _int(workload.get("estimated_initial_review_minutes")),
                    "batch_count": _int(batch.get("batch_count")),
                    "selected_candidate_count": _int(batch.get("selected_candidate_count")),
                    "risk_tier_chunk_counts": _dict(batch.get("risk_tier_chunk_counts")) or reapproval_counts,
                },
                "Use reapproval_review_batches_current.csv/json, record human decisions, ensure UI/API approval appends complete per-chunk review_decision_events, reapprove affected chunks or regenerate approval evidence through the updated approval flow, reindex, rebuild the MCP bundle, then rerun product readiness and release evidence verification. Do not silently edit an old approval journal to pass the gate.",
                [
                    "reapproval batch decisions",
                    "approval provenance fields",
                    "append-only approval journal records",
                    "complete per-chunk AI/human/approval review decision events",
                    "reprocessed source documents",
                ],
                [
                    "approval provenance coverage complete",
                    "approval journal coverage complete",
                    "approval journal review-event coverage complete",
                    "runtime drift warning removed",
                    "release evidence verification passed",
                ],
            )
        )
    apply_plan_codes = [
        code for code in REAPPROVAL_APPLY_PLAN_REMEDIATION_CODES if code in product_codes
    ]
    if apply_plan_codes:
        apply_plan = _dict(product.get("reapproval_apply_plan_summary"))
        observed_steps = _dict(apply_plan.get("observed_execution_step_counts"))
        missing_required_steps = [
            step
            for step in REAPPROVAL_APPLY_REQUIRED_EXECUTION_STEPS
            if _int(observed_steps.get(step)) <= 0
        ]
        items.append(
            _item(
                "reapproval_apply_plan_safety",
                4,
                "Operations",
                apply_plan_codes,
                "Fix the reapproval apply plan safety contract before any dedicated apply or reindex step.",
                {
                    "report_count": _int(apply_plan.get("report_count")),
                    "passed": bool(apply_plan.get("passed")),
                    "blocker_count": _int(apply_plan.get("blocker_count")),
                    "unsafe_contract_violation_count": _int(
                        apply_plan.get("unsafe_contract_violation_count")
                    ),
                    "ready_plan_count": _int(apply_plan.get("ready_plan_count")),
                    "batch_count": _int(apply_plan.get("batch_count")),
                    "batch_apply_control_count": _int(
                        apply_plan.get("batch_apply_control_count")
                    ),
                    "direct_metadata_write_allowed_count": _int(
                        apply_plan.get("direct_metadata_write_allowed_count")
                    ),
                    "mcp_publish_allowed_count": _int(apply_plan.get("mcp_publish_allowed_count")),
                    "missing_required_execution_steps": missing_required_steps,
                    "release_gate_status_counts": _dict(
                        apply_plan.get("release_gate_status_counts")
                    ),
                },
                "Regenerate the read-only reapproval apply plan, then keep the future apply implementation on the shared review workflow contract with approval preconditions, preapproval security scan, review-flag acknowledgement, approval hash recalculation, journals, export refresh, explicit reindex as a separate phase by default, conditional vector sync only after existing successful index state, and MCP visibility verification.",
                [
                    "passed reapproval decision validation report",
                    "safe reapproval apply plan JSON/Markdown",
                    "reviewer-approved batch decisions",
                ],
                [
                    "reapproval_apply_plan_summary passed",
                    "unsafe_contract_violation_count is zero",
                    "release evidence verification passed",
                ],
            )
        )
    unmapped = sorted(warning_codes - KNOWN_WARNING_CODES)
    return _rank_items(items), unmapped


def _strict_public_readiness_counts(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    checks = [item for item in report.get("checks") or [] if isinstance(item, dict)]
    failed_checks = [str(item.get("name")) for item in checks if not bool(item.get("passed"))]
    summary = _dict(report.get("summary"))
    required_row_check = next(
        (item for item in checks if item.get("name") == "required_row_fields_present"),
        {},
    )
    required_row_details = _dict(required_row_check.get("details")) if isinstance(required_row_check, dict) else {}
    return {
        "passed": bool(report.get("passed")),
        "status": str(report.get("status") or ""),
        "readiness_profile": str(report.get("readiness_profile") or ""),
        "strict_release_evidence": bool(report.get("strict_release_evidence")),
        "failed_check_count": len(failed_checks),
        "failed_checks": failed_checks,
        "input_count": _int(summary.get("input_count")),
        "average_quality_score": summary.get("average_quality_score"),
        "failed_info_check_total": _int(summary.get("failed_info_check_total")),
        "recommendation_total": _int(summary.get("recommendation_total")),
        "required_row_fields_missing_count": _int(required_row_details.get("missing_count")),
    }


def _rank_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items.sort(key=lambda item: (_int(item.get("priority")), str(item.get("item_id"))))
    return items


def _item(
    item_id: str,
    priority: int,
    gate: str,
    warning_codes: list[str],
    summary: str,
    source_counts: dict[str, Any],
    recommended_action: str,
    operator_inputs_required: list[str],
    verification_after_remediation: list[str],
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "priority": priority,
        "gate": gate,
        "warning_codes": warning_codes,
        "summary": summary,
        "source_counts": source_counts,
        "recommended_action": recommended_action,
        "operator_inputs_required": operator_inputs_required,
        "verification_after_remediation": verification_after_remediation,
    }


def _plan_blockers(
    *,
    readiness_status: dict[str, Any],
    evidence_verification_status: dict[str, Any] | None,
    source_consistency_status: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if _int(readiness_status.get("blocking_count")) > 0 or readiness_status.get("blocking_codes"):
        blockers.append("product-readiness-blockers-present")
    if evidence_verification_status and (
        evidence_verification_status.get("passed") is False
        or _int(evidence_verification_status.get("failure_count")) > 0
    ):
        blockers.append("evidence-verification-failed")
    if source_consistency_status.get("missing_repo_commit_roles"):
        blockers.append("source-report-repo-commit-missing")
    elif not source_consistency_status.get("consistent", True):
        blockers.append("source-report-repo-commit-mismatch")
    return blockers


def _plan_status(plan_blockers: list[str], remediation_items: list[dict[str, Any]]) -> str:
    if plan_blockers:
        return "blocked"
    if remediation_items:
        return "ready_for_human_remediation"
    return "no_warnings"


def _release_gate_blockers(
    *,
    readiness_status: dict[str, Any],
    evidence_verification_status: dict[str, Any] | None,
    source_consistency_status: dict[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if _int(readiness_status.get("blocking_count")) > 0 or readiness_status.get("blocking_codes"):
        blockers.append(
            {
                "code": "product-readiness-blockers-present",
                "blocking_count": _int(readiness_status.get("blocking_count")),
                "blocking_codes": list(readiness_status.get("blocking_codes") or []),
            }
        )
    if _int(readiness_status.get("warning_count")) > 0 or readiness_status.get("warning_codes"):
        blockers.append(
            {
                "code": "product-readiness-warnings-present",
                "warning_count": _int(readiness_status.get("warning_count")),
                "warning_codes": list(readiness_status.get("warning_codes") or []),
            }
        )
    if evidence_verification_status and (
        evidence_verification_status.get("passed") is False
        or _int(evidence_verification_status.get("failure_count")) > 0
    ):
        blockers.append(
            {
                "code": "evidence-verification-failed",
                "failure_count": _int(evidence_verification_status.get("failure_count")),
            }
        )
    if evidence_verification_status and evidence_verification_status.get("dirty_worktree"):
        blockers.append(
            {
                "code": "evidence-generated-from-dirty-worktree",
                "dirty_worktree_counts": evidence_verification_status.get("dirty_worktree_counts") or {},
            }
        )
    release_blocker_count = _int(
        evidence_verification_status.get("release_blocker_count") if evidence_verification_status else 0
    )
    if release_blocker_count > 0:
        blockers.append(
            {
                "code": "evidence-release-blockers-present",
                "release_blocker_count": release_blocker_count,
                "release_blocker_warnings": (
                    evidence_verification_status.get("release_blocker_warnings")
                    if evidence_verification_status
                    else []
                ),
            }
        )
    if source_consistency_status.get("missing_repo_commit_roles"):
        blockers.append(
            {
                "code": "source-report-repo-commit-missing",
                "missing_repo_commit_roles": source_consistency_status.get("missing_repo_commit_roles"),
            }
        )
    elif not source_consistency_status.get("consistent", True):
        blockers.append(
            {
                "code": "source-report-repo-commit-mismatch",
                "repo_commits": source_consistency_status.get("repo_commits") or {},
            }
        )
    return blockers


def _verification_status(verification: dict[str, Any]) -> dict[str, Any] | None:
    if not verification:
        return None
    dirty = [
        item
        for item in verification.get("warnings") or []
        if isinstance(item, dict) and item.get("check") == "index_repo_worktree_dirty"
    ]
    release_blockers = [
        item
        for item in verification.get("warnings") or []
        if isinstance(item, dict) and item.get("check") == "json_artifact_release_blocker_count"
    ]
    return {
        "passed": verification.get("passed"),
        "failure_count": _int(verification.get("failure_count")),
        "warning_count": _int(verification.get("warning_count")),
        "repo_commit": verification.get("repo_commit"),
        "dirty_worktree": bool(dirty),
        "dirty_worktree_counts": dirty[0] if dirty else {},
        "release_blocker_count": sum(_int(item.get("release_blocker_count")) for item in release_blockers),
        "release_blocker_warnings": release_blockers,
    }


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


def _source_consistency_status(source_artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    repo_commits = {
        str(artifact.get("role")): artifact.get("repo_commit")
        for artifact in source_artifacts
        if isinstance(artifact.get("repo_commit"), str) and artifact.get("repo_commit")
    }
    unique_repo_commits = sorted(set(str(commit) for commit in repo_commits.values()))
    missing_roles = [
        str(artifact.get("role"))
        for artifact in source_artifacts
        if not isinstance(artifact.get("repo_commit"), str) or not artifact.get("repo_commit")
    ]
    return {
        "consistent": len(unique_repo_commits) <= 1 and not missing_roles,
        "repo_commits": repo_commits,
        "unique_repo_commits": unique_repo_commits,
        "missing_repo_commit_roles": missing_roles,
    }


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


def _int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _to_markdown(report: dict[str, Any]) -> str:
    readiness = _dict(report.get("readiness_status"))
    verification = _dict(report.get("evidence_verification_status"))
    lines = [
        "# MCP Readiness Remediation Plan",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Plan status: `{report.get('plan_status')}`",
        f"- Plan blockers: `{', '.join(report.get('plan_blockers') or []) or '-'}`",
        f"- Release gate status: `{report.get('release_gate_status')}`",
        f"- Release gate blockers: `{report.get('release_gate_blocker_count')}`",
        f"- Readiness passed: `{str(readiness.get('passed')).lower()}`",
        f"- Readiness blockers/warnings: {readiness.get('blocking_count')} / {readiness.get('warning_count')}",
        f"- Evidence verification passed: `{str(verification.get('passed')).lower()}`",
        f"- Remediation items: {report.get('remediation_item_count')}",
        "",
        "| Priority | Gate | Item | Warning Codes | Key Counts |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for item in report.get("remediation_items") or []:
        source_counts = item.get("source_counts") if isinstance(item.get("source_counts"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("priority")),
                    _md_cell(item.get("gate")),
                    _md_cell(item.get("summary")),
                    _md_cell(", ".join(item.get("warning_codes") or [])),
                    _md_cell(_compact_evidence(source_counts)),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Actions", ""])
    for item in report.get("remediation_items") or []:
        lines.extend(
            [
                f"### {item.get('priority')}. {item.get('item_id')}",
                "",
                f"- Gate: `{item.get('gate')}`",
                f"- Warning codes: `{', '.join(item.get('warning_codes') or [])}`",
                f"- Summary: {item.get('summary')}",
                f"- Action: {item.get('recommended_action')}",
                f"- Operator inputs: {', '.join(item.get('operator_inputs_required') or []) or '-'}",
                f"- Verify after: {', '.join(item.get('verification_after_remediation') or []) or '-'}",
                "",
            ]
        )
    if report.get("unmapped_warning_codes"):
        lines.extend(
            [
                "## Unmapped Warnings",
                "",
                ", ".join(report.get("unmapped_warning_codes") or []),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _compact_evidence(evidence: dict[str, Any]) -> str:
    parts = []
    for key, value in evidence.items():
        if isinstance(value, dict):
            value = {field: count for field, count in value.items() if count not in (0, "", None)}
        if value in ({}, [], "", None):
            continue
        parts.append(f"{key}={value}")
        if len(parts) >= 3:
            break
    return "; ".join(parts)


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an operator remediation plan from MCP product readiness warnings.")
    parser.add_argument("--product-readiness-report", "--readiness-report", dest="product_readiness_report", type=Path, required=True)
    parser.add_argument("--reapproval-worklist-report", type=Path)
    parser.add_argument("--evidence-verification-report", type=Path)
    parser.add_argument("--strict-public-readiness-report", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    report = build_mcp_readiness_remediation_plan(
        product_readiness_report=args.product_readiness_report,
        reapproval_worklist_report=args.reapproval_worklist_report,
        evidence_verification_report=args.evidence_verification_report,
        strict_public_readiness_report=args.strict_public_readiness_report,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
