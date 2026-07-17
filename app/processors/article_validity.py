from __future__ import annotations

from typing import Any


def build_article_validity_windows(
    *,
    effective_date: str | None,
    article_effective_overrides: list[dict[str, Any]] | None = None,
    revision_history: list[dict[str, Any]] | None = None,
) -> list[dict[str, str | None]]:
    windows: list[dict[str, str | None]] = []
    default_valid_from = effective_date
    if default_valid_from:
        windows.append(
            {
                "article_ref": "*",
                "valid_from": default_valid_from,
                "valid_to": None,
                "source": "document_effective_date",
            }
        )
    for override in article_effective_overrides or []:
        article_ref = str(override.get("article_ref") or "").strip()
        valid_from = str(override.get("effective_date") or "").strip() or None
        if not article_ref or not valid_from:
            continue
        windows.append(
            {
                "article_ref": article_ref,
                "valid_from": valid_from,
                "valid_to": None,
                "source": "article_effective_override",
            }
        )
    latest_revision_effective = _latest_revision_effective_date(revision_history or [])
    if latest_revision_effective:
        windows.append(
            {
                "article_ref": "*",
                "valid_from": latest_revision_effective,
                "valid_to": None,
                "source": "latest_revision_history_effective_date",
            }
        )
    return _dedupe_windows(windows)


def summarize_article_validity_windows(windows: list[dict[str, str | None]]) -> dict[str, Any]:
    override_windows = [item for item in windows if item.get("source") == "article_effective_override"]
    return {
        "window_count": len(windows),
        "override_window_count": len(override_windows),
        "default_window_count": len(windows) - len(override_windows),
        "article_refs": sorted({str(item.get("article_ref") or "") for item in override_windows if item.get("article_ref")}),
    }


def _latest_revision_effective_date(revision_history: list[dict[str, Any]]) -> str | None:
    for event in reversed(revision_history):
        value = event.get("effective_date") or event.get("date")
        if value:
            return str(value)
    return None


def _dedupe_windows(windows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
    result: list[dict[str, str | None]] = []
    for window in windows:
        key = (
            window.get("article_ref"),
            window.get("valid_from"),
            window.get("valid_to"),
            window.get("source"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(window)
    return result
