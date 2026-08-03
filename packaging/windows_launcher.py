from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from app.utils.fitz_compat import fitz
except ImportError:
    fitz = None

from scripts.find_available_ui_port import select_available_port


def _bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[1]


def _runtime_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "PR MCP Builder"
    return Path.home() / "AppData" / "Local" / "PR MCP Builder"


def _configure_runtime() -> tuple[Path, Path]:
    bundle_root = _bundle_root()
    runtime_root = _runtime_root()
    data_dir = runtime_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "exports").mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("APP_ENV", "local")
    os.environ.setdefault("DATA_DIR", str(data_dir))
    os.environ.setdefault("ARTIFACT_ROOT", str(data_dir))
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(data_dir / 'app.db').as_posix()}")
    os.environ.setdefault("INSTITUTION_PROFILES_PATH", str(data_dir / "institution_profiles.json"))
    os.environ.setdefault("QUALITY_PROFILES_PATH", str(data_dir / "quality_profiles.json"))
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    if getattr(sys, "frozen", False):
        os.environ.setdefault("REG_RAG_PACKAGED_EXE", str(Path(sys.executable).resolve()))

    os.chdir(runtime_root)
    return bundle_root, runtime_root


def portable_self_check() -> int:
    """Verify the bundled PDF dependency and parser without creating runtime data."""
    expected_text = "Portable PDF parser self-check"
    try:
        from app.parsers.pdf_parser import PDFParser

        if fitz is None:
            raise RuntimeError("pymupdf_not_available")
        with tempfile.TemporaryDirectory(prefix="pr-mcp-pdf-check-") as temporary_directory:
            pdf_path = Path(temporary_directory) / "self-check.pdf"
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((72, 72), expected_text)
                document.save(pdf_path)

            parsed = PDFParser().parse(pdf_path, document_id="portable-self-check")
            page_count = len(parsed.pages)
            text_verified = expected_text in parsed.raw_text
            if page_count != 1 or not text_verified:
                raise RuntimeError("unexpected_pdf_parser_result")
    except Exception:
        print(
            json.dumps(
                {
                    "schema_version": "pr-mcp-builder-portable-self-check-v1",
                    "status": "failed",
                    "reason": "pdf_parser_self_check_failed",
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "schema_version": "pr-mcp-builder-portable-self-check-v1",
                "status": "ok",
                "pages": page_count,
                "text_verified": text_verified,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    if "--portable-self-check" in sys.argv[1:]:
        return portable_self_check()

    if "--mcp-server" in sys.argv[1:]:
        server_args = [arg for arg in sys.argv[1:] if arg != "--mcp-server"]
        sys.argv = [sys.argv[0], *server_args]
        from scripts.run_regulation_mcp import main as run_mcp_server

        return int(run_mcp_server() or 0)

    if sys.argv[1:] == ["--version"]:
        from app import __version__

        print(__version__)
        return 0

    bundle_root, runtime_root = _configure_runtime()
    preferred_ui_port = int(os.getenv("REG_RAG_UI_PORT", "8501"))
    ui_port = select_available_port(preferred_ui_port)
    app_script = bundle_root / "frontend" / "streamlit_app.py"
    if not app_script.exists():
        print(f"[실행 오류] 프로그램 화면 파일을 찾을 수 없습니다: {app_script}")
        input("Enter 키를 누르면 닫힙니다.")
        return 2

    print("공공기관 규정 MCP 빌더를 시작합니다.")
    if ui_port != preferred_ui_port:
        print(f"기본 포트 {preferred_ui_port}이 사용 중이어서 {ui_port} 포트를 자동 선택했습니다.")
    print(f"브라우저 주소: http://127.0.0.1:{ui_port}")
    print(f"작업 데이터 저장 위치: {runtime_root / 'data'}")
    print("이 창을 닫으면 프로그램이 종료됩니다.")

    from streamlit.web import cli as streamlit_cli

    sys.argv = [
        "streamlit",
        "run",
        str(app_script),
        "--server.address=127.0.0.1",
        f"--server.port={ui_port}",
        "--server.headless=false",
        "--server.maxUploadSize=1000",
        "--global.developmentMode=false",
        "--browser.gatherUsageStats=false",
    ]
    return int(streamlit_cli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
