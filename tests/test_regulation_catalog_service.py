from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.services.regulation_catalog_service import (
    RegulationMetadata,
    filter_to_latest_active_versions,
    latest_history_version,
    read_regulation_metadata,
)


class RegulationCatalogServiceTests(unittest.TestCase):
    def test_filter_to_latest_active_versions_reads_each_metadata_once(self) -> None:
        documents = [
            {"document_id": "doc-old", "id": "doc-old"},
            {"document_id": "doc-new", "id": "doc-new"},
            {"document_id": "doc-legacy", "id": "doc-legacy"},
        ]

        metadata_by_document_id = {
            "doc-old": RegulationMetadata(
                profile_id="profile-a",
                regulation_id="reg-1",
                version="1",
                effective_from=date(2024, 1, 1),
                status="approved",
            ),
            "doc-new": RegulationMetadata(
                profile_id="profile-a",
                regulation_id="reg-1",
                version="2",
                effective_from=date(2025, 1, 1),
                status="approved",
            ),
            "doc-legacy": RegulationMetadata(
                profile_id=None,
                regulation_id=None,
                version=None,
                effective_from=None,
                status="approved",
            ),
        }

        def fake_read_regulation_metadata(document: object) -> RegulationMetadata:
            document_id = str((document or {}).get("document_id") if isinstance(document, dict) else "")
            if document_id == "doc-old":
                return RegulationMetadata(
                    profile_id="profile-a",
                    regulation_id="reg-1",
                    version="1",
                    effective_from=date(2024, 1, 1),
                    status="approved",
                )
            if document_id == "doc-new":
                return RegulationMetadata(
                    profile_id="profile-a",
                    regulation_id="reg-1",
                    version="2",
                    effective_from=date(2025, 1, 1),
                    status="approved",
                )
            return metadata_by_document_id["doc-legacy"]

        with patch(
            "app.services.regulation_catalog_service.read_regulation_metadata",
            side_effect=fake_read_regulation_metadata,
        ) as mocked_read:
            visible = filter_to_latest_active_versions(documents, as_of="2025-06-01")

        self.assertEqual(["doc-new", "doc-legacy"], [item["document_id"] for item in visible])
        self.assertEqual(3, mocked_read.call_count)

    def test_filter_to_latest_active_versions_keeps_incomplete_legacy_groups(self) -> None:
        documents = [
            {
                "document_id": "doc-legacy-1",
                "id": "doc-legacy-1",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-legacy",
                    "version": "",
                    "effective_from": None,
                    "status": "approved",
                },
            },
            {
                "document_id": "doc-legacy-2",
                "id": "doc-legacy-2",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-legacy",
                    "version": "",
                    "effective_from": None,
                    "status": "approved",
                },
            },
        ]

        visible = filter_to_latest_active_versions(documents, include_legacy=True)

        self.assertEqual(["doc-legacy-1", "doc-legacy-2"], [item["document_id"] for item in visible])

    def test_filter_to_latest_active_versions_hides_fully_catalogued_repealed_group(self) -> None:
        documents = [
            {
                "document_id": "doc-repealed",
                "id": "doc-repealed",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-repealed",
                    "version": "v1",
                    "effective_from": "2024-01-01",
                    "status": "repealed",
                },
            }
        ]

        visible = filter_to_latest_active_versions(documents, include_legacy=True)

        self.assertEqual([], [item["document_id"] for item in visible])

    def test_filter_to_latest_active_versions_hides_incomplete_dead_status_records(self) -> None:
        # Residual hole after the pre-catalog fail-open fix: a repealed or
        # superseded record that also happens to lack ``version`` or
        # ``effective_from`` must not fall through the pre-catalog exception as
        # current evidence, while a genuinely approved pre-catalog record still
        # does.  Each record sits in its own regulation group with no active
        # sibling, so ``latest_pair`` is ``None`` and the fail-open branch runs.
        documents = [
            {
                "document_id": "doc-approved-sparse",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-approved",
                    "version": "",
                    "effective_from": None,
                    "status": "approved",
                },
            },
            {
                "document_id": "doc-repealed-sparse",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-repealed",
                    "version": "v1",
                    "effective_from": None,
                    "status": "repealed",
                },
            },
            {
                "document_id": "doc-superseded-sparse",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-superseded",
                    "version": "",
                    "effective_from": "2020-01-01",
                    "status": "superseded",
                },
            },
        ]

        visible = filter_to_latest_active_versions(documents, include_legacy=True)

        self.assertEqual(
            ["doc-approved-sparse"],
            [item["document_id"] for item in visible],
        )

    def test_read_regulation_metadata_normalizes_legacy_lifecycle_aliases(self) -> None:
        metadata = read_regulation_metadata(
            {
                "document_id": "doc-legacy",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_no": "4-4-1",
                    "revision_date": "2025-01-02",
                    "regulation_status": "approved",
                },
            }
        )

        self.assertEqual("profile-a", metadata.profile_id)
        self.assertEqual("4-4-1", metadata.regulation_id)
        self.assertEqual("2025-01-02", metadata.version)
        self.assertEqual(date(2025, 1, 2), metadata.effective_from)
        self.assertEqual("approved", metadata.status)

    def test_filter_to_latest_active_versions_selects_latest_legacy_revision(self) -> None:
        documents = [
            {
                "document_id": "doc-legacy-old",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_no": "4-4-1",
                    "revision_date": "2024-01-01",
                    "regulation_status": "approved",
                },
            },
            {
                "document_id": "doc-legacy-new",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_no": "4-4-1",
                    "revision_date": "2025-01-02",
                    "regulation_status": "approved",
                },
            },
        ]

        visible = filter_to_latest_active_versions(documents, as_of="2025-06-01", include_legacy=False)

        self.assertEqual(["doc-legacy-new"], [item["document_id"] for item in visible])

    def test_latest_history_version_prefers_approved_over_superseded_on_shared_effective_date(self) -> None:
        # Auto-supersede backfills the prior version's effective_from to the
        # revision's effective_from, so a superseded and the current approved
        # version routinely share an effective date.  On that tie the current
        # approved version must win, not the superseded one via a version-token
        # comparison ("v2" sorting above "rev-20250701").
        documents = [
            {
                "document_id": "doc-superseded",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-y",
                    "version": "v2",
                    "effective_from": "2025-07-01",
                    "effective_to": "2025-07-01",
                    "status": "superseded",
                },
            },
            {
                "document_id": "doc-current",
                "metadata": {
                    "profile_id": "profile-a",
                    "regulation_id": "reg-y",
                    "version": "rev-20250701",
                    "effective_from": "2025-07-01",
                    "effective_to": None,
                    "status": "approved",
                },
            },
        ]

        current = latest_history_version(documents, as_of="2025-07-01")

        self.assertEqual("doc-current", current["document_id"])


if __name__ == "__main__":
    unittest.main()
