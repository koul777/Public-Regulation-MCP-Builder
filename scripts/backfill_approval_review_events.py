from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.storage.repository import JsonRepository


REQUIRED_EVENT_TYPES = ("approved", "human_review_confirmed", "ai_review_confirmed")


def build_approval_review_event_backfill_report(
    *,
    data_dir: Path,
    actor: str,
    approval_reference: str,
    apply: bool = False,
) -> dict[str, Any]:
    repository = JsonRepository(Settings(data_dir=data_dir, artifact_root=data_dir.parent))
    records = repository.list_approval_journal_records()
    superseded = _superseded_approval_record_ids(records)
    candidates = [
        record
        for record in records
        if _record_key(record)
        and _record_key(record) not in superseded
        and _missing_event_counts(record)
    ]
    corrections = [
        _build_correction_record(
            record,
            actor=actor,
            approval_reference=approval_reference,
        )
        for record in candidates
    ]
    if apply:
        existing_ids = {_record_key(record) for record in records}
        for correction in corrections:
            if _record_key(correction) not in existing_ids:
                repository.append_approval_record(correction)
                existing_ids.add(_record_key(correction))

    return {
        "report_type": "approval_review_event_backfill",
        "generated_at": _utc_now(),
        "data_dir": str(data_dir),
        "actor": actor,
        "approval_reference": approval_reference,
        "applied": bool(apply),
        "journal_record_count": len(records),
        "superseded_record_count": len(superseded),
        "candidate_count": len(candidates),
        "correction_count": len(corrections),
        "corrections": [
            {
                "approval_record_id": correction.get("approval_record_id"),
                "approval_id": correction.get("approval_id"),
                "document_id": correction.get("document_id"),
                "chunk_count": len(correction.get("chunk_ids") or []),
                "supersedes_approval_record_ids": correction.get("supersedes_approval_record_ids") or [],
                "review_decision_event_counts": correction.get("review_decision_event_counts") or {},
            }
            for correction in corrections
        ],
    }


def _build_correction_record(
    source: dict[str, Any],
    *,
    actor: str,
    approval_reference: str,
) -> dict[str, Any]:
    source_record_id = _record_key(source)
    chunk_ids = _chunk_ids(source)
    event_time = _utc_now()
    events: list[dict[str, Any]] = []
    for chunk_id in chunk_ids:
        if bool(source.get("ai_review_confirmed")):
            events.append(
                {
                    "event": "ai_review_confirmed",
                    "timestamp": event_time,
                    "actor": actor,
                    "chunk_id": chunk_id,
                    "ai_reflected": 0,
                    "ai_skipped": 0,
                    "ai_total": 0,
                    "ai_decisions": {},
                    "source_of_truth": _source_of_truth(source, chunk_id),
                    "correction_reason": "approval_journal_review_event_backfill",
                }
            )
        if bool(source.get("human_review_confirmed")):
            events.append(
                {
                    "event": "human_review_confirmed",
                    "timestamp": event_time,
                    "actor": actor,
                    "chunk_id": chunk_id,
                    "source_of_truth": _source_of_truth(source, chunk_id),
                    "correction_reason": "approval_journal_review_event_backfill",
                }
            )
        events.append(
            {
                "event": "approved",
                "timestamp": event_time,
                "actor": actor,
                "chunk_id": chunk_id,
                "source_of_truth": _source_of_truth(source, chunk_id),
                "correction_reason": "approval_journal_review_event_backfill",
            }
        )

    correction_id = _correction_record_id(source_record_id, chunk_ids, approval_reference)
    correction = dict(source)
    correction.update(
        {
            "approval_record_id": correction_id,
            "approved_at": event_time,
            "approved_by": actor,
            "review_decision_events": events,
            "review_decision_event_counts": dict(Counter(str(event.get("event") or "") for event in events)),
            "ai_review_confirmed": bool(source.get("ai_review_confirmed")),
            "human_review_confirmed": bool(source.get("human_review_confirmed")),
            "supersedes_approval_record_ids": [source_record_id],
            "approval_journal_correction": {
                "type": "review_decision_events_backfill",
                "source_approval_record_id": source_record_id,
                "approval_reference": approval_reference,
                "generated_at": event_time,
            },
            "note": f"{str(source.get('note') or '').strip()} approval_review_event_backfill".strip(),
        }
    )
    return correction


def _source_of_truth(source: dict[str, Any], chunk_id: str) -> dict[str, Any]:
    for approved_chunk in source.get("approved_chunks") or []:
        if not isinstance(approved_chunk, dict) or str(approved_chunk.get("chunk_id") or "") != chunk_id:
            continue
        metadata = approved_chunk.get("metadata") if isinstance(approved_chunk.get("metadata"), dict) else {}
        return {
            "table_source": str(metadata.get("table_source") or source.get("table_source") or ""),
            "kordoc_table_promoted": bool(
                metadata.get("kordoc_table_promoted") or source.get("kordoc_table_promoted")
            ),
        }
    return {
        "table_source": str(source.get("table_source") or ""),
        "kordoc_table_promoted": bool(source.get("kordoc_table_promoted")),
    }


def _missing_event_counts(record: dict[str, Any]) -> dict[str, int]:
    chunk_ids = set(_chunk_ids(record))
    if not chunk_ids:
        return {}
    events = record.get("review_decision_events") if isinstance(record.get("review_decision_events"), list) else []
    by_type = {event_type: set() for event_type in REQUIRED_EVENT_TYPES}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_name = str(event.get("event") or "").strip()
        chunk_id = str(event.get("chunk_id") or "").strip()
        if event_name in by_type and chunk_id in chunk_ids:
            by_type[event_name].add(chunk_id)
    required = ["approved"]
    if bool(record.get("human_review_confirmed")):
        required.append("human_review_confirmed")
    if bool(record.get("ai_review_confirmed")):
        required.append("ai_review_confirmed")
    return {
        event_type: len(chunk_ids) - len(by_type[event_type])
        for event_type in required
        if len(chunk_ids) - len(by_type[event_type]) > 0
    }


def _chunk_ids(record: dict[str, Any]) -> list[str]:
    return [str(chunk_id).strip() for chunk_id in record.get("chunk_ids") or [] if str(chunk_id).strip()]


def _record_key(record: dict[str, Any]) -> str:
    return str(record.get("approval_record_id") or record.get("approval_id") or "").strip()


def _superseded_approval_record_ids(records: list[dict[str, Any]]) -> set[str]:
    superseded: set[str] = set()
    for record in records:
        raw = record.get("supersedes_approval_record_ids")
        if isinstance(raw, list):
            superseded.update(str(value).strip() for value in raw if str(value).strip())
        raw = record.get("supersedes_approval_record_id")
        if raw:
            superseded.add(str(raw).strip())
    return superseded


def _correction_record_id(source_record_id: str, chunk_ids: list[str], approval_reference: str) -> str:
    digest = hashlib.sha256(
        "\n".join([source_record_id, approval_reference, *chunk_ids]).encode("utf-8")
    ).hexdigest()[:12]
    return f"approval_record_event_backfill_{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--actor", default="approval-journal-backfill")
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    report = build_approval_review_event_backfill_report(
        data_dir=args.data_dir,
        actor=args.actor,
        approval_reference=args.approval_reference,
        apply=bool(args.apply),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
