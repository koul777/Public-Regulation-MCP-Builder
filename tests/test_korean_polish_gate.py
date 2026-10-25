from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from scripts.korean_polish_gate import OPT_OUT_MARKER, check_files, check_text, main


class KoreanPolishGateTests(unittest.TestCase):
    def test_flags_symbols_and_phrases_the_team_always_rewrites(self) -> None:
        text = "\n".join(
            [
                "연결 순서 — 설치, 등록, 확인",
                "설치 → 등록 → 확인 순서로 진행합니다",
                "다음과 같습니다",
                "획기적인 MCP 연결",
                "데이터를 꽂으면 됩니다",
                "처리가 되어지면 끝",
                "결론적으로 연결됩니다",
                "잠시 기다립니다…",
            ]
        )
        rules = [item["rule"] for item in check_text(text, source="README.ko.md")]
        self.assertEqual(
            [
                "dash",
                "arrow",
                "chatbot-phrase",
                "hype-word",
                "metaphor-verb",
                "double-passive",
                "slogan-ending",
                "ellipsis",
            ],
            rules,
        )

    def test_code_english_and_clean_korean_lines_pass(self) -> None:
        text = "\n".join(
            [
                "```powershell",
                "Write-Host '설치 — 완료' # 코드 블록은 검사하지 않음",
                "```",
                "명령은 `a → b`처럼 입력합니다.",
                "Search -> fetch — English lines are not checked.",
                "설치한 뒤 연결 상태를 확인합니다.",
            ]
        )
        self.assertEqual([], check_text(text, source="guide.md"))

    def test_findings_do_not_copy_the_sentence(self) -> None:
        findings = check_text("기관 고유 문장 — 다음 단계", source="README.ko.md")
        self.assertEqual(1, len(findings))
        self.assertEqual({"source", "line", "rule", "label", "matches"}, set(findings[0]))
        self.assertEqual(["—"], findings[0]["matches"])

    def test_opt_out_marker_is_ignored_for_generated_guides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.ko.md"
            path.write_text(f"<!-- {OPT_OUT_MARKER} -->\n설치 — 완료\n", encoding="utf-8")
            self.assertTrue(check_files([path])["passed"])
            strict = check_files([path], honor_opt_out=False)
            self.assertFalse(strict["passed"])
            self.assertEqual(1, strict["issue_count"])

    def test_unreadable_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = check_files([Path(tmp) / "missing.md"])
            self.assertFalse(report["passed"])
            self.assertEqual("FileNotFoundError", report["skipped_sources"][0]["reason"])

    def test_cli_exit_code_follows_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "clean.md"
            clean.write_text("설치한 뒤 연결을 확인합니다.\n", encoding="utf-8")
            dirty = Path(tmp) / "dirty.txt"
            dirty.write_text("설치 → 확인\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, main([str(clean)]))
                self.assertEqual(2, main([str(dirty)]))
                self.assertEqual(2, main([tmp]))


if __name__ == "__main__":
    unittest.main()
