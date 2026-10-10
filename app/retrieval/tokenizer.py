from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any


LOGGER = logging.getLogger(__name__)
TOKENIZER_MODEL = "kiwi-tokenizer-v1"
FALLBACK_TOKENIZER_MODEL = "regex-ko-tokenizer-v1"

# Bulk index builds call tokenize() tens of times per record and most of those
# texts repeat. These two context-local switches let a builder first record the
# texts Kiwi would analyze, then analyze the unique ones in one batched call.
# Outside those blocks tokenize() behaves exactly as before.
_RECORDED_KIWI_INPUTS: ContextVar[dict[str, None] | None] = ContextVar("_RECORDED_KIWI_INPUTS", default=None)
_PRELOADED_KIWI_TOKENS: ContextVar[dict[str, list[str]] | None] = ContextVar(
    "_PRELOADED_KIWI_TOKENS", default=None
)

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
    # Fold to NFKC so decomposed (NFD) input and compatibility-width digits or
    # punctuation — both common in Korean PDF/HWP extraction — produce the
    # same tokens as ordinary keyboard input.
    # Both indexing and querying flow through here, keeping the two sides
    # consistent regardless of the source's Unicode composition.
    raw_text = unicodedata.normalize("NFKC", str(text or ""))
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
        recorded = _RECORDED_KIWI_INPUTS.get()
        if recorded is not None:
            recorded[raw_text] = None
            return []
        preloaded = _PRELOADED_KIWI_TOKENS.get()
        cached = preloaded.get(raw_text) if preloaded is not None else None
        tokens = list(cached) if cached is not None else _kiwi_tokens(kiwi, raw_text)
    combined = [*article_tokens, *tokens]
    if dedupe:
        return _dedupe_preserve_order(combined)
    return [token for token in combined if token]


@contextmanager
def recording_kiwi_inputs() -> Iterator[dict[str, None]]:
    """Collect the texts tokenize() would send to Kiwi, without analyzing them.

    Inside the block, every tokenize() call that would use Kiwi records its
    NFKC-normalized text in the yielded dict (insertion-ordered, unique) and
    returns ``[]``. Use it only for a dry pass whose results are discarded.
    """

    recorded: dict[str, None] = {}
    reset_token = _RECORDED_KIWI_INPUTS.set(recorded)
    try:
        yield recorded
    finally:
        _RECORDED_KIWI_INPUTS.reset(reset_token)


@contextmanager
def preloaded_kiwi_tokens(texts: Iterable[str]) -> Iterator[None]:
    """Analyze many texts in one batched Kiwi call and reuse the result.

    Inside the block, tokenize() answers Kiwi-path calls for these texts from
    the batch; any other text is analyzed as usual. Token output is identical
    to per-call analysis. Without Kiwi, or if the batch call fails, the block
    simply runs without preloading.
    """

    table: dict[str, list[str]] | None = None
    kiwi = _kiwi()
    if kiwi is not None:
        unique = list(dict.fromkeys(unicodedata.normalize("NFKC", str(text or "")) for text in texts))
        if unique:
            try:
                table = {
                    text: _kiwi_items_to_tokens(items) for text, items in zip(unique, kiwi.tokenize(unique))
                }
            except Exception as exc:  # pragma: no cover - defensive fallback
                LOGGER.warning("kiwipiepy batch tokenization failed; analyzing texts one by one: %s", exc)
                table = None
    reset_token = _PRELOADED_KIWI_TOKENS.set(table)
    try:
        yield
    finally:
        _PRELOADED_KIWI_TOKENS.reset(reset_token)


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
    try:
        analyzed = kiwi.tokenize(text)
    except Exception as exc:  # pragma: no cover - defensive fallback
        LOGGER.warning("kiwipiepy tokenization failed; falling back to regex Korean tokenizer: %s", exc)
        return _regex_tokens(text)
    return _kiwi_items_to_tokens(analyzed)


# Kiwi analyses repeat a small set of (form, tag) pairs thousands of times in a
# bulk index build, and the tokens a pair yields depend on nothing else.  The
# table maps the raw (form, tag) strings of an analysis item to those tokens
# (an empty tuple for items that are dropped).  It stops growing at the cap, so
# it cannot hold more than a bounded amount of memory; uncached pairs are
# simply computed.
_KIWI_ITEM_TOKEN_CACHE: dict[tuple[str, str], tuple[str, ...]] = {}
_KIWI_ITEM_TOKEN_CACHE_MAX = 200_000


def _kiwi_item_to_tokens(raw_form: Any, raw_tag: Any) -> tuple[str, ...]:
    form = str(raw_form or "").strip().lower()
    tag = str(raw_tag or "")
    if not form:
        return ()
    if tag.startswith(_KEEP_KIWI_TAG_PREFIXES) or _is_article_no(form):
        return tuple(_expand_token(form))
    return ()


def _kiwi_items_to_tokens(analyzed: Iterable[Any]) -> list[str]:
    tokens: list[str] = []
    cache = _KIWI_ITEM_TOKEN_CACHE
    for item in analyzed:
        raw_form = getattr(item, "form", "")
        raw_tag = getattr(item, "tag", "")
        if type(raw_form) is str and type(raw_tag) is str:
            key = (raw_form, raw_tag)
            cached = cache.get(key)
            if cached is None:
                cached = _kiwi_item_to_tokens(raw_form, raw_tag)
                if len(cache) < _KIWI_ITEM_TOKEN_CACHE_MAX and len(raw_form) <= 128:
                    cache[key] = cached
            tokens.extend(cached)
        else:
            tokens.extend(_kiwi_item_to_tokens(raw_form, raw_tag))
    return tokens


def _regex_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        tokens.extend(_expand_token(raw))
    return tokens


def _expand_token(token: str) -> list[str]:
    # Only a pure, short-word transform is shared. Return a fresh list so a
    # caller cannot mutate another request's token expansion. Full documents
    # and oversized words are never retained by this bounded cache.
    if not isinstance(token, str) or len(token) > 128:
        return _expand_token_uncached(token)
    return list(_cached_expanded_token(token))


@lru_cache(maxsize=2048)
def _cached_expanded_token(token: str) -> tuple[str, ...]:
    return tuple(_expand_token_uncached(token))


def _expand_token_uncached(token: str) -> list[str]:
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
