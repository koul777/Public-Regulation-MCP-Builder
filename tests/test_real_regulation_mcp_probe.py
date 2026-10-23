from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.real_regulation_mcp_probe import (
    PROJECT_ROOT, _normalize_specs, _private_paths, _probe_case, _probe_session, run_mcp_probe,
)
from scripts.run_mcp_smoke import run_mcp_smoke


def tool_result(payload: dict, *, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(structuredContent=payload, isError=is_error, content=[])


class FakeSession:
    def __init__(self, *, results: list | None = None, search_error: bool = False, wrong_fetch_id: bool = False) -> None:
        self.results = results if results is not None else [
            {"id": "result-a", "metadata": {"document_id": "doc-a", "chunk_id": "chunk-a"}},
        ]
        self.search_error = search_error
        self.wrong_fetch_id = wrong_fetch_id
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> SimpleNamespace:
        self.calls.append(("initialize", {}))
        return SimpleNamespace(protocolVersion="2025-11-25")

    async def list_tools(self) -> SimpleNamespace:
        self.calls.append(("tools/list", {}))
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in ("search", "fetch", "get_index_status")])

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        self.calls.append((name, arguments))
        if name == "get_index_status":
            return tool_result({"summary": {"record_count": 1}})
        if name == "search":
            return tool_result({"results": self.results}, is_error=self.search_error)
        if name == "fetch":
            return tool_result({"id": "wrong-id" if self.wrong_fetch_id else arguments["id"], "text": "Synthetic approved evidence."})
        raise AssertionError(name)


def specification(**overrides: object) -> dict:
    return _normalize_specs([{"id": "case-1", "query": "synthetic question", **overrides}])[0]


class RealRegulationMcpProbeTests(unittest.TestCase):
    def test_refuses_public_outputs_and_runtime_writes_before_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            with self.assertRaisesRegex(ValueError, "outside the source"):
                _private_paths(runtime, PROJECT_ROOT / "probe-output.json")
            with self.assertRaisesRegex(ValueError, "outside the runtime"):
                _private_paths(runtime, runtime / "probe-output.json")
            with self.assertRaisesRegex(ValueError, "outside the source"):
                _private_paths(PROJECT_ROOT / "data", Path(tmp) / "probe-output.json")
            output = Path(tmp) / "previous.json"
            output.write_text("existing evidence", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                _private_paths(runtime, output)
            self.assertEqual("existing evidence", output.read_text(encoding="utf-8"))

    def test_rejects_ambiguous_empty_evidence_expectations(self) -> None:
        with self.assertRaisesRegex(ValueError, "boolean"):
            specification(expect_no_evidence="false")
        with self.assertRaisesRegex(ValueError, "cannot also require"):
            specification(expect_no_evidence=True, target_chunk_ids=["chunk-a"])
        with self.assertRaisesRegex(ValueError, "unique"):
            _normalize_specs([specification(), specification()])

    def test_query_request_overrides_are_explicit_and_cannot_conflict(self) -> None:
        spec = specification(request_overrides={"profile_id": "profile-a", "document_id": "doc-a", "top_k": 3})
        self.assertEqual("profile-a", spec["profile_id"])
        self.assertEqual("doc-a", spec["document_id"])
        self.assertEqual(3, spec["top_k"])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            specification(profile_id="profile-a", request_overrides={"profile_id": "profile-b"})
        with self.assertRaisesRegex(ValueError, "supported MCP"):
            specification(request_overrides={"tenant_id": "other-tenant"})

    def test_runs_protocol_before_queries_and_preserves_fetch_scope(self) -> None:
        session = FakeSession()
        spec = specification(target_document_ids=["doc-a"], target_chunk_ids=["chunk-a"],
                             request_overrides={"as_of_date": "2025-01-01", "profile_id": "profile-a", "security_levels": ["internal"]})
        report: dict = {"protocol_checks": {}, "cases": []}
        asyncio.run(_probe_session(session, [spec], report))
        self.assertEqual(["initialize", "tools/list", "get_index_status", "search", "fetch"], [name for name, _ in session.calls])
        self.assertTrue(all(item["passed"] for item in report["protocol_checks"].values()))
        case = report["cases"][0]
        self.assertTrue(case["passed"])
        self.assertEqual(1, case["target_match_rank"])
        self.assertEqual("Synthetic approved evidence.", case["fetch"][0]["payload"]["text"])
        self.assertEqual({"id": "result-a", "as_of_date": "2025-01-01", "profile_id": "profile-a", "security_levels": ["internal"]}, session.calls[-1][1])

    def test_target_document_and_chunk_must_match_the_same_result(self) -> None:
        session = FakeSession(results=[
            {"id": "result-a", "metadata": {"document_id": "doc-a", "chunk_id": "chunk-x"}},
            {"id": "result-b", "metadata": {"document_id": "doc-b", "chunk_id": "chunk-a"}},
        ])
        case = asyncio.run(_probe_case(session, specification(target_document_ids=["doc-a"], target_chunk_ids=["chunk-a"])))
        self.assertFalse(case["passed"])
        self.assertTrue(case["search_protocol_passed"])
        self.assertIn("target_evidence_not_retrieved", case["errors"])

    def test_empty_evidence_passes_only_for_successful_empty_search(self) -> None:
        case = asyncio.run(_probe_case(FakeSession(results=[]), specification(expect_no_evidence=True)))
        self.assertTrue(case["passed"])
        self.assertEqual([], case["fetch"])
        denied = asyncio.run(_probe_case(FakeSession(results=[], search_error=True), specification(expect_no_evidence=True)))
        self.assertFalse(denied["passed"])
        self.assertFalse(denied["search_protocol_passed"])
        unexpected = asyncio.run(_probe_case(FakeSession(), specification(expect_no_evidence=True)))
        self.assertFalse(unexpected["passed"])
        self.assertIn("unexpected_evidence", unexpected["errors"])

    def test_fetch_must_return_the_requested_evidence_identifier(self) -> None:
        case = asyncio.run(_probe_case(FakeSession(wrong_fetch_id=True), specification()))
        self.assertFalse(case["passed"])
        self.assertTrue(case["retrieval_passed"])
        self.assertFalse(case["fetch"][0]["passed"])

    def test_timeout_retains_completed_protocol_evidence_and_fails(self) -> None:
        async def timeout(report: dict, specs: list, stderr: object) -> None:
            report["protocol_checks"]["initialize"] = {"passed": True}
            raise TimeoutError("synthetic timeout")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            output = Path(tmp) / "probe.json"
            with patch("scripts.real_regulation_mcp_probe._run_stdio", side_effect=timeout):
                report = run_mcp_probe(data_dir=runtime, query_specs=[specification()], tenant_id="tenant-a",
                    profile_id=None, tenant_storage_isolation=False, output_path=output)
            self.assertFalse(report["passed"])
            self.assertTrue(report["protocol_checks"]["initialize"]["passed"])
            self.assertFalse(report["protocol_checks"]["tools_list"]["passed"])
            self.assertIn("TimeoutError", report["errors"][0])
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_timeout_during_fetch_retains_the_incomplete_case_and_search_evidence(self) -> None:
        class BlockedFetchSession(FakeSession):
            async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
                if name == "fetch":
                    await asyncio.Event().wait()
                return await super().call_tool(name, arguments)

        report: dict = {"protocol_checks": {}, "cases": []}

        async def cancelled_probe() -> None:
            await asyncio.wait_for(_probe_session(BlockedFetchSession(), [specification()], report), timeout=0.02)

        with self.assertRaises(TimeoutError):
            asyncio.run(cancelled_probe())
        self.assertEqual(1, len(report["cases"]))
        case = report["cases"][0]
        self.assertFalse(case["passed"])
        self.assertFalse(case["completed"])
        self.assertTrue(case["search_protocol_passed"])
        self.assertEqual("result-a", case["search"]["results"][0]["id"])
        self.assertEqual(1, len(case["fetch"]))
        self.assertFalse(case["fetch"][0]["passed"])
        self.assertIn("cancelled", case["fetch"][0]["error"])
        self.assertGreater(case["duration_ms"], 0)

    def test_actual_stdio_process_reads_a_disposable_synthetic_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            prepared = run_mcp_smoke(data_dir=runtime, tenant_id="tenant-probe", tenant_storage_isolation=True,
                disposable_data_dir=True)
            self.assertTrue(prepared["passed"])
            report = run_mcp_probe(data_dir=runtime, query_specs=[{"id": "synthetic-wire", "query": "Article"}],
                tenant_id="tenant-probe", profile_id=None, tenant_storage_isolation=True,
                output_path=Path(tmp) / "probe.json", timeout_seconds=60)
            self.assertTrue(report["passed"], report["errors"] or report["cases"])
            self.assertTrue(all(check["passed"] for check in report["protocol_checks"].values()))
            self.assertGreaterEqual(len(report["cases"][0]["fetch"]), 1)
            self.assertFalse(report["release_ready"])
            self.assertFalse(report["provenance"]["approval_mutation"])


if __name__ == "__main__":
    unittest.main()
