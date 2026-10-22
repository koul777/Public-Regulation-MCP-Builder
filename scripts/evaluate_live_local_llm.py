from __future__ import annotations

"""Evaluate a real loopback RAG API with independently observed Ollama calls.

This opt-in integration helper never approves or indexes documents. Evidence and
answers are private artifacts outside the checkout; ordinary unit tests use only
synthetic data. A model label in an API response is not execution evidence.
"""

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.local_http import local_urlopen
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER
from app.rag.output_filter import sanitize_rag_answer


SCHEMA_VERSION = "live-local-llm-evaluation-v1"
VERDICT_VERSION = "observed-generation-explicit-verification-v5"
_MAX_BODY_BYTES = 4 * 1024 * 1024
_MAX_CONSECUTIVE_SERVER_FAILURES = 2
_MODEL_ROUTES = frozenset({("GET", "/api/tags"), ("GET", "/api/ps"),
                           ("GET", "/api/version"), ("POST", "/api/generate")})
_REQUEST_FIELDS = frozenset({"top_k", "security_levels", "department_ids", "document_id",
                             "profile_id", "metadata_profile", "as_of_date", "orchestration_mode",
                             "retrieval_mode", "llm_backend", "history", "claim_audit_mode"})
_NUMERIC_OPTIONS = frozenset({"temperature", "seed", "num_predict", "num_ctx", "top_k",
                              "top_p", "repeat_penalty", "num_gpu", "num_thread"})
_STRUCTURED_FIELDS = frozenset({"answer", "claims", "abstained", "evidence_context_ids", "findings",
                                "intent", "locators", "search_queries", "query", "normalized_query"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: bytes | str) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split())


def literal_fact_matches(anchor: str, answer: str) -> bool:
    """Literal anchor matching with numeric boundaries; not semantic grading.

    In particular 10 days is not present in 110 days, and a bare 10 does not
    match 10.5. Regular-expression syntax in an anchor remains literal data.
    """
    normalized = _normalized(anchor).casefold()
    if not normalized:
        return False
    parts = re.split(r"(\d+(?:[.,]\d+)*)", normalized)
    pattern = "".join(
        r"(?<!\d)(?<!\d[.,])" + re.escape(part) + r"(?!\d|[.,]\d)"
        if index % 2 else re.escape(part)
        for index, part in enumerate(parts)
    )
    return re.search(pattern, _normalized(answer).casefold()) is not None


def validate_loopback_endpoint(value: str) -> str:
    """Reject remote hosts, credentials, query strings and ambiguous base URLs."""
    parsed = urlsplit(str(value))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_loopback_endpoint") from exc
    if (parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port == 0):
        raise ValueError("invalid_loopback_endpoint")
    return value.rstrip("/")


def private_output_directory(path: Path, *, project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve existing parents (including junctions/symlinks) before writing."""
    resolved = Path(path).resolve()
    root = Path(project_root).resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError("private_output_must_be_outside_checkout")
    resolved.mkdir(parents=True, exist_ok=True)
    # Recheck after creation, including parents that were created concurrently.
    if resolved.resolve().is_relative_to(root):
        raise ValueError("private_output_must_be_outside_checkout")
    return resolved


def _read_json(url: str, *, payload: Mapping[str, Any] | None = None,
               headers: Mapping[str, str] | None = None, timeout: float = 10) -> tuple[int, dict[str, Any]]:
    request = Request(url, data=None if payload is None else _canonical(payload),
                      headers={"Content-Type": "application/json", **dict(headers or {})})
    try:
        with local_urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(_MAX_BODY_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        raw = exc.read(_MAX_BODY_BYTES + 1)
    if len(raw) > _MAX_BODY_BYTES:
        raise ValueError("response_body_limit_exceeded")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("response_must_be_object")
    return status, decoded


def generation_observation(request: Mapping[str, Any], response: Mapping[str, Any],
                           *, status: int, duration_ms: float) -> dict[str, Any]:
    """Keep execution metrics and hashes, never prompt, answer or secret text."""
    text = str(response.get("response") or "")
    try:
        structured = json.loads(text)
    except (TypeError, ValueError):
        structured = None
    candidate = structured.get("answer", "") if isinstance(structured, dict) else text
    options = request.get("options") if isinstance(request.get("options"), dict) else {}
    tokens = response.get("eval_count", 0)
    token_count = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else 0
    model = str(request.get("model") or "")
    returned_model = str(response.get("model") or "")
    completed = (status == 200 and response.get("done") is True and token_count > 0
                 and bool(text.strip()) and returned_model == model)
    return {
        "model": model,
        "returned_model": returned_model,
        "request_sha256": _sha(_canonical(request)),
        "prompt_sha256": _sha(str(request.get("prompt") or "")),
        "prompt_char_count": len(str(request.get("prompt") or "")),
        "response_sha256": _sha(text),
        "response_char_count": len(text),
        "candidate_answer_sha256": _sha(_normalized(sanitize_rag_answer(str(candidate)))) if candidate else None,
        "structured_keys": sorted(set(structured) & _STRUCTURED_FIELDS) if isinstance(structured, dict) else [],
        "options": {key: value for key, value in options.items()
                    if key in _NUMERIC_OPTIONS and _number(value) is not None},
        "think": request.get("think") if isinstance(request.get("think"), bool) else None,
        "stream": request.get("stream") if isinstance(request.get("stream"), bool) else None,
        "format_sha256": _sha(_canonical(request.get("format"))),
        "generation_completed": completed,
        "done": response.get("done") is True,
        "done_reason": response.get("done_reason") if response.get("done_reason") in {"stop", "length", "unload"} else None,
        "eval_count": token_count,
        "prompt_eval_count": _number(response.get("prompt_eval_count")),
        "prompt_eval_cached_count": _number(response.get("prompt_eval_cached_count")),
        "total_duration_ns": _number(response.get("total_duration")),
        "load_duration_ns": _number(response.get("load_duration")),
        "prompt_eval_duration_ns": _number(response.get("prompt_eval_duration")),
        "eval_duration_ns": _number(response.get("eval_duration")),
        "duration_ms": round(duration_ms, 3),
        "http_status": status,
    }


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


class LocalOllamaObserver:
    """Bounded forwarding observer for a dedicated test API process.

    Configure that API's RAG_LLM_ENDPOINT with ``endpoint``. Calls and cases are
    serialized; a timed-out request must finish before another case can begin.
    Only installed-model inspection and non-streaming generation are forwarded.
    """

    def __init__(self, upstream_endpoint: str = "http://127.0.0.1:11434", *, port: int = 0,
                 timeout_seconds: float = 600) -> None:
        self.upstream_endpoint = validate_loopback_endpoint(upstream_endpoint)
        self.port = int(port)
        self.timeout_seconds = float(timeout_seconds)
        if not 0 <= self.port <= 65535 or not 0 < self.timeout_seconds <= 600:
            raise ValueError("invalid_observer_settings")
        self._condition = threading.Condition()
        self._case_id: str | None = None
        self._sequence = 0
        self._latest_case_sequence: dict[str, int] = {}
        self._inflight = 0
        self._events: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("observer_not_running")
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "LocalOllamaObserver":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                owner._forward(self)

            def do_POST(self) -> None:
                owner._forward(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None

    @contextmanager
    def case(self, case_id: str) -> Iterator[None]:
        with self._condition:
            if self._case_id is not None or self._inflight:
                raise RuntimeError("observer_case_already_active")
            self._case_id = case_id
            self._sequence += 1
            self._latest_case_sequence[case_id] = self._sequence
        try:
            yield
        finally:
            with self._condition:
                finished = self._condition.wait_for(lambda: self._inflight == 0, timeout=self.timeout_seconds)
                self._case_id = None
                if not finished:
                    raise RuntimeError("observer_call_still_running")

    def events_for(self, case_id: str) -> list[dict[str, Any]]:
        with self._condition:
            sequence = self._latest_case_sequence.get(case_id)
            return [dict(event) for event in self._events
                    if event.get("case_id") == case_id and event.get("case_sequence") == sequence]

    def provenance(self) -> dict[str, Any]:
        """Query digest/version without exposing API credentials or local paths."""
        result: dict[str, Any] = {"observer": "loopback_forwarding", "python": platform.python_version()}
        try:
            status, tags = _read_json(self.upstream_endpoint + "/api/tags")
            result["model_inventory_status"] = status
            result["models"] = [{key: model.get(key) for key in ("name", "digest", "size", "details")}
                                for model in tags.get("models", []) if isinstance(model, dict)]
            status, version = _read_json(self.upstream_endpoint + "/api/version")
            result["runtime_version"] = version.get("version") if status == 200 else None
        except Exception as exc:
            result["error_type"] = type(exc).__name__
        return result

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        if (handler.command, handler.path) not in _MODEL_ROUTES:
            self._send(handler, 404, b'{"error":"observer_route_not_allowed"}')
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if length < 0 or length > _MAX_BODY_BYTES or handler.headers.get("Transfer-Encoding"):
                raise ValueError("invalid_body_length")
            raw = handler.rfile.read(length) if length else None
            payload = json.loads(raw) if raw is not None else None
            if handler.command == "POST" and (not isinstance(payload, dict) or payload.get("stream") is not False):
                raise ValueError("only_nonstream_generation_allowed")
        except (ValueError, TypeError):
            self._send(handler, 400, b'{"error":"observer_invalid_request"}')
            return
        with self._condition:
            case_id = self._case_id
            case_sequence = self._latest_case_sequence.get(case_id) if case_id is not None else None
            self._inflight += 1
        started = time.perf_counter()
        event: dict[str, Any] = {"case_id": case_id, "case_sequence": case_sequence, "route": handler.path,
                                 "started_at": _now(), "upstream_attempted": True}
        status, response_raw, response_payload = 502, b'{"error":"observer_upstream_failure"}', {}
        try:
            req = Request(self.upstream_endpoint + handler.path, data=raw, method=handler.command,
                          headers={"Content-Type": "application/json"})
            try:
                with local_urlopen(req, timeout=self.timeout_seconds) as response:
                    status = response.status
                    response_raw = response.read(_MAX_BODY_BYTES + 1)
            except HTTPError as exc:
                status = exc.code
                response_raw = exc.read(_MAX_BODY_BYTES + 1)
            if len(response_raw) > _MAX_BODY_BYTES:
                raise ValueError("upstream_body_limit_exceeded")
            decoded = json.loads(response_raw)
            if not isinstance(decoded, dict):
                raise ValueError("upstream_response_not_object")
            response_payload = decoded
        except Exception as exc:
            event["error_type"] = type(exc).__name__
            status, response_raw = 502, b'{"error":"observer_upstream_failure"}'
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            if handler.path == "/api/generate":
                event.update(generation_observation(payload or {}, response_payload,
                                                     status=status, duration_ms=elapsed))
            else:
                event.update(http_status=status, duration_ms=round(elapsed, 3))
            event["finished_at"] = _now()
            with self._condition:
                self._events.append(event)
                self._inflight -= 1
                self._condition.notify_all()
        self._send(handler, status, response_raw)

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, raw: bytes) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionError):
            pass


def _values(item: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = item.get(key)
        for entry in value if isinstance(value, (list, tuple)) else [value]:
            if entry not in (None, ""):
                values.add(str(entry))
    return values


def check_citations(citations: list[dict[str, Any]], spec: Mapping[str, Any], *,
                    require_content_hash: bool = True) -> dict[str, Any]:
    """Check identity and exact quotes against independent private gold evidence."""
    allowed = spec.get("allowed_citations") or []
    findings: list[dict[str, Any]] = []
    for index, citation in enumerate(citations):
        identifiers = _values(citation, "evidence_ids", "evidence_id", "chunk_id")
        approvals = _values(citation, "approval_ids", "approval_id")
        hashes = _values(citation, "content_hashes", "content_hash", "approved_content_hash")
        matched = []
        for source in allowed:
            if not isinstance(source, dict) or citation.get("document_id") != source.get("document_id"):
                continue
            source_ids = _values(source, "evidence_ids", "evidence_id", "chunk_id")
            source_approvals = _values(source, "approval_ids", "approval_id")
            # A title/article match alone is not an approved evidence identity.
            if not identifiers or not identifiers.intersection(source_ids):
                continue
            if not approvals or not approvals.intersection(source_approvals):
                continue
            if source.get("article_no") and citation.get("article_no") != source["article_no"]:
                continue
            citation_titles = _values(citation, "regulation_title", "document_title")
            if source.get("regulation_title") and citation_titles != {source["regulation_title"]}:
                continue
            matched.append(source)
        covered_ids = set().union(*[_values(source, "evidence_ids", "evidence_id", "chunk_id") for source in matched]) if matched else set()
        covered_approvals = set().union(*[_values(source, "approval_ids", "approval_id") for source in matched]) if matched else set()
        covered_hashes = set().union(*[_values(source, "content_hashes", "content_hash", "approved_content_hash") for source in matched]) if matched else set()
        hash_status = ("verified" if hashes and hashes.issubset(covered_hashes)
                       else "invalid" if hashes
                       else "not_available_in_gold" if not covered_hashes
                       else "missing" if require_content_hash
                       else "not_available_by_contract")
        hashes_ok = hash_status not in {"invalid", "missing"}
        page_ranges_ok = True
        for key, aggregate in (("source_page_start", min), ("source_page_end", max)):
            pages = [source[key] for source in matched if isinstance(source.get(key), int)]
            if citation.get(key) is not None and pages and citation[key] != aggregate(pages):
                page_ranges_ok = False
        identity_ok = (bool(matched) and identifiers.issubset(covered_ids) and approvals.issubset(covered_approvals)
                       and hashes_ok and page_ranges_ok)
        quote = _normalized(citation.get("support_quote"))
        texts = [_normalized(source.get("source_text") or source.get("support_quote")) for source in matched]
        quote_status = ("verified" if len(quote) >= 4 and any(quote in text for text in texts if text)
                        else "invalid" if quote else "not_provided")
        quote_ok = quote_status != "invalid" and (not spec.get("require_support_quote") or quote_status == "verified")
        findings.append({"index": index, "identity_verified": identity_ok,
                         "content_hash_status": hash_status,
                         "support_quote_status": quote_status, "passed": identity_ok and quote_ok})
    return {"citation_count": len(citations), "verified_count": sum(item["passed"] for item in findings),
            "all_verified": bool(citations) and all(item["passed"] for item in findings),
            "checks": findings}


def _answer_citation_context(answer: str, citations: list[dict[str, Any]], *,
                             legacy_deterministic: bool) -> dict[str, Any]:
    """Bind printed E markers to exact citations, without inventing legacy IDs."""
    markers = set(re.findall(r"\[(E\d+)\]", answer))
    values = [citation.get("context_id") for citation in citations]
    # The existing deterministic public citation contract has only evidence and
    # approval IDs. Its absent mapping is a limitation, not a verified binding.
    if legacy_deterministic and citations and all(value in (None, "") for value in values):
        return {"status": "not_available_by_contract", "passed": True,
                "answer_context_ids": sorted(markers), "citation_context_ids": []}
    valid = bool(values) and all(isinstance(value, str) and re.fullmatch(r"E\d+", value)
                                 for value in values)
    context_ids = set(values) if valid else set()
    passed = bool(markers) and valid and markers.issubset(context_ids)
    return {"status": "verified" if passed else "invalid", "passed": bool(passed),
            "answer_context_ids": sorted(markers), "citation_context_ids": sorted(context_ids)}


# Fixed codes for the server's non-2xx messages (app/api/routes_rag.py). Only the exact
# messages and message prefixes below are recognized; the raw detail text is never stored.
HTTP_ERROR_CODES = ("llm_backend_failed", "llm_backend_unavailable", "llm_output_invalid", "llm_runtime_failed",
                    "evidence_identity_verification_failed", "citation_verification_failed",
                    "claim_audit_fallback_unverified", "other")
_HTTP_ERROR_EXACT = {
    "Configured local LLM backend is not available.": "llm_backend_unavailable",
    "Local LLM backend request failed: backend unavailable": "llm_backend_unavailable",
    "Final evidence identity verification failed": "evidence_identity_verification_failed",
    "Fast chat evidence identity verification failed": "evidence_identity_verification_failed",
    "Answer citation verification failed": "citation_verification_failed",
}
_HTTP_ERROR_PREFIX = (
    ("Local LLM backend request failed: ", "llm_backend_failed"),
    ("Multi-model answer claim audit did not verify: ", "claim_audit_fallback_unverified"),
)
# "Multi-model local QA execution failed: <ExcType>" wraps the whole model chain, so the
# exception class name decides the code. Only the class name is read, and only when it is a
# bare identifier from this allowlist; any other suffix is "other".
_MULTI_MODEL_FAILED = re.compile(r"Multi-model local QA execution failed: ([A-Za-z_][A-Za-z0-9_]{0,63})")
_EXCEPTION_CLASS_CODES = {
    # Transport and timeout classes raised by app/agents/ollama_runtime.py and urllib.
    **dict.fromkeys(("TimeoutError", "URLError", "OllamaRuntimeError", "ConnectionError", "ConnectionResetError",
                     "ConnectionRefusedError", "ConnectionAbortedError", "BrokenPipeError", "RemoteDisconnected",
                     "IncompleteRead"), "llm_backend_failed"),
    # The model returned output the answer or audit checks rejected (grounded_qa.py ValueError,
    # pydantic ValidationError, invalid JSON).
    **dict.fromkeys(("ValueError", "ValidationError", "JSONDecodeError"), "llm_output_invalid"),
    # Runtime state: a required model is not installed (RuntimeError), or an inner HTTPException.
    **dict.fromkeys(("RuntimeError", "HTTPException"), "llm_runtime_failed"),
}


def _http_error_code(http_status: int, response: Mapping[str, Any]) -> str | None:
    """Classify a non-2xx response by its known server message, or None for 2xx."""
    if isinstance(http_status, int) and not isinstance(http_status, bool) and 200 <= http_status < 300:
        return None
    detail = response.get("detail") if isinstance(response, Mapping) else None
    if not isinstance(detail, str):
        return "other"
    if detail in _HTTP_ERROR_EXACT:
        return _HTTP_ERROR_EXACT[detail]
    wrapped = _MULTI_MODEL_FAILED.fullmatch(detail)
    if wrapped:
        return _EXCEPTION_CLASS_CODES.get(wrapped.group(1), "other")
    return next((code for prefix, code in _HTTP_ERROR_PREFIX if detail.startswith(prefix)), "other")


def score_case(spec: Mapping[str, Any], response: Mapping[str, Any], *, http_status: int,
               events: list[dict[str, Any]], duration_ms: float) -> dict[str, Any]:
    """Score observed execution, final output, fact anchors and negative gates separately."""
    orchestration = response.get("orchestration") if isinstance(response.get("orchestration"), dict) else {}
    mode = str(orchestration.get("answer_mode") or "unknown")
    answer = str(response.get("answer") or "")
    raw_citations = response.get("citations", [])
    citations = [item for item in raw_citations if isinstance(item, dict)] if isinstance(raw_citations, list) else []
    generation_events = [event for event in events if event.get("route") == "/api/generate"]
    completed = [event for event in generation_events if event.get("generation_completed") is True]
    model_called = bool(completed)
    # The production path normalizes abstentions to this exact safe response.
    # A mode label alone must not legitimize substantive invented content.
    abstained = _normalized(answer) == _normalized(NO_EVIDENCE_ANSWER)
    audit_status = str(orchestration.get("claim_audit_status") or "")
    citation_status = str(orchestration.get("citation_verification_status") or "")
    fallback = mode == "grounded_extractive" or audit_status.startswith("fallback_")
    audit_accepted = audit_status in {"verified", "deterministic_identity_verified"}
    citation_accepted = citation_status == "verified"
    answer_digest = _sha(_normalized(answer))
    model = orchestration.get("answer_model")
    answer_matches = any(event.get("model") == model and event.get("candidate_answer_sha256") == answer_digest
                         for event in completed)
    final_model_answer = (http_status == 200 and mode == "grounded_local" and model_called
                          and answer_matches and bool(answer.strip()) and not abstained and not fallback
                          and audit_accepted and citation_accepted)
    required = spec.get("required_facts") or []
    fact_checks = []
    for index, fact in enumerate(required):
        alternatives = fact if isinstance(fact, list) else [fact]
        fact_checks.append({"index": index, "matched": any(literal_fact_matches(str(value), answer)
                                                           for value in alternatives)})
    forbidden = spec.get("forbidden_facts") or []
    forbidden_matches = [index for index, fact in enumerate(forbidden) if literal_fact_matches(str(fact), answer)]
    correctness = {"method": "literal_gold_fact_anchors", "scoring_version": "numeric-boundaries-v1",
                   "required_count": len(fact_checks),
                   "matched_count": sum(item["matched"] for item in fact_checks),
                   "fact_checks": fact_checks, "forbidden_match_indices": forbidden_matches,
                   "status": "passed" if fact_checks and all(item["matched"] for item in fact_checks) and not forbidden_matches
                   else "failed" if forbidden_matches or fact_checks else "unverified"}
    legacy_deterministic = audit_status == "deterministic_identity_verified"
    citation_checks = check_citations(citations, spec, require_content_hash=not legacy_deterministic)
    context_check = _answer_citation_context(answer, citations, legacy_deterministic=legacy_deterministic)
    limitations = []
    if (context_check["status"] == "not_available_by_contract"
            or any(item["content_hash_status"] == "not_available_by_contract" for item in citation_checks["checks"])):
        limitations.append("deterministic_citation_omits_context_and_hash")
    if any(item["content_hash_status"] == "not_available_in_gold" for item in citation_checks["checks"]):
        limitations.append("content_hash_not_available_in_gold")
    errors: list[str] = []
    expect_block = bool(spec.get("expected_block"))
    expect_abstain = bool(spec.get("expected_abstain") or spec.get("expect_no_evidence"))
    expected_status = int(spec.get("expected_http_status") or (403 if expect_block else 200))
    if http_status != expected_status:
        errors.append("unexpected_http_status")
    if expect_block:
        if http_status not in {400, 401, 403, 404, 422}:
            errors.append("expected_client_rejection")
        if answer or citations or generation_events:
            errors.append("blocked_case_exposed_answer_or_called_model")
    elif expect_abstain:
        if http_status != 200 or not abstained or citations:
            errors.append("expected_clean_abstention")
        if forbidden_matches:
            errors.append("forbidden_fact_exposed")
    else:
        if not audit_status:
            errors.append("claim_audit_status_missing")
        if not citation_status:
            errors.append("citation_verification_status_missing")
        if not audit_accepted:
            errors.append("claim_audit_not_accepted")
        if not citation_accepted:
            errors.append("citation_verification_not_accepted")
        if not final_model_answer:
            errors.append("final_model_generation_not_verified")
        if correctness["status"] != "passed":
            errors.append("gold_facts_" + str(correctness["status"]))
        if not citation_checks["all_verified"]:
            errors.append("citation_not_verified")
        if not context_check["passed"]:
            errors.append("answer_citation_context_not_verified")
    if not isinstance(raw_citations, list) or len(citations) != len(raw_citations):
        errors.append("invalid_citations_shape")
    return {"id": str(spec.get("id") or ""), "status": "passed" if not errors else "failed",
            "verdict_version": VERDICT_VERSION,
            "http_status": http_status, "http_error_code": _http_error_code(http_status, response),
            "expected_abstain": expect_abstain, "expected_block": expect_block,
            "actual_model_called": model_called, "model_request_attempt_count": len(generation_events),
            "completed_generation_count": len(completed), "final_model_answer": final_model_answer,
            "answer_matches_observed_generation": answer_matches, "answer_model": model,
            "answer_mode": mode, "fallback": fallback, "abstained": abstained,
            "answer_present": bool(answer.strip()),
            "claim_audit_status": orchestration.get("claim_audit_status"),
            "citation_verification_status": orchestration.get("citation_verification_status"),
            "answer_citation_context": context_check, "verification_limitations": limitations,
            "correctness": correctness, "citation_checks": citation_checks,
            "duration_ms": round(duration_ms, 3), "errors": errors,
            "generation_events": generation_events}


def _query_specs(query_spec: Mapping[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = query_spec.get("queries") if isinstance(query_spec, Mapping) else query_spec
    if not isinstance(specs, list) or not specs:
        raise ValueError("nonempty_queries_required")
    ids: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict) or not str(spec.get("id") or "") or not str(spec.get("query") or "").strip():
            raise ValueError("case_id_and_query_required")
        if spec["id"] in ids:
            raise ValueError("duplicate_case_id")
        ids.add(spec["id"])
        for key in ("expected_abstain", "expect_no_evidence", "expected_block", "require_support_quote"):
            if key in spec and not isinstance(spec[key], bool):
                raise ValueError("case_boolean_required")
        if spec.get("expected_block") and int(spec.get("expected_http_status") or 403) not in {400, 401, 403, 404, 422}:
            raise ValueError("expected_block_requires_client_rejection_status")
        if (spec.get("expected_abstain") or spec.get("expect_no_evidence")) and (
            spec.get("expected_block") or int(spec.get("expected_http_status") or 200) != 200
        ):
            raise ValueError("expected_abstention_requires_success_status")
        for key in ("required_facts", "forbidden_facts", "allowed_citations"):
            if key in spec and not isinstance(spec[key], list):
                raise ValueError("case_list_required")
        for fact in spec.get("required_facts", []):
            values = fact if isinstance(fact, list) else [fact]
            if not values or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("nonempty_fact_anchors_required")
    return specs


def _request_payload(spec: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    overrides = spec.get("request_overrides") or {}
    if not isinstance(overrides, dict) or set(overrides) - _REQUEST_FIELDS or set(defaults) - _REQUEST_FIELDS:
        raise ValueError("unsupported_request_override")
    payload = {**defaults, **overrides, "query": spec["query"]}
    if spec.get("as_of_date") and "as_of_date" not in overrides:
        payload["as_of_date"] = spec["as_of_date"]
    return payload


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 3)


def _write_private_json(directory: Path, filename: str, payload: Any) -> Path:
    target = directory / filename
    if target.resolve().parent != directory:
        raise ValueError("case_output_path_escaped")
    # Evidence is append-only by filename. A rerun must choose a new directory.
    with target.open("xb") as stream:
        stream.write(_canonical(payload) + b"\n")
    return target


def _require_fresh_result_directory(directory: Path) -> None:
    reports = ("live-local-llm-report.json", "mcp-fetch-llm-bridge-report.json")
    if any((directory / name).exists() or (directory / name).is_symlink() for name in reports):
        raise ValueError("result_destination_already_contains_evidence")
    if any(directory.glob("case-*.json")) or any(directory.glob("bridge-*.json")):
        raise ValueError("result_destination_already_contains_evidence")


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [case for case in cases if not case["expected_abstain"] and not case["expected_block"]]
    negative = [case for case in cases if case["expected_abstain"] or case["expected_block"]]
    durations = [float(case["duration_ms"]) for case in cases]
    fact_count = sum(case["correctness"]["required_count"] for case in answerable)
    matched = sum(case["correctness"]["matched_count"] for case in answerable)
    return {"case_count": len(cases), "passed_count": sum(case["status"] == "passed" for case in cases),
            "failed_count": sum(case["status"] != "passed" for case in cases),
            "answerable_count": len(answerable), "negative_count": len(negative),
            "actual_model_called_count": sum(case["actual_model_called"] for case in cases),
            "final_model_answer_count": sum(case["final_model_answer"] for case in answerable),
            "final_model_answer_rate": sum(case["final_model_answer"] for case in answerable) / len(answerable) if answerable else None,
            "fallback_count": sum(case["fallback"] for case in cases),
            "abstention_count": sum(case["abstained"] for case in cases),
            "negative_passed_count": sum(case["status"] == "passed" for case in negative),
            "required_fact_count": fact_count, "matched_fact_count": matched,
            "required_fact_match_rate": matched / fact_count if fact_count else None,
            "citation_count": sum(case["citation_checks"]["citation_count"] for case in cases),
            "verified_citation_count": sum(case["citation_checks"]["verified_count"] for case in cases),
            "p50_duration_ms": _percentile(durations, .5), "p95_duration_ms": _percentile(durations, .95)}


def evaluate_live_local_llm(*, api_endpoint: str, query_spec: Mapping[str, Any] | list[dict[str, Any]],
                            observer: LocalOllamaObserver, output_dir: Path,
                            request_defaults: Mapping[str, Any] | None = None,
                            headers: Mapping[str, str] | None = None,
                            case_headers: Mapping[str, Mapping[str, str]] | None = None,
                            timeout_seconds: float = 600) -> dict[str, Any]:
    """Call a dedicated real HTTP API serially and save private evidence.

    ``case_headers`` permits explicit tenant-bound test identities without storing
    tokens in query specifications or reports. No headers are ever serialized.
    """
    endpoint = validate_loopback_endpoint(api_endpoint)
    specs = _query_specs(query_spec)
    defaults = dict(request_defaults or {})
    requests = [_request_payload(spec, defaults) for spec in specs]
    directory = private_output_directory(output_dir)
    _require_fresh_result_directory(directory)
    if not 0 < timeout_seconds <= 1200:
        raise ValueError("invalid_api_timeout")
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "path": "http_api_to_ollama",
                              "started_at": _now(), "query_spec_sha256": _sha(_canonical(query_spec)),
                              "provenance": observer.provenance(), "request_defaults": defaults,
                              "timing_policy": {"duration_ms": "case_wall_time_including_http_and_observer_drain",
                                                "generation_duration_ms": "upstream_nonstream_request_wall_time",
                                                "first_token_latency_measured": False},
                              "cases": []}
    consecutive_server_failures = 0
    for index, (spec, payload) in enumerate(zip(specs, requests), start=1):
        case_id = str(spec["id"])
        started = time.perf_counter()
        error_type = None
        try:
            with observer.case(case_id):
                status, response = _read_json(endpoint + "/api/rag/chat", payload=payload,
                                              headers={**dict(headers or {}), **dict((case_headers or {}).get(case_id, {}))},
                                              timeout=timeout_seconds)
        except Exception as exc:
            status, response, error_type = 0, {}, type(exc).__name__
        elapsed = (time.perf_counter() - started) * 1000
        events = observer.events_for(case_id)
        result = score_case(spec, response, http_status=status, events=events, duration_ms=elapsed)
        result["request_modes"] = {key: payload.get(key) for key in
                                   ("llm_backend", "orchestration_mode", "retrieval_mode", "claim_audit_mode")}
        if error_type:
            result["error_type"] = error_type
        filename = f"case-{index:04d}-{_sha(case_id)[:12]}.json"
        private_payload = {"request": payload, "response": response, "result": result}
        # Never allow user-supplied case identifiers to become filesystem paths.
        artifact = _write_private_json(directory, filename, private_payload)
        result["artifact_name"] = filename
        result["artifact_sha256"] = _sha(artifact.read_bytes())
        report["cases"].append(result)
        # The server may still be computing after an HTTP timeout. Do not queue
        # another case on top of it; the process owner must reap that API.
        if error_type is not None:
            report["interrupted_reason"] = "transport_or_observer_failure"
            break
        consecutive_server_failures = consecutive_server_failures + 1 if status >= 500 else 0
        if consecutive_server_failures >= _MAX_CONSECUTIVE_SERVER_FAILURES:
            report["interrupted_reason"] = "consecutive_server_failures"
            break
    report["aggregate"] = summarize_cases(report["cases"])
    report["not_run_count"] = len(specs) - len(report["cases"])
    report["not_run_case_ids"] = [str(spec["id"]) for spec in specs[len(report["cases"]):]]
    report["finished_at"] = _now()
    report["passed"] = (len(report["cases"]) == len(specs)
                        and report["aggregate"]["failed_count"] == 0)
    _write_private_json(directory, "live-local-llm-report.json", report)
    return report


def mcp_fetch_evidence(case: Mapping[str, Any], *, allow_empty: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert actual successful MCP fetch payloads without rewriting their text."""
    if case.get("passed") is not True or not isinstance(case.get("fetch"), list):
        raise ValueError("mcp_case_not_verified")
    if allow_empty and not case["fetch"]:
        search = case.get("search")
        if (case.get("search_protocol_passed") is True and type(case.get("result_count")) is int
                and case["result_count"] == 0 and isinstance(search, dict) and search.get("results") == []):
            return [], []
        raise ValueError("mcp_empty_search_not_verified")
    evidence: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in case["fetch"]:
        if not isinstance(item, dict) or item.get("passed") is not True or not isinstance(item.get("payload"), dict):
            raise ValueError("mcp_fetch_not_verified")
        payload = item["payload"]
        metadata = payload.get("metadata")
        text = payload.get("text")
        if not isinstance(metadata, dict) or not isinstance(text, str) or not text.strip():
            raise ValueError("mcp_fetch_evidence_missing")
        content_hash = str(metadata.get("approved_content_hash") or metadata.get("content_hash") or "")
        if (metadata.get("approval_status") != "approved" or not metadata.get("approval_id")
                or not metadata.get("document_id") or not metadata.get("chunk_id")
                or re.fullmatch(r"[a-f0-9]{64}", content_hash) is None):
            raise ValueError("mcp_fetch_approval_identity_missing")
        identity = str(metadata["document_id"]) + ":" + str(metadata["chunk_id"])
        if identity in seen:
            continue
        seen.add(identity)
        evidence.append({**metadata, "text": text})
        lineage.append({"document_id": metadata["document_id"], "chunk_id": metadata["chunk_id"],
                        "mcp_fetch_payload_sha256": _sha(_canonical(payload)),
                        "mcp_text_sha256": _sha(text), "forwarded_text_sha256": _sha(evidence[-1]["text"]),
                        "approved_content_hash": content_hash})
    if not evidence:
        raise ValueError("mcp_fetch_evidence_empty")
    return evidence, lineage


def evaluate_mcp_fetch_bridge(*, mcp_report: Mapping[str, Any] | Path,
                              query_spec: Mapping[str, Any] | list[dict[str, Any]],
                              observer: LocalOllamaObserver, output_dir: Path,
                              claim_audit_mode: str = "model") -> dict[str, Any]:
    """Exercise MCP-fetched evidence through existing QA roles as a harness path.

    This is deliberately separate from the production HTTP API result. The input
    must be a successful real transport report with the exact same case query.
    """
    from app.api.routes_rag import _orchestrated_chat_answer
    from app.core.config import Settings

    if claim_audit_mode not in {"model", "deterministic"}:
        raise ValueError("invalid_claim_audit_mode")
    if isinstance(mcp_report, Path):
        raw = mcp_report.read_bytes()
        source = json.loads(raw)
    else:
        source = dict(mcp_report)
        raw = _canonical(source)
    if not isinstance(source, dict) or source.get("passed") is not True:
        raise ValueError("mcp_transport_report_not_verified")
    directory = private_output_directory(output_dir)
    _require_fresh_result_directory(directory)
    specs = _query_specs(query_spec)
    by_id = {str(case.get("id")): case for case in source.get("cases", []) if isinstance(case, dict)}
    settings = Settings(data_dir=directory / "unused-runtime", rag_llm_backend="ollama",
                        rag_llm_endpoint=observer.endpoint, rag_llm_model="qwen3:8b")
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "path": "mcp_fetch_to_ollama_harness",
                              "production_endpoint": False, "started_at": _now(),
                              "mcp_report_sha256": _sha(raw), "query_spec_sha256": _sha(_canonical(query_spec)),
                              "approval_review_status": source.get("approval_review_status"),
                              "provenance": observer.provenance(), "cases": []}
    consecutive_server_failures = 0
    for index, spec in enumerate(specs, start=1):
        case_id = str(spec["id"])
        started = time.perf_counter()
        lineage: list[dict[str, Any]] = []
        response: dict[str, Any] = {}
        status = 0
        error_type = None
        error_chain: list[str] = []
        try:
            case = by_id.get(case_id)
            if case is None or case.get("query") != spec["query"]:
                raise ValueError("mcp_query_identity_mismatch")
            evidence, lineage = mcp_fetch_evidence(
                case, allow_empty=bool(spec.get("expected_abstain") or spec.get("expect_no_evidence")),
            )
            with observer.case(case_id):
                answer = _orchestrated_chat_answer(settings, spec["query"], evidence,
                                                   use_model_claim_audit=claim_audit_mode == "model")
            response = {"answer": answer["answer"], "citations": answer["citations"], "orchestration": answer}
            status = 200
        except Exception as exc:
            error_type = type(exc).__name__
            status = int(getattr(exc, "status_code", 0))
            current: BaseException | None = exc
            while current is not None and len(error_chain) < 5:
                error_chain.append(type(current).__name__)
                current = current.__cause__
        result = score_case(spec, response, http_status=status, events=observer.events_for(case_id),
                            duration_ms=(time.perf_counter() - started) * 1000)
        result["mcp_evidence_lineage"] = lineage
        if error_type:
            result["error_type"] = error_type
            # Types diagnose nested runtime timeouts without serializing messages
            # that can contain private paths, tokens or source document text.
            result["error_chain_types"] = error_chain
        filename = f"bridge-{index:04d}-{_sha(case_id)[:12]}.json"
        artifact = _write_private_json(directory, filename, {"response": response, "result": result})
        result.update(artifact_name=filename, artifact_sha256=_sha(artifact.read_bytes()))
        report["cases"].append(result)
        if error_type is not None and status == 0:
            report["interrupted_reason"] = "transport_observer_or_evidence_failure"
            break
        consecutive_server_failures = consecutive_server_failures + 1 if status >= 500 else 0
        if consecutive_server_failures >= _MAX_CONSECUTIVE_SERVER_FAILURES:
            report["interrupted_reason"] = "consecutive_server_failures"
            break
    report["aggregate"] = summarize_cases(report["cases"])
    report["not_run_count"] = len(specs) - len(report["cases"])
    report["not_run_case_ids"] = [str(spec["id"]) for spec in specs[len(report["cases"]):]]
    report["passed"] = (len(report["cases"]) == len(specs)
                        and report["aggregate"]["failed_count"] == 0)
    report["finished_at"] = _now()
    _write_private_json(directory, "mcp-fetch-llm-bridge-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--observer-port", type=int, required=True,
                        help="The dedicated API must already use this loopback RAG_LLM_ENDPOINT port.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--retrieval-mode", choices=("auto", "fast"), default="auto")
    parser.add_argument("--claim-audit-mode", choices=("model", "deterministic"), default="model")
    parser.add_argument("--tenant-id")
    parser.add_argument("--auth-token-env", default="REG_RAG_EVAL_API_TOKEN",
                        help="Read a bearer token from this environment variable; never serialize it.")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.query_spec.read_text(encoding="utf-8"))
        token = os.environ.get(args.auth_token_env, "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if args.tenant_id:
            headers["X-Tenant-Id"] = args.tenant_id
        with LocalOllamaObserver(args.ollama_url, port=args.observer_port) as observer:
            report = evaluate_live_local_llm(api_endpoint=args.api_url, query_spec=spec, observer=observer,
                                            output_dir=args.output_dir, timeout_seconds=args.timeout_seconds,
                                            headers=headers,
                                            request_defaults={"llm_backend": "ollama", "orchestration_mode": "multi_model",
                                                              "retrieval_mode": args.retrieval_mode, "claim_audit_mode": args.claim_audit_mode})
    except Exception as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        return 2
    print(json.dumps({"schema_version": SCHEMA_VERSION, "passed": report["passed"], "aggregate": report["aggregate"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
