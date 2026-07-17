from __future__ import annotations

import json
from pathlib import Path
import io
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
                    "tool_names": ["fetch", "search"],
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
                    "tool_names": ["fetch", "search"],
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

    def test_cli_fails_without_any_config(self) -> None:
        stdout = io.StringIO()

        exit_code = run(["--fail-on-issue"], stdout=stdout)

        self.assertEqual(2, exit_code)
        self.assertIn("At least one", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
