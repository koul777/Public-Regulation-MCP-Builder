from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFETY_NOTE = (
    "This report summarizes human review labels only; it does not approve chunks, "
    "change approval status, or write VectorDB artifacts."
)


def load_triage_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def summarize_review_triage_labels(
    rows: list[dict[str, str]],
    *,
    source_csv: Path | None = None,
    generated_at: str | None = None,
    action_limit: int = 5,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    category_state: dict[str, dict[str, Any]] = {}
    label_counts: Counter[str] = Counter()
    group_size_by_label: Counter[str] = Counter()
    labeled_count = 0
    unlabeled_count = 0
    total_group_size = 0
    labeled_group_size = 0
    unlabeled_group_size = 0
    human_note_count = 0

    for row in rows:
        category = clean_text(row.get("review_category")) or "uncategorized"
        label = clean_text(row.get("human_label"))
        group_size = parse_group_size(row.get("group_size"))
        total_group_size += group_size
        state = category_state.setdefault(
            category,
            {
                "review_category": category,
                "row_count": 0,
                "labeled_count": 0,
                "unlabeled_count": 0,
                "total_group_size": 0,
                "labeled_group_size": 0,
                "unlabeled_group_size": 0,
                "human_label_counts": Counter(),
                "group_size_by_label": Counter(),
                "document_ids": set(),
                "institution_names": set(),
                "label_options": set(),
            },
        )
        state["row_count"] += 1
        state["total_group_size"] += group_size
        add_if_present(state["document_ids"], row.get("document_id"))
        add_if_present(state["institution_names"], row.get("institution_name"))
        add_if_present(state["label_options"], row.get("label_options"))

        if label:
            labeled_count += 1
            labeled_group_size += group_size
            label_counts[label] += 1
            group_size_by_label[label] += group_size
            state["labeled_count"] += 1
            state["labeled_group_size"] += group_size
            state["human_label_counts"][label] += 1
            state["group_size_by_label"][label] += group_size
            if clean_text(row.get("human_notes")):
                human_note_count += 1
        else:
            unlabeled_count += 1
            unlabeled_group_size += group_size
            state["unlabeled_count"] += 1
            state["unlabeled_group_size"] += group_size

    category_summaries = [
        serialize_category_summary(state)
        for _, state in sorted(category_state.items(), key=lambda item: item[0])
    ]
    report: dict[str, Any] = {
        "report_type": "review_triage_label_summary",
        "generated_at": generated_at,
        "source_csv": str(source_csv) if source_csv is not None else None,
        "row_count": len(rows),
        "labeled_count": labeled_count,
        "unlabeled_count": unlabeled_count,
        "total_group_size": total_group_size,
        "labeled_group_size": labeled_group_size,
        "unlabeled_group_size": unlabeled_group_size,
        "human_note_count": human_note_count,
        "human_label_counts": sorted_counter(label_counts),
        "group_size_by_label": sorted_counter(group_size_by_label),
        "category_label_counts": {
            summary["review_category"]: summary["human_label_counts"]
            for summary in category_summaries
        },
        "category_summaries": category_summaries,
        "safety_note": SAFETY_NOTE,
    }
    report["next_recommended_actions"] = next_recommended_actions(report, limit=action_limit)
    return report


def build_review_triage_label_summary(
    *,
    triage_csv: Path,
    out_json: Path,
    out_md: Path,
    generated_at: str | None = None,
    action_limit: int = 5,
) -> dict[str, Any]:
    rows = load_triage_rows(triage_csv)
    report = summarize_review_triage_labels(
        rows,
        source_csv=triage_csv,
        generated_at=generated_at,
        action_limit=action_limit,
    )
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, out_md)
    return report


def serialize_category_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_category": state["review_category"],
        "row_count": state["row_count"],
        "labeled_count": state["labeled_count"],
        "unlabeled_count": state["unlabeled_count"],
        "total_group_size": state["total_group_size"],
        "labeled_group_size": state["labeled_group_size"],
        "unlabeled_group_size": state["unlabeled_group_size"],
        "human_label_counts": sorted_counter(state["human_label_counts"]),
        "group_size_by_label": sorted_counter(state["group_size_by_label"]),
        "document_count": len(state["document_ids"]),
        "institution_count": len(state["institution_names"]),
        "label_options": sorted(state["label_options"]),
    }


def next_recommended_actions(report: dict[str, Any], *, limit: int = 5) -> list[str]:
    if limit <= 0:
        return []

    actions: list[str] = []
    if report["unlabeled_count"]:
        actions.append(
            f"Complete {report['unlabeled_count']} unlabeled triage group(s) before changing parser rules or approval gates."
        )
        top_unlabeled = top_category_by(report["category_summaries"], "unlabeled_group_size")
        if top_unlabeled:
            actions.append(
                "Start remaining labeling in "
                f"`{top_unlabeled['review_category']}` "
                f"({top_unlabeled['unlabeled_count']} unlabeled group(s), "
                f"group_size {top_unlabeled['unlabeled_group_size']})."
            )

    if report["human_label_counts"]:
        label, group_size = top_counter_item(report["group_size_by_label"])
        label_count = report["human_label_counts"].get(label, 0)
        actions.append(
            f"Prioritize remediation planning for `{label}` because it covers "
            f"{group_size} queued item(s) across {label_count} labeled group(s)."
        )
        if report["human_note_count"]:
            actions.append(
                f"Review {report['human_note_count']} human note(s) for parser uncertainty and edge cases before closing triage items."
            )
    else:
        actions.append("Collect at least one completed human_label before using this report for remediation planning.")

    actions.append("Keep approval and VectorDB indexing separate; this report is label analytics only.")
    return actions[:limit]


def top_category_by(category_summaries: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    candidates = [summary for summary in category_summaries if int(summary.get(key) or 0) > 0]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (-int(item.get(key) or 0), item["review_category"]))[0]


def top_counter_item(values: dict[str, int]) -> tuple[str, int]:
    return sorted(values.items(), key=lambda item: (-int(item[1]), item[0]))[0]


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Review Triage Label Summary",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Source CSV: `{report.get('source_csv')}`",
        f"- Triage groups: {report.get('row_count')}",
        f"- Labeled groups: {report.get('labeled_count')}",
        f"- Unlabeled groups: {report.get('unlabeled_count')}",
        f"- Total group_size: {report.get('total_group_size')}",
        f"- Unlabeled group_size: {report.get('unlabeled_group_size')}",
        f"- Safety: {report.get('safety_note')}",
        "",
        "## Category Summary",
        "",
        "| Category | Rows | Labeled | Unlabeled | Group size | Documents | Institutions | Label options |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for summary in report.get("category_summaries") or []:
        lines.append(
            "| {category} | {rows} | {labeled} | {unlabeled} | {group_size} | {docs} | {institutions} | {options} |".format(
                category=markdown_cell(summary.get("review_category")),
                rows=summary.get("row_count"),
                labeled=summary.get("labeled_count"),
                unlabeled=summary.get("unlabeled_count"),
                group_size=summary.get("total_group_size"),
                docs=summary.get("document_count"),
                institutions=summary.get("institution_count"),
                options=markdown_cell("; ".join(summary.get("label_options") or [])),
            )
        )

    lines.extend(
        [
            "",
            "## Category And Label Counts",
            "",
            "| Category | Human label | Groups | Total group_size |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for summary in report.get("category_summaries") or []:
        category = summary.get("review_category")
        for label, count in (summary.get("human_label_counts") or {}).items():
            group_size = (summary.get("group_size_by_label") or {}).get(label, 0)
            lines.append(
                f"| {markdown_cell(category)} | {markdown_cell(label)} | {count} | {group_size} |"
            )
        if summary.get("unlabeled_count"):
            lines.append(
                "| {category} | (unlabeled) | {count} | {group_size} |".format(
                    category=markdown_cell(category),
                    count=summary.get("unlabeled_count"),
                    group_size=summary.get("unlabeled_group_size"),
                )
            )

    lines.extend(["", "## Group Size By Label", ""])
    if report.get("group_size_by_label"):
        for label, group_size in (report.get("group_size_by_label") or {}).items():
            lines.append(f"- {markdown_cell(label)}: {group_size}")
    else:
        lines.append("- No completed human labels.")

    lines.extend(["", "## Next Recommended Actions", ""])
    for action in report.get("next_recommended_actions") or []:
        lines.append(f"- {markdown_cell(action)}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in sorted(counter.items(), key=lambda item: (-int(item[1]), item[0]))
    }


def add_if_present(values: set[str], value: Any) -> None:
    text = clean_text(value)
    if text:
        values.add(text)


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def parse_group_size(value: Any) -> int:
    text = clean_text(value)
    if not text:
        return 1
    try:
        return max(int(float(text)), 0)
    except ValueError:
        return 0


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize completed human labels from a review queue triage CSV.")
    parser.add_argument("--triage-csv", required=True, help="Input review queue triage CSV with human_label columns.")
    parser.add_argument("--out-json", required=True, help="Output JSON summary path.")
    parser.add_argument("--out-md", required=True, help="Output Markdown summary path.")
    parser.add_argument("--timestamp", default=None, help="Optional generated_at value for reproducible reports.")
    parser.add_argument("--action-limit", type=int, default=5, help="Maximum recommended actions to include.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = build_review_triage_label_summary(
            triage_csv=Path(args.triage_csv),
            out_json=Path(args.out_json),
            out_md=Path(args.out_md),
            generated_at=args.timestamp,
            action_limit=args.action_limit,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "labeled_count": report["labeled_count"],
                "unlabeled_count": report["unlabeled_count"],
                "out_json": str(Path(args.out_json)),
                "out_md": str(Path(args.out_md)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
