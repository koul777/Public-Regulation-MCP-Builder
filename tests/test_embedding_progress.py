"""색인 중 임베딩 진행이 화면으로 보고되는지 고정한다.

실제 인사규정(조항 214개)을 CPU에서 색인하자 Qwen3 임베딩만 수십 분이 걸렸고, 그동안 진행
보고가 없어 화면이 멈춘 것처럼 보였다. 콜백이 있으면 조각 단위로 n/N을 보고하고, 콜백이
없으면 예전처럼 한 번에 계산해야 한다. 보고 콜백이 실패해도 색인은 계속돼야 한다.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.ingestion import embedding_adapter
from app.ingestion.embedding_adapter import EMBEDDING_PROGRESS_SLICE, embedding_progress


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]


def _records(count: int) -> list[dict[str, Any]]:
    return [
        {
            "schema_version": embedding_adapter.VECTOR_RECORD_SCHEMA_VERSION,
            "record_id": f"r{index}",
            "chunk_id": f"c{index}",
            "document_id": "doc",
            "text": f"제{index}조 합성 본문",
            "metadata": {},
        }
        for index in range(count)
    ]


class EmbeddingProgressTests(unittest.TestCase):
    def _run(self, count: int, callback=None) -> tuple[_FakeAdapter, list]:
        adapter = _FakeAdapter()
        with patch.object(embedding_adapter, "_qwen_embedding_adapter", return_value=adapter), patch.object(
            embedding_adapter, "_validated_record_text", side_effect=lambda record, model: record["text"]
        ), patch.object(embedding_adapter, "summarize_embedded_records", return_value={}):
            if callback is None:
                embedded, _ = embedding_adapter.embed_vector_records(_records(count), model=QWEN3_EMBEDDING_MODEL)
            else:
                with embedding_progress(callback):
                    embedded, _ = embedding_adapter.embed_vector_records(_records(count), model=QWEN3_EMBEDDING_MODEL)
        return adapter, embedded

    def test_reports_each_slice_and_finishes_at_total(self) -> None:
        events: list[tuple[int, int]] = []
        count = EMBEDDING_PROGRESS_SLICE * 2 + 3
        adapter, embedded = self._run(count, lambda done, total: events.append((done, total)))
        self.assertEqual(count, len(embedded))
        self.assertEqual([EMBEDDING_PROGRESS_SLICE, EMBEDDING_PROGRESS_SLICE, 3], adapter.calls)
        self.assertEqual((0, count), events[0])
        self.assertEqual((count, count), events[-1])
        self.assertEqual(sorted(done for done, _ in events), [done for done, _ in events])

    def test_without_listener_embeds_in_one_call(self) -> None:
        adapter, embedded = self._run(40)
        self.assertEqual([40], adapter.calls)
        self.assertEqual(40, len(embedded))

    def test_failing_display_callback_never_breaks_indexing(self) -> None:
        def boom(done: int, total: int) -> None:
            raise RuntimeError("display failed")

        adapter, embedded = self._run(20, boom)
        self.assertEqual(20, len(embedded))

    def test_listener_is_scoped_to_the_context(self) -> None:
        events: list[tuple[int, int]] = []
        with embedding_progress(lambda done, total: events.append((done, total))):
            pass
        adapter, _ = self._run(20)
        self.assertEqual([], events)
        self.assertEqual([20], adapter.calls)


if __name__ == "__main__":
    unittest.main()
