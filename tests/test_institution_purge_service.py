from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.core.tenant_access import institution_storage_dir
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.run import ProcessingRun
from app.services.institution_purge_service import InstitutionPurgeService
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


if __name__ == "__main__":
    unittest.main()
