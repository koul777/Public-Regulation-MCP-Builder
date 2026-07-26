from __future__ import annotations


SETUP_BUNDLE_FILES = {
    "manifest": "manifest.json",
    "bundle_status": "bundle_status.json",
    "full_config": "mcp_config.bundle.json",
    "readme": "README.md",
    "readme_ko": "README.ko.md",
    "codex_config": "codex_config_snippet.toml",
    "claude_desktop": "claude_desktop_config.json",
    "claude_code_stdio": "claude_code_add_stdio.ps1",
    "claude_code_http": "claude_code_add_http.ps1",
    "stdio_launcher": "run_mcp_stdio_server.ps1",
    "chatgpt": "chatgpt_connector.json",
    "claude_remote": "claude_https_mcp.json",
    "run_stdio": "run_local_stdio_server.ps1",
    "validate": "validate_mcp_smoke.ps1",
    "client_config_smoke": "validate_client_config_smoke.ps1",
    "remote_validate": "validate_chatgpt_remote_mcp.ps1",
    "doctor": "doctor_mcp_connection.ps1",
    "connect": "connect_mcp_client.ps1",
    "usage_guide": "MCP \uc0ac\uc6a9 \uc2dc\uc791\ud558\uae30.txt",
    "chatgpt_desktop_local": "chatgpt_desktop_local_mcp.json",
    "install": "install_local_package.ps1",
}

LEGACY_CONNECTION_ARTIFACT_FILENAMES = frozenset(
    {
        "\uc124\uce58 \ud6c4 MCP \uc0ac\uc6a9 \ubc29\ubc95 \ubcf4\uae30.bat",
        "Codex\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "ChatGPT Desktop\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "Claude Desktop\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "Claude Code\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "ChatGPT HTTPS\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "ChatGPT \ubcf4\uc548 Tunnel\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "Claude HTTPS\uc5d0 \uc5f0\uacb0\ud558\uae30.bat",
        "CHATGPT_DESKTOP_CONNECT_GUIDE.md",
        "CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md",
        "CODEX_AGENT_CONNECT_PROMPT.md",
        "CLAUDE_CODE_AGENT_CONNECT_PROMPT.md",
        "\uc5f0\uacb0 \uc0c1\ud0dc \ud655\uc778\ud558\uae30.bat",
        "Codex \ud50c\ub7ec\uadf8\uc778 MCP \uc785\ub825\uac12.txt",
        "claude_api_fragment.json",
        "run_http_server.ps1",
        "run_chatgpt_data_server.ps1",
        "run_openai_secure_tunnel.ps1",
    }
)

OPTIONAL_SETUP_BUNDLE_FILES = frozenset(
    {
        # Only generated when a public HTTPS MCP URL is configured.
        SETUP_BUNDLE_FILES["claude_code_http"],
    }
)

ALL_SETUP_BUNDLE_FILES = frozenset(SETUP_BUNDLE_FILES.values())
REQUIRED_SETUP_BUNDLE_FILES = frozenset(ALL_SETUP_BUNDLE_FILES - OPTIONAL_SETUP_BUNDLE_FILES)
