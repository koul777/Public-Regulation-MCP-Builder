"""Isolated Streamlit fixture and read-only state probe for the browser journey.

RR_REAL_BEGINNER_ROOT must point to a unique directory inside repository tmp/.
Only the public synthetic DOCX is used. The optional complete MCP journey uses
installed Kordoc and deterministic test embeddings; it never deploys to Vercel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def fixture_root() -> Path:
    value = os.environ.get("RR_REAL_BEGINNER_ROOT", "")
    if not value:
        raise RuntimeError("RR_REAL_BEGINNER_ROOT is required")
    target = Path(value).resolve()
    temporary = (ROOT / "tmp").resolve()
    if not target.is_relative_to(temporary) or target == temporary:
        raise RuntimeError("fixture root must be a unique child of repository tmp")
    target.mkdir(parents=True, exist_ok=True)
    return target


def snapshot() -> None:
    """Print persisted state from a separate process, independent of browser DOM."""
    from app.core.config import Settings
    from app.storage.repository import JsonRepository

    target = fixture_root()
    repository = JsonRepository(Settings(data_dir=target, artifact_root=target))
    documents = []
    for document in repository.list_documents():
        document_id = str(document.document_id)
        chunks = repository.get_chunks(document_id)
        documents.append({
            "document_id": document_id,
            "status": str(document.status),
            "chunks": [{
                "chunk_id": str(chunk.chunk_id),
                "article_no": str((chunk.metadata or {}).get("article_no") or ""),
                "hierarchy_path": str((chunk.metadata or {}).get("hierarchy_path") or ""),
                "approval_status": str(chunk.approval_status),
            } for chunk in chunks],
            "approval_journal_count": len(repository.list_approval_journal_records(document_id)),
            "indexing_jobs": repository.list_indexing_jobs(document_id),
        })
    vector_files = [
        path.relative_to(target).as_posix()
        for path in target.rglob("approved_vectors.jsonl")
    ]
    print(json.dumps({"documents": documents, "vector_files": vector_files}, ensure_ascii=False))


def serve() -> None:
    import streamlit as st
    from app.services.synthetic_sample_service import build_synthetic_regulation_docx

    target = fixture_root()
    complete_mcp = os.environ.get("RR_COMPLETE_MCP_JOURNEY") == "1"
    if complete_mcp:
        from types import SimpleNamespace
        from app.ingestion import embedding_adapter
        from app.services import vercel_deployment_service
        from scripts import generate_mcp_client_config as mcp_export

        # Fail closed independently of widget/session defaults. This test may only
        # export into its unique synthetic directory, even if the UI resets a field.
        if not getattr(mcp_export.write_mcp_runtime_data_bundle, "_fixture_guarded", False):
            original_export = mcp_export.write_mcp_runtime_data_bundle

            def guarded_export(*args, **kwargs):
                output = Path(kwargs["out_dir"]).resolve()
                if not output.is_relative_to(target):
                    raise AssertionError("Synthetic browser export escaped its fixture directory")
                return original_export(*args, **kwargs)

            guarded_export._fixture_guarded = True
            mcp_export.write_mcp_runtime_data_bundle = guarded_export

        embedding_adapter._qwen_embedding_adapter = lambda *args, **kwargs: SimpleNamespace(
            encode_documents=lambda texts: [[1.0] + [0.0] * 383 for _ in texts],
        )
        vercel_deployment_service.vercel_command = lambda: ["synthetic-unused-vercel"]
        st.session_state.setdefault("ai_usage_path", "mcp")
    sample = target / "synthetic_beginner_regulation.docx"
    if not sample.exists():
        sample.write_bytes(build_synthetic_regulation_docx())
    st.session_state.setdefault("real_beginner_fixture_id", uuid4().hex)
    st.session_state.setdefault("ai_connection_overrides", {
        "data_dir": target,
        "artifact_root": target,
        "institution_profiles_path": "",
        "app_env": "test",
        "api_auth_required": False,
        "tenant_storage_isolation": False,
        "enable_agent_review": False,
        "local_structure_review_enabled": False,
        "enable_kordoc_table_parser": complete_mcp,
        "openai_api_key": "",
        "anthropic_api_key": "",
    })
    runpy.run_path(str(ROOT / "frontend" / "streamlit_app.py"), run_name="__main__")


if __name__ == "__main__" and "--snapshot" in sys.argv:
    snapshot()
else:
    serve()
