from __future__ import annotations

"""Bounded, short-lived CPU inference for the local Qwen reranker."""

import json
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any

from app.retrieval.semantic_models import Qwen3RerankerAdapter, _validated_texts


WORKER_PROTOCOL_VERSION = 1
MAX_WORKER_DOCUMENTS = 50
MAX_WORKER_INPUT_BYTES = 8 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 64 * 1024
MAX_WORKER_STDERR_BYTES = 64 * 1024
WORKER_MEMORY_FLOOR_BYTES = 512 * 1024 * 1024
WORKER_OWNER_PID_ENV = "REGULATION_RERANKER_OWNER_PID"
_WORKER_FAILURE_CODES = frozenset({
    "reranker_worker_timeout", "reranker_worker_memory_floor",
    "reranker_worker_invalid_response", "reranker_worker_invalid_score",
    "reranker_worker_score_count_mismatch", "reranker_worker_input_too_large",
    "reranker_worker_output_too_large", "reranker_worker_stderr_too_large",
    "reranker_worker_pipe_error", "reranker_worker_pipe_not_closed",
    "reranker_worker_failed",
})


class RerankerWorkerError(RuntimeError):
    """A bounded reason code, never worker output or private source text."""

    def __init__(self, reason_code: str) -> None:
        self._reason_code = (
            reason_code
            if type(reason_code) is str and reason_code in _WORKER_FAILURE_CODES
            else "reranker_worker_failed"
        )
        super().__init__(self._reason_code)

    @property
    def reason_code(self) -> str:
        """Return the allowlisted code suitable for a public degraded trace."""
        return self._reason_code


class IsolatedQwen3RerankerAdapter(Qwen3RerankerAdapter):
    """Keep the adapter cached, but reclaim CPU weights after every score call.

    The worker uses the same scorer, input normalization, FP32 capability policy,
    batch size and last-token logits. Process exit also releases framework and
    allocator allocations that dropping model references does not reclaim.
    """

    def __init__(self, *, timeout_seconds: float = 300.0, **kwargs: Any) -> None:
        kwargs.setdefault("local_files_only", True)
        super().__init__(**kwargs)
        if self.device not in {"cpu", "cpu:0"} or self.local_files_only is not True:
            raise ValueError("isolated_reranker_requires_local_cpu")
        if self._model is not None or self._tokenizer is not None:
            raise ValueError("isolated_reranker_does_not_accept_loaded_runtime")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("invalid_reranker_worker_timeout")
        if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 600:
            raise ValueError("invalid_reranker_worker_timeout")
        self.timeout_seconds = float(timeout_seconds)

    def score(self, query: str, documents: list[str], *, batch_size: int = 8) -> list[float]:
        if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_WORKER_DOCUMENTS:
            raise ValueError("invalid_reranker_worker_document_count")
        normalized_query = _validated_texts([query])[0]
        normalized_documents = _validated_texts(documents)
        bounded_batch = max(1, min(int(batch_size), 32))
        request = {
            "version": WORKER_PROTOCOL_VERSION,
            "query": normalized_query,
            "documents": normalized_documents,
            "model_name": self.model_name,
            "model_source": self.model_source,
            "max_length": self.max_length,
            "batch_size": bounded_batch,
        }
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("reranker_worker_input_too_large")
        output = _run_worker(payload, timeout_seconds=self.timeout_seconds)
        return _validated_worker_scores(output, expected_count=len(normalized_documents))


def _worker_command() -> list[str]:
    # Module execution works from an installed wheel without a source checkout.
    return [sys.executable, "-m", "app.retrieval.reranker_worker"]


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass  # A process can exit between poll and terminate.
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=5)
    else:
        process.wait(timeout=5)


def _run_worker(payload: bytes, *, timeout_seconds: float) -> bytes:
    if len(payload) > MAX_WORKER_INPUT_BYTES:
        raise RerankerWorkerError("reranker_worker_input_too_large")
    env = dict(os.environ)
    env.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONFAULTHANDLER": "1",
        WORKER_OWNER_PID_ENV: str(os.getpid()),
    })
    process = subprocess.Popen(
        _worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout = bytearray()
    stderr = bytearray()
    failures: list[str] = []
    failed = threading.Event()

    def read_pipe(pipe: Any, target: bytearray, limit: int, reason: str) -> None:
        try:
            while block := pipe.read(4096):
                remaining = limit - len(target)
                target.extend(block[:max(0, remaining)])
                if len(block) > remaining:
                    if not failed.is_set():
                        failures.append(reason)
                    failed.set()
                    # Continue draining until the owner terminates the worker.
        except (OSError, ValueError):
            if not failed.is_set():
                failures.append("reranker_worker_pipe_error")
            failed.set()

    def write_input() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            # A failed child may exit before reading its input. Its return code
            # and response validation below still make that a failed call.
            pass

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=read_pipe, args=(process.stdout, stdout, MAX_WORKER_OUTPUT_BYTES,
                                                "reranker_worker_output_too_large"), daemon=True),
        threading.Thread(target=read_pipe, args=(process.stderr, stderr, MAX_WORKER_STDERR_BYTES,
                                                "reranker_worker_stderr_too_large"), daemon=True),
        threading.Thread(target=write_input, daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if failed.is_set():
                raise RerankerWorkerError(failures[0])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RerankerWorkerError("reranker_worker_timeout")
            failed.wait(min(0.05, remaining))
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise RerankerWorkerError("reranker_worker_pipe_not_closed")
        if failed.is_set():
            raise RerankerWorkerError(failures[0])
        if process.returncode == 75:
            raise RerankerWorkerError("reranker_worker_memory_floor")
        if process.returncode != 0:
            raise RerankerWorkerError("reranker_worker_failed")
        return bytes(stdout)
    finally:
        try:
            _terminate_and_reap(process)
        finally:
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


def _validated_worker_scores(output: bytes, *, expected_count: int) -> list[float]:
    if len(output) > MAX_WORKER_OUTPUT_BYTES:
        raise RerankerWorkerError("reranker_worker_output_too_large")
    try:
        result = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_response_object)
    except (UnicodeDecodeError, ValueError):
        raise RerankerWorkerError("reranker_worker_invalid_response") from None
    if not isinstance(result, dict) or set(result) != {"version", "status", "scores"}:
        raise RerankerWorkerError("reranker_worker_invalid_response")
    if type(result["version"]) is not int or result["version"] != WORKER_PROTOCOL_VERSION or result["status"] != "ok":
        raise RerankerWorkerError("reranker_worker_invalid_response")
    scores = result["scores"]
    if not isinstance(scores, list) or len(scores) != expected_count:
        raise RerankerWorkerError("reranker_worker_score_count_mismatch")
    if any(type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1 for value in scores):
        raise RerankerWorkerError("reranker_worker_invalid_score")
    return [float(value) for value in scores]


def _unique_response_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_response_field")
        result[key] = value
    return result
