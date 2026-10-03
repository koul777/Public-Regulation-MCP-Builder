"""Remember the operator's AI review choices between restarts, without the key.

The sidebar toggle used to live only in process memory. An operator who
turned AI review on, closed the app for the day, and preprocessed the next
morning got "AI 검수 사용 안 함" on every article with no hint that the switch
had reset. Persisting the non-secret part of the choice (switch, provider,
model, address, limits) under the runtime data directory makes the switch
survive a restart; the screen then asks for the API key explicitly instead
of silently skipping the review.

API keys are deliberately excluded. If the operator wants the key to survive
too, they opt into writing ``.env`` from the sidebar.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PREFERENCES_FILENAME = "ai_review_preferences.json"

BOOL_FIELDS = ("enable_agent_review",)
TEXT_FIELDS = (
    "llm_provider",
    "agent_review_model",
    "agent_review_api_base_url",
    "azure_openai_endpoint",
    "anthropic_api_base_url",
)
INT_FIELDS = (
    "agent_review_max_chunks_per_document",
    "agent_review_max_input_tokens_per_document",
)
PREFERENCE_FIELDS: tuple[str, ...] = BOOL_FIELDS + TEXT_FIELDS + INT_FIELDS
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "openai_api_key",
        "openai_compatible_api_key",
        "azure_openai_api_key",
        "anthropic_api_key",
        "api_auth_token",
        "api_auth_tokens",
    }
)


def preferences_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / PREFERENCES_FILENAME


def _coerce(field: str, value: Any) -> Any:
    if field in BOOL_FIELDS:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if field in INT_FIELDS:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None
    text = str(value or "").strip()
    return text or None


def select_preferences(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the persistable, non-secret fields with normalized types."""

    selected: dict[str, Any] = {}
    for field in PREFERENCE_FIELDS:
        if field in SECRET_FIELDS or field not in values:
            continue
        coerced = _coerce(field, values[field])
        if coerced is None:
            continue
        selected[field] = coerced
    return selected


def save_ai_review_preferences(data_dir: Path | str, values: Mapping[str, Any]) -> Path:
    """Write the non-secret AI review preferences; secrets are dropped even if passed."""

    target = preferences_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "preferences": select_preferences(values)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_ai_review_preferences(data_dir: Path | str) -> dict[str, Any]:
    """Return saved preferences, or an empty dict when nothing usable is stored."""

    target = preferences_path(data_dir)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    preferences = raw.get("preferences") if isinstance(raw, dict) else None
    if not isinstance(preferences, Mapping):
        return {}
    return select_preferences(preferences)


def clear_ai_review_preferences(data_dir: Path | str) -> None:
    target = preferences_path(data_dir)
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
