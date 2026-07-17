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

from scripts.report_metadata import current_repo_commit


RELEASE_READY_DECISIONS = {
    "approve_with_disclosure",
    "approve_index_with_disclosure",
    "block_ambiguous_indexing",
}

DECISION_ALLOWED_OPERATOR_DECISIONS = {
    "temporal_ambiguity_index_policy": {
        "approve_index_with_disclosure",
        "block_ambiguous_indexing",
    },
    "temporal_ambiguity_answer_policy": {
        "approve_with_disclosure",
    },
}


def validate_temporal_ambiguity_policy_decisions(
    *,
    scope_report: Path,
    decisions_csv: Path,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    scope = _load_json(scope_report)
    rows = _load_csv(decisions_csv)
    findings = _validate_scope_coverage(scope, rows)
    findings.extend(_validate_rows(rows))
    blocking_count = sum(1 for finding in findings if finding["severity"] == "blocker")
    warning_count = sum(1 for finding in findings if finding["severity"] == "warning")
    status = "policy_decisions_valid" if blocking_count == 0 else "blocked_pending_policy_decisions"
    report = {
        "report_type": "temporal_ambiguity_policy_decision_validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "scope_report": str(scope_report),
        "scope_report_sha256": _sha256_file(scope_report),
        "decisions_csv": str(decisions_csv),
        "decisions_csv_sha256": _sha256_file(decisions_csv),
        "scope_status": str(scope.get("status") or ""),
        "scope_passed": bool(scope.get("passed")),
        "status": status,
        "passed": blocking_count == 0,
        "decision_row_count": len(rows),
        "release_blocking_row_count": sum(1 for row in rows if _bool(row.get("blocks_product_release"))),
        "operator_decision_counts": dict(Counter(_clean(row.get("operator_decision")) or "blank" for row in rows)),
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "findings": findings,
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _validate_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not rows:
        findings.append(_finding("blocker", "temporal-policy-decisions-empty", "", "No temporal policy decision rows were provided."))
        return findings
    for index, row in enumerate(rows, start=1):
        decision_id = _clean(row.get("decision_id"))
        if not _bool(row.get("blocks_product_release")):
            continue
        operator_decision = _clean(row.get("operator_decision"))
        if not operator_decision:
            findings.append(_finding("blocker", "temporal-policy-decision-blank", decision_id, "Release-blocking decision is blank.", row=index))
            continue
        allowed_decisions = _allowed_operator_decisions(decision_id)
        if operator_decision not in allowed_decisions:
            findings.append(
                _finding(
                    "blocker",
                    "temporal-policy-decision-not-release-ready",
                    decision_id,
                    f"operator_decision must be one of {sorted(allowed_decisions)} for this decision_id.",
                    row=index,
                )
            )
        for field in ("owner", "decision_reference"):
            if not _clean(row.get(field)):
                findings.append(_finding("blocker", "temporal-policy-evidence-missing", decision_id, f"Missing {field}.", row=index, field=field))
        if decision_id == "temporal_ambiguity_index_policy":
            for field in ("accepted_ambiguity_fields", "post_policy_readiness_report"):
                if not _clean(row.get(field)):
                    findings.append(_finding("blocker", "temporal-policy-evidence-missing", decision_id, f"Missing {field}.", row=index, field=field))
        if decision_id == "temporal_ambiguity_answer_policy":
            for field in ("answer_disclosure_policy", "sample_answer_artifact"):
                if not _clean(row.get(field)):
                    findings.append(_finding("blocker", "temporal-policy-evidence-missing", decision_id, f"Missing {field}.", row=index, field=field))
    return findings


def _allowed_operator_decisions(decision_id: str) -> set[str]:
    return DECISION_ALLOWED_OPERATOR_DECISIONS.get(decision_id, RELEASE_READY_DECISIONS)


def _validate_scope_coverage(scope: dict[str, Any], rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    requirements = [item for item in scope.get("decision_requirements") or [] if isinstance(item, dict)]
    if not bool(scope.get("passed")) and not requirements:
        findings.append(
            _finding(
                "blocker",
                "temporal-policy-scope-requirements-missing",
                "",
                "Scope report is not passed but contains no decision_requirements rows.",
            )
        )
        return findings
    expected_ids = {
        decision_id
        for requirement in requirements
        if bool(requirement.get("blocks_product_release"))
        for decision_id in [_clean(requirement.get("decision_id"))]
        if decision_id
    }
    if not expected_ids:
        return findings
    release_row_ids = {
        _clean(row.get("decision_id"))
        for row in rows
        if _bool(row.get("blocks_product_release")) and _clean(row.get("decision_id"))
    }
    for decision_id in sorted(expected_ids - release_row_ids):
        findings.append(
            _finding(
                "blocker",
                "temporal-policy-decision-row-missing",
                decision_id,
                "Required release-blocking temporal policy decision row is missing from decisions CSV.",
            )
        )
    return findings


def _finding(
    severity: str,
    code: str,
    decision_id: str,
    detail: str,
    *,
    row: int | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "decision_id": decision_id,
        "row": row,
        "field": field or "",
        "detail": detail,
    }


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Temporal Ambiguity Policy Decision Validation",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Scope report: `{report.get('scope_report')}`",
        f"- Decisions CSV: `{report.get('decisions_csv')}`",
        f"- Status: `{report.get('status')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Decision rows: {report.get('decision_row_count')}",
        f"- Release-blocking rows: {report.get('release_blocking_row_count')}",
        f"- Blockers: {report.get('blocking_count')}",
        f"- Warnings: {report.get('warning_count')}",
        "",
        "## Findings",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("- none")
    for finding in findings:
        lines.append(
            "- {severity} `{code}` {decision} row={row} field={field}: {detail}".format(
                severity=finding.get("severity"),
                code=finding.get("code"),
                decision=finding.get("decision_id") or "",
                row=finding.get("row") or "",
                field=finding.get("field") or "",
                detail=finding.get("detail") or "",
            )
        )
    return "\n".join(lines).strip() + "\n"


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate completed temporal ambiguity policy decision rows.")
    parser.add_argument("--scope-report", required=True)
    parser.add_argument("--decisions-csv", required=True)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    try:
        report = validate_temporal_ambiguity_policy_decisions(
            scope_report=Path(args.scope_report),
            decisions_csv=Path(args.decisions_csv),
            out_json=Path(args.out_json) if args.out_json else None,
            out_md=Path(args.out_md) if args.out_md else None,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=stdout)
        return 2
    print(
        json.dumps(
            {
                "ok": report["passed"],
                "status": report["status"],
                "blocking_count": report["blocking_count"],
                "warning_count": report["warning_count"],
                "api_call_count": 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=stdout,
    )
    return 1 if args.fail_on_issue and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
