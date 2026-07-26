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
_DESKTOP_PROCESS_NAME = "ChatGPT"
_KNOWN_DESKTOP_PACKAGE_FAMILIES = ("OpenAI.Codex_2p2nqsd0c76g0",)
_DESKTOP_LOG_RELATIVE_PATHS = (
    Path("LocalCache") / "Local" / "Codex" / "Logs",
    Path("LocalCache") / "Local" / "ChatGPT" / "Logs",
)
_SAFE_PACKAGE_FAMILY = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


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
    log_discovery_status: str | None = None,
    log_root_candidate_count: int = 0,
    log_root_existing_count: int = 0,
    log_file_count: int | None = None,
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
    status_list_observed_without_error = (
        post_registration_successes > 0 and post_registration_errors == 0
    )
    normalized_log_discovery_status = str(log_discovery_status or "").strip() or (
        "logs_loaded" if sessions else "not_checked"
    )
    observed_log_file_count = len(sessions) if log_file_count is None else max(0, int(log_file_count))
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
    elif normalized_log_discovery_status == "log_root_missing":
        observation_status = "desktop_log_root_not_found"
    elif normalized_log_discovery_status == "logs_not_found":
        observation_status = "desktop_log_files_not_found"
    elif normalized_log_discovery_status == "logs_unreadable":
        observation_status = "desktop_log_files_unreadable"
    elif not new_session_observed:
        observation_status = "post_registration_log_session_not_observed"
    elif post_registration_errors > 0:
        observation_status = "mcp_status_list_error_observed"
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
            "discovery_status": normalized_log_discovery_status,
            "root_candidate_count": max(0, int(log_root_candidate_count)),
            "existing_root_count": max(0, int(log_root_existing_count)),
            "log_file_count": observed_log_file_count,
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
        "desktop_log_discovery_status": str(logs.get("discovery_status") or "not_checked"),
        "desktop_log_root_found": _safe_nonnegative_count(logs.get("existing_root_count")) > 0,
        "desktop_log_files_found": _safe_nonnegative_count(logs.get("log_file_count")) > 0,
        "mcp_status_list_observed_without_error": bool(logs.get("mcp_status_list_observed_without_error")),
        "mcp_status_list_error_observed": (
            _safe_nonnegative_count(logs.get("mcp_status_list_error_count")) > 0
        ),
        "tool_exposure_not_verified": True,
        "conversation_attachment_not_verified": True,
    }
    return _redact_support_value(summary)


def _safe_nonnegative_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
        f"$items=@(Get-Process -Name {_DESKTOP_PROCESS_NAME} -ErrorAction SilentlyContinue | ForEach-Object {{"
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


def _is_desktop_package_family(value: object) -> bool:
    family = str(value or "").strip()
    if not family or not _SAFE_PACKAGE_FAMILY.fullmatch(family):
        return False
    normalized = family.casefold()
    return normalized.startswith("openai.") and (
        "codex" in normalized or "chatgpt" in normalized
    )


def _discover_appx_desktop_package_families() -> list[str]:
    """Return safe package-family identities for installed ChatGPT.exe apps."""

    if os.name != "nt":
        return []
    script = (
        "$items=@(Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {"
        "$_.InstallLocation -and "
        "(Test-Path -LiteralPath (Join-Path $_.InstallLocation 'app\\ChatGPT.exe') -PathType Leaf)"
        "} | ForEach-Object { [string]$_.PackageFamilyName });"
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
    return sorted({str(value).strip() for value in values if _is_desktop_package_family(value)})


def discover_desktop_log_roots(
    *,
    local_app_data: str | Path | None = None,
    package_family_names: Iterable[str] | None = None,
) -> list[Path]:
    """Build safe log-root candidates without exposing them in reports."""

    local_root_text = str(local_app_data or os.getenv("LOCALAPPDATA") or "").strip()
    if not local_root_text:
        return []
    local_root = Path(local_root_text)
    packages_root = local_root / "Packages"
    families: set[str] = set(_KNOWN_DESKTOP_PACKAGE_FAMILIES)
    if package_family_names is None:
        families.update(_discover_appx_desktop_package_families())
        if packages_root.is_dir():
            try:
                families.update(
                    path.name
                    for path in packages_root.iterdir()
                    if path.is_dir() and _is_desktop_package_family(path.name)
                )
            except OSError:
                pass
    else:
        families.update(
            str(value).strip()
            for value in package_family_names
            if _is_desktop_package_family(value)
        )

    roots: list[Path] = []
    seen: set[str] = set()
    for family in sorted(families):
        if not _is_desktop_package_family(family):
            continue
        package_root = packages_root / family
        for relative_path in _DESKTOP_LOG_RELATIVE_PATHS:
            candidate = package_root / relative_path
            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                roots.append(candidate)
    return roots


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
    if args.log_file:
        candidate_log_roots: list[Path] = []
        existing_log_roots: list[Path] = []
        log_paths = list(args.log_file)
        log_discovery_status = "explicit_log_files"
    else:
        candidate_log_roots = [args.log_root] if args.log_root is not None else discover_desktop_log_roots()
        existing_log_roots = [path for path in candidate_log_roots if path.is_dir()]
        log_paths = []
        seen_log_paths: set[str] = set()
        for root in existing_log_roots:
            for path in discover_log_files(root):
                key = os.path.normcase(str(path))
                if key not in seen_log_paths:
                    seen_log_paths.add(key)
                    log_paths.append(path)
        if not existing_log_roots:
            log_discovery_status = "log_root_missing"
        elif not log_paths:
            log_discovery_status = "logs_not_found"
        else:
            log_discovery_status = "logs_discovered"
    sessions = [session for path in log_paths if (session := load_log_session(path)) is not None]
    if log_paths and not sessions:
        log_discovery_status = "logs_unreadable"
    elif sessions:
        log_discovery_status = "logs_loaded"
    report = evaluate_recognition_observation(
        registration=registration,
        process_start_times=process_times,
        log_sessions=sessions,
        log_discovery_status=log_discovery_status,
        log_root_candidate_count=len(candidate_log_roots),
        log_root_existing_count=len(existing_log_roots),
        log_file_count=len(log_paths),
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
