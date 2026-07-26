from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO


SCHEMA_VERSION = "claude-desktop-connection-observation-v1"
_TIMESTAMP = re.compile(
    r"^\s*\[?(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))\]?"
)
_ERROR_MARKER = re.compile(
    r"(?i)\b(?:denied|eacces|enoent|error|exception|failed|failure|invalid|timeout)\b"
)
_REGISTRATION_TIME_KEYS = (
    "claude_desktop_registration_updated_at",
    "claude_desktop_config_registered_at",
)


@dataclass(frozen=True)
class RegistrationObservation:
    occurred_at: datetime | None
    source: str
    bundle_status_read_succeeded: bool


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Parse an ISO timestamp and normalize it to timezone-aware UTC."""

    if isinstance(value, datetime):
        parsed = value
    else:
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
    return parsed.astimezone(timezone.utc)


def isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_claude_log_text(
    text: str,
    *,
    server_name: str,
    fallback_started_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Return only timestamp and exact-name-hit metadata from one log.

    Raw log text, filenames, local paths, usernames, and matching lines are not
    retained. A server-name hit is an observation, not proof that Claude exposed
    a tool or attached it to a conversation.
    """

    normalized_name = str(server_name or "").casefold()
    timestamps: list[datetime] = []
    hits: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        timestamp_match = _TIMESTAMP.match(line)
        line_timestamp = (
            parse_timestamp(timestamp_match.group("timestamp"))
            if timestamp_match
            else None
        )
        if line_timestamp is not None:
            timestamps.append(line_timestamp)
        if not normalized_name or normalized_name not in line.casefold():
            continue
        hits.append(
            {
                "occurred_at": isoformat_utc(line_timestamp),
                "error_marker_observed": bool(_ERROR_MARKER.search(line)),
            }
        )

    fallback = parse_timestamp(fallback_started_at)
    started_at = min(timestamps) if timestamps else fallback
    ended_at = max(timestamps) if timestamps else fallback
    return {
        "started_at": isoformat_utc(started_at),
        "ended_at": isoformat_utc(ended_at),
        "server_name_hit_count": len(hits),
        "server_name_error_hit_count": sum(
            bool(hit["error_marker_observed"]) for hit in hits
        ),
        "server_name_hits": hits,
    }


def evaluate_claude_desktop_observation(
    *,
    installation: Mapping[str, Any],
    process_start_times: Iterable[str | datetime],
    registration: RegistrationObservation,
    log_sessions: Iterable[Mapping[str, Any]],
    config_observation: Mapping[str, Any],
    log_discovery_succeeded: bool,
    log_reads_succeeded: bool,
    generated_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate read-only Claude Desktop evidence without overclaiming."""

    discovery_succeeded = bool(installation.get("discovery_succeeded"))
    appx_detected = bool(installation.get("appx_detected")) if discovery_succeeded else False
    legacy_detected = bool(installation.get("legacy_detected")) if discovery_succeeded else False
    installed = bool(appx_detected or legacy_detected) if discovery_succeeded else False
    processes = sorted(
        parsed
        for value in process_start_times
        if (parsed := parse_timestamp(value)) is not None
    )
    if not discovery_succeeded:
        processes = []

    registration_at = registration.occurred_at
    sessions = [dict(session) for session in log_sessions]
    sessions.sort(
        key=lambda item: parse_timestamp(item.get("started_at"))
        or datetime.max.replace(tzinfo=timezone.utc)
    )
    post_registration_sessions: list[dict[str, Any]] = []
    post_registration_hits = 0
    post_registration_non_error_hits = 0
    if registration_at is not None:
        for session in sessions:
            session_started = parse_timestamp(session.get("started_at"))
            session_hits = session.get("server_name_hits")
            session_hits = session_hits if isinstance(session_hits, list) else []
            hits_after_registration = 0
            non_error_hits_after_registration = 0
            for hit in session_hits:
                if not isinstance(hit, Mapping):
                    continue
                hit_at = parse_timestamp(hit.get("occurred_at"))
                is_post_registration = (
                    hit_at >= registration_at
                    if hit_at is not None
                    else bool(session_started and session_started >= registration_at)
                )
                if not is_post_registration:
                    continue
                hits_after_registration += 1
                if not bool(hit.get("error_marker_observed")):
                    non_error_hits_after_registration += 1
            if session_started is not None and session_started >= registration_at:
                post_registration_sessions.append(session)
            elif hits_after_registration:
                post_registration_sessions.append(session)
            post_registration_hits += hits_after_registration
            post_registration_non_error_hits += non_error_hits_after_registration

    post_registration_processes = (
        [started for started in processes if registration_at and started >= registration_at]
        if registration_at is not None
        else []
    )
    if not discovery_succeeded:
        restart_status = "windows_discovery_failed"
        restart_required: bool | None = None
    elif registration_at is None:
        restart_status = "registration_time_unknown"
        restart_required = None
    elif not processes:
        restart_status = "desktop_not_running"
        restart_required = False
    elif processes[0] < registration_at:
        restart_status = "running_process_predates_registration"
        restart_required = True
    else:
        restart_status = "running_process_started_after_registration"
        restart_required = False

    config_exists = bool(config_observation.get("exists"))
    config_read_succeeded = bool(config_observation.get("read_succeeded"))
    content_sha256 = (
        str(config_observation.get("content_sha256") or "").strip() or None
        if config_exists and config_read_succeeded
        else None
    )
    server_name_hit_observed = post_registration_hits > 0
    loader_observation_ready = bool(
        discovery_succeeded
        and installed
        and config_exists
        and config_read_succeeded
        and registration_at is not None
        and registration.bundle_status_read_succeeded
        and post_registration_processes
        and restart_required is False
        and log_discovery_succeeded
        and log_reads_succeeded
        and post_registration_sessions
        and server_name_hit_observed
        and post_registration_non_error_hits > 0
    )

    if not discovery_succeeded:
        observation_status = "windows_discovery_failed"
    elif not installed:
        observation_status = "claude_desktop_not_detected"
    elif not config_exists or not config_read_succeeded:
        observation_status = "config_not_observed"
    elif registration_at is None or not registration.bundle_status_read_succeeded:
        observation_status = "registration_time_not_verified"
    elif restart_required:
        observation_status = "desktop_restart_required"
    elif not processes:
        observation_status = "desktop_not_running"
    elif not log_discovery_succeeded or not log_reads_succeeded:
        observation_status = "desktop_log_discovery_failed"
    elif not post_registration_sessions:
        observation_status = "post_registration_log_session_not_observed"
    elif not server_name_hit_observed:
        observation_status = "post_registration_server_name_not_observed"
    elif post_registration_non_error_hits == 0:
        observation_status = "only_server_name_error_hits_observed"
    else:
        observation_status = "restart_and_server_name_observed"

    generated = parse_timestamp(generated_at) or datetime.now(timezone.utc)
    report = {
        "report_type": "claude_desktop_connection_observation",
        "schema_version": SCHEMA_VERSION,
        "generated_at": isoformat_utc(generated),
        "observation_status": observation_status,
        "recognition_observation_ready": loader_observation_ready,
        "installation": {
            "discovery_succeeded": discovery_succeeded,
            "appx_detected": appx_detected,
            "legacy_detected": legacy_detected,
            "detected": installed,
        },
        "registration": {
            "observed": registration_at is not None,
            "occurred_at": isoformat_utc(registration_at),
            "source": registration.source,
            "bundle_status_read_succeeded": registration.bundle_status_read_succeeded,
        },
        "config_observation": {
            "exists": config_exists,
            "content_sha256": content_sha256,
        },
        "desktop_process": {
            "detected": bool(processes),
            "count": len(processes),
            "earliest_started_at": isoformat_utc(processes[0]) if processes else None,
            "latest_started_at": isoformat_utc(processes[-1]) if processes else None,
            "post_registration_process_count": len(post_registration_processes),
            "restart_required": restart_required,
            "restart_status": restart_status,
        },
        "desktop_logs": {
            "discovery_succeeded": bool(log_discovery_succeeded),
            "reads_succeeded": bool(log_reads_succeeded),
            "session_count": len(sessions),
            "post_registration_session_count": len(post_registration_sessions),
            "post_registration_session_observed": bool(post_registration_sessions),
            "post_registration_server_name_hit_count": post_registration_hits,
            "post_registration_non_error_server_name_hit_count": post_registration_non_error_hits,
            "post_registration_server_name_observed": server_name_hit_observed,
        },
        # A sanitized log hit is useful restart/loader-observation evidence but
        # is not sufficient proof of a successful tool inventory or a tool call.
        "claude_desktop_loader_observed": loader_observation_ready,
        "claude_desktop_loader_verified": False,
        "claude_desktop_conversation_verified": False,
        "conversation_attachment_unverified": True,
        "end_to_end_verified": False,
        "path_details_redacted": True,
    }
    report["support_summary"] = {
        "status": observation_status,
        "app_detected": installed,
        "config_observed": bool(config_exists and config_read_succeeded),
        "registration_observed": registration_at is not None,
        "desktop_process_detected": bool(processes),
        "desktop_restart_required": restart_required,
        "post_registration_log_session_observed": bool(post_registration_sessions),
        "post_registration_server_name_observed": server_name_hit_observed,
        "loader_observation_ready": loader_observation_ready,
        "loader_not_verified": True,
        "conversation_not_verified": True,
    }
    return report


def discover_windows_claude_state() -> dict[str, Any]:
    """Discover only booleans and process timestamps; fail closed on errors."""

    failed = {
        "discovery_succeeded": False,
        "appx_detected": False,
        "legacy_detected": False,
        "process_start_times": [],
    }
    if os.name != "nt":
        return failed
    script = r'''
$ErrorActionPreference = "Stop"
$AppxDetected = @(Get-AppxPackage -ErrorAction Stop | Where-Object {
  $_.Name -match '(?i)claude'
}).Count -gt 0
$LegacyCandidates = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\Claude\Claude.exe'),
  (Join-Path $env:LOCALAPPDATA 'AnthropicClaude\Claude.exe'),
  (Join-Path $env:ProgramFiles 'Claude\Claude.exe'),
  (Join-Path $env:ProgramFiles 'Anthropic\Claude.exe')
)
if (${env:ProgramFiles(x86)}) {
  $LegacyCandidates += Join-Path ${env:ProgramFiles(x86)} 'Claude\Claude.exe'
}
$LegacyDetected = @($LegacyCandidates | Where-Object {
  $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
}).Count -gt 0
$ProcessStarts = @()
$ProcessReadSucceeded = $true
foreach ($Process in @(Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -eq 'Claude' })) {
  try { $ProcessStarts += $Process.StartTime.ToUniversalTime().ToString('o') }
  catch { $ProcessReadSucceeded = $false }
}
[pscustomobject]@{
  discovery_succeeded = [bool]$ProcessReadSucceeded
  appx_detected = [bool]$AppxDetected
  legacy_detected = [bool]$LegacyDetected
  process_start_times = @($ProcessStarts)
} | ConvertTo-Json -Compress
'''
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return failed
    if completed.returncode != 0 or not completed.stdout.strip():
        return failed
    try:
        payload = json.loads(completed.stdout.strip().lstrip("\ufeff"))
    except json.JSONDecodeError:
        return failed
    if not isinstance(payload, dict) or payload.get("discovery_succeeded") is not True:
        return failed
    raw_times = payload.get("process_start_times")
    raw_times = raw_times if isinstance(raw_times, list) else [raw_times] if raw_times else []
    process_times = [
        isoformat_utc(parsed)
        for value in raw_times
        if (parsed := parse_timestamp(value)) is not None
    ]
    return {
        "discovery_succeeded": True,
        "appx_detected": bool(payload.get("appx_detected")),
        "legacy_detected": bool(payload.get("legacy_detected")),
        "process_start_times": process_times,
    }


def observe_config(path: Path | None) -> tuple[dict[str, Any], datetime | None]:
    if path is None or not path.is_file():
        return {"exists": False, "read_succeeded": False, "content_sha256": None}, None
    try:
        payload = path.read_bytes()
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return {"exists": True, "read_succeeded": False, "content_sha256": None}, None
    return {
        "exists": True,
        "read_succeeded": True,
        "content_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }, modified_at


def load_registration_observation(
    bundle_status_path: Path | None,
    *,
    config_modified_at: datetime | None,
) -> RegistrationObservation:
    status_read_succeeded = bundle_status_path is None
    if bundle_status_path is not None:
        try:
            payload = json.loads(bundle_status_path.read_text(encoding="utf-8-sig"))
            status_read_succeeded = isinstance(payload, dict)
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
            status_read_succeeded = False
        if isinstance(payload, dict):
            for key in _REGISTRATION_TIME_KEYS:
                parsed = parse_timestamp(payload.get(key))
                if parsed is not None:
                    return RegistrationObservation(parsed, "bundle_status", status_read_succeeded)
    if config_modified_at is not None:
        return RegistrationObservation(
            config_modified_at,
            "config_mtime",
            status_read_succeeded,
        )
    return RegistrationObservation(None, "unavailable", status_read_succeeded)


def default_config_path() -> Path | None:
    app_data = str(os.getenv("APPDATA") or "").strip()
    return Path(app_data) / "Claude" / "claude_desktop_config.json" if app_data else None


def default_log_root() -> Path | None:
    app_data = str(os.getenv("APPDATA") or "").strip()
    return Path(app_data) / "Claude" / "logs" if app_data else None


def discover_log_files(log_root: Path | None) -> tuple[list[Path], bool]:
    if log_root is None or not log_root.exists():
        return [], True
    if not log_root.is_dir():
        return [], False
    try:
        paths = [path for path in log_root.rglob("*.log") if path.is_file()]
        paths.sort(key=lambda path: path.stat().st_mtime)
    except OSError:
        return [], False
    return paths, True


def load_log_session(path: Path, *, server_name: str) -> tuple[dict[str, Any] | None, bool]:
    try:
        fallback = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, False
    return parse_claude_log_text(
        text,
        server_name=server_name,
        fallback_started_at=fallback,
    ), True


def _server_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or "\r" in normalized or "\n" in normalized:
        raise argparse.ArgumentTypeError("server name must be 1-256 characters without line breaks")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Observe Claude Desktop installation, restart, and sanitized post-registration "
            "server-name log evidence without claiming conversation attachment."
        )
    )
    parser.add_argument("--bundle-status", type=Path)
    parser.add_argument("--config-path", type=Path)
    parser.add_argument("--server-name", required=True, type=_server_name)
    parser.add_argument("--log-root", type=Path)
    parser.add_argument("--log-file", type=Path, action="append", default=[])
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    windows_discovery: Callable[[], dict[str, Any]] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    try:
        installation = (windows_discovery or discover_windows_claude_state)()
    except Exception:
        installation = {
            "discovery_succeeded": False,
            "appx_detected": False,
            "legacy_detected": False,
            "process_start_times": [],
        }
    if not isinstance(installation, Mapping):
        installation = {
            "discovery_succeeded": False,
            "appx_detected": False,
            "legacy_detected": False,
            "process_start_times": [],
        }
    config_path = args.config_path or default_config_path()
    config_observation, config_modified_at = observe_config(config_path)
    registration = load_registration_observation(
        args.bundle_status,
        config_modified_at=config_modified_at,
    )

    log_paths: list[Path] = list(args.log_file)
    discovery_root = args.log_root
    if discovery_root is None and not args.log_file:
        discovery_root = default_log_root()
    discovered_paths, log_discovery_succeeded = discover_log_files(discovery_root)
    seen: set[Path] = set()
    unique_log_paths: list[Path] = []
    for path in [*log_paths, *discovered_paths]:
        try:
            key = path.resolve(strict=False)
        except (OSError, RuntimeError):
            key = path
        if key in seen:
            continue
        seen.add(key)
        unique_log_paths.append(path)

    sessions: list[dict[str, Any]] = []
    log_reads_succeeded = True
    for path in unique_log_paths:
        session, read_succeeded = load_log_session(path, server_name=args.server_name)
        log_reads_succeeded = log_reads_succeeded and read_succeeded
        if session is not None:
            sessions.append(session)

    report = evaluate_claude_desktop_observation(
        installation=installation,
        process_start_times=installation.get("process_start_times") or [],
        registration=registration,
        log_sessions=sessions,
        config_observation=config_observation,
        log_discovery_succeeded=log_discovery_succeeded,
        log_reads_succeeded=log_reads_succeeded,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    output.write(rendered)
    if args.fail_on_issue and not report["recognition_observation_ready"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
