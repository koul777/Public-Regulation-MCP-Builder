from __future__ import annotations

from datetime import date
import io
from pathlib import Path
import tempfile
import unittest

from app.core.config import Settings
from app.services.document_service import DocumentService
from app.services.regulation_metadata_service import (
    infer_regulation_metadata,
    regulation_title_from_filename,
    regulation_upload_sort_key,
)
from app.storage.repository import JsonRepository


class RegulationMetadataServiceTests(unittest.TestCase):
    def test_filename_detection_groups_revisions_and_finds_approved_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repository)

            original = service.upload_stream(
                "인사규정_2024.01.01.pdf",
                io.BytesIO(b"%PDF-1.4\noriginal"),
                profile_id="institution-a",
                tenant_id="tenant-a",
            )
            repository.upsert_document(original.model_copy(update={"regulation_status": "approved"}))
            revision = service.upload_stream(
                "인사규정_2025.07.01_일부개정본.pdf",
                io.BytesIO(b"%PDF-1.4\nrevision"),
                profile_id="institution-a",
                tenant_id="tenant-a",
            )

            original_path = service.path_for(original)
            revision_path = service.path_for(revision)

        self.assertEqual("인사규정", original.document_name)
        self.assertEqual(original.regulation_id, revision.regulation_id)
        self.assertEqual("rev-20240101", original.regulation_version)
        self.assertEqual("rev-20250701", revision.regulation_version)
        self.assertEqual(original.document_id, revision.supersedes_document_id)
        self.assertNotEqual(original_path.parent, revision_path.parent)
        self.assertEqual(original_path.parents[2], revision_path.parents[2])
        self.assertEqual("rev-20240101", original_path.parent.name)
        self.assertEqual("rev-20250701", revision_path.parent.name)
        self.assertEqual("versions", original_path.parents[1].name)
        self.assertEqual("regulations", original_path.parents[3].name)
        self.assertEqual("institution-a", original_path.parents[4].name)

    def test_different_regulations_are_saved_in_different_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            service = DocumentService(settings=settings)
            personnel = service.upload("인사규정.pdf", b"%PDF-1.4\npersonnel")
            accounting = service.upload("회계규정.pdf", b"%PDF-1.4\naccounting")

            personnel_path = service.path_for(personnel)
            accounting_path = service.path_for(accounting)

        self.assertNotEqual(personnel.regulation_id, accounting.regulation_id)
        self.assertNotEqual(personnel_path.parent, accounting_path.parent)
        self.assertEqual("regulations", personnel_path.parents[3].name)
        self.assertEqual("regulations", accounting_path.parents[3].name)

    def test_extracted_text_improves_generic_filename_and_dates(self) -> None:
        detected = infer_regulation_metadata(
            "scan_001.pdf",
            text=(
                "임직원 행동강령\n"
                "제정 2023. 1. 2.\n"
                "제3차 개정 2025년 6월 30일\n"
                "이 강령은 2025. 7. 1.부터 시행한다."
            ),
            today=date(2026, 7, 16),
        )

        self.assertEqual("임직원 행동강령", detected.document_name)
        self.assertEqual("reg-임직원-행동강령", detected.regulation_id)
        self.assertEqual("rev-3", detected.regulation_version)
        self.assertEqual("2025-06-30", detected.revision_date)
        self.assertEqual("2025-07-01", detected.effective_from)
        self.assertEqual("content", detected.title_source)

    def test_content_revision_history_overrides_conflicting_filename_version_and_date(self) -> None:
        detected = infer_regulation_metadata(
            "인사규정_v99_2030.12.31.pdf",
            text=(
                "인사규정\n"
                "제정 2020. 1. 1.\n"
                "제3차 일부개정 2025. 6. 30.\n"
                "이 규정은 2025. 7. 1.부터 시행한다."
            ),
            today=date(2026, 7, 16),
        )

        self.assertEqual("인사규정", detected.document_name)
        self.assertEqual("rev-3", detected.regulation_version)
        self.assertEqual("2025-06-30", detected.revision_date)
        self.assertEqual("2025-07-01", detected.effective_from)
        self.assertEqual("content", detected.version_source)
        self.assertEqual("content", detected.revision_date_source)
        self.assertEqual("content", detected.effective_from_source)

    def test_batch_sort_orders_older_revision_before_newer_revision(self) -> None:
        filenames = [
            "인사규정_2025.07.01_개정.pdf",
            "회계규정_2024.03.01.pdf",
            "인사규정_2024.01.01.pdf",
        ]

        ordered = sorted(filenames, key=regulation_upload_sort_key)

        self.assertLess(ordered.index("인사규정_2024.01.01.pdf"), ordered.index("인사규정_2025.07.01_개정.pdf"))

    def test_undated_uploads_receive_sequential_versions_within_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            service = DocumentService(settings=settings)
            first = service.upload("인사규정.pdf", b"%PDF-1.4\nfirst")
            second = service.upload("인사규정_개정본.pdf", b"%PDF-1.4\nsecond")

        self.assertEqual("v1", first.regulation_version)
        self.assertEqual("v2", second.regulation_version)

    def test_compact_dated_combined_binders_link_as_revisions_inside_one_institution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            service = DocumentService(settings=settings, repository=repository)
            original = service.upload_stream(
                "\uaddc\uc815\uc9d1\ud1b5\ud569\ubcf8_260520.pdf",
                io.BytesIO(b"%PDF-1.4\nold binder"),
                profile_id="institution-aks",
                tenant_id="tenant-a",
            )
            repository.upsert_document(original.model_copy(update={"regulation_status": "approved"}))
            revision = service.upload_stream(
                "\uaddc\uc815\uc9d1\ud1b5\ud569\ubcf8_270101_\uac1c\uc815\ud310.pdf",
                io.BytesIO(b"%PDF-1.4\nnew binder"),
                profile_id="institution-aks",
                tenant_id="tenant-a",
            )

        self.assertEqual("\uaddc\uc815\uc9d1", original.document_name)
        self.assertEqual(original.regulation_id, revision.regulation_id)
        self.assertEqual("rev-20260520", original.regulation_version)
        self.assertEqual("rev-20270101", revision.regulation_version)
        self.assertEqual(original.document_id, revision.supersedes_document_id)

    def test_public_institution_history_overrides_filename_copy_numbers_and_classification(self) -> None:
        histories = {
            "3-2-1. \uc778\uc0ac\uaddc\uc8153.hwp": """
                \uc778\uc0ac\uaddc\uc815
                3-2-1. \uc778\uc0ac\uaddc\uc815
                \uc77c\ubd80\uac1c\uc815 2023. 3. 27. \uaddc\uc815 \uc81c1186\ud638(\uc2dc\ud589 2023. 3. 27.)
            """,
            "4-2-1. \uc778\uc0ac\uaddc\uc8152.hwp": """
                \uc778\uc0ac\uaddc\uc815
                \uc77c\ubd80\uac1c\uc815 2023. 12. 20. \uaddc\uc815 \uc81c1208\ud638(\uc2dc\ud589 2023. 12. 20.)
                \uc77c\ubd80\uac1c\uc815 2023. 3. 27. \uaddc\uc815 \uc81c1186\ud638
            """,
            "4-2-1. \uc778\uc0ac\uaddc\uc8151.hwp": """
                \uc778\uc0ac\uaddc\uc815
                \uc77c\ubd80\uac1c\uc815 2025. 12. 22. \uaddc\uc815 \uc81c1250\ud638(\uc2dc\ud589 2026. 1. 1.)
                \uc77c\ubd80\uac1c\uc815 2023. 12. 20. \uaddc\uc815 \uc81c1208\ud638
            """,
        }

        detected = [infer_regulation_metadata(filename, text=text) for filename, text in histories.items()]

        self.assertTrue(all(regulation_title_from_filename(name) == "\uc778\uc0ac\uaddc\uc815" for name in histories))
        self.assertEqual({"\uc778\uc0ac\uaddc\uc815"}, {item.document_name for item in detected})
        self.assertEqual(1, len({item.regulation_id for item in detected}))
        self.assertEqual(
            ["2023-03-27", "2023-12-20", "2025-12-22"],
            [item.revision_date for item in detected],
        )
        self.assertEqual(
            ["2023-03-27", "2023-12-20", "2026-01-01"],
            [item.effective_from for item in detected],
        )
        self.assertEqual(
            ["rev-20230327", "rev-20231220", "rev-20251222"],
            [item.regulation_version for item in detected],
        )


if __name__ == "__main__":
    unittest.main()
