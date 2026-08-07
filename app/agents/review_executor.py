from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.agents.execution_audit import append_provider_execution_record
from app.agents.execution_guard import payload_hash
from app.agents.provider_config import (
    agent_review_api_key,
    agent_review_configuration_reason,
    normalize_agent_review_provider,
)
from app.agents.review_context import review_context_for_metadata
from app.core.config import Settings
from app.schemas.chunk import Chunk


HTTP_POST = Callable[[str, dict[str, str], dict[str, Any], int], dict[str, Any]]

SYSTEM_PROMPT = """You review Korean public-institution regulation parser output.
Return compact JSON only. Do not approve the document.

Never rewrite, reformat, or restate the regulation text itself: the stored text is legal
evidence that a human edits and approves. Report findings only.

Write every "issues" entry and "recommended_human_check" in Korean. A Korean operator reads
them directly on the approval screen.

Report a finding only when you can point at the specific text that is wrong. Quote the short
fragment you mean, so the operator can find it. Look for: article/appendix boundaries merged
or split, missing body text, broken table structure, garbled Korean characters, and footnotes
or captions attached to the wrong article.

If the chunk has no such problem, return "issues": [] and leave "recommended_human_check"
empty. Never write filler such as "no parsing risks identified", "spacing should be verified",
or "line break consistency" — an operator who reads 200 of those stops reading all of them.
Do not report a finding merely because text is short, because a heading has no body, or
because spacing or punctuation could differ.

risk_level: "high" when the chunk should not be approved as-is (text missing, wrong article
merged in, table content lost); "medium" when a human must compare against the source to
decide; "low" for notation-level remarks. Most chunks are clean and get no finding at all.
"""
PARSER_REVIEW_PAYLOAD_CLASSIFICATION = "bounded_parser_review_chunks"
LOCAL_PATH_PATTERN = re.compile(r"(?i)(?:[a-z]:\\|\\\\[^\\]+\\|/(?:users|home|tmp|var|etc)/)")


class AgentReviewExecutor:
    """Runs the main AI review draft against configured provider APIs."""

    def __init__(self, settings: Settings, *, http_post: HTTP_POST | None = None) -> None:
        self.settings = settings
        self.http_post = http_post or _post_json

    def execute(
        self,
        *,
        document_id: str,
        run_id: str,
        plan: dict[str, Any],
        chunks: list[Chunk],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        if str(plan.get("status") or "") != "planned" or int(plan.get("selected_count") or 0) <= 0:
            return plan

        readiness = self._readiness()
        result = dict(plan)
        result["provider_execution_ready"] = readiness["ready"]
        if not readiness["ready"]:
            result["status"] = "api_configuration_needed"
            result["skip_reason"] = readiness["reason"]
            return result

        provider = normalize_agent_review_provider(self.settings.llm_provider)
        batches = self._candidate_batches(plan)
        payloads = [self._provider_payload(plan, chunks, provider=provider, candidates=batch) for batch in batches]
        payloads = [payload for payload in payloads if payload["_item_count"]]
        payload_digest = payload_hash(
            {"provider": self.settings.llm_provider, "request": [p["request"] for p in payloads]}
        )
        for payload in payloads:
            payload_leak_reason = _payload_local_path_leak_reason(payload["request"])
            if payload_leak_reason:
                result.update(
                    {
                        "status": "provider_execution_blocked",
                        "skip_reason": payload_leak_reason,
                        "api_call_count": 0,
                        "payload_hash": payload_digest,
                    }
                )
                return result

        review_items: list[dict[str, Any]] = []
        response_texts: list[str] = []
        request_ids: list[str] = []
        failures: list[dict[str, Any]] = []
        api_call_count = 0
        succeeded_batches = 0
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        started = time.perf_counter()
        # 묶음끼리는 서로를 기다릴 이유가 없다. 순차로 부르면 277조항짜리 규정에서
        # 70번을 줄줄이 기다리게 되어 검수 하나에 수십 분이 걸린다. 응답 대기가
        # 대부분이라 스레드로 겹쳐 부르면 벽시계 시간이 병렬 수만큼 줄어든다.
        outcomes = self._request_batches(
            payloads,
            provider=provider,
            progress_callback=progress_callback,
        )
        for batch_index, (payload, outcome) in enumerate(zip(payloads, outcomes), start=1):
            api_call_count += int(outcome["attempts"])
            # 실패한 묶음도 제공자 호출은 실제로 일어났다. 토큰과 요청 ID는 성공 여부와
            # 무관하게 남겨야 감사 기록이 실제 사용량과 맞는다.
            prompt_tokens += int(outcome["prompt_tokens"])
            completion_tokens += int(outcome["completion_tokens"])
            total_tokens += int(outcome["total_tokens"])
            if outcome["request_id"]:
                request_ids.append(str(outcome["request_id"]))
            if not outcome["ok"]:
                # 한 묶음이 끝내 실패해도 나머지는 버리지 않는다. 되돌려받은 검수 결과가
                # 하나라도 있으면 사람이 그만큼은 바로 쓸 수 있어야 한다.
                failures.append(
                    {
                        "batch_index": batch_index,
                        "chunk_ids": list(payload["chunk_ids"]),
                        "attempts": int(outcome["attempts"]),
                        "reason": str(outcome["reason"]),
                        "error": str(outcome["error"]),
                    }
                )
                continue
            succeeded_batches += 1
            response_texts.append(str(outcome["response_text"]))
            batch_items = outcome["review_json"].get("items")
            if isinstance(batch_items, list):
                review_items.extend(item for item in batch_items if isinstance(item, dict))
        elapsed_seconds = round(time.perf_counter() - started, 3)

        reviewed_chunk_ids = {
            str(item.get("chunk_id") or "") for item in review_items if str(item.get("chunk_id") or "").strip()
        }
        failed_chunk_ids = [
            chunk_id
            for failure in failures
            for chunk_id in failure["chunk_ids"]
            if chunk_id not in reviewed_chunk_ids
        ]
        # 응답이 비어 있는 것과 응답을 못 받은 것은 다르다. 제공자가 '고칠 곳 없음'으로
        # 빈 items를 준 경우까지 실패로 적으면 화면이 또 거짓말을 한다.
        if succeeded_batches:
            status = "executed"
            skip_reason = "provider_partial_batches_failed" if failures else None
        else:
            status = "provider_execution_failed"
            skip_reason = str(failures[0]["reason"]) if failures else "provider_request_failed"
        provider_request_id = request_ids[0] if request_ids else ""

        result.update(
            {
                "status": status,
                "skip_reason": skip_reason,
                "api_call_count": api_call_count,
                "batch_count": len(payloads),
                "failed_batch_count": len(failures),
                "failed_batches": failures,
                "unreviewed_chunk_ids": failed_chunk_ids,
                "reviewed_chunk_count": len(reviewed_chunk_ids),
                "provider_request_id": provider_request_id,
                "provider_request_ids": request_ids,
                "provider_elapsed_seconds": elapsed_seconds,
                "provider_response_text": "\n".join(response_texts),
                "provider_review_json": {"items": review_items},
                "payload_hash": payload_digest,
                "actual_input_tokens": prompt_tokens,
                "actual_output_tokens": completion_tokens,
                "actual_total_tokens": total_tokens,
                "actual_cost": "0",
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if failures:
            result["provider_error"] = failures[0]["error"]
        self._append_execution_audit(
            document_id=document_id,
            run_id=run_id,
            result=result,
            payload_digest=payload_digest,
            provider_request_id=provider_request_id or "unknown",
            total_tokens=total_tokens,
        )
        return result

    def _candidate_batches(self, plan: dict[str, Any]) -> list[list[dict[str, Any]]]:
        """선정된 조항을 작은 묶음으로 나눈다.

        한 번에 20개를 보내면 교정본 출력이 1만 토큰을 넘어 응답이 오래 걸리고,
        늦으면 그 20개가 통째로 사라졌다. 묶음을 작게 나누면 각 호출이 짧게 끝나고,
        한 묶음이 실패해도 나머지 결과는 그대로 남는다.
        """
        candidates = [
            candidate
            for candidate in (plan.get("selected_candidates") or [])
            if isinstance(candidate, dict)
        ]
        size = max(1, int(self.settings.agent_review_chunks_per_request))
        return [candidates[start : start + size] for start in range(0, len(candidates), size)]

    def _request_batches(
        self,
        payloads: list[dict[str, Any]],
        *,
        provider: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """묶음들을 동시에 부르고, 결과는 보낸 순서대로 돌려준다.

        순서를 지키는 이유는 실패 보고와 감사 기록이 '몇 번째 묶음'으로 남기 때문이다.
        동시 실행 수는 제공자 쪽 속도 제한에 걸리지 않도록 설정으로 묶어 둔다.

        AI 응답을 기다리는 동안이 전처리에서 가장 길게 멈춰 보이는 구간이다.
        ``progress_callback``으로 실제로 끝난 묶음 수를 세어 알린다.
        """
        if not payloads:
            return []
        total = len(payloads)
        completed = 0

        def report_completed() -> None:
            nonlocal completed
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)

        if progress_callback is not None:
            progress_callback(0, total)
        max_parallel = max(1, int(self.settings.agent_review_max_parallel_requests))
        if max_parallel == 1 or total == 1:
            outcomes = []
            for payload in payloads:
                outcomes.append(self._request_batch_with_retries(payload["request"], provider=provider))
                report_completed()
            return outcomes
        with ThreadPoolExecutor(max_workers=min(max_parallel, total)) as pool:
            futures = [
                pool.submit(self._request_batch_with_retries, payload["request"], provider=provider)
                for payload in payloads
            ]
            # 완료 순서로 세고, 결과는 보낸 순서로 돌려준다.
            for _finished in as_completed(futures):
                report_completed()
            return [future.result() for future in futures]

    def _request_batch_with_retries(self, payload: dict[str, Any], *, provider: str) -> dict[str, Any]:
        """한 묶음을 성공할 때까지 정해진 횟수만큼 다시 부른다.

        네트워크 지연이나 일시적인 제공자 오류로 검수가 통째로 없어지면 안 된다.
        시간이 모자라 실패한 경우가 대부분이라 재시도 사이에 잠깐 기다린다.
        """
        attempts = 0
        last_error = ""
        # 응답을 못 받은 것과, 받았지만 JSON이 깨진 것은 원인이 달라 안내도 달라야 한다.
        last_reason = "provider_request_failed"
        last_request_id = ""
        # 실패한 시도도 제공자 쪽에서는 실제로 처리된 호출이다. 토큰을 세지 않으면
        # 감사 기록과 예산이 실제 사용량보다 적게 남는다.
        spent_prompt_tokens = 0
        spent_completion_tokens = 0
        spent_total_tokens = 0
        max_attempts = max(1, int(self.settings.agent_review_max_attempts))
        timeout_seconds = max(1, int(self.settings.agent_review_timeout_seconds))
        for attempt in range(1, max_attempts + 1):
            attempts += 1
            try:
                response = self.http_post(
                    self._chat_url(provider),
                    self._request_headers(provider),
                    payload,
                    timeout_seconds,
                )
            except Exception as exc:
                last_error = str(exc)
                last_reason = "provider_request_failed"
                if attempt < max_attempts:
                    time.sleep(min(8.0, 2.0 * attempt))
                continue
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            prompt_tokens = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
            completion_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
            total_tokens = _safe_int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
            spent_prompt_tokens += prompt_tokens
            spent_completion_tokens += completion_tokens
            spent_total_tokens += total_tokens
            last_request_id = str(response.get("id") or "") or last_request_id
            try:
                response_text = _extract_provider_text(response, provider=provider)
                review_json = _parse_json_object(response_text)
            except ValueError as exc:
                last_error = str(exc)
                last_reason = str(exc) or "provider_response_invalid_json"
                if attempt < max_attempts:
                    time.sleep(min(8.0, 2.0 * attempt))
                continue
            return {
                "ok": True,
                "attempts": attempts,
                "reason": "",
                "error": "",
                "response_text": response_text,
                "review_json": review_json,
                "request_id": last_request_id,
                "prompt_tokens": spent_prompt_tokens,
                "completion_tokens": spent_completion_tokens,
                "total_tokens": spent_total_tokens,
            }
        return {
            "ok": False,
            "attempts": attempts,
            "reason": last_reason,
            "error": last_error or "provider_request_failed",
            "response_text": "",
            "review_json": {},
            "request_id": last_request_id,
            "prompt_tokens": spent_prompt_tokens,
            "completion_tokens": spent_completion_tokens,
            "total_tokens": spent_total_tokens,
        }

    def _readiness(self) -> dict[str, Any]:
        reason = agent_review_configuration_reason(self.settings)
        return {"ready": not reason, "reason": reason}

    def _chat_url(self, provider: str) -> str:
        if provider == "azure-openai":
            return _append_api_path(self.settings.azure_openai_endpoint, "/openai/v1/chat/completions")
        if provider == "anthropic":
            return _append_api_path(self.settings.anthropic_api_base_url, "/v1/messages")
        return _append_api_path(self.settings.agent_review_api_base_url, "/v1/chat/completions")

    def _request_headers(self, provider: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = agent_review_api_key(self.settings)
        if provider == "azure-openai":
            headers["api-key"] = api_key
        elif provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _provider_payload(
        self,
        plan: dict[str, Any],
        chunks: list[Chunk],
        *,
        provider: str,
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """한 묶음의 요청 본문과, 그 묶음이 무엇을 담고 있는지를 함께 돌려준다."""
        openai_payload, chunk_ids = self._chat_payload(plan, chunks, candidates=candidates)
        if provider == "anthropic":
            request: dict[str, Any] = {
                "model": openai_payload["model"],
                "temperature": openai_payload["temperature"],
                "max_tokens": openai_payload["max_tokens"],
                "system": SYSTEM_PROMPT,
                "messages": [openai_payload["messages"][1]],
            }
        else:
            request = openai_payload
        return {"request": request, "chunk_ids": chunk_ids, "_item_count": len(chunk_ids)}

    def _chat_payload(
        self,
        plan: dict[str, Any],
        chunks: list[Chunk],
        *,
        candidates: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        items: list[dict[str, Any]] = []
        selected = plan.get("selected_candidates") or [] if candidates is None else candidates
        for candidate in selected:
            chunk_id = str(candidate.get("chunk_id") or "")
            chunk = chunks_by_id.get(chunk_id)
            if not chunk:
                continue
            items.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "chunk_type": chunk.chunk_type,
                    "source_page_start": chunk.source_page_start,
                    "source_page_end": chunk.source_page_end,
                    "review_reasons": candidate.get("reasons") or [],
                    "content_hash": candidate.get("content_hash"),
                    "review_context": review_context_for_metadata(chunk.metadata or {}),
                    "text": chunk.normalized_text or chunk.text,
                }
            )
        user_payload = {
            "task": "Create an AI review draft for human parser QA. Do not approve the content.",
            "required_json_shape": {
                "items": [
                    {
                        "chunk_id": "string",
                        "risk_level": "low|medium|high",
                        "issues": ["string"],
                        "recommended_human_check": "string",
                        "confidence": 0.0,
                    }
                ]
            },
            "items": items,
        }
        # 묶음별 호출이므로 출력 상한도 그 묶음 몫만 잡는다. 계획서의 전체 몫을 그대로
        # 쓰면 조항 몇 개짜리 호출에 1만 토큰을 열어 두게 되어 응답이 길어진다.
        # 조항당 몫은 계획서에서 나눠 쓴다. 묶음 전체를 합치면 계획서가 예약한 총량과
        # 같아지므로 예산 검사와 어긋나지 않는다.
        if candidates is None:
            max_tokens = max(1, int(plan.get("estimated_output_tokens") or 512))
        else:
            planned_output_tokens = int(plan.get("estimated_output_tokens") or 0)
            planned_chunk_count = max(1, int(plan.get("selected_count") or 0))
            per_chunk_tokens = (
                max(1, planned_output_tokens // planned_chunk_count)
                if planned_output_tokens > 0
                else max(1, int(self.settings.agent_review_max_output_tokens_per_chunk))
            )
            max_tokens = max(1, per_chunk_tokens * len(items))
        return (
            {
                "model": self.settings.agent_review_model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            },
            [str(item["chunk_id"]) for item in items],
        )

    def _append_execution_audit(
        self,
        *,
        document_id: str,
        run_id: str,
        result: dict[str, Any],
        payload_digest: str,
        provider_request_id: str,
        total_tokens: int,
    ) -> None:
        estimated_cost = str(result.get("estimated_cost") or "0")
        if estimated_cost == "":
            estimated_cost = "0"
        reserved_total_tokens = max(int(result.get("estimated_total_tokens") or 0), total_tokens)
        append_provider_execution_record(
            self.settings,
            {
                "actor": "system:processing_service",
                "approval_reference": "parser_ai_review_default",
                "document_id": document_id,
                "run_id": run_id,
                "provider": self.settings.llm_provider,
                "model": self.settings.agent_review_model,
                "budget_reservation_id": "implicit_parser_ai_review",
                "prompt_hash": payload_hash({"system": SYSTEM_PROMPT}),
                "payload_hash": payload_digest,
                "payload_classification": PARSER_REVIEW_PAYLOAD_CLASSIFICATION,
                "reserved_total_tokens": reserved_total_tokens,
                "actual_total_tokens": total_tokens,
                "estimated_cost": estimated_cost,
                "actual_cost": "0",
                "provider_request_id": provider_request_id,
                "outcome": str(result.get("status") or "executed"),
            },
        )


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI provider request failed with HTTP {exc.code}: {body[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"AI provider request failed: {exc.reason}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("AI provider response was not a JSON object.")
    return parsed


def _payload_local_path_leak_reason(value: Any) -> str:
    stack: list[tuple[str, Any]] = [("payload", value)]
    while stack:
        path, current = stack.pop()
        if isinstance(current, str):
            if LOCAL_PATH_PATTERN.search(current):
                return f"provider_payload_local_path_leak:{path}"
            continue
        if isinstance(current, dict):
            stack.extend((f"{path}.{key}", item) for key, item in current.items())
            continue
        if isinstance(current, list):
            stack.extend((f"{path}[{index}]", item) for index, item in enumerate(current))
    return ""


def _extract_provider_text(response: dict[str, Any], *, provider: str) -> str:
    if provider == "anthropic":
        content = response.get("content")
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return ""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _append_api_path(base_url: str, path: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    normalized_path = "/" + path.strip("/")
    if base.lower().endswith(normalized_path.lower()):
        return base
    for suffix in ("/v1", "/openai/v1"):
        if base.lower().endswith(suffix) and normalized_path.lower().startswith(suffix + "/"):
            return base + normalized_path[len(suffix) :]
    return base + normalized_path


def _parse_json_object(text: str) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("provider_response_missing_content")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("provider_response_invalid_json") from None
    if not isinstance(parsed, dict):
        raise ValueError("provider_response_not_json_object")
    return parsed


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
