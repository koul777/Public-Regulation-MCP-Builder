from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

# ``python scripts\\local_llm_doctor.py`` puts ``scripts`` ahead of the
# repository root on sys.path. Prefer this checkout over an unrelated
# site-packages installation when the CLI is run directly.
if __package__ in {None, ""}:
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from app.core.config import Settings
from app.services.local_llm_readiness_service import diagnose_local_llm_settings
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
    return diagnose_local_llm_settings(
        settings, probe=probe,
        availability_check=local_llm_available, probe_runner=probe_local_llm,
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
