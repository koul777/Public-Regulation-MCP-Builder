from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

if __package__ in {None, ""}:  # Keep a third-party ``scripts`` package from shadowing this repo.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from scripts import check_chatgpt_desktop_recognition as chatgpt_observer
    from scripts import inspect_claude_desktop_connection as claude_observer
    from scripts import mcp_client_status
except (ImportError, ModuleNotFoundError):  # pragma: no cover - local fallback
    import check_chatgpt_desktop_recognition as chatgpt_observer
    import inspect_claude_desktop_connection as claude_observer
    import mcp_client_status


SCHEMA_VERSION = "mcp-client-connection-refresh-v1"
TARGETS = ("chatgpt-desktop-local", "claude-desktop")
_SAFE_TOKEN = re.compile(r"^[a-z0-9_]{1,96}$")
_MAX_COUNT = 2_147_483_647

ConnectionProbe = Callable[[str, Path, Path | None, str], Mapping[str, Any]]
Clock = Callable[[], datetime]
AttemptIdFactory = Callable[[], str]


class RefreshError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManualRegistrationEvidence:
    config_path: Path
    config_fingerprint: str
    config_source_digest: str
    snippet_path: Path
    snippet_source_digest: str


def _server_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or "\r" in normalized or "\n" in normalized:
        raise argparse.ArgumentTypeError(
            "server identity must be 1-256 characters without line breaks"
        )
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh a sanitized, read-only Desktop MCP observation in an existing "
            "bundle_status.json. This does not install, configure, or verify a connection."
        )
    )
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--server", "--server-name", dest="server_name", required=True, type=_server_name)
    parser.add_argument("--bundle-status", required=True, type=Path)
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        help="Exact extracted bundle root, used only for explicit manual registration adoption.",
    )
    parser.add_argument(
        "--codex-config",
        type=Path,
        help="Read-only Codex-host config override for tests or explicit local use.",
    )
    parser.add_argument(
        "--adopt-manual-registration",
        action="store_true",
        help=(
            "When no attempt exists, adopt an exact read-only config/snippet match; "
            "never writes the config or verifies loader/tool/conversation success."
        ),
    )
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def _read_status(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RefreshError("bundle_status_read_failed") from exc
    if not isinstance(payload, dict):
        raise RefreshError("bundle_status_invalid")
    return payload, hashlib.sha256(raw).hexdigest()


def _v5_client_record(
    status: Mapping[str, Any], target: str
) -> Mapping[str, Any] | None:
    if status.get("schema_version") != mcp_client_status.SCHEMA_VERSION:
        return None
    if status.get("status_model") != mcp_client_status.STATUS_MODEL:
        raise RefreshError("bundle_status_v5_model_invalid")
    connections = status.get("client_connections")
    if not isinstance(connections, Mapping):
        raise RefreshError("bundle_status_v5_connections_invalid")
    record = connections.get(target)
    if not isinstance(record, Mapping) or record.get("target") != target:
        raise RefreshError("bundle_status_v5_client_record_invalid")
    return record


def _optional_attempt_id(status: Mapping[str, Any], target: str) -> str | None:
    record = _v5_client_record(status, target)
    if record is not None:
        last_attempt = _mapping(record.get("last_attempt"))
        effective = _mapping(record.get("effective"))
        attempt_id = str(
            last_attempt.get("id") or effective.get("attempt_id") or ""
        ).strip()
        return attempt_id or None
    attempt_id = str(status.get("installation_attempt_id") or "").strip()
    return attempt_id or None


def _existing_attempt_id(status: Mapping[str, Any], target: str) -> str:
    attempt_id = _optional_attempt_id(status, target)
    if not attempt_id:
        raise RefreshError("installation_attempt_id_missing")
    return attempt_id


def _validate_server_identity(status: Mapping[str, Any], supplied: str) -> None:
    recorded = str(status.get("server_name") or "").strip()
    if not recorded:
        raise RefreshError("bundle_status_server_identity_missing")
    if not hmac.compare_digest(recorded.encode("utf-8"), supplied.encode("utf-8")):
        raise RefreshError("bundle_status_server_identity_mismatch")


def _config_path_from_status(status: Mapping[str, Any], target: str) -> Path | None:
    key = "direct_config_path" if target == "chatgpt-desktop-local" else "claude_desktop_config_path"
    value = status.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return Path(text) if text else None


def _default_codex_config_path() -> Path | None:
    codex_home = str(os.getenv("CODEX_HOME") or "").strip()
    if codex_home:
        return Path(codex_home) / "config.toml"
    user_profile = str(os.getenv("USERPROFILE") or "").strip()
    return Path(user_profile) / ".codex" / "config.toml" if user_profile else None


def _read_toml(path: Path, *, error_code: str) -> tuple[dict[str, Any], bytes, str]:
    try:
        raw = path.read_bytes()
        payload = tomllib.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RefreshError(error_code) from exc
    if not isinstance(payload, dict):
        raise RefreshError(error_code)
    return payload, raw, hashlib.sha256(raw).hexdigest()


def _exact_server_entry(
    payload: Mapping[str, Any], server_name: str, *, source: str
) -> Mapping[str, Any]:
    servers = payload.get("mcp_servers")
    if not isinstance(servers, Mapping):
        raise RefreshError(f"manual_registration_{source}_entry_missing")
    candidates = [
        (key, value)
        for key, value in servers.items()
        if isinstance(key, str) and key.casefold() == server_name.casefold()
    ]
    if len(candidates) > 1:
        raise RefreshError(f"manual_registration_{source}_entry_ambiguous")
    if not candidates or candidates[0][0] != server_name:
        raise RefreshError(f"manual_registration_{source}_entry_missing")
    entry = candidates[0][1]
    if not isinstance(entry, Mapping):
        raise RefreshError(f"manual_registration_{source}_entry_invalid")
    return entry


def _normalized_manual_entry(entry: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise RefreshError(f"manual_registration_{source}_entry_invalid")
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise RefreshError(f"manual_registration_{source}_entry_invalid")
    args = entry.get("args")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise RefreshError(f"manual_registration_{source}_entry_invalid")
    # Compare Codex's effective five-field stdio contract. An omitted ``env``
    # is empty and an omitted ``enabled`` is enabled, matching Codex defaults.
    env = entry.get("env", {})
    if not isinstance(env, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise RefreshError(f"manual_registration_{source}_entry_invalid")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RefreshError(f"manual_registration_{source}_entry_invalid")
    return {
        "command": command,
        "cwd": cwd,
        "args": list(args),
        "env": dict(env),
        "enabled": enabled,
    }


def _manual_registration_evidence(
    *,
    bundle_dir: Path,
    bundle_status_path: Path,
    config_path: Path,
    server_name: str,
) -> ManualRegistrationEvidence:
    try:
        resolved_bundle_dir = bundle_dir.resolve(strict=True)
        resolved_status = bundle_status_path.resolve(strict=True)
        resolved_config = config_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RefreshError("manual_registration_source_missing") from exc
    expected_status = resolved_bundle_dir / "bundle_status.json"
    if resolved_status != expected_status:
        raise RefreshError("bundle_status_not_in_bundle_dir")
    snippet_path = resolved_bundle_dir / "codex_config_snippet.toml"
    snippet, _snippet_raw, snippet_digest = _read_toml(
        snippet_path,
        error_code="manual_registration_snippet_unreadable",
    )
    current, config_raw, config_digest = _read_toml(
        resolved_config,
        error_code="manual_registration_config_unreadable",
    )
    snippet_entry = _normalized_manual_entry(
        _exact_server_entry(snippet, server_name, source="snippet"),
        source="snippet",
    )
    current_entry = _normalized_manual_entry(
        _exact_server_entry(current, server_name, source="config"),
        source="config",
    )
    if snippet_entry != current_entry:
        raise RefreshError("manual_registration_entry_mismatch")
    return ManualRegistrationEvidence(
        config_path=resolved_config,
        config_fingerprint="sha256:" + hashlib.sha256(config_raw).hexdigest(),
        config_source_digest=config_digest,
        snippet_path=snippet_path,
        snippet_source_digest=snippet_digest,
    )


def _source_digest(path: Path, *, error_code: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RefreshError(error_code) from exc


def _new_attempt_id(factory: AttemptIdFactory | None) -> str:
    value = str((factory or (lambda: uuid.uuid4().hex))() or "").strip()
    if not value or len(value) > 256 or "\r" in value or "\n" in value:
        raise RefreshError("manual_registration_attempt_id_invalid")
    return value


def _registration_timestamp(clock: Clock | None) -> str:
    observed = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(observed, datetime):
        raise RefreshError("manual_registration_timestamp_invalid")
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_token(value: object, *, fallback: str = "unknown") -> str:
    token = str(value or "").strip().lower()
    return token if _SAFE_TOKEN.fullmatch(token) else fallback


def _safe_count(value: object) -> int:
    try:
        return min(_MAX_COUNT, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_timestamp(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sanitize_chatgpt(report: Mapping[str, Any]) -> dict[str, Any]:
    registration = _mapping(report.get("registration"))
    process = _mapping(report.get("desktop_process"))
    logs = _mapping(report.get("desktop_logs"))
    status = _safe_token(report.get("observation_status"))
    return {
        "schema_version": SCHEMA_VERSION,
        "target": "chatgpt-desktop-local",
        "observed_at": _safe_timestamp(report.get("generated_at")),
        "observation_status": status,
        "recognition_observation_ready": bool(report.get("recognition_observation_ready")),
        "registration_observed": bool(registration.get("observed")),
        "desktop_process_detected": bool(process.get("detected")),
        "desktop_process_count": _safe_count(process.get("count")),
        "post_registration_process_count": _safe_count(
            process.get("post_registration_process_count")
        ),
        "desktop_restart_required": (
            process.get("restart_required")
            if isinstance(process.get("restart_required"), bool)
            else None
        ),
        "desktop_restart_status": _safe_token(process.get("restart_status")),
        "desktop_log_discovery_status": _safe_token(logs.get("discovery_status")),
        "desktop_log_file_count": _safe_count(logs.get("log_file_count")),
        "post_registration_log_session_observed": bool(
            logs.get("post_registration_session_observed")
        ),
        "mcp_status_list_observed_without_error": bool(
            logs.get("mcp_status_list_observed_without_error")
        ),
        "mcp_status_list_error_observed": _safe_count(
            logs.get("mcp_status_list_error_count")
        )
        > 0,
        "tool_exposure_verified": False,
        "conversation_attachment_verified": False,
        "end_to_end_verified": False,
        "path_details_redacted": True,
    }


def _sanitize_claude(report: Mapping[str, Any]) -> dict[str, Any]:
    installation = _mapping(report.get("installation"))
    registration = _mapping(report.get("registration"))
    config = _mapping(report.get("config_observation"))
    process = _mapping(report.get("desktop_process"))
    logs = _mapping(report.get("desktop_logs"))
    status = _safe_token(report.get("observation_status"))
    return {
        "schema_version": SCHEMA_VERSION,
        "target": "claude-desktop",
        "observed_at": _safe_timestamp(report.get("generated_at")),
        "observation_status": status,
        "recognition_observation_ready": bool(report.get("recognition_observation_ready")),
        "installation_detected": bool(installation.get("detected")),
        "config_observed": bool(config.get("exists")),
        "registration_observed": bool(registration.get("observed")),
        "desktop_process_detected": bool(process.get("detected")),
        "desktop_process_count": _safe_count(process.get("count")),
        "post_registration_process_count": _safe_count(
            process.get("post_registration_process_count")
        ),
        "desktop_restart_required": (
            process.get("restart_required")
            if isinstance(process.get("restart_required"), bool)
            else None
        ),
        "desktop_restart_status": _safe_token(process.get("restart_status")),
        "desktop_log_discovery_succeeded": bool(logs.get("discovery_succeeded")),
        "desktop_log_reads_succeeded": bool(logs.get("reads_succeeded")),
        "post_registration_log_session_observed": bool(
            logs.get("post_registration_session_observed")
        ),
        "post_registration_server_identity_observed": bool(
            logs.get("post_registration_server_name_observed")
        ),
        "loader_observed": bool(report.get("claude_desktop_loader_observed")),
        "loader_verified": False,
        "conversation_attachment_verified": False,
        "end_to_end_verified": False,
        "path_details_redacted": True,
    }


def sanitize_observation(target: str, report: Mapping[str, Any]) -> dict[str, Any]:
    if target == "chatgpt-desktop-local":
        return _sanitize_chatgpt(report)
    if target == "claude-desktop":
        return _sanitize_claude(report)
    raise RefreshError("target_unsupported")


def _chatgpt_probe(
    _target: str,
    bundle_status_path: Path,
    config_path: Path | None,
    _server_name_value: str,
) -> Mapping[str, Any]:
    registration = chatgpt_observer.load_registration_observation(
        bundle_status_path=bundle_status_path,
        config_path=config_path,
    )
    try:
        process_times = chatgpt_observer.discover_desktop_process_start_times()
    except Exception:
        process_times = []
    try:
        candidate_roots = chatgpt_observer.discover_desktop_log_roots()
    except Exception:
        candidate_roots = []
    existing_roots = [path for path in candidate_roots if path.is_dir()]
    log_paths: list[Path] = []
    seen: set[str] = set()
    for root in existing_roots:
        for path in chatgpt_observer.discover_log_files(root):
            key = os.path.normcase(str(path))
            if key not in seen:
                seen.add(key)
                log_paths.append(path)
    sessions = [
        session
        for path in log_paths
        if (session := chatgpt_observer.load_log_session(path)) is not None
    ]
    if not existing_roots:
        discovery_status = "log_root_missing"
    elif not log_paths:
        discovery_status = "logs_not_found"
    elif not sessions:
        discovery_status = "logs_unreadable"
    else:
        discovery_status = "logs_loaded"
    return chatgpt_observer.evaluate_recognition_observation(
        registration=registration,
        process_start_times=process_times,
        log_sessions=sessions,
        log_discovery_status=discovery_status,
        log_root_candidate_count=len(candidate_roots),
        log_root_existing_count=len(existing_roots),
        log_file_count=len(log_paths),
    )


def _claude_probe(
    _target: str,
    bundle_status_path: Path,
    config_path: Path | None,
    server_name_value: str,
) -> Mapping[str, Any]:
    try:
        installation = claude_observer.discover_windows_claude_state()
    except Exception:
        installation = {
            "discovery_succeeded": False,
            "appx_detected": False,
            "legacy_detected": False,
            "process_start_times": [],
        }
    config_observation, config_modified_at = claude_observer.observe_config(config_path)
    registration = claude_observer.load_registration_observation(
        bundle_status_path,
        config_modified_at=config_modified_at,
    )
    log_root = claude_observer.default_log_root()
    log_paths, log_discovery_succeeded = claude_observer.discover_log_files(log_root)
    sessions: list[dict[str, Any]] = []
    log_reads_succeeded = True
    for path in log_paths:
        session, read_succeeded = claude_observer.load_log_session(
            path,
            server_name=server_name_value,
        )
        log_reads_succeeded = log_reads_succeeded and read_succeeded
        if session is not None:
            sessions.append(session)
    return claude_observer.evaluate_claude_desktop_observation(
        installation=installation,
        process_start_times=installation.get("process_start_times") or [],
        registration=registration,
        log_sessions=sessions,
        config_observation=config_observation,
        log_discovery_succeeded=log_discovery_succeeded,
        log_reads_succeeded=log_reads_succeeded,
    )


def _default_probe(
    target: str,
    bundle_status_path: Path,
    config_path: Path | None,
    server_name_value: str,
) -> Mapping[str, Any]:
    if target == "chatgpt-desktop-local":
        return _chatgpt_probe(target, bundle_status_path, config_path, server_name_value)
    if target == "claude-desktop":
        return _claude_probe(target, bundle_status_path, config_path, server_name_value)
    raise RefreshError("target_unsupported")


def _merge_observation_fields(
    status: dict[str, Any], target: str, observation: Mapping[str, Any]
) -> None:
    observed_at = observation.get("observed_at")
    observation_status = str(observation.get("observation_status") or "unknown")
    if target == "chatgpt-desktop-local":
        status["chatgpt_desktop_connection_observation"] = dict(observation)
        status["desktop_recognition_observation_status"] = observation_status
        status["desktop_process_detected"] = bool(observation.get("desktop_process_detected"))
        status["desktop_restart_checked_at"] = observed_at
        status["desktop_restart_required"] = observation.get("desktop_restart_required")
        status["desktop_restart_status"] = str(
            observation.get("desktop_restart_status") or "unknown"
        )
        status["desktop_restart_reason_code"] = observation_status
        status["desktop_restarted_after_registration"] = (
            _safe_count(observation.get("post_registration_process_count")) > 0
            and observation.get("desktop_restart_required") is False
        )
        status["desktop_post_registration_log_session_observed"] = bool(
            observation.get("post_registration_log_session_observed")
        )
        status["desktop_status_scan_request_observed"] = bool(
            observation.get("mcp_status_list_observed_without_error")
        )
        return

    status["claude_desktop_connection_observation"] = dict(observation)
    status["claude_desktop_recognition_observation_status"] = observation_status
    status["claude_desktop_process_detected"] = bool(
        observation.get("desktop_process_detected")
    )
    status["claude_desktop_restart_checked_at"] = observed_at
    status["claude_desktop_restart_required"] = observation.get("desktop_restart_required")
    status["claude_desktop_restart_status"] = str(
        observation.get("desktop_restart_status") or "unknown"
    )
    status["claude_desktop_restarted_after_registration"] = (
        _safe_count(observation.get("post_registration_process_count")) > 0
        and observation.get("desktop_restart_required") is False
    )
    status["claude_desktop_post_registration_log_session_observed"] = bool(
        observation.get("post_registration_log_session_observed")
    )
    status["claude_desktop_server_name_observed"] = bool(
        observation.get("post_registration_server_identity_observed")
    )
    status["claude_desktop_loader_observed"] = bool(observation.get("loader_observed"))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.refresh-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def _commit_v5_manual_registration(
    status: Mapping[str, Any],
    *,
    evidence: ManualRegistrationEvidence,
    attempt_id: str,
    registered_at: str,
) -> dict[str, Any]:
    """Record only the exact Settings registration in the v5 client model."""

    target = "chatgpt-desktop-local"
    started = mcp_client_status.begin_attempt(
        status,
        target,
        attempt_id,
        started_at=registered_at,
    )
    return mcp_client_status.commit_success(
        started,
        target,
        attempt_id,
        verified_stages={
            "registration": {
                "manual_settings_registration_adopted": True,
                "exact_entry_match_verified": True,
            }
        },
        config_entry_fingerprint=evidence.config_fingerprint,
        config_container_fingerprint=evidence.config_fingerprint,
        bundle_fingerprint=status.get("bundle_fingerprint"),
        bundle_location_fingerprint=str(evidence.snippet_path.parent),
        verified_at=registered_at,
    )


def _v5_transport_observation_transition(
    status: Mapping[str, Any],
    *,
    target: str,
    attempt_id: str,
    observation: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Upgrade an exact registration-only v5 scope through transport, and no further."""

    if target != "chatgpt-desktop-local":
        return None
    record = _v5_client_record(status, target)
    if record is None:
        return None
    transport_observed = bool(
        observation.get("recognition_observation_ready")
        and observation.get("registration_observed")
        and observation.get("post_registration_log_session_observed")
        and observation.get("mcp_status_list_observed_without_error")
    )
    if not transport_observed:
        return None

    last_attempt = _mapping(record.get("last_attempt"))
    effective = _mapping(record.get("effective"))
    stages = _mapping(record.get("stages"))
    registration = _mapping(stages.get("registration"))
    transport = _mapping(stages.get("transport"))
    if (
        last_attempt.get("id") != attempt_id
        or last_attempt.get("state") != "completed"
        or effective.get("attempt_id") != attempt_id
        or registration.get("state") != "verified"
        or registration.get("attempt_id") != attempt_id
        or transport.get("state") == "verified"
    ):
        return None
    # This observer may extend only an isolated registration-only scope. It
    # must never rewrite loader, app inventory, client surface, or conversation
    # evidence produced by another setup path.
    for stage_name in (
        "loader",
        "fresh_app_server",
        "client_reload",
        "client_surface",
        "conversation",
    ):
        if _mapping(stages.get(stage_name)).get("state") != "not_checked":
            return None
    if transport.get("state") != "not_checked":
        return None

    config_fingerprint = effective.get("config_entry_fingerprint")
    location_fingerprint = effective.get("bundle_location_fingerprint")
    runtime_fingerprint = status.get("runtime_fingerprint")
    if not config_fingerprint or not location_fingerprint or not runtime_fingerprint:
        return None
    observed_at = str(observation.get("observed_at") or "").strip() or None
    started_at = str(last_attempt.get("started_at") or "").strip() or observed_at
    started = mcp_client_status.begin_attempt(
        status,
        target,
        attempt_id,
        started_at=started_at,
    )
    return mcp_client_status.commit_success(
        started,
        target,
        attempt_id,
        verified_stages={
            "registration": {
                "registration_scope_preserved": True,
                "exact_entry_match_verified": True,
            },
            "transport": {
                "desktop_transport_observed": True,
                "post_registration_session_observed": True,
                "mcp_status_list_observed_without_error": True,
            },
        },
        config_entry_fingerprint=config_fingerprint,
        config_container_fingerprint=effective.get("config_container_fingerprint"),
        runtime_fingerprint=runtime_fingerprint,
        bundle_fingerprint=effective.get("bundle_fingerprint")
        or status.get("bundle_fingerprint"),
        bundle_location_fingerprint=location_fingerprint,
        verified_at=observed_at,
    )


def _commit_manual_registration_adoption(
    path: Path,
    *,
    expected_status_digest: str,
    expected_server_name: str,
    evidence: ManualRegistrationEvidence,
    attempt_id: str,
    registered_at: str,
) -> None:
    current, current_digest = _read_status(path)
    if current_digest != expected_status_digest:
        raise RefreshError("bundle_status_changed_during_manual_adoption")
    _validate_server_identity(current, expected_server_name)
    if _optional_attempt_id(current, "chatgpt-desktop-local") is not None:
        raise RefreshError("installation_attempt_created_during_manual_adoption")
    if (
        _source_digest(
            evidence.config_path,
            error_code="manual_registration_source_changed",
        )
        != evidence.config_source_digest
        or _source_digest(
            evidence.snippet_path,
            error_code="manual_registration_source_changed",
        )
        != evidence.snippet_source_digest
    ):
        raise RefreshError("manual_registration_source_changed")

    is_v5 = _v5_client_record(current, "chatgpt-desktop-local") is not None
    if is_v5:
        current = _commit_v5_manual_registration(
            current,
            evidence=evidence,
            attempt_id=attempt_id,
            registered_at=registered_at,
        )
    legacy_updates = {
        "installation_attempt_id": attempt_id,
        "installation_state": "installed_pending_desktop_verification",
        "connection_state": "pending_desktop_verification",
        "direct_config_registered": True,
        "direct_config_path": str(evidence.config_path),
        "installed_config_fingerprint": evidence.config_fingerprint,
        "desktop_mcp_registration_updated_at": registered_at,
        "direct_config_loader_verified": False,
        "loader_verification_state": "not_checked",
        "loader_verification_reason": "manual_registration_pending_verification",
        "installed_config_transport_verified": False,
        "installed_config_transport_runtime_fingerprint": None,
        "direct_stdio_verified": False,
        "transport_end_to_end_verified": False,
        "fresh_codex_app_server_inventory_verified": False,
        "fresh_codex_app_server_runtime_fingerprint": None,
        "desktop_app_server_loader_verified": False,
        "desktop_app_server_tool_count": 0,
        "desktop_app_server_tool_names": [],
        "desktop_app_server_server_info": None,
        "desktop_app_server_error": None,
        "desktop_recognition_observation_status": "not_checked",
        "desktop_restarted_after_registration": False,
        "desktop_post_registration_log_session_observed": False,
        "desktop_status_scan_request_observed": False,
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
        "conversation_attachment_unverified": True,
        "tool_scan_unverified": True,
        "end_to_end_verified": False,
    }
    if is_v5:
        # Keep the v5 transition's legacy projection authoritative. Only add
        # compatibility metadata that the projection model does not own.
        for projected_key in (
            "installation_attempt_id",
            "installation_state",
            "connection_state",
            "installed_config_fingerprint",
            "direct_config_registered",
            "direct_config_loader_verified",
            "installed_config_transport_verified",
            "direct_stdio_verified",
            "fresh_codex_app_server_inventory_verified",
            "desktop_app_server_loader_verified",
            "desktop_tool_scan_verified",
            "conversation_attachment_verified",
            "transport_end_to_end_verified",
            "end_to_end_verified",
        ):
            legacy_updates.pop(projected_key, None)
    current.update(legacy_updates)
    _atomic_write_json(path, current)


def _commit_observation(
    path: Path,
    *,
    expected_digest: str,
    expected_attempt_id: str,
    expected_server_name: str,
    target: str,
    observation: Mapping[str, Any],
    manual_evidence: ManualRegistrationEvidence | None = None,
) -> None:
    current, current_digest = _read_status(path)
    if current_digest != expected_digest:
        raise RefreshError("bundle_status_changed_during_observation")
    if not hmac.compare_digest(
        _existing_attempt_id(current, target).encode("utf-8"),
        expected_attempt_id.encode("utf-8"),
    ):
        raise RefreshError("installation_attempt_changed_during_observation")
    _validate_server_identity(current, expected_server_name)
    if manual_evidence is not None and (
        _source_digest(
            manual_evidence.config_path,
            error_code="manual_registration_source_changed_during_observation",
        )
        != manual_evidence.config_source_digest
        or _source_digest(
            manual_evidence.snippet_path,
            error_code="manual_registration_source_changed_during_observation",
        )
        != manual_evidence.snippet_source_digest
    ):
        raise RefreshError("manual_registration_source_changed_during_observation")
    transitioned = _v5_transport_observation_transition(
        current,
        target=target,
        attempt_id=expected_attempt_id,
        observation=observation,
    )
    if transitioned is not None:
        current = transitioned
    _merge_observation_fields(current, target, observation)
    if _existing_attempt_id(current, target) != expected_attempt_id:
        raise RefreshError("installation_attempt_preservation_failed")
    _atomic_write_json(path, current)


def _safe_result(
    *,
    target: str,
    observation: Mapping[str, Any],
    status_updated: bool,
    manual_registration_adopted: bool,
) -> dict[str, Any]:
    return {
        "report_type": "mcp_client_connection_refresh",
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "ok": bool(observation.get("recognition_observation_ready")),
        "status_updated": status_updated,
        "installation_attempt_preserved": status_updated and not manual_registration_adopted,
        "installation_attempt_created_by_explicit_adoption": manual_registration_adopted,
        "manual_registration_adopted": manual_registration_adopted,
        "connection_verified": False,
        "conversation_attachment_verified": False,
        "observation": dict(observation),
        "path_details_redacted": True,
    }


def _safe_error(
    target: str,
    code: str,
    *,
    status_updated: bool = False,
    manual_registration_adopted: bool = False,
) -> dict[str, Any]:
    return {
        "report_type": "mcp_client_connection_refresh",
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "ok": False,
        "status_updated": status_updated,
        "manual_registration_adopted": manual_registration_adopted,
        "connection_verified": False,
        "error_code": _safe_token(code, fallback="refresh_failed"),
        "path_details_redacted": True,
    }


def _emit(payload: Mapping[str, Any], *, output: TextIO, out_json: Path | None) -> None:
    if out_json is not None:
        _atomic_write_json(out_json, payload)
    output.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def run(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    probe: ConnectionProbe | None = None,
    clock: Clock | None = None,
    attempt_id_factory: AttemptIdFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    out_json_for_emit = args.out_json
    status_updated = False
    manual_registration_adopted = False
    manual_evidence: ManualRegistrationEvidence | None = None
    try:
        if args.adopt_manual_registration and args.target != "chatgpt-desktop-local":
            raise RefreshError("manual_registration_target_unsupported")
        if args.out_json is not None:
            try:
                if args.out_json.resolve(strict=False) == args.bundle_status.resolve(strict=False):
                    out_json_for_emit = None
                    raise RefreshError("out_json_must_not_replace_bundle_status")
            except OSError as exc:
                raise RefreshError("output_path_validation_failed") from exc
        status, digest = _read_status(args.bundle_status)
        _validate_server_identity(status, args.server_name)
        attempt_id = _optional_attempt_id(status, args.target)
        if attempt_id is None:
            if not args.adopt_manual_registration:
                raise RefreshError("installation_attempt_id_missing")
            if args.bundle_dir is None:
                raise RefreshError("manual_registration_bundle_dir_required")
            selected_config_path = args.codex_config or _default_codex_config_path()
            if selected_config_path is None:
                raise RefreshError("manual_registration_config_path_unavailable")
            manual_evidence = _manual_registration_evidence(
                bundle_dir=args.bundle_dir,
                bundle_status_path=args.bundle_status,
                config_path=selected_config_path,
                server_name=args.server_name,
            )
            attempt_id = _new_attempt_id(attempt_id_factory)
            registered_at = _registration_timestamp(clock)
            _commit_manual_registration_adoption(
                args.bundle_status,
                expected_status_digest=digest,
                expected_server_name=args.server_name,
                evidence=manual_evidence,
                attempt_id=attempt_id,
                registered_at=registered_at,
            )
            status_updated = True
            manual_registration_adopted = True
            status, digest = _read_status(args.bundle_status)
            if not hmac.compare_digest(
                _existing_attempt_id(status, args.target).encode("utf-8"),
                attempt_id.encode("utf-8"),
            ):
                raise RefreshError("manual_registration_attempt_commit_failed")
        config_path = _config_path_from_status(status, args.target)
        raw_report = (probe or _default_probe)(
            args.target,
            args.bundle_status,
            config_path,
            args.server_name,
        )
        if not isinstance(raw_report, Mapping):
            raise RefreshError("observer_report_invalid")
        observation = sanitize_observation(args.target, raw_report)
        if observation.get("observed_at") is None:
            observation["observed_at"] = datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        _commit_observation(
            args.bundle_status,
            expected_digest=digest,
            expected_attempt_id=attempt_id,
            expected_server_name=args.server_name,
            target=args.target,
            observation=observation,
            manual_evidence=manual_evidence,
        )
        status_updated = True
        result = _safe_result(
            target=args.target,
            observation=observation,
            status_updated=True,
            manual_registration_adopted=manual_registration_adopted,
        )
        _emit(result, output=output, out_json=out_json_for_emit)
        if args.fail_on_issue and not observation["recognition_observation_ready"]:
            return 2
        return 0
    except RefreshError as exc:
        result = _safe_error(
            args.target,
            exc.code,
            status_updated=status_updated,
            manual_registration_adopted=manual_registration_adopted,
        )
    except Exception:
        result = _safe_error(
            args.target,
            "refresh_failed",
            status_updated=status_updated,
            manual_registration_adopted=manual_registration_adopted,
        )
    try:
        _emit(result, output=output, out_json=out_json_for_emit)
    except Exception:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
