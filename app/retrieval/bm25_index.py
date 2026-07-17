from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from app.retrieval.tokenizer import tokenize, tokenizer_name


BM25_INDEX_VERSION = "reg-rag-bm25-index-v1"
BM25_RETRIEVAL_MODEL = "kiwi-bm25-v1"
DEFAULT_BM25_FILENAME = "bm25_index.json"
BM25_STRUCTURED_METADATA_VERSION = 2

_STRUCTURED_METADATA_FIELD_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("article_no", 8),
    ("regulation_no", 6),
    ("chapter_no", 4),
    ("section_no", 4),
    ("part_no", 4),
    ("paragraph_no", 3),
    ("item_no", 3),
    ("hierarchy_path", 3),
    ("article_refs", 6),
    ("internal_regulation_refs", 4),
    ("regulation_article_refs", 4),
    ("appendix_refs", 6),
    ("form_refs", 6),
    ("external_law_refs", 3),
    ("references", 3),
    ("table_citation_label", 6),
    ("table_appendix_no", 5),
    ("table_appendix_title", 5),
    ("table_source", 2),
    ("table_geometry_source", 2),
    ("chunk_type", 1),
    ("table_like", 1),
    ("answer_intents", 1),
    ("answer_keywords", 1),
    ("answer_facts", 1),
)


@dataclass(frozen=True)
class Bm25Index:
    index_version: str
    structured_metadata_version: int
    generated_at: str
    tokenizer: str
    k1: float
    b: float
    source_content_hashes: str
    document_count: int
    average_document_length: float
    document_frequencies: dict[str, int]
    documents: list[dict[str, Any]]

    @classmethod
    def build(
        cls,
        records: Iterable[dict[str, Any]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        title_weight: int = 2,
    ) -> "Bm25Index":
        documents: list[dict[str, Any]] = []
        document_frequencies: Counter[str] = Counter()
        total_length = 0
        normalized_records = list(records)
        for record in normalized_records:
            term_frequencies = _weighted_term_frequencies(record, title_weight=title_weight)
            if not term_frequencies:
                continue
            document_terms = dict(sorted(term_frequencies.items()))
            for token in document_terms:
                document_frequencies[token] += 1
            document_length = sum(int(value) for value in document_terms.values())
            total_length += document_length
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            documents.append(
                {
                    "id": str(record.get("id") or ""),
                    "document_id": str(record.get("document_id") or metadata.get("document_id") or ""),
                    "chunk_id": str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
                    "content_hash": str(record.get("content_hash") or ""),
                    "document_length": document_length,
                    "term_frequencies": document_terms,
                }
            )
        average_length = total_length / len(documents) if documents else 0.0
        return cls(
            index_version=BM25_INDEX_VERSION,
            structured_metadata_version=BM25_STRUCTURED_METADATA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            tokenizer=tokenizer_name(),
            k1=k1,
            b=b,
            source_content_hashes=source_content_hashes(normalized_records),
            document_count=len(documents),
            average_document_length=round(average_length, 6),
            document_frequencies=dict(sorted(document_frequencies.items())),
            documents=documents,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Bm25Index":
        if payload.get("index_version") != BM25_INDEX_VERSION:
            raise ValueError(f"Unsupported BM25 index_version: {payload.get('index_version')}")
        documents = payload.get("documents")
        document_frequencies = payload.get("document_frequencies")
        if not isinstance(documents, list) or not isinstance(document_frequencies, dict):
            raise ValueError("BM25 index is missing documents or document_frequencies.")
        return cls(
            index_version=str(payload["index_version"]),
            structured_metadata_version=int(payload.get("structured_metadata_version") or 1),
            generated_at=str(payload.get("generated_at") or ""),
            tokenizer=str(payload.get("tokenizer") or ""),
            k1=float(payload.get("k1", 1.5)),
            b=float(payload.get("b", 0.75)),
            source_content_hashes=str(payload.get("source_content_hashes") or ""),
            document_count=int(payload.get("document_count") or len(documents)),
            average_document_length=float(payload.get("average_document_length") or 0.0),
            document_frequencies={str(key): int(value) for key, value in document_frequencies.items()},
            documents=[item for item in documents if isinstance(item, dict)],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_version": self.index_version,
            "structured_metadata_version": self.structured_metadata_version,
            "generated_at": self.generated_at,
            "tokenizer": self.tokenizer,
            "retrieval_model": BM25_RETRIEVAL_MODEL,
            "k1": self.k1,
            "b": self.b,
            "source_content_hashes": self.source_content_hashes,
            "document_count": self.document_count,
            "average_document_length": self.average_document_length,
            "document_frequencies": self.document_frequencies,
            "documents": self.documents,
        }

    def is_stale_for(self, records: Iterable[dict[str, Any]]) -> bool:
        return self.source_content_hashes != source_content_hashes(records)

    def score(self, query: str, *, allowed_ids: set[str] | None = None) -> dict[str, float]:
        query_term_counts = Counter(
            tokenize(query, dedupe=False, tokenizer_model=self.tokenizer)
        )
        if not query_term_counts or not self.documents:
            return {}
        scores: dict[str, float] = {}
        avg_len = self.average_document_length or 1.0
        corpus_size = max(self.document_count, 1)
        idf_by_term: dict[str, float] = {}
        for term in query_term_counts:
            df = int(self.document_frequencies.get(term) or 0)
            if df > 0:
                idf_by_term[term] = math.log(1.0 + ((corpus_size - df + 0.5) / (df + 0.5)))
        if not idf_by_term:
            return {}
        for document in self.documents:
            record_id = str(document.get("id") or "")
            if allowed_ids is not None and record_id not in allowed_ids:
                continue
            term_frequencies = document.get("term_frequencies")
            if not record_id or not isinstance(term_frequencies, dict):
                continue
            doc_len = float(document.get("document_length") or 0.0)
            score = 0.0
            for term, query_count in query_term_counts.items():
                tf = float(term_frequencies.get(term) or 0.0)
                if tf <= 0.0:
                    continue
                idf = idf_by_term.get(term)
                if idf is None:
                    continue
                denominator = tf + self.k1 * (1.0 - self.b + self.b * (doc_len / avg_len))
                if denominator:
                    score += query_count * idf * ((tf * (self.k1 + 1.0)) / denominator)
            if score > 0.0:
                scores[record_id] = round(score, 8)
        return scores


def default_bm25_index_path(vector_path: Path) -> Path:
    return vector_path.parent / DEFAULT_BM25_FILENAME


def write_bm25_index(path: Path, records: Iterable[dict[str, Any]]) -> Bm25Index:
    index = Bm25Index.build(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{hashlib.sha256(index.generated_at.encode('utf-8')).hexdigest()[:12]}.tmp")
    try:
        tmp_path.write_text(json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return index


def load_bm25_index(path: Path) -> Bm25Index | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return Bm25Index.from_dict(payload)
    except (TypeError, ValueError):
        return None


def source_content_hashes(records: Iterable[dict[str, Any]]) -> str:
    hashes = sorted(str(record.get("content_hash") or "") for record in records if str(record.get("content_hash") or ""))
    payload = json.dumps(hashes, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _weighted_term_frequencies(record: dict[str, Any], *, title_weight: int) -> Counter[str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    counter: Counter[str] = Counter(tokenize(str(record.get("text") or "")))
    for field in ("regulation_title", "article_title"):
        for token in tokenize(str(metadata.get(field) or "")):
            counter[token] += max(1, int(title_weight))
    for field, weight in _STRUCTURED_METADATA_FIELD_WEIGHTS:
        _add_weighted_tokens(counter, metadata.get(field), weight)
    return counter


def _add_weighted_tokens(counter: Counter[str], value: Any, weight: int) -> None:
    if weight <= 0:
        return
    for token in _tokenizable_values(value):
        for item in tokenize(str(token or "")):
            counter[item] += weight


def _tokenizable_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]
