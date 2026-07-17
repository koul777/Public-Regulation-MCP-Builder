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

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.storage.repository import JsonRepository
from scripts.build_approval_worklist import (
    APPROVAL_WORKLIST_STATUSES,
    approval_chunk_row,
    clean_text,
    review_candidate_fingerprint,
)
from scripts.report_metadata import current_repo_commit


DEFAULT_MAX_CHUNKS_PER_BATCH = 100
REVIEW_TYPES = ("manual_attention", "low_risk_batch")
REVIEW_STRATEGY_BY_TYPE = {
    "manual_attention": "operator_manual_review",
    "low_risk_batch": "human_bulk_review",
}
CSV_FIELDS = [
    "batch_rank",
    "review_batch_id",
    "review_type",
    "review_strategy",
    "document_id",
    "document_name",
    "filename",
    "chunk_count",
    "chunk_ids",
    "review_batch_chunk_fingerprint",
    "review_priority_tier_counts",
    "top_attention_reasons",
    "review_flags_acknowledged_required",
    "worklist_report_path",
    "worklist_report_sha256",
    "review_batch_manifest_path",
    "review_batch_manifest_sha256",
]


def build_approval_review_batches(
    *,
    data_dir: str | Path,
    worklist_report: str | Path,
    worklist_report_artifact_path: str | None = None,
    tenant_id: str = "default",
    tenant_storage_isolation: bool = False,
    max_chunks_per_batch: int = DEFAULT_MAX_CHUNKS_PER_BATCH,
    include_review_types: Sequence[str] = REVIEW_TYPES,
    default_security_level: str = "internal",
) -> dict[str, Any]:
    if max_chunks_per_batch <= 0:
        raise ValueError("max_chunks_per_batch must be greater than zero.")
    include_types = tuple(dict.fromkeys(str(item) for item in include_review_types))
    unsupported = sorted(set(include_types) - set(REVIEW_TYPES))
    if unsupported:
        raise ValueError(f"Unsupported review types: {', '.join(unsupported)}")

    settings = Settings(data_dir=Path(data_dir), tenant_storage_isolation=tenant_storage_isolation)
    effective_settings = settings_for_tenant(settings, tenant_id)
    repository = JsonRepository(effective_settings)
    worklist_path = Path(worklist_report)
    worklist = _load_worklist_report(worklist_path)
    worklist_sha256 = _sha256_file(worklist_path)
    evidence_path = _safe_relative_artifact_path(
        worklist_report_artifact_path if worklist_report_artifact_path else worklist_path
    )

    batches: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    selected_counts: Counter[str] = Counter()
    source_documents = worklist.get("documents") if isinstance(worklist.get("documents"), list) else []
    _append_worklist_scope_findings(
        findings,
        worklist=worklist,
        effective_data_dir=effective_settings.data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    if not evidence_path:
        findings.append(
            _finding(
                "blocker",
                "worklist-report-path-not-relative",
                "Approval worklist evidence path must be a safe relative artifact path.",
                path=str(worklist_path),
            )
        )

    for document_index, document_row in enumerate(source_documents, start=1):
        document_id = clean_text(document_row.get("document_id"))
        document = repository.get_document(document_id) if document_id else None
        if document is None:
            findings.append(
                _finding(
                    "blocker",
                    "worklist-document-missing",
                    "A document from the approval worklist is missing from the runtime repository.",
                    document_id=document_id,
                )
            )
            continue
        chunks = repository.get_chunks(document_id)
        grouped_chunks: dict[str, list[dict[str, Any]]] = {review_type: [] for review_type in REVIEW_TYPES}
        for chunk in chunks:
            row = approval_chunk_row(chunk)
            if row["approval_status"] not in APPROVAL_WORKLIST_STATUSES:
                continue
            review_type = _review_type_for_row(row)
            if not review_type or review_type not in include_types:
                continue
            chunk_item = _chunk_item(chunk, row)
            grouped_chunks[review_type].append(chunk_item)
            selected_counts[review_type] += 1
        _append_document_fingerprint_findings(
            findings,
            document_row=document_row,
            grouped_chunks=grouped_chunks,
            include_review_types=include_types,
        )
        for review_type in REVIEW_TYPES:
            chunk_items = grouped_chunks[review_type]
            if not chunk_items:
                continue
            _append_document_batches(
                batches,
                document_row=document_row,
                document_index=document_index,
                review_type=review_type,
                chunk_items=chunk_items,
                max_chunks_per_batch=max_chunks_per_batch,
                worklist_report_path=evidence_path,
                worklist_report_sha256=worklist_sha256,
                default_security_level=default_security_level,
            )

    _append_consistency_findings(
        findings,
        worklist=worklist,
        selected_counts=selected_counts,
        include_review_types=include_types,
    )
    for rank, batch in enumerate(batches, start=1):
        batch["batch_rank"] = rank

    severity_counts = Counter(str(item.get("severity") or "") for item in findings)
    chunk_count = sum(int(batch["chunk_count"]) for batch in batches)
    review_type_counts = Counter(str(batch["review_type"]) for batch in batches)
    report = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(Path(data_dir)),
        "effective_data_dir": str(effective_settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": evidence_path,
            "sha256": worklist_sha256,
            "effective_data_dir": clean_text(worklist.get("effective_data_dir")),
            "tenant_id": clean_text(worklist.get("tenant_id")),
            "tenant_storage_isolation": bool(worklist.get("tenant_storage_isolation")),
            "generated_at": clean_text(worklist.get("generated_at")),
            "document_count": int(worklist.get("document_count") or 0),
            "total_chunks": int(worklist.get("total_chunks") or 0),
            "manual_attention_chunks": int(worklist.get("manual_attention_chunks") or 0),
            "low_risk_batch_review_candidate_chunks": int(
                worklist.get("low_risk_batch_review_candidate_chunks") or 0
            ),
        },
        "max_chunks_per_batch": max_chunks_per_batch,
        "include_review_types": list(include_types),
        "default_security_level": default_security_level,
        "batch_count": len(batches),
        "approval_chunk_count": chunk_count,
        "manual_attention_chunks": int(selected_counts.get("manual_attention", 0)),
        "low_risk_batch_review_candidate_chunks": int(selected_counts.get("low_risk_batch", 0)),
        "review_type_batch_counts": dict(sorted(review_type_counts.items())),
        "blocker_count": int(severity_counts.get("blocker", 0)),
        "warning_count": int(severity_counts.get("warning", 0)),
        "passed": int(severity_counts.get("blocker", 0)) == 0,
        "findings": findings,
        "batches": batches,
        "safety_note": (
            "This manifest does not approve chunks or write Vector DB records. "
            "Operators must inspect each batch, set the final security level, acknowledge parser/table flags when required, "
            "then call the approval API or use the local operator UI."
        ),
    }
    return report


def _load_worklist_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("report_type") != "approval_worklist":
        raise ValueError("worklist_report must be an approval_worklist JSON report.")
    return payload


def _append_document_batches(
    batches: list[dict[str, Any]],
    *,
    document_row: dict[str, Any],
    document_index: int,
    review_type: str,
    chunk_items: list[dict[str, Any]],
    max_chunks_per_batch: int,
    worklist_report_path: str,
    worklist_report_sha256: str,
    default_security_level: str,
) -> None:
    for local_index, start in enumerate(range(0, len(chunk_items), max_chunks_per_batch), start=1):
        batch_chunks = chunk_items[start : start + max_chunks_per_batch]
        tier_counts = Counter(str(item["review_priority_tier"]) for item in batch_chunks)
        attention_counts = Counter(reason for item in batch_chunks for reason in item["attention_reasons"])
        review_strategy = REVIEW_STRATEGY_BY_TYPE[review_type]
        chunk_ids = [str(item["chunk_id"]) for item in batch_chunks]
        requires_ack = review_type == "manual_attention" or bool(attention_counts)
        batch_fingerprint = _review_batch_chunk_fingerprint(batch_chunks, review_type)
        batch_id = _review_batch_id(
            worklist_report_sha256=worklist_report_sha256,
            document_index=document_index,
            review_type=review_type,
            local_index=local_index,
            batch_chunk_fingerprint=batch_fingerprint,
        )
        batches.append(
            {
                "batch_rank": 0,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": batch_fingerprint,
                "review_type": review_type,
                "review_strategy": review_strategy,
                "document_id": clean_text(document_row.get("document_id")),
                "document_name": clean_text(document_row.get("document_name")),
                "filename": clean_text(document_row.get("filename")),
                "institution_name": clean_text(document_row.get("institution_name")),
                "apba_id": clean_text(document_row.get("apba_id")),
                "source_system": clean_text(document_row.get("source_system")),
                "source_record_id": clean_text(document_row.get("source_record_id")),
                "source_file_id": clean_text(document_row.get("source_file_id")),
                "chunk_count": len(batch_chunks),
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
                "review_priority_tier_counts": dict(sorted(tier_counts.items())),
                "top_attention_reasons": dict(attention_counts.most_common(10)),
                "review_flags_acknowledged_required": requires_ack,
                "approval_request_template": {
                    "chunk_ids": chunk_ids,
                    "security_level": default_security_level,
                    "review_flags_acknowledged": False if requires_ack else False,
                    "worklist_report_path": worklist_report_path,
                    "worklist_report_sha256": worklist_report_sha256,
                    "review_batch_id": batch_id,
                    "review_batch_chunk_fingerprint": batch_fingerprint,
                    "review_strategy": review_strategy,
                },
                "operator_instruction": (
                    "Review every chunk in this batch before approval. "
                    "If review_flags_acknowledged_required is true, set review_flags_acknowledged=true only after inspection."
                ),
            }
        )


def _append_consistency_findings(
    findings: list[dict[str, Any]],
    *,
    worklist: dict[str, Any],
    selected_counts: Counter[str],
    include_review_types: Sequence[str],
) -> None:
    expectations = {
        "manual_attention": int(worklist.get("manual_attention_chunks") or 0),
        "low_risk_batch": int(worklist.get("low_risk_batch_review_candidate_chunks") or 0),
    }
    for review_type, expected in expectations.items():
        if review_type not in include_review_types:
            continue
        actual = int(selected_counts.get(review_type, 0))
        if actual != expected:
            findings.append(
                _finding(
                    "blocker",
                    "worklist-runtime-count-mismatch",
                    "Runtime chunk selection does not match the approval worklist summary.",
                    review_type=review_type,
                    expected_chunks=expected,
                    actual_chunks=actual,
                )
            )


def _append_worklist_scope_findings(
    findings: list[dict[str, Any]],
    *,
    worklist: dict[str, Any],
    effective_data_dir: Path,
    tenant_id: str,
    tenant_storage_isolation: bool,
) -> None:
    worklist_tenant = clean_text(worklist.get("tenant_id"))
    if worklist_tenant and worklist_tenant != tenant_id:
        findings.append(
            _finding(
                "blocker",
                "worklist-tenant-mismatch",
                "Approval worklist tenant_id does not match the requested runtime tenant.",
                worklist_tenant_id=worklist_tenant,
                tenant_id=tenant_id,
            )
        )
    if bool(worklist.get("tenant_storage_isolation")) != bool(tenant_storage_isolation):
        findings.append(
            _finding(
                "blocker",
                "worklist-tenant-isolation-mismatch",
                "Approval worklist tenant storage isolation does not match the requested runtime.",
                worklist_tenant_storage_isolation=bool(worklist.get("tenant_storage_isolation")),
                tenant_storage_isolation=bool(tenant_storage_isolation),
            )
        )
    worklist_effective_dir = clean_text(worklist.get("effective_data_dir"))
    if worklist_effective_dir:
        try:
            same_dir = Path(worklist_effective_dir).resolve() == Path(effective_data_dir).resolve()
        except OSError:
            same_dir = False
        if not same_dir:
            findings.append(
                _finding(
                    "blocker",
                    "worklist-effective-data-dir-mismatch",
                    "Approval worklist effective_data_dir does not match the requested runtime repository.",
                    worklist_effective_data_dir=worklist_effective_dir,
                    effective_data_dir=str(effective_data_dir),
                )
            )


def _append_document_fingerprint_findings(
    findings: list[dict[str, Any]],
    *,
    document_row: dict[str, Any],
    grouped_chunks: dict[str, list[dict[str, Any]]],
    include_review_types: Sequence[str],
) -> None:
    mapping = {
        "manual_attention": "manual_attention_fingerprint",
        "low_risk_batch": "low_risk_batch_review_candidate_fingerprint",
    }
    for review_type, field_name in mapping.items():
        if review_type not in include_review_types:
            continue
        expected = clean_text(document_row.get(field_name))
        if not expected:
            continue
        actual = review_candidate_fingerprint(_fingerprint_rows(grouped_chunks.get(review_type) or [], review_type))
        if expected != actual:
            findings.append(
                _finding(
                    "blocker",
                    "worklist-document-fingerprint-mismatch",
                    "Runtime chunk IDs, review tiers, or review content hashes do not match the approval worklist document fingerprint.",
                    document_id=clean_text(document_row.get("document_id")),
                    review_type=review_type,
                    expected_fingerprint=expected,
                    actual_fingerprint=actual,
                )
            )


def _fingerprint_rows(chunk_items: Sequence[dict[str, Any]], review_type: str) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": clean_text(item.get("chunk_id")),
            "review_content_hash": clean_text(item.get("review_content_hash")),
            "approval_status": clean_text(item.get("approval_status")),
            "review_priority_tier": clean_text(item.get("review_priority_tier")),
            "manual_attention": review_type == "manual_attention",
            "low_risk_batch_candidate": review_type == "low_risk_batch",
        }
        for item in chunk_items
    ]


def _review_type_for_row(row: dict[str, Any]) -> str:
    if row.get("manual_attention"):
        return "manual_attention"
    if row.get("low_risk_batch_candidate"):
        return "low_risk_batch"
    return ""


def _chunk_item(chunk: Any, row: dict[str, Any]) -> dict[str, Any]:
    metadata = getattr(chunk, "metadata", {}) or {}
    return {
        "chunk_id": clean_text(getattr(chunk, "chunk_id", "")),
        "review_content_hash": clean_text(row.get("review_content_hash")),
        "approval_status": clean_text(row.get("approval_status")),
        "review_priority_tier": clean_text(row.get("review_priority_tier")),
        "review_category": clean_text(row.get("review_category")),
        "attention_reasons": [str(item) for item in row.get("attention_reasons", [])],
        "chunk_type": clean_text(getattr(chunk, "chunk_type", "")),
        "article_no": clean_text(metadata.get("article_no")),
        "article_title": clean_text(metadata.get("article_title")),
    }


def _review_batch_id(
    *,
    worklist_report_sha256: str,
    document_index: int,
    review_type: str,
    local_index: int,
    batch_chunk_fingerprint: str,
) -> str:
    return (
        f"approval-{worklist_report_sha256[:12]}-{document_index:03d}-"
        f"{review_type.replace('_', '-')}-{local_index:03d}-{batch_chunk_fingerprint[:12]}"
    )


def _review_batch_chunk_fingerprint(chunk_items: Sequence[dict[str, Any]], review_type: str) -> str:
    payload = [
        {
            "chunk_id": clean_text(item.get("chunk_id")),
            "review_content_hash": clean_text(item.get("review_content_hash")),
            "approval_status": clean_text(item.get("approval_status")),
            "review_type": review_type,
            "review_priority_tier": clean_text(item.get("review_priority_tier")),
            "review_category": clean_text(item.get("review_category")),
            "attention_reasons": sorted(str(reason) for reason in item.get("attention_reasons") or []),
        }
        for item in chunk_items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_relative_artifact_path(value: str | Path) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            text = candidate.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except ValueError:
            return ""
    if text.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text) or ".." in text.split("/"):
        return ""
    return text


def _apply_review_batch_manifest_evidence(report: dict[str, Any], manifest_artifact_path: str) -> None:
    if not manifest_artifact_path:
        return
    report["approval_request_path"] = manifest_artifact_path
    report.setdefault("approval_request_sha256", "")
    for batch in report.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        template = batch.get("approval_request_template")
        if isinstance(template, dict):
            template["review_batch_manifest_path"] = manifest_artifact_path
            template.setdefault("review_batch_manifest_sha256", "")


def _finding(severity: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "detail": detail, **extra}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path: Path, batches: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for batch in batches:
            row = dict(batch)
            row["chunk_ids"] = ";".join(str(item) for item in batch.get("chunk_ids") or [])
            row["review_batch_chunk_fingerprint"] = str(batch.get("review_batch_chunk_fingerprint") or "")
            row["review_priority_tier_counts"] = _format_mapping(batch.get("review_priority_tier_counts") or {})
            row["top_attention_reasons"] = _format_mapping(batch.get("top_attention_reasons") or {})
            template = batch.get("approval_request_template") or {}
            row["worklist_report_path"] = template.get("worklist_report_path", "")
            row["worklist_report_sha256"] = template.get("worklist_report_sha256", "")
            row["review_batch_manifest_path"] = template.get("review_batch_manifest_path", "")
            row["review_batch_manifest_sha256"] = template.get("review_batch_manifest_sha256", "")
            writer.writerow(row)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    worklist = report.get("worklist_report") or {}
    lines = [
        "# Approval Review Batch Manifest",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Data dir: `{report.get('data_dir')}`",
        f"- Effective data dir: `{report.get('effective_data_dir')}`",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Worklist report path: `{worklist.get('approval_request_path') or worklist.get('path')}`",
        f"- Worklist report sha256: `{worklist.get('sha256')}`",
        f"- Review batch manifest path: `{report.get('approval_request_path') or ''}`",
        f"- Review batch manifest sha256: `{report.get('approval_request_sha256') or ''}`",
        f"- Batches: `{report.get('batch_count')}`",
        f"- Approval chunks: `{report.get('approval_chunk_count')}`",
        f"- Manual-attention chunks: `{report.get('manual_attention_chunks')}`",
        f"- Low-risk batch-review candidate chunks: `{report.get('low_risk_batch_review_candidate_chunks')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        "",
        f"Safety: {report.get('safety_note')}",
        "",
    ]
    findings = report.get("findings") or []
    if findings:
        lines.extend(["## Findings", ""])
        for finding in findings:
            lines.append(
                f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}"
            )
        lines.append("")
    lines.extend(
        [
            "## Batches",
            "",
            "| Rank | Batch ID | Batch fingerprint | Type | Strategy | Document | Chunks | Ack required | Tiers | Top reasons |",
            "| ---: | --- | --- | --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for batch in (report.get("batches") or [])[:100]:
        lines.append(
            "| {rank} | {batch_id} | {fingerprint} | {review_type} | {strategy} | {document} | {chunks} | {ack} | {tiers} | {reasons} |".format(
                rank=batch.get("batch_rank"),
                batch_id=md_cell(batch.get("review_batch_id")),
                fingerprint=md_cell(str(batch.get("review_batch_chunk_fingerprint") or "")[:12]),
                review_type=md_cell(batch.get("review_type")),
                strategy=md_cell(batch.get("review_strategy")),
                document=md_cell(batch.get("document_name") or batch.get("filename") or batch.get("document_id")),
                chunks=batch.get("chunk_count"),
                ack=str(batch.get("review_flags_acknowledged_required")).lower(),
                tiers=md_cell(_format_mapping(batch.get("review_priority_tier_counts") or {})),
                reasons=md_cell(_format_mapping(batch.get("top_attention_reasons") or {})),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_mapping(mapping: dict[str, Any]) -> str:
    return "; ".join(f"{key}={value}" for key, value in mapping.items())


def md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build human approval review batch manifests from an approval worklist and runtime repository."
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--worklist-report", type=Path, required=True)
    parser.add_argument(
        "--worklist-report-artifact-path",
        help="Safe relative path to store in approval requests when --worklist-report is an absolute path.",
    )
    parser.add_argument("--max-chunks-per-batch", type=int, default=DEFAULT_MAX_CHUNKS_PER_BATCH)
    parser.add_argument(
        "--include-review-type",
        action="append",
        choices=REVIEW_TYPES,
        help="Review type to include. Repeat to override the default of both manual_attention and low_risk_batch.",
    )
    parser.add_argument("--default-security-level", default="internal")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_approval_review_batches(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        worklist_report=args.worklist_report,
        worklist_report_artifact_path=args.worklist_report_artifact_path,
        max_chunks_per_batch=args.max_chunks_per_batch,
        include_review_types=args.include_review_type or REVIEW_TYPES,
        default_security_level=args.default_security_level,
    )
    if args.out_json:
        _apply_review_batch_manifest_evidence(report, _safe_relative_artifact_path(args.out_json))
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_csv:
        write_csv(args.out_csv, report["batches"])
    if args.out_md:
        write_markdown(report, args.out_md)
    console_payload = (
        _console_summary(
            report,
            out_json=args.out_json,
            out_csv=args.out_csv,
            out_md=args.out_md,
        )
        if args.out_json or args.out_csv or args.out_md
        else report
    )
    _print_json(console_payload, stdout=stdout)
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def _print_json(report: dict[str, Any], *, stdout: TextIO | None = None) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if stdout is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(payload, file=stdout or sys.stdout)


def _console_summary(
    report: dict[str, Any],
    *,
    out_json: Path | None = None,
    out_csv: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    return {
        "report_type": report.get("report_type"),
        "passed": report.get("passed"),
        "blocker_count": report.get("blocker_count"),
        "warning_count": report.get("warning_count"),
        "batch_count": report.get("batch_count"),
        "approval_chunk_count": report.get("approval_chunk_count"),
        "manual_attention_chunks": report.get("manual_attention_chunks"),
        "low_risk_batch_review_candidate_chunks": report.get("low_risk_batch_review_candidate_chunks"),
        "worklist_report_sha256": (report.get("worklist_report") or {}).get("sha256"),
        "review_batch_manifest_path": report.get("approval_request_path") or "",
        "review_batch_manifest_sha256": report.get("approval_request_sha256") or "",
        "out_json": str(out_json) if out_json else "",
        "out_csv": str(out_csv) if out_csv else "",
        "out_md": str(out_md) if out_md else "",
    }


if __name__ == "__main__":
    raise SystemExit(main())
