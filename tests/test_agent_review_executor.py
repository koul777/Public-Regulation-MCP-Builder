from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.agents.review_executor import AgentReviewExecutor
from app.core.config import Settings
from app.schemas.chunk import Chunk


def review_chunk() -> Chunk:
    return Chunk(
        chunk_id="chunk_review",
        document_id="doc_review",
        source_node_ids=["node_1"],
        chunk_type="table",
        text="broken table text",
        normalized_text="broken table text",
        retrieval_text="[source]\nbroken table text",
        metadata={"table_like": True},
        source_page_start=3,
        source_page_end=3,
    )


def planned_review() -> dict[str, Any]:
    return {
        "status": "planned",
        "provider": "openai",
        "model": "review-model",
        "selected_count": 1,
        "estimated_output_tokens": 100,
        "estimated_total_tokens": 200,
        "estimated_cost": "0",
        "selected_candidates": [
            {
                "chunk_id": "chunk_review",
                "reasons": ["table_like_without_cell_rows"],
                "content_hash": "sha256:" + ("a" * 64),
            }
        ],
    }


class AgentReviewExecutorTests(unittest.TestCase):
    def test_missing_api_key_keeps_configuration_needed_without_http_call(self) -> None:
        calls: list[dict[str, Any]] = []
        executor = AgentReviewExecutor(
            Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key="", agent_review_model="review-model"),
            http_post=lambda *args: calls.append({"args": args}) or {},
        )

        result = executor.execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review(),
            chunks=[review_chunk()],
        )

        self.assertEqual(result["status"], "api_configuration_needed")
        self.assertEqual(result["skip_reason"], "openai_api_key_missing")
        self.assertEqual(calls, [])

    def test_executes_openai_chat_completion_and_records_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[dict[str, Any]] = []

            def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
                calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
                return {
                    "id": "chatcmpl-test",
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"items":[{"chunk_id":"chunk_review","risk_level":"high",'
                                    '"issues":["table structure may be broken"],'
                                    '"recommended_human_check":"Compare the source table on page 3.",'
                                    '"confidence":0.82}]}'
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
                }

            settings = Settings(
                data_dir=Path(tmp),
                enable_agent_review=True,
                llm_provider="openai",
                openai_api_key="secret-key",
                agent_review_model="review-model",
                agent_review_timeout_seconds=7,
            )
            executor = AgentReviewExecutor(settings, http_post=fake_post)

            result = executor.execute(
                document_id="doc_review",
                run_id="run_review",
                plan=planned_review(),
                chunks=[review_chunk()],
            )

            self.assertEqual(result["status"], "executed")
            self.assertEqual(result["api_call_count"], 1)
            self.assertEqual(result["provider_request_id"], "chatcmpl-test")
            self.assertEqual(result["actual_total_tokens"], 60)
            self.assertEqual(result["provider_review_json"]["items"][0]["chunk_id"], "chunk_review")
            self.assertEqual(calls[0]["url"], "https://api.openai.com/v1/chat/completions")
            self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer secret-key")
            self.assertEqual(calls[0]["payload"]["model"], "review-model")
            self.assertEqual(calls[0]["payload"]["max_tokens"], 100)
            self.assertIn("messages", calls[0]["payload"])
            audit_path = Path(tmp) / "repository" / "provider_execution_audit.jsonl"
            self.assertTrue(audit_path.exists())
            audit_text = audit_path.read_text(encoding="utf-8")
            self.assertIn("chatcmpl-test", audit_text)
            self.assertIn("bounded_parser_review_chunks", audit_text)

    def test_executes_azure_openai_with_resource_endpoint_and_api_key_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[dict[str, Any]] = []

            def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
                calls.append({"url": url, "headers": headers, "payload": payload})
                return {
                    "id": "azure-review",
                    "choices": [{"message": {"content": '{"items":[]}'}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
                }

            executor = AgentReviewExecutor(
                Settings(
                    data_dir=Path(tmp),
                    enable_agent_review=True,
                    llm_provider="azure-openai",
                    azure_openai_endpoint="https://sample.openai.azure.com",
                    azure_openai_api_key="azure-secret",
                    agent_review_model="review-deployment",
                ),
                http_post=fake_post,
            )

            result = executor.execute(
                document_id="doc_review", run_id="run_review", plan=planned_review(), chunks=[review_chunk()]
            )

            self.assertEqual(result["status"], "executed")
            self.assertEqual(calls[0]["url"], "https://sample.openai.azure.com/openai/v1/chat/completions")
            self.assertEqual(calls[0]["headers"]["api-key"], "azure-secret")
            self.assertNotIn("Authorization", calls[0]["headers"])

    def test_executes_anthropic_messages_and_normalizes_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[dict[str, Any]] = []

            def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
                calls.append({"url": url, "headers": headers, "payload": payload})
                return {
                    "id": "msg_review",
                    "content": [{"type": "text", "text": '{"items":[]}'}],
                    "usage": {"input_tokens": 21, "output_tokens": 5},
                }

            executor = AgentReviewExecutor(
                Settings(
                    data_dir=Path(tmp),
                    enable_agent_review=True,
                    llm_provider="anthropic",
                    anthropic_api_key="anthropic-secret",
                    agent_review_model="claude-haiku-4-5",
                ),
                http_post=fake_post,
            )

            result = executor.execute(
                document_id="doc_review", run_id="run_review", plan=planned_review(), chunks=[review_chunk()]
            )

            self.assertEqual(result["status"], "executed")
            self.assertEqual(result["actual_total_tokens"], 26)
            self.assertEqual(calls[0]["url"], "https://api.anthropic.com/v1/messages")
            self.assertEqual(calls[0]["headers"]["x-api-key"], "anthropic-secret")
            self.assertEqual(calls[0]["headers"]["anthropic-version"], "2023-06-01")
            self.assertIn("system", calls[0]["payload"])
            self.assertEqual(len(calls[0]["payload"]["messages"]), 1)

    def test_openai_compatible_local_api_does_not_require_key(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
            calls.append({"url": url, "headers": headers})
            return {"id": "local-review", "choices": [{"message": {"content": '{"items":[]}'}}]}

        executor = AgentReviewExecutor(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                llm_provider="openai-compatible",
                openai_compatible_api_key="",
                agent_review_api_base_url="http://127.0.0.1:11434/v1",
                agent_review_model="local-model",
            ),
            http_post=fake_post,
        )

        result = executor.execute(
            document_id="doc_review", run_id="run_review", plan=planned_review(), chunks=[review_chunk()]
        )

        self.assertEqual(result["status"], "executed")
        self.assertEqual(calls[0]["url"], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("Authorization", calls[0]["headers"])

    def test_openai_compatible_requires_an_explicit_non_openai_base_url(self) -> None:
        calls: list[dict[str, Any]] = []
        executor = AgentReviewExecutor(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                llm_provider="openai-compatible",
                agent_review_api_base_url="https://api.openai.com",
                agent_review_model="local-model",
            ),
            http_post=lambda *args: calls.append({"args": args}) or {},
        )

        result = executor.execute(
            document_id="doc_review", run_id="run_review", plan=planned_review(), chunks=[review_chunk()]
        )

        self.assertEqual(result["status"], "api_configuration_needed")
        self.assertEqual(result["skip_reason"], "openai_compatible_base_url_missing")
        self.assertEqual(calls, [])

    def test_malformed_provider_json_is_not_marked_executed_and_records_failed_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
                return {
                    "id": "chatcmpl-bad-json",
                    "choices": [{"message": {"content": "not json at all"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                }

            settings = Settings(
                data_dir=Path(tmp),
                enable_agent_review=True,
                llm_provider="openai",
                openai_api_key="secret-key",
                agent_review_model="review-model",
            )
            executor = AgentReviewExecutor(settings, http_post=fake_post)

            result = executor.execute(
                document_id="doc_review",
                run_id="run_review",
                plan=planned_review(),
                chunks=[review_chunk()],
            )

            self.assertEqual(result["status"], "provider_execution_failed")
            self.assertEqual(result["skip_reason"], "provider_response_invalid_json")
            self.assertEqual(result["api_call_count"], 1)
            self.assertEqual(result["provider_review_json"], {})
            self.assertEqual(result["actual_total_tokens"], 14)
            audit_path = Path(tmp) / "repository" / "provider_execution_audit.jsonl"
            audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(audit_rows[0]["provider_request_id"], "chatcmpl-bad-json")
            self.assertEqual(audit_rows[0]["outcome"], "provider_execution_failed")

    def test_local_path_payload_leak_is_blocked_before_http_call(self) -> None:
        calls: list[dict[str, Any]] = []
        chunk = review_chunk().model_copy(update={"text": r"See C:\\secret\\raw.pdf", "normalized_text": r"See C:\\secret\\raw.pdf"})
        settings = Settings(
            data_dir=Path("data"),
            enable_agent_review=True,
            llm_provider="openai",
            openai_api_key="secret-key",
            agent_review_model="review-model",
        )
        executor = AgentReviewExecutor(
            settings,
            http_post=lambda *args: calls.append({"args": args}) or {},
        )

        result = executor.execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review(),
            chunks=[chunk],
        )

        self.assertEqual(result["status"], "provider_execution_blocked")
        self.assertTrue(str(result["skip_reason"]).startswith("provider_payload_local_path_leak:"))
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(calls, [])

    def test_payload_includes_bounded_table_review_context(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
            calls.append({"payload": payload})
            return {
                "id": "chatcmpl-context",
                "choices": [{"message": {"content": '{"items":[]}'}}],
                "usage": {"total_tokens": 1},
            }

        chunk = review_chunk().model_copy(
            update={
                "metadata": {
                    "table_like": True,
                    "table_classification": "probable_table_extraction_failed",
                    "table_rows": ["A | B", "1 | 2"],
                    "kordoc_table_inventory": {
                        "status": "parsed",
                        "table_count": 9,
                        "stored_table_count": 3,
                        "tables_truncated": True,
                        "tables": [
                            {
                                "table_index": 1,
                                "row_count": 2,
                                "column_count": 2,
                                "cell_count": 4,
                                "cell_rows": [{"row_index": 0, "cells": ["A", "B"], "raw": "A | B"}],
                            }
                        ],
                    },
                    "kordoc_table_match": {
                        "match_label": "medium_review_match",
                        "match_score": 34,
                        "table_index": 1,
                    },
                    "source_path": r"C:\\secret\\raw.pdf",
                }
            }
        )
        settings = Settings(
            data_dir=Path("data"),
            enable_agent_review=True,
            llm_provider="openai",
            openai_api_key="secret-key",
            agent_review_model="review-model",
        )
        executor = AgentReviewExecutor(settings, http_post=fake_post)

        executor.execute(document_id="doc_review", run_id="run_review", plan=planned_review(), chunks=[chunk])

        user_content = calls[0]["payload"]["messages"][1]["content"]
        payload = json.loads(user_content)
        context = payload["items"][0]["review_context"]
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertEqual(context["table_classification"], "probable_table_extraction_failed")
        self.assertEqual(context["kordoc_table_match"]["match_label"], "medium_review_match")
        self.assertEqual(context["kordoc_table_inventory"]["table_count"], 9)
        self.assertEqual(context["kordoc_table_inventory"]["table_samples"][0]["column_count"], 2)
        self.assertNotIn("source_path", context)
        self.assertNotIn("secret", encoded)


if __name__ == "__main__":
    unittest.main()
