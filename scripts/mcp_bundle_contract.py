from __future__ import annotations

import re


CHATGPT_PLUGIN_MCP_CONTAINER = "mcpServers"
CHATGPT_PLUGIN_MCP_PATH = "./.mcp.json"
CHATGPT_PLUGIN_VERSION_PATTERN = re.compile(r"^0\.1\.0\+codex\.[0-9a-f]{12}$")


def normalized_chatgpt_plugin_name(server_name: str) -> str:
    """Return the loader-safe plugin package name for an MCP server."""
    normalized = re.sub(r"[^a-z0-9]+", "-", server_name.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return (normalized or "regulation-mcp")[:64].rstrip("-")


def chatgpt_local_marketplace_name(server_name: str) -> str:
    """Return the loader-safe local marketplace name for an MCP server."""
    suffix = "-local"
    base = normalized_chatgpt_plugin_name(server_name)
    if len(base) + len(suffix) > 64:
        base = base[: 64 - len(suffix)].rstrip("-")
    return base + suffix


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
    "claude_api": "claude_api_fragment.json",
    "run_stdio": "run_local_stdio_server.ps1",
    "run_http": "run_http_server.ps1",
    "run_chatgpt": "run_chatgpt_data_server.ps1",
    "openai_tunnel": "run_openai_secure_tunnel.ps1",
    "validate": "validate_mcp_smoke.ps1",
    "client_config_smoke": "validate_client_config_smoke.ps1",
    "remote_validate": "validate_chatgpt_remote_mcp.ps1",
    "doctor": "doctor_mcp_connection.ps1",
    "connect": "connect_mcp_client.ps1",
    "usage_guide": "MCP 사용 시작하기.txt",
    "usage_guide_bat": "설치 후 MCP 사용 방법 보기.bat",
    "codex_plugin_guide": "Codex 플러그인 MCP 입력값.txt",
    "connect_codex_bat": "Codex에 연결하기.bat",
    "connect_chatgpt_desktop_bat": "ChatGPT Desktop에 연결하기.bat",
    "connect_claude_desktop_bat": "Claude Desktop에 연결하기.bat",
    "connect_claude_code_bat": "Claude Code에 연결하기.bat",
    "connect_chatgpt_https_bat": "ChatGPT HTTPS에 연결하기.bat",
    "connect_chatgpt_tunnel_bat": "ChatGPT 보안 Tunnel에 연결하기.bat",
    "connect_claude_https_bat": "Claude HTTPS에 연결하기.bat",
    "chatgpt_desktop_local": "chatgpt_desktop_local_mcp.json",
    "chatgpt_desktop_agent_prompt": "CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md",
    "codex_agent_prompt": "CODEX_AGENT_CONNECT_PROMPT.md",
    "claude_code_agent_prompt": "CLAUDE_CODE_AGENT_CONNECT_PROMPT.md",
    "doctor_bat": "연결 상태 확인하기.bat",
    "install": "install_local_package.ps1",
}

OPTIONAL_SETUP_BUNDLE_FILES = frozenset(
    {
        # Only generated when a public HTTPS MCP URL is configured.
        SETUP_BUNDLE_FILES["claude_code_http"],
    }
)

ALL_SETUP_BUNDLE_FILES = frozenset(SETUP_BUNDLE_FILES.values())
REQUIRED_SETUP_BUNDLE_FILES = frozenset(ALL_SETUP_BUNDLE_FILES - OPTIONAL_SETUP_BUNDLE_FILES)
