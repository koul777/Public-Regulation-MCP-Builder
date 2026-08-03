from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable
import unicodedata

from app.retrieval.tokenizer import (
    FALLBACK_TOKENIZER_MODEL,
    tokenize,
    tokenizer_name,
)


BM25_INDEX_VERSION = "reg-rag-bm25-index-v2"
BM25_RETRIEVAL_MODEL = "kiwi-bm25-v1"
DEFAULT_BM25_FILENAME = "bm25_index.json"
BM25_STRUCTURED_METADATA_VERSION = 3
_FAST_QUERY_TOKEN_RE = re.compile(r"[0-9A-Za-z\uac00-\ud7a3]+", re.UNICODE)
_FAST_QUERY_MAX_RAW_TOKENS = 64
_FAST_QUERY_MAX_TOKEN_LENGTH = 64
_FAST_QUERY_MAX_SUBTERM_LENGTH = 32

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
        return self.score_terms(
            tokenize(query, dedupe=False, tokenizer_model=self.tokenizer),
            allowed_ids=allowed_ids,
        )

    def score_fast_query(
        self,
        query: str,
        *,
        allowed_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """Score a bounded candidate query without initializing Kiwi.

        Serialized BM25 vocabulary terms recover useful Korean compound
        segments that the regex tokenizer cannot split on its own. This path
        is intended for a pre-authorized hierarchy candidate set; it never
        adds document identifiers outside ``allowed_ids``.
        """

        return self.score_terms(
            _fast_query_terms(query, self.document_frequencies),
            allowed_ids=allowed_ids,
        )

    def score_terms(
        self,
        query_terms: Iterable[str],
        *,
        allowed_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """Score already-tokenized terms using the serialized BM25 weights."""

        query_term_counts = Counter(
            normalized
            for term in query_terms
            if (
                normalized := unicodedata.normalize(
                    "NFC",
                    str(term or "").strip().lower(),
                )
            )
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


def _fast_query_terms(
    query: str,
    document_frequencies: dict[str, int],
) -> list[str]:
    """Build cold-start-safe query terms from regex and indexed vocabulary."""

    normalized_query = unicodedata.normalize("NFC", str(query or "")).lower()
    terms = tokenize(
        normalized_query,
        dedupe=False,
        tokenizer_model=FALLBACK_TOKENIZER_MODEL,
    )
    seen = set(terms)
    raw_tokens = _FAST_QUERY_TOKEN_RE.findall(normalized_query)
    for raw_token in raw_tokens[:_FAST_QUERY_MAX_RAW_TOKENS]:
        if len(raw_token) > _FAST_QUERY_MAX_TOKEN_LENGTH:
            continue
        for derived in _indexed_subterms(raw_token, document_frequencies):
            if derived in seen:
                continue
            seen.add(derived)
            terms.append(derived)
    return terms


def _indexed_subterms(
    raw_token: str,
    document_frequencies: dict[str, int],
) -> tuple[str, ...]:
    """Choose a deterministic, high-coverage segmentation from index terms."""

    token = str(raw_token or "").strip().lower()
    token_length = len(token)
    if token_length < 3:
        return ()

    # State: covered characters, skipped characters, first match offset, terms.
    states: list[tuple[int, int, int, tuple[str, ...]]] = [
        (0, 0, token_length, ()) for _ in range(token_length + 1)
    ]
    states[token_length] = (0, 0, token_length, ())
    for position in range(token_length - 1, -1, -1):
        suffix = states[position + 1]
        best = (
            suffix[0],
            suffix[1] + 1,
            min(token_length, suffix[2] + 1),
            suffix[3],
        )
        maximum_end = min(
            token_length,
            position + _FAST_QUERY_MAX_SUBTERM_LENGTH,
        )
        for end in range(maximum_end, position + 1, -1):
            if end - position < 2:
                break
            # The whole regex token is already emitted by the fallback
            # tokenizer. Proper substrings are the useful cold-start bridge.
            if position == 0 and end == token_length:
                continue
            candidate_term = token[position:end]
            if (
                not any(character.isalpha() for character in candidate_term)
                or int(document_frequencies.get(candidate_term) or 0) <= 0
            ):
                continue
            tail = states[end]
            candidate = (
                tail[0] + len(candidate_term),
                tail[1],
                0,
                (candidate_term, *tail[3]),
            )
            candidate_key = (
                candidate[0],
                -candidate[1],
                -candidate[2],
                len(candidate[3]),
            )
            best_key = (
                best[0],
                -best[1],
                -best[2],
                len(best[3]),
            )
            if candidate_key > best_key:
                best = candidate
        states[position] = best
    return states[0][3]


def default_bm25_index_path(vector_path: Path) -> Path:
    return vector_path.parent / DEFAULT_BM25_FILENAME


def write_bm25_index(path: Path, records: Iterable[dict[str, Any]]) -> Bm25Index:
    index = Bm25Index.build(records)
    _write_bm25_index(path, index)
    return index


def update_bm25_index_for_documents(
    path: Path,
    *,
    previous_records: Iterable[dict[str, Any]],
    final_records: Iterable[dict[str, Any]],
    changed_document_ids: Iterable[str],
) -> tuple[Bm25Index, bool]:
    """Update BM25 terms for changed documents without retokenizing the corpus.

    Returns ``(index, incremental)``. Any stale, incompatible, or ambiguous
    prior state falls back to a deterministic full rebuild.
    """

    previous = list(previous_records)
    final = list(final_records)
    changed_ids = {str(value or "").strip() for value in changed_document_ids if str(value or "").strip()}
    current = load_bm25_index(path)
    if (
        current is None
        or not changed_ids
        or current.structured_metadata_version != BM25_STRUCTURED_METADATA_VERSION
        or current.tokenizer != tokenizer_name()
        or current.is_stale_for(previous)
        or not _only_documents_changed(previous, final, changed_ids)
    ):
        rebuilt = Bm25Index.build(final)
        _write_bm25_index(path, rebuilt)
        return rebuilt, False

    final_ids = {str(record.get("id") or "") for record in final if str(record.get("id") or "")}
    retained_documents = [
        document
        for document in current.documents
        if str(document.get("id") or "") in final_ids
        and str(document.get("document_id") or "") not in changed_ids
    ]
    changed_index = Bm25Index.build(
        [
            record
            for record in final
            if _record_document_id(record) in changed_ids
        ],
        k1=current.k1,
        b=current.b,
    )
    documents = sorted(
        [*retained_documents, *changed_index.documents],
        key=lambda document: str(document.get("id") or ""),
    )
    document_frequencies: Counter[str] = Counter()
    total_length = 0
    for document in documents:
        terms = document.get("term_frequencies")
        if not isinstance(terms, dict):
            rebuilt = Bm25Index.build(final, k1=current.k1, b=current.b)
            _write_bm25_index(path, rebuilt)
            return rebuilt, False
        document_frequencies.update(str(term) for term in terms)
        total_length += int(document.get("document_length") or 0)
    updated = Bm25Index(
        index_version=BM25_INDEX_VERSION,
        structured_metadata_version=BM25_STRUCTURED_METADATA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        tokenizer=current.tokenizer,
        k1=current.k1,
        b=current.b,
        source_content_hashes=source_content_hashes(final),
        document_count=len(documents),
        average_document_length=round(total_length / len(documents), 6) if documents else 0.0,
        document_frequencies=dict(sorted(document_frequencies.items())),
        documents=documents,
    )
    _write_bm25_index(path, updated)
    return updated, True


def _write_bm25_index(path: Path, index: Bm25Index) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{hashlib.sha256(index.generated_at.encode('utf-8')).hexdigest()[:12]}.tmp")
    try:
        tmp_path.write_text(json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _only_documents_changed(
    previous_records: list[dict[str, Any]],
    final_records: list[dict[str, Any]],
    changed_document_ids: set[str],
) -> bool:
    previous_unchanged = {
        str(record.get("id") or ""): str(record.get("content_hash") or "")
        for record in previous_records
        if _record_document_id(record) not in changed_document_ids
    }
    final_unchanged = {
        str(record.get("id") or ""): str(record.get("content_hash") or "")
        for record in final_records
        if _record_document_id(record) not in changed_document_ids
    }
    return previous_unchanged == final_unchanged


def _record_document_id(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return str(record.get("document_id") or metadata.get("document_id") or "")


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
    canonical_hierarchy_path = _canonical_hierarchy_path(metadata)
    raw_hierarchy_path = str(metadata.get("hierarchy_path") or "").strip()
    canonicalized = bool(
        metadata.get("canonical_hierarchy_path")
        or metadata.get("chunker_version")
        or canonical_hierarchy_path != raw_hierarchy_path
    )
    counter: Counter[str] = Counter(
        tokenize(_canonical_record_text(record, canonical_hierarchy_path), dedupe=False)
    )
    for field in ("regulation_title", "article_title"):
        for token in tokenize(str(metadata.get(field) or "")):
            counter[token] += max(1, int(title_weight))
    for field, weight in _STRUCTURED_METADATA_FIELD_WEIGHTS:
        if field == "hierarchy_path":
            value = canonical_hierarchy_path
        elif canonicalized and field in {
            "part_no",
            "chapter_no",
            "section_no",
        }:
            # These fields may describe a binder catalog wrapper. The
            # regulation-local hierarchy already carries the true structure.
            continue
        else:
            value = metadata.get(field)
        _add_weighted_tokens(counter, value, weight)
    return counter


def _canonical_record_text(
    record: dict[str, Any],
    canonical_hierarchy_path: str,
) -> str:
    text = str(record.get("text") or "")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    regulation_title = str(
        metadata.get("canonical_regulation_title")
        or metadata.get("regulation_title")
        or ""
    ).strip()
    if regulation_title:
        text = re.sub(
            r"(?m)^\[문서명\][ \t]*.*$",
            f"[문서명] {regulation_title}",
            text,
            count=1,
        )
    if canonical_hierarchy_path:
        text = re.sub(
            r"(?m)^\[위치\][ \t]*.*$",
            f"[위치] {canonical_hierarchy_path}",
            text,
            count=1,
        )
    return text


def _canonical_hierarchy_path(metadata: dict[str, Any]) -> str:
    explicit = str(metadata.get("canonical_hierarchy_path") or "").strip()
    if explicit:
        return explicit
    raw_path = str(metadata.get("hierarchy_path") or "").strip()
    segments = [segment.strip() for segment in raw_path.split(">") if segment.strip()]
    title = str(metadata.get("regulation_title") or "").strip()
    regulation_no = str(metadata.get("regulation_no") or "").strip()
    title_key = _canonical_path_key(title)
    number_key = _canonical_number_key(regulation_no)
    # Prefer the regulation title. A short number (for example ``1``) can
    # otherwise attach the canonical path to a binder's ``제1편``/``제1장``.
    matched_index = next(
        (
            index
            for index, segment in enumerate(segments)
            if title_key and title_key in _canonical_path_key(segment)
        ),
        None,
    )
    if matched_index is None:
        matched_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if not _is_structural_path_segment(segment)
                and number_key
                and number_key in _canonical_number_key(segment)
            ),
            None,
        )
    if matched_index is not None:
        tail = segments[matched_index + 1 :]
    else:
        structural_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if re.match(
                    r"^(?:제\s*\d+(?:의\s*\d+)?\s*(?:편|장|절|관|조|항)|부칙|별표|별지|서식)",
                    segment,
                )
            ),
            len(segments),
        )
        tail = segments[structural_index:]
    canonical_segments = [title or regulation_no, *tail]
    return " > ".join(segment for segment in canonical_segments if segment)


def _canonical_path_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]", "", normalized)


def _canonical_number_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[‐-―−]", "-", normalized)
    return re.sub(r"[^0-9a-z가-힣./-]", "", normalized)


def _is_structural_path_segment(value: object) -> bool:
    marker = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))
    return bool(
        re.match(r"^제?\d+(?:의\d+)?(?:편|장|절|관|조|항|호)", marker)
        or re.match(r"^(?:부칙|별표|별지|서식)", marker)
    )


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
