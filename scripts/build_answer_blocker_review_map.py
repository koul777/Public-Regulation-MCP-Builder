from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


MAP_FIELDNAMES = [
    "rank",
    "query",
    "quality_issue_count",
    "quality_issue_codes",
    "missing_terms",
    "missing_article_nos",
    "missing_article_titles",
    "citation_chunk_ids",
    "citation_articles",
    "citation_pages",
    "blocker_category",
    "recommended_artifacts",
    "recommended_next_action",
    "human_resolution",
    "human_notes",
]


def build_answer_blocker_review_map(
    *,
    demo_answers_json: Path,
    out_json: Path,
    out_csv: Path,
    out_md: Path,
    table_unit_review_csv: Path | None = None,
    table_source_traceability_report: Path | None = None,
    table_risk_csv: Path | None = None,
    review_triage_csv: Path | None = None,
    query_spec_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    demo = _load_json(demo_answers_json)
    items = [item for item in demo.get("items") or [] if isinstance(item, dict)]
    failed_items = [item for item in items if not item.get("passed") or int(item.get("quality_issue_count") or 0) > 0]
    rows = [
        _row(
            item,
            rank=index,
            table_unit_review_csv=table_unit_review_csv,
            table_source_traceability_report=table_source_traceability_report,
            table_risk_csv=table_risk_csv,
            review_triage_csv=review_triage_csv,
            query_spec_path=query_spec_path,
        )
        for index, item in enumerate(failed_items, start=1)
    ]

    issue_code_counts = Counter(
        code for row in rows for code in _split_joined(row["quality_issue_codes"])
    )
    blocker_category_counts = Counter(row["blocker_category"] for row in rows)
    report = {
        "report_type": "answer_blocker_review_map",
        "generated_at": generated_at,
        "source_demo_answers": str(demo_answers_json),
        "query_count": len(items),
        "failed_query_count": len(failed_items),
        "quality_issue_count": sum(int(row["quality_issue_count"] or 0) for row in rows),
        "issue_code_counts": dict(issue_code_counts),
        "blocker_category_counts": dict(blocker_category_counts),
        "artifacts": {
            "json": str(out_json),
            "csv": str(out_csv),
            "markdown": str(out_md),
        },
        "input_artifacts": {
            "table_unit_review_csv": str(table_unit_review_csv) if table_unit_review_csv else "",
            "table_source_traceability_report": (
                str(table_source_traceability_report) if table_source_traceability_report else ""
            ),
            "table_risk_csv": str(table_risk_csv) if table_risk_csv else "",
            "review_triage_csv": str(review_triage_csv) if review_triage_csv else "",
            "query_spec_path": str(query_spec_path) if query_spec_path else "",
        },
        "safety_note": (
            "This blocker map is read-only. It does not approve chunks, edit query specs, "
            "acknowledge review flags, index vectors, or publish MCP evidence."
        ),
    }

    _write_csv(out_csv, rows)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_markdown(report, rows), encoding="utf-8")
    return report


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("demo_answers_json must contain a JSON object.")
    return payload


def _row(
    item: dict[str, Any],
    *,
    rank: int,
    table_unit_review_csv: Path | None,
    table_source_traceability_report: Path | None,
    table_risk_csv: Path | None,
    review_triage_csv: Path | None,
    query_spec_path: Path | None,
) -> dict[str, str]:
    issues = [issue for issue in item.get("quality_issues") or [] if isinstance(issue, dict)]
    codes = _ordered_unique(issue.get("code") for issue in issues)
    missing_terms = _ordered_unique(term for issue in issues for term in issue.get("missing_terms") or [])
    missing_article_nos = _ordered_unique(
        value
        for issue in issues
        if issue.get("code") == "expected-article-no-missing"
        for value in issue.get("missing_values") or []
    )
    missing_article_titles = _ordered_unique(
        value
        for issue in issues
        if issue.get("code") == "expected-article-title-missing"
        for value in issue.get("missing_values") or []
    )
    citations = [citation for citation in item.get("citations") or [] if isinstance(citation, dict)]
    category = _classify(item, missing_terms, missing_article_nos, missing_article_titles, citations)
    return {
        "rank": str(rank),
        "query": str(item.get("query") or ""),
        "quality_issue_count": str(item.get("quality_issue_count") or len(issues)),
        "quality_issue_codes": "; ".join(codes),
        "missing_terms": "; ".join(missing_terms),
        "missing_article_nos": "; ".join(missing_article_nos),
        "missing_article_titles": "; ".join(missing_article_titles),
        "citation_chunk_ids": "; ".join(str(citation.get("chunk_id") or "") for citation in citations[:8]),
        "citation_articles": "; ".join(
            _citation_article(citation) for citation in citations[:8] if _citation_article(citation)
        ),
        "citation_pages": "; ".join(_citation_page(citation) for citation in citations[:8] if _citation_page(citation)),
        "blocker_category": category,
        "recommended_artifacts": _recommended_artifacts(
            category,
            table_unit_review_csv=table_unit_review_csv,
            table_source_traceability_report=table_source_traceability_report,
            table_risk_csv=table_risk_csv,
            review_triage_csv=review_triage_csv,
            query_spec_path=query_spec_path,
        ),
        "recommended_next_action": _recommended_action(category),
        "human_resolution": "",
        "human_notes": "",
    }


def _classify(
    item: dict[str, Any],
    missing_terms: list[str],
    missing_article_nos: list[str],
    missing_article_titles: list[str],
    citations: list[dict[str, Any]],
) -> str:
    blob = " ".join(
        [
            str(item.get("query") or ""),
            " ".join(missing_terms),
            " ".join(missing_article_nos),
            " ".join(missing_article_titles),
            " ".join(str(citation.get("chunk_id") or "") for citation in citations),
            " ".join(str(citation.get("article_title") or "") for citation in citations),
        ]
    )
    if any(token in blob for token in ("적용례", "부칙", "시행일")) or re.search(r"20\d{2}년", blob):
        return "supplementary_temporal_review"
    if any(token in blob for token in ("별표", "표 ", "기준표")):
        return "table_parentage_or_structure_review"
    if any(token in blob for token in ("신고서", "서식", "별지", "휴직자")) or any(
        str(citation.get("chunk_id") or "").find("_form_") >= 0 for citation in citations
    ):
        return "form_parentage_review"
    if missing_article_nos or missing_article_titles:
        return "query_goldset_or_citation_metadata_review"
    if missing_terms:
        return "answer_term_coverage_review"
    return "answer_quality_review"


def _recommended_artifacts(
    category: str,
    *,
    table_unit_review_csv: Path | None,
    table_source_traceability_report: Path | None,
    table_risk_csv: Path | None,
    review_triage_csv: Path | None,
    query_spec_path: Path | None,
) -> str:
    paths: list[str] = []
    if category == "table_parentage_or_structure_review":
        paths.extend(
            _path_text(path)
            for path in (
                table_unit_review_csv,
                table_source_traceability_report,
                table_risk_csv,
                review_triage_csv,
            )
        )
    elif category in {"form_parentage_review", "supplementary_temporal_review"}:
        paths.extend(_path_text(path) for path in (review_triage_csv, table_risk_csv))
    elif category == "query_goldset_or_citation_metadata_review":
        paths.extend(_path_text(path) for path in (query_spec_path, review_triage_csv))
    else:
        paths.extend(_path_text(path) for path in (query_spec_path, review_triage_csv))
    return "; ".join(path for path in paths if path)


def _recommended_action(category: str) -> str:
    actions = {
        "table_parentage_or_structure_review": (
            "Check the improved draft table/unit review artifacts, confirm source-table parentage, then approve/index only after human review."
        ),
        "form_parentage_review": (
            "Confirm the form/별지 governing article relationship in the improved draft and review triage before official approval."
        ),
        "supplementary_temporal_review": (
            "Confirm supplementary-provision/effective-date context before treating this as answer-ready evidence."
        ),
        "query_goldset_or_citation_metadata_review": (
            "Check whether the expected article in the query spec is correct or whether citation metadata still needs parser repair."
        ),
        "answer_term_coverage_review": "Inspect expected terms and supporting evidence; decide whether retrieval or answer wording needs repair.",
        "answer_quality_review": "Inspect answer quality manually and classify the blocker before changing code.",
    }
    return actions.get(category, "Inspect the failed answer and assign it to a review owner.")


def _citation_article(citation: dict[str, Any]) -> str:
    article_no = str(citation.get("article_no") or "")
    article_title = str(citation.get("article_title") or "")
    return " ".join(part for part in (article_no, article_title) if part)


def _citation_page(citation: dict[str, Any]) -> str:
    start = citation.get("source_page_start")
    end = citation.get("source_page_end")
    if start in (None, ""):
        return ""
    if end not in (None, "", start):
        return f"p.{start}-{end}"
    return f"p.{start}"


def _path_text(path: Path | None) -> str:
    return str(path) if path else ""


def _ordered_unique(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


def _split_joined(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAP_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# Answer Blocker Review Map",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Source demo answers: `{report['source_demo_answers']}`",
        f"- Query count: {int(report['query_count']):,}",
        f"- Failed query count: {int(report['failed_query_count']):,}",
        f"- Quality issues: {int(report['quality_issue_count']):,}",
        "",
        "## Blocker Categories",
        "",
        "| Category | Count |",
        "| --- | ---: |",
    ]
    for category, count in sorted(report["blocker_category_counts"].items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"| {_md(category)} | {int(count):,} |")
    lines.extend(["", "## Issue Codes", "", "| Code | Count |", "| --- | ---: |"])
    for code, count in sorted(report["issue_code_counts"].items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"| {_md(code)} | {int(count):,} |")
    lines.extend(
        [
            "",
            "## Failed Queries",
            "",
            "| Rank | Category | Query | Issues | Missing Articles | Recommended Action |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        missing_articles = "; ".join(
            value for value in (row["missing_article_nos"], row["missing_article_titles"]) if value
        )
        lines.append(
            f"| {row['rank']} | {_md(row['blocker_category'])} | {_md(row['query'])} | "
            f"{_md(row['quality_issue_codes'])} | {_md(missing_articles)} | "
            f"{_md(row['recommended_next_action'])} |"
        )
    lines.append("")
    lines.append(f"Safety: {report['safety_note']}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map failed MCP demo-answer queries to review artifacts.")
    parser.add_argument("--demo-answers-json", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--table-unit-review-csv", type=Path)
    parser.add_argument("--table-source-traceability-report", type=Path)
    parser.add_argument("--table-risk-csv", type=Path)
    parser.add_argument("--review-triage-csv", type=Path)
    parser.add_argument("--query-spec-path", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_answer_blocker_review_map(
            demo_answers_json=args.demo_answers_json,
            out_json=args.out_json,
            out_csv=args.out_csv,
            out_md=args.out_md,
            table_unit_review_csv=args.table_unit_review_csv,
            table_source_traceability_report=args.table_source_traceability_report,
            table_risk_csv=args.table_risk_csv,
            review_triage_csv=args.review_triage_csv,
            query_spec_path=args.query_spec_path,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
