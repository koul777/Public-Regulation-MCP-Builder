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


HASH_CHUNK_BYTES = 1024 * 1024
COMPLETION_FIELDS = ("decision", "decision_owner", "decision_reference")
REQUIRED_FIELDS = ("decision_id", "workstream", "summary", *COMPLETION_FIELDS)
PLACEHOLDER_VALUES = {"", "-", "?", "todo", "tbd", "pending", "undecided", "none", "n/a", "na"}


def build_owner_decision_gate(
    *,
    decisions_csv: Path,
    readiness_summary_report: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    rows = _load_decision_rows(decisions_csv)
    summary = _load_json(readiness_summary_report) if readiness_summary_report else {}
    expected_ids = _expected_decision_ids(summary)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_ids = Counter(row.get("decision_id", "").strip() for row in rows if row.get("decision_id", "").strip())

    decision_statuses: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        decision_id = row.get("decision_id", "").strip()
        missing_fields = [field for field in REQUIRED_FIELDS if _is_placeholder(row.get(field))]
        completion_missing_fields = [field for field in COMPLETION_FIELDS if _is_placeholder(row.get(field))]
        if not decision_id:
            blockers.append(
                {
                    "code": "missing_decision_id",
                    "row_number": index,
                    "field": "decision_id",
                    "detail": "Decision rows must include a stable decision_id.",
                }
            )
        for field in missing_fields:
            blockers.append(
                {
                    "code": "missing_required_decision_field",
                    "decision_id": decision_id,
                    "row_number": index,
                    "field": field,
                    "detail": f"Required owner decision field '{field}' is blank or placeholder.",
                }
            )
        decision_statuses.append(
            {
                "decision_id": decision_id,
                "workstream": row.get("workstream", "").strip(),
                "complete": not completion_missing_fields and bool(decision_id),
                "missing_fields": completion_missing_fields,
            }
        )

    for decision_id, count in seen_ids.items():
        if count > 1:
            blockers.append(
                {
                    "code": "duplicate_decision_id",
                    "decision_id": decision_id,
                    "detail": f"Decision id appears {count} times.",
                }
            )

    row_ids = {decision_id for decision_id in seen_ids if decision_id}
    missing_expected = sorted(expected_ids - row_ids)
    unexpected = sorted(row_ids - expected_ids) if expected_ids else []
    for decision_id in missing_expected:
        blockers.append(
            {
                "code": "missing_expected_decision",
                "decision_id": decision_id,
                "detail": "Readiness summary requires this owner decision, but the CSV row is missing.",
            }
        )
    for decision_id in unexpected:
        warnings.append(
            {
                "code": "unexpected_decision_id",
                "decision_id": decision_id,
                "detail": "Decision id is not listed in the readiness summary.",
            }
        )

    field_missing_counts = Counter(
        str(blocker.get("field"))
        for blocker in blockers
        if blocker.get("code") == "missing_required_decision_field" and blocker.get("field")
    )
    complete_count = sum(1 for status in decision_statuses if status["complete"])
    incomplete_ids = [
        status["decision_id"] for status in decision_statuses if not status["complete"] and status["decision_id"]
    ]
    blocker_count = len(blockers)
    report = {
        "report_type": "github_publish_owner_decision_gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": blocker_count == 0,
        "status": "owner_decisions_ready" if blocker_count == 0 else "blocked_pending_owner_decisions",
        "decision_count": len(rows),
        "expected_decision_count": len(expected_ids) if expected_ids else None,
        "complete_decision_count": complete_count,
        "incomplete_decision_count": len(rows) - complete_count,
        "incomplete_decision_ids": incomplete_ids,
        "required_fields": list(REQUIRED_FIELDS),
        "completion_fields": list(COMPLETION_FIELDS),
        "required_field_missing_counts": dict(sorted(field_missing_counts.items())),
        "missing_expected_decision_ids": missing_expected,
        "unexpected_decision_ids": unexpected,
        "duplicate_decision_ids": sorted([decision_id for decision_id, count in seen_ids.items() if count > 1]),
        "decision_statuses": decision_statuses,
        "blocker_count": blocker_count,
        "warning_count": len(warnings),
        "blockers": blockers,
        "warnings": warnings,
        "source_reports": {
            "decisions_csv": str(decisions_csv),
            "readiness_summary_report": str(readiness_summary_report) if readiness_summary_report else None,
        },
        "source_report_artifacts": [
            _source_artifact("decisions_csv", decisions_csv),
            *(
                [_source_artifact("readiness_summary_report", readiness_summary_report)]
                if readiness_summary_report
                else []
            ),
        ],
        "safety_note": (
            "This gate is read-only. It does not apply cleanup actions, remove files, add a license, "
            "or approve public publication."
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


def _load_decision_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [{key: str(value or "") for key, value in row.items() if key is not None} for row in reader]


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _expected_decision_ids(summary: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in summary.get("owner_decisions_required") or []:
        if isinstance(item, dict) and item.get("decision_id"):
            ids.add(str(item["decision_id"]))
    return ids


def _is_placeholder(value: Any) -> bool:
    return str(value or "").strip().lower() in PLACEHOLDER_VALUES


def _source_artifact(role: str, path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "role": role,
        "path": str(path),
        "exists": exists,
        "sha256": _sha256_file(path) if exists else None,
        "byte_count": path.stat().st_size if exists else None,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# GitHub Publish Owner Decision Gate",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Decision completion: {report.get('complete_decision_count')} / {report.get('decision_count')}",
        f"- Blockers: {report.get('blocker_count')}",
        f"- Warnings: {report.get('warning_count')}",
        "",
        "## Decision Status",
        "",
        "| Decision ID | Workstream | Complete | Missing Fields |",
        "| --- | --- | --- | --- |",
    ]
    for status in report.get("decision_statuses") or []:
        if not isinstance(status, dict):
            continue
        lines.append(
            "| {decision_id} | {workstream} | `{complete}` | {missing_fields} |".format(
                decision_id=_md_cell(status.get("decision_id")),
                workstream=_md_cell(status.get("workstream")),
                complete=str(bool(status.get("complete"))).lower(),
                missing_fields=_md_cell(", ".join(status.get("missing_fields") or []) or "-"),
            )
        )

    blockers = report.get("blockers") or []
    lines.extend(["", "## Blockers", ""])
    if not blockers:
        lines.append("- None.")
    else:
        lines.extend(
            [
                "| Code | Decision ID | Field | Detail |",
                "| --- | --- | --- | --- |",
            ]
        )
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            lines.append(
                "| {code} | {decision_id} | {field} | {detail} |".format(
                    code=_md_cell(blocker.get("code")),
                    decision_id=_md_cell(blocker.get("decision_id")),
                    field=_md_cell(blocker.get("field")),
                    detail=_md_cell(blocker.get("detail")),
                )
            )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check owner decisions required before public GitHub cleanup.")
    parser.add_argument("--decisions-csv", type=Path, required=True)
    parser.add_argument("--readiness-summary-report", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-blocker", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = build_owner_decision_gate(
        decisions_csv=args.decisions_csv,
        readiness_summary_report=args.readiness_summary_report,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    if args.json:
        stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        stdout.write(_to_markdown(report))
    if args.fail_on_blocker and int(report["blocker_count"]) > 0:
        return 1
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
