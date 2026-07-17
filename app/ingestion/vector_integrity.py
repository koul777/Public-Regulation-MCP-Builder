from __future__ import annotations

import math
from typing import Any

from app.ingestion.embedding_adapter import (
    EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION,
    LOCAL_HASH_EMBEDDING_MODEL,
    MAX_EMBEDDING_DIMENSIONS,
    local_hash_embedding,
    stable_embedding_hash,
)


def embedded_vector_integrity_reason(record: dict[str, Any]) -> str:
    if record.get("schema_version") != EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION:
        return ""
    record_id = str(record.get("id") or "")
    embedding = record.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        return "missing_embedding"
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in embedding):
        return "embedding_non_numeric"
    if any(not math.isfinite(float(value)) for value in embedding):
        return "embedding_non_finite"
    dimensions = record.get("embedding_dimensions")
    if (
        not isinstance(dimensions, int)
        or isinstance(dimensions, bool)
        or dimensions != len(embedding)
        or not 1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
    ):
        return "embedding_dimensions_invalid"
    expected_hash = stable_embedding_hash([float(value) for value in embedding])
    if str(record.get("embedding_hash") or "") != expected_hash:
        return "embedding_hash_mismatch"
    if str(record.get("embedding_model") or "") == LOCAL_HASH_EMBEDDING_MODEL:
        text = str(record.get("text") or "").strip()
        dimensions = len(embedding)
        expected_embedding = local_hash_embedding(text, dimensions=dimensions)
        if [float(value) for value in embedding] != expected_embedding:
            return "embedding_vector_mismatch"
    return ""
