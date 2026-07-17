from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.institution_profiles import load_institution_profile_registry, normalize_profile_id
from app.core.config import Settings
from app.core.tenant_access import tenant_storage_key


GENERIC_PROFILE_IDS = {"public_institution", "default-public-institution"}


def build_profile_provenance_report(
    *,
    batch_report: Path | Sequence[Path],
    institution_profiles: Path | None = None,
    runtime_data_dir: Path | None = None,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    batch_reports = list(batch_report) if isinstance(batch_report, (list, tuple)) else [batch_report]
    rows: list[dict[str, Any]] = []
    for report_path in batch_reports:
        rows.extend(_load_rows(Path(report_path)))
    registry_summary: dict[str, Any] = {}
    registry_profile_ids: set[str] = set()
    registry_profiles = {}
    if institution_profiles:
        registry = load_institution_profile_registry(institution_profiles)
        registry_summary = registry.summary()
        registry_profile_ids = set(registry.profiles)
        registry_profiles = registry.profiles
    profile_counts = Counter(str(row.get("profile_id") or "missing") for row in rows)
    institution_counts = Counter(str(row.get("institution_name") or "missing") for row in rows)
    apba_id_counts = Counter(str(row.get("apba_id") or "missing") for row in rows)
    file_type_counts = Counter(_file_type(row) for row in rows)
    unknown_profile_counts = {
        profile_id: count
        for profile_id, count in sorted(profile_counts.items())
        if institution_profiles and normalize_profile_id(profile_id) not in registry_profile_ids
    }
    unknown_by_institution: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        profile_id = str(row.get("profile_id") or "missing")
        if institution_profiles and normalize_profile_id(profile_id) in registry_profile_ids:
            continue
        institution = str(row.get("institution_name") or "missing")
        unknown_by_institution[institution][profile_id] += 1
    profile_mismatches = _profile_mismatch_samples(rows, registry_profiles)
    runtime_rows = _load_runtime_profile_rows(
        runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    runtime_profile_counts = Counter(str(row.get("profile_id") or "missing") for row in runtime_rows)
    runtime_unknown_profile_counts = {
        profile_id: count
        for profile_id, count in sorted(runtime_profile_counts.items())
        if profile_id == "missing"
        or (institution_profiles and normalize_profile_id(profile_id) not in registry_profile_ids)
    }
    runtime_profile_mismatches = _profile_mismatch_samples(runtime_rows, registry_profiles)
    findings = []
    if not institution_profiles:
        findings.append(
            {
                "severity": "warning",
                "code": "institution-profile-registry-missing",
                "detail": "No institution profile registry was provided.",
            }
        )
    elif unknown_profile_counts:
        findings.append(
            {
                "severity": "blocker",
                "code": "unknown-batch-profile-id",
                "detail": "Some batch rows reference profile ids absent from the institution registry.",
                "profile_counts": unknown_profile_counts,
            }
        )
    if profile_mismatches:
        findings.append(
            {
                "severity": "blocker",
                "code": "profile-row-mismatch",
                "detail": "Some batch rows use a profile whose PUBLIC_PORTAL apba_id or institution_name does not match the row.",
                "sample_count": len(profile_mismatches),
                "samples": profile_mismatches[:20],
            }
        )
    if runtime_unknown_profile_counts:
        findings.append(
            {
                "severity": "blocker",
                "code": "unknown-runtime-profile-id",
                "detail": "Approved runtime vectors reference profile ids absent from the institution registry.",
                "profile_counts": runtime_unknown_profile_counts,
            }
        )
    if runtime_profile_mismatches:
        findings.append(
            {
                "severity": "blocker",
                "code": "runtime-profile-row-mismatch",
                "detail": "Approved runtime vectors do not match their institution profile provenance.",
                "sample_count": len(runtime_profile_mismatches),
                "samples": runtime_profile_mismatches[:20],
            }
        )
    profile_keys = {key for key in profile_counts if key != "missing"}
    if profile_keys and profile_keys.issubset(GENERIC_PROFILE_IDS):
        findings.append(
            {
                "severity": "warning",
                "code": "generic-profile-only",
                "detail": "Batch rows use only a generic public institution profile id.",
            }
        )
    report = {
        "report_type": "profile_provenance",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch_report": str(batch_reports[0]) if len(batch_reports) == 1 else None,
        "batch_reports": [str(path) for path in batch_reports],
        "institution_profiles": str(institution_profiles) if institution_profiles else None,
        "runtime_data_dir": str(runtime_data_dir) if runtime_data_dir else None,
        "tenant_id": tenant_id,
        "runtime_profile_counts": dict(sorted(runtime_profile_counts.items())),
        "runtime_unknown_profile_counts": runtime_unknown_profile_counts,
        "runtime_profile_mismatch_count": len(runtime_profile_mismatches),
        "runtime_profile_mismatch_samples": runtime_profile_mismatches[:20],
        "runtime_profile_binding_passed": not runtime_unknown_profile_counts and not runtime_profile_mismatches,
        "row_count": len(rows),
        "institution_count": len([key for key in institution_counts if key != "missing"]),
        "apba_id_count": len([key for key in apba_id_counts if key != "missing"]),
        "apba_id_counts": dict(sorted(apba_id_counts.items())),
        "file_type_counts": dict(sorted(file_type_counts.items())),
        "batch_profile_counts": dict(sorted(profile_counts.items())),
        "registry_summary": registry_summary,
        "registry_profile_bindings": {
            profile_id: {
                "apba_id": getattr(profile, "apba_id", None),
                "institution_name": getattr(profile, "institution_name", None),
                "tenant_id": getattr(profile, "tenant_id", None),
            }
            for profile_id, profile in sorted(registry_profiles.items())
        },
        "matched_profile_ids": sorted(
            profile_id for profile_id in profile_counts if normalize_profile_id(profile_id) in registry_profile_ids
        ),
        "unknown_profile_counts": unknown_profile_counts,
        "unknown_profile_institution_samples": _unknown_samples(unknown_by_institution),
        "profile_mismatch_count": len(profile_mismatches),
        "profile_mismatch_samples": profile_mismatches[:20],
        "finding_count": len(findings),
        "blocker_count": sum(1 for item in findings if item["severity"] == "blocker"),
        "warning_count": sum(1 for item in findings if item["severity"] == "warning"),
        "findings": findings,
        "passed": not any(item["severity"] == "blocker" for item in findings),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _unknown_samples(unknown_by_institution: dict[str, dict[str, int]], *, limit: int = 20) -> list[dict[str, Any]]:
    samples = []
    for institution in sorted(unknown_by_institution)[:limit]:
        samples.append(
            {
                "institution_name": institution,
                "profile_counts": dict(sorted(unknown_by_institution[institution].items())),
            }
        )
    return samples


def _profile_mismatch_samples(
    rows: list[dict[str, Any]],
    registry_profiles: dict[str, Any],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not registry_profiles:
        return []
    samples: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        profile_id = str(row.get("profile_id") or "").strip()
        profile = registry_profiles.get(normalize_profile_id(profile_id))
        if profile is None:
            continue
        mismatched_fields: list[str] = []
        expected_apba_id = _normalize_identifier(getattr(profile, "apba_id", None))
        actual_apba_id = _normalize_identifier(row.get("apba_id"))
        if expected_apba_id and actual_apba_id and expected_apba_id != actual_apba_id:
            mismatched_fields.append("apba_id")
        expected_institution = _normalize_text(getattr(profile, "institution_name", None))
        actual_institution = _normalize_text(row.get("institution_name"))
        if expected_institution and actual_institution and expected_institution != actual_institution:
            mismatched_fields.append("institution_name")
        if mismatched_fields:
            samples.append(
                {
                    "row_index": index,
                    "filename": row.get("filename") or row.get("input_path") or "",
                    "profile_id": profile_id,
                    "mismatched_fields": mismatched_fields,
                    "expected_apba_id": getattr(profile, "apba_id", None),
                    "actual_apba_id": row.get("apba_id"),
                    "expected_institution_name": getattr(profile, "institution_name", None),
                    "actual_institution_name": row.get("institution_name"),
                }
            )
            if len(samples) >= limit:
                break
    return samples


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _file_type(row: dict[str, Any]) -> str:
    value = str(row.get("file_type") or "").strip().lower()
    if value:
        return value
    for field in ("filename", "document_name", "input_path"):
        suffix = Path(str(row.get(field) or "")).suffix.lower().lstrip(".")
        if suffix:
            return suffix
    return "unknown"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    batch = _load_json(path)
    return [row for row in batch.get("rows", []) or [] if isinstance(row, dict)]


def _load_runtime_profile_rows(
    runtime_data_dir: Path | None,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool | None,
) -> list[dict[str, Any]]:
    if runtime_data_dir is None:
        return []
    effective_dir = runtime_data_dir
    if tenant_storage_isolation is None:
        tenant_storage_isolation = Settings(data_dir=runtime_data_dir).tenant_storage_isolation
    if tenant_storage_isolation:
        effective_dir = runtime_data_dir / "tenants" / tenant_id
    path = effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        row = dict(metadata)
        for field in ("document_id", "chunk_id", "institution_name", "profile_id", "apba_id"):
            if record.get(field) not in (None, "") and field not in row:
                row[field] = record.get(field)
        row["filename"] = record.get("id") or row.get("chunk_id") or row.get("document_id") or ""
        rows.append(row)
    return rows


def _to_markdown(report: dict[str, Any]) -> str:
    registry = report.get("registry_summary") or {}
    lines = [
        "# Profile Provenance Report",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blockers: {report.get('blocker_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- Rows: {report.get('row_count')}",
        f"- Institutions: {report.get('institution_count')}",
        f"- PUBLIC_PORTAL apba IDs: {_format_counter(report.get('apba_id_counts') or {})}",
        f"- Batch profiles: {_format_counter(report.get('batch_profile_counts') or {})}",
        f"- Registry profiles: {registry.get('profile_count', 0)}",
        f"- Registry sha256: `{registry.get('sha256') or ''}`",
        f"- Unknown profiles: {_format_counter(report.get('unknown_profile_counts') or {})}",
        f"- Runtime profiles: {_format_counter(report.get('runtime_profile_counts') or {})}",
        f"- Runtime unknown profiles: {_format_counter(report.get('runtime_unknown_profile_counts') or {})}",
        f"- Runtime profile mismatches: {report.get('runtime_profile_mismatch_count', 0)}",
        f"- Runtime binding passed: `{str(report.get('runtime_profile_binding_passed')).lower()}`",
        f"- API calls: {report.get('api_call_count')}",
        "",
        "## File Types",
        "",
        "| Type | Count |",
        "| --- | ---: |",
    ]
    for file_type, count in (report.get("file_type_counts") or {}).items():
        lines.append(f"| {_md_cell(file_type)} | {count} |")
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for finding in report["findings"]:
            lines.append(f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}")
    samples = report.get("unknown_profile_institution_samples") or []
    if samples:
        lines.extend(["", "## Unknown Profile Samples", "", "| Institution | Profiles |", "| --- | --- |"])
        for sample in samples:
            lines.append(
                f"| {_md_cell(sample.get('institution_name'))} | {_md_cell(_format_counter(sample.get('profile_counts') or {}))} |"
            )
    mismatch_samples = report.get("profile_mismatch_samples") or []
    if mismatch_samples:
        lines.extend(
            [
                "",
                "## Profile Mismatch Samples",
                "",
                "| Row | Profile | Fields | Expected apba_id | Actual apba_id | Filename |",
                "| ---: | --- | --- | --- | --- | --- |",
            ]
        )
        for sample in mismatch_samples:
            lines.append(
                "| "
                f"{sample.get('row_index')} | "
                f"{_md_cell(sample.get('profile_id'))} | "
                f"{_md_cell(', '.join(sample.get('mismatched_fields') or []))} | "
                f"{_md_cell(sample.get('expected_apba_id'))} | "
                f"{_md_cell(sample.get('actual_apba_id'))} | "
                f"{_md_cell(sample.get('filename'))} |"
            )
    return "\n".join(lines).rstrip() + "\n"


def _format_counter(counter: dict[str, Any]) -> str:
    if not counter:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items()))


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build profile provenance evidence for a public-institution batch.")
    parser.add_argument("--batch-report", action="append", required=True)
    parser.add_argument("--institution-profiles", default=None)
    parser.add_argument("--runtime-data-dir", default=None)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    report = build_profile_provenance_report(
        batch_report=[Path(path) for path in args.batch_report],
        institution_profiles=Path(args.institution_profiles) if args.institution_profiles else None,
        runtime_data_dir=Path(args.runtime_data_dir) if args.runtime_data_dir else None,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=False if args.flat_storage else None,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
