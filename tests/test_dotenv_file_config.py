""".env 로딩과 설정 모듈 연결을 고정한다.

문서는 오래전부터 영구 설정을 .env에 두라고 안내했지만 읽는 코드가 없었다. 실제 환경변수가
항상 우선하고, 파일이 없어도 조용히 넘어가며, 쓰기는 다른 줄을 보존해야 한다.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import dotenv_file


class DotenvFileTests(unittest.TestCase):
    def test_parse_ignores_comments_quotes_and_export_prefix(self) -> None:
        parsed = dotenv_file.parse_dotenv(
            "# comment\n"
            "ENABLE_AGENT_REVIEW=true\n"
            "export LLM_PROVIDER='anthropic'\n"
            'AGENT_REVIEW_MODEL="claude-review" # trailing\n'
            "OPENAI_API_KEY=sk-with#hash\n"
            "BROKEN LINE\n"
        )
        self.assertEqual(
            {
                "ENABLE_AGENT_REVIEW": "true",
                "LLM_PROVIDER": "anthropic",
                "AGENT_REVIEW_MODEL": "claude-review",
                "OPENAI_API_KEY": "sk-with#hash",
            },
            parsed,
        )

    def test_load_applies_missing_keys_but_never_overrides_real_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("REG_RAG_TEST_A=from-file\nREG_RAG_TEST_B=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"REG_RAG_TEST_A": "from-shell"}, clear=False):
                os.environ.pop("REG_RAG_TEST_B", None)
                applied = dotenv_file.load_dotenv_file(env_path)
                self.assertEqual(["REG_RAG_TEST_B"], applied)
                self.assertEqual("from-shell", os.environ["REG_RAG_TEST_A"])
                self.assertEqual("from-file", os.environ["REG_RAG_TEST_B"])
                os.environ.pop("REG_RAG_TEST_B", None)

    def test_load_missing_file_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], dotenv_file.load_dotenv_file(Path(tmp) / "absent.env"))

    def test_write_updates_in_place_and_keeps_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "# keep me\nAPP_ENV=local\nENABLE_AGENT_REVIEW=false\nOPENAI_API_KEY=\n",
                encoding="utf-8",
            )
            dotenv_file.write_dotenv_values(
                {"ENABLE_AGENT_REVIEW": "true", "OPENAI_API_KEY": "sk test", "AGENT_REVIEW_MODEL": "gpt-4.1-mini"},
                env_path,
            )
            text = env_path.read_text(encoding="utf-8")
            self.assertEqual(
                ["# keep me", "APP_ENV=local", "ENABLE_AGENT_REVIEW=true", 'OPENAI_API_KEY="sk test"', "AGENT_REVIEW_MODEL=gpt-4.1-mini"],
                text.splitlines(),
            )
            self.assertEqual("sk test", dotenv_file.parse_dotenv(text)["OPENAI_API_KEY"])

    def test_dotenv_path_honours_override_variable(self) -> None:
        with patch.dict(os.environ, {dotenv_file.DOTENV_PATH_ENV: "C:/elsewhere/custom.env"}):
            self.assertEqual(Path("C:/elsewhere/custom.env"), dotenv_file.dotenv_path())
        self.assertEqual(dotenv_file.project_root() / ".env", dotenv_file.dotenv_path())

    def test_config_module_loads_dotenv_at_import(self) -> None:
        source = (dotenv_file.project_root() / "app" / "core" / "config.py").read_text(encoding="utf-8")
        self.assertIn("load_dotenv_file()", source)
        self.assertLess(source.index("load_dotenv_file()"), source.index("class Settings"))


if __name__ == "__main__":
    unittest.main()
