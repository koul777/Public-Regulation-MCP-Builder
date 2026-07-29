from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_mcp_smoke import run_mcp_smoke
from scripts.run_mcp_transport_smoke import (
    _call_profile_tools,
    _valid_reference_cycle_payload,
    _valid_reference_lookup_payload,
    build_parser,
    run_mcp_transport_smoke,
)


class RunMcpTransportSmokeTests(unittest.TestCase):
    def test_cli_accepts_bearer_token_environment_selector(self) -> None:
        args = build_parser().parse_args(["--transport", "streamable-http", "--http-bearer-token-env", "MCP_TOKEN"])

        self.assertEqual(args.http_bearer_token_env, "MCP_TOKEN")

    def test_run_mcp_transport_smoke_passes_with_synthetic_data(self) -> None:
        report = run_mcp_transport_smoke(
            tenant_id="tenant-mcp-transport-smoke",
            tenant_storage_isolation=True,
            no_warm_cache=True,
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["transport"], "stdio")
        self.assertEqual("Article", report["query"])
        self.assertTrue(report["no_warm_cache"])
        self.assertEqual("temporary", report["preparation"]["data_dir_mode"])
        self.assertTrue(report["preparation"]["synthetic_runtime"])
        self.assertFalse(report["preparation"]["handoff_evidence"])
        self.assertGreaterEqual(report["full_profile"]["search_result_count"], 1)
        self.assertGreaterEqual(report["full_profile"]["warm_search_result_count"], 1)
        self.assertTrue(report["full_profile"]["fetch_has_text"])
        self.assertEqual(
            set(report["chatgpt_data_profile"]["tool_names"]),
            {
                "search",
                "fetch",
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
                "get_regulation_references",
                "list_regulation_reference_cycles",
            },
        )
        self.assertTrue(report["chatgpt_data_profile"]["catalog_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["hierarchy_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["exact_article_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["reference_lookup_attempted"])
        self.assertTrue(report["chatgpt_data_profile"]["reference_lookup_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["reference_cycle_lookup_attempted"])
        self.assertTrue(report["chatgpt_data_profile"]["reference_cycle_lookup_verified"])
        self.assertIsInstance(report["full_profile"]["search_metadata"].get("timing_ms"), dict)
        for profile_name in ("full_profile", "chatgpt_data_profile"):
            profile = report[profile_name]
            self.assertEqual("Article", profile["query"])
            self.assertTrue(profile["no_warm_cache"])
            for field in (
                "list_tools_elapsed_ms",
                "search_elapsed_ms",
                "fetch_elapsed_ms",
                "warm_search_elapsed_ms",
                "total_elapsed_ms",
            ):
                self.assertIn(field, profile)
                self.assertGreaterEqual(profile[field], 0.0)
            self.assertIn("reference_lookup_elapsed_ms", profile)
            self.assertIn("reference_cycle_lookup_elapsed_ms", profile)
            self.assertGreaterEqual(profile["reference_lookup_elapsed_ms"], 0.0)
            self.assertGreaterEqual(profile["reference_cycle_lookup_elapsed_ms"], 0.0)

    def test_streamable_http_transport_smoke_passes_with_synthetic_data(self) -> None:
        report = run_mcp_transport_smoke(
            tenant_id="tenant-mcp-streamable-http-smoke",
            tenant_storage_isolation=True,
            transport="streamable-http",
            no_warm_cache=True,
            timeout_seconds=30.0,
        )

        self.assertTrue(report["passed"], report.get("error"))
        self.assertEqual("streamable-http", report["transport"])
        self.assertEqual("127.0.0.1", report["host"])
        self.assertGreaterEqual(report["full_profile"]["search_result_count"], 1)
        self.assertGreaterEqual(report["full_profile"]["warm_search_result_count"], 1)
        self.assertTrue(report["full_profile"]["fetch_has_text"])
        self.assertTrue(report["full_profile"]["session_id_present"])
        self.assertEqual(
            set(report["chatgpt_data_profile"]["tool_names"]),
            {
                "search",
                "fetch",
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
                "get_regulation_references",
                "list_regulation_reference_cycles",
            },
        )
        self.assertTrue(report["chatgpt_data_profile"]["catalog_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["hierarchy_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["exact_article_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["reference_lookup_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["reference_cycle_lookup_verified"])
        self.assertTrue(report["chatgpt_data_profile"]["session_id_present"])

    def test_authenticated_streamable_http_transport_verifies_bearer_wire(self) -> None:
        report = run_mcp_transport_smoke(
            tenant_id="tenant-mcp-authenticated-http-smoke",
            tenant_storage_isolation=True,
            transport="streamable-http",
            http_bearer_token="smoke-token",
            no_warm_cache=True,
            timeout_seconds=30.0,
        )

        self.assertTrue(report["passed"], report.get("error"))
        self.assertEqual(report["http_auth"], {"configured": True, "wire_verified": True})
        self.assertTrue(report["full_profile"]["auth_wire_verified"])
        self.assertTrue(report["full_profile"]["fetch_has_text"])

    def test_skip_preparation_does_not_seed_existing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            tenant_id = "tenant-mcp-transport-smoke"
            run_mcp_smoke(
                data_dir=data_dir,
                tenant_id=tenant_id,
                tenant_storage_isolation=True,
                allow_persistent_smoke_data=True,
            )
            vector_path = (
                data_dir
                / "tenants"
                / tenant_id
                / "vector_db"
                / tenant_id
                / "approved_vectors.jsonl"
            )
            before = vector_path.read_text(encoding="utf-8")

            report = run_mcp_transport_smoke(
                data_dir=data_dir,
                tenant_id=tenant_id,
                tenant_storage_isolation=True,
                prepare=False,
            )
            after = vector_path.read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertTrue(report["preparation"]["skipped"])
        self.assertEqual(before, after)

    def test_explicit_runtime_with_preparation_requires_persistent_smoke_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            report = run_mcp_transport_smoke(
                data_dir=data_dir,
                tenant_id="tenant-mcp-transport-smoke",
                tenant_storage_isolation=True,
            )
            vector_path = (
                data_dir
                / "tenants"
                / "tenant-mcp-transport-smoke"
                / "vector_db"
                / "tenant-mcp-transport-smoke"
                / "approved_vectors.jsonl"
            )

        self.assertFalse(report["passed"])
        self.assertEqual("Article", report["query"])
        self.assertEqual("explicit_refused", report["preparation"]["data_dir_mode"])
        self.assertFalse(report["preparation"]["handoff_evidence"])
        self.assertIn("--allow-persistent-smoke-data", report["error"])
        self.assertFalse(vector_path.exists())

    def test_flat_storage_transport_smoke_uses_flat_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            tenant_id = "tenant-mcp-transport-flat-smoke"

            report = run_mcp_transport_smoke(
                data_dir=data_dir,
                tenant_id=tenant_id,
                tenant_storage_isolation=False,
                allow_persistent_smoke_data=True,
            )
            tenant_dir_exists = (data_dir / "tenants").exists()

        self.assertTrue(report["passed"], report.get("error"))
        self.assertFalse(report["tenant_storage_isolation"])
        self.assertFalse(tenant_dir_exists)
        self.assertGreaterEqual(report["full_profile"]["search_result_count"], 1)

    def test_profile_tools_accept_empty_reference_and_cycle_pages(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def list_tools(self):
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(name="fetch"),
                        SimpleNamespace(name="get_regulation_article"),
                        SimpleNamespace(name="get_regulation_references"),
                        SimpleNamespace(name="get_regulation_toc"),
                        SimpleNamespace(name="list_regulation_reference_cycles"),
                        SimpleNamespace(name="list_regulations"),
                        SimpleNamespace(name="search"),
                    ]
                )

            async def call_tool(self, name, arguments):
                self.calls.append((name, dict(arguments)))
                payloads = {
                    "list_regulations": {
                        "regulations": [{"regulation_unit_id": "unit-1"}],
                        "total_count": 1,
                    },
                    "get_regulation_toc": {
                        "regulation": {"regulation_unit_id": "unit-1"},
                        "nodes": [{"node_type": "article", "number": "제1조"}],
                    },
                    "get_regulation_article": {
                        "articles": [{"text": "approved article"}],
                    },
                    "get_regulation_references": {
                        "regulation": {"regulation_unit_id": "unit-1"},
                        "references": [],
                        "cycles": [],
                        "total_count": 0,
                        "page": 1,
                        "page_size": 50,
                        "next_cursor": None,
                        "metadata": {
                            "hierarchical_index_ready": True,
                            "direction": "both",
                            "status": None,
                            "cycle_count_for_regulation": 0,
                        },
                    },
                    "list_regulation_reference_cycles": {
                        "cycles": [],
                        "total_count": 0,
                        "page": 1,
                        "page_size": 50,
                        "next_cursor": None,
                        "metadata": {
                            "hierarchical_index_ready": True,
                            "approved_current_corpus_only": True,
                            "cycle_algorithm": "deterministic_tarjan_scc",
                        },
                    },
                    "search": {
                        "results": [{"id": "result-1"}],
                        "metadata": {},
                    },
                    "fetch": {
                        "text": "approved text",
                    },
                }
                return SimpleNamespace(structuredContent=payloads[name])

        session = FakeSession()

        profile = asyncio.run(
            _call_profile_tools(
                session,
                tool_profile="chatgpt-data",
                profile_id=None,
                query="Article",
                no_warm_cache=True,
                profile_started_at=0.0,
            )
        )

        self.assertTrue(profile["passed"])
        self.assertTrue(profile["reference_lookup_verified"])
        self.assertTrue(profile["reference_cycle_lookup_verified"])
        self.assertEqual(0, profile["reference_lookup_result_count"])
        self.assertEqual(0, profile["reference_lookup_cycle_count"])
        self.assertEqual(0, profile["reference_cycle_result_count"])
        self.assertEqual(
            [
                ("list_regulations", {"page": 1, "page_size": 100}),
                ("get_regulation_toc", {"regulation_unit_id": "unit-1"}),
                ("get_regulation_article", {"regulation_unit_id": "unit-1", "article_no": "제1조"}),
                ("get_regulation_references", {"regulation_unit_id": "unit-1", "page": 1, "page_size": 50}),
                ("list_regulation_reference_cycles", {"regulation_unit_id": "unit-1", "page": 1, "page_size": 50}),
                ("search", {"query": "Article"}),
                ("fetch", {"id": "result-1"}),
                ("search", {"query": "Article"}),
            ],
            session.calls,
        )

    def test_reference_payload_validation_rejects_missing_collection_keys_and_boolean_counts(self) -> None:
        reference_base = {
            "regulation": {"regulation_unit_id": "unit-1"},
            "references": [],
            "cycles": [],
            "total_count": 0,
            "page": 1,
            "page_size": 50,
            "next_cursor": None,
            "metadata": {
                "hierarchical_index_ready": True,
                "direction": "both",
            },
        }
        cycle_base = {
            "cycles": [],
            "total_count": 0,
            "page": 1,
            "page_size": 50,
            "next_cursor": None,
            "metadata": {"hierarchical_index_ready": True},
        }

        for missing_key in ("references", "cycles"):
            with self.subTest(payload="references", missing_key=missing_key):
                invalid = dict(reference_base)
                invalid.pop(missing_key)
                self.assertFalse(_valid_reference_lookup_payload(invalid))

        invalid_cycle = dict(cycle_base)
        invalid_cycle.pop("cycles")
        self.assertFalse(_valid_reference_cycle_payload(invalid_cycle))

        invalid_regulation = dict(reference_base)
        invalid_regulation["regulation"] = {}
        self.assertFalse(_valid_reference_lookup_payload(invalid_regulation))

        for field_name in ("total_count", "page", "page_size"):
            with self.subTest(payload="references", boolean_field=field_name):
                invalid = dict(reference_base)
                invalid[field_name] = True
                self.assertFalse(_valid_reference_lookup_payload(invalid))
            with self.subTest(payload="cycles", boolean_field=field_name):
                invalid = dict(cycle_base)
                invalid[field_name] = True
                self.assertFalse(_valid_reference_cycle_payload(invalid))


if __name__ == "__main__":
    unittest.main()
