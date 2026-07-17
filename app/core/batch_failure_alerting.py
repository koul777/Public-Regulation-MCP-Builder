from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


PUBLIC_ITEM_FIELDS = (
    "filename",
    "document_name",
    "document_id",
    "institution_name",
    "source_system",
    "source_record_id",
    "source_file_id",
    "profile_id",
    "failure_category",
    "ocr_required",
    "ocr_page_count",
    "retry_recommended",
    "failure_next_action",
)

INTERNAL_ITEM_FIELDS = PUBLIC_ITEM_FIELDS + ("input_path",)

SOURCE_SELECTION_WARNING_FIELDS = (
    "filename",
    "document_id",
    "institution_name",
    "apba_id",
    "profile_id",
    "source_record_id",
    "source_file_id",
    "selection_warning",
    "selection_policy",
    "selected_latest_file",
    "latest_file_no",
    "latest_file_name",
    "latest_file_ext",
)


def build_failure_alert(
    batch_report: dict[str, Any],
    *,
    batch_report_file: str = "",
    readiness_report: dict[str, Any] | None = None,
    include_local_paths: bool = False,
    max_items: int = 50,
) -> dict[str, Any]:
    failed_rows = [row for row in batch_report.get("rows", []) or [] if row.get("status") == "failed"]
    items = [
        _alert_item(row, include_local_paths=include_local_paths)
        for row in failed_rows[: max(0, max_items)]
    ]
    readiness_status = readiness_report.get("status") if readiness_report else None
    readiness_passed = readiness_report.get("passed") if readiness_report else None
    failed_count = int(batch_report.get("failed_count", len(failed_rows)) or 0)
    ocr_required_count = int(batch_report.get("ocr_required_count", 0) or 0)
    retry_recommended_failed_count = int(batch_report.get("retry_recommended_failed_count", 0) or 0)
    failure_category_counts = dict(batch_report.get("failure_category_counts", {}) or {})
    source_selection_warning_count, source_selection_warning_samples = _source_selection_warning_summary(
        readiness_report
    )
    needs_attention = failed_count > 0 or readiness_passed is False
    status = "needs_attention" if needs_attention else "ok"
    severity = _severity(
        failed_count=failed_count,
        failure_category_counts=failure_category_counts,
        readiness_report=readiness_report,
    )
    return {
        "report_type": "batch_failure_alert",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "severity": severity,
        "mode": "alert_only",
        "api_call_count": 0,
        "source_batch_report_file": batch_report_file,
        "source_batch_generated_at": batch_report.get("generated_at"),
        "source_readiness_status": readiness_status,
        "summary": {
            "input_count": batch_report.get("input_count", 0),
            "successful_count": batch_report.get("successful_count", 0),
            "failed_count": failed_count,
            "failure_category_counts": failure_category_counts,
            "ocr_required_count": ocr_required_count,
            "ocr_required_page_count": batch_report.get("ocr_required_page_count", 0),
            "retry_recommended_failed_count": retry_recommended_failed_count,
            "readiness_status": readiness_status,
            "readiness_passed": readiness_passed,
            "source_selection_warning_count": source_selection_warning_count,
        },
        "source_selection_warning_samples": source_selection_warning_samples,
        "recommended_actions": _recommended_actions(
            batch_report_file=batch_report_file,
            failed_count=failed_count,
            ocr_required_count=ocr_required_count,
            retry_recommended_failed_count=retry_recommended_failed_count,
            readiness_passed=readiness_passed,
            source_selection_warning_count=source_selection_warning_count,
        ),
        "items": items,
        "truncated_item_count": max(0, len(failed_rows) - len(items)),
    }


def _alert_item(row: dict[str, Any], *, include_local_paths: bool) -> dict[str, Any]:
    fields = INTERNAL_ITEM_FIELDS if include_local_paths else PUBLIC_ITEM_FIELDS
    item = {field: row.get(field, "") for field in fields}
    item["error_summary"] = _error_summary(str(row.get("error") or ""))
    if not include_local_paths:
        item.pop("input_path", None)
    return item


def _error_summary(error: str, *, limit: int = 240) -> str:
    cleaned = " ".join(error.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _source_selection_warning_summary(
    readiness_report: dict[str, Any] | None,
    *,
    sample_limit: int = 3,
) -> tuple[int, list[dict[str, Any]]]:
    if not readiness_report:
        return 0, []
    failures = readiness_report.get("failures", {}) or {}
    warning_rows = failures.get("source_selection_warnings", []) or []
    warning_count = _readiness_check_detail_count(
        readiness_report,
        check_name="source_selection_has_no_warnings",
        detail_name="warning_count",
    )
    if warning_count is None:
        warning_count = len(warning_rows)
    samples = [
        {field: row.get(field, "") for field in SOURCE_SELECTION_WARNING_FIELDS}
        for row in warning_rows[: max(0, sample_limit)]
    ]
    return warning_count, samples


def _readiness_check_detail_count(
    readiness_report: dict[str, Any],
    *,
    check_name: str,
    detail_name: str,
) -> int | None:
    for check in readiness_report.get("checks", []) or []:
        if check.get("name") != check_name:
            continue
        details = check.get("details", {}) or {}
        value = details.get(detail_name)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _severity(
    *,
    failed_count: int,
    failure_category_counts: dict[str, int],
    readiness_report: dict[str, Any] | None,
) -> str:
    if failed_count == 0 and (readiness_report is None or readiness_report.get("passed") is not False):
        return "info"
    non_ocr_failures = {
        category: count
        for category, count in failure_category_counts.items()
        if category and category != "ocr_required"
    }
    if non_ocr_failures:
        return "critical"
    if readiness_report and readiness_report.get("passed") is False and not _ocr_only_readiness_failure(readiness_report):
        return "critical"
    return "warning"


OCR_ONLY_READINESS_FAILURES = {
    "all_inputs_successful",
    "no_failed_rows",
    "no_ocr_required_rows",
    "average_quality_at_or_above_minimum",
}


def _ocr_only_readiness_failure(readiness_report: dict[str, Any]) -> bool:
    failed_checks = {
        check.get("name")
        for check in readiness_report.get("checks", []) or []
        if not check.get("passed")
    }
    return failed_checks.issubset(OCR_ONLY_READINESS_FAILURES)


def _recommended_actions(
    *,
    batch_report_file: str,
    failed_count: int,
    ocr_required_count: int,
    retry_recommended_failed_count: int,
    readiness_passed: bool | None,
    source_selection_warning_count: int,
) -> list[dict[str, Any]]:
    if failed_count == 0 and readiness_passed is not False:
        return []
    actions: list[dict[str, Any]] = []
    if ocr_required_count > 0:
        actions.append(
            {
                "action_type": "export_ocr_manifest",
                "target_count": ocr_required_count,
                "command": (
                    "python scripts/export_ocr_manifest.py "
                    f"--batch-report reports/{batch_report_file}"
                ),
            }
        )
    if retry_recommended_failed_count > 0:
        actions.append(
            {
                "action_type": "export_retry_manifest",
                "target_count": retry_recommended_failed_count,
                "command": (
                    "python scripts/export_batch_retry_manifest.py "
                    f"--batch-report reports/{batch_report_file} --require-existing-files"
                ),
            }
        )
    if source_selection_warning_count > 0:
        actions.append(
            {
                "action_type": "review_source_selection_warnings",
                "target_count": source_selection_warning_count,
                "command": (
                    "python scripts/validate_public_batch_readiness.py "
                    f"--batch-report reports/{batch_report_file}"
                ),
            }
        )
    if readiness_passed is False:
        actions.append(
            {
                "action_type": "review_public_batch_readiness",
                "target_count": 1,
                "command": (
                    "python scripts/validate_public_batch_readiness.py "
                    f"--batch-report reports/{batch_report_file}"
                ),
            }
        )
    if not actions and failed_count > 0:
        actions.append(
            {
                "action_type": "review_failed_rows",
                "target_count": failed_count,
                "command": f"Inspect reports/{batch_report_file}",
            }
        )
    return actions
