from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from scripts.export_vectordb_ingestion import _iter_batch_chunks, load_json


DEFAULT_QUERIES = [
    {
        "id": "incheon_overseas_dispatch",
        "question": "인천국제공항공사 해외파견 근무자 선발 절차와 제출 서류는?",
        "expected_terms": ["인천국제공항공사", "해외", "파견", "선발", "제출"],
    },
    {
        "id": "contract_emergency_bid",
        "question": "계약사무처리지침에서 긴급 입찰공고 사유서 작성 근거는 무엇인가?",
        "expected_terms": ["계약사무처리지침", "긴급", "입찰공고", "사유서", "제35조"],
    },
    {
        "id": "sick_leave_counting",
        "question": "복무편람에서 병가와 질병 지각 조퇴 외출은 어떻게 산정하는가?",
        "expected_terms": ["복무편람", "병가", "질병", "지각", "조퇴", "외출", "8시간"],
    },
    {
        "id": "public_hiring_principles",
        "question": "공공기관 직원 채용 절차와 공개경쟁 원칙은 무엇인가?",
        "expected_terms": ["채용", "공개경쟁", "공고", "절차", "인사위원회"],
    },
    {
        "id": "sole_source_contract_form",
        "question": "수의계약 사유서에는 어떤 법적 근거와 요청 사유를 적어야 하는가?",
        "expected_terms": ["수의계약", "사유서", "법적 근거", "요청사유", "시행령"],
    },
]


SUSPICIOUS_TEXT_PATTERN = re.compile(r"[�兀才汫╨昏뼼쬼쒸ü]")
REVIEW_CHUNK_TYPES = {"appendix", "form"}


def evaluate_retrieval(
    chunks: list[dict[str, Any]],
    relation_edges: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    edges_by_chunk = group_edges_by_chunk(relation_edges)
    all_query_terms = sorted({term for query in queries for term in query_terms(query)})
    idf = build_idf(chunks, all_query_terms)
    results = [
        evaluate_query(query, chunks, edges_by_chunk, idf, top_k=top_k)
        for query in queries
    ]
    answerable_results = [result for result in results if not result.get("expect_no_evidence")]
    no_evidence_results = [result for result in results if result.get("expect_no_evidence")]
    answerable_count = sum(1 for result in answerable_results if result["answerable"])
    no_evidence_passed_count = sum(1 for result in no_evidence_results if result.get("no_evidence_passed"))
    relation_supported_count = sum(1 for result in results if result["relation_edge_count"] > 0)
    quality_flag_counts = Counter(
        flag["code"]
        for result in results
        for chunk in result["top_chunks"]
        for flag in chunk["quality_flags"]
    )
    quality_flag_chunk_count = sum(
        1
        for result in results
        for chunk in result["top_chunks"]
        if chunk["quality_flags"]
    )
    quality_warning_chunk_count = sum(
        1
        for result in results
        for chunk in result["top_chunks"]
        if any(flag["severity"] == "warning" for flag in chunk["quality_flags"])
    )
    return {
        "report_type": "rag_retrieval_eval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query_count": len(results),
        "answerable_query_count": len(answerable_results),
        "answerable_count": answerable_count,
        "answerable_ratio": round(answerable_count / len(answerable_results), 3) if answerable_results else 0.0,
        "expect_no_evidence_query_count": len(no_evidence_results),
        "no_evidence_passed_count": no_evidence_passed_count,
        "no_evidence_failed_count": len(no_evidence_results) - no_evidence_passed_count,
        "relation_supported_count": relation_supported_count,
        "relation_supported_ratio": round(relation_supported_count / len(results), 3) if results else 0.0,
        "top_k": top_k,
        "results": results,
        "quality_flag_counts": dict(quality_flag_counts),
        "quality_flag_chunk_count": quality_flag_chunk_count,
        "quality_warning_chunk_count": quality_warning_chunk_count,
        "quality_warning_query_count": sum(1 for result in results if result["quality_warning_chunk_count"]),
        "api_call_count": 0,
    }


def evaluate_query(
    query: dict[str, Any],
    chunks: list[dict[str, Any]],
    edges_by_chunk: dict[str, list[dict[str, Any]]],
    idf: dict[str, float],
    *,
    top_k: int,
) -> dict[str, Any]:
    expect_no_evidence = bool(query.get("expect_no_evidence") or query.get("expected_no_evidence"))
    terms = query_terms(query, include_expected_terms=not expect_no_evidence)
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in chunks:
        score = score_chunk(chunk, terms, idf, edges_by_chunk.get(str(chunk.get("chunk_id") or ""), []))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("document_name") or ""), str(item[1].get("chunk_id") or "")))
    top_chunks = [
        chunk_result(chunk, score, edges_by_chunk.get(str(chunk.get("chunk_id") or ""), []))
        for score, chunk in scored[:top_k]
    ]
    expected_terms = [str(term) for term in query.get("expected_terms") or [] if str(term).strip()]
    term_hits = expected_term_hits(expected_terms, top_chunks)
    per_chunk_term_hits = [expected_term_hits_for_chunk(expected_terms, chunk) for chunk in top_chunks]
    max_chunk_term_hits = max(per_chunk_term_hits, key=len, default=[])
    relation_edge_count = sum(len(item["relation_edges"]) for item in top_chunks)
    quality_flag_counts = Counter(
        flag["code"]
        for chunk in top_chunks
        for flag in chunk["quality_flags"]
    )
    top_score = round(scored[0][0], 3) if scored else 0.0
    hit_ratio = round(len(term_hits) / len(expected_terms), 3) if expected_terms else 0.0
    max_chunk_hit_ratio = round(len(max_chunk_term_hits) / len(expected_terms), 3) if expected_terms else 0.0
    answerable_candidate = bool(top_chunks and (not expected_terms or hit_ratio >= 0.5))
    if expect_no_evidence:
        answerable_candidate = bool(top_chunks and (not expected_terms or max_chunk_hit_ratio >= 0.8))
    return {
        "id": query.get("id") or stable_query_id(query_text(query)),
        "question": query_text(query),
        "expect_no_evidence": expect_no_evidence,
        "expected_terms": expected_terms,
        "expected_term_hits": term_hits,
        "expected_term_hit_ratio": hit_ratio,
        "max_chunk_expected_term_hits": max_chunk_term_hits,
        "max_chunk_expected_term_hit_ratio": max_chunk_hit_ratio,
        "answerable": bool(answerable_candidate and not expect_no_evidence),
        "no_evidence_passed": bool(expect_no_evidence and not answerable_candidate),
        "top_score": top_score,
        "relation_edge_count": relation_edge_count,
        "relation_type_counts": dict(Counter(edge["relation_type"] for item in top_chunks for edge in item["relation_edges"])),
        "quality_flag_counts": dict(quality_flag_counts),
        "quality_flag_chunk_count": sum(1 for chunk in top_chunks if chunk["quality_flags"]),
        "quality_warning_chunk_count": sum(
            1
            for chunk in top_chunks
            if any(flag["severity"] == "warning" for flag in chunk["quality_flags"])
        ),
        "top_chunks": top_chunks,
    }


def query_terms(query: dict[str, Any], *, include_expected_terms: bool = True) -> list[str]:
    values = [query_text(query)]
    if include_expected_terms:
        values.append(" ".join(query.get("expected_terms") or []))
    text = " ".join(str(value or "") for value in values)
    terms = []
    seen = set()
    for term in tokenize(text):
        if term not in seen:
            terms.append(term)
            seen.add(term)
    return terms


def score_chunk(chunk: dict[str, Any], terms: list[str], idf: dict[str, float], relation_edges: list[dict[str, Any]]) -> float:
    body_blob = chunk_body_blob(chunk)
    title_blob = chunk_title_blob(chunk)
    metadata_blob = chunk_metadata_blob(chunk)
    full_blob = " ".join([body_blob, title_blob, metadata_blob])
    edge_blob = " ".join(
        str(edge.get(field) or "")
        for edge in relation_edges
        for field in ("relation_type", "target_label", "evidence_text")
    ).lower()
    score = 0.0
    for term in terms:
        term_key = term.lower()
        weight = idf.get(term_key, 1.0)
        body_count = body_blob.count(term_key)
        title_count = title_blob.count(term_key)
        metadata_count = metadata_blob.count(term_key)
        if body_count:
            score += min(body_count, 5) * weight * 1.0
        if title_count:
            score += min(title_count, 3) * weight * 1.4
        if metadata_count:
            score += min(metadata_count, 2) * weight * 0.25
        if term_key in edge_blob:
            score += 0.35
    if str(chunk.get("table_markdown") or ""):
        score += 0.15
    if looks_garbled_text(full_blob):
        score *= 0.35
    return round(score, 6)


def build_idf(chunks: list[dict[str, Any]], terms: list[str]) -> dict[str, float]:
    if not chunks:
        return {term: 1.0 for term in terms}
    document_frequency = Counter()
    for chunk in chunks:
        blob = chunk_search_blob(chunk)
        for term in terms:
            if term.lower() in blob:
                document_frequency[term.lower()] += 1
    total = len(chunks)
    return {
        term.lower(): round(math.log((total + 1) / (document_frequency[term.lower()] + 1)) + 1.0, 3)
        for term in terms
    }


def chunk_result(chunk: dict[str, Any], score: float, relation_edges: list[dict[str, Any]]) -> dict[str, Any]:
    text = str(chunk.get("normalized_text") or chunk.get("text") or chunk.get("retrieval_text") or "")
    return {
        "score": round(score, 3),
        "chunk_id": chunk.get("chunk_id"),
        "document_id": chunk.get("document_id"),
        "document_name": chunk.get("document_name"),
        "institution_name": chunk.get("institution_name"),
        "source_file": chunk.get("source_file"),
        "chunk_type": chunk.get("chunk_type"),
        "hierarchy_path": chunk.get("hierarchy_path"),
        "page_start": chunk.get("source_page_start"),
        "page_end": chunk.get("source_page_end"),
        "article_no": chunk.get("article_no"),
        "article_title": chunk.get("article_title"),
        "snippet": snippet(text),
        "quality_flags": chunk_quality_flags(chunk, text, relation_edges),
        "relation_edges": relation_edge_samples(relation_edges),
    }


def expected_term_hits(expected_terms: list[str], top_chunks: list[dict[str, Any]]) -> list[str]:
    hits: list[str] = []
    for chunk in top_chunks:
        for term in expected_term_hits_for_chunk(expected_terms, chunk):
            if term not in hits:
                hits.append(term)
    return hits


def expected_term_hits_for_chunk(expected_terms: list[str], chunk: dict[str, Any]) -> list[str]:
    parts = [
        str(chunk.get("document_name") or ""),
        str(chunk.get("institution_name") or ""),
        str(chunk.get("hierarchy_path") or ""),
        str(chunk.get("snippet") or ""),
        " ".join(str(edge.get("target_label") or "") for edge in chunk.get("relation_edges") or []),
        " ".join(str(edge.get("evidence_text") or "") for edge in chunk.get("relation_edges") or []),
    ]
    haystack = " ".join(parts).lower()
    return [term for term in expected_terms if str(term).lower() in haystack]


def relation_edge_samples(edges: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    priority = {
        "article_cites_regulation_article": 0,
        "chunk_cites_regulation_article": 0,
        "article_cites_law_article": 1,
        "chunk_cites_law_article": 1,
        "chunk_defines_inline_article": 2,
        "article_cites_article": 3,
        "article_cites_article_clause": 3,
    }
    sorted_edges = sorted(
        edges,
        key=lambda edge: (priority.get(str(edge.get("relation_type") or ""), 9), str(edge.get("target_label") or "")),
    )
    return [
        {
            "relation_type": edge.get("relation_type"),
            "target_label": edge.get("target_label"),
            "evidence_type": edge.get("evidence_type"),
            "evidence_text": edge.get("evidence_text"),
            "confidence": edge.get("confidence"),
        }
        for edge in sorted_edges[:limit]
    ]


def group_edges_by_chunk(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        chunk_id = str(edge.get("chunk_id") or "")
        if chunk_id:
            result[chunk_id].append(edge)
    return result


def chunk_search_blob(chunk: dict[str, Any]) -> str:
    return " ".join([chunk_body_blob(chunk), chunk_title_blob(chunk), chunk_metadata_blob(chunk)]).lower()


def chunk_body_blob(chunk: dict[str, Any]) -> str:
    values = [
        chunk.get("retrieval_text"),
        chunk.get("normalized_text"),
        chunk.get("text"),
        chunk.get("table_markdown"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def chunk_title_blob(chunk: dict[str, Any]) -> str:
    values = [
        chunk.get("hierarchy_path"),
        chunk.get("article_no"),
        chunk.get("article_title"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def chunk_metadata_blob(chunk: dict[str, Any]) -> str:
    values = [
        chunk.get("document_name"),
        chunk.get("institution_name"),
        chunk.get("source_file"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def chunk_quality_flags(
    chunk: dict[str, Any],
    text: str,
    relation_edges: list[dict[str, Any]],
) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    chunk_type = str(chunk.get("chunk_type") or "").lower()
    if chunk_type in REVIEW_CHUNK_TYPES:
        flags.append(
            {
                "code": "form_or_appendix_candidate",
                "severity": "review",
                "label": "별표/서식 근거 후보",
            }
        )
    if chunk.get("table_markdown") or any(
        str(edge.get("relation_type") or "").startswith("table_")
        for edge in relation_edges
    ):
        flags.append(
            {
                "code": "table_context_candidate",
                "severity": "review",
                "label": "표/행 근거 후보",
            }
        )
    relation_text = " ".join(
        str(edge.get(field) or "")
        for edge in relation_edges
        for field in ("target_label", "evidence_text")
    )
    if looks_garbled_text(" ".join([text, relation_text])):
        flags.append(
            {
                "code": "ocr_or_encoding_noise",
                "severity": "warning",
                "label": "OCR/인코딩 노이즈 의심",
            }
        )
    return flags


def looks_garbled_text(text: str) -> bool:
    return bool(SUSPICIOUS_TEXT_PATTERN.search(str(text or "")))


def tokenize(text: str) -> list[str]:
    return [
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣·ㆍ]+", str(text or ""))
        if len(token) >= 2
    ]


def snippet(text: str, *, max_chars: int = 260) -> str:
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def stable_query_id(question: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", question).strip("_").lower()
    return slug[:48] or "query"


def query_text(query: dict[str, Any]) -> str:
    return str(query.get("question") or query.get("query") or "").strip()


def load_relation_edges(path: Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    edges = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            edge = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid relation JSONL at {path}:{line_no}: {exc}") from exc
        if isinstance(edge, dict):
            edges.append(edge)
    return edges


def load_queries(path: Path | None) -> list[dict[str, Any]]:
    if not path:
        return list(DEFAULT_QUERIES)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("queries")
    if not isinstance(payload, list):
        raise ValueError("Query file must contain a list or an object with a 'queries' list.")
    return [query for query in payload if isinstance(query, dict) and query_text(query)]


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# RAG Retrieval Evaluation",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Source mode: {report.get('source_mode')}",
        f"- Runtime: {report.get('effective_runtime_data_dir') or '-'}",
        f"- Source chunks: {report.get('source_chunk_count', '-')}",
        f"- Queries: {report.get('query_count')}",
        f"- Answerable: {report.get('answerable_count')} / {report.get('answerable_query_count')} ({report.get('answerable_ratio')})",
        f"- No-evidence controls: {report.get('no_evidence_passed_count')} passed / {report.get('expect_no_evidence_query_count')} total",
        f"- Relation-supported: {report.get('relation_supported_count')} ({report.get('relation_supported_ratio')})",
        f"- Quality-flagged chunks: {report.get('quality_flag_chunk_count', 0)}",
        f"- Warning chunks: {report.get('quality_warning_chunk_count', 0)}",
        f"- API calls: {report.get('api_call_count')}",
        "",
        "## Query Results",
    ]
    for result in report.get("results") or []:
        lines.extend(
            [
                "",
                f"### {result.get('id')}",
                "",
                f"- Question: {result.get('question')}",
                f"- Expected no evidence: {str(bool(result.get('expect_no_evidence'))).lower()}",
                f"- Answerable: {result.get('answerable')}",
                f"- No-evidence passed: {result.get('no_evidence_passed')}",
                f"- Expected term hit ratio: {result.get('expected_term_hit_ratio')}",
                f"- Hits: {', '.join(result.get('expected_term_hits') or [])}",
                f"- Relation edge count in top chunks: {result.get('relation_edge_count')}",
                f"- Quality flags: {format_counter(result.get('quality_flag_counts') or {})}",
                "",
                "| Rank | Score | Document | Page | Chunk | Flags | Snippet |",
                "|---:|---:|---|---:|---|---|---|",
            ]
        )
        for rank, chunk in enumerate(result.get("top_chunks") or [], start=1):
            lines.append(
                "| {rank} | {score} | {doc} | {page} | `{chunk_id}` | {flags} | {snippet} |".format(
                    rank=rank,
                    score=chunk.get("score"),
                    doc=escape_md(str(chunk.get("document_name") or "")),
                    page=chunk.get("page_start") or "",
                    chunk_id=chunk.get("chunk_id") or "",
                    flags=escape_md(", ".join(flag["code"] for flag in chunk.get("quality_flags") or [])),
                    snippet=escape_md(str(chunk.get("snippet") or "")),
                )
            )
        relation_lines = []
        for chunk in result.get("top_chunks") or []:
            for edge in chunk.get("relation_edges") or []:
                relation_lines.append(
                    f"- `{chunk.get('chunk_id')}`: {edge.get('relation_type')} -> {edge.get('target_label')} ({edge.get('evidence_text')})"
                )
        if relation_lines:
            lines.extend(["", "Relation samples:", *relation_lines[:12]])
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def escape_md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def format_counter(counter: dict[str, Any]) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items()))


def load_batch_chunks_from_reports(batch_report_paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    batch_reports = []
    chunks = []
    for path in batch_report_paths:
        report = load_json(path)
        batch_reports.append(report)
        chunks.extend(_iter_batch_chunks(report, batch_report_path=path))
    return chunks, batch_reports


def load_runtime_chunks(
    runtime_data_dir: Path,
    *,
    tenant_id: str,
    tenant_storage_isolation: bool | None = None,
    approved_only: bool = False,
) -> tuple[list[dict[str, Any]], Path, bool]:
    isolation = tenant_storage_isolation
    if isolation is None:
        isolation = runtime_data_dir.joinpath("tenants", tenant_storage_key(tenant_id)).is_dir()
    effective_dir = settings_for_tenant(
        Settings(data_dir=runtime_data_dir, tenant_storage_isolation=isolation),
        tenant_id,
    ).data_dir
    repository_dir = effective_dir / "repository"
    chunks: list[dict[str, Any]] = []
    for path in sorted(repository_dir.glob("*_chunks.json")) if repository_dir.exists() else []:
        payload = load_json(path)
        if isinstance(payload, list):
            chunks.extend(chunk for chunk in payload if isinstance(chunk, dict))
    chunks = [chunk for chunk in chunks if _chunk_visible_to_tenant(chunk, tenant_id)]
    if approved_only:
        chunks = [chunk for chunk in chunks if _approval_status(chunk) == "APPROVED"]
    return chunks, effective_dir, isolation


def _chunk_visible_to_tenant(chunk: dict[str, Any], tenant_id: str) -> bool:
    metadata = _metadata(chunk)
    chunk_tenant = str(chunk.get("tenant_id") or metadata.get("tenant_id") or "").strip()
    if tenant_id == "default":
        return chunk_tenant in {"", "default"}
    return chunk_tenant == tenant_id


def _approval_status(chunk: dict[str, Any]) -> str:
    metadata = _metadata(chunk)
    return str(chunk.get("approval_status") or metadata.get("approval_status") or "").strip().upper()


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def single_or_list(values: list[Any]) -> Any:
    return values[0] if len(values) == 1 else values


def query_spec_metadata(path: Path | None, queries: list[dict[str, Any]]) -> dict[str, Any]:
    if path is None:
        return {
            "query_spec_path": "",
            "query_spec_sha256": "",
            "query_spec_byte_count": None,
            "query_spec_item_count": None,
        }
    data = path.read_bytes()
    return {
        "query_spec_path": str(path),
        "query_spec_sha256": hashlib.sha256(data).hexdigest(),
        "query_spec_byte_count": len(data),
        "query_spec_item_count": len(queries),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate offline RAG retrieval evidence from batch chunks.")
    parser.add_argument("--batch-report", action="append", default=[])
    parser.add_argument("--runtime-data-dir", default=None)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--no-tenant-storage-isolation", action="store_true")
    parser.add_argument("--approved-only", action="store_true")
    parser.add_argument("--relation-graph", default=None)
    parser.add_argument("--queries-json", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    batch_report_paths = [Path(value) for value in args.batch_report]
    out_json = Path(args.out_json) if args.out_json else Path("reports") / f"rag_retrieval_eval_{timestamp}.json"
    out_md = Path(args.out_md) if args.out_md else out_json.with_suffix(".md")
    try:
        if bool(batch_report_paths) == bool(args.runtime_data_dir):
            raise ValueError("Provide exactly one of --batch-report or --runtime-data-dir.")
        batch_reports: list[dict[str, Any]] = []
        runtime_effective_dir: Path | None = None
        runtime_isolation: bool | None = None
        if batch_report_paths:
            chunks, batch_reports = load_batch_chunks_from_reports(batch_report_paths)
            source_mode = "batch_report"
        else:
            tenant_storage_isolation = None
            if args.tenant_storage_isolation:
                tenant_storage_isolation = True
            if args.no_tenant_storage_isolation:
                tenant_storage_isolation = False
            chunks, runtime_effective_dir, runtime_isolation = load_runtime_chunks(
                Path(args.runtime_data_dir),
                tenant_id=args.tenant_id,
                tenant_storage_isolation=tenant_storage_isolation,
                approved_only=args.approved_only,
            )
            source_mode = "runtime"
        edges = load_relation_edges(Path(args.relation_graph) if args.relation_graph else None)
        queries_path = Path(args.queries_json) if args.queries_json else None
        queries = load_queries(queries_path)
        report = evaluate_retrieval(chunks, edges, queries, top_k=args.top_k)
        report.update(query_spec_metadata(queries_path, queries))
        report["source_mode"] = source_mode
        report["source_batch_report_file"] = single_or_list([path.name for path in batch_report_paths]) if batch_report_paths else None
        report["source_batch_report_files"] = [path.name for path in batch_report_paths]
        report["runtime_data_dir"] = str(Path(args.runtime_data_dir)) if args.runtime_data_dir else None
        report["effective_runtime_data_dir"] = str(runtime_effective_dir) if runtime_effective_dir else None
        report["tenant_id"] = args.tenant_id if args.runtime_data_dir else None
        report["tenant_storage_isolation"] = runtime_isolation
        report["approved_only"] = bool(args.approved_only) if args.runtime_data_dir else None
        report["source_chunk_count"] = len(chunks)
        report["source_relation_graph_file"] = Path(args.relation_graph).name if args.relation_graph else None
        report["source_batch_generated_at"] = (
            single_or_list([batch_report.get("generated_at") for batch_report in batch_reports])
            if batch_reports
            else None
        )
        report["out_json"] = str(out_json)
        report["out_md"] = str(out_md)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(out_md, report)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **{key: report[key] for key in ['query_count', 'answerable_query_count', 'answerable_count', 'answerable_ratio', 'expect_no_evidence_query_count', 'no_evidence_passed_count', 'no_evidence_failed_count', 'relation_supported_count', 'relation_supported_ratio', 'api_call_count', 'out_json', 'out_md']}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
