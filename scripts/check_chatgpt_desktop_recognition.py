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
from typing import Any, Iterable, Sequence, TextIO


_TIMESTAMP_PREFIX = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))"
)
_MCP_STATUS_METHOD = "mcpServerStatus/list"
_ERROR_CODE = re.compile(r"\berrorCode=(?P<value>[^\s]+)", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n\t]*")
_USER_PATH = re.compile(r"(?i)\b(?:users|home)[\\/][^\\/\s]+")


@dataclass(frozen=True)
class RegistrationObservation:
    occurred_at: datetime | None
    source: str


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Parse a timestamp and normalize it to timezone-aware UTC."""

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


def parse_desktop_log_text(
    text: str,
    *,
    fallback_started_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Summarize one Desktop log without retaining raw lines or local paths.

    A successful ``mcpServerStatus/list`` event means only that Desktop routed
    the inventory request without an observed error. It does not prove that a
    particular MCP server or any tools were exposed to a conversation.
    """

    timestamps: list[datetime] = []
    status_events: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        timestamp_match = _TIMESTAMP_PREFIX.match(line)
        line_timestamp = parse_timestamp(timestamp_match.group("timestamp")) if timestamp_match else None
        if line_timestamp is not None:
            timestamps.append(line_timestamp)
        if _MCP_STATUS_METHOD not in line:
            continue
        error_match = _ERROR_CODE.search(line)
        error_value = error_match.group("value").strip('"\',') if error_match else None
        has_error = bool(error_value and error_value.casefold() not in {"null", "none", "false", "0"})
        if re.search(r"(?i)\b(?:failed|failure|exception)\b", line):
            has_error = True
        status_events.append(
            {
                "occurred_at": isoformat_utc(line_timestamp),
                "error_observed": has_error,
                "successful_response_observed": (
                    not has_error
                    and "response" in line.casefold()
                    and (error_value is None or error_value.casefold() in {"null", "none", "false", "0"})
                ),
            }
        )

    fallback = parse_timestamp(fallback_started_at)
    started_at = min(timestamps) if timestamps else fallback
    ended_at = max(timestamps) if timestamps else fallback
    success_count = sum(bool(event["successful_response_observed"]) for event in status_events)
    error_count = sum(bool(event["error_observed"]) for event in status_events)
    return {
        "started_at": isoformat_utc(started_at),
        "ended_at": isoformat_utc(ended_at),
        "mcp_status_list_event_count": len(status_events),
        "mcp_status_list_success_count": success_count,
        "mcp_status_list_error_count": error_count,
        "mcp_status_list_events": status_events,
    }


def evaluate_recognition_observation(
    *,
    registration: RegistrationObservation,
    process_start_times: Iterable[str | datetime],
    log_sessions: Iterable[dict[str, Any]],
    generated_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate registration, restart, and status-list evidence without overclaiming."""

    registration_at = registration.occurred_at
    processes = sorted(
        parsed
        for value in process_start_times
        if (parsed := parse_timestamp(value)) is not None
    )
    sessions = [dict(session) for session in log_sessions]
    sessions.sort(key=lambda item: parse_timestamp(item.get("started_at")) or datetime.max.replace(tzinfo=timezone.utc))

    if registration_at is None:
        restart_status = "registration_time_unknown"
        restart_required: bool | None = None
        post_registration_processes: list[datetime] = []
    elif not processes:
        restart_status = "desktop_not_running"
        restart_required = False
        post_registration_processes = []
    elif processes[0] < registration_at:
        restart_status = "running_process_predates_registration"
        restart_required = True
        post_registration_processes = [item for item in processes if item >= registration_at]
    else:
        restart_status = "running_process_started_after_registration"
        restart_required = False
        post_registration_processes = list(processes)

    post_registration_sessions: list[dict[str, Any]] = []
    if registration_at is not None:
        post_registration_sessions = [
            session
            for session in sessions
            if (started := parse_timestamp(session.get("started_at"))) is not None and started >= registration_at
        ]

    post_registration_successes = 0
    post_registration_errors = 0
    for session in post_registration_sessions:
        for event in session.get("mcp_status_list_events", []):
            if not isinstance(event, dict):
                continue
            occurred_at = parse_timestamp(event.get("occurred_at"))
            if registration_at is not None and occurred_at is not None and occurred_at < registration_at:
                continue
            post_registration_successes += int(bool(event.get("successful_response_observed")))
            post_registration_errors += int(bool(event.get("error_observed")))

    new_session_observed = bool(post_registration_sessions)
    status_list_observed_without_error = post_registration_successes > 0
    observation_ready = bool(
        registration_at is not None
        and post_registration_processes
        and not restart_required
        and new_session_observed
        and status_list_observed_without_error
    )
    if registration_at is None:
        observation_status = "registration_time_unknown"
    elif restart_required:
        observation_status = "restart_required"
    elif not processes:
        observation_status = "desktop_not_running"
    elif not new_session_observed:
        observation_status = "post_registration_log_session_not_observed"
    elif not status_list_observed_without_error:
        observation_status = "mcp_status_list_success_not_observed"
    else:
        observation_status = "restart_and_mcp_status_list_observed"

    generated = parse_timestamp(generated_at) or datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "report_type": "chatgpt_desktop_recognition_observation",
        "generated_at": isoformat_utc(generated),
        "observation_status": observation_status,
        "recognition_observation_ready": observation_ready,
        "registration": {
            "observed": registration_at is not None,
            "occurred_at": isoformat_utc(registration_at),
            "source": registration.source,
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
            "session_count": len(sessions),
            "post_registration_session_count": len(post_registration_sessions),
            "post_registration_session_observed": new_session_observed,
            "mcp_status_list_success_count": post_registration_successes,
            "mcp_status_list_error_count": post_registration_errors,
            "mcp_status_list_observed_without_error": status_list_observed_without_error,
        },
        # These values are deliberately fixed. A Desktop inventory request is
        # not proof of a tool scan result or attachment to a conversation.
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
        "conversation_attachment_unverified": True,
        "end_to_end_verified": False,
        "path_details_redacted": True,
    }
    report["support_summary"] = build_support_summary(report)
    return report


def build_support_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Return a path-free, username-free summary suitable for support."""

    process = report.get("desktop_process") if isinstance(report.get("desktop_process"), dict) else {}
    logs = report.get("desktop_logs") if isinstance(report.get("desktop_logs"), dict) else {}
    registration = report.get("registration") if isinstance(report.get("registration"), dict) else {}
    summary = {
        "status": str(report.get("observation_status") or "unknown"),
        "registration_observed": bool(registration.get("observed")),
        "registration_source": str(registration.get("source") or "unknown"),
        "desktop_process_detected": bool(process.get("detected")),
        "desktop_restart_required": process.get("restart_required"),
        "post_registration_log_session_observed": bool(logs.get("post_registration_session_observed")),
        "mcp_status_list_observed_without_error": bool(logs.get("mcp_status_list_observed_without_error")),
        "tool_exposure_not_verified": True,
        "conversation_attachment_not_verified": True,
    }
    return _redact_support_value(summary)


def _redact_support_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_support_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_support_value(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = _WINDOWS_ABSOLUTE_PATH.sub("[local-path-redacted]", value)
    redacted = _USER_PATH.sub("[user-path-redacted]", redacted)
    return redacted


def load_registration_observation(
    *,
    registration_time: str | None = None,
    bundle_status_path: Path | None = None,
    config_path: Path | None = None,
) -> RegistrationObservation:
    explicit = parse_timestamp(registration_time)
    if explicit is not None:
        return RegistrationObservation(explicit, "explicit")
    if bundle_status_path is not None:
        try:
            payload = json.loads(bundle_status_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            for key in ("desktop_mcp_registration_updated_at", "desktop_plugin_registration_updated_at"):
                parsed = parse_timestamp(payload.get(key))
                if parsed is not None:
                    return RegistrationObservation(parsed, f"bundle_status:{key}")
    if config_path is not None:
        try:
            return RegistrationObservation(
                datetime.fromtimestamp(config_path.stat().st_mtime, tz=timezone.utc),
                "config_mtime",
            )
        except OSError:
            pass
    return RegistrationObservation(None, "unavailable")


def discover_desktop_process_start_times() -> list[datetime]:
    if os.name != "nt":
        return []
    script = (
        "$items=@(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | ForEach-Object {"
        "try { $_.StartTime.ToUniversalTime().ToString('o') } catch {} });"
        "$items | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    values = payload if isinstance(payload, list) else [payload]
    return sorted(parsed for value in values if (parsed := parse_timestamp(value)) is not None)


def default_desktop_log_root() -> Path | None:
    local_app_data = str(os.getenv("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        return None
    return (
        Path(local_app_data)
        / "Packages"
        / "OpenAI.Codex_2p2nqsd0c76g0"
        / "LocalCache"
        / "Local"
        / "Codex"
        / "Logs"
    )


def discover_log_files(log_root: Path | None) -> list[Path]:
    if log_root is None or not log_root.is_dir():
        return []
    try:
        return sorted(
            (path for path in log_root.rglob("*.log") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return []


def load_log_session(path: Path) -> dict[str, Any] | None:
    try:
        fallback = datetime.fromtimestamp(path.stat().st_ctime, tz=timezone.utc)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_desktop_log_text(text, fallback_started_at=fallback)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Observe whether ChatGPT Desktop restarted after MCP registration and routed an "
            "error-free mcpServerStatus/list response. This does not verify tool exposure."
        )
    )
    parser.add_argument("--registration-time")
    parser.add_argument("--bundle-status", type=Path)
    parser.add_argument("--config-path", type=Path)
    parser.add_argument("--process-start-time", action="append", default=[])
    parser.add_argument("--log-root", type=Path)
    parser.add_argument("--log-file", type=Path, action="append", default=[])
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    registration = load_registration_observation(
        registration_time=args.registration_time,
        bundle_status_path=args.bundle_status,
        config_path=args.config_path,
    )
    process_times = (
        [parsed for value in args.process_start_time if (parsed := parse_timestamp(value)) is not None]
        if args.process_start_time
        else discover_desktop_process_start_times()
    )
    log_paths = list(args.log_file) if args.log_file else discover_log_files(args.log_root or default_desktop_log_root())
    sessions = [session for path in log_paths if (session := load_log_session(path)) is not None]
    report = evaluate_recognition_observation(
        registration=registration,
        process_start_times=process_times,
        log_sessions=sessions,
    )
    config_exists = bool(args.config_path is not None and args.config_path.is_file())
    config_sha256 = None
    if config_exists:
        try:
            config_sha256 = "sha256:" + hashlib.sha256(args.config_path.read_bytes()).hexdigest()
        except OSError:
            config_exists = False
    report["config_observation"] = {
        "exists": config_exists,
        "content_sha256": config_sha256,
    }
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
