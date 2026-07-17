from __future__ import annotations

import argparse
import hashlib
import json
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
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_rag_security_evidence import build_rag_security_evidence


def run_secure_rag_smoke(
    *,
    data_dir: Path | None = None,
    tenant_id: str = "tenant-smoke",
    tenant_storage_isolation: bool = True,
    out_json: Path | None = None,
    allow_persistent_smoke_data: bool = False,
) -> dict[str, Any]:
    if data_dir is None:
        with tempfile.TemporaryDirectory(prefix="reg_rag_secure_smoke_") as tmp:
            return _run_smoke_with_data_dir(
                Path(tmp) / "data",
                tenant_id=tenant_id,
                tenant_storage_isolation=tenant_storage_isolation,
                out_json=out_json,
                data_dir_mode="temporary",
                persistent_smoke_data_opt_in=False,
            )
    if not allow_persistent_smoke_data:
        raise ValueError(
            "Refusing to write synthetic approved/indexed secure-RAG smoke data into an explicit data directory. "
            "Use the default temporary mode or pass --allow-persistent-smoke-data only for an explicitly "
            "disposable runtime."
        )
    return _run_smoke_with_data_dir(
        data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        out_json=out_json,
        data_dir_mode="explicit_persistent_opt_in",
        persistent_smoke_data_opt_in=True,
    )


def _run_smoke_with_data_dir(
    data_dir: Path,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool,
    out_json: Path | None,
    data_dir_mode: str,
    persistent_smoke_data_opt_in: bool,
) -> dict[str, Any]:
    base_settings = Settings(
        data_dir=data_dir,
        artifact_root=data_dir.parent,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    repository_settings = settings_for_tenant(base_settings, tenant_id)
    repository = JsonRepository(repository_settings)
    document_id = "doc_secure_rag_smoke"
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename="secure_rag_smoke.pdf",
            document_name="Secure RAG Smoke",
            file_type="pdf",
            file_hash="secure-rag-smoke-hash",
            tenant_id=tenant_id,
            status="completed",
        )
    )
    repository.save_processing_result(
        document_id,
        [],
        [
            Chunk(
                chunk_id="chunk-secure-rag-smoke-1",
                document_id=document_id,
                chunk_type="article",
                text="Secure RAG smoke approval evidence text.",
                retrieval_text="Secure RAG smoke approval evidence text.",
                security_level="internal",
            )
        ],
        [],
    )
    auth = AuthContext(actor="secure-rag-smoke", tenant_id=tenant_id, auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=base_settings), patch.object(
        routes_rag, "get_settings", return_value=base_settings
    ):
        chunks = repository.get_chunks(document_id)
        approval_evidence = _write_smoke_approval_evidence(
            base_settings,
            runtime_settings=repository_settings,
            tenant_id=tenant_id,
            document_id=document_id,
            chunks=chunks,
        )
        approval = routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=["chunk-secure-rag-smoke-1"],
                approval_id="approval-secure-rag-smoke",
                security_level="internal",
                **approval_evidence,
            ),
            auth,
        )
        index_job = routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )
        search = routes_rag.rag_search(
            routes_rag.RagSearchRequest(query="approval evidence", security_levels=["internal"], document_id=document_id),
            auth,
        )
        runtime = routes_rag.rag_runtime_test(routes_rag.RagRuntimeTestRequest(query="approval evidence"), auth)

    evidence = build_rag_security_evidence(
        data_dir=base_settings.data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    report = {
        "report_type": "secure_rag_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "data_dir_mode": data_dir_mode,
        "synthetic_runtime": True,
        "handoff_evidence": False,
        "persistent_smoke_data_opt_in": persistent_smoke_data_opt_in,
        "passed": bool(evidence.get("passed")) and bool(search["results"]) and bool(runtime.get("ok")),
        "approval_id": approval["approval_id"],
        "indexing_status": index_job["status"],
        "search_result_count": len(search["results"]),
        "runtime_ok": bool(runtime.get("ok")),
        "evidence_summary": {
            "passed": bool(evidence.get("passed")),
            "vector_record_count": evidence.get("vector_record_count"),
            "approval_record_count": evidence.get("approval_record_count"),
            "indexing_job_count": evidence.get("indexing_job_count"),
            "rag_trace_count": evidence.get("rag_trace_count"),
            "api_audit_action_counts": evidence.get("api_audit_action_counts"),
            "stale_vector_record_count": evidence.get("stale_vector_record_count"),
            "metadata_failure_count": evidence.get("metadata_failure_count"),
            "approval_chain_failure_count": evidence.get("approval_chain_failure_count"),
            "indexing_job_failure_count": evidence.get("indexing_job_failure_count"),
            "audit_control_failure_count": evidence.get("audit_control_failure_count"),
            "component_manifest_hash": evidence.get("component_manifest_hash"),
        },
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _write_smoke_approval_evidence(
    settings: Settings,
    *,
    runtime_settings: Settings,
    tenant_id: str,
    document_id: str,
    chunks: list[Chunk],
) -> dict[str, str]:
    reports = settings.artifact_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_relative = f"reports/{document_id}_approval_worklist.json"
    batch_relative = f"reports/{document_id}_approval_review_batches.json"
    worklist_path = settings.artifact_root / worklist_relative
    batch_path = settings.artifact_root / batch_relative
    worklist = {
        "report_type": "approval_worklist",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(runtime_settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": settings.tenant_storage_isolation,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [{"document_id": document_id, "total_chunks": len(chunks)}],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)
    review_type = "low_risk_batch"
    batch_chunks = [
        {
            "chunk_id": chunk.chunk_id,
            "review_content_hash": routes_documents._review_content_hash(chunk),
            "approval_status": chunk.approval_status,
            "review_priority_tier": "no_signal",
            "review_category": "low_risk_batch_review_candidate",
            "attention_reasons": [],
        }
        for chunk in chunks
    ]
    fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(runtime_settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": settings.tenant_storage_isolation,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": worklist_relative,
            "sha256": worklist_sha256,
            "effective_data_dir": str(runtime_settings.data_dir),
            "tenant_id": tenant_id,
            "tenant_storage_isolation": settings.tenant_storage_isolation,
            "document_count": 1,
            "total_chunks": len(chunks),
        },
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": fingerprint,
                "review_type": review_type,
                "review_strategy": "human_bulk_review",
                "document_id": document_id,
                "chunk_count": len(chunks),
                "chunk_ids": [chunk.chunk_id for chunk in chunks],
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
            }
        ],
    }
    batch_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": worklist_relative,
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": batch_relative,
        "review_batch_manifest_sha256": _sha256_file(batch_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local secure RAG approval/index/search evidence smoke.")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--tenant-id", default="tenant-smoke")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    parser.add_argument("--allow-persistent-smoke-data", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_json = Path(args.out_json) if args.out_json else None
    try:
        report = run_secure_rag_smoke(
            data_dir=Path(args.data_dir) if args.data_dir else None,
            tenant_id=args.tenant_id,
            tenant_storage_isolation=not args.flat_storage,
            out_json=out_json,
            allow_persistent_smoke_data=args.allow_persistent_smoke_data,
        )
    except ValueError as exc:
        report = {
            "report_type": "secure_rag_smoke",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": args.tenant_id,
            "tenant_storage_isolation": not args.flat_storage,
            "data_dir_mode": "explicit_refused" if args.data_dir else "unknown",
            "synthetic_runtime": True,
            "handoff_evidence": False,
            "persistent_smoke_data_opt_in": bool(args.allow_persistent_smoke_data),
            "passed": False,
            "error": str(exc),
        }
        if out_json:
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
