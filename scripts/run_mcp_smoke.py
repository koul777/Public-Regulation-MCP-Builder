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

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.mcp_server.regulation_tools import (
    compare_versions,
    fetch_regulation,
    get_article,
    get_citation,
    get_index_status,
    get_regulation_history,
    get_table,
    list_documents,
    mcp_auth_context,
    search_regulations,
)
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.build_rag_security_evidence import build_rag_security_evidence


def run_mcp_smoke(
    *,
    data_dir: Path | None = None,
    tenant_id: str = "tenant-mcp-smoke",
    profile_id: str | None = None,
    tenant_storage_isolation: bool = True,
    out_json: Path | None = None,
    allow_existing_data: bool = False,
    allow_persistent_smoke_data: bool = False,
    disposable_data_dir: bool = False,
) -> dict[str, Any]:
    if data_dir is None:
        with tempfile.TemporaryDirectory(prefix="reg_rag_mcp_smoke_") as tmp:
            return _run_smoke_with_data_dir(
                Path(tmp) / "data",
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                out_json=out_json,
                allow_existing_data=allow_existing_data,
                data_dir_mode="temporary",
                persistent_smoke_data_opt_in=False,
            )
    if not allow_persistent_smoke_data and not disposable_data_dir:
        raise ValueError(
            "Refusing to write synthetic MCP smoke documents into an explicit data directory. "
            "Use the default temporary mode or pass --allow-persistent-smoke-data only for an explicitly "
            "disposable runtime."
        )
    data_dir_mode = "temporary" if disposable_data_dir else "explicit_persistent_opt_in"
    return _run_smoke_with_data_dir(
        data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        tenant_storage_isolation=tenant_storage_isolation,
        out_json=out_json,
        allow_existing_data=allow_existing_data,
        data_dir_mode=data_dir_mode,
        persistent_smoke_data_opt_in=allow_persistent_smoke_data,
    )


def _run_smoke_with_data_dir(
    data_dir: Path,
    *,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    out_json: Path | None,
    allow_existing_data: bool,
    data_dir_mode: str,
    persistent_smoke_data_opt_in: bool,
) -> dict[str, Any]:
    base_settings = Settings(
        data_dir=data_dir,
        artifact_root=data_dir.parent,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    repository_settings = settings_for_tenant(base_settings, tenant_id)
    if not allow_existing_data and _has_non_smoke_runtime_data(repository_settings):
        raise ValueError(
            "Refusing to write synthetic MCP smoke documents into an existing non-smoke runtime. "
            "Use a temporary data directory, run transport smoke with --skip-preparation, or pass "
            "--allow-existing-data only for an explicitly disposable runtime."
        )
    auth = AuthContext(actor="mcp-smoke", tenant_id=tenant_id, auth_mode="api_token", role="admin")
    _save_smoke_document(
        repository_settings,
        document_id="doc_mcp_smoke_v1",
        tenant_id=tenant_id,
        profile_id=profile_id,
        regulation_id="reg_mcp_smoke",
        regulation_version="1.0",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
        article_text="Article 1: Requests shall be submitted through the designated process.",
        table_text="Category | Content\nApplication | Article 1",
    )
    _save_smoke_document(
        repository_settings,
        document_id="doc_mcp_smoke_v2",
        tenant_id=tenant_id,
        profile_id=profile_id,
        regulation_id="reg_mcp_smoke",
        regulation_version="2.0",
        effective_from="2026-01-01",
        supersedes_document_id="doc_mcp_smoke_v1",
        article_text="Article 2: Requests shall include the required supporting information.",
        table_text="Category | Content\nApplication | Article 2",
    )
    with patch.object(routes_documents, "get_settings", return_value=base_settings):
        for document_id, approval_id in (
            ("doc_mcp_smoke_v1", "approval-mcp-smoke-v1"),
            ("doc_mcp_smoke_v2", "approval-mcp-smoke-v2"),
        ):
            chunks = JsonRepository(repository_settings).get_chunks(document_id)
            approval_evidence = _write_smoke_approval_evidence(
                base_settings,
                runtime_settings=repository_settings,
                tenant_id=tenant_id,
                document_id=document_id,
                chunks=chunks,
            )
            routes_documents.approve_review_chunks(
                document_id,
                routes_documents.ApprovalRequest(
                    chunk_ids=[f"{document_id}-article", f"{document_id}-table"],
                    approval_id=approval_id,
                    security_level="internal",
                    **approval_evidence,
                ),
                auth,
            )
            routes_documents.index_document(
                document_id,
                routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                auth,
            )

    mcp_auth = mcp_auth_context(tenant_id=tenant_id)
    search = search_regulations(
        settings=repository_settings,
        auth=mcp_auth,
        query="Article",
        profile_id=profile_id,
        security_levels=["internal"],
    )
    fetched = fetch_regulation(
        settings=repository_settings,
        auth=mcp_auth,
        result_id=search["results"][0]["id"] if search["results"] else "",
        security_levels=["internal"],
    ) if search["results"] else {}
    citation = (
        get_citation(settings=repository_settings, auth=mcp_auth, result_id=search["results"][0]["id"])
        if search["results"]
        else {}
    )
    articles = get_article(
        settings=repository_settings,
        auth=mcp_auth,
        document_id="doc_mcp_smoke_v2",
        article_no="1",
        security_levels=["internal"],
    )
    table = get_table(
        settings=repository_settings,
        auth=mcp_auth,
        table_id="mcp-smoke-table",
        document_id="doc_mcp_smoke_v2",
        security_levels=["internal"],
    )
    comparison = compare_versions(
        settings=repository_settings,
        auth=mcp_auth,
        base_document_id="doc_mcp_smoke_v1",
        target_document_id="doc_mcp_smoke_v2",
        security_levels=["internal"],
    )
    documents = list_documents(settings=repository_settings, auth=mcp_auth, security_levels=["internal"])
    index_status = get_index_status(
        settings=repository_settings,
        auth=mcp_auth,
        document_id="doc_mcp_smoke_v2",
        security_levels=["internal"],
    )
    history = get_regulation_history(
        settings=repository_settings,
        auth=mcp_auth,
        regulation_id="reg_mcp_smoke",
        profile_id=profile_id,
    )
    history_versions = history.get("versions") if isinstance(history.get("versions"), list) else []
    history_has_superseded = any(
        str(version.get("regulation_status") or "").strip().casefold() == "superseded"
        for version in history_versions
        if isinstance(version, dict)
    )
    evidence = build_rag_security_evidence(
        data_dir=base_settings.data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    report = {
        "report_type": "local_mcp_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "data_dir_mode": data_dir_mode,
        "synthetic_runtime": True,
        "handoff_evidence": False,
        "persistent_smoke_data_opt_in": persistent_smoke_data_opt_in,
        "existing_data_opt_in": allow_existing_data,
        "passed": bool(
            search["results"]
            and fetched.get("text")
            and citation.get("metadata", {}).get("approved_content_hash")
            and articles["articles"]
            and table["tables"]
            and comparison["summary"]["changed_count"] >= 1
            and documents["documents"]
            and index_status["documents"]
            and index_status["documents"][0]["indexing_status"] == "indexed"
            and history.get("current_document_id") == "doc_mcp_smoke_v2"
            and history_has_superseded
            and evidence.get("passed")
        ),
        "search_result_count": len(search["results"]),
        "fetch_has_text": bool(fetched.get("text")),
        "citation_has_approved_hash": bool(citation.get("metadata", {}).get("approved_content_hash")),
        "article_count": len(articles["articles"]),
        "table_count": len(table["tables"]),
        "document_count": len(documents["documents"]),
        "comparison_summary": comparison["summary"],
        "index_status_summary": index_status["summary"],
        "history_summary": {
            "current_document_id": history.get("current_document_id"),
            "as_of_date": history.get("as_of_date"),
            "version_count": len(history_versions),
            "has_superseded_version": history_has_superseded,
        },
        "evidence_summary": {
            "passed": bool(evidence.get("passed")),
            "vector_record_count": evidence.get("vector_record_count"),
            "approval_record_count": evidence.get("approval_record_count"),
            "approval_vector_sync_outcome_count": evidence.get("approval_vector_sync_outcome_count"),
            "approval_vector_sync_legacy_approval_count": evidence.get(
                "approval_vector_sync_legacy_approval_count"
            ),
            "approval_vector_sync_policy": evidence.get("approval_vector_sync_policy"),
            "approval_vector_sync_failure_count": evidence.get("approval_vector_sync_failure_count"),
            "approval_vector_sync_failure_samples": evidence.get("approval_vector_sync_failure_samples"),
            "indexing_job_count": evidence.get("indexing_job_count"),
            "rag_trace_count": evidence.get("rag_trace_count"),
            "api_audit_action_counts": evidence.get("api_audit_action_counts"),
            "audit_control_failure_count": evidence.get("audit_control_failure_count"),
            "component_manifest_hash": evidence.get("component_manifest_hash"),
        },
    }
    runtime_manifest_path = repository_settings.data_dir / "mcp_runtime_manifest.json"
    runtime_manifest_path.write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "generated_at": report["generated_at"],
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "document_ids": ["doc_mcp_smoke_v1", "doc_mcp_smoke_v2"],
                "record_count": evidence.get("vector_record_count"),
                "synthetic_runtime": True,
                "provenance": "run_mcp_smoke",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
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


def _has_non_smoke_runtime_data(settings: Settings) -> bool:
    repository_dir = settings.data_dir / "repository"
    if not repository_dir.exists():
        return False
    for path in repository_dir.glob("*_chunks.json"):
        if not path.name.startswith("doc_mcp_smoke"):
            return True
    return False


def _save_smoke_document(
    settings: Settings,
    *,
    document_id: str,
    tenant_id: str,
    profile_id: str | None,
    article_text: str,
    table_text: str,
    regulation_id: str,
    regulation_version: str,
    effective_from: str,
    effective_to: str | None = None,
    supersedes_document_id: str | None = None,
) -> None:
    repository = JsonRepository(settings)
    lifecycle_metadata = {
        "document_name": f"MCP Smoke Regulation {regulation_version}",
        "institution_name": "Synthetic Institution",
        "regulation_title": "MCP Smoke Regulation",
        "source_system": "synthetic-fixture",
        "source_url": f"https://example.invalid/regulations/{document_id}",
        "source_page_start": 1,
        "source_page_end": 1,
        "profile_id": profile_id,
        "regulation_id": regulation_id,
        "regulation_version": regulation_version,
        "revision_date": effective_from,
        "effective_date": effective_from,
        "regulation_status": "draft",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "repealed_at": None,
        "supersedes_document_id": supersedes_document_id,
    }
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            document_name=document_id,
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            institution_name="Synthetic Institution",
            source_system="synthetic-fixture",
            source_url=f"https://example.invalid/regulations/{document_id}",
            tenant_id=tenant_id,
            profile_id=profile_id,
            regulation_id=regulation_id,
            regulation_version=regulation_version,
            revision_date=effective_from,
            effective_from=effective_from,
            effective_to=effective_to,
            repealed_at=None,
            supersedes_document_id=supersedes_document_id,
            regulation_status="draft",
            status="completed",
        )
    )
    repository.save_processing_result(
        document_id,
        [],
        [
            Chunk(
                chunk_id=f"{document_id}-article",
                document_id=document_id,
                chunk_type="article",
                text=article_text,
                retrieval_text=article_text,
                source_page_start=1,
                source_page_end=1,
                metadata={**lifecycle_metadata, "article_no": "1", "article_title": "Application requests"},
                security_level="internal",
            ),
            Chunk(
                chunk_id=f"{document_id}-table",
                document_id=document_id,
                chunk_type="appendix",
                text=table_text,
                retrieval_text=table_text,
                source_page_start=1,
                source_page_end=1,
                metadata={**lifecycle_metadata,
                    "table_like": True,
                    "table_id": "mcp-smoke-table",
                    "table_title": "Application request table",
                    "table_rows": table_text.splitlines(),
                },
                security_level="internal",
            ),
        ],
        [],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local MCP approval/index/tool/evidence smoke.")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--tenant-id", default="tenant-mcp-smoke")
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    parser.add_argument("--allow-existing-data", action="store_true")
    parser.add_argument("--allow-persistent-smoke-data", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_json = Path(args.out_json) if args.out_json else None
    try:
        report = run_mcp_smoke(
            data_dir=Path(args.data_dir) if args.data_dir else None,
            tenant_id=args.tenant_id,
            profile_id=args.profile_id,
            tenant_storage_isolation=not args.flat_storage,
            out_json=out_json,
            allow_existing_data=args.allow_existing_data,
            allow_persistent_smoke_data=args.allow_persistent_smoke_data,
        )
    except ValueError as exc:
        report = {
            "report_type": "local_mcp_smoke",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": args.tenant_id,
            "tenant_storage_isolation": not args.flat_storage,
            "data_dir_mode": "explicit_refused" if args.data_dir else "unknown",
            "synthetic_runtime": True,
            "handoff_evidence": False,
            "persistent_smoke_data_opt_in": bool(args.allow_persistent_smoke_data),
            "existing_data_opt_in": bool(args.allow_existing_data),
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
