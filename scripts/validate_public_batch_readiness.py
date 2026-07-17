from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.institution_profiles import InstitutionProfileRegistry, load_institution_profile_registry
from scripts.embedding_readiness import evaluate_embedding_readiness
from scripts.report_metadata import current_repo_commit

DEFAULT_ARTIFACT_FIELDS = ("quality_json", "quality_md", "tables_csv", "tables_jsonl")
DEFAULT_MIN_AVERAGE_QUALITY = 98.0
REUSED_ZERO_FIELDS = (
    "agent_review_candidate_count",
    "agent_review_selected_count",
    "agent_review_estimated_input_tokens",
    "agent_review_estimated_output_tokens",
    "agent_review_estimated_total_tokens",
)
REUSED_EMPTY_EVIDENCE_FIELDS = (
    "agent_review_budget_reservation_id",
    "agent_review_approval_reference",
    "agent_review_model",
    "agent_review_payload_hash",
    "agent_review_estimated_cost",
    "agent_review_actual_cost",
    "agent_review_provider_request_id",
    "agent_review_plan_json",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_public_batch_readiness(
    report: dict[str, Any],
    *,
    batch_report_path: Path | None = None,
    min_average_quality: float = DEFAULT_MIN_AVERAGE_QUALITY,
    max_failed_info: int = 0,
    max_recommendations: int = 0,
    max_table_attention: int = 0,
    max_current_ai_tokens: int = 0,
    required_row_fields: list[str] | None = None,
    required_artifact_fields: list[str] | None = None,
    institution_profile_registry: InstitutionProfileRegistry | None = None,
    strict_institution_profiles: bool = False,
    embedding_cost_estimates: list[dict[str, Any]] | None = None,
    require_semantic_embedding_approval: bool = False,
    embedding_approval_reference: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    rows = report.get("rows", []) or []
    successful_rows = [row for row in rows if row.get("status") in {"completed", "skipped_unchanged"}]
    required_row_fields = required_row_fields or []
    required_artifact_fields = required_artifact_fields or list(DEFAULT_ARTIFACT_FIELDS)
    missing_required_fields = _missing_required_fields(successful_rows, required_row_fields, sample_limit)
    missing_profile_required_fields = _missing_profile_required_fields(
        successful_rows,
        institution_profile_registry,
        strict_institution_profiles,
        sample_limit,
    )
    missing_required_fields.extend(missing_profile_required_fields)
    missing_artifacts = _missing_artifacts(successful_rows, required_artifact_fields, batch_report_path, sample_limit)
    reused_ai_leaks = _reused_ai_evidence_leaks(successful_rows, sample_limit)
    failed_row_samples = _failed_row_samples(rows, sample_limit)
    ocr_required_rows = _ocr_required_row_samples(rows, sample_limit)
    failed_info_rows = _counted_row_samples(
        successful_rows,
        "failed_info_check_count",
        sample_limit,
        extra_fields=("top_failed_info_check",),
    )
    recommendation_rows = _counted_row_samples(
        successful_rows,
        "recommendation_count",
        sample_limit,
        extra_fields=("top_recommendation",),
    )
    source_selection_warning_rows = _source_selection_warning_rows(successful_rows, sample_limit)
    embedding_readiness = evaluate_embedding_readiness(
        embedding_cost_estimates,
        require_semantic_provider_approval=require_semantic_embedding_approval,
        approval_reference=embedding_approval_reference,
    )

    input_count = _to_int(report.get("input_count"))
    successful_count = _to_int(report.get("successful_count"))
    failed_count = _to_int(report.get("failed_count"))
    ocr_required_count = _to_int(report.get("ocr_required_count"))
    ocr_required_page_count = _to_int(report.get("ocr_required_page_count"))
    retry_recommended_failed_count = _to_int(report.get("retry_recommended_failed_count"))
    quality_passed_count = _to_int(report.get("quality_passed_count"))
    failed_info_total = _to_int(report.get("failed_info_check_total"))
    recommendation_total = _to_int(report.get("recommendation_total"))
    table_attention_total = _to_int(report.get("table_false_positive_attention_total"))
    current_ai_tokens = _to_int(report.get("agent_review_estimated_total_tokens_total"))
    average_quality = _to_float(report.get("average_quality_score"))

    checks = [
        _check("batch_has_inputs", input_count > 0, {"input_count": input_count}),
        _check(
            "all_inputs_successful",
            input_count > 0 and successful_count == input_count,
            {"input_count": input_count, "successful_count": successful_count},
        ),
        _check("no_failed_rows", failed_count == 0, {"failed_count": failed_count}),
        _check(
            "no_ocr_required_rows",
            ocr_required_count == 0,
            {"ocr_required_count": ocr_required_count, "ocr_required_page_count": ocr_required_page_count},
        ),
        _check(
            "all_successful_rows_quality_passed",
            quality_passed_count == successful_count,
            {"quality_passed_count": quality_passed_count, "successful_count": successful_count},
        ),
        _check(
            "average_quality_at_or_above_minimum",
            average_quality >= min_average_quality,
            {"average_quality_score": average_quality, "minimum": min_average_quality},
        ),
        _check(
            "failed_info_checks_within_limit",
            failed_info_total <= max_failed_info,
            {"failed_info_check_total": failed_info_total, "maximum": max_failed_info},
        ),
        _check(
            "recommendations_within_limit",
            recommendation_total <= max_recommendations,
            {"recommendation_total": recommendation_total, "maximum": max_recommendations},
        ),
        _check(
            "table_attention_within_limit",
            table_attention_total <= max_table_attention,
            {"table_false_positive_attention_total": table_attention_total, "maximum": max_table_attention},
        ),
        _check(
            "current_ai_tokens_within_limit",
            current_ai_tokens <= max_current_ai_tokens,
            {"agent_review_estimated_total_tokens_total": current_ai_tokens, "maximum": max_current_ai_tokens},
        ),
        _check(
            "agent_review_batch_budget_not_exceeded",
            report.get("agent_review_batch_budget_exceeded") is not True,
            {"agent_review_batch_budget_exceeded": report.get("agent_review_batch_budget_exceeded")},
        ),
        _check(
            "required_row_fields_present",
            not missing_required_fields,
            {
                "required_row_fields": required_row_fields,
                "institution_profile_registry": institution_profile_registry.summary()
                if institution_profile_registry
                else None,
                "missing_count": len(missing_required_fields),
            },
        ),
        _check(
            "required_artifacts_exist",
            not missing_artifacts,
            {"required_artifact_fields": required_artifact_fields, "missing_count": len(missing_artifacts)},
        ),
        _check(
            "reused_rows_clear_current_ai_evidence",
            not reused_ai_leaks,
            {"leak_count": len(reused_ai_leaks)},
        ),
        _check(
            "source_selection_has_no_warnings",
            not source_selection_warning_rows,
            {"warning_count": len(source_selection_warning_rows)},
        ),
    ]
    checks.extend(embedding_readiness["checks"])
    passed = all(item["passed"] for item in checks)
    thresholds = _readiness_thresholds(
        min_average_quality=min_average_quality,
        max_failed_info=max_failed_info,
        max_recommendations=max_recommendations,
        max_table_attention=max_table_attention,
        max_current_ai_tokens=max_current_ai_tokens,
        required_row_fields=required_row_fields,
        required_artifact_fields=required_artifact_fields,
        strict_institution_profiles=strict_institution_profiles,
        require_semantic_embedding_approval=require_semantic_embedding_approval,
    )
    readiness_profile = _readiness_profile(thresholds)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_from": report.get("generated_at", ""),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "readiness_profile": readiness_profile,
        "strict_release_evidence": passed and readiness_profile == "strict",
        "thresholds": thresholds,
        "status": "public_batch_ready" if passed else "needs_attention",
        "passed": passed,
        "summary": {
            "input_count": input_count,
            "successful_count": successful_count,
            "failed_count": failed_count,
            "failure_category_counts": report.get("failure_category_counts", {}),
            "ocr_required_count": ocr_required_count,
            "ocr_required_page_count": ocr_required_page_count,
            "retry_recommended_failed_count": retry_recommended_failed_count,
            "quality_passed_count": quality_passed_count,
            "average_quality_score": average_quality,
            "failed_info_check_total": failed_info_total,
            "recommendation_total": recommendation_total,
            "table_false_positive_attention_total": table_attention_total,
            "agent_review_estimated_total_tokens_total": current_ai_tokens,
            "agent_review_batch_budget_exceeded": report.get("agent_review_batch_budget_exceeded") is True,
            **embedding_readiness["summary"],
        },
        "checks": checks,
        "failures": {
            "failed_rows": failed_row_samples,
            "ocr_required_rows": ocr_required_rows,
            "failed_info_check_rows": failed_info_rows,
            "recommendation_rows": recommendation_rows,
            "missing_required_fields": missing_required_fields,
            "missing_artifacts": missing_artifacts,
            "reused_ai_evidence_leaks": reused_ai_leaks,
            "source_selection_warnings": source_selection_warning_rows,
            "embedding_readiness": embedding_readiness["failures"],
        },
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Public Batch Readiness",
        "",
        f"- Status: {report['status']}",
        f"- Passed: {report['passed']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in report["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Checks", ""])
    for item in report["checks"]:
        status = "PASS" if item["passed"] else "FAIL"
        lines.append(f"- {status}: {item['name']} ({json.dumps(item['details'], ensure_ascii=False, sort_keys=True)})")
    for failure_group, failures in report["failures"].items():
        if not failures:
            continue
        lines.extend(["", f"## {failure_group}", ""])
        for failure in failures:
            lines.append(f"- {json.dumps(failure, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n"


def _check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "details": details}


def _readiness_thresholds(
    *,
    min_average_quality: float,
    max_failed_info: int,
    max_recommendations: int,
    max_table_attention: int,
    max_current_ai_tokens: int,
    required_row_fields: list[str],
    required_artifact_fields: list[str],
    strict_institution_profiles: bool,
    require_semantic_embedding_approval: bool,
) -> dict[str, Any]:
    return {
        "min_average_quality": min_average_quality,
        "max_failed_info": max_failed_info,
        "max_recommendations": max_recommendations,
        "max_table_attention": max_table_attention,
        "max_current_ai_tokens": max_current_ai_tokens,
        "required_row_fields": list(required_row_fields),
        "required_artifact_fields": list(required_artifact_fields),
        "strict_institution_profiles": strict_institution_profiles,
        "require_semantic_embedding_approval": require_semantic_embedding_approval,
    }


def _readiness_profile(thresholds: dict[str, Any]) -> str:
    has_review_tolerance = any(
        _to_int(thresholds.get(key)) > 0
        for key in (
            "max_failed_info",
            "max_recommendations",
            "max_table_attention",
            "max_current_ai_tokens",
        )
    )
    if _to_float(thresholds.get("min_average_quality")) < DEFAULT_MIN_AVERAGE_QUALITY:
        has_review_tolerance = True
    return "review_tolerance" if has_review_tolerance else "strict"


def _missing_required_fields(
    rows: list[dict[str, Any]], required_fields: list[str], sample_limit: int
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        missing = [field for field in required_fields if row.get(field) in (None, "")]
        if missing:
            failures.append(_row_failure(row, {"missing_fields": missing}))
        if len(failures) >= sample_limit:
            break
    return failures


def _missing_profile_required_fields(
    rows: list[dict[str, Any]],
    registry: InstitutionProfileRegistry | None,
    strict: bool,
    sample_limit: int,
) -> list[dict[str, Any]]:
    if registry is None:
        return []
    failures: list[dict[str, Any]] = []
    for row in rows:
        profile_id = row.get("profile_id")
        try:
            required_fields = registry.required_row_fields_for(profile_id, strict=strict)
        except ValueError as exc:
            failures.append(_row_failure(row, {"profile_id": profile_id, "profile_error": str(exc)}))
            if len(failures) >= sample_limit:
                break
            continue
        missing = [field for field in required_fields if row.get(field) in (None, "")]
        if missing:
            failures.append(
                _row_failure(
                    row,
                    {
                        "profile_id": profile_id,
                        "missing_fields": missing,
                        "required_by": "institution_profile_registry",
                    },
                )
            )
        if len(failures) >= sample_limit:
            break
    return failures


def _missing_artifacts(
    rows: list[dict[str, Any]],
    artifact_fields: list[str],
    batch_report_path: Path | None,
    sample_limit: int,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        missing: list[dict[str, str]] = []
        for field in artifact_fields:
            raw_path = str(row.get(field) or "")
            if not raw_path:
                missing.append({"field": field, "path": raw_path})
                continue
            if not _artifact_exists(raw_path, batch_report_path):
                missing.append({"field": field, "path": raw_path})
        if missing:
            failures.append(_row_failure(row, {"missing_artifacts": missing}))
        if len(failures) >= sample_limit:
            break
    return failures


def _artifact_exists(raw_path: str, batch_report_path: Path | None) -> bool:
    path = Path(raw_path)
    if path.is_absolute():
        return path.is_file()
    candidates = [path, PROJECT_ROOT / path]
    if batch_report_path is not None:
        candidates.append(batch_report_path.resolve().parent / path)
    return any(candidate.is_file() for candidate in candidates)


def _reused_ai_evidence_leaks(rows: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "skipped_unchanged":
            continue
        leaked: dict[str, Any] = {}
        if row.get("agent_review_status") != "skipped":
            leaked["agent_review_status"] = row.get("agent_review_status")
        if row.get("agent_review_skip_reason") != "reused_unchanged":
            leaked["agent_review_skip_reason"] = row.get("agent_review_skip_reason")
        for field in REUSED_ZERO_FIELDS:
            if _to_int(row.get(field)) != 0:
                leaked[field] = row.get(field)
        if row.get("agent_review_budget_exhausted") is not False:
            leaked["agent_review_budget_exhausted"] = row.get("agent_review_budget_exhausted")
        for field in REUSED_EMPTY_EVIDENCE_FIELDS:
            if row.get(field) not in (None, ""):
                leaked[field] = row.get(field)
        if leaked:
            failures.append(_row_failure(row, {"leaked_fields": leaked}))
        if len(failures) >= sample_limit:
            break
    return failures


def _source_selection_warning_rows(rows: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        warning = str(row.get("selection_warning") or "").strip()
        if not warning:
            continue
        failures.append(
            _row_failure(
                row,
                {
                    "selection_warning": warning,
                    "selection_policy": row.get("selection_policy", ""),
                    "selected_latest_file": row.get("selected_latest_file", ""),
                    "latest_file_no": row.get("latest_file_no", ""),
                    "latest_file_name": row.get("latest_file_name", ""),
                    "latest_file_ext": row.get("latest_file_ext", ""),
                },
            )
        )
        if len(failures) >= sample_limit:
            break
    return failures


def _failed_row_samples(rows: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "failed":
            continue
        failures.append(
            _row_failure(
                row,
                {
                    "failure_category": row.get("failure_category", ""),
                    "error": row.get("error", ""),
                    "failure_next_action": row.get("failure_next_action", ""),
                    "retry_recommended": row.get("retry_recommended"),
                    "ocr_required": row.get("ocr_required"),
                    "ocr_page_count": row.get("ocr_page_count"),
                },
            )
        )
        if len(failures) >= sample_limit:
            break
    return failures


def _ocr_required_row_samples(rows: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        if not (row.get("ocr_required") is True or row.get("failure_category") == "ocr_required"):
            continue
        failures.append(
            _row_failure(
                row,
                {
                    "failure_category": row.get("failure_category", ""),
                    "error": row.get("error", ""),
                    "failure_next_action": row.get("failure_next_action", ""),
                    "ocr_page_count": row.get("ocr_page_count"),
                },
            )
        )
        if len(failures) >= sample_limit:
            break
    return failures


def _counted_row_samples(
    rows: list[dict[str, Any]],
    count_field: str,
    sample_limit: int,
    *,
    extra_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in rows:
        count = _to_int(row.get(count_field))
        if count <= 0:
            continue
        details: dict[str, Any] = {count_field: count}
        for field in extra_fields:
            value = row.get(field)
            if value not in (None, "", [], {}):
                details[field] = value
        failures.append(_row_failure(row, details))
        if len(failures) >= sample_limit:
            break
    return failures


def _row_failure(row: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": row.get("filename", ""),
        "input_path": row.get("input_path", ""),
        "document_id": row.get("document_id", ""),
        "institution_name": row.get("institution_name", ""),
        "apba_id": row.get("apba_id", ""),
        "profile_id": row.get("profile_id", ""),
        "source_record_id": row.get("source_record_id", ""),
        "source_file_id": row.get("source_file_id", ""),
        "status": row.get("status", ""),
        **details,
    }


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a public-institution batch quality report for rollout readiness.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--min-average-quality", type=float, default=DEFAULT_MIN_AVERAGE_QUALITY)
    parser.add_argument("--max-failed-info", type=int, default=0)
    parser.add_argument("--max-recommendations", type=int, default=0)
    parser.add_argument("--max-table-attention", type=int, default=0)
    parser.add_argument("--max-current-ai-tokens", type=int, default=0)
    parser.add_argument("--required-row-field", action="append", default=[])
    parser.add_argument("--required-artifact-field", action="append", default=list(DEFAULT_ARTIFACT_FIELDS))
    parser.add_argument("--institution-profiles", default=None, help="Institution profile registry JSON file.")
    parser.add_argument(
        "--strict-institution-profiles",
        action="store_true",
        help="Fail readiness if a successful row profile_id is absent from --institution-profiles.",
    )
    parser.add_argument("--embedding-cost-estimate", action="append", default=[])
    parser.add_argument("--require-semantic-embedding-approval", action="store_true")
    parser.add_argument("--embedding-approval-reference", default=None)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_report_path = Path(args.batch_report)
    institution_profiles = (
        load_institution_profile_registry(args.institution_profiles) if args.institution_profiles else None
    )
    report = validate_public_batch_readiness(
        load_json(batch_report_path),
        batch_report_path=batch_report_path,
        min_average_quality=args.min_average_quality,
        max_failed_info=args.max_failed_info,
        max_recommendations=args.max_recommendations,
        max_table_attention=args.max_table_attention,
        max_current_ai_tokens=args.max_current_ai_tokens,
        required_row_fields=args.required_row_field,
        required_artifact_fields=args.required_artifact_field,
        institution_profile_registry=institution_profiles,
        strict_institution_profiles=args.strict_institution_profiles,
        embedding_cost_estimates=[load_json(Path(path)) for path in args.embedding_cost_estimate],
        require_semantic_embedding_approval=args.require_semantic_embedding_approval,
        embedding_approval_reference=args.embedding_approval_reference,
        sample_limit=args.sample_limit,
    )
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out_md:
        Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_md).write_text(to_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
