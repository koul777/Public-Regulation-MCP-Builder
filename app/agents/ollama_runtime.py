from __future__ import annotations

"""Minimal localhost-only Ollama runtime shared by bounded model agents."""

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from app.agents.model_router import require_loopback_endpoint


DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"


class OllamaRuntimeError(RuntimeError):
    pass


class OllamaGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    response_text: str
    duration_ms: float = Field(ge=0.0)
    prompt_eval_count: int = Field(default=0, ge=0)
    eval_count: int = Field(default=0, ge=0)


class OllamaRuntime:
    def __init__(self, endpoint: str = DEFAULT_OLLAMA_ENDPOINT) -> None:
        self.endpoint = require_loopback_endpoint(endpoint)

    def installed_models(self, *, timeout_seconds: int = 5) -> set[str]:
        payload = self._request_json("GET", "/api/tags", None, timeout_seconds=timeout_seconds)
        models = payload.get("models")
        if not isinstance(models, list):
            return set()
        names: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            for key in ("name", "model"):
                name = str(item.get(key) or "").strip()
                if name:
                    names.add(name)
        return names

    def model_available(self, model: str, *, timeout_seconds: int = 5) -> bool:
        normalized = str(model or "").strip()
        if not normalized:
            return False
        names = self.installed_models(timeout_seconds=timeout_seconds)
        return normalized in names or f"{normalized}:latest" in names

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        timeout_seconds: int,
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
        json_schema: dict[str, Any] | str | None = None,
    ) -> OllamaGeneration:
        normalized_model = str(model or "").strip()
        normalized_prompt = str(prompt or "").strip()
        if not normalized_model or not normalized_prompt:
            raise ValueError("Ollama generation requires model and prompt")
        payload: dict[str, Any] = {
            "model": normalized_model,
            "prompt": normalized_prompt,
            "stream": False,
            "think": False,
            "keep_alive": "2m",
            "options": {
                "temperature": float(temperature),
                "seed": 0,
                "num_predict": max(32, min(int(max_output_tokens), 8192)),
            },
        }
        if json_schema:
            payload["format"] = json_schema
        started = time.perf_counter()
        response = self._request_json(
            "POST",
            "/api/generate",
            payload,
            timeout_seconds=timeout_seconds,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        return OllamaGeneration(
            model=normalized_model,
            response_text=str(response.get("response") or "").strip(),
            duration_ms=round(duration_ms, 3),
            prompt_eval_count=_nonnegative_int(response.get("prompt_eval_count")),
            eval_count=_nonnegative_int(response.get("eval_count")),
        )

    def generate_json(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, Any],
        timeout_seconds: int,
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
    ) -> tuple[dict[str, Any], OllamaGeneration]:
        structured_prompt = (
            str(prompt).rstrip()
            + "\n\n반드시 아래 JSON Schema와 정확히 일치하는 JSON 객체만 출력하라. "
            + "스키마에 없는 필드는 출력하지 말라.\nJSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False, sort_keys=True)
        )
        try:
            generation = self.generate(
                model=model,
                prompt=structured_prompt,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                json_schema=schema,
            )
        except OllamaRuntimeError as exc:
            # Some Ollama/llama.cpp builds reject valid nested JSON Schema as a
            # grammar. Keep JSON mode on and enforce the full schema client-side
            # with Pydantic instead of silently accepting free-form text.
            if "failed to parse grammar" not in str(exc).lower():
                raise
            generation = self.generate(
                model=model,
                prompt=structured_prompt,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                json_schema="json",
            )
        try:
            decoded = json.loads(generation.response_text)
        except json.JSONDecodeError as exc:
            raise ValueError("Ollama structured response is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Ollama structured response must be a JSON object")
        return decoded, generation

    def _request_json(
        self,
        method: str,
        route: str,
        payload: dict[str, Any] | None,
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.endpoint + route,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=max(1, min(int(timeout_seconds), 600))) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")[:1000]
            try:
                error_payload = json.loads(raw_error)
            except json.JSONDecodeError:
                error_payload = {}
            message = str(error_payload.get("error") or f"HTTP {exc.code}").strip()[:500]
            raise OllamaRuntimeError(f"Ollama request failed: {message}") from exc
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("Ollama response must be a JSON object")
        return decoded


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
