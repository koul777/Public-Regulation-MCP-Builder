from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.mcp_server.regulation_tools import fetch_regulation, mcp_auth_context, search_regulations
from app.processors.answer_profile import append_answer_profile_to_retrieval_text, build_answer_profile
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.mcp_publish_approval_evidence import build_publish_approval_evidence


DEFAULT_TENANT_ID = "tenant-regulation-publish"
DEFAULT_SECURITY_LEVEL = "internal"
DEFAULT_SOURCE_SYSTEM = "PREPROCESSED_LOCAL"
DEFAULT_SOURCE_URL = "local-preprocessed-regulation-corpus"
DEFAULT_EMBEDDING_DIMENSIONS = 384
ARTICLE_RE = re.compile(r"(제\s*\d+\s*조(?:의\s*\d+)?)\s*\(([^)]+)\)")


def prepare_mcp_publish_runtime(
    *,
    source_data_dir: Path,
    target_data_dir: Path,
    source_document_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    security_level: str = DEFAULT_SECURITY_LEVEL,
    profile_id: str | None = None,
    institution_name: str | None = None,
    document_name: str | None = None,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    include_all_chunks: bool = True,
    selection_keywords: list[str] | None = None,
    smoke_queries: list[str] | None = None,
    operator_approval_reference: str | None = None,
    operator_reviewer_id: str | None = None,
    out_json: Path | None = None,
) -> dict[str, Any]:
    source_repository = JsonRepository(Settings(data_dir=source_data_dir))
    source_document = source_repository.get_document(source_document_id)
    if source_document is None:
        raise ValueError(f"Source document not found: {source_document_id}")
    effective_profile_id = str(profile_id or source_document.profile_id or "").strip()
    if not effective_profile_id:
        raise ValueError(
            "A concrete profile_id is required for official MCP publish; "
            "provide --profile-id or assign profile_id to the source document."
        )
    raw_chunks = _load_raw_chunks(source_data_dir, source_document_id)
    selected_chunks = [
        _prepare_chunk(
            raw,
            tenant_id=tenant_id,
            security_level=security_level,
            institution_name=institution_name or source_document.institution_name or "",
            document_name=document_name or source_document.document_name or source_document.filename,
            source_system=source_system,
            source_url=source_document.source_url or DEFAULT_SOURCE_URL,
            profile_id=effective_profile_id,
        )
        for raw in raw_chunks
        if include_all_chunks or _is_selected_chunk(raw, selection_keywords or [])
    ]
    if not selected_chunks:
        raise ValueError("No chunks selected for MCP publish runtime.")

    base_settings = Settings(data_dir=target_data_dir, artifact_root=target_data_dir, tenant_storage_isolation=True)
    target_settings = settings_for_tenant(base_settings, tenant_id)
    reset_performed = reset_target_tenant_runtime(target_data_dir=target_data_dir, tenant_data_dir=target_settings.data_dir)
    repository = JsonRepository(target_settings)
    document = _prepare_document(
        source_document,
        tenant_id=tenant_id,
        institution_name=institution_name,
        document_name=document_name,
        source_system=source_system,
        profile_id=effective_profile_id,
    )
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], selected_chunks, [])

    auth = AuthContext(actor="mcp-runtime-publisher", tenant_id=tenant_id, auth_mode="script", role="admin")
    chunk_ids = [chunk.chunk_id for chunk in selected_chunks]
    approval_evidence = build_publish_approval_evidence(
        data_dir=target_data_dir,
        artifact_root=base_settings.artifact_root,
        tenant_id=tenant_id,
        tenant_storage_isolation=True,
        document_id=document.document_id,
        chunk_ids=chunk_ids,
        security_level=security_level,
        artifact_prefix="mcp_publish",
        operator_approval_reference=operator_approval_reference,
        operator_reviewer_id=operator_reviewer_id,
    )
    approval_records: list[dict[str, Any]] = []
    with patch.object(routes_documents, "get_settings", return_value=base_settings):
        approval_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        for index, approval_request in enumerate(approval_evidence["approval_requests"], start=1):
            request_payload = dict(approval_request)
            request_payload.update(
                {
                    "approval_id": f"approval-mcp-publish-{approval_stamp}-{index:03d}",
                    "security_level": security_level,
                    "note": (
                        "Preprocessed regulation chunks approved for local MCP publish runtime "
                        f"after human review reference={approval_evidence['operator_approval_reference']} "
                        f"reviewer={approval_evidence['operator_reviewer_id']}."
                    ),
                }
            )
            approval_records.append(
                routes_documents.approve_review_chunks(
                    document.document_id,
                    routes_documents.ApprovalRequest(**request_payload),
                    auth,
                )
            )
        index_job = routes_documents.index_document(
            document.document_id,
            routes_documents.IndexRequest(
                target_type="local-jsonl",
                embedding_dimensions=DEFAULT_EMBEDDING_DIMENSIONS,
            ),
            auth,
        )

    smoke = _run_mcp_smoke_queries(
        target_settings,
        tenant_id=tenant_id,
        security_level=security_level,
        smoke_queries=smoke_queries or [],
    )
    report = {
        "report_type": "mcp_publish_runtime",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_data_dir": str(source_data_dir),
        "target_data_dir": str(target_data_dir),
        "tenant_data_dir": str(target_settings.data_dir),
        "tenant_id": tenant_id,
        "profile_id": effective_profile_id,
        "synthetic_runtime": False,
        "provenance": "approved_publish_runtime",
        "source_document_id": source_document_id,
        "document_id": document.document_id,
        "institution_name": document.institution_name,
        "document_name": document.document_name,
        "source_chunk_count": len(raw_chunks),
        "selected_chunk_count": len(selected_chunks),
        "approved_chunk_count": sum(len(record.get("chunk_ids", [])) for record in approval_records),
        "approval_record_count": len(approval_records),
        "approval_evidence": {
            key: value
            for key, value in approval_evidence.items()
            if key not in {"approval_requests"}
        },
        "indexed_record_count": int(index_job.get("record_count") or 0),
        "index_status": index_job.get("status"),
        "reset_performed": reset_performed,
        "vector_artifacts": index_job.get("artifacts", {}),
        "mcp_smoke": smoke,
        "passed": bool(
            selected_chunks
            and int(index_job.get("record_count") or 0) == len(selected_chunks)
            and all(item.get("result_count", 0) > 0 and item.get("fetch_has_text") for item in smoke)
        ),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def reset_target_tenant_runtime(*, target_data_dir: Path, tenant_data_dir: Path) -> bool:
    target_root = target_data_dir.resolve()
    tenant_root = tenant_data_dir.resolve()
    tenants_root = (target_root / "tenants").resolve()
    if tenants_root not in tenant_root.parents:
        raise ValueError(f"Refusing to reset tenant runtime outside target tenants directory: {tenant_root}")
    if tenant_root == tenants_root or tenant_root == target_root:
        raise ValueError(f"Refusing to reset unsafe tenant runtime path: {tenant_root}")
    if not tenant_root.exists():
        return False
    shutil.rmtree(tenant_root)
    return True


def _load_raw_chunks(source_data_dir: Path, document_id: str) -> list[dict[str, Any]]:
    path = source_data_dir / "repository" / f"{document_id}_chunks.json"
    if not path.is_file():
        raise ValueError(f"Source chunks not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError(f"Source chunks must be a JSON array: {path}")
    return [item for item in payload if isinstance(item, dict)]


def _is_selected_chunk(raw: dict[str, Any], selection_keywords: list[str]) -> bool:
    if not selection_keywords:
        return False
    blob = _chunk_blob(raw)
    return any(keyword and keyword in blob for keyword in selection_keywords)


def _chunk_blob(raw: dict[str, Any]) -> str:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return "\n".join(
        [
            str(raw.get("retrieval_text") or ""),
            str(raw.get("normalized_text") or ""),
            str(raw.get("text") or ""),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ]
    )


def _prepare_document(
    source_document: Document,
    *,
    tenant_id: str,
    institution_name: str | None,
    document_name: str | None,
    source_system: str,
    profile_id: str,
) -> Document:
    return source_document.model_copy(
        update={
            "document_name": document_name or source_document.document_name or source_document.filename,
            "institution_name": institution_name or source_document.institution_name,
            "source_system": source_system or source_document.source_system or DEFAULT_SOURCE_SYSTEM,
            "source_url": source_document.source_url or DEFAULT_SOURCE_URL,
            "profile_id": profile_id,
            "tenant_id": tenant_id,
        }
    )


def _prepare_chunk(
    raw: dict[str, Any],
    *,
    tenant_id: str,
    security_level: str,
    institution_name: str,
    document_name: str,
    source_system: str,
    source_url: str,
    profile_id: str,
) -> Chunk:
    raw_copy = dict(raw)
    raw_copy["approval_status"] = "draft"
    raw_copy["approval_id"] = None
    raw_copy["approved_by"] = None
    raw_copy["approved_at"] = None
    raw_copy["approved_content_hash"] = None
    raw_copy["security_level"] = security_level
    raw_copy["department_acl"] = []
    metadata = dict(raw_copy.get("metadata") or {})
    blob = _chunk_blob(raw_copy)
    metadata["tenant_id"] = tenant_id
    if institution_name:
        metadata["institution_name"] = institution_name
    if document_name:
        metadata["document_name"] = document_name
    metadata["source_system"] = metadata.get("source_system") or source_system
    metadata["source_url"] = metadata.get("source_url") or source_url
    raw_profile_id = str(metadata.get("profile_id") or "").strip()
    if raw_profile_id and raw_profile_id.casefold() != str(profile_id).strip().casefold():
        raise ValueError(
            f"Chunk profile_id '{raw_profile_id}' conflicts with publish profile_id '{profile_id}'."
        )
    metadata["profile_id"] = profile_id
    article_no, article_title = _infer_article(blob, metadata)
    if article_no and not metadata.get("article_no"):
        metadata["article_no"] = article_no
    if article_title and not metadata.get("article_title"):
        metadata["article_title"] = article_title
    answer_text = str(raw_copy.get("normalized_text") or raw_copy.get("text") or raw_copy.get("retrieval_text") or "")
    answer_profile = build_answer_profile(answer_text, metadata)
    metadata.update(answer_profile)
    if answer_profile and raw_copy.get("retrieval_text"):
        raw_copy["retrieval_text"] = append_answer_profile_to_retrieval_text(
            str(raw_copy.get("retrieval_text") or ""),
            answer_profile,
        )
    raw_copy["metadata"] = metadata
    return Chunk.model_validate(raw_copy)


def _infer_article(blob: str, metadata: dict[str, Any]) -> tuple[str, str]:
    article_no = str(metadata.get("article_no") or "").strip()
    article_title = str(metadata.get("article_title") or "").strip()
    if article_no and article_title:
        return article_no, article_title
    lifecycle_title = _infer_article_lifecycle_title(blob, article_no=article_no)
    if lifecycle_title:
        inferred_no, inferred_title = lifecycle_title
        return article_no or inferred_no, article_title or inferred_title
    match = ARTICLE_RE.search(blob)
    if not match:
        return article_no, article_title
    return article_no or re.sub(r"\s+", "", match.group(1)), article_title or match.group(2).strip()


def _infer_article_lifecycle_title(blob: str, *, article_no: str = "") -> tuple[str, str] | None:
    if article_no:
        match = re.search(rf"{re.escape(article_no)}\s*<?\s*(삭제|삭\s*제|생략)", blob)
        if match:
            return article_no, _normalize_article_lifecycle_title(match.group(1))
    match = re.search(r"(제\s*\d+\s*조(?:의\s*\d+)?)\s*<?\s*(삭제|삭\s*제|생략)", blob)
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)), _normalize_article_lifecycle_title(match.group(2))


def _normalize_article_lifecycle_title(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    if compact == "생략":
        return "생략"
    return "삭제"


def _run_mcp_smoke_queries(
    settings: Settings,
    *,
    tenant_id: str,
    security_level: str,
    smoke_queries: list[str],
) -> list[dict[str, Any]]:
    auth = mcp_auth_context(tenant_id=tenant_id, actor="mcp-runtime-smoke", role="operator")
    results: list[dict[str, Any]] = []
    for query in smoke_queries:
        search = search_regulations(
            settings=settings,
            auth=auth,
            query=query,
            top_k=5,
            security_levels=[security_level],
        )
        first = search["results"][0] if search["results"] else None
        fetched = (
            fetch_regulation(
                settings=settings,
                auth=auth,
                result_id=str(first.get("id") or ""),
                security_levels=[security_level],
            )
            if first
            else {}
        )
        results.append(
            {
                "query": query,
                "result_count": len(search["results"]),
                "top_result": _compact_result(first),
                "fetch_has_text": bool(fetched.get("text")),
                "fetch_text_preview": " ".join(str(fetched.get("text") or "").split())[:260],
            }
        )
    return results


def _compact_result(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    return {
        "title": result.get("title") or "",
        "document_id": result.get("document_id") or "",
        "chunk_id": result.get("chunk_id") or "",
        "score": result.get("score"),
        "url": result.get("url") or "",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a preprocessed regulation corpus as a local MCP runtime.")
    parser.add_argument("--source-data-dir", type=Path, default=Path("data"))
    parser.add_argument("--target-data-dir", type=Path, required=True)
    parser.add_argument("--source-document-id", required=True)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--security-level", default=DEFAULT_SECURITY_LEVEL)
    parser.add_argument(
        "--profile-id",
        default=None,
        help="Concrete institution profile id; required when the source document has no profile_id.",
    )
    parser.add_argument("--institution-name", default=None)
    parser.add_argument("--document-name", default=None)
    parser.add_argument("--source-system", default=DEFAULT_SOURCE_SYSTEM)
    parser.set_defaults(include_all_chunks=True)
    parser.add_argument(
        "--include-all-chunks",
        action="store_true",
        help="Use the full corpus when packaging an operator-confirmed publish runtime. Default.",
    )
    parser.add_argument(
        "--keyword-subset",
        action="store_false",
        dest="include_all_chunks",
        help="Use only chunks containing --selection-keyword values when packaging an operator-confirmed runtime.",
    )
    parser.add_argument("--selection-keyword", action="append", default=[])
    parser.add_argument("--smoke-query", action="append", default=[])
    parser.add_argument(
        "--operator-approval-reference",
        default=None,
        help="Required human review ticket, approval memo, or operator sign-off reference for official publish.",
    )
    parser.add_argument(
        "--operator-reviewer-id",
        default=None,
        help="Required reviewer/operator ID that completed the human review before publish.",
    )
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = prepare_mcp_publish_runtime(
        source_data_dir=args.source_data_dir,
        target_data_dir=args.target_data_dir,
        source_document_id=args.source_document_id,
        tenant_id=args.tenant_id,
        security_level=args.security_level,
        profile_id=args.profile_id,
        institution_name=args.institution_name,
        document_name=args.document_name,
        source_system=args.source_system,
        include_all_chunks=args.include_all_chunks,
        selection_keywords=args.selection_keyword,
        smoke_queries=args.smoke_query,
        operator_approval_reference=args.operator_approval_reference,
        operator_reviewer_id=args.operator_reviewer_id,
        out_json=args.out_json,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report.get("passed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
