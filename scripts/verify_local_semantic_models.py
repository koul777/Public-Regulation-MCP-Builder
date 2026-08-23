from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow this operational CLI to run directly from a source checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.model_router import QWEN3_EMBEDDING_MODEL, QWEN3_RERANKER_MODEL
from app.retrieval.semantic_models import (
    Qwen3EmbeddingAdapter,
    Qwen3RerankerAdapter,
    cosine_similarity,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify local Qwen3 semantic embedding and reranker models.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/image_pipeline_6hour/local_semantic_model_verification.json"),
    )
    return parser


def verify() -> dict:
    query = "사용자 접근권한은 얼마나 자주 검토해야 하나요?"
    relevant = "정보시스템 관리자는 모든 사용자 접근권한을 분기마다 검토하여야 한다."
    unrelated = "출장자는 교통비와 숙박비 영수증을 제출하여야 한다."
    report: dict = {
        "schema_version": "reg-rag-local-semantic-verification-v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "device": "cpu",
        "embedding": {"model": QWEN3_EMBEDDING_MODEL, "passed": False},
        "reranker": {"model": QWEN3_RERANKER_MODEL, "passed": False},
        "passed": False,
    }

    started = time.perf_counter()
    embedding_adapter = Qwen3EmbeddingAdapter(device="cpu", local_files_only=True)
    query_vector = embedding_adapter.encode_queries([query], batch_size=1)[0]
    document_vectors = embedding_adapter.encode_documents([relevant, unrelated], batch_size=2)
    relevant_similarity = cosine_similarity(query_vector, document_vectors[0])
    unrelated_similarity = cosine_similarity(query_vector, document_vectors[1])
    embedding_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    embedding_passed = (
        len(query_vector) > 0
        and len(query_vector) == len(document_vectors[0]) == len(document_vectors[1])
        and abs(_norm(query_vector) - 1.0) < 0.001
        and relevant_similarity > unrelated_similarity
    )
    report["embedding"] = {
        "model": QWEN3_EMBEDDING_MODEL,
        "passed": embedding_passed,
        "dimensions": len(query_vector),
        "query_vector_norm": round(_norm(query_vector), 8),
        "relevant_similarity": relevant_similarity,
        "unrelated_similarity": unrelated_similarity,
        "relevance_margin": round(relevant_similarity - unrelated_similarity, 8),
        "duration_ms": embedding_elapsed_ms,
    }
    del embedding_adapter, query_vector, document_vectors
    gc.collect()

    started = time.perf_counter()
    reranker_adapter = Qwen3RerankerAdapter(
        device="cpu",
        max_length=1024,
        local_files_only=True,
        model_path=Path("data/semantic_runtime_models/qwen3-reranker-0.6b"),
    )
    reranker_scores = reranker_adapter.score(query, [relevant, unrelated], batch_size=2)
    reranker_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    reranker_passed = (
        len(reranker_scores) == 2
        and all(0.0 <= score <= 1.0 for score in reranker_scores)
        and reranker_scores[0] > reranker_scores[1]
    )
    report["reranker"] = {
        "model": QWEN3_RERANKER_MODEL,
        "passed": reranker_passed,
        "relevant_score": reranker_scores[0] if reranker_scores else None,
        "unrelated_score": reranker_scores[1] if len(reranker_scores) > 1 else None,
        "relevance_margin": (
            round(reranker_scores[0] - reranker_scores[1], 8)
            if len(reranker_scores) > 1
            else None
        ),
        "duration_ms": reranker_elapsed_ms,
    }
    report["passed"] = bool(embedding_passed and reranker_passed)
    return report


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = verify()
    except Exception as exc:
        report = {
            "schema_version": "reg-rag-local-semantic-verification-v1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
