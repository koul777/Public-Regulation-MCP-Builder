from __future__ import annotations

import struct
import unittest
from pathlib import Path
from xml.etree import ElementTree


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
            "## 강의 A: Claude Desktop 로컬 STDIO 연결",
            "### A-1. 로컬 STDIO 번들 만들기",
            "### A-2. 생성 파일을 먼저 확인",
            "### A-3. Claude Desktop 설정 파일 열기",
            "### A-4. 기존 설정을 지우지 않고 새 서버만 합치기",
            "### A-5. 저장하고 Claude Desktop을 완전히 다시 시작",
            "### A-6. 번들 자체를 먼저 진단하는 방법",
            '"scripts.run_regulation_mcp"',
            '"PYTHONPATH"',
            '"PYTHONSAFEPATH": "1"',
            "`initialize` → `tools/list` → `search` → `fetch`",
            "기존 내용과 새 내용이 모두 남아 있어야 합니다",
            "설정 > 개발자 > 로컬 MCP 서버 > 구성 편집",
            "Settings > Developer > Edit Config",
            "상태가 `running`",
        ):
            self.assertIn(expected, text)

    def test_readme_contains_beginner_vercel_https_walkthrough(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "## 강의 B: Vercel HTTPS 배포와 연결",
            "### B-2. 배포 전용 폴더 만들기",
            "prepare_vercel_mcp_deployment.py",
            "생성 완료 화면 읽는 법",
            "MCP 파일 묶음 생성 완료`는 Vercel 배포 완료가 아닙니다",
            "### B-3. Vercel CLI 설치하고 로그인",
            "vercel login",
            "MCP_ALLOW_UNAUTHENTICATED_HTTP",
            "MCP_TOOL_PROFILE",
            "Aliased",
            "run_mcp_client_config_smoke.py",
            "mcp_initialized",
            "end_to_end_verified",
            "Customize > Connectors",
            "https://<deployment>/mcp",
        ):
            self.assertIn(expected, text)

    def test_readme_uses_current_workflow_images(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        expected_images = (
            "docs/assets/pr-mcp-builder-hero.png",
            "docs/assets/readme-guide-01-start.png",
            "docs/assets/readme-guide-01-dashboard.png",
            "docs/assets/readme-guide-02-upload.png",
            "docs/assets/readme-guide-02-progress.png",
            "docs/assets/readme-guide-02-preprocess-complete.png",
            "docs/assets/readme-guide-03-load.png",
            "docs/assets/readme-guide-03-multi-regulation.png",
            "docs/assets/readme-guide-03-chunk-context.png",
            "docs/assets/readme-guide-04-human-review.png",
            "docs/assets/readme-guide-04-approval-actions.png",
            "docs/assets/readme-guide-04-indexed.png",
            "docs/assets/readme-claude-mcp-01-bundle.svg",
            "docs/assets/readme-claude-mcp-02-config.svg",
            "docs/assets/readme-claude-mcp-03-verify.svg",
            "docs/assets/readme-vercel-claude-connection.svg",
            "docs/assets/readme-course-00-completion-guide.png",
            "docs/assets/readme-course-01-stdio-bundle.png",
            "docs/assets/readme-course-02-claude-stdio-config.png",
            "docs/assets/readme-course-03-vercel-production.png",
            "docs/assets/readme-course-04-claude-remote-connector.png",
            "docs/assets/readme-course-05-mcp-verification.png",
        )

        for image_path in expected_images:
            self.assertIn(image_path, text)
            self.assertTrue((REPO_ROOT / image_path).is_file(), image_path)

        for retired_image in (
            "readme-guide-05-mcp-next.png",
            "readme-guide-06-bundle.png",
            "readme-guide-06-generated-files.png",
            "readme-guide-07-chatgpt-https.png",
            "readme-guide-08-claude-https.png",
            "readme-guide-09-generated-bat-files.png",
        ):
            self.assertNotIn(retired_image, text)

    def test_readme_course_captures_are_valid_pngs(self) -> None:
        for filename in (
            "readme-course-00-completion-guide.png",
            "readme-course-01-stdio-bundle.png",
            "readme-course-02-claude-stdio-config.png",
            "readme-course-03-vercel-production.png",
            "readme-course-04-claude-remote-connector.png",
            "readme-course-05-mcp-verification.png",
        ):
            path = REPO_ROOT / "docs" / "assets" / filename
            with self.subTest(filename=filename):
                self.assertTrue(path.is_file(), path)
                png = path.read_bytes()
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", png[16:24]), (1600, 900))
                self.assertGreater(path.stat().st_size, 10_000)

    def test_claude_mcp_guide_svgs_are_valid_and_accessible(self) -> None:
        for filename in (
            "readme-claude-mcp-01-bundle.svg",
            "readme-claude-mcp-02-config.svg",
            "readme-claude-mcp-03-verify.svg",
        ):
            path = REPO_ROOT / "docs" / "assets" / filename
            root = ElementTree.parse(path).getroot()
            namespace = {"svg": "http://www.w3.org/2000/svg"}

            self.assertTrue(root.tag.endswith("svg"))
            self.assertEqual("0 0 1600 900", root.attrib.get("viewBox"))
            self.assertIsNotNone(root.find("svg:title", namespace))
            self.assertIsNotNone(root.find("svg:desc", namespace))


if __name__ == "__main__":
    unittest.main()
