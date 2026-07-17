from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.core.institution_profiles import load_institution_profile_registry
from scripts.build_institution_profile_registry_from_batch import build_institution_profile_registry_from_batch
from scripts.build_profile_provenance_report import build_profile_provenance_report


class BuildInstitutionProfileRegistryFromBatchTests(unittest.TestCase):
    def test_builds_registry_that_satisfies_profile_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            registry_out = root / "institution_profiles.json"
            report_out = root / "registry_report.json"
            _write_batch(batch)

            report = build_institution_profile_registry_from_batch(
                batch_reports=[batch],
                out_registry=registry_out,
                out_report_json=report_out,
            )
            registry = load_institution_profile_registry(registry_out)
            provenance = build_profile_provenance_report(batch_report=batch, institution_profiles=registry_out)
            report_written = report_out.exists()

        self.assertTrue(report["passed"])
        self.assertTrue(report_written)
        self.assertEqual(2, report["batch_profile_count"])
        self.assertEqual(3, report["profile_count"])
        self.assertEqual("C0147", registry.resolve("public_portal-c0147", strict=True).apba_id)
        self.assertIn("apba_id", registry.resolve("public_portal-c0147", strict=True).required_row_fields)
        self.assertTrue(provenance["passed"])
        self.assertEqual({}, provenance["unknown_profile_counts"])

    def test_report_registry_sha_matches_written_registry_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            registry_out = root / "institution_profiles.json"
            report_out = root / "registry_report.json"
            _write_batch(batch)

            report = build_institution_profile_registry_from_batch(
                batch_reports=[batch],
                out_registry=registry_out,
                out_report_json=report_out,
            )
            written_report = json.loads(report_out.read_text(encoding="utf-8"))
            written_registry_sha = hashlib.sha256(registry_out.read_bytes()).hexdigest()

        self.assertTrue(report["passed"])
        self.assertEqual(written_registry_sha, report["registry_sha256"])
        self.assertEqual(written_registry_sha, written_report["registry_sha256"])

    def test_extends_existing_registry_with_missing_batch_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            existing = root / "existing.json"
            registry_out = root / "extended.json"
            _write_batch(batch)
            existing.write_text(
                json.dumps(
                    {
                        "default_profile_id": "default-public-institution",
                        "profiles": {
                            "default-public-institution": {"required_row_fields": ["profile_id"]},
                            "public_portal-c0147": {
                                "institution_name": "Agency A",
                                "required_row_fields": ["profile_id"],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = build_institution_profile_registry_from_batch(
                batch_reports=[batch],
                existing_registry=existing,
                out_registry=registry_out,
            )
            registry = load_institution_profile_registry(registry_out)

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["new_profile_count"])
        self.assertEqual(1, report["updated_profile_count"])
        self.assertEqual("C0147", registry.resolve("public_portal-c0147", strict=True).apba_id)
        self.assertEqual("C0165", registry.resolve("public_portal-c0165", strict=True).apba_id)

    def test_generalizes_multiple_document_urls_for_same_public_portal_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            registry_out = root / "profiles.json"
            first = _row(profile_id="public_portal-c0147", institution_name="Agency A", apba_id="C0147")
            second = {
                **_row(profile_id="public_portal-c0147", institution_name="Agency A", apba_id="C0147"),
                "source_url": "https://example.org/regulations/item/itemBoard21110.do?apbaId=C0147&idx=2",
            }
            _write_json(batch, {"rows": [first, second]})

            report = build_institution_profile_registry_from_batch(
                batch_reports=[batch],
                out_registry=registry_out,
            )
            registry = load_institution_profile_registry(registry_out)

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["conflict_count"])
        self.assertEqual(
            "https://example.org/regulations/item/itemOrganList.do?apbaId=C0147&reportFormRootNo=21110",
            registry.resolve("public_portal-c0147", strict=True).source_url,
        )

    def test_blocks_conflicting_identity_for_same_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch.json"
            _write_json(
                batch,
                {
                    "rows": [
                        _row(profile_id="public_portal-c0147", institution_name="Agency A", apba_id="C0147"),
                        _row(profile_id="public_portal-c0147", institution_name="Agency B", apba_id="C0165"),
                    ]
                },
            )

            report = build_institution_profile_registry_from_batch(batch_reports=[batch])

        self.assertFalse(report["passed"])
        self.assertGreaterEqual(report["conflict_count"], 2)
        self.assertIn("batch_profile_identity_conflict", {item["reason"] for item in report["conflicts"]})

    def test_builds_registry_from_stratified_manifest_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.csv"
            registry_out = root / "institution_profiles.json"
            _write_csv_manifest(manifest)

            report = build_institution_profile_registry_from_batch(
                batch_reports=[manifest],
                out_registry=registry_out,
            )
            registry = load_institution_profile_registry(registry_out)
            provenance = build_profile_provenance_report(
                batch_report=manifest,
                institution_profiles=registry_out,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(2, report["batch_profile_count"])
        self.assertEqual(2, report["apba_id_counts"]["C0147"])
        self.assertEqual("C0147", registry.resolve("public_portal-c0147", strict=True).apba_id)
        self.assertTrue(provenance["passed"])
        self.assertEqual(0, provenance["warning_count"])
        self.assertEqual({"public_portal-c0147": 2, "public_portal-c0165": 1}, provenance["batch_profile_counts"])


def _write_batch(path: Path) -> None:
    _write_json(
        path,
        {
            "rows": [
                _row(profile_id="public_portal-c0147", institution_name="Agency A", apba_id="C0147"),
                _row(profile_id="public_portal-c0165", institution_name="Agency B", apba_id="C0165", file_type="pdf"),
            ]
        },
    )


def _row(
    *,
    profile_id: str,
    institution_name: str,
    apba_id: str,
    file_type: str = "hwp",
) -> dict:
    return {
        "filename": f"{profile_id}.{file_type}",
        "file_type": file_type,
        "document_id": f"doc-{profile_id}",
        "status": "completed",
        "institution_name": institution_name,
        "apba_id": apba_id,
        "profile_id": profile_id,
        "source_system": "PUBLIC_PORTAL",
        "source_url": f"https://example.org/regulations/item/itemOrganList.do?apbaId={apba_id}&reportFormRootNo=21110",
        "source_record_id": f"record-{apba_id}",
        "source_file_id": f"file-{apba_id}",
        "source_posted_date": "2026-07-09",
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv_manifest(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "input_path,document_name,institution_name,apba_id,source_system,source_record_id,source_file_id,profile_id,selection_bucket",
                "data/a.hwp,a.hwp,Agency A,C0147,PUBLIC_PORTAL,record-a,file-a,public_portal-c0147,hwp",
                "data/b.pdf,b.pdf,Agency A,C0147,PUBLIC_PORTAL,record-b,file-b,public_portal-c0147,pdf",
                "data/c.hwpx,c.hwpx,Agency B,C0165,PUBLIC_PORTAL,record-c,file-c,public_portal-c0165,hwpx",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
