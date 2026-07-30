from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mcp_server.regulation_tools import mcp_auth_context, search_regulations, settings_for_mcp_project
from scripts.benchmark_mcp_queries import _verified_runtime_target_ids
from scripts.report_metadata import (
    capture_mcp_performance_source_state,
    current_repo_commit,
    finalize_mcp_performance_source_state,
)


EVALUATION_CUTOFFS = (1, 3, 5)
MAX_MCP_TOP_K = 20


def evaluate_mcp_retrieval_quality(
    *,
    data_dir: Path,
    tenant_id: str,
    query_specs: list[dict[str, Any]],
    profile_id: str | None = None,
    top_k: int = 5,
    security_levels: list[str] | None = None,
    department_ids: list[str] | None = None,
    as_of_date: str | None = None,
    tenant_storage_isolation: bool | None = None,
    query_spec_source: Path | None = None,
    min_recall_at_1: float | None = None,
    min_recall_at_3: float | None = None,
    min_recall_at_5: float | None = None,
    min_mrr: float | None = None,
    min_document_recall_at_1: float | None = None,
    min_document_recall_at_3: float | None = None,
    min_document_recall_at_5: float | None = None,
    max_no_evidence_false_positive_rate: float | None = None,
    min_no_evidence_abstention_rate: float | None = None,
    out_json: Path | None = None,
) -> dict[str, Any]:
    """Evaluate the approved MCP search path against labeled chunk/document targets."""
    started_source_state = capture_mcp_performance_source_state(PROJECT_ROOT)
    normalized_specs = normalize_query_specs(query_specs)
    fingerprint = _query_spec_fingerprint(
        query_specs,
        query_spec_source,
        item_count=len(normalized_specs),
    )
    thresholds = _thresholds(
        min_recall_at_1=min_recall_at_1,
        min_recall_at_3=min_recall_at_3,
        min_recall_at_5=min_recall_at_5,
        min_mrr=min_mrr,
        min_document_recall_at_1=min_document_recall_at_1,
        min_document_recall_at_3=min_document_recall_at_3,
        min_document_recall_at_5=min_document_recall_at_5,
        max_no_evidence_false_positive_rate=max_no_evidence_false_positive_rate,
        min_no_evidence_abstention_rate=min_no_evidence_abstention_rate,
    )
    requested_top_k, search_top_k = _normalize_top_k(top_k)
    levels = [str(level) for level in (security_levels or ["internal"]) if str(level).strip()]
    departments = [str(value) for value in (department_ids or []) if str(value).strip()]
    settings = settings_for_mcp_project(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        api_audit_enabled=False,
        rag_trace_enabled=False,
    )
    auth = mcp_auth_context(tenant_id=tenant_id, department_ids=departments)
    target_validations = _runtime_target_validations(
        settings=settings,
        auth=auth,
        tenant_id=tenant_id,
        query_specs=normalized_specs,
        profile_id=profile_id,
        default_as_of_date=as_of_date,
    )
    validation_by_id = {item["id"]: item for item in target_validations}
    results = []
    for spec in normalized_specs:
        validation = validation_by_id.get(spec["id"])
        if validation and not validation["valid"]:
            results.append(_invalid_runtime_target_result(spec, validation=validation))
            continue
        results.append(
            _evaluate_query(
                settings=settings,
                auth=auth,
                spec=spec,
                search_top_k=search_top_k,
                security_levels=levels,
                department_ids=departments,
                profile_id=profile_id,
                default_as_of_date=as_of_date,
            )
        )
    summary = _summarize_results(results)
    search_error_findings = _search_error_findings(results)
    runtime_target_findings = _runtime_target_findings(results)
    threshold_findings = _threshold_findings(summary, thresholds)
    findings = search_error_findings + runtime_target_findings + threshold_findings
    valid_query_spec_count = int(summary["valid_query_spec_count"])
    source_state = finalize_mcp_performance_source_state(
        started_source_state,
        PROJECT_ROOT,
    )
    report = {
        "report_type": "mcp_retrieval_quality",
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_state": source_state,
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "security_levels": levels,
        "department_ids": departments,
        "as_of_date": as_of_date,
        "settings_overrides": {
            "api_audit_enabled": False,
            "rag_trace_enabled": False,
        },
        "requested_top_k": requested_top_k,
        "search_top_k": search_top_k,
        "evaluation_cutoffs": list(EVALUATION_CUTOFFS),
        "evaluation_policy": {
            "primary_relevance": (
                "target_chunk_ids when present; otherwise target_document_ids"
            ),
            "multi_target_recall": "unique matched targets divided by unique labeled targets",
            "no_evidence_abstention": "successful search with zero results",
            "no_evidence_false_positive": "successful search with one or more results",
        },
        "query_count": len(results),
        "summary": summary,
        "thresholds": thresholds,
        "thresholds_configured": any(value is not None for value in thresholds.values()),
        "threshold_failure_count": len(threshold_findings),
        "search_error_finding_count": len(search_error_findings),
        "query_spec_validation_finding_count": len(runtime_target_findings),
        "finding_count": len(findings),
        "findings": findings,
        "passed": not findings,
        "search_call_count": valid_query_spec_count,
        "api_call_count": 0,
        "results": results,
    }
    report.update(summary)
    report.update(fingerprint)
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def normalize_query_specs(query_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate query specifications and normalize singular/plural target identifiers."""
    if not isinstance(query_specs, list) or not query_specs:
        raise ValueError("query_specs must contain at least one query specification.")
    normalized: list[dict[str, Any]] = []
    for index, raw_spec in enumerate(query_specs, start=1):
        if not isinstance(raw_spec, dict):
            raise ValueError(f"query_specs[{index - 1}] must be an object.")
        query = str(raw_spec.get("query") or raw_spec.get("question") or "").strip()
        if not query:
            raise ValueError(f"query_specs[{index - 1}] must include query or question.")
        expect_no_evidence = bool(
            raw_spec.get("expect_no_evidence") or raw_spec.get("expected_no_evidence")
        )
        target_chunk_ids = _target_ids(raw_spec, "target_chunk_id", "target_chunk_ids")
        target_document_ids = _target_ids(raw_spec, "target_document_id", "target_document_ids")
        if expect_no_evidence and (target_chunk_ids or target_document_ids):
            raise ValueError(
                f"query_specs[{index - 1}] cannot combine expect_no_evidence with target identifiers."
            )
        if not expect_no_evidence and not (target_chunk_ids or target_document_ids):
            raise ValueError(
                f"query_specs[{index - 1}] must include a target chunk/document identifier "
                "or set expect_no_evidence."
            )
        normalized.append(
            {
                "id": str(raw_spec.get("id") or f"query_{index:03d}").strip(),
                "query": query,
                "expect_no_evidence": expect_no_evidence,
                "target_chunk_ids": target_chunk_ids,
                "target_document_ids": target_document_ids,
                "as_of_date": _optional_text(raw_spec.get("as_of_date")),
            }
        )
    return normalized


def load_query_specs(path: Path) -> list[dict[str, Any]]:
    """Load query specifications from a list or a queries/items wrapper object."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if "queries" in payload:
            payload = payload["queries"]
        elif "items" in payload:
            payload = payload["items"]
        elif "query_specs" in payload:
            payload = payload["query_specs"]
    if not isinstance(payload, list):
        raise ValueError("--query-spec-json must contain a list or an object with queries/items/query_specs.")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError("--query-spec-json entries must be objects.")
    return list(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate Recall@1/3/5, MRR, document recall, and MCP no-evidence behavior."
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--query-spec-json", required=True)
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Requested result depth; evaluation always retrieves at least 5.",
    )
    parser.add_argument("--security-level", action="append", default=None)
    parser.add_argument("--department-id", action="append", default=None)
    parser.add_argument("--as-of-date", default=None)
    storage_group = parser.add_mutually_exclusive_group()
    storage_group.add_argument("--tenant-storage-isolation", action="store_true")
    storage_group.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--min-recall-at-1", type=float, default=None)
    parser.add_argument("--min-recall-at-3", type=float, default=None)
    parser.add_argument("--min-recall-at-5", type=float, default=None)
    parser.add_argument("--min-mrr", type=float, default=None)
    parser.add_argument("--min-document-recall-at-1", type=float, default=None)
    parser.add_argument("--min-document-recall-at-3", type=float, default=None)
    parser.add_argument(
        "--min-document-recall-at-5",
        "--min-document-recall",
        dest="min_document_recall_at_5",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--max-no-evidence-false-positive-rate",
        "--max-false-positive-rate",
        dest="max_no_evidence_false_positive_rate",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--min-no-evidence-abstention-rate",
        "--min-abstention-rate",
        dest="min_no_evidence_abstention_rate",
        type=float,
        default=None,
    )
    parser.add_argument("--out-json", default=None)
    parser.add_argument(
        "--fail-on-threshold",
        action="store_true",
        help="Return exit code 2 when a threshold or MCP search evaluation finding exists.",
    )
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    query_spec_source = Path(args.query_spec_json)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    elif args.flat_storage:
        tenant_storage_isolation = False
    report = evaluate_mcp_retrieval_quality(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        query_specs=load_query_specs(query_spec_source),
        top_k=args.top_k,
        security_levels=args.security_level,
        department_ids=args.department_id,
        as_of_date=args.as_of_date,
        tenant_storage_isolation=tenant_storage_isolation,
        query_spec_source=query_spec_source,
        min_recall_at_1=args.min_recall_at_1,
        min_recall_at_3=args.min_recall_at_3,
        min_recall_at_5=args.min_recall_at_5,
        min_mrr=args.min_mrr,
        min_document_recall_at_1=args.min_document_recall_at_1,
        min_document_recall_at_3=args.min_document_recall_at_3,
        min_document_recall_at_5=args.min_document_recall_at_5,
        max_no_evidence_false_positive_rate=args.max_no_evidence_false_positive_rate,
        min_no_evidence_abstention_rate=args.min_no_evidence_abstention_rate,
        out_json=Path(args.out_json) if args.out_json else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_threshold and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


def _evaluate_query(
    *,
    settings: Any,
    auth: Any,
    spec: dict[str, Any],
    search_top_k: int,
    security_levels: list[str],
    department_ids: list[str],
    profile_id: str | None,
    default_as_of_date: str | None,
) -> dict[str, Any]:
    try:
        response = search_regulations(
            settings=settings,
            auth=auth,
            query=spec["query"],
            top_k=search_top_k,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=profile_id,
            as_of_date=spec.get("as_of_date") or default_as_of_date,
        )
        if not isinstance(response, dict):
            raise TypeError("search_regulations returned a non-object response.")
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise TypeError("search_regulations response results must be a list.")
        if not all(isinstance(result, dict) for result in raw_results):
            raise TypeError("search_regulations response results entries must be objects.")
        metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
        ranked_results = [
            _ranked_result(result, rank, spec)
            for rank, result in enumerate(raw_results, start=1)
        ]
        return _query_result(spec, ranked_results, metadata=metadata, error=None)
    except Exception as exc:
        return _query_result(
            spec,
            [],
            metadata={},
            error={"type": type(exc).__name__, "message": str(exc)},
        )


def _query_result(
    spec: dict[str, Any],
    ranked_results: list[dict[str, Any]],
    *,
    metadata: Mapping[str, Any],
    error: dict[str, str] | None,
) -> dict[str, Any]:
    expect_no_evidence = bool(spec["expect_no_evidence"])
    target_chunk_ids = list(spec["target_chunk_ids"])
    target_document_ids = list(spec["target_document_ids"])
    first_relevant_rank = next(
        (int(result["rank"]) for result in ranked_results if result["primary_target_match"]),
        None,
    )
    item: dict[str, Any] = {
        "id": spec["id"],
        "query": spec["query"],
        "expect_no_evidence": expect_no_evidence,
        "query_spec_valid": True,
        "runtime_target_available": True,
        "missing_target_chunk_ids": [],
        "missing_target_document_ids": [],
        "target_chunk_id": target_chunk_ids[0] if len(target_chunk_ids) == 1 else None,
        "target_chunk_ids": target_chunk_ids,
        "target_document_id": (
            target_document_ids[0] if len(target_document_ids) == 1 else None
        ),
        "target_document_ids": target_document_ids,
        "search_succeeded": error is None,
        "search_error": error,
        "result_count": len(ranked_results),
        "first_relevant_rank": first_relevant_rank,
        "reciprocal_rank": (
            round(1.0 / first_relevant_rank, 6)
            if first_relevant_rank is not None
            else (None if expect_no_evidence else 0.0)
        ),
        "no_evidence_false_positive": bool(expect_no_evidence and error is None and ranked_results),
        "no_evidence_abstained": bool(expect_no_evidence and error is None and not ranked_results),
        "trace_id": metadata.get("trace_id"),
        "retrieval_strategy": metadata.get("retrieval_strategy"),
        "trace": _trace_summary(metadata),
        "results": ranked_results,
    }
    for cutoff in EVALUATION_CUTOFFS:
        chunk_recall = _recall_at(
            ranked_results,
            target_chunk_ids,
            identity_field="chunk_id",
            cutoff=cutoff,
        )
        document_recall = _recall_at(
            ranked_results,
            target_document_ids,
            identity_field="document_id",
            cutoff=cutoff,
        )
        primary_recall = chunk_recall if target_chunk_ids else document_recall
        item[f"recall_at_{cutoff}"] = None if expect_no_evidence else (primary_recall or 0.0)
        item[f"chunk_recall_at_{cutoff}"] = chunk_recall
        item[f"document_recall_at_{cutoff}"] = document_recall
    return item


def _invalid_runtime_target_result(
    spec: dict[str, Any],
    *,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    item = _query_result(
        spec,
        [],
        metadata={},
        error=None,
    )
    item.update(
        {
            "query_spec_valid": False,
            "runtime_target_available": False,
            "missing_target_chunk_ids": list(validation.get("missing_target_chunk_ids") or []),
            "missing_target_document_ids": list(validation.get("missing_target_document_ids") or []),
            "search_succeeded": False,
            "search_error": None,
        }
    )
    return item


def _ranked_result(result: Mapping[str, Any], rank: int, spec: Mapping[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    verbatim = result.get("verbatim") if isinstance(result.get("verbatim"), dict) else {}
    chunk_id = _first_text(result.get("chunk_id"), metadata.get("chunk_id"), verbatim.get("chunk_id"))
    document_id = _first_text(
        result.get("document_id"),
        metadata.get("document_id"),
        verbatim.get("document_id"),
    )
    target_chunk_ids = set(spec.get("target_chunk_ids") or [])
    target_document_ids = set(spec.get("target_document_ids") or [])
    chunk_match = bool(chunk_id and chunk_id in target_chunk_ids)
    document_match = bool(document_id and document_id in target_document_ids)
    return {
        "rank": rank,
        "id": str(result.get("id") or ""),
        "title": str(result.get("title") or ""),
        "chunk_id": chunk_id,
        "document_id": document_id,
        "chunk_target_match": chunk_match,
        "document_target_match": document_match,
        "primary_target_match": chunk_match if target_chunk_ids else document_match,
    }


def _trace_summary(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": metadata.get("trace_id"),
        "retrieval_strategy": metadata.get("retrieval_strategy"),
        "result_count": metadata.get("result_count"),
        "timing_ms": metadata.get("timing_ms") if isinstance(metadata.get("timing_ms"), dict) else {},
        "lifecycle_selection": (
            metadata.get("lifecycle_selection")
            if isinstance(metadata.get("lifecycle_selection"), dict)
            else {}
        ),
        "refused": bool(metadata.get("refused")),
        "refusal_reason": metadata.get("refusal_reason") or metadata.get("reason"),
    }


def _recall_at(
    ranked_results: list[dict[str, Any]],
    target_ids: list[str],
    *,
    identity_field: str,
    cutoff: int,
) -> float | None:
    if not target_ids:
        return None
    expected = set(target_ids)
    retrieved = {
        str(result.get(identity_field) or "")
        for result in ranked_results[:cutoff]
        if str(result.get(identity_field) or "") in expected
    }
    return round(len(retrieved) / len(expected), 6)


def _summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid_results = [item for item in results if item.get("query_spec_valid", True)]
    answerable = [item for item in valid_results if not item["expect_no_evidence"]]
    chunk_targeted = [item for item in answerable if item["target_chunk_ids"]]
    document_targeted = [item for item in answerable if item["target_document_ids"]]
    no_evidence = [item for item in valid_results if item["expect_no_evidence"]]
    false_positive_count = sum(bool(item["no_evidence_false_positive"]) for item in no_evidence)
    abstention_count = sum(bool(item["no_evidence_abstained"]) for item in no_evidence)
    summary: dict[str, Any] = {
        "valid_query_spec_count": len(valid_results),
        "invalid_query_spec_count": len(results) - len(valid_results),
        "answerable_query_count": len(answerable),
        "chunk_target_query_count": len(chunk_targeted),
        "document_target_query_count": len(document_targeted),
        "no_evidence_query_count": len(no_evidence),
        "expect_no_evidence_query_count": len(no_evidence),
        "search_error_count": sum(not item["search_succeeded"] for item in valid_results),
        "mrr": _mean([float(item["reciprocal_rank"] or 0.0) for item in answerable]),
        "mean_reciprocal_rank": _mean(
            [float(item["reciprocal_rank"] or 0.0) for item in answerable]
        ),
        "no_evidence_false_positive_count": false_positive_count,
        "no_evidence_false_positive_rate": _ratio(false_positive_count, len(no_evidence)),
        "no_evidence_abstention_count": abstention_count,
        "no_evidence_abstention_rate": _ratio(abstention_count, len(no_evidence)),
    }
    for cutoff in EVALUATION_CUTOFFS:
        summary[f"recall_at_{cutoff}"] = _mean(
            [float(item[f"recall_at_{cutoff}"] or 0.0) for item in answerable]
        )
        summary[f"chunk_recall_at_{cutoff}"] = _mean(
            [float(item[f"chunk_recall_at_{cutoff}"] or 0.0) for item in chunk_targeted]
        )
        summary[f"document_recall_at_{cutoff}"] = _mean(
            [float(item[f"document_recall_at_{cutoff}"] or 0.0) for item in document_targeted]
        )
    return summary


def _thresholds(
    *,
    min_recall_at_1: float | None,
    min_recall_at_3: float | None,
    min_recall_at_5: float | None,
    min_mrr: float | None,
    min_document_recall_at_1: float | None,
    min_document_recall_at_3: float | None,
    min_document_recall_at_5: float | None,
    max_no_evidence_false_positive_rate: float | None,
    min_no_evidence_abstention_rate: float | None,
) -> dict[str, float | None]:
    values = {
        "min_recall_at_1": min_recall_at_1,
        "min_recall_at_3": min_recall_at_3,
        "min_recall_at_5": min_recall_at_5,
        "min_mrr": min_mrr,
        "min_document_recall_at_1": min_document_recall_at_1,
        "min_document_recall_at_3": min_document_recall_at_3,
        "min_document_recall_at_5": min_document_recall_at_5,
        "max_no_evidence_false_positive_rate": max_no_evidence_false_positive_rate,
        "min_no_evidence_abstention_rate": min_no_evidence_abstention_rate,
    }
    for name, value in values.items():
        if value is not None and not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1.")
    return {name: (None if value is None else float(value)) for name, value in values.items()}


def _threshold_findings(
    summary: Mapping[str, Any],
    thresholds: Mapping[str, float | None],
) -> list[dict[str, Any]]:
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
    findings: list[dict[str, Any]] = []
    for threshold_name, metric_name, count_name, comparison in checks:
        threshold = thresholds.get(threshold_name)
        if threshold is None:
            continue
        if int(summary.get(count_name) or 0) <= 0:
            findings.append(
                {
                    "code": "retrieval-quality-metric-unavailable",
                    "metric": metric_name,
                    "threshold": threshold,
                    "required_query_count_field": count_name,
                }
            )
            continue
        actual = float(summary.get(metric_name) or 0.0)
        failed = actual < threshold if comparison == "minimum" else actual > threshold
        if failed:
            findings.append(
                {
                    "code": "retrieval-quality-threshold-not-met",
                    "metric": metric_name,
                    "comparison": comparison,
                    "actual": actual,
                    "threshold": threshold,
                }
            )
    return findings


def _search_error_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "code": "mcp-search-error",
            "query_id": item["id"],
            "query": item["query"],
            "error": item["search_error"],
        }
        for item in results
        if not item["search_succeeded"] and item.get("query_spec_valid", True)
    ]


def _runtime_target_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "code": "query-spec-target-missing-from-runtime",
            "query_id": item["id"],
            "query": item["query"],
            "missing_target_chunk_ids": item.get("missing_target_chunk_ids") or [],
            "missing_target_document_ids": item.get("missing_target_document_ids") or [],
        }
        for item in results
        if not item.get("query_spec_valid", True)
    ]


def _runtime_target_validations(
    *,
    settings: Any,
    auth: Any | None = None,
    tenant_id: str,
    query_specs: list[dict[str, Any]],
    profile_id: str | None,
    default_as_of_date: str | None,
) -> list[dict[str, Any]]:
    requested_chunk_ids = {
        identifier
        for spec in query_specs
        for identifier in spec.get("target_chunk_ids") or []
    }
    requested_document_ids = {
        identifier
        for spec in query_specs
        for identifier in spec.get("target_document_ids") or []
    }
    if not requested_chunk_ids and not requested_document_ids:
        return [
            {
                "id": spec["id"],
                "valid": True,
                "missing_target_chunk_ids": [],
                "missing_target_document_ids": [],
            }
            for spec in query_specs
        ]
    data_dir = getattr(settings, "data_dir", None)
    as_of_dates = {
        _optional_text(spec.get("as_of_date")) or _optional_text(default_as_of_date)
        for spec in query_specs
    }
    runtime_targets = (
        _verified_runtime_target_ids(
            settings=settings,
            auth=auth or mcp_auth_context(tenant_id=tenant_id),
            profile_id=profile_id,
            requested_chunk_ids=requested_chunk_ids,
            requested_document_ids=requested_document_ids,
            as_of_dates=as_of_dates,
        )
        if data_dir is not None
        else None
    )
    validations: list[dict[str, Any]] = []
    for spec in query_specs:
        scoped_as_of = _optional_text(spec.get("as_of_date")) or _optional_text(
            default_as_of_date
        )
        found_chunk_ids, found_document_ids = (
            runtime_targets.get(scoped_as_of, (set(), set()))
            if runtime_targets is not None
            else (set(), set())
        )
        missing_chunk_ids = [
            identifier for identifier in spec.get("target_chunk_ids") or [] if identifier not in found_chunk_ids
        ]
        missing_document_ids = [
            identifier
            for identifier in spec.get("target_document_ids") or []
            if identifier not in found_document_ids
        ]
        validations.append(
            {
                "id": spec["id"],
                "valid": not missing_chunk_ids and not missing_document_ids,
                "missing_target_chunk_ids": missing_chunk_ids,
                "missing_target_document_ids": missing_document_ids,
            }
        )
    return validations


def _target_ids(spec: Mapping[str, Any], singular_key: str, plural_key: str) -> list[str]:
    values: list[Any] = []
    for key in (singular_key, plural_key):
        value = spec.get(key)
        if isinstance(value, set):
            values.extend(sorted(value, key=lambda item: str(item)))
        elif isinstance(value, (list, tuple)):
            values.extend(value)
        elif value is not None:
            values.append(value)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        identifier = str(value or "").strip()
        if identifier and identifier not in seen:
            normalized.append(identifier)
            seen.add(identifier)
    return normalized


def _query_spec_fingerprint(
    query_specs: list[dict[str, Any]],
    source: Path | None,
    *,
    item_count: int,
) -> dict[str, Any]:
    if source is not None:
        payload = source.read_bytes()
        basis = "source_file"
        path = str(source)
    else:
        payload = json.dumps(
            query_specs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_fingerprint_default,
        ).encode("utf-8")
        basis = "canonical_json"
        path = None
    return {
        "query_spec_path": path,
        "query_spec_fingerprint_basis": basis,
        "query_spec_byte_count": len(payload),
        "query_spec_sha256": hashlib.sha256(payload).hexdigest(),
        "query_spec_item_count": item_count,
    }


def _normalize_top_k(value: int) -> tuple[int, int]:
    requested = int(value)
    if requested < 1:
        raise ValueError("top_k must be greater than zero.")
    return requested, min(MAX_MCP_TOP_K, max(max(EVALUATION_CUTOFFS), requested))


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _json_fingerprint_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value, key=lambda item: str(item))
    raise TypeError(f"Query specification value is not JSON serializable: {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
