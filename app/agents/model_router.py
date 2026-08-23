from __future__ import annotations

"""Local-only model profiles and deterministic task-to-model routing."""

from dataclasses import asdict, dataclass
import ipaddress
from typing import Literal
from urllib.parse import urlparse


QWEN3_QUERY_MODEL = "qwen3:1.7b"
QWEN3_REVIEW_MODEL = "qwen3:4b"
QWEN3_ANSWER_MODEL = "qwen3:8b"
QWEN3_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
QWEN3_RERANKER_MODEL = "Qwen/Qwen3-Reranker-0.6B"
PADDLE_OCR_KOREAN_MODEL = "korean_PP-OCRv5_mobile_rec"

ModelRuntime = Literal["ollama", "sentence_transformers", "cross_encoder", "paddleocr"]


@dataclass(frozen=True)
class ModelProfileSpec:
    profile_id: str
    level: str
    capability: str
    runtime: ModelRuntime
    model: str
    endpoint: str | None = None
    max_input_units: int = 0
    timeout_seconds: int = 60
    temperature: float | None = None
    thinking_enabled: bool = False
    external_network_allowed: bool = False


MODEL_PROFILE_REGISTRY: dict[str, ModelProfileSpec] = {
    "ocr-korean-v5": ModelProfileSpec(
        profile_id="ocr-korean-v5",
        level="S1",
        capability="ocr",
        runtime="paddleocr",
        model=PADDLE_OCR_KOREAN_MODEL,
        max_input_units=1,
        timeout_seconds=120,
    ),
    "embedding-qwen3-0.6b": ModelProfileSpec(
        profile_id="embedding-qwen3-0.6b",
        level="S2-E",
        capability="embedding",
        runtime="sentence_transformers",
        model=QWEN3_EMBEDDING_MODEL,
        max_input_units=32_768,
        timeout_seconds=120,
    ),
    "reranker-qwen3-0.6b": ModelProfileSpec(
        profile_id="reranker-qwen3-0.6b",
        level="S2-R",
        capability="reranking",
        runtime="cross_encoder",
        model=QWEN3_RERANKER_MODEL,
        max_input_units=32_768,
        timeout_seconds=120,
    ),
    "query-qwen3-1.7b": ModelProfileSpec(
        profile_id="query-qwen3-1.7b",
        level="L1",
        capability="query_analysis",
        runtime="ollama",
        model=QWEN3_QUERY_MODEL,
        endpoint="http://127.0.0.1:11434",
        max_input_units=4_096,
        timeout_seconds=20,
        temperature=0.0,
    ),
    "review-qwen3-4b": ModelProfileSpec(
        profile_id="review-qwen3-4b",
        level="L2",
        capability="bounded_review",
        runtime="ollama",
        model=QWEN3_REVIEW_MODEL,
        endpoint="http://127.0.0.1:11434",
        max_input_units=8_192,
        timeout_seconds=45,
        temperature=0.0,
    ),
    "answer-qwen3-8b": ModelProfileSpec(
        profile_id="answer-qwen3-8b",
        level="L3",
        capability="grounded_answer",
        runtime="ollama",
        model=QWEN3_ANSWER_MODEL,
        endpoint="http://127.0.0.1:11434",
        max_input_units=16_384,
        timeout_seconds=60,
        temperature=0.1,
    ),
}


ROLE_MODEL_PROFILES: dict[str, str] = {
    "ocr_extractor": "ocr-korean-v5",
    "structure_reviewer": "review-qwen3-4b",
    "table_reviewer": "review-qwen3-4b",
    "semantic_embedder": "embedding-qwen3-0.6b",
    "query_analyst": "query-qwen3-1.7b",
    "query_rewriter": "query-qwen3-1.7b",
    "reranker": "reranker-qwen3-0.6b",
    "grounded_answerer": "answer-qwen3-8b",
    "claim_auditor": "review-qwen3-4b",
}


def get_model_profile(profile_id: str) -> ModelProfileSpec:
    normalized = str(profile_id or "").strip()
    try:
        return MODEL_PROFILE_REGISTRY[normalized]
    except KeyError:
        known = ", ".join(sorted(MODEL_PROFILE_REGISTRY))
        raise ValueError(f"Unknown model profile: {normalized or '<empty>'}. Known profiles: {known}") from None


def model_profile_for_role(role_id: str) -> ModelProfileSpec | None:
    profile_id = ROLE_MODEL_PROFILES.get(str(role_id or "").strip())
    return get_model_profile(profile_id) if profile_id else None


def model_profile_manifest() -> list[dict[str, object]]:
    return [asdict(MODEL_PROFILE_REGISTRY[key]) for key in sorted(MODEL_PROFILE_REGISTRY)]


def validate_model_registry() -> None:
    for profile_id, profile in MODEL_PROFILE_REGISTRY.items():
        if profile_id != profile.profile_id:
            raise ValueError(f"Model profile key does not match profile_id: {profile_id}")
        if profile.external_network_allowed:
            raise ValueError(f"External network access is forbidden for model profile: {profile_id}")
        if profile.runtime == "ollama":
            require_loopback_endpoint(profile.endpoint)
        elif profile.endpoint is not None:
            raise ValueError(f"In-process model profile must not declare an endpoint: {profile_id}")
    for role_id, profile_id in ROLE_MODEL_PROFILES.items():
        if not role_id or profile_id not in MODEL_PROFILE_REGISTRY:
            raise ValueError(f"Invalid role model mapping: {role_id} -> {profile_id}")


def require_loopback_endpoint(endpoint: str | None) -> str:
    value = str(endpoint or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Local model endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Local model endpoint must not include credentials, query, or fragment")
    hostname = parsed.hostname.strip().lower()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("Local model endpoint must use a loopback host")
        except ValueError as exc:
            if str(exc) == "Local model endpoint must use a loopback host":
                raise
            raise ValueError("Local model endpoint must use localhost or a loopback IP") from exc
    return value.rstrip("/")


validate_model_registry()
