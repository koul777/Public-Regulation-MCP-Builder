"""Local indexing setup; no document data enters the setup subprocess."""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel


_PACKAGES = {"torch": "torch", "transformers": "transformers", "sentence_transformers": "sentence-transformers"}
_PROBE_PREFIX = "INDEXING_READY:"
_MODEL_PROBE = """
import json, math
from app.retrieval.semantic_models import Qwen3EmbeddingAdapter
adapter = Qwen3EmbeddingAdapter(device="cpu", truncate_dim=384, local_files_only=False)
vector = adapter.encode_documents(["Local indexing readiness check."])[0]
ready = len(vector) == 384 and all(math.isfinite(float(v)) for v in vector)
ready = ready and abs(sum(float(v) ** 2 for v in vector) - 1.0) < 0.01
print("INDEXING_READY:" + json.dumps({"ready": bool(ready), "dimensions": len(vector)}))
"""


class IndexingPackageStatus(BaseModel):
    missing_packages: tuple[str, ...] = ()
    can_prepare: bool = True

    @property
    def available(self) -> bool:
        return not self.missing_packages


class IndexingPreparationResult(BaseModel):
    ready: bool = False
    reason: str


def check_indexing_packages() -> IndexingPackageStatus:
    """Check installation without importing heavy models or contacting a server."""
    importlib.invalidate_caches()
    missing: list[str] = []
    for module, package in _PACKAGES.items():
        try:
            present = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, OSError):
            present = False
        if not present:
            missing.append(package)
    return IndexingPackageStatus(
        missing_packages=tuple(missing), can_prepare=not bool(getattr(sys, "frozen", False)),
    )


def prepare_indexing_runtime(*, runner: Callable[..., Any] | None = None) -> IndexingPreparationResult:
    """Install and probe only on an explicit setup click, using fixed inputs.

    Setup may download packages/model weights. Its probe uses a fixed synthetic
    sentence, never uploads or reads an institution document. Captured process
    output can contain paths/tokens, so only fixed reason codes leave this API.
    """
    status = check_indexing_packages()
    if not status.can_prepare:
        return IndexingPreparationResult(reason="managed_runtime_required")
    run = runner or subprocess.run
    options: dict[str, Any] = {
        "check": False, "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace", "timeout": 900,
        "stdin": subprocess.DEVNULL, "cwd": str(Path(__file__).resolve().parents[2]),
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if not status.available:
            installed = run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "sentence-transformers>=5.0,<6.0"], **options,
            )
            if installed.returncode != 0:
                return IndexingPreparationResult(reason="package_install_failed")
        probe = run([sys.executable, "-c", _MODEL_PROBE], **options)
        if probe.returncode != 0:
            return IndexingPreparationResult(reason="model_prepare_failed")
        lines = [line for line in str(probe.stdout).splitlines() if line.startswith(_PROBE_PREFIX)]
        result = json.loads(lines[-1][len(_PROBE_PREFIX):]) if lines else {}
        if not isinstance(result, dict) or result.get("ready") is not True or result.get("dimensions") != 384:
            return IndexingPreparationResult(reason="model_probe_invalid")
    except subprocess.TimeoutExpired:
        return IndexingPreparationResult(reason="setup_timeout")
    except (OSError, ValueError, TypeError):
        return IndexingPreparationResult(reason="setup_unavailable")
    importlib.invalidate_caches()
    return IndexingPreparationResult(ready=True, reason="ready")


def indexing_preparation_guidance(reason: str) -> str:
    """Return a public, fixed next action without exposing provider output."""
    return {
        "ready": "검색 모델의 실제 실행을 확인했습니다. 아래 승인·색인 또는 등록만 실행 버튼을 누르세요.",
        "managed_runtime_required": "이 실행판에서는 패키지를 변경할 수 없습니다. 검색 모델이 포함된 배포본을 사용하거나 Python 소스 실행 환경에서 준비해 주세요.",
        "package_install_failed": "검색 패키지를 설치하지 못했습니다. 인터넷 연결과 앱의 Python 환경 쓰기 권한을 확인한 뒤 다시 실행하세요.",
        "model_prepare_failed": "검색 모델을 준비하지 못했습니다. 인터넷 연결과 모델 저장 공간을 확인한 뒤 다시 실행하세요.",
        "model_probe_invalid": "검색 모델의 정상 실행을 확인하지 못했습니다. 준비·점검을 다시 실행하세요.",
        "setup_timeout": "검색 준비 대기 시간을 초과했습니다. 인터넷 연결과 저장 공간을 확인한 뒤 다시 실행하세요.",
    }.get(reason, "검색 준비 도구를 실행하지 못했습니다. 앱의 Python 환경을 확인한 뒤 다시 실행하세요.")
