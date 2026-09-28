"""Application service facade for MCP connection observations.

The Streamlit operator UI should not own bundle-status parsing or process
invocation details.  Keeping those details here gives other frontends and
CLI-backed workflows one conservative, testable contract to call.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.readiness_adapter import SAFE_REASON_CODE_PATTERN
from scripts.mcp_connection_diagnostic import (
    STAGE_ORDER as _MCP_CONNECTION_STAGE_ORDER,
    diagnostic_from_bundle_status,
)
from scripts.refresh_mcp_client_connection import run as refresh_mcp_client_connection


DiagnosticBuilder = Callable[..., dict[str, Any]]
RefreshRunner = Callable[..., int]
MAX_CONFIG_FINGERPRINT_BYTES = 4 * 1024 * 1024
MCP_CONNECTION_STAGE_ORDER = _MCP_CONNECTION_STAGE_ORDER


def _safe_reason_code(value: object) -> str:
    """Keep diagnostic reasons bounded and free of paths or arbitrary text."""

    candidate = str(value or "").strip()
    return candidate if SAFE_REASON_CODE_PATTERN.fullmatch(candidate) else "refresh_failed"


def _is_unc_path(value: str) -> bool:
    """Return whether a user-provided Windows path points to a network share."""

    normalized = value.replace("/", "\\")
    return normalized.startswith("\\\\")


def _local_config_fingerprint(value: object) -> str | None:
    """Hash a bounded local config file without following a remote share path."""

    path_text = str(value or "").strip()
    if not path_text or _is_unc_path(path_text):
        return None
    path = Path(path_text)
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_FINGERPRINT_BYTES:
            return None
        digest = hashlib.sha256()
        bytes_read = 0
        with path.open("rb") as config_file:
            for chunk in iter(lambda: config_file.read(1024 * 1024), b""):
                bytes_read += len(chunk)
                if bytes_read > MAX_CONFIG_FINGERPRINT_BYTES:
                    return None
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def read_mcp_connection_diagnostic(
    bundle_dir: str | Path,
    connection_target: str | None = None,
    *,
    diagnostic_builder: DiagnosticBuilder = diagnostic_from_bundle_status,
) -> tuple[dict[str, Any], str | None]:
    """Read the current bundle status and return a fail-closed diagnostic.

    ``diagnostic_builder`` is injectable so callers can keep deterministic
    tests without importing or executing the Streamlit module.
    """

    bundle_path_text = str(bundle_dir or "").strip()
    if not bundle_path_text:
        return (
            diagnostic_builder({}, connection_target=connection_target),
            "bundle_dir_unavailable",
        )
    status_path = Path(bundle_dir) / "bundle_status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except OSError:
        return (
            diagnostic_builder({}, connection_target=connection_target),
            "bundle_status_unavailable",
        )
    except (UnicodeError, json.JSONDecodeError):
        return (
            diagnostic_builder({}, connection_target=connection_target),
            "bundle_status_invalid",
        )
    if not isinstance(payload, dict):
        return (
            diagnostic_builder({}, connection_target=connection_target),
            "bundle_status_invalid",
        )

    v5_connections = (
        payload.get("client_connections")
        if payload.get("schema_version") == "mcp-bundle-status-v5"
        and isinstance(payload.get("client_connections"), dict)
        else None
    )
    selected_record = (
        v5_connections.get(connection_target)
        if isinstance(v5_connections, dict)
        and isinstance(v5_connections.get(connection_target), dict)
        else None
    )
    selected_effective = (
        selected_record.get("effective")
        if isinstance(selected_record, dict)
        and isinstance(selected_record.get("effective"), dict)
        else {}
    )
    selected_last_attempt = (
        selected_record.get("last_attempt")
        if isinstance(selected_record, dict)
        and isinstance(selected_record.get("last_attempt"), dict)
        else {}
    )
    if selected_record is not None:
        attempt_id = str(
            selected_effective.get("attempt_id")
            or selected_last_attempt.get("id")
            or ""
        ).strip() or None
    else:
        attempt_id = str(
            payload.get("installation_attempt_id")
            or payload.get("attempt_id")
            or ""
        ).strip() or None

    is_claude_desktop = connection_target == "claude-desktop"
    is_claude_code = connection_target == "claude-code"
    if is_claude_desktop:
        fingerprint_field = "claude_desktop_config_fingerprint"
        path_field: str | None = "claude_desktop_config_path"
        registration_field = "claude_desktop_config_registered"
    elif is_claude_code:
        fingerprint_field = "claude_code_config_fingerprint"
        path_field = None
        registration_field = "claude_code_registered"
    else:
        fingerprint_field = "installed_config_fingerprint"
        path_field = "direct_config_path"
        registration_field = "direct_config_registered"

    if selected_record is not None:
        config_fingerprint = str(
            selected_effective.get("config_entry_fingerprint") or ""
        ).strip() or None
    else:
        config_fingerprint = str(
            payload.get(fingerprint_field)
            or payload.get("config_fingerprint")
            or ""
        ).strip() or None

    legacy_projection_matches_target = (
        selected_record is None
        or payload.get("legacy_projection_target") == connection_target
    )
    if (
        path_field
        and legacy_projection_matches_target
        and payload.get(registration_field) is True
    ):
        config_fingerprint = _local_config_fingerprint(payload.get(path_field))

    diagnostic = diagnostic_builder(
        payload,
        attempt_id=attempt_id,
        config_fingerprint=config_fingerprint,
        checked_at=payload.get("updated_at") or payload.get("generated_at"),
        connection_target=connection_target,
    )
    return diagnostic, None


def refresh_mcp_connection_observation(
    bundle_dir: str | Path,
    connection_target: str,
    server_name: str,
    *,
    refresh_runner: RefreshRunner = refresh_mcp_client_connection,
) -> tuple[bool, str]:
    """Refresh an observable desktop status without claiming connectivity."""

    # The refresh script intentionally rejects the retired ChatGPT local
    # integration.  Keep the facade aligned with that contract so a caller
    # cannot turn a legacy target into a misleading pending observation.
    if connection_target != "claude-desktop":
        return False, "target_not_observable"

    status_path = Path(bundle_dir) / "bundle_status.json"
    output = io.StringIO()
    refresh_args = [
        "--target",
        connection_target,
        "--server-name",
        server_name,
        "--bundle-status",
        str(status_path),
        "--bundle-dir",
        str(Path(bundle_dir)),
    ]
    try:
        exit_code = refresh_runner(refresh_args, stdout=output)
    except Exception:
        # The observer is an optional diagnostic.  Never let a client-side
        # probe exception take down the operator UI or look like connectivity.
        return False, "refresh_failed"
    try:
        output.seek(0)
        result = json.loads(output.read())
    except (TypeError, json.JSONDecodeError):
        return False, "refresh_report_invalid"
    if not isinstance(result, dict):
        return False, "refresh_report_invalid"
    if result.get("error_code"):
        return False, _safe_reason_code(result["error_code"])
    if exit_code != 0:
        return False, "refresh_failed"
    if result.get("status_updated") is not True:
        return False, "refresh_failed"
    if exit_code == 0 and result.get("ok") is True:
        return True, "observation_ready"
    return True, "observation_recorded_pending"
