from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any


LOGGER = logging.getLogger(__name__)
TOKENIZER_MODEL = "kiwi-tokenizer-v1"
FALLBACK_TOKENIZER_MODEL = "regex-ko-tokenizer-v1"

_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)
_ARTICLE_NO_RE = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?", re.IGNORECASE)
_NORMALIZED_ARTICLE_NO_RE = re.compile(r"제\d+조(?:의\d+)?", re.IGNORECASE)
_KEEP_KIWI_TAG_PREFIXES = ("N", "VV", "VA", "XR", "SL", "SN")
_KOREAN_SUFFIXES = tuple(
    sorted(
        {
            "으로부터",
            "로부터",
            "에게서",
            "에서도",
            "에서",
            "에게",
            "으로",
            "부터",
            "까지",
            "하고",
            "하며",
            "하는",
            "하여",
            "하면",
            "하게",
            "했다",
            "한다",
            "된다",
            "되어",
            "된",
            "한",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "에",
            "로",
            "와",
            "과",
            "도",
            "만",
            "의",
        },
        key=len,
        reverse=True,
    )
)
_KOREAN_COMPOUND_SUFFIXES = (
    "\ud734\uc9c1",  # leave
    "\uc808\ucc28",  # procedure
    "\uc2e0\uccad",  # application
    "\uaddc\uc815",  # regulation
    "\uc218\ub2f9",  # allowance
    "\uc9c0\uae09",  # payment
    "\ucc44\uc6a9",  # hiring
    "\uacc4\uc57d",  # contract
    "\uac80\uc0ac",  # inspection
    "\uac80\uc218",  # acceptance inspection
    "\uc11c\uc2dd",  # form
    "\ubcc4\ud45c",  # attached table
)


def tokenize(
    text: str,
    *,
    dedupe: bool = True,
    prefer_regex_if_kiwi_cold: bool = False,
    tokenizer_model: str | None = None,
) -> list[str]:
    raw_text = str(text or "")
    article_tokens = [_normalize_article_no(match.group(0)) for match in _ARTICLE_NO_RE.finditer(raw_text)]
    if tokenizer_model == FALLBACK_TOKENIZER_MODEL:
        kiwi = None
    elif tokenizer_model == TOKENIZER_MODEL:
        kiwi = _kiwi()
    else:
        kiwi = None if prefer_regex_if_kiwi_cold and not kiwi_is_ready() else _kiwi()
    if kiwi is None:
        tokens = _regex_tokens(raw_text)
    else:
        tokens = _kiwi_tokens(kiwi, raw_text)
    combined = [*article_tokens, *tokens]
    if dedupe:
        return _dedupe_preserve_order(combined)
    return [token for token in combined if token]


def tokenizer_name() -> str:
    return TOKENIZER_MODEL if _kiwi() is not None else FALLBACK_TOKENIZER_MODEL


def kiwi_is_ready() -> bool:
    return bool(_kiwi.cache_info().currsize)


@lru_cache(maxsize=1)
def _kiwi() -> Any | None:
    try:
        from kiwipiepy import Kiwi  # type: ignore
    except Exception as exc:  # pragma: no cover - environment-dependent branch
        LOGGER.warning("kiwipiepy is unavailable; falling back to regex Korean tokenizer: %s", exc)
        return None
    try:
        return Kiwi()
    except Exception as exc:  # pragma: no cover - environment-dependent branch
        LOGGER.warning("kiwipiepy initialization failed; falling back to regex Korean tokenizer: %s", exc)
        return None


def _kiwi_tokens(kiwi: Any, text: str) -> list[str]:
    tokens: list[str] = []
    try:
        analyzed = kiwi.tokenize(text)
    except Exception as exc:  # pragma: no cover - defensive fallback
        LOGGER.warning("kiwipiepy tokenization failed; falling back to regex Korean tokenizer: %s", exc)
        return _regex_tokens(text)
    for item in analyzed:
        form = str(getattr(item, "form", "") or "").strip().lower()
        tag = str(getattr(item, "tag", "") or "")
        if not form:
            continue
        if _is_article_no(form) or tag.startswith(_KEEP_KIWI_TAG_PREFIXES):
            tokens.extend(_expand_token(form))
    return tokens


def _regex_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        tokens.extend(_expand_token(raw))
    return tokens


def _expand_token(token: str) -> list[str]:
    normalized = _normalize_token(token)
    if not normalized:
        return []
    if _is_article_no(normalized):
        return [normalized]
    compound_parts = _korean_compound_parts(normalized)
    stripped = _strip_korean_suffix(normalized)
    expanded = [normalized, *compound_parts]
    if stripped != normalized:
        expanded.append(stripped)
    return expanded


def _normalize_token(token: str) -> str:
    normalized = re.sub(r"\s+", "", str(token or "").strip().lower())
    return normalized


def _normalize_article_no(token: str) -> str:
    return re.sub(r"\s+", "", str(token or "").strip().lower())


def _is_article_no(token: str) -> bool:
    return bool(_NORMALIZED_ARTICLE_NO_RE.fullmatch(token))


def _strip_korean_suffix(token: str) -> str:
    if len(token) <= 2:
        return token
    for suffix in _KOREAN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _korean_compound_parts(token: str) -> list[str]:
    parts: list[str] = []
    for suffix in _KOREAN_COMPOUND_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            prefix = token[: -len(suffix)]
            if prefix:
                parts.extend([prefix, suffix])
            break
    return parts


def _dedupe_preserve_order(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped
