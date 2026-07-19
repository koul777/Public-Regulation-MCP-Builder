from __future__ import annotations

import hashlib
import json
import unittest
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.ingestion.vector_adapter import stable_content_hash
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.structure import StructureNode
from app.storage.repository import JsonRepository
from scripts.generate_mcp_client_config import (
    _recommended_runtime_smoke_query,
    build_mcp_client_config,
    main,
    parse_args,
    write_mcp_runtime_data_bundle,
    write_mcp_setup_bundle,
    write_mcp_setup_bundle_zip,
)
from scripts.mcp_bundle_contract import ALL_SETUP_BUNDLE_FILES, REQUIRED_SETUP_BUNDLE_FILES


class GenerateMcpClientConfigTests(unittest.TestCase):
    def test_cli_accepts_explicit_wheel_dist_directory(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["generate_mcp_client_config.py", "--include-wheel", "--wheel-dist-dir", "reports/run/dist"],
        ):
            args = parse_args()

        self.assertTrue(args.include_wheel)
        self.assertEqual("reports/run/dist", args.wheel_dist_dir)

    def test_cli_accepts_repeated_document_ids_for_selected_regulations(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "generate_mcp_client_config.py",
                "--document-id",
                "doc-a",
                "--document-id",
                "doc-b",
            ],
        ):
            args = parse_args()

        self.assertEqual(["doc-a", "doc-b"], args.document_id)

    def test_cli_accepts_canonical_chatgpt_local_and_remote_profiles(self) -> None:
        for profile in ("chatgpt-desktop-local", "chatgpt-remote"):
            with self.subTest(profile=profile), patch.object(
                sys,
                "argv",
                ["generate_mcp_client_config.py", "--client-profile", profile],
            ):
                args = parse_args()

            self.assertEqual(profile, args.client_profile)

    def test_committed_bundle_readmes_require_real_runtime_visibility_audit(self) -> None:
        reports_dir = Path(__file__).resolve().parents[1] / "reports"
        readmes = sorted(reports_dir.glob("mcp_connection_bundle*/README*.md"))
        if not readmes:
            self.skipTest("No generated MCP connection bundle README artifacts present.")

        missing = []
        for path in readmes:
            text = path.read_text(encoding="utf-8")
            if (
                "reg-rag-mcp-index-visibility" not in text
                or "--forbid-smoke-docs" not in text
                or "--require-indexed" not in text
            ):
                missing.append(path.relative_to(reports_dir.parent).as_posix())

        self.assertEqual([], missing)

    def test_builds_stdio_config_with_tenant_isolation(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-test",
            data_dir="data",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
            transport="stdio",
        )

        server = config["mcpServers"]["govreg-test"]
        self.assertEqual(server["command"], "reg-rag-mcp-server")
        self.assertIn("--tenant-storage-isolation", server["args"])
        self.assertNotIn("--flat-storage", server["args"])
        self.assertIn("tenant-a", server["args"])

    def test_builds_streamable_http_config_with_client_url(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-http",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
            transport="streamable-http",
            host="0.0.0.0",
            port=9000,
            actor="mcp-tenant-a",
            role="operator",
            department_ids=["hr", "legal"],
        )

        server = config["mcpServers"]["govreg-http"]
        self.assertEqual(server["url"], "http://127.0.0.1:9000/mcp")
        self.assertEqual(server["transport"], "streamable-http")
        self.assertIn("--host", server["serverCommand"]["args"])
        self.assertIn("0.0.0.0", server["serverCommand"]["args"])
        self.assertIn("--tenant-storage-isolation", server["serverCommand"]["args"])
        self.assertIn("mcp-tenant-a", server["serverCommand"]["args"])
        self.assertIn("operator", server["serverCommand"]["args"])
        self.assertEqual(server["serverCommand"]["args"].count("--department-id"), 2)

    def test_builds_claude_code_stdio_config(self) -> None:
        config = build_mcp_client_config(
            client_profile="claude-code",
            transport="stdio",
            tenant_id="tenant-a",
            role="operator",
        )

        self.assertEqual(config["type"], "stdio")
        self.assertEqual(config["command"], "reg-rag-mcp-server")
        self.assertIn("--tenant-id", config["args"])
        self.assertIn("--flat-storage", config["args"])
        self.assertIn("tenant-a", config["args"])
        self.assertIn("operator", config["args"])

    def test_builds_chatgpt_connector_config_with_public_url(self) -> None:
        config = build_mcp_client_config(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            public_url="https://mcp.example.go.kr/govreg",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
        )

        self.assertEqual(config["connector_url"], "https://mcp.example.go.kr/govreg/mcp")
        self.assertTrue(config["chatgpt_setup"]["requires_reachable_https"])
        self.assertTrue(config["chatgpt_setup"]["https_endpoint_ready"])
        self.assertIn("search", config["compatible_tools"])
        self.assertIn("fetch", config["compatible_tools"])
        self.assertIn("get_index_status", config["compatible_tools"])
        self.assertIn("--tenant-storage-isolation", config["server_start"]["args"])
        self.assertIn("--tool-profile", config["server_start"]["args"])
        self.assertIn("full", config["server_start"]["args"])
        self.assertIn("--allowed-http-host", config["server_start"]["args"])
        self.assertIn("--allowed-http-origin", config["server_start"]["args"])
        self.assertIn("--no-warm-cache", config["server_start"]["args"])
        self.assertIn("--http-bearer-token-env", config["server_start"]["args"])
        self.assertIn("--auth-issuer-url", config["server_start"]["args"])
        self.assertIn("https://mcp.example.go.kr/govreg", config["server_start"]["args"])
        self.assertEqual(config["server_auth"]["token_env"], "MCP_AUTH_TOKEN")
        self.assertNotIn("$BundleDataDir", config["openai_secure_tunnel"]["copy_paste_ps"])

    def test_chatgpt_connector_requires_https_public_url(self) -> None:
        config = build_mcp_client_config(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            public_url="http://mcp.example.go.kr",
        )

        self.assertFalse(config["ready"])
        self.assertEqual(config["connector_url"], "http://mcp.example.go.kr/mcp")
        self.assertIn("public_url_must_use_https", config["missing"])
        self.assertTrue(config["chatgpt_setup"]["requires_reachable_https"])
        self.assertFalse(config["chatgpt_setup"]["https_endpoint_ready"])

    def test_builds_bundle_for_common_clients(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr/mcp",
        )

        self.assertIn("quickstart", config)
        self.assertIn("claude_desktop", config)
        self.assertIn("claude_code", config)
        self.assertIn("chatgpt_desktop_local", config)
        self.assertIn("chatgpt_remote", config)
        self.assertIn("claude_api", config)
        self.assertEqual(config["chatgpt_remote"]["connector_url"], "https://mcp.example.go.kr/mcp")
        self.assertEqual(config["claude_api"]["mcp_servers"][0]["url"], "https://mcp.example.go.kr/mcp")
        self.assertEqual(config["quickstart"]["claude_code"]["command"], "claude")
        self.assertEqual(config["quickstart"]["claude_code"]["args"][:3], ["mcp", "add-json", "aks_mcp"])
        claude_desktop_args = config["claude_desktop"]["mcpServers"]["aks_mcp"]["args"]
        claude_code_args = config["claude_code"]["args"]
        self.assertIn("--no-warm-cache", claude_desktop_args)
        self.assertIn("--no-warm-cache", claude_code_args)
        self.assertEqual(config["quickstart"]["chatgpt_remote"]["verification_tools"], ["get_index_status"])
        self.assertTrue(config["quickstart"]["chatgpt_remote"]["requires_reachable_https"])
        self.assertTrue(config["quickstart"]["chatgpt_remote"]["https_endpoint_ready"])
        self.assertIn("openai_secure_tunnel", config["quickstart"]["chatgpt_remote"]["connection_options"])
        self.assertEqual(config["quickstart"]["chatgpt_desktop_local"]["profile"], "chatgpt-desktop-local")
        self.assertTrue(config["quickstart"]["chatgpt_desktop_local"]["conversation_attachment_unverified"])
        self.assertEqual(config["quickstart"]["openai_secure_tunnel"]["tunnel_id_env"], "OPENAI_TUNNEL_ID")
        self.assertEqual(config["quickstart"]["openai_secure_tunnel"]["setup_state"], "manual_setup_required")
        self.assertNotIn("--tool-profile chatgpt-data", config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
        self.assertIn("--no-warm-cache", config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
        self.assertIn("--flat-storage", config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
        self.assertIn("--fail-on-warning", config["quickstart"]["openai_secure_tunnel"]["commands"]["readiness"]["args"])
        self.assertIn(
            "--audit-index-visibility",
            config["quickstart"]["openai_secure_tunnel"]["commands"]["readiness"]["args"],
        )
        self.assertEqual(config["quickstart"]["claude_api"]["copy_fields"], ["mcp_servers", "tools", "betas"])
        self.assertIn("--http-bearer-token-env", config["quickstart"]["run_http_server"]["args"])
        self.assertIn("--auth-issuer-url", config["quickstart"]["run_http_server"]["args"])
        self.assertIn("--no-warm-cache", config["quickstart"]["run_http_server"]["args"])
        self.assertIn("https://mcp.example.go.kr", config["quickstart"]["run_http_server"]["args"])
        self.assertEqual("full", config["quickstart"]["run_chatgpt_data_server"]["tool_profile"])
        self.assertIn("--no-warm-cache", config["quickstart"]["run_chatgpt_data_server"]["args"])
        self.assertIn("copy_paste", config["quickstart"])
        self.assertIn("claude mcp add-json", config["quickstart"]["copy_paste"]["claude_code_stdio_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["claude_code_stdio_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["run_local_stdio_server_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn('Assert-EnvVar "MCP_AUTH_TOKEN"', config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("reg-rag-mcp-doctor --client-profile bundle", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("--audit-index-visibility", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn('Assert-Command "reg-rag-mcp-doctor"', config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("if ($LASTEXITCODE -ne 0)", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertNotIn("--tool-profile chatgpt-data", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("reg-rag-mcp-doctor --client-profile chatgpt-remote", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("--audit-index-visibility", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("tunnel-client init", config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('$ErrorActionPreference = "Stop"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Assert-Command "tunnel-client"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Assert-EnvVar "CONTROL_PLANE_API_KEY"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Assert-EnvVar "OPENAI_TUNNEL_ID"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn("if ($LASTEXITCODE -ne 0)", config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn("reg-rag-mcp-doctor", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("--bundle-dir $BundleDir", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("--audit-index-visibility", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("--tenant-id tenant-a", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("mcp_connection_readiness.json", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("--out-json $DoctorReport", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertEqual("reg-rag-mcp-index-visibility", config["quickstart"]["audit_index_visibility"]["command"])
        self.assertIn("reg-rag-mcp-index-visibility", config["quickstart"]["copy_paste"]["audit_index_visibility_ps"])
        self.assertNotIn("--allow-local-only-bundle", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("chatgpt-tunnel", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertIn("Claude Desktop config updated", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertIn("Claude Code CLI was not found", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertIn("No ChatGPT remote connector_url is ready", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertTrue(config["quickstart"]["warnings"])

    def test_builds_claude_api_connector_config(self) -> None:
        config = build_mcp_client_config(
            client_profile="claude-api",
            transport="streamable-http",
            public_url="https://mcp.example.go.kr",
            server_name="govreg-local",
        )

        self.assertEqual(config["mcp_servers"][0]["type"], "url")
        self.assertEqual(config["mcp_servers"][0]["url"], "https://mcp.example.go.kr/mcp")
        self.assertEqual(config["mcp_servers"][0]["authorization_token_env"], "MCP_AUTH_TOKEN")
        self.assertIn("--no-warm-cache", config["server_start"]["args"])
        self.assertEqual(config["tools"][0]["type"], "mcp_toolset")
        self.assertEqual(config["tools"][0]["mcp_server_name"], "govreg-local")
        self.assertIn("mcp-client-2025-11-20", config["betas"])
        self.assertIn("--auth-issuer-url", config["server_start"]["args"])
        self.assertIn("https://mcp.example.go.kr", config["server_start"]["args"])
        self.assertIn("connection_steps", config)

    def test_bundle_without_public_url_marks_remote_profiles_not_ready(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        self.assertFalse(config["chatgpt"]["ready"])
        self.assertIn("public_url_https_mcp_endpoint", config["chatgpt"]["missing"])
        self.assertFalse(config["chatgpt"]["chatgpt_setup"]["https_endpoint_ready"])
        self.assertFalse(config["quickstart"]["chatgpt_remote"]["https_endpoint_ready"])
        self.assertFalse(config["claude_api"]["ready"])
        self.assertEqual([], config["claude_api"]["mcp_servers"])
        self.assertIsNone(config["quickstart"]["chatgpt_remote"]["connector_url"])
        self.assertIsNone(config["quickstart"]["claude_api"]["mcp_server_url"])
        self.assertIsNone(config["quickstart"]["copy_paste"]["claude_code_http_ps"])
        self.assertIn("--allow-local-only-bundle", config["quickstart"]["copy_paste"]["doctor_ps"])

    def test_bundle_can_enforce_min_visible_records_in_doctor_commands(self) -> None:
        config = build_mcp_client_config(
            client_profile="bundle",
            tenant_id="tenant-a",
            min_visible_records=5000,
        )

        self.assertIn("5000", config["quickstart"]["audit_index_visibility"]["args"])
        self.assertIn("--min-visible-records 5000", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("--min-visible-records 5000", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn(
            "5000",
            config["quickstart"]["openai_secure_tunnel"]["commands"]["readiness"]["args"],
        )

    def test_writes_copy_paste_setup_bundle(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr",
        )

        with tempfile.TemporaryDirectory() as tmp:
            files = write_mcp_setup_bundle(
                config,
                tmp,
                server_name="govreg-local",
                preferred_python=sys.executable,
                preferred_project_root=Path(__file__).resolve().parents[1],
            )
            output_dir = Path(tmp)
            generated_names = {path.name for path in output_dir.iterdir() if path.is_file()}

            self.assertTrue(REQUIRED_SETUP_BUNDLE_FILES.issubset(generated_names))
            self.assertEqual(ALL_SETUP_BUNDLE_FILES, generated_names)
            self.assertTrue((output_dir / "README.md").exists())
            self.assertTrue((output_dir / "README.ko.md").exists())
            self.assertTrue((output_dir / "bundle_status.json").exists())
            self.assertTrue((output_dir / "codex_config_snippet.toml").exists())
            self.assertTrue((output_dir / "claude_desktop_config.json").exists())
            self.assertTrue((output_dir / "chatgpt_connector.json").exists())
            self.assertTrue((output_dir / "chatgpt_desktop_local_mcp.json").exists())
            self.assertTrue((output_dir / "claude_api_fragment.json").exists())
            self.assertTrue((output_dir / "run_http_server.ps1").exists())
            self.assertTrue((output_dir / "run_chatgpt_data_server.ps1").exists())
            self.assertTrue((output_dir / "run_openai_secure_tunnel.ps1").exists())
            self.assertTrue((output_dir / "doctor_mcp_connection.ps1").exists())
            self.assertTrue((output_dir / "validate_client_config_smoke.ps1").exists())
            self.assertTrue((output_dir / "connect_mcp_client.ps1").exists())
            self.assertTrue((output_dir / "MCP 사용 시작하기.txt").exists())
            self.assertTrue((output_dir / "설치 후 MCP 사용 방법 보기.bat").exists())
            self.assertTrue((output_dir / "Codex 플러그인 MCP 입력값.txt").exists())
            self.assertTrue((output_dir / "Codex에 연결하기.bat").exists())
            self.assertTrue((output_dir / "ChatGPT Desktop에 연결하기.bat").exists())
            self.assertTrue((output_dir / "Claude Desktop에 연결하기.bat").exists())
            self.assertTrue((output_dir / "Claude Code에 연결하기.bat").exists())
            self.assertTrue((output_dir / "ChatGPT HTTPS에 연결하기.bat").exists())
            self.assertTrue((output_dir / "ChatGPT 보안 Tunnel에 연결하기.bat").exists())
            self.assertTrue((output_dir / "Claude HTTPS에 연결하기.bat").exists())
            self.assertTrue((output_dir / "연결 상태 확인하기.bat").exists())
            self.assertTrue((output_dir / "install_local_package.ps1").exists())
            self.assertTrue((output_dir / "claude_code_add_stdio.ps1").exists())
            self.assertIn("claude_code_stdio", files)
            self.assertIn("codex_config", files)
            self.assertIn("client_config_smoke", files)
            self.assertIn("chatgpt_desktop_local", files)
            self.assertIn("connect", files)
            self.assertIn("usage_guide", files)
            self.assertIn("usage_guide_bat", files)
            self.assertIn("codex_plugin_guide", files)
            self.assertIn("connect_codex_bat", files)
            self.assertIn("connect_chatgpt_desktop_bat", files)
            self.assertIn("connect_claude_desktop_bat", files)
            self.assertIn("connect_claude_code_bat", files)
            self.assertIn("connect_chatgpt_https_bat", files)
            self.assertIn("connect_chatgpt_tunnel_bat", files)
            self.assertIn("connect_claude_https_bat", files)
            self.assertIn("doctor_bat", files)
            self.assertIn("install", files)
            manifest = (output_dir / "manifest.json").read_text(encoding="utf-8")
            bundle_status = json.loads((output_dir / "bundle_status.json").read_text(encoding="utf-8"))
            readme = (output_dir / "README.md").read_text(encoding="utf-8")
            readme_ko = (output_dir / "README.ko.md").read_text(encoding="utf-8")
            codex_snippet = tomllib.loads((output_dir / "codex_config_snippet.toml").read_text(encoding="utf-8"))
            codex_server = codex_snippet["mcp_servers"]["govreg-local"]
            codex_args = codex_server["args"]
            self.assertTrue((output_dir / "run_mcp_stdio_server.ps1").exists())
            self.assertEqual("powershell.exe", codex_server["command"])
            self.assertEqual(str(output_dir.resolve()), codex_server["cwd"])
            self.assertIn("-NoProfile", codex_args)
            self.assertIn("-File", codex_args)
            self.assertIn(str((output_dir / "run_mcp_stdio_server.ps1").resolve()), codex_args)
            self.assertIn(str((output_dir / "data").resolve()), codex_args)
            self.assertIn("--transport", codex_args)
            self.assertIn("stdio", codex_args)
            self.assertIn("--no-warm-cache", codex_args)
            self.assertIn("--flat-storage", codex_args)
            stdio_launcher = (output_dir / "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")
            self.assertIn('Get-Command "reg-rag-mcp-server"', stdio_launcher)
            self.assertIn("scripts\\run_regulation_mcp.py", stdio_launcher)
            self.assertIn(f"$PreferredPython = '{sys.executable}'", stdio_launcher)
            self.assertIn(
                f"$PreferredProjectRoot = '{Path(__file__).resolve().parents[1]}'",
                stdio_launcher,
            )
            self.assertIn("if (-not $ProjectRoot -and $PreferredProjectRoot)", stdio_launcher)
            self.assertLess(
                stdio_launcher.index("$ProjectRoot = Find-ProjectRoot"),
                stdio_launcher.index('$ConsoleCommand = Get-Command "reg-rag-mcp-server"'),
            )
            self.assertIn("install_local_package.ps1 once", stdio_launcher)
            self.assertIn(
                "claude mcp add-json --scope local govreg-local",
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'claude mcp remove "govreg-local" --scope local',
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "reg-rag-mcp-doctor --client-profile bundle --transport stdio",
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "--forbid-smoke-docs",
                (output_dir / "run_local_stdio_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "reg-rag-mcp-doctor --client-profile bundle --transport stdio",
                (output_dir / "run_local_stdio_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn('"manifest": "manifest.json"', manifest)
            self.assertIn('"bundle_status": "bundle_status.json"', manifest)
            self.assertIn('"readme": "README.md"', manifest)
            self.assertIn('"readme_ko": "README.ko.md"', manifest)
            self.assertIn('"codex_config": "codex_config_snippet.toml"', manifest)
            self.assertIn('"chatgpt_desktop_local": "chatgpt_desktop_local_mcp.json"', manifest)
            self.assertIn('"stdio_launcher": "run_mcp_stdio_server.ps1"', manifest)
            self.assertIn('"client_config_smoke": "validate_client_config_smoke.ps1"', manifest)
            self.assertIn('"mode": "secure_mcp_tunnel"', manifest)
            self.assertIn('"ready": "manual_setup_required"', manifest)
            self.assertIn('"primary_file": "ChatGPT 보안 Tunnel에 연결하기.bat"', manifest)
            self.assertIn('"config_file": "run_openai_secure_tunnel.ps1"', manifest)
            self.assertIn('"connect": "connect_mcp_client.ps1"', manifest)
            self.assertIn('"usage_guide": "MCP 사용 시작하기.txt"', manifest)
            self.assertIn('"usage_guide_bat": "설치 후 MCP 사용 방법 보기.bat"', manifest)
            self.assertIn('"codex_plugin_guide": "Codex 플러그인 MCP 입력값.txt"', manifest)
            self.assertIn('"connect_codex_bat": "Codex에 연결하기.bat"', manifest)
            self.assertIn('"connect_chatgpt_desktop_bat": "ChatGPT Desktop에 연결하기.bat"', manifest)
            self.assertIn('"connect_claude_desktop_bat": "Claude Desktop에 연결하기.bat"', manifest)
            self.assertIn('"connect_claude_code_bat": "Claude Code에 연결하기.bat"', manifest)
            self.assertIn('"connect_chatgpt_https_bat": "ChatGPT HTTPS에 연결하기.bat"', manifest)
            self.assertIn('"connect_chatgpt_tunnel_bat": "ChatGPT 보안 Tunnel에 연결하기.bat"', manifest)
            self.assertIn('"connect_claude_https_bat": "Claude HTTPS에 연결하기.bat"', manifest)
            self.assertIn('"doctor_bat": "연결 상태 확인하기.bat"', manifest)
            self.assertIn('"primary_file": "Codex에 연결하기.bat"', manifest)
            self.assertIn('"config_file": "codex_config_snippet.toml"', manifest)
            self.assertIn('"client": "ChatGPT Desktop"', manifest)
            self.assertIn('"profile": "chatgpt-desktop-local"', manifest)
            self.assertIn('"profile": "chatgpt-remote"', manifest)
            self.assertIn('"primary_file": "ChatGPT Desktop에 연결하기.bat"', manifest)
            self.assertIn('"primary_file": "Claude Desktop에 연결하기.bat"', manifest)
            self.assertIn('"config_file": "claude_desktop_config.json"', manifest)
            self.assertIn('"install": "install_local_package.ps1"', manifest)
            self.assertIn("Run `connect_mcp_client.ps1`", readme)
            self.assertIn("Use `Codex에 연결하기.bat` for direct Codex CLI compatibility", readme)
            self.assertIn("double-click `Claude Desktop에 연결하기.bat`", readme)
            self.assertIn("@govreg-local MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", readme)
            self.assertIn("bundle_status.json", readme)
            self.assertIn("codex_config_snippet.toml", readme)
            self.assertIn("ChatGPT Desktop에 연결하기.bat", readme)
            self.assertIn("validate_client_config_smoke.ps1", readme)
            self.assertIn("recommended_smoke_query", readme)
            self.assertIn("checkout before any older global console command", readme)
            self.assertIn("-ValidateClaudeDesktop", readme)
            self.assertIn("--codex-config", readme)
            self.assertIn("--claude-desktop-config", readme)
            self.assertIn("Get-Content -Encoding UTF8", readme)
            self.assertIn("approval journals and vector IDs are keyed", readme)
            self.assertIn("reg-rag-mcp-index-visibility", readme)
            self.assertIn("reg-rag-mcp-index-visibility", readme_ko)
            self.assertIn("pip install -e .", readme)
            self.assertIn("bundled `reg_rag_preprocessor-*.whl`", readme)
            self.assertIn("--include-wheel", readme)
            self.assertIn("https://help.openai.com/en/articles/20001256-plugins-in-codex", readme)
            self.assertIn("https://modelcontextprotocol.io/specification/2025-11-25/basic/transports", readme)
            self.assertIn("https://docs.anthropic.com/en/docs/claude-code/mcp", readme)
            self.assertIn("MCP 연결 번들", readme_ko)
            self.assertIn("bundle_status.json", readme_ko)
            self.assertIn("codex_config_snippet.toml", readme_ko)
            self.assertIn("validate_client_config_smoke.ps1", readme_ko)
            self.assertIn("recommended_smoke_query", readme_ko)
            self.assertIn("오래된 전역 콘솔 명령보다 먼저 실행", readme_ko)
            self.assertIn("connect_mcp_client.ps1", readme_ko)
            self.assertIn("Codex에 연결하기.bat", readme_ko)
            self.assertIn("ChatGPT Desktop에 연결하기.bat", readme_ko)
            self.assertIn("Claude Desktop에 연결하기.bat", readme_ko)
            self.assertIn("Claude Code에 연결하기.bat", readme_ko)
            self.assertIn("ChatGPT HTTPS에 연결하기.bat", readme_ko)
            self.assertIn("ChatGPT 보안 Tunnel에 연결하기.bat", readme_ko)
            self.assertIn("Claude HTTPS에 연결하기.bat", readme_ko)
            self.assertIn("연결 상태 확인하기.bat", readme_ko)
            self.assertIn("@govreg-local MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", readme_ko)
            self.assertIn("-ValidateClaudeDesktop", readme_ko)
            self.assertIn("--codex-config", readme_ko)
            self.assertIn("--claude-desktop-config", readme_ko)
            self.assertIn("Get-Content -Encoding UTF8", readme_ko)
            self.assertIn("승인 저널과 벡터 ID", readme_ko)
            self.assertIn("install_local_package.ps1", readme_ko)
            self.assertIn("pip install -e .", readme_ko)
            self.assertIn("https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta%29", readme_ko)
            self.assertIn("https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector", readme_ko)
            self.assertIn("ChatGPT", readme_ko)
            self.assertIn("| Codex CLI | local_stdio | true | `Codex에 연결하기.bat` |", readme)
            self.assertIn("| Claude Desktop | local_stdio | true | `Claude Desktop에 연결하기.bat` |", readme)
            self.assertIn("| Claude Code | local_stdio | true | `Claude Code에 연결하기.bat` |", readme)
            self.assertIn("| ChatGPT | secure_mcp_tunnel | manual_setup_required | `ChatGPT 보안 Tunnel에 연결하기.bat` |", readme)
            self.assertIn("| ChatGPT Desktop | local_stdio | plugin_registration_required | `ChatGPT Desktop에 연결하기.bat` |", readme)
            chatgpt_desktop_local = json.loads((output_dir / "chatgpt_desktop_local_mcp.json").read_text(encoding="utf-8"))
            self.assertEqual("chatgpt-desktop-local", chatgpt_desktop_local["profile"])
            self.assertEqual("ChatGPT Desktop", chatgpt_desktop_local["client"])
            self.assertFalse(chatgpt_desktop_local["chatgpt_direct_local_mcp_supported"])
            self.assertTrue(chatgpt_desktop_local["conversation_attachment_unverified"])
            self.assertEqual("local_stdio", chatgpt_desktop_local["mode"])
            self.assertEqual("powershell.exe", chatgpt_desktop_local["ui_fields"]["command"])
            self.assertEqual(str(output_dir.resolve()), chatgpt_desktop_local["ui_fields"]["cwd"])
            self.assertIn(
                str((output_dir / "run_mcp_stdio_server.ps1").resolve()),
                chatgpt_desktop_local["ui_fields"]["args"],
            )
            self.assertIn(str((output_dir / "data").resolve()), chatgpt_desktop_local["ui_fields"]["args"])
            chatgpt_desktop_bat = (output_dir / "ChatGPT Desktop에 연결하기.bat").read_text(encoding="utf-8")
            self.assertIn("-Target chatgpt-desktop-local -InstallChatGptDesktopPlugin", chatgpt_desktop_bat)
            self.assertIn("+ > 더 보기 > govreg-local", chatgpt_desktop_bat)
            self.assertIn("@govreg-local MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", chatgpt_desktop_bat)
            self.assertIn("[다음 단계]", chatgpt_desktop_bat)
            self.assertIn("@govreg-local MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", chatgpt_desktop_bat)
            usage_guide = (output_dir / "MCP 사용 시작하기.txt").read_text(encoding="utf-8")
            self.assertIn("등록된 MCP 이름: govreg-local", usage_guide)
            self.assertIn("codex mcp list", usage_guide)
            self.assertIn("claude mcp list", usage_guide)
            self.assertIn("같은 MCP 업데이트", usage_guide)
            plugin_guide = (output_dir / "Codex 플러그인 MCP 입력값.txt").read_text(encoding="utf-8")
            self.assertIn("MCP 이름: govreg-local", plugin_guide)
            self.assertIn("실행 명령: powershell.exe", plugin_guide)
            self.assertIn(f"작업 중인 디렉터리: {output_dir.resolve()}", plugin_guide)
            self.assertIn(str((output_dir / "run_mcp_stdio_server.ps1").resolve()), plugin_guide)
            self.assertIn('start "" notepad.exe "%~dp0MCP 사용 시작하기.txt"', (output_dir / "설치 후 MCP 사용 방법 보기.bat").read_text(encoding="utf-8"))
            self.assertIn(
                'Assert-EnvVar "MCP_AUTH_TOKEN"',
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "$script:McpPreferredPython",
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "function reg-rag-mcp-server",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertNotIn("<strong-token>", (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"))
            self.assertIn(
                '$ErrorActionPreference = "Stop"',
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-Command "reg-rag-mcp-server"',
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "reg-rag-mcp-doctor --client-profile bundle",
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "--tool-profile full",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "reg-rag-mcp-doctor --client-profile chatgpt-remote",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '$ErrorActionPreference = "Stop"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-Command "tunnel-client"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-EnvVar "CONTROL_PLANE_API_KEY"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-EnvVar "OPENAI_TUNNEL_ID"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "<runtime-api-key>",
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "tunnel-client run",
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn("reg-rag-mcp-doctor", (output_dir / "doctor_mcp_connection.ps1").read_text(encoding="utf-8"))
            self.assertIn("--bundle-dir $BundleDir", (output_dir / "doctor_mcp_connection.ps1").read_text(encoding="utf-8"))
            wizard = (output_dir / "connect_mcp_client.ps1").read_text(encoding="utf-8")
            self.assertIn(
                '[ValidateSet("menu", "install", "claude-desktop", "claude-code", "codex", "chatgpt-desktop-local", "chatgpt-remote"',
                wizard,
            )
            self.assertIn("function Show-ChatGptDesktop", wizard)
            self.assertIn("ChatGPT Secure MCP Tunnel", wizard)
            self.assertIn("Claude API MCP server URL", wizard)
            self.assertIn("Get-ClaudeDesktopConfigPath", wizard)
            self.assertIn("Get-CodexConfigPath", wizard)
            self.assertIn("[switch]$ValidateClaudeDesktop", wizard)
            self.assertIn("[switch]$InstallCodex", wizard)
            self.assertIn("Test-ClaudeDesktopConfig", wizard)
            self.assertIn("Install-CodexConfig", wizard)
            self.assertIn("Build-CodexConfigSnippet", wizard)
            self.assertIn("Get-BundleServerEntry", wizard)
            self.assertIn("Claude Desktop config is not valid JSON", wizard)
            self.assertIn("pasting the whole generated JSON as a second top-level object", wizard)
            self.assertIn("Claude Code CLI was not found", wizard)
            self.assertIn("reg-rag-mcp-index-visibility", wizard)
            self.assertIn("Get-McpCommandInvocation", wizard)
            self.assertIn("Invoke-McpCommand", wizard)
            self.assertIn(str(sys.executable), wizard)
            self.assertIn(str(Path(__file__).resolve().parents[1]), wizard)
            self.assertIn("Run-LocalStdioDoctor", wizard)
            self.assertIn("$LocalStdioDoctorArgs", wizard)
            self.assertIn("--allow-local-only-bundle", wizard)
            self.assertIn("--transport', 'stdio", wizard)
            self.assertIn('[string]$CodexConfigPath = ""', wizard)
            self.assertLess(wizard.index("$env:USERPROFILE"), wizard.index("$env:CODEX_HOME"))
            self.assertIn("$LegacyDefaultForSameProfile", wizard)
            self.assertIn("Removed duplicate entries for this bundle", wizard)
            self.assertIn("Codex config verification failed after writing", wizard)
            codex_bat = (output_dir / "Codex에 연결하기.bat").read_text(encoding="utf-8")
            claude_desktop_bat = (output_dir / "Claude Desktop에 연결하기.bat").read_text(encoding="utf-8")
            claude_code_bat = (output_dir / "Claude Code에 연결하기.bat").read_text(encoding="utf-8")
            doctor_bat = (output_dir / "연결 상태 확인하기.bat").read_text(encoding="utf-8")
            for launcher in [codex_bat, claude_desktop_bat, claude_code_bat, doctor_bat]:
                self.assertIn("@echo off", launcher)
                self.assertIn("chcp 65001 >nul", launcher)
                self.assertIn('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0', launcher)
                self.assertIn("pause", launcher)
            self.assertIn("-Target doctor", doctor_bat)
            self.assertIn('connect_mcp_client.ps1" -Target codex -InstallCodex', codex_bat)
            self.assertIn('connect_mcp_client.ps1" -Target claude-desktop -InstallClaudeDesktop', claude_desktop_bat)
            self.assertIn('connect_mcp_client.ps1" -Target claude-code', claude_code_bat)
            self.assertIn('connect_mcp_client.ps1" -Target doctor', doctor_bat)
            self.assertIn(
                "https://mcp.example.go.kr/mcp",
                (output_dir / "chatgpt_connector.json").read_text(encoding="utf-8"),
            )
            for script_name in [
                "claude_code_add_stdio.ps1",
                "run_local_stdio_server.ps1",
                "run_http_server.ps1",
                "run_chatgpt_data_server.ps1",
                "run_openai_secure_tunnel.ps1",
                "doctor_mcp_connection.ps1",
            ]:
                script = (output_dir / script_name).read_text(encoding="utf-8")
                self.assertIn('$BundleDataDir = Join-Path $BundleDir "data"', script)
                self.assertIn("--data-dir $BundleDataDir", script)
            claude_code_stdio = (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8")
            self.assertIn("$ClaudeCodeArgs = @(", claude_code_stdio)
            self.assertIn('$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"', claude_code_stdio)
            self.assertIn('command = "powershell.exe"', claude_code_stdio)
            self.assertIn("$ClaudeCodeJson = $ClaudeCodeConfig | ConvertTo-Json", claude_code_stdio)
            self.assertIn("--no-warm-cache", claude_code_stdio)
            self.assertIn(
                "--no-warm-cache",
                (output_dir / "run_local_stdio_server.ps1").read_text(encoding="utf-8"),
            )
            validate_script = (output_dir / "validate_mcp_smoke.ps1").read_text(encoding="utf-8")
            self.assertIn("reg-rag-mcp-transport-smoke", validate_script)
            self.assertIn("mcp_runtime_manifest.json", validate_script)
            self.assertIn("recommended_smoke_query", validate_script)
            self.assertIn("-Encoding UTF8", validate_script)
            self.assertIn("--no-warm-cache", validate_script)
            client_config_smoke_script = (output_dir / "validate_client_config_smoke.ps1").read_text(encoding="utf-8")
            self.assertIn("reg-rag-mcp-client-config-smoke", client_config_smoke_script)
            self.assertIn("mcp_client_config_smoke.json", client_config_smoke_script)
            self.assertIn("--codex-config", client_config_smoke_script)
            self.assertIn("--claude-desktop-config", client_config_smoke_script)
            self.assertIn("Set-McpBundlePaths", client_config_smoke_script)
            self.assertIn("Write-CodexBundleConfig", client_config_smoke_script)
            self.assertIn('$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"', client_config_smoke_script)
            self.assertIn("Set-McpBundlePaths", wizard)
            self.assertIn(
                '$Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")',
                wizard,
            )
            self.assertIn("-Encoding UTF8 | ConvertFrom-Json", wizard)
            self.assertEqual("mcp_bundle_status", bundle_status["report_type"])
            self.assertFalse(bundle_status["runtime_data_ready"])
            self.assertEqual(0, bundle_status["record_count"])
            self.assertEqual("validate_client_config_smoke.ps1", bundle_status["first_use"]["client_config_smoke_script"])
            self.assertIn("mcp_connection_readiness.json", bundle_status["stale_status_reports_cleared_on_generation"])

    @unittest.skipUnless(os.name == "nt", "Codex Windows installer script test")
    def test_codex_installer_replaces_same_profile_legacy_default_and_verifies_paths(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
            profile_id="institution-test",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "AKS_MCP"
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-output-ok")\nraise SystemExit(0)\n', encoding="utf-8")
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aks_mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            moved_bundle_dir = root / "다른 위치" / "Renamed Bundle"
            moved_bundle_dir.parent.mkdir(parents=True)
            bundle_dir.rename(moved_bundle_dir)
            moved_connect_script = moved_bundle_dir / Path(files["connect"]).name
            codex_config = root / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True, exist_ok=True)
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        "args = ['--profile-id', 'institution-test', 'C:\\old-bundle']",
                        "",
                        "[mcp_servers.aks_mcp]",
                        'command = "aksmcp"',
                        "",
                        "[mcp_servers.other]",
                        'command = "other-server"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            env["PATH"] = os.pathsep.join(
                [str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_exe.parent)]
            )

            completed = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(moved_connect_script),
                    "-Target",
                    "codex",
                    "-InstallCodex",
                    "-CodexConfigPath",
                    str(codex_config),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            installed = tomllib.loads(codex_config.read_text(encoding="utf-8-sig"))
            self.assertEqual({"aks_mcp", "other"}, set(installed["mcp_servers"]))
            self.assertEqual("other-server", installed["mcp_servers"]["other"]["command"])
            aks_entry = installed["mcp_servers"]["aks_mcp"]
            self.assertEqual("powershell.exe", aks_entry["command"])
            self.assertIn(str((moved_bundle_dir / "run_mcp_stdio_server.ps1").resolve()), aks_entry["args"])
            self.assertIn(str((moved_bundle_dir / "data").resolve()), aks_entry["args"])
            self.assertNotIn(str(bundle_dir.resolve()), json.dumps(aks_entry, ensure_ascii=False))
            self.assertTrue(list(codex_config.parent.glob("config.toml.bak-*")))
            self.assertIn("doctor-output-ok", completed.stdout)
            self.assertIn("Removed duplicate entries for this bundle: govreg-local", completed.stdout)
            self.assertIn("Verified MCP server name and bundle paths: aks_mcp", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "Claude Desktop Windows installer script test")
    def test_claude_desktop_installer_replaces_legacy_profile_and_verifies_paths(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
            profile_id="institution-test",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("claude-doctor-ok")\n', encoding="utf-8")
            bundle_dir = root / "AKS_MCP"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aks_mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            appdata_dir = root / "AppData" / "Roaming"
            claude_config = appdata_dir / "Claude" / "claude_desktop_config.json"
            claude_config.parent.mkdir(parents=True)
            claude_config.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "mcpServers": {
                            "govreg-local": {
                                "command": "powershell.exe",
                                "args": ["--profile-id", "institution-test", r"C:\old-bundle"],
                            },
                            "aks_mcp": {"command": "aksmcp", "args": []},
                            "other": {"command": "other-server", "args": []},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            env["APPDATA"] = str(appdata_dir)
            env["PATH"] = os.pathsep.join(
                [str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_exe.parent)]
            )

            completed = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "claude-desktop",
                    "-InstallClaudeDesktop",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            installed = json.loads(claude_config.read_text(encoding="utf-8-sig"))
            self.assertEqual("dark", installed["theme"])
            self.assertEqual({"aks_mcp", "other"}, set(installed["mcpServers"]))
            aks_args = installed["mcpServers"]["aks_mcp"]["args"]
            self.assertIn(str((bundle_dir / "run_mcp_stdio_server.ps1").resolve()), aks_args)
            self.assertIn(str((bundle_dir / "data").resolve()), aks_args)
            self.assertTrue(list(claude_config.parent.glob("claude_desktop_config.json.bak-*")))
            self.assertIn("claude-doctor-ok", completed.stdout)
            self.assertIn(
                "Removed duplicate Claude Desktop entries for this bundle: govreg-local",
                completed.stdout,
            )
            self.assertIn("Verified MCP server name and bundle paths: aks_mcp", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop BAT automation test")
    def test_chatgpt_desktop_bat_registers_plugin_idempotently_from_korean_space_path(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "가짜 프로젝트"
            scripts_dir = fake_project_root / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "check_mcp_connection_readiness.py").write_text(
                'print("bat-doctor-ok")\nraise SystemExit(0)\n',
                encoding="utf-8",
            )
            (scripts_dir / "run_mcp_client_config_smoke.py").write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "out = pathlib.Path(sys.argv[sys.argv.index('--out-json') + 1])",
                        "payload = {key: True for key in ('launcher_ready', 'process_started', 'mcp_initialized', 'tools_discovered', 'end_to_end_verified')}",
                        "payload['passed'] = True",
                        "out.write_text(json.dumps(payload), encoding='utf-8')",
                        "print('bat-client-smoke-ok')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            bundle_dir = root / "한글 경로" / "MCP 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            codex_log = root / "codex-calls.txt"
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\necho %*>>\"%CODEX_TEST_LOG%\"\r\nexit /b 0\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CODEX_TEST_LOG"] = str(codex_log)
            cmd_exe = windows_dir / "System32" / "cmd.exe"
            bat_path = Path(files["connect_chatgpt_desktop_bat"])

            completed_runs = []
            for _ in range(2):
                completed_runs.append(
                    subprocess.run(
                        [str(cmd_exe), "/d", "/c", str(bat_path)],
                        cwd=root,
                        env=env,
                        input="\n",
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                )

            for completed in completed_runs:
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("bat-doctor-ok", completed.stdout)
                self.assertIn("bat-client-smoke-ok", completed.stdout)
                self.assertIn("@aksmcp MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", completed.stdout)
            calls = codex_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, sum("plugin marketplace add" in call for call in calls))
            self.assertEqual(2, sum("plugin add aksmcp@aksmcp-local" in call for call in calls))
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8-sig"))
            self.assertTrue(status["launcher_ready"])
            self.assertTrue(status["process_started"])
            self.assertTrue(status["mcp_initialized"])
            self.assertTrue(status["tools_discovered"])
            self.assertTrue(status["plugin_registered"])
            self.assertTrue(status["conversation_attachment_unverified"])
            self.assertTrue(status["end_to_end_verified"])
            plugin_config = json.loads(
                (bundle_dir / "chatgpt-desktop-local-plugin" / "plugins" / "aksmcp" / ".mcp.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            plugin_args = plugin_config["mcpServers"]["aksmcp"]["args"]
            self.assertIn(str((bundle_dir / "run_mcp_stdio_server.ps1").resolve()), plugin_args)
            self.assertIn(str((bundle_dir / "data").resolve()), plugin_args)

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop BAT failure-state test")
    def test_chatgpt_desktop_bat_does_not_mark_failed_plugin_registration_connected(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-before-plugin-failure")\n', encoding="utf-8")
            bundle_dir = root / "한글 실패 경로" / "MCP 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                '@echo off\r\nif "%1 %2"=="plugin add" exit /b 9\r\nexit /b 0\r\n',
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_dir)]
            )
            completed = subprocess.run(
                [str(windows_dir / "System32" / "cmd.exe"), "/d", "/c", files["connect_chatgpt_desktop_bat"]],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8-sig"))
            self.assertFalse(status["plugin_registered"])
            self.assertFalse(status["mcp_initialized"])
            self.assertFalse(status["tools_discovered"])
            self.assertFalse(status["end_to_end_verified"])
            self.assertTrue(status["conversation_attachment_unverified"])

    @unittest.skipUnless(os.name == "nt", "Claude Desktop damaged bundle JSON recovery test")
    def test_claude_desktop_bat_recovers_damaged_bundle_json_in_korean_path(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("claude-recovery-doctor-ok")\n', encoding="utf-8")
            bundle_dir = root / "한글 경로" / "Claude MCP 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            (bundle_dir / "claude_desktop_config.json").write_text(
                r'{"mcpServers":{"aksmcp":{"command":"powershell.exe","args":["C:\bad\data"]}}}',
                encoding="utf-8",
            )
            appdata_dir = root / "사용자 AppData" / "Roaming"
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["APPDATA"] = str(appdata_dir)
            env["PATH"] = os.pathsep.join(
                [str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_dir)]
            )
            completed = subprocess.run(
                [str(windows_dir / "System32" / "cmd.exe"), "/d", "/c", files["connect_claude_desktop_bat"]],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("recovering the MCP entry", completed.stdout)
            recovered_source = json.loads(
                (bundle_dir / "claude_desktop_config.json").read_text(encoding="utf-8-sig")
            )
            installed_path = appdata_dir / "Claude" / "claude_desktop_config.json"
            installed = json.loads(installed_path.read_text(encoding="utf-8-sig"))
            for payload in (recovered_source, installed):
                args = payload["mcpServers"]["aksmcp"]["args"]
                self.assertIn(str((bundle_dir / "run_mcp_stdio_server.ps1").resolve()), args)
                self.assertIn(str((bundle_dir / "data").resolve()), args)

    @unittest.skipUnless(os.name == "nt", "Windows stdio launcher fallback test")
    def test_stdio_launcher_uses_generated_project_runtime_without_path_command(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_server = fake_project_root / "scripts" / "run_regulation_mcp.py"
            fake_server.parent.mkdir(parents=True)
            fake_server.write_text('print("stdio-fallback-ok")\n', encoding="utf-8")
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.write_text('print("doctor-fallback-ok")\n', encoding="utf-8")
            bundle_dir = root / "standalone" / "AKS_MCP"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aks_mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            env["PATH"] = os.pathsep.join([str(windows_dir / "System32"), str(powershell_exe.parent)])

            completed = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["stdio_launcher"],
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            completed_doctor = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["doctor"],
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("stdio-fallback-ok", completed.stdout)
        self.assertEqual(0, completed_doctor.returncode, completed_doctor.stdout + completed_doctor.stderr)
        self.assertIn("doctor-fallback-ok", completed_doctor.stdout)

    def test_setup_bundle_scripts_use_bundle_local_data_dir(self) -> None:
        stale_data_dir = r"C:\stale\mcp_connection_bundle\data"
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            data_dir=stale_data_dir,
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr",
        )

        with tempfile.TemporaryDirectory() as tmp:
            write_mcp_setup_bundle(
                config,
                tmp,
                server_name="govreg-local",
                preferred_python=sys.executable,
                preferred_project_root=Path(__file__).resolve().parents[1],
            )
            output_dir = Path(tmp)

            for script_name in [
                "claude_code_add_stdio.ps1",
                "run_local_stdio_server.ps1",
                "run_http_server.ps1",
                "run_chatgpt_data_server.ps1",
                "run_openai_secure_tunnel.ps1",
                "doctor_mcp_connection.ps1",
            ]:
                script = (output_dir / script_name).read_text(encoding="utf-8")
                self.assertIn('$BundleDataDir = Join-Path $BundleDir "data"', script)
                self.assertIn("--data-dir $BundleDataDir", script)
                self.assertNotIn(stale_data_dir, script)

            claude_desktop = json.loads((output_dir / "claude_desktop_config.json").read_text(encoding="utf-8"))
            claude_args = claude_desktop["mcpServers"]["govreg-local"]["args"]
            stdio_launcher = (output_dir / "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")
            self.assertIn(str((output_dir / "data").resolve()), claude_args)
            self.assertNotIn(stale_data_dir, claude_args)
            bundle_config = json.loads((output_dir / "mcp_config.bundle.json").read_text(encoding="utf-8"))
            bundle_data_dir = str((output_dir / "data").resolve())
            claude_code_payload = json.loads(bundle_config["quickstart"]["claude_code"]["args"][3])
            self.assertIn(bundle_data_dir, claude_code_payload["args"])
            self.assertNotIn(stale_data_dir, bundle_config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
            self.assertIn(
                f"--data-dir {bundle_data_dir}",
                bundle_config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"],
            )
            self.assertNotIn(stale_data_dir, json.dumps(bundle_config, ensure_ascii=False))

            wizard = (output_dir / "connect_mcp_client.ps1").read_text(encoding="utf-8")
            self.assertIn("Get-BundleDataDir", wizard)
            self.assertIn("Set-McpBundlePaths", wizard)
            self.assertIn(
                '$Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")',
                wizard,
            )

    def test_setup_bundle_normalizes_python_stdio_client_configs(self) -> None:
        stale_data_dir = r"C:\stale\mcp_connection_bundle\data"
        stale_script = r"C:\stale\source\scripts\run_regulation_mcp.py"
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            data_dir=stale_data_dir,
            tenant_id="tenant-a",
        )
        server = config["claude_desktop"]["mcpServers"]["govreg-local"]
        server["command"] = sys.executable
        server["args"] = [
            stale_script,
            "--data-dir",
            stale_data_dir,
            "--tenant-id",
            "tenant-a",
            "--transport",
            "stdio",
            "--flat-storage",
            "--no-warm-cache",
        ]
        config["claude_code"]["command"] = sys.executable
        config["claude_code"]["args"] = list(server["args"])

        with tempfile.TemporaryDirectory() as tmp:
            write_mcp_setup_bundle(
                config,
                tmp,
                server_name="govreg-local",
                preferred_python=sys.executable,
                preferred_project_root=Path(__file__).resolve().parents[1],
            )
            output_dir = Path(tmp)
            launcher = str((output_dir / "run_mcp_stdio_server.ps1").resolve())
            bundle_data_dir = str((output_dir / "data").resolve())
            codex_snippet = tomllib.loads((output_dir / "codex_config_snippet.toml").read_text(encoding="utf-8"))
            codex_args = codex_snippet["mcp_servers"]["govreg-local"]["args"]
            claude_desktop = json.loads((output_dir / "claude_desktop_config.json").read_text(encoding="utf-8"))
            claude_args = claude_desktop["mcpServers"]["govreg-local"]["args"]
            stdio_launcher = (output_dir / "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")

        for args in (codex_args, claude_args):
            self.assertEqual("-File", args[3])
            self.assertEqual(launcher, args[4])
            self.assertIn(bundle_data_dir, args)
            self.assertNotIn(stale_script, args)
            self.assertNotIn(stale_data_dir, args)
        self.assertNotIn(stale_script, stdio_launcher)
        self.assertIn(f"$PreferredPython = '{sys.executable}'", stdio_launcher)

    def test_setup_bundle_clears_stale_status_reports(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            stale_readiness = output_dir / "mcp_connection_readiness.json"
            stale_smoke = output_dir / "mcp_transport_smoke.json"
            stale_readiness.write_text('{"effective_data_dir":"C:/old"}\n', encoding="utf-8")
            stale_smoke.write_text('{"passed":false}\n', encoding="utf-8")

            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            self.assertFalse(stale_readiness.exists())
            self.assertFalse(stale_smoke.exists())
            status = json.loads((output_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["runtime_data_ready"])
            self.assertEqual(
                ["mcp_connection_readiness.json", "mcp_transport_smoke.json"],
                status["stale_status_reports_cleared_on_generation"],
            )

    def test_zips_setup_bundle_for_operator_handoff(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")
            (output_dir / "operator_notes.tmp").write_text("do not ship", encoding="utf-8")
            zip_progress: list[tuple[int, int, str]] = []
            result = write_mcp_setup_bundle_zip(
                output_dir,
                zip_path,
                progress_callback=lambda current, total, name: zip_progress.append((current, total, name)),
            )

            self.assertEqual(str(zip_path), result)
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("connect_mcp_client.ps1", names)
            self.assertIn("Codex에 연결하기.bat", names)
            self.assertIn("ChatGPT Desktop에 연결하기.bat", names)
            self.assertIn("Claude Desktop에 연결하기.bat", names)
            self.assertIn("Claude Code에 연결하기.bat", names)
            self.assertIn("연결 상태 확인하기.bat", names)
            self.assertIn("install_local_package.ps1", names)
            self.assertIn("README.ko.md", names)
            self.assertIn("codex_config_snippet.toml", names)
            self.assertIn("chatgpt_desktop_local_mcp.json", names)
            self.assertIn("chatgpt_connector.json", names)
            self.assertIn("claude_api_fragment.json", names)
            self.assertIn("run_openai_secure_tunnel.ps1", names)
            self.assertNotIn("operator_notes.tmp", names)
            self.assertTrue(zip_progress)
            self.assertEqual(zip_progress[-1][0], zip_progress[-1][1])
            self.assertEqual(sorted(item[0] for item in zip_progress), [item[0] for item in zip_progress])

    def test_zip_includes_bundled_runtime_data_directory(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / "approval_snapshot.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_approval_snapshot",
                        "document_ids": ["doc-current"],
                        "entries": [{"document_id": "doc-current", "chunk_id": "chunk-1"}],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            write_mcp_setup_bundle_zip(output_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("data/vector_db/tenant-a/approved_vectors.jsonl", names)
            self.assertIn("data/repository/doc-current_chunks.json", names)
            self.assertIn("data/repository/approval_snapshot.json", names)

    def test_zip_rejects_cross_tenant_runtime_vector_store_files(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            tenant_a_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            tenant_b_vector_dir = output_dir / "data" / "vector_db" / "tenant-b"
            runtime_repository_dir.mkdir(parents=True)
            tenant_a_vector_dir.mkdir(parents=True)
            tenant_b_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"], tenant_id="tenant-a")
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            vector_record = json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n"
            (tenant_a_vector_dir / "approved_vectors.jsonl").write_text(vector_record, encoding="utf-8")
            (tenant_b_vector_dir / "approved_vectors.jsonl").write_text(vector_record, encoding="utf-8")
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("outside the manifest tenant", str(raised.exception))
        self.assertIn("tenant-b/approved_vectors.jsonl", str(raised.exception))

    def test_zip_rejects_runtime_data_without_manifest(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("missing a valid mcp_runtime_manifest.json", str(raised.exception))

    def test_zip_rejects_stale_runtime_document_artifacts(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / "doc-stale_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("stale document artifacts", str(raised.exception))

    def test_zip_excludes_runtime_locks_and_trace_logs(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / ".write.lock").write_text("", encoding="utf-8")
            (runtime_repository_dir / "api_audit.jsonl").write_text("{}\n", encoding="utf-8")
            (runtime_repository_dir / "rag_traces.jsonl").write_text("{}\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            write_mcp_setup_bundle_zip(output_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

        self.assertIn("data/repository/doc-current_chunks.json", names)
        self.assertNotIn("data/repository/.write.lock", names)
        self.assertNotIn("data/repository/api_audit.jsonl", names)
        self.assertNotIn("data/repository/rag_traces.jsonl", names)

    def test_zip_rejects_raw_runtime_repository_results_even_for_manifest_document(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / "doc-current_nodes.json").write_text("[]\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("raw preprocessing artifacts", str(raised.exception))
        self.assertIn("doc-current_nodes.json", str(raised.exception))

    def test_runtime_bundle_requires_kordoc_table_parser_evidence_for_hwp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata={})
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn("requires Kordoc table parsing", str(raised.exception))
        self.assertIn("rerun preprocessing", str(raised.exception))

    def test_runtime_bundle_exports_only_selected_document_set(self) -> None:
        records = [
            _runtime_export_record(
                "doc-a",
                "chunk-a",
                metadata={"regulation_id": "reg-personnel", "hierarchy_path": "인사규정 > 제1조"},
            ),
            _runtime_export_record("doc-b", "chunk-b"),
            _runtime_export_record(
                "doc-c",
                "chunk-c",
                metadata={"regulation_id": "reg-service", "hierarchy_path": "복무규정 > 제1조"},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            with patch(
                "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                return_value=records,
            ):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=Path(tmp) / "source",
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    profile_id="public_portal-test-profile",
                    document_ids=["doc-a", "doc-c"],
                    scope="selected_documents",
                    require_kordoc_table_parser=False,
                    require_source_metadata=False,
                )

            exported_records = [
                json.loads(line)
                for line in (output_dir / "data" / "vector_db" / "tenant-a" / "approved_vectors.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]

        self.assertEqual("selected_documents", runtime_manifest["scope"])
        self.assertEqual(["doc-a", "doc-c"], runtime_manifest["document_ids"])
        self.assertEqual({"doc-a", "doc-c"}, {record["document_id"] for record in exported_records})
        metadata_by_document = {record["document_id"]: record["metadata"] for record in exported_records}
        self.assertEqual("reg-personnel", metadata_by_document["doc-a"]["regulation_id"])
        self.assertEqual("인사규정 > 제1조", metadata_by_document["doc-a"]["hierarchy_path"])
        self.assertEqual("reg-service", metadata_by_document["doc-c"]["regulation_id"])
        self.assertEqual("복무규정 > 제1조", metadata_by_document["doc-c"]["hierarchy_path"])

    def test_runtime_bundle_rejects_selected_document_missing_visible_records(self) -> None:
        records = [_runtime_export_record("doc-a", "chunk-a")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                return_value=records,
            ):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=Path(tmp) / "source",
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        profile_id="public_portal-test-profile",
                        document_ids=["doc-a", "doc-missing"],
                        scope="selected_documents",
                        require_kordoc_table_parser=False,
                        require_source_metadata=False,
                    )

        self.assertIn("not all MCP-visible", str(raised.exception))
        self.assertIn("doc-missing", str(raised.exception))

    def test_runtime_bundle_exports_kordoc_table_parser_summary(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 2, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]
            runtime_progress: list[tuple[int, str, int | None, int | None]] = []

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                    progress_callback=lambda percent, message, current, total: runtime_progress.append(
                        (percent, message, current, total)
                    ),
                )

            saved_manifest = json.loads(
                (output_dir / "data" / "mcp_runtime_manifest.json").read_text(encoding="utf-8")
            )
            approval_snapshot = json.loads(
                (output_dir / "data" / "repository" / "approval_snapshot.json").read_text(encoding="utf-8")
            )

        summary = runtime_manifest["kordoc_table_parser_summary"]
        self.assertTrue(runtime_manifest["kordoc_table_parser_required"])
        self.assertEqual(summary["parsed_document_count"], 1)
        self.assertEqual(summary["documents"][0]["parser"], "kordoc")
        self.assertEqual(summary["documents"][0]["table_count"], 2)
        self.assertTrue(runtime_manifest["source_metadata_required"])
        self.assertTrue(runtime_manifest["source_metadata_summary"]["complete"])
        self.assertEqual("규정", runtime_manifest["recommended_smoke_query"])
        self.assertEqual(saved_manifest["kordoc_table_parser_summary"]["parsed_document_count"], 1)
        self.assertTrue(saved_manifest["source_metadata_summary"]["complete"])
        self.assertEqual("규정", saved_manifest["recommended_smoke_query"])
        self.assertEqual("mcp_runtime_approval_snapshot", approval_snapshot["report_type"])
        self.assertEqual(1, approval_snapshot["snapshot_count"])
        self.assertEqual("doc-kordoc", approval_snapshot["entries"][0]["document_id"])
        self.assertEqual("chunk-1", approval_snapshot["entries"][0]["chunk_id"])
        self.assertTrue(runtime_progress)
        self.assertEqual(100, runtime_progress[-1][0])
        self.assertEqual(sorted(item[0] for item in runtime_progress), [item[0] for item in runtime_progress])

    def test_runtime_bundle_preserves_bulk_approval_review_events(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 2, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 2,
        }
        bulk_events = [
            {
                "event": "human_review_confirmed",
                "timestamp": "2026-07-10T00:00:00+00:00",
                "actor": "operator",
                "chunk_id": "chunk-1",
                "sequence": index,
            }
            for index in range(150)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            JsonRepository(settings).append_approval_record(
                {
                    "approval_record_id": "approval-record-bulk-review-events",
                    "approval_id": "approval-bulk-review-events",
                    "document_id": "doc-kordoc",
                    "tenant_id": "tenant-a",
                    "chunk_ids": ["chunk-1"],
                    "approved_content_hashes": {"chunk-1": "approved-hash"},
                    "approved_chunks": [
                        {
                            "chunk_id": "chunk-1",
                            "approved_content_hash": "approved-hash",
                        }
                    ],
                    "approved_by": "operator",
                    "approved_at": "2026-07-10T00:00:01+00:00",
                    "human_review_confirmed": True,
                    "review_decision_events": bulk_events,
                    "worklist_evidence": _approval_worklist_evidence(),
                }
            )
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            journal_records = [
                json.loads(line)
                for line in (output_dir / "data" / "repository" / "journals" / "approvals.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]

        exported_bulk = next(
            record for record in journal_records if record.get("approval_id") == "approval-bulk-review-events"
        )
        self.assertEqual(2, runtime_manifest["approval_record_count"])
        self.assertEqual(150, len(exported_bulk["review_decision_events"]))

    def test_runtime_bundle_exports_recommended_smoke_query_from_article_metadata(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 2, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 2,
        }
        record_metadata = {
            "chunk_type": "article",
            "article_no": "제7조",
            "article_title": "위임전결",
            "appendix_refs": ["별표1"],
            "regulation_title": "권한위임전결규정",
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1", metadata=record_metadata)]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            saved_manifest = json.loads(
                (output_dir / "data" / "mcp_runtime_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual("제7조 위임전결", runtime_manifest["recommended_smoke_query"])
        self.assertEqual("제7조 위임전결", saved_manifest["recommended_smoke_query"])

    def test_recommended_smoke_query_recovers_from_broken_article_metadata(self) -> None:
        records = [
            _runtime_export_record(
                "doc-kordoc",
                "chunk-broken-metadata",
                metadata={
                    "chunk_type": "article",
                    "article_no": "??",
                    "article_title": "??",
                    "regulation_title": "권한위임전결규정",
                },
            )
        ]
        records[0]["text"] = "제7조(위임전결) 권한의 위임전결 기준은 별표에 따른다."

        self.assertEqual("제7조 위임전결", _recommended_runtime_smoke_query(records))

    def test_recommended_smoke_query_prefers_substantive_article_over_supplementary_transition(self) -> None:
        records = [
            _runtime_export_record(
                "doc-kordoc",
                "chunk-transition",
                metadata={
                    "chunk_type": "article",
                    "article_no": "제2조",
                    "article_title": "장해심사 권역별 통합심사 등 시행에 따른 경과조치",
                    "appendix_refs": ["별표1"],
                },
            ),
            _runtime_export_record(
                "doc-kordoc",
                "chunk-delegation",
                metadata={
                    "chunk_type": "article",
                    "article_no": "제7조",
                    "article_title": "위임전결",
                    "appendix_refs": ["별표1"],
                },
            ),
        ]

        self.assertEqual("제7조 위임전결", _recommended_runtime_smoke_query(records))

    def test_cli_out_dir_writes_clean_runtime_data_bundle(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _seed_runtime_bundle_document(root, file_type="hwp", metadata=metadata)
            output_dir = root / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with (
                patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records),
                patch.object(
                    sys,
                    "argv",
                    [
                        "generate_mcp_client_config.py",
                        "--client-profile",
                        "bundle",
                        "--server-name",
                        "govreg-local",
                        "--data-dir",
                        str(settings.data_dir),
                        "--tenant-id",
                        "tenant-a",
                        "--document-id",
                        "doc-kordoc",
                        "--out-dir",
                        str(output_dir),
                    ],
                ),
                patch("builtins.print"),
            ):
                exit_code = main()

            runtime_data_dir = output_dir / "data"
            runtime_manifest = json.loads(
                runtime_data_dir.joinpath("mcp_runtime_manifest.json").read_text(encoding="utf-8")
            )
            raw_results = sorted(
                path.name
                for path in runtime_data_dir.joinpath("repository").glob("*.json")
                if path.name.endswith(("_nodes.json", "_issues.json", "_quality.json"))
            )

            self.assertEqual(0, exit_code)
            self.assertTrue((output_dir / "mcp_config.bundle.json").is_file())
            self.assertTrue((runtime_data_dir / "mcp_runtime_manifest.json").is_file())
            self.assertTrue((output_dir / "bundle_status.json").is_file())
            self.assertEqual("doc-kordoc", runtime_manifest["document_id"])
            self.assertTrue((runtime_data_dir / "repository" / "doc-kordoc_chunks.json").is_file())
            self.assertEqual([], raw_results)
            bundle_status = json.loads((output_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertTrue(bundle_status["runtime_data_ready"])
            self.assertEqual("doc-kordoc", bundle_status["document_id"])
            self.assertEqual(1, bundle_status["record_count"])
            self.assertEqual(runtime_manifest["recommended_smoke_query"], bundle_status["recommended_smoke_query"])

    def test_cli_can_write_source_only_setup_bundle_without_runtime_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "generate_mcp_client_config.py",
                        "--client-profile",
                        "bundle",
                        "--out-dir",
                        str(output_dir),
                        "--skip-runtime-data",
                    ],
                ),
                patch("builtins.print"),
            ):
                exit_code = main()

            self.assertEqual(0, exit_code)
            self.assertTrue((output_dir / "mcp_config.bundle.json").is_file())
            self.assertFalse((output_dir / "data").exists())

    def test_cli_source_only_bundle_removes_stale_runtime_data_before_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            stale_data_dir = output_dir / "data"
            stale_data_dir.mkdir(parents=True)
            (stale_data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"document_ids": ["stale-document"]}),
                encoding="utf-8",
            )
            zip_out = Path(tmp) / "bundle.zip"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "generate_mcp_client_config.py",
                        "--client-profile",
                        "bundle",
                        "--out-dir",
                        str(output_dir),
                        "--zip-out",
                        str(zip_out),
                        "--skip-runtime-data",
                    ],
                ),
                patch("builtins.print"),
            ):
                exit_code = main()

            with zipfile.ZipFile(zip_out) as archive:
                names = archive.namelist()

            self.assertEqual(0, exit_code)
            self.assertFalse(stale_data_dir.exists())
            self.assertFalse(any(name.startswith("data/") for name in names))

    def test_runtime_bundle_requires_source_metadata_on_approved_records(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]
            records[0]["metadata"].pop("source_url")

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn("requires citation/source metadata", str(raised.exception))
        self.assertIn("source_url", str(raised.exception))

    def test_runtime_bundle_does_not_fall_back_to_flat_source_when_auto_isolated_source_is_empty(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            isolated_dir = settings.data_dir / "tenants" / "tenant-a"
            isolated_dir.mkdir(parents=True)
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]
            seen_data_dirs: list[Path] = []

            def visible_records(*, settings, auth, document_id, profile_id=None):
                seen_data_dirs.append(Path(settings.data_dir))
                return [] if Path(settings.data_dir) == isolated_dir else records

            with patch(
                "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                side_effect=visible_records,
            ):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=output_dir,
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn(isolated_dir, seen_data_dirs)
        self.assertNotIn(settings.data_dir, seen_data_dirs)
        self.assertIn("No MCP-visible approved records", str(raised.exception))

    def test_runtime_bundle_rejects_stale_vector_when_current_chunk_is_draft(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc-kordoc")
            draft_chunk = chunks[0].model_copy(update={"approval_status": "draft"})
            repository.save_processing_result("doc-kordoc", [], [draft_chunk], [])
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn("current repository chunks no longer match approved vector records", str(raised.exception))
        self.assertIn("chunk_not_approved", str(raised.exception))

    def test_runtime_bundle_does_not_export_raw_nodes_issues_or_quality_reports(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc-kordoc")
            repository.save_processing_result(
                "doc-kordoc",
                [
                    StructureNode(
                        node_id="node-draft",
                        document_id="doc-kordoc",
                        node_type="article",
                        text="DRAFT ONLY RAW NODE TEXT",
                        order_index=1,
                    )
                ],
                chunks,
                [],
            )
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            repository_dir = output_dir / "data" / "repository"
            result_files = set(runtime_manifest["files"]["result_files"])

            self.assertTrue((repository_dir / "doc-kordoc_chunks.json").exists())
            self.assertFalse((repository_dir / "doc-kordoc_nodes.json").exists())
            self.assertFalse((repository_dir / "doc-kordoc_issues.json").exists())
            self.assertFalse((repository_dir / "doc-kordoc_quality.json").exists())
            self.assertNotIn(str(repository_dir / "doc-kordoc_nodes.json"), result_files)

    def test_runtime_bundle_export_clears_stale_runtime_files(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            output_dir = Path(tmp) / "bundle"
            stale_repository = output_dir / "data" / "repository"
            stale_vector = output_dir / "data" / "vector_db" / "tenant-a"
            stale_repository.mkdir(parents=True)
            stale_vector.mkdir(parents=True)
            stale_chunk = stale_repository / "doc-stale_chunks.json"
            stale_manifest = output_dir / "data" / "mcp_runtime_manifest.json"
            stale_vector_file = stale_vector / "approved_vectors.jsonl"
            stale_top_level_file = output_dir / "data" / "legacy_export.json"
            stale_chunk.write_text("[]\n", encoding="utf-8")
            stale_manifest.write_text("{}\n", encoding="utf-8")
            stale_vector_file.write_text("{}\n", encoding="utf-8")
            stale_top_level_file.write_text("{}\n", encoding="utf-8")
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            self.assertFalse(stale_chunk.exists())
            self.assertFalse(stale_top_level_file.exists())
            self.assertTrue((output_dir / "data" / "repository" / "doc-kordoc_chunks.json").exists())
            self.assertEqual(runtime_manifest["bm25_index_status"], "ready")
            self.assertEqual(runtime_manifest["bm25_document_count"], 1)
            self.assertTrue((output_dir / "data" / "vector_db" / "tenant-a" / "bm25_index.json").exists())
            self.assertEqual("ready", runtime_manifest["hierarchical_index_status"])
            self.assertEqual(1, runtime_manifest["regulation_count"])
            self.assertEqual(1, runtime_manifest["regulation_version_count"])
            self.assertGreaterEqual(runtime_manifest["toc_node_count"], 1)
            self.assertEqual("reg-rag-logical-corpus-v1", runtime_manifest["rebuild_fingerprint_schema_version"])
            self.assertRegex(runtime_manifest["logical_corpus_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(runtime_manifest["rebuild_contract"]["input_order_independent"])
            hierarchy_path = output_dir / "data" / "hierarchy" / "regulation_hierarchy.sqlite3"
            self.assertTrue(hierarchy_path.exists())
            self.assertEqual(
                runtime_manifest["files"]["hierarchical_index_sha256"],
                hashlib.sha256(hierarchy_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                1,
                len(list((output_dir / "data" / "repository").glob("doc-*_chunks.json"))),
            )

    def test_local_only_setup_bundle_writes_required_contract_files(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            write_mcp_setup_bundle(config, tmp, server_name="govreg-local")
            output_dir = Path(tmp)
            generated_names = {path.name for path in output_dir.iterdir() if path.is_file()}

        self.assertTrue(REQUIRED_SETUP_BUNDLE_FILES.issubset(generated_names))
        self.assertNotIn("claude_code_add_http.ps1", generated_names)

    def test_packaged_app_writes_executable_first_stdio_launcher(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")
        packaged_exe = r"C:\Program Files\PR MCP Builder\PR MCP Builder.exe"

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"REG_RAG_PACKAGED_EXE": packaged_exe}):
                write_mcp_setup_bundle(config, tmp, server_name="govreg-local")
            launcher = Path(tmp, "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")

        self.assertIn(packaged_exe, launcher)
        self.assertIn("& $PackagedExe --mcp-server @ServerArgs", launcher)
        self.assertLess(launcher.index("$PackagedExe"), launcher.index("function Find-ProjectRoot"))

    def test_zip_can_include_built_wheel_for_self_contained_handoff(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "bundle"
            dist_dir = root / "dist"
            zip_path = root / "bundle.zip"
            wheel = dist_dir / "reg_rag_preprocessor-0.1.0-py3-none-any.whl"
            dist_dir.mkdir()
            wheel.write_bytes(b"fake wheel")
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            write_mcp_setup_bundle_zip(output_dir, zip_path, include_wheel=True, dist_dir=dist_dir)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("reg_rag_preprocessor-0.1.0-py3-none-any.whl", names)
            self.assertIn("install_local_package.ps1", names)

    def test_zip_include_wheel_can_run_outside_project_cwd(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "bundle"
            dist_dir = root / "dist"
            other_cwd = root / "other"
            zip_path = root / "bundle.zip"
            wheel = dist_dir / "reg_rag_preprocessor-0.1.0-py3-none-any.whl"
            dist_dir.mkdir()
            other_cwd.mkdir()
            wheel.write_bytes(b"fake wheel")
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            previous_cwd = Path.cwd()
            try:
                os.chdir(other_cwd)
                write_mcp_setup_bundle_zip(output_dir, zip_path, include_wheel=True)
            finally:
                os.chdir(previous_cwd)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("reg_rag_preprocessor-0.1.0-py3-none-any.whl", names)

    def test_zip_out_inside_bundle_does_not_include_itself(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = output_dir / "bundle.zip"
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")
            write_mcp_setup_bundle_zip(output_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertNotIn("bundle.zip", names)
            self.assertIn("connect_mcp_client.ps1", names)

    def test_rejects_unknown_transport(self) -> None:
        with self.assertRaises(ValueError):
            build_mcp_client_config(transport="websocket")

    def test_rejects_unknown_client_profile(self) -> None:
        with self.assertRaises(ValueError):
            build_mcp_client_config(client_profile="unknown-client")


def _seed_runtime_bundle_document(root: Path, *, file_type: str, metadata: dict) -> Settings:
    settings = Settings(data_dir=root / "data")
    repository = JsonRepository(settings)
    document = Document(
        document_id="doc-kordoc",
        filename=f"rules.{file_type}",
        document_name="Rules",
        institution_name="Test Institution",
        source_system="PUBLIC_PORTAL",
        source_url="https://example.test/rules",
        profile_id="public_portal-test-profile",
        file_type=file_type,
        file_hash="hash",
        tenant_id="tenant-a",
        status="completed",
        regulation_id="reg-kordoc",
        regulation_version="v1",
        effective_from="2026-01-01",
        regulation_status="approved",
    )
    chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc-kordoc",
        chunk_type="article",
        text="approved text",
        normalized_text="approved text",
        retrieval_text="approved text",
        metadata={
            "chunk_id": "chunk-1",
            "document_id": "doc-kordoc",
            "tenant_id": "tenant-a",
            "approval_status": "approved",
            "approval_id": "approval-kordoc",
            "approved_content_hash": "approved-hash",
            "security_level": "internal",
            "institution_name": "Test Institution",
            "source_system": "PUBLIC_PORTAL",
            "source_url": "https://example.test/rules",
            "profile_id": "public_portal-test-profile",
            "regulation_id": "reg-kordoc",
            "regulation_version": "v1",
            "effective_from": "2026-01-01",
            "regulation_status": "approved",
            **metadata,
        },
        approval_status="approved",
        approval_id="approval-kordoc",
        approved_content_hash="approved-hash",
        security_level="internal",
    )
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], [chunk], [])
    repository.append_approval_record(
        {
            "approval_record_id": "approval-record-kordoc",
            "approval_id": "approval-kordoc",
            "document_id": "doc-kordoc",
            "tenant_id": "tenant-a",
            "chunk_ids": ["chunk-1"],
            "approved_content_hashes": {"chunk-1": "approved-hash"},
            "approved_chunks": [
                {
                    "chunk_id": "chunk-1",
                    "approved_content_hash": "approved-hash",
                }
            ],
            "approved_by": "operator",
            "approved_at": "2026-07-10T00:00:00+00:00",
            "worklist_evidence": _approval_worklist_evidence(),
        }
    )
    return settings


def _write_runtime_data_manifest(
    runtime_data_dir: Path,
    document_ids: list[str],
    *,
    tenant_id: str = "tenant-a",
) -> None:
    runtime_data_dir.mkdir(parents=True, exist_ok=True)
    runtime_data_dir.joinpath("mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": tenant_id,
                "document_id": document_ids[0] if document_ids else None,
                "document_ids": document_ids,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_runtime_repository_manifest(repository_dir: Path, document_ids: list[str]) -> None:
    repository_dir.mkdir(parents=True, exist_ok=True)
    repository_dir.joinpath("manifest.json").write_text(
        json.dumps({"documents": {document_id: {"document_id": document_id} for document_id in document_ids}}),
        encoding="utf-8",
    )


def _runtime_export_record(document_id: str, chunk_id: str, *, metadata: dict | None = None) -> dict:
    metadata = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "tenant_id": "tenant-a",
        "approval_status": "approved",
        "approval_id": "approval-kordoc",
        "approved_content_hash": "approved-hash",
        "security_level": "internal",
        "approval_worklist_report_path": "reports/worklist.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/batches.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "batch-kordoc",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "operator_manual_review",
        "institution_name": "Test Institution",
        "source_system": "PUBLIC_PORTAL",
        "source_url": "https://example.test/rules",
        "profile_id": "public_portal-test-profile",
        "regulation_id": "reg-kordoc",
        "regulation_version": "v1",
        "effective_from": "2026-01-01",
        "regulation_status": "approved",
        **(metadata or {}),
    }
    text = "approved text"
    return {
        "id": f"{document_id}:{chunk_id}",
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


def _approval_worklist_evidence() -> dict[str, str]:
    return {
        "worklist_report_path": "reports/worklist.json",
        "worklist_report_sha256": "a" * 64,
        "review_batch_manifest_path": "reports/batches.json",
        "review_batch_manifest_sha256": "b" * 64,
        "review_batch_id": "batch-kordoc",
        "review_batch_chunk_fingerprint": "c" * 64,
        "review_strategy": "operator_manual_review",
    }


if __name__ == "__main__":
    unittest.main()
