from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import (
    MCP_PERFORMANCE_SOURCE_SCOPE,
    capture_mcp_performance_source_state,
    current_repo_commit,
    finalize_mcp_performance_source_state,
)

HASH_CHUNK_BYTES = 1024 * 1024
REQUIRED_FIRST_QUERY_THRESHOLD_NAMES = (
    "effective_min_success_count",
    "min_result_count",
    "max_cold_p95_ms",
)
REQUIRED_RETRIEVAL_QUALITY_THRESHOLD_NAMES = (
    "min_recall_at_5",
    "min_mrr",
    "min_document_recall_at_5",
    "max_no_evidence_false_positive_rate",
    "min_no_evidence_abstention_rate",
)
REQUIRED_CONCURRENT_QUERY_POLICY_NAMES = (
    "min_concurrency",
    "min_task_count",
    "max_task_total_ms",
    "max_batch_elapsed_ms",
)
PUBLIC_EVIDENCE_REDACTED_KEYS = frozenset(
    {
        "actor",
        "chunk_id",
        "chunk_ids",
        "data_dir",
        "department_ids",
        "document_id",
        "document_ids",
        "error",
        "id",
        "ids",
        "input_sha256",
        "measurements",
        "path",
        "profile_id",
        "query",
        "query_id",
        "query_set_sha256",
        "query_sha256",
        "query_spec_sha256",
        "query_spec_path",
        "regulation_id",
        "repo_commit",
        "result_id",
        "results",
        "sha256",
        "source_sha256",
        "source_path",
        "source_reports",
        "target_chunk_id",
        "target_chunk_ids",
        "target_document_id",
        "target_document_ids",
        "tenant_id",
        "text",
        "title",
        "trace_id",
        "url",
    }
)
QUERY_BENCHMARK_ADVISORY_FINDING_CODES = frozenset(
    {
        "benchmark-query-spec-target-missing-from-runtime",
    }
)
RETRIEVAL_QUALITY_ADVISORY_FINDING_CODES = frozenset(
    {
        "query-spec-target-missing-from-runtime",
    }
)
UNKNOWN_REPO_COMMIT_VALUES = frozenset(
    {
        "n/a",
        "na",
        "none",
        "not_available",
        "null",
        "unavailable",
        "unknown",
    }
)


def build_mcp_performance_load_evidence(
    *,
    query_benchmark_report: Path,
    transport_smoke_report: Path,
    index_visibility_report: Path,
    approved_vectors_jsonl: Path,
    bm25_index_json: Path,
    first_query_benchmark_report: Path | None = None,
    retrieval_quality_report: Path | None = None,
    concurrent_query_benchmark_report: Path | None = None,
    min_warm_records: int | None = None,
    max_total_p95_ms: float | None = None,
    max_warm_search_p95_ms: float | None = None,
    max_transport_warm_search_ms: float | None = None,
    require_latency_slo: bool = False,
    require_repo_commit_consistency: bool = False,
    require_first_query_benchmark: bool = False,
    require_retrieval_quality: bool = False,
    require_concurrent_query_benchmark: bool = False,
    expected_first_query_retrieval_strategy: str | None = None,
    min_concurrent_query_concurrency: int | None = None,
    min_concurrent_query_task_count: int | None = None,
    max_concurrent_query_task_total_ms: float | None = None,
    max_concurrent_query_batch_elapsed_ms: float | None = None,
    require_indexed_visibility: bool = False,
    require_visibility_match: bool = True,
    require_no_smoke_docs: bool = True,
    out_json: Path | None = None,
    out_md: Path | None = None,
    out_public_json: Path | None = None,
    out_public_md: Path | None = None,
) -> dict[str, Any]:
    started_source_state = capture_mcp_performance_source_state(PROJECT_ROOT)
    _validate_latency_thresholds(
        {
            "max_total_p95_ms": max_total_p95_ms,
            "max_warm_search_p95_ms": max_warm_search_p95_ms,
            "max_transport_warm_search_ms": max_transport_warm_search_ms,
        }
    )
    concurrent_policy = {
        "min_concurrency": min_concurrent_query_concurrency,
        "min_task_count": min_concurrent_query_task_count,
        "max_task_total_ms": max_concurrent_query_task_total_ms,
        "max_batch_elapsed_ms": max_concurrent_query_batch_elapsed_ms,
    }
    _validate_concurrent_query_policy(concurrent_policy)
    if expected_first_query_retrieval_strategy not in {
        None,
        "flat_rag",
        "catalog_toc_body",
    }:
        raise ValueError(
            "expected_first_query_retrieval_strategy must be flat_rag or "
            "catalog_toc_body."
        )
    benchmark = _load_json_artifact(query_benchmark_report, role="query_benchmark")
    transport = _load_json_artifact(transport_smoke_report, role="transport_smoke")
    visibility = _load_json_artifact(index_visibility_report, role="index_visibility")
    vector_file = _jsonl_file_summary(approved_vectors_jsonl, role="approved_vectors")
    bm25_file = _bm25_file_summary(bm25_index_json, role="bm25_index")
    first_query = (
        _load_json_artifact(first_query_benchmark_report, role="first-query-benchmark")
        if first_query_benchmark_report is not None
        else None
    )
    retrieval_quality = (
        _load_json_artifact(retrieval_quality_report, role="retrieval-quality")
        if retrieval_quality_report is not None
        else None
    )
    concurrent_query = (
        _load_json_artifact(
            concurrent_query_benchmark_report,
            role="concurrent-query-benchmark",
        )
        if concurrent_query_benchmark_report is not None
        else None
    )
    selected_report_payloads = {
        "query_benchmark": benchmark["payload"],
        "transport_smoke": transport["payload"],
        "index_visibility": visibility["payload"],
    }
    if first_query is not None:
        selected_report_payloads["first_query_benchmark"] = first_query["payload"]
    if retrieval_quality is not None:
        selected_report_payloads["retrieval_quality"] = retrieval_quality["payload"]
    if concurrent_query is not None:
        selected_report_payloads["concurrent_query_benchmark"] = concurrent_query[
            "payload"
        ]
    repo_commit_consistency, repo_commit_findings = (
        _source_repo_commit_consistency(
            selected_report_payloads,
            required=require_repo_commit_consistency,
        )
    )
    first_query_source_findings = (
        list(first_query["findings"]) if first_query is not None else []
    )
    if first_query is not None and not first_query["findings"]:
        first_query_source_findings.extend(
            _first_query_benchmark_findings(first_query["payload"])
        )
    retrieval_quality_source_findings = (
        list(retrieval_quality["findings"])
        if retrieval_quality is not None
        else []
    )
    if retrieval_quality is not None and not retrieval_quality["findings"]:
        retrieval_quality_source_findings.extend(
            _retrieval_quality_findings(retrieval_quality["payload"])
        )
    concurrent_query_source_findings = (
        list(concurrent_query["findings"])
        if concurrent_query is not None
        else []
    )
    if concurrent_query is not None and not concurrent_query["findings"]:
        concurrent_query_source_findings.extend(
            _concurrent_query_benchmark_findings(concurrent_query["payload"])
        )
        concurrent_query_source_findings.extend(
            _concurrent_query_external_policy_findings(
                concurrent_query["payload"],
                policy=concurrent_policy,
                min_warm_records=min_warm_records,
            )
        )
    first_query_gate = _first_query_release_gate(
        first_query,
        first_query_source_findings,
    )
    retrieval_quality_gate = _retrieval_quality_release_gate(
        retrieval_quality,
        retrieval_quality_source_findings,
    )
    concurrent_query_gate = _concurrent_query_release_gate(
        concurrent_query,
        concurrent_query_source_findings,
        policy=concurrent_policy,
    )

    findings: list[dict[str, Any]] = [
        *benchmark["findings"],
        *transport["findings"],
        *visibility["findings"],
        *vector_file["findings"],
        *bm25_file["findings"],
        *first_query_source_findings,
        *retrieval_quality_source_findings,
        *concurrent_query_source_findings,
        *repo_commit_findings,
    ]
    if require_first_query_benchmark and not first_query_gate["passed"]:
        findings.append(
            _finding(
                "blocker",
                "first-query-benchmark-release-gate-missing",
                "Release evidence requires a valid first-query benchmark with configured thresholds.",
                present=first_query_gate["present"],
                missing_thresholds=first_query_gate["missing_thresholds"],
            )
        )
    if require_retrieval_quality and not retrieval_quality_gate["passed"]:
        findings.append(
            _finding(
                "blocker",
                "retrieval-quality-release-gate-missing",
                "Release evidence requires valid retrieval-quality evidence with all core thresholds.",
                present=retrieval_quality_gate["present"],
                missing_thresholds=retrieval_quality_gate["missing_thresholds"],
            )
        )
    if (
        require_concurrent_query_benchmark
        and not concurrent_query_gate["passed"]
    ):
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-benchmark-release-gate-missing",
                "Release evidence requires a valid concurrent-query benchmark and all external policy values.",
                present=concurrent_query_gate["present"],
                missing_policy_values=concurrent_query_gate[
                    "missing_policy_values"
                ],
            )
        )
    reported_first_query_strategy = (
        _dict(
            _dict(first_query.get("payload")).get("thresholds")
        ).get("required_retrieval_strategy")
        if first_query is not None
        else None
    )
    if (
        expected_first_query_retrieval_strategy is not None
        and reported_first_query_strategy
        != expected_first_query_retrieval_strategy
    ):
        findings.append(
            _finding(
                "blocker",
                "first-query-retrieval-strategy-policy-mismatch",
                "First-query evidence does not enforce the externally required retrieval strategy.",
                expected_strategy=expected_first_query_retrieval_strategy,
                reported_strategy=reported_first_query_strategy,
            )
        )

    benchmark_summary = _benchmark_summary(benchmark["payload"])
    transport_summary = _transport_summary(transport["payload"])
    visibility_summary = _visibility_summary(visibility["payload"])
    file_summary = {
        "approved_vectors": vector_file["summary"],
        "bm25_index": bm25_file["summary"],
    }
    first_query_summary = (
        _first_query_benchmark_summary(
            first_query["payload"],
            first_query["summary"],
            load_succeeded=not first_query["findings"],
            source_findings=first_query_source_findings,
        )
        if first_query is not None
        else None
    )
    retrieval_quality_summary = (
        _retrieval_quality_summary(
            retrieval_quality["payload"],
            retrieval_quality["summary"],
            load_succeeded=not retrieval_quality["findings"],
            source_findings=retrieval_quality_source_findings,
        )
        if retrieval_quality is not None
        else None
    )
    concurrent_query_summary = (
        _concurrent_query_benchmark_summary(
            concurrent_query["payload"],
            concurrent_query["summary"],
            load_succeeded=not concurrent_query["findings"],
            source_findings=concurrent_query_source_findings,
        )
        if concurrent_query is not None
        else None
    )

    findings.extend(
        _benchmark_findings(
            benchmark["payload"],
            summary=benchmark_summary,
            min_warm_records=min_warm_records,
            max_total_p95_ms=max_total_p95_ms,
            max_warm_search_p95_ms=max_warm_search_p95_ms,
        )
    )
    findings.extend(
        _transport_findings(
            transport["payload"],
            summary=transport_summary,
            max_transport_warm_search_ms=max_transport_warm_search_ms,
        )
    )
    findings.extend(
        _visibility_findings(
            visibility["payload"],
            summary=visibility_summary,
            require_indexed_visibility=require_indexed_visibility,
            require_visibility_match=require_visibility_match,
            require_no_smoke_docs=require_no_smoke_docs,
        )
    )
    findings.extend(
        _record_count_findings(
            benchmark_summary=benchmark_summary,
            visibility_summary=visibility_summary,
            vector_summary=vector_file["summary"],
            bm25_summary=bm25_file["summary"],
        )
    )

    latency_thresholds = {
        "max_total_p95_ms": max_total_p95_ms,
        "max_warm_search_p95_ms": max_warm_search_p95_ms,
        "max_transport_warm_search_ms": max_transport_warm_search_ms,
    }
    missing_latency_thresholds = [
        name for name, value in latency_thresholds.items() if value is None
    ]
    latency_slo_evaluated = not missing_latency_thresholds
    if require_latency_slo and missing_latency_thresholds:
        findings.append(
            _finding(
                "blocker",
                "latency-slo-thresholds-missing",
                "Release performance evidence requires all latency SLO thresholds.",
                missing_thresholds=missing_latency_thresholds,
            )
        )

    source_state = finalize_mcp_performance_source_state(
        started_source_state,
        PROJECT_ROOT,
    )
    source_state_consistency, source_state_findings = (
        _source_state_consistency(
            selected_report_payloads,
            builder_source_state=source_state,
            required=require_repo_commit_consistency,
        )
    )
    findings.extend(source_state_findings)

    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    evidence_ready = blocker_count == 0 and warning_count == 0
    latency_failure_codes = {
        "query-benchmark-total-p95-missing",
        "query-benchmark-total-p95-too-high",
        "query-benchmark-warm-search-p95-missing",
        "query-benchmark-warm-search-p95-too-high",
        "transport-warm-search-missing",
        "transport-warm-search-too-high",
    }
    latency_slo_passed = latency_slo_evaluated and not any(
        item.get("code") in latency_failure_codes for item in findings
    )
    source_reports = {
        "query_benchmark_report": str(query_benchmark_report),
        "transport_smoke_report": str(transport_smoke_report),
        "index_visibility_report": str(index_visibility_report),
        "approved_vectors_jsonl": str(approved_vectors_jsonl),
        "bm25_index_json": str(bm25_index_json),
    }
    if first_query_benchmark_report is not None:
        source_reports["first_query_benchmark_report"] = str(first_query_benchmark_report)
    if retrieval_quality_report is not None:
        source_reports["retrieval_quality_report"] = str(retrieval_quality_report)
    if concurrent_query_benchmark_report is not None:
        source_reports["concurrent_query_benchmark_report"] = str(
            concurrent_query_benchmark_report
        )

    report = {
        "report_type": "mcp_performance_load_evidence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_state": source_state,
        "passed": blocker_count == 0,
        "evidence_ready": evidence_ready,
        "performance_release_ready": (
            evidence_ready
            and latency_slo_passed
            and first_query_gate["passed"]
            and retrieval_quality_gate["passed"]
            and concurrent_query_gate["passed"]
            and repo_commit_consistency["fully_verified"]
            and source_state_consistency["fully_verified"]
        ),
        "repo_commit_consistency": repo_commit_consistency,
        "source_state_consistency": source_state_consistency,
        "first_query_release_gate": {
            **first_query_gate,
            "required": require_first_query_benchmark,
        },
        "retrieval_quality_release_gate": {
            **retrieval_quality_gate,
            "required": require_retrieval_quality,
        },
        "concurrent_query_release_gate": {
            **concurrent_query_gate,
            "required": require_concurrent_query_benchmark,
        },
        "latency_slo": {
            "required": require_latency_slo,
            "evaluated": latency_slo_evaluated,
            "passed": latency_slo_passed,
            "missing_thresholds": missing_latency_thresholds,
            "claim_scope": (
                "functional_and_latency_slo"
                if latency_slo_evaluated
                else "functional_evidence_only_no_latency_slo"
            ),
        },
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "finding_count": len(findings),
        "findings": findings,
        "thresholds": {
            "min_warm_records": min_warm_records,
            "max_total_p95_ms": max_total_p95_ms,
            "max_warm_search_p95_ms": max_warm_search_p95_ms,
            "max_transport_warm_search_ms": max_transport_warm_search_ms,
            "require_indexed_visibility": require_indexed_visibility,
            "require_repo_commit_consistency": require_repo_commit_consistency,
            "require_visibility_match": require_visibility_match,
            "require_no_smoke_docs": require_no_smoke_docs,
            "require_first_query_benchmark": require_first_query_benchmark,
            "require_retrieval_quality": require_retrieval_quality,
            "require_concurrent_query_benchmark": (
                require_concurrent_query_benchmark
            ),
            "expected_first_query_retrieval_strategy": (
                expected_first_query_retrieval_strategy
            ),
            "min_concurrent_query_concurrency": (
                min_concurrent_query_concurrency
            ),
            "min_concurrent_query_task_count": (
                min_concurrent_query_task_count
            ),
            "max_concurrent_query_task_total_ms": (
                max_concurrent_query_task_total_ms
            ),
            "max_concurrent_query_batch_elapsed_ms": (
                max_concurrent_query_batch_elapsed_ms
            ),
        },
        "source_reports": source_reports,
        "query_benchmark_summary": benchmark_summary,
        "transport_smoke_summary": transport_summary,
        "index_visibility_summary": visibility_summary,
        "file_summary": file_summary,
    }
    if first_query_summary is not None:
        report["first_query_benchmark_summary"] = first_query_summary
    if retrieval_quality_summary is not None:
        report["retrieval_quality_summary"] = retrieval_quality_summary
    if concurrent_query_summary is not None:
        report["concurrent_query_benchmark_summary"] = concurrent_query_summary
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    if out_public_json or out_public_md:
        public_report = public_mcp_performance_load_evidence(report)
        if out_public_json:
            out_public_json.parent.mkdir(parents=True, exist_ok=True)
            out_public_json.write_text(
                json.dumps(public_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if out_public_md:
            out_public_md.parent.mkdir(parents=True, exist_ok=True)
            out_public_md.write_text(_to_markdown(public_report), encoding="utf-8")
    return report


def public_mcp_performance_load_evidence(
    report: dict[str, Any],
) -> dict[str, Any]:
    """Return a shareable derivative without local or institution identifiers."""

    public_report = _redact_public_evidence_value(report)
    public_report["report_scope"] = "public_summary"
    source_state = _dict(report.get("source_state"))
    public_report["source_state"] = {
        "scope": str(source_state.get("scope") or ""),
        "status": str(source_state.get("status") or ""),
        "file_count": source_state.get("file_count"),
        "byte_count": source_state.get("byte_count"),
        "stable": source_state.get("stable") is True,
    }
    source_consistency = _dict(report.get("source_state_consistency"))
    public_report["source_state_consistency"] = {
        "scope": str(source_consistency.get("scope") or ""),
        "required": source_consistency.get("required") is True,
        "status": str(source_consistency.get("status") or ""),
        "passed": source_consistency.get("passed") is True,
        "consistent": source_consistency.get("consistent") is True,
        "fully_verified": source_consistency.get("fully_verified") is True,
        "selected_report_count": _int(
            source_consistency.get("selected_report_count")
        ),
        "verified_report_count": _int(
            source_consistency.get("verified_report_count")
        ),
        "legacy_missing_report_count": len(
            source_consistency.get("legacy_missing_report_roles") or []
        ),
        "unavailable_report_count": len(
            source_consistency.get("unavailable_report_roles") or []
        ),
        "invalid_report_count": len(
            source_consistency.get("invalid_report_roles") or []
        ),
        "digest_group_count": _int(
            source_consistency.get("digest_group_count")
        ),
    }
    concurrent_summary = _dict(
        report.get("concurrent_query_benchmark_summary")
    )
    if concurrent_summary:
        public_report["concurrent_query_benchmark_summary"] = {
            name: concurrent_summary.get(name)
            for name in (
                "source_load_status",
                "source_validation_status",
                "report_type",
                "schema_version",
                "schema_contract",
                "passed",
                "finding_count",
                "query_count",
                "rounds",
                "concurrency",
                "task_count",
                "measurement_count",
                "successful_count",
                "error_count",
                "answerable_measurement_count",
                "no_evidence_measurement_count",
                "answerable_zero_result_count",
                "no_evidence_nonzero_result_count",
                "warm_record_count",
                "batch_elapsed_ms",
                "task_total_p95_ms",
                "task_total_max_ms",
                "recomputed_task_total_max_ms",
                "api_call_count",
            )
        }
    public_report["findings"] = [
        {
            "severity": str(item.get("severity") or ""),
            "code": str(item.get("code") or ""),
            "detail": "See the private evidence report for local diagnostic details.",
        }
        for item in report.get("findings", [])
        if isinstance(item, dict)
    ]
    public_report["finding_count"] = len(public_report["findings"])
    return public_report


def _redact_public_evidence_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_public_evidence_value(item)
            for key, item in value.items()
            if str(key) not in PUBLIC_EVIDENCE_REDACTED_KEYS
        }
    if isinstance(value, list):
        return [_redact_public_evidence_value(item) for item in value]
    return value


def _load_json_artifact(path: Path, *, role: str) -> dict[str, Any]:
    summary = {
        "role": role,
        "path": str(path),
        "exists": path.is_file(),
        "byte_count": path.stat().st_size if path.is_file() else 0,
        "sha256": _sha256(path) if path.is_file() else "",
    }
    if not path.is_file():
        return {
            "summary": summary,
            "payload": {},
            "findings": [_finding("blocker", f"{role}-missing", f"{path} is missing.")],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "summary": summary,
            "payload": {},
            "findings": [_finding("blocker", f"{role}-parse-error", str(exc))],
        }
    if not isinstance(payload, dict):
        return {
            "summary": summary,
            "payload": {},
            "findings": [
                _finding(
                    "blocker",
                    f"{role}-root-invalid",
                    "JSON report root must be an object.",
                )
            ],
        }
    summary["report_type"] = str(payload.get("report_type") or "")
    return {"summary": summary, "payload": payload, "findings": []}


def _source_repo_commit_consistency(
    report_payloads: dict[str, dict[str, Any]],
    *,
    required: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    commit_roles: dict[str, list[str]] = {}
    unavailable_roles: list[str] = []
    invalid_roles: list[str] = []
    for role, payload in report_payloads.items():
        state, normalized_commit = _normalized_source_repo_commit(
            payload.get("repo_commit")
        )
        if state == "verified":
            assert normalized_commit is not None
            commit_roles.setdefault(normalized_commit, []).append(role)
        elif state == "unavailable":
            unavailable_roles.append(role)
        else:
            invalid_roles.append(role)

    commit_groups = [
        {
            "repo_commit": repo_commit,
            "report_roles": sorted(roles),
        }
        for repo_commit, roles in sorted(commit_roles.items())
    ]
    selected_report_count = len(report_payloads)
    verified_report_count = sum(len(roles) for roles in commit_roles.values())
    consistent = len(commit_groups) <= 1
    fully_verified = bool(
        selected_report_count
        and verified_report_count == selected_report_count
        and consistent
    )
    passed = bool(
        consistent
        and not invalid_roles
        and (fully_verified or not required)
    )
    if invalid_roles:
        status = "invalid"
    elif not consistent:
        status = "mismatch"
    elif fully_verified:
        status = "verified"
    elif required:
        status = "unverifiable_required"
    else:
        status = "compatible_unverified"

    findings: list[dict[str, Any]] = []
    if not consistent:
        findings.append(
            _finding(
                "blocker",
                "source-report-repo-commit-mismatch",
                "Selected source reports were not generated from one repository commit.",
                commit_groups=commit_groups,
            )
        )
    if invalid_roles:
        findings.append(
            _finding(
                "blocker",
                "source-report-repo-commit-invalid",
                "Selected source reports contain malformed repo_commit values.",
                report_roles=sorted(invalid_roles),
            )
        )
    if required and unavailable_roles:
        findings.append(
            _finding(
                "blocker",
                "source-report-repo-commit-unverifiable",
                "Strict release evidence requires repo_commit on every selected source report.",
                report_roles=sorted(unavailable_roles),
            )
        )

    return (
        {
            "required": required,
            "status": status,
            "passed": passed,
            "consistent": consistent,
            "fully_verified": fully_verified,
            "selected_report_count": selected_report_count,
            "verified_report_count": verified_report_count,
            "unavailable_report_roles": sorted(unavailable_roles),
            "invalid_report_roles": sorted(invalid_roles),
            "commit_group_count": len(commit_groups),
            "commit_groups": commit_groups,
        },
        findings,
    )


def _normalized_source_repo_commit(value: Any) -> tuple[str, str | None]:
    if value is None:
        return "unavailable", None
    if not isinstance(value, str):
        return "invalid", None
    normalized = value.strip().lower()
    if not normalized or normalized in UNKNOWN_REPO_COMMIT_VALUES:
        return "unavailable", None
    if len(normalized) != 40 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return "invalid", None
    return "verified", normalized


def _source_state_consistency(
    report_payloads: dict[str, dict[str, Any]],
    *,
    builder_source_state: dict[str, Any],
    required: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selected_states: dict[str, Any] = {
        role: payload.get("source_state")
        for role, payload in report_payloads.items()
    }
    selected_states["evidence_builder"] = builder_source_state
    digest_roles: dict[str, list[str]] = {}
    legacy_missing_roles: list[str] = []
    invalid_roles: list[str] = []
    unavailable_states: list[dict[str, str]] = []
    for role, value in selected_states.items():
        state, digest, reported_status = _normalized_report_source_state(value)
        if state == "verified":
            assert digest is not None
            digest_roles.setdefault(digest, []).append(role)
        elif state == "legacy_missing":
            legacy_missing_roles.append(role)
        elif state == "unavailable":
            unavailable_states.append(
                {
                    "report_role": role,
                    "status": reported_status,
                }
            )
        else:
            invalid_roles.append(role)

    digest_groups = [
        {
            "sha256": digest,
            "report_roles": sorted(roles),
        }
        for digest, roles in sorted(digest_roles.items())
    ]
    selected_report_count = len(selected_states)
    verified_report_count = sum(len(roles) for roles in digest_roles.values())
    consistent = len(digest_groups) <= 1
    fully_verified = bool(
        selected_report_count
        and verified_report_count == selected_report_count
        and consistent
    )
    unavailable_roles = sorted(
        item["report_role"] for item in unavailable_states
    )
    passed = bool(
        consistent
        and not invalid_roles
        and not unavailable_roles
        and (fully_verified or not required)
    )
    if invalid_roles:
        status = "invalid"
    elif unavailable_roles:
        status = "source_unavailable"
    elif not consistent:
        status = "mismatch"
    elif fully_verified:
        status = "verified"
    elif required:
        status = "unverifiable_required"
    else:
        status = "compatible_unverified"

    findings: list[dict[str, Any]] = []
    if not consistent:
        findings.append(
            _finding(
                "blocker",
                "source-report-source-state-mismatch",
                "Selected source reports and the evidence builder do not share one source-state digest.",
                digest_groups=digest_groups,
            )
        )
    if invalid_roles:
        findings.append(
            _finding(
                "blocker",
                "source-report-source-state-invalid",
                "Selected source reports contain malformed source_state metadata.",
                report_roles=sorted(invalid_roles),
            )
        )
    if unavailable_states:
        findings.append(
            _finding(
                "blocker",
                "source-report-source-state-unavailable",
                "A selected source report or the evidence builder could not verify a stable source state.",
                report_states=sorted(
                    unavailable_states,
                    key=lambda item: item["report_role"],
                ),
            )
        )
    if required and legacy_missing_roles:
        findings.append(
            _finding(
                "blocker",
                "source-report-source-state-unverifiable",
                "Strict release evidence requires source_state on every selected source report.",
                report_roles=sorted(legacy_missing_roles),
            )
        )

    return (
        {
            "scope": MCP_PERFORMANCE_SOURCE_SCOPE,
            "required": required,
            "status": status,
            "passed": passed,
            "consistent": consistent,
            "fully_verified": fully_verified,
            "selected_report_count": selected_report_count,
            "verified_report_count": verified_report_count,
            "legacy_missing_report_roles": sorted(legacy_missing_roles),
            "unavailable_report_roles": unavailable_roles,
            "invalid_report_roles": sorted(invalid_roles),
            "digest_group_count": len(digest_groups),
            "digest_groups": digest_groups,
        },
        findings,
    )


def _normalized_report_source_state(
    value: Any,
) -> tuple[str, str | None, str]:
    if value is None:
        return "legacy_missing", None, "missing"
    if not isinstance(value, dict):
        return "invalid", None, "invalid"
    scope = value.get("scope")
    status = value.get("status")
    digest = value.get("sha256")
    stable = value.get("stable")
    file_count = value.get("file_count")
    byte_count = value.get("byte_count")
    if scope != MCP_PERFORMANCE_SOURCE_SCOPE or not isinstance(status, str):
        return "invalid", None, str(status or "invalid")
    if status in {"unavailable", "changed_during_run"}:
        if (
            digest is None
            and stable is False
            and file_count is None
            and byte_count is None
        ):
            return "unavailable", None, status
        return "invalid", None, status
    if status != "available" or stable is not True:
        return "invalid", None, status
    if not isinstance(digest, str) or not _is_sha256(digest):
        return "invalid", None, status
    if (
        _strict_nonnegative_int(file_count) is None
        or _strict_nonnegative_int(byte_count) is None
    ):
        return "invalid", None, status
    return "verified", str(digest).lower(), status


def _jsonl_file_summary(path: Path, *, role: str) -> dict[str, Any]:
    summary = {
        "role": role,
        "path": str(path),
        "exists": path.is_file(),
        "byte_count": path.stat().st_size if path.is_file() else 0,
        "sha256": "",
        "record_count": 0,
    }
    if not path.is_file():
        return {
            "summary": summary,
            "findings": [_finding("blocker", f"{role}-missing", f"{path} is missing.")],
        }
    digest = hashlib.sha256()
    record_count = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            if line.strip():
                record_count += 1
    summary["sha256"] = digest.hexdigest()
    summary["record_count"] = record_count
    return {"summary": summary, "findings": []}


def _bm25_file_summary(path: Path, *, role: str) -> dict[str, Any]:
    base = _load_json_artifact(path, role=role)
    summary = dict(base["summary"])
    payload = base["payload"]
    if payload:
        documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []
        document_frequencies = (
            payload.get("document_frequencies") if isinstance(payload.get("document_frequencies"), dict) else {}
        )
        summary.update(
            {
                "index_version": str(payload.get("index_version") or ""),
                "retrieval_model": str(payload.get("retrieval_model") or ""),
                "tokenizer": str(payload.get("tokenizer") or ""),
                "document_count": _int(payload.get("document_count")) or len(documents),
                "documents_array_count": len(documents),
                "document_frequency_count": len(document_frequencies),
            }
        )
    return {"summary": summary, "findings": base["findings"]}


def _benchmark_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    summary = _dict(report.get("summary"))
    total = _dict(summary.get("total_elapsed_ms"))
    warm_total = _dict(summary.get("warm_total_elapsed_ms"))
    warm_search = _dict(summary.get("warm_search_elapsed_ms"))
    warmup = _dict(report.get("warmup"))
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "finding_count": _int(report.get("finding_count")),
        "query_count": _int(report.get("query_count")),
        "iterations": _int(report.get("iterations")),
        "measurement_count": _int(summary.get("measurement_count")),
        "warm_record_count": _int(warmup.get("record_count")),
        "bm25_index_ready": bool(warmup.get("bm25_index_ready")),
        "reported_min_warm_records": _int(report.get("min_warm_records")),
        "total_p50_ms": _optional_float(total.get("p50")),
        "total_p95_ms": _optional_float(total.get("p95")),
        "total_max_ms": _optional_float(total.get("max")),
        "warm_total_p50_ms": _optional_float(warm_total.get("p50")),
        "warm_total_p95_ms": _optional_float(warm_total.get("p95")),
        "warm_total_max_ms": _optional_float(warm_total.get("max")),
        "warm_search_p50_ms": _optional_float(warm_search.get("p50")),
        "warm_search_p95_ms": _optional_float(warm_search.get("p95")),
        "warm_search_max_ms": _optional_float(warm_search.get("max")),
        "api_call_count": _int(report.get("api_call_count")),
        "query_spec_sha256": str(report.get("query_spec_sha256") or ""),
    }


def _first_query_benchmark_summary(
    report: dict[str, Any],
    source: dict[str, Any],
    *,
    load_succeeded: bool,
    source_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _dict(report.get("summary"))
    process = _dict(summary.get("process_wall_elapsed_ms"))
    successful_process = _dict(summary.get("successful_process_wall_elapsed_ms"))
    cold = _dict(summary.get("cold"))
    warm = _dict(summary.get("warm"))
    cold_search = _dict(cold.get("search_elapsed_ms"))
    warm_search = _dict(warm.get("search_elapsed_ms"))
    return {
        **_source_report_identity(source),
        "source_load_status": "loaded" if load_succeeded else "failed",
        "source_validation_status": "accepted" if not source_findings else "blocked",
        "source_validation_finding_codes": [
            str(item.get("code") or "") for item in source_findings
        ],
        "report_type": str(report.get("report_type") or ""),
        "schema_version": report.get("schema_version"),
        "schema_contract": "mcp_first_query_benchmark_structural_v1",
        "passed": _optional_bool(report.get("passed")),
        "finding_count": _int(report.get("finding_count")),
        "finding_codes": _report_finding_codes(report),
        "query_count": _int(report.get("query_count")),
        "measurement_count": _int(summary.get("measurement_count")),
        "cold_process_wall_p95_ms": _optional_float(process.get("p95")),
        "cold_successful_process_wall_p95_ms": _optional_float(
            successful_process.get("p95")
        ),
        "cold_search_p95_ms": _optional_float(cold_search.get("p95")),
        "warm_search_p95_ms": _optional_float(warm_search.get("p95")),
        "cold_requested_count": _int(cold.get("requested_count")),
        "cold_attempt_count": _int(cold.get("attempt_count")),
        "cold_successful_count": _int(cold.get("successful_count")),
        "cold_failed_count": _int(cold.get("failed_count")),
        "warm_attempt_count": _int(warm.get("attempt_count")),
        "warm_successful_count": _int(warm.get("successful_count")),
        "warm_failed_count": _int(warm.get("failed_count")),
        "timed_out_count": _int(summary.get("timed_out_count")),
        "invalid_protocol_count": _int(summary.get("invalid_protocol_count")),
    }


def _retrieval_quality_summary(
    report: dict[str, Any],
    source: dict[str, Any],
    *,
    load_succeeded: bool,
    source_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _dict(report.get("summary"))
    return {
        **_source_report_identity(source),
        "source_load_status": "loaded" if load_succeeded else "failed",
        "source_validation_status": "accepted" if not source_findings else "blocked",
        "source_validation_finding_codes": [
            str(item.get("code") or "") for item in source_findings
        ],
        "report_type": str(report.get("report_type") or ""),
        "schema_version": report.get("schema_version"),
        "passed": _optional_bool(report.get("passed")),
        "finding_count": _int(report.get("finding_count")),
        "finding_codes": _report_finding_codes(report),
        "query_count": _int(report.get("query_count")),
        "valid_query_spec_count": _int(summary.get("valid_query_spec_count")),
        "invalid_query_spec_count": _int(summary.get("invalid_query_spec_count")),
        "answerable_query_count": _int(summary.get("answerable_query_count")),
        "document_target_query_count": _int(
            summary.get("document_target_query_count")
        ),
        "no_evidence_query_count": _int(summary.get("no_evidence_query_count")),
        "search_error_count": _int(summary.get("search_error_count")),
        "recall_at_1": _optional_float(summary.get("recall_at_1")),
        "recall_at_3": _optional_float(summary.get("recall_at_3")),
        "recall_at_5": _optional_float(summary.get("recall_at_5")),
        "mrr": _optional_float(summary.get("mrr")),
        "document_recall_at_1": _optional_float(
            summary.get("document_recall_at_1")
        ),
        "document_recall_at_3": _optional_float(
            summary.get("document_recall_at_3")
        ),
        "document_recall_at_5": _optional_float(
            summary.get("document_recall_at_5")
        ),
        "no_evidence_false_positive_count": _int(
            summary.get("no_evidence_false_positive_count")
        ),
        "no_evidence_false_positive_rate": _optional_float(
            summary.get("no_evidence_false_positive_rate")
        ),
        "no_evidence_abstention_count": _int(
            summary.get("no_evidence_abstention_count")
        ),
        "no_evidence_abstention_rate": _optional_float(
            summary.get("no_evidence_abstention_rate")
        ),
        "query_spec_sha256": str(report.get("query_spec_sha256") or ""),
    }


def _concurrent_query_benchmark_summary(
    report: dict[str, Any],
    source: dict[str, Any],
    *,
    load_succeeded: bool,
    source_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _dict(report.get("summary"))
    warmup = _dict(report.get("warmup"))
    total = _dict(summary.get("total_elapsed_ms"))
    measurements = (
        report.get("measurements")
        if isinstance(report.get("measurements"), list)
        else []
    )
    finite_totals = [
        float(item["total_elapsed_ms"])
        for item in measurements
        if isinstance(item, dict)
        and _is_nonnegative_number(item.get("total_elapsed_ms"))
    ]
    return {
        **_source_report_identity(source),
        "source_load_status": "loaded" if load_succeeded else "failed",
        "source_validation_status": (
            "accepted" if not source_findings else "blocked"
        ),
        "source_validation_finding_codes": [
            str(item.get("code") or "") for item in source_findings
        ],
        "report_type": str(report.get("report_type") or ""),
        "schema_version": report.get("schema_version"),
        "schema_contract": "mcp_concurrent_query_benchmark_structural_v1",
        "passed": _optional_bool(report.get("passed")),
        "finding_count": _int(report.get("finding_count")),
        "finding_codes": _report_finding_codes(report),
        "query_count": _int(report.get("query_count")),
        "rounds": _int(report.get("rounds")),
        "concurrency": _int(report.get("concurrency")),
        "task_count": _int(report.get("task_count")),
        "measurement_count": _int(summary.get("measurement_count")),
        "successful_count": _int(summary.get("successful_count")),
        "error_count": _int(summary.get("error_count")),
        "answerable_measurement_count": _int(
            summary.get("answerable_measurement_count")
        ),
        "no_evidence_measurement_count": _int(
            summary.get("no_evidence_measurement_count")
        ),
        "answerable_zero_result_count": _int(
            summary.get("answerable_zero_result_count")
        ),
        "no_evidence_nonzero_result_count": _int(
            summary.get("no_evidence_nonzero_result_count")
        ),
        "warm_record_count": _int(warmup.get("record_count")),
        "batch_elapsed_ms": _optional_float(summary.get("batch_elapsed_ms")),
        "task_total_p95_ms": _optional_float(total.get("p95")),
        "task_total_max_ms": _optional_float(total.get("max")),
        "recomputed_task_total_max_ms": (
            max(finite_totals) if finite_totals else None
        ),
        "api_call_count": _int(report.get("api_call_count")),
    }


def _first_query_release_gate(
    artifact: dict[str, Any] | None,
    source_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = artifact["payload"] if artifact is not None else {}
    thresholds = _dict(payload.get("thresholds"))
    missing_thresholds = [
        name
        for name in REQUIRED_FIRST_QUERY_THRESHOLD_NAMES
        if not _valid_first_query_release_threshold(
            name,
            thresholds.get(name),
        )
    ]
    return {
        "present": artifact is not None,
        "thresholds_configured": not missing_thresholds,
        "missing_thresholds": missing_thresholds,
        "passed": bool(
            artifact is not None
            and not source_findings
            and payload.get("passed") is True
            and not missing_thresholds
        ),
    }


def _valid_first_query_release_threshold(name: str, value: Any) -> bool:
    if name in {"effective_min_success_count", "min_result_count"}:
        normalized = _strict_nonnegative_int(value)
        return normalized is not None and normalized >= 1
    return _is_nonnegative_number(value)


def _retrieval_quality_release_gate(
    artifact: dict[str, Any] | None,
    source_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = artifact["payload"] if artifact is not None else {}
    thresholds = _dict(payload.get("thresholds"))
    missing_thresholds = [
        name
        for name in REQUIRED_RETRIEVAL_QUALITY_THRESHOLD_NAMES
        if not _is_unit_interval_number(thresholds.get(name))
    ]
    thresholds_configured = (
        payload.get("thresholds_configured") is True
        and not missing_thresholds
    )
    source_finding_codes = set(_report_finding_codes(payload))
    advisory_only_failure = bool(source_finding_codes) and source_finding_codes.issubset(
        RETRIEVAL_QUALITY_ADVISORY_FINDING_CODES
    )
    return {
        "present": artifact is not None,
        "thresholds_configured": thresholds_configured,
        "missing_thresholds": missing_thresholds,
        "passed": bool(
            artifact is not None
            and not source_findings
            and (payload.get("passed") is True or advisory_only_failure)
            and thresholds_configured
        ),
    }


def _concurrent_query_release_gate(
    artifact: dict[str, Any] | None,
    source_findings: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    missing_policy_values = [
        name
        for name in REQUIRED_CONCURRENT_QUERY_POLICY_NAMES
        if policy.get(name) is None
    ]
    return {
        "present": artifact is not None,
        "policy_configured": not missing_policy_values,
        "missing_policy_values": missing_policy_values,
        "passed": bool(
            artifact is not None
            and not source_findings
            and artifact["payload"].get("passed") is True
            and not missing_policy_values
        ),
    }


def _first_query_benchmark_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if report.get("report_type") != "mcp_first_query_benchmark":
        findings.append(
            _finding(
                "blocker",
                "first-query-benchmark-report-type-invalid",
                "First-query benchmark report_type is invalid.",
                expected_report_type="mcp_first_query_benchmark",
                actual_report_type=report.get("report_type"),
            )
        )
    schema_version = report.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        findings.append(
            _finding(
                "blocker",
                "first-query-benchmark-schema-invalid",
                "First-query benchmark schema_version is unsupported.",
                expected_schema_version=1,
                actual_schema_version=schema_version,
            )
        )
    structure_issues = _first_query_structure_issues(report)
    if structure_issues:
        findings.append(
            _finding(
                "blocker",
                "first-query-benchmark-structure-invalid",
                "First-query benchmark is missing required metrics or has inconsistent fields.",
                issues=structure_issues,
            )
        )
    threshold_issues = _first_query_threshold_issues(report)
    if threshold_issues:
        findings.append(
            _finding(
                "blocker",
                "first-query-benchmark-threshold-inconsistent",
                "First-query metrics do not satisfy the thresholds recorded in the report.",
                issues=threshold_issues,
            )
        )
    if isinstance(report.get("passed"), bool) and not report["passed"]:
        findings.append(
            _finding(
                "blocker",
                "first-query-benchmark-failed",
                "First-query benchmark report did not pass.",
                source_finding_count=_int(report.get("finding_count")),
                source_finding_codes=_report_finding_codes(report),
            )
        )
    return findings


def _retrieval_quality_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if report.get("report_type") != "mcp_retrieval_quality":
        findings.append(
            _finding(
                "blocker",
                "retrieval-quality-report-type-invalid",
                "Retrieval-quality report_type is invalid.",
                expected_report_type="mcp_retrieval_quality",
                actual_report_type=report.get("report_type"),
            )
        )
    schema_version = report.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        findings.append(
            _finding(
                "blocker",
                "retrieval-quality-schema-invalid",
                "Retrieval-quality schema_version is unsupported.",
                expected_schema_version=1,
                actual_schema_version=schema_version,
            )
        )
    structure_issues = _retrieval_quality_structure_issues(report)
    if structure_issues:
        findings.append(
            _finding(
                "blocker",
                "retrieval-quality-structure-invalid",
                "Retrieval-quality report is missing required metrics or has inconsistent fields.",
                issues=structure_issues,
            )
        )
    threshold_issues = _retrieval_quality_threshold_issues(report)
    if threshold_issues:
        findings.append(
            _finding(
                "blocker",
                "retrieval-quality-threshold-inconsistent",
                "Retrieval metrics do not satisfy the thresholds recorded in the report.",
                issues=threshold_issues,
            )
        )
    source_finding_codes = set(_report_finding_codes(report))
    advisory_only_failure = bool(source_finding_codes) and source_finding_codes.issubset(
        RETRIEVAL_QUALITY_ADVISORY_FINDING_CODES
    )
    if (
        isinstance(report.get("passed"), bool)
        and not report["passed"]
        and not advisory_only_failure
    ):
        findings.append(
            _finding(
                "blocker",
                "retrieval-quality-failed",
                "Retrieval-quality report did not pass.",
                source_finding_count=_int(report.get("finding_count")),
                source_finding_codes=_report_finding_codes(report),
            )
        )
    return findings


def _concurrent_query_benchmark_findings(
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if report.get("report_type") != "mcp_concurrent_query_benchmark":
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-benchmark-report-type-invalid",
                "Concurrent-query benchmark report_type is invalid.",
                expected_report_type="mcp_concurrent_query_benchmark",
                actual_report_type=report.get("report_type"),
            )
        )
    schema_version = report.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-benchmark-schema-invalid",
                "Concurrent-query benchmark schema_version is unsupported.",
                expected_schema_version=1,
                actual_schema_version=schema_version,
            )
        )
    structure_issues = _concurrent_query_structure_issues(report)
    if structure_issues:
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-benchmark-structure-invalid",
                "Concurrent-query benchmark has malformed or inconsistent measurements.",
                issues=structure_issues,
            )
        )
    threshold_issues = _concurrent_query_reported_threshold_issues(report)
    if threshold_issues:
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-benchmark-threshold-inconsistent",
                "Concurrent-query measurements do not satisfy the thresholds recorded by the producer.",
                issues=threshold_issues,
            )
        )
    if isinstance(report.get("passed"), bool) and not report["passed"]:
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-benchmark-failed",
                "Concurrent-query benchmark report did not pass.",
                source_finding_count=_int(report.get("finding_count")),
                source_finding_codes=_report_finding_codes(report),
            )
        )
    return findings


def _concurrent_query_external_policy_findings(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
    min_warm_records: int | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    concurrency = _strict_nonnegative_int(report.get("concurrency"))
    task_count = _strict_nonnegative_int(report.get("task_count"))
    min_concurrency = policy.get("min_concurrency")
    min_task_count = policy.get("min_task_count")
    if (
        min_concurrency is not None
        and (concurrency is None or concurrency < int(min_concurrency))
    ):
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-concurrency-below-policy",
                "Concurrent-query benchmark concurrency is below the external release policy.",
                actual_concurrency=concurrency,
                minimum_concurrency=min_concurrency,
            )
        )
    if (
        min_task_count is not None
        and (task_count is None or task_count < int(min_task_count))
    ):
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-task-count-below-policy",
                "Concurrent-query benchmark task count is below the external release policy.",
                actual_task_count=task_count,
                minimum_task_count=min_task_count,
            )
        )

    measurements = (
        report.get("measurements")
        if isinstance(report.get("measurements"), list)
        else []
    )
    max_task_total_ms = policy.get("max_task_total_ms")
    if max_task_total_ms is not None:
        violating_totals = [
            float(item["total_elapsed_ms"])
            for item in measurements
            if isinstance(item, dict)
            and _is_nonnegative_number(item.get("total_elapsed_ms"))
            and float(item["total_elapsed_ms"]) > float(max_task_total_ms)
        ]
        if violating_totals:
            findings.append(
                _finding(
                    "blocker",
                    "concurrent-query-task-total-exceeds-policy",
                    "One or more concurrent tasks exceed the external per-task latency policy.",
                    violation_count=len(violating_totals),
                    maximum_actual_ms=max(violating_totals),
                    maximum_allowed_ms=max_task_total_ms,
                )
            )

    batch_elapsed_ms = _dict(report.get("summary")).get("batch_elapsed_ms")
    max_batch_elapsed_ms = policy.get("max_batch_elapsed_ms")
    if (
        max_batch_elapsed_ms is not None
        and _is_nonnegative_number(batch_elapsed_ms)
        and float(batch_elapsed_ms) > float(max_batch_elapsed_ms)
    ):
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-batch-elapsed-exceeds-policy",
                "Concurrent batch elapsed time exceeds the external release policy.",
                actual_ms=batch_elapsed_ms,
                maximum_allowed_ms=max_batch_elapsed_ms,
            )
        )
    warm_record_count = _strict_nonnegative_int(
        _dict(report.get("warmup")).get("record_count")
    )
    if (
        min_warm_records is not None
        and (
            warm_record_count is None
            or warm_record_count < int(min_warm_records)
        )
    ):
        findings.append(
            _finding(
                "blocker",
                "concurrent-query-warm-record-count-below-minimum",
                "Concurrent-query warm record count is below the aggregate minimum.",
                actual_record_count=warm_record_count,
                minimum_record_count=min_warm_records,
            )
        )
    return findings


def _first_query_threshold_issues(report: dict[str, Any]) -> list[str]:
    thresholds = _dict(report.get("thresholds"))
    summary = _dict(report.get("summary"))
    cold = _dict(summary.get("cold"))
    warm = _dict(summary.get("warm"))
    issues: list[str] = []
    effective_min_success = thresholds.get("effective_min_success_count")
    if _valid_first_query_release_threshold(
        "effective_min_success_count",
        effective_min_success,
    ):
        successful_count = _strict_nonnegative_int(
            cold.get("successful_count")
        )
        if successful_count is None or successful_count < int(effective_min_success):
            issues.append("effective_min_success_count")
    elif effective_min_success is not None:
        issues.append("effective_min_success_count")

    min_result_count = thresholds.get("min_result_count")
    if _valid_first_query_release_threshold(
        "min_result_count",
        min_result_count,
    ):
        for scope, values in (("cold", cold), ("warm", warm)):
            answerable_successful_count = _strict_nonnegative_int(
                values.get("answerable_successful_count")
            )
            result_stats = _dict(values.get("answerable_result_count"))
            result_stats_count = _strict_nonnegative_int(
                result_stats.get("count")
            )
            result_minimum = _optional_float(result_stats.get("min"))
            if answerable_successful_count is None:
                issues.append(f"min_result_count.{scope}")
            elif answerable_successful_count == 0:
                if result_stats_count not in {None, 0}:
                    issues.append(f"min_result_count.{scope}")
            elif (
                result_stats_count != answerable_successful_count
                or result_minimum is None
                or result_minimum < int(min_result_count)
            ):
                issues.append(f"min_result_count.{scope}")
    elif min_result_count is not None:
        issues.append("min_result_count")
    if (_strict_nonnegative_int(cold.get("answerable_requested_count")) or 0) <= 0:
        issues.append("answerable_requested_count")

    required_strategy = thresholds.get("required_retrieval_strategy")
    if required_strategy not in {None, "flat_rag", "catalog_toc_body"}:
        issues.append("required_retrieval_strategy")
    else:
        for scope, values in (("cold", cold), ("warm", warm)):
            operational_count = _strict_nonnegative_int(
                values.get("operational_successful_count")
            )
            result_failure_count = _strict_nonnegative_int(
                values.get("result_requirement_failed_count")
            )
            strategy_failure_count = _strict_nonnegative_int(
                values.get("retrieval_strategy_requirement_failed_count")
            )
            qualified_count = _strict_nonnegative_int(
                values.get("successful_count")
            )
            if None in {
                operational_count,
                result_failure_count,
                strategy_failure_count,
                qualified_count,
            }:
                issues.append(f"qualification_counts.{scope}")
                continue
            assert operational_count is not None
            assert result_failure_count is not None
            assert strategy_failure_count is not None
            assert qualified_count is not None
            count_consistent = bool(
                result_failure_count <= operational_count
                and strategy_failure_count <= operational_count
                and qualified_count <= operational_count - result_failure_count
                and qualified_count
                <= operational_count - strategy_failure_count
                and qualified_count
                >= operational_count
                - result_failure_count
                - strategy_failure_count
            )
            if required_strategy is None:
                count_consistent = bool(
                    count_consistent
                    and strategy_failure_count == 0
                    and qualified_count
                    == operational_count - result_failure_count
                )
            if not count_consistent:
                issues.append(f"qualification_counts.{scope}")
    max_cold_p95 = thresholds.get("max_cold_p95_ms")
    if _is_nonnegative_number(max_cold_p95):
        actual_cold_p95 = _optional_float(
            _dict(summary.get("process_wall_elapsed_ms")).get("p95")
        )
        if actual_cold_p95 is None or actual_cold_p95 > float(max_cold_p95):
            issues.append("max_cold_p95_ms")
    max_warm_p95 = thresholds.get("max_warm_p95_ms")
    if _is_nonnegative_number(max_warm_p95):
        actual_warm_p95 = _optional_float(
            _dict(_dict(summary.get("warm")).get("search_elapsed_ms")).get("p95")
        )
        if actual_warm_p95 is None or actual_warm_p95 > float(max_warm_p95):
            issues.append("max_warm_p95_ms")
    warm_failed_count = _strict_nonnegative_int(warm.get("failed_count"))
    if warm_failed_count is None or warm_failed_count > 0:
        issues.append("warm_failed_count")
    return issues


def _retrieval_quality_threshold_issues(report: dict[str, Any]) -> list[str]:
    thresholds = _dict(report.get("thresholds"))
    summary = _dict(report.get("summary"))
    checks = (
        ("min_recall_at_1", "recall_at_1", "answerable_query_count", "minimum"),
        ("min_recall_at_3", "recall_at_3", "answerable_query_count", "minimum"),
        ("min_recall_at_5", "recall_at_5", "answerable_query_count", "minimum"),
        ("min_mrr", "mrr", "answerable_query_count", "minimum"),
        (
            "min_document_recall_at_1",
            "document_recall_at_1",
            "document_target_query_count",
            "minimum",
        ),
        (
            "min_document_recall_at_3",
            "document_recall_at_3",
            "document_target_query_count",
            "minimum",
        ),
        (
            "min_document_recall_at_5",
            "document_recall_at_5",
            "document_target_query_count",
            "minimum",
        ),
        (
            "max_no_evidence_false_positive_rate",
            "no_evidence_false_positive_rate",
            "no_evidence_query_count",
            "maximum",
        ),
        (
            "min_no_evidence_abstention_rate",
            "no_evidence_abstention_rate",
            "no_evidence_query_count",
            "minimum",
        ),
    )
    issues: list[str] = []
    for threshold_name, metric_name, count_name, comparison in checks:
        threshold = thresholds.get(threshold_name)
        if threshold is None:
            continue
        if not _is_unit_interval_number(threshold):
            issues.append(threshold_name)
            continue
        query_count = _strict_nonnegative_int(summary.get(count_name))
        actual = summary.get(metric_name)
        if (
            query_count is None
            or query_count <= 0
            or not _is_unit_interval_number(actual)
        ):
            issues.append(threshold_name)
            continue
        threshold_value = float(threshold)
        actual_value = float(actual)
        failed = (
            actual_value < threshold_value
            if comparison == "minimum"
            else actual_value > threshold_value
        )
        if failed:
            issues.append(threshold_name)
    return issues


def _concurrent_query_reported_threshold_issues(
    report: dict[str, Any],
) -> list[str]:
    thresholds = report.get("thresholds")
    if not isinstance(thresholds, dict):
        return ["thresholds"]
    issues: list[str] = []
    configured_values: list[Any] = []
    min_warm_records = thresholds.get("min_warm_records")
    configured_values.append(min_warm_records)
    if min_warm_records is not None:
        normalized_minimum = _strict_nonnegative_int(min_warm_records)
        warm_record_count = _strict_nonnegative_int(
            _dict(report.get("warmup")).get("record_count")
        )
        if (
            normalized_minimum is None
            or warm_record_count is None
            or warm_record_count < normalized_minimum
        ):
            issues.append("min_warm_records")
    measurements = (
        report.get("measurements")
        if isinstance(report.get("measurements"), list)
        else []
    )
    max_task_total_ms = thresholds.get("max_task_total_ms")
    configured_values.append(max_task_total_ms)
    if max_task_total_ms is not None:
        if not _is_nonnegative_number(max_task_total_ms):
            issues.append("max_task_total_ms")
        elif any(
            isinstance(item, dict)
            and _is_nonnegative_number(item.get("total_elapsed_ms"))
            and float(item["total_elapsed_ms"]) > float(max_task_total_ms)
            for item in measurements
        ):
            issues.append("max_task_total_ms")
    max_batch_elapsed_ms = thresholds.get("max_batch_elapsed_ms")
    configured_values.append(max_batch_elapsed_ms)
    if max_batch_elapsed_ms is not None:
        batch_elapsed_ms = _dict(report.get("summary")).get(
            "batch_elapsed_ms"
        )
        if (
            not _is_nonnegative_number(max_batch_elapsed_ms)
            or not _is_nonnegative_number(batch_elapsed_ms)
            or float(batch_elapsed_ms) > float(max_batch_elapsed_ms)
        ):
            issues.append("max_batch_elapsed_ms")
    for name in (
        "min_warm_records",
        "max_task_total_ms",
        "max_batch_elapsed_ms",
    ):
        if report.get(name) != thresholds.get(name):
            issues.append(f"{name}.top_level_consistency")
    expected_configured = any(value is not None for value in configured_values)
    if report.get("thresholds_configured") is not expected_configured:
        issues.append("thresholds_configured")
    return sorted(set(issues))


def _concurrent_query_structure_issues(
    report: dict[str, Any],
) -> list[str]:
    issues = _report_envelope_issues(report)
    query_count = _strict_nonnegative_int(report.get("query_count"))
    rounds = _strict_nonnegative_int(report.get("rounds"))
    concurrency = _strict_nonnegative_int(report.get("concurrency"))
    task_count = _strict_nonnegative_int(report.get("task_count"))
    top_k = _strict_nonnegative_int(report.get("top_k"))
    for name, value in (
        ("query_count", query_count),
        ("rounds", rounds),
        ("concurrency", concurrency),
        ("task_count", task_count),
        ("top_k", top_k),
    ):
        if value is None or value <= 0:
            issues.append(name)
    if (
        query_count is not None
        and rounds is not None
        and task_count is not None
        and task_count != query_count * rounds
    ):
        issues.append("task_count_consistency")
    if (
        concurrency is not None
        and task_count is not None
        and concurrency > task_count
    ):
        issues.append("concurrency_task_count_consistency")
    if _strict_nonnegative_int(report.get("api_call_count")) != 0:
        issues.append("api_call_count")
    settings_overrides = report.get("settings_overrides")
    if not isinstance(settings_overrides, dict):
        issues.append("settings_overrides")
    else:
        for name in ("api_audit_enabled", "rag_trace_enabled"):
            if settings_overrides.get(name) is not False:
                issues.append(f"settings_overrides.{name}")

    warmup = report.get("warmup")
    if not isinstance(warmup, dict):
        issues.append("warmup")
    else:
        warm_record_count = _strict_nonnegative_int(warmup.get("record_count"))
        if warm_record_count is None or warm_record_count <= 0:
            issues.append("warmup.record_count")
        if not isinstance(warmup.get("warmed"), bool):
            issues.append("warmup.warmed")
        elif warmup.get("warmed") is False and warmup.get("skipped") is not True:
            issues.append("warmup.warmed_or_skipped")
        if not (
            warmup.get("bm25_index_ready") is True
            or warmup.get("hierarchical_index_ready") is True
        ):
            issues.append("warmup.index_ready")
        if not _is_nonnegative_number(warmup.get("external_elapsed_ms")):
            issues.append("warmup.external_elapsed_ms")
        issues.extend(
            _finite_timing_mapping_issues(
                warmup.get("timing_ms"),
                "warmup.timing_ms",
                required=False,
            )
        )

    measurements = report.get("measurements")
    if not isinstance(measurements, list):
        return sorted(set([*issues, "measurements"]))
    if task_count is not None and len(measurements) != task_count:
        issues.append("measurements.task_count_consistency")

    successful_items: list[dict[str, Any]] = []
    error_count = 0
    answerable_items: list[dict[str, Any]] = []
    no_evidence_items: list[dict[str, Any]] = []
    seen_task_keys: set[tuple[int, int]] = set()
    fetch_elapsed_values: list[float] = []
    for index, raw_item in enumerate(measurements):
        path = f"measurements[{index}]"
        if not isinstance(raw_item, dict):
            issues.append(path)
            continue
        item = raw_item
        round_index = _strict_nonnegative_int(item.get("round"))
        query_index = _strict_nonnegative_int(item.get("query_index"))
        if (
            round_index is None
            or round_index <= 0
            or (rounds is not None and round_index > rounds)
        ):
            issues.append(f"{path}.round")
        if (
            query_index is None
            or query_index <= 0
            or (query_count is not None and query_index > query_count)
        ):
            issues.append(f"{path}.query_index")
        if round_index is not None and query_index is not None:
            task_key = (round_index, query_index)
            if task_key in seen_task_keys:
                issues.append("measurements.task_key_uniqueness")
            seen_task_keys.add(task_key)
        if not isinstance(item.get("query"), str) or not item["query"].strip():
            issues.append(f"{path}.query")
        if not isinstance(item.get("expect_no_evidence"), bool):
            issues.append(f"{path}.expect_no_evidence")

        error_value = item.get("error")
        if error_value is not None and (
            not isinstance(error_value, str) or not error_value
        ):
            issues.append(f"{path}.error")
        has_error = isinstance(error_value, str) and bool(error_value)
        if has_error:
            error_count += 1
        else:
            successful_items.append(item)
            if bool(item.get("expect_no_evidence")):
                no_evidence_items.append(item)
            else:
                answerable_items.append(item)

        search_result_count = _strict_nonnegative_int(
            item.get("search_result_count")
        )
        fetch_result_count = _strict_nonnegative_int(
            item.get("fetch_result_count")
        )
        if search_result_count is None:
            issues.append(f"{path}.search_result_count")
        if fetch_result_count is None:
            issues.append(f"{path}.fetch_result_count")
        if (
            search_result_count is not None
            and fetch_result_count is not None
            and fetch_result_count > search_result_count
        ):
            issues.append(f"{path}.fetch_result_count_consistency")
        if not _is_nonnegative_number(item.get("total_elapsed_ms")):
            issues.append(f"{path}.total_elapsed_ms")
        if not has_error:
            for timing_name in (
                "search_elapsed_ms",
                "fetch_elapsed_ms",
                "answer_elapsed_ms",
            ):
                if not _is_nonnegative_number(item.get(timing_name)):
                    issues.append(f"{path}.{timing_name}")
            if _strict_nonnegative_int(item.get("answer_char_count")) is None:
                issues.append(f"{path}.answer_char_count")
            if bool(item.get("expect_no_evidence")):
                if search_result_count != 0 or fetch_result_count != 0:
                    issues.append(f"{path}.no_evidence_result_count")
            elif (
                search_result_count is None
                or fetch_result_count is None
                or search_result_count < 1
                or fetch_result_count < 1
            ):
                issues.append(f"{path}.answerable_result_count")

        fetch_measurements = item.get("fetch_measurements")
        if not has_error and not isinstance(fetch_measurements, list):
            issues.append(f"{path}.fetch_measurements")
        elif isinstance(fetch_measurements, list):
            if (
                fetch_result_count is not None
                and len(fetch_measurements) != fetch_result_count
            ):
                issues.append(f"{path}.fetch_measurement_count")
            for fetch_index, fetch_item in enumerate(fetch_measurements):
                fetch_path = f"{path}.fetch_measurements[{fetch_index}]"
                if not isinstance(fetch_item, dict):
                    issues.append(fetch_path)
                    continue
                if not _is_nonnegative_number(fetch_item.get("elapsed_ms")):
                    issues.append(f"{fetch_path}.elapsed_ms")
                else:
                    fetch_elapsed_values.append(
                        float(fetch_item["elapsed_ms"])
                    )
                normalized_fetch_index = _strict_nonnegative_int(
                    fetch_item.get("fetch_index")
                )
                if normalized_fetch_index is None or normalized_fetch_index <= 0:
                    issues.append(f"{fetch_path}.fetch_index")
        issues.extend(
            _finite_timing_mapping_issues(
                item.get("mcp_search_timing_ms"),
                f"{path}.mcp_search_timing_ms",
                required=not has_error,
            )
        )

    if task_count is not None and len(seen_task_keys) != task_count:
        issues.append("measurements.task_key_coverage")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        return sorted(set([*issues, "summary"]))
    measurement_count = _strict_nonnegative_int(summary.get("measurement_count"))
    successful_count = _strict_nonnegative_int(summary.get("successful_count"))
    summary_error_count = _strict_nonnegative_int(summary.get("error_count"))
    expected_successful_count = len(measurements) - error_count
    if measurement_count != len(measurements):
        issues.append("summary.measurement_count_consistency")
    if successful_count != expected_successful_count:
        issues.append("summary.successful_count_consistency")
    if summary_error_count != error_count:
        issues.append("summary.error_count_consistency")
    if (
        measurement_count is not None
        and successful_count is not None
        and summary_error_count is not None
        and successful_count + summary_error_count != measurement_count
    ):
        issues.append("summary.success_error_count_consistency")

    answerable_count = len(answerable_items)
    no_evidence_count = len(no_evidence_items)
    answerable_zero_count = sum(
        1
        for item in answerable_items
        if _strict_nonnegative_int(item.get("search_result_count")) == 0
    )
    no_evidence_nonzero_count = sum(
        1
        for item in no_evidence_items
        if (_strict_nonnegative_int(item.get("search_result_count")) or 0) > 0
    )
    for name, expected in (
        ("answerable_measurement_count", answerable_count),
        ("no_evidence_measurement_count", no_evidence_count),
        ("answerable_zero_result_count", answerable_zero_count),
        ("no_evidence_nonzero_result_count", no_evidence_nonzero_count),
    ):
        if _strict_nonnegative_int(summary.get(name)) != expected:
            issues.append(f"summary.{name}_consistency")
    if successful_count is not None and (
        answerable_count + no_evidence_count != successful_count
    ):
        issues.append("summary.expectation_count_consistency")

    total_values = _concurrent_measurement_values(
        measurements,
        "total_elapsed_ms",
    )
    search_values = _concurrent_measurement_values(
        successful_items,
        "search_elapsed_ms",
    )
    fetch_values = _concurrent_measurement_values(
        successful_items,
        "fetch_elapsed_ms",
    )
    answer_values = _concurrent_measurement_values(
        successful_items,
        "answer_elapsed_ms",
    )
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("total_elapsed_ms"),
            "summary.total_elapsed_ms",
            total_values,
        )
    )
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("search_elapsed_ms"),
            "summary.search_elapsed_ms",
            search_values,
        )
    )
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("fetch_elapsed_ms"),
            "summary.fetch_elapsed_ms",
            fetch_values,
        )
    )
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("answer_elapsed_ms"),
            "summary.answer_elapsed_ms",
            answer_values,
        )
    )
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("single_fetch_elapsed_ms"),
            "summary.single_fetch_elapsed_ms",
            fetch_elapsed_values,
        )
    )
    answerable_result_values = [
        float(item["search_result_count"])
        for item in answerable_items
        if _strict_nonnegative_int(item.get("search_result_count")) is not None
    ]
    no_evidence_result_values = [
        float(item["search_result_count"])
        for item in no_evidence_items
        if _strict_nonnegative_int(item.get("search_result_count")) is not None
    ]
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("answerable_result_count"),
            "summary.answerable_result_count",
            answerable_result_values,
        )
    )
    issues.extend(
        _concurrent_stats_structure_issues(
            summary.get("no_evidence_result_count"),
            "summary.no_evidence_result_count",
            no_evidence_result_values,
        )
    )
    if not _is_nonnegative_number(summary.get("batch_elapsed_ms")):
        issues.append("summary.batch_elapsed_ms")
    search_counts = [
        normalized
        for item in measurements
        if isinstance(item, dict)
        for normalized in [
            _strict_nonnegative_int(item.get("search_result_count"))
        ]
        if normalized is not None
    ]
    fetch_counts = [
        normalized
        for item in measurements
        if isinstance(item, dict)
        for normalized in [
            _strict_nonnegative_int(item.get("fetch_result_count"))
        ]
        if normalized is not None
    ]
    expected_search_min = min(search_counts or [0])
    expected_fetch_min = min(fetch_counts or [0])
    if _strict_nonnegative_int(summary.get("search_result_count_min")) != expected_search_min:
        issues.append("summary.search_result_count_min_consistency")
    if _strict_nonnegative_int(summary.get("fetch_result_count_min")) != expected_fetch_min:
        issues.append("summary.fetch_result_count_min_consistency")
    return sorted(set(issues))


def _concurrent_measurement_values(
    measurements: list[Any],
    key: str,
) -> list[float]:
    return [
        float(item[key])
        for item in measurements
        if isinstance(item, dict) and _is_nonnegative_number(item.get(key))
    ]


def _finite_timing_mapping_issues(
    value: Any,
    path: str,
    *,
    required: bool,
) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, dict):
        return [path]
    return [
        f"{path}.{key}"
        for key, item in value.items()
        if not _is_nonnegative_number(item)
    ]


def _concurrent_stats_structure_issues(
    value: Any,
    path: str,
    measurements: list[float],
) -> list[str]:
    if not isinstance(value, dict):
        return [path]
    issues: list[str] = []
    count = _strict_nonnegative_int(value.get("count"))
    if count != len(measurements):
        issues.append(f"{path}.count")
    names = ("min", "p50", "p95", "max", "avg")
    if not measurements:
        for name in names:
            if value.get(name) is not None:
                issues.append(f"{path}.{name}")
        return issues
    numeric_values: dict[str, float] = {}
    for name in names:
        item = value.get(name)
        if not _is_nonnegative_number(item):
            issues.append(f"{path}.{name}")
        else:
            numeric_values[name] = float(item)
    if len(numeric_values) == len(names):
        if not (
            numeric_values["min"]
            <= numeric_values["p50"]
            <= numeric_values["p95"]
            <= numeric_values["max"]
        ):
            issues.append(f"{path}.order")
        if not math.isclose(
            numeric_values["min"],
            min(measurements),
            abs_tol=1e-3,
        ):
            issues.append(f"{path}.min_consistency")
        if not math.isclose(
            numeric_values["max"],
            max(measurements),
            abs_tol=1e-3,
        ):
            issues.append(f"{path}.max_consistency")
        if not math.isclose(
            numeric_values["avg"],
            round(sum(measurements) / len(measurements), 3),
            abs_tol=1e-3,
        ):
            issues.append(f"{path}.avg_consistency")
    return issues


def _first_query_structure_issues(report: dict[str, Any]) -> list[str]:
    issues = _report_envelope_issues(report)
    query_count = _strict_nonnegative_int(report.get("query_count"))
    if query_count is None or query_count <= 0:
        issues.append("query_count")
    iterations_per_query = _strict_nonnegative_int(report.get("iterations_per_query"))
    if iterations_per_query is None or iterations_per_query <= 0:
        issues.append("iterations_per_query")
    if _strict_nonnegative_int(report.get("warm_iterations_per_child")) is None:
        issues.append("warm_iterations_per_child")
    if _strict_nonnegative_int(report.get("api_call_count")) != 0:
        issues.append("api_call_count")
    search_call_count = _strict_nonnegative_int(report.get("search_call_count"))
    if search_call_count is None:
        issues.append("search_call_count")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return sorted(set([*issues, "summary"]))
    measurement_count = _strict_nonnegative_int(summary.get("measurement_count"))
    if measurement_count is None or measurement_count <= 0:
        issues.append("summary.measurement_count")
    if (
        measurement_count is not None
        and query_count is not None
        and iterations_per_query is not None
        and measurement_count != query_count * iterations_per_query
    ):
        issues.append("summary.measurement_count_consistency")
    issues.extend(
        _stats_structure_issues(
            summary.get("process_wall_elapsed_ms"),
            "summary.process_wall_elapsed_ms",
            p95_required=True,
        )
    )
    issues.extend(
        _stats_structure_issues(
            summary.get("successful_process_wall_elapsed_ms"),
            "summary.successful_process_wall_elapsed_ms",
            p95_required=False,
        )
    )
    cold = summary.get("cold")
    warm = summary.get("warm")
    cold_counts: dict[str, int] = {}
    warm_counts: dict[str, int] = {}
    if not isinstance(cold, dict):
        issues.append("summary.cold")
    else:
        cold_counts = _required_counts(
            cold,
            (
                "requested_count",
                "answerable_requested_count",
                "no_evidence_requested_count",
                "attempt_count",
                "not_attempted_count",
                "operational_successful_count",
                "successful_count",
                "answerable_successful_count",
                "no_evidence_successful_count",
                "failed_count",
                "result_requirement_failed_count",
                "retrieval_strategy_requirement_failed_count",
            ),
            prefix="summary.cold",
            issues=issues,
        )
        if measurement_count is not None and cold_counts.get("requested_count") != measurement_count:
            issues.append("summary.cold.requested_count_consistency")
        if all(
            name in cold_counts
            for name in (
                "requested_count",
                "answerable_requested_count",
                "no_evidence_requested_count",
            )
        ) and (
            cold_counts["answerable_requested_count"]
            + cold_counts["no_evidence_requested_count"]
            != cold_counts["requested_count"]
        ):
            issues.append("summary.cold.expectation_count_consistency")
        if all(name in cold_counts for name in ("attempt_count", "not_attempted_count", "requested_count")):
            if (
                cold_counts["attempt_count"] + cold_counts["not_attempted_count"]
                != cold_counts["requested_count"]
            ):
                issues.append("summary.cold.attempt_count_consistency")
        if all(name in cold_counts for name in ("successful_count", "failed_count", "requested_count")):
            if (
                cold_counts["successful_count"] + cold_counts["failed_count"]
                != cold_counts["requested_count"]
            ):
                issues.append("summary.cold.success_count_consistency")
        if all(
            name in cold_counts
            for name in (
                "successful_count",
                "answerable_successful_count",
                "no_evidence_successful_count",
            )
        ) and (
            cold_counts["answerable_successful_count"]
            + cold_counts["no_evidence_successful_count"]
            != cold_counts["successful_count"]
        ):
            issues.append("summary.cold.expectation_success_consistency")
        cold_success_count = cold_counts.get("successful_count", 0)
        issues.extend(
            _stats_structure_issues(
                cold.get("search_elapsed_ms"),
                "summary.cold.search_elapsed_ms",
                p95_required=cold_success_count > 0,
            )
        )
        issues.extend(
            _result_count_stats_structure_issues(
                cold.get("result_count"),
                "summary.cold.result_count",
                successful_count=cold_success_count,
            )
        )
        issues.extend(
            _result_count_stats_structure_issues(
                cold.get("answerable_result_count"),
                "summary.cold.answerable_result_count",
                successful_count=cold_counts.get(
                    "answerable_successful_count",
                    0,
                ),
            )
        )
        issues.extend(
            _no_evidence_result_count_stats_structure_issues(
                cold.get("no_evidence_result_count"),
                "summary.cold.no_evidence_result_count",
                successful_count=cold_counts.get(
                    "no_evidence_successful_count",
                    0,
                ),
            )
        )
    if not isinstance(warm, dict):
        issues.append("summary.warm")
    else:
        warm_counts = _required_counts(
            warm,
            (
                "attempt_count",
                "answerable_attempt_count",
                "no_evidence_attempt_count",
                "operational_successful_count",
                "successful_count",
                "answerable_successful_count",
                "no_evidence_successful_count",
                "failed_count",
                "result_requirement_failed_count",
                "retrieval_strategy_requirement_failed_count",
            ),
            prefix="summary.warm",
            issues=issues,
        )
        if all(name in warm_counts for name in ("attempt_count", "successful_count", "failed_count")):
            if (
                warm_counts["successful_count"] + warm_counts["failed_count"]
                != warm_counts["attempt_count"]
            ):
                issues.append("summary.warm.success_count_consistency")
        if all(
            name in warm_counts
            for name in (
                "attempt_count",
                "answerable_attempt_count",
                "no_evidence_attempt_count",
            )
        ) and (
            warm_counts["answerable_attempt_count"]
            + warm_counts["no_evidence_attempt_count"]
            != warm_counts["attempt_count"]
        ):
            issues.append("summary.warm.expectation_count_consistency")
        if all(
            name in warm_counts
            for name in (
                "successful_count",
                "answerable_successful_count",
                "no_evidence_successful_count",
            )
        ) and (
            warm_counts["answerable_successful_count"]
            + warm_counts["no_evidence_successful_count"]
            != warm_counts["successful_count"]
        ):
            issues.append("summary.warm.expectation_success_consistency")
        issues.extend(
            _stats_structure_issues(
                warm.get("search_elapsed_ms"),
                "summary.warm.search_elapsed_ms",
                p95_required=warm_counts.get("successful_count", 0) > 0,
            )
        )
        issues.extend(
            _result_count_stats_structure_issues(
                warm.get("result_count"),
                "summary.warm.result_count",
                successful_count=warm_counts.get("successful_count", 0),
            )
        )
        issues.extend(
            _result_count_stats_structure_issues(
                warm.get("answerable_result_count"),
                "summary.warm.answerable_result_count",
                successful_count=warm_counts.get(
                    "answerable_successful_count",
                    0,
                ),
            )
        )
        issues.extend(
            _no_evidence_result_count_stats_structure_issues(
                warm.get("no_evidence_result_count"),
                "summary.warm.no_evidence_result_count",
                successful_count=warm_counts.get(
                    "no_evidence_successful_count",
                    0,
                ),
            )
        )
    for name in ("timed_out_count", "invalid_protocol_count"):
        if _strict_nonnegative_int(summary.get(name)) is None:
            issues.append(f"summary.{name}")
    if (
        search_call_count is not None
        and "attempt_count" in cold_counts
        and "attempt_count" in warm_counts
        and search_call_count
        != cold_counts["attempt_count"] + warm_counts["attempt_count"]
    ):
        issues.append("search_call_count_consistency")
    return sorted(set(issues))


def _retrieval_quality_structure_issues(report: dict[str, Any]) -> list[str]:
    issues = _report_envelope_issues(report)
    query_count = _strict_nonnegative_int(report.get("query_count"))
    if query_count is None or query_count <= 0:
        issues.append("query_count")
    search_call_count = _strict_nonnegative_int(report.get("search_call_count"))
    query_spec_item_count = _strict_nonnegative_int(report.get("query_spec_item_count"))
    if query_spec_item_count is None or query_spec_item_count != query_count:
        issues.append("query_spec_item_count")
    if _strict_nonnegative_int(report.get("api_call_count")) != 0:
        issues.append("api_call_count")
    if not _is_sha256(report.get("query_spec_sha256")):
        issues.append("query_spec_sha256")
    threshold_failure_count = _strict_nonnegative_int(
        report.get("threshold_failure_count")
    )
    search_error_finding_count = _strict_nonnegative_int(
        report.get("search_error_finding_count")
    )
    query_spec_validation_finding_count = _strict_nonnegative_int(
        report.get("query_spec_validation_finding_count")
    )
    finding_count = _strict_nonnegative_int(report.get("finding_count"))
    if threshold_failure_count is None:
        issues.append("threshold_failure_count")
    if search_error_finding_count is None:
        issues.append("search_error_finding_count")
    if query_spec_validation_finding_count is None:
        issues.append("query_spec_validation_finding_count")
    if (
        threshold_failure_count is not None
        and search_error_finding_count is not None
        and query_spec_validation_finding_count is not None
        and finding_count is not None
        and threshold_failure_count
        + search_error_finding_count
        + query_spec_validation_finding_count
        != finding_count
    ):
        issues.append("source_finding_count_consistency")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return sorted(set([*issues, "summary"]))
    valid_query_spec_count = _strict_nonnegative_int(
        summary.get("valid_query_spec_count")
    )
    invalid_query_spec_count = _strict_nonnegative_int(
        summary.get("invalid_query_spec_count")
    )
    if (
        (valid_query_spec_count is None)
        != (invalid_query_spec_count is None)
    ):
        issues.append("summary.query_spec_validity_count_pair")
    if (
        valid_query_spec_count is not None
        and invalid_query_spec_count is not None
        and query_count is not None
        and valid_query_spec_count + invalid_query_spec_count != query_count
    ):
        issues.append("summary.valid_invalid_query_count_consistency")
    expected_search_call_count = (
        valid_query_spec_count
        if valid_query_spec_count is not None
        else query_count
    )
    if (
        search_call_count is None
        or expected_search_call_count is None
        or search_call_count != expected_search_call_count
    ):
        issues.append("search_call_count")
    counts = _required_counts(
        summary,
        (
            "answerable_query_count",
            "chunk_target_query_count",
            "document_target_query_count",
            "no_evidence_query_count",
            "search_error_count",
            "no_evidence_false_positive_count",
            "no_evidence_abstention_count",
        ),
        prefix="summary",
        issues=issues,
    )
    if (
        expected_search_call_count is not None
        and "answerable_query_count" in counts
        and "no_evidence_query_count" in counts
        and counts["answerable_query_count"] + counts["no_evidence_query_count"]
        != expected_search_call_count
    ):
        issues.append("summary.query_count_consistency")
    answerable_count = counts.get("answerable_query_count")
    for name in ("chunk_target_query_count", "document_target_query_count"):
        if (
            answerable_count is not None
            and name in counts
            and counts[name] > answerable_count
        ):
            issues.append(f"summary.{name}_consistency")
    no_evidence_count = counts.get("no_evidence_query_count")
    for name in (
        "no_evidence_false_positive_count",
        "no_evidence_abstention_count",
    ):
        if (
            no_evidence_count is not None
            and name in counts
            and counts[name] > no_evidence_count
        ):
            issues.append(f"summary.{name}_consistency")
    metric_names = (
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "mrr",
        "mean_reciprocal_rank",
        "document_recall_at_1",
        "document_recall_at_3",
        "document_recall_at_5",
        "no_evidence_false_positive_rate",
        "no_evidence_abstention_rate",
    )
    for name in metric_names:
        if not _is_unit_interval_number(summary.get(name)):
            issues.append(f"summary.{name}")
    if (
        _is_unit_interval_number(summary.get("mrr"))
        and _is_unit_interval_number(summary.get("mean_reciprocal_rank"))
        and not math.isclose(
            float(summary["mrr"]),
            float(summary["mean_reciprocal_rank"]),
            abs_tol=1e-6,
        )
    ):
        issues.append("summary.mrr_consistency")
    if (
        search_error_finding_count is not None
        and counts.get("search_error_count") != search_error_finding_count
    ):
        issues.append("summary.search_error_count_consistency")
    if (
        invalid_query_spec_count is not None
        and query_spec_validation_finding_count is not None
        and invalid_query_spec_count != query_spec_validation_finding_count
    ):
        issues.append("summary.invalid_query_spec_count_consistency")
    return sorted(set(issues))


def _report_envelope_issues(report: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    passed = report.get("passed")
    if not isinstance(passed, bool):
        issues.append("passed")
    finding_count = _strict_nonnegative_int(report.get("finding_count"))
    if finding_count is None:
        issues.append("finding_count")
    findings = report.get("findings")
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        issues.append("findings")
    elif finding_count is not None and finding_count != len(findings):
        issues.append("finding_count_consistency")
    if (
        isinstance(passed, bool)
        and finding_count is not None
        and passed != (finding_count == 0)
    ):
        issues.append("passed_consistency")
    return issues


def _stats_structure_issues(
    value: Any,
    path: str,
    *,
    p95_required: bool,
) -> list[str]:
    if not isinstance(value, dict) or "p95" not in value:
        return [path]
    p95 = value.get("p95")
    if p95 is None:
        return [f"{path}.p95"] if p95_required else []
    if not _is_nonnegative_number(p95):
        return [f"{path}.p95"]
    return []


def _result_count_stats_structure_issues(
    value: Any,
    path: str,
    *,
    successful_count: int,
) -> list[str]:
    if not isinstance(value, dict):
        return [path]
    count = _strict_nonnegative_int(value.get("count"))
    minimum = value.get("min")
    if count != successful_count:
        return [f"{path}.count"]
    if successful_count == 0:
        return [] if minimum is None else [f"{path}.min"]
    if not _is_nonnegative_number(minimum):
        return [f"{path}.min"]
    return []


def _no_evidence_result_count_stats_structure_issues(
    value: Any,
    path: str,
    *,
    successful_count: int,
) -> list[str]:
    issues = _result_count_stats_structure_issues(
        value,
        path,
        successful_count=successful_count,
    )
    if issues or successful_count == 0:
        return issues
    assert isinstance(value, dict)
    if (
        _optional_float(value.get("min")) != 0.0
        or _optional_float(value.get("max")) != 0.0
    ):
        return [f"{path}.zero_result_consistency"]
    return []


def _required_counts(
    source: dict[str, Any],
    names: tuple[str, ...],
    *,
    prefix: str,
    issues: list[str],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in names:
        value = _strict_nonnegative_int(source.get(name))
        if value is None:
            issues.append(f"{prefix}.{name}")
        else:
            result[name] = value
    return result


def _source_report_identity(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_path": str(source.get("path") or ""),
        "source_sha256": str(source.get("sha256") or ""),
        "source_byte_count": _int(source.get("byte_count")),
    }


def _report_finding_codes(report: dict[str, Any]) -> list[str]:
    return [
        str(item.get("code") or "")
        for item in report.get("findings") or []
        if isinstance(item, dict) and str(item.get("code") or "")
    ]


def _transport_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    profiles = {
        name: _transport_profile_summary(_dict(report.get(name)))
        for name in ("full_profile", "chatgpt_data_profile")
        if report.get(name)
    }
    warm_values = [
        value
        for profile in profiles.values()
        for value in [profile.get("warm_search_elapsed_ms")]
        if isinstance(value, (int, float))
    ]
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "tenant_id": str(report.get("tenant_id") or ""),
        "tenant_storage_isolation": report.get("tenant_storage_isolation"),
        "transport": str(report.get("transport") or ""),
        "profile_count": len(profiles),
        "max_warm_search_elapsed_ms": max(warm_values) if warm_values else None,
        "profiles": profiles,
    }


def _transport_profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": bool(profile.get("passed")),
        "tool_profile": str(profile.get("tool_profile") or ""),
        "search_result_count": _int(profile.get("search_result_count")),
        "warm_search_result_count": _int(profile.get("warm_search_result_count")),
        "fetch_has_text": bool(profile.get("fetch_has_text")),
        "list_tools_elapsed_ms": _optional_float(profile.get("list_tools_elapsed_ms")),
        "search_elapsed_ms": _optional_float(profile.get("search_elapsed_ms")),
        "warm_search_elapsed_ms": _optional_float(profile.get("warm_search_elapsed_ms")),
        "fetch_elapsed_ms": _optional_float(profile.get("fetch_elapsed_ms")),
        "total_elapsed_ms": _optional_float(profile.get("total_elapsed_ms")),
    }


def _visibility_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    requirements = _dict(report.get("requirements"))
    status_counts = {
        str(name): _int(value)
        for name, value in _dict(report.get("status_counts")).items()
    }
    return {
        "report_type": str(report.get("report_type") or ""),
        "passed": bool(report.get("passed")),
        "tenant_id": str(report.get("tenant_id") or ""),
        "require_indexed": _optional_bool(requirements.get("require_indexed")),
        "document_count": _int(report.get("document_count")),
        "status_counts": status_counts,
        "indexed_document_count": _int(status_counts.get("indexed")),
        "total_approved_chunks": _int(report.get("total_approved_chunks")),
        "total_indexable_record_count": _int(report.get("total_indexable_record_count")),
        "total_mcp_visible_records": _int(report.get("total_mcp_visible_records")),
        "total_skipped_unapproved_count": _int(report.get("total_skipped_unapproved_count")),
        "smoke_like_document_count": _int(report.get("smoke_like_document_count")),
        "finding_count": _int(report.get("finding_count")),
    }


def _benchmark_findings(
    report: dict[str, Any],
    *,
    summary: dict[str, Any],
    min_warm_records: int | None,
    max_total_p95_ms: float | None,
    max_warm_search_p95_ms: float | None,
) -> list[dict[str, Any]]:
    if not report:
        return []
    findings: list[dict[str, Any]] = []
    source_finding_codes = set(_report_finding_codes(report))
    advisory_only_failure = bool(source_finding_codes) and source_finding_codes.issubset(
        QUERY_BENCHMARK_ADVISORY_FINDING_CODES
    )
    if not report.get("passed") and not advisory_only_failure:
        findings.append(_finding("blocker", "query-benchmark-failed", "Query benchmark report did not pass."))
    if _int(summary.get("measurement_count")) <= 0:
        findings.append(
            _finding("blocker", "query-benchmark-measurements-missing", "Query benchmark has no measurements.")
        )
    effective_min_records = min_warm_records or summary.get("reported_min_warm_records")
    if effective_min_records and _int(summary.get("warm_record_count")) < int(effective_min_records):
        findings.append(
            _finding(
                "blocker",
                "query-benchmark-warm-record-count-low",
                "Warm benchmark record count is below the configured minimum.",
                actual_record_count=summary.get("warm_record_count"),
                threshold_record_count=int(effective_min_records),
            )
        )
    if max_total_p95_ms is not None:
        actual_total_p95 = summary.get("warm_total_p95_ms")
        metric_name = "warm_total_p95_ms"
        if not isinstance(actual_total_p95, (int, float)):
            actual_total_p95 = summary.get("total_p95_ms")
            metric_name = "total_p95_ms"
        if not isinstance(actual_total_p95, (int, float)):
            findings.append(
                _finding("blocker", "query-benchmark-total-p95-missing", "Query benchmark total p95 is missing.")
            )
        elif float(actual_total_p95) > max_total_p95_ms:
            findings.append(
                _finding(
                    "blocker",
                    "query-benchmark-total-p95-too-high",
                    "Query benchmark total p95 exceeded the configured threshold.",
                    actual_ms=actual_total_p95,
                    metric_name=metric_name,
                    threshold_ms=max_total_p95_ms,
                )
            )
    if max_warm_search_p95_ms is not None:
        actual_warm_p95 = summary.get("warm_search_p95_ms")
        if not isinstance(actual_warm_p95, (int, float)):
            findings.append(
                _finding(
                    "blocker",
                    "query-benchmark-warm-search-p95-missing",
                    "Query benchmark warm-search p95 is missing.",
                )
            )
        elif float(actual_warm_p95) > max_warm_search_p95_ms:
            findings.append(
                _finding(
                    "blocker",
                    "query-benchmark-warm-search-p95-too-high",
                    "Query benchmark warm-search p95 exceeded the configured threshold.",
                    actual_ms=actual_warm_p95,
                    threshold_ms=max_warm_search_p95_ms,
                )
            )
    if _int(summary.get("api_call_count")) > 0:
        findings.append(_finding("warning", "query-benchmark-api-calls", "Benchmark recorded external API calls."))
    return findings


def _transport_findings(
    report: dict[str, Any],
    *,
    summary: dict[str, Any],
    max_transport_warm_search_ms: float | None,
) -> list[dict[str, Any]]:
    if not report:
        return []
    findings: list[dict[str, Any]] = []
    if not report.get("passed"):
        findings.append(_finding("blocker", "transport-smoke-failed", "Transport smoke report did not pass."))
    for name, profile in (summary.get("profiles") or {}).items():
        if not profile.get("passed"):
            findings.append(
                _finding("blocker", "transport-profile-failed", f"Transport profile {name} did not pass.")
            )
    actual = summary.get("max_warm_search_elapsed_ms")
    if max_transport_warm_search_ms is not None:
        if not isinstance(actual, (int, float)):
            findings.append(
                _finding("blocker", "transport-warm-search-missing", "Transport warm-search latency is missing.")
            )
        elif float(actual) > max_transport_warm_search_ms:
            findings.append(
                _finding(
                    "blocker",
                    "transport-warm-search-too-high",
                    "Transport smoke warm-search elapsed time exceeded the configured threshold.",
                    actual_ms=actual,
                    threshold_ms=max_transport_warm_search_ms,
                )
            )
    return findings


def _visibility_findings(
    report: dict[str, Any],
    *,
    summary: dict[str, Any],
    require_indexed_visibility: bool,
    require_visibility_match: bool,
    require_no_smoke_docs: bool,
) -> list[dict[str, Any]]:
    if not report:
        return []
    findings: list[dict[str, Any]] = []
    if not report.get("passed"):
        findings.append(_finding("blocker", "index-visibility-failed", "Index visibility report did not pass."))
    if require_indexed_visibility:
        if summary.get("require_indexed") is not True:
            findings.append(
                _finding(
                    "blocker",
                    "index-visibility-indexed-requirement-missing",
                    "Strict release evidence requires a visibility report produced with --require-indexed.",
                    reported_require_indexed=summary.get("require_indexed"),
                )
            )
        document_count = _int(summary.get("document_count"))
        status_counts = _dict(summary.get("status_counts"))
        indexed_document_count = _int(status_counts.get("indexed"))
        status_document_count = sum(
            _int(value) for value in status_counts.values()
        )
        if (
            document_count <= 0
            or status_document_count != document_count
            or indexed_document_count != document_count
        ):
            findings.append(
                _finding(
                    "blocker",
                    "index-visibility-indexed-status-incomplete",
                    "Visibility evidence does not prove that every audited document has indexing_status=indexed.",
                    document_count=document_count,
                    indexed_document_count=indexed_document_count,
                    status_document_count=status_document_count,
                    status_counts=status_counts,
                )
            )
    indexable = _int(summary.get("total_indexable_record_count"))
    visible = _int(summary.get("total_mcp_visible_records"))
    if require_visibility_match and indexable != visible:
        findings.append(
            _finding(
                "blocker",
                "index-visible-record-count-mismatch",
                "MCP-visible record count does not match indexable record count.",
                indexable_record_count=indexable,
                visible_record_count=visible,
            )
        )
    smoke_docs = _int(summary.get("smoke_like_document_count"))
    if require_no_smoke_docs and smoke_docs > 0:
        findings.append(
            _finding(
                "blocker",
                "index-visibility-smoke-documents-present",
                "Smoke-like documents are present in the visibility report.",
                smoke_like_document_count=smoke_docs,
            )
        )
    return findings


def _record_count_findings(
    *,
    benchmark_summary: dict[str, Any],
    visibility_summary: dict[str, Any],
    vector_summary: dict[str, Any],
    bm25_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    counts = {
        "benchmark_warm_records": _int(benchmark_summary.get("warm_record_count")),
        "visibility_indexable_records": _int(visibility_summary.get("total_indexable_record_count")),
        "visibility_mcp_visible_records": _int(visibility_summary.get("total_mcp_visible_records")),
        "approved_vector_jsonl_records": _int(vector_summary.get("record_count")),
        "bm25_document_count": _int(bm25_summary.get("document_count")),
    }
    comparable = {key: value for key, value in counts.items() if value > 0}
    if len(set(comparable.values())) <= 1:
        return []
    return [
        _finding(
            "blocker",
            "large-runtime-record-count-mismatch",
            "Large-runtime evidence record counts do not agree.",
            **counts,
        )
    ]


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"severity": severity, "code": code, "detail": detail}
    item.update(extra)
    return item


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _to_markdown(report: dict[str, Any]) -> str:
    benchmark = report.get("query_benchmark_summary") or {}
    transport = report.get("transport_smoke_summary") or {}
    visibility = report.get("index_visibility_summary") or {}
    files = report.get("file_summary") or {}
    vector = files.get("approved_vectors") or {}
    bm25 = files.get("bm25_index") or {}
    first_query = report.get("first_query_benchmark_summary") or {}
    retrieval_quality = report.get("retrieval_quality_summary") or {}
    concurrent_query = report.get("concurrent_query_benchmark_summary") or {}
    optional_lines: list[str] = []
    if first_query:
        optional_lines.extend(
            [
                (
                    "- First-query cold process/search p95 ms: "
                    f"{first_query.get('cold_process_wall_p95_ms')} / "
                    f"{first_query.get('cold_search_p95_ms')}"
                ),
                (
                    "- First-query warm p95 ms and cold/warm successes: "
                    f"{first_query.get('warm_search_p95_ms')} / "
                    f"{first_query.get('cold_successful_count')} / "
                    f"{first_query.get('warm_successful_count')}"
                ),
            ]
        )
    if retrieval_quality:
        optional_lines.extend(
            [
                (
                    "- Retrieval Recall@1/3/5 and MRR: "
                    f"{retrieval_quality.get('recall_at_1')} / "
                    f"{retrieval_quality.get('recall_at_3')} / "
                    f"{retrieval_quality.get('recall_at_5')} / "
                    f"{retrieval_quality.get('mrr')}"
                ),
                (
                    "- Retrieval document Recall@1/3/5: "
                    f"{retrieval_quality.get('document_recall_at_1')} / "
                    f"{retrieval_quality.get('document_recall_at_3')} / "
                    f"{retrieval_quality.get('document_recall_at_5')}"
                ),
                (
                    "- Retrieval no-evidence false-positive/abstention rates: "
                    f"{retrieval_quality.get('no_evidence_false_positive_rate')} / "
                    f"{retrieval_quality.get('no_evidence_abstention_rate')}"
                ),
            ]
        )
    if concurrent_query:
        optional_lines.extend(
            [
                (
                    "- Concurrent tasks/concurrency/successes/errors: "
                    f"{concurrent_query.get('task_count')} / "
                    f"{concurrent_query.get('concurrency')} / "
                    f"{concurrent_query.get('successful_count')} / "
                    f"{concurrent_query.get('error_count')}"
                ),
                (
                    "- Concurrent batch/task p95/task max ms: "
                    f"{concurrent_query.get('batch_elapsed_ms')} / "
                    f"{concurrent_query.get('task_total_p95_ms')} / "
                    f"{concurrent_query.get('recomputed_task_total_max_ms')}"
                ),
            ]
        )
    lines = [
        "# MCP Performance Load Evidence",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Evidence ready: `{str(report.get('evidence_ready')).lower()}`",
        f"- Performance release ready: `{str(report.get('performance_release_ready')).lower()}`",
        (
            "- Source repo commits required/status/fully verified: "
            f"{(report.get('repo_commit_consistency') or {}).get('required')} / "
            f"{(report.get('repo_commit_consistency') or {}).get('status')} / "
            f"{(report.get('repo_commit_consistency') or {}).get('fully_verified')}"
        ),
        (
            "- Source state required/status/fully verified: "
            f"{(report.get('source_state_consistency') or {}).get('required')} / "
            f"{(report.get('source_state_consistency') or {}).get('status')} / "
            f"{(report.get('source_state_consistency') or {}).get('fully_verified')}"
        ),
        f"- Latency SLO evaluated: `{str((report.get('latency_slo') or {}).get('evaluated')).lower()}`",
        (
            "- First-query release gate present/configured/passed: "
            f"{(report.get('first_query_release_gate') or {}).get('present')} / "
            f"{(report.get('first_query_release_gate') or {}).get('thresholds_configured')} / "
            f"{(report.get('first_query_release_gate') or {}).get('passed')}"
        ),
        (
            "- Retrieval-quality release gate present/configured/passed: "
            f"{(report.get('retrieval_quality_release_gate') or {}).get('present')} / "
            f"{(report.get('retrieval_quality_release_gate') or {}).get('thresholds_configured')} / "
            f"{(report.get('retrieval_quality_release_gate') or {}).get('passed')}"
        ),
        (
            "- Concurrent-query release gate present/configured/passed: "
            f"{(report.get('concurrent_query_release_gate') or {}).get('present')} / "
            f"{(report.get('concurrent_query_release_gate') or {}).get('policy_configured')} / "
            f"{(report.get('concurrent_query_release_gate') or {}).get('passed')}"
        ),
        f"- Blocking: {report.get('blocking_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- Query benchmark total p95/max ms: {benchmark.get('total_p95_ms')} / {benchmark.get('total_max_ms')}",
        (
            f"- Query benchmark warm records: {benchmark.get('warm_record_count')} / min "
            f"{report.get('thresholds', {}).get('min_warm_records') or benchmark.get('reported_min_warm_records')}"
        ),
        f"- Transport max warm-search ms: {transport.get('max_warm_search_elapsed_ms')}",
        (
            "- Visibility indexable/visible records: "
            f"{visibility.get('total_indexable_record_count')} / "
            f"{visibility.get('total_mcp_visible_records')}"
        ),
        (
            "- Visibility require-indexed provenance/indexed documents: "
            f"{visibility.get('require_indexed')} / "
            f"{visibility.get('indexed_document_count')} of "
            f"{visibility.get('document_count')}"
        ),
        f"- Approved vector JSONL: {vector.get('record_count')} records / {vector.get('byte_count')} bytes",
        (
            f"- BM25 index: {bm25.get('document_count')} documents / "
            f"{bm25.get('byte_count')} bytes / model `{bm25.get('retrieval_model')}`"
        ),
        *optional_lines,
        "",
        "## Findings",
        "",
    ]
    if report.get("findings"):
        lines.extend(
            f"- `{item.get('severity')}` `{item.get('code')}`: {item.get('detail')}"
            for item in report["findings"]
        )
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _validate_latency_thresholds(
    thresholds: dict[str, float | None],
) -> None:
    for name, value in thresholds.items():
        if value is not None and not _is_nonnegative_number(value):
            raise ValueError(f"{name} must be a finite non-negative number.")


def _validate_concurrent_query_policy(policy: dict[str, Any]) -> None:
    minimum_domains = {
        "min_concurrency": 2,
        "min_task_count": 1,
    }
    for name, minimum in minimum_domains.items():
        value = policy.get(name)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            raise ValueError(
                f"{name} must be an integer greater than or equal to {minimum}."
            )
    for name in ("max_task_total_ms", "max_batch_elapsed_ms"):
        value = policy.get(name)
        if value is not None and not _is_nonnegative_number(value):
            raise ValueError(f"{name} must be a finite non-negative number.")


def _is_unit_interval_number(value: Any) -> bool:
    return _is_nonnegative_number(value) and float(value) <= 1.0


def _is_sha256(value: Any) -> bool:
    normalized = str(value or "").lower()
    return len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compose MCP performance/load evidence from benchmark, transport, "
            "visibility, and local index files."
        )
    )
    parser.add_argument("--query-benchmark-report", required=True)
    parser.add_argument("--transport-smoke-report", required=True)
    parser.add_argument("--index-visibility-report", required=True)
    parser.add_argument("--approved-vectors-jsonl", required=True)
    parser.add_argument("--bm25-index-json", required=True)
    parser.add_argument(
        "--first-query-benchmark-report",
        default=None,
        help="Optional JSON report produced by benchmark_mcp_first_query.py.",
    )
    parser.add_argument(
        "--retrieval-quality-report",
        default=None,
        help="Optional JSON report produced by evaluate_mcp_retrieval_quality.py.",
    )
    parser.add_argument(
        "--concurrent-query-benchmark-report",
        "--concurrent-benchmark-report",
        dest="concurrent_query_benchmark_report",
        default=None,
        help="Optional JSON report produced by benchmark_mcp_concurrent_queries.py.",
    )
    parser.add_argument("--min-warm-records", type=int, default=None)
    parser.add_argument("--max-total-p95-ms", type=float, default=None)
    parser.add_argument("--max-warm-search-p95-ms", type=float, default=None)
    parser.add_argument("--max-transport-warm-search-ms", type=float, default=None)
    parser.add_argument(
        "--require-latency-slo",
        action="store_true",
        help="Fail closed unless all three latency thresholds are configured and pass.",
    )
    parser.add_argument(
        "--require-repo-commit-consistency",
        action="store_true",
        help=(
            "Fail closed unless every selected JSON source report has the same "
            "valid repo_commit and source_state, matching the evidence builder."
        ),
    )
    parser.add_argument(
        "--require-first-query-benchmark",
        action="store_true",
        help="Fail closed unless threshold-bearing first-query evidence is present and valid.",
    )
    parser.add_argument(
        "--require-retrieval-quality",
        action="store_true",
        help="Fail closed unless threshold-bearing retrieval-quality evidence is present and valid.",
    )
    parser.add_argument(
        "--require-concurrent-query-benchmark",
        action="store_true",
        help=(
            "Fail closed unless valid concurrent-query evidence and all "
            "external concurrent policy values are present."
        ),
    )
    parser.add_argument(
        "--min-concurrent-query-concurrency",
        "--min-concurrent-concurrency",
        "--concurrent-min-concurrency",
        dest="min_concurrent_query_concurrency",
        type=int,
        default=None,
        help="External minimum configured concurrency; must be at least 2.",
    )
    parser.add_argument(
        "--min-concurrent-query-task-count",
        "--min-concurrent-task-count",
        "--concurrent-min-task-count",
        dest="min_concurrent_query_task_count",
        type=int,
        default=None,
        help="External minimum number of measured concurrent tasks.",
    )
    parser.add_argument(
        "--max-concurrent-query-task-total-ms",
        "--max-concurrent-task-total-ms",
        "--concurrent-max-task-total-ms",
        dest="max_concurrent_query_task_total_ms",
        type=float,
        default=None,
        help="External maximum total elapsed time for every concurrent task.",
    )
    parser.add_argument(
        "--max-concurrent-query-batch-elapsed-ms",
        "--max-concurrent-batch-elapsed-ms",
        "--concurrent-max-batch-elapsed-ms",
        dest="max_concurrent_query_batch_elapsed_ms",
        type=float,
        default=None,
        help="External maximum concurrent batch wall-clock elapsed time.",
    )
    parser.add_argument(
        "--expected-first-query-retrieval-strategy",
        choices=("flat_rag", "catalog_toc_body"),
        default=None,
        help=(
            "Require the first-query report to enforce this retrieval strategy "
            "as an external release policy."
        ),
    )
    parser.add_argument(
        "--require-indexed-visibility",
        action="store_true",
        help=(
            "Fail closed unless the visibility report records "
            "requirements.require_indexed=true and every audited document is indexed."
        ),
    )
    parser.add_argument("--allow-visibility-mismatch", action="store_true")
    parser.add_argument("--allow-smoke-docs", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument(
        "--out-public-json",
        default=None,
        help="Write a redacted shareable JSON derivative without local or institution identifiers.",
    )
    parser.add_argument(
        "--out-public-md",
        default=None,
        help="Write a redacted shareable Markdown derivative.",
    )
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = build_mcp_performance_load_evidence(
        query_benchmark_report=Path(args.query_benchmark_report),
        transport_smoke_report=Path(args.transport_smoke_report),
        index_visibility_report=Path(args.index_visibility_report),
        approved_vectors_jsonl=Path(args.approved_vectors_jsonl),
        bm25_index_json=Path(args.bm25_index_json),
        first_query_benchmark_report=(
            Path(args.first_query_benchmark_report)
            if args.first_query_benchmark_report
            else None
        ),
        retrieval_quality_report=(
            Path(args.retrieval_quality_report)
            if args.retrieval_quality_report
            else None
        ),
        concurrent_query_benchmark_report=(
            Path(args.concurrent_query_benchmark_report)
            if args.concurrent_query_benchmark_report
            else None
        ),
        min_warm_records=args.min_warm_records,
        max_total_p95_ms=args.max_total_p95_ms,
        max_warm_search_p95_ms=args.max_warm_search_p95_ms,
        max_transport_warm_search_ms=args.max_transport_warm_search_ms,
        require_latency_slo=args.require_latency_slo,
        require_repo_commit_consistency=args.require_repo_commit_consistency,
        require_first_query_benchmark=args.require_first_query_benchmark,
        require_retrieval_quality=args.require_retrieval_quality,
        require_concurrent_query_benchmark=(
            args.require_concurrent_query_benchmark
        ),
        expected_first_query_retrieval_strategy=(
            args.expected_first_query_retrieval_strategy
        ),
        min_concurrent_query_concurrency=(
            args.min_concurrent_query_concurrency
        ),
        min_concurrent_query_task_count=args.min_concurrent_query_task_count,
        max_concurrent_query_task_total_ms=(
            args.max_concurrent_query_task_total_ms
        ),
        max_concurrent_query_batch_elapsed_ms=(
            args.max_concurrent_query_batch_elapsed_ms
        ),
        require_indexed_visibility=args.require_indexed_visibility,
        require_visibility_match=not args.allow_visibility_mismatch,
        require_no_smoke_docs=not args.allow_smoke_docs,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
        out_public_json=Path(args.out_public_json) if args.out_public_json else None,
        out_public_md=Path(args.out_public_md) if args.out_public_md else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report["evidence_ready"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
