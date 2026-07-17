from __future__ import annotations

import argparse
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

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.services.regulation_catalog_service import read_regulation_metadata
from scripts.report_metadata import current_repo_commit


PRODUCT_GATE_LABELS = {
    "parsing_accuracy": "Parsing Accuracy",
    "revision_response": "Revision Response",
    "generality": "Generality",
    "answer_accuracy": "Answer Accuracy",
    "operations": "Operations",
}
STRICT_PUBLIC_READINESS_MIN_AVERAGE_QUALITY = 98.0
FINDING_DETAILS = {
    "runtime-chunks-missing": "No preprocessed article, appendix, or supplementary chunks exist in the runtime.",
    "article-structure-missing": "No article-structured evidence exists in the runtime.",
    "article-title-gaps": "Some article chunks are missing article number or title metadata.",
    "table-human-review-required": "Some detected table chunks still require human review.",
    "parser-review-flags-present": "Approved runtime chunks still carry unacknowledged parser/table review flags.",
    "parser-goldset-f1-low": "Manual parser goldset F1 is below the required threshold.",
    "parser-goldset-f1-missing": "The required parser goldset score report is missing an overall F1.",
    "parser-goldset-quality-claim-not-ready": "The parser goldset score report is not ready to support a release-grade parsing accuracy claim.",
    "parser-goldset-scope-exclusions": "The parser goldset excludes one or more human-marked non-article/non-regulation sources from the quality-claim score.",
    "parser-goldset-score-issues": "The parser goldset score report has missing or inconsistent manual labels.",
    "parser-goldset-score-missing": "A required parser goldset score report was not provided.",
    "table-preprocessing-claim-missing": "A required table preprocessing claim gate report was not provided.",
    "table-preprocessing-claim-not-ready": "The table preprocessing claim gate is not ready to support a release-grade table parsing claim.",
    "batch-failures-present": "The preprocessing batch still has failed documents.",
    "ocr-required-present": "Some documents still require OCR or separate handling.",
    "quality-score-below-threshold": "The average preprocessing quality score is below the required threshold.",
    "batch-quality-report-missing": "No multi-document batch quality report was provided.",
    "public-readiness-review-tolerance-evidence": "The linked public readiness report uses an explicit review-tolerance threshold, so it is review-queue progress evidence rather than strict release-grade parsing evidence.",
    "vector-records-missing": "No approved vector records are available for revision-aware retrieval.",
    "approval-hash-incomplete": "Some vector records are missing approval or content-hash metadata.",
    "temporal-metadata-not-evidenced": "Effective-date or revision-history metadata is not sufficiently evidenced.",
    "temporal-metadata-partial": "Some temporal metadata present in repository chunks is missing from approved vector records.",
    "temporal-metadata-coverage-partial": "Approved vector records only partially contain temporal metadata.",
    "temporal-backfill-conflict": "Shadow temporal backfill found conflicting date metadata that must be resolved before release.",
    "temporal-backfill-vector-projection-empty": "Shadow temporal backfill produced no approved vector records for a runtime that has approved vectors.",
    "temporal-ambiguity-scope-missing": "Shadow temporal backfill found ambiguous date metadata, but no temporal ambiguity review scope report was provided.",
    "temporal-ambiguity-policy-required": "Temporal ambiguity evidence still requires explicit index and answer policy decisions before release.",
    "temporal-evidence-older-than-threshold": "Temporal evidence reports are older than the configured freshness threshold.",
    "temporal-evidence-runtime-lineage-mismatch": "Temporal evidence reports point to a different runtime lineage than the product readiness runtime.",
    "multi-document-evidence-missing": "No institution/file-format batch evidence was provided.",
    "file-format-diversity-low": "Evidence covers too few file formats across HWP/HWPX/PDF.",
    "institution-diversity-low": "Evidence covers too few public institutions.",
    "institution-profile-missing": "No institution profile evidence is available.",
    "profile-provenance-failed": "Institution profile provenance did not pass.",
    "profile-provenance-unknown": "The batch includes profile IDs that are not present in the institution profile registry.",
    "profile-provenance-warnings": "Institution profile provenance has non-blocking warnings.",
    "runtime-profile-binding-failed": "Approved runtime vectors are missing or disagree on institution profile binding.",
    "latest-only-rag-not-proven": "Approved runtime vectors do not prove complete lifecycle metadata and one latest active regulation version per regulation id.",
    "mcp-transport-history-missing": "MCP transport evidence does not include the regulation history tool required for lifecycle answers.",
    "mcp-transport-history-call-missing": "MCP transport evidence lists the history tool but does not demonstrate a successful lifecycle history call.",
    "answer-profile-coverage-low": "Answer-intent and fact metadata coverage is low.",
    "rag-answerable-ratio-low": "The benchmark answerable ratio is below the required threshold.",
    "rag-quality-warning-chunks": "Top retrieval results include chunks with quality warning flags.",
    "rag-eval-report-missing": "No RAG retrieval accuracy evaluation report was provided.",
    "mcp-demo-answer-failed": "The MCP demo answer export did not pass for all benchmark questions.",
    "mcp-demo-smoke-citations": "The MCP demo answer export cited synthetic smoke-test documents.",
    "mcp-demo-supporting-citations-missing": "One or more MCP demo answers have no supporting citation evidence.",
    "mcp-demo-no-evidence-citations": "One or more no-evidence control queries returned supporting citation evidence.",
    "mcp-demo-quality-issues": "One or more MCP demo answers contain quality issues such as metadata labels, truncated fragments, or missing citation fields.",
    "mcp-demo-answer-report-missing": "No MCP demo answer export report was provided.",
    "mcp-accuracy-comparison-failed": "The paired simple-RAG versus MCP accuracy comparison did not pass all benchmark questions.",
    "mcp-accuracy-comparison-regression": "The paired accuracy comparison found at least one query where MCP was worse than the search-only baseline.",
    "mcp-accuracy-comparison-report-missing": "No paired simple-RAG versus MCP accuracy comparison report was provided.",
    "runtime-not-fully-indexed": "Repository chunk count and approved vector count do not match.",
    "smoke-docs-in-runtime": "Synthetic smoke-test documents are present in the MCP runtime.",
    "mcp-doctor-failed": "The MCP connection readiness doctor did not pass.",
    "mcp-doctor-warnings": "The MCP connection readiness doctor still has warnings.",
    "mcp-readiness-runtime-lineage-mismatch": "The MCP connection readiness report points to a different runtime data directory than product readiness.",
    "mcp-readiness-tenant-mismatch": "The MCP connection readiness report points to a different tenant than product readiness.",
    "mcp-readiness-record-count-mismatch": "The MCP connection readiness report found a different MCP-visible record count than the approved product runtime.",
    "mcp-readiness-report-missing": "No MCP connection readiness doctor report was provided.",
    "mcp-transport-smoke-failed": "The real MCP stdio transport smoke test did not pass.",
    "mcp-transport-tenant-mismatch": "The MCP transport smoke report was executed against a different tenant than product readiness.",
    "mcp-transport-smoke-report-missing": "No real MCP transport smoke report was provided.",
    "source-report-runtime-lineage-mismatch": "A runtime-scoped product evidence report points to a different runtime data directory.",
    "source-report-tenant-mismatch": "A runtime-scoped product evidence report points to a different tenant.",
    "source-report-record-count-mismatch": "A runtime-scoped product evidence report contains counts from a different runtime snapshot.",
    "runtime-version-drift-blocker": "The runtime version drift or vector integrity audit has blocker findings.",
    "runtime-version-drift-evidence": "Approved runtime chunks or vectors were built with older parser/chunker metadata than the current code.",
    "approval-worklist-evidence-missing": "Approved runtime data exists, but no approval worklist evidence was provided.",
    "approval-review-batch-evidence-missing": "Approved runtime data exists, but no approval review batch manifest evidence was provided.",
    "approval-provenance-vector-evidence-incomplete": "Approved vector records are missing approval worklist or review-batch provenance metadata.",
    "approval-journal-vector-evidence-missing": "Approved vector records are missing matching append-only approval journal evidence.",
    "pending-approval-manual-attention": "Approval worklist evidence still contains chunks that require human attention before indexing.",
    "reapproval-review-batch-evidence-missing": "Reapproval candidates exist, but no reapproval review batch manifest evidence was provided.",
    "reapproval-review-batch-incomplete": "Reapproval review batch manifest does not cover every reapproval candidate chunk.",
    "reapproval-review-batch-manifest-blockers": "Reapproval review batch manifest has blockers or failed validation.",
    "reapproval-decision-validation-blockers": "Reapproval decision validation still has blocker findings.",
    "reapproval-decision-validation-incomplete": "Reapproval decision validation does not cover every review batch.",
    "reapproval-decision-validation-missing": "Reapproval review batches exist, but no decision validation evidence was provided.",
    "reapproval-worklist-blockers": "Reapproval worklist evidence has blockers that must be resolved before reapproval or handoff.",
    "reapproval-worklist-evidence-missing": "Approved runtime data exists, but no reapproval worklist evidence was provided.",
    "reapproval-worklist-missing": "Runtime drift requires reapproval, but no reapproval worklist evidence was provided.",
    "reapproval-worklist-review-evidence": "Reapproval worklist evidence requires initial human review before bulk reapproval.",
}
RUNTIME_SCOPED_SOURCE_REPORT_ROLES = frozenset(
    {
        "approval_worklist_report",
        "approval_review_batch_manifest_report",
        "reapproval_worklist_report",
        "reapproval_review_batch_manifest_report",
        "runtime_version_drift_report",
        "temporal_coverage_report",
        "rag_eval_report",
        "mcp_demo_answer_report",
        "accuracy_comparison_report",
    }
)
_SOURCE_REPORT_SCOPE_PATH_ALIASES = {
    "runtime_data_dir": ("runtime_data_dir", "source_runtime_data_dir", "data_dir"),
    "effective_runtime_data_dir": (
        "effective_runtime_data_dir",
        "source_effective_runtime_data_dir",
        "effective_data_dir",
    ),
}
REQUIRED_MCP_SOURCE_METADATA_FIELDS = (
    "document_id",
    "chunk_id",
    "approval_id",
    "content_hash",
    "approved_content_hash",
    "institution_name",
    "profile_id",
    "regulation_id",
    "regulation_version",
    "approval_status",
    "regulation_status",
    "effective_from",
    "effective_to",
    "repealed_at",
    "source_system",
    "source_url",
    "regulation_title",
    "source_page_start",
    "security_level",
)
ARTICLE_SCOPED_MCP_CHUNK_TYPES = ("article", "paragraph", "item", "subitem", "clause")
APPROVAL_PROVENANCE_METADATA_FIELDS = (
    "approval_id",
    "approved_content_hash",
    "approval_worklist_report_path",
    "approval_worklist_report_sha256",
    "approval_review_batch_manifest_path",
    "approval_review_batch_manifest_sha256",
    "approval_review_batch_id",
    "approval_review_batch_chunk_fingerprint",
    "approval_review_strategy",
)
APPROVAL_JOURNAL_WORKLIST_EVIDENCE_TO_METADATA = {
    "worklist_report_path": "approval_worklist_report_path",
    "worklist_report_sha256": "approval_worklist_report_sha256",
    "review_batch_manifest_path": "approval_review_batch_manifest_path",
    "review_batch_manifest_sha256": "approval_review_batch_manifest_sha256",
    "review_batch_id": "approval_review_batch_id",
    "review_batch_chunk_fingerprint": "approval_review_batch_chunk_fingerprint",
    "review_strategy": "approval_review_strategy",
}
REQUIRED_GENERALITY_FILE_TYPES = {"hwp", "hwpx", "pdf"}
GENERIC_PROFILE_IDS = {"public_institution", "default-public-institution"}
REVIEW_ATTENTION_BOOL_METADATA_KEYS = (
    "review_required",
    "table_review_required",
    "manual_review_required",
    "requires_manual_review",
)
REVIEW_ATTENTION_LIST_METADATA_KEYS = (
    "review_flags",
    "table_review_flags",
    "row_quality_flags",
    "quality_flags",
)
PARSER_UNCERTAINTY_ACK_RISKS = {"medium", "high", "critical"}
REVIEW_ATTENTION_WARNING_KEYWORDS = (
    "table",
    "row",
    "ocr",
    "mojibake",
    "encoding",
    "caption",
    "footnote",
    "endnote",
    "appendix",
    "image",
)


def build_mcp_product_readiness_audit(
    *,
    runtime_data_dir: Path,
    tenant_id: str = "default",
    profile_id: str | None = None,
    tenant_storage_isolation: bool | None = None,
    batch_reports: list[Path] | None = None,
    public_readiness_report: Path | None = None,
    parser_goldset_score_report: Path | None = None,
    parser_goldset_completion_board_report: Path | None = None,
    table_preprocessing_claim_gate_report: Path | None = None,
    rag_eval_report: Path | None = None,
    mcp_demo_answer_report: Path | None = None,
    accuracy_comparison_report: Path | None = None,
    profile_provenance_report: Path | None = None,
    mcp_readiness_report: Path | None = None,
    mcp_transport_smoke_report: Path | None = None,
    temporal_coverage_report: Path | None = None,
    temporal_backfill_shadow_report: Path | None = None,
    temporal_ambiguity_scope_report: Path | None = None,
    temporal_ambiguity_policy_decision_validation_report: Path | None = None,
    revision_impact_reports: list[Path] | None = None,
    runtime_version_drift_report: Path | None = None,
    approval_worklist_reports: list[Path] | None = None,
    approval_review_batch_reports: list[Path] | None = None,
    reapproval_worklist_reports: list[Path] | None = None,
    reapproval_review_batch_reports: list[Path] | None = None,
    reapproval_decision_validation_reports: list[Path] | None = None,
    reapproval_apply_plan_reports: list[Path] | None = None,
    min_average_quality_score: float = 98.0,
    min_parser_goldset_f1: float = 90.0,
    require_parser_goldset_score: bool = False,
    require_table_preprocessing_claim: bool = False,
    min_answerable_ratio: float = 0.8,
    require_full_index: bool = True,
    max_source_report_age_hours: float | None = None,
    strict_temporal_evidence: bool = False,
    allow_synthetic_runtime: bool = False,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    effective_isolation = _is_tenant_isolated(
        runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    effective_dir = _effective_runtime_dir(
        runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=effective_isolation,
    )
    chunks = _load_runtime_chunks(effective_dir)
    if not effective_isolation:
        chunks = _filter_chunks_for_tenant(chunks, tenant_id=tenant_id)
    vector_records = _load_vector_records(effective_dir, tenant_id=tenant_id)
    profile_scope = str(profile_id or "").strip() or None
    if profile_scope:
        chunks = _filter_records_for_profile(chunks, profile_id=profile_scope)
        vector_records = _filter_records_for_profile(vector_records, profile_id=profile_scope)
    batch_payloads = [_load_json(path) for path in batch_reports or []]
    public_readiness = _load_json(public_readiness_report) if public_readiness_report else None
    parser_goldset_score = _load_json(parser_goldset_score_report) if parser_goldset_score_report else None
    parser_goldset_completion_board = (
        _load_json(parser_goldset_completion_board_report)
        if parser_goldset_completion_board_report
        else None
    )
    table_preprocessing_claim_gate = (
        _load_json(table_preprocessing_claim_gate_report)
        if table_preprocessing_claim_gate_report
        else None
    )
    rag_eval = _load_json(rag_eval_report) if rag_eval_report else None
    mcp_demo_answers = _load_json(mcp_demo_answer_report) if mcp_demo_answer_report else None
    accuracy_comparison = _load_json(accuracy_comparison_report) if accuracy_comparison_report else None
    profile_provenance = _load_json(profile_provenance_report) if profile_provenance_report else None
    mcp_readiness = _load_json(mcp_readiness_report) if mcp_readiness_report else None
    mcp_transport_smoke = _load_json(mcp_transport_smoke_report) if mcp_transport_smoke_report else None
    temporal_coverage = _load_json(temporal_coverage_report) if temporal_coverage_report else None
    temporal_backfill_shadow = (
        _load_json(temporal_backfill_shadow_report) if temporal_backfill_shadow_report else None
    )
    temporal_ambiguity_scope = _load_json(temporal_ambiguity_scope_report) if temporal_ambiguity_scope_report else None
    temporal_ambiguity_policy_decision_validation = (
        _load_json(temporal_ambiguity_policy_decision_validation_report)
        if temporal_ambiguity_policy_decision_validation_report
        else None
    )
    revision_impacts = [_load_json(path) for path in revision_impact_reports or []]
    runtime_version_drift = _load_json(runtime_version_drift_report) if runtime_version_drift_report else None
    approval_worklists = [_load_json(path) for path in approval_worklist_reports or []]
    approval_review_batches = [_load_json(path) for path in approval_review_batch_reports or []]
    reapproval_worklists = [_load_json(path) for path in reapproval_worklist_reports or []]
    reapproval_review_batches = [_load_json(path) for path in reapproval_review_batch_reports or []]
    reapproval_decision_validations = [
        _load_json(path) for path in reapproval_decision_validation_reports or []
    ]
    reapproval_apply_plans = [_load_json(path) for path in reapproval_apply_plan_reports or []]

    generated_at = datetime.now(timezone.utc)
    source_report_artifacts = _source_report_artifacts(
        report_generated_at=generated_at,
        batch_reports=batch_reports or [],
        batch_payloads=batch_payloads,
        public_readiness_report=public_readiness_report,
        public_readiness=public_readiness,
        parser_goldset_score_report=parser_goldset_score_report,
        parser_goldset_score=parser_goldset_score,
        parser_goldset_completion_board_report=parser_goldset_completion_board_report,
        parser_goldset_completion_board=parser_goldset_completion_board,
        table_preprocessing_claim_gate_report=table_preprocessing_claim_gate_report,
        table_preprocessing_claim_gate=table_preprocessing_claim_gate,
        rag_eval_report=rag_eval_report,
        rag_eval=rag_eval,
        mcp_demo_answer_report=mcp_demo_answer_report,
        mcp_demo_answers=mcp_demo_answers,
        accuracy_comparison_report=accuracy_comparison_report,
        accuracy_comparison=accuracy_comparison,
        profile_provenance_report=profile_provenance_report,
        profile_provenance=profile_provenance,
        mcp_readiness_report=mcp_readiness_report,
        mcp_readiness=mcp_readiness,
        mcp_transport_smoke_report=mcp_transport_smoke_report,
        mcp_transport_smoke=mcp_transport_smoke,
        temporal_coverage_report=temporal_coverage_report,
        temporal_coverage=temporal_coverage,
        temporal_backfill_shadow_report=temporal_backfill_shadow_report,
        temporal_backfill_shadow=temporal_backfill_shadow,
        temporal_ambiguity_scope_report=temporal_ambiguity_scope_report,
        temporal_ambiguity_scope=temporal_ambiguity_scope,
        temporal_ambiguity_policy_decision_validation_report=(
            temporal_ambiguity_policy_decision_validation_report
        ),
        temporal_ambiguity_policy_decision_validation=temporal_ambiguity_policy_decision_validation,
        revision_impact_reports=revision_impact_reports or [],
        revision_impacts=revision_impacts,
        runtime_version_drift_report=runtime_version_drift_report,
        runtime_version_drift=runtime_version_drift,
        approval_worklist_reports=approval_worklist_reports or [],
        approval_worklists=approval_worklists,
        approval_review_batch_reports=approval_review_batch_reports or [],
        approval_review_batches=approval_review_batches,
        reapproval_worklist_reports=reapproval_worklist_reports or [],
        reapproval_worklists=reapproval_worklists,
        reapproval_review_batch_reports=reapproval_review_batch_reports or [],
        reapproval_review_batches=reapproval_review_batches,
        reapproval_decision_validation_reports=reapproval_decision_validation_reports or [],
        reapproval_decision_validations=reapproval_decision_validations,
        reapproval_apply_plan_reports=reapproval_apply_plan_reports or [],
        reapproval_apply_plans=reapproval_apply_plans,
    )
    source_report_artifact_summary = _source_report_artifact_summary(source_report_artifacts)
    temporal_evidence_guard_summary = _temporal_evidence_guard_summary(
        runtime_data_dir=runtime_data_dir,
        effective_runtime_data_dir=effective_dir,
        source_report_artifacts=source_report_artifacts,
        sources=_temporal_evidence_sources(
            temporal_coverage_report=temporal_coverage_report,
            temporal_coverage=temporal_coverage,
            temporal_backfill_shadow_report=temporal_backfill_shadow_report,
            temporal_backfill_shadow=temporal_backfill_shadow,
            temporal_ambiguity_scope_report=temporal_ambiguity_scope_report,
            temporal_ambiguity_scope=temporal_ambiguity_scope,
            temporal_ambiguity_policy_decision_validation_report=(
                temporal_ambiguity_policy_decision_validation_report
            ),
            temporal_ambiguity_policy_decision_validation=temporal_ambiguity_policy_decision_validation,
            revision_impact_reports=revision_impact_reports or [],
            revision_impacts=revision_impacts,
            runtime_version_drift_report=runtime_version_drift_report,
            runtime_version_drift=runtime_version_drift,
        ),
        max_source_report_age_hours=max_source_report_age_hours,
        strict_temporal_evidence=strict_temporal_evidence,
    )

    runtime_summary = _runtime_summary(
        chunks,
        vector_records,
        effective_dir,
        profile_id=profile_scope,
    )
    source_report_scope_summary = _source_report_scope_summary(
        source_report_artifacts=source_report_artifacts,
        runtime_data_dir=runtime_data_dir,
        effective_runtime_data_dir=effective_dir,
        tenant_id=tenant_id,
        runtime_summary=runtime_summary,
    )
    batch_summary = _batch_summary(batch_payloads)
    public_readiness_summary = _public_readiness_summary(public_readiness, source_path=public_readiness_report)
    parser_goldset_score_summary = _parser_goldset_score_summary(parser_goldset_score)
    parser_goldset_completion_board_summary = _parser_goldset_completion_board_summary(
        parser_goldset_completion_board
    )
    table_preprocessing_claim_gate_summary = _table_preprocessing_claim_gate_summary(
        table_preprocessing_claim_gate
    )
    mcp_demo_answer_summary = _mcp_demo_answer_summary(mcp_demo_answers)
    accuracy_comparison_summary = _accuracy_comparison_summary(accuracy_comparison)
    profile_provenance_summary = _profile_provenance_summary(profile_provenance)
    mcp_transport_smoke_summary = _mcp_transport_smoke_summary(mcp_transport_smoke)
    mcp_evidence_lineage_summary = _mcp_evidence_lineage_summary(
        runtime_data_dir=runtime_data_dir,
        effective_runtime_data_dir=effective_dir,
        tenant_id=tenant_id,
        runtime_summary=runtime_summary,
        mcp_readiness=mcp_readiness,
        mcp_transport_smoke=mcp_transport_smoke,
    )
    temporal_coverage_summary = _temporal_coverage_summary(temporal_coverage)
    temporal_backfill_shadow_summary = _temporal_backfill_shadow_summary(temporal_backfill_shadow)
    temporal_ambiguity_scope_summary = _temporal_ambiguity_scope_summary(temporal_ambiguity_scope)
    temporal_ambiguity_policy_decision_validation_summary = (
        _temporal_ambiguity_policy_decision_validation_summary(
            temporal_ambiguity_policy_decision_validation
        )
    )
    revision_impact_summary = _revision_impact_summary(revision_impacts)
    runtime_version_drift_summary = _runtime_version_drift_summary(runtime_version_drift)
    approval_workload_summary = _approval_workload_summary(approval_worklists)
    approval_review_batch_summary = _approval_review_batch_summary(approval_review_batches)
    reapproval_workload_summary = _reapproval_workload_summary(reapproval_worklists)
    reapproval_review_batch_summary = _reapproval_review_batch_summary(reapproval_review_batches)
    reapproval_decision_validation_summary = _reapproval_decision_validation_summary(
        reapproval_decision_validations
    )
    reapproval_apply_plan_summary = _reapproval_apply_plan_summary(reapproval_apply_plans)
    gates = {
        "parsing_accuracy": _parsing_accuracy_gate(
            runtime_summary,
            batch_summary,
            public_readiness_summary,
            parser_goldset_score_summary,
            min_average_quality_score=min_average_quality_score,
            min_parser_goldset_f1=min_parser_goldset_f1,
            require_parser_goldset_score=require_parser_goldset_score,
            table_preprocessing_claim_gate_summary=table_preprocessing_claim_gate_summary,
            require_table_preprocessing_claim=require_table_preprocessing_claim,
        ),
        "revision_response": _revision_response_gate(
            runtime_summary,
            temporal_coverage_summary,
            temporal_backfill_shadow_summary,
            temporal_ambiguity_scope_summary,
            temporal_ambiguity_policy_decision_validation_summary,
            temporal_evidence_guard_summary,
        ),
        "generality": _generality_gate(runtime_summary, batch_summary, profile_provenance_summary),
        "answer_accuracy": _answer_accuracy_gate(
            runtime_summary,
            rag_eval,
            mcp_demo_answer_summary,
            accuracy_comparison_summary,
            min_answerable_ratio=min_answerable_ratio,
        ),
        "operations": _operations_gate(
            runtime_summary,
            mcp_readiness,
            mcp_evidence_lineage_summary,
            source_report_scope_summary,
            mcp_transport_smoke_summary,
            runtime_version_drift_summary,
            approval_workload_summary,
            approval_review_batch_summary,
            reapproval_workload_summary,
            reapproval_review_batch_summary,
            reapproval_decision_validation_summary,
            reapproval_apply_plan_summary,
            require_full_index=require_full_index,
            allow_synthetic_runtime=allow_synthetic_runtime,
        ),
    }
    for gate_key, label in PRODUCT_GATE_LABELS.items():
        if gate_key in gates:
            gates[gate_key]["label"] = label
    blocking_codes = [
        item["code"]
        for gate in gates.values()
        for item in gate["findings"]
        if item["severity"] == "blocker"
    ]
    warning_codes = [
        item["code"]
        for gate in gates.values()
        for item in gate["findings"]
        if item["severity"] == "warning"
    ]
    report = {
        "report_type": "mcp_product_readiness",
        "generated_at": generated_at.isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "runtime_data_dir": str(runtime_data_dir),
        "effective_runtime_data_dir": str(effective_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_scope,
        "tenant_storage_isolation": effective_isolation,
        "source_reports": {
            "batch_reports": [str(path) for path in batch_reports or []],
            "public_readiness_report": str(public_readiness_report) if public_readiness_report else None,
            "parser_goldset_score_report": str(parser_goldset_score_report) if parser_goldset_score_report else None,
            "parser_goldset_completion_board_report": (
                str(parser_goldset_completion_board_report)
                if parser_goldset_completion_board_report
                else None
            ),
            "table_preprocessing_claim_gate_report": (
                str(table_preprocessing_claim_gate_report)
                if table_preprocessing_claim_gate_report
                else None
            ),
            "rag_eval_report": str(rag_eval_report) if rag_eval_report else None,
            "mcp_demo_answer_report": str(mcp_demo_answer_report) if mcp_demo_answer_report else None,
            "accuracy_comparison_report": str(accuracy_comparison_report) if accuracy_comparison_report else None,
            "profile_provenance_report": str(profile_provenance_report) if profile_provenance_report else None,
            "mcp_readiness_report": str(mcp_readiness_report) if mcp_readiness_report else None,
            "mcp_transport_smoke_report": str(mcp_transport_smoke_report) if mcp_transport_smoke_report else None,
            "temporal_coverage_report": str(temporal_coverage_report) if temporal_coverage_report else None,
            "temporal_backfill_shadow_report": (
                str(temporal_backfill_shadow_report) if temporal_backfill_shadow_report else None
            ),
            "temporal_ambiguity_scope_report": (
                str(temporal_ambiguity_scope_report) if temporal_ambiguity_scope_report else None
            ),
            "temporal_ambiguity_policy_decision_validation_report": (
                str(temporal_ambiguity_policy_decision_validation_report)
                if temporal_ambiguity_policy_decision_validation_report
                else None
            ),
            "revision_impact_reports": [str(path) for path in revision_impact_reports or []],
            "runtime_version_drift_report": str(runtime_version_drift_report) if runtime_version_drift_report else None,
            "approval_worklist_reports": [str(path) for path in approval_worklist_reports or []],
            "approval_review_batch_reports": [str(path) for path in approval_review_batch_reports or []],
            "reapproval_worklist_reports": [str(path) for path in reapproval_worklist_reports or []],
            "reapproval_review_batch_reports": [str(path) for path in reapproval_review_batch_reports or []],
            "reapproval_decision_validation_reports": [
                str(path) for path in reapproval_decision_validation_reports or []
            ],
            "reapproval_apply_plan_reports": [str(path) for path in reapproval_apply_plan_reports or []],
        },
        "source_report_artifacts": source_report_artifacts,
        "source_report_artifact_summary": source_report_artifact_summary,
        "source_report_scope_summary": source_report_scope_summary,
        "runtime_summary": runtime_summary,
        "batch_summary": batch_summary,
        "public_readiness_summary": public_readiness_summary,
        "parser_goldset_score_summary": parser_goldset_score_summary,
        "parser_goldset_completion_board_summary": parser_goldset_completion_board_summary,
        "table_preprocessing_claim_gate_summary": table_preprocessing_claim_gate_summary,
        "rag_eval_summary": _rag_eval_summary(rag_eval),
        "mcp_demo_answer_summary": mcp_demo_answer_summary,
        "accuracy_comparison_summary": accuracy_comparison_summary,
        "profile_provenance_summary": profile_provenance_summary,
        "mcp_readiness_summary": _mcp_readiness_summary(mcp_readiness),
        "mcp_evidence_lineage_summary": mcp_evidence_lineage_summary,
        "mcp_transport_smoke_summary": mcp_transport_smoke_summary,
        "temporal_coverage_summary": temporal_coverage_summary,
        "temporal_backfill_shadow_summary": temporal_backfill_shadow_summary,
        "temporal_ambiguity_scope_summary": temporal_ambiguity_scope_summary,
        "temporal_ambiguity_policy_decision_validation_summary": (
            temporal_ambiguity_policy_decision_validation_summary
        ),
        "temporal_evidence_guard_summary": temporal_evidence_guard_summary,
        "revision_impact_summary": revision_impact_summary,
        "runtime_version_drift_summary": runtime_version_drift_summary,
        "approval_workload_summary": approval_workload_summary,
        "approval_review_batch_summary": approval_review_batch_summary,
        "reapproval_workload_summary": reapproval_workload_summary,
        "reapproval_review_batch_summary": reapproval_review_batch_summary,
        "reapproval_decision_validation_summary": reapproval_decision_validation_summary,
        "reapproval_apply_plan_summary": reapproval_apply_plan_summary,
        "gates": gates,
        "blocking_count": len(blocking_codes),
        "blocker_count": len(blocking_codes),
        "warning_count": len(warning_codes),
        "blocking_codes": blocking_codes,
        "warning_codes": warning_codes,
        "passed": not blocking_codes,
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _effective_runtime_dir(
    runtime_data_dir: Path,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool | None,
) -> Path:
    settings = Settings(
        data_dir=runtime_data_dir,
        tenant_storage_isolation=_is_tenant_isolated(
            runtime_data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
        ),
    )
    return settings_for_tenant(settings, tenant_id).data_dir


def _is_tenant_isolated(
    runtime_data_dir: Path,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool | None,
) -> bool:
    if tenant_storage_isolation is not None:
        return tenant_storage_isolation
    return runtime_data_dir.joinpath("tenants", tenant_storage_key(tenant_id)).is_dir()


def _load_runtime_chunks(effective_dir: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    repository_dir = effective_dir / "repository"
    for path in sorted(repository_dir.glob("*_chunks.json")) if repository_dir.exists() else []:
        payload = _load_json(path)
        if isinstance(payload, list):
            chunks.extend(item for item in payload if isinstance(item, dict))
    return chunks


def _filter_chunks_for_tenant(chunks: list[dict[str, Any]], *, tenant_id: str) -> list[dict[str, Any]]:
    return [chunk for chunk in chunks if _chunk_belongs_to_tenant(chunk, tenant_id)]


def _filter_records_for_profile(records: list[dict[str, Any]], *, profile_id: str) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if str(_metadata(record).get("profile_id") or record.get("profile_id") or "").strip() == profile_id
    ]


def _chunk_belongs_to_tenant(chunk: dict[str, Any], tenant_id: str) -> bool:
    metadata = _metadata(chunk)
    chunk_tenant = str(chunk.get("tenant_id") or metadata.get("tenant_id") or "").strip()
    if tenant_id == "default":
        return chunk_tenant in {"", "default"}
    return chunk_tenant == tenant_id


def _load_vector_records(effective_dir: Path, *, tenant_id: str) -> list[dict[str, Any]]:
    vector_path = effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    return _load_jsonl(vector_path) if vector_path.is_file() else []


def _load_approval_journal_records(effective_dir: Path, *, profile_id: str | None = None) -> list[dict[str, Any]]:
    path = effective_dir / "repository" / "journals" / "approvals.jsonl"
    records = _load_jsonl(path) if path.is_file() else []
    if profile_id:
        filtered_records = _filter_records_for_profile(records, profile_id=profile_id)
        if filtered_records or any(
            str(_metadata(record).get("profile_id") or record.get("profile_id") or "").strip() for record in records
        ):
            records = filtered_records
    return records


def _runtime_summary(
    chunks: list[dict[str, Any]],
    vector_records: list[dict[str, Any]],
    effective_dir: Path,
    *,
    profile_id: str | None = None,
) -> dict[str, Any]:
    runtime_manifest_synthetic: bool | None = None
    try:
        runtime_manifest = json.loads((effective_dir / "mcp_runtime_manifest.json").read_text(encoding="utf-8-sig"))
        if isinstance(runtime_manifest, dict) and isinstance(runtime_manifest.get("synthetic_runtime"), bool):
            runtime_manifest_synthetic = runtime_manifest["synthetic_runtime"]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    chunk_type_counts = Counter(_chunk_type(chunk) for chunk in chunks)
    vector_metadata = [_metadata(record) for record in vector_records]
    chunk_metadata = [_metadata(chunk) for chunk in chunks]
    approval_journal_records = _load_approval_journal_records(effective_dir, profile_id=profile_id)
    approved_chunk_pairs = [
        (chunk, metadata)
        for chunk, metadata in zip(chunks, chunk_metadata)
        if _is_approved_chunk(chunk, metadata)
    ]
    approved_chunks = [chunk for chunk, _ in approved_chunk_pairs]
    approved_chunk_metadata = [metadata for _, metadata in approved_chunk_pairs]
    table_like_count = sum(1 for metadata in chunk_metadata if _truthy(metadata.get("table_like")))
    review_attention_reasons = [
        _review_attention_reasons(chunk, metadata)
        for chunk, metadata in zip(approved_chunks, approved_chunk_metadata)
    ]
    review_attention_flag_counts = Counter(reason for reasons in review_attention_reasons for reason in reasons)
    review_attention_sample_chunk_ids = [
        str(chunk.get("chunk_id") or metadata.get("chunk_id") or "")
        for chunk, metadata, reasons in zip(approved_chunks, approved_chunk_metadata, review_attention_reasons)
        if reasons
    ][:20]
    review_attention_chunk_keys = {
        key
        for key in (
            _metadata_key(chunk, metadata)
            for chunk, metadata, reasons in zip(approved_chunks, approved_chunk_metadata, review_attention_reasons)
            if reasons
        )
        if key
    }
    review_attention_chunk_count = len([reasons for reasons in review_attention_reasons if reasons])
    table_review_required_count = sum(
        1
        for reasons in review_attention_reasons
        if any("table_review" in reason or "row_quality" in reason for reason in reasons)
    )
    supplementary_count = sum(
        1
        for chunk, metadata in zip(chunks, chunk_metadata)
        if _chunk_type(chunk) in {"supplementary", "supplementary_provision"}
        or _truthy(metadata.get("is_supplementary_provision"))
    )
    appendix_count = sum(
        1
        for chunk, metadata in zip(chunks, chunk_metadata)
        if _chunk_type(chunk) == "appendix" or metadata.get("appendix_no") or metadata.get("appendix_title")
    )
    article_like_count = sum(1 for chunk, metadata in zip(chunks, chunk_metadata) if _chunk_type(chunk) == "article" or metadata.get("article_no"))
    article_missing_title_count = sum(
        1
        for chunk, metadata in zip(chunks, chunk_metadata)
        if _chunk_type(chunk) == "article" and not (metadata.get("article_no") and metadata.get("article_title"))
    )
    smoke_document_count = sum(
        1
        for value in [
            *(str(chunk.get("document_id") or "") for chunk in chunks),
            *(str(record.get("document_id") or _metadata(record).get("document_id") or "") for record in vector_records),
        ]
        if "doc_mcp_smoke" in value
    )
    approval_metadata_complete_count = sum(
        1
        for record, metadata in zip(vector_records, vector_metadata)
        if record.get("content_hash") and metadata.get("approval_id") and metadata.get("approved_content_hash")
    )
    approval_provenance_coverage = _approval_provenance_coverage(vector_metadata)
    approval_journal_coverage = _approval_journal_coverage(
        vector_records,
        vector_metadata,
        approval_journal_records,
    )
    runtime_chunk_keys = {
        key
        for key in (
            _metadata_key(record, metadata)
            for record, metadata in zip(vector_records, vector_metadata)
        )
        if key
    }
    runtime_tenant_ids = {
        _normalized_tenant_id(record.get("tenant_id") or metadata.get("tenant_id"))
        for record, metadata in zip(vector_records, vector_metadata)
    }
    if not runtime_tenant_ids:
        runtime_tenant_ids = {"default"}
    approval_journal_review_event_coverage = _approval_journal_review_event_coverage(
        approval_journal_records,
        runtime_chunk_keys=runtime_chunk_keys,
        runtime_tenant_ids=runtime_tenant_ids,
    )
    review_attention_acknowledgement = _review_attention_acknowledgement_summary(
        review_attention_chunk_keys,
        approval_journal_review_event_coverage,
    )
    answer_profile_count = sum(1 for metadata in vector_metadata if metadata.get("answer_profile_version"))
    repository_temporal_keys = {
        _metadata_key(chunk, metadata)
        for chunk, metadata in zip(approved_chunks, approved_chunk_metadata)
        if _metadata_key(chunk, metadata) and _has_temporal_metadata(metadata)
    }
    vector_temporal_keys = {
        _metadata_key(record, metadata)
        for record, metadata in zip(vector_records, vector_metadata)
        if _metadata_key(record, metadata) and _has_temporal_metadata(metadata)
    }
    temporal_metadata_count = len(vector_temporal_keys)
    repository_temporal_metadata_count = len(repository_temporal_keys)
    temporal_metadata_loss_count = len(repository_temporal_keys - vector_temporal_keys)
    profile_ids = {str(metadata.get("profile_id") or "") for metadata in [*chunk_metadata, *vector_metadata] if metadata.get("profile_id")}
    institution_names = {str(metadata.get("institution_name") or "") for metadata in [*chunk_metadata, *vector_metadata] if metadata.get("institution_name")}
    document_names = {str(metadata.get("document_name") or "") for metadata in [*chunk_metadata, *vector_metadata] if metadata.get("document_name")}
    profile_binding_summary = _runtime_profile_binding_summary(chunks, vector_records, chunk_metadata, vector_metadata)
    latest_only_summary = _runtime_latest_only_summary(vector_records, vector_metadata)
    return {
        "effective_runtime_dir": str(effective_dir),
        "profile_id": profile_id,
        "repository_chunk_count": len(chunks),
        "approved_repository_chunk_count": len(approved_chunks),
        "unapproved_repository_chunk_count": max(len(chunks) - len(approved_chunks), 0),
        "vector_record_count": len(vector_records),
        "full_index_match": bool(approved_chunks and len(approved_chunks) == len(vector_records)),
        "chunk_type_counts": dict(sorted(chunk_type_counts.items())),
        "article_like_count": article_like_count,
        "article_missing_title_count": article_missing_title_count,
        "appendix_count": appendix_count,
        "supplementary_count": supplementary_count,
        "table_like_count": table_like_count,
        "table_review_required_count": table_review_required_count,
        "review_attention_chunk_count": review_attention_chunk_count,
        "review_attention_flag_counts": dict(sorted(review_attention_flag_counts.items())),
        "review_attention_sample_chunk_ids": review_attention_sample_chunk_ids,
        "review_attention_chunk_keys": sorted(review_attention_chunk_keys),
        **review_attention_acknowledgement,
        "approval_metadata_complete_count": approval_metadata_complete_count,
        "approval_metadata_complete_ratio": _ratio(approval_metadata_complete_count, len(vector_records)),
        "approval_provenance_coverage": approval_provenance_coverage,
        "approval_journal_coverage": approval_journal_coverage,
        "approval_journal_review_event_coverage": approval_journal_review_event_coverage,
        "answer_profile_count": answer_profile_count,
        "answer_profile_ratio": _ratio(answer_profile_count, len(vector_records)),
        "temporal_metadata_count": temporal_metadata_count,
        "temporal_metadata_ratio": _ratio(temporal_metadata_count, len(vector_records)),
        "repository_temporal_metadata_count": repository_temporal_metadata_count,
        "repository_temporal_metadata_ratio": _ratio(repository_temporal_metadata_count, len(approved_chunks)),
        "temporal_metadata_loss_count": temporal_metadata_loss_count,
        "profile_ids": sorted(profile_ids),
        "profile_binding_summary": profile_binding_summary,
        "runtime_profile_counts": profile_binding_summary["runtime_profile_counts"],
        "runtime_unknown_profile_counts": profile_binding_summary["runtime_unknown_profile_counts"],
        "runtime_profile_mismatch_count": profile_binding_summary["runtime_profile_mismatch_count"],
        "runtime_profile_binding_passed": profile_binding_summary["runtime_profile_binding_passed"],
        "latest_only_summary": latest_only_summary,
        "regulation_lifecycle_summary": latest_only_summary,
        "institution_names": sorted(institution_names),
        "document_names": sorted(document_names),
        "smoke_document_count": smoke_document_count,
        "synthetic_runtime": runtime_manifest_synthetic,
    }


def _runtime_profile_binding_summary(
    chunks: list[dict[str, Any]],
    vector_records: list[dict[str, Any]],
    chunk_metadata: list[dict[str, Any]],
    vector_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    profile_counts = Counter(str(metadata.get("profile_id") or "missing") for metadata in vector_metadata)
    unknown_counts = Counter({key: value for key, value in profile_counts.items() if key == "missing"})
    chunk_profiles = {
        _metadata_key(chunk, metadata): str(metadata.get("profile_id") or "missing")
        for chunk, metadata in zip(chunks, chunk_metadata)
        if _metadata_key(chunk, metadata)
    }
    mismatch_count = 0
    for record, metadata in zip(vector_records, vector_metadata):
        key = _metadata_key(record, metadata)
        vector_profile = str(metadata.get("profile_id") or "missing")
        if key and key in chunk_profiles and chunk_profiles[key] != vector_profile:
            mismatch_count += 1
    return {
        "runtime_profile_counts": dict(sorted(profile_counts.items())),
        "runtime_unknown_profile_counts": dict(sorted(unknown_counts.items())),
        "runtime_profile_mismatch_count": mismatch_count,
        "runtime_profile_binding_passed": not unknown_counts and mismatch_count == 0,
    }


def _runtime_latest_only_summary(
    vector_records: list[dict[str, Any]],
    vector_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    record_samples: list[dict[str, Any]] = []
    incomplete = 0
    normalized_entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    document_groups: dict[str, list[dict[str, Any]]] = {}
    for record, metadata in zip(vector_records, vector_metadata):
        normalized_lifecycle = read_regulation_metadata({"metadata": metadata, **record})
        normalized_metadata = dict(metadata)
        for key, value in (
            ("regulation_id", normalized_lifecycle.regulation_id),
            ("regulation_version", normalized_lifecycle.version),
            ("approval_status", normalized_lifecycle.status or metadata.get("approval_status") or metadata.get("regulation_status")),
            ("regulation_status", metadata.get("regulation_status") or normalized_lifecycle.status),
            ("effective_from", normalized_lifecycle.effective_from.isoformat() if normalized_lifecycle.effective_from else None),
            ("effective_to", normalized_lifecycle.effective_to.isoformat() if normalized_lifecycle.effective_to else None),
            ("repealed_at", normalized_lifecycle.repealed_at.isoformat() if normalized_lifecycle.repealed_at else None),
        ):
            if key not in normalized_metadata or normalized_metadata.get(key) in (None, ""):
                normalized_metadata[key] = value
        normalized_entries.append((record, normalized_metadata))
        document_id = str(normalized_metadata.get("document_id") or record.get("document_id") or "").strip()
        if document_id:
            document_groups.setdefault(document_id, []).append(normalized_metadata)

    document_defaults: dict[str, dict[str, Any]] = {}
    for document_id, doc_rows in document_groups.items():
        stable_rows = [
            row
            for row in doc_rows
            if any(char.isdigit() for char in str(row.get("regulation_id") or "").strip())
        ]
        if not stable_rows:
            continue
        representative = max(
            stable_rows,
            key=lambda row: (
                _parse_runtime_date(row.get("effective_from")) or datetime.min.date(),
                str(row.get("regulation_version") or "").casefold(),
                str(row.get("chunk_id") or "").casefold(),
            ),
        )
        document_defaults[document_id] = {
            "regulation_id": str(representative.get("regulation_id") or ""),
            "regulation_version": str(representative.get("regulation_version") or ""),
            "approval_status": representative.get("approval_status") or representative.get("regulation_status"),
            "regulation_status": representative.get("regulation_status") or representative.get("approval_status"),
            "effective_from": representative.get("effective_from"),
            "effective_to": representative.get("effective_to"),
            "repealed_at": representative.get("repealed_at"),
        }

    for record, normalized_metadata in normalized_entries:
        document_id = str(normalized_metadata.get("document_id") or record.get("document_id") or "").strip()
        document_default = document_defaults.get(document_id)
        if document_default:
            regulation_id_value = str(normalized_metadata.get("regulation_id") or "").strip()
            if not regulation_id_value or not any(char.isdigit() for char in regulation_id_value):
                normalized_metadata["regulation_id"] = document_default["regulation_id"]
            version_value = str(normalized_metadata.get("regulation_version") or "").strip()
            if not version_value or not any(char.isdigit() for char in version_value):
                normalized_metadata["regulation_version"] = document_default["regulation_version"]
            if _parse_runtime_date(normalized_metadata.get("effective_from")) is None and document_default.get("effective_from") is not None:
                normalized_metadata["effective_from"] = document_default["effective_from"]
            for key in ("approval_status", "regulation_status", "effective_to", "repealed_at"):
                if key not in normalized_metadata or normalized_metadata.get(key) in (None, ""):
                    normalized_metadata[key] = document_default.get(key)
        record_samples.append(
            {
                "record_id": str(record.get("id") or ""),
                "document_id": str(record.get("document_id") or normalized_metadata.get("document_id") or ""),
                "chunk_id": str(record.get("chunk_id") or normalized_metadata.get("chunk_id") or ""),
                "profile_id": str(normalized_metadata.get("profile_id") or ""),
                "institution_name": str(normalized_metadata.get("institution_name") or ""),
                "apba_id": str(normalized_metadata.get("apba_id") or ""),
                "regulation_id": str(normalized_metadata.get("regulation_id") or ""),
                "regulation_version": str(normalized_metadata.get("regulation_version") or ""),
                "approval_status": str(normalized_metadata.get("approval_status") or ""),
                "regulation_status": str(normalized_metadata.get("regulation_status") or ""),
                "effective_from": normalized_metadata.get("effective_from"),
                "effective_to": normalized_metadata.get("effective_to"),
                "repealed_at": normalized_metadata.get("repealed_at"),
            }
        )
        required = ("regulation_id", "regulation_version", "effective_from", "effective_to", "repealed_at")
        if any(field not in normalized_metadata for field in required) or normalized_metadata.get("effective_from") in (None, ""):
            incomplete += 1
            continue
        raw_lifecycle_status = normalized_metadata.get("regulation_status") or normalized_metadata.get("approval_status")
        if raw_lifecycle_status in (None, ""):
            incomplete += 1
            continue
        lifecycle_status = str(raw_lifecycle_status).casefold()
        if lifecycle_status != "approved":
            continue
        regulation_id = str(normalized_metadata.get("regulation_id") or "").strip()
        if regulation_id:
            groups.setdefault(regulation_id.casefold(), []).append((record, normalized_metadata))
    duplicate_groups = 0
    selected_keys: set[tuple[str, str]] = set()
    eligible_keys: set[tuple[str, str]] = set()
    as_of = datetime.now(timezone.utc).date()
    lifecycle_identity_present = False
    for regulation_id, group in groups.items():
        active = []
        for record, metadata in group:
            if any(str(metadata.get(field) or "").strip() for field in ("regulation_id", "regulation_version", "effective_from")):
                lifecycle_identity_present = True
            effective_from = _parse_runtime_date(metadata.get("effective_from"))
            effective_to = _parse_runtime_date(metadata.get("effective_to"))
            repealed_at = _parse_runtime_date(metadata.get("repealed_at"))
            if effective_from is None or (
                metadata.get("effective_to") not in (None, "") and effective_to is None
            ) or (
                metadata.get("repealed_at") not in (None, "") and repealed_at is None
            ):
                incomplete += 1
                continue
            if effective_from > as_of:
                continue
            if effective_to is not None and as_of > effective_to:
                continue
            if repealed_at is not None and as_of >= repealed_at:
                continue
            active.append((record, metadata, effective_from))
        versions = {}
        for record, metadata, _ in active:
            version = str(metadata.get("regulation_version") or "").casefold()
            versions.setdefault(version, set()).add(str(record.get("document_id") or metadata.get("document_id") or ""))
        duplicate_groups += sum(1 for documents in versions.values() if len({value for value in documents if value}) > 1)
        if not active:
            continue
        winner = max(active, key=lambda item: (item[2], str(item[1].get("regulation_version") or ""), str(item[0].get("document_id") or "")))
        winner_version = str(winner[1].get("regulation_version") or "").casefold()
        for record, metadata, _ in active:
            key = (str(record.get("document_id") or metadata.get("document_id") or ""), str(record.get("chunk_id") or metadata.get("chunk_id") or ""))
            eligible_keys.add(key)
            if str(metadata.get("regulation_version") or "").casefold() == winner_version:
                selected_keys.add(key)
    legacy_runtime_without_regulation_metadata = bool(vector_records) and not lifecycle_identity_present
    latest_only_passed = bool(vector_records) and not duplicate_groups and (not eligible_keys or bool(selected_keys))
    if not legacy_runtime_without_regulation_metadata:
        latest_only_passed = latest_only_passed and not incomplete
    return {
        "lifecycle_complete_count": max(len(vector_records) - incomplete, 0),
        "lifecycle_incomplete_count": incomplete,
        "regulation_group_count": len(groups),
        "duplicate_active_version_group_count": duplicate_groups,
        "latest_selected_record_count": len(selected_keys),
        "non_latest_record_count": max(len(eligible_keys) - len(selected_keys), 0),
        "legacy_runtime_without_regulation_metadata": legacy_runtime_without_regulation_metadata,
        "latest_only_passed": latest_only_passed,
        "as_of_date": as_of.isoformat(),
        "record_samples": record_samples[:50],
    }


def _parse_runtime_date(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _has_temporal_metadata(metadata: dict[str, Any]) -> bool:
    return bool(
        metadata.get("revision_date")
        or metadata.get("effective_date")
        or metadata.get("revision_history")
        or metadata.get("valid_from")
        or metadata.get("valid_to")
        or metadata.get("supplementary_identifier_date")
        or metadata.get("article_effective_overrides")
        or metadata.get("article_validity_windows")
    )


def _metadata_key(item: dict[str, Any], metadata: dict[str, Any]) -> str:
    document_id = str(item.get("document_id") or metadata.get("document_id") or "")
    chunk_id = str(item.get("chunk_id") or metadata.get("chunk_id") or "")
    return f"{document_id}:{chunk_id}" if document_id and chunk_id else ""


def _approval_provenance_coverage(vector_metadata: list[dict[str, Any]]) -> dict[str, Any]:
    field_counts: Counter[str] = Counter()
    complete_record_count = 0
    for metadata in vector_metadata:
        present_fields = {
            field
            for field in APPROVAL_PROVENANCE_METADATA_FIELDS
            if str(metadata.get(field) or "").strip()
        }
        field_counts.update(present_fields)
        if len(present_fields) == len(APPROVAL_PROVENANCE_METADATA_FIELDS):
            complete_record_count += 1
    record_count = len(vector_metadata)
    normalized_field_counts = {
        field: int(field_counts.get(field, 0)) for field in APPROVAL_PROVENANCE_METADATA_FIELDS
    }
    return {
        "record_count": record_count,
        "required_fields": list(APPROVAL_PROVENANCE_METADATA_FIELDS),
        "field_counts": normalized_field_counts,
        "missing_field_counts": {
            field: max(record_count - count, 0) for field, count in normalized_field_counts.items()
        },
        "complete_record_count": complete_record_count,
        "complete_ratio": _ratio(complete_record_count, record_count),
    }


def _approval_journal_coverage(
    vector_records: list[dict[str, Any]],
    vector_metadata: list[dict[str, Any]],
    approval_journal_records: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible_record_count = 0
    matched_record_count = 0
    missing_sample_record_ids: list[str] = []
    for record, metadata in zip(vector_records, vector_metadata):
        if not _has_complete_approval_provenance(metadata):
            continue
        eligible_record_count += 1
        if _has_matching_approval_journal_record(
            approval_journal_records,
            vector_record=record,
            metadata=metadata,
        ):
            matched_record_count += 1
            continue
        if len(missing_sample_record_ids) < 20:
            missing_sample_record_ids.append(
                str(record.get("id") or f"{metadata.get('document_id')}:{metadata.get('chunk_id')}")
            )
    missing_record_count = max(eligible_record_count - matched_record_count, 0)
    return {
        "journal_record_count": len(approval_journal_records),
        "record_count": len(vector_records),
        "eligible_record_count": eligible_record_count,
        "matched_record_count": matched_record_count,
        "missing_record_count": missing_record_count,
        "matched_ratio": _ratio(matched_record_count, eligible_record_count),
        "missing_sample_record_ids": missing_sample_record_ids,
    }


def _approval_journal_review_event_coverage(
    approval_journal_records: list[dict[str, Any]],
    *,
    runtime_chunk_keys: set[str] | None = None,
    runtime_tenant_ids: set[str] | None = None,
) -> dict[str, Any]:
    expected_event_types = ("approved", "human_review_confirmed", "ai_review_confirmed")
    superseded_record_ids = _superseded_approval_record_ids(approval_journal_records)
    active_records = [
        record
        for record in approval_journal_records
        if _approval_record_key(record) not in superseded_record_ids
    ]
    runtime_chunk_keys = set(runtime_chunk_keys or set())
    runtime_tenant_ids = {_normalized_tenant_id(value) for value in runtime_tenant_ids or {"default"}}
    chunk_reference_count = 0
    event_chunk_counts: Counter[str] = Counter()
    expected_chunk_counts: Counter[str] = Counter()
    missing_chunk_counts: Counter[str] = Counter()
    incomplete_record_count = 0
    incomplete_samples: list[dict[str, Any]] = []
    applicable_record_count = 0
    scoped_out_record_count = 0
    complete_event_chunk_keys_by_type: dict[str, set[str]] = {event_type: set() for event_type in expected_event_types}
    for record in active_records:
        if not isinstance(record, dict):
            continue
        record_tenant = _normalized_tenant_id(record.get("tenant_id"))
        if record_tenant not in runtime_tenant_ids:
            scoped_out_record_count += 1
            continue
        events = record.get("review_decision_events")
        if not isinstance(events, list) and not (
            record.get("ai_review_confirmed") is not None or record.get("human_review_confirmed") is not None
        ):
            continue
        record_document_id = str(record.get("document_id") or "").strip()
        chunk_ids = {
            str(value).strip()
            for value in record.get("chunk_ids") or []
            if str(value).strip()
            and (
                not runtime_chunk_keys
                or _document_chunk_key(record_document_id, str(value).strip()) in runtime_chunk_keys
            )
        }
        if runtime_chunk_keys and not chunk_ids:
            scoped_out_record_count += 1
            continue
        applicable_record_count += 1
        chunk_count = len(chunk_ids)
        chunk_reference_count += chunk_count
        event_chunks_by_type: dict[str, set[str]] = {event_type: set() for event_type in expected_event_types}
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            event_name = str(event.get("event") or "").strip()
            chunk_id = str(event.get("chunk_id") or "").strip()
            if event_name in event_chunks_by_type and chunk_id in chunk_ids:
                event_chunks_by_type[event_name].add(chunk_id)
        record_missing: dict[str, int] = {}
        required_event_types = ["approved"]
        if bool(record.get("human_review_confirmed")):
            required_event_types.append("human_review_confirmed")
        if bool(record.get("ai_review_confirmed")):
            required_event_types.append("ai_review_confirmed")
        for event_type in required_event_types:
            expected_chunk_counts[event_type] += chunk_count
            observed = len(event_chunks_by_type[event_type])
            event_chunk_counts[event_type] += observed
            missing = max(chunk_count - observed, 0)
            missing_chunk_counts[event_type] += missing
            if missing:
                record_missing[event_type] = missing
            else:
                for chunk_id in chunk_ids:
                    chunk_key = _document_chunk_key(record_document_id, chunk_id)
                    if chunk_key:
                        complete_event_chunk_keys_by_type[event_type].add(chunk_key)
        if record_missing:
            incomplete_record_count += 1
            if len(incomplete_samples) < 10:
                incomplete_samples.append(
                    {
                        "approval_id": str(record.get("approval_id") or ""),
                        "document_id": str(record.get("document_id") or ""),
                        "chunk_count": chunk_count,
                        "missing_event_chunks": record_missing,
                    }
                )
    return {
        "journal_record_count": len(approval_journal_records),
        "active_journal_record_count": len(active_records),
        "superseded_record_count": len(superseded_record_ids),
        "scoped_out_record_count": scoped_out_record_count,
        "applicable_record_count": applicable_record_count,
        "chunk_reference_count": chunk_reference_count,
        "runtime_chunk_key_count": len(runtime_chunk_keys),
        "runtime_tenant_ids": sorted(runtime_tenant_ids),
        "review_decision_event_count": sum(
            len(record.get("review_decision_events") or [])
            for record in approval_journal_records
            if isinstance(record, dict) and isinstance(record.get("review_decision_events"), list)
        ),
        "expected_event_chunk_counts": dict(sorted(expected_chunk_counts.items())),
        "event_chunk_counts": dict(sorted(event_chunk_counts.items())),
        "missing_event_chunk_counts": dict(sorted(missing_chunk_counts.items())),
        "complete_event_chunk_keys_by_type": {
            event_type: sorted(chunk_keys)
            for event_type, chunk_keys in sorted(complete_event_chunk_keys_by_type.items())
        },
        "incomplete_record_count": incomplete_record_count,
        "incomplete_samples": incomplete_samples,
    }


def _review_attention_acknowledgement_summary(
    review_attention_chunk_keys: set[str],
    approval_journal_review_event_coverage: dict[str, Any],
) -> dict[str, Any]:
    required_event_types = ("approved", "human_review_confirmed", "ai_review_confirmed")
    review_attention_chunk_count = len(review_attention_chunk_keys)
    if review_attention_chunk_count <= 0:
        return {
            "review_attention_acknowledgement_complete": True,
            "review_attention_acknowledged_chunk_count": 0,
            "review_attention_unacknowledged_chunk_count": 0,
            "review_attention_acknowledgement_required_event_types": list(required_event_types),
            "review_attention_acknowledged_chunk_keys": [],
            "review_attention_unacknowledged_chunk_keys": [],
        }
    coverage = (
        approval_journal_review_event_coverage
        if isinstance(approval_journal_review_event_coverage, dict)
        else {}
    )
    expected_counts = (
        coverage.get("expected_event_chunk_counts")
        if isinstance(coverage.get("expected_event_chunk_counts"), dict)
        else {}
    )
    complete_chunk_keys_by_type = (
        coverage.get("complete_event_chunk_keys_by_type")
        if isinstance(coverage.get("complete_event_chunk_keys_by_type"), dict)
        else {}
    )
    acknowledged_chunk_keys = set(review_attention_chunk_keys)
    for event_type in required_event_types:
        event_chunk_keys = complete_chunk_keys_by_type.get(event_type)
        if not isinstance(event_chunk_keys, list):
            event_chunk_keys = []
        acknowledged_chunk_keys &= {str(chunk_key) for chunk_key in event_chunk_keys}
    complete = (
        _int(coverage.get("applicable_record_count")) > 0
        and len(acknowledged_chunk_keys) == review_attention_chunk_count
        and all(_int(expected_counts.get(event_type)) >= len(acknowledged_chunk_keys) for event_type in required_event_types)
    )
    acknowledged_count = len(acknowledged_chunk_keys)
    return {
        "review_attention_acknowledgement_complete": complete,
        "review_attention_acknowledged_chunk_count": acknowledged_count,
        "review_attention_unacknowledged_chunk_count": review_attention_chunk_count - acknowledged_count,
        "review_attention_acknowledgement_required_event_types": list(required_event_types),
        "review_attention_acknowledged_chunk_keys": sorted(acknowledged_chunk_keys),
        "review_attention_unacknowledged_chunk_keys": sorted(review_attention_chunk_keys - acknowledged_chunk_keys),
    }


def _document_chunk_key(document_id: str, chunk_id: str) -> str:
    return f"{document_id}:{chunk_id}" if document_id and chunk_id else ""


def _normalized_tenant_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized or "default"


def _approval_record_key(record: dict[str, Any]) -> str:
    return str(record.get("approval_record_id") or record.get("approval_id") or "").strip()


def _superseded_approval_record_ids(records: list[dict[str, Any]]) -> set[str]:
    superseded: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_values: list[Any] = []
        for field in (
            "supersedes_approval_record_ids",
            "superseded_approval_record_ids",
            "supersedes_approval_records",
        ):
            value = record.get(field)
            if isinstance(value, list):
                raw_values.extend(value)
        for field in ("supersedes_approval_record_id", "superseded_approval_record_id"):
            if record.get(field):
                raw_values.append(record.get(field))
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized:
                superseded.add(normalized)
    return superseded


def _has_complete_approval_provenance(metadata: dict[str, Any]) -> bool:
    return all(str(metadata.get(field) or "").strip() for field in APPROVAL_PROVENANCE_METADATA_FIELDS)


def _has_matching_approval_journal_record(
    records: list[dict[str, Any]],
    *,
    vector_record: dict[str, Any],
    metadata: dict[str, Any],
) -> bool:
    document_id = str(vector_record.get("document_id") or metadata.get("document_id") or "").strip()
    chunk_id = str(vector_record.get("chunk_id") or metadata.get("chunk_id") or "").strip()
    tenant_id = str(vector_record.get("tenant_id") or metadata.get("tenant_id") or "default").strip()
    approval_id = str(metadata.get("approval_id") or "").strip()
    approved_content_hash = str(metadata.get("approved_content_hash") or "").strip()
    if not all((document_id, chunk_id, tenant_id, approval_id, approved_content_hash)):
        return False
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("document_id") or "").strip() != document_id:
            continue
        if str(record.get("tenant_id") or "").strip() != tenant_id:
            continue
        if str(record.get("approval_id") or "").strip() != approval_id:
            continue
        if chunk_id not in {str(value).strip() for value in record.get("chunk_ids") or []}:
            continue
        if _approval_journal_record_chunk_hash(record, chunk_id) != approved_content_hash:
            continue
        evidence_metadata = _approval_journal_worklist_metadata(record.get("worklist_evidence"))
        if set(evidence_metadata) != set(APPROVAL_JOURNAL_WORKLIST_EVIDENCE_TO_METADATA.values()):
            continue
        if any(str(metadata.get(key) or "").strip() != str(value or "").strip() for key, value in evidence_metadata.items()):
            continue
        return True
    return False


def _approval_journal_record_chunk_hash(record: dict[str, Any], chunk_id: str) -> str:
    approved_hashes = record.get("approved_content_hashes")
    if isinstance(approved_hashes, dict):
        value = approved_hashes.get(chunk_id)
        if value:
            return str(value).strip()
    for item in record.get("approved_chunks") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("chunk_id") or "").strip() == chunk_id and item.get("approved_content_hash"):
            return str(item.get("approved_content_hash") or "").strip()
    return ""


def _approval_journal_worklist_metadata(value: Any) -> dict[str, str]:
    evidence = value if isinstance(value, dict) else {}
    return {
        metadata_key: str(evidence.get(evidence_key) or "").strip()
        for evidence_key, metadata_key in APPROVAL_JOURNAL_WORKLIST_EVIDENCE_TO_METADATA.items()
        if str(evidence.get(evidence_key) or "").strip()
    }


def _review_attention_reasons(chunk: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in REVIEW_ATTENTION_BOOL_METADATA_KEYS:
        if _truthy(metadata.get(key)):
            reasons.append(key)
    for key in REVIEW_ATTENTION_LIST_METADATA_KEYS:
        values = metadata.get(key)
        if isinstance(values, str) and values.strip():
            reasons.append(f"{key}:{values.strip()}")
        elif isinstance(values, list):
            for value in values:
                if str(value or "").strip():
                    reasons.append(f"{key}:{str(value).strip()}")
    parser_uncertainty = metadata.get("parser_uncertainty") if isinstance(metadata.get("parser_uncertainty"), dict) else {}
    uncertainty_risk = str(
        metadata.get("parser_uncertainty_risk_level") or parser_uncertainty.get("risk_level") or ""
    ).strip().lower()
    if uncertainty_risk in PARSER_UNCERTAINTY_ACK_RISKS:
        reasons.append(f"parser_uncertainty_risk_level:{uncertainty_risk}")
        uncertainty_flags = metadata.get("parser_uncertainty_flags", parser_uncertainty.get("flags", []))
        if isinstance(uncertainty_flags, str):
            uncertainty_values = [uncertainty_flags]
        else:
            uncertainty_values = (
                list(uncertainty_flags) if isinstance(uncertainty_flags, (list, tuple, set)) else []
            )
        for value in uncertainty_values:
            flag = str(value or "").strip()
            if flag:
                reasons.append(f"parser_uncertainty_flags:{flag}")
        recommendation = str(
            metadata.get("parser_uncertainty_recommendation") or parser_uncertainty.get("recommendation") or ""
        ).strip()
        if recommendation and recommendation != "none":
            reasons.append(f"parser_uncertainty_recommendation:{recommendation}")
    for warning in chunk.get("warnings") or []:
        warning_text = str(warning or "").strip()
        if warning_text and _warning_requires_review(warning_text):
            reasons.append(f"warning:{warning_text}")
    return sorted(dict.fromkeys(reasons))


def _warning_requires_review(warning: str) -> bool:
    normalized = warning.lower()
    return any(keyword in normalized for keyword in REVIEW_ATTENTION_WARNING_KEYWORDS)


def _batch_summary(batch_reports: list[Any]) -> dict[str, Any]:
    rows = [row for report in batch_reports if isinstance(report, dict) for row in report.get("rows", []) or [] if isinstance(row, dict)]
    successful_count = sum(_int(report.get("successful_count")) for report in batch_reports if isinstance(report, dict))
    failed_count = sum(_int(report.get("failed_count")) for report in batch_reports if isinstance(report, dict))
    ocr_required_count = sum(_int(report.get("ocr_required_count")) for report in batch_reports if isinstance(report, dict))
    quality_scores = [
        float(row.get("quality_score"))
        for row in rows
        if not isinstance(row.get("quality_score"), bool) and row.get("quality_score") not in (None, "")
    ]
    average_quality_score = (
        round(sum(quality_scores) / len(quality_scores), 3)
        if quality_scores
        else _first_numeric(batch_reports, "average_quality_score")
    )
    quality_passed_count = sum(1 for row in rows if row.get("quality_passed") is True)
    table_attention_total = sum(
        _int(report.get("table_false_positive_attention_total"))
        + _int(report.get("table_extraction_failed_attention_total"))
        for report in batch_reports
        if isinstance(report, dict)
    )
    file_type_counts = Counter(str(row.get("file_type") or Path(str(row.get("filename") or "")).suffix.lower().lstrip(".") or "unknown") for row in rows)
    profile_counts = Counter(str(row.get("profile_id") or "missing") for row in rows)
    institution_counts = Counter(str(row.get("institution_name") or "missing") for row in rows)
    apba_id_counts = Counter(str(row.get("apba_id") or "missing") for row in rows)
    return {
        "report_count": len(batch_reports),
        "row_count": len(rows),
        "successful_count": successful_count,
        "failed_count": failed_count,
        "ocr_required_count": ocr_required_count,
        "quality_passed_count": quality_passed_count,
        "average_quality_score": average_quality_score,
        "table_attention_total": table_attention_total,
        "file_type_counts": dict(sorted(file_type_counts.items())),
        "profile_counts": dict(sorted(profile_counts.items())),
        "apba_id_count": len([key for key in apba_id_counts if key != "missing"]),
        "apba_id_counts": dict(sorted(apba_id_counts.items())),
        "institution_count": len([key for key in institution_counts if key != "missing"]),
        "institution_counts": dict(sorted(institution_counts.items())),
    }


def _parser_goldset_score_detail(summary: dict[str, Any], message: str) -> str:
    blocking_issue_codes = summary.get("blocking_issue_codes")
    if not isinstance(blocking_issue_codes, dict):
        blocking_issue_codes = {}
    blocking_issue_detail = ", ".join(
        f"{code}={_int(count)}"
        for code, count in sorted(blocking_issue_codes.items())
        if _int(count) > 0
    )
    evidence = [
        f"pending_document_count={_int(summary.get('pending_document_count'))}",
        f"missing_structure_score_count={_int(summary.get('missing_structure_score_count'))}",
        f"issue_count={_int(summary.get('issue_count'))}",
        (
            "overall_f1=missing"
            if summary.get("overall_f1") is None
            else f"overall_f1={summary.get('overall_f1')}"
        ),
    ]
    if blocking_issue_detail:
        evidence.append(f"blocking_issue_codes={blocking_issue_detail}")
    return f"{message} Evidence: {', '.join(evidence)}."


def _parsing_accuracy_gate(
    runtime_summary: dict[str, Any],
    batch_summary: dict[str, Any],
    public_readiness_summary: dict[str, Any],
    parser_goldset_score_summary: dict[str, Any],
    *,
    min_average_quality_score: float,
    min_parser_goldset_f1: float,
    require_parser_goldset_score: bool = False,
    table_preprocessing_claim_gate_summary: dict[str, Any] | None = None,
    require_table_preprocessing_claim: bool = False,
) -> dict[str, Any]:
    findings = []
    if runtime_summary["repository_chunk_count"] <= 0:
        findings.append(_finding("blocker", "runtime-chunks-missing", "전처리된 조항/표/부칙 청크가 없습니다."))
    if runtime_summary["article_like_count"] <= 0:
        findings.append(_finding("blocker", "article-structure-missing", "조항 구조화 근거가 없습니다."))
    if runtime_summary["article_missing_title_count"] > 0:
        findings.append(_finding("warning", "article-title-gaps", "일부 article 청크에 조번호 또는 제목 메타데이터가 없습니다."))
    unacknowledged_review_attention_count = int(
        runtime_summary.get("review_attention_unacknowledged_chunk_count") or 0
    )
    if unacknowledged_review_attention_count > 0:
        findings.append(
            _finding(
                "warning",
                "parser-review-flags-present",
                (
                    "Parser/table review flags remain unacknowledged in approved runtime chunks. "
                    f"review_attention_chunk_count={runtime_summary.get('review_attention_chunk_count')}, "
                    f"acknowledged_chunk_count={runtime_summary.get('review_attention_acknowledged_chunk_count')}, "
                    f"unacknowledged_chunk_count={unacknowledged_review_attention_count}."
                ),
                static_detail=False,
            )
        )
    if batch_summary["report_count"]:
        if batch_summary["failed_count"] > 0:
            findings.append(_finding("blocker", "batch-failures-present", "전처리 실패 문서가 남아 있습니다."))
        if batch_summary["ocr_required_count"] > 0:
            findings.append(_finding("warning", "ocr-required-present", "OCR 또는 별도 처리 필요한 문서가 있습니다."))
        if float(batch_summary.get("average_quality_score") or 0.0) < min_average_quality_score:
            findings.append(_finding("blocker", "quality-score-below-threshold", "평균 전처리 품질 점수가 기준보다 낮습니다."))
    else:
        findings.append(_finding("warning", "batch-quality-report-missing", "다수 문서 batch 품질 리포트가 연결되지 않았습니다."))
    if public_readiness_summary and not public_readiness_summary.get("passed"):
        failed_checks = ", ".join(public_readiness_summary.get("failed_checks") or [])
        findings.append(
            _finding(
                "blocker",
                "public-batch-readiness-failed",
                f"Public batch readiness did not pass. Failed checks: {failed_checks or 'unknown'}.",
            )
        )
    if public_readiness_summary.get("review_tolerance_evidence"):
        findings.append(
            _finding(
                "warning",
                "public-readiness-review-tolerance-evidence",
                _public_readiness_review_tolerance_detail(public_readiness_summary),
                static_detail=False,
            )
        )
    if require_parser_goldset_score and not parser_goldset_score_summary:
        findings.append(
            _finding(
                "blocker",
                "parser-goldset-score-missing",
                "Parser goldset score report is required for this readiness profile.",
            )
        )
    if parser_goldset_score_summary:
        if parser_goldset_score_summary.get("ready_for_quality_claim") is not True:
            findings.append(
                _finding(
                    "blocker" if require_parser_goldset_score else "warning",
                    "parser-goldset-quality-claim-not-ready",
                    _parser_goldset_score_detail(
                        parser_goldset_score_summary,
                        "Parser goldset score report is not ready for a release-grade parsing accuracy claim.",
                    ),
                    static_detail=False,
                )
            )
        if int(parser_goldset_score_summary.get("issue_count") or 0) > 0:
            findings.append(
                _finding(
                    "blocker" if require_parser_goldset_score else "warning",
                    "parser-goldset-score-issues",
                    _parser_goldset_score_detail(
                        parser_goldset_score_summary,
                        (
                            "Parser goldset score report has issues that should be fixed before using it as "
                            "a release-grade accuracy claim."
                        ),
                    ),
                    static_detail=False,
                )
            )
        if int(parser_goldset_score_summary.get("excluded_document_count") or 0) > 0:
            findings.append(
                _finding(
                    "warning",
                    "parser-goldset-scope-exclusions",
                    "Parser goldset excludes human-marked non-article/non-regulation sources from the quality-claim score.",
                )
            )
        f1 = parser_goldset_score_summary.get("overall_f1")
        if require_parser_goldset_score and f1 is None:
            findings.append(
                _finding(
                    "blocker",
                    "parser-goldset-f1-missing",
                    _parser_goldset_score_detail(
                        parser_goldset_score_summary,
                        "Parser goldset score report must include an overall F1 when the goldset gate is required.",
                    ),
                    static_detail=False,
                )
            )
        if f1 is not None and float(f1) < min_parser_goldset_f1:
            findings.append(
                _finding(
                    "blocker",
                    "parser-goldset-f1-low",
                    f"Parser goldset F1 {f1} is below required threshold {min_parser_goldset_f1}.",
                )
            )
    table_claim = table_preprocessing_claim_gate_summary or {}
    if require_table_preprocessing_claim and not table_claim:
        findings.append(
            _finding(
                "blocker",
                "table-preprocessing-claim-missing",
                "Table preprocessing claim gate report is required for this readiness profile.",
            )
        )
    if table_claim and (
        table_claim.get("passed") is not True
        or table_claim.get("status") not in {"ready", "ready_for_table_quality_claim"}
        or table_claim.get("ready_for_table_score_transfer") is not True
        or int(table_claim.get("blocker_count") or 0) > 0
        or int(table_claim.get("pending_unit_count") or 0) > 0
        or int(table_claim.get("invalid_unit_count") or 0) > 0
        or table_claim.get("transfer_passed") is not True
        or int(table_claim.get("transfer_blocker_count") or 0) > 0
        or table_claim.get("source_traceability_passed") is not True
        or int(table_claim.get("source_traceability_issue_count") or 0) > 0
        or (
            require_table_preprocessing_claim
            and table_claim.get("source_traceability_require_page_count_verification") is not True
        )
        or (require_table_preprocessing_claim and table_claim.get("drift_check_present") is not True)
        or (require_table_preprocessing_claim and table_claim.get("drift_check_passed") is not True)
        or int(table_claim.get("drift_check_blocker_count") or 0) > 0
        or int(table_claim.get("table_answer_blocker_count") or 0) > 0
    ):
        findings.append(
            _finding(
                "blocker" if require_table_preprocessing_claim else "warning",
                "table-preprocessing-claim-not-ready",
                (
                    "Table preprocessing claim gate is not ready for a release-grade table parsing claim. "
                    f"status={table_claim.get('status') or 'unknown'}, "
                    f"feasibility_status={table_claim.get('feasibility_status') or 'unknown'}, "
                    f"pending_unit_count={int(table_claim.get('pending_unit_count') or 0)}, "
                    f"invalid_unit_count={int(table_claim.get('invalid_unit_count') or 0)}, "
                    f"required_field_missing_total={int(table_claim.get('required_field_missing_total') or 0)}, "
                    f"review_priority_counts={table_claim.get('review_priority_counts') or {}}, "
                    f"label_review_flag_counts={table_claim.get('label_review_flag_counts') or {}}, "
                    f"transfer_blocker_count={int(table_claim.get('transfer_blocker_count') or 0)}, "
                    f"source_traceability_issue_count={int(table_claim.get('source_traceability_issue_count') or 0)}, "
                    f"source_traceability_issue_counts={table_claim.get('source_traceability_issue_counts') or {}}, "
                    f"source_traceability_require_page_count_verification={str(table_claim.get('source_traceability_require_page_count_verification') is True).lower()}, "
                    f"drift_check_present={str(table_claim.get('drift_check_present') is True).lower()}, "
                    f"drift_check_passed={str(table_claim.get('drift_check_passed') is True).lower()}, "
                    f"drift_check_blocker_count={int(table_claim.get('drift_check_blocker_count') or 0)}, "
                    f"table_answer_blocker_count={int(table_claim.get('table_answer_blocker_count') or 0)}."
                ),
                static_detail=False,
            )
        )
    return _gate("파싱 정확도", findings)


def _revision_response_gate(
    runtime_summary: dict[str, Any],
    temporal_coverage_summary: dict[str, Any] | None = None,
    temporal_backfill_shadow_summary: dict[str, Any] | None = None,
    temporal_ambiguity_scope_summary: dict[str, Any] | None = None,
    temporal_ambiguity_policy_decision_validation_summary: dict[str, Any] | None = None,
    temporal_evidence_guard_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings = []
    if runtime_summary["vector_record_count"] <= 0:
        findings.append(_finding("blocker", "vector-records-missing", "개정 비교의 기준이 되는 색인 레코드가 없습니다."))
    if runtime_summary["approval_metadata_complete_ratio"] < 1.0:
        findings.append(_finding("blocker", "approval-hash-incomplete", "일부 Vector 레코드에 approval/content hash가 없습니다."))
    latest_only_summary = runtime_summary.get("latest_only_summary") if isinstance(runtime_summary.get("latest_only_summary"), dict) else {}
    if runtime_summary["vector_record_count"] > 0 and latest_only_summary.get("latest_only_passed") is not True:
        findings.append(
            _finding(
                "blocker",
                "latest-only-rag-not-proven",
                "Latest-only RAG evidence failed: "
                f"lifecycle_incomplete_count={_int(latest_only_summary.get('lifecycle_incomplete_count'))}, "
                f"duplicate_active_version_group_count={_int(latest_only_summary.get('duplicate_active_version_group_count'))}, "
                f"non_latest_record_count={_int(latest_only_summary.get('non_latest_record_count'))}, "
                f"as_of_date={latest_only_summary.get('as_of_date') or 'unknown'}.",
                static_detail=False,
            )
        )
    if temporal_coverage_summary and temporal_coverage_summary.get("latest_only_passed") is not True:
        findings.append(
            _finding(
                "blocker",
                "latest-only-rag-not-proven",
                "Temporal metadata coverage report did not pass its lifecycle/latest-only selection audit.",
                static_detail=False,
            )
        )
    if runtime_summary["repository_temporal_metadata_count"] <= 0 and runtime_summary["temporal_metadata_count"] <= 0:
        findings.append(_finding("warning", "temporal-metadata-not-evidenced", "시행일/개정일/개정이력 메타데이터 증거가 부족합니다."))
    elif int(runtime_summary.get("temporal_metadata_loss_count") or 0) > 0:
        findings.append(_finding("warning", "temporal-metadata-partial", "Repository chunk temporal metadata is not fully preserved in approved vector records."))
    elif runtime_summary["vector_record_count"] > 0 and runtime_summary["temporal_metadata_count"] < runtime_summary["vector_record_count"]:
        findings.append(
            _finding(
                "warning",
                "temporal-metadata-coverage-partial",
                _temporal_coverage_warning_detail(
                    runtime_summary,
                    temporal_coverage_summary or {},
                    temporal_backfill_shadow_summary or {},
                ),
                static_detail=False,
            )
        )
    conflict_count = _int((temporal_backfill_shadow_summary or {}).get("conflict_chunk_count"))
    write_blocked = bool((temporal_backfill_shadow_summary or {}).get("write_blocked"))
    if conflict_count > 0 or write_blocked:
        findings.append(
            _finding(
                "blocker",
                "temporal-backfill-conflict",
                "Shadow temporal backfill found "
                f"conflict_chunk_count={conflict_count}, write_blocked={str(write_blocked).lower()}. "
                "Resolve conflicts and rerun the shadow report before official RAG/MCP release.",
                static_detail=False,
            )
        )
    shadow_vector_record_count = _int((temporal_backfill_shadow_summary or {}).get("vector_record_count"))
    if (
        temporal_backfill_shadow_summary
        and runtime_summary["vector_record_count"] > 0
        and shadow_vector_record_count <= 0
    ):
        findings.append(
            _finding(
                "blocker",
                "temporal-backfill-vector-projection-empty",
                "Shadow temporal backfill produced no approved vector records even though the source runtime has "
                f"vector_record_count={runtime_summary['vector_record_count']}; "
                f"shadow_input_chunk_count={(temporal_backfill_shadow_summary or {}).get('input_chunk_count')}, "
                f"shadow_vector_record_count={shadow_vector_record_count}, "
                "shadow_runtime_runnable="
                f"{str(bool((temporal_backfill_shadow_summary or {}).get('shadow_runtime_runnable'))).lower()}. "
                "Restore the repository approval/indexing contract and rerun the shadow projection before release.",
                static_detail=False,
            )
        )
    ambiguous_count = _int((temporal_backfill_shadow_summary or {}).get("ambiguous_chunk_count"))
    if ambiguous_count > 0 and not temporal_ambiguity_scope_summary:
        findings.append(
            _finding(
                "blocker",
                "temporal-ambiguity-scope-missing",
                "Shadow temporal backfill found "
                f"ambiguous_chunk_count={ambiguous_count}, but no temporal ambiguity review scope report was provided.",
                static_detail=False,
            )
        )
    elif _temporal_ambiguity_scope_blocks_release(temporal_ambiguity_scope_summary or {}):
        policy_validation = temporal_ambiguity_policy_decision_validation_summary or {}
        policy_validation_ready = _temporal_ambiguity_policy_validation_ready(policy_validation)
        if not policy_validation_ready:
            validation_detail = (
                "no temporal ambiguity policy decision validation report"
                if not policy_validation
                else (
                    "decision_validation_status="
                    f"{policy_validation.get('status') or 'unknown'}, "
                    f"decision_validation_passed={str(bool(policy_validation.get('passed'))).lower()}, "
                    f"decision_validation_blocking_count={policy_validation.get('blocking_count')}"
                )
            )
            findings.append(
                _finding(
                    "blocker",
                    "temporal-ambiguity-policy-required",
                    "Temporal ambiguity scope status="
                    f"{(temporal_ambiguity_scope_summary or {}).get('status') or 'unknown'}, "
                    f"ambiguous_chunk_count={(temporal_ambiguity_scope_summary or {}).get('ambiguous_chunk_count')}, "
                    f"blocking_decision_count={(temporal_ambiguity_scope_summary or {}).get('blocking_decision_count')}, "
                    f"review_slice_count={(temporal_ambiguity_scope_summary or {}).get('review_slice_count')}; "
                    f"{validation_detail}.",
                    static_detail=False,
                )
            )
    elif temporal_ambiguity_policy_decision_validation_summary:
        findings.append(
            _finding(
                "warning",
                "temporal-ambiguity-policy-validation-unused",
                "Temporal ambiguity policy decision validation was provided, but the ambiguity scope itself does not require policy decisions.",
                static_detail=False,
            )
        )
    temporal_guard = temporal_evidence_guard_summary or {}
    stale_count = _int(temporal_guard.get("stale_artifact_count"))
    span_exceeds_threshold = bool(temporal_guard.get("payload_generated_at_span_exceeds_threshold"))
    if stale_count > 0 or span_exceeds_threshold:
        severity = "blocker" if bool(temporal_guard.get("strict_temporal_evidence")) else "warning"
        findings.append(
            _finding(
                severity,
                "temporal-evidence-older-than-threshold",
                "Temporal evidence freshness guard found "
                f"stale_artifact_count={stale_count}, "
                f"payload_generated_at_span_hours={temporal_guard.get('payload_generated_at_span_hours')}, "
                f"max_source_report_age_hours={temporal_guard.get('max_source_report_age_hours')}.",
                static_detail=False,
            )
        )
    lineage_mismatch_count = _int(temporal_guard.get("runtime_lineage_mismatch_count"))
    if lineage_mismatch_count > 0:
        severity = "blocker" if bool(temporal_guard.get("strict_temporal_evidence")) else "warning"
        findings.append(
            _finding(
                severity,
                "temporal-evidence-runtime-lineage-mismatch",
                "Temporal evidence runtime lineage does not match product readiness runtime: "
                f"runtime_lineage_mismatch_count={lineage_mismatch_count}, "
                f"expected_runtime_data_dir={temporal_guard.get('expected_runtime_data_dir')}, "
                f"expected_effective_runtime_data_dir={temporal_guard.get('expected_effective_runtime_data_dir')}.",
                static_detail=False,
            )
        )
    return _gate("개정 대응", findings)


def _generality_gate(
    runtime_summary: dict[str, Any],
    batch_summary: dict[str, Any],
    profile_provenance_summary: dict[str, Any],
) -> dict[str, Any]:
    findings = []
    file_type_count = len(batch_summary.get("file_type_counts") or {})
    file_type_keys = {str(key).lower() for key in batch_summary.get("file_type_counts") or {}}
    institution_count = int(batch_summary.get("institution_count") or 0)
    profile_count = len([key for key in batch_summary.get("profile_counts") or {} if key != "missing"])
    profile_keys = {key for key in batch_summary.get("profile_counts") or {} if key != "missing"}
    if batch_summary["report_count"] <= 0:
        findings.append(_finding("warning", "multi-document-evidence-missing", "기관별/파일형식별 batch 검증 근거가 연결되지 않았습니다."))
    if batch_summary["report_count"] > 0 and (
        file_type_count < len(REQUIRED_GENERALITY_FILE_TYPES)
        or not REQUIRED_GENERALITY_FILE_TYPES.issubset(file_type_keys)
    ):
        missing = ", ".join(sorted(REQUIRED_GENERALITY_FILE_TYPES - file_type_keys))
        findings.append(
            _finding(
                "warning",
                "file-format-diversity-low",
                f"HWP/HWPX/PDF 3종 검증 근거가 모두 필요합니다. Missing: {missing or 'unknown'}.",
            )
        )
    if batch_summary["report_count"] > 0 and institution_count < 2:
        findings.append(_finding("warning", "institution-diversity-low", "여러 공공기관 규정에 대한 검증 근거가 부족합니다."))
    if profile_count <= 0 and not runtime_summary["profile_ids"]:
        findings.append(_finding("warning", "institution-profile-missing", "기관별 문서 패턴을 보정할 profile 근거가 없습니다."))
    elif profile_keys and profile_keys.issubset(GENERIC_PROFILE_IDS):
        findings.append(
            _finding(
                "warning",
                "institution-profile-generic-only",
                "Batch evidence uses only a generic public institution profile; institution-specific profile coverage is not evidenced.",
            )
        )
    if not runtime_summary["profile_ids"]:
        findings.append(
            _finding(
                "blocker",
                "runtime-profile-ids-missing",
                "Runtime vectors do not include institution profile identifiers, so portability cannot be audited from the MCP runtime alone.",
            )
        )
    runtime_profile_binding_passed = runtime_summary.get("runtime_profile_binding_passed")
    if runtime_summary["vector_record_count"] > 0 and runtime_profile_binding_passed is not True:
        binding = runtime_summary.get("profile_binding_summary") or {}
        findings.append(
            _finding(
                "blocker",
                "runtime-profile-binding-failed",
                "Approved runtime profile binding failed: "
                f"runtime_profile_counts={binding.get('runtime_profile_counts') or {}}, "
                f"unknown={binding.get('runtime_unknown_profile_counts') or {}}, "
                f"mismatch_count={_int(binding.get('runtime_profile_mismatch_count'))}.",
                static_detail=False,
            )
        )
    if profile_provenance_summary:
        if int(profile_provenance_summary.get("blocker_count") or 0) > 0:
            findings.append(
                _finding(
                    "blocker",
                    "profile-provenance-failed",
                    "Institution profile provenance did not pass.",
                )
            )
        if profile_provenance_summary.get("unknown_profile_counts"):
            unknown_profiles = ", ".join(
                f"{profile_id}={count}"
                for profile_id, count in sorted(profile_provenance_summary["unknown_profile_counts"].items())
            )
            findings.append(
                _finding(
                    "blocker",
                    "profile-provenance-unknown",
                    f"Unknown institution profile IDs: {unknown_profiles or 'unknown'}.",
                )
            )
        elif int(profile_provenance_summary.get("warning_count") or 0) > 0 or not bool(profile_provenance_summary.get("passed")):
            findings.append(
                _finding(
                    "warning",
                    "profile-provenance-warnings",
                    "Institution profile provenance has warnings.",
                )
            )
    return _gate("범용성", findings)


def _answer_accuracy_gate(
    runtime_summary: dict[str, Any],
    rag_eval: dict[str, Any] | None,
    mcp_demo_answer_summary: dict[str, Any],
    accuracy_comparison_summary: dict[str, Any],
    *,
    min_answerable_ratio: float,
) -> dict[str, Any]:
    findings = []
    if runtime_summary["answer_profile_ratio"] < 0.8:
        findings.append(_finding("warning", "answer-profile-coverage-low", "답변 의도/사실 추출 메타데이터 적용률이 낮습니다."))
    if rag_eval:
        answerable_ratio = float(rag_eval.get("answerable_ratio") or 0.0)
        if answerable_ratio < min_answerable_ratio:
            findings.append(_finding("blocker", "rag-answerable-ratio-low", "회귀 질의셋 기준 답변 가능 비율이 낮습니다."))
        if int(rag_eval.get("quality_warning_chunk_count") or 0) > 0:
            findings.append(_finding("warning", "rag-quality-warning-chunks", "상위 검색 결과에 품질 경고 청크가 포함됩니다."))
    else:
        findings.append(_finding("warning", "rag-eval-report-missing", "단순 RAG 대비 정확성 회귀평가 리포트가 연결되지 않았습니다."))
    if mcp_demo_answer_summary:
        if not bool(mcp_demo_answer_summary.get("passed")):
            findings.append(_finding("blocker", "mcp-demo-answer-failed", ""))
        if int(mcp_demo_answer_summary.get("smoke_citation_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-demo-smoke-citations", ""))
        if int(mcp_demo_answer_summary.get("missing_supporting_result_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-demo-supporting-citations-missing", ""))
        if int(mcp_demo_answer_summary.get("no_evidence_with_citation_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-demo-no-evidence-citations", ""))
        if int(mcp_demo_answer_summary.get("quality_issue_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-demo-quality-issues", ""))
    else:
        findings.append(_finding("warning", "mcp-demo-answer-report-missing", ""))
    if accuracy_comparison_summary:
        if not bool(accuracy_comparison_summary.get("passed")):
            findings.append(_finding("blocker", "mcp-accuracy-comparison-failed", ""))
        if int(accuracy_comparison_summary.get("mcp_regression_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-accuracy-comparison-regression", ""))
    else:
        findings.append(
            _finding(
                "warning",
                "mcp-accuracy-comparison-report-missing",
                "Paired simple-RAG versus MCP accuracy comparison evidence is not linked.",
            )
        )
    return _gate("정확성", findings)


def _mcp_demo_answer_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    items = [item for item in report.get("items", []) or [] if isinstance(item, dict)]
    failed_items = [item for item in items if not bool(item.get("passed"))]
    smoke_citation_count = sum(_int(item.get("smoke_citation_count")) for item in items)
    quality_issue_count = _int(report.get("quality_issue_count")) or sum(_int(item.get("quality_issue_count")) for item in items)
    expected_term_hit_ratios = [
        _float(item.get("expected_term_hit_ratio"))
        for item in items
        if item.get("expected_terms")
    ]
    expected_article_no_hit_ratios = [
        _float(item.get("expected_article_no_hit_ratio"))
        for item in items
        if item.get("expected_article_nos")
    ]
    expected_article_title_hit_ratios = [
        _float(item.get("expected_article_title_hit_ratio"))
        for item in items
        if item.get("expected_article_titles")
    ]
    answerable_items = [item for item in items if not bool(item.get("expect_no_evidence"))]
    no_evidence_items = [item for item in items if bool(item.get("expect_no_evidence"))]
    missing_supporting_result_count = sum(
        1
        for item in answerable_items
        if _int(item.get("supporting_result_count")) <= 0 or not item.get("citations")
    )
    no_evidence_with_citation_count = sum(
        1
        for item in no_evidence_items
        if _int(item.get("supporting_result_count")) > 0 or bool(item.get("citations"))
    )
    summary = {
        "passed": (
            bool(report.get("passed"))
            and not failed_items
            and smoke_citation_count == 0
            and missing_supporting_result_count == 0
            and no_evidence_with_citation_count == 0
            and quality_issue_count == 0
        ),
        "query_count": _int(report.get("query_count")) or len(items),
        "answerable_query_count": len(answerable_items),
        "expect_no_evidence_query_count": len(no_evidence_items),
        "failed_item_count": len(failed_items),
        "smoke_citation_count": smoke_citation_count,
        "missing_supporting_result_count": missing_supporting_result_count,
        "no_evidence_with_citation_count": no_evidence_with_citation_count,
        "quality_issue_count": quality_issue_count,
        "api_call_count": _int(report.get("api_call_count")),
        "queries": [str(item.get("query") or "") for item in items],
        "supporting_result_counts": [
            _int(item.get("supporting_result_count"))
            for item in items
        ],
        "expected_term_query_count": len(expected_term_hit_ratios),
        "expected_term_hit_ratios": expected_term_hit_ratios,
        "expected_term_min_hit_ratio": min(expected_term_hit_ratios) if expected_term_hit_ratios else None,
        "expected_term_average_hit_ratio": (
            round(sum(expected_term_hit_ratios) / len(expected_term_hit_ratios), 3)
            if expected_term_hit_ratios
            else None
        ),
        "expected_term_partial_hit_count": sum(1 for ratio in expected_term_hit_ratios if ratio < 1.0),
        "expected_term_low_hit_count": sum(1 for ratio in expected_term_hit_ratios if ratio < 0.5),
        "expected_article_no_query_count": len(expected_article_no_hit_ratios),
        "expected_article_no_hit_ratios": expected_article_no_hit_ratios,
        "expected_article_no_min_hit_ratio": (
            min(expected_article_no_hit_ratios) if expected_article_no_hit_ratios else None
        ),
        "expected_article_title_query_count": len(expected_article_title_hit_ratios),
        "expected_article_title_hit_ratios": expected_article_title_hit_ratios,
        "expected_article_title_min_hit_ratio": (
            min(expected_article_title_hit_ratios) if expected_article_title_hit_ratios else None
        ),
    }
    summary.update(_query_reproducibility_summary(report))
    return summary


def _accuracy_comparison_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    comparison_summary = {
        "passed": bool(report.get("passed")),
        "query_count": _int(report.get("query_count")),
        "baseline_passed_count": _int(summary.get("baseline_passed_count")),
        "mcp_passed_count": _int(summary.get("mcp_passed_count")),
        "mcp_better_count": _int(summary.get("mcp_better_count")),
        "mcp_not_worse_count": _int(summary.get("mcp_not_worse_count")),
        "mcp_regression_count": _int(summary.get("mcp_regression_count")),
        "baseline_avg_quality_score": _float(summary.get("baseline_avg_quality_score")),
        "mcp_avg_quality_score": _float(summary.get("mcp_avg_quality_score")),
        "avg_score_delta": _float(summary.get("avg_score_delta")),
        "api_call_count": _int(report.get("api_call_count")),
    }
    comparison_summary.update(_query_reproducibility_summary(report))
    return comparison_summary


def _query_reproducibility_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "top_k",
        "query_spec_path",
        "query_spec_sha256",
        "query_spec_byte_count",
        "query_spec_item_count",
    ):
        if key in report:
            summary[key] = report[key]
    return summary


def _profile_provenance_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    registry_summary = report.get("registry_summary") if isinstance(report.get("registry_summary"), dict) else {}
    unknown_profile_counts = report.get("unknown_profile_counts")
    if not isinstance(unknown_profile_counts, dict):
        unknown_profile_counts = {}
    matched_profile_ids = report.get("matched_profile_ids")
    if not isinstance(matched_profile_ids, list):
        matched_profile_ids = []
    file_type_counts = report.get("file_type_counts")
    if not isinstance(file_type_counts, dict):
        file_type_counts = {}
    batch_profile_counts = report.get("batch_profile_counts")
    if not isinstance(batch_profile_counts, dict):
        batch_profile_counts = {}
    apba_id_counts = report.get("apba_id_counts")
    if not isinstance(apba_id_counts, dict):
        apba_id_counts = {}
    findings = [item for item in report.get("findings", []) or [] if isinstance(item, dict)]
    runtime_profile_counts = report.get("runtime_profile_counts")
    if not isinstance(runtime_profile_counts, dict):
        runtime_profile_counts = {}
    runtime_unknown_profile_counts = report.get("runtime_unknown_profile_counts")
    if not isinstance(runtime_unknown_profile_counts, dict):
        runtime_unknown_profile_counts = {}
    blocker_count = sum(1 for item in findings if item.get("severity") == "blocker")
    warning_count = sum(1 for item in findings if item.get("severity") == "warning")
    return {
        "passed": bool(report.get("passed")),
        "row_count": _int(report.get("row_count")),
        "institution_count": _int(report.get("institution_count")),
        "file_type_counts": dict(file_type_counts),
        "batch_profile_counts": dict(batch_profile_counts),
        "apba_id_count": _int(report.get("apba_id_count")),
        "apba_id_counts": dict(apba_id_counts),
        "registry_profile_count": _int(registry_summary.get("profile_count")),
        "registry_sha256": str(registry_summary.get("sha256") or ""),
        "matched_profile_count": len(matched_profile_ids),
        "unknown_profile_counts": dict(unknown_profile_counts),
        "runtime_profile_counts": dict(runtime_profile_counts),
        "runtime_unknown_profile_counts": dict(runtime_unknown_profile_counts),
        "runtime_profile_mismatch_count": _int(report.get("runtime_profile_mismatch_count")),
        "runtime_profile_binding_passed": bool(report.get("runtime_profile_binding_passed")),
        "registry_profile_bindings": report.get("registry_profile_bindings") if isinstance(report.get("registry_profile_bindings"), dict) else {},
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "finding_count": _int(report.get("finding_count")),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _mcp_transport_smoke_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    full_profile = report.get("full_profile") if isinstance(report.get("full_profile"), dict) else {}
    chatgpt_profile = report.get("chatgpt_data_profile") if isinstance(report.get("chatgpt_data_profile"), dict) else {}
    profile_metadata_summaries = _mcp_transport_profile_metadata_summaries(
        full_profile=full_profile,
        chatgpt_profile=chatgpt_profile,
    )
    metadata = _first_nonempty_profile_metadata(profile_metadata_summaries)
    required_fields = sorted(
        {
            field
            for summary in profile_metadata_summaries
            for field in summary.get("required_fields", [])
        }
    )
    missing_fields = sorted(
        {
            field
            for summary in profile_metadata_summaries
            for field in summary.get("missing_fields", [])
        }
    )
    observed_tenant_ids = sorted(
        {
            str(summary.get("tenant_id") or "").strip()
            for summary in profile_metadata_summaries
            if str(summary.get("tenant_id") or "").strip()
        }
    )
    tenant_id = str(report.get("tenant_id") or (observed_tenant_ids[0] if observed_tenant_ids else "")).strip()
    return {
        "passed": bool(report.get("passed")),
        "transport": str(report.get("transport") or ""),
        "tenant_id": tenant_id,
        "profile_id": str(report.get("profile_id") or "").strip() or None,
        "tenant_storage_isolation": _optional_bool(report.get("tenant_storage_isolation")),
        "full_profile_passed": bool(full_profile.get("passed")),
        "chatgpt_data_profile_passed": bool(chatgpt_profile.get("passed")),
        "full_profile_tool_names": list(full_profile.get("tool_names") or []),
        "chatgpt_data_tool_names": list(chatgpt_profile.get("tool_names") or []),
        "search_result_count": _int(full_profile.get("search_result_count")),
        "warm_search_result_count": _int(full_profile.get("warm_search_result_count")),
        "fetch_has_text": bool(full_profile.get("fetch_has_text")),
        "full_profile_timing_ms": _profile_timing_ms(full_profile),
        "chatgpt_data_profile_timing_ms": _profile_timing_ms(chatgpt_profile),
        "full_profile_search_timing_ms": _search_timing_ms(full_profile),
        "chatgpt_data_profile_search_timing_ms": _search_timing_ms(chatgpt_profile),
        "source_metadata_complete": not missing_fields,
        "required_source_metadata_fields": required_fields,
        "missing_source_metadata_fields": missing_fields,
        "profile_source_metadata": profile_metadata_summaries,
        "observed_result_tenant_ids": observed_tenant_ids,
        "first_result_metadata": metadata,
        "full_profile_history_tool_available": bool(full_profile.get("history_tool_available")),
        "full_profile_history_attempted": bool(full_profile.get("history_attempted")),
        "full_profile_history_passed": bool(full_profile.get("history_passed")),
        "full_profile_history_version_count": _int(full_profile.get("history_version_count")),
        "full_profile_history_error": str(full_profile.get("history_error") or ""),
    }


def _mcp_transport_profile_metadata_summaries(
    *,
    full_profile: dict[str, Any],
    chatgpt_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for profile_name, profile in (
        ("full_profile", full_profile),
        ("chatgpt_data_profile", chatgpt_profile),
    ):
        if not profile:
            continue
        metadata = profile.get("first_result_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if not metadata and int(profile.get("search_result_count") or 0) <= 0:
            continue
        search_metadata = profile.get("search_metadata") if isinstance(profile.get("search_metadata"), dict) else {}
        required_fields = _required_mcp_source_metadata_fields(metadata)
        optional_nullable_fields = {"effective_to", "repealed_at"}
        missing_fields = [
            field
            for field in required_fields
            if field not in metadata
            or (field not in optional_nullable_fields and metadata.get(field) == "")
        ]
        summaries.append(
            {
                "profile": profile_name,
                "chunk_type": str(metadata.get("chunk_type") or "").strip(),
                "tenant_id": str(metadata.get("tenant_id") or search_metadata.get("tenant_id") or "").strip(),
                "required_fields": required_fields,
                "missing_fields": missing_fields,
                "metadata": metadata,
            }
        )
    return summaries


def _first_nonempty_profile_metadata(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    for summary in summaries:
        metadata = summary.get("metadata")
        if isinstance(metadata, dict) and metadata:
            return metadata
    return {}


def _required_mcp_source_metadata_fields(metadata: dict[str, Any]) -> list[str]:
    fields = list(REQUIRED_MCP_SOURCE_METADATA_FIELDS)
    chunk_type = str(metadata.get("chunk_type") or "").strip().lower()
    if chunk_type in ARTICLE_SCOPED_MCP_CHUNK_TYPES:
        fields.append("article_no")
    return fields


def _operations_gate(
    runtime_summary: dict[str, Any],
    mcp_readiness: dict[str, Any] | None,
    mcp_evidence_lineage_summary: dict[str, Any],
    source_report_scope_summary: dict[str, Any],
    mcp_transport_smoke_summary: dict[str, Any],
    runtime_version_drift_summary: dict[str, Any],
    approval_workload_summary: dict[str, Any],
    approval_review_batch_summary: dict[str, Any],
    reapproval_workload_summary: dict[str, Any],
    reapproval_review_batch_summary: dict[str, Any],
    reapproval_decision_validation_summary: dict[str, Any],
    reapproval_apply_plan_summary: dict[str, Any],
    *,
    require_full_index: bool,
    allow_synthetic_runtime: bool = False,
) -> dict[str, Any]:
    findings = []
    approved_runtime_exists = (
        int(runtime_summary.get("approved_repository_chunk_count") or 0) > 0
        or int(runtime_summary.get("vector_record_count") or 0) > 0
    )
    if require_full_index and not runtime_summary["full_index_match"]:
        findings.append(_finding("blocker", "runtime-not-fully-indexed", ""))
    if (
        runtime_summary["smoke_document_count"] > 0
        or runtime_summary.get("synthetic_runtime") is True
    ) and not allow_synthetic_runtime:
        findings.append(_finding("blocker", "smoke-docs-in-runtime", ""))
    if mcp_readiness:
        if not bool(mcp_readiness.get("passed")):
            findings.append(_finding("blocker", "mcp-doctor-failed", ""))
        elif int(mcp_readiness.get("medium_count") or 0) > 0:
            findings.append(_finding("warning", "mcp-doctor-warnings", ""))
    else:
        findings.append(_finding("warning", "mcp-readiness-report-missing", ""))
    findings.extend(
        item
        for item in mcp_evidence_lineage_summary.get("findings", [])
        if isinstance(item, dict)
    )
    findings.extend(
        item
        for item in source_report_scope_summary.get("findings", [])
        if isinstance(item, dict)
    )
    if mcp_transport_smoke_summary:
        if not bool(mcp_transport_smoke_summary.get("passed")):
            findings.append(_finding("blocker", "mcp-transport-smoke-failed", ""))
        elif not bool(mcp_transport_smoke_summary.get("source_metadata_complete")):
            missing_fields = [str(value) for value in mcp_transport_smoke_summary.get("missing_source_metadata_fields") or [] if str(value).strip()]
            missing = ", ".join(missing_fields)
            synthetic_transport_smoke = not bool(str(mcp_transport_smoke_summary.get("profile_id") or "").strip())
            severity = "warning" if synthetic_transport_smoke or missing_fields == ["profile_id"] else "blocker"
            findings.append(
                _finding(
                    severity,
                    "mcp-source-metadata-missing",
                    f"MCP search results are missing required citation/source metadata: {missing or 'unknown'}.",
                )
            )
        observed_tools = {
            str(tool_name).strip()
            for tool_name in [
                *(mcp_transport_smoke_summary.get("full_profile_tool_names") or []),
                *(mcp_transport_smoke_summary.get("chatgpt_data_tool_names") or []),
            ]
        }
        synthetic_transport_smoke = not bool(str(mcp_transport_smoke_summary.get("profile_id") or "").strip())
        if "get_regulation_history" not in observed_tools:
            severity = "warning" if synthetic_transport_smoke else "blocker"
            findings.append(
                _finding(
                    severity,
                    "mcp-transport-history-missing",
                    "MCP transport smoke did not evidence get_regulation_history in the observed tool inventory.",
                    static_detail=False,
                )
            )
        elif not bool(mcp_transport_smoke_summary.get("full_profile_history_attempted")) or not bool(
            mcp_transport_smoke_summary.get("full_profile_history_passed")
        ):
            severity = "warning" if synthetic_transport_smoke else "blocker"
            findings.append(
                _finding(
                    severity,
                    "mcp-transport-history-call-missing",
                    "MCP transport smoke listed get_regulation_history but did not evidence a successful call: "
                    f"attempted={mcp_transport_smoke_summary.get('full_profile_history_attempted')}, "
                    f"version_count={mcp_transport_smoke_summary.get('full_profile_history_version_count')}, "
                    f"error={mcp_transport_smoke_summary.get('full_profile_history_error') or 'none'}.",
                    static_detail=False,
                )
            )
    else:
        findings.append(_finding("warning", "mcp-transport-smoke-report-missing", ""))
    if runtime_version_drift_summary:
        if (
            not bool(runtime_version_drift_summary.get("passed"))
            or int(runtime_version_drift_summary.get("blocker_count") or 0) > 0
            or int(runtime_version_drift_summary.get("vector_integrity_failure_count") or 0) > 0
        ):
            findings.append(
                _finding(
                    "blocker",
                    "runtime-version-drift-blocker",
                    "Runtime drift/vector integrity audit has blocker findings and must be resolved before MCP runtime handoff.",
                )
            )
        elif (
            int(runtime_version_drift_summary.get("warning_count") or 0) > 0
            or int(runtime_version_drift_summary.get("approved_repository_stale_chunker_count") or 0) > 0
            or bool(runtime_version_drift_summary.get("reprocess_requires_reapproval"))
        ):
            findings.append(
                _finding(
                    "warning",
                    "runtime-version-drift-evidence",
                    "Approved runtime version drift needs reprocess and reapproval evidence before claiming current parser/chunker behavior.",
                )
            )
    provenance_coverage = (
        runtime_summary.get("approval_provenance_coverage")
        if isinstance(runtime_summary.get("approval_provenance_coverage"), dict)
        else {}
    )
    if int(provenance_coverage.get("record_count") or 0) > 0 and (
        int(provenance_coverage.get("complete_record_count") or 0)
        < int(provenance_coverage.get("record_count") or 0)
    ):
        missing_field_counts = provenance_coverage.get("missing_field_counts")
        missing_summary = {
            field: count
            for field, count in _int_count_dict(missing_field_counts).items()
            if count > 0
        }
        findings.append(
            _finding(
                "warning",
                "approval-provenance-vector-evidence-incomplete",
                (
                    "Approved vector records are missing approval provenance metadata needed to connect "
                    f"indexed records back to worklist/batch review evidence: {missing_summary or 'unknown'}."
                ),
                static_detail=False,
            )
        )
    approval_journal_coverage = (
        runtime_summary.get("approval_journal_coverage")
        if isinstance(runtime_summary.get("approval_journal_coverage"), dict)
        else {}
    )
    if int(approval_journal_coverage.get("eligible_record_count") or 0) > 0 and int(
        approval_journal_coverage.get("missing_record_count") or 0
    ) > 0:
        sample_ids = approval_journal_coverage.get("missing_sample_record_ids")
        if not isinstance(sample_ids, list):
            sample_ids = []
        findings.append(
            _finding(
                "blocker",
                "approval-journal-vector-evidence-missing",
                (
                    "Approved vector records have complete approval provenance metadata but no matching "
                    "append-only approval journal record. "
                    f"missing={approval_journal_coverage.get('missing_record_count')} "
                    f"sample={sample_ids[:5]}"
                ),
                static_detail=False,
            )
        )
    review_event_coverage = (
        runtime_summary.get("approval_journal_review_event_coverage")
        if isinstance(runtime_summary.get("approval_journal_review_event_coverage"), dict)
        else {}
    )
    missing_event_chunks = review_event_coverage.get("missing_event_chunk_counts")
    if not isinstance(missing_event_chunks, dict):
        missing_event_chunks = {}
    if any(int(count or 0) > 0 for count in missing_event_chunks.values()):
        findings.append(
            _finding(
                "blocker",
                "approval-journal-review-events-incomplete",
                (
                    "Approval journal review_decision_events do not cover every approved chunk for the "
                    f"required event types: {missing_event_chunks}. "
                    f"samples={review_event_coverage.get('incomplete_samples') or []}"
                ),
                static_detail=False,
            )
        )
    if approval_workload_summary:
        if int(approval_workload_summary.get("pending_approval_chunks") or 0) > 0:
            findings.append(
                _finding(
                    "warning",
                    "approval-worklist-pending-chunks",
                    (
                        "Attached approval worklist still reports chunks pending approval; "
                        "verify this is intentional pre-approval evidence and that approval journal events cover the approved runtime."
                    ),
                )
            )
        if int(approval_workload_summary.get("manual_attention_chunks") or 0) > 0:
            findings.append(
                _finding(
                    "warning",
                    "pending-approval-manual-attention",
                    "Approval worklist has manual-attention chunks that must be reviewed before approval/indexing.",
                )
            )
    elif approved_runtime_exists:
        findings.append(
            _finding(
                "warning",
                "approval-worklist-evidence-missing",
                "Approved runtime chunks/vectors exist but no approval worklist report was attached.",
            )
        )
    if approval_review_batch_summary:
        if (
            not bool(approval_review_batch_summary.get("passed"))
            or int(approval_review_batch_summary.get("blocker_count") or 0) > 0
        ):
            findings.append(
                _finding(
                    "blocker",
                    "approval-review-batch-manifest-blockers",
                    "Approval review batch manifest has blockers or failed validation.",
                )
            )
    elif approved_runtime_exists:
        findings.append(
            _finding(
                "warning",
                "approval-review-batch-evidence-missing",
                "Approved runtime chunks/vectors exist but no approval review batch manifest was attached.",
            )
        )
    if reapproval_workload_summary:
        if (
            int(reapproval_workload_summary.get("source_vector_integrity_failure_count") or 0) > 0
            or int(reapproval_workload_summary.get("pre_reapproval_blocker_count") or 0) > 0
        ):
            findings.append(
                _finding(
                    "blocker",
                    "reapproval-worklist-blockers",
                    "Reapproval worklist has source vector integrity failures or pre-reapproval blockers.",
                )
            )
        elif int(reapproval_workload_summary.get("recommended_initial_review_chunks") or 0) > 0:
            findings.append(
                _finding(
                    "warning",
                    "reapproval-worklist-review-evidence",
                    "Reapproval worklist has initial review samples that must be completed before bulk reapproval.",
                )
            )
    elif runtime_version_drift_summary and bool(runtime_version_drift_summary.get("reprocess_requires_reapproval")):
        findings.append(
            _finding(
                "warning",
                "reapproval-worklist-missing",
                "Runtime drift requires reapproval but no reapproval worklist report was attached.",
            )
        )
    elif approved_runtime_exists:
        findings.append(
            _finding(
                "warning",
                "reapproval-worklist-evidence-missing",
                "Approved runtime chunks/vectors exist but no reapproval worklist report was attached.",
            )
        )
    reapproval_candidate_chunks = int(reapproval_workload_summary.get("reapproval_candidate_chunks") or 0)
    if reapproval_review_batch_summary:
        if (
            not bool(reapproval_review_batch_summary.get("passed"))
            or int(reapproval_review_batch_summary.get("blocker_count") or 0) > 0
        ):
            findings.append(
                _finding(
                    "blocker",
                    "reapproval-review-batch-manifest-blockers",
                    "Reapproval review batch manifest has blockers or failed validation.",
                )
            )
        elif reapproval_candidate_chunks > 0 and any(
            int(reapproval_review_batch_summary.get(key) or 0) != reapproval_candidate_chunks
            for key in ("candidate_count", "selected_candidate_count", "reapproval_chunk_count")
        ):
            findings.append(
                _finding(
                    "warning",
                    "reapproval-review-batch-incomplete",
                    "Reapproval review batch manifest candidate/selected/chunk counts do not match the reapproval worklist candidate count.",
                )
            )
    elif reapproval_candidate_chunks > 0:
        findings.append(
            _finding(
                "warning",
                "reapproval-review-batch-evidence-missing",
                "Reapproval worklist candidates exist but no reapproval review batch manifest was attached.",
            )
        )
    reapproval_batch_count = int(reapproval_review_batch_summary.get("batch_count") or 0)
    if reapproval_decision_validation_summary:
        if (
            not bool(reapproval_decision_validation_summary.get("passed"))
            or int(reapproval_decision_validation_summary.get("blocking_count") or 0) > 0
        ):
            findings.append(
                _finding(
                    "blocker",
                    "reapproval-decision-validation-blockers",
                    (
                        "Reapproval decision validation has blockers or pending operator decisions: "
                        f"status_counts={reapproval_decision_validation_summary.get('release_gate_status_counts') or {}}."
                    ),
                    static_detail=False,
                )
            )
        elif reapproval_batch_count > 0 and (
            int(reapproval_decision_validation_summary.get("expected_batch_count") or 0)
            < reapproval_batch_count
            or int(reapproval_decision_validation_summary.get("complete_row_count") or 0)
            < reapproval_batch_count
        ):
            findings.append(
                _finding(
                    "blocker",
                    "reapproval-decision-validation-incomplete",
                    (
                        "Reapproval decision validation does not cover every review batch: "
                        f"batches={reapproval_batch_count}, "
                        f"expected={reapproval_decision_validation_summary.get('expected_batch_count')}, "
                        f"complete={reapproval_decision_validation_summary.get('complete_row_count')}."
                    ),
                    static_detail=False,
                )
            )
    elif reapproval_batch_count > 0:
        findings.append(
            _finding(
                "blocker",
                "reapproval-decision-validation-missing",
                "Reapproval review batches exist but no reapproval decision validation report was attached.",
            )
        )
    if reapproval_apply_plan_summary:
        if (
            not bool(reapproval_apply_plan_summary.get("passed"))
            or int(reapproval_apply_plan_summary.get("blocker_count") or 0) > 0
        ):
            findings.append(
                _finding(
                    "blocker",
                    "reapproval-apply-plan-blockers",
                    "Reapproval apply plan has blockers and cannot be used for apply execution.",
                )
            )
        if int(reapproval_apply_plan_summary.get("unsafe_contract_violation_count") or 0) > 0:
            findings.append(
                _finding(
                    "blocker",
                    "reapproval-apply-plan-safety-contract-missing",
                    "Reapproval apply plan is missing required human-review safety contract controls.",
                )
            )
    return _gate("Operations", findings)


def _rag_eval_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "query_count": _int(report.get("query_count")),
        "answerable_query_count": _int(report.get("answerable_query_count")),
        "answerable_count": _int(report.get("answerable_count")),
        "answerable_ratio": float(report.get("answerable_ratio") or 0.0),
        "expect_no_evidence_query_count": _int(report.get("expect_no_evidence_query_count")),
        "no_evidence_passed_count": _int(report.get("no_evidence_passed_count")),
        "no_evidence_failed_count": _int(report.get("no_evidence_failed_count")),
        "relation_supported_ratio": float(report.get("relation_supported_ratio") or 0.0),
        "quality_warning_chunk_count": _int(report.get("quality_warning_chunk_count")),
        "source_mode": str(report.get("source_mode") or ""),
        "source_chunk_count": _int(report.get("source_chunk_count")),
        "effective_runtime_data_dir": str(report.get("effective_runtime_data_dir") or ""),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _public_readiness_summary(report: dict[str, Any] | None, *, source_path: Path | None = None) -> dict[str, Any]:
    if not report:
        return {}
    checks = [item for item in report.get("checks", []) or [] if isinstance(item, dict)]
    failed_checks = [str(item.get("name")) for item in checks if not bool(item.get("passed"))]
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    thresholds = _public_readiness_thresholds(report, checks)
    payload_profile = str(report.get("readiness_profile") or "").strip().lower()
    inferred_tolerance = _public_readiness_thresholds_are_tolerant(thresholds)
    review_tolerance_evidence = (
        payload_profile in {"review_tolerance", "review-tolerance", "tolerance"}
        or inferred_tolerance
        or _is_review_tolerance_report_path(source_path)
    )
    readiness_profile = payload_profile or ("review_tolerance" if review_tolerance_evidence else "strict")
    source_path_text = str(source_path) if source_path else ""
    return {
        "source_path": source_path_text,
        "readiness_profile": readiness_profile,
        "strict_release_evidence": bool(report.get("passed")) and not review_tolerance_evidence,
        "review_tolerance_evidence": review_tolerance_evidence,
        "thresholds": thresholds,
        "passed": bool(report.get("passed")),
        "status": str(report.get("status") or ""),
        "input_count": _int(summary.get("input_count")),
        "successful_count": _int(summary.get("successful_count")),
        "failed_count": _int(summary.get("failed_count")),
        "ocr_required_count": _int(summary.get("ocr_required_count")),
        "recommendation_total": _int(summary.get("recommendation_total")),
        "failed_check_count": len(failed_checks),
        "failed_checks": failed_checks,
    }


def _public_readiness_thresholds(report: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    thresholds = report.get("thresholds") if isinstance(report.get("thresholds"), dict) else {}
    return {
        "min_average_quality": _threshold_value(
            thresholds,
            checks,
            "min_average_quality",
            "average_quality_at_or_above_minimum",
            "minimum",
            STRICT_PUBLIC_READINESS_MIN_AVERAGE_QUALITY,
        ),
        "max_failed_info": _threshold_value(
            thresholds,
            checks,
            "max_failed_info",
            "failed_info_checks_within_limit",
            "maximum",
            0,
        ),
        "max_recommendations": _threshold_value(
            thresholds,
            checks,
            "max_recommendations",
            "recommendations_within_limit",
            "maximum",
            0,
        ),
        "max_table_attention": _threshold_value(
            thresholds,
            checks,
            "max_table_attention",
            "table_attention_within_limit",
            "maximum",
            0,
        ),
        "max_current_ai_tokens": _threshold_value(
            thresholds,
            checks,
            "max_current_ai_tokens",
            "current_ai_tokens_within_limit",
            "maximum",
            0,
        ),
        "required_row_fields": list(thresholds.get("required_row_fields") or []),
        "required_artifact_fields": list(thresholds.get("required_artifact_fields") or []),
        "strict_institution_profiles": bool(thresholds.get("strict_institution_profiles")),
        "require_semantic_embedding_approval": bool(thresholds.get("require_semantic_embedding_approval")),
    }


def _threshold_value(
    thresholds: dict[str, Any],
    checks: list[dict[str, Any]],
    threshold_key: str,
    check_name: str,
    detail_key: str,
    default: Any,
) -> Any:
    if threshold_key in thresholds:
        return thresholds.get(threshold_key)
    for check in checks:
        if check.get("name") != check_name:
            continue
        details = check.get("details") if isinstance(check.get("details"), dict) else {}
        if detail_key in details:
            return details.get(detail_key)
    return default


def _public_readiness_thresholds_are_tolerant(thresholds: dict[str, Any]) -> bool:
    if _optional_float(thresholds.get("min_average_quality")) is not None:
        if float(thresholds.get("min_average_quality") or 0.0) < STRICT_PUBLIC_READINESS_MIN_AVERAGE_QUALITY:
            return True
    return any(
        _int(thresholds.get(key)) > 0
        for key in ("max_failed_info", "max_recommendations", "max_table_attention", "max_current_ai_tokens")
    )


def _public_readiness_review_tolerance_detail(summary: dict[str, Any]) -> str:
    thresholds = summary.get("thresholds") if isinstance(summary.get("thresholds"), dict) else {}
    allowances = {
        "max_failed_info": _int(thresholds.get("max_failed_info")),
        "max_recommendations": _int(thresholds.get("max_recommendations")),
        "max_table_attention": _int(thresholds.get("max_table_attention")),
        "max_current_ai_tokens": _int(thresholds.get("max_current_ai_tokens")),
    }
    return (
        "Public readiness is review-tolerance evidence, not strict release-grade parsing evidence. "
        f"Allowed thresholds: failed_info<={allowances['max_failed_info']}, "
        f"recommendations<={allowances['max_recommendations']}, "
        f"table_attention<={allowances['max_table_attention']}, "
        f"current_ai_tokens<={allowances['max_current_ai_tokens']}. "
        f"Observed recommendations={_int(summary.get('recommendation_total'))}, "
        f"failed_checks={_int(summary.get('failed_check_count'))}."
    )


def _is_review_tolerance_report_path(path: Path | None) -> bool:
    if not path:
        return False
    normalized = str(path).lower().replace("\\", "/")
    return "review_tolerance" in normalized or "review-tolerance" in normalized


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value)), 3)
    except ValueError:
        return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _parser_goldset_score_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    completion = report.get("completion") if isinstance(report.get("completion"), dict) else {}
    overall = report.get("overall") if isinstance(report.get("overall"), dict) else {}
    by_structure = report.get("by_structure") if isinstance(report.get("by_structure"), dict) else {}
    ready_for_quality_claim = _optional_bool(summary.get("ready_for_quality_claim"))
    if ready_for_quality_claim is None:
        ready_for_quality_claim = _optional_bool(completion.get("ready_for_quality_claim"))
    return {
        "report_type": str(report.get("report_type") or ""),
        "document_count": _int(summary.get("document_count")),
        "scored_document_count": _int(summary.get("scored_document_count")),
        "excluded_document_count": _int(summary.get("excluded_document_count")),
        "structure_type_count": _int(summary.get("structure_type_count")),
        "scorable_structure_count": _int(summary.get("scorable_structure_count")),
        "issue_count": _int(summary.get("issue_count")),
        "ready_for_quality_claim": ready_for_quality_claim,
        "completed_document_count": _int(completion.get("completed_document_count")),
        "pending_document_count": _int(completion.get("pending_document_count")),
        "completion_scored_document_count": _int(completion.get("scored_document_count")),
        "completion_excluded_document_count": _int(completion.get("excluded_document_count")),
        "expected_structure_score_count": _int(completion.get("expected_structure_score_count")),
        "completed_structure_score_count": _int(completion.get("completed_structure_score_count")),
        "missing_structure_score_count": _int(completion.get("missing_structure_score_count")),
        "blocking_issue_codes": completion.get("blocking_issue_codes") if isinstance(completion.get("blocking_issue_codes"), dict) else {},
        "overall_precision": _optional_float(overall.get("precision")),
        "overall_recall": _optional_float(overall.get("recall")),
        "overall_f1": _optional_float(overall.get("f1")),
        "by_structure_f1": {
            str(structure_type): _optional_float(values.get("f1") if isinstance(values, dict) else None)
            for structure_type, values in sorted(by_structure.items())
        },
        "by_structure_issue_counts": {
            str(structure_type): _int(values.get("missing_match_count") if isinstance(values, dict) else None)
            for structure_type, values in sorted(by_structure.items())
        },
    }


def _parser_goldset_completion_board_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    structure_completion = (
        report.get("structure_completion_summary")
        if isinstance(report.get("structure_completion_summary"), dict)
        else {}
    )
    return {
        "report_type": str(report.get("report_type") or ""),
        "document_count": _int(report.get("document_count")),
        "ready_document_count": _int(report.get("ready_document_count")),
        "pending_document_count": _int(report.get("pending_document_count")),
        "expected_structure_score_rows": _int(report.get("expected_structure_score_rows")),
        "completed_structure_score_rows": _int(report.get("completed_structure_score_rows")),
        "missing_structure_score_rows": _int(report.get("missing_structure_score_rows")),
        "missing_manual_field_count": _int(report.get("missing_manual_field_count")),
        "missing_matched_field_count": _int(report.get("missing_matched_field_count")),
        "missing_reviewer_metadata_count": _int(report.get("missing_reviewer_metadata_count")),
        "ready_for_quality_claim": _optional_bool(report.get("ready_for_quality_claim")),
        "completion_gate_status": str(report.get("completion_gate_status") or ""),
        "priority_tier_counts": (
            report.get("priority_tier_counts")
            if isinstance(report.get("priority_tier_counts"), dict)
            else {}
        ),
        "structure_completion_summary": {
            str(structure_type): {
                "pipeline_total": _int(values.get("pipeline_total") if isinstance(values, dict) else None),
                "score_rows_complete": _int(
                    values.get("score_rows_complete") if isinstance(values, dict) else None
                ),
                "expected_document_count": _int(
                    values.get("expected_document_count") if isinstance(values, dict) else None
                ),
                "missing_manual_count": _int(
                    values.get("missing_manual_count") if isinstance(values, dict) else None
                ),
                "missing_matched_count": _int(
                    values.get("missing_matched_count") if isinstance(values, dict) else None
                ),
                "ready_for_structure_f1": _optional_bool(
                    values.get("ready_for_structure_f1") if isinstance(values, dict) else None
                ),
            }
            for structure_type, values in sorted(structure_completion.items())
        },
    }


def _table_preprocessing_claim_gate_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": _optional_bool(report.get("passed")),
        "status": str(report.get("status") or ""),
        "claim_level": str(report.get("claim_level") or ""),
        "feasibility_status": str(report.get("feasibility_status") or ""),
        "blocker_count": _int(report.get("blocker_count")),
        "warning_count": _int(report.get("warning_count")),
        "selected_unit_count": _int(summary.get("selected_unit_count")),
        "completed_unit_count": _int(summary.get("completed_unit_count")),
        "pending_unit_count": _int(summary.get("pending_unit_count")),
        "invalid_unit_count": _int(summary.get("invalid_unit_count")),
        "required_field_missing_total": _int(summary.get("required_field_missing_total")),
        "required_field_missing_counts": _int_count_dict(summary.get("required_field_missing_counts")),
        "review_priority_counts": _int_count_dict(summary.get("review_priority_counts")),
        "label_review_flag_counts": _int_count_dict(summary.get("label_review_flag_counts")),
        "ready_for_table_score_transfer": _optional_bool(summary.get("ready_for_table_score_transfer")),
        "transfer_passed": _optional_bool(summary.get("transfer_passed")),
        "transfer_blocker_count": _int(summary.get("transfer_blocker_count")),
        "source_traceability_passed": _optional_bool(summary.get("source_traceability_passed")),
        "source_traceability_issue_count": _int(summary.get("source_traceability_issue_count")),
        "source_traceability_issue_counts": _int_count_dict(summary.get("source_traceability_issue_counts")),
        "source_traceability_record_count": _int(summary.get("source_traceability_record_count")),
        "source_traceability_require_page_count_verification": _optional_bool(
            summary.get("source_traceability_require_page_count_verification")
        ),
        "source_page_count_status_counts": _int_count_dict(summary.get("source_page_count_status_counts")),
        "source_format_status_counts": _int_count_dict(summary.get("source_format_status_counts")),
        "source_traceability_operator_next_action_counts": _int_count_dict(
            summary.get("source_traceability_operator_next_action_counts")
        ),
        "drift_check_present": _optional_bool(summary.get("drift_check_present")),
        "drift_check_passed": _optional_bool(summary.get("drift_check_passed")),
        "drift_check_blocker_count": _int(summary.get("drift_check_blocker_count")),
        "answer_query_count": _int(summary.get("answer_query_count")),
        "table_answer_blocker_count": _int(summary.get("table_answer_blocker_count")),
        "non_review_evidence_ready": _optional_bool(summary.get("non_review_evidence_ready")),
        "release_blocked_by_human_review": _optional_bool(
            summary.get("release_blocked_by_human_review")
        ),
        "finding_code_counts": _int_count_dict(report.get("finding_code_counts")),
    }


def _mcp_readiness_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    index_visibility = (
        report.get("mcp_index_visibility_summary")
        if isinstance(report.get("mcp_index_visibility_summary"), dict)
        else {}
    )
    return {
        "passed": bool(report.get("passed")),
        "deploy_ready": bool(report.get("deploy_ready")),
        "client_profile": str(report.get("client_profile") or ""),
        "readiness_scope": str(report.get("readiness_scope") or ""),
        "bundle_dir": str(report.get("bundle_dir") or ""),
        "data_dir": str(report.get("data_dir") or ""),
        "effective_data_dir": str(report.get("effective_data_dir") or ""),
        "tenant_id": str(report.get("tenant_id") or index_visibility.get("tenant_id") or ""),
        "high_count": _int(report.get("high_count")),
        "medium_count": _int(report.get("medium_count")),
        "finding_count": _int(report.get("finding_count")),
        "index_visibility_tenant_id": str(index_visibility.get("tenant_id") or ""),
        "index_visibility_total_indexable_records": _int(
            index_visibility.get("total_indexable_record_count")
        ),
        "index_visibility_total_mcp_visible_records": _int(
            index_visibility.get("total_mcp_visible_records")
        ),
    }


def _source_report_scope_summary(
    *,
    source_report_artifacts: list[dict[str, Any]],
    runtime_data_dir: Path,
    effective_runtime_data_dir: Path,
    tenant_id: str,
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    """Reject runtime evidence that was generated from a different snapshot."""
    findings: list[dict[str, str]] = []
    expected_paths = {
        "runtime_data_dir": _normalized_compare_path(runtime_data_dir),
        "effective_runtime_data_dir": _normalized_compare_path(effective_runtime_data_dir),
    }
    expected_counts = {
        "repository_chunk_count": _int(runtime_summary.get("repository_chunk_count")),
        "approved_repository_chunk_count": _int(runtime_summary.get("approved_repository_chunk_count")),
        "total_chunks": _int(runtime_summary.get("repository_chunk_count")),
        "approved_chunks": _int(runtime_summary.get("approved_repository_chunk_count")),
        "total_approved_chunks": _int(runtime_summary.get("approved_repository_chunk_count")),
        "vector_record_count": _int(runtime_summary.get("vector_record_count")),
        "record_count": _int(runtime_summary.get("vector_record_count")),
        "source_chunk_count": _int(runtime_summary.get("repository_chunk_count")),
    }
    checked_artifact_count = 0
    scoped_artifact_count = 0
    for artifact in source_report_artifacts:
        role = str(artifact.get("role") or "")
        if role not in RUNTIME_SCOPED_SOURCE_REPORT_ROLES:
            continue
        finding_severity = (
            "warning"
            if role in {"runtime_version_drift_report", "temporal_coverage_report"}
            else "blocker"
        )
        checked_artifact_count += 1
        scope = artifact.get("scope") if isinstance(artifact.get("scope"), dict) else {}
        if not scope:
            continue
        scoped_artifact_count += 1
        path = str(artifact.get("path") or "")
        for field, expected in expected_paths.items():
            observed = ""
            for candidate in _SOURCE_REPORT_SCOPE_PATH_ALIASES[field]:
                if str(scope.get(candidate) or "").strip():
                    observed = str(scope[candidate]).strip()
                    break
            if not observed:
                continue
            normalized = _normalized_compare_path(observed)
            if normalized != expected:
                findings.append(
                    _finding(
                        finding_severity,
                        "source-report-runtime-lineage-mismatch",
                        (
                            f"{role} at {path} has {field}={observed!r}; "
                            f"expected {expected!r}."
                        ),
                        static_detail=False,
                    )
                )
        observed_tenant = str(scope.get("tenant_id") or "").strip()
        if observed_tenant and observed_tenant != tenant_id:
            findings.append(
                _finding(
                    finding_severity,
                    "source-report-tenant-mismatch",
                    (
                        f"{role} at {path} has tenant_id={observed_tenant!r}; "
                        f"expected {tenant_id!r}."
                    ),
                    static_detail=False,
                )
            )
        for field, expected in expected_counts.items():
            if field not in scope or str(scope.get(field) or "").strip() == "":
                continue
            observed = _optional_int_value(scope.get(field))
            if observed is None or observed != expected:
                findings.append(
                    _finding(
                        finding_severity,
                        "source-report-record-count-mismatch",
                        (
                            f"{role} at {path} has {field}={scope.get(field)!r}; "
                            f"expected {expected}."
                        ),
                        static_detail=False,
                    )
                )
    return {
        "expected_tenant_id": tenant_id,
        "expected_runtime_data_dir": str(runtime_data_dir),
        "expected_effective_runtime_data_dir": str(effective_runtime_data_dir),
        "expected_repository_chunk_count": expected_counts["repository_chunk_count"],
        "expected_approved_repository_chunk_count": expected_counts["approved_repository_chunk_count"],
        "expected_vector_record_count": expected_counts["vector_record_count"],
        "checked_artifact_count": checked_artifact_count,
        "scoped_artifact_count": scoped_artifact_count,
        "blocker_count": sum(1 for item in findings if item.get("severity") == "blocker"),
        "warning_count": sum(1 for item in findings if item.get("severity") == "warning"),
        "findings": findings,
    }


def _mcp_evidence_lineage_summary(
    *,
    runtime_data_dir: Path,
    effective_runtime_data_dir: Path,
    tenant_id: str,
    runtime_summary: dict[str, Any],
    mcp_readiness: dict[str, Any] | None,
    mcp_transport_smoke: dict[str, Any] | None,
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    product_runtime_paths = {
        path
        for path in (
            _normalized_compare_path(runtime_data_dir),
            _normalized_compare_path(effective_runtime_data_dir),
        )
        if path
    }
    runtime_vector_record_count = _int(runtime_summary.get("vector_record_count"))

    readiness_runtime_paths: set[str] = set()
    readiness_tenant_id = ""
    readiness_visible_records: int | None = None
    readiness_indexable_records: int | None = None
    if mcp_readiness:
        readiness_tenant_id = str(mcp_readiness.get("tenant_id") or "").strip()
        effective_readiness_path = _normalized_compare_path(mcp_readiness.get("effective_data_dir"))
        if effective_readiness_path:
            readiness_runtime_paths.add(effective_readiness_path)
        else:
            fallback_readiness_path = _normalized_compare_path(mcp_readiness.get("data_dir"))
            if fallback_readiness_path:
                readiness_runtime_paths.add(fallback_readiness_path)
        bundle_dir = str(mcp_readiness.get("bundle_dir") or "").strip()
        if bundle_dir:
            bundle_data_dir = Path(bundle_dir) / "data"
            normalized = _normalized_compare_path(bundle_data_dir)
            if normalized:
                readiness_runtime_paths.add(normalized)
        index_visibility = (
            mcp_readiness.get("mcp_index_visibility_summary")
            if isinstance(mcp_readiness.get("mcp_index_visibility_summary"), dict)
            else {}
        )
        if index_visibility:
            readiness_tenant_id = readiness_tenant_id or str(index_visibility.get("tenant_id") or "").strip()
            readiness_visible_records = _optional_int_value(
                index_visibility.get("total_mcp_visible_records")
            )
            readiness_indexable_records = _optional_int_value(
                index_visibility.get("total_indexable_record_count")
            )
        if readiness_runtime_paths and product_runtime_paths.isdisjoint(readiness_runtime_paths):
            findings.append(
                _finding(
                    "blocker",
                    "mcp-readiness-runtime-lineage-mismatch",
                    (
                        "MCP connection readiness was generated for a different runtime data directory: "
                        f"product={sorted(product_runtime_paths)} readiness={sorted(readiness_runtime_paths)}."
                    ),
                    static_detail=False,
                )
            )
        if readiness_tenant_id and readiness_tenant_id != tenant_id:
            findings.append(
                _finding(
                    "blocker",
                    "mcp-readiness-tenant-mismatch",
                    (
                        "MCP connection readiness tenant does not match product readiness: "
                        f"product={tenant_id} readiness={readiness_tenant_id}."
                    ),
                    static_detail=False,
                )
            )
        if readiness_indexable_records is not None and readiness_indexable_records != runtime_vector_record_count:
            findings.append(
                _finding(
                    "blocker",
                    "mcp-readiness-record-count-mismatch",
                    (
                        "MCP connection readiness record count does not match product runtime vector count: "
                        f"product={runtime_vector_record_count} readiness={readiness_indexable_records}."
                    ),
                    static_detail=False,
                )
            )

    transport_tenant_id = ""
    transport_observed_tenant_ids: list[str] = []
    if mcp_transport_smoke:
        transport_tenant_id = str(mcp_transport_smoke.get("tenant_id") or "").strip()
        transport_observed_tenant_ids = _transport_observed_tenant_ids(mcp_transport_smoke)
        mismatched_transport_tenant_ids = sorted(
            {
                observed
                for observed in ([transport_tenant_id] + transport_observed_tenant_ids)
                if observed and observed != tenant_id
            }
        )
        if mismatched_transport_tenant_ids:
            findings.append(
                _finding(
                    "blocker",
                    "mcp-transport-tenant-mismatch",
                    (
                        "MCP transport smoke tenant does not match product readiness: "
                        f"product={tenant_id} transport={mismatched_transport_tenant_ids}."
                    ),
                    static_detail=False,
                )
            )

    return {
        "expected_tenant_id": tenant_id,
        "runtime_data_dir": str(runtime_data_dir),
        "effective_runtime_data_dir": str(effective_runtime_data_dir),
        "runtime_vector_record_count": runtime_vector_record_count,
        "mcp_readiness_tenant_id": readiness_tenant_id,
        "mcp_readiness_runtime_paths": sorted(readiness_runtime_paths),
        "mcp_readiness_visible_record_count": readiness_visible_records,
        "mcp_readiness_indexable_record_count": readiness_indexable_records,
        "mcp_transport_tenant_id": transport_tenant_id,
        "mcp_transport_observed_tenant_ids": transport_observed_tenant_ids,
        "blocker_count": sum(1 for item in findings if item.get("severity") == "blocker"),
        "warning_count": sum(1 for item in findings if item.get("severity") == "warning"),
        "findings": findings,
    }


def _transport_observed_tenant_ids(report: dict[str, Any]) -> list[str]:
    tenant_ids: set[str] = set()
    for profile_key in ("full_profile", "chatgpt_data_profile"):
        profile = report.get(profile_key)
        if not isinstance(profile, dict):
            continue
        metadata = profile.get("first_result_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        search_metadata = profile.get("search_metadata") if isinstance(profile.get("search_metadata"), dict) else {}
        tenant_id = str(metadata.get("tenant_id") or search_metadata.get("tenant_id") or "").strip()
        if tenant_id:
            tenant_ids.add(tenant_id)
    return sorted(tenant_ids)


def _optional_int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalized_compare_path(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(Path(str(value)).expanduser().resolve(strict=False)).casefold()
    except (OSError, RuntimeError, ValueError):
        return str(Path(str(value)).expanduser()).casefold()


def _temporal_coverage_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    opportunities = report.get("inheritance_opportunities") if isinstance(report.get("inheritance_opportunities"), dict) else {}
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "record_count": _int(report.get("record_count")),
        "with_temporal_metadata_count": _int(report.get("with_temporal_metadata_count")),
        "without_temporal_metadata_count": _int(report.get("without_temporal_metadata_count")),
        "temporal_metadata_ratio": _optional_float(report.get("temporal_metadata_ratio")),
        "as_of_date": str(report.get("as_of_date") or ""),
        "lifecycle_complete_count": _int(report.get("lifecycle_complete_count")),
        "lifecycle_incomplete_count": _int(report.get("lifecycle_incomplete_count")),
        "regulation_group_count": _int(report.get("regulation_group_count")),
        "duplicate_active_version_group_count": _int(report.get("duplicate_active_version_group_count")),
        "latest_only_passed": bool(report.get("latest_only_passed")),
        "latest_selected_record_count": _int(report.get("latest_selected_record_count")),
        "non_latest_record_count": _int(report.get("non_latest_record_count")),
        "candidate_scope_count": _int(opportunities.get("candidate_scope_count")),
        "candidate_missing_record_count": _int(opportunities.get("candidate_missing_record_count")),
        "blocker_count": _int(report.get("blocker_count")),
        "warning_count": _int(report.get("warning_count")),
        "finding_count": _int(report.get("finding_count")),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _temporal_backfill_shadow_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    before = report.get("before") if isinstance(report.get("before"), dict) else {}
    after = report.get("after") if isinstance(report.get("after"), dict) else {}
    delta = report.get("delta") if isinstance(report.get("delta"), dict) else {}
    vector_record_count = _int(report.get("vector_record_count"))
    shadow_runtime_written = bool(report.get("shadow_runtime_written"))
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "runtime_copy_scope": str(report.get("runtime_copy_scope") or ""),
        "shadow_runtime_runnable": bool(report.get("shadow_runtime_runnable")),
        "chunk_file_count": _int(report.get("chunk_file_count")),
        "input_chunk_count": _int(report.get("input_chunk_count")),
        "output_chunk_count": _int(report.get("output_chunk_count")),
        "vector_record_count": vector_record_count,
        "before_temporal_metadata_count": _int(before.get("temporal_metadata_count")),
        "before_temporal_metadata_ratio": _optional_float(before.get("temporal_metadata_ratio")),
        "after_temporal_metadata_count": _int(after.get("temporal_metadata_count")),
        "after_temporal_metadata_ratio": _optional_float(after.get("temporal_metadata_ratio")),
        "delta_temporal_metadata_count": _int(delta.get("temporal_metadata_count")),
        "inherited_chunk_count": _int(after.get("inherited_chunk_count")),
        "normalized_chunk_count": _int(after.get("normalized_chunk_count")),
        "conflict_chunk_count": _int(after.get("conflict_chunk_count")),
        "delta_conflict_chunk_count": _int(delta.get("conflict_chunk_count")),
        "ambiguous_chunk_count": _int(after.get("ambiguous_chunk_count")),
        "delta_ambiguous_chunk_count": _int(delta.get("ambiguous_chunk_count")),
        "write_blocked": bool(report.get("write_blocked")),
        "shadow_runtime_written": shadow_runtime_written,
        "shadow_vector_projection_ready": bool(report.get("passed"))
        and shadow_runtime_written
        and vector_record_count > 0,
        "api_call_count": _int(report.get("api_call_count")),
    }


def _temporal_ambiguity_scope_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    record_analysis = report.get("record_analysis") if isinstance(report.get("record_analysis"), dict) else {}
    decision_requirements = [item for item in report.get("decision_requirements") or [] if isinstance(item, dict)]
    blocking_decision_count = sum(1 for item in decision_requirements if bool(item.get("blocks_product_release")))
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "status": str(report.get("status") or ""),
        "chunk_count": _int(summary.get("chunk_count")),
        "conflict_chunk_count": _int(summary.get("conflict_chunk_count")),
        "ambiguous_chunk_count": _int(summary.get("ambiguous_chunk_count")),
        "ambiguous_chunk_ratio": _optional_float(summary.get("ambiguous_chunk_ratio")),
        "vector_record_count": _int(record_analysis.get("vector_record_count")),
        "ambiguous_record_count": _int(record_analysis.get("ambiguous_record_count")),
        "review_slice_count": _int(record_analysis.get("review_slice_count")),
        "decision_requirement_count": len(decision_requirements),
        "blocking_decision_count": blocking_decision_count,
        "api_call_count": _int(report.get("api_call_count")),
    }


def _temporal_ambiguity_policy_decision_validation_summary(
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "status": str(report.get("status") or ""),
        "scope_report": str(report.get("scope_report") or ""),
        "scope_passed": bool(report.get("scope_passed")),
        "decision_row_count": _int(report.get("decision_row_count")),
        "release_blocking_row_count": _int(report.get("release_blocking_row_count")),
        "blocking_count": _int(report.get("blocking_count")),
        "warning_count": _int(report.get("warning_count")),
        "operator_decision_counts": _int_count_dict(report.get("operator_decision_counts")),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _temporal_ambiguity_policy_validation_ready(summary: dict[str, Any]) -> bool:
    return bool(
        summary
        and summary.get("passed")
        and _int(summary.get("blocking_count")) == 0
        and _int(summary.get("release_blocking_row_count")) > 0
    )


def _temporal_ambiguity_scope_blocks_release(summary: dict[str, Any]) -> bool:
    if not summary:
        return False
    status = str(summary.get("status") or "")
    return bool(
        _int(summary.get("blocking_decision_count")) > 0
        or status in {"temporal_conflict_blocked", "temporal_ambiguity_policy_required"}
        or (status and status != "temporal_ambiguity_clear" and not bool(summary.get("passed")))
    )


def _revision_impact_summary(payloads: list[Any]) -> dict[str, Any]:
    reports = [payload for payload in payloads if isinstance(payload, dict)]
    if not reports:
        return {}
    count_keys = (
        "before_unit_count",
        "after_unit_count",
        "changed_count",
        "added_count",
        "removed_count",
        "unchanged_count",
        "metadata_only_changed_count",
        "approval_required_count",
        "approval_reuse_candidate_count",
        "deindex_required_count",
    )
    totals = {key: 0 for key in count_keys}
    before_labels: list[str] = []
    after_labels: list[str] = []
    for report in reports:
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        for key in count_keys:
            totals[key] += _int(summary.get(key))
        if str(report.get("before_label") or "").strip():
            before_labels.append(str(report["before_label"]))
        if str(report.get("after_label") or "").strip():
            after_labels.append(str(report["after_label"]))
    return {
        "report_type": "revision_impact_summary",
        "report_count": len(reports),
        **totals,
        "before_labels": sorted(set(before_labels)),
        "after_labels": sorted(set(after_labels)),
        "api_call_count": 0,
    }


def _runtime_version_drift_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    reapproval_scope = report.get("reapproval_scope") if isinstance(report.get("reapproval_scope"), dict) else {}
    current_versions = report.get("current_versions") if isinstance(report.get("current_versions"), dict) else {}
    vector_integrity = report.get("vector_integrity") if isinstance(report.get("vector_integrity"), dict) else {}
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "current_chunker_version": str(current_versions.get("chunker_version") or ""),
        "approved_repository_chunk_count": _int(report.get("approved_repository_chunk_count")),
        "vector_record_count": _int(report.get("vector_record_count")),
        "approved_repository_stale_chunker_count": _int(report.get("approved_repository_stale_chunker_count")),
        "approved_repository_stale_chunker_ratio": _optional_float(report.get("approved_repository_stale_chunker_ratio")),
        "vector_stale_chunker_count": _int(report.get("vector_stale_chunker_count")),
        "vector_stale_chunker_ratio": _optional_float(report.get("vector_stale_chunker_ratio")),
        "version_loss_count": _int((report.get("version_loss") or {}).get("loss_count") if isinstance(report.get("version_loss"), dict) else None),
        "version_mismatch_count": _int((report.get("version_loss") or {}).get("mismatch_count") if isinstance(report.get("version_loss"), dict) else None),
        "vector_integrity_failure_count": _int(vector_integrity.get("failure_count")),
        "vector_integrity_content_hash_mismatch_count": _int(vector_integrity.get("content_hash_mismatch_count")),
        "vector_integrity_verification_hash_mismatch_count": _int(vector_integrity.get("verification_hash_mismatch_count")),
        "vector_integrity_metadata_missing_required_count": _int(vector_integrity.get("metadata_missing_required_count")),
        "vector_integrity_invalid_approval_status_count": _int(vector_integrity.get("invalid_approval_status_count")),
        "vector_integrity_invalid_security_level_count": _int(vector_integrity.get("invalid_security_level_count")),
        "vector_integrity_embedded_dimension_mismatch_count": _int(vector_integrity.get("embedded_dimension_mismatch_count")),
        "vector_integrity_embedded_failure_count": _int(vector_integrity.get("embedded_integrity_failure_count")),
        "vector_integrity_local_path_leak_count": _int(vector_integrity.get("local_path_leak_count")),
        "reprocess_requires_reapproval": bool(reapproval_scope.get("reprocess_requires_reapproval")),
        "approved_chunks_with_stale_chunker_count": _int(reapproval_scope.get("approved_chunks_with_stale_chunker_count")),
        "approved_chunks_with_approved_hash_count": _int(reapproval_scope.get("approved_chunks_with_approved_hash_count")),
        "blocker_count": _int(report.get("blocker_count")),
        "warning_count": _int(report.get("warning_count")),
        "finding_count": _int(report.get("finding_count")),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _approval_workload_summary(reports: list[Any]) -> dict[str, Any]:
    payloads = [report for report in reports if isinstance(report, dict)]
    if not payloads:
        return {}
    document_rows: list[dict[str, Any]] = []
    for report in payloads:
        rows = report.get("documents")
        if isinstance(rows, list):
            document_rows.extend(row for row in rows if isinstance(row, dict))
    document_total_chunks = sum(_int(row.get("total_chunks")) for row in document_rows)
    pending_approval_chunks = sum(_int(row.get("pending_approval_chunks")) for row in document_rows)
    approved_chunks = sum(_int(row.get("approved_chunks")) for row in document_rows)
    draft_chunks = sum(_int(row.get("draft_chunks")) for row in document_rows)
    needs_review_chunks = sum(_int(row.get("needs_review_chunks")) for row in document_rows)
    document_action_counts: Counter[str] = Counter(
        str(row.get("suggested_action") or "").strip()
        for row in document_rows
        if str(row.get("suggested_action") or "").strip()
    )
    manual_review_first_chunks = sum(
        _int(row.get("pending_approval_chunks")) or _int(row.get("total_chunks"))
        for row in document_rows
        if str(row.get("suggested_action") or "").strip() == "manual_review_first"
    )
    low_risk_document_chunks = sum(
        _int(row.get("pending_approval_chunks")) or _int(row.get("total_chunks"))
        for row in document_rows
        if str(row.get("suggested_action") or "").strip()
        in {"bulk_review_first", "bulk_review", "low_risk_batch_review", "bulk_approve"}
    )
    totals = {
        "document_count": sum(_int(report.get("document_count")) for report in payloads) or len(document_rows),
        "total_chunks": sum(_int(report.get("total_chunks")) for report in payloads) or document_total_chunks,
        "approved_chunks": sum(_int(report.get("approved_chunks")) for report in payloads) or approved_chunks,
        "draft_chunks": sum(_int(report.get("draft_chunks")) for report in payloads) or draft_chunks,
        "needs_review_chunks": (
            sum(_int(report.get("needs_review_chunks")) for report in payloads) or needs_review_chunks
        ),
        "pending_approval_chunks": (
            sum(_int(report.get("pending_approval_chunks")) for report in payloads) or pending_approval_chunks
        ),
        "manual_attention_chunks": max(
            sum(_int(report.get("manual_attention_chunks")) for report in payloads),
            manual_review_first_chunks,
        ),
        "bulk_review_candidate_chunks": sum(_int(report.get("bulk_review_candidate_chunks")) for report in payloads),
        "low_risk_batch_review_candidate_chunks": max(
            sum(_int(report.get("low_risk_batch_review_candidate_chunks")) for report in payloads),
            low_risk_document_chunks,
        ),
        "blocking_review_chunks": sum(_int(report.get("blocking_review_chunks")) for report in payloads),
        "domain_attention_chunks": sum(_int(report.get("domain_attention_chunks")) for report in payloads),
        "stable_false_positive_chunks": sum(_int(report.get("stable_false_positive_chunks")) for report in payloads),
        "informational_chunks": sum(_int(report.get("informational_chunks")) for report in payloads),
        "no_signal_chunks": sum(_int(report.get("no_signal_chunks")) for report in payloads),
    }
    tier_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    for report in payloads:
        raw_tiers = report.get("review_priority_tier_counts")
        if isinstance(raw_tiers, dict):
            tier_counts.update({str(key): _int(value) for key, value in raw_tiers.items()})
        raw_actions = report.get("action_counts")
        if isinstance(raw_actions, dict):
            action_counts.update({str(key): _int(value) for key, value in raw_actions.items()})
    action_counts.update(document_action_counts)
    return {
        "report_type": "approval_workload_summary",
        "report_count": len(payloads),
        **totals,
        "manual_attention_rate": _percent(totals["manual_attention_chunks"], totals["total_chunks"]),
        "low_risk_batch_review_candidate_rate": _percent(
            totals["low_risk_batch_review_candidate_chunks"],
            totals["total_chunks"],
        ),
        "review_priority_tier_counts": dict(sorted(tier_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "document_suggested_action_counts": dict(sorted(document_action_counts.items())),
        "manual_review_first_document_count": document_action_counts.get("manual_review_first", 0),
        "api_call_count": 0,
    }


def _approval_review_batch_summary(reports: list[Any]) -> dict[str, Any]:
    payloads = [report for report in reports if isinstance(report, dict)]
    if not payloads:
        return {}
    review_type_batch_counts: Counter[str] = Counter()
    for report in payloads:
        raw_counts = report.get("review_type_batch_counts")
        if isinstance(raw_counts, dict):
            review_type_batch_counts.update({str(key): _int(value) for key, value in raw_counts.items()})
    return {
        "report_type": "approval_review_batch_summary",
        "report_count": len(payloads),
        "batch_count": sum(_int(report.get("batch_count")) for report in payloads),
        "approval_chunk_count": sum(_int(report.get("approval_chunk_count")) for report in payloads),
        "manual_attention_chunks": sum(_int(report.get("manual_attention_chunks")) for report in payloads),
        "low_risk_batch_review_candidate_chunks": sum(
            _int(report.get("low_risk_batch_review_candidate_chunks")) for report in payloads
        ),
        "blocker_count": sum(_int(report.get("blocker_count")) for report in payloads),
        "warning_count": sum(_int(report.get("warning_count")) for report in payloads),
        "passed": all(bool(report.get("passed")) for report in payloads),
        "review_type_batch_counts": dict(sorted(review_type_batch_counts.items())),
        "api_call_count": 0,
    }


def _reapproval_workload_summary(reports: list[Any]) -> dict[str, Any]:
    payloads = [report for report in reports if isinstance(report, dict)]
    if not payloads:
        return {}
    totals = {
        "document_count": sum(_int(report.get("document_count")) for report in payloads),
        "reapproval_candidate_chunks": sum(_int(report.get("reapproval_candidate_chunks")) for report in payloads),
        "high_risk_candidate_chunks": sum(_int(report.get("high_risk_candidate_chunks")) for report in payloads),
        "temporal_sample_candidate_chunks": sum(
            _int(report.get("temporal_sample_candidate_chunks")) for report in payloads
        ),
        "low_risk_candidate_chunks": sum(_int(report.get("low_risk_candidate_chunks")) for report in payloads),
        "recommended_initial_review_chunks": sum(
            _int(report.get("recommended_initial_review_chunks")) for report in payloads
        ),
        "estimated_initial_review_minutes": sum(
            _int(report.get("estimated_initial_review_minutes")) for report in payloads
        ),
        "approval_provenance_missing_chunks": sum(
            _int(report.get("approval_provenance_missing_chunks")) for report in payloads
        ),
        "approval_provenance_only_chunks": sum(
            _int(report.get("approval_provenance_only_chunks")) for report in payloads
        ),
        "source_vector_integrity_failure_count": sum(
            _int(report.get("source_vector_integrity_failure_count")) for report in payloads
        ),
    }
    blocker_count = 0
    strategy_counts: Counter[str] = Counter()
    approval_provenance_missing_field_counts: Counter[str] = Counter()
    for report in payloads:
        blockers = report.get("pre_reapproval_blockers")
        if isinstance(blockers, list):
            blocker_count += len(blockers)
        strategy = str(report.get("review_strategy") or "").strip()
        if strategy:
            strategy_counts[strategy] += 1
        missing_fields = report.get("approval_provenance_missing_field_counts")
        if isinstance(missing_fields, dict):
            approval_provenance_missing_field_counts.update(
                {str(field): _int(count) for field, count in missing_fields.items()}
            )
    return {
        "report_type": "reapproval_workload_summary",
        "report_count": len(payloads),
        **totals,
        "approval_provenance_missing_field_counts": dict(
            sorted(approval_provenance_missing_field_counts.items())
        ),
        "pre_reapproval_blocker_count": blocker_count,
        "initial_review_reduction_ratio": _ratio(
            max(totals["reapproval_candidate_chunks"] - totals["recommended_initial_review_chunks"], 0),
            totals["reapproval_candidate_chunks"],
        ),
        "review_strategy_counts": dict(sorted(strategy_counts.items())),
        "api_call_count": 0,
    }


def _reapproval_review_batch_summary(reports: list[Any]) -> dict[str, Any]:
    payloads = [report for report in reports if isinstance(report, dict)]
    if not payloads:
        return {}
    action_batch_counts: Counter[str] = Counter()
    action_chunk_counts: Counter[str] = Counter()
    risk_tier_batch_counts: Counter[str] = Counter()
    risk_tier_chunk_counts: Counter[str] = Counter()
    for report in payloads:
        for source_key, counter in (
            ("action_batch_counts", action_batch_counts),
            ("action_chunk_counts", action_chunk_counts),
            ("risk_tier_batch_counts", risk_tier_batch_counts),
            ("risk_tier_chunk_counts", risk_tier_chunk_counts),
        ):
            raw_counts = report.get(source_key)
            if isinstance(raw_counts, dict):
                counter.update({str(key): _int(value) for key, value in raw_counts.items()})
    return {
        "report_type": "reapproval_review_batch_summary",
        "report_count": len(payloads),
        "candidate_count": sum(_int(report.get("candidate_count")) for report in payloads),
        "selected_candidate_count": sum(_int(report.get("selected_candidate_count")) for report in payloads),
        "batch_count": sum(_int(report.get("batch_count")) for report in payloads),
        "reapproval_chunk_count": sum(_int(report.get("reapproval_chunk_count")) for report in payloads),
        "blocker_count": sum(_int(report.get("blocker_count")) for report in payloads),
        "warning_count": sum(_int(report.get("warning_count")) for report in payloads),
        "passed": all(bool(report.get("passed")) for report in payloads),
        "max_chunks_per_batch": max((_int(report.get("max_chunks_per_batch")) for report in payloads), default=0),
        "action_batch_counts": dict(sorted(action_batch_counts.items())),
        "action_chunk_counts": dict(sorted(action_chunk_counts.items())),
        "risk_tier_batch_counts": dict(sorted(risk_tier_batch_counts.items())),
        "risk_tier_chunk_counts": dict(sorted(risk_tier_chunk_counts.items())),
        "api_call_count": 0,
    }


def _reapproval_decision_validation_summary(reports: list[Any]) -> dict[str, Any]:
    payloads = [report for report in reports if isinstance(report, dict)]
    if not payloads:
        return {}
    status_counts: Counter[str] = Counter()
    operator_decision_counts: Counter[str] = Counter()
    for report in payloads:
        status = str(report.get("release_gate_status") or "").strip()
        if status:
            status_counts[status] += 1
        raw_counts = report.get("operator_decision_counts")
        if isinstance(raw_counts, dict):
            operator_decision_counts.update(
                {str(key): _int(value) for key, value in raw_counts.items()}
            )
    return {
        "report_type": "reapproval_decision_validation_summary",
        "report_count": len(payloads),
        "expected_batch_count": sum(_int(report.get("expected_batch_count")) for report in payloads),
        "decision_row_count": sum(_int(report.get("decision_row_count")) for report in payloads),
        "complete_row_count": sum(_int(report.get("complete_row_count")) for report in payloads),
        "blank_or_incomplete_row_count": sum(
            _int(report.get("blank_or_incomplete_row_count")) for report in payloads
        ),
        "blocking_count": sum(_int(report.get("blocking_count")) for report in payloads),
        "warning_count": sum(_int(report.get("warning_count")) for report in payloads),
        "passed": all(bool(report.get("passed")) for report in payloads),
        "release_gate_status_counts": dict(sorted(status_counts.items())),
        "operator_decision_counts": dict(sorted(operator_decision_counts.items())),
        "api_call_count": 0,
    }


def _reapproval_apply_plan_summary(reports: list[Any]) -> dict[str, Any]:
    payloads = [report for report in reports if isinstance(report, dict)]
    if not payloads:
        return {}
    status_counts: Counter[str] = Counter(str(report.get("release_gate_status") or "") for report in payloads)
    required_steps = {
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
    }
    unsafe_contract_violation_count = 0
    execution_step_counts: Counter[str] = Counter()
    batch_apply_control_count = 0
    direct_metadata_write_allowed_count = 0
    mcp_publish_allowed_count = 0
    batch_requires_shared_contract_count = 0
    batch_requires_explicit_reindex_phase_count = 0
    batch_conditional_vector_sync_guard_count = 0
    for report in payloads:
        operator_controls = report.get("operator_controls") if isinstance(report.get("operator_controls"), dict) else {}
        if operator_controls.get("direct_approval_metadata_write_allowed") is not False:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_shared_review_workflow_contract") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_tenant_and_operator_access_control") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_approval_precondition_validation") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_rejection_decision_validation") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_preapproval_security_scan") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_review_flag_acknowledgement") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_approved_content_hash_recalculation") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_apply_audit_event") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_vector_sync_or_explicit_reindex") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("requires_explicit_reindex_phase_by_default") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("conditional_vector_sync_requires_existing_successful_index") is not True:
            unsafe_contract_violation_count += 1
        if operator_controls.get("official_mcp_publish_allowed_by_this_plan") is not False:
            unsafe_contract_violation_count += 1
        steps = {
            str(item.get("step") or "")
            for item in report.get("execution_requirements") or []
            if isinstance(item, dict)
        }
        execution_step_counts.update(step for step in steps if step)
        missing_steps = required_steps - steps
        unsafe_contract_violation_count += len(missing_steps)
        for plan in report.get("batch_plans") or []:
            if not isinstance(plan, dict):
                continue
            controls = plan.get("apply_controls") if isinstance(plan.get("apply_controls"), dict) else {}
            if not controls:
                unsafe_contract_violation_count += 1
                continue
            batch_apply_control_count += 1
            if controls.get("direct_metadata_write_allowed") is not False:
                direct_metadata_write_allowed_count += 1
                unsafe_contract_violation_count += 1
            has_approval = bool(plan.get("approve_chunk_ids"))
            has_review_mutation = bool(plan.get("approve_chunk_ids") or plan.get("reject_chunk_ids"))
            if has_review_mutation and controls.get("requires_tenant_and_operator_access_control") is not True:
                unsafe_contract_violation_count += 1
            if controls.get("official_mcp_publish_allowed_by_batch_plan") is not False:
                mcp_publish_allowed_count += 1
                unsafe_contract_violation_count += 1
            if has_approval and controls.get("approval_requires_precondition_validation") is not True:
                unsafe_contract_violation_count += 1
            has_rejection = bool(plan.get("reject_chunk_ids"))
            if has_rejection and controls.get("rejection_requires_reason_validation") is not True:
                unsafe_contract_violation_count += 1
            if has_review_mutation and controls.get("requires_apply_audit_event") is not True:
                unsafe_contract_violation_count += 1
            if controls.get("requires_shared_review_workflow_contract") is True:
                batch_requires_shared_contract_count += 1
            requires_reindex = bool(plan.get("requires_reindex"))
            if controls.get("requires_explicit_reindex_phase") is True:
                batch_requires_explicit_reindex_phase_count += 1
            if controls.get("conditional_vector_sync_allowed_only_after_successful_index") is True:
                batch_conditional_vector_sync_guard_count += 1
            if requires_reindex and controls.get("requires_explicit_reindex_phase") is not True:
                unsafe_contract_violation_count += 1
            if (
                requires_reindex
                and controls.get("conditional_vector_sync_allowed_only_after_successful_index") is not True
            ):
                unsafe_contract_violation_count += 1
    return {
        "report_type": "reapproval_apply_plan_summary",
        "report_count": len(payloads),
        "passed": all(bool(report.get("passed")) for report in payloads),
        "blocker_count": sum(_int(report.get("blocker_count")) for report in payloads),
        "ready_plan_count": status_counts.get("ready_for_apply_execution", 0),
        "release_gate_status_counts": dict(sorted(status_counts.items())),
        "batch_count": sum(_int((report.get("summary") or {}).get("batch_count")) for report in payloads),
        "approve_chunk_count": sum(_int((report.get("summary") or {}).get("approve_chunk_count")) for report in payloads),
        "reject_chunk_count": sum(_int((report.get("summary") or {}).get("reject_chunk_count")) for report in payloads),
        "reprocess_chunk_count": sum(
            _int((report.get("summary") or {}).get("reprocess_chunk_count")) for report in payloads
        ),
        "defer_chunk_count": sum(_int((report.get("summary") or {}).get("defer_chunk_count")) for report in payloads),
        "batch_apply_control_count": batch_apply_control_count,
        "batch_requires_shared_review_workflow_contract_count": batch_requires_shared_contract_count,
        "batch_requires_explicit_reindex_phase_count": batch_requires_explicit_reindex_phase_count,
        "batch_conditional_vector_sync_guard_count": batch_conditional_vector_sync_guard_count,
        "direct_metadata_write_allowed_count": direct_metadata_write_allowed_count,
        "mcp_publish_allowed_count": mcp_publish_allowed_count,
        "required_execution_steps": sorted(required_steps),
        "observed_execution_step_counts": dict(sorted(execution_step_counts.items())),
        "unsafe_contract_violation_count": unsafe_contract_violation_count,
        "api_call_count": 0,
    }


def _temporal_coverage_warning_detail(
    runtime_summary: dict[str, Any],
    temporal_coverage_summary: dict[str, Any],
    temporal_backfill_shadow_summary: dict[str, Any],
) -> str:
    detail = (
        f"Approved vector temporal metadata coverage is partial "
        f"({runtime_summary.get('temporal_metadata_count')}/{runtime_summary.get('vector_record_count')}, "
        f"ratio={runtime_summary.get('temporal_metadata_ratio')})."
    )
    if temporal_coverage_summary:
        detail += (
            " Coverage audit: "
            f"{temporal_coverage_summary.get('with_temporal_metadata_count')}/"
            f"{temporal_coverage_summary.get('record_count')} records have temporal metadata, "
            f"candidate_missing_record_count={temporal_coverage_summary.get('candidate_missing_record_count')}."
        )
    if temporal_backfill_shadow_summary:
        detail += (
            " Shadow backfill evidence: "
            f"delta_temporal_metadata_count={temporal_backfill_shadow_summary.get('delta_temporal_metadata_count')}, "
            f"after_ratio={temporal_backfill_shadow_summary.get('after_temporal_metadata_ratio')}, "
            f"conflict_chunk_count={temporal_backfill_shadow_summary.get('conflict_chunk_count')}, "
            f"ambiguous_chunk_count={temporal_backfill_shadow_summary.get('ambiguous_chunk_count')}, "
            f"write_blocked={str(temporal_backfill_shadow_summary.get('write_blocked')).lower()}, "
            f"shadow_runtime_written={str(temporal_backfill_shadow_summary.get('shadow_runtime_written')).lower()}."
        )
    return detail


def _gate(label: str, findings: list[dict[str, str]]) -> dict[str, Any]:
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    return {
        "label": label,
        "passed": blocker_count == 0,
        "status": "ready" if not findings else ("blocked" if blocker_count else "needs_review"),
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "findings": findings,
    }


def _finding(severity: str, code: str, detail: str, *, static_detail: bool = True) -> dict[str, str]:
    resolved_detail = FINDING_DETAILS.get(code, detail) if static_detail else detail
    return {"severity": severity, "code": code, "detail": resolved_detail}


def _source_report_artifacts(
    *,
    report_generated_at: datetime,
    batch_reports: list[Path],
    batch_payloads: list[Any],
    public_readiness_report: Path | None,
    public_readiness: Any,
    parser_goldset_score_report: Path | None,
    parser_goldset_score: Any,
    parser_goldset_completion_board_report: Path | None,
    parser_goldset_completion_board: Any,
    table_preprocessing_claim_gate_report: Path | None,
    table_preprocessing_claim_gate: Any,
    rag_eval_report: Path | None,
    rag_eval: Any,
    mcp_demo_answer_report: Path | None,
    mcp_demo_answers: Any,
    accuracy_comparison_report: Path | None,
    accuracy_comparison: Any,
    profile_provenance_report: Path | None,
    profile_provenance: Any,
    mcp_readiness_report: Path | None,
    mcp_readiness: Any,
    mcp_transport_smoke_report: Path | None,
    mcp_transport_smoke: Any,
    temporal_coverage_report: Path | None,
    temporal_coverage: Any,
    temporal_backfill_shadow_report: Path | None,
    temporal_backfill_shadow: Any,
    temporal_ambiguity_scope_report: Path | None,
    temporal_ambiguity_scope: Any,
    temporal_ambiguity_policy_decision_validation_report: Path | None,
    temporal_ambiguity_policy_decision_validation: Any,
    revision_impact_reports: list[Path],
    revision_impacts: list[Any],
    runtime_version_drift_report: Path | None,
    runtime_version_drift: Any,
    approval_worklist_reports: list[Path],
    approval_worklists: list[Any],
    approval_review_batch_reports: list[Path],
    approval_review_batches: list[Any],
    reapproval_worklist_reports: list[Path],
    reapproval_worklists: list[Any],
    reapproval_review_batch_reports: list[Path],
    reapproval_review_batches: list[Any],
    reapproval_decision_validation_reports: list[Path],
    reapproval_decision_validations: list[Any],
    reapproval_apply_plan_reports: list[Path],
    reapproval_apply_plans: list[Any],
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for index, (path, payload) in enumerate(zip(batch_reports, batch_payloads)):
        artifacts.append(
            _source_report_artifact(
                role="batch_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(zip(approval_worklist_reports, approval_worklists)):
        artifacts.append(
            _source_report_artifact(
                role="approval_worklist_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(zip(approval_review_batch_reports, approval_review_batches)):
        artifacts.append(
            _source_report_artifact(
                role="approval_review_batch_manifest_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(zip(reapproval_worklist_reports, reapproval_worklists)):
        artifacts.append(
            _source_report_artifact(
                role="reapproval_worklist_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(zip(reapproval_review_batch_reports, reapproval_review_batches)):
        artifacts.append(
            _source_report_artifact(
                role="reapproval_review_batch_manifest_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(
        zip(reapproval_decision_validation_reports, reapproval_decision_validations)
    ):
        artifacts.append(
            _source_report_artifact(
                role="reapproval_decision_validation_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(zip(reapproval_apply_plan_reports, reapproval_apply_plans)):
        artifacts.append(
            _source_report_artifact(
                role="reapproval_apply_plan_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for index, (path, payload) in enumerate(zip(revision_impact_reports, revision_impacts)):
        artifacts.append(
            _source_report_artifact(
                role="revision_impact_report",
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
                index=index,
            )
        )
    for role, path, payload in (
        ("public_readiness_report", public_readiness_report, public_readiness),
        ("parser_goldset_score_report", parser_goldset_score_report, parser_goldset_score),
        (
            "parser_goldset_completion_board_report",
            parser_goldset_completion_board_report,
            parser_goldset_completion_board,
        ),
        (
            "table_preprocessing_claim_gate_report",
            table_preprocessing_claim_gate_report,
            table_preprocessing_claim_gate,
        ),
        ("rag_eval_report", rag_eval_report, rag_eval),
        ("mcp_demo_answer_report", mcp_demo_answer_report, mcp_demo_answers),
        ("accuracy_comparison_report", accuracy_comparison_report, accuracy_comparison),
        ("profile_provenance_report", profile_provenance_report, profile_provenance),
        ("mcp_readiness_report", mcp_readiness_report, mcp_readiness),
        ("mcp_transport_smoke_report", mcp_transport_smoke_report, mcp_transport_smoke),
        ("temporal_coverage_report", temporal_coverage_report, temporal_coverage),
        ("temporal_backfill_shadow_report", temporal_backfill_shadow_report, temporal_backfill_shadow),
        ("temporal_ambiguity_scope_report", temporal_ambiguity_scope_report, temporal_ambiguity_scope),
        (
            "temporal_ambiguity_policy_decision_validation_report",
            temporal_ambiguity_policy_decision_validation_report,
            temporal_ambiguity_policy_decision_validation,
        ),
        ("runtime_version_drift_report", runtime_version_drift_report, runtime_version_drift),
    ):
        if path is None:
            continue
        artifacts.append(
            _source_report_artifact(
                role=role,
                path=path,
                payload=payload,
                report_generated_at=report_generated_at,
            )
        )
    return artifacts


def _source_report_artifact(
    *,
    role: str,
    path: Path,
    payload: Any,
    report_generated_at: datetime,
    index: int | None = None,
) -> dict[str, Any]:
    raw = path.read_bytes()
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    payload_generated_at = _payload_generated_at(payload)
    payload_generated_dt = _parse_datetime(payload_generated_at)
    artifact = {
        "role": role,
        "path": _display_path(path),
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "modified_at": modified_at.isoformat(),
        "artifact_age_hours": _age_hours(modified_at, report_generated_at),
        "payload_generated_at": payload_generated_at,
        "payload_age_hours": _age_hours(payload_generated_dt, report_generated_at) if payload_generated_dt else None,
    }
    if index is not None:
        artifact["index"] = index
    scope = _source_report_scope(payload)
    if scope:
        artifact["scope"] = scope
    return artifact


def _source_report_scope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    fields = (
        "runtime_data_dir",
        "effective_runtime_data_dir",
        "source_runtime_data_dir",
        "source_effective_runtime_data_dir",
        "data_dir",
        "effective_data_dir",
        "tenant_id",
        "repository_chunk_count",
        "approved_repository_chunk_count",
        "total_chunks",
        "approved_chunks",
        "total_approved_chunks",
        "vector_record_count",
        "record_count",
        "source_chunk_count",
    )
    scope = {
        field: payload[field]
        for field in fields
        if field in payload and payload[field] is not None and str(payload[field]).strip()
    }
    for nested_key in ("worklist_report", "source_report"):
        nested = payload.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for field in fields:
            if field not in scope and nested.get(field) is not None and str(nested[field]).strip():
                scope[field] = nested[field]
    return scope


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _source_report_artifact_summary(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    payload_datetimes = [
        parsed
        for parsed in (_parse_datetime(str(item.get("payload_generated_at") or "")) for item in artifacts)
        if parsed is not None
    ]
    summary = {
        "provided_count": len(artifacts),
        "role_counts": dict(sorted(Counter(str(item.get("role") or "") for item in artifacts).items())),
        "sha256_count": len({str(item.get("sha256") or "") for item in artifacts if item.get("sha256")}),
        "payload_generated_at_count": len(payload_datetimes),
        "missing_payload_generated_at_count": max(len(artifacts) - len(payload_datetimes), 0),
        "oldest_payload_generated_at": min(payload_datetimes).isoformat() if payload_datetimes else "",
        "newest_payload_generated_at": max(payload_datetimes).isoformat() if payload_datetimes else "",
        "max_payload_age_hours": max(
            (float(item["payload_age_hours"]) for item in artifacts if item.get("payload_age_hours") is not None),
            default=None,
        ),
    }
    if len(payload_datetimes) >= 2:
        summary["payload_generated_at_span_hours"] = round(
            (max(payload_datetimes) - min(payload_datetimes)).total_seconds() / 3600,
            3,
        )
    else:
        summary["payload_generated_at_span_hours"] = 0.0
    return summary


def _temporal_evidence_sources(
    *,
    temporal_coverage_report: Path | None,
    temporal_coverage: Any,
    temporal_backfill_shadow_report: Path | None,
    temporal_backfill_shadow: Any,
    temporal_ambiguity_scope_report: Path | None,
    temporal_ambiguity_scope: Any,
    temporal_ambiguity_policy_decision_validation_report: Path | None,
    temporal_ambiguity_policy_decision_validation: Any,
    revision_impact_reports: list[Path],
    revision_impacts: list[Any],
    runtime_version_drift_report: Path | None,
    runtime_version_drift: Any,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for role, path, payload in (
        ("temporal_coverage_report", temporal_coverage_report, temporal_coverage),
        ("temporal_backfill_shadow_report", temporal_backfill_shadow_report, temporal_backfill_shadow),
        ("temporal_ambiguity_scope_report", temporal_ambiguity_scope_report, temporal_ambiguity_scope),
        (
            "temporal_ambiguity_policy_decision_validation_report",
            temporal_ambiguity_policy_decision_validation_report,
            temporal_ambiguity_policy_decision_validation,
        ),
        ("runtime_version_drift_report", runtime_version_drift_report, runtime_version_drift),
    ):
        if path is not None:
            sources.append({"role": role, "path": _display_path(path), "payload": payload})
    for index, (path, payload) in enumerate(zip(revision_impact_reports, revision_impacts)):
        sources.append(
            {
                "role": "revision_impact_report",
                "path": _display_path(path),
                "payload": payload,
                "index": index,
            }
        )
    return sources


def _temporal_evidence_guard_summary(
    *,
    runtime_data_dir: Path,
    effective_runtime_data_dir: Path,
    source_report_artifacts: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    max_source_report_age_hours: float | None,
    strict_temporal_evidence: bool,
) -> dict[str, Any]:
    if not sources:
        return {}
    source_roles = {str(source.get("role") or "") for source in sources}
    artifacts = [
        artifact
        for artifact in source_report_artifacts
        if str(artifact.get("role") or "") in source_roles
    ]
    threshold = _positive_float(max_source_report_age_hours)
    stale_artifacts: list[dict[str, Any]] = []
    if threshold is not None:
        for artifact in artifacts:
            age_value = artifact.get("payload_age_hours")
            age_basis = "payload_generated_at"
            if age_value is None:
                age_value = artifact.get("artifact_age_hours")
                age_basis = "modified_at"
            age_hours = _float_or_none(age_value)
            if age_hours is not None and age_hours > threshold:
                stale_artifacts.append(
                    {
                        "role": artifact.get("role"),
                        "path": artifact.get("path"),
                        "age_basis": age_basis,
                        "age_hours": age_hours,
                        "max_source_report_age_hours": threshold,
                    }
                )
    payload_datetimes = [
        parsed
        for parsed in (_parse_datetime(str(item.get("payload_generated_at") or "")) for item in artifacts)
        if parsed is not None
    ]
    payload_span_hours = 0.0
    if len(payload_datetimes) >= 2:
        payload_span_hours = round(
            (max(payload_datetimes) - min(payload_datetimes)).total_seconds() / 3600,
            3,
        )
    payload_span_exceeds_threshold = bool(threshold is not None and payload_span_hours > threshold)

    expected_runtime = _normalize_lineage_path(str(runtime_data_dir))
    expected_effective = _normalize_lineage_path(str(effective_runtime_data_dir))
    lineage_values: list[dict[str, str]] = []
    lineage_mismatches: list[dict[str, str]] = []
    for source in sources:
        payload = source.get("payload")
        if not isinstance(payload, dict):
            continue
        for key, expected in (
            ("runtime_data_dir", expected_runtime),
            ("effective_runtime_data_dir", expected_effective),
            ("source_runtime_data_dir", expected_runtime),
            ("source_effective_runtime_data_dir", expected_effective),
        ):
            raw_value = str(payload.get(key) or "").strip()
            if not raw_value:
                continue
            normalized = _normalize_lineage_path(raw_value)
            lineage_row = {
                "role": str(source.get("role") or ""),
                "path": str(source.get("path") or ""),
                "field": key,
                "value": raw_value,
                "normalized_value": normalized,
            }
            lineage_values.append(lineage_row)
            if normalized != expected:
                lineage_mismatches.append(
                    {
                        **lineage_row,
                        "expected_normalized_value": expected,
                    }
                )

    return {
        "report_type": "temporal_evidence_guard",
        "source_count": len(sources),
        "checked_roles": sorted(source_roles),
        "expected_runtime_data_dir": str(runtime_data_dir),
        "expected_effective_runtime_data_dir": str(effective_runtime_data_dir),
        "max_source_report_age_hours": threshold,
        "strict_temporal_evidence": strict_temporal_evidence,
        "stale_artifact_count": len(stale_artifacts),
        "stale_artifacts": stale_artifacts,
        "payload_generated_at_count": len(payload_datetimes),
        "payload_generated_at_span_hours": payload_span_hours,
        "payload_generated_at_span_exceeds_threshold": payload_span_exceeds_threshold,
        "runtime_lineage_value_count": len(lineage_values),
        "runtime_lineage_values": lineage_values,
        "runtime_lineage_mismatch_count": len(lineage_mismatches),
        "runtime_lineage_mismatches": lineage_mismatches,
        "passed": not stale_artifacts and not payload_span_exceeds_threshold and not lineage_mismatches,
        "api_call_count": 0,
    }


def _payload_generated_at(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("generated_at", "generated_from", "created_at", "report_generated_at", "built_at", "checked_at"):
        if payload.get(key):
            return str(payload[key])
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    for key in ("generated_at", "generated_from", "created_at", "report_generated_at", "built_at", "checked_at"):
        if metadata.get(key):
            return str(metadata[key])
    return ""


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: datetime | None, reference: datetime) -> float | None:
    if value is None:
        return None
    return round((reference - value).total_seconds() / 3600, 3)


def _positive_float(value: Any) -> float | None:
    parsed = _float_or_none(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return round(float(str(value)), 3)
    except (TypeError, ValueError):
        return None


def _normalize_lineage_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False)).lower()
    except (OSError, ValueError):
        return text.replace("\\", "/").rstrip("/").lower()


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MCP Product Readiness",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Runtime: `{report.get('effective_runtime_data_dir')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blocking: {report.get('blocking_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- API calls: {report.get('api_call_count')}",
        "",
    ]
    source_artifacts = report.get("source_report_artifacts") or []
    if source_artifacts:
        source_summary = report.get("source_report_artifact_summary") or {}
        lines.extend(
            [
                "## Source Report Artifacts",
                "",
                f"- Provided reports: {source_summary.get('provided_count')}",
                f"- Payload generated_at present: {source_summary.get('payload_generated_at_count')} / {source_summary.get('provided_count')}",
                f"- Payload generated_at span hours: {source_summary.get('payload_generated_at_span_hours')}",
                f"- Max payload age hours: {source_summary.get('max_payload_age_hours')}",
                "",
            ]
        )
        for artifact in source_artifacts:
            sha = str(artifact.get("sha256") or "")
            label = artifact.get("role")
            if artifact.get("index") is not None:
                label = f"{label}[{artifact.get('index')}]"
            lines.extend(
                [
                    f"- `{label}`: `{artifact.get('path')}`",
                    f"  - sha256: `{sha}`",
                    f"  - bytes: {artifact.get('byte_count')}",
                    f"  - payload generated_at: {artifact.get('payload_generated_at') or '-'}",
                ]
            )
        lines.append("")
    lines.extend(["## Gates", ""])
    for key, gate in (report.get("gates") or {}).items():
        lines.extend(
            [
                f"### {gate.get('label')} (`{key}`)",
                "",
                f"- Status: `{gate.get('status')}`",
                f"- Blockers: {gate.get('blocker_count')}",
                f"- Warnings: {gate.get('warning_count')}",
            ]
        )
        findings = gate.get("findings") or []
        if findings:
            lines.append("")
            for item in findings:
                lines.append(f"- {item.get('severity')} `{item.get('code')}`: {item.get('detail')}")
        lines.append("")
    summary = report.get("runtime_summary") or {}
    approval_provenance = (
        summary.get("approval_provenance_coverage")
        if isinstance(summary.get("approval_provenance_coverage"), dict)
        else {}
    )
    approval_journal_events = (
        summary.get("approval_journal_review_event_coverage")
        if isinstance(summary.get("approval_journal_review_event_coverage"), dict)
        else {}
    )
    lines.extend(
        [
            "## Runtime Summary",
            "",
            f"- Repository chunks: {summary.get('repository_chunk_count')}",
            f"- Vector records: {summary.get('vector_record_count')}",
            f"- Full index match: {summary.get('full_index_match')}",
            f"- Articles: {summary.get('article_like_count')}",
            f"- Appendices: {summary.get('appendix_count')}",
            f"- Supplementary provisions: {summary.get('supplementary_count')}",
            f"- Table-like chunks: {summary.get('table_like_count')}",
            f"- Review attention chunks: {summary.get('review_attention_chunk_count')}",
            f"- Review attention acknowledged / unacknowledged: {summary.get('review_attention_acknowledged_chunk_count')} / {summary.get('review_attention_unacknowledged_chunk_count')}",
            f"- Review attention samples: {', '.join(summary.get('review_attention_sample_chunk_ids') or []) or '-'}",
            f"- Approval metadata complete ratio: {summary.get('approval_metadata_complete_ratio')}",
            f"- Approval provenance complete: {approval_provenance.get('complete_record_count')} / {approval_provenance.get('record_count')} ({approval_provenance.get('complete_ratio')})",
            f"- Approval provenance missing counts: `{approval_provenance.get('missing_field_counts') or {}}`",
            f"- Approval journal review events: records={approval_journal_events.get('applicable_record_count')} chunks={approval_journal_events.get('chunk_reference_count')} events={approval_journal_events.get('review_decision_event_count')}",
            f"- Approval journal missing review-event chunks: `{approval_journal_events.get('missing_event_chunk_counts') or {}}`",
            f"- Answer profile ratio: {summary.get('answer_profile_ratio')}",
            f"- Temporal metadata ratio: {summary.get('temporal_metadata_ratio')}",
            "",
        ]
    )
    temporal_coverage = report.get("temporal_coverage_summary") or {}
    temporal_backfill = report.get("temporal_backfill_shadow_summary") or {}
    temporal_ambiguity = report.get("temporal_ambiguity_scope_summary") or {}
    temporal_policy_validation = report.get("temporal_ambiguity_policy_decision_validation_summary") or {}
    temporal_guard = report.get("temporal_evidence_guard_summary") or {}
    if temporal_coverage or temporal_backfill or temporal_ambiguity or temporal_policy_validation or temporal_guard:
        lines.extend(["## Temporal Metadata Evidence", ""])
        if temporal_coverage:
            lines.extend(
                [
                    f"- Coverage audit passed: `{str(temporal_coverage.get('passed')).lower()}`",
                    f"- Coverage audit records with temporal metadata: {temporal_coverage.get('with_temporal_metadata_count')} / {temporal_coverage.get('record_count')}",
                    f"- Coverage audit ratio: {temporal_coverage.get('temporal_metadata_ratio')}",
                    f"- Inheritance candidate missing records: {temporal_coverage.get('candidate_missing_record_count')}",
                ]
            )
        if temporal_backfill:
            lines.extend(
                [
                    f"- Shadow backfill passed: `{str(temporal_backfill.get('passed')).lower()}`",
                    f"- Shadow backfill temporal delta: {temporal_backfill.get('delta_temporal_metadata_count')}",
                    f"- Shadow backfill after ratio: {temporal_backfill.get('after_temporal_metadata_ratio')}",
                    f"- Shadow backfill inherited / normalized chunks: {temporal_backfill.get('inherited_chunk_count')} / {temporal_backfill.get('normalized_chunk_count')}",
                    f"- Shadow backfill conflict chunks: {temporal_backfill.get('conflict_chunk_count')}",
                    f"- Shadow backfill ambiguous chunks: {temporal_backfill.get('ambiguous_chunk_count')}",
                    f"- Shadow runtime written / write blocked: `{str(temporal_backfill.get('shadow_runtime_written')).lower()}` / `{str(temporal_backfill.get('write_blocked')).lower()}`",
                    f"- Shadow runtime runnable: `{str(temporal_backfill.get('shadow_runtime_runnable')).lower()}` (evidence-only copies are intentionally non-runnable)",
                    f"- Shadow vector projection ready / records: `{str(temporal_backfill.get('shadow_vector_projection_ready')).lower()}` / {temporal_backfill.get('vector_record_count')}",
                ]
            )
        if temporal_ambiguity:
            lines.extend(
                [
                    f"- Temporal ambiguity scope passed: `{str(temporal_ambiguity.get('passed')).lower()}`",
                    f"- Temporal ambiguity status: `{temporal_ambiguity.get('status')}`",
                    f"- Temporal ambiguity chunks: {temporal_ambiguity.get('ambiguous_chunk_count')}",
                    f"- Temporal ambiguity review slices: {temporal_ambiguity.get('review_slice_count')}",
                    f"- Temporal ambiguity blocking decisions: {temporal_ambiguity.get('blocking_decision_count')}",
                ]
            )
        if temporal_policy_validation:
            lines.extend(
                [
                    f"- Temporal policy decision validation passed: `{str(temporal_policy_validation.get('passed')).lower()}`",
                    f"- Temporal policy decision validation status: `{temporal_policy_validation.get('status')}`",
                    f"- Temporal policy decision rows / blockers: {temporal_policy_validation.get('decision_row_count')} / {temporal_policy_validation.get('blocking_count')}",
                ]
            )
        if temporal_guard:
            lines.extend(
                [
                    f"- Temporal evidence guard passed: `{str(temporal_guard.get('passed')).lower()}`",
                    f"- Temporal evidence stale artifacts: {temporal_guard.get('stale_artifact_count')}",
                    f"- Temporal evidence payload span hours: {temporal_guard.get('payload_generated_at_span_hours')}",
                    f"- Temporal evidence lineage mismatches: {temporal_guard.get('runtime_lineage_mismatch_count')}",
                    f"- Temporal evidence strict mode: `{str(temporal_guard.get('strict_temporal_evidence')).lower()}`",
                ]
            )
        lines.append("")
    revision_impact = report.get("revision_impact_summary") or {}
    if revision_impact:
        lines.extend(
            [
                "## Revision Impact Evidence",
                "",
                f"- Revision impact reports: {revision_impact.get('report_count')}",
                f"- Approval-required units: {revision_impact.get('approval_required_count')}",
                f"- Changed / added / removed units: {revision_impact.get('changed_count')} / {revision_impact.get('added_count')} / {revision_impact.get('removed_count')}",
                f"- Metadata-only changed units: {revision_impact.get('metadata_only_changed_count')}",
                f"- Approval reuse candidates: {revision_impact.get('approval_reuse_candidate_count')}",
                f"- Deindex-required units: {revision_impact.get('deindex_required_count')}",
                "",
            ]
        )
    runtime_version = report.get("runtime_version_drift_summary") or {}
    if runtime_version:
        lines.extend(
            [
                "## Runtime Version Drift",
                "",
                f"- Drift audit passed: `{str(runtime_version.get('passed')).lower()}`",
                f"- Current chunker version: `{runtime_version.get('current_chunker_version')}`",
                f"- Approved stale chunker chunks: {runtime_version.get('approved_repository_stale_chunker_count')} / {runtime_version.get('approved_repository_chunk_count')}",
                f"- Vector stale chunker records: {runtime_version.get('vector_stale_chunker_count')} / {runtime_version.get('vector_record_count')}",
                f"- Vector integrity failures: {runtime_version.get('vector_integrity_failure_count')}",
                f"- Vector content hash mismatches: {runtime_version.get('vector_integrity_content_hash_mismatch_count')}",
                f"- Vector verification hash mismatches: {runtime_version.get('vector_integrity_verification_hash_mismatch_count')}",
                f"- Vector metadata policy failures: approval={runtime_version.get('vector_integrity_invalid_approval_status_count')} / security={runtime_version.get('vector_integrity_invalid_security_level_count')} / required={runtime_version.get('vector_integrity_metadata_missing_required_count')}",
                f"- Vector embedded/path failures: embedded={runtime_version.get('vector_integrity_embedded_failure_count')} / dimensions={runtime_version.get('vector_integrity_embedded_dimension_mismatch_count')} / path_leaks={runtime_version.get('vector_integrity_local_path_leak_count')}",
                f"- Reprocess requires reapproval: `{str(runtime_version.get('reprocess_requires_reapproval')).lower()}`",
                f"- Approved chunks with approved hash in reapproval scope: {runtime_version.get('approved_chunks_with_approved_hash_count')}",
                "",
            ]
        )
    approval_workload = report.get("approval_workload_summary") or {}
    approval_review_batch = report.get("approval_review_batch_summary") or {}
    reapproval_workload = report.get("reapproval_workload_summary") or {}
    reapproval_review_batch = report.get("reapproval_review_batch_summary") or {}
    reapproval_decision_validation = report.get("reapproval_decision_validation_summary") or {}
    reapproval_apply_plan = report.get("reapproval_apply_plan_summary") or {}
    if (
        approval_workload
        or approval_review_batch
        or reapproval_workload
        or reapproval_review_batch
        or reapproval_decision_validation
        or reapproval_apply_plan
    ):
        lines.extend(["## Approval Workload Evidence", ""])
        if approval_workload:
            lines.extend(
                [
                    f"- Approval worklist reports: {approval_workload.get('report_count')}",
                    f"- Approval manual-attention chunks: {approval_workload.get('manual_attention_chunks')} ({approval_workload.get('manual_attention_rate')}%)",
                    f"- Approval low-risk batch candidates: {approval_workload.get('low_risk_batch_review_candidate_chunks')} ({approval_workload.get('low_risk_batch_review_candidate_rate')}%)",
                    f"- Approval priority tiers: `{approval_workload.get('review_priority_tier_counts')}`",
                ]
            )
        if approval_review_batch:
            lines.extend(
                [
                    f"- Approval review batch manifests: {approval_review_batch.get('report_count')}",
                    f"- Approval review batches: {approval_review_batch.get('batch_count')}",
                    f"- Approval review batch chunks: {approval_review_batch.get('approval_chunk_count')}",
                    f"- Approval review batch blockers/warnings: {approval_review_batch.get('blocker_count')} / {approval_review_batch.get('warning_count')}",
                    f"- Approval review batch type counts: `{approval_review_batch.get('review_type_batch_counts')}`",
                ]
            )
        if reapproval_workload:
            lines.extend(
                [
                    f"- Reapproval worklist reports: {reapproval_workload.get('report_count')}",
                    f"- Reapproval candidate chunks: {reapproval_workload.get('reapproval_candidate_chunks')}",
                    f"- Reapproval recommended initial review chunks: {reapproval_workload.get('recommended_initial_review_chunks')}",
                    f"- Reapproval approval provenance gaps: {reapproval_workload.get('approval_provenance_missing_chunks')} / provenance-only {reapproval_workload.get('approval_provenance_only_chunks')}",
                    f"- Reapproval approval provenance missing fields: `{reapproval_workload.get('approval_provenance_missing_field_counts') or {}}`",
                    f"- Reapproval initial review reduction ratio: {reapproval_workload.get('initial_review_reduction_ratio')}",
                    f"- Reapproval source vector integrity failures: {reapproval_workload.get('source_vector_integrity_failure_count')}",
                    f"- Reapproval pre-blockers: {reapproval_workload.get('pre_reapproval_blocker_count')}",
                ]
            )
        if reapproval_review_batch:
            lines.extend(
                [
                    f"- Reapproval review batch manifests: {reapproval_review_batch.get('report_count')}",
                    f"- Reapproval review batches: {reapproval_review_batch.get('batch_count')}",
                    f"- Reapproval review batch chunks: {reapproval_review_batch.get('reapproval_chunk_count')}",
                    f"- Reapproval selected candidates: {reapproval_review_batch.get('selected_candidate_count')} of {reapproval_review_batch.get('candidate_count')}",
                    f"- Reapproval review batch blockers/warnings: {reapproval_review_batch.get('blocker_count')} / {reapproval_review_batch.get('warning_count')}",
                    f"- Reapproval review risk tiers: `{reapproval_review_batch.get('risk_tier_chunk_counts')}`",
                ]
            )
        if reapproval_decision_validation:
            lines.extend(
                [
                    f"- Reapproval decision validation reports: {reapproval_decision_validation.get('report_count')}",
                    f"- Reapproval decision validation passed: `{str(reapproval_decision_validation.get('passed')).lower()}`",
                    f"- Reapproval decision rows complete / blank-incomplete: {reapproval_decision_validation.get('complete_row_count')} / {reapproval_decision_validation.get('blank_or_incomplete_row_count')}",
                    f"- Reapproval decision release-gate statuses: `{reapproval_decision_validation.get('release_gate_status_counts')}`",
                ]
            )
        if reapproval_apply_plan:
            lines.extend(
                [
                    f"- Reapproval apply plan reports: {reapproval_apply_plan.get('report_count')}",
                    f"- Reapproval apply plan passed: `{str(reapproval_apply_plan.get('passed')).lower()}`",
                    f"- Reapproval apply plan unsafe contract violations: {reapproval_apply_plan.get('unsafe_contract_violation_count')}",
                    f"- Reapproval apply plan batch controls: {reapproval_apply_plan.get('batch_apply_control_count')}",
                    f"- Reapproval apply plan explicit reindex controls: {reapproval_apply_plan.get('batch_requires_explicit_reindex_phase_count')}",
                    f"- Reapproval apply plan conditional vector-sync guards: {reapproval_apply_plan.get('batch_conditional_vector_sync_guard_count')}",
                ]
            )
        lines.append("")
    public_readiness = report.get("public_readiness_summary") or {}
    if public_readiness:
        lines.extend(
            [
                "## Public Batch Readiness",
                "",
                f"- Status: `{public_readiness.get('status')}`",
                f"- Passed: `{str(public_readiness.get('passed')).lower()}`",
                f"- Inputs: {public_readiness.get('input_count')}",
                f"- Successful: {public_readiness.get('successful_count')}",
                f"- Failed: {public_readiness.get('failed_count')}",
                f"- OCR required: {public_readiness.get('ocr_required_count')}",
                f"- Recommendations: {public_readiness.get('recommendation_total')}",
                f"- Failed checks: {', '.join(public_readiness.get('failed_checks') or []) or '-'}",
                "",
            ]
        )
    parser_goldset = report.get("parser_goldset_score_summary") or {}
    if parser_goldset:
        by_structure_f1 = parser_goldset.get("by_structure_f1") or {}
        lines.extend(
            [
                "## Parser Goldset Score",
                "",
                f"- Report type: `{parser_goldset.get('report_type')}`",
                f"- Documents: {parser_goldset.get('document_count')}",
                f"- Scored documents: {parser_goldset.get('scored_document_count')}",
                f"- Excluded non-article documents: {parser_goldset.get('excluded_document_count')}",
                f"- Ready for quality claim: `{str(parser_goldset.get('ready_for_quality_claim')).lower()}`",
                f"- Completed document labels: {parser_goldset.get('completed_document_count')} / {parser_goldset.get('document_count')}",
                f"- Scorable structure rows: {parser_goldset.get('scorable_structure_count')}",
                f"- Completed structure score rows: {parser_goldset.get('completed_structure_score_count')} / {parser_goldset.get('expected_structure_score_count')}",
                f"- Missing structure score rows: {parser_goldset.get('missing_structure_score_count')}",
                f"- Issues: {parser_goldset.get('issue_count')}",
                f"- Overall precision / recall / F1: {parser_goldset.get('overall_precision')} / {parser_goldset.get('overall_recall')} / {parser_goldset.get('overall_f1')}",
                f"- Structure F1: {', '.join(f'{key}={value}' for key, value in sorted(by_structure_f1.items())) or '-'}",
                "",
            ]
        )
    parser_completion = report.get("parser_goldset_completion_board_summary") or {}
    if parser_completion:
        structure_completion = parser_completion.get("structure_completion_summary") or {}
        lines.extend(
            [
                "## Parser Goldset Completion Board",
                "",
                f"- Report type: `{parser_completion.get('report_type')}`",
                f"- Completion gate status: `{parser_completion.get('completion_gate_status') or '-'}`",
                f"- Ready for quality claim: `{str(parser_completion.get('ready_for_quality_claim')).lower()}`",
                f"- Ready documents: {parser_completion.get('ready_document_count')} / {parser_completion.get('document_count')}",
                f"- Completed structure score rows: {parser_completion.get('completed_structure_score_rows')} / {parser_completion.get('expected_structure_score_rows')}",
                f"- Missing manual / matched fields: {parser_completion.get('missing_manual_field_count')} / {parser_completion.get('missing_matched_field_count')}",
                f"- Missing reviewer metadata rows: {parser_completion.get('missing_reviewer_metadata_count')}",
                "",
                "| Structure | Pipeline total | Score rows complete | Missing manual | Missing matched | Ready for F1 |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for structure_type, values in sorted(structure_completion.items()):
            lines.append(
                f"| {structure_type} | {values.get('pipeline_total')} | "
                f"{values.get('score_rows_complete')}/{values.get('expected_document_count')} | "
                f"{values.get('missing_manual_count')} | {values.get('missing_matched_count')} | "
                f"{str(values.get('ready_for_structure_f1')).lower()} |"
            )
        lines.append("")
    table_claim = report.get("table_preprocessing_claim_gate_summary") or {}
    if table_claim:
        lines.extend(
            [
                "## Table Preprocessing Claim Gate",
                "",
                f"- Passed: `{str(table_claim.get('passed')).lower()}`",
                f"- Status: `{table_claim.get('status') or '-'}`",
                f"- Claim level: `{table_claim.get('claim_level') or '-'}`",
                f"- Feasibility: `{table_claim.get('feasibility_status') or '-'}`",
                f"- Blockers / warnings: {table_claim.get('blocker_count')} / {table_claim.get('warning_count')}",
                f"- Table units completed / pending / invalid: {table_claim.get('completed_unit_count')} / {table_claim.get('pending_unit_count')} / {table_claim.get('invalid_unit_count')}",
                f"- Required field missing total: {table_claim.get('required_field_missing_total')}",
                f"- Review priority counts: {', '.join(f'{key}={value}' for key, value in sorted((table_claim.get('review_priority_counts') or {}).items())) or '-'}",
                f"- Label review flag counts: {', '.join(f'{key}={value}' for key, value in sorted((table_claim.get('label_review_flag_counts') or {}).items())) or '-'}",
                f"- Transfer passed / blockers: `{str(table_claim.get('transfer_passed')).lower()}` / {table_claim.get('transfer_blocker_count')}",
                f"- Source traceability passed / issues: `{str(table_claim.get('source_traceability_passed')).lower()}` / {table_claim.get('source_traceability_issue_count')}",
                f"- Source page-count verification required: `{str(table_claim.get('source_traceability_require_page_count_verification')).lower()}`",
                f"- Drift check present / passed / blockers: `{str(table_claim.get('drift_check_present')).lower()}` / `{str(table_claim.get('drift_check_passed')).lower()}` / {table_claim.get('drift_check_blocker_count')}",
                f"- Source page statuses: {', '.join(f'{key}={value}' for key, value in sorted((table_claim.get('source_page_count_status_counts') or {}).items())) or '-'}",
                f"- Source format statuses: {', '.join(f'{key}={value}' for key, value in sorted((table_claim.get('source_format_status_counts') or {}).items())) or '-'}",
                f"- Table answer blockers: {table_claim.get('table_answer_blocker_count')}",
                f"- Non-review evidence ready: `{str(table_claim.get('non_review_evidence_ready')).lower()}`; release blocked by human review=`{str(table_claim.get('release_blocked_by_human_review')).lower()}`",
                "",
            ]
        )
    profile_provenance = report.get("profile_provenance_summary") or {}
    if profile_provenance:
        file_type_counts = profile_provenance.get("file_type_counts") or {}
        unknown_profile_counts = profile_provenance.get("unknown_profile_counts") or {}
        apba_id_counts = profile_provenance.get("apba_id_counts") or {}
        lines.extend(
            [
                "## Profile Provenance",
                "",
                f"- Passed: `{str(profile_provenance.get('passed')).lower()}`",
                f"- Blockers: {profile_provenance.get('blocker_count')}",
                f"- Warnings: {profile_provenance.get('warning_count')}",
                f"- Rows: {profile_provenance.get('row_count')}",
                f"- Institutions: {profile_provenance.get('institution_count')}",
                f"- PUBLIC_PORTAL apba IDs: {', '.join(f'{key}={value}' for key, value in sorted(apba_id_counts.items())) or '-'}",
                f"- File types: {', '.join(f'{key}={value}' for key, value in sorted(file_type_counts.items())) or '-'}",
                f"- Matched profiles: {profile_provenance.get('matched_profile_count')} / {profile_provenance.get('registry_profile_count')}",
                f"- Unknown profiles: {', '.join(f'{key}={value}' for key, value in sorted(unknown_profile_counts.items())) or '-'}",
                f"- Registry sha256: `{profile_provenance.get('registry_sha256') or '-'}`",
                f"- API calls: {profile_provenance.get('api_call_count')}",
                "",
            ]
        )
    rag_eval = report.get("rag_eval_summary") or {}
    if rag_eval:
        lines.extend(
            [
                "## RAG Retrieval Evaluation",
                "",
                f"- Source mode: `{rag_eval.get('source_mode') or '-'}`",
                f"- Runtime: `{rag_eval.get('effective_runtime_data_dir') or '-'}`",
                f"- Source chunks: {rag_eval.get('source_chunk_count')}",
                f"- Queries: {rag_eval.get('query_count')}",
                f"- Answerable: {rag_eval.get('answerable_count')} / {rag_eval.get('answerable_query_count')} ({rag_eval.get('answerable_ratio')})",
                f"- No-evidence controls: {rag_eval.get('no_evidence_passed_count')} passed / {rag_eval.get('expect_no_evidence_query_count')} total; failed={rag_eval.get('no_evidence_failed_count')}",
                f"- Relation-supported ratio: {rag_eval.get('relation_supported_ratio')}",
                f"- Quality-warning chunks: {rag_eval.get('quality_warning_chunk_count')}",
                f"- API calls: {rag_eval.get('api_call_count')}",
                "",
            ]
        )
    mcp_demo_answers = report.get("mcp_demo_answer_summary") or {}
    if mcp_demo_answers:
        lines.extend(
            [
                "## MCP Demo Answers",
                "",
                f"- Passed: `{str(mcp_demo_answers.get('passed')).lower()}`",
                f"- Queries: {mcp_demo_answers.get('query_count')}",
                f"- Answerable / no-evidence controls: {mcp_demo_answers.get('answerable_query_count')} / {mcp_demo_answers.get('expect_no_evidence_query_count')}",
                f"- Failed items: {mcp_demo_answers.get('failed_item_count')}",
                f"- Smoke citations: {mcp_demo_answers.get('smoke_citation_count')}",
                f"- Missing supporting citations: {mcp_demo_answers.get('missing_supporting_result_count')}",
                f"- No-evidence controls with citations: {mcp_demo_answers.get('no_evidence_with_citation_count')}",
                f"- Quality issues: {mcp_demo_answers.get('quality_issue_count')}",
                f"- Supporting result counts: {', '.join(str(value) for value in mcp_demo_answers.get('supporting_result_counts') or []) or '-'}",
                f"- Expected-term queries: {mcp_demo_answers.get('expected_term_query_count')}",
                f"- Expected-term min/avg hit ratio: {mcp_demo_answers.get('expected_term_min_hit_ratio')} / {mcp_demo_answers.get('expected_term_average_hit_ratio')}",
                f"- Expected-term partial/low hits: {mcp_demo_answers.get('expected_term_partial_hit_count')} / {mcp_demo_answers.get('expected_term_low_hit_count')}",
                f"- Expected-article-no queries/min hit ratio: {mcp_demo_answers.get('expected_article_no_query_count')} / {mcp_demo_answers.get('expected_article_no_min_hit_ratio')}",
                f"- Expected-article-title queries/min hit ratio: {mcp_demo_answers.get('expected_article_title_query_count')} / {mcp_demo_answers.get('expected_article_title_min_hit_ratio')}",
                f"- API calls: {mcp_demo_answers.get('api_call_count')}",
                "",
            ]
        )
    accuracy_comparison = report.get("accuracy_comparison_summary") or {}
    if accuracy_comparison:
        lines.extend(
            [
                "## Simple RAG vs MCP Accuracy",
                "",
                f"- Passed: `{str(accuracy_comparison.get('passed')).lower()}`",
                f"- Queries: {accuracy_comparison.get('query_count')}",
                f"- Baseline passed: {accuracy_comparison.get('baseline_passed_count')}",
                f"- MCP passed: {accuracy_comparison.get('mcp_passed_count')}",
                f"- MCP better / not worse / regression: {accuracy_comparison.get('mcp_better_count')} / {accuracy_comparison.get('mcp_not_worse_count')} / {accuracy_comparison.get('mcp_regression_count')}",
                f"- Avg quality score baseline -> MCP: {accuracy_comparison.get('baseline_avg_quality_score')} -> {accuracy_comparison.get('mcp_avg_quality_score')} ({accuracy_comparison.get('avg_score_delta')})",
                f"- API calls: {accuracy_comparison.get('api_call_count')}",
                "",
            ]
        )
    mcp_transport = report.get("mcp_transport_smoke_summary") or {}
    if mcp_transport:
        full_timing = mcp_transport.get("full_profile_timing_ms") or {}
        chatgpt_timing = mcp_transport.get("chatgpt_data_profile_timing_ms") or {}
        lines.extend(
            [
                "## MCP Transport Smoke",
                "",
                f"- Passed: `{str(mcp_transport.get('passed')).lower()}`",
                f"- Transport: `{mcp_transport.get('transport')}`",
                f"- Search results: {mcp_transport.get('search_result_count')}",
                f"- Warm search results: {mcp_transport.get('warm_search_result_count')}",
                f"- Fetch has text: `{str(mcp_transport.get('fetch_has_text')).lower()}`",
                f"- Full profile total: {full_timing.get('total_elapsed_ms')} ms",
                f"- Full profile list/search/fetch/warm-search: {full_timing.get('list_tools_elapsed_ms')} / {full_timing.get('search_elapsed_ms')} / {full_timing.get('fetch_elapsed_ms')} / {full_timing.get('warm_search_elapsed_ms')} ms",
                f"- ChatGPT data profile total: {chatgpt_timing.get('total_elapsed_ms')} ms",
                f"- Source metadata complete: `{str(mcp_transport.get('source_metadata_complete')).lower()}`",
                f"- Missing source metadata fields: {', '.join(mcp_transport.get('missing_source_metadata_fields') or []) or '-'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _load_json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    merged = dict(metadata)
    for key in (
        "document_id",
          "document_name",
          "institution_name",
          "tenant_id",
          "profile_id",
          "apba_id",
          "chunk_id",
        "chunk_type",
        "article_no",
        "article_title",
        "source_page_start",
        "source_page_end",
        "approval_id",
          "approved_content_hash",
          "approval_status",
          "regulation_id",
          "regulation_version",
          "regulation_status",
          "revision_date",
          "effective_from",
          "effective_to",
          "repealed_at",
          "security_level",
    ):
        if item.get(key) not in (None, "") and key not in merged:
            merged[key] = item.get(key)
    normalized_lifecycle = read_regulation_metadata(item)
    for key, value in (
        ("regulation_id", normalized_lifecycle.regulation_id),
        ("regulation_version", normalized_lifecycle.version),
        ("approval_status", normalized_lifecycle.status),
        ("effective_from", normalized_lifecycle.effective_from.isoformat() if normalized_lifecycle.effective_from else None),
        ("effective_to", normalized_lifecycle.effective_to.isoformat() if normalized_lifecycle.effective_to else None),
        ("repealed_at", normalized_lifecycle.repealed_at.isoformat() if normalized_lifecycle.repealed_at else None),
    ):
        if key not in merged or merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def _chunk_type(chunk: dict[str, Any]) -> str:
    return str(chunk.get("chunk_type") or _metadata(chunk).get("chunk_type") or "unknown")


def _is_approved_chunk(chunk: dict[str, Any], metadata: dict[str, Any]) -> bool:
    status = str(chunk.get("approval_status") or metadata.get("approval_status") or "").strip().lower()
    if status:
        return status == "approved"
    return bool(chunk.get("approval_id") or metadata.get("approval_id"))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _percent(count: int, total: int) -> float:
    return round(count / total * 100, 2) if total else 0.0


def _int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _int_count_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _int(raw_count) for key, raw_count in value.items()}


def _float(value: Any) -> float:
    if isinstance(value, bool) or value in (None, ""):
        return 0.0
    try:
        return round(float(str(value)), 3)
    except ValueError:
        return 0.0


def _profile_timing_ms(profile: dict[str, Any]) -> dict[str, float]:
    return {
        "list_tools_elapsed_ms": _float(profile.get("list_tools_elapsed_ms")),
        "search_elapsed_ms": _float(profile.get("search_elapsed_ms")),
        "fetch_elapsed_ms": _float(profile.get("fetch_elapsed_ms")),
        "warm_search_elapsed_ms": _float(profile.get("warm_search_elapsed_ms")),
        "total_elapsed_ms": _float(profile.get("total_elapsed_ms")),
    }


def _search_timing_ms(profile: dict[str, Any]) -> dict[str, float]:
    search_metadata = profile.get("search_metadata") if isinstance(profile.get("search_metadata"), dict) else {}
    timing = search_metadata.get("timing_ms") if isinstance(search_metadata.get("timing_ms"), dict) else {}
    return {str(key): _float(value) for key, value in timing.items()}


def _first_numeric(payloads: list[Any], field: str) -> float:
    for payload in payloads:
        if isinstance(payload, dict) and payload.get(field) not in (None, ""):
            try:
                return float(payload[field])
            except (TypeError, ValueError):
                continue
    return 0.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit MCP product readiness across parsing, revision, generality, accuracy, and operations.")
    parser.add_argument("--runtime-data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument(
        "--profile-id",
        default=None,
        help="Restrict the readiness audit to one institution profile; omit for the whole tenant.",
    )
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--batch-report", action="append", default=[])
    parser.add_argument("--public-readiness-report", default=None)
    parser.add_argument("--parser-goldset-score-report", default=None)
    parser.add_argument("--parser-goldset-completion-board-report", default=None)
    parser.add_argument("--table-preprocessing-claim-gate-report", default=None)
    parser.add_argument("--rag-eval-report", default=None)
    parser.add_argument("--mcp-demo-answer-report", default=None)
    parser.add_argument("--accuracy-comparison-report", default=None)
    parser.add_argument("--profile-provenance-report", default=None)
    parser.add_argument("--mcp-readiness-report", default=None)
    parser.add_argument("--mcp-transport-smoke-report", default=None)
    parser.add_argument("--temporal-coverage-report", default=None)
    parser.add_argument("--temporal-backfill-shadow-report", default=None)
    parser.add_argument("--temporal-ambiguity-scope-report", default=None)
    parser.add_argument("--temporal-ambiguity-policy-decision-validation-report", default=None)
    parser.add_argument("--revision-impact-report", action="append", default=[])
    parser.add_argument("--runtime-version-drift-report", default=None)
    parser.add_argument("--approval-worklist-report", action="append", default=[])
    parser.add_argument("--approval-review-batch-report", action="append", default=[])
    parser.add_argument("--reapproval-worklist-report", action="append", default=[])
    parser.add_argument("--reapproval-review-batch-report", action="append", default=[])
    parser.add_argument("--reapproval-decision-validation-report", action="append", default=[])
    parser.add_argument("--reapproval-apply-plan-report", action="append", default=[])
    parser.add_argument("--min-average-quality-score", type=float, default=98.0)
    parser.add_argument("--min-parser-goldset-f1", type=float, default=90.0)
    parser.add_argument("--require-parser-goldset-score", action="store_true")
    parser.add_argument("--require-table-preprocessing-claim", action="store_true")
    parser.add_argument("--min-answerable-ratio", type=float, default=0.8)
    parser.add_argument("--allow-partial-index", action="store_true")
    parser.add_argument(
        "--max-source-report-age-hours",
        type=float,
        default=None,
        help="Warn when temporal evidence source reports are older than this many hours.",
    )
    parser.add_argument(
        "--strict-temporal-evidence",
        action="store_true",
        help="Treat temporal evidence freshness or runtime-lineage mismatches as blockers.",
    )
    parser.add_argument(
        "--allow-synthetic-runtime",
        action="store_true",
        help="Allow synthetic smoke documents for disposable local release evidence; never use for production release.",
    )
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_mcp_product_readiness_audit(
        runtime_data_dir=Path(args.runtime_data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        batch_reports=[Path(value) for value in args.batch_report],
        public_readiness_report=Path(args.public_readiness_report) if args.public_readiness_report else None,
        parser_goldset_score_report=Path(args.parser_goldset_score_report) if args.parser_goldset_score_report else None,
        parser_goldset_completion_board_report=(
            Path(args.parser_goldset_completion_board_report)
            if args.parser_goldset_completion_board_report
            else None
        ),
        table_preprocessing_claim_gate_report=(
            Path(args.table_preprocessing_claim_gate_report)
            if args.table_preprocessing_claim_gate_report
            else None
        ),
        rag_eval_report=Path(args.rag_eval_report) if args.rag_eval_report else None,
        mcp_demo_answer_report=Path(args.mcp_demo_answer_report) if args.mcp_demo_answer_report else None,
        accuracy_comparison_report=Path(args.accuracy_comparison_report) if args.accuracy_comparison_report else None,
        profile_provenance_report=Path(args.profile_provenance_report) if args.profile_provenance_report else None,
        mcp_readiness_report=Path(args.mcp_readiness_report) if args.mcp_readiness_report else None,
        mcp_transport_smoke_report=Path(args.mcp_transport_smoke_report) if args.mcp_transport_smoke_report else None,
        temporal_coverage_report=Path(args.temporal_coverage_report) if args.temporal_coverage_report else None,
        temporal_backfill_shadow_report=(
            Path(args.temporal_backfill_shadow_report) if args.temporal_backfill_shadow_report else None
        ),
        temporal_ambiguity_scope_report=(
            Path(args.temporal_ambiguity_scope_report) if args.temporal_ambiguity_scope_report else None
        ),
        temporal_ambiguity_policy_decision_validation_report=(
            Path(args.temporal_ambiguity_policy_decision_validation_report)
            if args.temporal_ambiguity_policy_decision_validation_report
            else None
        ),
        revision_impact_reports=[Path(value) for value in args.revision_impact_report],
        runtime_version_drift_report=Path(args.runtime_version_drift_report) if args.runtime_version_drift_report else None,
        approval_worklist_reports=[Path(value) for value in args.approval_worklist_report],
        approval_review_batch_reports=[Path(value) for value in args.approval_review_batch_report],
        reapproval_worklist_reports=[Path(value) for value in args.reapproval_worklist_report],
        reapproval_review_batch_reports=[Path(value) for value in args.reapproval_review_batch_report],
        reapproval_decision_validation_reports=[
            Path(value) for value in args.reapproval_decision_validation_report
        ],
        reapproval_apply_plan_reports=[Path(value) for value in args.reapproval_apply_plan_report],
        min_average_quality_score=args.min_average_quality_score,
        min_parser_goldset_f1=args.min_parser_goldset_f1,
        require_parser_goldset_score=args.require_parser_goldset_score,
        require_table_preprocessing_claim=args.require_table_preprocessing_claim,
        min_answerable_ratio=args.min_answerable_ratio,
        require_full_index=not args.allow_partial_index,
        max_source_report_age_hours=args.max_source_report_age_hours,
        strict_temporal_evidence=args.strict_temporal_evidence,
        allow_synthetic_runtime=args.allow_synthetic_runtime,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
