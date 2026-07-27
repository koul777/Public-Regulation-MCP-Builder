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
            "scripts.run_regulation_mcp",
            "PYTHONPATH",
            "PYTHONSAFEPATH",
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
        self.assertIn("Aliased:", text)
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
            "### A-1. Builder에서 Claude Desktop 번들 만들기",
            "### A-2. Builder에서 JSON 복사하기",
            "### A-3. Claude Desktop 설정 파일 열기",
            "### A-4. JSON 붙여 넣고 저장하기",
            "### A-5. Claude Desktop 완전히 종료하고 다시 열기",
            "### A-6. `running` 확인하기",
            "### A-7. `search`와 `fetch` 확인하기",
            "### A-8. 기존 설정이 있을 때 병합하기",
            "### A-9. `disconnected`일 때 진단하기",
            "같은 Windows PC에서 지금 바로 Claude Desktop에 붙일 것",
            "**처음 연결이면 A-1부터 A-7까지만 그대로 하면 끝입니다.**",
            "이 두 상자의 복사 아이콘은 누르지 않습니다.",
            "처음 연결할 때: 설정 파일 전체에 붙여 넣을 JSON 복사",
            "병합할 `mcpServers` JSON 복사",
            "**붙여 넣을 위치는 파일 전체입니다.**",
            "초보자는 `mcpServers` 안쪽 줄을 손으로 맞추지 않습니다.",
            "기존 서버가 있을 때: `mcpServers` 안에 넣을 새 서버 한 항목 복사",
            "Windows 작업표시줄도 개인정보 노출을 막기 위해 제거했습니다.",
            "connect_mcp_client.ps1",
            "-InstallClaudeDesktop",
            "Installed-config stdio verification passed",
            "CLAUDE DESKTOP VERIFICATION REQUIRED",
            "docs/assets/readme-course-02-claude-settings-menu.png",
            "docs/assets/readme-course-02c-claude-developer-config-edit.png",
            "docs/assets/readme-course-02d-claude-config-file-explorer.png",
            "docs/assets/readme-course-02b-claude-local-mcp-server.png",
            "docs/assets/readme-course-07-claude-config-editor.png",
            "`initialize` → `tools/list` → `search` → `fetch`",
            "서버 이름 옆 파란 배지가 **`running`**",
            "claude mcp get test2",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("<생성된-", text)

    def test_readme_contains_field_by_field_chatgpt_stdio_walkthrough(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "## ChatGPT Desktop 로컬 STDIO 연결",
            "### C-1. Builder에서 ChatGPT Desktop 번들 만들기",
            "### C-2. Builder에서 복사할 값 찾기",
            "### C-3. ChatGPT Desktop에서 MCP 추가 화면 열기",
            "### C-4. Name과 Command 넣기",
            "### C-5. Arguments를 한 줄씩 서로 다른 칸에 넣기",
            "### C-6. Environment와 Working directory 넣고 저장하기",
            "### C-7. 서버 켜고 `search`와 `fetch` 확인하기",
            "Argument 1/17 — 아래 한 줄만 복사",
            "Argument 2/17 — 아래 한 줄만 복사",
            "`Argument 17/17`이 마지막",
            "**+ 인자 추가**를 한 번 누릅니다.",
            "바로 아래 코드 상자의 **복사 아이콘**을 누릅니다.",
            "**한 인자 칸에는 한 줄만 넣습니다.**",
            "Builder에 표시된 개수 = ChatGPT의 인자 칸 개수",
            "**실행 명령** 칸",
            "`powershell.exe`만 넣습니다.",
            "첫 값부터 **+ 환경 변수 추가**를 누르지 않습니다.",
            "첫 값부터 **+ 변수 추가**를 누르지 않습니다.",
            "**+ 환경 변수 추가**",
            "**Working directory 복사**",
            "**설정 → 플러그인 → MCP**",
            "긴 JSON을 해석하지 말고 현재 화면의 개별 복사",
            "docs/assets/readme-course-06-chatgpt-stdio-form.png",
            "docs/assets/readme-course-06b-chatgpt-stdio-filled.png",
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
            '$VercelProject = Read-Host "새 Vercel 프로젝트 이름"',
            '$McpUrl = Read-Host "B-6에서 복사한 전체 /mcp 주소"',
            "claude mcp add --transport http --scope user",
            "최종 URL을 넣어 번들을 **다시 생성한 경우에만**",
        ):
            self.assertIn(expected, text)
        self.assertGreaterEqual(
            text.count('$StageDir = Read-Host "B-2에서 만든 배포 전용 폴더 전체 경로"'),
            3,
        )
        self.assertNotIn("<프로젝트-이름>", text)
        self.assertNotIn("<deployment>", text)

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
            "docs/assets/readme-claude-mcp-03-verify.svg",
            "docs/assets/readme-vercel-claude-connection.svg",
            "docs/assets/readme-course-00-completion-guide.png",
            "docs/assets/readme-course-00b-real-completion.png",
            "docs/assets/readme-course-00c-builder-chatgpt-stdio-selection.png",
            "docs/assets/readme-course-00d-builder-claude-selection.png",
            "docs/assets/readme-course-01b-builder-chatgpt-stdio-output.png",
            "docs/assets/readme-course-03-vercel-production.png",
            "docs/assets/readme-course-04-claude-remote-connector.png",
            "docs/assets/readme-course-04b-builder-claude-direct-config.png",
            "docs/assets/readme-course-05-mcp-verification.png",
            "docs/assets/readme-course-02-claude-settings-menu.png",
            "docs/assets/readme-course-02c-claude-developer-config-edit.png",
            "docs/assets/readme-course-02d-claude-config-file-explorer.png",
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
            "readme-course-00c-builder-chatgpt-stdio-selection.png",
            "readme-course-00d-builder-claude-selection.png",
            "readme-course-01b-builder-chatgpt-stdio-output.png",
            "readme-course-02-claude-settings-menu.png",
            "readme-course-02c-claude-developer-config-edit.png",
            "readme-course-02d-claude-config-file-explorer.png",
            "readme-course-02b-claude-local-mcp-server.png",
            "readme-course-04b-builder-claude-direct-config.png",
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
                self.assertGreaterEqual(width, 1_400)
                self.assertGreaterEqual(height, 780)
                self.assertGreater(path.stat().st_size, 50_000)
                chunk_types: list[bytes] = []
                offset = 8
                while offset + 12 <= len(png):
                    chunk_length = struct.unpack(">I", png[offset : offset + 4])[0]
                    chunk_type = png[offset + 4 : offset + 8]
                    chunk_types.append(chunk_type)
                    offset += 12 + chunk_length
                    if chunk_type == b"IEND":
                        break
                for private_chunk in (b"tEXt", b"zTXt", b"iTXt", b"eXIf"):
                    self.assertNotIn(private_chunk, chunk_types)

    def test_claude_config_editor_capture_excludes_windows_taskbar(self) -> None:
        path = (
            REPO_ROOT
            / "docs"
            / "assets"
            / "readme-course-07-claude-config-editor.png"
        )
        png = path.read_bytes()

        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (1_586, 952))

    def test_readme_explains_real_capture_clicks_values_and_success_states(
        self,
    ) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "계정 이름, 이메일, 최근 대화, 로컬 절대경로",
            "가려진 빈칸을 비워 두라는 뜻은 아닙니다.",
            "실제 생성 완료 화면에서 확인할 곳",
            "처음부터 연결 완료까지 완주 지도",
            "완주 경로 A — Claude Desktop 로컬 STDIO",
            "완주 경로 C — ChatGPT Desktop 로컬 STDIO",
            "완주 경로 B — Vercel Streamable HTTP",
            "생성 버튼을 누르기 전 — 연결 앱·저장 폴더·서버 이름",
            "Builder에서 JSON 복사하기",
            "Vercel은 [B-6](#b-6-production-배포)의 `vercel --prod`",
            "왼쪽 아래 **프로필 영역**",
            "**구성 편집**",
            "**`claude_desktop_config`** 파일",
            "**붙여 넣을 위치는 파일 전체입니다.**",
            "서버 이름 옆 파란 배지가 **`running`**",
            "%APPDATA%\\Claude\\claude_desktop_config.json",
            "플러그인 / 앱 / MCP / 스킬",
            "**+ 서버 추가**",
            "Argument 1/17",
            "**+ 인자 추가**",
            "**한 인자 칸에는 한 줄만 넣습니다.**",
            "**작업 중인 디렉터리**",
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

    def test_readme_maps_chatgpt_desktop_builder_boxes_to_app_fields(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "**Name** 아래 코드 상자",
            "맨 위 **이름** 칸",
            "**Command 복사** 아래 코드 상자",
            "ChatGPT의 **실행 명령** 칸",
            "ChatGPT의 첫 번째 **인자** 칸",
            "ChatGPT의 두 번째 인자 칸",
            "`Argument 17/17`이 마지막",
            "Builder에 `Environment (0개)`",
            "Builder의 **Working directory 복사**",
            "오른쪽 아래 **저장**",
        ):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
