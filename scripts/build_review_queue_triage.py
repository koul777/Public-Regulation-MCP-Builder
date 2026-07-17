from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CATEGORIES = (
    "table_extraction_blocker",
    "parser_uncertainty_blocker",
    "parser_uncertainty_review",
    "hwp_binary_geometry_review",
    "supplementary_effective_date_review",
    "ocr_or_encoding_blocker",
)

CATEGORY_LABEL_OPTIONS = {
    "table_extraction_blocker": "true_extraction_failure | acceptable_linearized_table | not_table | needs_parser_fix",
    "parser_uncertainty_blocker": "true_parser_blocker | acceptable_after_source_check | needs_ocr_or_reparse | needs_parser_fix",
    "parser_uncertainty_review": "source_match_confirmed | minor_uncertainty_ok | needs_source_recheck | needs_parser_fix",
    "hwp_binary_geometry_review": "real_table_geometry | form_field_layout | paragraph_false_positive | needs_parser_fix",
    "supplementary_effective_date_review": "true_effective_date_issue | inherited_date_ok | revision_context_needed | parser_rule_fix",
    "ocr_or_encoding_blocker": "true_ocr_or_encoding | false_positive | source_scan_needed",
}

CATEGORY_NEXT_ACTION = {
    "table_extraction_blocker": "Compare source table with extracted chunk and mark whether parser repair is required.",
    "parser_uncertainty_blocker": "Compare extracted text with the source file and resolve OCR/parser uncertainty before approval.",
    "parser_uncertainty_review": "Spot-check parser uncertainty flags and confirm whether bulk approval remains acceptable.",
    "hwp_binary_geometry_review": "Check whether HWP binary table/form geometry was materially lost or only conservatively flagged.",
    "supplementary_effective_date_review": "Confirm whether supplementary-provision effective dates need inheritance, revision context, or parser-rule repair.",
    "ocr_or_encoding_blocker": "Confirm whether text is unreadable, OCR is needed, or the blocker is a false positive.",
}

TRIAGE_FIELDNAMES = [
    "triage_rank",
    "review_category",
    "review_severity_rank",
    "priority_tier",
    "group_size",
    "review_group_key",
    "institution_name",
    "apba_id",
    "profile_id",
    "filename",
    "extension",
    "source_record_id",
    "source_file_id",
    "document_id",
    "chunk_id",
    "chunk_type",
    "page_start",
    "page_end",
    "table_review_flags",
    "table_classification",
    "table_review_reason",
    "table_structured_row_count",
    "table_record_count",
    "table_header_cells",
    "source_hwp_extraction_modes",
    "source_hwp_native_table_geometry",
    "source_hwpx_parser_review_flags",
    "parser_uncertainty_source",
    "parser_uncertainty_risk_level",
    "parser_uncertainty_confidence",
    "parser_uncertainty_flags",
    "parser_uncertainty_recommendation",
    "chunk_artifact",
    "review_reason",
    "review_step",
    "label_options",
    "suggested_next_action",
    "human_label",
    "human_notes",
    "snippet",
]


def safe_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def load_review_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def review_group_key(row: dict[str, str]) -> str:
    explicit = (row.get("review_group_key") or "").strip()
    if explicit:
        return explicit
    return "|".join(
        [
            row.get("review_category") or "",
            row.get("document_id") or "",
            row.get("chunk_id") or "",
        ]
    )


def primary_group_row(rows: list[dict[str, str]]) -> dict[str, str]:
    for row in rows:
        if str(row.get("review_group_primary") or "").lower() == "true":
            return row
    return rows[0]


def group_size(rows: list[dict[str, str]]) -> int:
    declared = max((safe_int(row.get("review_group_duplicate_count")) for row in rows), default=0)
    return max(declared, len(rows))


def build_triage_rows(
    rows: list[dict[str, str]],
    *,
    categories: list[str],
    max_per_category: int,
) -> list[dict[str, str]]:
    category_order = {category: index for index, category in enumerate(categories)}
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        category = row.get("review_category") or ""
        if category not in category_order:
            continue
        grouped[review_group_key(row)].append(row)

    group_records: list[tuple[dict[str, str], int]] = []
    for group_rows in grouped.values():
        primary = primary_group_row(group_rows)
        group_records.append((primary, group_size(group_rows)))

    group_records.sort(
        key=lambda item: (
            category_order.get(item[0].get("review_category") or "", 999),
            safe_int(item[0].get("review_severity_rank")),
            -item[1],
            item[0].get("institution_name") or "",
            item[0].get("filename") or "",
            item[0].get("chunk_id") or "",
        )
    )

    selected: list[dict[str, str]] = []
    selected_keys: set[str] = set()

    def append_row(primary: dict[str, str], size: int) -> None:
        category = primary.get("review_category") or ""
        row = {field: primary.get(field, "") for field in TRIAGE_FIELDNAMES}
        row["triage_rank"] = str(len(selected) + 1)
        row["group_size"] = str(size)
        row["label_options"] = CATEGORY_LABEL_OPTIONS.get(category, "")
        row["suggested_next_action"] = CATEGORY_NEXT_ACTION.get(category, "")
        row["human_label"] = ""
        row["human_notes"] = ""
        selected.append(row)
        selected_keys.add(review_group_key(primary))

    for category in categories:
        category_records = [
            (primary, size)
            for primary, size in group_records
            if (primary.get("review_category") or "") == category
        ]
        used_documents: set[str] = set()
        used_institutions: set[str] = set()
        selected_for_category = 0

        for primary, size in category_records:
            if selected_for_category >= max_per_category:
                break
            key = review_group_key(primary)
            if key in selected_keys:
                continue
            document_id = primary.get("document_id") or ""
            institution = primary.get("institution_name") or ""
            if document_id and document_id in used_documents:
                continue
            if institution and institution in used_institutions:
                continue
            append_row(primary, size)
            selected_for_category += 1
            if document_id:
                used_documents.add(document_id)
            if institution:
                used_institutions.add(institution)

        for primary, size in category_records:
            if selected_for_category >= max_per_category:
                break
            key = review_group_key(primary)
            if key in selected_keys:
                continue
            append_row(primary, size)
            selected_for_category += 1
    return selected


def write_triage_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRIAGE_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_triage_markdown(
    *,
    source_csv: Path,
    rows: list[dict[str, str]],
    categories: list[str],
    max_per_category: int,
    generated_at: str,
) -> str:
    category_counts = Counter(row.get("review_category") or "" for row in rows)
    lines = [
        "# Review Queue Triage Packet",
        "",
        f"- Generated at: {generated_at}",
        f"- Source CSV: `{source_csv}`",
        f"- Selected groups: {len(rows):,}",
        f"- Max per category: {max_per_category:,}",
        "",
        "## Scope",
        "",
        "This packet does not approve or downgrade any regulation content. It selects representative review groups so a human reviewer can label blocker patterns before parser or queue-rule changes.",
        "",
        "## Category Summary",
        "",
        "| Category | Selected groups | Label options |",
        "| --- | ---: | --- |",
    ]
    for category in categories:
        lines.append(
            f"| {category} | {category_counts.get(category, 0):,} | {markdown_cell(CATEGORY_LABEL_OPTIONS.get(category, ''))} |"
        )

    lines.extend(
        [
            "",
            "## Selected Groups",
            "",
            "| Rank | Category | Group | Institution | File | Chunk | Flags | Snippet |",
            "| ---: | --- | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['triage_rank']} | {markdown_cell(row['review_category'])} | {markdown_cell(row['group_size'])} | "
            f"{markdown_cell(row['institution_name'])} | {markdown_cell(row['filename'])} | "
            f"{markdown_cell(row['chunk_id'])} | {markdown_cell(row['table_review_flags'] or row['review_reason'])} | "
            f"{markdown_cell(row['snippet'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_triage_packet(
    *,
    review_csv: Path,
    out_csv: Path,
    out_md: Path,
    categories: list[str],
    max_per_category: int,
    generated_at: str | None = None,
) -> dict[str, Path]:
    if max_per_category <= 0:
        raise ValueError("--max-per-category must be greater than zero.")
    generated_at = generated_at or datetime.now().strftime("%Y%m%d-%H%M%S")
    rows = load_review_rows(review_csv)
    triage_rows = build_triage_rows(rows, categories=categories, max_per_category=max_per_category)
    write_triage_csv(out_csv, triage_rows)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(
        make_triage_markdown(
            source_csv=review_csv,
            rows=triage_rows,
            categories=categories,
            max_per_category=max_per_category,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )
    return {"csv": out_csv, "markdown": out_md}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a grouped human-labeling packet from a parsing review queue CSV.")
    parser.add_argument("--review-csv", required=True, help="Input parsing_review_queue_*.csv file.")
    parser.add_argument("--out-csv", required=True, help="Output triage CSV with human label columns.")
    parser.add_argument("--out-md", required=True, help="Output triage Markdown summary.")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Review category to include. Repeat to include multiple categories. Defaults to table/parser/HWP/temporal/OCR blockers.",
    )
    parser.add_argument("--max-per-category", type=int, default=20)
    parser.add_argument("--timestamp", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    categories = args.category or list(DEFAULT_CATEGORIES)
    outputs = build_triage_packet(
        review_csv=Path(args.review_csv),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        categories=categories,
        max_per_category=args.max_per_category,
        generated_at=args.timestamp,
    )
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
