from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse

# ``python scripts\\local_llm_doctor.py`` puts ``scripts`` ahead of the
# repository root on sys.path. Prefer this checkout over an unrelated
# site-packages installation when the CLI is run directly.
if __package__ in {None, ""}:
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from app.core.config import Settings
from app.rag.local_llm import DEFAULT_LOCAL_LLM_MODEL, local_llm_available, probe_local_llm


def diagnose_local_llm(
    *,
    backend: str = "ollama",
    endpoint: str = "http://127.0.0.1:11434",
    model: str = DEFAULT_LOCAL_LLM_MODEL,
    data_dir: Path = Path("./data"),
    probe: bool = True,
) -> dict[str, Any]:
    settings = Settings(
        data_dir=data_dir,
        rag_llm_backend=backend,
        rag_llm_endpoint=endpoint,
        rag_llm_model=model,
    )
    normalized_backend = str(backend or "extractive").strip().lower()
    if normalized_backend == "extractive":
        return {
            "report_type": "local_llm_doctor_v1",
            "passed": True,
            "backend": "extractive",
            "model": None,
            "endpoint_host": None,
            "probe": False,
            "reason": "model_free_mode",
        }

    endpoint_allowed = local_llm_available(settings)
    result: dict[str, Any] = {
        "report_type": "local_llm_doctor_v1",
        "passed": False,
        "backend": normalized_backend,
        "model": model or DEFAULT_LOCAL_LLM_MODEL,
        "endpoint": _safe_endpoint(endpoint),
        "endpoint_allowed": endpoint_allowed,
        "probe": bool(probe),
    }
    if not endpoint_allowed:
        result["reason"] = "endpoint_not_allowed_or_missing"
        return result
    if not probe:
        result["passed"] = True
        result["reason"] = "local_endpoint_configuration_valid"
        return result
    health = probe_local_llm(settings)
    result.update(
        {
            "passed": bool(health.get("available")),
            "health": health,
            "reason": "available" if health.get("available") else "local_backend_unavailable",
        }
    )
    result.pop("endpoint", None)
    result["endpoint_host"] = health.get("endpoint_host")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Qwen3 8B local RAG backend.")
    parser.add_argument("--backend", default="ollama", choices=("extractive", "ollama", "llama-cpp", "openai-compatible"))
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=DEFAULT_LOCAL_LLM_MODEL)
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--no-probe", action="store_true")
    args = parser.parse_args(argv)
    report = diagnose_local_llm(
        backend=args.backend,
        endpoint=args.endpoint,
        model=args.model,
        data_dir=Path(args.data_dir),
        probe=not args.no_probe,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


def _safe_endpoint(endpoint: str) -> str:
    # The doctor may show a localhost endpoint for operator diagnostics, but
    # never echoes arbitrary URLs or credentials into reports.
    value = str(endpoint or "").strip()
    if "@" in value:
        return "[redacted-endpoint]"
    try:
        if urlparse(value).hostname not in {"127.0.0.1", "localhost", "::1"}:
            return "[non-local-endpoint-redacted]"
    except ValueError:
        return "[invalid-endpoint-redacted]"
    return value


if __name__ == "__main__":
    raise SystemExit(main())
