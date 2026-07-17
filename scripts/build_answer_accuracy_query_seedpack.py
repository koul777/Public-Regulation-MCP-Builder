"""Build an expanded answer-accuracy query seedpack from approved vectors."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TYPE_ORDER = ("article", "appendix", "form", "table", "supplementary_provision", "paragraph")
ARTICLE_REF_PATTERN = re.compile(
    r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?"
)
RELATED_UNIT_PATTERNS = (
    (re.compile(r"별지\s*제?\s*(\d+(?:의\s*\d+)?)\s*호\s*서식"), "별지제{}호서식"),
    (re.compile(r"별표\s*제?\s*(\d+(?:의\s*\d+)?)"), "별표{}"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            text = line.strip()
            if text:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "query",
        "expect_no_evidence",
        "expected_terms",
        "expected_article_nos",
        "expected_article_titles",
        "target_chunk_id",
        "target_chunk_type",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            item = dict(row)
            for key in ("expected_terms", "expected_article_nos", "expected_article_titles"):
                item[key] = "; ".join(str(value) for value in item.get(key) or [])
            writer.writerow(item)


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _unique(values: list[str], *, limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
        if len(result) >= limit:
            break
    return result


def _record_sort_key(record: dict[str, Any]) -> tuple[int, str, str]:
    metadata = _dict(record.get("metadata"))
    chunk_type = _clean(metadata.get("chunk_type"))
    try:
        type_rank = TYPE_ORDER.index(chunk_type)
    except ValueError:
        type_rank = len(TYPE_ORDER)
    return (type_rank, _clean(metadata.get("article_no") or metadata.get("table_appendix_no")), _clean(record.get("chunk_id")))


def _metadata_richness(record: dict[str, Any]) -> int:
    metadata = _dict(record.get("metadata"))
    fields = [
        metadata.get("article_no"),
        metadata.get("article_title"),
        metadata.get("direct_article_no"),
        metadata.get("direct_article_title"),
        metadata.get("table_citation_label"),
        metadata.get("table_appendix_no"),
        *_list(metadata.get("article_refs")),
        *_list(metadata.get("appendix_refs")),
        *_list(metadata.get("form_refs")),
    ]
    return sum(1 for value in fields if _clean(value))


def _normalized_article_ref(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _label_governing_article_refs(record: dict[str, Any]) -> list[str]:
    """Return governing refs stated in a form/appendix label, not refs in its body."""
    metadata = _dict(record.get("metadata"))
    sources = [
        metadata.get("table_citation_label"),
        metadata.get("hierarchy_path"),
    ]
    values: list[str] = []
    for source in sources:
        text = _clean(source)
        if not text:
            continue
        related_sections = re.findall(r"\(([^)]*관련[^)]*)\)", text)
        for section in related_sections:
            values.extend(
                _normalized_article_ref(match.group(0))
                for match in ARTICLE_REF_PATTERN.finditer(section)
            )
    return _unique(values, limit=3)


def _related_unit_identity(record: dict[str, Any]) -> str:
    """Normalize split chunks that belong to one logical appendix or form."""
    metadata = _dict(record.get("metadata"))
    sources = [
        *_list(metadata.get("form_refs")),
        *_list(metadata.get("appendix_refs")),
        metadata.get("table_appendix_no"),
        metadata.get("table_citation_label"),
        metadata.get("hierarchy_path"),
        record.get("chunk_id"),
    ]
    for source in sources:
        text = _clean(source)
        if not text:
            continue
        for pattern, template in RELATED_UNIT_PATTERNS:
            match = pattern.search(text)
            if match:
                number = re.sub(r"\s+", "", match.group(1))
                return template.format(number)
    return ""


def _candidate_representative_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """Prefer a logical unit's header over richer internal body chunks."""
    metadata = _dict(record.get("metadata"))
    has_governing_label = bool(_label_governing_article_refs(record))
    has_unit_ref = bool(
        _list(metadata.get("form_refs")) or _list(metadata.get("appendix_refs"))
    )
    has_citation_label = bool(_clean(metadata.get("table_citation_label")))
    return (
        -int(has_governing_label),
        -int(has_unit_ref),
        -int(has_citation_label),
        -_metadata_richness(record),
        _record_sort_key(record),
    )


def _expected_terms(record: dict[str, Any]) -> list[str]:
    metadata = _dict(record.get("metadata"))
    chunk_type = _clean(metadata.get("chunk_type"))
    if chunk_type == "article":
        values = [
            metadata.get("article_no"),
            metadata.get("article_title"),
            metadata.get("direct_article_no"),
            metadata.get("direct_article_title"),
        ]
    elif chunk_type in {"appendix", "form", "table"}:
        label_governing_refs = _label_governing_article_refs(record)
        fallback_article_refs = [] if label_governing_refs else _list(metadata.get("article_refs"))[:1]
        values = [
            metadata.get("table_citation_label"),
            metadata.get("table_appendix_no"),
            *label_governing_refs[:1],
            *fallback_article_refs,
            *_list(metadata.get("appendix_refs"))[:1],
            *_list(metadata.get("form_refs"))[:1],
        ]
    elif chunk_type == "supplementary_provision":
        values = [*_list(metadata.get("article_refs"))[:2], metadata.get("article_no")]
    else:
        values = [metadata.get("article_no"), metadata.get("article_title")]
    terms = _unique([str(value) for value in values if value], limit=4)
    if terms:
        return terms
    return _unique([str(metadata.get("regulation_title") or "")], limit=1)


def _query_for_record(record: dict[str, Any]) -> str:
    metadata = _dict(record.get("metadata"))
    chunk_type = _clean(metadata.get("chunk_type"))
    regulation = _clean(metadata.get("regulation_title")) or "해당 규정"
    article_no = _clean(metadata.get("article_no") or metadata.get("direct_article_no"))
    article_title = _clean(metadata.get("article_title") or metadata.get("direct_article_title"))
    appendix_title = _clean(metadata.get("table_appendix_title"))
    article_refs = [_clean(value) for value in _list(metadata.get("article_refs")) if _clean(value)]
    label = _clean(
        metadata.get("table_citation_label")
        or metadata.get("table_appendix_no")
        or _related_unit_identity(record)
    )
    if chunk_type == "article" and article_no:
        title = f"({article_title})" if article_title else ""
        return f"{regulation} {article_no}{title}의 핵심 내용과 적용 조건은 무엇인가?"
    if chunk_type in {"appendix", "table", "form"} and label:
        if appendix_title and appendix_title not in {label, regulation}:
            label = f"{label} {appendix_title}"
        elif article_title and article_title not in {label, regulation}:
            label = f"{label} {article_title}"
        governing_refs = _label_governing_article_refs(record) or article_refs[:1]
        if governing_refs and governing_refs[0] not in label:
            label = f"{label} ({governing_refs[0]} 관련)"
        return f"{regulation}의 {label}에는 어떤 기준이나 항목이 정리되어 있는가?"
    if chunk_type == "supplementary_provision":
        if article_refs:
            return f"{regulation}의 부칙에서 {article_refs[0]} 관련 시행일 또는 적용 내용은 무엇인가?"
        return f"{regulation}의 부칙 또는 시행일 관련 내용은 무엇인가?"
    return f"{regulation}에서 이 조각이 설명하는 업무 기준은 무엇인가?"


def _query_id(prefix: str, record: dict[str, Any], index: int) -> str:
    chunk_id = _clean(record.get("chunk_id"))
    suffix = hashlib.sha1(chunk_id.encode("utf-8")).hexdigest()[:8] if chunk_id else f"{index:03d}"
    return f"{prefix}_{index:03d}_{suffix}"


def _candidate_spec(record: dict[str, Any], index: int) -> dict[str, Any]:
    metadata = _dict(record.get("metadata"))
    expected_article_nos = _expected_article_nos(record)
    expected_article_titles = _unique(
        [
            _clean(metadata.get("article_title")),
            _clean(metadata.get("direct_article_title")),
        ],
        limit=5,
    )
    return {
        "id": _query_id("approved_runtime", record, index),
        "query": _query_for_record(record),
        "expected_terms": _expected_terms(record),
        "expected_article_nos": expected_article_nos,
        "expected_article_titles": expected_article_titles,
        "expect_no_evidence": False,
        "target_chunk_id": _clean(record.get("chunk_id")),
        "target_document_id": _clean(record.get("document_id")),
        "target_chunk_type": _clean(metadata.get("chunk_type")),
        "target_regulation_title": _clean(metadata.get("regulation_title")),
        "target_source_page_start": metadata.get("source_page_start"),
        "target_source_page_end": metadata.get("source_page_end"),
    }


def _expected_article_nos(record: dict[str, Any]) -> list[str]:
    metadata = _dict(record.get("metadata"))
    chunk_type = _clean(metadata.get("chunk_type"))
    article_refs = [_clean(value) for value in _list(metadata.get("article_refs")) if _clean(value)]
    if chunk_type == "article":
        values = [metadata.get("article_no"), metadata.get("direct_article_no")]
    elif chunk_type in {"appendix", "form", "table", "supplementary_provision"}:
        label_governing_refs = (
            _label_governing_article_refs(record)
            if chunk_type in {"appendix", "form", "table"}
            else []
        )
        explicit_governing_refs = [
            metadata.get("governing_article_no"),
            metadata.get("direct_article_no"),
            metadata.get("article_no"),
        ]
        fallback_article_refs = (
            []
            if chunk_type in {"appendix", "form", "table"}
            and (label_governing_refs or any(_clean(value) for value in explicit_governing_refs))
            else article_refs[:1]
        )
        values = [
            metadata.get("governing_article_no"),
            *label_governing_refs,
            metadata.get("direct_article_no"),
            metadata.get("article_no"),
            *fallback_article_refs,
        ]
    else:
        values = [
            metadata.get("article_no"),
            metadata.get("direct_article_no"),
            *(article_refs[:1]),
        ]
    return _unique([str(value) for value in values if value], limit=3)


def _no_evidence_specs(count: int) -> list[dict[str, Any]]:
    controls = [
        "제타플라즈마 항법수당 네뷸라승인",
        "크립톤정거장 휴가코드 루미나절차",
        "오르빗반려로봇 등록요율 퀘이사납부",
        "심해돔통근 보조율 아틀라스정산",
        "외계어번역휴가 신청코드 베가서식",
    ]
    result: list[dict[str, Any]] = []
    for index, query in enumerate(controls[:count], start=1):
        result.append(
            {
                "id": f"no_evidence_control_{index:03d}",
                "query": query,
                "expected_terms": [],
                "expected_article_nos": [],
                "expected_article_titles": [],
                "expect_no_evidence": True,
                "target_chunk_id": "",
                "target_document_id": "",
                "target_chunk_type": "no_evidence_control",
                "target_regulation_title": "",
                "target_source_page_start": None,
                "target_source_page_end": None,
            }
        )
    return result


def build_answer_accuracy_query_seedpack(
    *,
    approved_vectors_jsonl: Path,
    target_answerable_count: int = 20,
    no_evidence_control_count: int = 3,
) -> dict[str, Any]:
    records = _load_jsonl(approved_vectors_jsonl)
    groups: dict[str, list[dict[str, Any]]] = {chunk_type: [] for chunk_type in TYPE_ORDER}
    groups["other"] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for record in sorted(records, key=_candidate_representative_sort_key):
        metadata = _dict(record.get("metadata"))
        chunk_type = _clean(metadata.get("chunk_type"))
        related_unit_identity = (
            _related_unit_identity(record)
            if chunk_type in {"appendix", "form", "table"}
            else ""
        )
        if related_unit_identity:
            key = (
                chunk_type,
                _clean(record.get("document_id")),
                related_unit_identity,
            )
        else:
            key = (
                chunk_type,
                _clean(metadata.get("article_no") or metadata.get("table_citation_label")),
                _clean(metadata.get("article_title") or metadata.get("table_appendix_no")),
            )
        if not any(key):
            key = (chunk_type, _clean(record.get("chunk_id")), "")
        if key in seen_keys and chunk_type in {"article", "appendix", "form", "table"}:
            continue
        terms = _expected_terms(record)
        if not terms:
            continue
        seen_keys.add(key)
        groups.setdefault(chunk_type if chunk_type in TYPE_ORDER else "other", []).append(record)

    for bucket in groups.values():
        bucket.sort(key=lambda record: (-_metadata_richness(record), _record_sort_key(record)))

    selected: list[dict[str, Any]] = []
    while len(selected) < target_answerable_count:
        progressed = False
        for chunk_type in (*TYPE_ORDER, "other"):
            bucket = groups.get(chunk_type) or []
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            progressed = True
            if len(selected) >= target_answerable_count:
                break
        if not progressed:
            break

    answerable_specs = [
        _candidate_spec(record, index) for index, record in enumerate(selected, start=1)
    ]
    no_evidence_specs = _no_evidence_specs(no_evidence_control_count)
    query_specs = answerable_specs + no_evidence_specs
    type_counts = Counter(spec.get("target_chunk_type") for spec in answerable_specs)
    return {
        "report_type": "answer_accuracy_query_seedpack",
        "generated_at": _utc_now(),
        "source_approved_vectors_jsonl": str(approved_vectors_jsonl),
        "source_approved_vectors_sha256": _sha256(approved_vectors_jsonl),
        "source_record_count": len(records),
        "target_answerable_count": target_answerable_count,
        "answerable_query_count": len(answerable_specs),
        "no_evidence_control_count": len(no_evidence_specs),
        "query_spec_count": len(query_specs),
        "answerable_chunk_type_counts": dict(sorted(type_counts.items())),
        "query_specs": query_specs,
        "safety_note": (
            "This seedpack is read-only. It prepares candidate query specs but does not execute "
            "retrieval, approve chunks, or write Vector DB records."
        ),
        "api_call_count": 0,
    }


def render_markdown(report: dict[str, Any], *, query_spec_json: Path, query_spec_csv: Path) -> str:
    lines = [
        "# Answer Accuracy Query Seedpack",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Source records: {report.get('source_record_count')}",
        f"- Query specs: {report.get('query_spec_count')}",
        f"- Answerable / no-evidence controls: {report.get('answerable_query_count')} / {report.get('no_evidence_control_count')}",
        f"- Query spec JSON: `{query_spec_json}`",
        f"- Query spec CSV: `{query_spec_csv}`",
        f"- Chunk types: {report.get('answerable_chunk_type_counts')}",
        "",
        "## First Queries",
        "",
    ]
    for spec in report.get("query_specs", [])[:10]:
        if not isinstance(spec, dict):
            continue
        lines.append(
            f"- `{spec.get('id')}` ({spec.get('target_chunk_type')}): {spec.get('query')}"
        )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-vectors-jsonl", required=True, type=Path)
    parser.add_argument("--target-answerable-count", type=int, default=20)
    parser.add_argument("--no-evidence-control-count", type=int, default=3)
    parser.add_argument("--out-report-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--out-query-spec-json", required=True, type=Path)
    parser.add_argument("--out-query-spec-csv", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_answer_accuracy_query_seedpack(
        approved_vectors_jsonl=args.approved_vectors_jsonl,
        target_answerable_count=args.target_answerable_count,
        no_evidence_control_count=args.no_evidence_control_count,
    )
    _write_json(args.out_report_json, report)
    _write_json(args.out_query_spec_json, report["query_specs"])
    _write_csv(args.out_query_spec_csv, [dict(item) for item in report["query_specs"]])
    _write_text(
        args.out_md,
        render_markdown(
            report,
            query_spec_json=args.out_query_spec_json,
            query_spec_csv=args.out_query_spec_csv,
        ),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
