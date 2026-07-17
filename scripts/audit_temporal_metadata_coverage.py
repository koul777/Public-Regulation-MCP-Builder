from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.tenant_access import tenant_storage_key
from app.services.regulation_catalog_service import read_regulation_metadata


TEMPORAL_FIELDS = (
    "revision_date",
    "effective_date",
    "revision_history",
    "valid_from",
    "valid_to",
    "supplementary_identifier_date",
    "article_effective_overrides",
    "article_validity_windows",
)
LIFECYCLE_FIELDS = (
    "regulation_id",
    "regulation_version",
    "approval_status",
    "effective_from",
    "effective_to",
    "repealed_at",
)


def build_temporal_metadata_coverage_report(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    profile_id: str | None = None,
    tenant_storage_isolation: bool | None = None,
    sample_limit: int = 25,
    as_of_date: str | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    effective_dir = _effective_runtime_dir(
        data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    records = _load_vector_records(effective_dir, tenant_id=tenant_id)
    rows = [_record_row(record) for record in records]
    requested_profile_id = str(profile_id or "").strip()
    if requested_profile_id:
        rows = [row for row in rows if row.get("profile_id") == requested_profile_id]
    document_defaults = _document_lifecycle_defaults(rows)
    for row in rows:
        document_default = document_defaults.get(str(row.get("document_id") or "").strip())
        if document_default:
            _apply_document_lifecycle_defaults(row, document_default)
    parsed_as_of = _parse_date(as_of_date)
    effective_as_of = parsed_as_of or date.today()
    with_temporal = [row for row in rows if row["has_temporal_metadata"]]
    without_temporal = [row for row in rows if not row["has_temporal_metadata"]]
    lifecycle_incomplete = [row for row in rows if not row["lifecycle_complete"]]
    latest_selection = _latest_selection(rows, as_of=effective_as_of)
    by_chunk_type = _coverage_by(rows, "chunk_type")
    by_temporal_field = {
        field: sum(1 for row in rows if row["metadata"].get(field) not in (None, "", [], {}))
        for field in TEMPORAL_FIELDS
    }
    inheritance_opportunities = _inheritance_opportunities(rows)
    findings = []
    if as_of_date and parsed_as_of is None:
        findings.append(
            {
                "severity": "blocker",
                "code": "invalid-as-of-date",
                "detail": f"The requested as_of_date is not a valid ISO date: {as_of_date}.",
            }
        )
    if not rows:
        findings.append(
            {
                "severity": "blocker",
                "code": "temporal-vector-records-missing",
                "detail": "No approved vector records were available for temporal metadata audit.",
            }
        )
    elif not with_temporal:
        findings.append(
            {
                "severity": "warning",
                "code": "temporal-metadata-not-evidenced",
                "detail": "No approved vector records contain temporal metadata.",
            }
        )
    elif without_temporal:
        findings.append(
            {
                "severity": "warning",
                "code": "temporal-metadata-partial",
                "detail": "Only part of the approved runtime contains temporal metadata.",
            }
        )
    if inheritance_opportunities["candidate_missing_record_count"] > 0:
        findings.append(
            {
                "severity": "info",
                "code": "temporal-inheritance-opportunity",
                "detail": "Some records without temporal metadata share a document/regulation scope with records that do have temporal metadata.",
            }
        )
    if lifecycle_incomplete:
        findings.append(
            {
                "severity": "blocker",
                "code": "regulation-lifecycle-incomplete",
                "detail": "Approved vector records are missing one or more required regulation lifecycle fields.",
                "sample_count": min(len(lifecycle_incomplete), sample_limit),
            }
        )
    if latest_selection["duplicate_active_version_group_count"] > 0:
        findings.append(
            {
                "severity": "blocker",
                "code": "duplicate-active-regulation-version",
                "detail": "More than one active document is present for the same regulation id and version.",
                "group_count": latest_selection["duplicate_active_version_group_count"],
            }
        )
    report = {
        "report_type": "temporal_metadata_coverage",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_data_dir": str(data_dir),
        "effective_runtime_data_dir": str(effective_dir),
        "tenant_id": tenant_id,
        "profile_id": requested_profile_id or None,
        "temporal_fields": list(TEMPORAL_FIELDS),
        "lifecycle_fields": list(LIFECYCLE_FIELDS),
        "as_of_date": effective_as_of.isoformat(),
        "record_count": len(rows),
        "with_temporal_metadata_count": len(with_temporal),
        "without_temporal_metadata_count": len(without_temporal),
        "temporal_metadata_ratio": _ratio(len(with_temporal), len(rows)),
        "by_chunk_type": by_chunk_type,
        "by_temporal_field": by_temporal_field,
        "inheritance_opportunities": inheritance_opportunities,
        "missing_samples": _missing_samples(without_temporal, limit=sample_limit),
        "lifecycle_complete_count": len(rows) - len(lifecycle_incomplete),
        "lifecycle_incomplete_count": len(lifecycle_incomplete),
        "regulation_group_count": latest_selection["regulation_group_count"],
        "duplicate_active_version_group_count": latest_selection["duplicate_active_version_group_count"],
        "latest_only_passed": latest_selection["latest_only_passed"] and not lifecycle_incomplete,
        "latest_selected_record_count": latest_selection["latest_selected_record_count"],
        "non_latest_record_count": latest_selection["non_latest_record_count"],
        "latest_selection_samples": latest_selection["samples"],
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


def _effective_runtime_dir(data_dir: Path, *, tenant_id: str, tenant_storage_isolation: bool | None) -> Path:
    if tenant_storage_isolation is None:
        tenant_storage_isolation = Settings(data_dir=data_dir).tenant_storage_isolation
    if tenant_storage_isolation:
        return data_dir / "tenants" / tenant_id
    return data_dir


def _load_vector_records(effective_dir: Path, *, tenant_id: str) -> list[dict[str, Any]]:
    vector_path = effective_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    if not vector_path.is_file():
        return []
    rows = []
    for line in vector_path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _record_row(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    metadata = dict(metadata)
    for field in (*LIFECYCLE_FIELDS, "profile_id", "tenant_id", "apba_id"):
        if record.get(field) not in (None, "") and field not in metadata:
            metadata[field] = record.get(field)
    if "approval_status" not in metadata and metadata.get("regulation_status") not in (None, ""):
        metadata["approval_status"] = metadata.get("regulation_status")
    if "approval_status" not in metadata and record.get("approval_status") not in (None, ""):
        metadata["approval_status"] = record.get("approval_status")
    normalized_lifecycle = read_regulation_metadata(record)
    for field, value in (
        ("regulation_id", normalized_lifecycle.regulation_id),
        ("regulation_version", normalized_lifecycle.version),
        ("effective_from", normalized_lifecycle.effective_from.isoformat() if normalized_lifecycle.effective_from else None),
        ("effective_to", normalized_lifecycle.effective_to.isoformat() if normalized_lifecycle.effective_to else None),
        ("repealed_at", normalized_lifecycle.repealed_at.isoformat() if normalized_lifecycle.repealed_at else None),
    ):
        if field not in metadata or metadata.get(field) in (None, ""):
            metadata[field] = value
    temporal_values = {
        field: metadata.get(field)
        for field in TEMPORAL_FIELDS
        if metadata.get(field) not in (None, "", [], {})
    }
    return {
        "record_id": str(record.get("id") or ""),
        "document_id": str(record.get("document_id") or metadata.get("document_id") or ""),
        "chunk_id": str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
        "chunk_type": str(metadata.get("chunk_type") or record.get("chunk_type") or "unknown"),
        "institution_name": str(metadata.get("institution_name") or ""),
        "regulation_title": str(metadata.get("regulation_title") or metadata.get("document_name") or ""),
        "article_no": str(metadata.get("article_no") or ""),
        "article_title": str(metadata.get("article_title") or ""),
        "source_page_start": metadata.get("source_page_start"),
        "metadata": metadata,
        "temporal_values": temporal_values,
        "has_temporal_metadata": bool(temporal_values),
        "regulation_id": str(metadata.get("regulation_id") or ""),
        "regulation_version": str(metadata.get("regulation_version") or ""),
        "approval_status": str(metadata.get("approval_status") or metadata.get("regulation_status") or ""),
        "effective_from": metadata.get("effective_from"),
        "effective_to": metadata.get("effective_to"),
        "repealed_at": metadata.get("repealed_at"),
        "profile_id": str(metadata.get("profile_id") or ""),
        "lifecycle_complete": _lifecycle_complete(metadata),
    }


def _document_lifecycle_defaults(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    documents: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        document_id = str(row.get("document_id") or "").strip()
        if document_id:
            documents[document_id].append(row)
    defaults: dict[str, dict[str, Any]] = {}
    for document_id, doc_rows in documents.items():
        stable_rows = [
            row
            for row in doc_rows
            if any(char.isdigit() for char in str(row.get("regulation_id") or "").strip())
        ]
        if not stable_rows:
            continue
        representative = max(
            stable_rows,
            key=lambda row: (
                _parse_date(row.get("effective_from")) or date.min,
                _version_key(str(row.get("regulation_version") or "")),
                str(row.get("chunk_id") or "").casefold(),
            ),
        )
        defaults[document_id] = {
            "regulation_id": str(representative.get("regulation_id") or ""),
            "regulation_version": str(representative.get("regulation_version") or ""),
            "approval_status": representative.get("approval_status") or representative.get("regulation_status"),
            "regulation_status": representative.get("regulation_status") or representative.get("approval_status"),
            "effective_from": representative.get("effective_from"),
            "effective_to": representative.get("effective_to"),
            "repealed_at": representative.get("repealed_at"),
        }
    return defaults


def _apply_document_lifecycle_defaults(row: dict[str, Any], defaults: dict[str, Any]) -> None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else None

    def _apply(field: str, value: Any) -> None:
        row[field] = value
        if metadata is not None:
            metadata[field] = value

    regulation_id = str(row.get("regulation_id") or "").strip()
    if not regulation_id or not any(char.isdigit() for char in regulation_id):
        _apply("regulation_id", defaults.get("regulation_id"))
    version = str(row.get("regulation_version") or "").strip()
    if not version or not any(char.isdigit() for char in version):
        _apply("regulation_version", defaults.get("regulation_version"))
    if _parse_date(row.get("effective_from")) is None and defaults.get("effective_from") is not None:
        _apply("effective_from", defaults.get("effective_from"))
    for field in ("approval_status", "regulation_status", "effective_to", "repealed_at"):
        if field not in row or row.get(field) in (None, ""):
            _apply(field, defaults.get(field))
    row["lifecycle_complete"] = _lifecycle_complete(metadata or {})


def _lifecycle_complete(metadata: dict[str, Any]) -> bool:
    if any(field not in metadata for field in LIFECYCLE_FIELDS):
        return False
    if not str(metadata.get("regulation_id") or "").strip():
        return False
    if not str(metadata.get("regulation_version") or "").strip():
        return False
    if str(metadata.get("approval_status") or "").strip().casefold() != "approved":
        return False
    if _parse_date(metadata.get("effective_from")) is None:
        return False
    for field in ("effective_to", "repealed_at"):
        raw = metadata.get(field)
        if raw not in (None, "") and _parse_date(raw) is None:
            return False
    return True


def _latest_selection(rows: list[dict[str, Any]], *, as_of: date) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    active_version_documents: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        regulation_id = row["regulation_id"].strip()
        version = row["regulation_version"].strip()
        if not regulation_id or not version or row["approval_status"].casefold() != "approved":
            continue
        effective_from = _parse_date(row["effective_from"])
        effective_to = _parse_date(row["effective_to"])
        repealed_at = _parse_date(row["repealed_at"])
        if effective_from is None or effective_from > as_of:
            continue
        if effective_to is not None and as_of > effective_to:
            continue
        if repealed_at is not None and as_of >= repealed_at:
            continue
        groups[regulation_id.casefold()].append(row)
        document_id = row["document_id"]
        active_version_documents[(regulation_id.casefold(), version.casefold())].add(document_id)
    latest_keys: set[tuple[str, str]] = set()
    for regulation_id, group in groups.items():
        winner = max(
            group,
            key=lambda row: (
                _parse_date(row["effective_from"]) or date.min,
                _version_key(row["regulation_version"]),
                row["document_id"].casefold(),
            ),
        )
        winner_version = winner["regulation_version"].casefold()
        latest_keys.update(
            (row["document_id"], row["chunk_id"])
            for row in group
            if row["regulation_version"].casefold() == winner_version
        )
    eligible_keys = {
        (row["document_id"], row["chunk_id"])
        for group in groups.values()
        for row in group
    }
    duplicate_count = sum(
        1 for documents in active_version_documents.values() if len({value for value in documents if value}) > 1
    )
    samples = [
        {"regulation_id": regulation_id, "record_count": len(group)}
        for regulation_id, group in sorted(groups.items())
    ][:25]
    selected_count = len(latest_keys)
    return {
        "regulation_group_count": len(groups),
        "duplicate_active_version_group_count": duplicate_count,
        "latest_selected_record_count": selected_count,
        "non_latest_record_count": max(len(eligible_keys) - selected_count, 0),
        "latest_only_passed": not duplicate_count and (not eligible_keys or bool(latest_keys)),
        "samples": samples,
    }


def _version_key(value: Any) -> tuple[Any, ...]:
    parts = []
    for token in str(value or "").replace("-", ".").split("."):
        parts.append((0, int(token)) if token.isdigit() else (1, token.casefold()))
    return tuple(parts)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _coverage_by(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        key = str(row.get(field) or "missing")
        counters[key]["total"] += 1
        counters[key]["with_temporal" if row["has_temporal_metadata"] else "without_temporal"] += 1
    return {
        key: {
            "total": counter["total"],
            "with_temporal": counter["with_temporal"],
            "without_temporal": counter["without_temporal"],
            "temporal_metadata_ratio": _ratio(counter["with_temporal"], counter["total"]),
        }
        for key, counter in sorted(counters.items())
    }


def _inheritance_opportunities(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_scope: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scope[(row["document_id"], row["regulation_title"])].append(row)
    scopes = []
    for (document_id, regulation_title), scope_rows in by_scope.items():
        with_count = sum(1 for row in scope_rows if row["has_temporal_metadata"])
        without_count = len(scope_rows) - with_count
        if with_count and without_count:
            scopes.append(
                {
                    "document_id": document_id,
                    "regulation_title": regulation_title,
                    "with_temporal": with_count,
                    "without_temporal": without_count,
                    "total": len(scope_rows),
                }
            )
    scopes.sort(key=lambda item: (-int(item["without_temporal"]), item["document_id"], item["regulation_title"]))
    return {
        "candidate_scope_count": len(scopes),
        "candidate_missing_record_count": sum(int(item["without_temporal"]) for item in scopes),
        "top_scopes": scopes[:20],
    }


def _missing_samples(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    keys = (
        "document_id",
        "chunk_id",
        "chunk_type",
        "institution_name",
        "regulation_title",
        "article_no",
        "article_title",
        "source_page_start",
    )
    return [{key: row.get(key) for key in keys} for row in rows[:limit]]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Temporal Metadata Coverage",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Runtime: `{report.get('effective_runtime_data_dir')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blockers: {report.get('blocker_count')}",
        f"- Warnings: {report.get('warning_count')}",
        f"- Records: {report.get('record_count')}",
        f"- With temporal metadata: {report.get('with_temporal_metadata_count')}",
        f"- Without temporal metadata: {report.get('without_temporal_metadata_count')}",
        f"- Temporal metadata ratio: {report.get('temporal_metadata_ratio')}",
        f"- API calls: {report.get('api_call_count')}",
        "",
        "## Findings",
        "",
    ]
    for finding in report.get("findings") or []:
        lines.append(f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}")
    lines.extend(["", "## By Chunk Type", "", "| Chunk type | Total | With temporal | Without temporal | Ratio |", "| --- | ---: | ---: | ---: | ---: |"])
    for chunk_type, row in (report.get("by_chunk_type") or {}).items():
        lines.append(
            f"| {_md_cell(chunk_type)} | {row.get('total')} | {row.get('with_temporal')} | {row.get('without_temporal')} | {row.get('temporal_metadata_ratio')} |"
        )
    lines.extend(["", "## Temporal Fields", "", "| Field | Count |", "| --- | ---: |"])
    for field, count in (report.get("by_temporal_field") or {}).items():
        lines.append(f"| {_md_cell(field)} | {count} |")
    opportunities = report.get("inheritance_opportunities") or {}
    lines.extend(
        [
            "",
            "## Inheritance Opportunities",
            "",
            f"- Candidate scopes: {opportunities.get('candidate_scope_count')}",
            f"- Candidate missing records: {opportunities.get('candidate_missing_record_count')}",
            "",
            "| Document | Regulation | With temporal | Without temporal |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for row in opportunities.get("top_scopes") or []:
        lines.append(
            f"| {_md_cell(row.get('document_id'))} | {_md_cell(row.get('regulation_title'))} | {row.get('with_temporal')} | {row.get('without_temporal')} |"
        )
    lines.extend(["", "## Missing Samples", "", "| Chunk | Type | Regulation | Article | Page |", "| --- | --- | --- | --- | ---: |"])
    for row in report.get("missing_samples") or []:
        article = " ".join(value for value in [row.get("article_no"), row.get("article_title")] if value)
        lines.append(
            f"| {_md_cell(row.get('chunk_id'))} | {_md_cell(row.get('chunk_type'))} | {_md_cell(row.get('regulation_title'))} | {_md_cell(article)} | {_md_cell(row.get('source_page_start'))} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit temporal metadata coverage in approved vector records.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile-id", default=None)
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=25)
    parser.add_argument("--as-of-date", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-blocker", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_temporal_metadata_coverage_report(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        sample_limit=args.sample_limit,
        as_of_date=args.as_of_date,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_blocker and report["blocker_count"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
