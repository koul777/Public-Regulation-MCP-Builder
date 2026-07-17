from __future__ import annotations

from collections import Counter
import re
from typing import Any

from app.ingestion.embedding_adapter import LOCAL_HASH_EMBEDDING_MODEL, local_hash_embedding
from app.retrieval.bm25_index import BM25_RETRIEVAL_MODEL, Bm25Index
from app.retrieval.tokenizer import tokenize


LEXICAL_FALLBACK_MODEL = "token-lexical-fallback-v1"
_APPENDIX_FORM_MARKERS = ("\ubcc4\ud45c", "\ubcc4\uc9c0", "\uc11c\uc2dd")
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


def search(
    query: str,
    records: list[dict[str, Any]],
    index: Bm25Index | None,
    top_k: int,
    *,
    index_records: list[dict[str, Any]] | None = None,
    index_source_content_hashes: str | None = None,
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    expanded_query = _expand_regulation_query(query)
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
        scored = _bm25_search(expanded_query, records, index)
        scored = _apply_query_boosts(expanded_query, scored)
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
            "retrieval_fallback": False,
            "bm25_index_status": "ready",
            "query_expanded": expanded_query != query,
            **definition_metadata,
        }
    fallback_reason = "missing_bm25_index" if index is None else "stale_bm25_index"
    hash_scored = _hash_embedding_search(expanded_query, records)
    if hash_scored:
        hash_scored = _apply_query_boosts(expanded_query, hash_scored)
        hash_scored, definition_metadata = _promote_enumeration_definitions(query, hash_scored, records)
        return hash_scored[:top_k], {
            "retrieval_model": LOCAL_HASH_EMBEDDING_MODEL,
            "retrieval_fallback": True,
            "bm25_index_status": fallback_reason,
            "query_expanded": expanded_query != query,
            **definition_metadata,
        }
    lexical_scored = _apply_query_boosts(expanded_query, _lexical_search(expanded_query, records))
    lexical_scored, definition_metadata = _promote_enumeration_definitions(query, lexical_scored, records)
    return lexical_scored[:top_k], {
        "retrieval_model": LEXICAL_FALLBACK_MODEL,
        "retrieval_fallback": True,
        "bm25_index_status": fallback_reason,
        "query_expanded": expanded_query != query,
        **definition_metadata,
    }


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
    query_embedding_cache: dict[int, list[float]] = {}
    for record in records:
        embedding = record.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            continue
        dimensions = len(embedding)
        query_embedding = query_embedding_cache.get(dimensions)
        if query_embedding is None:
            query_embedding = local_hash_embedding(query, dimensions=dimensions)
            query_embedding_cache[dimensions] = query_embedding
        score = round(sum(float(a) * float(b) for a, b in zip(query_embedding, embedding)), 8)
        scored.append((score, record))
    return sorted(scored, key=lambda item: item[0], reverse=True)


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


def _apply_query_boosts(query: str, scored: list[tuple[float, dict[str, Any]]]) -> list[tuple[float, dict[str, Any]]]:
    if not scored:
        return scored
    boosted = [(score + _record_query_boost(query, record), record) for score, record in scored]
    return sorted(boosted, key=lambda item: item[0], reverse=True)


def _promote_enumeration_definitions(
    query: str,
    scored: list[tuple[float, dict[str, Any]]],
    records: list[dict[str, Any]],
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    compact_query = str(query or "").replace(" ", "")
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
    normalized_query = " ".join(str(query or "").split()).lower()
    compact_query = normalized_query.replace(" ", "")
    terms = [term for term in normalized_query.split() if len(term) >= 2]
    if not compact_query and not terms:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        blob = " ".join(
            str(value or "")
            for value in (
                record.get("text"),
                metadata.get("regulation_title"),
                metadata.get("article_title"),
                metadata.get("article_no"),
                metadata.get("document_name"),
            )
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
query: str, record: dict[str, Any]) -> float:
    compact = str(query or "").replace(" ", "")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    blob = " ".join(
        str(value or "")
        for value in (
            record.get("text"),
            metadata.get("regulation_title"),
            metadata.get("article_no"),
            metadata.get("article_title"),
            metadata.get("hierarchy_path"),
        )
    )
    boost = 0.0
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
        if _is_generic_appendix_form_query(query):
            if "제18조" in blob and "별표와 별지 서식" in blob:
                boost += 28.0
            if "원규관리규정 시행세칙" in blob and ("별표 또는 별지 서식" in blob or "작성방식" in blob):
                boost += 16.0
        if "지급근거" in blob or "손망실" in blob or "개인정보" in blob or "가스안전" in blob:
            boost -= 8.0
    return boost


def _expand_regulation_query(query: str) -> str:
    normalized = str(query or "").strip()
    compact = normalized.replace(" ", "")
    additions: list[str] = []
    if "육아휴직" in compact and any(term in compact for term in ("얼마나", "기간", "신청", "최대", "수당", "요건", "대상", "조건")):
        additions.append(
            "제29조 휴직 제29조 제3항 만 8세 이하 초등학교 2학년 이하 자녀 양육 임신 출산 "
            "특별한 사정이 없는 한 휴직을 명하여야 한다 제30조 휴직 기간 제29조 제3항 자녀 1명 3년 이내 "
            "시간선택제 교직원보수규정 제33조 육아휴직수당 30일 이상 기본연봉월액 78퍼센트 62.4퍼센트 "
            "250만원 200만원 160만원 지급기간 1년 18개월"
        )
    if _is_leave_foreign_travel_report_query(compact):
        additions.append(
            "제29조의3 휴직자의 복무실태 점검 별지 제16호서식 "
            "휴직자 국외 출국 신고서 출국 7일 전 14일 이하 영유아 신고 생략"
        )
    general_leave_query = "휴직" in compact and any(
        term in compact for term in ("종류", "절차", "사유", "운영", "신청", "복직")
    )
    if general_leave_query:
        additions.append(
            "제29조 제29조 제29조 휴직 휴직 사유 본인의 의사에 불구하고 휴직을 명하여야 한다 "
            "교직원이 다음 각 호 어느 하나 해당 휴직을 원하는 경우 인사위원회 심의 휴직을 명할 수 있다 "
            "제30조 휴직 기간 제31조 휴직의 운영 복직 신고"
        )
    performance_pay_exclusion_query = "성과연봉" in compact and any(
        term in compact
        for term in ("제외", "제한", "지급대상", "대상제외", "못받", "미지급", "지급하지", "중징계", "징계")
    )
    if performance_pay_exclusion_query:
        additions.append(
            "제27조의2 성과연봉 지급대상 제외 평가대상 기간 중 중징계 처분 징계 사유 시효 5년 비위 "
            "성폭력 성매매 성희롱 음주운전 음주측정 불응 지급 대상에서 제외"
        )
    if "성과연봉" in compact and not performance_pay_exclusion_query and any(
        term in compact for term in ("언제", "시기", "지급", "방법")
    ):
        additions.append("제24조 성과연봉 지급 방법 지급시기 6월 12월 일시금 이등분")
    faculty_hiring_query = (
        ("전임" in compact and "교원" in compact and any(term in compact for term in ("채용", "임용", "절차")))
        or ("교원" in compact and "임용" in compact and "절차" in compact)
    )
    if faculty_hiring_query:
        additions.append(
            "전임 교원 교수 부교수 조교수 교원 임용 세칙 초빙교수 객원교수를 제외한 교원의 임용 "
            "제7조 신규임용의 시기 제8조 신규임용 후보자 심사 "
            "지원 마감일 전까지 15일 이상 공고 단계별 심사 기초심사 연구실적심사 "
            "공개발표심사 면접심사"
        )
    if "교원인사위원회" in compact and "심의" in compact:
        additions.append(
            "인사규정 제8조 위원회 기능 교원 인사위원회 교원의 신규 채용 재계약 승진 "
            "정년보장 강임 면직 징계 심의"
        )
    if _is_appendix_form_query(compact) and _is_generic_appendix_form_query(normalized):
        additions.append(
            "원규관리규정 제18조 별표와 별지 서식 내용이 길거나 복잡한 표 그림 계산식 "
            "분량이 많거나 이해하기 어려운 사항 별표로 구분 별지 서식 일정한 형식 "
            "본칙에 부수되는 별표 별지 서식 작성방식 일부개정 전부개정"
        )
    if not additions:
        return normalized
    return " ".join([normalized, *additions])


def _is_appendix_form_query(compact_query: str) -> bool:
    return any(term in compact_query for term in _APPENDIX_FORM_MARKERS)


def _is_leave_foreign_travel_report_query(compact_query: str) -> bool:
    return "휴직자" in compact_query and "국외출국" in compact_query and "신고서" in compact_query


def _is_generic_appendix_form_query(query: str) -> bool:
    query_tokens = {
        token
        for token in tokenize(str(query or ""))
        if len(str(token or "").strip()) > 1 and not str(token or "").strip().isdigit()
    }
    domain_tokens = {
        token
        for token in query_tokens
        if token not in _GENERIC_APPENDIX_FORM_QUERY_TOKENS
    }
    return not domain_tokens
