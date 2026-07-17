from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


DECISION_COLUMNS = [
    "decision_id",
    "blocks_product_release",
    "required_decision",
    "evidence_required",
    "allowed_operator_decisions",
    "minimum_evidence_fields",
    "policy_guidance",
    "decision_status",
    "operator_decision",
    "accepted_ambiguity_fields",
    "answer_disclosure_policy",
    "owner",
    "decision_reference",
    "sample_answer_artifact",
    "post_policy_readiness_report",
]


DECISION_GUIDANCE = {
    "temporal_ambiguity_index_policy": {
        "allowed_operator_decisions": ["approve_index_with_disclosure", "block_ambiguous_indexing"],
        "minimum_evidence_fields": [
            "operator_decision",
            "accepted_ambiguity_fields",
            "owner",
            "decision_reference",
            "post_policy_readiness_report",
        ],
        "policy_guidance": (
            "If indexing is approved, document accepted ambiguity fields and require downstream disclosure. "
            "If indexing is blocked, prove ambiguous chunks stay out of the approved vector index."
        ),
    },
    "temporal_ambiguity_answer_policy": {
        "allowed_operator_decisions": ["approve_with_disclosure"],
        "minimum_evidence_fields": [
            "operator_decision",
            "answer_disclosure_policy",
            "owner",
            "decision_reference",
            "sample_answer_artifact",
        ],
        "policy_guidance": (
            "MCP answers must disclose ambiguous effective/revision dates and cite the supporting evidence."
        ),
    },
}


def build_temporal_ambiguity_policy_decision_sheet(
    *,
    scope_report: Path,
    out_json: Path | None = None,
    out_csv: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    scope = _load_json(scope_report)
    requirements = [item for item in scope.get("decision_requirements") or [] if isinstance(item, dict)]
    rows = [_decision_row(requirement) for requirement in requirements]
    missing_scope_requirements = not requirements and not bool(scope.get("passed"))
    blocking_pending_count = sum(
        1
        for row in rows
        if row["blocks_product_release"] and row["decision_status"] == "pending_operator_decision"
    )
    ready_for_policy_validation = blocking_pending_count == 0 and not missing_scope_requirements
    report = {
        "report_type": "temporal_ambiguity_policy_decision_sheet",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "scope_report": str(scope_report),
        "scope_report_sha256": _sha256_file(scope_report),
        "scope_status": str(scope.get("status") or ""),
        "scope_passed": bool(scope.get("passed")),
        "summary": {
            "decision_count": len(rows),
            "release_blocking_decision_count": sum(1 for row in rows if row["blocks_product_release"]),
            "pending_release_blocking_decision_count": blocking_pending_count,
            "missing_scope_decision_requirement_count": 1 if missing_scope_requirements else 0,
            "ready_for_policy_validation": ready_for_policy_validation,
        },
        "findings": (
            [
                {
                    "severity": "blocker",
                    "code": "temporal-scope-decision-requirements-missing",
                    "detail": "Scope report is not passed but contains no decision_requirements rows.",
                }
            ]
            if missing_scope_requirements
            else []
        ),
        "decision_rows": rows,
        "safety_note": (
            "This sheet is an operator decision input artifact only. It does not approve temporal policy, "
            "rewrite temporal metadata, apply backfill, or publish MCP records."
        ),
        "api_call_count": 0,
    }
    report["passed"] = bool(report["summary"]["ready_for_policy_validation"])
    report["status"] = "ready_for_policy_validation" if report["passed"] else "pending_operator_policy_decisions"
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_csv:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        _write_csv(out_csv, rows)
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _decision_row(requirement: dict[str, Any]) -> dict[str, Any]:
    blocks = bool(requirement.get("blocks_product_release"))
    decision_id = str(requirement.get("decision_id") or "")
    guidance = DECISION_GUIDANCE.get(decision_id, {})
    return {
        "decision_id": decision_id,
        "blocks_product_release": blocks,
        "required_decision": str(requirement.get("required_decision") or ""),
        "evidence_required": "; ".join(str(item) for item in requirement.get("evidence_required") or []),
        "allowed_operator_decisions": "; ".join(str(item) for item in guidance.get("allowed_operator_decisions", [])),
        "minimum_evidence_fields": "; ".join(str(item) for item in guidance.get("minimum_evidence_fields", [])),
        "policy_guidance": str(guidance.get("policy_guidance") or ""),
        "decision_status": "pending_operator_decision" if blocks else "not_required",
        "operator_decision": "",
        "accepted_ambiguity_fields": "",
        "answer_disclosure_policy": "",
        "owner": "",
        "decision_reference": "",
        "sample_answer_artifact": "",
        "post_policy_readiness_report": "",
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Temporal Ambiguity Policy Decision Sheet",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Scope report: `{report.get('scope_report')}`",
        f"- Scope status: `{report.get('scope_status')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Decision rows: {summary.get('decision_count')}",
        f"- Release-blocking decisions: {summary.get('release_blocking_decision_count')}",
        f"- Pending release-blocking decisions: {summary.get('pending_release_blocking_decision_count')}",
        "",
        "## Decisions",
        "",
        "| Decision ID | Blocks Release | Status | Allowed Decisions | Minimum Evidence Fields | Required Decision |",
        "|---|---:|---|---|---|---|",
    ]
    for row in report.get("decision_rows") or []:
        lines.append(
            "| {decision_id} | `{blocks}` | `{status}` | {allowed} | {minimum} | {required} |".format(
                decision_id=_md(row.get("decision_id")),
                blocks=str(bool(row.get("blocks_product_release"))).lower(),
                status=_md(row.get("decision_status")),
                allowed=_md(row.get("allowed_operator_decisions")),
                minimum=_md(row.get("minimum_evidence_fields")),
                required=_md(row.get("required_decision")),
            )
        )
    lines.extend(["", "## Policy Guidance", ""])
    for row in report.get("decision_rows") or []:
        if row.get("policy_guidance"):
            lines.append(f"- `{_md(row.get('decision_id'))}`: {_md(row.get('policy_guidance'))}")
    lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
    return "\n".join(lines).strip() + "\n"


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an operator decision sheet for temporal ambiguity policy.")
    parser.add_argument("--scope-report", required=True)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    try:
        report = build_temporal_ambiguity_policy_decision_sheet(
            scope_report=Path(args.scope_report),
            out_json=Path(args.out_json) if args.out_json else None,
            out_csv=Path(args.out_csv) if args.out_csv else None,
            out_md=Path(args.out_md) if args.out_md else None,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=stdout)
        return 2
    print(json.dumps({"ok": True, "status": report["status"], **report["summary"]}, ensure_ascii=False, indent=2), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
