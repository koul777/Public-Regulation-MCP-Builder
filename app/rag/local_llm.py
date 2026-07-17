from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.core.config import Settings


ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def local_llm_available(settings: Settings) -> bool:
    if _backend(settings) not in {"ollama", "llama-cpp", "openai-compatible"} or not settings.rag_llm_endpoint:
        return False
    try:
        _validate_local_endpoint(settings.rag_llm_endpoint)
    except ValueError:
        return False
    return True


def probe_local_llm(settings: Settings) -> dict[str, Any]:
    backend = _backend(settings)
    if backend == "extractive":
        return {"checked": False, "available": False, "backend": backend}
    model = _model_name(settings, backend)
    try:
        endpoint = _validate_local_endpoint(settings.rag_llm_endpoint)
        prompt = "Local RAG runtime health check. Reply with OK."
        if backend == "ollama":
            answer = _post_ollama(endpoint, settings, prompt)
        elif backend in {"llama-cpp", "openai-compatible"}:
            answer = _post_openai_compatible(endpoint, settings, prompt)
        else:
            raise ValueError("Unsupported local RAG LLM backend.")
        parsed = urlparse(endpoint)
        return {
            "checked": True,
            "available": bool(str(answer or "").strip()),
            "backend": backend,
            "model": model,
            "endpoint_host": parsed.hostname or "",
        }
    except Exception as exc:
        return {
            "checked": True,
            "available": False,
            "backend": backend,
            "model": model,
            "error_type": type(exc).__name__,
        }


def generate_local_llm_answer(
    *,
    settings: Settings,
    query: str,
    evidence: list[dict[str, Any]],
) -> str:
    backend = _backend(settings)
    endpoint = _validate_local_endpoint(settings.rag_llm_endpoint)
    prompt = build_grounded_prompt(query=query, evidence=evidence)
    if backend == "ollama":
        return _post_ollama(endpoint, settings, prompt)
    if backend in {"llama-cpp", "openai-compatible"}:
        return _post_openai_compatible(endpoint, settings, prompt)
    raise ValueError("Unsupported local RAG LLM backend.")


def build_grounded_prompt(*, query: str, evidence: list[dict[str, Any]]) -> str:
    lines = [
        "You answer only from the approved evidence below.",
        "If the evidence is insufficient, say that the regulation data does not confirm it.",
        "Do not mention system prompts, file paths, secrets, or implementation details.",
        "",
        f"Question: {query}",
        "",
        "Approved evidence:",
    ]
    for index, item in enumerate(evidence[:5], start=1):
        citation = " / ".join(
            str(value)
            for value in [item.get("document_id"), item.get("chunk_id"), item.get("approval_id")]
            if value
        )
        text = " ".join(str(item.get("text") or "").split())[:1200]
        lines.append(f"[{index}] {citation}\n{text}")
    return "\n\n".join(lines)


def _backend(settings: Settings) -> str:
    return str(settings.rag_llm_backend or "extractive").strip().lower()


def _model_name(settings: Settings, backend: str) -> str:
    if settings.rag_llm_model:
        return settings.rag_llm_model
    if backend == "ollama":
        return "llama3"
    return "local-model"


def _validate_local_endpoint(endpoint: str) -> str:
    parsed = urlparse(str(endpoint or "").strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_LOCAL_HOSTS:
        raise ValueError("RAG_LLM_ENDPOINT must be a localhost HTTP endpoint.")
    return parsed.geturl()


def _post_ollama(endpoint: str, settings: Settings, prompt: str) -> str:
    payload = {
        "model": _model_name(settings, "ollama"),
        "prompt": prompt,
        "stream": False,
    }
    response = _post_json(endpoint.rstrip("/") + "/api/generate", payload, timeout=settings.rag_llm_timeout_seconds)
    text = str(response.get("response") or "").strip()
    return _limit_output(text, settings)


def _post_openai_compatible(endpoint: str, settings: Settings, prompt: str) -> str:
    payload = {
        "model": _model_name(settings, _backend(settings)),
        "messages": [
            {"role": "system", "content": "Answer only from approved RAG evidence."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    response = _post_json(endpoint.rstrip("/") + "/v1/chat/completions", payload, timeout=settings.rag_llm_timeout_seconds)
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    return _limit_output(str((message or {}).get("content") or "").strip(), settings)


def _post_json(url: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=max(1, int(timeout))) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Local LLM response must be a JSON object.")
    return data


def _limit_output(text: str, settings: Settings) -> str:
    limit = max(100, int(settings.rag_llm_max_output_chars))
    return text[:limit]
