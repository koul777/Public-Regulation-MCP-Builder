from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence, TextIO
from uuid import uuid4


DEFAULT_REQUIRED_TOOLS = ("get_index_status", "search", "fetch")
MCP_STATUS_PAGE_LIMIT = 100
MCP_STATUS_MAX_PAGES = 100


def check_codex_app_server_mcp(
    *,
    server_name: str,
    required_tools: Sequence[str] = DEFAULT_REQUIRED_TOOLS,
    timeout_seconds: float = 30.0,
    codex_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Probe MCP inventory through a fresh Codex app-server process.

    This is intentionally stronger than parsing config.toml or accepting a
    successful ``codex mcp get`` exit code: the fresh process must initialize
    the MCP child and return its discovered tools.  It is compatibility
    evidence for the API used by Desktop, not proof that a running Desktop
    process scanned or attached the server to a conversation.
    """

    probe_id = str(uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    normalized_name = str(server_name or "").strip()
    required = sorted({str(item).strip() for item in required_tools if str(item).strip()})
    command = list(codex_command or _codex_app_server_command())
    provenance = _probe_provenance(command)
    if not normalized_name:
        return _failed_report(
            server_name="",
            required_tools=required,
            error="server_name is required",
            probe_id=probe_id,
            generated_at=generated_at,
            provenance=provenance,
            reason_code="server_name_required",
        )
    if not command:
        return _failed_report(
            server_name=normalized_name,
            required_tools=required,
            error="Codex CLI was not found on PATH.",
            probe_id=probe_id,
            generated_at=generated_at,
            provenance=provenance,
            reason_code="codex_cli_unavailable",
        )

    process: subprocess.Popen[str] | None = None
    messages: queue.Queue[tuple[str, str]] = queue.Queue()
    stderr_tail: list[str] = []
    initialized = False
    status_received = False
    status_request_id = 2
    page_count = 0
    entries: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    try:
        process = subprocess.Popen(
            [*command, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        provenance["process_id"] = process.pid
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        threading.Thread(target=_read_lines, args=(process.stdout, "stdout", messages), daemon=True).start()
        threading.Thread(target=_read_lines, args=(process.stderr, "stderr", messages), daemon=True).start()
        _send_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "reg-rag-mcp-desktop-loader-check", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            try:
                source, line = messages.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if source == "stderr":
                if line:
                    stderr_tail.append(line)
                    stderr_tail = stderr_tail[-10:]
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("id") == 1:
                if payload.get("error"):
                    return _failed_report(
                        server_name=normalized_name,
                        required_tools=required,
                        error=f"Codex app-server initialize failed: {_safe_json(payload.get('error'))}",
                        probe_id=probe_id,
                        generated_at=generated_at,
                        provenance=provenance,
                        reason_code="app_server_initialize_failed",
                    )
                initialized = isinstance(payload.get("result"), dict)
                if not initialized:
                    return _failed_report(
                        server_name=normalized_name,
                        required_tools=required,
                        error="Codex app-server returned no initialize result.",
                        probe_id=probe_id,
                        generated_at=generated_at,
                        provenance=provenance,
                        reason_code="app_server_initialize_result_missing",
                    )
                _send_message(process, {"method": "initialized"})
                _send_status_list_request(process, request_id=status_request_id)
                continue
            if payload.get("id") != status_request_id:
                continue
            status_received = True
            if payload.get("error"):
                return _failed_report(
                    server_name=normalized_name,
                    required_tools=required,
                    initialized=initialized,
                    status_received=True,
                    error=f"Codex app-server MCP inventory failed: {_safe_json(payload.get('error'))}",
                    probe_id=probe_id,
                    generated_at=generated_at,
                    provenance=provenance,
                    reason_code="mcp_inventory_failed",
                    server_count=len(entries),
                    page_count=page_count,
                )
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            page_entries = result.get("data") if isinstance(result.get("data"), list) else []
            entries.extend(item for item in page_entries if isinstance(item, dict))
            page_count += 1
            next_cursor_value = result.get("nextCursor")
            next_cursor = str(next_cursor_value).strip() if next_cursor_value is not None else ""
            if next_cursor:
                if next_cursor in seen_cursors:
                    return _failed_report(
                        server_name=normalized_name,
                        required_tools=required,
                        initialized=initialized,
                        status_received=True,
                        server_count=len(entries),
                        page_count=page_count,
                        error="Codex app-server MCP inventory returned a repeated pagination cursor.",
                        probe_id=probe_id,
                        generated_at=generated_at,
                        provenance=provenance,
                        reason_code="pagination_cursor_cycle",
                    )
                if page_count >= MCP_STATUS_MAX_PAGES:
                    return _failed_report(
                        server_name=normalized_name,
                        required_tools=required,
                        initialized=initialized,
                        status_received=True,
                        server_count=len(entries),
                        page_count=page_count,
                        error="Codex app-server MCP inventory exceeded the pagination safety limit.",
                        probe_id=probe_id,
                        generated_at=generated_at,
                        provenance=provenance,
                        reason_code="pagination_limit_exceeded",
                    )
                seen_cursors.add(next_cursor)
                status_request_id += 1
                _send_status_list_request(
                    process,
                    request_id=status_request_id,
                    cursor=next_cursor,
                )
                continue

            matching = [item for item in entries if item.get("name") == normalized_name]
            if not matching:
                return _failed_report(
                    server_name=normalized_name,
                    required_tools=required,
                    initialized=initialized,
                    status_received=True,
                    server_count=len(entries),
                    error=f"Codex app-server did not list MCP server {normalized_name}.",
                    probe_id=probe_id,
                    generated_at=generated_at,
                    provenance=provenance,
                    reason_code="server_not_found",
                    page_count=page_count,
                )
            if len(matching) != 1:
                return _failed_report(
                    server_name=normalized_name,
                    required_tools=required,
                    initialized=initialized,
                    status_received=True,
                    server_count=len(entries),
                    server_found=True,
                    matching_server_count=len(matching),
                    page_count=page_count,
                    error=f"Codex app-server listed MCP server {normalized_name} more than once.",
                    probe_id=probe_id,
                    generated_at=generated_at,
                    provenance=provenance,
                    reason_code="duplicate_server_name",
                )
            entry = matching[0]
            tools = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
            tool_names = sorted(str(name) for name in tools if str(name).strip())
            missing = sorted(set(required) - set(tool_names))
            server_info = entry.get("serverInfo") if isinstance(entry.get("serverInfo"), dict) else None
            config_scope = provenance.get("config_scope") if isinstance(provenance.get("config_scope"), dict) else {}
            config_content_after = _active_config_content_fingerprint()
            config_scope["config_content_sha256_after_process"] = config_content_after
            config_scope["config_content_stable_during_probe"] = (
                config_scope.get("config_content_sha256_before_process") == config_content_after
            )
            config_scope["config_content_sha256"] = (
                config_content_after if config_scope["config_content_stable_during_probe"] else None
            )
            config_stable = bool(config_scope["config_content_stable_during_probe"])
            passed = not missing and bool(tool_names) and config_stable
            if missing:
                error = f"Required MCP tools were not discovered: {', '.join(missing)}"
                reason_code = "required_tools_missing"
            elif not config_stable:
                error = "Codex config changed while the fresh app-server probe was running."
                reason_code = "config_changed_during_probe"
            else:
                error = None
                reason_code = None
            return {
                "report_type": "codex_app_server_mcp_status",
                "probe_scope": "fresh_codex_app_server_process",
                "probe_id": probe_id,
                "generated_at": generated_at,
                "provenance": provenance,
                "passed": passed,
                "app_server_initialized": initialized,
                "status_list_received": True,
                "server_name": normalized_name,
                "server_found": True,
                "server_count": len(entries),
                "matching_server_count": 1,
                "page_count": page_count,
                "pagination_exhausted": True,
                "auth_status": entry.get("authStatus"),
                "tool_count": len(tool_names),
                "tool_names": tool_names,
                "required_tools": required,
                "missing_tools": missing,
                "server_info": server_info,
                "error": error,
                "reason_code": reason_code,
                "timeout_reason": None,
            }
        if process.poll() is not None:
            timeout_reason = None
            reason_code = "app_server_exited"
            detail = "Codex app-server exited before MCP status was returned."
        elif not initialized:
            timeout_reason = "initialize_timeout"
            reason_code = "timeout"
            detail = "Timed out waiting for Codex app-server initialization."
        else:
            timeout_reason = "mcp_status_list_timeout"
            reason_code = "timeout"
            detail = "Timed out waiting for Codex app-server MCP status."
        if stderr_tail:
            # App-server diagnostics may contain local paths or upstream MCP
            # error details. Keep the report actionable without copying those
            # diagnostics into a handoff artifact.
            detail = f"{detail} app-server wrote {len(stderr_tail)} diagnostic line(s) to stderr."
        return _failed_report(
            server_name=normalized_name,
            required_tools=required,
            initialized=initialized,
            status_received=status_received,
            server_count=len(entries),
            page_count=page_count,
            error=detail,
            probe_id=probe_id,
            generated_at=generated_at,
            provenance=provenance,
            reason_code=reason_code,
            timeout_reason=timeout_reason,
        )
    except (OSError, ValueError) as exc:
        return _failed_report(
            server_name=normalized_name,
            required_tools=required,
            initialized=initialized,
            status_received=status_received,
            error=f"Could not run Codex app-server ({exc.__class__.__name__}).",
            probe_id=probe_id,
            generated_at=generated_at,
            provenance=provenance,
            reason_code="app_server_launch_failed",
            server_count=len(entries),
            page_count=page_count,
        )
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                # EOF lets app-server close any MCP children before we fall
                # back to terminating the process on timeout.
                process.stdin.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _codex_app_server_command() -> list[str]:
    configured = str(os.getenv("CODEX_NATIVE_EXECUTABLE") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return [str(candidate)] if candidate.is_file() else []
    discovered = shutil.which("codex")
    if not discovered:
        return []
    path = Path(discovered)
    if path.suffix.casefold() == ".exe":
        return [str(path)]
    if os.name == "nt":
        package_root = path.parent / "node_modules" / "@openai" / "codex"
        native = sorted(package_root.glob("node_modules/@openai/codex-win32-*/vendor/*/codex.exe"))
        if native:
            return [str(native[0])]
    return [str(path)]


def _probe_provenance(command: Sequence[str]) -> dict[str, Any]:
    executable = _normalized_executable_path(command[0]) if command else None
    default_codex_home = _normalized_path(Path.home() / ".codex")
    configured_home = str(os.getenv("CODEX_HOME") or "").strip()
    if configured_home:
        codex_home = _normalized_path(configured_home)
        config_source = "CODEX_HOME"
    else:
        codex_home = default_codex_home
        config_source = "default_user_home"
    config_path = _normalized_path(Path(codex_home) / "config.toml") if codex_home else None
    config_content_before = _file_content_fingerprint(config_path)
    return {
        "executable_path": executable,
        "executable_version": _executable_version(executable),
        "process_id": None,
        "config_scope": {
            "source": config_source,
            "uses_default_codex_home": os.path.normcase(codex_home) == os.path.normcase(default_codex_home),
            "codex_home_sha256": _path_fingerprint(codex_home),
            "config_path_sha256": _path_fingerprint(config_path),
            "config_file_name": "config.toml",
            "config_exists": bool(config_path and Path(config_path).is_file()),
            "config_content_sha256_before_process": config_content_before,
            "config_content_sha256_after_process": None,
            "config_content_stable_during_probe": None,
            "config_content_sha256": None,
        },
    }


def _normalized_executable_path(value: str) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    discovered = shutil.which(candidate)
    return _normalized_path(discovered or candidate)


def _normalized_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _path_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    normalized = os.path.normcase(value).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _active_config_content_fingerprint() -> str | None:
    configured_home = str(os.getenv("CODEX_HOME") or "").strip()
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return _file_content_fingerprint(codex_home / "config.toml")


def _file_content_fingerprint(value: str | Path | None) -> str | None:
    if value is None:
        return None
    try:
        payload = Path(value).read_bytes()
    except OSError:
        return None
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _executable_version(executable: str | None) -> str | None:
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return output[:200] or None


def _read_lines(stream: TextIO, source: str, messages: queue.Queue[tuple[str, str]]) -> None:
    for line in stream:
        messages.put((source, line.rstrip("\r\n")))


def _send_message(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise OSError("Codex app-server stdin is not available.")
    process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _send_status_list_request(
    process: subprocess.Popen[str],
    *,
    request_id: int,
    cursor: str | None = None,
) -> None:
    params: dict[str, Any] = {"detail": "full", "limit": MCP_STATUS_PAGE_LIMIT}
    if cursor:
        params["cursor"] = cursor
    _send_message(
        process,
        {
            "id": request_id,
            "method": "mcpServerStatus/list",
            "params": params,
        },
    )


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))[:1000]


def _failed_report(
    *,
    server_name: str,
    required_tools: Sequence[str],
    error: str,
    initialized: bool = False,
    status_received: bool = False,
    server_count: int | None = None,
    server_found: bool = False,
    matching_server_count: int = 0,
    page_count: int = 0,
    probe_id: str | None = None,
    generated_at: str | None = None,
    provenance: dict[str, Any] | None = None,
    reason_code: str | None = None,
    timeout_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "report_type": "codex_app_server_mcp_status",
        "probe_scope": "fresh_codex_app_server_process",
        "probe_id": probe_id or str(uuid4()),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "provenance": provenance or _probe_provenance([]),
        "passed": False,
        "app_server_initialized": initialized,
        "status_list_received": status_received,
        "server_name": server_name,
        "server_found": server_found,
        "server_count": server_count,
        "matching_server_count": matching_server_count,
        "page_count": page_count,
        "pagination_exhausted": False,
        "auth_status": None,
        "tool_count": 0,
        "tool_names": [],
        "required_tools": list(required_tools),
        "missing_tools": list(required_tools),
        "server_info": None,
        "error": error,
        "reason_code": reason_code,
        "timeout_reason": timeout_reason,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an MCP through a fresh Codex app-server inventory process."
    )
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--require-tool", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--codex-executable",
        help="Probe this Codex executable instead of the first codex command on PATH.",
    )
    parser.add_argument("--out-json")
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or __import__("sys").stdout
    required = args.require_tool or list(DEFAULT_REQUIRED_TOOLS)
    report = check_codex_app_server_mcp(
        server_name=args.server_name,
        required_tools=required,
        timeout_seconds=args.timeout_seconds,
        codex_command=[args.codex_executable] if args.codex_executable else None,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    output.write(rendered + "\n")
    if args.out_json:
        path = Path(args.out_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 2 if args.fail_on_issue and not report["passed"] else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
