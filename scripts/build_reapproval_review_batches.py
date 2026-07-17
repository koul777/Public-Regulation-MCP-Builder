from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.reapproval_decision_contract import (
    REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS,
    REAPPROVAL_DECISION_OPERATOR_DECISIONS,
    REAPPROVAL_DECISION_REQUIRED_FIELDS,
    REAPPROVAL_OVERRIDE_DECISIONS,
)
from scripts.report_metadata import current_repo_commit


DEFAULT_MAX_CHUNKS_PER_BATCH = 100
SUPPORTED_REVIEW_TIERS = ("high", "medium", "low")
CSV_FIELDS = [
    "batch_rank",
    "reapproval_batch_id",
    "suggested_action",
    "review_risk_tier",
    "review_strategy",
    "document_id",
    "document_name",
    "filename",
    "chunk_count",
    "chunk_ids",
    "reapproval_batch_chunk_fingerprint",
    "approval_provenance_missing_field_counts",
    "top_reapproval_reasons",
    "worklist_report_path",
    "worklist_report_sha256",
    "worklist_chunks_path",
    "worklist_chunks_sha256",
]
DECISION_TEMPLATE_FIELDS = [
    *CSV_FIELDS,
    "operator_decision",
    "reviewer_id",
    "reviewed_at",
    "decision_notes",
    "chunk_decision_overrides_json",
    "approval_scope_confirmation",
    "allowed_operator_decisions",
    "required_operator_fields",
    "approval_scope_confirmation_options",
    "override_decision_options",
    "decision_entry_guidance",
]

DECISION_ENTRY_GUIDANCE = (
    "Fill one batch-level operator_decision from allowed_operator_decisions. "
    "Use partial_with_overrides only with non-empty chunk_decision_overrides_json keyed by chunk_id. "
    "Blank rows, unsupported values, and missing scope confirmation remain blocked."
)


def build_reapproval_review_batches(
    *,
    worklist_report: str | Path,
    worklist_chunks_report: str | Path | None = None,
    worklist_chunks_csv: str | Path | None = None,
    worklist_report_artifact_path: str | None = None,
    worklist_chunks_artifact_path: str | None = None,
    max_chunks_per_batch: int = DEFAULT_MAX_CHUNKS_PER_BATCH,
    include_review_tiers: Sequence[str] = SUPPORTED_REVIEW_TIERS,
    include_suggested_actions: Sequence[str] | None = None,
) -> dict[str, Any]:
    if max_chunks_per_batch <= 0:
        raise ValueError("max_chunks_per_batch must be greater than zero.")
    if bool(worklist_chunks_report) == bool(worklist_chunks_csv):
        raise ValueError("Provide exactly one of worklist_chunks_report or worklist_chunks_csv.")

    include_tiers = tuple(dict.fromkeys(str(value).strip().lower() for value in include_review_tiers))
    unsupported_tiers = sorted(set(include_tiers) - set(SUPPORTED_REVIEW_TIERS))
    if unsupported_tiers:
        raise ValueError(f"Unsupported review tiers: {', '.join(unsupported_tiers)}")
    include_actions = tuple(
        dict.fromkeys(str(value).strip() for value in (include_suggested_actions or []) if str(value).strip())
    )

    worklist_path = Path(worklist_report)
    chunks_path = Path(worklist_chunks_report or worklist_chunks_csv or "")
    worklist = _load_worklist_report(worklist_path)
    chunk_payload, chunk_rows = _load_chunk_candidates(chunks_path, is_csv=bool(worklist_chunks_csv))
    worklist_sha256 = _sha256_file(worklist_path)
    chunks_sha256 = _sha256_file(chunks_path)
    worklist_evidence_path = _safe_relative_artifact_path(
        worklist_report_artifact_path if worklist_report_artifact_path else worklist_path
    )
    chunks_evidence_path = _safe_relative_artifact_path(
        worklist_chunks_artifact_path if worklist_chunks_artifact_path else chunks_path
    )

    findings: list[dict[str, Any]] = []
    if not worklist_evidence_path:
        findings.append(
            _finding(
                "blocker",
                "worklist-report-path-not-relative",
                "Reapproval worklist evidence path must be a safe relative artifact path.",
                path=str(worklist_path),
            )
        )
    if not chunks_evidence_path:
        findings.append(
            _finding(
                "blocker",
                "worklist-chunks-path-not-relative",
                "Reapproval chunk candidate evidence path must be a safe relative artifact path.",
                path=str(chunks_path),
            )
        )
    _append_consistency_findings(findings, worklist=worklist, chunk_payload=chunk_payload, chunk_rows=chunk_rows)

    selected_rows = [
        row
        for row in chunk_rows
        if str(row.get("review_risk_tier") or "").strip().lower() in include_tiers
        and (not include_actions or str(row.get("suggested_action") or "").strip() in include_actions)
    ]
    selected_rows.sort(
        key=lambda row: (
            _int(row.get("document_rank")),
            str(row.get("suggested_action") or ""),
            _tier_order(row.get("review_risk_tier")),
            str(row.get("document_id") or ""),
            str(row.get("chunk_id") or ""),
        )
    )

    batches: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in selected_rows:
        key = (
            str(row.get("suggested_action") or ""),
            str(row.get("review_risk_tier") or "").strip().lower(),
            str(row.get("document_id") or ""),
        )
        grouped.setdefault(key, []).append(row)
    for group_index, ((suggested_action, risk_tier, document_id), rows) in enumerate(grouped.items(), start=1):
        for local_index, start in enumerate(range(0, len(rows), max_chunks_per_batch), start=1):
            batch_rows = rows[start : start + max_chunks_per_batch]
            fingerprint = _batch_chunk_fingerprint(batch_rows, suggested_action=suggested_action, risk_tier=risk_tier)
            batch_id = _batch_id(
                worklist_sha256=worklist_sha256,
                group_index=group_index,
                local_index=local_index,
                suggested_action=suggested_action,
                risk_tier=risk_tier,
                fingerprint=fingerprint,
            )
            first = batch_rows[0]
            reason_counts = Counter(reason for row in batch_rows for reason in _list_value(row.get("reapproval_reasons")))
            provenance_counts = Counter(
                field for row in batch_rows for field in _list_value(row.get("approval_provenance_missing_fields"))
            )
            chunk_ids = [str(row.get("chunk_id") or "") for row in batch_rows]
            batches.append(
                {
                    "batch_rank": 0,
                    "reapproval_batch_id": batch_id,
                    "reapproval_batch_chunk_fingerprint": fingerprint,
                    "suggested_action": suggested_action,
                    "review_risk_tier": risk_tier,
                    "review_strategy": str(first.get("review_strategy") or ""),
                    "document_id": document_id,
                    "document_name": str(first.get("document_name") or ""),
                    "filename": str(first.get("filename") or ""),
                    "institution_name": str(first.get("institution_name") or ""),
                    "apba_id": str(first.get("apba_id") or ""),
                    "source_system": str(first.get("source_system") or ""),
                    "source_record_id": str(first.get("source_record_id") or ""),
                    "source_file_id": str(first.get("source_file_id") or ""),
                    "chunk_count": len(batch_rows),
                    "chunk_ids": chunk_ids,
                    "chunks": batch_rows,
                    "approval_provenance_missing_field_counts": dict(sorted(provenance_counts.items())),
                    "top_reapproval_reasons": _format_counter(reason_counts, limit=8),
                    "worklist_report_path": worklist_evidence_path,
                    "worklist_report_sha256": worklist_sha256,
                    "worklist_chunks_path": chunks_evidence_path,
                    "worklist_chunks_sha256": chunks_sha256,
                    "reapproval_task_template": {
                        "chunk_ids": chunk_ids,
                        "worklist_report_path": worklist_evidence_path,
                        "worklist_report_sha256": worklist_sha256,
                        "worklist_chunks_path": chunks_evidence_path,
                        "worklist_chunks_sha256": chunks_sha256,
                        "reapproval_batch_id": batch_id,
                        "reapproval_batch_chunk_fingerprint": fingerprint,
                        "suggested_action": suggested_action,
                        "review_risk_tier": risk_tier,
                        "review_strategy": str(first.get("review_strategy") or ""),
                    },
                }
            )

    for rank, batch in enumerate(batches, start=1):
        batch["batch_rank"] = rank

    severity_counts = Counter(str(item.get("severity") or "") for item in findings)
    action_counts = Counter(str(batch["suggested_action"]) for batch in batches)
    tier_counts = Counter(str(batch["review_risk_tier"]) for batch in batches)
    selected_action_chunk_counts = Counter(str(row.get("suggested_action") or "") for row in selected_rows)
    selected_tier_chunk_counts = Counter(str(row.get("review_risk_tier") or "") for row in selected_rows)
    return {
        "report_type": "reapproval_review_batch_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "worklist_report": {
            "path": str(worklist_path),
            "artifact_path": worklist_evidence_path,
            "sha256": worklist_sha256,
            "generated_at": str(worklist.get("generated_at") or ""),
            "tenant_id": str(worklist.get("tenant_id") or ""),
            "effective_data_dir": str(worklist.get("effective_data_dir") or ""),
            "reapproval_candidate_chunks": _int(worklist.get("reapproval_candidate_chunks")),
        },
        "worklist_chunks": {
            "path": str(chunks_path),
            "artifact_path": chunks_evidence_path,
            "sha256": chunks_sha256,
            "report_type": str(chunk_payload.get("report_type") or "reapproval_worklist_chunk_candidates"),
            "candidate_count": len(chunk_rows),
        },
        "max_chunks_per_batch": max_chunks_per_batch,
        "include_review_tiers": list(include_tiers),
        "include_suggested_actions": list(include_actions),
        "candidate_count": len(chunk_rows),
        "selected_candidate_count": len(selected_rows),
        "batch_count": len(batches),
        "reapproval_chunk_count": sum(int(batch["chunk_count"]) for batch in batches),
        "action_batch_counts": dict(sorted(action_counts.items())),
        "risk_tier_batch_counts": dict(sorted(tier_counts.items())),
        "action_chunk_counts": dict(sorted(selected_action_chunk_counts.items())),
        "risk_tier_chunk_counts": dict(sorted(selected_tier_chunk_counts.items())),
        "blocker_count": int(severity_counts.get("blocker", 0)),
        "warning_count": int(severity_counts.get("warning", 0)),
        "passed": int(severity_counts.get("blocker", 0)) == 0,
        "findings": findings,
        "batches": batches,
        "decision_template_fields": list(DECISION_TEMPLATE_FIELDS),
        "decision_template_operator_decisions": list(REAPPROVAL_DECISION_OPERATOR_DECISIONS),
        "decision_template_required_fields": list(REAPPROVAL_DECISION_REQUIRED_FIELDS),
        "decision_template_approval_scope_confirmations": list(REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS),
        "decision_template_override_decisions": list(REAPPROVAL_OVERRIDE_DECISIONS),
        "decision_template_guidance": DECISION_ENTRY_GUIDANCE,
        "decision_template_note": (
            "Decision template rows are batch-level operator inputs. Use chunk_decision_overrides_json "
            "for partial approvals or rejected chunk exceptions; do not treat blank rows as approval."
        ),
        "safety_note": (
            "This manifest is read-only. It does not reprocess files, approve chunks, or write Vector DB records. "
            "Operators must complete the listed reapproval action and reindex approved chunks separately."
        ),
    }


def _load_worklist_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "reapproval_worklist":
        raise ValueError("worklist_report must be a reapproval_worklist JSON report.")
    return payload


def _load_chunk_candidates(path: Path, *, is_csv: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if is_csv:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [_normalize_row(dict(row)) for row in csv.DictReader(handle)]
        return {"report_type": "reapproval_worklist_chunk_candidates_csv", "candidate_count": len(rows)}, rows
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "reapproval_worklist_chunk_candidates":
        raise ValueError("worklist_chunks_report must be a reapproval_worklist_chunk_candidates JSON report.")
    rows = [_normalize_row(row) for row in payload.get("candidates") or [] if isinstance(row, dict)]
    return payload, rows


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    for key in ("approval_provenance_missing_fields", "reapproval_reasons"):
        normalized[key] = _list_value(normalized.get(key))
    return normalized


def _append_consistency_findings(
    findings: list[dict[str, Any]],
    *,
    worklist: dict[str, Any],
    chunk_payload: dict[str, Any],
    chunk_rows: list[dict[str, Any]],
) -> None:
    expected = _int(worklist.get("reapproval_candidate_chunks"))
    payload_count = _int(chunk_payload.get("candidate_count"))
    if payload_count and payload_count != len(chunk_rows):
        findings.append(
            _finding(
                "blocker",
                "chunk-candidate-count-mismatch",
                "Chunk candidate report count does not match the number of candidate rows.",
                candidate_count=payload_count,
                row_count=len(chunk_rows),
            )
        )
    if expected and expected != len(chunk_rows):
        findings.append(
            _finding(
                "blocker",
                "worklist-chunk-candidate-count-mismatch",
                "Reapproval worklist candidate count does not match chunk candidate rows.",
                worklist_candidate_count=expected,
                row_count=len(chunk_rows),
            )
        )
    for field in ("tenant_id", "effective_data_dir"):
        worklist_value = str(worklist.get(field) or "").strip()
        chunk_value = str(chunk_payload.get(field) or "").strip()
        if not worklist_value:
            findings.append(
                _finding(
                    "blocker",
                    f"worklist-{field.replace('_', '-')}-missing",
                    "Reapproval worklist runtime identity metadata is required for the v2 review chain.",
                    field=field,
                )
            )
        elif chunk_value and worklist_value != chunk_value:
            findings.append(
                _finding(
                    "blocker",
                    f"worklist-chunks-{field.replace('_', '-')}-mismatch",
                    "Reapproval worklist and chunk candidate export metadata do not match.",
                    field=field,
                    worklist_value=worklist_value,
                    chunk_candidate_value=chunk_value,
                )
            )
    invalid_review_hash_ids = sorted(
        str(row.get("chunk_id") or "")
        for row in chunk_rows
        if not _is_sha256(row.get("review_content_hash"))
    )
    if invalid_review_hash_ids:
        findings.append(
            _finding(
                "blocker",
                "chunk-review-content-hash-missing-or-invalid",
                "Every reapproval candidate must include a full review_content_hash.",
                chunk_ids=invalid_review_hash_ids[:50],
                invalid_count=len(invalid_review_hash_ids),
            )
        )


def _batch_chunk_fingerprint(rows: Sequence[dict[str, Any]], *, suggested_action: str, risk_tier: str) -> str:
    payload = [
        {
            "document_id": str(row.get("document_id") or ""),
            "chunk_id": str(row.get("chunk_id") or ""),
            "suggested_action": suggested_action,
            "review_risk_tier": risk_tier,
            "approved_content_hash_short": str(row.get("approved_content_hash_short") or ""),
            "review_content_hash": str(row.get("review_content_hash") or ""),
            "vector_content_hash_short": str(row.get("vector_content_hash_short") or ""),
            "approval_provenance_missing_fields": _list_value(row.get("approval_provenance_missing_fields")),
            "reapproval_reasons": _list_value(row.get("reapproval_reasons")),
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _batch_id(
    *,
    worklist_sha256: str,
    group_index: int,
    local_index: int,
    suggested_action: str,
    risk_tier: str,
    fingerprint: str,
) -> str:
    action_slug = re.sub(r"[^a-z0-9]+", "-", suggested_action.lower()).strip("-") or "action"
    return (
        f"reapproval-{worklist_sha256[:12]}-{group_index:04d}-{local_index:03d}-"
        f"{action_slug}-{risk_tier}-{fingerprint[:12]}"
    )


def _safe_relative_artifact_path(value: str | Path | None) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        return ""
    return candidate.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    payload = {"severity": severity, "code": code, "detail": detail}
    payload.update(extra)
    return payload


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, (tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        if ";" in value:
            return [part.strip() for part in value.split(";") if part.strip()]
        if value.strip():
            return [value.strip()]
    return []


def _format_counter(counter: Counter[str], *, limit: int | None = None) -> str:
    items = counter.most_common(limit) if limit else sorted(counter.items())
    return "; ".join(f"{key}={value}" for key, value in items)


def _tier_order(value: Any) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(value or "").strip().lower(), 99)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_cell(row.get(field)) for field in CSV_FIELDS})


def write_decision_template_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_TEMPLATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            template_row = {field: _csv_cell(row.get(field)) for field in CSV_FIELDS}
            template_row.update(
                {
                    "operator_decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "decision_notes": "",
                    "chunk_decision_overrides_json": "[]",
                    "approval_scope_confirmation": "",
                    "allowed_operator_decisions": "; ".join(REAPPROVAL_DECISION_OPERATOR_DECISIONS),
                    "required_operator_fields": "; ".join(REAPPROVAL_DECISION_REQUIRED_FIELDS),
                    "approval_scope_confirmation_options": "; ".join(REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS),
                    "override_decision_options": "; ".join(REAPPROVAL_OVERRIDE_DECISIONS),
                    "decision_entry_guidance": DECISION_ENTRY_GUIDANCE,
                }
            )
            writer.writerow(template_row)


def _csv_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "; ".join(f"{key}={value[key]}" for key in sorted(value))
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    return "" if value is None else value


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Reapproval Review Batch Manifest",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Blockers / warnings: {report.get('blocker_count')} / {report.get('warning_count')}",
        f"- Candidate chunks: `{report.get('candidate_count')}`",
        f"- Selected candidate chunks: `{report.get('selected_candidate_count')}`",
        f"- Batches: `{report.get('batch_count')}`",
        f"- Reapproval chunks in batches: `{report.get('reapproval_chunk_count')}`",
        f"- Action chunk counts: `{report.get('action_chunk_counts') or {}}`",
        f"- Risk tier chunk counts: `{report.get('risk_tier_chunk_counts') or {}}`",
        "",
        f"Safety: {report.get('safety_note')}",
        "",
        "## Batches",
        "",
        "| Rank | Action | Risk | Document | Chunks | Fingerprint | Reasons |",
        "| ---: | --- | --- | --- | ---: | --- | --- |",
    ]
    for batch in report.get("batches") or []:
        document = batch.get("document_name") or batch.get("filename") or batch.get("document_id")
        lines.append(
            "| {rank} | {action} | {risk} | {document} | {chunks} | {fingerprint} | {reasons} |".format(
                rank=batch.get("batch_rank"),
                action=_md_cell(batch.get("suggested_action")),
                risk=_md_cell(batch.get("review_risk_tier")),
                document=_md_cell(document),
                chunks=batch.get("chunk_count"),
                fingerprint=_md_cell(str(batch.get("reapproval_batch_chunk_fingerprint") or "")[:12]),
                reasons=_md_cell(batch.get("top_reapproval_reasons")),
            )
        )
    findings = report.get("findings") or []
    lines.extend(["", "## Findings", ""])
    if findings:
        for finding in findings:
            lines.append(f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}")
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Decision Template",
            "",
            "- Optional CSV output adds blank operator decision columns to each batch row.",
            f"- Allowed operator decisions: `{', '.join(REAPPROVAL_DECISION_OPERATOR_DECISIONS)}`",
            f"- Required operator fields: `{', '.join(REAPPROVAL_DECISION_REQUIRED_FIELDS)}`",
            f"- Allowed scope confirmations: `{', '.join(REAPPROVAL_APPROVAL_SCOPE_CONFIRMATIONS)}`",
            f"- Allowed override decisions: `{', '.join(REAPPROVAL_OVERRIDE_DECISIONS)}`",
            "- Partial approvals must use `chunk_decision_overrides_json` and still require operator approval evidence.",
            f"- Guidance: {DECISION_ENTRY_GUIDANCE}",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build read-only reapproval review batches from chunk candidates.")
    parser.add_argument("--worklist-report", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--worklist-chunks-report", type=Path)
    source.add_argument("--worklist-chunks-csv", type=Path)
    parser.add_argument("--worklist-report-artifact-path")
    parser.add_argument("--worklist-chunks-artifact-path")
    parser.add_argument("--max-chunks-per-batch", type=int, default=DEFAULT_MAX_CHUNKS_PER_BATCH)
    parser.add_argument("--include-review-tier", action="append", default=[])
    parser.add_argument("--include-suggested-action", action="append", default=[])
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument("--out-decision-template-csv", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_reapproval_review_batches(
        worklist_report=args.worklist_report,
        worklist_chunks_report=args.worklist_chunks_report,
        worklist_chunks_csv=args.worklist_chunks_csv,
        worklist_report_artifact_path=args.worklist_report_artifact_path,
        worklist_chunks_artifact_path=args.worklist_chunks_artifact_path,
        max_chunks_per_batch=args.max_chunks_per_batch,
        include_review_tiers=args.include_review_tier or SUPPORTED_REVIEW_TIERS,
        include_suggested_actions=args.include_suggested_action,
    )
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_csv:
        write_csv(args.out_csv, report["batches"])
    if args.out_decision_template_csv:
        write_decision_template_csv(args.out_decision_template_csv, report["batches"])
    if args.out_md:
        write_markdown(report, args.out_md)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
