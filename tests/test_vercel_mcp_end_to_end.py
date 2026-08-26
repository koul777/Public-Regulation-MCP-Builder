from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.ingestion.vector_adapter import vector_record_from_chunk
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.generate_mcp_client_config import write_mcp_runtime_data_bundle
from scripts.prepare_vercel_mcp_deployment import prepare_vercel_mcp_deployment


class VercelMcpEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_vercel_entrypoint_serves_approved_chatgpt_data_runtime(self) -> None:
        """Exercise the approved-source -> Vercel stage -> ASGI MCP path without tool mocks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_data_dir = root / "approved-source"
            bundle_dir = root / "runtime-bundle"
            stage_dir = root / "vercel-stage"
            source_settings = Settings(
                data_dir=source_data_dir,
                tenant_storage_isolation=True,
            )
            _write_approved_regulation_source(source_settings)

            runtime_manifest = write_mcp_runtime_data_bundle(
                source_data_dir=source_settings.data_dir,
                out_dir=bundle_dir,
                tenant_id="tenant-e2e",
                profile_id="public-regulation-e2e",
                document_id="regulation-e2e-document",
                tenant_storage_isolation=True,
                require_kordoc_table_parser=False,
            )
            self.assertEqual(1, runtime_manifest["record_count"])
            self.assertFalse(runtime_manifest["tenant_storage_isolation"])
            self.assertTrue(runtime_manifest["source_tenant_storage_isolation"])
            self.assertTrue((bundle_dir / "data" / "repository").is_dir())
            self.assertFalse((bundle_dir / "data" / "tenants").exists())
            prepare_vercel_mcp_deployment(
                runtime_data_dir=bundle_dir / "data",
                out_dir=stage_dir,
            )

            app = _load_staged_entrypoint(stage_dir / "api" / "index.py", stage_dir / "mcp_runtime")
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://mcp.e2e.test",
                ) as http_client:
                    refused_stream = await http_client.get(
                        "/mcp", headers={"Accept": "text/event-stream"}
                    )
                    self.assertEqual(405, refused_stream.status_code)
                    self.assertEqual("POST", refused_stream.headers["allow"])

                    async with streamable_http_client(
                        "http://mcp.e2e.test/mcp",
                        http_client=http_client,
                    ) as (read, write, _get_session_id):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual(
                                [
                                    "fetch",
                                    "get_regulation_article",
                                    "get_regulation_references",
                                    "get_regulation_toc",
                                    "list_regulation_reference_cycles",
                                    "list_regulations",
                                    "search",
                                ],
                                sorted(tool.name for tool in tools.tools),
                            )

                            catalog = _tool_payload(
                                await session.call_tool("list_regulations", {"page": 1, "page_size": 10})
                            )
                            self.assertEqual(1, catalog["total_count"])
                            self.assertEqual(1, len(catalog["regulations"]))
                            self.assertEqual("종단검증 규정", catalog["regulations"][0]["regulation_title"])

                            search = _tool_payload(
                                await session.call_tool("search", {"query": "종단고유어"})
                            )
                            self.assertEqual(1, len(search["results"]))
                            search_result = search["results"][0]
                            self.assertEqual("https://example.test/regulations/e2e", search_result["url"])

                            fetched = _tool_payload(
                                await session.call_tool("fetch", {"id": search_result["id"]})
                            )
                            self.assertIn("종단고유어", fetched["text"])
                            self.assertEqual("https://example.test/regulations/e2e", fetched["url"])
                            self.assertEqual(
                                "https://example.test/regulations/e2e",
                                fetched["metadata"]["source_url"],
                            )


def _load_staged_entrypoint(entrypoint: Path, runtime_data_dir: Path):
    spec = importlib.util.spec_from_file_location("staged_vercel_mcp_entrypoint", entrypoint)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load staged Vercel entrypoint: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    environment = {
        "MCP_DATA_DIR": str(runtime_data_dir),
        "MCP_ALLOW_UNAUTHENTICATED_HTTP": "true",
        "MCP_ALLOWED_HTTP_HOSTS": "mcp.e2e.test",
        "MCP_TOOL_PROFILE": "chatgpt-data",
        "MCP_WARM_CACHE": "false",
        "MCP_AUTH_TOKEN": "",
    }
    with patch.dict(os.environ, environment, clear=False):
        # The staged app must derive these bindings from its sealed runtime
        # manifest, even when the developer shell normally defines overrides.
        os.environ.pop("MCP_TENANT_ID", None)
        os.environ.pop("MCP_PROFILE_ID", None)
        spec.loader.exec_module(module)
    return module.app


def _write_approved_regulation_source(base_settings: Settings) -> None:
    tenant_settings = settings_for_tenant(base_settings, "tenant-e2e")
    repository = JsonRepository(tenant_settings)
    document = Document(
        document_id="regulation-e2e-document",
        filename="e2e-regulation.txt",
        document_name="종단검증 규정",
        file_type="txt",
        file_hash="e2e-source-hash",
        institution_name="종단 검증 기관",
        source_system="PUBLIC_PORTAL",
        source_url="https://example.test/regulations/e2e",
        profile_id="public-regulation-e2e",
        regulation_id="regulation-e2e",
        regulation_version="2026.1",
        effective_from="2026-01-01",
        regulation_status="approved",
        tenant_id="tenant-e2e",
        status="completed",
    )
    text = "제1조(목적) 이 규정은 종단고유어에 관한 승인된 공개 규정 본문이다."
    metadata = {
        "chunk_id": "regulation-e2e-chunk-1",
        "document_id": document.document_id,
        "tenant_id": "tenant-e2e",
        "approval_status": "approved",
        "approval_id": "approval-e2e",
        "approved_content_hash": "approved-content-hash-e2e",
        "security_level": "internal",
        "institution_name": document.institution_name,
        "source_system": document.source_system,
        "source_url": document.source_url,
        "profile_id": document.profile_id,
        "regulation_id": document.regulation_id,
        "regulation_version": document.regulation_version,
        "effective_from": document.effective_from,
        "regulation_status": document.regulation_status,
        "canonical_regulation_title": "종단검증 규정",
        "canonical_regulation_no": "E2E-1",
        "regulation_title": "종단검증 규정",
        "regulation_no": "E2E-1",
        "article_no": "제1조",
        "article_title": "목적",
        "hierarchy_path": "종단검증 규정 > 제1조 목적",
        "canonical_hierarchy_path": "종단검증 규정 > 제1조 목적",
        "approval_worklist_report_path": "reports/worklist.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/batches.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "batch-e2e",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "operator_manual_review",
    }
    chunk = Chunk(
        chunk_id="regulation-e2e-chunk-1",
        document_id=document.document_id,
        chunk_type="article",
        text=text,
        normalized_text=text,
        retrieval_text=text,
        metadata=metadata,
        approval_status="approved",
        approval_id="approval-e2e",
        approved_content_hash="approved-content-hash-e2e",
        security_level="internal",
    )
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], [chunk], [])
    repository.append_approval_record(
        {
            "approval_record_id": "approval-record-e2e",
            "approval_id": "approval-e2e",
            "document_id": document.document_id,
            "tenant_id": "tenant-e2e",
            "chunk_ids": [chunk.chunk_id],
            "approved_content_hashes": {chunk.chunk_id: "approved-content-hash-e2e"},
            "approved_chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "approved_content_hash": "approved-content-hash-e2e",
                }
            ],
            "approved_by": "e2e-operator",
            "approved_at": "2026-08-01T00:00:00+00:00",
            "worklist_evidence": {
                "worklist_report_path": "reports/worklist.json",
                "worklist_report_sha256": "a" * 64,
                "review_batch_manifest_path": "reports/batches.json",
                "review_batch_manifest_sha256": "b" * 64,
                "review_batch_id": "batch-e2e",
                "review_batch_chunk_fingerprint": "c" * 64,
                "review_strategy": "operator_manual_review",
            },
        }
    )
    vector_path = (
        tenant_settings.data_dir
        / "vector_db"
        / "tenant-e2e"
        / "approved_vectors.jsonl"
    )
    vector_path.parent.mkdir(parents=True, exist_ok=True)
    vector_chunk = chunk.model_dump(mode="json")
    vector_chunk["tenant_id"] = document.tenant_id
    vector_chunk["department_acl"] = []
    vector_path.write_text(
        json.dumps(vector_record_from_chunk(vector_chunk), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _tool_payload(result: object) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", "")
        if text:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                return decoded
    raise AssertionError(f"MCP tool did not return structured JSON: {result!r}")


if __name__ == "__main__":
    unittest.main()
