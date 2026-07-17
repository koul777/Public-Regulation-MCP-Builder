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

from scripts.reapproval_decision_contract import (
    REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS,
    REAPPROVAL_DECISION_OPERATOR_DECISIONS,
    REAPPROVAL_DECISION_REQUIRED_FIELDS,
    REAPPROVAL_OVERRIDE_DECISIONS,
    is_allowed_operator_decision,
    normalize_override_decision,
    normalize_operator_decision,
    row_missing_required_decision_fields,
)
from scripts.report_metadata import current_repo_commit


APPROVAL_SCOPE_CONFIRMATIONS = REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS


def validate_reapproval_decisions(
    *,
    reapproval_review_batch_report: Path,
    decision_template_csv: Path,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    manifest = _load_manifest(reapproval_review_batch_report)
    rows = _load_decision_rows(decision_template_csv)
    batches = [batch for batch in manifest.get("batches") or [] if isinstance(batch, dict)]
    expected_batch_ids = [
        str(batch.get("reapproval_batch_id") or "").strip()
        for batch in batches
    ]
    expected_batch_ids = [batch_id for batch_id in expected_batch_ids if batch_id]
    batch_chunk_ids_by_batch_id = {
        str(batch.get("reapproval_batch_id") or "").strip(): {
            str(chunk_id).strip()
            for chunk_id in batch.get("chunk_ids") or []
            if str(chunk_id or "").strip()
        }
        for batch in batches
        if str(batch.get("reapproval_batch_id") or "").strip()
    }
    findings = _manifest_findings(manifest=manifest, expected_batch_ids=expected_batch_ids)
    findings.extend(
        _findings(
            expected_batch_ids=expected_batch_ids,
            rows=rows,
            batch_chunk_ids_by_batch_id=batch_chunk_ids_by_batch_id,
        )
    )
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    complete_row_count = sum(1 for row in rows if _row_is_complete(row))
    action_counts = Counter(normalize_operator_decision(row.get("operator_decision")) or "blank" for row in rows)
    report = {
        "report_type": "reapproval_decision_validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": blocker_count == 0,
        "release_gate_status": _release_gate_status(
            blocker_count=blocker_count,
            expected_batch_count=len(expected_batch_ids),
        ),
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "findings": findings,
        "source_reports": {
            "reapproval_review_batch_report": str(reapproval_review_batch_report),
            "decision_template_csv": str(decision_template_csv),
        },
        "source_report_artifacts": [
            _source_artifact("reapproval_review_batch_manifest_report", reapproval_review_batch_report),
            _source_artifact("reapproval_decision_template_csv", decision_template_csv),
        ],
        "manifest_summary": {
            "passed": bool(manifest.get("passed")),
            "blocker_count": _int_or_none(manifest.get("blocker_count")),
            "warning_count": _int_or_none(manifest.get("warning_count")),
            "reported_batch_count": _int_or_none(manifest.get("batch_count")),
            "actual_batch_count": len(expected_batch_ids),
        },
        "expected_batch_count": len(expected_batch_ids),
        "decision_row_count": len(rows),
        "complete_row_count": complete_row_count,
        "blank_or_incomplete_row_count": max(len(rows) - complete_row_count, 0),
        "operator_decision_counts": dict(sorted(action_counts.items())),
        "allowed_operator_decisions": list(REAPPROVAL_DECISION_OPERATOR_DECISIONS),
        "required_operator_fields": list(REAPPROVAL_DECISION_REQUIRED_FIELDS),
        "allowed_approval_scope_confirmations": list(APPROVAL_SCOPE_CONFIRMATIONS),
        "allowed_override_decisions": list(REAPPROVAL_OVERRIDE_DECISIONS),
        "operator_controls": {
            "auto_approval": False,
            "auto_reindex": False,
            "applies_reapproval_decisions": False,
            "ready_for_apply": blocker_count == 0 and len(expected_batch_ids) > 0,
        },
        "safety_note": (
            "This validation is read-only. It does not approve chunks, apply reapproval decisions, "
            "write Vector DB records, reindex, or publish MCP artifacts."
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


def _manifest_findings(*, manifest: dict[str, Any], expected_batch_ids: Sequence[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    manifest_blocker_count = _int_or_none(manifest.get("blocker_count"))
    if manifest_blocker_count is None:
        findings.append(
            _finding(
                "blocker",
                "reapproval-review-batch-blocker-count-missing-or-invalid",
                "Manifest blocker_count must be present and numeric.",
            )
        )
    if not bool(manifest.get("passed")) or (manifest_blocker_count is not None and manifest_blocker_count > 0):
        findings.append(
            _finding(
                "blocker",
                "reapproval-review-batch-manifest-not-ready",
                "Reapproval review batch manifest must pass before operator decision validation can pass.",
                manifest_passed=bool(manifest.get("passed")),
                manifest_blocker_count=manifest_blocker_count,
            )
        )
    reported_batch_count = _int_or_none(manifest.get("batch_count"))
    if reported_batch_count is None:
        findings.append(
            _finding(
                "blocker",
                "reapproval-review-batch-count-missing-or-invalid",
                "Manifest batch_count must be present and numeric.",
                actual_batch_count=len(expected_batch_ids),
            )
        )
    elif reported_batch_count != len(expected_batch_ids):
        findings.append(
            _finding(
                "blocker",
                "reapproval-review-batch-count-mismatch",
                "Manifest batch_count must match the number of concrete reapproval batch ids.",
                reported_batch_count=reported_batch_count,
                actual_batch_count=len(expected_batch_ids),
            )
        )
    duplicate_ids = sorted(batch_id for batch_id, count in Counter(expected_batch_ids).items() if batch_id and count > 1)
    if duplicate_ids:
        findings.append(
            _finding(
                "blocker",
                "reapproval-review-batch-duplicate-id",
                "Manifest contains duplicate reapproval batch ids.",
                duplicate_batch_ids=duplicate_ids,
            )
        )
    return findings


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "reapproval_review_batch_manifest":
        raise ValueError("reapproval_review_batch_report must be a reapproval_review_batch_manifest JSON report.")
    return payload


def _load_decision_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _findings(
    *,
    expected_batch_ids: Sequence[str],
    rows: Sequence[dict[str, Any]],
    batch_chunk_ids_by_batch_id: dict[str, set[str]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    expected = set(expected_batch_ids)
    row_ids = [str(row.get("reapproval_batch_id") or "").strip() for row in rows]
    row_id_counts = Counter(row_ids)
    if expected_batch_ids and not rows:
        findings.append(_finding("blocker", "decision-template-empty", "Decision template has no rows."))
    missing_required_counts: Counter[str] = Counter()
    invalid_decisions: Counter[str] = Counter()
    invalid_scope_confirmations: Counter[str] = Counter()
    invalid_override_rows: list[str] = []
    partial_without_override_rows: list[str] = []
    missing_batch_id_count = 0
    unknown_ids = set()
    for row in rows:
        batch_id = str(row.get("reapproval_batch_id") or "").strip()
        if not batch_id:
            missing_batch_id_count += 1
        elif batch_id not in expected:
            unknown_ids.add(batch_id)
        for field in row_missing_required_decision_fields(row):
            missing_required_counts[field] += 1
        decision = normalize_operator_decision(row.get("operator_decision"))
        if decision and not is_allowed_operator_decision(decision):
            invalid_decisions[decision] += 1
        scope = str(row.get("approval_scope_confirmation") or "").strip()
        if scope and scope not in APPROVAL_SCOPE_CONFIRMATIONS:
            invalid_scope_confirmations[scope] += 1
        override_status = _override_status(row.get("chunk_decision_overrides_json"))
        if override_status == "invalid":
            invalid_override_rows.append(batch_id)
        elif batch_id in batch_chunk_ids_by_batch_id:
            findings.extend(
                _override_scope_findings(
                    row,
                    batch_chunk_ids=batch_chunk_ids_by_batch_id[batch_id],
                )
            )
        if decision == "partial_with_overrides" and override_status != "non_empty":
            partial_without_override_rows.append(batch_id)
    duplicate_ids = sorted(batch_id for batch_id, count in row_id_counts.items() if batch_id and count > 1)
    missing_ids = sorted(expected - {batch_id for batch_id in row_ids if batch_id})
    if missing_batch_id_count:
        findings.append(
            _finding(
                "blocker",
                "decision-template-batch-id-missing",
                "Every decision row must identify a reapproval batch.",
                missing_batch_id_count=missing_batch_id_count,
            )
        )
    if duplicate_ids:
        findings.append(
            _finding(
                "blocker",
                "decision-template-duplicate-batch-id",
                "Decision template contains duplicate reapproval batch ids.",
                duplicate_batch_ids=duplicate_ids,
            )
        )
    if unknown_ids:
        findings.append(
            _finding(
                "blocker",
                "decision-template-unknown-batch-id",
                "Decision template contains batch ids that are not in the manifest.",
                unknown_batch_ids=sorted(unknown_ids),
            )
        )
    if missing_ids:
        findings.append(
            _finding(
                "blocker",
                "decision-template-missing-batches",
                "Some manifest batches have no decision row.",
                missing_batch_ids=missing_ids,
            )
        )
    if missing_required_counts:
        findings.append(
            _finding(
                "blocker",
                "decision-template-required-fields-missing",
                "Operator decision rows must include decision, reviewer, timestamp, and scope confirmation.",
                missing_field_counts=dict(sorted(missing_required_counts.items())),
            )
        )
    if invalid_decisions:
        findings.append(
            _finding(
                "blocker",
                "decision-template-invalid-operator-decision",
                "Operator decisions must match the allowed decision contract.",
                invalid_operator_decisions=dict(sorted(invalid_decisions.items())),
                allowed_operator_decisions=list(REAPPROVAL_DECISION_OPERATOR_DECISIONS),
            )
        )
    if invalid_scope_confirmations:
        findings.append(
            _finding(
                "blocker",
                "decision-template-invalid-scope-confirmation",
                "Approval scope confirmation must explicitly confirm the batch-level scope.",
                invalid_scope_confirmations=dict(sorted(invalid_scope_confirmations.items())),
                allowed_approval_scope_confirmations=list(APPROVAL_SCOPE_CONFIRMATIONS),
            )
        )
    if invalid_override_rows:
        findings.append(
            _finding(
                "blocker",
                "decision-template-invalid-overrides-json",
                "chunk_decision_overrides_json must be valid JSON when provided.",
                reapproval_batch_ids=sorted(set(invalid_override_rows)),
            )
        )
    if partial_without_override_rows:
        findings.append(
            _finding(
                "blocker",
                "partial-decision-overrides-missing",
                "partial_with_overrides decisions require non-empty chunk_decision_overrides_json.",
                reapproval_batch_ids=sorted(set(partial_without_override_rows)),
            )
        )
    if not expected_batch_ids:
        findings.append(
            _finding(
                "warning",
                "reapproval-batches-empty",
                "The reapproval batch manifest has no batches to validate.",
            )
        )
    return findings


def _override_scope_findings(row: dict[str, Any], *, batch_chunk_ids: set[str]) -> list[dict[str, Any]]:
    text = str(row.get("chunk_decision_overrides_json") or "").strip()
    if not text or text == "[]":
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    raw_items: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        raw_items = [(str(key), value) for key, value in payload.items()]
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                raw_items.append(("", None))
                continue
            raw_items.append(
                (
                    str(item.get("chunk_id") or "").strip(),
                    item.get("operator_decision", item.get("decision", item.get("action"))),
                )
            )
    else:
        return [
            _finding(
                "blocker",
                "decision-template-unsupported-overrides-shape",
                "chunk_decision_overrides_json must be a JSON object or a list of objects.",
                reapproval_batch_ids=[str(row.get("reapproval_batch_id") or "").strip()],
            )
        ]

    batch_id = str(row.get("reapproval_batch_id") or "").strip()
    missing_chunk_id_count = 0
    duplicate_chunk_ids: set[str] = set()
    seen_chunk_ids: set[str] = set()
    unknown_chunk_ids: set[str] = set()
    invalid_override_decisions: dict[str, str] = {}
    for chunk_id, raw_decision in raw_items:
        if not chunk_id:
            missing_chunk_id_count += 1
            continue
        if chunk_id in seen_chunk_ids:
            duplicate_chunk_ids.add(chunk_id)
        seen_chunk_ids.add(chunk_id)
        if chunk_id not in batch_chunk_ids:
            unknown_chunk_ids.add(chunk_id)
        if not normalize_override_decision(raw_decision):
            invalid_override_decisions[chunk_id] = str(raw_decision or "")

    findings: list[dict[str, Any]] = []
    if missing_chunk_id_count:
        findings.append(
            _finding(
                "blocker",
                "decision-template-override-chunk-id-missing",
                "Every override entry must include chunk_id.",
                reapproval_batch_ids=[batch_id],
                missing_chunk_id_count=missing_chunk_id_count,
            )
        )
    if duplicate_chunk_ids:
        findings.append(
            _finding(
                "blocker",
                "decision-template-duplicate-override-chunk-id",
                "Override entries must not repeat chunk_id within the same batch.",
                reapproval_batch_ids=[batch_id],
                duplicate_chunk_ids=sorted(duplicate_chunk_ids),
            )
        )
    if unknown_chunk_ids:
        findings.append(
            _finding(
                "blocker",
                "decision-template-override-chunk-id-outside-batch",
                "Override chunk_id must be present in the matching reapproval batch manifest.",
                reapproval_batch_ids=[batch_id],
                unknown_chunk_ids=sorted(unknown_chunk_ids),
            )
        )
    if invalid_override_decisions:
        findings.append(
            _finding(
                "blocker",
                "decision-template-invalid-override-decision",
                "Override decisions must be approve, reject, needs_reprocess, or defer aliases.",
                reapproval_batch_ids=[batch_id],
                invalid_override_decisions=dict(sorted(invalid_override_decisions.items())),
                allowed_override_decisions=list(REAPPROVAL_OVERRIDE_DECISIONS),
            )
        )
    return findings


def _row_is_complete(row: dict[str, Any]) -> bool:
    decision = normalize_operator_decision(row.get("operator_decision"))
    scope = str(row.get("approval_scope_confirmation") or "").strip()
    if row_missing_required_decision_fields(row):
        return False
    if not is_allowed_operator_decision(decision):
        return False
    if scope not in APPROVAL_SCOPE_CONFIRMATIONS:
        return False
    if _override_status(row.get("chunk_decision_overrides_json")) == "invalid":
        return False
    if decision == "partial_with_overrides" and _override_status(row.get("chunk_decision_overrides_json")) != "non_empty":
        return False
    return True


def _override_status(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "[]":
        return "empty"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "invalid"
    if isinstance(parsed, (list, dict)) and parsed:
        return "non_empty"
    return "empty"


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _release_gate_status(*, blocker_count: int, expected_batch_count: int) -> str:
    if blocker_count:
        return "blocked_pending_operator_decisions"
    if expected_batch_count <= 0:
        return "no_reapproval_batches"
    return "ready_for_reapproval_apply"


def _source_artifact(role: str, path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "role": role,
        "path": str(path),
        "exists": path.is_file(),
        "byte_count": None,
        "sha256": None,
    }
    if not path.is_file():
        return item
    data = path.read_bytes()
    item["byte_count"] = len(data)
    item["sha256"] = hashlib.sha256(data).hexdigest()
    return item


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    payload = {"severity": severity, "code": code, "detail": detail}
    payload.update(extra)
    return payload


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Reapproval Decision Validation",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Release gate status: `{report.get('release_gate_status')}`",
        f"- Expected batches: `{report.get('expected_batch_count')}`",
        f"- Decision rows: `{report.get('decision_row_count')}`",
        f"- Complete rows: `{report.get('complete_row_count')}`",
        f"- Blank/incomplete rows: `{report.get('blank_or_incomplete_row_count')}`",
        f"- Blocking / warning count: `{report.get('blocking_count')}` / `{report.get('warning_count')}`",
        "",
        "## Findings",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("- None")
    for finding in findings:
        lines.append(f"- `{finding.get('severity')}` `{finding.get('code')}`: {finding.get('detail')}")
    lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate completed reapproval decision CSV before any apply or reindex step."
    )
    parser.add_argument("--reapproval-review-batch-report", type=Path, required=True)
    parser.add_argument("--decision-template-csv", type=Path, required=True)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = validate_reapproval_decisions(
        reapproval_review_batch_report=args.reapproval_review_batch_report,
        decision_template_csv=args.decision_template_csv,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_issue and int(report["blocking_count"]) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
