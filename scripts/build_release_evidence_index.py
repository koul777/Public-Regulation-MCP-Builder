from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_contracts import MCP_CORE_REPORT_PATHS


DEFAULT_REPORT_ARTIFACTS = (
    Path("reports/private_release_gate_current.json"),
    Path("reports/private_release_readiness_current.json"),
    Path("reports/private_release_manifest_current.json"),
    Path("reports/github_private_visibility_current.json"),
    Path("reports/release_hygiene_current.json"),
    Path("reports/private_release_smoke_current.json"),
)
HERMES_MCP_REPORT_ARTIFACTS = (
    Path("reports/hermes_mcp_check_current.json"),
    Path("reports/hermes_mcp_check_current.md"),
    Path("reports/installed_console_scripts_hermes.json"),
    Path("reports/mcp_smoke_hermes.json"),
    Path("reports/mcp_transport_smoke_hermes.json"),
    Path("reports/mcp_client_bundle_hermes.json"),
    Path("reports/mcp_connection_bundle_hermes.zip"),
    Path("reports/mcp_connection_readiness_bundle_hermes.json"),
    Path("reports/mcp_connection_readiness_chatgpt_https_hermes.json"),
    Path("reports/mcp_connection_readiness_chatgpt_tunnel_hermes.json"),
)
MCP_PRODUCT_READINESS_REPORT_ARTIFACTS = (
    MCP_CORE_REPORT_PATHS["mcp_readiness_authority"],
    Path("reports/mcp_readiness_authority_current.md"),
    MCP_CORE_REPORT_PATHS["mcp_product_readiness"],
    Path("reports/mcp_product_readiness_current.md"),
    Path("reports/table_review_source_traceability_current.json"),
    Path("reports/table_review_source_traceability_current.md"),
    Path("reports/parsing_goldset_table_drift_check_current.json"),
    Path("reports/parsing_goldset_table_drift_check_current.md"),
    Path("reports/table_preprocessing_claim_gate_current.json"),
    Path("reports/table_preprocessing_claim_gate_current.md"),
    MCP_CORE_REPORT_PATHS["mcp_demo_answers"],
    Path("reports/simple_rag_vs_mcp_accuracy_current.json"),
    MCP_CORE_REPORT_PATHS["mcp_transport_smoke"],
    MCP_CORE_REPORT_PATHS["mcp_index_visibility_audit"],
    MCP_CORE_REPORT_PATHS["mcp_connection_readiness"],
    Path("reports/mcp_handoff_current.json"),
    Path("reports/mcp_query_benchmark_current.json"),
    Path("reports/mcp_query_benchmark_current.md"),
    Path("reports/mcp_answer_evidence_bundle_current.json"),
    Path("reports/mcp_answer_evidence_bundle_current.md"),
    Path("reports/mcp_performance_load_evidence_current.json"),
    Path("reports/mcp_performance_load_evidence_current.md"),
    Path("reports/mcp_cold_start_benchmark_current.json"),
    Path("reports/mcp_cold_start_benchmark_current.md"),
    Path("reports/mcp_concurrent_benchmark_current.json"),
    Path("reports/mcp_concurrent_benchmark_current.md"),
    Path("reports/aks_mcp_publish_runtime_report.json"),
    Path("data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_worklist.json"),
    Path("data/aks_mcp_publish_runtime/reports/aks_mcp_publish_tenant-aks-publish_approval_review_batches.json"),
    Path("reports/approval_worklist_current.json"),
    Path("reports/approval_worklist_current.md"),
    Path("reports/approval_review_batches_current.json"),
    Path("reports/approval_review_batches_current.md"),
    Path("reports/reapproval_worklist_current.json"),
    Path("reports/reapproval_worklist_current_chunks.csv"),
    Path("reports/reapproval_worklist_current_chunks.json"),
    Path("reports/reapproval_worklist_current.md"),
    Path("reports/reapproval_review_batches_current.json"),
    Path("reports/reapproval_review_batches_current.csv"),
    Path("reports/reapproval_review_batches_current.md"),
    Path("reports/reapproval_review_batch_decisions_current.csv"),
    Path("reports/reapproval_decision_validation_current.json"),
    Path("reports/reapproval_decision_validation_current.md"),
    Path("reports/reapproval_apply_plan_current.json"),
    Path("reports/reapproval_apply_plan_current.md"),
    Path("reports/reapproval_review_burden_current.json"),
    Path("reports/reapproval_review_burden_current.md"),
    Path("reports/mcp_readiness_remediation_plan_current.json"),
    Path("reports/mcp_readiness_remediation_plan_current.md"),
    Path("reports/github_publish_readiness_current.json"),
    Path("reports/github_publish_readiness_current.md"),
    Path("reports/github_publish_execution_plan_current.json"),
    Path("reports/github_publish_execution_plan_current.md"),
    Path("reports/strict_public_readiness_gap_summary_current.json"),
    Path("reports/strict_public_readiness_gap_summary_current.md"),
    Path("reports/strict_public_readiness_gap_worklist_current.csv"),
    Path("reports/strict_public_readiness_gap_worklist_current.md"),
    Path("reports/temporal_ambiguity_review_scope_current.json"),
    Path("reports/temporal_ambiguity_review_scope_current.md"),
)
EVIDENCE_PROFILES = ("private-release", "hermes-mcp", "mcp-product-readiness")
ALLOWLIST_ARTIFACT = Path(".release-hygiene-allowlist.json")
HASH_CHUNK_BYTES = 1024 * 1024


def build_release_evidence_index(
    repo_root: Path | str | None = None,
    artifact_paths: Iterable[Path | str] | None = None,
    *,
    evidence_profile: str = "private-release",
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else PROJECT_ROOT
    observed_at = generated_at or datetime.now(timezone.utc).isoformat()
    selected_artifacts = (
        list(artifact_paths) if artifact_paths is not None else _default_artifact_paths(root, evidence_profile)
    )
    artifacts = [_artifact_evidence(root, artifact_path, generated_at=observed_at) for artifact_path in selected_artifacts]
    repo_commit = _repo_commit_from_artifacts(artifacts) or _current_repo_commit(root)
    repo_worktree = _repo_worktree_state(root)
    return {
        "index_type": "release_evidence_index",
        "index_version": 1,
        "evidence_profile": evidence_profile,
        "generated_at": observed_at,
        "repo_commit": repo_commit,
        "repo_worktree": repo_worktree,
        "repo_root_name": root.name,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


def _default_artifact_paths(repo_root: Path, evidence_profile: str) -> list[Path]:
    if evidence_profile == "hermes-mcp":
        return list(HERMES_MCP_REPORT_ARTIFACTS)
    if evidence_profile == "mcp-product-readiness":
        return list(MCP_PRODUCT_READINESS_REPORT_ARTIFACTS)
    artifacts = list(DEFAULT_REPORT_ARTIFACTS)
    if (repo_root / ALLOWLIST_ARTIFACT).is_file():
        artifacts.append(ALLOWLIST_ARTIFACT)
    dist_dir = repo_root / "dist"
    artifacts.extend(_dist_matches(dist_dir, "*.whl"))
    artifacts.extend(_dist_matches(dist_dir, "*.tar.gz"))
    return artifacts


def _repo_commit_from_artifacts(artifacts: Sequence[dict[str, Any]]) -> str | None:
    commits = sorted(
        {
            str(summary["repo_commit"]).strip()
            for artifact in artifacts
            if isinstance((summary := artifact.get("json_summary")), dict) and summary.get("repo_commit")
        }
    )
    if len(commits) == 1:
        return commits[0]
    return None


def _current_repo_commit(repo_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return None
    commit = completed.stdout.decode("utf-8", "replace").strip()
    return commit if len(commit) == 40 else None


def _repo_worktree_state(repo_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=normal"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "state": "unknown",
            "dirty": None,
            "tracked_change_count": None,
            "untracked_change_count": None,
        }
    status_lines = [
        line
        for line in completed.stdout.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    untracked_count = sum(1 for line in status_lines if line.startswith("??"))
    tracked_count = len(status_lines) - untracked_count
    return {
        "state": "dirty" if status_lines else "clean",
        "dirty": bool(status_lines),
        "tracked_change_count": tracked_count,
        "untracked_change_count": untracked_count,
    }


def _dist_matches(dist_dir: Path, pattern: str) -> list[Path]:
    if not dist_dir.is_dir():
        return []
    return [Path("dist") / path.name for path in sorted(dist_dir.glob(pattern))]


def _artifact_evidence(repo_root: Path, artifact_path: Path | str, *, generated_at: str) -> dict[str, Any]:
    original_path = Path(artifact_path)
    candidate = original_path if original_path.is_absolute() else repo_root / original_path
    exists = candidate.exists()
    evidence: dict[str, Any] = {
        "artifact_path": _display_artifact_path(repo_root, original_path, candidate),
        "exists": exists,
        "size_bytes": None,
        "sha256": None,
        "generated_at": generated_at,
    }
    if exists and candidate.is_file():
        evidence["size_bytes"] = candidate.stat().st_size
        evidence["sha256"] = _sha256_file(candidate)
        summary = _json_artifact_summary(candidate)
        if summary:
            evidence["json_summary"] = summary
    return evidence


def _json_artifact_summary(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in (
        "report_type",
        "manifest_type",
        "index_type",
        "status",
        "mode",
        "harness_mode",
        "passed",
        "handoff_ready",
        "authority_version",
        "decision",
        "deploy_ready",
        "readiness_scope",
        "client_profile",
        "server_name",
        "tenant_id",
        "transport",
        "blocking_count",
        "blocker_count",
        "warning_count",
        "evidence_ready",
        "top_k",
        "iterations",
        "rounds",
        "concurrency",
        "query_count",
        "task_count",
        "min_warm_records",
        "min_record_count",
        "max_process_elapsed_ms",
        "max_task_total_ms",
        "max_batch_elapsed_ms",
        "query_spec_path",
        "query_spec_sha256",
        "query_spec_byte_count",
        "query_spec_item_count",
        "candidate_count",
        "selected_candidate_count",
        "batch_count",
        "approval_chunk_count",
        "reapproval_chunk_count",
        "max_chunks_per_batch",
        "quality_issue_count",
        "expected_term_min_hit_ratio",
        "expected_term_average_hit_ratio",
        "expected_term_low_hit_count",
        "check_count",
        "artifact_count",
        "raw_finding_count",
        "finding_count",
        "suppressed_finding_count",
        "repo_commit",
        "data_dir_mode",
        "handoff_evidence",
        "document_count",
        "total_chunks",
        "manual_attention_chunks",
        "low_risk_batch_review_candidate_chunks",
        "reapproval_candidate_chunks",
        "recommended_initial_review_chunks",
        "approval_provenance_missing_chunks",
        "approval_provenance_only_chunks",
        "source_vector_integrity_failure_count",
        "source_chunk_count",
        "selected_chunk_count",
        "approved_chunk_count",
        "indexed_record_count",
        "approval_record_count",
        "initial_review_reduction_ratio",
        "baseline_full_review_minutes",
        "decision_template_row_count",
        "decision_template_operator_decision_complete_count",
        "decision_template_operator_decision_blank_count",
        "expected_batch_count",
        "decision_row_count",
        "complete_row_count",
        "blank_or_incomplete_row_count",
        "release_gate_status",
        "release_blocker_count",
    ):
        if key in payload:
            summary[key] = payload[key]
    if isinstance(payload.get("source_report_artifacts"), list):
        summary["source_report_artifact_count"] = len(payload["source_report_artifacts"])
    if isinstance(payload.get("authoritative_artifacts"), list):
        summary["authoritative_artifact_count"] = len(payload["authoritative_artifacts"])
    if isinstance(payload.get("supersedes"), list):
        summary["supersedes_count"] = len(payload["supersedes"])
    if "failed_check_names" in payload and isinstance(payload["failed_check_names"], list):
        summary["failed_check_count"] = len(payload["failed_check_names"])
    _merge_nested_mcp_summary(summary, payload)
    _merge_product_readiness_nested_summary(summary, payload)
    _merge_index_visibility_nested_summary(summary, payload)
    _merge_connection_readiness_summary(summary, payload)
    _merge_reapproval_worklist_summary(summary, payload)
    _merge_publish_runtime_summary(summary, payload)
    _merge_demo_answer_item_summary(summary, payload)
    _merge_temporal_ambiguity_review_scope_summary(summary, payload)
    if payload.get("path_details_redacted") is True:
        summary["path_details_redacted"] = True
    return summary


def _merge_nested_mcp_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    for section_key in ("mcp_demo_answer_summary", "demo_summary"):
        section = payload.get(section_key)
        if not isinstance(section, dict):
            continue
        for key in (
            "query_count",
            "quality_issue_count",
            "expected_term_min_hit_ratio",
            "expected_term_average_hit_ratio",
            "expected_term_low_hit_count",
        ):
            if key in section and key not in summary:
                summary[key] = section[key]


def _merge_product_readiness_nested_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    for section_key, keys in (
        (
            "temporal_coverage_summary",
            (
                "record_count",
                "with_temporal_metadata_count",
                "without_temporal_metadata_count",
                "temporal_metadata_ratio",
                "candidate_missing_record_count",
            ),
        ),
        (
            "temporal_backfill_shadow_summary",
            (
                "delta_temporal_metadata_count",
                "after_temporal_metadata_ratio",
                "conflict_chunk_count",
                "ambiguous_chunk_count",
                "write_blocked",
                "shadow_runtime_written",
            ),
        ),
        (
            "temporal_ambiguity_scope_summary",
            (
                "status",
                "ambiguous_chunk_count",
                "ambiguous_chunk_ratio",
                "vector_record_count",
                "ambiguous_record_count",
                "review_slice_count",
                "blocking_decision_count",
            ),
        ),
        (
            "temporal_evidence_guard_summary",
            (
                "source_count",
                "stale_artifact_count",
                "payload_generated_at_span_hours",
                "payload_generated_at_span_exceeds_threshold",
                "runtime_lineage_mismatch_count",
                "runtime_lineage_value_count",
                "strict_temporal_evidence",
                "passed",
            ),
        ),
        (
            "revision_impact_summary",
            (
                "report_count",
                "before_unit_count",
                "after_unit_count",
                "changed_count",
                "added_count",
                "removed_count",
                "metadata_only_changed_count",
                "approval_required_count",
                "approval_reuse_candidate_count",
                "deindex_required_count",
            ),
        ),
        (
            "runtime_version_drift_summary",
            (
                "current_chunker_version",
                "approved_repository_stale_chunker_count",
                "vector_stale_chunker_count",
                "vector_integrity_failure_count",
                "vector_integrity_content_hash_mismatch_count",
                "vector_integrity_verification_hash_mismatch_count",
                "vector_integrity_metadata_missing_required_count",
                "vector_integrity_invalid_approval_status_count",
                "vector_integrity_invalid_security_level_count",
                "vector_integrity_embedded_dimension_mismatch_count",
                "vector_integrity_embedded_failure_count",
                "vector_integrity_local_path_leak_count",
                "reprocess_requires_reapproval",
                "approved_chunks_with_approved_hash_count",
            ),
        ),
        (
            "table_preprocessing_claim_gate_summary",
            (
                "passed",
                "status",
                "feasibility_status",
                "blocker_count",
                "selected_unit_count",
                "completed_unit_count",
                "pending_unit_count",
                "invalid_unit_count",
                "transfer_blocker_count",
                "source_traceability_issue_count",
                "source_traceability_record_count",
                "source_traceability_require_page_count_verification",
                "drift_check_present",
                "drift_check_passed",
                "drift_check_blocker_count",
                "table_answer_blocker_count",
            ),
        ),
        (
            "approval_workload_summary",
            (
                "report_count",
                "document_count",
                "total_chunks",
                "manual_attention_chunks",
                "manual_attention_rate",
                "low_risk_batch_review_candidate_chunks",
                "low_risk_batch_review_candidate_rate",
                "blocking_review_chunks",
                "domain_attention_chunks",
            ),
        ),
        (
            "approval_review_batch_summary",
            (
                "report_count",
                "batch_count",
                "approval_chunk_count",
                "manual_attention_chunks",
                "low_risk_batch_review_candidate_chunks",
                "blocker_count",
                "warning_count",
            ),
        ),
        (
            "reapproval_workload_summary",
            (
                "report_count",
                "document_count",
                "reapproval_candidate_chunks",
                "high_risk_candidate_chunks",
                "temporal_sample_candidate_chunks",
                "low_risk_candidate_chunks",
                "recommended_initial_review_chunks",
                "estimated_initial_review_minutes",
                "approval_provenance_missing_chunks",
                "approval_provenance_only_chunks",
                "source_vector_integrity_failure_count",
                "pre_reapproval_blocker_count",
                "initial_review_reduction_ratio",
            ),
        ),
        (
            "reapproval_review_batch_summary",
            (
                "report_count",
                "candidate_count",
                "selected_candidate_count",
                "batch_count",
                "reapproval_chunk_count",
                "blocker_count",
                "warning_count",
                "max_chunks_per_batch",
            ),
        ),
    ):
        section = payload.get(section_key)
        if not isinstance(section, dict):
            continue
        for key in keys:
            if key in section:
                summary[f"{section_key}_{key}"] = section[key]
        if (
            section_key == "reapproval_workload_summary"
            and isinstance(section.get("approval_provenance_missing_field_counts"), dict)
        ):
            missing_counts = section["approval_provenance_missing_field_counts"]
            summary["reapproval_workload_summary_approval_provenance_missing_field_counts"] = missing_counts
            for field, count in sorted(missing_counts.items()):
                summary[f"reapproval_workload_summary_approval_provenance_missing_{field}_count"] = count
        if section_key == "reapproval_review_batch_summary":
            for count_key in ("risk_tier_chunk_counts", "action_chunk_counts"):
                counts = section.get(count_key)
                if isinstance(counts, dict):
                    summary[f"reapproval_review_batch_summary_{count_key}"] = counts
                    for field, count in sorted(counts.items()):
                        summary[f"reapproval_review_batch_summary_{count_key}_{field}_count"] = count
    runtime_summary = payload.get("runtime_summary")
    if isinstance(runtime_summary, dict):
        approval_provenance = runtime_summary.get("approval_provenance_coverage")
        if isinstance(approval_provenance, dict):
            for key in ("record_count", "complete_record_count", "complete_ratio"):
                if key in approval_provenance:
                    summary[f"runtime_summary_approval_provenance_{key}"] = approval_provenance[key]
            missing_counts = approval_provenance.get("missing_field_counts")
            if isinstance(missing_counts, dict):
                summary["runtime_summary_approval_provenance_missing_field_counts"] = missing_counts
                for field, count in sorted(missing_counts.items()):
                    summary[f"runtime_summary_approval_provenance_missing_{field}_count"] = count


def _merge_index_visibility_nested_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != "mcp_index_visibility_audit":
        return
    parser_uncertainty = payload.get("parser_uncertainty_summary")
    if isinstance(parser_uncertainty, dict):
        for key in (
            "record_count",
            "parser_uncertainty_record_count",
            "missing_parser_uncertainty_count",
        ):
            if key in parser_uncertainty:
                summary[f"parser_uncertainty_summary_{key}"] = parser_uncertainty[key]
        if isinstance(parser_uncertainty.get("risk_level_counts"), dict):
            summary["parser_uncertainty_summary_risk_level_counts"] = parser_uncertainty["risk_level_counts"]
        if isinstance(parser_uncertainty.get("flag_counts"), dict):
            summary["parser_uncertainty_summary_flag_counts"] = parser_uncertainty["flag_counts"]
    approval_provenance = payload.get("approval_provenance_coverage")
    if isinstance(approval_provenance, dict):
        for key in ("record_count", "complete_record_count"):
            if key in approval_provenance:
                summary[f"approval_provenance_coverage_{key}"] = approval_provenance[key]
        missing_counts = approval_provenance.get("missing_field_counts")
        if isinstance(missing_counts, dict):
            summary["approval_provenance_coverage_missing_field_counts"] = missing_counts
            for field, count in sorted(missing_counts.items()):
                summary[f"approval_provenance_coverage_missing_{field}_count"] = count
    approval_journal = payload.get("approval_journal_coverage")
    if isinstance(approval_journal, dict):
        for key in (
            "journal_record_count",
            "record_count",
            "eligible_record_count",
            "matched_record_count",
            "missing_record_count",
        ):
            if key in approval_journal:
                summary[f"approval_journal_coverage_{key}"] = approval_journal[key]


def _merge_connection_readiness_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != "mcp_connection_readiness":
        return
    visibility = payload.get("mcp_index_visibility_summary")
    if not isinstance(visibility, dict):
        return
    for key in (
        "passed",
        "tenant_id",
        "document_count",
        "total_approved_chunks",
        "total_mcp_visible_records",
        "finding_count",
    ):
        if key in visibility:
            summary[f"mcp_index_visibility_{key}"] = visibility[key]
    provenance = visibility.get("approval_provenance_coverage")
    if isinstance(provenance, dict):
        for key in ("record_count", "complete_record_count", "complete_ratio"):
            if key in provenance:
                summary[f"mcp_index_visibility_approval_provenance_coverage_{key}"] = provenance[key]
        missing_counts = provenance.get("missing_field_counts")
        if isinstance(missing_counts, dict):
            summary["mcp_index_visibility_approval_provenance_coverage_missing_field_counts"] = missing_counts
            for field, count in sorted(missing_counts.items()):
                summary[f"mcp_index_visibility_approval_provenance_coverage_missing_{field}_count"] = count
    journal = visibility.get("approval_journal_coverage")
    if isinstance(journal, dict):
        for key in (
            "journal_record_count",
            "record_count",
            "eligible_record_count",
            "matched_record_count",
            "missing_record_count",
        ):
            if key in journal:
                summary[f"mcp_index_visibility_approval_journal_coverage_{key}"] = journal[key]


def _merge_reapproval_worklist_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != "reapproval_worklist":
        return
    missing_counts = payload.get("approval_provenance_missing_field_counts")
    if isinstance(missing_counts, dict):
        summary["approval_provenance_missing_field_counts"] = missing_counts
        for field, count in sorted(missing_counts.items()):
            summary[f"approval_provenance_missing_{field}_count"] = count


def _merge_publish_runtime_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") not in {"mcp_publish_runtime", "aks_mcp_publish_runtime"}:
        return
    approval_evidence = payload.get("approval_evidence")
    if not isinstance(approval_evidence, dict):
        return
    for key in (
        "worklist_report_path",
        "worklist_report_sha256",
        "review_batch_manifest_path",
        "review_batch_manifest_sha256",
        "approval_request_count",
        "approval_chunk_count",
        "manual_attention_batch_count",
        "manual_attention_chunk_count",
    ):
        if key in approval_evidence:
            summary[f"approval_evidence_{key}"] = approval_evidence[key]
    review_type_counts = approval_evidence.get("review_type_batch_counts")
    if isinstance(review_type_counts, dict):
        summary["approval_evidence_review_type_batch_counts"] = review_type_counts
        for review_type, count in sorted(review_type_counts.items()):
            summary[f"approval_evidence_review_type_batch_counts_{review_type}_count"] = count
    artifacts = approval_evidence.get("artifacts")
    if isinstance(artifacts, dict):
        for artifact_role, artifact_path in sorted(artifacts.items()):
            summary[f"approval_evidence_artifact_{artifact_role}"] = artifact_path


def _merge_temporal_ambiguity_review_scope_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != "temporal_ambiguity_review_scope":
        return
    temporal_summary = payload.get("summary")
    if isinstance(temporal_summary, dict):
        for key in (
            "chunk_count",
            "before_temporal_metadata_count",
            "after_temporal_metadata_count",
            "delta_temporal_metadata_count",
            "conflict_chunk_count",
            "ambiguous_chunk_count",
            "ambiguous_chunk_ratio",
            "shadow_runtime_written",
            "write_blocked",
        ):
            if key in temporal_summary:
                summary[f"temporal_ambiguity_{key}"] = temporal_summary[key]
    record_analysis = payload.get("record_analysis")
    if isinstance(record_analysis, dict):
        for key in ("vector_record_count", "ambiguous_record_count", "review_slice_count"):
            if key in record_analysis:
                summary[f"temporal_ambiguity_{key}"] = record_analysis[key]
        for count_key in ("ambiguous_by_chunk_type", "ambiguous_by_field_from_records"):
            counts = record_analysis.get(count_key)
            if isinstance(counts, dict):
                summary[f"temporal_ambiguity_{count_key}"] = counts


def _merge_demo_answer_item_summary(summary: dict[str, Any], payload: dict[str, Any]) -> None:
    if payload.get("report_type") != "mcp_demo_answers":
        return
    items = [item for item in payload.get("items", []) or [] if isinstance(item, dict)]
    ratios = [
        float(item.get("expected_term_hit_ratio"))
        for item in items
        if item.get("expected_terms") and _is_number_like(item.get("expected_term_hit_ratio"))
    ]
    if ratios:
        summary.setdefault("expected_term_min_hit_ratio", round(min(ratios), 3))
        summary.setdefault("expected_term_average_hit_ratio", round(sum(ratios) / len(ratios), 3))
        summary.setdefault("expected_term_low_hit_count", sum(1 for ratio in ratios if ratio < 0.5))


def _is_number_like(value: Any) -> bool:
    if isinstance(value, bool) or value in (None, ""):
        return False
    try:
        float(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _display_artifact_path(repo_root: Path, original_path: Path, candidate: Path) -> str:
    if not original_path.is_absolute():
        return original_path.as_posix()
    resolved_candidate = candidate.resolve()
    try:
        return resolved_candidate.relative_to(repo_root).as_posix()
    except ValueError:
        return f"outside-repo/{resolved_candidate.name}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a JSON evidence index for existing release or Hermes handoff artifacts without re-running gates."
    )
    parser.add_argument("--repo-root", default=None, help="Repository root used to resolve relative artifact paths.")
    parser.add_argument("--out-json", default=None, help="Optional path to write the evidence index JSON.")
    parser.add_argument(
        "--profile",
        choices=EVIDENCE_PROFILES,
        default="private-release",
        help="Evidence artifact preset to index when --artifact is not supplied.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=None,
        help="Artifact path to index; may be repeated. Relative paths are resolved from --repo-root.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()
    index = build_release_evidence_index(repo_root, artifact_paths=args.artifact, evidence_profile=args.profile)
    output = json.dumps(index, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(output, encoding="utf-8")
    print(output, file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
