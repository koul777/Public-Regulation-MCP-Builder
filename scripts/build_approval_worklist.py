from __future__ import annotations

import argparse
import csv
import hashlib
import json
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
from scripts.analyze_regulation_corpus import (
    chunk_review_flags,
    review_priority_tier,
    review_severity_for_flags,
)
from scripts.report_metadata import current_repo_commit


ATTENTION_BOOL_METADATA_KEYS = (
    "review_required",
    "table_review_required",
    "manual_review_required",
    "requires_manual_review",
)
ATTENTION_LIST_METADATA_KEYS = (
    "review_flags",
    "table_review_flags",
    "row_quality_flags",
    "quality_flags",
    "source_hwpx_parser_review_flags",
    "parser_uncertainty_flags",
)
SUPPLEMENTARY_EFFECTIVE_DATE_KEYWORDS = ("부칙", "시행일", "적용일", "시행한다", "부터 시행", "적용한다")
SUPPLEMENTARY_EFFECTIVE_DATE_COMPACT_KEYWORDS = (
    "부칙",
    "공포한날부터시행",
    "날부터시행",
    "부터시행",
    "시행일",
    "적용일",
)

REVIEW_PRIORITY_TIERS = (
    "blocking_review",
    "domain_attention",
    "stable_false_positive",
    "informational",
    "no_signal",
)
MANUAL_ATTENTION_TIERS = {"blocking_review", "domain_attention"}
LOW_RISK_BATCH_REVIEW_TIERS = {"stable_false_positive", "informational", "no_signal"}
APPROVAL_WORKLIST_STATUSES = {"draft", "needs_review"}
REVIEW_CONTENT_HASH_VERSION = "approval-review-content-v1"

CSV_FIELDS = [
    "rank",
    "suggested_action",
    "document_id",
    "document_name",
    "filename",
    "institution_name",
    "apba_id",
    "profile_id",
    "source_system",
    "source_record_id",
    "source_file_id",
    "total_chunks",
    "approved_chunks",
    "draft_chunks",
    "needs_review_chunks",
    "bulk_review_candidate_chunks",
    "manual_attention_chunks",
    "blocking_review_chunks",
    "domain_attention_chunks",
    "stable_false_positive_chunks",
    "informational_chunks",
    "no_signal_chunks",
    "low_risk_batch_review_candidate_chunks",
    "blocked_or_rejected_chunks",
    "bulk_review_candidate_rate",
    "low_risk_batch_review_candidate_rate",
    "priority_tier_counts",
    "top_attention_reasons",
]


def build_approval_worklist(
    *,
    data_dir: str | Path,
    source_system: str | None = None,
    apba_id: str | None = None,
    tenant_id: str = "default",
    tenant_storage_isolation: bool = False,
) -> dict[str, Any]:
    settings = Settings(data_dir=Path(data_dir), tenant_storage_isolation=tenant_storage_isolation)
    effective_settings = settings_for_tenant(settings, tenant_id)
    repository = JsonRepository(effective_settings)
    filters = {
        "source_system": clean_text(source_system),
        "apba_id": clean_text(apba_id),
    }
    documents: list[dict[str, Any]] = []
    status_totals: Counter[str] = Counter()
    attention_reason_totals: Counter[str] = Counter()
    review_priority_totals: Counter[str] = Counter()

    for document in repository.list_documents():
        chunks = repository.get_chunks(document.document_id)
        if not document_matches_filters(document, chunks, filters):
            continue
        chunk_rows = [approval_chunk_row(chunk) for chunk in chunks]
        status_counts = Counter(clean_text(chunk.approval_status) or "missing" for chunk in chunks)
        status_totals.update(status_counts)
        review_candidate_rows = [
            row for row in chunk_rows if row["approval_status"] in APPROVAL_WORKLIST_STATUSES
        ]
        priority_counts = Counter(str(row["review_priority_tier"]) for row in review_candidate_rows)
        for tier in REVIEW_PRIORITY_TIERS:
            priority_counts.setdefault(tier, 0)
        review_priority_totals.update(priority_counts)
        attention_reasons = Counter(
            reason
            for row in review_candidate_rows
            for reason in row["attention_reasons"]
            if row["manual_attention"]
        )
        attention_reason_totals.update(attention_reasons)
        manual_attention_chunk_count = sum(1 for row in review_candidate_rows if row["manual_attention"])
        bulk_candidate_count = sum(1 for row in review_candidate_rows if row["low_risk_batch_candidate"])
        blocked_or_rejected_count = sum(
            status_counts.get(status, 0) for status in ("security_blocked", "rejected", "superseded")
        )
        total_chunks = len(chunks)
        suggested_action = suggested_document_action(
            total_chunks=total_chunks,
            bulk_candidate_count=bulk_candidate_count,
            manual_attention_chunk_count=manual_attention_chunk_count,
            blocked_or_rejected_count=blocked_or_rejected_count,
        )
        documents.append(
            {
                "suggested_action": suggested_action,
                "document_id": document.document_id,
                "document_name": document.document_name or "",
                "filename": document.filename,
                "institution_name": document.institution_name or "",
                "apba_id": metadata_value(document, chunks, "apba_id"),
                "profile_id": metadata_value(document, chunks, "profile_id"),
                "source_system": metadata_value(document, chunks, "source_system"),
                "source_record_id": document.source_record_id or "",
                "source_file_id": document.source_file_id or "",
                "total_chunks": total_chunks,
                "approved_chunks": status_counts.get("approved", 0),
                "draft_chunks": status_counts.get("draft", 0),
                "needs_review_chunks": status_counts.get("needs_review", 0),
                "bulk_review_candidate_chunks": bulk_candidate_count,
                "manual_attention_chunks": manual_attention_chunk_count,
                "blocking_review_chunks": priority_counts.get("blocking_review", 0),
                "domain_attention_chunks": priority_counts.get("domain_attention", 0),
                "stable_false_positive_chunks": priority_counts.get("stable_false_positive", 0),
                "informational_chunks": priority_counts.get("informational", 0),
                "no_signal_chunks": priority_counts.get("no_signal", 0),
                "low_risk_batch_review_candidate_chunks": bulk_candidate_count,
                "blocked_or_rejected_chunks": blocked_or_rejected_count,
                "bulk_review_candidate_rate": percentage(bulk_candidate_count, total_chunks),
                "low_risk_batch_review_candidate_rate": percentage(bulk_candidate_count, total_chunks),
                "priority_tier_counts": format_counter(priority_counts, limit=len(REVIEW_PRIORITY_TIERS)),
                "top_attention_reasons": format_counter(attention_reasons, limit=5),
                "review_candidate_fingerprint": review_candidate_fingerprint(review_candidate_rows),
                "manual_attention_fingerprint": review_candidate_fingerprint(
                    [row for row in review_candidate_rows if row["manual_attention"]]
                ),
                "low_risk_batch_review_candidate_fingerprint": review_candidate_fingerprint(
                    [row for row in review_candidate_rows if row["low_risk_batch_candidate"]]
                ),
            }
        )

    action_order = {"manual_review_first": 0, "bulk_review_candidate": 1, "already_approved_or_empty": 2}
    documents.sort(
        key=lambda row: (
            action_order.get(str(row["suggested_action"]), 99),
            -int(row["manual_attention_chunks"]),
            -int(row["bulk_review_candidate_chunks"]),
            str(row["apba_id"]),
            str(row["filename"]),
        )
    )
    for index, row in enumerate(documents, start=1):
        row["rank"] = index

    action_counts = Counter(str(row["suggested_action"]) for row in documents)
    total_chunks = sum(int(row["total_chunks"]) for row in documents)
    bulk_candidates = sum(int(row["bulk_review_candidate_chunks"]) for row in documents)
    manual_attention = sum(int(row["manual_attention_chunks"]) for row in documents)
    for tier in REVIEW_PRIORITY_TIERS:
        review_priority_totals.setdefault(tier, 0)
    return {
        "report_type": "approval_worklist",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(Path(data_dir)),
        "effective_data_dir": str(effective_settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "filters": {key: value for key, value in filters.items() if value},
        "document_count": len(documents),
        "total_chunks": total_chunks,
        "approval_status_totals": dict(sorted(status_totals.items())),
        "bulk_review_candidate_chunks": bulk_candidates,
        "manual_attention_chunks": manual_attention,
        "blocking_review_chunks": review_priority_totals.get("blocking_review", 0),
        "domain_attention_chunks": review_priority_totals.get("domain_attention", 0),
        "stable_false_positive_chunks": review_priority_totals.get("stable_false_positive", 0),
        "informational_chunks": review_priority_totals.get("informational", 0),
        "no_signal_chunks": review_priority_totals.get("no_signal", 0),
        "low_risk_batch_review_candidate_chunks": bulk_candidates,
        "bulk_review_candidate_rate": percentage(bulk_candidates, total_chunks),
        "low_risk_batch_review_candidate_rate": percentage(bulk_candidates, total_chunks),
        "manual_attention_rate": percentage(manual_attention, total_chunks),
        "action_counts": dict(sorted(action_counts.items())),
        "review_priority_tier_counts": {
            tier: int(review_priority_totals.get(tier, 0)) for tier in REVIEW_PRIORITY_TIERS
        },
        "attention_reason_totals": dict(attention_reason_totals.most_common(20)),
        "documents": documents,
        "safety_note": (
            "This worklist does not approve chunks or write VectorDB records. "
            "Bulk-review candidates still require a human approval action before indexing."
        ),
    }


def approval_chunk_row(chunk: Any) -> dict[str, Any]:
    status = clean_text(getattr(chunk, "approval_status", "")).lower() or "missing"
    signal = chunk_review_signal(chunk)
    metadata_reasons = metadata_attention_reasons(chunk)
    tier = str(signal["review_priority_tier"])
    manual_attention = status == "needs_review" or tier in MANUAL_ATTENTION_TIERS or bool(metadata_reasons)
    low_risk_batch_candidate = status == "draft" and not manual_attention and tier in LOW_RISK_BATCH_REVIEW_TIERS
    return {
        "chunk_id": clean_text(getattr(chunk, "chunk_id", "")),
        "review_content_hash": review_content_hash(chunk),
        "approval_status": status,
        "review_flags": signal["review_flags"],
        "review_priority_tier": tier,
        "review_category": signal["review_category"],
        "attention_reasons": chunk_attention_reasons(
            chunk,
            signal=signal,
            metadata_reasons=metadata_reasons,
        ),
        "manual_attention": manual_attention,
        "low_risk_batch_candidate": low_risk_batch_candidate,
    }


def review_candidate_fingerprint(rows: Sequence[dict[str, Any]]) -> str:
    payload = [
        {
            "chunk_id": clean_text(row.get("chunk_id")),
            "review_content_hash": clean_text(row.get("review_content_hash")),
            "approval_status": clean_text(row.get("approval_status")),
            "review_priority_tier": clean_text(row.get("review_priority_tier")),
            "manual_attention": bool(row.get("manual_attention")),
            "low_risk_batch_candidate": bool(row.get("low_risk_batch_candidate")),
        }
        for row in sorted(rows, key=lambda item: clean_text(item.get("chunk_id")))
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_content_hash(chunk: Any) -> str:
    row = chunk_to_review_dict(chunk)
    text_basis, text = _review_text_basis(row)
    payload = {
        "schema_version": REVIEW_CONTENT_HASH_VERSION,
        "chunk_type": clean_text(row.get("chunk_type")),
        "source_page_start": row.get("source_page_start"),
        "source_page_end": row.get("source_page_end"),
        "text_basis": text_basis,
        "text": text,
        "metadata": _json_safe(row.get("metadata") or {}),
        "warnings": _json_safe(row.get("warnings") or []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_text_basis(row: dict[str, Any]) -> tuple[str, str]:
    for field_name in ("retrieval_text", "normalized_text", "text"):
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            return field_name, value.strip()
    return "text", ""


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def chunk_review_signal(chunk: Any) -> dict[str, Any]:
    row = chunk_to_review_dict(chunk)
    flags = chunk_review_flags(row)
    tier = review_priority_tier(row, flags)
    severity = review_severity_for_flags(flags, tier)
    return {
        "review_flags": flags,
        "review_priority_tier": tier,
        "review_category": severity.get("review_category", ""),
        "review_severity_rank": severity.get("review_severity_rank"),
        "review_step": severity.get("review_step", ""),
    }


def chunk_to_review_dict(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        row = dict(chunk)
    elif hasattr(chunk, "model_dump"):
        row = chunk.model_dump(mode="json")
    elif hasattr(chunk, "dict"):
        row = chunk.dict()
    else:
        row = {
            "chunk_id": getattr(chunk, "chunk_id", ""),
            "document_id": getattr(chunk, "document_id", ""),
            "chunk_type": getattr(chunk, "chunk_type", ""),
            "text": getattr(chunk, "text", ""),
            "normalized_text": getattr(chunk, "normalized_text", None),
            "retrieval_text": getattr(chunk, "retrieval_text", None),
            "metadata": getattr(chunk, "metadata", {}) or {},
            "warnings": getattr(chunk, "warnings", []) or [],
        }
    row.setdefault("metadata", getattr(chunk, "metadata", {}) or {})
    row.setdefault("warnings", getattr(chunk, "warnings", []) or [])
    row.setdefault("approval_status", getattr(chunk, "approval_status", ""))
    return row


def chunk_attention_reasons(
    chunk: Any,
    *,
    signal: dict[str, Any] | None = None,
    metadata_reasons: list[str] | None = None,
) -> list[str]:
    signal = signal or chunk_review_signal(chunk)
    flags = [str(flag) for flag in signal.get("review_flags", []) if clean_text(flag)]
    tier = clean_text(signal.get("review_priority_tier"))
    metadata = getattr(chunk, "metadata", {}) or {}
    reasons: list[str] = []
    if clean_text(getattr(chunk, "approval_status", "")).lower() == "needs_review":
        reasons.append("approval_status_needs_review")
    if tier in MANUAL_ATTENTION_TIERS:
        reasons.extend(flags)
        if signal.get("review_category"):
            reasons.append(f"review_category:{signal.get('review_category')}")
    reasons.extend(metadata_reasons if metadata_reasons is not None else metadata_attention_reasons(chunk))
    if "processor_warning_candidate" in flags:
        for warning in getattr(chunk, "warnings", []) or []:
            warning_text = clean_text(warning)
            if warning_text:
                reasons.append(f"warning:{warning_text}")
    if "supplementary_or_effective_date_candidate" in flags or (
        tier in MANUAL_ATTENTION_TIERS and supplementary_effective_date_attention(chunk, metadata)
    ):
        reasons.append("supplementary_or_effective_date_candidate")
    return sorted(dict.fromkeys(reasons))


def metadata_attention_reasons(chunk: Any) -> list[str]:
    metadata = getattr(chunk, "metadata", {}) or {}
    reasons: list[str] = []
    for key in ATTENTION_BOOL_METADATA_KEYS:
        if metadata.get(key):
            reasons.append(key)
    for key in ATTENTION_LIST_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            reasons.append(f"{key}:{value.strip()}")
        elif isinstance(value, list):
            for item in value:
                if clean_text(item):
                    reasons.append(f"{key}:{clean_text(item)}")
    return sorted(dict.fromkeys(reasons))


def supplementary_effective_date_attention(chunk: Any, metadata: dict[str, Any]) -> bool:
    chunk_type = clean_text(getattr(chunk, "chunk_type", "")).lower()
    if metadata.get("is_supplementary_provision") or chunk_type in {"supplementary", "supplementary_provision"}:
        return True
    for key in (
        "supplementary_label",
        "supplementary_identifier_date",
        "effective_date",
        "valid_from",
        "valid_to",
        "revision_date",
    ):
        if clean_text(metadata.get(key)):
            return True
    if metadata.get("article_effective_overrides"):
        return True
    text = " ".join(
        clean_text(value)
        for value in (
            getattr(chunk, "text", ""),
            getattr(chunk, "normalized_text", ""),
            getattr(chunk, "retrieval_text", ""),
            metadata.get("hierarchy_path"),
        )
        if clean_text(value)
    )
    compact_text = "".join(text.split())
    return any(keyword in text for keyword in SUPPLEMENTARY_EFFECTIVE_DATE_KEYWORDS) or any(
        keyword in compact_text for keyword in SUPPLEMENTARY_EFFECTIVE_DATE_COMPACT_KEYWORDS
    )


def suggested_document_action(
    *,
    total_chunks: int,
    bulk_candidate_count: int,
    manual_attention_chunk_count: int,
    blocked_or_rejected_count: int,
) -> str:
    if manual_attention_chunk_count or blocked_or_rejected_count:
        return "manual_review_first"
    if bulk_candidate_count:
        return "bulk_review_candidate"
    return "already_approved_or_empty"


def document_matches_filters(document: Any, chunks: Sequence[Any], filters: dict[str, str]) -> bool:
    source_system = clean_text(filters.get("source_system"))
    apba_id = clean_text(filters.get("apba_id"))
    if source_system and metadata_value(document, chunks, "source_system").upper() != source_system.upper():
        return False
    if apba_id and metadata_value(document, chunks, "apba_id") != apba_id:
        return False
    return True


def metadata_value(document: Any, chunks: Sequence[Any], key: str) -> str:
    value = clean_text(getattr(document, key, None))
    if value:
        return value
    for chunk in chunks:
        metadata = getattr(chunk, "metadata", {}) or {}
        value = clean_text(metadata.get(key))
        if value:
            return value
    return ""


def percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def format_counter(counter: Counter[str], *, limit: int) -> str:
    return "; ".join(f"{key}={value}" for key, value in counter.most_common(limit))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Approval Worklist",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Data dir: `{report.get('data_dir')}`",
        f"- Effective data dir: `{report.get('effective_data_dir')}`",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Filters: `{report.get('filters') or {}}`",
        f"- Documents: `{report.get('document_count')}`",
        f"- Total chunks: `{report.get('total_chunks')}`",
        f"- Bulk-review candidate chunks: `{report.get('bulk_review_candidate_chunks')}` ({report.get('bulk_review_candidate_rate')}%)",
        f"- Manual-attention chunks: `{report.get('manual_attention_chunks')}` ({report.get('manual_attention_rate')}%)",
        f"- Low-risk batch-review candidate chunks: `{report.get('low_risk_batch_review_candidate_chunks')}` ({report.get('low_risk_batch_review_candidate_rate')}%)",
        f"- Review priority tiers: `{report.get('review_priority_tier_counts')}`",
        f"- Approval status totals: `{report.get('approval_status_totals')}`",
        f"- Action counts: `{report.get('action_counts')}`",
        "",
        f"Safety: {report.get('safety_note')}",
        "",
        "## Top Documents",
        "",
        "| Rank | Action | Document | APBA | Chunks | Manual attention | Low-risk batch | Blocking | Domain | Top reasons |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in (report.get("documents") or [])[:50]:
        name = row.get("document_name") or row.get("filename") or row.get("document_id")
        lines.append(
            "| {rank} | {action} | {document} | {apba} | {chunks} | {attention} | {low_risk} | {blocking} | {domain} | {reasons} |".format(
                rank=row.get("rank"),
                action=md_cell(approval_action_label(row.get("suggested_action"))),
                document=md_cell(name),
                apba=md_cell(row.get("apba_id")),
                chunks=row.get("total_chunks"),
                attention=row.get("manual_attention_chunks"),
                low_risk=row.get("low_risk_batch_review_candidate_chunks"),
                blocking=row.get("blocking_review_chunks"),
                domain=row.get("domain_attention_chunks"),
                reasons=md_cell(row.get("top_attention_reasons")),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def approval_action_label(value: Any) -> str:
    labels = {
        "manual_review_first": "operator manual review first",
        "bulk_review_candidate": "human bulk-review candidate, not approved",
        "already_approved_or_empty": "no draft approval action",
    }
    return labels.get(str(value or ""), str(value or ""))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a human approval worklist from draft repository chunks.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--source-system")
    parser.add_argument("--apba-id")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_approval_worklist(
        data_dir=args.data_dir,
        source_system=args.source_system,
        apba_id=args.apba_id,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
    )
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_csv:
        write_csv(args.out_csv, report["documents"])
    if args.out_md:
        write_markdown(report, args.out_md)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
