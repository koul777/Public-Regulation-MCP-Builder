"""이미 전처리를 마친 규정에 재사용 가능한 AI 검수 결과를 되살린다.

같은 규정을 다시 올리면 내용 해시가 그대로라 AI 검수 호출을 건너뛴다. 예전 코드는
호출만 건너뛰고 이전 결과를 새 문서로 옮기지 않아, 검수를 켜고 전처리했는데도 승인
화면의 조항마다 'AI 검수 의견 없음'만 남았다. 코드는 고쳤지만 그 사이에 만들어진
문서는 이미 저장되어 있으므로, 다시 전처리하지 않고 여기서 결과만 채워 넣는다.

승인·색인이 끝난 조항은 건드리지 않는다. 저장된 조항의 메타데이터는 승인 시점
내용 해시에 함께 들어가므로, 지금 고치면 색인된 근거와 화면이 갈라진다.

    python -m scripts.backfill_agent_review_findings --dry-run
    python -m scripts.backfill_agent_review_findings --document doc_xxxxxxxx
"""

from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.schemas.chunk import ChunkOptions
from app.services.processing_service import (
    AGENT_REVIEW_FINDINGS_KEY,
    ProcessingService,
)
from app.storage.repository import JsonRepository


def _pending(chunk) -> bool:
    return str(getattr(chunk, "approval_status", "") or "") != "approved" and not str(
        getattr(chunk, "approved_content_hash", "") or ""
    ).strip()


def backfill(document_ids: list[str] | None = None, *, dry_run: bool = False) -> int:
    settings = get_settings()
    repository = JsonRepository(settings)
    service = ProcessingService(settings=settings, repository=repository)
    cache_scope_hash = service.agent_review_policy.cache_scope_hash()
    options = ChunkOptions(enable_agent_review=True)
    cache_index_by_tenant: dict[str, dict[str, tuple[str, str]]] = {}
    repaired_documents = 0

    for run in repository.list_runs():
        if run.status != "completed":
            continue
        if document_ids and run.document_id not in document_ids:
            continue
        agent_review = (run.stats or {}).get("agent_review") or {}
        if str(agent_review.get("skip_reason") or "") != "review_candidates_cached":
            continue
        if int(agent_review.get("reused_chunk_count") or 0):
            continue  # 고친 코드로 실행된 문서라 이미 결과가 들어 있다.

        chunks = repository.get_chunks(run.document_id)
        if not chunks or any(
            (chunk.metadata or {}).get(AGENT_REVIEW_FINDINGS_KEY) for chunk in chunks
        ):
            continue
        quality_report = repository.get_quality_report(run.document_id)
        if quality_report is None:
            print(f"{run.document_id}: 품질 보고서가 없어 건너뜁니다.")
            continue

        tenant_key = str(run.tenant_id or "")
        if tenant_key not in cache_index_by_tenant:
            cache_index_by_tenant[tenant_key] = service._agent_review_cache_index(
                run.tenant_id,
                cache_scope_hash=cache_scope_hash,
            )
        cache_index = cache_index_by_tenant[tenant_key]

        plan = service.agent_review_policy.plan(
            chunks,
            quality_report,
            options,
            cached_content_hashes=set(cache_index),
        )
        reused = service._reuse_cached_review_findings(chunks, plan, cache_index)
        restored = [item for item in reused if item["has_findings"]]
        if not restored:
            continue

        blocked = [
            item["chunk_id"]
            for item in restored
            if not _pending(next(c for c in chunks if c.chunk_id == item["chunk_id"]))
        ]
        if blocked:
            print(
                f"{run.document_id}: 이미 승인된 조항 {len(blocked):,}개가 있어 건너뜁니다"
                " (다시 전처리해야 반영됩니다)."
            )
            continue

        print(
            f"{run.document_id}: 조항 {len(restored):,}개에 이전 AI 검수 의견을 되살립니다"
            f" (재사용 대상 {len(reused):,}개)."
        )
        if not dry_run:
            repository.save_chunks(run.document_id, chunks)
        repaired_documents += 1

    print(
        ("[미리보기] " if dry_run else "")
        + f"규정 {repaired_documents:,}개를 처리했습니다."
    )
    return repaired_documents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", action="append", dest="documents", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(args.documents, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
