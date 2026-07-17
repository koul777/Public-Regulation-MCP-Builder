from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.institution_profiles import (
    institution_profile_registry_to_dict,
    load_institution_profile_registry,
    load_institution_profile_registry_from_bytes,
    normalize_profile_id,
)


DEFAULT_REQUIRED_ROW_FIELDS = (
    "institution_name",
    "apba_id",
    "source_system",
    "source_url",
    "source_record_id",
    "source_file_id",
    "source_posted_date",
    "profile_id",
)


def build_institution_profile_registry_from_batch(
    *,
    batch_reports: Sequence[str | Path],
    existing_registry: str | Path | None = None,
    required_row_fields: Sequence[str] = DEFAULT_REQUIRED_ROW_FIELDS,
    out_registry: str | Path | None = None,
    out_report_json: str | Path | None = None,
    out_md: str | Path | None = None,
) -> dict[str, Any]:
    existing_payload: dict[str, Any] = {"profiles": {}}
    existing_profile_ids: set[str] = set()
    if existing_registry:
        existing = load_institution_profile_registry(existing_registry)
        existing_payload = institution_profile_registry_to_dict(existing)
        existing_profile_ids = set(existing.profiles)

    profile_values: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {
            "profile_id": set(),
            "institution_name": set(),
            "apba_id": set(),
            "source_system": set(),
            "source_url": set(),
        }
    )
    profile_counts: Counter[str] = Counter()
    apba_id_counts: Counter[str] = Counter()
    row_count = 0
    successful_row_count = 0

    for batch_report in batch_reports:
        batch_path = Path(batch_report)
        for row in _load_rows(batch_path):
            if not isinstance(row, dict):
                continue
            row_count += 1
            if row.get("status") in {"completed", "skipped_unchanged"}:
                successful_row_count += 1
            profile_id = _clean(row.get("profile_id"))
            if not profile_id:
                continue
            normalized = normalize_profile_id(profile_id)
            profile_counts[profile_id] += 1
            if _clean(row.get("apba_id")):
                apba_id_counts[_clean(row.get("apba_id"))] += 1
            profile_values[normalized]["profile_id"].add(profile_id)
            for field in ("institution_name", "apba_id", "source_system", "source_url"):
                value = _clean(row.get(field))
                if value:
                    profile_values[normalized][field].add(value)

    conflicts: list[dict[str, Any]] = []
    profiles = dict(existing_payload.get("profiles") or {})
    for normalized_profile_id, values in sorted(profile_values.items()):
        profile_id = _single_value(values["profile_id"]) or normalized_profile_id
        existing_profile = profiles.get(profile_id) or profiles.get(normalized_profile_id) or {}
        if existing_profile and not isinstance(existing_profile, dict):
            conflicts.append(
                {
                    "profile_id": profile_id,
                    "field": "profile",
                    "reason": "existing_profile_not_object",
                }
            )
            existing_profile = {}
        profile_update = dict(existing_profile)
        for field in ("institution_name", "apba_id", "source_system", "source_url"):
            batch_value = _single_value(values[field])
            if field == "source_url" and len(values[field]) > 1:
                batch_value = _profile_source_url(_single_value(values["apba_id"]))
            if len(values[field]) > 1:
                if field == "source_url" and batch_value:
                    profile_update[field] = batch_value
                    continue
                conflicts.append(
                    {
                        "profile_id": profile_id,
                        "field": field,
                        "reason": "batch_profile_identity_conflict",
                        "values": sorted(values[field]),
                    }
                )
                continue
            existing_value = _clean(profile_update.get(field))
            if existing_value and batch_value and existing_value != batch_value and field in {"institution_name", "apba_id"}:
                conflicts.append(
                    {
                        "profile_id": profile_id,
                        "field": field,
                        "reason": "existing_profile_identity_conflict",
                        "existing": existing_value,
                        "batch": batch_value,
                    }
                )
                continue
            if batch_value:
                profile_update[field] = batch_value
        profile_update.setdefault("display_name", _display_name(profile_id, profile_update))
        profile_update["required_row_fields"] = list(required_row_fields)
        profile_update["max_upload_mb"] = int(profile_update.get("max_upload_mb") or 1000)
        profile_update["notes"] = "Generated or updated from batch quality report source identity."
        profiles[profile_id] = profile_update

    if "default-public-institution" not in profiles:
        profiles["default-public-institution"] = {
            "display_name": "Default public institution regulation batch",
            "required_row_fields": ["profile_id"],
            "max_upload_mb": 1000,
        }
    default_profile_id = _clean(existing_payload.get("default_profile_id")) or "default-public-institution"
    payload = {
        "default_profile_id": default_profile_id,
        "profiles": dict(sorted(profiles.items())),
    }
    registry_bytes = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    validation_error = ""
    registry_sha256 = ""
    try:
        registry = load_institution_profile_registry_from_bytes(registry_bytes)
        registry_sha256 = registry.sha256
    except Exception as exc:
        validation_error = str(exc)
        conflicts.append(
            {
                "profile_id": "",
                "field": "registry",
                "reason": "generated_registry_validation_error",
                "detail": validation_error,
            }
        )

    generated_profile_ids = set(profile_values)
    report = {
        "report_type": "institution_profile_registry_from_batch",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch_reports": [str(Path(path)) for path in batch_reports],
        "existing_registry": str(Path(existing_registry)) if existing_registry else None,
        "row_count": row_count,
        "successful_row_count": successful_row_count,
        "profile_count": len(payload["profiles"]),
        "batch_profile_count": len(generated_profile_ids),
        "new_profile_count": len(generated_profile_ids - existing_profile_ids),
        "updated_profile_count": len(generated_profile_ids & existing_profile_ids),
        "profile_counts": dict(sorted(profile_counts.items())),
        "apba_id_counts": dict(sorted(apba_id_counts.items())),
        "required_row_fields": list(required_row_fields),
        "registry_sha256": registry_sha256,
        "conflict_count": len(conflicts),
        "conflicts": conflicts[:50],
        "passed": not conflicts,
        "api_call_count": 0,
    }
    if out_registry and not validation_error and not conflicts:
        out_registry_path = Path(out_registry)
        out_registry_path.parent.mkdir(parents=True, exist_ok=True)
        out_registry_path.write_bytes(registry_bytes)
        report["out_registry"] = str(out_registry_path)
    if out_report_json:
        out_report_path = Path(out_report_json)
        out_report_path.parent.mkdir(parents=True, exist_ok=True)
        out_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md_path = Path(out_md)
        out_md_path.parent.mkdir(parents=True, exist_ok=True)
        out_md_path.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    batch = _load_json(path)
    return [row for row in batch.get("rows", []) or [] if isinstance(row, dict)]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _single_value(values: set[str]) -> str:
    return next(iter(values)) if len(values) == 1 else ""


def _display_name(profile_id: str, profile: dict[str, Any]) -> str:
    institution_name = _clean(profile.get("institution_name"))
    apba_id = _clean(profile.get("apba_id")).upper()
    if institution_name and apba_id:
        return f"{institution_name} ({apba_id}) PUBLIC_PORTAL internal regulation profile"
    if institution_name:
        return f"{institution_name} PUBLIC_PORTAL internal regulation profile"
    return f"{profile_id} institution profile"


def _profile_source_url(apba_id: str) -> str:
    cleaned = _clean(apba_id).upper()
    if not cleaned:
        return ""
    return f"https://example.org/regulations/item/itemOrganList.do?apbaId={cleaned}&reportFormRootNo=21110"


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Institution Profile Registry From Batch",
        "",
        f"- Passed: `{report.get('passed')}`",
        f"- Batch reports: `{report.get('batch_reports')}`",
        f"- Existing registry: `{report.get('existing_registry')}`",
        f"- Rows: `{report.get('row_count')}`",
        f"- Batch profiles: `{report.get('batch_profile_count')}`",
        f"- New profiles: `{report.get('new_profile_count')}`",
        f"- Updated profiles: `{report.get('updated_profile_count')}`",
        f"- Output registry: `{report.get('out_registry', '')}`",
        f"- Registry sha256: `{report.get('registry_sha256')}`",
        f"- Conflicts: `{report.get('conflict_count')}`",
        "",
        "## Profile Counts",
        "",
        "| Profile | Count |",
        "| --- | ---: |",
    ]
    for profile_id, count in (report.get("profile_counts") or {}).items():
        lines.append(f"| {_md_cell(profile_id)} | {count} |")
    if report.get("conflicts"):
        lines.extend(["", "## Conflicts", ""])
        for conflict in report["conflicts"]:
            lines.append(f"- `{conflict.get('profile_id')}` `{conflict.get('field')}` `{conflict.get('reason')}`")
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or extend institution profile registry evidence from batch reports.")
    parser.add_argument("--batch-report", action="append", required=True, help="Batch quality JSON report. Repeat to merge.")
    parser.add_argument("--existing-registry", help="Existing institution profile registry JSON to extend.")
    parser.add_argument(
        "--required-row-field",
        action="append",
        default=[],
        help="Required field for generated profiles. Defaults to strict PUBLIC_PORTAL source identity fields.",
    )
    parser.add_argument("--out-registry", help="Write generated registry JSON.")
    parser.add_argument("--out-report-json", help="Write generation evidence report JSON.")
    parser.add_argument("--out-md", help="Write generation evidence report Markdown.")
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_institution_profile_registry_from_batch(
        batch_reports=args.batch_report,
        existing_registry=args.existing_registry,
        required_row_fields=tuple(args.required_row_field or DEFAULT_REQUIRED_ROW_FIELDS),
        out_registry=args.out_registry,
        out_report_json=args.out_report_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    if args.fail_on_issue and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
