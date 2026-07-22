from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "mcp-bundle-status-v5"
CLIENT_SCHEMA_VERSION = "mcp-client-connection-v1"
STATUS_MODEL = "client-connections-v1"

CLIENT_TARGETS = (
    "claude-code",
    "codex",
    "claude-desktop",
    "chatgpt-desktop-local",
    "chatgpt-remote",
    "chatgpt-tunnel",
    "claude-api",
)

STAGE_ORDER = (
    "registration",
    "loader",
    "transport",
    "fresh_app_server",
    "client_reload",
    "client_surface",
    "conversation",
)

_TARGET_POLICIES: dict[str, dict[str, Any]] = {
    "claude-code": {
        "transport": "stdio",
        "config_resource_id": "claude-code-user-config",
        "configuration_required_stages": ("registration", "loader", "transport"),
        "connection_required_stages": (
            "registration",
            "loader",
            "transport",
            "client_reload",
            "client_surface",
            "conversation",
        ),
    },
    "codex": {
        "transport": "stdio",
        "config_resource_id": "codex-host-user-config",
        "configuration_required_stages": (
            "registration",
            "loader",
            "transport",
            "fresh_app_server",
        ),
        "connection_required_stages": STAGE_ORDER,
    },
    "claude-desktop": {
        "transport": "stdio",
        "config_resource_id": "claude-desktop-user-config",
        "configuration_required_stages": ("registration", "transport"),
        "connection_required_stages": (
            "registration",
            "loader",
            "transport",
            "client_reload",
            "client_surface",
            "conversation",
        ),
    },
    "chatgpt-desktop-local": {
        "transport": "stdio",
        "config_resource_id": "codex-host-user-config",
        "configuration_required_stages": (
            "registration",
            "loader",
            "transport",
            "fresh_app_server",
        ),
        "connection_required_stages": STAGE_ORDER,
    },
    "chatgpt-remote": {
        "transport": "streamable-http",
        "config_resource_id": "chatgpt-remote-connector",
        "configuration_required_stages": ("transport", "registration", "loader"),
        "connection_required_stages": (
            "transport",
            "registration",
            "loader",
            "client_surface",
            "conversation",
        ),
    },
    "chatgpt-tunnel": {
        "transport": "secure-mcp-tunnel",
        "config_resource_id": "chatgpt-secure-tunnel",
        "configuration_required_stages": ("transport", "registration", "loader"),
        "connection_required_stages": (
            "transport",
            "registration",
            "loader",
            "client_surface",
            "conversation",
        ),
    },
    "claude-api": {
        "transport": "streamable-http",
        "config_resource_id": "claude-https-connector",
        "configuration_required_stages": ("transport", "registration"),
        "connection_required_stages": (
            "transport",
            "registration",
            "loader",
            "conversation",
        ),
    },
}

_LOCAL_TARGETS = frozenset(
    {"claude-code", "codex", "claude-desktop", "chatgpt-desktop-local"}
)
_REMOTE_TARGETS = frozenset({"chatgpt-remote", "chatgpt-tunnel", "claude-api"})
_SAFE_SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:@+-]{1,256}")
_SAFE_REASON = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_SHA256_FINGERPRINT = re.compile(r"sha256:[0-9a-fA-F]{64}")
_SENSITIVE_KEY_PARTS = (
    "args",
    "authorization",
    "command",
    "cookie",
    "credential",
    "cwd",
    "environment",
    "password",
    "path",
    "secret",
    "token",
    "url",
)


def create_bundle_status(
    server_name: str,
    *,
    runtime_fingerprint: object | None = None,
    bundle_fingerprint: object | None = None,
    generated_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Create a v5 status document with one isolated record per supported client."""

    normalized_server_name = _server_name(server_name)
    normalized_runtime = _fingerprint(runtime_fingerprint)
    normalized_bundle = _fingerprint(bundle_fingerprint)
    timestamp = _timestamp(generated_at)
    connections = {
        target: _empty_client_connection(
            target,
            server_name=normalized_server_name,
            runtime_fingerprint=normalized_runtime,
            bundle_fingerprint=normalized_bundle,
        )
        for target in CLIENT_TARGETS
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status_model": STATUS_MODEL,
        "server_name": normalized_server_name,
        "generated_at": timestamp,
        "updated_at": timestamp,
        "runtime_fingerprint": normalized_runtime,
        "bundle_fingerprint": normalized_bundle,
        "active_target": None,
        "legacy_projection_target": None,
        "legacy_projection_updated_at": None,
        "legacy_migration_state": "not_required",
        "client_connections": connections,
        **_empty_legacy_projection(),
    }


def begin_attempt(
    status: Mapping[str, Any],
    target: str,
    attempt_id: object,
    *,
    started_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Begin one target attempt without clearing that target's effective baseline."""

    answer = _status_copy(status)
    record = _client_record(answer, target)
    if record["last_attempt"].get("state") == "in_progress":
        raise ValueError(f"An MCP client connection attempt is already active for {target}")
    timestamp = _timestamp(started_at)
    record["last_attempt"] = {
        "id": _identifier(attempt_id),
        "state": "in_progress",
        "started_at": timestamp,
        "finished_at": None,
        "reason_code": "in_progress",
        "rollback_complete": None,
    }
    answer["active_target"] = target
    answer["updated_at"] = timestamp
    return project_legacy(answer, target, projected_at=timestamp)


def commit_success(
    status: Mapping[str, Any],
    target: str,
    attempt_id: object,
    *,
    verified_stages: Mapping[str, Mapping[str, Any] | None] | Iterable[str],
    config_entry_fingerprint: object | None = None,
    config_container_fingerprint: object | None = None,
    runtime_fingerprint: object | None = None,
    bundle_fingerprint: object | None = None,
    bundle_location_fingerprint: object | None = None,
    verified_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Commit current-attempt evidence and derive configured/connected conservatively."""

    answer = _status_copy(status)
    record = _client_record(answer, target)
    attempt = _current_attempt(record, attempt_id)
    timestamp = _timestamp(verified_at)
    normalized_attempt_id = str(attempt["id"])
    normalized_config = _fingerprint(config_entry_fingerprint)
    normalized_container = _fingerprint(config_container_fingerprint)
    normalized_runtime = _fingerprint(runtime_fingerprint)
    normalized_bundle = _fingerprint(bundle_fingerprint)
    normalized_location = _fingerprint(bundle_location_fingerprint)

    if isinstance(verified_stages, Mapping):
        stage_items = verified_stages.items()
    else:
        stage_items = ((name, None) for name in verified_stages)
    stage_items = list(stage_items)
    stage_names = {name for name, _ in stage_items}
    if "registration" in stage_names and normalized_config is None:
        raise ValueError("registration verification requires a config entry fingerprint")
    if (
        target in _LOCAL_TARGETS
        and "registration" in stage_names
        and normalized_location is None
    ):
        raise ValueError("local registration verification requires a bundle location fingerprint")
    if any(name != "registration" for name in stage_names) and normalized_runtime is None:
        raise ValueError("runtime-bound verification requires a runtime fingerprint")
    # A completed attempt is an atomic evidence snapshot. Never combine stages
    # retained from an older config/runtime with a newly committed attempt.
    for stage_name in STAGE_ORDER:
        if stage_name not in stage_names:
            record["stages"][stage_name] = _empty_stage()
    seen: set[str] = set()
    for stage_name, raw_evidence in stage_items:
        if stage_name not in STAGE_ORDER:
            raise ValueError(f"Unknown MCP client connection stage: {stage_name}")
        if stage_name in seen:
            raise ValueError(f"Duplicate MCP client connection stage: {stage_name}")
        seen.add(stage_name)
        evidence = raw_evidence if isinstance(raw_evidence, Mapping) else {}
        if stage_name == "conversation" and not _has_conversation_proof(
            evidence,
            expected_server_name=str(record["server_name"]),
        ):
            raise ValueError(
                "conversation verification requires current tool-call proof"
            )
        record["stages"][stage_name] = {
            "state": "verified",
            "attempt_id": normalized_attempt_id,
            "checked_at": timestamp,
            "reason_code": "ok",
            "config_entry_fingerprint": normalized_config,
            "runtime_fingerprint": (
                None if stage_name == "registration" else normalized_runtime
            ),
            "evidence": _sanitize_evidence(evidence),
        }

    effective_state = _derived_effective_state(record)
    runtime_bound = any(stage_name != "registration" for stage_name in seen)
    record["effective"] = {
        "state": effective_state,
        "attempt_id": normalized_attempt_id,
        "bundle_fingerprint": normalized_bundle,
        "bundle_location_fingerprint": normalized_location,
        "config_entry_fingerprint": normalized_config,
        "config_container_fingerprint": normalized_container,
        "runtime_fingerprint": normalized_runtime if runtime_bound else None,
        "verified_at": timestamp,
    }
    record["last_attempt"] = {
        **attempt,
        "state": "completed",
        "finished_at": timestamp,
        "reason_code": "ok",
        "rollback_complete": None,
    }
    _refresh_readiness(record)
    if "registration" in seen:
        _invalidate_replaced_shared_config_siblings(
            answer,
            target=target,
            config_entry_fingerprint=normalized_config,
            bundle_location_fingerprint=normalized_location,
            runtime_fingerprint=normalized_runtime,
            checked_at=timestamp,
        )
    answer["active_target"] = target
    answer["updated_at"] = timestamp
    if normalized_runtime:
        answer["runtime_fingerprint"] = normalized_runtime
    if normalized_bundle:
        answer["bundle_fingerprint"] = normalized_bundle
    return project_legacy(answer, target, projected_at=timestamp)


def fail_rolled_back(
    status: Mapping[str, Any],
    target: str,
    attempt_id: object,
    *,
    reason_code: object,
    finished_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Record a failed attempt while preserving the prior effective connection exactly."""

    answer = _status_copy(status)
    record = _client_record(answer, target)
    attempt = _current_attempt(record, attempt_id)
    timestamp = _timestamp(finished_at)
    record["last_attempt"] = {
        **attempt,
        "state": "failed_rolled_back",
        "finished_at": timestamp,
        "reason_code": _reason(reason_code),
        "rollback_complete": True,
    }
    answer["active_target"] = target
    answer["updated_at"] = timestamp
    return project_legacy(answer, target, projected_at=timestamp)


def fail_unverified(
    status: Mapping[str, Any],
    target: str,
    attempt_id: object,
    *,
    reason_code: object,
    finished_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Terminate a failed attempt when external rollback was not confirmed.

    The prior effective baseline remains unchanged, but the failed attempt is
    explicitly marked non-rollback so a later retry is never blocked by a
    permanently ``in_progress`` record.
    """

    answer = _status_copy(status)
    record = _client_record(answer, target)
    attempt = _current_attempt(record, attempt_id)
    timestamp = _timestamp(finished_at)
    record["last_attempt"] = {
        **attempt,
        "state": "failed_unverified",
        "finished_at": timestamp,
        "reason_code": _reason(reason_code),
        "rollback_complete": False,
    }
    answer["active_target"] = target
    answer["updated_at"] = timestamp
    return project_legacy(answer, target, projected_at=timestamp)


def invalidate_runtime(
    status: Mapping[str, Any],
    previous_runtime_fingerprint: object,
    *,
    next_runtime_fingerprint: object | None = None,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Stale only evidence bound to the replaced local runtime fingerprint."""

    answer = _status_copy(status)
    previous = _fingerprint(previous_runtime_fingerprint)
    if previous is None:
        raise ValueError("previous runtime fingerprint is required")
    next_value = _fingerprint(next_runtime_fingerprint)
    timestamp = _timestamp(checked_at)
    for target in _LOCAL_TARGETS:
        record = _client_record(answer, target)
        if record["effective"].get("runtime_fingerprint") != previous:
            continue
        _stale_matching_stages(
            record,
            reason_code="runtime_changed",
            checked_at=timestamp,
            predicate=lambda stage: stage.get("runtime_fingerprint") == previous,
        )
        record["readiness"]["runtime_ready"] = bool(next_value)
    answer["runtime_fingerprint"] = next_value
    answer["updated_at"] = timestamp
    return _refresh_current_projection(answer, timestamp)


def invalidate_location(
    status: Mapping[str, Any],
    previous_location_fingerprint: object,
    *,
    next_location_fingerprint: object | None = None,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Stale local absolute-path registrations after a bundle relocation."""

    answer = _status_copy(status)
    previous = _fingerprint(previous_location_fingerprint)
    if previous is None:
        raise ValueError("previous bundle location fingerprint is required")
    next_value = _fingerprint(next_location_fingerprint)
    timestamp = _timestamp(checked_at)
    for target in _LOCAL_TARGETS:
        record = _client_record(answer, target)
        if record["effective"].get("bundle_location_fingerprint") != previous:
            continue
        _stale_matching_stages(
            record,
            reason_code="bundle_location_changed",
            checked_at=timestamp,
            predicate=lambda stage: stage.get("state") == "verified",
        )
        record["observed_bundle_location_fingerprint"] = next_value
    answer["updated_at"] = timestamp
    return _refresh_current_projection(answer, timestamp)


def invalidate_config_entry(
    status: Mapping[str, Any],
    target: str,
    observed_config_entry_fingerprint: object,
    *,
    checked_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Stale one client when its canonical MCP entry no longer matches."""

    answer = _status_copy(status)
    record = _client_record(answer, target)
    observed = _fingerprint(observed_config_entry_fingerprint)
    if observed is None:
        raise ValueError("observed config entry fingerprint is required")
    current = record["effective"].get("config_entry_fingerprint")
    timestamp = _timestamp(checked_at)
    if current and current != observed:
        _stale_matching_stages(
            record,
            reason_code="config_entry_changed",
            checked_at=timestamp,
            predicate=lambda stage: stage.get("state") == "verified",
        )
    record["observed_config_entry_fingerprint"] = observed
    answer["updated_at"] = timestamp
    return _refresh_current_projection(answer, timestamp)


def project_legacy(
    status: Mapping[str, Any],
    target: str,
    *,
    projected_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Project one authoritative client record into the legacy top-level fields."""

    answer = _status_copy(status)
    record = _client_record(answer, target)
    timestamp = _timestamp(projected_at or answer.get("updated_at"))
    stages = record["stages"]
    attempt = record["last_attempt"]
    effective = record["effective"]
    registration = stages["registration"].get("state") == "verified"
    loader = stages["loader"].get("state") == "verified"
    transport = stages["transport"].get("state") == "verified"
    fresh_inventory = stages["fresh_app_server"].get("state") == "verified"
    surface = stages["client_surface"].get("state") == "verified"
    conversation = stages["conversation"].get("state") == "verified"
    attempt_state = str(attempt.get("state") or "not_started")
    if attempt_state == "in_progress":
        installation_state = "installing"
    elif attempt_state == "failed_rolled_back":
        installation_state = "failed_rolled_back"
    elif effective.get("state") == "stale":
        installation_state = "installed_config_changed_revalidation_required"
    elif effective.get("state") in {"configured", "connected"}:
        installation_state = "installed_loader_verified"
    else:
        installation_state = "not_installed"

    projection = _empty_legacy_projection()
    projection.update(
        {
            "installation_attempt_id": attempt.get("id") or effective.get("attempt_id"),
            "installation_state": installation_state,
            "connection_state": str(effective.get("state") or "not_configured"),
            "installed_config_fingerprint": effective.get("config_entry_fingerprint"),
            "direct_config_registered": target in {"codex", "chatgpt-desktop-local"}
            and registration,
            "direct_config_loader_verified": target in {"codex", "chatgpt-desktop-local"}
            and loader,
            "installed_config_transport_verified": target
            in {"codex", "chatgpt-desktop-local"}
            and transport,
            "direct_stdio_verified": target in _LOCAL_TARGETS and transport,
            "fresh_codex_app_server_inventory_verified": target
            in {"codex", "chatgpt-desktop-local"}
            and fresh_inventory,
            "desktop_app_server_loader_verified": target
            in {"codex", "chatgpt-desktop-local"}
            and fresh_inventory,
            "claude_desktop_config_registered": target == "claude-desktop" and registration,
            "claude_desktop_config_fingerprint": (
                effective.get("config_entry_fingerprint") if target == "claude-desktop" else None
            ),
            "claude_desktop_config_transport_verified": target == "claude-desktop"
            and transport,
            "desktop_tool_scan_verified": target == "chatgpt-desktop-local" and surface,
            "conversation_attachment_verified": target == "chatgpt-desktop-local"
            and conversation,
            "transport_end_to_end_verified": transport,
            "end_to_end_verified": effective.get("state") == "connected",
            "remote_endpoint_verified": target in _REMOTE_TARGETS and transport,
        }
    )
    answer.update(projection)
    answer["legacy_projection_target"] = target
    answer["legacy_projection_updated_at"] = timestamp
    return answer


def adapt_legacy_status(
    legacy_status: Mapping[str, Any],
    *,
    target: str | None = None,
    adapted_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Conservatively bind attributable v4 evidence to one v5 client record."""

    timestamp = _timestamp(adapted_at or legacy_status.get("updated_at") or legacy_status.get("generated_at"))
    server_name = legacy_status.get("server_name") or "legacy-mcp"
    runtime_fingerprint = legacy_status.get("runtime_fingerprint")
    answer = create_bundle_status(
        str(server_name),
        runtime_fingerprint=runtime_fingerprint,
        bundle_fingerprint=legacy_status.get("bundle_fingerprint"),
        generated_at=timestamp,
    )
    selected_target = _legacy_target(legacy_status, explicit_target=target)
    if selected_target is None:
        answer["legacy_migration_state"] = "target_revalidation_required"
        answer["updated_at"] = timestamp
        return answer

    record = _client_record(answer, selected_target)
    attempt_id = _identifier(
        legacy_status.get("installation_attempt_id") or "legacy-unattributed-attempt"
    )
    config_fingerprint = _fingerprint(
        legacy_status.get("installed_config_fingerprint")
        or legacy_status.get("claude_desktop_config_fingerprint")
    )
    normalized_runtime = _fingerprint(runtime_fingerprint)
    claims = _legacy_stage_claims(legacy_status, selected_target)
    attributed = bool(
        legacy_status.get("installation_attempt_id") and config_fingerprint and normalized_runtime
    )
    for stage_name, claimed in claims.items():
        if not claimed:
            continue
        record["stages"][stage_name] = {
            "state": "verified" if attributed else "pending",
            "attempt_id": attempt_id,
            "checked_at": timestamp,
            "reason_code": "legacy_migrated" if attributed else "legacy_evidence_unattributed",
            "config_entry_fingerprint": config_fingerprint,
            "runtime_fingerprint": (
                None if stage_name == "registration" else normalized_runtime
            ),
            "evidence": {"legacy_source": True, "legacy_claimed_verified": True},
        }

    # Product-surface and conversation booleans from v4 did not contain current
    # session proof, so they are never promoted during migration.
    record["last_attempt"] = {
        "id": attempt_id,
        "state": "completed" if attributed else "legacy_unattributed",
        "started_at": None,
        "finished_at": timestamp,
        "reason_code": "legacy_migrated" if attributed else "legacy_evidence_unattributed",
        "rollback_complete": None,
    }
    record["effective"] = {
        "state": _derived_effective_state(record) if attributed else "not_configured",
        "attempt_id": attempt_id if attributed else None,
        "bundle_fingerprint": _fingerprint(legacy_status.get("bundle_fingerprint")),
        "bundle_location_fingerprint": None,
        "config_entry_fingerprint": config_fingerprint if attributed else None,
        "config_container_fingerprint": None,
        "runtime_fingerprint": normalized_runtime if attributed else None,
        "verified_at": timestamp if attributed else None,
    }
    _refresh_readiness(record)
    answer["legacy_migration_state"] = "migrated" if attributed else "target_revalidation_required"
    answer["updated_at"] = timestamp
    return project_legacy(answer, selected_target, projected_at=timestamp)


def _empty_client_connection(
    target: str,
    *,
    server_name: str,
    runtime_fingerprint: str | None,
    bundle_fingerprint: str | None,
) -> dict[str, Any]:
    policy = _TARGET_POLICIES[target]
    configuration_ready = target in _LOCAL_TARGETS
    return {
        "schema_version": CLIENT_SCHEMA_VERSION,
        "target": target,
        "server_name": server_name,
        "transport": policy["transport"],
        "config_resource_id": policy["config_resource_id"],
        "last_attempt": {
            "id": None,
            "state": "not_started",
            "started_at": None,
            "finished_at": None,
            "reason_code": "not_started",
            "rollback_complete": None,
        },
        "effective": {
            "state": "not_configured",
            "attempt_id": None,
            "bundle_fingerprint": bundle_fingerprint,
            "bundle_location_fingerprint": None,
            "config_entry_fingerprint": None,
            "config_container_fingerprint": None,
            "runtime_fingerprint": None,
            "verified_at": None,
        },
        "configuration_required_stages": list(policy["configuration_required_stages"]),
        "connection_required_stages": list(policy["connection_required_stages"]),
        "stage_order": list(STAGE_ORDER),
        "stages": {name: _empty_stage() for name in STAGE_ORDER},
        "readiness": {
            "artifact_ready": True,
            "configuration_ready": configuration_ready,
            "runtime_ready": bool(runtime_fingerprint),
            "endpoint_verified": False,
            "registration_verified": False,
            "tool_discovery_verified": False,
            "conversation_verified": False,
            "manual_action_required": True,
        },
    }


def _empty_stage() -> dict[str, Any]:
    return {
        "state": "not_checked",
        "attempt_id": None,
        "checked_at": None,
        "reason_code": "not_checked",
        "config_entry_fingerprint": None,
        "runtime_fingerprint": None,
        "evidence": {},
    }


def _empty_legacy_projection() -> dict[str, Any]:
    return {
        "installation_attempt_id": None,
        "installation_state": "not_installed",
        "connection_state": "not_configured",
        "installed_config_fingerprint": None,
        "direct_config_registered": False,
        "direct_config_loader_verified": False,
        "installed_config_transport_verified": False,
        "direct_stdio_verified": False,
        "fresh_codex_app_server_inventory_verified": False,
        "desktop_app_server_loader_verified": False,
        "claude_desktop_config_registered": False,
        "claude_desktop_config_fingerprint": None,
        "claude_desktop_config_transport_verified": False,
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
        "transport_end_to_end_verified": False,
        "end_to_end_verified": False,
        "remote_endpoint_verified": False,
    }


def _stale_matching_stages(
    record: dict[str, Any],
    *,
    reason_code: str,
    checked_at: str,
    predicate: Any,
) -> None:
    changed = False
    for stage in record["stages"].values():
        if not predicate(stage):
            continue
        stage["state"] = "stale"
        stage["reason_code"] = reason_code
        stage["checked_at"] = checked_at
        changed = True
    if changed:
        record["effective"]["state"] = "stale"
        _refresh_readiness(record)


def _invalidate_replaced_shared_config_siblings(
    status: dict[str, Any],
    *,
    target: str,
    config_entry_fingerprint: str | None,
    bundle_location_fingerprint: str | None,
    runtime_fingerprint: str | None,
    checked_at: str,
) -> None:
    """Stale sibling evidence when one shared client config is replaced.

    Codex and ChatGPT Desktop currently share the Codex-host user config. A
    successful write for one target can therefore replace the other target's
    effective server entry even though their loader, surface, and conversation
    evidence remain product-specific. Identical entry/location contracts keep
    each sibling's own evidence; no stage is copied between products. A runtime
    change still stales only runtime-bound sibling stages and preserves its
    independently verified registration.
    """

    source = _client_record(status, target)
    resource_id = str(source.get("config_resource_id") or "")
    if not resource_id:
        return
    for sibling_target, sibling in status["client_connections"].items():
        if (
            sibling_target == target
            or sibling.get("config_resource_id") != resource_id
        ):
            continue
        verified_stages = [
            stage
            for stage in sibling["stages"].values()
            if stage.get("state") == "verified"
        ]
        if not verified_stages:
            continue
        sibling_effective = sibling["effective"]
        same_entry = (
            config_entry_fingerprint is not None
            and sibling_effective.get("config_entry_fingerprint")
            == config_entry_fingerprint
        )
        same_location = (
            bundle_location_fingerprint is not None
            and sibling_effective.get("bundle_location_fingerprint")
            == bundle_location_fingerprint
        )
        if not (same_entry and same_location):
            sibling["observed_config_entry_fingerprint"] = config_entry_fingerprint
            sibling["observed_bundle_location_fingerprint"] = (
                bundle_location_fingerprint
            )
            _stale_matching_stages(
                sibling,
                reason_code="shared_config_replaced",
                checked_at=checked_at,
                predicate=lambda stage: stage.get("state") == "verified",
            )
            continue
        if runtime_fingerprint is None:
            continue
        _stale_matching_stages(
            sibling,
            reason_code="shared_runtime_replaced",
            checked_at=checked_at,
            predicate=lambda stage: (
                stage.get("state") == "verified"
                and stage.get("runtime_fingerprint") is not None
                and stage.get("runtime_fingerprint") != runtime_fingerprint
            ),
        )


def _derived_effective_state(record: Mapping[str, Any]) -> str:
    stages = record["stages"]
    required_for_connection = record["connection_required_stages"]
    required_for_configuration = record["configuration_required_stages"]
    if all(stages[name].get("state") == "verified" for name in required_for_connection):
        return "connected"
    if all(stages[name].get("state") == "verified" for name in required_for_configuration):
        return "configured"
    if any(stages[name].get("state") == "stale" for name in required_for_configuration):
        return "stale"
    return "partially_verified" if any(
        stage.get("state") == "verified" for stage in stages.values()
    ) else "not_configured"


def _refresh_readiness(record: dict[str, Any]) -> None:
    stages = record["stages"]
    state = str(record["effective"].get("state") or "not_configured")
    readiness = record["readiness"]
    readiness["runtime_ready"] = bool(record["effective"].get("runtime_fingerprint"))
    readiness["endpoint_verified"] = record["target"] in _REMOTE_TARGETS and (
        stages["transport"].get("state") == "verified"
    )
    readiness["registration_verified"] = stages["registration"].get("state") == "verified"
    readiness["tool_discovery_verified"] = stages["loader"].get("state") == "verified"
    readiness["conversation_verified"] = stages["conversation"].get("state") == "verified"
    readiness["configuration_ready"] = state in {
        "configured",
        "connected",
    }
    readiness["manual_action_required"] = state != "connected"


def _refresh_current_projection(status: dict[str, Any], timestamp: str) -> dict[str, Any]:
    target = status.get("legacy_projection_target")
    if target in CLIENT_TARGETS:
        return project_legacy(status, str(target), projected_at=timestamp)
    return status


def _legacy_target(
    legacy_status: Mapping[str, Any], *, explicit_target: str | None
) -> str | None:
    if explicit_target is not None:
        _target(explicit_target)
        return explicit_target
    projected = legacy_status.get("legacy_projection_target")
    if projected in CLIENT_TARGETS:
        return str(projected)
    if legacy_status.get("claude_desktop_config_registered") is True:
        return "claude-desktop"
    if legacy_status.get("plugin_registered") is True:
        return "chatgpt-desktop-local"
    # A v4 direct config was shared by Codex and ChatGPT Desktop and did not
    # record which client initiated the flow. Never duplicate that success.
    return None


def _legacy_stage_claims(
    legacy_status: Mapping[str, Any], target: str
) -> dict[str, bool]:
    if target == "claude-desktop":
        return {
            "registration": legacy_status.get("claude_desktop_config_registered") is True,
            "transport": legacy_status.get("claude_desktop_config_transport_verified") is True,
            "loader": False,
            "fresh_app_server": False,
        }
    if target in {"codex", "chatgpt-desktop-local"}:
        plugin = legacy_status.get("plugin_registered") is True
        return {
            "registration": plugin or legacy_status.get("direct_config_registered") is True,
            "loader": (
                legacy_status.get("plugin_loader_verified") is True
                if plugin
                else legacy_status.get("direct_config_loader_verified") is True
            ),
            "transport": legacy_status.get("transport_end_to_end_verified") is True
            and (
                legacy_status.get("plugin_stdio_verified") is True
                if plugin
                else legacy_status.get("direct_stdio_verified") is True
            ),
            "fresh_app_server": legacy_status.get(
                "fresh_codex_app_server_inventory_verified"
            )
            is True
            and legacy_status.get("desktop_app_server_loader_verified") is True,
        }
    if target in _REMOTE_TARGETS:
        return {
            "transport": legacy_status.get("remote_endpoint_verified") is True,
            "registration": False,
            "loader": False,
            "fresh_app_server": False,
        }
    return {
        "registration": False,
        "loader": False,
        "transport": False,
        "fresh_app_server": False,
    }


def _status_copy(status: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(status, Mapping):
        raise TypeError("status must be a mapping")
    answer = copy.deepcopy(dict(status))
    if answer.get("schema_version") != SCHEMA_VERSION or answer.get("status_model") != STATUS_MODEL:
        raise ValueError("status is not an mcp-bundle-status-v5 document")
    connections = answer.get("client_connections")
    if (
        not isinstance(connections, dict)
        or len(connections) != len(CLIENT_TARGETS)
        or set(connections) != set(CLIENT_TARGETS)
    ):
        raise ValueError("status must contain every supported client_connections record")
    return answer


def _client_record(status: dict[str, Any], target: str) -> dict[str, Any]:
    _target(target)
    record = status["client_connections"].get(target)
    if not isinstance(record, dict) or record.get("target") != target:
        raise ValueError(f"Invalid client connection record for {target}")
    return record


def _current_attempt(record: Mapping[str, Any], attempt_id: object) -> dict[str, Any]:
    normalized = _identifier(attempt_id)
    attempt = record.get("last_attempt")
    if not isinstance(attempt, Mapping):
        raise ValueError("client connection has no current attempt")
    if attempt.get("id") != normalized or attempt.get("state") != "in_progress":
        raise ValueError("client connection attempt does not match the active attempt")
    return copy.deepcopy(dict(attempt))


def _target(target: str) -> None:
    if target not in CLIENT_TARGETS:
        raise ValueError(f"Unsupported MCP client target: {target}")


def _server_name(value: object) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_SERVER_NAME.fullmatch(normalized):
        raise ValueError("server_name must use lowercase ASCII letters, numbers, hyphens, or underscores")
    return normalized


def _identifier(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("identifier is required")
    if _SAFE_IDENTIFIER.fullmatch(normalized):
        return normalized
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def _fingerprint(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip()
    if _SHA256_FINGERPRINT.fullmatch(normalized):
        return normalized.lower()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def _reason(value: object) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized if _SAFE_REASON.fullmatch(normalized) else "unspecified_failure"


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _sanitize_evidence_value(str(key), value)
        for key, value in evidence.items()
    }


def _sanitize_evidence_value(key: str, value: Any) -> Any:
    if any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return _sanitize_evidence(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_evidence_value(key, item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _fingerprint(value)


def _has_conversation_proof(
    evidence: Mapping[str, Any], *, expected_server_name: str
) -> bool:
    if evidence.get("tool_call_verified") is not True:
        return False
    if str(evidence.get("server_name") or "").strip() != expected_server_name:
        return False
    if not all(
        str(evidence.get(name) or "").strip()
        for name in ("tool_name", "conversation_id")
    ):
        return False
    return any(
        str(evidence.get(name) or "").strip()
        for name in ("result_nonce", "result_nonce_hash", "verification_nonce_hash")
    )


def initialize_status_document(
    existing_status: Mapping[str, Any] | None,
    *,
    server_name: object | None = None,
    runtime_fingerprint: object | None = None,
    bundle_fingerprint: object | None = None,
    initialized_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Add the v5 client model while retaining legacy top-level evidence.

    An already-valid v5 document is returned unchanged. A legacy document keeps
    all of its top-level fields except the two schema selectors, whose previous
    values are copied into explicit ``legacy_*`` fields before v5 is installed.
    No legacy success claim is promoted into a client record by this operation.
    """

    if existing_status is not None and not isinstance(existing_status, Mapping):
        raise TypeError("existing status must be a mapping")
    if (
        existing_status is not None
        and existing_status.get("schema_version") == SCHEMA_VERSION
        and existing_status.get("status_model") == STATUS_MODEL
    ):
        return _status_copy(existing_status)

    source = copy.deepcopy(dict(existing_status or {}))
    selected_server_name = source.get("server_name") or server_name
    selected_runtime = (
        source.get("runtime_fingerprint")
        if source.get("runtime_fingerprint") is not None
        else runtime_fingerprint
    )
    selected_bundle = (
        source.get("bundle_fingerprint")
        if source.get("bundle_fingerprint") is not None
        else bundle_fingerprint
    )
    timestamp = _timestamp(
        initialized_at or source.get("updated_at") or source.get("generated_at")
    )
    created = create_bundle_status(
        _server_name(selected_server_name),
        runtime_fingerprint=selected_runtime,
        bundle_fingerprint=selected_bundle,
        generated_at=timestamp,
    )

    previous_schema = source.get("schema_version")
    previous_model = source.get("status_model")
    if previous_schema is not None and previous_schema != SCHEMA_VERSION:
        source.setdefault("legacy_schema_version", copy.deepcopy(previous_schema))
    if previous_model is not None and previous_model != STATUS_MODEL:
        source.setdefault("legacy_status_model", copy.deepcopy(previous_model))

    # Retain legacy evidence as-is while installing only empty, isolated v5
    # client records. Migration into a client remains an explicit later action.
    answer = source
    for key, value in created.items():
        answer.setdefault(key, copy.deepcopy(value))
    answer["schema_version"] = SCHEMA_VERSION
    answer["status_model"] = STATUS_MODEL
    if existing_status is not None and "legacy_migration_state" not in existing_status:
        answer["legacy_migration_state"] = "pending_adapter"
    answer["client_connections"] = created["client_connections"]
    return _status_copy(answer)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser whose failures never echo user-supplied values."""

    def error(self, message: str) -> None:
        del message
        raise ValueError("invalid_arguments")


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="python -m scripts.mcp_client_status",
        description="Atomically update an MCP client connection status document.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--status-file", required=True)
    init_parser.add_argument("--server-name")
    init_parser.add_argument("--runtime-fingerprint")
    init_parser.add_argument("--bundle-fingerprint")
    init_parser.add_argument("--timestamp")

    begin_parser = subparsers.add_parser("begin")
    _add_target_attempt_arguments(begin_parser)
    begin_parser.add_argument("--timestamp")

    commit_parser = subparsers.add_parser("commit")
    _add_target_attempt_arguments(commit_parser)
    commit_parser.add_argument(
        "--verified-stage",
        action="append",
        choices=STAGE_ORDER,
        default=[],
    )
    commit_parser.add_argument("--config-entry-fingerprint")
    commit_parser.add_argument("--config-container-fingerprint")
    commit_parser.add_argument("--runtime-fingerprint")
    commit_parser.add_argument("--bundle-fingerprint")
    commit_parser.add_argument("--bundle-location-fingerprint")
    commit_parser.add_argument("--preserve-legacy-projection", action="store_true")
    commit_parser.add_argument("--timestamp")

    fail_parser = subparsers.add_parser("fail-rolled-back")
    _add_target_attempt_arguments(fail_parser)
    fail_parser.add_argument("--reason", "--reason-code", dest="reason", required=True)
    fail_parser.add_argument("--preserve-legacy-projection", action="store_true")
    fail_parser.add_argument("--timestamp")

    unverified_parser = subparsers.add_parser("fail-unverified")
    _add_target_attempt_arguments(unverified_parser)
    unverified_parser.add_argument("--reason", "--reason-code", dest="reason", required=True)
    unverified_parser.add_argument("--preserve-legacy-projection", action="store_true")
    unverified_parser.add_argument("--timestamp")

    invalidate_parser = subparsers.add_parser("invalidate-runtime")
    invalidate_parser.add_argument("--status-file", required=True)
    invalidate_parser.add_argument("--previous-runtime-fingerprint", required=True)
    invalidate_parser.add_argument("--next-runtime-fingerprint")
    invalidate_parser.add_argument("--timestamp")
    return parser


def _add_target_attempt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--target", choices=CLIENT_TARGETS, required=True)
    parser.add_argument("--attempt-id", required=True)


def _read_status_file(status_path: Path) -> tuple[dict[str, Any], str]:
    raw = status_path.read_bytes()
    try:
        loaded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("status file is not valid UTF-8 JSON") from exc
    if not isinstance(loaded, dict):
        raise ValueError("status file root must be a JSON object")
    return loaded, hashlib.sha256(raw).hexdigest()


def _atomic_write_status_file(
    status_path: Path,
    status: Mapping[str, Any],
    *,
    expected_digest: str | None,
) -> None:
    """Fsync and replace a status file only if its original digest still matches."""

    parent = status_path.parent
    if not parent.is_dir():
        raise FileNotFoundError("status file parent directory does not exist")
    serialized = (
        json.dumps(status, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
    ).encode("utf-8")
    temp_path = parent / f".{status_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

        if status_path.exists():
            current_digest = hashlib.sha256(status_path.read_bytes()).hexdigest()
            if expected_digest is None or current_digest != expected_digest:
                raise RuntimeError("concurrent status modification detected")
        elif expected_digest is not None:
            raise RuntimeError("concurrent status modification detected")

        os.replace(temp_path, status_path)
        _fsync_directory(parent)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _status_file_lock(status_path: Path) -> Iterable[None]:
    """Serialize this CLI's full read/validate/replace transaction."""

    lock_path = status_path.parent / f".{status_path.name}.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_cli_action(args: argparse.Namespace) -> tuple[str | None, str]:
    status_path = Path(args.status_file).expanduser().resolve(strict=False)
    with _status_file_lock(status_path):
        if args.action == "init":
            if status_path.exists():
                current, expected_digest = _read_status_file(status_path)
            else:
                current, expected_digest = None, None
            updated = initialize_status_document(
                current,
                server_name=args.server_name,
                runtime_fingerprint=args.runtime_fingerprint,
                bundle_fingerprint=args.bundle_fingerprint,
                initialized_at=args.timestamp,
            )
            _atomic_write_status_file(
                status_path,
                updated,
                expected_digest=expected_digest,
            )
            return None, "initialized"

        current, expected_digest = _read_status_file(status_path)
        if args.action == "begin":
            updated = begin_attempt(
                current,
                args.target,
                args.attempt_id,
                started_at=args.timestamp,
            )
            state = "in_progress"
        elif args.action == "commit":
            preserved_legacy = None
            if args.preserve_legacy_projection:
                preserved_legacy = {
                    key: copy.deepcopy(current.get(key, default))
                    for key, default in _empty_legacy_projection().items()
                }
                preserved_legacy["legacy_projection_target"] = current.get(
                    "legacy_projection_target"
                )
                preserved_legacy["legacy_projection_updated_at"] = current.get(
                    "legacy_projection_updated_at"
                )
            updated = commit_success(
                current,
                args.target,
                args.attempt_id,
                verified_stages=args.verified_stage,
                config_entry_fingerprint=args.config_entry_fingerprint,
                config_container_fingerprint=args.config_container_fingerprint,
                runtime_fingerprint=args.runtime_fingerprint,
                bundle_fingerprint=args.bundle_fingerprint,
                bundle_location_fingerprint=args.bundle_location_fingerprint,
                verified_at=args.timestamp,
            )
            if preserved_legacy is not None:
                updated.update(preserved_legacy)
            state = str(
                updated["client_connections"][args.target]["effective"]["state"]
            )
        elif args.action == "fail-rolled-back":
            preserved_legacy = None
            if args.preserve_legacy_projection:
                preserved_legacy = {
                    key: copy.deepcopy(current.get(key, default))
                    for key, default in _empty_legacy_projection().items()
                }
                preserved_legacy["legacy_projection_target"] = current.get(
                    "legacy_projection_target"
                )
                preserved_legacy["legacy_projection_updated_at"] = current.get(
                    "legacy_projection_updated_at"
                )
            updated = fail_rolled_back(
                current,
                args.target,
                args.attempt_id,
                reason_code=args.reason,
                finished_at=args.timestamp,
            )
            if preserved_legacy is not None:
                updated.update(preserved_legacy)
            state = "failed_rolled_back"
        elif args.action == "fail-unverified":
            preserved_legacy = None
            if args.preserve_legacy_projection:
                preserved_legacy = {
                    key: copy.deepcopy(current.get(key, default))
                    for key, default in _empty_legacy_projection().items()
                }
                preserved_legacy["legacy_projection_target"] = current.get(
                    "legacy_projection_target"
                )
                preserved_legacy["legacy_projection_updated_at"] = current.get(
                    "legacy_projection_updated_at"
                )
            updated = fail_unverified(
                current,
                args.target,
                args.attempt_id,
                reason_code=args.reason,
                finished_at=args.timestamp,
            )
            if preserved_legacy is not None:
                updated.update(preserved_legacy)
            state = "failed_unverified"
        elif args.action == "invalidate-runtime":
            updated = invalidate_runtime(
                current,
                args.previous_runtime_fingerprint,
                next_runtime_fingerprint=args.next_runtime_fingerprint,
                checked_at=args.timestamp,
            )
            state = (
                "stale"
                if any(
                    record["effective"].get("state") == "stale"
                    for record in updated["client_connections"].values()
                )
                else "unchanged"
            )
        else:
            raise ValueError("invalid action")

        _atomic_write_status_file(
            status_path,
            updated,
            expected_digest=expected_digest,
        )
        target = args.target if args.action != "invalidate-runtime" else None
        return target, state


def _safe_action(argv: list[str]) -> str:
    return argv[0] if argv and argv[0] in {
        "init",
        "begin",
        "commit",
        "fail-rolled-back",
        "fail-unverified",
        "invalidate-runtime",
    } else "invalid"


def _emit_cli_result(*, target: str | None, action: str, ok: bool, state: str) -> None:
    print(
        json.dumps(
            {"target": target, "action": action, "ok": ok, "state": state},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def main(argv: list[str] | None = None) -> int:
    cli_argv = list(sys.argv[1:] if argv is None else argv)
    action = _safe_action(cli_argv)
    try:
        args = _build_cli_parser().parse_args(cli_argv)
        target, state = _run_cli_action(args)
    except Exception as exc:
        # Never reflect an exception, path, server, attempt, fingerprint, or
        # secret into process output. Callers inspect the nonzero exit code.
        _emit_cli_result(target=None, action=action, ok=False, state="failed")
        print("MCP client status command failed.", file=sys.stderr)
        if os.environ.get("MCP_CLIENT_STATUS_DEBUG") == "1":
            print(f"Failure class: {type(exc).__name__}", file=sys.stderr)
        return 1
    _emit_cli_result(target=target, action=action, ok=True, state=state)
    return 0


__all__ = [
    "CLIENT_SCHEMA_VERSION",
    "CLIENT_TARGETS",
    "SCHEMA_VERSION",
    "STAGE_ORDER",
    "STATUS_MODEL",
    "adapt_legacy_status",
    "begin_attempt",
    "commit_success",
    "create_bundle_status",
    "fail_rolled_back",
    "fail_unverified",
    "initialize_status_document",
    "invalidate_config_entry",
    "invalidate_location",
    "invalidate_runtime",
    "project_legacy",
]


if __name__ == "__main__":
    raise SystemExit(main())
