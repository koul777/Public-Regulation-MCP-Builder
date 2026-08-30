from __future__ import annotations

import json
import re
import struct
import unittest
from pathlib import Path
from xml.etree import ElementTree


REPO_ROOT = Path(__file__).resolve().parents[1]

QWEN_README_CAPTURE_FILENAMES = (
    "readme-qwen-01-mode-choice.png",
    "readme-qwen-02-launch.png",
    "readme-qwen-03-ready.png",
    "readme-qwen-04-progress.png",
    "readme-qwen-05-answer-citations.png",
    "readme-qwen-06-mcp-path.png",
)
QWEN_README_DEMO_FILENAMES = (
    "public-regulation-qwen-rag-demo.gif",
    "public-regulation-qwen-rag-demo.mp4",
)
PUBLIC_README_ASSET_PREFIX = (
    "https://raw.githubusercontent.com/koul777/"
    "Public-Regulation-MCP-Builder/main/docs/assets/"
)
PUBLIC_ASSET_FORBIDDEN_PATH_MARKERS = (
    "c:\\users\\",
    "c:/users/",
    "/users/",
    "c:\\workspace\\",
    "c:/workspace/",
)


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
            "https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta",
            "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels",
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
        self.assertIn("Settings > Apps > Advanced settings > Developer mode", text)
        self.assertIn("OpenAI Secure MCP Tunnel", text)
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

    def test_readme_maps_the_five_builder_targets_to_methods_a_through_e(
        self,
    ) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "## 2. 다섯 방법 중 하나 선택하기",
            "### 방법 A — Claude Code 로컬 STDIO",
            "### 방법 B — Codex CLI / Codex IDE 로컬 STDIO",
            "### 방법 C — Claude Desktop 로컬 STDIO",
            "### 방법 D — ChatGPT · Vercel HTTPS MCP",
            "### 방법 E — Claude · Vercel HTTPS MCP",
            "`Claude Code`",
            "`Codex CLI / Codex IDE`",
            "`Claude Desktop`",
            "`ChatGPT · Vercel HTTPS MCP`",
            "`Claude · Vercel HTTPS MCP`",
            "**Claude Code / Claude CLI**에 붙일 것 → **방법 A**",
            "**Claude Desktop 앱의 JSON 설정 파일**에 붙일 것 → **방법 C**",
            "**A는 Claude Code(Claude CLI)** 입니다.",
            "**C는 Claude Desktop** 입니다.",
            "Vercel 주소가 아직 없어도 D를 선택",
            "배포 준비용 MCP 묶음",
            "방법 A·B·C는 로컬 STDIO이고, 방법 D·E는 Vercel HTTPS입니다.",
            "같은 PC의 Claude Code에서 사용",
            "같은 PC의 Codex CLI 또는 IDE에서 사용",
            "ChatGPT에서 사용",
            "Claude에서 Vercel 주소로 원격 사용",
        ):
            self.assertIn(expected, text)

        detailed_headings = (
            "## 방법 A 상세: Claude Code 로컬 STDIO 연결",
            "## 방법 B 상세: Codex CLI / Codex IDE 로컬 STDIO 연결",
            "## 방법 C 상세: Claude Desktop 로컬 STDIO 연결",
            "## 방법 D·E 공통 준비: Vercel HTTPS 배포와 검증",
            "## 방법 D 상세: ChatGPT · Vercel HTTPS MCP 연결",
            "## 방법 E 상세: Claude · Vercel HTTPS MCP 연결",
        )
        positions = [text.index(heading) for heading in detailed_headings]
        self.assertEqual(sorted(positions), positions)

        self.assertNotIn("완주 경로", text)
        self.assertNotIn("### 초보자용 한 줄 결정", text)
        self.assertNotIn("두 화면을 섞지 마세요.", text)
        self.assertNotIn("로컬은 Developer > Edit Config", text)
        self.assertNotIn("로컬 연결이면 Claude Desktop을 설치", text)

    def test_readme_contains_beginner_claude_stdio_walkthrough(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "## 0. 완전 처음이라면 여기부터",
            "### 0-2. 이 문서의 명령과 경로 읽는 법",
            "### 0-3. 원하는 폴더에서 PowerShell 여는 가장 쉬운 방법",
            "## 방법 C 상세: Claude Desktop 로컬 STDIO 연결",
            "### C-1. Builder에서 Claude Desktop 번들 만들기",
            "### C-2. Builder에서 JSON 복사하기",
            "### C-3. Claude Desktop 설정 파일 열기",
            "### C-4. JSON 붙여 넣고 저장하기",
            "### C-5. Claude Desktop 완전히 종료하고 다시 열기",
            "### C-6. `running` 확인하기",
            "### C-7. `search`와 `fetch` 확인하기",
            "### C-8. 기존 설정이 있을 때 병합하기",
            "### C-9. `disconnected`일 때 진단하기",
            "**Builder의 첫 번째 JSON 상자 전체를 복사해서, 열린 `claude_desktop_config.json` 파일 전체를 덮어씁니다.**",
            "Claude Desktop과 이 프로그램을 같은 PC에서 사용",
            "**처음 연결이면 C-1부터 C-7까지만 그대로 하면 끝입니다.**",
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

    def test_readme_shows_exact_claude_json_merge_before_and_after(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "#### 직접 병합할 때 정확히 어디에 붙여 넣는지",
            "**첫 번째 JSON 상자**는 새 파일이거나 빈 파일일 때 파일 전체에 붙여 넣습니다.",
            "**두 번째 JSON 상자**는 기존 파일에 다른 서버가 있을 때 `\"mcpServers\"` 중괄호 안에만 붙여 넣습니다.",
            "기존 `weather-mcp`의 마지막 `}` 뒤에 쉼표 `,`를 하나 붙이고",
            "새 서버는 `\"mcpServers\": {`와 그 닫는 `}` **사이**에 있습니다.",
            "기존 `preferences`는 `mcpServers` 밖에 그대로 남아 있습니다.",
            '"PYTHONPATH": "C:\\\\Public Regulation MCP"',
            '"PYTHONSAFEPATH": "1"',
        ):
            self.assertIn(expected, text)

        final_marker = "최종 파일은 아래처럼 됩니다."
        final_start = text.index("```json", text.index(final_marker)) + len("```json")
        final_end = text.index("```", final_start)
        merged = json.loads(text[final_start:final_end])

        self.assertEqual({"weather-mcp", "기관-규정"}, set(merged["mcpServers"]))
        self.assertEqual({"theme": "dark"}, merged["preferences"])
        self.assertEqual(
            "C:\\Public Regulation MCP",
            merged["mcpServers"]["기관-규정"]["env"]["PYTHONPATH"],
        )
        self.assertEqual(
            ["-m", "scripts.run_regulation_mcp"],
            merged["mcpServers"]["기관-규정"]["args"][:2],
        )

    def test_readme_separates_codex_local_from_chatgpt_remote(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "## 방법 B 상세: Codex CLI / Codex IDE 로컬 STDIO 연결",
            "### B-2. Codex CLI·IDE에 생성된 TOML 넣기",
            "notepad %USERPROFILE%\\.codex\\config.toml",
            "`codex_config_snippet.toml` 전체를 **그 아래에** 붙이면",
            "[mcp_servers.weather]",
            "[mcp_servers.기관_규정]",
            "같은 `[mcp_servers.기관_규정]` 제목이 이미 있으면 두 개를 만들지 말고",
            "Codex 연결\n완료입니다.",
            "ChatGPT는 로컬 STDIO MCP에 직접 연결하지 않습니다",
            "ChatGPT 웹 원격 MCP",
            "Settings > Apps > Advanced settings > Developer mode",
            "OpenAI Secure MCP Tunnel",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("ChatGPT Desktop 로컬 STDIO", text)
        self.assertNotIn("+ 서버 추가 > STDIO", text)
    def test_readme_contains_beginner_vercel_https_walkthrough(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "## 방법 D·E 공통 준비: Vercel HTTPS 배포와 검증",
            "### V-2. 배포 전용 폴더 만들기",
            "prepare_vercel_mcp_deployment.py",
            "생성 완료 화면 읽는 법",
            "MCP 파일 묶음 생성 완료`는 Vercel 배포 완료가 아닙니다",
            "ChatGPT에서 사용",
            "### V-3. Vercel CLI 설치하고 로그인",
            "node --version",
            "npm --version",
            "vercel --version",
            "vercel login",
            "홈페이지에도 들어가지만 실제 배포 명령은 PowerShell에서 실행",
            "MCP_ALLOW_UNAUTHENTICATED_HTTP",
            "MCP_TOOL_PROFILE",
            "Aliased",
            "브라우저로 읽는 홈페이지가 아니므로",
            "Settings > Apps > Advanced settings > Developer mode",
            "OpenAI 공식 ChatGPT Developer mode·MCP 지원 안내",
            "OpenAI Secure MCP Tunnel",
            "ChatGPT 웹 앱 설정",
            "**인증**",
            "**URL (MCP URL / Server URL)**",
            "**로컬 Command / Arguments / Working directory** | 넣지 않음",
            "run_mcp_client_config_smoke.py",
            "mcp_initialized",
            "end_to_end_verified",
            "Customize > Connectors",
            "run_mcp_stdio_server.ps1",
            '$VercelProject = Read-Host "새 Vercel 프로젝트 이름"',
            '$McpUrl = Read-Host "V-6에서 복사한 전체 /mcp 주소"',
        ):
            self.assertIn(expected, text)
        self.assertNotIn(
            "### 선택 사항: Codex CLI·IDE와 Claude Code에도 같은 원격 URL 사용",
            text,
        )
        self.assertGreaterEqual(
            text.count('$StageDir = Read-Host "V-2에서 만든 배포 전용 폴더 전체 경로"'),
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
            "docs/assets/readme-course-00d-builder-claude-selection.png",
            "docs/assets/readme-course-03-vercel-production.png",
            "docs/assets/readme-course-04-claude-remote-connector.png",
            "docs/assets/readme-course-04b-builder-claude-direct-config.png",
            "docs/assets/readme-course-05-mcp-verification.png",
            "docs/assets/readme-course-02-claude-settings-menu.png",
            "docs/assets/readme-course-02c-claude-developer-config-edit.png",
            "docs/assets/readme-course-02d-claude-config-file-explorer.png",
            "docs/assets/readme-course-02b-claude-local-mcp-server.png",
            "docs/assets/readme-course-07-claude-config-editor.png",
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

    def test_readme_embeds_qwen_demo_and_step_captures(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        image_filenames = (
            QWEN_README_DEMO_FILENAMES[0],
            *QWEN_README_CAPTURE_FILENAMES,
        )

        for filename in image_filenames:
            asset_path = f"docs/assets/{filename}"
            public_asset_url = f"{PUBLIC_README_ASSET_PREFIX}{filename}"
            with self.subTest(filename=filename):
                self.assertRegex(
                    text,
                    rf"!\[[^\]]+\]\({re.escape(public_asset_url)}"
                    rf"(?:\s+\"[^\"]*\")?\)",
                )
                self.assertTrue((REPO_ROOT / asset_path).is_file(), asset_path)

        mp4_path = f"docs/assets/{QWEN_README_DEMO_FILENAMES[1]}"
        public_mp4_url = (
            f"{PUBLIC_README_ASSET_PREFIX}{QWEN_README_DEMO_FILENAMES[1]}"
        )
        self.assertRegex(
            text,
            rf"(?:\[[^\]]+\]\({re.escape(public_mp4_url)}"
            rf"(?:\s+\"[^\"]*\")?\)"
            rf"|(?:src|href)=[\"']{re.escape(public_mp4_url)}[\"'])",
        )
        self.assertTrue((REPO_ROOT / mp4_path).is_file(), mp4_path)
        for public_safety_notice in (
            "합성 샘플",
            "실제 기관명",
            "사용자 로컬 경로",
        ):
            self.assertIn(public_safety_notice, text)

    def test_qwen_readme_png_captures_are_valid_and_public_safe(self) -> None:
        for filename in QWEN_README_CAPTURE_FILENAMES:
            path = REPO_ROOT / "docs" / "assets" / filename
            with self.subTest(filename=filename):
                self.assertTrue(path.is_file(), path)
                png = path.read_bytes()
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertGreaterEqual(len(png), 24)
                width, height = struct.unpack(">II", png[16:24])
                self.assertGreaterEqual(width, 1_400)
                self.assertGreaterEqual(height, 780)
                self.assertGreater(path.stat().st_size, 10_000)

                chunk_types: list[bytes] = []
                offset = 8
                while offset + 12 <= len(png):
                    chunk_length = struct.unpack(">I", png[offset : offset + 4])[0]
                    chunk_end = offset + 12 + chunk_length
                    self.assertLessEqual(chunk_end, len(png))
                    chunk_type = png[offset + 4 : offset + 8]
                    chunk_types.append(chunk_type)
                    offset = chunk_end
                    if chunk_type == b"IEND":
                        break
                self.assertIn(b"IHDR", chunk_types)
                self.assertIn(b"IEND", chunk_types)
                for private_chunk in (b"tEXt", b"zTXt", b"iTXt", b"eXIf"):
                    self.assertNotIn(private_chunk, chunk_types)
                self._assert_public_asset_has_no_local_path(path, png)

    def test_qwen_readme_demo_media_have_valid_public_safe_signatures(self) -> None:
        gif_path = (
            REPO_ROOT
            / "docs"
            / "assets"
            / QWEN_README_DEMO_FILENAMES[0]
        )
        self.assertTrue(gif_path.is_file(), gif_path)
        gif = gif_path.read_bytes()
        self.assertIn(gif[:6], (b"GIF87a", b"GIF89a"))
        self.assertGreaterEqual(len(gif), 13)
        self._assert_public_asset_has_no_local_path(gif_path, gif)

        mp4_path = (
            REPO_ROOT
            / "docs"
            / "assets"
            / QWEN_README_DEMO_FILENAMES[1]
        )
        self.assertTrue(mp4_path.is_file(), mp4_path)
        mp4 = mp4_path.read_bytes()
        self.assertGreaterEqual(len(mp4), 12)
        self.assertEqual(mp4[4:8], b"ftyp")
        first_atom_size = struct.unpack(">I", mp4[:4])[0]
        self.assertGreaterEqual(first_atom_size, 8)
        self.assertLessEqual(first_atom_size, len(mp4))
        self._assert_public_asset_has_no_local_path(mp4_path, mp4)

    def _assert_public_asset_has_no_local_path(
        self,
        path: Path,
        payload: bytes,
    ) -> None:
        lowered_payload = payload.lower()
        for marker in PUBLIC_ASSET_FORBIDDEN_PATH_MARKERS:
            with self.subTest(path=path.name, marker=marker):
                self.assertNotIn(marker.encode("utf-8"), lowered_payload)
                self.assertNotIn(marker.encode("utf-16-le"), lowered_payload)

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
            "## 2. 다섯 방법 중 하나 선택하기",
            "### 방법 A — Claude Code 로컬 STDIO",
            "### 방법 B — Codex CLI / Codex IDE 로컬 STDIO",
            "### 방법 C — Claude Desktop 로컬 STDIO",
            "### 방법 D — ChatGPT · Vercel HTTPS MCP",
            "### 방법 E — Claude · Vercel HTTPS MCP",
            "생성 버튼을 누르기 전 — 연결 앱·저장 폴더·서버 이름",
            "Builder에서 JSON 복사하기",
            "Vercel은 [V-6](#v-6-production-배포)의 `vercel --prod`",
            "왼쪽 아래 **프로필 영역**",
            "**구성 편집**",
            "**`claude_desktop_config`** 파일",
            "**붙여 넣을 위치는 파일 전체입니다.**",
            "서버 이름 옆 파란 배지가 **`running`**",
            "%APPDATA%\\Claude\\claude_desktop_config.json",
            "Settings > Apps > Advanced settings > Developer mode",
            "Apps 설정에서 새 앱",
            "ChatGPT 웹 앱 설정",
            "**URL (MCP URL / Server URL)**",
            "OpenAI Secure MCP Tunnel",
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

    def test_readme_maps_chatgpt_web_remote_fields(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        for expected in (
            "ChatGPT 웹 앱 설정",
            "**이름 (Name)**",
            "**URL (MCP URL / Server URL)**",
            "**인증**",
            "**로컬 Command / Arguments / Working directory** | 넣지 않음",
            "Apps 설정에서 새 앱을 만들고",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("ChatGPT의 **실행 명령** 칸", text)


if __name__ == "__main__":
    unittest.main()
