from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.analyze_regulation_corpus import (
    GOLDSET_COMPLETE_LABEL_STATUSES,
    GOLDSET_SCORE_SPECS,
    effective_goldset_matched_count,
    optional_int,
    _goldset_scope,
)


BOARD_FIELDNAMES = [
    "priority_rank",
    "review_order",
    "priority_tier",
    "recommended_next_action",
    "document_id",
    "score_scope",
    "excluded_from_quality_claim",
    "exclusion_reason",
    "label_status",
    "ready_for_quality_claim",
    "score_rows_complete",
    "score_rows_expected",
    "missing_manual_fields",
    "missing_matched_fields",
    "missing_reviewer_metadata",
    "missing_structures",
    "next_structure_checklist",
    "extension",
    "institution_name",
    "filename",
    "packet_path",
    "table_burden_score",
    "attachment_temporal_burden_score",
    "pipeline_article_count",
    "pipeline_paragraph_item_count",
    "pipeline_appendix_form_count",
    "pipeline_table_count",
    "pipeline_nested_table_count",
    "pipeline_supplementary_effective_date_count",
    "pipeline_footnote_caption_count",
    "human_progress_notes",
]

DISPLAY_TEXT_FIELDS = {
    "institution_name",
    "filename",
    "source_path",
}


def build_parsing_goldset_completion_board(
    *,
    labels_csv: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    packet_dir: Path | None = None,
    max_md_rows: int = 30,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if max_md_rows <= 0:
        raise ValueError("max_md_rows must be greater than zero.")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    label_rows = _load_label_rows(labels_csv)
    packet_index = _packet_index(packet_dir) if packet_dir else {}
    rows = [
        _board_row(label_row, packet_index=packet_index)
        for label_row in label_rows
    ]
    rows.sort(key=_row_sort_key)
    for index, row in enumerate(rows, start=1):
        row["priority_rank"] = str(index)

    status_counts = Counter(row["label_status"] or "(missing)" for row in rows)
    priority_counts = Counter(row["priority_tier"] for row in rows)
    expected_score_rows = len(rows) * len(GOLDSET_SCORE_SPECS)
    completed_score_rows = sum(int(row["score_rows_complete"]) for row in rows)
    ready_document_count = sum(1 for row in rows if row["ready_for_quality_claim"] == "true")
    missing_manual_field_count = sum(
        len(_split_fields(row["missing_manual_fields"]))
        for row in rows
    )
    missing_matched_field_count = sum(
        len(_split_fields(row["missing_matched_fields"]))
        for row in rows
    )
    missing_reviewer_metadata_count = sum(
        1 for row in rows if row["missing_reviewer_metadata"]
    )
    quality_claim_rows = [row for row in rows if row["excluded_from_quality_claim"] != "true"]
    excluded_rows = [row for row in rows if row["excluded_from_quality_claim"] == "true"]
    structure_completion = _structure_completion_summary(label_rows)
    report = {
        "report_type": "parsing_goldset_completion_board",
        "generated_at": generated_at,
        "source_labels_csv": str(labels_csv),
        "packet_dir": str(packet_dir) if packet_dir else "",
        "document_count": len(rows),
        "ready_document_count": ready_document_count,
        "pending_document_count": max(len(rows) - ready_document_count, 0),
        "expected_structure_score_rows": expected_score_rows,
        "completed_structure_score_rows": completed_score_rows,
        "missing_structure_score_rows": max(expected_score_rows - completed_score_rows, 0),
        "missing_manual_field_count": missing_manual_field_count,
        "missing_matched_field_count": missing_matched_field_count,
        "missing_reviewer_metadata_count": missing_reviewer_metadata_count,
        "quality_claim_document_count": len(quality_claim_rows),
        "quality_claim_completed_structure_score_rows": sum(
            int(row["score_rows_complete"]) for row in quality_claim_rows
        ),
        "quality_claim_expected_structure_score_rows": len(quality_claim_rows) * len(GOLDSET_SCORE_SPECS),
        "quality_claim_missing_matched_field_count": sum(
            len(_split_fields(row["missing_matched_fields"]))
            for row in quality_claim_rows
        ),
        "excluded_document_count": len(excluded_rows),
        "ready_for_quality_claim": bool(rows) and ready_document_count == len(rows),
        "completion_gate_status": (
            "ready_for_quality_claim"
            if bool(rows) and ready_document_count == len(rows)
            else "blocked_pending_human_labels"
        ),
        "accepted_label_statuses": sorted(GOLDSET_COMPLETE_LABEL_STATUSES),
        "label_status_counts": dict(status_counts),
        "priority_tier_counts": dict(priority_counts),
        "structure_completion_summary": structure_completion,
        "max_md_rows": max_md_rows,
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "safety_note": (
            "This board is read-only. It does not fill human labels, approve chunks, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, rows)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, rows[:max_md_rows]), encoding="utf-8")
    return report


def _load_label_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [_normalize_label_row(dict(row)) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("labels_csv must contain at least one row.")
    if "document_id" not in rows[0]:
        raise ValueError("labels_csv must include a document_id column.")
    return rows


def _normalize_label_row(row: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for field, value in row.items():
        text = "" if value is None else str(value)
        normalized[field] = (
            _repair_utf8_as_cp949_mojibake(text)
            if field in DISPLAY_TEXT_FIELDS
            else text
        )
    return normalized


def _repair_utf8_as_cp949_mojibake(value: str) -> str:
    if not value or not _has_mojibake_signal(value):
        return value
    try:
        repaired = value.encode("cp949").decode("utf-8")
    except UnicodeError:
        return value
    return repaired if _text_quality_score(repaired) > _text_quality_score(value) else value


def _has_mojibake_signal(value: str) -> bool:
    return "\ufffd" in value or any(_is_cjk_ideograph(char) for char in value)


def _text_quality_score(value: str) -> int:
    hangul_count = sum(1 for char in value if _is_hangul_syllable(char))
    cjk_count = sum(1 for char in value if _is_cjk_ideograph(char))
    replacement_count = value.count("\ufffd")
    return hangul_count * 2 - cjk_count * 3 - replacement_count * 100


def _is_hangul_syllable(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _is_cjk_ideograph(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _packet_index(packet_dir: Path) -> dict[str, str]:
    if not packet_dir.exists():
        raise ValueError(f"packet_dir does not exist: {packet_dir}")
    index: dict[str, str] = {}
    for path in sorted(packet_dir.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        parts = path.stem.split("_")
        for offset, part in enumerate(parts):
            if part != "doc":
                continue
            for end in range(offset + 2, len(parts) + 1):
                index.setdefault("_".join(parts[offset:end]), str(path))
    return index


def _board_row(label_row: dict[str, str], *, packet_index: dict[str, str]) -> dict[str, str]:
    document_id = str(label_row.get("document_id") or "").strip()
    scope = _goldset_scope(label_row)
    status = str(label_row.get("label_status") or "").strip().lower()
    reviewer = str(label_row.get("reviewer") or "").strip()
    reviewed_at = str(label_row.get("reviewed_at") or "").strip()
    source_path = str(label_row.get("source_path") or "")
    institution_name = _best_display_text(
        str(label_row.get("institution_name") or ""),
        fallback=_institution_from_source_path(source_path),
    )
    filename = _best_display_text(
        str(label_row.get("filename") or ""),
        fallback=_filename_from_source_path(source_path),
    )
    missing_metadata = [
        field
        for field, value in (("reviewer", reviewer), ("reviewed_at", reviewed_at))
        if not value
    ]
    completed_structures = 0
    missing_manual_fields: list[str] = []
    missing_matched_fields: list[str] = []
    derived_zero_matched_fields: list[str] = []
    missing_structures: list[str] = []
    next_structure_checklist: list[str] = []
    for structure_type, spec in GOLDSET_SCORE_SPECS.items():
        manual_field = spec["manual_field"]
        pipeline_field = spec["pipeline_field"]
        match_field = spec["match_field"]
        manual = optional_int(label_row.get(manual_field))
        pipeline = optional_int(label_row.get(pipeline_field))
        label_matched = optional_int(label_row.get(match_field))
        matched, matched_source = effective_goldset_matched_count(
            manual_count=manual,
            pipeline_count=pipeline,
            matched_count=label_matched,
        )
        missing_parts: list[str] = []
        if manual is None:
            missing_manual_fields.append(manual_field)
            missing_parts.append(f"manual={manual_field}")
        if matched is None:
            missing_matched_fields.append(match_field)
            missing_parts.append(f"matched={match_field}")
        elif matched_source == "derived_zero_bound":
            derived_zero_matched_fields.append(match_field)
        if missing_parts:
            missing_structures.append(structure_type)
            pipeline_hint = "" if pipeline is None else str(pipeline)
            next_structure_checklist.append(
                f"{structure_type}: {', '.join(missing_parts)}"
                + (f", pipeline={pipeline_hint}" if pipeline_hint else "")
            )
        if manual is not None and pipeline is not None and matched is not None:
            completed_structures += 1

    ready = (
        status in GOLDSET_COMPLETE_LABEL_STATUSES
        and not missing_metadata
        and completed_structures == len(GOLDSET_SCORE_SPECS)
    )
    table_burden = (
        _pipeline_count(label_row, "pipeline_table_count")
        + _pipeline_count(label_row, "pipeline_nested_table_count") * 3
        + _pipeline_count(label_row, "pipeline_appendix_form_count")
    )
    attachment_temporal_burden = table_burden + _pipeline_count(
        label_row,
        "pipeline_supplementary_effective_date_count",
    ) + _pipeline_count(label_row, "pipeline_footnote_caption_count") * 2
    priority_tier = _priority_tier(label_row, table_burden, attachment_temporal_burden, ready)
    return {
        "priority_rank": "",
        "review_order": str(label_row.get("review_order") or ""),
        "priority_tier": priority_tier,
        "recommended_next_action": _recommended_next_action(priority_tier, ready),
        "document_id": document_id,
        "score_scope": str(scope.get("score_scope") or "quality_claim"),
        "excluded_from_quality_claim": str(bool(scope.get("excluded_from_quality_claim"))).lower(),
        "exclusion_reason": str(scope.get("exclusion_reason") or ""),
        "label_status": status,
        "ready_for_quality_claim": str(ready).lower(),
        "score_rows_complete": str(completed_structures),
        "score_rows_expected": str(len(GOLDSET_SCORE_SPECS)),
        "missing_manual_fields": "; ".join(missing_manual_fields),
        "missing_matched_fields": "; ".join(missing_matched_fields),
        "missing_reviewer_metadata": "; ".join(missing_metadata),
        "missing_structures": "; ".join(missing_structures),
        "next_structure_checklist": " / ".join(next_structure_checklist),
        "extension": str(label_row.get("extension") or ""),
        "institution_name": institution_name,
        "filename": filename,
        "packet_path": packet_index.get(document_id, ""),
        "table_burden_score": str(table_burden),
        "attachment_temporal_burden_score": str(attachment_temporal_burden),
        "pipeline_article_count": _text_count(label_row, "pipeline_article_count"),
        "pipeline_paragraph_item_count": _text_count(label_row, "pipeline_paragraph_item_count"),
        "pipeline_appendix_form_count": _text_count(label_row, "pipeline_appendix_form_count"),
        "pipeline_table_count": _text_count(label_row, "pipeline_table_count"),
        "pipeline_nested_table_count": _text_count(label_row, "pipeline_nested_table_count"),
        "pipeline_supplementary_effective_date_count": _text_count(
            label_row,
            "pipeline_supplementary_effective_date_count",
        ),
        "pipeline_footnote_caption_count": _text_count(label_row, "pipeline_footnote_caption_count"),
        "human_progress_notes": (
            "derived matched=0 for: " + "; ".join(derived_zero_matched_fields)
            if derived_zero_matched_fields
            else ""
        ),
    }


def _best_display_text(value: str, *, fallback: str = "") -> str:
    value = _repair_utf8_as_cp949_mojibake(value)
    fallback = _repair_utf8_as_cp949_mojibake(fallback)
    if not value:
        return fallback
    if (
        fallback
        and _has_mojibake_signal(value)
        and _text_quality_score(fallback) > _text_quality_score(value)
    ):
        return fallback
    return value


def _filename_from_source_path(source_path: str) -> str:
    parts = _path_parts(source_path)
    return parts[-1] if parts else ""


def _institution_from_source_path(source_path: str) -> str:
    parts = _path_parts(source_path)
    if len(parts) < 2:
        return ""
    parent = parts[-2]
    if "_" in parent:
        prefix, institution = parent.split("_", 1)
        if prefix.startswith("C") and prefix[1:].isdigit():
            return institution
    return parent


def _path_parts(path: str) -> list[str]:
    return [part for part in str(path or "").replace("\\", "/").split("/") if part]


def _pipeline_count(row: dict[str, str], field: str) -> int:
    return optional_int(row.get(field)) or 0


def _text_count(row: dict[str, str], field: str) -> str:
    value = optional_int(row.get(field))
    return "" if value is None else str(value)


def _priority_tier(
    row: dict[str, str],
    table_burden: int,
    attachment_temporal_burden: int,
    ready: bool,
) -> str:
    if ready:
        return "completed_score_ready"
    extension = str(row.get("extension") or "").strip().lower()
    table_count = _pipeline_count(row, "pipeline_table_count")
    appendix_form_count = _pipeline_count(row, "pipeline_appendix_form_count")
    if table_count >= 50 or appendix_form_count >= 50 or table_burden >= 100:
        return "table_heavy_first"
    if attachment_temporal_burden >= 120:
        return "attachment_temporal_heavy"
    if extension in {".hwp", ".hwpx"} and table_burden >= 40:
        return "hwp_hwpx_table_probe"
    if _pipeline_count(row, "pipeline_supplementary_effective_date_count") >= 40:
        return "supplementary_temporal_probe"
    return "baseline_goldset_fill"


def _recommended_next_action(priority_tier: str, ready: bool) -> str:
    if ready:
        return "Run the goldset score command with fail-on-goldset-issue and preserve the evidence artifact."
    actions = {
        "table_heavy_first": "Fill manual and matched counts for table, nested table, and appendix/form structures first.",
        "attachment_temporal_heavy": "Review appendix/form, table, supplementary effective-date, and footnote/caption structures before count scoring.",
        "hwp_hwpx_table_probe": "Compare HWP/HWPX source tables with extracted table/form chunks before approving parser quality.",
        "supplementary_temporal_probe": "Confirm supplementary-provision and effective-date boundaries before scoring this document.",
        "baseline_goldset_fill": "Fill every manual_* and matched_* count, reviewer, reviewed_at, and final label_status.",
    }
    return actions.get(priority_tier, actions["baseline_goldset_fill"])


def _row_sort_key(row: dict[str, str]) -> tuple[int, int, int, int, str]:
    tier_order = {
        "table_heavy_first": 0,
        "attachment_temporal_heavy": 1,
        "hwp_hwpx_table_probe": 2,
        "supplementary_temporal_probe": 3,
        "baseline_goldset_fill": 4,
        "completed_score_ready": 9,
    }
    return (
        tier_order.get(row["priority_tier"], 99),
        -_safe_int(row["attachment_temporal_burden_score"]),
        -_safe_int(row["table_burden_score"]),
        _safe_int(row["review_order"]) or 999999,
        row["document_id"],
    )


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _split_fields(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _structure_completion_summary(label_rows: Sequence[dict[str, str]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    row_count = len(label_rows)
    for structure_type, spec in GOLDSET_SCORE_SPECS.items():
        manual_field = spec["manual_field"]
        pipeline_field = spec["pipeline_field"]
        match_field = spec["match_field"]
        manual_filled = sum(1 for row in label_rows if optional_int(row.get(manual_field)) is not None)
        pipeline_filled = sum(1 for row in label_rows if optional_int(row.get(pipeline_field)) is not None)
        matched_filled = sum(
            1
            for row in label_rows
            if effective_goldset_matched_count(
                manual_count=optional_int(row.get(manual_field)),
                pipeline_count=optional_int(row.get(pipeline_field)),
                matched_count=optional_int(row.get(match_field)),
            )[0]
            is not None
        )
        derived_zero_matched = sum(
            1
            for row in label_rows
            if effective_goldset_matched_count(
                manual_count=optional_int(row.get(manual_field)),
                pipeline_count=optional_int(row.get(pipeline_field)),
                matched_count=optional_int(row.get(match_field)),
            )[1]
            == "derived_zero_bound"
        )
        complete_rows = sum(
            1
            for row in label_rows
            if optional_int(row.get(manual_field)) is not None
            and optional_int(row.get(pipeline_field)) is not None
            and effective_goldset_matched_count(
                manual_count=optional_int(row.get(manual_field)),
                pipeline_count=optional_int(row.get(pipeline_field)),
                matched_count=optional_int(row.get(match_field)),
            )[0]
            is not None
        )
        summary[structure_type] = {
            "expected_document_count": row_count,
            "manual_count_filled": manual_filled,
            "pipeline_count_filled": pipeline_filled,
            "matched_count_filled": matched_filled,
            "derived_zero_matched_count": derived_zero_matched,
            "score_rows_complete": complete_rows,
            "missing_manual_count": max(row_count - manual_filled, 0),
            "missing_pipeline_count": max(row_count - pipeline_filled, 0),
            "missing_matched_count": max(row_count - matched_filled, 0),
            "pipeline_total": sum(_pipeline_count(row, pipeline_field) for row in label_rows),
            "ready_for_structure_f1": row_count > 0 and complete_rows == row_count,
        }
    return summary


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BOARD_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Parsing Goldset Completion Board",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source labels CSV: `{report['source_labels_csv']}`",
        f"- Packet directory: `{report['packet_dir']}`",
        f"- Documents: {int(report['document_count']):,}",
        f"- Ready documents: {int(report['ready_document_count']):,}",
        f"- Completed structure score rows: {int(report['completed_structure_score_rows']):,} / {int(report['expected_structure_score_rows']):,}",
        f"- Missing manual fields: {int(report['missing_manual_field_count']):,}",
        f"- Missing matched fields: {int(report['missing_matched_field_count']):,}",
        f"- Missing reviewer metadata rows: {int(report['missing_reviewer_metadata_count']):,}",
        f"- Quality-claim documents: {int(report['quality_claim_document_count']):,}",
        f"- Quality-claim completed structure rows: {int(report['quality_claim_completed_structure_score_rows']):,} / {int(report['quality_claim_expected_structure_score_rows']):,}",
        f"- Quality-claim missing matched fields: {int(report['quality_claim_missing_matched_field_count']):,}",
        f"- Excluded documents: {int(report['excluded_document_count']):,}",
        f"- Ready for quality claim: {str(report['ready_for_quality_claim']).lower()}",
        f"- Completion gate status: `{report['completion_gate_status']}`",
        "",
        "## Safety Note",
        "",
        report["safety_note"],
        "",
        "Precision/recall is not claimable until every document has reviewer metadata, completed label status, and all manual_* and matched_* counts.",
        "",
        "## Priority Tiers",
        "",
        "| Tier | Documents |",
        "| --- | ---: |",
    ]
    for tier, count in sorted(report["priority_tier_counts"].items()):
        lines.append(f"| {_md(tier)} | {int(count):,} |")
    lines.extend(
        [
            "",
            "## Structure Completion",
            "",
            "| Structure | Pipeline total | Score rows complete | Missing manual | Missing matched | Ready for F1 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for structure_type, summary in report.get("structure_completion_summary", {}).items():
        lines.append(
            f"| {_md(structure_type)} | {int(summary['pipeline_total']):,} | "
            f"{int(summary['score_rows_complete']):,}/{int(summary['expected_document_count']):,} | "
            f"{int(summary['missing_manual_count']):,} | {int(summary['missing_matched_count']):,} | "
            f"{str(summary['ready_for_structure_f1']).lower()} |"
        )
    lines.extend(
        [
            "",
            "## Documents",
            "",
            "| Rank | Tier | Document | Institution | File | Scope | Excluded | Ext | Complete | Table burden | Attachment/temporal burden | Missing manual | Missing matched | Missing structures |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['priority_rank']} | {_md(row['priority_tier'])} | {_md(row['document_id'])} | "
            f"{_md(row['institution_name'])} | {_md(row['filename'])} | "
            f"{_md(row['score_scope'])} | {_md(row['excluded_from_quality_claim'])} | "
            f"{_md(row['extension'])} | {row['score_rows_complete']}/{row['score_rows_expected']} | "
            f"{int(row['table_burden_score']):,} | {int(row['attachment_temporal_burden_score']):,} | "
            f"{len(_split_fields(row['missing_manual_fields'])):,} | {len(_split_fields(row['missing_matched_fields'])):,} | "
            f"{_md(row['missing_structures'])} |"
        )
    lines.extend(
        [
            "",
            "## Review Checklist",
            "",
            "| Rank | Document | Next action | Missing structures | Fill these fields | Packet |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['priority_rank']} | {_md(row['document_id'])} | {_md(row['recommended_next_action'])} | "
            f"{_md(row['missing_structures'])} | {_md(row['next_structure_checklist'])} | {_md(row['packet_path'])} |"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only parsing goldset completion board.")
    parser.add_argument("--labels-csv", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-csv", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--packet-dir", type=Path)
    parser.add_argument("--max-md-rows", type=int, default=30)
    parser.add_argument(
        "--fail-on-incomplete",
        action="store_true",
        help="Exit with status 2 when the completion board is not ready for a quality claim.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_parsing_goldset_completion_board(
        labels_csv=args.labels_csv,
        packet_dir=args.packet_dir,
        out_json=args.out_json,
        out_csv=args.out_csv,
        out_md=args.out_md,
        max_md_rows=args.max_md_rows,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "report_type": report["report_type"],
                "document_count": report["document_count"],
                "ready_for_quality_claim": report["ready_for_quality_claim"],
                "completion_gate_status": report["completion_gate_status"],
                "out_json": report["artifacts"]["json"],
                "out_csv": report["artifacts"]["csv"],
                "out_md": report["artifacts"]["markdown"],
            },
            ensure_ascii=False,
        )
    )
    if args.fail_on_incomplete and not report["ready_for_quality_claim"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
