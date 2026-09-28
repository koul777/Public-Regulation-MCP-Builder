"""Launch the optional local chat app without frontend process construction."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.find_available_ui_port import select_available_port


def start_local_qwen_chat(
    *,
    project_root: Path,
    environment: Mapping[str, str],
    packaged_executable: str = "",
    port_selector: Callable[..., int] | None = None,
    process_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Start a hidden loopback-only process; its existence is not readiness.

    The caller still verifies the app health endpoint and the model separately.
    Environment filtering remains in the existing frontend compatibility wrapper.
    """
    port = (port_selector or select_available_port)(8502, host="127.0.0.1", search_count=100)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("로컬 챗봇에 사용할 포트를 확인하지 못했습니다. 다시 실행해 주세요.")
    command = (
        [packaged_executable, "--qwen-chat"] if packaged_executable
        else [sys.executable, "-m", "scripts.run_qwen_chat"]
    ) + ["--port", str(port), "--headless"]
    options: dict[str, Any] = {
        "cwd": str(project_root), "env": dict(environment),
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = (process_factory or subprocess.Popen)(command, **options)
    return {
        "url": f"http://127.0.0.1:{port}", "pid": int(process.pid),
        "started_at": datetime.now(timezone.utc).isoformat(), "_process": process,
    }
