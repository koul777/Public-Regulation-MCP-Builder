from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mcp_server.regulation_tools import fetch_regulation, mcp_auth_context, search_regulations, settings_for_mcp_project
from app.rag.extractive_answer import build_structured_extractive_answer, select_supporting_answer_results
from scripts.report_metadata import current_repo_commit


DEFAULT_QUERIES = [
    "육아휴직의 요건과 기간, 수당은?",
    "전임 교원 채용 절차는?",
    "성과연봉은 언제 어떻게 지급되나?",
]
HASH_CHUNK_BYTES = 1024 * 1024
REQUIRED_CITATION_FIELDS = (
    "document_id",
    "chunk_id",
    "approval_id",
    "profile_id",
    "source_system",
    "source_page_start",
)


def export_mcp_demo_answers(
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
    selected_queries = normalize_query_specs(queries=queries, query_specs=query_specs)
    levels = security_levels or ["internal"]
    items = [
        _demo_answer(
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
    report = {
        "report_type": "mcp_demo_answers",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "security_levels": levels,
        "top_k": top_k,
        "query_count": len(items),
        "passed": all(item["passed"] for item in items),
        "quality_issue_count": sum(len(item.get("quality_issues") or []) for item in items),
        "items": items,
        "api_call_count": 0,
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


def normalize_query_specs(
    *,
    queries: list[str] | None = None,
    query_specs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if query_specs:
        normalized = []
        for item in query_specs:
            query = str(item.get("query") or item.get("question") or "").strip()
            if not query:
                continue
            normalized.append(
                {
                    "query": query,
                    "expected_terms": [str(term) for term in item.get("expected_terms") or [] if str(term).strip()],
                    "expected_article_nos": [
                        str(value) for value in item.get("expected_article_nos") or [] if str(value).strip()
                    ],
                    "expected_article_titles": [
                        str(value) for value in item.get("expected_article_titles") or [] if str(value).strip()
                    ],
                    "expect_no_evidence": bool(item.get("expect_no_evidence") or item.get("expected_no_evidence")),
                }
            )
        return normalized
    return [
        {"query": str(query).strip(), "expected_terms": [], "expect_no_evidence": False}
        for query in queries or DEFAULT_QUERIES
        if str(query or "").strip()
    ]


def _demo_answer(
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
    results = search.get("results") if isinstance(search.get("results"), list) else []
    fetched = [
        fetch_regulation(
            settings=settings,
            auth=auth,
            result_id=str(result.get("id") or ""),
            security_levels=security_levels,
        )
        for result in results
        if result.get("id")
    ]
    evidence = [_flatten_mcp_result(result) for result in fetched]
    answer = build_structured_extractive_answer(query, evidence)
    supporting_evidence = select_supporting_answer_results(query, evidence)
    citations = [_citation(result) for result in supporting_evidence]
    smoke_citation_count = sum(1 for citation in citations if "doc_mcp_smoke" in citation.get("document_id", ""))
    expected_term_hits = _expected_term_hits(expected_terms, answer, supporting_evidence)
    expected_term_hit_ratio = round(len(expected_term_hits) / len(expected_terms), 3) if expected_terms else 1.0
    expected_article_no_hits = _expected_article_hits(expected_article_nos, citations, "article_no")
    expected_article_title_hits = _expected_article_hits(expected_article_titles, citations, "article_title")
    expected_article_no_hit_ratio = (
        round(len(expected_article_no_hits) / len(expected_article_nos), 3) if expected_article_nos else 1.0
    )
    expected_article_title_hit_ratio = (
        round(len(expected_article_title_hits) / len(expected_article_titles), 3) if expected_article_titles else 1.0
    )
    if expect_no_evidence:
        quality_issues = _expected_no_evidence_quality_issues(
            results=results,
            fetched=fetched,
            supporting_evidence=supporting_evidence,
            citations=citations,
        )
        passed = not quality_issues
    else:
        quality_issues = _quality_issues(
            answer,
            citations,
            supporting_evidence,
            expected_terms,
            expected_article_nos=expected_article_nos,
            expected_article_titles=expected_article_titles,
        )
        passed = (
            bool(results)
            and bool(fetched)
            and bool(citations)
            and bool(answer.strip())
            and smoke_citation_count == 0
            and not quality_issues
        )
    return {
        "query": query,
        "expect_no_evidence": expect_no_evidence,
        "passed": passed,
        "search_result_count": len(results),
        "fetch_result_count": len(fetched),
        "supporting_result_count": len(supporting_evidence),
        "expected_terms": expected_terms,
        "expected_term_hits": expected_term_hits,
        "expected_term_hit_ratio": expected_term_hit_ratio,
        "expected_article_nos": expected_article_nos,
        "expected_article_no_hits": expected_article_no_hits,
        "expected_article_no_hit_ratio": expected_article_no_hit_ratio,
        "expected_article_titles": expected_article_titles,
        "expected_article_title_hits": expected_article_title_hits,
        "expected_article_title_hit_ratio": expected_article_title_hit_ratio,
        "answer": answer,
        "citations": citations,
        "trace_id": (search.get("metadata") or {}).get("trace_id"),
        "smoke_citation_count": smoke_citation_count,
        "quality_issue_count": len(quality_issues),
        "quality_issues": quality_issues,
    }


def _flatten_mcp_result(result: dict[str, Any]) -> dict[str, Any]:
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
        "content_hash": str(result.get("content_hash") or ""),
        "approved_content_hash": str(result.get("approved_content_hash") or ""),
        "profile_id": str(result.get("profile_id") or ""),
        "source_system": str(result.get("source_system") or ""),
        "source_url": str(result.get("source_url") or ""),
        "security_level": str(result.get("security_level") or ""),
        "text": str(result.get("text") or ""),
    }


def _quality_issues(
    answer: str,
    citations: list[dict[str, Any]],
    evidence: list[dict[str, Any]] | None = None,
    expected_terms: list[str] | None = None,
    *,
    expected_article_nos: list[str] | None = None,
    expected_article_titles: list[str] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    issues.extend(_answer_text_quality_issues(answer))
    issues.extend(_citation_quality_issues(citations))
    issues.extend(_expected_term_quality_issues(expected_terms or [], answer, evidence or []))
    issues.extend(_expected_article_quality_issues(expected_article_nos or [], citations, "article_no"))
    issues.extend(_expected_article_quality_issues(expected_article_titles or [], citations, "article_title"))
    return issues


def _expected_no_evidence_quality_issues(
    *,
    results: list[dict[str, Any]],
    fetched: list[dict[str, Any]],
    supporting_evidence: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not results and not fetched and not supporting_evidence and not citations:
        return []
    return [
        {
            "code": "expected-no-evidence-results-returned",
            "search_result_count": len(results),
            "fetch_result_count": len(fetched),
            "supporting_result_count": len(supporting_evidence),
            "citation_count": len(citations),
            "detail": "This query is marked expect_no_evidence but approved MCP evidence was returned.",
        }
    ]


def _expected_term_quality_issues(
    expected_terms: list[str],
    answer: str,
    evidence: list[dict[str, Any]],
    *,
    minimum_hit_ratio: float = 0.5,
) -> list[dict[str, Any]]:
    if not expected_terms:
        return []
    hits = _expected_term_hits(expected_terms, answer, evidence)
    ratio = len(hits) / len(expected_terms)
    if ratio >= minimum_hit_ratio:
        return []
    missing = [term for term in expected_terms if term not in hits]
    return [
        {
            "code": "expected-term-coverage-low",
            "expected_term_hit_ratio": round(ratio, 3),
            "missing_terms": missing,
            "detail": f"Only {len(hits)}/{len(expected_terms)} expected terms were found in the answer or supporting evidence.",
        }
    ]


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


def _expected_article_quality_issues(
    expected_values: list[str],
    citations: list[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    if not expected_values:
        return []
    hits = _expected_article_hits(expected_values, citations, field)
    if len(hits) == len(expected_values):
        return []
    missing = [value for value in expected_values if value not in hits]
    return [
        {
            "code": f"expected-{field.replace('_', '-')}-missing",
            "missing_values": missing,
            "detail": f"Supporting citations did not include expected {field}: {', '.join(missing)}",
        }
    ]


def _expected_article_hits(expected_values: list[str], citations: list[dict[str, Any]], field: str) -> list[str]:
    citation_values = _citation_article_values(citations, field)
    hits: list[str] = []
    for value in expected_values:
        normalized = _normalize_article_title_text(value)
        if normalized and any(normalized in citation_value for citation_value in citation_values):
            hits.append(value)
    return hits


def _citation_article_values(citations: list[dict[str, Any]], field: str) -> list[str]:
    values: list[str] = []
    for citation in citations:
        values.append(_normalize_article_title_text(citation.get(field)))
        for candidate in citation.get("article_label_candidates") or []:
            if isinstance(candidate, dict):
                values.append(_normalize_article_title_text(candidate.get(field)))
        if field == "article_title":
            values.append(_normalize_article_title_text(citation.get("text")))
        if field == "article_no":
            values.extend(_normalize_article_title_text(value) for value in citation.get("article_refs") or [])
    return [value for value in values if value]


def _normalize_article_title_text(value: Any) -> str:
    normalized = str(value or "").lower()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("원규", "규정")
    return normalized


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


def _answer_text_quality_issues(answer: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(str(answer or "").splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        lower = line.lower()
        if not line:
            continue
        if "doc_mcp_smoke" in lower:
            issues.append(_quality_issue("answer-smoke-doc-reference", line_no, line))
        if re.match(
            r"^(키워드|문서명|문서|위치|보안등급|청크|의도|chunk|keywords|duration|payment|procedure|procedure_step|eligibility|exception|condition|source|intent|obligation|prohibition|reference|definition|scope):",
            line,
            flags=re.IGNORECASE,
        ):
            issues.append(_quality_issue("answer-metadata-label", line_no, line))
        if re.search(r"\b20\d\s+\d년|일 시금|정 산", line):
            issues.append(_quality_issue("answer-bad-spacing", line_no, line))
        if re.search(r"(?:\b20\d|제\d+조(?:의\d+)?|원칙으|이루어지지|성과연봉을 일)$", line):
            issues.append(_quality_issue("answer-fragment-line", line_no, line))
        if line.startswith(("게 ", "거쳐 ", "따라 ", "따른 ", "하여 ")):
            issues.append(_quality_issue("answer-fragment-line", line_no, line))
    return issues


def _citation_quality_issues(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not citations:
        return [{"code": "citation-missing", "detail": "No supporting citations were selected."}]
    for index, citation in enumerate(citations, start=1):
        if "doc_mcp_smoke" in str(citation.get("document_id") or ""):
            issues.append(
                {
                    "code": "citation-smoke-document",
                    "citation_index": index,
                    "detail": str(citation.get("document_id") or ""),
                }
            )
        missing = [field for field in REQUIRED_CITATION_FIELDS if citation.get(field) in (None, "")]
        if missing:
            issues.append(
                {
                    "code": "citation-missing-required-fields",
                    "citation_index": index,
                    "missing_fields": missing,
                }
            )
    return issues


def _quality_issue(code: str, line_no: int, line: str) -> dict[str, Any]:
    return {
        "code": code,
        "line": line_no,
        "detail": line[:160],
    }


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MCP Demo Answers",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Tenant: `{report.get('tenant_id')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Quality issues: {report.get('quality_issue_count')}",
        f"- API calls: {report.get('api_call_count')}",
        "",
    ]
    for index, item in enumerate(report.get("items") or [], start=1):
        lines.extend(
            [
                f"## {index}. {item.get('query')}",
                "",
                f"- Passed: `{str(item.get('passed')).lower()}`",
                f"- Expected no evidence: `{str(item.get('expect_no_evidence', False)).lower()}`",
                f"- Search results: {item.get('search_result_count')}",
                f"- Fetch results: {item.get('fetch_result_count')}",
                f"- Supporting results: {item.get('supporting_result_count')}",
                f"- Expected term hit ratio: {item.get('expected_term_hit_ratio')}",
                f"- Expected article no hit ratio: {item.get('expected_article_no_hit_ratio')}",
                f"- Expected article title hit ratio: {item.get('expected_article_title_hit_ratio')}",
                f"- Quality issues: {item.get('quality_issue_count')}",
                f"- Trace: `{item.get('trace_id')}`",
                "",
                "### Answer",
                "",
                str(item.get("answer") or "").strip(),
                "",
                "### Sources",
                "",
                "| Regulation | Article | Page | Approval | Profile | Source |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for citation in item.get("citations") or []:
            article = " ".join(
                value
                for value in [citation.get("article_no"), citation.get("article_title")]
                if value
            )
            page = _page_label(citation)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _md_cell(citation.get("regulation_title") or citation.get("document_name")),
                        _md_cell(article),
                        _md_cell(page),
                        _md_cell(citation.get("approval_id")),
                        _md_cell(citation.get("profile_id")),
                        _md_cell(citation.get("source_system") or citation.get("source_url")),
                    ]
                )
                + " |"
            )
        quality_issues = item.get("quality_issues") or []
        if quality_issues:
            lines.extend(["", "### Quality Issues", ""])
            for issue in quality_issues:
                lines.append(f"- `{issue.get('code')}`: {_md_cell(issue.get('detail') or issue.get('missing_fields'))}")
        lines.append("")
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
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export grounded MCP demo answers from an approved local regulation runtime.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument(
        "--query-spec-json",
        default=None,
        help="Optional JSON file with [{query/question, expected_terms}] demo answer checks.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--security-level", action="append", default=["internal"])
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
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
    report = export_mcp_demo_answers(
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
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def load_query_specs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("queries") or payload.get("items") or []
    if not isinstance(payload, list):
        raise ValueError("--query-spec-json must contain a list or an object with queries/items.")
    return [item for item in payload if isinstance(item, dict)]


def query_spec_fingerprint(path: Path, *, item_count: int | None = None) -> dict[str, Any]:
    return {
        "query_spec_path": str(path),
        "query_spec_byte_count": path.stat().st_size,
        "query_spec_sha256": _sha256_file(path),
        "query_spec_item_count": item_count,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
