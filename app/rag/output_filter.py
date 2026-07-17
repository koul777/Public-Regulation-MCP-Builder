from __future__ import annotations

import re

from app.core.api_audit import redact_sensitive_paths


OUTPUT_SECRET_PATTERNS = (
    re.compile(
        r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|password|private[_-]?key)\b[\"']?\s*[:=]\s*[\"'])[^\s\"',;}]+([\"'])"
    ),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|password|private[_-]?key)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)
OUTPUT_LOCAL_PATH_PATTERNS = (
    re.compile(r"(?i)(?<![\w.-])(?:~|/(?:etc|root|home|Users|var|tmp|mnt|workspace|data|app))/[^\s\"'<>`,;)]+"),
)


def sanitize_rag_answer(answer: str) -> str:
    sanitized = redact_sensitive_paths(str(answer or ""))
    for pattern in OUTPUT_LOCAL_PATH_PATTERNS:
        sanitized = pattern.sub("[local-path-redacted]", sanitized)
    for pattern in OUTPUT_SECRET_PATTERNS:
        sanitized = pattern.sub(_redact_secret_match, sanitized)
    return sanitized


def _redact_secret_match(match: re.Match[str]) -> str:
    if match.lastindex == 2:
        return f"{match.group(1)}[secret-redacted]{match.group(2)}"
    return "[secret-redacted]"
