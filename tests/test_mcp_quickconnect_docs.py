from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class McpQuickConnectDocsTests(unittest.TestCase):
    def test_quickconnect_doc_lists_only_supported_connection_paths(self) -> None:
        text = (REPO_ROOT / "docs" / "mcp_quickconnect_ko.md").read_text(
            encoding="utf-8"
        )

        for expected in (
            "claude_desktop_config.json",
            "claude_code_add_stdio.ps1",
            "claude_code_add_http.ps1",
            "codex_config_snippet.toml",
            "chatgpt_desktop_local_mcp.json",
            "command",
            "args",
            "cwd",
            "env",
            "https://<deployment>/mcp",
            "reg-rag-mcp-vercel-stage",
            "MCP_ALLOW_UNAUTHENTICATED_HTTP",
            "bearer_token_env_var",
            "https://learn.chatgpt.com/docs/extend/mcp",
            "https://code.claude.com/docs/en/mcp",
            "설정 > 개발자 > 로컬 MCP 서버 > 구성 편집",
            "파일·커넥터 추가 > Connectors",
            "https://modelcontextprotocol.io/docs/develop/connect-local-servers",
        ):
            self.assertIn(expected, text)
        for retired in (
            ".bat",
            "AGENT_CONNECT_PROMPT",
            "run_openai_secure_tunnel.ps1",
            "chatgpt-desktop-local-plugin",
        ):
            self.assertNotIn(retired, text)

    def test_client_config_doc_matches_direct_stdio_and_vercel_contract(self) -> None:
        text = (
            REPO_ROOT / "docs" / "mcp_client_config_examples_ko.md"
        ).read_text(encoding="utf-8")

        self.assertIn("command", text)
        self.assertIn("args", text)
        self.assertIn("cwd", text)
        self.assertIn("env", text)
        self.assertIn("codex_config_snippet.toml", text)
        self.assertIn("chatgpt_desktop_local_mcp.json", text)
        self.assertIn("claude_desktop_config.json", text)
        self.assertIn("claude_code_add_stdio.ps1", text)
        self.assertIn("claude_code_add_http.ps1", text)
        self.assertIn("https://<deployment>/mcp", text)
        self.assertIn("bearer_token_env_var", text)
        self.assertIn("MCP_ALLOWED_HTTP_HOSTS", text)
        self.assertNotIn("run_openai_secure_tunnel.ps1", text)
        self.assertNotIn("AGENT_CONNECT_PROMPT", text)

    def test_claude_beginner_guide_preserves_verified_stdio_contract(self) -> None:
        text = (REPO_ROOT / "docs" / "mcp_quickconnect_ko.md").read_text(
            encoding="utf-8"
        )

        for expected in (
            '"-m"',
            '"scripts.run_regulation_mcp"',
            '"PYTHONPATH"',
            '"PYTHONSAFEPATH": "1"',
            "`initialize`, `tools/list`, `search`, `fetch`",
            "기존 서버와 `preferences`",
            "작업 표시줄 알림 영역에서도 **종료**",
        ):
            self.assertIn(expected, text)
        self.assertTrue(
            text.rstrip().endswith(
                "HWP/HWPX 문서 구조와 표 추출 교차 검증에는 Kordoc을 사용했습니다."
            )
        )

    def test_readme_links_quickconnect_and_vercel_docs(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("docs/mcp_quickconnect_ko.md", text)
        self.assertIn("docs/vercel_https_mcp_ko.md", text)
        self.assertIn("Settings > MCP servers > Add server", text)
        self.assertIn("설정 > 개발자 > 로컬 MCP 서버 > 구성 편집", text)
        self.assertIn("파일·커넥터 추가 > Connectors", text)
        self.assertIn("Customize > Connectors", text)
        self.assertIn(
            "https://modelcontextprotocol.io/docs/develop/connect-local-servers",
            text,
        )
        self.assertIn("https://<deployment>/mcp", text)
        self.assertNotIn("CHATGPT_DESKTOP_CONNECT_GUIDE.md", text)
        self.assertNotIn("CODEX_AGENT_CONNECT_PROMPT.md", text)
        self.assertNotIn("CLAUDE_CODE_AGENT_CONNECT_PROMPT.md", text)

    def test_readme_contains_beginner_claude_stdio_walkthrough(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "### 1. MCP 파일 묶음 만들기",
            "### 2. 생성된 설정과 Claude 설정 열기",
            "### 3. 새 서버 항목 합치기",
            "### 4. Claude Desktop 완전히 다시 시작하기",
            "### 5. search와 fetch로 실제 연결 확인하기",
            '"scripts.run_regulation_mcp"',
            '"PYTHONPATH"',
            '"PYTHONSAFEPATH": "1"',
            "`initialize` → `tools/list` → `search` → `fetch`",
            "다른 서버나\n`preferences`",
            "상태가 `running`",
        ):
            self.assertIn(expected, text)

    def test_readme_uses_current_workflow_images(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        expected_images = (
            "docs/assets/pr-mcp-builder-hero.png",
            "docs/assets/readme-guide-01-start.png",
            "docs/assets/readme-guide-02-upload.png",
            "docs/assets/readme-guide-02-preprocess-complete.png",
            "docs/assets/readme-guide-03-multi-regulation.png",
            "docs/assets/readme-guide-03-chunk-context.png",
            "docs/assets/readme-guide-04-human-review.png",
        )

        for image_path in expected_images:
            self.assertIn(image_path, text)
            self.assertTrue((REPO_ROOT / image_path).is_file(), image_path)

        for retired_image in (
            "readme-guide-05-mcp-next.png",
            "readme-guide-06-bundle.png",
            "readme-guide-06-generated-files.png",
            "readme-guide-09-generated-bat-files.png",
        ):
            self.assertNotIn(retired_image, text)


if __name__ == "__main__":
    unittest.main()
