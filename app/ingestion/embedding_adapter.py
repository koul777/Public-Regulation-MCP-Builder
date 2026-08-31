from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.ingestion.vector_adapter import VECTOR_RECORD_SCHEMA_VERSION, stable_content_hash, vector_record_path_leaks
from app.retrieval.semantic_models import Qwen3EmbeddingAdapter


EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION = "reg-rag-embedded-vector-record-v1"
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


def embed_vector_records(
    records: Iterable[dict[str, Any]],
    *,
    dimensions: int = 384,
    model: str = LOCAL_HASH_EMBEDDING_MODEL,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    record_list = list(records)
    if model != QWEN3_EMBEDDING_MODEL or not record_list:
        embedded = [embed_vector_record(record, dimensions=dimensions, model=model) for record in record_list]
        return embedded, summarize_embedded_records(embedded, model=model, dimensions=dimensions)

    texts = [_validated_record_text(record, model=model) for record in record_list]
    embeddings = _qwen_embedding_adapter(dimensions).encode_documents(texts)
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
    vector = [0.0] * dimensions
    tokens = _tokens(text)
    if not tokens:
        tokens = [text]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = -1.0 if digest[4] & 1 else 1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [round(value / norm, 8) for value in vector]
    return vector


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
