from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "mcp-connection-diagnostic-v1"
STAGE_ORDER = (
    "registration",
    "loader",
    "transport",
    "fresh_app_server",
    "desktop_reload",
    "desktop_surface",
    "conversation",
)
CORE_CONFIGURATION_STAGES = STAGE_ORDER[:4]
STAGE_STATES = frozenset({"not_checked", "pending", "verified", "failed", "stale"})

_STATE_ALIASES = {
    "complete": "verified",
    "completed": "verified",
    "connected": "verified",
    "error": "failed",
    "fail": "failed",
    "ok": "verified",
    "passed": "verified",
    "success": "verified",
    "unverified": "pending",
    "waiting": "pending",
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@+-]{1,256}$")
_SAFE_REASON = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:[\\/][^\s,;\"']+|\\\\[^\s,;\"']+)")
_POSIX_SENSITIVE_PATH = re.compile(
    r"(?i)/(?:users|home|workspace|tmp|var|etc|opt)(?:/[^\s,;\"']*)?"
)
_PROFILE_PATH = re.compile(r"(?i)(?:%userprofile%|\$home|~)[\\/][^\s,;\"']+")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "bearer",
    "command",
    "cookie",
    "credential",
    "cwd",
    "environment",
    "password",
    "path",
    "secret",
    "token",
)
_CONVERSATION_RESULT_IDS = (
    "result_nonce",
    "result_nonce_hash",
    "verification_nonce_hash",
)


def build_connection_diagnostic(
    stages: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    attempt_id: str | None,
    config_fingerprint: str | None,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Normalize current-attempt MCP evidence without performing I/O.

    A verified stage is accepted only when its attempt and configuration
    fingerprint match the current diagnostic inputs. The final ``connected``
    state additionally requires explicit proof of a tool call made from the
    current conversation.
    """

    current_attempt = _safe_identifier_value(attempt_id)
    current_fingerprint = _safe_identifier_value(config_fingerprint)
    normalized_checked_at = _normalize_checked_at(checked_at)
    raw_stages = _coerce_stage_mapping(stages)

    normalized_stages: dict[str, dict[str, Any]] = {}
    for stage_name in STAGE_ORDER:
        normalized_stages[stage_name] = _normalize_stage(
            stage_name,
            raw_stages.get(stage_name),
            current_attempt=current_attempt,
            current_fingerprint=current_fingerprint,
            default_checked_at=normalized_checked_at,
        )

    configured = all(
        normalized_stages[name]["state"] == "verified" for name in CORE_CONFIGURATION_STAGES
    )
    all_verified = all(stage["state"] == "verified" for stage in normalized_stages.values())
    conversation_proved = _normalized_conversation_has_proof(normalized_stages["conversation"])
    connected = all_verified and conversation_proved
    overall_state = "connected" if connected else "configured" if configured else "pending"
    first_blocking_stage = next(
        (name for name in STAGE_ORDER if normalized_stages[name]["state"] != "verified"),
        None,
    )
    support_summary, next_action = _support_guidance(
        overall_state=overall_state,
        first_blocking_stage=first_blocking_stage,
        stages=normalized_stages,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": current_attempt,
        "config_fingerprint": current_fingerprint,
        "checked_at": normalized_checked_at,
        "stage_order": list(STAGE_ORDER),
        "stages": normalized_stages,
        "overall_state": overall_state,
        "configured": configured,
        "pending": not connected,
        "connected": connected,
        "has_failures": any(stage["state"] == "failed" for stage in normalized_stages.values()),
        "stale_stages": [
            name for name, stage in normalized_stages.items() if stage["state"] == "stale"
        ],
        "first_blocking_stage": first_blocking_stage,
        "support_summary": support_summary,
        "next_action": next_action,
    }


def diagnostic_from_bundle_status(
    bundle_status: Mapping[str, Any],
    *,
    attempt_id: str | None = None,
    config_fingerprint: str | None = None,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Convert legacy ``bundle_status.json`` fields conservatively.

    Legacy Desktop surface and end-to-end booleans have no per-attempt
    conversation proof. They are retained as sanitized evidence but never
    promoted to current ``verified`` stages by this converter.
    """

    status = dict(bundle_status or {})
    source_attempt = _safe_identifier_value(
        status.get("installation_attempt_id") or status.get("attempt_id")
    )
    source_fingerprint = _safe_identifier_value(
        status.get("installed_config_fingerprint") or status.get("config_fingerprint")
    )
    current_attempt = _safe_identifier_value(attempt_id) if attempt_id is not None else source_attempt
    current_fingerprint = (
        _safe_identifier_value(config_fingerprint)
        if config_fingerprint is not None
        else source_fingerprint
    )
    explicit_legacy_binding = bool(
        attempt_id is not None
        and config_fingerprint is not None
        and current_attempt
        and current_fingerprint
    )
    source_identity_present = bool(source_attempt or source_fingerprint)
    evidence_attempt = (
        source_attempt if source_identity_present else current_attempt if explicit_legacy_binding else None
    )
    evidence_fingerprint = (
        source_fingerprint
        if source_identity_present
        else current_fingerprint
        if explicit_legacy_binding
        else None
    )
    generic_checked_at = _first_checked_at(
        checked_at,
        status.get("updated_at"),
        status.get("generated_at"),
        status.get("generated_at_utc"),
    )
    attributed = bool(
        evidence_attempt
        and evidence_fingerprint
        and evidence_attempt == current_attempt
        and evidence_fingerprint == current_fingerprint
    )

    def legacy_stage(
        claimed: bool,
        *,
        false_reason: str,
        evidence: Mapping[str, Any],
        stage_checked_at: Any = None,
    ) -> dict[str, Any]:
        if claimed and attributed:
            state = "verified"
            reason_code = "ok"
        elif claimed:
            state = "pending"
            reason_code = "legacy_evidence_unattributed"
        else:
            state = "pending"
            reason_code = false_reason
        return _legacy_stage_payload(
            state=state,
            reason_code=reason_code,
            attempt_id=evidence_attempt,
            config_fingerprint=evidence_fingerprint,
            checked_at=_first_checked_at(stage_checked_at, generic_checked_at),
            evidence=evidence,
        )

    direct_registered = bool(status.get("direct_config_registered"))
    plugin_registered = bool(status.get("plugin_registered"))
    direct_loader = bool(status.get("direct_config_loader_verified"))
    plugin_loader = bool(status.get("plugin_loader_verified"))
    registration_source = (
        "direct"
        if direct_registered and not plugin_registered
        else "plugin"
        if plugin_registered and not direct_registered
        else None
    )
    connection_source = (
        "direct"
        if registration_source == "direct" and direct_loader and not plugin_loader
        else "plugin"
        if registration_source == "plugin" and plugin_loader and not direct_loader
        else None
    )
    runtime_fingerprint = _safe_identifier_value(status.get("runtime_fingerprint"))
    if connection_source == "direct":
        transport_claimed = bool(
            status.get("installed_config_transport_verified")
            and status.get("direct_stdio_verified")
            and status.get("transport_end_to_end_verified")
        )
        transport_runtime_fingerprint = _safe_identifier_value(
            status.get("installed_config_transport_runtime_fingerprint")
        )
    elif connection_source == "plugin":
        transport_claimed = bool(
            status.get("plugin_stdio_verified")
            and status.get("transport_end_to_end_verified")
        )
        transport_runtime_fingerprint = _safe_identifier_value(
            status.get("plugin_stdio_runtime_fingerprint")
        )
    else:
        transport_claimed = False
        transport_runtime_fingerprint = None
    transport_runtime_bound = bool(
        runtime_fingerprint
        and transport_runtime_fingerprint
        and transport_runtime_fingerprint == runtime_fingerprint
    )
    transport_verified = bool(transport_claimed and transport_runtime_bound)
    app_server_claimed = bool(
        status.get("fresh_codex_app_server_inventory_verified")
        and status.get("desktop_app_server_loader_verified")
    )
    app_server_runtime_fingerprint = _safe_identifier_value(
        status.get("fresh_codex_app_server_runtime_fingerprint")
    )
    app_server_runtime_bound = bool(
        runtime_fingerprint
        and app_server_runtime_fingerprint
        and app_server_runtime_fingerprint == runtime_fingerprint
    )
    app_server_verified = bool(connection_source and app_server_claimed and app_server_runtime_bound)

    stages: dict[str, dict[str, Any]] = {
        "registration": legacy_stage(
            registration_source is not None,
            false_reason=(
                "registration_source_ambiguous"
                if direct_registered and plugin_registered
                else "registration_not_verified"
            ),
            stage_checked_at=status.get("desktop_mcp_registration_updated_at"),
            evidence={
                "legacy_source": True,
                "direct_config_registered": direct_registered,
                "plugin_registered": plugin_registered,
                "connection_source_id": registration_source,
            },
        ),
        "loader": legacy_stage(
            connection_source is not None,
            false_reason=(
                "loader_source_mismatch"
                if direct_loader or plugin_loader
                else "loader_not_verified"
            ),
            evidence={
                "legacy_source": True,
                "direct_config_loader_verified": direct_loader,
                "plugin_loader_verified": plugin_loader,
                "connection_source_id": connection_source,
            },
        ),
        "transport": legacy_stage(
            transport_verified,
            false_reason=(
                "transport_runtime_fingerprint_mismatch"
                if transport_claimed and not transport_runtime_bound
                else "transport_not_verified"
            ),
            evidence={
                "legacy_source": True,
                "connection_source_id": connection_source,
                "direct_stdio_verified": bool(status.get("direct_stdio_verified")),
                "installed_config_transport_verified": bool(
                    status.get("installed_config_transport_verified")
                ),
                "plugin_stdio_verified": bool(status.get("plugin_stdio_verified")),
                "transport_end_to_end_verified": bool(
                    status.get("transport_end_to_end_verified")
                ),
                "runtime_fingerprint_bound": transport_runtime_bound,
            },
        ),
        "fresh_app_server": legacy_stage(
            app_server_verified,
            false_reason=(
                "fresh_app_server_runtime_fingerprint_mismatch"
                if app_server_claimed and not app_server_runtime_bound
                else "fresh_app_server_not_verified"
            ),
            evidence={
                "legacy_source": True,
                "connection_source_id": connection_source,
                "desktop_app_server_loader_verified": app_server_verified,
                "fresh_codex_app_server_inventory_verified": bool(
                    status.get("fresh_codex_app_server_inventory_verified")
                ),
                "runtime_fingerprint_bound": app_server_runtime_bound,
                "tool_count": _safe_count(status.get("desktop_app_server_tool_count")),
            },
        ),
    }

    restart_required = status.get("desktop_restart_required")
    restart_status = str(status.get("desktop_restart_status") or "").strip().casefold()
    reload_verified = restart_required is False and restart_status == "up_to_date"
    if restart_required is True:
        reload_reason = "desktop_restart_required"
    elif restart_status == "not_running":
        reload_reason = "desktop_not_running"
    else:
        reload_reason = "desktop_reload_not_verified"
    stages["desktop_reload"] = legacy_stage(
        reload_verified,
        false_reason=reload_reason,
        stage_checked_at=status.get("desktop_restart_checked_at"),
        evidence={
            "legacy_source": True,
            "desktop_process_detected": bool(status.get("desktop_process_detected")),
            "desktop_restart_required": restart_required,
            "desktop_restart_status": restart_status or None,
        },
    )

    legacy_surface_claim = bool(status.get("desktop_tool_scan_verified"))
    stages["desktop_surface"] = _legacy_stage_payload(
        state="pending",
        reason_code=(
            "legacy_surface_proof_not_current"
            if legacy_surface_claim
            else "desktop_surface_not_verified"
        ),
        attempt_id=evidence_attempt,
        config_fingerprint=evidence_fingerprint,
        checked_at=generic_checked_at,
        evidence={
            "legacy_source": True,
            "legacy_claimed_verified": legacy_surface_claim,
        },
    )

    legacy_conversation_claim = bool(
        status.get("conversation_attachment_verified") or status.get("end_to_end_verified")
    )
    stages["conversation"] = _legacy_stage_payload(
        state="pending",
        reason_code=(
            "legacy_conversation_proof_not_current"
            if legacy_conversation_claim
            else "conversation_proof_required"
        ),
        attempt_id=evidence_attempt,
        config_fingerprint=evidence_fingerprint,
        checked_at=generic_checked_at,
        evidence={
            "legacy_source": True,
            "legacy_claimed_attachment_verified": bool(
                status.get("conversation_attachment_verified")
            ),
            "legacy_claimed_end_to_end_verified": bool(status.get("end_to_end_verified")),
        },
    )

    return build_connection_diagnostic(
        stages,
        attempt_id=current_attempt,
        config_fingerprint=current_fingerprint,
        checked_at=generic_checked_at,
    )


def normalize_connection_diagnostic(
    stages: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    attempt_id: str | None,
    config_fingerprint: str | None,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Alias with a name suited to callers that already hold stage evidence."""

    return build_connection_diagnostic(
        stages,
        attempt_id=attempt_id,
        config_fingerprint=config_fingerprint,
        checked_at=checked_at,
    )


def convert_bundle_status(
    bundle_status: Mapping[str, Any],
    *,
    attempt_id: str | None = None,
    config_fingerprint: str | None = None,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias for :func:`diagnostic_from_bundle_status`."""

    return diagnostic_from_bundle_status(
        bundle_status,
        attempt_id=attempt_id,
        config_fingerprint=config_fingerprint,
        checked_at=checked_at,
    )


def _coerce_stage_mapping(
    stages: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(stages, Mapping):
        nested = stages.get("stages")
        source = nested if isinstance(nested, Mapping) else stages
        return {
            name: value
            for name, value in source.items()
            if name in STAGE_ORDER and isinstance(value, Mapping)
        }
    if isinstance(stages, Sequence) and not isinstance(stages, (str, bytes, bytearray)):
        answer: dict[str, Mapping[str, Any]] = {}
        for item in stages:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("stage") or item.get("name") or "").strip()
            if name in STAGE_ORDER:
                answer[name] = item
        return answer
    raise TypeError("stages must be a mapping or a sequence of stage mappings")


def _normalize_stage(
    stage_name: str,
    raw_stage: Mapping[str, Any] | None,
    *,
    current_attempt: str | None,
    current_fingerprint: str | None,
    default_checked_at: str | None,
) -> dict[str, Any]:
    supplied = raw_stage is not None
    raw = dict(raw_stage or {})
    state = _normalize_state(raw.get("state", raw.get("verified"))) if supplied else "not_checked"
    reason_code = _normalize_reason(raw.get("reason_code"))
    stage_attempt = _safe_identifier_value(raw.get("attempt_id"))
    raw_evidence = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    stage_fingerprint = _safe_identifier_value(
        raw.get("config_fingerprint", raw_evidence.get("config_fingerprint"))
    )
    stage_checked_at = _first_checked_at(raw.get("checked_at"), default_checked_at)

    if supplied and state != "not_checked":
        if current_attempt and stage_attempt and stage_attempt != current_attempt:
            state = "stale"
            reason_code = "stale_attempt"
        elif current_attempt and not stage_attempt and state in {"verified", "failed", "stale"}:
            state = "stale"
            reason_code = "evidence_attempt_missing"
        elif not current_attempt and state == "verified":
            state = "pending"
            reason_code = "current_attempt_missing"
        elif current_fingerprint and stage_fingerprint and stage_fingerprint != current_fingerprint:
            state = "stale"
            reason_code = "stale_config_fingerprint"
        elif current_fingerprint and not stage_fingerprint and state in {"verified", "failed", "stale"}:
            state = "stale"
            reason_code = "evidence_config_fingerprint_missing"
        elif not current_fingerprint and state == "verified":
            state = "pending"
            reason_code = "current_config_fingerprint_missing"

    if stage_name == "conversation" and state == "verified" and not _has_conversation_proof(raw_evidence):
        state = "pending"
        reason_code = "conversation_proof_missing"

    if not reason_code:
        reason_code = "ok" if state == "verified" else state

    evidence = _sanitize_evidence(raw_evidence)
    if stage_fingerprint and "config_fingerprint" not in evidence:
        evidence["config_fingerprint"] = stage_fingerprint
    return {
        "state": state,
        "attempt_id": stage_attempt,
        "checked_at": stage_checked_at,
        "reason_code": reason_code,
        "evidence": evidence,
    }


def _legacy_stage_payload(
    *,
    state: str,
    reason_code: str,
    attempt_id: str | None,
    config_fingerprint: str | None,
    checked_at: str | None,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    answer = dict(evidence)
    if config_fingerprint:
        answer["config_fingerprint"] = config_fingerprint
    return {
        "state": state,
        "attempt_id": attempt_id,
        "checked_at": checked_at,
        "reason_code": reason_code,
        "evidence": answer,
    }


def _normalize_state(value: Any) -> str:
    if value is True:
        return "verified"
    if value is False or value is None:
        return "pending"
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = _STATE_ALIASES.get(normalized, normalized)
    return normalized if normalized in STAGE_STATES else "pending"


def _normalize_reason(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized if _SAFE_REASON.fullmatch(normalized) else None


def _safe_identifier_value(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if _SAFE_IDENTIFIER.fullmatch(normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def _normalize_checked_at(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _first_checked_at(*values: Any) -> str | None:
    for value in values:
        normalized = _normalize_checked_at(value)
        if normalized:
            return normalized
    return None


def _has_conversation_proof(evidence: Mapping[str, Any]) -> bool:
    if evidence.get("tool_call_verified") is not True:
        return False
    if not all(
        bool(str(evidence.get(name) or "").strip())
        for name in ("server_name", "tool_name", "conversation_id")
    ):
        return False
    return any(
        bool(str(evidence.get(name) or "").strip()) for name in _CONVERSATION_RESULT_IDS
    )


def _normalized_conversation_has_proof(stage: Mapping[str, Any]) -> bool:
    evidence = stage.get("evidence")
    return isinstance(evidence, Mapping) and _has_conversation_proof(evidence)


def _sanitize_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_evidence_value(str(key), value) for key, value in evidence.items()}


def _sanitize_evidence_value(key: str, value: Any) -> Any:
    key_folded = key.casefold()
    if key_folded != "config_fingerprint" and any(
        part in key_folded for part in _SENSITIVE_KEY_PARTS
    ):
        return "[redacted]"
    if isinstance(value, Mapping):
        return _sanitize_evidence(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_evidence_value(key, item) for item in value]
    if isinstance(value, str):
        return _redact_paths(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _redact_paths(value: str) -> str:
    answer = _WINDOWS_PATH.sub("[redacted-path]", value)
    answer = _POSIX_SENSITIVE_PATH.sub("[redacted-path]", answer)
    return _PROFILE_PATH.sub("[redacted-path]", answer)


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _support_guidance(
    *,
    overall_state: str,
    first_blocking_stage: str | None,
    stages: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str]:
    if overall_state == "connected":
        return (
            "Connection verified through a current conversation tool call.",
            "No connection action is required.",
        )
    if first_blocking_stage is None:
        return (
            "Connection evidence is incomplete.",
            "Run the connection diagnostic again for the current attempt.",
        )

    reason_code = str(stages[first_blocking_stage].get("reason_code") or "pending")
    if reason_code in {
        "stale_attempt",
        "stale_config_fingerprint",
        "evidence_attempt_missing",
        "evidence_config_fingerprint_missing",
        "legacy_evidence_unattributed",
    }:
        action = "Discard old evidence and rerun this stage for the current configuration attempt."
    elif first_blocking_stage == "registration":
        action = "Install and verify the MCP registration for the current configuration."
    elif first_blocking_stage == "loader":
        action = "Run a fresh loader inventory check for the registered MCP server."
    elif first_blocking_stage == "transport":
        action = "Run the direct MCP protocol smoke check with the registered launch contract."
    elif first_blocking_stage == "fresh_app_server":
        action = "Start a fresh app-server process and verify the expected MCP tools."
    elif first_blocking_stage == "desktop_reload":
        action = "Fully quit and restart Desktop, then run the post-restart check."
    elif first_blocking_stage == "desktop_surface":
        action = "Open a new Desktop conversation and confirm the server in the MCP picker."
    else:
        action = "Run the generated conversation verification prompt and confirm its proof."

    if overall_state == "configured":
        summary = "MCP configuration is current; Desktop or conversation verification is still pending."
    else:
        label = first_blocking_stage.replace("_", " ")
        summary = f"MCP connection verification is incomplete at the {label} stage."
    return summary, action


__all__ = [
    "SCHEMA_VERSION",
    "CORE_CONFIGURATION_STAGES",
    "STAGE_ORDER",
    "STAGE_STATES",
    "build_connection_diagnostic",
    "convert_bundle_status",
    "diagnostic_from_bundle_status",
    "normalize_connection_diagnostic",
]
