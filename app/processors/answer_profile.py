from __future__ import annotations

import re
from typing import Any, Mapping


ANSWER_PROFILE_VERSION = "reg-rag-answer-profile-v1"
ANSWER_PROFILE_MARKER = "[답변분류]"


def clean_answer_profile_text(value: str) -> str:
    cleaned = " ".join(str(value or "").split())
    cleaned = re.sub(r"\s*<[^>]{1,120}>", "", cleaned)
    cleaned = re.sub(r"\s*<[^>]*$", "", cleaned)
    cleaned = cleaned.replace("일 시금", "일시금").replace("정 산", "정산")
    cleaned = cleaned.replace("하 되", "하되").replace("경 우", "경우")
    cleaned = cleaned.replace("교 직원", "교직원").replace("재직기 간", "재직기간")
    cleaned = cleaned.replace("휴 직한", "휴직한").replace("7개 월째", "7개월째")
    cleaned = cleaned.replace("해당 하는", "해당하는").replace("70 만원", "70만원")
    cleaned = cleaned.replace("임신또는", "임신 또는").replace("3년이내", "3년 이내")
    cleaned = cleaned.replace("다 음", "다음").replace("음주운 전", "음주운전")
    cleaned = cleaned.replace("등 급", "등급").replace("징계 량", "징계량").replace("다 시", "다시")
    if re.fullmatch(
        r"(?:\d+(?:-\d+)+\.\s*)?[가-힣A-Za-z0-9·ㆍ\s]+(?:규정|세칙|지침|요강|규칙)\s*\d+(?:\.\d+)*\.?>?",
        cleaned,
    ):
        return ""
    cleaned = re.sub(r"([①-⑳])(?=[가-힣A-Za-z0-9])", r"\1 ", cleaned)
    cleaned = re.sub(r"(\d+\s*(?:년|개월|월|일|시간|분))\s*(이내|이상|이하|초과|미만|까지)", r"\1 \2", cleaned)
    cleaned = re.sub(r"([가-힣])\s+(으로|로|에|에서|에게|부터|까지|보다|처럼|만큼|와|과|를|을|은|는|도|의)\b", r"\1\2", cleaned)
    return cleaned.strip(" ;,")


def build_answer_profile(text: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    sentences = _sentences(text)
    intents = _answer_intents(text, metadata)
    keywords = _answer_keywords(text, metadata, intents)
    facts = _answer_facts(sentences)
    outline = _answer_outline(sentences, facts)
    if not intents and not keywords and not facts and not outline:
        return {}
    return {
        "answer_profile_version": ANSWER_PROFILE_VERSION,
        "answer_intents": intents,
        "answer_keywords": keywords,
        "answer_facts": facts,
        "answer_outline": outline,
    }


def append_answer_profile_to_retrieval_text(retrieval_text: str, profile: Mapping[str, Any]) -> str:
    if not profile or ANSWER_PROFILE_MARKER in retrieval_text:
        return retrieval_text
    lines = answer_profile_retrieval_lines(profile)
    if not lines:
        return retrieval_text
    return f"{retrieval_text.rstrip()}\n{ANSWER_PROFILE_MARKER}\n" + "\n".join(lines)


def answer_profile_retrieval_lines(profile: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    intents = [str(value) for value in profile.get("answer_intents") or [] if str(value).strip()]
    keywords = [str(value) for value in profile.get("answer_keywords") or [] if str(value).strip()]
    if intents:
        lines.append("의도: " + ", ".join(intents[:10]))
    if keywords:
        lines.append("키워드: " + ", ".join(keywords[:20]))
    for fact in profile.get("answer_facts") or []:
        if not isinstance(fact, Mapping):
            continue
        fact_type = str(fact.get("type") or "").strip()
        value = str(fact.get("value") or "").strip()
        if fact_type and value:
            lines.append(f"- {fact_type}: {value}")
        if len(lines) >= 12:
            break
    return lines


def _answer_intents(text: str, metadata: Mapping[str, Any]) -> list[str]:
    haystack = " ".join(
        str(value or "")
        for value in (
            text,
            metadata.get("regulation_title"),
            metadata.get("article_title"),
            metadata.get("paragraph_label"),
        )
    )
    rules = (
        ("procedure", ("절차", "단계", "심사", "공고", "접수", "선정", "선발", "임용", "채용", "신청", "승인")),
        ("eligibility", ("대상", "자격", "요건", "기준", "해당", "제외", "제한")),
        ("duration", ("기간", "기한", "이내", "이상", "이하", "초과", "미만", "까지", "년", "개월", "일")),
        ("payment", ("지급", "수당", "연봉", "보수", "급여", "금액", "만원", "%", "퍼센트", "일시금")),
        ("obligation", ("하여야 한다", "해야 한다", "하여야 하며", "제출", "신고", "통보", "금지")),
        ("exception", ("다만", "예외", "불구하고", "제외한다", "아니하다")),
        ("definition", ("정의", "뜻은", "이란", "라 함은", "말한다")),
    )
    intents: list[str] = []
    for intent, terms in rules:
        if any(term in haystack for term in terms):
            intents.append(intent)
    return intents


def _answer_keywords(text: str, metadata: Mapping[str, Any], intents: list[str]) -> list[str]:
    candidates: list[str] = []
    for key in ("regulation_title", "article_title", "paragraph_label"):
        value = str(metadata.get(key) or "").strip()
        if value:
            candidates.append(value)
    candidates.extend(intents)
    candidates.extend(_terms_from_text(text))
    return _unique(candidates, limit=24)


def _terms_from_text(text: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[가-힣A-Za-z0-9·ㆍ%]+", text):
        token = token.strip()
        if len(token) < 2:
            continue
        if token in {"한다", "있는", "없는", "경우", "다음", "각호", "사항", "따라", "대한", "관한"}:
            continue
        if re.fullmatch(r"\d+", token):
            continue
        if re.search(r"(휴직|채용|임용|심사|공고|지급|연봉|보수|수당|기간|자녀|시간선택제|자격|기준|위원회)", token):
            terms.append(token)
    return terms


def _answer_facts(sentences: list[str]) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for sentence in sentences:
        for step in _procedure_steps(sentence):
            facts.append({"type": "procedure_step", "value": step, "sentence": sentence})
        if _has_duration_fact(sentence):
            facts.append({"type": "duration", "value": _duration_value(sentence), "sentence": sentence})
        if _has_payment_fact(sentence):
            facts.append({"type": "payment", "value": _payment_value(sentence), "sentence": sentence})
        if _has_condition_fact(sentence):
            facts.append({"type": "condition", "value": sentence, "sentence": sentence})
        if _has_obligation_fact(sentence):
            facts.append({"type": "obligation", "value": sentence, "sentence": sentence})
        if _has_exception_fact(sentence):
            facts.append({"type": "exception", "value": sentence, "sentence": sentence})
        if len(facts) >= 20:
            break
    return _unique_facts(facts)


def _procedure_steps(sentence: str) -> list[str]:
    if not re.search(r"(절차|단계|심사|공고|접수|선정|선발|임용|채용)", sentence):
        return []
    matches = list(re.finditer(r"(?:^|\s)(?:\d+\.|[가-힣]\.|[①②③④⑤⑥⑦⑧⑨⑩])\s*([^①②③④⑤⑥⑦⑧⑨⑩]+?)(?=\s+(?:\d+\.|[가-힣]\.|[①②③④⑤⑥⑦⑧⑨⑩])|$)", sentence))
    steps = [_clean_fact_value(match.group(1)) for match in matches]
    steps = [step for step in steps if step]
    if steps:
        return steps[:12]
    cleaned = _clean_fact_value(sentence)
    if len(cleaned) <= 40 and re.search(r"(심사|공고|접수|면접|선발|선정|임용|채용)", cleaned):
        return [cleaned]
    if "단계" in sentence and "심사" in sentence:
        return [_clean_fact_value(sentence)]
    return []


def _has_duration_fact(sentence: str) -> bool:
    return bool(re.search(r"\d+\s*(?:년|개월|월|일|시간|분)", sentence)) and bool(
        re.search(r"(기간|기한|이내|이상|이하|초과|미만|까지|범위)", sentence)
    )


def _duration_value(sentence: str) -> str:
    values = re.findall(r"\d+\s*(?:년|개월|월|일|시간|분)(?:\s*(?:이내|이상|이하|초과|미만|까지))?", sentence)
    if values:
        return ", ".join(_unique([value.replace(" ", "") for value in values], limit=8))
    return sentence


def _has_payment_fact(sentence: str) -> bool:
    return bool(
        re.search(r"(지급|수당|연봉|보수|급여|금액|일시금|성과급|환수|계좌이체|요구불예금)", sentence)
        or re.search(r"\d+\s*(?:원|만원|%)", sentence)
    )


def _payment_value(sentence: str) -> str:
    values = re.findall(r"\d+\s*(?:월|원|만원|%|퍼센트)|일시금|매월|계좌이체|요구불예금", sentence)
    if values:
        return ", ".join(_unique([value.replace(" ", "") for value in values], limit=10))
    return sentence


def _has_condition_fact(sentence: str) -> bool:
    return bool(re.search(r"(대상|자격|요건|기준|경우|해당|제외|제한)", sentence))


def _has_obligation_fact(sentence: str) -> bool:
    return bool(re.search(r"(하여야 한다|해야 한다|하여야 하며|제출|신고|통보|금지|할 수 없다)", sentence))


def _has_exception_fact(sentence: str) -> bool:
    return bool(re.search(r"(다만|예외|불구하고|제외한다|아니하다)", sentence))


def _answer_outline(sentences: list[str], facts: list[dict[str, str]]) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    fact_sentences = {fact["sentence"] for fact in facts if fact.get("sentence")}
    for index, sentence in enumerate(sentences):
        score = 0
        if sentence in fact_sentences:
            score += 4
        if re.search(r"(한다|하여야|할 수 있다|할 수 없다|이내|이상|지급|심사|공고|자격)", sentence):
            score += 2
        if re.search(r"(제\d+조|제\s*\d+\s*조)", sentence):
            score += 1
        if score:
            scored.append((-score, index, sentence))
    if not scored:
        return sentences[:3]
    return [sentence for _, _, sentence in sorted(scored)[:5]]


def _sentences(text: str) -> list[str]:
    cleaned = str(text or "")
    cleaned = re.sub(r"\[[^\]]+\]", "\n", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []
    raw = re.split(r"(?:(?<=[.!?。])\s+|(?=제\s*\d+\s*조\s*\()|(?=\d+\.\s)|(?=[①②③④⑤⑥⑦⑧⑨⑩]))", cleaned)
    sentences: list[str] = []
    for part in raw:
        sentence = _clean_fact_value(part)
        if sentence and sentence not in sentences:
            sentences.append(sentence)
    return sentences


def _clean_fact_value(value: str) -> str:
    value = clean_answer_profile_text(value)
    value = re.sub(r"^[-•]\s*", "", value)
    return value.strip(" ;,")


def _unique(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_fact_value(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _unique_facts(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for fact in facts:
        key = (fact.get("type", ""), fact.get("value", ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(fact)
    return result
