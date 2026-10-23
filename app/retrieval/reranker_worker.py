from __future__ import annotations

"""Private-pipe protocol for one local CPU reranker request."""

import contextlib
import ctypes
import json
import os
import sys
import threading
from typing import Any

from app.agents.model_router import QWEN3_RERANKER_MODEL
from app.retrieval.isolated_reranker import (
    MAX_WORKER_DOCUMENTS, MAX_WORKER_INPUT_BYTES, WORKER_MEMORY_FLOOR_BYTES,
    WORKER_PROTOCOL_VERSION, WORKER_OWNER_PID_ENV,
)
from app.retrieval.semantic_models import Qwen3RerankerAdapter


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_request_field")
        result[key] = value
    return result


def _parse_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_WORKER_INPUT_BYTES:
        raise ValueError("request_too_large")
    request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    expected = {"version", "query", "documents", "model_name", "model_source", "max_length", "batch_size"}
    if not isinstance(request, dict) or set(request) != expected:
        raise ValueError("invalid_request_fields")
    if type(request["version"]) is not int or request["version"] != WORKER_PROTOCOL_VERSION:
        raise ValueError("invalid_request_version")
    if request["model_name"] != QWEN3_RERANKER_MODEL:
        raise ValueError("invalid_request_model")
    if not isinstance(request["model_source"], str) or not 1 <= len(request["model_source"]) <= 4096:
        raise ValueError("invalid_request_source")
    if not isinstance(request["query"], str) or not request["query"].strip():
        raise ValueError("invalid_request_query")
    documents = request["documents"]
    if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_WORKER_DOCUMENTS:
        raise ValueError("invalid_request_document_count")
    if any(not isinstance(text, str) or not text.strip() for text in documents):
        raise ValueError("invalid_request_document")
    for key, low, high in (("max_length", 512, 8192), ("batch_size", 1, 32)):
        if type(request[key]) is not int or not low <= request[key] <= high:
            raise ValueError("invalid_request_integer")
    return request


def _available_commit_bytes() -> int | None:
    """Best-effort Windows commit observation; absence is not a safety claim."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in (
                    "total_physical", "available_physical", "total_pagefile", "available_pagefile",
                    "total_virtual", "available_virtual", "available_extended_virtual",
                )
            ]

        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        function = ctypes.windll.kernel32.GlobalMemoryStatusEx
        function.argtypes = [ctypes.POINTER(MemoryStatus)]
        function.restype = wintypes.BOOL
        return int(state.available_pagefile) if function(ctypes.byref(state)) else None
    except (AttributeError, OSError, ValueError):
        return None


class _ParentLifetime:
    """After capture, bind Windows checks to the original process handle."""

    def __init__(self, expected_pid: int | None = None) -> None:
        current_parent = os.getppid()
        self.pid = expected_pid if expected_pid is not None else current_parent
        self.handle: Any = None
        self.kernel: Any = None
        self.already_gone = expected_pid is not None and current_parent != expected_pid
        if os.name == "nt" and not self.already_gone:
            try:
                from ctypes import wintypes
                self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                self.kernel.OpenProcess.restype = wintypes.HANDLE
                self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                self.kernel.WaitForSingleObject.restype = wintypes.DWORD
                self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                self.kernel.CloseHandle.restype = wintypes.BOOL
                self.handle = self.kernel.OpenProcess(0x100000, False, self.pid)
                self.already_gone = not self.handle and ctypes.get_last_error() == 87
            except (AttributeError, OSError):
                self.kernel = None

    def is_alive(self) -> bool | None:
        if self.already_gone:
            return False
        if os.name != "nt":
            return os.getppid() == self.pid
        if self.handle is None or self.kernel is None:
            return None
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        return False if state == 0 else True if state == 258 else None

    def close(self) -> None:
        if self.handle is not None and self.kernel is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _expected_owner_pid() -> int | None:
    # Direct CLI invocation has no authenticated launch environment and falls
    # back to a best-effort current-parent check. API launches always set this.
    value = os.environ.get(WORKER_OWNER_PID_ENV)
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 2**32 - 1:
        raise ValueError("invalid_worker_owner")
    return int(value)


def _memory_guard(stop: threading.Event, parent: _ParentLifetime | None = None) -> None:
    while not stop.is_set():
        if parent is not None and parent.is_alive() is False:
            os._exit(76)
        available = _available_commit_bytes()
        if available is not None and available < WORKER_MEMORY_FLOOR_BYTES:
            # This process owns only one inference. Do not alter other workers,
            # the parent API, Ollama, or any system-wide memory setting.
            os._exit(75)
        stop.wait(0.25)


def _execute(request: dict[str, Any]) -> list[float]:
    source = request["model_source"]
    adapter = Qwen3RerankerAdapter(
        model_name=request["model_name"], device="cpu", max_length=request["max_length"],
        local_files_only=True, model_path=None if source == QWEN3_RERANKER_MODEL else source,
    )
    return adapter.score(request["query"], request["documents"], batch_size=request["batch_size"])


def main() -> int:
    try:
        request = _parse_request(sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1))
        expected_owner = _expected_owner_pid()
    except (ValueError, UnicodeDecodeError):
        return 2
    # Also enforce offline loading when invoked directly, not just by the API.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    stop = threading.Event()
    parent = _ParentLifetime(expected_pid=expected_owner)
    guard = threading.Thread(target=_memory_guard, args=(stop, parent), daemon=True)
    guard.start()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            scores = _execute(request)
        result = {"version": WORKER_PROTOCOL_VERSION, "status": "ok", "scores": scores}
        sys.stdout.write(json.dumps(result, separators=(",", ":"), allow_nan=False))
        sys.stdout.flush()
        return 0
    except Exception:
        # Neither original text, paths nor third-party exception messages leave
        # this protocol. The parent checks the nonzero process exit status.
        return 1
    finally:
        stop.set()
        guard.join(timeout=1)
        parent.close()


if __name__ == "__main__":
    raise SystemExit(main())
