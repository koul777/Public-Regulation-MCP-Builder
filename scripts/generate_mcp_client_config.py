from __future__ import annotations

import argparse
import base64
import copy
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import zipfile
from functools import wraps
from uuid import uuid4
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.mcp_bundle_contract import (
    ALL_SETUP_BUNDLE_FILES,
    SETUP_BUNDLE_FILES,
    chatgpt_local_marketplace_name as _chatgpt_local_marketplace_name,
    normalized_chatgpt_plugin_name as _normalized_plugin_name,
)
from scripts.mcp_client_status import (
    create_bundle_status as create_client_connection_status,
    invalidate_runtime as invalidate_client_connection_runtime,
)
from app.api import routes_rag
from app.core.tenant_access import tenant_storage_key
from app.ingestion.vector_adapter import stable_content_hash
from app.mcp_server.regulation_tools import mcp_auth_context, settings_for_mcp_project
from app.retrieval.bm25_index import write_bm25_index
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    canonicalize_runtime_records,
    hierarchical_index_path,
    write_vector_records_with_offsets,
)
from app.services.regulation_catalog_service import filter_to_latest_active_versions
from app.storage.repository import JsonRepository


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
CHATGPT_DESKTOP_PLUGIN_TEMPLATE_REVISION = "chatgpt-desktop-local-plugin-v5"
SAFE_MCP_SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CLAUDE_CODE_RESERVED_MCP_SERVER_NAMES = frozenset(
    {"workspace", "claude-in-chrome", "computer-use", "claude-preview", "claude-browser"}
)
ACTIVE_LOCAL_INSTALLATION_STATES = {
    "preflight_direct",
    "preflight_plugin",
    "preflight_claude_code",
    "preflight_claude_desktop",
    "installing",
    "installing_plugin",
    "plugin_installed_pending_loader_verification",
}
BUNDLE_GENERATION_TRANSITIONAL_STATES = {
    "setup_refresh_in_progress",
    "runtime_refresh_in_progress",
}
RUNTIME_PYTHON_MARKER_FILENAME = "runtime_python.json"
RUNTIME_PYTHON_MARKER_SCHEMA_VERSION = 2
RUNTIME_IDENTITY_SCOPE = "mcp-command-modules-v1"
AGENT_CONNECT_BUNDLE_NAME_MARKER = "<PROGRAM_BUNDLE_NAME>"
AGENT_CONNECT_BUNDLE_DIR_MARKER = "<PROGRAM_BUNDLE_DIR>"
AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER = "<PROGRAM_BUNDLE_DIR_PS_LITERAL>"
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


def _agent_connect_bundle_context(
    *,
    prompt_file: str,
    fallback_file: str,
) -> str:
    return f"""생성 프로그램이 지정한 번들 폴더 이름: `{AGENT_CONNECT_BUNDLE_NAME_MARKER}`
생성 프로그램이 지정한 번들 절대경로: `{AGENT_CONNECT_BUNDLE_DIR_MARKER}`

반드시 위 폴더 하나를 번들 루트로 사용하고 다음 핵심 구조를 먼저 확인해.

```text
{AGENT_CONNECT_BUNDLE_NAME_MARKER}\\
├─ {prompt_file}
├─ manifest.json
├─ bundle_status.json
├─ connect_mcp_client.ps1
├─ install_local_package.ps1
├─ run_mcp_stdio_server.ps1
├─ {fallback_file}
├─ data\\
├─ reg_rag_preprocessor-*.whl  (독립 배포용 wheel을 포함한 경우)
└─ runtime_python.json         (설치가 성공하면 생성 또는 갱신)
```
"""


def render_agent_connect_prompt_for_program(
    prompt_text: str,
    *,
    bundle_dir: str | Path,
    source_name: str | Path | None = None,
) -> str:
    """Materialize the current local bundle path only in the operator UI copy text.

    Prompt files stored in the transferable ZIP keep placeholders so they do not
    disclose the generation host path and continue to work after relocation.
    """

    resolved_bundle_dir = str(Path(bundle_dir).resolve())
    if "\r" in resolved_bundle_dir or "\n" in resolved_bundle_dir:
        raise ValueError("bundle_dir must not contain line breaks.")
    powershell_literal = "'" + resolved_bundle_dir.replace("'", "''") + "'"
    bundle_name = Path(resolved_bundle_dir).name
    source_leaf = (
        str(source_name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
    )
    legacy_chatgpt_prompt_name = "CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md"
    has_program_path_markers = (
        AGENT_CONNECT_BUNDLE_NAME_MARKER in prompt_text
        or AGENT_CONNECT_BUNDLE_DIR_MARKER in prompt_text
        or AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER in prompt_text
    )
    if (
        source_leaf == legacy_chatgpt_prompt_name.casefold()
        or legacy_chatgpt_prompt_name.casefold() in prompt_text.casefold()
    ):
        return _render_legacy_chatgpt_desktop_guide(bundle_dir=Path(resolved_bundle_dir))
    rendered = prompt_text.replace(
        AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER,
        powershell_literal,
    ).replace(
        AGENT_CONNECT_BUNDLE_NAME_MARKER,
        bundle_name,
    ).replace(
        AGENT_CONNECT_BUNDLE_DIR_MARKER,
        resolved_bundle_dir,
    )
    if has_program_path_markers:
        return rendered

    # Bundles created before the program-path markers were introduced should
    # become immediately usable when their prompt is copied from the updated
    # operator UI. Replace only the known first discovery step and preserve the
    # rest of the signed-off connection procedure verbatim.
    legacy_step = re.compile(
        r"^1\. 현재 작업공간에서 `(?P<prompt_file>[^`]+)`[^\r\n]*$",
        flags=re.MULTILINE,
    )
    match = legacy_step.search(rendered)
    if match is None:
        return rendered
    prompt_file = match.group("prompt_file")
    fallback_by_prompt = {
        "CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md": "ChatGPT Desktop에 연결하기.bat",
        "CHATGPT_DESKTOP_CONNECT_GUIDE.md": "ChatGPT Desktop에 연결하기.bat",
        "CODEX_AGENT_CONNECT_PROMPT.md": "Codex에 연결하기.bat",
        "CLAUDE_CODE_AGENT_CONNECT_PROMPT.md": "Claude Code에 연결하기.bat",
    }
    portable_context = _agent_connect_bundle_context(
        prompt_file=prompt_file,
        fallback_file=fallback_by_prompt.get(prompt_file, "<대상별 연결 BAT>"),
    )
    materialized_context = portable_context.replace(
        AGENT_CONNECT_BUNDLE_NAME_MARKER,
        bundle_name,
    ).replace(
        AGENT_CONNECT_BUNDLE_DIR_MARKER,
        resolved_bundle_dir,
    )
    first_line_end = rendered.find("\n")
    if first_line_end >= 0:
        rendered = rendered[: first_line_end + 1] + "\n" + materialized_context + rendered[first_line_end + 1 :]
    else:
        rendered = rendered + "\n\n" + materialized_context
    match = legacy_step.search(rendered)
    if match is None:
        return rendered
    replacement = (
        "1. 생성 프로그램이 지정한 현재 번들 폴더를 사용해 "
        f"`$BundleDir = {powershell_literal}`를 실행하고, `{prompt_file}`, `manifest.json`, "
        "`bundle_status.json`, `connect_mcp_client.ps1`이 모두 그 폴더 바로 아래에 있는지 확인한 뒤 "
        "`Set-Location -LiteralPath $BundleDir`을 실행해. 경로가 없거나 접근할 수 없으면 임의 경로를 "
        "검색하거나 설치하지 말고 그 정확한 폴더를 작업공간으로 열거나 추가해 달라고 요청해."
    )
    return legacy_step.sub(lambda _: replacement, rendered, count=1)


def _render_legacy_chatgpt_desktop_guide(*, bundle_dir: Path) -> str:
    """Replace obsolete Desktop agent instructions with the current settings guide.

    Older bundles told a ChatGPT Desktop conversation to execute the Codex CLI
    installer.  Reusing that text after only materializing its path would keep
    the unsafe product mismatch alive.  Recover only the local stdio launch
    contract, relocate its path-bearing arguments, and otherwise fail closed
    with a regeneration notice.
    """

    config_path = bundle_dir / SETUP_BUNDLE_FILES["chatgpt_desktop_local"]
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _legacy_chatgpt_desktop_regeneration_notice(bundle_dir)

    ui_fields = payload.get("ui_fields")
    server_name = str(payload.get("server_name") or "").strip()
    if not isinstance(ui_fields, dict):
        return _legacy_chatgpt_desktop_regeneration_notice(bundle_dir)
    if not server_name:
        server_name = str(ui_fields.get("name") or "").strip()
    if not SAFE_MCP_SERVER_NAME.fullmatch(server_name):
        return _legacy_chatgpt_desktop_regeneration_notice(bundle_dir)
    if str(ui_fields.get("command") or "").strip().lower() != "powershell.exe":
        return _legacy_chatgpt_desktop_regeneration_notice(bundle_dir)
    raw_args = ui_fields.get("args")
    if not isinstance(raw_args, list) or not all(isinstance(arg, str) for arg in raw_args):
        return _legacy_chatgpt_desktop_regeneration_notice(bundle_dir)

    relocated_args = _validated_legacy_chatgpt_desktop_args(raw_args, bundle_dir=bundle_dir)
    if relocated_args is None:
        return _legacy_chatgpt_desktop_regeneration_notice(bundle_dir)

    current_payload = copy.deepcopy(payload)
    current_payload["ui_fields"] = {
        **ui_fields,
        "name": server_name,
        "cwd": str(bundle_dir.resolve()),
        "args": relocated_args,
    }
    portable_guide = _chatgpt_desktop_setup_guide(
        server_name,
        config=current_payload,
        bundle_dir=bundle_dir,
        allow_bat_fallback=False,
    )
    return render_agent_connect_prompt_for_program(portable_guide, bundle_dir=bundle_dir)


def _validated_legacy_chatgpt_desktop_args(
    raw_args: list[str],
    *,
    bundle_dir: Path,
) -> list[str] | None:
    """Validate and relocate only the generated local-stdio launcher grammar."""

    launcher_path = (bundle_dir / SETUP_BUNDLE_FILES["stdio_launcher"]).resolve()
    data_dir = (bundle_dir / "data").resolve()
    if not launcher_path.is_file() or not data_dir.is_dir():
        return None
    if len(raw_args) < 12 or any("\x00" in value or "\r" in value or "\n" in value for value in raw_args):
        return None
    if [value.casefold() for value in raw_args[:4]] != [
        "-noprofile",
        "-executionpolicy",
        "bypass",
        "-file",
    ]:
        return None
    if _path_leaf(raw_args[4]) != SETUP_BUNDLE_FILES["stdio_launcher"].casefold():
        return None

    server_args = raw_args[5:]
    value_flags = {
        "--data-dir",
        "--tenant-id",
        "--transport",
        "--profile-id",
        "--actor",
        "--role",
        "--department-id",
        "--tool-profile",
    }
    repeatable_value_flags = {"--department-id"}
    storage_flags = {"--flat-storage", "--tenant-storage-isolation"}
    switch_flags = {"--no-warm-cache"}
    forbidden_tokens = ("codex", "connect_mcp_client", "installcodex", "installpackage")
    values: dict[str, list[str]] = {}
    storage_flag: str | None = None
    seen_switches: set[str] = set()
    index = 0
    while index < len(server_args):
        flag = server_args[index]
        normalized_flag = flag.casefold()
        if any(token in normalized_flag for token in forbidden_tokens):
            return None
        if normalized_flag in value_flags:
            if index + 1 >= len(server_args):
                return None
            value = server_args[index + 1]
            normalized_value = value.casefold()
            if not value or value.startswith("-") or any(
                token in normalized_value for token in forbidden_tokens
            ):
                return None
            if normalized_flag not in repeatable_value_flags and normalized_flag in values:
                return None
            values.setdefault(normalized_flag, []).append(value)
            index += 2
            continue
        if normalized_flag in storage_flags:
            if storage_flag is not None:
                return None
            storage_flag = normalized_flag
            index += 1
            continue
        if normalized_flag in switch_flags:
            if normalized_flag in seen_switches:
                return None
            seen_switches.add(normalized_flag)
            index += 1
            continue
        # This rejects unexpected single-dash PowerShell parameters as well as
        # unknown or malformed server flags.
        return None

    required_value_flags = {"--data-dir", "--tenant-id", "--transport", "--tool-profile"}
    if not required_value_flags.issubset(values):
        return None
    if storage_flag is None or "--no-warm-cache" not in seen_switches:
        return None
    if values["--transport"] != ["stdio"] or values["--tool-profile"] != ["chatgpt-data"]:
        return None

    canonical_server_args = [
        "--data-dir",
        values["--data-dir"][0],
        "--tenant-id",
        values["--tenant-id"][0],
        "--transport",
        "stdio",
    ]
    for flag in ("--profile-id", "--actor", "--role"):
        if flag in values:
            canonical_server_args.extend([flag, values[flag][0]])
    for department_id in values.get("--department-id", []):
        canonical_server_args.extend(["--department-id", department_id])
    canonical_server_args.extend([storage_flag, "--tool-profile", "chatgpt-data", "--no-warm-cache"])
    if [value.casefold() for value in server_args] != [
        value.casefold() for value in canonical_server_args
    ]:
        return None

    relocated_args = list(raw_args)
    relocated_args[4] = str(launcher_path)
    data_index = next(
        index for index, value in enumerate(relocated_args) if value.casefold() == "--data-dir"
    )
    relocated_args[data_index + 1] = str(data_dir)
    return relocated_args


def _legacy_chatgpt_desktop_regeneration_notice(bundle_dir: Path) -> str:
    return f"""# ChatGPT Desktop MCP 연결 안내 갱신 필요

현재 폴더: `{bundle_dir.resolve()}`

이 폴더의 `CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md`는 ChatGPT Desktop에 Codex CLI 설치를 요청하던 구형 형식이며 실행하면 안 됩니다. 구형 `ChatGPT Desktop에 연결하기.bat`도 실행하지 마세요.

현재 프로그램에서 MCP 파일 묶음을 다시 생성한 뒤 새 `CHATGPT_DESKTOP_CONNECT_GUIDE.md`의 Name·Command·Working directory·Arguments를 ChatGPT Desktop의 `Settings > MCP servers > Add server`에 입력하세요.
"""


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


def _guard_local_mcp_materialization(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        with _windows_named_mutex("Local\\PRMCPBuilder-LocalMcpInstallation", timeout_ms=30_000):
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
        "claude-api",
        "bundle",
    }
    if normalized_profile not in valid_profiles:
        raise ValueError(
            "client_profile must be generic, claude-desktop, claude-code, chatgpt-desktop-local, "
            "chatgpt-remote, chatgpt (legacy remote alias), claude-api, or bundle."
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
        chatgpt_desktop_local = build_mcp_client_config(
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
            client_profile="chatgpt-desktop-local",
            remote_auth_token_env=remote_auth_token_env,
        )
        chatgpt_desktop_local = _with_bundle_stdio_fast_start(chatgpt_desktop_local)
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
        claude_api = build_mcp_client_config(
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
            client_profile="claude-api",
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
                claude_api=claude_api,
                remote_auth_token_env=remote_auth_token_env,
                min_visible_records=min_visible_records,
            ),
            "claude_desktop": claude_desktop,
            "claude_code": claude_code,
            "chatgpt_desktop_local": chatgpt_desktop_local,
            "chatgpt_remote": chatgpt_remote,
            # Backward-compatible alias. New code and generated guidance use chatgpt_remote.
            "chatgpt": chatgpt_remote,
            "claude_api": claude_api,
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
    if normalized_profile == "claude-api":
        return _claude_api_connector_config(
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
) -> dict[str, str]:
    """Write copy/paste-ready MCP setup artifacts for common clients."""
    server_name = _validate_mcp_server_name(server_name)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_active_bundle_installation(output_dir)
    _clear_stale_bundle_status_reports(output_dir)
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
    quickstart = json_config.get("quickstart") if isinstance(json_config, dict) else None
    if not isinstance(quickstart, dict):
        quickstart = {}
    # ``copy_paste`` values are executable PowerShell, not structured command
    # metadata.  Keep their original bundle-relative $BundleDir/$BundleDataDir
    # expressions instead of freezing the generation machine's output path.
    source_copy_paste = source_quickstart.get("copy_paste")
    if isinstance(source_copy_paste, dict):
        quickstart["copy_paste"] = copy.deepcopy(source_copy_paste)
    # The executable tunnel script is bundle-relative.  Keep duplicate JSON
    # copy/paste fields equally movable instead of freezing the generation
    # directory into them; other structured command fields remain explicit
    # so readiness tools can inspect their resolved paths.
    bundle_tunnel = source_quickstart.get("openai_secure_tunnel")
    bundle_tunnel_script = (
        bundle_tunnel.get("copy_paste_ps")
        if isinstance(bundle_tunnel, dict)
        else None
    )
    if isinstance(bundle_tunnel_script, str):
        quickstart_tunnel = quickstart.get("openai_secure_tunnel")
        if isinstance(quickstart_tunnel, dict):
            quickstart_tunnel["copy_paste_ps"] = bundle_tunnel_script
        quickstart_copy_paste = quickstart.get("copy_paste")
        if isinstance(source_copy_paste, dict) and isinstance(quickstart_copy_paste, dict):
            source_tunnel_copy = source_copy_paste.get("openai_secure_tunnel_ps")
            if isinstance(source_tunnel_copy, str):
                quickstart_copy_paste["openai_secure_tunnel_ps"] = source_tunnel_copy
        for profile_name in ("chatgpt_remote", "chatgpt"):
            profile = json_config.get(profile_name)
            tunnel = profile.get("openai_secure_tunnel") if isinstance(profile, dict) else None
            if isinstance(tunnel, dict):
                tunnel["copy_paste_ps"] = bundle_tunnel_script
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
            "run_http",
            "run_chatgpt",
            "openai_tunnel",
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
        elif path.suffix.lower() == ".bat":
            _write_utf8_no_bom(path, rendered.replace("\r\n", "\n").replace("\n", "\r\n"))
        else:
            _write_utf8_no_bom(path, rendered)
        files[key] = str(path)

    write_json("full_config", json_config)
    claude_desktop_stdio_config: dict[str, Any] = {}
    chatgpt_codex_stdio_config: dict[str, Any] = {}
    chatgpt_desktop_local_payload: dict[str, Any] = {}
    if "claude_desktop" in json_config:
        claude_desktop_stdio_config = _local_stdio_config_for_server(
            json_config["claude_desktop"],
            server_name=server_name,
        )
        write_json("claude_desktop", claude_desktop_stdio_config)
        chatgpt_codex_stdio_config = _local_stdio_config_for_server(
            json_config.get("chatgpt_desktop_local") or json_config["claude_desktop"],
            server_name=server_name,
        )
        codex_snippet = _codex_config_snippet(chatgpt_codex_stdio_config, server_name=server_name)
        if codex_snippet:
            write_text("codex_config", codex_snippet)
        chatgpt_desktop_local_payload = _chatgpt_desktop_local_config(
            chatgpt_codex_stdio_config,
            server_name=server_name,
            bundle_dir=output_dir,
        )
        write_json("chatgpt_desktop_local", chatgpt_desktop_local_payload)
        files.update(
            _write_chatgpt_desktop_local_plugin(
                chatgpt_codex_stdio_config,
                output_dir=output_dir,
                server_name=server_name,
            )
        )
        write_text(
            "codex_plugin_guide",
            _codex_plugin_manual_guide(
                chatgpt_codex_stdio_config,
                server_name=server_name,
                bundle_dir=output_dir,
            ),
        )
    if "chatgpt_remote" in json_config or "chatgpt" in json_config:
        write_json("chatgpt", json_config.get("chatgpt_remote") or json_config["chatgpt"])
    if "claude_api" in json_config:
        write_json("claude_api", json_config["claude_api"])
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
    if copy_paste.get("run_http_server_ps"):
        write_text("run_http", copy_paste["run_http_server_ps"])
    if copy_paste.get("run_chatgpt_data_server_ps"):
        write_text("run_chatgpt", copy_paste["run_chatgpt_data_server_ps"])
    if copy_paste.get("openai_secure_tunnel_ps"):
        write_text("openai_tunnel", copy_paste["openai_secure_tunnel_ps"])
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
            chatgpt_desktop_config=chatgpt_codex_stdio_config,
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
    write_text(
        "chatgpt_desktop_agent_prompt",
        _chatgpt_desktop_setup_guide(
            server_name,
            config=chatgpt_desktop_local_payload,
            bundle_dir=output_dir,
        ),
    )
    write_text("codex_agent_prompt", _codex_agent_connect_prompt(server_name))
    write_text("claude_code_agent_prompt", _claude_code_agent_connect_prompt(server_name))
    write_text(
        "usage_guide_bat",
        _windows_open_text_file_script(SETUP_BUNDLE_FILES["usage_guide"]),
    )
    write_text(
        "connect_codex_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-InstallPackage -Target codex -InstallCodex",
            next_steps=[
                "Codex를 완전히 종료한 뒤 다시 실행합니다.",
                "새 task에서 /mcp를 입력해 등록 이름을 확인합니다.",
                f"{server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘. 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_chatgpt_desktop_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-InstallPackage -Target chatgpt-desktop-direct",
            next_steps=[
                "ChatGPT Desktop을 완전히 종료한 뒤 다시 실행합니다.",
                "ChatGPT Desktop에서 새 대화를 엽니다.",
                f"/mcp를 입력해 {server_name}이 연결됨으로 보이는지 먼저 확인합니다.",
                f"{server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘. 라고 입력합니다.",
                "이 BAT는 ChatGPT Desktop이 읽는 공유 로컬 MCP 설정 항목을 기록·검증하는 보조 수단입니다.",
            ],
        ),
    )
    write_text(
        "connect_claude_desktop_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-InstallPackage -Target claude-desktop -InstallClaudeDesktop",
            next_steps=[
                "Claude Desktop을 완전히 종료한 뒤 다시 실행합니다.",
                f"새 대화에서 {server_name} MCP를 사용해서 등록된 규정 목록을 보여줘 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_claude_code_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-InstallPackage -Target claude-code",
            next_steps=[
                "Claude Code를 다시 실행합니다.",
                "대화에서 /mcp를 입력해 등록 이름을 확인합니다.",
                f"{server_name} MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘. 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_chatgpt_https_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target chatgpt-remote",
            next_steps=[
                "공개 endpoint의 MCP OAuth 2.1 검증을 마친 뒤 --chatgpt-oauth-ready로 번들을 생성합니다. 정적 MCP_AUTH_TOKEN은 ChatGPT에 입력할 수 없습니다.",
                "ChatGPT Settings > Security and login에서 Developer mode를 켠 뒤 Settings > Plugins 또는 https://chatgpt.com/plugins 의 +를 엽니다.",
                f"복사된 HTTPS 주소로 앱 이름을 {server_name} 으로 만들고 발견된 도구 목록의 search와 fetch를 확인합니다.",
                f"새 대화의 tools 메뉴에서 앱을 선택한 뒤 {server_name}의 search로 인사규정을 찾고 fetch로 첫 결과 원문과 출처를 확인해줘 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_chatgpt_tunnel_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target chatgpt-tunnel",
            next_steps=[
                "ChatGPT 웹의 Settings > Security and login에서 Developer mode를 켠 뒤 Settings > Plugins 또는 https://chatgpt.com/plugins 를 엽니다.",
                f"+로 {server_name} 앱을 만들 때 Connection을 Tunnel로 선택하고 승인된 tunnel_id를 지정합니다.",
                f"새 대화의 + > More에서 앱을 선택한 뒤 {server_name}의 search로 인사규정을 찾고 fetch로 첫 결과 원문과 출처를 확인해줘 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_claude_https_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target claude-api",
            next_steps=[
                f"생성된 HTTPS 설정에서 MCP 이름 {server_name} 과 URL을 확인합니다.",
                "이 BAT는 Claude Messages API용 JSON 조각을 표시하며 Claude 앱의 connector 설정을 자동 변경하지 않습니다.",
                f"Messages API에서는 {server_name} MCP를 활성화한 뒤 search로 인사규정을 찾고 fetch로 첫 결과 원문과 출처를 확인합니다.",
                "Claude 앱에서 쓸 때는 JSON 조각이 아니라 같은 HTTPS MCP URL만 Customize > Connectors에 등록합니다.",
            ],
        ),
    )
    write_text(
        "doctor_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target doctor",
        ),
    )

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
            "claude_api": bool((json_config.get("claude_api") or {}).get("ready")),
        },
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
    backup_dir = output_dir.parent / f".{output_dir.name}.setup-backup-{uuid4().hex}"
    plugin_marketplace_root = output_dir / "chatgpt-desktop-local-plugin"
    targets = [
        *(output_dir / name for name in sorted(ALL_SETUP_BUNDLE_FILES)),
        plugin_marketplace_root,
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
                    "plugin_stdio_verified": False,
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

        # The generated plugin marketplace is a single authoritative tree for
        # the current server name.  Writing into the previous tree would leave
        # stale plugin.json/.mcp.json pairs behind when the operator renames the
        # server.  The tree is already part of the transaction backup above,
        # so remove it before generation and let rollback restore it verbatim
        # if any later setup write fails.
        if plugin_marketplace_root.is_dir():
            shutil.rmtree(plugin_marketplace_root)
        elif plugin_marketplace_root.exists():
            plugin_marketplace_root.unlink()

        return _write_mcp_setup_bundle_untransactional(
            config,
            output_dir,
            server_name=server_name,
            preferred_python=preferred_python,
            preferred_project_root=preferred_project_root,
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
        "process_started": False,
        "mcp_initialized": False,
        "tools_discovered": False,
        "plugin_install_command_succeeded": False,
        "plugin_manifest_validated": False,
        "plugin_discoverable": False,
        "plugin_loader_verified": False,
        "plugin_name_conflict_detected": False,
        "plugin_registered": False,
        "plugin_rollback_performed": False,
        "plugin_rollback_complete": None,
        "legacy_plugin_conflict_detected": False,
        "legacy_plugin_removed_for_direct_config": False,
        "legacy_plugin_restored_after_direct_failure": False,
        "legacy_plugin_marketplace_removed": False,
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
        "plugin_stdio_verified": False,
        "plugin_stdio_runtime_fingerprint": None,
        "desktop_process_detected": False,
        "desktop_process_started_at": None,
        "desktop_mcp_registration_updated_at": None,
        "desktop_plugin_registration_updated_at": None,
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
            "plugin_install_command_succeeded": (
                "The Codex plugin install command exited successfully; this alone is not registration proof."
            ),
            "plugin_manifest_validated": (
                "plugin.json, .mcp.json, and marketplace.json passed strict UTF-8-without-BOM JSON validation."
            ),
            "plugin_discoverable": (
                "An enabled selector with this bundle's exact cachebuster version and marketplace source was found in Codex plugin list JSON."
            ),
            "plugin_loader_verified": (
                "codex mcp get resolved this plugin's server with the current bundle launcher and data paths."
            ),
            "plugin_name_conflict_detected": (
                "A direct or unrelated MCP entry already used this server name after the target plugin was removed."
            ),
            "plugin_registered": (
                "True only after manifest validation, install command success, exact version/source discoverability, and MCP loader verification all succeed."
            ),
            "legacy_plugin_removed_for_direct_config": (
                "An exact generated plugin selector with the same MCP name was removed before installing the preferred direct config."
            ),
            "legacy_plugin_restored_after_direct_failure": (
                "The previous generated plugin was reinstalled because direct config installation failed and rolled back."
            ),
            "direct_config_registered": (
                "The direct MCP entry was written to the local MCP configuration used by ChatGPT Desktop."
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
                "True when a running ChatGPT Desktop process predates the latest MCP registration; false means "
                "not running or already current; null means not checked or unknown."
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
                "A ChatGPT Desktop tool scan exposed the expected MCP tools; direct stdio smoke does not set this."
            ),
            "conversation_attachment_verified": (
                "The registered MCP tools were observed in the current conversation."
            ),
            "conversation_attachment_unverified": (
                "Restart Desktop and verify the direct server with /mcp in a new conversation."
            ),
            "end_to_end_verified": (
                "ChatGPT Desktop exposed the tools and the current conversation successfully invoked them."
            ),
            "transport_end_to_end_verified": (
                "The generated launcher passed the direct MCP protocol chain; this does not prove Desktop exposure."
            ),
        },
        "profiles": {
            "chatgpt-desktop-local": {
                "transport": "stdio",
                "surface": "chatgpt_desktop_mcp_settings",
                "tool_profile": "chatgpt-data",
            },
            "chatgpt-remote": {"transport": "streamable-http", "surface": "remote_mcp_app"},
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
    except (OSError, json.JSONDecodeError):
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
                "plugin_stdio_verified",
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
            merged["plugin_stdio_runtime_fingerprint"] = None
            merged["fresh_codex_app_server_runtime_fingerprint"] = None
            merged["conversation_attachment_unverified"] = True
            merged["tool_scan_unverified"] = True
            merged["desktop_app_server_tool_count"] = 0
            merged["desktop_app_server_tool_names"] = []
            merged["desktop_app_server_server_info"] = None
            merged["desktop_app_server_error"] = "runtime_changed_revalidation_required"
            if bool(existing.get("plugin_registered")):
                merged["installation_state"] = "plugin_installed_loader_verified_runtime_changed"
            elif bool(existing.get("direct_config_registered")):
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
    value = re.sub(
        r"(\$env:PRMCPBUILDER_TUNNEL_DATA_DIR\s*=\s*)(?:\"[^\"]*\"|'[^']*'|\S+)",
        lambda match: match.group(1) + _powershell_single_quoted_json(bundle_data_dir),
        value,
    )
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
        stdio_server_args = _stdio_server_args_from_client_entry(server)
        if stdio_server_args is not None:
            transport = _arg_value(stdio_server_args, "--transport")
            if transport in {None, "stdio"}:
                server["command"] = "powershell.exe"
                server["args"] = _powershell_stdio_launcher_client_args(launcher, stdio_server_args)
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
    return leaf in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}


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


def _codex_plugin_manual_guide(
    claude_desktop_config: dict[str, Any],
    *,
    server_name: str,
    bundle_dir: str | Path,
) -> str:
    mcp_servers = claude_desktop_config.get("mcpServers")
    server = mcp_servers.get(server_name) if isinstance(mcp_servers, dict) else None
    if not isinstance(server, dict):
        server = {}
    command = str(server.get("command") or "powershell.exe")
    args = [str(arg) for arg in server.get("args", [])] if isinstance(server.get("args"), list) else []
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    resolved_bundle_dir = str(Path(bundle_dir).resolve())

    def portable_value(value: str) -> str:
        return value.replace(resolved_bundle_dir, "<BUNDLE_DIR>")

    portable_args = [portable_value(arg) for arg in args]
    lines = [
        "Codex MCP 수동 호환 입력값",
        "",
        "권장 방법: Codex에 연결하기.bat를 실행해 사용자 MCP 설정을 직접 등록·검증합니다.",
        "BAT를 사용할 수 없으면 아래 값을 ~/.codex/config.toml에 직접 반영합니다.",
        "연결 설정, 로컬 경로, 토큰, API 키 또는 tunnel ID를 대화 프롬프트에 붙여넣지 않습니다.",
        "CODEX_AGENT_CONNECT_PROMPT.md는 로컬 파일·터미널 권한이 있는 에이전트용 선택적 자동화 자료이며 필수 입력이 아닙니다.",
        "<BUNDLE_DIR>은 이 TXT가 들어 있는 압축 해제 폴더의 현재 절대경로로 바꿉니다.",
        "",
        f"MCP 이름: {server_name}",
        f"실행 명령: {command}",
        "작업 중인 디렉터리: <BUNDLE_DIR>",
        "",
        "인자 - 아래 항목을 위에서부터 하나씩 추가:",
    ]
    lines.extend(f"{index}. {arg}" for index, arg in enumerate(portable_args, start=1))
    if not portable_args:
        lines.append("없음")
    lines.extend(["", "환경 변수:"])
    if env:
        lines.extend(f"{key}={portable_value(str(value))}" for key, value in env.items())
    else:
        lines.append("비워 둠")
    lines.extend(
        [
            "",
            "저장 후 Codex 앱을 완전히 종료하고 다시 실행합니다.",
            f"새 task에서 /mcp를 입력해 {server_name} 이름이 보이는지 확인합니다.",
            f"확인 요청: {server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘.",
        ]
    )
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


def _chatgpt_desktop_local_config(
    claude_desktop_config: dict[str, Any],
    *,
    server_name: str,
    bundle_dir: str | Path,
) -> dict[str, Any]:
    mcp_servers = claude_desktop_config.get("mcpServers")
    server = mcp_servers.get(server_name) if isinstance(mcp_servers, dict) else None
    if not isinstance(server, dict):
        server = {}
    args = server.get("args")
    if not isinstance(args, list):
        args = []
    plugin_name = _normalized_plugin_name(server_name)
    return {
        "profile": "chatgpt-desktop-local",
        "client": "ChatGPT Desktop",
        "surface": "chatgpt_desktop_mcp_settings",
        "mode": "local_stdio",
        "tool_profile": "chatgpt-data",
        "verification_tools": ["search", "fetch"],
        "connection_configuration_method": "direct_config",
        "connection_prompt_required": False,
        "secret_input_policy": "environment_or_oauth_only",
        "chatgpt_direct_local_mcp_supported": True,
        "primary_registration": "chatgpt_desktop_settings_mcp_servers",
        "supported_runtime_note": (
            "Register the local STDIO server in ChatGPT Desktop Settings > MCP servers, "
            "restart Desktop, and verify it with /mcp and an actual tool call."
        ),
        "server_name": server_name,
        "plugin_name": plugin_name,
        "plugin_marketplace_root": "chatgpt-desktop-local-plugin",
        "plugin_manifest": f"chatgpt-desktop-local-plugin/plugins/{plugin_name}/.codex-plugin/plugin.json",
        "plugin_mcp_config": f"chatgpt-desktop-local-plugin/plugins/{plugin_name}/.mcp.json",
        "plugin_install_command_succeeded": False,
        "plugin_manifest_validated": False,
        "plugin_discoverable": False,
        "plugin_loader_verified": False,
        "plugin_name_conflict_detected": False,
        "plugin_registered": False,
        "plugin_rollback_performed": False,
        "plugin_rollback_complete": None,
        "legacy_plugin_conflict_detected": False,
        "legacy_plugin_removed_for_direct_config": False,
        "legacy_plugin_restored_after_direct_failure": False,
        "legacy_plugin_marketplace_removed": False,
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
        "plugin_stdio_verified": False,
        "plugin_stdio_runtime_fingerprint": None,
        "desktop_process_detected": False,
        "desktop_process_started_at": None,
        "desktop_mcp_registration_updated_at": None,
        "desktop_plugin_registration_updated_at": None,
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
        "ui_fields": {
            "name": server_name,
            "command": str(server.get("command") or "powershell.exe"),
            "args": [str(arg) for arg in args],
            "cwd": str(Path(bundle_dir).resolve()),
            "env": dict(server.get("env") or {}) if isinstance(server.get("env"), dict) else {},
            "env_passthrough": [],
        },
        "operator_steps": [
            "Open ChatGPT Desktop Settings > MCP servers > Add server.",
            "Copy the generated name, STDIO command, working directory, and arguments from CHATGPT_DESKTOP_CONNECT_GUIDE.md.",
            "Never paste this connection configuration, local paths, tokens, API keys, or tunnel IDs into a chat prompt.",
            "Save the server and select Restart in ChatGPT Desktop.",
            "If manual entry is impractical or you need the advanced shared config path, double-click the generated ChatGPT Desktop connection BAT as fallback. The BAT writes config.toml but cannot enable a Desktop feature that the installed build or workspace does not expose.",
            f"Run /mcp first and verify that {server_name} is connected.",
            f"Verification prompt: {server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘.",
        ],
        "status_semantics": {
            "plugin_install_command_succeeded": "The plugin install command returned success; this is not discoverability proof.",
            "plugin_manifest_validated": "All companion JSON files passed strict UTF-8-without-BOM validation.",
            "plugin_discoverable": "An enabled selector with the exact cachebuster version and marketplace source appeared in Codex plugin list JSON.",
            "plugin_loader_verified": "Codex mcp get resolved this plugin's MCP entry with the current launcher and data paths.",
            "plugin_name_conflict_detected": "A direct or unrelated MCP definition already used this name before the plugin was installed.",
            "plugin_registered": "Manifest validation, install command success, exact version/source discoverability, and Codex MCP loader verification all succeeded.",
            "legacy_plugin_removed_for_direct_config": "An exact generated plugin selector with the same MCP name was removed before installing the preferred direct config.",
            "legacy_plugin_restored_after_direct_failure": "The previous generated plugin was reinstalled because direct config installation failed and rolled back.",
            "desktop_restart_required": "True when a running ChatGPT Desktop process predates the latest MCP registration; false means not running or already current; null means not checked or unknown.",
            "desktop_restart_status": "One of not_checked, required, not_running, up_to_date, or unknown.",
            "desktop_app_server_loader_verified": "Compatibility alias for a fresh Codex app-server process inventory; it is not the running Desktop scan.",
            "fresh_codex_app_server_inventory_verified": "A separate Codex app-server process returned the required tools with recorded provenance.",
            "installed_config_transport_verified": "The exact installed config entry passed the direct MCP protocol contract.",
            "desktop_status_scan_request_observed": "A restarted Desktop routed mcpServerStatus/list without error; this does not prove tool exposure.",
            "direct_stdio_verified": "Direct initialize, tools/list, search, and fetch succeeded over stdio.",
            "desktop_tool_scan_verified": "ChatGPT Desktop exposed the expected tools after its own tool scan.",
            "conversation_attachment_verified": "The registered MCP tools were observed in the current conversation.",
            "conversation_attachment_unverified": "A restarted Desktop and new conversation must still confirm the registered MCP with /mcp and an actual tool call.",
            "transport_end_to_end_verified": "The generated launcher passed the direct MCP protocol chain; this does not prove Desktop exposure.",
            "end_to_end_verified": "ChatGPT Desktop exposed the tools and the current conversation successfully invoked them.",
        },
        "troubleshooting": [
            "입력창의 + 버튼 선택",
            "더 보기 선택",
            f"{server_name} 선택",
        ],
    }


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


def _write_chatgpt_desktop_local_plugin(
    local_stdio_config: dict[str, Any],
    *,
    output_dir: Path,
    server_name: str,
) -> dict[str, str]:
    """Write a portable local plugin marketplace for unified ChatGPT Desktop/Codex discovery."""
    plugin_name = _normalized_plugin_name(server_name)
    marketplace_name = _chatgpt_local_marketplace_name(server_name)
    marketplace_root = output_dir / "chatgpt-desktop-local-plugin"
    plugin_root = marketplace_root / "plugins" / plugin_name
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    mcp_path = plugin_root / ".mcp.json"
    marketplace_path = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)

    mcp_servers = local_stdio_config.get("mcpServers") if isinstance(local_stdio_config, dict) else None
    if not isinstance(mcp_servers, dict) or not isinstance(mcp_servers.get(server_name), dict):
        raise ValueError(f"Local stdio config does not contain MCP server {server_name}.")

    # Codex plugin .mcp.json uses the camelCase ``mcpServers`` shape accepted
    # by the current plugin validator, bundled plugins, and app-server loader.
    # Codex's standalone config.toml uses the separate ``mcp_servers`` table.
    plugin_mcp_config = {"mcpServers": {server_name: mcp_servers[server_name]}}
    cachebuster_source = {
        "template_revision": CHATGPT_DESKTOP_PLUGIN_TEMPLATE_REVISION,
        "plugin_name": plugin_name,
        "server_name": server_name,
        "mcp_config": plugin_mcp_config,
    }
    cachebuster = hashlib.sha256(
        json.dumps(cachebuster_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    plugin_manifest = {
        "name": plugin_name,
        "version": f"0.1.0+codex.{cachebuster}",
        "description": "Privacy-reduced Korean public-institution regulation search and fetch tools over local MCP.",
        "author": {"name": "Public Regulation MCP Builder contributors"},
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": server_name,
            "shortDescription": "Search approved local regulation data.",
            "longDescription": (
                "Runs the approved regulation MCP locally through stdio and exposes only read-only search and fetch."
            ),
            "developerName": "Public Regulation MCP Builder contributors",
            "category": "Productivity",
            "capabilities": ["Read"],
            "defaultPrompt": [
                f"{server_name} MCP의 search 도구로 인사규정을 찾아줘.",
                f"search 결과의 첫 번째 id를 {server_name} MCP의 fetch 도구로 조회해 원문과 출처를 보여줘.",
            ],
        },
    }
    marketplace = {
        "name": marketplace_name,
        "interface": {"displayName": f"{server_name} Local"},
        "plugins": [
            {
                "name": plugin_name,
                "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }
    _write_json_utf8_no_bom(manifest_path, plugin_manifest)
    _write_json_utf8_no_bom(mcp_path, plugin_mcp_config)
    _write_json_utf8_no_bom(marketplace_path, marketplace)
    return {
        "chatgpt_desktop_plugin_manifest": str(manifest_path),
        "chatgpt_desktop_plugin_mcp": str(mcp_path),
        "chatgpt_desktop_plugin_marketplace": str(marketplace_path),
    }


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
) -> dict[str, Any]:
    """Write approved MCP-visible runtime data under ``out_dir/data``.

    The generated setup JSON is not enough for a working local MCP handoff. The
    MCP server also needs the approved vector records, the repository manifest,
    approved chunks, and the approval journal used by the visibility gate.
    """
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
    if normalized_scope in {"selected_documents", "selected_institution", "institution_profile"} and not str(profile_id or "").strip():
        raise ValueError("Institution-scoped MCP bundles require profile_id.")
    if not str(document_id or "").strip() and not requested_document_ids and not str(profile_id or "").strip():
        raise ValueError("MCP runtime export requires document_id or profile_id; tenant-wide export is not allowed.")
    resolved_scope = normalized_scope or (
        "document" if document_id else "selected_documents" if requested_document_ids else "institution_profile"
    )

    output_dir = Path(out_dir)
    final_runtime_data_dir = output_dir / "data"
    runtime_data_dir = _runtime_data_dir or final_runtime_data_dir
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
        missing_document_ids = sorted(requested_document_id_set - visible_document_ids)
        if missing_document_ids:
            raise ValueError(
                "Selected regulations are not all MCP-visible. Approve and index these document IDs first: "
                + ", ".join(missing_document_ids)
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

    document_ids = sorted(
        {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
        }
    )
    source_repository = JsonRepository(source_settings)
    source_metadata_summary = _runtime_source_metadata_summary(records, source_repository, document_ids)
    if require_source_metadata:
        _require_runtime_source_metadata(source_metadata_summary)
    if require_kordoc_table_parser:
        kordoc_table_parser_summary = _require_kordoc_table_parser_evidence(source_repository, document_ids)
    else:
        kordoc_table_parser_summary = _kordoc_table_parser_evidence_summary(source_repository, document_ids)
    _report_runtime_progress(progress_callback, 12, "출처·표 파서 증빙 확인", len(document_ids), len(document_ids))
    _prepare_runtime_data_export_dir(runtime_data_dir, source_settings.data_dir)
    runtime_repository_dir = runtime_data_dir / "repository"
    runtime_repository_dir.mkdir(parents=True, exist_ok=True)
    runtime_vector_dir = runtime_data_dir / "vector_db" / tenant_storage_key(tenant_id)
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

    manifest = _empty_runtime_repository_manifest()
    total_chunks = 0
    approval_records: list[dict[str, Any]] = []
    indexing_jobs: list[dict[str, Any]] = []
    exported_result_files: list[str] = []
    document_total = len(document_ids)
    for document_index, current_document_id in enumerate(document_ids, start=1):
        document = source_repository.get_document(current_document_id)
        if document is None:
            continue
        manifest["documents"][current_document_id] = document.model_dump(mode="json")

        visible_chunk_ids = {
            str(record.get("chunk_id") or (record.get("metadata") or {}).get("chunk_id") or "")
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "") == current_document_id
        }
        records_by_chunk_id = {
            str(record.get("chunk_id") or (record.get("metadata") or {}).get("chunk_id") or ""): record
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "") == current_document_id
        }
        chunks = _current_approved_chunks_for_runtime_export(
            repository=source_repository,
            document_id=current_document_id,
            visible_chunk_ids=visible_chunk_ids,
            records_by_chunk_id=records_by_chunk_id,
        )
        total_chunks += len(chunks)
        _write_runtime_result_json(
            runtime_repository_dir,
            current_document_id,
            "chunks",
            [chunk.model_dump(mode="json") for chunk in chunks],
            exported_result_files,
        )

        approval_records.extend(source_repository.list_approval_journal_records(current_document_id))
        indexing_jobs.extend(source_repository.list_indexing_jobs(current_document_id))
        _report_runtime_progress(
            progress_callback,
            80 + int((document_index / max(document_total, 1)) * 14),
            "문서별 승인 이력 묶기",
            document_index,
            document_total,
        )

    for index, record in enumerate(approval_records, start=1):
        key = str(record.get("approval_record_id") or record.get("approval_id") or f"approval_{index}")
        manifest["approvals"][key] = record
    for index, record in enumerate(indexing_jobs, start=1):
        key = str(record.get("indexing_job_id") or f"indexing_job_{index}")
        manifest["indexing_jobs"][key] = record

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
    _report_runtime_progress(progress_callback, 97, "런타임 manifest 생성", len(records), len(records))

    runtime_manifest = {
        "report_type": "mcp_runtime_data_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "scope": resolved_scope,
        "synthetic_runtime": False,
        "provenance": "approved_runtime_bundle_export",
        "tenant_storage_isolation": bool(getattr(source_settings, "tenant_storage_isolation", False)),
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
        "recommended_smoke_query": _recommended_runtime_smoke_query(records),
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
    runtime_manifest_path.write_text(json.dumps(runtime_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if _write_status:
        _write_bundle_status(output_dir, runtime_manifest=runtime_manifest)
    return runtime_manifest


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
    _assert_no_active_bundle_installation(output_dir)
    runtime_data_dir = output_dir / "data"
    if runtime_data_dir.resolve() == Path(source_data_dir).resolve():
        raise ValueError("Runtime bundle output data dir must not be the same as the source data dir.")
    staging_dir = output_dir / f".data-stage-{uuid4().hex}"
    backup_dir = output_dir / f".data-backup-{uuid4().hex}"
    status_path = output_dir / SETUP_BUNDLE_FILES["bundle_status"]
    prior_status_exists = status_path.is_file()
    prior_status_bytes = status_path.read_bytes() if prior_status_exists else None
    stale_report_snapshots = {
        output_dir / filename: (output_dir / filename).read_bytes()
        for filename in STALE_BUNDLE_STATUS_REPORT_FILENAMES
        if (output_dir / filename).is_file()
    }
    manifest: dict[str, Any] | None = None
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
        )
        staged_manifest = _read_runtime_manifest(staging_dir)
        if not staged_manifest or staged_manifest != manifest:
            raise RuntimeError("Staged MCP runtime manifest did not pass commit validation.")

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
                    "plugin_stdio_verified": False,
                    "plugin_stdio_runtime_fingerprint": None,
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
        os.replace(staging_dir, runtime_data_dir)
        data_swapped = True
        _clear_stale_bundle_status_reports(output_dir)
        _write_bundle_status(output_dir, runtime_manifest=manifest)
        transaction_complete = True
    except BaseException:
        if data_swapped and runtime_data_dir.exists():
            if runtime_data_dir.is_dir():
                shutil.rmtree(runtime_data_dir)
            else:
                runtime_data_dir.unlink()
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
        raise
    finally:
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir, ignore_errors=True)
        elif staging_dir.exists():
            staging_dir.unlink(missing_ok=True)
        if transaction_complete:
            if backup_dir.is_dir():
                shutil.rmtree(backup_dir, ignore_errors=True)
            elif backup_dir.exists():
                backup_dir.unlink(missing_ok=True)

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
    entries = []
    for record in sorted(records, key=lambda item: (str(item.get("document_id") or ""), str(item.get("chunk_id") or ""))):
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
        chunks.append(chunk)
    if invalid:
        sample = ", ".join(invalid[:5])
        raise ValueError(
            "MCP runtime export is stale: current repository chunks no longer match approved vector records. "
            f"{sample}. Reapprove and reindex before creating a handoff bundle."
        )
    return chunks


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
    plugin_root = source_dir / "chatgpt-desktop-local-plugin"
    if plugin_root.is_dir():
        plugin_manifests = sorted((plugin_root / "plugins").glob("*/.codex-plugin/plugin.json"))
        plugin_mcp_configs = sorted((plugin_root / "plugins").glob("*/.mcp.json"))
        if len(plugin_manifests) != 1 or len(plugin_mcp_configs) != 1:
            raise ValueError(
                "Generated ChatGPT Desktop plugin bundle must contain exactly one plugin.json and one .mcp.json."
            )
        plugin_json_paths = [
            *plugin_manifests,
            *plugin_mcp_configs,
            plugin_root / ".agents" / "plugins" / "marketplace.json",
        ]
        for path in plugin_json_paths:
            if not path.is_file():
                raise FileNotFoundError(f"Generated ChatGPT Desktop plugin companion file is missing: {path}")
            _load_strict_utf8_json_for_bundle(path)
            archive_files.append((path, path.relative_to(source_dir).as_posix()))
    runtime_data_dir = source_dir / "data"
    include_empty_runtime_directory = (
        not runtime_data_dir.is_dir()
        or not any(runtime_data_dir.iterdir())
    )
    if runtime_data_dir.is_dir():
        _validate_runtime_data_bundle_consistency(runtime_data_dir)
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
    "claude_api_fragment.json",
    "claude_desktop_config.json",
    "mcp_config.bundle.json",
    "data/mcp_runtime_manifest.json",
}


def _portable_handoff_payload(path: Path, *, arcname: str, source_dir: Path) -> bytes | None:
    """Remove build-host bundle paths from handoff configuration templates."""

    normalized_arcname = arcname.replace("\\", "/")
    is_plugin_mcp = normalized_arcname.startswith("chatgpt-desktop-local-plugin/") and normalized_arcname.endswith(
        "/.mcp.json"
    )
    if normalized_arcname in PORTABLE_HANDOFF_JSON_FILES or is_plugin_mcp:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        portable = _replace_bundle_path_with_placeholder(payload, source_dir=source_dir)
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
        raise ValueError(f"Generated plugin companion JSON must be UTF-8 without BOM: {path}")
    try:
        return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_bundle_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Generated plugin companion file must contain strict UTF-8 JSON: {path}: {exc}") from exc


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


def _runtime_data_files_requiring_manifest(runtime_data_dir: Path) -> list[Path]:
    files: list[Path] = []
    repository_dir = runtime_data_dir / "repository"
    if repository_dir.is_dir():
        for path in sorted(repository_dir.glob("*.json")):
            if path.name in {"manifest.json", "approval_snapshot.json"} or any(
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
    expected_storage_key = tenant_storage_key(tenant_id)
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
    if path.name.startswith(".") or path.name in RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES:
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
    claude_api_ready = bool((config.get("claude_api") or {}).get("ready"))
    connections = [
        {
            "client": "Claude Desktop",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["connect_claude_desktop_bat"],
            "config_file": SETUP_BUNDLE_FILES["claude_desktop"],
            "operator_action": "Double-click the Claude Desktop connection button.",
        },
        {
            "client": "Claude Code",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["claude_code_agent_prompt"],
            "fallback_file": SETUP_BUNDLE_FILES["connect_claude_code_bat"],
            "config_file": SETUP_BUNDLE_FILES["claude_code_stdio"],
            "operator_action": "Paste the agent request into Claude Code; use the BAT only as fallback.",
        },
        {
            "client": "ChatGPT Desktop",
            "profile": "chatgpt-desktop-local",
            "tool_profile": "chatgpt-data",
            "mode": "local_stdio",
            "ready": True,
            "registration_required": True,
            "registration_verified": False,
            "primary_file": SETUP_BUNDLE_FILES["chatgpt_desktop_agent_prompt"],
            "fallback_file": SETUP_BUNDLE_FILES["connect_chatgpt_desktop_bat"],
            "config_file": SETUP_BUNDLE_FILES["chatgpt_desktop_local"],
            "plugin_marketplace_root": "chatgpt-desktop-local-plugin",
            "operator_action": (
                "Open ChatGPT Desktop Settings > MCP servers > Add server, enter the generated STDIO fields, save and restart, then verify /mcp and an actual tool call. Use the Desktop BAT only as fallback."
            ),
        },
        {
            "client": "Codex CLI",
            "profile": "codex-compatibility",
            "tool_profile": "chatgpt-data",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["connect_codex_bat"],
            "fallback_file": SETUP_BUNDLE_FILES["codex_config"],
            "optional_agent_prompt": SETUP_BUNDLE_FILES["codex_agent_prompt"],
            "config_file": SETUP_BUNDLE_FILES["codex_config"],
            "operator_action": (
                "Run the Codex BAT or apply the generated config directly. "
                "Do not paste connection configuration or secrets into a chat prompt."
            ),
        },
        {
            "client": "ChatGPT 원격 MCP",
            "profile": "chatgpt-remote",
            "mode": "https_connector",
            "ready": False,
            "configuration_ready": chatgpt_ready,
            "remote_endpoint_verified": False,
            "tool_scan_unverified": True,
            "primary_file": SETUP_BUNDLE_FILES["connect_chatgpt_https_bat"],
            "config_file": SETUP_BUNDLE_FILES["chatgpt"],
            "server_file": SETUP_BUNDLE_FILES["run_chatgpt"],
            "operator_action": "Double-click the ChatGPT HTTPS connection button, then register connector_url in ChatGPT.",
        },
        {
            "client": "ChatGPT 웹",
            "mode": "secure_mcp_tunnel",
            "ready": "manual_setup_required",
            "primary_file": SETUP_BUNDLE_FILES["connect_chatgpt_tunnel_bat"],
            "config_file": SETUP_BUNDLE_FILES["openai_tunnel"],
            "operator_action": "Set approved tunnel credentials once, then double-click the ChatGPT Tunnel connection button.",
        },
        {
            "client": "Claude (HTTPS MCP)",
            "mode": "https_mcp_connector",
            "ready": claude_api_ready,
            "primary_file": SETUP_BUNDLE_FILES["connect_claude_https_bat"],
            "config_file": SETUP_BUNDLE_FILES["claude_api"],
            "server_file": SETUP_BUNDLE_FILES["run_http"],
            "operator_action": "Double-click the Claude HTTPS button; register only the URL in Claude app Connectors, or use the generated fragment in a Messages API request.",
        },
    ]
    connection_order = {
        client: index
        for index, client in enumerate(
            (
                "Claude Code",
                "Codex CLI",
                "Claude Desktop",
                "ChatGPT Desktop",
                "ChatGPT 원격 MCP",
                "ChatGPT 웹",
                "Claude (HTTPS MCP)",
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


def _chatgpt_desktop_setup_guide(
    server_name: str,
    *,
    config: dict[str, Any],
    bundle_dir: str | Path,
    allow_bat_fallback: bool = True,
) -> str:
    ui_fields = config.get("ui_fields") if isinstance(config, dict) else None
    if not isinstance(ui_fields, dict):
        ui_fields = {}
    resolved_bundle_dir = str(Path(bundle_dir).resolve())

    def portable(value: object) -> str:
        return str(value).replace(resolved_bundle_dir, AGENT_CONNECT_BUNDLE_DIR_MARKER)

    command = portable(ui_fields.get("command") or "powershell.exe")
    cwd = portable(ui_fields.get("cwd") or resolved_bundle_dir)
    raw_args = ui_fields.get("args")
    args = [portable(arg) for arg in raw_args] if isinstance(raw_args, list) else []
    env = ui_fields.get("env") if isinstance(ui_fields.get("env"), dict) else {}
    argument_lines = "\n".join(f"{index}. {arg}" for index, arg in enumerate(args, start=1)) or "없음"
    environment_note = "비워 둠" if not env else "비밀값을 복사하지 말고 승인된 로컬 환경변수만 설정"
    fallback_note = (
        f"`@{server_name}` 반복 입력은 설치나 연결 확인을 대신하지 않는다. "
        "수동 입력이 어렵거나 고급 설정 파일 경로를 사용할 때만 이 번들 폴더의 "
        "`ChatGPT Desktop에 연결하기.bat`를 Windows 탐색기에서 실행한다. 이 BAT는 ChatGPT Desktop이 "
        "사용하는 사용자 `~/.codex/config.toml`에 같은 STDIO 항목을 백업·기록하고 검증하는 "
        "보조 수단이지, Desktop에 없는 MCP 기능이나 메뉴를 새로 활성화하는 설치 프로그램은 아니다. "
        "메뉴가 보이지 않으면 먼저 ChatGPT Desktop을 최신 버전으로 갱신하고 현재 계정·워크스페이스에서 "
        "MCP가 제공되는지 확인한다. BAT 실행 후에도 ChatGPT Desktop을 완전히 재시작하고 새 대화에서 `/mcp`와 "
        "실제 `search`와 `fetch` 호출을 확인해야 하며, 둘 다 노출되지 않으면 연결 완료로 판단하지 말고 "
        "원격 HTTPS MCP 또는 Secure MCP Tunnel을 사용한다."
        if allow_bat_fallback
        else f"`@{server_name}` 반복 입력은 설치나 연결 확인을 대신하지 않는다. 이 안내는 구형 연결 프롬프트에서 안전하게 변환됐으므로 구형 BAT를 실행하지 말고 위 Settings 입력값만 사용한다."
    )
    portable_source_note = (
        "아래 경로에 `PROGRAM_BUNDLE_DIR` 자리표시자가 그대로 보이면 ZIP 이식용 원본 파일이다. "
        "그 값을 ChatGPT Desktop에 복사하지 말고 프로그램 생성 결과 화면에서 실제 절대경로가 "
        "채워진 코드 상자를 사용하거나, 마지막의 보조 BAT를 실행한다."
        if allow_bat_fallback
        else "아래 입력값은 구형 프롬프트에서 안전하게 복구해 현재 번들 절대경로로 다시 계산했다. "
        "구형 BAT는 실행하지 말고 이 Settings 입력값만 사용한다."
    )
    return f"""# ChatGPT Desktop MCP 연결 안내

이 안내는 **ChatGPT Desktop 전용**이다. 다른 제품의 에이전트 실행 요청이 아니며, 일반 대화창에 설치 프롬프트로 붙여넣지 않는다.
연결 설정·로컬 경로·토큰·API 키·tunnel ID는 대화 프롬프트에 넣지 않고 Settings 또는 승인된 환경변수에만 입력한다.

{portable_source_note}

{_agent_connect_bundle_context(prompt_file="CHATGPT_DESKTOP_CONNECT_GUIDE.md", fallback_file="ChatGPT Desktop에 연결하기.bat")}

## ChatGPT Desktop에서 등록

1. ChatGPT Desktop의 `Settings > MCP servers > Add server`를 연다.
2. 새 STDIO 서버 입력 화면이 표시되는지 확인한다.
3. 아래 값을 그대로 입력한다.

```text
Name: {server_name}
Type: STDIO
Command: {command}
Working directory: {cwd}
Environment: {environment_note}
```

Arguments — 아래 항목을 표시된 순서대로 하나씩 추가한다.

```text
{argument_lines}
```

4. `Save`를 선택한 뒤 ChatGPT Desktop의 `Restart`를 실행한다.
5. 재시작 후 새 대화에서 `/mcp`를 입력해 `{server_name}`이 보이는지 확인한다.
6. `{server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘.`라고 입력해 실제 도구 호출을 확인한다.

{fallback_note}
"""


def _codex_agent_connect_prompt(server_name: str) -> str:
    return f"""# Codex MCP 선택적 로컬 자동화 요청

기본 연결 방법은 `Codex에 연결하기.bat` 실행 또는 `codex_config_snippet.toml`의 직접 설정이다. 이 요청문은 로컬 파일·터미널 권한이 있는 Codex 에이전트에서만 쓰는 선택적 자동화 자료이며 연결에 필수적이지 않다. 원격·일반 채팅에 붙여넣지 말고 토큰, API 키, tunnel ID 또는 별도 비밀값을 추가하지 않는다.

압축을 푼 연결 번들 폴더를 로컬 작업공간으로 연 경우에만 아래 작업을 수행해줘.

{_agent_connect_bundle_context(prompt_file="CODEX_AGENT_CONNECT_PROMPT.md", fallback_file="Codex에 연결하기.bat")}

1. 위 경로가 실제 절대경로로 채워져 있으면 `$BundleDir = {AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER}`를 실행하고, 필수 파일이 그 폴더 바로 아래에 있는지 확인한 뒤 `Set-Location -LiteralPath $BundleDir`을 실행해. 경로가 없거나 접근할 수 없으면 임의 경로로 설치하지 말고 그 정확한 폴더를 작업공간으로 열거나 추가해 달라고 요청해. 위 값이 여전히 `PROGRAM_BUNDLE_DIR` 자리표시자인 원본 파일을 직접 붙여넣은 경우에만 현재 작업공간에서 `CODEX_AGENT_CONNECT_PROMPT.md`를 정확히 하나 찾아 그 부모 폴더를 사용해. 검색 결과가 0개 또는 여러 개면 중단해.
2. `manifest.json`, `bundle_status.json`, `connect_mcp_client.ps1`을 읽고 서버 이름이 `{server_name}`인지 확인해.
3. 비밀값을 출력하거나 설정 파일에 저장하지 말고 `powershell -NoProfile -ExecutionPolicy Bypass -File .\\connect_mcp_client.ps1 -InstallPackage -Target codex -InstallCodex`를 한 번 실행해. 이 단일 프로세스 안에서 번들 wheel 설치, active Python Scripts 경로 보정, doctor, 현재 사용자의 Codex MCP 설정 백업·갱신, stdio 및 app-server 검증까지 모두 끝내야 해.
4. 위 명령이 0이 아닌 종료 코드로 끝나거나 doctor·등록·로더 검증 중 하나라도 실패하면 성공으로 보고하지 마.
5. 설치 후 `codex mcp get {server_name} --json`을 실행하고 `powershell.exe`, `-File`, `--data-dir`가 이 번들의 현재 절대 경로를 가리키는지 확인해. 이름이 같아도 다른 경로를 가리키면 성공으로 보고하지 마.
6. `bundle_status.json`의 `client_connections.codex`에서 `last_attempt.state=completed`, `effective.state=configured`, registration·loader·transport·fresh_app_server stage가 모두 같은 현재 attempt에서 verified인지 확인해. 호환용 최상위 direct 필드나 다른 클라이언트의 성공 상태를 Codex 성공으로 대신 사용하지 마.
7. 설치와 로더 검증이 모두 끝난 뒤 Codex를 완전히 종료하고 다시 실행해야 한다고 알려줘. 재시작한 새 task에서 `/mcp`로 `{server_name}`을 확인한 뒤 정확히 `{server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘.`라고 입력해 실제 도구 호출까지 확인하도록 안내해.

현재 화면에서 로컬 파일 또는 터미널 실행 권한이 없다면 성공했다고 말하지 말고, `manifest.json`의 `files.connect_codex_bat`가 가리키는 BAT를 사용자가 실행하도록 안내해.
"""


def _claude_code_agent_connect_prompt(server_name: str) -> str:
    return f"""# Claude Code 에이전트 MCP 연결 요청

압축을 푼 연결 번들 폴더를 로컬 작업공간으로 연 뒤 아래 작업을 수행해줘.

{_agent_connect_bundle_context(prompt_file="CLAUDE_CODE_AGENT_CONNECT_PROMPT.md", fallback_file="Claude Code에 연결하기.bat")}

1. 위 경로가 실제 절대경로로 채워져 있으면 `$BundleDir = {AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER}`를 실행하고, 필수 파일이 그 폴더 바로 아래에 있는지 확인한 뒤 `Set-Location -LiteralPath $BundleDir`을 실행해. 경로가 없거나 접근할 수 없으면 임의 경로로 설치하지 말고 그 정확한 폴더를 작업공간으로 열거나 추가해 달라고 요청해. 위 값이 여전히 `PROGRAM_BUNDLE_DIR` 자리표시자인 원본 파일을 직접 붙여넣은 경우에만 현재 작업공간에서 `CLAUDE_CODE_AGENT_CONNECT_PROMPT.md`를 정확히 하나 찾아 그 부모 폴더를 사용해. 검색 결과가 0개 또는 여러 개면 중단해.
2. `manifest.json`, `bundle_status.json`, `connect_mcp_client.ps1`을 읽고 서버 이름이 `{server_name}`인지 확인해.
3. `~/.claude/settings.json`의 `enabledMcpjsonServers`에 이름만 추가하거나 사용자 홈의 `~/.mcp.json`을 user scope 저장소처럼 직접 편집하지 마. 프로젝트 루트 `.mcp.json`은 project scope이고, 공식 user scope 저장소는 `~/.claude.json`이다. 이 요청은 저장 파일을 추측해 편집하지 않고 공식 CLI의 user scope를 사용해야 해.
4. 비밀값을 출력하거나 설정 파일에 저장하지 말고 `powershell -NoProfile -ExecutionPolicy Bypass -File .\\connect_mcp_client.ps1 -InstallPackage -Target claude-code`를 한 번 실행해. 이 단일 프로세스 안에서 번들 wheel 설치, active Python Scripts 경로 보정, doctor, `claude mcp add --transport stdio --scope user`, 등록 readback, 실제 stdio protocol smoke를 끝내야 해.
5. 위 명령이 0이 아닌 종료 코드로 끝나거나 doctor·user scope 등록·`Status: Connected`·실제 stdio 검증 중 하나라도 실패하면 성공으로 보고하지 마.
6. 설치 후 `claude mcp get {server_name}`을 실행하고 Scope가 User이고 Status가 Connected이며 `powershell.exe`, `-File`, `--data-dir`가 이 번들의 현재 절대 경로를 가리키는지 확인해. 이름이 같아도 다른 scope나 경로이면 성공으로 보고하지 마.
7. `bundle_status.json`의 `client_connections.claude-code`에서 `last_attempt.state=completed`, `effective.state=configured`, registration·loader·transport stage가 모두 현재 attempt에서 verified인지 확인해. 다른 클라이언트의 성공 필드를 Claude Code 성공으로 대신 사용하지 마.
8. 설치와 로더 검증이 모두 끝난 뒤 Claude Code를 완전히 종료하고 다시 실행해야 한다고 알려줘. 재시작한 새 대화에서 `/mcp`로 `{server_name}`을 확인한 뒤 정확히 `{server_name} MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘.`라고 입력해 실제 도구 호출까지 확인하도록 안내해. 이 실제 새 대화 호출 전에는 연결 완료가 아니라 설정 완료로만 보고해.

현재 화면에서 로컬 파일 또는 터미널 실행 권한이 없다면 성공했다고 말하지 말고, `manifest.json`의 `files.connect_claude_code_bat`가 가리키는 BAT를 사용자가 실행하도록 안내해.
"""


def _mcp_first_use_guide(server_name: str) -> str:
    return f"""PR MCP Builder 설치 후 사용 안내

등록된 MCP 이름: {server_name}

핵심 사용 순서
1. 아래 대상별 목록에서 사용할 프로그램 하나를 선택합니다.
2. Codex CLI는 `Codex에 연결하기.bat` 또는 직접 설정을 사용하고, Claude Code만 로컬 에이전트 요청문을 사용할 수 있습니다.
3. Claude Desktop은 전용 BAT를 실행하고, ChatGPT Desktop은 GUIDE 값을 Settings > MCP servers > Add server에 직접 등록합니다.
4. 원격 대상은 승인된 HTTPS 주소 또는 Secure MCP Tunnel을 먼저 준비합니다.
5. 로컬 클라이언트는 등록 후 완전히 종료·재실행하고, 지원하는 대상에서는 /mcp로 이름을 확인합니다. 원격 ChatGPT/Claude 연결은 새 대화에서 앱 또는 Connector를 첨부합니다. `@` 멘션은 연결 확인 수단이 아닙니다.
6. ChatGPT Desktop·Codex·원격 ChatGPT는 `search`와 `fetch`로 확인합니다. Claude 로컬 운영자 프로필은 `get_index_status`도 사용할 수 있습니다.
7. 연결 설정·로컬 경로·토큰·API 키·tunnel ID를 일반 대화 프롬프트에 붙여넣지 않습니다. 비밀값은 승인된 환경변수 또는 OAuth에만 둡니다.

대상별 연결 안내
- Claude Code: CLAUDE_CODE_AGENT_CONNECT_PROMPT.md
- Codex CLI: Codex에 연결하기.bat 또는 codex_config_snippet.toml 직접 설정
- Claude Desktop: Claude Desktop에 연결하기.bat
- ChatGPT Desktop: CHATGPT_DESKTOP_CONNECT_GUIDE.md
- ChatGPT 원격 MCP: ChatGPT HTTPS에 연결하기.bat
- ChatGPT 웹: ChatGPT 보안 Tunnel에 연결하기.bat
- Claude (HTTPS MCP): Claude HTTPS에 연결하기.bat

Claude Desktop·Claude Code 로컬 full 프로필의 설치 후 도구 확인
{server_name} MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘.

도구를 명시해서 확인하려면 아래 문장을 입력합니다.

{server_name} MCP의 list_regulations 도구를 사용해서 등록된 규정 목록을 보여줘.

ChatGPT Desktop·Codex·원격 ChatGPT/보안 Tunnel의 chatgpt-data 프로필 확인
{server_name} MCP의 search 도구로 인사규정을 찾고, 반환된 첫 번째 id를 fetch 도구로 조회해 조문 원문과 출처를 보여줘.

ChatGPT Desktop 로컬 direct MCP
- 기본: CHATGPT_DESKTOP_CONNECT_GUIDE.md의 Name, STDIO, Command, Working directory, Arguments를 Settings > MCP servers > Add server에 입력
- 보조 설치: 수동 입력이 어렵거나 고급 공유 설정 파일 경로가 필요할 때만 ChatGPT Desktop에 연결하기.bat. BAT는 Desktop 제품 기능을 활성화하지 않음
- 확인: Save 후 Restart하고 새 대화에서 /mcp 및 실제 search/fetch 호출
- 주의: @{server_name} 반복 입력은 연결 확인이나 설치를 대신하지 않음

Codex CLI 호환
- 권장 설치: Codex에 연결하기.bat
- 직접 설정: codex_config_snippet.toml을 ~/.codex/config.toml에 반영
- 선택적 자동화: 로컬 파일·터미널 권한이 있는 경우에만 CODEX_AGENT_CONNECT_PROMPT.md 사용
- 보안: 연결 설정과 비밀값을 프롬프트에 넣지 않음
- 확인: 새 task에서 /mcp와 실제 search/fetch 호출
- 터미널 확인: codex mcp list

Claude Desktop
- 기본 설치: Claude Desktop에 연결하기.bat
- 확인: BAT의 설치 검증이 끝난 뒤 앱을 완전히 종료하고 다시 실행해 새 대화에서 MCP 이름을 포함해 요청
- 주의: Claude Desktop에는 위 에이전트 프롬프트 및 /mcp 공통 절차를 적용하지 않음

Claude Code
- 권장: CLAUDE_CODE_AGENT_CONNECT_PROMPT.md를 Claude Code 에이전트에 붙여넣어 user scope 등록과 `claude mcp get` 검증을 맡김
- 설치: Claude Code에 연결하기.bat
- 확인: 대화에서 /mcp
- 터미널 확인: claude mcp list

ChatGPT 원격 HTTPS custom app
- ChatGPT 대화는 localhost MCP에 직접 연결하지 않습니다.
- ChatGPT HTTPS BAT로 승인된 공개 HTTPS MCP를 준비합니다.
- ChatGPT 웹의 Settings > Security and login에서 Developer mode를 켠 뒤 Settings > Plugins 또는 https://chatgpt.com/plugins 의 +에서 앱 이름을 {server_name}으로 등록합니다.
- 새 대화의 tools 메뉴에서 {server_name} 앱을 선택한 뒤 실제 search/fetch 요청으로 확인합니다.

ChatGPT 웹 Secure MCP Tunnel
- ChatGPT 보안 Tunnel BAT로 승인된 tunnel_id와 로컬 MCP 실행을 준비합니다.
- Secure MCP Tunnel 전용 가이드에 따라 Settings > Security and login에서 Developer mode를 켜고 Settings > Plugins 또는 https://chatgpt.com/plugins 에서 +를 누릅니다.
- 앱의 Connection을 Tunnel로 선택해 tunnel_id를 지정하고, 새 대화의 + > More에서 {server_name} 앱을 선택한 뒤 실제 search/fetch 요청으로 확인합니다.
- 이 Plugins 화면의 개발자 모드 tunnel 앱 생성과 Work mode marketplace 플러그인 설치는 목적이 다릅니다. ChatGPT 웹이 로컬 config.toml이나 로컬 stdio 플러그인을 읽는다고 안내하지 않습니다.

Claude (HTTPS MCP)
- Claude 앱의 custom connector는 HTTPS MCP URL을 Customize > Connectors에 등록하고 대화의 + > Connectors에서 활성화합니다. Team/Enterprise 조직 등록은 Owner가 Organization settings > Connectors에서 수행합니다.
- Claude Messages API는 claude_api_fragment.json의 mcp_servers, tools, betas를 API 요청에 사용합니다. 이 JSON 조각을 Claude 앱의 Connectors 화면에 붙여 넣지 않습니다.

실제 규정 조회 예시
{server_name} MCP에서 인사규정을 찾고 관련 조문 원문과 출처를 보여줘. search 결과는 fetch로 확인해.

같은 MCP 업데이트
- 같은 이름으로 다시 생성하면 ChatGPT Desktop은 새 안내 값을 기존 Settings > MCP servers 항목에 반영하고, Codex CLI는 BAT 또는 직접 설정으로 교체합니다. Claude Code는 대상별 에이전트 요청문 또는 BAT로 교체합니다.
- 새 번들은 현재 승인된 전체 청크를 다시 포함하므로 추가·개정 청크가 같은 MCP에 반영됩니다.
- 저장 폴더를 옮겼다면 ChatGPT Desktop은 새 폴더 기준 안내 값으로 Settings 항목을 갱신하고, Codex는 새 위치에서 BAT를 다시 실행하거나 직접 설정 경로를 갱신합니다.
- ChatGPT 앱의 도구 정의 snapshot이 오래되면 Plugins 설정에서 Refresh를 실행하거나 앱을 다시 생성합니다.

문제가 있으면 연결 상태 확인하기.bat를 실행한 뒤 연결 BAT를 다시 실행합니다.
"""


def _windows_open_text_file_script(file_name: str) -> str:
    return "\n".join(
        [
            "@echo off",
            "chcp 65001 >nul",
            f'start "" notepad.exe "%~dp0{file_name}"',
        ]
    )


def _windows_batch_launcher_script(
    script_name: str,
    args: str = "",
    *,
    next_steps: list[str] | None = None,
) -> str:
    command = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0{script_name}"'
    if args:
        command = f"{command} {args}"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        command,
        "if errorlevel 1 (",
        "  echo.",
        "  echo [연결 실패] 위 오류를 확인하세요.",
        "  pause",
        "  exit /b 1",
        ")",
    ]
    if next_steps:
        lines.extend(["echo.", "echo [다음 단계]"])
        lines.extend(
            f"echo {index}. {_windows_batch_echo_text(step)}"
            for index, step in enumerate(next_steps, start=1)
        )
        lines.append("echo 자세한 안내는 설치 후 MCP 사용 방법 보기.bat를 실행하세요.")
    lines.append("pause")
    return "\n".join(lines)


def _windows_batch_echo_text(value: str) -> str:
    """Escape CMD metacharacters used in generated human-readable echo lines."""
    escaped = str(value).replace("^", "^^")
    for character in ("&", "|", "<", ">"):
        escaped = escaped.replace(character, f"^{character}")
    return escaped


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
    plugin_name = _normalized_plugin_name(server_name)
    marketplace_name = _chatgpt_local_marketplace_name(server_name)
    script = r'''param(
  [ValidateSet("menu", "install", "claude-desktop", "claude-code", "codex", "chatgpt-desktop-direct", "chatgpt-desktop-local", "chatgpt-remote", "chatgpt-desktop", "chatgpt-https", "chatgpt-tunnel", "claude-api", "doctor")]
  [string]$Target = "menu",
  [string]$CodexConfigPath = "",
  [switch]$InstallClaudeDesktop,
  [switch]$InstallCodex,
  [switch]$InstallChatGptDesktopPlugin,
  [switch]$ValidateClaudeDesktop,
  [switch]$InstallPackage
)

$ErrorActionPreference = "Stop"
$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerName = "__SERVER_NAME__"
$PluginName = "__PLUGIN_NAME__"
$PluginMarketplaceName = "__PLUGIN_MARKETPLACE_NAME__"
$PluginTemplateRevision = "__PLUGIN_TEMPLATE_REVISION__"
$EmbeddedClaudeDesktopConfigBase64 = "__EMBEDDED_CLAUDE_DESKTOP_CONFIG_BASE64__"
$EmbeddedChatGptDesktopConfigBase64 = "__EMBEDDED_CHATGPT_DESKTOP_CONFIG_BASE64__"
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
    $ExpectedHash = (Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash
    $ActualHash = (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash
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
    Write-Warning "Generated claude_desktop_config.json is invalid; recovering the MCP entry from the embedded UTF-8 configuration."
    return Read-EmbeddedBundleServerConfig $EmbeddedClaudeDesktopConfigBase64 "Claude Desktop"
  }
}

function Read-ChatGptDesktopBundleServerConfig {
  $ConfigPath = Get-ChatGptDesktopPluginMcpPath
  try {
    return Read-StrictUtf8Json $ConfigPath
  } catch {
    Write-Warning "Generated ChatGPT Desktop .mcp.json is invalid; recovering the MCP entry from the embedded UTF-8 configuration."
    return Read-EmbeddedBundleServerConfig $EmbeddedChatGptDesktopConfigBase64 "ChatGPT Desktop"
  }
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
    plugin_install_command_succeeded = $false
    plugin_manifest_validated = $false
    plugin_discoverable = $false
    plugin_loader_verified = $false
    plugin_name_conflict_detected = $false
    plugin_registered = $false
    plugin_rollback_performed = $false
    plugin_rollback_complete = $null
    plugin_conflict_check_state = "not_checked"
    plugin_conflict_check_reason = "not_checked"
    legacy_plugin_conflict_detected = $false
    legacy_plugin_removed_for_direct_config = $false
    legacy_plugin_restored_after_direct_failure = $false
    legacy_plugin_marketplace_removed = $false
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
    plugin_stdio_verified = $false
    plugin_stdio_runtime_fingerprint = $null
    desktop_process_detected = $false
    desktop_process_started_at = $null
    desktop_mcp_registration_updated_at = $null
    desktop_plugin_registration_updated_at = $null
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
      throw "Client-specific MCP status tracking is required for this bundle, but its recorded Python runtime or scripts.mcp_client_status module is unavailable. Run the generated connection BAT with -InstallPackage, then retry."
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
  $UnresolvedStates = @("preflight_direct", "preflight_plugin", "preflight_claude_code", "preflight_claude_desktop", "installing", "installing_plugin", "plugin_installed_pending_loader_verification")
  if ($UnresolvedStates -contains [string]$Status.installation_state) {
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = "failed_before_verified_install"
      connection_state = "failed"
      loader_verification_state = "failed"
      loader_verification_reason = $ReasonCode
      direct_config_registered = $false
      direct_config_loader_verified = $false
      plugin_registered = $false
      plugin_loader_verified = $false
      installed_config_transport_verified = $false
      direct_stdio_verified = $false
      plugin_stdio_verified = $false
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

function Get-ChatGptDesktopPluginRoot {
  return BundlePath "chatgpt-desktop-local-plugin"
}

function Get-ChatGptDesktopPluginMcpPath {
  return Join-Path (Join-Path (Join-Path (Get-ChatGptDesktopPluginRoot) "plugins") $PluginName) ".mcp.json"
}

function Get-ChatGptDesktopPluginManifestPath {
  return Join-Path (Join-Path (Join-Path (Join-Path (Get-ChatGptDesktopPluginRoot) "plugins") $PluginName) ".codex-plugin") "plugin.json"
}

function Get-ChatGptDesktopMarketplaceManifestPath {
  return Join-Path (Join-Path (Join-Path (Get-ChatGptDesktopPluginRoot) ".agents") "plugins") "marketplace.json"
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

function Run-InstalledPluginConfigSmoke([string]$PluginConfigPath) {
  $StatusBeforeSmoke = Read-JsonFile "bundle_status.json"
  if ([string]$StatusBeforeSmoke.installation_attempt_id -ne $InstallationAttemptId) {
    throw "Installed-plugin smoke does not belong to the current installation attempt."
  }
  $SmokeRuntimeFingerprint = [string]$StatusBeforeSmoke.runtime_fingerprint
  $ReportPath = BundlePath "codex_installed_plugin_config_smoke.json"
  if (Test-Path -LiteralPath $ReportPath) { Remove-Item -LiteralPath $ReportPath -Force }
  $SmokeArgs = @(
    "--server-name", $ServerName,
    "--plugin-mcp-config", $PluginConfigPath,
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
      $ExpectedConfigFullPath = [System.IO.Path]::GetFullPath($PluginConfigPath)
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
    [string]$SmokeResults[0].label -eq "chatgpt_desktop_local" -and
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
    process_started = [bool]($Report -and $Report.process_started)
    mcp_initialized = [bool]($Report -and $Report.mcp_initialized)
    tools_discovered = [bool]($Report -and $Report.tools_discovered)
    generated_client_configs_transport_verified = [bool]$Verified
    plugin_stdio_verified = [bool]$Verified
    plugin_stdio_runtime_fingerprint = $(if ($Verified) { $SmokeRuntimeFingerprint } else { $null })
    direct_stdio_verified = $false
    transport_end_to_end_verified = [bool]$Verified
    desktop_tool_scan_verified = $false
    conversation_attachment_verified = $false
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
  if (-not $Verified -and [string]$CurrentStatus.installation_state -like "plugin_*") {
    $NextInstallationState = "plugin_installed_loader_verified_pending_fresh_inventory"
  }
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
  $Source = Read-ChatGptDesktopBundleServerConfig
  $Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")
  if (-not $Source.PSObject.Properties["mcpServers"]) {
    throw "Generated ChatGPT Desktop .mcp.json does not contain mcpServers."
  }
  $Server = $Source.mcpServers.PSObject.Properties[$ServerName]
  if (-not $Server) {
    throw "Generated ChatGPT Desktop .mcp.json does not contain server $ServerName."
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

function Test-CodexCliExecutable {
  return -not [string]::IsNullOrWhiteSpace((Resolve-CodexCliExecutable))
}

function Remove-GeneratedPluginConflictForDirectConfig {
  $script:LegacyPluginRemovedThisAttempt = $false
  $PluginSelector = "$PluginName@$PluginMarketplaceName"
  $ListResult = Invoke-CodexCli @("plugin", "list", "--json")
  if ($ListResult.ExitCode -ne 0) {
    throw "Could not inspect optional plugin inventory before direct MCP installation. Registration stopped to avoid ambiguous direct/plugin loading."
  }
  try {
    $Inventory = (($ListResult.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    throw "Codex plugin inventory was not valid JSON. Registration stopped to avoid ambiguous direct/plugin loading."
  }
  $Conflicts = @($Inventory.installed | Where-Object {
    [string]$_.pluginId -eq $PluginSelector -and $_.installed -eq $true
  })
  if ($Conflicts.Count -eq 0) { return $false }
  Update-BundleStatus @{ legacy_plugin_conflict_detected = $true }
  $RemoveResult = Invoke-CodexCli @("plugin", "remove", $PluginSelector, "--json")
  $RemoveResult.Output | Out-Host
  if ($RemoveResult.ExitCode -ne 0) {
    throw "The old generated plugin $PluginSelector conflicts with the preferred direct MCP entry and could not be removed."
  }
  $script:LegacyPluginRemovedThisAttempt = $true
  $VerifyResult = Invoke-CodexCli @("plugin", "list", "--json")
  try {
    $VerifiedInventory = (($VerifyResult.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    throw "The old generated plugin removal could not be verified from Codex plugin inventory."
  }
  $StillInstalled = @($VerifiedInventory.installed | Where-Object {
    [string]$_.pluginId -eq $PluginSelector -and $_.installed -eq $true
  })
  if ($VerifyResult.ExitCode -ne 0 -or $StillInstalled.Count -ne 0) {
    throw "The old generated plugin $PluginSelector is still installed, so direct MCP registration stopped to avoid ambiguous loading."
  }
  Update-BundleStatus @{
    legacy_plugin_removed_for_direct_config = $true
    legacy_plugin_restored_after_direct_failure = $false
    plugin_registered = $false
    plugin_discoverable = $false
    plugin_loader_verified = $false
    plugin_stdio_verified = $false
    generated_client_configs_transport_verified = $false
  }
  Write-Host "Removed conflicting optional plugin before direct MCP registration: $PluginSelector"
  return $true
}

function Remove-GeneratedPluginMarketplaceAfterDirectConfig {
  $MarketplaceRemoved = $false
  $MarketplaceList = Invoke-CodexCli @("plugin", "marketplace", "list", "--json")
  if ($MarketplaceList.ExitCode -eq 0) {
    try {
      $MarketplaceInventory = (($MarketplaceList.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
      $MarketplaceMatches = @($MarketplaceInventory.marketplaces | Where-Object {
        [string]$_.name -eq $PluginMarketplaceName
      })
      if ($MarketplaceMatches.Count -gt 0) {
        $MarketplaceRemove = Invoke-CodexCli @("plugin", "marketplace", "remove", $PluginMarketplaceName, "--json")
        $MarketplaceRemove.Output | Out-Host
        if ($MarketplaceRemove.ExitCode -eq 0) { $MarketplaceRemoved = $true }
      }
    } catch {
      Write-Warning "The obsolete generated plugin marketplace could not be inspected; the conflicting plugin itself was removed."
    }
  }
  Update-BundleStatus @{ legacy_plugin_marketplace_removed = $MarketplaceRemoved }
}

function Restore-GeneratedPluginAfterDirectFailure {
  $PluginSelector = "$PluginName@$PluginMarketplaceName"
  $RestoreResult = Invoke-CodexCli @("plugin", "add", $PluginSelector)
  $RestoreResult.Output | Out-Host
  if ($RestoreResult.ExitCode -ne 0) {
    Write-Warning "The previous optional plugin could not be restored after direct MCP installation failed."
    return $false
  }
  $VerifyResult = Invoke-CodexCli @("plugin", "list", "--json")
  try {
    $VerifiedInventory = (($VerifyResult.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    Write-Warning "The previous optional plugin restore could not be verified from Codex plugin inventory."
    return $false
  }
  $Restored = @($VerifiedInventory.installed | Where-Object {
    [string]$_.pluginId -eq $PluginSelector -and $_.installed -eq $true -and $_.enabled -eq $true
  }).Count -eq 1
  if ($VerifyResult.ExitCode -ne 0 -or -not $Restored) {
    Write-Warning "The previous optional plugin was not discoverable after restore."
    return $false
  }
  Update-BundleStatus @{
    legacy_plugin_removed_for_direct_config = $false
    legacy_plugin_restored_after_direct_failure = $true
  }
  Write-Host "Restored previous optional plugin after direct MCP installation failed: $PluginSelector"
  return $true
}

function Install-CodexConfig([string]$ConsumerName = "Codex CLI") {
  $CodexCliAvailable = [bool](Test-CodexCliExecutable)
  $LegacyPluginRemoved = $false
  $script:LegacyPluginRemovedThisAttempt = $false
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
  if ($CodexCliAvailable) {
    $LegacyPluginRemoved = [bool](Remove-GeneratedPluginConflictForDirectConfig)
  } else {
    Update-BundleStatus @{
      plugin_conflict_check_state = "not_checked"
      plugin_conflict_check_reason = "codex_cli_unavailable"
    }
  }
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
  $InstalledConfigFingerprint = "sha256:" + (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
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
    "sha256:" + (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
  } else {
    $null
  }
  if ([string]::IsNullOrWhiteSpace($PostSmokeConfigFingerprint) -or
      -not [string]::Equals($PostSmokeConfigFingerprint, $InstalledConfigFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "$ConsumerName MCP config changed during installed-config stdio verification; the prior state will be restored."
  }
  if (-not $CodexCliAvailable) {
    $DesktopRestartState = Get-ChatGptDesktopRestartState -RegistrationUpdatedAtUtc $DirectRegistrationUpdatedAtUtc
    $PendingConnectionState = if ($DesktopRestartState.desktop_restart_status -eq "not_running") {
      "pending_desktop_launch"
    } else {
      "pending_desktop_restart"
    }
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = "installed_pending_desktop_verification"
      connection_state = $PendingConnectionState
      direct_config_registered = $true
      direct_config_loader_verified = $false
      loader_verification_state = "blocked"
      loader_verification_reason = "codex_cli_unavailable"
      direct_config_rollback_performed = $false
      installed_config_fingerprint = $InstalledConfigFingerprint
      desktop_process_detected = $DesktopRestartState.desktop_process_detected
      desktop_process_started_at = $DesktopRestartState.desktop_process_started_at
      desktop_mcp_registration_updated_at = $DirectRegistrationUpdatedAtUtc.ToString("o")
      desktop_restart_checked_at = $DesktopRestartState.desktop_restart_checked_at
      desktop_restart_required = $DesktopRestartState.desktop_restart_required
      desktop_restart_status = $DesktopRestartState.desktop_restart_status
      desktop_restart_reason_code = $DesktopRestartState.desktop_restart_reason_code
      desktop_tool_scan_verified = $false
      conversation_attachment_verified = $false
      end_to_end_verified = $false
    }
    $script:CodexLoaderVerified = $false
    Write-Host "$ConsumerName MCP config updated: $TargetPath"
    Write-Host "[CONFIGURED - DESKTOP VERIFICATION REQUIRED] MCP config readback and direct transport passed."
    if ($ConsumerName -eq "ChatGPT Desktop") {
      Write-Warning "Automatic loader verification is unavailable. The valid local MCP config was preserved; Desktop verification remains pending."
      Write-Host "Fully quit ChatGPT Desktop, start it again, open a new conversation, run /mcp, and select the exact server name $ServerName."
      Write-Host "Do not report this state as connected until the Desktop surface and an actual MCP tool call are verified."
    } else {
      Write-Warning "A trusted executable Codex host CLI was not found, so loader verification remains pending. The valid config was preserved instead of rolled back."
      Write-Host "Restart Codex CLI and verify $ServerName with /mcp in a new task."
    }
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
  $DesktopRestartState = Get-ChatGptDesktopRestartState -RegistrationUpdatedAtUtc $DirectRegistrationUpdatedAtUtc
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installation_state = "installed_loader_verified"
    connection_state = $(if ($DesktopRestartState.desktop_restart_required) { "pending_desktop_restart" } else { "pending_desktop_tool_scan" })
    direct_config_registered = $true
    direct_config_loader_verified = $true
    loader_verification_state = "verified"
    loader_verification_reason = $null
    direct_config_rollback_performed = $false
    direct_config_path = $TargetPath
    installed_config_fingerprint = $InstalledConfigFingerprint
    desktop_process_detected = $DesktopRestartState.desktop_process_detected
    desktop_process_started_at = $DesktopRestartState.desktop_process_started_at
    desktop_mcp_registration_updated_at = $DirectRegistrationUpdatedAtUtc.ToString("o")
    desktop_restart_checked_at = $DesktopRestartState.desktop_restart_checked_at
    desktop_restart_required = $DesktopRestartState.desktop_restart_required
    desktop_restart_status = $DesktopRestartState.desktop_restart_status
    desktop_restart_reason_code = $DesktopRestartState.desktop_restart_reason_code
  }
  if ($LegacyPluginRemoved -or $script:LegacyPluginRemovedThisAttempt) {
    Remove-GeneratedPluginMarketplaceAfterDirectConfig
  }
  $RemovedDuplicates = @($RemovedNames | Where-Object { $_ -and $_ -ne $ServerName } | Select-Object -Unique)
  if ($RemovedDuplicates.Count -gt 0) {
    Write-Host "Removed duplicate entries for this bundle: $($RemovedDuplicates -join ', ')"
  }
  Write-Host "$ConsumerName MCP config updated: $TargetPath"
  Write-Host "Verified MCP server name and bundle paths: $ServerName"
  switch ($DesktopRestartState.desktop_restart_status) {
    "required" {
      Write-Warning "[RESTART REQUIRED] ChatGPT Desktop started before this direct MCP registration. Fully quit every ChatGPT.exe process, restart the app, and open a new conversation."
    }
    "not_running" { Write-Host "[DESKTOP NOT RUNNING] The next launch will load the direct MCP config." }
    "up_to_date" { Write-Host "[DESKTOP CURRENT] The running Desktop started after direct MCP registration." }
    default { Write-Warning "[RESTART STATUS UNKNOWN] Fully restart ChatGPT Desktop before testing." }
  }
  if ($ConsumerName -eq "ChatGPT Desktop") {
    Write-Host "Restart ChatGPT Desktop and verify $ServerName from /mcp in a new conversation."
  } else {
    Write-Host "Restart Codex CLI or reload MCP servers to pick up $ServerName."
  }
  } catch {
    $InstallError = $_
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
    $PluginRestoreFailed = $false
    if ($LegacyPluginRemoved -or $script:LegacyPluginRemovedThisAttempt) {
      try { $PluginRestoreFailed = -not [bool](Restore-GeneratedPluginAfterDirectFailure) }
      catch {
        $PluginRestoreFailed = $true
        Write-Warning "The previous optional plugin restore raised an error: $($_.Exception.Message)"
      }
    }
    $RollbackComplete = -not $RollbackFailureMessage -and -not $PluginRestoreFailed
    try {
      Update-BundleStatus @{
        installation_attempt_id = $InstallationAttemptId
        installation_state = $(if ($RollbackComplete) { "failed_rolled_back" } else { "failed_rollback_incomplete" })
        connection_state = "failed"
        direct_config_registered = $false
        direct_config_loader_verified = $false
        loader_verification_state = "failed"
        loader_verification_reason = $(if ($RollbackComplete) { "install_failed_prior_state_restored" } else { "rollback_incomplete" })
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
      throw "Direct MCP installation failed and prior state could not be restored completely. Config rollback error='$RollbackFailureMessage'; plugin_restore_failed=$PluginRestoreFailed. Original error: $($InstallError.Exception.Message)"
    }
    Fail-ClientConnectionAttempt "direct_install_failed_prior_state_restored" -RolledBack
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
  $ActualFingerprint = "sha256:" + (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if (-not [string]::IsNullOrWhiteSpace($ExpectedFingerprint) -and
      -not [string]::Equals($ActualFingerprint, $ExpectedFingerprint, [System.StringComparison]::OrdinalIgnoreCase)) {
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
    $OriginalConfigFingerprint = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash
    Copy-Item -LiteralPath $TargetPath -Destination $BackupPath
    $BackupConfigFingerprint = (Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash
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
  Write-Host "[CONFIGURED - CLAUDE DESKTOP VERIFICATION REQUIRED] Restart Claude Desktop, confirm the server in Connectors, and invoke get_index_status in a new conversation."
  } catch {
    $InstallError = $_
    $RollbackComplete = $false
    try {
      if ($HadExistingConfig -and $BackupPath -and (Test-Path -LiteralPath $BackupPath)) {
        Restore-FileAtomically $BackupPath $TargetPath
        $RestoredConfigFingerprint = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash
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
  param([switch]$ForChatGptDesktop)
  Show-Header
  $InstallDirect = $InstallCodex -or $ForChatGptDesktop
  $ConsumerName = if ($ForChatGptDesktop) { "ChatGPT Desktop" } else { "Codex CLI" }
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
      Run-ChatGptDesktopRecognitionObservation (Get-CodexConfigPath)
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
        Run-ChatGptDesktopRecognitionObservation (Get-CodexConfigPath)
        $PostProbeStatus = Read-JsonFile "bundle_status.json"
        $CurrentConfigFingerprint = if (Test-Path -LiteralPath (Get-CodexConfigPath)) {
          "sha256:" + (Get-FileHash -LiteralPath (Get-CodexConfigPath) -Algorithm SHA256).Hash.ToLowerInvariant()
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
        if ($ForChatGptDesktop) {
          Write-Warning "[DESKTOP VERIFICATION PENDING] The local MCP config and direct transport passed, but automatic Desktop loader verification was unavailable."
        } else {
          Write-Warning "[LOADER VERIFICATION PENDING] The config and direct transport passed, but a fresh Codex CLI loader inventory was not available."
        }
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

function Show-ChatGptDesktop {
  Show-Header
  if ($InstallChatGptDesktopPlugin -or $InstallCodex) {
    $LocalInstallMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-LocalMcpInstallation")
    $LocalInstallLockAcquired = $false
    try {
      try { $LocalInstallLockAcquired = $LocalInstallMutex.WaitOne([TimeSpan]::FromSeconds(30)) }
      catch [System.Threading.AbandonedMutexException] { $LocalInstallLockAcquired = $true }
      if (-not $LocalInstallLockAcquired) {
        throw "Another local MCP connection process is running. Wait for it to finish, then retry."
      }
      Start-LocalInstallationAttempt "preflight_plugin"
      if (-not (Run-LocalStdioDoctor)) {
        throw "Local MCP doctor failed; ChatGPT Desktop plugin installation was not attempted."
      }
      Install-ChatGptDesktopPlugin
      Run-Script "validate_client_config_smoke.ps1"
      Run-CodexAppServerMcpCheck
      $PluginVerifiedStatus = Read-JsonFile "bundle_status.json"
      Complete-ClientConnectionAttempt @("registration", "loader", "transport", "fresh_app_server") ([string]$PluginVerifiedStatus.installed_config_fingerprint) ([string]$PluginVerifiedStatus.runtime_fingerprint)
      Write-Host ""
      Write-Host "Plugin registration and MCP protocol validation completed."
      Write-Host "This still does not prove that the plugin is attached to the current conversation."
      Write-Host "Fully quit ChatGPT Desktop, start it again, and open a new conversation."
      Write-Host "First run /mcp and verify that $ServerName is connected."
      Write-Host "On a Work/Codex surface that exposes local plugins, select + > More > $ServerName."
      Write-Host "Verification prompt: $ServerName MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 fetch로 조회해 원문과 출처를 보여줘."
      return
    } catch {
      $PluginShowError = $_
      Mark-CurrentAttemptFailedIfUnresolved "plugin_preflight_or_install_failed"
      throw $PluginShowError
    } finally {
      if ($LocalInstallLockAcquired) { $LocalInstallMutex.ReleaseMutex() }
      $LocalInstallMutex.Dispose()
    }
  }
  Write-Host "Generated local plugin marketplace: $(Get-ChatGptDesktopPluginRoot)"
  Write-Host "Registration and conversation attachment are separate states."
  Write-Host "To register/update automatically:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -InstallPackage -Target chatgpt-desktop-local -InstallChatGptDesktopPlugin"
}

function Install-ChatGptDesktopPlugin {
  function Invoke-CodexPluginCli([string[]]$Arguments) {
    # codex.ps1 forwards native stderr as PowerShell error records. With the
    # bundle-wide Stop policy that would abort before we can inspect the CLI
    # exit code (including an expected "not installed" during cleanup).
    return Invoke-CodexCli -Arguments $Arguments
  }

  function Get-PluginMarketplaceSource([object]$MarketplaceEntry) {
    if (-not $MarketplaceEntry) { return "" }
    $SourceValue = $MarketplaceEntry.source
    if ($SourceValue -is [string]) { return [string]$SourceValue }
    if ($SourceValue -and $SourceValue.PSObject.Properties["source"]) {
      return [string]$SourceValue.source
    }
    return ""
  }

  $MarketplaceRoot = Get-ChatGptDesktopPluginRoot
  $PluginMcpPath = Get-ChatGptDesktopPluginMcpPath
  $PluginManifestPath = Get-ChatGptDesktopPluginManifestPath
  $MarketplaceManifest = Get-ChatGptDesktopMarketplaceManifestPath
  $ExpectedLauncher = BundlePath "run_mcp_stdio_server.ps1"
  $ExpectedDataDir = Get-BundleDataDir
  if (-not (Test-Path -LiteralPath $MarketplaceManifest)) {
    throw "Generated plugin marketplace is missing: $MarketplaceManifest"
  }
  if (-not (Test-Path -LiteralPath $PluginMcpPath)) {
    throw "Generated plugin MCP config is missing: $PluginMcpPath"
  }
  if (-not (Test-CodexCliExecutable)) {
    throw "A trusted executable Codex host CLI was not found. Install/update ChatGPT Desktop with Codex support, then rerun this button."
  }

  $ExistingPluginMcp = Read-StrictUtf8Json $PluginMcpPath
  $ExistingPluginArgs = @()
  if ($ExistingPluginMcp.PSObject.Properties["mcpServers"] -and
      $ExistingPluginMcp.mcpServers.PSObject.Properties[$ServerName]) {
    $ExistingPluginArgs = @($ExistingPluginMcp.mcpServers.PSObject.Properties[$ServerName].Value.args)
  }
  $Source = Read-ChatGptDesktopBundleServerConfig
  $Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")
  $PluginSource = [ordered]@{ mcpServers = $Source.mcpServers }
  $RewrittenPluginArgs = @($PluginSource.mcpServers.PSObject.Properties[$ServerName].Value.args)
  $PluginPathsChanged = ($ExistingPluginArgs | ConvertTo-Json -Compress) -ne ($RewrittenPluginArgs | ConvertTo-Json -Compress)
  Write-JsonUtf8NoBom $PluginMcpPath $PluginSource 50

  if ($PluginPathsChanged) {
    $ManifestForCachebuster = Read-StrictUtf8Json $PluginManifestPath
    $PluginMcpText = Get-Content -LiteralPath $PluginMcpPath -Raw -Encoding UTF8
    $CachebusterSource = "$PluginTemplateRevision`n$BundleDir`n$PluginMcpText"
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
      $HashBytes = $Sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($CachebusterSource))
    } finally {
      $Sha256.Dispose()
    }
    $Cachebuster = -join ($HashBytes | ForEach-Object { $_.ToString("x2") })
    $ManifestForCachebuster.version = "0.1.0+codex.$($Cachebuster.Substring(0, 12))"
    Write-JsonUtf8NoBom $PluginManifestPath $ManifestForCachebuster 50
  }

  $PluginManifest = Read-StrictUtf8Json $PluginManifestPath
  $PluginMcp = Read-StrictUtf8Json $PluginMcpPath
  $Marketplace = Read-StrictUtf8Json $MarketplaceManifest
  if ($PluginManifest.name -ne $PluginName) { throw "Plugin manifest name mismatch: $PluginManifestPath" }
  if ([string]$PluginManifest.mcpServers -ne "./.mcp.json") { throw "Plugin manifest mcpServers must point to ./.mcp.json." }
  if ([string]$PluginManifest.version -notmatch '^0\.1\.0\+codex\.[0-9a-f]{12}$') { throw "Plugin manifest is missing the required cachebuster version." }
  if ($PluginMcp.PSObject.Properties["mcp_servers"]) { throw "Plugin MCP config uses unsupported mcp_servers; regenerate it with the current mcpServers container." }
  if (-not $PluginMcp.mcpServers.PSObject.Properties[$ServerName]) { throw "Plugin MCP config does not contain mcpServers entry $ServerName." }
  $ExpectedPluginArgs = @($PluginMcp.mcpServers.PSObject.Properties[$ServerName].Value.args | ForEach-Object { [string]$_ })
  $MarketplacePlugin = @($Marketplace.plugins | Where-Object { $_.name -eq $PluginName })
  if ($MarketplacePlugin.Count -ne 1) { throw "Marketplace manifest does not contain exactly one $PluginName plugin entry." }
  $ExpectedPluginVersion = [string]$PluginManifest.version
  Start-LocalInstallationAttempt "installing_plugin"
  Update-BundleStatus @{ plugin_manifest_validated = $true }

  $PluginSelector = "$PluginName@$PluginMarketplaceName"
  $InstallMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-$PluginMarketplaceName")
  $InstallLockAcquired = $false
  $ExternalPluginStateMutated = $false
  $PriorPlugin = $null
  $PriorMarketplace = $null
  $PriorMarketplaceSource = $null
  $PriorPluginVersion = $null
  $PriorPluginEnabled = $null
  $PriorPluginEnabledKnown = $false
  try {
    try {
      $InstallLockAcquired = $InstallMutex.WaitOne([TimeSpan]::FromSeconds(30))
    } catch [System.Threading.AbandonedMutexException] {
      $InstallLockAcquired = $true
    }
    if (-not $InstallLockAcquired) {
      throw "Another $PluginMarketplaceName plugin installation is still running. Wait for it to finish, then retry."
    }

    $PriorListResult = Invoke-CodexPluginCli @("plugin", "list", "--json")
    if ($PriorListResult.ExitCode -ne 0) {
      throw "Could not capture the existing plugin inventory before installation; no plugin state was changed."
    }
    try {
      $PriorInventory = (($PriorListResult.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
    } catch {
      throw "The existing plugin inventory was not valid JSON; no plugin state was changed."
    }
    if (-not $PriorInventory -or -not $PriorInventory.PSObject.Properties["installed"]) {
      throw "The existing plugin inventory did not contain an installed list; no plugin state was changed."
    }
    $PriorMatches = @($PriorInventory.installed | Where-Object {
      [string]$_.pluginId -eq $PluginSelector -and $_.installed -eq $true
    })
    if ($PriorMatches.Count -gt 1) {
      throw "More than one existing $PluginSelector entry was found; no plugin state was changed."
    }
    if ($PriorMatches.Count -eq 1) {
      $PriorPlugin = $PriorMatches[0]
      $PriorMarketplaceSource = Get-PluginMarketplaceSource $PriorPlugin.marketplaceSource
      $PriorPluginVersion = [string]$PriorPlugin.version
      if (-not $PriorPlugin.PSObject.Properties["enabled"] -or [string]::IsNullOrWhiteSpace($PriorPluginVersion)) {
        throw "The existing plugin inventory did not identify its exact version and enabled state; no plugin state was changed."
      }
      $PriorPluginEnabled = [bool]$PriorPlugin.enabled
      $PriorPluginEnabledKnown = $true
      if ([string]::IsNullOrWhiteSpace($PriorMarketplaceSource)) {
        throw "The existing plugin inventory did not identify its marketplace source; no plugin state was changed."
      }
    } else {
      $PriorMarketplaceList = Invoke-CodexPluginCli @("plugin", "marketplace", "list", "--json")
      if ($PriorMarketplaceList.ExitCode -ne 0) {
        throw "Could not capture the existing marketplace inventory before installation; no plugin state was changed."
      }
      try {
        $PriorMarketplaceInventory = (($PriorMarketplaceList.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
      } catch {
        throw "The existing marketplace inventory was not valid JSON; no plugin state was changed."
      }
      if (-not $PriorMarketplaceInventory -or -not $PriorMarketplaceInventory.PSObject.Properties["marketplaces"]) {
        throw "The existing marketplace inventory did not contain a marketplaces list; no plugin state was changed."
      }
      $PriorMarketplaceMatches = @($PriorMarketplaceInventory.marketplaces | Where-Object {
        [string]$_.name -eq $PluginMarketplaceName
      })
      if ($PriorMarketplaceMatches.Count -gt 1) {
        throw "More than one existing $PluginMarketplaceName marketplace entry was found; no plugin state was changed."
      }
      if ($PriorMarketplaceMatches.Count -eq 1) {
        $PriorMarketplace = $PriorMarketplaceMatches[0]
        $PriorMarketplaceSource = Get-PluginMarketplaceSource $PriorMarketplace
        if ([string]::IsNullOrWhiteSpace($PriorMarketplaceSource)) {
          throw "The existing marketplace inventory did not identify its source; no plugin state was changed."
        }
      }
    }

    $ExternalPluginStateMutated = $true
    $null = Invoke-CodexPluginCli @("plugin", "remove", $PluginSelector, "--json")
    $null = Invoke-CodexPluginCli @("plugin", "marketplace", "remove", $PluginMarketplaceName, "--json")
    $NameConflict = Invoke-CodexPluginCli @("mcp", "get", $ServerName, "--json")
    $ExistingNamedMcp = $null
    if ($NameConflict.ExitCode -eq 0) {
      try {
        $ExistingNamedMcp = (($NameConflict.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
      } catch {
        $ExistingNamedMcp = $null
      }
    }
    if ($ExistingNamedMcp -and [string]$ExistingNamedMcp.name -eq $ServerName) {
      Update-BundleStatus @{
        plugin_name_conflict_detected = $true
        plugin_loader_verified = $false
        plugin_registered = $false
      }
      throw "An existing direct or unrelated MCP entry already uses the name $ServerName. Remove or rename that entry before installing this plugin; otherwise codex mcp get cannot prove which source was loaded."
    }
    $MarketplaceAdd = Invoke-CodexPluginCli @("plugin", "marketplace", "add", $MarketplaceRoot, "--json")
    $MarketplaceAdd.Output | Out-Host
    if ($MarketplaceAdd.ExitCode -ne 0) {
      throw "Failed to register the current marketplace source: $MarketplaceRoot"
    }

    $PluginInstallSucceeded = $false
    $PluginInstallOutput = @()
    for ($PluginInstallAttempt = 1; $PluginInstallAttempt -le 3; $PluginInstallAttempt++) {
      $PluginInstall = Invoke-CodexPluginCli @("plugin", "add", $PluginSelector, "--json")
      $PluginInstallOutput = @($PluginInstall.Output)
      if ($PluginInstall.ExitCode -eq 0) {
        $PluginInstallOutput | Out-Host
        $PluginInstallSucceeded = $true
        break
      }
      if ($PluginInstallAttempt -lt 3) {
        Start-Sleep -Milliseconds (250 * $PluginInstallAttempt)
        $MarketplaceRetry = Invoke-CodexPluginCli @("plugin", "marketplace", "add", $MarketplaceRoot, "--json")
        if ($MarketplaceRetry.ExitCode -ne 0) {
          break
        }
      }
    }
    if (-not $PluginInstallSucceeded) {
      $PluginInstallOutput | Out-Host
      throw "Failed to register ChatGPT Desktop local plugin $PluginSelector after 3 attempts."
    }
    $PluginRegistrationUpdatedAtUtc = [DateTimeOffset]::UtcNow
    try {
      $InstalledPayload = (($PluginInstallOutput | Out-String) | ConvertFrom-Json -ErrorAction Stop)
    } catch {
      throw "Plugin install command returned invalid JSON, so the installed cache cannot be verified."
    }
    $InstalledCacheRoot = [string]$InstalledPayload.installedPath
    $InstalledCacheMcpPath = Join-Path $InstalledCacheRoot ".mcp.json"
    if ([string]$InstalledPayload.pluginId -ne $PluginSelector -or
        [string]$InstalledPayload.version -ne $ExpectedPluginVersion -or
        [string]::IsNullOrWhiteSpace($InstalledCacheRoot) -or
        -not (Test-Path -LiteralPath $InstalledCacheMcpPath)) {
      throw "Plugin install command did not return the expected plugin id, version, and cached .mcp.json path."
    }
    $InstalledCacheMcp = Read-StrictUtf8Json $InstalledCacheMcpPath
    if ($InstalledCacheMcp.PSObject.Properties["mcp_servers"] -or
        -not $InstalledCacheMcp.PSObject.Properties["mcpServers"] -or
        -not $InstalledCacheMcp.mcpServers.PSObject.Properties[$ServerName]) {
      throw "Installed plugin cache does not contain the canonical mcpServers entry $ServerName."
    }
    $InstalledCacheEntry = $InstalledCacheMcp.mcpServers.PSObject.Properties[$ServerName].Value
    $InstalledCacheArgs = @($InstalledCacheEntry.args)
    if ([string]$InstalledCacheEntry.command -ne "powershell.exe" -or
        -not (Test-SamePath (Get-SingleArgumentValue $InstalledCacheArgs "-File") $ExpectedLauncher) -or
        -not (Test-SamePath (Get-SingleArgumentValue $InstalledCacheArgs "--data-dir") $ExpectedDataDir) -or
        -not (Test-SameMcpArguments $InstalledCacheArgs $ExpectedPluginArgs)) {
      throw "Installed plugin cache exists, but its command or full MCP argument contract does not match the current bundle."
    }
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installation_state = "plugin_installed_pending_loader_verification"
    connection_state = "not_connected"
    launcher_ready = $true
    plugin_install_command_succeeded = $true
    plugin_discoverable = $false
    plugin_loader_verified = $false
    plugin_registered = $false
  }
  $ListResult = Invoke-CodexPluginCli @("plugin", "list", "--json")
  $ListOutput = @($ListResult.Output)
  $ListExitCode = $ListResult.ExitCode
  if ($ListExitCode -ne 0) {
    Update-BundleStatus @{
      plugin_discoverable = $false
      plugin_registered = $false
    }
    throw "Plugin install command succeeded, but codex plugin list --json failed. Do not report it as registered."
  }
  $ListText = ($ListOutput | Out-String)
  try {
    $PluginInventory = $ListText | ConvertFrom-Json -ErrorAction Stop
  } catch {
    Update-BundleStatus @{
      plugin_discoverable = $false
      plugin_registered = $false
    }
    throw "Plugin install command succeeded, but codex plugin list --json returned invalid JSON. Do not report it as registered."
  }
  $InstalledPlugin = @($PluginInventory.installed | Where-Object {
    $_.pluginId -eq $PluginSelector -and $_.installed -eq $true -and $_.enabled -eq $true
  })
  if ($InstalledPlugin.Count -ne 1) {
    Update-BundleStatus @{
      plugin_discoverable = $false
      plugin_registered = $false
    }
    throw "Plugin install command succeeded, but exactly one enabled $PluginSelector entry was not discoverable. Do not report it as registered."
  }
  if ([string]$InstalledPlugin[0].version -ne $ExpectedPluginVersion) {
    Update-BundleStatus @{
      plugin_discoverable = $false
      plugin_registered = $false
    }
    throw "Plugin discovery returned stale version $($InstalledPlugin[0].version); expected $ExpectedPluginVersion."
  }
  $ExpectedMarketplaceRoot = [System.IO.Path]::GetFullPath($MarketplaceRoot).TrimEnd('\')
  $DiscoveredMarketplaceRoot = Get-PluginMarketplaceSource $InstalledPlugin[0].marketplaceSource
  if ($DiscoveredMarketplaceRoot.StartsWith('\\?\')) {
    $DiscoveredMarketplaceRoot = $DiscoveredMarketplaceRoot.Substring(4)
  }
  try {
    $DiscoveredMarketplaceRoot = [System.IO.Path]::GetFullPath($DiscoveredMarketplaceRoot).TrimEnd('\')
  } catch {
    $DiscoveredMarketplaceRoot = ""
  }
  if (-not [string]::Equals($ExpectedMarketplaceRoot, $DiscoveredMarketplaceRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Update-BundleStatus @{
      plugin_discoverable = $false
      plugin_registered = $false
    }
    throw "Plugin discovery returned a stale marketplace source: $DiscoveredMarketplaceRoot; expected $ExpectedMarketplaceRoot."
  }
  Update-BundleStatus @{
    plugin_discoverable = $true
    plugin_loader_verified = $false
    plugin_registered = $false
  }
  $McpGetResult = Invoke-CodexPluginCli @("mcp", "get", $ServerName, "--json")
  if ($McpGetResult.ExitCode -ne 0) {
    throw "Plugin is installed and discoverable, but codex mcp get could not resolve $ServerName. Check the .mcp.json mcpServers schema."
  }
  try {
    $LoadedMcp = (($McpGetResult.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    throw "codex mcp get returned invalid JSON for $ServerName. Do not report the plugin as registered."
  }
  $LoadedTransport = $LoadedMcp.transport
  $LoadedArgs = @($LoadedTransport.args)
  $LoadedLauncher = Get-SingleArgumentValue $LoadedArgs "-File"
  $LoadedDataDir = Get-SingleArgumentValue $LoadedArgs "--data-dir"
  $LoaderVerified = [string]$LoadedMcp.name -eq $ServerName -and
    $LoadedMcp.enabled -eq $true -and
    [string]$LoadedTransport.type -eq "stdio" -and
    [string]$LoadedTransport.command -eq "powershell.exe" -and
    (Test-SamePath $LoadedLauncher $ExpectedLauncher) -and
    (Test-SamePath $LoadedDataDir $ExpectedDataDir) -and
    (Test-SameMcpArguments $LoadedArgs $ExpectedPluginArgs)
  if (-not $LoaderVerified) {
    throw "Codex MCP loader resolved $ServerName, but its command or full MCP argument contract does not match the current plugin."
  }
  if (-not (Run-InstalledPluginConfigSmoke $InstalledCacheMcpPath)) {
    throw "The installed plugin cache could not complete the MCP initialize/tools/search/fetch transport contract."
  }
  $InstalledPluginFingerprint = "sha256:" + (Get-FileHash -LiteralPath $InstalledCacheMcpPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $DesktopRestartState = Get-ChatGptDesktopRestartState -RegistrationUpdatedAtUtc $PluginRegistrationUpdatedAtUtc
  Update-BundleStatus @{
    installation_attempt_id = $InstallationAttemptId
    installation_state = "plugin_installed_loader_verified"
    connection_state = $(if ($DesktopRestartState.desktop_restart_required) { "pending_desktop_restart" } else { "pending_desktop_tool_scan" })
    launcher_ready = $true
    plugin_install_command_succeeded = $true
    plugin_manifest_validated = $true
    plugin_discoverable = $true
    plugin_loader_verified = $true
    plugin_registered = $true
    installed_config_fingerprint = $InstalledPluginFingerprint
    loader_verification_state = "verified"
    loader_verification_reason = $null
    desktop_process_detected = $DesktopRestartState.desktop_process_detected
    desktop_process_started_at = $DesktopRestartState.desktop_process_started_at
    desktop_mcp_registration_updated_at = $PluginRegistrationUpdatedAtUtc.ToString("o")
    desktop_plugin_registration_updated_at = $PluginRegistrationUpdatedAtUtc.ToString("o")
    desktop_restart_checked_at = $DesktopRestartState.desktop_restart_checked_at
    desktop_restart_required = $DesktopRestartState.desktop_restart_required
    desktop_restart_status = $DesktopRestartState.desktop_restart_status
    desktop_restart_reason_code = $DesktopRestartState.desktop_restart_reason_code
    desktop_tool_scan_verified = $false
    conversation_attachment_verified = $false
    conversation_attachment_unverified = $true
    end_to_end_verified = $false
  }
    Write-Host "Plugin registered and resolved by the Codex MCP loader: $PluginSelector ($ExpectedPluginVersion)"
    switch ($DesktopRestartState.desktop_restart_status) {
      "required" {
        Write-Warning "[RESTART REQUIRED] ChatGPT Desktop started before this plugin registration. Fully quit every ChatGPT.exe process, restart the app, and open a new conversation."
      }
      "not_running" { Write-Host "[DESKTOP NOT RUNNING] The next launch will load the plugin." }
      "up_to_date" { Write-Host "[DESKTOP CURRENT] The running Desktop started after registration." }
      default { Write-Warning "[RESTART STATUS UNKNOWN] Fully restart ChatGPT Desktop before testing." }
    }
    Write-Host "Registration is complete; attachment to the current conversation remains unverified."
  } catch {
    $PluginInstallError = $_
    $RollbackComplete = $true
    if ($ExternalPluginStateMutated) {
      try {
        $null = Invoke-CodexPluginCli @("plugin", "remove", $PluginSelector, "--json")
        $null = Invoke-CodexPluginCli @("plugin", "marketplace", "remove", $PluginMarketplaceName, "--json")
        if (-not [string]::IsNullOrWhiteSpace($PriorMarketplaceSource)) {
          $RestoreMarketplace = Invoke-CodexPluginCli @("plugin", "marketplace", "add", $PriorMarketplaceSource, "--json")
          if ($RestoreMarketplace.ExitCode -ne 0) {
            Write-Warning "Plugin rollback could not restore the prior marketplace source."
            $RollbackComplete = $false
          }
        }
        if ($PriorPlugin) {
          $RestorePlugin = Invoke-CodexPluginCli @("plugin", "add", $PluginSelector, "--json")
          if ($RestorePlugin.ExitCode -ne 0) {
            Write-Warning "Plugin rollback could not reinstall the prior plugin selector."
            $RollbackComplete = $false
          } elseif ($PriorPluginEnabledKnown -and -not $PriorPluginEnabled) {
            $RestoreDisabledState = Invoke-CodexPluginCli @("plugin", "disable", $PluginSelector, "--json")
            if ($RestoreDisabledState.ExitCode -ne 0) {
              Write-Warning "Plugin rollback could not restore the prior disabled state."
              $RollbackComplete = $false
            }
          }
          $RestoreList = Invoke-CodexPluginCli @("plugin", "list", "--json")
          try { $RestoreInventory = (($RestoreList.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop) }
          catch { $RestoreInventory = $null }
          $RestoredMatches = @()
          if ($RestoreInventory) {
            $RestoredMatches = @($RestoreInventory.installed | Where-Object {
              [string]$_.pluginId -eq $PluginSelector -and $_.installed -eq $true
            })
          }
          if ($RestoreList.ExitCode -ne 0 -or $RestoredMatches.Count -ne 1) {
            Write-Warning "Plugin rollback could not read exactly one restored prior plugin (exit=$($RestoreList.ExitCode), matches=$($RestoredMatches.Count))."
            $RollbackComplete = $false
          } else {
            $RestoredPlugin = $RestoredMatches[0]
            $RestoredVersionMatches = [string]$RestoredPlugin.version -eq $PriorPluginVersion
            $RestoredEnabledMatches = (-not $PriorPluginEnabledKnown) -or ([bool]$RestoredPlugin.enabled -eq $PriorPluginEnabled)
            $RestoredSourceMatches = Test-SamePath (Get-PluginMarketplaceSource $RestoredPlugin.marketplaceSource) $PriorMarketplaceSource
            if (-not $RestoredVersionMatches -or -not $RestoredEnabledMatches -or -not $RestoredSourceMatches) {
              Write-Warning "Plugin rollback verification mismatch: version=$RestoredVersionMatches enabled=$RestoredEnabledMatches marketplace_source=$RestoredSourceMatches"
              $RollbackComplete = $false
            }
          }
        } else {
          $RestoreList = Invoke-CodexPluginCli @("plugin", "list", "--json")
          try { $RestoreInventory = (($RestoreList.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop) }
          catch { $RestoreInventory = $null }
          $UnexpectedRestoredPlugin = @()
          if ($RestoreInventory) {
            $UnexpectedRestoredPlugin = @($RestoreInventory.installed | Where-Object {
              [string]$_.pluginId -eq $PluginSelector -and $_.installed -eq $true
            })
          }
          if ($RestoreList.ExitCode -ne 0 -or -not $RestoreInventory -or $UnexpectedRestoredPlugin.Count -ne 0) {
            $RollbackComplete = $false
          }
          $RestoreMarketplaceList = Invoke-CodexPluginCli @("plugin", "marketplace", "list", "--json")
          try { $RestoreMarketplaceInventory = (($RestoreMarketplaceList.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop) }
          catch { $RestoreMarketplaceInventory = $null }
          $RestoredMarketplaceMatches = @()
          if ($RestoreMarketplaceInventory) {
            $RestoredMarketplaceMatches = @($RestoreMarketplaceInventory.marketplaces | Where-Object {
              [string]$_.name -eq $PluginMarketplaceName
            })
          }
          if ($PriorMarketplace) {
            $ExactMarketplaceMatches = @($RestoredMarketplaceMatches | Where-Object {
              (Test-SamePath (Get-PluginMarketplaceSource $_) $PriorMarketplaceSource)
            })
            if ($RestoreMarketplaceList.ExitCode -ne 0 -or $ExactMarketplaceMatches.Count -ne 1) { $RollbackComplete = $false }
          } elseif ($RestoreMarketplaceList.ExitCode -ne 0 -or -not $RestoreMarketplaceInventory -or $RestoredMarketplaceMatches.Count -ne 0) {
            $RollbackComplete = $false
          }
        }
      } catch {
        $RollbackComplete = $false
        Write-Warning "Plugin rollback raised an error: $($_.Exception.Message)"
      }
    }
    if ($ExternalPluginStateMutated -and -not $RollbackComplete) {
      Write-Warning "Plugin rollback did not reproduce the complete prior plugin and marketplace inventory."
    }
    Update-BundleStatus @{
      installation_attempt_id = $InstallationAttemptId
      installation_state = $(if ($ExternalPluginStateMutated -and $RollbackComplete) { "failed_rolled_back" } elseif ($ExternalPluginStateMutated) { "failed_rollback_incomplete" } else { "failed_no_external_change" })
      connection_state = "failed"
      plugin_install_command_succeeded = $false
      plugin_discoverable = $false
      plugin_loader_verified = $false
      plugin_registered = $false
      plugin_stdio_verified = $false
      generated_client_configs_transport_verified = $false
      transport_end_to_end_verified = $false
      desktop_tool_scan_verified = $false
      conversation_attachment_verified = $false
      end_to_end_verified = $false
      plugin_rollback_performed = [bool]$ExternalPluginStateMutated
      plugin_rollback_complete = $(if ($ExternalPluginStateMutated) { [bool]$RollbackComplete } else { $null })
    }
    if ($ExternalPluginStateMutated -and -not $RollbackComplete) {
      throw "Plugin installation failed and the prior plugin state could not be restored completely. Original error: $($PluginInstallError.Exception.Message)"
    }
    if ($ExternalPluginStateMutated -and $RollbackComplete) {
      Fail-ClientConnectionAttempt "plugin_install_failed_prior_state_restored" -RolledBack
    } else {
      Fail-ClientConnectionAttempt "plugin_install_failed_prior_state_preserved"
    }
    throw $PluginInstallError
  } finally {
    if ($InstallLockAcquired) {
      $InstallMutex.ReleaseMutex()
    }
    $InstallMutex.Dispose()
  }
}

function Show-ChatGptHttps {
  Show-Header
  Warn-IfCoreCommandsMissing | Out-Null
  $Connector = Read-JsonFile "chatgpt_connector.json"
  if (-not $Connector.connector_url) {
    throw "No ChatGPT remote connector_url is ready. Regenerate with --public-url https://your-host.example/mcp, or use -Target chatgpt-tunnel for Secure MCP Tunnel."
  }
  if (-not $Connector.ready) {
    throw "Direct ChatGPT HTTPS is not app-ready. Configure and test MCP OAuth 2.1, regenerate with --chatgpt-oauth-ready, or use -Target chatgpt-tunnel. A static MCP_AUTH_TOKEN cannot be entered into ChatGPT."
  }
  Write-Host "ChatGPT connector URL:"
  Write-Host "  $($Connector.connector_url)"
  if (Get-Command Set-Clipboard -ErrorAction SilentlyContinue) {
    $Connector.connector_url | Set-Clipboard
    Write-Host "The connector URL was copied to the clipboard."
  }
  Write-Host ""
  Write-Host "Start the data-only MCP server with:"
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f (BundlePath "run_chatgpt_data_server.ps1"))
  Write-Host ""
  Write-Host "Then enable Developer mode in ChatGPT Settings > Security and login, and open Settings > Plugins or https://chatgpt.com/plugins."
  Write-Host "Create or refresh the developer-mode app with the connector URL and verify the discovered tools include search/fetch."
  Write-Host "MCP OAuth 2.1 must be available at the public endpoint. MCP_AUTH_TOKEN is only a gateway-to-origin secret and is never entered into ChatGPT."
  Write-Host "Validate the deployed endpoint with:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$((BundlePath 'validate_chatgpt_remote_mcp.ps1'))`""
  Write-Host "Open a new ChatGPT conversation, then select $ServerName from + > More."
  Write-Host "Verification prompt: $ServerName MCP의 search 도구로 인사규정을 찾고, 반환된 첫 번째 id를 fetch 도구로 조회해 조문 원문과 출처를 보여줘."
  Start-Process "https://chatgpt.com"
}

function Show-ChatGptTunnel {
  Show-Header
  if (-not (Warn-IfCoreCommandsMissing)) {
    return
  }
  $TunnelScriptPath = BundlePath "run_openai_secure_tunnel.ps1"
  Write-Host "OpenAI Secure MCP Tunnel script:"
  Write-Host "  $TunnelScriptPath"
  Write-Host "Set CONTROL_PLANE_API_KEY and OPENAI_TUNNEL_ID in the approved runtime environment before running it."
  Write-Host "Running tunnel script..."
  Run-Script "run_openai_secure_tunnel.ps1"
  Write-Host "Enable Developer mode in ChatGPT Settings > Security and login, then open Settings > Plugins or https://chatgpt.com/plugins."
  Write-Host "Create the app with +, choose Tunnel under Connection, and select the approved tunnel_id."
  Write-Host "In a new conversation, select the app from + > More and verify search/fetch."
}

function Show-ClaudeApi {
  Show-Header
  Warn-IfCoreCommandsMissing | Out-Null
  $Fragment = Read-JsonFile "claude_api_fragment.json"
  Write-Host "Claude API MCP server URL:"
  if ($Fragment.mcp_servers -and $Fragment.mcp_servers.Count -gt 0) {
    Write-Host "  $($Fragment.mcp_servers[0].url)"
  } else {
    throw "No Claude HTTPS MCP URL is ready. Regenerate the bundle with --public-url https://your-host.example/mcp."
  }
  Write-Host "For the Claude app, register only this URL under Customize > Connectors, then enable it from + > Connectors in a conversation."
  Write-Host "This command does not modify the Claude app connector settings."
  Write-Host "Copy mcp_servers, tools, and betas from claude_api_fragment.json into the Messages API request."
}

function Show-Menu {
  Show-Header
  Write-Host "Choose a target:"
  Write-Host "  0. Install/check local package commands"
  Write-Host "  1. Claude Code local stdio"
  Write-Host "  2. Codex CLI local stdio"
  Write-Host "  3. Claude Desktop local stdio"
  Write-Host "  4. ChatGPT Desktop local MCP (Settings-compatible stdio)"
  Write-Host "  5. ChatGPT remote MCP (HTTPS)"
  Write-Host "  6. ChatGPT web (ChatGPT Secure MCP Tunnel)"
  Write-Host "  7. Claude HTTPS (app connector URL / Messages API fragment)"
  Write-Host "  8. Doctor/readiness check"
  $Choice = Read-Host "Target"
  switch ($Choice) {
    "0" { Invoke-WithLocalConnectionFlow { Install-LocalPackage } }
    "1" { Invoke-WithLocalConnectionFlow { Register-ClaudeCode } }
    "2" { Invoke-WithLocalConnectionFlow { Show-Codex } }
    "3" { Invoke-WithLocalConnectionFlow { Show-ClaudeDesktop } }
    "4" { Invoke-WithLocalConnectionFlow { Show-Codex -ForChatGptDesktop } }
    "5" { Show-ChatGptHttps }
    "6" { Show-ChatGptTunnel }
    "7" { Show-ClaudeApi }
    "8" { Run-Doctor }
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
    "chatgpt-desktop-direct" { Show-Codex -ForChatGptDesktop }
    "chatgpt-desktop-local" { Show-ChatGptDesktop }
    "chatgpt-remote" { Show-ChatGptHttps }
    "chatgpt-desktop" { Show-Codex -ForChatGptDesktop }
    "chatgpt-https" { Show-ChatGptHttps }
    "chatgpt-tunnel" { Show-ChatGptTunnel }
    "claude-api" { Show-ClaudeApi }
    "doctor" { Run-Doctor }
  }
}

$LocalConnectionTargets = @("install", "claude-desktop", "claude-code", "codex", "chatgpt-desktop-direct", "chatgpt-desktop-local", "chatgpt-desktop")
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
  # Remote/tunnel commands can be long-lived. Serialize an explicitly
  # requested package install, then release the local mutation lock before
  # starting the server or tunnel process.
  if ($InstallPackage) {
    Invoke-WithLocalConnectionFlow { Install-PackageIfRequested }
  }
  Invoke-SelectedTarget
}
'''
    return (
        script.replace("__SERVER_NAME__", server_name)
        .replace("__PLUGIN_NAME__", plugin_name)
        .replace("__PLUGIN_MARKETPLACE_NAME__", marketplace_name)
        .replace("__PLUGIN_TEMPLATE_REVISION__", CHATGPT_DESKTOP_PLUGIN_TEMPLATE_REVISION)
        .replace("__EMBEDDED_CLAUDE_DESKTOP_CONFIG_BASE64__", embedded_config_base64)
        .replace("__EMBEDDED_CHATGPT_DESKTOP_CONFIG_BASE64__", embedded_config_base64)
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
    chatgpt_desktop_config: dict[str, Any],
) -> str:
    """Bind each generated installer's fallback to its own client config."""

    rendered = script
    for variable_name, config in (
        ("EmbeddedClaudeDesktopConfigBase64", claude_desktop_config),
        ("EmbeddedChatGptDesktopConfigBase64", chatgpt_desktop_config),
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
    claude_api: dict[str, Any],
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
    http_args = _server_args(
        data_dir=script_data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        transport="streamable-http",
        actor=actor,
        role=role,
        department_ids=department_ids,
        tenant_storage_isolation=tenant_storage_isolation,
    ) + ["--host", host, "--port", str(int(port))] + _http_auth_args(remote_auth_token_env) + _auth_issuer_args(
        chatgpt_remote.get("connector_url")
    )
    http_args = _with_no_warm_cache(http_args)
    chatgpt_http_args = (
        _server_args(
            data_dir=script_data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            transport="streamable-http",
            actor=actor,
            role=role,
            department_ids=department_ids,
            tenant_storage_isolation=tenant_storage_isolation,
            # ChatGPT is an external cloud client.  Keep its tool surface and
            # citation metadata on the explicit privacy-reduced profile; the
            # local/full HTTP command above remains available for approved
            # operator-only deployments.
            tool_profile="chatgpt-data",
        )
        + ["--host", host, "--port", str(int(port))]
        + _http_auth_args(remote_auth_token_env)
        + _auth_issuer_args(chatgpt_remote.get("connector_url"))
    )
    chatgpt_http_args = _with_no_warm_cache(chatgpt_http_args)
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
    http_doctor_args = [
        "--client-profile",
        "bundle",
        "--transport",
        "streamable-http",
        "--host",
        host,
        "--data-dir",
        script_data_dir,
        "--fail-on-warning",
    ]
    if remote_auth_token_env:
        http_doctor_args.extend(["--token-env", remote_auth_token_env])
    if chatgpt_remote["connector_url"]:
        http_doctor_args.extend(["--public-url", chatgpt_remote["connector_url"]])
    http_doctor_args.extend(_doctor_index_visibility_args(tenant_id, tenant_storage_isolation, min_visible_records))
    chatgpt_doctor_args = [
        "--client-profile",
        "chatgpt-remote",
        "--transport",
        "streamable-http",
        "--host",
        host,
        "--data-dir",
        script_data_dir,
        "--fail-on-warning",
    ]
    if remote_auth_token_env:
        chatgpt_doctor_args.extend(["--token-env", remote_auth_token_env])
    if chatgpt_remote["connector_url"]:
        chatgpt_doctor_args.extend(["--public-url", chatgpt_remote["connector_url"]])
    chatgpt_doctor_args.extend(_doctor_index_visibility_args(tenant_id, tenant_storage_isolation, min_visible_records))
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
    run_http_server_ps = _powershell_http_command(
        "reg-rag-mcp-server",
        http_args,
        remote_auth_token_env,
        doctor_args=http_doctor_args,
    )
    run_chatgpt_http_server_ps = _powershell_http_command(
        "reg-rag-mcp-server",
        chatgpt_http_args,
        remote_auth_token_env,
        doctor_args=chatgpt_doctor_args,
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
        claude_code_http_args = ["mcp", "add", "--transport", "http", server_name, chatgpt_remote["connector_url"]]
        if remote_auth_token_env:
            claude_code_http_args.extend(
                ["--header", "Authorization: Bearer ${" + remote_auth_token_env + "}"]
            )
        claude_code_http_ps = _powershell_command("claude", claude_code_http_args)
    openai_tunnel = _openai_secure_tunnel_config(
        server_name=server_name,
        data_dir=script_data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        actor=actor,
        role=role,
        department_ids=department_ids,
        tenant_storage_isolation=tenant_storage_isolation,
        min_visible_records=min_visible_records,
    )
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
                "the real tenant DB. External ChatGPT connectors use chatgpt-data and should validate search and fetch."
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
        "chatgpt_desktop_local": {
            "profile": "chatgpt-desktop-local",
            "tool_profile": "chatgpt-data",
            "transport": "stdio",
            "primary_registration": "chatgpt_desktop_settings_mcp_servers",
            "connection_configuration_method": "direct_config",
            "connection_prompt_required": False,
            "secret_input_policy": "environment_or_oauth_only",
            "optional_plugin_distribution": "generated_local_marketplace",
            "server": chatgpt_desktop_local,
            "conversation_attachment_unverified": True,
            "verification_tools": ["search", "fetch"],
            "verification_prompt": (
                f"{server_name} MCP의 search 도구로 인사규정을 찾고 첫 번째 id를 "
                "fetch로 조회해 원문과 출처를 보여줘."
            ),
        },
        "run_http_server": {
            "command": "reg-rag-mcp-server",
            "args": http_args,
            "url": chatgpt_remote["connector_url"],
            "auth": _remote_auth_summary(remote_auth_token_env),
        },
        "run_chatgpt_data_server": {
            "command": "reg-rag-mcp-server",
            "args": chatgpt_http_args,
            "url": chatgpt_remote["connector_url"],
            "tool_profile": "chatgpt-data",
            "auth": chatgpt_remote["server_auth"],
        },
        "claude_desktop": {
            "paste_json_section": "claude_desktop.mcpServers",
            "config_file_candidates": [
                "%APPDATA%\\Claude\\claude_desktop_config.json",
                "~/Library/Application Support/Claude/claude_desktop_config.json",
            ],
        },
        "claude_code": {
            "command": "claude",
            "args": claude_code_cli_args,
        },
        "chatgpt_remote": {
            "profile": "chatgpt-remote",
            "setup": chatgpt_remote["chatgpt_setup"]["location"],
            "connector_url": chatgpt_remote["connector_url"],
            "requires_reachable_https": chatgpt_remote["chatgpt_setup"]["requires_reachable_https"],
            "https_endpoint_ready": chatgpt_remote["chatgpt_setup"]["https_endpoint_ready"],
            "oauth_ready": chatgpt_remote["chatgpt_setup"]["oauth_ready"],
            "configuration_ready": chatgpt_remote["configuration_ready"],
            "verification_tools": ["search", "fetch"],
            "tool_profile": "chatgpt-data",
            "auth_required": True,
            "custom_static_bearer_supported": False,
            "connection_options": ["https_endpoint", "openai_secure_tunnel"],
        },
        "openai_secure_tunnel": openai_tunnel,
        "claude_api": {
            "copy_fields": ["mcp_servers", "tools", "betas"],
            "mcp_server_url": claude_api["mcp_servers"][0]["url"] if claude_api["mcp_servers"] else None,
            "authorization_token_env": remote_auth_token_env,
        },
        "warnings": _quickstart_warnings(host=host, chatgpt=chatgpt_remote, claude_api=claude_api),
        "copy_paste": {
            "validate_synthetic_chain_ps": _powershell_command("reg-rag-mcp-smoke", ["--fail-on-issue"]),
            "validate_runtime_transport_ps": validate_runtime_transport_ps,
            "validate_client_config_smoke_ps": validate_client_config_smoke_ps,
            "audit_index_visibility_ps": _powershell_command("reg-rag-mcp-index-visibility", index_visibility_args),
            "run_local_stdio_server_ps": run_local_stdio_server_ps,
            "run_http_server_ps": run_http_server_ps,
            "run_chatgpt_data_server_ps": run_chatgpt_http_server_ps,
            "claude_code_stdio_ps": claude_code_stdio_ps,
            "claude_code_http_ps": claude_code_http_ps,
            "openai_secure_tunnel_ps": openai_tunnel.get("copy_paste_ps"),
            "doctor_ps": _powershell_doctor_bundle_script(doctor_args),
            "connect_wizard_ps": _connect_wizard_script(
                server_name=server_name,
                local_stdio_server_args=stdio_args,
                local_stdio_doctor_args=stdio_doctor_args,
            ),
            "chatgpt_connector_url": chatgpt_remote["connector_url"],
            "claude_api_mcp_server_url": claude_api["mcp_servers"][0]["url"] if claude_api["mcp_servers"] else None,
        },
    }


def _quickstart_warnings(*, host: str, chatgpt: dict[str, Any], claude_api: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    has_remote_url = bool(chatgpt.get("connector_url") or (claude_api.get("mcp_servers") or []))
    if has_remote_url and host in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        warnings.append(
            "A remote HTTPS URL is configured, but the generated HTTP server command binds to loopback. "
            "Use --host 0.0.0.0 or an approved reverse proxy on the same host for remote clients."
        )
    return warnings


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
    oauth_ready = bool(chatgpt_oauth_ready)
    missing = []
    if not connector_url:
        missing.append("public_url_https_mcp_endpoint")
    elif not https_endpoint_ready:
        missing.append("public_url_must_use_https")
    if not oauth_ready:
        missing.append("chatgpt_mcp_oauth_2_1_not_attested")
    return {
        "profile": "chatgpt-remote",
        "transport": "streamable-http",
        "connector_name": server_name,
        "connector_url": connector_url,
        "ready": bool(https_endpoint_ready and oauth_ready),
        "configuration_ready": bool(https_endpoint_ready and oauth_ready),
        "remote_endpoint_verified": False,
        "tool_scan_unverified": True,
        "conversation_attachment_unverified": True,
        "end_to_end_verified": False,
        "missing": missing,
        "chatgpt_setup": {
            "location": "ChatGPT Settings > Security and login (Developer mode), then Settings > Plugins or https://chatgpt.com/plugins",
            "connector_url": connector_url,
            "requires_reachable_https": True,
            "https_endpoint_ready": https_endpoint_ready,
            "authentication_mode": "mcp-oauth-2.1",
            "oauth_ready": oauth_ready,
            "recommended_description": (
                "Search and fetch approved local regulation evidence from the institution's MCP server."
            ),
            "authentication_required": True,
            "authentication_note": (
                "ChatGPT cannot present a custom API key or operator-provided static bearer token. "
                "The public endpoint must implement MCP OAuth 2.1, or use Secure MCP Tunnel instead. "
                "A backend bearer token may protect the origin behind an OAuth-aware gateway."
            ),
        },
        "server_start": {
            "command": "reg-rag-mcp-server",
            "args": _with_no_warm_cache(
                _server_args(
                    data_dir=data_dir,
                    tenant_id=tenant_id,
                    profile_id=profile_id,
                    transport="streamable-http",
                    actor=actor,
                    role=role,
                    department_ids=department_ids,
                    tenant_storage_isolation=tenant_storage_isolation,
                    tool_profile="chatgpt-data",
                )
                + ["--host", host, "--port", str(int(port))]
                + _http_auth_args(remote_auth_token_env)
                + _auth_issuer_args(public_url)
            ),
        },
        "openai_secure_tunnel": _openai_secure_tunnel_config(
            server_name=server_name,
            data_dir=data_dir,
            tenant_id=tenant_id,
            profile_id=profile_id,
            actor=actor,
            role=role,
            department_ids=department_ids,
            tenant_storage_isolation=tenant_storage_isolation,
            min_visible_records=min_visible_records,
        ),
        "server_auth": {
            "required": True,
            "mode": "mcp-oauth-2.1",
            "oauth_ready": oauth_ready,
            "backend_token_env": remote_auth_token_env,
            "custom_static_bearer_supported_by_chatgpt": False,
            "note": (
                "Use MCP OAuth 2.1 at the public endpoint. The backend token is only for an "
                "OAuth-aware gateway-to-origin hop and is not entered into ChatGPT."
            ),
        },
        "compatible_tools": [
            "search",
            "fetch",
        ],
        "connection_steps": [
            "Run the HTTP MCP server from server_start.",
            "Set the bearer token environment variable or use an approved authenticated reverse proxy.",
            "Expose the /mcp endpoint through an approved HTTPS URL.",
            "Complete MCP OAuth 2.1 discovery, PKCE, audience, scope, and callback validation before attesting --chatgpt-oauth-ready.",
            "Enable Developer mode in ChatGPT Settings > Security and login.",
            "Create a developer-mode app from Settings > Plugins and enter connector_url as the MCP server URL.",
            "Verify the discovered tool list includes search and fetch before using the app.",
            "Select the app from + > More in a new chat, then ask ChatGPT to search first and fetch returned result IDs for evidence.",
        ],
        "notes": [
            "ChatGPT cannot connect directly to a local MCP server; use reachable HTTPS or Secure MCP Tunnel.",
            "ChatGPT does not support custom API keys; a static MCP_AUTH_TOKEN alone cannot authenticate a ChatGPT developer-mode app.",
            "The chatgpt-data profile uses the exact search(query) and fetch(id) input signatures required for data-source compatibility.",
            "Citation URLs are absolute user-openable HTTP(S) source URLs or empty when no such source exists.",
            "Do not expose streamable-http or SSE MCP without authentication or approved network controls.",
            "Use only public or separately approved data when routing MCP responses to an external cloud AI.",
        ],
    }


def _openai_secure_tunnel_config(
    *,
    server_name: str,
    data_dir: str,
    tenant_id: str,
    profile_id: str | None,
    actor: str | None,
    role: str | None,
    department_ids: list[str] | None,
    tenant_storage_isolation: bool,
    min_visible_records: int = 1,
) -> dict[str, Any]:
    profile = f"{_slug(server_name)}-chatgpt-remote"
    tunnel_id_env = "OPENAI_TUNNEL_ID"
    control_plane_api_key_env = "CONTROL_PLANE_API_KEY"
    stdio_args = _server_args(
        data_dir=data_dir,
        tenant_id=tenant_id,
        profile_id=profile_id,
        transport="stdio",
        actor=actor,
        role=role,
        department_ids=department_ids,
        tenant_storage_isolation=tenant_storage_isolation,
        # The tunnel terminates at ChatGPT, so it must use the same external
        # data profile as the HTTPS connector.
        tool_profile="chatgpt-data",
    )
    stdio_args = _with_no_warm_cache(stdio_args)
    mcp_command = _powershell_command("reg-rag-mcp-server", stdio_args)
    tunnel_data_env = "PRMCPBUILDER_TUNNEL_DATA_DIR"
    encoded_server_args = list(stdio_args)
    for index, value in enumerate(encoded_server_args[:-1]):
        if str(value) == "--data-dir":
            encoded_server_args[index + 1] = f"$env:{tunnel_data_env}"
    encoded_launcher_script = "\n".join(
        [
            '$ErrorActionPreference = "Stop"',
            f'$ServerArgs = {_powershell_array_literal(encoded_server_args)}',
            "& 'reg-rag-mcp-server' @ServerArgs",
            "exit $LASTEXITCODE",
        ]
    )
    encoded_launcher = base64.b64encode(encoded_launcher_script.encode("utf-16-le")).decode("ascii")
    init_args = [
        "init",
        "--sample",
        "sample_mcp_stdio_local",
        "--profile",
        profile,
        "--tunnel-id",
        f"$env:{tunnel_id_env}",
        "--mcp-command",
        "$McpCommand",
    ]
    doctor_args = ["doctor", "--profile", profile, "--explain"]
    run_args = ["run", "--profile", profile]
    readiness_args = [
        "--client-profile",
        "chatgpt-remote",
        "--connection-mode",
        "openai-tunnel",
        "--transport",
        "stdio",
        "--data-dir",
        data_dir,
        "--fail-on-warning",
    ]
    readiness_args.extend(_doctor_index_visibility_args(tenant_id, tenant_storage_isolation, min_visible_records))
    script_lines = ['$ErrorActionPreference = "Stop"']
    if data_dir == BUNDLE_DATA_DIR_ARG:
        script_lines.extend(_powershell_bundle_data_dir_lines())
    script_lines.extend(
        [
            (
                f'$env:{tunnel_data_env} = $BundleDataDir'
                if data_dir == BUNDLE_DATA_DIR_ARG
                else f'$env:{tunnel_data_env} = {_powershell_single_quoted_json(data_dir)}'
            ),
            f"$McpCommand = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded_launcher}'",
            'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
            'function Assert-EnvVar([string]$Name) { $Value = [Environment]::GetEnvironmentVariable($Name); if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "<*>") { throw "$Name must be set to an approved non-placeholder value before running this script." } }',
            'Assert-Command "reg-rag-mcp-doctor"',
            'Assert-Command "reg-rag-mcp-server"',
            'Assert-Command "tunnel-client"',
            f'Assert-EnvVar "{control_plane_api_key_env}"',
            f'Assert-EnvVar "{tunnel_id_env}"',
            _powershell_command("reg-rag-mcp-doctor", readiness_args),
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            _powershell_command(
                "tunnel-client",
                init_args,
                prequoted_indexes={len(init_args) - 1},
            ),
            _powershell_command("tunnel-client", doctor_args),
            _powershell_command("tunnel-client", run_args),
        ]
    )
    script = "\n".join(script_lines)
    return {
        "profile": profile,
        "recommended_when": "ChatGPT must reach a private or internal MCP server without opening inbound public firewall access.",
        "requires": [
            "OpenAI Platform tunnel_id",
            "runtime API key for tunnel-client",
            "tunnel-client installed on a host that can reach the local MCP server",
            "ChatGPT workspace/operator permission to use the tunnel",
        ],
        "tunnel_id_env": tunnel_id_env,
        "control_plane_api_key_env": control_plane_api_key_env,
        "setup_state": "manual_setup_required",
        "stdio_mcp_command": mcp_command,
        "commands": {
            "readiness": {"command": "reg-rag-mcp-doctor", "args": readiness_args},
            "init": {"command": "tunnel-client", "args": init_args},
            "doctor": {"command": "tunnel-client", "args": doctor_args},
            "run": {"command": "tunnel-client", "args": run_args},
        },
        "chatgpt_setup": [
            "Create or select an OpenAI Secure MCP Tunnel in Platform tunnel settings.",
            "Run this script inside the network that can reach the local regulation MCP data directory.",
            "Enable Developer mode in ChatGPT Settings > Security and login.",
            "Open Settings > Plugins or https://chatgpt.com/plugins, select +, choose Tunnel under Connection, and select the tunnel_id.",
            "Create or refresh the developer-mode tunnel app and verify the discovered privacy-reduced read-only tools include search and fetch.",
        ],
        "copy_paste_ps": script,
        "docs": [
            "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels",
            "https://developers.openai.com/apps-sdk/deploy/connect-chatgpt",
        ],
    }


def _claude_api_connector_config(
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
    mcp_servers = []
    if https_endpoint_ready:
        server_definition: dict[str, Any] = {
            "type": "url",
            "url": connector_url,
            "name": server_name,
        }
        mcp_servers.append(server_definition)
    tools = (
        [
            {
                "type": "mcp_toolset",
                "mcp_server_name": server_name,
                "default_config": {"enabled": True},
            }
        ]
        if https_endpoint_ready
        else []
    )
    missing = []
    if not connector_url:
        missing.append("public_url_https_mcp_endpoint")
    elif not https_endpoint_ready:
        missing.append("public_url_must_use_https")
    return {
        "mcp_servers": mcp_servers,
        "tools": tools,
        "betas": ["mcp-client-2025-11-20"],
        "ready": https_endpoint_ready,
        "missing": missing,
        "connection_steps": [
            "Run the HTTP MCP server from server_start.",
            "Set the bearer token environment variable or use an approved authenticated reverse proxy.",
            "Expose the /mcp endpoint through an approved HTTPS URL.",
            "Copy mcp_servers, tools, and betas into the Claude Messages API request.",
            "If server_auth.token_env is set, read that environment variable and inject its value as authorization_token immediately before sending the request.",
        ],
        "server_start": {
            "command": "reg-rag-mcp-server",
            "args": _with_no_warm_cache(
                _server_args(
                    data_dir=data_dir,
                    tenant_id=tenant_id,
                    profile_id=profile_id,
                    transport="streamable-http",
                    actor=actor,
                    role=role,
                    department_ids=department_ids,
                    tenant_storage_isolation=tenant_storage_isolation,
                    tool_profile="chatgpt-data",
                )
                + ["--host", host, "--port", str(int(port))]
                + _http_auth_args(remote_auth_token_env)
                + _auth_issuer_args(public_url)
            ),
        },
        "server_auth": _remote_auth_summary(remote_auth_token_env),
        "notes": [
            "Claude Messages API MCP connector requires an HTTPS URL server definition.",
            "Do not expose streamable-http or SSE MCP without authentication or approved network controls.",
            "Do not store the token or its environment-variable name as a non-schema field inside mcp_servers.",
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
    return {
        "required": True,
        "token_env": remote_auth_token_env,
        "note": "Use bearer token auth or an approved authenticated reverse proxy before exposing HTTP/SSE MCP.",
    }


def _setup_bundle_readme(*, config: dict[str, Any], files: dict[str, str], server_name: str) -> str:
    chatgpt_ready = bool((config.get("chatgpt_remote") or config.get("chatgpt") or {}).get("ready"))
    claude_api_ready = bool((config.get("claude_api") or {}).get("ready"))
    quickstart = config.get("quickstart") if isinstance(config.get("quickstart"), dict) else {}
    warnings = quickstart.get("warnings") if isinstance(quickstart.get("warnings"), list) else []
    warning_block = "\n".join(f"- {warning}" for warning in warnings) if warnings else "- None."
    audit_index_command = _portable_bundle_doc_command(
        quickstart.get("copy_paste", {}).get(
            "audit_index_visibility_ps",
            "reg-rag-mcp-index-visibility --data-dir .\\data --tenant-id <tenant> --fail-on-issue",
        )
    )
    connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | {str(item['ready']).lower()} | `{item['primary_file']}` |"
        for item in _setup_bundle_connections(config)
    )
    return f"""# MCP Connection Bundle

This folder contains generated setup files for the `{server_name}` MCP server.

## Fast Path

For ChatGPT Desktop, use the program's generated-result code box for `{files.get("chatgpt_desktop_agent_prompt", SETUP_BUNDLE_FILES["chatgpt_desktop_agent_prompt"])}` and enter its materialized Name, STDIO command, working directory, and arguments in `Settings > MCP servers > Add server`. Do not copy a literal `<PROGRAM_BUNDLE_DIR>` from the portable file stored in the ZIP. Save, restart ChatGPT Desktop, run `/mcp` in a new conversation, then verify `search` followed by `fetch`. Use the ChatGPT Desktop BAT only when manual entry is impractical or the advanced shared `~/.codex/config.toml` path is required. The BAT cannot enable MCP in a Desktop build or workspace that does not expose the feature. Repeated `@` mentions do not install or verify an MCP server.

For Codex CLI, run `{files.get("connect_codex_bat", SETUP_BUNDLE_FILES["connect_codex_bat"])}` or apply `{files.get("codex_config", SETUP_BUNDLE_FILES["codex_config"])}` directly to `~/.codex/config.toml`, restart Codex, verify `/mcp`, then call `search` and `fetch`. `{files.get("codex_agent_prompt", SETUP_BUNDLE_FILES["codex_agent_prompt"])}` is optional local automation only; it is not a required installation prompt.

For Claude Code, open this bundle as its local workspace and use `{files.get("claude_code_agent_prompt", SETUP_BUNDLE_FILES["claude_code_agent_prompt"])}` or its BAT, then restart and verify `/mcp` plus `get_index_status`.

Never paste connection configuration, local paths, tokens, API keys, or tunnel IDs into a chat prompt. Keep secrets in approved environment variables or OAuth only.

Claude Desktop follows a separate path: double-click `{files.get("connect_claude_desktop_bat", SETUP_BUNDLE_FILES["connect_claude_desktop_bat"])}`. The BAT backs up and merges the user config, then verifies initialize, tools/list, and get_index_status from that exact installed config. This does not prove Desktop loader or conversation exposure. Fully quit and restart Claude Desktop, confirm the server in Connectors, and invoke get_index_status in a new conversation. Do not apply the `/mcp` step above to Claude Desktop.

Use `{files.get("doctor_bat", SETUP_BUNDLE_FILES["doctor_bat"])}` for bundle and installed-config preflight diagnostics.
That diagnostic does not verify client registration, loader recognition, or a tool call in the current conversation.
Use `{files.get("usage_guide_bat", SETUP_BUNDLE_FILES["usage_guide_bat"])}` for client-specific verification commands and named invocation examples.
The ChatGPT Desktop BAT is a fallback that installs the local MCP entry read by ChatGPT Desktop.
The generated plugin marketplace remains an optional package for Work/Codex plugin distribution, not the default connection path.
The `.bat` files are thin double-click launchers around the generated PowerShell scripts.
If you move or rename this folder, rerun the connection button from the new location so the client config is
updated to the new launcher and `data` paths.
Regenerating and reconnecting with the same MCP name replaces the existing client entry. The regenerated bundle contains
the current approved corpus, so added and revised chunks remain available through the same MCP name.

Run `{files.get("connect", SETUP_BUNDLE_FILES["connect"])}` and choose doctor first, then Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop, ChatGPT remote MCP,
ChatGPT web (Secure MCP Tunnel), Claude HTTPS MCP, or doctor. For non-interactive setup, pass `-InstallPackage -Target claude-code`,
`-InstallPackage -Target codex -InstallCodex`, optional `-InstallPackage -Target chatgpt-desktop-local -InstallChatGptDesktopPlugin`, `-Target chatgpt-remote`,
`-Target chatgpt-tunnel`, `-Target claude-api`, or
`-InstallPackage -Target claude-desktop -InstallClaudeDesktop`. Use `-Target claude-desktop -ValidateClaudeDesktop`
first when Claude Desktop reports a JSON parsing error.

Check `{files.get("bundle_status", SETUP_BUNDLE_FILES["bundle_status"])}` first when a client appears slow to recognize the MCP.
It is regenerated from `data/mcp_runtime_manifest.json` and shows the current approved record count and `recommended_smoke_query`.
`plugin_registered=true` requires strict companion JSON validation, a successful install command, an enabled exact-version/source entry in `codex plugin list --json`, validation of the installed plugin cache, and a matching `codex mcp get --json` result. It still does not mean the current conversation attached the plugin. `direct_stdio_verified` and `transport_end_to_end_verified` record the generated launcher's MCP chain, while `desktop_tool_scan_verified`, `conversation_attachment_verified`, and Desktop `end_to_end_verified` remain false until verified in the product surface.
Older doctor, transport, client-config, remote, and Codex app-server run reports are cleared on generation so stale evidence does not
look like the current bundle state.
After installing or merging a client config, rerun doctor with the installed config path when a client still opens the old runtime:
`reg-rag-mcp-doctor --client-profile bundle --bundle-dir . --allow-local-only-bundle --codex-config $HOME\\.codex\\config.toml`
or add `--claude-desktop-config "$env:APPDATA\\Claude\\claude_desktop_config.json"` on Windows. This catches stale
`--data-dir`, missing `--no-warm-cache`, and storage-mode flag mismatches.

Run these scripts from a shell where the package console commands are installed. If they are missing, run
`{files.get("install", SETUP_BUNDLE_FILES["install"])}` first. It runs `pip install -e .` when the bundle is inside
the repository, installs a bundled `reg_rag_preprocessor-*.whl` when present outside the repository, or accepts
`-PackagePath path\\to\\reg_rag_preprocessor*.whl` for a separate wheel handoff. Generate a self-contained zip with
`reg-rag-mcp-config --client-profile bundle --include-wheel --zip-out ...` after `python -m build --sdist --wheel`.

## Connection Matrix

| Client | Mode | Setup artifact ready | Primary file |
| --- | --- | --- | --- |
{connection_rows}

`Setup artifact ready` only describes generated files or URL fields. It does not mean client registration, loader recognition, or a conversation tool call succeeded.

## Local Desktop and CLI

1. For Claude Desktop, double-click `{files.get("connect_claude_desktop_bat", SETUP_BUNDLE_FILES["connect_claude_desktop_bat"])}`. It runs the doctor before installation. Handoff-ZIP JSON/TOML files are portable templates: before any manual merge, replace every `<BUNDLE_DIR>` marker with the extracted bundle's absolute path (JSON paths require escaped backslashes). Then merge `{files.get("claude_desktop", SETUP_BUNDLE_FILES["claude_desktop"])}` into the
   Claude Desktop config file. The generated file already contains an `mcpServers` object.
   Run `{files.get("connect", SETUP_BUNDLE_FILES["connect"])}` with `-Target claude-desktop -ValidateClaudeDesktop`
   to validate the existing Claude Desktop JSON before merging. Automatic install runs the doctor gate before writing the config.
2. For Claude Code, open this bundle as its workspace and paste `{files.get("claude_code_agent_prompt", SETUP_BUNDLE_FILES["claude_code_agent_prompt"])}` into the agent. If it cannot execute locally, double-click `{files.get("connect_claude_code_bat", SETUP_BUNDLE_FILES["connect_claude_code_bat"])}`. For manual setup, run `{files.get("claude_code_stdio", SETUP_BUNDLE_FILES["claude_code_stdio"])}` in PowerShell.
   The script runs the doctor gate, replaces legacy local/user entries, registers the local stdio server with
   `--scope user`, and verifies it with `claude mcp get` so it remains available outside the bundle directory.
3. For ChatGPT Desktop, use the program's generated-result code box for `{files.get("chatgpt_desktop_agent_prompt", SETUP_BUNDLE_FILES["chatgpt_desktop_agent_prompt"])}` and enter its materialized fields in `Settings > MCP servers > Add server`. A literal `<PROGRAM_BUNDLE_DIR>` in the ZIP copy is not an input value. Save, fully quit and restart ChatGPT Desktop, open a new conversation, and run `/mcp`. Use `{files.get("connect_chatgpt_desktop_bat", SETUP_BUNDLE_FILES["connect_chatgpt_desktop_bat"])}` only when manual entry is impractical or the advanced shared `~/.codex/config.toml` path is required. A successful config write does not enable a missing Desktop feature or prove tool exposure.
   The generated plugin package follows the official `.codex-plugin/plugin.json` to `./.mcp.json` layout, but is optional and is not installed by the ChatGPT Desktop button.
   For direct Codex CLI compatibility, run `{files.get("connect_codex_bat", SETUP_BUNDLE_FILES["connect_codex_bat"])}`. For manual Codex setup, first materialize every `<BUNDLE_DIR>` marker to a forward-slash absolute path such as `C:/MCP/aksmcp2` (or escape every backslash for valid TOML), then add `{files.get("codex_config", SETUP_BUNDLE_FILES["codex_config"])}` to `$HOME\\.codex\\config.toml`
   or replace the existing `[mcp_servers.{server_name}]` block. The snippet points `--data-dir` at this bundle's
   `data` directory and includes `--no-warm-cache` plus the generated storage-mode flag. Local stdio client
   configs launch `{files.get("stdio_launcher", SETUP_BUNDLE_FILES["stdio_launcher"])}` through PowerShell instead
   of calling `reg-rag-mcp-server` directly. A successful package install writes `runtime_python.json` schema 2
   with the selected Python and SHA-256 identities for {len(RUNTIME_IDENTITY_MODULES)} MCP command modules. The launcher validates that
   identity with `PYTHONPATH` isolated and uses it before any source checkout, environment override, or PATH
   command. A damaged or drifted marker fails closed and asks for reinstall; fallback discovery is only used
   before a marker exists.
4. Validate generated Codex and Claude Desktop local stdio configs with `{files.get("client_config_smoke", SETUP_BUNDLE_FILES["client_config_smoke"])}`.
   It launches MCP through the exact generated `command`/`args` and completes `initialize`, `tools/list`,
   `get_index_status`, `search`, and `fetch`.
5. Validate the bundled runtime transport with `{files.get("validate", SETUP_BUNDLE_FILES["validate"])}`. It reads `data/mcp_runtime_manifest.json` and uses the generated `recommended_smoke_query` when present.
6. Real runtime visibility audit command used by the doctor gate:

```powershell
{audit_index_command}
```

## ChatGPT

The `chatgpt-desktop-local` profile provides the exact local STDIO fields for ChatGPT Desktop's built-in MCP server settings.
Fully restart the app and run `/mcp` in a new conversation; `@{server_name}` is not a connection check.
ChatGPT remote apps need a reachable HTTPS `/mcp` endpoint; ChatGPT does not directly connect to a localhost MCP endpoint.
Direct authenticated apps require MCP OAuth 2.1. ChatGPT cannot present a custom API key or a static
`MCP_AUTH_TOKEN`; that token is only suitable for an OAuth-aware gateway-to-origin hop. Generate a direct
profile with `--chatgpt-oauth-ready` only after discovery, PKCE, audience, scopes, and callback validation pass.
Use `{files.get("run_chatgpt", SETUP_BUNDLE_FILES["run_chatgpt"])}` on the server for the external `chatgpt-data` profile, then register the URL from
`{files.get("chatgpt", SETUP_BUNDLE_FILES["chatgpt"])}` after enabling Developer mode in ChatGPT Settings > Security and login, then create the developer-mode app from Settings > Plugins or https://chatgpt.com/plugins. ChatGPT web does not read local Codex `config.toml`; select the draft app from `+ > More` in a new chat. Reviewed marketplace distribution remains separate from a developer-mode draft app.

HTTPS configuration artifact ready: `{str(chatgpt_ready).lower()}`. This does not verify endpoint reachability, the ChatGPT tool scan, or conversation attachment. If false, finish OAuth and regenerate with `--public-url https://your-host.example/mcp --chatgpt-oauth-ready`, or use Secure MCP Tunnel.

For private or internal servers, use `{files.get("openai_tunnel", SETUP_BUNDLE_FILES["openai_tunnel"])}` as the
OpenAI Secure MCP Tunnel template. It keeps the MCP server inside the local network and lets ChatGPT select the
tunnel after enabling Developer mode in Settings > Security and login and creating the developer-mode app with
`+` under Settings > Plugins (or https://chatgpt.com/plugins).
Choose Tunnel under Connection and select the approved tunnel ID. This dedicated tunnel path is separate from
the public-HTTPS URL path in the same developer-app screen and from reviewed marketplace plugin installation.

## Claude HTTPS and Claude API

For the Claude app, register the reachable HTTPS MCP URL as a custom connector under Customize > Connectors, then enable it for a conversation from + > Connectors. Team and Enterprise organization registration is performed by an Owner under Organization settings > Connectors. Do not paste the API request fragment into the Claude connector UI.

Claude API needs an HTTPS URL MCP server definition. Copy `{files.get("claude_api", SETUP_BUNDLE_FILES["claude_api"])}` into
the Messages API request fields `mcp_servers`, `tools`, and `betas`.

Claude API request fragment ready: `{str(claude_api_ready).lower()}`. This does not verify endpoint reachability or a Messages API tool call. If false, regenerate with `--public-url https://your-host.example/mcp`.

## Korean Text Display

Bundle JSON and Markdown files are written as UTF-8. If Korean document names or chunk IDs look like `蹂꾪몴`
or replacement characters in Windows PowerShell, the file is usually being displayed as CP949 instead of UTF-8;
it is not evidence that the MCP data is corrupted. Inspect files with `Get-Content -Encoding UTF8 ...`,
`chcp 65001`, or a UTF-8-aware editor/browser/GitHub view. Do not regenerate data or change chunk IDs only for
this display symptom because approval journals and vector IDs are keyed by those IDs.

## Security

Do not expose HTTP MCP without authentication or approved network controls. Generated HTTP and tunnel scripts do
not store secrets. `MCP_AUTH_TOKEN` protects only a backend/origin hop and is not ChatGPT-facing authentication;
direct ChatGPT apps require OAuth 2.1. Set tunnel credentials only in the approved runtime environment. Generated
HTTP commands run `reg-rag-mcp-doctor --fail-on-warning` before starting the server.

## Warnings

{warning_block}

## Official References

- ChatGPT Desktop/Codex MCP: https://learn.chatgpt.com/docs/extend/mcp
- ChatGPT and Codex Plugins: https://help.openai.com/en/articles/20001256-plugins-in-codex
- ChatGPT developer mode and MCP apps: https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta
- ChatGPT MCP app authentication: https://developers.openai.com/apps-sdk/build/auth
- OpenAI Secure MCP Tunnel: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- MCP Streamable HTTP transport: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Claude API MCP connector: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
- Claude custom connectors: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp
"""



def _setup_bundle_readme_ko(*, config: dict[str, Any], files: dict[str, str], server_name: str) -> str:
    chatgpt_ready = bool((config.get("chatgpt_remote") or config.get("chatgpt") or {}).get("ready"))
    claude_api_ready = bool((config.get("claude_api") or {}).get("ready"))
    quickstart = config.get("quickstart") if isinstance(config.get("quickstart"), dict) else {}
    warnings = quickstart.get("warnings") if isinstance(quickstart.get("warnings"), list) else []
    warning_block = "\n".join(f"- {warning}" for warning in warnings) if warnings else "- 없음."
    audit_index_command = _portable_bundle_doc_command(
        quickstart.get("copy_paste", {}).get(
            "audit_index_visibility_ps",
            "reg-rag-mcp-index-visibility --data-dir .\\data --tenant-id <tenant> --fail-on-issue",
        )
    )
    connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | {str(item['ready']).lower()} | `{item['primary_file']}` |"
        for item in _setup_bundle_connections(config)
    )
    return f"""# MCP 연결 번들

이 폴더는 `{server_name}` MCP 서버를 ChatGPT Desktop 로컬 direct MCP, ChatGPT 원격 MCP, Codex CLI, Claude Desktop, Claude Code, Claude API에 연결하기 위한 생성 파일 묶음입니다.

## 가장 빠른 경로

ChatGPT Desktop은 프로그램 생성 결과 화면의 `{files.get('chatgpt_desktop_agent_prompt', SETUP_BUNDLE_FILES['chatgpt_desktop_agent_prompt'])}` 코드 상자에 표시된 Name·STDIO·Command·Working directory·Arguments를 `Settings > MCP servers > Add server`에 입력합니다. ZIP 원본에 `<PROGRAM_BUNDLE_DIR>`이 보이면 그대로 입력하지 않습니다. Save 후 앱을 완전히 재시작하고 새 대화에서 `/mcp`로 `{server_name}`을 확인한 뒤 `search`와 `fetch`를 차례로 호출합니다. 수동 입력이 어렵거나 고급 설정 파일 경로를 사용할 때만 Desktop 전용 BAT를 사용합니다. 이 BAT는 공유 `~/.codex/config.toml`을 백업·기록·검증하지만 Desktop에 없는 MCP 기능이나 메뉴를 활성화하지는 않습니다.

Codex CLI는 `{files.get('connect_codex_bat', SETUP_BUNDLE_FILES['connect_codex_bat'])}`를 실행하거나 `{files.get('codex_config', SETUP_BUNDLE_FILES['codex_config'])}`을 `~/.codex/config.toml`에 직접 반영합니다. Codex를 재시작한 뒤 `/mcp`와 실제 `search`·`fetch` 호출로 확인합니다. `{files.get('codex_agent_prompt', SETUP_BUNDLE_FILES['codex_agent_prompt'])}`는 로컬 파일·터미널 권한이 있는 에이전트용 선택적 자동화 자료이며 필수 설치 프롬프트가 아닙니다.

Claude Code는 압축을 푼 번들을 로컬 작업공간으로 열고 `{files.get('claude_code_agent_prompt', SETUP_BUNDLE_FILES['claude_code_agent_prompt'])}` 또는 전용 BAT로 등록한 뒤 `/mcp`와 `get_index_status`를 확인합니다.

연결 설정·로컬 경로·토큰·API 키·tunnel ID는 대화 프롬프트에 붙여넣지 않습니다. 비밀값은 승인된 환경변수 또는 OAuth에만 둡니다.

Claude Desktop은 별도 경로입니다. `{files.get('connect_claude_desktop_bat', SETUP_BUNDLE_FILES['connect_claude_desktop_bat'])}`를 더블클릭하면 사용자 설정을 백업·병합하고 그 설치 설정으로 initialize·tools/list·get_index_status까지 검증합니다. 이 성공은 Desktop 로더나 현재 대화 노출 성공이 아니므로 앱을 완전히 종료·재실행한 뒤 Connectors와 실제 도구 호출을 확인합니다. Claude Desktop에는 위 `/mcp` 공통 절차를 적용하지 않습니다.

번들·설정 사전 진단만 실행할 때는 `{files.get('doctor_bat', SETUP_BUNDLE_FILES['doctor_bat'])}`를 더블클릭합니다. 이 진단은 클라이언트 등록·로더 인식·현재 대화 도구 호출 성공을 뜻하지 않습니다.
클라이언트별 확인 명령과 이름 기반 호출 예시는 `{files.get('usage_guide_bat', SETUP_BUNDLE_FILES['usage_guide_bat'])}`를 실행해 확인합니다.
ChatGPT Desktop BAT는 수동 입력이 어렵거나 고급 설정 파일 경로가 필요할 때만 쓰는 보조 등록 수단입니다. 공유 `~/.codex/config.toml` 기록 성공은 Desktop 메뉴·도구 노출 성공과 다릅니다. 메뉴와 `/mcp`가 계속 보이지 않으면 앱 업데이트와 계정·워크스페이스 제공 여부를 확인한 뒤 원격 HTTPS MCP 또는 Secure MCP Tunnel을 사용합니다. 생성된 로컬 플러그인 마켓플레이스는 별도 플러그인 배포가 필요할 때만 쓰는 선택 산출물입니다.
이 `.bat` 파일들은 내부에서 생성된 PowerShell 스크립트를 대신 실행하는 안전한 연결 버튼입니다.
이 폴더를 이동하거나 이름을 바꿨다면 새 위치에서 연결 버튼을 다시 실행합니다. 그러면 AI 앱 설정의 실행 파일과 `data` 경로가 새 폴더 기준으로 교체됩니다.
같은 MCP 이름으로 다시 생성하고 연결 버튼을 실행하면 기존 설정을 중복 추가하지 않고 교체합니다. 새 번들은 현재 승인된 전체 corpus를 다시 만들기 때문에 추가·개정 청크가 같은 MCP 이름에 반영됩니다.

먼저 다음 명령에서 doctor를 실행해 실제 런타임 visibility gate를 확인한 뒤 연결할 클라이언트를 선택합니다.

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}"
```

클라이언트가 MCP를 늦게 인식하거나 엉뚱한 상태를 보여주면 먼저 `{files.get('bundle_status', SETUP_BUNDLE_FILES['bundle_status'])}`를 확인합니다.
이 파일은 `data/mcp_runtime_manifest.json` 기준으로 다시 생성되며 현재 승인 record 수와 `recommended_smoke_query`를 보여줍니다.
예전 doctor·transport·client-config·remote·Codex app-server 실행 보고서는 번들 생성 시 정리해서 현재 상태처럼 보이지 않게 합니다.
클라이언트 설정을 병합하거나 설치한 뒤에도 예전 런타임을 보는 것 같으면 설치된 설정 파일까지 doctor로 확인합니다.
예: `reg-rag-mcp-doctor --client-profile bundle --bundle-dir . --allow-local-only-bundle --codex-config $HOME\\.codex\\config.toml`
또는 Windows Claude Desktop은 `--claude-desktop-config "$env:APPDATA\\Claude\\claude_desktop_config.json"`를 추가합니다.
이 검사는 stale `--data-dir`, `--no-warm-cache` 누락, 저장소 모드 플래그 불일치를 잡습니다.

`reg-rag-mcp-*` 콘솔 명령이 보이지 않으면 먼저 설치 보조 스크립트를 실행합니다. 번들에 wheel 파일이 있으면 저장소 아래에 압축을 풀었더라도 항상 그 wheel을 우선 설치합니다. wheel이 없을 때만 상위 저장소의 `pyproject.toml`을 찾아 editable 설치를 사용하고, 그 경로도 없으면 `dist`의 wheel을 찾습니다.

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('install', SETUP_BUNDLE_FILES['install'])}"
```

저장소 없이 번들 하나만 전달해야 하면 `python -m build --sdist --wheel` 실행 후 `reg-rag-mcp-config --client-profile bundle --include-wheel --zip-out ...`로 wheel 포함 zip을 생성합니다.

비대화형 실행 예시:

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -InstallPackage -Target claude-code
powershell -NoProfile -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -InstallPackage -Target codex -InstallCodex
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -Target chatgpt-remote
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -Target chatgpt-tunnel
```

## 연결 선택지

| 클라이언트 | 방식 | 설정 산출물 준비 | 주요 파일 |
| --- | --- | --- | --- |
{connection_rows}

`설정 산출물 준비`는 생성 파일 또는 URL 필드의 준비 여부이며, 클라이언트 등록·로더 인식·현재 대화 도구 호출 성공을 뜻하지 않습니다.

## Claude 연결

- 사전 진단: `{files.get('doctor_bat', SETUP_BUNDLE_FILES['doctor_bat'])}`를 먼저 실행합니다. indexed record, smoke 문서 배제, append-only approval journal coverage가 통과해야 합니다.
- Claude Desktop: `{files.get('connect_claude_desktop_bat', SETUP_BUNDLE_FILES['connect_claude_desktop_bat'])}`를 더블클릭합니다. 배포 ZIP 안의 JSON/TOML은 `<BUNDLE_DIR>` 템플릿이므로 수동 설정 전에 모든 표시를 현재 압축 해제 폴더의 절대 경로로 바꿔야 합니다(JSON에서는 역슬래시를 이스케이프). 그 뒤에만 `{files.get('claude_desktop', SETUP_BUNDLE_FILES['claude_desktop'])}`의 `mcpServers`를 Claude Desktop 설정에 병합합니다. 자동 병합은 doctor gate를 통과한 뒤 `connect_mcp_client.ps1 -InstallPackage -Target claude-desktop -InstallClaudeDesktop`로 수행하며, 설치된 사용자 설정으로 initialize·tools/list·get_index_status를 검증합니다. 이 상태는 Desktop 로더·대화 노출 확인 대기입니다. JSON 파싱 오류가 났다면 먼저 `connect_mcp_client.ps1 -Target claude-desktop -ValidateClaudeDesktop`으로 기존 설정 파일을 검증합니다.
- Claude Code: `{files.get('claude_code_agent_prompt', SETUP_BUNDLE_FILES['claude_code_agent_prompt'])}`를 에이전트에 붙여넣는 방식을 우선 사용합니다. 보조 BAT `{files.get('connect_claude_code_bat', SETUP_BUNDLE_FILES['connect_claude_code_bat'])}`도 로컬 stdio MCP를 공식 사용자 범위(`--scope user`, 저장소 `~/.claude.json`)에 등록하고 `claude mcp get`으로 확인합니다. `~/.claude/settings.json`의 `enabledMcpjsonServers`는 이 user-scope 등록 목록이 아닙니다. 따라서 생성 폴더 밖의 다른 프로젝트에서도 같은 사용자에게 보입니다.
- Claude 앱 custom connector: 승인된 HTTPS MCP URL만 `Customize > Connectors`에 등록하고 대화의 `+` > `Connectors`에서 활성화합니다. Team/Enterprise 조직 등록은 Owner가 `Organization settings > Connectors`에서 수행합니다. 아래 API JSON 조각을 앱 화면에 붙여 넣지 않습니다.
- Claude API: `{files.get('claude_api', SETUP_BUNDLE_FILES['claude_api'])}`의 `mcp_servers`, `tools`, `betas`를 Messages API 요청에 넣습니다. `server_auth.token_env`가 있으면 요청 직전에 해당 환경변수 값을 `authorization_token`으로 주입하며, 토큰이나 비공식 `authorization_token_env` 필드를 JSON에 저장하지 않습니다. 요청 fragment 준비: `{str(claude_api_ready).lower()}`이며 실제 endpoint 또는 도구 호출 검증은 별도입니다.
- 클라이언트 설정 smoke: `{files.get('client_config_smoke', SETUP_BUNDLE_FILES['client_config_smoke'])}`를 실행하면 생성된 Codex/Claude Desktop 설정 파일의 `command`/`args` 그대로 MCP를 띄우고 `list_tools`, `get_index_status`, `search`, `fetch`를 확인합니다.
- 런타임 smoke 검증: `{files.get('validate', SETUP_BUNDLE_FILES['validate'])}`를 실행하면 `data/mcp_runtime_manifest.json`의 `recommended_smoke_query`를 읽어 실제 번들 데이터로 `search`/`fetch`를 확인합니다.

## ChatGPT Desktop 로컬 direct MCP 및 Codex CLI 연결

- ChatGPT Desktop 로컬 direct MCP: `{files.get('chatgpt_desktop_agent_prompt', SETUP_BUNDLE_FILES['chatgpt_desktop_agent_prompt'])}`에 표시된 Name, STDIO, Command, Working directory, Arguments를 ChatGPT Desktop의 `Settings > MCP servers > Add server`에 입력하는 방식이 기본입니다. Save 후 Restart하고 새 대화에서 `/mcp`로 `{server_name}`을 확인한 뒤 실제 `search`와 `fetch`를 호출합니다. 수동 입력이 어렵거나 고급 설정 파일 경로를 사용할 때만 보조 BAT `{files.get('connect_chatgpt_desktop_bat', SETUP_BUNDLE_FILES['connect_chatgpt_desktop_bat'])}`를 사용합니다. BAT는 공유 `~/.codex/config.toml`을 기록·검증하지만 제품 기능을 활성화하지는 않습니다. `@{server_name}` 반복 입력은 설치나 연결 확인을 대신하지 않습니다.
- Codex CLI 호환: `{files.get('connect_codex_bat', SETUP_BUNDLE_FILES['connect_codex_bat'])}`를 기본 연결 버튼으로 사용합니다. 수동 설정이 필요하면 먼저 `{files.get('codex_config', SETUP_BUNDLE_FILES['codex_config'])}`의 모든 `<BUNDLE_DIR>`을 `C:/MCP/aksmcp2`처럼 슬래시(`/`)를 쓴 현재 압축 해제 폴더의 절대 경로로 바꿉니다(역슬래시를 쓰려면 TOML 규칙에 맞게 각각 이스케이프). 그 뒤 TOML 블록을 `$HOME\\.codex\\config.toml`에 넣거나 기존 `[mcp_servers.{server_name}]` 블록과 교체합니다. 선택적 에이전트 요청문에는 비밀값이나 별도 설정 블록을 추가하지 않습니다.
- 이 스니펫은 `--data-dir`을 이 번들의 `data` 폴더로 고정하고 `--no-warm-cache`와 저장소 모드 플래그를 포함합니다. 그래서 예전 번들이나 다른 MCP 서버를 물고 느리게 인식하는 문제를 줄입니다.
- 로컬 stdio 설정은 `reg-rag-mcp-server`를 직접 부르지 않고 `{files.get('stdio_launcher', SETUP_BUNDLE_FILES['stdio_launcher'])}`를 PowerShell로 실행합니다. 설치가 성공하면 선택한 Python과 MCP 명령 모듈 {len(RUNTIME_IDENTITY_MODULES)}개의 SHA-256 build identity를 `runtime_python.json` schema 2에 기록합니다. launcher는 `PYTHONPATH`를 격리해 이 identity를 다시 확인한 뒤 저장소 checkout, `REG_RAG_PYTHON`, PATH보다 먼저 사용합니다. marker가 손상되거나 같은 Python의 모듈이 바뀌면 다른 runtime으로 조용히 fallback하지 않고 재설치를 요구합니다. marker가 아직 없는 설치 전 단계에서만 생성 당시 checkout과 명시적 runtime 탐색을 허용합니다.
- Codex CLI 설정을 붙여 넣은 뒤에는 `reg-rag-mcp-doctor --client-profile bundle --bundle-dir . --allow-local-only-bundle --codex-config $HOME\\.codex\\config.toml`로 실제 설치된 설정을 확인합니다.

## ChatGPT 연결

- ChatGPT Desktop 로컬 방식: `Settings > MCP servers > Add server` 내장 등록이 기본입니다. 생성 안내의 실제 입력값을 등록하고 Save 후 Restart한 뒤 새 대화에서 `/mcp`와 `search`·`fetch`를 확인합니다. 생성 플러그인은 별도 플러그인 배포가 명시적으로 필요할 때만 쓰는 선택 산출물입니다. 현재 제품 화면이 로컬 direct MCP를 노출하지 않으면 원격 HTTPS 또는 Secure MCP Tunnel 방식을 사용합니다.
- ChatGPT Desktop 전용 BAT는 수동 입력이 어렵거나 고급 설정 파일 경로가 필요할 때만 사용하는 보조 경로입니다. `~/.codex/config.toml` 기록 성공을 Desktop 연결 완료로 과장하지 않으며, 메뉴와 `/mcp`가 현재 제품에 노출되지 않으면 앱 업데이트·계정 정책 확인 후 원격 HTTPS 또는 Secure MCP Tunnel로 전환합니다. ChatGPT Desktop 연결 안내에는 Codex 에이전트 실행 명령을 표시하지 않습니다.
- HTTPS 방식: `{files.get('run_chatgpt', SETUP_BUNDLE_FILES['run_chatgpt'])}`로 외부 응답 경계인 `chatgpt-data` MCP 서버를 실행합니다. 직접 인증 endpoint는 MCP OAuth 2.1을 구현해야 하며 정적 `MCP_AUTH_TOKEN`을 ChatGPT에 입력할 수 없습니다. discovery·PKCE·audience·scope·callback 검증 후 `--chatgpt-oauth-ready`로 생성한 경우에만 ChatGPT Settings > Security and login에서 Developer mode를 켜고 Settings > Plugins 또는 https://chatgpt.com/plugins 의 +에서 `{files.get('chatgpt', SETUP_BUNDLE_FILES['chatgpt'])}`의 `connector_url`을 MCP server URL로 등록합니다. ChatGPT는 localhost MCP나 로컬 Codex `config.toml`을 웹 대화에서 직접 읽지 않습니다. 새 대화의 + > More에서 앱을 선택합니다. 검토·배포된 marketplace 플러그인은 개발자 모드 초안 앱과 별도 상태입니다. HTTPS 설정 산출물 준비: `{str(chatgpt_ready).lower()}`이며 실제 endpoint 도달·도구 목록 발견·대화 첨부 검증은 별도입니다.
- 상태 판정: `plugin_registered=true`는 companion JSON, 설치된 플러그인 캐시, `codex plugin list --json`의 exact version/source, `codex mcp get --json`의 현재 번들 경로가 모두 일치할 때만 기록합니다. `direct_stdio_verified`와 `transport_end_to_end_verified`는 직접 전송 검증이며, `desktop_tool_scan_verified`, `conversation_attachment_verified`, Desktop `end_to_end_verified`는 제품 화면에서 실제 확인하기 전까지 false입니다.
- 내부망/비공개 방식: 외부 inbound 방화벽을 열지 않아야 하면 `{files.get('openai_tunnel', SETUP_BUNDLE_FILES['openai_tunnel'])}`를 사용합니다. `CONTROL_PLANE_API_KEY`와 `OPENAI_TUNNEL_ID`는 파일에 쓰지 말고 실행 환경변수로 설정합니다. Settings > Security and login에서 Developer mode를 켜고 Settings > Plugins 또는 https://chatgpt.com/plugins 에서 +로 앱을 만든 뒤 Connection을 Tunnel로 선택합니다. 공개 HTTPS 방식은 같은 화면에서 MCP server URL을 입력하며, 검토·배포된 marketplace 플러그인 설치는 별도 단계입니다.

## 사전 진단

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('doctor', SETUP_BUNDLE_FILES['doctor'])}"
```

실제 운영 런타임에 승인 record가 보이고 smoke 문서가 섞이지 않았는지는 다음 명령으로 확인합니다.

```powershell
{audit_index_command}
```

## 보안 주의

- 토큰, API 키, 터널 ID 같은 승인값을 파일에 저장하지 마십시오.
- `MCP_AUTH_TOKEN`은 OAuth gateway 뒤의 origin 보호용이며 ChatGPT에 입력하는 인증값이 아닙니다. 직접 HTTPS 앱은 MCP OAuth 2.1이 필요하고, 비공개 서버는 `CONTROL_PLANE_API_KEY`와 `OPENAI_TUNNEL_ID`를 승인된 실행 환경에만 설정해 Secure MCP Tunnel을 사용하십시오.
- 생성된 HTTP 실행 스크립트는 서버 시작 전에 `reg-rag-mcp-doctor --fail-on-warning`을 실행합니다.
- ChatGPT 또는 Claude 원격 MCP로 반환되는 데이터는 외부 AI 서비스에 전달될 수 있습니다. 공개 가능 데이터 또는 별도 승인된 데이터만 사용하십시오.
- 비공개 규정 데이터는 로컬 stdio 또는 승인된 내부망 MCP 연결을 우선 사용하십시오.

## 한글 표시가 깨져 보일 때

이 번들의 JSON/Markdown 파일은 UTF-8입니다. Windows PowerShell이나 일부 뷰어에서 `별표`가 `蹂꾪몴`처럼
보이면 파일 손상이 아니라 CP949로 잘못 표시한 증상일 가능성이 큽니다. `Get-Content -Encoding UTF8 ...`,
`chcp 65001`, UTF-8 편집기, 브라우저, GitHub 화면으로 확인하십시오. 이 증상만으로 데이터를 재생성하거나
chunk_id를 바꾸면 승인 저널과 벡터 ID가 함께 바뀌므로 하지 않습니다.

## 경고

{warning_block}

## 공식 참고

- ChatGPT Desktop/Codex MCP: https://learn.chatgpt.com/docs/extend/mcp
- ChatGPT와 Codex 플러그인: https://help.openai.com/en/articles/20001256-plugins-in-codex
- ChatGPT 개발자 모드와 MCP 앱: https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta
- ChatGPT MCP 앱 인증: https://developers.openai.com/apps-sdk/build/auth
- OpenAI Secure MCP Tunnel: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- MCP Streamable HTTP 전송 규격: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Claude API MCP connector: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
- Claude custom connectors: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp
"""


def _powershell_http_command(
    command: str,
    args: list[object],
    token_env: str | None,
    *,
    doctor_args: list[object] | None = None,
) -> str:
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        'function Assert-EnvVar([string]$Name) { $Value = [Environment]::GetEnvironmentVariable($Name); if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "<*>") { throw "$Name must be set to an approved non-placeholder value before running this script." } }',
    ]
    if token_env:
        lines.append(f'Assert-EnvVar "{token_env}"')
    if doctor_args:
        lines.append('$DoctorPython = Resolve-BundleModulePython "scripts.check_mcp_connection_readiness"')
        lines.append('$DoctorArgs = ' + _powershell_array_literal(doctor_args))
        lines.append('$DoctorExitCode = Invoke-BundlePythonModule $DoctorPython "scripts.check_mcp_connection_readiness" $DoctorArgs')
        lines.append("if ($DoctorExitCode -ne 0) { exit $DoctorExitCode }")
    lines.append('$ServerPython = Resolve-BundleModulePython "scripts.run_regulation_mcp"')
    lines.append('$ServerArgs = ' + _powershell_array_literal(args))
    lines.append('$ServerExitCode = Invoke-BundlePythonModule $ServerPython "scripts.run_regulation_mcp" $ServerArgs')
    lines.append('if ($ServerExitCode -ne 0) { exit $ServerExitCode }')
    return "\n".join(lines)


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
    raw_value_indexes = {len(args) + 1, len(args) + 4}
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
        'if (-not $SmokeVerified) { throw "Runtime MCP smoke did not produce a fresh passing search/fetch report." }',
    ]
    return "\n".join(lines).replace("__TENANT_ID__", tenant_id).replace("__STORAGE_FLAG__", storage_flag)


def _powershell_bundle_client_config_smoke_script(*, server_name: str) -> str:
    plugin_name = _normalized_plugin_name(server_name)
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        *_powershell_bundle_runtime_module_resolver_lines(),
        '$ServerName = "__SERVER_NAME__"',
        '$SmokeReport = Join-Path $BundleDir "mcp_client_config_smoke.json"',
        '$CodexConfig = Join-Path $BundleDir "codex_config_snippet.toml"',
        '$ClaudeDesktopConfig = Join-Path $BundleDir "claude_desktop_config.json"',
        '$PluginMcpConfig = Join-Path $BundleDir "chatgpt-desktop-local-plugin\\plugins\\__PLUGIN_NAME__\\.mcp.json"',
        '$BundleStatus = Join-Path $BundleDir "bundle_status.json"',
        '$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"',
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
        'function Update-PluginBundleConfig {',
        '  $Plugin = Get-Content -LiteralPath $PluginMcpConfig -Raw -Encoding UTF8 | ConvertFrom-Json',
        '  if ($Plugin.PSObject.Properties["mcp_servers"]) { throw "Generated ChatGPT Desktop plugin config uses unsupported mcp_servers; regenerate the bundle." }',
        '  if (-not $Plugin.mcpServers) { throw "Generated ChatGPT Desktop plugin config is missing mcpServers." }',
        '  $ServerProperty = $Plugin.mcpServers.PSObject.Properties[$ServerName]',
        '  if (-not $ServerProperty) { throw "Generated ChatGPT Desktop plugin config is missing MCP server $ServerName." }',
        '  $Server = $ServerProperty.Value',
        '  $Server.command = "powershell.exe"',
        '  $Server.args = @(Set-McpBundlePaths @($Server.args))',
        '  Write-JsonUtf8NoBom $PluginMcpConfig $Plugin 40',
        '  return @($Server.args)',
        '}',
        'if (-not (Test-Path -LiteralPath $CodexConfig)) { throw "Missing generated Codex config snippet: $CodexConfig" }',
        'if (-not (Test-Path -LiteralPath $ClaudeDesktopConfig)) { throw "Missing generated Claude Desktop config: $ClaudeDesktopConfig" }',
        'if (-not (Test-Path -LiteralPath $PluginMcpConfig)) { throw "Missing generated ChatGPT Desktop plugin MCP config: $PluginMcpConfig" }',
        'if (-not (Test-Path -LiteralPath $StdioLauncher)) { throw "Missing generated stdio launcher: $StdioLauncher" }',
        '$ClaudeDesktopArgs = Update-ClaudeDesktopBundleConfig',
        '$PluginArgs = Update-PluginBundleConfig',
        '# ChatGPT Desktop and Codex use the ChatGPT-local source; Claude Desktop keeps its own source.',
        'Write-CodexBundleConfig $PluginArgs',
        '$SmokeArgs = @("--server-name", $ServerName, "--codex-config", $CodexConfig, "--claude-desktop-config", $ClaudeDesktopConfig, "--plugin-mcp-config", $PluginMcpConfig, "--out-json", $SmokeReport, "--fail-on-issue")',
        '$McpPython = Resolve-BundleModulePython "scripts.run_mcp_client_config_smoke"',
        'if (Test-Path -LiteralPath $SmokeReport) { Remove-Item -LiteralPath $SmokeReport -Force }',
        '$SmokeExitCode = Invoke-BundlePythonModule $McpPython "scripts.run_mcp_client_config_smoke" $SmokeArgs',
        '$SmokeResult = $null',
        'if (Test-Path -LiteralPath $SmokeReport) { try { $SmokeResult = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $SmokeResult = $null } }',
        '$SmokeVerified = $SmokeExitCode -eq 0 -and $SmokeResult -and [string]$SmokeResult.report_type -eq "mcp_client_config_smoke" -and $SmokeResult.passed -eq $true -and $SmokeResult.launcher_ready -eq $true -and $SmokeResult.process_started -eq $true -and $SmokeResult.mcp_initialized -eq $true -and $SmokeResult.tools_discovered -eq $true -and $SmokeResult.end_to_end_verified -eq $true -and @($SmokeResult.results).Count -eq 3',
        'Write-Host "Client config smoke report: $SmokeReport"',
        'if (-not $SmokeVerified) { throw "Client config smoke did not produce a fresh passing three-client report." }',
    ]
    return "\n".join(lines).replace("__SERVER_NAME__", server_name).replace("__PLUGIN_NAME__", plugin_name)


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
        'if ([string]::IsNullOrWhiteSpace($RemoteUrl)) { throw "No ChatGPT remote HTTPS endpoint is configured. Regenerate with --public-url https://your-host.example/mcp or use Secure MCP Tunnel." }',
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
        'Write-Host "Protocol validation does not replace ChatGPT Settings > Plugins create/refresh or per-conversation attachment."',
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
        '  return "sha256:" + (Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash.ToLowerInvariant()',
        '}',
        'function Restore-ClaudeConfigAtomically([string]$BackupPath, [string]$TargetPath) {',
        '  $Parent = Split-Path -Parent $TargetPath',
        '  $TemporaryPath = Join-Path $Parent (".claude.{0}.{1}.restore-tmp" -f $PID, [Guid]::NewGuid().ToString("N"))',
        '  $ReplaceBackupPath = Join-Path $Parent (".claude.{0}.{1}.restore-bak" -f $PID, [Guid]::NewGuid().ToString("N"))',
        '  try {',
        '    Copy-Item -LiteralPath $BackupPath -Destination $TemporaryPath -Force',
        '    if (Test-Path -LiteralPath $TargetPath) { [System.IO.File]::Replace($TemporaryPath, $TargetPath, $ReplaceBackupPath, $true) }',
        '    else { Move-Item -LiteralPath $TemporaryPath -Destination $TargetPath }',
        '    if ((Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash) { throw "Claude Code config rollback hash mismatch." }',
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
        '    $ClaudeOriginalConfigFingerprint = (Get-FileHash -LiteralPath $ClaudeUserConfig -Algorithm SHA256).Hash',
        '    Copy-Item -LiteralPath $ClaudeUserConfig -Destination $ClaudeConfigBackup -Force',
        '    $ClaudeConfigBackupFingerprint = (Get-FileHash -LiteralPath $ClaudeConfigBackup -Algorithm SHA256).Hash',
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
    lines = [
            'param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ServerArgs)',
            '$ErrorActionPreference = "Stop"',
            *_powershell_bundle_data_dir_lines(),
            "$PreferredPython = " + _powershell_single_quoted_json(preferred_python_value),
            "$PreferredProjectRoot = " + _powershell_single_quoted_json(preferred_project_root_value),
            "$DefaultServerArgs = " + _powershell_array_literal(default_server_args),
            'if (-not $ServerArgs -or $ServerArgs.Count -eq 0) { $ServerArgs = $DefaultServerArgs }',
    ]
    if packaged_executable:
        escaped_executable = packaged_executable.replace("'", "''")
        lines.extend(
            [
                f"$PackagedExe = '{escaped_executable}'",
                'if (Test-Path -LiteralPath $PackagedExe) {',
                '  & $PackagedExe --mcp-server @ServerArgs',
                '  exit $LASTEXITCODE',
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
            '    if (-not [System.IO.Path]::IsPathRooted($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { throw "recorded Python is unavailable" }',
            '    $Leaf = [System.IO.Path]::GetFileNameWithoutExtension($Candidate)',
            '    if ($Leaf -notmatch "^python(?:\\d+(?:\\.\\d+)*)?$") { throw "recorded executable is not Python" }',
            '    $Resolved = (Resolve-Path -LiteralPath $Candidate).Path',
            '    if (-not (Test-RuntimeMarkerIdentity $Resolved $Marker)) { throw "recorded MCP command-module identity mismatch" }',
            '    return $Resolved',
            '  } catch {',
            '    throw "runtime_python.json is invalid. Re-run install_local_package.ps1. $($_.Exception.Message)"',
            '  }',
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
            '  $ScriptPath = Join-Path $ProjectRoot "scripts\\run_regulation_mcp.py"',
            '  $PythonCandidates = @()',
            '  $RecordedRuntimePython = Get-RecordedRuntimePython',
            '  if ($RecordedRuntimePython) { $PythonCandidates += $RecordedRuntimePython }',
            '  if ($env:REG_RAG_PYTHON) { $PythonCandidates += $env:REG_RAG_PYTHON }',
            '  if ($PreferredPython) { $PythonCandidates += $PreferredPython }',
            '  $PythonCandidates += (Join-Path $ProjectRoot ".venv\\Scripts\\python.exe")',
            '  $PythonCandidates += "python"',
            '  $PyLauncherPython = Get-PyLauncherPython',
            '  if ($PyLauncherPython) { $PythonCandidates += $PyLauncherPython }',
            '  foreach ($Candidate in $PythonCandidates) {',
            '    if (-not $Candidate) { continue }',
            '    $Command = $null',
            '    if (Test-Path -LiteralPath $Candidate) { $Command = $Candidate }',
            '    else {',
            '      $Resolved = Get-Command $Candidate -ErrorAction SilentlyContinue',
            '      if ($Resolved) { $Command = $Resolved.Source }',
            '    }',
            '    if ($Command) {',
            '      $env:PYTHONPATH = if ($env:PYTHONPATH) { "$ProjectRoot;$env:PYTHONPATH" } else { $ProjectRoot }',
            '      $ProbeErrorAction = $ErrorActionPreference',
            '      $ErrorActionPreference = "Continue"',
            '      & $Command -c "import scripts.run_regulation_mcp,sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 41)" 1>$null 2>$null',
            '      $ProbeExitCode = $LASTEXITCODE',
            '      $ErrorActionPreference = $ProbeErrorAction',
            '      if ($ProbeExitCode -ne 0) { continue }',
            '      & $Command $ScriptPath @ArgsToPass',
            '      exit $LASTEXITCODE',
            '    }',
            '  }',
            '  throw "Python was not found. Install the bundled wheel or set REG_RAG_PYTHON to the project Python executable."',
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
            '# An extracted bundle may not contain the source checkout. When the operator points',
            '# REG_RAG_PYTHON at the installed wheel environment, invoke its packaged module',
            '# directly instead of relying on a stale console script from another PATH entry.',
            '$PackagedPythonCandidates = @()',
            '$RecordedRuntimePython = Get-RecordedRuntimePython',
            'if ($RecordedRuntimePython) { $PackagedPythonCandidates += $RecordedRuntimePython }',
            'if ($env:REG_RAG_PYTHON) { $PackagedPythonCandidates += $env:REG_RAG_PYTHON }',
            'if ($PreferredPython) { $PackagedPythonCandidates += $PreferredPython }',
            '$PathPython = Get-Command "python" -ErrorAction SilentlyContinue',
            'if ($PathPython) { $PackagedPythonCandidates += $PathPython.Source }',
            '$PyLauncherPython = Get-PyLauncherPython',
            'if ($PyLauncherPython) { $PackagedPythonCandidates += $PyLauncherPython }',
            'foreach ($Candidate in $PackagedPythonCandidates) {',
            '  if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) { continue }',
            '  $PackagedProbeErrorAction = $ErrorActionPreference',
            '  $ErrorActionPreference = "Continue"',
            '  & $Candidate -c "import scripts.run_regulation_mcp,sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 41)" 1>$null 2>$null',
            '  $PackagedProbeExitCode = $LASTEXITCODE',
            '  $ErrorActionPreference = $PackagedProbeErrorAction',
            '  if ($PackagedProbeExitCode -eq 0) {',
            '    & $Candidate -m scripts.run_regulation_mcp @ServerArgs',
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
            'throw "reg-rag-mcp-server was not found on PATH, and neither the generated project runtime nor a source checkout is available. Run install_local_package.ps1 once, then restart the MCP client."',
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
        '  if ($env:REG_RAG_PYTHON -and (Test-BundlePythonModule $env:REG_RAG_PYTHON $ModuleName)) { return (Resolve-Path -LiteralPath $env:REG_RAG_PYTHON).Path }',
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
            "claude-api",
            "bundle",
        ],
        default="generic",
        help=(
            "Output shape for the target client. Use chatgpt-desktop-local for the local stdio plugin, "
            "chatgpt-remote for reachable HTTPS Streamable HTTP, or bundle for all supported clients. "
            "The chatgpt value remains a legacy alias for chatgpt-remote."
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
            "Environment variable used by generated remote HTTP origin commands for bearer auth. "
            "ChatGPT cannot accept this as a custom API key; direct ChatGPT apps still require MCP OAuth 2.1."
        ),
    )
    parser.add_argument(
        "--chatgpt-oauth-ready",
        action="store_true",
        help=(
            "Attest that the public ChatGPT /mcp endpoint implements and has been tested with MCP OAuth 2.1. "
            "Without this attestation the direct ChatGPT profile remains not ready; use Secure MCP Tunnel instead."
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
        help="Include the latest dist/reg_rag_preprocessor-*.whl in the setup bundle zip.",
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
        remote_auth_token_env=args.remote_auth_token_env,
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
