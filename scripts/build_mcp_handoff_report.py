from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_contracts import REPORT_TYPE_MCP_HANDOFF_REPORT
from scripts.mcp_bundle_contract import REQUIRED_SETUP_BUNDLE_FILES, SETUP_BUNDLE_FILES
from scripts.report_metadata import current_repo_commit

BUNDLE_FILES = SETUP_BUNDLE_FILES
HASH_CHUNK_BYTES = 1024 * 1024
HANDOFF_SCHEMA_VERSION = 2
REQUIRED_APPROVAL_REVIEW_EVENT_TYPES = (
    "ai_review_confirmed",
    "approved",
    "human_review_confirmed",
)


def build_mcp_handoff_report(
    *,
    product_readiness_report: Path,
    mcp_demo_answer_report: Path,
    mcp_readiness_report: Path | None = None,
    mcp_index_visibility_report: Path | None = None,
    mcp_query_benchmark_report: Path | None = None,
    authority_manifest: Path | None = None,
    bundle_dir: Path | None = None,
    server_name: str | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    product = _load_json(product_readiness_report)
    demo = _load_json(mcp_demo_answer_report)
    readiness = _load_json(mcp_readiness_report) if mcp_readiness_report else {}
    index_visibility = _load_json(mcp_index_visibility_report) if mcp_index_visibility_report else {}
    query_benchmark = _load_json(mcp_query_benchmark_report) if mcp_query_benchmark_report else {}
    authority = _load_json(authority_manifest) if authority_manifest else {}
    bundle = _inspect_bundle(bundle_dir, expected_server_name=server_name) if bundle_dir else {}
    source_report_artifacts = _source_report_artifacts(
        product_readiness_report=product_readiness_report,
        mcp_demo_answer_report=mcp_demo_answer_report,
        mcp_readiness_report=mcp_readiness_report,
        mcp_index_visibility_report=mcp_index_visibility_report,
        mcp_query_benchmark_report=mcp_query_benchmark_report,
        authority_manifest=authority_manifest,
    )
    findings = [
        *_authority_findings(authority, product_readiness_report),
        *_product_findings(product),
        *_demo_findings(demo),
        *_readiness_findings(readiness),
        *_index_visibility_findings(index_visibility),
        *_query_benchmark_findings(query_benchmark),
        *_bundle_findings(bundle),
        *_bundle_product_lineage_findings(bundle, product),
    ]
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    report = {
        "report_type": REPORT_TYPE_MCP_HANDOFF_REPORT,
        "handoff_schema_version": HANDOFF_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "server_name": server_name or bundle.get("server_name") or "",
        "decision": "ready_for_local_claude_desktop_mvp" if blocker_count == 0 and warning_count == 0 else "needs_attention",
        "passed": blocker_count == 0,
        "handoff_ready": blocker_count == 0 and warning_count == 0,
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "findings": findings,
        "source_reports": {
            "product_readiness_report": str(product_readiness_report),
            "mcp_demo_answer_report": str(mcp_demo_answer_report),
            "mcp_readiness_report": str(mcp_readiness_report) if mcp_readiness_report else None,
            "mcp_index_visibility_report": str(mcp_index_visibility_report) if mcp_index_visibility_report else None,
            "mcp_query_benchmark_report": str(mcp_query_benchmark_report) if mcp_query_benchmark_report else None,
            "authority_manifest": str(authority_manifest) if authority_manifest else None,
            "bundle_dir": str(bundle_dir) if bundle_dir else None,
        },
        "source_report_artifacts": source_report_artifacts,
        "authority_summary": _authority_summary(authority),
        "product_summary": _product_summary(product),
        "demo_summary": _demo_summary(demo),
        "mcp_readiness_summary": _readiness_summary(readiness),
        "mcp_index_visibility_summary": _index_visibility_summary(index_visibility),
        "mcp_query_benchmark_summary": _query_benchmark_summary(query_benchmark),
        "bundle_summary": bundle,
        "operator_steps": _operator_steps(bundle_dir, demo),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _product_summary(report: dict[str, Any]) -> dict[str, Any]:
    runtime = _dict(report.get("runtime_summary"))
    review_events = _approval_journal_review_event_summary(
        _dict(runtime.get("approval_journal_review_event_coverage"))
    )
    public_readiness = _dict(report.get("public_readiness_summary"))
    rag_eval = _dict(report.get("rag_eval_summary"))
    approval_workload = _dict(report.get("approval_workload_summary"))
    approval_review_batch = _dict(report.get("approval_review_batch_summary"))
    reapproval_workload = _dict(report.get("reapproval_workload_summary"))
    reapproval_review_batch = _dict(report.get("reapproval_review_batch_summary"))
    gates = _dict(report.get("gates"))
    return {
        "passed": bool(report.get("passed")),
        "blocking_count": _int(report.get("blocking_count")),
        "warning_count": _int(report.get("warning_count")),
        "api_call_count": _int(report.get("api_call_count")),
        "tenant_id": str(report.get("tenant_id") or ""),
        "effective_runtime_data_dir": str(report.get("effective_runtime_data_dir") or ""),
        "repository_chunk_count": _int(runtime.get("repository_chunk_count")),
        "vector_record_count": _int(runtime.get("vector_record_count")),
        "full_index_match": bool(runtime.get("full_index_match")),
        "approval_journal_review_event_coverage": review_events,
        "article_like_count": _int(runtime.get("article_like_count")),
        "appendix_count": _int(runtime.get("appendix_count")),
        "supplementary_count": _int(runtime.get("supplementary_count")),
        "public_batch_inputs": _int(public_readiness.get("input_count")),
        "public_batch_successful": _int(public_readiness.get("successful_count")),
        "public_batch_failed": _int(public_readiness.get("failed_count")),
        "rag_answerable_ratio": float(rag_eval.get("answerable_ratio") or 0.0),
        "rag_quality_warning_chunk_count": _int(rag_eval.get("quality_warning_chunk_count")),
        "approval_workload": {
            "report_count": _int(approval_workload.get("report_count")),
            "document_count": _int(approval_workload.get("document_count")),
            "total_chunks": _int(approval_workload.get("total_chunks")),
            "manual_attention_chunks": _int(approval_workload.get("manual_attention_chunks")),
            "manual_attention_rate": _float(approval_workload.get("manual_attention_rate")),
            "low_risk_batch_review_candidate_chunks": _int(
                approval_workload.get("low_risk_batch_review_candidate_chunks")
            ),
            "low_risk_batch_review_candidate_rate": _float(
                approval_workload.get("low_risk_batch_review_candidate_rate")
            ),
            "blocking_review_chunks": _int(approval_workload.get("blocking_review_chunks")),
            "domain_attention_chunks": _int(approval_workload.get("domain_attention_chunks")),
        },
        "approval_review_batch": {
            "report_count": _int(approval_review_batch.get("report_count")),
            "batch_count": _int(approval_review_batch.get("batch_count")),
            "approval_chunk_count": _int(approval_review_batch.get("approval_chunk_count")),
            "manual_attention_chunks": _int(approval_review_batch.get("manual_attention_chunks")),
            "low_risk_batch_review_candidate_chunks": _int(
                approval_review_batch.get("low_risk_batch_review_candidate_chunks")
            ),
            "blocker_count": _int(approval_review_batch.get("blocker_count")),
            "warning_count": _int(approval_review_batch.get("warning_count")),
        },
        "reapproval_workload": {
            "report_count": _int(reapproval_workload.get("report_count")),
            "document_count": _int(reapproval_workload.get("document_count")),
            "reapproval_candidate_chunks": _int(reapproval_workload.get("reapproval_candidate_chunks")),
            "recommended_initial_review_chunks": _int(
                reapproval_workload.get("recommended_initial_review_chunks")
            ),
            "estimated_initial_review_minutes": _int(
                reapproval_workload.get("estimated_initial_review_minutes")
            ),
            "approval_provenance_missing_chunks": _int(
                reapproval_workload.get("approval_provenance_missing_chunks")
            ),
            "approval_provenance_only_chunks": _int(
                reapproval_workload.get("approval_provenance_only_chunks")
            ),
            "approval_provenance_missing_field_counts": dict(
                reapproval_workload.get("approval_provenance_missing_field_counts")
                if isinstance(reapproval_workload.get("approval_provenance_missing_field_counts"), dict)
                else {}
            ),
            "source_vector_integrity_failure_count": _int(
                reapproval_workload.get("source_vector_integrity_failure_count")
            ),
            "pre_reapproval_blocker_count": _int(reapproval_workload.get("pre_reapproval_blocker_count")),
            "initial_review_reduction_ratio": _float4(
                reapproval_workload.get("initial_review_reduction_ratio")
            ),
        },
        "reapproval_review_batch": {
            "report_count": _int(reapproval_review_batch.get("report_count")),
            "candidate_count": _int(reapproval_review_batch.get("candidate_count")),
            "selected_candidate_count": _int(
                reapproval_review_batch.get("selected_candidate_count")
            ),
            "batch_count": _int(reapproval_review_batch.get("batch_count")),
            "reapproval_chunk_count": _int(reapproval_review_batch.get("reapproval_chunk_count")),
            "blocker_count": _int(reapproval_review_batch.get("blocker_count")),
            "warning_count": _int(reapproval_review_batch.get("warning_count")),
            "risk_tier_chunk_counts": dict(
                reapproval_review_batch.get("risk_tier_chunk_counts")
                if isinstance(reapproval_review_batch.get("risk_tier_chunk_counts"), dict)
                else {}
            ),
            "action_chunk_counts": dict(
                reapproval_review_batch.get("action_chunk_counts")
                if isinstance(reapproval_review_batch.get("action_chunk_counts"), dict)
                else {}
            ),
        },
        "gates": {
            key: {
                "status": str(_dict(value).get("status") or ""),
                "blocker_count": _int(_dict(value).get("blocker_count")),
                "warning_count": _int(_dict(value).get("warning_count")),
            }
            for key, value in gates.items()
        },
    }


def _approval_journal_review_event_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    status = _approval_review_event_coverage_status(report)
    return {
        "journal_record_count": _int(report.get("journal_record_count")),
        "applicable_record_count": _int(report.get("applicable_record_count")),
        "chunk_reference_count": _int(report.get("chunk_reference_count")),
        "review_decision_event_count": _int(report.get("review_decision_event_count")),
        "expected_event_chunk_counts": _int_dict(report.get("expected_event_chunk_counts")),
        "event_chunk_counts": _int_dict(report.get("event_chunk_counts")),
        "missing_event_chunk_counts": _int_dict(report.get("missing_event_chunk_counts")),
        "incomplete_record_count": _int(report.get("incomplete_record_count")),
        "computed_missing_event_chunk_counts": status["computed_missing_event_chunk_counts"],
        "missing_required_event_types": status["missing_required_event_types"],
        "malformed_count_fields": status["malformed_count_fields"],
    }


def _authority_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "blocking_count": _int(report.get("blocking_count")),
        "warning_count": _int(report.get("warning_count")),
        "finding_count": _int(report.get("finding_count")),
        "authoritative_artifact_count": len(report.get("authoritative_artifacts") or []),
        "supersedes_count": len(report.get("supersedes") or []),
        "repo_commit": str(report.get("repo_commit") or ""),
    }


def _demo_summary(report: dict[str, Any]) -> dict[str, Any]:
    items = [_dict(item) for item in report.get("items", []) or [] if isinstance(item, dict)]
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
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "query_count": _int(report.get("query_count")) or len(items),
        "quality_issue_count": _int(report.get("quality_issue_count")),
        "api_call_count": _int(report.get("api_call_count")),
        "queries": [str(item.get("query") or "") for item in items],
        "supporting_result_counts": [_int(item.get("supporting_result_count")) for item in items],
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
        "sample_citations": [
            {
                "query": str(item.get("query") or ""),
                "citations": [
                    {
                        "document_id": str(citation.get("document_id") or ""),
                        "chunk_id": str(citation.get("chunk_id") or ""),
                        "article_no": str(citation.get("article_no") or ""),
                        "article_title": str(citation.get("article_title") or ""),
                    }
                    for citation in item.get("citations", [])[:3]
                    if isinstance(citation, dict)
                ],
            }
            for item in items[:5]
        ],
    }


def _readiness_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "passed": bool(report.get("passed")),
        "deploy_ready": bool(report.get("deploy_ready")),
        "high_count": _int(report.get("high_count")),
        "medium_count": _int(report.get("medium_count")),
        "finding_count": _int(report.get("finding_count")),
        "client_profile": str(report.get("client_profile") or ""),
        "connection_mode": str(report.get("connection_mode") or ""),
        "transport": str(report.get("transport") or ""),
        "allow_local_only_bundle": bool(report.get("allow_local_only_bundle")),
    }


def _index_visibility_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "passed": bool(report.get("passed")),
        "document_count": _int(report.get("document_count")),
        "total_approved_chunks": _int(report.get("total_approved_chunks")),
        "total_mcp_visible_records": _int(report.get("total_mcp_visible_records")),
        "total_skipped_unapproved_count": _int(report.get("total_skipped_unapproved_count")),
        "smoke_like_document_count": _int(report.get("smoke_like_document_count")),
        "parser_evidence_summary": _dict(report.get("parser_evidence_summary")),
        "parser_uncertainty_summary": _dict(report.get("parser_uncertainty_summary")),
        "approval_provenance_coverage": _dict(report.get("approval_provenance_coverage")),
        "approval_journal_coverage": _dict(report.get("approval_journal_coverage")),
        "status_counts": _dict(report.get("status_counts")),
        "finding_count": _int(report.get("finding_count")),
        "effective_data_dir": str(report.get("effective_data_dir") or ""),
    }


def _query_benchmark_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    summary = _dict(report.get("summary"))
    total = _dict(summary.get("total_elapsed_ms"))
    warm_search = _dict(summary.get("warm_search_elapsed_ms"))
    warmup = _dict(report.get("warmup"))
    return {
        "passed": bool(report.get("passed")),
        "query_count": _int(report.get("query_count")),
        "iterations": _int(report.get("iterations")),
        "warm_record_count": _int(warmup.get("record_count")),
        "min_warm_records": _int(report.get("min_warm_records")),
        "measurement_count": _int(summary.get("measurement_count")),
        "finding_count": _int(report.get("finding_count")),
        "total_p50_ms": _float(total.get("p50")),
        "total_p95_ms": _float(total.get("p95")),
        "total_max_ms": _float(total.get("max")),
        "warm_search_p50_ms": _float(warm_search.get("p50")),
        "warm_search_p95_ms": _float(warm_search.get("p95")),
        "warm_search_max_ms": _float(warm_search.get("max")),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _inspect_bundle(bundle_dir: Path | None, *, expected_server_name: str | None) -> dict[str, Any]:
    if bundle_dir is None:
        return {}
    summary: dict[str, Any] = {
        "bundle_dir": str(bundle_dir),
        "exists": bundle_dir.is_dir(),
        "missing_files": [],
        "claude_desktop_config_valid": False,
        "claude_desktop_server_names": [],
        "server_name": "",
        "expected_server_present": False,
    }
    if not bundle_dir.is_dir():
        return summary
    missing = [name for name in sorted(REQUIRED_SETUP_BUNDLE_FILES) if not bundle_dir.joinpath(name).is_file()]
    summary["missing_files"] = missing
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = _load_json(manifest_path)
        summary["server_name"] = str(manifest.get("server_name") or "")
        summary["manifest_ready"] = manifest.get("ready") if isinstance(manifest.get("ready"), dict) else {}
    runtime_manifest_path = bundle_dir / "data" / "mcp_runtime_manifest.json"
    if runtime_manifest_path.is_file():
        try:
            runtime_manifest = _load_json(runtime_manifest_path)
        except json.JSONDecodeError as exc:
            summary["runtime_manifest_json_error"] = str(exc)
        else:
            document_ids = runtime_manifest.get("document_ids")
            summary["runtime_manifest"] = {
                "path": str(runtime_manifest_path),
                "tenant_id": str(runtime_manifest.get("tenant_id") or ""),
                "tenant_storage_isolation": bool(runtime_manifest.get("tenant_storage_isolation")),
                "document_ids": [str(value) for value in document_ids] if isinstance(document_ids, list) else [],
                "record_count": _int(runtime_manifest.get("record_count")),
                "chunk_count": _int(runtime_manifest.get("chunk_count")),
                "source_data_dir": str(runtime_manifest.get("source_data_dir") or ""),
                "runtime_data_dir": str(runtime_manifest.get("runtime_data_dir") or ""),
                "kordoc_table_parser_required": bool(runtime_manifest.get("kordoc_table_parser_required")),
                "kordoc_table_parser_summary": _dict(runtime_manifest.get("kordoc_table_parser_summary")),
            }
    config_path = bundle_dir / BUNDLE_FILES["claude_desktop"]
    if config_path.is_file():
        try:
            config = _load_json(config_path)
        except json.JSONDecodeError as exc:
            summary["claude_desktop_json_error"] = str(exc)
        else:
            servers = config.get("mcpServers") if isinstance(config, dict) else None
            if isinstance(servers, dict) and servers:
                summary["claude_desktop_config_valid"] = True
                summary["claude_desktop_server_names"] = sorted(str(name) for name in servers)
    expected = expected_server_name or str(summary.get("server_name") or "")
    summary["expected_server_name"] = expected
    summary["expected_server_present"] = bool(
        expected and expected in set(summary.get("claude_desktop_server_names") or [])
    )
    return summary


def _source_report_artifacts(
    *,
    product_readiness_report: Path,
    mcp_demo_answer_report: Path,
    mcp_readiness_report: Path | None,
    mcp_index_visibility_report: Path | None,
    mcp_query_benchmark_report: Path | None,
    authority_manifest: Path | None,
) -> list[dict[str, Any]]:
    paths = [
        ("product_readiness_report", product_readiness_report),
        ("mcp_demo_answer_report", mcp_demo_answer_report),
        ("mcp_readiness_report", mcp_readiness_report),
        ("mcp_index_visibility_report", mcp_index_visibility_report),
        ("mcp_query_benchmark_report", mcp_query_benchmark_report),
        ("authority_manifest", authority_manifest),
    ]
    return [_source_report_artifact(role, path) for role, path in paths if path is not None]


def _source_report_artifact(role: str, path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "role": role,
        "path": str(path),
        "exists": path.is_file(),
        "byte_count": None,
        "sha256": None,
        "report_type": "",
        "repo_commit": "",
        "passed": None,
    }
    if not path.is_file():
        return item
    item["byte_count"] = path.stat().st_size
    item["sha256"] = _sha256_file(path)
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return item
    item["report_type"] = str(payload.get("report_type") or payload.get("index_type") or "")
    item["repo_commit"] = str(payload.get("repo_commit") or "")
    if "passed" in payload:
        item["passed"] = bool(payload.get("passed"))
    return item


def _authority_findings(authority: dict[str, Any], product_readiness_report: Path) -> list[dict[str, str]]:
    if not authority:
        return []
    findings: list[dict[str, str]] = []
    if authority.get("report_type") != "mcp_readiness_authority":
        findings.append(_finding("blocker", "authority-report-type", "Authority manifest is not an MCP readiness authority report."))
        return findings
    if not bool(authority.get("passed")) or _int(authority.get("blocking_count")) > 0:
        findings.append(_finding("blocker", "authority-not-passing", "MCP readiness authority manifest did not pass."))
    product_path = _normalize_artifact_path(str(product_readiness_report))
    product_sha = _sha256_file(product_readiness_report) if product_readiness_report.is_file() else ""
    authoritative = [item for item in authority.get("authoritative_artifacts") or [] if isinstance(item, dict)]
    product_is_authoritative = any(
        item.get("role") == "product_readiness"
        and (
            _normalize_artifact_path(str(item.get("path") or "")) == product_path
            or (product_sha and str(item.get("sha256") or "") == product_sha)
        )
        for item in authoritative
    )
    if not product_is_authoritative:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-not-authoritative",
                "Product readiness report is not listed as the authoritative product_readiness artifact.",
            )
        )
    superseded = [item for item in authority.get("supersedes") or [] if isinstance(item, dict)]
    product_is_superseded = any(
        _normalize_artifact_path(str(item.get("path") or "")) == product_path
        or (product_sha and str(item.get("sha256") or "") == product_sha)
        for item in superseded
    )
    if product_is_superseded:
        findings.append(
            _finding(
                "blocker",
                "product-readiness-superseded",
                "Product readiness report is explicitly superseded by the authority manifest.",
            )
        )
    return findings


def _product_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not bool(report.get("passed")):
        findings.append(_finding("blocker", "product-readiness-failed", "MCP product readiness did not pass."))
    if _int(report.get("blocking_count")) > 0:
        findings.append(_finding("blocker", "product-readiness-blockers", "Product readiness still has blocking findings."))
    if _int(report.get("warning_count")) > 0:
        findings.append(_finding("warning", "product-readiness-warnings", "Product readiness still has warning findings."))
    if _int(report.get("api_call_count")) > 0:
        findings.append(_finding("warning", "unexpected-api-calls", "Readiness report recorded API calls."))
    review_events = _dict(_dict(report.get("runtime_summary")).get("approval_journal_review_event_coverage"))
    review_event_status = _approval_review_event_coverage_status(review_events)
    if not review_events and _int(_dict(report.get("runtime_summary")).get("vector_record_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "approval-journal-review-events-missing",
                "Approval journal review_decision_event coverage is missing.",
            )
        )
    elif (
        review_event_status["incomplete_record_count"] > 0
        or any(count > 0 for count in review_event_status["computed_missing_event_chunk_counts"].values())
        or review_event_status["missing_required_event_types"]
    ):
        findings.append(
            _finding(
                "blocker",
                "approval-journal-review-events-incomplete",
                "Approval journal review_decision_events do not cover every approved chunk.",
            )
        )
    if review_event_status["malformed_count_fields"]:
        findings.append(
            _finding(
                "blocker",
                "approval-journal-review-events-malformed",
                "Approval journal review_decision_event coverage contains malformed counts.",
            )
        )
    for gate_name, gate in _dict(report.get("gates")).items():
        gate_payload = _dict(gate)
        gate_status = str(gate_payload.get("status") or "").lower()
        gate_blocker_count = _int(gate_payload.get("blocker_count"))
        if gate_status in {"blocked", "failed"} or gate_blocker_count > 0:
            findings.append(
                _finding(
                    "blocker",
                    "product-gate-blocked",
                    f"Product readiness gate {gate_name} is blocked: status={gate_status or 'missing'}, blockers={gate_blocker_count}.",
                )
            )
    return findings


def _demo_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if str(report.get("report_type") or "") != "mcp_demo_answers":
        findings.append(
            _finding(
                "blocker",
                "demo-report-type-mismatch",
                "MCP demo answer report must have report_type=mcp_demo_answers.",
            )
        )
        return findings
    if not bool(report.get("passed")):
        findings.append(_finding("blocker", "demo-answers-failed", "MCP demo answers did not pass."))
    items = [item for item in report.get("items", []) or [] if isinstance(item, dict)]
    if (_int(report.get("query_count")) or len(items)) <= 0:
        findings.append(_finding("blocker", "demo-answer-query-count-zero", "MCP demo answer report contains no demo queries."))
    if _int(report.get("quality_issue_count")) > 0:
        findings.append(_finding("blocker", "demo-answer-quality-issues", "MCP demo answers contain quality issues."))
    if _int(report.get("api_call_count")) > 0:
        findings.append(_finding("warning", "demo-api-calls", "Demo answer export recorded API calls."))
    return findings


def _readiness_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    if not report:
        return [_finding("warning", "mcp-readiness-report-missing", "No MCP connection readiness report was provided.")]
    findings: list[dict[str, str]] = []
    if not bool(report.get("passed")) or _int(report.get("high_count")) > 0:
        findings.append(_finding("blocker", "mcp-readiness-high-findings", "MCP readiness doctor has high findings."))
    if _int(report.get("medium_count")) > 0:
        findings.append(_finding("warning", "mcp-readiness-medium-findings", "MCP readiness doctor has medium findings."))
    return findings


def _index_visibility_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    if not report:
        return []
    findings: list[dict[str, str]] = []
    if not bool(report.get("passed")):
        findings.append(_finding("blocker", "mcp-index-visibility-failed", "MCP index visibility audit did not pass."))
    if _int(report.get("total_mcp_visible_records")) <= 0:
        findings.append(_finding("blocker", "mcp-index-empty", "No approved records are visible to MCP clients."))
    if _int(report.get("smoke_like_document_count")):
        findings.append(_finding("blocker", "mcp-smoke-documents-visible", "Smoke-test documents are visible in the MCP runtime."))
    approval_journal = _dict(report.get("approval_journal_coverage"))
    if _int(report.get("total_mcp_visible_records")) > 0 and not approval_journal:
        findings.append(
            _finding(
                "blocker",
                "approval-journal-coverage-missing",
                "MCP index visibility report does not include append-only approval journal coverage.",
            )
        )
    elif _int(approval_journal.get("missing_record_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "approval-journal-records-missing",
                "Some MCP-visible approved records lack matching append-only approval journal records.",
            )
        )
    elif _int(approval_journal.get("eligible_record_count")) > 0 and _int(
        approval_journal.get("matched_record_count")
    ) < _int(approval_journal.get("eligible_record_count")):
        findings.append(
            _finding(
                "blocker",
                "approval-journal-coverage-incomplete",
                "MCP-visible approval journal coverage is incomplete.",
            )
        )
    return findings


def _query_benchmark_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    if not report:
        return []
    findings: list[dict[str, str]] = []
    if not bool(report.get("passed")):
        findings.append(
            _finding("warning", "mcp-query-benchmark-failed", "MCP query benchmark exceeded a configured threshold or returned no results.")
        )
    if _int(report.get("api_call_count")) > 0:
        findings.append(_finding("warning", "mcp-query-benchmark-api-calls", "MCP query benchmark recorded API calls."))
    return findings


def _bundle_findings(summary: dict[str, Any]) -> list[dict[str, str]]:
    if not summary:
        return [_finding("warning", "bundle-not-provided", "No MCP setup bundle was provided.")]
    findings: list[dict[str, str]] = []
    if not summary.get("exists"):
        findings.append(_finding("blocker", "bundle-dir-missing", "MCP setup bundle directory does not exist."))
        return findings
    if summary.get("missing_files"):
        missing = ", ".join(summary.get("missing_files") or [])
        findings.append(_finding("blocker", "bundle-files-missing", f"MCP setup bundle is missing required files: {missing}."))
    if not summary.get("claude_desktop_config_valid"):
        findings.append(_finding("blocker", "claude-desktop-config-invalid", "Claude Desktop config is missing a non-empty mcpServers object."))
    elif summary.get("expected_server_name") and not summary.get("expected_server_present"):
        findings.append(_finding("blocker", "claude-desktop-server-missing", "Claude Desktop config does not contain the expected MCP server name."))
    return findings


def _bundle_product_lineage_findings(bundle: dict[str, Any], product: dict[str, Any]) -> list[dict[str, str]]:
    runtime_manifest = _dict(bundle.get("runtime_manifest"))
    if not runtime_manifest or not product:
        return []
    findings: list[dict[str, str]] = []
    product_tenant = str(product.get("tenant_id") or "")
    bundle_tenant = str(runtime_manifest.get("tenant_id") or "")
    if product_tenant and bundle_tenant and product_tenant != bundle_tenant:
        findings.append(
            _finding(
                "blocker",
                "bundle-product-tenant-mismatch",
                f"MCP bundle tenant_id ({bundle_tenant}) does not match product readiness tenant_id ({product_tenant}).",
            )
        )
    runtime = _dict(product.get("runtime_summary"))
    product_record_count = _int(runtime.get("vector_record_count"))
    bundle_record_count = _int(runtime_manifest.get("record_count"))
    if product_record_count and bundle_record_count and product_record_count != bundle_record_count:
        findings.append(
            _finding(
                "blocker",
                "bundle-product-record-count-mismatch",
                f"MCP bundle record_count ({bundle_record_count}) does not match product readiness vector_record_count ({product_record_count}).",
            )
        )
    return findings


def _operator_steps(bundle_dir: Path | None, demo_report: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = str(bundle_dir) if bundle_dir else "<bundle-dir>"
    queries = [str(item.get("query") or "") for item in demo_report.get("items", [])[:5] if isinstance(item, dict)]
    return [
        {
            "name": "install_package",
            "command": f'powershell -ExecutionPolicy Bypass -File "{bundle}\\install_local_package.ps1"',
        },
        {
            "name": "run_doctor",
            "command": f'powershell -ExecutionPolicy Bypass -File "{bundle}\\doctor_mcp_connection.ps1"',
        },
        {
            "name": "validate_claude_desktop_config",
            "command": f'powershell -ExecutionPolicy Bypass -File "{bundle}\\connect_mcp_client.ps1" -Target claude-desktop -ValidateClaudeDesktop',
            "note": "Run before merging if Claude Desktop has shown a JSON parse error.",
        },
        {
            "name": "merge_claude_desktop_config",
            "source_file": f"{bundle}\\claude_desktop_config.json",
            "target_file": r"%APPDATA%\Claude\claude_desktop_config.json",
            "command": f'powershell -ExecutionPolicy Bypass -File "{bundle}\\connect_mcp_client.ps1" -Target claude-desktop -InstallClaudeDesktop',
            "note": "Prefer the automatic merge command. If merging manually, merge only the mcpServers object and do not paste an extra top-level object inside an existing JSON object.",
        },
        {
            "name": "restart_claude_desktop",
            "note": "Fully quit Claude Desktop from the tray/task manager, then start it again so the MCP tools load.",
        },
        {
            "name": "ask_demo_queries",
            "queries": queries,
        },
    ]


def _to_markdown(report: dict[str, Any]) -> str:
    product = report.get("product_summary") or {}
    demo = report.get("demo_summary") or {}
    readiness = report.get("mcp_readiness_summary") or {}
    visibility = report.get("mcp_index_visibility_summary") or {}
    authority = report.get("authority_summary") or {}
    approval_workload = product.get("approval_workload") or {}
    approval_review_batch = product.get("approval_review_batch") or {}
    approval_review_events = _dict(product.get("approval_journal_review_event_coverage"))
    reapproval_workload = product.get("reapproval_workload") or {}
    reapproval_review_batch = product.get("reapproval_review_batch") or {}
    parser_evidence = _dict(visibility.get("parser_evidence_summary"))
    parser_uncertainty = _dict(visibility.get("parser_uncertainty_summary"))
    approval_provenance = _dict(visibility.get("approval_provenance_coverage"))
    approval_journal = _dict(visibility.get("approval_journal_coverage"))
    benchmark = report.get("mcp_query_benchmark_summary") or {}
    bundle = report.get("bundle_summary") or {}
    lines = [
        "# AKS MCP Handoff Report",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Server: `{report.get('server_name')}`",
        f"- Decision: `{report.get('decision')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Handoff ready: `{str(report.get('handoff_ready')).lower()}`",
        f"- Blocking: {report.get('blocking_count')}",
        f"- Warnings: {report.get('warning_count')}",
        "",
        "## Current State",
        "",
        f"- Product readiness: `{str(product.get('passed')).lower()}` / blockers {product.get('blocking_count')} / warnings {product.get('warning_count')}",
        f"- Approved chunks and vectors: {product.get('repository_chunk_count')} / {product.get('vector_record_count')}",
        f"- Full index match: `{str(product.get('full_index_match')).lower()}`",
        f"- Public batch: {product.get('public_batch_successful')} successful of {product.get('public_batch_inputs')}",
        f"- Approval workload: manual attention {approval_workload.get('manual_attention_chunks')} ({approval_workload.get('manual_attention_rate')}%) / low-risk human batch candidates {approval_workload.get('low_risk_batch_review_candidate_chunks')} ({approval_workload.get('low_risk_batch_review_candidate_rate')}%)",
        f"- Approval priority blockers/domain: {approval_workload.get('blocking_review_chunks')} / {approval_workload.get('domain_attention_chunks')}",
        f"- Approval review batches: {approval_review_batch.get('batch_count')} batches / {approval_review_batch.get('approval_chunk_count')} chunks / blockers {approval_review_batch.get('blocker_count')}",
        f"- Reapproval workload: candidates {reapproval_workload.get('reapproval_candidate_chunks')} / initial human review {reapproval_workload.get('recommended_initial_review_chunks')} / reduction ratio {reapproval_workload.get('initial_review_reduction_ratio')}",
        f"- Reapproval approval provenance gaps: {reapproval_workload.get('approval_provenance_missing_chunks')} / provenance-only {reapproval_workload.get('approval_provenance_only_chunks')} / missing {reapproval_workload.get('approval_provenance_missing_field_counts') or {}}",
        f"- Reapproval blockers/vector integrity failures: {reapproval_workload.get('pre_reapproval_blocker_count')} / {reapproval_workload.get('source_vector_integrity_failure_count')}",
        f"- Reapproval review batches: {reapproval_review_batch.get('batch_count')} batches / {reapproval_review_batch.get('reapproval_chunk_count')} chunks / selected {reapproval_review_batch.get('selected_candidate_count')} of {reapproval_review_batch.get('candidate_count')} / blockers {reapproval_review_batch.get('blocker_count')}",
        f"- RAG answerable ratio: {product.get('rag_answerable_ratio')}",
        f"- Demo answers: `{str(demo.get('passed')).lower()}` / queries {demo.get('query_count')} / quality issues {demo.get('quality_issue_count')}",
        f"- Demo expected-term min/avg hit ratio: {demo.get('expected_term_min_hit_ratio')} / {demo.get('expected_term_average_hit_ratio')}",
        f"- Demo expected-article-no queries/min hit ratio: {demo.get('expected_article_no_query_count')} / {demo.get('expected_article_no_min_hit_ratio')}",
        f"- Demo expected-article-title queries/min hit ratio: {demo.get('expected_article_title_query_count')} / {demo.get('expected_article_title_min_hit_ratio')}",
        f"- MCP doctor: `{str(readiness.get('passed')).lower()}` / high {readiness.get('high_count')} / medium {readiness.get('medium_count')}",
        f"- MCP-visible records: {visibility.get('total_mcp_visible_records')} / approved chunks {visibility.get('total_approved_chunks')} / smoke docs {visibility.get('smoke_like_document_count')}",
        f"- Parser evidence in MCP-visible index: HWPX docs {parser_evidence.get('hwpx_evidence_document_count', 0)} / HWP mode docs {parser_evidence.get('hwp_extraction_mode_document_count', 0)} / HWP geometry-review docs {parser_evidence.get('hwp_native_table_geometry_review_document_count', 0)}",
        f"- Parser uncertainty in MCP-visible index: risks {parser_uncertainty.get('risk_level_counts') or {}} / flags {parser_uncertainty.get('flag_counts') or {}}",
        f"- Approval provenance coverage: complete {approval_provenance.get('complete_record_count', 0)} of {approval_provenance.get('record_count', 0)} / missing {approval_provenance.get('missing_field_counts') or {}}",
        f"- Approval journal coverage: matched {approval_journal.get('matched_record_count', 0)} of {approval_journal.get('eligible_record_count', approval_journal.get('record_count', 0))} / missing {approval_journal.get('missing_record_count', 0)} / journal records {approval_journal.get('journal_record_count', 0)}",
        f"- Approval journal review events: incomplete records {approval_review_events.get('incomplete_record_count', 0)} / missing chunks {approval_review_events.get('missing_event_chunk_counts') or {}}",
        f"- MCP query benchmark: `{str(benchmark.get('passed')).lower()}` / total p95 {benchmark.get('total_p95_ms')} ms / warm search p95 {benchmark.get('warm_search_p95_ms')} ms / warm records {benchmark.get('warm_record_count')} of min {benchmark.get('min_warm_records')}",
        f"- Claude Desktop config valid: `{str(bundle.get('claude_desktop_config_valid')).lower()}`",
        f"- Authority manifest: `{str(authority.get('passed')).lower()}` / authoritative {authority.get('authoritative_artifact_count')} / supersedes {authority.get('supersedes_count')}",
        "",
        "## Claude Desktop Steps",
        "",
    ]
    for step in report.get("operator_steps") or []:
        lines.append(f"### {step.get('name')}")
        if step.get("command"):
            lines.extend(["", "```powershell", str(step["command"]), "```"])
        if step.get("source_file"):
            lines.append(f"- Source: `{step.get('source_file')}`")
        if step.get("target_file"):
            lines.append(f"- Target: `{step.get('target_file')}`")
        if step.get("note"):
            lines.append(f"- Note: {step.get('note')}")
        queries = step.get("queries") if isinstance(step.get("queries"), list) else []
        for query in queries:
            lines.append(f"- `{query}`")
        lines.append("")
    findings = report.get("findings") or []
    lines.extend(["## Findings", ""])
    if findings:
        for item in findings:
            lines.append(f"- {item.get('severity')} `{item.get('code')}`: {item.get('detail')}")
    else:
        lines.append("- None.")
    lines.append("")
    lines.extend(["## Source Reports", ""])
    for key, value in (report.get("source_reports") or {}).items():
        lines.append(f"- {key}: `{value}`")
    source_artifacts = report.get("source_report_artifacts") or []
    if source_artifacts:
        lines.extend(["", "## Source Report Digests", ""])
        for artifact in source_artifacts:
            lines.append(
                f"- `{artifact.get('role')}`: `{artifact.get('path')}` "
                f"sha256=`{artifact.get('sha256') or '-'}` passed=`{str(artifact.get('passed')).lower()}`"
            )
    return "\n".join(lines).rstrip() + "\n"


def _finding(severity: str, code: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "detail": detail}


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON report must be an object: {path}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_artifact_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _int(count) for key, count in value.items()}


def _approval_review_event_coverage_status(report: dict[str, Any]) -> dict[str, Any]:
    expected_raw = report.get("expected_event_chunk_counts")
    observed_raw = report.get("event_chunk_counts")
    precomputed_raw = report.get("missing_event_chunk_counts")
    expected, expected_bad = _strict_int_dict(expected_raw)
    observed, observed_bad = _strict_int_dict(observed_raw)
    precomputed, precomputed_bad = _strict_int_dict(precomputed_raw)
    incomplete = _strict_int(report.get("incomplete_record_count"))
    malformed_fields: list[str] = []
    if not isinstance(expected_raw, dict):
        malformed_fields.append("expected_event_chunk_counts")
    malformed_fields.extend(f"expected_event_chunk_counts.{key}" for key in expected_bad)
    if not isinstance(observed_raw, dict):
        malformed_fields.append("event_chunk_counts")
    malformed_fields.extend(f"event_chunk_counts.{key}" for key in observed_bad)
    if not isinstance(precomputed_raw, dict):
        malformed_fields.append("missing_event_chunk_counts")
    malformed_fields.extend(f"missing_event_chunk_counts.{key}" for key in precomputed_bad)
    if incomplete is None:
        malformed_fields.append("incomplete_record_count")
    missing_types = [
        event_type
        for event_type in REQUIRED_APPROVAL_REVIEW_EVENT_TYPES
        if event_type not in expected or event_type not in observed or event_type not in precomputed
    ]
    computed_missing = {
        event_type: max(0, expected.get(event_type, 0) - observed.get(event_type, 0))
        for event_type in REQUIRED_APPROVAL_REVIEW_EVENT_TYPES
    }
    mismatched_precomputed = [
        event_type
        for event_type, count in computed_missing.items()
        if event_type in precomputed and precomputed[event_type] != count
    ]
    malformed_fields.extend(
        f"missing_event_chunk_counts.{event_type}.mismatch"
        for event_type in mismatched_precomputed
    )
    return {
        "incomplete_record_count": incomplete if incomplete is not None else 0,
        "computed_missing_event_chunk_counts": computed_missing,
        "missing_required_event_types": missing_types,
        "malformed_count_fields": sorted(set(malformed_fields)),
    }


def _strict_int_dict(value: Any) -> tuple[dict[str, int], list[str]]:
    if not isinstance(value, dict):
        return {}, []
    parsed: dict[str, int] = {}
    malformed: list[str] = []
    for key, raw_count in value.items():
        count = _strict_int(raw_count)
        if count is None:
            malformed.append(str(key))
        else:
            parsed[str(key)] = count
    return parsed, malformed


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        count = int(str(value))
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    if isinstance(value, bool) or value in (None, ""):
        return 0.0
    try:
        return round(float(str(value)), 3)
    except (TypeError, ValueError):
        return 0.0


def _float4(value: Any) -> float:
    if isinstance(value, bool) or value in (None, ""):
        return 0.0
    try:
        return round(float(str(value)), 4)
    except (TypeError, ValueError):
        return 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local MCP handoff report from readiness, demo, and connection artifacts.")
    parser.add_argument("--product-readiness-report", required=True)
    parser.add_argument("--mcp-demo-answer-report", required=True)
    parser.add_argument("--mcp-readiness-report", default=None)
    parser.add_argument("--mcp-index-visibility-report", default=None)
    parser.add_argument("--mcp-query-benchmark-report", default=None)
    parser.add_argument("--authority-manifest", default=None)
    parser.add_argument("--bundle-dir", default=None)
    parser.add_argument("--server-name", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_mcp_handoff_report(
        product_readiness_report=Path(args.product_readiness_report),
        mcp_demo_answer_report=Path(args.mcp_demo_answer_report),
        mcp_readiness_report=Path(args.mcp_readiness_report) if args.mcp_readiness_report else None,
        mcp_index_visibility_report=Path(args.mcp_index_visibility_report) if args.mcp_index_visibility_report else None,
        mcp_query_benchmark_report=Path(args.mcp_query_benchmark_report) if args.mcp_query_benchmark_report else None,
        authority_manifest=Path(args.authority_manifest) if args.authority_manifest else None,
        bundle_dir=Path(args.bundle_dir) if args.bundle_dir else None,
        server_name=args.server_name,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report["handoff_ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
