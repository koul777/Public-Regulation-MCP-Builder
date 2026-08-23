from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class BeginnerQuickstartDocsTests(unittest.TestCase):
    def test_readme_keeps_today_and_prior_update_summaries_above_product(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        today_heading = "# 최근 업데이트: 2026년 8월 23일"
        # 새 절을 맨 위에 얹을 때마다 이전 절은 순서대로 아래로 내려가야 한다. 위에서
        # 아래로 최신순이 아니면 무엇이 지금 버전인지 읽는 사람이 알 수 없다.
        prior_headings = (
            "# 이전 업데이트: 2026년 8월 3일",
            "# 이전 업데이트: 2026년 7월 29일~8월 1일",
        )
        prior_heading = prior_headings[-1]
        product_heading = "# PR MCP Builder"

        self.assertTrue(readme.startswith(today_heading))
        previous_index = readme.index(today_heading)
        for heading in prior_headings:
            self.assertLess(previous_index, readme.index(heading))
            previous_index = readme.index(heading)
        self.assertLess(previous_index, readme.index(product_heading))
        # 이번 절이 실제로 무엇을 고쳤는지 남긴다. 날짜만 바꾸고 내용을 비워 두면
        # 업데이트 내역이 있으나 마나 해진다.
        for phrase in (
            "Request timeout",
            "전처리 진행 막대가 뒤로 되돌아가던 문제",
            "깨진 HWP 글자 복구",
            "AI 검수 의견이 화면에 보이지 않던 문제",
            "기관을 지우면 정말 지워지도록",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme[: readme.index(prior_headings[0])])
        for phrase in (
            "초보자 안내 모드 추가",
            "개별 규정 파일과 합본 규정집의 결과 통일",
            "계층 색인을 자동 생성",
            "list_regulations",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme[: readme.index(prior_heading)])
        self.assertLess(readme.index("## 7월 29일"), readme.index(product_heading))
        self.assertLess(readme.index("## 7월 31일"), readme.index(product_heading))
        self.assertLess(readme.index("## 8월 1일"), readme.index(product_heading))
        self.assertIn("비전공자도 이해하기 쉽도록", readme[: readme.index(product_heading)])

    def test_readme_puts_beginner_flow_before_connection_details(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        beginner_start = readme.index("## 처음 사용자를 위한 5분 빠른 시작")
        connection_details = readme.index("## 2. 다섯 방법 중 하나 선택하기")
        self.assertLess(readme.index("# PR MCP Builder"), beginner_start)
        self.assertLess(beginner_start, readme.index("### 무엇을 할 수 있나요?"))
        self.assertLess(beginner_start, connection_details)

        positions = [
            readme.index(label, beginner_start, connection_details)
            for label in (
                "① 문서 올려서 전처리",
                "② 결과 확인",
                "③ 검수하고 승인",
                "④ Qwen 규정 챗봇·AI 연결",
            )
        ]
        self.assertEqual(sorted(positions), positions)

        for phrase in (
            "문서 업로드",
            "전처리 시작",
            "AI 검수",
            "초보자 안내 시작",
            "일반 모드로 계속",
            "초보자 안내 모드",
            "빨간 테두리",
            "화살표",
            "안내 건너뛰기",
            "처음부터 다시 보기",
            "승인과 색인은 자동으로",
            "공식 MCP 품질 준비 확인",
            "Node.js/npm",
            "동의해 버튼을 누른 경우에만",
            "Kordoc 사용 가능",
            "안전 재전처리",
            "화면 진입만으로 시작되지 않으며",
            "정리된 내용(청크)",
            "이슈",
            "생성할 MCP 이름 (필수 입력)",
            "MCP로 쓸 파일 묶음 만들기",
            "승인하고 색인",
            "선택한 조항 반려",
            "앱별 등록·연결 진단",
            "AI 앱 등록",
            "앱 재시작 또는 새 대화",
            "연결 진단",
            "search",
            "fetch",
            "list_regulations",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme[beginner_start:connection_details])

    def test_operator_quickstart_matches_safe_beginner_guide(self) -> None:
        quickstart = (REPO_ROOT / "docs" / "operator_quickstart_ko.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(quickstart.split())

        positions = [
            quickstart.index(label)
            for label in (
                "① 문서 올려서 전처리",
                "② 결과 확인",
                "③ 검수하고 승인",
                "④ Qwen 규정 챗봇·AI 연결",
            )
        ]
        self.assertEqual(sorted(positions), positions)
        for phrase in (
            "문서 업로드",
            "전처리 시작",
            "AI 검수",
            "품질 통과 표시는 자동 승인이 아니며",
            "승인·색인 완료",
            "초보자 안내 모드",
            "일반 모드로 계속",
            "오류가 아니라 현재 안내 대상 표시",
            "승인이나 색인을 자동 실행하지 않는다",
            "MCP 이름 입력이나 파일 생성만으로 선택하지 않는다",
            "Kordoc 설치·검증 시작",
            "사용자 전역 설치",
            "Kordoc 사용 가능",
            "안전 재전처리",
            "들어가기만 해서는 시작되지 않고",
            "정리된 내용(청크)",
            "이슈",
            "생성할 MCP 이름 (필수 입력)",
            "MCP로 쓸 파일 묶음 만들기",
            "승인하고 색인",
            "선택한 조항 반려",
            "앱별 등록·연결 진단",
            "list_regulations",
            ".\\START_HERE.bat",
            "별도의 PowerShell 창",
            "로컬 Streamlit 운영 화면과 함께 설정하지 않는다",
            "README의 방법 A~E",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)

    def test_beginner_labels_match_streamlit_controls(self) -> None:
        ui_source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        documented = "\n".join(
            (
                (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
                (REPO_ROOT / "docs" / "operator_quickstart_ko.md").read_text(encoding="utf-8"),
            )
        )

        for label in (
            "문서 업로드",
            "전처리 시작",
            "AI 검수",
            "승인하고 색인",
            "선택한 조항 반려",
            "생성할 MCP 이름 (필수 입력)",
            "MCP로 쓸 파일 묶음 만들기",
        ):
            with self.subTest(label=label):
                self.assertIn(label, ui_source)
                self.assertIn(label, documented)

    def test_combined_book_guidance_promises_logical_parity_and_fails_closed(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = (REPO_ROOT / "docs" / "operator_quickstart_ko.md").read_text(
            encoding="utf-8"
        )
        combined = " ".join((readme + "\n" + quickstart).split())

        for phrase in (
            "규정별 파일 여러 개",
            "여러 규정을 합친 통합 규정집 한 개",
            "같은 규정·목차·조문",
            "계층 색인은",
            "자동 생성",
            "별표·붙임",
            "경계가 불명확하면",
            "생성이 안전하게 멈춥니다",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)

    def test_readme_separates_portable_and_source_connection_diagnostics(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.split())

        for phrase in (
            "Windows 실행판은 포함된 EXE",
            "소스 실행 전용",
            "PR MCP Builder.exe --mcp-server",
            "ConvertFrom-Json",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)
        self.assertNotIn('python -m json.tool "$env:APPDATA\\Claude', normalized)

    def test_readme_routes_chatgpt_to_supported_remote_path(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        rendered_source = re.sub(r"<!--.*?-->", "", readme, flags=re.DOTALL)

        self.assertIn("방법 B — Codex CLI / Codex IDE 로컬 STDIO", rendered_source)
        self.assertIn("ChatGPT 웹의 Developer mode", rendered_source)
        self.assertIn("ChatGPT는 로컬 MCP 서버에 직접 연결하지 않고", rendered_source)
        self.assertIn("OpenAI Secure MCP Tunnel", rendered_source)
        self.assertNotIn("ChatGPT Desktop 로컬 STDIO", rendered_source)
        self.assertNotIn("ChatGPT Desktop / Codex CLI / Codex IDE", rendered_source)

    def test_connection_docs_do_not_advertise_chatgpt_local_stdio(self) -> None:
        quickconnect = (REPO_ROOT / "docs" / "mcp_quickconnect_ko.md").read_text(
            encoding="utf-8"
        )
        examples = (
            REPO_ROOT / "docs" / "mcp_client_config_examples_ko.md"
        ).read_text(encoding="utf-8")
        combined = quickconnect + "\n" + examples

        self.assertIn("ChatGPT는 로컬 MCP 서버에 직접 연결하지 않습니다", combined)
        self.assertIn("ChatGPT 웹", combined)
        self.assertIn("Secure MCP Tunnel", combined)
        self.assertNotIn("ChatGPT Desktop / Codex CLI / Codex IDE", combined)
        self.assertNotIn("ChatGPT Desktop·Codex 공용 설정", combined)
        self.assertNotIn("Settings > MCP servers > Add server", combined)


if __name__ == "__main__":
    unittest.main()
