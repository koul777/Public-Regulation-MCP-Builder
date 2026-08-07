from __future__ import annotations

import json
import tempfile
import threading
import time
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


def planned_review_for(chunk_ids: list[str], *, output_tokens_per_chunk: int = 100) -> dict[str, Any]:
    return {
        "status": "planned",
        "provider": "openai",
        "model": "review-model",
        "selected_count": len(chunk_ids),
        "estimated_output_tokens": output_tokens_per_chunk * len(chunk_ids),
        "estimated_total_tokens": 2 * output_tokens_per_chunk * len(chunk_ids),
        "estimated_cost": "0",
        "selected_candidates": [
            {
                "chunk_id": chunk_id,
                "reasons": ["table_like_without_cell_rows"],
                "content_hash": "sha256:" + ("a" * 64),
            }
            for chunk_id in chunk_ids
        ],
    }


def review_chunks_for(chunk_ids: list[str]) -> list[Chunk]:
    return [review_chunk().model_copy(update={"chunk_id": chunk_id}) for chunk_id in chunk_ids]


def openai_response(chunk_ids: list[str], *, response_id: str = "chatcmpl-batch") -> dict[str, Any]:
    items = [
        {
            "chunk_id": chunk_id,
            "risk_level": "medium",
            "issues": [f"{chunk_id} 조문 경계 확인 필요"],
            "recommended_human_check": "원문과 대조",
        }
        for chunk_id in chunk_ids
    ]
    return {
        "id": response_id,
        "choices": [{"message": {"content": json.dumps({"items": items})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class AgentReviewNeverRewritesTextTests(unittest.TestCase):
    """AI는 규정 본문을 다시 쓰지 않는다.

    본문 재작성을 시켰을 때 되돌아온 교정본의 77%가 원문과 완전히 같았고, 실제로
    바뀐 것들은 ``2012. 6. 14.``를 ``2012. 06. 14.``로 만드는 식이었다. 저장된 본문은
    MCP가 인용할 근거라 사람이 고쳐야 한다.
    """

    def test_prompt_forbids_rewriting_the_regulation_text(self) -> None:
        from app.agents.review_executor import SYSTEM_PROMPT

        self.assertIn("Never rewrite", SYSTEM_PROMPT)
        self.assertNotIn("corrected_text", SYSTEM_PROMPT)

    def test_prompt_asks_for_korean_findings_and_bans_filler(self) -> None:
        """지적의 95%가 영어로 왔다. 한국인 운영자가 승인 화면에서 그대로 읽는 글이다."""
        from app.agents.review_executor import SYSTEM_PROMPT

        self.assertIn("in Korean", SYSTEM_PROMPT)
        self.assertIn('"issues": []', SYSTEM_PROMPT)
        self.assertIn("no parsing risks identified", SYSTEM_PROMPT)
        self.assertIn("spacing should be verified", SYSTEM_PROMPT)

    def test_requested_json_shape_has_no_rewrite_field(self) -> None:
        sent: list[dict[str, Any]] = []

        def fake_post(url, headers, payload, timeout):
            sent.append(json.loads(payload["messages"][1]["content"]))
            return openai_response(["chunk_review"])

        AgentReviewExecutor(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                llm_provider="openai",
                openai_api_key="secret-key",
                agent_review_model="review-model",
            ),
            http_post=fake_post,
        ).execute(
            document_id="doc_review", run_id="run_review", plan=planned_review(), chunks=[review_chunk()]
        )

        shape = sent[0]["required_json_shape"]["items"][0]
        self.assertNotIn("corrected_text", shape)
        self.assertIn("issues", shape)
        self.assertIn("recommended_human_check", shape)


class AgentReviewFindingsTests(unittest.TestCase):
    """AI 지적은 본문이 아니라 메타데이터에 붙는다."""

    def _chunk(self):
        return review_chunk().model_copy(update={"chunk_id": "chunk_review", "metadata": {"a": 1}})

    def test_findings_are_stored_without_touching_the_text(self) -> None:
        from app.services.processing_service import _apply_ai_review_findings

        chunk = self._chunk()
        original = chunk.text
        _apply_ai_review_findings(
            [chunk],
            {
                "provider_review_json": {
                    "items": [
                        {
                            "chunk_id": "chunk_review",
                            "risk_level": "high",
                            "issues": ["조문 경계가 합쳐졌을 수 있음"],
                            "recommended_human_check": "제3조 시작 지점을 원문과 대조",
                            # 제공자가 지시를 어기고 본문을 보내도 저장하지 않는다.
                            "corrected_text": "AI가 다시 쓴 본문",
                        }
                    ]
                }
            },
        )

        self.assertEqual(original, chunk.text)
        self.assertIsNone(chunk.ai_preprocessed_text)
        findings = chunk.metadata["agent_review_findings"]
        self.assertEqual("high", findings["risk_level"])
        self.assertEqual(["조문 경계가 합쳐졌을 수 있음"], findings["issues"])
        self.assertEqual("제3조 시작 지점을 원문과 대조", findings["recommended_human_check"])
        self.assertEqual(1, chunk.metadata["a"])

    def test_filler_lines_are_not_stored_as_findings(self) -> None:
        """빈 말이 지적 자리에 쌓이면 진짜 지적까지 안 읽게 된다.

        실제 실행 기록 600건에서 "No parsing risks identified." 18건,
        "Spacing and line break consistency" 15건이 같은 문구로 반복됐다.
        """
        from app.services.processing_service import _apply_ai_review_findings

        chunk = self._chunk()
        _apply_ai_review_findings(
            [chunk],
            {
                "provider_review_json": {
                    "items": [
                        {
                            "chunk_id": "chunk_review",
                            "risk_level": "low",
                            "issues": [
                                "No parsing risks identified.",
                                "Spacing and line break consistency",
                                "지적 사항 없음",
                                "제3조 본문이 제2조 끝에 붙어 있음",
                            ],
                            "recommended_human_check": "해당 없음",
                        }
                    ]
                }
            },
        )

        findings = chunk.metadata["agent_review_findings"]
        self.assertEqual(["제3조 본문이 제2조 끝에 붙어 있음"], findings["issues"])
        self.assertEqual("", findings["recommended_human_check"])

    def test_a_real_finding_that_ends_in_the_word_none_is_kept(self) -> None:
        """'본문이 없음'은 본문 누락이라는 가장 무거운 지적이다. '문제 없음'과 갈라야 한다."""
        from app.services.processing_service import _agent_review_finding_texts

        self.assertEqual(
            ["제3조 본문이 없음", "제5조 제2항 이후 내용 없음"],
            _agent_review_finding_texts(
                ["문제 없음", "제3조 본문이 없음", "특이사항 없음", "제5조 제2항 이후 내용 없음"]
            ),
        )

    def test_a_chunk_whose_findings_are_all_filler_is_left_alone(self) -> None:
        from app.services.processing_service import _apply_ai_review_findings

        chunk = self._chunk()
        _apply_ai_review_findings(
            [chunk],
            {
                "provider_review_json": {
                    "items": [
                        {
                            "chunk_id": "chunk_review",
                            "issues": ["No parsing risks identified.", "None"],
                            "recommended_human_check": "",
                        }
                    ]
                }
            },
        )

        self.assertNotIn("agent_review_findings", chunk.metadata)

    def test_an_item_with_no_finding_leaves_the_chunk_alone(self) -> None:
        from app.services.processing_service import _apply_ai_review_findings

        chunk = self._chunk()
        _apply_ai_review_findings(
            [chunk],
            {"provider_review_json": {"items": [{"chunk_id": "chunk_review", "issues": [], "risk_level": "low"}]}},
        )

        self.assertNotIn("agent_review_findings", chunk.metadata)


class AgentReviewBatchingTests(unittest.TestCase):
    """AI 검수는 '시간 안에 되면'이 아니라 반드시 끝나야 한다.

    한 번에 다 보내면 늦어져서 통째로 버려졌다. 작게 나눠 부르고, 실패한 묶음만
    다시 부르고, 그래도 안 되면 나머지 결과는 살린다.
    """

    def _executor(self, http_post, **overrides: Any) -> AgentReviewExecutor:
        settings = Settings(
            data_dir=Path("data"),
            enable_agent_review=True,
            llm_provider="openai",
            openai_api_key="secret-key",
            agent_review_model="review-model",
            **overrides,
        )
        return AgentReviewExecutor(settings, http_post=http_post)

    def test_selected_chunks_are_split_into_small_requests(self) -> None:
        chunk_ids = [f"chunk_{index}" for index in range(9)]
        calls: list[dict[str, Any]] = []

        def fake_post(url, headers, payload, timeout):
            sent = json.loads(payload["messages"][1]["content"])
            calls.append(payload)
            return openai_response([item["chunk_id"] for item in sent["items"]])

        result = self._executor(fake_post, agent_review_chunks_per_request=4).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
        )

        self.assertEqual("executed", result["status"])
        # 9개를 4개씩 → 4 + 4 + 1
        self.assertEqual(3, len(calls))
        self.assertEqual(3, result["batch_count"])
        self.assertEqual(9, result["reviewed_chunk_count"])
        self.assertEqual(9, len(result["provider_review_json"]["items"]))
        # 묶음별 출력 상한을 합치면 계획서가 예약한 총량과 같아야 예산이 어긋나지 않는다.
        self.assertEqual(900, sum(int(payload["max_tokens"]) for payload in calls))

    def test_a_slow_batch_is_retried_instead_of_being_thrown_away(self) -> None:
        chunk_ids = ["chunk_0", "chunk_1"]
        attempts: list[int] = []

        def fake_post(url, headers, payload, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("AI provider request failed: timed out")
            sent = json.loads(payload["messages"][1]["content"])
            return openai_response([item["chunk_id"] for item in sent["items"]])

        result = self._executor(fake_post, agent_review_chunks_per_request=4).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
        )

        self.assertEqual("executed", result["status"])
        self.assertEqual(2, len(attempts))
        self.assertEqual(0, result["failed_batch_count"])
        self.assertEqual(2, result["reviewed_chunk_count"])

    def test_one_dead_batch_does_not_discard_the_reviews_that_came_back(self) -> None:
        chunk_ids = [f"chunk_{index}" for index in range(4)]

        def fake_post(url, headers, payload, timeout):
            sent = json.loads(payload["messages"][1]["content"])
            batch_ids = [item["chunk_id"] for item in sent["items"]]
            if "chunk_2" in batch_ids:
                raise RuntimeError("AI provider request failed: timed out")
            return openai_response(batch_ids)

        result = self._executor(
            fake_post, agent_review_chunks_per_request=2, agent_review_max_attempts=1
        ).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
        )

        # 살아남은 절반은 그대로 쓸 수 있어야 한다.
        self.assertEqual("executed", result["status"])
        self.assertEqual("provider_partial_batches_failed", result["skip_reason"])
        self.assertEqual(1, result["failed_batch_count"])
        self.assertEqual(2, result["reviewed_chunk_count"])
        # 어떤 조항이 검수되지 않았는지 이름으로 남아야 사람이 그 조항만 다시 볼 수 있다.
        self.assertEqual(["chunk_2", "chunk_3"], result["unreviewed_chunk_ids"])

    def test_every_batch_failing_is_reported_as_a_failure_not_a_clean_review(self) -> None:
        chunk_ids = ["chunk_0", "chunk_1"]

        def fake_post(url, headers, payload, timeout):
            raise RuntimeError("AI provider request failed: timed out")

        result = self._executor(
            fake_post, agent_review_chunks_per_request=1, agent_review_max_attempts=1
        ).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
        )

        self.assertEqual("provider_execution_failed", result["status"])
        self.assertEqual(0, result["reviewed_chunk_count"])
        self.assertEqual(2, result["failed_batch_count"])
        self.assertEqual(chunk_ids, result["unreviewed_chunk_ids"])

    def test_batches_are_sent_concurrently_instead_of_one_after_another(self) -> None:
        """전체 조항 검수는 묶음이 수십 개다. 순차로 부르면 검수 하나가 수십 분이 된다."""
        chunk_ids = [f"chunk_{index}" for index in range(12)]
        in_flight = 0
        peak_in_flight = 0
        lock = threading.Lock()

        def fake_post(url, headers, payload, timeout):
            nonlocal in_flight, peak_in_flight
            with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            sent = json.loads(payload["messages"][1]["content"])
            return openai_response([item["chunk_id"] for item in sent["items"]])

        result = self._executor(
            fake_post, agent_review_chunks_per_request=2, agent_review_max_parallel_requests=6
        ).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
        )

        self.assertEqual("executed", result["status"])
        self.assertEqual(6, result["batch_count"])
        self.assertEqual(12, result["reviewed_chunk_count"])
        self.assertGreater(peak_in_flight, 1)

    def test_reports_each_finished_batch_so_the_wait_is_countable(self) -> None:
        """AI 응답을 기다리는 동안이 전처리에서 가장 길게 멈춰 보인다.

        끝난 묶음 수를 세어 알려야 화면이 죽은 것인지 기다리는 것인지 구분된다.
        """
        chunk_ids = [f"chunk_{index}" for index in range(8)]

        def fake_post(url, headers, payload, timeout):
            sent = json.loads(payload["messages"][1]["content"])
            return openai_response([item["chunk_id"] for item in sent["items"]])

        events: list[tuple[int, int]] = []

        result = self._executor(
            fake_post, agent_review_chunks_per_request=2, agent_review_max_parallel_requests=4
        ).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
            progress_callback=lambda completed, total: events.append((completed, total)),
        )

        self.assertEqual("executed", result["status"])
        self.assertEqual((0, 4), events[0])
        self.assertEqual((4, 4), events[-1])
        self.assertEqual([0, 1, 2, 3, 4], [completed for completed, _total in events])

    def test_sequential_batches_also_report_progress(self) -> None:
        chunk_ids = [f"chunk_{index}" for index in range(4)]

        def fake_post(url, headers, payload, timeout):
            sent = json.loads(payload["messages"][1]["content"])
            return openai_response([item["chunk_id"] for item in sent["items"]])

        events: list[tuple[int, int]] = []

        self._executor(
            fake_post, agent_review_chunks_per_request=2, agent_review_max_parallel_requests=1
        ).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
            progress_callback=lambda completed, total: events.append((completed, total)),
        )

        self.assertEqual([(0, 2), (1, 2), (2, 2)], events)

    def test_parallel_results_keep_the_batch_order_for_failure_reporting(self) -> None:
        chunk_ids = [f"chunk_{index}" for index in range(6)]

        def fake_post(url, headers, payload, timeout):
            sent = json.loads(payload["messages"][1]["content"])
            batch_ids = [item["chunk_id"] for item in sent["items"]]
            # 마지막 묶음만 죽인다. 순서가 섞이면 엉뚱한 묶음이 실패로 기록된다.
            if "chunk_4" in batch_ids:
                raise RuntimeError("AI provider request failed: timed out")
            time.sleep(0.02 if "chunk_0" in batch_ids else 0.0)
            return openai_response(batch_ids)

        result = self._executor(
            fake_post,
            agent_review_chunks_per_request=2,
            agent_review_max_parallel_requests=4,
            agent_review_max_attempts=1,
        ).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review_for(chunk_ids),
            chunks=review_chunks_for(chunk_ids),
        )

        self.assertEqual(1, result["failed_batch_count"])
        self.assertEqual(3, result["failed_batches"][0]["batch_index"])
        self.assertEqual(["chunk_4", "chunk_5"], result["failed_batches"][0]["chunk_ids"])
        self.assertEqual(["chunk_4", "chunk_5"], result["unreviewed_chunk_ids"])

    def test_empty_items_from_the_provider_still_counts_as_executed(self) -> None:
        def fake_post(url, headers, payload, timeout):
            return {
                "id": "chatcmpl-empty",
                "choices": [{"message": {"content": '{"items":[]}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            }

        result = self._executor(fake_post).execute(
            document_id="doc_review",
            run_id="run_review",
            plan=planned_review(),
            chunks=[review_chunk()],
        )

        # '고칠 곳 없음'과 '응답을 못 받음'은 다르다.
        self.assertEqual("executed", result["status"])
        self.assertIsNone(result["skip_reason"])
        self.assertEqual(0, result["failed_batch_count"])


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
            # 깨진 응답은 다시 부른다. 한 번 실패했다고 검수를 통째로 버리지 않는다.
            self.assertEqual(result["api_call_count"], 3)
            self.assertEqual(result["provider_review_json"], {"items": []})
            # 실패한 시도도 제공자 쪽에서는 처리된 호출이라 토큰이 그대로 쌓인다.
            self.assertEqual(result["actual_total_tokens"], 42)
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
