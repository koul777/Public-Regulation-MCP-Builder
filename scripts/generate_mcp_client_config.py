from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.mcp_bundle_contract import ALL_SETUP_BUNDLE_FILES, SETUP_BUNDLE_FILES
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
)
UTF8_BOM = b"\xef\xbb\xbf"
CHATGPT_DESKTOP_PLUGIN_TEMPLATE_REVISION = "chatgpt-desktop-local-plugin-v3"


def _write_utf8_no_bom(path: Path, text: str) -> None:
    """Write machine-readable text as strict UTF-8 without a byte-order mark."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8")
    if encoded.startswith(UTF8_BOM):
        raise ValueError(f"Refusing to write a UTF-8 BOM to machine-readable file: {path}")
    path.write_bytes(encoded)


def _write_json_utf8_no_bom(path: Path, payload: Any) -> None:
    _write_utf8_no_bom(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


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
    min_visible_records: int = 1,
) -> dict[str, Any]:
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


def write_mcp_setup_bundle(
    config: dict[str, Any],
    out_dir: str | Path,
    *,
    server_name: str,
    preferred_python: str | Path | None = None,
    preferred_project_root: str | Path | None = None,
) -> dict[str, str]:
    """Write copy/paste-ready MCP setup artifacts for common clients."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", server_name):
        raise ValueError("server_name must use lowercase ASCII letters, numbers, dot, hyphen, or underscore.")
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
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
    if "claude_desktop" in json_config:
        local_stdio_config = _local_stdio_config_for_server(
            json_config.get("chatgpt_desktop_local") or json_config["claude_desktop"],
            server_name=server_name,
        )
        write_json("claude_desktop", local_stdio_config)
        codex_snippet = _codex_config_snippet(local_stdio_config, server_name=server_name)
        if codex_snippet:
            write_text("codex_config", codex_snippet)
        write_json(
            "chatgpt_desktop_local",
            _chatgpt_desktop_local_config(
                local_stdio_config,
                server_name=server_name,
                bundle_dir=output_dir,
            ),
        )
        files.update(
            _write_chatgpt_desktop_local_plugin(
                local_stdio_config,
                output_dir=output_dir,
                server_name=server_name,
            )
        )
        write_text(
            "codex_plugin_guide",
            _codex_plugin_manual_guide(
                local_stdio_config,
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
        write_text(
            "connect",
            _with_connect_wizard_preferred_runtime(
                copy_paste["connect_wizard_ps"],
                preferred_python=preferred_python,
                preferred_project_root=preferred_project_root,
            ),
        )
    write_text("install", _install_local_package_script())
    write_text("usage_guide", _mcp_first_use_guide(server_name))
    write_text(
        "usage_guide_bat",
        _windows_open_text_file_script(SETUP_BUNDLE_FILES["usage_guide"]),
    )
    write_text(
        "connect_codex_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target codex -InstallCodex",
            next_steps=[
                "Codex를 완전히 종료한 뒤 다시 실행합니다.",
                "새 task에서 /mcp를 입력해 등록 이름을 확인합니다.",
                f"새 task에서 {server_name} MCP를 사용해서 등록된 규정 목록을 보여줘 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_chatgpt_desktop_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target chatgpt-desktop-local -InstallChatGptDesktopPlugin",
            next_steps=[
                "ChatGPT Desktop을 완전히 종료한 뒤 다시 실행합니다.",
                f"새 대화에서 + > 더 보기 > {server_name}을 선택하거나 @{server_name}을 멘션합니다.",
                f"@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘. 라고 입력합니다.",
                "플러그인 등록 완료와 현재 대화의 도구 첨부는 서로 다른 상태입니다.",
            ],
        ),
    )
    write_text(
        "connect_claude_desktop_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target claude-desktop -InstallClaudeDesktop",
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
            "-Target claude-code",
            next_steps=[
                "Claude Code를 다시 실행합니다.",
                "대화에서 /mcp를 입력해 등록 이름을 확인합니다.",
                f"{server_name} MCP를 사용해서 등록된 규정 목록을 보여줘 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_chatgpt_https_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target chatgpt-remote",
            next_steps=[
                "열린 ChatGPT 웹의 Settings, Apps, Create에서 복사된 HTTPS 주소를 등록합니다.",
                f"앱 이름은 {server_name} 으로 입력하고 Scan tools와 Create를 승인합니다.",
                f"새 대화에서 앱을 선택한 뒤 {server_name}에서 등록된 규정 목록을 보여줘 라고 입력합니다.",
            ],
        ),
    )
    write_text(
        "connect_chatgpt_tunnel_bat",
        _windows_batch_launcher_script(
            SETUP_BUNDLE_FILES["connect"],
            "-Target chatgpt-tunnel",
            next_steps=[
                "ChatGPT 웹의 Settings, Apps에서 보안 터널 MCP를 승인합니다.",
                f"앱 이름은 {server_name} 으로 등록합니다.",
                f"새 대화에서 앱을 선택한 뒤 {server_name}에서 등록된 규정 목록을 보여줘 라고 입력합니다.",
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
                f"Claude 요청에서 {server_name} MCP를 활성화한 뒤 규정 목록을 요청합니다.",
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
    payload: dict[str, Any] = {
        "report_type": "mcp_bundle_status",
        "schema_version": "mcp-bundle-status-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_dir": str(output_dir),
        "runtime_data_dir": str(runtime_data_dir),
        "runtime_data_ready": runtime_ready,
        "launcher_ready": (output_dir / SETUP_BUNDLE_FILES["stdio_launcher"]).is_file(),
        "process_started": False,
        "mcp_initialized": False,
        "tools_discovered": False,
        "plugin_install_command_succeeded": False,
        "plugin_manifest_validated": False,
        "plugin_discoverable": False,
        "plugin_registered": False,
        "direct_stdio_verified": False,
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
        "conversation_attachment_unverified": True,
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
            "plugin_registered": (
                "True only after manifest validation, install command success, and exact version/source discoverability all succeed."
            ),
            "direct_stdio_verified": (
                "The generated launcher passed initialize, tools/list, and get_index_status directly over stdio."
            ),
            "desktop_tool_scan_verified": (
                "A ChatGPT Desktop tool scan exposed the expected MCP tools; direct stdio smoke does not set this."
            ),
            "conversation_attachment_verified": (
                "The plugin was selected or mentioned and its tools were observed in the current conversation."
            ),
            "conversation_attachment_unverified": (
                "Select the plugin from + > More or mention it in each new conversation when required."
            ),
            "end_to_end_verified": (
                "Protocol end-to-end verification through the generated transport; Desktop exposure is tracked separately."
            ),
        },
        "profiles": {
            "chatgpt-desktop-local": {"transport": "stdio", "surface": "unified_plugin_directory"},
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
    _write_json_utf8_no_bom(
        path,
        _bundle_status_payload(
            output_dir,
            config=config,
            setup_manifest=setup_manifest,
            runtime_manifest=runtime_manifest,
        ),
    )
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
        "# Paste or replace this server block in $HOME\\.codex\\config.toml.",
        "# Keep --data-dir pointed at this bundle's data directory to avoid stale or slow MCP startup.",
        f"[mcp_servers.{_toml_key(server_name)}]",
        f"command = {_toml_string(command)}",
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
    lines = [
        "Codex 앱 > 설정 > 플러그인 > MCP 수동 입력값",
        "",
        "기본 방법: Codex에 연결하기.bat를 실행하면 아래 값이 자동 등록됩니다.",
        "이 파일은 자동 등록이 되지 않을 때만 사용합니다.",
        "",
        f"MCP 이름: {server_name}",
        f"실행 명령: {command}",
        f"작업 중인 디렉터리: {Path(bundle_dir).resolve()}",
        "",
        "인자 - 아래 항목을 위에서부터 하나씩 추가:",
    ]
    lines.extend(f"{index}. {arg}" for index, arg in enumerate(args, start=1))
    if not args:
        lines.append("없음")
    lines.extend(["", "환경 변수:"])
    if env:
        lines.extend(f"{key}={value}" for key, value in env.items())
    else:
        lines.append("비워 둠")
    lines.extend(
        [
            "",
            "저장 후 Codex 앱을 완전히 종료하고 다시 실행합니다.",
            f"새 task에서 /mcp를 입력해 {server_name} 이름이 보이는지 확인합니다.",
            f"확인 요청: {server_name} MCP를 사용해서 등록된 규정 목록을 보여줘.",
        ]
    )
    return "\n".join(lines)


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
        "surface": "unified_chatgpt_codex_plugin_directory",
        "mode": "local_stdio",
        "chatgpt_direct_local_mcp_supported": False,
        "supported_runtime_note": (
            "The local stdio dependency is intended for the Codex-capable surface in the unified desktop app. "
            "ChatGPT conversations require a remote MCP app or Secure MCP Tunnel unless the product explicitly exposes the local plugin."
        ),
        "server_name": server_name,
        "plugin_name": plugin_name,
        "plugin_marketplace_root": "chatgpt-desktop-local-plugin",
        "plugin_manifest": f"chatgpt-desktop-local-plugin/plugins/{plugin_name}/.codex-plugin/plugin.json",
        "plugin_mcp_config": f"chatgpt-desktop-local-plugin/plugins/{plugin_name}/.mcp.json",
        "plugin_install_command_succeeded": False,
        "plugin_manifest_validated": False,
        "plugin_discoverable": False,
        "plugin_registered": False,
        "direct_stdio_verified": False,
        "desktop_tool_scan_verified": False,
        "conversation_attachment_verified": False,
        "conversation_attachment_unverified": True,
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
            "Double-click ChatGPT Desktop에 연결하기.bat.",
            "The installer registers the generated local plugin marketplace and plugin package.",
            "Fully quit ChatGPT Desktop, start it again, and open a new conversation.",
            f"Select the {server_name} plugin from + > More, or mention @{server_name}.",
            f"Verification prompt: @{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.",
        ],
        "status_semantics": {
            "plugin_install_command_succeeded": "The plugin install command returned success; this is not discoverability proof.",
            "plugin_manifest_validated": "All companion JSON files passed strict UTF-8-without-BOM validation.",
            "plugin_discoverable": "An enabled selector with the exact cachebuster version and marketplace source appeared in Codex plugin list JSON.",
            "plugin_registered": "Manifest validation, install command success, and exact version/source discoverability all succeeded.",
            "direct_stdio_verified": "Direct initialize, tools/list, and get_index_status succeeded over stdio.",
            "desktop_tool_scan_verified": "ChatGPT Desktop exposed the expected tools after its own tool scan.",
            "conversation_attachment_verified": "The plugin tools were observed in the current conversation.",
            "conversation_attachment_unverified": "The current conversation must still select or mention the plugin.",
            "end_to_end_verified": "The generated transport passed the MCP protocol chain; Desktop exposure is separate.",
        },
        "troubleshooting": [
            "입력창의 + 버튼 선택",
            "더 보기 선택",
            f"{server_name} 선택",
            f"또는 @{server_name} 멘션",
        ],
    }


def _normalized_plugin_name(server_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", server_name.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return (normalized or "regulation-mcp")[:64].rstrip("-")


def _chatgpt_local_marketplace_name(server_name: str) -> str:
    suffix = "-local"
    base = _normalized_plugin_name(server_name)
    if len(base) + len(suffix) > 64:
        base = base[: 64 - len(suffix)].rstrip("-")
    return base + suffix


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

    # Codex plugin .mcp.json uses the official wrapped ``mcp_servers`` shape.
    # Claude Desktop's separate config continues to use ``mcpServers``.
    plugin_mcp_config = {"mcp_servers": {server_name: mcp_servers[server_name]}}
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
        "description": "Korean public-institution regulation search and index-status tools over local MCP.",
        "author": {"name": "Public Regulation MCP Builder contributors"},
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": server_name,
            "shortDescription": "Search approved local regulation data.",
            "longDescription": (
                "Runs the approved regulation MCP locally through stdio and exposes read-only regulation tools."
            ),
            "developerName": "Public Regulation MCP Builder contributors",
            "category": "Productivity",
            "capabilities": ["Read"],
            "defaultPrompt": [
                f"@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.",
                f"@{server_name} 등록된 규정 목록을 보여줘.",
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
    runtime_data_dir = output_dir / "data"
    _clear_stale_bundle_status_reports(output_dir)
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
        "source_data_dir": str(source_settings.data_dir),
        "runtime_data_dir": str(runtime_data_dir),
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
    runtime_manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    runtime_manifest_path.write_text(json.dumps(runtime_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runtime_manifest["files"]["runtime_manifest"] = str(runtime_manifest_path)
    _write_bundle_status(output_dir, runtime_manifest=runtime_manifest)
    _report_runtime_progress(progress_callback, 100, "기관 전체 MCP 데이터 생성 완료", len(records), len(records))
    return runtime_manifest


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
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in archive_files:
            info = zipfile.ZipInfo.from_file(path, arcname=arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, archive.open(info, "w") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
                    bytes_written += len(block)
                    if progress_callback is not None:
                        progress_callback(bytes_written, total_bytes, arcname)
    return str(zip_path)


def _load_strict_utf8_json_for_bundle(path: Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(UTF8_BOM):
        raise ValueError(f"Generated plugin companion JSON must be UTF-8 without BOM: {path}")
    try:
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Generated plugin companion file must contain strict UTF-8 JSON: {path}: {exc}") from exc


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
    return [
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
            "primary_file": SETUP_BUNDLE_FILES["connect_claude_code_bat"],
            "config_file": SETUP_BUNDLE_FILES["claude_code_stdio"],
            "operator_action": "Double-click the Claude Code connection button.",
        },
        {
            "client": "ChatGPT Desktop",
            "profile": "chatgpt-desktop-local",
            "mode": "local_stdio",
            "ready": "plugin_registration_required",
            "primary_file": SETUP_BUNDLE_FILES["connect_chatgpt_desktop_bat"],
            "config_file": SETUP_BUNDLE_FILES["chatgpt_desktop_local"],
            "plugin_marketplace_root": "chatgpt-desktop-local-plugin",
            "operator_action": (
                "Register the local plugin, fully restart ChatGPT Desktop, then attach it with + > More or @mention."
            ),
        },
        {
            "client": "Codex CLI",
            "profile": "codex-compatibility",
            "mode": "local_stdio",
            "ready": True,
            "primary_file": SETUP_BUNDLE_FILES["connect_codex_bat"],
            "config_file": SETUP_BUNDLE_FILES["codex_config"],
            "operator_action": "Use the Codex compatibility button only for direct CLI MCP registration.",
        },
        {
            "client": "ChatGPT",
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
            "client": "ChatGPT",
            "mode": "secure_mcp_tunnel",
            "ready": "manual_setup_required",
            "primary_file": SETUP_BUNDLE_FILES["connect_chatgpt_tunnel_bat"],
            "config_file": SETUP_BUNDLE_FILES["openai_tunnel"],
            "operator_action": "Set approved tunnel credentials once, then double-click the ChatGPT Tunnel connection button.",
        },
        {
            "client": "Claude API",
            "mode": "https_mcp_connector",
            "ready": claude_api_ready,
            "primary_file": SETUP_BUNDLE_FILES["connect_claude_https_bat"],
            "config_file": SETUP_BUNDLE_FILES["claude_api"],
            "server_file": SETUP_BUNDLE_FILES["run_http"],
            "operator_action": "Double-click the Claude HTTPS connection button, then use the generated API fragment.",
        },
    ]


def _install_local_package_script() -> str:
    return r'''param(
  [string]$PackagePath = "",
  [switch]$NoEditable
)

$ErrorActionPreference = "Stop"
$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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
  return Get-ChildItem -Path $BundleDir -Filter "reg_rag_preprocessor-*.whl" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
}

function Assert-Python {
  if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python was not found on PATH. Install Python 3.11+ or activate the approved Python environment first."
  }
}

function Assert-McpCommands {
  $Missing = @()
  foreach ($Name in @("reg-rag-mcp-server", "reg-rag-mcp-config", "reg-rag-mcp-doctor", "reg-rag-mcp-smoke", "reg-rag-mcp-index-visibility")) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
      $Missing += $Name
    }
  }
  if ($Missing.Count -gt 0) {
    throw "Package installed, but these console commands are still not on PATH: $($Missing -join ', '). Activate the Python environment used for installation."
  }
}

Assert-Python

if ($PackagePath) {
  $ResolvedPackage = Resolve-Path $PackagePath
  python -m pip install $ResolvedPackage.Path
} else {
  $ProjectRoot = Get-ProjectRoot
  $BundledWheel = Get-BundledWheel
  if ($NoEditable -and $BundledWheel) {
    python -m pip install $BundledWheel.FullName
  } elseif (-not $ProjectRoot -and $BundledWheel) {
    python -m pip install $BundledWheel.FullName
  } elseif (-not $ProjectRoot) {
    throw "Could not find pyproject.toml above this bundle and no bundled wheel was found. Run from a bundle inside the repository, pass -PackagePath path\to\reg_rag_preprocessor*.whl, or regenerate the zip with --include-wheel."
  } elseif ($NoEditable) {
    $Wheel = Get-ChildItem -Path (Join-Path $ProjectRoot "dist") -Filter "reg_rag_preprocessor-*.whl" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending |
      Select-Object -First 1
    if (-not $Wheel) {
      throw "No wheel found under $ProjectRoot\dist. Build one first, omit -NoEditable, or regenerate the bundle zip with --include-wheel."
    }
    python -m pip install $Wheel.FullName
  } else {
    python -m pip install -e $ProjectRoot
  }
}

if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

Assert-McpCommands
Write-Host "reg-rag MCP console commands are installed and visible on PATH."
'''


def _mcp_first_use_guide(server_name: str) -> str:
    return f"""PR MCP Builder 설치 후 사용 안내

등록된 MCP 이름: {server_name}

핵심 사용 순서
1. 사용할 AI 앱의 연결 BAT를 더블클릭합니다.
2. 오류 없이 끝나면 AI 앱을 완전히 종료한 뒤 다시 실행합니다.
3. 새 대화에서 아래 문장을 입력합니다.

@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.

도구를 명시해서 확인하려면 아래 문장을 입력합니다.

{server_name} MCP의 list_regulations 도구를 사용해서 등록된 규정 목록을 보여줘.

ChatGPT Desktop 로컬 플러그인
- 설치: ChatGPT Desktop에 연결하기.bat
- BAT가 생성된 로컬 플러그인 마켓플레이스와 {server_name} 플러그인을 등록
- 확인: 앱을 완전히 다시 시작하고 새 대화의 + > 더 보기에서 {server_name} 선택 또는 @{server_name} 멘션
- 주의: 플러그인 등록 완료와 현재 대화에 도구가 첨부된 상태는 별개

Codex CLI 호환
- 설치: Codex에 연결하기.bat
- 확인: 새 task에서 /mcp
- 터미널 확인: codex mcp list

Claude Desktop
- 설치: Claude Desktop에 연결하기.bat
- 확인: 앱을 완전히 다시 시작한 뒤 새 대화에서 MCP 이름을 포함해 요청

Claude Code
- 설치: Claude Code에 연결하기.bat
- 확인: 대화에서 /mcp
- 터미널 확인: claude mcp list

ChatGPT 웹
- ChatGPT 대화는 localhost MCP에 직접 연결하지 않습니다.
- ChatGPT HTTPS 또는 보안 Tunnel BAT로 원격 MCP를 준비합니다.
- ChatGPT 웹의 Settings > Apps > Create에서 앱 이름을 {server_name}으로 등록합니다.
- 새 대화에서 앱을 선택하거나 @{server_name}을 지정한 뒤 요청합니다.

실제 규정 조회 예시
{server_name} MCP에서 인사규정을 찾고 관련 조문 원문과 출처를 보여줘. search 결과는 fetch로 확인해.

같은 MCP 업데이트
- 같은 이름으로 다시 생성하고 같은 클라이언트 BAT를 실행하면 기존 설정을 교체합니다.
- 새 번들은 현재 승인된 전체 청크를 다시 포함하므로 추가·개정 청크가 같은 MCP에 반영됩니다.
- 저장 폴더를 옮겼다면 새 폴더에서 BAT를 다시 실행해 경로를 갱신합니다.
- ChatGPT 앱의 도구 정의 snapshot이 오래되면 Apps 설정에서 도구를 새로고침하거나 앱을 다시 생성합니다.

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
        "reg-rag-mcp-index-visibility": r"scripts\audit_mcp_index_visibility.py",
    }
    lines = [
        "$script:McpPreferredPython = " + _powershell_single_quoted_json(preferred_python_value),
        "$script:McpPreferredProjectRoot = " + _powershell_single_quoted_json(preferred_project_root_value),
        'if (Test-Path -LiteralPath $script:McpPreferredPython) {',
        '  $env:PYTHONPATH = if ($env:PYTHONPATH) { "$script:McpPreferredProjectRoot;$env:PYTHONPATH" } else { $script:McpPreferredProjectRoot }',
    ]
    for command_name, relative_script in command_scripts.items():
        variable_name = "McpPreferred" + "".join(part.title() for part in command_name.split("-")) + "Script"
        lines.extend(
            [
                f"  $script:{variable_name} = Join-Path $script:McpPreferredProjectRoot "
                + _powershell_single_quoted_json(relative_script),
                f"  if (Test-Path -LiteralPath $script:{variable_name}) {{",
                f"    function {command_name} {{ & $script:McpPreferredPython $script:{variable_name} @args }}",
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
  [ValidateSet("menu", "install", "claude-desktop", "claude-code", "codex", "chatgpt-desktop-local", "chatgpt-remote", "chatgpt-desktop", "chatgpt-https", "chatgpt-tunnel", "claude-api", "doctor")]
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
$EmbeddedClaudeDesktopConfigBase64 = "__EMBEDDED_CLAUDE_DESKTOP_CONFIG_BASE64__"
$PreferredPython = ""
$PreferredProjectRoot = ""
$McpCommandScripts = @{
  "reg-rag-mcp-server" = "scripts\run_regulation_mcp.py"
  "reg-rag-mcp-doctor" = "scripts\check_mcp_connection_readiness.py"
  "reg-rag-mcp-smoke" = "scripts\run_mcp_smoke.py"
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

function Write-JsonUtf8NoBom([string]$LiteralPath, [object]$Value, [int]$Depth = 50) {
  $Json = ($Value | ConvertTo-Json -Depth $Depth) + [Environment]::NewLine
  Write-Utf8NoBom $LiteralPath $Json
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

function Read-BundleServerConfig {
  try {
    return Read-JsonFile "claude_desktop_config.json"
  } catch {
    Write-Warning "Generated claude_desktop_config.json is invalid; recovering the MCP entry from the embedded UTF-8 configuration."
    try {
      $Bytes = [Convert]::FromBase64String($EmbeddedClaudeDesktopConfigBase64)
      $Json = [Text.Encoding]::UTF8.GetString($Bytes)
      return $Json | ConvertFrom-Json
    } catch {
      throw "Both the generated and embedded MCP configurations are invalid: $($_.Exception.Message)"
    }
  }
}

function Update-BundleStatus([hashtable]$Values) {
  $StatusPath = BundlePath "bundle_status.json"
  if (-not (Test-Path -LiteralPath $StatusPath)) { return }
  try {
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
    Write-JsonUtf8NoBom $StatusPath $Status 50
  } catch {
    Write-Warning "Could not update bundle_status.json: $($_.Exception.Message)"
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
  & $Path
}

function Test-CoreCommands {
  return Test-NamedCommands @("reg-rag-mcp-server", "reg-rag-mcp-doctor", "reg-rag-mcp-smoke", "reg-rag-mcp-index-visibility")
}

function Test-DoctorCommands {
  return Test-NamedCommands @("reg-rag-mcp-doctor")
}

function Get-McpCommandInvocation([string]$Name) {
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

function Invoke-McpCommand([string]$Name, [object[]]$Arguments) {
  $Invocation = @(Get-McpCommandInvocation $Name)
  if ($Invocation.Count -eq 0) {
    throw "$Name was not found on PATH and no generated project runtime fallback is available."
  }
  $Executable = $Invocation[0]
  $PrefixArgs = @()
  if ($Invocation.Count -gt 1) {
    $PrefixArgs = @($Invocation[1..($Invocation.Count - 1)])
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$PreferredProjectRoot;$env:PYTHONPATH" } else { $PreferredProjectRoot }
  }
  & $Executable @PrefixArgs @Arguments | Out-Host
  $CommandExitCode = $LASTEXITCODE
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

function Install-LocalPackage {
  Show-Header
  Run-Script "install_local_package.ps1"
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
  if ($env:USERPROFILE) {
    return Join-Path (Join-Path $env:USERPROFILE ".codex") "config.toml"
  }
  if ($HOME) {
    return Join-Path (Join-Path $HOME ".codex") "config.toml"
  }
  if ($env:CODEX_HOME) {
    return Join-Path $env:CODEX_HOME "config.toml"
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
  $Source = Read-BundleServerConfig
  $Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")
  if (-not $Source.PSObject.Properties["mcpServers"]) {
    throw "claude_desktop_config.json does not contain mcpServers."
  }
  $Server = $Source.mcpServers.PSObject.Properties[$ServerName]
  if (-not $Server) {
    throw "claude_desktop_config.json does not contain server $ServerName."
  }
  return $Server.Value
}

function Build-CodexConfigSnippet {
  $Entry = Get-BundleServerEntry
  $Lines = @()
  $Lines += "# Generated by connect_mcp_client.ps1 from $BundleDir"
  $Lines += "# Re-run with -Target codex -InstallCodex after moving or unzipping the MCP bundle."
  $Lines += "[mcp_servers.$(Format-TomlKey $ServerName)]"
  $Lines += "command = $(Format-TomlString ([string]$Entry.command))"
  $Lines += "cwd = $(Format-TomlString $BundleDir)"
  $Lines += "args = ["
  foreach ($Arg in @($Entry.args)) {
    $Lines += "  $(Format-TomlString ([string]$Arg)),"
  }
  $Lines += "]"
  return ($Lines -join [Environment]::NewLine)
}

function Install-CodexConfig {
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
  if (Test-Path -LiteralPath $TargetPath) {
    $BackupPath = "$TargetPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
    Copy-Item -LiteralPath $TargetPath -Destination $BackupPath
    $Existing = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8
    Write-Host "Backup created: $BackupPath"
  }
  $RemovedNames = [System.Collections.Generic.List[string]]::new()
  $Pattern = "(?ms)^\[mcp_servers\.(?<name>[^\]]+)\]\r?\n.*?(?=^\[|\z)"
  $TomlLauncherPath = $LauncherPath.Replace("\", "\\")
  $TomlBundleDataDir = $BundleDataDir.Replace("\", "\\")
  $Clean = [regex]::Replace($Existing, $Pattern, {
    param($Match)
    $ExistingName = Normalize-TomlSectionName $Match.Groups["name"].Value
    $SameName = $ExistingName -eq $ServerName
    $SameBundle = $Match.Value.IndexOf($LauncherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $Match.Value.IndexOf($TomlLauncherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $Match.Value.IndexOf($BundleDataDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $Match.Value.IndexOf($TomlBundleDataDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $LegacyDefaultForSameProfile = $ServerName -ne "govreg-local" -and
      $ExistingName -eq "govreg-local" -and
      $GeneratedProfileId -and
      $Match.Value.IndexOf($GeneratedProfileId, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if ($SameName -or $SameBundle -or $LegacyDefaultForSameProfile) {
      $RemovedNames.Add($ExistingName)
      return ""
    }
    return $Match.Value
  }).TrimEnd()
  $Output = if ([string]::IsNullOrWhiteSpace($Clean)) { $Snippet } else { $Clean + [Environment]::NewLine + [Environment]::NewLine + $Snippet }
  Write-Utf8NoBom $TargetPath ($Output + [Environment]::NewLine)
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
    throw "Codex config verification failed after writing: $TargetPath"
  }
  $RemovedDuplicates = @($RemovedNames | Where-Object { $_ -and $_ -ne $ServerName } | Select-Object -Unique)
  if ($RemovedDuplicates.Count -gt 0) {
    Write-Host "Removed duplicate entries for this bundle: $($RemovedDuplicates -join ', ')"
  }
  Write-Host "Codex config updated: $TargetPath"
  Write-Host "Verified MCP server name and bundle paths: $ServerName"
  Write-Host "Restart Codex or reload MCP servers to pick up $ServerName."
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

function Install-ClaudeDesktopConfig {
  $Source = Read-BundleServerConfig
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

  if (Test-Path -LiteralPath $TargetPath) {
    $BackupPath = "$TargetPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
    Copy-Item -LiteralPath $TargetPath -Destination $BackupPath
    try {
      $Target = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
      throw "Existing Claude Desktop config is not valid JSON: $TargetPath. Backup created at $BackupPath. Fix the JSON first, or move the invalid file aside and rerun this installer. Common cause: pasting the whole generated JSON as a second top-level object instead of merging mcpServers. Original parser error: $($_.Exception.Message)"
    }
    Write-Host "Backup created: $BackupPath"
  } else {
    $Target = [pscustomobject]@{}
  }

  if (-not $Target.PSObject.Properties["mcpServers"]) {
    Add-Member -InputObject $Target -MemberType NoteProperty -Name "mcpServers" -Value ([pscustomobject]@{})
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
    $Target.mcpServers.PSObject.Properties |
      ForEach-Object { $_.Name } |
      Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
  )
  foreach ($ExistingName in $ExistingNames) {
    $ExistingServer = $Target.mcpServers.PSObject.Properties[$ExistingName].Value
    $ExistingArgs = @($ExistingServer.args)
    $SameName = $ExistingName -eq $ServerName
    $SameBundle = $ExistingArgs -contains $LauncherPath -or $ExistingArgs -contains $BundleDataDir
    $LegacyDefaultForSameProfile = $ServerName -ne "govreg-local" -and
      $ExistingName -eq "govreg-local" -and
      $GeneratedProfileId -and
      $ExistingArgs -contains $GeneratedProfileId
    if ($SameName -or $SameBundle -or $LegacyDefaultForSameProfile) {
      $Target.mcpServers.PSObject.Properties.Remove($ExistingName)
      $RemovedNames.Add($ExistingName)
    }
  }

  foreach ($Server in $Source.mcpServers.PSObject.Properties) {
    Add-Member -InputObject $Target.mcpServers -MemberType NoteProperty -Name $Server.Name -Value $Server.Value
  }

  Write-JsonUtf8NoBom $TargetPath $Target 50
  $WrittenTarget = Get-Content -LiteralPath $TargetPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $InstalledProperty = $WrittenTarget.mcpServers.PSObject.Properties[$ServerName]
  if (-not $InstalledProperty) {
    throw "Claude Desktop config verification failed after writing server ${ServerName}: $TargetPath"
  }
  $InstalledArgs = @($InstalledProperty.Value.args)
  if ($InstalledArgs -notcontains $LauncherPath -or $InstalledArgs -notcontains $BundleDataDir) {
    throw "Claude Desktop config verification failed after writing bundle paths: $TargetPath"
  }
  $RemovedDuplicates = @($RemovedNames | Where-Object { $_ -and $_ -ne $ServerName } | Select-Object -Unique)
  if ($RemovedDuplicates.Count -gt 0) {
    Write-Host "Removed duplicate Claude Desktop entries for this bundle: $($RemovedDuplicates -join ', ')"
  }
  Write-Host "Claude Desktop config updated: $TargetPath"
  Write-Host "Verified MCP server name and bundle paths: $ServerName"
  Write-Host "Restart Claude Desktop to load the MCP server."
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
    if (-not (Test-ClaudeDesktopConfig)) {
      return
    }
    if (-not (Run-LocalStdioDoctor)) {
      return
    }
    Install-ClaudeDesktopConfig
    return
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
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}" -Target claude-desktop -InstallClaudeDesktop' -f $PSCommandPath)
}

function Register-ClaudeCode {
  Show-Header
  if (-not (Run-LocalStdioDoctor)) {
    return
  }
  if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Warning "Claude Code CLI was not found on PATH."
    Write-Host "After installing Claude Code, run:"
    Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f (BundlePath 'claude_code_add_stdio.ps1'))
    return
  }
  Run-Script "claude_code_add_stdio.ps1"
  Write-Host "Claude Code registered user-scoped stdio MCP server."
}

function Show-Codex {
  Show-Header
  if ($InstallCodex) {
    if (-not (Run-LocalStdioDoctor)) {
      return
    }
    Install-CodexConfig
    return
  }
  try {
    Write-Host "Codex config path: $(Get-CodexConfigPath)"
  } catch {
    Write-Warning $_.Exception.Message
  }
  Write-Host "Generated snippet: $(BundlePath 'codex_config_snippet.toml')"
  Write-Host "To install/update automatically:"
  Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}" -Target codex -InstallCodex' -f $PSCommandPath)
}

function Show-ChatGptDesktop {
  Show-Header
  if ($InstallChatGptDesktopPlugin -or $InstallCodex) {
    if (-not (Run-LocalStdioDoctor)) {
      return
    }
    Install-ChatGptDesktopPlugin
    Run-Script "validate_client_config_smoke.ps1"
    Write-Host ""
    Write-Host "Plugin registration and MCP protocol validation completed."
    Write-Host "This still does not prove that the plugin is attached to the current conversation."
    Write-Host "Fully quit ChatGPT Desktop, start it again, and open a new conversation."
    Write-Host "Then select + > More > $ServerName, or mention @$ServerName."
    Write-Host "Verification prompt: @$ServerName MCP 연결 상태와 사용 가능한 규정 도구를 보여줘."
    return
  }
  Write-Host "Generated local plugin marketplace: $(Get-ChatGptDesktopPluginRoot)"
  Write-Host "Registration and conversation attachment are separate states."
  Write-Host "To register/update automatically:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Target chatgpt-desktop-local -InstallChatGptDesktopPlugin"
}

function Install-ChatGptDesktopPlugin {
  function Invoke-CodexPluginCli([string[]]$Arguments) {
    # codex.ps1 forwards native stderr as PowerShell error records. With the
    # bundle-wide Stop policy that would abort before we can inspect the CLI
    # exit code (including an expected "not installed" during cleanup).
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
      $ErrorActionPreference = "Continue"
      $CommandOutput = @(& codex @Arguments 2>&1)
      $CommandExitCode = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $PreviousErrorActionPreference
    }
    return [pscustomobject]@{
      ExitCode = $CommandExitCode
      Output = $CommandOutput
    }
  }

  $MarketplaceRoot = Get-ChatGptDesktopPluginRoot
  $PluginMcpPath = Get-ChatGptDesktopPluginMcpPath
  $PluginManifestPath = Get-ChatGptDesktopPluginManifestPath
  $MarketplaceManifest = Get-ChatGptDesktopMarketplaceManifestPath
  if (-not (Test-Path -LiteralPath $MarketplaceManifest)) {
    throw "Generated plugin marketplace is missing: $MarketplaceManifest"
  }
  if (-not (Test-Path -LiteralPath $PluginMcpPath)) {
    throw "Generated plugin MCP config is missing: $PluginMcpPath"
  }
  if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "Codex CLI was not found on PATH. Install/update ChatGPT Desktop with Codex support, then rerun this button."
  }

  $Source = Read-BundleServerConfig
  $Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")
  Write-JsonUtf8NoBom (BundlePath "claude_desktop_config.json") $Source 50
  $PluginSource = [ordered]@{ mcp_servers = $Source.mcpServers }
  Write-JsonUtf8NoBom $PluginMcpPath $PluginSource 50

  $PluginManifest = Read-StrictUtf8Json $PluginManifestPath
  $PluginMcp = Read-StrictUtf8Json $PluginMcpPath
  $Marketplace = Read-StrictUtf8Json $MarketplaceManifest
  if ($PluginManifest.name -ne $PluginName) { throw "Plugin manifest name mismatch: $PluginManifestPath" }
  if ([string]$PluginManifest.mcpServers -ne "./.mcp.json") { throw "Plugin manifest mcpServers must point to ./.mcp.json." }
  if ([string]$PluginManifest.version -notmatch '^0\.1\.0\+codex\.[0-9a-f]{12}$') { throw "Plugin manifest is missing the required cachebuster version." }
  if (-not $PluginMcp.mcp_servers.PSObject.Properties[$ServerName]) { throw "Plugin MCP config does not contain official mcp_servers entry $ServerName." }
  $MarketplacePlugin = @($Marketplace.plugins | Where-Object { $_.name -eq $PluginName })
  if ($MarketplacePlugin.Count -ne 1) { throw "Marketplace manifest does not contain exactly one $PluginName plugin entry." }
  $ExpectedPluginVersion = [string]$PluginManifest.version
  Update-BundleStatus @{
    plugin_manifest_validated = $true
    plugin_install_command_succeeded = $false
    plugin_discoverable = $false
    plugin_registered = $false
  }

  $PluginSelector = "$PluginName@$PluginMarketplaceName"
  $InstallMutex = New-Object System.Threading.Mutex($false, "Local\PRMCPBuilder-$PluginMarketplaceName")
  $InstallLockAcquired = $false
  try {
    try {
      $InstallLockAcquired = $InstallMutex.WaitOne([TimeSpan]::FromSeconds(30))
    } catch [System.Threading.AbandonedMutexException] {
      $InstallLockAcquired = $true
    }
    if (-not $InstallLockAcquired) {
      throw "Another $PluginMarketplaceName plugin installation is still running. Wait for it to finish, then retry."
    }

    $null = Invoke-CodexPluginCli @("plugin", "remove", $PluginSelector, "--json")
    $null = Invoke-CodexPluginCli @("plugin", "marketplace", "remove", $PluginMarketplaceName, "--json")
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
  Update-BundleStatus @{
    launcher_ready = $true
    plugin_install_command_succeeded = $true
    plugin_discoverable = $false
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
  $DiscoveredMarketplaceRoot = [string]$InstalledPlugin[0].marketplaceSource.source
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
    launcher_ready = $true
    plugin_install_command_succeeded = $true
    plugin_manifest_validated = $true
    plugin_discoverable = $true
    plugin_registered = $true
    desktop_tool_scan_verified = $false
    conversation_attachment_verified = $false
    conversation_attachment_unverified = $true
    end_to_end_verified = $false
  }
    Write-Host "Plugin registered in the unified ChatGPT/Codex plugin directory: $PluginSelector ($ExpectedPluginVersion)"
    Write-Host "Registration is complete; attachment to the current conversation remains unverified."
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
  Write-Host "Then open ChatGPT Settings > Apps/Connectors > Create and register the connector URL."
  Write-Host "Choose the matching authentication method, click Scan Tools, and verify get_index_status is present."
  Write-Host "Set MCP_AUTH_TOKEN in the approved runtime environment before starting or validating the HTTP endpoint."
  Write-Host "Validate the deployed endpoint with:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$((BundlePath 'validate_chatgpt_remote_mcp.ps1'))`""
  Write-Host "After creating the app, fully restart ChatGPT Desktop and open a new conversation."
  Write-Host "Select + > More > $ServerName, or mention @$ServerName."
  Write-Host "Verification prompt: @$ServerName MCP 연결 상태와 사용 가능한 규정 도구를 보여줘."
  Start-Process "https://chatgpt.com/"
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
  Write-Host "Copy mcp_servers, tools, and betas from claude_api_fragment.json into the Messages API request."
}

function Show-Menu {
  Show-Header
  Write-Host "Choose a target:"
  Write-Host "  0. Install/check local package commands"
  Write-Host "  1. Claude Desktop local stdio"
  Write-Host "  2. Claude Code local stdio"
  Write-Host "  3. Codex CLI local stdio (compatibility)"
  Write-Host "  4. ChatGPT Desktop local plugin (stdio)"
  Write-Host "  5. ChatGPT remote MCP (streamable HTTP)"
  Write-Host "  6. ChatGPT Secure MCP Tunnel"
  Write-Host "  7. Claude API HTTPS MCP connector"
  Write-Host "  8. Doctor/readiness check"
  $Choice = Read-Host "Target"
  switch ($Choice) {
    "0" { Install-LocalPackage }
    "1" { Show-ClaudeDesktop }
    "2" { Register-ClaudeCode }
    "3" { Show-Codex }
    "4" { Show-ChatGptDesktop }
    "5" { Show-ChatGptHttps }
    "6" { Show-ChatGptTunnel }
    "7" { Show-ClaudeApi }
    "8" { Run-Doctor }
    default { throw "Unknown choice: $Choice" }
  }
}

if ($InstallPackage) {
  Install-LocalPackage
}

switch ($Target) {
  "menu" { Show-Menu }
  "install" { Install-LocalPackage }
  "claude-desktop" { Show-ClaudeDesktop }
  "claude-code" { Register-ClaudeCode }
  "codex" { Show-Codex }
  "chatgpt-desktop-local" { Show-ChatGptDesktop }
  "chatgpt-remote" { Show-ChatGptHttps }
  "chatgpt-desktop" { Show-ChatGptDesktop }
  "chatgpt-https" { Show-ChatGptHttps }
  "chatgpt-tunnel" { Show-ChatGptTunnel }
  "claude-api" { Show-ClaudeApi }
  "doctor" { Run-Doctor }
}
'''
    return (
        script.replace("__SERVER_NAME__", server_name)
        .replace("__PLUGIN_NAME__", plugin_name)
        .replace("__PLUGIN_MARKETPLACE_NAME__", marketplace_name)
        .replace("__EMBEDDED_CLAUDE_DESKTOP_CONFIG_BASE64__", embedded_config_base64)
        .replace(
            "__LOCAL_STDIO_DOCTOR_ARGS__",
            _powershell_array_literal(local_stdio_doctor_args or []),
        )
    )


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
            tool_profile="full",
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
            claude_code_http_args.extend(["--header", "Authorization: Bearer $env:" + remote_auth_token_env])
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
                "Run against the actual full-profile server after starting it; synthetic smoke does not validate "
                "the real tenant DB. For the full remote profile, validate get_index_status, search, and fetch."
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
            "transport": "stdio",
            "plugin_registration": "generated_local_marketplace",
            "server": chatgpt_desktop_local,
            "conversation_attachment_unverified": True,
            "verification_prompt": f"@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.",
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
            "tool_profile": "full",
            "auth": _remote_auth_summary(remote_auth_token_env),
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
            "verification_tools": ["get_index_status"],
            "auth_required": True,
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
    min_visible_records: int = 1,
) -> dict[str, Any]:
    connector_url = _remote_connector_url(public_url=public_url)
    https_endpoint_ready = bool(connector_url and connector_url.startswith("https://"))
    missing = []
    if not connector_url:
        missing.append("public_url_https_mcp_endpoint")
    elif not https_endpoint_ready:
        missing.append("public_url_must_use_https")
    return {
        "profile": "chatgpt-remote",
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
            "location": "ChatGPT Settings > Apps/Connectors > Create",
            "connector_url": connector_url,
            "requires_reachable_https": True,
            "https_endpoint_ready": https_endpoint_ready,
            "recommended_description": (
                "Search and fetch approved local regulation evidence from the institution's MCP server."
            ),
            "authentication_required": True,
            "authentication_note": "Protect the HTTPS /mcp endpoint with an approved reverse proxy, OAuth flow, or bearer-token gateway.",
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
        "server_auth": _remote_auth_summary(remote_auth_token_env),
        "compatible_tools": [
            "search",
            "fetch",
            "list_documents",
            "list_regulations",
            "get_regulation_toc",
            "get_regulation_article",
            "get_regulation_history",
            "get_article",
            "get_table",
            "compare_versions",
            "get_citation",
            "get_index_status",
        ],
        "connection_steps": [
            "Run the HTTP MCP server from server_start.",
            "Set the bearer token environment variable or use an approved authenticated reverse proxy.",
            "Expose the /mcp endpoint through an approved HTTPS URL.",
            "Create a ChatGPT app/connector with connector_url.",
            "Click Scan Tools and verify get_index_status is discovered before creating the app.",
            "Ask ChatGPT to search first, then fetch returned result IDs for evidence.",
        ],
        "notes": [
            "ChatGPT cannot connect directly to a local MCP server; use reachable HTTPS or Secure MCP Tunnel.",
            "Search and fetch are no longer mandatory for custom MCP apps, but remain available for evidence workflows.",
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
        tool_profile="full",
    )
    stdio_args = _with_no_warm_cache(stdio_args)
    mcp_command = _powershell_command("reg-rag-mcp-server", stdio_args)
    init_args = [
        "init",
        "--sample",
        "sample_mcp_stdio_local",
        "--profile",
        profile,
        "--tunnel-id",
        f"$env:{tunnel_id_env}",
        "--mcp-command",
        mcp_command,
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
            'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
            'function Assert-EnvVar([string]$Name) { $Value = [Environment]::GetEnvironmentVariable($Name); if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "<*>") { throw "$Name must be set to an approved non-placeholder value before running this script." } }',
            'Assert-Command "reg-rag-mcp-doctor"',
            'Assert-Command "reg-rag-mcp-server"',
            'Assert-Command "tunnel-client"',
            f'Assert-EnvVar "{control_plane_api_key_env}"',
            f'Assert-EnvVar "{tunnel_id_env}"',
            _powershell_command("reg-rag-mcp-doctor", readiness_args),
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            _powershell_command("tunnel-client", init_args),
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
            "In ChatGPT connector/app settings, choose Tunnel under Connection and select the tunnel_id.",
            "Run Scan Tools and verify the full read-only profile includes get_index_status.",
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
    mcp_servers = []
    if connector_url:
        server_definition: dict[str, Any] = {
            "type": "url",
            "url": connector_url,
            "name": server_name,
        }
        if remote_auth_token_env:
            server_definition["authorization_token_env"] = remote_auth_token_env
        mcp_servers.append(server_definition)
    return {
        "mcp_servers": mcp_servers,
        "tools": [
            {
                "type": "mcp_toolset",
                "mcp_server_name": server_name,
                "default_config": {"enabled": True},
            }
        ],
        "betas": ["mcp-client-2025-11-20"],
        "ready": bool(connector_url),
        "missing": [] if connector_url else ["public_url_https_mcp_endpoint"],
        "connection_steps": [
            "Run the HTTP MCP server from server_start.",
            "Set the bearer token environment variable or use an approved authenticated reverse proxy.",
            "Expose the /mcp endpoint through an approved HTTPS URL.",
            "Copy mcp_servers, tools, and betas into the Claude Messages API request.",
            "Add authorization_token only if the HTTP MCP deployment enforces matching authentication.",
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
            "Add authorization_token only after the MCP HTTP deployment has matching authentication.",
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
    connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | {str(item['ready']).lower()} | `{item['primary_file']}` |"
        for item in _setup_bundle_connections(config)
    )
    return f"""# MCP Connection Bundle

This folder contains generated setup files for the `{server_name}` MCP server.

## Fast Path

For Windows operators who should not run PowerShell directly:

1. Quit every AI app you want to connect.
2. For the ChatGPT Desktop local plugin profile, double-click
   `{files.get("connect_chatgpt_desktop_bat", SETUP_BUNDLE_FILES["connect_chatgpt_desktop_bat"])}`. For Codex CLI, Claude Desktop, or Claude Code use
   `{files.get("connect_codex_bat", SETUP_BUNDLE_FILES["connect_codex_bat"])}`,
   `{files.get("connect_claude_desktop_bat", SETUP_BUNDLE_FILES["connect_claude_desktop_bat"])}`, or
   `{files.get("connect_claude_code_bat", SETUP_BUNDLE_FILES["connect_claude_code_bat"])}`.
3. If the window shows no error, restart the AI app.
4. In a new ChatGPT Desktop conversation, select `{server_name}` from `+ > More` or mention it, then ask:
   `@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.`

Use `{files.get("doctor_bat", SETUP_BUNDLE_FILES["doctor_bat"])}` when you only want to check the bundle connection state.
Use `{files.get("usage_guide_bat", SETUP_BUNDLE_FILES["usage_guide_bat"])}` for client-specific verification commands and named invocation examples.
The ChatGPT Desktop button registers the generated local plugin marketplace. Manual Codex CLI MCP values remain in
`{files.get("codex_plugin_guide", SETUP_BUNDLE_FILES["codex_plugin_guide"])}` for compatibility only.
The `.bat` files are thin double-click launchers around the generated PowerShell scripts.
If you move or rename this folder, rerun the connection button from the new location so the client config is
updated to the new launcher and `data` paths.
Regenerating and reconnecting with the same MCP name replaces the existing client entry. The regenerated bundle contains
the current approved corpus, so added and revised chunks remain available through the same MCP name.

Run `{files.get("connect", SETUP_BUNDLE_FILES["connect"])}` and choose doctor first, then Codex, ChatGPT Desktop, Claude Desktop, Claude Code, ChatGPT HTTPS,
ChatGPT Secure MCP Tunnel, Claude API, or doctor. For non-interactive setup, pass `-Target claude-code`,
`-Target chatgpt-desktop-local -InstallChatGptDesktopPlugin`, `-Target chatgpt-remote`,
`-Target chatgpt-tunnel`, `-Target claude-api`, or
`-Target claude-desktop -InstallClaudeDesktop`. Use `-Target claude-desktop -ValidateClaudeDesktop`
first when Claude Desktop reports a JSON parsing error.

Check `{files.get("bundle_status", SETUP_BUNDLE_FILES["bundle_status"])}` first when a client appears slow to recognize the MCP.
It is regenerated from `data/mcp_runtime_manifest.json` and shows the current approved record count and `recommended_smoke_query`.
`plugin_registered=true` requires strict companion JSON validation, a successful install command, and an enabled entry in
`codex plugin list --json` whose cachebuster version and marketplace source match this bundle. It still does not mean the current conversation attached the plugin. `direct_stdio_verified` records
the generated launcher's MCP chain, while `desktop_tool_scan_verified` and `conversation_attachment_verified` remain
separate. `end_to_end_verified=true` is reserved for a successful MCP `initialize`, `tools/list`, and `get_index_status` chain.
Older `mcp_connection_readiness.json` and `mcp_transport_smoke.json` run outputs are cleared on generation so stale evidence does not
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

| Client | Mode | Ready | Primary file |
| --- | --- | --- | --- |
{connection_rows}

## Local Desktop and CLI

1. Run `{files.get("doctor_bat", SETUP_BUNDLE_FILES["doctor_bat"])}` before connecting a local stdio client. It verifies the real runtime visibility gate, including indexed records, non-smoke data, and append-only approval journal coverage.
2. For Claude Desktop, double-click `{files.get("connect_claude_desktop_bat", SETUP_BUNDLE_FILES["connect_claude_desktop_bat"])}`. For manual setup, merge `{files.get("claude_desktop", SETUP_BUNDLE_FILES["claude_desktop"])}` into the
   Claude Desktop config file. The generated file already contains an `mcpServers` object.
   Run `{files.get("connect", SETUP_BUNDLE_FILES["connect"])}` with `-Target claude-desktop -ValidateClaudeDesktop`
   to validate the existing Claude Desktop JSON before merging. Automatic install runs the doctor gate before writing the config.
3. For Claude Code, double-click `{files.get("connect_claude_code_bat", SETUP_BUNDLE_FILES["connect_claude_code_bat"])}`. For manual setup, run `{files.get("claude_code_stdio", SETUP_BUNDLE_FILES["claude_code_stdio"])}` in PowerShell.
   The script runs the doctor gate, replaces legacy local/user entries, registers the local stdio server with
   `--scope user`, and verifies it with `claude mcp get` so it remains available outside the bundle directory.
4. For ChatGPT Desktop local execution, double-click `{files.get("connect_chatgpt_desktop_bat", SETUP_BUNDLE_FILES["connect_chatgpt_desktop_bat"])}`. It registers the generated marketplace and `{server_name}` plugin. Fully quit and restart ChatGPT Desktop, open a new conversation, then select the plugin from `+ > More` or mention `@{server_name}`. Direct local stdio availability in a ChatGPT conversation is product-surface dependent; when it is unavailable, use the separate `chatgpt-remote` HTTPS or Secure MCP Tunnel profile.
   The plugin follows the official `.codex-plugin/plugin.json` to `./.mcp.json` layout, with an `mcp_servers` container in `.mcp.json`.
   Use `{files.get("connect_codex_bat", SETUP_BUNDLE_FILES["connect_codex_bat"])}` for direct Codex CLI compatibility. For manual Codex setup, paste `{files.get("codex_config", SETUP_BUNDLE_FILES["codex_config"])}` into `$HOME\\.codex\\config.toml`
   or replace the existing `[mcp_servers.{server_name}]` block. The snippet points `--data-dir` at this bundle's
   `data` directory and includes `--no-warm-cache` plus the generated storage-mode flag. Local stdio client
   configs launch `{files.get("stdio_launcher", SETUP_BUNDLE_FILES["stdio_launcher"])}` through PowerShell instead
   of calling `reg-rag-mcp-server` directly. When the bundle is inside a source checkout, the launcher uses that
   checkout before any older global console command; standalone bundles fall back to the installed command.
5. Validate generated Codex and Claude Desktop local stdio configs with `{files.get("client_config_smoke", SETUP_BUNDLE_FILES["client_config_smoke"])}`.
   It launches MCP through the exact generated `command`/`args` and completes `initialize`, `tools/list`,
   `get_index_status`, `search`, and `fetch`.
6. Validate the bundled runtime transport with `{files.get("validate", SETUP_BUNDLE_FILES["validate"])}`. It reads `data/mcp_runtime_manifest.json` and uses the generated `recommended_smoke_query` when present.
7. Real runtime visibility audit command used by the doctor gate:

```powershell
{quickstart.get("copy_paste", {}).get("audit_index_visibility_ps", "reg-rag-mcp-index-visibility --data-dir <runtime> --tenant-id <tenant> --fail-on-issue")}
```

## ChatGPT

The `chatgpt-desktop-local` profile registers a local plugin for the unified desktop plugin directory. Registration and
conversation attachment are separate states: fully restart the app, then use `+ > More > {server_name}` or `@{server_name}`.
ChatGPT remote apps need a reachable HTTPS `/mcp` endpoint; ChatGPT does not directly connect to a localhost MCP endpoint.
Use `{files.get("run_chatgpt", SETUP_BUNDLE_FILES["run_chatgpt"])}` on the server for the full read-only tool profile, then register the URL from
`{files.get("chatgpt", SETUP_BUNDLE_FILES["chatgpt"])}` in ChatGPT Settings > Apps/Connectors.

Ready: `{str(chatgpt_ready).lower()}`. If false, regenerate with `--public-url https://your-host.example/mcp`.

For private or internal servers, use `{files.get("openai_tunnel", SETUP_BUNDLE_FILES["openai_tunnel"])}` as the
OpenAI Secure MCP Tunnel template. It keeps the MCP server inside the local network and lets ChatGPT select the
tunnel in connector/app settings.

## Claude API

Claude API needs an HTTPS URL MCP server definition. Copy `{files.get("claude_api", SETUP_BUNDLE_FILES["claude_api"])}` into
the Messages API request fields `mcp_servers`, `tools`, and `betas`.

Ready: `{str(claude_api_ready).lower()}`. If false, regenerate with `--public-url https://your-host.example/mcp`.

## Korean Text Display

Bundle JSON and Markdown files are written as UTF-8. If Korean document names or chunk IDs look like `蹂꾪몴`
or replacement characters in Windows PowerShell, the file is usually being displayed as CP949 instead of UTF-8;
it is not evidence that the MCP data is corrupted. Inspect files with `Get-Content -Encoding UTF8 ...`,
`chcp 65001`, or a UTF-8-aware editor/browser/GitHub view. Do not regenerate data or change chunk IDs only for
this display symptom because approval journals and vector IDs are keyed by those IDs.

## Security

Do not expose HTTP MCP without authentication or approved network controls. Generated HTTP and tunnel scripts do
not store secrets. Set `MCP_AUTH_TOKEN`, `CONTROL_PLANE_API_KEY`, or `OPENAI_TUNNEL_ID` in the approved runtime
environment before launch. Generated HTTP commands run `reg-rag-mcp-doctor --fail-on-warning` before starting the server.

## Warnings

{warning_block}

## Official References

- ChatGPT and Codex Plugins: https://help.openai.com/en/articles/20001256-plugins-in-codex
- ChatGPT developer mode and MCP apps: https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta%29
- MCP Streamable HTTP transport: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Claude API MCP connector: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
- Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp
"""



def _setup_bundle_readme_ko(*, config: dict[str, Any], files: dict[str, str], server_name: str) -> str:
    chatgpt_ready = bool((config.get("chatgpt_remote") or config.get("chatgpt") or {}).get("ready"))
    claude_api_ready = bool((config.get("claude_api") or {}).get("ready"))
    quickstart = config.get("quickstart") if isinstance(config.get("quickstart"), dict) else {}
    warnings = quickstart.get("warnings") if isinstance(quickstart.get("warnings"), list) else []
    warning_block = "\n".join(f"- {warning}" for warning in warnings) if warnings else "- 없음."
    connection_rows = "\n".join(
        f"| {item['client']} | {item['mode']} | {str(item['ready']).lower()} | `{item['primary_file']}` |"
        for item in _setup_bundle_connections(config)
    )
    return f"""# MCP 연결 번들

이 폴더는 `{server_name}` MCP 서버를 ChatGPT Desktop 로컬 플러그인, ChatGPT 원격 MCP, Codex CLI, Claude Desktop, Claude Code, Claude API에 연결하기 위한 생성 파일 묶음입니다.

## 가장 빠른 경로

비개발자용 Windows 사용자는 `.ps1`을 직접 실행하지 말고 생성된 연결 버튼을 더블클릭합니다.

1. 사용할 AI 앱을 모두 종료합니다.
2. ChatGPT Desktop 로컬 플러그인은 `{files.get('connect_chatgpt_desktop_bat', SETUP_BUNDLE_FILES['connect_chatgpt_desktop_bat'])}`를 더블클릭합니다. Codex CLI, Claude Desktop, Claude Code는 각각 `{files.get('connect_codex_bat', SETUP_BUNDLE_FILES['connect_codex_bat'])}`, `{files.get('connect_claude_desktop_bat', SETUP_BUNDLE_FILES['connect_claude_desktop_bat'])}`, `{files.get('connect_claude_code_bat', SETUP_BUNDLE_FILES['connect_claude_code_bat'])}`를 사용합니다.
3. 창에 오류가 없으면 AI 앱을 다시 실행합니다.
4. ChatGPT Desktop 새 대화에서 `+ > 더 보기 > {server_name}`을 선택하거나 `@{server_name}`을 멘션한 뒤 `@{server_name} MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.`라고 입력합니다.

연결 상태만 확인할 때는 `{files.get('doctor_bat', SETUP_BUNDLE_FILES['doctor_bat'])}`를 더블클릭합니다.
클라이언트별 확인 명령과 이름 기반 호출 예시는 `{files.get('usage_guide_bat', SETUP_BUNDLE_FILES['usage_guide_bat'])}`를 실행해 확인합니다.
ChatGPT Desktop BAT는 생성된 로컬 플러그인 마켓플레이스를 자동 등록합니다. `{files.get('codex_plugin_guide', SETUP_BUNDLE_FILES['codex_plugin_guide'])}`는 Codex CLI 수동 호환 설정용입니다.
이 `.bat` 파일들은 내부에서 생성된 PowerShell 스크립트를 대신 실행하는 안전한 연결 버튼입니다.
이 폴더를 이동하거나 이름을 바꿨다면 새 위치에서 연결 버튼을 다시 실행합니다. 그러면 AI 앱 설정의 실행 파일과 `data` 경로가 새 폴더 기준으로 교체됩니다.
같은 MCP 이름으로 다시 생성하고 연결 버튼을 실행하면 기존 설정을 중복 추가하지 않고 교체합니다. 새 번들은 현재 승인된 전체 corpus를 다시 만들기 때문에 추가·개정 청크가 같은 MCP 이름에 반영됩니다.

먼저 다음 명령에서 doctor를 실행해 실제 런타임 visibility gate를 확인한 뒤 연결할 클라이언트를 선택합니다.

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}"
```

클라이언트가 MCP를 늦게 인식하거나 엉뚱한 상태를 보여주면 먼저 `{files.get('bundle_status', SETUP_BUNDLE_FILES['bundle_status'])}`를 확인합니다.
이 파일은 `data/mcp_runtime_manifest.json` 기준으로 다시 생성되며 현재 승인 record 수와 `recommended_smoke_query`를 보여줍니다.
예전 실행 결과인 `mcp_connection_readiness.json`, `mcp_transport_smoke.json`은 번들 생성 시 정리해서 현재 상태처럼 보이지 않게 합니다.
클라이언트 설정을 병합하거나 설치한 뒤에도 예전 런타임을 보는 것 같으면 설치된 설정 파일까지 doctor로 확인합니다.
예: `reg-rag-mcp-doctor --client-profile bundle --bundle-dir . --allow-local-only-bundle --codex-config $HOME\\.codex\\config.toml`
또는 Windows Claude Desktop은 `--claude-desktop-config "$env:APPDATA\\Claude\\claude_desktop_config.json"`를 추가합니다.
이 검사는 stale `--data-dir`, `--no-warm-cache` 누락, 저장소 모드 플래그 불일치를 잡습니다.

`reg-rag-mcp-*` 콘솔 명령이 보이지 않으면 먼저 설치 보조 스크립트를 실행합니다. 번들이 저장소 안에서 생성된 경우 `pip install -e .`를 자동으로 실행하고, 저장소 밖에서 압축을 푼 번들에 wheel 파일이 포함되어 있으면 해당 wheel을 설치합니다.

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('install', SETUP_BUNDLE_FILES['install'])}"
```

저장소 없이 번들 하나만 전달해야 하면 `python -m build --sdist --wheel` 실행 후 `reg-rag-mcp-config --client-profile bundle --include-wheel --zip-out ...`로 wheel 포함 zip을 생성합니다.

비대화형 실행 예시:

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -Target claude-code
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -Target chatgpt-desktop-local -InstallChatGptDesktopPlugin
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -Target chatgpt-remote
powershell -ExecutionPolicy Bypass -File "{files.get('connect', SETUP_BUNDLE_FILES['connect'])}" -Target chatgpt-tunnel
```

## 연결 선택지

| 클라이언트 | 방식 | 준비 상태 | 주요 파일 |
| --- | --- | --- | --- |
{connection_rows}

## Claude 연결

- 사전 진단: `{files.get('doctor_bat', SETUP_BUNDLE_FILES['doctor_bat'])}`를 먼저 실행합니다. indexed record, smoke 문서 배제, append-only approval journal coverage가 통과해야 합니다.
- Claude Desktop: `{files.get('connect_claude_desktop_bat', SETUP_BUNDLE_FILES['connect_claude_desktop_bat'])}`를 더블클릭합니다. 수동 설정이 필요할 때만 `{files.get('claude_desktop', SETUP_BUNDLE_FILES['claude_desktop'])}`의 `mcpServers`를 Claude Desktop 설정에 병합합니다. 자동 병합은 doctor gate를 통과한 뒤 `connect_mcp_client.ps1 -Target claude-desktop -InstallClaudeDesktop`로 수행합니다. JSON 파싱 오류가 났다면 먼저 `connect_mcp_client.ps1 -Target claude-desktop -ValidateClaudeDesktop`으로 기존 설정 파일을 검증합니다.
- Claude Code: `{files.get('connect_claude_code_bat', SETUP_BUNDLE_FILES['connect_claude_code_bat'])}`를 더블클릭하면 로컬 stdio MCP를 사용자 범위(`--scope user`)에 등록하고 `claude mcp get`으로 확인합니다. 따라서 생성 폴더 밖의 다른 프로젝트에서도 같은 사용자에게 보입니다. 수동 설정이 필요할 때만 `{files.get('claude_code_stdio', SETUP_BUNDLE_FILES['claude_code_stdio'])}`를 실행합니다.
- Claude API: `{files.get('claude_api', SETUP_BUNDLE_FILES['claude_api'])}`의 `mcp_servers`, `tools`, `betas`를 Messages API 요청에 넣습니다. Ready: `{str(claude_api_ready).lower()}`.
- 클라이언트 설정 smoke: `{files.get('client_config_smoke', SETUP_BUNDLE_FILES['client_config_smoke'])}`를 실행하면 생성된 Codex/Claude Desktop 설정 파일의 `command`/`args` 그대로 MCP를 띄우고 `list_tools`, `search`, `fetch`를 확인합니다.
- 런타임 smoke 검증: `{files.get('validate', SETUP_BUNDLE_FILES['validate'])}`를 실행하면 `data/mcp_runtime_manifest.json`의 `recommended_smoke_query`를 읽어 실제 번들 데이터로 `search`/`fetch`를 확인합니다.

## ChatGPT Desktop 로컬 플러그인 및 Codex CLI 연결

- ChatGPT Desktop 로컬 플러그인: `{files.get('connect_chatgpt_desktop_bat', SETUP_BUNDLE_FILES['connect_chatgpt_desktop_bat'])}`를 더블클릭하면 생성된 마켓플레이스와 `{server_name}` 플러그인을 자동 등록합니다. 플러그인은 공식 `.codex-plugin/plugin.json` → `./.mcp.json` 구조와 `.mcp.json`의 `mcp_servers` 컨테이너를 사용합니다. 앱을 완전히 종료하고 다시 실행한 뒤 Plugins를 새로고침하고, 새 대화에서 `+ > 더 보기 > {server_name}`을 선택하거나 `@{server_name}`을 멘션합니다. 플러그인 등록 완료와 현재 대화에 도구가 첨부된 상태는 별개입니다.
- Codex CLI 호환: `{files.get('connect_codex_bat', SETUP_BUNDLE_FILES['connect_codex_bat'])}`를 더블클릭합니다. 수동 설정이 필요할 때만 `{files.get('codex_config', SETUP_BUNDLE_FILES['codex_config'])}`의 TOML 블록을 `$HOME\\.codex\\config.toml`에 붙여 넣거나 기존 `[mcp_servers.{server_name}]` 블록과 교체합니다.
- 이 스니펫은 `--data-dir`을 이 번들의 `data` 폴더로 고정하고 `--no-warm-cache`와 저장소 모드 플래그를 포함합니다. 그래서 예전 번들이나 다른 MCP 서버를 물고 느리게 인식하는 문제를 줄입니다.
- 로컬 stdio 설정은 `reg-rag-mcp-server`를 직접 부르지 않고 `{files.get('stdio_launcher', SETUP_BUNDLE_FILES['stdio_launcher'])}`를 PowerShell로 실행합니다. 번들이 저장소 checkout 안에 있으면 현재 checkout의 `scripts\\run_regulation_mcp.py`를 오래된 전역 콘솔 명령보다 먼저 실행하고, 독립 배포 번들은 PATH의 `reg-rag-mcp-server`로 fallback합니다. 그래도 찾지 못하면 `install_local_package.ps1`을 한 번 실행하라는 오류를 냅니다.
- 붙여 넣은 뒤에는 `reg-rag-mcp-doctor --client-profile bundle --bundle-dir . --allow-local-only-bundle --codex-config $HOME\\.codex\\config.toml`로 실제 설치된 설정을 확인합니다.

## ChatGPT 연결

- ChatGPT Desktop 로컬 방식: 생성 플러그인을 등록한 뒤 완전히 재시작하고 새 대화에서 선택하거나 멘션합니다. 제품 화면이 로컬 stdio 플러그인을 ChatGPT 대화에 노출하지 않으면 원격 HTTPS 또는 Secure MCP Tunnel 방식을 사용합니다.
- HTTPS 방식: `{files.get('run_chatgpt', SETUP_BUNDLE_FILES['run_chatgpt'])}`로 전체 읽기 전용 도구 MCP 서버를 실행하고, `{files.get('chatgpt', SETUP_BUNDLE_FILES['chatgpt'])}`의 `connector_url`을 ChatGPT Settings > Apps/Connectors에 등록합니다. ChatGPT는 localhost MCP에 직접 연결하지 않습니다. Ready: `{str(chatgpt_ready).lower()}`.
- 상태 판정: `plugin_registered=true`는 companion JSON 검증, 설치 명령 성공, `codex plugin list --json`의 활성 플러그인 cachebuster 버전과 공급 마켓플레이스 경로가 현재 번들과 정확히 일치하는 경우에만 기록합니다. `direct_stdio_verified`, `desktop_tool_scan_verified`, `conversation_attachment_verified`는 서로 별도이며, `end_to_end_verified=true`는 실제 `initialize`, `tools/list`, `get_index_status`가 모두 성공한 경우에만 기록합니다.
- 내부망/비공개 방식: 외부 inbound 방화벽을 열지 않아야 하면 `{files.get('openai_tunnel', SETUP_BUNDLE_FILES['openai_tunnel'])}`를 사용합니다. `CONTROL_PLANE_API_KEY`와 `OPENAI_TUNNEL_ID`는 파일에 쓰지 말고 실행 환경변수로 설정합니다.

## 사전 진단

```powershell
powershell -ExecutionPolicy Bypass -File "{files.get('doctor', SETUP_BUNDLE_FILES['doctor'])}"
```

실제 운영 런타임에 승인 record가 보이고 smoke 문서가 섞이지 않았는지는 다음 명령으로 확인합니다.

```powershell
{quickstart.get("copy_paste", {}).get("audit_index_visibility_ps", "reg-rag-mcp-index-visibility --data-dir <runtime> --tenant-id <tenant> --fail-on-issue")}
```

## 보안 주의

- 토큰, API 키, 터널 ID 같은 승인값을 파일에 저장하지 마십시오.
- 원격 HTTP/Tunnel 실행 전에 `MCP_AUTH_TOKEN`, `CONTROL_PLANE_API_KEY`, `OPENAI_TUNNEL_ID`를 승인된 환경변수로 설정하십시오.
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

- ChatGPT와 Codex 플러그인: https://help.openai.com/en/articles/20001256-plugins-in-codex
- ChatGPT 개발자 모드와 MCP 앱: https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta%29
- MCP Streamable HTTP 전송 규격: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Claude API MCP connector: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
- Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp
"""


def _powershell_http_command(
    command: str,
    args: list[object],
    token_env: str | None,
    *,
    doctor_args: list[object] | None = None,
) -> str:
    command_line = _powershell_command(command, args)
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'function Assert-EnvVar([string]$Name) { $Value = [Environment]::GetEnvironmentVariable($Name); if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "<*>") { throw "$Name must be set to an approved non-placeholder value before running this script." } }',
        'Assert-Command "reg-rag-mcp-doctor"',
        f'Assert-Command "{command}"',
    ]
    if token_env:
        lines.append(f'Assert-EnvVar "{token_env}"')
    if doctor_args:
        lines.append(_powershell_command("reg-rag-mcp-doctor", doctor_args))
        lines.append("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }")
    lines.append(command_line)
    return "\n".join(lines)


def _powershell_stdio_guarded_command(
    command: str,
    args: list[object],
    *,
    doctor_args: list[object],
    prequoted_indexes: set[int] | None = None,
) -> str:
    command_line = _powershell_command(command, args, prequoted_indexes=prequoted_indexes)
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'Assert-Command "reg-rag-mcp-doctor"',
        f'Assert-Command "{command}"',
        _powershell_command("reg-rag-mcp-doctor", doctor_args),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        command_line,
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
        '$DoctorReport = Join-Path $BundleDir "mcp_connection_readiness.json"',
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'Assert-Command "reg-rag-mcp-doctor"',
        _powershell_command("reg-rag-mcp-doctor", doctor_args),
        'Write-Host "Doctor report: $DoctorReport"',
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
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
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'Assert-Command "reg-rag-mcp-transport-smoke"',
        '$SmokeArgs = @("--data-dir", $BundleDataDir, "--tenant-id", "__TENANT_ID__", "--skip-preparation", "--query", $Query, "--out-json", $SmokeReport, "--fail-on-issue", "__STORAGE_FLAG__")',
        '$SmokeHelp = (& reg-rag-mcp-transport-smoke --help 2>&1 | Out-String)',
        'if ($SmokeHelp -match "--no-warm-cache") { $SmokeArgs += "--no-warm-cache" }',
        'Write-Host "Runtime smoke query: $Query"',
        '& reg-rag-mcp-transport-smoke @SmokeArgs',
        'Write-Host "Transport smoke report: $SmokeReport"',
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
    ]
    return "\n".join(lines).replace("__TENANT_ID__", tenant_id).replace("__STORAGE_FLAG__", storage_flag)


def _powershell_bundle_client_config_smoke_script(*, server_name: str) -> str:
    plugin_name = _normalized_plugin_name(server_name)
    lines: list[str] = [
        '$ErrorActionPreference = "Stop"',
        *_powershell_bundle_data_dir_lines(),
        '$ServerName = "__SERVER_NAME__"',
        '$SmokeReport = Join-Path $BundleDir "mcp_client_config_smoke.json"',
        '$CodexConfig = Join-Path $BundleDir "codex_config_snippet.toml"',
        '$ClaudeDesktopConfig = Join-Path $BundleDir "claude_desktop_config.json"',
        '$PluginMcpConfig = Join-Path $BundleDir "chatgpt-desktop-local-plugin\\plugins\\__PLUGIN_NAME__\\.mcp.json"',
        '$BundleStatus = Join-Path $BundleDir "bundle_status.json"',
        '$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"',
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'function Write-Utf8NoBom([string]$LiteralPath, [string]$Value) { $Utf8NoBom = New-Object System.Text.UTF8Encoding($false); [System.IO.File]::WriteAllText($LiteralPath, $Value, $Utf8NoBom) }',
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
        '  if (-not $Plugin.mcp_servers) { throw "Generated ChatGPT Desktop plugin config is missing official mcp_servers." }',
        '  $ServerProperty = $Plugin.mcp_servers.PSObject.Properties[$ServerName]',
        '  if (-not $ServerProperty) { throw "Generated ChatGPT Desktop plugin config is missing MCP server $ServerName." }',
        '  $Server = $ServerProperty.Value',
        '  $Server.command = "powershell.exe"',
        '  $Server.args = @(Set-McpBundlePaths @($Server.args))',
        '  Write-JsonUtf8NoBom $PluginMcpConfig $Plugin 40',
        '  return @($Server.args)',
        '}',
        'Assert-Command "reg-rag-mcp-client-config-smoke"',
        'if (-not (Test-Path -LiteralPath $CodexConfig)) { throw "Missing generated Codex config snippet: $CodexConfig" }',
        'if (-not (Test-Path -LiteralPath $ClaudeDesktopConfig)) { throw "Missing generated Claude Desktop config: $ClaudeDesktopConfig" }',
        'if (-not (Test-Path -LiteralPath $PluginMcpConfig)) { throw "Missing generated ChatGPT Desktop plugin MCP config: $PluginMcpConfig" }',
        'if (-not (Test-Path -LiteralPath $StdioLauncher)) { throw "Missing generated stdio launcher: $StdioLauncher" }',
        '$CurrentArgs = Update-ClaudeDesktopBundleConfig',
        '$PluginArgs = Update-PluginBundleConfig',
        'Write-CodexBundleConfig $CurrentArgs',
        'if (($PluginArgs -join "`n") -ne ($CurrentArgs -join "`n")) { throw "Generated plugin and Claude Desktop MCP args diverged after bundle path update." }',
        '$SmokeArgs = @("--server-name", $ServerName, "--codex-config", $CodexConfig, "--claude-desktop-config", $ClaudeDesktopConfig, "--plugin-mcp-config", $PluginMcpConfig, "--out-json", $SmokeReport, "--fail-on-issue")',
        '& reg-rag-mcp-client-config-smoke @SmokeArgs',
        '$SmokeExitCode = $LASTEXITCODE',
        'if ((Test-Path -LiteralPath $SmokeReport) -and (Test-Path -LiteralPath $BundleStatus)) {',
        '  $Smoke = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json',
        '  $Status = Get-Content -LiteralPath $BundleStatus -Raw -Encoding UTF8 | ConvertFrom-Json',
        '  foreach ($Name in @("launcher_ready", "process_started", "mcp_initialized", "tools_discovered", "end_to_end_verified")) {',
        '    if ($Status.PSObject.Properties[$Name]) { $Status.$Name = [bool]$Smoke.$Name } else { Add-Member -InputObject $Status -MemberType NoteProperty -Name $Name -Value ([bool]$Smoke.$Name) }',
        '  }',
        '  $Status.direct_stdio_verified = [bool]$Smoke.end_to_end_verified',
        '  Write-JsonUtf8NoBom $BundleStatus $Status 50',
        '}',
        'Write-Host "Client config smoke report: $SmokeReport"',
        "if ($SmokeExitCode -ne 0) { exit $SmokeExitCode }",
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
        f'$ServerName = {_powershell_single_quoted_json(server_name)}',
        f'$RemoteUrl = {_powershell_single_quoted_json(url)}',
        f'$TokenEnv = {_powershell_single_quoted_json(token_name)}',
        '$SmokeReport = Join-Path $BundleDir "mcp_chatgpt_remote_smoke.json"',
        '$BundleStatus = Join-Path $BundleDir "bundle_status.json"',
        'function Write-Utf8NoBom([string]$LiteralPath, [string]$Value) { $Utf8NoBom = New-Object System.Text.UTF8Encoding($false); [System.IO.File]::WriteAllText($LiteralPath, $Value, $Utf8NoBom) }',
        'function Write-JsonUtf8NoBom([string]$LiteralPath, [object]$Value, [int]$Depth = 50) { Write-Utf8NoBom $LiteralPath (($Value | ConvertTo-Json -Depth $Depth) + [Environment]::NewLine) }',
        'if ([string]::IsNullOrWhiteSpace($RemoteUrl)) { throw "No ChatGPT remote HTTPS endpoint is configured. Regenerate with --public-url https://your-host.example/mcp or use Secure MCP Tunnel." }',
        'if (-not $RemoteUrl.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) { throw "ChatGPT remote MCP requires an https:// endpoint." }',
        'if (-not (Get-Command reg-rag-mcp-client-config-smoke -ErrorAction SilentlyContinue)) { throw "reg-rag-mcp-client-config-smoke was not found on PATH." }',
        '$SmokeArgs = @("--server-name", $ServerName, "--remote-url", $RemoteUrl, "--out-json", $SmokeReport, "--fail-on-issue")',
        'if ($TokenEnv) { $SmokeArgs += @("--remote-token-env", $TokenEnv) }',
        '& reg-rag-mcp-client-config-smoke @SmokeArgs',
        '$SmokeExitCode = $LASTEXITCODE',
        'if ((Test-Path -LiteralPath $SmokeReport) -and (Test-Path -LiteralPath $BundleStatus)) {',
        '  $Smoke = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json',
        '  $Status = Get-Content -LiteralPath $BundleStatus -Raw -Encoding UTF8 | ConvertFrom-Json',
        '  foreach ($Name in @("launcher_ready", "process_started", "mcp_initialized", "tools_discovered", "end_to_end_verified")) {',
        '    if ($Status.PSObject.Properties[$Name]) { $Status.$Name = [bool]$Smoke.$Name } else { Add-Member -InputObject $Status -MemberType NoteProperty -Name $Name -Value ([bool]$Smoke.$Name) }',
        '  }',
        '  $Status.remote_endpoint_verified = [bool]$Smoke.end_to_end_verified',
        '  $Status.tool_scan_unverified = $true',
        '  $Status.conversation_attachment_unverified = $true',
        '  Write-JsonUtf8NoBom $BundleStatus $Status 50',
        '}',
        'Write-Host "Remote MCP validation report: $SmokeReport"',
        'Write-Host "Protocol validation does not replace ChatGPT Settings > Apps > Scan Tools or per-conversation attachment."',
        'if ($SmokeExitCode -ne 0) { exit $SmokeExitCode }',
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
        '$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"',
        'if (-not (Test-Path -LiteralPath $StdioLauncher)) { throw "Missing generated stdio launcher: $StdioLauncher" }',
        'function Assert-Command([string]$Name) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found on PATH. Install this package in the active Python environment first." } }',
        'function Invoke-ClaudeMcpCli([string[]]$Arguments) {',
        '  $PreviousErrorActionPreference = $ErrorActionPreference',
        '  try {',
        '    $ErrorActionPreference = "Continue"',
        '    $CommandOutput = @(& claude @Arguments 2>&1)',
        '    $CommandExitCode = $LASTEXITCODE',
        '  } finally {',
        '    $ErrorActionPreference = $PreviousErrorActionPreference',
        '  }',
        '  return [pscustomobject]@{ ExitCode = $CommandExitCode; Output = $CommandOutput }',
        '}',
        'Assert-Command "reg-rag-mcp-doctor"',
        'Assert-Command "claude"',
        _powershell_command("reg-rag-mcp-doctor", doctor_args),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        "$ClaudeCodeArgs = " + _powershell_array_literal(server_args),
        '$LauncherArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $StdioLauncher) + $ClaudeCodeArgs',
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
            'function Invoke-ServerFromSource([string]$ProjectRoot, [string[]]$ArgsToPass) {',
            '  $ScriptPath = Join-Path $ProjectRoot "scripts\\run_regulation_mcp.py"',
            '  $PythonCandidates = @()',
            '  if ($env:REG_RAG_PYTHON) { $PythonCandidates += $env:REG_RAG_PYTHON }',
            '  if ($PreferredPython) { $PythonCandidates += $PreferredPython }',
            '  $PythonCandidates += (Join-Path $ProjectRoot ".venv\\Scripts\\python.exe")',
            '  $PythonCandidates += "python"',
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
            '      & $Command $ScriptPath @ArgsToPass',
            '      exit $LASTEXITCODE',
            '    }',
            '  }',
            '  throw "Python was not found. Install the bundled wheel or set REG_RAG_PYTHON to the project Python executable."',
            '}',
            '$ProjectRoot = Find-ProjectRoot',
            'if (-not $ProjectRoot -and $PreferredProjectRoot) {',
            '  $PreferredScript = Join-Path $PreferredProjectRoot "scripts\\run_regulation_mcp.py"',
            '  if (Test-Path -LiteralPath $PreferredScript) { $ProjectRoot = $PreferredProjectRoot }',
            '}',
            'if ($ProjectRoot) { Invoke-ServerFromSource $ProjectRoot $ServerArgs }',
            '$ConsoleCommand = Get-Command "reg-rag-mcp-server" -ErrorAction SilentlyContinue',
            'if ($ConsoleCommand) {',
            '  & $ConsoleCommand.Source @ServerArgs',
            '  exit $LASTEXITCODE',
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


def _powershell_array_literal(args: list[object] | tuple[object, ...]) -> str:
    return "@(" + ", ".join(_powershell_array_value(str(arg)) for arg in args) + ")"


def _powershell_array_value(value: str) -> str:
    if value == BUNDLE_DATA_DIR_ARG:
        return BUNDLE_DATA_DIR_ARG
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
        return '""'
    if any(char.isspace() for char in value) or any(char in value for char in ['"', "'"]):
        return '"' + value.replace("`", "``").replace('"', '`"') + '"'
    return value


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
        help="Environment variable used by generated remote HTTP server commands for bearer auth.",
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
