"""Run the disposable first-success journey for a beginner MCP operator.

This command deliberately verifies the local, safe part of the journey:
synthetic sample availability and the real stdio MCP wire contract. It does
not claim that Claude Desktop has been restarted or that a human has approved
an institution document. Those two actions remain explicit manual steps.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import platform
import sys
from typing import Any
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.synthetic_sample_service import build_synthetic_regulation_docx
from scripts.local_llm_doctor import diagnose_local_llm

try:
    from scripts.run_mcp_transport_smoke import run_mcp_transport_smoke
    _MCP_TRANSPORT_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - depends on the host installation
    # Keep the doctor useful even when a broken Python installation prevents
    # the MCP dependency graph from importing. The stage report will fail
    # closed with a stable reason instead of showing a traceback to beginners.
    run_mcp_transport_smoke = None  # type: ignore[assignment]
    _MCP_TRANSPORT_IMPORT_ERROR = type(exc).__name__


def run_beginner_first_success(
    *,
    verify_local_llm: bool = False,
    llm_endpoint: str = "http://127.0.0.1:11434",
    llm_model: str = "qwen3:8b",
    timeout_seconds: float = 30.0,
    out_json: Path | None = None,
) -> dict[str, Any]:
    """Run disposable checks and return a path-safe beginner report."""

    stages: list[dict[str, Any]] = []
    environment = _check_environment()
    stages.append(environment)

    sample = _check_synthetic_sample()
    stages.append(sample)

    transport = _run_stdio_contract(timeout_seconds=timeout_seconds)
    stages.append(transport)

    local_llm: dict[str, Any] | None = None
    if verify_local_llm:
        raw = diagnose_local_llm(
            backend="ollama",
            endpoint=llm_endpoint,
            model=llm_model,
            probe=True,
        )
        local_llm = {
            "name": "local_llm",
            "passed": bool(raw.get("passed")),
            "backend": raw.get("backend"),
            "model": raw.get("model"),
            "reason": raw.get("reason"),
        }
        stages.append(local_llm)

    required = [environment, sample, transport]
    passed = all(bool(stage.get("passed")) for stage in required) and (
        local_llm is None or bool(local_llm.get("passed"))
    )
    report: dict[str, Any] = {
        "report_type": "beginner_first_success_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "required_stage_count": len(required),
        "stages": stages,
        "manual_next_steps": [
            "승인된 기관 규정으로 MCP 번들을 생성하세요.",
            "Claude Desktop 설정의 mcpServers에 생성된 항목을 등록하세요.",
            "Claude Desktop을 트레이에서 완전히 종료한 뒤 다시 실행하세요.",
            "list_regulations, search, fetch를 실제 Claude 대화에서 호출하세요.",
        ],
    }
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def _check_environment() -> dict[str, Any]:
    required_modules = {
        "mcp": importlib.util.find_spec("mcp") is not None,
        "docx": importlib.util.find_spec("docx") is not None,
    }
    passed = sys.version_info >= (3, 11) and all(required_modules.values())
    return {
        "name": "environment",
        "passed": passed,
        "python": platform.python_version(),
        "python_supported": sys.version_info >= (3, 11),
        "required_modules": required_modules,
    }


def _check_synthetic_sample() -> dict[str, Any]:
    try:
        payload = build_synthetic_regulation_docx()
        is_docx = zipfile.is_zipfile(io.BytesIO(payload))
        has_content = len(payload) > 1024
        passed = is_docx and has_content
        return {
            "name": "synthetic_sample",
            "passed": passed,
            "format": "docx" if is_docx else "invalid",
            "size_bytes": len(payload),
            "redistributable": True,
        }
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        return {
            "name": "synthetic_sample",
            "passed": False,
            "reason": type(exc).__name__,
        }


def _run_stdio_contract(*, timeout_seconds: float) -> dict[str, Any]:
    if run_mcp_transport_smoke is None:
        return {
            "name": "mcp_stdio",
            "passed": False,
            "reason": "mcp_dependency_import_failed",
            "error_type": _MCP_TRANSPORT_IMPORT_ERROR,
        }
    try:
        # The underlying MCP server emits protocol logs on both streams. Keep
        # the beginner command readable; the structured summary below is the
        # authoritative result and never contains local paths.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            raw = run_mcp_transport_smoke(
                tenant_id="tenant-beginner-first-success",
                tenant_storage_isolation=True,
                transport="stdio",
                timeout_seconds=timeout_seconds,
                no_warm_cache=True,
            )
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        return {
            "name": "mcp_stdio",
            "passed": False,
            "reason": type(exc).__name__,
        }
    full = raw.get("full_profile") if isinstance(raw.get("full_profile"), dict) else {}
    data = raw.get("chatgpt_data_profile") if isinstance(raw.get("chatgpt_data_profile"), dict) else {}
    return {
        "name": "mcp_stdio",
        "passed": bool(raw.get("passed")),
        "transport": "stdio",
        "initialize": bool(full.get("mcp_initialized")),
        "list_regulations": bool(data.get("catalog_verified")),
        "search": bool(data.get("search_result_count", 0)),
        "fetch": bool(data.get("fetch_has_text")),
        "hierarchy": bool(data.get("hierarchy_verified")),
        "citation_fetch_has_text": bool(full.get("fetch_has_text")),
        "tenant_isolation": True,
        "synthetic_runtime_only": bool(raw.get("preparation", {}).get("synthetic_runtime")),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the disposable beginner first-success MCP journey."
    )
    parser.add_argument("--verify-local-llm", action="store_true")
    parser.add_argument("--llm-endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--llm-model", default="qwen3:8b")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--out-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_beginner_first_success(
        verify_local_llm=args.verify_local_llm,
        llm_endpoint=args.llm_endpoint,
        llm_model=args.llm_model,
        timeout_seconds=args.timeout_seconds,
        out_json=args.out_json,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
