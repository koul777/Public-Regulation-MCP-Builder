from __future__ import annotations

import json
from pathlib import Path
import asyncio
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_mcp_client_config_smoke import (
    _external_metadata_violations,
    _exception_message,
    _remote_unauthenticated_request_is_rejected,
    _search_with_fallback,
    _successful_tool_payload,
    _valid_fetch_payload,
    _valid_index_status_summary,
    _valid_search_results,
    _validate_strict_jsonrpc_stdout,
    run,
    run_mcp_client_config_smoke,
)


class RunMcpClientConfigSmokeTests(unittest.TestCase):
    def test_exception_group_message_includes_nested_cause(self) -> None:
        message = _exception_message(ExceptionGroup("unhandled errors in a TaskGroup", [FileNotFoundError("server")]))

        self.assertIn("ExceptionGroup", message)
        self.assertIn("FileNotFoundError", message)
        self.assertIn("server", message)

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
                    "strict_stdio_wire_verified": True,
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
                    "strict_stdio_wire_verified": True,
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

    def test_claude_code_config_smoke_keeps_client_identity_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "claude_code_stdio_smoke.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "govreg-local": {
                                "type": "stdio",
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
                    "strict_stdio_wire_verified": True,
                    "index_status_verified": True,
                    "end_to_end_verified": True,
                }

            with patch("scripts.run_mcp_client_config_smoke._run_client_entry", new=fake_run_client_entry):
                report = run_mcp_client_config_smoke(
                    claude_code_config=config,
                    server_name="govreg-local",
                )

        self.assertTrue(report["passed"])
        self.assertEqual("claude_code", report["results"][0]["label"])
        self.assertEqual("powershell.exe", report["results"][0]["command"])
        self.assertEqual(["-File", str(root / "run_mcp_stdio_server.ps1")], report["results"][0]["args"])

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
                    "strict_stdio_wire_verified": True,
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
        self.assertTrue(report["direct_stdio_verified"])
        self.assertEqual(
            "aksmcp MCP의 search 도구로 인사규정을 찾고, 반환된 첫 번째 id를 "
            "fetch 도구로 조회해 조문 원문과 출처를 보여줘.",
            report["verification_prompt"],
        )
        answer = report["verification_answer"]
        self.assertEqual("verified", answer["status"])
        self.assertTrue(answer["get_index_status_verified"])
        self.assertIn("get_index_status", answer["available_regulation_tools"])
        self.assertEqual(7, answer["index_status_summaries"][0]["indexed_records"])
        self.assertTrue(answer["conversation_attachment_unverified"])
        self.assertFalse(answer["desktop_tool_scan_verified"])
        self.assertFalse(answer["conversation_attachment_verified"])

    def test_plugin_config_rejects_utf8_bom_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".mcp.json"
            payload = {"mcpServers": {"aksmcp": {"command": "python", "args": []}}}
            config.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))

            report = run_mcp_client_config_smoke(plugin_mcp_config=config, server_name="aksmcp")

        self.assertFalse(report["passed"])
        self.assertFalse(report["process_started"])
        self.assertFalse(report["results"][0]["config_encoding_verified"])
        self.assertIn("EF BB BF", report["results"][0]["error"])

    def test_plugin_config_rejects_snake_case_container_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".mcp.json"
            payload = {"mcp_servers": {"aksmcp": {"command": "python", "args": []}}}
            config.write_text(json.dumps(payload), encoding="utf-8")

            report = run_mcp_client_config_smoke(plugin_mcp_config=config, server_name="aksmcp")

        self.assertFalse(report["passed"])
        self.assertFalse(report["process_started"])
        self.assertIn("requires mcpServers", report["results"][0]["error"])

    def test_plugin_config_rejects_duplicate_json_keys_before_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".mcp.json"
            config.write_text(
                '{"mcpServers":{"aksmcp":{"command":"python","command":"other","args":[]}}}',
                encoding="utf-8",
            )

            report = run_mcp_client_config_smoke(plugin_mcp_config=config, server_name="aksmcp")

        self.assertFalse(report["passed"])
        self.assertFalse(report["process_started"])
        self.assertIn("duplicate JSON key: command", report["results"][0]["error"])

    def test_claude_desktop_config_keeps_explicit_bom_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "claude_desktop_config.json"
            payload = {"mcpServers": {"aksmcp": {"command": "python", "args": []}}}
            config.write_text(json.dumps(payload), encoding="utf-8-sig")

            async def fake_run_client_entry(*, command, args, query):
                return {
                    "passed": True,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "strict_stdio_wire_verified": True,
                    "index_status_verified": True,
                    "end_to_end_verified": True,
                }

            with patch("scripts.run_mcp_client_config_smoke._run_client_entry", new=fake_run_client_entry):
                report = run_mcp_client_config_smoke(
                    claude_desktop_config=config,
                    server_name="aksmcp",
                )

        self.assertTrue(report["passed"])
        self.assertTrue(report["results"][0]["config_encoding_verified"])

    def test_strict_stdio_wire_rejects_bom_blank_lines_and_non_json_notices(self) -> None:
        valid = (
            b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
            b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
        )
        self.assertEqual(2, _validate_strict_jsonrpc_stdout(valid)["message_count"])

        invalid_streams = {
            "bom": b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"result":{}}\n',
            "blank": b'{"jsonrpc":"2.0","id":1,"result":{}}\n\n',
            "notice": b'MCP server ready\n',
            "invalid_utf8": b"\xff\n",
        }
        for label, stdout in invalid_streams.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                _validate_strict_jsonrpc_stdout(stdout)

    def test_search_smoke_falls_back_when_manifest_query_returns_no_results(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.queries: list[str] = []

            async def call_tool(self, name, arguments):
                self.queries.append(arguments["query"])
                results = [{"id": "result-1"}] if arguments["query"] == "규정" else []
                return SimpleNamespace(structuredContent={"results": results})

        session = FakeSession()
        _payload, results, query_used, attempted, _elapsed = asyncio.run(
            _search_with_fallback(session, query="제3조 다른 규정의 개정")
        )

        self.assertEqual([{"id": "result-1"}], results)
        self.assertEqual("규정", query_used)
        self.assertEqual(["제3조 다른 규정의 개정", "제1조", "규정"], attempted)
        self.assertEqual(attempted, session.queries)

    def test_remote_rejects_http_and_does_not_report_connected(self) -> None:
        report = run_mcp_client_config_smoke(
            remote_url="http://127.0.0.1:8000/mcp",
            server_name="aksmcp",
        )

        self.assertFalse(report["passed"])
        self.assertFalse(report["end_to_end_verified"])
        self.assertEqual("not_verified", report["verification_answer"]["status"])
        self.assertIn("https://", report["results"][0]["error"])

    def test_remote_requires_token_env_even_when_no_token_name_is_supplied(self) -> None:
        report = run_mcp_client_config_smoke(
            remote_url="https://mcp.example.test/mcp",
            remote_token_env=None,
            server_name="aksmcp",
        )

        result = report["results"][0]
        self.assertFalse(report["passed"])
        self.assertFalse(result["auth_wire_verified"])
        self.assertIn("bearer-token environment variable", result["error"])

    def test_remote_url_rejects_and_sanitizes_credentials_query_and_fragment(self) -> None:
        urls = (
            "https://user:sentinel-password@mcp.example.test/mcp",
            "https://mcp.example.test/mcp?token=sentinel-query",
            "https://mcp.example.test/mcp#sentinel-fragment",
            "https://localhost/mcp",
            "https://127.0.0.1/mcp",
        )
        for remote_url in urls:
            with self.subTest(remote_url=remote_url):
                report = run_mcp_client_config_smoke(
                    remote_url=remote_url,
                    remote_token_env="MCP_TEST_TOKEN",
                    server_name="aksmcp",
                )
                serialized = json.dumps(report)
                self.assertFalse(report["passed"])
                self.assertNotIn("sentinel-password", serialized)
                self.assertNotIn("sentinel-query", serialized)
                self.assertNotIn("sentinel-fragment", serialized)

    def test_remote_auth_probe_rejects_unauthenticated_and_random_invalid_bearer(self) -> None:
        seen_headers: list[dict[str, str]] = []

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, *, json, headers):
                seen_headers.append(headers)
                return SimpleNamespace(status_code=401)

        with patch("scripts.run_mcp_client_config_smoke.httpx.AsyncClient", return_value=FakeAsyncClient()):
            rejected = asyncio.run(
                _remote_unauthenticated_request_is_rejected(url="https://mcp.example.test/mcp")
            )

        self.assertTrue(rejected)
        self.assertEqual(2, len(seen_headers))
        self.assertNotIn("Authorization", seen_headers[0])
        self.assertRegex(seen_headers[1]["Authorization"], r"^Bearer invalid-")

    def test_remote_auth_probe_fails_if_invalid_bearer_is_accepted(self) -> None:
        status_codes = iter((401, 200))

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, *, json, headers):
                return SimpleNamespace(status_code=next(status_codes))

        with patch("scripts.run_mcp_client_config_smoke.httpx.AsyncClient", return_value=FakeAsyncClient()):
            rejected = asyncio.run(
                _remote_unauthenticated_request_is_rejected(url="https://mcp.example.test/mcp")
            )

        self.assertFalse(rejected)

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
        ), patch(
            "scripts.run_mcp_client_config_smoke._remote_unauthenticated_request_is_rejected",
            return_value=True,
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

    def test_remote_external_profile_accepts_search_fetch_without_index_status(self) -> None:
        async def fake_remote_entry(*, url, token):
            return {
                "passed": True,
                "process_started": True,
                "mcp_initialized": True,
                "tools_discovered": True,
                "index_status_verified": False,
                "contract_verified": True,
                "end_to_end_verified": True,
                "verification_mode": "search_fetch",
                "tool_names": ["fetch", "search"],
                "index_status_summary": {},
                "session_id_present": True,
            }

        with patch.dict(os.environ, {"MCP_TEST_TOKEN": "secret"}, clear=False), patch(
            "scripts.run_mcp_client_config_smoke._run_remote_entry",
            new=fake_remote_entry,
        ), patch(
            "scripts.run_mcp_client_config_smoke._remote_unauthenticated_request_is_rejected",
            return_value=True,
        ):
            report = run_mcp_client_config_smoke(
                remote_url="https://mcp.example.test/mcp",
                remote_token_env="MCP_TEST_TOKEN",
                server_name="aksmcp",
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["end_to_end_verified"])
        self.assertFalse(report["verification_answer"]["get_index_status_verified"])
        self.assertTrue(report["verification_answer"]["search_fetch_verified"])
        self.assertIn("search_fetch", report["verification_answer"]["verification_modes"])

    def test_remote_smoke_redacts_token_from_exception(self) -> None:
        async def fake_remote_entry(*, url, token):
            raise RuntimeError(f"request failed with Authorization: Bearer {token}")

        with patch.dict(os.environ, {"MCP_TEST_TOKEN": "sentinel-secret"}, clear=False), patch(
            "scripts.run_mcp_client_config_smoke._run_remote_entry",
            new=fake_remote_entry,
        ), patch(
            "scripts.run_mcp_client_config_smoke._remote_unauthenticated_request_is_rejected",
            return_value=True,
        ):
            report = run_mcp_client_config_smoke(
                remote_url="https://mcp.example.test/mcp",
                remote_token_env="MCP_TEST_TOKEN",
                server_name="aksmcp",
            )

        serialized = json.dumps(report)
        self.assertNotIn("sentinel-secret", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_remote_rejects_server_that_does_not_enforce_bearer_auth(self) -> None:
        async def fake_remote_entry(*, url, token):
            return {
                "passed": True,
                "process_started": True,
                "mcp_initialized": True,
                "tools_discovered": True,
                "contract_verified": True,
                "end_to_end_verified": True,
                "tool_names": ["fetch", "search"],
            }

        with patch.dict(os.environ, {"MCP_TEST_TOKEN": "secret"}, clear=False), patch(
            "scripts.run_mcp_client_config_smoke._run_remote_entry",
            new=fake_remote_entry,
        ), patch(
            "scripts.run_mcp_client_config_smoke._remote_unauthenticated_request_is_rejected",
            return_value=False,
        ):
            report = run_mcp_client_config_smoke(
                remote_url="https://mcp.example.test/mcp",
                remote_token_env="MCP_TEST_TOKEN",
                server_name="aksmcp",
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["results"][0]["auth_wire_verified"])
        self.assertTrue(report["results"][0]["protocol_contract_verified"])

    def test_remote_tool_contract_rejects_is_error_and_malformed_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "isError=true"):
            _successful_tool_payload(
                SimpleNamespace(isError=True, structuredContent={"summary": {"document_count": 1}}),
                tool_name="get_index_status",
            )
        self.assertFalse(_valid_index_status_summary({"unexpected": "value"}))
        self.assertFalse(_valid_index_status_summary({"document_count": -1}))
        self.assertTrue(_valid_index_status_summary({"document_count": 0, "status_counts": {}}))
        self.assertFalse(_valid_search_results([{"title": "missing id"}]))
        self.assertFalse(_valid_search_results([{"id": ""}]))
        self.assertTrue(_valid_search_results([{"id": "result-1"}]))
        self.assertFalse(_valid_fetch_payload({"text": "   "}))
        self.assertTrue(_valid_fetch_payload({"text": "approved text"}))

    def test_external_metadata_deny_list_detects_internal_fields(self) -> None:
        self.assertEqual(
            ["approval_review_batch_manifest_path", "source_file_id"],
            _external_metadata_violations(
                [
                    {"source_file_id": "internal-file", "source_record_id": ""},
                    {"approval_review_batch_manifest_path": "reports/internal.json"},
                ]
            ),
        )

    def test_cli_fails_without_any_config(self) -> None:
        stdout = io.StringIO()

        exit_code = run(["--fail-on-issue"], stdout=stdout)

        self.assertEqual(2, exit_code)
        self.assertIn("At least one", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
