from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.check_parsing_goldset_table_drift import (
    check_parsing_goldset_table_drift,
    main,
)


class CheckParsingGoldsetTableDriftTests(unittest.TestCase):
    def test_passes_when_reports_match_current_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_bundle(root)

            report = check_parsing_goldset_table_drift(
                table_unit_review_summary_report=paths["summary"],
                table_count_transfer_validation_report=paths["transfer"],
                table_source_traceability_report=paths["traceability"],
                base_dir=root,
                out_json=root / "reports" / "drift.json",
                out_md=root / "reports" / "drift.md",
                generated_at="2026-07-10T00:00:00+00:00",
            )
            payload = json.loads((root / "reports" / "drift.json").read_text(encoding="utf-8"))
            markdown = (root / "reports" / "drift.md").read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(4, len(report["source_artifacts"]))
        self.assertTrue(all(artifact["sha256"] for artifact in report["source_artifacts"]))
        self.assertTrue(payload["passed"])
        self.assertIn("No drift detected", markdown)

    def test_blocks_source_count_lineage_and_embedded_row_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_bundle(root)
            old_summary = root / "old_summary.json"
            _write_json(old_summary, {"report_type": "old"})
            _write_json(
                paths["summary"],
                {
                    "report_type": "parsing_goldset_table_unit_review_summary",
                    "source_table_units_csv": "units.csv",
                    "row_count": 3,
                    "selected_unit_count": 3,
                    "document_count": 1,
                },
            )
            _write_json(
                paths["transfer"],
                {
                    "report_type": "parsing_goldset_table_count_transfer_validation",
                    "source_labels_csv": "labels.csv",
                    "source_table_review_summary_json": "old_summary.json",
                    "labels_document_count": 1,
                },
            )
            _write_json(
                paths["traceability"],
                {
                    "report_type": "table_review_source_traceability",
                    "source_table_review_batches_csv": "batches.csv",
                    "record_count": 2,
                    "batch_count": 2,
                    "batches": [
                        {
                            "table_review_batch_id": "batch-a",
                            "source_page_ranges": "2-2",
                        },
                        {
                            "table_review_batch_id": "batch-old",
                            "source_page_ranges": "1-1",
                        }
                    ],
                },
            )

            report = check_parsing_goldset_table_drift(
                table_unit_review_summary_report=paths["summary"],
                table_count_transfer_validation_report=paths["transfer"],
                table_source_traceability_report=paths["traceability"],
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("table-units-row-count-drift", codes)
        self.assertIn("labels-document-count-drift", codes)
        self.assertIn("transfer-summary-lineage-mismatch", codes)
        self.assertIn("traceability-record-count-drift", codes)
        self.assertIn("traceability-embedded-record-count-drift", codes)
        self.assertIn("traceability-source-page-range-drift", codes)

    def test_cli_can_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_bundle(root)
            _write_json(
                paths["summary"],
                {
                    "report_type": "parsing_goldset_table_unit_review_summary",
                    "source_table_units_csv": "missing.csv",
                    "row_count": 2,
                    "selected_unit_count": 2,
                    "document_count": 2,
                },
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--table-unit-review-summary-report",
                        str(paths["summary"]),
                        "--table-count-transfer-validation-report",
                        str(paths["transfer"]),
                        "--table-source-traceability-report",
                        str(paths["traceability"]),
                        "--base-dir",
                        str(root),
                        "--fail-on-issue",
                    ]
                )

        self.assertEqual(1, exit_code)
        self.assertIn('"ok": false', stdout.getvalue())

    def test_blocks_traceability_source_path_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_bundle(root)
            _write_json(
                paths["traceability"],
                {
                    "report_type": "table_review_source_traceability",
                    "source_table_review_batches_csv": "batches.csv",
                    "record_count": 1,
                    "batch_count": 1,
                    "batches": [
                        {
                            "table_review_batch_id": "batch-a",
                            "source_path": "old_source.pdf",
                            "source_page_ranges": "1-1",
                        }
                    ],
                },
            )

            report = check_parsing_goldset_table_drift(
                table_unit_review_summary_report=paths["summary"],
                table_count_transfer_validation_report=paths["transfer"],
                table_source_traceability_report=paths["traceability"],
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("traceability-source-path-drift", codes)

    def test_blocks_missing_table_unit_packet_linkage_for_batch_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_bundle(root)
            _write_csv(
                paths["batches"],
                [
                    {
                        "table_review_batch_id": "batch-a",
                        "document_id": "doc-a",
                        "source_path": "source.pdf",
                        "source_page_ranges": "1-1",
                    }
                ],
            )

            report = check_parsing_goldset_table_drift(
                table_unit_review_summary_report=paths["summary"],
                table_count_transfer_validation_report=paths["transfer"],
                table_source_traceability_report=paths["traceability"],
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("table-review-batch-unit-packet-link-missing", codes)

    def test_accepts_matching_table_unit_traceability_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = root / "units.csv"
            labels = root / "labels.csv"
            summary = root / "summary.json"
            transfer = root / "transfer.json"
            traceability = root / "traceability.json"
            _write_csv(
                units,
                [
                    {
                        "table_unit_key": "unit-a",
                        "document_id": "doc-a",
                        "source_path": "source.pdf",
                        "source_page_start": "1",
                        "source_page_end": "1",
                    }
                ],
            )
            _write_csv(labels, [{"document_id": "doc-a"}])
            _write_json(
                summary,
                {
                    "report_type": "parsing_goldset_table_unit_review_summary",
                    "source_table_units_csv": "units.csv",
                    "row_count": 1,
                    "selected_unit_count": 1,
                    "document_count": 1,
                },
            )
            _write_json(
                transfer,
                {
                    "report_type": "parsing_goldset_table_count_transfer_validation",
                    "source_labels_csv": "labels.csv",
                    "source_table_review_summary_json": "summary.json",
                    "labels_document_count": 1,
                },
            )
            _write_json(
                traceability,
                {
                    "report_type": "table_review_source_traceability",
                    "source_record_type": "table_unit",
                    "source_table_review_batches_csv": "units.csv",
                    "record_count": 1,
                    "batch_count": 1,
                    "batches": [
                        {
                            "table_review_batch_id": "unit-a",
                            "source_path": "source.pdf",
                            "source_page_ranges": "1-1",
                        }
                    ],
                },
            )

            report = check_parsing_goldset_table_drift(
                table_unit_review_summary_report=summary,
                table_count_transfer_validation_report=transfer,
                table_source_traceability_report=traceability,
                base_dir=root,
                generated_at="2026-07-10T00:00:00+00:00",
            )

        self.assertTrue(report["passed"])
        self.assertEqual([], report["findings"])


def _seed_bundle(root: Path) -> dict[str, Path]:
    units = root / "units.csv"
    labels = root / "labels.csv"
    batches = root / "batches.csv"
    summary = root / "summary.json"
    transfer = root / "transfer.json"
    traceability = root / "traceability.json"
    _write_csv(
        units,
        [
            {"table_unit_key": "unit-a", "document_id": "doc-a"},
            {"table_unit_key": "unit-b", "document_id": "doc-b"},
        ],
    )
    _write_csv(labels, [{"document_id": "doc-a"}, {"document_id": "doc-b"}])
    _write_csv(
        batches,
        [
            {
                "table_review_batch_id": "batch-a",
                "document_id": "doc-a",
                "source_path": "source.pdf",
                "source_page_ranges": "1-1",
                "table_unit_packet_csv": "units.csv",
            }
        ],
    )
    _write_json(
        summary,
        {
            "report_type": "parsing_goldset_table_unit_review_summary",
            "source_table_units_csv": "units.csv",
            "row_count": 2,
            "selected_unit_count": 2,
            "document_count": 2,
        },
    )
    _write_json(
        transfer,
        {
            "report_type": "parsing_goldset_table_count_transfer_validation",
            "source_labels_csv": "labels.csv",
            "source_table_review_summary_json": "summary.json",
            "labels_document_count": 2,
        },
    )
    _write_json(
        traceability,
        {
            "report_type": "table_review_source_traceability",
            "source_table_review_batches_csv": "batches.csv",
            "record_count": 1,
            "batch_count": 1,
            "batches": [
                {
                    "table_review_batch_id": "batch-a",
                    "source_path": "source.pdf",
                    "source_page_ranges": "1-1",
                }
            ],
        },
    )
    return {
        "units": units,
        "labels": labels,
        "batches": batches,
        "summary": summary,
        "transfer": transfer,
        "traceability": traceability,
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
