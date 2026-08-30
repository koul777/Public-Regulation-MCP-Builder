from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import routes_documents, routes_rag
from app.core.api_audit import api_audit_path
from app.core.config import Settings, get_settings
from app.core.input_limits import (
    MAX_IDENTIFIER_CHARS,
    MAX_MCP_ARTICLE_NO_CHARS,
    MAX_MCP_DEPARTMENT_IDS,
    MAX_MCP_IDENTIFIER_CHARS,
    MAX_MCP_PAGE,
    MAX_MCP_PAGE_SIZE,
    MAX_MCP_QUERY_CHARS,
    MAX_MCP_RESULT_ID_CHARS,
    MAX_MCP_SCOPE_VALUE_CHARS,
    MAX_MCP_SECURITY_LEVELS,
    MAX_MCP_TOP_K,
    MAX_METADATA_PATCH_KEYS,
    MAX_REQUEST_JSON_DEPTH,
    MAX_REVIEW_CHUNK_IDS,
    MAX_SHORT_LABEL_CHARS,
    MAX_SPLIT_PARTS,
    validate_json_value_budget,
)
from app.main import app
from app.mcp_server.regulation_server import create_regulation_mcp_server
from app.mcp_server.regulation_tools import get_article, mcp_auth_context, search_regulations


class ReviewApiInputLimitTests(unittest.TestCase):
    def test_approval_model_preserves_large_bulk_review_evidence(self) -> None:
        bulk_count = 6_000

        request = routes_documents.ApprovalRequest(
            chunk_ids=[f"chunk-{index}" for index in range(bulk_count)],
            review_decision_events=[
                {
                    "event": "human_review_confirmed",
                    "actor": "reviewer",
                    "chunk_id": f"chunk-{index}",
                    "timestamp": "2026-07-13T00:00:00+00:00",
                }
                for index in range(bulk_count)
            ],
        )

        self.assertEqual(bulk_count, len(request.chunk_ids))
        self.assertEqual(bulk_count, len(request.review_decision_events))

    def test_review_models_reject_oversized_or_deep_collections(self) -> None:
        with self.assertRaises(ValidationError):
            routes_documents.SplitChunkRequest(texts=["part"] * (MAX_SPLIT_PARTS + 1))

        with self.assertRaises(ValidationError):
            routes_documents.ReviewChunkUpdateRequest(
                metadata_patch={f"key-{index}": index for index in range(MAX_METADATA_PATCH_KEYS + 1)}
            )

        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(MAX_REQUEST_JSON_DEPTH + 1):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        with self.assertRaises(ValidationError):
            routes_documents.ApprovalRequest(review_decision_events=[nested])

    def test_nested_json_budget_rejects_nonfinite_numbers_and_nonstring_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validate_json_value_budget(
                {"score": float("nan")},
                field_name="metadata",
                max_items=10,
                max_text_chars=100,
            )
        with self.assertRaisesRegex(ValueError, "keys must be strings"):
            validate_json_value_budget(
                {1: "value"},
                field_name="metadata",
                max_items=10,
                max_text_chars=100,
            )

    def test_authenticated_review_and_index_endpoints_return_422_before_service_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                api_auth_required=True,
                api_auth_token="secret",
                api_default_tenant_id="tenant-a",
            )
            headers = {
                "Authorization": "Bearer secret",
                "X-Actor": "input-limit-test",
                "X-Tenant-Id": "tenant-a",
            }
            app.dependency_overrides[get_settings] = lambda: settings
            try:
                client = TestClient(app)
                approval_response = client.post(
                    "/api/documents/not-loaded/review/approve",
                    headers=headers,
                    json={"chunk_ids": [f"chunk-{index}" for index in range(MAX_REVIEW_CHUNK_IDS + 1)]},
                )
                index_response = client.post(
                    "/api/documents/not-loaded/index",
                    headers=headers,
                    json={"collection_name": "c" * (MAX_SHORT_LABEL_CHARS + 1)},
                )
            finally:
                app.dependency_overrides.clear()

        self.assertEqual(422, approval_response.status_code)
        self.assertEqual("chunk_ids", approval_response.json()["detail"][0]["loc"][-1])
        self.assertEqual(422, index_response.status_code)
        self.assertEqual("collection_name", index_response.json()["detail"][0]["loc"][-1])


class McpInputLimitTests(unittest.TestCase):
    def test_mcp_tool_schemas_publish_string_list_and_integer_bounds(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="input-limit-test",
            warm_cache=False,
        )
        tools = server._tool_manager._tools
        search_properties = tools["search"].parameters["properties"]
        fetch_properties = tools["fetch"].parameters["properties"]
        article_properties = tools["get_article"].parameters["properties"]
        catalog_properties = tools["list_regulations"].parameters["properties"]

        self.assertEqual(
            {
                "minLength": 1,
                "maxLength": MAX_MCP_QUERY_CHARS,
                "title": "Query",
                "type": "string",
            },
            search_properties["query"],
        )
        self.assertEqual(
            {
                "default": 5,
                "minimum": 1,
                "maximum": MAX_MCP_TOP_K,
                "title": "Top K",
                "type": "integer",
            },
            search_properties["top_k"],
        )
        expected_scope_item = {
            "minLength": 1,
            "maxLength": MAX_MCP_SCOPE_VALUE_CHARS,
            "type": "string",
        }
        self.assertEqual(
            {
                "anyOf": [
                    {
                        "items": expected_scope_item,
                        "maxItems": MAX_MCP_SECURITY_LEVELS,
                        "type": "array",
                    },
                    {"type": "null"},
                ],
                "default": None,
                "title": "Security Levels",
            },
            search_properties["security_levels"],
        )
        self.assertEqual(
            {
                "anyOf": [
                    {
                        "items": expected_scope_item,
                        "maxItems": MAX_MCP_DEPARTMENT_IDS,
                        "type": "array",
                    },
                    {"type": "null"},
                ],
                "default": None,
                "title": "Department Ids",
            },
            search_properties["department_ids"],
        )
        self.assertEqual(
            {
                "minLength": 1,
                "maxLength": MAX_MCP_RESULT_ID_CHARS,
                "title": "Id",
                "type": "string",
            },
            fetch_properties["id"],
        )
        self.assertEqual(
            {
                "minLength": 1,
                "maxLength": MAX_MCP_IDENTIFIER_CHARS,
                "title": "Document Id",
                "type": "string",
            },
            article_properties["document_id"],
        )
        self.assertEqual(
            {
                "minLength": 1,
                "maxLength": MAX_MCP_ARTICLE_NO_CHARS,
                "title": "Article No",
                "type": "string",
            },
            article_properties["article_no"],
        )
        self.assertEqual(
            {
                "anyOf": [
                    {"maxLength": MAX_MCP_IDENTIFIER_CHARS, "type": "string"},
                    {"type": "null"},
                ],
                "default": None,
                "title": "Profile Id",
            },
            search_properties["profile_id"],
        )
        self.assertEqual(
            {
                "default": 1,
                "minimum": 1,
                "maximum": MAX_MCP_PAGE,
                "title": "Page",
                "type": "integer",
            },
            catalog_properties["page"],
        )
        self.assertEqual(
            {
                "default": 50,
                "minimum": 1,
                "maximum": MAX_MCP_PAGE_SIZE,
                "title": "Page Size",
                "type": "integer",
            },
            catalog_properties["page_size"],
        )

    def test_mcp_protocol_validation_rejects_oversized_query_before_tool_function(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="input-limit-test",
            warm_cache=False,
        )
        search_tool = server._tool_manager._tools["search"]

        with self.assertRaises(Exception) as raised:
            asyncio.run(search_tool.run({"query": "q" * (MAX_MCP_QUERY_CHARS + 1)}))

        self.assertIn("at most", str(raised.exception))
        self.assertIn(str(MAX_MCP_QUERY_CHARS), str(raised.exception))

    def test_direct_mcp_service_rejects_oversized_inputs_before_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                department_ids=["department-a"],
            )
            with patch.object(
                routes_rag,
                "search_rag_records",
                side_effect=AssertionError("oversized query must not reach retrieval"),
            ):
                with self.assertRaises(ValueError):
                    search_regulations(
                        settings=settings,
                        auth=auth,
                        query="q" * (MAX_MCP_QUERY_CHARS + 1),
                    )
            audit_rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            ]

            with patch(
                "app.mcp_server.regulation_tools._visible_records",
                side_effect=AssertionError("oversized article input must not scan records"),
            ):
                with self.assertRaises(ValueError):
                    get_article(
                        settings=settings,
                        auth=auth,
                        document_id="d" * (MAX_IDENTIFIER_CHARS + 1),
                        article_no="1",
                    )

        self.assertEqual(400, audit_rows[-1]["status_code"])
        self.assertEqual("failure", audit_rows[-1]["outcome"])

    def test_direct_mcp_service_accepts_query_at_documented_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                department_ids=["department-a"],
            )
            with patch(
                "app.mcp_server.regulation_tools.routes_rag.search_records",
                return_value=([], {"trace_id": "trace-boundary", "timing_ms": {}}),
            ) as search_records:
                response = search_regulations(
                    settings=settings,
                    auth=auth,
                    query="q" * MAX_MCP_QUERY_CHARS,
                    department_ids=["department-a"] * MAX_MCP_DEPARTMENT_IDS,
                )

        self.assertEqual([], response["results"])
        self.assertEqual("trace-boundary", response["metadata"]["trace_id"])
        search_records.assert_called_once()


if __name__ == "__main__":
    unittest.main()
