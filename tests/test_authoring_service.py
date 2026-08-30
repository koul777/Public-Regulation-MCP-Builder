from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    AuthoringExportRequest,
    AuthoringProject,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectStatus,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    DraftNodeType,
)
from app.services.authoring_service import (
    AuthoringConflictError,
    AuthoringSelfFreezeError,
    AuthoringService,
    AuthoringStateTransitionError,
    semantic_content_hash,
)
from app.services.authoring_safety_service import REDACTED_AUTHORING_REASON
from app.storage.authoring_repository import AuthoringRepositoryIntegrityError


class AuthoringServiceTests(unittest.TestCase):
    def test_freeze_staging_failure_does_not_publish_state_or_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            events_before = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

            with patch.object(
                service.repository,
                "_validate_staged_generation",
                side_effect=AuthoringRepositoryIntegrityError(
                    "simulated staged-generation verification failure"
                ),
            ):
                with self.assertRaises(AuthoringRepositoryIntegrityError):
                    service.freeze_project(
                        review.project_id,
                        AuthoringProjectFreezeRequest(
                            expected_revision=review.revision,
                        ),
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )

            loaded = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events_after = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            next_generation_name = f"{review.revision + 1:020d}.json"
            staged_snapshot = (
                service.repository.snapshots_root
                / str(review.project_id)
                / next_generation_name
            )
            staged_event = (
                service.repository.events_root
                / str(review.project_id)
                / next_generation_name
            )
            staged_snapshot_exists = staged_snapshot.exists()
            staged_event_exists = staged_event.exists()

        self.assertEqual(AuthoringProjectStatus.REVIEW_REQUESTED, loaded.status)
        self.assertEqual(review.revision, loaded.revision)
        self.assertEqual(events_before, events_after)
        self.assertFalse(staged_snapshot_exists)
        self.assertFalse(staged_event_exists)

    def test_local_self_freeze_requires_explicit_training_consent_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            project = _review_requested_project(service, actor="author-a")

            with self.assertRaisesRegex(AuthoringSelfFreezeError, "training-only"):
                service.freeze_project(
                    project.project_id,
                    AuthoringProjectFreezeRequest(expected_revision=project.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )

            artifact = service.freeze_project(
                project.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=project.revision,
                    allow_training_self_freeze=True,
                    comment="로컬 작성 흐름을 연습하기 위한 자체 확인입니다.",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )
            frozen = service.get_project(
                project.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                project.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.CONTENT_FROZEN, frozen.status)
        self.assertTrue(artifact.training_only)
        self.assertTrue(frozen.training_only)
        self.assertEqual(frozen.revision, frozen.frozen_revision)
        self.assertEqual(frozen.semantic_content_hash, artifact.content_hash)
        self.assertEqual(frozen.frozen_content_hash, artifact.content_hash)
        self.assertEqual("content_frozen", events[-1]["event_type"])
        self.assertEqual("author-a", events[-1]["actor"])
        self.assertTrue(events[-1]["metadata"]["self_freeze"])
        self.assertTrue(events[-1]["metadata"]["training_only"])

    def test_authenticated_or_protected_mode_rejects_self_freeze(self) -> None:
        cases = (
            Settings(app_env="local", api_auth_required=True),
            Settings(app_env="production", api_auth_required=False),
        )
        for base_settings in cases:
            with self.subTest(settings=base_settings), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(
                    **{
                        **base_settings.__dict__,
                        "data_dir": Path(tmp) / "data",
                    }
                )
                service = AuthoringService(settings)
                project = _review_requested_project(service, actor="author-a")

                with self.assertRaisesRegex(AuthoringSelfFreezeError, "reviewer"):
                    service.freeze_project(
                        project.project_id,
                        AuthoringProjectFreezeRequest(
                            expected_revision=project.revision,
                            allow_training_self_freeze=True,
                            comment="명시했더라도 보호 환경에서는 허용되면 안 됩니다.",
                        ),
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="author-a",
                    )

                artifact = service.freeze_project(
                    project.project_id,
                    AuthoringProjectFreezeRequest(
                        expected_revision=project.revision,
                        comment="독립 검토자가 동결합니다.",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )

                self.assertFalse(artifact.training_only)

    def test_changes_freeze_json_export_and_reopen_follow_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            changes = service.request_changes(
                review.project_id,
                AuthoringTransitionRequest(
                    expected_revision=review.revision,
                    comment="담당부서의 기록 보관 책임을 더 명확히 적어 주세요.",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            drafting = service.start_drafting(
                review.project_id,
                AuthoringTransitionRequest(expected_revision=changes.revision),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )
            second_review = service.request_review(
                review.project_id,
                AuthoringTransitionRequest(expected_revision=drafting.revision),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )
            frozen_artifact = service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=second_review.revision,
                    comment="수정 사항을 확인했습니다.",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            export = service.export_project(
                review.project_id,
                AuthoringExportRequest(
                    expected_revision=frozen.revision,
                    export_format="json",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            exported = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            reopened = service.reopen_project(
                review.project_id,
                AuthoringTransitionRequest(
                    expected_revision=exported.revision,
                    comment="다음 개정을 작성합니다.",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )
            events = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

            export_path = (
                Path(tmp)
                / "data"
                / "authoring"
                / "exports"
                / str(review.project_id)
                / f"{frozen_artifact.revision:020d}"
                / f"{frozen_artifact.content_hash}.json"
            )
            stored = export_path.read_bytes()

        self.assertEqual(
            "담당부서의 기록 보관 책임을 더 명확히 적어 주세요.",
            changes.change_request_comment,
        )
        self.assertEqual(changes.change_request_comment, drafting.change_request_comment)
        self.assertIsNone(second_review.change_request_comment)
        self.assertNotIn(
            "담당부서의 기록 보관 책임",
            json.dumps(events, ensure_ascii=False),
        )
        payload = json.loads(export.content.decode("utf-8"))
        self.assertEqual(OFFICIAL_BOUNDARY_NOTICE, payload["boundary_notice"])
        self.assertFalse(payload["official_approval"])
        self.assertEqual(frozen_artifact.content_hash, payload["semantic_content_sha256"])
        self.assertEqual(hashlib.sha256(stored).hexdigest(), export.content_sha256)
        self.assertEqual(export.content, stored)
        self.assertEqual(AuthoringProjectStatus.EXPORTED, exported.status)
        self.assertEqual("reviewer-b", exported.exported_by)
        self.assertIsNotNone(exported.exported_at)
        self.assertEqual(AuthoringProjectStatus.DRAFTING, reopened.status)
        self.assertIsNone(reopened.last_lint_report)
        self.assertEqual(frozen_artifact.revision, reopened.frozen_revision)
        self.assertEqual(frozen_artifact.content_hash, reopened.frozen_content_hash)
        self.assertEqual(
            [
                "created",
                "updated",
                "updated",
                "review_requested",
                "changes_requested",
                "updated",
                "review_requested",
                "content_frozen",
                "exported",
                "reopened",
            ],
            [event["event_type"] for event in events],
        )
        self.assertEqual("reviewer-b", events[-3]["actor"])
        self.assertEqual("author-a", events[-1]["actor"])

    def test_markdown_export_contains_boundary_and_semantic_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            artifact = service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            export = service.export_project(
                review.project_id,
                AuthoringExportRequest(
                    expected_revision=frozen.revision,
                    export_format="markdown",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )

        text = export.content.decode("utf-8")
        self.assertTrue(text.startswith(f"> **{OFFICIAL_BOUNDARY_NOTICE}**\n"))
        self.assertIn(OFFICIAL_BOUNDARY_NOTICE, text)
        self.assertIn(artifact.content_hash, text)
        self.assertIn("기존 시스템 승인 또는 공개를 뜻하지 않습니다", text)
        self.assertTrue(export.filename.endswith(".md"))

    def test_markdown_export_keeps_boundary_before_a_multiline_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            project = AuthoringProject.model_validate(
                {
                    **_drafting_project(service, actor="author-a").model_dump(mode="json"),
                    "title": "기록 관리 규정\n# 사용자 입력 제목",
                    "responsible_department": "기록\n관리부",
                    "legal_bases": ["정관 제10조\n# 사용자 입력 근거"],
                    "references": [
                        {
                            "citation": "정관 제10조\n- 사용자 입력 인용",
                            "source_title": "근거 자료\n## 사용자 입력 자료명",
                        }
                    ],
                    "frozen_revision": 2,
                    "frozen_content_hash": "a" * 64,
                }
            )

            text = service._render_markdown_export(project).decode("utf-8")

        self.assertTrue(text.startswith(f"> **{OFFICIAL_BOUNDARY_NOTICE}**\n"))
        self.assertGreater(text.index("# 기록 관리 규정"), text.index(OFFICIAL_BOUNDARY_NOTICE))
        self.assertNotIn("\n# 사용자 입력 제목", text)
        self.assertIn("- 담당부서: 기록 관리부", text)
        self.assertIn("- 정관 제10조 # 사용자 입력 근거", text)
        self.assertNotIn("\n# 사용자 입력 근거", text)
        self.assertIn(
            "- 정관 제10조 - 사용자 입력 인용 (근거 자료 ## 사용자 입력 자료명)",
            text,
        )
        self.assertNotIn("\n- 사용자 입력 인용", text)
        self.assertNotIn("\n## 사용자 입력 자료명", text)

    def test_export_can_be_reloaded_in_a_fresh_service_without_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original = service.export_project(
                review.project_id,
                AuthoringExportRequest(
                    expected_revision=frozen.revision,
                    export_format="markdown",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            exported = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events_before = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

            restored = _service(tmp, api_auth_required=True).get_export(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            after = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events_after = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(original.content, restored.content)
        self.assertEqual(original.content_sha256, restored.content_sha256)
        self.assertEqual(original.filename, restored.filename)
        self.assertEqual(exported.revision, after.revision)
        self.assertEqual(events_before, events_after)

    def test_get_export_rejects_missing_or_tampered_artifact(self) -> None:
        for scenario in ("missing", "appended", "boundary_removed", "non_utf8"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                service = _service(tmp, api_auth_required=True)
                review = _review_requested_project(service, actor="author-a")
                service.freeze_project(
                    review.project_id,
                    AuthoringProjectFreezeRequest(
                        expected_revision=review.revision,
                        comment="독립 검토 완료",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )
                frozen = service.get_project(
                    review.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
                artifact = service.export_project(
                    review.project_id,
                    AuthoringExportRequest(
                        expected_revision=frozen.revision,
                        export_format="markdown",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )
                export_path = (
                    Path(tmp)
                    / "data"
                    / "authoring"
                    / "exports"
                    / str(review.project_id)
                    / f"{artifact.frozen_revision:020d}"
                    / f"{artifact.semantic_content_sha256}.md"
                )
                if scenario == "missing":
                    export_path.unlink()
                elif scenario == "appended":
                    export_path.write_bytes(artifact.content + b"\nTAMPERED\n")
                elif scenario == "boundary_removed":
                    notice = OFFICIAL_BOUNDARY_NOTICE.encode("utf-8")
                    export_path.write_bytes(
                        artifact.content.replace(notice, b"X" * len(notice), 1)
                    )
                else:
                    export_path.write_bytes(b"\xff" + artifact.content[1:])

                with self.assertRaises(AuthoringRepositoryIntegrityError):
                    _service(tmp, api_auth_required=True).get_export(
                        review.project_id,
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )

    def test_freeze_requires_every_beginner_checklist_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            # Keep the freeze check as defence in depth for an older or
            # externally migrated review snapshot. New service flows block an
            # incomplete checklist before entering REVIEW_REQUESTED.
            checklist = list(review.checklist)
            checklist[0] = checklist[0].model_copy(update={"completed": False})
            review = service.repository.save_project(
                review.model_copy(update={"checklist": checklist}),
                tenant_id="tenant-a",
                expected_revision=review.revision,
                actor="legacy-migration",
                event_type="updated",
                event_metadata={"migration": True},
            )
            event_count = len(
                service.list_events(
                    review.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

            with self.assertRaisesRegex(
                AuthoringStateTransitionError,
                "Every beginner checklist item",
            ):
                service.freeze_project(
                    review.project_id,
                    AuthoringProjectFreezeRequest(
                        expected_revision=review.revision,
                        comment="독립 검토 완료",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )

            unchanged = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.REVIEW_REQUESTED, unchanged.status)
        self.assertEqual(event_count, len(events))

    def test_freeze_rejects_a_tampered_legacy_checklist_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            tampered = [
                review.checklist[0].model_copy(
                    update={"label": "검토 없이 완료", "completed": True}
                )
            ]
            review = service.repository.save_project(
                review.model_copy(update={"checklist": tampered}),
                tenant_id="tenant-a",
                expected_revision=review.revision,
                actor="legacy-migration",
                event_type="updated",
                event_metadata={"migration": True},
            )
            event_count = len(
                service.list_events(
                    review.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

            with self.assertRaisesRegex(
                AuthoringStateTransitionError,
                "checklist definition",
            ):
                service.freeze_project(
                    review.project_id,
                    AuthoringProjectFreezeRequest(
                        expected_revision=review.revision,
                        comment="독립 검토 완료",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )

            unchanged = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.REVIEW_REQUESTED, unchanged.status)
        self.assertEqual(event_count, len(events))

    def test_freeze_rejects_a_tampered_server_clause_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            clauses = list(review.clauses)
            clauses[0] = clauses[0].model_copy(
                update={"beginner_guidance": "변조된 구조 안내"}
            )
            candidate = review.model_copy(update={"clauses": clauses})
            candidate = candidate.model_copy(
                update={"semantic_content_hash": semantic_content_hash(candidate)}
            )
            tampered = service.repository.save_project(
                candidate,
                tenant_id="tenant-a",
                expected_revision=review.revision,
                actor="legacy-migration",
                event_type="updated",
                event_metadata={"migration": True},
            )
            event_count = len(
                service.list_events(
                    tampered.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

            with self.assertRaisesRegex(
                AuthoringStateTransitionError,
                "server-owned definition",
            ):
                service.freeze_project(
                    tampered.project_id,
                    AuthoringProjectFreezeRequest(
                        expected_revision=tampered.revision,
                        comment="구조 변조를 놓친 검토",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )

            unchanged = service.get_project(
                tampered.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                tampered.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.REVIEW_REQUESTED, unchanged.status)
        self.assertEqual(tampered.revision, unchanged.revision)
        self.assertEqual(event_count, len(events))

    def test_export_rejects_current_content_that_differs_from_frozen_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            clauses = list(frozen.clauses)
            clauses[0] = clauses[0].model_copy(
                update={"body": "UNFROZEN_REPLAY_MARKER"}
            )
            candidate = frozen.model_copy(update={"clauses": clauses})
            candidate = candidate.model_copy(
                update={"semantic_content_hash": semantic_content_hash(candidate)}
            )
            malformed = service.repository.save_project(
                candidate,
                tenant_id="tenant-a",
                expected_revision=frozen.revision,
                actor="legacy-migration",
                event_type="updated",
                event_metadata={"migration": True},
            )
            event_count = len(
                service.list_events(
                    malformed.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "does not match its declared content hash",
            ):
                service.export_project(
                    malformed.project_id,
                    AuthoringExportRequest(
                        expected_revision=malformed.revision,
                        export_format="json",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )

            unchanged = service.get_project(
                malformed.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                malformed.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.CONTENT_FROZEN, unchanged.status)
        self.assertEqual(malformed.revision, unchanged.revision)
        self.assertEqual(event_count, len(events))

    def test_export_rejects_symbolic_link_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            exports_dir = Path(tmp) / "data" / "authoring" / "exports"
            outside = Path(tmp) / "outside"
            outside.mkdir()
            try:
                os.symlink(outside, exports_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(
                AuthoringRepositoryIntegrityError,
                "symbolic links",
            ):
                service.export_project(
                    review.project_id,
                    AuthoringExportRequest(
                        expected_revision=frozen.revision,
                        export_format="json",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )

    def test_export_failure_keeps_verified_project_content_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            event_count = len(
                service.list_events(
                    review.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

            with patch.object(
                service.repository,
                "_commit_generation",
                side_effect=OSError("simulated export failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated export failure"):
                    service.export_project(
                        review.project_id,
                        AuthoringExportRequest(
                            expected_revision=frozen.revision,
                            export_format="json",
                        ),
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )

            unchanged = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            export_files = list(
                (Path(tmp) / "data" / "authoring" / "exports").rglob("*.json")
            )

        self.assertEqual(AuthoringProjectStatus.CONTENT_FROZEN, unchanged.status)
        self.assertEqual(frozen.revision, unchanged.revision)
        self.assertEqual(event_count, len(events))
        self.assertEqual([], export_files)

    def test_losing_concurrent_export_does_not_delete_winner_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            competing_service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            request = AuthoringExportRequest(
                expected_revision=frozen.revision,
                export_format="json",
            )
            winning_artifact = None
            original_save = service.repository.save_exported_project

            def commit_competing_export(*args, **kwargs):
                nonlocal winning_artifact
                winning_artifact = competing_service.export_project(
                    review.project_id,
                    request,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )
                return original_save(*args, **kwargs)

            with patch.object(
                service.repository,
                "save_exported_project",
                side_effect=commit_competing_export,
            ):
                with self.assertRaises(AuthoringConflictError):
                    service.export_project(
                        review.project_id,
                        request,
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )

            self.assertIsNotNone(winning_artifact)
            export_files = list((Path(tmp) / "data" / "authoring" / "exports").rglob("*.json"))
            self.assertEqual(1, len(export_files))
            self.assertEqual(winning_artifact.content, export_files[0].read_bytes())
            committed = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            self.assertEqual(AuthoringProjectStatus.EXPORTED, committed.status)

    def test_export_losing_to_reopen_removes_uncommitted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            competing_service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            request = AuthoringExportRequest(
                expected_revision=frozen.revision,
                export_format="json",
            )
            original_save = service.repository.save_exported_project

            def commit_competing_reopen(*args, **kwargs):
                competing_service.reopen_project(
                    review.project_id,
                    AuthoringTransitionRequest(expected_revision=frozen.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )
                return original_save(*args, **kwargs)

            with patch.object(
                service.repository,
                "save_exported_project",
                side_effect=commit_competing_reopen,
            ):
                with self.assertRaises(AuthoringConflictError):
                    service.export_project(
                        review.project_id,
                        request,
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )

            export_files = list(
                (Path(tmp) / "data" / "authoring" / "exports").rglob("*.json")
            )
            committed = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual([], export_files)
        self.assertEqual(AuthoringProjectStatus.DRAFTING, committed.status)

    def test_export_starting_after_completed_purge_cannot_recreate_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            purge_repository = _service(tmp, api_auth_required=True).repository
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="independent review complete",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original_save = service.repository.save_exported_project
            purge_result = None

            def purge_then_try_export(*args, **kwargs):
                nonlocal purge_result
                purge_result = purge_repository.purge_profile_projects(
                    "institution-a",
                    tenant_id="tenant-a",
                )
                return original_save(*args, **kwargs)

            with patch.object(
                service.repository,
                "save_exported_project",
                side_effect=purge_then_try_export,
            ):
                with self.assertRaises(KeyError):
                    service.export_project(
                        review.project_id,
                        AuthoringExportRequest(
                            expected_revision=frozen.revision,
                            export_format="json",
                        ),
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )

            export_files = list(
                (Path(tmp) / "data" / "authoring" / "exports").rglob("*.json")
            )
            remaining_count = purge_repository.profile_project_count(
                "institution-a",
                tenant_id="tenant-a",
            )

        self.assertIsNotNone(purge_result)
        self.assertTrue(purge_result.completed)
        self.assertEqual(0, remaining_count)
        self.assertEqual([], export_files)

    def test_purge_cannot_enter_between_export_artifact_and_manifest_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            purge_repository = _service(tmp, api_auth_required=True).repository
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="independent review complete",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            writer_entered = Event()
            release_writer = Event()
            purge_started = Event()
            purge_finished = Event()
            export_errors: list[BaseException] = []
            purge_results = []
            original_writer = service.repository._write_export_artifact_unlocked

            def paused_writer(path, content):
                writer_entered.set()
                if not release_writer.wait(5):
                    raise TimeoutError("test did not release export writer")
                return original_writer(path, content)

            def run_export() -> None:
                try:
                    service.export_project(
                        review.project_id,
                        AuthoringExportRequest(
                            expected_revision=frozen.revision,
                            export_format="json",
                        ),
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )
                except BaseException as exc:  # captured for the test thread
                    export_errors.append(exc)

            def run_purge() -> None:
                purge_started.set()
                purge_results.append(
                    purge_repository.purge_profile_projects(
                        "institution-a",
                        tenant_id="tenant-a",
                    )
                )
                purge_finished.set()

            with patch.object(
                service.repository,
                "_write_export_artifact_unlocked",
                side_effect=paused_writer,
            ):
                export_thread = Thread(target=run_export)
                export_thread.start()
                self.assertTrue(writer_entered.wait(5))
                purge_thread = Thread(target=run_purge)
                purge_thread.start()
                self.assertTrue(purge_started.wait(5))
                self.assertFalse(purge_finished.wait(0.1))
                release_writer.set()
                export_thread.join(5)
                purge_thread.join(5)

            self.assertFalse(export_thread.is_alive())
            self.assertFalse(purge_thread.is_alive())
            self.assertEqual([], export_errors)
            self.assertEqual(1, len(purge_results))
            self.assertTrue(purge_results[0].completed)
            self.assertEqual(
                0,
                purge_repository.profile_project_count(
                    "institution-a",
                    tenant_id="tenant-a",
                ),
            )
            self.assertEqual(
                [],
                list(
                    (Path(tmp) / "data" / "authoring" / "exports").rglob(
                        "*.json"
                    )
                ),
            )

    def test_late_loser_keeps_export_that_won_before_immediate_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            competing_service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="독립 검토 완료",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            request = AuthoringExportRequest(
                expected_revision=frozen.revision,
                export_format="json",
            )
            winning_artifact = None
            original_save = service.repository.save_exported_project

            def commit_export_then_reopen(*args, **kwargs):
                nonlocal winning_artifact
                winning_artifact = competing_service.export_project(
                    review.project_id,
                    request,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )
                exported = competing_service.get_project(
                    review.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
                competing_service.reopen_project(
                    review.project_id,
                    AuthoringTransitionRequest(expected_revision=exported.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="reviewer-b",
                )
                return original_save(*args, **kwargs)

            with patch.object(
                service.repository,
                "save_exported_project",
                side_effect=commit_export_then_reopen,
            ):
                with self.assertRaises(AuthoringConflictError):
                    service.export_project(
                        review.project_id,
                        request,
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                        actor="reviewer-b",
                    )

            export_files = list(
                (Path(tmp) / "data" / "authoring" / "exports").rglob("*.json")
            )
            export_bytes = export_files[0].read_bytes() if len(export_files) == 1 else b""
            committed = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertIsNotNone(winning_artifact)
        self.assertEqual(1, len(export_files))
        self.assertEqual(winning_artifact.content, export_bytes)
        self.assertEqual(AuthoringProjectStatus.DRAFTING, committed.status)

    def test_service_keeps_review_text_in_snapshot_and_only_hashes_event_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            review = _review_requested_project(service, actor="author-a")

            changed = service.request_changes(
                review.project_id,
                AuthoringTransitionRequest(
                    expected_revision=review.revision,
                    comment="C:" + r"\Users\Jane Doe\private draft.docx를 확인하세요.",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            events = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        expected_hash = hashlib.sha256(
            REDACTED_AUTHORING_REASON.encode("utf-8")
        ).hexdigest()
        self.assertEqual(REDACTED_AUTHORING_REASON, changed.change_request_comment)
        self.assertEqual("provided", events[-1]["reason"])
        self.assertEqual(expected_hash, events[-1]["metadata"]["reason_sha256"])
        self.assertNotIn("Jane Doe", json.dumps(events, ensure_ascii=False))

    def test_transition_comment_text_is_absent_from_immutable_events(self) -> None:
        marker = "DRAFT_BODY_MARKER_7f21 copied from a clause"
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            review = _review_requested_project(service, actor="author-a")

            changed = service.request_changes(
                review.project_id,
                AuthoringTransitionRequest(
                    expected_revision=review.revision,
                    comment=marker,
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            events = service.list_events(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(marker, changed.change_request_comment)
        self.assertNotIn(marker, json.dumps(events, ensure_ascii=False))
        self.assertEqual("provided", events[-1]["reason"])
        self.assertEqual(
            hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            events[-1]["metadata"]["reason_sha256"],
        )

    def test_blocking_lint_prevents_review_without_appending_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            created = service.create_project(
                _create_request().model_copy(update={"title": "미완성 규정"}),
                tenant_id="tenant-a",
                actor="author-a",
            )
            drafting = service.start_drafting(
                created.project_id,
                AuthoringTransitionRequest(expected_revision=created.revision),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )

            with self.assertRaisesRegex(AuthoringStateTransitionError, "blocking lint"):
                service.request_review(
                    created.project_id,
                    AuthoringTransitionRequest(expected_revision=drafting.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )

            events = service.list_events(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(2, len(events))
        self.assertEqual("updated", events[-1]["event_type"])

    def test_start_drafting_requires_saved_metadata_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            created = service.create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-a",
                    title="미완성 규정",
                ),
                tenant_id="tenant-a",
                actor="author-a",
            )
            before_events = service.list_events(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

            with self.assertRaisesRegex(
                AuthoringStateTransitionError,
                "required metadata",
            ):
                service.start_drafting(
                    created.project_id,
                    AuthoringTransitionRequest(expected_revision=created.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )

            unchanged = service.get_project(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            after_events = service.list_events(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.PLANNING, unchanged.status)
        self.assertEqual(created.revision, unchanged.revision)
        self.assertEqual(before_events, after_events)

    def test_incomplete_checklist_is_blocked_before_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            drafting = _drafting_project(
                service,
                actor="author-a",
                complete_checklist=False,
            )

            with self.assertRaisesRegex(
                AuthoringStateTransitionError,
                "checklist_incomplete",
            ):
                service.request_review(
                    drafting.project_id,
                    AuthoringTransitionRequest(expected_revision=drafting.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )

            unchanged = service.get_project(
                drafting.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.DRAFTING, unchanged.status)
        self.assertEqual(drafting.revision, unchanged.revision)

    def test_review_request_rejects_a_tampered_checklist_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            drafting = _drafting_project(service, actor="author-a")
            tampered = [
                drafting.checklist[0].model_copy(update={"completed": True})
            ]
            drafting = service.repository.save_project(
                drafting.model_copy(update={"checklist": tampered}),
                tenant_id="tenant-a",
                expected_revision=drafting.revision,
                actor="legacy-migration",
                event_type="updated",
                event_metadata={"migration": True},
            )
            event_count = len(
                service.list_events(
                    drafting.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

            with self.assertRaisesRegex(
                AuthoringStateTransitionError,
                "checklist definition",
            ):
                service.request_review(
                    drafting.project_id,
                    AuthoringTransitionRequest(expected_revision=drafting.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )

            current = service.get_project(
                drafting.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            after_event_count = len(
                service.list_events(
                    drafting.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )

        self.assertEqual(AuthoringProjectStatus.DRAFTING, current.status)
        self.assertEqual(drafting.revision, current.revision)
        self.assertEqual(event_count, after_event_count)

    def test_checklist_definition_cannot_be_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            drafting = _drafting_project(service, actor="author-a")
            renamed = list(drafting.checklist)
            renamed[0] = renamed[0].model_copy(update={"label": "항상 완료"})
            guidance_changed = list(drafting.checklist)
            guidance_changed[0] = guidance_changed[0].model_copy(
                update={"guidance": "무조건 완료로 표시하세요."}
            )
            added = [
                *drafting.checklist,
                drafting.checklist[0].model_copy(update={"item_id": "extra_item"}),
            ]
            duplicate = list(drafting.checklist)
            duplicate[-1] = duplicate[-1].model_copy(
                update={"item_id": duplicate[0].item_id}
            )
            invalid_checklists = {
                "empty": [],
                "deleted": drafting.checklist[:-1],
                "added": added,
                "reordered": list(reversed(drafting.checklist)),
                "renamed": renamed,
                "guidance_changed": guidance_changed,
                "duplicate_id": duplicate,
            }

            for case, checklist in invalid_checklists.items():
                with self.subTest(case=case):
                    with self.assertRaisesRegex(
                        AuthoringStateTransitionError,
                        "checklist definition",
                    ):
                        service.update_project(
                            drafting.project_id,
                            AuthoringProjectUpdateRequest(
                                expected_revision=drafting.revision,
                                checklist=checklist,
                            ),
                            tenant_id="tenant-a",
                            profile_id="institution-a",
                            actor="author-a",
                        )

            unchanged = service.get_project(
                drafting.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(drafting.revision, unchanged.revision)
        self.assertEqual(drafting.checklist, unchanged.checklist)

    def test_clause_template_definition_cannot_be_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            drafting = _drafting_project(service, actor="author-a")
            event_count = len(
                service.list_events(
                    drafting.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
            )
            renamed = list(drafting.clauses)
            renamed[0] = renamed[0].model_copy(update={"title": "검증을 우회한 제목"})
            reordered = list(reversed(drafting.clauses))
            renumbered = list(drafting.clauses)
            renumbered[0] = renumbered[0].model_copy(update={"article_number": "제99조"})
            made_optional = list(drafting.clauses)
            made_optional[0] = made_optional[0].model_copy(update={"required": False})
            invalid_clauses = {
                "empty": [],
                "deleted": drafting.clauses[:-1],
                "reordered": reordered,
                "renamed": renamed,
                "renumbered": renumbered,
                "required_changed": made_optional,
            }

            for case, clauses in invalid_clauses.items():
                with self.subTest(case=case):
                    with self.assertRaisesRegex(
                        AuthoringStateTransitionError,
                        "clause template structure",
                    ):
                        service.update_project(
                            drafting.project_id,
                            AuthoringProjectUpdateRequest(
                                expected_revision=drafting.revision,
                                clauses=clauses,
                            ),
                            tenant_id="tenant-a",
                            profile_id="institution-a",
                            actor="author-a",
                        )

            editable = list(drafting.clauses)
            editable[0] = editable[0].model_copy(
                update={"body": "이 규정은 업무 처리 기준을 정함을 목적으로 한다."}
            )
            updated = service.update_project(
                drafting.project_id,
                AuthoringProjectUpdateRequest(
                    expected_revision=drafting.revision,
                    clauses=editable,
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )

            events = service.list_events(
                drafting.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(drafting.revision + 1, updated.revision)
        self.assertEqual(editable, updated.clauses)
        self.assertEqual(event_count + 1, len(events))

    def test_stale_revision_and_invalid_transition_do_not_mutate_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            created = service.create_project(
                _create_request(),
                tenant_id="tenant-a",
                actor="author-a",
            )
            before_events = service.list_events(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

            with self.assertRaises(AuthoringConflictError) as conflict:
                service.start_drafting(
                    created.project_id,
                    AuthoringTransitionRequest(expected_revision=99),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )
            with self.assertRaises(AuthoringStateTransitionError):
                service.request_review(
                    created.project_id,
                    AuthoringTransitionRequest(
                        expected_revision=created.revision,
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                    actor="author-a",
                )

            unchanged = service.get_project(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            after_events = service.list_events(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(99, conflict.exception.expected_revision)
        self.assertEqual(1, conflict.exception.actual_revision)
        self.assertEqual(AuthoringProjectStatus.PLANNING, unchanged.status)
        self.assertEqual(before_events, after_events)

    def test_same_tenant_different_profile_is_missing_for_project_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            created = service.create_project(
                _create_request(),
                tenant_id="tenant-a",
                actor="author-a",
            )
            operations = {
                "get": lambda: service.get_project(
                    created.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                ),
                "events": lambda: service.list_events(
                    created.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                ),
                "lint": lambda: service.lint_project(
                    created.project_id,
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                ),
                "update": lambda: service.update_project(
                    created.project_id,
                    AuthoringProjectUpdateRequest(
                        expected_revision=created.revision,
                        title="다른 기관에서 바꾼 제목",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                    actor="operator-b",
                ),
                "transition": lambda: service.start_drafting(
                    created.project_id,
                    AuthoringTransitionRequest(expected_revision=created.revision),
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                    actor="operator-b",
                ),
            }

            for operation, invoke in operations.items():
                with self.subTest(operation=operation), self.assertRaises(KeyError):
                    invoke()

            unchanged = service.get_project(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            events = service.list_events(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(1, unchanged.revision)
        self.assertEqual(["created"], [event["event_type"] for event in events])

    def test_profile_scope_uses_canonical_institution_profile_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            request = _create_request().model_copy(
                update={"profile_id": "  Institution-A  "}
            )
            created = service.create_project(
                request,
                tenant_id="tenant-a",
                actor="author-a",
            )
            loaded = service.get_project(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="INSTITUTION-A",
            )
            projects = service.list_projects(
                tenant_id="tenant-a",
                profile_id=" institution-A ",
            )

        self.assertEqual("institution-a", created.profile_id)
        self.assertEqual(created.project_id, loaded.project_id)
        self.assertEqual([created.project_id], [project.project_id for project in projects])

    def test_same_tenant_different_profile_cannot_freeze_or_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, api_auth_required=True)
            review = _review_requested_project(service, actor="author-a")
            with self.assertRaises(KeyError):
                service.freeze_project(
                    review.project_id,
                    AuthoringProjectFreezeRequest(
                        expected_revision=review.revision,
                        comment="다른 프로필 검토",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                    actor="reviewer-b",
                )

            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="해당 프로필 독립 검토",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            with self.assertRaises(KeyError):
                service.export_project(
                    review.project_id,
                    AuthoringExportRequest(
                        expected_revision=frozen.revision,
                        export_format="json",
                    ),
                    tenant_id="tenant-a",
                    profile_id="institution-b",
                    actor="reviewer-b",
                )

            unchanged = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(AuthoringProjectStatus.CONTENT_FROZEN, unchanged.status)

    def test_semantic_hash_changes_only_when_authored_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            created = service.create_project(
                _create_request(),
                tenant_id="tenant-a",
                actor="author-a",
            )
            original_hash = created.semantic_content_hash
            drafting = service.start_drafting(
                created.project_id,
                AuthoringTransitionRequest(expected_revision=created.revision),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )
            updated = service.update_project(
                created.project_id,
                AuthoringProjectUpdateRequest(
                    expected_revision=drafting.revision,
                    purpose="변경된 목적 문장입니다.",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )

        self.assertEqual(original_hash, drafting.semantic_content_hash)
        self.assertNotEqual(original_hash, updated.semantic_content_hash)
        self.assertEqual(semantic_content_hash(updated), updated.semantic_content_hash)

    def test_training_only_is_permanent_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            review = _review_requested_project(service, actor="author-a")
            service.freeze_project(
                review.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    allow_training_self_freeze=True,
                    comment="로컬 연습 동결",
                ),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )
            frozen = service.get_project(
                review.project_id,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            reopened = service.reopen_project(
                review.project_id,
                AuthoringTransitionRequest(expected_revision=frozen.revision),
                tenant_id="tenant-a",
                profile_id="institution-a",
                actor="author-a",
            )

        self.assertTrue(reopened.training_only)

    def test_service_has_no_official_approval_or_rag_dependency(self) -> None:
        source = Path("app/services/authoring_service.py").read_text(encoding="utf-8")
        forbidden_imports = (
            "app.schemas.document",
            "app.schemas.chunk",
            "approval_journal",
            "vector_adapter",
            "app.retrieval",
            "app.mcp_server",
        )
        for forbidden in forbidden_imports:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


def _service(
    tmp: str,
    *,
    api_auth_required: bool = False,
) -> AuthoringService:
    return AuthoringService(
        Settings(
            app_env="local",
            api_auth_required=api_auth_required,
            data_dir=Path(tmp) / "data",
            artifact_root=Path(tmp),
        )
    )


def _create_request() -> AuthoringProjectCreateRequest:
    return AuthoringProjectCreateRequest(
        profile_id="institution-a",
        title="기록 관리 규정",
        purpose="기관의 기록 관리 절차와 책임을 명확히 정합니다.",
        scope="기관의 모든 부서와 기록 관리 담당자에게 적용합니다.",
        legal_bases=["공공기록물 관리에 관한 법률 및 기관 내부 방침"],
        responsible_department="기록관리부",
        planned_effective_date=date(2026, 10, 1),
    )


def _review_requested_project(
    service: AuthoringService,
    *,
    actor: str,
    complete_checklist: bool = True,
) -> AuthoringProject:
    drafting = _drafting_project(
        service,
        actor=actor,
        complete_checklist=complete_checklist,
    )
    return service.request_review(
        drafting.project_id,
        AuthoringTransitionRequest(expected_revision=drafting.revision),
        tenant_id="tenant-a",
        profile_id="institution-a",
        actor=actor,
    )


def _drafting_project(
    service: AuthoringService,
    *,
    actor: str,
    complete_checklist: bool = True,
) -> AuthoringProject:
    created = service.create_project(
        _create_request(),
        tenant_id="tenant-a",
        actor=actor,
    )
    clauses = []
    for clause in created.clauses:
        if clause.required and clause.node_type not in {
            DraftNodeType.CHAPTER,
            DraftNodeType.SECTION,
        }:
            clauses.append(
                clause.model_copy(
                    update={
                        "body": f"{clause.title or clause.article_number}에 필요한 담당자, 절차, 기록 기준을 구체적으로 정합니다."
                    }
                )
            )
        else:
            clauses.append(clause)
    updated = service.update_project(
        created.project_id,
        AuthoringProjectUpdateRequest(
            expected_revision=created.revision,
            clauses=clauses,
            checklist=[
                item.model_copy(update={"completed": complete_checklist})
                for item in created.checklist
            ],
        ),
        tenant_id="tenant-a",
        profile_id="institution-a",
        actor=actor,
    )
    drafting = service.start_drafting(
        created.project_id,
        AuthoringTransitionRequest(expected_revision=updated.revision),
        tenant_id="tenant-a",
        profile_id="institution-a",
        actor=actor,
    )
    return drafting


if __name__ == "__main__":
    unittest.main()
