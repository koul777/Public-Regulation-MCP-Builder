from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.core.gc_pause import gc_paused
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash, vector_record_path_leaks
from app.retrieval.semantic_models import Qwen3EmbeddingAdapter


EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION = "reg-rag-embedded-vector-record-v1"
# 로컬 Qwen3 임베딩은 CPU에서 조항당 수 초가 걸린다. 조항 200개짜리 규정이면 수십 분인데
# 진행 보고가 없으면 화면이 멈춘 것처럼 보인다. 표시 전용 콜백을 문맥 변수로 넘겨
# API 계약을 바꾸지 않고 '임베딩 n/N'을 화면에 보여 준다.
EmbeddingProgressCallback = Callable[[int, int], None]
_EMBEDDING_PROGRESS_CALLBACK: ContextVar[EmbeddingProgressCallback | None] = ContextVar(
    "embedding_progress_callback",
    default=None,
)
EMBEDDING_PROGRESS_SLICE = 16
LOCAL_HASH_EMBEDDING_MODEL = "local-hash-embedding-v1"
MAX_EMBEDDING_DIMENSIONS = 4096


def embed_vector_record(
    record: dict[str, Any],
    *,
    dimensions: int = 384,
    model: str = LOCAL_HASH_EMBEDDING_MODEL,
) -> dict[str, Any]:
    text = _validated_record_text(record, model=model)
    if model == LOCAL_HASH_EMBEDDING_MODEL:
        embedding = local_hash_embedding(text, dimensions=dimensions)
        runtime = "deterministic_hash"
        semantic = False
    else:
        embedding = _qwen_embedding_adapter(dimensions).encode_documents([text])[0]
        dimensions = len(embedding)
        runtime = "sentence_transformers"
        semantic = True
    return _embedded_record(
        record,
        text=text,
        embedding=embedding,
        dimensions=dimensions,
        model=model,
        runtime=runtime,
        semantic=semantic,
    )


def _embedded_record(
    record: dict[str, Any],
    *,
    text: str,
    embedding: list[float],
    dimensions: int,
    model: str,
    runtime: str,
    semantic: bool,
) -> dict[str, Any]:
    embedded = dict(record)
    embedded["schema_version"] = EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
    embedded["source_schema_version"] = record.get("schema_version")
    embedded["embedding_model"] = model
    embedded["embedding_dimensions"] = dimensions
    embedded["embedding"] = embedding
    embedded["embedding_hash"] = stable_embedding_hash(embedding)
    embedded["embedding_runtime"] = runtime
    embedded["embedding_semantic"] = semantic
    embedded["embedding_normalized"] = True
    embedded["embedding_generated_at"] = datetime.now(timezone.utc).isoformat()
    embedded["content_hash"] = stable_content_hash(text, embedded.get("metadata") or {})
    return embedded


@contextmanager
def embedding_progress(callback: EmbeddingProgressCallback) -> Iterator[None]:
    """Report ``(embedded, total)`` while Qwen3 vectors are computed in this context."""

    token = _EMBEDDING_PROGRESS_CALLBACK.set(callback)
    try:
        yield
    finally:
        _EMBEDDING_PROGRESS_CALLBACK.reset(token)


def _emit_embedding_progress(done: int, total: int) -> None:
    callback = _EMBEDDING_PROGRESS_CALLBACK.get()
    if callback is None:
        return
    try:
        callback(int(done), int(total))
    except Exception:
        # A display callback must never break indexing.
        return


def _encode_with_progress(adapter: Any, texts: list[str]) -> list[list[float]]:
    if _EMBEDDING_PROGRESS_CALLBACK.get() is None:
        return adapter.encode_documents(texts)
    total = len(texts)
    _emit_embedding_progress(0, total)
    vectors: list[list[float]] = []
    for start in range(0, total, EMBEDDING_PROGRESS_SLICE):
        vectors.extend(adapter.encode_documents(texts[start : start + EMBEDDING_PROGRESS_SLICE]))
        _emit_embedding_progress(len(vectors), total)
    return vectors


def embed_vector_records(
    records: Iterable[dict[str, Any]],
    *,
    dimensions: int = 384,
    model: str = LOCAL_HASH_EMBEDDING_MODEL,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    record_list = list(records)
    if model != QWEN3_EMBEDDING_MODEL or not record_list:
        with gc_paused():  # local hash embeddings: a long-lived acyclic list per record
            embedded = [embed_vector_record(record, dimensions=dimensions, model=model) for record in record_list]
            return embedded, summarize_embedded_records(embedded, model=model, dimensions=dimensions)

    texts = [_validated_record_text(record, model=model) for record in record_list]
    embeddings = _encode_with_progress(_qwen_embedding_adapter(dimensions), texts)
    if len(embeddings) != len(record_list):
        raise ValueError(
            "Qwen3 embedding adapter returned "
            f"{len(embeddings)} embeddings for {len(record_list)} vector records."
        )
    embedded = [
        _embedded_record(
            record,
            text=text,
            embedding=embedding,
            dimensions=len(embedding),
            model=model,
            runtime="sentence_transformers",
            semantic=True,
        )
        for record, text, embedding in zip(record_list, texts, embeddings)
    ]
    return embedded, summarize_embedded_records(embedded, model=model, dimensions=dimensions)


def _validated_record_text(record: dict[str, Any], *, model: str) -> str:
    if record.get("schema_version") not in {VECTOR_RECORD_SCHEMA_VERSION, EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION}:
        raise ValueError(f"Unsupported vector record schema_version: {record.get('schema_version')}")
    if model not in {LOCAL_HASH_EMBEDDING_MODEL, QWEN3_EMBEDDING_MODEL}:
        raise ValueError(f"Unsupported embedding model: {model}")
    text = str(record.get("text") or "").strip()
    if not text:
        raise ValueError(f"Vector record {record.get('id') or ''} is missing text.")
    return text


def local_hash_embedding(text: str, *, dimensions: int = 384) -> list[float]:
    if (
        not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or not 1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
    ):
        raise ValueError(
            f"Embedding dimensions must be an integer between 1 and {MAX_EMBEDDING_DIMENSIONS}."
        )
    tokens = _tokens(text)
    if not tokens:
        tokens = [text]
    if len(tokens) > _SPARSE_EMBEDDING_MAX_TOKENS:
        return _dense_local_hash_embedding(tokens, dimensions)
    # A text touches far fewer buckets than there are dimensions, so only the
    # touched buckets are accumulated and normalized.  Every touched bucket
    # holds a sum of +/-1.0 added in token order (an exact small integer, as in
    # the dense algorithm), the sum of squares is an exact integer below 2**53,
    # and untouched or cancelled buckets are 0.0 either way
    # (round(0.0 / norm, 8) == 0.0), so the result is bit-identical.
    buckets: dict[int, float] = {}
    for token in tokens:
        if len(token) <= _TOKEN_BUCKET_CACHE_MAX_CHARS:
            index, sign = _cached_token_bucket(token, dimensions)
        else:
            index, sign = _token_bucket(token, dimensions)
        buckets[index] = buckets.get(index, 0.0) + sign
    norm = math.sqrt(sum(value * value for value in buckets.values()))
    vector = [0.0] * dimensions
    if norm:
        for index, value in buckets.items():
            vector[index] = round(value / norm, 8)
    else:
        for index, value in buckets.items():
            vector[index] = value
    return vector


# Beyond this many tokens the squared bucket sums are no longer guaranteed to
# be exactly representable, so the original dense accumulation order is used.
_SPARSE_EMBEDDING_MAX_TOKENS = 1_000_000


def _dense_local_hash_embedding(tokens: list[str], dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens:
        if len(token) <= _TOKEN_BUCKET_CACHE_MAX_CHARS:
            index, sign = _cached_token_bucket(token, dimensions)
        else:
            index, sign = _token_bucket(token, dimensions)
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [round(value / norm, 8) for value in vector]
    return vector


def _token_bucket(token: str, dimensions: int) -> tuple[int, float]:
    """Return the (index, sign) one token contributes to a local hash embedding.

    This is a pure function of ``token`` and ``dimensions``. Only this per-token
    SHA-256 step is memoized; callers still recompute the full vector from the
    record text, so integrity checks keep detecting tampered text or vectors.
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % dimensions
    sign = -1.0 if digest[4] & 1 else 1.0
    return index, sign


# Integrity validation recomputes local_hash_embedding for every stored row on
# each indexing run and cold search, and regulation vocabulary repeats heavily.
# 65,536 entries holds a typical institution's whole token vocabulary for one
# dimension count. Only tokens of at most 64 characters are cached, so a full
# cache (entry, key tuple and retained token string) measures ~18 MiB for
# typical Korean tokens and ~34 MiB in the worst case (64 astral-plane
# characters per token). Longer tokens, such as a long whole-text fallback for
# text without word characters, are hashed directly and never retained.
_TOKEN_BUCKET_CACHE_SIZE = 65_536
_TOKEN_BUCKET_CACHE_MAX_CHARS = 64
_cached_token_bucket = lru_cache(maxsize=_TOKEN_BUCKET_CACHE_SIZE)(_token_bucket)


@lru_cache(maxsize=4)
def _qwen_embedding_adapter(dimensions: int) -> Qwen3EmbeddingAdapter:
    if (
        not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or not 64 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
    ):
        raise ValueError("Qwen3 embedding dimensions must be between 64 and 4096")
    return Qwen3EmbeddingAdapter(
        device="cpu",
        truncate_dim=dimensions,
        local_files_only=True,
    )


def summarize_embedded_records(
    records: list[dict[str, Any]],
    *,
    model: str = LOCAL_HASH_EMBEDDING_MODEL,
    dimensions: int = 384,
) -> dict[str, Any]:
    ids = [str(record.get("id") or "") for record in records]
    duplicate_ids = sorted([record_id for record_id, count in Counter(ids).items() if record_id and count > 1])
    invalid_dimensions = [
        str(record.get("id") or "")
        for record in records
        if not isinstance(record.get("embedding"), list) or len(record["embedding"]) != dimensions
    ]
    leaks = vector_record_path_leaks(records)
    return {
        "schema_version": EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION,
        "record_count": len(records),
        "embedding_model": model,
        "embedding_dimensions": dimensions,
        "duplicate_id_count": len(duplicate_ids),
        "duplicate_id_samples": duplicate_ids[:20],
        "invalid_embedding_dimension_count": len(invalid_dimensions),
        "invalid_embedding_dimension_samples": invalid_dimensions[:20],
        "local_path_leak_count": len(leaks),
        "local_path_leak_samples": leaks[:20],
    }


def stable_embedding_hash(embedding: list[float]) -> str:
    payload = json.dumps(embedding, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w가-힣]+", text.lower())
