from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.verify_table_review_source_traceability import (
    _pdf_open_issue_code,
    _pdf_page_count,
    main,
    verify_table_review_source_traceability,
)


class VerifyTableReviewSourceTraceabilityTests(unittest.TestCase):
    def test_checks_source_files_and_pdf_page_ranges_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pdf(root / "source.pdf", page_count=2)
            _write_hwpx(root / "source.hwpx")
            _write_hwp(root / "source.hwp")
            source_csv = _seed_batches_csv(root)

            report = verify_table_review_source_traceability(
                table_review_batches_csv=source_csv,
                out_json=root / "reports" / "trace.json",
                out_csv=root / "reports" / "trace.csv",
                out_md=root / "reports" / "trace.md",
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "trace.json").read_text(encoding="utf-8"))
            with (root / "reports" / "trace.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "trace.md").read_text(encoding="utf-8")

        self.assertFalse(report["traceability_passed"])
        self.assertEqual(5, report["batch_count"])
        self.assertEqual(2, report["blocked_batch_count"])
        self.assertEqual({"source-page-range-exceeds-pdf-page-count": 1, "source-file-missing": 1}, report["issue_counts"])
        self.assertEqual(2, report["source_format_status_counts"]["verified_pdf"])
        self.assertEqual(1, report["source_format_status_counts"]["source_missing"])
        self.assertEqual("verified_pdf", rows[0]["page_count_status"])
        self.assertEqual("verified_pdf", rows[0]["source_format_status"])
        self.assertEqual("2", rows[0]["pdf_page_count"])
        self.assertEqual("", rows[0]["issue_codes"])
        self.assertIn("source-page-range-exceeds-pdf-page-count", rows[1]["issue_codes"])
        self.assertIn("source-file-missing", rows[2]["issue_codes"])
        self.assertEqual("not_checked_for_format", rows[3]["page_count_status"])
        self.assertEqual("verified_hwpx_zip", rows[3]["source_format_status"])
        self.assertEqual("verified_hwp_ole", rows[4]["source_format_status"])
        self.assertEqual(5, len(payload["batches"]))
        self.assertEqual(1, payload["source_format_status_counts"]["verified_hwpx_zip"])
        self.assertEqual(1, payload["source_format_status_counts"]["verified_hwp_ole"])
        self.assertIn("Source Format Status", markdown)
        self.assertIn("Table Review Source Traceability", markdown)

    def test_cli_returns_nonzero_with_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pdf(root / "source.pdf", page_count=1)
            source_csv = _seed_batches_csv(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--table-review-batches-csv",
                        str(source_csv),
                        "--out-json",
                        str(root / "reports" / "trace.json"),
                        "--out-csv",
                        str(root / "reports" / "trace.csv"),
                        "--out-md",
                        str(root / "reports" / "trace.md"),
                        "--base-dir",
                        str(root),
                        "--fail-on-issue",
                    ]
                )

        self.assertEqual(1, exit_code)
        self.assertIn('"ok": false', stdout.getvalue())

    def test_strict_page_count_verification_blocks_hwp_hwpx_unverified_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_hwpx(root / "source.hwpx")
            _write_hwp(root / "source.hwp")
            source_csv = _seed_hwp_hwpx_only_csv(root)

            report = verify_table_review_source_traceability(
                table_review_batches_csv=source_csv,
                out_json=root / "reports" / "trace.json",
                out_csv=root / "reports" / "trace.csv",
                out_md=root / "reports" / "trace.md",
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
                require_page_count_verification=True,
            )
            with (root / "reports" / "trace.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "trace.md").read_text(encoding="utf-8")

        self.assertFalse(report["traceability_passed"])
        self.assertTrue(report["require_page_count_verification"])
        self.assertEqual(2, report["blocked_batch_count"])
        self.assertEqual({"source-page-range-not-verified-for-format": 2}, report["issue_counts"])
        self.assertEqual("verified_hwpx_zip", rows[0]["source_format_status"])
        self.assertEqual("verified_hwp_ole", rows[1]["source_format_status"])
        self.assertIn("source-page-range-not-verified-for-format", rows[0]["issue_codes"])
        self.assertIn("Export or render HWP/HWPX to a page-counted review source", rows[0]["operator_next_action"])
        self.assertIn("Export or render HWP/HWPX to a page-counted review source", rows[1]["operator_next_action"])
        self.assertIn("Require page count verification: true", markdown)
        self.assertIn("Operator Next Actions", markdown)
        self.assertIn("Export or render HWP/HWPX to a page-counted review source", markdown)

    def test_strict_page_count_accepts_verified_nonpaginated_hwp_hwpx_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_hwpx(root / "source.hwpx")
            _write_hwp(root / "source.hwp")
            source_csv = _seed_hwp_hwpx_only_csv(root)

            report = verify_table_review_source_traceability(
                table_review_batches_csv=source_csv,
                out_json=root / "reports" / "trace.json",
                out_csv=root / "reports" / "trace.csv",
                out_md=root / "reports" / "trace.md",
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
                require_page_count_verification=True,
                allow_verified_nonpaginated_formats=True,
            )
            with (root / "reports" / "trace.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = (root / "reports" / "trace.md").read_text(encoding="utf-8")

        self.assertTrue(report["traceability_passed"])
        self.assertTrue(report["allow_verified_nonpaginated_formats"])
        self.assertEqual(0, report["blocked_batch_count"])
        self.assertEqual({}, report["issue_counts"])
        self.assertEqual("verified_nonpaginated_source", rows[0]["page_count_status"])
        self.assertEqual("verified_nonpaginated_source", rows[1]["page_count_status"])
        self.assertEqual("verified_hwpx_zip", rows[0]["source_format_status"])
        self.assertEqual("verified_hwp_ole", rows[1]["source_format_status"])
        self.assertEqual("", rows[0]["issue_codes"])
        self.assertIn("Allow verified non-paginated formats: true", markdown)

    def test_accepts_table_unit_review_packet_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_pdf(root / "source.pdf", page_count=3)
            source_csv = _seed_units_csv(root)

            report = verify_table_review_source_traceability(
                table_review_batches_csv=source_csv,
                out_json=root / "reports" / "trace.json",
                out_csv=root / "reports" / "trace.csv",
                out_md=root / "reports" / "trace.md",
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )
            with (root / "reports" / "trace.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertTrue(report["traceability_passed"])
        self.assertEqual("table_unit", report["source_record_type"])
        self.assertEqual("unit-a", rows[0]["table_review_batch_id"])
        self.assertEqual("1-2", rows[0]["source_page_ranges"])

    def test_pdf_page_count_falls_back_when_fitz_open_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            _write_pdf(source, page_count=2)

            def fail_open(_path: object) -> object:
                raise PermissionError("fitz unavailable")

            def read_with_fallback(_path: object) -> object:
                return SimpleNamespace(pages=[object(), object()])

            with patch.dict(
                sys.modules,
                {
                    "fitz": SimpleNamespace(open=fail_open),
                    "pypdf": SimpleNamespace(PdfReader=read_with_fallback),
                },
            ):
                page_count, error = _pdf_page_count(source)

        self.assertEqual(2, page_count)
        self.assertEqual("", error)

    def test_pdf_reader_backend_unavailable_is_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.pdf").write_bytes(b"%PDF-1.4\n")
            source_csv = _seed_units_csv(root)
            backend_error = (
                "fitz: [Errno 13] Permission denied: "
                "'C:\\Users\\dd\\anaconda3\\fitz\\__init__.py'; "
                "pypdf: No module named cryptography; "
                "pdfplumber: [Errno 13] Permission denied: "
                "'C:\\Users\\dd\\anaconda3\\pdfplumber\\__init__.py'"
            )

            with patch(
                "scripts.verify_table_review_source_traceability._pdf_page_count",
                return_value=(0, backend_error),
            ):
                report = verify_table_review_source_traceability(
                    table_review_batches_csv=source_csv,
                    out_json=root / "reports" / "trace.json",
                    out_csv=root / "reports" / "trace.csv",
                    out_md=root / "reports" / "trace.md",
                    base_dir=root,
                    generated_at="2026-07-10T00:00:00+00:00",
                )
            with (root / "reports" / "trace.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertFalse(report["traceability_passed"])
        self.assertEqual({"pdf-reader-backend-unavailable": 1}, report["issue_counts"])
        self.assertEqual("pdf_reader_backend_unavailable", rows[0]["page_count_status"])
        self.assertEqual("pdf_reader_backend_unavailable", rows[0]["source_format_status"])
        self.assertEqual("pdf-reader-backend-unavailable", rows[0]["issue_codes"])
        self.assertIn("Python PDF reader backend", rows[0]["operator_next_action"])

    def test_pdf_reader_backend_unavailable_does_not_mask_source_open_failure(self) -> None:
        mixed_error = (
            "fitz: cannot open broken document; "
            "pypdf: No module named cryptography; "
            "pdfplumber: No module named pdfminer"
        )

        self.assertEqual("pdf-open-failed", _pdf_open_issue_code(mixed_error))


def _seed_batches_csv(root: Path) -> Path:
    rows = [
        _row("1", "batch-1", "doc_a", "source.pdf", ".pdf", "1-2"),
        _row("2", "batch-2", "doc_a", "source.pdf", ".pdf", "3-3"),
        _row("3", "batch-3", "doc_b", "missing.pdf", ".pdf", "1-1"),
        _row("4", "batch-4", "doc_c", "source.hwpx", ".hwpx", "1-1"),
        _row("5", "batch-5", "doc_d", "source.hwp", ".hwp", "1-1"),
    ]
    path = root / "batches.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _seed_hwp_hwpx_only_csv(root: Path) -> Path:
    rows = [
        _row("1", "batch-hwpx", "doc_hwpx", "source.hwpx", ".hwpx", "1-1"),
        _row("2", "batch-hwp", "doc_hwp", "source.hwp", ".hwp", "1-1"),
    ]
    path = root / "hwp_hwpx_batches.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _seed_units_csv(root: Path) -> Path:
    rows = [
        {
            "unit_rank": "1",
            "table_unit_key": "unit-a",
            "regulation_title": "Rule A",
            "source_path": "source.pdf",
            "source_page_start": "1",
            "source_page_end": "2",
        }
    ]
    path = root / "units.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(
    rank: str,
    batch_id: str,
    document_id: str,
    source_path: str,
    extension: str,
    source_page_ranges: str,
) -> dict[str, str]:
    return {
        "batch_rank": rank,
        "table_review_batch_id": batch_id,
        "document_id": document_id,
        "source_path": source_path,
        "extension": extension,
        "source_page_ranges": source_page_ranges,
    }


def _write_pdf(path: Path, *, page_count: int) -> None:
    import fitz

    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    document.save(path)
    document.close()


def _write_hwpx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Contents/section0.xml", "<section />")


def _write_hwp(path: Path) -> None:
    path.write_bytes(bytes.fromhex("d0 cf 11 e0 a1 b1 1a e1") + b"\x00" * 32)


if __name__ == "__main__":
    unittest.main()
