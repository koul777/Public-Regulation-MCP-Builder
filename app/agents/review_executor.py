from __future__ import annotations

import json
import re
import time
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
For each item, identify parsing risks that require human review, especially article boundaries,
appendix/form boundaries, tables, nested tables, footnotes, captions, and broken Korean text.
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
        payload = self._provider_payload(plan, chunks, provider=provider)
        payload_digest = payload_hash({"provider": self.settings.llm_provider, "request": payload})
        payload_leak_reason = _payload_local_path_leak_reason(payload)
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
        started = time.perf_counter()
        try:
            response = self.http_post(
                self._chat_url(provider),
                self._request_headers(provider),
                payload,
                max(1, int(self.settings.agent_review_timeout_seconds)),
            )
        except Exception as exc:
            result["status"] = "provider_execution_failed"
            result["skip_reason"] = "provider_request_failed"
            result["provider_error"] = str(exc)
            return result

        elapsed_seconds = round(time.perf_counter() - started, 3)
        response_text = _extract_provider_text(response, provider=provider)
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        prompt_tokens = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
        provider_request_id = str(response.get("id") or "")

        try:
            review_json = _parse_json_object(response_text)
        except ValueError as exc:
            result.update(
                {
                    "status": "provider_execution_failed",
                    "skip_reason": str(exc),
                    "api_call_count": 1,
                    "provider_request_id": provider_request_id,
                    "provider_elapsed_seconds": elapsed_seconds,
                    "provider_response_text": response_text,
                    "provider_review_json": {},
                    "payload_hash": payload_digest,
                    "actual_input_tokens": prompt_tokens,
                    "actual_output_tokens": completion_tokens,
                    "actual_total_tokens": total_tokens,
                    "actual_cost": "0",
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._append_execution_audit(
                document_id=document_id,
                run_id=run_id,
                result=result,
                payload_digest=payload_digest,
                provider_request_id=provider_request_id or "unknown",
                total_tokens=total_tokens,
            )
            return result

        result.update(
            {
                "status": "executed",
                "skip_reason": None,
                "api_call_count": 1,
                "provider_request_id": provider_request_id,
                "provider_elapsed_seconds": elapsed_seconds,
                "provider_response_text": response_text,
                "provider_review_json": review_json,
                "payload_hash": payload_digest,
                "actual_input_tokens": prompt_tokens,
                "actual_output_tokens": completion_tokens,
                "actual_total_tokens": total_tokens,
                "actual_cost": "0",
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._append_execution_audit(
            document_id=document_id,
            run_id=run_id,
            result=result,
            payload_digest=payload_digest,
            provider_request_id=provider_request_id or "unknown",
            total_tokens=total_tokens,
        )
        return result

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

    def _provider_payload(self, plan: dict[str, Any], chunks: list[Chunk], *, provider: str) -> dict[str, Any]:
        openai_payload = self._chat_payload(plan, chunks)
        if provider != "anthropic":
            return openai_payload
        return {
            "model": openai_payload["model"],
            "temperature": openai_payload["temperature"],
            "max_tokens": openai_payload["max_tokens"],
            "system": SYSTEM_PROMPT,
            "messages": [openai_payload["messages"][1]],
        }

    def _chat_payload(self, plan: dict[str, Any], chunks: list[Chunk]) -> dict[str, Any]:
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        items: list[dict[str, Any]] = []
        for candidate in plan.get("selected_candidates") or []:
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
        return {
            "model": self.settings.agent_review_model,
            "temperature": 0,
            "max_tokens": max(1, int(plan.get("estimated_output_tokens") or 512)),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }

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
