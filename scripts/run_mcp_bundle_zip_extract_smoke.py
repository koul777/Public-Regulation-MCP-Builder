from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import tomllib
from typing import Any, Sequence, TextIO
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit  # noqa: E402
from scripts.generate_mcp_client_config import (  # noqa: E402
    OMISSION_DISPOSITION_SNAPSHOT_FILENAME,
    validate_mcp_runtime_data_bundle_integrity,
)


def run_mcp_bundle_zip_extract_smoke(
    *,
    bundle_zip: str | Path,
    extract_dir: str | Path,
    out_json: str | Path | None = None,
    server_name: str = "regulation_mcp",
    timeout_seconds: float = 60.0,
    overwrite: bool = False,
    require_console_scripts: bool = False,
) -> dict[str, Any]:
    # The installed console script can run from any working directory while
    # ``PROJECT_ROOT`` points inside site-packages.  Resolve operator-provided
    # paths before the child PowerShell process changes its working directory.
    zip_path = Path(bundle_zip).expanduser().resolve()
    target_dir = Path(extract_dir).expanduser().resolve()
    if not zip_path.is_file():
        raise FileNotFoundError(f"Bundle zip not found: {zip_path}")
    if target_dir.exists():
        if not overwrite:
            raise FileExistsError(f"Extract dir already exists: {target_dir}. Pass --overwrite to replace it.")
        _safe_remove_dir(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    _extract_archive_safely(zip_path, target_dir)
    runtime_manifest_path = target_dir / "data" / "mcp_runtime_manifest.json"
    runtime_integrity_checked = runtime_manifest_path.is_file()
    if runtime_integrity_checked:
        validate_mcp_runtime_data_bundle_integrity(target_dir / "data")

    # Running this module through ``python path\to\script.py`` does not add
    # that interpreter's Scripts directory to PATH.  Keep the smoke child and
    # its console-script discovery on the same Python runtime that launched
    # this check, instead of accidentally falling back to stale global tools.
    child_env = os.environ.copy()
    interpreter_bin = str(Path(sys.executable).resolve().parent)
    inherited_path = child_env.get("PATH", "")
    child_env["PATH"] = os.pathsep.join(
        part for part in (interpreter_bin, inherited_path) if part
    )
    child_env["REG_RAG_PYTHON"] = str(Path(sys.executable).resolve())
    child_env["REG_RAG_PYTHON_PROJECT_ROOT"] = str(PROJECT_ROOT)

    required_console_scripts = (
        "reg-rag-mcp-client-config-smoke",
        "reg-rag-mcp-server",
    )
    console_script_resolution = {
        name: shutil.which(name, path=child_env["PATH"]) for name in required_console_scripts
    }
    missing_console_scripts = [
        name for name, resolved in console_script_resolution.items() if not resolved
    ]

    script_path = target_dir / "validate_client_config_smoke.ps1"
    powershell = _powershell_command()
    if powershell is None:
        raise RuntimeError("PowerShell was not found; cannot run validate_client_config_smoke.ps1.")
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        env=child_env,
    )
    client_smoke_path = target_dir / "mcp_client_config_smoke.json"
    client_smoke = _read_json(client_smoke_path)
    path_checks = _client_config_path_checks(target_dir=target_dir, server_name=server_name)
    report = {
        "report_type": "mcp_bundle_zip_extract_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "bundle_zip": str(zip_path),
        "extract_dir": str(target_dir),
        "server_name": server_name,
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "client_smoke_report": str(client_smoke_path),
        "client_smoke_passed": bool(client_smoke.get("passed")),
        "path_checks": path_checks,
        "required_console_scripts": list(required_console_scripts),
        "console_script_resolution": console_script_resolution,
        "missing_console_scripts": missing_console_scripts,
        "require_console_scripts": bool(require_console_scripts),
        "environment_checks_passed": not (require_console_scripts and missing_console_scripts),
        "runtime_integrity_checked": runtime_integrity_checked,
        "runtime_integrity_passed": True,
        "passed": bool(
            completed.returncode == 0
            and client_smoke.get("passed")
            and path_checks.get("passed")
            and not (require_console_scripts and missing_console_scripts)
        ),
    }
    if out_json is not None:
        out_path = Path(out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _safe_remove_dir(path: Path) -> None:
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Refusing to remove filesystem root: {path}")
    if len(resolved.parts) < 4:
        raise ValueError(f"Refusing to remove shallow directory: {path}")
    shutil.rmtree(resolved)


def _extract_archive_safely(
    archive_path: Path,
    destination: Path,
    *,
    max_entries: int = 2048,
    max_entry_bytes: int = 64 * 1024 * 1024,
    max_total_bytes: int = 256 * 1024 * 1024,
) -> None:
    """Extract only regular, bounded members into the requested directory."""
    destination = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > max_entries:
            raise ValueError(f"Bundle archive has too many entries: {len(members)} > {max_entries}.")
        total_bytes = 0
        seen_targets: set[Path] = set()
        for member in members:
            posix_name = PurePosixPath(member.filename)
            windows_name = PureWindowsPath(member.filename)
            if (
                not member.filename
                or posix_name.is_absolute()
                or windows_name.is_absolute()
                or ".." in posix_name.parts
                or ".." in windows_name.parts
                or "\x00" in member.filename
                or (windows_name.drive and windows_name.drive != "")
            ):
                raise ValueError(f"Unsafe bundle archive member: {member.filename}")
            mode = (member.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise ValueError(f"Symlink bundle archive member is not allowed: {member.filename}")
            if member.file_size > max_entry_bytes:
                raise ValueError(
                    f"Bundle archive member is too large: {member.filename} ({member.file_size} bytes)."
                )
            total_bytes += member.file_size
            if total_bytes > max_total_bytes:
                raise ValueError(f"Bundle archive exceeds the uncompressed size limit ({max_total_bytes} bytes).")
            target = (destination / Path(*posix_name.parts)).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"Bundle archive member escapes extraction directory: {member.filename}")
            if target in seen_targets:
                raise ValueError(f"Duplicate bundle archive member path: {member.filename}")
            seen_targets.add(target)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not _runtime_archive_member_allowed(posix_name):
                raise ValueError(
                    "Bundle archive runtime member is outside the sealed handoff allowlist: "
                    + member.filename
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)


def _runtime_archive_member_allowed(path: PurePosixPath) -> bool:
    parts = path.parts
    if not parts or parts[0] != "data":
        return True
    if len(parts) == 2 and parts[1] == ".keep":
        return True
    normalized = path.as_posix()
    if normalized == "data/mcp_runtime_manifest.json":
        return True
    if normalized in {
        "data/repository/manifest.json",
        "data/repository/approval_snapshot.json",
        f"data/repository/{OMISSION_DISPOSITION_SNAPSHOT_FILENAME}",
        "data/repository/journals/approvals.jsonl",
        "data/repository/journals/indexing_jobs.jsonl",
        "data/hierarchy/regulation_hierarchy.sqlite3",
    }:
        return True
    if (
        len(parts) == 3
        and parts[:2] == ("data", "repository")
        and parts[2].endswith("_chunks.json")
        and parts[2] != "_chunks.json"
    ):
        return True
    if (
        len(parts) == 4
        and parts[:2] == ("data", "vector_db")
        and bool(parts[2])
        and parts[3] in {"approved_vectors.jsonl", "bm25_index.json"}
    ):
        return True
    return False


def _powershell_command() -> str | None:
    for candidate in ("powershell.exe", "powershell", "pwsh"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _client_config_path_checks(*, target_dir: Path, server_name: str) -> dict[str, Any]:
    expected_launcher = str((target_dir / "run_mcp_stdio_server.ps1").resolve())
    expected_data_dir = str((target_dir / "data").resolve())
    codex = _codex_server(target_dir / "codex_config_snippet.toml", server_name)
    claude = _claude_desktop_server(target_dir / "claude_desktop_config.json", server_name)
    chatgpt_legacy_guard = _chatgpt_desktop_warning_artifact_check(target_dir)
    clients = {
        "codex": _entry_path_check(codex, expected_launcher=expected_launcher, expected_data_dir=expected_data_dir),
        "claude_desktop": _entry_path_check(claude, expected_launcher=expected_launcher, expected_data_dir=expected_data_dir),
        # This is a packaging-policy guard, not a connection smoke.  A passing
        # result proves only that the retained legacy filename is warning-only
        # and cannot be mistaken for a runnable local ChatGPT configuration.
        "chatgpt_desktop_local": chatgpt_legacy_guard,
    }
    return {
        "passed": all(bool(client.get("passed")) for client in clients.values()),
        "expected_launcher": expected_launcher,
        "expected_data_dir": expected_data_dir,
        "clients": clients,
    }


def _codex_server(path: Path, server_name: str) -> dict[str, Any]:
    payload = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
    entry = servers.get(server_name) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


def _claude_desktop_server(path: Path, server_name: str) -> dict[str, Any]:
    payload = _read_json(path)
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    entry = servers.get(server_name) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


def _chatgpt_desktop_warning_artifact_check(target_dir: Path) -> dict[str, Any]:
    path = target_dir / "chatgpt_desktop_local_mcp.json"
    base_result: dict[str, Any] = {
        "desktop_config_path": str(path),
        "guard_type": "warning_only_compatibility_artifact",
        "connection_verified": False,
        "process_started": False,
        "runnable_config_present": None,
    }
    runnable_fields: list[str] = []
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("forbidden UTF-8 BOM EF BB BF")
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            **base_result,
            "passed": False,
            "guard_passed": False,
            "strict_utf8_without_bom": False,
            "config_schema_verified": False,
            "encoding_error": str(exc),
        }
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        if not isinstance(payload, dict):
            raise ValueError("legacy compatibility artifact must be a JSON object")
        if payload.get("support_status") != "unsupported":
            raise ValueError("support_status must equal unsupported")
        if payload.get("direct_local_supported") is not False:
            raise ValueError("direct_local_supported must equal false")
        if (
            "chatgpt_direct_local_mcp_supported" in payload
            and payload.get("chatgpt_direct_local_mcp_supported") is not False
        ):
            raise ValueError("chatgpt_direct_local_mcp_supported must equal false")
        runnable_fields = sorted(
            key
            for key in ("mcpServers", "ui_fields", "command", "args", "cwd", "env")
            if key in payload
        )
        if runnable_fields:
            raise ValueError(
                "warning-only artifact must not contain runnable fields: "
                + ", ".join(runnable_fields)
            )
        return {
            **base_result,
            "passed": True,
            "guard_passed": True,
            "strict_utf8_without_bom": True,
            "config_schema_verified": True,
            "warning_only_artifact_verified": True,
            "runnable_config_present": False,
            "support_status": "unsupported",
            "direct_local_supported": False,
            "chatgpt_direct_local_mcp_supported": payload.get(
                "chatgpt_direct_local_mcp_supported"
            ),
            "runnable_fields_present": [],
        }
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            **base_result,
            "passed": False,
            "guard_passed": False,
            "strict_utf8_without_bom": True,
            "config_schema_verified": False,
            "runnable_config_present": bool(runnable_fields),
            "runnable_fields_present": runnable_fields,
            "schema_error": str(exc),
        }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _entry_path_check(entry: dict[str, Any], *, expected_launcher: str, expected_data_dir: str) -> dict[str, Any]:
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    launcher = _arg_value(args, "-File")
    data_dir = _arg_value(args, "--data-dir")
    return {
        "passed": bool(
            str(entry.get("command") or "").lower() == "powershell.exe"
            and _same_path(launcher, expected_launcher)
            and _same_path(data_dir, expected_data_dir)
            and "--no-warm-cache" in [str(arg) for arg in args]
        ),
        "command": entry.get("command"),
        "launcher": launcher,
        "data_dir": data_dir,
        "has_no_warm_cache": "--no-warm-cache" in [str(arg) for arg in args],
    }


def _arg_value(args: Sequence[Any], flag: str) -> str | None:
    values = [str(arg) for arg in args]
    for index, value in enumerate(values[:-1]):
        if value == flag:
            return values[index + 1]
    return None


def _same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract an MCP bundle zip and verify local client config recognition.")
    parser.add_argument("--bundle-zip", required=True)
    parser.add_argument("--extract-dir", required=True)
    parser.add_argument("--server-name", default="regulation_mcp")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--require-console-scripts",
        action="store_true",
        help="Fail unless the client smoke and server console scripts resolve from the active environment.",
    )
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        report = run_mcp_bundle_zip_extract_smoke(
            bundle_zip=args.bundle_zip,
            extract_dir=args.extract_dir,
            server_name=args.server_name,
            timeout_seconds=args.timeout_seconds,
            out_json=args.out_json,
            overwrite=args.overwrite,
            require_console_scripts=args.require_console_scripts,
        )
    except Exception as exc:
        report = {
            "report_type": "mcp_bundle_zip_extract_smoke",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repo_commit": current_repo_commit(PROJECT_ROOT),
            "passed": False,
            "error": str(exc),
        }
        if args.out_json:
            out_path = Path(args.out_json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_issue and not report.get("passed"):
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
