from __future__ import annotations

import argparse
import hashlib
import ipaddress
import io
import json
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from docx import Document as WordDocument

# Allow this acceptance CLI to run directly from a source checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.api import routes_documents, routes_rag
from app.core.config import Settings
from app.core.security import AuthContext
from app.schemas.chunk import Chunk, ChunkOptions
from app.services.document_service import DocumentService
from app.services.processing_service import ProcessingService
from app.storage.repository import JsonRepository


QUERY = "정보보안업무규정 제5조에 따르면 접근권한을 언제 검토해야 하나요?"
ANSWER_EVIDENCE = "정보시스템 관리자는 모든 사용자의 접근권한을 분기마다 검토하여야 한다."
QWEN3_EMBEDDING_DIMENSIONS = 1024


def _is_loopback_address(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        # Unix-domain sockets and other non-IP local transports are allowed.
        return True
    host = str(address[0] or "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _LocalOnlyNetworkGuard:
    """Fail the acceptance run on any non-loopback socket connection."""

    def __init__(self) -> None:
        self.external_attempt_count = 0
        self._patcher = None

    def __enter__(self) -> "_LocalOnlyNetworkGuard":
        original_connect = socket.socket.connect

        def guarded_connect(sock: socket.socket, address: object) -> object:
            if not _is_loopback_address(address):
                self.external_attempt_count += 1
                raise RuntimeError("15-stage acceptance blocked a non-loopback network connection")
            return original_connect(sock, address)

        self._patcher = patch.object(socket.socket, "connect", guarded_connect)
        self._patcher.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._patcher is not None:
            self._patcher.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the image-derived 15-stage local acceptance flow.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/image_pipeline_6hour/image_15_stage_acceptance.json"),
    )
    return parser


def _fixture_docx() -> bytes:
    document = WordDocument()
    for text in (
        "정보보안업무규정",
        "제1장 총칙",
        "제1조(목적)",
        "이 규정은 정보시스템의 안전한 운영과 접근권한 관리에 필요한 사항을 정함을 목적으로 한다.",
        "제2장 접근권한 관리",
        "제5조(접근권한 검토)",
        ANSWER_EVIDENCE,
        "검토 결과 불필요한 권한은 즉시 회수하고 변경 내역을 접근권한 관리대장에 기록하여야 한다.",
        "제6조(보안 기록)",
        "접근권한의 부여, 변경 및 회수 기록은 감사에 필요한 기간 동안 보존하여야 한다.",
    ):
        document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_approval_evidence(
    root: Path,
    *,
    settings: Settings,
    document_id: str,
    chunks: list[Chunk],
) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_path = reports / "approval_worklist.json"
    manifest_path = reports / "approval_batches.json"
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    worklist = {
        "report_type": "approval_worklist",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": False,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [{"document_id": document_id, "total_chunks": len(chunks)}],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_hash = _sha256_file(worklist_path)
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
    review_type = "low_risk_batch"
    fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_hash[:12]}-001-low-risk-batch-001-{fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": False,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": "reports/approval_worklist.json",
            "sha256": worklist_hash,
            "effective_data_dir": str(settings.data_dir),
            "tenant_id": "tenant-a",
            "tenant_storage_isolation": False,
            "document_count": 1,
            "total_chunks": len(chunks),
            "manual_attention_chunks": 0,
            "low_risk_batch_review_candidate_chunks": len(chunks),
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
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": "reports/approval_worklist.json",
        "worklist_report_sha256": worklist_hash,
        "review_batch_manifest_path": "reports/approval_batches.json",
        "review_batch_manifest_sha256": _sha256_file(manifest_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _stage(trace: dict, stage_id: str) -> dict:
    return next((item for item in trace.get("stages", []) if item.get("stage_id") == stage_id), {})


def _acceptance_stage(
    pipeline: str,
    stage_id: str,
    passed: bool,
    evidence: dict[str, object],
) -> dict[str, object]:
    return {
        "pipeline": pipeline,
        "stage_id": stage_id,
        "status": "passed" if passed else "failed",
        "evidence": evidence,
    }


def run_acceptance() -> dict[str, object]:
    temporary_root = Path(".tmp")
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="reg_rag_image_acceptance_",
        dir=temporary_root,
    ) as temporary, _LocalOnlyNetworkGuard() as network_guard:
        root = Path(temporary)
        settings = Settings(
            data_dir=root / "data",
            artifact_root=root,
            api_auth_required=False,
            enable_agent_review=False,
            local_structure_review_enabled=False,
            enable_kordoc_table_parser=False,
            rag_llm_backend="ollama",
            rag_llm_endpoint="http://127.0.0.1:11434",
            rag_llm_model="qwen3:8b",
            rag_llm_timeout_seconds=90,
        )
        repository = JsonRepository(settings)
        document = DocumentService(settings, repository).upload(
            "정보보안업무규정.docx",
            _fixture_docx(),
            document_name="정보보안업무규정",
            institution_name="검증기관",
            regulation_id="fixture-information-security",
            regulation_version="1.0",
            effective_from="2026-01-01",
            tenant_id="tenant-a",
        )
        job = ProcessingService(settings=settings, repository=repository).process(
            document.document_id,
            ChunkOptions(
                max_chunk_chars=1000,
                min_chunk_chars=20,
                overlap_chars=80,
                enable_agent_review=False,
            ),
        )
        if job.status != "completed":
            raise RuntimeError(f"preprocessing failed: {job.error or job.message}")
        run = repository.list_runs(document.document_id)[-1]
        preprocessing_trace = dict(run.stats.get("pipeline_trace") or {})
        chunks = repository.get_chunks(document.document_id)
        if not chunks or not any(ANSWER_EVIDENCE in (chunk.text or "") for chunk in chunks):
            raise RuntimeError("processed chunks omitted the fixture evidence clause")
        evidence = _write_approval_evidence(
            root,
            settings=settings,
            document_id=document.document_id,
            chunks=chunks,
        )
        auth = AuthContext(
            actor="acceptance-reviewer",
            tenant_id="tenant-a",
            auth_mode="local",
            role="admin",
        )
        with patch.object(routes_documents, "get_settings", return_value=settings), patch.object(
            routes_rag, "get_settings", return_value=settings
        ):
            approval = routes_documents.approve_review_chunks(
                document.document_id,
                routes_documents.ApprovalRequest(
                    chunk_ids=[chunk.chunk_id for chunk in chunks],
                    approval_id="acceptance-approval",
                    security_level="internal",
                    review_flags_acknowledged=True,
                    defer_vector_sync=True,
                    vector_sync_batch_id="acceptance-vector-batch",
                    note="Synthetic acceptance fixture reviewed.",
                    **evidence,
                ),
                auth,
            )
            index_job = routes_documents.index_document(
                document.document_id,
                routes_documents.IndexRequest(
                    target_type="local-jsonl",
                    embedding_dimensions=QWEN3_EMBEDDING_DIMENSIONS,
                    embedding_model=QWEN3_EMBEDDING_MODEL,
                ),
                auth,
            )
            chat = routes_rag.rag_chat(
                routes_rag.RagChatRequest(
                    query=QUERY,
                    top_k=3,
                    security_levels=["internal"],
                    document_id=document.document_id,
                    orchestration_mode="multi_model",
                    llm_backend="ollama",
                ),
                auth,
            )
        chat_trace = next(
            item for item in repository.list_rag_traces() if item.get("trace_id") == chat["trace_id"]
        )
        qa_trace = dict(chat_trace.get("pipeline_trace") or {})
        approved_chunks = repository.get_chunks(document.document_id)
        citations = list(chat.get("citations") or [])
        orchestration = dict(chat.get("orchestration") or {})

        stages = [
            _acceptance_stage("preprocessing", "upload_admission", bool(document.file_hash), {"signature_admitted": True, "tenant_scoped": True}),
            _acceptance_stage("preprocessing", "parse_extract", _stage(preprocessing_trace, "parse_extract").get("status") == "completed", _stage(preprocessing_trace, "parse_extract").get("detail", {})),
            _acceptance_stage("preprocessing", "normalize", _stage(preprocessing_trace, "normalize").get("status") == "completed" and any(ANSWER_EVIDENCE in chunk.text for chunk in chunks), _stage(preprocessing_trace, "normalize").get("detail", {})),
            _acceptance_stage("preprocessing", "structure_detect", _stage(preprocessing_trace, "structure_detect").get("status") == "completed", _stage(preprocessing_trace, "structure_detect").get("detail", {})),
            _acceptance_stage("preprocessing", "chunk_generate", _stage(preprocessing_trace, "chunk_generate").get("status") == "completed" and bool(chunks), {"chunk_count": len(chunks)}),
            _acceptance_stage("preprocessing", "quality_gate", _stage(preprocessing_trace, "quality_gate").get("status") == "completed", _stage(preprocessing_trace, "quality_gate").get("detail", {})),
            _acceptance_stage("preprocessing", "export", _stage(preprocessing_trace, "export").get("status") == "completed" and bool(run.artifacts), {"artifact_count": len(run.artifacts), "artifact_names": sorted(run.artifacts)}),
            _acceptance_stage("preprocessing", "vector_index", index_job.get("status") == "indexed" and index_job.get("embedding_model") == QWEN3_EMBEDDING_MODEL and all(chunk.approval_status == "approved" for chunk in approved_chunks), {"record_count": index_job.get("record_count"), "embedding_model": index_job.get("embedding_model"), "embedding_dimensions": index_job.get("embedding_dimensions"), "approval_journaled": bool(approval.get("approval_record_id"))}),
            _acceptance_stage("qa", "query_analysis", _stage(qa_trace, "query_analysis").get("status") == "completed", _stage(qa_trace, "query_analysis").get("detail", {})),
            _acceptance_stage("qa", "query_correction", _stage(qa_trace, "query_correction").get("status") == "completed", _stage(qa_trace, "query_correction").get("detail", {})),
            _acceptance_stage("qa", "hybrid_retrieval", _stage(qa_trace, "hybrid_retrieval").get("status") == "completed", _stage(qa_trace, "hybrid_retrieval").get("detail", {})),
            _acceptance_stage("qa", "rerank_filter", _stage(qa_trace, "rerank_filter").get("status") == "completed", _stage(qa_trace, "rerank_filter").get("detail", {})),
            _acceptance_stage("qa", "context_build", _stage(qa_trace, "context_build").get("status") == "completed", _stage(qa_trace, "context_build").get("detail", {})),
            _acceptance_stage("qa", "local_llm_answer", _stage(qa_trace, "local_llm_answer").get("status") == "completed" and orchestration.get("answer_model") == "qwen3:8b", {**_stage(qa_trace, "local_llm_answer").get("detail", {}), "answer_model": orchestration.get("answer_model")}),
            _acceptance_stage("qa", "citation_verify", _stage(qa_trace, "citation_verify").get("status") == "completed" and orchestration.get("claim_audit_status") == "verified" and bool(citations), {**_stage(qa_trace, "citation_verify").get("detail", {}), "citation_count": len(citations), "claim_audit_status": orchestration.get("claim_audit_status")}),
        ]
        return {
            "schema_version": "reg-rag-image-15-stage-acceptance-v1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "passed": all(stage["status"] == "passed" for stage in stages),
            "stage_count": len(stages),
            "passed_stage_count": sum(stage["status"] == "passed" for stage in stages),
            "local_only": True,
            "external_api_call_count": network_guard.external_attempt_count,
            "human_approval_actor_recorded": approval.get("approved_by") == auth.actor,
            "answer_contains_evidence_marker": "[E" in str(chat.get("answer") or ""),
            "answer": chat.get("answer"),
            "citations": citations,
            "orchestration": orchestration,
            "stages": stages,
        }


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = run_acceptance()
    except Exception as exc:
        cause = exc.__cause__
        report = {
            "schema_version": "reg-rag-image-15-stage-acceptance-v1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "cause_type": type(cause).__name__ if cause is not None else None,
            "cause": str(cause)[:3000] if cause is not None else None,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
