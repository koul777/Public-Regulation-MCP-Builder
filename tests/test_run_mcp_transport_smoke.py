from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_mcp_smoke import run_mcp_smoke
from scripts.run_mcp_transport_smoke import (
    TEMPORAL_AS_OF_CASES,
    _call_profile_tools,
    _validate_temporal_case_payloads,
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
        self.assertTrue(report["as_of_date_verification_passed"])
        self.assertTrue(report["full_profile"]["as_of_date_verification_passed"])
        temporal = report["full_profile"]["as_of_date_verification"]
        self.assertTrue(temporal["attempted"])
        self.assertTrue(temporal["passed"], temporal["error"])
        self.assertEqual(["2025-06-01", "2026-06-01"], temporal["as_of_dates"])
        self.assertTrue(temporal["selected_document_ids_differ"])
        self.assertTrue(temporal["history_versions_differ"])
        self.assertTrue(temporal["toc_versions_differ"])
        self.assertTrue(temporal["article_document_ids_differ"])
        self.assertTrue(temporal["article_versions_differ"])
        self.assertTrue(temporal["article_texts_differ"])
        self.assertEqual(
            ["doc_mcp_smoke_v1", "doc_mcp_smoke_v2"],
            [case["history_current_document_id"] for case in temporal["cases"]],
        )
        self.assertEqual(
            ["doc_mcp_smoke_v1", "doc_mcp_smoke_v2"],
            [case["article_document_id"] for case in temporal["cases"]],
        )
        self.assertEqual(
            ["1.0", "2.0"],
            [case["article_regulation_version"] for case in temporal["cases"]],
        )
        for case in temporal["cases"]:
            self.assertTrue(case["passed"], case["errors"])
            for field in ("history_elapsed_ms", "toc_elapsed_ms", "article_elapsed_ms"):
                self.assertGreaterEqual(case[field], 0.0)
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
        self.assertTrue(report["full_profile"]["as_of_date_verification_passed"])
        self.assertTrue(report["full_profile"]["as_of_date_verification"]["passed"])
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
        self.assertIsNone(profile["as_of_date_verification_passed"])
        self.assertFalse(profile["as_of_date_verification"]["applicable"])
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
        self.assertTrue(all("as_of_date" not in arguments for _, arguments in session.calls))

    def test_full_profile_temporal_verification_uses_exact_article_identity(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def list_tools(self):
                names = [
                    "fetch",
                    "get_index_status",
                    "get_regulation_article",
                    "get_regulation_history",
                    "get_regulation_references",
                    "get_regulation_toc",
                    "list_documents",
                    "list_regulation_reference_cycles",
                    "list_regulations",
                    "search",
                ]
                return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

            async def call_tool(self, name, arguments):
                self.calls.append((name, dict(arguments)))
                if name == "list_regulations":
                    return SimpleNamespace(
                        structuredContent={
                            "regulations": [
                                {
                                    "regulation_unit_id": "unit-right",
                                    "regulation_title": "MCP Smoke Regulation",
                                }
                            ],
                            "total_count": 1,
                        }
                    )
                if name == "get_regulation_toc":
                    as_of_date = str(arguments.get("as_of_date") or "")
                    if not as_of_date:
                        return SimpleNamespace(
                            structuredContent={
                                "regulation": {"regulation_unit_id": "unit-right"},
                                "nodes": [{"node_type": "article", "number": "1"}],
                            }
                        )
                    case_spec = next(case for case in TEMPORAL_AS_OF_CASES if case["as_of_date"] == as_of_date)
                    return SimpleNamespace(
                        structuredContent={
                            "regulation": {
                                "regulation_unit_id": "unit-right",
                                "version": case_spec["expected_regulation_version"],
                                "effective_from": case_spec["expected_effective_from"],
                                "effective_to": case_spec["expected_effective_to"],
                            },
                            "nodes": [{"node_type": "article", "number": "1"}],
                            "metadata": {
                                "hierarchical_index_ready": True,
                                "as_of_date": as_of_date,
                            },
                        }
                    )
                if name == "get_regulation_article":
                    as_of_date = str(arguments.get("as_of_date") or "")
                    if not as_of_date:
                        return SimpleNamespace(
                            structuredContent={
                                "regulation_unit_id": "unit-right",
                                "article_no": "1",
                                "articles": [
                                    {
                                        "text": "current article",
                                        "metadata": {
                                            "regulation_id": "reg_mcp_smoke",
                                            "document_id": "doc_mcp_smoke_v2",
                                        },
                                        "verbatim": {
                                            "regulation_id": "reg_mcp_smoke",
                                            "document_id": "doc_mcp_smoke_v2",
                                        },
                                    }
                                ],
                            }
                        )
                    case_spec = next(case for case in TEMPORAL_AS_OF_CASES if case["as_of_date"] == as_of_date)
                    article_text = f"Article 1 {case_spec['label']} text"
                    articles = []
                    for chunk_suffix in ("a", "b"):
                        articles.append(
                            {
                                "text": article_text,
                                "verbatim_text": article_text,
                                "verbatim": {
                                    "text": article_text,
                                    "document_id": case_spec["expected_document_id"],
                                },
                                "metadata": {
                                    "document_id": case_spec["expected_document_id"],
                                    "regulation_version": case_spec["expected_regulation_version"],
                                    "effective_from": case_spec["expected_effective_from"],
                                    "effective_to": case_spec["expected_effective_to"],
                                    "chunk_id": f"chunk-{chunk_suffix}",
                                },
                            }
                        )
                    return SimpleNamespace(
                        structuredContent={
                            "regulation_unit_id": "unit-right",
                            "article_no": "1",
                            "as_of_date": as_of_date,
                            "articles": articles,
                        }
                    )
                if name == "get_regulation_history":
                    case_spec = next(
                        case for case in TEMPORAL_AS_OF_CASES if case["as_of_date"] == arguments["as_of_date"]
                    )
                    return SimpleNamespace(
                        structuredContent={
                            "regulation_id": "reg_mcp_smoke",
                            "as_of_date": arguments["as_of_date"],
                            "current_document_id": case_spec["expected_document_id"],
                            "versions": [
                                {
                                    "document_id": case_spec["expected_document_id"],
                                    "regulation_version": case_spec["expected_regulation_version"],
                                    "effective_from": case_spec["expected_effective_from"],
                                    "effective_to": case_spec["expected_effective_to"],
                                    "is_current": True,
                                    "is_effective_on_as_of": True,
                                }
                            ],
                        }
                    )
                if name == "get_regulation_references":
                    return SimpleNamespace(
                        structuredContent={
                            "regulation": {"regulation_unit_id": "unit-right"},
                            "references": [],
                            "cycles": [],
                            "total_count": 0,
                            "page": 1,
                            "page_size": 50,
                            "next_cursor": None,
                            "metadata": {
                                "hierarchical_index_ready": True,
                                "direction": "both",
                                "cycle_count_for_regulation": 0,
                            },
                        }
                    )
                if name == "list_regulation_reference_cycles":
                    return SimpleNamespace(
                        structuredContent={
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
                        }
                    )
                if name == "get_index_status":
                    return SimpleNamespace(structuredContent={"summary": {"approved_record_count": 2}})
                if name == "search":
                    return SimpleNamespace(
                        structuredContent={
                            "results": [
                                {
                                    "id": "result-wrong",
                                    "metadata": {
                                        "regulation_id": "reg-wrong",
                                        "document_id": "doc-wrong",
                                    },
                                }
                            ],
                            "metadata": {},
                        }
                    )
                if name == "fetch":
                    return SimpleNamespace(structuredContent={"text": "approved text"})
                raise AssertionError(f"Unexpected tool call: {name}")

        session = FakeSession()

        profile = asyncio.run(
            _call_profile_tools(
                session,
                tool_profile="full",
                profile_id=None,
                query="Article",
                no_warm_cache=True,
                profile_started_at=0.0,
            )
        )

        self.assertTrue(profile["passed"], profile["history_error"])
        self.assertTrue(profile["as_of_date_verification_passed"])
        self.assertEqual("doc_mcp_smoke_v2", profile["history_current_document_id"])
        self.assertEqual("doc_mcp_smoke_v2", profile["history_current_match_target_document_id"])
        history_calls = [arguments for tool_name, arguments in session.calls if tool_name == "get_regulation_history"]
        self.assertEqual(2, len(history_calls))
        self.assertTrue(all(arguments["regulation_id"] == "reg_mcp_smoke" for arguments in history_calls))

    def test_temporal_payload_validation_accepts_multiple_article_entries(self) -> None:
        case_spec = TEMPORAL_AS_OF_CASES[0]
        regulation_id = "reg_mcp_smoke"
        regulation_unit_id = "stable-unit-id"
        article_no = "1"
        article_text = "Article 1 synthetic historical text"

        result = _validate_temporal_case_payloads(
            case_spec=case_spec,
            regulation_id=regulation_id,
            regulation_unit_id=regulation_unit_id,
            article_no=article_no,
            history_payload={
                "regulation_id": regulation_id,
                "as_of_date": case_spec["as_of_date"],
                "current_document_id": case_spec["expected_document_id"],
                "versions": [
                    {
                        "document_id": case_spec["expected_document_id"],
                        "regulation_version": case_spec["expected_regulation_version"],
                        "effective_from": case_spec["expected_effective_from"],
                        "effective_to": case_spec["expected_effective_to"],
                        "is_current": True,
                        "is_effective_on_as_of": True,
                    }
                ],
            },
            toc_payload={
                "regulation": {
                    "regulation_unit_id": regulation_unit_id,
                    "version": "rev-20250101",
                    "effective_from": case_spec["expected_effective_from"],
                    "effective_to": case_spec["expected_effective_to"],
                },
                "nodes": [{"node_type": "article", "number": article_no}],
                "metadata": {
                    "hierarchical_index_ready": True,
                    "as_of_date": case_spec["as_of_date"],
                },
            },
            article_payload={
                "regulation_unit_id": regulation_unit_id,
                "article_no": article_no,
                "as_of_date": case_spec["as_of_date"],
                "articles": [
                    {
                        "text": article_text,
                        "verbatim_text": article_text,
                        "verbatim": {
                            "text": article_text,
                            "document_id": case_spec["expected_document_id"],
                        },
                        "metadata": {
                            "document_id": case_spec["expected_document_id"],
                            "regulation_version": case_spec["expected_regulation_version"],
                            "effective_from": case_spec["expected_effective_from"],
                            "effective_to": case_spec["expected_effective_to"],
                            "chunk_id": "chunk-a",
                        },
                    },
                    {
                        "text": article_text,
                        "verbatim_text": article_text,
                        "verbatim": {
                            "text": article_text,
                            "document_id": case_spec["expected_document_id"],
                        },
                        "metadata": {
                            "document_id": case_spec["expected_document_id"],
                            "regulation_version": case_spec["expected_regulation_version"],
                            "effective_from": case_spec["expected_effective_from"],
                            "effective_to": case_spec["expected_effective_to"],
                            "chunk_id": "chunk-b",
                        },
                    },
                ],
            },
        )

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(case_spec["expected_document_id"], result["article_document_id"])

    def test_temporal_payload_validation_rejects_empty_and_malformed_responses(self) -> None:
        case_spec = TEMPORAL_AS_OF_CASES[0]
        regulation_id = "reg_mcp_smoke"
        regulation_unit_id = "stable-unit-id"
        article_no = "1"
        history_payload = {
            "regulation_id": regulation_id,
            "as_of_date": case_spec["as_of_date"],
            "current_document_id": case_spec["expected_document_id"],
            "versions": [
                {
                    "document_id": case_spec["expected_document_id"],
                    "regulation_version": case_spec["expected_regulation_version"],
                    "effective_from": case_spec["expected_effective_from"],
                    "effective_to": case_spec["expected_effective_to"],
                    "is_current": True,
                    "is_effective_on_as_of": True,
                }
            ],
        }
        toc_payload = {
            "regulation": {
                "regulation_unit_id": regulation_unit_id,
                "version": "rev-20250101",
                "effective_from": case_spec["expected_effective_from"],
                "effective_to": case_spec["expected_effective_to"],
            },
            "nodes": [{"node_type": "article", "number": article_no}],
            "metadata": {
                "hierarchical_index_ready": True,
                "as_of_date": case_spec["as_of_date"],
            },
        }
        article_text = "Article 1 synthetic historical text"
        article_payload = {
            "regulation_unit_id": regulation_unit_id,
            "article_no": article_no,
            "as_of_date": case_spec["as_of_date"],
            "articles": [
                {
                    "text": article_text,
                    "verbatim_text": article_text,
                    "verbatim": {
                        "text": article_text,
                        "document_id": case_spec["expected_document_id"],
                    },
                    "metadata": {
                        "document_id": case_spec["expected_document_id"],
                        "regulation_version": case_spec["expected_regulation_version"],
                        "effective_from": case_spec["expected_effective_from"],
                        "effective_to": case_spec["expected_effective_to"],
                    },
                }
            ],
        }

        valid = _validate_temporal_case_payloads(
            case_spec=case_spec,
            regulation_id=regulation_id,
            regulation_unit_id=regulation_unit_id,
            article_no=article_no,
            history_payload=history_payload,
            toc_payload=toc_payload,
            article_payload=article_payload,
        )
        self.assertTrue(valid["passed"], valid["errors"])
        self.assertTrue(valid["article_text_sha256"])

        empty = _validate_temporal_case_payloads(
            case_spec=case_spec,
            regulation_id=regulation_id,
            regulation_unit_id=regulation_unit_id,
            article_no=article_no,
            history_payload={},
            toc_payload={},
            article_payload={},
        )
        self.assertFalse(empty["passed"])
        self.assertEqual(3, len(empty["errors"]))

        malformed_payloads = (
            ({"versions": {}}, toc_payload, article_payload, "history_verified"),
            (history_payload, {"regulation": [], "nodes": {}}, article_payload, "toc_verified"),
            (history_payload, toc_payload, {"articles": {}}, "article_verified"),
        )
        for malformed_history, malformed_toc, malformed_article, failed_field in malformed_payloads:
            with self.subTest(failed_field=failed_field):
                result = _validate_temporal_case_payloads(
                    case_spec=case_spec,
                    regulation_id=regulation_id,
                    regulation_unit_id=regulation_unit_id,
                    article_no=article_no,
                    history_payload=malformed_history,
                    toc_payload=malformed_toc,
                    article_payload=malformed_article,
                )
                self.assertFalse(result["passed"])
                self.assertFalse(result[failed_field])

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
