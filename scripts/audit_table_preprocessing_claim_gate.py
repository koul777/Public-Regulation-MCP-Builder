from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


TABLE_BLOCKER_CATEGORY = "table_parentage_or_structure_review"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


def audit_table_preprocessing_claim_gate(
    *,
    table_unit_review_summary: Path,
    table_count_transfer_validation: Path,
    table_source_traceability_report: Path,
    answer_blocker_review_map: Path | None = None,
    table_drift_check_report: Path | None = None,
    require_table_drift_check: bool = False,
    out_json: Path | None = None,
    out_md: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    review_summary = _load_json_report(
        table_unit_review_summary,
        expected_report_type="parsing_goldset_table_unit_review_summary",
    )
    transfer_validation = _load_json_report(
        table_count_transfer_validation,
        expected_report_type="parsing_goldset_table_count_transfer_validation",
    )
    source_traceability = _load_json_report(
        table_source_traceability_report,
        expected_report_type="table_review_source_traceability",
    )
    answer_map = (
        _load_json_report(answer_blocker_review_map, expected_report_type="answer_blocker_review_map")
        if answer_blocker_review_map
        else {}
    )
    drift_check = (
        _load_json_report(table_drift_check_report, expected_report_type="parsing_goldset_table_drift_check")
        if table_drift_check_report
        else {}
    )
    drift_lineage = (
        _table_drift_source_report_lineage(
            drift_check=drift_check,
            drift_check_report=table_drift_check_report,
            table_unit_review_summary=table_unit_review_summary,
            table_count_transfer_validation=table_count_transfer_validation,
            table_source_traceability_report=table_source_traceability_report,
        )
        if drift_check and table_drift_check_report
        else {}
    )

    findings = _build_findings(
        review_summary=review_summary,
        transfer_validation=transfer_validation,
        source_traceability=source_traceability,
        answer_map=answer_map,
        drift_check=drift_check,
        drift_lineage=drift_lineage,
        require_table_drift_check=require_table_drift_check,
    )
    blocker_count = sum(1 for finding in findings if finding.get("severity") == "blocker")
    warning_count = sum(1 for finding in findings if finding.get("severity") == "warning")
    traceability_ready = (
        bool(source_traceability.get("traceability_passed"))
        and _int(source_traceability.get("blocked_batch_count")) == 0
        and _int(source_traceability.get("issue_count")) == 0
    )
    human_review_complete = (
        bool(review_summary.get("ready_for_table_score_transfer"))
        and _int(review_summary.get("pending_unit_count")) == 0
        and _int(review_summary.get("invalid_unit_count")) == 0
    )
    score_transfer_ready = (
        bool(transfer_validation.get("passed"))
        and _int(transfer_validation.get("blocker_count")) == 0
    )
    answer_map_present = bool(answer_map)
    table_answer_blocker_count = _table_answer_blocker_count(answer_map)
    answer_table_blockers_closed = answer_map_present and table_answer_blocker_count == 0
    drift_check_ready = (not require_table_drift_check and not drift_check) or (
        bool(drift_check)
        and bool(drift_check.get("passed"))
        and _int(drift_check.get("blocker_count")) == 0
        and bool(drift_lineage.get("matches"))
    )
    non_review_evidence_ready = bool(
        traceability_ready
        and drift_check_ready
        and answer_table_blockers_closed
    )
    passed = bool(
        traceability_ready
        and human_review_complete
        and score_transfer_ready
        and answer_table_blockers_closed
        and drift_check_ready
        and blocker_count == 0
    )
    status = _status(
        traceability_ready=traceability_ready,
        drift_check_ready=drift_check_ready,
        human_review_complete=human_review_complete,
        score_transfer_ready=score_transfer_ready,
        answer_table_blockers_closed=answer_table_blockers_closed,
        passed=passed,
    )
    release_blocked_by_human_review = bool(
        non_review_evidence_ready
        and status == "blocked_pending_human_review"
    )
    report = {
        "report_type": "table_preprocessing_claim_gate",
        "generated_at": generated_at,
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": passed,
        "status": status,
        "claim_level": "quality_claim_ready" if passed else "review_ready_not_accuracy_proven",
        "feasibility_status": "feasible_with_human_review" if traceability_ready and drift_check_ready else "blocked_before_review",
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "source_report_artifacts": _source_artifacts(
            table_unit_review_summary=table_unit_review_summary,
            table_count_transfer_validation=table_count_transfer_validation,
            table_source_traceability_report=table_source_traceability_report,
            answer_blocker_review_map=answer_blocker_review_map,
            table_drift_check_report=table_drift_check_report,
        ),
        "summary": {
            "source_compare_only": bool(review_summary.get("source_compare_only")),
            "document_count": _int(review_summary.get("document_count")),
            "selected_unit_count": _int(review_summary.get("selected_unit_count")),
            "completed_unit_count": _int(review_summary.get("completed_unit_count")),
            "pending_unit_count": _int(review_summary.get("pending_unit_count")),
            "invalid_unit_count": _int(review_summary.get("invalid_unit_count")),
            "required_field_missing_total": _int(review_summary.get("required_field_missing_total")),
            "required_field_missing_counts": _dict(review_summary.get("required_field_missing_counts")),
            "review_priority_counts": _dict(review_summary.get("review_priority_counts")),
            "label_review_flag_counts": _dict(review_summary.get("label_review_flag_counts")),
            "ready_for_table_score_transfer": bool(review_summary.get("ready_for_table_score_transfer")),
            "transfer_passed": bool(transfer_validation.get("passed")),
            "transfer_blocker_count": _int(transfer_validation.get("blocker_count")),
            "transfer_finding_code_counts": _dict(transfer_validation.get("finding_code_counts")),
            "transfer_root_cause_summary": _dict(transfer_validation.get("root_cause_summary")),
            "source_traceability_passed": bool(source_traceability.get("traceability_passed")),
            "source_traceability_issue_count": _int(source_traceability.get("issue_count")),
            "source_traceability_issue_counts": _dict(source_traceability.get("issue_counts")),
            "source_traceability_blocked_record_count": _int(source_traceability.get("blocked_batch_count")),
            "source_traceability_record_count": _int(
                source_traceability.get("record_count", source_traceability.get("batch_count"))
            ),
            "source_traceability_require_page_count_verification": bool(
                source_traceability.get("require_page_count_verification")
            ),
            "source_page_count_status_counts": _dict(source_traceability.get("page_count_status_counts")),
            "source_format_status_counts": _dict(source_traceability.get("source_format_status_counts")),
            "source_traceability_operator_next_action_counts": _dict(
                source_traceability.get("operator_next_action_counts")
            ),
            "drift_check_present": bool(drift_check),
            "drift_check_passed": bool(drift_check.get("passed")) if drift_check else None,
            "drift_check_blocker_count": _int(drift_check.get("blocker_count")) if drift_check else None,
            "drift_check_source_reports_match": (
                bool(drift_lineage.get("matches")) if drift_check else None
            ),
            "drift_check_lineage_mismatch_count": (
                len(drift_lineage.get("mismatches") or []) if drift_check else None
            ),
            "answer_blocker_map_present": answer_map_present,
            "answer_query_count": _int(answer_map.get("query_count")) if answer_map else None,
            "answer_failed_query_count": _int(answer_map.get("failed_query_count")) if answer_map else None,
            "answer_quality_issue_count": _int(answer_map.get("quality_issue_count")) if answer_map else None,
            "table_answer_blocker_count": table_answer_blocker_count if answer_map else None,
            "non_review_evidence_ready": non_review_evidence_ready,
            "release_blocked_by_human_review": release_blocked_by_human_review,
        },
        "finding_code_counts": dict(Counter(str(finding.get("code") or "") for finding in findings)),
        "findings": findings,
        "next_steps": _next_steps(
            traceability_ready=traceability_ready,
            drift_check_ready=drift_check_ready,
            human_review_complete=human_review_complete,
            score_transfer_ready=score_transfer_ready,
            answer_table_blockers_closed=answer_table_blockers_closed,
            source_traceability_issue_counts=_dict(source_traceability.get("issue_counts")),
        ),
        "safety_note": (
            "This claim gate is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _build_findings(
    *,
    review_summary: dict[str, Any],
    transfer_validation: dict[str, Any],
    source_traceability: dict[str, Any],
    answer_map: dict[str, Any],
    drift_check: dict[str, Any],
    drift_lineage: dict[str, Any],
    require_table_drift_check: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if require_table_drift_check and not drift_check:
        findings.append(
            _finding(
                "blocker",
                "table-evidence-drift-check-missing",
                "A table drift check report is required for release-grade table preprocessing evidence.",
            )
        )
    if drift_check and (not bool(drift_check.get("passed")) or _int(drift_check.get("blocker_count")) > 0):
        findings.append(
            _finding(
                "blocker",
                "table-evidence-drift-detected",
                "Table evidence reports are not aligned with their current source files or report lineage.",
                drift_blocker_count=_int(drift_check.get("blocker_count")),
                drift_warning_count=_int(drift_check.get("warning_count")),
            )
        )
    if drift_check and not bool(drift_lineage.get("matches")):
        mismatches = drift_lineage.get("mismatches") or []
        findings.append(
            _finding(
                "blocker",
                "table-evidence-drift-source-reports-mismatch",
                "Table drift source_reports do not resolve to the exact summary, transfer, and traceability reports supplied to this claim gate.",
                mismatch_count=len(mismatches),
                mismatch_roles=[str(item.get("role") or "") for item in mismatches],
                mismatches=mismatches,
            )
        )
    if not bool(source_traceability.get("traceability_passed")):
        findings.append(
            _finding(
                "blocker",
                "table-source-traceability-blocked",
                "Table source traceability must pass before table preprocessing quality can be claimed.",
                issue_count=_int(source_traceability.get("issue_count")),
                issue_counts=_dict(source_traceability.get("issue_counts")),
                blocked_record_count=_int(source_traceability.get("blocked_batch_count")),
            )
        )
    if _int(review_summary.get("pending_unit_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "table-human-review-pending",
                "Table units are selected and source-traceable, but human review is not complete.",
                pending_unit_count=_int(review_summary.get("pending_unit_count")),
                selected_unit_count=_int(review_summary.get("selected_unit_count")),
                required_field_missing_total=_int(review_summary.get("required_field_missing_total")),
            )
        )
    if _int(review_summary.get("invalid_unit_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "table-human-review-invalid",
                "Table unit review has invalid rows that must be corrected.",
                invalid_unit_count=_int(review_summary.get("invalid_unit_count")),
            )
        )
    if not bool(review_summary.get("ready_for_table_score_transfer")):
        findings.append(
            _finding(
                "blocker",
                "table-review-summary-not-ready",
                "Reviewed table unit counts are not ready to transfer into parsing goldset scoring.",
            )
        )
    if not bool(transfer_validation.get("passed")) or _int(transfer_validation.get("blocker_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "table-count-transfer-blocked",
                "Manual and matched table counts cannot yet be transferred into the goldset labels.",
                transfer_blocker_count=_int(transfer_validation.get("blocker_count")),
            )
        )
    table_answer_blocker_count = _table_answer_blocker_count(answer_map)
    if table_answer_blocker_count > 0:
        findings.append(
            _finding(
                "blocker",
                "table-answer-blockers-open",
                "Answer evidence still has unresolved table parentage or structure blockers.",
                table_answer_blocker_count=table_answer_blocker_count,
            )
        )
    if not answer_map:
        findings.append(
            _finding(
                "warning",
                "table-answer-blocker-map-missing",
                "No answer blocker review map was provided, so answer-level table failures were not checked.",
            )
        )
    return findings


def _status(
    *,
    traceability_ready: bool,
    drift_check_ready: bool,
    human_review_complete: bool,
    score_transfer_ready: bool,
    answer_table_blockers_closed: bool,
    passed: bool,
) -> str:
    if passed:
        return "ready_for_table_quality_claim"
    if not drift_check_ready:
        return "blocked_evidence_drift"
    if not traceability_ready:
        return "blocked_source_traceability"
    if not human_review_complete:
        return "blocked_pending_human_review"
    if not score_transfer_ready:
        return "blocked_goldset_count_transfer"
    if not answer_table_blockers_closed:
        return "blocked_answer_table_review"
    return "blocked_unknown"


def _next_steps(
    *,
    traceability_ready: bool,
    drift_check_ready: bool,
    human_review_complete: bool,
    score_transfer_ready: bool,
    answer_table_blockers_closed: bool,
    source_traceability_issue_counts: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    if not drift_check_ready:
        steps.append(
            {
                "step": "repair_table_evidence_lineage",
                "detail": "Regenerate the table review summary, transfer validation, and traceability reports from the same current source files.",
            }
        )
    if not traceability_ready:
        steps.append(
            {
                "step": "repair_source_traceability",
                "detail": _source_traceability_next_step_detail(source_traceability_issue_counts or {}),
            }
        )
    if not human_review_complete:
        steps.append(
            {
                "step": "complete_table_unit_human_review",
                "detail": "Fill reviewer status and manual/matched table counts for every selected table unit.",
            }
        )
    if not score_transfer_ready:
        steps.append(
            {
                "step": "transfer_reviewed_table_counts_to_goldset",
                "detail": "Re-run table count transfer validation after reviewed totals are present in the labels.",
            }
        )
    if not answer_table_blockers_closed:
        steps.append(
            {
                "step": "close_answer_level_table_blockers",
                "detail": "Resolve table parentage and structure blocker queries before using answer evidence as a quality claim.",
            }
        )
    return steps


def _source_traceability_next_step_detail(issue_counts: dict[str, Any]) -> str:
    if _int(issue_counts.get("pdf-reader-backend-unavailable")) > 0:
        return (
            "Fix the Python PDF reader backend or run traceability in the packaged project environment "
            "before table review; the source PDFs have not been proven invalid."
        )
    return "Resolve missing source files, invalid page ranges, or source format issues before review."


def _table_drift_source_report_lineage(
    *,
    drift_check: dict[str, Any],
    drift_check_report: Path,
    table_unit_review_summary: Path,
    table_count_transfer_validation: Path,
    table_source_traceability_report: Path,
) -> dict[str, Any]:
    expected_reports = {
        "table_unit_review_summary_report": table_unit_review_summary,
        "table_count_transfer_validation_report": table_count_transfer_validation,
        "table_source_traceability_report": table_source_traceability_report,
    }
    source_reports = _dict(drift_check.get("source_reports"))
    mismatches: list[dict[str, str]] = []
    resolved_reports: dict[str, str] = {}
    for role, expected in expected_reports.items():
        expected_path = expected.resolve()
        source_value = str(source_reports.get(role) or "").strip()
        actual_path, resolution_error = _resolve_drift_source_report_path(
            source_value,
            drift_check=drift_check,
            drift_check_report=drift_check_report,
        )
        resolved_reports[role] = str(actual_path or "")
        if actual_path is not None and _same_resolved_path(expected_path, actual_path):
            continue
        mismatches.append(
            {
                "role": role,
                "source_value": source_value,
                "expected_path": str(expected_path),
                "actual_path": str(actual_path or ""),
                "reason": resolution_error or "resolved_path_mismatch",
            }
        )
    return {
        "matches": not mismatches,
        "resolved_source_reports": resolved_reports,
        "mismatches": mismatches,
    }


def _resolve_drift_source_report_path(
    source_value: str,
    *,
    drift_check: dict[str, Any],
    drift_check_report: Path,
) -> tuple[Path | None, str]:
    if not source_value:
        return None, "source_report_missing"
    path = Path(source_value).expanduser()
    if not path.is_absolute():
        base_dir_value = str(drift_check.get("base_dir") or "").strip()
        if not base_dir_value:
            return None, "relative_source_report_without_base_dir"
        base_dir = Path(base_dir_value).expanduser()
        if not base_dir.is_absolute():
            base_dir = drift_check_report.resolve().parent / base_dir
        path = base_dir / path
    try:
        return path.resolve(), ""
    except (OSError, RuntimeError):
        return None, "source_report_path_unresolvable"


def _same_resolved_path(expected: Path, actual: Path) -> bool:
    try:
        return expected.samefile(actual)
    except OSError:
        return expected == actual


def _source_artifacts(
    *,
    table_unit_review_summary: Path,
    table_count_transfer_validation: Path,
    table_source_traceability_report: Path,
    answer_blocker_review_map: Path | None,
    table_drift_check_report: Path | None,
) -> list[dict[str, Any]]:
    artifacts = [
        ("table_unit_review_summary", table_unit_review_summary),
        ("table_count_transfer_validation", table_count_transfer_validation),
        ("table_source_traceability_report", table_source_traceability_report),
    ]
    if answer_blocker_review_map:
        artifacts.append(("answer_blocker_review_map", answer_blocker_review_map))
    if table_drift_check_report:
        artifacts.append(("table_drift_check_report", table_drift_check_report))
    return [_artifact(role, path) for role, path in artifacts]


def _artifact(role: str, path: Path) -> dict[str, Any]:
    exists = path.is_file()
    item: dict[str, Any] = {
        "role": role,
        "path": str(path),
        "exists": exists,
        "byte_count": None,
        "sha256": None,
        "report_type": "",
        "passed": None,
    }
    if not exists:
        return item
    item["byte_count"] = path.stat().st_size
    item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        item["report_type"] = str(payload.get("report_type") or "")
        if "passed" in payload:
            item["passed"] = bool(payload.get("passed"))
        elif "traceability_passed" in payload:
            item["passed"] = bool(payload.get("traceability_passed"))
    return item


def _load_json_report(path: Path, *, expected_report_type: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("report_type") != expected_report_type:
        raise ValueError(f"{path} must be a {expected_report_type} report.")
    return payload


def _table_answer_blocker_count(answer_map: dict[str, Any]) -> int:
    if not answer_map:
        return 0
    counts = _dict(answer_map.get("blocker_category_counts"))
    return _int(counts.get(TABLE_BLOCKER_CATEGORY))


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, **extra}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_markdown(report: dict[str, Any]) -> str:
    summary = _dict(report.get("summary"))
    lines = [
        "# Table Preprocessing Claim Gate",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Status: `{report.get('status')}`",
        f"- Claim level: `{report.get('claim_level')}`",
        f"- Feasibility status: `{report.get('feasibility_status')}`",
        f"- Blockers / warnings: {report.get('blocker_count')} / {report.get('warning_count')}",
        "",
        "## Summary",
        "",
        f"- Documents: {summary.get('document_count')}",
        f"- Selected / completed / pending / invalid units: {summary.get('selected_unit_count')} / {summary.get('completed_unit_count')} / {summary.get('pending_unit_count')} / {summary.get('invalid_unit_count')}",
        f"- Required field missing total: {summary.get('required_field_missing_total')}; missing counts=`{summary.get('required_field_missing_counts')}`",
        f"- Review priority counts: `{summary.get('review_priority_counts')}`; label flag counts=`{summary.get('label_review_flag_counts')}`",
        f"- Source traceability: `{str(summary.get('source_traceability_passed')).lower()}`; issues={summary.get('source_traceability_issue_count')}; require page-count verification=`{str(summary.get('source_traceability_require_page_count_verification')).lower()}`; page statuses=`{summary.get('source_page_count_status_counts')}`; format statuses=`{summary.get('source_format_status_counts')}`",
        f"- Source traceability issue counts: `{summary.get('source_traceability_issue_counts')}`; operator next actions=`{summary.get('source_traceability_operator_next_action_counts')}`",
        f"- Drift check: present=`{str(summary.get('drift_check_present')).lower()}`; passed=`{str(summary.get('drift_check_passed')).lower()}`; blockers={summary.get('drift_check_blocker_count')}; source reports match=`{str(summary.get('drift_check_source_reports_match')).lower()}`; lineage mismatches={summary.get('drift_check_lineage_mismatch_count')}",
        f"- Count transfer: `{str(summary.get('transfer_passed')).lower()}`; blockers={summary.get('transfer_blocker_count')}",
        f"- Count-transfer root cause: `{_dict(summary.get('transfer_root_cause_summary')).get('primary_blocker', '')}`; next=`{_dict(summary.get('transfer_root_cause_summary')).get('recommended_next_step', '')}`",
        f"- Answer blocker map present: `{str(summary.get('answer_blocker_map_present')).lower()}`",
        f"- Answer-level table blockers: {summary.get('table_answer_blocker_count')}",
        f"- Non-review evidence ready: `{str(summary.get('non_review_evidence_ready')).lower()}`; release blocked by human review=`{str(summary.get('release_blocked_by_human_review')).lower()}`",
        "",
        "## Findings",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("- None")
    else:
        for finding in findings:
            extras = {
                key: value
                for key, value in finding.items()
                if key not in {"severity", "code", "detail"}
            }
            suffix = f" `{extras}`" if extras else ""
            lines.append(
                f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}{suffix}"
            )
    lines.extend(["", "## Next Steps", ""])
    next_steps = report.get("next_steps") or []
    if not next_steps:
        lines.append("- None")
    else:
        for step in next_steps:
            lines.append(f"- `{step.get('step')}`: {step.get('detail')}")
    lines.extend(["", "## Source Artifacts", ""])
    for artifact in report.get("source_report_artifacts") or []:
        lines.append(
            f"- `{artifact.get('role')}`: `{artifact.get('path')}` sha256=`{artifact.get('sha256') or '-'}`"
        )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit whether table preprocessing evidence is ready for a quality claim."
    )
    parser.add_argument("--table-unit-review-summary", type=Path, required=True)
    parser.add_argument("--table-count-transfer-validation", type=Path, required=True)
    parser.add_argument("--table-source-traceability-report", type=Path, required=True)
    parser.add_argument("--answer-blocker-review-map", type=Path)
    parser.add_argument("--table-drift-check-report", type=Path)
    parser.add_argument(
        "--require-table-drift-check",
        action="store_true",
        help="Block release-grade table claims unless a passing table drift check report is provided.",
    )
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-blocker", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = audit_table_preprocessing_claim_gate(
        table_unit_review_summary=args.table_unit_review_summary,
        table_count_transfer_validation=args.table_count_transfer_validation,
        table_source_traceability_report=args.table_source_traceability_report,
        answer_blocker_review_map=args.answer_blocker_review_map,
        table_drift_check_report=args.table_drift_check_report,
        require_table_drift_check=args.require_table_drift_check,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_blocker and not bool(report.get("passed")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
