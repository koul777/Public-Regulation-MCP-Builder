from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.institution_profiles import (
    delete_institution_profile,
    institution_profile_registry_to_bytes,
    load_institution_profile_registry,
    load_institution_profile_registry_from_bytes,
    save_institution_profile_registry,
    upsert_institution_profile,
)
from scripts.validate_institution_profiles import main, validate_institution_profiles


class InstitutionProfileRegistryTests(unittest.TestCase):
    def test_delete_profile_reassigns_default_and_handles_last_profile(self) -> None:
        registry = load_institution_profile_registry_from_bytes(
            json.dumps(
                {
                    "default_profile_id": "agency-b",
                    "profiles": {
                        "agency-b": {"display_name": "Agency B"},
                        "agency-a": {"display_name": "Agency A"},
                    },
                }
            ).encode("utf-8")
        )

        without_default = delete_institution_profile(registry, "AGENCY-B")
        empty = delete_institution_profile(without_default, "agency-a")

        self.assertEqual(["agency-a"], sorted(without_default.profiles))
        self.assertEqual("agency-a", without_default.default_profile_id)
        self.assertEqual({}, empty.profiles)
        self.assertIsNone(empty.default_profile_id)

    def test_loads_registry_with_profile_defaults_and_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "default_profile_id": "public_portal-etc-law",
                        "profiles": {
                            "public_portal-etc-law": {
                            "display_name": "PUBLIC_PORTAL",
                            "institution_name": "PUBLIC_PORTAL public institution disclosure",
                            "apba_id": "C0147",
                            "source_system": "PUBLIC_PORTAL",
                            "source_url": "https://example.org/regulations/etc/etcLawList.do",
                                "required_row_fields": ["source_system", "apba_id", "source_record_id", "profile_id"],
                                "max_upload_mb": 1000,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            registry = load_institution_profile_registry(path)
            profile = registry.resolve("PUBLIC_PORTAL-ETC-LAW", strict=True)

        self.assertIsNotNone(profile)
        self.assertEqual(profile.institution_name, "PUBLIC_PORTAL public institution disclosure")
        self.assertEqual(profile.apba_id, "C0147")
        self.assertEqual(profile.required_row_fields, ("source_system", "apba_id", "source_record_id", "profile_id"))
        self.assertEqual(profile.max_upload_mb, 1000)
        self.assertEqual(len(registry.sha256), 64)

    def test_loads_registry_from_uploaded_bytes(self) -> None:
        content = json.dumps(
            {
                "default_profile_id": "strict-public",
                "profiles": {
                    "strict-public": {
                        "display_name": "Strict",
                        "required_row_fields": ["institution_name", "profile_id"],
                    }
                },
            }
        ).encode("utf-8")

        registry = load_institution_profile_registry_from_bytes(content)

        self.assertEqual(registry.default_profile_id, "strict-public")
        self.assertEqual(registry.resolve(None, strict=True).profile_id, "strict-public")
        self.assertEqual(len(registry.sha256), 64)

    def test_save_registry_writes_canonical_json_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(json.dumps({"profiles": {"old": {"source_system": "OLD"}}}), encoding="utf-8")
            registry = load_institution_profile_registry_from_bytes(
                json.dumps(
                    {
                        "default_profile_id": "strict-public",
                        "profiles": {
                            "strict-public": {
                                "display_name": "Strict",
                                "institution_name": "기관",
                                "required_row_fields": ["institution_name", "profile_id"],
                                "max_upload_mb": 100,
                            }
                        },
                    }
                ).encode("utf-8")
            )

            result = save_institution_profile_registry(path, registry)
            reloaded = load_institution_profile_registry(path)
            backup_path = Path(result["backup_path"])

            self.assertEqual(result["profile_count"], 1)
            self.assertTrue(backup_path.exists())
            self.assertIn("old", backup_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded.default_profile_id, "strict-public")
            self.assertEqual(reloaded.resolve("strict-public", strict=True).institution_name, "기관")
            self.assertEqual(result["sha256"], load_institution_profile_registry_from_bytes(path.read_bytes()).sha256)

    def test_registry_to_bytes_round_trips_validated_registry(self) -> None:
        registry = load_institution_profile_registry_from_bytes(
            json.dumps({"profiles": {"known": {"source_system": "PUBLIC_PORTAL"}}}).encode("utf-8")
        )

        content = institution_profile_registry_to_bytes(registry)
        reloaded = load_institution_profile_registry_from_bytes(content)

        self.assertEqual(reloaded.resolve("known", strict=True).source_system, "PUBLIC_PORTAL")
        self.assertTrue(content.endswith(b"\n"))

    def test_upsert_profile_creates_and_updates_validated_registry(self) -> None:
        registry = load_institution_profile_registry_from_bytes(json.dumps({"profiles": {}}).encode("utf-8"))

        created = upsert_institution_profile(
            registry,
            "agency-a",
            display_name="Agency A",
            institution_name="Agency A",
            apba_id="C0147",
            source_system="PORTAL",
            source_url="https://example.test",
            required_row_fields=["institution_name", "profile_id"],
            max_upload_mb=500,
            notes="pilot",
            make_default=True,
        )
        updated = upsert_institution_profile(
            created,
            "AGENCY-A",
            display_name="Agency A Updated",
            required_row_fields=["profile_id"],
        )

        profile = updated.resolve("agency-a", strict=True)
        self.assertEqual(updated.default_profile_id, "agency-a")
        self.assertEqual(profile.display_name, "Agency A Updated")
        self.assertIsNone(profile.apba_id)
        self.assertEqual(profile.required_row_fields, ("profile_id",))
        self.assertIsNone(profile.max_upload_mb)
        self.assertEqual(len(updated.sha256), 64)

    def test_upsert_profile_rejects_invalid_required_fields(self) -> None:
        registry = load_institution_profile_registry_from_bytes(json.dumps({"profiles": {}}).encode("utf-8"))

        with self.assertRaisesRegex(ValueError, "unsupported required_row_field"):
            upsert_institution_profile(registry, "agency-a", required_row_fields=["local_path"])

    def test_strict_resolve_rejects_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(json.dumps({"profiles": {"known": {}}}), encoding="utf-8")
            registry = load_institution_profile_registry(path)

        with self.assertRaisesRegex(ValueError, "Unknown institution profile_id"):
            registry.resolve("typo", strict=True)

    def test_rejects_unknown_or_duplicate_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(
                json.dumps({"profiles": {"bad": {"required_row_fields": ["source_system", "source_system"]}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate required_row_field"):
                load_institution_profile_registry(path)

            path.write_text(
                json.dumps({"profiles": {"bad": {"required_row_fields": ["local_path"]}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported required_row_field"):
                load_institution_profile_registry(path)

    def test_validate_script_returns_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(json.dumps({"profiles": {"known": {"source_system": "PUBLIC_PORTAL"}}}), encoding="utf-8")

            report = validate_institution_profiles(path)

        self.assertTrue(report["valid"])
        self.assertEqual(report["profile_count"], 1)
        self.assertEqual(report["profiles"]["known"]["source_system"], "PUBLIC_PORTAL")

    def test_validate_script_main_returns_failure_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "institution_profiles.json"
            path.write_text(json.dumps({"profiles": {"bad": {"max_upload_mb": 0}}}), encoding="utf-8")
            with patch("sys.argv", ["validate_institution_profiles.py", str(path)]), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as stdout:
                exit_code = main()

        self.assertEqual(exit_code, 2)
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["valid"])
        self.assertIn("max_upload_mb", output["error"])


if __name__ == "__main__":
    unittest.main()
