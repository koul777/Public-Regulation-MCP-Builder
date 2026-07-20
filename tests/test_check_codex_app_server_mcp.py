from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_codex_app_server_mcp import check_codex_app_server_mcp, run


FAKE_APP_SERVER = r'''
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}), flush=True)
    elif request.get("id") == 2:
        print(json.dumps({"id": 2, "result": {"data": [{
            "name": "aksmcp2",
            "authStatus": "unsupported",
            "tools": {"get_index_status": {}, "search": {}, "fetch": {}},
            "resources": [],
            "resourceTemplates": [],
            "serverInfo": {"name": "Regulation MCP", "version": "1.0"}
        }]}}), flush=True)
        break
'''


class CodexAppServerMcpTests(unittest.TestCase):
    def test_inventory_must_find_server_and_required_tools(self) -> None:
        report = check_codex_app_server_mcp(
            server_name="aksmcp2",
            codex_command=[sys.executable, "-c", FAKE_APP_SERVER],
            timeout_seconds=5,
        )

        self.assertTrue(report["passed"])
        self.assertTrue(report["app_server_initialized"])
        self.assertTrue(report["server_found"])
        self.assertEqual(3, report["tool_count"])
        self.assertEqual([], report["missing_tools"])
        self.assertEqual("fresh_codex_app_server_process", report["probe_scope"])
        self.assertTrue(report["probe_id"])
        self.assertTrue(report["generated_at"])
        self.assertEqual(str(Path(sys.executable).resolve()), report["provenance"]["executable_path"])
        self.assertIn("Python", report["provenance"]["executable_version"])
        self.assertIsInstance(report["provenance"]["process_id"], int)
        self.assertEqual(1, report["page_count"])
        self.assertTrue(report["pagination_exhausted"])
        self.assertEqual(1, report["matching_server_count"])
        self.assertIsNone(report["timeout_reason"])

    def test_inventory_fails_closed_for_missing_tool(self) -> None:
        report = check_codex_app_server_mcp(
            server_name="aksmcp2",
            required_tools=["get_index_status", "missing_tool"],
            codex_command=[sys.executable, "-c", FAKE_APP_SERVER],
            timeout_seconds=5,
        )

        self.assertFalse(report["passed"])
        self.assertTrue(report["server_found"])
        self.assertEqual(["missing_tool"], report["missing_tools"])

    def test_cli_fails_when_codex_is_unavailable(self) -> None:
        stdout = io.StringIO()

        with patch("scripts.check_codex_app_server_mcp._codex_app_server_command", return_value=[]):
            exit_code = run(["--server-name", "aksmcp2", "--fail-on-issue"], stdout=stdout)

        self.assertEqual(2, exit_code)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["passed"])
        self.assertEqual("codex_cli_unavailable", report["reason_code"])
        self.assertIsNone(report["provenance"]["executable_path"])
        self.assertIsNone(report["provenance"]["process_id"])

    def test_cli_can_probe_an_explicit_codex_executable(self) -> None:
        stdout = io.StringIO()

        exit_code = run(
            [
                "--server-name",
                "aksmcp2",
                "--codex-executable",
                sys.executable,
                "--fail-on-issue",
            ],
            stdout=stdout,
        )

        self.assertEqual(2, exit_code)
        report = json.loads(stdout.getvalue())
        self.assertEqual(str(Path(sys.executable).resolve()), report["provenance"]["executable_path"])
        self.assertNotEqual("codex_cli_unavailable", report["reason_code"])

    def test_inventory_follows_next_cursor_until_target_is_found(self) -> None:
        fake_app_server = r'''
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}), flush=True)
    elif request.get("method") == "mcpServerStatus/list":
        cursor = (request.get("params") or {}).get("cursor")
        if not cursor:
            print(json.dumps({"id": request["id"], "result": {
                "data": [{"name": "other", "tools": {}}],
                "nextCursor": "page-2"
            }}), flush=True)
        else:
            print(json.dumps({"id": request["id"], "result": {"data": [{
                "name": "aksmcp2",
                "authStatus": "unsupported",
                "tools": {"get_index_status": {}, "search": {}, "fetch": {}},
                "serverInfo": {"name": "Regulation MCP", "version": "1.0"}
            }], "nextCursor": None}}), flush=True)
            break
'''
        report = check_codex_app_server_mcp(
            server_name="aksmcp2",
            codex_command=[sys.executable, "-c", fake_app_server],
            timeout_seconds=5,
        )

        self.assertTrue(report["passed"])
        self.assertEqual(2, report["page_count"])
        self.assertEqual(2, report["server_count"])
        self.assertTrue(report["pagination_exhausted"])

    def test_inventory_fails_closed_for_duplicate_server_name_across_pages(self) -> None:
        fake_app_server = r'''
import json
import sys
entry = {
    "name": "aksmcp2",
    "authStatus": "unsupported",
    "tools": {"get_index_status": {}, "search": {}, "fetch": {}},
    "serverInfo": {"name": "Regulation MCP", "version": "1.0"}
}
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}), flush=True)
    elif request.get("method") == "mcpServerStatus/list":
        cursor = (request.get("params") or {}).get("cursor")
        result = {"data": [entry], "nextCursor": "page-2" if not cursor else None}
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
        if cursor:
            break
'''
        report = check_codex_app_server_mcp(
            server_name="aksmcp2",
            codex_command=[sys.executable, "-c", fake_app_server],
            timeout_seconds=5,
        )

        self.assertFalse(report["passed"])
        self.assertTrue(report["server_found"])
        self.assertEqual(2, report["matching_server_count"])
        self.assertEqual("duplicate_server_name", report["reason_code"])
        self.assertIn("more than once", report["error"])

    def test_status_timeout_has_explicit_reason(self) -> None:
        fake_app_server = r'''
import json
import sys
import time
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}), flush=True)
    elif request.get("method") == "mcpServerStatus/list":
        time.sleep(2)
'''
        report = check_codex_app_server_mcp(
            server_name="aksmcp2",
            codex_command=[sys.executable, "-c", fake_app_server],
            timeout_seconds=1,
        )

        self.assertFalse(report["passed"])
        self.assertTrue(report["app_server_initialized"])
        self.assertEqual("timeout", report["reason_code"])
        self.assertEqual("mcp_status_list_timeout", report["timeout_reason"])

    def test_initialize_timeout_has_distinct_reason(self) -> None:
        fake_app_server = r'''
import time
time.sleep(2)
'''
        report = check_codex_app_server_mcp(
            server_name="aksmcp2",
            codex_command=[sys.executable, "-c", fake_app_server],
            timeout_seconds=1,
        )

        self.assertFalse(report["passed"])
        self.assertFalse(report["app_server_initialized"])
        self.assertEqual("timeout", report["reason_code"])
        self.assertEqual("initialize_timeout", report["timeout_reason"])

    def test_provenance_records_explicit_codex_home_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "custom-codex-home"
            codex_home.mkdir()
            config_bytes = b"[mcp_servers.aksmcp2]\ncommand = 'fixture'\n"
            (codex_home / "config.toml").write_bytes(config_bytes)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                report = check_codex_app_server_mcp(
                    server_name="aksmcp2",
                    codex_command=[sys.executable, "-c", FAKE_APP_SERVER],
                    timeout_seconds=5,
                )

        scope = report["provenance"]["config_scope"]
        self.assertEqual("CODEX_HOME", scope["source"])
        self.assertFalse(scope["uses_default_codex_home"])
        self.assertEqual(64, len(scope["codex_home_sha256"]))
        self.assertEqual(64, len(scope["config_path_sha256"]))
        self.assertNotIn(str(codex_home), json.dumps(scope))
        self.assertTrue(scope["config_exists"])
        expected_content_fingerprint = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        self.assertEqual(expected_content_fingerprint, scope["config_content_sha256_before_process"])
        self.assertEqual(expected_content_fingerprint, scope["config_content_sha256_after_process"])
        self.assertTrue(scope["config_content_stable_during_probe"])
        self.assertEqual(expected_content_fingerprint, scope["config_content_sha256"])

    def test_inventory_fails_closed_when_config_changes_during_probe(self) -> None:
        mutating_app_server = r'''
import json
import os
import pathlib
import sys
config_path = pathlib.Path(os.environ["CODEX_HOME"]) / "config.toml"
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}}), flush=True)
    elif request.get("id") == 2:
        config_path.write_text("changed during probe\n", encoding="utf-8")
        print(json.dumps({"id": 2, "result": {"data": [{
            "name": "aksmcp2",
            "tools": {"get_index_status": {}, "search": {}, "fetch": {}}
        }]}}), flush=True)
        break
'''
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("original\n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                report = check_codex_app_server_mcp(
                    server_name="aksmcp2",
                    codex_command=[sys.executable, "-c", mutating_app_server],
                    timeout_seconds=5,
                )

        scope = report["provenance"]["config_scope"]
        self.assertFalse(report["passed"])
        self.assertEqual("config_changed_during_probe", report["reason_code"])
        self.assertFalse(scope["config_content_stable_during_probe"])
        self.assertIsNone(scope["config_content_sha256"])


if __name__ == "__main__":
    unittest.main()
