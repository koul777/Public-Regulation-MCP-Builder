"""Build document-level review batches for parser goldset open items."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BATCH_FIELDS = (
    "batch_rank",
    "in_first_review_batch",
    "document_id",
    "filename",
    "open_item_count",
    "matched_count_items",
    "label_status_items",
    "structures",
    "pipeline_count_total",
    "source_path",
    "open_source_command",
    "recommended_action",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{key: str(value or "") for key, value in row.items() if key is not None} for row in reader]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ps_literal(path: str) -> str:
    return path.replace("'", "''")


def _recommended_action(structures: set[str], item_kind_counts: Counter[str]) -> str:
    if {"table", "nested_table", "appendix_form"} & structures:
        return "Review table/form evidence first, then fill matched counts and label_status."
    if "paragraph_item" in structures:
        return "Compare article body clauses/items against extracted chunks, then fill matched counts."
    if item_kind_counts.get("label_status") and len(item_kind_counts) == 1:
        return "Confirm the document review is complete and update label_status."
    return "Fill open parser goldset fields and update label_status after verification."


def build_parsing_goldset_review_batches(
    *,
    open_item_worklist_csv: Path,
    first_batch_document_count: int = 6,
) -> dict[str, Any]:
    rows = _load_rows(open_item_worklist_csv)
    by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)
    malformed_rows: list[dict[str, str]] = []
    for row in rows:
        document_id = row.get("document_id", "").strip()
        if document_id:
            by_doc[document_id].append(row)
        else:
            malformed_rows.append(row)

    batches: list[dict[str, Any]] = []
    for document_id, items in by_doc.items():
        priority_rank = min(_int(item.get("priority_rank"), 999999) for item in items)
        item_kind_counts = Counter(item.get("item_kind", "") for item in items if item.get("item_kind"))
        structure_counts = Counter(item.get("structure", "") for item in items if item.get("structure"))
        structures = {structure for structure in structure_counts if structure}
        pipeline_count_total = sum(_int(item.get("pipeline_count")) for item in items)
        first = items[0]
        source_path = first.get("source_path", "")
        batches.append(
            {
                "batch_rank": priority_rank,
                "document_id": document_id,
                "filename": first.get("filename", ""),
                "open_item_count": len(items),
                "item_kind_counts": dict(sorted(item_kind_counts.items())),
                "matched_count_items": item_kind_counts.get("matched_count", 0),
                "label_status_items": item_kind_counts.get("label_status", 0),
                "structure_counts": dict(sorted(structure_counts.items())),
                "structures": ", ".join(sorted(structures)) or "-",
                "pipeline_count_total": pipeline_count_total,
                "source_path": source_path,
                "open_source_command": (
                    f"Invoke-Item -LiteralPath '{_ps_literal(source_path)}'" if source_path else ""
                ),
                "recommended_action": _recommended_action(structures, item_kind_counts),
                "open_columns": [item.get("column_name", "") for item in items],
            }
        )

    if malformed_rows:
        priority_rank = min(_int(item.get("priority_rank"), 999999) for item in malformed_rows)
        item_kind_counts = Counter(
            item.get("item_kind", "") for item in malformed_rows if item.get("item_kind")
        )
        structure_counts = Counter(
            item.get("structure", "") for item in malformed_rows if item.get("structure")
        )
        structures = {structure for structure in structure_counts if structure}
        pipeline_count_total = sum(_int(item.get("pipeline_count")) for item in malformed_rows)
        first = malformed_rows[0]
        source_path = first.get("source_path", "")
        batches.append(
            {
                "batch_rank": priority_rank,
                "document_id": "__missing_document_id__",
                "filename": first.get("filename", ""),
                "open_item_count": len(malformed_rows),
                "item_kind_counts": dict(sorted(item_kind_counts.items())),
                "matched_count_items": item_kind_counts.get("matched_count", 0),
                "label_status_items": item_kind_counts.get("label_status", 0),
                "structure_counts": dict(sorted(structure_counts.items())),
                "structures": ", ".join(sorted(structures)) or "-",
                "pipeline_count_total": pipeline_count_total,
                "source_path": source_path,
                "open_source_command": (
                    f"Invoke-Item -LiteralPath '{_ps_literal(source_path)}'" if source_path else ""
                ),
                "recommended_action": (
                    "Fix missing document_id values in the parser open-item worklist before "
                    "using this batch for release evidence."
                ),
                "open_columns": [item.get("column_name", "") for item in malformed_rows],
                "malformed_reason": "missing_document_id",
            }
        )

    batches.sort(
        key=lambda item: (
            _int(item.get("batch_rank"), 999999),
            -_int(item.get("open_item_count")),
            str(item.get("document_id") or ""),
        )
    )
    for index, batch in enumerate(batches, start=1):
        batch["batch_rank"] = index
        batch["in_first_review_batch"] = index <= first_batch_document_count

    first_batch = [batch for batch in batches if batch["in_first_review_batch"]]
    return {
        "report_type": "parsing_goldset_review_batches",
        "generated_at": _utc_now(),
        "source_open_item_worklist_csv": str(open_item_worklist_csv),
        "document_batch_count": len(batches),
        "open_item_count": len(rows),
        "malformed_open_item_count": len(malformed_rows),
        "malformed_open_items": malformed_rows[:10],
        "first_batch_document_count": len(first_batch),
        "first_batch_open_item_count": sum(_int(batch.get("open_item_count")) for batch in first_batch),
        "first_batch_pipeline_count_total": sum(
            _int(batch.get("pipeline_count_total")) for batch in first_batch
        ),
        "item_kind_counts": dict(Counter(row.get("item_kind", "") for row in rows if row.get("item_kind"))),
        "structure_counts": dict(Counter(row.get("structure", "") for row in rows if row.get("structure"))),
        "first_review_batch": first_batch,
        "document_batches": batches,
        "safety_note": (
            "This review-batch report is read-only. It does not fill labels, infer counts, "
            "approve chunks, or write Vector DB records."
        ),
        "api_call_count": 0,
    }


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Parsing Goldset Review Batches",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Documents with open items: {report.get('document_batch_count')}",
        f"- Open items: {report.get('open_item_count')}",
        f"- Malformed open items: {report.get('malformed_open_item_count')}",
        f"- First review batch: {report.get('first_batch_document_count')} documents / {report.get('first_batch_open_item_count')} open items",
        f"- First batch pipeline count total: {report.get('first_batch_pipeline_count_total')}",
        "",
        "## First Review Batch",
        "",
        "| Rank | Document | Items | Matched | Label Status | Structures | Pipeline Total | First Action |",
        "| ---: | --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    for batch in report.get("first_review_batch") or []:
        if not isinstance(batch, dict):
            continue
        lines.append(
            "| {rank} | {doc} | {items} | {matched} | {label_status} | {structures} | {pipeline} | {action} |".format(
                rank=_md_cell(batch.get("batch_rank")),
                doc=_md_cell(batch.get("document_id")),
                items=_md_cell(batch.get("open_item_count")),
                matched=_md_cell(batch.get("matched_count_items")),
                label_status=_md_cell(batch.get("label_status_items")),
                structures=_md_cell(batch.get("structures")),
                pipeline=_md_cell(batch.get("pipeline_count_total")),
                action=_md_cell(batch.get("recommended_action")),
            )
        )
    lines.extend(["", "## Source Commands", ""])
    for batch in report.get("first_review_batch") or []:
        if not isinstance(batch, dict):
            continue
        command = batch.get("open_source_command") or "-"
        lines.append(f"- `{batch.get('document_id')}`: `{command}`")
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def write_batch_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BATCH_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report.get("document_batches") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-item-worklist-csv", required=True, type=Path)
    parser.add_argument("--first-batch-document-count", type=int, default=6)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-csv", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_parsing_goldset_review_batches(
        open_item_worklist_csv=args.open_item_worklist_csv,
        first_batch_document_count=args.first_batch_document_count,
    )
    _write_json(args.out_json, report)
    write_batch_csv(args.out_csv, report)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
