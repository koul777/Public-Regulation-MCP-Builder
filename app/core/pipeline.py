from __future__ import annotations

import hashlib
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.agents.review_policy import AgentReviewPolicy
from app.processors.chunker import CHUNKER_VERSION
from app.processors.kordoc_table_parser import resolve_kordoc_command, split_command
from app.schemas.chunk import ChunkOptions


PREPROCESSOR_PIPELINE_VERSION = "2026.10.08-combined-book-typeset-boundaries-1"
_SAFE_VERSION = re.compile(r"(?<![A-Za-z0-9])v?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)(?![A-Za-z0-9])")


def processing_options_payload(
    options: ChunkOptions,
    *,
    settings: Any | None = None,
    quality_profiles_sha256: str | None = None,
) -> dict:
    payload = options.model_dump(mode="json")
    payload["pipeline_version"] = PREPROCESSOR_PIPELINE_VERSION
    payload["chunker_version"] = CHUNKER_VERSION
    payload["main_ai_review_stage"] = "parser_ai_review_draft"
    profile_hash = quality_profiles_sha256 or ""
    if settings is not None:
        if not profile_hash:
            profile_hash = quality_profile_config_hash(getattr(settings, "quality_profiles_path", ""))
        if profile_hash:
            payload["quality_profiles_sha256"] = profile_hash
        if bool(getattr(settings, "quality_profiles_strict", False)):
            payload["quality_profiles_strict"] = True
        agent_review_policy = AgentReviewPolicy(settings)
        request_enabled = bool(options.enable_agent_review)
        provider_execution_ready = agent_review_policy.provider_execution_configured()
        payload["agent_review_request_enabled"] = request_enabled
        payload["agent_review_cache_scope_hash"] = agent_review_policy.cache_scope_hash()
        payload["agent_review_provider_execution_ready"] = provider_execution_ready
        payload["agent_review_provider_execution_enabled"] = request_enabled and provider_execution_ready
        payload["kordoc_table_parser_enabled"] = bool(getattr(settings, "enable_kordoc_table_parser", False))
        if bool(getattr(settings, "enable_kordoc_table_parser", False)):
            payload["kordoc_table_as_main"] = bool(getattr(settings, "kordoc_table_as_main", False))
            payload["kordoc_table_promote_min_match"] = str(
                getattr(settings, "kordoc_table_promote_min_match", "") or ""
            )
            payload["kordoc_table_max_tables"] = int(getattr(settings, "kordoc_table_max_tables", 0) or 0)
            command_status = kordoc_table_command_status(str(getattr(settings, "kordoc_table_command", "") or ""))
            payload["kordoc_table_command_label"] = command_status["label"]
            payload["kordoc_table_command_available"] = command_status["available"]
            if command_status.get("resolved_name"):
                payload["kordoc_table_command_resolved_name"] = command_status["resolved_name"]
            if command_status.get("version"):
                payload["kordoc_table_command_version"] = command_status["version"]
        pdf_ocr_backend = str(getattr(settings, "pdf_ocr_backend", "") or "").strip()
        if pdf_ocr_backend:
            payload["pdf_ocr_backend"] = pdf_ocr_backend
            payload["pdf_ocr_language"] = str(getattr(settings, "pdf_ocr_language", "") or "")
            payload["pdf_ocr_render_scale"] = getattr(settings, "pdf_ocr_render_scale", "")
            payload["pdf_ocr_timeout_seconds"] = getattr(settings, "pdf_ocr_timeout_seconds", "")
            pdf_ocr_max_pages = int(getattr(settings, "pdf_ocr_max_pages", 0) or 0)
            if pdf_ocr_max_pages > 0:
                payload["pdf_ocr_max_pages"] = pdf_ocr_max_pages
    elif profile_hash:
        payload["quality_profiles_sha256"] = profile_hash
    return payload


@lru_cache(maxsize=32)
def kordoc_table_command_status(command: str) -> dict[str, Any]:
    parts = _split_command_for_status(command)
    label = parts[0].replace("\\", "/").rsplit("/", 1)[-1] if parts else ""
    status: dict[str, Any] = {
        "label": label,
        "available": False,
        "resolved_name": "",
        "version": "",
        "reason": "command_empty" if not parts else "command_not_found",
    }
    if not label:
        return status
    resolved = resolve_kordoc_command(parts[0])
    if not resolved:
        return status
    # Preserve a stable evidence label even when a Windows path is inspected
    # by a non-Windows CI runner (and vice versa).
    status["resolved_name"] = str(resolved).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    version = _command_version(resolved, *parts[1:])
    if version:
        status["available"] = True
        status["version"] = version
        status["reason"] = "" if version != "unverified" else "version_unrecognized"
    else:
        status["reason"] = "version_probe_failed"
    return status


def _split_command_for_status(command: str) -> list[str]:
    command = str(command or "").strip()
    if not command:
        return []
    return split_command(command)


def _command_version(resolved: str, *configured_args: str) -> str:
    """Probe the actual configured CLI, returning only a safe version token."""
    argv = [resolved, *configured_args, "--version"]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    elif os.name == "nt" and resolved.lower().endswith(".ps1"):
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *argv]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        completed = subprocess.run(
            argv,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            **options,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    text = (completed.stdout or completed.stderr or "")[:4096].strip()
    if not text:
        return ""
    match = _SAFE_VERSION.search(text.splitlines()[0][:256])
    return match.group(1)[:80] if match else "unverified"


def quality_profile_config_hash(path: str | Path | None) -> str:
    if not path:
        return ""
    profile_path = Path(path).expanduser()
    if not profile_path.exists():
        return ""
    return hashlib.sha256(profile_path.read_bytes()).hexdigest()
