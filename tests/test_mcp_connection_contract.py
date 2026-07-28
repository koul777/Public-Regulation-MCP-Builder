from __future__ import annotations

import unittest

from scripts.generate_mcp_client_config import build_mcp_client_config


class McpConnectionContractTests(unittest.TestCase):
    def test_chatgpt_remote_uses_deployed_url_without_local_start_args(self) -> None:
        config = build_mcp_client_config(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            public_url="https://mcp.example.go.kr",
            server_name="govreg-local",
        )

        self.assertNotIn("server_start", config)
        self.assertEqual(
            "https://mcp.example.go.kr/mcp",
            config["connector_url"],
        )
        self.assertEqual(
            [
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
                "search",
                "fetch",
            ],
            config["compatible_tools"],
        )
        self.assertEqual(
            "MCP_AUTH_TOKEN",
            config["config_toml"]["bearer_token_env_var"],
        )


if __name__ == "__main__":
    unittest.main()
