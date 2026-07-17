from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.export_law_reference_report import load_chunks_jsonl
from scripts.export_vectordb_ingestion import _iter_batch_chunks, load_json


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


RELATION_COLUMNS = [
    "edge_id",
    "relation_type",
    "source_id",
    "source_label",
    "target_id",
    "target_label",
    "document_id",
    "chunk_id",
    "source_page_start",
    "source_page_end",
    "weight",
    "confidence",
    "evidence_type",
    "evidence_text",
    "metadata_json",
]

STOP_TERMS = {
    "본문",
    "위치",
    "문서명",
    "규정",
    "지침",
    "기준",
    "요령",
    "규칙",
    "내규",
    "정관",
    "세칙",
    "요강",
    "시행령",
    "시행세칙",
    "운영지침",
    "관리규정",
    "법",
    "방법",
    "내용",
    "사항",
    "제정",
    "개정",
    "신설",
    "삭제",
    "시행",
    "경우",
    "해당",
    "관련",
    "따른",
    "따라",
    "위하여",
    "하여야",
    "한다",
    "있다",
    "없다",
    "대한",
    "관한",
    "각호",
    "다음",
    "그밖의",
}
TERM_SUFFIXES = (
    "규정",
    "세칙",
    "지침",
    "기준",
    "요령",
    "편람",
    "내규",
    "정관",
    "법",
    "시행령",
    "시행규칙",
    "계약",
    "입찰",
    "보증",
    "채용",
    "인사",
    "복무",
    "직제",
    "위임전결",
    "품질검사",
    "안전보건",
    "자산관리",
    "내부통제",
    "교육훈련",
    "수의계약",
)
ARTICLE_REF_PATTERN = r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?"
ARTICLE_HEADING_REF_PATTERN = r"제\s*\d+\s*조(?:\s*의\s*\d+)?"
NAMED_ARTICLE_REF = re.compile(rf"[「｢『“\"](?P<name>[^」｣』”\"]{{2,80}})[」｣』”\"]\s*(?P<article>{ARTICLE_REF_PATTERN})")
QUOTED_REFERENCE_NAME = re.compile(r"[「｢『“\"](?P<name>[^」｣』”\"]{2,80})[」｣』”\"]")
DIRECT_LAW_ARTICLE_REF = re.compile(
    rf"(?P<name>[가-힣A-Za-z0-9·ㆍ\s]{{2,80}}?(?:법률|법|시행령|시행규칙))\s*(?P<article>{ARTICLE_REF_PATTERN})"
)
DIRECT_LAW_NAME_TAIL = re.compile(
    r"([가-힣A-Za-z0-9·ㆍ]+(?:법률|법)(?:\s*(?:시행령|시행규칙))?|[가-힣A-Za-z0-9·ㆍ]+시행령)$"
)
LAW_NAME_PREFIX_MARKERS = ("경우등", "등", "및", "또는", "따른", "따라", "의한", "정하는", "사항은")
GENERIC_LAW_NAMES = {"법", "법률", "법시행령", "법시행규칙", "시행령", "시행규칙"}
SAME_LAW_ALIASES = {"동법", "동시행령", "동시행규칙", "동법시행령", "동법시행규칙"}
INLINE_ARTICLE_HEADING = re.compile(
    rf"(?P<article>{ARTICLE_HEADING_REF_PATTERN})"
    r"(?:\s*[\(（][^\)）\n]{1,80}[\)）]|\s*<\s*삭제[^>\n]{0,80}>|\s*삭제\s*<[^>\n]{0,80}>)"
)
INTERNAL_REFERENCE_SUFFIXES = ("규정", "지침", "내규", "정관", "세칙", "요령", "기준", "편람", "규칙", "예규")
INTERNAL_REFERENCE_TOKEN = re.compile(
    rf"[가-힣A-Za-z0-9·ㆍ\s]{{2,100}}(?:{'|'.join(map(re.escape, INTERNAL_REFERENCE_SUFFIXES))})"
)
MAX_CONTEXT_PAGE_GAP = 3


def export_relation_graph(
    *,
    chunks_jsonl: Path | None = None,
    batch_report_path: Path | list[Path] | None = None,
    out_jsonl: Path,
    out_manifest: Path,
    out_csv: Path | None = None,
    out_md: Path | None = None,
    max_terms_per_chunk: int = 12,
) -> dict[str, Any]:
    if chunks_jsonl is None and batch_report_path is None:
        raise ValueError("Provide either chunks_jsonl or batch_report_path.")
    if chunks_jsonl is not None and batch_report_path is not None:
        raise ValueError("Provide only one of chunks_jsonl or batch_report_path.")

    source_label: str
    if batch_report_path is not None:
        batch_report_paths = normalize_batch_report_paths(batch_report_path)
        chunks = []
        generated_at_values = []
        input_count = 0
        successful_count = 0
        for path in batch_report_paths:
            batch_report = load_json(path)
            chunks.extend(_iter_batch_chunks(batch_report, batch_report_path=path))
            generated_at_values.append(batch_report.get("generated_at"))
            input_count += int(batch_report.get("input_count") or 0)
            successful_count += int(batch_report.get("successful_count") or 0)
        source_label = ", ".join(path.name for path in batch_report_paths)
        source_batch_generated_at = generated_at_values[0] if len(generated_at_values) == 1 else generated_at_values
    else:
        chunks = load_chunks_jsonl(chunks_jsonl)
        source_label = str(chunks_jsonl)
        source_batch_generated_at = None
        input_count = None
        successful_count = None

    edges = build_relation_edges(chunks, max_terms_per_chunk=max_terms_per_chunk)
    manifest = relation_manifest(
        edges,
        chunks=chunks,
        source_label=source_label,
        source_batch_generated_at=source_batch_generated_at,
        input_count=input_count,
        successful_count=successful_count,
        max_terms_per_chunk=max_terms_per_chunk,
    )
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    out_manifest.parent.mkdir(parents=True, exist_ok=True)
    out_jsonl.write_text(
        "\n".join(json.dumps(edge, ensure_ascii=False, sort_keys=True) for edge in edges) + ("\n" if edges else ""),
        encoding="utf-8",
    )
    out_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    manifest["out_jsonl"] = str(out_jsonl)
    manifest["out_manifest"] = str(out_manifest)
    if out_csv:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        out_csv.write_text(edges_to_csv(edges), encoding="utf-8")
        manifest["out_csv"] = str(out_csv)
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(manifest_to_markdown(manifest), encoding="utf-8")
        manifest["out_md"] = str(out_md)
    return manifest


def normalize_batch_report_paths(batch_report_path: Path | list[Path]) -> list[Path]:
    if isinstance(batch_report_path, list):
        paths = batch_report_path
    else:
        paths = [batch_report_path]
    result = [Path(path) for path in paths if path]
    if not result:
        raise ValueError("Provide at least one batch report path.")
    return result


def build_relation_edges(chunks: list[dict[str, Any]], *, max_terms_per_chunk: int = 12) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    term_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    article_index = build_article_index(chunks)
    contextual_article_contexts = build_contextual_regulation_article_contexts(chunks)
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or "")
        edges.extend(
            reference_edges(
                chunk,
                article_index=article_index,
                contextual_article_context=contextual_article_contexts.get(chunk_id),
            )
        )
        edges.extend(table_edges(chunk))
        terms = extract_chunk_terms(chunk, max_terms=max_terms_per_chunk)
        add_term_cooccurrence_edges(term_pairs, terms, chunk)
    edges.extend(sorted(term_pairs.values(), key=lambda edge: (-edge["weight"], edge["source_label"], edge["target_label"])))
    return dedupe_edges(edges)


def reference_edges(
    chunk: dict[str, Any],
    *,
    article_index: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    contextual_article_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    metadata = chunk.get("metadata") or chunk
    source_id, source_label = source_node(chunk)
    edges: list[dict[str, Any]] = []
    named_edges, paired_article_refs = named_article_reference_edges(chunk, source_id, source_label)
    edges.extend(named_edges)
    inline_article_defs = inline_article_definition_refs(chunk)
    edges.extend(inline_article_definition_edges(chunk, source_id, source_label, inline_article_defs))
    contextual_law_article_contexts = contextual_law_article_refs(chunk)
    text = chunk_reference_text(chunk)
    external_names = {normalize_key(clean_law_reference_name(value)) for value in metadata.get("external_law_refs") or []}

    seen_article_refs: set[str] = set()
    for ref_edge in metadata.get("reference_edges") or []:
        if ref_edge.get("type") != "article":
            continue
        value = str(ref_edge.get("value") or "").strip()
        if not value:
            continue
        if normalize_article_ref(value) in paired_article_refs:
            continue
        if normalize_article_ref(value) in inline_article_defs:
            continue
        law_context = contextual_law_article_contexts.get(normalize_article_ref(value))
        if law_context:
            edges.append(
                contextual_law_article_edge(
                    chunk,
                    source_id,
                    source_label,
                    value,
                    law_context,
                    raw_reference_edge=ref_edge,
                )
            )
            seen_article_refs.add(value)
            continue
        seen_article_refs.add(value)
        resolved_ref_edge = ref_edge
        if not ref_edge.get("resolved") and article_index is not None:
            resolved_ref_edge = resolve_base_article_reference(chunk, value, article_index) or ref_edge
        if not resolved_ref_edge.get("resolved"):
            contextual_edge = contextual_regulation_article_edge(
                chunk,
                source_id,
                source_label,
                value,
                contextual_article_context,
                raw_reference_edge=ref_edge,
            )
            if contextual_edge:
                edges.append(contextual_edge)
                continue
        target_id, target_label = article_target_node(chunk, value, resolved_ref_edge)
        relation_type = article_relation_type(chunk, value, resolved_ref_edge)
        edges.append(
            make_edge(
                relation_type,
                source_id,
                source_label,
                target_id,
                target_label,
                chunk,
                evidence_type="article_ref",
                evidence_text=value,
                confidence=1.0 if resolved_ref_edge.get("resolved") else 0.65,
                metadata={
                    "resolved": bool(resolved_ref_edge.get("resolved")),
                    "base_article_resolved": bool(resolved_ref_edge.get("base_article_resolved")),
                    "article_ref": normalize_article_ref(value),
                    "base_article_ref": base_article_ref(value),
                    "resolution_status": article_resolution_status(chunk, value, resolved_ref_edge),
                    "source_chunk_type": metadata.get("chunk_type") or chunk.get("chunk_type"),
                    "raw_reference_edge": ref_edge,
                },
            )
        )

    for value in metadata.get("article_refs") or []:
        value = str(value or "").strip()
        if not value or value in seen_article_refs:
            continue
        if normalize_article_ref(value) in paired_article_refs:
            continue
        if normalize_article_ref(value) in inline_article_defs:
            continue
        law_context = contextual_law_article_contexts.get(normalize_article_ref(value))
        if law_context:
            edges.append(contextual_law_article_edge(chunk, source_id, source_label, value, law_context))
            continue
        resolved_ref_edge = resolve_base_article_reference(chunk, value, article_index or {}) if article_index else None
        if not resolved_ref_edge:
            contextual_edge = contextual_regulation_article_edge(
                chunk,
                source_id,
                source_label,
                value,
                contextual_article_context,
            )
            if contextual_edge:
                edges.append(contextual_edge)
                continue
        target_id, target_label = article_target_node(chunk, value, resolved_ref_edge)
        relation_type = article_relation_type(chunk, value, resolved_ref_edge)
        edges.append(
            make_edge(
                relation_type,
                source_id,
                source_label,
                target_id,
                target_label,
                chunk,
                evidence_type="article_ref",
                evidence_text=value,
                confidence=0.85 if resolved_ref_edge else 0.6,
                metadata={
                    "resolved": bool(resolved_ref_edge),
                    "base_article_resolved": bool((resolved_ref_edge or {}).get("base_article_resolved")),
                    "article_ref": normalize_article_ref(value),
                    "base_article_ref": base_article_ref(value),
                    "resolution_status": article_resolution_status(chunk, value, resolved_ref_edge),
                    "source_chunk_type": metadata.get("chunk_type") or chunk.get("chunk_type"),
                },
            )
        )

    for item in metadata.get("regulation_article_refs") or []:
        regulation_ref = clean_internal_reference_name(str((item or {}).get("regulation_ref") or ""))
        article_ref = normalize_article_ref(str((item or {}).get("article_ref") or "").strip())
        if not regulation_ref or not article_ref:
            continue
        alias_law_ref = resolve_same_law_alias_reference_name(regulation_ref, text, reference_position(text, regulation_ref))
        direct_law_ref = direct_law_reference_name(regulation_ref, external_names)
        if alias_law_ref or direct_law_ref or is_same_law_alias_name(regulation_ref):
            law_ref = alias_law_ref or direct_law_ref or clean_law_reference_name(regulation_ref)
            relation_type = "article_cites_law_article" if metadata.get("article_no") else "chunk_cites_law_article"
            edges.append(
                make_edge(
                    relation_type,
                    source_id,
                    source_label,
                    f"law_article:{normalize_key(law_ref)}:{normalize_key(article_ref)}",
                    f"{law_ref} {article_ref}",
                    chunk,
                    evidence_type="regulation_article_ref",
                    evidence_text=f"{regulation_ref} {article_ref}",
                    confidence=0.86,
                    metadata={
                        "reference_name": law_ref,
                        "article_ref": article_ref,
                        "scope": "external",
                        "alias_reference_name": regulation_ref,
                        "alias_resolved": bool(alias_law_ref),
                    },
                )
            )
            continue
        relation_type = "article_cites_regulation_article" if metadata.get("article_no") else "chunk_cites_regulation_article"
        edges.append(
            make_edge(
                relation_type,
                source_id,
                source_label,
                f"regulation_article:{normalize_key(regulation_ref)}:{normalize_key(article_ref)}",
                f"{regulation_ref} {article_ref}",
                chunk,
                evidence_type="regulation_article_ref",
                evidence_text=f"{regulation_ref} {article_ref}",
                confidence=0.9,
                metadata={"reference_name": regulation_ref, "article_ref": article_ref, "scope": "internal"},
            )
        )

    for value in metadata.get("internal_regulation_refs") or []:
        value = clean_internal_reference_name(str(value or ""))
        if not value:
            continue
        alias_law_ref = resolve_same_law_alias_reference_name(value, text, reference_position(text, value))
        direct_law_ref = direct_law_reference_name(value, external_names)
        if alias_law_ref or direct_law_ref or is_same_law_alias_name(value):
            law_ref = alias_law_ref or direct_law_ref or clean_law_reference_name(value)
            relation_type = "article_cites_law" if metadata.get("article_no") else "chunk_cites_law"
            edges.append(
                make_edge(
                    relation_type,
                    source_id,
                    source_label,
                    f"law:{normalize_key(law_ref)}",
                    law_ref,
                    chunk,
                    evidence_type="internal_regulation_ref",
                    evidence_text=value,
                    confidence=0.72,
                    metadata={"alias_reference_name": value, "alias_resolved": bool(alias_law_ref)},
                )
            )
            continue
        relation_type = "article_cites_regulation" if metadata.get("article_no") else "chunk_cites_regulation"
        edges.append(
            make_edge(
                relation_type,
                source_id,
                source_label,
                f"regulation:{normalize_key(value)}",
                value,
                chunk,
                evidence_type="internal_regulation_ref",
                evidence_text=value,
                confidence=0.8,
            )
        )

    for value in metadata.get("external_law_refs") or []:
        value = clean_law_reference_name(str(value or ""))
        if not value:
            continue
        value = resolve_same_law_alias_reference_name(value, text, reference_position(text, value)) or value
        if is_same_law_alias_name(value) or normalize_key(value) in GENERIC_LAW_NAMES:
            continue
        relation_type = "article_cites_law" if metadata.get("article_no") else "chunk_cites_law"
        edges.append(
            make_edge(
                relation_type,
                source_id,
                source_label,
                f"law:{normalize_key(value)}",
                value,
                chunk,
                evidence_type="external_law_ref",
                evidence_text=value,
                confidence=0.8,
            )
        )
    return edges


def contextual_law_article_refs(chunk: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata = chunk.get("metadata") or chunk
    chunk_type = str(metadata.get("chunk_type") or chunk.get("chunk_type") or "")
    external_law_refs = [clean_reference_name(str(value)) for value in metadata.get("external_law_refs") or []]
    external_names = {normalize_key(value) for value in external_law_refs if value}
    text = chunk_reference_text(chunk)
    result: dict[str, dict[str, Any]] = {}
    for match in DIRECT_LAW_ARTICLE_REF.finditer(text):
        name = direct_law_reference_name(match.group("name"), external_names)
        article_ref = normalize_article_ref(match.group("article"))
        if not name or not article_ref:
            continue
        result[article_ref] = {
            "law_ref": name,
            "context_article_ref": article_ref,
            "context_start": match.start(),
            "context_end": match.end(),
            "context_text": match.group(0),
            "context_source": "direct_law_article",
        }

    if metadata.get("article_no") or chunk_type not in {"form", "appendix"} or not external_names:
        return result

    law_contexts: list[dict[str, Any]] = []
    for match in NAMED_ARTICLE_REF.finditer(text):
        name = clean_reference_name(match.group("name"))
        if normalize_key(name) not in external_names:
            continue
        law_contexts.append(
            {
                "law_ref": name,
                "context_article_ref": normalize_article_ref(match.group("article")),
                "context_start": match.start(),
                "context_end": match.end(),
                "context_text": match.group(0),
            }
        )
    if not law_contexts:
        return result
    for article_match in re.finditer(ARTICLE_REF_PATTERN, text):
        article_ref = normalize_article_ref(article_match.group(0))
        previous_contexts = [context for context in law_contexts if int(context["context_end"]) <= article_match.start()]
        if not article_ref or not previous_contexts:
            continue
        context = previous_contexts[-1]
        if intervening_internal_named_reference(text[int(context["context_end"]) : article_match.start()]):
            continue
        result.setdefault(article_ref, context)
    return result


def direct_law_reference_name(raw_name: str, external_names: set[str]) -> str | None:
    candidates = direct_law_name_candidates(raw_name)
    for name in candidates:
        normalized_name = normalize_key(name)
        if external_names and (
            normalized_name in external_names
            or any(normalized_name.startswith(key) or key.startswith(normalized_name) for key in external_names)
        ):
            return name
    for name in candidates:
        if is_probable_external_law_name(name):
            return name
    return None


def chunk_reference_text(chunk: dict[str, Any]) -> str:
    return " ".join(str(value or "") for value in [chunk.get("text"), chunk.get("retrieval_text"), chunk.get("normalized_text")])


def reference_position(text: str, value: str) -> int:
    compact_value = normalize_key(value)
    if not compact_value:
        return len(text)
    direct_index = text.find(value)
    if direct_index >= 0:
        return direct_index
    compact_text = normalize_key(text)
    compact_index = compact_text.find(compact_value)
    if compact_index < 0:
        return len(text)
    seen_compact_chars = 0
    for index, char in enumerate(text):
        if normalize_key(char):
            if seen_compact_chars == compact_index:
                return index
            seen_compact_chars += 1
    return len(text)


def resolve_same_law_alias_reference_name(raw_name: str, text: str, start: int) -> str | None:
    if not is_same_law_alias_name(raw_name):
        return None
    previous_name = previous_law_reference_name(text, start)
    if not previous_name:
        return None
    alias = normalize_key(raw_name)
    root_name = law_root_name(previous_name)
    if alias == "동법":
        return root_name
    if alias in {"동시행령", "동법시행령"}:
        return f"{root_name} 시행령".strip()
    if alias in {"동시행규칙", "동법시행규칙"}:
        return f"{root_name} 시행규칙".strip()
    return None


def previous_law_reference_name(text: str, start: int) -> str | None:
    prefix = text[: max(0, start)]
    candidates: list[tuple[int, str]] = []
    for match in QUOTED_REFERENCE_NAME.finditer(prefix):
        name = clean_law_reference_name(match.group("name"))
        if name and not is_same_law_alias_name(name) and is_probable_external_law_name(name):
            candidates.append((match.start(), name))
    for match in DIRECT_LAW_ARTICLE_REF.finditer(prefix):
        name = direct_law_reference_name(match.group("name"), set())
        if name and not is_same_law_alias_name(name):
            candidates.append((match.start(), name))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def law_root_name(value: str) -> str:
    name = clean_reference_name(value)
    name = re.sub(r"\s*시행(?:령|규칙)$", "", name).strip()
    name = re.sub(r"시행(?:령|규칙)$", "", name).strip()
    return name or clean_reference_name(value)


def is_same_law_alias_name(value: str) -> bool:
    return normalize_key(value) in SAME_LAW_ALIASES


def direct_law_name_candidates(raw_name: str) -> list[str]:
    name = clean_law_reference_name(raw_name)
    if not name:
        return []
    compact_name = re.sub(r"\s+", "", name)
    bases: list[str] = []
    for base in (name, compact_name):
        for marker in LAW_NAME_PREFIX_MARKERS:
            if marker in base:
                bases.append(base.rsplit(marker, 1)[-1])
        bases.append(base)

    candidates: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for candidate in law_name_tail_candidates(base):
            normalized = normalize_key(candidate)
            if not normalized or normalized in seen or normalized in GENERIC_LAW_NAMES:
                continue
            seen.add(normalized)
            candidates.append(candidate)
    return candidates


def law_name_tail_candidates(value: str) -> list[str]:
    name = clean_reference_name(value)
    candidates = [name]
    tail_match = DIRECT_LAW_NAME_TAIL.search(name)
    if tail_match:
        candidates.insert(0, clean_reference_name(tail_match.group(1)))
    return candidates


def is_probable_external_law_name(value: str) -> bool:
    compact = normalize_key(clean_law_reference_name(value))
    if compact in GENERIC_LAW_NAMES:
        return False
    if "법" in compact or compact.endswith("시행령"):
        return True
    return bool(
        compact.endswith("계약사무규칙")
        and ("공기업" in compact or "준정부기관" in compact or "공공기관" in compact)
    )


def intervening_internal_named_reference(text: str) -> bool:
    for match in NAMED_ARTICLE_REF.finditer(text):
        name = clean_reference_name(match.group("name"))
        if name and is_internal_reference_name(name):
            return True
    return False


def contextual_law_article_edge(
    chunk: dict[str, Any],
    source_id: str,
    source_label: str,
    value: str,
    context: dict[str, Any],
    *,
    raw_reference_edge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = chunk.get("metadata") or chunk
    law_ref = clean_law_reference_name(str(context.get("law_ref") or ""))
    article_ref = normalize_article_ref(value)
    relation_type = "article_cites_law_article" if metadata.get("article_no") else "chunk_cites_law_article"
    return make_edge(
        relation_type,
        source_id,
        source_label,
        f"law_article:{normalize_key(law_ref)}:{normalize_key(article_ref)}",
        f"{law_ref} {article_ref}",
        chunk,
        evidence_type="contextual_law_article_ref",
        evidence_text=value,
        confidence=0.78,
        metadata={
            "reference_name": law_ref,
            "article_ref": article_ref,
            "base_article_ref": base_article_ref(article_ref),
            "scope": "external",
            "context_inferred": True,
            "context_article_ref": context.get("context_article_ref"),
            "context_text": context.get("context_text"),
            "source_chunk_type": metadata.get("chunk_type") or chunk.get("chunk_type"),
            "resolution_status": "context_law_inferred",
            "raw_reference_edge": raw_reference_edge,
        },
    )


def inline_article_definition_refs(chunk: dict[str, Any]) -> set[str]:
    metadata = chunk.get("metadata") or chunk
    if metadata.get("article_no"):
        return set()
    chunk_type = str(metadata.get("chunk_type") or chunk.get("chunk_type") or "")
    if chunk_type == "article":
        return set()
    text = " ".join(str(value or "") for value in [chunk.get("text"), chunk.get("retrieval_text"), chunk.get("normalized_text")])
    matches = list(INLINE_ARTICLE_HEADING.finditer(text))
    refs: set[str] = set()
    for match in matches:
        if not inline_article_heading_is_definition(text, match, match_count=len(matches)):
            continue
        article_ref = normalize_article_ref(match.group("article"))
        if article_ref:
            refs.add(article_ref)
    return refs


def inline_article_heading_is_definition(text: str, match: re.Match[str], *, match_count: int) -> bool:
    if match.start() <= 10:
        return True
    if match_count >= 2:
        return True
    prefix = text[max(0, match.start() - 8) : match.start()]
    if "\n" in prefix or "\r" in prefix:
        return True
    stripped = prefix.rstrip()
    return bool(stripped and stripped[-1] in ".。;；>〉")


def inline_article_definition_edges(
    chunk: dict[str, Any],
    source_id: str,
    source_label: str,
    inline_article_defs: set[str],
) -> list[dict[str, Any]]:
    if not inline_article_defs:
        return []
    metadata = chunk.get("metadata") or chunk
    document_id = str(chunk.get("document_id") or metadata.get("document_id") or "")
    regulation = metadata_regulation_name(metadata)
    chunk_type = str(metadata.get("chunk_type") or chunk.get("chunk_type") or "")
    edges: list[dict[str, Any]] = []
    for article_ref in sorted(inline_article_defs, key=normalize_key):
        edges.append(
            make_edge(
                "chunk_defines_inline_article",
                source_id,
                source_label,
                f"article:{document_id}:{normalize_key(regulation)}:{normalize_key(article_ref)}",
                " ".join(item for item in [regulation, article_ref] if item).strip(),
                chunk,
                evidence_type="inline_article_heading",
                evidence_text=article_ref,
                confidence=0.82,
                metadata={
                    "article_ref": article_ref,
                    "source_chunk_type": chunk_type,
                    "resolution_status": "inline_article_defined_in_chunk",
                },
            )
        )
    return edges


def build_contextual_regulation_article_contexts(chunks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    local_context_by_chunk_id: dict[str, dict[str, Any]] = {}
    chunk_records: list[tuple[dict[str, Any], str, str, tuple[str, str], dict[str, Any] | None]] = []
    for chunk in chunks:
        metadata = chunk.get("metadata") or chunk
        chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or "")
        if not chunk_id:
            continue
        document_id = str(chunk.get("document_id") or metadata.get("document_id") or "")
        source_regulation = str(metadata.get("regulation_no") or metadata.get("regulation_title") or "")
        document_regulation_key = (document_id, normalize_key(source_regulation))
        local_context = local_regulation_article_context(chunk)
        if local_context:
            local_context_by_chunk_id[chunk_id] = local_context
        chunk_records.append((chunk, chunk_id, document_id, document_regulation_key, local_context))

    current_by_document_regulation: dict[tuple[str, str], dict[str, Any]] = {}
    for chunk, chunk_id, _document_id, document_regulation_key, local_context in chunk_records:
        if local_context:
            contexts[chunk_id] = local_context
            current_by_document_regulation[document_regulation_key] = local_context
            continue
        carried_context = current_by_document_regulation.get(document_regulation_key)
        if carried_context and context_page_gap(carried_context, chunk) <= MAX_CONTEXT_PAGE_GAP:
            contexts[chunk_id] = {**carried_context, "context_scope": "carried"}
    for index, (chunk, chunk_id, _document_id, document_regulation_key, _local_context) in enumerate(chunk_records):
        if chunk_id in contexts or not chunk_has_unqualified_article_ref(chunk):
            continue
        for next_chunk, next_chunk_id, _next_document_id, next_document_regulation_key, _next_local in chunk_records[index + 1 : index + 4]:
            if next_document_regulation_key != document_regulation_key:
                break
            next_context = local_context_by_chunk_id.get(next_chunk_id)
            if not next_context:
                continue
            if lookahead_page_gap(chunk, next_chunk) > 0:
                break
            contexts[chunk_id] = {**next_context, "context_scope": "lookahead"}
            break
    return contexts


def chunk_has_unqualified_article_ref(chunk: dict[str, Any]) -> bool:
    metadata = chunk.get("metadata") or chunk
    return bool(metadata.get("article_refs") or any(ref.get("type") == "article" for ref in metadata.get("reference_edges") or []))


def lookahead_page_gap(chunk: dict[str, Any], next_chunk: dict[str, Any]) -> int:
    chunk_start = int_or_none(chunk.get("source_page_start"))
    next_start = int_or_none(next_chunk.get("source_page_start"))
    if chunk_start is None or next_start is None:
        return 0
    return max(0, next_start - chunk_start)


def local_regulation_article_context(chunk: dict[str, Any]) -> dict[str, Any] | None:
    metadata = chunk.get("metadata") or chunk
    contexts: list[dict[str, str]] = []
    for item in metadata.get("regulation_article_refs") or []:
        regulation_ref = clean_internal_reference_name(str((item or {}).get("regulation_ref") or ""))
        article_ref = normalize_article_ref(str((item or {}).get("article_ref") or ""))
        if regulation_ref and article_ref and not direct_law_reference_name(regulation_ref, set()):
            contexts.append({"regulation_ref": regulation_ref, "article_ref": article_ref})

    text = chunk_reference_text(chunk)
    internal_names = {normalize_key(value) for value in metadata.get("internal_regulation_refs") or []}
    external_names = {normalize_key(value) for value in metadata.get("external_law_refs") or []}
    for match in NAMED_ARTICLE_REF.finditer(text):
        name = clean_reference_name(match.group("name"))
        article_ref = normalize_article_ref(match.group("article"))
        if resolve_same_law_alias_reference_name(name, text, match.start()):
            continue
        normalized_name = normalize_key(name)
        if (
            name
            and article_ref
            and (normalized_name in internal_names or is_internal_reference_name(name))
            and normalized_name not in external_names
        ):
            contexts.append({"regulation_ref": clean_internal_reference_name(name), "article_ref": article_ref})

    if not contexts:
        return None
    regulation_keys = {normalize_key(item["regulation_ref"]) for item in contexts}
    if len(regulation_keys) != 1:
        return None
    regulation_ref = contexts[0]["regulation_ref"]
    article_refs = sorted({item["article_ref"] for item in contexts}, key=normalize_key)
    return {
        "regulation_ref": regulation_ref,
        "context_article_refs": article_refs,
        "context_chunk_id": chunk.get("chunk_id"),
        "context_page_start": chunk.get("source_page_start"),
        "context_page_end": chunk.get("source_page_end"),
        "context_scope": "local",
    }


def context_page_gap(context: dict[str, Any], chunk: dict[str, Any]) -> int:
    source_end = int_or_none(context.get("context_page_end")) or int_or_none(context.get("context_page_start"))
    chunk_start = int_or_none(chunk.get("source_page_start"))
    if source_end is None or chunk_start is None:
        return 0
    return max(0, chunk_start - source_end)


def int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def contextual_regulation_article_edge(
    chunk: dict[str, Any],
    source_id: str,
    source_label: str,
    value: str,
    context: dict[str, Any] | None,
    *,
    raw_reference_edge: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not context:
        return None
    metadata = chunk.get("metadata") or chunk
    regulation_ref = str(context.get("regulation_ref") or "").strip()
    article_ref = normalize_article_ref(value)
    source_regulation = metadata_regulation_name(metadata)
    if not regulation_ref or not article_ref or normalize_key(regulation_ref) == normalize_key(source_regulation):
        return None
    relation_type = "article_cites_regulation_article" if metadata.get("article_no") else "chunk_cites_regulation_article"
    context_scope = str(context.get("context_scope") or "local")
    confidence = {"local": 0.78, "carried": 0.72, "lookahead": 0.68}.get(context_scope, 0.7)
    return make_edge(
        relation_type,
        source_id,
        source_label,
        f"regulation_article:{normalize_key(regulation_ref)}:{normalize_key(article_ref)}",
        f"{regulation_ref} {article_ref}",
        chunk,
        evidence_type="contextual_article_ref",
        evidence_text=value,
        confidence=confidence,
        metadata={
            "reference_name": regulation_ref,
            "article_ref": article_ref,
            "base_article_ref": base_article_ref(article_ref),
            "scope": "internal",
            "context_inferred": True,
            "context_scope": context_scope,
            "context_chunk_id": context.get("context_chunk_id"),
            "context_article_refs": context.get("context_article_refs") or [],
            "resolution_status": "context_regulation_inferred",
            "raw_reference_edge": raw_reference_edge,
        },
    )


def named_article_reference_edges(
    chunk: dict[str, Any],
    source_id: str,
    source_label: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    metadata = chunk.get("metadata") or chunk
    text = chunk_reference_text(chunk)
    internal_names = {normalize_key(value) for value in metadata.get("internal_regulation_refs") or []}
    external_names = {normalize_key(value) for value in metadata.get("external_law_refs") or []}
    paired_article_refs: set[str] = set()
    edges: list[dict[str, Any]] = []
    for match in NAMED_ARTICLE_REF.finditer(text):
        name = clean_reference_name(match.group("name"))
        article = normalize_article_ref(match.group("article"))
        if not name or not article:
            continue
        paired_article_refs.add(article)
        alias_name = resolve_same_law_alias_reference_name(name, text, match.start())
        alias_resolved = bool(alias_name)
        same_law_alias = is_same_law_alias_name(name)
        if alias_name:
            name = alias_name
        law_name = direct_law_reference_name(name, external_names)
        if law_name:
            name = law_name
        normalized_name = normalize_key(name)
        internal = (
            (normalized_name in internal_names or is_internal_reference_name(name))
            and not alias_resolved
            and not same_law_alias
            and not law_name
        )
        external = alias_resolved or same_law_alias or bool(law_name) or normalized_name in external_names or not internal
        if external:
            relation_type = "article_cites_law_article" if metadata.get("article_no") else "chunk_cites_law_article"
            target_id = f"law_article:{normalize_key(name)}:{normalize_key(article)}"
        else:
            name = clean_internal_reference_name(name)
            relation_type = "article_cites_regulation_article" if metadata.get("article_no") else "chunk_cites_regulation_article"
            target_id = f"regulation_article:{normalize_key(name)}:{normalize_key(article)}"
        edges.append(
            make_edge(
                relation_type,
                source_id,
                source_label,
                target_id,
                f"{name} {article}",
                chunk,
                evidence_type="named_article_ref",
                evidence_text=match.group(0),
                confidence=0.9,
                metadata={
                    "reference_name": name,
                    "article_ref": article,
                    "scope": "external" if external else "internal",
                    "alias_reference_name": clean_reference_name(match.group("name")) if (alias_resolved or same_law_alias) else None,
                    "alias_resolved": alias_resolved,
                },
            )
        )
    return edges, paired_article_refs


def table_edges(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = chunk.get("metadata") or chunk
    if not metadata.get("table_like") and not metadata.get("table_cell_rows") and not metadata.get("table_rows"):
        return []
    source_id, source_label = source_node(chunk)
    table_id = str(metadata.get("table_id") or f"{chunk.get('chunk_id')}_table")
    table_label = str(
        metadata.get("table_citation_label")
        or metadata.get("table_title")
        or metadata.get("table_appendix_title")
        or metadata.get("hierarchy_path")
        or table_id
    )
    edges = [
        make_edge(
            "chunk_has_table",
            source_id,
            source_label,
            f"table:{table_id}",
            table_label,
            chunk,
            evidence_type="table",
            evidence_text=table_label,
            confidence=float(metadata.get("table_confidence") or 0.0),
            metadata={
                "table_classification": metadata.get("table_classification"),
                "table_review_required": bool(metadata.get("table_review_required")),
                "table_review_flags": metadata.get("table_review_flags") or [],
            },
        )
    ]
    cell_rows = metadata.get("table_cell_rows") or []
    if cell_rows:
        header_cells = [
            str(cell).strip()
            for cell in (metadata.get("table_header_cells") or (cell_rows[0].get("cells") if cell_rows else []))
            if str(cell).strip()
        ]
        for column_index, header in enumerate(header_cells):
            field_id = table_field_id(table_id, header)
            edges.append(
                make_edge(
                    "table_has_column",
                    f"table:{table_id}",
                    table_label,
                    field_id,
                    header,
                    chunk,
                    evidence_type="table_header",
                    evidence_text=header,
                    confidence=float(metadata.get("table_confidence") or 0.0),
                    metadata={"column_index": column_index},
                )
            )
        for index, row in enumerate(cell_rows):
            row_id = table_row_id(table_id, row.get("row_index", index))
            row_label = str(row.get("raw") or " | ".join(str(cell) for cell in row.get("cells") or []))
            edges.append(
                make_edge(
                    "table_has_row",
                    f"table:{table_id}",
                    table_label,
                    row_id,
                    row_label,
                    chunk,
                    evidence_type="table_row",
                    evidence_text=row_label,
                    confidence=float(metadata.get("table_confidence") or 0.0),
                    metadata={
                        "row_kind": "cell",
                        "row_index": row.get("row_index", index),
                        "row_quality_flags": row.get("row_quality_flags") or [],
                    },
                )
            )
            for column_index, cell in enumerate(row.get("cells") or []):
                cell_value = str(cell).strip()
                if not cell_value:
                    continue
                header = header_cells[column_index] if column_index < len(header_cells) else None
                edges.append(
                    make_edge(
                        "table_row_has_cell",
                        row_id,
                        row_label,
                        table_cell_id(table_id, row.get("row_index", index), column_index),
                        cell_value,
                        chunk,
                        evidence_type="table_cell",
                        evidence_text=cell_value,
                        confidence=float(metadata.get("table_confidence") or 0.0),
                        metadata={
                            "row_index": row.get("row_index", index),
                            "column_index": column_index,
                            "header": header,
                        },
                    )
                )
            record = table_record_for_row(metadata, row)
            for header, value in record.items():
                if not header or value in (None, ""):
                    continue
                field_id = table_field_id(table_id, str(header))
                value_id = table_value_id(str(value))
                edges.append(
                    make_edge(
                        "table_row_has_field",
                        row_id,
                        row_label,
                        field_id,
                        str(header),
                        chunk,
                        evidence_type="table_field",
                        evidence_text=f"{header}: {value}",
                        confidence=float(metadata.get("table_confidence") or 0.0),
                        metadata={"value": value, "value_node_id": value_id},
                    )
                )
                edges.append(
                    make_edge(
                        "table_row_has_value",
                        row_id,
                        row_label,
                        value_id,
                        str(value),
                        chunk,
                        evidence_type="table_value",
                        evidence_text=f"{header}: {value}",
                        confidence=float(metadata.get("table_confidence") or 0.0),
                        metadata={"field": str(header), "field_node_id": field_id},
                    )
                )
                edges.append(
                    make_edge(
                        "table_field_has_value",
                        field_id,
                        str(header),
                        value_id,
                        str(value),
                        chunk,
                        evidence_type="table_field_value",
                        evidence_text=f"{header}: {value}",
                        confidence=float(metadata.get("table_confidence") or 0.0),
                        metadata={"row_node_id": row_id, "row_label": row_label},
                    )
                )
    else:
        for index, raw in enumerate(metadata.get("table_rows") or []):
            raw_text = raw_table_row_text(raw)
            if not raw_text:
                continue
            edges.append(
                make_edge(
                    "table_has_raw_row",
                    f"table:{table_id}",
                    table_label,
                    table_row_id(table_id, index),
                    raw_text,
                    chunk,
                    evidence_type="raw_table_row",
                    evidence_text=raw_text,
                    confidence=float(metadata.get("table_confidence") or 0.0),
                    metadata={"row_kind": "raw", "row_index": index},
                )
            )
    return edges


def add_term_cooccurrence_edges(
    term_pairs: dict[tuple[str, str], dict[str, Any]],
    terms: list[str],
    chunk: dict[str, Any],
) -> None:
    unique_terms = sorted({term for term in terms if term})
    for left, right in combinations(unique_terms, 2):
        key = tuple(sorted((normalize_key(left), normalize_key(right))))
        source_label, target_label = sorted((left, right))
        edge = term_pairs.get(key)
        if edge is None:
            edge = make_edge(
                "term_cooccurs_with_term",
                f"term:{key[0]}",
                source_label,
                f"term:{key[1]}",
                target_label,
                chunk,
                evidence_type="term_window",
                evidence_text=str(chunk.get("chunk_id") or ""),
                confidence=0.5,
                weight=0,
                metadata={"sample_chunk_ids": []},
            )
            term_pairs[key] = edge
        edge["weight"] = int(edge.get("weight") or 0) + 1
        samples = edge.setdefault("metadata", {}).setdefault("sample_chunk_ids", [])
        chunk_id = chunk.get("chunk_id")
        if chunk_id and len(samples) < 5 and chunk_id not in samples:
            samples.append(chunk_id)
        edge["edge_id"] = stable_edge_id(edge)


def extract_chunk_terms(chunk: dict[str, Any], *, max_terms: int) -> list[str]:
    metadata = chunk.get("metadata") or chunk
    terms: Counter[str] = Counter()
    for field in ("internal_regulation_refs", "external_law_refs"):
        for value in metadata.get(field) or []:
            term = clean_term(str(value))
            if term:
                terms[term] += 3
    text = " ".join(
        str(value or "")
        for value in [
            metadata.get("regulation_title"),
            metadata.get("article_title"),
            metadata.get("hierarchy_path"),
            chunk.get("retrieval_text") or chunk.get("text") or chunk.get("normalized_text"),
        ]
    )
    for term in candidate_terms(text):
        terms[term] += 1
    for row in metadata.get("table_cell_rows") or []:
        for cell in row.get("cells") or []:
            for term in candidate_terms(str(cell)):
                terms[term] += 1
    ranked = sorted(terms.items(), key=lambda item: (-item[1], len(item[0]), item[0]))
    return [term for term, _ in ranked[:max_terms]]


def build_article_index(chunks: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for chunk in chunks:
        metadata = chunk.get("metadata") or chunk
        article_no = base_article_ref(str(metadata.get("article_no") or ""))
        if not article_no:
            continue
        document_id = str(chunk.get("document_id") or metadata.get("document_id") or "")
        regulation = normalize_key(metadata_regulation_name(metadata))
        if not document_id or not regulation:
            continue
        key = (document_id, regulation, normalize_key(article_no))
        existing = index.get(key)
        if existing is None or article_chunk_rank(chunk) < article_chunk_rank(existing):
            index[key] = chunk
    return index


def resolve_base_article_reference(
    chunk: dict[str, Any],
    value: str,
    article_index: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    metadata = chunk.get("metadata") or chunk
    base_ref = base_article_ref(value)
    if not base_ref:
        return None
    document_id = str(chunk.get("document_id") or metadata.get("document_id") or "")
    regulation = normalize_key(metadata_regulation_name(metadata))
    target = article_index.get((document_id, regulation, normalize_key(base_ref)))
    if not target:
        return None
    target_metadata = target.get("metadata") or target
    return {
        "type": "article",
        "value": value,
        "resolved": True,
        "base_article_resolved": normalize_article_ref(value) != normalize_article_ref(base_ref),
        "target_document_id": target.get("document_id"),
        "target_chunk_id": target.get("chunk_id"),
        "target_chunk_type": target.get("chunk_type"),
        "target_regulation_no": target_metadata.get("regulation_no"),
        "target_regulation_title": target_metadata.get("regulation_title"),
        "target_article_no": target_metadata.get("article_no") or base_ref,
        "target_article_title": target_metadata.get("article_title"),
    }


def article_relation_type(chunk: dict[str, Any], value: str, ref_edge: dict[str, Any] | None) -> str:
    metadata = chunk.get("metadata") or chunk
    current_base = base_article_ref(str(metadata.get("article_no") or ""))
    target_base = base_article_ref(value)
    if ref_edge and ref_edge.get("target_article_no"):
        target_base = base_article_ref(str(ref_edge.get("target_article_no") or target_base))
    if current_base and target_base and normalize_article_ref(current_base) == normalize_article_ref(target_base):
        return "article_cites_own_article_clause"
    if ref_edge and ref_edge.get("base_article_resolved"):
        return "article_cites_article_clause"
    return "article_cites_article"


def article_resolution_status(chunk: dict[str, Any], value: str, ref_edge: dict[str, Any] | None) -> str:
    if ref_edge and ref_edge.get("resolved"):
        return "resolved"
    metadata = chunk.get("metadata") or chunk
    chunk_type = str(metadata.get("chunk_type") or chunk.get("chunk_type") or "")
    article_ref = normalize_article_ref(value)
    base_ref = base_article_ref(value)
    if chunk_type in {"appendix", "form", "table"}:
        return f"{chunk_type}_context_target_not_indexed"
    if base_ref and normalize_article_ref(base_ref) != article_ref:
        return "base_article_not_indexed"
    return "target_article_not_indexed"


def article_chunk_rank(chunk: dict[str, Any]) -> tuple[int, int]:
    type_rank = {"article": 0, "paragraph": 1, "item": 2, "appendix": 3}
    page = chunk.get("source_page_start")
    return type_rank.get(str(chunk.get("chunk_type") or ""), 9), int(page if isinstance(page, int) else 999999)


def candidate_terms(text: str) -> Iterable[str]:
    for quoted in re.findall(r"[「｢]([^」｣]{2,80})[」｣]", text):
        term = clean_term(quoted)
        if term:
            yield term
    for raw in re.findall(r"[가-힣A-Za-z0-9·ㆍ\-]{2,40}", text):
        term = clean_term(raw)
        if not term:
            continue
        compact = normalize_key(term)
        if any(compact.endswith(normalize_key(suffix)) for suffix in TERM_SUFFIXES):
            yield term
            continue
        if len(term) >= 3 and re.search(r"(계약|입찰|보증|인사|복무|직제|채용|검사|안전|품질|위임|전결|교육|훈련)", term):
            yield term


def clean_term(value: str) -> str | None:
    term = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;()[]<>")
    term = normalize_term_surface(term)
    cleaned_internal_name = clean_internal_reference_name(term)
    if cleaned_internal_name != term and is_internal_reference_name(cleaned_internal_name):
        term = cleaned_internal_name
    if not term or len(term) < 2 or len(term) > 80:
        return None
    compact = normalize_key(term)
    if is_same_law_alias_name(term):
        return None
    if compact in GENERIC_LAW_NAMES:
        return None
    if compact in NORMALIZED_STOP_TERMS:
        return None
    if re.fullmatch(r"\d+(?:[.\-]\d+)*", compact):
        return None
    if is_code_like_term(compact):
        return None
    if re.match(r"^제\d+조", compact):
        return None
    if re.search(r"(한다|있다|없다|된다|따른다)$", compact):
        return None
    return term


def is_code_like_term(compact: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z가-힣]{1,20}\d{2,}", compact))


def normalize_term_surface(value: str) -> str:
    term = str(value or "").strip()
    term = re.sub(r"^(?:및|또는|또|각)\s*", "", term).strip()
    term = re.sub(r"(제\s*\d+\s*조.*)$", "", term).strip()
    term = re.sub(r"별표\s*\d+(?:\s*-\s*\d+)?$", "", term).strip()
    term = re.sub(r"(?<=[가-힣A-Za-z0-9])제$", "", term).strip()
    for _ in range(2):
        stripped = re.sub(
            r"(?<=[가-힣A-Za-z0-9])(?:은|는|이|가|을|를|의|에|에서|에게|으로|로|와|과|도|만)$",
            "",
            term,
        ).strip()
        if stripped == term:
            break
        term = stripped
    law_name = direct_law_reference_name(term, set())
    if law_name:
        term = law_name
    return collapse_repeated_surface(term)


def collapse_repeated_surface(value: str) -> str:
    term = str(value or "").strip()
    compact = normalize_key(term)
    if not compact:
        return term
    for repeat_count in range(5, 1, -1):
        if len(compact) % repeat_count:
            continue
        unit_length = len(compact) // repeat_count
        unit = compact[:unit_length]
        if not unit or unit * repeat_count != compact:
            continue
        if len(term) % repeat_count == 0:
            surface_unit = term[: len(term) // repeat_count]
            if normalize_key(surface_unit) == unit:
                return surface_unit.strip()
        return unit
    return term


def metadata_regulation_name(metadata: dict[str, Any]) -> str:
    return clean_regulation_label(str(metadata.get("regulation_no") or metadata.get("regulation_title") or ""))


def clean_regulation_label(value: str) -> str:
    label = clean_reference_name(value)
    label = re.sub(r"^\d+\.\s*", "", label).strip()
    return collapse_repeated_surface(label)


def source_node(chunk: dict[str, Any]) -> tuple[str, str]:
    metadata = chunk.get("metadata") or chunk
    document_id = str(chunk.get("document_id") or metadata.get("document_id") or "")
    regulation = metadata_regulation_name(metadata)
    article = str(metadata.get("article_no") or "")
    if article:
        return (
            f"article:{document_id}:{normalize_key(regulation)}:{normalize_key(article)}",
            " ".join(value for value in [regulation, article, str(metadata.get("article_title") or "")] if value).strip(),
        )
    if regulation:
        return (
            f"regulation_context:{document_id}:{normalize_key(regulation)}:{chunk.get('chunk_id')}",
            regulation,
        )
    return f"chunk:{chunk.get('chunk_id')}", str(metadata.get("hierarchy_path") or chunk.get("chunk_id") or "")


def article_target_node(
    chunk: dict[str, Any],
    value: str,
    ref_edge: dict[str, Any] | None,
) -> tuple[str, str]:
    metadata = chunk.get("metadata") or chunk
    if ref_edge and ref_edge.get("target_chunk_id"):
        target_document_id = str(ref_edge.get("target_document_id") or chunk.get("document_id") or "")
        target_regulation = clean_regulation_label(
            str(ref_edge.get("target_regulation_no") or ref_edge.get("target_regulation_title") or "")
        )
        target_article = str(ref_edge.get("target_article_no") or value)
        return (
            f"article:{target_document_id}:{normalize_key(target_regulation)}:{normalize_key(target_article)}",
            " ".join(
                item
                for item in [target_regulation, target_article, str(ref_edge.get("target_article_title") or "")]
                if item
            ).strip(),
        )
    document_id = str(chunk.get("document_id") or metadata.get("document_id") or "")
    regulation = metadata_regulation_name(metadata)
    return (
        f"article:{document_id}:{normalize_key(regulation)}:{normalize_key(value)}",
        " ".join(item for item in [regulation, value] if item).strip(),
    )


def table_record_for_row(metadata: dict[str, Any], table_row: dict[str, Any]) -> dict[str, Any]:
    row_index = table_row.get("row_index")
    for record in metadata.get("table_records") or []:
        if record.get("row_index") == row_index:
            return record.get("record") or {}
    return table_row.get("record") or {}


def raw_table_row_text(table_row: Any) -> str:
    if isinstance(table_row, str):
        return table_row.strip()
    if isinstance(table_row, dict):
        raw = table_row.get("raw")
        if raw:
            return str(raw).strip()
        return " ".join(str(cell) for cell in table_row.get("cells") or []).strip()
    return str(table_row or "").strip()


def table_row_id(table_id: str, row_index: Any) -> str:
    return f"table_row:{normalize_key(table_id)}:{row_index}"


def table_cell_id(table_id: str, row_index: Any, column_index: int) -> str:
    return f"table_cell:{normalize_key(table_id)}:{row_index}:{column_index}"


def table_field_id(table_id: str, header: str) -> str:
    return f"table_field:{normalize_key(table_id)}:{normalize_key(header)}"


def table_value_id(value: str) -> str:
    return f"table_value:{normalize_key(value)}"


def make_edge(
    relation_type: str,
    source_id: str,
    source_label: str,
    target_id: str,
    target_label: str,
    chunk: dict[str, Any],
    *,
    evidence_type: str,
    evidence_text: str,
    confidence: float,
    weight: int = 1,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    edge = {
        "relation_type": relation_type,
        "source_id": source_id,
        "source_label": source_label,
        "target_id": target_id,
        "target_label": target_label,
        "document_id": chunk.get("document_id"),
        "chunk_id": chunk.get("chunk_id"),
        "source_page_start": chunk.get("source_page_start"),
        "source_page_end": chunk.get("source_page_end"),
        "weight": weight,
        "confidence": round(float(confidence), 3),
        "evidence_type": evidence_type,
        "evidence_text": evidence_text,
        "metadata": metadata or {},
    }
    edge["edge_id"] = stable_edge_id(edge)
    return edge


def stable_edge_id(edge: dict[str, Any]) -> str:
    payload = "|".join(
        str(edge.get(key) or "")
        for key in ["relation_type", "source_id", "target_id", "chunk_id", "evidence_type", "evidence_text"]
    )
    return "edge_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for edge in edges:
        edge_id = str(edge.get("edge_id") or stable_edge_id(edge))
        if edge_id in seen:
            continue
        seen.add(edge_id)
        result.append(edge)
    return result


def relation_manifest(
    edges: list[dict[str, Any]],
    *,
    chunks: list[dict[str, Any]],
    source_label: str,
    source_batch_generated_at: Any,
    input_count: int | None,
    successful_count: int | None,
    max_terms_per_chunk: int,
) -> dict[str, Any]:
    relation_counts = Counter(edge.get("relation_type") for edge in edges)
    unresolved_article_edges = [
        edge for edge in edges
        if edge.get("relation_type") == "article_cites_article" and not (edge.get("metadata") or {}).get("resolved")
    ]
    unresolved_by_status = Counter(
        (edge.get("metadata") or {}).get("resolution_status") or "unknown" for edge in unresolved_article_edges
    )
    unresolved_by_chunk_type = Counter(
        (edge.get("metadata") or {}).get("source_chunk_type") or "unknown" for edge in unresolved_article_edges
    )
    unresolved_samples = [
        {
            "source": edge.get("source_label"),
            "target": edge.get("target_label"),
            "chunk_id": edge.get("chunk_id"),
            "evidence": edge.get("evidence_text"),
            "status": (edge.get("metadata") or {}).get("resolution_status"),
        }
        for edge in unresolved_article_edges[:20]
    ]
    table_relation_counts = {
        relation_type: count
        for relation_type, count in sorted(relation_counts.items())
        if str(relation_type).startswith("table_") or relation_type == "chunk_has_table"
    }
    top_term_edges = [
        {
            "source": edge.get("source_label"),
            "target": edge.get("target_label"),
            "weight": edge.get("weight"),
        }
        for edge in sorted(
            (edge for edge in edges if edge.get("relation_type") == "term_cooccurs_with_term"),
            key=lambda item: int(item.get("weight") or 0),
            reverse=True,
        )[:20]
    ]
    return {
        "report_type": "relation_graph",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source_label,
        "source_batch_generated_at": source_batch_generated_at,
        "input_count": input_count,
        "successful_count": successful_count,
        "chunk_count": len(chunks),
        "edge_count": len(edges),
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "table_relation_counts": table_relation_counts,
        "unresolved_article_edge_count": len(unresolved_article_edges),
        "unresolved_article_by_status": dict(sorted(unresolved_by_status.items())),
        "unresolved_article_by_chunk_type": dict(sorted(unresolved_by_chunk_type.items())),
        "unresolved_article_samples": unresolved_samples,
        "max_terms_per_chunk": max_terms_per_chunk,
        "top_term_cooccurrences": top_term_edges,
        "api_call_count": 0,
    }


def edges_to_csv(edges: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=RELATION_COLUMNS)
    writer.writeheader()
    for edge in edges:
        row = dict(edge)
        row["metadata_json"] = json.dumps(row.pop("metadata", {}) or {}, ensure_ascii=False, sort_keys=True)
        writer.writerow({column: row.get(column) for column in RELATION_COLUMNS})
    return buffer.getvalue()


def manifest_to_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Relation Graph Report",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source: {manifest.get('source')}",
        f"- Chunks: {manifest.get('chunk_count')}",
        f"- Edges: {manifest.get('edge_count')}",
        f"- Unresolved article edges: {manifest.get('unresolved_article_edge_count')}",
        "",
        "## Relation Types",
    ]
    for relation_type, count in (manifest.get("relation_type_counts") or {}).items():
        lines.append(f"- {relation_type}: {count}")
    lines.extend(["", "## Table Relations"])
    for relation_type, count in (manifest.get("table_relation_counts") or {}).items():
        lines.append(f"- {relation_type}: {count}")
    lines.extend(["", "## Unresolved Article Edges"])
    for status, count in (manifest.get("unresolved_article_by_status") or {}).items():
        lines.append(f"- {status}: {count}")
    if manifest.get("unresolved_article_by_chunk_type"):
        lines.append("")
        lines.append("By chunk type:")
        for chunk_type, count in (manifest.get("unresolved_article_by_chunk_type") or {}).items():
            lines.append(f"- {chunk_type}: {count}")
    if manifest.get("unresolved_article_samples"):
        lines.append("")
        lines.append("Samples:")
        for item in manifest.get("unresolved_article_samples") or []:
            lines.append(
                f"- {item.get('source')} -> {item.get('target')} "
                f"({item.get('status')}, {item.get('chunk_id')}): {item.get('evidence')}"
            )
    lines.extend(["", "## Top Term Cooccurrences"])
    for item in manifest.get("top_term_cooccurrences") or []:
        lines.append(f"- {item.get('source')} <-> {item.get('target')}: {item.get('weight')}")
    return "\n".join(lines).strip() + "\n"


def normalize_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(value or "")).lower()


NORMALIZED_STOP_TERMS = {normalize_key(item) for item in STOP_TERMS}


def normalize_article_ref(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def base_article_ref(value: str) -> str | None:
    compact = normalize_article_ref(value)
    match = re.match(r"^(제\d+조(?:의\d+)?)", compact)
    return match.group(1) if match else None


def clean_reference_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" .,:;()[]<>")


def clean_law_reference_name(value: str) -> str:
    name = clean_reference_name(value)
    replacements = [
        (r"공\s+기업", "공기업"),
        (r"준\s+정부기관", "준정부기관"),
        (r"계약사무규\s+칙", "계약사무규칙"),
        (r"시행\s+령", "시행령"),
        (r"시행\s+규칙", "시행규칙"),
    ]
    for pattern, replacement in replacements:
        name = re.sub(pattern, replacement, name)
    name = re.sub(r"(?<=[가-힣])\s+(?=법(?:률)?(?:시행|$))", "", name)
    return name


def clean_internal_reference_name(value: str) -> str:
    name = clean_reference_name(value)
    overcapture_trimmed = trim_internal_reference_overcapture(name)
    if overcapture_trimmed != name:
        return overcapture_trimmed
    for marker in (
        "에도 불구하고 ",
        "에도불구하고 ",
        "불구하고 ",
        "따른 ",
        "따라 ",
        "통해 ",
        "통해",
        "위반하여 ",
        "위반하여",
        "정하여 ",
        "정하여",
    ):
        if marker in name:
            candidate = clean_reference_name(name.rsplit(marker, 1)[-1])
            if candidate and is_internal_reference_name(candidate):
                return candidate
    return name


def trim_internal_reference_overcapture(value: str) -> str:
    name = clean_reference_name(value)
    if not name:
        return name
    marker_present = any(
        marker in name
        for marker in (
            "->",
            "본문 중",
            "으로 한다",
            "각각",
            "제1조",
            "제2조",
            "①",
            "②",
            "③",
        )
    )
    suffix_hits = sum(normalize_key(name).count(normalize_key(suffix)) for suffix in INTERNAL_REFERENCE_SUFFIXES)
    if not marker_present and suffix_hits <= 1:
        return name
    pieces = re.split(
        r"->|본문 중|으로 한다|각각|[「｢『“\"”」｣』]|[①②③④⑤⑥⑦⑧⑨⑩]|제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*[\(（][^\)）]{0,80}[\)）])?",
        name,
    )
    candidates: list[str] = []
    for piece in pieces:
        for match in INTERNAL_REFERENCE_TOKEN.finditer(piece):
            candidate = collapse_repeated_surface(clean_reference_name(match.group(0)))
            if candidate and is_internal_reference_name(candidate):
                candidates.append(candidate)
    return candidates[-1] if candidates else name


def is_internal_reference_name(value: str) -> bool:
    compact = normalize_key(value)
    return any(compact.endswith(normalize_key(suffix)) for suffix in INTERNAL_REFERENCE_SUFFIXES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export relation graph edges from chunk JSONL or a batch quality report.")
    parser.add_argument("--chunks-jsonl", default=None)
    parser.add_argument("--batch-report", action="append", default=None)
    parser.add_argument("--out-jsonl", default=None)
    parser.add_argument("--out-manifest", default=None)
    parser.add_argument("--out-csv", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--max-terms-per-chunk", type=int, default=12)
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    batch_reports = args.batch_report or []
    if not args.chunks_jsonl and not batch_reports:
        raise SystemExit("Provide --chunks-jsonl or --batch-report.")
    if args.chunks_jsonl and batch_reports:
        raise SystemExit("Provide only one of --chunks-jsonl or --batch-report.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_jsonl = Path(args.out_jsonl) if args.out_jsonl else Path("reports") / f"relation_graph_{timestamp}.jsonl"
    out_manifest = (
        Path(args.out_manifest)
        if args.out_manifest
        else out_jsonl.with_suffix(".manifest.json")
    )
    out_csv = Path(args.out_csv) if args.out_csv else out_jsonl.with_suffix(".csv")
    out_md = Path(args.out_md) if args.out_md else out_jsonl.with_suffix(".md")
    try:
        manifest = export_relation_graph(
            chunks_jsonl=Path(args.chunks_jsonl) if args.chunks_jsonl else None,
            batch_report_path=[Path(path) for path in batch_reports] if batch_reports else None,
            out_jsonl=out_jsonl,
            out_manifest=out_manifest,
            out_csv=out_csv,
            out_md=out_md,
            max_terms_per_chunk=args.max_terms_per_chunk,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
