from __future__ import annotations

import re


REDACTED_AUTHORING_REASON = "작성 사유에 로컬 절대 경로가 포함되어 내용이 숨겨졌습니다."
_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|\\\\|(?<![\w/:])/(?!/)(?=[^\s]))"
)


def sanitize_authoring_reason(value: object, *, max_chars: int = 1000) -> str:
    """Return single-line authoring guidance without a local absolute path."""

    raw_text = str(value or "")
    if _ABSOLUTE_PATH_RE.search(raw_text):
        return REDACTED_AUTHORING_REASON
    return " ".join(raw_text.split())[:max_chars]
