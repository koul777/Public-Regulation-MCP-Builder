from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.check_mcp_connection_readiness import (
    BUNDLE_REQUIRED_FILES,
    _find_smoke_artifacts,
    check_mcp_connection_readiness,
    run,
)


class CheckMcpConnectionReadinessTests(unittest.TestCase):
    def test_script_help_runs_from_file_path(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "check_mcp_connection_readiness.py"), "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual("", result.stderr)
        self.assertEqual(0, result.returncode)
        self.assertIn("--audit-index-visibility", result.stdout)
        self.assertIn("--codex-config", result.stdout)

    def test_chatgpt_requires_http_transport_and_public_https_url(self) -> None:
        with patch("scripts.check_mcp_connection_readiness.current_repo_commit", return_value="a" * 40):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt",
                transport="stdio",
                public_url=None,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("generated_at", report)
        self.assertIsInstance(report["repo_commit"], str)
        self.assertEqual(40, len(report["repo_commit"]))
        self.assertIn("remote-client-stdio", codes)
        self.assertIn("missing-public-url", codes)

    def test_canonical_chatgpt_local_and_remote_profiles_are_distinct(self) -> None:
        local = check_mcp_connection_readiness(
            client_profile="chatgpt-desktop-local",
            transport="stdio",
            check_cli=False,
            check_data=False,
        )
        remote = check_mcp_connection_readiness(
            client_profile="chatgpt-remote",
            transport="stdio",
            check_cli=False,
            check_data=False,
        )

        self.assertTrue(local["passed"])
        self.assertNotIn("remote-client-stdio", {item["code"] for item in local["findings"]})
        self.assertFalse(remote["passed"])
        self.assertIn("remote-client-stdio", {item["code"] for item in remote["findings"]})

    def test_non_loopback_http_requires_token_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            report = check_mcp_connection_readiness(
                client_profile="bundle",
                transport="streamable-http",
                host="0.0.0.0",
                public_url="https://mcp.example.go.kr",
                token_env="MCP_AUTH_TOKEN",
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertTrue(report["passed"])
        self.assertIn("public-url-missing-mcp-suffix", codes)
        self.assertIn("http-auth-token-env-empty", codes)

    def test_ready_remote_configuration_has_no_high_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "vector_db").mkdir()
            with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "token"}, clear=True):
                report = check_mcp_connection_readiness(
                    client_profile="chatgpt",
                    transport="streamable-http",
                    host="0.0.0.0",
                    public_url="https://mcp.example.go.kr/mcp",
                    data_dir=data_dir,
                )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["high_count"])
        self.assertEqual("configuration", report["readiness_scope"])
        self.assertFalse(report["deploy_ready"])
        self.assertFalse(report["remote_probe"]["performed"])

    def test_direct_remote_requires_token_env_even_when_backend_host_is_loopback(self) -> None:
        report = check_mcp_connection_readiness(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            host="127.0.0.1",
            public_url="https://mcp.example.go.kr/mcp",
            token_env=None,
            check_cli=False,
            check_data=False,
        )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("missing-remote-auth-token-env", codes)

    def test_direct_remote_rejects_sse_as_unverified_transport(self) -> None:
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "real-token"}, clear=False), patch(
            "scripts.run_mcp_client_config_smoke.run_mcp_client_config_smoke",
            side_effect=AssertionError("SSE must not be probed as Streamable HTTP"),
        ):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt-remote",
                transport="sse",
                host="127.0.0.1",
                public_url="https://mcp.example.go.kr/mcp",
                check_cli=False,
                check_data=False,
                probe_public_url=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn("remote-sse-not-supported", {finding["code"] for finding in report["findings"]})
        self.assertTrue(report["remote_probe"]["performed"])
        self.assertFalse(report["remote_probe"]["passed"])
        self.assertEqual("unsupported_sse_transport", report["remote_probe"]["detail"])

    def test_direct_remote_rejects_non_public_ip_literals(self) -> None:
        literals = (
            "https://localhost/mcp",
            "https://service.localhost/mcp",
            "https://127.0.0.1/mcp",
            "https://10.0.0.8/mcp",
            "https://169.254.1.2/mcp",
            "https://192.0.2.1/mcp",
            "https://[::1]/mcp",
        )
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "real-token"}, clear=False):
            for public_url in literals:
                with self.subTest(public_url=public_url):
                    report = check_mcp_connection_readiness(
                        client_profile="chatgpt-remote",
                        transport="streamable-http",
                        public_url=public_url,
                        check_cli=False,
                        check_data=False,
                    )
                    self.assertFalse(report["passed"])
                    self.assertIn(
                        "public-url-non-public-literal-host",
                        {finding["code"] for finding in report["findings"]},
                    )

    def test_public_url_probe_failure_blocks_deploy_readiness(self) -> None:
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "token"}, clear=True):
            with patch(
                "scripts.run_mcp_client_config_smoke.run_mcp_client_config_smoke",
                side_effect=OSError("unreachable"),
            ):
                report = check_mcp_connection_readiness(
                    client_profile="chatgpt",
                    transport="streamable-http",
                    host="0.0.0.0",
                    public_url="https://mcp.example.go.kr/mcp",
                    check_data=False,
                    probe_public_url=True,
                )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertFalse(report["deploy_ready"])
        self.assertEqual("deploy", report["readiness_scope"])
        self.assertTrue(report["remote_probe"]["performed"])
        self.assertIn("public-url-probe-failed", codes)

    def test_public_url_probe_requires_mcp_protocol_contract(self) -> None:
        fake_html_response = {
            "passed": False,
            "results": [
                {
                    "mcp_initialized": False,
                    "tools_discovered": False,
                    "contract_verified": False,
                    "error": "HTTP 200 text/html is not an MCP response",
                }
            ],
        }
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "token"}, clear=True), patch(
            "scripts.run_mcp_client_config_smoke.run_mcp_client_config_smoke",
            return_value=fake_html_response,
        ):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt-remote",
                transport="streamable-http",
                host="0.0.0.0",
                public_url="https://mcp.example.go.kr/mcp",
                check_data=False,
                probe_public_url=True,
            )

        self.assertFalse(report["deploy_ready"])
        self.assertFalse(report["remote_probe"]["protocol_verified"])
        self.assertIn("public-url-mcp-protocol-failed", {item["code"] for item in report["findings"]})

    def test_public_url_probe_accepts_only_verified_mcp_contract(self) -> None:
        verified = {
            "passed": True,
            "results": [
                {
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "contract_verified": True,
                    "auth_wire_verified": True,
                    "tool_names": ["fetch", "search"],
                }
            ],
        }
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "token"}, clear=True), patch(
            "scripts.run_mcp_client_config_smoke.run_mcp_client_config_smoke",
            return_value=verified,
        ):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt-remote",
                transport="streamable-http",
                host="0.0.0.0",
                public_url="https://mcp.example.go.kr/mcp",
                check_data=False,
                probe_public_url=True,
            )

        self.assertTrue(report["deploy_ready"])
        self.assertTrue(report["remote_probe"]["protocol_verified"])
        self.assertEqual(["fetch", "search"], report["remote_probe"]["tool_names"])

    def test_public_url_probe_requires_fail_closed_auth_wire(self) -> None:
        protocol_only = {
            "passed": True,
            "results": [
                {
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "contract_verified": True,
                    "auth_wire_verified": False,
                    "tool_names": ["fetch", "search"],
                }
            ],
        }
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "token"}, clear=True), patch(
            "scripts.run_mcp_client_config_smoke.run_mcp_client_config_smoke",
            return_value=protocol_only,
        ):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt-remote",
                transport="streamable-http",
                host="0.0.0.0",
                public_url="https://mcp.example.go.kr/mcp",
                check_data=False,
                probe_public_url=True,
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["deploy_ready"])
        self.assertFalse(report["remote_probe"]["protocol_verified"])

    def test_public_url_probe_exception_redacts_bearer_token(self) -> None:
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "sentinel-probe-secret"}, clear=True), patch(
            "scripts.run_mcp_client_config_smoke.run_mcp_client_config_smoke",
            side_effect=RuntimeError("Authorization: Bearer sentinel-probe-secret"),
        ):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt-remote",
                transport="streamable-http",
                host="0.0.0.0",
                public_url="https://mcp.example.go.kr/mcp",
                check_data=False,
                probe_public_url=True,
            )

        serialized = json.dumps(report)
        self.assertNotIn("sentinel-probe-secret", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_malformed_public_url_blocks_remote_configuration(self) -> None:
        for public_url in (
            "https://",
            "https://?tenant=default",
            "https://mcp.example.go.kr/mcp?tenant=default",
            "https://user:secret@mcp.example.go.kr/mcp",
            "https://mcp.example.go.kr/mcp#fragment",
        ):
            with self.subTest(public_url=public_url), patch.dict(
                os.environ,
                {"MCP_AUTH_TOKEN": "token"},
                clear=True,
            ):
                report = check_mcp_connection_readiness(
                    client_profile="chatgpt-remote",
                    transport="streamable-http",
                    host="0.0.0.0",
                    public_url=public_url,
                    check_data=False,
                    check_cli=False,
                )

            codes = {finding["code"] for finding in report["findings"]}
            self.assertFalse(report["passed"])
            self.assertIn("public-url-invalid", codes)
            self.assertIsNone(report["remote_probe"]["url"])

    def test_openai_tunnel_blocks_without_runtime_prerequisites(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt",
                connection_mode="openai-tunnel",
                transport="stdio",
                check_cli=True,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertEqual("openai-tunnel", report["connection_mode"])
        self.assertNotIn("remote-client-stdio", codes)
        self.assertIn("openai-tunnel-id-env-empty", codes)
        self.assertIn("openai-control-plane-api-key-env-empty", codes)

    def test_openai_tunnel_passes_with_cli_and_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_TUNNEL_ID": "tunnel-123",
                "CONTROL_PLANE_API_KEY": "runtime-key-123",
            },
            clear=True,
        ), patch("scripts.check_mcp_connection_readiness.shutil.which", return_value="tunnel-client"):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt",
                connection_mode="openai-tunnel",
                transport="stdio",
                check_cli=True,
                check_data=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["high_count"])

    def test_openai_tunnel_is_not_valid_for_claude_api(self) -> None:
        report = check_mcp_connection_readiness(
            client_profile="claude-api",
            connection_mode="openai-tunnel",
            transport="stdio",
            check_cli=False,
            check_data=False,
        )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("openai-tunnel-not-claude-api", codes)

    def test_openai_tunnel_rejects_placeholder_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENAI_TUNNEL_ID": "<tunnel_id>", "CONTROL_PLANE_API_KEY": "<runtime-api-key>"},
            clear=True,
        ):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt",
                connection_mode="openai-tunnel",
                transport="stdio",
                check_cli=False,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("openai-tunnel-id-env-placeholder", codes)
        self.assertIn("openai-control-plane-api-key-env-placeholder", codes)

    def test_http_auth_rejects_placeholder_token(self) -> None:
        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "<strong-token>"}, clear=True):
            report = check_mcp_connection_readiness(
                client_profile="chatgpt",
                transport="streamable-http",
                host="0.0.0.0",
                public_url="https://mcp.example.go.kr/mcp",
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("http-auth-token-env-placeholder", codes)

    def test_bundle_dir_checks_required_files_and_secretless_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                check_data=False,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["high_count"])

    def test_bundle_connection_summary_splits_local_and_remote_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            (bundle_dir / "bundle_status.json").write_text(
                json.dumps(
                    {
                        "connections": [
                            {"client": "Claude Desktop", "mode": "local_stdio", "ready": True},
                            {"client": "Claude Code", "mode": "local_stdio", "ready": True},
                            {"client": "Codex", "mode": "local_stdio", "ready": True},
                            {
                                "client": "ChatGPT",
                                "mode": "https_connector",
                                "ready": False,
                                "operator_action": "Register connector_url.",
                            },
                            {
                                "client": "ChatGPT",
                                "mode": "secure_mcp_tunnel",
                                "ready": "manual_setup_required",
                                "operator_action": "Set tunnel environment variables.",
                            },
                            {
                                "client": "Claude API",
                                "mode": "https_mcp_connector",
                                "ready": False,
                                "operator_action": "Use HTTPS MCP connector.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        summary = report["bundle_connection_summary"]
        self.assertTrue(summary["local_stdio_ready"])
        self.assertEqual(3, summary["local_stdio_ready_count"])
        self.assertFalse(summary["remote_connector_ready"])
        self.assertEqual(0, summary["remote_ready_count"])
        self.assertEqual(3, summary["remote_connector_count"])
        self.assertEqual(2, summary["remote_not_ready_count"])
        self.assertEqual(1, summary["remote_manual_setup_required_count"])

    def test_bundle_dir_rejects_secret_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            (bundle_dir / "run_http_server.ps1").write_text(
                '$env:MCP_AUTH_TOKEN = "<strong-token>"\n',
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("bundle-token-assignment", codes)
        self.assertIn("bundle-placeholder-secret", codes)

    def test_cli_accepts_bundle_dir_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            stdout = io.StringIO()

            exit_code = run(
                ["--client-profile", "bundle", "--bundle-dir", str(bundle_dir), "--skip-data-check", "--json"],
                stdout=stdout,
            )

        self.assertEqual(0, exit_code)
        self.assertIn('"bundle_dir"', stdout.getvalue())

    def test_bundle_dir_defaults_data_check_to_bundle_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            _write_minimal_bundle(bundle_dir)
            bundle_data_dir = bundle_dir / "data"
            bundle_data_dir.mkdir()
            checked_data_dirs: list[Path] = []

            def fake_check_data_dir(
                data_dir: Path,
                findings: list[object],
                *,
                require_full_index: bool = False,
            ) -> None:
                checked_data_dirs.append(data_dir)

            with patch("scripts.check_mcp_connection_readiness._check_data_dir", side_effect=fake_check_data_dir):
                report = check_mcp_connection_readiness(
                    client_profile="bundle",
                    bundle_dir=bundle_dir,
                    allow_local_only_bundle=True,
                )

        self.assertTrue(report["passed"])
        self.assertEqual([bundle_data_dir], checked_data_dirs)

    def test_installed_codex_config_rejects_stale_bundle_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            _write_minimal_bundle(bundle_dir, chatgpt_ready=False, claude_api_ready=False)
            stale_data_dir = (Path(tmp) / "old_bundle" / "data").as_posix()
            codex_config = Path(tmp) / "config.toml"
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "reg-rag-mcp-server"',
                        f'args = ["--data-dir", "{stale_data_dir}", "--transport", "stdio"]',
                    ]
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                codex_config=codex_config,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("installed-client-data-dir-mismatch", codes)
        self.assertIn("installed-client-missing-no-warm-cache", codes)
        self.assertIn("installed-client-storage-flag-missing", codes)
        summary = report["installed_client_config_summary"]
        self.assertEqual("govreg-local", summary["server_name"])
        self.assertIn("codex", summary["clients"])

    def test_installed_codex_config_passes_for_matching_fast_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            _write_minimal_bundle(bundle_dir)
            bundle_data_dir = bundle_dir / "data"
            bundle_data_dir.mkdir()
            launcher_path = bundle_dir / "run_mcp_stdio_server.ps1"
            launcher_path.write_text("reg-rag-mcp-server @args\n", encoding="utf-8")
            (bundle_data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_storage_isolation": False, "document_ids": []}),
                encoding="utf-8",
            )
            codex_config = Path(tmp) / "config.toml"
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        f'cwd = "{bundle_dir.as_posix()}"',
                        (
                            'args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "'
                            + launcher_path.as_posix()
                            + '", "--data-dir", "'
                            + bundle_data_dir.as_posix()
                            + '", "--tenant-id", "default", "--transport", "stdio", '
                            '"--flat-storage", "--tool-profile", "full", "--no-warm-cache"]'
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                codex_config=codex_config,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertTrue(report["passed"])
        self.assertNotIn("installed-client-data-dir-mismatch", codes)
        self.assertNotIn("installed-client-missing-no-warm-cache", codes)
        summary = report["installed_client_config_summary"]
        self.assertEqual("--flat-storage", summary["expected_storage_flag"])
        self.assertEqual("checked", summary["clients"]["codex"]["status"])
        self.assertEqual(launcher_path.as_posix(), summary["clients"]["codex"]["stdio_launcher_path"])

    def test_installed_codex_config_rejects_disabled_and_duplicate_contract_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            _write_minimal_bundle(bundle_dir)
            bundle_data_dir = bundle_dir / "data"
            bundle_data_dir.mkdir()
            launcher_path = bundle_dir / "run_mcp_stdio_server.ps1"
            launcher_path.write_text("reg-rag-mcp-server @args\n", encoding="utf-8")
            (bundle_data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_id": "default", "tenant_storage_isolation": False, "document_ids": []}),
                encoding="utf-8",
            )
            codex_config = Path(tmp) / "config.toml"
            stale_data_dir = (Path(tmp) / "stale" / "data").as_posix()
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        "enabled = false",
                        f'cwd = "{bundle_dir.as_posix()}"',
                        (
                            'args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "'
                            + launcher_path.as_posix()
                            + '", "--data-dir", "'
                            + bundle_data_dir.as_posix()
                            + '", "--data-dir", "'
                            + stale_data_dir
                            + '", "--tenant-id", "default", "--transport", "stdio", '
                            '"--flat-storage", "--tool-profile", "minimal", "--no-warm-cache"]'
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                codex_config=codex_config,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("installed-client-local-contract-mismatch", codes)
        summary = report["installed_client_config_summary"]["clients"]["codex"]
        self.assertEqual("invalid_contract", summary["status"])
        self.assertIn("entry is disabled", summary["contract_issues"])
        self.assertIn("args differ from the generated bundle contract", summary["contract_issues"])

    def test_installed_client_config_rejects_stale_or_missing_stdio_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            _write_minimal_bundle(bundle_dir)
            bundle_data_dir = bundle_dir / "data"
            bundle_data_dir.mkdir()
            current_launcher = bundle_dir / "run_mcp_stdio_server.ps1"
            current_launcher.write_text("reg-rag-mcp-server @args\n", encoding="utf-8")
            stale_launcher = Path(tmp) / "old_bundle" / "run_mcp_stdio_server.ps1"
            stale_launcher.parent.mkdir()
            stale_launcher.write_text("reg-rag-mcp-server @args\n", encoding="utf-8")
            (bundle_data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_storage_isolation": False, "document_ids": []}),
                encoding="utf-8",
            )
            codex_config = Path(tmp) / "config.toml"
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        (
                            'args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "'
                            + stale_launcher.as_posix()
                            + '", "--data-dir", "'
                            + bundle_data_dir.as_posix()
                            + '", "--transport", "stdio", "--flat-storage", "--no-warm-cache"]'
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                codex_config=codex_config,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("installed-client-stdio-launcher-mismatch", codes)

    def test_installed_client_config_rejects_missing_stdio_launcher_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            _write_minimal_bundle(bundle_dir)
            bundle_data_dir = bundle_dir / "data"
            bundle_data_dir.mkdir()
            missing_launcher = bundle_dir / "run_mcp_stdio_server.ps1"
            missing_launcher.unlink()
            (bundle_data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_storage_isolation": False, "document_ids": []}),
                encoding="utf-8",
            )
            codex_config = Path(tmp) / "config.toml"
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        (
                            'args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "'
                            + missing_launcher.as_posix()
                            + '", "--data-dir", "'
                            + bundle_data_dir.as_posix()
                            + '", "--transport", "stdio", "--flat-storage", "--no-warm-cache"]'
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                server_name="govreg-local",
                bundle_dir=bundle_dir,
                codex_config=codex_config,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("installed-client-stdio-launcher-missing", codes)

    def test_bundle_dir_rejects_not_ready_remote_profiles_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir, chatgpt_ready=False, claude_api_ready=False)

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
            )

        codes = [finding["code"] for finding in report["findings"]]
        self.assertFalse(report["passed"])
        self.assertEqual(2, codes.count("bundle-remote-profile-not-ready"))

    def test_bundle_dir_allows_not_ready_remote_profiles_for_local_only_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir, chatgpt_ready=False, claude_api_ready=False)

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = [finding["code"] for finding in report["findings"]]
        self.assertTrue(report["passed"])
        self.assertNotIn("bundle-remote-profile-not-ready", codes)
        self.assertTrue(report["allow_local_only_bundle"])

    def test_bundle_dir_rejects_invalid_claude_desktop_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            (bundle_dir / "claude_desktop_config.json").write_text('{"mcpServers": { "bad": }', encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("bundle-json-invalid", codes)

    def test_bundle_dir_rejects_claude_desktop_without_mcp_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            (bundle_dir / "claude_desktop_config.json").write_text("{}", encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("bundle-claude-desktop-config-invalid", codes)

    def test_bundle_dir_rejects_snake_case_chatgpt_plugin_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            plugin_mcp = (
                bundle_dir
                / "chatgpt-desktop-local-plugin"
                / "plugins"
                / "govreg-local"
                / ".mcp.json"
            )
            plugin_mcp.write_text(
                json.dumps({"mcp_servers": {"govreg-local": {"command": "python", "args": []}}}),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_cli=False,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("bundle-chatgpt-plugin-container-unsupported", codes)

    def test_bundle_dir_rejects_chatgpt_plugin_source_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            marketplace_path = (
                bundle_dir / "chatgpt-desktop-local-plugin" / ".agents" / "plugins" / "marketplace.json"
            )
            marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
            marketplace["plugins"][0]["source"]["path"] = "../outside"
            marketplace_path.write_text(json.dumps(marketplace), encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_cli=False,
                check_data=False,
                allow_local_only_bundle=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "bundle-chatgpt-plugin-marketplace-contract-invalid",
            {finding["code"] for finding in report["findings"]},
        )

    def test_bundle_dir_rejects_unpaired_chatgpt_plugin_launcher_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            mcp_path = (
                bundle_dir
                / "chatgpt-desktop-local-plugin"
                / "plugins"
                / "govreg-local"
                / ".mcp.json"
            )
            payload = json.loads(mcp_path.read_text(encoding="utf-8"))
            args = payload["mcpServers"]["govreg-local"]["args"]
            args[args.index("-File") + 1] = "wrong-launcher.ps1"
            args.append("run_mcp_stdio_server.ps1")
            mcp_path.write_text(json.dumps(payload), encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_cli=False,
                check_data=False,
                allow_local_only_bundle=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "bundle-chatgpt-plugin-stdio-args-invalid",
            {finding["code"] for finding in report["findings"]},
        )

    def test_bundle_dir_rejects_duplicate_plugin_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            mcp_path = (
                bundle_dir
                / "chatgpt-desktop-local-plugin"
                / "plugins"
                / "govreg-local"
                / ".mcp.json"
            )
            original = mcp_path.read_text(encoding="utf-8").strip()
            duplicate = '{"mcpServers": {}, "mcpServers": ' + original[len('{"mcpServers": '):]
            mcp_path.write_text(duplicate, encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_cli=False,
                check_data=False,
                allow_local_only_bundle=True,
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "bundle-chatgpt-plugin-mcp-invalid",
            {finding["code"] for finding in report["findings"]},
        )

    def test_bundle_dir_rejects_stale_runtime_document_artifacts_even_when_data_check_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            runtime_dir = bundle_dir / "data"
            repository_dir = runtime_dir / "repository"
            vector_dir = runtime_dir / "vector_db" / "default"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            (runtime_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"document_ids": ["doc_current"]}),
                encoding="utf-8",
            )
            (repository_dir / "manifest.json").write_text(
                json.dumps({"documents": {"doc_current": {"document_id": "doc_current"}}}),
                encoding="utf-8",
            )
            (repository_dir / "doc_current_chunks.json").write_text("[]\n", encoding="utf-8")
            (repository_dir / "doc_stale_chunks.json").write_text("[]\n", encoding="utf-8")
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc_current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-stale-document-artifacts", codes)

    def test_bundle_dir_rejects_stale_approval_snapshot_even_when_data_check_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            runtime_dir = bundle_dir / "data"
            repository_dir = runtime_dir / "repository"
            repository_dir.mkdir(parents=True)
            (runtime_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"document_ids": ["doc_current"]}),
                encoding="utf-8",
            )
            (repository_dir / "manifest.json").write_text(
                json.dumps({"documents": {"doc_current": {"document_id": "doc_current"}}}),
                encoding="utf-8",
            )
            (repository_dir / "doc_current_chunks.json").write_text("[]\n", encoding="utf-8")
            (repository_dir / "approval_snapshot.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_approval_snapshot",
                        "document_ids": ["doc_current", "doc_stale"],
                        "entries": [{"document_id": "doc_stale", "chunk_id": "chunk-stale"}],
                    }
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-stale-document-artifacts", codes)

    def test_bundle_dir_rejects_runtime_data_without_manifest_even_when_data_check_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            runtime_dir = bundle_dir / "data"
            repository_dir = runtime_dir / "repository"
            vector_dir = runtime_dir / "vector_db" / "default"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            (repository_dir / "manifest.json").write_text(
                json.dumps({"documents": {"doc_current": {"document_id": "doc_current"}}}),
                encoding="utf-8",
            )
            (repository_dir / "doc_current_chunks.json").write_text("[]\n", encoding="utf-8")
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc_current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-manifest-missing", codes)

    def test_bundle_dir_requires_manifest_for_approval_snapshot_only_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            repository_dir = bundle_dir / "data" / "repository"
            repository_dir.mkdir(parents=True)
            (repository_dir / "approval_snapshot.json").write_text(
                json.dumps({"report_type": "mcp_runtime_approval_snapshot", "document_ids": ["doc_current"]}),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-manifest-missing", codes)

    def test_bundle_dir_rejects_cross_tenant_runtime_vector_artifacts_even_when_data_check_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            runtime_dir = bundle_dir / "data"
            repository_dir = runtime_dir / "repository"
            tenant_a_vector_dir = runtime_dir / "vector_db" / "tenant-a"
            tenant_b_vector_dir = runtime_dir / "vector_db" / "tenant-b"
            repository_dir.mkdir(parents=True)
            tenant_a_vector_dir.mkdir(parents=True)
            tenant_b_vector_dir.mkdir(parents=True)
            (runtime_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_id": "tenant-a", "document_ids": ["doc_current"]}),
                encoding="utf-8",
            )
            (repository_dir / "manifest.json").write_text(
                json.dumps({"documents": {"doc_current": {"document_id": "doc_current"}}}),
                encoding="utf-8",
            )
            (repository_dir / "doc_current_chunks.json").write_text("[]\n", encoding="utf-8")
            vector_record = json.dumps({"document_id": "doc_current"}, ensure_ascii=False) + "\n"
            (tenant_a_vector_dir / "approved_vectors.jsonl").write_text(vector_record, encoding="utf-8")
            (tenant_b_vector_dir / "approved_vectors.jsonl").write_text(vector_record, encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-cross-tenant-vector-artifacts", codes)

    def test_bundle_dir_rejects_raw_runtime_repository_results_even_when_data_check_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            _write_minimal_bundle(bundle_dir)
            runtime_dir = bundle_dir / "data"
            repository_dir = runtime_dir / "repository"
            vector_dir = runtime_dir / "vector_db" / "default"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            (runtime_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"document_ids": ["doc_current"]}),
                encoding="utf-8",
            )
            (repository_dir / "manifest.json").write_text(
                json.dumps({"documents": {"doc_current": {"document_id": "doc_current"}}}),
                encoding="utf-8",
            )
            (repository_dir / "doc_current_chunks.json").write_text("[]\n", encoding="utf-8")
            (repository_dir / "doc_current_nodes.json").write_text("[]\n", encoding="utf-8")
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc_current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                check_data=False,
                allow_local_only_bundle=True,
            )
            default_data_report = check_mcp_connection_readiness(
                client_profile="bundle",
                bundle_dir=bundle_dir,
                allow_local_only_bundle=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-raw-preprocessing-artifacts", codes)
        default_codes = [finding["code"] for finding in default_data_report["findings"]]
        self.assertEqual(1, default_codes.count("mcp-runtime-raw-preprocessing-artifacts"))

    def test_cli_returns_nonzero_for_high_findings(self) -> None:
        stdout = io.StringIO()

        exit_code = run(
            ["--client-profile", "claude-api", "--transport", "stdio", "--json", "--skip-data-check"],
            stdout=stdout,
        )

        self.assertEqual(1, exit_code)
        self.assertIn('"remote-client-stdio"', stdout.getvalue())

    def test_data_check_rejects_smoke_documents_in_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_dir = data_dir / "tenants" / "tenant-a" / "repository"
            repository_dir.mkdir(parents=True)
            (repository_dir / "doc_mcp_smoke_v1_chunks.json").write_text("[]", encoding="utf-8")

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=data_dir,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-smoke-docs-present", codes)

    def test_smoke_artifact_scan_streams_large_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_dir = data_dir / "vector_db" / "tenant-a"
            vector_dir.mkdir(parents=True)
            vector_path = vector_dir / "approved_vectors.jsonl"
            vector_path.write_bytes(b"x" * (1024 * 1024 + 7) + b"doc_mcp_smoke\n")

            with patch.object(Path, "read_text", side_effect=AssertionError("read_text should not be used")):
                matches = _find_smoke_artifacts(data_dir)

        self.assertEqual([vector_path], matches)

    def test_data_dir_rejects_runtime_data_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "tenants" / "tenant-a"
            repository_dir = runtime_dir / "repository"
            vector_dir = runtime_dir / "vector_db" / "tenant-a"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            (repository_dir / "doc_current_chunks.json").write_text("[]\n", encoding="utf-8")
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc_current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=data_dir,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-manifest-missing", codes)

    def test_require_full_index_rejects_partial_vector_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            tenant_dir = data_dir / "tenants" / "tenant-a"
            repository_dir = tenant_dir / "repository"
            vector_dir = tenant_dir / "vector_db" / "tenant-a"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            (tenant_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_id": "tenant-a", "document_ids": ["doc"]}),
                encoding="utf-8",
            )
            chunks = [{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-2"}]
            (repository_dir / "doc_chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"id": "doc:chunk-1", "document_id": "doc"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=data_dir,
                require_full_index=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-runtime-not-fully-indexed", codes)

    def test_require_full_index_passes_full_vector_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            tenant_dir = data_dir / "tenants" / "tenant-a"
            repository_dir = tenant_dir / "repository"
            vector_dir = tenant_dir / "vector_db" / "tenant-a"
            repository_dir.mkdir(parents=True)
            vector_dir.mkdir(parents=True)
            (tenant_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_id": "tenant-a", "document_ids": ["doc"]}),
                encoding="utf-8",
            )
            chunks = [{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-2"}]
            (repository_dir / "doc_chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            (vector_dir / "approved_vectors.jsonl").write_text(
                "".join(
                    [
                        json.dumps({"id": "doc:chunk-1", "document_id": "doc"}, ensure_ascii=False) + "\n",
                        json.dumps({"id": "doc:chunk-2", "document_id": "doc"}, ensure_ascii=False) + "\n",
                    ]
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=data_dir,
                require_full_index=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertTrue(report["passed"])
        self.assertNotIn("mcp-runtime-not-fully-indexed", codes)

    def test_index_visibility_audit_summary_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=True)
            _prepare_indexed_document(settings, document_id="doc-real")

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=settings.data_dir,
                audit_index_visibility=True,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                forbid_smoke_docs=True,
                require_indexed=True,
            )

        self.assertTrue(report["passed"])
        summary = report["mcp_index_visibility_summary"]
        self.assertIsInstance(summary, dict)
        self.assertEqual(1, summary["document_count"])
        self.assertEqual(1, summary["total_approved_chunks"])
        self.assertEqual(1, summary["total_indexable_record_count"])
        self.assertEqual(1, summary["total_mcp_visible_records"])
        self.assertEqual({"indexed": 1}, summary["status_counts"])
        self.assertEqual(0, summary["parser_evidence_summary"]["hwpx_evidence_document_count"])
        self.assertEqual(1, summary["approval_provenance_coverage"]["complete_record_count"])
        self.assertEqual(1, summary["approval_journal_coverage"]["matched_record_count"])
        self.assertEqual(0, summary["approval_journal_coverage"]["missing_record_count"])

    def test_index_visibility_audit_passes_with_root_runtime_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=True)
            _prepare_indexed_document(settings, document_id="doc-real")
            (settings.data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_data_bundle",
                        "tenant_id": "tenant-a",
                        "tenant_storage_isolation": True,
                        "document_ids": ["doc-real"],
                    }
                ),
                encoding="utf-8",
            )

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=settings.data_dir,
                audit_index_visibility=True,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=1,
                forbid_smoke_docs=True,
                require_indexed=True,
        )

        self.assertTrue(report["passed"])
        self.assertNotIn("mcp-runtime-manifest-missing", {finding["code"] for finding in report["findings"]})

    def test_index_integrity_flags_auto_enable_visibility_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=True)
            _prepare_indexed_document(settings, document_id="doc-real")

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                require_indexed=True,
            )

        self.assertTrue(report["passed"])
        self.assertIsInstance(report["mcp_index_visibility_summary"], dict)
        self.assertEqual(1, report["mcp_index_visibility_summary"]["total_mcp_visible_records"])

    def test_index_integrity_flags_without_tenant_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=Path(tmp) / "data",
                require_indexed=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("index-visibility-tenant-required", codes)

    def test_explicit_visibility_audit_without_tenant_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=Path(tmp) / "data",
                audit_index_visibility=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("index-visibility-tenant-required", codes)
        self.assertIsNone(report["mcp_index_visibility_summary"])

    def test_min_visible_records_auto_enables_visibility_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=True)
            _prepare_indexed_document(settings, document_id="doc-real")

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=settings.data_dir,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                min_visible_records=2,
            )

        self.assertFalse(report["passed"])
        self.assertIsInstance(report["mcp_index_visibility_summary"], dict)
        self.assertIn("mcp-index-too-few-visible-records", {item["code"] for item in report["findings"]})

    def test_index_visibility_audit_blocks_wrong_tenant_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=True)
            _prepare_indexed_document(settings, document_id="doc-real")

            report = check_mcp_connection_readiness(
                client_profile="claude-desktop",
                data_dir=settings.data_dir,
                audit_index_visibility=True,
                tenant_id="tenant-b",
                tenant_storage_isolation=True,
                min_visible_records=1,
                require_indexed=True,
            )

        codes = {finding["code"] for finding in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("mcp-index-no-documents", codes)
        self.assertIn("mcp-index-too-few-visible-records", codes)


def _write_minimal_bundle(bundle_dir: Path, *, chatgpt_ready: bool = True, claude_api_ready: bool = True) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for filename in BUNDLE_REQUIRED_FILES:
        content = "safe"
        if filename.endswith(".json"):
            content = "{}"
        if filename == "manifest.json":
            content = json.dumps(
                {
                    "server_name": "govreg-local",
                    "ready": {"chatgpt": chatgpt_ready, "claude_api": claude_api_ready},
                }
            )
        if filename == "claude_desktop_config.json":
            content = json.dumps(
                {
                    "mcpServers": {
                        "govreg-local": {
                            "type": "stdio",
                            "command": "reg-rag-mcp-server",
                            "args": ["--transport", "stdio"],
                        }
                    }
                }
            )
        if filename == "connect_mcp_client.ps1":
            content = 'powershell -File "install_local_package.ps1"'
        (bundle_dir / filename).write_text(content, encoding="utf-8")
    plugin_root = bundle_dir / "chatgpt-desktop-local-plugin"
    plugin_dir = plugin_root / "plugins" / "govreg-local"
    manifest_dir = plugin_dir / ".codex-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (plugin_root / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
    (plugin_root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "govreg-local-local",
                "plugins": [
                    {
                        "name": "govreg-local",
                        "source": {"source": "local", "path": "./plugins/govreg-local"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Productivity",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "govreg-local",
                "version": "0.1.0+codex.123456789abc",
                "mcpServers": "./.mcp.json",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "govreg-local": {
                        "type": "stdio",
                        "command": "powershell.exe",
                        "args": [
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            "run_mcp_stdio_server.ps1",
                            "--data-dir",
                            "data",
                            "--tenant-id",
                            "default",
                            "--transport",
                            "stdio",
                            "--flat-storage",
                            "--tool-profile",
                            "full",
                            "--no-warm-cache",
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _prepare_indexed_document(settings: Settings, *, document_id: str) -> None:
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    tenant_settings = settings_for_tenant(approval_settings, "tenant-a")
    repository = JsonRepository(tenant_settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            document_name=f"{document_id}.pdf",
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            tenant_id="tenant-a",
            status="completed",
        )
    )
    repository.save_processing_result(
        document_id,
        [],
        [
            Chunk(
                chunk_id=f"{document_id}:chunk-1",
                document_id=document_id,
                chunk_type="article",
                text="approved regulation text",
                retrieval_text="approved regulation text",
                security_level="internal",
            )
        ],
        [],
    )
    chunks = repository.get_chunks(document_id)
    evidence = _write_approval_evidence(
        approval_settings,
        tenant_settings=tenant_settings,
        document_id=document_id,
        chunks=chunks,
    )
    auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[f"{document_id}:chunk-1"],
                approval_id=f"approval-{document_id}",
                review_flags_acknowledged=True,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )
    (tenant_settings.data_dir / "mcp_runtime_manifest.json").write_text(
        json.dumps({"tenant_id": "tenant-a", "document_ids": [document_id]}),
        encoding="utf-8",
    )
    repository_dir = tenant_settings.data_dir / "repository"
    for suffix in ("_nodes.json", "_issues.json", "_quality.json"):
        artifact_path = repository_dir / f"{document_id}{suffix}"
        if artifact_path.exists():
            artifact_path.unlink()


def _write_approval_evidence(
    settings: Settings,
    *,
    tenant_settings: Settings,
    document_id: str,
    chunks: list[Chunk],
) -> dict[str, str]:
    reports = settings.artifact_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_relative = f"reports/{document_id}_approval_worklist.json"
    batch_relative = f"reports/{document_id}_approval_review_batches.json"
    worklist_path = settings.artifact_root / worklist_relative
    batch_path = settings.artifact_root / batch_relative
    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(tenant_settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": True,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [{"document_id": document_id, "total_chunks": len(chunks)}],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)
    review_type = "low_risk_batch"
    batch_chunks = [
        {
            "chunk_id": chunk.chunk_id,
            "review_content_hash": routes_documents._review_content_hash(chunk),
            "approval_status": chunk.approval_status,
            "review_priority_tier": "no_signal",
            "review_category": "low_risk_batch_review_candidate",
            "attention_reasons": [],
        }
        for chunk in chunks
    ]
    fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": "2026-07-10T00:00:01+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(tenant_settings.data_dir),
        "tenant_id": "tenant-a",
        "tenant_storage_isolation": True,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": worklist_relative,
            "sha256": worklist_sha256,
            "effective_data_dir": str(tenant_settings.data_dir),
            "tenant_id": "tenant-a",
            "tenant_storage_isolation": True,
            "document_count": 1,
            "total_chunks": len(chunks),
        },
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": fingerprint,
                "review_type": review_type,
                "review_strategy": "human_bulk_review",
                "document_id": document_id,
                "chunk_count": len(chunks),
                "chunk_ids": [chunk.chunk_id for chunk in chunks],
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
            }
        ],
    }
    batch_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": worklist_relative,
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": batch_relative,
        "review_batch_manifest_sha256": _sha256_file(batch_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
