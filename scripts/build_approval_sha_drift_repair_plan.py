from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


RUNTIME_REPORT_TYPES = {"aks_mcp_publish_runtime", "mcp_publish_runtime"}

APPROVAL_SOURCE_ARTIFACTS = {
    "worklist_json": {
        "label": "approval_worklist",
        "path_key": "worklist_report_path",
        "sha_key": "worklist_report_sha256",
    },
    "review_batch_manifest_json": {
        "label": "approval_review_batch_manifest",
        "path_key": "review_batch_manifest_path",
        "sha_key": "review_batch_manifest_sha256",
    },
}

VECTOR_APPROVAL_FIELDS = {
    "approval_worklist_report_sha256": "worklist_report_sha256",
    "approval_review_batch_manifest_sha256": "review_batch_manifest_sha256",
}

JOURNAL_APPROVAL_FIELDS = {
    "worklist_report_sha256": "worklist_report_sha256",
    "review_batch_manifest_sha256": "review_batch_manifest_sha256",
}


def build_approval_sha_drift_repair_plan(
    *,
    publish_runtime_report: Path,
    repo_root: Path = PROJECT_ROOT,
    vector_jsonl: Path | None = None,
    approval_journal: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    runtime_report_path = _resolve_path(repo_root, publish_runtime_report, base_dir=Path.cwd())
    runtime = _load_runtime_report(runtime_report_path)
    approval_evidence = runtime.get("approval_evidence") if isinstance(runtime.get("approval_evidence"), dict) else {}
    blockers: list[dict[str, Any]] = []

    source_artifacts = _source_artifact_statuses(
        repo_root=repo_root,
        runtime_report_path=runtime_report_path,
        approval_evidence=approval_evidence,
    )
    _append_source_artifact_blockers(source_artifacts, blockers)

    vector_path = (
        _resolve_path(repo_root, vector_jsonl, base_dir=runtime_report_path.parent)
        if vector_jsonl is not None
        else _infer_vector_path(repo_root=repo_root, runtime=runtime, runtime_report_path=runtime_report_path)
    )
    vector_status = _jsonl_approval_field_status(
        repo_root=repo_root,
        path=vector_path,
        path_role="approved_vectors_jsonl",
        scope="vector_metadata",
        field_to_evidence_key=VECTOR_APPROVAL_FIELDS,
        approval_evidence=approval_evidence,
        value_getter=lambda record, field: _dict_value(record.get("metadata"), field),
    )
    _append_jsonl_status_blockers(vector_status, blockers, code_prefix="publish-runtime-vector")

    journal_path = (
        _resolve_path(repo_root, approval_journal, base_dir=runtime_report_path.parent)
        if approval_journal is not None
        else _infer_approval_journal_path(repo_root=repo_root, runtime=runtime, runtime_report_path=runtime_report_path)
    )
    journal_status = _jsonl_approval_field_status(
        repo_root=repo_root,
        path=journal_path,
        path_role="approval_journal_jsonl",
        scope="approval_journal",
        field_to_evidence_key=JOURNAL_APPROVAL_FIELDS,
        approval_evidence=approval_evidence,
        value_getter=lambda record, field: _dict_value(record.get("worklist_evidence"), field),
    )
    _append_jsonl_status_blockers(journal_status, blockers, code_prefix="publish-runtime-journal")

    blocker_codes = sorted({str(blocker["code"]) for blocker in blockers})
    repair_sequence = _repair_sequence(blocker_codes=blocker_codes)
    report = {
        "report_type": "approval_sha_drift_repair_plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(repo_root),
        "publish_runtime_report": _display_path(runtime_report_path, repo_root),
        "publish_runtime_report_type": runtime.get("report_type"),
        "tenant_id": runtime.get("tenant_id"),
        "passed": not blockers,
        "release_gate_status": "approval_sha_lineage_ready" if not blockers else "blocked_approval_sha_drift",
        "blocker_count": len(blockers),
        "blocker_codes": blocker_codes,
        "blockers": blockers,
        "approval_source_artifacts": source_artifacts,
        "vector_approval_sha_status": vector_status,
        "approval_journal_sha_status": journal_status,
        "repair_sequence": repair_sequence,
        "operator_controls": {
            "read_only": True,
            "auto_approval": False,
            "auto_reindex": False,
            "direct_vector_metadata_patch_allowed": False,
            "direct_journal_edit_allowed": False,
            "requires_review_workflow_for_approval_mutation": True,
            "requires_reindex_or_runtime_regeneration_for_vector_repair": True,
        },
        "safety_note": (
            "This plan is read-only. It does not approve chunks, edit approval journals, "
            "patch Vector DB metadata, reindex, or publish MCP artifacts."
        ),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _load_runtime_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("publish_runtime_report must be a JSON object.")
    report_type = str(payload.get("report_type") or "")
    if report_type not in RUNTIME_REPORT_TYPES:
        raise ValueError(
            "publish_runtime_report must have report_type "
            f"{sorted(RUNTIME_REPORT_TYPES)}; got {report_type!r}."
        )
    return payload


def _source_artifact_statuses(
    *,
    repo_root: Path,
    runtime_report_path: Path,
    approval_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    artifact_paths = approval_evidence.get("artifacts") if isinstance(approval_evidence.get("artifacts"), dict) else {}
    statuses: list[dict[str, Any]] = []
    for role, spec in APPROVAL_SOURCE_ARTIFACTS.items():
        claimed_sha = str(approval_evidence.get(spec["sha_key"]) or "")
        path_value = str(artifact_paths.get(role) or approval_evidence.get(spec["path_key"]) or "")
        resolved_path = _resolve_path(repo_root, Path(path_value), base_dir=runtime_report_path.parent) if path_value else None
        actual_sha = _sha256_file(resolved_path) if resolved_path and resolved_path.is_file() else None
        if not path_value:
            status = "path_missing"
        elif resolved_path is None or not resolved_path.is_file():
            status = "file_missing"
        elif not _valid_sha256(claimed_sha):
            status = "claimed_sha_missing"
        elif actual_sha != claimed_sha:
            status = "sha_drift"
        else:
            status = "passed"
        statuses.append(
            {
                "role": role,
                "label": spec["label"],
                "source_path": path_value,
                "resolved_path": _display_path(resolved_path, repo_root) if resolved_path else None,
                "claimed_sha256": claimed_sha,
                "actual_sha256": actual_sha,
                "status": status,
                "passed": status == "passed",
            }
        )
    return statuses


def _jsonl_approval_field_status(
    *,
    repo_root: Path,
    path: Path | None,
    path_role: str,
    scope: str,
    field_to_evidence_key: dict[str, str],
    approval_evidence: dict[str, Any],
    value_getter: Callable[[dict[str, Any], str], object],
) -> dict[str, Any]:
    jsonl = _read_jsonl_status(path) if path and path.is_file() else _empty_jsonl_status()
    records = list(jsonl["records"])
    checks = [
        _approval_field_check(
            records=records,
            scope=scope,
            field=field,
            expected_sha256=str(approval_evidence.get(evidence_key) or ""),
            value_getter=value_getter,
        )
        for field, evidence_key in field_to_evidence_key.items()
    ]
    if path is None:
        status = "path_missing"
    elif not path.is_file():
        status = "file_missing"
    elif jsonl["malformed_line_count"] or jsonl["non_object_line_count"]:
        status = "malformed_jsonl"
    elif not records:
        status = "records_missing"
    elif all(check["passed"] for check in checks):
        status = "passed"
    else:
        status = "sha_drift"
    return {
        "path_role": path_role,
        "path": _display_path(path, repo_root) if path else None,
        "status": status,
        "passed": status == "passed",
        "line_count": jsonl["line_count"],
        "record_count": len(records),
        "malformed_line_count": jsonl["malformed_line_count"],
        "non_object_line_count": jsonl["non_object_line_count"],
        "first_malformed_lines": jsonl["first_malformed_lines"],
        "checks": checks,
    }


def _approval_field_check(
    *,
    records: list[dict[str, Any]],
    scope: str,
    field: str,
    expected_sha256: str,
    value_getter: Callable[[dict[str, Any], str], object],
) -> dict[str, Any]:
    observed: dict[str, int] = {}
    missing_count = 0
    mismatch_count = 0
    for record in records:
        value = str(value_getter(record, field) or "").strip()
        if not value:
            missing_count += 1
            continue
        observed[value] = observed.get(value, 0) + 1
        if value != expected_sha256:
            mismatch_count += 1
    mismatched = {value: count for value, count in observed.items() if value != expected_sha256}
    return {
        "scope": scope,
        "field": field,
        "expected_sha256": expected_sha256,
        "expected_sha256_valid": _valid_sha256(expected_sha256),
        "record_count": len(records),
        "missing_count": missing_count,
        "mismatch_count": mismatch_count,
        "observed_sha256_counts": dict(sorted(observed.items())),
        "mismatched_sha256_counts": dict(sorted(mismatched.items())),
        "passed": bool(_valid_sha256(expected_sha256) and records and missing_count == 0 and mismatch_count == 0),
    }


def _append_source_artifact_blockers(source_artifacts: list[dict[str, Any]], blockers: list[dict[str, Any]]) -> None:
    status_codes = {
        "path_missing": "approval-source-artifact-path-missing",
        "file_missing": "approval-source-artifact-file-missing",
        "claimed_sha_missing": "approval-source-artifact-fingerprint-missing",
        "sha_drift": "approval-source-artifact-sha-drift",
    }
    for item in source_artifacts:
        status = str(item.get("status") or "")
        if status == "passed":
            continue
        blockers.append(
            {
                "code": status_codes.get(status, "approval-source-artifact-unready"),
                "severity": "blocker",
                "role": item.get("role"),
                "source_path": item.get("source_path"),
                "claimed_sha256": item.get("claimed_sha256"),
                "actual_sha256": item.get("actual_sha256"),
                "reason": "Approval source artifact lineage is not reproducible from the publish runtime report.",
            }
        )


def _append_jsonl_status_blockers(status: dict[str, Any], blockers: list[dict[str, Any]], *, code_prefix: str) -> None:
    if status.get("passed"):
        return
    path_status = str(status.get("status") or "")
    path_role = str(status.get("path_role") or "")
    if path_status in {"path_missing", "file_missing", "records_missing", "malformed_jsonl"}:
        reason = {
            "path_missing": f"{path_role} path could not be inferred for approval SHA lineage verification.",
            "file_missing": f"{path_role} is missing and must be restored before approval SHA lineage verification.",
            "records_missing": f"{path_role} contains no JSON object records for approval SHA lineage verification.",
            "malformed_jsonl": f"{path_role} contains malformed or non-object JSONL rows.",
        }.get(path_status, f"{path_role} is not available for approval SHA lineage verification.")
        blockers.append(
            {
                "code": f"{code_prefix}-{path_status.replace('_', '-')}",
                "severity": "blocker",
                "path_role": path_role,
                "path": status.get("path"),
                "line_count": status.get("line_count"),
                "record_count": status.get("record_count"),
                "malformed_line_count": status.get("malformed_line_count"),
                "non_object_line_count": status.get("non_object_line_count"),
                "first_malformed_lines": status.get("first_malformed_lines"),
                "reason": reason,
            }
        )
        return
    for check in status.get("checks") or []:
        if check.get("passed"):
            continue
        if not check.get("expected_sha256_valid"):
            code = f"{code_prefix}-approval-fingerprint-missing"
        else:
            code = f"{code_prefix}-approval-sha-drift"
        blockers.append(
            {
                "code": code,
                "severity": "blocker",
                "path_role": path_role,
                "path": status.get("path"),
                "field": check.get("field"),
                "expected_sha256": check.get("expected_sha256"),
                "record_count": check.get("record_count"),
                "missing_count": check.get("missing_count"),
                "mismatch_count": check.get("mismatch_count"),
                "observed_sha256_counts": check.get("mismatched_sha256_counts"),
                "reason": "Runtime approval SHA metadata does not match publish runtime approval_evidence.",
            }
        )


def _repair_sequence(*, blocker_codes: Sequence[str]) -> list[dict[str, Any]]:
    code_set = set(blocker_codes)
    steps: list[dict[str, Any]] = []
    if any(code.startswith("approval-source-artifact") for code in code_set):
        steps.append(
            {
                "step": "restore_or_regenerate_approval_source_artifacts",
                "required": True,
                "detail": (
                    "Restore the approval worklist and review batch manifest referenced by the publish runtime "
                    "or regenerate them from the same approved source selection."
                ),
            }
        )
    if "approval-source-artifact-sha-drift" in code_set or "approval-source-artifact-fingerprint-missing" in code_set:
        steps.append(
            {
                "step": "refresh_publish_runtime_approval_evidence",
                "required": True,
                "detail": (
                    "After source artifacts are stable, rerun the publish runtime preparation so approval_evidence "
                    "claims the exact artifact SHA256 values."
                ),
            }
        )
    if any(code.startswith("publish-runtime-journal") for code in code_set):
        steps.append(
            {
                "step": "repair_approval_journal_through_review_workflow",
                "required": True,
                "detail": (
                    "Replay or reapply approval decisions through the review workflow contract so journal "
                    "worklist_evidence matches the stable approval source hashes; do not edit journal lines in place."
                ),
            }
        )
    if any(code.startswith("publish-runtime-vector") for code in code_set):
        steps.append(
            {
                "step": "reindex_or_regenerate_approved_vectors",
                "required": True,
                "detail": (
                    "Reindex or regenerate approved Vector DB records from the approved repository after approval "
                    "source hashes are stable; do not patch vector metadata directly."
                ),
            }
        )
    steps.append(
        {
            "step": "rerun_release_evidence_verification",
            "required": bool(code_set),
            "detail": (
                "Regenerate this repair plan and rerun release evidence verification before MCP handoff. "
                "If there are no blockers, archive the report with the publish runtime evidence."
            ),
        }
    )
    return steps


def _infer_vector_path(*, repo_root: Path, runtime: dict[str, Any], runtime_report_path: Path) -> Path | None:
    tenant_id = str(runtime.get("tenant_id") or "").strip()
    candidates: list[Path] = []
    validation = runtime.get("approval_evidence_validation") if isinstance(runtime.get("approval_evidence_validation"), dict) else {}
    manifest = runtime.get("runtime_manifest") if isinstance(runtime.get("runtime_manifest"), dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for value in (
        validation.get("vector_path"),
        files.get("vector_jsonl"),
        _dict_value(runtime.get("vector_artifacts"), "local_jsonl"),
        _dict_value(runtime.get("vector_artifacts"), "vector_jsonl"),
    ):
        resolved = _resolve_optional_path(repo_root, value, base_dir=runtime_report_path.parent)
        if resolved is not None:
            candidates.append(resolved)
    for value in (runtime.get("tenant_data_dir"), runtime.get("target_data_dir")):
        base = _resolve_optional_path(repo_root, value, base_dir=runtime_report_path.parent)
        if base is None or not tenant_id:
            continue
        candidates.append(base / "vector_db" / tenant_id / "approved_vectors.jsonl")
        candidates.append(base / "tenants" / tenant_id / "vector_db" / tenant_id / "approved_vectors.jsonl")
    return _first_existing_or_first(candidates)


def _infer_approval_journal_path(*, repo_root: Path, runtime: dict[str, Any], runtime_report_path: Path) -> Path | None:
    tenant_id = str(runtime.get("tenant_id") or "").strip()
    candidates: list[Path] = []
    validation = runtime.get("approval_evidence_validation") if isinstance(runtime.get("approval_evidence_validation"), dict) else {}
    manifest = runtime.get("runtime_manifest") if isinstance(runtime.get("runtime_manifest"), dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for value in (validation.get("approval_journal_path"), files.get("approval_journal")):
        resolved = _resolve_optional_path(repo_root, value, base_dir=runtime_report_path.parent)
        if resolved is not None:
            candidates.append(resolved)
    for value in (runtime.get("tenant_data_dir"), runtime.get("target_data_dir")):
        base = _resolve_optional_path(repo_root, value, base_dir=runtime_report_path.parent)
        if base is None:
            continue
        candidates.append(base / "repository" / "journals" / "approvals.jsonl")
        if tenant_id:
            candidates.append(base / "tenants" / tenant_id / "repository" / "journals" / "approvals.jsonl")
    return _first_existing_or_first(candidates)


def _resolve_optional_path(repo_root: Path, value: object, *, base_dir: Path) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _resolve_path(repo_root, Path(text), base_dir=base_dir)


def _resolve_path(repo_root: Path, path: Path, *, base_dir: Path) -> Path:
    raw = str(path).strip()
    if path.is_absolute():
        return path.resolve()
    normalized = raw.replace("\\", "/")
    candidates = [
        (repo_root / normalized).resolve(),
        (base_dir / normalized).resolve(),
        (repo_root / raw).resolve(),
        (base_dir / raw).resolve(),
    ]
    return _first_existing_or_first(candidates) or candidates[0]


def _first_existing_or_first(candidates: Sequence[Path]) -> Path | None:
    if not candidates:
        return None
    seen: set[str] = set()
    deduped: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    for candidate in deduped:
        if candidate.is_file():
            return candidate
    return deduped[0]


def _empty_jsonl_status() -> dict[str, Any]:
    return {
        "records": [],
        "line_count": 0,
        "malformed_line_count": 0,
        "non_object_line_count": 0,
        "first_malformed_lines": [],
    }


def _read_jsonl_status(path: Path | None) -> dict[str, Any]:
    status = _empty_jsonl_status()
    if path is None or not path.is_file():
        return status
    records: list[dict[str, Any]] = []
    first_malformed_lines: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        status["line_count"] += 1
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            status["malformed_line_count"] += 1
            if len(first_malformed_lines) < 5:
                first_malformed_lines.append(
                    {"line_number": line_number, "error": exc.msg, "column": exc.colno}
                )
            continue
        if isinstance(payload, dict):
            records.append(payload)
        else:
            status["non_object_line_count"] += 1
            if len(first_malformed_lines) < 5:
                first_malformed_lines.append(
                    {"line_number": line_number, "error": "JSONL record is not an object", "column": None}
                )
    status["records"] = records
    status["first_malformed_lines"] = first_malformed_lines
    return status


def _dict_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)


def _display_path(path: Path | None, repo_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Approval SHA Drift Repair Plan",
        "",
        f"- Status: `{report.get('release_gate_status')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blockers: `{report.get('blocker_count')}`",
        f"- Runtime report: `{report.get('publish_runtime_report')}`",
        f"- Tenant: `{report.get('tenant_id') or ''}`",
        "",
        "## Source Artifacts",
        "",
        "| Role | Status | Claimed SHA | Actual SHA | Path |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in report.get("approval_source_artifacts") or []:
        lines.append(
            "| "
            f"{_md(item.get('role'))} | "
            f"{_md(item.get('status'))} | "
            f"{_short_sha(item.get('claimed_sha256'))} | "
            f"{_short_sha(item.get('actual_sha256'))} | "
            f"{_md(item.get('resolved_path') or item.get('source_path') or '')} |"
        )
    lines.extend(["", "## Runtime SHA Checks", ""])
    for section_name, key in (
        ("Vector Metadata", "vector_approval_sha_status"),
        ("Approval Journal", "approval_journal_sha_status"),
    ):
        status = report.get(key) or {}
        lines.extend(
            [
                f"### {section_name}",
                "",
                f"- Path: `{status.get('path') or ''}`",
                f"- Status: `{status.get('status')}`",
                f"- Records: `{status.get('record_count')}`",
                "",
                "| Field | Expected SHA | Missing | Mismatch | Observed mismatch SHA counts |",
                "| --- | --- | ---: | ---: | --- |",
            ]
        )
        for check in status.get("checks") or []:
            lines.append(
                "| "
                f"{_md(check.get('field'))} | "
                f"{_short_sha(check.get('expected_sha256'))} | "
                f"{check.get('missing_count')} | "
                f"{check.get('mismatch_count')} | "
                f"{_md(check.get('mismatched_sha256_counts') or {})} |"
            )
        lines.append("")
    lines.extend(["## Repair Sequence", ""])
    for idx, step in enumerate(report.get("repair_sequence") or [], start=1):
        lines.append(f"{idx}. `{step.get('step')}` - {_md(step.get('detail'))}")
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for blocker in report.get("blockers") or []:
            lines.append(f"- `{blocker.get('code')}`: {_md(blocker.get('reason'))}")
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def _short_sha(value: object) -> str:
    text = str(value or "")
    if len(text) < 12:
        return _md(text)
    return _md(f"{text[:12]}...")


def _md(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only repair plan for publish runtime approval SHA drift."
    )
    parser.add_argument("--publish-runtime-report", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--vector-jsonl", type=Path)
    parser.add_argument("--approval-journal", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-drift", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_approval_sha_drift_repair_plan(
        publish_runtime_report=args.publish_runtime_report,
        repo_root=args.repo_root,
        vector_jsonl=args.vector_jsonl,
        approval_journal=args.approval_journal,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(
        json.dumps(
            {
                "ok": bool(report["passed"]),
                "blocker_count": report["blocker_count"],
                "blocker_codes": report["blocker_codes"],
                "out_json": str(args.out_json) if args.out_json else "",
                "out_md": str(args.out_md) if args.out_md else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.fail_on_drift and not report["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
