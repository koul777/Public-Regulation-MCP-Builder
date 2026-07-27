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
        self.assertIn("설정 > 개발자 > 로컬 MCP 서버", text)
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
            "## 0. 완전 처음이라면 여기부터",
            "### 0-2. 이 문서의 명령과 경로 읽는 법",
            "### 0-3. 원하는 폴더에서 PowerShell 여는 가장 쉬운 방법",
            "## 강의 A: Claude Desktop 로컬 STDIO 연결",
            "### A-1. 로컬 STDIO 번들 만들기",
            "### A-2. 생성 파일을 먼저 확인",
            "### A-3. 자동 연결 마법사 실행 — 처음 사용자는 이 방법을 권장",
            "### A-4. 자동 연결이 안 될 때 Claude Desktop 설정 파일 열기",
            "### A-5. 기존 설정을 지우지 않고 새 서버만 수동 병합하기",
            "### A-6. 저장하고 Claude Desktop을 완전히 다시 시작",
            "### A-7. 번들 자체를 먼저 진단하는 방법",
            "같은 Windows PC에서 지금 바로 Claude Desktop에 붙일 것",
            "JSON 편집이 자신 없으면 아래 순서로 복구합니다.",
            "connect_mcp_client.ps1",
            "-InstallClaudeDesktop",
            "Installed-config stdio verification passed",
            "CLAUDE DESKTOP VERIFICATION REQUIRED",
            "claude_desktop_config.json.bak-",
            "docs/assets/readme-course-02-claude-settings-menu.png",
            "docs/assets/readme-course-02b-claude-local-mcp-server.png",
            "docs/assets/readme-course-07-claude-config-editor.png",
            "생성된 절대 Python 경로 또는 fallback `powershell.exe`",
            '"scripts.run_regulation_mcp"',
            '"PYTHONPATH"',
            '"PYTHONSAFEPATH": "1"',
            "`initialize` → `tools/list` → `search` → `fetch`",
            "기존 내용과 새 내용이 모두 남아 있어야 합니다",
            "설정 > 개발자 > 로컬 MCP 서버 > 구성 편집",
            "Settings > Developer > Edit Config",
            "%APPDATA%\\Claude\\claude_desktop_config.json",
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
            "다른 PC, 다른 계정, 모바일, 클라우드 AI에서 쓸 것",
            "### B-3. Vercel CLI 설치하고 로그인",
            "node --version",
            "npm --version",
            "vercel --version",
            "vercel login",
            "홈페이지에도 들어가지만 실제 배포 명령은 PowerShell에서 실행",
            "MCP_ALLOW_UNAUTHENTICATED_HTTP",
            "MCP_TOOL_PROFILE",
            "Aliased",
            "브라우저로 읽는 홈페이지가 아니므로",
            "docs/assets/readme-course-08-chatgpt-http-form.png",
            "**기본 token 환경 변수**",
            "**환경 변수의 헤더**",
            "**URL (MCP URL / Server URL)**",
            "**Command / Executable** | 넣지 않음",
            "run_mcp_client_config_smoke.py",
            "mcp_initialized",
            "end_to_end_verified",
            "Customize > Connectors",
            "run_mcp_stdio_server.ps1",
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
            "docs/assets/readme-course-00b-real-completion.png",
            "docs/assets/readme-course-01-stdio-bundle.png",
            "docs/assets/readme-course-03-vercel-production.png",
            "docs/assets/readme-course-04-claude-remote-connector.png",
            "docs/assets/readme-course-05-mcp-verification.png",
            "docs/assets/readme-course-02-claude-settings-menu.png",
            "docs/assets/readme-course-02b-claude-local-mcp-server.png",
            "docs/assets/readme-course-06-chatgpt-plugin-home.png",
            "docs/assets/readme-course-06-chatgpt-plugin-settings.png",
            "docs/assets/readme-course-06-chatgpt-mcp-tab.png",
            "docs/assets/readme-course-06-chatgpt-stdio-form.png",
            "docs/assets/readme-course-06b-chatgpt-stdio-filled.png",
            "docs/assets/readme-course-07-claude-config-editor.png",
            "docs/assets/readme-course-08-chatgpt-http-form.png",
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

    def test_readme_real_settings_captures_are_valid_pngs(self) -> None:
        for filename in (
            "readme-course-00b-real-completion.png",
            "readme-course-02-claude-settings-menu.png",
            "readme-course-02b-claude-local-mcp-server.png",
            "readme-course-06-chatgpt-plugin-home.png",
            "readme-course-06-chatgpt-plugin-settings.png",
            "readme-course-06-chatgpt-mcp-tab.png",
            "readme-course-06-chatgpt-stdio-form.png",
            "readme-course-06b-chatgpt-stdio-filled.png",
            "readme-course-07-claude-config-editor.png",
            "readme-course-08-chatgpt-http-form.png",
        ):
            path = REPO_ROOT / "docs" / "assets" / filename
            with self.subTest(filename=filename):
                self.assertTrue(path.is_file(), path)
                png = path.read_bytes()
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", png[16:24])
                self.assertGreaterEqual(width, 1_580)
                self.assertGreaterEqual(height, 900)
                self.assertGreater(path.stat().st_size, 50_000)

    def test_readme_explains_real_capture_clicks_values_and_success_states(
        self,
    ) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "계정 이름, 이메일, 최근 대화, 로컬 절대경로",
            "가려진 빈칸을 비워 두라는 뜻은 아닙니다.",
            "실제 생성 완료 화면에서 확인할 곳",
            "Vercel은 [B-6](#b-6-production-배포)의 `vercel --prod`",
            "실제 화면 1 — Claude Desktop에서 설정 열기",
            "실제 화면 2 — `claude_desktop_config.json`에서 병합할 위치",
            "실제 화면 3 — `running` 확인",
            "플러그인 / 앱 / MCP / 스킬",
            "**+ 서버 추가**",
            "**예시 placeholder**",
            "**이름 (Name)**",
            "**실행 명령 (Executable / Command)**",
            "**인자 (Arguments / args)**",
            "**작업 중인 디렉터리 (Working directory / cwd)**",
            "실제 입력 완료 화면 — `powershell.exe` 방식",
            "`-File` 바로 다음 칸",
            "캡처의 마지막 보이는 줄에서 임의로 끝내지 마세요.",
            "**스트리밍 가능한 HTTP**",
            "`https://mcp.example.com/mcp`",
            "`MCP_BEARER_TOKEN`",
        ):
            self.assertIn(expected, text)

    def test_claude_mcp_guide_svgs_are_valid_and_accessible(self) -> None:
        for filename in (
            "readme-claude-mcp-01-bundle.svg",
            "readme-claude-mcp-02-config.svg",
            "readme-claude-mcp-03-verify.svg",
            "readme-course-06-chatgpt-desktop-stdio-settings.svg",
            "readme-course-07-claude-desktop-config-editor.svg",
            "readme-course-08-vercel-http-mcp-settings.svg",
        ):
            path = REPO_ROOT / "docs" / "assets" / filename
            root = ElementTree.parse(path).getroot()
            namespace = {"svg": "http://www.w3.org/2000/svg"}

            self.assertTrue(root.tag.endswith("svg"))
            self.assertEqual("0 0 1600 900", root.attrib.get("viewBox"))
            self.assertIsNotNone(root.find("svg:title", namespace))
            self.assertIsNotNone(root.find("svg:desc", namespace))

    def test_readme_maps_chatgpt_desktop_fields_to_generated_ui_fields(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "**이름 (Name)** | `ui_fields.name`",
            "**유형 (Transport / Type)** | `ui_fields.transport`",
            "**실행 명령 (Executable / Command)** | `ui_fields.command`",
            "**작업 중인 디렉터리 (Working directory / cwd)** | `ui_fields.cwd`",
            "**인자 (Arguments / args)** | `ui_fields.args`",
            "**환경 변수 (Environment / env)** | `ui_fields.env`",
            "**환경 변수 패스스루 (Environment passthrough)** | `ui_fields.env_passthrough`",
            "Command 칸에는\n`powershell.exe`만",
            "Arguments 칸에\n각각 별도 항목",
            "직접 Python 방식이 생성되었다면",
        ):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
