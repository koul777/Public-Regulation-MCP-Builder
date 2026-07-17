from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_profile_provenance_report import build_profile_provenance_report


class BuildProfileProvenanceReportTests(unittest.TestCase):
    def test_reports_unknown_generic_profile_against_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            registry = root / "institution_profiles.json"
            out_json = root / "profile.json"
            out_md = root / "profile.md"
            _write_json(
                batch,
                {
                    "rows": [
                        {
                            "filename": "a.hwp",
                            "institution_name": "Agency A",
                            "apba_id": "C0147",
                            "profile_id": "public_institution",
                        },
                        {
                            "filename": "b.pdf",
                            "institution_name": "Agency B",
                            "apba_id": "C0165",
                            "profile_id": "public_institution",
                        },
                    ]
                },
            )
            _write_json(
                registry,
                {
                    "default_profile_id": "public_portal-a",
                    "profiles": {
                        "public_portal-a": {
                            "display_name": "Agency A",
                            "institution_name": "Agency A",
                            "required_row_fields": ["profile_id"],
                        }
                    },
                },
            )

            report = build_profile_provenance_report(
                batch_report=batch,
                institution_profiles=registry,
                out_json=out_json,
                out_md=out_md,
            )
            markdown = out_md.read_text(encoding="utf-8")
            self.assertTrue(out_json.exists())

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["blocker_count"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual({"public_institution": 2}, report["unknown_profile_counts"])
        self.assertEqual(["generic-profile-only", "unknown-batch-profile-id"], sorted(f["code"] for f in report["findings"]))
        self.assertEqual(
            {"generic-profile-only": "warning", "unknown-batch-profile-id": "blocker"},
            {finding["code"]: finding["severity"] for finding in report["findings"]},
        )
        self.assertIn("Profile Provenance Report", markdown)
        self.assertIn("PUBLIC_PORTAL apba IDs", markdown)
        self.assertEqual(2, report["apba_id_count"])
        self.assertEqual({"C0147": 1, "C0165": 1}, report["apba_id_counts"])
        self.assertEqual({"hwp": 1, "pdf": 1}, report["file_type_counts"])

    def test_blocks_profile_apba_mismatch_against_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            registry = root / "institution_profiles.json"
            _write_json(
                batch,
                {
                    "rows": [
                        {
                            "filename": "wrong.pdf",
                            "institution_name": "Agency B",
                            "apba_id": "C0165",
                            "profile_id": "public_portal-c0147",
                        }
                    ]
                },
            )
            _write_json(
                registry,
                {
                    "profiles": {
                        "public_portal-c0147": {
                            "display_name": "PUBLIC_PORTAL C0147",
                            "institution_name": "Agency A",
                            "apba_id": "C0147",
                            "required_row_fields": ["profile_id", "apba_id"],
                        }
                    }
                },
            )

            report = build_profile_provenance_report(batch_report=batch, institution_profiles=registry)

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["blocker_count"])
        self.assertEqual(["profile-row-mismatch"], [finding["code"] for finding in report["findings"]])
        self.assertEqual(1, report["profile_mismatch_count"])
        self.assertEqual(["apba_id", "institution_name"], report["profile_mismatch_samples"][0]["mismatched_fields"])

    def test_default_public_profile_only_warns_as_generic_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            registry = root / "institution_profiles.json"
            _write_json(
                batch,
                {
                    "rows": [
                        {
                            "filename": "generic.hwp",
                            "institution_name": "Agency A",
                            "profile_id": "default-public-institution",
                        }
                    ]
                },
            )
            _write_json(
                registry,
                {
                    "default_profile_id": "default-public-institution",
                    "profiles": {
                        "default-public-institution": {
                            "display_name": "Default",
                            "required_row_fields": ["profile_id"],
                        }
                    },
                },
            )

            report = build_profile_provenance_report(batch_report=batch, institution_profiles=registry)

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["blocker_count"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual(["generic-profile-only"], [finding["code"] for finding in report["findings"]])

    def test_accepts_multiple_batch_reports_for_combined_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch_a = root / "batch-a.json"
            batch_b = root / "batch-b.json"
            registry = root / "institution_profiles.json"
            _write_json(
                batch_a,
                {
                    "rows": [
                        {
                            "filename": "a.hwp",
                            "institution_name": "Agency A",
                            "apba_id": "C0147",
                            "profile_id": "public_portal-c0147",
                        }
                    ]
                },
            )
            _write_json(
                batch_b,
                {
                    "rows": [
                        {
                            "filename": "b.pdf",
                            "institution_name": "Agency B",
                            "apba_id": "C0165",
                            "profile_id": "public_portal-c0165",
                        }
                    ]
                },
            )
            _write_json(
                registry,
                {
                    "profiles": {
                        "public_portal-c0147": {"institution_name": "Agency A", "apba_id": "C0147"},
                        "public_portal-c0165": {"institution_name": "Agency B", "apba_id": "C0165"},
                    }
                },
            )

            report = build_profile_provenance_report(
                batch_report=[batch_a, batch_b],
                institution_profiles=registry,
            )

        self.assertTrue(report["passed"])
        self.assertIsNone(report["batch_report"])
        self.assertEqual([str(batch_a), str(batch_b)], report["batch_reports"])
        self.assertEqual(2, report["row_count"])
        self.assertEqual({"public_portal-c0147": 1, "public_portal-c0165": 1}, report["batch_profile_counts"])

    def test_accepts_stratified_manifest_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.csv"
            registry = root / "institution_profiles.json"
            manifest.write_text(
                "\n".join(
                    [
                        "input_path,document_name,institution_name,apba_id,source_system,source_record_id,source_file_id,profile_id,selection_bucket",
                        "data/a.hwp,a.hwp,Agency A,C0147,PUBLIC_PORTAL,record-a,file-a,public_portal-c0147,hwp",
                        "data/b.hwpx,b.hwpx,Agency B,C0165,PUBLIC_PORTAL,record-b,file-b,public_portal-c0165,hwpx",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _write_json(
                registry,
                {
                    "profiles": {
                        "public_portal-c0147": {"institution_name": "Agency A", "apba_id": "C0147"},
                        "public_portal-c0165": {"institution_name": "Agency B", "apba_id": "C0165"},
                    }
                },
            )

            report = build_profile_provenance_report(
                batch_report=manifest,
                institution_profiles=registry,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["warning_count"])
        self.assertEqual({"hwp": 1, "hwpx": 1}, report["file_type_counts"])
        self.assertEqual({"public_portal-c0147": 1, "public_portal-c0165": 1}, report["batch_profile_counts"])


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
