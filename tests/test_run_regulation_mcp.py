from __future__ import annotations

import argparse
import io
import os
import unittest
from unittest.mock import patch

from app.mcp_server.regulation_server import create_regulation_mcp_server
from scripts.run_regulation_mcp import _resolve_http_bearer_token, _validate_http_auth_posture, main, parse_args


class RunRegulationMcpTests(unittest.TestCase):
    def test_rejects_unauthenticated_non_loopback_http(self) -> None:
        args = argparse.Namespace(
            transport="streamable-http",
            host="0.0.0.0",
            allow_unauthenticated_http=False,
        )

        with self.assertRaises(SystemExit):
            _validate_http_auth_posture(args, None)

    def test_allows_non_loopback_http_with_bearer_token(self) -> None:
        args = argparse.Namespace(
            transport="streamable-http",
            host="0.0.0.0",
            allow_unauthenticated_http=False,
        )

        _validate_http_auth_posture(args, "secret-token")

    def test_resolves_bearer_token_from_env(self) -> None:
        args = argparse.Namespace(http_bearer_token=None, http_bearer_token_env="MCP_AUTH_TOKEN")

        with patch.dict(os.environ, {"MCP_AUTH_TOKEN": "secret-token"}):
            self.assertEqual("secret-token", _resolve_http_bearer_token(args))

    def test_create_server_enables_bearer_auth_when_token_is_supplied(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="default",
            http_bearer_token="secret-token",
        )

        self.assertIsNotNone(server.settings.auth)
        self.assertEqual(["mcp:read"], server.settings.auth.required_scopes)

    def test_loopback_server_enables_dns_rebinding_protection(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="default",
            host="127.0.0.1",
            port=8123,
            warm_cache=False,
        )

        security = server.settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn("127.0.0.1:*", security.allowed_hosts)
        self.assertIn("http://127.0.0.1:*", security.allowed_origins)

    def test_public_https_issuer_and_explicit_host_origin_are_allowlisted(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="default",
            host="0.0.0.0",
            port=8000,
            http_bearer_token="secret-token",
            auth_issuer_url="https://mcp.example.go.kr",
            allowed_http_hosts=["mcp.example.go.kr"],
            allowed_http_origins=["https://chatgpt.com/"],
            warm_cache=False,
        )

        security = server.settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn("mcp.example.go.kr", security.allowed_hosts)
        self.assertIn("https://mcp.example.go.kr", security.allowed_origins)
        self.assertIn("https://chatgpt.com", security.allowed_origins)

    def test_cli_accepts_repeated_http_host_and_origin_allowlists(self) -> None:
        with patch(
            "sys.argv",
            [
                "run_regulation_mcp.py",
                "--allowed-http-host",
                "mcp.example.go.kr",
                "--allowed-http-host",
                "proxy.example.go.kr",
                "--allowed-http-origin",
                "https://chatgpt.com",
            ],
        ):
            args = parse_args()

        self.assertEqual(["mcp.example.go.kr", "proxy.example.go.kr"], args.allowed_http_host)
        self.assertEqual(["https://chatgpt.com"], args.allowed_http_origin)

    def test_create_server_warms_runtime_cache_by_default(self) -> None:
        with (
            patch(
                "app.mcp_server.regulation_server.warm_mcp_runtime",
                return_value={"warmed": True, "record_count": 1},
            ) as warm,
            patch("app.mcp_server.regulation_server.start_background_tokenizer_warmup") as background_warmup,
        ):
            server = create_regulation_mcp_server(data_dir="data", tenant_id="default")

        warm.assert_called_once()
        background_warmup.assert_not_called()
        self.assertEqual({"warmed": True, "record_count": 1}, server._reg_rag_warmup_status)

    def test_create_server_can_skip_runtime_cache_warmup(self) -> None:
        background_status = {"started": True, "completed": False, "warmup_mode": "background_tokenizer"}
        with (
            patch("app.mcp_server.regulation_server.warm_mcp_runtime") as warm,
            patch(
                "app.mcp_server.regulation_server.start_background_tokenizer_warmup",
                return_value=background_status,
            ) as background_warmup,
        ):
            server = create_regulation_mcp_server(data_dir="data", tenant_id="default", warm_cache=False)

        warm.assert_not_called()
        background_warmup.assert_called_once_with(delay_seconds=5.0)
        self.assertEqual(
            {
                "warmed": False,
                "skipped": True,
                "background_tokenizer_warmup": background_status,
            },
            server._reg_rag_warmup_status,
        )


    def test_cli_preserves_default_and_no_warm_cache_tokenizer_behavior(self) -> None:
        for options, warm_cache in (([], True), (["--no-warm-cache"], False)):
            with self.subTest(options=options), patch("sys.argv", ["run_regulation_mcp.py", *options]), patch(
                "scripts.run_regulation_mcp.create_regulation_mcp_server"
            ) as create:
                self.assertEqual(0, main())
                self.assertEqual(warm_cache, create.call_args.kwargs["warm_cache"])
                self.assertTrue(create.call_args.kwargs["background_tokenizer_warmup"])
                create.return_value.run.assert_called_once_with(transport="stdio")

    def test_cli_background_opt_out_requires_no_warm_cache(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), patch("sys.argv", ["run_regulation_mcp.py", "--no-background-tokenizer-warmup"]), patch(
            "scripts.run_regulation_mcp.create_regulation_mcp_server"
        ) as create:
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(2, raised.exception.code)
            self.assertIn("--no-background-tokenizer-warmup requires --no-warm-cache", stderr.getvalue())
            create.assert_not_called()

    def test_cli_background_opt_out_preserves_scope_and_transport(self) -> None:
        arguments = ["run_regulation_mcp.py", "--no-warm-cache", "--no-background-tokenizer-warmup",
                     "--tenant-id", "synthetic-tenant", "--profile-id", "synthetic-profile",
                     "--tenant-storage-isolation", "--department-id", "synthetic-department",
                     "--actor", "synthetic-reader", "--role", "viewer"]
        with patch("sys.argv", arguments), patch("scripts.run_regulation_mcp.create_regulation_mcp_server") as create:
            self.assertEqual(0, main())
        kwargs = create.call_args.kwargs
        self.assertFalse(kwargs["warm_cache"])
        self.assertFalse(kwargs["background_tokenizer_warmup"])
        self.assertEqual("synthetic-tenant", kwargs["tenant_id"])
        self.assertEqual("synthetic-profile", kwargs["profile_id"])
        self.assertTrue(kwargs["tenant_storage_isolation"])
        self.assertEqual(["synthetic-department"], kwargs["department_ids"])
        self.assertEqual("synthetic-reader", kwargs["actor"])
        self.assertEqual("viewer", kwargs["role"])
        create.return_value.run.assert_called_once_with(transport="stdio")

    def test_cli_background_opt_out_keeps_non_loopback_auth_gate(self) -> None:
        arguments = ["run_regulation_mcp.py", "--no-warm-cache", "--no-background-tokenizer-warmup",
                     "--transport", "streamable-http", "--host", "0.0.0.0"]
        with patch("sys.argv", arguments), patch("scripts.run_regulation_mcp.create_regulation_mcp_server") as create:
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertIn("unauthenticated MCP HTTP/SSE", str(raised.exception))
            create.assert_not_called()

    def test_cli_background_opt_out_keeps_protected_storage_gate(self) -> None:
        arguments = ["run_regulation_mcp.py", "--no-warm-cache", "--no-background-tokenizer-warmup", "--flat-storage"]
        with patch.dict(os.environ, {"APP_ENV": "production"}), patch("sys.argv", arguments), patch(
            "scripts.run_regulation_mcp.create_regulation_mcp_server"
        ) as create:
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertIn("protected environment", str(raised.exception))
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
