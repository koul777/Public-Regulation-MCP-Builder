from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


def build_parsing_goldset_start_here(
    *,
    labels_csv: Path,
    completion_board_json: Path | None = None,
    table_review_batches_csv: Path | None = None,
    out_json: Path,
    out_md: Path,
    out_worklist_csv: Path | None = None,
    top_doc_count: int = 5,
    first_table_batch_count: int = 5,
    base_dir: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if top_doc_count <= 0:
        raise ValueError("top_doc_count must be greater than zero.")
    if first_table_batch_count < 0:
        raise ValueError("first_table_batch_count must be zero or greater.")

    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    base_dir = (base_dir or Path.cwd()).resolve()
    labels_csv = _resolve(base_dir, labels_csv)
    labels = _load_csv(labels_csv)
    labels_by_doc = {str(row.get("document_id") or "").strip(): row for row in labels}
    completion = _load_json(_resolve(base_dir, completion_board_json)) if completion_board_json else {}
    board_rows = completion.get("rows") if isinstance(completion.get("rows"), list) else []
    if not board_rows:
        board_rows = [_fallback_board_row(row) for row in labels]
    accepted_label_statuses = _accepted_label_statuses(completion)

    top_docs = [
        _document_action(
            row,
            labels_by_doc=labels_by_doc,
            base_dir=base_dir,
            accepted_label_statuses=accepted_label_statuses,
        )
        for row in sorted(board_rows, key=_board_row_sort_key)
        if str(row.get("ready_for_quality_claim") or "").lower() != "true"
    ][:top_doc_count]
    structure_review_queue = _structure_review_queue(completion, board_rows)
    open_items = _open_item_rows(
        board_rows,
        labels_by_doc=labels_by_doc,
        base_dir=base_dir,
        accepted_label_statuses=accepted_label_statuses,
    )

    table_batches = []
    if table_review_batches_csv:
        table_batches_path = _resolve(base_dir, table_review_batches_csv)
        table_batches = [
            _table_batch_action(row, base_dir=base_dir)
            for row in _load_csv(table_batches_path)[:first_table_batch_count]
        ]

    report = {
        "report_type": "parsing_goldset_start_here",
        "generated_at": generated_at,
        "source_artifacts": {
            "labels_csv": _artifact(labels_csv),
            "completion_board_json": _artifact(_resolve(base_dir, completion_board_json))
            if completion_board_json
            else None,
            "table_review_batches_csv": _artifact(_resolve(base_dir, table_review_batches_csv))
            if table_review_batches_csv
            else None,
        },
        "open_commands": {
            "open_label_csv": _invoke_item_command(labels_csv),
            "select_label_csv_in_explorer": _explorer_select_command(labels_csv),
        },
        "completion_summary": _completion_summary(completion, len(labels)),
        "open_item_summary": _open_item_summary(open_items),
        "structure_review_queue": structure_review_queue,
        "top_document_count": len(top_docs),
        "top_documents": top_docs,
        "first_table_batch_count": len(table_batches),
        "first_table_batches": table_batches,
        "safety_note": (
            "This start-here packet is read-only. It does not fill labels, infer human counts, "
            "approve chunks, index vectors, or publish MCP evidence."
        ),
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report), encoding="utf-8")
    if out_worklist_csv:
        _write_open_item_worklist(out_worklist_csv, open_items)
        report["output_artifacts"] = {"open_item_worklist_csv": str(out_worklist_csv)}
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out_md.write_text(_markdown(report), encoding="utf-8")
    return report


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError(f"{path} must contain at least one data row.")
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return payload


def _fallback_board_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "priority_rank": str(row.get("review_order") or ""),
        "review_order": str(row.get("review_order") or ""),
        "priority_tier": "baseline_goldset_fill",
        "recommended_next_action": "Fill every manual_* and matched_* count, reviewer, reviewed_at, and label_status.",
        "document_id": str(row.get("document_id") or ""),
        "label_status": str(row.get("label_status") or ""),
        "ready_for_quality_claim": "false",
        "score_rows_complete": "",
        "score_rows_expected": "",
        "missing_manual_fields": "; ".join(
            key for key, value in row.items() if key.startswith("manual_") and not str(value or "").strip()
        ),
        "missing_matched_fields": "; ".join(
            key for key, value in row.items() if key.startswith("matched_") and not str(value or "").strip()
        ),
        "missing_reviewer_metadata": "; ".join(
            key for key in ("reviewer", "reviewed_at") if not str(row.get(key) or "").strip()
        ),
        "packet_path": "",
    }


def _document_action(
    row: dict[str, Any],
    *,
    labels_by_doc: dict[str, dict[str, str]],
    base_dir: Path,
    accepted_label_statuses: set[str],
) -> dict[str, Any]:
    document_id = str(row.get("document_id") or "").strip()
    label = labels_by_doc.get(document_id, {})
    label_status = str(row.get("label_status") or label.get("label_status") or "").strip()
    label_status_ready = label_status.lower() in accepted_label_statuses
    source_path = _resolve_optional(base_dir, label.get("source_path"))
    packet_path = _resolve_optional(base_dir, row.get("packet_path"))
    chunk_artifact = _resolve_optional(base_dir, label.get("chunk_artifact"))
    manual_fields = _split_semicolon(row.get("missing_manual_fields"))
    matched_fields = _split_semicolon(row.get("missing_matched_fields"))
    metadata_fields = _split_semicolon(row.get("missing_reviewer_metadata"))
    return {
        "priority_rank": _int(row.get("priority_rank")),
        "review_order": _int(row.get("review_order")),
        "document_id": document_id,
        "label_status": label_status,
        "label_status_ready": label_status_ready,
        "status_next_action": (
            "Set label_status to reviewed or approved after confirming all counts are final."
            if not label_status_ready
            else ""
        ),
        "priority_tier": row.get("priority_tier") or "",
        "recommended_next_action": row.get("recommended_next_action") or "",
        "institution_name": label.get("institution_name") or row.get("institution_name") or "",
        "filename": label.get("filename") or (source_path.name if source_path else "") or row.get("filename") or "",
        "extension": label.get("extension") or (source_path.suffix if source_path else "") or row.get("extension") or "",
        "source_path": str(source_path) if source_path else "",
        "source_exists": bool(source_path and source_path.exists()),
        "open_source_command": _invoke_item_command(source_path) if source_path else "",
        "packet_path": str(packet_path) if packet_path else "",
        "packet_exists": bool(packet_path and packet_path.exists()),
        "open_packet_command": _invoke_item_command(packet_path) if packet_path else "",
        "chunk_artifact": str(chunk_artifact) if chunk_artifact else "",
        "chunk_artifact_exists": bool(chunk_artifact and chunk_artifact.exists()),
        "score_rows_complete": _int(row.get("score_rows_complete")),
        "score_rows_expected": _int(row.get("score_rows_expected")),
        "missing_manual_fields": manual_fields,
        "missing_matched_fields": matched_fields,
        "missing_reviewer_metadata": metadata_fields,
        "field_counts": {
            "missing_manual": len(manual_fields),
            "missing_matched": len(matched_fields),
            "missing_reviewer_metadata": len(metadata_fields),
            "missing_label_status": 0 if label_status_ready else 1,
        },
        "pipeline_counts": {
            "article": _int(label.get("pipeline_article_count") or row.get("pipeline_article_count")),
            "paragraph_item": _int(
                label.get("pipeline_paragraph_item_count") or row.get("pipeline_paragraph_item_count")
            ),
            "appendix_form": _int(label.get("pipeline_appendix_form_count") or row.get("pipeline_appendix_form_count")),
            "table": _int(label.get("pipeline_table_count") or row.get("pipeline_table_count")),
            "nested_table": _int(label.get("pipeline_nested_table_count") or row.get("pipeline_nested_table_count")),
            "supplementary_effective_date": _int(
                label.get("pipeline_supplementary_effective_date_count")
                or row.get("pipeline_supplementary_effective_date_count")
            ),
            "footnote_caption": _int(
                label.get("pipeline_footnote_caption_count") or row.get("pipeline_footnote_caption_count")
            ),
        },
    }


def _table_batch_action(row: dict[str, str], *, base_dir: Path) -> dict[str, Any]:
    source_path = _resolve_optional(base_dir, row.get("source_path"))
    packet_path = _resolve_optional(base_dir, row.get("table_unit_packet_csv"))
    return {
        "batch_rank": _int(row.get("batch_rank")),
        "table_review_batch_id": row.get("table_review_batch_id") or "",
        "document_id": row.get("document_id") or "",
        "review_priority": row.get("review_priority") or "",
        "unit_count": _int(row.get("unit_count")),
        "source_page_ranges": row.get("source_page_ranges") or "",
        "label_review_flag_counts": row.get("label_review_flag_counts") or "",
        "source_path": str(source_path) if source_path else "",
        "source_exists": bool(source_path and source_path.exists()),
        "open_source_command": _invoke_item_command(source_path) if source_path else "",
        "table_unit_packet_csv": str(packet_path) if packet_path else "",
        "table_unit_packet_exists": bool(packet_path and packet_path.exists()),
    }


def _completion_summary(completion: dict[str, Any], fallback_document_count: int) -> dict[str, Any]:
    return {
        "document_count": _int(completion.get("document_count")) or fallback_document_count,
        "ready_document_count": _int(completion.get("ready_document_count")),
        "pending_document_count": _int(completion.get("pending_document_count")),
        "expected_structure_score_rows": _int(completion.get("expected_structure_score_rows")),
        "completed_structure_score_rows": _int(completion.get("completed_structure_score_rows")),
        "missing_manual_field_count": _int(completion.get("missing_manual_field_count")),
        "missing_matched_field_count": _int(completion.get("missing_matched_field_count")),
        "missing_reviewer_metadata_count": _int(completion.get("missing_reviewer_metadata_count")),
        "completion_gate_status": completion.get("completion_gate_status") or "unknown",
        "ready_for_quality_claim": bool(completion.get("ready_for_quality_claim")),
    }


def _accepted_label_statuses(completion: dict[str, Any]) -> set[str]:
    values = completion.get("accepted_label_statuses")
    if not isinstance(values, list) or not values:
        values = ["approved", "completed", "human_reviewed", "reviewed"]
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _structure_review_queue(
    completion: dict[str, Any],
    board_rows: list[dict[str, Any]],
    *,
    max_docs_per_structure: int = 5,
) -> list[dict[str, Any]]:
    structure_summary = completion.get("structure_completion_summary")
    if not isinstance(structure_summary, dict):
        return []

    rows_by_structure: dict[str, list[dict[str, Any]]] = {}
    for row in board_rows:
        structures = _split_semicolon(row.get("missing_structures"))
        if not structures:
            structures = [
                structure
                for structure in (
                    _matched_field_to_structure(field)
                    for field in _split_semicolon(row.get("missing_matched_fields"))
                )
                if structure
            ]
        for structure in structures:
            rows_by_structure.setdefault(structure, []).append(row)

    queue = []
    for structure, stats in structure_summary.items():
        if not isinstance(stats, dict):
            continue
        missing_matched = _int(stats.get("missing_matched_count"))
        ready = _bool(stats.get("ready_for_structure_f1"))
        if missing_matched <= 0 and ready:
            continue
        documents = [
            str(row.get("document_id") or "")
            for row in sorted(rows_by_structure.get(str(structure), []), key=_board_row_sort_key)
            if str(row.get("document_id") or "")
        ][:max_docs_per_structure]
        queue.append(
            {
                "structure": str(structure),
                "missing_matched_count": missing_matched,
                "score_rows_complete": _int(stats.get("score_rows_complete")),
                "expected_document_count": _int(stats.get("expected_document_count")),
                "pipeline_total": _int(stats.get("pipeline_total")),
                "ready_for_structure_f1": ready,
                "first_document_ids": documents,
                "recommended_action": _structure_recommended_action(str(structure)),
            }
        )

    queue.sort(
        key=lambda item: (
            -_int(item.get("missing_matched_count")),
            -_int(item.get("pipeline_total")),
            str(item.get("structure") or ""),
        )
    )
    for index, item in enumerate(queue, start=1):
        item["priority_rank"] = index
    return queue


def _matched_field_to_structure(field: str) -> str:
    if not field.startswith("matched_") or not field.endswith("_count"):
        return ""
    return field.removeprefix("matched_").removesuffix("_count")


def _structure_recommended_action(structure: str) -> str:
    if structure in {"table", "nested_table", "appendix_form"}:
        return "Compare source tables/forms against extracted chunks and fill matched counts."
    if structure == "supplementary_effective_date":
        return "Check supplementary provisions and effective-date markers before filling matched counts."
    if structure == "paragraph_item":
        return "Compare article body clauses/items against extracted paragraph and item chunks."
    if structure == "footnote_caption":
        return "Check captions and footnotes only after core article/table fields are reviewed."
    return "Fill the remaining matched count for this structure."


OPEN_ITEM_FIELDS = (
    "priority_rank",
    "document_id",
    "filename",
    "item_kind",
    "column_name",
    "structure",
    "pipeline_count",
    "label_status",
    "recommended_action",
    "source_path",
)


def _open_item_rows(
    board_rows: list[dict[str, Any]],
    *,
    labels_by_doc: dict[str, dict[str, str]],
    base_dir: Path,
    accepted_label_statuses: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for board_row in sorted(board_rows, key=_board_row_sort_key):
        document_id = str(board_row.get("document_id") or "").strip()
        label = labels_by_doc.get(document_id, {})
        source_path = _resolve_optional(base_dir, label.get("source_path"))
        label_status = str(board_row.get("label_status") or label.get("label_status") or "").strip()
        common = {
            "priority_rank": _int(board_row.get("priority_rank")),
            "document_id": document_id,
            "filename": label.get("filename") or board_row.get("filename") or "",
            "label_status": label_status,
            "source_path": str(source_path) if source_path else "",
        }
        for field in _split_semicolon(board_row.get("missing_manual_fields")):
            rows.append(
                {
                    **common,
                    "item_kind": "manual_count",
                    "column_name": field,
                    "structure": _count_field_to_structure(field, prefix="manual_"),
                    "pipeline_count": _pipeline_count_for_field(field, label, board_row),
                    "recommended_action": "Fill the manual count from source review.",
                }
            )
        for field in _split_semicolon(board_row.get("missing_matched_fields")):
            rows.append(
                {
                    **common,
                    "item_kind": "matched_count",
                    "column_name": field,
                    "structure": _count_field_to_structure(field, prefix="matched_"),
                    "pipeline_count": _pipeline_count_for_field(field, label, board_row),
                    "recommended_action": "Fill the matched count after comparing source and extracted output.",
                }
            )
        for field in _split_semicolon(board_row.get("missing_reviewer_metadata")):
            rows.append(
                {
                    **common,
                    "item_kind": "reviewer_metadata",
                    "column_name": field,
                    "structure": "",
                    "pipeline_count": "",
                    "recommended_action": "Fill reviewer metadata for the completed review.",
                }
            )
        if label_status.lower() not in accepted_label_statuses:
            rows.append(
                {
                    **common,
                    "item_kind": "label_status",
                    "column_name": "label_status",
                    "structure": "",
                    "pipeline_count": "",
                    "recommended_action": "Set label_status to reviewed or approved after confirming all counts.",
                }
            )
    return rows


def _open_item_summary(open_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "open_item_count": len(open_items),
        "item_kind_counts": dict(Counter(str(item.get("item_kind") or "") for item in open_items)),
        "structure_counts": dict(
            Counter(str(item.get("structure") or "") for item in open_items if item.get("structure"))
        ),
    }


def _write_open_item_worklist(path: Path, open_items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OPEN_ITEM_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(open_items)


def _count_field_to_structure(field: str, *, prefix: str) -> str:
    if not field.startswith(prefix) or not field.endswith("_count"):
        return ""
    return field.removeprefix(prefix).removesuffix("_count")


def _pipeline_count_for_field(
    field: str,
    label: dict[str, str],
    board_row: dict[str, Any],
) -> int | str:
    structure = _count_field_to_structure(field, prefix="manual_") or _count_field_to_structure(
        field,
        prefix="matched_",
    )
    if not structure:
        return ""
    return _int(label.get(f"pipeline_{structure}_count") or board_row.get(f"pipeline_{structure}_count"))


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "byte_count": path.stat().st_size if path.exists() else 0,
    }


def _resolve(base_dir: Path, path: Path | str | None) -> Path:
    if path is None:
        raise ValueError("path must not be None")
    path = Path(path)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _resolve_optional(base_dir: Path, value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _resolve(base_dir, text)


def _split_semicolon(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _board_row_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        _int(row.get("priority_rank")) or 999_999,
        _int(row.get("review_order")) or 999_999,
        str(row.get("document_id") or ""),
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _invoke_item_command(path: Path) -> str:
    return f"Invoke-Item -LiteralPath {_ps_quote(path)}"


def _explorer_select_command(path: Path) -> str:
    return f'explorer.exe /select,"{path}"'


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any]) -> str:
    summary = report["completion_summary"]
    open_item_summary = report["open_item_summary"]
    labels_path = report["source_artifacts"]["labels_csv"]["path"]
    lines = [
        "# Parsing Goldset Start Here",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Label CSV: `{labels_path}`",
        f"- Completion gate: `{summary['completion_gate_status']}`",
        f"- Documents ready: {summary['ready_document_count']} / {summary['document_count']}",
        f"- Structure score rows: {summary['completed_structure_score_rows']} / {summary['expected_structure_score_rows']}",
        f"- Missing manual / matched fields: {summary['missing_manual_field_count']} / {summary['missing_matched_field_count']}",
        f"- Open review items: {open_item_summary['open_item_count']}",
        "",
        "## Open The Label CSV",
        "",
        "```powershell",
        report["open_commands"]["open_label_csv"],
        report["open_commands"]["select_label_csv_in_explorer"],
        "```",
        "",
    ]
    output_artifacts = report.get("output_artifacts") or {}
    if output_artifacts.get("open_item_worklist_csv"):
        lines.extend(
            [
                f"- Open-item worklist CSV: `{output_artifacts['open_item_worklist_csv']}`",
                "",
            ]
        )
    if report["structure_review_queue"]:
        lines.extend(
            [
                "## Structure Review Queue",
                "",
                "| Priority | Structure | Missing Matched | Complete / Expected | Pipeline Total | First Documents |",
                "| ---: | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for item in report["structure_review_queue"]:
            lines.append(
                f"| {item['priority_rank']} | `{_md(item['structure'])}` | {item['missing_matched_count']} | "
                f"{item['score_rows_complete']} / {item['expected_document_count']} | {item['pipeline_total']} | "
                f"{_md(', '.join(item['first_document_ids']) or '-')} |"
            )
        lines.append("")
    lines.extend(
        [
        "## First Documents",
        "",
        "| Priority | Document | Label Status | Source Exists | Packet Exists | Open Items | Next Action |",
        "| ---: | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for document in report["top_documents"]:
        missing_count = (
            document["field_counts"]["missing_manual"]
            + document["field_counts"]["missing_matched"]
            + document["field_counts"]["missing_reviewer_metadata"]
            + document["field_counts"]["missing_label_status"]
        )
        next_action = document["recommended_next_action"]
        if missing_count == 1 and document.get("status_next_action"):
            next_action = document["status_next_action"]
        lines.append(
            f"| {document['priority_rank']} | `{_md(document['document_id'])}` / {_md(document['filename'])} | "
            f"`{_md(document['label_status'])}` | {str(document['source_exists']).lower()} | "
            f"{str(document['packet_exists']).lower()} | {missing_count} | {_md(next_action)} |"
        )
    if report["first_table_batches"]:
        lines.extend(
            [
                "",
                "## First Table Batches",
                "",
                "| Batch | Document | Units | Source Exists | Label Flags | Page Ranges |",
                "| ---: | --- | ---: | --- | --- | --- |",
            ]
        )
        for batch in report["first_table_batches"]:
            lines.append(
                f"| {batch['batch_rank']} | `{_md(batch['document_id'])}` | {batch['unit_count']} | "
                f"{str(batch['source_exists']).lower()} | {_md(batch['label_review_flag_counts'])} | "
                f"{_md(batch['source_page_ranges'])} |"
            )
    lines.extend(["", "## Safety Note", "", report["safety_note"], ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a start-here packet for parsing goldset review.")
    parser.add_argument("--labels-csv", required=True, type=Path)
    parser.add_argument("--completion-board-json", type=Path)
    parser.add_argument("--table-review-batches-csv", type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--out-worklist-csv", type=Path)
    parser.add_argument("--top-doc-count", type=int, default=5)
    parser.add_argument("--first-table-batch-count", type=int, default=5)
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    report = build_parsing_goldset_start_here(
        labels_csv=args.labels_csv,
        completion_board_json=args.completion_board_json,
        table_review_batches_csv=args.table_review_batches_csv,
        out_json=args.out_json,
        out_md=args.out_md,
        out_worklist_csv=args.out_worklist_csv,
        top_doc_count=args.top_doc_count,
        first_table_batch_count=args.first_table_batch_count,
        base_dir=args.base_dir,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "out_json": str(args.out_json),
                "out_md": str(args.out_md),
                "out_worklist_csv": str(args.out_worklist_csv) if args.out_worklist_csv else None,
                "top_document_count": report["top_document_count"],
                "first_table_batch_count": report["first_table_batch_count"],
                "open_item_count": report["open_item_summary"]["open_item_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
