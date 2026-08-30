from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from threading import Barrier, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.core.config import Settings
from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    AuthoringProject,
    AuthoringProjectStatus,
    ClauseDraft,
)
from app.schemas.authoring_integrity import semantic_content_hash
from app.storage.authoring_repository import (
    AuthoringProjectNotFoundError,
    AuthoringRepository,
    AuthoringRepositoryIntegrityError,
    AuthoringRevisionConflictError,
)
from app.storage import authoring_repository as repository_module


class AuthoringRepositoryTests(unittest.TestCase):
    def test_schema_v1_manifest_is_atomically_upgraded_with_profile_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            manifest_path = (
                data_dir / "authoring" / "projects" / f"{created.project_id}.json"
            )
            legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            legacy_manifest["manifest_schema_version"] = 1
            legacy_manifest.pop("profile_id")
            manifest_path.write_text(
                json.dumps(legacy_manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            loaded = repository.get_project(
                str(created.project_id),
                tenant_id="tenant-a",
            )
            listed = repository.list_projects(
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            count = repository.profile_project_count(
                "institution-a",
                tenant_id="tenant-a",
            )
            migrated = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(created.project_id, loaded.project_id)
        self.assertEqual([created.project_id], [project.project_id for project in listed])
        self.assertEqual(1, count)
        self.assertEqual(2, migrated["manifest_schema_version"])
        self.assertEqual("institution-a", migrated["profile_id"])

    def test_create_rejects_noncanonical_tenant_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")
            project = _project(tenant_id=" Tenant-A ")

            with self.assertRaisesRegex(ValueError, "canonical lowercase tenant"):
                repository.create_project(project, actor="author-a")

    def test_create_is_isolated_and_events_do_not_contain_draft_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(Settings(data_dir=data_dir))
            draft_body_marker = (
                "Internal draft clause body that must stay out of audit events."
            )
            project = _project(
                tenant_id="tenant-a",
                title="Internal Draft Title",
                purpose="Sensitive internal drafting purpose",
                clauses=[
                    ClauseDraft(
                        article_number="Article 1",
                        title="Purpose",
                        body=draft_body_marker,
                    )
                ],
            )

            created = repository.create_project(project, actor="author-a")
            loaded = repository.get_project(
                str(created.project_id),
                tenant_id="tenant-a",
            )
            events = repository.list_events(
                str(created.project_id),
                tenant_id="tenant-a",
            )

            authoring_root = data_dir / "authoring"
            event_text = json.dumps(events, ensure_ascii=False)
            self.assertTrue(authoring_root.is_dir())
            self.assertFalse((data_dir / "repository" / "authoring").exists())
            self.assertEqual(1, created.revision)
            self.assertEqual(draft_body_marker, loaded.clauses[0].body)
            self.assertEqual("created", events[0]["event_type"])
            self.assertEqual("author-a", events[0]["actor"])
            self.assertEqual("tenant-a", events[0]["tenant_id"])
            self.assertEqual(1, events[0]["revision"])
            self.assertRegex(str(events[0]["content_hash"]), r"^[0-9a-f]{64}$")
            self.assertIn("reason", events[0])
            self.assertNotIn(draft_body_marker, event_text)
            self.assertNotIn(project.title, event_text)
            self.assertNotIn(project.purpose, event_text)
            self.assertEqual(
                1,
                len(
                    list(
                        (
                            authoring_root
                            / "snapshots"
                            / str(created.project_id)
                        ).glob("*.json")
                    )
                ),
            )

    def test_save_increments_revision_and_rejects_stale_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            candidate = _with_semantic_hash(
                created.model_copy(
                    update={"title": "Revision two", "updated_by": "author-b"}
                )
            )

            updated = repository.save_project(
                candidate,
                tenant_id="tenant-a",
                expected_revision=1,
                actor="author-b",
            )
            with self.assertRaises(AuthoringRevisionConflictError) as context:
                repository.save_project(
                    candidate,
                    tenant_id="tenant-a",
                    expected_revision=1,
                    actor="stale-author",
                )

            loaded = repository.get_project(
                str(created.project_id),
                tenant_id="tenant-a",
            )
            events = repository.list_events(
                str(created.project_id),
                tenant_id="tenant-a",
            )
            snapshot_dir = (
                Path(tmp)
                / "data"
                / "authoring"
                / "snapshots"
                / str(created.project_id)
            )

            self.assertEqual(2, updated.revision)
            self.assertEqual(2, loaded.revision)
            self.assertEqual("Revision two", loaded.title)
            self.assertEqual(1, context.exception.expected_revision)
            self.assertEqual(2, context.exception.actual_revision)
            self.assertEqual([1, 2], [event["revision"] for event in events])
            self.assertEqual(2, len(list(snapshot_dir.glob("*.json"))))

    def test_save_rejects_stale_semantic_hash_without_a_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")
            created = repository.create_project(
                _project(
                    tenant_id="tenant-a",
                    clauses=[ClauseDraft(article_number="Article 1", body="original")],
                ),
                actor="author-a",
            )
            event_count = len(
                repository.list_events(
                    str(created.project_id),
                    tenant_id="tenant-a",
                )
            )
            changed_clauses = [
                created.clauses[0].model_copy(update={"body": "unreviewed replacement"})
            ]

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "semantic content hash",
            ):
                repository.save_project(
                    created.model_copy(update={"clauses": changed_clauses}),
                    tenant_id="tenant-a",
                    expected_revision=created.revision,
                    actor="migration",
                    event_metadata={"migration": True},
                )

            current = repository.get_project(
                str(created.project_id),
                tenant_id="tenant-a",
            )
            events = repository.list_events(
                str(created.project_id),
                tenant_id="tenant-a",
            )

        self.assertEqual(created.revision, current.revision)
        self.assertEqual("original", current.clauses[0].body)
        self.assertEqual(event_count, len(events))

    def test_cross_tenant_access_is_indistinguishable_from_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )

            with self.assertRaises(AuthoringProjectNotFoundError):
                repository.get_project(
                    str(created.project_id),
                    tenant_id="tenant-b",
                )
            with self.assertRaises(AuthoringProjectNotFoundError):
                repository.list_events(
                    str(created.project_id),
                    tenant_id="tenant-b",
                )
            with self.assertRaises(AuthoringProjectNotFoundError):
                repository.save_project(
                    created,
                    tenant_id="tenant-b",
                    expected_revision=1,
                    actor="tenant-b-author",
                )

            self.assertEqual([], repository.list_projects(tenant_id="tenant-b"))
            self.assertEqual(
                [created.project_id],
                [item.project_id for item in repository.list_projects(tenant_id="tenant-a")],
            )

    def test_project_id_must_be_a_canonical_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")

            for unsafe_id in (
                "../outside",
                "author_123",
                "00000000-0000-0000-0000-000000000000/../../outside",
                " 00000000-0000-0000-0000-000000000000",
            ):
                with self.subTest(project_id=unsafe_id):
                    with self.assertRaises(ValueError):
                        repository.get_project(unsafe_id, tenant_id="tenant-a")

            self.assertFalse((Path(tmp) / "outside.json").exists())

    def test_concurrent_writers_allow_only_one_revision_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            first_repository = AuthoringRepository(data_dir)
            second_repository = AuthoringRepository(data_dir)
            created = first_repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            barrier = Barrier(2)
            outcomes: list[str] = []

            def write(repository: AuthoringRepository, title: str) -> None:
                candidate = _with_semantic_hash(
                    created.model_copy(update={"title": title, "updated_by": title})
                )
                barrier.wait()
                try:
                    repository.save_project(
                        candidate,
                        tenant_id="tenant-a",
                        expected_revision=1,
                        actor=title,
                    )
                    outcomes.append("saved")
                except AuthoringRevisionConflictError:
                    outcomes.append("conflict")

            threads = [
                Thread(target=write, args=(first_repository, "writer-one")),
                Thread(target=write, args=(second_repository, "writer-two")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            loaded = first_repository.get_project(
                str(created.project_id),
                tenant_id="tenant-a",
            )

            self.assertEqual(["conflict", "saved"], sorted(outcomes))
            self.assertEqual(2, loaded.revision)
            self.assertIn(loaded.title, {"writer-one", "writer-two"})

    def test_failed_manifest_replace_does_not_publish_partial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            candidate = _with_semantic_hash(
                created.model_copy(
                    update={"title": "must not publish", "updated_by": "author-b"}
                )
            )

            with patch.object(
                repository,
                "_atomic_write_json",
                side_effect=OSError("simulated manifest failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated manifest failure"):
                    repository.save_project(
                        candidate,
                        tenant_id="tenant-a",
                        expected_revision=1,
                        actor="author-b",
                    )

            loaded = repository.get_project(
                str(created.project_id),
                tenant_id="tenant-a",
            )
            event_dir = data_dir / "authoring" / "events" / str(created.project_id)
            snapshot_dir = data_dir / "authoring" / "snapshots" / str(created.project_id)
            self.assertEqual(1, loaded.revision)
            self.assertNotEqual("must not publish", loaded.title)
            self.assertEqual(1, len(list(event_dir.glob("*.json"))))
            self.assertEqual(1, len(list(snapshot_dir.glob("*.json"))))

    def test_event_metadata_rejects_draft_content_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")
            project = _project(tenant_id="tenant-a")

            for key in ("body", "draft_text", "clause-content", "purpose"):
                with self.subTest(key=key):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Draft content is not allowed",
                    ):
                        repository.create_project(
                            project,
                            actor="author-a",
                            event_metadata={key: "sensitive value"},
                        )

            for key in ("feedback", "message", "opaque_alias"):
                with self.subTest(key=key):
                    with self.assertRaises(ValueError):
                        repository.create_project(
                            project,
                            actor="author-a",
                            event_metadata={key: "DRAFT_BODY_MARKER"},
                        )

    def test_tampered_event_metadata_is_rejected_before_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            event_path = data_dir / "authoring" / "events" / str(created.project_id) / "00000000000000000001.json"
            manifest_path = data_dir / "authoring" / "projects" / f"{created.project_id}.json"
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["metadata"] = {"draft_text": "must not leak"}
            unsigned = dict(event)
            unsigned.pop("event_sha256", None)
            event["event_sha256"] = repository_module._record_sha256(unsigned)
            event_path.write_text(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["event_sha256"] = event["event_sha256"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "metadata",
            ):
                repository.list_events(
                    str(created.project_id),
                    tenant_id="tenant-a",
                )

    def test_v2_event_metadata_rejects_invalid_allowed_values_before_storage(self) -> None:
        invalid_metadata = (
            {"from_status": "DRAFT_BODY_ALIAS_MARKER", "to_status": "drafting"},
            {"reason_supplied": "DRAFT_REASON_ALIAS", "reason_sha256": "0" * 64},
            {"reason_supplied": True, "reason_sha256": "not-a-hash"},
            {"export_format": "pdf", "file_sha256": "0" * 64},
            {"export_format": "json"},
            {"self_freeze": 1, "training_only": True},
            {"lint_error_count": -1, "lint_warning_count": 0},
            {"template_id_value": "DRAFT_ALIAS"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            for metadata in invalid_metadata:
                with self.subTest(metadata=metadata):
                    with self.assertRaises(ValueError):
                        repository.create_project(
                            _project(tenant_id="tenant-a"),
                            actor="author-a",
                            event_metadata=metadata,
                        )
            self.assertEqual(
                [],
                list((data_dir / "authoring" / "projects").glob("*.json")),
            )

    def test_v2_event_metadata_context_mismatch_is_rejected_before_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            with self.assertRaisesRegex(ValueError, "snapshot status"):
                repository.create_project(
                    _project(tenant_id="tenant-a"),
                    actor="author-a",
                    event_metadata={
                        "from_status": "planning",
                        "to_status": "drafting",
                    },
                )
            self.assertEqual(
                [],
                list((data_dir / "authoring" / "projects").glob("*.json")),
            )

    def test_tampered_v2_allowed_metadata_value_is_rejected_on_read(self) -> None:
        marker = "DRAFT_REASON_ALIAS_MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            event_path = (
                data_dir
                / "authoring"
                / "events"
                / str(created.project_id)
                / "00000000000000000001.json"
            )
            manifest_path = (
                data_dir
                / "authoring"
                / "projects"
                / f"{created.project_id}.json"
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["metadata"] = {
                "reason_supplied": marker,
                "reason_sha256": "0" * 64,
            }
            event["event_sha256"] = repository_module._record_sha256(event)
            event_path.write_text(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["event_sha256"] = event["event_sha256"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "metadata",
            ):
                repository.list_events(
                    str(created.project_id),
                    tenant_id="tenant-a",
                )

    def test_legacy_event_reason_is_content_free_in_the_read_model(self) -> None:
        marker = "LEGACY_DRAFT_BODY_MARKER_83bc"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            event_path = (
                data_dir
                / "authoring"
                / "events"
                / str(created.project_id)
                / "00000000000000000001.json"
            )
            manifest_path = (
                data_dir
                / "authoring"
                / "projects"
                / f"{created.project_id}.json"
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["event_schema_version"] = 1
            event["reason"] = marker
            unsigned = dict(event)
            unsigned.pop("event_sha256", None)
            event["event_sha256"] = repository_module._record_sha256(unsigned)
            event_path.write_text(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["event_sha256"] = event["event_sha256"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            visible = repository.list_events(
                str(created.project_id),
                tenant_id="tenant-a",
            )

        visible_text = json.dumps(visible, ensure_ascii=False)
        self.assertNotIn(marker, visible_text)
        self.assertEqual("provided", visible[0]["reason"])
        self.assertTrue(visible[0]["read_model_redacted"])
        self.assertEqual(
            hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            visible[0]["metadata"]["reason_sha256"],
        )

    def test_legacy_unknown_metadata_is_hashed_and_redacted_from_read_model(self) -> None:
        legacy_value = "manual-review"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            event_path = (
                data_dir
                / "authoring"
                / "events"
                / str(created.project_id)
                / "00000000000000000001.json"
            )
            manifest_path = (
                data_dir
                / "authoring"
                / "projects"
                / f"{created.project_id}.json"
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["event_schema_version"] = 1
            event["metadata"] = {"legacy_workflow_code": legacy_value}
            event["event_sha256"] = repository_module._record_sha256(event)
            event_path.write_text(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["event_sha256"] = event["event_sha256"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            visible = repository.list_events(
                str(created.project_id),
                tenant_id="tenant-a",
            )

        visible_text = json.dumps(visible, ensure_ascii=False)
        self.assertNotIn(legacy_value, visible_text)
        self.assertNotIn("legacy_workflow_code", visible_text)
        self.assertTrue(visible[0]["read_model_redacted"])
        self.assertTrue(visible[0]["legacy_metadata_redacted"])
        self.assertRegex(visible[0]["legacy_metadata_sha256"], r"^[0-9a-f]{64}$")

    def test_legacy_to_v2_event_chain_remains_readable_without_legacy_value(self) -> None:
        legacy_value = "manual-review"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            event_path = (
                data_dir
                / "authoring"
                / "events"
                / str(created.project_id)
                / "00000000000000000001.json"
            )
            manifest_path = (
                data_dir
                / "authoring"
                / "projects"
                / f"{created.project_id}.json"
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["event_schema_version"] = 1
            event["metadata"] = {"legacy_workflow_code": legacy_value}
            event["event_sha256"] = repository_module._record_sha256(event)
            event_path.write_text(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["event_sha256"] = event["event_sha256"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            updated = repository.save_project(
                _with_semantic_hash(
                    created.model_copy(update={"title": "두 번째 버전"})
                ),
                tenant_id="tenant-a",
                expected_revision=1,
                actor="author-b",
                event_metadata={
                    "from_status": "planning",
                    "to_status": "planning",
                },
            )

            visible = repository.list_events(
                str(updated.project_id),
                tenant_id="tenant-a",
            )

        self.assertEqual([1, 2], [item["event_schema_version"] for item in visible])
        self.assertNotIn(legacy_value, json.dumps(visible, ensure_ascii=False))
        self.assertTrue(visible[0]["legacy_metadata_redacted"])
        self.assertNotIn("read_model_redacted", visible[1])

    def test_update_review_freeze_and_export_each_append_auditable_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = AuthoringRepository(Path(tmp) / "data")
            current = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
                reason="Initial project setup",
            )
            transitions = [
                ("updated", AuthoringProjectStatus.DRAFTING, "Filled required fields"),
                (
                    "review_requested",
                    AuthoringProjectStatus.REVIEW_REQUESTED,
                    "Ready for human review",
                ),
            ]
            for event_type, status, reason in transitions:
                current = repository.save_project(
                    current.model_copy(
                        update={"status": status, "updated_by": "author-a"}
                    ),
                    tenant_id="tenant-a",
                    expected_revision=current.revision,
                    actor="author-a",
                    event_type=event_type,
                    reason=reason,
                )

            frozen_hash = semantic_content_hash(current)
            current = repository.save_project(
                current.model_copy(
                    update={
                        "status": AuthoringProjectStatus.CONTENT_FROZEN,
                        "semantic_content_hash": frozen_hash,
                        "frozen_revision": current.revision + 1,
                        "frozen_content_hash": frozen_hash,
                        "updated_by": "reviewer-b",
                    }
                ),
                tenant_id="tenant-a",
                expected_revision=current.revision,
                actor="reviewer-b",
                event_type="content_frozen",
                reason="Independent reviewer froze this revision",
            )
            frozen_revision = current.revision
            current = repository.save_project(
                current.model_copy(
                    update={
                        "status": AuthoringProjectStatus.EXPORTED,
                        "updated_by": "author-a",
                    }
                ),
                tenant_id="tenant-a",
                expected_revision=current.revision,
                actor="author-a",
                event_type="exported",
                reason="Exported as a non-official draft package",
            )

            events = repository.list_events(
                str(current.project_id),
                tenant_id="tenant-a",
            )
            verified = repository.verify_frozen_artifact_manifest(
                str(current.project_id),
                tenant_id="tenant-a",
                frozen_revision=frozen_revision,
                content_hash=frozen_hash,
            )
            wrong_hash_verified = repository.verify_frozen_artifact_manifest(
                str(current.project_id),
                tenant_id="tenant-a",
                frozen_revision=frozen_revision,
                content_hash="b" * 64,
            )
            manifest = json.loads(
                (
                    Path(tmp)
                    / "data"
                    / "authoring"
                    / "projects"
                    / f"{current.project_id}.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(
                [
                    "created",
                    "updated",
                    "review_requested",
                    "content_frozen",
                    "exported",
                ],
                [event["event_type"] for event in events],
            )
            self.assertTrue(verified)
            self.assertFalse(wrong_hash_verified)
            self.assertEqual(OFFICIAL_BOUNDARY_NOTICE, manifest["boundary_notice"])
            self.assertEqual(frozen_hash, manifest["frozen_content_hash"])
            self.assertRegex(str(manifest["content_hash"]), r"^[0-9a-f]{64}$")
            for expected_revision, event in enumerate(events, start=1):
                self.assertEqual(expected_revision, event["revision"])
                self.assertEqual("tenant-a", event["tenant_id"])
                self.assertTrue(event["actor"])
                self.assertIn("reason", event)
                self.assertRegex(str(event["content_hash"]), r"^[0-9a-f]{64}$")

    def test_snapshot_tampering_is_detected_before_deserialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            snapshot_path = (
                data_dir
                / "authoring"
                / "snapshots"
                / str(created.project_id)
                / "00000000000000000001.json"
            )
            snapshot_path.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "integrity check",
            ):
                repository.get_project(
                    str(created.project_id),
                    tenant_id="tenant-a",
                )

    def test_manifest_boundary_notice_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository = AuthoringRepository(data_dir)
            created = repository.create_project(
                _project(tenant_id="tenant-a"),
                actor="author-a",
            )
            manifest_path = (
                data_dir
                / "authoring"
                / "projects"
                / f"{created.project_id}.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["boundary_notice"] = "공식 승인"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "boundary notice",
            ):
                repository.get_project(
                    str(created.project_id),
                    tenant_id="tenant-a",
                )


def _project(
    *,
    tenant_id: str,
    title: str = "Beginner Regulation Draft",
    purpose: str = "Describe the institution's internal operating rule.",
    clauses: list[ClauseDraft] | None = None,
) -> AuthoringProject:
    return AuthoringProject(
        project_id=uuid4(),
        tenant_id=tenant_id,
        profile_id="institution-a",
        title=title,
        purpose=purpose,
        clauses=list(clauses or []),
        created_by="author-a",
        updated_by="author-a",
    )


def _with_semantic_hash(project: AuthoringProject) -> AuthoringProject:
    return project.model_copy(
        update={"semantic_content_hash": semantic_content_hash(project)}
    )


if __name__ == "__main__":
    unittest.main()
