from __future__ import annotations

"""Run private real-document, real-MCP and live local-model regression evidence.

This command never approves documents. It copies an existing runtime into a new
private run directory, checks the supplied source documents, and exercises the
existing policy boundaries. Technical success does not certify human review.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True)
class RunOptions:
    runtime_source: Path
    corpus_manifest: Path
    query_spec: Path
    output_dir: Path
    tenant_id: str
    profile_id: str | None = None
    tenant_storage_isolation: bool = False
    model: str = "qwen3:8b"
    ollama_endpoint: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 600
    api_role: str = "operator"
    security_levels: tuple[str, ...] = ("internal",)
    enable_kordoc_table_parser: bool = True
    request_defaults: dict[str, Any] = field(default_factory=dict)
    preprocessing_document_ids: tuple[str, ...] = ()


class EvidenceError(ValueError):
    """A fixed, path-free evidence or configuration failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def require_private_path(path: Path, *, project_root: Path = PROJECT_ROOT) -> Path:
    """Reject public-checkout paths, including links into the checkout."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise EvidenceError("private_path_must_be_absolute")
    if any(_is_link(part) for part in (expanded, *expanded.parents)):
        raise EvidenceError("private_path_links_not_allowed")
    resolved = expanded.resolve()
    if resolved == project_root.resolve() or resolved.is_relative_to(project_root.resolve()):
        raise EvidenceError("private_path_inside_public_checkout")
    return resolved


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _load_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EvidenceError("expected_json_object")
    return data


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def runtime_fingerprint(root: Path) -> dict[str, Any]:
    """Hash every supplied runtime file; refuse links and concurrent changes."""
    if _is_link(root):
        raise EvidenceError("runtime_links_not_allowed")
    files = sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix())
    digest = hashlib.sha256()
    count = size = 0
    identities = []
    entries: dict[str, dict[str, Any]] = {}
    for path in files:
        if _is_link(path):
            raise EvidenceError("runtime_links_not_allowed")
        if not path.is_file():
            continue
        before = path.stat()
        content_hash = _sha256(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise EvidenceError("runtime_changed_during_fingerprint")
        name = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(bytes.fromhex(content_hash))
        identities.append((path, after.st_size, after.st_mtime_ns))
        entries[path.relative_to(root).as_posix()] = {"sha256": content_hash, "byte_count": after.st_size}
        count += 1
        size += after.st_size
    if not count:
        raise EvidenceError("runtime_has_no_files")
    if files != sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        raise EvidenceError("runtime_changed_during_fingerprint")
    for path, file_size, modified in identities:
        final = path.stat()
        if (file_size, modified) != (final.st_size, final.st_mtime_ns):
            raise EvidenceError("runtime_changed_during_fingerprint")
    return {"sha256": digest.hexdigest(), "file_count": count, "byte_count": size, "files": entries}


def active_runtime_integrity(root: Path, before: Mapping[str, Any], *,
                             tenant_id: str, tenant_storage_isolation: bool) -> dict[str, Any]:
    """Protect the actual queried copy, allowing only declared append-only logs.

    Approval journals, compressed journals, chunks, manifests, vectors and
    hierarchy databases are immutable. Synchronization files can appear only
    with their writer's exact initial bytes. No directory is exempted wholesale.
    """
    from app.core.tenant_access import tenant_directory_key

    prefixes = {""}
    if tenant_storage_isolation:
        prefixes.add(f"tenants/{tenant_directory_key(tenant_id)}/")
    append_only = {prefix + name for prefix in prefixes for name in (
        "repository/api_audit.jsonl", "repository/journals/rag_traces.jsonl",
    )}
    empty_locks = {prefix + name for prefix in prefixes for name in (
        "repository/.api_audit.lock", "repository/.write.lock",
    )}
    hierarchy_locks = {prefix + "hierarchy/.regulation_hierarchy.sqlite3.reg-rag.lock" for prefix in prefixes}
    after = runtime_fingerprint(root)
    old_files = before["files"]
    new_files = after["files"]
    changes = []
    findings = []

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate audit JSON key")
            result[key] = value
        return result

    for name in sorted(set(old_files) | set(new_files)):
        old, new = old_files.get(name), new_files.get(name)
        if old == new:
            continue
        changes.append(name)
        if name in empty_locks and old is None and new and new["byte_count"] == 0:
            continue
        if (name in hierarchy_locks and (old is None or old["byte_count"] == 0)
                and new == {"sha256": hashlib.sha256(b"\0").hexdigest(), "byte_count": 1}):
            continue
        if name not in append_only or new is None:
            findings.append({"file": name, "reason": "protected_runtime_file_changed"})
            continue
        old_size = old["byte_count"] if old else 0
        if new["byte_count"] < old_size:
            findings.append({"file": name, "reason": "audit_log_truncated"})
            continue
        with (root / name).open("rb") as stream:
            prefix_hash = hashlib.sha256()
            remaining = old_size
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    break
                prefix_hash.update(block)
                remaining -= len(block)
            if old and (remaining or prefix_hash.hexdigest() != old["sha256"]):
                findings.append({"file": name, "reason": "audit_log_prefix_changed"})
                continue
            try:
                if old_size:
                    stream.seek(old_size - 1)
                    if stream.read(1) != b"\n":
                        raise ValueError("existing audit record has no separator")
                for line in stream:
                    if not line.endswith(b"\n") or not isinstance(json.loads(line, object_pairs_hook=unique_object), dict):
                        raise ValueError("invalid appended record")
            except (ValueError, UnicodeError):
                findings.append({"file": name, "reason": "audit_log_append_invalid"})
    return {"passed": not findings, "changed_files": changes, "findings": findings,
            "fingerprint_before": before, "fingerprint_after": after,
            "policy": "immutable_runtime_with_declared_append_only_audit_v1"}


def verify_corpus(manifest_path: Path) -> dict[str, Any]:
    """Validate raw bytes independently of any preexisting approval status."""
    manifest = _load_object(manifest_path)
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise EvidenceError("corpus_has_no_documents")
    seen_ids: set[str] = set()
    source_hashes: set[str] = set()
    checked: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            raise EvidenceError("corpus_document_invalid")
        document_id = str(document.get("document_id") or "")
        if not document_id or document_id in seen_ids:
            raise EvidenceError("corpus_document_identity_invalid")
        seen_ids.add(document_id)
        source = require_private_path(Path(str(document.get("snapshot_pdf") or "")))
        expected = str(document.get("source_sha256") or "").lower()
        if not source.is_file() or len(expected) != 64:
            raise EvidenceError("corpus_source_missing_or_unfingerprinted")
        if _is_link(source) or source.suffix.lower() != ".pdf":
            raise EvidenceError("corpus_source_format_not_supported")
        if _sha256(source) != expected:
            raise EvidenceError("corpus_source_hash_mismatch")
        with source.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise EvidenceError("corpus_pdf_signature_invalid")
        source_hashes.add(expected)
        checked.append({
            "document_id": document_id,
            "source_sha256": expected,
            "human_review_complete": document.get("human_review_complete") is True,
        })
    return {
        "passed": True,
        "manifest_sha256": _sha256(manifest_path),
        "document_count": len(checked),
        "unique_source_count": len(source_hashes),
        "human_review_complete": all(item["human_review_complete"] for item in checked),
        "label_provenance": manifest.get("label_provenance"),
        "documents": checked,
        "new_approval_records_created": 0,
    }


def validate_options(options: RunOptions) -> dict[str, Any]:
    source = require_private_path(options.runtime_source)
    output = require_private_path(options.output_dir)
    corpus = require_private_path(options.corpus_manifest)
    queries = require_private_path(options.query_spec)
    if not source.is_dir() or _is_link(options.runtime_source):
        raise EvidenceError("runtime_source_missing_or_linked")
    if _overlap(source, output):
        raise EvidenceError("output_overlaps_runtime_source")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise EvidenceError("output_must_be_new_or_empty")
    if any(path.is_relative_to(output) for path in (corpus, queries)):
        raise EvidenceError("inputs_must_be_outside_output")
    if type(options.tenant_storage_isolation) is not bool:
        raise EvidenceError("storage_mode_must_be_boolean")
    if type(options.enable_kordoc_table_parser) is not bool:
        raise EvidenceError("table_parser_setting_must_be_boolean")
    if (not isinstance(options.tenant_id, str) or not options.tenant_id.strip()
            or not isinstance(options.model, str) or not options.model.strip()
            or (options.profile_id is not None and (not isinstance(options.profile_id, str) or not options.profile_id.strip()))
            or type(options.timeout_seconds) is not int or not 1 <= options.timeout_seconds <= 600):
        raise EvidenceError("runtime_configuration_invalid")
    # The direct evaluator and stdio probe both use the existing operator role.
    # Reject a mixed-role run instead of attributing viewer/API denials to MCP.
    if options.api_role != "operator":
        raise EvidenceError("unsupported_regression_api_role")
    if (not isinstance(options.security_levels, tuple) or not options.security_levels
            or any(not isinstance(value, str) or value not in {"public", "internal"} for value in options.security_levels)
            or len(set(options.security_levels)) != len(options.security_levels)):
        raise EvidenceError("security_levels_must_be_unique_operator_levels")
    endpoint = urlsplit(options.ollama_endpoint)
    if (endpoint.scheme not in {"http", "https"}
            or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}
            or endpoint.port == 0):
        raise EvidenceError("ollama_endpoint_must_be_loopback")
    payload = _load_object(queries)
    cases = payload.get("queries")
    if not isinstance(cases, list) or not cases:
        raise EvidenceError("query_spec_has_no_cases")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise EvidenceError("query_ids_must_be_unique_nonempty_strings")
    expected_scope = {"tenant_id": options.tenant_id, "profile_id": options.profile_id,
                      "tenant_storage_isolation": options.tenant_storage_isolation}

    def check_scope(scope: Any) -> None:
        if not isinstance(scope, dict):
            raise EvidenceError("query_scope_must_be_object")
        if any(key in scope and (type(scope[key]) is not type(value) or scope[key] != value)
               for key, value in expected_scope.items()):
            raise EvidenceError("query_scope_does_not_match_runtime")

    def check_overrides(overrides: Any) -> None:
        if not isinstance(overrides, dict):
            raise EvidenceError("request_overrides_must_be_object")
        if "profile_id" in overrides and overrides["profile_id"] != options.profile_id:
            raise EvidenceError("mixed_profile_query_spec_not_supported")
        if "llm_backend" in overrides and overrides["llm_backend"] != "ollama":
            raise EvidenceError("live_regression_requires_ollama")
        if "tenant_id" in overrides:
            raise EvidenceError("tenant_is_bound_by_authentication")
        if set(overrides) & {"document_id", "security_levels", "department_ids", "as_of_date", "top_k"}:
            raise EvidenceError("retrieval_overrides_not_shared_by_all_stages")

    check_scope(payload.get("scope", {}))
    check_overrides(options.request_defaults)
    if set(options.request_defaults) - {"llm_backend", "orchestration_mode", "retrieval_mode", "claim_audit_mode", "history", "profile_id"}:
        raise EvidenceError("unsupported_integrated_request_default")
    for case in cases:
        if case.get("profile_id", options.profile_id) != options.profile_id:
            raise EvidenceError("mixed_profile_query_spec_not_supported")
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise EvidenceError("query_text_must_be_nonempty_string")
        check_scope(case.get("scope", {}))
        check_overrides(case.get("request_overrides", {}))
        if set(case.get("request_overrides", {})) - {"profile_id"}:
            raise EvidenceError("case_overrides_must_be_shared_mcp_scope")
        if set(case) & {"document_id", "security_levels", "department_ids", "top_k"}:
            raise EvidenceError("retrieval_overrides_not_shared_by_all_stages")
        document_ids = case.get("target_document_ids", [])
        if (not isinstance(document_ids, list)
                or any(not isinstance(value, str) or not value for value in document_ids)):
            raise EvidenceError("query_document_ids_must_be_string_list")
    if (not isinstance(options.preprocessing_document_ids, tuple)
            or any(not isinstance(value, str) or not value for value in options.preprocessing_document_ids)):
        raise EvidenceError("preprocessing_document_ids_must_be_string_sequence")
    corpus_report = verify_corpus(corpus)
    known_document_ids = {item["document_id"] for item in corpus_report["documents"]}
    requested_document_ids = set(options.preprocessing_document_ids)
    for case in cases:
        requested_document_ids.update(case.get("target_document_ids") or [])
        if case.get("target_document_id"):
            requested_document_ids.add(case["target_document_id"])
    if not requested_document_ids.issubset(known_document_ids):
        raise EvidenceError("query_or_preprocessing_document_missing_from_corpus")
    return {"corpus": corpus_report, "query_count": len(cases), "query_spec_sha256": _sha256(queries)}


@contextmanager
def managed_local_api(options: RunOptions, *, runtime: Path, endpoint: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Start an authenticated, loopback-only API and always reap its process."""
    from app.core.local_http import local_urlopen

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = dict(os.environ)
    env.update({
        "APP_ENV": "local", "DATA_DIR": str(runtime),
        "TENANT_STORAGE_ISOLATION": str(options.tenant_storage_isolation).lower(),
        "API_AUTH_REQUIRED": "true", "API_AUTH_TOKEN": "",
        # The existing viewer role permits only public records. Internal test
        # corpora require the existing operator clearance; policy is unchanged.
        "API_AUTH_TOKENS": json.dumps({token: {"role": options.api_role, "actor": "regression-reader", "tenant_id": options.tenant_id}}),
        "API_DEFAULT_TENANT_ID": options.tenant_id,
        "ENABLE_AGENT_REVIEW": "false", "LOCAL_STRUCTURE_REVIEW_ENABLED": "false",
        "ENABLE_REGULATION_AUTHORING": "false",
        "RAG_LLM_BACKEND": "ollama", "RAG_LLM_ENDPOINT": endpoint,
        "RAG_LLM_MODEL": options.model,
        "RAG_LLM_TIMEOUT_SECONDS": str(options.timeout_seconds),
        "RAG_RATE_LIMIT_REQUESTS_PER_WINDOW": "10000",
        "RAG_TRACE_ENABLED": "true", "API_AUDIT_ENABLED": "true",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONFAULTHANDLER": "1",
    })
    api_url = f"http://127.0.0.1:{port}"
    log_path = options.output_dir / "api-server.log"
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lifecycle = {"pid": process.pid, "started_at": datetime.now(timezone.utc).isoformat(),
                     "cleanup_action": "not_started"}
        lifecycle_path = options.output_dir / "api-process.json"
        try:
            # Record no environment or authentication headers. The PID and
            # pre-cleanup exit code tie native faults to this owned process.
            _write_json(lifecycle_path, lifecycle)
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise EvidenceError("local_api_startup_failed")
                try:
                    with local_urlopen(api_url + "/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise EvidenceError("local_api_startup_timeout")
                time.sleep(0.2)
            yield api_url, {"Authorization": f"Bearer {token}", "X-Tenant-Id": options.tenant_id}
        finally:
            lifecycle["exit_code_before_cleanup"] = process.poll()
            try:
                if lifecycle["exit_code_before_cleanup"] is None:
                    lifecycle["cleanup_action"] = "terminated_by_owner"
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        lifecycle["cleanup_action"] = "killed_by_owner_after_timeout"
                        process.kill()
                        process.wait(timeout=15)
                else:
                    lifecycle["cleanup_action"] = "exited_before_cleanup"
            finally:
                lifecycle["exit_code_after_cleanup"] = process.poll()
                lifecycle["finished_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(lifecycle_path, lifecycle)


def _record_stage(report: dict[str, Any], name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = operation()
        if not isinstance(result, dict):
            raise EvidenceError("stage_report_invalid")
        stage = {"status": "passed" if result.get("passed") is True else "failed", "result": result}
    except Exception as exc:
        stage = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, EvidenceError):
            stage["reason_code"] = str(exc)
    stage["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    report["stages"][name] = stage
    return stage


def run_real_regulation_e2e(options: RunOptions) -> dict[str, Any]:
    """Collect independently reported pipeline stages without automatic approval."""
    validation = validate_options(options)
    from scripts.check_real_regulation_kpi_guardrails import load_policy
    from scripts.report_metadata import capture_mcp_performance_source_state, finalize_mcp_performance_source_state, current_repo_commit

    kpi_policy = load_policy()

    started = datetime.now(timezone.utc)
    source_state = capture_mcp_performance_source_state()
    original_runtime = runtime_fingerprint(options.runtime_source)
    options.output_dir.mkdir(parents=True, exist_ok=True)
    runtime = options.output_dir / "runtime"
    shutil.copytree(options.runtime_source, runtime)
    if runtime_fingerprint(runtime) != original_runtime:
        raise EvidenceError("runtime_copy_hash_mismatch")
    query_payload = _load_object(options.query_spec)
    queries = query_payload["queries"]
    report: dict[str, Any] = {
        "report_type": "real_regulation_e2e", "schema_version": 1,
        "started_at": started.isoformat(), "repo_commit": current_repo_commit(),
        "runtime_source_fingerprint": original_runtime,
        "corpus": validation["corpus"], "query_spec_sha256": validation["query_spec_sha256"],
        "query_count": len(queries), "label_provenance": query_payload.get("label_provenance", "unspecified"),
        "runtime_scope": {"tenant_id": options.tenant_id, "profile_id": options.profile_id, "tenant_storage_isolation": options.tenant_storage_isolation},
        "configuration": {"model": options.model, "api_role": options.api_role, "request_defaults": options.request_defaults,
                          "security_levels": list(options.security_levels), "mcp_top_k": 5,
                          "enable_kordoc_table_parser": options.enable_kordoc_table_parser,
                          "timeout_seconds": options.timeout_seconds, "preprocessing_document_ids": list(options.preprocessing_document_ids)},
        "stages": {}, "passed": False, "product_release_ready": False,
        "human_approval_created": False,
        "kpi_policy": {key: kpi_policy[key] for key in ("policy_id", "policy_version", "sha256")},
    }
    report_path = options.output_dir / "report.json"
    _write_json(report_path, report)

    def preprocessing() -> dict[str, Any]:
        from scripts.real_regulation_corpus_probe import run_corpus_probe
        target_ids = {str(value) for query in queries for value in query.get("target_document_ids", [])}
        target_ids.update(str(query["target_document_id"]) for query in queries if query.get("target_document_id"))
        selected = options.preprocessing_document_ids or tuple(sorted(target_ids)) or None
        result = run_corpus_probe(corpus_manifest=options.corpus_manifest, output_dir=options.output_dir / "preprocessing",
                                 selected_document_ids=selected, enable_kordoc_table_parser=options.enable_kordoc_table_parser)
        completed_ids = {row["source_document_id"] for row in result.get("results", []) if row.get("passed") is True}
        result["query_source_coverage_complete"] = target_ids.issubset(completed_ids)
        result["passed"] = result.get("passed") is True and result["query_source_coverage_complete"]
        return result

    def retrieval() -> dict[str, Any]:
        from scripts.evaluate_mcp_retrieval_quality import evaluate_mcp_retrieval_quality
        return evaluate_mcp_retrieval_quality(data_dir=runtime, tenant_id=options.tenant_id, profile_id=options.profile_id,
            security_levels=list(options.security_levels),
            tenant_storage_isolation=options.tenant_storage_isolation, query_specs=queries, query_spec_source=options.query_spec,
            # Same values as the KPI targets; no-evidence limits mirror the hard guardrail.
            min_recall_at_5=kpi_policy["targets"]["retrieval_recall_at_5"]["minimum"],
            min_mrr=kpi_policy["targets"]["retrieval_mrr"]["minimum"],
            max_no_evidence_false_positive_rate=0.0, min_no_evidence_abstention_rate=1.0,
            out_json=options.output_dir / "retrieval.json")

    def mcp() -> dict[str, Any]:
        from scripts.real_regulation_mcp_probe import run_mcp_probe
        scoped_queries = [{**query, "security_levels": list(options.security_levels)} for query in queries]
        return run_mcp_probe(data_dir=runtime, tenant_id=options.tenant_id, profile_id=options.profile_id,
                             query_specs=scoped_queries, tenant_storage_isolation=options.tenant_storage_isolation,
                             output_path=options.output_dir / "mcp.json", timeout_seconds=options.timeout_seconds)

    def live_llm() -> dict[str, Any]:
        from scripts.evaluate_live_local_llm import LocalOllamaObserver, evaluate_live_local_llm
        defaults = {"llm_backend": "ollama", "orchestration_mode": "multi_model", "retrieval_mode": "auto", "claim_audit_mode": "model"}
        defaults.update(options.request_defaults)
        defaults["security_levels"] = list(options.security_levels)
        if options.profile_id:
            defaults["profile_id"] = options.profile_id
        with LocalOllamaObserver(upstream_endpoint=options.ollama_endpoint, port=0,
                                 timeout_seconds=options.timeout_seconds) as observer:
            with managed_local_api(options, runtime=runtime, endpoint=observer.endpoint) as (api_url, headers):
                return evaluate_live_local_llm(api_endpoint=api_url, query_spec=query_payload, observer=observer,
                    output_dir=options.output_dir / "llm", request_defaults=defaults, headers=headers, timeout_seconds=options.timeout_seconds)

    for name, operation in (("preprocessing", preprocessing), ("retrieval", retrieval), ("mcp", mcp), ("live_llm", live_llm)):
        _record_stage(report, name, operation)
        _write_json(report_path, report)

    def integrity() -> dict[str, Any]:
        same_runtime = runtime_fingerprint(options.runtime_source) == original_runtime
        same_corpus = verify_corpus(options.corpus_manifest) == validation["corpus"]
        same_queries = _sha256(options.query_spec) == validation["query_spec_sha256"]
        return {"passed": same_runtime and same_corpus and same_queries, "runtime_source_unchanged": same_runtime,
                "corpus_unchanged": same_corpus, "query_spec_unchanged": same_queries}

    _record_stage(report, "input_integrity", integrity)
    _record_stage(report, "active_runtime_integrity", lambda: active_runtime_integrity(
        runtime, original_runtime, tenant_id=options.tenant_id,
        tenant_storage_isolation=options.tenant_storage_isolation))
    report["source_state"] = finalize_mcp_performance_source_state(source_state)
    report["passed"] = (all(stage["status"] == "passed" for stage in report["stages"].values())
                        and report["source_state"].get("stable") is True)
    report["ended_at"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
    report["release_limitation"] = "Technical regression evidence does not confer human source review or deployment approval."
    _write_json(report_path, report)
    return report


def options_from_config(config: Mapping[str, Any], output_dir: Path) -> RunOptions:
    for key in ("runtime_source", "corpus_manifest", "query_spec", "tenant_id"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise EvidenceError("required_configuration_missing")
    if not isinstance(config.get("request_defaults", {}), dict):
        raise EvidenceError("request_overrides_must_be_object")
    document_ids = config.get("preprocessing_document_ids", [])
    if not isinstance(document_ids, list):
        raise EvidenceError("preprocessing_document_ids_must_be_string_sequence")
    levels = config.get("security_levels", ["internal"])
    if not isinstance(levels, list):
        raise EvidenceError("security_levels_must_be_unique_operator_levels")
    return RunOptions(runtime_source=Path(config["runtime_source"]), corpus_manifest=Path(config["corpus_manifest"]),
        query_spec=Path(config["query_spec"]), output_dir=output_dir, tenant_id=config["tenant_id"],
        profile_id=config.get("profile_id"), tenant_storage_isolation=config.get("tenant_storage_isolation", False),
        model=config.get("model", "qwen3:8b"), ollama_endpoint=config.get("ollama_endpoint", "http://127.0.0.1:11434"),
        timeout_seconds=config.get("timeout_seconds", 600), api_role=config.get("api_role", "operator"),
        security_levels=tuple(levels),
        enable_kordoc_table_parser=config.get("enable_kordoc_table_parser", True),
        request_defaults=dict(config.get("request_defaults", {})),
        preprocessing_document_ids=tuple(document_ids))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run private real-regulation MCP and live local LLM regression.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        config_path = require_private_path(args.config)
        options = options_from_config(_load_object(config_path), args.output_dir)
        if args.check_only:
            validation = validate_options(options)
            print(json.dumps({"configuration_valid": True, "query_count": validation["query_count"], "execution_performed": False}))
            return 0
        report = run_real_regulation_e2e(options)
        print(json.dumps({"passed": report["passed"], "product_release_ready": False, "query_count": report["query_count"],
                          "stage_status": {name: stage["status"] for name, stage in report["stages"].items()}}))
        return 0 if report["passed"] else 2
    except Exception as exc:
        reason = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        print(json.dumps({"passed": False, "reason_code": reason}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
