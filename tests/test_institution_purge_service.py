from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.tenant_access import (
    institution_storage_dir,
    settings_for_tenant,
    tenant_storage_key,
)
from app.retrieval.bm25_index import (
    default_bm25_index_path,
    load_bm25_index,
    write_bm25_index,
)
from app.schemas.authoring import AuthoringProjectCreateRequest
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.run import ProcessingRun
from app.services.authoring_service import AuthoringService
from app.services.institution_purge_service import InstitutionPurgeService
from app.storage.authoring_repository import AuthoringRepository
from app.storage.repository import JsonRepository


class InstitutionPurgeServiceTests(unittest.TestCase):
    """기관을 지울 때 그 기관 규정이 어디에도 남지 않는지 고정한다.

    프로필만 지우면 규정·승인 기록이 남고, 기관 ID가 기관명 해시라 같은 이름으로
    다시 등록하는 순간 전부 되살아났다. 운영자에게는 삭제가 안 된 것으로 보였다.
    """

    def _seed(self, root: Path, *, profile_id: str, document_id: str) -> tuple[Settings, JsonRepository]:
        settings = Settings(data_dir=root / "data")
        repository = JsonRepository(settings)
        repository.upsert_document(
            Document(
                document_id=document_id,
                filename=f"{document_id}.hwp",
                document_name="인사규정",
                file_type="hwp",
                file_hash=f"hash-{document_id}",
                tenant_id="default",
                profile_id=profile_id,
                status="completed",
            )
        )
        repository.save_chunks(
            document_id,
            [
                Chunk(
                    chunk_id=f"{document_id}_chunk_1",
                    document_id=document_id,
                    chunk_type="article",
                    text="제1조(목적) 본문",
                    approval_status="approved",
                )
            ],
        )
        repository.upsert_run(
            ProcessingRun(
                run_id=f"run_{document_id}",
                document_id=document_id,
                job_id=f"job_{document_id}",
                tenant_id="default",
                status="completed",
                started_at=datetime.now(timezone.utc),
                elapsed_seconds=1.0,
            )
        )
        repository.append_approval_record(
            {
                "approval_id": f"approval_{document_id}",
                "document_id": document_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
                # 승인 기록에는 승인 시점 조항별 해시가 함께 남는다. 계획 화면은
                # 조항 파일을 열지 않고 이 기록으로 승인 조항 수를 센다.
                "approved_content_hashes": {f"{document_id}_chunk_1": "sha256:" + "a" * 64},
            }
        )
        export_dir = settings.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / f"{document_id}.jsonl").write_text("{}\n", encoding="utf-8")
        vector_dir = settings.data_dir / "vector_db" / "default"
        vector_dir.mkdir(parents=True, exist_ok=True)
        (vector_dir / "approved_vectors.jsonl").write_text(
            json.dumps(
                {
                    "id": f"{document_id}_chunk_1",
                    "document_id": document_id,
                    "text": "제1조(목적) 본문",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return settings, repository

    def test_purge_removes_documents_records_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, repository = self._seed(
                root, profile_id="institution-abc", document_id="doc_purge_me"
            )
            service = InstitutionPurgeService(settings=settings, repository=repository)

            plan = service.plan("institution-abc")
            self.assertEqual(1, plan.document_count)
            self.assertEqual(1, plan.approved_chunk_count)
            self.assertEqual(1, plan.export_file_count)

            result = service.purge("institution-abc")

            self.assertEqual(1, result.deleted_document_count)
            self.assertEqual(1, result.deleted_export_count)
            self.assertEqual([], [document.document_id for document in repository.list_documents()])
            self.assertEqual([], repository.list_runs())
            self.assertEqual([], repository.list_approval_records())
            self.assertFalse((settings.data_dir / "exports" / "doc_purge_me.jsonl").exists())
            self.assertEqual(
                "",
                (settings.data_dir / "vector_db" / "default" / "approved_vectors.jsonl")
                .read_text(encoding="utf-8")
                .strip(),
            )

    def test_purge_removes_schema_valid_index_records(self) -> None:
        """실제 색인 형식(검증을 통과하는 레코드)도 함께 빠져야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, repository = self._seed(
                root, profile_id="institution-abc", document_id="doc_purge_me"
            )
            vector_path = settings.data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            approved_record = {
                "schema_version": "reg-rag-vector-record-v1",
                "id": "doc_purge_me_chunk_1",
                "document_id": "doc_purge_me",
                "chunk_id": "doc_purge_me_chunk_1",
                "text": "제1조(목적) 본문",
                "metadata": {
                    "document_id": "doc_purge_me",
                    "approval_status": "approved",
                    "approval_id": "approval_doc_purge_me",
                    "approved_content_hash": "sha256:" + "a" * 64,
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                    "approved_by": "operator",
                    "tenant_id": "default",
                },
            }
            vector_path.write_text(
                json.dumps(approved_record, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            result = InstitutionPurgeService(settings=settings, repository=repository).purge(
                "institution-abc"
            )

            self.assertEqual(1, result.deindexed_record_count)
            self.assertEqual("", vector_path.read_text(encoding="utf-8").strip())

    def test_bm25_write_failure_converges_safely_on_retry(self) -> None:
        """A retry must repair stale BM25 before deleting the source document."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, repository = self._seed(
                root, profile_id="institution-abc", document_id="doc_purge_me"
            )
            service = InstitutionPurgeService(settings=settings, repository=repository)
            vector_path = settings.data_dir / "vector_db" / "default" / "approved_vectors.jsonl"
            records = [
                json.loads(line)
                for line in vector_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            bm25_path = default_bm25_index_path(vector_path)
            write_bm25_index(bm25_path, records)
            real_write = write_bm25_index
            call_count = 0

            def flaky_write(path: Path, current_records) -> object:
                nonlocal call_count
                call_count += 1
                materialized = list(current_records)
                if call_count == 1:
                    raise OSError("simulated BM25 write failure")
                return real_write(path, materialized)

            with patch(
                "app.services.institution_purge_service.write_bm25_index",
                side_effect=flaky_write,
            ):
                first = service.purge("institution-abc")
                self.assertFalse(first.completed)
                self.assertIsNotNone(repository.get_document("doc_purge_me"))
                self.assertEqual("", vector_path.read_text(encoding="utf-8").strip())
                self.assertEqual(1, len(load_bm25_index(bm25_path).documents))

                second = service.purge("institution-abc")

            repaired = load_bm25_index(bm25_path)
            self.assertTrue(second.completed)
            self.assertIsNone(repository.get_document("doc_purge_me"))
            self.assertIsNotNone(repaired)
            self.assertEqual([], repaired.documents)

    def test_vector_artifact_failure_keeps_document_discoverable_for_retry(self) -> None:
        """Post-index cleanup failure must not erase the durable retry target."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, repository = self._seed(
                root, profile_id="institution-abc", document_id="doc_purge_me"
            )
            service = InstitutionPurgeService(settings=settings, repository=repository)
            artifact_dir = (
                settings.data_dir
                / "vector_ingestion"
                / tenant_storage_key("doc_purge_me")
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "receipt.json").write_text("{}", encoding="utf-8")
            real_remove = service._remove_vector_artifacts
            call_count = 0

            def flaky_remove(document_id: str, result) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    result.failures.append("simulated vector artifact failure")
                    return
                real_remove(document_id, result)

            with patch.object(
                service,
                "_remove_vector_artifacts",
                side_effect=flaky_remove,
            ):
                first = service.purge("institution-abc")
                self.assertFalse(first.completed)
                self.assertIsNotNone(repository.get_document("doc_purge_me"))
                self.assertTrue(artifact_dir.is_dir())

                second = service.purge("institution-abc")

            self.assertTrue(second.completed)
            self.assertIsNone(repository.get_document("doc_purge_me"))
            self.assertFalse(artifact_dir.exists())

    def test_purge_leaves_other_institutions_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, repository = self._seed(
                root, profile_id="institution-abc", document_id="doc_purge_me"
            )
            repository.upsert_document(
                Document(
                    document_id="doc_keep_me",
                    filename="keep.hwp",
                    document_name="보수규정",
                    file_type="hwp",
                    file_hash="hash-keep",
                    tenant_id="default",
                    profile_id="institution-other",
                    status="completed",
                )
            )
            repository.upsert_run(
                ProcessingRun(
                    run_id="run_keep",
                    document_id="doc_keep_me",
                    job_id="job_keep",
                    tenant_id="default",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    elapsed_seconds=1.0,
                )
            )
            service = InstitutionPurgeService(settings=settings, repository=repository)

            service.purge("institution-abc")

            self.assertEqual(
                ["doc_keep_me"], [document.document_id for document in repository.list_documents()]
            )
            self.assertEqual(["run_keep"], [run.run_id for run in repository.list_runs()])

    def test_pending_uploads_alone_still_count_as_stored_data(self) -> None:
        """문서가 없어도 대기 중인 규정 파일만 남아 있을 수 있다.

        문서만 보고 판단하면 그 기관은 화면 어디에도 나타나지 않다가, 같은 이름으로
        다시 등록하는 순간 되살아난다. 실제로 이 상태로 규정 파일 171개가 남아 있었다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            repository = JsonRepository(settings)
            # 화면이 폴더를 만들 때 쓰는 함수를 그대로 쓴다. 여기서 폴더 이름을 손으로
            # 적으면, 지우는 쪽이 이름을 잘못 알고 있어도 시험은 통과한다.
            pending_dir = institution_storage_dir(
                settings.data_dir / "pending_uploads", "institution-abc", create=True
            )
            (pending_dir / "인사규정.hwp").write_bytes(b"hwp")
            service = InstitutionPurgeService(settings=settings, repository=repository)

            self.assertEqual({"institution-abc"}, service.profile_ids_with_stored_data())
            plan = service.plan("institution-abc")
            self.assertEqual(0, plan.document_count)
            self.assertEqual(1, plan.pending_file_count)
            self.assertFalse(plan.is_empty)

            service.purge("institution-abc")

            self.assertFalse(pending_dir.exists())
            self.assertEqual(set(), service.profile_ids_with_stored_data())

    def test_stored_data_is_reported_by_profile_id_not_folder_name(self) -> None:
        """폴더 이름을 기관 ID로 착각하면 살아 있는 기관 파일을 지운다.

        폴더 이름은 기관 ID의 해시다. 그것을 기관 ID로 돌려주면 등록된 기관과 한 번도
        일치하지 않아, 멀쩡한 기관이 '주인 없는 데이터'로 표시된다. 그 화면에서 지우면
        지금 쓰고 있는 기관의 대기 파일이 날아간다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            repository = JsonRepository(settings)
            pending_dir = institution_storage_dir(
                settings.data_dir / "pending_uploads", "institution-abc", create=True
            )
            (pending_dir / "인사규정.hwp").write_bytes(b"hwp")
            service = InstitutionPurgeService(settings=settings, repository=repository)

            reported = service.profile_ids_with_stored_data()

            self.assertEqual({"institution-abc"}, reported)
            self.assertNotIn(pending_dir.name, reported)
            # 등록된 기관을 빼고 나면 주인 없는 데이터는 없어야 한다.
            self.assertEqual(set(), reported - {"institution-abc"})

    def test_marker_only_folder_is_not_reported_as_leftover_data(self) -> None:
        """표식만 있는 빈 폴더는 지울 것이 남은 것으로 세지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            repository = JsonRepository(settings)
            institution_storage_dir(
                settings.data_dir / "pending_uploads", "institution-abc", create=True
            )
            service = InstitutionPurgeService(settings=settings, repository=repository)

            self.assertEqual(set(), service.profile_ids_with_stored_data())
            self.assertEqual(0, service.plan("institution-abc").pending_file_count)
            self.assertTrue(service.plan("institution-abc").is_empty)

    def test_purged_records_do_not_return_after_reload(self) -> None:
        """저널을 다시 읽어도 지운 기록이 되살아나지 않아야 한다.

        저널은 평소 덧붙이기만 하고 캐시로 읽는다. 캐시나 압축 구간을 남겨 두면
        같은 이름으로 기관을 다시 만드는 순간 지운 기록이 그대로 돌아온다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, repository = self._seed(
                root, profile_id="institution-abc", document_id="doc_purge_me"
            )
            InstitutionPurgeService(settings=settings, repository=repository).purge(
                "institution-abc"
            )

            reopened = JsonRepository(settings)

            self.assertEqual([], reopened.list_runs())
            self.assertEqual([], reopened.list_approval_records())
            self.assertEqual([], reopened.list_documents())

    def test_plan_and_purge_include_authoring_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            repository = JsonRepository(settings)
            project = AuthoringService(settings).create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-abc",
                    title="인사 규정",
                ),
                tenant_id="default",
                actor="author",
            )
            authoring_export = (
                settings.authoring_dir
                / "exports"
                / str(project.project_id)
                / "00000000000000000001"
                / "draft.md"
            )
            authoring_export.parent.mkdir(parents=True)
            authoring_export.write_text("draft", encoding="utf-8")
            service = InstitutionPurgeService(settings=settings, repository=repository)

            plan = service.plan("institution-abc")
            result = service.purge("institution-abc")

            self.assertEqual(1, plan.authoring_project_count)
            self.assertFalse(plan.is_empty)
            self.assertTrue(result.completed)
            self.assertEqual(1, result.requested_authoring_project_count)
            self.assertEqual(1, result.deleted_authoring_project_count)
            self.assertFalse(authoring_export.exists())
            self.assertNotIn(
                "institution-abc",
                service.profile_ids_with_stored_data(),
            )

    def test_authoring_purge_preserves_other_profile_and_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            authoring = AuthoringService(settings)
            target = authoring.create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-abc",
                    title="삭제 대상",
                ),
                tenant_id="tenant-a",
                actor="author",
            )
            other_profile = authoring.create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-other",
                    title="다른 기관",
                ),
                tenant_id="tenant-a",
                actor="author",
            )
            other_tenant = authoring.create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-abc",
                    title="다른 테넌트",
                ),
                tenant_id="tenant-b",
                actor="author",
            )
            service = InstitutionPurgeService(settings=settings, repository=repository)

            result = service.purge("institution-abc", tenant_id="tenant-a")

            self.assertTrue(result.completed)
            authoring_repository = AuthoringRepository(settings)
            with self.assertRaises(KeyError):
                authoring_repository.get_project(
                    str(target.project_id), tenant_id="tenant-a"
                )
            self.assertEqual(
                other_profile.project_id,
                authoring_repository.get_project(
                    str(other_profile.project_id), tenant_id="tenant-a"
                ).project_id,
            )
            self.assertEqual(
                other_tenant.project_id,
                authoring_repository.get_project(
                    str(other_tenant.project_id), tenant_id="tenant-b"
                ).project_id,
            )

    def test_authoring_cleanup_failure_keeps_profile_data_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            AuthoringService(settings).create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-abc",
                    title="삭제 실패 초안",
                ),
                tenant_id="default",
                actor="author",
            )
            pending_dir = institution_storage_dir(
                settings.data_dir / "pending_uploads",
                "institution-abc",
                create=True,
            )
            pending_file = pending_dir / "pending.hwp"
            pending_file.write_bytes(b"hwp")
            authoring_repository = AuthoringRepository(settings)
            service = InstitutionPurgeService(
                settings=settings,
                repository=repository,
                authoring_repository=authoring_repository,
            )

            with patch.object(
                authoring_repository,
                "_remove_project_directory",
                side_effect=OSError("simulated authoring cleanup failure"),
            ):
                first = service.purge("institution-abc")

            self.assertFalse(first.completed)
            self.assertTrue(first.failures)
            self.assertTrue(pending_file.is_file())
            self.assertEqual(1, service.plan("institution-abc").authoring_project_count)

            second = service.purge("institution-abc")
            self.assertTrue(second.completed)
            self.assertFalse(pending_dir.exists())

    def test_optional_tenant_uses_validated_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                api_default_tenant_id=" Tenant-A ",
            )
            service = InstitutionPurgeService(
                settings=settings,
                repository=JsonRepository(settings),
            )

            with self.assertRaises(ValueError):
                service.plan("institution-abc")

    def test_tenant_isolated_authoring_root_is_planned_and_purged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                tenant_storage_isolation=True,
            )
            tenant_settings = settings_for_tenant(settings, "tenant-a")
            project = AuthoringService(tenant_settings).create_project(
                AuthoringProjectCreateRequest(
                    profile_id="institution-abc",
                    title="테넌트 분리 초안",
                ),
                tenant_id="tenant-a",
                actor="author",
            )
            service = InstitutionPurgeService(
                settings=settings,
                repository=JsonRepository(settings),
            )

            plan = service.plan("institution-abc", tenant_id="tenant-a")
            result = service.purge("institution-abc", tenant_id="tenant-a")

            self.assertEqual(1, plan.authoring_project_count)
            self.assertTrue(result.completed)
            self.assertFalse(
                (
                    tenant_settings.authoring_dir
                    / "projects"
                    / f"{project.project_id}.json"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()
