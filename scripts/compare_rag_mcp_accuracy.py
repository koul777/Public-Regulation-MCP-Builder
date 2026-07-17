from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mcp_server.regulation_tools import fetch_regulation, mcp_auth_context, search_regulations, settings_for_mcp_project
from app.rag.extractive_answer import build_structured_extractive_answer, select_supporting_answer_results
from scripts.export_mcp_demo_answers import REQUIRED_CITATION_FIELDS, normalize_query_specs, query_spec_fingerprint
from scripts.report_metadata import current_repo_commit


def compare_rag_mcp_accuracy(
    *,
    data_dir: Path,
    tenant_id: str,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
    top_k: int = 5,
    security_levels: list[str] | None = None,
    tenant_storage_isolation: bool | None = None,
    query_spec_source: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    settings = settings_for_mcp_project(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    auth = mcp_auth_context(tenant_id=tenant_id)
    levels = security_levels or ["internal"]
    selected_queries = _normalize_specs(queries=queries, query_specs=query_specs)
    items = [
        _compare_query(
            settings=settings,
            auth=auth,
            query=str(spec["query"]),
            expected_terms=[str(term) for term in spec.get("expected_terms") or []],
            expected_article_nos=[str(value) for value in spec.get("expected_article_nos") or []],
            expected_article_titles=[str(value) for value in spec.get("expected_article_titles") or []],
            expect_no_evidence=bool(spec.get("expect_no_evidence") or spec.get("expected_no_evidence")),
            top_k=top_k,
            security_levels=levels,
        )
        for spec in selected_queries
    ]
    summary = _summary(items)
    report = {
        "report_type": "simple_rag_vs_mcp_accuracy",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "security_levels": levels,
        "top_k": top_k,
        "query_count": len(items),
        "summary": summary,
        "passed": summary["mcp_regression_count"] == 0 and summary["mcp_passed_count"] == len(items),
        "comparison_note": (
            "Baseline is search-only RAG over top snippets. MCP uses the same approved retrieval scope plus fetch "
            "and structured citation metadata before extractive answering."
        ),
        "api_call_count": 0,
        "items": items,
    }
    if query_spec_source:
        report.update(query_spec_fingerprint(query_spec_source, item_count=len(selected_queries)))
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _normalize_specs(
    *,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_query_specs(queries=queries, query_specs=query_specs)
    specs_by_query = {
        str(item.get("query") or item.get("question") or "").strip(): item
        for item in query_specs or []
        if isinstance(item, dict)
    }
    for spec in normalized:
        original = specs_by_query.get(str(spec.get("query") or "").strip()) or {}
        spec["expect_no_evidence"] = bool(original.get("expect_no_evidence") or original.get("expected_no_evidence"))
    return normalized


def _compare_query(
    *,
    settings: Any,
    auth: Any,
    query: str,
    expected_terms: list[str],
    expected_article_nos: list[str],
    expected_article_titles: list[str],
    expect_no_evidence: bool,
    top_k: int,
    security_levels: list[str],
) -> dict[str, Any]:
    search = search_regulations(
        settings=settings,
        auth=auth,
        query=query,
        top_k=top_k,
        security_levels=security_levels,
    )
    search_results = search.get("results") if isinstance(search.get("results"), list) else []
    baseline_evidence = [_flatten_result(result) for result in search_results]
    baseline_answer = build_structured_extractive_answer(query, baseline_evidence)
    baseline_supporting = select_supporting_answer_results(query, baseline_evidence)
    baseline_citations = [_citation(result) for result in baseline_supporting]

    fetched = [
        fetch_regulation(
            settings=settings,
            auth=auth,
            result_id=str(result.get("id") or ""),
            security_levels=security_levels,
        )
        for result in search_results
        if result.get("id")
    ]
    mcp_evidence = [_flatten_result(result) for result in fetched]
    mcp_answer = build_structured_extractive_answer(query, mcp_evidence)
    mcp_supporting = select_supporting_answer_results(query, mcp_evidence)
    mcp_citations = [_citation(result) for result in mcp_supporting]

    baseline_metrics = _quality_metrics(
        answer=baseline_answer,
        evidence=baseline_supporting,
        citations=baseline_citations,
        result_count=len(search_results),
        expected_terms=expected_terms,
        expected_article_nos=expected_article_nos,
        expected_article_titles=expected_article_titles,
        expect_no_evidence=expect_no_evidence,
    )
    mcp_metrics = _quality_metrics(
        answer=mcp_answer,
        evidence=mcp_supporting,
        citations=mcp_citations,
        result_count=len(fetched),
        expected_terms=expected_terms,
        expected_article_nos=expected_article_nos,
        expected_article_titles=expected_article_titles,
        expect_no_evidence=expect_no_evidence,
    )
    score_delta = round(mcp_metrics["quality_score"] - baseline_metrics["quality_score"], 3)
    coverage_regression_fields = _coverage_regression_fields(baseline_metrics, mcp_metrics)
    return {
        "query": query,
        "expect_no_evidence": expect_no_evidence,
        "expected_terms": expected_terms,
        "expected_article_nos": expected_article_nos,
        "expected_article_titles": expected_article_titles,
        "baseline": {
            "mode": "search_only_rag",
            "result_count": len(search_results),
            "supporting_result_count": len(baseline_supporting),
            "answer": baseline_answer,
            "citations": baseline_citations,
            "metrics": baseline_metrics,
        },
        "mcp": {
            "mode": "mcp_search_fetch_answer",
            "search_result_count": len(search_results),
            "fetch_result_count": len(fetched),
            "supporting_result_count": len(mcp_supporting),
            "answer": mcp_answer,
            "citations": mcp_citations,
            "metrics": mcp_metrics,
            "trace_id": (search.get("metadata") or {}).get("trace_id"),
        },
        "score_delta": score_delta,
        "mcp_better": score_delta > 0,
        "coverage_regression_fields": coverage_regression_fields,
        "mcp_not_worse": score_delta >= 0 and not coverage_regression_fields,
        "mcp_regression": (
            bool(coverage_regression_fields)
            or score_delta < 0
            or (baseline_metrics["passed"] and not mcp_metrics["passed"])
        ),
    }


def _quality_metrics(
    *,
    answer: str,
    evidence: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    result_count: int,
    expected_terms: list[str],
    expected_article_nos: list[str],
    expected_article_titles: list[str],
    expect_no_evidence: bool,
) -> dict[str, Any]:
    term_hits = _expected_term_hits(expected_terms, answer, evidence)
    article_no_hits = _expected_article_hits(expected_article_nos, citations, "article_no")
    article_title_hits = _expected_article_hits(expected_article_titles, citations, "article_title")
    citation_completeness_ratio = _citation_completeness_ratio(citations)
    unsupported_line_count = _unsupported_line_count(answer, evidence)
    evidence_char_count = sum(len(str(item.get("text") or "")) for item in evidence)
    if expect_no_evidence:
        passed = result_count == 0
        quality_score = 1.0 if passed else 0.0
    else:
        passed = (
            result_count > 0
            and bool(answer.strip())
            and _ratio(len(term_hits), len(expected_terms)) >= (0.5 if expected_terms else 1.0)
            and _ratio(len(article_no_hits), len(expected_article_nos)) >= 1.0
            and _ratio(len(article_title_hits), len(expected_article_titles)) >= 1.0
            and citation_completeness_ratio >= 0.8
            and unsupported_line_count == 0
        )
        quality_score = _quality_score(
            expected_term_hit_ratio=_ratio(len(term_hits), len(expected_terms)),
            expected_article_no_hit_ratio=_ratio(len(article_no_hits), len(expected_article_nos)),
            expected_article_title_hit_ratio=_ratio(len(article_title_hits), len(expected_article_titles)),
            citation_completeness_ratio=citation_completeness_ratio,
            unsupported_line_count=unsupported_line_count,
        )
    return {
        "passed": passed,
        "quality_score": quality_score,
        "expected_term_hits": term_hits,
        "expected_term_hit_ratio": _ratio(len(term_hits), len(expected_terms)),
        "expected_article_no_hits": article_no_hits,
        "expected_article_no_hit_ratio": _ratio(len(article_no_hits), len(expected_article_nos)),
        "expected_article_title_hits": article_title_hits,
        "expected_article_title_hit_ratio": _ratio(len(article_title_hits), len(expected_article_titles)),
        "citation_completeness_ratio": citation_completeness_ratio,
        "unsupported_line_count": unsupported_line_count,
        "evidence_char_count": evidence_char_count,
    }


def _quality_score(
    *,
    expected_term_hit_ratio: float,
    expected_article_no_hit_ratio: float,
    expected_article_title_hit_ratio: float,
    citation_completeness_ratio: float,
    unsupported_line_count: int,
) -> float:
    score = (
        expected_term_hit_ratio * 0.35
        + expected_article_no_hit_ratio * 0.2
        + expected_article_title_hit_ratio * 0.2
        + citation_completeness_ratio * 0.25
    )
    score -= min(unsupported_line_count, 5) * 0.1
    return round(max(0.0, min(1.0, score)), 3)


def _coverage_regression_fields(baseline_metrics: dict[str, Any], mcp_metrics: dict[str, Any]) -> list[str]:
    fields = []
    for field in (
        "expected_term_hit_ratio",
        "expected_article_no_hit_ratio",
        "expected_article_title_hit_ratio",
    ):
        baseline = float(baseline_metrics.get(field) or 0.0)
        mcp = float(mcp_metrics.get(field) or 0.0)
        if mcp < baseline:
            fields.append(field)
    return fields


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_scores = [float((item["baseline"]["metrics"] or {}).get("quality_score") or 0.0) for item in items]
    mcp_scores = [float((item["mcp"]["metrics"] or {}).get("quality_score") or 0.0) for item in items]
    return {
        "baseline_passed_count": sum(1 for item in items if item["baseline"]["metrics"]["passed"]),
        "mcp_passed_count": sum(1 for item in items if item["mcp"]["metrics"]["passed"]),
        "mcp_better_count": sum(1 for item in items if item["mcp_better"]),
        "mcp_not_worse_count": sum(1 for item in items if item["mcp_not_worse"]),
        "mcp_regression_count": sum(1 for item in items if item["mcp_regression"]),
        "baseline_avg_quality_score": round(statistics.fmean(baseline_scores), 3) if baseline_scores else 0.0,
        "mcp_avg_quality_score": round(statistics.fmean(mcp_scores), 3) if mcp_scores else 0.0,
        "avg_score_delta": round(statistics.fmean([float(item["score_delta"]) for item in items]), 3) if items else 0.0,
    }


def _flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    flattened = dict(metadata)
    flattened["id"] = result.get("id") or ""
    flattened["title"] = result.get("title") or ""
    flattened["url"] = result.get("url") or ""
    flattened["text"] = result.get("text") or ""
    return flattened


def _citation(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": str(result.get("document_id") or ""),
        "chunk_id": str(result.get("chunk_id") or ""),
        "document_name": str(result.get("document_name") or ""),
        "institution_name": str(result.get("institution_name") or ""),
        "regulation_title": str(result.get("regulation_title") or ""),
        "article_no": str(result.get("article_no") or ""),
        "article_title": str(result.get("article_title") or ""),
        "article_refs": result.get("article_refs") or [],
        "article_label_candidates": _article_label_candidates(result),
        "source_page_start": result.get("source_page_start"),
        "source_page_end": result.get("source_page_end"),
        "approval_id": str(result.get("approval_id") or ""),
        "profile_id": str(result.get("profile_id") or ""),
        "source_system": str(result.get("source_system") or ""),
        "security_level": str(result.get("security_level") or ""),
    }


def _expected_term_hits(expected_terms: list[str], answer: str, evidence: list[dict[str, Any]]) -> list[str]:
    haystack_parts = [str(answer or "")]
    for item in evidence:
        haystack_parts.extend(
            str(item.get(field) or "")
            for field in (
                "text",
                "title",
                "document_name",
                "institution_name",
                "regulation_title",
                "article_no",
                "article_title",
            )
        )
    haystack = " ".join(haystack_parts).lower()
    compact_haystack = _compact_term_text(haystack)
    hits: list[str] = []
    for term in expected_terms:
        normalized = str(term or "").lower()
        compact = _compact_term_text(normalized)
        if normalized and (normalized in haystack or (compact and compact in compact_haystack)):
            hits.append(term)
    return hits


def _compact_term_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _expected_article_hits(expected_values: list[str], citations: list[dict[str, Any]], field: str) -> list[str]:
    if not expected_values:
        return []
    citation_values = _citation_article_values(citations, field)
    hits: list[str] = []
    for value in expected_values:
        normalized = str(value or "").lower()
        if normalized and any(normalized in citation_value for citation_value in citation_values):
            hits.append(value)
    return hits


def _citation_article_values(citations: list[dict[str, Any]], field: str) -> list[str]:
    values: list[str] = []
    for citation in citations:
        values.append(str(citation.get(field) or "").lower())
        for candidate in citation.get("article_label_candidates") or []:
            if isinstance(candidate, dict):
                values.append(str(candidate.get(field) or "").lower())
        if field == "article_no":
            values.extend(str(value or "").lower() for value in citation.get("article_refs") or [])
    return [value for value in values if value]


def _article_label_candidates(result: dict[str, Any]) -> list[dict[str, str]]:
    text = str(result.get("text") or "")
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    pattern = re.compile(r"(\uc81c\d+\uc870(?:\uc758\d+)?)\s*\(([^)\n]{1,80})\)")
    for match in pattern.finditer(text):
        article_no = " ".join(match.group(1).split())
        article_title = " ".join(match.group(2).split())
        key = (article_no, article_title)
        if article_no and article_title and key not in seen:
            seen.add(key)
            candidates.append({"article_no": article_no, "article_title": article_title})
    reference_pattern = re.compile(r"\uc81c\d+\uc870(?:\uc758\d+)?(?:\uc81c\d+\ud56d)?")
    for match in reference_pattern.finditer(text):
        article_no = " ".join(match.group(0).split())
        key = (article_no, "")
        if article_no and key not in seen:
            seen.add(key)
            candidates.append({"article_no": article_no, "article_title": ""})
    return candidates[:12]


def _citation_completeness_ratio(citations: list[dict[str, Any]]) -> float:
    if not citations:
        return 0.0
    total = len(citations) * len(REQUIRED_CITATION_FIELDS)
    present = sum(
        1
        for citation in citations
        for field in REQUIRED_CITATION_FIELDS
        if citation.get(field) not in (None, "")
    )
    return round(present / total, 3) if total else 0.0


def _unsupported_line_count(answer: str, evidence: list[dict[str, Any]]) -> int:
    if not answer.strip():
        return 0
    evidence_text = " ".join(str(item.get("text") or "") for item in evidence).lower()
    if not evidence_text:
        return len([line for line in answer.splitlines() if line.strip()])
    unsupported = 0
    for raw_line in answer.splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if not line:
            continue
        if _is_structural_answer_line(line):
            continue
        tokens = [token for token in line.lower().split() if len(token) >= 2]
        if not tokens:
            continue
        overlap = sum(1 for token in tokens if token in evidence_text)
        if overlap == 0:
            unsupported += 1
    return unsupported


def _is_structural_answer_line(line: str) -> bool:
    lowered = line.lower()
    if line in {"승인된 규정 근거 기준입니다.", "확인된 내용", "근거 조항", "관련 근거를 찾지 못했습니다."}:
        return True
    if "approval=" in lowered:
        return True
    if lowered.startswith(("source:", "sources:", "citation:", "citations:")):
        return True
    return False


def _ratio(hit_count: int, expected_count: int) -> float:
    if expected_count <= 0:
        return 1.0
    return round(hit_count / expected_count, 3)


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Simple RAG vs MCP Accuracy",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Queries: {report.get('query_count')}",
        f"- Baseline passed: {summary.get('baseline_passed_count')}",
        f"- MCP passed: {summary.get('mcp_passed_count')}",
        f"- MCP better / not worse / regression: {summary.get('mcp_better_count')} / {summary.get('mcp_not_worse_count')} / {summary.get('mcp_regression_count')}",
        f"- Avg quality score baseline -> MCP: {summary.get('baseline_avg_quality_score')} -> {summary.get('mcp_avg_quality_score')} ({summary.get('avg_score_delta')})",
        f"- API calls: {report.get('api_call_count')}",
        "",
        str(report.get("comparison_note") or ""),
        "",
        "| Query | Baseline score | MCP score | Delta | MCP passed | Regression fields | Term ratio | Article ratio | Citation ratio |",
        "| --- | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for item in report.get("items") or []:
        baseline_metrics = item["baseline"]["metrics"]
        mcp_metrics = item["mcp"]["metrics"]
        article_ratio = min(
            float(mcp_metrics.get("expected_article_no_hit_ratio") or 0.0),
            float(mcp_metrics.get("expected_article_title_hit_ratio") or 0.0),
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(item.get("query")),
                    _md_cell(baseline_metrics.get("quality_score")),
                    _md_cell(mcp_metrics.get("quality_score")),
                    _md_cell(item.get("score_delta")),
                    _md_cell(str(mcp_metrics.get("passed")).lower()),
                    _md_cell(", ".join(item.get("coverage_regression_fields") or [])),
                    _md_cell(mcp_metrics.get("expected_term_hit_ratio")),
                    _md_cell(article_ratio),
                    _md_cell(mcp_metrics.get("citation_completeness_ratio")),
                ]
            )
            + " |"
        )
    for index, item in enumerate(report.get("items") or [], start=1):
        lines.extend(
            [
                "",
                f"## {index}. {item.get('query')}",
                "",
                f"- Baseline result/supporting: {item['baseline'].get('result_count')} / {item['baseline'].get('supporting_result_count')}",
                f"- MCP search/fetch/supporting: {item['mcp'].get('search_result_count')} / {item['mcp'].get('fetch_result_count')} / {item['mcp'].get('supporting_result_count')}",
                f"- Score delta: {item.get('score_delta')}",
                f"- Coverage regression fields: {', '.join(item.get('coverage_regression_fields') or []) or '-'}",
                f"- Trace: `{item['mcp'].get('trace_id')}`",
                "",
                "### MCP Answer",
                "",
                str(item["mcp"].get("answer") or "").strip(),
                "",
                "### MCP Citations",
                "",
                "| Regulation | Article | Page | Approval | Profile | Source |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for citation in item["mcp"].get("citations") or []:
            article = " ".join(value for value in [citation.get("article_no"), citation.get("article_title")] if value)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _md_cell(citation.get("regulation_title") or citation.get("document_name")),
                        _md_cell(article),
                        _md_cell(_page_label(citation)),
                        _md_cell(citation.get("approval_id")),
                        _md_cell(citation.get("profile_id")),
                        _md_cell(citation.get("source_system")),
                    ]
                )
                + " |"
            )
    return "\n".join(lines).rstrip() + "\n"


def _page_label(citation: dict[str, Any]) -> str:
    start = citation.get("source_page_start")
    end = citation.get("source_page_end")
    if start and end and start != end:
        return f"p.{start}-{end}"
    if start:
        return f"p.{start}"
    return ""


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def load_query_specs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("queries") or payload.get("items") or []
    if not isinstance(payload, list):
        raise ValueError("--query-spec-json must contain a list or an object with queries/items.")
    return [item for item in payload if isinstance(item, dict)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare search-only RAG and MCP fetch-backed answer quality.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--query-spec-json", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--security-level", action="append", default=["internal"])
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-regression", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    query_specs = load_query_specs(Path(args.query_spec_json)) if args.query_spec_json else None
    query_spec_source = Path(args.query_spec_json) if args.query_spec_json else None
    report = compare_rag_mcp_accuracy(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        queries=args.query or None,
        query_specs=query_specs,
        top_k=args.top_k,
        security_levels=args.security_level,
        tenant_storage_isolation=tenant_storage_isolation,
        query_spec_source=query_spec_source,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_regression and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
