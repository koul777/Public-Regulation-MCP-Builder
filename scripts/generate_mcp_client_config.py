from __future__ import annotations

import argparse
import base64
import copy
from contextlib import closing, contextmanager
import ctypes
from datetime import date, datetime, timezone
import errno
import hashlib
import hmac
import importlib.util
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from functools import wraps
from uuid import uuid4
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.mcp_bundle_contract import (  # noqa: E402
    ALL_SETUP_BUNDLE_FILES,
    LEGACY_CONNECTION_ARTIFACT_FILENAMES,
    SETUP_BUNDLE_FILES,
)
from scripts.mcp_client_status import (  # noqa: E402
    create_bundle_status as create_client_connection_status,
    invalidate_runtime as invalidate_client_connection_runtime,
)
from app.api import routes_rag  # noqa: E402
from app.core.tenant_access import resource_visible_to_tenant, tenant_directory_key  # noqa: E402
from app.ingestion.vector_adapter import stable_content_hash  # noqa: E402
from app.mcp_server.regulation_tools import mcp_auth_context, settings_for_mcp_project  # noqa: E402
from app.retrieval.bm25_index import (  # noqa: E402
    BM25_INDEX_VERSION,
    BM25_STRUCTURED_METADATA_VERSION,
    load_bm25_index,
    source_content_hashes,
    write_bm25_index,
)
from app.retrieval.hierarchical_index import (  # noqa: E402
    HIERARCHICAL_INDEX_SCHEMA_VERSION,
    REBUILD_FINGERPRINT_SCHEMA_VERSION,
    build_hierarchical_runtime_index,
    canonicalize_runtime_records,
    hierarchical_index_path,
    index_summary as hierarchical_index_summary,
    logical_corpus_sha256_for_records,
    write_vector_records_with_offsets,
)
from app.retrieval.tokenizer import tokenizer_name  # noqa: E402
from app.services.review_decision_service import approved_content_hash  # noqa: E402
from app.services.regulation_catalog_service import filter_to_latest_active_versions  # noqa: E402
from app.storage.repository import JsonRepository  # noqa: E402


KORDOC_TABLE_REQUIRED_FILE_TYPES = {"hwp", "hwpx", "pdf", "docx"}
REQUIRED_MCP_SOURCE_METADATA_FIELDS = (
    "institution_name",
    "profile_id",
    "source_system",
    "source_url",
    "regulation_id",
    "regulation_version",
    "regulation_status",
    "effective_from",
)
BUNDLE_DATA_DIR_ARG = "$BundleDataDir"
RUNTIME_REPOSITORY_RESULT_SUFFIXES = ("_chunks.json", "_nodes.json", "_issues.json", "_quality.json")
RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES = {
    ".api_audit.lock",
    ".regulation_hierarchy.sqlite3.reg-rag.lock",
    ".write.lock",
    "api_audit.jsonl",
    "rag_traces.jsonl",
    "rag_feedback.jsonl",
}
BUNDLE_ZIP_EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "cache",
    "caches",
    "dist",
    "node_modules",
    "venv",
}
STALE_BUNDLE_STATUS_REPORT_FILENAMES = (
    "mcp_connection_readiness.json",
    "mcp_transport_smoke.json",
    "mcp_client_config_smoke.json",
    "mcp_chatgpt_remote_smoke.json",
    "codex_app_server_mcp_status.json",
    "claude_desktop_installed_mcp_config_smoke.json",
)
UTF8_BOM = b"\xef\xbb\xbf"
SAFE_MCP_SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CLAUDE_CODE_RESERVED_MCP_SERVER_NAMES = frozenset(
    {"workspace", "claude-in-chrome", "computer-use", "claude-preview", "claude-browser"}
)
ACTIVE_LOCAL_INSTALLATION_STATES = {
    "preflight_direct",
    "preflight_claude_code",
    "preflight_claude_desktop",
    "installing",
}
BUNDLE_GENERATION_TRANSITIONAL_STATES = {
    "setup_refresh_in_progress",
    "runtime_refresh_in_progress",
}
RUNTIME_PYTHON_MARKER_FILENAME = "runtime_python.json"
CHATGPT_DATA_TOOL_NAMES = (
    "list_regulations",
    "get_regulation_toc",
    "get_regulation_article",
    "get_regulation_references",
    "list_regulation_reference_cycles",
    "search",
    "fetch",
)
CHATGPT_MCP_HELP_URL = (
    "https://help.openai.com/en/articles/"
    "12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta"
)
CHATGPT_SECURE_MCP_TUNNEL_URL = (
    "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
)
CHATGPT_LOCAL_MCP_UNSUPPORTED_REASON = (
    "ChatGPT does not directly connect to a local MCP server. Use a reachable "
    "remote HTTPS MCP app in ChatGPT web, or use OpenAI Secure MCP Tunnel for "
    "a private, on-premises, or developer-machine server."
)
PORTABLE_HANDOFF_MINIMUM_PYTHON_VERSION = "3.11"
RUNTIME_PYTHON_MARKER_SCHEMA_VERSION = 2
RUNTIME_IDENTITY_SCOPE = "mcp-command-modules-v1"
RUNTIME_DATA_REUSE_SCHEMA_VERSION = "mcp-runtime-data-reuse-v1"
OMISSION_DISPOSITION_SNAPSHOT_FILENAME = "omission_disposition_snapshot.json"
OMISSION_DISPOSITION_SNAPSHOT_SCHEMA_VERSION = (
    "mcp-runtime-omission-disposition-snapshot-v1"
)
OMISSION_DISPOSITION_ENTRY_FIELDS = frozenset(
    {
        "tenant_id",
        "document_id",
        "chunk_id",
        "content_hash",
        "latest_decision_id",
        "latest_decision_status",
        "latest_decision_at",
        "disposition",
        "exported",
        "requested",
    }
)
RUNTIME_APPROVAL_DECISION_FIELDS = frozenset(
    {
        "approval_id",
        "tenant_id",
        "document_id",
        "approved_at",
        "chunk_ids",
        "approved_content_hashes",
    }
)
RUNTIME_APPROVAL_DECISION_REQUIRED_FIELDS = RUNTIME_APPROVAL_DECISION_FIELDS
RUNTIME_DATA_SWAP_SCHEMA_VERSION = "mcp-runtime-data-swap-v1"
RUNTIME_DATA_SWAP_MARKER_FILENAME = ".data-swap-transaction.json"
RUNTIME_DATA_STAGE_NAME = re.compile(r"\.data-stage-[a-f0-9]{32}")
RUNTIME_DATA_BACKUP_NAME = re.compile(r"\.data-backup-[a-f0-9]{32}")
MCP_MATERIALIZATION_LOCK_SUFFIX = ".mcp-materialization.lock"
RUNTIME_IDENTITY_MODULES = (
    "scripts.run_regulation_mcp",
    "scripts.check_mcp_connection_readiness",
    "scripts.run_mcp_smoke",
    "scripts.run_mcp_transport_smoke",
    "scripts.run_mcp_client_config_smoke",
    "scripts.check_codex_app_server_mcp",
    "scripts.check_chatgpt_desktop_recognition",
    "scripts.inspect_claude_desktop_connection",
    "scripts.refresh_mcp_client_connection",
    "scripts.mcp_client_status",
    "scripts.audit_mcp_index_visibility",
)


def _is_source_project_root(path: str | Path | None) -> bool:
    if not path:
        return False
    root = Path(path).expanduser()
    return (
        root.is_dir()
        and (root / "pyproject.toml").is_file()
        and (root / "scripts" / "run_regulation_mcp.py").is_file()
    )


def _runtime_marker_python(marker_path: Path) -> Path | None:
    """Return the marker Python only when the complete v2 identity is valid."""

    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        if not isinstance(marker, dict):
            return None
        module_sha256 = marker.get("module_sha256")
        if (
            marker.get("schema_version") != RUNTIME_PYTHON_MARKER_SCHEMA_VERSION
            or marker.get("minimum_python") != "3.11"
            or marker.get("package_import") != "scripts.run_regulation_mcp"
            or marker.get("identity_scope") != RUNTIME_IDENTITY_SCOPE
            or marker.get("hash_algorithm") != "sha256"
            or not isinstance(module_sha256, dict)
            or set(module_sha256) != set(RUNTIME_IDENTITY_MODULES)
            or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", str(module_sha256.get(name) or ""))
                for name in RUNTIME_IDENTITY_MODULES
            )
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(marker.get("build_identity_sha256") or ""),
            )
        ):
            return None
        datetime.fromisoformat(str(marker.get("written_at") or "").replace("Z", "+00:00"))
        candidate = Path(str(marker.get("python_executable") or "")).expanduser()
        if not candidate.is_absolute() or not candidate.is_file():
            return None
        if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", candidate.stem, re.IGNORECASE):
            return None

        names_json = json.dumps(list(RUNTIME_IDENTITY_MODULES), separators=(",", ":"))
        expected_json = json.dumps(module_sha256, separators=(",", ":"))
        verifier = subprocess.run(
            [
                str(candidate.resolve()),
                "-c",
                "import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))",
                _runtime_identity_verifier_base64(),
                base64.b64encode(names_json.encode("utf-8")).decode("ascii"),
                base64.b64encode(expected_json.encode("utf-8")).decode("ascii"),
                str(marker["build_identity_sha256"]),
            ],
            cwd=str(marker_path.parent.resolve()),
            env={
                key: value
                for key, value in os.environ.items()
                if key.upper() != "PYTHONPATH"
            }
            | {"PYTHONSAFEPATH": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        return candidate.resolve() if verifier.returncode == 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def _python_imports_source_project(python_path: Path, project_root: Path) -> bool:
    """Probe the exact source import with an explicit, cwd-independent PYTHONPATH."""

    try:
        completed = subprocess.run(
            [
                str(python_path),
                "-c",
                (
                    "import sys; "
                    "sys.version_info >= (3, 11) or sys.exit(41); "
                    "import scripts.run_regulation_mcp"
                ),
            ],
            cwd=str(project_root),
            env={
                **os.environ,
                "PYTHONPATH": str(project_root.resolve()),
                "PYTHONSAFEPATH": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _resolve_claude_source_runtime(
    *,
    output_dir: Path,
    preferred_python: str | Path | None,
    preferred_project_root: str | Path | None,
) -> tuple[Path, Path] | None:
    """Resolve a Claude source runtime in the documented deterministic order."""

    project_root = (
        Path(preferred_project_root).expanduser().resolve()
        if _is_source_project_root(preferred_project_root)
        else PROJECT_ROOT.resolve()
        if _is_source_project_root(PROJECT_ROOT)
        else None
    )
    if project_root is None:
        return None

    candidates: list[Path | None] = [
        _runtime_marker_python(output_dir / RUNTIME_PYTHON_MARKER_FILENAME),
        project_root / ".venv" / "Scripts" / "python.exe",
        Path(os.environ["REG_RAG_PYTHON"]).expanduser()
        if os.environ.get("REG_RAG_PYTHON", "").strip()
        else None,
        Path(preferred_python).expanduser() if str(preferred_python or "").strip() else None,
    ]
    checked: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in checked:
            continue
        checked.add(key)
        if not resolved.is_absolute() or not resolved.is_file():
            continue
        if _python_imports_source_project(resolved, project_root):
            return resolved, project_root
    return None


def _with_direct_claude_source_runtime(
    config: dict[str, Any],
    *,
    server_name: str,
    python_path: Path,
    project_root: Path,
) -> dict[str, Any]:
    """Replace one Claude stdio entry with a cwd-independent source invocation."""

    normalized = _local_stdio_config_for_server(config, server_name=server_name)
    source = normalized["mcpServers"][server_name]
    server_args = _stdio_server_args_from_client_entry(source)
    if server_args is None:
        raise ValueError(f"Claude Desktop server {server_name} is not a local stdio entry.")
    entry: dict[str, Any] = {
        "command": str(python_path.resolve()),
        "args": ["-m", "scripts.run_regulation_mcp", *server_args],
        "env": {
            **(dict(source.get("env") or {}) if isinstance(source.get("env"), dict) else {}),
            "PYTHONPATH": str(project_root.resolve()),
            "PYTHONSAFEPATH": "1",
        },
    }
    return {"mcpServers": {server_name: entry}}


def _runtime_identity_builder_base64() -> str:
    code = """\
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

names = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
if not isinstance(names, list) or len(names) != len(set(names)):
    raise SystemExit(42)
module_sha256 = {}
for name in names:
    spec = importlib.util.find_spec(name)
    origin = Path(spec.origin) if spec and spec.origin else None
    if origin is None or not origin.is_file():
        raise SystemExit(43)
    module_sha256[name] = "sha256:" + hashlib.sha256(origin.read_bytes()).hexdigest()
canonical = json.dumps(module_sha256, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
payload = {
    "module_sha256": module_sha256,
    "build_identity_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
"""
    return base64.b64encode(code.encode("utf-8")).decode("ascii")


def _runtime_identity_verifier_base64() -> str:
    code = """\
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

names = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
expected = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
expected_build = sys.argv[3]
if not isinstance(names, list) or len(names) != len(set(names)) or set(expected) != set(names):
    raise SystemExit(42)
actual = {}
for name in names:
    spec = importlib.util.find_spec(name)
    origin = Path(spec.origin) if spec and spec.origin else None
    if origin is None or not origin.is_file():
        raise SystemExit(43)
    actual[name] = "sha256:" + hashlib.sha256(origin.read_bytes()).hexdigest()
canonical = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
actual_build = "sha256:" + hashlib.sha256(canonical).hexdigest()
raise SystemExit(0 if actual == expected and actual_build == expected_build else 44)
"""
    return base64.b64encode(code.encode("utf-8")).decode("ascii")


def _python_runtime_probe_base64() -> str:
    code = """\
import importlib
import importlib.util
import sys
import traceback

module_name = "scripts.run_regulation_mcp"
if sys.version_info < (3, 11):
    print(
        f"Python version is below 3.11: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        file=sys.stderr,
    )
    raise SystemExit(41)
try:
    spec = importlib.util.find_spec(module_name)
except Exception:
    traceback.print_exc(file=sys.stderr)
    raise SystemExit(42)
if spec is None:
    print(f"MCP module import failed: {module_name} was not found", file=sys.stderr)
    raise SystemExit(42)
try:
    importlib.import_module(module_name)
except BaseException:
    print(f"Required dependency import failed while importing {module_name}:", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    raise SystemExit(43)
"""
    return base64.b64encode(code.encode("utf-8")).decode("ascii")


def _powershell_file_sha256_function_lines() -> list[str]:
    """Return a module-independent SHA-256 helper for generated installers."""

    return [
        "function Get-McpFileSha256([string]$LiteralPath) {",
        "  $Stream = [System.IO.File]::Open(",
        "    $LiteralPath,",
        "    [System.IO.FileMode]::Open,",
        "    [System.IO.FileAccess]::Read,",
        "    [System.IO.FileShare]::Read",
        "  )",
        "  try {",
        "    $Hasher = [System.Security.Cryptography.SHA256]::Create()",
        "    try {",
        '      return -join ($Hasher.ComputeHash($Stream) | ForEach-Object { $_.ToString("x2") })',
        "    } finally {",
        "      $Hasher.Dispose()",
        "    }",
        "  } finally {",
        "    $Stream.Dispose()",
        "  }",
        "}",
    ]


def _powershell_runtime_identity_validator_lines() -> list[str]:
    modules_literal = _powershell_array_literal(RUNTIME_IDENTITY_MODULES)
    verifier_literal = _powershell_single_quoted_json(_runtime_identity_verifier_base64())
    return [
        "function Test-RuntimeMarkerShape([object]$Marker, [string[]]$RuntimeModules) {",
        '  if ([int]$Marker.schema_version -ne 2 -or [string]$Marker.minimum_python -ne "3.11" -or [string]$Marker.package_import -ne "scripts.run_regulation_mcp" -or [string]$Marker.identity_scope -ne "mcp-command-modules-v1" -or [string]$Marker.hash_algorithm -ne "sha256") { return $false }',
        '  if (-not $Marker.module_sha256 -or @($Marker.module_sha256.PSObject.Properties).Count -ne $RuntimeModules.Count) { return $false }',
        '  foreach ($ModuleName in $RuntimeModules) {',
        '    $HashProperty = $Marker.module_sha256.PSObject.Properties[$ModuleName]',
        '    if (-not $HashProperty -or [string]$HashProperty.Value -notmatch "^sha256:[0-9a-f]{64}$") { return $false }',
        '  }',
        '  if ([string]$Marker.build_identity_sha256 -notmatch "^sha256:[0-9a-f]{64}$") { return $false }',
        '  return $true',
        '}',
        "function Test-RuntimeMarkerIdentity([string]$PythonPath, [object]$Marker) {",
        f"  $RuntimeModules = {modules_literal}",
        f"  $IdentityVerifierBase64 = {verifier_literal}",
        '  if (-not (Test-RuntimeMarkerShape $Marker $RuntimeModules)) { return $false }',
        '  $BuildIdentity = [string]$Marker.build_identity_sha256',
        '  $RuntimeModulesJson = $RuntimeModules | ConvertTo-Json -Compress',
        '  $ExpectedHashesJson = $Marker.module_sha256 | ConvertTo-Json -Depth 10 -Compress',
        '  $RuntimeModulesBase64 = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($RuntimeModulesJson))',
        '  $ExpectedHashesBase64 = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($ExpectedHashesJson))',
        '  $PreviousErrorActionPreference = $ErrorActionPreference',
        '  $HadPythonPath = Test-Path Env:PYTHONPATH',
        '  $PreviousPythonPath = $env:PYTHONPATH',
        '  $HadSafePath = Test-Path Env:PYTHONSAFEPATH',
        '  $PreviousSafePath = $env:PYTHONSAFEPATH',
        '  try {',
        '    $ErrorActionPreference = "Continue"',
        '    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue',
        '    $env:PYTHONSAFEPATH = "1"',
        '    # This verifier runs before the real stdio server. Never let it inherit',
        '    # the MCP stdin/stdout handles, or a slow/site-customized Python can',
        '    # consume the initialize frame intended for the server process.',
        '    $VerifierStartInfo = New-Object System.Diagnostics.ProcessStartInfo',
        '    $VerifierStartInfo.FileName = $PythonPath',
        '    $VerifierStartInfo.Arguments = "-c __import__(\'builtins\').exec(__import__(\'base64\').b64decode(__import__(\'sys\').argv.pop(1))) $IdentityVerifierBase64 $RuntimeModulesBase64 $ExpectedHashesBase64 $BuildIdentity"',
        '    $VerifierStartInfo.UseShellExecute = $false',
        '    $VerifierStartInfo.CreateNoWindow = $true',
        '    $VerifierStartInfo.RedirectStandardInput = $true',
        '    $VerifierStartInfo.RedirectStandardOutput = $true',
        '    $VerifierStartInfo.RedirectStandardError = $true',
        '    $VerifierProcess = New-Object System.Diagnostics.Process',
        '    $VerifierProcess.StartInfo = $VerifierStartInfo',
        '    [void]$VerifierProcess.Start()',
        '    $VerifierProcess.StandardInput.Close()',
        '    $null = $VerifierProcess.StandardOutput.ReadToEnd()',
        '    $null = $VerifierProcess.StandardError.ReadToEnd()',
        '    $VerifierProcess.WaitForExit()',
        '    $VerifierExitCode = $VerifierProcess.ExitCode',
        '    $VerifierProcess.Dispose()',
        '    return $VerifierExitCode -eq 0',
        '  } catch {',
        '    return $false',
        '  } finally {',
        '    $ErrorActionPreference = $PreviousErrorActionPreference',
        '    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
        '    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }',
        '  }',
        '}',
    ]


def _write_utf8_no_bom(path: Path, text: str) -> None:
    """Write machine-readable text as strict UTF-8 without a byte-order mark."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8")
    if encoded.startswith(UTF8_BOM):
        raise ValueError(f"Refusing to write a UTF-8 BOM to machine-readable file: {path}")
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_bytes(encoded)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json_utf8_no_bom(path: Path, payload: Any) -> None:
    _write_utf8_no_bom(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _replace_file_bytes_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.restore")
    try:
        temporary_path.write_bytes(payload)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _windows_named_mutex(name: str, *, timeout_ms: int = 10_000) -> Any:
    if os.name != "nt":
        yield
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError("Could not create the bundle status mutex.")
    acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(handle, int(timeout_ms))
        if wait_result not in (0x00000000, 0x00000080):
            raise TimeoutError("Timed out waiting to update bundle_status.json.")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


@contextmanager
def _bundle_status_write_guard() -> Any:
    """Share the Windows status mutex used by the generated PowerShell installer."""

    with _windows_named_mutex("Local\\PRMCPBuilder-BundleStatus"):
        yield


def _bundle_materialization_lock_path(output_dir: str | Path) -> Path:
    """Return a stable sibling lock path without allowing a root-wide lock."""

    resolved_output = Path(output_dir).expanduser().resolve()
    if resolved_output == Path(resolved_output.anchor):
        raise ValueError("MCP bundle materialization cannot lock a filesystem root.")
    lock_path = resolved_output.parent / f".{resolved_output.name}{MCP_MATERIALIZATION_LOCK_SUFFIX}"
    if lock_path.parent != resolved_output.parent or lock_path.name in {"", ".", ".."}:
        raise ValueError("MCP bundle materialization lock escaped the output directory parent.")
    return lock_path


@contextmanager
def _bundle_materialization_file_lock(
    output_dir: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> Any:
    """Serialize bundle mutations on Windows and POSIX using an OS file lock."""

    lock_path = _bundle_materialization_lock_path(output_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise RuntimeError(f"Refusing a symbolic-link MCP materialization lock: {lock_path}")
    open_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        open_flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    file_descriptor = os.open(lock_path, open_flags, 0o600)
    handle = os.fdopen(file_descriptor, "r+b", buffering=0)
    acquired = False
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for MCP bundle materialization lock: {lock_path}"
                        ) from exc
                    time.sleep(0.05)
        elif os.name == "posix":
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for MCP bundle materialization lock: {lock_path}"
                        ) from exc
                    time.sleep(0.05)
        else:
            raise RuntimeError(f"Unsupported platform for MCP bundle materialization lock: {os.name}")
        yield lock_path
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _guard_local_mcp_materialization(function: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(function)

    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(*args, **kwargs)
        output_dir = bound.arguments.get("out_dir")
        with _windows_named_mutex("Local\\PRMCPBuilder-LocalMcpInstallation", timeout_ms=30_000):
            if output_dir is None:
                return function(*args, **kwargs)
            with _bundle_materialization_file_lock(output_dir):
                return function(*args, **kwargs)

    return guarded


def _assert_no_active_bundle_installation(output_dir: Path) -> None:
    status_path = output_dir / SETUP_BUNDLE_FILES["bundle_status"]
    if not status_path.is_file():
        return
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Existing bundle_status.json is unreadable; refusing a concurrent bundle rewrite.") from exc
    if isinstance(payload, dict) and str(payload.get("installation_state") or "") in ACTIVE_LOCAL_INSTALLATION_STATES:
        raise RuntimeError("MCP setup files cannot be regenerated during an active connection attempt.")


def _validate_mcp_server_name(server_name: str) -> str:
    normalized = str(server_name or "").strip()
    if not SAFE_MCP_SERVER_NAME.fullmatch(normalized):
        raise ValueError(
            "server_name must be 1-64 lowercase ASCII letters, numbers, hyphens, or underscores."
        )
    if normalized in CLAUDE_CODE_RESERVED_MCP_SERVER_NAMES:
        raise ValueError(
            "server_name is reserved by Claude Code; choose a distinct MCP server name."
        )
    return normalized


def build_mcp_client_config(
    *,
    server_name: str = "regulation_mcp",
    data_dir: str = "data",
    tenant_id: str = "default",
    profile_id: str | None = None,
    tenant_storage_isolation: bool = False,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    actor: str | None = None,
    role: str | None = None,
    department_ids: list[str] | None = None,
    client_profile: str = "generic",
    public_url: str | None = None,
    remote_auth_token_env: str | None = "MCP_AUTH_TOKEN",
    chatgpt_oauth_ready: bool = False,
    min_visible_records: int = 1,
) -> dict[str, Any]:
    server_name = _validate_mcp_server_name(server_name)
    if remote_auth_token_env is not None and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", remote_auth_token_env
    ):
        raise ValueError(
            "remote_auth_token_env must be a valid environment variable name."
        )
    normalized_profile = client_profile.strip().lower()
    valid_profiles = {
        "generic",
        "claude-desktop",
        "claude-code",
        "chatgpt",
        "chatgpt-desktop-local",
        "chatgpt-remote",
        "claude-remote",
        "claude-api",
        "bundle",
    }
    if normalized_profile not in valid_profiles:
        raise ValueError(
            "client_profile must be generic, claude-desktop, claude-code, chatgpt-desktop-local, "
            "chatgpt-remote, claude-remote, legacy chatgpt/claude-api aliases, or bundle."
        )
    if normalized_profile == "bundle":
        claude_desktop = build_mcp_client_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            tenant_storage_isolation=tenant_storage_isolation,
            transport="stdio",
            host=host,
            port=port,
            actor=actor,
            role=role,
            department_ids=department_ids,
            client_profile="claude-desktop",
            remote_auth_token_env=remote_auth_token_env,
        )
        claude_code = build_mcp_client_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            tenant_storage_isolation=tenant_storage_isolation,
            transport="stdio",
            host=host,
            port=port,
            actor=actor,
            role=role,
            department_ids=department_ids,
            client_profile="claude-code",
            remote_auth_token_env=remote_auth_token_env,
        )
        claude_desktop = _with_bundle_stdio_fast_start(claude_desktop)
        claude_code = _with_bundle_stdio_fast_start(claude_code)
        chatgpt_desktop_local = _unsupported_chatgpt_desktop_local_payload(
            server_name=server_name
        )
        chatgpt_remote = build_mcp_client_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            tenant_storage_isolation=tenant_storage_isolation,
            transport="streamable-http",
            host=host,
            port=port,
            actor=actor,
            role=role,
            department_ids=department_ids,
            client_profile="chatgpt-remote",
            public_url=public_url,
            remote_auth_token_env=remote_auth_token_env,
            chatgpt_oauth_ready=chatgpt_oauth_ready,
            min_visible_records=min_visible_records,
        )
        claude_remote = build_mcp_client_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            tenant_storage_isolation=tenant_storage_isolation,
            transport="streamable-http",
            host=host,
            port=port,
            actor=actor,
            role=role,
            department_ids=department_ids,
            client_profile="claude-remote",
            public_url=public_url,
            remote_auth_token_env=remote_auth_token_env,
            min_visible_records=min_visible_records,
        )
        return {
            "quickstart": _bundle_quickstart(
                server_name=server_name,
                data_dir=data_dir,
                tenant_id=tenant_id,
                profile_id=profile_id,
                tenant_storage_isolation=tenant_storage_isolation,
                host=host,
                port=port,
                actor=actor,
                role=role,
                department_ids=department_ids,
                claude_code=claude_code,
                chatgpt_desktop_local=chatgpt_desktop_local,
                chatgpt_remote=chatgpt_remote,
                claude_remote=claude_remote,
                remote_auth_token_env=remote_auth_token_env,
                min_visible_records=min_visible_records,
            ),
            "claude_desktop": claude_desktop,
            "claude_code": claude_code,
            "chatgpt_desktop_local": chatgpt_desktop_local,
            "chatgpt_remote": chatgpt_remote,
            # Backward-compatible alias. New code and generated guidance use chatgpt_remote.
            "chatgpt": chatgpt_remote,
            "claude_remote": claude_remote,
        }
    normalized_transport = transport.strip().lower()
    if normalized_profile in {"chatgpt", "chatgpt-remote"}:
        return _chatgpt_connector_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            host=host,
            port=port,
            actor=actor,
            role=role,
            department_ids=department_ids,
            tenant_storage_isolation=tenant_storage_isolation,
            public_url=public_url,
            remote_auth_token_env=remote_auth_token_env,
            chatgpt_oauth_ready=chatgpt_oauth_ready,
            min_visible_records=min_visible_records,
        )
    if normalized_profile in {"claude-remote", "claude-api"}:
        return _claude_remote_connector_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            host=host,
            port=port,
            actor=actor,
            role=role,
            department_ids=department_ids,
            tenant_storage_isolation=tenant_storage_isolation,
            public_url=public_url,
            remote_auth_token_env=remote_auth_token_env,
        )
    if normalized_profile == "chatgpt-desktop-local":
        return _unsupported_chatgpt_desktop_local_payload(server_name=server_name)
    if normalized_profile == "claude-code":
        if normalized_transport == "stdio":
            return _stdio_server_config(
                data_dir=data_dir,
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=actor,
                role=role,
                department_ids=department_ids,
                tenant_storage_isolation=tenant_storage_isolation,
                include_type=True,
            )
        if normalized_transport == "streamable-http":
            return _http_server_config(host=host, port=port, public_url=public_url, include_transport_alias=True)
        raise ValueError("transport must be stdio or streamable-http.")
    if normalized_transport == "stdio":
        args = _server_args(
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            transport="stdio",
            actor=actor,
            role=role,
            department_ids=department_ids,
            tenant_storage_isolation=tenant_storage_isolation,
            tool_profile=(
                "chatgpt-data"
                if normalized_profile == "chatgpt-desktop-local"
                else "full"
            ),
        )
        return {
            "mcpServers": {
                server_name: {
                    **(
                        {"type": "stdio"}
                        if normalized_profile in {"claude-desktop", "chatgpt-desktop-local"}
                        else {}
                    ),
                    "command": "reg-rag-mcp-server",
                    "args": args,
                }
            }
        }
    if normalized_transport == "streamable-http":
        client_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        return {
            "mcpServers": {
                server_name: {
                    "url": f"http://{client_host}:{int(port)}/mcp",
                    "transport": "streamable-http",
                    **({"type": "http"} if normalized_profile == "claude-desktop" else {}),
                    "serverCommand": {
                        "command": "reg-rag-mcp-server",
                        "args": (
                            _server_args(
                                data_dir=data_dir,
                                tenant_id=tenant_id,
                                profile_id=profile_id,
                                transport="streamable-http",
                                actor=actor,
                                role=role,
                                department_ids=department_ids,
                                tenant_storage_isolation=tenant_storage_isolation,
                            )
                            + [
                                "--host",
                                host,
                                "--port",
                                str(int(port)),
                            ]
                            + _http_auth_args(remote_auth_token_env)
                            + _auth_issuer_args(public_url)
                        ),
                    },
                }
            }
        }
    raise ValueError("transport must be stdio or streamable-http.")


def _write_mcp_setup_bundle_untransactional(
    config: dict[str, Any],
    out_dir: str | Path,
    *,
    server_name: str,
    preferred_python: str | Path | None = None,
    preferred_project_root: str | Path | None = None,
    claude_source_runtime: tuple[Path, Path] | None = None,
) -> dict[str, str]:
    """Write copy/paste-ready MCP setup artifacts for common clients."""
    server_name = _validate_mcp_server_name(server_name)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_active_bundle_installation(output_dir)
    _clear_stale_bundle_status_reports(output_dir)
    _remove_legacy_connection_artifacts(output_dir)
    source_quickstart = config.get("quickstart") if isinstance(config, dict) else None
    if not isinstance(source_quickstart, dict):
        source_quickstart = {}
    json_config = _with_explicit_bundle_data_dir(config, output_dir / "data")
    stdio_launcher_path = output_dir / SETUP_BUNDLE_FILES["stdio_launcher"]
    stdio_launcher_default_args = _bundle_stdio_launcher_default_args(
        json_config,
        server_name=server_name,
        bundle_data_dir=output_dir / "data",
    )
    json_config = _with_bundle_stdio_launcher(json_config, launcher_path=stdio_launcher_path, server_name=server_name)
    # Preserve the supported Codex launcher form before the optional Claude
    # Desktop source-runtime rewrite below.  Claude may deliberately use a
    # direct Python module entry, while Codex must keep the portable bundle
    # launcher contract.
    codex_stdio_source_config: dict[str, Any] = {}
    if isinstance(json_config.get("claude_desktop"), dict):
        codex_stdio_source_config = _local_stdio_config_for_server(
            json_config["claude_desktop"],
            server_name=server_name,
        )
    if claude_source_runtime is not None and isinstance(json_config.get("claude_desktop"), dict):
        direct_python, direct_project_root = claude_source_runtime
        json_config["claude_desktop"] = _with_direct_claude_source_runtime(
            json_config["claude_desktop"],
            server_name=server_name,
            python_path=direct_python,
            project_root=direct_project_root,
        )
    quickstart = json_config.get("quickstart") if isinstance(json_config, dict) else None
    if not isinstance(quickstart, dict):
        quickstart = {}
    # ``copy_paste`` values are executable PowerShell, not structured command
    # metadata.  Keep their original bundle-relative $BundleDir/$BundleDataDir
    # expressions instead of freezing the generation machine's output path.
    source_copy_paste = source_quickstart.get("copy_paste")
    if isinstance(source_copy_paste, dict):
        quickstart["copy_paste"] = copy.deepcopy(source_copy_paste)
    files: dict[str, str] = {}

    def write_json(key: str, payload: Any) -> None:
        path = output_dir / SETUP_BUNDLE_FILES[key]
        _write_json_utf8_no_bom(path, payload)
        files[key] = str(path)

    def write_text(key: str, text: str) -> None:
        if key in {
            "claude_code_stdio",
            "claude_code_http",
            "run_stdio",
            "validate",
            "client_config_smoke",
            "remote_validate",
            "doctor",
        }:
            text = _with_preferred_mcp_command_functions(
                text,
                preferred_python=preferred_python,
                preferred_project_root=preferred_project_root,
            )
        path = output_dir / SETUP_BUNDLE_FILES[key]
        # Windows PowerShell 5.1 treats a BOM-less script as the active ANSI
        # code page. A UTF-8 BOM is therefore required when generated paths or
        # server names contain Korean characters.
        encoding = "utf-8-sig" if path.suffix.lower() == ".ps1" else "utf-8"
        rendered = text.rstrip() + "\n"
        if encoding == "utf-8-sig":
            path.write_text(rendered, encoding=encoding)
        else:
            _write_utf8_no_bom(path, rendered)
        files[key] = str(path)

    write_json("full_config", json_config)
    claude_desktop_stdio_config: dict[str, Any] = {}
    codex_stdio_config: dict[str, Any] = {}
    chatgpt_desktop_local_payload: dict[str, Any] = {}
    if "claude_desktop" in json_config:
        claude_desktop_stdio_config = _local_stdio_config_for_server(
            json_config["claude_desktop"],
            server_name=server_name,
        )
        write_json("claude_desktop", claude_desktop_stdio_config)
        # Codex has its own supported local stdio path.  Do not source it from
        # the retained ChatGPT-local compatibility artifact, which is
        # deliberately warning-only and non-runnable.
        codex_stdio_config = codex_stdio_source_config
        codex_snippet = _codex_config_snippet(codex_stdio_config, server_name=server_name)
        if codex_snippet:
            write_text("codex_config", codex_snippet)
        chatgpt_desktop_local_payload = _chatgpt_desktop_local_config(
            codex_stdio_config,
            server_name=server_name,
            bundle_dir=output_dir,
        )
        write_json("chatgpt_desktop_local", chatgpt_desktop_local_payload)
    if "chatgpt_remote" in json_config or "chatgpt" in json_config:
        write_json("chatgpt", json_config.get("chatgpt_remote") or json_config["chatgpt"])
    if "claude_remote" in json_config:
        write_json("claude_remote", json_config["claude_remote"])
    packaged_executable = os.getenv("REG_RAG_PACKAGED_EXE", "").strip()
    write_text(
        "stdio_launcher",
        _powershell_stdio_launcher_script(
            stdio_launcher_default_args,
            packaged_executable=packaged_executable or None,
            preferred_python=preferred_python,
            preferred_project_root=preferred_project_root,
        ),
    )

    copy_paste = source_quickstart.get("copy_paste") if isinstance(source_quickstart.get("copy_paste"), dict) else {}
    if copy_paste.get("claude_code_stdio_ps"):
        write_text("claude_code_stdio", copy_paste["claude_code_stdio_ps"])
    if copy_paste.get("claude_code_http_ps"):
        write_text("claude_code_http", copy_paste["claude_code_http_ps"])
    if copy_paste.get("run_local_stdio_server_ps"):
        write_text("run_stdio", copy_paste["run_local_stdio_server_ps"])
    validate_ps = copy_paste.get("validate_runtime_transport_ps") or copy_paste.get("validate_synthetic_chain_ps")
    if validate_ps:
        write_text("validate", validate_ps)
    if copy_paste.get("validate_client_config_smoke_ps"):
        write_text("client_config_smoke", copy_paste["validate_client_config_smoke_ps"])
    chatgpt_remote_config = json_config.get("chatgpt_remote") or json_config.get("chatgpt") or {}
    write_text(
        "remote_validate",
        _powershell_chatgpt_remote_validation_script(
            server_name=server_name,
            connector_url=chatgpt_remote_config.get("connector_url"),
            token_env=(chatgpt_remote_config.get("server_auth") or {}).get("token_env"),
        ),
    )
    if copy_paste.get("doctor_ps"):
        write_text("doctor", copy_paste["doctor_ps"])
    if copy_paste.get("connect_wizard_ps"):
        connect_wizard = _with_product_embedded_mcp_configs(
            copy_paste["connect_wizard_ps"],
            claude_desktop_config=claude_desktop_stdio_config,
            codex_config=codex_stdio_config,
        )
        write_text(
            "connect",
            _with_connect_wizard_preferred_runtime(
                connect_wizard,
                preferred_python=preferred_python,
                preferred_project_root=preferred_project_root,
            ),
        )
    write_text("install", _install_local_package_script())
    write_text("usage_guide", _mcp_first_use_guide(server_name))

    manifest = {
        "server_name": server_name,
        "installation_attempt_id": None,
        "installation_state": "not_installed",
        "connection_state": "not_configured",
        "profile": "bundle",
        "mcp_protocol": "MCP",
        "mcp_server": {
            "role": "protocol implementation and tool host",
            "available_transports": ["stdio", "streamable-http"],
        },
        "files": {
            **{key: _bundle_relative_path(output_dir, path) for key, path in files.items()},
            "manifest": SETUP_BUNDLE_FILES["manifest"],
            "bundle_status": SETUP_BUNDLE_FILES["bundle_status"],
            "readme": SETUP_BUNDLE_FILES["readme"],
            "readme_ko": SETUP_BUNDLE_FILES["readme_ko"],
        },
        "ready": {
            "chatgpt_remote": bool(
                (json_config.get("chatgpt_remote") or json_config.get("chatgpt") or {}).get("ready")
            ),
            "claude_remote": bool((json_config.get("claude_remote") or {}).get("ready")),
        },
        "portable_handoff_runtime": _portable_handoff_runtime_requirements(
            wheel_included=None
        ),
        "connections": _setup_bundle_connections(json_config),
    }
    write_json("manifest", manifest)
    write_json("bundle_status", _bundle_status_payload(output_dir, config=json_config, setup_manifest=manifest))
    write_text(
        "readme",
        _setup_bundle_readme(config=json_config, files=manifest["files"], server_name=server_name),
    )
    write_text(
        "readme_ko",
        _setup_bundle_readme_ko(config=json_config, files=manifest["files"], server_name=server_name),
    )
    return files


@_guard_local_mcp_materialization
def write_mcp_setup_bundle(
    config: dict[str, Any],
    out_dir: str | Path,
    *,
    server_name: str,
    preferred_python: str | Path | None = None,
    preferred_project_root: str | Path | None = None,
) -> dict[str, str]:
    """Write setup artifacts as a rollback-safe bundle transaction."""

    server_name = _validate_mcp_server_name(server_name)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_active_bundle_installation(output_dir)
    claude_source_runtime = _resolve_claude_source_runtime(
        output_dir=output_dir,
        preferred_python=preferred_python,
        preferred_project_root=preferred_project_root,
    )
    backup_dir = output_dir.parent / f".{output_dir.name}.setup-backup-{uuid4().hex}"
    retired_artifact_root = output_dir / "chatgpt-desktop-local-plugin"
    targets = [
        *(output_dir / name for name in sorted(ALL_SETUP_BUNDLE_FILES)),
        retired_artifact_root,
        output_dir / RUNTIME_PYTHON_MARKER_FILENAME,
        *(output_dir / name for name in STALE_BUNDLE_STATUS_REPORT_FILENAMES),
    ]
    unique_targets = list(dict.fromkeys(targets))
    existing_targets: dict[Path, Path] = {}
    mutation_started = False
    preserve_backup_dir = False
    backup_dir.mkdir(parents=True, exist_ok=False)
    try:
        for index, target in enumerate(unique_targets):
            if not target.exists():
                continue
            backup_path = backup_dir / str(index)
            if target.is_dir():
                shutil.copytree(target, backup_path)
            else:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_path)
            existing_targets[target] = backup_path

        mutation_started = True
        status_path = output_dir / SETUP_BUNDLE_FILES["bundle_status"]
        if status_path.is_file():
            try:
                refresh_status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Existing bundle status is unreadable; setup refresh was not started.") from exc
            if not isinstance(refresh_status, dict):
                raise RuntimeError("Existing bundle status is invalid; setup refresh was not started.")
            refresh_status.update(
                {
                    "installation_state": "setup_refresh_in_progress",
                    "connection_state": "pending_setup_refresh",
                    "process_started": False,
                    "mcp_initialized": False,
                    "tools_discovered": False,
                    "installed_config_transport_verified": False,
                    "generated_client_configs_transport_verified": False,
                    "claude_code_transport_verified": False,
                    "claude_code_transport_runtime_fingerprint": None,
                    "claude_code_conversation_verified": False,
                    "direct_stdio_verified": False,
                    "transport_end_to_end_verified": False,
                    "claude_desktop_config_transport_verified": False,
                    "claude_desktop_config_transport_runtime_fingerprint": None,
                    "claude_desktop_loader_observed": False,
                    "claude_desktop_loader_verified": False,
                    "claude_desktop_conversation_verified": False,
                    "fresh_codex_app_server_inventory_verified": False,
                    "desktop_app_server_loader_verified": False,
                    "desktop_tool_scan_verified": False,
                    "conversation_attachment_verified": False,
                    "conversation_attachment_unverified": True,
                    "tool_scan_unverified": True,
                    "end_to_end_verified": False,
                }
            )
            _write_json_utf8_no_bom(status_path, refresh_status)

        # A setup refresh may include a different wheel even when the public
        # package version is unchanged.  Do not keep an old authoritative
        # runtime marker across that boundary; the next -InstallPackage run
        # records the newly installed runtime.  Transaction rollback restores
        # the exact prior marker if generation fails below.
        (output_dir / RUNTIME_PYTHON_MARKER_FILENAME).unlink(missing_ok=True)

        # Remove the retired directory transactionally so regenerating a
        # direct-only bundle also cleans old releases without weakening
        # rollback if a later setup write fails.
        if retired_artifact_root.is_dir():
            shutil.rmtree(retired_artifact_root)
        elif retired_artifact_root.exists():
            retired_artifact_root.unlink()

        return _write_mcp_setup_bundle_untransactional(
            config,
            output_dir,
            server_name=server_name,
            preferred_python=preferred_python,
            preferred_project_root=preferred_project_root,
            claude_source_runtime=claude_source_runtime,
        )
    except BaseException as setup_error:
        if mutation_started:
            rollback_errors: list[str] = []
            for target in reversed(unique_targets):
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                except BaseException as rollback_error:
                    rollback_errors.append(
                        f"remove {target}: {type(rollback_error).__name__}: {rollback_error}"
                    )
            for target, backup_path in existing_targets.items():
                try:
                    if backup_path.is_dir():
                        shutil.copytree(backup_path, target, dirs_exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup_path, target)
                except BaseException as rollback_error:
                    rollback_errors.append(
                        f"restore {target}: {type(rollback_error).__name__}: {rollback_error}"
                    )
            if rollback_errors:
                preserve_backup_dir = True
                rollback_summary = "; ".join(rollback_errors[:8])
                if len(rollback_errors) > 8:
                    rollback_summary += f"; and {len(rollback_errors) - 8} more rollback error(s)"
                raise RuntimeError(
                    "Setup bundle generation failed and rollback was incomplete. "
                    f"Recovery backup retained at '{backup_dir}'. "
                    f"Original error: {type(setup_error).__name__}: {setup_error}. "
                    f"Rollback failures: {rollback_summary}"
                ) from setup_error
        raise
    finally:
        if not preserve_backup_dir:
            shutil.rmtree(backup_dir, ignore_errors=True)


def _clear_stale_bundle_status_reports(output_dir: Path) -> list[str]:
    cleared: list[str] = []
    for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES:
        path = output_dir / filename
        if not path.is_file():
            continue
        path.unlink()
        cleared.append(filename)
    return cleared


def _remove_legacy_connection_artifacts(output_dir: Path) -> list[str]:
    """Remove exact legacy BAT and agent-prompt artifacts from regenerated bundles."""
    removed: list[str] = []
    for filename in sorted(LEGACY_CONNECTION_ARTIFACT_FILENAMES):
        path = output_dir / filename
        if not path.is_file():
            continue
        path.unlink()
        removed.append(filename)
    legacy_plugin_dir = output_dir / "chatgpt-desktop-local-plugin"
    if legacy_plugin_dir.is_dir():
        shutil.rmtree(legacy_plugin_dir)
        removed.append(legacy_plugin_dir.name)
    return removed


def _bundle_status_payload(
    output_dir: Path,
    *,
    config: dict[str, Any] | None = None,
    setup_manifest: dict[str, Any] | None = None,
    runtime_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_data_dir = output_dir / "data"
    if setup_manifest is None:
        setup_manifest = _read_setup_bundle_manifest(output_dir)
    manifest = runtime_manifest if isinstance(runtime_manifest, dict) else _read_runtime_manifest(runtime_data_dir)
    runtime_ready = bool(manifest)
    runtime_fingerprint = (
        hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if runtime_ready
        else None
    )
    payload: dict[str, Any] = {
        "report_type": "mcp_bundle_status",
        "schema_version": "mcp-bundle-status-v4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "installation_attempt_id": None,
        "installation_state": "not_installed",
        "connection_state": "not_configured",
        "bundle_dir": str(output_dir),
        "runtime_data_dir": str(runtime_data_dir),
        "runtime_data_ready": runtime_ready,
        "runtime_fingerprint": runtime_fingerprint,
        "launcher_ready": (output_dir / SETUP_BUNDLE_FILES["stdio_launcher"]).is_file(),
        "launcher_ready_note": (
            "launcher_ready only confirms that the PowerShell file exists; it does not prove that a fresh "
            "handoff target has a compatible packaged executable or Python 3.11+."
        ),
        "portable_handoff_runtime": _portable_handoff_runtime_requirements(
            wheel_included=None
        ),
        "process_started": False,
        "mcp_initialized": False,
        "tools_discovered": False,
        "direct_config_registered": False,
        "direct_config_loader_verified": False,
        "loader_verification_state": "not_checked",
        "loader_verification_reason": "not_checked",
        "direct_config_rollback_performed": False,
        "direct_config_path": None,
        "installed_config_fingerprint": None,
        "installed_config_transport_verified": False,
        "installed_config_transport_runtime_fingerprint": None,
        "generated_client_configs_transport_verified": False,
        "claude_code_registered": False,
        "claude_code_config_fingerprint": None,
        "claude_code_loader_verified": False,
        "claude_code_transport_verified": False,
        "claude_code_transport_runtime_fingerprint": None,
        "claude_code_registration_updated_at": None,
        "claude_code_conversation_verified": False,
        "claude_desktop_config_registered": False,
        "claude_desktop_config_path": None,
        "claude_desktop_config_fingerprint": None,
        "claude_desktop_config_transport_verified": False,
        "claude_desktop_config_transport_runtime_fingerprint": None,
        "claude_desktop_registration_updated_at": None,
        "claude_desktop_process_detected": False,
        "claude_desktop_process_started_at": None,
        "claude_desktop_restart_checked_at": None,
        "claude_desktop_restart_required": None,
        "claude_desktop_restart_status": "not_checked",
        "claude_desktop_restarted_after_registration": False,
        "claude_desktop_post_registration_log_session_observed": False,
        "claude_desktop_server_name_observed": False,
        "claude_desktop_loader_observed": False,
        "claude_desktop_loader_verified": False,
        "claude_desktop_conversation_verified": False,
        "desktop_process_detected": False,
        "desktop_process_started_at": None,
        "desktop_mcp_registration_updated_at": None,
        "desktop_restart_checked_at": None,
        "desktop_restart_required": None,
        "desktop_restart_status": "not_checked",
        "desktop_restart_reason_code": "not_checked",
        "desktop_app_server_loader_verified": False,
        "fresh_codex_app_server_inventory_verified": False,
        "fresh_codex_app_server_runtime_fingerprint": None,
        "desktop_app_server_tool_count": 0,
        "desktop_app_server_tool_names": [],
        "desktop_app_server_server_info": None,
        "desktop_app_server_error": None,
        "desktop_recognition_observation_status": "not_checked",
        "desktop_restarted_after_registration": False,
        "desktop_post_registration_log_session_observed": False,
        "desktop_status_scan_request_observed": False,
        "direct_stdio_verified": False,
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
        "conversation_attachment_unverified": True,
        "transport_end_to_end_verified": False,
        "end_to_end_verified": False,
        "remote_endpoint_verified": False,
        "tool_scan_unverified": True,
        "connection_state_notes": {
            "direct_config_registered": (
                "The direct MCP entry was written to the local Codex configuration."
            ),
            "direct_config_loader_verified": (
                "codex mcp get resolved the direct entry with this bundle's exact launcher and data paths."
            ),
            "installed_config_transport_verified": (
                "The exact installed config entry passed initialize, tools/list, and get_index_status over stdio."
            ),
            "claude_code_registered": (
                "Claude Code contains the exact user-scoped server entry for this bundle."
            ),
            "claude_code_loader_verified": (
                "claude mcp get resolved the user-scoped entry with this bundle's exact launcher and data paths."
            ),
            "claude_code_transport_verified": (
                "The current bundle runtime passed the generated initialize, tools/list, search, and fetch smoke."
            ),
            "claude_desktop_config_registered": (
                "The Claude Desktop user configuration contains this bundle's exact server entry and paths."
            ),
            "claude_desktop_config_transport_verified": (
                "The exact installed Claude Desktop configuration passed initialize, tools/list, and get_index_status over stdio."
            ),
            "claude_desktop_config_transport_runtime_fingerprint": (
                "The runtime fingerprint bound to the latest successful installed Claude Desktop config smoke."
            ),
            "claude_desktop_loader_observed": (
                "A restarted Claude Desktop process and post-registration server-name log event were observed; this is not tool inventory proof."
            ),
            "claude_desktop_loader_verified": (
                "Claude Desktop itself loaded the server after restart; direct stdio smoke alone does not set this."
            ),
            "claude_desktop_conversation_verified": (
                "A Claude Desktop conversation successfully invoked a tool from this MCP server."
            ),
            "desktop_restart_required": (
                "Legacy compatibility field only. ChatGPT local MCP is unsupported and this value must not be "
                "used as connection evidence."
            ),
            "desktop_restart_status": (
                "One of not_checked, required, not_running, up_to_date, or unknown."
            ),
            "desktop_app_server_loader_verified": (
                "Compatibility alias for a fresh Codex app-server process inventory; it is not the running Desktop scan."
            ),
            "fresh_codex_app_server_inventory_verified": (
                "A separate Codex app-server process returned the required tools with recorded executable/config provenance."
            ),
            "desktop_status_scan_request_observed": (
                "A restarted Desktop log routed mcpServerStatus/list without an error; this does not prove tool exposure."
            ),
            "direct_stdio_verified": (
                "The generated launcher passed initialize, tools/list, search, and fetch directly over stdio."
            ),
            "desktop_tool_scan_verified": (
                "Legacy compatibility field only; ChatGPT local MCP is unsupported."
            ),
            "conversation_attachment_verified": (
                "The registered MCP tools were observed in the current conversation."
            ),
            "conversation_attachment_unverified": (
                "Legacy compatibility field only; use ChatGPT web with a remote HTTPS MCP app instead."
            ),
            "end_to_end_verified": (
                "Legacy compatibility field only; it cannot establish unsupported ChatGPT local MCP support."
            ),
            "transport_end_to_end_verified": (
                "The generated launcher passed the direct MCP protocol chain; this does not prove Desktop exposure."
            ),
        },
        "profiles": {
            "chatgpt-desktop-local": {
                "support_status": "unsupported",
                "direct_local_supported": False,
                "transport": "unsupported",
                "surface": "legacy_compatibility_artifact",
                "official_help_url": CHATGPT_MCP_HELP_URL,
                "secure_mcp_tunnel_url": CHATGPT_SECURE_MCP_TUNNEL_URL,
            },
            "chatgpt-remote": {
                "transport": "streamable-http",
                "surface": "chatgpt_web_app",
                "official_help_url": CHATGPT_MCP_HELP_URL,
            },
            "claude-desktop": {"transport": "stdio"},
            "claude-code": {"transport": "stdio"},
        },
        "stale_status_reports_cleared_on_generation": list(STALE_BUNDLE_STATUS_REPORT_FILENAMES),
        "first_use": {
                "doctor_script": SETUP_BUNDLE_FILES["doctor"],
                "validate_script": SETUP_BUNDLE_FILES["validate"],
                "client_config_smoke_script": SETUP_BUNDLE_FILES["client_config_smoke"],
                "run_stdio_script": SETUP_BUNDLE_FILES["run_stdio"],
            },
    }
    if setup_manifest is not None:
        payload["server_name"] = setup_manifest.get("server_name")
        payload["connections"] = setup_manifest.get("connections") or []
    if config is not None:
        quickstart = config.get("quickstart") if isinstance(config.get("quickstart"), dict) else {}
        payload["configured_tenant_id"] = _quickstart_tenant_id(quickstart)
    if runtime_ready:
        payload.update(
            {
                "tenant_id": manifest.get("tenant_id"),
                "tenant_storage_isolation": bool(manifest.get("tenant_storage_isolation")),
                "document_id": manifest.get("document_id"),
                "document_ids": manifest.get("document_ids") or [],
                "record_count": manifest.get("record_count"),
                "chunk_count": manifest.get("chunk_count"),
                "recommended_smoke_query": manifest.get("recommended_smoke_query"),
                "bm25_index_status": manifest.get("bm25_index_status"),
                "bm25_document_count": manifest.get("bm25_document_count"),
                "kordoc_table_parser_summary": manifest.get("kordoc_table_parser_summary") or {},
            }
        )
    else:
        payload["recommended_smoke_query"] = None
        payload["record_count"] = 0
    server_name = str(payload.get("server_name") or "").strip()
    if server_name:
        setup_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                setup_manifest or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        client_status = create_client_connection_status(
            server_name,
            runtime_fingerprint=runtime_fingerprint,
            bundle_fingerprint=setup_fingerprint,
            generated_at=payload["generated_at"],
        )
        for key in (
            "schema_version",
            "status_model",
            "active_target",
            "legacy_projection_target",
            "legacy_projection_updated_at",
            "legacy_migration_state",
            "client_connections",
        ):
            payload[key] = client_status[key]
    return payload


def _read_runtime_manifest(runtime_data_dir: Path) -> dict[str, Any]:
    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_setup_bundle_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / SETUP_BUNDLE_FILES["manifest"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_bundle_status(
    output_dir: Path,
    *,
    config: dict[str, Any] | None = None,
    setup_manifest: dict[str, Any] | None = None,
    runtime_manifest: dict[str, Any] | None = None,
) -> Path:
    path = output_dir / SETUP_BUNDLE_FILES["bundle_status"]
    refreshed = _bundle_status_payload(
            output_dir,
            config=config,
            setup_manifest=setup_manifest,
            runtime_manifest=runtime_manifest,
        )
    with _bundle_status_write_guard():
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                decoded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(decoded, dict):
                    existing = decoded
            except (OSError, json.JSONDecodeError):
                existing = {}
        # Bundle generation itself briefly records a refresh state so a
        # concurrent installer cannot treat half-written data as ready.  A
        # brand-new bundle has no installation attempt, so that internal
        # transaction marker must not become its durable public state.  Keep
        # the marker for an already-installed bundle: the fingerprint logic
        # below then preserves registration facts while invalidating runtime
        # verification evidence.
        if (
            not str(existing.get("installation_attempt_id") or "")
            and str(existing.get("installation_state") or "")
            in BUNDLE_GENERATION_TRANSITIONAL_STATES
        ):
            existing = {}
        if str(existing.get("installation_state") or "") in ACTIVE_LOCAL_INSTALLATION_STATES:
            raise RuntimeError("MCP runtime data cannot replace bundle status during an active connection attempt.")
        merged = {**refreshed, **existing}
        for key in (
            "report_type",
            "schema_version",
            "generated_at",
            "bundle_dir",
            "runtime_data_dir",
            "runtime_data_ready",
            "runtime_fingerprint",
            "launcher_ready",
            "launcher_ready_note",
            "portable_handoff_runtime",
            "profiles",
            "connection_state_notes",
            "ui_fields",
            "tenant_id",
            "tenant_storage_isolation",
            "document_id",
            "document_ids",
            "record_count",
            "chunk_count",
            "recommended_smoke_query",
            "bm25_index_status",
            "bm25_document_count",
            "kordoc_table_parser_summary",
        ):
            if key in refreshed:
                merged[key] = refreshed[key]
        prior_fingerprint = str(existing.get("runtime_fingerprint") or "")
        next_fingerprint = str(refreshed.get("runtime_fingerprint") or "")
        if existing and prior_fingerprint != next_fingerprint:
            if isinstance(merged.get("client_connections"), dict):
                if prior_fingerprint:
                    try:
                        client_invalidated = invalidate_client_connection_runtime(
                            merged,
                            prior_fingerprint,
                            next_runtime_fingerprint=next_fingerprint or None,
                        )
                        merged["client_connections"] = client_invalidated[
                            "client_connections"
                        ]
                    except (TypeError, ValueError):
                        pass
                for client_record in merged["client_connections"].values():
                    if not isinstance(client_record, dict):
                        continue
                    readiness = client_record.get("readiness")
                    if isinstance(readiness, dict):
                        readiness["runtime_ready"] = bool(next_fingerprint)
            for key in (
                "process_started",
                "mcp_initialized",
                "tools_discovered",
                "installed_config_transport_verified",
                "generated_client_configs_transport_verified",
                "claude_code_transport_verified",
                "claude_code_conversation_verified",
                "claude_desktop_config_transport_verified",
                "claude_desktop_loader_observed",
                "claude_desktop_loader_verified",
                "claude_desktop_conversation_verified",
                "direct_stdio_verified",
                "transport_end_to_end_verified",
                "fresh_codex_app_server_inventory_verified",
                "desktop_app_server_loader_verified",
                "desktop_tool_scan_verified",
                "conversation_attachment_verified",
                "end_to_end_verified",
            ):
                merged[key] = False
            merged["installed_config_transport_runtime_fingerprint"] = None
            merged["claude_code_transport_runtime_fingerprint"] = None
            merged["claude_desktop_config_transport_runtime_fingerprint"] = None
            merged["fresh_codex_app_server_runtime_fingerprint"] = None
            merged["conversation_attachment_unverified"] = True
            merged["tool_scan_unverified"] = True
            merged["desktop_app_server_tool_count"] = 0
            merged["desktop_app_server_tool_names"] = []
            merged["desktop_app_server_server_info"] = None
            merged["desktop_app_server_error"] = "runtime_changed_revalidation_required"
            if bool(existing.get("direct_config_registered")):
                merged["installation_state"] = "installed_loader_verified_runtime_changed"
            elif bool(existing.get("claude_desktop_config_registered")):
                merged["installation_state"] = (
                    "installed_pending_claude_desktop_verification_runtime_changed"
                )
            merged["connection_state"] = "pending_runtime_revalidation"
        _write_json_utf8_no_bom(path, merged)
    return path


def _quickstart_tenant_id(quickstart: dict[str, Any]) -> str | None:
    audit = quickstart.get("audit_index_visibility") if isinstance(quickstart, dict) else None
    args = audit.get("args") if isinstance(audit, dict) else None
    if not isinstance(args, list):
        return None
    for index, value in enumerate(args[:-1]):
        if str(value) == "--tenant-id":
            return str(args[index + 1])
    return None


def _with_explicit_bundle_data_dir(config: dict[str, Any], data_dir: str | Path) -> dict[str, Any]:
    payload = json.loads(json.dumps(config, ensure_ascii=False))
    bundle_data_dir = str(Path(data_dir).resolve())

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            args = value.get("args")
            if isinstance(args, list):
                for index, item in enumerate(args[:-1]):
                    if str(item) == "--data-dir":
                        args[index + 1] = bundle_data_dir
            for key, child in list(value.items()):
                if isinstance(child, str):
                    value[key] = _with_explicit_bundle_data_dir_string(child, bundle_data_dir)
                    continue
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, str):
                    value[index] = _with_explicit_bundle_data_dir_string(child, bundle_data_dir)
                    continue
                visit(child)

    visit(payload)
    return payload


def _with_explicit_bundle_data_dir_string(value: str, bundle_data_dir: str) -> str:
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, (dict, list)):
            container = {"value": payload}
            explicit = _with_explicit_bundle_data_dir(container, bundle_data_dir)["value"]
            return json.dumps(explicit, ensure_ascii=False, separators=(",", ":"))
    if "--data-dir" not in value:
        return value
    return re.sub(
        r"(--data-dir\s+)(?:\"[^\"]*\"|'[^']*'|\S+)",
        lambda match: match.group(1) + _quote_command_data_dir(bundle_data_dir),
        value,
    )


def _with_bundle_stdio_launcher(config: dict[str, Any], *, launcher_path: str | Path, server_name: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(config, ensure_ascii=False))
    launcher = str(Path(launcher_path).resolve())

    def patch_node(node: Any) -> Any:
        if isinstance(node, dict):
            patch_server(node)
            for key, child in list(node.items()):
                node[key] = patch_node(child)
            return node
        if isinstance(node, list):
            for index, child in enumerate(node):
                node[index] = patch_node(child)
            return node
        if isinstance(node, str):
            return patch_json_string(node)
        return node

    def patch_json_string(value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith(("{", "[")):
            return value
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        if not isinstance(decoded, (dict, list)):
            return value
        patched = patch_node(decoded)
        return json.dumps(patched, ensure_ascii=False, separators=(",", ":"))

    def patch_server(server: Any) -> None:
        if not isinstance(server, dict):
            return
        args = server.get("args")
        direct_source = (
            isinstance(args, list)
            and len(args) >= 2
            and str(args[0]) == "-m"
            and str(args[1]) == "scripts.run_regulation_mcp"
        )
        stdio_server_args = _stdio_server_args_from_client_entry(server)
        if stdio_server_args is not None:
            transport = _arg_value(stdio_server_args, "--transport")
            if transport in {None, "stdio"}:
                server["command"] = "powershell.exe"
                server["args"] = _powershell_stdio_launcher_client_args(launcher, stdio_server_args)
                if direct_source and isinstance(server.get("env"), dict):
                    env = dict(server["env"])
                    env.pop("PYTHONPATH", None)
                    env.pop("PYTHONSAFEPATH", None)
                    if env:
                        server["env"] = env
                    else:
                        server.pop("env", None)
        server_command = server.get("serverCommand")
        if isinstance(server_command, dict):
            patch_server(server_command)

    return patch_node(payload)


def _stdio_server_args_from_client_entry(server: dict[str, Any]) -> list[str] | None:
    args = server.get("args")
    if not isinstance(args, list):
        return None
    args_text = [str(arg) for arg in args]
    command = str(server.get("command") or "")
    if command == "reg-rag-mcp-server":
        return args_text
    if (
        len(args_text) >= 2
        and args_text[0] == "-m"
        and args_text[1] == "scripts.run_regulation_mcp"
    ):
        return args_text[2:]
    if _is_python_command(command) and args_text and _is_run_regulation_mcp_script(args_text[0]):
        return args_text[1:]
    if _is_powershell_command(command):
        file_index = _case_insensitive_arg_index(args_text, "-File")
        if file_index is not None and file_index + 1 < len(args_text):
            if _is_stdio_launcher_script(args_text[file_index + 1]):
                return args_text[file_index + 2 :]
        if args_text and _is_run_regulation_mcp_script(args_text[0]):
            return args_text[1:]
    return None


def _case_insensitive_arg_index(args: list[str], expected: str) -> int | None:
    expected_lower = expected.lower()
    for index, arg in enumerate(args):
        if arg.lower() == expected_lower:
            return index
    return None


def _is_python_command(command: str) -> bool:
    leaf = _path_leaf(command)
    return bool(
        leaf in {"py", "py.exe"}
        or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", leaf)
    )


def _is_powershell_command(command: str) -> bool:
    leaf = _path_leaf(command)
    return leaf in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def _is_run_regulation_mcp_script(value: str) -> bool:
    return _path_leaf(value) == "run_regulation_mcp.py"


def _is_stdio_launcher_script(value: str) -> bool:
    return _path_leaf(value) == "run_mcp_stdio_server.ps1"


def _path_leaf(value: str) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def _bundle_stdio_launcher_default_args(
    config: dict[str, Any],
    *,
    server_name: str,
    bundle_data_dir: str | Path,
) -> list[object]:
    server_args: list[Any] = []
    claude_desktop = config.get("claude_desktop")
    if isinstance(claude_desktop, dict):
        servers = claude_desktop.get("mcpServers")
        if isinstance(servers, dict):
            server = servers.get(server_name)
            if isinstance(server, dict) and isinstance(server.get("args"), list):
                normalized_args = _stdio_server_args_from_client_entry(server)
                server_args = list(normalized_args if normalized_args is not None else server["args"])
    bundle_data_dir_text = str(Path(bundle_data_dir).resolve())
    relative_args: list[object] = []
    for arg in server_args:
        if str(arg) == bundle_data_dir_text:
            relative_args.append(BUNDLE_DATA_DIR_ARG)
        else:
            relative_args.append(str(arg))
    return relative_args


def _powershell_stdio_launcher_client_args(launcher_path: str, server_args: list[Any]) -> list[str]:
    return [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        launcher_path,
        *[str(arg) for arg in server_args],
    ]


def _quote_command_data_dir(value: str) -> str:
    if any(char.isspace() for char in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _codex_config_snippet(claude_desktop_config: dict[str, Any], *, server_name: str) -> str:
    mcp_servers = claude_desktop_config.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        return ""
    server = mcp_servers.get(server_name)
    if not isinstance(server, dict):
        return ""
    command = str(server.get("command") or "reg-rag-mcp-server")
    args = server.get("args")
    if not isinstance(args, list):
        args = []
    cwd = ""
    for index, arg in enumerate(args[:-1]):
        if str(arg).lower() == "-file":
            cwd = str(Path(str(args[index + 1])).resolve().parent)
            break
    lines = [
        "# Handoff ZIPs use <BUNDLE_DIR> as a template marker; materialize it to the extracted absolute path before manual use.",
        "# On Windows, use a forward-slash absolute path such as C:/MCP/aksmcp2, or escape every backslash for valid TOML.",
        "# Paste or replace this server block in $HOME\\.codex\\config.toml only after that path materialization.",
        "# Keep --data-dir pointed at this bundle's data directory to avoid stale or slow MCP startup.",
        f"[mcp_servers.{_toml_key(server_name)}]",
        f"command = {_toml_string(command)}",
        "startup_timeout_sec = 45",
    ]
    if cwd:
        lines.append(f"cwd = {_toml_string(cwd)}")
    lines.append("args = [")
    lines.extend(f"  {_toml_string(str(arg))}," for arg in args)
    lines.append("]")
    return "\n".join(lines)


def _portable_bundle_doc_command(command: object) -> str:
    """Render a bundle command without retaining its build-host data path."""

    text = str(command)
    return re.sub(
        r"(?i)(--data-dir\s+).+?(\s+--tenant-id\b)",
        r"\1.\\data\2",
        text,
        count=1,
    )


def _unsupported_chatgpt_desktop_local_payload(
    *,
    server_name: str,
    bundle_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a compatibility artifact that cannot be mistaken for a runnable config."""

    payload: dict[str, Any] = {
        "profile": "chatgpt-desktop-local",
        "client": "ChatGPT",
        "surface": "legacy_compatibility_artifact",
        "support_status": "unsupported",
        "direct_local_supported": False,
        # Retain the old field name for readers that consumed earlier bundles,
        # but fail closed instead of advertising a runnable local contract.
        "chatgpt_direct_local_mcp_supported": False,
        "server_name": server_name,
        "warning": CHATGPT_LOCAL_MCP_UNSUPPORTED_REASON,
        "official_help_url": CHATGPT_MCP_HELP_URL,
        "secure_mcp_tunnel_url": CHATGPT_SECURE_MCP_TUNNEL_URL,
        "replacement_paths": {
            "remote_https": {
                "surface": "chatgpt_web",
                "transport": "streamable-http",
                "requires_reachable_https": True,
                "official_help_url": CHATGPT_MCP_HELP_URL,
            },
            "private_network_or_developer_machine": {
                "transport": "secure_mcp_tunnel",
                "official_guide_url": CHATGPT_SECURE_MCP_TUNNEL_URL,
            },
        },
        "operator_steps": [
            "Do not enter a local command, arguments, working directory, or environment in ChatGPT.",
            "For a reachable remote server, use ChatGPT web Developer mode and create an app with the final HTTPS /mcp endpoint.",
            "For a private, on-premises, or developer-machine server, follow the OpenAI Secure MCP Tunnel guide.",
        ],
    }
    if bundle_dir is not None:
        payload["compatibility_artifact_path"] = str(
            Path(bundle_dir).resolve() / SETUP_BUNDLE_FILES["chatgpt_desktop_local"]
        )
    return payload


def _chatgpt_desktop_local_config(
    claude_desktop_config: dict[str, Any],
    *,
    server_name: str,
    bundle_dir: str | Path,
) -> dict[str, Any]:
    """Write the legacy filename as a warning-only, non-runnable artifact."""

    del claude_desktop_config
    return _unsupported_chatgpt_desktop_local_payload(
        server_name=server_name,
        bundle_dir=bundle_dir,
    )


def _local_stdio_config_for_server(
    local_stdio_config: dict[str, Any],
    *,
    server_name: str,
) -> dict[str, Any]:
    """Return a one-server stdio config consistently keyed by the requested bundle name."""
    mcp_servers = local_stdio_config.get("mcpServers") if isinstance(local_stdio_config, dict) else None
    if not isinstance(mcp_servers, dict) or not mcp_servers:
        raise ValueError("Local stdio config must contain exactly one MCP server entry.")
    selected = mcp_servers.get(server_name)
    if not isinstance(selected, dict):
        candidates = [entry for entry in mcp_servers.values() if isinstance(entry, dict)]
        if len(candidates) != 1:
            raise ValueError(f"Local stdio config does not contain an unambiguous MCP server {server_name}.")
        selected = candidates[0]
    normalized = dict(local_stdio_config)
    normalized["mcpServers"] = {server_name: dict(selected)}
    return normalized


def _bundle_relative_path(output_dir: Path, path: str | Path) -> str:
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        return candidate.name


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _replace_runtime_path_prefixes(value: Any, *, source_root: Path, target_root: Path) -> Any:
    """Rebase staging paths before a runtime manifest is committed."""

    source_prefixes = {
        str(source_root): str(target_root),
        str(source_root.resolve()): str(target_root.resolve()),
        source_root.as_posix(): target_root.as_posix(),
        source_root.resolve().as_posix(): target_root.resolve().as_posix(),
    }
    if isinstance(value, dict):
        return {
            key: _replace_runtime_path_prefixes(child, source_root=source_root, target_root=target_root)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_runtime_path_prefixes(child, source_root=source_root, target_root=target_root)
            for child in value
        ]
    if not isinstance(value, str):
        return value
    for source_prefix, target_prefix in sorted(source_prefixes.items(), key=lambda item: len(item[0]), reverse=True):
        if value == source_prefix:
            return target_prefix
        if value.startswith(source_prefix + os.sep) or value.startswith(source_prefix + "/"):
            return target_prefix + value[len(source_prefix) :]
    return value


def _canonical_content_sha256(value: Any) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _runtime_data_builder_implementation_sha256() -> str:
    """Fingerprint loaded source modules that define runtime export semantics."""

    module_names = {
        "app.api.routes_rag",
        "app.core.tenant_access",
        "app.ingestion.vector_adapter",
        "app.mcp_server.regulation_tools",
        "app.retrieval.bm25_index",
        "app.retrieval.hierarchical_index",
        "app.retrieval.regulation_reference_graph",
        "app.retrieval.tokenizer",
        "app.services.regulation_catalog_service",
        "app.storage.repository",
    }
    source_sha256 = {
        "scripts.generate_mcp_client_config": _sha256_file_content(Path(__file__)),
    }
    for module_name in sorted(module_names):
        module = sys.modules.get(module_name)
        source_path_value = (
            getattr(module, "__file__", None)
            if module is not None
            else getattr(importlib.util.find_spec(module_name), "origin", None)
        )
        source_path = Path(source_path_value) if source_path_value else None
        source_sha256[module_name] = (
            _sha256_file_content(source_path)
            if source_path is not None and source_path.is_file()
            else None
        )
    return _canonical_content_sha256(source_sha256)


def _prepare_mcp_runtime_data_bundle_inputs(
    *,
    source_data_dir: str | Path,
    tenant_id: str,
    profile_id: str | None,
    document_id: str | None,
    document_ids: list[str] | None,
    scope: str | None,
    tenant_storage_isolation: bool | None,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    require_kordoc_table_parser: bool,
    require_source_metadata: bool,
    progress_callback: Callable[[int, str, int | None, int | None], None] | None,
) -> dict[str, Any]:
    """Validate and snapshot every source value that can affect a runtime export."""

    requested_document_ids = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in (document_ids or [])
            if str(value or "").strip()
        )
    )
    normalized_scope = str(scope or "").strip().lower() or (
        "selected_documents" if requested_document_ids else None
    )
    if normalized_scope not in {
        None,
        "document",
        "selected_documents",
        "selected_institution",
        "institution_profile",
    }:
        raise ValueError("scope must be document, selected_documents, or selected_institution.")
    if normalized_scope == "document" and not str(document_id or "").strip():
        raise ValueError("document scope requires document_id.")
    if normalized_scope == "selected_documents" and not requested_document_ids:
        raise ValueError("selected_documents scope requires document_ids.")
    if normalized_scope == "selected_documents" and str(document_id or "").strip():
        raise ValueError("selected_documents scope must not include document_id.")
    if requested_document_ids and normalized_scope != "selected_documents":
        raise ValueError("document_ids can be used only with selected_documents scope.")
    if normalized_scope == "selected_institution" and str(document_id or "").strip():
        raise ValueError("selected_institution scope must not include document_id.")
    if (
        normalized_scope in {"selected_documents", "selected_institution", "institution_profile"}
        and not str(profile_id or "").strip()
    ):
        raise ValueError("Institution-scoped MCP bundles require profile_id.")
    if not str(document_id or "").strip() and not requested_document_ids and not str(profile_id or "").strip():
        raise ValueError("MCP runtime export requires document_id or profile_id; tenant-wide export is not allowed.")
    resolved_scope = normalized_scope or (
        "document" if document_id else "selected_documents" if requested_document_ids else "institution_profile"
    )

    source_settings = settings_for_mcp_project(
        data_dir=source_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    auth = mcp_auth_context(
        tenant_id=tenant_id,
        actor=actor or "mcp-bundle-exporter",
        role=role or "operator",
        department_ids=department_ids,
    )
    records = _runtime_visible_records_for_export(
        settings=source_settings,
        auth=auth,
        profile_id=profile_id,
        document_id=document_id,
    )
    source_repository = JsonRepository(source_settings)
    expected_document_ids: set[str] | None = None
    if resolved_scope in {"selected_institution", "institution_profile"}:
        expected_document_ids = _institution_runtime_export_document_ids(
            repository=source_repository,
            tenant_id=tenant_id,
            profile_id=str(profile_id or ""),
        )
    if requested_document_ids:
        requested_document_id_set = set(requested_document_ids)
        records = [
            record
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            in requested_document_id_set
        ]
        visible_document_ids = {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
        }
        missing_document_id_set = requested_document_id_set - visible_document_ids
        explicitly_rejected_document_ids = _runtime_export_fully_rejected_document_ids(
            repository=source_repository,
            document_ids=missing_document_id_set,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        missing_document_ids = sorted(
            missing_document_id_set - explicitly_rejected_document_ids
        )
        if missing_document_ids:
            raise ValueError(
                "Selected regulations are not all MCP-visible. Approve and index these document IDs first: "
                + ", ".join(missing_document_ids)
            )
    if resolved_scope in {"selected_institution", "institution_profile"}:
        visible_document_ids = {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
        }
        omitted_document_ids = sorted((expected_document_ids or set()) - visible_document_ids)
        if omitted_document_ids:
            raise ValueError(
                "Institution-scoped MCP runtime export would omit approved or superseded source documents. "
                "Ensure every current approved chunk is approval-journal-valid and indexed for these document IDs: "
                + ", ".join(omitted_document_ids)
            )
    if not records:
        target = (
            f" for document_ids={','.join(requested_document_ids)}"
            if requested_document_ids
            else f" for document_id={document_id}"
            if document_id
            else ""
        )
        raise ValueError(f"No MCP-visible approved records are available{target}. Approve and index first.")
    records = canonicalize_runtime_records(records)
    _report_runtime_progress(progress_callback, 5, "승인된 규정 레코드 확인", len(records), len(records))

    exported_document_ids = sorted(
        {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        }
    )
    selected_source_document_ids = sorted(
        set(requested_document_ids)
        if requested_document_ids
        else {str(document_id).strip()}
        if str(document_id or "").strip()
        else set(expected_document_ids or exported_document_ids)
    )
    source_metadata_summary = _runtime_source_metadata_summary(
        records,
        source_repository,
        exported_document_ids,
    )
    if require_source_metadata:
        _require_runtime_source_metadata(source_metadata_summary)
    if require_kordoc_table_parser:
        kordoc_table_parser_summary = _require_kordoc_table_parser_evidence(
            source_repository,
            exported_document_ids,
        )
    else:
        kordoc_table_parser_summary = _kordoc_table_parser_evidence_summary(
            source_repository,
            exported_document_ids,
        )
    _report_runtime_progress(
        progress_callback,
        12,
        "출처 및 파서 증빙 확인",
        len(exported_document_ids),
        len(exported_document_ids),
    )

    repository_manifest = _empty_runtime_repository_manifest()
    chunks_by_document: dict[str, list[dict[str, Any]]] = {}
    records_by_document: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        current_document_id = str(record.get("document_id") or metadata.get("document_id") or "")
        current_chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
        records_by_document.setdefault(current_document_id, {})[current_chunk_id] = record

    selected_document_id_set = set(exported_document_ids)
    audit_document_id_set = set(selected_source_document_ids)
    approval_records_by_document: dict[str, list[dict[str, Any]]] = {}
    for record in source_repository.list_approval_journal_records():
        current_document_id = record.get("document_id")
        if isinstance(current_document_id, str) and current_document_id in audit_document_id_set:
            approval_records_by_document.setdefault(current_document_id, []).append(record)
    review_records_by_document: dict[str, list[dict[str, Any]]] = {}
    for record in source_repository.list_review_journal_records():
        current_document_id = record.get("document_id")
        if isinstance(current_document_id, str) and current_document_id in audit_document_id_set:
            review_records_by_document.setdefault(current_document_id, []).append(record)
    indexing_jobs_by_document: dict[str, list[dict[str, Any]]] = {}
    for record in source_repository.list_indexing_jobs():
        current_document_id = record.get("document_id")
        if isinstance(current_document_id, str) and current_document_id in selected_document_id_set:
            indexing_jobs_by_document.setdefault(current_document_id, []).append(record)

    approval_records: list[dict[str, Any]] = []
    indexing_jobs: list[dict[str, Any]] = []
    for current_document_id in exported_document_ids:
        document = source_repository.get_document(current_document_id)
        if document is None:
            continue
        repository_manifest["documents"][current_document_id] = document.model_dump(mode="json")
        records_by_chunk_id = records_by_document.get(current_document_id, {})
        chunks = _current_approved_chunks_for_runtime_export(
            repository=source_repository,
            document_id=current_document_id,
            visible_chunk_ids=set(records_by_chunk_id),
            records_by_chunk_id=records_by_chunk_id,
            tenant_id=tenant_id,
            approval_records=approval_records_by_document.get(current_document_id, []),
            review_records=review_records_by_document.get(current_document_id, []),
        )
        completeness_issue = _runtime_export_document_completeness_issue(
            repository=source_repository,
            document_id=current_document_id,
            visible_chunk_ids=set(records_by_chunk_id),
            tenant_id=tenant_id,
            approval_records=approval_records_by_document.get(current_document_id, []),
            review_records=review_records_by_document.get(current_document_id, []),
        )
        if completeness_issue is not None:
            missing_chunk_ids = completeness_issue["missing_approved_chunk_ids"]
            unresolved_chunks = completeness_issue["unresolved_chunks"]
            missing_text = (
                " Missing approved-but-unindexed chunks: "
                + ", ".join(missing_chunk_ids[:5])
                + "."
                if missing_chunk_ids
                else ""
            )
            unresolved_text = (
                " Resolve remaining chunk reviews: "
                + ", ".join(
                    f"{item['chunk_id']}:{item['approval_status']}"
                    for item in unresolved_chunks[:5]
                )
                + "."
                if unresolved_chunks
                else ""
            )
            unaudited_rejection_ids = completeness_issue["unaudited_rejection_chunk_ids"]
            unaudited_rejection_text = (
                " Record explicit rejection decisions for these chunks: "
                + ", ".join(unaudited_rejection_ids[:5])
                + "."
                if unaudited_rejection_ids
                else ""
            )
            unaudited_superseded_ids = completeness_issue["unaudited_superseded_chunk_ids"]
            unaudited_superseded_text = (
                " Record split/merge decisions for these superseded chunks: "
                + ", ".join(unaudited_superseded_ids[:5])
                + "."
                if unaudited_superseded_ids
                else ""
            )
            raise ValueError(
                "MCP runtime export would be incomplete for document "
                f"{current_document_id}. Approve or explicitly reject every current chunk, "
                "then reindex before creating a handoff bundle."
                f"{missing_text}{unresolved_text}{unaudited_rejection_text}"
                f"{unaudited_superseded_text}"
            )
        chunks_by_document[current_document_id] = [
            chunk.model_dump(mode="json")
            for chunk in chunks
        ]
        approval_records.extend(approval_records_by_document.get(current_document_id, ()))
        indexing_jobs.extend(indexing_jobs_by_document.get(current_document_id, ()))

    omission_disposition_projection = _runtime_omission_disposition_projection(
        repository=source_repository,
        tenant_id=tenant_id,
        requested_document_ids=selected_source_document_ids,
        exported_records=records,
        approval_records_by_document=approval_records_by_document,
        review_records_by_document=review_records_by_document,
    )

    # Approval and indexing history is authoritative in append-only journals.
    # Keep the manifest keys for JsonRepository schema compatibility without
    # duplicating large journal payloads into repository/manifest.json.
    repository_manifest["approvals"] = {}
    repository_manifest["indexing_jobs"] = {}

    # The source approval journal is an operator audit artifact and can contain
    # reviewer identities, notes, local paths, review evidence, and event
    # payloads.  The portable runtime needs only a deterministic decision
    # ledger for latest-decision ordering and approval/content binding.
    approval_records = _runtime_approval_decision_projection(approval_records)

    effective_department_ids = sorted(
        {
            str(value or "").strip()
            for value in getattr(auth, "department_ids", ())
            if str(value or "").strip()
        }
    )
    recommended_smoke_query = _recommended_runtime_smoke_query(records)
    fingerprint_payload = {
        "schema_version": RUNTIME_DATA_REUSE_SCHEMA_VERSION,
        "builder_contract": {
            "bm25_index_version": BM25_INDEX_VERSION,
            "bm25_structured_metadata_version": BM25_STRUCTURED_METADATA_VERSION,
            "bm25_tokenizer": tokenizer_name(),
            "hierarchical_index_schema_version": HIERARCHICAL_INDEX_SCHEMA_VERSION,
            "rebuild_fingerprint_schema_version": REBUILD_FINGERPRINT_SCHEMA_VERSION,
            # Static deployment bundles must be regenerated when a
            # future-effective revision crosses into force, even if no source
            # bytes changed.
            "lifecycle_as_of_date": date.today().isoformat(),
            "implementation_sha256": _runtime_data_builder_implementation_sha256(),
        },
        "request": {
            "tenant_id": tenant_id,
            "profile_id": profile_id,
            "document_id": document_id,
            "requested_document_ids": sorted(requested_document_ids),
            "scope": resolved_scope,
            "tenant_storage_isolation": tenant_storage_isolation,
            "effective_tenant_storage_isolation": bool(
                getattr(source_settings, "tenant_storage_isolation", False)
            ),
            "actor": str(getattr(auth, "actor", "") or ""),
            "role": str(getattr(auth, "role", "") or ""),
            "department_ids": effective_department_ids,
            "require_kordoc_table_parser": bool(require_kordoc_table_parser),
            "require_source_metadata": bool(require_source_metadata),
        },
        "document_ids": exported_document_ids,
        "records": records,
        "repository_manifest": repository_manifest,
        "chunks_by_document": chunks_by_document,
        "approval_records": approval_records,
        "indexing_jobs": indexing_jobs,
        "omission_disposition_projection": omission_disposition_projection,
        "recommended_smoke_query": recommended_smoke_query,
        "source_metadata_summary": source_metadata_summary,
        "kordoc_table_parser_summary": kordoc_table_parser_summary,
    }
    return {
        "requested_document_ids": requested_document_ids,
        "selected_source_document_ids": selected_source_document_ids,
        "resolved_scope": resolved_scope,
        "source_settings": source_settings,
        "auth": auth,
        "records": records,
        "document_ids": exported_document_ids,
        "repository_manifest": repository_manifest,
        "chunks_by_document": chunks_by_document,
        "approval_records": approval_records,
        "indexing_jobs": indexing_jobs,
        "omission_disposition_projection": omission_disposition_projection,
        "total_chunks": sum(len(chunks) for chunks in chunks_by_document.values()),
        "recommended_smoke_query": recommended_smoke_query,
        "source_metadata_summary": source_metadata_summary,
        "kordoc_table_parser_summary": kordoc_table_parser_summary,
        "manifest_identity": {
            "tenant_id": tenant_id,
            "profile_id": profile_id,
            "scope": resolved_scope,
            # Runtime exports always use the portable flat repository layout;
            # retain the source setting separately for provenance and reuse
            # validation.
            "tenant_storage_isolation": False,
            "source_tenant_storage_isolation": bool(
                getattr(source_settings, "tenant_storage_isolation", False)
            ),
            "document_id": document_id,
            "document_ids": exported_document_ids,
            "kordoc_table_parser_required": bool(require_kordoc_table_parser),
            "source_metadata_required": bool(require_source_metadata),
        },
        "input_sha256": _canonical_content_sha256(fingerprint_payload),
    }


def _write_mcp_runtime_data_bundle_uncommitted(
    *,
    source_data_dir: str | Path,
    out_dir: str | Path,
    tenant_id: str = "default",
    profile_id: str | None = None,
    document_id: str | None = None,
    document_ids: list[str] | None = None,
    scope: str | None = None,
    tenant_storage_isolation: bool | None = None,
    actor: str | None = None,
    role: str | None = None,
    department_ids: list[str] | None = None,
    require_kordoc_table_parser: bool = True,
    require_source_metadata: bool = True,
    progress_callback: Callable[[int, str, int | None, int | None], None] | None = None,
    _runtime_data_dir: Path | None = None,
    _write_status: bool = True,
    _prepared_runtime_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write approved MCP-visible runtime data under ``out_dir/data``.

    The generated setup JSON is not enough for a working local MCP handoff. The
    MCP server also needs the approved vector records, the repository manifest,
    approved chunks, and the approval journal used by the visibility gate.
    """
    prepared = _prepared_runtime_inputs or _prepare_mcp_runtime_data_bundle_inputs(
        source_data_dir=source_data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        document_id=document_id,
        document_ids=document_ids,
        scope=scope,
        tenant_storage_isolation=tenant_storage_isolation,
        actor=actor,
        role=role,
        department_ids=department_ids,
        require_kordoc_table_parser=require_kordoc_table_parser,
        require_source_metadata=require_source_metadata,
        progress_callback=None,
    )

    output_dir = Path(out_dir)
    final_runtime_data_dir = output_dir / "data"
    runtime_data_dir = _runtime_data_dir or final_runtime_data_dir
    source_settings = prepared["source_settings"]
    auth = prepared["auth"]
    records = prepared["records"]
    _report_runtime_progress(progress_callback, 5, "승인된 규정 레코드 확인", len(records), len(records))

    exported_document_ids = prepared["document_ids"]
    document_ids = exported_document_ids
    source_metadata_summary = prepared["source_metadata_summary"]
    kordoc_table_parser_summary = prepared["kordoc_table_parser_summary"]
    resolved_scope = str(prepared["resolved_scope"])
    _report_runtime_progress(progress_callback, 12, "출처·표 파서 증빙 확인", len(document_ids), len(document_ids))
    _prepare_runtime_data_export_dir(runtime_data_dir, source_settings.data_dir)
    runtime_repository_dir = runtime_data_dir / "repository"
    runtime_repository_dir.mkdir(parents=True, exist_ok=True)
    runtime_vector_dir = runtime_data_dir / "vector_db" / tenant_directory_key(tenant_id)
    runtime_vector_dir.mkdir(parents=True, exist_ok=True)

    vector_path = runtime_vector_dir / "approved_vectors.jsonl"
    vector_offsets = write_vector_records_with_offsets(
        vector_path,
        records,
        progress_callback=lambda current, total: _report_runtime_progress(
            progress_callback,
            14 + int((current / max(total, 1)) * 18),
            "승인 벡터 저장",
            current,
            total,
        ),
    )
    bm25_index_path = runtime_vector_dir / "bm25_index.json"
    _report_runtime_progress(progress_callback, 34, "빠른 본문 검색 색인 생성", 0, len(records))
    bm25_index = write_bm25_index(bm25_index_path, records)
    _report_runtime_progress(progress_callback, 44, "빠른 본문 검색 색인 완료", len(records), len(records))
    hierarchy_path = hierarchical_index_path(runtime_data_dir)
    hierarchy_summary = build_hierarchical_runtime_index(
        hierarchy_path,
        records,
        tenant_id=tenant_id,
        profile_id=profile_id,
        vector_offsets=vector_offsets,
        progress_callback=lambda percent, message, current, total: _report_runtime_progress(
            progress_callback,
            45 + int(percent * 0.35),
            message,
            current,
            total,
        ),
    )
    expected_source_content_hashes = source_content_hashes(records)
    expected_logical_corpus_sha256 = logical_corpus_sha256_for_records(
        records,
        tenant_id=tenant_id,
        profile_id=profile_id,
    )
    if (
        bm25_index.source_content_hashes != expected_source_content_hashes
        or hierarchy_summary.get("source_content_hashes")
        != expected_source_content_hashes
        or hierarchy_summary.get("logical_corpus_sha256")
        != expected_logical_corpus_sha256
    ):
        raise RuntimeError(
            "Generated runtime indexes are not bound to the same approved corpus."
        )

    manifest = copy.deepcopy(prepared["repository_manifest"])
    total_chunks = int(prepared["total_chunks"])
    approval_records = prepared["approval_records"]
    indexing_jobs = prepared["indexing_jobs"]
    chunks_by_document = prepared["chunks_by_document"]
    exported_result_files: list[str] = []
    document_total = len(document_ids)
    for document_index, current_document_id in enumerate(document_ids, start=1):
        chunks = chunks_by_document.get(current_document_id)
        if chunks is None:
            continue
        _write_runtime_result_json(
            runtime_repository_dir,
            current_document_id,
            "chunks",
            chunks,
            exported_result_files,
        )
        _report_runtime_progress(
            progress_callback,
            80 + int((document_index / max(document_total, 1)) * 14),
            "문서별 승인 이력 묶기",
            document_index,
            document_total,
        )

    manifest_path = runtime_repository_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_runtime_journal(runtime_repository_dir, "approvals", approval_records)
    _write_runtime_journal(runtime_repository_dir, "indexing_jobs", indexing_jobs)
    approval_snapshot_path = _write_runtime_approval_snapshot_sidecar(
        runtime_data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        document_ids=document_ids,
        records=records,
        auth=auth,
    )
    omission_disposition_snapshot_path = _write_runtime_omission_disposition_sidecar(
        runtime_repository_dir=runtime_repository_dir,
        projection=prepared["omission_disposition_projection"],
    )
    _report_runtime_progress(progress_callback, 97, "런타임 manifest 생성", len(records), len(records))

    runtime_manifest = {
        "report_type": "mcp_runtime_data_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_data_reuse": {
            "schema_version": RUNTIME_DATA_REUSE_SCHEMA_VERSION,
            "input_sha256": prepared["input_sha256"],
        },
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "scope": resolved_scope,
        "synthetic_runtime": False,
        "provenance": "approved_runtime_bundle_export",
        # The distributable data directory is deliberately flattened to
        # data/repository and data/vector_db/<tenant>.  Runtime consumers must
        # not prepend tenants/<tenant> to that exported layout.
        "tenant_storage_isolation": False,
        "source_tenant_storage_isolation": bool(
            getattr(source_settings, "tenant_storage_isolation", False)
        ),
        # The distributable runtime proves its own tenant/document/index
        # contents.  Do not leak the operator's source checkout, upload, or a
        # previous release-candidate path into the handoff manifest.
        "source_data_dir": None,
        "source_data_provenance": "approved_local_export",
        "runtime_data_dir": str(final_runtime_data_dir),
        "document_id": document_id,
        "document_ids": document_ids,
        "record_count": len(records),
        "chunk_count": total_chunks,
        "recommended_smoke_query": prepared["recommended_smoke_query"],
        "approval_record_count": len(approval_records),
        "indexing_job_count": len(indexing_jobs),
        "kordoc_table_parser_required": bool(require_kordoc_table_parser),
        "kordoc_table_parser_summary": kordoc_table_parser_summary,
        "source_metadata_required": bool(require_source_metadata),
        "source_metadata_summary": source_metadata_summary,
        "bm25_document_count": bm25_index.document_count,
        "bm25_index_status": "ready",
        "hierarchical_index_status": "ready",
        "hierarchical_index": hierarchy_summary,
        "rebuild_fingerprint_schema_version": hierarchy_summary["rebuild_fingerprint_schema_version"],
        "logical_corpus_sha256": hierarchy_summary["logical_corpus_sha256"],
        "rebuild_contract": {
            "scope": "institution_regulation_revision_toc_article",
            "input_order_independent": True,
            "institution_identity": "normalized_institution_name",
            "regulation_identity": "institution_profile_plus_normalized_regulation_title",
            "latest_version_rule": "maximum_content_revision_or_effective_date",
            "approval_rule": "approved_and_superseded_history_current_approved_default",
        },
        "regulation_count": hierarchy_summary["regulation_count"],
        "regulation_version_count": hierarchy_summary["regulation_version_count"],
        "toc_node_count": hierarchy_summary["toc_node_count"],
        "files": {
            "vector_jsonl": str(vector_path),
            "bm25_index": str(bm25_index_path) if bm25_index_path.is_file() else None,
            "hierarchical_index": str(hierarchy_path),
            "hierarchical_index_sha256": hierarchy_summary["sha256"],
            "repository_manifest": str(manifest_path),
            "approval_journal": str(runtime_repository_dir / "journals" / "approvals.jsonl"),
            "approval_snapshot": str(approval_snapshot_path),
            "omission_disposition_snapshot": str(omission_disposition_snapshot_path),
            "result_files": exported_result_files,
        },
    }
    runtime_manifest = _replace_runtime_path_prefixes(
        runtime_manifest,
        source_root=runtime_data_dir,
        target_root=final_runtime_data_dir,
    )
    runtime_manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    runtime_manifest["files"]["runtime_manifest"] = str(final_runtime_data_dir / "mcp_runtime_manifest.json")
    runtime_manifest["runtime_data_reuse"]["file_sha256"] = _runtime_data_file_sha256(runtime_data_dir)
    runtime_manifest["runtime_data_reuse"]["manifest_sha256"] = _runtime_manifest_content_sha256(
        runtime_manifest
    )
    runtime_manifest_path.write_text(json.dumps(runtime_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if _write_status:
        _write_bundle_status(output_dir, runtime_manifest=runtime_manifest)
    return runtime_manifest


def _sha256_file_content(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _is_mutable_runtime_generated_file(path: Path) -> bool:
    """Return whether *path* is operational state, not immutable bundle data."""

    return path.name in RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES


def _runtime_data_file_sha256(runtime_data_dir: Path) -> dict[str, str]:
    """Hash immutable reusable data, excluding the manifest and operational state."""

    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    digests: dict[str, str] = {}
    for path in sorted(runtime_data_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                "Runtime data bundles cannot contain symbolic links: "
                + path.relative_to(runtime_data_dir).as_posix()
            )
        if (
            not path.is_file()
            or path == manifest_path
            or _is_mutable_runtime_generated_file(path)
        ):
            continue
        digests[path.relative_to(runtime_data_dir).as_posix()] = _sha256_file_content(path)
    return digests


def _runtime_manifest_content_sha256(manifest: dict[str, Any]) -> str:
    payload = copy.deepcopy(manifest)
    reuse = payload.get("runtime_data_reuse")
    if isinstance(reuse, dict):
        reuse.pop("manifest_sha256", None)
    return _canonical_content_sha256(payload)


def validate_mcp_runtime_data_bundle_integrity(
    runtime_data_dir: Path,
    *,
    expected_logical_corpus_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate an already-generated runtime bundle against its sealed manifest.

    This check intentionally does not need access to the source repository.  It is
    used by operator-facing readiness checks after generation, where a missing or
    modified approval journal, snapshot, vector, or index must fail closed.
    """

    runtime_data_dir = Path(runtime_data_dir)
    if runtime_data_dir.is_symlink() or not runtime_data_dir.is_dir():
        raise ValueError("MCP runtime data directory is missing or is a symbolic link.")
    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("MCP runtime manifest is missing or is a symbolic link.")
    manifest = _load_strict_utf8_json_for_bundle(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("MCP runtime manifest must contain a JSON object.")
    if manifest.get("report_type") != "mcp_runtime_data_bundle":
        raise ValueError("MCP runtime manifest has the wrong report_type.")
    if manifest.get("synthetic_runtime") is not False:
        raise ValueError("Synthetic runtime data cannot satisfy an approved MCP bundle.")
    if manifest.get("provenance") != "approved_runtime_bundle_export":
        raise ValueError("MCP runtime data has invalid provenance.")
    if int(manifest.get("record_count") or 0) <= 0:
        raise ValueError("MCP runtime data does not contain approved vector records.")
    document_ids = manifest.get("document_ids")
    if not isinstance(document_ids, list) or not all(
        str(document_id or "").strip() for document_id in document_ids
    ):
        raise ValueError("MCP runtime manifest does not identify its source documents.")

    expected_corpus_hash = str(expected_logical_corpus_sha256 or "").strip().lower()
    actual_corpus_hash = str(manifest.get("logical_corpus_sha256") or "").strip().lower()
    if expected_corpus_hash and actual_corpus_hash != expected_corpus_hash:
        raise ValueError("MCP runtime logical corpus fingerprint is stale.")

    reuse = manifest.get("runtime_data_reuse")
    if not isinstance(reuse, dict) or reuse.get("schema_version") != RUNTIME_DATA_REUSE_SCHEMA_VERSION:
        raise ValueError("MCP runtime reuse metadata is missing or invalid.")
    expected_manifest_sha256 = str(reuse.get("manifest_sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256):
        raise ValueError("MCP runtime manifest fingerprint is missing or invalid.")
    if _runtime_manifest_content_sha256(manifest) != expected_manifest_sha256:
        raise ValueError("MCP runtime manifest content does not match its fingerprint.")

    expected_file_sha256 = reuse.get("file_sha256")
    if not isinstance(expected_file_sha256, dict) or not expected_file_sha256:
        raise ValueError("MCP runtime file fingerprints are missing.")
    normalized_file_sha256 = {
        str(relative_path): str(digest).strip().lower()
        for relative_path, digest in expected_file_sha256.items()
    }
    if any(
        not relative_path
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        for relative_path, digest in normalized_file_sha256.items()
    ):
        raise ValueError("MCP runtime file fingerprints are invalid.")
    if _runtime_data_file_sha256(runtime_data_dir) != normalized_file_sha256:
        raise ValueError("MCP runtime data files do not match the sealed manifest.")
    _validate_runtime_data_bundle_consistency(runtime_data_dir)

    return manifest


def _runtime_manifest_reuse_input_sha256(manifest: dict[str, Any]) -> str | None:
    reuse = manifest.get("runtime_data_reuse")
    if not isinstance(reuse, dict):
        return None
    if reuse.get("schema_version") != RUNTIME_DATA_REUSE_SCHEMA_VERSION:
        return None
    value = str(reuse.get("input_sha256") or "").strip().lower()
    return value if re.fullmatch(r"[a-f0-9]{64}", value) else None


def _iter_strict_jsonl_for_runtime_reuse(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as handle:
        if handle.read(len(UTF8_BOM)) == UTF8_BOM:
            raise ValueError(f"Generated runtime JSONL must be UTF-8 without BOM: {path}")
        handle.seek(0)
        for line_number, raw_line in enumerate(handle, start=1):
            try:
                line = raw_line.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"Generated runtime JSONL must be strict UTF-8: {path}:{line_number}"
                ) from exc
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_bundle_json_keys,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"Generated runtime JSONL is invalid at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"Generated runtime JSONL must contain objects: {path}:{line_number}"
                )
            yield value


def _strict_jsonl_matches_runtime_reuse(
    path: Path,
    expected_records: list[dict[str, Any]],
) -> bool:
    with closing(_iter_strict_jsonl_for_runtime_reuse(path)) as actual_records:
        for expected in expected_records:
            try:
                actual = next(actual_records)
            except StopIteration:
                return False
            if actual != expected:
                return False
        try:
            next(actual_records)
        except StopIteration:
            return True
        return False


def _validate_reusable_runtime_data_bundle(
    runtime_data_dir: Path,
    *,
    final_runtime_data_dir: Path,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    """Return a reusable manifest only after content and security projection checks."""

    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Reusable MCP runtime manifest cannot be a symbolic link.")
    manifest = _load_strict_utf8_json_for_bundle(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("Reusable MCP runtime manifest must contain a JSON object.")

    expected_input_sha256 = str(prepared["input_sha256"])
    if _runtime_manifest_reuse_input_sha256(manifest) != expected_input_sha256:
        raise ValueError("MCP runtime reuse input fingerprint does not match approved source inputs.")
    reuse = manifest.get("runtime_data_reuse")
    assert isinstance(reuse, dict)
    expected_manifest_sha256 = str(reuse.get("manifest_sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256):
        raise ValueError("MCP runtime reuse manifest fingerprint is missing or invalid.")
    if _runtime_manifest_content_sha256(manifest) != expected_manifest_sha256:
        raise ValueError("MCP runtime manifest content does not match its reuse fingerprint.")

    identity = prepared["manifest_identity"]
    for field, expected in identity.items():
        if manifest.get(field) != expected:
            raise ValueError(f"MCP runtime manifest field {field} does not match the requested source scope.")
    if manifest.get("report_type") != "mcp_runtime_data_bundle":
        raise ValueError("Reusable MCP runtime manifest has the wrong report_type.")
    if manifest.get("synthetic_runtime") is not False:
        raise ValueError("Synthetic runtime data cannot satisfy an approved-source reuse request.")
    if manifest.get("provenance") != "approved_runtime_bundle_export":
        raise ValueError("Reusable MCP runtime data has invalid provenance.")
    if manifest.get("source_data_dir") is not None:
        raise ValueError("Reusable MCP runtime data leaks a source data path.")
    if manifest.get("runtime_data_dir") != str(final_runtime_data_dir):
        raise ValueError("Reusable MCP runtime data paths no longer match the output directory.")
    expected_source_projection = {
        "source_data_provenance": "approved_local_export",
        "record_count": len(prepared["records"]),
        "chunk_count": int(prepared["total_chunks"]),
        "recommended_smoke_query": prepared["recommended_smoke_query"],
        "approval_record_count": len(prepared["approval_records"]),
        "indexing_job_count": len(prepared["indexing_jobs"]),
        "kordoc_table_parser_summary": prepared["kordoc_table_parser_summary"],
        "source_metadata_summary": prepared["source_metadata_summary"],
        "bm25_index_status": "ready",
        "hierarchical_index_status": "ready",
        "rebuild_fingerprint_schema_version": REBUILD_FINGERPRINT_SCHEMA_VERSION,
    }
    for field, expected in expected_source_projection.items():
        if manifest.get(field) != expected:
            raise ValueError(f"Reusable MCP runtime manifest source field {field} is stale.")

    expected_file_sha256 = reuse.get("file_sha256")
    if not isinstance(expected_file_sha256, dict) or not expected_file_sha256:
        raise ValueError("MCP runtime reuse file digests are missing.")
    normalized_file_sha256 = {
        str(relative_path): str(digest).strip().lower()
        for relative_path, digest in expected_file_sha256.items()
    }
    if any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in normalized_file_sha256.values()):
        raise ValueError("MCP runtime reuse file digests are invalid.")
    if _runtime_data_file_sha256(runtime_data_dir) != normalized_file_sha256:
        raise ValueError("MCP runtime data files do not match the validated reuse manifest.")
    _validate_runtime_data_bundle_consistency(runtime_data_dir)

    tenant_id = str(identity["tenant_id"])
    vector_dir = runtime_data_dir / "vector_db" / tenant_directory_key(tenant_id)
    vector_path = vector_dir / "approved_vectors.jsonl"
    if not _strict_jsonl_matches_runtime_reuse(vector_path, prepared["records"]):
        raise ValueError("Reusable approved vector records no longer match the approved source projection.")

    repository_dir = runtime_data_dir / "repository"
    repository_manifest = _load_strict_utf8_json_for_bundle(repository_dir / "manifest.json")
    if repository_manifest != prepared["repository_manifest"]:
        raise ValueError("Reusable repository manifest no longer matches the approved source projection.")
    for current_document_id, chunks in prepared["chunks_by_document"].items():
        chunk_path = repository_dir / f"{current_document_id}_chunks.json"
        if _load_strict_utf8_json_for_bundle(chunk_path) != chunks:
            raise ValueError(
                f"Reusable approved chunks no longer match the source projection: {current_document_id}"
            )
    if not _strict_jsonl_matches_runtime_reuse(
        repository_dir / "journals" / "approvals.jsonl",
        prepared["approval_records"],
    ):
        raise ValueError("Reusable approval journal no longer matches the approved source projection.")
    if not _strict_jsonl_matches_runtime_reuse(
        repository_dir / "journals" / "indexing_jobs.jsonl",
        prepared["indexing_jobs"],
    ):
        raise ValueError("Reusable indexing journal no longer matches the approved source projection.")

    approval_snapshot = _load_strict_utf8_json_for_bundle(repository_dir / "approval_snapshot.json")
    if not isinstance(approval_snapshot, dict):
        raise ValueError("Reusable approval snapshot must contain a JSON object.")
    expected_snapshot_entries = _runtime_approval_snapshot_entries(prepared["records"])
    expected_snapshot_projection = {
        "report_type": "mcp_runtime_approval_snapshot",
        "schema_version": "mcp-runtime-approval-snapshot-v1",
        "tenant_id": tenant_id,
        "document_ids": prepared["document_ids"],
        "record_count": len(prepared["records"]),
        "snapshot_count": len(expected_snapshot_entries),
        "entries": expected_snapshot_entries,
    }
    for field, expected in expected_snapshot_projection.items():
        if approval_snapshot.get(field) != expected:
            raise ValueError(f"Reusable approval snapshot field {field} is invalid.")
    runtime_settings = settings_for_mcp_project(
        data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=False,
    )
    runtime_repository = JsonRepository(runtime_settings)
    current_file_signatures = {
        key: (list(value) if value is not None else None)
        for key, value in routes_rag._runtime_approval_snapshot_file_signatures(runtime_repository).items()
    }
    if approval_snapshot.get("file_signatures") != current_file_signatures:
        raise ValueError("Reusable approval snapshot source signatures are stale.")

    omission_snapshot = _load_strict_utf8_json_for_bundle(
        repository_dir / OMISSION_DISPOSITION_SNAPSHOT_FILENAME
    )
    if not isinstance(omission_snapshot, dict):
        raise ValueError("Reusable omission disposition snapshot must contain a JSON object.")
    expected_omission_projection = prepared["omission_disposition_projection"]
    if any(
        omission_snapshot.get(field) != expected
        for field, expected in expected_omission_projection.items()
    ):
        raise ValueError("Reusable omission disposition snapshot is stale.")
    if _runtime_export_audit_timestamp(omission_snapshot.get("generated_at")) is None:
        raise ValueError("Reusable omission disposition snapshot generated_at is invalid.")
    _validate_runtime_omission_disposition_snapshot(runtime_data_dir, manifest)

    bm25_index = load_bm25_index(vector_dir / "bm25_index.json")
    expected_source_content_hashes = source_content_hashes(prepared["records"])
    if (
        bm25_index is None
        or bm25_index.tokenizer != tokenizer_name()
        or bm25_index.is_stale_for(prepared["records"])
        or bm25_index.source_content_hashes != expected_source_content_hashes
    ):
        raise ValueError("Reusable BM25 index is missing, incompatible, or stale.")
    if bm25_index.document_count != len(bm25_index.documents):
        raise ValueError("Reusable BM25 index document counts are inconsistent.")
    if int(manifest.get("bm25_document_count") or 0) != bm25_index.document_count:
        raise ValueError("Reusable BM25 index count does not match the runtime manifest.")
    source_records_by_id = {
        str(record.get("id") or ""): record
        for record in prepared["records"]
        if str(record.get("id") or "")
    }
    seen_bm25_ids: set[str] = set()
    for document in bm25_index.documents:
        record_id = str(document.get("id") or "")
        source_record = source_records_by_id.get(record_id)
        source_metadata = (
            source_record.get("metadata")
            if isinstance(source_record, dict) and isinstance(source_record.get("metadata"), dict)
            else {}
        )
        if (
            not record_id
            or record_id in seen_bm25_ids
            or source_record is None
            or str(document.get("document_id") or "")
            != str(source_record.get("document_id") or source_metadata.get("document_id") or "")
            or str(document.get("chunk_id") or "")
            != str(source_record.get("chunk_id") or source_metadata.get("chunk_id") or "")
            or str(document.get("content_hash") or "") != str(source_record.get("content_hash") or "")
        ):
            raise ValueError("Reusable BM25 index contains records outside the approved source projection.")
        seen_bm25_ids.add(record_id)

    hierarchy_path = hierarchical_index_path(runtime_data_dir)
    try:
        hierarchy = hierarchical_index_summary(hierarchy_path)
    except Exception as exc:
        raise ValueError("Reusable hierarchy index could not be read.") from exc
    manifest_hierarchy = manifest.get("hierarchical_index")
    if not isinstance(manifest_hierarchy, dict):
        raise ValueError("Reusable hierarchy manifest summary is missing.")
    try:
        expected_logical_corpus_sha256 = logical_corpus_sha256_for_records(
            prepared["records"],
            tenant_id=tenant_id,
            profile_id=(
                str(identity["profile_id"]).strip()
                if identity["profile_id"]
                else None
            ),
        )
    except ValueError as exc:
        raise ValueError(
            "Reusable hierarchy logical-corpus source projection is invalid."
        ) from exc
    manifest_logical_corpus_sha256 = manifest.get("logical_corpus_sha256")
    if (
        not isinstance(manifest_logical_corpus_sha256, str)
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            manifest_logical_corpus_sha256,
        )
        or manifest_logical_corpus_sha256
        != expected_logical_corpus_sha256
    ):
        raise ValueError(
            "Reusable hierarchy logical-corpus fingerprint does not match "
            "the approved source projection."
        )
    record_profile_ids: set[str] = set()
    for record in prepared["records"]:
        metadata = (
            record.get("metadata")
            if isinstance(record.get("metadata"), dict)
            else {}
        )
        scoped_values = {
            str(value or "").strip()
            for value in (record.get("profile_id"), metadata.get("profile_id"))
            if str(value or "").strip()
        }
        if len(scoped_values) != 1:
            raise ValueError(
                "Reusable hierarchy source records have a missing or conflicting profile scope."
            )
        record_profile_ids.update(scoped_values)
    if len(record_profile_ids) != 1:
        raise ValueError(
            "Reusable hierarchy source records do not share one profile scope."
        )
    expected_hierarchy_profile_id = next(iter(record_profile_ids))
    requested_profile_id = str(identity["profile_id"] or "").strip()
    if (
        requested_profile_id
        and expected_hierarchy_profile_id != requested_profile_id
    ):
        raise ValueError(
            "Reusable hierarchy source records do not match the requested profile scope."
        )
    expected_hierarchy = {
        "schema_version": HIERARCHICAL_INDEX_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "profile_id": expected_hierarchy_profile_id,
        "record_count": len(prepared["records"]),
        "source_content_hashes": expected_source_content_hashes,
        "logical_corpus_sha256": expected_logical_corpus_sha256,
        "regulation_count": int(manifest.get("regulation_count") or 0),
        "current_regulation_count": int(
            manifest_hierarchy.get("current_regulation_count") or 0
        ),
        "regulation_version_count": int(manifest.get("regulation_version_count") or 0),
        "toc_node_count": int(manifest.get("toc_node_count") or 0),
        "reference_edge_count": int(manifest_hierarchy.get("reference_edge_count") or 0),
        "resolved_reference_edge_count": int(
            manifest_hierarchy.get("resolved_reference_edge_count") or 0
        ),
        "unresolved_reference_edge_count": int(
            manifest_hierarchy.get("unresolved_reference_edge_count") or 0
        ),
        "ambiguous_reference_edge_count": int(
            manifest_hierarchy.get("ambiguous_reference_edge_count") or 0
        ),
        "reference_cycle_count": int(manifest_hierarchy.get("reference_cycle_count") or 0),
        "path": str(hierarchy_path),
    }
    if not isinstance(hierarchy, dict) or any(
        hierarchy.get(field) != expected
        for field, expected in expected_hierarchy.items()
    ):
        raise ValueError("Reusable hierarchy index does not match the requested tenant/profile corpus.")
    if expected_hierarchy["reference_edge_count"] != sum(
        expected_hierarchy[field]
        for field in (
            "resolved_reference_edge_count",
            "unresolved_reference_edge_count",
            "ambiguous_reference_edge_count",
        )
    ):
        raise ValueError("Reusable hierarchy reference-edge counts are inconsistent.")
    expected_manifest_hierarchy = {
        "schema_version": HIERARCHICAL_INDEX_SCHEMA_VERSION,
        "rebuild_fingerprint_schema_version": REBUILD_FINGERPRINT_SCHEMA_VERSION,
        "logical_corpus_sha256": expected_logical_corpus_sha256,
        **{
            field: expected_hierarchy[field]
            for field in (
                "record_count",
                "source_content_hashes",
                "regulation_count",
                "current_regulation_count",
                "regulation_version_count",
                "toc_node_count",
                "reference_edge_count",
                "resolved_reference_edge_count",
                "unresolved_reference_edge_count",
                "ambiguous_reference_edge_count",
                "reference_cycle_count",
                "path",
            )
        },
    }
    if any(
        manifest_hierarchy.get(field) != expected
        for field, expected in expected_manifest_hierarchy.items()
    ):
        raise ValueError("Reusable hierarchy manifest summary is inconsistent.")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Reusable MCP runtime manifest is missing file paths.")
    if manifest_hierarchy.get("sha256") != files.get("hierarchical_index_sha256"):
        raise ValueError("Reusable hierarchy fingerprints are inconsistent.")
    expected_result_files = [
        str(final_runtime_data_dir / "repository" / f"{current_document_id}_chunks.json")
        for current_document_id in prepared["document_ids"]
        if current_document_id in prepared["chunks_by_document"]
    ]
    expected_paths = {
        "vector_jsonl": str(
            final_runtime_data_dir
            / "vector_db"
            / tenant_directory_key(tenant_id)
            / "approved_vectors.jsonl"
        ),
        "bm25_index": str(
            final_runtime_data_dir
            / "vector_db"
            / tenant_directory_key(tenant_id)
            / "bm25_index.json"
        ),
        "hierarchical_index": str(hierarchical_index_path(final_runtime_data_dir)),
        "repository_manifest": str(final_runtime_data_dir / "repository" / "manifest.json"),
        "approval_journal": str(
            final_runtime_data_dir / "repository" / "journals" / "approvals.jsonl"
        ),
        "approval_snapshot": str(final_runtime_data_dir / "repository" / "approval_snapshot.json"),
        "omission_disposition_snapshot": str(
            final_runtime_data_dir / "repository" / OMISSION_DISPOSITION_SNAPSHOT_FILENAME
        ),
        "result_files": expected_result_files,
        "runtime_manifest": str(final_runtime_data_dir / "mcp_runtime_manifest.json"),
    }
    for field, expected in expected_paths.items():
        if files.get(field) != expected:
            raise ValueError(f"Reusable MCP runtime file path {field} is stale.")
    return manifest


def _runtime_swap_file_snapshot(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symbolic-link runtime swap snapshot: {path}")
    if not path.exists():
        return {"exists": False}
    if not path.is_file():
        raise RuntimeError(f"Runtime swap snapshot target is not a file: {path}")
    payload = path.read_bytes()
    return {
        "exists": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "base64": base64.b64encode(payload).decode("ascii"),
    }


def _restore_runtime_swap_file_snapshot(path: Path, snapshot: Any) -> None:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("exists"), bool):
        raise RuntimeError(f"Runtime swap snapshot is invalid for {path.name}.")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Refusing an unsafe runtime swap snapshot target: {path}")
    if not snapshot["exists"]:
        path.unlink(missing_ok=True)
        return
    encoded = snapshot.get("base64")
    expected_sha256 = str(snapshot.get("sha256") or "").strip().lower()
    if not isinstance(encoded, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise RuntimeError(f"Runtime swap snapshot payload is invalid for {path.name}.")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise RuntimeError(f"Runtime swap snapshot payload is invalid for {path.name}.") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError(f"Runtime swap snapshot hash mismatch for {path.name}.")
    _replace_file_bytes_atomically(path, payload)


def _runtime_swap_child_path(output_dir: Path, name: Any, pattern: re.Pattern[str]) -> Path:
    if not isinstance(name, str) or not pattern.fullmatch(name) or Path(name).name != name:
        raise RuntimeError(f"Runtime data swap contains an unsafe path name: {name!r}")
    child = output_dir / name
    if child.parent.resolve() != output_dir.resolve():
        raise RuntimeError(f"Runtime data swap path escaped the bundle directory: {name}")
    if child.is_symlink():
        raise RuntimeError(f"Refusing a symbolic-link runtime data swap path: {child}")
    return child


def _runtime_swap_artifacts(output_dir: Path, prefix: str, pattern: re.Pattern[str]) -> list[Path]:
    artifacts: list[Path] = []
    for path in output_dir.iterdir():
        if not path.name.startswith(prefix):
            continue
        if not pattern.fullmatch(path.name):
            raise RuntimeError(f"Unrecognized runtime data swap artifact requires review: {path.name}")
        if path.is_symlink():
            raise RuntimeError(f"Refusing a symbolic-link runtime data swap artifact: {path}")
        artifacts.append(path)
    return sorted(artifacts, key=lambda item: item.name)


def _remove_runtime_swap_artifact(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing to remove a symbolic-link runtime data swap artifact: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _runtime_swap_candidate_manifest(
    runtime_data_dir: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    if runtime_data_dir.is_symlink() or not runtime_data_dir.is_dir():
        raise RuntimeError(f"Recovered runtime data candidate is not a safe directory: {runtime_data_dir}")
    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError(f"Recovered runtime data candidate has no safe manifest: {runtime_data_dir}")
    if (
        expected_manifest_sha256 is not None
        and _sha256_file_content(manifest_path) != expected_manifest_sha256
    ):
        raise RuntimeError("Recovered runtime data candidate does not match the staged manifest.")
    manifest = _load_strict_utf8_json_for_bundle(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("report_type") != "mcp_runtime_data_bundle":
        raise RuntimeError("Recovered runtime data candidate has an invalid manifest.")
    _validate_runtime_data_bundle_consistency(runtime_data_dir)
    return manifest


def _runtime_swap_candidate_has_manifest(
    runtime_data_dir: Path,
    expected_manifest_sha256: str,
) -> bool:
    if runtime_data_dir.is_symlink():
        raise RuntimeError("Refusing a symbolic-link runtime data candidate during recovery.")
    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    if manifest_path.is_symlink():
        raise RuntimeError("Refusing a symbolic-link runtime manifest during recovery.")
    return (
        runtime_data_dir.is_dir()
        and manifest_path.is_file()
        and _sha256_file_content(manifest_path) == expected_manifest_sha256
    )


def _runtime_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _runtime_swap_status_matches_manifest(output_dir: Path, manifest: dict[str, Any]) -> bool:
    status_path = output_dir / SETUP_BUNDLE_FILES["bundle_status"]
    if status_path.is_symlink() or not status_path.is_file():
        return False
    try:
        status = _load_strict_utf8_json_for_bundle(status_path)
    except (OSError, ValueError):
        return False
    return (
        isinstance(status, dict)
        and status.get("runtime_data_ready") is True
        and str(status.get("runtime_fingerprint") or "") == _runtime_manifest_fingerprint(manifest)
    )


def _create_runtime_data_swap_marker(
    *,
    output_dir: Path,
    runtime_data_dir: Path,
    staging_dir: Path,
    backup_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    if runtime_data_dir.is_symlink():
        raise RuntimeError("Runtime data directory cannot be a symbolic link.")
    staging_dir = _runtime_swap_child_path(output_dir, staging_dir.name, RUNTIME_DATA_STAGE_NAME)
    backup_dir = _runtime_swap_child_path(output_dir, backup_dir.name, RUNTIME_DATA_BACKUP_NAME)
    staged_manifest_path = staging_dir / "mcp_runtime_manifest.json"
    if staged_manifest_path.is_symlink() or not staged_manifest_path.is_file():
        raise RuntimeError("Staged MCP runtime data has no safe manifest for swap recovery.")
    marker_path = output_dir / RUNTIME_DATA_SWAP_MARKER_FILENAME
    if marker_path.is_symlink() or marker_path.exists():
        raise RuntimeError("An unresolved MCP runtime data swap marker already exists.")
    payload = {
        "schema_version": RUNTIME_DATA_SWAP_SCHEMA_VERSION,
        "transaction_id": uuid4().hex,
        "phase": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_data_name": "data",
        "staging_name": staging_dir.name,
        "backup_name": backup_dir.name,
        "prior_data_exists": runtime_data_dir.exists(),
        "staged_manifest_sha256": _sha256_file_content(staged_manifest_path),
        "status_snapshot": _runtime_swap_file_snapshot(
            output_dir / SETUP_BUNDLE_FILES["bundle_status"]
        ),
        "stale_report_snapshots": {
            filename: _runtime_swap_file_snapshot(output_dir / filename)
            for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES
        },
    }
    _write_json_utf8_no_bom(marker_path, payload)
    return marker_path, payload


def _update_runtime_data_swap_marker(
    marker_path: Path,
    marker: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    if phase not in {"prepared", "backup_created", "data_promoted", "committed"}:
        raise ValueError(f"Unsupported runtime data swap phase: {phase}")
    updated = {**marker, "phase": phase, "updated_at": datetime.now(timezone.utc).isoformat()}
    _write_json_utf8_no_bom(marker_path, updated)
    return updated


def _load_runtime_data_swap_marker(output_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    marker_path = output_dir / RUNTIME_DATA_SWAP_MARKER_FILENAME
    if marker_path.is_symlink():
        raise RuntimeError("Refusing a symbolic-link MCP runtime data swap marker.")
    if not marker_path.exists():
        return None
    if not marker_path.is_file():
        raise RuntimeError("MCP runtime data swap marker is not a file.")
    marker = _load_strict_utf8_json_for_bundle(marker_path)
    if not isinstance(marker, dict):
        raise RuntimeError("MCP runtime data swap marker must contain an object.")
    if marker.get("schema_version") != RUNTIME_DATA_SWAP_SCHEMA_VERSION:
        raise RuntimeError("MCP runtime data swap marker has an unsupported schema.")
    if not re.fullmatch(r"[a-f0-9]{32}", str(marker.get("transaction_id") or "")):
        raise RuntimeError("MCP runtime data swap marker has an invalid transaction id.")
    if marker.get("phase") not in {"prepared", "backup_created", "data_promoted", "committed"}:
        raise RuntimeError("MCP runtime data swap marker has an invalid phase.")
    if marker.get("runtime_data_name") != "data":
        raise RuntimeError("MCP runtime data swap marker targets an unexpected directory.")
    if not isinstance(marker.get("prior_data_exists"), bool):
        raise RuntimeError("MCP runtime data swap marker has invalid prior-data state.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(marker.get("staged_manifest_sha256") or "")):
        raise RuntimeError("MCP runtime data swap marker has an invalid staged manifest hash.")
    _runtime_swap_child_path(output_dir, marker.get("staging_name"), RUNTIME_DATA_STAGE_NAME)
    _runtime_swap_child_path(output_dir, marker.get("backup_name"), RUNTIME_DATA_BACKUP_NAME)
    return marker_path, marker


def _restore_runtime_swap_auxiliary_files(output_dir: Path, marker: dict[str, Any]) -> None:
    snapshots = marker.get("stale_report_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(STALE_BUNDLE_STATUS_REPORT_FILENAMES):
        raise RuntimeError("MCP runtime data swap report snapshots are incomplete.")
    for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES:
        _restore_runtime_swap_file_snapshot(output_dir / filename, snapshots[filename])
    _restore_runtime_swap_file_snapshot(
        output_dir / SETUP_BUNDLE_FILES["bundle_status"],
        marker.get("status_snapshot"),
    )


def _recover_marked_runtime_data_swap(
    output_dir: Path,
    marker_path: Path,
    marker: dict[str, Any],
) -> str:
    runtime_data_dir = output_dir / "data"
    if runtime_data_dir.is_symlink():
        raise RuntimeError("Refusing a symbolic-link runtime data directory during recovery.")
    staging_dir = _runtime_swap_child_path(
        output_dir,
        marker["staging_name"],
        RUNTIME_DATA_STAGE_NAME,
    )
    backup_dir = _runtime_swap_child_path(
        output_dir,
        marker["backup_name"],
        RUNTIME_DATA_BACKUP_NAME,
    )
    stages = _runtime_swap_artifacts(output_dir, ".data-stage-", RUNTIME_DATA_STAGE_NAME)
    backups = _runtime_swap_artifacts(output_dir, ".data-backup-", RUNTIME_DATA_BACKUP_NAME)
    unexpected = [
        path.name
        for path in [*stages, *backups]
        if path not in {staging_dir, backup_dir}
    ]
    if unexpected:
        raise RuntimeError(
            "Ambiguous MCP runtime data swap artifacts require manual review: "
            + ", ".join(sorted(unexpected))
        )

    expected_manifest_sha256 = str(marker["staged_manifest_sha256"])
    phase = str(marker["phase"])
    prior_data_exists = bool(marker["prior_data_exists"])
    if phase == "committed":
        _runtime_swap_candidate_manifest(
            runtime_data_dir,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        _remove_runtime_swap_artifact(staging_dir)
        _remove_runtime_swap_artifact(backup_dir)
        marker_path.unlink()
        return "completed_committed_swap"

    if prior_data_exists:
        if backup_dir.exists():
            _remove_runtime_swap_artifact(runtime_data_dir)
            os.replace(backup_dir, runtime_data_dir)
        elif not runtime_data_dir.exists():
            raise RuntimeError(
                "Interrupted MCP runtime swap lost both the final data directory and its recorded backup."
            )
        elif phase != "prepared" and _runtime_swap_candidate_has_manifest(
            runtime_data_dir,
            expected_manifest_sha256,
        ):
            raise RuntimeError(
                "Interrupted MCP runtime swap cannot roll back because its recorded backup is missing; "
                "the promoted data was preserved for manual review."
            )
        _restore_runtime_swap_auxiliary_files(output_dir, marker)
        _remove_runtime_swap_artifact(staging_dir)
        marker_path.unlink()
        return "rolled_back_to_prior_data"

    if runtime_data_dir.exists() and staging_dir.exists():
        raise RuntimeError(
            "Interrupted first-time MCP runtime swap has both staged and promoted data; recovery is ambiguous."
        )
    candidate = runtime_data_dir if runtime_data_dir.exists() else staging_dir
    manifest = _runtime_swap_candidate_manifest(
        candidate,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if candidate == staging_dir:
        os.replace(staging_dir, runtime_data_dir)
    _clear_stale_bundle_status_reports(output_dir)
    _write_bundle_status(output_dir, runtime_manifest=manifest)
    _remove_runtime_swap_artifact(backup_dir)
    marker_path.unlink()
    return "completed_first_runtime_swap"


def _recover_legacy_runtime_data_swap(output_dir: Path) -> str | None:
    """Recover pre-marker swap artifacts without guessing among backups."""

    runtime_data_dir = output_dir / "data"
    if runtime_data_dir.is_symlink():
        raise RuntimeError("Refusing a symbolic-link runtime data directory during recovery.")
    stages = _runtime_swap_artifacts(output_dir, ".data-stage-", RUNTIME_DATA_STAGE_NAME)
    backups = _runtime_swap_artifacts(output_dir, ".data-backup-", RUNTIME_DATA_BACKUP_NAME)
    if len(backups) > 1:
        raise RuntimeError(
            "Ambiguous legacy MCP runtime backups require manual review: "
            + ", ".join(path.name for path in backups)
        )
    if backups:
        backup_dir = backups[0]
        if not runtime_data_dir.exists():
            os.replace(backup_dir, runtime_data_dir)
            for stage in stages:
                _remove_runtime_swap_artifact(stage)
            return "restored_unique_legacy_backup"
        manifest = _runtime_swap_candidate_manifest(runtime_data_dir)
        if not _runtime_swap_status_matches_manifest(output_dir, manifest):
            raise RuntimeError(
                "Legacy MCP runtime backup is ambiguous because bundle status does not identify "
                "the current data directory."
            )
        _remove_runtime_swap_artifact(backup_dir)
        for stage in stages:
            _remove_runtime_swap_artifact(stage)
        return "accepted_status_verified_legacy_data"
    if not stages:
        return None
    if runtime_data_dir.exists():
        for stage in stages:
            _remove_runtime_swap_artifact(stage)
        return "removed_abandoned_legacy_staging"

    valid_stages: list[tuple[Path, dict[str, Any]]] = []
    for stage in stages:
        try:
            manifest = _runtime_swap_candidate_manifest(stage)
        except (OSError, RuntimeError, ValueError):
            _remove_runtime_swap_artifact(stage)
        else:
            valid_stages.append((stage, manifest))
    if len(valid_stages) > 1:
        raise RuntimeError(
            "Ambiguous legacy MCP runtime staging directories require manual review: "
            + ", ".join(path.name for path, _manifest in valid_stages)
        )
    if not valid_stages:
        return "removed_incomplete_legacy_staging"
    stage, manifest = valid_stages[0]
    os.replace(stage, runtime_data_dir)
    _clear_stale_bundle_status_reports(output_dir)
    _write_bundle_status(output_dir, runtime_manifest=manifest)
    return "promoted_unique_legacy_staging"


def _recover_interrupted_runtime_data_swap(output_dir: Path) -> str | None:
    """Resolve one interrupted swap or fail closed when backups are ambiguous."""

    output_dir = output_dir.resolve()
    if output_dir == Path(output_dir.anchor):
        raise ValueError("MCP runtime swap recovery cannot target a filesystem root.")
    marker_state = _load_runtime_data_swap_marker(output_dir)
    if marker_state is not None:
        marker_path, marker = marker_state
        return _recover_marked_runtime_data_swap(output_dir, marker_path, marker)
    return _recover_legacy_runtime_data_swap(output_dir)


@_guard_local_mcp_materialization
def write_mcp_runtime_data_bundle(
    *,
    source_data_dir: str | Path,
    out_dir: str | Path,
    tenant_id: str = "default",
    profile_id: str | None = None,
    document_id: str | None = None,
    document_ids: list[str] | None = None,
    scope: str | None = None,
    tenant_storage_isolation: bool | None = None,
    actor: str | None = None,
    role: str | None = None,
    department_ids: list[str] | None = None,
    require_kordoc_table_parser: bool = True,
    require_source_metadata: bool = True,
    progress_callback: Callable[[int, str, int | None, int | None], None] | None = None,
) -> dict[str, Any]:
    """Build runtime data in staging, then atomically commit data and status."""

    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_data_dir = output_dir / "data"
    if runtime_data_dir.resolve() == Path(source_data_dir).resolve():
        raise ValueError("Runtime bundle output data dir must not be the same as the source data dir.")
    _recover_interrupted_runtime_data_swap(output_dir)
    _assert_no_active_bundle_installation(output_dir)
    staging_dir = output_dir / f".data-stage-{uuid4().hex}"
    backup_dir = output_dir / f".data-backup-{uuid4().hex}"
    swap_marker_path = output_dir / RUNTIME_DATA_SWAP_MARKER_FILENAME
    status_path = output_dir / SETUP_BUNDLE_FILES["bundle_status"]
    prior_status_exists = status_path.is_file()
    prior_status_bytes = status_path.read_bytes() if prior_status_exists else None
    stale_report_snapshots = {
        output_dir / filename: (output_dir / filename).read_bytes()
        for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES
        if (output_dir / filename).is_file()
    }
    prepared_runtime_inputs: dict[str, Any] | None = None
    existing_manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    existing_manifest = (
        {}
        if runtime_data_dir.is_symlink() or existing_manifest_path.is_symlink()
        else _read_runtime_manifest(runtime_data_dir)
    )
    existing_input_sha256 = _runtime_manifest_reuse_input_sha256(existing_manifest)
    if existing_input_sha256:
        prepared_runtime_inputs = _prepare_mcp_runtime_data_bundle_inputs(
            source_data_dir=source_data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            document_id=document_id,
            document_ids=document_ids,
            scope=scope,
            tenant_storage_isolation=tenant_storage_isolation,
            actor=actor,
            role=role,
            department_ids=department_ids,
            require_kordoc_table_parser=require_kordoc_table_parser,
            require_source_metadata=require_source_metadata,
            progress_callback=None,
        )
        if existing_input_sha256 == prepared_runtime_inputs["input_sha256"]:
            try:
                reused_manifest = _validate_reusable_runtime_data_bundle(
                    runtime_data_dir,
                    final_runtime_data_dir=runtime_data_dir,
                    prepared=prepared_runtime_inputs,
                )
            except Exception:
                # Reuse is only an optimization. Any candidate-read or
                # validation failure falls back to the normal staged rebuild;
                # current-source approval validation already completed above
                # and is deliberately outside this exception boundary.
                pass
            else:
                try:
                    _clear_stale_bundle_status_reports(output_dir)
                    _write_bundle_status(output_dir, runtime_manifest=reused_manifest)
                except BaseException:
                    for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES:
                        (output_dir / filename).unlink(missing_ok=True)
                    for report_path, report_bytes in stale_report_snapshots.items():
                        _replace_file_bytes_atomically(report_path, report_bytes)
                    if prior_status_exists and prior_status_bytes is not None:
                        _replace_file_bytes_atomically(status_path, prior_status_bytes)
                    elif status_path.exists():
                        status_path.unlink()
                    raise
                _report_runtime_progress(
                    progress_callback,
                    100,
                    "기존 MCP 런타임 데이터 검증 및 재사용 완료",
                    int(reused_manifest.get("record_count") or 0),
                    int(reused_manifest.get("record_count") or 0),
                )
                return reused_manifest

    manifest: dict[str, Any] | None = None
    swap_marker: dict[str, Any] | None = None
    data_swapped = False
    transaction_complete = False
    try:
        manifest = _write_mcp_runtime_data_bundle_uncommitted(
            source_data_dir=source_data_dir,
            out_dir=output_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            document_id=document_id,
            document_ids=document_ids,
            scope=scope,
            tenant_storage_isolation=tenant_storage_isolation,
            actor=actor,
            role=role,
            department_ids=department_ids,
            require_kordoc_table_parser=require_kordoc_table_parser,
            require_source_metadata=require_source_metadata,
            progress_callback=progress_callback,
            _runtime_data_dir=staging_dir,
            _write_status=False,
            _prepared_runtime_inputs=prepared_runtime_inputs,
        )
        staged_manifest = _read_runtime_manifest(staging_dir)
        if not staged_manifest or staged_manifest != manifest:
            raise RuntimeError("Staged MCP runtime manifest did not pass commit validation.")
        swap_marker_path, swap_marker = _create_runtime_data_swap_marker(
            output_dir=output_dir,
            runtime_data_dir=runtime_data_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
        )

        if prior_status_exists:
            try:
                refresh_status = json.loads((prior_status_bytes or b"").decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Existing bundle status is unreadable; runtime refresh was not committed.") from exc
            if not isinstance(refresh_status, dict):
                raise RuntimeError("Existing bundle status is invalid; runtime refresh was not committed.")
            refresh_status.update(
                {
                    "installation_state": "runtime_refresh_in_progress",
                    "connection_state": "pending_runtime_refresh",
                    "process_started": False,
                    "mcp_initialized": False,
                    "tools_discovered": False,
                    "installed_config_transport_verified": False,
                    "installed_config_transport_runtime_fingerprint": None,
                    "generated_client_configs_transport_verified": False,
                    "claude_code_transport_verified": False,
                    "claude_code_transport_runtime_fingerprint": None,
                    "claude_code_conversation_verified": False,
                    "direct_stdio_verified": False,
                    "transport_end_to_end_verified": False,
                    "claude_desktop_config_transport_verified": False,
                    "claude_desktop_config_transport_runtime_fingerprint": None,
                    "claude_desktop_loader_observed": False,
                    "claude_desktop_loader_verified": False,
                    "claude_desktop_conversation_verified": False,
                    "fresh_codex_app_server_inventory_verified": False,
                    "fresh_codex_app_server_runtime_fingerprint": None,
                    "desktop_app_server_loader_verified": False,
                    "desktop_app_server_tool_count": 0,
                    "desktop_app_server_tool_names": [],
                    "desktop_app_server_server_info": None,
                    "desktop_app_server_error": "runtime_refresh_in_progress",
                    "desktop_tool_scan_verified": False,
                    "conversation_attachment_verified": False,
                    "conversation_attachment_unverified": True,
                    "tool_scan_unverified": True,
                    "end_to_end_verified": False,
                }
            )
            _write_json_utf8_no_bom(status_path, refresh_status)

        if runtime_data_dir.exists():
            os.replace(runtime_data_dir, backup_dir)
            swap_marker = _update_runtime_data_swap_marker(
                swap_marker_path,
                swap_marker,
                "backup_created",
            )
        os.replace(staging_dir, runtime_data_dir)
        data_swapped = True
        swap_marker = _update_runtime_data_swap_marker(
            swap_marker_path,
            swap_marker,
            "data_promoted",
        )
        _clear_stale_bundle_status_reports(output_dir)
        _write_bundle_status(output_dir, runtime_manifest=manifest)
        swap_marker = _update_runtime_data_swap_marker(
            swap_marker_path,
            swap_marker,
            "committed",
        )
        transaction_complete = True
    except BaseException as exc:
        try:
            if swap_marker_path.exists():
                _recover_interrupted_runtime_data_swap(output_dir)
            else:
                if data_swapped and runtime_data_dir.exists():
                    _remove_runtime_swap_artifact(runtime_data_dir)
                if backup_dir.exists():
                    os.replace(backup_dir, runtime_data_dir)
                for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES:
                    report_path = output_dir / filename
                    report_path.unlink(missing_ok=True)
                for report_path, report_bytes in stale_report_snapshots.items():
                    _replace_file_bytes_atomically(report_path, report_bytes)
                if prior_status_exists and prior_status_bytes is not None:
                    _replace_file_bytes_atomically(status_path, prior_status_bytes)
                elif status_path.exists():
                    status_path.unlink()
        except BaseException as recovery_exc:
            if hasattr(exc, "add_note"):
                exc.add_note(f"MCP runtime data swap recovery also failed: {recovery_exc}")
        raise
    finally:
        if transaction_complete:
            _remove_runtime_swap_artifact(staging_dir)
            _remove_runtime_swap_artifact(backup_dir)
            swap_marker_path.unlink()
        elif not swap_marker_path.exists():
            _remove_runtime_swap_artifact(staging_dir)

    assert manifest is not None
    _report_runtime_progress(
        progress_callback,
        100,
        "기관 전체 MCP 데이터 생성 완료",
        int(manifest.get("record_count") or 0),
        int(manifest.get("record_count") or 0),
    )
    return manifest


def _report_runtime_progress(
    callback: Callable[[int, str, int | None, int | None], None] | None,
    percent: int,
    message: str,
    current: int | None = None,
    total: int | None = None,
) -> None:
    if callback is not None:
        callback(max(0, min(100, int(percent))), message, current, total)


def _runtime_approval_snapshot_entries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for record in sorted(
        records,
        key=lambda item: (
            str(item.get("document_id") or ""),
            str(item.get("chunk_id") or ""),
        ),
    ):
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        document_id = str(record.get("document_id") or metadata.get("document_id") or "")
        chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
        if not document_id or not chunk_id:
            continue
        entries.append(
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "approval_id": metadata.get("approval_id"),
                "approved_content_hash": metadata.get("approved_content_hash"),
                "security_level": str(metadata.get("security_level") or "").strip().lower(),
                "department_acl": sorted(routes_rag._department_acl_set(metadata.get("department_acl"))),
                "content_hash": str(record.get("content_hash") or ""),
            }
        )
    return entries


def _write_runtime_approval_snapshot_sidecar(
    *,
    runtime_data_dir: Path,
    tenant_id: str,
    document_ids: list[str],
    records: list[dict[str, Any]],
    auth: Any,
) -> Path:
    runtime_settings = settings_for_mcp_project(
        data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=False,
    )
    runtime_repository = JsonRepository(runtime_settings)
    entries = _runtime_approval_snapshot_entries(records)
    sidecar_path = runtime_repository.root / "approval_snapshot.json"
    payload = {
        "report_type": "mcp_runtime_approval_snapshot",
        "schema_version": "mcp-runtime-approval-snapshot-v1",
        "tenant_id": tenant_id,
        "document_ids": document_ids,
        "record_count": len(records),
        "snapshot_count": len(entries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_signatures": {
            key: (list(value) if value is not None else None)
            for key, value in routes_rag._runtime_approval_snapshot_file_signatures(runtime_repository).items()
        },
        "entries": entries,
    }
    sidecar_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sidecar_path


def _write_runtime_omission_disposition_sidecar(
    *,
    runtime_repository_dir: Path,
    projection: dict[str, Any],
) -> Path:
    sidecar_path = runtime_repository_dir / OMISSION_DISPOSITION_SNAPSHOT_FILENAME
    payload = copy.deepcopy(projection)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    sidecar_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return sidecar_path


def _runtime_approval_decision_projection(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project source approvals to the sealed runtime's minimal decision ledger.

    In particular, never copy human identities, free text, workstation paths,
    worklist/review evidence, nested chunk snapshots, security scan details, or
    review event history into a portable handoff.
    """

    projected: list[dict[str, Any]] = []
    for source in records:
        if not isinstance(source, dict):
            raise ValueError("Runtime approval decisions must originate from JSON objects.")
        chunk_ids = sorted(_runtime_export_audit_record_chunk_ids(source))
        approved_content_hashes = {
            chunk_id: approved_hash
            for chunk_id in chunk_ids
            if (approved_hash := _runtime_export_approval_record_hash(source, chunk_id))
        }
        record = {
            "approval_id": str(source.get("approval_id") or "").strip(),
            "tenant_id": str(source.get("tenant_id") or "").strip(),
            "document_id": str(source.get("document_id") or "").strip(),
            "approved_at": str(source.get("approved_at") or "").strip(),
            "chunk_ids": chunk_ids,
            "approved_content_hashes": approved_content_hashes,
        }
        _validate_runtime_approval_decision(record)
        projected.append(record)
    return sorted(
        projected,
        key=lambda record: (
            str(record["document_id"]),
            str(record["approved_at"]),
            str(record["approval_id"]),
        ),
    )


def _validate_runtime_approval_decision(record: dict[str, Any]) -> None:
    fields = set(record)
    if (
        not RUNTIME_APPROVAL_DECISION_REQUIRED_FIELDS.issubset(fields)
        or not fields.issubset(RUNTIME_APPROVAL_DECISION_FIELDS)
    ):
        raise ValueError("Runtime approval decision contains missing or unapproved fields.")
    if any(
        not isinstance(record.get(field), str) or not str(record.get(field) or "").strip()
        for field in ("approval_id", "tenant_id", "document_id", "approved_at")
    ):
        raise ValueError("Runtime approval decision identity or timestamp is missing.")
    if _runtime_export_audit_timestamp(record.get("approved_at")) is None:
        raise ValueError("Runtime approval decision approved_at is invalid.")
    chunk_ids = record.get("chunk_ids")
    approved_hashes = record.get("approved_content_hashes")
    if (
        not isinstance(chunk_ids, list)
        or any(not isinstance(chunk_id, str) or not chunk_id for chunk_id in chunk_ids)
        or chunk_ids != sorted(set(chunk_ids))
        or not isinstance(approved_hashes, dict)
        or any(
            not isinstance(chunk_id, str)
            or chunk_id not in chunk_ids
            or not isinstance(value, str)
            or not value.strip()
            for chunk_id, value in approved_hashes.items()
        )
        or set(approved_hashes) != set(chunk_ids)
    ):
        raise ValueError("Runtime approval decision chunk/hash binding is invalid.")


def _validate_runtime_approval_decision_journal(
    runtime_data_dir: Path,
    manifest: dict[str, Any],
) -> None:
    journal_path = runtime_data_dir / "repository" / "journals" / "approvals.jsonl"
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError("Runtime approval decision journal is missing or is a symbolic link.")
    try:
        records = list(_iter_strict_jsonl_for_runtime_reuse(journal_path))
    except (OSError, ValueError) as exc:
        raise ValueError("Runtime approval decision journal is invalid.") from exc
    if manifest.get("approval_record_count") != len(records):
        raise ValueError("Runtime approval decision journal count does not match the manifest.")
    tenant_id = str(manifest.get("tenant_id") or "").strip()
    document_ids = {
        str(value or "").strip()
        for value in manifest.get("document_ids") or []
        if str(value or "").strip()
    }
    for record in records:
        _validate_runtime_approval_decision(record)
        if record["tenant_id"] != tenant_id or record["document_id"] not in document_ids:
            raise ValueError("Runtime approval decision is outside the sealed tenant/document scope.")


def _prepare_runtime_data_export_dir(runtime_data_dir: Path, source_data_dir: str | Path) -> None:
    runtime_path = runtime_data_dir.resolve()
    source_path = Path(source_data_dir).resolve()
    if runtime_path == source_path:
        raise ValueError("Runtime bundle output data dir must not be the same as the source data dir.")
    if runtime_path == Path(runtime_path.anchor):
        raise ValueError("Runtime bundle output data dir must not be a filesystem root.")

    runtime_data_dir.mkdir(parents=True, exist_ok=True)
    for path in runtime_data_dir.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _remove_runtime_data_bundle(output_dir: Path) -> None:
    """Ensure source-only bundles cannot inherit runtime data from an older run."""
    runtime_data_dir = (output_dir / "data").resolve()
    if runtime_data_dir == Path(runtime_data_dir.anchor):
        raise ValueError("Runtime bundle output data dir must not be a filesystem root.")
    if runtime_data_dir.is_dir():
        shutil.rmtree(runtime_data_dir)
    elif runtime_data_dir.exists():
        runtime_data_dir.unlink()


def _runtime_visible_records_for_export(
    *,
    settings,
    auth,
    profile_id: str | None,
    document_id: str | None,
) -> list[dict[str, Any]]:
    requested_document_id = str(document_id or "").strip()
    source_records = routes_rag._load_local_vector_records(settings, auth)
    if requested_document_id:
        source_records = [
            record
            for record in source_records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            == requested_document_id
        ]
    repository = JsonRepository(settings)
    repository_cache = routes_rag._RagRequestRepositoryCache(repository)
    approval_snapshot = routes_rag._load_cached_approval_snapshot(repository, source_records, auth)
    request = routes_rag.RagSearchRequest(
        query="mcp runtime bundle export",
        top_k=1,
        document_id=requested_document_id or None,
        profile_id=profile_id,
        department_ids=list(auth.department_ids),
    )
    visible_records = [
        record
        for record in source_records
        if _record_has_mcp_export_metadata(record, auth=auth)
        and routes_rag._record_visible_to_request(
            record,
            request=request,
            auth=auth,
            repository=repository,
            repository_cache=repository_cache,
            approval_snapshot=approval_snapshot,
            requested_department_ids=frozenset(auth.department_ids),
        )
    ]
    # Institution bundles retain approved predecessor editions so the
    # hierarchy index can link each internal regulation across revisions.
    # Normal search still selects only the current regulation version. A
    # single-document bundle remains limited to its requested document.
    if not requested_document_id:
        allowed_document_ids = {
            current_document_id
            for current_document_id in {
                str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
                for record in visible_records
            }
            if current_document_id
            for document in [repository.get_document(current_document_id)]
            if document is not None
            and str(getattr(document, "regulation_status", "") or "").strip().casefold()
            in {"approved", "superseded"}
        }
        return [
            record
            for record in visible_records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            in allowed_document_ids
        ]

    # A single-document bundle is not a historical archive. Select lifecycle
    # state from the authoritative repository document rather than trusting
    # potentially stale vector metadata.
    visible_document_ids = {
        str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        for record in visible_records
    }
    catalog_documents = [
        document
        for current_document_id in visible_document_ids
        if current_document_id
        for document in [repository.get_document(current_document_id)]
        if document is not None
    ]
    latest_documents = filter_to_latest_active_versions(
        catalog_documents,
        include_legacy=False,
    )
    latest_document_ids = {str(document.document_id) for document in latest_documents}
    return [
        record
        for record in visible_records
        if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        in latest_document_ids
    ]


def _institution_runtime_export_document_ids(
    *,
    repository: JsonRepository,
    tenant_id: str,
    profile_id: str,
) -> set[str]:
    """Return institution documents that a complete runtime export must represent."""

    normalized_profile_id = str(profile_id or "").strip().casefold()
    if not normalized_profile_id:
        return set()
    return {
        str(document.document_id)
        for document in repository.list_documents()
        if resource_visible_to_tenant(document, tenant_id)
        and str(getattr(document, "profile_id", "") or "").strip().casefold()
        == normalized_profile_id
        and str(getattr(document, "regulation_status", "") or "").strip().casefold()
        in {"approved", "superseded"}
    }


def _runtime_export_fully_rejected_document_ids(
    *,
    repository: JsonRepository,
    document_ids: set[str],
    tenant_id: str,
    profile_id: str | None,
) -> set[str]:
    """Return selected documents whose active chunks are terminally rejected."""

    rejected_document_ids: set[str] = set()
    for document_id in sorted(str(value or "").strip() for value in document_ids):
        if not document_id:
            continue
        document = repository.get_document(document_id)
        if document is None:
            continue
        if not resource_visible_to_tenant(document, tenant_id):
            continue
        if (
            str(profile_id or "").strip()
            and str(getattr(document, "profile_id", "") or "").strip().casefold()
            != str(profile_id or "").strip().casefold()
        ):
            continue
        active_chunks = [
            chunk
            for chunk in repository.get_chunks(document_id)
            if str(getattr(chunk, "approval_status", "") or "").strip().lower()
            != "superseded"
        ]
        if active_chunks and all(
            str(getattr(chunk, "approval_status", "") or "").strip().lower()
            == "rejected"
            for chunk in active_chunks
        ) and _runtime_export_rejection_journal_covers_chunks(
            repository=repository,
            document_id=document_id,
            tenant_id=tenant_id,
            chunks=active_chunks,
        ):
            rejected_document_ids.add(document_id)
    return rejected_document_ids


def _runtime_export_rejection_journal_covers_chunks(
    *,
    repository: JsonRepository,
    document_id: str,
    tenant_id: str,
    chunks: list[Any],
    review_records: list[dict[str, Any]] | None = None,
    approval_records: list[dict[str, Any]] | None = None,
) -> bool:
    """Verify that every terminal rejection is bound to an append-only decision.

    A bare ``approval_status=rejected`` value is confidentiality-safe because
    it cannot expose content, but accepting it would let a damaged or manually
    edited repository silently omit a selected regulation.  Require the review
    journal's post-decision content hash for completeness and auditability.
    """

    expected_chunk_ids = {
        str(getattr(chunk, "chunk_id", "") or "").strip()
        for chunk in chunks
        if str(getattr(chunk, "chunk_id", "") or "").strip()
    }
    return len(expected_chunk_ids) == len(chunks) and (
        _runtime_export_rejection_journal_covered_chunk_ids(
            repository=repository,
            document_id=document_id,
            tenant_id=tenant_id,
            chunks=chunks,
            review_records=review_records,
            approval_records=approval_records,
        )
        == expected_chunk_ids
    )


def _runtime_export_rejection_journal_covered_chunk_ids(
    *,
    repository: JsonRepository,
    document_id: str,
    tenant_id: str,
    chunks: list[Any],
    review_records: list[dict[str, Any]] | None = None,
    approval_records: list[dict[str, Any]] | None = None,
) -> set[str]:
    if not chunks:
        return set()
    expected_hashes = {
        str(getattr(chunk, "chunk_id", "") or "").strip(): approved_content_hash(chunk)
        for chunk in chunks
        if str(getattr(chunk, "chunk_id", "") or "").strip()
    }
    if len(expected_hashes) != len(chunks):
        return set()

    audit_events: dict[str, list[tuple[datetime, bool]]] = {
        chunk_id: [] for chunk_id in expected_hashes
    }
    ambiguous_chunk_ids: set[str] = set()
    current_review_records = (
        repository.list_review_journal_records(document_id)
        if review_records is None
        else review_records
    )
    for record in current_review_records:
        if str(record.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            continue
        record_chunk_ids = {
            str(value or "").strip()
            for value in record.get("chunk_ids") or []
            if str(value or "").strip()
        }.intersection(expected_hashes)
        if not record_chunk_ids:
            continue
        reviewed_at = _runtime_export_audit_timestamp(record.get("reviewed_at"))
        if reviewed_at is None:
            ambiguous_chunk_ids.update(record_chunk_ids)
            continue
        after_hashes = record.get("after_content_hashes")
        for chunk_id in record_chunk_ids:
            actual_hash = (
                str(after_hashes.get(chunk_id) or "").strip().lower()
                if isinstance(after_hashes, dict)
                else ""
            )
            valid_rejection = all(
                (
                    str(record.get("action") or "").strip().lower() == "reject",
                    str(record.get("status") or "").strip().lower() == "rejected",
                    bool(str(record.get("reason") or "").strip()),
                    bool(str(record.get("reviewed_by") or "").strip()),
                    bool(actual_hash),
                    bool(
                        actual_hash
                        and hmac.compare_digest(actual_hash, expected_hashes[chunk_id].lower())
                    ),
                )
            )
            audit_events[chunk_id].append((reviewed_at, valid_rejection))

    # A historical rejection must not authorize omission after the same chunk
    # was approved later. Both sources are append-only journals; mutable
    # manifest mirrors are deliberately excluded from this decision.
    current_approval_records = (
        repository.list_approval_journal_records(document_id)
        if approval_records is None
        else approval_records
    )
    for record in current_approval_records:
        if str(record.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            continue
        record_chunk_ids = {
            str(value or "").strip()
            for value in record.get("chunk_ids") or []
            if str(value or "").strip()
        }
        approved_chunks = record.get("approved_chunks")
        if isinstance(approved_chunks, list):
            record_chunk_ids.update(
                str(item.get("chunk_id") or "").strip()
                for item in approved_chunks
                if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
            )
        record_chunk_ids.intersection_update(expected_hashes)
        if not record_chunk_ids:
            continue
        approved_at = _runtime_export_audit_timestamp(record.get("approved_at"))
        if approved_at is None:
            ambiguous_chunk_ids.update(record_chunk_ids)
            continue
        for chunk_id in record_chunk_ids:
            audit_events[chunk_id].append((approved_at, False))

    covered_chunk_ids: set[str] = set()
    for chunk_id, events in audit_events.items():
        if chunk_id in ambiguous_chunk_ids or not events:
            continue
        latest_at = max(event[0] for event in events)
        latest_events = [valid_rejection for event_at, valid_rejection in events if event_at == latest_at]
        if len(latest_events) == 1 and latest_events[0]:
            covered_chunk_ids.add(chunk_id)
    return covered_chunk_ids


def _runtime_export_audit_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime_export_supersession_journal_covered_chunk_ids(
    *,
    tenant_id: str,
    chunks: list[Any],
    all_chunk_ids: set[str],
    review_records: list[dict[str, Any]],
    approval_records: list[dict[str, Any]],
) -> set[str]:
    """Bind superseded chunks to the latest append-only split/merge decision."""

    expected_hashes = {
        str(getattr(chunk, "chunk_id", "") or "").strip(): approved_content_hash(chunk)
        for chunk in chunks
        if str(getattr(chunk, "chunk_id", "") or "").strip()
    }
    events: dict[str, list[tuple[datetime, bool]]] = {
        chunk_id: [] for chunk_id in expected_hashes
    }
    ambiguous: set[str] = set()
    for record in review_records:
        if str(record.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            continue
        affected = _runtime_export_audit_record_chunk_ids(record).intersection(expected_hashes)
        if not affected:
            continue
        reviewed_at = _runtime_export_audit_timestamp(record.get("reviewed_at"))
        if reviewed_at is None:
            ambiguous.update(affected)
            continue
        before_hashes = record.get("before_content_hashes")
        created_chunk_ids = {
            str(value or "").strip()
            for value in record.get("created_chunk_ids") or []
            if str(value or "").strip()
        }
        action = str(record.get("action") or "").strip().lower()
        for chunk_id in affected:
            recorded_hash = (
                str(before_hashes.get(chunk_id) or "").strip().lower()
                if isinstance(before_hashes, dict)
                else ""
            )
            valid_supersession = bool(
                action in {"split", "merge"}
                and created_chunk_ids
                and created_chunk_ids.issubset(all_chunk_ids)
                and recorded_hash
                and hmac.compare_digest(recorded_hash, expected_hashes[chunk_id].lower())
            )
            events[chunk_id].append((reviewed_at, valid_supersession))

    for record in approval_records:
        if str(record.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            continue
        affected = _runtime_export_audit_record_chunk_ids(record).intersection(expected_hashes)
        if not affected:
            continue
        approved_at = _runtime_export_audit_timestamp(record.get("approved_at"))
        if approved_at is None:
            ambiguous.update(affected)
            continue
        for chunk_id in affected:
            events[chunk_id].append((approved_at, False))

    covered: set[str] = set()
    for chunk_id, chunk_events in events.items():
        if chunk_id in ambiguous or not chunk_events:
            continue
        latest_at = max(timestamp for timestamp, _valid in chunk_events)
        latest = [valid for timestamp, valid in chunk_events if timestamp == latest_at]
        if len(latest) == 1 and latest[0]:
            covered.add(chunk_id)
    return covered


def _runtime_omission_disposition_projection(
    *,
    repository: JsonRepository,
    tenant_id: str,
    requested_document_ids: list[str],
    exported_records: list[dict[str, Any]],
    approval_records_by_document: dict[str, list[dict[str, Any]]],
    review_records_by_document: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Project only the sealed decision needed to account for every chunk.

    The source journals contain reviewer identities, notes, paths, reasons, and
    before/after maps.  None of those fields belong in a portable MCP bundle.
    This projection deliberately retains only the identity, timestamp, status,
    and content binding of the unique latest decision.
    """

    requested_ids = sorted(
        {
            str(document_id or "").strip()
            for document_id in requested_document_ids
            if str(document_id or "").strip()
        }
    )
    if not requested_ids:
        raise ValueError("MCP omission disposition snapshot has no requested documents.")

    exported_pairs: set[tuple[str, str]] = set()
    for record in exported_records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        pair = (
            str(record.get("document_id") or metadata.get("document_id") or "").strip(),
            str(record.get("chunk_id") or metadata.get("chunk_id") or "").strip(),
        )
        if not all(pair) or pair in exported_pairs:
            raise ValueError("MCP omission disposition snapshot found an invalid exported chunk identity.")
        exported_pairs.add(pair)

    entries: list[dict[str, Any]] = []
    source_pairs: set[tuple[str, str]] = set()
    for document_id in requested_ids:
        document = repository.get_document(document_id)
        if document is None or not resource_visible_to_tenant(document, tenant_id):
            raise ValueError(
                "MCP omission disposition snapshot cannot include a missing or cross-tenant document: "
                + document_id
            )
        chunks = list(repository.get_chunks(document_id))
        if not chunks:
            raise ValueError(
                "MCP omission disposition snapshot cannot account for a document without chunks: "
                + document_id
            )
        all_chunk_ids = {
            str(getattr(chunk, "chunk_id", "") or "").strip() for chunk in chunks
        }
        if "" in all_chunk_ids or len(all_chunk_ids) != len(chunks):
            raise ValueError(
                "MCP omission disposition snapshot found missing or duplicate chunk IDs for document: "
                + document_id
            )

        approval_records = approval_records_by_document.get(document_id, [])
        review_records = review_records_by_document.get(document_id, [])
        for chunk in sorted(chunks, key=lambda item: str(getattr(item, "chunk_id", "") or "")):
            chunk_id = str(getattr(chunk, "chunk_id", "") or "").strip()
            if str(getattr(chunk, "document_id", "") or "").strip() != document_id:
                raise ValueError(
                    "MCP omission disposition snapshot found a cross-document chunk: "
                    f"{document_id}/{chunk_id}"
                )
            pair = (document_id, chunk_id)
            source_pairs.add(pair)
            current_status = str(getattr(chunk, "approval_status", "") or "").strip().lower()
            current_hash = (
                str(getattr(chunk, "approved_content_hash", "") or "").strip().lower()
                if current_status == "approved"
                else approved_content_hash(chunk).strip().lower()
            )
            expected_disposition = {
                "approved": "exported",
                "rejected": "omitted_rejected",
                "superseded": "omitted_superseded",
            }.get(current_status)
            if expected_disposition is None:
                raise ValueError(
                    "MCP omission disposition snapshot found an unresolved current chunk status: "
                    f"{document_id}/{chunk_id}:{current_status or 'missing'}"
                )
            if (pair in exported_pairs) != (expected_disposition == "exported"):
                raise ValueError(
                    "MCP omission disposition snapshot classification does not match the exported corpus: "
                    f"{document_id}/{chunk_id}"
                )

            events: list[tuple[datetime, str | None, str]] = []
            for record in approval_records:
                if str(record.get("tenant_id") or "").strip() != tenant_id:
                    continue
                if chunk_id not in _runtime_export_audit_record_chunk_ids(record):
                    continue
                approved_at = _runtime_export_audit_timestamp(record.get("approved_at"))
                if approved_at is None:
                    raise ValueError(
                        "MCP omission disposition snapshot found a missing or malformed decision timestamp: "
                        f"{document_id}/{chunk_id}"
                    )
                decision_id = str(record.get("approval_id") or "").strip()
                record_hash = _runtime_export_approval_record_hash(record, chunk_id).strip().lower()
                current_approval_id = str(getattr(chunk, "approval_id", "") or "").strip()
                valid = bool(
                    decision_id
                    and current_approval_id
                    and decision_id == current_approval_id
                    and record_hash
                    and hmac.compare_digest(record_hash, current_hash)
                )
                events.append((approved_at, "exported" if valid else None, decision_id))

            for record in review_records:
                if str(record.get("tenant_id") or "").strip() != tenant_id:
                    continue
                if chunk_id not in _runtime_export_audit_record_chunk_ids(record):
                    continue
                reviewed_at = _runtime_export_audit_timestamp(record.get("reviewed_at"))
                if reviewed_at is None:
                    raise ValueError(
                        "MCP omission disposition snapshot found a missing or malformed decision timestamp: "
                        f"{document_id}/{chunk_id}"
                    )
                action = str(record.get("action") or "").strip().lower()
                decision_id = str(record.get("review_id") or "").strip()
                disposition: str | None = None
                if action == "reject":
                    after_hashes = record.get("after_content_hashes")
                    record_hash = (
                        str(after_hashes.get(chunk_id) or "").strip().lower()
                        if isinstance(after_hashes, dict)
                        else ""
                    )
                    if all(
                        (
                            decision_id,
                            str(record.get("status") or "").strip().lower() == "rejected",
                            str(record.get("reason") or "").strip(),
                            str(record.get("reviewed_by") or "").strip(),
                            record_hash,
                            record_hash and hmac.compare_digest(record_hash, current_hash),
                        )
                    ):
                        disposition = "omitted_rejected"
                elif action in {"split", "merge"}:
                    before_hashes = record.get("before_content_hashes")
                    record_hash = (
                        str(before_hashes.get(chunk_id) or "").strip().lower()
                        if isinstance(before_hashes, dict)
                        else ""
                    )
                    created_chunk_ids = {
                        str(value or "").strip()
                        for value in record.get("created_chunk_ids") or []
                        if str(value or "").strip()
                    }
                    if all(
                        (
                            decision_id,
                            created_chunk_ids,
                            created_chunk_ids.issubset(all_chunk_ids),
                            record_hash,
                            record_hash and hmac.compare_digest(record_hash, current_hash),
                        )
                    ):
                        disposition = "omitted_superseded"
                events.append((reviewed_at, disposition, decision_id))

            if not events:
                raise ValueError(
                    "MCP omission disposition snapshot found no decision for current chunk: "
                    f"{document_id}/{chunk_id}"
                )
            latest_at = max(timestamp for timestamp, _disposition, _decision_id in events)
            latest = [event for event in events if event[0] == latest_at]
            if len(latest) != 1:
                raise ValueError(
                    "MCP omission disposition snapshot found tied latest decisions: "
                    f"{document_id}/{chunk_id}"
                )
            _timestamp, disposition, decision_id = latest[0]
            if disposition != expected_disposition or not decision_id:
                raise ValueError(
                    "MCP omission disposition snapshot latest decision does not match current state: "
                    f"{document_id}/{chunk_id}"
                )
            entries.append(
                {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "content_hash": current_hash,
                    "latest_decision_id": decision_id,
                    "latest_decision_status": {
                        "exported": "approved",
                        "omitted_rejected": "rejected",
                        "omitted_superseded": "superseded",
                    }[disposition],
                    "latest_decision_at": latest_at.isoformat(),
                    "disposition": disposition,
                    "exported": disposition == "exported",
                    "requested": True,
                }
            )

    extra_exported_pairs = sorted(exported_pairs - source_pairs)
    if extra_exported_pairs:
        raise ValueError(
            "MCP omission disposition snapshot found exported chunks outside the requested documents: "
            + ", ".join(f"{document_id}/{chunk_id}" for document_id, chunk_id in extra_exported_pairs[:5])
        )
    return _runtime_omission_disposition_top_level(entries, requested_ids)


def _runtime_omission_disposition_top_level(
    entries: list[dict[str, Any]],
    requested_document_ids: list[str],
) -> dict[str, Any]:
    exported_entries = [entry for entry in entries if entry.get("disposition") == "exported"]
    omitted_entries = [entry for entry in entries if str(entry.get("disposition") or "").startswith("omitted_")]
    exported_document_ids = sorted({str(entry["document_id"]) for entry in exported_entries})
    omitted_document_ids = sorted({str(entry["document_id"]) for entry in omitted_entries})
    requested_chunk_ids = [str(entry["chunk_id"]) for entry in entries]
    exported_chunk_ids = [str(entry["chunk_id"]) for entry in exported_entries]
    omitted_chunk_ids = [str(entry["chunk_id"]) for entry in omitted_entries]
    return {
        "report_type": "mcp_runtime_omission_disposition_snapshot",
        "schema_version": OMISSION_DISPOSITION_SNAPSHOT_SCHEMA_VERSION,
        "tenant_id": str(entries[0]["tenant_id"]) if entries else "",
        "requested_document_ids": list(requested_document_ids),
        "exported_document_ids": exported_document_ids,
        "omitted_document_ids": omitted_document_ids,
        "requested_document_count": len(requested_document_ids),
        "exported_document_count": len(exported_document_ids),
        "omitted_document_count": len(omitted_document_ids),
        "requested_chunk_ids": requested_chunk_ids,
        "exported_chunk_ids": exported_chunk_ids,
        "omitted_chunk_ids": omitted_chunk_ids,
        "requested_chunk_count": len(requested_chunk_ids),
        "exported_chunk_count": len(exported_chunk_ids),
        "omitted_chunk_count": len(omitted_chunk_ids),
        "disposition_counts": {
            disposition: sum(entry.get("disposition") == disposition for entry in entries)
            for disposition in ("exported", "omitted_rejected", "omitted_superseded")
        },
        "entry_count": len(entries),
        "entries": entries,
    }


def _recommended_runtime_smoke_query(records: list[dict[str, Any]]) -> str:
    candidates: list[tuple[int, int, str]] = []
    for index, record in enumerate(records):
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        chunk_type = str(metadata.get("chunk_type") or record.get("chunk_type") or "").strip().lower()
        article_no = _first_smoke_query_value(
            metadata.get("article_no"),
            metadata.get("direct_article_no"),
            record.get("article_no"),
        )
        article_title = _first_smoke_query_value(
            metadata.get("article_title"),
            metadata.get("direct_article_title"),
            record.get("article_title"),
        )
        if not (article_no and article_title):
            parsed_no, parsed_title = _article_query_from_text(record.get("text"))
            article_no = article_no or parsed_no
            article_title = article_title or parsed_title
        if article_no and article_title and chunk_type in {"article", "paragraph", "item", "subitem", "clause"}:
            query = f"{article_no} {article_title}"
            score = 100
            if chunk_type == "article":
                score += 20
            if metadata.get("appendix_refs") or metadata.get("form_refs"):
                score += 35
            if any(term in article_title for term in ("시행일", "경과조치", "적용례")):
                score -= 45
            if len(query) > 30:
                score -= 10
            candidates.append((score, -index, query))
    if candidates:
        return max(candidates)[2]

    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        article_no = _first_smoke_query_value(
            metadata.get("article_no"),
            metadata.get("direct_article_no"),
            record.get("article_no"),
        )
        if article_no:
            return article_no
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        for field in ("regulation_title", "document_name"):
            value = _first_smoke_query_value(metadata.get(field), record.get(field))
            if value:
                return value
    return "규정"


def _first_smoke_query_value(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not _looks_like_broken_smoke_query(text):
            return text
    return ""


def _looks_like_broken_smoke_query(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if "\ufffd" in text:
        return True
    question_count = text.count("?")
    return question_count >= 2 and question_count >= max(2, len(text) // 4)


def _article_query_from_text(value: object) -> tuple[str, str]:
    text = str(value or "")
    match = re.search(r"(제\d+조(?:의\d+)?)\s*\(([^)\n]{1,80})\)", text)
    if not match:
        return "", ""
    article_no = _first_smoke_query_value(match.group(1))
    article_title = _first_smoke_query_value(match.group(2))
    return article_no, article_title


def _current_approved_chunks_for_runtime_export(
    *,
    repository: JsonRepository,
    document_id: str,
    visible_chunk_ids: set[str],
    records_by_chunk_id: dict[str, dict[str, Any]],
    tenant_id: str,
    approval_records: list[dict[str, Any]],
    review_records: list[dict[str, Any]],
) -> list[Any]:
    chunks_by_id = {str(chunk.chunk_id): chunk for chunk in repository.get_chunks(document_id)}
    missing = sorted(chunk_id for chunk_id in visible_chunk_ids if chunk_id and chunk_id not in chunks_by_id)
    if missing:
        sample = ", ".join(missing[:5])
        raise ValueError(f"MCP runtime export is stale: approved vector records reference missing chunks: {sample}")
    chunks: list[Any] = []
    invalid: list[str] = []
    for chunk_id in sorted(chunk_id for chunk_id in visible_chunk_ids if chunk_id):
        chunk = chunks_by_id[chunk_id]
        record = records_by_chunk_id.get(chunk_id) or {}
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        reason = _runtime_export_chunk_mismatch_reason(chunk, record, metadata)
        if reason:
            invalid.append(f"{chunk_id}:{reason}")
            continue
        if not _runtime_export_latest_decision_allows_approval(
            chunk=chunk,
            tenant_id=tenant_id,
            approval_records=approval_records,
            review_records=review_records,
        ):
            invalid.append(f"{chunk_id}:latest_audit_decision_not_current_approval")
            continue
        chunks.append(chunk)
    if invalid:
        sample = ", ".join(invalid[:5])
        raise ValueError(
            "MCP runtime export is stale: current repository chunks no longer match approved vector records. "
            f"{sample}. Reapprove and reindex before creating a handoff bundle."
        )
    return chunks


def _runtime_export_latest_decision_allows_approval(
    *,
    chunk: Any,
    tenant_id: str,
    approval_records: list[dict[str, Any]],
    review_records: list[dict[str, Any]],
) -> bool:
    """Require the latest append-only decision to match the current approval."""

    chunk_id = str(getattr(chunk, "chunk_id", "") or "").strip()
    current_approval_id = str(getattr(chunk, "approval_id", "") or "").strip()
    current_hash = str(getattr(chunk, "approved_content_hash", "") or "").strip().lower()
    if not chunk_id or not current_approval_id or not current_hash:
        return False

    events: list[tuple[datetime, bool]] = []
    for record in approval_records:
        if str(record.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            continue
        record_chunk_ids = _runtime_export_audit_record_chunk_ids(record)
        if chunk_id not in record_chunk_ids:
            continue
        approved_at = _runtime_export_audit_timestamp(record.get("approved_at"))
        if approved_at is None:
            return False
        record_hash = _runtime_export_approval_record_hash(record, chunk_id)
        matches_current = (
            str(record.get("approval_id") or "").strip() == current_approval_id
            and bool(record_hash)
            and hmac.compare_digest(record_hash.lower(), current_hash)
        )
        events.append((approved_at, matches_current))

    for record in review_records:
        if str(record.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            continue
        if chunk_id not in _runtime_export_audit_record_chunk_ids(record):
            continue
        reviewed_at = _runtime_export_audit_timestamp(record.get("reviewed_at"))
        if reviewed_at is None:
            return False
        events.append((reviewed_at, False))

    if not events:
        return False
    latest_at = max(event[0] for event in events)
    latest_events = [matches_current for event_at, matches_current in events if event_at == latest_at]
    return len(latest_events) == 1 and latest_events[0]


def _runtime_export_audit_record_chunk_ids(record: dict[str, Any]) -> set[str]:
    chunk_ids = {
        str(value or "").strip()
        for value in record.get("chunk_ids") or []
        if str(value or "").strip()
    }
    approved_chunks = record.get("approved_chunks")
    if isinstance(approved_chunks, list):
        chunk_ids.update(
            str(item.get("chunk_id") or "").strip()
            for item in approved_chunks
            if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
        )
    return chunk_ids


def _runtime_export_approval_record_hash(record: dict[str, Any], chunk_id: str) -> str:
    approved_hashes = record.get("approved_content_hashes")
    if isinstance(approved_hashes, dict):
        value = str(approved_hashes.get(chunk_id) or "").strip()
        if value:
            return value
    approved_chunks = record.get("approved_chunks")
    if isinstance(approved_chunks, list):
        for item in approved_chunks:
            if not isinstance(item, dict) or str(item.get("chunk_id") or "").strip() != chunk_id:
                continue
            return str(item.get("approved_content_hash") or "").strip()
    return ""


def _runtime_export_document_completeness_issue(
    *,
    repository: JsonRepository,
    document_id: str,
    visible_chunk_ids: set[str],
    tenant_id: str,
    approval_records: list[dict[str, Any]] | None = None,
    review_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return a fail-closed reason when runtime export would hide current chunks."""

    source_chunks = list(repository.get_chunks(document_id))
    superseded_chunks = [
        chunk
        for chunk in source_chunks
        if str(getattr(chunk, "approval_status", "") or "").strip().lower() == "superseded"
    ]
    active_chunks = [
        chunk
        for chunk in source_chunks
        if str(getattr(chunk, "approval_status", "") or "").strip().lower() != "superseded"
    ]
    approved_source_chunk_ids = {
        str(chunk.chunk_id)
        for chunk in active_chunks
        if str(getattr(chunk, "approval_status", "") or "").strip().lower() == "approved"
    }
    missing_approved_chunk_ids = sorted(
        chunk_id
        for chunk_id in approved_source_chunk_ids
        if chunk_id not in visible_chunk_ids
    )
    unresolved_chunks = [
        {
            "chunk_id": str(chunk.chunk_id),
            "approval_status": str(getattr(chunk, "approval_status", "") or "").strip().lower() or "missing",
        }
        for chunk in active_chunks
        if str(getattr(chunk, "approval_status", "") or "").strip().lower()
        not in {"approved", "rejected"}
    ]
    rejected_chunks = [
        chunk
        for chunk in active_chunks
        if str(getattr(chunk, "approval_status", "") or "").strip().lower() == "rejected"
    ]
    journal_covered_rejection_ids = _runtime_export_rejection_journal_covered_chunk_ids(
        repository=repository,
        document_id=document_id,
        tenant_id=tenant_id,
        chunks=rejected_chunks,
        approval_records=approval_records,
        review_records=review_records,
    )
    unaudited_rejection_chunk_ids = sorted(
        str(chunk.chunk_id)
        for chunk in rejected_chunks
        if str(chunk.chunk_id) not in journal_covered_rejection_ids
    )
    journal_covered_superseded_ids = _runtime_export_supersession_journal_covered_chunk_ids(
        tenant_id=tenant_id,
        chunks=superseded_chunks,
        all_chunk_ids={str(chunk.chunk_id) for chunk in source_chunks},
        review_records=review_records or [],
        approval_records=approval_records or [],
    )
    unaudited_superseded_chunk_ids = sorted(
        str(chunk.chunk_id)
        for chunk in superseded_chunks
        if str(chunk.chunk_id) not in journal_covered_superseded_ids
    )
    if (
        not missing_approved_chunk_ids
        and not unresolved_chunks
        and not unaudited_rejection_chunk_ids
        and not unaudited_superseded_chunk_ids
    ):
        return None
    return {
        "document_id": document_id,
        "active_chunk_count": len(active_chunks),
        "approved_source_chunk_count": len(approved_source_chunk_ids),
        "exported_record_count": len(visible_chunk_ids),
        "missing_approved_chunk_ids": missing_approved_chunk_ids,
        "unresolved_chunks": unresolved_chunks,
        "unaudited_rejection_chunk_ids": unaudited_rejection_chunk_ids,
        "unaudited_superseded_chunk_ids": unaudited_superseded_chunk_ids,
    }


def _runtime_export_chunk_mismatch_reason(chunk: Any, record: dict[str, Any], metadata: dict[str, Any]) -> str:
    if str(getattr(chunk, "approval_status", "") or "").strip().lower() != "approved":
        return "chunk_not_approved"
    if str(getattr(chunk, "approval_id", "") or "") != str(metadata.get("approval_id") or ""):
        return "approval_id_mismatch"
    if str(getattr(chunk, "approved_content_hash", "") or "") != str(metadata.get("approved_content_hash") or ""):
        return "approved_content_hash_mismatch"
    if str(getattr(chunk, "security_level", "") or "").strip().lower() != str(metadata.get("security_level") or "").strip().lower():
        return "security_level_mismatch"
    record_acl = routes_rag._department_acl_set(metadata.get("department_acl"))
    chunk_acl = routes_rag._department_acl_set(getattr(chunk, "department_acl", []))
    if chunk_acl != record_acl:
        return "department_acl_mismatch"
    expected_metadata = dict(metadata)
    expected_hash = stable_content_hash(str(record.get("text") or ""), expected_metadata)
    if expected_hash != str(record.get("content_hash") or ""):
        return "record_content_hash_invalid"
    return ""


def _runtime_source_metadata_summary(
    records: list[dict[str, Any]],
    repository: JsonRepository,
    document_ids: list[str],
) -> dict[str, Any]:
    record_missing: dict[str, dict[str, int]] = {}
    document_missing: dict[str, list[str]] = {}
    complete_record_count = 0
    for record in records:
        document_id = str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        missing = [field for field in REQUIRED_MCP_SOURCE_METADATA_FIELDS if metadata.get(field) in (None, "")]
        if not missing:
            complete_record_count += 1
            continue
        field_counts = record_missing.setdefault(document_id or "missing-document-id", {})
        for field in missing:
            field_counts[field] = field_counts.get(field, 0) + 1
    for document_id in document_ids:
        document = repository.get_document(document_id)
        if document is None:
            document_missing[document_id] = list(REQUIRED_MCP_SOURCE_METADATA_FIELDS)
            continue
        missing = [
            field
            for field in REQUIRED_MCP_SOURCE_METADATA_FIELDS
            if getattr(document, field, None) in (None, "")
        ]
        if missing:
            document_missing[document_id] = missing
    missing_fields = sorted(
        {
            field
            for field_counts in record_missing.values()
            for field in field_counts
        }
        | {
            field
            for fields in document_missing.values()
            for field in fields
        }
    )
    return {
        "required_fields": list(REQUIRED_MCP_SOURCE_METADATA_FIELDS),
        "record_count": len(records),
        "complete_record_count": complete_record_count,
        "missing_record_count": len(records) - complete_record_count,
        "missing_fields": missing_fields,
        "missing_by_document": document_missing,
        "missing_record_field_counts_by_document": record_missing,
        "complete": not missing_fields,
    }


def _require_runtime_source_metadata(summary: dict[str, Any]) -> None:
    if bool(summary.get("complete")):
        return
    missing_fields = ", ".join(summary.get("missing_fields") or REQUIRED_MCP_SOURCE_METADATA_FIELDS)
    document_samples = []
    missing_by_document = summary.get("missing_by_document") if isinstance(summary.get("missing_by_document"), dict) else {}
    for document_id, fields in list(sorted(missing_by_document.items()))[:5]:
        document_samples.append(f"{document_id}({', '.join(fields)})")
    sample_text = "; ".join(document_samples)
    detail = f" Affected documents: {sample_text}." if sample_text else ""
    raise ValueError(
        "MCP runtime export requires citation/source metadata on approved records and documents: "
        f"{missing_fields}.{detail} Fill the document information, reprocess if needed, approve, "
        "and reindex before creating a handoff bundle."
    )


def _record_has_mcp_export_metadata(record: dict[str, Any], *, auth) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if str(metadata.get("approval_status") or "").strip().lower() != "approved":
        return False
    if not metadata.get("approval_id") or not metadata.get("approved_content_hash"):
        return False
    if not all(
        metadata.get(key)
        for key in (
            "approval_worklist_report_path",
            "approval_worklist_report_sha256",
            "approval_review_batch_manifest_path",
            "approval_review_batch_manifest_sha256",
            "approval_review_batch_id",
            "approval_review_batch_chunk_fingerprint",
            "approval_review_strategy",
        )
    ):
        return False
    tenant_id = str(metadata.get("tenant_id") or "").strip()
    if tenant_id and tenant_id != auth.tenant_id:
        return False
    security_level = str(metadata.get("security_level") or "").strip().lower()
    if security_level not in routes_rag.ROLE_SECURITY_LEVELS.get(auth.role, frozenset()):
        return False
    department_acl = routes_rag._department_acl_set(metadata.get("department_acl"))
    if department_acl and auth.role != routes_rag.API_ROLE_ADMIN and not set(auth.department_ids).intersection(department_acl):
        return False
    return True


def _require_kordoc_table_parser_evidence(repository: JsonRepository, document_ids: list[str]) -> dict[str, Any]:
    summary = _kordoc_table_parser_evidence_summary(repository, document_ids)
    missing = [
        item
        for item in summary["documents"]
        if item.get("required") and not _has_kordoc_parsed_evidence(item)
    ]
    if missing:
        sample = "; ".join(
            f"{item.get('document_id')}("
            f"{item.get('file_type')}, status={item.get('status') or 'missing'}, parser={item.get('parser') or 'missing'}"
            ")"
            for item in missing[:10]
        )
        raise ValueError(
            "MCP bundle creation requires Kordoc table parsing for HWP/HWPX/PDF/DOCX documents. "
            f"Missing or failed Kordoc evidence: {sample}. "
            "Install Kordoc (`npm install -g kordoc`) and rerun preprocessing, human approval, "
            "and indexing before creating the MCP bundle."
        )
    return summary


def _kordoc_table_parser_evidence_summary(repository: JsonRepository, document_ids: list[str]) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    for document_id in document_ids:
        document = repository.get_document(document_id)
        if document is None:
            continue
        file_type = _document_file_type(document)
        status, parser, table_count = _document_kordoc_table_status(repository, document_id)
        required = file_type in KORDOC_TABLE_REQUIRED_FILE_TYPES
        documents.append(
            {
                "document_id": document_id,
                "file_type": file_type,
                "required": required,
                "status": status,
                "parser": parser,
                "table_count": table_count,
            }
        )
    required_documents = [item for item in documents if item["required"]]
    parsed_documents = [item for item in required_documents if _has_kordoc_parsed_evidence(item)]
    return {
        "required_file_types": sorted(KORDOC_TABLE_REQUIRED_FILE_TYPES),
        "document_count": len(documents),
        "required_document_count": len(required_documents),
        "parsed_document_count": len(parsed_documents),
        "missing_or_failed_document_count": len(required_documents) - len(parsed_documents),
        "documents": documents,
    }


def _has_kordoc_parsed_evidence(item: dict[str, Any]) -> bool:
    return item.get("status") == "parsed" and item.get("parser") == "kordoc"


def _document_file_type(document: Any) -> str:
    value = str(getattr(document, "file_type", "") or "").strip().lower().lstrip(".")
    if value:
        return value
    return Path(str(getattr(document, "filename", "") or "")).suffix.lower().lstrip(".")


def _document_kordoc_table_status(repository: JsonRepository, document_id: str) -> tuple[str, str, int]:
    try:
        chunks = repository.get_chunks(document_id)
    except Exception:
        chunks = []
    for chunk in chunks:
        metadata = chunk.metadata or {}
        inventory = metadata.get("kordoc_table_inventory")
        inventory = inventory if isinstance(inventory, dict) else {}
        status = str(metadata.get("kordoc_table_parser_status") or inventory.get("status") or "").strip()
        parser = str(inventory.get("parser") or "").strip()
        table_count = _safe_int(metadata.get("kordoc_table_count", inventory.get("table_count", 0)))
        if status:
            return status, parser, table_count
    return "missing", "", 0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _empty_runtime_repository_manifest() -> dict[str, Any]:
    return {
        "documents": {},
        "jobs": {},
        "runs": {},
        "approvals": {},
        "review_decisions": {},
        "indexing_jobs": {},
        "rag_traces": {},
        "rag_feedback": {},
        "security_scans": {},
    }


def _write_runtime_result_json(
    repository_dir: Path,
    document_id: str,
    result_name: str,
    payload: Any,
    exported_files: list[str],
) -> None:
    path = repository_dir / f"{document_id}_{result_name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    exported_files.append(str(path))


def _write_runtime_journal(repository_dir: Path, journal_name: str, records: list[dict[str, Any]]) -> None:
    journal_dir = repository_dir / "journals"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{journal_name}.jsonl"
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


@_guard_local_mcp_materialization
def write_mcp_setup_bundle_zip(
    out_dir: str | Path,
    zip_out: str | Path,
    *,
    include_wheel: bool = False,
    wheel_path: str | Path | None = None,
    dist_dir: str | Path = "dist",
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> str:
    """Zip a generated MCP setup bundle for handoff to another operator."""
    source_dir = Path(out_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Setup bundle directory does not exist: {source_dir}")
    _assert_no_active_bundle_installation(source_dir)
    zip_path = Path(zip_out)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path_resolved = zip_path.resolve()
    expected_names = set(ALL_SETUP_BUNDLE_FILES)
    wheel = _resolve_setup_bundle_wheel(
        include_wheel=include_wheel,
        wheel_path=wheel_path,
        dist_dir=dist_dir,
        source_dir=source_dir,
    )
    archive_files: list[tuple[Path, str]] = [
        (path, path.name)
        for path in sorted(source_dir.iterdir())
        if path.is_file() and path.name in expected_names and path.resolve() != zip_path_resolved
    ]
    runtime_data_dir = source_dir / "data"
    include_empty_runtime_directory = (
        not runtime_data_dir.is_dir()
        or not any(runtime_data_dir.iterdir())
    )
    if runtime_data_dir.is_dir():
        _validate_runtime_data_bundle_consistency(runtime_data_dir)
        runtime_manifest = _runtime_manifest_payload(runtime_data_dir)
        reuse = (
            runtime_manifest.get("runtime_data_reuse")
            if isinstance(runtime_manifest, dict)
            else None
        )
        if (
            isinstance(reuse, dict)
            and reuse.get("schema_version") == RUNTIME_DATA_REUSE_SCHEMA_VERSION
        ):
            validate_mcp_runtime_data_bundle_integrity(runtime_data_dir)
        archive_files.extend(
            (path, path.relative_to(source_dir).as_posix())
            for path in sorted(runtime_data_dir.rglob("*"))
            if (
                path.is_file()
                and path.resolve() != zip_path_resolved
                and _include_runtime_data_file_in_zip(path, runtime_data_dir=runtime_data_dir)
            )
        )
    if wheel is not None and wheel.resolve() != zip_path_resolved:
        archive_files.append((wheel, wheel.name))

    total_bytes = sum(path.stat().st_size for path, _arcname in archive_files)
    bytes_written = 0
    if progress_callback is not None:
        progress_callback(0, total_bytes, "압축 준비")
    temporary_zip_path = zip_path.with_name(f".{zip_path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if include_empty_runtime_directory:
                directory_info = zipfile.ZipInfo("data/")
                directory_info.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(directory_info, b"")
            for path, arcname in archive_files:
                info = zipfile.ZipInfo.from_file(path, arcname=arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                portable_payload = _portable_handoff_payload(
                    path,
                    arcname=arcname,
                    source_dir=source_dir,
                    wheel_included=wheel is not None,
                )
                if portable_payload is not None:
                    archive.writestr(info, portable_payload)
                    bytes_written += path.stat().st_size
                    if progress_callback is not None:
                        progress_callback(bytes_written, total_bytes, arcname)
                    continue
                with path.open("rb") as source, archive.open(info, "w") as target:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        bytes_written += len(block)
                        if progress_callback is not None:
                            progress_callback(bytes_written, total_bytes, arcname)
        with zipfile.ZipFile(temporary_zip_path, "r") as completed_archive:
            if completed_archive.testzip() is not None:
                raise ValueError("Generated MCP setup ZIP failed its integrity check.")
        os.replace(temporary_zip_path, zip_path)
    finally:
        temporary_zip_path.unlink(missing_ok=True)
    return str(zip_path)


PORTABLE_HANDOFF_JSON_FILES = {
    "bundle_status.json",
    "chatgpt_connector.json",
    "chatgpt_desktop_local_mcp.json",
    "claude_https_mcp.json",
    "claude_desktop_config.json",
    "manifest.json",
    "mcp_config.bundle.json",
    "data/mcp_runtime_manifest.json",
}


def _portable_handoff_runtime_requirements(
    *,
    wheel_included: bool | None,
) -> dict[str, Any]:
    """Describe the fail-closed runtime gate for a bundle copied to a new PC."""

    return {
        "applies_to": "fresh_target_local_stdio",
        "bundled_windows_executable": False,
        "wheel_included": wheel_included,
        "minimum_python_version": PORTABLE_HANDOFF_MINIMUM_PYTHON_VERSION,
        "python_required_when_packaged_executable_absent": True,
        "included_wheel_is_not_a_python_runtime": True,
        "fresh_target_local_stdio_ready": False,
        "required_action": (
            "On the target PC, place a compatible packaged executable beside the extracted bundle or install "
            "Python 3.11+ and run install_local_package.ps1 with an included wheel or approved package source."
        ),
        "remote_https_client_requires_local_python": False,
    }


def _portable_handoff_readme_section(
    *,
    korean: bool,
    wheel_included: bool,
) -> str:
    wheel_value = "예" if korean and wheel_included else "아니요" if korean else "yes" if wheel_included else "no"
    if korean:
        return f"""
## 이 전달 ZIP의 대상 PC 실행 조건

- Windows 실행 파일 포함: 아니요. 원 PC에 설치된 EXE의 절대 경로는 대상 PC 실행 근거가 아닙니다.
- wheel 포함: {wheel_value}. wheel은 Python 실행 환경이 아니라 패키지 설치 파일입니다.
- 새 대상 PC에서 지원되는 로컬 stdio를 쓰려면 번들 옆에 별도로 제공된 호환 EXE가 있거나 Python 3.11+가 설치되어 있어야 합니다.
- 호환 EXE가 없으면 Python 3.11+를 설치한 뒤 `install_local_package.ps1`을 먼저 실행하세요. wheel이 포함되지 않았다면 승인된 wheel 또는 소스 패키지도 별도로 준비해야 합니다.
- `launcher_ready=true`는 런처 파일 존재만 뜻하며 대상 PC 실행 준비 완료를 뜻하지 않습니다.
"""
    return f"""
## This handoff ZIP: target-PC runtime gate

- Windows executable included: no. An absolute path to an executable installed on the source PC is not target-PC runtime evidence.
- Wheel included: {wheel_value}. A wheel is a package installer payload, not a Python runtime.
- Supported local stdio on a fresh target PC requires either a separately supplied compatible executable beside the bundle or Python 3.11+.
- Without that executable, install Python 3.11+ and run `install_local_package.ps1` first. If no wheel is included, separately provide an approved wheel or source package.
- `launcher_ready=true` means only that the launcher file exists; it does not mean the target runtime is ready.
"""


def _portable_handoff_powershell_payload(text: str, *, source_dir: Path) -> bytes:
    """Remove source-PC runtime hints from PowerShell copied into a handoff ZIP."""

    portable = _replace_bundle_path_text(text, source_dir=source_dir)
    for variable_name in (
        "PreferredPython",
        "PreferredProjectRoot",
        "InstalledPackagedExe",
        # Compatibility with launchers generated before InstalledPackagedExe
        # and BundledPackagedExe were separated.
        "PackagedExe",
    ):
        portable = re.sub(
            rf"(?m)^\${variable_name}\s*=.*$",
            f"${variable_name} = ''",
            portable,
        )
    return (portable.rstrip() + "\n").encode("utf-8-sig")


def _portable_handoff_payload(
    path: Path,
    *,
    arcname: str,
    source_dir: Path,
    wheel_included: bool,
) -> bytes | None:
    """Remove build-host bundle paths from handoff configuration templates."""

    normalized_arcname = arcname.replace("\\", "/")
    if normalized_arcname in {
        SETUP_BUNDLE_FILES["readme"],
        SETUP_BUNDLE_FILES["readme_ko"],
    }:
        text = path.read_text(encoding="utf-8-sig")
        text += _portable_handoff_readme_section(
            korean=normalized_arcname == SETUP_BUNDLE_FILES["readme_ko"],
            wheel_included=wheel_included,
        )
        return (text.rstrip() + "\n").encode("utf-8")
    if normalized_arcname.lower().endswith(".ps1"):
        return _portable_handoff_powershell_payload(
            path.read_text(encoding="utf-8-sig"),
            source_dir=source_dir,
        )
    if normalized_arcname in PORTABLE_HANDOFF_JSON_FILES:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        portable_source = _with_bundle_stdio_launcher(
            payload,
            launcher_path=source_dir / SETUP_BUNDLE_FILES["stdio_launcher"],
            server_name=str(
                payload.get("server_name")
                or (
                    next(iter(payload["mcpServers"]))
                    if isinstance(payload.get("mcpServers"), dict) and payload["mcpServers"]
                    else "regulation_mcp"
                )
            )
            if isinstance(payload, dict)
            else "regulation_mcp",
        )
        portable = _replace_bundle_path_with_placeholder(portable_source, source_dir=source_dir)
        if normalized_arcname in {
            SETUP_BUNDLE_FILES["bundle_status"],
            SETUP_BUNDLE_FILES["manifest"],
        } and isinstance(portable, dict):
            portable["portable_handoff_runtime"] = _portable_handoff_runtime_requirements(
                wheel_included=wheel_included
            )
        if normalized_arcname == "data/mcp_runtime_manifest.json" and isinstance(portable, dict):
            reuse = portable.get("runtime_data_reuse")
            if isinstance(reuse, dict) and reuse.get("schema_version") == RUNTIME_DATA_REUSE_SCHEMA_VERSION:
                reuse["manifest_sha256"] = _runtime_manifest_content_sha256(portable)
        return (json.dumps(portable, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if normalized_arcname == SETUP_BUNDLE_FILES["codex_config"]:
        text = path.read_text(encoding="utf-8-sig")
        return (_replace_bundle_path_text(text, source_dir=source_dir).rstrip() + "\n").encode("utf-8")
    return None


def _replace_bundle_path_with_placeholder(value: Any, *, source_dir: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_bundle_path_with_placeholder(item, source_dir=source_dir)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_bundle_path_with_placeholder(item, source_dir=source_dir) for item in value]
    if isinstance(value, str):
        return _replace_bundle_path_text(value, source_dir=source_dir)
    return value


def _replace_bundle_path_text(value: str, *, source_dir: Path) -> str:
    base_candidates = {
        str(source_dir),
        str(source_dir.resolve()),
        source_dir.as_posix(),
        source_dir.resolve().as_posix(),
    }
    candidates = {
        candidate
        for base in base_candidates
        for candidate in (base, base.replace("\\", "\\\\"))
    }
    result = value
    for candidate in sorted((item for item in candidates if item), key=len, reverse=True):
        result = re.sub(re.escape(candidate), "<BUNDLE_DIR>", result, flags=re.IGNORECASE)
    return result


def _load_strict_utf8_json_for_bundle(path: Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(UTF8_BOM):
        raise ValueError(f"Generated bundle JSON must be UTF-8 without BOM: {path}")
    try:
        return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_bundle_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Generated bundle file must contain strict UTF-8 JSON: {path}: {exc}") from exc


def _reject_duplicate_bundle_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_runtime_data_bundle_consistency(runtime_data_dir: Path) -> None:
    manifest_ids = _runtime_manifest_document_ids(runtime_data_dir)
    if not manifest_ids:
        manifest_required = _runtime_data_files_requiring_manifest(runtime_data_dir)
        if manifest_required:
            raise ValueError(
                "Runtime data bundle contains repository/vector artifacts but is missing a valid "
                "mcp_runtime_manifest.json with document_ids: "
                + ", ".join(path.relative_to(runtime_data_dir).as_posix() for path in manifest_required[:10])
            )
        return
    disallowed = _disallowed_runtime_repository_result_files(runtime_data_dir)
    if disallowed:
        raise ValueError(
            "Runtime data bundle contains raw preprocessing artifacts that must not be shipped in an MCP handoff zip: "
            + ", ".join(path.relative_to(runtime_data_dir).as_posix() for path in disallowed[:10])
        )
    forbidden_audit_artifacts = _forbidden_runtime_audit_artifacts(runtime_data_dir)
    if forbidden_audit_artifacts:
        raise ValueError(
            "Runtime data bundle contains review decisions or raw audit artifacts that must not be shipped: "
            + ", ".join(
                path.relative_to(runtime_data_dir).as_posix()
                for path in forbidden_audit_artifacts[:10]
            )
        )
    unexpected_vectors = _unexpected_runtime_vector_store_files(runtime_data_dir)
    if unexpected_vectors:
        raise ValueError(
            "Runtime data bundle contains vector store files outside the manifest tenant: "
            + ", ".join(path.relative_to(runtime_data_dir).as_posix() for path in unexpected_vectors[:10])
        )
    hierarchy_issue = _runtime_hierarchy_index_issue(runtime_data_dir)
    if hierarchy_issue:
        raise ValueError(f"Runtime data bundle hierarchical index is invalid: {hierarchy_issue}")
    document_sets = {
        "repository result files": _repository_result_file_document_ids(runtime_data_dir),
        "repository manifest": _repository_manifest_document_ids(runtime_data_dir),
        "approved vectors": _vector_document_ids(runtime_data_dir),
        "approval snapshot": _approval_snapshot_document_ids(runtime_data_dir),
    }
    stale: list[str] = []
    for label, document_ids in document_sets.items():
        extra = sorted(document_ids - manifest_ids)
        if extra:
            stale.append(f"{label}: {', '.join(extra[:5])}")
    if stale:
        raise ValueError(
            "Runtime data bundle contains stale document artifacts outside mcp_runtime_manifest.document_ids: "
            + "; ".join(stale)
        )
    manifest = _runtime_manifest_payload(runtime_data_dir)
    reuse = manifest.get("runtime_data_reuse") if isinstance(manifest, dict) else None
    approval_journal_path = (
        runtime_data_dir / "repository" / "journals" / "approvals.jsonl"
    )
    if approval_journal_path.exists() or (
        isinstance(reuse, dict)
        and reuse.get("schema_version") == RUNTIME_DATA_REUSE_SCHEMA_VERSION
    ):
        _validate_runtime_approval_decision_journal(runtime_data_dir, manifest)
    omission_path = runtime_data_dir / "repository" / OMISSION_DISPOSITION_SNAPSHOT_FILENAME
    if omission_path.exists() or (
        isinstance(reuse, dict)
        and reuse.get("schema_version") == RUNTIME_DATA_REUSE_SCHEMA_VERSION
    ):
        _validate_runtime_omission_disposition_snapshot(runtime_data_dir, manifest)
    if isinstance(reuse, dict) and reuse.get("schema_version") == RUNTIME_DATA_REUSE_SCHEMA_VERSION:
        unexpected_files = [
            path
            for path in sorted(runtime_data_dir.rglob("*"))
            if path.is_file()
            and not _is_mutable_runtime_generated_file(path)
            and not _include_runtime_data_file_in_zip(path, runtime_data_dir=runtime_data_dir)
        ]
        if unexpected_files:
            raise ValueError(
                "Sealed runtime data contains files outside the handoff allowlist: "
                + ", ".join(
                    path.relative_to(runtime_data_dir).as_posix()
                    for path in unexpected_files[:10]
                )
            )


def _validate_runtime_omission_disposition_snapshot(
    runtime_data_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    sidecar_path = runtime_data_dir / "repository" / OMISSION_DISPOSITION_SNAPSHOT_FILENAME
    if sidecar_path.is_symlink() or not sidecar_path.is_file():
        raise ValueError("Runtime omission disposition snapshot is missing or is a symbolic link.")
    payload = _load_strict_utf8_json_for_bundle(sidecar_path)
    if not isinstance(payload, dict):
        raise ValueError("Runtime omission disposition snapshot must contain a JSON object.")
    generated_at = _runtime_export_audit_timestamp(payload.get("generated_at"))
    if generated_at is None:
        raise ValueError("Runtime omission disposition snapshot generated_at is missing or invalid.")
    tenant_id = str(manifest.get("tenant_id") or "").strip()
    if not tenant_id or payload.get("tenant_id") != tenant_id:
        raise ValueError("Runtime omission disposition snapshot tenant does not match the runtime manifest.")
    files = manifest.get("files")
    declared_path = (
        str(files.get("omission_disposition_snapshot") or "").replace("\\", "/")
        if isinstance(files, dict)
        else ""
    )
    expected_suffix = f"/data/repository/{OMISSION_DISPOSITION_SNAPSHOT_FILENAME}"
    if not declared_path.endswith(expected_suffix):
        raise ValueError("Runtime manifest does not declare the omission disposition snapshot path.")
    requested_document_ids = payload.get("requested_document_ids")
    if (
        not isinstance(requested_document_ids, list)
        or requested_document_ids != sorted(set(str(value or "").strip() for value in requested_document_ids))
        or not requested_document_ids
        or any(not str(value or "").strip() for value in requested_document_ids)
    ):
        raise ValueError("Runtime omission disposition requested document IDs are invalid.")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Runtime omission disposition entries are missing.")

    seen_pairs: set[tuple[str, str]] = set()
    normalized_entries: list[dict[str, Any]] = []
    expected_status = {
        "exported": "approved",
        "omitted_rejected": "rejected",
        "omitted_superseded": "superseded",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != OMISSION_DISPOSITION_ENTRY_FIELDS:
            raise ValueError("Runtime omission disposition entries contain unapproved fields.")
        document_id = str(entry.get("document_id") or "").strip()
        chunk_id = str(entry.get("chunk_id") or "").strip()
        pair = (document_id, chunk_id)
        disposition = str(entry.get("disposition") or "").strip()
        if (
            entry.get("tenant_id") != tenant_id
            or document_id not in requested_document_ids
            or not chunk_id
            or pair in seen_pairs
            or not str(entry.get("content_hash") or "").strip()
            or not str(entry.get("latest_decision_id") or "").strip()
            or disposition not in expected_status
            or entry.get("latest_decision_status") != expected_status.get(disposition)
            or type(entry.get("exported")) is not bool
            or entry.get("exported") is not (disposition == "exported")
            or entry.get("requested") is not True
            or _runtime_export_audit_timestamp(entry.get("latest_decision_at")) is None
        ):
            raise ValueError("Runtime omission disposition entry classification is invalid.")
        seen_pairs.add(pair)
        normalized_entries.append(entry)

    entry_document_ids = {
        str(entry["document_id"]) for entry in normalized_entries
    }
    if set(requested_document_ids) != entry_document_ids:
        raise ValueError(
            "Runtime omission disposition requested documents do not have complete chunk coverage."
        )

    expected_projection = _runtime_omission_disposition_top_level(
        normalized_entries,
        [str(value) for value in requested_document_ids],
    )
    expected_keys = set(expected_projection) | {"generated_at"}
    if set(payload) != expected_keys or any(
        payload.get(field) != expected
        for field, expected in expected_projection.items()
    ):
        raise ValueError("Runtime omission disposition top-level counts or IDs are inconsistent.")

    manifest_document_ids = manifest.get("document_ids")
    if not isinstance(manifest_document_ids, list) or sorted(manifest_document_ids) != payload.get(
        "exported_document_ids"
    ):
        raise ValueError("Runtime omission disposition exported documents do not match the manifest.")

    vector_pairs: set[tuple[str, str]] = set()
    vector_approval_bindings: dict[tuple[str, str], tuple[str, str]] = {}
    vector_dir = runtime_data_dir / "vector_db" / tenant_directory_key(tenant_id)
    vector_path = vector_dir / "approved_vectors.jsonl"
    try:
        vector_records = list(_iter_strict_jsonl_for_runtime_reuse(vector_path))
    except (OSError, ValueError) as exc:
        raise ValueError("Runtime omission disposition cannot verify approved vectors.") from exc
    for record in vector_records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        pair = (
            str(record.get("document_id") or metadata.get("document_id") or "").strip(),
            str(record.get("chunk_id") or metadata.get("chunk_id") or "").strip(),
        )
        if not all(pair) or pair in vector_pairs:
            raise ValueError("Runtime omission disposition found invalid approved vector identities.")
        vector_pairs.add(pair)
        vector_approval_bindings[pair] = (
            str(metadata.get("approval_id") or "").strip(),
            str(metadata.get("approved_content_hash") or "").strip().lower(),
        )
    exported_entries = {
        (str(entry["document_id"]), str(entry["chunk_id"])): entry
        for entry in normalized_entries
        if entry["exported"] is True
    }
    if set(exported_entries) != vector_pairs:
        raise ValueError("Runtime omission disposition exported chunks do not match approved vectors.")
    for pair, entry in exported_entries.items():
        approval_id, content_hash = vector_approval_bindings[pair]
        if (
            approval_id != entry["latest_decision_id"]
            or content_hash != str(entry["content_hash"]).strip().lower()
        ):
            raise ValueError("Runtime omission disposition approval binding does not match approved vectors.")
    return payload


def _runtime_data_files_requiring_manifest(runtime_data_dir: Path) -> list[Path]:
    files: list[Path] = []
    repository_dir = runtime_data_dir / "repository"
    if repository_dir.is_dir():
        for path in sorted(repository_dir.glob("*.json")):
            if path.name in {
                "manifest.json",
                "approval_snapshot.json",
                OMISSION_DISPOSITION_SNAPSHOT_FILENAME,
            } or any(
                path.name.endswith(suffix) for suffix in RUNTIME_REPOSITORY_RESULT_SUFFIXES
            ):
                files.append(path)
    vector_dir = runtime_data_dir / "vector_db"
    if vector_dir.is_dir():
        files.extend(
            path
            for path in sorted(vector_dir.rglob("*"))
            if path.is_file() and path.name in {"approved_vectors.jsonl", "bm25_index.json"}
        )
    hierarchy_file = hierarchical_index_path(runtime_data_dir)
    if hierarchy_file.is_file():
        files.append(hierarchy_file)
    return files


def _runtime_hierarchy_index_issue(runtime_data_dir: Path) -> str | None:
    payload = _runtime_manifest_payload(runtime_data_dir)
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    index_path = hierarchical_index_path(runtime_data_dir)
    hierarchy_declared = bool(
        payload.get("hierarchical_index_status")
        or files.get("hierarchical_index")
        or files.get("hierarchical_index_sha256")
        or index_path.exists()
    )
    if not hierarchy_declared:
        return None
    if payload.get("hierarchical_index_status") != "ready":
        return "mcp_runtime_manifest.json does not mark the hierarchy index ready"
    expected_hash = str(files.get("hierarchical_index_sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        return "hierarchical_index_sha256 is missing or invalid"
    if not index_path.is_file():
        return f"missing {index_path.relative_to(runtime_data_dir).as_posix()}"
    digest = hashlib.sha256()
    with index_path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != expected_hash:
        return "hierarchical index SHA-256 does not match the runtime manifest"

    try:
        hierarchy = hierarchical_index_summary(index_path)
    except Exception:
        return "hierarchical index metadata could not be read"
    if not isinstance(hierarchy, dict):
        return "hierarchical index metadata is missing"
    hierarchy_source_content_hashes = hierarchy.get("source_content_hashes")
    if (
        not isinstance(hierarchy_source_content_hashes, str)
        or not re.fullmatch(r"[a-f0-9]{64}", hierarchy_source_content_hashes)
    ):
        return "hierarchical index source-content binding is missing or invalid"
    manifest_hierarchy = payload.get("hierarchical_index")
    if (
        not isinstance(manifest_hierarchy, dict)
        or manifest_hierarchy.get("source_content_hashes")
        != hierarchy_source_content_hashes
    ):
        return "hierarchical index source-content binding does not match the runtime manifest"
    hierarchy_logical_corpus_sha256 = hierarchy.get("logical_corpus_sha256")
    manifest_logical_corpus_sha256 = payload.get("logical_corpus_sha256")
    nested_logical_corpus_sha256 = manifest_hierarchy.get(
        "logical_corpus_sha256"
    )
    if (
        not isinstance(hierarchy_logical_corpus_sha256, str)
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            hierarchy_logical_corpus_sha256,
        )
    ):
        return "hierarchical index logical-corpus fingerprint is missing or invalid"
    if (
        not isinstance(manifest_logical_corpus_sha256, str)
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            manifest_logical_corpus_sha256,
        )
        or not isinstance(nested_logical_corpus_sha256, str)
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            nested_logical_corpus_sha256,
        )
        or manifest_logical_corpus_sha256
        != hierarchy_logical_corpus_sha256
        or nested_logical_corpus_sha256
        != hierarchy_logical_corpus_sha256
    ):
        return "logical-corpus fingerprints do not match the hierarchical index"

    tenant_id = str(payload.get("tenant_id") or "").strip()
    if not tenant_id:
        return "runtime manifest tenant_id is missing"
    vector_path = (
        runtime_data_dir
        / "vector_db"
        / tenant_directory_key(tenant_id)
        / "approved_vectors.jsonl"
    )
    if not vector_path.is_file():
        return f"missing {vector_path.relative_to(runtime_data_dir).as_posix()}"
    try:
        vector_records = list(_iter_strict_jsonl_for_runtime_reuse(vector_path))
        projected_source_content_hashes = source_content_hashes(vector_records)
        projected_logical_corpus_sha256 = logical_corpus_sha256_for_records(
            vector_records,
            tenant_id=tenant_id,
            profile_id=(
                str(payload["profile_id"]).strip()
                if payload.get("profile_id")
                else None
            ),
        )
    except (OSError, ValueError):
        return "approved vector source projection is invalid"
    if projected_source_content_hashes != hierarchy_source_content_hashes:
        return "approved vector source-content binding does not match the hierarchical index"
    if projected_logical_corpus_sha256 != hierarchy_logical_corpus_sha256:
        return "approved vector logical-corpus fingerprint does not match the hierarchical index"

    bm25_path = (
        runtime_data_dir
        / "vector_db"
        / tenant_directory_key(tenant_id)
        / "bm25_index.json"
    )
    bm25_declared = bool(
        payload.get("bm25_index_status")
        or files.get("bm25_index")
        or bm25_path.exists()
    )
    if bm25_declared:
        if payload.get("bm25_index_status") != "ready":
            return "mcp_runtime_manifest.json does not mark the BM25 index ready"
        bm25_index = load_bm25_index(bm25_path)
        if bm25_index is None:
            return f"missing or invalid {bm25_path.relative_to(runtime_data_dir).as_posix()}"
        if (
            not re.fullmatch(
                r"[a-f0-9]{64}",
                bm25_index.source_content_hashes,
            )
            or bm25_index.source_content_hashes
            != hierarchy_source_content_hashes
        ):
            return "BM25 source-content binding does not match the hierarchical index"
    return None


def _runtime_manifest_payload(runtime_data_dir: Path) -> dict[str, Any]:
    manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    if not manifest_path.is_file():
        return {}
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _runtime_manifest_document_ids(runtime_data_dir: Path) -> set[str]:
    payload = _runtime_manifest_payload(runtime_data_dir)
    if not payload:
        return set()
    values = payload.get("document_ids")
    if isinstance(values, list):
        return {str(value) for value in values if str(value).strip()}
    value = str(payload.get("document_id") or "").strip()
    return {value} if value else set()


def _unexpected_runtime_vector_store_files(runtime_data_dir: Path) -> list[Path]:
    payload = _runtime_manifest_payload(runtime_data_dir)
    tenant_id = str(payload.get("tenant_id") or "").strip() if payload else ""
    if not tenant_id:
        return []
    expected_storage_key = tenant_directory_key(tenant_id)
    vector_dir = runtime_data_dir / "vector_db"
    if not vector_dir.is_dir():
        return []
    unexpected: list[Path] = []
    for path in sorted(vector_dir.rglob("*")):
        if not path.is_file() or path.name not in {"approved_vectors.jsonl", "bm25_index.json"}:
            continue
        try:
            relative_parts = path.relative_to(vector_dir).parts
        except ValueError:
            continue
        if not relative_parts or relative_parts[0] != expected_storage_key:
            unexpected.append(path)
    return unexpected


def _disallowed_runtime_repository_result_files(runtime_data_dir: Path) -> list[Path]:
    repository_dir = runtime_data_dir / "repository"
    if not repository_dir.is_dir():
        return []
    disallowed_suffixes = tuple(
        suffix for suffix in RUNTIME_REPOSITORY_RESULT_SUFFIXES if suffix != "_chunks.json"
    )
    return sorted(
        path
        for path in repository_dir.glob("*.json")
        if any(path.name.endswith(suffix) for suffix in disallowed_suffixes)
    )


def _forbidden_runtime_audit_artifacts(runtime_data_dir: Path) -> list[Path]:
    repository_dir = runtime_data_dir / "repository"
    if not repository_dir.is_dir():
        return []
    forbidden_names = {
        "review_decisions.jsonl",
        "review_decisions.json",
        "review_journal.jsonl",
        "raw_review_artifacts.json",
    }
    return sorted(
        path
        for path in repository_dir.rglob("*")
        if path.is_file()
        and (
            path.name.casefold() in forbidden_names
            or "review_decision" in path.name.casefold()
            or path.parent.name.casefold() in {"raw", "artifacts", "review_artifacts"}
        )
    )


def _repository_result_file_document_ids(runtime_data_dir: Path) -> set[str]:
    repository_dir = runtime_data_dir / "repository"
    document_ids: set[str] = set()
    if not repository_dir.is_dir():
        return document_ids
    for path in repository_dir.glob("*.json"):
        for suffix in RUNTIME_REPOSITORY_RESULT_SUFFIXES:
            if path.name.endswith(suffix):
                document_ids.add(path.name[: -len(suffix)])
                break
    return document_ids


def _repository_manifest_document_ids(runtime_data_dir: Path) -> set[str]:
    manifest_path = runtime_data_dir / "repository" / "manifest.json"
    if not manifest_path.is_file():
        return set()
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, dict):
        return set()
    return {str(document_id) for document_id in documents if str(document_id).strip()}


def _vector_document_ids(runtime_data_dir: Path) -> set[str]:
    document_ids: set[str] = set()
    vector_dir = runtime_data_dir / "vector_db"
    if not vector_dir.is_dir():
        return document_ids
    for path in sorted(vector_dir.rglob("approved_vectors.jsonl")):
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
                document_id = str(record.get("document_id") or metadata.get("document_id") or "").strip()
                if document_id:
                    document_ids.add(document_id)
    return document_ids


def _approval_snapshot_document_ids(runtime_data_dir: Path) -> set[str]:
    sidecar_path = runtime_data_dir / "repository" / "approval_snapshot.json"
    if not sidecar_path.is_file():
        return set()
    payload = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        return set()
    document_ids = {
        str(value)
        for value in payload.get("document_ids") or []
        if str(value).strip()
    }
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        document_id = str(entry.get("document_id") or "").strip()
        if document_id:
            document_ids.add(document_id)
    return document_ids


def _include_runtime_data_file_in_zip(path: Path, *, runtime_data_dir: Path) -> bool:
    relative_parts = path.relative_to(runtime_data_dir).parts
    if any(part.casefold() in BUNDLE_ZIP_EXCLUDED_DIR_NAMES for part in relative_parts[:-1]):
        return False
    if path.name.startswith(".") or _is_mutable_runtime_generated_file(path):
        return False
    if path.name == "mcp_runtime_manifest.json":
        return True
    if path.name in {"approved_vectors.jsonl", "bm25_index.json"} and "vector_db" in path.parts:
        return True
    if path.name == "regulation_hierarchy.sqlite3" and path.parent.name == "hierarchy":
        return True
    if path.name == "manifest.json" and path.parent.name == "repository":
        return True
    if path.name == "approval_snapshot.json" and path.parent.name == "repository":
        return True
    if (
        path.name == OMISSION_DISPOSITION_SNAPSHOT_FILENAME
        and path.parent.name == "repository"
    ):
        return True
    if path.name.endswith("_chunks.json") and path.parent.name == "repository":
        return True
    if (
        path.name in {"approvals.jsonl", "indexing_jobs.jsonl"}
        and path.parent.name == "journals"
        and path.parent.parent.name == "repository"
    ):
        return True
    return False


def _resolve_setup_bundle_wheel(
    *,
    include_wheel: bool,
    wheel_path: str | Path | None,
    dist_dir: str | Path,
    source_dir: Path,
) -> Path | None:
    if wheel_path is not None:
        wheel = Path(wheel_path)
        if not wheel.is_absolute() and not wheel.is_file():
            for base in (source_dir.parent, Path(__file__).resolve().parents[1]):
                candidate = base / wheel
                if candidate.is_file():
                    wheel = candidate
                    break
        if not wheel.is_file():
            raise FileNotFoundError(f"Wheel file does not exist: {wheel}")
        if wheel.suffix.lower() != ".whl":
            raise ValueError(f"Wheel path must point to a .whl file: {wheel}")
        return wheel
    if not include_wheel:
        return None
    dist = Path(dist_dir)
    dist_candidates = [dist] if dist.is_absolute() else [dist, source_dir.parent / dist, Path(__file__).resolve().parents[1] / dist]
    wheels: list[Path] = []
    seen: set[Path] = set()
    for candidate_dir in dist_candidates:
        for candidate in candidate_dir.glob("reg_rag_preprocessor-*.whl"):
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                wheels.append(resolved)
    wheels = sorted(wheels, key=lambda path: path.stat().st_mtime, reverse=True)
    if not wheels:
        searched = ", ".join(str(path) for path in dist_candidates)
        raise FileNotFoundError(f"No reg_rag_preprocessor wheel found under: {searched}. Run python -m build first.")
    return wheels[0]


def _setup_bundle_connections(config: dict[str, Any]) -> list[dict[str, Any]]:
    chatgpt_ready = bool((config.get("chatgpt_remote") or config.get("chatgpt") or {}).get("ready"))
    claude_remote_ready = bool((config.get("claude_remote") or {}).get("ready"))
    connections = [
        {
            "client": "Claude Desktop",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["claude_desktop"],
            "config_file": SETUP_BUNDLE_FILES["claude_desktop"],
            "operator_action": "Merge mcpServers into Claude Desktop settings, restart the app, and verify a tool call.",
        },
        {
            "client": "Claude Code",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["claude_code_stdio"],
            "config_file": SETUP_BUNDLE_FILES["claude_code_stdio"],
            "operator_action": "Run the generated PowerShell registration command, restart Claude Code, and verify the server.",
        },
        {
            "client": "Codex CLI / Codex IDE",
            "profile": "codex-local",
            "tool_profile": "full",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["codex_config"],
            "config_file": SETUP_BUNDLE_FILES["codex_config"],
            "operator_action": (
                "Apply the generated server block to ~/.codex/config.toml, restart Codex, and verify search then fetch."
            ),
        },
        {
            "client": "ChatGPT web · remote HTTPS MCP",
            "profile": "chatgpt-remote",
            "mode": "streamable_http",
            "ready": False,
            "configuration_ready": chatgpt_ready,
            "remote_endpoint_verified": False,
            "tool_scan_unverified": True,
            "primary_file": SETUP_BUNDLE_FILES["chatgpt"],
            "config_file": SETUP_BUNDLE_FILES["chatgpt"],
            "operator_action": (
                "Confirm Developer mode and workspace permission in ChatGPT web, create an app "
                "with the final HTTPS /mcp URL, scan tools, then verify search and fetch."
            ),
            "official_help_url": CHATGPT_MCP_HELP_URL,
            "secure_mcp_tunnel_url": CHATGPT_SECURE_MCP_TUNNEL_URL,
        },
        {
            "client": "Claude · Vercel HTTPS MCP",
            "mode": "streamable_http",
            "ready": claude_remote_ready,
            "primary_file": SETUP_BUNDLE_FILES["claude_remote"],
            "config_file": SETUP_BUNDLE_FILES["claude_remote"],
            "operator_action": (
                "Deploy the common Vercel server and register only its final HTTPS /mcp URL "
                "and approved authentication in Claude."
            ),
        },
    ]
    connection_order = {
        client: index
        for index, client in enumerate(
            (
                "Claude Code",
                "Codex CLI / Codex IDE",
                "Claude Desktop",
                "ChatGPT web · remote HTTPS MCP",
                "Claude · Vercel HTTPS MCP",
            )
        )
    }
    return sorted(connections, key=lambda item: connection_order[str(item["client"])])


def _install_local_package_script() -> str:
    script = r'''param(
  [string]$PackagePath = "",
  [switch]$NoEditable,
  [switch]$ConnectionFlowLockHeld
)

$ErrorActionPreference = "Stop"
$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonCommand = ""
$StandaloneInstallMutex = $null
$StandaloneInstallLockAcquired = $false

try {
if (-not $ConnectionFlowLockHeld) {
  $StandaloneInstallMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-LocalMcpConnectionFlow")
  try { $StandaloneInstallLockAcquired = $StandaloneInstallMutex.WaitOne([TimeSpan]::FromSeconds(180)) }
  catch [System.Threading.AbandonedMutexException] { $StandaloneInstallLockAcquired = $true }
  if (-not $StandaloneInstallLockAcquired) {
    throw "Timed out waiting for another local MCP installation or registration flow to finish."
  }
}

function Get-ProjectRoot {
  $Cursor = Resolve-Path $BundleDir
  while ($Cursor) {
    $Pyproject = Join-Path $Cursor "pyproject.toml"
    if (Test-Path -LiteralPath $Pyproject) {
      return $Cursor.Path
    }
    $Parent = Split-Path -Parent $Cursor
    if (-not $Parent -or $Parent -eq $Cursor.Path) {
      break
    }
    $Cursor = Resolve-Path $Parent
  }
  return $null
}

function Get-BundledWheel {
  $Wheels = @(Get-ChildItem -Path $BundleDir -Filter "reg_rag_preprocessor-*.whl" -File -ErrorAction SilentlyContinue)
  if ($Wheels.Count -gt 1) {
    throw "Multiple bundled reg_rag_preprocessor wheels were found. Keep exactly one wheel beside install_local_package.ps1, then retry."
  }
  if ($Wheels.Count -eq 1) { return $Wheels[0] }
  return $null
}

function Test-SupportedPython([string]$CommandPath) {
  if (-not $CommandPath) { return $false }
  $PreviousErrorActionPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & $CommandPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 41)" 1>$null 2>$null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  } finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
  }
}

function Get-PythonFromPyLauncher([string]$PyCommand) {
  if (-not $PyCommand) { return $null }
  foreach ($Selector in @("-3.11", "-3")) {
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
      $ErrorActionPreference = "Continue"
      $Output = @(& $PyCommand $Selector -c "import base64,os,sys; print(base64.b64encode(os.path.abspath(sys.executable).encode('utf-8')).decode('ascii')) if sys.version_info >= (3, 11) else sys.exit(41)" 2>$null)
      $ExitCode = $LASTEXITCODE
    } catch {
      $ExitCode = 1
      $Output = @()
    } finally {
      $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ExitCode -ne 0) { continue }
    $EncodedCandidate = [string]($Output | Select-Object -Last 1)
    try {
      $Candidate = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($EncodedCandidate))
    } catch {
      continue
    }
    if ([System.IO.Path]::IsPathRooted($Candidate) -and (Test-Path -LiteralPath $Candidate -PathType Leaf) -and (Test-SupportedPython $Candidate)) {
      return (Resolve-Path -LiteralPath $Candidate).Path
    }
  }
  return $null
}

function Assert-Python {
  $Candidates = @()
  if ($env:REG_RAG_PYTHON -and (Test-Path -LiteralPath $env:REG_RAG_PYTHON -PathType Leaf)) {
    $Candidates += (Resolve-Path -LiteralPath $env:REG_RAG_PYTHON).Path
  }
  foreach ($Name in @("python", "python3")) {
    $Resolved = Get-Command $Name -ErrorAction SilentlyContinue
    if ($Resolved -and $Resolved.Source) { $Candidates += $Resolved.Source }
  }
  foreach ($Candidate in @($Candidates | Select-Object -Unique)) {
    if (Test-SupportedPython $Candidate) {
      $script:PythonCommand = $Candidate
      return
    }
  }
  $Py = Get-Command py -ErrorAction SilentlyContinue
  if ($Py -and $Py.Source) {
    $PyPython = Get-PythonFromPyLauncher $Py.Source
    if ($PyPython) {
      $script:PythonCommand = $PyPython
      return
    }
  }
  throw "Python 3.11+ was not found through REG_RAG_PYTHON, python/python3, or the Windows py launcher. Install Python 3.11+ or activate the approved Python environment first."
}

function Add-ActivePythonRuntimeToPath {
  if (-not $script:PythonCommand) {
    throw "The active Python executable could not be resolved."
  }
  $ScriptsOutput = @(& $script:PythonCommand -c "import base64,sysconfig; print(base64.b64encode((sysconfig.get_path('scripts') or '').encode('utf-8')).decode('ascii'))")
  $ScriptsProbeExitCode = $LASTEXITCODE
  if ($ScriptsProbeExitCode -ne 0) {
    throw "Could not determine the console-script directory for the active Python environment."
  }
  $EncodedScriptsDir = [string]($ScriptsOutput | Select-Object -First 1)
  try {
    $ScriptsDir = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($EncodedScriptsDir))
  } catch {
    throw "The console-script directory returned by the active Python environment was invalid."
  }
  if (-not [string]::IsNullOrWhiteSpace($ScriptsDir) -and (Test-Path -LiteralPath $ScriptsDir)) {
    $PathEntries = @($env:Path -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $OtherPathEntries = @($PathEntries | Where-Object { -not [string]::Equals($_, $ScriptsDir, [System.StringComparison]::OrdinalIgnoreCase) })
    $env:Path = (@($ScriptsDir) + $OtherPathEntries) -join ';'
    $script:PythonScriptsDir = (Resolve-Path -LiteralPath $ScriptsDir).Path
  }
  $env:REG_RAG_PYTHON = $script:PythonCommand
}

function Assert-McpCommands {
  $Missing = @()
  $WrongRuntime = @()
  foreach ($Name in @("reg-rag-mcp-server", "reg-rag-mcp-config", "reg-rag-mcp-doctor", "reg-rag-mcp-smoke", "reg-rag-mcp-codex-app-server-check", "reg-rag-mcp-desktop-recognition-check", "reg-rag-mcp-client-config-smoke", "reg-rag-mcp-index-visibility")) {
    $ResolvedCommand = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $ResolvedCommand -or -not $ResolvedCommand.Source) {
      $Missing += $Name
    } elseif (-not $script:PythonScriptsDir -or -not [string]::Equals((Split-Path -Parent $ResolvedCommand.Source), $script:PythonScriptsDir, [System.StringComparison]::OrdinalIgnoreCase)) {
      $WrongRuntime += $Name
    }
  }
  if ($Missing.Count -gt 0) {
    throw "Package installed, but these console commands are still not on PATH: $($Missing -join ', '). Activate the Python environment used for installation."
  }
  if ($WrongRuntime.Count -gt 0) {
    throw "Package installed, but these console commands resolve to a different Python runtime: $($WrongRuntime -join ', '). Re-run with REG_RAG_PYTHON set to the approved Python executable."
  }
}

function Write-RuntimePythonMarker {
  if (-not $script:PythonCommand -or -not (Test-Path -LiteralPath $script:PythonCommand -PathType Leaf)) {
    throw "The installed Python executable could not be recorded for Desktop restart."
  }
  $ResolvedPython = (Resolve-Path -LiteralPath $script:PythonCommand).Path
  $Leaf = [System.IO.Path]::GetFileNameWithoutExtension($ResolvedPython)
  if ($Leaf -notmatch '^python(?:\d+(?:\.\d+)*)?$') {
    throw "The selected runtime is not a Python executable and was not recorded."
  }
  $RuntimeModules = __RUNTIME_IDENTITY_MODULES__
  $IdentityBuilderBase64 = __RUNTIME_IDENTITY_BUILDER_BASE64__
  $RuntimeModulesJson = $RuntimeModules | ConvertTo-Json -Compress
  $RuntimeModulesBase64 = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($RuntimeModulesJson))
  $HadPythonPath = Test-Path Env:PYTHONPATH
  $PreviousPythonPath = $env:PYTHONPATH
  $HadSafePath = Test-Path Env:PYTHONSAFEPATH
  $PreviousSafePath = $env:PYTHONSAFEPATH
  $PreviousErrorActionPreference = $ErrorActionPreference
  try {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    $env:PYTHONSAFEPATH = "1"
    $IdentityOutput = @(& $ResolvedPython -c "import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))" $IdentityBuilderBase64 $RuntimeModulesBase64 2>$null)
    $IdentityExitCode = $LASTEXITCODE
  } finally {
    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }
  }
  if ($IdentityExitCode -ne 0) {
    throw "The installed MCP runtime identity could not be computed. Reinstall the generated bundle wheel."
  }
  try {
    $IdentityJson = [string]($IdentityOutput | Select-Object -Last 1)
    $Identity = $IdentityJson | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "The installed MCP runtime returned an invalid identity payload."
  }
  if (@($Identity.module_sha256.PSObject.Properties).Count -ne $RuntimeModules.Count) {
    throw "The installed MCP runtime identity is missing command modules."
  }
  $ModuleHashes = [ordered]@{}
  foreach ($ModuleName in $RuntimeModules) {
    $HashProperty = $Identity.module_sha256.PSObject.Properties[$ModuleName]
    $ModuleHash = if ($HashProperty) { [string]$HashProperty.Value } else { "" }
    if ($ModuleHash -notmatch '^sha256:[0-9a-f]{64}$') {
      throw "The installed MCP runtime identity is invalid for $ModuleName."
    }
    $ModuleHashes[$ModuleName] = $ModuleHash
  }
  $BuildIdentity = [string]$Identity.build_identity_sha256
  if ($BuildIdentity -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "The installed MCP runtime aggregate identity is invalid."
  }
  $Marker = [ordered]@{
    schema_version = 2
    python_executable = $ResolvedPython
    minimum_python = "3.11"
    package_import = "scripts.run_regulation_mcp"
    identity_scope = "mcp-command-modules-v1"
    hash_algorithm = "sha256"
    module_sha256 = $ModuleHashes
    build_identity_sha256 = $BuildIdentity
    written_at = [DateTime]::UtcNow.ToString("o")
  }
  $MarkerPath = Join-Path $BundleDir "runtime_python.json"
  $TemporaryPath = Join-Path $BundleDir (".runtime_python.{0}.{1}.tmp" -f $PID, [Guid]::NewGuid().ToString("N"))
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  try {
    [System.IO.File]::WriteAllText($TemporaryPath, (($Marker | ConvertTo-Json -Depth 10) + [Environment]::NewLine), $Utf8NoBom)
    Move-Item -LiteralPath $TemporaryPath -Destination $MarkerPath -Force
  } finally {
    if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -LiteralPath $TemporaryPath -Force }
  }
}

Assert-Python
Add-ActivePythonRuntimeToPath

if ($PackagePath) {
  $ResolvedPackage = Resolve-Path $PackagePath
  & $PythonCommand -m pip install $ResolvedPackage.Path
} else {
  $ProjectRoot = Get-ProjectRoot
  $BundledWheel = Get-BundledWheel
  # A distributable bundle must be reproducible even when it is extracted
  # somewhere under a developer checkout.  Prefer the wheel shipped beside
  # this script over an ancestor pyproject.toml; otherwise the same ZIP can
  # silently become an editable install on the build machine.
  if ($BundledWheel) {
    & $PythonCommand -m pip install $BundledWheel.FullName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    # A regenerated bundle may keep the same package version. After pip has
    # satisfied dependencies above, replace only this distribution so the
    # recorded runtime is guaranteed to match the wheel shipped here.
    & $PythonCommand -m pip install --force-reinstall --no-deps $BundledWheel.FullName
  } elseif (-not $ProjectRoot) {
    throw "Could not find pyproject.toml above this bundle and no bundled wheel was found. Run from a bundle inside the repository, pass -PackagePath path\to\reg_rag_preprocessor*.whl, or regenerate the zip with --include-wheel."
  } elseif ($NoEditable) {
    $Wheel = Get-ChildItem -Path (Join-Path $ProjectRoot "dist") -Filter "reg_rag_preprocessor-*.whl" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending |
      Select-Object -First 1
    if (-not $Wheel) {
      throw "No wheel found under $ProjectRoot\dist. Build one first, omit -NoEditable, or regenerate the bundle zip with --include-wheel."
    }
    & $PythonCommand -m pip install $Wheel.FullName
  } else {
    & $PythonCommand -m pip install -e $ProjectRoot
  }
}

if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

Add-ActivePythonRuntimeToPath
Assert-McpCommands
Write-RuntimePythonMarker
Write-Host "reg-rag MCP console commands are installed and visible on PATH."
} finally {
  if ($StandaloneInstallLockAcquired) { $StandaloneInstallMutex.ReleaseMutex() }
  if ($StandaloneInstallMutex) { $StandaloneInstallMutex.Dispose() }
}
'''
    return (
        script.replace(
            "__RUNTIME_IDENTITY_MODULES__",
            _powershell_array_literal(RUNTIME_IDENTITY_MODULES),
        )
        .replace(
            "__RUNTIME_IDENTITY_BUILDER_BASE64__",
            _powershell_single_quoted_json(_runtime_identity_builder_base64()),
        )
    )


def _mcp_first_use_guide(server_name: str) -> str:
    return f"""PR MCP Builder MCP 연결 안내

등록된 MCP 이름: {server_name}

지원하는 정식 연결 방식은 세 가지입니다.

1. 로컬 stdio
   - Codex CLI: `codex_config_snippet.toml`을 `~/.codex/config.toml`에 반영
   - Claude Code: `claude_code_add_stdio.ps1` 실행
   - Claude Desktop: `claude_desktop_config.json`의 `mcpServers` 병합
   - 전달 ZIP에는 원 PC의 설치 EXE가 포함되지 않음
   - 새 대상 PC에서 번들 옆 호환 EXE가 없으면 Python 3.11+ 설치 후 `install_local_package.ps1`을 먼저 실행
   - ZIP에 wheel이 있어도 wheel은 Python 실행 환경이 아니라 패키지 설치 파일임
   - 등록 후 클라이언트를 완전히 종료·재실행하고 실제 도구 호출로 검증

2. ChatGPT 웹 원격 HTTPS MCP
   - 생성된 staging의 `api/index.py`와 승인된 runtime bundle을 Vercel에 배포
   - 공개 endpoint는 `https://<deployment>/mcp`
   - ChatGPT 웹에서 Developer mode 사용 가능 여부와 플랜·워크스페이스 권한을 먼저 확인
   - Pro는 Developer mode에서 read/fetch MCP를 연결할 수 있고, full MCP는 Business·Enterprise·Edu 대상
   - Settings > Apps > Advanced settings에서 Developer mode를 켠 뒤 Apps > Create에서 HTTPS URL과 승인된 인증만 등록
   - ChatGPT는 로컬 MCP 서버에 직접 연결하지 않음
   - 사설망·온프레미스·개발 PC 서버는 OpenAI Secure MCP Tunnel 안내를 따름
   - 공식 조건: {CHATGPT_MCP_HELP_URL}
   - Secure MCP Tunnel: {CHATGPT_SECURE_MCP_TUNNEL_URL}

3. Claude 원격 HTTPS MCP
   - Vercel에 배포한 같은 승인 endpoint를 Claude Connector에 URL과 승인된 인증으로 등록

호환성 주의
- `chatgpt_desktop_local_mcp.json`은 이전 번들 판독기용 경고 파일일 뿐 실행 설정이 아님
- 이 파일의 `support_status=unsupported`, `direct_local_supported=false`를 확인하고 로컬 Command·Arguments를 ChatGPT에 입력하지 않음

운영 파일
- 로컬 서버: `run_mcp_stdio_server.ps1`
- Vercel 배포 준비: `reg-rag-mcp-vercel-stage`
- 연결 진단: `doctor_mcp_connection.ps1`
- transport 검증: `validate_mcp_smoke.ps1`
- 전체 설정: `mcp_config.bundle.json`

검증 예시
- `{server_name}`의 `search`로 규정을 찾습니다.
- 반환된 첫 번째 id를 `{server_name}`의 `fetch`로 조회해 원문과 출처를 확인합니다.

보안 원칙
- 승인된 청크만 runtime bundle에 포함합니다.
- 로컬 절대경로와 비밀값을 대화에 입력하지 않습니다.
- 토큰은 환경변수 또는 secret manager에만 둡니다.
- 로컬 `data/` 전체를 Vercel에 올리지 않습니다.
"""


def _with_connect_wizard_preferred_runtime(
    script: str,
    *,
    preferred_python: str | Path | None,
    preferred_project_root: str | Path | None,
) -> str:
    preferred_python_value = str(preferred_python or "").strip()
    preferred_project_root_value = str(preferred_project_root or "").strip()
    return script.replace(
        '$PreferredPython = ""',
        f"$PreferredPython = {_powershell_single_quoted_json(preferred_python_value)}",
    ).replace(
        '$PreferredProjectRoot = ""',
        f"$PreferredProjectRoot = {_powershell_single_quoted_json(preferred_project_root_value)}",
    )


def _with_preferred_mcp_command_functions(
    script: str,
    *,
    preferred_python: str | Path | None,
    preferred_project_root: str | Path | None,
) -> str:
    preferred_python_value = str(preferred_python or "").strip()
    preferred_project_root_value = str(preferred_project_root or "").strip()
    if not preferred_python_value or not preferred_project_root_value:
        return script
    command_scripts = {
        "reg-rag-mcp-server": r"scripts\run_regulation_mcp.py",
        "reg-rag-mcp-doctor": r"scripts\check_mcp_connection_readiness.py",
        "reg-rag-mcp-smoke": r"scripts\run_mcp_smoke.py",
        "reg-rag-mcp-transport-smoke": r"scripts\run_mcp_transport_smoke.py",
        "reg-rag-mcp-client-config-smoke": r"scripts\run_mcp_client_config_smoke.py",
        "reg-rag-mcp-codex-app-server-check": r"scripts\check_codex_app_server_mcp.py",
        "reg-rag-mcp-index-visibility": r"scripts\audit_mcp_index_visibility.py",
    }
    lines = [
        "$script:McpPreferredPython = " + _powershell_single_quoted_json(preferred_python_value),
        "$script:McpPreferredProjectRoot = " + _powershell_single_quoted_json(preferred_project_root_value),
        'function Invoke-McpPreferredSource([string]$PythonPath, [string]$ProjectRoot, [string]$ScriptPath, [object[]]$Arguments) {',
        '  $HadPythonPath = Test-Path Env:PYTHONPATH',
        '  $PreviousPythonPath = $env:PYTHONPATH',
        '  try {',
        '    $env:PYTHONPATH = if ($PreviousPythonPath) { "$ProjectRoot;$PreviousPythonPath" } else { $ProjectRoot }',
        '    & $PythonPath $ScriptPath @Arguments',
        '    $InvocationExitCode = $LASTEXITCODE',
        '  } finally {',
        '    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
        '  }',
        '  $global:LASTEXITCODE = $InvocationExitCode',
        '}',
        'if (Test-Path -LiteralPath $script:McpPreferredPython) {',
    ]
    for command_name, relative_script in command_scripts.items():
        variable_name = "McpPreferred" + "".join(part.title() for part in command_name.split("-")) + "Script"
        lines.extend(
            [
                f"  $script:{variable_name} = Join-Path $script:McpPreferredProjectRoot "
                + _powershell_single_quoted_json(relative_script),
                f"  if (Test-Path -LiteralPath $script:{variable_name}) {{",
                f"    function {command_name} {{ Invoke-McpPreferredSource $script:McpPreferredPython $script:McpPreferredProjectRoot $script:{variable_name} $args }}",
                "  }",
            ]
        )
    lines.append("}")
    bootstrap = "\n".join(lines)
    marker = '$ErrorActionPreference = "Stop"'
    if marker in script:
        return script.replace(marker, marker + "\n" + bootstrap, 1)
    return bootstrap + "\n" + script


def _connect_wizard_script(
    *,
    server_name: str,
    local_stdio_server_args: list[object] | None = None,
    local_stdio_doctor_args: list[object] | None = None,
) -> str:
    embedded_config = {
        "mcpServers": {
            server_name: {
                "type": "stdio",
                "command": "powershell.exe",
                "args": [
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "run_mcp_stdio_server.ps1",
                    *[str(value) for value in (local_stdio_server_args or [])],
                ],
            }
        }
    }
    embedded_config_base64 = base64.b64encode(
        json.dumps(embedded_config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    script = r'''param(
  [ValidateSet("menu", "install", "claude-desktop", "claude-code", "codex", "chatgpt-desktop-direct", "chatgpt-desktop-local", "chatgpt-remote", "chatgpt-desktop", "chatgpt-https", "claude-remote", "claude-api", "doctor")]
  [string]$Target = "menu",
  [string]$CodexConfigPath = "",
  [switch]$InstallClaudeDesktop,
  [switch]$InstallCodex,
  [switch]$ValidateClaudeDesktop,
  [switch]$InstallPackage
)

$ErrorActionPreference = "Stop"
__FILE_SHA256_FUNCTION__
$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerName = "__SERVER_NAME__"
$EmbeddedClaudeDesktopConfigBase64 = "__EMBEDDED_CLAUDE_DESKTOP_CONFIG_BASE64__"
$EmbeddedCodexConfigBase64 = "__EMBEDDED_CODEX_CONFIG_BASE64__"
$PreferredPython = ""
$PreferredProjectRoot = ""
$InstallationAttemptId = [Guid]::NewGuid().ToString("N")
$script:CodexLoaderVerified = $false
$script:CodexCliResolutionAttempted = $false
$script:ResolvedCodexCliExecutable = $null
$script:ConnectionTarget = $Target
$McpCommandScripts = @{
  "reg-rag-mcp-server" = "scripts\run_regulation_mcp.py"
  "reg-rag-mcp-doctor" = "scripts\check_mcp_connection_readiness.py"
  "reg-rag-mcp-smoke" = "scripts\run_mcp_smoke.py"
  "reg-rag-mcp-codex-app-server-check" = "scripts\check_codex_app_server_mcp.py"
  "reg-rag-mcp-desktop-recognition-check" = "scripts\check_chatgpt_desktop_recognition.py"
  "reg-rag-mcp-client-config-smoke" = "scripts\run_mcp_client_config_smoke.py"
  "reg-rag-mcp-index-visibility" = "scripts\audit_mcp_index_visibility.py"
}

function BundlePath([string]$Name) {
  return Join-Path $BundleDir $Name
}

function Write-Utf8NoBom([string]$LiteralPath, [string]$Value) {
  $Parent = Split-Path -Parent $LiteralPath
  if ($Parent) { New-Item -ItemType Directory -Force -Path $Parent | Out-Null }
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($LiteralPath, $Value, $Utf8NoBom)
}

function Write-AtomicUtf8NoBom([string]$LiteralPath, [string]$Value) {
  $Parent = Split-Path -Parent $LiteralPath
  if ($Parent) { New-Item -ItemType Directory -Force -Path $Parent | Out-Null }
  $TemporaryPath = Join-Path $Parent (".{0}.{1}.{2}.tmp" -f ([System.IO.Path]::GetFileName($LiteralPath)), $PID, [Guid]::NewGuid().ToString("N"))
  $ReplaceBackupPath = Join-Path $Parent (".{0}.{1}.{2}.replace-bak" -f ([System.IO.Path]::GetFileName($LiteralPath)), $PID, [Guid]::NewGuid().ToString("N"))
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  try {
    [System.IO.File]::WriteAllText($TemporaryPath, $Value, $Utf8NoBom)
    if (Test-Path -LiteralPath $LiteralPath) {
      # Windows PowerShell 5.1 rejects a null backup path for File.Replace.
      # Keep the replacement backup in the same directory so the operation
      # remains atomic, then remove this internal backup after success.
      [System.IO.File]::Replace($TemporaryPath, $LiteralPath, $ReplaceBackupPath, $true)
    } else {
      Move-Item -LiteralPath $TemporaryPath -Destination $LiteralPath
    }
  } finally {
    if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -LiteralPath $TemporaryPath -Force }
    if (Test-Path -LiteralPath $ReplaceBackupPath) { Remove-Item -LiteralPath $ReplaceBackupPath -Force }
  }
}

function Restore-FileAtomically([string]$BackupPath, [string]$LiteralPath) {
  $Parent = Split-Path -Parent $LiteralPath
  $TemporaryPath = Join-Path $Parent (".{0}.{1}.{2}.restore-tmp" -f ([System.IO.Path]::GetFileName($LiteralPath)), $PID, [Guid]::NewGuid().ToString("N"))
  $ReplaceBackupPath = Join-Path $Parent (".{0}.{1}.{2}.restore-bak" -f ([System.IO.Path]::GetFileName($LiteralPath)), $PID, [Guid]::NewGuid().ToString("N"))
  try {
    Copy-Item -LiteralPath $BackupPath -Destination $TemporaryPath -Force
    if (Test-Path -LiteralPath $LiteralPath) {
      [System.IO.File]::Replace($TemporaryPath, $LiteralPath, $ReplaceBackupPath, $true)
    } else {
      Move-Item -LiteralPath $TemporaryPath -Destination $LiteralPath
    }
    $ExpectedHash = Get-McpFileSha256 $BackupPath
    $ActualHash = Get-McpFileSha256 $LiteralPath
    if (-not [string]::Equals($ExpectedHash, $ActualHash, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw "Restored file hash does not match the prior backup."
    }
  } finally {
    if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -LiteralPath $TemporaryPath -Force }
    if (Test-Path -LiteralPath $ReplaceBackupPath) { Remove-Item -LiteralPath $ReplaceBackupPath -Force }
  }
}

function Write-JsonUtf8NoBom([string]$LiteralPath, [object]$Value, [int]$Depth = 50) {
  $Json = ($Value | ConvertTo-Json -Depth $Depth) + [Environment]::NewLine
  Write-Utf8NoBom $LiteralPath $Json
}

function Get-SingleArgumentValue([object[]]$Arguments, [string]$Flag) {
  $Matches = @()
  for ($Index = 0; $Index -lt ($Arguments.Count - 1); $Index++) {
    if ([string]$Arguments[$Index] -eq $Flag) {
      $Matches += [string]$Arguments[$Index + 1]
    }
  }
  if ($Matches.Count -ne 1) { return $null }
  return $Matches[0]
}

function Test-SamePath([string]$Left, [string]$Right) {
  if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
    return $false
  }
  try {
    $LeftFull = [System.IO.Path]::GetFullPath($Left).TrimEnd('\')
    $RightFull = [System.IO.Path]::GetFullPath($Right).TrimEnd('\')
    return [string]::Equals($LeftFull, $RightFull, [System.StringComparison]::OrdinalIgnoreCase)
  } catch {
    return $false
  }
}

function Test-SameMcpArguments([object[]]$Actual, [object[]]$Expected) {
  $ActualValues = @($Actual | ForEach-Object { [string]$_ })
  $ExpectedValues = @($Expected | ForEach-Object { [string]$_ })
  if ($ActualValues.Count -ne $ExpectedValues.Count) {
    return $false
  }
  for ($Index = 0; $Index -lt $ExpectedValues.Count; $Index++) {
    $PreviousExpected = if ($Index -gt 0) { $ExpectedValues[$Index - 1] } else { "" }
    if ($PreviousExpected -in @("-File", "--data-dir")) {
      if (-not (Test-SamePath $ActualValues[$Index] $ExpectedValues[$Index])) {
        return $false
      }
      continue
    }
    if (-not [string]::Equals($ActualValues[$Index], $ExpectedValues[$Index], [System.StringComparison]::Ordinal)) {
      return $false
    }
  }
  return $true
}

function Read-StrictUtf8Json([string]$LiteralPath) {
  $Bytes = [System.IO.File]::ReadAllBytes($LiteralPath)
  if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
    throw "$LiteralPath must be UTF-8 without BOM."
  }
  $StrictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
  $Json = $StrictUtf8.GetString($Bytes)
  return $Json | ConvertFrom-Json
}

function Read-JsonFile([string]$Name) {
  return Get-Content -LiteralPath (BundlePath $Name) -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Read-EmbeddedBundleServerConfig([string]$EncodedConfig, [string]$ProductLabel) {
  try {
    $Bytes = [Convert]::FromBase64String($EncodedConfig)
    $Json = [Text.Encoding]::UTF8.GetString($Bytes)
    return $Json | ConvertFrom-Json
  } catch {
    throw "The embedded $ProductLabel MCP configuration is invalid: $($_.Exception.Message)"
  }
}

function Read-ClaudeDesktopBundleServerConfig {
  try {
    return Read-JsonFile "claude_desktop_config.json"
  } catch {
    Write-Warning "Invalid Claude Desktop JSON; recovering the MCP entry."
    return Read-EmbeddedBundleServerConfig $EmbeddedClaudeDesktopConfigBase64 "Claude Desktop"
  }
}

function Read-CodexBundleServerConfig {
  return Read-EmbeddedBundleServerConfig $EmbeddedCodexConfigBase64 "Codex"
}

function Update-BundleStatus([hashtable]$Values) {
  $StatusPath = BundlePath "bundle_status.json"
  if (-not (Test-Path -LiteralPath $StatusPath)) {
    throw "bundle_status.json is missing; connection evidence cannot be recorded safely."
  }
  $StatusMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-BundleStatus")
  $StatusLockAcquired = $false
  try {
    try { $StatusLockAcquired = $StatusMutex.WaitOne([TimeSpan]::FromSeconds(10)) }
    catch [System.Threading.AbandonedMutexException] { $StatusLockAcquired = $true }
    if (-not $StatusLockAcquired) {
      throw "Timed out waiting to update bundle_status.json."
    }
    $Status = Get-Content -LiteralPath $StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($Name in $Values.Keys) {
      if ($Status.PSObject.Properties[$Name]) {
        $Status.$Name = $Values[$Name]
      } else {
        Add-Member -InputObject $Status -MemberType NoteProperty -Name $Name -Value $Values[$Name]
      }
    }
    $UpdatedAt = [DateTime]::UtcNow.ToString("o")
    if ($Status.PSObject.Properties["updated_at"]) {
      $Status.updated_at = $UpdatedAt
    } else {
      Add-Member -InputObject $Status -MemberType NoteProperty -Name "updated_at" -Value $UpdatedAt
    }
    $Json = ($Status | ConvertTo-Json -Depth 50) + [Environment]::NewLine
    Write-AtomicUtf8NoBom $StatusPath $Json
  } catch {
    throw "Could not update bundle_status.json: $($_.Exception.Message)"
  } finally {
    if ($StatusLockAcquired) { $StatusMutex.ReleaseMutex() }
    $StatusMutex.Dispose()
  }
}

function Start-LocalInstallationAttempt([string]$InstallationState) {
  Initialize-ClientConnectionAttempt
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installation_state = $InstallationState
    connection_state = "not_connected"
    process_started = $false
    mcp_initialized = $false
    tools_discovered = $false
    installation_failure_stage = $null
    installation_failure_reason = $null
    direct_config_registered = $false
    direct_config_loader_verified = $false
    loader_verification_state = "not_checked"
    loader_verification_reason = "not_checked"
    direct_config_rollback_performed = $false
    direct_config_path = $null
    installed_config_fingerprint = $null
    installed_config_transport_verified = $false
    installed_config_transport_runtime_fingerprint = $null
    generated_client_configs_transport_verified = $false
    claude_code_registered = $false
    claude_code_config_fingerprint = $null
    claude_code_loader_verified = $false
    claude_code_transport_verified = $false
    claude_code_transport_runtime_fingerprint = $null
    claude_code_registration_updated_at = $null
    claude_code_conversation_verified = $false
    claude_desktop_config_registered = $false
    claude_desktop_config_path = $null
    claude_desktop_config_fingerprint = $null
    claude_desktop_config_transport_verified = $false
    claude_desktop_config_transport_runtime_fingerprint = $null
    claude_desktop_registration_updated_at = $null
    claude_desktop_process_detected = $false
    claude_desktop_process_started_at = $null
    claude_desktop_restart_checked_at = $null
    claude_desktop_restart_required = $null
    claude_desktop_restart_status = "not_checked"
    claude_desktop_restarted_after_registration = $false
    claude_desktop_post_registration_log_session_observed = $false
    claude_desktop_server_name_observed = $false
    claude_desktop_loader_observed = $false
    claude_desktop_loader_verified = $false
    claude_desktop_conversation_verified = $false
    desktop_process_detected = $false
    desktop_process_started_at = $null
    desktop_mcp_registration_updated_at = $null
    desktop_restart_checked_at = $null
    desktop_restart_required = $null
    desktop_restart_status = "not_checked"
    desktop_restart_reason_code = "not_checked"
    desktop_app_server_loader_verified = $false
    fresh_codex_app_server_inventory_verified = $false
    fresh_codex_app_server_runtime_fingerprint = $null
    desktop_app_server_tool_count = 0
    desktop_app_server_tool_names = @()
    desktop_app_server_server_info = $null
    desktop_app_server_error = $null
    desktop_recognition_observation_status = "not_checked"
    desktop_recognition_observation_reason = "not_checked"
    desktop_restarted_after_registration = $false
    desktop_post_registration_log_session_observed = $false
    desktop_status_scan_request_observed = $false
    direct_stdio_verified = $false
    desktop_tool_scan_verified = $false
    conversation_attachment_verified = $false
    conversation_attachment_unverified = $true
    transport_end_to_end_verified = $false
    end_to_end_verified = $false
    remote_endpoint_verified = $false
    tool_scan_unverified = $true
  }
}

function Get-ClientConnectionStatusTarget {
  # Use the immutable script-parameter snapshot. PowerShell uses dynamic
  # scoping, so a child installer variable named $Target must never redirect
  # another client's v5 status transition.
  switch ($script:ConnectionTarget) {
    "claude-code" { return "claude-code" }
    "claude-desktop" { return "claude-desktop" }
    "codex" { return "codex" }
    "chatgpt-desktop-direct" { return "chatgpt-desktop-local" }
    "chatgpt-desktop-local" { return "chatgpt-desktop-local" }
    "chatgpt-desktop" { return "chatgpt-desktop-local" }
    default { return $null }
  }
}

function Invoke-ClientConnectionStatusCli([object[]]$Arguments) {
  $StatusPython = $null
  $StatusProjectRoot = $null
  $RuntimeMarkerPath = BundlePath "runtime_python.json"
  if (Test-Path -LiteralPath $RuntimeMarkerPath -PathType Leaf) {
    try {
      $RuntimeMarker = Get-Content -LiteralPath $RuntimeMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
      $RuntimeCandidate = [string]$RuntimeMarker.python_executable
      $RuntimeLeaf = [System.IO.Path]::GetFileNameWithoutExtension($RuntimeCandidate)
      if ([System.IO.Path]::IsPathRooted($RuntimeCandidate) -and
          $RuntimeLeaf -match "^python(?:\d+(?:\.\d+)*)?$" -and
          (Test-Path -LiteralPath $RuntimeCandidate -PathType Leaf)) {
        $StatusPython = (Resolve-Path -LiteralPath $RuntimeCandidate).Path
      }
    } catch {
      $StatusPython = $null
    }
  }
  if (-not $StatusPython) {
    $StatusProjectRoot = $PreferredProjectRoot
    $SourceStatusModule = if ($StatusProjectRoot) { Join-Path $StatusProjectRoot "scripts\mcp_client_status.py" } else { $null }
    if ($PreferredPython -and $SourceStatusModule -and
        (Test-Path -LiteralPath $PreferredPython -PathType Leaf) -and
        (Test-Path -LiteralPath $SourceStatusModule -PathType Leaf)) {
      $StatusPython = (Resolve-Path -LiteralPath $PreferredPython).Path
    }
  }
  if (-not $StatusPython) {
    $StatusRequiresClientTracking = $false
    try {
      $CurrentStatus = Read-JsonFile "bundle_status.json"
      $StatusRequiresClientTracking = [bool]$CurrentStatus.PSObject.Properties["client_connections"]
    } catch {
      $StatusRequiresClientTracking = $false
    }
    if ($StatusRequiresClientTracking) {
      throw "Client-specific MCP status tracking is required for this bundle, but its recorded Python runtime or scripts.mcp_client_status module is unavailable. Run install_local_package.ps1 with Python 3.11+, then retry connect_mcp_client.ps1."
    }
    Write-Warning "Client-specific status tracking is unavailable only because this is a pre-v5 legacy/source-only bundle; legacy verification will continue."
    return $false
  }
  $HadPythonPath = Test-Path Env:PYTHONPATH
  $PreviousPythonPath = $env:PYTHONPATH
  $HadSafePath = Test-Path Env:PYTHONSAFEPATH
  $PreviousSafePath = $env:PYTHONSAFEPATH
  $PreviousErrorActionPreference = $ErrorActionPreference
  $ExitCode = 1
  try {
    if ($StatusProjectRoot) { $env:PYTHONPATH = $StatusProjectRoot } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    $env:PYTHONSAFEPATH = "1"
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 1
    $CliOutput = @(& $StatusPython -m scripts.mcp_client_status @Arguments 2>&1)
    $ExitCode = [int]$global:LASTEXITCODE
    $CliOutput | Out-Host
  } finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }
  }
  if ($ExitCode -ne 0) {
    throw "Client-specific MCP status transition failed."
  }
  return $true
}

function Initialize-ClientConnectionAttempt {
  $ClientTarget = Get-ClientConnectionStatusTarget
  if (-not $ClientTarget) { return }
  $ClientStatusPath = BundlePath "bundle_status.json"
  if (-not (Invoke-ClientConnectionStatusCli @("init", "--status-file", $ClientStatusPath, "--server-name", $ServerName))) {
    return
  }
  $Status = Read-JsonFile "bundle_status.json"
  $ClientRecordProperty = $Status.client_connections.PSObject.Properties[$ClientTarget]
  $AlreadyStarted = $false
  if ($ClientRecordProperty) {
    $LastAttempt = $ClientRecordProperty.Value.last_attempt
    $AlreadyStarted = [string]$LastAttempt.id -eq $InstallationAttemptId -and [string]$LastAttempt.state -eq "in_progress"
  }
  if (-not $AlreadyStarted) {
    $null = Invoke-ClientConnectionStatusCli @(
      "begin", "--status-file", $ClientStatusPath,
      "--target", $ClientTarget,
      "--attempt-id", $InstallationAttemptId
    )
  }
}

function Complete-ClientConnectionAttempt(
  [string[]]$VerifiedStages,
  [string]$ConfigEntryFingerprint,
  [string]$RuntimeFingerprint
) {
  $ClientTarget = Get-ClientConnectionStatusTarget
  if (-not $ClientTarget) { return }
  $ClientStatusPath = BundlePath "bundle_status.json"
  $Arguments = @(
    "commit", "--status-file", $ClientStatusPath,
    "--target", $ClientTarget,
    "--attempt-id", $InstallationAttemptId,
    "--config-entry-fingerprint", $ConfigEntryFingerprint,
    "--bundle-location-fingerprint", $BundleDir,
    "--preserve-legacy-projection"
  )
  if (-not [string]::IsNullOrWhiteSpace($RuntimeFingerprint)) {
    $Arguments += @("--runtime-fingerprint", $RuntimeFingerprint)
  }
  foreach ($Stage in $VerifiedStages) {
    $Arguments += @("--verified-stage", $Stage)
  }
  $null = Invoke-ClientConnectionStatusCli $Arguments
}

function Fail-ClientConnectionAttempt(
  [string]$ReasonCode,
  [switch]$RolledBack
) {
  $ClientTarget = Get-ClientConnectionStatusTarget
  if (-not $ClientTarget) { return $false }
  $ClientStatusPath = BundlePath "bundle_status.json"
  try {
    $Status = Read-JsonFile "bundle_status.json"
    $ClientRecordProperty = $Status.client_connections.PSObject.Properties[$ClientTarget]
    if (-not $ClientRecordProperty) { return $false }
    $LastAttempt = $ClientRecordProperty.Value.last_attempt
    if ([string]$LastAttempt.id -ne $InstallationAttemptId -or [string]$LastAttempt.state -ne "in_progress") {
      return $false
    }
    $FailureAction = if ($RolledBack) { "fail-rolled-back" } else { "fail-unverified" }
    $null = Invoke-ClientConnectionStatusCli @(
      $FailureAction, "--status-file", $ClientStatusPath,
      "--target", $ClientTarget,
      "--attempt-id", $InstallationAttemptId,
      "--reason-code", $ReasonCode,
      "--preserve-legacy-projection"
    )
    return $true
  } catch {
    Write-Warning "Could not finalize the client-specific failure status; the original connection error will be preserved."
    return $false
  }
}

function Mark-CurrentAttemptFailedIfUnresolved([string]$ReasonCode) {
  $Status = Read-JsonFile "bundle_status.json"
  if ([string]$Status.installation_attempt_id -ne $InstallationAttemptId) {
    Write-Warning "The client-specific attempt did not start, so no top-level failure projection was written; the original connection error will be preserved."
    return $false
  }
  $UnresolvedStates = @("preflight_direct", "preflight_claude_code", "preflight_claude_desktop", "installing")
  if ($UnresolvedStates -contains [string]$Status.installation_state) {
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = "failed_before_verified_install"
      connection_state = "failed"
      loader_verification_state = "failed"
      loader_verification_reason = $ReasonCode
      direct_config_registered = $false
      direct_config_loader_verified = $false
          installed_config_transport_verified = $false
      direct_stdio_verified = $false
        generated_client_configs_transport_verified = $false
      claude_code_registered = $false
      claude_code_config_fingerprint = $null
      claude_code_loader_verified = $false
      claude_code_transport_verified = $false
      claude_code_transport_runtime_fingerprint = $null
      claude_code_registration_updated_at = $null
      claude_code_conversation_verified = $false
      claude_desktop_config_registered = $false
      claude_desktop_config_path = $null
      claude_desktop_config_fingerprint = $null
      claude_desktop_config_transport_verified = $false
      claude_desktop_config_transport_runtime_fingerprint = $null
      claude_desktop_registration_updated_at = $null
      claude_desktop_process_detected = $false
      claude_desktop_process_started_at = $null
      claude_desktop_restart_checked_at = $null
      claude_desktop_restart_required = $null
      claude_desktop_restart_status = "not_checked"
      claude_desktop_restarted_after_registration = $false
      claude_desktop_post_registration_log_session_observed = $false
      claude_desktop_server_name_observed = $false
      claude_desktop_loader_observed = $false
      claude_desktop_loader_verified = $false
      claude_desktop_conversation_verified = $false
      transport_end_to_end_verified = $false
      desktop_tool_scan_verified = $false
      conversation_attachment_verified = $false
      end_to_end_verified = $false
    }
  }
  $null = Fail-ClientConnectionAttempt $ReasonCode
  return $true
}

function Get-ChatGptDesktopRestartState {
  param(
    [Parameter(Mandatory = $true)]
    [DateTimeOffset]$RegistrationUpdatedAtUtc,
    [Parameter(Mandatory = $false)]
    [AllowNull()]
    [object[]]$Processes
  )
  $CheckedAtUtc = [DateTimeOffset]::UtcNow
  if (-not $PSBoundParameters.ContainsKey("Processes")) {
    try {
      $Processes = @(Get-Process -Name "ChatGPT" -ErrorAction SilentlyContinue)
    } catch {
      return [pscustomobject]@{
        desktop_process_detected = $false
        desktop_process_started_at = $null
        desktop_restart_checked_at = $CheckedAtUtc.ToString("o")
        desktop_restart_required = $null
        desktop_restart_status = "unknown"
        desktop_restart_reason_code = "process_query_failed"
      }
    }
  } else {
    $Processes = @($Processes)
  }
  if ($Processes.Count -eq 0) {
    return [pscustomobject]@{
      desktop_process_detected = $false
      desktop_process_started_at = $null
      desktop_restart_checked_at = $CheckedAtUtc.ToString("o")
      desktop_restart_required = $false
      desktop_restart_status = "not_running"
      desktop_restart_reason_code = "desktop_not_running"
    }
  }
  $StartTimesUtc = @()
  foreach ($DesktopProcess in $Processes) {
    try {
      $StartTimesUtc += ([DateTimeOffset]$DesktopProcess.StartTime).ToUniversalTime()
    } catch {
      # A short-lived or access-restricted renderer must not abort installation.
    }
  }
  if ($StartTimesUtc.Count -eq 0) {
    return [pscustomobject]@{
      desktop_process_detected = $true
      desktop_process_started_at = $null
      desktop_restart_checked_at = $CheckedAtUtc.ToString("o")
      desktop_restart_required = $null
      desktop_restart_status = "unknown"
      desktop_restart_reason_code = "process_start_unavailable"
    }
  }
  $EarliestStartUtc = $StartTimesUtc | Sort-Object | Select-Object -First 1
  $RestartRequired = $EarliestStartUtc -lt $RegistrationUpdatedAtUtc
  return [pscustomobject]@{
    desktop_process_detected = $true
    desktop_process_started_at = $EarliestStartUtc.ToString("o")
    desktop_restart_checked_at = $CheckedAtUtc.ToString("o")
    desktop_restart_required = $RestartRequired
    desktop_restart_status = $(if ($RestartRequired) { "required" } else { "up_to_date" })
    desktop_restart_reason_code = $(
      if ($RestartRequired) { "process_predates_mcp_registration" }
      else { "process_started_after_mcp_registration" }
    )
  }
}

function Get-BundleDataDir {
  $Path = Join-Path $BundleDir "data"
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Bundled data directory was not found: $Path"
  }
  return $Path
}

function Set-McpBundlePaths([object]$Config, [string]$DataDir, [string]$LauncherPath) {
  if (-not $Config) {
    return $Config
  }
  if ($Config.PSObject.Properties["args"] -and $Config.args) {
    for ($Index = 0; $Index -lt ($Config.args.Count - 1); $Index++) {
      if ($Config.args[$Index] -eq "--data-dir") {
        $Config.args[$Index + 1] = $DataDir
      }
      if ($Config.args[$Index] -eq "-File" -and (Split-Path -Leaf $Config.args[$Index + 1]) -eq "run_mcp_stdio_server.ps1") {
        $Config.args[$Index + 1] = $LauncherPath
      }
    }
  }
  if ($Config.PSObject.Properties["serverCommand"] -and $Config.serverCommand) {
    Set-McpBundlePaths $Config.serverCommand $DataDir $LauncherPath | Out-Null
  }
  if ($Config.PSObject.Properties["mcpServers"] -and $Config.mcpServers) {
    foreach ($Server in @($Config.mcpServers.PSObject.Properties)) {
      Set-McpBundlePaths $Server.Value $DataDir $LauncherPath | Out-Null
    }
  }
  return $Config
}

function Run-Script([string]$Name) {
  $Path = BundlePath $Name
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Missing generated file: $Name"
  }
  $global:LASTEXITCODE = 0
  & $Path
  $ScriptExitCode = $LASTEXITCODE
  if ($ScriptExitCode -ne 0) {
    throw "$Name failed with exit code $ScriptExitCode."
  }
}

function Test-CoreCommands {
  return Test-NamedCommands @("reg-rag-mcp-server", "reg-rag-mcp-doctor", "reg-rag-mcp-smoke", "reg-rag-mcp-index-visibility")
}

function Test-DoctorCommands {
  return Test-NamedCommands @("reg-rag-mcp-doctor")
}

__RUNTIME_IDENTITY_VALIDATOR__

function Get-RecordedRuntimePython([string]$RequiredModule) {
  $MarkerPath = BundlePath "runtime_python.json"
  if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) { return $null }
  try {
    $Marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $null = [DateTimeOffset]::Parse([string]$Marker.written_at)
    $Candidate = [string]$Marker.python_executable
    if (-not [System.IO.Path]::IsPathRooted($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
      throw "runtime_python.json does not point to an available Python executable."
    }
    $Leaf = [System.IO.Path]::GetFileNameWithoutExtension($Candidate)
    if ($Leaf -notmatch '^python(?:\d+(?:\.\d+)*)?$') {
      throw "runtime_python.json does not point to a Python executable."
    }
    $ResolvedPython = (Resolve-Path -LiteralPath $Candidate).Path
    if (-not (Test-RuntimeMarkerIdentity $ResolvedPython $Marker)) {
      throw "The recorded MCP runtime command-module identity does not match this installation. Re-run install_local_package.ps1."
    }
    return $ResolvedPython
  } catch {
    throw "The recorded MCP runtime is invalid: $($_.Exception.Message)"
  }
}

function Get-McpCommandInvocation([string]$Name) {
  if ($McpCommandScripts.ContainsKey($Name)) {
    $RelativeScript = [string]$McpCommandScripts[$Name]
    $ModuleName = ($RelativeScript -replace '\\', '.' -replace '\.py$', '')
    $RecordedPython = Get-RecordedRuntimePython $ModuleName
    if ($RecordedPython) {
      return @($RecordedPython, "-m", $ModuleName)
    }
  }
  if ($PreferredPython -and $PreferredProjectRoot -and $McpCommandScripts.ContainsKey($Name)) {
    $ScriptPath = Join-Path $PreferredProjectRoot $McpCommandScripts[$Name]
    if ((Test-Path -LiteralPath $PreferredPython) -and (Test-Path -LiteralPath $ScriptPath)) {
      return @($PreferredPython, $ScriptPath)
    }
  }
  $Resolved = Get-Command $Name -ErrorAction SilentlyContinue
  if ($Resolved) {
    return @($Resolved.Source)
  }
  return @()
}

function Invoke-McpCommand([string]$Name, [object[]]$Arguments, [switch]$SuppressOutput) {
  $Invocation = @(Get-McpCommandInvocation $Name)
  if ($Invocation.Count -eq 0) {
    throw "$Name was not found on PATH and no generated project runtime fallback is available."
  }
  $Executable = $Invocation[0]
  $PrefixArgs = @()
  if ($Invocation.Count -gt 1) {
    $PrefixArgs = @($Invocation[1..($Invocation.Count - 1)])
  }
  $MarkerModuleInvocation = $PrefixArgs -contains "-m"
  $PreferredSourceInvocation = -not $MarkerModuleInvocation -and $PrefixArgs.Count -eq 1 -and $PreferredProjectRoot
  $HadPythonPath = Test-Path Env:PYTHONPATH
  $PreviousPythonPath = $env:PYTHONPATH
  $HadSafePath = Test-Path Env:PYTHONSAFEPATH
  $PreviousSafePath = $env:PYTHONSAFEPATH
  try {
    if ($MarkerModuleInvocation) {
      Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
      $env:PYTHONSAFEPATH = "1"
    } elseif ($PreferredSourceInvocation) {
      $env:PYTHONPATH = if ($PreviousPythonPath) { "$PreferredProjectRoot;$PreviousPythonPath" } else { $PreferredProjectRoot }
    }
    if ($SuppressOutput) {
      $null = @(& $Executable @PrefixArgs @Arguments 2>&1)
    } else {
      & $Executable @PrefixArgs @Arguments | Out-Host
    }
    $CommandExitCode = $LASTEXITCODE
  } finally {
    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }
  }
  return [int]$CommandExitCode
}

function Test-NamedCommands([string[]]$Names) {
  $Missing = @()
  foreach ($Name in $Names) {
    if (@(Get-McpCommandInvocation $Name).Count -eq 0) {
      $Missing += $Name
    }
  }
  return $Missing
}

function Show-InstallHint([object[]]$Missing) {
  Write-Warning "MCP commands are unavailable from PATH and the generated project runtime: $($Missing -join ', ')"
  Write-Host "Install the bundled package once:"
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f (BundlePath 'install_local_package.ps1'))
  Write-Host "Or rerun this wizard with -InstallPackage."
}

function Warn-IfCoreCommandsMissing {
  return Warn-IfCommandsMissing (Test-CoreCommands)
}

function Warn-IfDoctorCommandsMissing {
  return Warn-IfCommandsMissing (Test-DoctorCommands)
}

function Warn-IfCommandsMissing([object[]]$Missing) {
  if ($Missing.Count -gt 0) {
    Show-InstallHint $Missing
    return $false
  }
  return $true
}

function Show-Header {
  Write-Host ""
  Write-Host "PR MCP Builder connection bundle: $ServerName"
  Write-Host "Bundle: $BundleDir"
  Write-Host ""
}

function Run-Doctor {
  Show-Header
  if (-not (Run-LocalStdioDoctor)) { exit 1 }
  Write-Host "Local MCP readiness check passed."
}

function Run-LocalStdioDoctor {
  $BundleDataDir = Get-BundleDataDir
  $LocalStdioDoctorArgs = __LOCAL_STDIO_DOCTOR_ARGS__
  $ExitCode = Invoke-McpCommand "reg-rag-mcp-doctor" $LocalStdioDoctorArgs
  return ($ExitCode -eq 0)
}

function Run-InstalledCodexConfigSmoke([string]$ConfigPath) {
  $StatusBeforeSmoke = Read-JsonFile "bundle_status.json"
  if ([string]$StatusBeforeSmoke.installation_attempt_id -ne $InstallationAttemptId) {
    throw "Installed-config smoke does not belong to the current installation attempt."
  }
  $SmokeRuntimeFingerprint = [string]$StatusBeforeSmoke.runtime_fingerprint
  $ReportPath = BundlePath "codex_installed_mcp_config_smoke.json"
  if (Test-Path -LiteralPath $ReportPath) { Remove-Item -LiteralPath $ReportPath -Force }
  $SmokeArgs = @(
    "--server-name", $ServerName,
    "--codex-config", $ConfigPath,
    "--timeout-seconds", "75",
    "--out-json", $ReportPath,
    "--fail-on-issue"
  )
  $SmokeStartedAtUtc = [DateTimeOffset]::UtcNow
  $ExitCode = Invoke-McpCommand "reg-rag-mcp-client-config-smoke" $SmokeArgs
  $Report = $null
  if (Test-Path -LiteralPath $ReportPath) {
    try { $Report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { $Report = $null }
  }
  $SmokeFinishedAtUtc = [DateTimeOffset]::UtcNow
  $SmokeResults = @($(if ($Report) { $Report.results } else { @() }))
  $ReportGeneratedAtUtc = $null
  if ($Report) {
    try { $ReportGeneratedAtUtc = [DateTimeOffset]::Parse([string]$Report.generated_at) }
    catch { $ReportGeneratedAtUtc = $null }
  }
  $ResultPathMatches = $false
  if ($SmokeResults.Count -eq 1) {
    try {
      $ExpectedConfigFullPath = [System.IO.Path]::GetFullPath($ConfigPath)
      $ReportedConfigFullPath = [System.IO.Path]::GetFullPath([string]$SmokeResults[0].config_path)
      $ResultPathMatches = [string]::Equals($ExpectedConfigFullPath, $ReportedConfigFullPath, [System.StringComparison]::OrdinalIgnoreCase)
    } catch { $ResultPathMatches = $false }
  }
  $StatusAfterSmoke = Read-JsonFile "bundle_status.json"
  $Verified = $ExitCode -eq 0 -and $Report -and
    [string]$Report.report_type -eq "mcp_client_config_smoke" -and
    $Report.passed -eq $true -and
    [string]$Report.server_name -eq $ServerName -and
    $ReportGeneratedAtUtc -and $ReportGeneratedAtUtc -ge $SmokeStartedAtUtc -and $ReportGeneratedAtUtc -le $SmokeFinishedAtUtc.AddSeconds(5) -and
    $SmokeResults.Count -eq 1 -and
    [string]$SmokeResults[0].label -eq "codex" -and
    $ResultPathMatches -and
    $SmokeResults[0].passed -eq $true -and
    $SmokeResults[0].contract_verified -eq $true -and
    [string]$StatusAfterSmoke.installation_attempt_id -eq $InstallationAttemptId -and
    [string]$StatusAfterSmoke.runtime_fingerprint -eq $SmokeRuntimeFingerprint -and
    $Report.launcher_ready -eq $true -and
    $Report.process_started -eq $true -and
    $Report.mcp_initialized -eq $true -and
    $Report.tools_discovered -eq $true -and
    $Report.end_to_end_verified -eq $true
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installed_config_transport_verified = [bool]$Verified
    installed_config_transport_runtime_fingerprint = $(if ($Verified) { $SmokeRuntimeFingerprint } else { $null })
    direct_stdio_verified = [bool]$Verified
    transport_end_to_end_verified = [bool]$Verified
    desktop_tool_scan_verified = $false
    conversation_attachment_verified = $false
    end_to_end_verified = $false
  }
  return [bool]$Verified
}

function Run-InstalledClaudeDesktopConfigSmoke([string]$ConfigPath) {
  $StatusBeforeSmoke = Read-JsonFile "bundle_status.json"
  if ([string]$StatusBeforeSmoke.installation_attempt_id -ne $InstallationAttemptId) {
    throw "Installed Claude Desktop config smoke does not belong to the current installation attempt."
  }
  $SmokeRuntimeFingerprint = [string]$StatusBeforeSmoke.runtime_fingerprint
  $ReportPath = BundlePath "claude_desktop_installed_mcp_config_smoke.json"
  if (Test-Path -LiteralPath $ReportPath) { Remove-Item -LiteralPath $ReportPath -Force }
  $SmokeArgs = @(
    "--server-name", $ServerName,
    "--claude-desktop-config", $ConfigPath,
    "--timeout-seconds", "75",
    "--out-json", $ReportPath,
    "--fail-on-issue"
  )
  $SmokeStartedAtUtc = [DateTimeOffset]::UtcNow
  $ExitCode = Invoke-McpCommand "reg-rag-mcp-client-config-smoke" $SmokeArgs
  $Report = $null
  if (Test-Path -LiteralPath $ReportPath) {
    try { $Report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { $Report = $null }
  }
  $SmokeFinishedAtUtc = [DateTimeOffset]::UtcNow
  $SmokeResults = @($(if ($Report) { $Report.results } else { @() }))
  $ReportGeneratedAtUtc = $null
  if ($Report) {
    try { $ReportGeneratedAtUtc = [DateTimeOffset]::Parse([string]$Report.generated_at) }
    catch { $ReportGeneratedAtUtc = $null }
  }
  $ResultPathMatches = $false
  if ($SmokeResults.Count -eq 1) {
    try {
      $ExpectedConfigFullPath = [System.IO.Path]::GetFullPath($ConfigPath)
      $ReportedConfigFullPath = [System.IO.Path]::GetFullPath([string]$SmokeResults[0].config_path)
      $ResultPathMatches = [string]::Equals($ExpectedConfigFullPath, $ReportedConfigFullPath, [System.StringComparison]::OrdinalIgnoreCase)
    } catch { $ResultPathMatches = $false }
  }
  $StatusAfterSmoke = Read-JsonFile "bundle_status.json"
  $Verified = $ExitCode -eq 0 -and $Report -and
    [string]$Report.report_type -eq "mcp_client_config_smoke" -and
    $Report.passed -eq $true -and
    [string]$Report.server_name -eq $ServerName -and
    $ReportGeneratedAtUtc -and $ReportGeneratedAtUtc -ge $SmokeStartedAtUtc -and $ReportGeneratedAtUtc -le $SmokeFinishedAtUtc.AddSeconds(5) -and
    $SmokeResults.Count -eq 1 -and
    [string]$SmokeResults[0].label -eq "claude_desktop" -and
    $ResultPathMatches -and
    $SmokeResults[0].passed -eq $true -and
    $SmokeResults[0].contract_verified -eq $true -and
    [string]$StatusAfterSmoke.installation_attempt_id -eq $InstallationAttemptId -and
    [string]$StatusAfterSmoke.runtime_fingerprint -eq $SmokeRuntimeFingerprint -and
    $Report.launcher_ready -eq $true -and
    $Report.process_started -eq $true -and
    $Report.mcp_initialized -eq $true -and
    $Report.tools_discovered -eq $true -and
    $Report.end_to_end_verified -eq $true
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    claude_desktop_config_transport_verified = [bool]$Verified
    claude_desktop_config_transport_runtime_fingerprint = $(if ($Verified -and -not [string]::IsNullOrWhiteSpace($SmokeRuntimeFingerprint)) { $SmokeRuntimeFingerprint } else { $null })
    direct_stdio_verified = [bool]$Verified
    transport_end_to_end_verified = [bool]$Verified
    claude_desktop_loader_verified = $false
    claude_desktop_conversation_verified = $false
    end_to_end_verified = $false
  }
  return [bool]$Verified
}

function Run-CodexAppServerMcpCheck {
  $StatusBeforeProbe = Read-JsonFile "bundle_status.json"
  if ([string]$StatusBeforeProbe.installation_attempt_id -ne $InstallationAttemptId) {
    throw "Codex app-server probe does not belong to the current installation attempt."
  }
  $ProbeRuntimeFingerprint = [string]$StatusBeforeProbe.runtime_fingerprint
  $DirectConfigProbe = $StatusBeforeProbe.direct_config_registered -eq $true
  $InstalledConfigFingerprint = [string]$StatusBeforeProbe.installed_config_fingerprint
  $ReportPath = BundlePath "codex_app_server_mcp_status.json"
  if (Test-Path -LiteralPath $ReportPath) { Remove-Item -LiteralPath $ReportPath -Force }
  $CodexExecutable = Resolve-CodexCliExecutable
  if ([string]::IsNullOrWhiteSpace($CodexExecutable)) {
    throw "A trusted executable Codex host CLI is unavailable for the fresh app-server probe."
  }
  $ProbeStartedAtUtc = [DateTimeOffset]::UtcNow
  $CheckArgs = @(
    "--server-name", $ServerName,
    "--require-tool", "search",
    "--require-tool", "fetch",
    "--timeout-seconds", "75",
    "--codex-executable", $CodexExecutable,
    "--out-json", $ReportPath,
    "--fail-on-issue"
  )
  $ExitCode = Invoke-McpCommand "reg-rag-mcp-codex-app-server-check" $CheckArgs -SuppressOutput
  $Report = $null
  if (Test-Path -LiteralPath $ReportPath) {
    try {
      $Report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
      $Report = $null
    }
  }
  $RequiredTools = @("search", "fetch")
  $ToolNames = if ($Report -and $Report.tool_names) { @($Report.tool_names | ForEach-Object { [string]$_ }) } else { @() }
  $RequiredToolsFound = @($RequiredTools | Where-Object { $ToolNames -notcontains $_ }).Count -eq 0
  $GeneratedAtUtc = $null
  if ($Report -and $Report.generated_at) {
    try { $GeneratedAtUtc = [DateTimeOffset]::Parse([string]$Report.generated_at).ToUniversalTime() }
    catch { $GeneratedAtUtc = $null }
  }
  $ExpectedConfigPath = [System.IO.Path]::GetFullPath((Get-CodexConfigPath)).ToLowerInvariant()
  $ExpectedConfigBytes = [Text.Encoding]::UTF8.GetBytes($ExpectedConfigPath)
  $ConfigSha256 = [System.Security.Cryptography.SHA256]::Create()
  try {
    $ExpectedConfigFingerprint = -join ($ConfigSha256.ComputeHash($ExpectedConfigBytes) | ForEach-Object { $_.ToString("x2") })
  } finally {
    $ConfigSha256.Dispose()
  }
  $Provenance = if ($Report -and $Report.provenance) { $Report.provenance } else { $null }
  $ConfigScope = if ($Provenance -and $Provenance.config_scope) { $Provenance.config_scope } else { $null }
  $ExpectedExecutablePath = [System.IO.Path]::GetFullPath($CodexExecutable).ToLowerInvariant()
  $ExpectedExecutableBytes = [Text.Encoding]::UTF8.GetBytes($ExpectedExecutablePath)
  $ExecutableSha256 = [System.Security.Cryptography.SHA256]::Create()
  try {
    $ExpectedExecutableFingerprint = -join ($ExecutableSha256.ComputeHash($ExpectedExecutableBytes) | ForEach-Object { $_.ToString("x2") })
  } finally {
    $ExecutableSha256.Dispose()
  }
  $ExecutablePathVerified = $Provenance -and
    -not $Provenance.PSObject.Properties["executable_path"] -and
    [string]$Provenance.executable_path_sha256 -eq $ExpectedExecutableFingerprint -and
    [string]$Provenance.executable_file_name -eq [System.IO.Path]::GetFileName($CodexExecutable)
  $ConfigContentVerified = -not $DirectConfigProbe -or (
    $ConfigScope -and
    $ConfigScope.config_content_stable_during_probe -eq $true -and
    -not [string]::IsNullOrWhiteSpace($InstalledConfigFingerprint) -and
    [string]$ConfigScope.config_content_sha256 -eq $InstalledConfigFingerprint
  )
  $Verified = $ExitCode -eq 0 -and
    $Report -and
    [string]$Report.report_type -eq "codex_app_server_mcp_status" -and
    [string]$Report.probe_scope -eq "fresh_codex_app_server_process" -and
    -not [string]::IsNullOrWhiteSpace([string]$Report.probe_id) -and
    $GeneratedAtUtc -and $GeneratedAtUtc -ge $ProbeStartedAtUtc.AddSeconds(-2) -and
    $ExecutablePathVerified -and
    [int]$Provenance.process_id -gt 0 -and
    $ConfigScope -and $ConfigScope.config_exists -eq $true -and
    [string]$ConfigScope.config_path_sha256 -eq $ExpectedConfigFingerprint -and
    $ConfigContentVerified -and
    $Report.passed -eq $true -and
    $Report.app_server_initialized -eq $true -and
    $Report.status_list_received -eq $true -and
    $Report.server_found -eq $true -and
    [string]$Report.server_name -eq $ServerName -and
    $RequiredToolsFound
  $CurrentStatus = Read-JsonFile "bundle_status.json"
  if ([string]$CurrentStatus.installation_attempt_id -ne $InstallationAttemptId) {
    throw "Codex app-server evidence does not belong to the current installation attempt."
  }
  if ([string]$CurrentStatus.runtime_fingerprint -ne $ProbeRuntimeFingerprint) {
    $Verified = $false
  }
  $NextInstallationState = if ($Verified) { [string]$CurrentStatus.installation_state } else { "installed_loader_verified_pending_fresh_inventory" }
  $NextConnectionState = if ($Verified) { [string]$CurrentStatus.connection_state } else { "pending_fresh_loader_inventory" }
  if (-not $Verified) {
    # Registration, loader lookup, and stdio transport were already verified.
    # Close the v5 attempt first; the legacy pending-fresh projection written
    # below must remain authoritative after that partial commit.
    Complete-ClientConnectionAttempt @("registration", "loader", "transport") ([string]$CurrentStatus.installed_config_fingerprint) ([string]$CurrentStatus.runtime_fingerprint)
  }
  $SafeAppServerError = if ($Verified) {
    $null
  } elseif ($Report -and -not [string]::IsNullOrWhiteSpace([string]$Report.reason_code)) {
    [string]$Report.reason_code
  } else {
    "fresh_app_server_report_missing_or_invalid"
  }
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    fresh_codex_app_server_inventory_verified = [bool]$Verified
    fresh_codex_app_server_runtime_fingerprint = $(if ($Verified) { $ProbeRuntimeFingerprint } else { $null })
    desktop_app_server_loader_verified = [bool]$Verified
    desktop_app_server_tool_count = $(if ($Report) { [int]$Report.tool_count } else { 0 })
    desktop_app_server_tool_names = $ToolNames
    desktop_app_server_server_info = $(if ($Report) { $Report.server_info } else { $null })
    desktop_app_server_error = $SafeAppServerError
    installation_state = $NextInstallationState
    connection_state = $NextConnectionState
  }
  if (-not $Verified) {
    throw "Codex app-server did not initialize and expose the required MCP tools for $ServerName."
  }
  Write-Host "Codex app-server loaded $ServerName with $($ToolNames.Count) tools."
}

function Run-ChatGptDesktopRecognitionObservation([string]$ConfigPath) {
  if (@(Get-McpCommandInvocation "reg-rag-mcp-desktop-recognition-check").Count -eq 0) {
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      desktop_recognition_observation_status = "not_checked"
      desktop_recognition_observation_reason = "recognition_checker_unavailable"
      desktop_tool_scan_verified = $false
      conversation_attachment_verified = $false
      end_to_end_verified = $false
    }
    Write-Warning "Desktop restart/log observation checker is unavailable; restart and /mcp verification remain required."
    return
  }
  $ReportPath = BundlePath "chatgpt_desktop_recognition.json"
  if (Test-Path -LiteralPath $ReportPath) { Remove-Item -LiteralPath $ReportPath -Force }
  $ObservationArgs = @(
    "--bundle-status", (BundlePath "bundle_status.json"),
    "--config-path", $ConfigPath,
    "--out-json", $ReportPath
  )
  $ExitCode = Invoke-McpCommand "reg-rag-mcp-desktop-recognition-check" $ObservationArgs
  $Report = $null
  if ($ExitCode -eq 0 -and (Test-Path -LiteralPath $ReportPath)) {
    try { $Report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { $Report = $null }
  }
  $Process = if ($Report -and $Report.desktop_process) { $Report.desktop_process } else { $null }
  $Logs = if ($Report -and $Report.desktop_logs) { $Report.desktop_logs } else { $null }
  $StatusBeforeObservation = Read-JsonFile "bundle_status.json"
  if ([string]$StatusBeforeObservation.installation_attempt_id -ne $InstallationAttemptId) {
    throw "Desktop recognition evidence does not belong to the current installation attempt."
  }
  $ConfigObservation = if ($Report -and $Report.config_observation) { $Report.config_observation } else { $null }
  $ConfigFingerprintMatches = $ConfigObservation -and
    $ConfigObservation.exists -eq $true -and
    [string]$ConfigObservation.content_sha256 -eq [string]$StatusBeforeObservation.installed_config_fingerprint
  if ($StatusBeforeObservation.direct_config_registered -eq $true -and -not $ConfigFingerprintMatches) {
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = "installed_config_changed_revalidation_required"
      connection_state = "pending_config_revalidation"
      direct_config_registered = $false
      direct_config_loader_verified = $false
      loader_verification_state = "stale"
      loader_verification_reason = "installed_config_fingerprint_changed"
      installed_config_fingerprint = $null
      installed_config_transport_verified = $false
      installed_config_transport_runtime_fingerprint = $null
      direct_stdio_verified = $false
      transport_end_to_end_verified = $false
      fresh_codex_app_server_inventory_verified = $false
      fresh_codex_app_server_runtime_fingerprint = $null
      desktop_app_server_loader_verified = $false
      desktop_app_server_tool_count = 0
      desktop_app_server_tool_names = @()
      desktop_app_server_server_info = $null
      desktop_app_server_error = "installed_config_fingerprint_changed"
      desktop_tool_scan_verified = $false
      conversation_attachment_verified = $false
      conversation_attachment_unverified = $true
      tool_scan_unverified = $true
      end_to_end_verified = $false
    }
  }
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    desktop_recognition_observation_status = $(if ($Report) { [string]$Report.observation_status } else { "check_failed" })
    desktop_restart_required = $(if ($Process) { $Process.restart_required } else { $null })
    desktop_restarted_after_registration = [bool]($Process -and -not $Process.restart_required -and $Process.post_registration_process_count -gt 0)
    desktop_post_registration_log_session_observed = [bool]($Logs -and $Logs.post_registration_session_observed)
    desktop_status_scan_request_observed = [bool]($Logs -and $Logs.mcp_status_list_observed_without_error)
    desktop_tool_scan_verified = $false
    conversation_attachment_verified = $false
    end_to_end_verified = $false
  }
  if ($Report) {
    Write-Host "Desktop observation: $($Report.observation_status). This observes restart/status requests only, not tool exposure."
  } else {
    Write-Warning "Desktop restart/log observation could not be evaluated."
  }
}

function Install-LocalPackage {
  Show-Header
  $Path = BundlePath "install_local_package.ps1"
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Missing generated file: install_local_package.ps1"
  }
  $global:LASTEXITCODE = 0
  & $Path -ConnectionFlowLockHeld
  $ScriptExitCode = $LASTEXITCODE
  if ($ScriptExitCode -ne 0) {
    throw "install_local_package.ps1 failed with exit code $ScriptExitCode."
  }
}

function Invoke-WithLocalConnectionFlow([scriptblock]$Action) {
  $ConnectionFlowMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-LocalMcpConnectionFlow")
  $ConnectionFlowLockAcquired = $false
  try {
    try { $ConnectionFlowLockAcquired = $ConnectionFlowMutex.WaitOne([TimeSpan]::FromSeconds(180)) }
    catch [System.Threading.AbandonedMutexException] { $ConnectionFlowLockAcquired = $true }
    if (-not $ConnectionFlowLockAcquired) {
      throw "Timed out waiting for another local MCP installation or registration flow to finish."
    }
    & $Action
  } finally {
    if ($ConnectionFlowLockAcquired) { $ConnectionFlowMutex.ReleaseMutex() }
    $ConnectionFlowMutex.Dispose()
  }
}

function Get-ClaudeDesktopConfigPath {
  if ($env:APPDATA) {
    return Join-Path (Join-Path $env:APPDATA "Claude") "claude_desktop_config.json"
  }
  if ($HOME) {
    return Join-Path $HOME "Library/Application Support/Claude/claude_desktop_config.json"
  }
  throw "Cannot determine Claude Desktop config path. Manually merge claude_desktop_config.json."
}

function Get-CodexConfigPath {
  if ($CodexConfigPath) {
    return [System.IO.Path]::GetFullPath($CodexConfigPath)
  }
  if ($env:CODEX_HOME) {
    return Join-Path $env:CODEX_HOME "config.toml"
  }
  if ($env:USERPROFILE) {
    return Join-Path (Join-Path $env:USERPROFILE ".codex") "config.toml"
  }
  if ($HOME) {
    return Join-Path (Join-Path $HOME ".codex") "config.toml"
  }
  throw "Cannot determine Codex config path. Manually merge codex_config_snippet.toml."
}

function Format-TomlString([string]$Value) {
  return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Format-TomlKey([string]$Value) {
  if ($Value -match "^[A-Za-z0-9_-]+$") { return $Value }
  return Format-TomlString $Value
}

function Normalize-TomlSectionName([string]$Value) {
  return $Value.Trim().Trim('"').Trim("'")
}

function Get-BundleServerEntry {
  $Source = Read-CodexBundleServerConfig
  $Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")
  if (-not $Source.PSObject.Properties["mcpServers"]) {
    throw "Generated Codex MCP configuration does not contain mcpServers."
  }
  $Server = $Source.mcpServers.PSObject.Properties[$ServerName]
  if (-not $Server) {
    throw "Generated Codex MCP configuration does not contain server $ServerName."
  }
  return $Server.Value
}

function Build-CodexConfigSnippet {
  $Entry = Get-BundleServerEntry
  $Lines = @()
  $Lines += "# Generated by connect_mcp_client.ps1 from $BundleDir"
  $Lines += "# Re-run with -InstallPackage -Target codex -InstallCodex after moving or unzipping the MCP bundle."
  $Lines += "[mcp_servers.$(Format-TomlKey $ServerName)]"
  $Lines += "command = $(Format-TomlString ([string]$Entry.command))"
  $Lines += "startup_timeout_sec = 45"
  $Lines += "cwd = $(Format-TomlString $BundleDir)"
  $Lines += "args = ["
  foreach ($Arg in @($Entry.args)) {
    $Lines += "  $(Format-TomlString ([string]$Arg)),"
  }
  $Lines += "]"
  return ($Lines -join [Environment]::NewLine)
}

function Invoke-CodexCommandCapture([string]$Command, [string[]]$Arguments) {
  $PreviousErrorActionPreference = $ErrorActionPreference
  $PreviousConsoleOutputEncoding = [Console]::OutputEncoding
  $PreviousPowerShellOutputEncoding = $OutputEncoding
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  $CommandOutput = @()
  $CommandExitCode = 1
  try {
    $ErrorActionPreference = "Continue"
    [Console]::OutputEncoding = $Utf8NoBom
    $OutputEncoding = $Utf8NoBom
    $global:LASTEXITCODE = 1
    $CommandOutput = @(& $Command @Arguments 2>&1)
    $CommandExitCode = [int]$global:LASTEXITCODE
  } catch {
    $CommandOutput = @($_)
    $CommandExitCode = 1
  } finally {
    $OutputEncoding = $PreviousPowerShellOutputEncoding
    [Console]::OutputEncoding = $PreviousConsoleOutputEncoding
    $ErrorActionPreference = $PreviousErrorActionPreference
  }
  return [pscustomobject]@{
    ExitCode = $CommandExitCode
    Output = $CommandOutput
  }
}

function Test-CodexCommandVersion([string]$Command) {
  if ([string]::IsNullOrWhiteSpace($Command)) { return $false }
  $Probe = Invoke-CodexCommandCapture $Command @("--version")
  return $Probe.ExitCode -eq 0
}

function Test-IsWindowsAppsCodexCommand([string]$Candidate) {
  if ([string]::IsNullOrWhiteSpace($Candidate) -or -not [System.IO.Path]::IsPathRooted($Candidate)) {
    return $false
  }
  try { $CandidateFullPath = [System.IO.Path]::GetFullPath($Candidate) }
  catch { return $false }
  $BlockedRoots = @()
  if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $BlockedRoots += Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
  }
  foreach ($ProgramFilesRoot in @($env:ProgramFiles, $env:ProgramW6432)) {
    if (-not [string]::IsNullOrWhiteSpace($ProgramFilesRoot)) {
      $BlockedRoots += Join-Path $ProgramFilesRoot "WindowsApps"
    }
  }
  foreach ($BlockedRoot in $BlockedRoots) {
    try { $BlockedFullPath = [System.IO.Path]::GetFullPath($BlockedRoot).TrimEnd([char[]]"\/") }
    catch { continue }
    if ([string]::Equals($CandidateFullPath, $BlockedFullPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $CandidateFullPath.StartsWith($BlockedFullPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function Get-CodexPathCommandCandidate {
  $CommandInfos = @(Get-Command codex -All -ErrorAction SilentlyContinue)
  foreach ($CommandInfo in $CommandInfos) {
    $Candidate = if (-not [string]::IsNullOrWhiteSpace([string]$CommandInfo.Path)) {
      [string]$CommandInfo.Path
    } else {
      [string]$CommandInfo.Name
    }
    if (Test-IsWindowsAppsCodexCommand $Candidate) { continue }

    # npm's PowerShell/cmd shim is compatible with the interactive CLI, but a
    # fresh app-server subprocess needs the native executable. Prefer the native
    # binary belonging to that same PATH installation when it is present.
    if (-not [string]::IsNullOrWhiteSpace([string]$CommandInfo.Path) -and
        @(".cmd", ".ps1") -contains [System.IO.Path]::GetExtension([string]$CommandInfo.Path).ToLowerInvariant()) {
      $PackageRoot = Join-Path (Split-Path -Parent ([string]$CommandInfo.Path)) "node_modules\@openai\codex"
      if (Test-Path -LiteralPath $PackageRoot -PathType Container) {
        $NativeCandidates = @(
          Get-ChildItem -LiteralPath (Join-Path $PackageRoot "node_modules\@openai") -Directory -Filter "codex-win32-*" -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ChildItem -LiteralPath (Join-Path $_.FullName "vendor") -Recurse -File -Filter "codex.exe" -ErrorAction SilentlyContinue } |
            Sort-Object FullName
        )
        foreach ($NativeCandidate in $NativeCandidates) {
          if (Test-CodexCommandVersion $NativeCandidate.FullName) { return $NativeCandidate.FullName }
        }
      }
    }
    if (Test-CodexCommandVersion $Candidate) { return $Candidate }
  }
  return $null
}

function Test-CodexAppCacheCandidate([string]$CandidatePath, [string]$CanonicalRoot) {
  try {
    $RootItem = Get-Item -LiteralPath $CanonicalRoot -Force -ErrorAction Stop
    $CandidateItem = Get-Item -LiteralPath $CandidatePath -Force -ErrorAction Stop
    if (-not ($RootItem -is [System.IO.DirectoryInfo]) -or -not ($CandidateItem -is [System.IO.FileInfo])) {
      return $false
    }
    $RootFullPath = [System.IO.Path]::GetFullPath($RootItem.FullName).TrimEnd([char[]]"\/")
    $CandidateFullPath = [System.IO.Path]::GetFullPath($CandidateItem.FullName)
    $RootPrefix = $RootFullPath + [System.IO.Path]::DirectorySeparatorChar
    if (-not $CandidateFullPath.StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }
    $RelativeParts = @($CandidateFullPath.Substring($RootPrefix.Length).Split([char[]]"\/", [System.StringSplitOptions]::RemoveEmptyEntries))
    if ($RelativeParts.Count -ne 2 -or
        -not [string]::Equals($RelativeParts[1], "codex.exe", [System.StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }

    # Do not trust a junction or symlink that only appears to live below the
    # canonical app cache root.
    $ReachedRoot = $false
    $Cursor = $CandidateItem
    while ($Cursor) {
      if (($Cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
      $CursorFullPath = [System.IO.Path]::GetFullPath($Cursor.FullName).TrimEnd([char[]]"\/")
      if ([string]::Equals($CursorFullPath, $RootFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        $ReachedRoot = $true
        break
      }
      $Cursor = if ($Cursor -is [System.IO.DirectoryInfo]) {
        $Cursor.Parent
      } elseif ($Cursor -is [System.IO.FileInfo]) {
        $Cursor.Directory
      } else {
        $null
      }
    }
    if (-not $ReachedRoot) { return $false }

    $Signature = Get-AuthenticodeSignature -LiteralPath $CandidateFullPath -ErrorAction Stop
    $SignerSubject = if ($Signature.SignerCertificate) { [string]$Signature.SignerCertificate.Subject } else { "" }
    if ([string]$Signature.Status -ne "Valid" -or
        $SignerSubject.IndexOf("OpenAI", [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
      return $false
    }
    return Test-CodexCommandVersion $CandidateFullPath
  } catch {
    return $false
  }
}

function Get-CodexAppCacheCandidate {
  if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
      [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    return $null
  }
  $AppCacheRoot = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin"
  if (-not (Test-Path -LiteralPath $AppCacheRoot -PathType Container)) { return $null }
  try {
    $CanonicalRoot = (Get-Item -LiteralPath $AppCacheRoot -Force -ErrorAction Stop).FullName
    $VersionDirectories = @(Get-ChildItem -LiteralPath $CanonicalRoot -Directory -Force -ErrorAction Stop | Sort-Object LastWriteTimeUtc -Descending)
    foreach ($VersionDirectory in $VersionDirectories) {
      $Candidate = Join-Path $VersionDirectory.FullName "codex.exe"
      if (Test-CodexAppCacheCandidate $Candidate $CanonicalRoot) {
        return [System.IO.Path]::GetFullPath($Candidate)
      }
    }
  } catch {
    return $null
  }
  return $null
}

function Resolve-CodexCliExecutable {
  if ($script:CodexCliResolutionAttempted) { return $script:ResolvedCodexCliExecutable }
  $script:CodexCliResolutionAttempted = $true
  $PathCandidate = Get-CodexPathCommandCandidate
  if (-not [string]::IsNullOrWhiteSpace($PathCandidate)) {
    $script:ResolvedCodexCliExecutable = $PathCandidate
    return $script:ResolvedCodexCliExecutable
  }
  $AppCacheCandidate = Get-CodexAppCacheCandidate
  if (-not [string]::IsNullOrWhiteSpace($AppCacheCandidate)) {
    $script:ResolvedCodexCliExecutable = $AppCacheCandidate
    return $script:ResolvedCodexCliExecutable
  }
  $script:ResolvedCodexCliExecutable = $null
  return $null
}

function Invoke-CodexCli([string[]]$Arguments) {
  $Command = Resolve-CodexCliExecutable
  if ([string]::IsNullOrWhiteSpace($Command)) {
    return [pscustomobject]@{ ExitCode = 127; Output = @() }
  }
  return Invoke-CodexCommandCapture $Command $Arguments
}

function ConvertTo-CodexDiagnosticText([object[]]$Output, [int]$MaxLength = 8192) {
  $Text = (($Output | Out-String).Trim())
  if ([string]::IsNullOrWhiteSpace($Text)) { return "<no output>" }
  if ($Text.Length -le $MaxLength) { return $Text }
  return $Text.Substring(0, $MaxLength) + "...<truncated>"
}

function Test-CodexCliExecutable {
  return -not [string]::IsNullOrWhiteSpace((Resolve-CodexCliExecutable))
}

function Install-CodexConfig([string]$ConsumerName = "Codex CLI") {
  $CodexCliAvailable = [bool](Test-CodexCliExecutable)
  $script:DirectInstallFailureReason = "direct_install_failed"
  $Snippet = Build-CodexConfigSnippet
  $TargetPath = Get-CodexConfigPath
  $LauncherPath = BundlePath "run_mcp_stdio_server.ps1"
  $BundleDataDir = Get-BundleDataDir
  $GeneratedEntry = Get-BundleServerEntry
  $GeneratedProfileId = ""
  for ($Index = 0; $Index -lt ($GeneratedEntry.args.Count - 1); $Index++) {
    if ($GeneratedEntry.args[$Index] -eq "--profile-id") {
      $GeneratedProfileId = [string]$GeneratedEntry.args[$Index + 1]
      break
    }
  }
  $TargetDir = Split-Path -Parent $TargetPath
  New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
  $Existing = ""
  $HadExistingConfig = Test-Path -LiteralPath $TargetPath
  $BackupPath = $null
  if ($HadExistingConfig) {
    $BackupPath = "$TargetPath.bak-$(Get-Date -Format yyyyMMddHHmmssfff)"
    Copy-Item -LiteralPath $TargetPath -Destination $BackupPath
    $Existing = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8
    Write-Host "Backup created: $BackupPath"
  }
  Start-LocalInstallationAttempt "installing"
  try {
  $RemovedNames = [System.Collections.Generic.List[string]]::new()
  $Pattern = "(?ms)^\[mcp_servers\.(?<name>[^\]]+)\]\r?\n.*?(?=^\[|\z)"
  $TomlLauncherPath = $LauncherPath.Replace("\", "\\")
  $TomlBundleDataDir = $BundleDataDir.Replace("\", "\\")
  $Clean = [regex]::Replace($Existing, $Pattern, {
    param($Match)
    $ExistingName = Normalize-TomlSectionName $Match.Groups["name"].Value
    $ExistingRootName = ($ExistingName -split '\.', 2)[0]
    $ParentWasRemoved = $RemovedNames.Contains($ExistingRootName)
    $SameName = $ExistingName -eq $ServerName -or
      $ExistingName.StartsWith("$ServerName.", [System.StringComparison]::OrdinalIgnoreCase)
    $SameBundle = $Match.Value.IndexOf($LauncherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $Match.Value.IndexOf($TomlLauncherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $Match.Value.IndexOf($BundleDataDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $Match.Value.IndexOf($TomlBundleDataDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $LegacyDefaultForSameProfile = $ServerName -ne "govreg-local" -and
      $ExistingName -eq "govreg-local" -and
      $GeneratedProfileId -and
      $Match.Value.IndexOf($GeneratedProfileId, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if ($ParentWasRemoved -or $SameName -or $SameBundle -or $LegacyDefaultForSameProfile) {
      $RemovedNames.Add($ExistingRootName)
      return ""
    }
    return $Match.Value
  }).TrimEnd()
  $Output = if ([string]::IsNullOrWhiteSpace($Clean)) { $Snippet } else { $Clean + [Environment]::NewLine + [Environment]::NewLine + $Snippet }
  Write-AtomicUtf8NoBom $TargetPath ($Output + [Environment]::NewLine)
  $InstalledConfigFingerprint = "sha256:" + (Get-McpFileSha256 $TargetPath)
  $DirectRegistrationUpdatedAtUtc = [DateTimeOffset]::UtcNow
  $Written = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8
  $InstalledBlock = ""
  foreach ($Match in [regex]::Matches($Written, $Pattern)) {
    if ((Normalize-TomlSectionName $Match.Groups["name"].Value) -eq $ServerName) {
      $InstalledBlock = $Match.Value
      break
    }
  }
  $Installed = $InstalledBlock -and
    ($InstalledBlock.IndexOf($LauncherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $InstalledBlock.IndexOf($TomlLauncherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -and
    ($InstalledBlock.IndexOf($BundleDataDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $InstalledBlock.IndexOf($TomlBundleDataDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
  if (-not $Installed) {
    throw "$ConsumerName MCP config verification failed after writing: $TargetPath"
  }
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    direct_config_registered = $true
    direct_config_loader_verified = $false
    direct_config_rollback_performed = $false
    direct_config_path = $TargetPath
    installed_config_fingerprint = $InstalledConfigFingerprint
  }
  if (-not (Run-InstalledCodexConfigSmoke $TargetPath)) {
    throw "The installed $ConsumerName MCP config could not complete its initialize/tools/search/fetch transport contract."
  }
  $PostSmokeConfigFingerprint = if (Test-Path -LiteralPath $TargetPath -PathType Leaf) {
    "sha256:" + (Get-McpFileSha256 $TargetPath)
  } else {
    $null
  }
  if ([string]::IsNullOrWhiteSpace($PostSmokeConfigFingerprint) -or
      -not [string]::Equals($PostSmokeConfigFingerprint, $InstalledConfigFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "$ConsumerName MCP config changed during installed-config stdio verification; the prior state will be restored."
  }
  if (-not $CodexCliAvailable) {
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = "installed_pending_codex_loader_verification"
      connection_state = "configured_pending_codex_loader"
      direct_config_registered = $true
      direct_config_loader_verified = $false
      loader_verification_state = "blocked"
      loader_verification_reason = "codex_cli_unavailable"
      direct_config_rollback_performed = $false
      installed_config_fingerprint = $InstalledConfigFingerprint
      conversation_attachment_verified = $false
      end_to_end_verified = $false
    }
    $script:CodexLoaderVerified = $false
    Write-Host "$ConsumerName MCP config updated: $TargetPath"
    Write-Host "[CONFIGURED - CODEX LOADER VERIFICATION REQUIRED] MCP config readback and direct transport passed."
    Write-Warning "A trusted executable Codex host CLI was not found, so loader verification remains pending. The valid config was preserved instead of rolled back."
    Write-Host "Install or restart Codex CLI and verify $ServerName with /mcp in a new task."
    return
  }
  $LoaderResult = Invoke-CodexCli @("mcp", "get", $ServerName, "--json")
  $LoaderOutput = $LoaderResult.Output
  $LoaderExitCode = $LoaderResult.ExitCode
  if ($LoaderExitCode -ne 0) {
    throw "codex mcp get could not resolve the newly written direct MCP entry: $($LoaderOutput -join [Environment]::NewLine)"
  }
  try {
    $LoaderEntry = ($LoaderOutput -join [Environment]::NewLine) | ConvertFrom-Json
  } catch {
    throw "codex mcp get returned invalid JSON for the direct MCP entry: $($_.Exception.Message)"
  }
  if (-not $LoaderEntry) {
    throw "codex mcp get returned no JSON object for the direct MCP entry."
  }
  $LoaderTransport = if ($LoaderEntry.PSObject.Properties["transport"]) { $LoaderEntry.transport } else { $LoaderEntry }
  $LoaderArgs = @($LoaderTransport.args | ForEach-Object { [string]$_ })
  $LoaderLauncher = Get-SingleArgumentValue $LoaderArgs "-File"
  $LoaderDataDir = Get-SingleArgumentValue $LoaderArgs "--data-dir"
  $ExpectedArgs = @($GeneratedEntry.args | ForEach-Object { [string]$_ })
  $LoaderVerified = ([string]$LoaderEntry.name -eq $ServerName) -and
    ($LoaderEntry.enabled -eq $true) -and
    ([double]$LoaderEntry.startup_timeout_sec -eq 45) -and
    ([string]$LoaderTransport.type -eq "stdio") -and
    ([string]$LoaderTransport.command -ieq ([string]$GeneratedEntry.command)) -and
    (Test-SamePath ([string]$LoaderTransport.cwd) $BundleDir) -and
    (Test-SamePath $LoaderLauncher $LauncherPath) -and
    (Test-SamePath $LoaderDataDir $BundleDataDir) -and
    (Test-SameMcpArguments $LoaderArgs $ExpectedArgs)
  if (-not $LoaderVerified) {
    throw "codex mcp get resolved a disabled, stale, or contract-mismatched direct MCP entry for $ServerName."
  }
  $script:CodexLoaderVerified = $true
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installation_state = "installed_loader_verified"
    connection_state = "configured_pending_codex_conversation"
    direct_config_registered = $true
    direct_config_loader_verified = $true
    loader_verification_state = "verified"
    loader_verification_reason = $null
    direct_config_rollback_performed = $false
    direct_config_path = $TargetPath
    installed_config_fingerprint = $InstalledConfigFingerprint
  }
  $RemovedDuplicates = @($RemovedNames | Where-Object { $_ -and $_ -ne $ServerName } | Select-Object -Unique)
  if ($RemovedDuplicates.Count -gt 0) {
    Write-Host "Removed duplicate entries for this bundle: $($RemovedDuplicates -join ', ')"
  }
  Write-Host "$ConsumerName MCP config updated: $TargetPath"
  Write-Host "Verified MCP server name and bundle paths: $ServerName"
  Write-Host "Restart Codex CLI or reload MCP servers, then verify $ServerName with /mcp in a new task."
  } catch {
    $InstallError = $_
    $FailureReasonCode = [string]$script:DirectInstallFailureReason
    if ([string]::IsNullOrWhiteSpace($FailureReasonCode)) {
      $FailureReasonCode = "direct_install_failed"
    }
    $RollbackPerformed = $false
    $RollbackFailureMessage = ""
    try {
      if ($HadExistingConfig -and $BackupPath -and (Test-Path -LiteralPath $BackupPath)) {
        Restore-FileAtomically $BackupPath $TargetPath
        $RollbackPerformed = $true
      } elseif ((-not $HadExistingConfig) -and (Test-Path -LiteralPath $TargetPath)) {
        Remove-Item -LiteralPath $TargetPath -Force
        $RollbackPerformed = $true
      }
      if ($RollbackPerformed) {
        Write-Warning "$ConsumerName MCP config installation failed; the previous config state was restored."
      }
    } catch {
      $RollbackFailureMessage = $_.Exception.Message
      Write-Warning "$ConsumerName MCP config installation failed and automatic config rollback also failed: $RollbackFailureMessage"
    }
    $RollbackComplete = -not $RollbackFailureMessage
    try {
      Update-BundleStatus @{
        installation_attempt_id = $InstallationAttemptId
        installation_state = $(if ($RollbackComplete) { "failed_rolled_back" } else { "failed_rollback_incomplete" })
        connection_state = "failed"
        installation_failure_stage = "direct_registration_or_verification"
        installation_failure_reason = $FailureReasonCode
        direct_config_registered = $false
        direct_config_loader_verified = $false
        loader_verification_state = "failed"
        loader_verification_reason = $(if ($RollbackComplete) { "${FailureReasonCode}_prior_state_restored" } else { "rollback_incomplete" })
        installed_config_transport_verified = $false
        direct_stdio_verified = $false
        transport_end_to_end_verified = $false
        desktop_app_server_loader_verified = $false
        fresh_codex_app_server_inventory_verified = $false
        desktop_tool_scan_verified = $false
        conversation_attachment_verified = $false
        end_to_end_verified = $false
        direct_config_rollback_performed = $RollbackPerformed
        direct_config_path = $TargetPath
        installed_config_fingerprint = $null
      }
    } catch {
      $RollbackFailureMessage = if ($RollbackFailureMessage) { "$RollbackFailureMessage; status=$($_.Exception.Message)" } else { "status=$($_.Exception.Message)" }
      $RollbackComplete = $false
    }
    if (-not $RollbackComplete) {
      throw "Direct MCP installation failed and prior config state could not be restored completely. Config rollback error='$RollbackFailureMessage'. Original error: $($InstallError.Exception.Message)"
    }
    Fail-ClientConnectionAttempt "${FailureReasonCode}_prior_state_restored" -RolledBack
    throw $InstallError
  }
}

function Test-ClaudeDesktopConfig {
  $TargetPath = Get-ClaudeDesktopConfigPath
  if (-not (Test-Path -LiteralPath $TargetPath)) {
    Write-Host "Claude Desktop config does not exist yet: $TargetPath"
    Write-Host "Automatic install can create it."
    return $true
  }

  try {
    $Target = Get-Content -LiteralPath $TargetPath -Raw | ConvertFrom-Json
  } catch {
    Write-Warning "Claude Desktop config is not valid JSON: $TargetPath"
    Write-Warning "Do not paste the whole generated claude_desktop_config.json inside an existing JSON object. Merge only the mcpServers entry, or run this script with -InstallClaudeDesktop after fixing the file."
    Write-Warning "Original parser error: $($_.Exception.Message)"
    return $false
  }

  if (-not $Target.PSObject.Properties["mcpServers"]) {
    Write-Host "Claude Desktop config is valid JSON but has no mcpServers object yet."
  } else {
    $Names = @($Target.mcpServers.PSObject.Properties | ForEach-Object { $_.Name })
    if ($Names.Count -gt 0) {
      Write-Host "Claude Desktop config is valid JSON. Existing MCP servers: $($Names -join ', ')"
    } else {
      Write-Host "Claude Desktop config is valid JSON. mcpServers is present but empty."
    }
  }
  return $true
}

function Assert-ClaudeDesktopInstalledContract(
  [string]$TargetPath,
  [object]$GeneratedServer,
  [string]$ExpectedFingerprint = ""
) {
  if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
    throw "Claude Desktop config contract verification could not find the installed config: $TargetPath"
  }
  try {
    $InstalledConfig = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "Claude Desktop config contract verification could not parse the installed config: $TargetPath. $($_.Exception.Message)"
  }
  if (-not $InstalledConfig.PSObject.Properties["mcpServers"]) {
    throw "Claude Desktop config contract verification found no mcpServers object: $TargetPath"
  }
  $InstalledProperty = $InstalledConfig.mcpServers.PSObject.Properties[$ServerName]
  if (-not $InstalledProperty) {
    throw "Claude Desktop config contract verification found no server ${ServerName}: $TargetPath"
  }
  $InstalledServer = $InstalledProperty.Value
  $ExpectedType = [string]$GeneratedServer.type
  $InstalledType = [string]$InstalledServer.type
  if (-not [string]::Equals($InstalledType, $ExpectedType, [System.StringComparison]::Ordinal)) {
    throw "Claude Desktop config contract verification found a mismatched transport type for ${ServerName}."
  }
  if (-not [string]::Equals([string]$InstalledServer.command, [string]$GeneratedServer.command, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Claude Desktop config contract verification found a mismatched command for ${ServerName}."
  }
  if (-not (Test-SameMcpArguments @($InstalledServer.args) @($GeneratedServer.args))) {
    throw "Claude Desktop config contract verification found incomplete, reordered, or mismatched arguments for ${ServerName}."
  }
  $ExpectedEnvProperties = @($(if ($GeneratedServer.env) { $GeneratedServer.env.PSObject.Properties } else { @() }))
  $InstalledEnvProperties = @($(if ($InstalledServer.env) { $InstalledServer.env.PSObject.Properties } else { @() }))
  if ($ExpectedEnvProperties.Count -ne $InstalledEnvProperties.Count) {
    throw "Claude Desktop config contract verification found a mismatched environment for ${ServerName}."
  }
  foreach ($ExpectedEnvProperty in $ExpectedEnvProperties) {
    $InstalledEnvProperty = $InstalledServer.env.PSObject.Properties[$ExpectedEnvProperty.Name]
    if (-not $InstalledEnvProperty -or -not [string]::Equals([string]$InstalledEnvProperty.Value, [string]$ExpectedEnvProperty.Value, [System.StringComparison]::Ordinal)) {
      throw "Claude Desktop config contract verification found a mismatched environment value for ${ServerName}: $($ExpectedEnvProperty.Name)."
    }
  }
  $ActualFingerprint = "sha256:" + (Get-McpFileSha256 $TargetPath)
  if (-not [string]::IsNullOrWhiteSpace($ExpectedFingerprint) -and
      -not [string]::Equals($ActualFingerprint, $ExpectedFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) {
    [Console]::Out.WriteLine("Claude Desktop config changed after its installed launch contract was verified.")
    throw "Claude Desktop config changed after its installed launch contract was verified."
  }
  return $ActualFingerprint
}

function Install-ClaudeDesktopConfig {
  $Source = Read-ClaudeDesktopBundleServerConfig
  $Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")
  if (-not $Source.PSObject.Properties["mcpServers"]) {
    throw "claude_desktop_config.json does not contain mcpServers."
  }
  # Self-heal a damaged generated JSON file after the embedded UTF-8 fallback
  # succeeds, so later validation and reruns use a valid source file.
  Write-JsonUtf8NoBom (BundlePath "claude_desktop_config.json") $Source 50

  $TargetPath = Get-ClaudeDesktopConfigPath
  $TargetDir = Split-Path -Parent $TargetPath
  New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

  $HadExistingConfig = Test-Path -LiteralPath $TargetPath
  $BackupPath = $null
  $BackupConfigFingerprint = $null
  if ($HadExistingConfig) {
    $BackupPath = "$TargetPath.bak-$(Get-Date -Format yyyyMMddHHmmssfff)"
    $OriginalConfigFingerprint = Get-McpFileSha256 $TargetPath
    Copy-Item -LiteralPath $TargetPath -Destination $BackupPath
    $BackupConfigFingerprint = Get-McpFileSha256 $BackupPath
    if (-not [string]::Equals($OriginalConfigFingerprint, $BackupConfigFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw "Claude Desktop config backup hash mismatch; installation was not attempted."
    }
    try {
      $TargetConfig = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
      throw "Existing Claude Desktop config is not valid JSON: $TargetPath. Backup created at $BackupPath. Fix the JSON first, or move the invalid file aside and rerun this installer. Common cause: pasting the whole generated JSON as a second top-level object instead of merging mcpServers. Original parser error: $($_.Exception.Message)"
    }
    Write-Host "Backup created: $BackupPath"
  } else {
    $TargetConfig = [pscustomobject]@{}
  }

  try {
  if (-not $TargetConfig.PSObject.Properties["mcpServers"]) {
    Add-Member -InputObject $TargetConfig -MemberType NoteProperty -Name "mcpServers" -Value ([pscustomobject]@{})
  }

  $SourceServerProperty = $Source.mcpServers.PSObject.Properties[$ServerName]
  if (-not $SourceServerProperty) {
    throw "claude_desktop_config.json does not contain server $ServerName."
  }
  $GeneratedServer = $SourceServerProperty.Value
  $GeneratedProfileId = ""
  for ($Index = 0; $Index -lt ($GeneratedServer.args.Count - 1); $Index++) {
    if ($GeneratedServer.args[$Index] -eq "--profile-id") {
      $GeneratedProfileId = [string]$GeneratedServer.args[$Index + 1]
      break
    }
  }
  $LauncherPath = BundlePath "run_mcp_stdio_server.ps1"
  $BundleDataDir = Get-BundleDataDir
  $RemovedNames = [System.Collections.Generic.List[string]]::new()
  $ExistingNames = @(
    $TargetConfig.mcpServers.PSObject.Properties |
      ForEach-Object { $_.Name } |
      Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
  )
  foreach ($ExistingName in $ExistingNames) {
    $ExistingServer = $TargetConfig.mcpServers.PSObject.Properties[$ExistingName].Value
    $ExistingArgs = @($ExistingServer.args)
    $SameName = $ExistingName -eq $ServerName
    $SameBundle = $ExistingArgs -contains $LauncherPath -or $ExistingArgs -contains $BundleDataDir
    $LegacyDefaultForSameProfile = $ServerName -ne "govreg-local" -and
      $ExistingName -eq "govreg-local" -and
      $GeneratedProfileId -and
      $ExistingArgs -contains $GeneratedProfileId
    if ($SameName -or $SameBundle -or $LegacyDefaultForSameProfile) {
      $TargetConfig.mcpServers.PSObject.Properties.Remove($ExistingName)
      $RemovedNames.Add($ExistingName)
    }
  }

  foreach ($Server in $Source.mcpServers.PSObject.Properties) {
    Add-Member -InputObject $TargetConfig.mcpServers -MemberType NoteProperty -Name $Server.Name -Value $Server.Value
  }

  $TargetJson = ($TargetConfig | ConvertTo-Json -Depth 50) + [Environment]::NewLine
  Write-AtomicUtf8NoBom $TargetPath $TargetJson
  $InstalledConfigFingerprint = Assert-ClaudeDesktopInstalledContract $TargetPath $GeneratedServer
  $ClaudeRegistrationUpdatedAt = [DateTimeOffset]::UtcNow.ToString("o")
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    claude_desktop_config_registered = $true
    claude_desktop_config_path = $TargetPath
    claude_desktop_config_fingerprint = $InstalledConfigFingerprint
    claude_desktop_config_transport_verified = $false
    claude_desktop_config_transport_runtime_fingerprint = $null
    claude_desktop_registration_updated_at = $ClaudeRegistrationUpdatedAt
    claude_desktop_process_detected = $false
    claude_desktop_process_started_at = $null
    claude_desktop_restart_checked_at = $null
    claude_desktop_restart_required = $null
    claude_desktop_restart_status = "not_checked"
    claude_desktop_restarted_after_registration = $false
    claude_desktop_post_registration_log_session_observed = $false
    claude_desktop_server_name_observed = $false
    claude_desktop_loader_observed = $false
    claude_desktop_loader_verified = $false
    claude_desktop_conversation_verified = $false
  }
  if (-not (Run-InstalledClaudeDesktopConfigSmoke $TargetPath)) {
    throw "The installed Claude Desktop MCP config could not complete initialize, tools/list, and get_index_status."
  }
  $null = Assert-ClaudeDesktopInstalledContract $TargetPath $GeneratedServer $InstalledConfigFingerprint
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installation_state = "installed_pending_claude_desktop_verification"
    connection_state = "pending_claude_desktop_restart"
    claude_desktop_config_registered = $true
    claude_desktop_config_transport_verified = $true
    claude_desktop_loader_verified = $false
    claude_desktop_conversation_verified = $false
  }
  $ClaudeVerifiedStatus = Read-JsonFile "bundle_status.json"
  Complete-ClientConnectionAttempt @("registration", "transport") $InstalledConfigFingerprint ([string]$ClaudeVerifiedStatus.runtime_fingerprint)
  $RemovedDuplicates = @($RemovedNames | Where-Object { $_ -and $_ -ne $ServerName } | Select-Object -Unique)
  if ($RemovedDuplicates.Count -gt 0) {
    Write-Host "Removed duplicate Claude Desktop entries for this bundle: $($RemovedDuplicates -join ', ')"
  }
  Write-Host "Claude Desktop config updated: $TargetPath"
  Write-Host "Verified MCP server name and bundle paths: $ServerName"
  Write-Host "Installed-config stdio verification passed."
  Write-Host "[CONFIGURED - CLAUDE DESKTOP VERIFICATION REQUIRED] Restart Claude Desktop, open Settings > Developer > Local MCP servers, confirm the server is running, then invoke search and fetch in a new conversation."
  } catch {
    $InstallError = $_
    $RollbackComplete = $false
    try {
      if ($HadExistingConfig -and $BackupPath -and (Test-Path -LiteralPath $BackupPath)) {
        Restore-FileAtomically $BackupPath $TargetPath
        $RestoredConfigFingerprint = Get-McpFileSha256 $TargetPath
        if (-not [string]::Equals($RestoredConfigFingerprint, $BackupConfigFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) {
          throw "Restored Claude Desktop config hash does not match the pre-install backup."
        }
        $RollbackComplete = $true
        Write-Warning "Claude Desktop config installation failed; the previous config was restored."
      } elseif ((-not $HadExistingConfig) -and (Test-Path -LiteralPath $TargetPath)) {
        Remove-Item -LiteralPath $TargetPath -Force
        $RollbackComplete = $true
        Write-Warning "Claude Desktop config installation failed; the newly created config was removed."
      } elseif (-not $HadExistingConfig) {
        $RollbackComplete = $true
      }
    } catch {
      Write-Warning "Claude Desktop config installation failed and automatic rollback also failed: $($_.Exception.Message)"
    }
    if ($RollbackComplete) {
      $null = Fail-ClientConnectionAttempt "claude_desktop_install_failed_prior_state_restored" -RolledBack
    } else {
      $null = Fail-ClientConnectionAttempt "claude_desktop_install_failed_rollback_incomplete"
    }
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = $(if ($RollbackComplete) { "failed_rolled_back" } else { "failed_rollback_incomplete" })
      connection_state = "failed"
      claude_desktop_config_registered = $false
      claude_desktop_config_path = $null
      claude_desktop_config_fingerprint = $null
      claude_desktop_config_transport_verified = $false
      claude_desktop_config_transport_runtime_fingerprint = $null
      claude_desktop_registration_updated_at = $null
      claude_desktop_process_detected = $false
      claude_desktop_process_started_at = $null
      claude_desktop_restart_checked_at = $null
      claude_desktop_restart_required = $null
      claude_desktop_restart_status = "not_checked"
      claude_desktop_restarted_after_registration = $false
      claude_desktop_post_registration_log_session_observed = $false
      claude_desktop_server_name_observed = $false
      claude_desktop_loader_observed = $false
      claude_desktop_loader_verified = $false
      claude_desktop_conversation_verified = $false
      direct_stdio_verified = $false
      transport_end_to_end_verified = $false
      end_to_end_verified = $false
    }
    throw $InstallError
  }
}

function Show-ClaudeDesktop {
  Show-Header
  if ($ValidateClaudeDesktop) {
    if (-not (Test-ClaudeDesktopConfig)) {
      exit 1
    }
    return
  }
  if ($InstallClaudeDesktop) {
    Start-LocalInstallationAttempt "preflight_claude_desktop"
    try {
      if (-not (Test-ClaudeDesktopConfig)) {
        throw "Claude Desktop configuration validation failed; installation was not attempted."
      }
      if (-not (Run-LocalStdioDoctor)) {
        throw "Local MCP doctor failed; Claude Desktop installation was not attempted."
      }
      Install-ClaudeDesktopConfig
      return
    } catch {
      $ClaudeDesktopInstallError = $_
      Mark-CurrentAttemptFailedIfUnresolved "claude_desktop_preflight_or_install_failed"
      throw $ClaudeDesktopInstallError
    }
  }
  try {
    Write-Host "Manual path: $(Get-ClaudeDesktopConfigPath)"
  } catch {
    Write-Warning $_.Exception.Message
  }
  Write-Host "Generated JSON: $(BundlePath 'claude_desktop_config.json')"
  Write-Host "To validate the existing Claude Desktop config:"
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}" -Target claude-desktop -ValidateClaudeDesktop' -f $PSCommandPath)
  Write-Host "To merge automatically:"
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}" -InstallPackage -Target claude-desktop -InstallClaudeDesktop' -f $PSCommandPath)
}

function Register-ClaudeCode {
  Show-Header
  Start-LocalInstallationAttempt "preflight_claude_code"
  try {
    if (-not (Run-LocalStdioDoctor)) {
      throw "Local MCP doctor failed; Claude Code registration was not attempted."
    }
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
      Write-Warning "Claude Code CLI was not found on PATH."
      Write-Host "After installing Claude Code, run:"
      Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f (BundlePath 'claude_code_add_stdio.ps1'))
      throw "Claude Code CLI is required to register and verify this MCP server."
    }
    Run-Script "claude_code_add_stdio.ps1"
    $ClaudeEvidencePath = BundlePath "claude_code_registration_evidence.json"
    if (-not (Test-Path -LiteralPath $ClaudeEvidencePath -PathType Leaf)) {
      throw "Claude Code registration did not produce current verification evidence."
    }
    try { $ClaudeEvidence = Get-Content -LiteralPath $ClaudeEvidencePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "Claude Code registration evidence is invalid." }
    if ([string]$ClaudeEvidence.schema_version -ne "claude-code-registration-evidence-v1" -or
        [string]$ClaudeEvidence.server_name -ne $ServerName -or
        [string]$ClaudeEvidence.scope -ne "user" -or
        $ClaudeEvidence.status_connected -ne $true -or
        $ClaudeEvidence.registration_verified -ne $true -or
        $ClaudeEvidence.transport_verified -ne $true -or
        [string]$ClaudeEvidence.config_entry_fingerprint -notmatch "^sha256:[0-9a-f]{64}$") {
      throw "Claude Code registration evidence did not verify the connected user-scoped launch contract."
    }
    $ClaudeCodeStatus = Read-JsonFile "bundle_status.json"
    if ([string]$ClaudeCodeStatus.installation_attempt_id -ne $InstallationAttemptId) {
      throw "Claude Code verification does not belong to the current installation attempt."
    }
    $ClaudeCodeRuntimeFingerprint = [string]$ClaudeCodeStatus.runtime_fingerprint
    $ClaudeCodeConfigFingerprint = [string]$ClaudeEvidence.config_entry_fingerprint
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = "installed_claude_code_configured"
      connection_state = "configured_pending_claude_code_conversation"
      claude_code_registered = $true
      claude_code_config_fingerprint = $ClaudeCodeConfigFingerprint
      claude_code_loader_verified = $true
      claude_code_transport_verified = $true
      claude_code_transport_runtime_fingerprint = $(if (-not [string]::IsNullOrWhiteSpace($ClaudeCodeRuntimeFingerprint)) { $ClaudeCodeRuntimeFingerprint } else { $null })
      claude_code_registration_updated_at = [DateTimeOffset]::UtcNow.ToString("o")
      claude_code_conversation_verified = $false
      conversation_attachment_verified = $false
      end_to_end_verified = $false
    }
    Complete-ClientConnectionAttempt @("registration", "loader", "transport") $ClaudeCodeConfigFingerprint $ClaudeCodeRuntimeFingerprint
    Write-Host "Claude Code registered user-scoped stdio MCP server."
    Write-Host "Runtime initialize/tools/get_index_status smoke passed."
    Write-Host "Open a fresh Claude Code task and invoke get_index_status before reporting conversation connection."
  } catch {
    $ClaudeCodeError = $_
    Mark-CurrentAttemptFailedIfUnresolved "claude_code_preflight_or_registration_failed"
    throw $ClaudeCodeError
  }
}

function Show-Codex {
  Show-Header
  $InstallDirect = $InstallCodex
  $ConsumerName = "Codex CLI"
  if ($InstallDirect) {
    $DirectConfigMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-LocalMcpInstallation")
    $DirectConfigLockAcquired = $false
    try {
      try { $DirectConfigLockAcquired = $DirectConfigMutex.WaitOne([TimeSpan]::FromSeconds(30)) }
      catch [System.Threading.AbandonedMutexException] { $DirectConfigLockAcquired = $true }
      if (-not $DirectConfigLockAcquired) {
        throw "Another MCP connection process is updating the local MCP config. Wait for it to finish, then retry."
      }
      Start-LocalInstallationAttempt "preflight_direct"
      if (-not (Run-LocalStdioDoctor)) {
        throw "Local MCP doctor failed; $ConsumerName configuration was not changed."
      }
      Install-CodexConfig $ConsumerName
      $DirectSmokeStatus = Read-JsonFile "bundle_status.json"
      if ([string]$DirectSmokeStatus.installation_attempt_id -ne $InstallationAttemptId) {
        throw "bundle_status.json does not belong to the current installation attempt."
      }
      if ($DirectSmokeStatus.direct_stdio_verified -ne $true) {
        throw "Direct MCP protocol smoke did not verify initialize, tools/list, search, and fetch."
      }
      Write-Host "Direct MCP protocol initialize/tools smoke passed."
      if ($script:CodexLoaderVerified) {
        Run-CodexAppServerMcpCheck
        $PostProbeStatus = Read-JsonFile "bundle_status.json"
        $CurrentConfigFingerprint = if (Test-Path -LiteralPath (Get-CodexConfigPath)) {
          "sha256:" + (Get-McpFileSha256 (Get-CodexConfigPath))
        } else {
          $null
        }
        if ([string]$PostProbeStatus.installation_attempt_id -ne $InstallationAttemptId -or
            $PostProbeStatus.direct_config_registered -ne $true -or
            $PostProbeStatus.fresh_codex_app_server_inventory_verified -ne $true -or
            [string]::IsNullOrWhiteSpace($CurrentConfigFingerprint) -or
            [string]$PostProbeStatus.installed_config_fingerprint -ne $CurrentConfigFingerprint) {
          throw "$ConsumerName MCP config changed during or immediately after fresh loader verification; revalidation is required."
        }
        Complete-ClientConnectionAttempt @("registration", "loader", "transport", "fresh_app_server") $CurrentConfigFingerprint ([string]$PostProbeStatus.runtime_fingerprint)
      } else {
        Write-Warning "[LOADER VERIFICATION PENDING] The config and direct transport passed, but a fresh Codex CLI loader inventory was not available."
        $PendingDirectStatus = Read-JsonFile "bundle_status.json"
        Complete-ClientConnectionAttempt @("registration", "transport") ([string]$PendingDirectStatus.installed_config_fingerprint) ([string]$PendingDirectStatus.runtime_fingerprint)
      }
      return
    } catch {
      $DirectShowError = $_
      Mark-CurrentAttemptFailedIfUnresolved "direct_preflight_or_install_failed"
      throw $DirectShowError
    } finally {
      if ($DirectConfigLockAcquired) { $DirectConfigMutex.ReleaseMutex() }
      $DirectConfigMutex.Dispose()
    }
  }
  try {
    Write-Host "$ConsumerName shared MCP config path: $(Get-CodexConfigPath)"
  } catch {
    Write-Warning $_.Exception.Message
  }
  Write-Host "Generated snippet: $(BundlePath 'codex_config_snippet.toml')"
  Write-Host "To install/update automatically:"
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}" -InstallPackage -Target codex -InstallCodex' -f $PSCommandPath)
}

function Show-ChatGptHttps {
  Show-Header
  Warn-IfCoreCommandsMissing | Out-Null
  $Connector = Read-JsonFile "chatgpt_connector.json"
  if (-not $Connector.connector_url) {
    throw "No ChatGPT web HTTPS MCP URL is ready. Regenerate with --public-url https://your-host.example/mcp."
  }
  Write-Host "ChatGPT web remote MCP URL:"
  Write-Host "  $($Connector.connector_url)"
  if (Get-Command Set-Clipboard -ErrorAction SilentlyContinue) {
    $Connector.connector_url | Set-Clipboard
    Write-Host "The connector URL was copied to the clipboard."
  }
  Write-Host ""
  Write-Host "ChatGPT does not directly connect to a local MCP server."
  Write-Host "Use ChatGPT web. Confirm that your plan, workspace role, and administrator settings allow Developer mode."
  Write-Host "Pro supports read/fetch MCP connections in Developer mode; full MCP is for Business, Enterprise, and Edu."
  Write-Host "Open Settings > Apps > Advanced settings, enable Developer mode, then create an app with this HTTPS /mcp URL."
  Write-Host "Choose supported authentication, scan tools, and create the app."
  Write-Host "Official requirements: https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta"
  Write-Host "Private/on-premises/developer-machine server: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
  Write-Host "Validate the deployed endpoint with:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$((BundlePath 'validate_chatgpt_remote_mcp.ps1'))`""
  Write-Host "Open a new ChatGPT web chat, select the created app, and confirm $ServerName."
  Write-Host "Verification: call search, then call fetch with an id returned by search."
}

function Show-UnsupportedChatGptLocal {
  Show-Header
  Write-Error "ChatGPT local STDIO is unsupported. ChatGPT does not directly connect to a local MCP server."
  Write-Host "Use -Target chatgpt-remote with a reachable HTTPS /mcp endpoint in ChatGPT web."
  Write-Host "Official requirements: https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta"
  Write-Host "For a private, on-premises, or developer-machine server, use OpenAI Secure MCP Tunnel:"
  Write-Host "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
  throw "Unsupported target: ChatGPT local STDIO"
}

function Show-ClaudeHttps {
  Show-Header
  Warn-IfCoreCommandsMissing | Out-Null
  $Remote = Read-JsonFile "claude_https_mcp.json"
  Write-Host "Claude Streamable HTTP URL:"
  if ($Remote.connector_url) {
    Write-Host "  $($Remote.connector_url)"
    if (Get-Command Set-Clipboard -ErrorAction SilentlyContinue) {
      $Remote.connector_url | Set-Clipboard
      Write-Host "The URL was copied to the clipboard."
    }
  } else {
    throw "No Claude HTTPS MCP URL is ready. Regenerate the bundle with --public-url https://your-host.example/mcp."
  }
  Write-Host "Register only this URL and approved authentication under Customize > Connectors."
  Write-Host "For Claude Code, run claude_code_add_http.ps1, then verify it with claude mcp get."
  Write-Host "Enable the connector from + > Connectors and verify search followed by fetch."
}

function Show-Menu {
  Show-Header
  Write-Host "Choose a target:"
  Write-Host "  0. Install/check local package commands"
  Write-Host "  1. Claude Code local stdio"
  Write-Host "  2. Codex CLI local stdio"
  Write-Host "  3. Claude Desktop local stdio"
  Write-Host "  4. ChatGPT web remote HTTPS MCP"
  Write-Host "  5. Claude Vercel HTTPS MCP"
  Write-Host "  6. Doctor/readiness check"
  $Choice = Read-Host "Target"
  switch ($Choice) {
    "0" { Invoke-WithLocalConnectionFlow { Install-LocalPackage } }
    "1" { Invoke-WithLocalConnectionFlow { Register-ClaudeCode } }
    "2" { Invoke-WithLocalConnectionFlow { Show-Codex } }
    "3" { Invoke-WithLocalConnectionFlow { Show-ClaudeDesktop } }
    "4" { Show-ChatGptHttps }
    "5" { Show-ClaudeHttps }
    "6" { Run-Doctor }
    default { throw "Unknown choice: $Choice" }
  }
}

function Install-PackageIfRequested {
  if ($InstallPackage) {
    $PreferredDoctorScript = if ($PreferredProjectRoot) { Join-Path $PreferredProjectRoot $McpCommandScripts["reg-rag-mcp-doctor"] } else { "" }
    $GeneratedRuntimeReady = $PreferredPython -and
      $PreferredDoctorScript -and
      (Test-Path -LiteralPath $PreferredPython -PathType Leaf) -and
      (Test-Path -LiteralPath $PreferredDoctorScript -PathType Leaf)
    if ($GeneratedRuntimeReady) {
      Write-Host "Generated project runtime is already available; package installation is not required for this connection run."
    } else {
      Install-LocalPackage
    }
  }
}

function Invoke-SelectedTarget {
  switch ($Target) {
    "menu" { Show-Menu }
    "install" { Install-LocalPackage }
    "claude-desktop" { Show-ClaudeDesktop }
    "claude-code" { Register-ClaudeCode }
    "codex" { Show-Codex }
    "chatgpt-desktop-direct" { Show-UnsupportedChatGptLocal }
    "chatgpt-desktop-local" { Show-UnsupportedChatGptLocal }
    "chatgpt-remote" { Show-ChatGptHttps }
    "chatgpt-desktop" { Show-UnsupportedChatGptLocal }
    "chatgpt-https" { Show-ChatGptHttps }
    "claude-remote" { Show-ClaudeHttps }
    "claude-api" { Show-ClaudeHttps }
    "doctor" { Run-Doctor }
  }
}

$LocalConnectionTargets = @("install", "claude-desktop", "claude-code", "codex")
if ($LocalConnectionTargets -contains $Target) {
  # Keep installation, runtime marker creation, registration, and transport
  # verification in one serialized flow.  Releasing after pip alone allows a
  # second bundle to replace the same-version wheel before the first bundle
  # verifies its config.
  Invoke-WithLocalConnectionFlow {
    Install-PackageIfRequested
    Invoke-SelectedTarget
  }
} else {
  # Remote commands can be long-lived. Serialize an explicitly
  # requested package install, then release the local mutation lock before
  # displaying remote setup guidance.
  if ($InstallPackage) {
    Invoke-WithLocalConnectionFlow { Install-PackageIfRequested }
  }
  Invoke-SelectedTarget
}
'''
    return (
        script.replace("__SERVER_NAME__", server_name)
        .replace("__EMBEDDED_CLAUDE_DESKTOP_CONFIG_BASE64__", embedded_config_base64)
        .replace("__EMBEDDED_CODEX_CONFIG_BASE64__", embedded_config_base64)
        .replace(
            "__FILE_SHA256_FUNCTION__",
            "\n".join(_powershell_file_sha256_function_lines()),
        )
        .replace(
            "__LOCAL_STDIO_DOCTOR_ARGS__",
            _powershell_array_literal(local_stdio_doctor_args or []),
        )
        .replace(
            "__RUNTIME_IDENTITY_VALIDATOR__",
            "\n".join(_powershell_runtime_identity_validator_lines()),
        )
    )


def _with_product_embedded_mcp_configs(
    script: str,
    *,
    claude_desktop_config: dict[str, Any],
    codex_config: dict[str, Any],
) -> str:
    """Bind each generated installer's fallback to its own client config."""

    rendered = script
    for variable_name, config in (
        ("EmbeddedClaudeDesktopConfigBase64", claude_desktop_config),
        ("EmbeddedCodexConfigBase64", codex_config),
    ):
        if not isinstance(config, dict) or not isinstance(config.get("mcpServers"), dict):
            raise ValueError(f"Cannot embed missing product MCP config: {variable_name}")
        encoded = base64.b64encode(
            json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        pattern = rf'(?m)^\${re.escape(variable_name)} = "[A-Za-z0-9+/=]+"$'
        rendered, replacement_count = re.subn(
            pattern,
            f'${variable_name} = "{encoded}"',
            rendered,
            count=1,
        )
        if replacement_count != 1:
            raise ValueError(f"Generated connection wizard is missing {variable_name}.")
    return rendered


def _stdio_server_config(
    *,
    data_dir: str,
    tenant_id: str,
    profile_id: str | None,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    tenant_storage_isolation: bool,
    include_type: bool,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "command": "reg-rag-mcp-server",
        "args": _server_args(
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            transport="stdio",
            actor=actor,
            role=role,
            department_ids=department_ids,
            tenant_storage_isolation=tenant_storage_isolation,
        ),
    }
    if include_type:
        config = {"type": "stdio", **config}
    return config


def _http_server_config(
    *,
    host: str,
    port: int,
    public_url: str | None,
    include_transport_alias: bool,
) -> dict[str, Any]:
    url = _connector_url(host=host, port=port, public_url=public_url)
    config: dict[str, Any] = {"type": "http", "url": url}
    if include_transport_alias:
        config["transport"] = "streamable-http"
    return config


def _bundle_codex_quickstart(*, server_name: str) -> dict[str, Any]:
    return {
        "config_file_candidates": [
            "%USERPROFILE%\\.codex\\config.toml",
            "~/.codex/config.toml",
        ],
        "config_snippet_file": SETUP_BUNDLE_FILES["codex_config"],
        "paste_toml_section": f"mcp_servers.{server_name}",
        "transport": "stdio",
        "verification_commands": [
            {"command": "codex", "args": ["mcp", "list"]},
            {"command": "codex", "args": ["mcp", "get", server_name]},
        ],
        "verification_tools": ["search", "fetch"],
    }


def _bundle_codex_claude_team_quickstart(*, server_name: str) -> dict[str, Any]:
    return {
        "team_id": "codex_claude_local_mcp_review_loop",
        "shared_server_name": server_name,
        "shared_transport": "stdio",
        "shared_runtime_source": "approved_bundle_data",
        "independence_rule": "claude_reviews_codex_changes_before_release",
        "claude_project_agents": [
            "regulation-security-auditor",
            "regulation-release-reviewer",
        ],
        "shared_prerequisites": [
            "validate_synthetic_chain",
            "audit_index_visibility",
            "validate_runtime_transport",
        ],
        "members": {
            "codex": {
                "quickstart_key": "codex",
                "responsibility": "implementation_and_runtime_fix",
                "verify_with": ["search", "fetch"],
            },
            "claude_code": {
                "quickstart_key": "claude_code",
                "responsibility": "independent_audit_and_regression_review",
                "verify_with": ["search", "fetch"],
            },
        },
        "handoff_sequence": [
            "codex_register_stdio",
            "claude_register_stdio",
            "codex_implement_and_run_focused_tests",
            "claude_security_audit",
            "codex_remediate_confirmed_findings",
            "claude_release_regression_review",
            "validate_client_config_smoke",
            "run_search_and_fetch_in_both_clients",
        ],
        "shared_artifacts": [
            SETUP_BUNDLE_FILES["codex_config"],
            SETUP_BUNDLE_FILES["claude_code_stdio"],
            "validate_client_config_smoke.ps1",
            "doctor_mcp_connection.ps1",
        ],
    }


def _bundle_quickstart(
    *,
    server_name: str,
    data_dir: str,
    tenant_id: str,
    profile_id: str | None,
    tenant_storage_isolation: bool,
    host: str,
    port: int,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    claude_code: dict[str, Any],
    chatgpt_desktop_local: dict[str, Any],
    chatgpt_remote: dict[str, Any],
    claude_remote: dict[str, Any],
    remote_auth_token_env: str | None,
    min_visible_records: int,
) -> dict[str, Any]:
    script_data_dir = BUNDLE_DATA_DIR_ARG
    stdio_args = _server_args(
        data_dir=script_data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        transport="stdio",
        actor=actor,
        role=role,
        department_ids=department_ids,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    stdio_args = _with_no_warm_cache(stdio_args)
    claude_code_command = str(claude_code.get("command") or "powershell.exe")
    claude_code_command_args = [
        str(value) for value in claude_code.get("args", [])
    ] if isinstance(claude_code.get("args"), list) else []
    claude_code_cli_args = [
        "mcp",
        "add",
        "--transport",
        "stdio",
        "--scope",
        "user",
        server_name,
        "--",
        claude_code_command,
        *claude_code_command_args,
    ]
    index_visibility_args = [
        "--data-dir",
        script_data_dir,
        "--tenant-id",
        tenant_id,
        "--min-visible-records",
        str(int(min_visible_records)),
        "--forbid-smoke-docs",
        "--require-indexed",
        "--fail-on-issue",
    ]
    if tenant_storage_isolation:
        index_visibility_args.append("--tenant-storage-isolation")
    validate_runtime_transport_ps = _powershell_bundle_runtime_transport_smoke_script(
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    validate_client_config_smoke_ps = _powershell_bundle_client_config_smoke_script(server_name=server_name)
    stdio_doctor_args = [
        "--client-profile",
        "bundle",
        "--transport",
        "stdio",
        "--data-dir",
        script_data_dir,
        "--allow-local-only-bundle",
        "--fail-on-warning",
    ]
    stdio_doctor_args.extend(_doctor_index_visibility_args(tenant_id, tenant_storage_isolation, min_visible_records))
    run_local_stdio_server_ps = _powershell_stdio_guarded_command(
        "reg-rag-mcp-server",
        stdio_args,
        doctor_args=stdio_doctor_args,
    )
    doctor_args = [
        "--client-profile",
        "bundle",
        "--transport",
        "streamable-http",
        "--host",
        host,
        "--data-dir",
        script_data_dir,
    ]
    if remote_auth_token_env:
        doctor_args.extend(["--token-env", remote_auth_token_env])
    if chatgpt_remote["connector_url"]:
        doctor_args.extend(["--public-url", chatgpt_remote["connector_url"]])
    else:
        doctor_args.append("--allow-local-only-bundle")
    doctor_args.extend(_doctor_index_visibility_args(tenant_id, tenant_storage_isolation, min_visible_records))
    claude_code_stdio_ps = _powershell_claude_code_stdio_bundle_script(
        server_name=server_name,
        server_args=stdio_args,
        doctor_args=stdio_doctor_args,
    )
    claude_code_http_ps = None
    if chatgpt_remote["connector_url"]:
        claude_code_http_args = [
            "mcp",
            "add",
            "--transport",
            "http",
            "--scope",
            "user",
        ]
        if remote_auth_token_env:
            claude_code_http_args.extend(
                ["--header", "Authorization: Bearer ${" + remote_auth_token_env + "}"]
            )
        claude_code_http_args.extend(
            [server_name, chatgpt_remote["connector_url"]]
        )
        claude_code_http_ps = _powershell_command("claude", claude_code_http_args)
    return {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "validate_synthetic_chain": {
            "command": "reg-rag-mcp-smoke",
            "args": ["--fail-on-issue"],
        },
        "validate_runtime_transport": {
            "command": "reg-rag-mcp-transport-smoke",
            "note": (
                "Runs against the bundled runtime data when data/mcp_runtime_manifest.json is present. "
                "The smoke query is read from recommended_smoke_query."
            ),
        },
        "check_existing_index": {
            "tool": "get_index_status",
            "note": (
                "Run against the actual local/full-profile server after starting it; synthetic smoke does not validate "
                "the real tenant DB. External ChatGPT connectors use chatgpt-data and should validate "
                "the catalog, hierarchy, exact-article, reference, reference-cycle, search, and fetch tools."
            ),
        },
        "audit_index_visibility": {
            "command": "reg-rag-mcp-index-visibility",
            "args": index_visibility_args,
            "note": "Run before client connection to verify the selected runtime exposes approved records and no smoke-test documents.",
        },
        "run_local_stdio_server": {
            "command": "reg-rag-mcp-server",
            "args": stdio_args,
        },
        # Compatibility key retained for older readers.  Its payload contains
        # no command, args, cwd, env, ui_fields, or mcpServers entry.
        "chatgpt_desktop_local": dict(chatgpt_desktop_local),
        "claude_desktop": {
            "paste_json_section": "claude_desktop.mcpServers",
            "config_file_candidates": [
                "%APPDATA%\\Claude\\claude_desktop_config.json",
                "~/Library/Application Support/Claude/claude_desktop_config.json",
            ],
        },
        "codex": _bundle_codex_quickstart(server_name=server_name),
        "claude_code": {
            "command": "claude",
            "args": claude_code_cli_args,
        },
        "chatgpt_remote": {
            "profile": "chatgpt-remote",
            "surface": "chatgpt_web",
            "web_only": True,
            "setup": chatgpt_remote["chatgpt_setup"]["location"],
            "connector_url": chatgpt_remote["connector_url"],
            "requires_reachable_https": chatgpt_remote["chatgpt_setup"]["requires_reachable_https"],
            "https_endpoint_ready": chatgpt_remote["chatgpt_setup"]["https_endpoint_ready"],
            "oauth_ready": chatgpt_remote["chatgpt_setup"]["oauth_ready"],
            "configuration_ready": chatgpt_remote["configuration_ready"],
            "verification_tools": list(CHATGPT_DATA_TOOL_NAMES),
            "tool_profile": "chatgpt-data",
            "authentication_modes": [
                "bearer_token_env_var",
                "oauth",
                "approved_public_unauthenticated",
            ],
            "bearer_token_env_var": remote_auth_token_env,
            "connection_options": ["vercel_https_endpoint"],
            "official_help_url": CHATGPT_MCP_HELP_URL,
            "secure_mcp_tunnel_url": CHATGPT_SECURE_MCP_TUNNEL_URL,
            "plan_requirements": chatgpt_remote["chatgpt_setup"]["plan_requirements"],
        },
        "vercel_https": {
            "stage_command": "reg-rag-mcp-vercel-stage",
            "connector_url": chatgpt_remote["connector_url"],
            "mcp_path": "/mcp",
            "shared_by_clients": [
                "ChatGPT web",
                "Codex CLI",
                "Codex IDE",
                "Claude",
            ],
            "bearer_token_env_var": remote_auth_token_env,
        },
        "claude_remote": {
            "profile": "claude-remote",
            "transport": "streamable-http",
            "connector_url": claude_remote.get("connector_url"),
            "ready": bool(claude_remote.get("ready")),
            "authentication_modes": [
                "bearer_header_environment_reference",
                "oauth",
                "approved_public_unauthenticated",
            ],
            "authorization_token_env": remote_auth_token_env,
        },
        "codex_claude_team": _bundle_codex_claude_team_quickstart(
            server_name=server_name
        ),
        "warnings": [],
        "copy_paste": {
            "validate_synthetic_chain_ps": _powershell_command("reg-rag-mcp-smoke", ["--fail-on-issue"]),
            "validate_runtime_transport_ps": validate_runtime_transport_ps,
            "validate_client_config_smoke_ps": validate_client_config_smoke_ps,
            "audit_index_visibility_ps": _powershell_command("reg-rag-mcp-index-visibility", index_visibility_args),
            "run_local_stdio_server_ps": run_local_stdio_server_ps,
            "claude_code_stdio_ps": claude_code_stdio_ps,
            "claude_code_http_ps": claude_code_http_ps,
            "doctor_ps": _powershell_doctor_bundle_script(doctor_args),
            "connect_wizard_ps": _connect_wizard_script(
                server_name=server_name,
                local_stdio_server_args=stdio_args,
                local_stdio_doctor_args=stdio_doctor_args,
            ),
            "chatgpt_connector_url": chatgpt_remote["connector_url"],
            "claude_connector_url": claude_remote.get("connector_url"),
        },
    }


def _chatgpt_connector_config(
    *,
    server_name: str,
    data_dir: str,
    tenant_id: str,
    profile_id: str | None,
    host: str,
    port: int,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    tenant_storage_isolation: bool,
    public_url: str | None,
    remote_auth_token_env: str | None,
    chatgpt_oauth_ready: bool,
    min_visible_records: int = 1,
) -> dict[str, Any]:
    connector_url = _remote_connector_url(public_url=public_url)
    https_endpoint_ready = bool(connector_url and connector_url.startswith("https://"))
    missing = []
    if not connector_url:
        missing.append("public_url_https_mcp_endpoint")
    elif not https_endpoint_ready:
        missing.append("public_url_must_use_https")
    oauth_ready = bool(chatgpt_oauth_ready)
    config_toml = {"url": connector_url}
    if remote_auth_token_env:
        config_toml["bearer_token_env_var"] = remote_auth_token_env
    return {
        "profile": "chatgpt-remote",
        "surface": "chatgpt_web",
        "web_only": True,
        "transport": "streamable-http",
        "connector_name": server_name,
        "connector_url": connector_url,
        "ready": https_endpoint_ready,
        "configuration_ready": https_endpoint_ready,
        "remote_endpoint_verified": False,
        "tool_scan_unverified": True,
        "conversation_attachment_unverified": True,
        "end_to_end_verified": False,
        "missing": missing,
        "chatgpt_setup": {
            "location": (
                "ChatGPT web > Settings > Apps > Advanced settings > Developer mode; "
                "then Apps > Create"
            ),
            "surface": "chatgpt_web",
            "web_only": True,
            "connector_url": connector_url,
            "transport": "streamable-http",
            "requires_reachable_https": True,
            "https_endpoint_ready": https_endpoint_ready,
            "authentication_modes": [
                "bearer_token_env_var",
                "oauth",
                "approved_public_unauthenticated",
            ],
            "bearer_token_env_var": remote_auth_token_env,
            "oauth_ready": oauth_ready,
            "recommended_description": (
                "Search and fetch approved local regulation evidence from the institution's MCP server."
            ),
            "official_help_url": CHATGPT_MCP_HELP_URL,
            "secure_mcp_tunnel_url": CHATGPT_SECURE_MCP_TUNNEL_URL,
            "direct_local_supported": False,
            "plan_requirements": {
                "pro": "Read/fetch MCP connections require Developer mode.",
                "business_enterprise_edu": (
                    "Full MCP is available on ChatGPT web; administrator, owner, publishing, "
                    "and RBAC requirements may apply."
                ),
                "mobile": "Custom MCP apps are not available on mobile.",
            },
            "authentication_note": (
                "Choose the supported authentication mechanism while creating the ChatGPT web app. "
                "For OAuth, make sure the provider issues refresh tokens. Keep secrets out of generated files."
            ),
        },
        "config_toml": config_toml,
        "deployment": {
            "platform": "vercel",
            "entrypoint": "api/index.py",
            "path": "/mcp",
        },
        "server_auth": {
            "mode": "bearer-or-oauth-or-approved-public",
            "oauth_ready": oauth_ready,
            "bearer_token_env_var": remote_auth_token_env,
            "bearer_supported_by_chatgpt_desktop_and_codex": False,
            "bearer_token_env_scope": "generated_remote_smoke_and_codex_clients_only",
            "note": (
                "For ChatGPT web, select a supported authentication mechanism when creating the app. "
                "Use OAuth for private remote endpoints when appropriate. Unauthenticated mode must "
                "be an explicit approved public read-only deployment."
            ),
        },
        "compatible_tools": list(CHATGPT_DATA_TOOL_NAMES),
        "connection_steps": [
            "Stage the approved runtime with reg-rag-mcp-vercel-stage and deploy it to Vercel.",
            "In ChatGPT web, confirm that your plan, workspace role, and administrator settings allow Developer mode.",
            "Open Settings > Apps > Advanced settings, enable Developer mode, and create a new app.",
            "Enter connector_url as the reachable HTTPS MCP endpoint and choose supported authentication.",
            "For a private local or on-premises server, use OpenAI Secure MCP Tunnel instead of a direct local connection.",
            "Scan tools and create the app.",
            "Verify the discovered tool list includes "
            f"{', '.join(CHATGPT_DATA_TOOL_NAMES)} before using the app.",
            "In a new chat, list the catalog, inspect one regulation TOC, article, and reference graph, "
            "review any reported reference cycles, then search and fetch evidence.",
        ],
        "notes": [
            "ChatGPT custom MCP apps use ChatGPT web and a reachable remote endpoint; ChatGPT does not directly connect to a local MCP server.",
            "Register only the deployed HTTPS /mcp URL in ChatGPT; do not enter a local command, arguments, folder, or environment.",
            "The chatgpt-data profile keeps the exact search(query) and fetch(id) input signatures required for "
            "data-source compatibility and adds read-only catalog, TOC, exact-article, reference, and "
            "reference-cycle tools.",
            "Citation URLs are absolute user-openable HTTP(S) source URLs or empty when no such source exists.",
            "Do not expose streamable-http or SSE MCP without authentication or approved network controls.",
            "Use only public or separately approved data when routing MCP responses to an external cloud AI.",
            f"Official ChatGPT MCP requirements: {CHATGPT_MCP_HELP_URL}",
            f"Private-server alternative: {CHATGPT_SECURE_MCP_TUNNEL_URL}",
        ],
    }


def _claude_remote_connector_config(
    *,
    server_name: str,
    data_dir: str,
    tenant_id: str,
    profile_id: str | None,
    host: str,
    port: int,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    tenant_storage_isolation: bool,
    public_url: str | None,
    remote_auth_token_env: str | None,
) -> dict[str, Any]:
    connector_url = _remote_connector_url(public_url=public_url)
    https_endpoint_ready = bool(
        connector_url and urlsplit(connector_url).scheme.lower() == "https"
    )
    missing = []
    if not connector_url:
        missing.append("public_url_https_mcp_endpoint")
    elif not https_endpoint_ready:
        missing.append("public_url_must_use_https")
    return {
        "profile": "claude-remote",
        "transport": "streamable-http",
        "connector_name": server_name,
        "connector_url": connector_url,
        "ready": https_endpoint_ready,
        "missing": missing,
        "registration": {
            "claude": "Customize > Connectors > Add custom connector",
            "claude_code_script": SETUP_BUNDLE_FILES["claude_code_http"],
        },
        "connection_steps": [
            "Stage the approved runtime with reg-rag-mcp-vercel-stage and deploy it to Vercel.",
            "Use only the final deployed HTTPS /mcp URL.",
            "Register that URL in Claude Customize > Connectors, or run claude_code_add_http.ps1 for Claude Code.",
            "Keep bearer credentials in an environment variable, or complete the approved OAuth flow.",
            "Verify search and fetch from a new conversation.",
        ],
        "server_auth": _remote_auth_summary(remote_auth_token_env),
        "notes": [
            "Claude remote custom connectors require an internet-reachable HTTPS MCP URL.",
            "Do not expose streamable-http or SSE MCP without authentication or approved network controls.",
            "Register only the final URL and approved authentication; do not enter a local directory.",
        ],
    }


def _connector_url(*, host: str, port: int, public_url: str | None) -> str:
    if public_url:
        normalized = _remote_connector_url(public_url=public_url)
        if normalized is None:
            raise ValueError(
                "public_url must be a valid HTTP(S) URL with a hostname and no query or fragment."
            )
        return normalized
    client_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{client_host}:{int(port)}/mcp"


def _remote_connector_url(*, public_url: str | None) -> str | None:
    if not public_url:
        return None
    cleaned = public_url.strip()
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
        # Accessing ``port`` validates malformed/non-numeric ports.
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        return None
    path = parsed.path.rstrip("/")
    if not path:
        path = "/mcp"
    elif not path.endswith("/mcp"):
        path = f"{path}/mcp"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _auth_issuer_args(public_url: str | None) -> list[str]:
    issuer_url = _auth_issuer_url(public_url)
    if not issuer_url:
        return []
    parsed = urlparse(issuer_url)
    args = ["--auth-issuer-url", issuer_url]
    if parsed.hostname:
        args.extend(["--allowed-http-host", parsed.netloc])
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        args.extend(["--allowed-http-origin", f"{parsed.scheme}://{parsed.netloc}"])
    return args


def _auth_issuer_url(public_url: str | None) -> str | None:
    connector_url = _remote_connector_url(public_url=public_url)
    if not connector_url:
        return None
    if connector_url.endswith("/mcp"):
        return connector_url[: -len("/mcp")]
    return connector_url


def _http_auth_args(remote_auth_token_env: str | None) -> list[str]:
    if not remote_auth_token_env:
        return []
    return ["--http-bearer-token-env", remote_auth_token_env]


def _remote_auth_summary(remote_auth_token_env: str | None) -> dict[str, Any]:
    if not remote_auth_token_env:
        return {
            "required": False,
            "mode": "approved_public_unauthenticated",
            "token_env": None,
            "note": (
                "Use only for an explicitly approved public read-only deployment; "
                "private endpoints require bearer authentication or OAuth."
            ),
        }
    return {
        "required": True,
        "mode": "bearer",
        "token_env": remote_auth_token_env,
        "note": "Use bearer token auth or an approved authenticated reverse proxy before exposing HTTP/SSE MCP.",
    }


def _canonical_readme_index_visibility_command(config: dict[str, Any]) -> str:
    quickstart = config.get("quickstart") if isinstance(config.get("quickstart"), dict) else {}
    audit = (
        quickstart.get("audit_index_visibility")
        if isinstance(quickstart.get("audit_index_visibility"), dict)
        else {}
    )
    command = str(audit.get("command") or "reg-rag-mcp-index-visibility")
    args = [str(value) for value in audit.get("args") or []]
    if "--data-dir" in args:
        data_index = args.index("--data-dir")
        if data_index + 1 < len(args):
            args[data_index + 1] = r".\data"
        else:
            args.append(r".\data")
    else:
        args[0:0] = ["--data-dir", r".\data"]
    for required_flag in ("--forbid-smoke-docs", "--require-indexed"):
        if required_flag not in args:
            args.append(required_flag)
    return _powershell_command(command, args)


def _setup_bundle_readme(*, config: dict[str, Any], files: dict[str, str], server_name: str) -> str:
    """Return the canonical direct-connection guide shipped in new bundles."""
    connections = _setup_bundle_connections(config)
    local_connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | `{item['primary_file']}` | {item['operator_action']} |"
        for item in connections
        if item["mode"] == "local_stdio"
    )
    remote_connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | `{item['primary_file']}` | {item['operator_action']} |"
        for item in connections
        if item["mode"] == "streamable_http"
    )
    index_visibility_command = _canonical_readme_index_visibility_command(config)
    return f"""# {server_name} MCP bundle

This bundle separates supported local stdio clients from remote HTTPS clients. It does not contain BAT launchers or agent prompts.

## Supported local stdio: Codex and Claude

Register the generated `command`, `args`, and `cwd` with the client, plus `env` only when the server requires it. A directory name alone is not an MCP stdio configuration:

| Client | Transport | Direct artifact | Action |
| --- | --- | --- | --- |
{local_connection_rows}

Core local files:

- Codex CLI: `{files.get('codex_config', SETUP_BUNDLE_FILES['codex_config'])}`
- Claude Code: `{files.get('claude_code_stdio', SETUP_BUNDLE_FILES['claude_code_stdio'])}`
- Claude Desktop: `{files.get('claude_desktop', SETUP_BUNDLE_FILES['claude_desktop'])}`
- stdio server: `{files.get('stdio_launcher', SETUP_BUNDLE_FILES['stdio_launcher'])}`
- diagnostics: `{files.get('doctor', SETUP_BUNDLE_FILES['doctor'])}`

Restart the client after registration. Verify server discovery, then call `search` and `fetch`.

## Local runtime on a fresh target PC

- An installed Windows app may use its installed executable on the same PC where this bundle was generated.
- A handoff ZIP does not include that installed executable. An absolute source-PC executable path is not target-PC runtime evidence.
- On a fresh target PC, supported local stdio requires either a separately supplied compatible executable beside the extracted bundle or Python 3.11+.
- Without that executable, install Python 3.11+ and run `install_local_package.ps1` first. Include an approved wheel in the ZIP or provide an approved wheel/source package separately. A wheel does not include Python itself.
- Remote HTTPS clients do not require Python on the client PC after the remote MCP endpoint has been deployed and approved.

## ChatGPT web: remote HTTPS MCP only

ChatGPT does not directly connect to a local MCP server. Do not enter a local command, arguments, working directory, or environment in ChatGPT. The retained `{files.get('chatgpt_desktop_local', SETUP_BUNDLE_FILES['chatgpt_desktop_local'])}` file is a warning-only compatibility artifact with `support_status=unsupported` and `direct_local_supported=false`; it is not a runnable config.

Current OpenAI requirements:

- Use ChatGPT **web**; custom MCP apps are not available on mobile.
- Enable Developer mode before creating the app. Plan, workspace role, administrator approval, publishing, and RBAC requirements may apply.
- Pro can connect read/fetch MCPs in Developer mode. Full MCP is available to Business, Enterprise, and Edu.
- Official requirements: {CHATGPT_MCP_HELP_URL}
- For a private, on-premises, or developer-machine server, use OpenAI Secure MCP Tunnel: {CHATGPT_SECURE_MCP_TUNNEL_URL}

Remote connection artifacts:

| Client | Transport | Direct artifact | Action |
| --- | --- | --- | --- |
{remote_connection_rows}

## Prepare a reachable Vercel HTTPS endpoint

Prepare a clean deployment directory from the repository root:

```powershell
reg-rag-mcp-vercel-stage --runtime-data-dir .\\data --out-dir .\\vercel-mcp-stage
```

Deploy that staging directory to Vercel. The public Streamable HTTP endpoint is:

```text
https://<deployment>/mcp
```

The same deployment and `/mcp` endpoint can serve ChatGPT web, Codex, and Claude; do not deploy separate client-specific MCP servers. For an approved public read-only endpoint, set `MCP_ALLOW_UNAUTHENTICATED_HTTP=true` and leave `MCP_AUTH_TOKEN` empty. For private remote data, use an approved authentication design such as OAuth; for a server that must remain private on-premises or on a developer machine, use Secure MCP Tunnel. Configure matching `MCP_TENANT_ID`, `MCP_PROFILE_ID`, and any custom-domain `MCP_ALLOWED_HTTP_HOSTS` in Vercel. In ChatGPT web, open Settings > Apps > Advanced settings, enable Developer mode, then use Apps > Create with the final HTTPS `/mcp` URL, choose supported authentication, scan tools, and create the app.

## Security

- Only approved runtime records belong in `data/`.
- Do not deploy raw uploads, traces, exports, secrets, or the full local data directory.
- Keep tokens in environment variables or a secret manager.
- Verify indexed visibility from the bundle root before deployment:

```powershell
{index_visibility_command}
```

- Run `{files.get('validate', SETUP_BUNDLE_FILES['validate'])}` and an actual `search`/`fetch` call before declaring the connection ready.
"""


def _setup_bundle_readme_ko(*, config: dict[str, Any], files: dict[str, str], server_name: str) -> str:
    """Return the Korean canonical direct-connection guide shipped in new bundles."""
    connections = _setup_bundle_connections(config)
    local_connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | `{item['primary_file']}` | {item['operator_action']} |"
        for item in connections
        if item["mode"] == "local_stdio"
    )
    remote_connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | `{item['primary_file']}` | {item['operator_action']} |"
        for item in connections
        if item["mode"] == "streamable_http"
    )
    index_visibility_command = _canonical_readme_index_visibility_command(config)
    return f"""# {server_name} MCP 번들

이 번들은 지원되는 로컬 stdio 클라이언트와 원격 HTTPS 클라이언트를 분리합니다. BAT 실행 파일과 에이전트 연결 프롬프트는 포함하지 않습니다.

## 지원되는 로컬 stdio: Codex와 Claude

생성된 `command`, `args`, `cwd`를 클라이언트에 등록하고 서버가 요구할 때만 `env`를 추가합니다. 디렉터리명만 지정해서는 stdio MCP가 인식되지 않습니다.

| 클라이언트 | 전송 | 직접 적용 파일 | 작업 |
| --- | --- | --- | --- |
{local_connection_rows}

핵심 파일:

- Codex CLI: `{files.get('codex_config', SETUP_BUNDLE_FILES['codex_config'])}`
- Claude Code: `{files.get('claude_code_stdio', SETUP_BUNDLE_FILES['claude_code_stdio'])}`
- Claude Desktop: `{files.get('claude_desktop', SETUP_BUNDLE_FILES['claude_desktop'])}`
- stdio 서버: `{files.get('stdio_launcher', SETUP_BUNDLE_FILES['stdio_launcher'])}`
- 연결 진단: `{files.get('doctor', SETUP_BUNDLE_FILES['doctor'])}`

등록 후 클라이언트를 완전히 재시작하고 서버가 보이는지 확인한 뒤 `search`와 `fetch`를 실제 호출합니다.

## 새 대상 PC의 로컬 실행 조건

- 이 번들을 만든 PC에서는 설치된 Windows 앱의 EXE를 사용할 수 있습니다.
- 전달 ZIP에는 원 PC에 설치된 EXE가 포함되지 않으며, 원 PC EXE의 절대 경로는 대상 PC 실행 근거가 아닙니다.
- 새 대상 PC에서 지원되는 로컬 stdio를 쓰려면 압축을 푼 번들 옆에 별도로 제공된 호환 EXE가 있거나 Python 3.11+가 설치되어 있어야 합니다.
- 호환 EXE가 없으면 Python 3.11+를 설치한 뒤 `install_local_package.ps1`을 먼저 실행하세요. ZIP에 승인된 wheel을 포함하거나 승인된 wheel/소스 패키지를 별도로 제공해야 하며, wheel 자체에는 Python이 포함되지 않습니다.
- 원격 MCP endpoint를 배포·승인한 뒤 사용하는 HTTPS 클라이언트에는 클라이언트 PC의 Python이 필요하지 않습니다.

## ChatGPT 웹: 원격 HTTPS MCP만 지원

ChatGPT는 로컬 MCP 서버에 직접 연결하지 않습니다. ChatGPT에 로컬 Command, Arguments, 작업 폴더 또는 환경변수를 입력하지 마세요. 남아 있는 `{files.get('chatgpt_desktop_local', SETUP_BUNDLE_FILES['chatgpt_desktop_local'])}`은 이전 판독기 호환용 경고 파일이며 `support_status=unsupported`, `direct_local_supported=false`인 실행 불가 파일입니다.

현재 OpenAI 공식 조건:

- ChatGPT **웹**을 사용합니다. 사용자 지정 MCP 앱은 모바일에서 사용할 수 없습니다.
- 앱을 만들기 전에 Developer mode를 켭니다. 플랜, 워크스페이스 역할, 관리자 승인, 게시와 RBAC 조건이 적용될 수 있습니다.
- Pro는 Developer mode에서 read/fetch MCP를 연결할 수 있습니다. full MCP는 Business·Enterprise·Edu 대상입니다.
- 공식 조건: {CHATGPT_MCP_HELP_URL}
- 사설망·온프레미스·개발 PC 서버는 OpenAI Secure MCP Tunnel을 사용합니다: {CHATGPT_SECURE_MCP_TUNNEL_URL}

원격 연결 파일:

| 클라이언트 | 전송 | 직접 적용 파일 | 작업 |
| --- | --- | --- | --- |
{remote_connection_rows}

## 외부에서 접속 가능한 Vercel HTTPS endpoint 준비

저장소 루트에서 승인 runtime만 포함하는 배포 디렉터리를 준비합니다.

```powershell
reg-rag-mcp-vercel-stage --runtime-data-dir .\\data --out-dir .\\vercel-mcp-stage
```

해당 디렉터리를 Vercel에 배포하면 Streamable HTTP endpoint는 다음 형식입니다.

```text
https://<deployment>/mcp
```

같은 Vercel 배포와 `/mcp` endpoint를 ChatGPT 웹·Codex·Claude가 공통으로 사용할 수 있으므로 클라이언트별 서버를 따로 배포하지 않습니다. 승인된 공개 read-only endpoint는 `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`를 명시하고 `MCP_AUTH_TOKEN`을 비웁니다. 비공개 원격 데이터에는 OAuth 같은 승인된 인증 방식을 사용하고, 서버를 사설망·온프레미스·개발 PC에 비공개로 유지해야 하면 Secure MCP Tunnel을 사용합니다. Vercel에는 manifest와 일치하는 `MCP_TENANT_ID`, `MCP_PROFILE_ID`, 사용자 도메인의 `MCP_ALLOWED_HTTP_HOSTS`를 설정합니다. ChatGPT 웹에서는 Settings > Apps > Advanced settings에서 Developer mode를 켜고, Apps > Create에서 최종 HTTPS `/mcp` URL과 지원되는 인증을 선택한 뒤 도구 스캔을 완료합니다.

## 보안

- `data/`에는 승인된 runtime record만 포함합니다.
- 원본 업로드, trace, export, 비밀값, 로컬 전체 data 디렉터리를 배포하지 않습니다.
- 토큰은 환경변수 또는 secret manager에만 둡니다.
- 배포 전에 번들 루트에서 색인 가시성을 검증합니다.

```powershell
{index_visibility_command}
```

- `{files.get('validate', SETUP_BUNDLE_FILES['validate'])}`와 실제 `search`·`fetch` 호출을 통과해야 연결 완료로 판단합니다.
"""


def _powershell_stdio_guarded_command(
    command: str,
    args: list[object],
    *,
    doctor_args: list[object],
    prequoted_indexes: set[int] | None = None,
) -> str:
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        '$DoctorPython = Resolve-BundleModulePython "scripts.check_mcp_connection_readiness"',
        '$DoctorArgs = ' + _powershell_array_literal(doctor_args),
        '$DoctorExitCode = Invoke-BundlePythonModule $DoctorPython "scripts.check_mcp_connection_readiness" $DoctorArgs',
        "if ($DoctorExitCode -ne 0) { exit $DoctorExitCode }",
        '$ServerPython = Resolve-BundleModulePython "scripts.run_regulation_mcp"',
        '$ServerArgs = ' + _powershell_array_literal(args),
        '$ServerExitCode = Invoke-BundlePythonModule $ServerPython "scripts.run_regulation_mcp" $ServerArgs',
        'if ($ServerExitCode -ne 0) { exit $ServerExitCode }',
    ]
    return "\n".join(lines)


def _doctor_index_visibility_args(
    tenant_id: str,
    tenant_storage_isolation: bool,
    min_visible_records: int,
) -> list[object]:
    args: list[object] = [
        "--audit-index-visibility",
        "--tenant-id",
        tenant_id,
        "--min-visible-records",
        str(int(min_visible_records)),
        "--forbid-smoke-docs",
        "--require-indexed",
    ]
    if tenant_storage_isolation:
        args.append("--tenant-storage-isolation")
    return args


def _powershell_doctor_bundle_script(args: list[object]) -> str:
    doctor_args = list(args) + ["--bundle-dir", "$BundleDir", "--json", "--out-json", "$DoctorReport"]
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        '$DoctorReport = Join-Path $BundleDir "mcp_connection_readiness.json"',
        'if (Test-Path -LiteralPath $DoctorReport) { Remove-Item -LiteralPath $DoctorReport -Force }',
        '$McpPython = Resolve-BundleModulePython "scripts.check_mcp_connection_readiness"',
        '$DoctorArgs = ' + _powershell_array_literal(doctor_args),
        '$DoctorArgs[' + str(len(args) + 1) + '] = $BundleDir',
        '$DoctorArgs[' + str(len(args) + 4) + '] = $DoctorReport',
        '$DoctorExitCode = Invoke-BundlePythonModule $McpPython "scripts.check_mcp_connection_readiness" $DoctorArgs',
        '$DoctorResult = $null',
        'if (Test-Path -LiteralPath $DoctorReport) { try { $DoctorResult = Get-Content -LiteralPath $DoctorReport -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $DoctorResult = $null } }',
        '$DoctorVerified = $DoctorExitCode -eq 0 -and $DoctorResult -and [string]$DoctorResult.report_type -eq "mcp_connection_readiness" -and $DoctorResult.passed -eq $true -and @($DoctorResult.findings).Count -eq 0',
        'Write-Host "Doctor report: $DoctorReport"',
        'if (-not $DoctorVerified) { throw "MCP doctor did not produce a fresh passing readiness report." }',
    ]
    return "\n".join(lines)


def _powershell_bundle_runtime_transport_smoke_script(
    *,
    tenant_id: str,
    tenant_storage_isolation: bool,
) -> str:
    storage_flag = "--tenant-storage-isolation" if tenant_storage_isolation else "--flat-storage"
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        '$ManifestPath = Join-Path $BundleDataDir "mcp_runtime_manifest.json"',
        '$SmokeReport = Join-Path $BundleDir "mcp_transport_smoke.json"',
        '$Query = "규정"',
        'if (Test-Path -LiteralPath $ManifestPath) {',
        '  try {',
        '    $RuntimeManifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json',
        '    if ($RuntimeManifest.recommended_smoke_query) { $Query = [string]$RuntimeManifest.recommended_smoke_query }',
        '  } catch {',
        '    Write-Warning "Could not read recommended_smoke_query from $ManifestPath. Falling back to a generic query."',
        '  }',
        '}',
        '$SmokeArgs = @("--data-dir", $BundleDataDir, "--tenant-id", "__TENANT_ID__", "--skip-preparation", "--query", $Query, "--out-json", $SmokeReport, "--fail-on-issue", "__STORAGE_FLAG__")',
        '$SmokeArgs += "--no-warm-cache"',
        '$McpPython = Resolve-BundleModulePython "scripts.run_mcp_transport_smoke"',
        'Write-Host "Runtime smoke query: $Query"',
        'if (Test-Path -LiteralPath $SmokeReport) { Remove-Item -LiteralPath $SmokeReport -Force }',
        '$SmokeExitCode = Invoke-BundlePythonModule $McpPython "scripts.run_mcp_transport_smoke" $SmokeArgs',
        '$SmokeResult = $null',
        'if (Test-Path -LiteralPath $SmokeReport) { try { $SmokeResult = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $SmokeResult = $null } }',
        '$SmokeVerified = $SmokeExitCode -eq 0 -and $SmokeResult -and [string]$SmokeResult.report_type -eq "mcp_transport_smoke" -and $SmokeResult.passed -eq $true -and $SmokeResult.process_started -eq $true -and $SmokeResult.mcp_initialized -eq $true -and $SmokeResult.tools_discovered -eq $true -and $SmokeResult.end_to_end_verified -eq $true -and $SmokeResult.full_profile.passed -eq $true -and [int]$SmokeResult.full_profile.search_result_count -gt 0 -and $SmokeResult.full_profile.fetch_has_text -eq $true -and $SmokeResult.chatgpt_data_profile.passed -eq $true -and [int]$SmokeResult.chatgpt_data_profile.search_result_count -gt 0 -and $SmokeResult.chatgpt_data_profile.fetch_has_text -eq $true',
        'Write-Host "Transport smoke report: $SmokeReport"',
        'if (-not $SmokeVerified) { Write-Output "Runtime MCP smoke did not produce a fresh passing search/fetch report."; throw "Runtime MCP smoke did not produce a fresh passing search/fetch report." }',
    ]
    return "\n".join(lines).replace("__TENANT_ID__", tenant_id).replace("__STORAGE_FLAG__", storage_flag)


def _powershell_bundle_client_config_smoke_script(*, server_name: str) -> str:
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        '$ServerName = "__SERVER_NAME__"',
        '$SmokeReport = Join-Path $BundleDir "mcp_client_config_smoke.json"',
        '$CodexConfig = Join-Path $BundleDir "codex_config_snippet.toml"',
        '$ClaudeDesktopConfig = Join-Path $BundleDir "claude_desktop_config.json"',
        '$BundleStatus = Join-Path $BundleDir "bundle_status.json"',
        '$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"',
        'if (Test-Path -LiteralPath $SmokeReport) { Remove-Item -LiteralPath $SmokeReport -Force }',
        'function Write-Utf8NoBom([string]$LiteralPath, [string]$Value) {',
        '  $Parent = Split-Path -Parent $LiteralPath',
        '  $TemporaryPath = Join-Path $Parent (".{0}.{1}.{2}.tmp" -f ([System.IO.Path]::GetFileName($LiteralPath)), $PID, [Guid]::NewGuid().ToString("N"))',
        '  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)',
        '  try { [System.IO.File]::WriteAllText($TemporaryPath, $Value, $Utf8NoBom); Move-Item -LiteralPath $TemporaryPath -Destination $LiteralPath -Force }',
        '  finally { if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -LiteralPath $TemporaryPath -Force } }',
        '}',
        'function Write-JsonUtf8NoBom([string]$LiteralPath, [object]$Value, [int]$Depth = 50) { Write-Utf8NoBom $LiteralPath (($Value | ConvertTo-Json -Depth $Depth) + [Environment]::NewLine) }',
        'function ConvertTo-TomlString([string]$Value) { return ($Value | ConvertTo-Json -Compress) }',
        'function ConvertTo-TomlKey([string]$Value) { if ($Value -match "^[A-Za-z0-9_-]+$") { return $Value }; return (ConvertTo-TomlString $Value) }',
        'function Set-McpBundlePaths([string[]]$ArgsToPatch) {',
        '  $Updated = @($ArgsToPatch)',
        '  for ($Index = 0; $Index -lt $Updated.Count - 1; $Index++) {',
        '    if ($Updated[$Index] -eq "--data-dir") { $Updated[$Index + 1] = $BundleDataDir }',
        '    if ($Updated[$Index] -eq "-File") { $Updated[$Index + 1] = $StdioLauncher }',
        '  }',
        '  if ($Updated -notcontains "--no-warm-cache") { $Updated += "--no-warm-cache" }',
        '  return $Updated',
        '}',
        'function Write-CodexBundleConfig([string[]]$ArgsToWrite) {',
        '  $Lines = @(',
        '    "# Paste or replace this server block in `$HOME\\.codex\\config.toml.",',
        '    "# Generated/validated for this extracted bundle directory.",',
        '    "[mcp_servers.$(ConvertTo-TomlKey $ServerName)]",',
        '    "command = `"powershell.exe`"",',
        '    "args = ["',
        '  )',
        '  foreach ($Arg in $ArgsToWrite) { $Lines += "  $(ConvertTo-TomlString $Arg)," }',
        '  $Lines += "]"',
        '  Write-Utf8NoBom $CodexConfig (($Lines -join [Environment]::NewLine) + [Environment]::NewLine)',
        '}',
        'function Update-ClaudeDesktopBundleConfig {',
        '  $Claude = Get-Content -LiteralPath $ClaudeDesktopConfig -Raw -Encoding UTF8 | ConvertFrom-Json',
        '  if (-not $Claude.mcpServers) { throw "Generated Claude Desktop config is missing mcpServers." }',
        '  $ServerProperty = $Claude.mcpServers.PSObject.Properties[$ServerName]',
        '  if (-not $ServerProperty) { throw "Generated Claude Desktop config is missing MCP server $ServerName." }',
        '  $Server = $ServerProperty.Value',
        '  $Server.command = "powershell.exe"',
        '  $Server.args = @(Set-McpBundlePaths @($Server.args))',
        '  Write-JsonUtf8NoBom $ClaudeDesktopConfig $Claude 40',
        '  return @($Server.args)',
        '}',
        'if (-not (Test-Path -LiteralPath $CodexConfig)) { throw "Missing generated Codex config snippet: $CodexConfig" }',
        'if (-not (Test-Path -LiteralPath $ClaudeDesktopConfig)) { throw "Missing generated Claude Desktop config: $ClaudeDesktopConfig" }',
        'if (-not (Test-Path -LiteralPath $StdioLauncher)) { throw "Missing generated stdio launcher: $StdioLauncher" }',
        '$ClaudeDesktopArgs = Update-ClaudeDesktopBundleConfig',
        'Write-CodexBundleConfig $ClaudeDesktopArgs',
        '$SmokeArgs = @("--server-name", $ServerName, "--codex-config", $CodexConfig, "--claude-desktop-config", $ClaudeDesktopConfig, "--out-json", $SmokeReport, "--fail-on-issue")',
        '$McpPython = Resolve-BundleModulePython "scripts.run_mcp_client_config_smoke"',
        '$SmokeExitCode = Invoke-BundlePythonModule $McpPython "scripts.run_mcp_client_config_smoke" $SmokeArgs',
        '$SmokeResult = $null',
        'if (Test-Path -LiteralPath $SmokeReport) { try { $SmokeResult = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $SmokeResult = $null } }',
        '$SmokeVerified = $SmokeExitCode -eq 0 -and $SmokeResult -and [string]$SmokeResult.report_type -eq "mcp_client_config_smoke" -and $SmokeResult.passed -eq $true -and $SmokeResult.launcher_ready -eq $true -and $SmokeResult.process_started -eq $true -and $SmokeResult.mcp_initialized -eq $true -and $SmokeResult.tools_discovered -eq $true -and $SmokeResult.end_to_end_verified -eq $true -and @($SmokeResult.results).Count -eq 2',
        'Write-Host "Client config smoke report: $SmokeReport"',
        'if (-not $SmokeVerified) { Write-Output "Client config smoke did not produce a fresh passing Codex and Claude Desktop report."; throw "Client config smoke did not produce a fresh passing Codex and Claude Desktop report." }',
    ]
    return "\n".join(lines).replace("__SERVER_NAME__", server_name)


def _powershell_chatgpt_remote_validation_script(
    *,
    server_name: str,
    connector_url: str | None,
    token_env: str | None,
) -> str:
    url = str(connector_url or "")
    token_name = str(token_env or "")
    lines = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        f'$ServerName = {_powershell_single_quoted_json(server_name)}',
        f'$RemoteUrl = {_powershell_single_quoted_json(url)}',
        f'$TokenEnv = {_powershell_single_quoted_json(token_name)}',
        '$SmokeReport = Join-Path $BundleDir "mcp_chatgpt_remote_smoke.json"',
        '$BundleStatus = Join-Path $BundleDir "bundle_status.json"',
        'function Write-Utf8NoBom([string]$LiteralPath, [string]$Value) { $Utf8NoBom = New-Object System.Text.UTF8Encoding($false); [System.IO.File]::WriteAllText($LiteralPath, $Value, $Utf8NoBom) }',
        'function Write-JsonUtf8NoBom([string]$LiteralPath, [object]$Value, [int]$Depth = 50) { Write-Utf8NoBom $LiteralPath (($Value | ConvertTo-Json -Depth $Depth) + [Environment]::NewLine) }',
        'if ([string]::IsNullOrWhiteSpace($RemoteUrl)) { throw "No ChatGPT web HTTPS MCP endpoint is configured. Regenerate with --public-url https://your-host.example/mcp." }',
        'if (-not $RemoteUrl.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) { throw "ChatGPT remote MCP requires an https:// endpoint." }',
        '$SmokeArgs = @("--server-name", $ServerName, "--remote-url", $RemoteUrl, "--out-json", $SmokeReport, "--fail-on-issue")',
        'if ($TokenEnv) { $SmokeArgs += @("--remote-token-env", $TokenEnv) }',
        '$McpPython = Resolve-BundleModulePython "scripts.run_mcp_client_config_smoke"',
        'if (Test-Path -LiteralPath $SmokeReport) { Remove-Item -LiteralPath $SmokeReport -Force }',
        '$SmokeExitCode = Invoke-BundlePythonModule $McpPython "scripts.run_mcp_client_config_smoke" $SmokeArgs',
        '$SmokeResult = $null',
        'if (Test-Path -LiteralPath $SmokeReport) { try { $SmokeResult = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $SmokeResult = $null } }',
        '$RemoteResults = if ($SmokeResult -and $SmokeResult.results) { @($SmokeResult.results | Where-Object { [string]$_.label -eq "chatgpt_remote" }) } else { @() }',
        '$SmokeVerified = $SmokeExitCode -eq 0 -and $SmokeResult -and [string]$SmokeResult.report_type -eq "mcp_client_config_smoke" -and $SmokeResult.passed -eq $true -and $SmokeResult.process_started -eq $true -and $SmokeResult.mcp_initialized -eq $true -and $SmokeResult.tools_discovered -eq $true -and $SmokeResult.end_to_end_verified -eq $true -and $RemoteResults.Count -eq 1 -and $RemoteResults[0].auth_wire_verified -eq $true -and $RemoteResults[0].contract_verified -eq $true',
        'Write-Host "Remote MCP validation report: $SmokeReport"',
        'Write-Host "Protocol validation does not replace ChatGPT web Developer mode permission, Apps > Create tool scan, app selection in a new chat, or an actual tool call."',
        'if (-not $SmokeVerified) { throw "Remote MCP validation did not produce a fresh passing authenticated protocol report." }',
    ]
    return "\n".join(lines)


def _powershell_claude_code_stdio_bundle_script(
    *,
    server_name: str,
    server_args: list[object],
    doctor_args: list[object],
) -> str:
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_file_sha256_function_lines(),
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        '$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"',
        '$ClaudeEvidencePath = Join-Path $BundleDir "claude_code_registration_evidence.json"',
        '$ClaudeSmokeReport = Join-Path $BundleDir "mcp_claude_code_registration_smoke.json"',
        'if (Test-Path -LiteralPath $ClaudeEvidencePath) { Remove-Item -LiteralPath $ClaudeEvidencePath -Force }',
        'if (Test-Path -LiteralPath $ClaudeSmokeReport) { Remove-Item -LiteralPath $ClaudeSmokeReport -Force }',
        'if (-not (Test-Path -LiteralPath $StdioLauncher)) { throw "Missing generated stdio launcher: $StdioLauncher" }',
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'function Invoke-ClaudeMcpCli([string[]]$Arguments) {',
        '  $PreviousErrorActionPreference = $ErrorActionPreference',
        '  $PreviousConsoleOutputEncoding = [Console]::OutputEncoding',
        '  $PreviousPowerShellOutputEncoding = $OutputEncoding',
        '  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)',
        '  try {',
        '    $ErrorActionPreference = "Continue"',
        '    [Console]::OutputEncoding = $Utf8NoBom',
        '    $OutputEncoding = $Utf8NoBom',
        '    $global:LASTEXITCODE = 1',
        '    $CommandOutput = @(& claude @Arguments 2>&1)',
        '    $CommandExitCode = [int]$global:LASTEXITCODE',
        '  } finally {',
        '    $OutputEncoding = $PreviousPowerShellOutputEncoding',
        '    [Console]::OutputEncoding = $PreviousConsoleOutputEncoding',
        '    $ErrorActionPreference = $PreviousErrorActionPreference',
        '  }',
        '  return [pscustomobject]@{ ExitCode = $CommandExitCode; Output = $CommandOutput }',
        '}',
        'function Get-ClaudeUserConfigPath {',
        '  if ($env:USERPROFILE) { return Join-Path $env:USERPROFILE ".claude.json" }',
        '  throw "Cannot determine the Claude Code user config path."',
        '}',
        'function Test-ExactClaudeMcpArguments([object[]]$Actual, [object[]]$Expected) {',
        '  $ActualValues = @($Actual | ForEach-Object { [string]$_ })',
        '  $ExpectedValues = @($Expected | ForEach-Object { [string]$_ })',
        '  if ($ActualValues.Count -ne $ExpectedValues.Count) { return $false }',
        '  for ($Index = 0; $Index -lt $ExpectedValues.Count; $Index++) {',
        '    if (-not [string]::Equals($ActualValues[$Index], $ExpectedValues[$Index], [System.StringComparison]::Ordinal)) { return $false }',
        '  }',
        '  return $true',
        '}',
        'function Assert-ClaudeUserConfigContract([string]$ConfigPath, [object[]]$ExpectedArgs) {',
        '  if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Claude Code user-scope config was not written." }',
        '  try { $InstalledConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }',
        '  catch { throw "Claude Code user-scope config is not valid JSON." }',
        '  if (-not $InstalledConfig.PSObject.Properties["mcpServers"]) { throw "Claude Code user-scope config has no mcpServers object." }',
        '  $InstalledProperty = $InstalledConfig.mcpServers.PSObject.Properties["' + server_name + '"]',
        '  if (-not $InstalledProperty) { throw "Claude Code user-scope config has no exact server entry for ' + server_name + '." }',
        '  $InstalledServer = $InstalledProperty.Value',
        '  if (-not [string]::Equals([string]$InstalledServer.type, "stdio", [System.StringComparison]::Ordinal)) { throw "Claude Code user-scope entry has the wrong transport type." }',
        '  if (-not [string]::Equals([string]$InstalledServer.command, "powershell.exe", [System.StringComparison]::OrdinalIgnoreCase)) { throw "Claude Code user-scope entry has the wrong command." }',
        '  if (-not (Test-ExactClaudeMcpArguments @($InstalledServer.args) $ExpectedArgs)) { throw "Claude Code user-scope entry has incomplete, duplicated, reordered, or mismatched arguments." }',
        '  return "sha256:" + (Get-McpFileSha256 $ConfigPath)',
        '}',
        'function Restore-ClaudeConfigAtomically([string]$BackupPath, [string]$TargetPath) {',
        '  $Parent = Split-Path -Parent $TargetPath',
        '  $TemporaryPath = Join-Path $Parent (".claude.{0}.{1}.restore-tmp" -f $PID, [Guid]::NewGuid().ToString("N"))',
        '  $ReplaceBackupPath = Join-Path $Parent (".claude.{0}.{1}.restore-bak" -f $PID, [Guid]::NewGuid().ToString("N"))',
        '  try {',
        '    Copy-Item -LiteralPath $BackupPath -Destination $TemporaryPath -Force',
        '    if (Test-Path -LiteralPath $TargetPath) { [System.IO.File]::Replace($TemporaryPath, $TargetPath, $ReplaceBackupPath, $true) }',
        '    else { Move-Item -LiteralPath $TemporaryPath -Destination $TargetPath }',
        '    if ((Get-McpFileSha256 $BackupPath) -ne (Get-McpFileSha256 $TargetPath)) { throw "Claude Code config rollback hash mismatch." }',
        '  } finally {',
        '    if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -LiteralPath $TemporaryPath -Force }',
        '    if (Test-Path -LiteralPath $ReplaceBackupPath) { Remove-Item -LiteralPath $ReplaceBackupPath -Force }',
        '  }',
        '}',
        'Assert-Command "reg-rag-mcp-doctor"',
        'Assert-Command "claude"',
        _powershell_command("reg-rag-mcp-doctor", doctor_args),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        "$ClaudeCodeArgs = " + _powershell_array_literal(server_args),
        '$LauncherArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $StdioLauncher) + $ClaudeCodeArgs',
        '$ClaudeUserConfig = Get-ClaudeUserConfigPath',
        '$ClaudeConfigExisted = $false',
        '$ClaudeConfigBackup = $null',
        '$ClaudeConfigBackupFingerprint = $null',
        '$ClaudeConfigMutex = New-Object System.Threading.Mutex($false, "Local\\PRMCPBuilder-ClaudeCodeConfig")',
        '$ClaudeConfigLockAcquired = $false',
        '$ClaudeMutationStarted = $false',
        'try {',
        '  try { $ClaudeConfigLockAcquired = $ClaudeConfigMutex.WaitOne([TimeSpan]::FromSeconds(30)) } catch [System.Threading.AbandonedMutexException] { $ClaudeConfigLockAcquired = $true }',
        '  if (-not $ClaudeConfigLockAcquired) { throw "Another Claude Code MCP registration is running. Wait for it to finish, then retry." }',
        '  $ClaudeConfigExisted = Test-Path -LiteralPath $ClaudeUserConfig -PathType Leaf',
        '  if ($ClaudeConfigExisted) { $ClaudeConfigBackup = Join-Path (Split-Path -Parent $ClaudeUserConfig) (".claude.{0}.{1}.transaction-bak" -f $PID, [Guid]::NewGuid().ToString("N")) }',
        '  if ($ClaudeConfigExisted) {',
        '    $ClaudeOriginalConfigFingerprint = Get-McpFileSha256 $ClaudeUserConfig',
        '    Copy-Item -LiteralPath $ClaudeUserConfig -Destination $ClaudeConfigBackup -Force',
        '    $ClaudeConfigBackupFingerprint = Get-McpFileSha256 $ClaudeConfigBackup',
        '    if (-not [string]::Equals($ClaudeOriginalConfigFingerprint, $ClaudeConfigBackupFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Claude Code user config backup hash mismatch; registration was not attempted." }',
        '  }',
        '  $ClaudeMutationStarted = $true',
        '# Remove both the legacy project-local entry and the target user entry before replacing it.',
        f'$null = Invoke-ClaudeMcpCli @("mcp", "remove", "{server_name}", "--scope", "local")',
        f'$null = Invoke-ClaudeMcpCli @("mcp", "remove", "{server_name}", "--scope", "user")',
        f'$ClaudeAddArgs = @("mcp", "add", "--transport", "stdio", "--scope", "user", "{server_name}", "--", "powershell.exe") + $LauncherArgs',
        '$ClaudeAdd = Invoke-ClaudeMcpCli $ClaudeAddArgs',
        '$ClaudeAdd.Output | Out-Host',
        'if ($ClaudeAdd.ExitCode -ne 0) { throw "Failed to register the updated Claude Code MCP entry." }',
        f'$ClaudeGet = Invoke-ClaudeMcpCli @("mcp", "get", "{server_name}")',
        '$ClaudeGet.Output | Out-Host',
        'if ($ClaudeGet.ExitCode -ne 0) { throw "Claude Code MCP registration could not be verified after writing user scope." }',
        '$ClaudeGetText = $ClaudeGet.Output -join [Environment]::NewLine',
        '$ClaudeScopeVerified = $ClaudeGetText -match "(?im)(Scope:\\s*User|user scope)"',
        '$ClaudeStatusConnected = $ClaudeGetText -match "(?im)^\\s*Status:\\s*(?:[^\\r\\n]*\\s)?Connected\\s*$"',
        'if (-not ($ClaudeScopeVerified -and $ClaudeStatusConnected)) { throw "Claude Code mcp get returned a disconnected or wrong-scope user registration." }',
        '$InstalledConfigFingerprint = Assert-ClaudeUserConfigContract $ClaudeUserConfig $LauncherArgs',
        '$SmokeArgs = @("--server-name", "' + server_name + '", "--claude-code-config", $ClaudeUserConfig, "--out-json", $ClaudeSmokeReport, "--fail-on-issue")',
        '$McpPython = Resolve-BundleModulePython "scripts.run_mcp_client_config_smoke"',
        '$SmokeExitCode = Invoke-BundlePythonModule $McpPython "scripts.run_mcp_client_config_smoke" $SmokeArgs',
        '$SmokeResult = $null',
        'if (Test-Path -LiteralPath $ClaudeSmokeReport) { try { $SmokeResult = Get-Content -LiteralPath $ClaudeSmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $SmokeResult = $null } }',
        '$SmokeResults = @($(if ($SmokeResult) { $SmokeResult.results } else { @() }))',
        '$SmokeEntry = $(if ($SmokeResults.Count -eq 1) { $SmokeResults[0] } else { $null })',
        '$SmokeConfigPathMatches = $false',
        'if ($SmokeEntry) { try { $SmokeConfigPathMatches = [string]::Equals([System.IO.Path]::GetFullPath([string]$SmokeEntry.config_path), [System.IO.Path]::GetFullPath($ClaudeUserConfig), [System.StringComparison]::OrdinalIgnoreCase) } catch { $SmokeConfigPathMatches = $false } }',
        '$SmokeVerified = $SmokeExitCode -eq 0 -and $SmokeResult -and [string]$SmokeResult.report_type -eq "mcp_client_config_smoke" -and $SmokeResult.passed -eq $true -and $SmokeResult.process_started -eq $true -and $SmokeResult.mcp_initialized -eq $true -and $SmokeResult.tools_discovered -eq $true -and $SmokeResult.end_to_end_verified -eq $true -and $SmokeResults.Count -eq 1 -and [string]$SmokeEntry.label -eq "claude_code" -and $SmokeConfigPathMatches -and [string]$SmokeEntry.command -eq "powershell.exe" -and (Test-ExactClaudeMcpArguments @($SmokeEntry.args) $LauncherArgs)',
        'if (-not $SmokeVerified) { throw "Claude Code launch contract did not complete initialize, tools/list, and get_index_status." }',
        '$PostSmokeConfigFingerprint = Assert-ClaudeUserConfigContract $ClaudeUserConfig $LauncherArgs',
        'if (-not [string]::Equals($PostSmokeConfigFingerprint, $InstalledConfigFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Claude Code user-scope config changed during installed-entry smoke verification." }',
        '$ContractCanonical = (@("user", "stdio", "powershell.exe") + $LauncherArgs) -join [char]0',
        '$ContractBytes = [Text.Encoding]::UTF8.GetBytes($ContractCanonical)',
        '$Sha256 = [Security.Cryptography.SHA256]::Create()',
        'try { $ContractFingerprint = "sha256:" + ([BitConverter]::ToString($Sha256.ComputeHash($ContractBytes)).Replace("-", "").ToLowerInvariant()) } finally { $Sha256.Dispose() }',
        '$Evidence = [ordered]@{ schema_version = "claude-code-registration-evidence-v1"; server_name = "' + server_name + '"; scope = "user"; status_connected = $true; registration_verified = $true; transport_verified = $true; config_entry_fingerprint = $ContractFingerprint }',
        '$EvidenceJson = ($Evidence | ConvertTo-Json -Depth 10) + [Environment]::NewLine',
        '$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)',
        '[System.IO.File]::WriteAllText($ClaudeEvidencePath, $EvidenceJson, $Utf8NoBom)',
        '} catch {',
        '  $ClaudeInstallError = $_',
        '  [Console]::Error.WriteLine("Claude Code transaction failed: " + [string]$ClaudeInstallError.Exception.Message)',
        '  $ClaudeRollbackComplete = $false',
        '  try {',
        '    if (-not $ClaudeMutationStarted) { $ClaudeRollbackComplete = $true }',
        '    elseif ($ClaudeConfigExisted -and $ClaudeConfigBackup -and (Test-Path -LiteralPath $ClaudeConfigBackup)) { Restore-ClaudeConfigAtomically $ClaudeConfigBackup $ClaudeUserConfig; $ClaudeRollbackComplete = $true }',
        '    elseif ((-not $ClaudeConfigExisted) -and (Test-Path -LiteralPath $ClaudeUserConfig)) { Remove-Item -LiteralPath $ClaudeUserConfig -Force; $ClaudeRollbackComplete = -not (Test-Path -LiteralPath $ClaudeUserConfig) }',
        '    else { $ClaudeRollbackComplete = $true }',
        '  } catch { Write-Warning "Claude Code config rollback failed: $($_.Exception.Message)" }',
        '  if (-not $ClaudeRollbackComplete) { throw "Claude Code MCP registration failed and the previous user config could not be restored. Original error: $($ClaudeInstallError.Exception.Message)" }',
        '  throw $ClaudeInstallError',
        '} finally {',
        '  if ($ClaudeConfigBackup -and (Test-Path -LiteralPath $ClaudeConfigBackup)) { Remove-Item -LiteralPath $ClaudeConfigBackup -Force }',
        '  if ($ClaudeConfigLockAcquired) { $ClaudeConfigMutex.ReleaseMutex() }',
        '  $ClaudeConfigMutex.Dispose()',
        '}',
    ]
    return "\n".join(lines)


def _powershell_stdio_launcher_script(
    default_server_args: list[object],
    *,
    packaged_executable: str | None = None,
    preferred_python: str | Path | None = None,
    preferred_project_root: str | Path | None = None,
) -> str:
    preferred_python_value = str(preferred_python or "").strip()
    preferred_project_root_value = str(preferred_project_root or "").strip()
    runtime_probe_base64 = _powershell_single_quoted_json(_python_runtime_probe_base64())
    lines = [
            'param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ServerArgs)',
            '$ErrorActionPreference = "Stop"',
            *_powershell_bundle_data_dir_lines(),
            "$PreferredPython = " + _powershell_single_quoted_json(preferred_python_value),
            "$PreferredProjectRoot = " + _powershell_single_quoted_json(preferred_project_root_value),
            "$RuntimeProbeBase64 = " + runtime_probe_base64,
            "$DefaultServerArgs = " + _powershell_array_literal(default_server_args),
            'if (-not $ServerArgs -or $ServerArgs.Count -eq 0) { $ServerArgs = $DefaultServerArgs }',
    ]
    if packaged_executable:
        escaped_executable = packaged_executable.replace("'", "''")
        escaped_executable_name = Path(packaged_executable).name.replace("'", "''")
        lines.extend(
            [
                f"$InstalledPackagedExe = '{escaped_executable}'",
                f"$BundledPackagedExe = Join-Path $BundleDir '{escaped_executable_name}'",
                'foreach ($PackagedExe in @($BundledPackagedExe, $InstalledPackagedExe)) {',
                '  if ([string]::IsNullOrWhiteSpace([string]$PackagedExe)) { continue }',
                '  if (Test-Path -LiteralPath $PackagedExe -PathType Leaf) {',
                '    & $PackagedExe --mcp-server @ServerArgs',
                '    exit $LASTEXITCODE',
                '  }',
                '}',
            ]
        )
    lines.extend(
        [
            *_powershell_runtime_identity_validator_lines(),
            'function Find-ProjectRoot {',
            '  $Current = $BundleDir',
            '  while ($Current) {',
            '    if ((Test-Path -LiteralPath (Join-Path $Current "pyproject.toml")) -and (Test-Path -LiteralPath (Join-Path $Current "scripts\\run_regulation_mcp.py"))) { return $Current }',
            '    $Parent = Split-Path -Parent $Current',
            '    if (-not $Parent -or $Parent -eq $Current) { break }',
            '    $Current = $Parent',
            '  }',
            '  return $null',
            '}',
            'function Get-RecordedRuntimePython {',
            '  $MarkerPath = Join-Path $BundleDir "runtime_python.json"',
            '  if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) { return $null }',
            '  try {',
            '    $Marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop',
            '    $null = [DateTimeOffset]::Parse([string]$Marker.written_at)',
            '    $Candidate = [string]$Marker.python_executable',
            '    if (-not [System.IO.Path]::IsPathRooted($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { throw "recorded runtime Python path is invalid or unavailable: $Candidate" }',
            '    $Leaf = [System.IO.Path]::GetFileNameWithoutExtension($Candidate)',
            '    if ($Leaf -notmatch "^python(?:\\d+(?:\\.\\d+)*)?$") { throw "recorded executable is not Python" }',
            '    $Resolved = (Resolve-Path -LiteralPath $Candidate).Path',
            '    if (-not (Test-RuntimeMarkerIdentity $Resolved $Marker)) { throw "runtime marker validation failed: recorded MCP command-module identity mismatch" }',
            '    if (-not (Test-McpPython $Resolved "runtime_python.json")) { throw "recorded runtime failed the Python 3.11 and package import probe" }',
            '    return $script:ValidatedMcpPython',
            '  } catch {',
            '    throw "runtime_python.json validation failed. Re-run install_local_package.ps1. $($_.Exception.Message)"',
            '  }',
            '}',
            'function Test-McpPython([string]$Candidate, [string]$CandidateLabel, [string]$SourceRoot = "") {',
            '  if ([string]::IsNullOrWhiteSpace($Candidate)) { return $false }',
            '  $Command = $null',
            '  if (Test-Path -LiteralPath $Candidate -PathType Leaf) { $Command = (Resolve-Path -LiteralPath $Candidate).Path }',
            '  else {',
            '    $ResolvedCommand = Get-Command $Candidate -ErrorAction SilentlyContinue',
            '    if ($ResolvedCommand) { $Command = $ResolvedCommand.Source }',
            '  }',
            '  if (-not $Command) {',
            '    [Console]::Error.WriteLine("Python executable not found [$CandidateLabel]: $Candidate")',
            '    return $false',
            '  }',
            '  $HadPythonPath = Test-Path Env:PYTHONPATH',
            '  $PreviousPythonPath = $env:PYTHONPATH',
            '  $HadSafePath = Test-Path Env:PYTHONSAFEPATH',
            '  $PreviousSafePath = $env:PYTHONSAFEPATH',
            '  $PreviousErrorActionPreference = $ErrorActionPreference',
            '  try {',
            '    $ErrorActionPreference = "Continue"',
            '    if ($SourceRoot) { $env:PYTHONPATH = $SourceRoot } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
            '    $env:PYTHONSAFEPATH = "1"',
            '    $ProbeDiagnostics = @(& $Command -c "import base64;exec(base64.b64decode(\'$RuntimeProbeBase64\'))" 2>&1)',
            '    $ProbeExitCode = $LASTEXITCODE',
            '  } catch {',
            '    $ProbeDiagnostics = @($_.Exception.Message)',
            '    $ProbeExitCode = 44',
            '  } finally {',
            '    $ErrorActionPreference = $PreviousErrorActionPreference',
            '    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
            '    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }',
            '  }',
            '  if ($ProbeExitCode -eq 0) { $script:ValidatedMcpPython = $Command; return $true }',
            '  $Category = switch ($ProbeExitCode) {',
            '    41 { "Python version is below 3.11" }',
            '    42 { "scripts.run_regulation_mcp import failed" }',
            '    43 { "Required dependency import failed" }',
            '    default { "Python runtime probe failed with exit code $ProbeExitCode" }',
            '  }',
            '  [Console]::Error.WriteLine("$Category [$CandidateLabel]: $Command")',
            '  foreach ($DiagnosticLine in $ProbeDiagnostics) {',
            '    if (-not [string]::IsNullOrWhiteSpace([string]$DiagnosticLine)) { [Console]::Error.WriteLine([string]$DiagnosticLine) }',
            '  }',
            '  return $false',
            '}',
            'function Get-PyLauncherPython {',
            '  $Py = Get-Command "py" -ErrorAction SilentlyContinue',
            '  if (-not $Py -or -not $Py.Source) { return $null }',
            '  foreach ($Selector in @("-3.11", "-3")) {',
            '    $PreviousErrorActionPreference = $ErrorActionPreference',
            '    try {',
            '      $ErrorActionPreference = "Continue"',
            '      $Output = @(& $Py.Source $Selector -c "import base64,os,sys; print(base64.b64encode(os.path.abspath(sys.executable).encode(\'utf-8\')).decode(\'ascii\')) if sys.version_info >= (3, 11) else sys.exit(41)" 2>$null)',
            '      $ExitCode = $LASTEXITCODE',
            '    } catch {',
            '      $ExitCode = 1',
            '      $Output = @()',
            '    } finally {',
            '      $ErrorActionPreference = $PreviousErrorActionPreference',
            '    }',
            '    if ($ExitCode -ne 0) { continue }',
            '    $EncodedCandidate = [string]($Output | Select-Object -Last 1)',
            '    try {',
            '      $Candidate = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($EncodedCandidate))',
            '    } catch {',
            '      continue',
            '    }',
            '    if ([System.IO.Path]::IsPathRooted($Candidate) -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return (Resolve-Path -LiteralPath $Candidate).Path }',
            '  }',
            '  return $null',
            '}',
            'function Invoke-RecordedRuntimeServer([string]$PythonPath, [string[]]$ArgsToPass) {',
            '  $HadPythonPath = Test-Path Env:PYTHONPATH',
            '  $PreviousPythonPath = $env:PYTHONPATH',
            '  $HadSafePath = Test-Path Env:PYTHONSAFEPATH',
            '  $PreviousSafePath = $env:PYTHONSAFEPATH',
            '  try {',
            '    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue',
            '    $env:PYTHONSAFEPATH = "1"',
            '    & $PythonPath -m scripts.run_regulation_mcp @ArgsToPass',
            '    $ServerExitCode = $LASTEXITCODE',
            '  } finally {',
            '    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
            '    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }',
            '  }',
            '  exit [int]$ServerExitCode',
            '}',
            'function Invoke-ServerFromSource([string]$ProjectRoot, [string[]]$ArgsToPass) {',
            '  $PythonCandidates = @(',
            '    [pscustomobject]@{ Path = (Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"); Label = "project .venv" },',
            '    [pscustomobject]@{ Path = $env:REG_RAG_PYTHON; Label = "REG_RAG_PYTHON" },',
            '    [pscustomobject]@{ Path = $PreferredPython; Label = "selected Python" },',
            '    [pscustomobject]@{ Path = "python"; Label = "PATH python" }',
            '  )',
            '  $PyLauncherPython = Get-PyLauncherPython',
            '  if ($PyLauncherPython) { $PythonCandidates += [pscustomobject]@{ Path = $PyLauncherPython; Label = "py launcher" } }',
            '  foreach ($Candidate in $PythonCandidates) {',
            '    if (Test-McpPython ([string]$Candidate.Path) ([string]$Candidate.Label) $ProjectRoot) {',
            '      $env:PYTHONPATH = $ProjectRoot',
            '      $env:PYTHONSAFEPATH = "1"',
            '      & $script:ValidatedMcpPython -m scripts.run_regulation_mcp @ArgsToPass',
            '      exit $LASTEXITCODE',
            '    }',
            '  }',
            '  [Console]::Error.WriteLine("No Python 3.11+ source runtime could import scripts.run_regulation_mcp from project root: $ProjectRoot")',
            '}',
            '$RecordedRuntimePython = Get-RecordedRuntimePython',
            'if ($RecordedRuntimePython) {',
            '  # Do not capture this function call in an assignment. Windows PowerShell 5.1',
            '  # buffers native stdout until the function returns, which deadlocks MCP stdio.',
            '  Invoke-RecordedRuntimeServer $RecordedRuntimePython $ServerArgs',
            '}',
            '$ProjectRoot = Find-ProjectRoot',
            'if (-not $ProjectRoot -and $PreferredProjectRoot) {',
            '  $PreferredScript = Join-Path $PreferredProjectRoot "scripts\\run_regulation_mcp.py"',
            '  if (Test-Path -LiteralPath $PreferredScript) { $ProjectRoot = $PreferredProjectRoot }',
            '}',
            'if ($ProjectRoot) { Invoke-ServerFromSource $ProjectRoot $ServerArgs }',
            'else { [Console]::Error.WriteLine("Project root discovery failed: no pyproject.toml and scripts\\run_regulation_mcp.py source root was found.") }',
            '# An extracted bundle may not contain the source checkout. When the operator points',
            '# REG_RAG_PYTHON at the installed wheel environment, invoke its packaged module',
            '# directly instead of relying on a stale console script from another PATH entry.',
            '$PackagedPythonCandidates = @()',
            '$RecordedRuntimePython = Get-RecordedRuntimePython',
            'if ($RecordedRuntimePython) { $PackagedPythonCandidates += [pscustomobject]@{ Path = $RecordedRuntimePython; Label = "runtime_python.json" } }',
            'if ($env:REG_RAG_PYTHON) { $PackagedPythonCandidates += [pscustomobject]@{ Path = $env:REG_RAG_PYTHON; Label = "REG_RAG_PYTHON" } }',
            'if ($PreferredPython) { $PackagedPythonCandidates += [pscustomobject]@{ Path = $PreferredPython; Label = "selected Python" } }',
            '$PathPython = Get-Command "python" -ErrorAction SilentlyContinue',
            'if ($PathPython) { $PackagedPythonCandidates += [pscustomobject]@{ Path = $PathPython.Source; Label = "PATH python" } }',
            '$PyLauncherPython = Get-PyLauncherPython',
            'if ($PyLauncherPython) { $PackagedPythonCandidates += [pscustomobject]@{ Path = $PyLauncherPython; Label = "py launcher" } }',
            'foreach ($Candidate in $PackagedPythonCandidates) {',
            '  if (Test-McpPython ([string]$Candidate.Path) ([string]$Candidate.Label)) {',
            '    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue',
            '    $env:PYTHONSAFEPATH = "1"',
            '    & $script:ValidatedMcpPython -m scripts.run_regulation_mcp @ServerArgs',
            '    exit $LASTEXITCODE',
            '  }',
            '}',
            '$ConsoleCommand = Get-Command "reg-rag-mcp-server" -ErrorAction SilentlyContinue',
            'if ($ConsoleCommand) {',
            '  $ConsoleProbe = Start-Process -FilePath $ConsoleCommand.Source -ArgumentList @("--help") -Wait -PassThru -WindowStyle Hidden',
            '  if ($ConsoleProbe.ExitCode -eq 0) {',
            '    & $ConsoleCommand.Source @ServerArgs',
            '    exit $LASTEXITCODE',
            '  }',
            '  throw "The installed MCP console command is not importable. Install the bundle wheel or set REG_RAG_PYTHON to its Python executable before reconnecting."',
            '}',
            'throw "No usable local MCP runtime is available. A handoff ZIP does not include the Windows application executable. On a fresh target PC, place a compatible packaged executable beside this bundle or install Python 3.11+, then run install_local_package.ps1 with an included wheel or approved package source before reconnecting."',
        ]
    )
    return "\n".join(lines)


def _powershell_bundle_data_dir_lines() -> list[str]:
    return [
        "$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path",
        '$BundleDataDir = Join-Path $BundleDir "data"',
        'if (-not (Test-Path -LiteralPath $BundleDataDir)) { throw "Bundled data directory was not found: $BundleDataDir" }',
    ]


def _powershell_bundle_runtime_module_resolver_lines() -> list[str]:
    """Resolve packaged commands through the bundle's recorded Python first.

    Double-clicked PowerShell scripts start in a fresh process, so the PATH
    update performed by install_local_package.ps1 is no longer present.  The
    persisted runtime marker is the authoritative bridge across that restart.
    """

    return [
        *_powershell_runtime_identity_validator_lines(),
        'function Test-BundlePythonModule([string]$PythonPath, [string]$ModuleName, [string]$ProjectRoot = "") {',
        '  if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { return $false }',
        '  $PreviousErrorActionPreference = $ErrorActionPreference',
        '  $HadPythonPath = Test-Path Env:PYTHONPATH',
        '  $PreviousPythonPath = $env:PYTHONPATH',
        '  $HadSafePath = Test-Path Env:PYTHONSAFEPATH',
        '  $PreviousSafePath = $env:PYTHONSAFEPATH',
        '  try {',
        '    $ErrorActionPreference = "Continue"',
        '    if ($ProjectRoot) { $env:PYTHONPATH = $ProjectRoot } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
        '    $env:PYTHONSAFEPATH = "1"',
        '    & $PythonPath -c "import importlib.util,sys; raise SystemExit(0 if sys.version_info >= (3,11) and importlib.util.find_spec(sys.argv[1]) else 42)" $ModuleName 1>$null 2>$null',
        '    return $LASTEXITCODE -eq 0',
        '  } catch {',
        '    return $false',
        '  } finally {',
        '    $ErrorActionPreference = $PreviousErrorActionPreference',
        '    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
        '    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }',
        '  }',
        '}',
        'function Resolve-BundleModulePython([string]$ModuleName) {',
        '  $script:McpResolvedSourceProjectRoot = ""',
        '  $MarkerPath = Join-Path $BundleDir "runtime_python.json"',
        '  if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) {',
        '    try {',
        '      $Marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop',
        '      $null = [DateTimeOffset]::Parse([string]$Marker.written_at)',
        '      $Candidate = [string]$Marker.python_executable',
        '      if (-not [System.IO.Path]::IsPathRooted($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { throw "recorded Python is unavailable" }',
        '      $Leaf = [System.IO.Path]::GetFileNameWithoutExtension($Candidate)',
        '      if ($Leaf -notmatch "^python(?:\\d+(?:\\.\\d+)*)?$") { throw "recorded executable is not Python" }',
        '      $Resolved = (Resolve-Path -LiteralPath $Candidate).Path',
        '      if (-not (Test-RuntimeMarkerIdentity $Resolved $Marker)) { throw "recorded MCP command-module identity mismatch" }',
        '      return $Resolved',
        '    } catch {',
        '      throw "runtime_python.json is invalid for $ModuleName. Re-run install_local_package.ps1. $($_.Exception.Message)"',
        '    }',
        '  }',
        '  if ($script:McpPreferredPython -and $script:McpPreferredProjectRoot -and (Test-BundlePythonModule $script:McpPreferredPython $ModuleName $script:McpPreferredProjectRoot)) {',
        '    $script:McpResolvedSourceProjectRoot = $script:McpPreferredProjectRoot',
        '    return (Resolve-Path -LiteralPath $script:McpPreferredPython).Path',
        '  }',
        '  $EnvProjectRoot = ""',
        '  if ($env:REG_RAG_PYTHON_PROJECT_ROOT -and (Test-Path -LiteralPath $env:REG_RAG_PYTHON_PROJECT_ROOT -PathType Container)) {',
        '    $EnvProjectRoot = (Resolve-Path -LiteralPath $env:REG_RAG_PYTHON_PROJECT_ROOT).Path',
        '  }',
        '  if ($env:REG_RAG_PYTHON -and (Test-BundlePythonModule $env:REG_RAG_PYTHON $ModuleName $EnvProjectRoot)) {',
        '    if ($EnvProjectRoot) { $script:McpResolvedSourceProjectRoot = $EnvProjectRoot }',
        '    return (Resolve-Path -LiteralPath $env:REG_RAG_PYTHON).Path',
        '  }',
        '  throw "No recorded or explicitly selected Python 3.11+ runtime can import $ModuleName. Run install_local_package.ps1 once, then retry."',
        '}',
        'function Invoke-BundlePythonModule([string]$PythonPath, [string]$ModuleName, [object[]]$Arguments) {',
        '  $PreviousErrorActionPreference = $ErrorActionPreference',
        '  $HadPythonPath = Test-Path Env:PYTHONPATH',
        '  $PreviousPythonPath = $env:PYTHONPATH',
        '  $HadSafePath = Test-Path Env:PYTHONSAFEPATH',
        '  $PreviousSafePath = $env:PYTHONSAFEPATH',
        '  try {',
        '    $ErrorActionPreference = "Continue"',
        '    if ($script:McpResolvedSourceProjectRoot) { $env:PYTHONPATH = $script:McpResolvedSourceProjectRoot } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
        '    $env:PYTHONSAFEPATH = "1"',
        '    & $PythonPath -m $ModuleName @Arguments 2>&1 | Out-Host',
        '    return [int]$LASTEXITCODE',
        '  } finally {',
        '    $ErrorActionPreference = $PreviousErrorActionPreference',
        '    if ($HadPythonPath) { $env:PYTHONPATH = $PreviousPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }',
        '    if ($HadSafePath) { $env:PYTHONSAFEPATH = $PreviousSafePath } else { Remove-Item Env:PYTHONSAFEPATH -ErrorAction SilentlyContinue }',
        '  }',
        '}',
    ]


def _powershell_array_literal(args: list[object] | tuple[object, ...]) -> str:
    return "@(" + ", ".join(_powershell_array_value(str(arg)) for arg in args) + ")"


def _powershell_array_value(value: str) -> str:
    if value == BUNDLE_DATA_DIR_ARG or re.fullmatch(r"\$env:[A-Za-z_][A-Za-z0-9_]*", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _powershell_command(
    command: str,
    args: list[object] | tuple[object, ...] | None = None,
    *,
    prequoted_indexes: set[int] | None = None,
) -> str:
    quoted_indexes = prequoted_indexes or set()
    parts = [command]
    for index, arg in enumerate(args or []):
        value = str(arg)
        parts.append(value if index in quoted_indexes else _powershell_arg(value))
    return " ".join(parts)


def _powershell_arg(value: str) -> str:
    if not value:
        return "''"
    if value == BUNDLE_DATA_DIR_ARG or re.fullmatch(r"\$env:[A-Za-z_][A-Za-z0-9_]*", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _powershell_single_quoted_json(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _slug(value: str) -> str:
    cleaned = []
    for char in value.strip().lower():
        if char.isascii() and char.isalnum():
            cleaned.append(char)
        elif char in {"-", "_", "."}:
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    return slug or "govreg"


def _server_args(
    *,
    data_dir: str,
    tenant_id: str,
    profile_id: str | None,
    transport: str,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    tenant_storage_isolation: bool,
    tool_profile: str = "full",
) -> list[str]:
    args = [
        "--data-dir",
        data_dir,
        "--tenant-id",
        tenant_id,
        "--transport",
        transport,
    ]
    if profile_id:
        args.extend(["--profile-id", profile_id])
    if actor:
        args.extend(["--actor", actor])
    if role:
        args.extend(["--role", role])
    for department_id in department_ids or []:
        if department_id:
            args.extend(["--department-id", department_id])
    if tenant_storage_isolation:
        args.append("--tenant-storage-isolation")
    else:
        args.append("--flat-storage")
    # Keep the tool surface explicit so a future server default cannot silently
    # change what generated local or remote client profiles expose.
    args.extend(["--tool-profile", tool_profile])
    return args


def _with_bundle_stdio_fast_start(config: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(config, ensure_ascii=False))

    def patch_node(node: Any) -> None:
        if not isinstance(node, dict):
            return
        args = node.get("args")
        if node.get("command") == "reg-rag-mcp-server" and isinstance(args, list):
            transport = _arg_value(args, "--transport")
            if transport in {None, "stdio"}:
                node["args"] = _with_no_warm_cache(args)
        server_command = node.get("serverCommand")
        if isinstance(server_command, dict):
            patch_node(server_command)
        servers = node.get("mcpServers")
        if isinstance(servers, dict):
            for server in servers.values():
                patch_node(server)

    patch_node(payload)
    return payload


def _with_no_warm_cache(args: list[Any]) -> list[Any]:
    updated = [str(arg) for arg in args]
    if "--no-warm-cache" not in updated:
        updated.append("--no-warm-cache")
    return updated


def _arg_value(args: list[Any], name: str) -> str | None:
    values = [str(arg) for arg in args]
    try:
        index = values.index(name)
    except ValueError:
        return None
    if index + 1 >= len(values):
        return None
    return values[index + 1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a generic MCP client config snippet.")
    parser.add_argument("--server-name", default="regulation_mcp")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument(
        "--profile-id",
        default=None,
        help="Institution profile to bind to generated MCP server commands and runtime bundle exports.",
    )
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help=(
            "When writing a setup bundle, export the approved document into bundle-local runtime data. "
            "Repeat this option to export a selected regulation set."
        ),
    )
    parser.add_argument(
        "--skip-runtime-data",
        action="store_true",
        help="Write setup/config artifacts without exporting runtime data; useful for source-only handoff bundles.",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument(
        "--client-profile",
        choices=[
            "generic",
            "claude-desktop",
            "claude-code",
            "chatgpt-desktop-local",
            "chatgpt-remote",
            "chatgpt",
            "claude-remote",
            "claude-api",
            "bundle",
        ],
        default="generic",
        help=(
            "Output shape for the target client. chatgpt-desktop-local is an accepted legacy input "
            "that emits an explicit unsupported warning and no runnable config. Use chatgpt-remote "
            "for ChatGPT web with a reachable HTTPS MCP endpoint, claude-remote for Claude HTTPS, "
            "or bundle for supported clients. The chatgpt and claude-api values remain legacy aliases."
        ),
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="Reachable HTTPS base URL or /mcp URL for ChatGPT/remote HTTP clients.",
    )
    parser.add_argument(
        "--remote-auth-token-env",
        default="MCP_AUTH_TOKEN",
        help=(
            "Environment variable used by generated remote validation and compatible clients for bearer auth. "
            "ChatGPT web authentication is selected when creating the app; keep token values out of generated files."
        ),
    )
    parser.add_argument(
        "--approved-public-unauthenticated",
        action="store_true",
        help=(
            "Generate remote connector artifacts for an explicitly approved public read-only endpoint "
            "without a bearer-token environment variable."
        ),
    )
    parser.add_argument(
        "--chatgpt-oauth-ready",
        action="store_true",
        help=(
            "Optionally attest that the public /mcp endpoint implements and has been tested with MCP OAuth 2.1. "
            "A valid HTTPS endpoint can also use bearer_token_env_var or approved public read-only mode."
        ),
    )
    parser.add_argument(
        "--min-visible-records",
        type=int,
        default=1,
        help="Minimum MCP-visible records required by generated index visibility and doctor commands.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--actor", default=None)
    parser.add_argument("--role", default=None)
    parser.add_argument("--department-id", action="append", default=[])
    parser.add_argument("--out-json", default=None)
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Write a copy/paste-ready setup bundle. Best used with --client-profile bundle.",
    )
    parser.add_argument(
        "--zip-out",
        default=None,
        help="Zip the generated setup bundle for handoff. Requires --out-dir and --client-profile bundle.",
    )
    parser.add_argument(
        "--include-wheel",
        action="store_true",
        help=(
            "Include the latest dist/reg_rag_preprocessor-*.whl in the setup bundle zip. "
            "The wheel is package payload only; a fresh target still needs Python 3.11+ unless a "
            "compatible packaged executable is supplied beside the bundle."
        ),
    )
    parser.add_argument(
        "--wheel-path",
        default=None,
        help="Specific wheel file to include in the setup bundle zip. Implies --include-wheel.",
    )
    parser.add_argument(
        "--wheel-dist-dir",
        default="dist",
        help="Directory searched for the latest wheel when --include-wheel is used.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = build_mcp_client_config(
        server_name=args.server_name,
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        profile_id=args.profile_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        transport=args.transport,
        host=args.host,
        port=args.port,
        actor=args.actor,
        role=args.role,
        department_ids=args.department_id,
        client_profile=args.client_profile,
        public_url=args.public_url,
        remote_auth_token_env=(
            None if args.approved_public_unauthenticated else args.remote_auth_token_env
        ),
        chatgpt_oauth_ready=args.chatgpt_oauth_ready,
        min_visible_records=args.min_visible_records,
    )
    output_config = config
    if args.out_dir:
        if args.client_profile != "bundle":
            raise SystemExit("--out-dir requires --client-profile bundle.")
        write_mcp_setup_bundle(config, args.out_dir, server_name=args.server_name)
        if args.skip_runtime_data:
            _remove_runtime_data_bundle(Path(args.out_dir))
        else:
            selected_document_ids = [str(value or "").strip() for value in args.document_id if str(value or "").strip()]
            write_mcp_runtime_data_bundle(
                source_data_dir=args.data_dir,
                out_dir=args.out_dir,
                tenant_id=args.tenant_id,
                profile_id=args.profile_id,
                document_id=selected_document_ids[0] if len(selected_document_ids) == 1 else None,
                document_ids=selected_document_ids if len(selected_document_ids) > 1 else None,
                scope=(
                    "document"
                    if len(selected_document_ids) == 1
                    else "selected_documents"
                    if selected_document_ids
                    else None
                ),
                tenant_storage_isolation=args.tenant_storage_isolation,
                actor=args.actor,
                role=args.role,
                department_ids=args.department_id,
            )
        final_bundle_config = Path(args.out_dir) / SETUP_BUNDLE_FILES["full_config"]
        if final_bundle_config.is_file():
            output_config = json.loads(final_bundle_config.read_text(encoding="utf-8-sig"))
    if args.zip_out:
        if args.client_profile != "bundle":
            raise SystemExit("--zip-out requires --client-profile bundle.")
        if not args.out_dir:
            raise SystemExit("--zip-out requires --out-dir.")
        write_mcp_setup_bundle_zip(
            args.out_dir,
            args.zip_out,
            include_wheel=args.include_wheel,
            wheel_path=args.wheel_path,
            dist_dir=args.wheel_dist_dir,
        )
    payload = json.dumps(output_config, ensure_ascii=False, indent=2)
    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
