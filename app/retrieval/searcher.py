from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
import re
from typing import Any
import unicodedata

from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL, local_hash_embedding
from app.agents.model_router import QWEN3_EMBEDDING_MODEL
from app.retrieval.bm25_index import BM25_RETRIEVAL_MODEL, Bm25Index
from app.retrieval.semantic_models import Qwen3EmbeddingAdapter, cosine_similarity, semantic_runtime_available
from app.retrieval.tokenizer import tokenize


LEXICAL_FALLBACK_MODEL = "token-lexical-fallback-v1"
_APPENDIX_FORM_MARKERS = ("\ubcc4\ud45c", "\ubcc4\uc9c0", "\uc11c\uc2dd")
_NAMED_REGULATION_PATTERN = re.compile(
    r"(?<![0-9A-Za-z\uac00-\ud7a3])"
    r"[0-9A-Za-z\uac00-\ud7a3]{2,}(?:\uaddc\uc815|\uaddc\uce59|\uc138\uce59|\uc9c0\uce68|\uc694\ub839|\ub0b4\uaddc)"
)
_NUMBERED_APPENDIX_FORM_PATTERN = re.compile(
    r"(?:\ubcc4\ud45c|\ubcc4\uc9c0)\s*(?:\uc81c\s*)?\d+(?:\s*[-\uc758]\s*\d+)*(?:\s*\ud638)?"
    r"(?:\s*\uc11c\uc2dd)?"
)
_UNNUMBERED_ATTACHMENT_PATTERN = re.compile(
    r"(?<![0-9A-Za-z\uac00-\ud7a3])"
    r"(?P<marker>\ubcc4\ud45c|\ubcc4\uc9c0|\uc11c\uc2dd)"
    r"(?:\uc5d0\uc11c\ub294|\uc73c\ub85c\ub294|\uc5d0\ub294|\uc73c\ub85c|\uc5d0\uc11c|"
    r"\uc740|\ub294|\uc774|\uac00|\uc744|\ub97c|\uc758|\uc640|\uacfc|\uc5d0|\ub85c|\ub3c4|\ub9cc)?"
    r"(?![0-9A-Za-z\uac00-\ud7a3])"
)
_STRUCTURAL_LOCATOR_PATTERN = re.compile(
    r"(?:\ubcc4\ud45c|\ubcc4\uc9c0)\s*(?:\uc81c\s*)?\d+(?:\s*[-\uc758]\s*\d+)*(?:\s*\ud638)?"
    r"(?:\s*\uc11c\uc2dd)?"
    r"|\uc81c\s*\d+(?:\s*\uc758\s*\d+)?\s*(?:\ud3b8|\uc7a5|\uc808|\uc870|\ud56d|\ud638|\ubaa9)"
)
_DIRECT_QUERY_LOCATOR_FIELDS = (
    "article_no",
    "chapter_no",
    "section_no",
    "part_no",
    "paragraph_no",
    "item_no",
    "table_appendix_no",
)
_REFERENCE_QUERY_LOCATOR_FIELDS = (
    "appendix_refs",
    "form_refs",
)
_DIRECT_QUERY_TITLE_FIELDS = (
    "article_title",
    "direct_article_title",
    "table_appendix_title",
)
_GENERIC_APPENDIX_FORM_QUERY_TOKENS = frozenset(
    {
        "\uac1c\uc815",
        "\uacbd\uc6b0",
        "\uad00\ub9ac",
        "\uad00\ub9ac\uaddc\uc815",
        "\uaddc\uc815",
        "\uadfc\uac70",
        "\ub0b4\uc6a9",
        "\ubc29\uc2dd",
        "\ubcc4\ud45c",
        "\ubcc4\uc9c0",
        "\uc11c\uc2dd",
        "\uc2dc\ud589",
        "\uc138\uce59",
        "\uc5b4\ub5bb",
        "\uc6d0\uaddc",
        "\uc791\uc131",
        "\uc804\ubd80",
        "\uc815\ud558",
        "\uc81c18\uc870",
        "\ud544\uc694",
        "\ud655\uc778",
        "\ud615\uc2dd",
    }
)


@dataclass
class _StructuredQueryContext:
    normalized_query: str
    compact_query: str
    matching_titles: frozenset[str]
    has_locator_intent: bool
    unnumbered_attachment_markers: frozenset[str] = frozenset()
    normalized_value_cache: dict[str, str] = field(default_factory=dict)
    locator_match_cache: dict[str, bool] = field(default_factory=dict)


def search(
    query: str,
    records: list[dict[str, Any]],
    index: Bm25Index | None,
    top_k: int,
    *,
    index_records: list[dict[str, Any]] | None = None,
    index_source_content_hashes: str | None = None,
    prefer_semantic: bool = True,
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    candidate_context = _build_structured_query_context(
        query,
        [(0.0, record) for record in records],
    )
    expanded_query = _expand_regulation_query(
        query,
        has_named_candidate=bool(candidate_context.matching_titles),
    )
    stale_source = records if index_records is None else index_records
    stale_index = (
        index is None
        or (
            index.source_content_hashes != index_source_content_hashes
            if index_source_content_hashes is not None
            else index.is_stale_for(stale_source)
        )
    )
    if index is not None and not stale_index:
        semantic_failure_reason = ""
        if prefer_semantic and _has_usable_embeddings(records):
            try:
                scored = _hybrid_bm25_hash_search(
                    expanded_query,
                    records,
                    index,
                    structured_context=candidate_context,
                )
            except Exception as exc:
                scored = []
                semantic_failure_reason = _semantic_failure_reason(exc)
            if scored:
                scored, definition_metadata = _promote_enumeration_definitions(query, scored, records)
                semantic_model = _record_embedding_model(records)
                return scored[:top_k], {
                    "retrieval_model": (
                        "hybrid-bm25-qwen3-v1"
                        if semantic_model == QWEN3_EMBEDDING_MODEL
                        else "hybrid-bm25-hash-v1"
                    ),
                    "semantic_embedding_model": semantic_model,
                    "retrieval_fallback": False,
                    "bm25_index_status": "ready",
                    "query_expanded": expanded_query != query,
                    "hybrid_keyword_weight": 0.65,
                    "hybrid_vector_weight": 0.35,
                    **definition_metadata,
                }
        scored = _bm25_search(expanded_query, records, index)
        scored = _apply_query_boosts(query, scored, structured_context=candidate_context)
        if not scored:
            literal_scored = _literal_substring_search(expanded_query, records)
            if literal_scored:
                return literal_scored[:top_k], {
                    "retrieval_model": LEXICAL_FALLBACK_MODEL,
                    "retrieval_fallback": True,
                    "bm25_index_status": "ready_bm25_no_hits_literal_fallback",
                    "query_expanded": expanded_query != query,
                }
        scored, definition_metadata = _promote_enumeration_definitions(query, scored, records)
        return scored[:top_k], {
            "retrieval_model": BM25_RETRIEVAL_MODEL,
            "retrieval_fallback": bool(semantic_failure_reason),
            "bm25_index_status": (
                "ready_semantic_query_fallback" if semantic_failure_reason else "ready"
            ),
            "query_expanded": expanded_query != query,
            **(
                {
                    "semantic_embedding_model": _record_embedding_model(records),
                    "semantic_query_status": "degraded",
                    "semantic_fallback_reason": semantic_failure_reason,
                }
                if semantic_failure_reason
                else {}
            ),
            **definition_metadata,
        }
    fallback_reason = "missing_bm25_index" if index is None else "stale_bm25_index"
    semantic_failure_reason = ""
    vector_scored: list[tuple[float, dict[str, Any]]] = []
    if prefer_semantic:
        try:
            vector_scored = _hash_embedding_search(expanded_query, records)
        except Exception as exc:
            semantic_failure_reason = _semantic_failure_reason(exc)
    if vector_scored:
        vector_scored = _apply_query_boosts(
            query,
            vector_scored,
            structured_context=candidate_context,
        )
        vector_scored, definition_metadata = _promote_enumeration_definitions(query, vector_scored, records)
        semantic_model = _record_embedding_model(records)
        return vector_scored[:top_k], {
            "retrieval_model": semantic_model or LOCAL_HASH_EMBEDDING_MODEL,
            "retrieval_fallback": True,
            "bm25_index_status": fallback_reason,
            "query_expanded": expanded_query != query,
            **definition_metadata,
        }
    lexical_scored = _apply_query_boosts(
        query,
        _lexical_search(expanded_query, records),
        structured_context=candidate_context,
    )
    lexical_scored, definition_metadata = _promote_enumeration_definitions(query, lexical_scored, records)
    return lexical_scored[:top_k], {
        "retrieval_model": LEXICAL_FALLBACK_MODEL,
        "retrieval_fallback": True,
        "bm25_index_status": (
            f"{fallback_reason}_semantic_query_fallback"
            if semantic_failure_reason
            else fallback_reason
        ),
        "query_expanded": expanded_query != query,
        **(
            {
                "semantic_embedding_model": _record_embedding_model(records),
                "semantic_query_status": "degraded",
                "semantic_fallback_reason": semantic_failure_reason,
            }
            if semantic_failure_reason
            else {}
        ),
        **definition_metadata,
    }


def _has_usable_embeddings(records: list[dict[str, Any]]) -> bool:
    dimensions: int | None = None
    for record in records:
        embedding = record.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            continue
        if not all(isinstance(value, (int, float)) for value in embedding):
            continue
        current_dimensions = len(embedding)
        if dimensions is None:
            dimensions = current_dimensions
        if current_dimensions == dimensions:
            return True
    return False


def _hybrid_bm25_hash_search(
    query: str,
    records: list[dict[str, Any]],
    index: Bm25Index,
    *,
    structured_context: _StructuredQueryContext,
) -> list[tuple[float, dict[str, Any]]]:
    """Fuse lexical and local-vector candidates without expanding visibility.

    Candidate visibility is already established by the caller. Reciprocal rank
    fusion is used instead of raw-score addition because BM25 and cosine-like
    hash scores are on different scales. The final structured boosts still run
    after fusion so exact article/table locators remain authoritative.
    """

    keyword_scored = _apply_query_boosts(
        query,
        _bm25_search(query, records, index),
        structured_context=structured_context,
    )
    vector_scored = _apply_query_boosts(
        query,
        _hash_embedding_search(query, records),
        structured_context=structured_context,
    )
    by_id = {str(record.get("id") or ""): record for record in records}
    fused: dict[str, float] = {}
    rank_constant = 60.0
    for rank, (_score, record) in enumerate(keyword_scored, start=1):
        record_id = str(record.get("id") or "")
        if record_id in by_id:
            fused[record_id] = fused.get(record_id, 0.0) + 0.65 / (rank_constant + rank)
    for rank, (_score, record) in enumerate(vector_scored, start=1):
        record_id = str(record.get("id") or "")
        if record_id in by_id:
            fused[record_id] = fused.get(record_id, 0.0) + 0.35 / (rank_constant + rank)
    return sorted(
        [(round(score, 8), by_id[record_id]) for record_id, score in fused.items()],
        key=lambda item: item[0],
        reverse=True,
    )


def _bm25_search(query: str, records: list[dict[str, Any]], index: Bm25Index) -> list[tuple[float, dict[str, Any]]]:
    records_by_id = {str(record.get("id") or ""): record for record in records}
    if not records_by_id:
        return []
    scores = index.score(query, allowed_ids=set(records_by_id))
    scored: list[tuple[float, dict[str, Any]]] = []
    for record_id, score in scores.items():
        record = records_by_id.get(record_id)
        if record is not None:
            scored.append((score, record))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def _hash_embedding_search(query: str, records: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    query_embedding_cache: dict[tuple[str, int], list[float]] = {}
    for record in records:
        embedding = record.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            continue
        dimensions = len(embedding)
        model = str(record.get("embedding_model") or LOCAL_HASH_EMBEDDING_MODEL)
        cache_key = (model, dimensions)
        query_embedding = query_embedding_cache.get(cache_key)
        if query_embedding is None:
            if model == QWEN3_EMBEDDING_MODEL:
                if not semantic_runtime_available():
                    raise RuntimeError("semantic_runtime_unavailable")
                query_embedding = _semantic_query_adapter(dimensions).encode_queries([query])[0]
            else:
                query_embedding = local_hash_embedding(query, dimensions=dimensions)
            query_embedding_cache[cache_key] = query_embedding
        score = cosine_similarity(query_embedding, [float(value) for value in embedding])
        scored.append((score, record))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def _semantic_failure_reason(exc: Exception) -> str:
    if str(exc) == "semantic_runtime_unavailable":
        return "semantic_runtime_unavailable"
    return f"semantic_query_{type(exc).__name__}"[:120]


def _record_embedding_model(records: list[dict[str, Any]]) -> str:
    models = {
        str(record.get("embedding_model") or "").strip()
        for record in records
        if isinstance(record.get("embedding"), list) and record.get("embedding")
    } - {""}
    if len(models) == 1:
        return next(iter(models))
    return LOCAL_HASH_EMBEDDING_MODEL if not models else "mixed-local-embeddings"


@lru_cache(maxsize=4)
def _semantic_query_adapter(dimensions: int) -> Qwen3EmbeddingAdapter:
    return Qwen3EmbeddingAdapter(
        device="cpu",
        truncate_dim=dimensions,
        local_files_only=True,
    )


def _lexical_search(query: str, records: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    query_terms = tokenize(query, prefer_regex_if_kiwi_cold=True)
    if not query_terms:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        score = _lexical_score_record(query_terms, record)
        if score > 0.0:
            scored.append((score, record))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def _lexical_score_record(query_terms: list[str], record: dict[str, Any]) -> float:
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    field_token_counts: Counter[str] = Counter()
    for value, weight in (
        (record.get("text"), 1),
        (metadata.get("regulation_title"), 2),
        (metadata.get("article_title"), 2),
        (metadata.get("article_no"), 2),
        (metadata.get("document_name"), 1),
    ):
        tokens = tokenize(str(value or ""), prefer_regex_if_kiwi_cold=True)
        for token in tokens:
            field_token_counts[token] += weight
    if not field_token_counts:
        return 0.0
    score = 0.0
    for term in query_terms:
        count = field_token_counts.get(term, 0)
        if count:
            score += min(count, 5)
    return round(score, 8)


def _apply_query_boosts(
    query: str,
    scored: list[tuple[float, dict[str, Any]]],
    *,
    structured_context: _StructuredQueryContext | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    if not scored:
        return scored
    compact_query = unicodedata.normalize("NFKC", str(query or "")).replace(" ", "")
    if structured_context is None:
        structured_context = _build_structured_query_context(query, scored)
    generic_appendix_form_query = (
        _is_appendix_form_query(compact_query)
        and _is_generic_appendix_form_query(query)
        and not structured_context.matching_titles
    )
    boosted = [
        (
            score
            + _record_query_boost(
                query,
                record,
                compact_query=compact_query,
                generic_appendix_form_query=generic_appendix_form_query,
                structured_context=structured_context,
            ),
            record,
        )
        for score, record in scored
    ]
    return sorted(boosted, key=lambda item: item[0], reverse=True)


def rerank_bm25_candidates(
    query: str,
    scored: list[tuple[float, dict[str, Any]]],
    index: Bm25Index,
) -> list[tuple[float, dict[str, Any]]]:
    """Rerank a pre-authorized candidate set with a verified BM25 index.

    The caller remains responsible for tenant, approval, and ACL filtering.
    Only identifiers already present in ``scored`` are admitted, so the full
    index can improve ranking without expanding the visible candidate set.
    """

    if not scored:
        return []
    allowed_ids = {
        str(record.get("id") or "").strip()
        for _score, record in scored
        if str(record.get("id") or "").strip()
    }
    if not allowed_ids:
        return _apply_query_boosts(query, scored)
    bm25_scores = index.score_fast_query(query, allowed_ids=allowed_ids)
    fused = [
        (
            float(base_score)
            + float(bm25_scores.get(str(record.get("id") or "").strip(), 0.0)),
            record,
        )
        for base_score, record in scored
    ]
    return _apply_query_boosts(query, fused)


def _promote_enumeration_definitions(
    query: str,
    scored: list[tuple[float, dict[str, Any]]],
    records: list[dict[str, Any]],
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    compact_query = unicodedata.normalize("NFKC", str(query or "")).replace(" ", "")
    if "종류" not in compact_query or not scored:
        return scored, {}

    enumerated_terms: list[str] = []
    for _, record in scored[:3]:
        enumerated_terms.extend(_enumerated_terms(str(record.get("text") or "")))
    enumerated_terms = list(dict.fromkeys(enumerated_terms))
    if not enumerated_terms:
        return scored, {}

    boosted = list(scored)
    index_by_id = {id(record): index for index, (_, record) in enumerate(scored)}
    promoted_terms: list[str] = []
    for term in enumerated_terms:
        definition = _definition_record_for_term(term, records)
        if definition is None:
            continue
        promoted_terms.append(term)
        position = index_by_id.get(id(definition))
        if position is not None:
            score, record = boosted[position]
            boosted[position] = (score + 24.0, record)
        else:
            boosted.append((24.0, definition))
            index_by_id[id(definition)] = len(boosted) - 1

    if not promoted_terms:
        return scored, {}
    return sorted(boosted, key=lambda item: item[0], reverse=True), {
        "enumeration_definition_terms": promoted_terms
    }


def _enumerated_terms(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9가-힣]+", str(text or ""))
    stopwords = {
        "교직원",
        "휴가",
        "휴가는",
        "종류",
        "구분",
        "구분한다",
        "및",
        "그리고",
        "등",
        "하는",
        "한다",
        "위해",
        "사용",
        "제",
        "조",
        "항",
    }
    normalized_terms: list[str] = []
    for token in tokens:
        normalized = _normalize_enumerated_term(token)
        if len(normalized) > 1 and normalized not in stopwords and normalized not in normalized_terms:
            normalized_terms.append(normalized)
    return normalized_terms


def _normalize_enumerated_term(token: str) -> str:
    normalized = str(token or "").strip()
    if len(normalized) <= 2:
        return normalized
    for suffix in (
        "으로",
        "로",
        "에게",
        "에서",
        "까지",
        "부터",
        "만",
        "도",
        "은",
        "는",
        "을",
        "를",
        "의",
        "과",
        "와",
        "이라",
        "이다",
    ):
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _definition_record_for_term(term: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        article_title = str(metadata.get("article_title") or "").strip()
        text = str(record.get("text") or "")
        if term == article_title or term in article_title:
            return record
        if term in text[:200]:
            return record
    return None


def _literal_substring_search(query: str, records: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    normalized_query = " ".join(
        unicodedata.normalize("NFKC", str(query or "")).split()
    ).lower()
    compact_query = normalized_query.replace(" ", "")
    terms = [term for term in normalized_query.split() if len(term) >= 2]
    if not compact_query and not terms:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        blob = unicodedata.normalize(
            "NFKC",
            " ".join(
                str(value or "")
                for value in (
                    record.get("text"),
                    metadata.get("regulation_title"),
                    metadata.get("article_title"),
                    metadata.get("article_no"),
                    metadata.get("document_name"),
                )
            ),
        ).lower()
        compact_blob = blob.replace(" ", "")
        score = 0.0
        if compact_query and compact_query in compact_blob:
            score += 10.0
        score += sum(2.0 for term in terms if term in blob or term.replace(" ", "") in compact_blob)
        if score > 0.0:
            scored.append((round(score, 8), record))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def _record_query_boost(
    query: str,
    record: dict[str, Any],
    *,
    compact_query: str | None = None,
    generic_appendix_form_query: bool | None = None,
    structured_context: _StructuredQueryContext | None = None,
) -> float:
    compact = (
        unicodedata.normalize("NFKC", str(query or "")).replace(" ", "")
        if compact_query is None
        else compact_query
    )
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    blob = unicodedata.normalize(
        "NFKC",
        " ".join(
            str(value or "")
            for value in (
                record.get("text"),
                metadata.get("regulation_title"),
                metadata.get("article_no"),
                metadata.get("article_title"),
                metadata.get("hierarchy_path"),
            )
        ),
    )
    boost = _structured_query_boost(
        query,
        metadata,
        context=structured_context,
    )
    if "육아휴직" in compact:
        if "제29조" in blob and ("만 8세" in blob or "초등학교 2학년" in blob or "자녀를 양육" in blob):
            boost += 18.0
        if "제30조" in blob and ("자녀 1명" in blob or "3년 이내" in blob or "휴직 기간" in blob):
            boost += 18.0
        if "제33조" in blob and ("육아휴직수당" in blob or "기본연봉월액" in blob):
            boost += 18.0
        if "육아휴직수당" in blob and ("78퍼센트" in blob or "62.4퍼센트" in blob):
            boost += 12.0
        if "시간선택제" in blob and "제7조" in blob and "육아휴직수당" not in blob:
            boost -= 6.0
    if _is_leave_foreign_travel_report_query(compact):
        if "제29조의3" in blob and "휴직자의 복무실태 점검" in blob:
            boost += 36.0
        if "별지 제16호서식" in blob and "휴직자 국외 출국 신고서" in blob:
            boost += 16.0
        if "금품등의 인도 및 처리" in blob:
            boost -= 10.0
    performance_pay_exclusion_query = "성과연봉" in compact and any(
        term in compact
        for term in ("제외", "제한", "지급대상", "대상제외", "못받", "미지급", "지급하지", "중징계", "징계")
    )
    if performance_pay_exclusion_query:
        if "제27조의2" in blob and "성과연봉 지급대상 제외" in blob:
            boost += 34.0
        if any(term in blob for term in ("중징계", "성폭력", "성매매", "성희롱", "음주운전", "음주측정")):
            boost += 10.0
        if "제24조" in blob and ("연봉의 지급 방법" in blob or "6월" in blob or "12월" in blob or "일시금" in blob):
            boost -= 14.0
    if "교원인사위원회" in compact and "심의" in compact:
        if "제8조" in blob and "위원회 기능" in blob and "교원 인사위원회" in blob:
            boost += 24.0
        if "교원업적평가 규정" in blob or "별지제3호서식" in blob:
            boost -= 8.0
    faculty_hiring_query = (
        ("전임" in compact and "교원" in compact and any(term in compact for term in ("채용", "임용", "절차")))
        or ("교원" in compact and "임용" in compact and "절차" in compact)
    )
    if faculty_hiring_query:
        if "교원 임용 세칙" in blob and (
            "신규임용 후보자 심사" in blob or "지원 마감일" in blob or "공개발표심사" in blob
        ):
            boost += 22.0
        if "제38조" in blob and "전임 교원" in blob and "교수, 부교수, 조교수" in blob:
            boost += 42.0
        if (
            "강사" in blob
            or "비전임교원" in blob
            or "연구직임용세칙" in blob
            or "초빙교수채용규정" in blob
            or "객원교수채용규정" in blob
            or "비정규직 인사관리 규정" in blob
        ):
            boost -= 10.0
    if _is_appendix_form_query(compact):
        is_generic = (
            _is_generic_appendix_form_query(query)
            if generic_appendix_form_query is None
            else generic_appendix_form_query
        )
        if is_generic:
            if "제18조" in blob and "별표와 별지 서식" in blob:
                boost += 28.0
            if "원규관리규정 시행세칙" in blob and ("별표 또는 별지 서식" in blob or "작성방식" in blob):
                boost += 16.0
        if "지급근거" in blob or "손망실" in blob or "개인정보" in blob or "가스안전" in blob:
            boost -= 8.0
    return boost


def _structured_query_boost(
    query: str,
    metadata: dict[str, Any],
    *,
    context: _StructuredQueryContext | None = None,
) -> float:
    """Prefer an explicitly named regulation and its exact structural locator.

    The boost is applied only after the caller has already limited records to
    the visible candidate set.  It therefore changes ranking, never approval,
    tenant, or ACL eligibility.
    """

    if context is None:
        context = _build_structured_query_context(
            query,
            [(0.0, {"metadata": metadata})],
        )
    if not context.matching_titles and not context.has_locator_intent:
        return 0.0

    regulation_title = str(metadata.get("regulation_title") or "").strip()
    normalized_title = _cached_compact_match_text(context, regulation_title)
    title_match = normalized_title in context.matching_titles
    direct_locator_match = _metadata_locator_match(
        metadata,
        _DIRECT_QUERY_LOCATOR_FIELDS,
        context,
    )
    direct_title_match = _metadata_text_match(
        metadata,
        _DIRECT_QUERY_TITLE_FIELDS,
        context,
    )
    reference_locator_match = _metadata_locator_match(
        metadata,
        _REFERENCE_QUERY_LOCATOR_FIELDS,
        context,
    )
    locator_match = direct_locator_match or reference_locator_match
    appendix_form_intent = _is_appendix_form_query(context.compact_query)
    attachment_chunk = _is_attachment_chunk(metadata)
    unnumbered_attachment_match = (
        title_match
        and attachment_chunk
        and bool(context.unnumbered_attachment_markers)
        and _attachment_markers_match(
            metadata,
            context.unnumbered_attachment_markers,
        )
    )

    boost = 0.0
    if title_match:
        boost += min(18.0, 6.0 + float(len(normalized_title)))
    if direct_title_match:
        boost += 14.0 if direct_locator_match else 8.0
    if direct_locator_match:
        boost += 2.0 if appendix_form_intent and not attachment_chunk else 16.0
    if reference_locator_match:
        boost += 22.0 if appendix_form_intent and attachment_chunk else 8.0
    if appendix_form_intent and attachment_chunk and reference_locator_match:
        boost += 8.0
    if unnumbered_attachment_match:
        boost += 22.0
    if title_match and (locator_match or direct_title_match):
        boost += 8.0
    if title_match and unnumbered_attachment_match:
        boost += 8.0
    # Keep the boost bounded, but leave enough headroom for an exact article
    # title to remain distinguishable from a sibling that shares the same
    # regulation title and article number.  A lower cap collapsed both cases
    # to nearly the same score and let a small lexical lead outrank the
    # explicitly named provision.
    return min(boost, 64.0)


def _build_structured_query_context(
    query: str,
    scored: list[tuple[float, dict[str, Any]]],
) -> _StructuredQueryContext:
    normalized_query = unicodedata.normalize("NFKC", str(query or "")).strip().lower()
    compact_query = _compact_match_text(normalized_query)
    has_named_regulation = _NAMED_REGULATION_PATTERN.search(normalized_query) is not None
    has_locator_intent = _STRUCTURAL_LOCATOR_PATTERN.search(normalized_query) is not None
    unnumbered_attachment_markers = _unnumbered_attachment_markers(
        normalized_query
    )
    if not compact_query:
        return _StructuredQueryContext(
            normalized_query=normalized_query,
            compact_query=compact_query,
            matching_titles=frozenset(),
            has_locator_intent=False,
            unnumbered_attachment_markers=frozenset(),
        )

    title_spans: dict[str, list[tuple[int, int]]] = {}
    normalized_title_cache: dict[str, str] = {}
    for _score, record in scored:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        raw_title = str(metadata.get("regulation_title") or "")
        normalized_title = normalized_title_cache.get(raw_title)
        if normalized_title is None:
            normalized_title = _compact_match_text(raw_title)
            normalized_title_cache[raw_title] = normalized_title
        if len(normalized_title) < 4 or normalized_title in title_spans:
            continue
        if normalized_title not in compact_query:
            continue
        spans = _query_named_title_spans(normalized_query, normalized_title)
        if spans:
            title_spans[normalized_title] = spans

    matching_titles = frozenset(
        title
        for title, spans in title_spans.items()
        if not _title_matches_only_inside_longer_title(title, spans, title_spans)
    )
    if (
        not has_named_regulation
        and not matching_titles
        and not has_locator_intent
    ):
        unnumbered_attachment_markers = frozenset()
    return _StructuredQueryContext(
        normalized_query=normalized_query,
        compact_query=compact_query,
        matching_titles=matching_titles,
        has_locator_intent=has_locator_intent,
        unnumbered_attachment_markers=unnumbered_attachment_markers,
    )


def _title_matches_only_inside_longer_title(
    title: str,
    spans: list[tuple[int, int]],
    title_spans: dict[str, list[tuple[int, int]]],
) -> bool:
    for longer_title, longer_spans in title_spans.items():
        if title == longer_title or title not in longer_title:
            continue
        if all(
            any(longer_start <= start and end <= longer_end for longer_start, longer_end in longer_spans)
            for start, end in spans
        ):
            return True
    return False


def _metadata_locator_match(
    metadata: dict[str, Any],
    fields: tuple[str, ...],
    context: _StructuredQueryContext,
) -> bool:
    if not context.has_locator_intent:
        return False
    for field_name in fields:
        for value in _metadata_values(metadata.get(field_name)):
            locator = _cached_compact_match_text(context, value)
            matched = context.locator_match_cache.get(locator)
            if matched is None:
                matched = _query_contains_locator(context.compact_query, locator)
                context.locator_match_cache[locator] = matched
            if matched:
                return True
    return False


def _metadata_text_match(
    metadata: dict[str, Any],
    fields: tuple[str, ...],
    context: _StructuredQueryContext,
) -> bool:
    for field_name in fields:
        for value in _metadata_values(metadata.get(field_name)):
            compact_value = _cached_compact_match_text(context, value)
            if len(compact_value) >= 3 and compact_value in context.compact_query:
                return True
    return False


def _cached_compact_match_text(
    context: _StructuredQueryContext,
    value: Any,
) -> str:
    raw_value = str(value or "")
    cached = context.normalized_value_cache.get(raw_value)
    if cached is None:
        cached = _compact_match_text(raw_value)
        context.normalized_value_cache[raw_value] = cached
    return cached


def _compact_match_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[^0-9A-Za-z\uac00-\ud7a3]+", "", normalized).lower()


def _query_contains_named_title(query: str, compact_title: str) -> bool:
    return bool(_query_named_title_spans(query, compact_title))


def _query_named_title_spans(
    query: str,
    compact_title: str,
) -> list[tuple[int, int]]:
    if not compact_title:
        return []
    separator = r"[^0-9A-Za-z\uac00-\ud7a3]*"
    title_pattern = separator.join(re.escape(character) for character in compact_title)
    normalized_query = unicodedata.normalize("NFKC", str(query or "")).lower()
    return [
        match.span()
        for match in re.finditer(
            rf"(?<![0-9A-Za-z\uac00-\ud7a3]){title_pattern}",
            normalized_query,
        )
    ]


def _query_contains_locator(compact_query: str, compact_locator: str) -> bool:
    if len(compact_locator) < 3 or not any(character.isdigit() for character in compact_locator):
        return False
    match = re.search(re.escape(compact_locator), compact_query)
    if match is None:
        return False
    trailing = compact_query[match.end() :]
    if not trailing:
        return True
    if not re.match(r"(?:[0-9]|[\uc758-][0-9])", trailing):
        return True
    if _allows_attachment_date_suffix(compact_locator, trailing):
        return True
    return False


def _allows_attachment_date_suffix(compact_locator: str, trailing: str) -> bool:
    if not trailing or not trailing[0].isdigit():
        return False
    if not any(marker in compact_locator for marker in _APPENDIX_FORM_MARKERS):
        return False
    return re.match(r"(?:19|20)\d{6}", trailing) is not None


def _metadata_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _is_attachment_chunk(metadata: dict[str, Any]) -> bool:
    chunk_type = str(metadata.get("chunk_type") or "").strip().lower()
    if chunk_type in {"appendix", "form"}:
        return True
    return chunk_type == "table" and any(
        metadata.get(field_name)
        for field_name in ("table_appendix_no", "appendix_refs", "form_refs")
    )


def _unnumbered_attachment_markers(query: str) -> frozenset[str]:
    normalized_query = unicodedata.normalize("NFKC", str(query or ""))
    residual_query = _NUMBERED_APPENDIX_FORM_PATTERN.sub(" ", normalized_query)
    return frozenset(
        match.group("marker")
        for match in _UNNUMBERED_ATTACHMENT_PATTERN.finditer(residual_query)
    )


def _attachment_markers_match(
    metadata: dict[str, Any],
    query_markers: frozenset[str],
) -> bool:
    candidate_markers: set[str] = set()
    chunk_type = str(metadata.get("chunk_type") or "").strip().lower()
    if chunk_type == "appendix":
        candidate_markers.add("\ubcc4\ud45c")
    elif chunk_type == "form":
        candidate_markers.update(("\ubcc4\uc9c0", "\uc11c\uc2dd"))
    for field_name in ("table_appendix_no", "appendix_refs", "form_refs"):
        for value in _metadata_values(metadata.get(field_name)):
            compact_value = _compact_match_text(value)
            candidate_markers.update(
                marker
                for marker in _APPENDIX_FORM_MARKERS
                if marker in compact_value
            )
    return bool(candidate_markers.intersection(query_markers))


def _expand_regulation_query(
    query: str,
    *,
    has_named_candidate: bool = False,
) -> str:
    normalized = str(query or "").strip()
    normalized_for_matching = unicodedata.normalize("NFKC", normalized)
    compact = normalized_for_matching.replace(" ", "")
    additions: list[str] = []
    if "육아휴직" in compact and any(term in compact for term in ("얼마나", "기간", "신청", "최대", "수당", "요건", "대상", "조건")):
        additions.append("육아휴직 휴직 신청 요건 대상 기간 복직 수당")
    if _is_leave_foreign_travel_report_query(compact):
        additions.append("휴직자 복무실태 국외 출국 신고 신고서 제출 기한")
    general_leave_query = "휴직" in compact and any(
        term in compact for term in ("종류", "절차", "사유", "운영", "신청", "복직")
    )
    if general_leave_query:
        additions.append("휴직 사유 종류 기간 절차 신청 운영 복직 신고")
    performance_pay_exclusion_query = "성과연봉" in compact and any(
        term in compact
        for term in ("제외", "제한", "지급대상", "대상제외", "못받", "미지급", "지급하지", "중징계", "징계")
    )
    if performance_pay_exclusion_query:
        additions.append("성과연봉 지급 대상 제외 제한 미지급 징계")
    if "성과연봉" in compact and not performance_pay_exclusion_query and any(
        term in compact for term in ("언제", "시기", "지급", "방법")
    ):
        additions.append("성과연봉 지급 방법 지급 시기")
    faculty_hiring_query = (
        ("전임" in compact and "교원" in compact and any(term in compact for term in ("채용", "임용", "절차")))
        or ("교원" in compact and "임용" in compact and "절차" in compact)
    )
    if faculty_hiring_query:
        additions.append("전임 교원 채용 신규 임용 절차 공고 심사")
    if "교원인사위원회" in compact and "심의" in compact:
        additions.append("교원 인사위원회 기능 심의 대상 신규 채용 재계약 승진 징계")
    if (
        _is_appendix_form_query(compact)
        and not has_named_candidate
        and _is_generic_appendix_form_query(normalized_for_matching)
    ):
        additions.append("별표 별지 서식 첨부 작성 방식 근거")
    if not additions:
        return normalized
    return " ".join([normalized, *additions])


def _is_appendix_form_query(compact_query: str) -> bool:
    return any(term in compact_query for term in _APPENDIX_FORM_MARKERS)


def _is_leave_foreign_travel_report_query(compact_query: str) -> bool:
    return "휴직자" in compact_query and "국외출국" in compact_query and "신고서" in compact_query


def _is_generic_appendix_form_query(query: str) -> bool:
    normalized_query = unicodedata.normalize("NFKC", str(query or ""))
    if _NUMBERED_APPENDIX_FORM_PATTERN.search(normalized_query):
        return False
    if _NAMED_REGULATION_PATTERN.search(normalized_query):
        return False
    query_tokens = {
        token
        for token in tokenize(normalized_query)
        if len(str(token or "").strip()) > 1 and not str(token or "").strip().isdigit()
    }
    domain_tokens = {
        token
        for token in query_tokens
        if token not in _GENERIC_APPENDIX_FORM_QUERY_TOKENS
    }
    return not domain_tokens
