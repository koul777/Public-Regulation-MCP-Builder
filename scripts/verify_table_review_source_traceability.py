from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


CSV_FIELDNAMES = [
    "batch_rank",
    "table_review_batch_id",
    "document_id",
    "source_path",
    "resolved_source_path",
    "extension",
    "source_exists",
    "source_page_ranges",
    "parsed_page_range_count",
    "max_source_page",
    "pdf_page_count",
    "page_count_status",
    "source_format_status",
    "source_format_detail",
    "issue_codes",
    "operator_next_action",
]

ISSUE_ORDER = {
    "source-file-missing": 0,
    "source-page-ranges-missing": 1,
    "source-page-range-invalid": 2,
    "pdf-reader-backend-unavailable": 3,
    "pdf-open-failed": 4,
    "hwpx-open-failed": 5,
    "hwp-signature-unrecognized": 6,
    "source-page-range-exceeds-pdf-page-count": 7,
    "source-page-range-not-verified-for-format": 8,
}

PAGE_RANGE_PATTERN = re.compile(r"^\s*(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?\s*$")
HWP_OLE_SIGNATURE = bytes.fromhex("d0 cf 11 e0 a1 b1 1a e1")


def verify_table_review_source_traceability(
    *,
    table_review_batches_csv: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    base_dir: Path | None = None,
    generated_at: str | None = None,
    require_page_count_verification: bool = False,
    allow_verified_nonpaginated_formats: bool = False,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    base_dir = (base_dir or Path.cwd()).resolve()
    batch_rows = _load_rows(table_review_batches_csv)
    source_record_type = _source_record_type(batch_rows[0])
    rows = [
        _inspect_row(
            row,
            base_dir=base_dir,
            require_page_count_verification=require_page_count_verification,
            allow_verified_nonpaginated_formats=allow_verified_nonpaginated_formats,
        )
        for row in batch_rows
    ]

    issue_counter = Counter(
        issue for row in rows for issue in _split_joined(row.get("issue_codes") or "")
    )
    status_counter = Counter(row.get("page_count_status") or "" for row in rows)
    format_status_counter = Counter(row.get("source_format_status") or "" for row in rows)
    next_action_counter = Counter(row.get("operator_next_action") or "" for row in rows)
    blocked_batch_count = sum(1 for row in rows if row.get("issue_codes"))
    report = {
        "report_type": "table_review_source_traceability",
        "generated_at": generated_at,
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "source_table_review_batches_csv": str(table_review_batches_csv),
        "source_record_type": source_record_type,
        "base_dir": str(base_dir),
        "require_page_count_verification": require_page_count_verification,
        "allow_verified_nonpaginated_formats": allow_verified_nonpaginated_formats,
        "record_count": len(rows),
        "batch_count": len(rows),
        "blocked_batch_count": blocked_batch_count,
        "issue_count": sum(issue_counter.values()),
        "issue_counts": dict(issue_counter),
        "page_count_status_counts": dict(status_counter),
        "source_format_status_counts": dict(format_status_counter),
        "operator_next_action_counts": dict(next_action_counter),
        "traceability_passed": blocked_batch_count == 0,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This source traceability check is read-only. It does not fill goldset labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, rows)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({**report, "batches": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, rows), encoding="utf-8")
    return report


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("table_review_batches_csv must contain at least one row.")
    columns = set(rows[0])
    batch_required = {"table_review_batch_id", "document_id", "source_path", "source_page_ranges"}
    unit_required = {"table_unit_key", "source_path", "source_page_start"}
    if not batch_required <= columns and not unit_required <= columns:
        required_text = "batch columns table_review_batch_id/document_id/source_path/source_page_ranges or unit columns table_unit_key/source_path/source_page_start"
        raise ValueError(f"table_review_batches_csv must include {required_text}.")
    return rows


def _inspect_row(
    row: dict[str, str],
    *,
    base_dir: Path,
    require_page_count_verification: bool = False,
    allow_verified_nonpaginated_formats: bool = False,
) -> dict[str, str]:
    source_path_text = str(row.get("source_path") or "").strip()
    source_path = Path(source_path_text) if source_path_text else Path()
    resolved_path = source_path if source_path.is_absolute() else base_dir / source_path
    source_exists = bool(source_path_text) and resolved_path.exists()
    page_ranges = _source_page_ranges(row)
    ranges, range_issues = _parse_page_ranges(page_ranges)
    extension = _extension(row, source_path)
    issues = list(range_issues)
    pdf_page_count = ""
    page_count_status = ""
    source_format_status = ""
    source_format_detail = ""

    if not source_exists:
        issues.append("source-file-missing")
        page_count_status = "source_missing"
        source_format_status = "source_missing"
    elif extension == ".pdf":
        page_count, error = _pdf_page_count(resolved_path)
        if error:
            issue_code = _pdf_open_issue_code(error)
            issues.append(issue_code)
            if issue_code == "pdf-reader-backend-unavailable":
                page_count_status = "pdf_reader_backend_unavailable"
                source_format_status = "pdf_reader_backend_unavailable"
            else:
                page_count_status = "pdf_open_failed"
                source_format_status = "pdf_open_failed"
            source_format_detail = error
        else:
            pdf_page_count = str(page_count)
            page_count_status = "verified_pdf"
            source_format_status = "verified_pdf"
            source_format_detail = f"page_count={page_count}"
            max_page = max((end for _, end in ranges), default=0)
            if max_page and max_page > page_count:
                issues.append("source-page-range-exceeds-pdf-page-count")
    elif extension == ".hwpx":
        source_format_status, source_format_detail, error = _hwpx_format_status(resolved_path)
        page_count_status = "not_checked_for_format"
        if error:
            issues.append(error)
    elif extension == ".hwp":
        source_format_status, source_format_detail, error = _hwp_format_status(resolved_path)
        page_count_status = "not_checked_for_format"
        if error:
            issues.append(error)
    else:
        page_count_status = "not_checked_for_format"
        source_format_status = "unsupported_format_not_checked"

    if (
        require_page_count_verification
        and source_exists
        and ranges
        and not issues
        and page_count_status != "verified_pdf"
    ):
        if allow_verified_nonpaginated_formats and _verified_nonpaginated_source(
            extension=extension,
            source_format_status=source_format_status,
        ):
            page_count_status = "verified_nonpaginated_source"
        else:
            issues.append("source-page-range-not-verified-for-format")

    issues = _ordered_issues(issues)
    max_source_page = max((end for _, end in ranges), default=0)
    operator_next_action = _operator_next_action(
        extension=extension,
        issues=issues,
        page_count_status=page_count_status,
        source_format_status=source_format_status,
    )
    return {
        "batch_rank": _record_rank(row),
        "table_review_batch_id": _record_id(row),
        "document_id": _document_id(row),
        "source_path": source_path_text,
        "resolved_source_path": str(resolved_path),
        "extension": extension,
        "source_exists": str(source_exists).lower(),
        "source_page_ranges": page_ranges,
        "parsed_page_range_count": str(len(ranges)),
        "max_source_page": str(max_source_page) if max_source_page else "",
        "pdf_page_count": pdf_page_count,
        "page_count_status": page_count_status,
        "source_format_status": source_format_status,
        "source_format_detail": source_format_detail,
        "issue_codes": "; ".join(issues),
        "operator_next_action": operator_next_action,
    }


def _source_record_type(row: dict[str, str]) -> str:
    if row.get("table_review_batch_id"):
        return "table_review_batch"
    return "table_unit"


def _record_rank(row: dict[str, str]) -> str:
    return str(row.get("batch_rank") or row.get("unit_rank") or "")


def _record_id(row: dict[str, str]) -> str:
    return str(row.get("table_review_batch_id") or row.get("table_unit_key") or "")


def _document_id(row: dict[str, str]) -> str:
    return str(row.get("document_id") or row.get("regulation_title") or "")


def _source_page_ranges(row: dict[str, str]) -> str:
    value = str(row.get("source_page_ranges") or "").strip()
    if value:
        return value
    start = str(row.get("source_page_start") or "").strip()
    end = str(row.get("source_page_end") or "").strip()
    if not start and not end:
        return ""
    return f"{start or '?'}-{end or start or '?'}"


def _parse_page_ranges(value: str) -> tuple[list[tuple[int, int]], list[str]]:
    tokens = [token.strip() for token in str(value or "").split(";") if token.strip()]
    if not tokens:
        return [], ["source-page-ranges-missing"]
    ranges: list[tuple[int, int]] = []
    issues: list[str] = []
    for token in tokens:
        match = PAGE_RANGE_PATTERN.match(token)
        if not match:
            issues.append("source-page-range-invalid")
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start <= 0 or end <= 0 or end < start:
            issues.append("source-page-range-invalid")
            continue
        ranges.append((start, end))
    return ranges, _ordered_issues(issues)


def _pdf_page_count(path: Path) -> tuple[int, str]:
    errors: list[str] = []
    try:
        import fitz  # PyMuPDF

        with fitz.open(path) as document:
            return int(document.page_count), ""
    except Exception as exc:  # pragma: no cover - exact backend errors vary by platform
        errors.append(f"fitz: {exc}")
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return len(reader.pages), ""
    except Exception as exc:  # pragma: no cover - exact backend errors vary by platform
        errors.append(f"pypdf: {exc}")
    try:
        import pdfplumber

        with pdfplumber.open(path) as document:
            return len(document.pages), ""
    except Exception as exc:  # pragma: no cover - exact backend errors vary by platform
        errors.append(f"pdfplumber: {exc}")
    return 0, "; ".join(errors)


def _pdf_open_issue_code(error: str) -> str:
    segments = _pdf_error_segments(error)
    if segments and all(_is_pdf_reader_backend_error(segment) for segment in segments):
        return "pdf-reader-backend-unavailable"
    return "pdf-open-failed"


def _pdf_error_segments(error: str) -> list[str]:
    return [segment.strip() for segment in str(error or "").split(";") if segment.strip()]


def _is_pdf_reader_backend_error(error: str) -> bool:
    lowered = str(error or "").lower()
    dependency_markers = (
        "no module named",
        "modulenotfounderror",
        "cannot import",
        "dll load failed",
        "importerror",
    )
    source_open_markers = (
        "broken document",
        "damaged document",
        "malformed pdf",
        "invalid pdf",
        "not a pdf",
        "startxref",
        "xref",
        "trailer",
        "eof marker",
        "cannot open",
        "failed to open",
        "file has not been decrypted",
    )
    package_path_markers = (
        "site-packages",
        "\\fitz\\",
        "/fitz/",
        "fitz\\__init__.py",
        "fitz/__init__.py",
        "\\pypdf\\",
        "/pypdf/",
        "pypdf\\__init__.py",
        "pypdf/__init__.py",
        "\\pdfplumber\\",
        "/pdfplumber/",
        "pdfplumber\\__init__.py",
        "pdfplumber/__init__.py",
        "\\cryptography\\",
        "/cryptography/",
        "cryptography\\__init__.py",
        "cryptography/__init__.py",
    )
    if any(marker in lowered for marker in source_open_markers):
        return False
    if any(marker in lowered for marker in dependency_markers):
        return True
    if "permission denied" in lowered and any(marker in lowered for marker in package_path_markers):
        return True
    return False


def _hwpx_format_status(path: Path) -> tuple[str, str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except Exception as exc:  # pragma: no cover - zip backend errors vary by platform
        return "hwpx_open_failed", str(exc), "hwpx-open-failed"
    has_core_xml = any(name.startswith("Contents/") and name.endswith(".xml") for name in names)
    if not has_core_xml:
        return "hwpx_open_failed", "missing Contents/*.xml entries", "hwpx-open-failed"
    return "verified_hwpx_zip", f"zip_entries={len(names)}", ""


def _hwp_format_status(path: Path) -> tuple[str, str, str]:
    signature = path.read_bytes()[: len(HWP_OLE_SIGNATURE)]
    if signature == HWP_OLE_SIGNATURE:
        return "verified_hwp_ole", "ole_compound_file_signature", ""
    return "hwp_signature_unrecognized", signature.hex(" "), "hwp-signature-unrecognized"


def _extension(row: dict[str, str], source_path: Path) -> str:
    extension = str(row.get("extension") or "").strip().lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    return extension or source_path.suffix.lower()


def _ordered_issues(values: Sequence[str]) -> list[str]:
    unique = {value for value in values if value}
    return sorted(unique, key=lambda value: (ISSUE_ORDER.get(value, 99), value))


def _operator_next_action(
    *,
    extension: str,
    issues: Sequence[str],
    page_count_status: str,
    source_format_status: str,
) -> str:
    issue_set = set(issues)
    if not issue_set:
        return "No action required."
    if "source-file-missing" in issue_set:
        return "Fix the source_path so the original regulation file can be opened before table review."
    if "source-page-ranges-missing" in issue_set:
        return "Add source_page_ranges for the table batch before source comparison review."
    if "source-page-range-invalid" in issue_set:
        return "Fix invalid source_page_ranges; use positive page numbers such as 3 or 3-5."
    if "pdf-reader-backend-unavailable" in issue_set:
        return (
            "Fix the Python PDF reader backend or run traceability in the packaged project environment; "
            "the source PDF has not been proven invalid."
        )
    if "pdf-open-failed" in issue_set:
        return "Replace or repair the PDF source file, then rerun source traceability."
    if "hwpx-open-failed" in issue_set:
        return "Replace or repair the HWPX ZIP/XML source file, then rerun source traceability."
    if "hwp-signature-unrecognized" in issue_set:
        return "Replace the HWP source with a valid OLE HWP file or convert it to a verified review source."
    if "source-page-range-exceeds-pdf-page-count" in issue_set:
        return "Fix table source page ranges so the maximum page does not exceed the PDF page count."
    if "source-page-range-not-verified-for-format" in issue_set:
        if extension in {".hwp", ".hwpx"} and source_format_status in {"verified_hwp_ole", "verified_hwpx_zip"}:
            return (
                "Export or render HWP/HWPX to a page-counted review source, then rerun traceability "
                "or keep the table accuracy claim blocked until manual source verification is recorded."
            )
        if page_count_status == "not_checked_for_format":
            return "Provide a page-counted review source for this format, then rerun source traceability."
    return "Resolve the listed issue_codes before claiming table source traceability."


def _verified_nonpaginated_source(*, extension: str, source_format_status: str) -> bool:
    return (
        extension in {".hwp", ".hwpx"}
        and source_format_status in {"verified_hwp_ole", "verified_hwpx_zip"}
    )


def _split_joined(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Table Review Source Traceability",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source table review batches CSV: `{report['source_table_review_batches_csv']}`",
        f"- Source record type: `{report['source_record_type']}`",
        f"- Base directory: `{report['base_dir']}`",
        f"- Require page count verification: {str(report.get('require_page_count_verification')).lower()}",
        "- Allow verified non-paginated formats: "
        f"{str(report.get('allow_verified_nonpaginated_formats')).lower()}",
        f"- Records: {int(report['record_count']):,}",
        f"- Blocked records: {int(report['blocked_batch_count']):,}",
        f"- Issue count: {int(report['issue_count']):,}",
        f"- Traceability passed: {str(report['traceability_passed']).lower()}",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "## Issue Counts",
        "",
        "| Issue | Count |",
        "| --- | ---: |",
    ]
    for issue, count in sorted(report["issue_counts"].items(), key=lambda item: ISSUE_ORDER.get(item[0], 99)):
        lines.append(f"| {_md(issue)} | {int(count):,} |")
    if not report["issue_counts"]:
        lines.append("| none | 0 |")

    lines.extend(
        [
            "",
            "## Operator Next Actions",
            "",
            "| Next action | Count |",
            "| --- | ---: |",
        ]
    )
    for action, count in sorted((report.get("operator_next_action_counts") or {}).items()):
        lines.append(f"| {_md(action)} | {int(count):,} |")

    lines.extend(
        [
            "",
            "## Source Format Status",
            "",
            "| Status | Count |",
            "| --- | ---: |",
        ]
    )
    for status, count in sorted((report.get("source_format_status_counts") or {}).items()):
        lines.append(f"| {_md(status)} | {int(count):,} |")

    lines.extend(
        [
            "",
            "## Records",
            "",
            "| Rank | Record | Document | Source exists | Pages | PDF pages | Page status | Format status | Issues | Next action |",
            "| ---: | --- | --- | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        pdf_pages = row["pdf_page_count"] or ""
        lines.append(
            f"| {_md(row['batch_rank'])} | {_md(row['table_review_batch_id'])} | "
            f"{_md(row['document_id'])} | {_md(row['source_exists'])} | "
            f"{_md(row['source_page_ranges'])} | {_md(pdf_pages)} | "
            f"{_md(row['page_count_status'])} | {_md(row['source_format_status'])} | "
            f"{_md(row['issue_codes'])} | {_md(row['operator_next_action'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-review-batches-csv", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-csv", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    parser.add_argument(
        "--require-page-count-verification",
        action="store_true",
        help="Block source records whose page ranges cannot be verified against a source page count.",
    )
    parser.add_argument(
        "--allow-verified-nonpaginated-formats",
        action="store_true",
        help=(
            "When page-count verification is required, allow verified HWP/HWPX sources "
            "as non-paginated originals instead of blocking them for missing PDF-style page counts."
        ),
    )
    parser.add_argument("--fail-on-issue", action="store_true")
    args = parser.parse_args(argv)

    report = verify_table_review_source_traceability(
        table_review_batches_csv=args.table_review_batches_csv,
        out_json=args.out_json,
        out_csv=args.out_csv,
        out_md=args.out_md,
        base_dir=args.base_dir,
        require_page_count_verification=args.require_page_count_verification,
        allow_verified_nonpaginated_formats=args.allow_verified_nonpaginated_formats,
    )
    print(
        json.dumps(
            {
                "ok": bool(report["traceability_passed"]),
                "record_count": report["record_count"],
                "batch_count": report["batch_count"],
                "blocked_batch_count": report["blocked_batch_count"],
                "issue_count": report["issue_count"],
                "require_page_count_verification": report["require_page_count_verification"],
                "allow_verified_nonpaginated_formats": report["allow_verified_nonpaginated_formats"],
                "out_json": str(args.out_json),
                "out_csv": str(args.out_csv),
                "out_md": str(args.out_md),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.fail_on_issue and not report["traceability_passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
