from __future__ import annotations

import unittest

from scripts.generate_mcp_client_config import build_mcp_client_config


class McpConnectionContractTests(unittest.TestCase):
    def test_chatgpt_remote_server_args_do_not_duplicate_tool_profile(self) -> None:
        config = build_mcp_client_config(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            public_url="https://mcp.example.go.kr",
            server_name="govreg-local",
        )

        args = config["server_start"]["args"]
        self.assertEqual(1, args.count("--tool-profile"))
        self.assertEqual("chatgpt-data", args[args.index("--tool-profile") + 1])


if __name__ == "__main__":
    unittest.main()
