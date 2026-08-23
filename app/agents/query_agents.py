from __future__ import annotations

"""Qwen3 1.7B query analysis and rewrite roles with deterministic guardrails."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import AgentResult, BaseAgent
from app.agents.model_router import QWEN3_QUERY_MODEL, get_model_profile
from app.agents.ollama_runtime import OllamaRuntime


QueryIntent = Literal[
    "exact_locator",
    "definition",
    "procedure",
    "eligibility",
    "obligation",
    "comparison",
    "temporal",
    "general",
]
LocatorKind = Literal["part", "chapter", "section", "article", "paragraph", "item", "subitem", "appendix", "form"]

_LOCATOR_PATTERN = re.compile(
    r"제\s*(?P<number>\d+)(?:\s*의\s*(?P<subnumber>\d+))?\s*"
    r"(?P<unit>편|장|절|관|조|항|호|목)(?:\s*의\s*(?P<postsubnumber>\d+))?"
)
_ATTACHMENT_PATTERN = re.compile(
    r"(?P<unit>별표|별지)\s*(?:제\s*)?(?P<number>\d+(?:\s*[-의]\s*\d+)*)\s*(?:호)?"
)
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}\.?\b"),
    re.compile(r"\b\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일\b"),
)
_REGULATION_PATTERN = re.compile(
    r"(?<![0-9A-Za-z가-힣])([0-9A-Za-z가-힣·ㆍ\s]{2,48}?(?:규정|규칙|세칙|지침|요령|내규))(?![0-9A-Za-z가-힣])"
)
_TERM_PATTERN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_UNIT_KIND: dict[str, LocatorKind] = {
    "편": "part",
    "장": "chapter",
    "절": "section",
    "관": "section",
    "조": "article",
    "항": "paragraph",
    "호": "item",
    "목": "subitem",
    "별표": "appendix",
    "별지": "form",
}


class QueryLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LocatorKind
    raw: str = Field(min_length=1, max_length=80)
    canonical: str = Field(min_length=1, max_length=80)


class QueryAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_query: str = Field(min_length=1, max_length=4000)
    normalized_query: str = Field(min_length=1, max_length=4000)
    intent: QueryIntent
    regulation_names: tuple[str, ...] = ()
    locators: tuple[QueryLocator, ...] = ()
    date_conditions: tuple[str, ...] = ()
    version_conditions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    requires_temporal_filter: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    analysis_mode: Literal["local_model", "deterministic_fallback"]
    model: str | None = None
    fallback_reason: str | None = Field(default=None, max_length=120)
    duration_ms: float = Field(default=0.0, ge=0.0)


class QueryRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_query: str = Field(min_length=1, max_length=4000)
    normalized_query: str = Field(min_length=1, max_length=4000)
    search_queries: tuple[str, ...] = Field(min_length=1, max_length=8)
    preserved_locators: tuple[str, ...] = ()
    rewrite_mode: Literal["local_model", "deterministic_fallback"]
    model: str | None = None
    fallback_reason: str | None = Field(default=None, max_length=120)
    duration_ms: float = Field(default=0.0, ge=0.0)


class _ModelAnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: QueryIntent
    regulation_names: list[str] = Field(default_factory=list, max_length=5)
    date_conditions: list[str] = Field(default_factory=list, max_length=5)
    version_conditions: list[str] = Field(default_factory=list, max_length=5)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    requires_temporal_filter: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


class _ModelRewriteDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_query: str = Field(min_length=1, max_length=4000)
    search_queries: list[str] = Field(min_length=1, max_length=8)


class QueryAnalysisAgent(BaseAgent):
    def __init__(self, runtime: OllamaRuntime | None = None) -> None:
        self.runtime = runtime or OllamaRuntime()
        self.profile = get_model_profile("query-qwen3-1.7b")

    def analyze(
        self,
        query: str,
        *,
        prefer_model: bool = True,
        strict_model: bool = False,
    ) -> QueryAnalysis:
        fallback = deterministic_query_analysis(query)
        if not prefer_model:
            return fallback
        try:
            if not self.runtime.model_available(self.profile.model):
                raise RuntimeError("required_model_not_installed")
            payload, generation = self.runtime.generate_json(
                model=self.profile.model,
                prompt=_analysis_prompt(fallback.original_query),
                schema=_ModelAnalysisDraft.model_json_schema(),
                timeout_seconds=self.profile.timeout_seconds,
                temperature=float(self.profile.temperature or 0.0),
                max_output_tokens=800,
            )
            draft = _ModelAnalysisDraft.model_validate(payload)
            return _merge_model_analysis(fallback, draft, generation.duration_ms)
        except Exception as exc:
            if strict_model:
                raise
            return fallback.model_copy(
                update={"fallback_reason": _safe_failure_reason(exc)}
            )

    def run(self, payload: dict) -> AgentResult:
        query = _payload_value(payload, "query")
        result = self.analyze(
            query,
            prefer_model=bool(payload.get("prefer_model", True)),
            strict_model=bool(payload.get("strict_model", False)),
        )
        return AgentResult(result.model_dump(mode="json"))


class QueryRewriteAgent(BaseAgent):
    def __init__(self, runtime: OllamaRuntime | None = None) -> None:
        self.runtime = runtime or OllamaRuntime()
        self.profile = get_model_profile("query-qwen3-1.7b")

    def rewrite(
        self,
        analysis: QueryAnalysis,
        *,
        prefer_model: bool = True,
        strict_model: bool = False,
    ) -> QueryRewrite:
        fallback = deterministic_query_rewrite(analysis)
        if not prefer_model:
            return fallback
        try:
            if not self.runtime.model_available(self.profile.model):
                raise RuntimeError("required_model_not_installed")
            payload, generation = self.runtime.generate_json(
                model=self.profile.model,
                prompt=_rewrite_prompt(analysis),
                schema=_ModelRewriteDraft.model_json_schema(),
                timeout_seconds=self.profile.timeout_seconds,
                temperature=float(self.profile.temperature or 0.0),
                max_output_tokens=900,
            )
            draft = _ModelRewriteDraft.model_validate(payload)
            return _merge_model_rewrite(fallback, draft, generation.duration_ms)
        except Exception as exc:
            if strict_model:
                raise
            return fallback.model_copy(
                update={"fallback_reason": _safe_failure_reason(exc)}
            )

    def run(self, payload: dict) -> AgentResult:
        raw_analysis = payload.get("analysis")
        if raw_analysis is None and payload.get("artifacts"):
            raw_analysis = payload["artifacts"][-1]
        analysis = QueryAnalysis.model_validate(raw_analysis)
        result = self.rewrite(
            analysis,
            prefer_model=bool(payload.get("prefer_model", True)),
            strict_model=bool(payload.get("strict_model", False)),
        )
        return AgentResult(result.model_dump(mode="json"))


def deterministic_query_analysis(query: str) -> QueryAnalysis:
    original = str(query or "").strip()
    normalized = " ".join(original.split())
    if not normalized:
        raise ValueError("query must not be empty")
    if len(normalized) > 4000:
        raise ValueError("query exceeds 4000 characters")
    locators = _extract_locators(normalized)
    dates = tuple(dict.fromkeys(match.group(0).strip() for pattern in _DATE_PATTERNS for match in pattern.finditer(normalized)))
    version_terms = tuple(
        term for term in ("현행", "시행 당시", "개정 전", "개정 후", "최신", "구 규정") if term in normalized
    )
    regulation_names = tuple(
        dict.fromkeys(" ".join(match.group(1).split()) for match in _REGULATION_PATTERN.finditer(normalized))
    )
    intent = _deterministic_intent(normalized, bool(locators), bool(dates or version_terms))
    stopwords = {"무엇", "어떻게", "알려줘", "인가요", "있나요", "규정", "관련", "대한", "에서", "으로"}
    keywords = tuple(
        dict.fromkeys(
            token for token in _TERM_PATTERN.findall(normalized) if token not in stopwords
        )
    )[:12]
    return QueryAnalysis(
        original_query=original,
        normalized_query=normalized,
        intent=intent,
        regulation_names=regulation_names,
        locators=locators,
        date_conditions=dates,
        version_conditions=version_terms,
        keywords=keywords,
        requires_temporal_filter=bool(dates or version_terms),
        confidence=0.72 if locators or regulation_names else 0.58,
        analysis_mode="deterministic_fallback",
        fallback_reason="model_not_requested",
    )


def deterministic_query_rewrite(analysis: QueryAnalysis) -> QueryRewrite:
    queries = [analysis.normalized_query]
    compact = re.sub(r"제\s*(\d+)\s*조", r"제\1조", analysis.normalized_query)
    compact = re.sub(r"제\s*(\d+)\s*항", r"제\1항", compact)
    compact = re.sub(r"제\s*(\d+)\s*호", r"제\1호", compact)
    compact = re.sub(r"(조|항|호)\s*의\s*(\d+)", r"\1의\2", compact)
    if compact != analysis.normalized_query:
        queries.append(compact)
    locator_text = " ".join(locator.canonical for locator in analysis.locators)
    keyword_text = " ".join(analysis.keywords[:8])
    if locator_text or keyword_text:
        queries.append(" ".join(part for part in (" ".join(analysis.regulation_names), locator_text, keyword_text) if part))
    return QueryRewrite(
        original_query=analysis.original_query,
        normalized_query=analysis.normalized_query,
        search_queries=tuple(_bounded_unique_queries(queries)),
        preserved_locators=tuple(locator.canonical for locator in analysis.locators),
        rewrite_mode="deterministic_fallback",
        fallback_reason="model_not_requested",
    )


def _extract_locators(query: str) -> tuple[QueryLocator, ...]:
    locators: list[QueryLocator] = []
    for match in _LOCATOR_PATTERN.finditer(query):
        number = match.group("number")
        subnumber = match.group("postsubnumber") or match.group("subnumber")
        unit = match.group("unit")
        canonical = f"제{number}{unit}{'의' + subnumber if subnumber else ''}"
        locators.append(QueryLocator(kind=_UNIT_KIND[unit], raw=match.group(0), canonical=canonical))
    for match in _ATTACHMENT_PATTERN.finditer(query):
        unit = match.group("unit")
        number = re.sub(r"\s+", "", match.group("number")).replace("-", "의")
        locators.append(QueryLocator(kind=_UNIT_KIND[unit], raw=match.group(0), canonical=f"{unit} 제{number}호"))
    unique: dict[tuple[str, str], QueryLocator] = {}
    for locator in locators:
        unique[(locator.kind, locator.canonical)] = locator
    return tuple(unique.values())


def _deterministic_intent(query: str, has_locator: bool, temporal: bool) -> QueryIntent:
    if has_locator:
        return "exact_locator"
    if temporal or any(term in query for term in ("언제", "시행일", "개정일")):
        return "temporal"
    if any(term in query for term in ("정의", "뜻", "의미")):
        return "definition"
    if any(term in query for term in ("절차", "방법", "어떻게", "신청")):
        return "procedure"
    if any(term in query for term in ("자격", "대상", "요건", "조건")):
        return "eligibility"
    if any(term in query for term in ("의무", "하여야", "책임", "금지")):
        return "obligation"
    if any(term in query for term in ("차이", "비교", "변경")):
        return "comparison"
    return "general"


def _merge_model_analysis(
    fallback: QueryAnalysis,
    draft: _ModelAnalysisDraft,
    duration_ms: float,
) -> QueryAnalysis:
    query_compact = re.sub(r"\s+", "", fallback.original_query)
    model_names = [
        " ".join(name.split())
        for name in draft.regulation_names
        if name.strip() and re.sub(r"\s+", "", name) in query_compact
    ]
    names = tuple(dict.fromkeys([*fallback.regulation_names, *model_names]))[:5]
    dates = tuple(dict.fromkeys([*fallback.date_conditions, *[item.strip() for item in draft.date_conditions if item.strip()]]))[:5]
    versions = tuple(dict.fromkeys([*fallback.version_conditions, *[item.strip() for item in draft.version_conditions if item.strip()]]))[:5]
    keywords = tuple(dict.fromkeys([*fallback.keywords, *[item.strip() for item in draft.keywords if item.strip()]]))[:12]
    return fallback.model_copy(
        update={
            "intent": "exact_locator" if fallback.locators else draft.intent,
            "regulation_names": names,
            "date_conditions": dates,
            "version_conditions": versions,
            "keywords": keywords,
            "requires_temporal_filter": bool(
                fallback.requires_temporal_filter or draft.requires_temporal_filter
            ),
            "confidence": round(min(1.0, max(fallback.confidence, draft.confidence)), 4),
            "analysis_mode": "local_model",
            "model": QWEN3_QUERY_MODEL,
            "fallback_reason": None,
            "duration_ms": duration_ms,
        }
    )


def _merge_model_rewrite(
    fallback: QueryRewrite,
    draft: _ModelRewriteDraft,
    duration_ms: float,
) -> QueryRewrite:
    queries = _bounded_unique_queries(
        [fallback.original_query, draft.normalized_query, *draft.search_queries, *fallback.search_queries]
    )
    for locator in fallback.preserved_locators:
        if not any(locator.replace(" ", "") in query.replace(" ", "") for query in queries):
            queries.append(f"{fallback.normalized_query} {locator}")
    return fallback.model_copy(
        update={
            "normalized_query": " ".join(draft.normalized_query.split()),
            "search_queries": tuple(_bounded_unique_queries(queries)[:8]),
            "rewrite_mode": "local_model",
            "model": QWEN3_QUERY_MODEL,
            "fallback_reason": None,
            "duration_ms": duration_ms,
        }
    )


def _analysis_prompt(query: str) -> str:
    return (
        "당신은 한국 공공기관 규정 검색 질의 분석기다. 답변하지 말고 JSON만 생성한다. "
        "질문에 실제로 나타난 규정명·날짜·버전 조건만 추출하고, 검색 핵심어를 최대 12개 제시한다. "
        "조문 locator는 별도의 결정론 파서가 보존하므로 만들어내지 않는다.\n"
        f"사용자 질문(JSON 문자열): {json.dumps(query, ensure_ascii=False)}"
    )


def _rewrite_prompt(analysis: QueryAnalysis) -> str:
    payload = analysis.model_dump(
        mode="json",
        exclude={"analysis_mode", "model", "fallback_reason", "duration_ms", "confidence"},
    )
    return (
        "당신은 한국 규정 검색어 보정기다. 답변하지 말고 JSON만 생성한다. "
        "원 질문 의미와 모든 조문 표기를 보존하면서 띄어쓰기·조문 표기·핵심어 조합을 보정한다. "
        "검색 query는 8개 이하이며 새로운 규정명이나 날짜를 발명하지 않는다.\n"
        f"질의 계획: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _bounded_unique_queries(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = " ".join(str(value or "").split())[:500]
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:8]


def _payload_value(payload: dict, key: str) -> str:
    value = payload.get(key)
    if value is None and payload.get("artifacts"):
        artifact = payload["artifacts"][-1]
        if isinstance(artifact, dict):
            value = artifact.get(key)
        elif isinstance(artifact, str):
            value = artifact
    return str(value or "")


def _safe_failure_reason(exc: Exception) -> str:
    message = str(exc)
    if message == "required_model_not_installed":
        return message
    return f"model_{type(exc).__name__}"[:120]
