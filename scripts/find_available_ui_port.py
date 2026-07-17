from __future__ import annotations

import argparse
import socket


def port_is_available(port: int, *, host: str = "127.0.0.1") -> bool:
    """Return whether a local TCP listener can bind to the requested port."""
    if not 1 <= int(port) <= 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((host, int(port)))
        except OSError:
            return False
    return True


def select_available_port(
    preferred_port: int = 8501,
    *,
    host: str = "127.0.0.1",
    search_count: int = 100,
) -> int:
    """Select the first available local port from the preferred port upward."""
    if not 1 <= int(preferred_port) <= 65535:
        raise ValueError("preferred_port must be between 1 and 65535")
    if search_count < 1:
        raise ValueError("search_count must be at least 1")
    last_port = min(65535, int(preferred_port) + int(search_count) - 1)
    for port in range(int(preferred_port), last_port + 1):
        if port_is_available(port, host=host):
            return port
    raise RuntimeError(f"No available local UI port found between {preferred_port} and {last_port}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print an available localhost port for the operator UI.")
    parser.add_argument("--preferred", type=int, default=8501)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--search-count", type=int, default=100)
    args = parser.parse_args(argv)
    print(
        select_available_port(
            args.preferred,
            host=args.host,
            search_count=args.search_count,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
