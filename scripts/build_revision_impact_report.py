import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHANGE_ORDER = {"changed": 0, "added": 1, "removed": 2, "unchanged": 3}
REVIEW_ACTIONS = {
    "changed": "review_changed_unit_before_reindex",
    "added": "review_and_approve_new_unit",
    "removed": "confirm_deindex_removed_unit",
    "unchanged": "reuse_previous_approval_candidate",
}
TRACKED_METADATA_FIELDS = (
    "tenant_id",
    "department_id",
    "department_ids",
    "department_acl",
    "security_level",
    "approval_status",
    "revision_date",
    "effective_date",
    "valid_from",
    "valid_to",
    "revision_events",
    "revision_history",
    "article_effective_overrides",
    "article_validity_windows",
    "is_supplementary_provision",
    "supplementary_identifier_date",
)
SOURCE_METADATA_FIELDS = (
    "institution_name",
    "apba_id",
    "profile_id",
    "source_system",
    "source_record_id",
    "source_file_id",
)


def load_chunks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Chunk file must contain a JSON array: {path}")
    return [row for row in payload if isinstance(row, dict)]


def metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    value = chunk.get("metadata")
    return value if isinstance(value, dict) else {}


def chunk_value(chunk: dict[str, Any], key: str, default: Any = "") -> Any:
    value = chunk.get(key)
    if value not in (None, ""):
        return value
    return metadata(chunk).get(key, default)


def normalized_text(value: Any) -> str:
    text = str(value or "")
    return re.sub(r"\s+", " ", text).strip()


def key_part(value: Any) -> str:
    return normalized_text(value).lower().replace(" ", "_")


def unit_identity(chunk: dict[str, Any], index: int) -> tuple[str, str]:
    chunk_type = str(chunk_value(chunk, "chunk_type", "unknown") or "unknown").lower()
    regulation_scope = regulation_scope_key(chunk)
    article_no = chunk_value(chunk, "article_no")
    if article_no:
        article_key = key_part(article_no)
        scoped_article_key = f"{regulation_scope}:{article_key}" if regulation_scope else article_key
        if is_supplementary_chunk(chunk, chunk_type):
            scope = supplementary_scope_key(chunk)
            scoped_supplementary = f"{regulation_scope}:{scope}" if regulation_scope else scope
            return f"supplementary_article:{scoped_supplementary}:{article_key}", "supplementary_article"
        return f"article:{scoped_article_key}", "article"

    table_id = chunk_value(chunk, "table_id")
    if table_id:
        return f"table:{scoped_unit_key(regulation_scope, table_id)}", "table"

    for field, unit_type in (
        ("appendix_no", "appendix"),
        ("table_appendix_no", "appendix"),
        ("form_no", "form"),
        ("table_form_no", "form"),
    ):
        value = chunk_value(chunk, field)
        if value:
            return f"{unit_type}:{scoped_unit_key(regulation_scope, value)}", unit_type

    title = chunk_value(chunk, "article_title") or chunk_value(chunk, "table_title")
    if chunk_type in {"appendix", "form", "table"} and title:
        return f"{chunk_type}:{scoped_unit_key(regulation_scope, title)}", chunk_type

    return f"{chunk_type}:ordinal:{index:05d}", chunk_type


def scoped_unit_key(regulation_scope: str, value: Any) -> str:
    unit_key = key_part(value)
    return f"{regulation_scope}:{unit_key}" if regulation_scope else unit_key


def is_supplementary_chunk(chunk: dict[str, Any], chunk_type: str | None = None) -> bool:
    normalized_type = (chunk_type or str(chunk_value(chunk, "chunk_type", "") or "")).lower()
    return normalized_type in {"supplementary", "supplementary_provision"} or bool(
        chunk_value(chunk, "is_supplementary_provision")
    )


def supplementary_scope_key(chunk: dict[str, Any]) -> str:
    for field in (
        "supplementary_identifier_date",
        "supplementary_label",
        "regulation_title",
        "document_name",
        "article_title",
    ):
        value = chunk_value(chunk, field)
        if value:
            return key_part(value)
    return "unspecified"


def regulation_scope_key(chunk: dict[str, Any]) -> str:
    for field in ("regulation_no", "regulation_title"):
        value = chunk_value(chunk, field)
        if value:
            return key_part(value)
    return ""


def build_units(chunks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for index, chunk in enumerate(chunks):
        key, unit_type = unit_identity(chunk, index)
        unit = grouped.setdefault(
            key,
            {
                "key": key,
                "unit_type": unit_type,
                "chunk_ids": [],
                "article_no": chunk_value(chunk, "article_no"),
                "article_title": chunk_value(chunk, "article_title"),
                "table_id": chunk_value(chunk, "table_id"),
                "table_title": chunk_value(chunk, "table_title"),
                "page_start": chunk_value(chunk, "source_page_start"),
                "page_end": chunk_value(chunk, "source_page_end"),
                "_sort_values": [],
                "_text_parts": [],
                "_tracked_metadata": {field: [] for field in TRACKED_METADATA_FIELDS},
                "_source_metadata": {field: [] for field in SOURCE_METADATA_FIELDS},
            },
        )
        chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")
        text_part = str(chunk.get("normalized_text") or chunk.get("retrieval_text") or chunk.get("text") or "")
        sort_value = (
            _page_number(chunk_value(chunk, "source_page_start")),
            chunk_id,
            index,
        )
        unit["chunk_ids"].append(chunk_id)
        unit["_text_parts"].append(text_part)
        unit["_sort_values"].append(sort_value)
        for field in TRACKED_METADATA_FIELDS:
            unit["_tracked_metadata"][field].append(chunk_value(chunk, field))
        for field in SOURCE_METADATA_FIELDS:
            unit["_source_metadata"][field].append(chunk_value(chunk, field))
        unit["page_start"] = _min_page(unit.get("page_start"), chunk_value(chunk, "source_page_start"))
        unit["page_end"] = _max_page(unit.get("page_end"), chunk_value(chunk, "source_page_end"))
    for unit in grouped.values():
        ordered_parts = sorted(
            zip(unit.pop("_sort_values"), unit["chunk_ids"], unit.pop("_text_parts")),
            key=lambda item: item[0],
        )
        unit["chunk_ids"] = [chunk_id for _, chunk_id, _ in ordered_parts]
        combined_text = normalized_text(" ".join(text for _, _, text in ordered_parts))
        unit["text"] = combined_text
        unit["content_hash"] = stable_unit_hash(unit)
        unit["tracked_metadata"] = compact_tracked_metadata(unit.pop("_tracked_metadata", {}))
        unit["source_metadata"] = compact_metadata_fields(unit.pop("_source_metadata", {}), SOURCE_METADATA_FIELDS)
        unit["metadata_hash"] = stable_metadata_hash(unit)
        unit["snippet"] = combined_text[:240]
        unit["chunk_count"] = len(unit["chunk_ids"])
    return grouped


def _page_number(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _min_page(left: Any, right: Any) -> Any:
    left_num = _page_number(left)
    right_num = _page_number(right)
    if not left_num:
        return right
    if not right_num:
        return left
    return min(left_num, right_num)


def _max_page(left: Any, right: Any) -> Any:
    return max(_page_number(left), _page_number(right)) or left or right


def stable_unit_hash(unit: dict[str, Any]) -> str:
    payload = {
        "key": unit.get("key"),
        "unit_type": unit.get("unit_type"),
        "article_no": normalized_text(unit.get("article_no")),
        "article_title": normalized_text(unit.get("article_title")),
        "table_id": normalized_text(unit.get("table_id")),
        "table_title": normalized_text(unit.get("table_title")),
        "text": normalized_text(unit.get("text")),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compact_tracked_metadata(values_by_field: dict[str, list[Any]]) -> dict[str, Any]:
    return compact_metadata_fields(values_by_field, TRACKED_METADATA_FIELDS)


def compact_metadata_fields(values_by_field: dict[str, list[Any]], fields: tuple[str, ...]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for field in fields:
        values = []
        seen = set()
        for raw_value in values_by_field.get(field, []):
            normalized = normalize_metadata_value(raw_value)
            if normalized is None:
                continue
            encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if encoded in seen:
                continue
            seen.add(encoded)
            values.append(normalized)
        if not values:
            continue
        compact[field] = values[0] if len(values) == 1 else values
    return compact


def normalize_metadata_value(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        text = normalized_text(value)
        return text or None
    if isinstance(value, dict):
        normalized = {
            str(key): normalize_metadata_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
        return {key: item for key, item in normalized.items() if item is not None} or None
    if isinstance(value, list):
        normalized_items = [normalize_metadata_value(item) for item in value]
        return [item for item in normalized_items if item is not None] or None
    return value


def stable_metadata_hash(unit: dict[str, Any]) -> str:
    payload = {
        "key": unit.get("key"),
        "tracked_metadata": unit.get("tracked_metadata") or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compare_chunk_units(
    before_chunks: list[dict[str, Any]],
    after_chunks: list[dict[str, Any]],
    *,
    before_label: str,
    after_label: str,
) -> dict[str, Any]:
    before_units = build_units(before_chunks)
    after_units = build_units(after_chunks)
    before_keys = set(before_units)
    after_keys = set(after_units)
    changed = [
        impact_row("changed", key, before_units[key], after_units[key])
        for key in sorted(before_keys & after_keys)
        if _unit_changed(before_units[key], after_units[key])
    ]
    unchanged = [
        impact_row("unchanged", key, before_units[key], after_units[key])
        for key in sorted(before_keys & after_keys)
        if not _unit_changed(before_units[key], after_units[key])
    ]
    added = [impact_row("added", key, None, after_units[key]) for key in sorted(after_keys - before_keys)]
    removed = [impact_row("removed", key, before_units[key], None) for key in sorted(before_keys - after_keys)]
    review_queue = sorted(
        [*changed, *added, *removed],
        key=lambda row: (CHANGE_ORDER[row["change_type"]], row["unit_type"], row["key"]),
    )
    return {
        "report_type": "revision_impact",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "before_label": before_label,
        "after_label": after_label,
        "summary": {
            "before_unit_count": len(before_units),
            "after_unit_count": len(after_units),
            "changed_count": len(changed),
            "added_count": len(added),
            "removed_count": len(removed),
            "unchanged_count": len(unchanged),
            "metadata_only_changed_count": sum(1 for row in changed if row.get("metadata_only_change")),
            "approval_required_count": len(review_queue),
            "approval_reuse_candidate_count": len(unchanged),
            "deindex_required_count": len(removed),
        },
        "review_queue": review_queue,
        "changed": changed,
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
    }


def _unit_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before["content_hash"] != after["content_hash"] or before["metadata_hash"] != after["metadata_hash"]


def impact_row(
    change_type: str,
    key: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    source = after or before or {}
    metadata_change_fields = metadata_diff_fields(before, after) if before and after else []
    metadata_only_change = bool(
        before
        and after
        and before.get("content_hash") == after.get("content_hash")
        and before.get("metadata_hash") != after.get("metadata_hash")
    )
    return {
        "change_type": change_type,
        "key": key,
        "unit_type": source.get("unit_type"),
        "review_action": REVIEW_ACTIONS[change_type],
        "approval_required": change_type in {"changed", "added", "removed"},
        "approval_reuse_candidate": change_type == "unchanged",
        "institution_name": unit_source_value(source, "institution_name"),
        "apba_id": unit_source_value(source, "apba_id"),
        "profile_id": unit_source_value(source, "profile_id"),
        "source_system": unit_source_value(source, "source_system"),
        "before_source_record_id": unit_source_value(before, "source_record_id"),
        "after_source_record_id": unit_source_value(after, "source_record_id"),
        "before_source_file_id": unit_source_value(before, "source_file_id"),
        "after_source_file_id": unit_source_value(after, "source_file_id"),
        "before_content_hash": before.get("content_hash") if before else "",
        "after_content_hash": after.get("content_hash") if after else "",
        "before_metadata_hash": before.get("metadata_hash") if before else "",
        "after_metadata_hash": after.get("metadata_hash") if after else "",
        "metadata_change_fields": metadata_change_fields,
        "metadata_only_change": metadata_only_change,
        "before_chunk_ids": ";".join(before.get("chunk_ids", [])) if before else "",
        "after_chunk_ids": ";".join(after.get("chunk_ids", [])) if after else "",
        "article_no": source.get("article_no") or "",
        "article_title": source.get("article_title") or "",
        "table_id": source.get("table_id") or "",
        "table_title": source.get("table_title") or "",
        "page_start": source.get("page_start") or "",
        "page_end": source.get("page_end") or "",
        "before_snippet": before.get("snippet", "") if before else "",
        "after_snippet": after.get("snippet", "") if after else "",
    }


def unit_source_value(unit: dict[str, Any] | None, field: str) -> str:
    if not unit:
        return ""
    value = (unit.get("source_metadata") or {}).get(field)
    if isinstance(value, list):
        return ";".join(str(item) for item in value if item not in (None, ""))
    return str(value or "")


def metadata_diff_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    before_metadata = before.get("tracked_metadata") or {}
    after_metadata = after.get("tracked_metadata") or {}
    changed = []
    for field in TRACKED_METADATA_FIELDS:
        if before_metadata.get(field) != after_metadata.get(field):
            changed.append(field)
    return changed


def make_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# Revision Impact Report",
        "",
        f"- Before: `{result['before_label']}`",
        f"- After: `{result['after_label']}`",
        f"- Changed: {summary['changed_count']}",
        f"- Added: {summary['added_count']}",
        f"- Removed: {summary['removed_count']}",
        f"- Unchanged: {summary['unchanged_count']}",
        f"- Metadata-only changed: {summary.get('metadata_only_changed_count', 0)}",
        f"- Approval required: {summary['approval_required_count']}",
        f"- Approval reuse candidates: {summary['approval_reuse_candidate_count']}",
        "",
        "## Review Queue",
        "",
        "Changed, added, and removed units should be reviewed before the approved vector index is updated. Unchanged units are approval-reuse candidates.",
        "",
        "| Change | Unit | Key | Action | Source record | Source file | Article | Metadata changes | Before hash | After hash | After snippet |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not result["review_queue"]:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    for row in result["review_queue"]:
        article = " ".join(value for value in [str(row.get("article_no") or ""), str(row.get("article_title") or "")] if value)
        metadata_fields = ", ".join(row.get("metadata_change_fields") or [])
        source_record = before_after_text(row.get("before_source_record_id"), row.get("after_source_record_id"))
        source_file = before_after_text(row.get("before_source_file_id"), row.get("after_source_file_id"))
        lines.append(
            f"| {cell(row.get('change_type'))} | {cell(row.get('unit_type'))} | {cell(row.get('key'))} | "
            f"{cell(row.get('review_action'))} | {cell(source_record)} | {cell(source_file)} | "
            f"{cell(article)} | {cell(metadata_fields)} | {cell(short_hash(row.get('before_content_hash')))} | "
            f"{cell(short_hash(row.get('after_content_hash')))} | {cell(row.get('after_snippet') or row.get('before_snippet'))} |"
        )
    lines.extend(["", "## Operational Meaning", ""])
    lines.append("- `changed` and `added`: send only those units to human review and approval before reindexing.")
    lines.append("- `removed`: confirm removal, then deindex the prior approved vector records.")
    lines.append("- `unchanged`: candidate for previous approval reuse if tenant/security metadata also matches.")
    return "\n".join(lines) + "\n"


def cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def short_hash(value: Any) -> str:
    text = str(value or "")
    return text[:12] if text else ""


def before_after_text(before: Any, after: Any) -> str:
    before_text = str(before or "")
    after_text = str(after or "")
    if before_text and after_text and before_text != after_text:
        return f"{before_text} -> {after_text}"
    return after_text or before_text


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "change_type",
        "unit_type",
        "key",
        "review_action",
        "approval_required",
        "approval_reuse_candidate",
        "institution_name",
        "apba_id",
        "profile_id",
        "source_system",
        "before_source_record_id",
        "after_source_record_id",
        "before_source_file_id",
        "after_source_file_id",
        "article_no",
        "article_title",
        "table_id",
        "table_title",
        "page_start",
        "page_end",
        "before_content_hash",
        "after_content_hash",
        "before_metadata_hash",
        "after_metadata_hash",
        "metadata_change_fields",
        "metadata_only_change",
        "before_chunk_ids",
        "after_chunk_ids",
        "before_snippet",
        "after_snippet",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["metadata_change_fields"] = ";".join(row.get("metadata_change_fields") or [])
            writer.writerow(row)


def write_revision_impact_report(
    before_path: Path,
    after_path: Path,
    out_prefix: Path,
) -> dict[str, Path]:
    result = compare_chunk_units(
        load_chunks(before_path),
        load_chunks(after_path),
        before_label=str(before_path),
        after_label=str(after_path),
    )
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    csv_path = out_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(make_markdown(result), encoding="utf-8")
    write_csv(csv_path, result["review_queue"])
    return {"json": json_path, "markdown": md_path, "csv": csv_path}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a unit-level revision impact report from two chunk JSON files.")
    parser.add_argument("--before-chunks", required=True)
    parser.add_argument("--after-chunks", required=True)
    parser.add_argument("--out-prefix", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    outputs = write_revision_impact_report(
        Path(args.before_chunks),
        Path(args.after_chunks),
        Path(args.out_prefix),
    )
    print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
