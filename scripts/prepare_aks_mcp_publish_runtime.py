from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api import routes_documents, routes_rag
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.mcp_server.regulation_tools import fetch_regulation, mcp_auth_context, search_regulations
from app.processors.answer_profile import append_answer_profile_to_retrieval_text, build_answer_profile
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.mcp_publish_approval_evidence import build_publish_approval_evidence


DEFAULT_SOURCE_DOC_ID = "doc_035798a12673"
DEFAULT_TENANT_ID = "tenant-aks-publish"
DEFAULT_SECURITY_LEVEL = "internal"
DEFAULT_AKS_PROFILE_ID = "aks-korean-studies"
DEFAULT_AKS_SOURCE_URL = "local-preprocessed-regulation-corpus"
DEFAULT_EMBEDDING_DIMENSIONS = 384

AKS_KEYWORDS = (
    "육아휴직",
    "휴직 기간",
    "휴직의 효력",
    "시간선택제",
    "육아휴직수당",
    "성과연봉",
    "기본연봉",
    "연봉의 조정",
    "성과연봉 지급대상 제외",
    "교직원보수규정",
    "교원 임용",
    "신규임용",
    "신규 임용",
    "공개발표심사",
    "면접심사",
    "연구실적심사",
    "기초심사",
    "교원 인사위원회",
    "임용예정",
    "교수임용자격기준표",
    "지원 마감일 전까지 15일",
)

REGULATION_TITLES = (
    "교직원보수규정",
    "교직원 보수규정",
    "인사규정",
    "교원 임용 세칙",
    "교원업적평가 규정",
    "연구직업적평가 규정",
)

SMOKE_QUERIES = (
    "육아휴직은 얼마나 신청할 수 있어?",
    "성과연봉은 언제 지급해?",
    "전임 교원 채용 절차는 어떻게 돼?",
)

ARTICLE_RE = re.compile(r"(제\s*\d+\s*조(?:의\s*\d+)?)\s*\(([^)]+)\)")


def prepare_aks_mcp_publish_runtime(
    *,
    source_data_dir: Path,
    target_data_dir: Path,
    source_document_id: str = DEFAULT_SOURCE_DOC_ID,
    tenant_id: str = DEFAULT_TENANT_ID,
    security_level: str = DEFAULT_SECURITY_LEVEL,
    include_all_chunks: bool = True,
    operator_approval_reference: str | None = None,
    operator_reviewer_id: str | None = None,
    draft_only: bool = False,
    out_json: Path | None = None,
) -> dict[str, Any]:
    source_repository = JsonRepository(Settings(data_dir=source_data_dir))
    source_document = source_repository.get_document(source_document_id)
    if source_document is None:
        raise ValueError(f"Source document not found: {source_document_id}")
    raw_chunks = _load_raw_chunks(source_data_dir, source_document_id)
    selected_chunks = [
        _prepare_chunk(raw, tenant_id=tenant_id, security_level=security_level)
        for raw in raw_chunks
        if include_all_chunks or _is_selected_chunk(raw)
    ]
    if not selected_chunks:
        raise ValueError("No chunks selected for AKS MCP publish runtime.")
    document = _prepare_document(source_document, tenant_id=tenant_id)
    if not draft_only:
        _preflight_publish_approval_evidence(
            document=document,
            selected_chunks=selected_chunks,
            tenant_id=tenant_id,
            security_level=security_level,
            operator_approval_reference=operator_approval_reference,
            operator_reviewer_id=operator_reviewer_id,
        )

    base_settings = Settings(data_dir=target_data_dir, artifact_root=target_data_dir, tenant_storage_isolation=True)
    target_settings = settings_for_tenant(base_settings, tenant_id)
    _reset_target_tenant_runtime(target_data_dir=target_data_dir, tenant_data_dir=target_settings.data_dir)
    repository = JsonRepository(target_settings)
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], selected_chunks, [])

    if draft_only:
        report = {
            "report_type": "aks_mcp_publish_runtime",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_data_dir": str(source_data_dir),
            "target_data_dir": str(target_data_dir),
            "tenant_data_dir": str(target_settings.data_dir),
            "tenant_id": tenant_id,
            "source_document_id": source_document_id,
            "document_id": document.document_id,
            "institution_name": document.institution_name,
            "source_chunk_count": len(raw_chunks),
            "selected_chunk_count": len(selected_chunks),
            "draft_only": True,
            "approved_chunk_count": 0,
            "approval_record_count": 0,
            "approval_evidence": {},
            "indexed_record_count": 0,
            "index_status": "skipped_draft_only",
            "vector_artifacts": {},
            "mcp_smoke": [],
            "ready_for_official_mcp": False,
            "passed": bool(selected_chunks),
            "next_steps": [
                "Build approval evidence from this draft runtime.",
                "Complete human review and acknowledge parser/table flags where required.",
                "Approve reviewed batches through the shared review workflow.",
                "Index approved chunks and run MCP visibility/connection readiness checks.",
            ],
            "safety_note": (
                "Draft-only mode writes draft repository chunks only. It does not approve chunks, "
                "write approval journal records, index Vector DB records, or run MCP smoke."
            ),
        }
        if out_json:
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report

    auth = AuthContext(actor="aks-mcp-publisher", tenant_id=tenant_id, auth_mode="script", role="admin")
    chunk_ids = [chunk.chunk_id for chunk in selected_chunks]
    approval_evidence = build_publish_approval_evidence(
        data_dir=target_data_dir,
        artifact_root=base_settings.artifact_root,
        tenant_id=tenant_id,
        tenant_storage_isolation=True,
        document_id=document.document_id,
        chunk_ids=chunk_ids,
        security_level=security_level,
        artifact_prefix="aks_mcp_publish",
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
                    "approval_id": f"approval-aks-publish-{approval_stamp}-{index:03d}",
                    "security_level": security_level,
                    "note": (
                        "AKS preprocessed regulation chunks approved for MCP MVP publish runtime "
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

    runtime_manifest = _write_runtime_contract(
        settings=target_settings,
        auth=auth,
        source_data_dir=source_data_dir,
        target_data_dir=target_data_dir,
        tenant_id=tenant_id,
        document=document,
        approval_record_count=len(approval_records),
        indexing_job_count=1 if index_job else 0,
        source_document_id=source_document_id,
    )
    approval_evidence_validation = _validate_publish_runtime_approval_evidence(
        vector_path=routes_rag._local_vector_path(target_settings, auth),
        approval_journal_path=repository.root / "journals" / "approvals.jsonl",
        approval_evidence=approval_evidence,
    )
    smoke = _run_mcp_smoke_queries(target_settings, tenant_id=tenant_id, security_level=security_level)
    report = {
        "report_type": "aks_mcp_publish_runtime",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_data_dir": str(source_data_dir),
        "target_data_dir": str(target_data_dir),
        "tenant_data_dir": str(target_settings.data_dir),
        "tenant_id": tenant_id,
        "source_document_id": source_document_id,
        "document_id": document.document_id,
        "institution_name": document.institution_name,
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
        "vector_artifacts": index_job.get("artifacts", {}),
        "runtime_manifest": runtime_manifest,
        "approval_evidence_validation": approval_evidence_validation,
        "mcp_smoke": smoke,
        "passed": bool(
            selected_chunks
            and int(index_job.get("record_count") or 0) == len(selected_chunks)
            and approval_evidence_validation.get("passed")
            and all(item.get("result_count", 0) > 0 and item.get("fetch_has_text") for item in smoke)
        ),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _load_raw_chunks(source_data_dir: Path, document_id: str) -> list[dict[str, Any]]:
    path = source_data_dir / "repository" / f"{document_id}_chunks.json"
    if not path.is_file():
        raise ValueError(f"Source chunks not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError(f"Source chunks must be a JSON array: {path}")
    return [item for item in payload if isinstance(item, dict)]


def _reset_target_tenant_runtime(*, target_data_dir: Path, tenant_data_dir: Path) -> bool:
    target_root = target_data_dir.resolve()
    tenant_root = tenant_data_dir.resolve()
    tenants_root = (target_root / "tenants").resolve()
    if tenants_root not in tenant_root.parents:
        raise ValueError(f"Refusing to reset tenant runtime outside target tenants directory: {tenant_root}")
    if tenant_root == tenants_root or tenant_root == target_root:
        raise ValueError(f"Refusing to reset unsafe tenant runtime path: {tenant_root}")
    _clear_root_runtime_artifacts(target_data_dir=target_data_dir)
    if not tenant_root.exists():
        return False
    shutil.rmtree(tenant_root)
    return True


def _clear_root_runtime_artifacts(*, target_data_dir: Path) -> None:
    target_root = target_data_dir.resolve()
    for relative_path in ("repository", "vector_db", "mcp_runtime_manifest.json"):
        _remove_runtime_artifact(target_root=target_root, path=target_root / relative_path)


def _remove_runtime_artifact(*, target_root: Path, path: Path) -> None:
    resolved = path.resolve()
    if resolved == target_root or target_root not in resolved.parents:
        raise ValueError(f"Refusing to remove runtime artifact outside target data dir: {resolved}")
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()


def _write_runtime_contract(
    *,
    settings: Settings,
    auth: AuthContext,
    source_data_dir: Path,
    target_data_dir: Path,
    tenant_id: str,
    document: Document,
    approval_record_count: int,
    indexing_job_count: int,
    source_document_id: str,
) -> dict[str, Any]:
    repository = JsonRepository(settings)
    document_ids = [document.document_id]
    _remove_raw_runtime_result_artifacts(repository.root, document_ids=document_ids)
    records = routes_rag._load_local_vector_records(settings, auth)
    vector_path = routes_rag._local_vector_path(settings, auth)
    bm25_index_path = routes_rag.default_bm25_index_path(vector_path)
    approval_snapshot_path = _write_runtime_approval_snapshot_sidecar(
        settings=settings,
        tenant_id=tenant_id,
        document_ids=document_ids,
        records=records,
    )
    chunk_count = sum(len(repository.get_chunks(document_id)) for document_id in document_ids)
    result_files = [
        str(repository.root / f"{document_id}_chunks.json")
        for document_id in document_ids
        if (repository.root / f"{document_id}_chunks.json").is_file()
    ]
    runtime_manifest = {
        "report_type": "mcp_runtime_data_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "synthetic_runtime": False,
        "provenance": "approved_aks_runtime_publish",
        "tenant_storage_isolation": True,
        "source_data_dir": str(source_data_dir),
        "target_data_dir": str(target_data_dir),
        "runtime_data_dir": str(settings.data_dir),
        "source_document_id": source_document_id,
        "document_id": document.document_id,
        "document_ids": document_ids,
        "record_count": len(records),
        "chunk_count": chunk_count,
        "recommended_smoke_query": _recommended_smoke_query(records),
        "approval_record_count": approval_record_count,
        "indexing_job_count": indexing_job_count,
        "bm25_document_count": len(records) if bm25_index_path.is_file() else 0,
        "bm25_index_status": "ready" if bm25_index_path.is_file() else "missing",
        "files": {
            "vector_jsonl": str(vector_path) if vector_path.is_file() else None,
            "bm25_index": str(bm25_index_path) if bm25_index_path.is_file() else None,
            "repository_manifest": str(repository.manifest_path),
            "approval_journal": str(repository.root / "journals" / "approvals.jsonl"),
            "approval_snapshot": str(approval_snapshot_path),
            "result_files": result_files,
        },
    }
    runtime_manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
    runtime_manifest_path.write_text(json.dumps(runtime_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runtime_manifest["files"]["runtime_manifest"] = str(runtime_manifest_path)
    return runtime_manifest


def _validate_publish_runtime_approval_evidence(
    *,
    vector_path: Path,
    approval_journal_path: Path,
    approval_evidence: dict[str, Any],
) -> dict[str, Any]:
    vector_records = _read_jsonl_objects(vector_path)
    approval_records = _read_jsonl_objects(approval_journal_path)
    checks = [
        _approval_evidence_field_check(
            records=vector_records,
            scope="vector_metadata",
            field="approval_worklist_report_sha256",
            expected_sha256=str(approval_evidence.get("worklist_report_sha256") or ""),
            value_getter=lambda record: _dict_value(record.get("metadata"), "approval_worklist_report_sha256"),
        ),
        _approval_evidence_field_check(
            records=vector_records,
            scope="vector_metadata",
            field="approval_review_batch_manifest_sha256",
            expected_sha256=str(approval_evidence.get("review_batch_manifest_sha256") or ""),
            value_getter=lambda record: _dict_value(record.get("metadata"), "approval_review_batch_manifest_sha256"),
        ),
        _approval_evidence_field_check(
            records=approval_records,
            scope="approval_journal",
            field="worklist_report_sha256",
            expected_sha256=str(approval_evidence.get("worklist_report_sha256") or ""),
            value_getter=lambda record: _dict_value(record.get("worklist_evidence"), "worklist_report_sha256"),
        ),
        _approval_evidence_field_check(
            records=approval_records,
            scope="approval_journal",
            field="review_batch_manifest_sha256",
            expected_sha256=str(approval_evidence.get("review_batch_manifest_sha256") or ""),
            value_getter=lambda record: _dict_value(record.get("worklist_evidence"), "review_batch_manifest_sha256"),
        ),
    ]
    passed = bool(
        vector_path.is_file()
        and approval_journal_path.is_file()
        and vector_records
        and approval_records
        and all(check.get("passed") for check in checks)
    )
    return {
        "passed": passed,
        "vector_path": str(vector_path),
        "approval_journal_path": str(approval_journal_path),
        "vector_record_count": len(vector_records),
        "approval_record_count": len(approval_records),
        "checks": checks,
    }


def _approval_evidence_field_check(
    *,
    records: list[dict[str, Any]],
    scope: str,
    field: str,
    expected_sha256: str,
    value_getter,
) -> dict[str, Any]:
    observed: dict[str, int] = {}
    missing_count = 0
    mismatch_count = 0
    for record in records:
        value = str(value_getter(record) or "").strip()
        if not value:
            missing_count += 1
            continue
        observed[value] = observed.get(value, 0) + 1
        if value != expected_sha256:
            mismatch_count += 1
    return {
        "scope": scope,
        "field": field,
        "expected_sha256": expected_sha256,
        "record_count": len(records),
        "missing_count": missing_count,
        "mismatch_count": mismatch_count,
        "observed_sha256_counts": observed,
        "passed": bool(expected_sha256 and records and missing_count == 0 and mismatch_count == 0),
    }


def _dict_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _remove_raw_runtime_result_artifacts(repository_dir: Path, *, document_ids: list[str]) -> None:
    for document_id in document_ids:
        for suffix in ("nodes", "issues", "quality"):
            path = repository_dir / f"{document_id}_{suffix}.json"
            if path.exists():
                path.unlink()


def _write_runtime_approval_snapshot_sidecar(
    *,
    settings: Settings,
    tenant_id: str,
    document_ids: list[str],
    records: list[dict[str, Any]],
) -> Path:
    repository = JsonRepository(settings)
    entries: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: (str(item.get("document_id") or ""), str(item.get("chunk_id") or ""))):
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        document_id = str(record.get("document_id") or metadata.get("document_id") or "")
        chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
        if not document_id or not chunk_id:
            continue
        entries.append(
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "approval_id": metadata.get("approval_id"),
                "approved_content_hash": metadata.get("approved_content_hash"),
                "security_level": str(metadata.get("security_level") or "").strip().lower(),
                "department_acl": sorted(routes_rag._department_acl_set(metadata.get("department_acl"))),
                "content_hash": str(record.get("content_hash") or ""),
            }
        )
    sidecar_path = repository.root / "approval_snapshot.json"
    payload = {
        "report_type": "mcp_runtime_approval_snapshot",
        "schema_version": "mcp-runtime-approval-snapshot-v1",
        "tenant_id": tenant_id,
        "document_ids": document_ids,
        "record_count": len(records),
        "snapshot_count": len(entries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_signatures": {
            key: (list(value) if value is not None else None)
            for key, value in routes_rag._runtime_approval_snapshot_file_signatures(repository).items()
        },
        "entries": entries,
    }
    sidecar_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sidecar_path


def _recommended_smoke_query(records: list[dict[str, Any]]) -> str:
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        article_no = str(metadata.get("article_no") or "").strip()
        article_title = str(metadata.get("article_title") or "").strip()
        if article_no and article_title:
            return f"{article_no} {article_title}"
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        title = str(metadata.get("regulation_title") or metadata.get("document_name") or "").strip()
        if title:
            return title
    return SMOKE_QUERIES[0] if SMOKE_QUERIES else "규정"


def _is_selected_chunk(raw: dict[str, Any]) -> bool:
    blob = _chunk_blob(raw)
    return any(keyword in blob for keyword in AKS_KEYWORDS)


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


def _preflight_publish_approval_evidence(
    *,
    document: Document,
    selected_chunks: list[Chunk],
    tenant_id: str,
    security_level: str,
    operator_approval_reference: str | None,
    operator_reviewer_id: str | None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="aks_mcp_publish_preflight_") as tmp:
        preflight_data_dir = Path(tmp)
        base_settings = Settings(data_dir=preflight_data_dir, artifact_root=preflight_data_dir, tenant_storage_isolation=True)
        tenant_settings = settings_for_tenant(base_settings, tenant_id)
        repository = JsonRepository(tenant_settings)
        repository.upsert_document(document)
        repository.save_processing_result(document.document_id, [], selected_chunks, [])
        build_publish_approval_evidence(
            data_dir=preflight_data_dir,
            artifact_root=base_settings.artifact_root,
            tenant_id=tenant_id,
            tenant_storage_isolation=True,
            document_id=document.document_id,
            chunk_ids=[chunk.chunk_id for chunk in selected_chunks],
            security_level=security_level,
            artifact_prefix="aks_mcp_publish_preflight",
            operator_approval_reference=operator_approval_reference,
            operator_reviewer_id=operator_reviewer_id,
        )


def _prepare_document(source_document: Document, *, tenant_id: str) -> Document:
    return source_document.model_copy(
        update={
            "document_name": "한국학중앙연구원 원규집(전처리본)",
            "institution_name": "한국학중앙연구원",
            "source_system": source_document.source_system or "AKS_PREPROCESSED_LOCAL",
            "source_url": source_document.source_url or DEFAULT_AKS_SOURCE_URL,
            "profile_id": source_document.profile_id or DEFAULT_AKS_PROFILE_ID,
            "tenant_id": tenant_id,
        }
    )


def _prepare_chunk(raw: dict[str, Any], *, tenant_id: str, security_level: str) -> Chunk:
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
    metadata["institution_name"] = "한국학중앙연구원"
    metadata["document_name"] = "한국학중앙연구원 원규집(전처리본)"
    metadata["source_system"] = metadata.get("source_system") or "AKS_PREPROCESSED_LOCAL"
    metadata["source_url"] = metadata.get("source_url") or DEFAULT_AKS_SOURCE_URL
    metadata["profile_id"] = metadata.get("profile_id") or DEFAULT_AKS_PROFILE_ID
    metadata["regulation_title"] = _infer_regulation_title(blob, metadata)
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


def _infer_regulation_title(blob: str, metadata: dict[str, Any]) -> str:
    existing = str(metadata.get("regulation_title") or "").strip()
    if existing:
        return existing
    for title in REGULATION_TITLES:
        if title in blob:
            return "교직원보수규정" if title == "교직원 보수규정" else title
    return ""


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


def _run_mcp_smoke_queries(settings: Settings, *, tenant_id: str, security_level: str) -> list[dict[str, Any]]:
    auth = mcp_auth_context(tenant_id=tenant_id, actor="aks-mcp-smoke", role="operator")
    results: list[dict[str, Any]] = []
    for query in SMOKE_QUERIES:
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
    parser = argparse.ArgumentParser(description="Prepare an AKS preprocessed regulation runtime for MCP demos.")
    parser.add_argument("--source-data-dir", type=Path, default=Path("data"))
    parser.add_argument("--target-data-dir", type=Path, default=Path("data/aks_mcp_publish_runtime"))
    parser.add_argument("--source-document-id", default=DEFAULT_SOURCE_DOC_ID)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--security-level", default=DEFAULT_SECURITY_LEVEL)
    parser.set_defaults(include_all_chunks=True)
    parser.add_argument(
        "--include-all-chunks",
        action="store_true",
        help="Use the full preprocessed AKS corpus when packaging an operator-confirmed runtime. This is the default.",
    )
    parser.add_argument(
        "--keyword-subset",
        action="store_false",
        dest="include_all_chunks",
        help="Use only the legacy keyword subset when packaging an operator-confirmed runtime.",
    )
    parser.add_argument("--out-json", type=Path, default=Path("reports/aks_mcp_publish_runtime_report.json"))
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
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help=(
            "Prepare draft tenant runtime only. This skips approval, approval journal writes, "
            "Vector DB indexing, and MCP smoke so human review evidence can be built first."
        ),
    )
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = prepare_aks_mcp_publish_runtime(
        source_data_dir=args.source_data_dir,
        target_data_dir=args.target_data_dir,
        source_document_id=args.source_document_id,
        tenant_id=args.tenant_id,
        security_level=args.security_level,
        include_all_chunks=args.include_all_chunks,
        operator_approval_reference=args.operator_approval_reference,
        operator_reviewer_id=args.operator_reviewer_id,
        draft_only=args.draft_only,
        out_json=args.out_json,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report.get("passed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
