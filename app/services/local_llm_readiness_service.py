"""One read-only local model diagnostic shared by CLI and operator screens."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.rag.local_llm import local_llm_available, probe_local_llm
from app.services.readiness_adapter import ReadinessCard, adapt_readiness_report


def diagnose_local_llm_settings(
    settings: Settings,
    *,
    probe: bool = True,
    availability_check: Callable[[Settings], bool] | None = None,
    probe_runner: Callable[[Settings], object] | None = None,
) -> dict[str, Any]:
    """Check configuration, then optionally probe; never return provider output.

    Only fixed reason codes, booleans and an allowlisted loopback hostname leave
    this boundary. A configured endpoint alone does not prove model readiness.
    """
    backend = str(settings.rag_llm_backend or "extractive").strip().lower()
    report: dict[str, Any] = {
        "report_type": "local_llm_doctor_v1",
        "backend": backend if backend in {"extractive", "ollama", "llama-cpp", "openai-compatible"} else "unsupported",
        "passed": False,
        "probe": False,
        "endpoint_host": None,
    }
    if backend == "extractive":
        return {**report, "passed": True, "model": None, "reason": "model_free_mode"}
    check = availability_check or local_llm_available
    try:
        endpoint = urlsplit(str(settings.rag_llm_endpoint or ""))
        allowed = endpoint.username is None and endpoint.password is None and check(settings) is True
    except (OSError, ValueError):
        allowed = False
    report["endpoint_allowed"] = allowed
    if not allowed:
        return {**report, "reason": "endpoint_not_allowed_or_missing"}
    if not probe:
        return {**report, "passed": True, "reason": "local_endpoint_configuration_valid"}
    report["probe"] = True
    try:
        health = (probe_runner or probe_local_llm)(settings)
    except Exception:
        return {**report, "reason": "local_probe_failed"}
    if (
        not isinstance(health, Mapping)
        or type(health.get("available")) is not bool
        or ("checked" in health and type(health["checked"]) is not bool)
    ):
        return {**report, "reason": "local_probe_invalid"}
    available = health["available"] and health.get("checked") is not False
    host = health.get("endpoint_host")
    report["endpoint_host"] = host if host in ("127.0.0.1", "localhost", "::1") else None
    # Keep the CLI's successful model field, using configuration rather than
    # an untrusted response. Never use it in beginner-facing status text.
    if available:
        report["model"] = settings.rag_llm_model
    return {
        **report,
        "passed": available,
        "health": {"available": available, "checked": True},
        "reason": "available" if available else "local_backend_unavailable",
    }


def check_local_llm_readiness(
    settings: Settings,
    *,
    probe: bool = True,
    probe_runner: Callable[[Settings], object] | None = None,
) -> ReadinessCard:
    """Return safe cause/action text without runtime paths or raw diagnostics."""
    report = diagnose_local_llm_settings(settings, probe=probe, probe_runner=probe_runner)
    return adapt_readiness_report(report, component="로컬 QA")
