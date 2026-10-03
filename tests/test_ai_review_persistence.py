"""AI 검수가 '켰는데 조용히 꺼지는' 경로를 막는 회귀 테스트.

세 가지를 고정한다.
1. ``.env``는 실제로 읽힌다(문서가 오래전부터 그렇게 안내했지만 읽는 코드가 없었다).
2. 사이드바 저장은 비밀 아닌 설정을 디스크에 남기고, 키는 절대 남기지 않는다.
3. 로컬 경로가 든 조항 하나가 문서 전체의 AI 검수를 막지 않는다.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.agents.review_executor import AgentReviewExecutor
from app.core import dotenv_file
from app.core.ai_review_preferences import (
    PREFERENCES_FILENAME,
    clear_ai_review_preferences,
    load_ai_review_preferences,
    save_ai_review_preferences,
)
from app.core.config import Settings
from tests.test_agent_review_executor import openai_response, planned_review_for, review_chunks_for


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


class AIReviewPreferencesTests(unittest.TestCase):
    def test_save_keeps_non_secret_fields_and_drops_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = save_ai_review_preferences(
                tmp,
                {
                    "enable_agent_review": True,
                    "llm_provider": "openai-compatible",
                    "agent_review_model": "synthetic-review",
                    "agent_review_api_base_url": "http://127.0.0.1:11434/v1",
                    "agent_review_max_chunks_per_document": "25",
                    "agent_review_max_input_tokens_per_document": 0,
                    "openai_api_key": "sk-secret",
                    "openai_compatible_api_key": "local-secret",
                    "anthropic_api_key": "ant-secret",
                    "data_dir": Path(tmp),
                },
            )
            self.assertEqual(PREFERENCES_FILENAME, path.name)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret", raw)
            self.assertNotIn("data_dir", raw)
            loaded = load_ai_review_preferences(tmp)
            self.assertEqual(
                {
                    "enable_agent_review": True,
                    "llm_provider": "openai-compatible",
                    "agent_review_model": "synthetic-review",
                    "agent_review_api_base_url": "http://127.0.0.1:11434/v1",
                    "agent_review_max_chunks_per_document": 25,
                    "agent_review_max_input_tokens_per_document": 0,
                },
                loaded,
            )

    def test_missing_or_corrupt_file_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({}, load_ai_review_preferences(tmp))
            (Path(tmp) / PREFERENCES_FILENAME).write_text("{not json", encoding="utf-8")
            self.assertEqual({}, load_ai_review_preferences(tmp))
            (Path(tmp) / PREFERENCES_FILENAME).write_text(json.dumps({"preferences": []}), encoding="utf-8")
            self.assertEqual({}, load_ai_review_preferences(tmp))

    def test_clear_removes_the_file_and_tolerates_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_ai_review_preferences(tmp, {"enable_agent_review": True})
            clear_ai_review_preferences(tmp)
            self.assertFalse((Path(tmp) / PREFERENCES_FILENAME).exists())
            clear_ai_review_preferences(tmp)

    def test_restored_switch_without_key_is_reported_as_setup_needed_not_off(self) -> None:
        """복원된 스위치는 켜져 있되, 키가 없으면 '설정 필요'로 드러나야 한다."""
        from app.agents.provider_config import agent_review_configuration_reason

        restored = Settings(
            enable_agent_review=True,
            llm_provider="openai",
            agent_review_model="gpt-4.1-mini",
            openai_api_key="",
        )
        self.assertEqual("openai_api_key_missing", agent_review_configuration_reason(restored))


class LeakBlockedChunkIsolationTests(unittest.TestCase):
    def _executor(self, http_post, **overrides: Any) -> AgentReviewExecutor:
        settings = Settings(
            data_dir=Path(self.tmp),
            enable_agent_review=True,
            llm_provider="openai",
            openai_api_key="secret-key",
            agent_review_model="review-model",
            **overrides,
        )
        return AgentReviewExecutor(settings, http_post=http_post)

    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def test_one_leaky_chunk_is_skipped_and_the_rest_are_still_reviewed(self) -> None:
        chunk_ids = ["chunk_0", "chunk_1", "chunk_2"]
        chunks = review_chunks_for(chunk_ids)
        leaky = chunks[1].model_copy(update={"text": r"첨부 D:\scan\raw.pdf 참조", "normalized_text": r"첨부 D:\scan\raw.pdf 참조"})
        chunks[1] = leaky
        sent_batches: list[list[str]] = []

        def fake_post(url, headers, payload, timeout):
            sent = json.loads(payload["messages"][1]["content"])
            self.assertNotIn("raw.pdf", payload["messages"][1]["content"])
            batch_ids = [item["chunk_id"] for item in sent["items"]]
            sent_batches.append(batch_ids)
            return openai_response(batch_ids)

        result = self._executor(fake_post, agent_review_chunks_per_request=2).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=chunks,
        )

        self.assertEqual("executed", result["status"])
        self.assertEqual("provider_partial_chunks_blocked_local_path", result["skip_reason"])
        self.assertEqual([["chunk_0"], ["chunk_2"]], sent_batches)
        self.assertEqual(["chunk_1"], result["leak_blocked_chunk_ids"])
        self.assertEqual(1, result["leak_blocked_chunk_count"])
        self.assertTrue(result["leak_blocked_locations"][0].startswith("provider_payload_local_path_leak:"))
        self.assertEqual(["chunk_1"], result["unreviewed_chunk_ids"])
        self.assertEqual(["chunk_0", "chunk_2"], result["reviewed_chunk_ids"])
        self.assertEqual(0, result["failed_batch_count"])
        self.assertEqual(2, result["api_call_count"])
        # 경로 자체는 기록에 남기지 않는다.
        self.assertNotIn("raw.pdf", json.dumps(result, ensure_ascii=False, default=str))

    def test_all_chunks_leaking_still_blocks_without_an_http_call(self) -> None:
        chunk_ids = ["chunk_0", "chunk_1"]
        chunks = [
            chunk.model_copy(update={"text": r"D:\secret\a.hwp", "normalized_text": r"D:\secret\a.hwp"})
            for chunk in review_chunks_for(chunk_ids)
        ]
        calls: list[Any] = []
        result = self._executor(lambda *args: calls.append(args) or {}).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=chunks,
        )
        self.assertEqual("provider_execution_blocked", result["status"])
        self.assertTrue(str(result["skip_reason"]).startswith("provider_payload_local_path_leak:"))
        self.assertEqual(chunk_ids, result["leak_blocked_chunk_ids"])
        self.assertEqual(0, result["api_call_count"])
        self.assertEqual([], calls)


class StatusTextTests(unittest.TestCase):
    def test_partial_leak_block_is_shown_as_partial_with_plain_guidance(self) -> None:
        from frontend import streamlit_app

        tag, message, complete = streamlit_app._ai_review_status_text(
            {
                "status": "executed",
                "request_enabled": True,
                "skip_reason": "provider_partial_chunks_blocked_local_path",
                "unreviewed_chunk_ids": ["chunk_1"],
                "leak_blocked_chunk_ids": ["chunk_1"],
                "leak_blocked_chunk_count": 1,
                "leak_blocked_locations": ["provider_payload_local_path_leak:payload.messages[1].content.items[0].text"],
            }
        )
        self.assertIn("일부 완료", tag)
        self.assertFalse(complete)
        self.assertIn("1개", message)
        self.assertIn("직접 확인", message)
        self.assertNotIn("provider_payload_local_path_leak", message)


if __name__ == "__main__":
    unittest.main()
