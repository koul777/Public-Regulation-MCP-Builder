"""기관 하나를 규정 데이터까지 통째로 지운다.

기관 프로필만 지우면 그 기관의 규정·승인 기록은 그대로 남는다. 게다가 기관 ID는
기관명의 해시라, 같은 이름으로 다시 등록하면 ID가 똑같이 계산되어 지운 줄 알았던
규정이 전부 다시 붙는다. 운영자 눈에는 삭제가 되지 않은 것과 구별되지 않는다.

되돌릴 수 없는 삭제이므로 두 단계로 나눈다. ``plan``은 무엇이 지워지는지만 세어
돌려주고, ``purge``는 그 계획대로 지운다. 화면은 반드시 계획을 먼저 보여 준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import shutil

from app.core.config import Settings, get_settings
from app.core.tenant_access import (
    INSTITUTION_STORAGE_MARKER,
    institution_profile_id_from_storage_dir,
    institution_storage_dirname,
    tenant_directory_key,
    tenant_storage_key,
)
from app.retrieval.bm25_index import default_bm25_index_path, write_bm25_index
from app.services.document_service import DocumentService
from app.storage.file_store import FileStore
from app.storage.repository import JsonRepository


_DOCUMENT_ID_PATTERN = re.compile(r'"document_id"\s*:\s*"([^"]+)"')


@dataclass(frozen=True)
class InstitutionPurgePlan:
    """기관 하나를 지울 때 사라지는 것들. 화면에 그대로 보여 주기 위한 값이다."""

    profile_id: str
    document_ids: tuple[str, ...] = ()
    chunk_count: int = 0
    approved_chunk_count: int = 0
    indexed_record_count: int = 0
    export_file_count: int = 0
    source_file_count: int = 0
    document_names: tuple[str, ...] = ()
    # 아직 전처리하지 않은 대기 규정 파일과 저장해 둔 작업. 문서가 하나도 없는 기관에도
    # 이것들만 남아 있을 수 있어, 세지 않으면 '지울 것이 없다'고 잘못 말하게 된다.
    pending_file_count: int = 0
    saved_project_count: int = 0
    # 조항 수를 실제로 셌는지. 세지 않았으면 0을 '없음'으로 읽으면 안 된다.
    counted_chunks: bool = True

    @property
    def document_count(self) -> int:
        return len(self.document_ids)

    @property
    def is_empty(self) -> bool:
        return not (
            self.document_ids
            or self.export_file_count
            or self.pending_file_count
            or self.saved_project_count
        )


@dataclass
class InstitutionPurgeResult:
    profile_id: str
    deleted_document_count: int = 0
    deindexed_record_count: int = 0
    deleted_export_count: int = 0
    deleted_source_file_count: int = 0
    deleted_journal_records: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class InstitutionPurgeService:
    def __init__(
        self,
        settings: Settings | None = None,
        repository: JsonRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or JsonRepository(self.settings)
        self.file_store = file_store or FileStore(self.settings)
        self.documents = DocumentService(self.settings, self.repository, self.file_store)

    def documents_for_profile(self, profile_id: str, *, documents: list | None = None) -> list:
        normalized = str(profile_id or "").strip().lower()
        if not normalized:
            return []
        return [
            document
            for document in (self.repository.list_documents() if documents is None else documents)
            if str(getattr(document, "profile_id", "") or "").strip().lower() == normalized
        ]

    def plan_many(
        self,
        profile_ids: list[str],
        *,
        count_chunks: bool = True,
    ) -> list[InstitutionPurgePlan]:
        """여러 기관을 한 번에 센다. 문서 목록을 한 번만 읽으려고 따로 둔다."""
        documents = self.repository.list_documents()
        return [
            self.plan(profile_id, count_chunks=count_chunks, documents=documents)
            for profile_id in profile_ids
        ]

    def plan(
        self,
        profile_id: str,
        *,
        count_chunks: bool = True,
        documents: list | None = None,
    ) -> InstitutionPurgePlan:
        """무엇이 지워지는지 센다.

        조항 수는 저장된 조항 파일이 아니라 실행 기록(run stats)에서 읽는다. 규정
        100여 개의 조항 파일을 다 열면 10초가 넘어 화면이 멈추는데, 실행 기록에는
        같은 수가 이미 들어 있다. ``count_chunks``를 끄면 그것마저 건너뛴다.
        """
        documents = self.documents_for_profile(profile_id, documents=documents)
        document_ids = tuple(str(document.document_id) for document in documents)
        chunk_count = 0
        approved_chunk_count = 0
        if count_chunks and document_ids:
            chunk_count = self._chunk_count(document_ids)
            approved_chunk_count = self._approved_chunk_count(document_ids)
        return InstitutionPurgePlan(
            profile_id=str(profile_id or "").strip(),
            document_ids=document_ids,
            chunk_count=chunk_count,
            approved_chunk_count=approved_chunk_count,
            counted_chunks=count_chunks,
            indexed_record_count=(
                self._indexed_record_count(documents, document_ids) if count_chunks else 0
            ),
            export_file_count=len(self._export_paths(document_ids)),
            source_file_count=sum(
                1 for document in documents if self._source_path(document) is not None
            ),
            pending_file_count=self._directory_file_count(
                self._profile_directory("pending_uploads", profile_id)
            ),
            saved_project_count=self._directory_file_count(
                self._profile_directory("operator_projects", profile_id)
            ),
            document_names=tuple(
                str(getattr(document, "document_name", "") or getattr(document, "filename", ""))
                for document in documents
            ),
        )

    def purge(self, profile_id: str) -> InstitutionPurgeResult:
        documents = self.documents_for_profile(profile_id)
        result = InstitutionPurgeResult(profile_id=str(profile_id or "").strip())
        document_ids = [str(document.document_id) for document in documents]
        # 색인을 먼저 끊는다. 순서를 뒤집으면 원본이 사라진 상태에서 색인만 남아,
        # MCP가 근거를 확인할 수 없는 답변을 계속 내놓는다.
        #
        # 규정 하나씩 끊으면 그때마다 색인 파일 전체(수백 MB)와 BM25 색인을 다시 쓴다.
        # 규정 300개짜리 기관에서는 그것만으로 한 시간이 넘었다. 한 번에 끊는다.
        result.deindexed_record_count += self._deindex_documents(documents, result)
        for document in documents:
            document_id = str(document.document_id)
            source_path = self._source_path(document)
            if source_path is not None:
                try:
                    source_path.unlink()
                    result.deleted_source_file_count += 1
                except OSError as exc:
                    result.failures.append(f"{document_id}: 원본 파일 삭제 실패 ({exc})")
            if self.repository.delete_document(document_id):
                result.deleted_document_count += 1
            self._remove_vector_artifacts(document_id, result)
        for path in self._export_paths(document_ids):
            try:
                path.unlink()
                result.deleted_export_count += 1
            except OSError as exc:
                result.failures.append(f"내보내기 파일 삭제 실패: {path.name} ({exc})")
        result.deleted_journal_records = self.repository.purge_document_records(document_ids)
        self._remove_profile_directories(profile_id, result)
        return result

    def _chunk_count(self, document_ids: tuple[str, ...]) -> int:
        """조항 수. 실행 기록에 없는 문서만 저장된 조항 파일을 연다."""
        targets = set(document_ids)
        counted_by_document: dict[str, int] = {}
        for run in self.repository.list_runs():
            if run.document_id not in targets or run.status != "completed":
                continue
            stored = int((run.stats or {}).get("chunk_count") or 0)
            if stored:
                # list_runs()는 시작 시각 오름차순이라 마지막 실행이 남는다.
                counted_by_document[run.document_id] = stored
        total = sum(counted_by_document.values())
        for document_id in targets - set(counted_by_document):
            try:
                total += len(self.repository.get_chunk_records(document_id))
            except Exception:
                continue
        return total

    def _approved_chunk_count(self, document_ids: tuple[str, ...]) -> int:
        """승인된 조항 수. 승인 기록에 승인 시점 해시가 조항별로 남아 있다."""
        targets = set(document_ids)
        total = 0
        for record in self.repository.list_approval_records():
            if str(record.get("document_id") or "") not in targets:
                continue
            hashes = record.get("approved_content_hashes")
            if isinstance(hashes, dict):
                total += len(hashes)
        return total

    def _source_path(self, document) -> Path | None:
        try:
            path = self.documents.path_for(document)
        except Exception:
            return None
        return path if path.is_file() else None

    def _export_paths(self, document_ids: list[str] | tuple[str, ...]) -> list[Path]:
        export_root = Path(self.settings.data_dir) / "exports"
        if not export_root.is_dir():
            return []
        paths: list[Path] = []
        for document_id in document_ids:
            paths.extend(sorted(export_root.glob(f"{document_id}.*")))
        return paths

    def _vector_index_path(self, tenant_id: str | None) -> Path:
        return (
            Path(self.settings.data_dir)
            / "vector_db"
            / tenant_directory_key(tenant_id)
            / "approved_vectors.jsonl"
        )

    def _indexed_record_count(self, documents: list, document_ids: tuple[str, ...]) -> int:
        targets = set(document_ids)
        if not targets:
            return 0
        counted = 0
        for path in {
            self._vector_index_path(getattr(document, "tenant_id", None)) for document in documents
        }:
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    # 한 줄에 임베딩 벡터가 통째로 들어 있어 줄마다 JSON을 파싱하면
                    # 확인 창 하나에 10초가 넘게 걸린다. 문서 ID만 뽑아 본다.
                    match = _DOCUMENT_ID_PATTERN.search(line)
                    if match and match.group(1) in targets:
                        counted += 1
        return counted

    def _deindex_documents(self, documents: list, result: InstitutionPurgeResult) -> int:
        """이 기관 규정 전부를 색인에서 한 번에 걷어낸다."""
        removed = 0
        by_index_path: dict[Path, set[str]] = {}
        for document in documents:
            path = self._vector_index_path(getattr(document, "tenant_id", None))
            by_index_path.setdefault(path, set()).add(str(document.document_id))
        for path, document_ids in by_index_path.items():
            if not path.is_file():
                continue
            removed += self._deindex_documents_by_line(path, document_ids, result)
        return removed

    def _deindex_documents_by_line(
        self,
        path: Path,
        document_ids: set[str],
        result: InstitutionPurgeResult,
    ) -> int:
        kept: list[dict] = []
        removed = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    # 지울 줄인지 먼저 싸게 판단한다. 임베딩까지 들어 있는 줄을 전부
                    # 파싱하면 색인 하나 정리하는 데 수십 분이 걸린다.
                    match = _DOCUMENT_ID_PATTERN.search(stripped)
                    if match and match.group(1) in document_ids:
                        removed += 1
                        continue
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    metadata = record.get("metadata") if isinstance(record, dict) else {}
                    record_document_id = str(
                        (record.get("document_id") if isinstance(record, dict) else "")
                        or (metadata or {}).get("document_id")
                        or ""
                    )
                    if record_document_id in document_ids:
                        removed += 1
                        continue
                    kept.append(record)
            if removed:
                path.write_text(
                    "".join(
                        json.dumps(record, ensure_ascii=False) + "\n" for record in kept
                    ),
                    encoding="utf-8",
                )
                write_bm25_index(default_bm25_index_path(path), kept)
        except OSError as exc:
            result.failures.append(f"색인 해제 실패: {path.name} ({exc})")
            return 0
        return removed

    def _remove_vector_artifacts(self, document_id: str, result: InstitutionPurgeResult) -> None:
        artifact_dir = (
            Path(self.settings.data_dir) / "vector_ingestion" / tenant_storage_key(document_id)
        )
        if not artifact_dir.is_dir():
            return
        try:
            shutil.rmtree(artifact_dir)
        except OSError as exc:
            result.failures.append(f"{document_id}: 색인 산출물 삭제 실패 ({exc})")

    def _profile_directory(self, name: str, profile_id: str) -> Path:
        # 화면이 폴더를 만들 때 쓰는 이름과 같은 함수를 써야 한다. 여기서 이름을 따로
        # 계산하면 셀 때도 지울 때도 폴더를 못 찾아, '지울 것이 없다'고 말한 뒤 아무것도
        # 지우지 않는다.
        return Path(self.settings.data_dir) / name / institution_storage_dirname(profile_id)

    def _directory_file_count(self, directory: Path) -> int:
        if not directory.is_dir():
            return 0
        # 표식 파일은 운영자가 넣은 자료가 아니다. 세면 빈 폴더가 '파일 1개'로 보여
        # 지울 것이 남은 것처럼 읽힌다.
        return sum(
            1
            for path in directory.rglob("*")
            if path.is_file() and path.name != INSTITUTION_STORAGE_MARKER
        )

    def profile_ids_with_stored_data(self) -> set[str]:
        """규정 데이터가 남아 있는 기관 ID.

        문서가 하나도 없어도 대기 중인 규정 파일이나 저장한 작업만 남아 있을 수 있다.
        문서만 보고 판단하면 그 기관은 화면 어디에도 나타나지 않아, 같은 이름으로 다시
        등록할 때 되살아난다.

        폴더 이름은 기관 ID의 해시라 이름을 그대로 기관 ID로 쓰면 안 된다. 그렇게 하면
        멀쩡히 등록된 기관이 '주인 없는 데이터'로 표시되고, 그 화면에서 지우면 살아 있는
        기관의 대기 파일이 날아간다. 폴더 안에 적어 둔 표식만 믿는다. 표식이 없는 예전
        폴더는 어느 기관 것인지 알 수 없으므로 보고하지 않는다.
        """
        found = {
            str(getattr(document, "profile_id", "") or "").strip().lower()
            for document in self.repository.list_documents()
        }
        for name in ("pending_uploads", "operator_projects"):
            root = Path(self.settings.data_dir) / name
            if not root.is_dir():
                continue
            for directory in root.iterdir():
                if not directory.is_dir():
                    continue
                if not self._directory_file_count(directory):
                    continue
                found.add(institution_profile_id_from_storage_dir(directory))
        found.discard("")
        return found

    def _remove_profile_directories(self, profile_id: str, result: InstitutionPurgeResult) -> None:
        """이 기관에만 속한 보조 폴더(대기 업로드, 저장한 작업)를 함께 지운다."""
        for directory in (
            self._profile_directory("operator_projects", profile_id),
            self._profile_directory("pending_uploads", profile_id),
        ):
            if not directory.is_dir():
                continue
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                result.failures.append(f"{directory.name} 폴더 삭제 실패 ({exc})")
