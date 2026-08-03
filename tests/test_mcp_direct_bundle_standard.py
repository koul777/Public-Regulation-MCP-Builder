from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from scripts.generate_mcp_client_config import (
    build_mcp_client_config,
    write_mcp_setup_bundle,
    write_mcp_setup_bundle_zip,
)
from scripts.mcp_bundle_contract import LEGACY_CONNECTION_ARTIFACT_FILENAMES


class McpDirectBundleStandardTests(unittest.TestCase):
    def test_bundle_contains_only_direct_connection_artifacts(self) -> None:
        config = build_mcp_client_config(
            server_name="direct-standard",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://example.vercel.app/mcp",
            chatgpt_oauth_ready=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            for filename in LEGACY_CONNECTION_ARTIFACT_FILENAMES:
                (bundle_dir / filename).write_text("legacy", encoding="utf-8")
            legacy_plugin = bundle_dir / "chatgpt-desktop-local-plugin"
            legacy_plugin.mkdir()
            (legacy_plugin / "plugin.json").write_text("{}", encoding="utf-8")

            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="direct-standard",
            )
            generated_names = {
                path.relative_to(bundle_dir).as_posix()
                for path in bundle_dir.rglob("*")
                if path.is_file()
            }
            desktop = json.loads(
                Path(files["chatgpt_desktop_local"]).read_text(encoding="utf-8")
            )
            codex = tomllib.loads(
                Path(files["codex_config"]).read_text(encoding="utf-8")
            )
            manifest = json.loads(Path(files["manifest"]).read_text(encoding="utf-8"))
            connect_script = Path(files["connect"]).read_text(encoding="utf-8-sig")

        lowered_names = {name.casefold() for name in generated_names}
        self.assertFalse(any(name.endswith(".bat") for name in lowered_names))
        self.assertFalse(any("prompt" in name for name in lowered_names))
        self.assertFalse(any("plugin" in name for name in lowered_names))
        self.assertTrue(
            {
                "run_http_server.ps1",
                "run_chatgpt_data_server.ps1",
                "run_openai_secure_tunnel.ps1",
            }.isdisjoint(lowered_names)
        )
        self.assertFalse(legacy_plugin.exists())
        self.assertTrue(
            {
                "chatgpt_desktop_local_mcp.json",
                "codex_config_snippet.toml",
                "claude_desktop_config.json",
                "claude_code_add_stdio.ps1",
                "claude_code_add_http.ps1",
                "claude_https_mcp.json",
                "run_mcp_stdio_server.ps1",
            }.issubset(generated_names)
        )
        self.assertNotIn("plugin", json.dumps(desktop, ensure_ascii=False).casefold())
        self.assertEqual("unsupported", desktop["support_status"])
        self.assertFalse(desktop["direct_local_supported"])
        self.assertFalse(desktop["chatgpt_direct_local_mcp_supported"])
        self.assertIn("does not directly connect to a local MCP server", desktop["warning"])
        self.assertIn("12584461", desktop["official_help_url"])
        self.assertIn("secure-mcp-tunnels", desktop["secure_mcp_tunnel_url"])
        for runnable_key in ("mcpServers", "ui_fields", "command", "args", "cwd", "env"):
            self.assertNotIn(runnable_key, desktop)
        codex_server = codex["mcp_servers"]["direct-standard"]
        self.assertEqual("powershell.exe", codex_server["command"])
        self.assertIn("--tool-profile", codex_server["args"])
        self.assertEqual(
            "full",
            codex_server["args"][codex_server["args"].index("--tool-profile") + 1],
        )
        connection_clients = {item["client"] for item in manifest["connections"]}
        self.assertIn(
            "Codex CLI / Codex IDE",
            connection_clients,
        )
        self.assertIn("ChatGPT web · remote HTTPS MCP", connection_clients)
        self.assertNotIn("ChatGPT Desktop / Codex CLI / Codex IDE", connection_clients)
        self.assertNotIn("plugin list", connect_script.casefold())
        self.assertIn("secure mcp tunnel", connect_script.casefold())
        self.assertNotIn("ChatGPT Desktop", connect_script)
        self.assertIn('"chatgpt-desktop-local" { Show-UnsupportedChatGptLocal }', connect_script)
        self.assertNotIn("settings > plugins", connect_script.casefold())

    def test_handoff_zip_excludes_bat_prompts_and_plugins(self) -> None:
        config = build_mcp_client_config(
            server_name="direct-standard",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://example.vercel.app/mcp",
            chatgpt_oauth_ready=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            write_mcp_setup_bundle(config, bundle_dir, server_name="direct-standard")
            write_mcp_setup_bundle_zip(bundle_dir, zip_path)
            with zipfile.ZipFile(zip_path) as archive:
                names = {name.casefold() for name in archive.namelist()}
                desktop = json.loads(
                    archive.read("chatgpt_desktop_local_mcp.json").decode("utf-8")
                )
                status = json.loads(archive.read("bundle_status.json").decode("utf-8"))

        self.assertFalse(any(name.endswith(".bat") for name in names))
        self.assertFalse(any("prompt" in name for name in names))
        self.assertFalse(any("plugin" in name for name in names))
        self.assertTrue(
            {
                "run_http_server.ps1",
                "run_chatgpt_data_server.ps1",
                "run_openai_secure_tunnel.ps1",
            }.isdisjoint(names)
        )
        self.assertIn("chatgpt_desktop_local_mcp.json", names)
        self.assertIn("claude_desktop_config.json", names)
        self.assertIn("claude_https_mcp.json", names)
        self.assertNotIn("claude_api_fragment.json", names)
        self.assertIn("run_mcp_stdio_server.ps1", names)
        self.assertEqual("unsupported", desktop["support_status"])
        self.assertFalse(desktop["direct_local_supported"])
        self.assertNotIn("mcpServers", desktop)
        self.assertFalse(
            status["portable_handoff_runtime"]["bundled_windows_executable"]
        )
        self.assertTrue(
            status["portable_handoff_runtime"][
                "python_required_when_packaged_executable_absent"
            ]
        )


if __name__ == "__main__":
    unittest.main()
