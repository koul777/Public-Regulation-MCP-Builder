from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.mcp_server.vercel_app import create_vercel_mcp_app
from scripts.prepare_vercel_mcp_deployment import prepare_vercel_mcp_deployment


class _FakeMcpServer:
    def __init__(self) -> None:
        self._reg_rag_scope = {}
        self.app = object()

    def streamable_http_app(self):
        return self.app


class VercelMcpAppTests(unittest.TestCase):
    def test_builds_stateless_read_only_chatgpt_data_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            _write_manifest(runtime)
            fake_server = _FakeMcpServer()
            with patch(
                "app.mcp_server.vercel_app.create_regulation_mcp_server",
                return_value=fake_server,
            ) as create_server:
                app = create_vercel_mcp_app(
                    {
                        "MCP_DATA_DIR": str(runtime),
                        "MCP_AUTH_TOKEN": "secret",
                        "VERCEL_URL": "preview.example.vercel.app",
                    }
                )

        self.assertIs(fake_server.app, app)
        kwargs = create_server.call_args.kwargs
        self.assertEqual("tenant-a", kwargs["tenant_id"])
        self.assertEqual("profile-a", kwargs["profile_id"])
        self.assertEqual("chatgpt-data", kwargs["tool_profile"])
        self.assertTrue(kwargs["stateless_http"])
        self.assertTrue(kwargs["json_response"])
        self.assertFalse(kwargs["api_audit_enabled"])
        self.assertFalse(kwargs["rag_trace_enabled"])
        self.assertFalse(kwargs["background_tokenizer_warmup"])
        self.assertEqual("https://preview.example.vercel.app", kwargs["auth_issuer_url"])
        self.assertIn("preview.example.vercel.app", kwargs["allowed_http_hosts"])
        self.assertTrue(fake_server._reg_rag_scope["read_only_runtime"])

    def test_refuses_unauthenticated_startup_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            _write_manifest(runtime)
            with self.assertRaisesRegex(RuntimeError, "refuses unauthenticated startup"):
                create_vercel_mcp_app(
                    {
                        "MCP_DATA_DIR": str(runtime),
                        "VERCEL_URL": "preview.example.vercel.app",
                    }
                )

    def test_allows_explicit_public_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            _write_manifest(runtime)
            fake_server = _FakeMcpServer()
            with patch(
                "app.mcp_server.vercel_app.create_regulation_mcp_server",
                return_value=fake_server,
            ) as create_server:
                create_vercel_mcp_app(
                    {
                        "MCP_DATA_DIR": str(runtime),
                        "MCP_ALLOW_UNAUTHENTICATED_HTTP": "true",
                        "MCP_ALLOWED_HTTP_HOSTS": "mcp.example.go.kr",
                    }
                )

        self.assertIsNone(create_server.call_args.kwargs["http_bearer_token"])
        self.assertEqual("chatgpt-data", create_server.call_args.kwargs["tool_profile"])

    def test_rejects_full_tool_profile_for_unauthenticated_public_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            _write_manifest(runtime)
            with patch("app.mcp_server.vercel_app.create_regulation_mcp_server") as create_server:
                with self.assertRaisesRegex(RuntimeError, "must use MCP_TOOL_PROFILE=chatgpt-data"):
                    create_vercel_mcp_app(
                        {
                            "MCP_DATA_DIR": str(runtime),
                            "MCP_ALLOW_UNAUTHENTICATED_HTTP": "true",
                            "MCP_ALLOWED_HTTP_HOSTS": "mcp.example.go.kr",
                            "MCP_TOOL_PROFILE": "full",
                        }
                    )

        create_server.assert_not_called()

    def test_allows_full_tool_profile_when_bearer_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            _write_manifest(runtime)
            fake_server = _FakeMcpServer()
            with patch(
                "app.mcp_server.vercel_app.create_regulation_mcp_server",
                return_value=fake_server,
            ) as create_server:
                create_vercel_mcp_app(
                    {
                        "MCP_DATA_DIR": str(runtime),
                        "MCP_AUTH_TOKEN": "secret",
                        "VERCEL_URL": "preview.example.vercel.app",
                        "MCP_TOOL_PROFILE": "full",
                    }
                )

        self.assertEqual("full", create_server.call_args.kwargs["tool_profile"])

    def test_rejects_tenant_override_that_does_not_match_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            _write_manifest(runtime)
            with self.assertRaisesRegex(RuntimeError, "MCP_TENANT_ID does not match"):
                create_vercel_mcp_app(
                    {
                        "MCP_DATA_DIR": str(runtime),
                        "MCP_TENANT_ID": "tenant-b",
                        "MCP_AUTH_TOKEN": "secret",
                        "VERCEL_URL": "preview.example.vercel.app",
                    }
                )


class PrepareVercelMcpDeploymentTests(unittest.TestCase):
    def test_prepares_source_and_approved_runtime_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "approved"
            target = root / "stage"
            _write_manifest(runtime)
            (runtime / "repository").mkdir()
            (runtime / "repository" / "manifest.json").write_text(
                '{"documents":{}}\n',
                encoding="utf-8",
            )

            report = prepare_vercel_mcp_deployment(
                runtime_data_dir=runtime,
                out_dir=target,
            )

            self.assertTrue((target / "api" / "index.py").is_file())
            self.assertTrue((target / "app" / "mcp_server" / "vercel_app.py").is_file())
            self.assertTrue((target / "mcp_runtime" / "mcp_runtime_manifest.json").is_file())
            self.assertTrue((target / ".vercelignore").is_file())
            self.assertFalse((target / "tests").exists())
            self.assertEqual("/mcp", report["mcp_path"])
            self.assertTrue(report["stateless_http"])
            vercel_config = json.loads((target / "vercel.json").read_text(encoding="utf-8"))
            self.assertIn("api/index.py", vercel_config["functions"])
            self.assertEqual(
                [{"source": "/mcp", "destination": "/api/index"}],
                vercel_config["rewrites"],
            )
            vercel_ignore = (target / ".vercelignore").read_text(encoding="utf-8")
            self.assertIn(".env*", vercel_ignore)
            self.assertIn(".vercel", vercel_ignore)
            deployment_report = json.loads(
                (target / "deployment_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(".", deployment_report["out_dir"])
            self.assertEqual("vercel --prod --cwd .", deployment_report["production_deploy_command"])

    def test_rejects_raw_preprocessing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "approved"
            _write_manifest(runtime)
            (runtime / "repository").mkdir()
            (runtime / "repository" / "doc_nodes.json").write_text("[]\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not be deployed"):
                prepare_vercel_mcp_deployment(
                    runtime_data_dir=runtime,
                    out_dir=root / "stage",
                )

    def test_rejects_local_audit_trace_feedback_and_lock_artifacts(self) -> None:
        forbidden_names = (
            ".api_audit.lock",
            ".write.lock",
            "api_audit.jsonl",
            "rag_feedback.jsonl",
            "rag_traces.jsonl",
        )
        for forbidden_name in forbidden_names:
            with self.subTest(forbidden_name=forbidden_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    runtime = root / "approved"
                    _write_manifest(runtime)
                    repository = runtime / "repository"
                    repository.mkdir()
                    (repository / forbidden_name).write_text("local runtime artifact\n", encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "must not be deployed"):
                        prepare_vercel_mcp_deployment(
                            runtime_data_dir=runtime,
                            out_dir=root / "stage",
                        )

    def test_refuses_to_overwrite_existing_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "approved"
            target = root / "stage"
            _write_manifest(runtime)
            target.mkdir()

            with self.assertRaisesRegex(ValueError, "already exists"):
                prepare_vercel_mcp_deployment(
                    runtime_data_dir=runtime,
                    out_dir=target,
                )


def _write_manifest(runtime: Path) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": "tenant-a",
                "profile_id": "profile-a",
                "tenant_storage_isolation": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
