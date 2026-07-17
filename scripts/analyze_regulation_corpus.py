from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


def utc_generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


RULE_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "obligation": [
        ("shall_do", r"(하여야 한다|해야 한다|하여야 하며|해야 하며|하여야 한다\.)"),
        ("must_submit", r"(제출하여야|보고하여야|신청하여야|통보하여야|준수하여야)"),
    ],
    "prohibition": [
        ("must_not", r"(하여서는 아니 된다|해서는 아니 된다|할 수 없다|금지한다|금지하여야)"),
        ("restriction", r"(제한할 수 있다|제한한다|배제한다)"),
    ],
    "permission": [
        ("may_do", r"(할 수 있다|둘 수 있다|정할 수 있다|요청할 수 있다|위탁할 수 있다)"),
        ("discretion", r"(필요하다고 인정하는 경우|필요한 경우에는)"),
    ],
    "definition": [
        ("definition_term", r"(\S+이란|\S+란|뜻은 다음과 같다|정의한다)"),
        ("means", r"(말한다|의미한다)"),
    ],
    "procedure": [
        ("approval", r"(승인|허가|인가|결재|심의|의결|협의)"),
        ("submission", r"(신청|제출|보고|통보|공고|공시)"),
    ],
    "delegation": [
        ("according_to", r"(정하는 바에 따라|따로 정한다|시행세칙|세부사항)"),
        ("authority", r"(원장|이사회|위원회|부서장|기관장).{0,20}(정한다|승인한다|위임한다)"),
    ],
    "exception": [
        ("except_clause", r"(다만|예외|제외한다|불구하고|특별한 사유)"),
    ],
    "revision": [
        ("revision_tag", r"(<개정\s*\d{4}\.\d{1,2}\.\d{1,2}\.?|개정\s*\d{4}\.)"),
        ("effective_date", r"(시행한다|시행일|부터 시행|적용한다)"),
    ],
    "reference": [
        ("article_reference", r"제\s*\d+\s*조(?:의\s*\d+)?(?:제\s*\d+\s*항)?"),
        ("appendix_reference", r"(별표\s*\d*|별지\s*제?\s*\d*\s*호?)"),
    ],
}

FOOTNOTE_CAPTION_KEYWORD_RE = re.compile(r"각주|미주|캡션|caption", re.IGNORECASE)
FOOTNOTE_CAPTION_LINE_RE = re.compile(
    r"^\s*(?:(?:[\[【<〈(]\s*)?(?:표|그림)\s*\d+\s*(?:[\.\-\):：\]】>〉)]|$)|"
    r"(?:각주|캡션|caption)\b|미주\s*(?:\d+|[\.\-\):：]|$))",
    re.IGNORECASE,
)


@dataclass
class CorpusPaths:
    workspace: Path
    document_id: str
    jsonl: Path
    repository: Path
    repository_dir: Path
    reports: Path


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_repository(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_result(paths: CorpusPaths, result_type: str) -> Any:
    split_path = paths.repository_dir / f"{paths.document_id}_{result_type}.json"
    if split_path.exists():
        return json.loads(split_path.read_text(encoding="utf-8"))
    legacy = load_repository(paths.repository)
    return legacy.get(result_type, {}).get(paths.document_id, [] if result_type != "quality" else None)


def text_len(row: dict[str, Any]) -> int:
    return len(str(row.get("text") or ""))


def snippet(text: str, max_chars: int = 180) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:max_chars]


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def has_independent_footnote_caption_marker(text: str) -> bool:
    for raw_line in str(text or "").splitlines() or [str(text or "")]:
        line = raw_line.strip()
        if not line or line.startswith("|"):
            continue
        if FOOTNOTE_CAPTION_LINE_RE.match(line):
            return True
    return False


def find_rule_candidates(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in chunks:
        text = str(row.get("text") or "")
        normalized = str(row.get("normalized_text") or text)
        for category, patterns in RULE_PATTERNS.items():
            for signal, pattern in patterns:
                match = re.search(pattern, normalized)
                if not match:
                    continue
                if signal == "article_reference" and is_self_article_reference(row, match):
                    continue
                candidates.append(
                    {
                        "chunk_id": row.get("chunk_id"),
                        "category": category,
                        "signal": signal,
                        "matched_text": match.group(0),
                        "document_name": row.get("document_name"),
                        "source_file": row.get("source_file"),
                        "page_start": row.get("source_page_start"),
                        "page_end": row.get("source_page_end"),
                        "chunk_type": row.get("chunk_type"),
                        "part_no": row.get("part_no"),
                        "part_title": row.get("part_title"),
                        "chapter_no": row.get("chapter_no"),
                        "chapter_title": row.get("chapter_title"),
                        "article_no": row.get("article_no"),
                        "article_title": row.get("article_title"),
                        "hierarchy_path": row.get("hierarchy_path"),
                        "snippet": snippet(text),
                    }
                )
                break
    return candidates


def is_self_article_reference(row: dict[str, Any], match: re.Match[str]) -> bool:
    article_no = normalize_article_no(str(row.get("article_no") or ""))
    matched = normalize_article_no(match.group(0))
    return bool(article_no and matched == article_no and match.start() <= 20)


def normalize_article_no(value: str) -> str:
    match = re.search(r"제\s*(\d+)\s*조(?:의\s*(\d+))?", value)
    if not match:
        return ""
    suffix = f"의{match.group(2)}" if match.group(2) else ""
    return f"제{match.group(1)}조{suffix}"


def summarize_issues(raw_issues: list[dict[str, Any]], raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = {node.get("node_id"): node for node in raw_nodes}
    enriched: list[dict[str, Any]] = []
    for issue in raw_issues:
        node = nodes.get(issue.get("target_id"), {})
        parent = nodes.get(node.get("parent_id"), {})
        enriched.append(
            {
                "issue_id": issue.get("issue_id"),
                "severity": issue.get("severity"),
                "issue_type": issue.get("issue_type"),
                "message": issue.get("message"),
                "target_id": issue.get("target_id"),
                "node_type": node.get("node_type"),
                "number": node.get("number"),
                "title": node.get("title"),
                "page_start": node.get("page_start"),
                "parent_type": parent.get("node_type"),
                "parent_number": parent.get("number"),
                "parent_title": parent.get("title"),
                "snippet": snippet(str(node.get("text") or "")),
            }
        )
    return enriched


def summarize_metadata_coverage(chunks: list[dict[str, Any]]) -> dict[str, int]:
    fields = [
        "references",
        "article_refs",
        "appendix_refs",
        "form_refs",
        "external_law_refs",
        "revision_events",
        "effective_date",
        "revision_date",
    ]
    coverage: dict[str, int] = {}
    for field in fields:
        coverage[f"chunks_with_{field}"] = sum(1 for row in chunks if has_value(row.get(field)))
    return coverage


def summarize_table_metrics(chunks: list[dict[str, Any]]) -> dict[str, int | float]:
    table_like = [row for row in chunks if row.get("table_like")]
    classification_counts = Counter(row.get("table_classification") for row in chunks if row.get("table_classification"))
    cell_counts = [len(row.get("table_cell_rows") or []) for row in table_like]
    raw_counts = [len(row.get("table_rows") or []) for row in table_like]
    column_counts = [int(row.get("table_column_count") or 0) for row in table_like]
    confidences = [float(row.get("table_confidence") or 0.0) for row in table_like]
    chunks_with_cell_rows = sum(1 for count in cell_counts if count > 0)
    return {
        "table_like_chunks": len(table_like),
        "chunks_with_table_cell_rows": chunks_with_cell_rows,
        "table_like_without_cell_rows": len(table_like) - chunks_with_cell_rows,
        "table_cell_row_count": sum(cell_counts),
        "table_raw_row_count": sum(raw_counts),
        "max_table_column_count": max(column_counts) if column_counts else 0,
        "avg_table_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "probable_table_false_positive_chunks": sum(
            count for classification, count in classification_counts.items() if str(classification).startswith("probable_false_positive")
        ),
        "probable_table_extraction_failed_chunks": classification_counts.get("probable_table_extraction_failed", 0),
    }


def quality_summary(raw_quality: dict[str, Any] | None) -> dict[str, Any]:
    if not raw_quality:
        return {}
    return {
        "passed": raw_quality.get("passed"),
        "score": raw_quality.get("score"),
        "issue_count": raw_quality.get("issue_count"),
        "duplicate_chunk_id_count": raw_quality.get("duplicate_chunk_id_count"),
        "missing_required_metadata_count": raw_quality.get("missing_required_metadata_count"),
        "table_metrics": raw_quality.get("table_metrics") or {},
        "metadata_coverage": raw_quality.get("metadata_coverage") or {},
        "coverage_metrics": raw_quality.get("coverage_metrics") or {},
    }


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(value)


def percent(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def safe_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_bool_text(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return ""


def _cell_preview(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " | ".join(str(item) for item in value if str(item).strip())
    return ""


def chunk_meta(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def chunk_get(chunk: dict[str, Any], key: str, default: Any = None) -> Any:
    value = chunk.get(key)
    if has_value(value) or isinstance(value, bool):
        return value
    metadata = chunk_meta(chunk)
    value = metadata.get(key)
    if has_value(value) or isinstance(value, bool):
        return value
    return default


VISIBLE_PARAGRAPH_ITEM_LINE_MARKER_RE = re.compile(
    r"^\s*(?:[\u2460-\u2473]|\d+\.\s+|[\uac00\ub098\ub2e4\ub77c\ub9c8\ubc14\uc0ac\uc544\uc790\ucc28\uce74\ud0c0\ud30c\ud558]\.\s+|\(\d+\)\s+)"
)
HWPX_ATTACHMENT_HEADING_RE = re.compile(
    r"[\[<\u3008]?\s*((?:\ubcc4\ud45c|\ubcc4\uc9c0)\s*(?:\uc81c\s*)?\d+\s*(?:\ud638)?(?:\s*\uc11c\uc2dd)?|\uc11c\uc2dd\s*(?:\uc81c\s*)?\d+\s*\ud638)",
    re.IGNORECASE,
)


def strip_location_preamble(text: str) -> str:
    lines = text.splitlines()
    body_lines: list[str] = []
    in_body = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[본문]":
            in_body = True
            continue
        if not in_body and stripped.startswith("[위치]"):
            continue
        if not in_body and stripped.startswith("[문서명]"):
            continue
        body_lines.append(line)
    return "\n".join(body_lines).strip()


def review_body_text(chunk: dict[str, Any]) -> str:
    normalized = str(chunk.get("normalized_text") or "").strip()
    if normalized:
        return normalized
    text = str(chunk.get("text") or "")
    return strip_location_preamble(text) or text.strip()


def normalized_extension(row: dict[str, Any]) -> str:
    filename = str(row.get("filename") or row.get("input_path") or row.get("source_file") or "")
    suffix = Path(filename).suffix.lower()
    return suffix if suffix else "unknown"


def normalized_chunk_type(chunk: dict[str, Any]) -> str:
    value = chunk_get(chunk, "chunk_type", "unknown")
    return str(value or "unknown")


def normalized_hwpx_attachment_key(chunk: dict[str, Any]) -> str:
    metadata = chunk_meta(chunk)
    candidates = [
        metadata.get("table_appendix_no"),
        metadata.get("table_citation_label"),
        metadata.get("appendix_no"),
        metadata.get("form_no"),
        metadata.get("table_appendix_title"),
        metadata.get("appendix_title"),
        metadata.get("form_title"),
        chunk.get("normalized_text"),
        chunk.get("text"),
    ]
    for candidate in candidates:
        text = str(candidate or "")
        match = HWPX_ATTACHMENT_HEADING_RE.search(text[:500])
        if not match:
            continue
        return re.sub(r"\s+", "", match.group(1)).lower()
    return ""


def visible_article_body_paragraph_item_count(chunk: dict[str, Any]) -> int:
    if normalized_chunk_type(chunk) != "article":
        return 0
    count = 0
    for raw_line in review_body_text(chunk).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|"):
            continue
        if VISIBLE_PARAGRAPH_ITEM_LINE_MARKER_RE.match(line):
            count += 1
    return count


def paragraph_item_chunk_count(chunks: list[dict[str, Any]]) -> int:
    total = 0
    for chunk in chunks:
        is_paragraph_item = normalized_chunk_type(chunk) in {"paragraph", "item", "subitem", "clause"} or has_value(
            chunk_get(chunk, "paragraph_no")
        )
        if not is_paragraph_item:
            continue
        total += 1
    return total


def article_structural_child_paragraph_item_count(chunks: list[dict[str, Any]]) -> int:
    total = 0
    seen_articles: set[str] = set()
    for index, chunk in enumerate(chunks):
        if normalized_chunk_type(chunk) != "article":
            continue
        count = safe_int(chunk_get(chunk, "paragraph_item_unit_count"))
        if count <= 0:
            continue
        regulation_no = str(chunk_get(chunk, "regulation_no") or "").strip()
        article_no = str(chunk_get(chunk, "article_no") or "").strip()
        if article_no:
            key = f"{regulation_no}|{article_no}"
        else:
            key = str(chunk_get(chunk, "entity_id") or chunk.get("chunk_id") or f"article-{index}")
        if key in seen_articles:
            continue
        seen_articles.add(key)
        total += count
    return total


def article_traceable_paragraph_item_unit_count(chunks: list[dict[str, Any]]) -> int:
    total = 0
    seen_articles: set[str] = set()
    for index, chunk in enumerate(chunks):
        if normalized_chunk_type(chunk) != "article":
            continue
        count = safe_int(chunk_get(chunk, "paragraph_item_traceable_unit_count"))
        if count <= 0:
            unit_ids = chunk_get(chunk, "paragraph_item_unit_ids", [])
            if isinstance(unit_ids, list):
                count = len([item for item in unit_ids if has_value(item)])
        if count <= 0:
            continue
        regulation_no = str(chunk_get(chunk, "regulation_no") or "").strip()
        article_no = str(chunk_get(chunk, "article_no") or "").strip()
        if article_no:
            key = f"{regulation_no}|{article_no}"
        else:
            key = str(chunk_get(chunk, "entity_id") or chunk.get("chunk_id") or f"article-{index}")
        if key in seen_articles:
            continue
        seen_articles.add(key)
        total += count
    return total


def footnote_link_logical_unit_count(chunks: list[dict[str, Any]]) -> int:
    total = 0
    for chunk in chunks:
        links = chunk_get(chunk, "footnote_links", [])
        if isinstance(links, list):
            total += len([item for item in links if has_value(item)])
        elif has_value(links):
            total += 1
    return total


def footnote_marker_reference_unit_count(chunks: list[dict[str, Any]]) -> int:
    total = 0
    for chunk in chunks:
        marker_count = safe_int(chunk_get(chunk, "footnote_marker_reference_count"))
        if marker_count > 0:
            total += marker_count
            continue
        marker_references = chunk_get(chunk, "footnote_marker_references", [])
        if isinstance(marker_references, list):
            total += sum(safe_int(item.get("marker_count")) for item in marker_references if isinstance(item, dict))
    return total


HWPX_FOOTNOTE_CAPTION_FLAGS = {
    "caption",
    "footnote",
    "footnotes",
    "endnote",
    "endnotes",
    "image_caption",
    "table_caption",
}
HWPX_COMPLEX_STRUCTURE_FLAGS = {
    "nested_table",
    "table_image",
    "table_note",
    "merged_cell",
}
HWPX_COMPLEX_STRUCTURE_COUNT_KEYS = (
    "source_hwpx_nested_table_count",
    "source_hwpx_table_image_count",
    "source_hwpx_table_note_count",
    "source_hwpx_merged_cell_count",
)
PARSER_UNCERTAINTY_BLOCKING_RISKS = {"high", "critical"}
PARSER_UNCERTAINTY_REVIEW_RISKS = {"medium"}
SUPPLEMENTARY_TRANSITION_KEYWORDS = (
    "\uacbd\uacfc\uc870\uce58",  # transition measure
    "\uc801\uc6a9\ub840",  # application example
    "\ud2b9\ub840",  # special case
    "\uc885\uc804",  # previous rule
    "\uc2dc\ud589\ub2f9\uc2dc",  # at enforcement time
    "\uc2dc\ud589 \ub2f9\uc2dc",
    "\uc720\ud6a8\uae30\uac04",  # validity period
    "\ud3d0\uc9c0",  # repeal
)


def load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_workspace_path(workspace: Path, value: str | Path | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = workspace / path
    return path


def report_path(workspace: Path, value: str | Path | None) -> str:
    if not value:
        return ""
    raw = Path(value)
    try:
        resolved = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
        return str(resolved.relative_to(workspace.resolve()))
    except (OSError, ValueError):
        return raw.name if raw.is_absolute() else str(raw)


def _casefold_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _filename_lookup_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return Path(raw).name.casefold()


def _source_path_lookup_key(workspace: Path, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    try:
        resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        try:
            text = str(resolved.relative_to(workspace.resolve()))
        except ValueError:
            text = str(resolved)
    except (OSError, RuntimeError):
        text = raw
    return text.replace("\\", "/").lstrip("./").casefold()


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def pipeline_count_lookup_keys(workspace: Path, row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("document_id", "current_runtime_document_id"):
        value = _casefold_text(row.get(field))
        if value:
            _append_unique(keys, value)
            _append_unique(keys, f"document_id:{value}")
    for field in ("source_path", "input_path"):
        value = _source_path_lookup_key(workspace, row.get(field))
        if value:
            _append_unique(keys, f"source_path:{value}")
    for field in ("filename", "latest_file_name", "selected_latest_file"):
        value = _filename_lookup_key(row.get(field))
        if value:
            _append_unique(keys, f"filename:{value}")
    return keys


def _add_pipeline_index_alias(index: dict[str, dict[str, Any]], key: str, info: dict[str, Any]) -> None:
    existing = index.get(key)
    if not existing:
        index[key] = info
        return
    if existing.get("document_id") == info.get("document_id"):
        return
    index[key] = {
        "_ambiguous": True,
        "match_key": key,
        "document_ids": sorted(
            {
                str(existing.get("document_id") or ""),
                str(info.get("document_id") or ""),
            }
        ),
    }


def resolve_pipeline_count_info(
    workspace: Path,
    pipeline_index: dict[str, dict[str, Any]],
    label_row: dict[str, Any],
) -> dict[str, Any]:
    for key in pipeline_count_lookup_keys(workspace, label_row):
        info = pipeline_index.get(key)
        if not info or info.get("_ambiguous"):
            continue
        matched = dict(info)
        matched["match_key"] = key
        return matched
    return {}


def load_batch_reports(workspace: Path, paths: list[str | Path], reports_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    report_paths = [resolve_workspace_path(workspace, path) for path in paths]
    report_paths = [path for path in report_paths if path is not None]
    if not report_paths:
        report_paths = [select_default_batch_report(workspace, reports_dir)]
    reports: list[tuple[Path, dict[str, Any]]] = []
    for path in report_paths:
        if not path.exists():
            raise FileNotFoundError(f"Batch report not found: {path}")
        reports.append((path, load_json_file(path)))
    return reports


def select_default_batch_report(workspace: Path, reports_dir: Path) -> Path:
    base = reports_dir if reports_dir.is_absolute() else workspace / reports_dir
    candidates = sorted(base.glob("batch_quality_*.json"))
    if not candidates:
        raise FileNotFoundError(f"No batch_quality_*.json files found in {base}")

    def score(path: Path) -> tuple[int, int, float]:
        try:
            payload = load_json_file(path)
        except (OSError, json.JSONDecodeError):
            return (0, 0, path.stat().st_mtime)
        return (
            safe_int(payload.get("successful_count")),
            safe_int(payload.get("input_count")),
            path.stat().st_mtime,
        )

    return max(candidates, key=score)


def dedupe_batch_rows(reports: list[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows_by_document: dict[str, dict[str, Any]] = {}
    for report_path, payload in reports:
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            row["_batch_report"] = str(report_path)
            document_id = str(row.get("document_id") or "")
            if not document_id:
                continue
            existing = rows_by_document.get(document_id)
            if existing is None:
                rows_by_document[document_id] = row
                continue
            current_status_score = status_score(str(row.get("status") or ""))
            existing_status_score = status_score(str(existing.get("status") or ""))
            if (current_status_score, safe_int(row.get("chunk_count"))) >= (
                existing_status_score,
                safe_int(existing.get("chunk_count")),
            ):
                rows_by_document[document_id] = row
    return list(rows_by_document.values())


def status_score(status: str) -> int:
    if status == "completed":
        return 3
    if status == "skipped_unchanged":
        return 2
    if status:
        return 1
    return 0


def find_chunk_file(
    workspace: Path,
    document_id: str,
    document_row: dict[str, Any] | None = None,
) -> Path | None:
    chunk_filename = _document_chunk_filename(document_id)
    if not chunk_filename:
        return None
    workspace_root = workspace.resolve()
    direct_candidates = [
        workspace / "data" / "repository" / chunk_filename,
        workspace / "data" / "private_release_runtime" / "repository" / chunk_filename,
    ]
    for candidate in direct_candidates:
        safe_candidate = _safe_existing_file(candidate, workspace_root)
        if safe_candidate is not None:
            return safe_candidate
    if document_row:
        row_candidates = _row_runtime_chunk_candidates(workspace, document_id, document_row)
        if len(row_candidates) == 1:
            return row_candidates[0]
    matches = sorted(
        safe_candidate
        for candidate in (workspace / "data").glob(f"**/repository/{chunk_filename}")
        if (safe_candidate := _safe_existing_file(candidate, workspace_root)) is not None
    )
    return matches[0] if matches else None


def _document_chunk_filename(document_id: str) -> str:
    value = str(document_id or "").strip()
    if (
        not value
        or value in {".", ".."}
        or any(marker in value for marker in ("/", "\\", "\x00"))
    ):
        return ""
    return f"{value}_chunks.json"


def _safe_existing_file(path: Path, boundary: Path) -> Path | None:
    try:
        resolved_boundary = boundary.resolve()
        resolved_path = path.resolve()
        resolved_path.relative_to(resolved_boundary)
        if not resolved_path.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path


def _row_runtime_chunk_candidates(
    workspace: Path,
    document_id: str,
    document_row: dict[str, Any],
) -> list[Path]:
    candidates: list[Path] = []
    workspace_root = workspace.resolve()
    chunk_filename = _document_chunk_filename(document_id)
    if not chunk_filename:
        return candidates
    for field in ("quality_json", "quality_md", "tables_jsonl", "tables_csv", "agent_review_plan_json"):
        artifact_path = resolve_workspace_path(workspace, document_row.get(field))
        if artifact_path is None:
            continue
        safe_artifact = _safe_existing_file(artifact_path, workspace_root)
        if safe_artifact is None:
            continue
        runtime_root = safe_artifact.parent.parent
        candidate = _safe_existing_file(runtime_root / "repository" / chunk_filename, runtime_root)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def load_document_chunks(
    workspace: Path,
    document_id: str,
    document_row: dict[str, Any] | None = None,
) -> tuple[Path | None, list[dict[str, Any]]]:
    path = find_chunk_file(workspace, document_id, document_row)
    if not path:
        return None, []
    payload = load_json_file(path)
    if isinstance(payload, list):
        return path, [row for row in payload if isinstance(row, dict)]
    return path, []


def chunk_review_flags(chunk: dict[str, Any], document_row: dict[str, Any] | None = None) -> list[str]:
    flags: set[str] = set()
    metadata = chunk_meta(chunk)
    text = review_body_text(chunk)
    chunk_type = normalized_chunk_type(chunk)

    warnings = chunk.get("warnings") or metadata.get("warnings") or []
    if warnings and not low_risk_processor_warning_signal(chunk, warnings):
        flags.add("processor_warning_candidate")

    table_like = bool(chunk_get(chunk, "table_like", False))
    table_classification = str(chunk_get(chunk, "table_classification", "") or "")
    table_flags = normalized_table_review_flags(chunk)
    if table_like or table_classification or table_flags or chunk_type == "table":
        flags.add("table_context_candidate")
    if chunk_get(chunk, "table_review_required", False) or table_flags:
        flags.add("table_review_required")
        flags.update(table_flags)
    if chunk_get(chunk, "table_probable_false_positive", False) or table_classification.startswith("probable_false_positive"):
        flags.add("table_false_positive_candidate")
    if chunk_get(chunk, "table_probable_extraction_failed", False) or table_classification == "probable_table_extraction_failed":
        flags.add("table_extraction_failed_candidate")
    hwp_extraction_modes = metadata.get("source_hwp_extraction_modes") or []
    hwp_native_table_geometry = metadata.get("source_hwp_native_table_geometry")
    if hwp_extraction_modes and hwp_native_table_geometry is False and (
        "table_context_candidate" in flags or chunk_type == "table"
    ):
        flags.add("hwp_binary_table_geometry_candidate")
    parser_uncertainty_risk = parser_uncertainty_risk_level(metadata)
    if parser_uncertainty_risk in PARSER_UNCERTAINTY_BLOCKING_RISKS:
        flags.add("parser_uncertainty_blocker")
    elif parser_uncertainty_risk in PARSER_UNCERTAINTY_REVIEW_RISKS:
        flags.add("parser_uncertainty_review")

    appendix_values = [
        metadata.get("table_appendix_no"),
        metadata.get("table_appendix_title"),
        metadata.get("appendix_no"),
        metadata.get("form_no"),
    ]
    appendix_text = text[:500]
    if chunk_type in {"appendix", "form"} or any(has_value(value) for value in appendix_values):
        flags.add("form_or_appendix_candidate")
    if chunk_type in {"paragraph", "item", "subitem"} and re.match(
        r"^\s*[\[【<〈]?\s*(?:\ubcc4\ud45c|\ubcc4\uc9c0|\uc11c\uc2dd)\b",
        appendix_text,
    ):
        flags.add("form_or_appendix_candidate")

    supplementary_signal = bool(chunk_get(chunk, "is_supplementary_provision", False)) or chunk_type in {
        "supplementary",
        "supplementary_provision",
    }
    effective_date_signal = has_value(metadata.get("effective_date")) or bool(
        re.search(r"\ubd80\uce59|\uc2dc\ud589\uc77c|\uc801\uc6a9\uc77c", text)
    )
    if (supplementary_signal or effective_date_signal) and not low_risk_supplementary_review_signal(chunk, text):
        flags.add("supplementary_or_effective_date_candidate")

    if chunk_type == "article":
        if not has_value(chunk_get(chunk, "article_no")) or not has_value(chunk_get(chunk, "article_title")):
            flags.add("article_title_or_boundary_gap")

    if "\ufffd" in text or "\x00" in text or likely_mojibake(text):
        flags.add("ocr_or_encoding_noise")
    if document_row and document_row.get("ocr_required") in (True, "true", "True", "1", 1):
        flags.add("ocr_or_encoding_noise")

    if has_independent_footnote_caption_marker(text):
        flags.add("footnote_or_caption_candidate")
    if (
        has_value(metadata.get("footnotes"))
        or has_value(metadata.get("endnotes"))
        or has_value(metadata.get("captions"))
        or has_value(metadata.get("footnote_links"))
        or has_value(metadata.get("footnote_marker_references"))
        or safe_int(metadata.get("footnote_marker_reference_count")) > 0
    ):
        flags.add("footnote_or_caption_candidate")
    source_hwpx_block_types = set(metadata.get("source_hwpx_block_types") or [])
    if source_hwpx_block_types.intersection({"caption", "footnote", "footnotes", "endnote", "endnotes", "image"}):
        flags.add("footnote_or_caption_candidate")
    if has_value(metadata.get("source_caption_count")) and has_independent_footnote_caption_marker(text):
        flags.add("footnote_or_caption_candidate")
    source_hwpx_review_flags = set(metadata.get("source_hwpx_parser_review_flags") or [])
    if source_hwpx_review_flags.intersection(HWPX_FOOTNOTE_CAPTION_FLAGS):
        flags.add("footnote_or_caption_candidate")
    if source_hwpx_review_flags.intersection(HWPX_COMPLEX_STRUCTURE_FLAGS):
        flags.add("hwpx_complex_structure_candidate")
    if any(safe_int(metadata.get(key)) > 0 for key in HWPX_COMPLEX_STRUCTURE_COUNT_KEYS):
        flags.add("hwpx_complex_structure_candidate")

    return sorted(flags)


def low_risk_processor_warning_signal(chunk: dict[str, Any], warnings: Any) -> bool:
    if isinstance(warnings, str):
        warning_values = {warnings.strip()}
    elif isinstance(warnings, (list, tuple, set)):
        warning_values = {str(value).strip() for value in warnings if str(value).strip()}
    else:
        warning_values = set()
    if warning_values != {"orphan_preamble_text"}:
        return False
    chunk_type = normalized_chunk_type(chunk)
    chunk_id = str(chunk.get("chunk_id") or "").lower()
    text = review_body_text(chunk).lower()
    return chunk_type == "paragraph" and ("preamble" in chunk_id or "preamble" in text)


def normalized_table_review_flags(chunk: dict[str, Any]) -> list[str]:
    table_flags = chunk_get(chunk, "table_review_flags", []) or []
    if isinstance(table_flags, str):
        values = [table_flags]
    else:
        values = list(table_flags) if isinstance(table_flags, (list, tuple, set)) else []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def parser_uncertainty_risk_level(metadata: dict[str, Any]) -> str:
    risk = str(metadata.get("parser_uncertainty_risk_level") or "").strip().lower()
    if risk:
        return risk
    report = metadata.get("parser_uncertainty")
    if isinstance(report, dict):
        return str(report.get("risk_level") or "").strip().lower()
    return ""


def parser_uncertainty_flags(metadata: dict[str, Any]) -> list[str]:
    values = metadata.get("parser_uncertainty_flags")
    if values is None and isinstance(metadata.get("parser_uncertainty"), dict):
        values = metadata["parser_uncertainty"].get("flags")
    if isinstance(values, str):
        raw_values = [values]
    else:
        raw_values = list(values) if isinstance(values, (list, tuple, set)) else []
    return sorted({str(value).strip() for value in raw_values if str(value).strip()})


def low_risk_supplementary_review_signal(chunk: dict[str, Any], text: str) -> bool:
    metadata = chunk_meta(chunk)
    overrides = metadata.get("article_effective_overrides") or chunk.get("article_effective_overrides") or []
    if has_value(overrides):
        return False
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return False
    if any(keyword in compact for keyword in SUPPLEMENTARY_TRANSITION_KEYWORDS):
        return False
    if supplementary_heading_only(compact):
        return True
    return bool(metadata.get("supplementary_boilerplate") is True)


def supplementary_heading_only(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return False
    return bool(
        re.fullmatch(
            r"\[?\s*\ubd80\s*\uce59\s*\]?(?:\s*[<(\[]?\s*\d{4}\s*[.\-]\s*\d{1,2}\s*[.\-]\s*\d{1,2}\.?\s*[>)\]]?)?",
            compact,
        )
    )


def likely_mojibake(text: str) -> bool:
    if not text:
        return False
    sample = text[:1200]
    compact = re.sub(r"\s+", "", sample)
    known_fragments = (
        "\ufffd",
        "\u00ec",
        "\u00ed",
        "\u00eb",
        "\u00ea",
        "\u0152",
    )
    known_fragment_hits = sum(compact.count(token) for token in known_fragments)
    cjk_chars = re.findall(r"[\u3400-\u9fff]", compact)
    # Preserve whitespace here. Compacting text turns normal checklist questions
    # such as "적정한가?\n채용공고" into false mojibake signals.
    question_hangul_pairs = re.findall(r"\?[\u3130-\u318f\uac00-\ud7a3]", sample)
    cjk_hangul_pairs = re.findall(
        r"[\u3400-\u9fff][\uac00-\ud7a3]|[\uac00-\ud7a3][\u3400-\u9fff]",
        compact,
    )
    if known_fragment_hits == 0 and len(question_hangul_pairs) < 2:
        return False
    suspicious_score = (
        known_fragment_hits * 4
        + len(question_hangul_pairs) * 3
        + len(cjk_hangul_pairs)
        + min(len(cjk_chars), 20)
    )
    if suspicious_score < 8:
        return False
    return (
        len(question_hangul_pairs) >= 2
        or len(cjk_hangul_pairs) >= 2
        or percent(suspicious_score, max(len(compact), 1)) >= 1.0
    )


REVIEW_FLAG_TIERS: dict[str, str] = {
    "ocr_or_encoding_noise": "blocking_review",
    "parser_uncertainty_blocker": "blocking_review",
    "table_extraction_failed_candidate": "blocking_review",
    "table_review_required": "blocking_review",
    "article_title_or_boundary_gap": "blocking_review",
    "processor_warning_candidate": "blocking_review",
    "parser_uncertainty_review": "domain_attention",
    "form_or_appendix_candidate": "domain_attention",
    "supplementary_or_effective_date_candidate": "domain_attention",
    "table_context_candidate": "domain_attention",
    "footnote_or_caption_candidate": "domain_attention",
    "hwpx_complex_structure_candidate": "domain_attention",
    "hwp_binary_table_geometry_candidate": "domain_attention",
    "table_false_positive_candidate": "informational",
}
REVIEW_PRIORITY_ORDER = {
    "blocking_review": 0,
    "domain_attention": 1,
    "stable_false_positive": 2,
    "informational": 3,
}
NON_ACTIONABLE_SOLE_REVIEW_FLAGS = {
    "form_or_appendix_candidate",
}
REVIEW_SEVERITY_RULES: tuple[tuple[str, int, str, str, str], ...] = (
    (
        "ocr_or_encoding_noise",
        0,
        "ocr_or_encoding_blocker",
        "Fix OCR/encoding before approval or indexing.",
        "The text may be unreadable or corrupted.",
    ),
    (
        "table_extraction_failed_candidate",
        1,
        "table_extraction_blocker",
        "Repair or manually confirm table structure before citation-grade use.",
        "A probable table extraction failure was detected.",
    ),
    (
        "table_review_required",
        2,
        "table_structure_review",
        "Verify table headers, rows, merged cells, and units.",
        "Table-like content needs structural review.",
    ),
    (
        "article_title_or_boundary_gap",
        3,
        "article_boundary_review",
        "Verify article number, title, and boundary.",
        "Article metadata is incomplete or ambiguous.",
    ),
    (
        "processor_warning_candidate",
        4,
        "processor_warning_review",
        "Inspect parser warning and compare with source text.",
        "The processor emitted a warning for this chunk.",
    ),
    (
        "parser_uncertainty_blocker",
        5,
        "parser_uncertainty_blocker",
        "Resolve parser uncertainty or manually compare with the source before approval or indexing.",
        "The parser emitted high or critical uncertainty for this chunk.",
    ),
    (
        "footnote_or_caption_candidate",
        6,
        "footnote_caption_review",
        "Confirm note/caption ownership and whether it must be attached to a table or figure.",
        "Footnote, endnote, image, or caption signals were detected.",
    ),
    (
        "hwpx_complex_structure_candidate",
        7,
        "hwpx_complex_structure_review",
        "Compare nested tables, merged cells, table-contained notes, and images with the source HWPX.",
        "Complex HWPX structure signals were preserved by the parser.",
    ),
    (
        "hwp_binary_table_geometry_candidate",
        7,
        "hwp_binary_geometry_review",
        "Confirm HWP table/form geometry against the source because native table geometry was not extracted.",
        "HWP binary parsing preserved extraction-mode evidence but not native table geometry.",
    ),
    (
        "parser_uncertainty_review",
        7,
        "parser_uncertainty_review",
        "Spot-check parser uncertainty flags before broad approval.",
        "The parser emitted medium uncertainty for this chunk.",
    ),
    (
        "form_or_appendix_candidate",
        8,
        "appendix_form_review",
        "Confirm appendix/form title, reference article, and field layout.",
        "Appendix or form content usually requires domain review.",
    ),
    (
        "supplementary_or_effective_date_candidate",
        9,
        "supplementary_effective_date_review",
        "Confirm supplementary provision, enforcement date, and application date facts.",
        "Effective-date language can affect answer validity.",
    ),
    (
        "table_context_candidate",
        10,
        "table_context_spot_check",
        "Spot-check whether table context was preserved.",
        "Table context was detected without a harder blocker.",
    ),
    (
        "table_false_positive_candidate",
        80,
        "stable_table_false_positive",
        "Batch-verify or defer if already known stable.",
        "The row looks like a known table false-positive pattern.",
    ),
)


def review_severity_for_flags(flags: list[str], tier: str) -> dict[str, Any]:
    if tier == "no_signal":
        return {
            "review_severity_rank": 99,
            "review_category": "no_signal",
            "review_focus": "No current review signal.",
            "review_step": "No immediate action.",
        }
    if tier == "stable_false_positive":
        return {
            "review_severity_rank": 80,
            "review_category": "stable_table_false_positive",
            "review_focus": "Known stable false-positive pattern.",
            "review_step": "Batch-verify or defer unless policy changes.",
        }
    for flag, rank, category, step, focus in REVIEW_SEVERITY_RULES:
        if flag in flags:
            return {
                "review_severity_rank": rank,
                "review_category": category,
                "review_focus": focus,
                "review_step": step,
            }
    if tier == "domain_attention":
        return {
            "review_severity_rank": 70,
            "review_category": "domain_attention",
            "review_focus": "Domain-sensitive regulation content.",
            "review_step": "Spot-check before broad reuse.",
        }
    return {
        "review_severity_rank": 90,
        "review_category": "informational",
        "review_focus": "Non-blocking signal retained for analysis.",
        "review_step": "Retain for trend review.",
    }


def review_priority_tier(chunk: dict[str, Any], flags: list[str]) -> str:
    if not flags:
        return "no_signal"
    actionable_flags = actionable_review_flags(flags)
    if not actionable_flags:
        return "informational"
    metadata = chunk_meta(chunk)
    if "table_false_positive_candidate" in actionable_flags and metadata.get("table_false_positive_stability") == "stable":
        hard_blocking = {
            "ocr_or_encoding_noise",
            "parser_uncertainty_blocker",
            "table_extraction_failed_candidate",
            "table_review_required",
            "article_title_or_boundary_gap",
            "processor_warning_candidate",
        }
        if not any(flag in hard_blocking for flag in actionable_flags):
            return "stable_false_positive"
    blocking_flags = [flag for flag in actionable_flags if REVIEW_FLAG_TIERS.get(flag) == "blocking_review"]
    if blocking_flags:
        return "blocking_review"
    if any(REVIEW_FLAG_TIERS.get(flag) == "domain_attention" for flag in actionable_flags):
        return "domain_attention"
    return "informational"


def actionable_review_flags(flags: list[str]) -> list[str]:
    if not flags:
        return []
    flag_set = set(flags)
    if flag_set and flag_set.issubset(NON_ACTIONABLE_SOLE_REVIEW_FLAGS):
        return []
    return flags


def review_priority_counts(chunks: list[dict[str, Any]], document_row: dict[str, Any] | None = None) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for chunk in chunks:
        counts[review_priority_tier(chunk, chunk_review_flags(chunk, document_row))] += 1
    for tier in ("no_signal", "domain_attention", "blocking_review", "stable_false_positive", "informational"):
        counts.setdefault(tier, 0)
    return dict(counts)


def document_inventory_from_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for chunk in chunks:
        metadata = chunk_meta(chunk)
        inventory = metadata.get("document_inventory") or metadata.get("hwp_inventory")
        if isinstance(inventory, dict):
            return inventory
    return None


def _inventory_pipeline_counts(inventory: dict[str, Any], chunk_count: int) -> dict[str, int]:
    hierarchy = inventory.get("hierarchy") if isinstance(inventory.get("hierarchy"), dict) else {}
    attachments = inventory.get("attachments") if isinstance(inventory.get("attachments"), dict) else {}
    tables = inventory.get("tables") if isinstance(inventory.get("tables"), dict) else {}
    supplements = inventory.get("supplements") if isinstance(inventory.get("supplements"), dict) else {}
    paragraph_or_item = (
        safe_int(hierarchy.get("paragraphs"))
        + safe_int(hierarchy.get("numbered_items"))
        + safe_int(hierarchy.get("hangul_items"))
        + safe_int(hierarchy.get("parenthesized_items"))
    )
    attachment_total = safe_int(attachments.get("total"))
    if attachment_total <= 0:
        attachment_total = (
            safe_int(attachments.get("annexes"))
            + safe_int(attachments.get("forms"))
            + safe_int(attachments.get("sheets"))
        )
    footnote_caption_count = safe_int(inventory.get("footnotes")) + safe_int(inventory.get("endnotes"))
    if str(inventory.get("source") or "").lower() != "hwp":
        footnote_caption_count += safe_int(inventory.get("captions"))
    return {
        "chunk_count": chunk_count,
        "article_count_distinct_article_no": safe_int(hierarchy.get("articles")),
        "paragraph_or_item_chunk_count": paragraph_or_item,
        "paragraph_marker_count_circled": safe_int(hierarchy.get("paragraphs")),
        "numbered_item_count": safe_int(hierarchy.get("numbered_items")),
        "hangul_item_count": safe_int(hierarchy.get("hangul_items")),
        "parenthesized_item_count": safe_int(hierarchy.get("parenthesized_items")),
        "appendix_or_form_candidate_count": attachment_total,
        "annex_candidate_count": safe_int(attachments.get("annexes")),
        "form_candidate_count": safe_int(attachments.get("forms")),
        "sheet_candidate_count": safe_int(attachments.get("sheets")),
        "table_like_chunk_count": safe_int(tables.get("total")),
        "nested_table_candidate_count": safe_int(tables.get("nested")),
        "supplementary_or_effective_date_candidate_count": safe_int(supplements.get("blocks")),
        "supplementary_block_count": safe_int(supplements.get("blocks")),
        "supplementary_blocks_with_effective_date_count": safe_int(supplements.get("blocks_with_effective_date")),
        "explicit_effective_article_count": safe_int(supplements.get("explicit_effective_articles")),
        "direct_effective_clause_count": safe_int(supplements.get("direct_effective_clauses")),
        "application_clause_count": safe_int(supplements.get("application_clauses")),
        "attachment_caption_count": safe_int(inventory.get("attachment_caption_count")),
        "note_line_count": safe_int(inventory.get("note_line_count")),
        "footnote_or_caption_candidate_count": footnote_caption_count,
    }


def kordoc_promoted_table_unit_count(chunks: list[dict[str, Any]]) -> int:
    table_units: set[str] = set()
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk_meta(chunk)
        if not bool(metadata.get("kordoc_table_promoted")) and str(metadata.get("table_source") or "").lower() != "kordoc":
            continue
        match = metadata.get("kordoc_table_match") if isinstance(metadata.get("kordoc_table_match"), dict) else {}
        unit_id = (
            match.get("table_id")
            or match.get("table_index")
            or metadata.get("table_id")
            or chunk.get("chunk_id")
            or f"kordoc-table-{index}"
        )
        table_units.add(str(unit_id))
    return len(table_units)


def table_citation_ready_chunk_count(chunks: list[dict[str, Any]]) -> int:
    count = 0
    for chunk in chunks:
        metadata = chunk_meta(chunk)
        if not bool(chunk_get(chunk, "table_like", False)):
            continue
        if bool(metadata.get("kordoc_table_unmatched_source")) and not chunk_get(chunk, "source_page_start", None):
            continue
        if not chunk_get(chunk, "table_cell_rows", []):
            continue
        if (
            chunk_get(chunk, "table_citation_label", "")
            or chunk_get(chunk, "table_appendix_no", "")
            or "별표" in str(chunk_get(chunk, "hierarchy_path", ""))
            or "별지" in str(chunk_get(chunk, "hierarchy_path", ""))
        ):
            count += 1
    return count


def page_less_kordoc_only_table_count(chunks: list[dict[str, Any]]) -> int:
    count = 0
    for chunk in chunks:
        metadata = chunk_meta(chunk)
        if not bool(chunk_get(chunk, "table_like", False)):
            continue
        if not bool(metadata.get("kordoc_table_unmatched_source")):
            continue
        if chunk_get(chunk, "source_page_start", None):
            continue
        count += 1
    return count


def table_like_without_cell_rows_count(chunks: list[dict[str, Any]]) -> int:
    return sum(
        1
        for chunk in chunks
        if bool(chunk_get(chunk, "table_like", False)) and not chunk_get(chunk, "table_cell_rows", [])
    )


def _inventory_source(inventory: dict[str, Any] | None) -> str:
    if not inventory:
        return ""
    return str(inventory.get("source") or "").lower()


def summarize_pipeline_counts(chunks: list[dict[str, Any]], document_row: dict[str, Any] | None = None) -> dict[str, Any]:
    chunk_types = Counter(normalized_chunk_type(chunk) for chunk in chunks)
    article_numbers = {
        str(chunk_get(chunk, "article_no"))
        for chunk in chunks
        if has_value(chunk_get(chunk, "article_no"))
    }
    inventory = document_inventory_from_chunks(chunks)
    paragraph_items = paragraph_item_chunk_count(chunks)
    paragraph_chunk_units = paragraph_items
    structural_article_body_items = article_structural_child_paragraph_item_count(chunks)
    traceable_article_body_items = article_traceable_paragraph_item_unit_count(chunks)
    visible_article_body_items = 0
    paragraph_item_count_source = "chunk_types"
    if inventory is None and structural_article_body_items:
        paragraph_items = structural_article_body_items
        paragraph_item_count_source = "structural_child_metadata"
    elif inventory is None and normalized_extension(document_row or {}) == ".pdf":
        visible_article_body_items = sum(visible_article_body_paragraph_item_count(chunk) for chunk in chunks)
        if visible_article_body_items:
            paragraph_items = max(paragraph_items, visible_article_body_items)
            paragraph_item_count_source = "visible_article_body_deduped"
    if inventory:
        paragraph_item_count_source = "document_inventory"
    flags_by_chunk = [chunk_review_flags(chunk, document_row) for chunk in chunks]
    review_required = sum(1 for flags in flags_by_chunk if actionable_review_flags(flags))
    table_like = sum(1 for chunk in chunks if bool(chunk_get(chunk, "table_like", False)))
    table_review_required = sum(1 for chunk in chunks if bool(chunk_get(chunk, "table_review_required", False)))
    appendix_or_form = sum(1 for flags in flags_by_chunk if "form_or_appendix_candidate" in flags)
    appendix_or_form_logical_units = _appendix_form_logical_unit_count(chunks, flags_by_chunk, document_row)
    appendix_or_form_count_source = "chunk_flags"
    if inventory:
        appendix_or_form_count_source = "document_inventory"
    elif appendix_or_form_logical_units:
        appendix_or_form_count_source = "appendix_form_logical_units"
    supplementary = sum(1 for flags in flags_by_chunk if "supplementary_or_effective_date_candidate" in flags)
    supplementary_logical_units = _supplementary_logical_unit_count(chunks, flags_by_chunk, document_row)
    footnote_caption = sum(1 for flags in flags_by_chunk if "footnote_or_caption_candidate" in flags)
    footnote_link_units = footnote_link_logical_unit_count(chunks)
    footnote_marker_units = footnote_marker_reference_unit_count(chunks)
    footnote_caption_count_source = "chunk_flags"
    footnote_caption_count = footnote_caption
    if footnote_link_units > footnote_caption_count:
        footnote_caption_count_source = "footnote_links"
        footnote_caption_count = footnote_link_units
    if footnote_marker_units > footnote_caption_count:
        footnote_caption_count_source = "footnote_marker_references"
        footnote_caption_count = footnote_marker_units
    nested_table_groups = {
        review_group_key(document_row or {}, chunk, chunk_meta(chunk), flags)
        for chunk, flags in zip(chunks, flags_by_chunk, strict=False)
        if "hwpx_complex_structure_candidate" in flags
        and (
            "nested_table" in set(chunk_meta(chunk).get("source_hwpx_parser_review_flags") or [])
            or safe_int(chunk_meta(chunk).get("source_hwpx_nested_table_count")) > 0
            or bool(chunk_meta(chunk).get("source_hwpx_nested_table_text_snippets"))
        )
    }
    priority_counts = review_priority_counts(chunks, document_row)
    inventory_counts: dict[str, int] = {}
    if inventory:
        inventory_counts = _inventory_pipeline_counts(inventory, len(chunks))
    paragraph_item_inventory_candidate_count = inventory_counts.get("paragraph_or_item_chunk_count", 0)
    paragraph_item_traceable_unit_count = traceable_article_body_items or structural_article_body_items or paragraph_chunk_units
    kordoc_table_units = kordoc_promoted_table_unit_count(chunks)
    inventory_table_like = inventory_counts.get("table_like_chunk_count")
    table_like_count_source = "chunk_flags"
    table_like_count = table_like
    if inventory_table_like is not None:
        table_like_count_source = "document_inventory"
        table_like_count = inventory_table_like
    if _inventory_source(inventory) == "hwp" and kordoc_table_units > 0:
        table_like_count_source = "kordoc_promoted_hwp"
        table_like_count = kordoc_table_units
    table_citation_ready_count = table_citation_ready_chunk_count(chunks)
    table_goldset_preserved_count = table_citation_ready_count
    table_goldset_count_source = "citation_ready"
    page_less_kordoc_only_count = page_less_kordoc_only_table_count(chunks)
    table_without_cell_rows = table_like_without_cell_rows_count(chunks)
    supplementary_count_source = "chunk_flags"
    supplementary_count = supplementary
    if supplementary_logical_units:
        supplementary_count_source = "supplementary_logical_units"
        supplementary_count = supplementary_logical_units
    if "supplementary_or_effective_date_candidate_count" in inventory_counts:
        supplementary_count_source = "document_inventory"
        supplementary_count = inventory_counts["supplementary_or_effective_date_candidate_count"]
    nested_table_count_source = "hwpx_complex_flags"
    if "nested_table_candidate_count" in inventory_counts:
        nested_inventory_source = _inventory_source(inventory) or "document"
        nested_table_count_source = f"{nested_inventory_source}_inventory"
    return {
        "chunk_count": len(chunks),
        "chunk_types": dict(chunk_types),
        "article_count_distinct_article_no": inventory_counts.get(
            "article_count_distinct_article_no", len(article_numbers)
        ),
        "paragraph_or_item_chunk_count": inventory_counts.get("paragraph_or_item_chunk_count", paragraph_items),
        "paragraph_item_count_source": paragraph_item_count_source,
        "structural_article_body_paragraph_item_count": structural_article_body_items,
        "paragraph_item_traceable_unit_count": paragraph_item_traceable_unit_count,
        "paragraph_item_inventory_candidate_count": paragraph_item_inventory_candidate_count,
        "paragraph_item_visible_marker_candidate_count": visible_article_body_items,
        "visible_article_body_paragraph_item_count": visible_article_body_items,
        "paragraph_marker_count_circled": inventory_counts.get("paragraph_marker_count_circled", 0),
        "numbered_item_count": inventory_counts.get("numbered_item_count", 0),
        "hangul_item_count": inventory_counts.get("hangul_item_count", 0),
        "parenthesized_item_count": inventory_counts.get("parenthesized_item_count", 0),
        "appendix_or_form_candidate_count": inventory_counts.get(
            "appendix_or_form_candidate_count",
            appendix_or_form_logical_units or appendix_or_form,
        ),
        "appendix_or_form_count_source": appendix_or_form_count_source,
        "appendix_or_form_candidate_chunk_count": appendix_or_form,
        "appendix_or_form_logical_unit_count": appendix_or_form_logical_units,
        "annex_candidate_count": inventory_counts.get("annex_candidate_count", 0),
        "form_candidate_count": inventory_counts.get("form_candidate_count", 0),
        "sheet_candidate_count": inventory_counts.get("sheet_candidate_count", 0),
        "supplementary_or_effective_date_candidate_count": supplementary_count,
        "supplementary_count_source": supplementary_count_source,
        "supplementary_or_effective_date_candidate_chunk_count": supplementary,
        "supplementary_logical_unit_count": supplementary_logical_units,
        "supplementary_block_count": inventory_counts.get("supplementary_block_count", 0),
        "supplementary_blocks_with_effective_date_count": inventory_counts.get(
            "supplementary_blocks_with_effective_date_count", 0
        ),
        "explicit_effective_article_count": inventory_counts.get("explicit_effective_article_count", 0),
        "direct_effective_clause_count": inventory_counts.get("direct_effective_clause_count", 0),
        "application_clause_count": inventory_counts.get("application_clause_count", 0),
        "table_like_chunk_count": table_like_count,
        "table_like_count_source": table_like_count_source,
        "table_goldset_preserved_count": table_goldset_preserved_count,
        "table_goldset_count_source": table_goldset_count_source,
        "table_citation_ready_chunk_count": table_citation_ready_count,
        "kordoc_promoted_table_unit_count": kordoc_table_units,
        "hwp_inventory_table_like_chunk_count": (
            inventory_table_like if _inventory_source(inventory) == "hwp" else 0
        ),
        "page_less_kordoc_only_table_count": page_less_kordoc_only_count,
        "table_like_without_cell_rows_count": table_without_cell_rows,
        "table_review_required_chunk_count": table_review_required,
        "footnote_or_caption_candidate_count": inventory_counts.get(
            "footnote_or_caption_candidate_count", footnote_caption_count
        ),
        "footnote_or_caption_count_source": (
            "document_inventory"
            if "footnote_or_caption_candidate_count" in inventory_counts
            else footnote_caption_count_source
        ),
        "footnote_link_logical_unit_count": footnote_link_units,
        "footnote_marker_reference_unit_count": footnote_marker_units,
        "attachment_caption_count": inventory_counts.get("attachment_caption_count", 0),
        "note_line_count": inventory_counts.get("note_line_count", 0),
        "nested_table_candidate_count": inventory_counts.get("nested_table_candidate_count", len(nested_table_groups)),
        "nested_table_count_source": nested_table_count_source,
        "review_required_candidate_count": review_required,
        "review_required_candidate_rate": percent(review_required, len(chunks)),
        "priority_counts": priority_counts,
        "blocking_review_candidate_count": priority_counts["blocking_review"],
        "blocking_review_candidate_rate": percent(priority_counts["blocking_review"], len(chunks)),
        "domain_attention_candidate_count": priority_counts["domain_attention"],
        "domain_attention_candidate_rate": percent(priority_counts["domain_attention"], len(chunks)),
    }


def _appendix_form_logical_unit_count(
    chunks: list[dict[str, Any]],
    flags_by_chunk: list[set[str]],
    document_row: dict[str, Any] | None = None,
) -> int:
    logical_ids: set[str] = set()
    extension = normalized_extension(document_row or {})
    last_hwpx_attachment_key = ""
    for index, (chunk, flags) in enumerate(zip(chunks, flags_by_chunk, strict=False), start=1):
        chunk_type = normalized_chunk_type(chunk)
        if extension == ".hwpx" and chunk_type == "table":
            continue
        if chunk_type not in {"appendix", "form"} and "form_or_appendix_candidate" not in flags:
            continue
        metadata = chunk_meta(chunk)
        logical_id = ""
        if extension == ".hwpx":
            attachment_key = normalized_hwpx_attachment_key(chunk)
            if attachment_key:
                logical_id = f"hwpx-attachment:{attachment_key}"
                last_hwpx_attachment_key = logical_id
            elif last_hwpx_attachment_key:
                logical_id = last_hwpx_attachment_key
        if not logical_id:
            logical_id = str(metadata.get("entity_id") or "").strip()
        if not logical_id:
            source_node_ids = chunk.get("source_node_ids")
            if isinstance(source_node_ids, list) and source_node_ids:
                logical_id = str(source_node_ids[0] or "").strip()
        if not logical_id:
            logical_id = str(chunk.get("chunk_id") or f"appendix-form-{index}").strip()
        if logical_id:
            logical_ids.add(logical_id)
    return len(logical_ids)


def _supplementary_logical_unit_count(
    chunks: list[dict[str, Any]],
    flags_by_chunk: list[set[str]],
    document_row: dict[str, Any] | None = None,
) -> int:
    candidate_count = 0
    supplementary_units = 0
    for chunk, flags in zip(chunks, flags_by_chunk, strict=False):
        if "supplementary_or_effective_date_candidate" not in flags:
            continue
        candidate_count += 1
        if normalized_chunk_type(chunk) in {"supplementary", "supplementary_provision"}:
            supplementary_units += 1
    if supplementary_units <= 0:
        return 0
    extension = normalized_extension(document_row or {})
    if extension == ".hwpx":
        return supplementary_units
    if extension == ".pdf" and supplementary_units >= 10 and candidate_count >= supplementary_units * 2:
        return supplementary_units
    return 0


def make_metric_bucket() -> dict[str, Any]:
    return {
        "document_count": 0,
        "chunk_count": 0,
        "review_required_candidate_count": 0,
        "review_required_candidate_rate": 0.0,
        "flag_counts": Counter(),
    }


def finalize_metric_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    result = dict(bucket)
    result["review_required_candidate_rate"] = percent(
        int(result["review_required_candidate_count"]),
        int(result["chunk_count"]),
    )
    result["flag_counts"] = dict(result["flag_counts"])
    return result


def finalize_priority_counts(counts: Counter[str], total: int) -> dict[str, dict[str, int | float]]:
    tiers = ("no_signal", "domain_attention", "blocking_review", "stable_false_positive", "informational")
    return {
        tier: {
            "count": int(counts.get(tier, 0)),
            "rate": percent(int(counts.get(tier, 0)), total),
        }
        for tier in tiers
    }


def _metadata_list_key(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, (str, int, float, bool)):
        items = [values]
    else:
        try:
            items = list(values)
        except TypeError:
            items = [values]
    normalized = sorted({str(value).strip() for value in items if str(value).strip()})
    return ",".join(normalized)


def _metadata_numeric_anchor(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, (str, int, float, bool)):
        items = [values]
    else:
        try:
            items = list(values)
        except TypeError:
            items = [values]
    numeric_values: list[int] = []
    text_values: list[str] = []
    for value in items:
        text = str(value).strip()
        if not text:
            continue
        try:
            numeric_values.append(int(text))
        except ValueError:
            text_values.append(text)
    if numeric_values:
        return str(max(numeric_values))
    return sorted(text_values)[-1] if text_values else ""


def review_group_key(
    row: dict[str, Any],
    chunk: dict[str, Any],
    metadata: dict[str, Any],
    flags: list[str] | None = None,
) -> str:
    document_id = str(row.get("document_id") or "")
    hwpx_xml_blocks = _metadata_list_key(metadata.get("source_hwpx_xml_block_indices"))
    nested_table_snippets = _metadata_list_key(metadata.get("source_hwpx_nested_table_text_snippets"))
    if nested_table_snippets:
        digest = hashlib.sha256(nested_table_snippets.encode("utf-8")).hexdigest()[:16]
        xml_anchor = _metadata_numeric_anchor(metadata.get("source_hwpx_xml_block_indices"))
        if xml_anchor:
            return f"doc:{document_id}|hwpx_nested:{digest}|xml:{xml_anchor}"
        return f"doc:{document_id}|hwpx_nested:{digest}"
    if hwpx_xml_blocks:
        return f"doc:{document_id}|hwpx_xml:{hwpx_xml_blocks}"
    revision_group = revision_structural_group_key(row, chunk, metadata)
    if revision_group:
        return revision_group
    supplementary_group = repeated_supplementary_group_key(row, chunk, metadata, flags or [])
    if supplementary_group:
        return supplementary_group
    note_group = repeated_note_group_key(row, chunk, metadata, flags or [])
    if note_group:
        return note_group
    hwp_geometry_group = hwp_binary_geometry_group_key(row, chunk, metadata, flags or [])
    if hwp_geometry_group:
        return hwp_geometry_group
    chunk_id = str(chunk.get("chunk_id") or "")
    return f"doc:{document_id}|chunk:{chunk_id}"


def revision_structural_group_key(row: dict[str, Any], chunk: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    source_record_id = str(row.get("source_record_id") or "").strip()
    if not source_record_id:
        return None
    chunk_type = normalized_chunk_type(chunk)
    if chunk_type not in {"appendix", "form", "table"}:
        return None
    table_flags = normalized_table_review_flags(chunk)
    if not (chunk_get(chunk, "table_review_required", False) or table_flags):
        return None
    unit_label = revision_unit_label(chunk, metadata)
    if not unit_label:
        return None
    if not revision_unit_label_groupable(unit_label):
        return None
    regulation_no = str(chunk_get(chunk, "regulation_no", "") or metadata.get("regulation_no") or "").strip()
    part = revision_chunk_part(str(chunk.get("chunk_id") or ""))
    flag_key = ",".join(table_flags)
    return (
        f"source_record:{source_record_id}|regulation:{regulation_no}|type:{chunk_type}|"
        f"unit:{unit_label}|part:{part}|table_flags:{flag_key}"
    )


def hwp_binary_geometry_group_key(
    row: dict[str, Any],
    chunk: dict[str, Any],
    metadata: dict[str, Any],
    flags: list[str],
) -> str | None:
    if "hwp_binary_table_geometry_candidate" not in flags:
        return None
    if "table_extraction_failed_candidate" in flags or "table_review_required" in flags:
        return None
    source_record_id = str(row.get("source_record_id") or "").strip()
    if not source_record_id:
        return None
    unit_label = revision_unit_label(chunk, metadata)
    anchor = ""
    if unit_label and revision_unit_label_groupable(unit_label):
        anchor = f"unit:{unit_label}"
    else:
        article_no = str(chunk_get(chunk, "article_no", "") or metadata.get("article_no") or "").strip()
        page_start = chunk_get(chunk, "source_page_start", None) or metadata.get("source_page_start")
        if article_no:
            anchor = f"article:{article_no}"
        elif page_start not in (None, ""):
            anchor = f"page:{page_start}"
    if not anchor:
        return None
    regulation_no = str(chunk_get(chunk, "regulation_no", "") or metadata.get("regulation_no") or "").strip()
    chunk_type = normalized_chunk_type(chunk)
    extraction_modes = _metadata_list_key(metadata.get("source_hwp_extraction_modes"))
    section_anchor = _metadata_numeric_anchor(metadata.get("source_hwp_section_indices"))
    return (
        f"source_record:{source_record_id}|regulation:{regulation_no}|hwp_geometry:{chunk_type}|"
        f"{anchor}|modes:{extraction_modes}|section:{section_anchor}"
    )


def revision_unit_label(chunk: dict[str, Any], metadata: dict[str, Any]) -> str:
    candidates: list[Any] = [
        metadata.get("table_appendix_no"),
        metadata.get("appendix_no"),
        metadata.get("form_no"),
    ]
    candidates.extend(metadata.get("appendix_refs") or [])
    candidates.extend(metadata.get("form_refs") or [])
    chunk_id_label = revision_unit_label_from_chunk_id(str(chunk.get("chunk_id") or ""))
    if chunk_id_label:
        candidates.append(chunk_id_label)
    normalized_candidates: list[str] = []
    for candidate in candidates:
        value = normalize_revision_unit_label(str(candidate or "").strip())
        if value:
            normalized_candidates.append(value)
    if not normalized_candidates:
        return ""
    return max(normalized_candidates, key=revision_unit_label_specificity)


def revision_unit_label_from_chunk_id(chunk_id: str) -> str:
    match = re.search(r"_(appendix|form|table)_(.+?)_\d{4}_p\d+_\d{3}$", chunk_id)
    if match:
        return normalize_revision_unit_label(match.group(2))
    match = re.search(r"_(appendix|form|table)_([^_]+)_", chunk_id)
    if match:
        return normalize_revision_unit_label(match.group(2))
    return ""


def normalize_revision_unit_label(value: str) -> str:
    value = re.sub(r"\s+", "", value or "")
    value = re.sub(r"(?<=\d)_(?=\d)", "-", value)
    return value.strip("_")


def revision_unit_label_specificity(value: str) -> tuple[int, int, str]:
    generic_labels = {"별지", "별표", "서식", "appendix", "form", "table"}
    normalized = value.lower()
    has_digit = bool(re.search(r"\d", value))
    is_generic = normalized in generic_labels or (not has_digit and len(value) <= 4)
    return (0 if is_generic else 1, len(value), value)


def revision_unit_label_groupable(value: str) -> bool:
    return bool(re.search(r"\d", value or ""))


def repeated_note_group_key(
    row: dict[str, Any],
    chunk: dict[str, Any],
    metadata: dict[str, Any],
    flags: list[str],
) -> str | None:
    if "footnote_or_caption_candidate" not in flags:
        return None
    source_record_id = str(row.get("source_record_id") or "").strip()
    if not source_record_id:
        return None
    chunk_type = normalized_chunk_type(chunk)
    if chunk_type == "article":
        anchor = str(chunk_get(chunk, "article_no", "") or "").strip()
    elif chunk_type in {"supplementary", "supplementary_provision"}:
        anchor = "supplementary"
    else:
        anchor = revision_unit_label(chunk, metadata)
    if not anchor:
        return None
    signature_text = review_group_signature_text(chunk)
    if len(signature_text) < 20:
        return None
    digest = hashlib.sha256(signature_text.encode("utf-8")).hexdigest()[:16]
    regulation_no = str(chunk_get(chunk, "regulation_no", "") or metadata.get("regulation_no") or "").strip()
    return (
        f"source_record:{source_record_id}|regulation:{regulation_no}|note:{chunk_type}|"
        f"anchor:{anchor}|text:{digest}"
    )


def repeated_supplementary_group_key(
    row: dict[str, Any],
    chunk: dict[str, Any],
    metadata: dict[str, Any],
    flags: list[str],
) -> str | None:
    if "supplementary_or_effective_date_candidate" not in flags:
        return None
    source_record_id = str(row.get("source_record_id") or "").strip()
    if not source_record_id:
        return None
    chunk_type = normalized_chunk_type(chunk)
    anchor = supplementary_group_anchor(chunk, chunk_type)
    signature_text = review_group_full_signature_text(chunk)
    if len(signature_text) < 20:
        return None
    digest = hashlib.sha256(signature_text.encode("utf-8")).hexdigest()[:16]
    regulation_no = str(chunk_get(chunk, "regulation_no", "") or metadata.get("regulation_no") or "").strip()
    flag_signature = review_group_list_signature(flags)
    temporal_signature = supplementary_temporal_signature(chunk, metadata)
    reference_signature = supplementary_reference_signature(chunk, metadata)
    apba_id = str(row.get("apba_id") or "").strip()
    profile_id = str(row.get("profile_id") or "").strip()
    return (
        f"apba:{apba_id}|profile:{profile_id}|source_record:{source_record_id}|regulation:{regulation_no}|"
        f"supplementary:{chunk_type}|anchor:{anchor}|flags:{flag_signature}|text:{digest}|"
        f"temporal:{temporal_signature}|refs:{reference_signature}"
    )


def supplementary_group_anchor(chunk: dict[str, Any], chunk_type: str) -> str:
    article_no = str(chunk_get(chunk, "article_no", "") or "").strip()
    if article_no:
        return article_no
    paragraph_no = str(chunk_get(chunk, "paragraph_no", "") or "").strip()
    if paragraph_no:
        return paragraph_no
    return chunk_type or "supplementary"


def supplementary_temporal_signature(chunk: dict[str, Any], metadata: dict[str, Any]) -> str:
    fields = [
        "supplementary_label",
        "supplementary_identifier_date",
        "effective_date",
        "valid_from",
        "valid_to",
        "revision_date",
        "is_supplementary_provision",
    ]
    return "|".join(f"{field}={review_group_value_signature(chunk_get(chunk, field, metadata.get(field)))}" for field in fields)


def supplementary_reference_signature(chunk: dict[str, Any], metadata: dict[str, Any]) -> str:
    fields = ["article_refs", "appendix_refs", "form_refs"]
    return "|".join(f"{field}={review_group_list_signature(chunk_get(chunk, field, metadata.get(field)))}" for field in fields)


def review_group_list_signature(values: Any) -> str:
    if not has_value(values):
        return ""
    if isinstance(values, (str, int, float, bool)):
        raw_items = [values]
    else:
        try:
            raw_items = list(values)
        except TypeError:
            raw_items = [values]
    normalized = [review_group_value_signature(value) for value in raw_items]
    return ",".join(sorted(value for value in normalized if value))


def review_group_value_signature(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            return re.sub(r"\s+", " ", str(value)).strip()
    return re.sub(r"\s+", " ", str(value)).strip()


def review_group_signature_text(chunk: dict[str, Any]) -> str:
    text = review_body_text(chunk)
    text = re.sub(r"^\[위치\].*?\[본문\]\s*", "", text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", text).strip()[:1200]


def review_group_full_signature_text(chunk: dict[str, Any]) -> str:
    text = str(chunk.get("text") or chunk.get("normalized_text") or "").strip() or review_body_text(chunk)
    return re.sub(r"\s+", " ", text).strip()[:1600]


def revision_chunk_part(chunk_id: str) -> str:
    match = re.search(r"_(\d{3})$", chunk_id)
    return match.group(1) if match else "001"


def annotate_review_groups(rows: list[dict[str, Any]]) -> dict[str, Any]:
    group_counts = Counter(str(row.get("review_group_key") or "") for row in rows)
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("review_group_key") or "")
        row["review_group_duplicate_count"] = int(group_counts.get(key, 0))
        row["review_group_primary"] = key not in seen
        seen.add(key)
    duplicate_rows = sum(count - 1 for count in group_counts.values() if count > 1)
    return {
        "review_queue_row_count": len(rows),
        "review_group_count": len(group_counts),
        "duplicate_review_queue_row_count": int(duplicate_rows),
        "grouped_review_workload_rate": percent(len(group_counts), len(rows)),
    }


def review_flag_sample(
    workspace: Path,
    row: dict[str, Any],
    chunk: dict[str, Any],
    flags: list[str],
    chunk_path: Path | None,
) -> dict[str, Any]:
    text = str(chunk.get("text") or chunk.get("normalized_text") or "")
    return {
        "document_id": row.get("document_id"),
        "filename": row.get("filename"),
        "extension": normalized_extension(row),
        "institution_name": row.get("institution_name"),
        "apba_id": row.get("apba_id"),
        "profile_id": row.get("profile_id"),
        "source_record_id": row.get("source_record_id"),
        "source_file_id": row.get("source_file_id"),
        "chunk_id": chunk.get("chunk_id"),
        "chunk_type": normalized_chunk_type(chunk),
        "page_start": chunk_get(chunk, "source_page_start"),
        "page_end": chunk_get(chunk, "source_page_end"),
        "priority_tier": review_priority_tier(chunk, flags),
        "flags": flags,
        "chunk_artifact": report_path(workspace, chunk_path),
        "snippet": snippet(text, 220),
    }


def review_queue_row(
    workspace: Path,
    row: dict[str, Any],
    chunk: dict[str, Any],
    flags: list[str],
    chunk_path: Path | None,
) -> dict[str, Any]:
    tier = review_priority_tier(chunk, flags)
    severity = review_severity_for_flags(flags, tier)
    metadata = chunk_meta(chunk)
    table_flags = normalized_table_review_flags(chunk)
    uncertainty_report = metadata.get("parser_uncertainty") if isinstance(metadata.get("parser_uncertainty"), dict) else {}
    uncertainty_flags = parser_uncertainty_flags(metadata)
    return {
        "priority_rank": REVIEW_PRIORITY_ORDER.get(tier, 9),
        "priority_tier": tier,
        **severity,
        "review_action": review_action_for_tier(tier),
        "review_reason": ", ".join(flags),
        "review_group_key": review_group_key(row, chunk, metadata, flags),
        "document_id": row.get("document_id"),
        "filename": row.get("filename"),
        "extension": normalized_extension(row),
        "institution_name": row.get("institution_name"),
        "apba_id": row.get("apba_id"),
        "profile_id": row.get("profile_id"),
        "source_record_id": row.get("source_record_id"),
        "source_file_id": row.get("source_file_id"),
        "chunk_id": chunk.get("chunk_id"),
        "chunk_type": normalized_chunk_type(chunk),
        "article_no": chunk_get(chunk, "article_no", ""),
        "article_title": chunk_get(chunk, "article_title", ""),
        "page_start": chunk_get(chunk, "source_page_start"),
        "page_end": chunk_get(chunk, "source_page_end"),
        "table_like": bool(chunk_get(chunk, "table_like", False)),
        "table_review_required": bool(chunk_get(chunk, "table_review_required", False)),
        "table_review_flags": ", ".join(table_flags),
        "table_classification": chunk_get(chunk, "table_classification", ""),
        "table_review_reason": chunk_get(chunk, "table_review_reason", ""),
        "table_structured_row_count": safe_int(chunk_get(chunk, "table_structured_row_count", 0)),
        "table_record_count": safe_int(chunk_get(chunk, "table_record_count", 0)),
        "table_header_cells": _cell_preview(chunk_get(chunk, "table_header_cells", [])),
        "source_hwpx_block_types": ", ".join(str(value) for value in metadata.get("source_hwpx_block_types") or []),
        "source_hwpx_parser_review_flags": ", ".join(
            str(value) for value in metadata.get("source_hwpx_parser_review_flags") or []
        ),
        "source_hwpx_nested_table_count": safe_int(metadata.get("source_hwpx_nested_table_count")),
        "source_hwpx_table_image_count": safe_int(metadata.get("source_hwpx_table_image_count")),
        "source_hwpx_table_note_count": safe_int(metadata.get("source_hwpx_table_note_count")),
        "source_hwpx_merged_cell_count": safe_int(metadata.get("source_hwpx_merged_cell_count")),
        "source_hwpx_table_direct_captions": "; ".join(
            str(value) for value in metadata.get("source_hwpx_table_direct_captions") or []
        ),
        "source_hwpx_table_image_captions": "; ".join(
            str(value) for value in metadata.get("source_hwpx_table_image_captions") or []
        ),
        "source_hwpx_table_note_snippets": "; ".join(
            str(value) for value in metadata.get("source_hwpx_table_note_snippets") or []
        ),
        "source_hwpx_nested_table_text_snippets": "; ".join(
            str(value) for value in metadata.get("source_hwpx_nested_table_text_snippets") or []
        ),
        "source_hwpx_xml_block_indices": ", ".join(
            str(value) for value in metadata.get("source_hwpx_xml_block_indices") or []
        ),
        "source_hwp_extraction_modes": ", ".join(str(value) for value in metadata.get("source_hwp_extraction_modes") or []),
        "source_hwp_streams": ", ".join(str(value) for value in metadata.get("source_hwp_streams") or []),
        "source_hwp_section_indices": ", ".join(
            str(value) for value in metadata.get("source_hwp_section_indices") or []
        ),
        "source_hwp_native_table_geometry": _optional_bool_text(metadata.get("source_hwp_native_table_geometry")),
        "parser_uncertainty_source": str(
            metadata.get("parser_uncertainty_source") or uncertainty_report.get("source") or ""
        ),
        "parser_uncertainty_risk_level": parser_uncertainty_risk_level(metadata),
        "parser_uncertainty_confidence": metadata.get(
            "parser_uncertainty_confidence", uncertainty_report.get("confidence", "")
        ),
        "parser_uncertainty_flags": ", ".join(uncertainty_flags),
        "parser_uncertainty_recommendation": str(
            metadata.get("parser_uncertainty_recommendation") or uncertainty_report.get("recommendation") or ""
        ),
        "parser_uncertainty_remediation_hint": str(
            metadata.get("parser_uncertainty_remediation_hint") or uncertainty_report.get("remediation_hint") or ""
        ),
        "chunk_artifact": report_path(workspace, chunk_path),
        "snippet": snippet(str(chunk.get("text") or chunk.get("normalized_text") or ""), 260),
    }


def review_action_for_tier(tier: str) -> str:
    if tier == "blocking_review":
        return "review_before_citation_grade_use"
    if tier == "domain_attention":
        return "domain_reviewer_spot_check"
    if tier == "stable_false_positive":
        return "batch_verify_or_defer"
    return "retain_for_analysis"


def build_parsing_automation_payload(
    workspace: Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_generated_at()
    reports = load_batch_reports(workspace, batch_report_paths, reports_dir)
    rows = dedupe_batch_rows(reports)

    overall = make_metric_bucket()
    by_extension: dict[str, dict[str, Any]] = defaultdict(make_metric_bucket)
    by_apba_id: dict[str, dict[str, Any]] = defaultdict(make_metric_bucket)
    by_chunk_type: dict[str, dict[str, Any]] = defaultdict(make_metric_bucket)
    by_extension_and_chunk_type: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(make_metric_bucket))
    document_summaries: list[dict[str, Any]] = []
    missing_chunk_artifacts: list[dict[str, Any]] = []
    failed_documents: list[dict[str, Any]] = []
    review_flag_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    review_queue: list[dict[str, Any]] = []
    review_category_counts: Counter[str] = Counter()
    review_category_counts_by_apba_id: dict[str, Counter[str]] = defaultdict(Counter)
    overall_priority_counts: Counter[str] = Counter()
    priority_counts_by_extension: dict[str, Counter[str]] = defaultdict(Counter)
    priority_counts_by_apba_id: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        status = str(row.get("status") or "")
        extension = normalized_extension(row)
        apba_id = str(row.get("apba_id") or "missing")
        document_id = str(row.get("document_id") or "")
        if status not in {"completed", "skipped_unchanged"}:
            failed_documents.append(
                {
                    "document_id": document_id,
                    "filename": row.get("filename"),
                    "extension": extension,
                    "institution_name": row.get("institution_name"),
                    "apba_id": row.get("apba_id"),
                    "profile_id": row.get("profile_id"),
                    "source_record_id": row.get("source_record_id"),
                    "source_file_id": row.get("source_file_id"),
                    "status": status,
                    "failure_category": row.get("failure_category"),
                    "ocr_required": row.get("ocr_required"),
                }
            )
            continue

        chunk_path, chunks = load_document_chunks(workspace, document_id, row)
        if not chunks:
            missing_chunk_artifacts.append(
                {
                    "document_id": document_id,
                    "filename": row.get("filename"),
                    "extension": extension,
                    "institution_name": row.get("institution_name"),
                    "apba_id": row.get("apba_id"),
                    "profile_id": row.get("profile_id"),
                    "source_record_id": row.get("source_record_id"),
                    "source_file_id": row.get("source_file_id"),
                    "expected_chunk_file": report_path(workspace, chunk_path),
                }
            )
            continue

        doc_flag_counts: Counter[str] = Counter()
        doc_review_required = 0
        doc_priority_counts: Counter[str] = Counter()
        doc_type_counts: Counter[str] = Counter()
        seen_doc_types: set[str] = set()
        for chunk in chunks:
            chunk_type = normalized_chunk_type(chunk)
            flags = chunk_review_flags(chunk, row)
            actionable_flags = actionable_review_flags(flags)
            priority_tier = review_priority_tier(chunk, flags)
            doc_type_counts[chunk_type] += 1
            doc_priority_counts[priority_tier] += 1
            overall_priority_counts[priority_tier] += 1
            priority_counts_by_extension[extension][priority_tier] += 1
            priority_counts_by_apba_id[apba_id][priority_tier] += 1
            if flags:
                sample = review_flag_sample(workspace, row, chunk, flags, chunk_path)
                for flag in flags:
                    if len(review_flag_samples[flag]) < 8:
                        review_flag_samples[flag].append(sample)
            if actionable_flags:
                doc_review_required += 1
                queue_row = review_queue_row(workspace, row, chunk, flags, chunk_path)
                review_queue.append(queue_row)
                review_category = str(queue_row.get("review_category") or "unknown")
                review_category_counts[review_category] += 1
                review_category_counts_by_apba_id[apba_id][review_category] += 1
            doc_flag_counts.update(flags)

            overall["chunk_count"] += 1
            overall["review_required_candidate_count"] += 1 if actionable_flags else 0
            overall["flag_counts"].update(flags)

            by_extension[extension]["chunk_count"] += 1
            by_extension[extension]["review_required_candidate_count"] += 1 if actionable_flags else 0
            by_extension[extension]["flag_counts"].update(flags)

            by_apba_id[apba_id]["chunk_count"] += 1
            by_apba_id[apba_id]["review_required_candidate_count"] += 1 if actionable_flags else 0
            by_apba_id[apba_id]["flag_counts"].update(flags)

            by_chunk_type[chunk_type]["chunk_count"] += 1
            by_chunk_type[chunk_type]["review_required_candidate_count"] += 1 if actionable_flags else 0
            by_chunk_type[chunk_type]["flag_counts"].update(flags)

            matrix_bucket = by_extension_and_chunk_type[extension][chunk_type]
            matrix_bucket["chunk_count"] += 1
            matrix_bucket["review_required_candidate_count"] += 1 if actionable_flags else 0
            matrix_bucket["flag_counts"].update(flags)
            seen_doc_types.add(chunk_type)

        overall["document_count"] += 1
        by_extension[extension]["document_count"] += 1
        by_apba_id[apba_id]["document_count"] += 1
        for chunk_type in seen_doc_types:
            by_chunk_type[chunk_type]["document_count"] += 1
            by_extension_and_chunk_type[extension][chunk_type]["document_count"] += 1

        pipeline_counts = summarize_pipeline_counts(chunks, row)
        document_summaries.append(
            {
                "document_id": document_id,
                "filename": row.get("filename"),
                "extension": extension,
                "institution_name": row.get("institution_name"),
                "apba_id": row.get("apba_id"),
                "profile_id": row.get("profile_id"),
                "source_record_id": row.get("source_record_id"),
                "source_file_id": row.get("source_file_id"),
                "status": status,
                "source_batch_report": report_path(workspace, row.get("_batch_report")),
                "chunk_artifact": report_path(workspace, chunk_path),
                "pipeline_counts": pipeline_counts,
                "chunk_type_counts": dict(doc_type_counts),
                "review_flag_counts": dict(doc_flag_counts),
                "priority_counts": dict(doc_priority_counts),
                "quality_score": safe_float(row.get("quality_score")),
                "quality_passed": row.get("quality_passed"),
                "table_like_chunks_from_batch": safe_int(row.get("table_like_chunks")),
                "probable_table_false_positive_chunks_from_batch": safe_int(row.get("probable_table_false_positive_chunks")),
            }
        )

    sorted_review_queue = sorted(
        review_queue,
        key=lambda item: (
            safe_int(item.get("priority_rank")),
            safe_int(item.get("review_severity_rank")),
            str(item.get("extension") or ""),
            str(item.get("filename") or ""),
            safe_int(item.get("page_start")),
            str(item.get("chunk_id") or ""),
        ),
    )
    review_group_summary = annotate_review_groups(sorted_review_queue)

    payload = {
        "report_type": "parsing_automation_ratio",
        "generated_at": generated_at,
        "scope": {
            "workspace": workspace.name,
            "batch_reports": [report_path(workspace, path) for path, _ in reports],
            "input_document_rows": len(rows),
            "analyzed_document_count": overall["document_count"],
            "failed_document_count": len(failed_documents),
            "missing_chunk_artifact_count": len(missing_chunk_artifacts),
        },
        "measurement_kind": "heuristic_review_need_rate",
        "important_limitations": [
            "This report measures candidate human-review workload, not factual parsing accuracy.",
            "A chunk is review-needed when actionable parser metadata, table signals, warnings, OCR/encoding signals, supplementary/caption signals, or appendix/form signals with additional uncertainty indicate ambiguity.",
            "Appendix/form-only signals are retained in flag counts and goldset candidate lists, but are treated as informational instead of immediate review-queue work.",
            "True precision/recall requires a manually counted goldset.",
            "Review groups preserve chunk-level uncertainty while letting operators handle repeated rows from the same HWPX XML source block together.",
        ],
        "overall": finalize_metric_bucket(overall),
        "by_extension": {key: finalize_metric_bucket(value) for key, value in sorted(by_extension.items())},
        "by_apba_id": {key: finalize_metric_bucket(value) for key, value in sorted(by_apba_id.items())},
        "priority_summary": finalize_priority_counts(overall_priority_counts, overall["chunk_count"]),
        "review_group_summary": review_group_summary,
        "priority_by_extension": {
            extension: finalize_priority_counts(priority_counts, by_extension[extension]["chunk_count"])
            for extension, priority_counts in sorted(priority_counts_by_extension.items())
        },
        "priority_by_apba_id": {
            apba_id: finalize_priority_counts(priority_counts, by_apba_id[apba_id]["chunk_count"])
            for apba_id, priority_counts in sorted(priority_counts_by_apba_id.items())
        },
        "review_category_summary": {
            category: {
                "count": int(count),
                "rate_of_review_queue": percent(int(count), len(review_queue)),
            }
            for category, count in sorted(review_category_counts.items(), key=lambda item: (-item[1], item[0]))
        },
        "review_category_by_apba_id": {
            apba_id: {
                category: {
                    "count": int(count),
                    "rate_of_apba_review_queue": percent(int(count), sum(category_counts.values())),
                }
                for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
            }
            for apba_id, category_counts in sorted(review_category_counts_by_apba_id.items())
        },
        "by_chunk_type": {key: finalize_metric_bucket(value) for key, value in sorted(by_chunk_type.items())},
        "by_extension_and_chunk_type": {
            extension: {
                chunk_type: finalize_metric_bucket(bucket)
                for chunk_type, bucket in sorted(type_map.items())
            }
            for extension, type_map in sorted(by_extension_and_chunk_type.items())
        },
        "failed_documents": failed_documents,
        "missing_chunk_artifacts": missing_chunk_artifacts,
        "review_flag_samples": {key: value for key, value in sorted(review_flag_samples.items())},
        "review_queue": sorted_review_queue,
        "documents": sorted(
            document_summaries,
            key=lambda row: (
                -safe_float(row.get("pipeline_counts", {}).get("review_required_candidate_rate")),
                str(row.get("extension") or ""),
                str(row.get("filename") or ""),
            ),
        ),
    }
    return payload


def make_parsing_automation_markdown(payload: dict[str, Any]) -> str:
    overall = payload["overall"]
    lines = [
        f"# Parsing Automation Ratio Report",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Measurement kind: `{payload['measurement_kind']}`",
        f"- Batch reports: {', '.join(payload['scope']['batch_reports'])}",
        f"- Analyzed documents: {payload['scope']['analyzed_document_count']:,}",
        f"- Failed documents in source reports: {payload['scope']['failed_document_count']:,}",
        f"- Missing chunk artifacts: {payload['scope']['missing_chunk_artifact_count']:,}",
        "",
        "## Important Limitation",
        "",
        "This is a heuristic workload report, not a parsing accuracy report. It estimates how much content can pass through automation without immediate human review signals. Real article/table/appendix precision and recall require the manual goldset workflow.",
        "",
        "## Overall",
        "",
        f"- Total chunks: {overall['chunk_count']:,}",
        f"- Human-review candidate chunks: {overall['review_required_candidate_count']:,}",
        f"- Heuristic review need rate: {overall['review_required_candidate_rate']}%",
        f"- Heuristic automation pass-through rate: {round(100.0 - overall['review_required_candidate_rate'], 2)}%",
        "",
        "## Review Group Summary",
        "",
        f"- Review queue rows: {safe_int((payload.get('review_group_summary') or {}).get('review_queue_row_count')):,}",
        f"- Source review groups: {safe_int((payload.get('review_group_summary') or {}).get('review_group_count')):,}",
        f"- Duplicate rows grouped under an existing source: {safe_int((payload.get('review_group_summary') or {}).get('duplicate_review_queue_row_count')):,}",
        f"- Grouped review workload rate: {safe_float((payload.get('review_group_summary') or {}).get('grouped_review_workload_rate'))}%",
        "",
        "## Review Priority Tiers",
        "",
        "| Tier | Chunks | Rate | Meaning |",
        "| --- | ---: | ---: | --- |",
    ]
    tier_meanings = {
        "no_signal": "No current heuristic review signal.",
        "domain_attention": "Expected regulation-review area such as supplementary provision, table context, caption, or appendix/form content with additional uncertainty.",
        "blocking_review": "Needs human review before citation-grade use due to extraction, warning, OCR/encoding, or article-boundary uncertainty.",
        "stable_false_positive": "Known stable table false-positive pattern; track separately from urgent review.",
        "informational": "Non-blocking signal retained for analysis, including appendix/form-only structure annotations.",
    }
    for tier, summary in payload.get("priority_summary", {}).items():
        lines.append(f"| {tier} | {summary['count']:,} | {summary['rate']}% | {tier_meanings.get(tier, '')} |")
    lines.extend(
        [
            "",
            "## Priority By File Extension",
            "",
            "| Extension | No signal | Domain attention | Blocking review | Stable false positive | Informational |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for extension, summary in payload.get("priority_by_extension", {}).items():
        lines.append(
            f"| {extension} | {summary['no_signal']['count']:,} ({summary['no_signal']['rate']}%) | "
            f"{summary['domain_attention']['count']:,} ({summary['domain_attention']['rate']}%) | "
            f"{summary['blocking_review']['count']:,} ({summary['blocking_review']['rate']}%) | "
            f"{summary['stable_false_positive']['count']:,} ({summary['stable_false_positive']['rate']}%) | "
            f"{summary['informational']['count']:,} ({summary['informational']['rate']}%) |"
        )
    if payload.get("priority_by_apba_id"):
        lines.extend(
            [
                "",
                "## Priority By Source Record ID",
                "",
                "| apba_id | No signal | Domain attention | Blocking review | Stable false positive | Informational |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for apba_id, summary in payload.get("priority_by_apba_id", {}).items():
            lines.append(
                f"| {markdown_cell(apba_id)} | {summary['no_signal']['count']:,} ({summary['no_signal']['rate']}%) | "
                f"{summary['domain_attention']['count']:,} ({summary['domain_attention']['rate']}%) | "
                f"{summary['blocking_review']['count']:,} ({summary['blocking_review']['rate']}%) | "
                f"{summary['stable_false_positive']['count']:,} ({summary['stable_false_positive']['rate']}%) | "
                f"{summary['informational']['count']:,} ({summary['informational']['rate']}%) |"
            )
    if payload.get("review_category_summary"):
        lines.extend(
            [
                "",
                "## Review Category Summary",
                "",
                "| Category | Queue rows | Share of review queue |",
                "| --- | ---: | ---: |",
            ]
        )
        for category, summary in payload["review_category_summary"].items():
            lines.append(
                f"| {markdown_cell(category)} | {summary['count']:,} | {summary['rate_of_review_queue']}% |"
            )
    if payload.get("review_category_by_apba_id"):
        lines.extend(
            [
                "",
                "## Review Category By Source Record ID",
                "",
                "| apba_id | Category | Queue rows | Share within apba queue |",
                "| --- | --- | ---: | ---: |",
            ]
        )
        for apba_id, category_map in payload["review_category_by_apba_id"].items():
            for category, summary in category_map.items():
                lines.append(
                    f"| {markdown_cell(apba_id)} | {markdown_cell(category)} | {summary['count']:,} | {summary['rate_of_apba_review_queue']}% |"
                )
    review_queue = payload.get("review_queue") or []
    if review_queue:
        lines.extend(
            [
                "",
                "## Human Review Queue",
                "",
                "This queue is ordered for human review. Start with `blocking_review`, then severity categories such as OCR/encoding, table extraction, table structure, and article boundary review. Stable table false positives are tracked separately so they do not crowd out urgent parser uncertainty.",
                "",
                "| Priority | Severity | Category | Action | Extension | Document | Page | Chunk type | Chunk ID | Reasons | Focus | Snippet |",
                "| --- | ---: | --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- |",
            ]
        )
        for item in review_queue[:30]:
            lines.append(
                f"| {markdown_cell(item.get('priority_tier'))} | {markdown_cell(item.get('review_severity_rank'))} | "
                f"{markdown_cell(item.get('review_category'))} | {markdown_cell(item.get('review_action'))} | "
                f"{markdown_cell(item.get('extension'))} | {markdown_cell(item.get('filename'))} | "
                f"{markdown_cell(item.get('page_start'))} | {markdown_cell(item.get('chunk_type'))} | "
                f"{markdown_cell(item.get('chunk_id'))} | {markdown_cell(item.get('review_reason'))} | "
                f"{markdown_cell(item.get('review_focus'))} | {markdown_cell(item.get('snippet'))} |"
            )
    lines.extend(
        [
            "",
        "## By File Extension",
        "",
        "| Extension | Docs | Chunks | Review-needed chunks | Review need rate | Top flags |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for extension, bucket in payload["by_extension"].items():
        top_flags = ", ".join(f"{name}={count}" for name, count in Counter(bucket["flag_counts"]).most_common(4))
        lines.append(
            f"| {extension} | {bucket['document_count']:,} | {bucket['chunk_count']:,} | "
            f"{bucket['review_required_candidate_count']:,} | {bucket['review_required_candidate_rate']}% | {top_flags or '-'} |"
        )

    if payload.get("by_apba_id"):
        lines.extend(
            [
                "",
                "## By Source Record ID",
                "",
                "| apba_id | Docs | Chunks | Review-needed chunks | Review need rate | Top flags |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for apba_id, bucket in payload["by_apba_id"].items():
            top_flags = ", ".join(f"{name}={count}" for name, count in Counter(bucket["flag_counts"]).most_common(4))
            lines.append(
                f"| {markdown_cell(apba_id)} | {bucket['document_count']:,} | {bucket['chunk_count']:,} | "
                f"{bucket['review_required_candidate_count']:,} | {bucket['review_required_candidate_rate']}% | {top_flags or '-'} |"
            )

    lines.extend(
        [
            "",
            "## By Structure Type",
            "",
            "| Chunk type | Docs | Chunks | Review-needed chunks | Review need rate | Top flags |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for chunk_type, bucket in payload["by_chunk_type"].items():
        top_flags = ", ".join(f"{name}={count}" for name, count in Counter(bucket["flag_counts"]).most_common(4))
        lines.append(
            f"| {chunk_type} | {bucket['document_count']:,} | {bucket['chunk_count']:,} | "
            f"{bucket['review_required_candidate_count']:,} | {bucket['review_required_candidate_rate']}% | {top_flags or '-'} |"
        )

    lines.extend(
        [
            "",
            "## Extension x Structure Matrix",
            "",
            "| Extension | Chunk type | Docs | Chunks | Review-needed chunks | Review need rate |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for extension, type_map in payload["by_extension_and_chunk_type"].items():
        for chunk_type, bucket in type_map.items():
            lines.append(
                f"| {extension} | {chunk_type} | {bucket['document_count']:,} | {bucket['chunk_count']:,} | "
                f"{bucket['review_required_candidate_count']:,} | {bucket['review_required_candidate_rate']}% |"
            )

    lines.extend(["", "## Highest Review-Need Documents", ""])
    lines.extend(
        [
            "| Extension | Document | Chunks | Review need rate | Table-like | Top flags |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in payload["documents"][:20]:
        counts = row["pipeline_counts"]
        top_flags = ", ".join(f"{name}={count}" for name, count in Counter(row["review_flag_counts"]).most_common(4))
        lines.append(
            f"| {row['extension']} | {row['filename']} | {counts['chunk_count']:,} | "
            f"{counts['review_required_candidate_rate']}% | {counts['table_like_chunk_count']:,} | {top_flags or '-'} |"
        )

    if payload.get("review_flag_samples"):
        lines.extend(["", "## Review Flag Samples", ""])
        for flag, samples in payload["review_flag_samples"].items():
            lines.extend(
                [
                    f"### {flag}",
                    "",
                    "| Extension | Document | Chunk type | Page | Chunk ID | Snippet |",
                    "| --- | --- | --- | ---: | --- | --- |",
                ]
            )
            for sample in samples[:5]:
                page = sample.get("page_start") or ""
                lines.append(
                    f"| {markdown_cell(sample.get('extension'))} | {markdown_cell(sample.get('filename'))} | "
                    f"{markdown_cell(sample.get('chunk_type'))} | {markdown_cell(page)} | "
                    f"{markdown_cell(sample.get('chunk_id'))} | {markdown_cell(sample.get('snippet'))} |"
                )
            lines.append("")

    if payload["failed_documents"]:
        lines.extend(["", "## Failed Or OCR-Required Inputs", ""])
        lines.extend(["| Extension | Document | Status | Failure category | OCR required |", "| --- | --- | --- | --- | --- |"])
        for row in payload["failed_documents"]:
            lines.append(
                f"| {row.get('extension')} | {row.get('filename')} | {row.get('status')} | "
                f"{row.get('failure_category') or '-'} | {row.get('ocr_required') or '-'} |"
            )

    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            f"The current corpus shows a heuristic automation pass-through rate of {round(100.0 - overall['review_required_candidate_rate'], 2)}%. "
            "That does not mean the parser is that accurate. It means those chunks did not trigger known review signals. "
            "Human review should focus first on tables, supplementary provisions/effective dates, OCR/encoding noise, processor warnings, and appendices/forms with additional uncertainty.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_parsing_automation_report(
    workspace: Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    out_json: str | Path | None = None,
    out_md: str | Path | None = None,
    out_review_csv: str | Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Path]:
    timestamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = build_parsing_automation_payload(
        workspace,
        batch_report_paths,
        reports_dir,
        generated_at=utc_generated_at(),
    )
    reports_base = reports_dir if reports_dir.is_absolute() else workspace / reports_dir
    reports_base.mkdir(parents=True, exist_ok=True)
    json_path = resolve_workspace_path(workspace, out_json) if out_json else reports_base / f"parsing_automation_ratio_{timestamp}.json"
    md_path = resolve_workspace_path(workspace, out_md) if out_md else reports_base / f"parsing_automation_ratio_{timestamp}.md"
    review_csv_path = (
        resolve_workspace_path(workspace, out_review_csv)
        if out_review_csv
        else reports_base / f"parsing_review_queue_{timestamp}.csv"
    )
    if json_path is None or md_path is None or review_csv_path is None:
        raise ValueError("Output paths could not be resolved.")
    write_json(json_path, payload)
    md_path.write_text(make_parsing_automation_markdown(payload), encoding="utf-8")
    write_review_queue_csv(review_csv_path, payload.get("review_queue") or [])
    return {"json": json_path, "markdown": md_path, "review_csv": review_csv_path}


def write_review_queue_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "priority_rank",
        "priority_tier",
        "review_severity_rank",
        "review_category",
        "review_action",
        "review_focus",
        "review_step",
        "review_reason",
        "review_group_key",
        "review_group_duplicate_count",
        "review_group_primary",
        "extension",
        "filename",
        "institution_name",
        "apba_id",
        "profile_id",
        "source_record_id",
        "source_file_id",
        "document_id",
        "page_start",
        "page_end",
        "chunk_type",
        "chunk_id",
        "article_no",
        "article_title",
        "table_like",
        "table_review_required",
        "table_review_flags",
        "table_classification",
        "table_review_reason",
        "table_structured_row_count",
        "table_record_count",
        "table_header_cells",
        "source_hwpx_block_types",
        "source_hwpx_parser_review_flags",
        "source_hwpx_nested_table_count",
        "source_hwpx_table_image_count",
        "source_hwpx_table_note_count",
        "source_hwpx_merged_cell_count",
        "source_hwpx_table_direct_captions",
        "source_hwpx_table_image_captions",
        "source_hwpx_table_note_snippets",
        "source_hwpx_nested_table_text_snippets",
        "source_hwpx_xml_block_indices",
        "source_hwp_extraction_modes",
        "source_hwp_streams",
        "source_hwp_section_indices",
        "source_hwp_native_table_geometry",
        "parser_uncertainty_source",
        "parser_uncertainty_risk_level",
        "parser_uncertainty_confidence",
        "parser_uncertainty_flags",
        "parser_uncertainty_recommendation",
        "parser_uncertainty_remediation_hint",
        "chunk_artifact",
        "snippet",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def select_goldset_rows(rows: list[dict[str, Any]], size: int) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    usable = [
        row
        for row in rows
        if str(row.get("status") or "") in {"completed", "skipped_unchanged"}
        and normalized_extension(row) in {".hwp", ".hwpx", ".pdf", ".docx"}
    ]
    by_ext: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usable:
        by_ext[normalized_extension(row)].append(row)
    for candidates in by_ext.values():
        candidates.sort(
            key=lambda row: (
                safe_int(row.get("table_like_chunks"))
                + safe_int(row.get("issue_count"))
                + safe_int(row.get("warning_count"))
                + safe_int(row.get("probable_table_false_positive_chunks")),
                safe_int(row.get("chunk_count")),
            ),
            reverse=True,
        )

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def take(extension: str, count: int) -> None:
        before = len(selected)
        used_institutions = {
            str(row.get("institution_name") or "")
            for row in selected
            if normalized_extension(row) == extension and row.get("institution_name")
        }
        for row in by_ext.get(extension, []):
            if len(selected) - before >= count:
                break
            document_id = str(row.get("document_id") or "")
            institution = str(row.get("institution_name") or "")
            if document_id in selected_ids or institution in used_institutions:
                continue
            selected.append(row)
            selected_ids.add(document_id)
            if institution:
                used_institutions.add(institution)
        for row in by_ext.get(extension, []):
            if len(selected) - before >= count:
                break
            document_id = str(row.get("document_id") or "")
            if document_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(document_id)
        actual = len(selected) - before
        if actual < count:
            notes.append(f"Requested {count} {extension} documents but only selected {actual}.")

    take(".hwp", 3)
    take(".hwpx", 3)
    remaining = max(0, size - len(selected))
    for extension in (".pdf", ".docx", ".hwp", ".hwpx"):
        if remaining <= 0:
            break
        for row in by_ext.get(extension, []):
            if remaining <= 0:
                break
            document_id = str(row.get("document_id") or "")
            if document_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(document_id)
            remaining -= 1

    if ".docx" not in {normalized_extension(row) for row in selected}:
        notes.append("No DOCX document was available in the selected batch report; PDF documents fill the non-HWP/HWPX remainder.")
    return selected[:size], notes


def pipeline_candidate_lists(chunks: list[dict[str, Any]], document_row: dict[str, Any] | None = None, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {
        "articles": [],
        "appendices_forms": [],
        "supplementary_effective_dates": [],
        "tables": [],
        "footnotes_captions": [],
    }
    seen_articles: set[tuple[str, str]] = set()
    for chunk in chunks:
        flags = chunk_review_flags(chunk, document_row)
        text = review_body_text(chunk)
        base = {
            "chunk_id": chunk.get("chunk_id"),
            "chunk_type": normalized_chunk_type(chunk),
            "page_start": chunk_get(chunk, "source_page_start"),
            "page_end": chunk_get(chunk, "source_page_end"),
            "article_no": chunk_get(chunk, "article_no", ""),
            "article_title": chunk_get(chunk, "article_title", ""),
            "snippet": snippet(text, 160),
        }
        article_key = (str(base["article_no"]), str(base["article_title"]))
        if has_value(base["article_no"]) and article_key not in seen_articles and len(candidates["articles"]) < limit:
            candidates["articles"].append(base)
            seen_articles.add(article_key)
        if "form_or_appendix_candidate" in flags and len(candidates["appendices_forms"]) < limit:
            candidates["appendices_forms"].append(base)
        if "supplementary_or_effective_date_candidate" in flags and len(candidates["supplementary_effective_dates"]) < limit:
            candidates["supplementary_effective_dates"].append(base)
        if chunk_get(chunk, "table_like", False) and len(candidates["tables"]) < limit:
            candidates["tables"].append(
                {
                    **base,
                    "table_title": chunk_get(chunk, "table_title", ""),
                    "table_review_required": chunk_get(chunk, "table_review_required", False),
                    "table_classification": chunk_get(chunk, "table_classification", ""),
                    "table_citation_label": chunk_get(chunk, "table_citation_label", ""),
                    "table_review_flags": ", ".join(normalized_table_review_flags(chunk)),
                    "source_parser_flags": ", ".join(
                        str(value)
                        for value in chunk_meta(chunk).get("source_hwpx_parser_review_flags") or []
                    ),
                    "table_structured_row_count": chunk_get(chunk, "table_structured_row_count", ""),
                    "table_column_count": chunk_get(chunk, "table_column_count", ""),
                }
            )
        if "footnote_or_caption_candidate" in flags and len(candidates["footnotes_captions"]) < limit:
            candidates["footnotes_captions"].append(base)
    return candidates


def append_candidate_section(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend([f"#### {title}", ""])
    if not rows:
        lines.extend(["- No candidates extracted.", ""])
        return
    lines.extend(
        [
            "| Chunk ID | Chunk type | Page | Article | Extra | Snippet |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in rows:
        article = " ".join(str(value) for value in [row.get("article_no"), row.get("article_title")] if value)
        page = str(row.get("page_start") or "")
        if row.get("page_end") and row.get("page_end") != row.get("page_start"):
            page = f"{page}-{row.get('page_end')}"
        extra_values = [
            f"label={row.get('table_citation_label')}" if row.get("table_citation_label") else "",
            row.get("table_title"),
            row.get("table_classification"),
            f"table_review_required={row.get('table_review_required')}" if "table_review_required" in row else "",
            f"flags={row.get('table_review_flags')}" if row.get("table_review_flags") else "",
            f"parser_flags={row.get('source_parser_flags')}" if row.get("source_parser_flags") else "",
            f"rows={row.get('table_structured_row_count')}" if row.get("table_structured_row_count") not in (None, "") else "",
            f"cols={row.get('table_column_count')}" if row.get("table_column_count") not in (None, "") else "",
        ]
        extra = ", ".join(str(value) for value in extra_values if value not in (None, ""))
        lines.append(
            f"| {markdown_cell(row.get('chunk_id') or '')} | {markdown_cell(row.get('chunk_type'))} | {markdown_cell(page)} | "
            f"{markdown_cell(article)} | {markdown_cell(extra)} | {markdown_cell(row.get('snippet'))} |"
        )
    lines.append("")


def make_goldset_markdown(
    workspace: Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    *,
    size: int = 12,
    generated_at: str | None = None,
) -> str:
    generated_at = generated_at or utc_generated_at()
    reports = load_batch_reports(workspace, batch_report_paths, reports_dir)
    rows = dedupe_batch_rows(reports)
    selected, notes = select_goldset_rows(rows, size)

    lines = [
        "# Parsing Manual Goldset Worksheet",
        "",
        f"- Generated at: {generated_at}",
        f"- Batch reports: {', '.join(report_path(workspace, path) for path, _ in reports)}",
        f"- Selected documents: {len(selected)}",
        "",
        "## Purpose",
        "",
        "This worksheet is the manual goldset step. It intentionally does not claim parser precision or recall until a human counts the true article, paragraph/item, appendix/form, table, supplementary-provision, effective-date, footnote/endnote, and caption facts in each source document.",
        "",
        "## Manual Counting Fields",
        "",
        "For each document, fill the `Manual` columns by inspecting the original document. Then compare them with the pipeline counts below.",
        "",
        "| Field | Human task | Pipeline proxy |",
        "| --- | --- | --- |",
        "| Articles | Count true regulation articles. | Distinct `article_no` in chunks. |",
        "| Paragraph/items | Count paragraph/item/subitem units where visible. | Paragraph/item/clause chunks plus paragraph metadata. |",
        "| Appendices/forms | Count appendices, forms, and form-like attachments. | Appendix/form candidates and refs. |",
        "| Tables | Count true tables and nested tables. | `table_like` and `table_review_required` chunks. |",
        "| Supplementary provisions/effective dates | Count supplementary provisions and effective/application-date facts. | Supplementary/effective-date candidates. |",
        "| Footnotes/endnotes/captions | Count explicit footnotes, endnotes, and figure/table captions. | Footnote/caption candidates. |",
        "",
        "## Selected Documents",
        "",
        "| # | Ext | Institution | Document | Document ID | Chunks | Pipeline articles | Pipeline appendix/forms | Pipeline tables | Review need rate | Chunk artifact |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    document_details: list[tuple[dict[str, Any], dict[str, Any], str, dict[str, list[dict[str, Any]]]]] = []
    for index, row in enumerate(selected, start=1):
        document_id = str(row.get("document_id") or "")
        chunk_path, chunks = load_document_chunks(workspace, document_id, row)
        pipeline = summarize_pipeline_counts(chunks, row)
        artifact = report_path(workspace, chunk_path)
        document_details.append((row, pipeline, artifact, pipeline_candidate_lists(chunks, row)))
        lines.append(
            f"| {index} | {normalized_extension(row)} | {row.get('institution_name') or ''} | {row.get('filename') or ''} | "
            f"{document_id} | {pipeline['chunk_count']:,} | {pipeline['article_count_distinct_article_no']:,} | "
            f"{pipeline['appendix_or_form_candidate_count']:,} | {pipeline['table_like_chunk_count']:,} | "
            f"{pipeline['review_required_candidate_rate']}% | {artifact} |"
        )

    if notes:
        lines.extend(["", "## Selection Notes", ""])
        for note in notes:
            lines.append(f"- {note}")

    lines.extend(
        [
            "",
            "## Accuracy Calculation Template",
            "",
            "| # | Document ID | Manual articles | Pipeline articles | Article precision | Article recall | Manual tables | Pipeline tables | Table preservation notes | Footnote/caption connection notes |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for index, (row, pipeline, _, _) in enumerate(document_details, start=1):
        lines.append(
            f"| {index} | {row.get('document_id')} | TBD | {pipeline['article_count_distinct_article_no']} | TBD | TBD | "
            f"TBD | {pipeline['table_like_chunk_count']} | TBD | TBD |"
        )

    lines.extend(["", "## Per-Document Pipeline Summary", ""])
    for index, (row, pipeline, artifact, candidates) in enumerate(document_details, start=1):
        lines.extend(
            [
                f"### {index}. {row.get('filename')}",
                "",
                f"- Document ID: `{row.get('document_id')}`",
                f"- Extension: `{normalized_extension(row)}`",
                f"- Institution: {row.get('institution_name') or ''}",
                f"- Source path: {report_path(workspace, row.get('input_path'))}",
                f"- Chunk artifact: {artifact}",
                f"- Pipeline chunk count: {pipeline['chunk_count']:,}",
                f"- Pipeline distinct article count: {pipeline['article_count_distinct_article_no']:,}",
                f"- Pipeline paragraph/item proxy count: {pipeline['paragraph_or_item_chunk_count']:,}",
                f"- Pipeline appendix/form candidate count: {pipeline['appendix_or_form_candidate_count']:,}",
                f"- Pipeline table-like chunk count: {pipeline['table_like_chunk_count']:,}",
                f"- Pipeline table review-required count: {pipeline['table_review_required_chunk_count']:,}",
                f"- Pipeline supplementary/effective-date candidate count: {pipeline['supplementary_or_effective_date_candidate_count']:,}",
                f"- Pipeline footnote/caption candidate count: {pipeline['footnote_or_caption_candidate_count']:,}",
                f"- Pipeline review-needed candidate rate: {pipeline['review_required_candidate_rate']}%",
                "",
                "Manual counts to fill:",
                "",
                "- Manual article count: TBD",
                "- Manual paragraph/item count: TBD",
                "- Manual appendix/form count: TBD",
                "- Manual true table count: TBD",
                "- Manual nested table count: TBD",
                "- Manual supplementary provision/effective-date count: TBD",
                "- Manual footnote/endnote count: TBD",
                "- Manual caption count: TBD",
                "- Parsing misses/false positives: TBD",
                "",
            ]
        )
        append_candidate_section(lines, "Pipeline article candidates", candidates["articles"])
        append_candidate_section(lines, "Pipeline appendix/form candidates", candidates["appendices_forms"])
        append_candidate_section(lines, "Pipeline supplementary/effective-date candidates", candidates["supplementary_effective_dates"])
        append_candidate_section(lines, "Pipeline table candidates", candidates["tables"])
        append_candidate_section(lines, "Pipeline footnote/caption candidates", candidates["footnotes_captions"])

    lines.extend(
        [
            "## Completion Rule",
            "",
            "After manual counts are filled, compute precision/recall per structure type. Until then, this file is a review worksheet, not a completed accuracy score.",
        ]
    )
    return "\n".join(lines) + "\n"


def make_goldset_label_template_rows(
    workspace: Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    *,
    size: int = 12,
) -> list[dict[str, Any]]:
    reports = load_batch_reports(workspace, batch_report_paths, reports_dir)
    rows = dedupe_batch_rows(reports)
    selected, _ = select_goldset_rows(rows, size)
    label_rows: list[dict[str, Any]] = []
    for index, row in enumerate(selected, start=1):
        document_id = str(row.get("document_id") or "")
        chunk_path, chunks = load_document_chunks(workspace, document_id, row)
        pipeline = summarize_pipeline_counts(chunks, row)
        label_row: dict[str, Any] = {
            "review_order": index,
            "label_status": "pending_human_review",
            "document_id": document_id,
            "extension": normalized_extension(row),
            "institution_name": row.get("institution_name") or "",
            "filename": row.get("filename") or "",
            "source_path": report_path(workspace, row.get("input_path")),
            "chunk_artifact": report_path(workspace, chunk_path),
            "reviewer": "",
            "reviewed_at": "",
        }
        for spec in GOLDSET_SCORE_SPECS.values():
            label_row[spec["pipeline_field"]] = safe_int(pipeline.get(spec["pipeline_summary_field"]))
            label_row[spec["manual_field"]] = ""
            label_row[spec["match_field"]] = ""
        label_row.update(
            {
                "pipeline_paragraph_marker_count_circled": safe_int(pipeline.get("paragraph_marker_count_circled")),
                "pipeline_numbered_item_count": safe_int(pipeline.get("numbered_item_count")),
                "pipeline_hangul_item_count": safe_int(pipeline.get("hangul_item_count")),
                "pipeline_parenthesized_item_count": safe_int(pipeline.get("parenthesized_item_count")),
                "pipeline_annex_count": safe_int(pipeline.get("annex_candidate_count")),
                "pipeline_form_count": safe_int(pipeline.get("form_candidate_count")),
                "pipeline_sheet_count": safe_int(pipeline.get("sheet_candidate_count")),
                "pipeline_supplementary_block_count": safe_int(pipeline.get("supplementary_block_count")),
                "pipeline_supplementary_blocks_with_effective_date_count": safe_int(
                    pipeline.get("supplementary_blocks_with_effective_date_count")
                ),
                "pipeline_explicit_effective_article_count": safe_int(pipeline.get("explicit_effective_article_count")),
                "pipeline_direct_effective_clause_count": safe_int(pipeline.get("direct_effective_clause_count")),
                "pipeline_application_clause_count": safe_int(pipeline.get("application_clause_count")),
            }
        )
        label_row.update(
            {
                "table_preservation_notes": "",
                "footnote_caption_connection_notes": "",
                "parser_miss_false_positive_notes": "",
            }
        )
        label_rows.append(label_row)
    return label_rows


def write_goldset_report(
    workspace: Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    out_md: str | Path | None = None,
    out_labels_csv: str | Path | None = None,
    timestamp: str | None = None,
    size: int = 12,
) -> dict[str, Path]:
    timestamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    reports_base = reports_dir if reports_dir.is_absolute() else workspace / reports_dir
    reports_base.mkdir(parents=True, exist_ok=True)
    md_path = resolve_workspace_path(workspace, out_md) if out_md else reports_base / f"parsing_manual_goldset_{timestamp}.md"
    labels_path = (
        resolve_workspace_path(workspace, out_labels_csv)
        if out_labels_csv
        else reports_base / f"parsing_manual_goldset_labels_{timestamp}.csv"
    )
    if md_path is None or labels_path is None:
        raise ValueError("Output path could not be resolved.")
    md_path.write_text(
        make_goldset_markdown(
            workspace,
            batch_report_paths,
            reports_dir,
            size=size,
            generated_at=utc_generated_at(),
        ),
        encoding="utf-8",
    )
    write_csv(
        labels_path,
        make_goldset_label_template_rows(
            workspace,
            batch_report_paths,
            reports_dir,
            size=size,
        ),
    )
    return {"markdown": md_path, "labels_csv": labels_path}


def _goldset_packet_filename(label_row: dict[str, Any]) -> str:
    order = safe_int(label_row.get("review_order"))
    document_id = str(label_row.get("document_id") or "missing-document-id").strip() or "missing-document-id"
    stem = Path(str(label_row.get("filename") or document_id)).stem
    safe_stem = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-")[:60] or "document"
    prefix = f"{order:02d}" if order else "00"
    return f"{prefix}_{document_id}_{safe_stem}.md"


def _load_chunks_for_label_row(workspace: Path, label_row: dict[str, Any]) -> tuple[Path | None, list[dict[str, Any]]]:
    chunk_artifact = str(label_row.get("chunk_artifact") or "").strip()
    if chunk_artifact:
        chunk_path = resolve_workspace_path(workspace, chunk_artifact)
        if chunk_path and chunk_path.exists():
            payload = load_json_file(chunk_path)
            if isinstance(payload, list):
                return chunk_path, [row for row in payload if isinstance(row, dict)]
    document_id = str(label_row.get("document_id") or "")
    return load_document_chunks(workspace, document_id, label_row)


def _structure_review_hint(structure_type: str) -> str:
    hints = {
        "article": "Count source-document article boundaries and match pipeline chunks with the same article number/title.",
        "paragraph_item": "Count visible paragraph, item, subitem, or clause units where the source document makes them explicit; HWP inventory uses line-leading circled, numbered, Hangul, and parenthesized markers within the main body.",
        "appendix_form": "Count appendices, forms, attached forms, and form-like attachments in the source document.",
        "table": "Count true source tables and mark matches only when table content is preserved enough for citation review.",
        "nested_table": "Count nested tables separately; match only when the nested table evidence is preserved or explicitly flagged for review.",
        "supplementary_effective_date": "Count supplementary provisions plus effective/application-date facts that affect answer timing.",
        "footnote_caption": "Count explicit footnotes, endnotes, table captions, figure captions, and caption-linked notes. General ※, *, and 주: note lines are retained as source evidence but are not counted as footnote/caption matches by default.",
    }
    return hints.get(structure_type, "Count source-document structures and matched true positives.")


def make_goldset_review_packet_markdown(workspace: Path, label_row: dict[str, Any]) -> str:
    document_id = str(label_row.get("document_id") or "").strip()
    chunk_path, chunks = _load_chunks_for_label_row(workspace, label_row)
    pipeline = summarize_pipeline_counts(chunks, label_row)
    candidates = pipeline_candidate_lists(chunks, label_row)
    lines = [
        f"# Parsing Goldset Review Packet: {label_row.get('review_order') or '-'}",
        "",
        f"- Document ID: `{document_id}`",
        f"- Label status: `{label_row.get('label_status') or ''}`",
        f"- Extension: `{label_row.get('extension') or ''}`",
        f"- Institution: {label_row.get('institution_name') or ''}",
        f"- File: {label_row.get('filename') or ''}",
        f"- Source path: {label_row.get('source_path') or ''}",
        f"- Chunk artifact: {report_path(workspace, chunk_path) if chunk_path else label_row.get('chunk_artifact') or ''}",
        f"- Pipeline chunk count: {pipeline['chunk_count']:,}",
        f"- Pipeline review-needed candidate rate: {pipeline['review_required_candidate_rate']}%",
        "",
        "## Reviewer Rule",
        "",
        "Do not use pipeline counts as the manual answer. Inspect the source document, fill manual counts, then fill matched counts by comparing the source structures with pipeline candidates.",
        "",
        "Set `label_status=reviewed`, `reviewer`, and `reviewed_at` only after every manual and matched field below is complete.",
        "",
        "## Required Label Fields",
        "",
        "| Structure | Pipeline field | Pipeline count | Manual field | Current manual | Matched field | Current matched | Review hint |",
        "| --- | --- | ---: | --- | ---: | --- | ---: | --- |",
    ]
    for structure_type, spec in GOLDSET_SCORE_SPECS.items():
        pipeline_value = optional_int(label_row.get(spec["pipeline_field"]))
        if pipeline_value is None:
            pipeline_value = safe_int(pipeline.get(spec["pipeline_summary_field"]))
        lines.append(
            f"| {structure_type} | `{spec['pipeline_field']}` | {pipeline_value:,} | "
            f"`{spec['manual_field']}` | {markdown_cell(label_row.get(spec['manual_field']))} | "
            f"`{spec['match_field']}` | {markdown_cell(label_row.get(spec['match_field']))} | "
            f"{markdown_cell(_structure_review_hint(structure_type))} |"
        )

    lines.extend(
        [
            "",
            "## Review Notes To Fill",
            "",
            "- `table_preservation_notes`: ",
            "- `footnote_caption_connection_notes`: ",
            "- `parser_miss_false_positive_notes`: ",
            "",
            "## Pipeline Summary",
            "",
            f"- Distinct article count: {pipeline['article_count_distinct_article_no']:,}",
            f"- Paragraph/item proxy count: {pipeline['paragraph_or_item_chunk_count']:,}",
            "- Paragraph/item breakdown: "
            f"circled={safe_int(pipeline.get('paragraph_marker_count_circled')):,}, "
            f"numbered={safe_int(pipeline.get('numbered_item_count')):,}, "
            f"hangul={safe_int(pipeline.get('hangul_item_count')):,}, "
            f"parenthesized={safe_int(pipeline.get('parenthesized_item_count')):,}",
            f"- Appendix/form candidate count: {pipeline['appendix_or_form_candidate_count']:,}",
            "- Appendix/form breakdown: "
            f"annexes={safe_int(pipeline.get('annex_candidate_count')):,}, "
            f"forms={safe_int(pipeline.get('form_candidate_count')):,}, "
            f"sheets={safe_int(pipeline.get('sheet_candidate_count')):,}",
            f"- Table-like chunk count: {pipeline['table_like_chunk_count']:,}",
            f"- Nested-table candidate count: {pipeline['nested_table_candidate_count']:,}",
            f"- Supplementary/effective-date candidate count: {pipeline['supplementary_or_effective_date_candidate_count']:,}",
            "- Supplementary/effective-date breakdown: "
            f"blocks={safe_int(pipeline.get('supplementary_block_count')):,}, "
            f"blocks_with_effective_date={safe_int(pipeline.get('supplementary_blocks_with_effective_date_count')):,}, "
            f"explicit_effective_articles={safe_int(pipeline.get('explicit_effective_article_count')):,}, "
            f"direct_effective_clauses={safe_int(pipeline.get('direct_effective_clause_count')):,}, "
            f"application_clauses={safe_int(pipeline.get('application_clause_count')):,}",
            f"- Footnote/caption candidate count: {pipeline['footnote_or_caption_candidate_count']:,}",
            "",
        ]
    )
    append_candidate_section(lines, "Pipeline article candidates", candidates["articles"])
    append_candidate_section(lines, "Pipeline appendix/form candidates", candidates["appendices_forms"])
    append_candidate_section(lines, "Pipeline supplementary/effective-date candidates", candidates["supplementary_effective_dates"])
    append_candidate_section(lines, "Pipeline table candidates", candidates["tables"])
    append_candidate_section(lines, "Pipeline footnote/caption candidates", candidates["footnotes_captions"])
    return "\n".join(lines) + "\n"


def write_goldset_review_packets(
    workspace: Path,
    labels_path: str | Path,
    out_dir: str | Path,
) -> dict[str, Path]:
    packet_dir = resolve_workspace_path(workspace, out_dir)
    if packet_dir is None:
        raise ValueError("Packet output directory could not be resolved.")
    packet_dir.mkdir(parents=True, exist_ok=True)
    label_rows = load_goldset_label_rows(labels_path)
    packet_paths: list[Path] = []
    for label_row in label_rows:
        packet_path = packet_dir / _goldset_packet_filename(label_row)
        packet_path.write_text(make_goldset_review_packet_markdown(workspace, label_row), encoding="utf-8")
        packet_paths.append(packet_path)
    index_path = packet_dir / "README.md"
    lines = [
        "# Parsing Goldset Review Packets",
        "",
        f"- Label source: `{report_path(workspace, labels_path)}`",
        f"- Packet count: {len(packet_paths)}",
        "",
        "| # | Document ID | Institution | File | Packet |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for index, (label_row, packet_path) in enumerate(zip(label_rows, packet_paths, strict=False), start=1):
        lines.append(
            f"| {index} | `{label_row.get('document_id') or ''}` | {markdown_cell(label_row.get('institution_name') or '')} | "
            f"{markdown_cell(label_row.get('filename') or '')} | `{report_path(workspace, packet_path)}` |"
        )
    lines.extend(
        [
            "",
            "## Completion Rule",
            "",
            "A packet is complete only after every manual and matched count is filled in the label CSV, reviewer metadata is present, and the score command passes with `--fail-on-goldset-issue`.",
        ]
    )
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"index": index_path, "packet_dir": packet_dir}


def refresh_goldset_label_rows(
    workspace: Path,
    label_rows: list[dict[str, Any]],
    batch_report_paths: list[str | Path],
    reports_dir: Path,
) -> list[dict[str, Any]]:
    pipeline_index = build_pipeline_count_index(workspace, batch_report_paths, reports_dir)
    refreshed: list[dict[str, Any]] = []
    for label_row in label_rows:
        row = dict(label_row)
        pipeline_info = resolve_pipeline_count_info(workspace, pipeline_index, label_row)
        pipeline_summary = pipeline_info.get("pipeline_counts") if isinstance(pipeline_info, dict) else None
        changed_structures: list[str] = []
        if pipeline_summary:
            if pipeline_info.get("document_id"):
                row["current_runtime_document_id"] = pipeline_info["document_id"]
            if pipeline_info.get("source_batch_report"):
                row["current_runtime_batch_report"] = pipeline_info["source_batch_report"]
            if pipeline_info.get("chunk_artifact"):
                row["chunk_artifact"] = pipeline_info["chunk_artifact"]
            for structure_type, spec in GOLDSET_SCORE_SPECS.items():
                old_pipeline = optional_int(row.get(spec["pipeline_field"]))
                new_pipeline = safe_int(pipeline_summary.get(spec["pipeline_summary_field"]))
                row[spec["pipeline_field"]] = new_pipeline
                if old_pipeline is not None and old_pipeline != new_pipeline:
                    changed_structures.append(structure_type)
                    row[spec["match_field"]] = ""
        else:
            changed_structures.append("pipeline_match_missing")

        if changed_structures:
            row["label_status"] = "pending_human_review"
            note = str(row.get("parser_miss_false_positive_notes") or "").strip()
            suffix = "pipeline refreshed; rerun matched-count review for: " + ", ".join(changed_structures)
            row["parser_miss_false_positive_notes"] = f"{note} | {suffix}" if note else suffix
        refreshed.append(row)
    return refreshed


def write_refreshed_goldset_labels(
    workspace: Path,
    labels_path: str | Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    out_labels_csv: str | Path,
) -> dict[str, Path]:
    out_path = resolve_workspace_path(workspace, out_labels_csv)
    if out_path is None:
        raise ValueError("Output label CSV path could not be resolved.")
    rows = refresh_goldset_label_rows(
        workspace,
        load_goldset_label_rows(labels_path),
        batch_report_paths,
        reports_dir,
    )
    write_csv(out_path, rows)
    return {"labels_csv": out_path}


GOLDSET_SCORE_SPECS: dict[str, dict[str, str]] = {
    "article": {
        "manual_field": "manual_article_count",
        "pipeline_field": "pipeline_article_count",
        "match_field": "matched_article_count",
        "pipeline_summary_field": "article_count_distinct_article_no",
    },
    "paragraph_item": {
        "manual_field": "manual_paragraph_item_count",
        "pipeline_field": "pipeline_paragraph_item_count",
        "match_field": "matched_paragraph_item_count",
        "pipeline_summary_field": "paragraph_or_item_chunk_count",
    },
    "appendix_form": {
        "manual_field": "manual_appendix_form_count",
        "pipeline_field": "pipeline_appendix_form_count",
        "match_field": "matched_appendix_form_count",
        "pipeline_summary_field": "appendix_or_form_candidate_count",
    },
    "table": {
        "manual_field": "manual_table_count",
        "pipeline_field": "pipeline_table_count",
        "match_field": "matched_table_count",
        "pipeline_summary_field": "table_goldset_preserved_count",
    },
    "nested_table": {
        "manual_field": "manual_nested_table_count",
        "pipeline_field": "pipeline_nested_table_count",
        "match_field": "matched_nested_table_count",
        "pipeline_summary_field": "nested_table_candidate_count",
    },
    "supplementary_effective_date": {
        "manual_field": "manual_supplementary_effective_date_count",
        "pipeline_field": "pipeline_supplementary_effective_date_count",
        "match_field": "matched_supplementary_effective_date_count",
        "pipeline_summary_field": "supplementary_or_effective_date_candidate_count",
    },
    "footnote_caption": {
        "manual_field": "manual_footnote_caption_count",
        "pipeline_field": "pipeline_footnote_caption_count",
        "match_field": "matched_footnote_caption_count",
        "pipeline_summary_field": "footnote_or_caption_candidate_count",
    },
}

GOLDSET_COMPLETE_LABEL_STATUSES = {
    "reviewed",
    "human_reviewed",
    "completed",
    "approved",
}


def effective_goldset_matched_count(
    *,
    manual_count: int | None,
    pipeline_count: int | None,
    matched_count: int | None,
) -> tuple[int | None, str]:
    if matched_count is not None:
        return matched_count, "label_file"
    if (
        manual_count is not None
        and pipeline_count is not None
        and manual_count >= 0
        and pipeline_count >= 0
        and (manual_count == 0 or pipeline_count == 0)
    ):
        return 0, "derived_zero_bound"
    return None, "missing"


GOLDSET_SCOPE_NOTE_FIELDS = (
    "notes",
    "parser_miss_false_positive_notes",
    "table_preservation_notes",
    "footnote_caption_connection_notes",
)
NON_REGULATION_GOLDSET_SCOPE_PATTERNS = (
    "\uc870\ubb38\ud615\ud0dc \uc544\ub2d8",
    "\uc870\ubb38 \ud615\ud0dc \uc544\ub2d8",
    "non-article",
    "not article",
    "not regulation",
)


def load_goldset_label_rows(path: str | Path) -> list[dict[str, Any]]:
    label_path = Path(path)
    suffix = label_path.suffix.lower()
    if suffix == ".csv":
        with label_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    payload = load_json_file(label_path)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [row for row in payload["rows"] if isinstance(row, dict)]
    raise ValueError("Goldset labels must be a CSV, a JSON list, or a JSON object with a rows list.")


def build_pipeline_count_index(
    workspace: Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
) -> dict[str, dict[str, Any]]:
    reports = load_batch_reports(workspace, batch_report_paths, reports_dir)
    rows = dedupe_batch_rows(reports)
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        document_id = str(row.get("document_id") or "")
        if not document_id or str(row.get("status") or "") not in {"completed", "skipped_unchanged"}:
            continue
        chunk_path, chunks = load_document_chunks(workspace, document_id, row)
        info = {
            "document_id": document_id,
            "filename": row.get("filename"),
            "institution_name": row.get("institution_name"),
            "extension": normalized_extension(row),
            "chunk_artifact": report_path(workspace, chunk_path),
            "source_batch_report": report_path(workspace, row.get("_batch_report")),
            "pipeline_counts": summarize_pipeline_counts(chunks, row),
        }
        for key in pipeline_count_lookup_keys(workspace, row):
            _add_pipeline_index_alias(index, key, info)
    return index


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ratio_percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return percent(numerator, denominator)


def f1_percent(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall <= 0:
        return 0.0
    return round((2 * precision * recall) / (precision + recall), 2)


def _pipeline_count_source_for_structure(
    structure_type: str,
    pipeline_summary: dict[str, Any] | None,
) -> str:
    if not pipeline_summary:
        return "label_file"
    source_fields = {
        "paragraph_item": "paragraph_item_count_source",
        "appendix_form": "appendix_or_form_count_source",
        "table": "table_goldset_count_source",
        "nested_table": "nested_table_count_source",
        "supplementary_effective_date": "supplementary_count_source",
        "footnote_caption": "footnote_or_caption_count_source",
    }
    source_field = source_fields.get(structure_type)
    if not source_field:
        return "pipeline_summary"
    return str(pipeline_summary.get(source_field) or "pipeline_summary")


def _pipeline_count_breakdown_for_structure(
    structure_type: str,
    pipeline_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if not pipeline_summary:
        return {}
    if structure_type == "paragraph_item":
        return {
            "structural_article_body_paragraph_item_count": safe_int(
                pipeline_summary.get("structural_article_body_paragraph_item_count")
            ),
            "paragraph_item_traceable_unit_count": safe_int(
                pipeline_summary.get("paragraph_item_traceable_unit_count")
            ),
            "paragraph_item_inventory_candidate_count": safe_int(
                pipeline_summary.get("paragraph_item_inventory_candidate_count")
            ),
            "visible_article_body_paragraph_item_count": safe_int(
                pipeline_summary.get("visible_article_body_paragraph_item_count")
            ),
            "paragraph_item_visible_marker_candidate_count": safe_int(
                pipeline_summary.get("paragraph_item_visible_marker_candidate_count")
            ),
            "paragraph_marker_count_circled": safe_int(pipeline_summary.get("paragraph_marker_count_circled")),
            "numbered_item_count": safe_int(pipeline_summary.get("numbered_item_count")),
            "hangul_item_count": safe_int(pipeline_summary.get("hangul_item_count")),
            "parenthesized_item_count": safe_int(pipeline_summary.get("parenthesized_item_count")),
        }
    if structure_type == "table":
        return {
            "table_goldset_preserved_count": safe_int(pipeline_summary.get("table_goldset_preserved_count")),
            "table_like_chunk_count": safe_int(pipeline_summary.get("table_like_chunk_count")),
            "table_citation_ready_chunk_count": safe_int(pipeline_summary.get("table_citation_ready_chunk_count")),
            "kordoc_promoted_table_unit_count": safe_int(pipeline_summary.get("kordoc_promoted_table_unit_count")),
            "hwp_inventory_table_like_chunk_count": safe_int(
                pipeline_summary.get("hwp_inventory_table_like_chunk_count")
            ),
            "page_less_kordoc_only_table_count": safe_int(
                pipeline_summary.get("page_less_kordoc_only_table_count")
            ),
            "table_like_without_cell_rows_count": safe_int(
                pipeline_summary.get("table_like_without_cell_rows_count")
            ),
            "table_review_required_chunk_count": safe_int(pipeline_summary.get("table_review_required_chunk_count")),
        }
    if structure_type == "nested_table":
        return {
            "nested_table_count_source": str(pipeline_summary.get("nested_table_count_source") or ""),
            "source_hwpx_nested_table_count": safe_int(pipeline_summary.get("source_hwpx_nested_table_count")),
        }
    return {}


def _goldset_drift_triage(
    *,
    structure_type: str,
    manual: int,
    pipeline: int,
    matched: int,
    count_source: str,
) -> dict[str, Any]:
    delta = pipeline - manual
    ratio = round(pipeline / manual, 4) if manual > 0 else None
    if delta == 0:
        category = "no_count_drift"
        reason = "Manual and pipeline counts are equal."
    elif (
        structure_type == "paragraph_item"
        and delta > 0
        and count_source in {"document_inventory", "structural_child_metadata", "visible_article_body_deduped"}
        and (manual <= 1 or delta >= max(20, int(manual * 0.35)))
    ):
        category = "scope_mismatch_candidate"
        reason = (
            "Paragraph/item count comes from inventory or article child metadata and is much larger than "
            "the manual label; inspect label scope before changing parser rules."
        )
    elif (
        structure_type == "table"
        and delta > 0
        and count_source in {"kordoc_promoted_hwp", "document_inventory"}
    ):
        category = "table_source_breakdown_needed"
        reason = "Table count is driven by Kordoc promotion or document inventory; compare Kordoc units against manual table scope."
    elif structure_type == "nested_table" and delta > 0 and count_source.endswith("_inventory"):
        category = "nested_inventory_review_candidate"
        reason = "Nested-table count comes from inventory metadata; verify source nested-table criteria before lowering parser recall."
    elif delta > 0:
        category = "over_detection_candidate"
        reason = "Pipeline count is greater than manual count."
    else:
        category = "under_detection_candidate"
        reason = "Pipeline count is lower than manual count."
    return {
        "category": category,
        "reason": reason,
        "pipeline_to_manual_ratio": ratio,
        "count_delta": delta,
        "matched_count": matched,
    }


def _manual_goldset_counts_are_zero(label_row: dict[str, Any]) -> bool:
    for spec in GOLDSET_SCORE_SPECS.values():
        if optional_int(label_row.get(spec["manual_field"])) not in (0, None):
            return False
    return True


def _goldset_scope(label_row: dict[str, Any]) -> dict[str, Any]:
    note_text = " ".join(str(label_row.get(field) or "") for field in GOLDSET_SCOPE_NOTE_FIELDS).strip()
    normalized = note_text.lower()
    non_regulation_note = any(pattern.lower() in normalized for pattern in NON_REGULATION_GOLDSET_SCOPE_PATTERNS)
    manual_zero = _manual_goldset_counts_are_zero(label_row)
    if non_regulation_note and manual_zero:
        return {
            "score_scope": "manual_non_article_form",
            "excluded_from_quality_claim": True,
            "exclusion_reason": "Manual goldset notes mark this source as not an article-form regulation.",
            "scope_note": note_text,
        }
    if non_regulation_note:
        return {
            "score_scope": "manual_scope_conflict",
            "excluded_from_quality_claim": False,
            "exclusion_reason": "",
            "scope_note": note_text,
        }
    return {
        "score_scope": "quality_claim",
        "excluded_from_quality_claim": False,
        "exclusion_reason": "",
        "scope_note": note_text,
    }


def _metric_score(
    *,
    structure_type: str,
    document_id: str,
    label_row: dict[str, Any],
    pipeline_summary: dict[str, Any] | None,
    batch_evidence_missing: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = GOLDSET_SCORE_SPECS[structure_type]
    issues: list[dict[str, Any]] = []
    manual = optional_int(label_row.get(spec["manual_field"]))
    label_pipeline = optional_int(label_row.get(spec["pipeline_field"]))
    pipeline = None if batch_evidence_missing else label_pipeline
    pipeline_count_origin = "batch_report_missing" if batch_evidence_missing else "label_file"
    if not batch_evidence_missing:
        if pipeline is None and pipeline_summary is not None:
            pipeline = safe_int(pipeline_summary.get(spec["pipeline_summary_field"]))
            pipeline_count_origin = "batch_report"
        elif pipeline_summary is not None and spec["pipeline_summary_field"] in pipeline_summary:
            pipeline = safe_int(pipeline_summary.get(spec["pipeline_summary_field"]))
            pipeline_count_origin = "batch_report"
    label_matched = optional_int(label_row.get(spec["match_field"]))
    if batch_evidence_missing:
        matched = None
        matched_count_source = "unavailable_batch_evidence"
    else:
        matched, matched_count_source = effective_goldset_matched_count(
            manual_count=manual,
            pipeline_count=pipeline,
            matched_count=label_matched,
        )
    count_source = _pipeline_count_source_for_structure(structure_type, pipeline_summary)
    if batch_evidence_missing:
        count_source = "batch_report_missing"

    if manual is None:
        issues.append(
            {
                "document_id": document_id,
                "structure_type": structure_type,
                "code": "manual-count-missing",
                "message": f"{spec['manual_field']} is required for scoring.",
            }
        )
    if pipeline is None:
        issues.append(
            {
                "document_id": document_id,
                "structure_type": structure_type,
                "code": "pipeline-count-missing",
                "message": f"{spec['pipeline_field']} is required when no batch report pipeline count is available.",
            }
        )
    if matched is None:
        issues.append(
            {
                "document_id": document_id,
                "structure_type": structure_type,
                "code": "matched-count-missing",
                "message": f"{spec['match_field']} is required to compute precision/recall.",
            }
        )

    score = {
        "structure_type": structure_type,
        "manual_count": manual,
        "pipeline_count": pipeline,
        "label_pipeline_count": label_pipeline,
        "pipeline_count_origin": pipeline_count_origin,
        "matched_count": matched,
        "label_matched_count": label_matched,
        "matched_count_source": matched_count_source,
        "false_positive_count": None,
        "false_negative_count": None,
        "count_delta": None,
        "precision": None,
        "recall": None,
        "f1": None,
        "scorable": False,
        "pipeline_count_source": count_source,
        "pipeline_count_breakdown": _pipeline_count_breakdown_for_structure(structure_type, pipeline_summary),
        "pipeline_to_manual_ratio": None,
        "drift_triage": "not_scorable",
        "drift_triage_reason": "Manual, pipeline, or matched count is missing.",
    }
    if manual is None or pipeline is None or matched is None:
        return score, issues

    invalid_for_scoring = False
    for count_name, count_value in (
        ("manual_count", manual),
        ("pipeline_count", pipeline),
        ("matched_count", matched),
    ):
        if count_value < 0:
            invalid_for_scoring = True
            issues.append(
                {
                    "document_id": document_id,
                    "structure_type": structure_type,
                    "code": "count-negative",
                    "message": "Manual, pipeline, and matched counts must be zero or greater.",
                    "field": count_name,
                    "value": count_value,
                }
            )

    if matched > manual or matched > pipeline:
        invalid_for_scoring = True
        code = "matched-count-exceeds-bound"
        message = "Matched count cannot exceed manual or pipeline counts."
        if pipeline_count_origin == "batch_report" and label_pipeline is not None and matched <= label_pipeline:
            code = "matched-count-stale-after-reprocess"
            message = (
                "Matched count fits the label-file pipeline count but exceeds the latest batch-report "
                "pipeline count; rerun human matching before claiming precision/recall."
            )
        issues.append(
            {
                "document_id": document_id,
                "structure_type": structure_type,
                "code": code,
                "message": message,
                "manual_count": manual,
                "pipeline_count": pipeline,
                "label_pipeline_count": label_pipeline,
                "pipeline_count_origin": pipeline_count_origin,
                "matched_count": matched,
            }
        )
    elif pipeline_count_origin == "batch_report" and label_pipeline is not None and label_pipeline != pipeline:
        invalid_for_scoring = True
        issues.append(
            {
                "document_id": document_id,
                "structure_type": structure_type,
                "code": "pipeline-count-stale-after-reprocess",
                "message": (
                    "Label-file pipeline count differs from the latest batch-report pipeline count; "
                    "rerun human matching before claiming precision/recall."
                ),
                "manual_count": manual,
                "pipeline_count": pipeline,
                "label_pipeline_count": label_pipeline,
                "pipeline_count_origin": pipeline_count_origin,
                "matched_count": matched,
            }
        )
    if invalid_for_scoring:
        return score, issues

    false_positive = max(pipeline - matched, 0)
    false_negative = max(manual - matched, 0)
    precision = ratio_percent(matched, pipeline)
    recall = ratio_percent(matched, manual)
    drift_triage = _goldset_drift_triage(
        structure_type=structure_type,
        manual=manual,
        pipeline=pipeline,
        matched=matched,
        count_source=count_source,
    )
    score.update(
        {
            "false_positive_count": false_positive,
            "false_negative_count": false_negative,
            "count_delta": pipeline - manual,
            "precision": precision,
            "recall": recall,
            "f1": f1_percent(precision, recall),
            "scorable": True,
            "pipeline_to_manual_ratio": drift_triage["pipeline_to_manual_ratio"],
            "drift_triage": drift_triage["category"],
            "drift_triage_reason": drift_triage["reason"],
        }
    )
    return score, issues


def _aggregate_structure_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    scorable = [score for score in scores if score.get("scorable")]
    manual_total = sum(safe_int(score.get("manual_count")) for score in scorable)
    pipeline_total = sum(safe_int(score.get("pipeline_count")) for score in scorable)
    matched_total = sum(safe_int(score.get("matched_count")) for score in scorable)
    precision = ratio_percent(matched_total, pipeline_total)
    recall = ratio_percent(matched_total, manual_total)
    f1 = f1_percent(precision, recall)
    macro_f1_values = [float(score["f1"]) for score in scorable if score.get("f1") is not None]
    return {
        "manual_total": manual_total,
        "pipeline_total": pipeline_total,
        "matched_total": matched_total,
        "false_positive_total": sum(safe_int(score.get("false_positive_count")) for score in scorable),
        "false_negative_total": sum(safe_int(score.get("false_negative_count")) for score in scorable),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "macro_f1": round(sum(macro_f1_values) / len(macro_f1_values), 2) if macro_f1_values else None,
        "scorable_count": len(scorable),
        "missing_match_count": sum(1 for score in scores if not score.get("scorable")),
    }


def _label_review_issues(row_number: int, document_id: str, label_row: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    label_status = str(label_row.get("label_status") or "").strip().lower()
    reviewer = str(label_row.get("reviewer") or "").strip()
    reviewed_at = str(label_row.get("reviewed_at") or "").strip()
    if label_status not in GOLDSET_COMPLETE_LABEL_STATUSES:
        issues.append(
            {
                "row_number": row_number,
                "document_id": document_id,
                "code": "label-status-not-complete",
                "message": "label_status must show completed human review before this row can support a parsing accuracy claim.",
                "label_status": label_status,
                "accepted_statuses": sorted(GOLDSET_COMPLETE_LABEL_STATUSES),
            }
        )
        return issues
    if not reviewer:
        issues.append(
            {
                "row_number": row_number,
                "document_id": document_id,
                "code": "reviewer-missing",
                "message": "reviewer is required when label_status is complete.",
            }
        )
    if not reviewed_at:
        issues.append(
            {
                "row_number": row_number,
                "document_id": document_id,
                "code": "reviewed-at-missing",
                "message": "reviewed_at is required when label_status is complete.",
            }
        )
    return issues


def _goldset_completion_summary(
    label_rows: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    scorable_structure_count: int,
    scored_document_count: int,
    excluded_document_count: int,
) -> dict[str, Any]:
    status_counts = Counter(str(row.get("label_status") or "").strip().lower() for row in label_rows)
    completed_document_count = sum(
        1
        for row in label_rows
        if str(row.get("label_status") or "").strip().lower() in GOLDSET_COMPLETE_LABEL_STATUSES
    )
    expected_structure_score_count = scored_document_count * len(GOLDSET_SCORE_SPECS)
    issue_code_counts = Counter(str(issue.get("code") or "unknown") for issue in issues)
    ready_for_quality_claim = (
        bool(documents)
        and scored_document_count > 0
        and completed_document_count == len(documents)
        and scorable_structure_count == expected_structure_score_count
        and not issues
    )
    return {
        "ready_for_quality_claim": ready_for_quality_claim,
        "accepted_label_statuses": sorted(GOLDSET_COMPLETE_LABEL_STATUSES),
        "label_status_counts": dict(sorted(status_counts.items())),
        "completed_document_count": completed_document_count,
        "pending_document_count": max(len(documents) - completed_document_count, 0),
        "scored_document_count": scored_document_count,
        "excluded_document_count": excluded_document_count,
        "expected_structure_score_count": expected_structure_score_count,
        "completed_structure_score_count": scorable_structure_count,
        "missing_structure_score_count": max(expected_structure_score_count - scorable_structure_count, 0),
        "blocking_issue_codes": dict(sorted(issue_code_counts.items())),
    }


def build_goldset_score_payload(
    workspace: Path,
    label_rows: list[dict[str, Any]],
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_generated_at()
    pipeline_index = build_pipeline_count_index(workspace, batch_report_paths, reports_dir) if batch_report_paths else {}
    documents: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    by_structure: dict[str, list[dict[str, Any]]] = {key: [] for key in GOLDSET_SCORE_SPECS}

    for row_number, label_row in enumerate(label_rows, start=1):
        document_id = str(label_row.get("document_id") or "").strip()
        if not document_id:
            issues.append(
                {
                    "row_number": row_number,
                    "code": "document-id-missing",
                    "message": "document_id is required.",
                }
            )
            continue
        review_issues = _label_review_issues(row_number, document_id, label_row)
        issues.extend(review_issues)
        label_review_complete = not any(
            issue.get("code") in {"label-status-not-complete", "reviewer-missing", "reviewed-at-missing"}
            for issue in review_issues
        )
        pipeline_info = resolve_pipeline_count_info(workspace, pipeline_index, label_row) if pipeline_index else {}
        if batch_report_paths and not pipeline_info:
            issues.append(
                {
                    "row_number": row_number,
                    "document_id": document_id,
                    "code": "batch-report-document-not-found",
                    "message": (
                        "The supplied batch report has no completed row matching this goldset document. "
                        "Do not fall back to label-file pipeline counts for a release-grade score."
                    ),
                    "batch_reports": [report_path(workspace, path) for path in batch_report_paths],
                }
            )
        pipeline_summary = pipeline_info.get("pipeline_counts") if isinstance(pipeline_info, dict) else None
        batch_evidence_missing = bool(batch_report_paths and not pipeline_info)
        scope = _goldset_scope(label_row)
        if scope["score_scope"] == "manual_scope_conflict":
            issues.append(
                {
                    "row_number": row_number,
                    "document_id": document_id,
                    "code": "manual-scope-conflict",
                    "message": "Goldset notes mark the row as non-article/non-regulation, but manual structure counts are not all zero.",
                    "scope_note": scope.get("scope_note", ""),
                }
            )
        structure_scores: dict[str, dict[str, Any]] = {}
        for structure_type in GOLDSET_SCORE_SPECS:
            score, metric_issues = _metric_score(
                structure_type=structure_type,
                document_id=document_id,
                label_row=label_row,
                pipeline_summary=pipeline_summary,
                batch_evidence_missing=batch_evidence_missing,
            )
            score["label_review_complete"] = label_review_complete
            if not label_review_complete:
                score["scorable"] = False
                score["drift_triage"] = "label_review_incomplete"
                score["drift_triage_reason"] = (
                    "Label row is not complete enough to support a parsing accuracy quality claim."
                )
            score["excluded_from_quality_claim"] = bool(scope["excluded_from_quality_claim"])
            structure_scores[structure_type] = score
            if not scope["excluded_from_quality_claim"]:
                by_structure[structure_type].append(score)
                issues.extend(metric_issues)
        documents.append(
            {
                "document_id": document_id,
                "filename": label_row.get("filename") or pipeline_info.get("filename", ""),
                "institution_name": label_row.get("institution_name") or pipeline_info.get("institution_name", ""),
                "extension": label_row.get("extension") or pipeline_info.get("extension", ""),
                "chunk_artifact": pipeline_info.get("chunk_artifact") or label_row.get("chunk_artifact", ""),
                "pipeline_document_id": pipeline_info.get("document_id", ""),
                "pipeline_match_key": pipeline_info.get("match_key", ""),
                "source_batch_report": pipeline_info.get("source_batch_report", ""),
                "label_status": str(label_row.get("label_status") or "").strip(),
                "reviewer": str(label_row.get("reviewer") or "").strip(),
                "reviewed_at": str(label_row.get("reviewed_at") or "").strip(),
                "score_scope": scope["score_scope"],
                "excluded_from_quality_claim": bool(scope["excluded_from_quality_claim"]),
                "exclusion_reason": scope["exclusion_reason"],
                "scope_note": scope["scope_note"],
                "scores": structure_scores,
                "notes": label_row.get("notes", ""),
            }
        )

    structure_summary = {
        structure_type: _aggregate_structure_scores(scores)
        for structure_type, scores in by_structure.items()
    }
    all_scorable_scores = [
        score
        for scores in by_structure.values()
        for score in scores
        if score.get("scorable")
    ]
    overall = _aggregate_structure_scores(all_scorable_scores)
    excluded_document_count = sum(1 for document in documents if document.get("excluded_from_quality_claim"))
    scored_document_count = len(documents) - excluded_document_count
    completion = _goldset_completion_summary(
        label_rows,
        documents,
        issues,
        len(all_scorable_scores),
        scored_document_count,
        excluded_document_count,
    )
    return {
        "report_type": "parsing_goldset_score",
        "generated_at": generated_at,
        "measurement_kind": "manual_goldset_precision_recall",
        "important_limitations": [
            "Precision and recall require matched_* counts from human review; manual and pipeline totals alone are not enough.",
            "Pipeline counts can be derived from the provided batch report or supplied directly in the label file.",
            "Rows with incomplete label_status, reviewer, or reviewed_at metadata cannot support a parsing accuracy quality claim.",
            "Rows explicitly marked by human review as non-article/non-regulation sources are excluded from quality-claim scoring and reported separately.",
            "This report scores structure preservation only; answer factuality is measured by separate RAG/MCP evaluation reports.",
        ],
        "summary": {
            "document_count": len(documents),
            "scored_document_count": scored_document_count,
            "excluded_document_count": excluded_document_count,
            "structure_type_count": len(GOLDSET_SCORE_SPECS),
            "scorable_structure_count": len(all_scorable_scores),
            "issue_count": len(issues),
            "ready_for_quality_claim": completion["ready_for_quality_claim"],
        },
        "completion": completion,
        "overall": overall,
        "by_structure": structure_summary,
        "documents": documents,
        "issues": issues,
    }


def make_goldset_score_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Parsing Goldset Score",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Measurement kind: `{payload['measurement_kind']}`",
        f"- Documents: {payload['summary']['document_count']:,}",
        f"- Scored documents: {payload['summary'].get('scored_document_count', payload['summary']['document_count']):,}",
        f"- Excluded non-article documents: {payload['summary'].get('excluded_document_count', 0):,}",
        f"- Scorable structure rows: {payload['summary']['scorable_structure_count']:,}",
        f"- Issues: {payload['summary']['issue_count']:,}",
        f"- Ready for quality claim: {payload['summary'].get('ready_for_quality_claim', False)}",
        "",
        "## Important Limitation",
        "",
        "Precision/recall is computed only when the label file contains `matched_*` counts. A manual total and a pipeline total alone can show count drift, but cannot prove true positives.",
        "",
        "## Completion Gate",
        "",
    ]
    completion = payload.get("completion") or {}
    lines.extend(
        [
            f"- Ready for quality claim: {completion.get('ready_for_quality_claim', False)}",
            f"- Completed document labels: {completion.get('completed_document_count', 0):,} / {payload['summary']['document_count']:,}",
            f"- Scored documents: {completion.get('scored_document_count', payload['summary'].get('scored_document_count', 0)):,}",
            f"- Excluded non-article documents: {completion.get('excluded_document_count', payload['summary'].get('excluded_document_count', 0)):,}",
            f"- Completed structure score rows: {completion.get('completed_structure_score_count', 0):,} / {completion.get('expected_structure_score_count', 0):,}",
            f"- Missing structure score rows: {completion.get('missing_structure_score_count', 0):,}",
            f"- Accepted label statuses: {', '.join(completion.get('accepted_label_statuses') or [])}",
        ]
    )
    if completion.get("blocking_issue_codes"):
        lines.extend(["", "| Issue code | Count |", "| --- | ---: |"])
        for code, count in completion["blocking_issue_codes"].items():
            lines.append(f"| {code} | {count:,} |")
    lines.extend(
        [
        "",
        "## Overall",
        "",
        "| Manual total | Pipeline total | Matched total | False positives | False negatives | Precision | Recall | F1 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    overall = payload["overall"]
    lines.append(
        f"| {overall['manual_total']:,} | {overall['pipeline_total']:,} | {overall['matched_total']:,} | "
        f"{overall['false_positive_total']:,} | {overall['false_negative_total']:,} | "
        f"{markdown_cell(overall['precision'])} | {markdown_cell(overall['recall'])} | {markdown_cell(overall['f1'])} |"
    )
    lines.extend(
        [
            "",
            "## By Structure",
            "",
            "| Structure | Manual | Pipeline | Matched | Precision | Recall | F1 | Macro F1 | Missing match rows |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for structure_type, summary in payload["by_structure"].items():
        lines.append(
            f"| {structure_type} | {summary['manual_total']:,} | {summary['pipeline_total']:,} | "
            f"{summary['matched_total']:,} | {markdown_cell(summary['precision'])} | "
            f"{markdown_cell(summary['recall'])} | {markdown_cell(summary['f1'])} | "
            f"{markdown_cell(summary['macro_f1'])} | {summary['missing_match_count']:,} |"
        )

    lines.extend(["", "## Documents", ""])
    for document in payload.get("documents", []):
        lines.extend(
            [
                f"### {document['document_id']}",
                "",
                f"- File: {document.get('filename') or ''}",
                f"- Institution: {document.get('institution_name') or ''}",
                f"- Chunk artifact: {document.get('chunk_artifact') or ''}",
                f"- Pipeline document ID: {document.get('pipeline_document_id') or ''}",
                f"- Pipeline match key: {document.get('pipeline_match_key') or ''}",
                f"- Source batch report: {document.get('source_batch_report') or ''}",
                f"- Score scope: {document.get('score_scope') or 'quality_claim'}",
                f"- Excluded from quality claim: {document.get('excluded_from_quality_claim', False)}",
                f"- Exclusion reason: {document.get('exclusion_reason') or ''}",
                "",
                "| Structure | Manual | Pipeline | Matched | Count source | Drift triage | Precision | Recall | F1 |",
                "| --- | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for structure_type, score in document["scores"].items():
            lines.append(
                f"| {structure_type} | {markdown_cell(score['manual_count'])} | {markdown_cell(score['pipeline_count'])} | "
                f"{markdown_cell(score['matched_count'])} | {markdown_cell(score.get('pipeline_count_source'))} | "
                f"{markdown_cell(score.get('drift_triage'))} | {markdown_cell(score['precision'])} | "
                f"{markdown_cell(score['recall'])} | {markdown_cell(score['f1'])} |"
            )
        lines.append("")

    if payload.get("issues"):
        lines.extend(["## Issues", ""])
        for issue in payload["issues"][:50]:
            lines.append(f"- {json.dumps(issue, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n"


def write_goldset_score_report(
    workspace: Path,
    labels_path: str | Path,
    batch_report_paths: list[str | Path],
    reports_dir: Path,
    out_json: str | Path | None = None,
    out_md: str | Path | None = None,
    refresh_labels_out_csv: str | Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Path]:
    timestamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    reports_base = reports_dir if reports_dir.is_absolute() else workspace / reports_dir
    reports_base.mkdir(parents=True, exist_ok=True)
    score_labels_path = labels_path
    if refresh_labels_out_csv is not None:
        score_labels_path = write_refreshed_goldset_labels(
            workspace,
            labels_path,
            batch_report_paths,
            reports_dir,
            refresh_labels_out_csv,
        )["labels_csv"]
    payload = build_goldset_score_payload(
        workspace,
        load_goldset_label_rows(score_labels_path),
        batch_report_paths,
        reports_dir,
        generated_at=utc_generated_at(),
    )
    json_path = resolve_workspace_path(workspace, out_json) if out_json else reports_base / f"parsing_goldset_score_{timestamp}.json"
    md_path = resolve_workspace_path(workspace, out_md) if out_md else reports_base / f"parsing_goldset_score_{timestamp}.md"
    if json_path is None or md_path is None:
        raise ValueError("Output paths could not be resolved.")
    write_json(json_path, payload)
    md_path.write_text(make_goldset_score_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_markdown(
    paths: CorpusPaths,
    chunks: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    stats: dict[str, Any],
) -> str:
    rule_counts = Counter(row["category"] for row in rules)
    signal_counts = Counter((row["category"], row["signal"]) for row in rules)
    issue_counts = Counter(row["issue_type"] for row in issues)
    chunk_type_counts = Counter(str(row.get("chunk_type")) for row in chunks)
    part_counts = Counter(
        " ".join(str(value) for value in [row.get("part_no"), row.get("part_title")] if value)
        for row in chunks
        if row.get("part_no") or row.get("part_title")
    )

    lines = [
        f"# Regulation Corpus Analysis: {paths.document_id}",
        "",
        "## Executive Summary",
        "",
        f"- Chunks analyzed: {len(chunks):,}",
        f"- Rule candidates found: {len(rules):,}",
        f"- Validation issues: {len(issues):,}",
        f"- Median chunk length: {stats['chunk_length']['median']}",
        f"- Max chunk length: {stats['chunk_length']['max']}",
        f"- Quality gate: {stats.get('quality_gate', {}).get('passed', 'n/a')} / score {stats.get('quality_gate', {}).get('score', 'n/a')}",
        "",
        "## Chunk Type Distribution",
        "",
    ]
    for chunk_type, count in chunk_type_counts.most_common():
        lines.append(f"- {chunk_type}: {count:,}")

    lines.extend(["", "## Top Regulation Sections", ""])
    for label, count in part_counts.most_common(20):
        lines.append(f"- {label}: {count:,} chunks")

    lines.extend(["", "## Rule Candidate Categories", ""])
    for category, count in rule_counts.most_common():
        lines.append(f"- {category}: {count:,}")

    lines.extend(["", "## Top Rule Signals", ""])
    for (category, signal), count in signal_counts.most_common(20):
        lines.append(f"- {category}/{signal}: {count:,}")

    lines.extend(["", "## Table Extraction", ""])
    for key, value in stats.get("table_metrics", {}).items():
        lines.append(f"- {key}: {value:,}" if isinstance(value, int) else f"- {key}: {value}")

    lines.extend(["", "## Metadata Coverage", ""])
    for key, value in stats.get("metadata_coverage", {}).items():
        lines.append(f"- {key}: {value:,}")

    if stats.get("quality_gate"):
        lines.extend(["", "## Quality Gate", ""])
        for key, value in stats["quality_gate"].items():
            if isinstance(value, (dict, list)):
                continue
            lines.append(f"- {key}: {value}")

    lines.extend(["", "## Validation Issues", ""])
    if issue_counts:
        for issue_type, count in issue_counts.most_common():
            lines.append(f"- {issue_type}: {count:,}")
    else:
        lines.append("- No validation issues")

    lines.extend(["", "## Issue Samples", ""])
    for row in issues[:20]:
        location = " > ".join(
            str(value)
            for value in [row.get("parent_number"), row.get("parent_title"), row.get("number"), row.get("title")]
            if value
        )
        lines.append(f"- page {row.get('page_start')}: {row.get('message')} ({location})")
        lines.append(f"  - {row.get('snippet')}")

    lines.extend(["", "## Rule Samples", ""])
    for row in rules[:30]:
        lines.append(
            f"- {row['category']}/{row['signal']} | {row.get('article_no') or ''} {row.get('article_title') or ''} | page {row.get('page_start')}"
        )
        lines.append(f"  - match: `{row['matched_text']}`")
        lines.append(f"  - {row['snippet']}")

    lines.extend(
        [
            "",
            "## Recommended Next Rules",
            "",
            "- Treat amendment cross-references like `제12조제1항`, `제6조에 따라`, and `제14조중` as references, not article starts.",
            "- Split sequence validation by document boundary, not just parent chapter, for integrated regulation books.",
            "- Detect regulation title boundaries before `제1조` resets inside the same chapter-like section.",
            "- Classify rule candidates into obligation, prohibition, permission, procedure, definition, delegation, exception, revision, and reference signals.",
            "- Keep raw table rows and structured table cell rows side by side so retrieval can use either original text or row-level metadata.",
        ]
    )
    return "\n".join(lines) + "\n"


def analyze(paths: CorpusPaths) -> dict[str, Path]:
    chunks = load_jsonl(paths.jsonl)
    raw_issues = load_result(paths, "issues")
    raw_nodes = load_result(paths, "nodes")
    raw_quality = load_result(paths, "quality")
    issues = summarize_issues(raw_issues, raw_nodes)
    rules = find_rule_candidates(chunks)
    table_metrics = summarize_table_metrics(chunks)
    metadata_coverage = summarize_metadata_coverage(chunks)
    quality_gate = quality_summary(raw_quality)

    lengths = [text_len(row) for row in chunks]
    stats = {
        "document_id": paths.document_id,
        "source_jsonl": str(paths.jsonl),
        "chunk_count": len(chunks),
        "issue_count": len(issues),
        "rule_candidate_count": len(rules),
        "chunk_length": {
            "min": min(lengths) if lengths else 0,
            "median": int(median(lengths)) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "over_2300": sum(1 for value in lengths if value > 2300),
        },
        "chunk_types": dict(Counter(str(row.get("chunk_type")) for row in chunks)),
        "table_like_chunks": table_metrics["table_like_chunks"],
        "table_cell_row_count": table_metrics["table_cell_row_count"],
        "table_metrics": table_metrics,
        "metadata_coverage": metadata_coverage,
        "quality_gate": quality_gate,
        "quality_score": quality_gate.get("score"),
        "quality_passed": quality_gate.get("passed"),
        "rule_categories": dict(Counter(row["category"] for row in rules)),
        "issue_types": dict(Counter(row["issue_type"] for row in issues)),
        "pages": {
            "min": min((row.get("source_page_start") or 0 for row in chunks), default=0),
            "max": max((row.get("source_page_end") or 0 for row in chunks), default=0),
        },
    }

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rules:
        if len(by_category[row["category"]]) < 30:
            by_category[row["category"]].append(row)
    stats["rule_samples_by_category"] = by_category

    paths.reports.mkdir(parents=True, exist_ok=True)
    prefix = paths.reports / f"regulation_analysis_{paths.document_id}"
    stats_path = prefix.with_suffix(".json")
    rules_path = paths.reports / f"regulation_rule_candidates_{paths.document_id}.csv"
    issues_path = paths.reports / f"regulation_issue_samples_{paths.document_id}.csv"
    markdown_path = prefix.with_suffix(".md")

    write_json(stats_path, stats)
    write_csv(rules_path, rules)
    write_csv(issues_path, issues)
    markdown_path.write_text(make_markdown(paths, chunks, rules, issues, stats), encoding="utf-8")

    return {
        "stats": stats_path,
        "rules": rules_path,
        "issues": issues_path,
        "markdown": markdown_path,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze RegRAG Prep corpus outputs.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--parsing-automation-report", action="store_true", help="Write heuristic parsing automation and review-need reports.")
    mode.add_argument("--parsing-goldset-template", action="store_true", help="Write a manual goldset worksheet for parsing accuracy review.")
    mode.add_argument("--parsing-goldset-score", action="store_true", help="Score a completed manual goldset label CSV/JSON.")
    mode.add_argument("--parsing-goldset-review-packets", action="store_true", help="Write per-document review packets from a goldset label CSV/JSON.")
    mode.add_argument(
        "--parsing-goldset-refresh-labels",
        action="store_true",
        help="Copy manual goldset labels while refreshing latest batch pipeline counts and clearing stale matched counts.",
    )
    parser.add_argument("--document-id", default=None)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--jsonl", default=None)
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--batch-report", action="append", default=[], help="Path to batch_quality_*.json. Repeat to merge reports.")
    parser.add_argument("--goldset-labels", default=None, help="Completed manual goldset label CSV/JSON for --parsing-goldset-score.")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--out-review-csv", default=None)
    parser.add_argument("--out-goldset-labels-csv", default=None)
    parser.add_argument("--out-goldset-review-packets-dir", default=None)
    parser.add_argument(
        "--refresh-goldset-labels-before-score",
        action="store_true",
        help="Refresh goldset labels to --out-goldset-labels-csv before scoring, then score the refreshed labels.",
    )
    parser.add_argument(
        "--fail-on-goldset-issue",
        action="store_true",
        help="Exit nonzero after writing --parsing-goldset-score reports unless the goldset is complete and quality-claim ready.",
    )
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--goldset-size", type=int, default=12)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    reports_dir = Path(args.reports_dir)

    if args.parsing_automation_report:
        outputs = write_parsing_automation_report(
            workspace,
            args.batch_report,
            reports_dir,
            out_json=args.out_json,
            out_md=args.out_md,
            out_review_csv=args.out_review_csv,
            timestamp=args.timestamp,
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))
        return

    if args.parsing_goldset_template:
        outputs = write_goldset_report(
            workspace,
            args.batch_report,
            reports_dir,
            out_md=args.out_md,
            out_labels_csv=args.out_goldset_labels_csv,
            timestamp=args.timestamp,
            size=args.goldset_size,
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))
        return

    if args.parsing_goldset_score:
        if not args.goldset_labels:
            raise SystemExit("--goldset-labels is required with --parsing-goldset-score.")
        if args.refresh_goldset_labels_before_score and not args.out_goldset_labels_csv:
            raise SystemExit("--out-goldset-labels-csv is required with --refresh-goldset-labels-before-score.")
        outputs = write_goldset_score_report(
            workspace,
            args.goldset_labels,
            args.batch_report,
            reports_dir,
            out_json=args.out_json,
            out_md=args.out_md,
            refresh_labels_out_csv=args.out_goldset_labels_csv if args.refresh_goldset_labels_before_score else None,
            timestamp=args.timestamp,
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))
        if args.fail_on_goldset_issue:
            payload = load_json_file(outputs["json"])
            if not payload.get("summary", {}).get("ready_for_quality_claim"):
                raise SystemExit(f"Parsing goldset is not quality-claim ready; see {outputs['json']}")
        return

    if args.parsing_goldset_review_packets:
        if not args.goldset_labels:
            raise SystemExit("--goldset-labels is required with --parsing-goldset-review-packets.")
        outputs = write_goldset_review_packets(
            workspace,
            args.goldset_labels,
            args.out_goldset_review_packets_dir or "reports/parsing_goldset_review_packets",
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))
        return

    if args.parsing_goldset_refresh_labels:
        if not args.goldset_labels:
            raise SystemExit("--goldset-labels is required with --parsing-goldset-refresh-labels.")
        if not args.out_goldset_labels_csv:
            raise SystemExit("--out-goldset-labels-csv is required with --parsing-goldset-refresh-labels.")
        outputs = write_refreshed_goldset_labels(
            workspace,
            args.goldset_labels,
            args.batch_report,
            reports_dir,
            args.out_goldset_labels_csv,
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))
        return

    if not args.document_id:
        raise SystemExit("--document-id is required unless a report mode is selected.")

    document_id = args.document_id
    paths = CorpusPaths(
        workspace=workspace,
        document_id=document_id,
        jsonl=Path(args.jsonl).resolve() if args.jsonl else workspace / "data" / "exports" / f"{document_id}.jsonl",
        repository=workspace / "data" / "repository.json",
        repository_dir=workspace / "data" / "repository",
        reports=reports_dir if reports_dir.is_absolute() else workspace / reports_dir,
    )
    outputs = analyze(paths)
    print(json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
