"""Explicit local setup operations, independent of Streamlit session state."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


_INSTALLER_GUIDANCE = {
    "windows_only": ("이 자동 설치는 Windows에서 사용할 수 있습니다.", "현재 운영체제의 Kordoc 설치 안내에 따라 설치한 뒤 준비 상태를 다시 확인하세요."),
    "installer_missing": ("앱에 Kordoc 설치 도구가 포함되어 있지 않습니다.", "설치 도구가 포함된 배포본을 다시 받은 뒤 설치를 다시 실행하세요."),
    "installer_timeout": ("정해진 시간 안에 설치가 끝나지 않았습니다.", "인터넷 연결과 npm 접근 상태를 확인한 뒤 설치를 다시 실행하세요."),
    "installer_unavailable": ("Windows 설치 도구를 실행하지 못했습니다.", "PowerShell 사용 권한을 확인하고 앱을 다시 연 뒤 설치를 다시 실행하세요."),
    "installer_failed": ("Kordoc 설치 또는 실행 확인에 실패했습니다.", "Node.js LTS와 npm이 설치되어 있는지, 인터넷 연결이 가능한지 확인한 뒤 설치를 다시 실행하세요."),
    "installer_command_mismatch": ("설치된 Kordoc 명령을 확인하지 못했습니다.", "npm 전역 설치 경로와 PATH를 확인한 뒤 설치를 다시 실행하세요."),
    "installer_version_mismatch": ("Kordoc 4.16.0 버전 확인에 실패했습니다.", "검증된 버전으로 다시 설치한 뒤 앱을 재시작하고 준비 상태를 확인하세요."),
}


def kordoc_installer_guidance(error: object) -> tuple[str, str]:
    """Return fixed Korean recovery copy, never echo an exception or path."""
    key = error if isinstance(error, str) else "installer_failed"
    return _INSTALLER_GUIDANCE.get(key, _INSTALLER_GUIDANCE["installer_failed"])


def kordoc_installer_candidates(project_root: Path) -> list[Path]:
    """Find shipped setup scripts in source, installed and portable layouts."""
    candidates: list[Path] = []
    try:
        executable_dir = Path(sys.executable).resolve().parent
        candidates.append(executable_dir / "INSTALL_KORDOC_KO.ps1")
    except OSError:
        executable_dir = None
    try:
        candidates.append(Path(sys.prefix).resolve() / "INSTALL_KORDOC_KO.ps1")
    except OSError:
        pass
    if executable_dir is not None:
        candidates.append(executable_dir.parent / "INSTALL_KORDOC_KO.ps1")
    candidates.extend((
        project_root / "INSTALL_KORDOC_KO.ps1",
        project_root / "packaging" / "INSTALL_KORDOC_KO.ps1",
    ))
    found: dict[str, Path] = {}
    for candidate in candidates:
        try:
            if candidate.is_file():
                found.setdefault(str(candidate).casefold(), candidate)
        except OSError:
            continue
    return list(found.values())


def run_kordoc_installer(
    candidates: Sequence[Path],
    *,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run only on an explicit install action; return no command output.

    npm/PowerShell output can contain credentials as well as paths. The public
    operator result intentionally contains only a stable reason code.
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "windows_only", "output": ""}
    if not candidates:
        return {"ok": False, "error": "installer_missing", "output": ""}
    options: dict[str, Any] = {
        "check": False, "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace", "timeout": 300,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = (runner or subprocess.run)(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(candidates[0]), "-PersistUserPath"],
            **options,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "installer_timeout", "output": ""}
    except OSError:
        return {"ok": False, "error": "installer_unavailable", "output": ""}
    failure_codes = {10: "installer_command_mismatch", 11: "installer_version_mismatch"}
    return {"ok": result.returncode == 0,
            "error": "" if result.returncode == 0 else failure_codes.get(result.returncode, "installer_failed"),
            "output": ""}
