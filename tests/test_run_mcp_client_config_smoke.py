from __future__ import annotations

import json
from pathlib import Path
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_mcp_client_config_smoke import run, run_mcp_client_config_smoke


class RunMcpClientConfigSmokeTests(unittest.TestCase):
    def test_codex_config_uses_bundle_recommended_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_data = root / "bundle" / "data"
            bundle_data.mkdir(parents=True)
            bundle_data.joinpath("mcp_runtime_manifest.json").write_text(
                json.dumps({"recommended_smoke_query": "제1조"}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = root / "config.toml"
            config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        (
                            'args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "'
                            + (root / "bundle" / "run_mcp_stdio_server.ps1").as_posix()
                            + '", "--data-dir", "'
                            + bundle_data.as_posix()
                            + '", "--transport", "stdio", "--flat-storage", "--no-warm-cache"]'
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            seen: dict[str, object] = {}

            async def fake_run_client_entry(*, command, args, query):
                seen.update({"command": command, "args": args, "query": query})
                return {
                    "passed": True,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "index_status_verified": True,
                    "end_to_end_verified": True,
                    "tool_names": ["fetch", "get_index_status", "search"],
                    "index_status_summary": {"indexed_records": 1},
                    "search_result_count": 1,
                    "fetch_has_text": True,
                }

            with patch("scripts.run_mcp_client_config_smoke._run_client_entry", new=fake_run_client_entry):
                report = run_mcp_client_config_smoke(codex_config=config, server_name="govreg-local")

        self.assertTrue(report["passed"])
        self.assertEqual("powershell.exe", seen["command"])
        self.assertEqual("제1조", seen["query"])
        self.assertIn("--no-warm-cache", seen["args"])

    def test_claude_desktop_config_smoke_reads_json_server_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "claude_desktop_config.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "govreg-local": {
                                "command": "powershell.exe",
                                "args": [
                                    "-NoProfile",
                                    "-ExecutionPolicy",
                                    "Bypass",
                                    "-File",
                                    str(root / "run_mcp_stdio_server.ps1"),
                                    "--data-dir",
                                    str(root / "data"),
                                    "--transport",
                                    "stdio",
                                    "--flat-storage",
                                    "--no-warm-cache",
                                ],
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            async def fake_run_client_entry(*, command, args, query):
                return {
                    "passed": True,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "index_status_verified": True,
                    "end_to_end_verified": True,
                    "tool_names": ["fetch", "get_index_status", "search"],
                    "index_status_summary": {"indexed_records": 1},
                    "search_result_count": 1,
                    "fetch_has_text": True,
                }

            with patch("scripts.run_mcp_client_config_smoke._run_client_entry", new=fake_run_client_entry):
                report = run_mcp_client_config_smoke(
                    claude_desktop_config=config,
                    query="임원",
                    server_name="govreg-local",
                )

        self.assertTrue(report["passed"])
        self.assertEqual("claude_desktop", report["results"][0]["label"])
        self.assertEqual("임원", report["results"][0]["query"])

    def test_plugin_config_generates_verified_prompt_answer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".mcp.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "aksmcp": {
                                "command": "powershell.exe",
                                "args": ["-File", str(root / "run_mcp_stdio_server.ps1")],
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            async def fake_run_client_entry(*, command, args, query):
                return {
                    "passed": True,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "index_status_verified": True,
                    "end_to_end_verified": True,
                    "tool_names": ["fetch", "get_index_status", "list_regulations", "search"],
                    "index_status_summary": {"indexed_records": 7, "tenant_id": "tenant-a"},
                    "search_result_count": 1,
                    "fetch_has_text": True,
                }

            with patch("scripts.run_mcp_client_config_smoke._run_client_entry", new=fake_run_client_entry):
                report = run_mcp_client_config_smoke(plugin_mcp_config=config, server_name="aksmcp")

        self.assertTrue(report["passed"])
        self.assertEqual("@aksmcp MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", report["verification_prompt"])
        answer = report["verification_answer"]
        self.assertEqual("verified", answer["status"])
        self.assertTrue(answer["get_index_status_verified"])
        self.assertIn("get_index_status", answer["available_regulation_tools"])
        self.assertEqual(7, answer["index_status_summaries"][0]["indexed_records"])
        self.assertTrue(answer["conversation_attachment_unverified"])

    def test_remote_rejects_http_and_does_not_report_connected(self) -> None:
        report = run_mcp_client_config_smoke(
            remote_url="http://127.0.0.1:8000/mcp",
            server_name="aksmcp",
        )

        self.assertFalse(report["passed"])
        self.assertFalse(report["end_to_end_verified"])
        self.assertEqual("not_verified", report["verification_answer"]["status"])
        self.assertIn("https://", report["results"][0]["error"])

    def test_remote_requires_named_token_and_preserves_unverified_states(self) -> None:
        with patch.dict(os.environ, {"MCP_TEST_TOKEN": ""}, clear=False):
            report = run_mcp_client_config_smoke(
                remote_url="https://mcp.example.test/mcp",
                remote_token_env="MCP_TEST_TOKEN",
                server_name="aksmcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["process_started"])
        self.assertFalse(report["mcp_initialized"])
        self.assertFalse(report["tools_discovered"])
        self.assertTrue(report["tool_scan_unverified"])

    def test_remote_initialize_tools_and_index_status_are_required_for_verification(self) -> None:
        async def fake_remote_entry(*, url, token):
            self.assertEqual("https://mcp.example.test/mcp", url)
            self.assertEqual("secret", token)
            return {
                "passed": True,
                "process_started": True,
                "mcp_initialized": True,
                "tools_discovered": True,
                "index_status_verified": True,
                "end_to_end_verified": True,
                "tool_names": ["get_index_status", "list_regulations", "search"],
                "index_status_summary": {"indexed_records": 3},
                "session_id_present": True,
            }

        with patch.dict(os.environ, {"MCP_TEST_TOKEN": "secret"}, clear=False), patch(
            "scripts.run_mcp_client_config_smoke._run_remote_entry",
            new=fake_remote_entry,
        ):
            report = run_mcp_client_config_smoke(
                remote_url="https://mcp.example.test/mcp",
                remote_token_env="MCP_TEST_TOKEN",
                server_name="aksmcp",
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["end_to_end_verified"])
        self.assertTrue(report["tool_scan_unverified"])
        self.assertEqual("verified", report["verification_answer"]["status"])

    def test_cli_fails_without_any_config(self) -> None:
        stdout = io.StringIO()

        exit_code = run(["--fail-on-issue"], stdout=stdout)

        self.assertEqual(2, exit_code)
        self.assertIn("At least one", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
