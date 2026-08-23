from __future__ import annotations

"""Launch the standalone Qwen regulation chatbot on a loopback-only address."""

import argparse
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Mapping

from scripts.find_available_ui_port import port_is_available, select_available_port


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PREFERRED_PORT = 8502
LOCAL_APP_ENVS = frozenset({"local", "dev", "development", "test"})
CHILD_SECRET_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "API_AUTH_TOKEN",
        "API_AUTH_TOKENS",
    }
)
CHILD_PROXY_ENV_NAMES = frozenset(
    {
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    }
)


def validate_loopback_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return normalized
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError("--host는 localhost 또는 loopback IP여야 합니다.") from exc
    if not address.is_loopback:
        raise ValueError("--host는 외부에 공개할 수 없으며 loopback IP여야 합니다.")
    return normalized


def validate_launch_environment(environment: Mapping[str, str]) -> None:
    app_env = str(environment.get("APP_ENV", "local") or "local").strip().lower()
    if app_env not in LOCAL_APP_ENVS:
        raise ValueError("보호된 APP_ENV에서는 로컬 Qwen 채팅을 실행할 수 없습니다.")
    if _environment_bool(environment, "API_AUTH_REQUIRED", False):
        raise ValueError("API_AUTH_REQUIRED=true인 보호 모드에서는 실행할 수 없습니다.")
    if _environment_bool(environment, "TENANT_STORAGE_ISOLATION", False):
        raise ValueError("TENANT_STORAGE_ISOLATION=true인 공유 모드에서는 실행할 수 없습니다.")


def resolve_launch_port(
    requested_port: int | None,
    *,
    host: str = DEFAULT_HOST,
    search_count: int = 100,
) -> int:
    """Use an explicit port exactly, or select the first free local default."""

    normalized_host = validate_loopback_host(host)
    if int(search_count) < 1:
        raise ValueError("search_count는 1 이상이어야 합니다.")
    if requested_port is None:
        if ":" in normalized_host:
            last_port = min(65535, DEFAULT_PREFERRED_PORT + search_count - 1)
            for candidate in range(DEFAULT_PREFERRED_PORT, last_port + 1):
                if _port_is_available(candidate, host=normalized_host):
                    return candidate
            raise RuntimeError("사용 가능한 localhost IPv6 포트를 찾지 못했습니다.")
        return select_available_port(
            DEFAULT_PREFERRED_PORT,
            host=normalized_host,
            search_count=search_count,
        )
    if not 1 <= int(requested_port) <= 65535:
        raise ValueError("--port는 1부터 65535 사이여야 합니다.")
    if not _port_is_available(int(requested_port), host=normalized_host):
        raise RuntimeError(f"요청한 localhost 포트 {requested_port}를 이미 다른 프로그램이 사용 중입니다.")
    return int(requested_port)


def streamlit_command(
    *,
    app_path: Path,
    host: str,
    port: int,
    headless: bool,
) -> list[str]:
    normalized_host = validate_loopback_host(host)
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        normalized_host,
        "--server.port",
        str(port),
        "--server.headless",
        "true" if headless else "false",
        "--browser.gatherUsageStats",
        "false",
    ]


def launch_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preserve builder-provided paths/RAG settings and add safe Qwen defaults."""

    environment = dict(os.environ if base is None else base)
    for name in CHILD_SECRET_ENV_NAMES | CHILD_PROXY_ENV_NAMES:
        environment.pop(name, None)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    environment["RAG_LLM_BACKEND"] = "ollama"
    environment["RAG_LLM_MODEL"] = "qwen3:8b"
    environment.setdefault("RAG_LLM_ENDPOINT", "http://127.0.0.1:11434")
    return environment


def local_url(host: str, port: int) -> str:
    normalized_host = validate_loopback_host(host)
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return f"http://{display_host}:{port}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="승인·색인된 로컬 규정을 qwen3:8b로 질문하는 별도 Streamlit 앱을 실행합니다."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="localhost/loopback 주소만 허용됩니다.")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="지정하면 이 포트를 정확히 사용합니다. 생략하면 8502부터 빈 포트를 찾습니다.",
    )
    parser.add_argument("--headless", action="store_true", help="브라우저를 자동으로 열지 않습니다.")
    parser.add_argument("--search-count", type=int, default=100, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        host = validate_loopback_host(args.host)
        if args.search_count < 1:
            raise ValueError("--search-count는 1 이상이어야 합니다.")
        environment = launch_environment()
        validate_launch_environment(environment)
        port = resolve_launch_port(
            args.port,
            host=host,
            search_count=args.search_count,
        )
        repository_root = Path(__file__).resolve().parents[1]
        app_path = repository_root / "frontend" / "qwen_chat_app.py"
        if not app_path.is_file():
            raise FileNotFoundError("독립 Qwen 채팅 앱 파일을 찾지 못했습니다.")
        command = streamlit_command(
            app_path=app_path,
            host=host,
            port=port,
            headless=args.headless,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[실행 중단] {exc}", file=sys.stderr)
        return 2

    print()
    print(f"로컬 Qwen 규정 챗봇: {local_url(host, port)}")
    print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    print()
    completed = subprocess.run(
        command,
        cwd=repository_root,
        env=environment,
        check=False,
    )
    return int(completed.returncode)


def _environment_bool(
    environment: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _port_is_available(port: int, *, host: str) -> bool:
    if ":" not in host:
        return port_is_available(port, host=host)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((host, int(port)))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
