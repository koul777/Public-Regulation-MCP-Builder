from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.run_mcp_smoke import run_mcp_smoke
from scripts.run_mcp_transport_smoke import build_parser, run_mcp_transport_smoke


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
        self.assertEqual(set(report["chatgpt_data_profile"]["tool_names"]), {"search", "fetch"})
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
        self.assertEqual(set(report["chatgpt_data_profile"]["tool_names"]), {"search", "fetch"})
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


if __name__ == "__main__":
    unittest.main()
