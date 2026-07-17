from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


HASH_CHUNK_BYTES = 1024 * 1024


def build_temporal_ambiguity_review_scope(
    *,
    temporal_report: Path,
    vector_records_jsonl: Path | None = None,
    sample_limit_per_slice: int = 25,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    temporal = _load_json(temporal_report)
    vector_path = vector_records_jsonl or _path_from_report(temporal, "vector_path")
    records = list(_iter_jsonl(vector_path)) if vector_path and vector_path.exists() else []
    record_analysis = _analyze_records(records, sample_limit_per_slice=sample_limit_per_slice)
    before = _dict(temporal.get("before"))
    after = _dict(temporal.get("after"))
    delta = _dict(temporal.get("delta"))
    conflict_count = _int(after.get("conflict_chunk_count"))
    ambiguous_count = _int(after.get("ambiguous_chunk_count"))
    chunk_count = _int(after.get("chunk_count")) or _int(temporal.get("output_chunk_count"))

    status = _status(conflict_count=conflict_count, ambiguous_count=ambiguous_count)
    report = {
        "report_type": "temporal_ambiguity_review_scope",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "status": status,
        "passed": status == "temporal_ambiguity_clear",
        "summary": {
            "chunk_count": chunk_count,
            "before_temporal_metadata_count": _int(before.get("temporal_metadata_count")),
            "after_temporal_metadata_count": _int(after.get("temporal_metadata_count")),
            "delta_temporal_metadata_count": _int(delta.get("temporal_metadata_count")),
            "before_temporal_metadata_ratio": before.get("temporal_metadata_ratio"),
            "after_temporal_metadata_ratio": after.get("temporal_metadata_ratio"),
            "conflict_chunk_count": conflict_count,
            "ambiguous_chunk_count": ambiguous_count,
            "ambiguous_chunk_ratio": _ratio(ambiguous_count, chunk_count),
            "shadow_runtime_written": bool(temporal.get("shadow_runtime_written")),
            "write_blocked": bool(temporal.get("write_blocked")),
        },
        "ambiguous_field_counts": _dict(after.get("ambiguous_field_counts")),
        "record_analysis": record_analysis,
        "decision_requirements": _decision_requirements(conflict_count=conflict_count, ambiguous_count=ambiguous_count),
        "recommended_review_sequence": _recommended_review_sequence(record_analysis),
        "source_reports": {
            "temporal_report": str(temporal_report),
            "vector_records_jsonl": str(vector_path) if vector_path else None,
        },
        "source_report_artifacts": [
            _source_artifact("temporal_report", temporal_report, temporal),
            *([_source_artifact("vector_records_jsonl", vector_path, {})] if vector_path else []),
        ],
        "safety_note": (
            "This scope report is read-only. It does not apply temporal backfill, approve ambiguity policy, "
            "or write Vector DB records."
        ),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _analyze_records(records: list[dict[str, Any]], *, sample_limit_per_slice: int) -> dict[str, Any]:
    chunk_type_counts: Counter[str] = Counter()
    field_counts: Counter[str] = Counter()
    chunk_type_field_counts: dict[str, Counter[str]] = defaultdict(Counter)
    slice_counts: Counter[tuple[str, str]] = Counter()
    samples_by_slice: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    ambiguous_record_count = 0
    for record in records:
        metadata = _dict(record.get("metadata"))
        fields = [str(field) for field in metadata.get("temporal_metadata_ambiguous_fields") or []]
        if not fields:
            continue
        ambiguous_record_count += 1
        chunk_type = str(metadata.get("chunk_type") or record.get("chunk_type") or "unknown")
        chunk_type_counts[chunk_type] += 1
        field_key = "+".join(sorted(fields))
        slice_key = (chunk_type, field_key)
        slice_counts[slice_key] += 1
        for field in fields:
            field_counts[field] += 1
            chunk_type_field_counts[chunk_type][field] += 1
        if len(samples_by_slice[slice_key]) < sample_limit_per_slice:
            samples_by_slice[slice_key].append(_sample_record(record, metadata, fields))

    review_slices = []
    for (chunk_type, field_key), count in sorted(slice_counts.items(), key=lambda item: (-item[1], item[0])):
        review_slices.append(
            {
                "slice_id": f"{chunk_type}:{field_key}",
                "chunk_type": chunk_type,
                "ambiguous_fields": field_key.split("+") if field_key else [],
                "candidate_count": count,
                "sample_count": len(samples_by_slice[(chunk_type, field_key)]),
                "samples": samples_by_slice[(chunk_type, field_key)],
            }
        )
    return {
        "vector_record_count": len(records),
        "ambiguous_record_count": ambiguous_record_count,
        "ambiguous_by_chunk_type": dict(sorted(chunk_type_counts.items())),
        "ambiguous_by_field_from_records": dict(sorted(field_counts.items())),
        "ambiguous_by_chunk_type_and_field": {
            chunk_type: dict(sorted(counter.items())) for chunk_type, counter in sorted(chunk_type_field_counts.items())
        },
        "review_slice_count": len(review_slices),
        "review_slices": review_slices,
    }


def _sample_record(record: dict[str, Any], metadata: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    return {
        "document_id": str(record.get("document_id") or metadata.get("document_id") or ""),
        "chunk_id": str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
        "chunk_type": str(metadata.get("chunk_type") or record.get("chunk_type") or ""),
        "regulation_title": str(metadata.get("regulation_title") or metadata.get("document_name") or ""),
        "article_no": str(metadata.get("article_no") or ""),
        "source_page_start": metadata.get("source_page_start"),
        "ambiguous_fields": fields,
    }


def _decision_requirements(*, conflict_count: int, ambiguous_count: int) -> list[dict[str, Any]]:
    requirements = []
    if conflict_count:
        requirements.append(
            {
                "decision_id": "temporal_conflict_resolution",
                "required_decision": "Resolve hard temporal conflicts before applying backfill to an approved runtime.",
                "blocks_product_release": True,
                "evidence_required": ["conflict sample review", "backfill rerun with conflict_chunk_count=0"],
            }
        )
    if ambiguous_count:
        requirements.extend(
            [
                {
                    "decision_id": "temporal_ambiguity_index_policy",
                    "required_decision": "Decide whether chunks with temporal_metadata_ambiguous_fields remain indexable.",
                    "blocks_product_release": True,
                    "evidence_required": ["owner decision reference", "accepted ambiguity fields", "post-policy readiness report"],
                },
                {
                    "decision_id": "temporal_ambiguity_answer_policy",
                    "required_decision": "Decide how MCP answers disclose ambiguous effective/revision dates.",
                    "blocks_product_release": True,
                    "evidence_required": ["answer wording policy", "sample MCP answers with ambiguity disclosure"],
                },
            ]
        )
    if not requirements:
        requirements.append(
            {
                "decision_id": "temporal_ambiguity_clear",
                "required_decision": "No temporal ambiguity release decision is required.",
                "blocks_product_release": False,
                "evidence_required": ["temporal ambiguity scope report"],
            }
        )
    return requirements


def _recommended_review_sequence(record_analysis: dict[str, Any]) -> list[dict[str, Any]]:
    slices = _list_of_dicts(record_analysis.get("review_slices"))
    sequence = []
    for index, review_slice in enumerate(slices[:5], start=1):
        sequence.append(
            {
                "order": index,
                "slice_id": review_slice.get("slice_id"),
                "candidate_count": review_slice.get("candidate_count"),
                "sample_count": review_slice.get("sample_count"),
                "review_goal": _review_goal(review_slice),
            }
        )
    return sequence


def _review_goal(review_slice: dict[str, Any]) -> str:
    chunk_type = str(review_slice.get("chunk_type") or "")
    fields = ", ".join(str(field) for field in review_slice.get("ambiguous_fields") or [])
    if chunk_type == "article":
        return f"Confirm article-level citation behavior when {fields} is ambiguous."
    if chunk_type in {"form", "appendix"}:
        return f"Confirm table/form citation behavior when {fields} is ambiguous."
    return f"Confirm whether {chunk_type} chunks can carry {fields} ambiguity without silent date inference."


def _status(*, conflict_count: int, ambiguous_count: int) -> str:
    if conflict_count:
        return "temporal_conflict_blocked"
    if ambiguous_count:
        return "temporal_ambiguity_policy_required"
    return "temporal_ambiguity_clear"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _path_from_report(report: dict[str, Any], key: str) -> Path | None:
    value = report.get(key)
    return Path(str(value)) if value else None


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                yield payload


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _source_artifact(role: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    exists = path.exists()
    return {
        "role": role,
        "path": str(path),
        "exists": exists,
        "sha256": _sha256_file(path) if exists else None,
        "byte_count": path.stat().st_size if exists else None,
        "report_type": payload.get("report_type") if payload else None,
        "generated_at": payload.get("generated_at") if payload else None,
        "repo_commit": payload.get("repo_commit") if payload else None,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_markdown(report: dict[str, Any]) -> str:
    summary = _dict(report.get("summary"))
    analysis = _dict(report.get("record_analysis"))
    lines = [
        "# Temporal Ambiguity Review Scope",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Passed: `{str(bool(report.get('passed'))).lower()}`",
        f"- Chunks: {summary.get('chunk_count')}",
        f"- Temporal metadata: {summary.get('before_temporal_metadata_count')} -> {summary.get('after_temporal_metadata_count')} ({summary.get('delta_temporal_metadata_count'):+})",
        f"- Conflict chunks: {summary.get('conflict_chunk_count')}",
        f"- Ambiguous chunks: {summary.get('ambiguous_chunk_count')} ({summary.get('ambiguous_chunk_ratio')})",
        f"- Vector records analyzed: {analysis.get('vector_record_count')}",
        "",
        "## Ambiguity Distribution",
        "",
        f"- By field: {_compact_mapping(report.get('ambiguous_field_counts'))}",
        f"- By chunk type: {_compact_mapping(analysis.get('ambiguous_by_chunk_type'))}",
        "",
        "| Chunk Type | effective_date | revision_date |",
        "| --- | ---: | ---: |",
    ]
    by_type_field = _dict(analysis.get("ambiguous_by_chunk_type_and_field"))
    for chunk_type, counts in by_type_field.items():
        counts_dict = _dict(counts)
        lines.append(
            f"| {chunk_type} | {_int(counts_dict.get('effective_date'))} | {_int(counts_dict.get('revision_date'))} |"
        )

    lines.extend(["", "## Review Slices", "", "| Slice | Candidates | Samples | Goal |", "| --- | ---: | ---: | --- |"])
    for item in _list_of_dicts(report.get("recommended_review_sequence")):
        lines.append(
            "| {slice_id} | {candidate_count} | {sample_count} | {review_goal} |".format(
                slice_id=_md_cell(item.get("slice_id")),
                candidate_count=item.get("candidate_count"),
                sample_count=item.get("sample_count"),
                review_goal=_md_cell(item.get("review_goal")),
            )
        )

    lines.extend(["", "## Decision Requirements", "", "| Decision ID | Blocks Product Release | Required Decision | Evidence |", "| --- | --- | --- | --- |"])
    for requirement in _list_of_dicts(report.get("decision_requirements")):
        lines.append(
            "| {decision_id} | `{blocks}` | {decision} | {evidence} |".format(
                decision_id=_md_cell(requirement.get("decision_id")),
                blocks=str(bool(requirement.get("blocks_product_release"))).lower(),
                decision=_md_cell(requirement.get("required_decision")),
                evidence=_md_cell(_compact_list(requirement.get("evidence_required"))),
            )
        )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def _compact_mapping(value: Any, *, limit: int = 6) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    items = [f"{key}={count}" for key, count in sorted(value.items())]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... (+{len(items) - limit})"


def _compact_list(value: Any, *, limit: int = 5) -> str:
    if not isinstance(value, list):
        return ""
    values = [str(item) for item in value]
    if len(values) <= limit:
        return ", ".join(values) or "-"
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit})"


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only temporal ambiguity review scope from a shadow backfill report."
    )
    parser.add_argument("--temporal-report", type=Path, required=True)
    parser.add_argument("--vector-records-jsonl", type=Path)
    parser.add_argument("--sample-limit-per-slice", type=int, default=25)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    report = build_temporal_ambiguity_review_scope(
        temporal_report=args.temporal_report,
        vector_records_jsonl=args.vector_records_jsonl,
        sample_limit_per_slice=args.sample_limit_per_slice,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
