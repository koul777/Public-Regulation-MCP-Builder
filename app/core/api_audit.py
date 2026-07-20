from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re
from threading import Lock
import time
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from app.core.config import Settings

if TYPE_CHECKING:
    from app.core.security import AuthContext


_API_AUDIT_LOCK = Lock()
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30.0
WINDOWS_ABSOLUTE_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")
UNC_PATH_RE = re.compile(r"\\\\[^\\]+\\[^\\]+")
PATH_WITH_EXTENSION_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Za-z]:[\\/]|\\\\[^\\/\r\n\"'<>`]+[\\/][^\\/\r\n\"'<>`]+[\\/]|"
    r"/(?:Users|home|var|tmp|mnt|workspace|data|app)/)"
    r"[^\"'<>`\r\n]*?\."
    r"(?:pdf|docx|hwpx|hwp|jsonl?|csv|md|txt|log|db|sqlite|tmp|yaml|yml|py|png|jpe?g|gif|bmp|webp)",
    re.IGNORECASE,
)
WINDOWS_QUOTED_PATH_RE = re.compile(
    r"(?:&\s*)?(?P<quote>['\"])[A-Za-z]:[\\/][^'\"\r\n]+(?P=quote)"
)
REQUIRED_API_AUDIT_FIELDS = ("actor", "tenant_id", "auth_mode", "action", "outcome", "status_code")
ALLOWED_OUTCOMES = {"success", "failure", "denied"}


def append_api_audit_record(settings: Settings, record: dict[str, Any]) -> dict[str, Any]:
    if not settings.api_audit_enabled:
        return {"recorded": False, **record}
    validate_api_audit_record(record)
    audit_record = {
        "record_id": f"api_{uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    audit_path = api_audit_path(settings)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with _API_AUDIT_LOCK, _audit_file_lock(audit_path.parent):
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return audit_record


def api_audit_path(settings: Settings) -> Path:
    return settings.data_dir / "repository" / "api_audit.jsonl"


def audit_api_event(
    settings: Settings,
    auth_context: AuthContext,
    *,
    action: str,
    outcome: str,
    status_code: int,
    resource_type: str = "",
    document_id: str = "",
    job_id: str = "",
    filename: str = "",
    export_format: str = "",
    source_system: str = "",
    source_record_id: str = "",
    source_file_id: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return append_api_audit_record(
        settings,
        {
            "actor": auth_context.actor,
            "tenant_id": auth_context.tenant_id,
            "auth_mode": auth_context.auth_mode,
            "api_role": auth_context.role,
            "action": action,
            "resource_type": resource_type,
            "document_id": document_id,
            "job_id": job_id,
            "filename": PureWindowsPath(filename).name if filename else "",
            "export_format": export_format,
            "source_system": _redacted_provenance(source_system),
            "source_record_id": _redacted_provenance(source_record_id),
            "source_file_id": _redacted_provenance(source_file_id),
            "outcome": outcome,
            "status_code": status_code,
            "detail": redact_sensitive_paths(detail),
        },
    )


def _redacted_provenance(value: str) -> str:
    """Sanitize a user-controlled provenance field for the audit record.

    ``source_system``/``source_record_id``/``source_file_id`` come straight
    from the upload request, so a path-shaped value would otherwise fail
    ``validate_api_audit_record`` and crash the write — leaving the committed
    upload with no audit record.  Redact embedded paths, then flag any residual
    bare prefix (e.g. ``/tmp/``) that redaction leaves intact so the field can
    never look like a local path.
    """

    redacted = redact_sensitive_paths(value)
    if _looks_like_sensitive_path(redacted):
        return "[local-path-redacted]"
    return redacted


def redact_sensitive_paths(value: str) -> str:
    if not value:
        return ""
    # PowerShell commonly presents an operator-opened artifact as
    # ``& 'C:\\Users\\...\\file.png'``.  Match the quoted command as a
    # whole so neither the invocation operator nor a filename suffix leaks.
    redacted = WINDOWS_QUOTED_PATH_RE.sub("[local-path-redacted]", str(value))
    redacted = PATH_WITH_EXTENSION_RE.sub("[local-path-redacted]", redacted)
    redacted = re.sub(
        r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>`]+",
        "[local-path-redacted]",
        redacted,
    )
    redacted = re.sub(r"\\\\[^\s\"'<>`]+", "[local-path-redacted]", redacted)
    redacted = re.sub(
        r"(?<![\w.-])/(?:Users|home|var|tmp|mnt|workspace|data|app)/[^\"'<>`]+",
        "[local-path-redacted]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def validate_api_audit_record(record: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_API_AUDIT_FIELDS if record.get(field) in (None, "")]
    if missing:
        raise ValueError(f"API audit record is missing required fields: {', '.join(missing)}")
    if record.get("outcome") not in ALLOWED_OUTCOMES:
        raise ValueError("API audit outcome must be success, failure, or denied.")
    try:
        status_code = int(record["status_code"])
    except (TypeError, ValueError):
        raise ValueError("API audit status_code must be an integer.") from None
    if status_code < 100 or status_code > 599:
        raise ValueError("API audit status_code must be a valid HTTP status code.")
    leaks = _sensitive_path_leaks(record)
    if leaks:
        first = leaks[0]
        raise ValueError(f"API audit record contains a local path in {first['path']}: {first['value']}")


def _sensitive_path_leaks(payload: Any) -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    _collect_sensitive_path_leaks(payload, path="$", leaks=leaks)
    return leaks


def _collect_sensitive_path_leaks(value: Any, *, path: str, leaks: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_sensitive_path_leaks(item, path=f"{path}.{key}", leaks=leaks)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_sensitive_path_leaks(item, path=f"{path}[{index}]", leaks=leaks)
        return
    if isinstance(value, str) and _looks_like_sensitive_path(value):
        leaks.append({"path": path, "value": value})


def _looks_like_sensitive_path(value: str) -> bool:
    normalized = value.strip()
    return bool(
        WINDOWS_ABSOLUTE_RE.search(normalized)
        or UNC_PATH_RE.search(normalized)
        or normalized.startswith(("/Users/", "/home/", "/var/", "/tmp/", "/mnt/", "/workspace/", "/data/", "/app/"))
    )


@contextmanager
def _audit_file_lock(root: Path):
    lock_path = root / ".api_audit.lock"
    with lock_path.open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for API audit lock: {handle.name}")
                time.sleep(_LOCK_POLL_SECONDS)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
