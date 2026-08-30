from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.authoring import AuthoringProjectCreateRequest
from app.services.authoring_service import AuthoringService
from app.storage.authoring_repository import (
    AuthoringProjectNotFoundError,
    AuthoringRepository,
    AuthoringRepositoryIntegrityError,
)


class AuthoringRepositoryPurgeTests(unittest.TestCase):
    def _create_project(
        self,
        settings: Settings,
        *,
        profile_id: str,
        tenant_id: str,
    ):
        return AuthoringService(settings).create_project(
            AuthoringProjectCreateRequest(
                profile_id=profile_id,
                title=f"{profile_id} 규정",
            ),
            tenant_id=tenant_id,
            actor="author",
        )

    def test_purge_removes_only_matching_profile_and_tenant_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            target = self._create_project(
                settings,
                profile_id="institution-a",
                tenant_id="tenant-a",
            )
            other_profile = self._create_project(
                settings,
                profile_id="institution-b",
                tenant_id="tenant-a",
            )
            other_tenant = self._create_project(
                settings,
                profile_id="institution-a",
                tenant_id="tenant-b",
            )
            repository = AuthoringRepository(settings)
            target_export = (
                settings.authoring_dir
                / "exports"
                / str(target.project_id)
                / "00000000000000000001"
                / "draft.json"
            )
            target_export.parent.mkdir(parents=True)
            target_export.write_text("{}", encoding="utf-8")

            result = repository.purge_profile_projects(
                "institution-a",
                tenant_id="tenant-a",
            )

            self.assertTrue(result.completed)
            self.assertEqual(1, result.requested_project_count)
            self.assertEqual(1, result.deleted_project_count)
            self.assertFalse(target_export.exists())
            self.assertFalse(
                (settings.authoring_dir / "snapshots" / str(target.project_id)).exists()
            )
            self.assertFalse(
                (settings.authoring_dir / "events" / str(target.project_id)).exists()
            )
            with self.assertRaises(AuthoringProjectNotFoundError):
                repository.get_project(str(target.project_id), tenant_id="tenant-a")
            self.assertEqual(
                other_profile.project_id,
                repository.get_project(
                    str(other_profile.project_id), tenant_id="tenant-a"
                ).project_id,
            )
            self.assertEqual(
                other_tenant.project_id,
                repository.get_project(
                    str(other_tenant.project_id), tenant_id="tenant-b"
                ).project_id,
            )

    def test_partial_failure_remains_counted_and_converges_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            project = self._create_project(
                settings,
                profile_id="institution-a",
                tenant_id="default",
            )
            repository = AuthoringRepository(settings)

            with patch.object(
                repository,
                "_remove_project_directory",
                side_effect=OSError("simulated cleanup failure"),
            ):
                first = repository.purge_profile_projects(
                    "institution-a",
                    tenant_id="default",
                )

            self.assertFalse(first.completed)
            self.assertEqual(1, repository.profile_project_count(
                "institution-a", tenant_id="default"
            ))
            self.assertTrue(
                (settings.authoring_dir / ".purges" / f"{project.project_id}.json").is_file()
            )
            with self.assertRaises(AuthoringProjectNotFoundError):
                repository.get_project(str(project.project_id), tenant_id="default")

            second = repository.purge_profile_projects(
                "institution-a",
                tenant_id="default",
            )

            self.assertTrue(second.completed)
            self.assertEqual(0, repository.profile_project_count(
                "institution-a", tenant_id="default"
            ))
            self.assertFalse(
                (settings.authoring_dir / ".purges" / f"{project.project_id}.json").exists()
            )

    def test_other_profile_snapshot_corruption_does_not_block_list_or_purge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            target = self._create_project(
                settings,
                profile_id="institution-a",
                tenant_id="tenant-a",
            )
            damaged = self._create_project(
                settings,
                profile_id="institution-b",
                tenant_id="tenant-a",
            )
            damaged_snapshot = (
                settings.authoring_dir
                / "snapshots"
                / str(damaged.project_id)
                / "00000000000000000001.json"
            )
            damaged_snapshot.write_text("{}", encoding="utf-8")
            service = AuthoringService(settings)
            repository = AuthoringRepository(settings)

            summaries = service.list_projects(
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            result = repository.purge_profile_projects(
                "institution-a",
                tenant_id="tenant-a",
            )

            self.assertEqual([target.project_id], [item.project_id for item in summaries])
            self.assertTrue(result.completed)
            with self.assertRaises(AuthoringProjectNotFoundError):
                repository.get_project(str(target.project_id), tenant_id="tenant-a")
            with self.assertRaises(AuthoringRepositoryIntegrityError):
                repository.get_project(str(damaged.project_id), tenant_id="tenant-a")

    def test_symlinked_export_directory_is_refused_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            project = self._create_project(
                settings,
                profile_id="institution-a",
                tenant_id="default",
            )
            repository = AuthoringRepository(settings)
            outside = root / "outside"
            outside.mkdir()
            protected = outside / "keep.txt"
            protected.write_text("keep", encoding="utf-8")
            export_link = settings.authoring_dir / "exports" / str(project.project_id)
            try:
                export_link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            result = repository.purge_profile_projects(
                "institution-a",
                tenant_id="default",
            )

            self.assertFalse(result.completed)
            self.assertTrue(protected.is_file())
            self.assertTrue(export_link.is_symlink())
            self.assertEqual(1, repository.profile_project_count(
                "institution-a", tenant_id="default"
            ))

            export_link.unlink()
            retried = repository.purge_profile_projects(
                "institution-a",
                tenant_id="default",
            )
            self.assertTrue(retried.completed)
            self.assertTrue(protected.is_file())

    def test_abrupt_initial_commit_intent_is_content_free_and_purgeable(self) -> None:
        marker = "CRASH_WINDOW_PRIVATE_DRAFT"
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = AuthoringRepository(settings)
            service = AuthoringService(settings, repository=repository)
            original_write = repository._atomic_write_json

            def stop_before_manifest(path, payload):
                if Path(path).parent == repository.projects_root:
                    raise KeyboardInterrupt("simulated abrupt stop")
                return original_write(path, payload)

            with patch.object(
                repository,
                "_atomic_write_json",
                side_effect=stop_before_manifest,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.create_project(
                        AuthoringProjectCreateRequest(
                            profile_id="institution-a",
                            title=marker,
                        ),
                        tenant_id="tenant-a",
                        actor="author-a",
                    )

            intents = list(repository.staging_root.glob("*.json"))
            snapshots = list(repository.snapshots_root.rglob("*.json"))
            events = list(repository.events_root.rglob("*.json"))
            self.assertEqual(1, len(intents))
            self.assertEqual(1, len(snapshots))
            self.assertEqual(1, len(events))
            self.assertNotIn(marker, intents[0].read_text(encoding="utf-8"))
            self.assertIn(marker, snapshots[0].read_text(encoding="utf-8"))
            self.assertEqual(
                1,
                repository.profile_project_count(
                    "institution-a",
                    tenant_id="tenant-a",
                ),
            )

            result = repository.purge_profile_projects(
                "institution-a",
                tenant_id="tenant-a",
            )

            self.assertTrue(result.completed)
            self.assertEqual(1, result.requested_project_count)
            self.assertEqual([], list(repository.staging_root.glob("*.json")))
            self.assertEqual([], list(repository.snapshots_root.rglob("*.json")))
            self.assertEqual([], list(repository.events_root.rglob("*.json")))

    def test_startup_removes_legacy_manifestless_sensitive_generations(self) -> None:
        marker = "LEGACY_MANIFESTLESS_PRIVATE_DRAFT"
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = AuthoringRepository(settings)
            service = AuthoringService(settings, repository=repository)
            original_write = repository._atomic_write_json

            def stop_before_manifest(path, payload):
                if Path(path).parent == repository.projects_root:
                    raise KeyboardInterrupt("simulated abrupt stop")
                return original_write(path, payload)

            with patch.object(
                repository,
                "_atomic_write_json",
                side_effect=stop_before_manifest,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.create_project(
                        AuthoringProjectCreateRequest(
                            profile_id="institution-a",
                            title=marker,
                        ),
                        tenant_id="tenant-a",
                        actor="author-a",
                    )

            for intent in repository.staging_root.glob("*.json"):
                intent.unlink()
            self.assertTrue(list(repository.snapshots_root.rglob("*.json")))

            recovered = AuthoringRepository(settings)

            self.assertEqual([], list(recovered.snapshots_root.rglob("*.json")))
            self.assertEqual([], list(recovered.events_root.rglob("*.json")))
            self.assertEqual([], list(recovered.exports_root.rglob("*.*")))

    def test_startup_recovers_an_interrupted_initial_commit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = AuthoringRepository(settings)
            service = AuthoringService(settings, repository=repository)
            original_write = repository._atomic_write_json

            def stop_before_manifest(path, payload):
                if Path(path).parent == repository.projects_root:
                    raise KeyboardInterrupt("simulated abrupt stop")
                return original_write(path, payload)

            with patch.object(
                repository,
                "_atomic_write_json",
                side_effect=stop_before_manifest,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.create_project(
                        AuthoringProjectCreateRequest(
                            profile_id="institution-a",
                            title="INTERRUPTED_INITIAL_PRIVATE_DRAFT",
                        ),
                        tenant_id="tenant-a",
                        actor="author-a",
                    )

            self.assertEqual(1, len(list(repository.staging_root.glob("*.json"))))
            self.assertTrue(list(repository.snapshots_root.rglob("*.json")))

            recovered = AuthoringRepository(settings)

            self.assertEqual([], list(recovered.staging_root.glob("*.json")))
            self.assertEqual([], list(recovered.snapshots_root.rglob("*.json")))
            self.assertEqual([], list(recovered.events_root.rglob("*.json")))
            self.assertEqual(
                0,
                recovered.profile_project_count(
                    "institution-a",
                    tenant_id="tenant-a",
                ),
            )

    def test_cleanup_rejects_noncanonical_tenant_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")

            with self.assertRaises(ValueError):
                repository.profile_project_count(
                    "institution-a",
                    tenant_id=" Tenant-A ",
                )


if __name__ == "__main__":
    unittest.main()
