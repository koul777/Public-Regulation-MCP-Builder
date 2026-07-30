from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping

from app.ingestion.vector_adapter import stable_content_hash
from app.retrieval.bm25_index import Bm25Index, source_content_hashes
from app.retrieval.searcher import rerank_bm25_candidates


HIERARCHICAL_INDEX_SCHEMA_VERSION = "reg-rag-hierarchical-index-v2"
REBUILD_FINGERPRINT_SCHEMA_VERSION = "reg-rag-logical-corpus-v2"
HIERARCHICAL_INDEX_RELATIVE_PATH = Path("hierarchy") / "regulation_hierarchy.sqlite3"
_DATE_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
_QUERY_TOKEN_RE = re.compile(r"[0-9A-Za-z\uac00-\ud7a3]+")
_ARTICLE_RE = re.compile(r"^\s*(\uc81c\s*\d+\s*\uc870(?:\uc758\s*\d+)?)")
_FTS_PREFIX_ARTICLE_LOCATOR_RE = re.compile(
    r"^\uc81c[1-9]\d*\uc870(?:\uc758[1-9]\d*)?$"
)
_HISTORICAL_LIFECYCLE_STATUSES = ("approved", "superseded", "repealed")
_CURRENT_LIFECYCLE_STATUSES = ("approved", "superseded")
_KOREAN_QUERY_SUFFIXES = (
    "\uc5d0\uc11c",
    "\uc73c\ub85c",
    "\uae4c\uc9c0",
    "\ubd80\ud130",
    "\uc774\ub77c\ub294",
    "\uc740",
    "\ub294",
    "\uc774",
    "\uac00",
    "\uc744",
    "\ub97c",
    "\uc758",
    "\uc640",
    "\uacfc",
    "\ub85c",
    "\uc5d0",
    "\ub3c4",
    "\ub9cc",
)


def _default_as_of_date() -> str:
    """Return the calendar date used for default current-version selection."""

    return date.today().isoformat()


def hierarchical_index_path(data_dir: str | Path) -> Path:
    """Return the conventional institution hierarchy index path."""
    return Path(data_dir) / HIERARCHICAL_INDEX_RELATIVE_PATH


def normalize_regulation_title(value: object) -> str:
    """Normalize a regulation title for stable institution-local identity."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", text)
    return text


def normalize_regulation_number(value: object) -> str:
    """Normalize a regulation number without collapsing distinct number segments."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\uac00-\ud7a3./-]", "", text)
    canonical_match = re.fullmatch(r"\uc81c?(\d+(?:[-./]\d+)*)(?:\ud638)?", text)
    if canonical_match:
        return "-".join(re.split(r"[-./]", canonical_match.group(1)))
    return text


def regulation_unit_id_for(
    *,
    profile_id: object,
    regulation_title: object,
    regulation_no: object = None,
) -> str:
    """Create a stable ID for one regulation inside an institution profile."""
    normalized_profile = unicodedata.normalize("NFKC", str(profile_id or "")).casefold().strip()
    normalized_title = normalize_regulation_title(regulation_title)
    normalized_no = normalize_regulation_number(regulation_no)
    if normalized_title and normalized_no:
        # A title normally identifies one institution regulation, but distinct
        # regulations can legally share a title. Keep their regulation numbers
        # in the fallback identity instead of silently collapsing one of them.
        identity = f"title:{normalized_title}\nno:{normalized_no}"
    else:
        identity = normalized_title or normalized_no or "unknown-regulation"
    digest = hashlib.sha256(f"{normalized_profile}\n{identity}".encode("utf-8")).hexdigest()[:20]
    return f"regunit-{digest}"


def canonicalize_runtime_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return records in a stable logical order independent of upload order."""
    return sorted(records, key=_runtime_record_sort_key)


def logical_corpus_sha256_for_records(
    records: Iterable[dict[str, Any]],
    *,
    tenant_id: str,
    profile_id: str | None,
) -> str:
    """Recompute the logical hierarchy fingerprint from scoped runtime records."""

    record_list = records if isinstance(records, list) else list(records)
    _, scoped_profile_id = _validated_runtime_record_scope(
        record_list,
        tenant_id=tenant_id,
        profile_id=profile_id,
    )
    canonical_records = canonicalize_runtime_records(record_list)
    record_identities = _canonical_record_regulation_identities(
        canonical_records,
        fallback_profile_id=scoped_profile_id,
    )
    version_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in canonical_records:
        document_id, chunk_id = _record_identity(record)
        _add_runtime_record_to_version_groups(
            version_groups,
            record=record,
            identity=record_identities.get((document_id, chunk_id), {}),
            scoped_profile_id=scoped_profile_id,
        )
    return _logical_corpus_hash(_finalize_versions(version_groups))


def _canonical_record_regulation_identities(
    records: Iterable[Mapping[str, Any]],
    *,
    fallback_profile_id: object,
) -> dict[tuple[str, str], dict[str, str]]:
    """Choose one stable identity for every regulation segment in each document.

    Parser-derived table rows can occasionally place a title fragment in
    ``regulation_no`` or shorten ``regulation_title``. Most uploaded files are
    one regulation revision, but a combined regulation book can contain
    multiple numbered regulations in one document. Numbered segments therefore
    stay distinct while unnumbered noise is reconciled to a unique segment only
    when the evidence is unambiguous. Once those document-local segments are
    fixed, authoritative lifecycle metadata may join renamed or renumbered
    revisions without letting a binder-level ID collapse sibling regulations.
    """

    records_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        metadata = _metadata(record)
        document_id, chunk_id = _record_identity(record)
        if not document_id:
            continue
        title = str(metadata.get("regulation_title") or metadata.get("document_name") or "").strip()
        regulation_no = str(metadata.get("regulation_no") or "").strip()
        records_by_document[document_id].append(
            {
                "record_key": (document_id, chunk_id),
                "metadata": metadata,
                "title": title,
                "normalized_title": normalize_regulation_title(title),
                "regulation_no": regulation_no,
                "normalized_regulation_no": (
                    normalize_regulation_number(regulation_no)
                    if _is_plausible_regulation_number(
                        regulation_no,
                        regulation_title=title,
                    )
                    else ""
                ),
            }
        )

    identities: dict[tuple[str, str], dict[str, str]] = {}
    for entries in records_by_document.values():
        numbered_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        unassigned_entries: list[dict[str, Any]] = []
        for entry in entries:
            normalized_no = str(entry["normalized_regulation_no"])
            if normalized_no:
                numbered_groups[normalized_no].append(entry)
            else:
                unassigned_entries.append(entry)

        numbered_titles = {
            group_key: _canonical_group_title(group_entries)
            for group_key, group_entries in numbered_groups.items()
        }
        assigned_groups: dict[str, list[dict[str, Any]]] = {
            group_key: list(group_entries)
            for group_key, group_entries in numbered_groups.items()
        }
        sole_numbered_group = next(iter(numbered_groups)) if len(numbered_groups) == 1 else None
        for entry in unassigned_entries:
            selected_group = sole_numbered_group
            if selected_group is None and numbered_groups:
                probe_title = str(entry["normalized_title"])
                matching_groups = [
                    group_key
                    for group_key, group_title in numbered_titles.items()
                    if _regulation_titles_match(probe_title, group_title)
                ]
                if len(matching_groups) == 1:
                    selected_group = matching_groups[0]
            if selected_group is None:
                selected_group = f"title:{entry['normalized_title'] or 'unknown'}"
            assigned_groups.setdefault(selected_group, []).append(entry)

        for group_key, group_entries in assigned_groups.items():
            allow_document_name_fallback = len(numbered_groups) <= 1
            title = _canonical_group_title(
                group_entries,
                allow_document_name_fallback=allow_document_name_fallback,
            )
            plausible_numbers = Counter(
                str(entry["regulation_no"])
                for entry in group_entries
                if str(entry["normalized_regulation_no"])
            )
            regulation_no = _consensus_value(
                plausible_numbers,
                normalizer=normalize_regulation_number,
            )
            profile_values = Counter(
                str(entry["metadata"].get("profile_id") or "").strip()
                for entry in group_entries
                if str(entry["metadata"].get("profile_id") or "").strip()
            )
            profile_id = _consensus_value(
                profile_values,
                normalizer=lambda value: unicodedata.normalize(
                    "NFKC",
                    str(value or ""),
                )
                .casefold()
                .strip(),
            ) or str(fallback_profile_id or "").strip()
            identity = {
                "title": title,
                "regulation_no": regulation_no,
                "profile_id": profile_id,
            }
            for entry in group_entries:
                identities[entry["record_key"]] = identity
    return _reconcile_authoritative_regulation_lineages(
        records_by_document,
        identities,
    )


def _reconcile_authoritative_regulation_lineages(
    records_by_document: Mapping[str, list[dict[str, Any]]],
    identities: dict[tuple[str, str], dict[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    """Join document-local regulation segments only on unambiguous lineage evidence.

    ``regulation_id`` is authoritative for a regulation family only when it is
    demonstrably regulation-scoped. A shared ID on two numbered segments in one
    combined document is treated as a binder/container ID and is ignored.
    ``supersedes_document_id`` is accepted only for a one-segment-to-one-segment
    document edge with no branch or cycle.
    """

    segments: list[dict[str, Any]] = []
    segments_by_document: dict[tuple[str, str], list[int]] = defaultdict(list)
    for document_id, entries in records_by_document.items():
        grouped_entries: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            record_key = entry["record_key"]
            identity = identities.get(record_key)
            if not identity:
                continue
            profile_key = _normalize_lineage_key(identity.get("profile_id"))
            fallback_unit_id = regulation_unit_id_for(
                profile_id=identity.get("profile_id"),
                regulation_title=identity.get("title"),
                regulation_no=identity.get("regulation_no"),
            )
            grouped_entries[(profile_key, fallback_unit_id)].append(entry)

        for (profile_key, fallback_unit_id), segment_entries in grouped_entries.items():
            normalized_document_id = _normalize_document_reference(document_id)
            metadata_values = [entry["metadata"] for entry in segment_entries]
            stable_regulation_id = _unique_normalized_metadata_value(
                metadata_values,
                "regulation_id",
                normalizer=_normalize_lineage_key,
            )
            supersedes_document_id = _unique_normalized_metadata_value(
                metadata_values,
                "supersedes_document_id",
                normalizer=_normalize_document_reference,
            )
            lineage_dates = [
                candidate
                for metadata in metadata_values
                if (
                    candidate := (
                        _first_date(
                            metadata.get("effective_from"),
                            metadata.get("valid_from"),
                            metadata.get("effective_date"),
                        )
                        or _latest_date(metadata.get("revision_date"))
                    )
                )
            ]
            document_names = {
                str(metadata.get("document_name") or "").strip()
                for metadata in metadata_values
                if str(metadata.get("document_name") or "").strip()
            }
            segment_index = len(segments)
            segments.append(
                {
                    "record_keys": [entry["record_key"] for entry in segment_entries],
                    "profile_key": profile_key,
                    "document_id": normalized_document_id,
                    "fallback_unit_id": fallback_unit_id,
                    "stable_regulation_id": stable_regulation_id,
                    "supersedes_document_id": supersedes_document_id,
                    "lineage_date": max(lineage_dates, default=""),
                    "container_context": any(
                        _is_generic_regulation_container_title(value)
                        for value in document_names
                    ),
                }
            )
            segments_by_document[(profile_key, normalized_document_id)].append(segment_index)

    if not segments:
        return identities

    parents = list(range(len(segments)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    # Preserve the legacy title/number identity behavior before adding stronger
    # lifecycle evidence.
    segments_by_fallback: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, segment in enumerate(segments):
        segments_by_fallback[
            (segment["profile_key"], segment["fallback_unit_id"])
        ].append(index)
    for matching_segments in segments_by_fallback.values():
        for index in matching_segments[1:]:
            union(matching_segments[0], index)

    segments_by_stable_id: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, segment in enumerate(segments):
        if segment["stable_regulation_id"]:
            segments_by_stable_id[
                (segment["profile_key"], segment["stable_regulation_id"])
            ].append(index)
    for (_profile_key, stable_regulation_id), matching_segments in segments_by_stable_id.items():
        if not _stable_lineage_group_is_unambiguous(
            stable_regulation_id,
            matching_segments,
            segments=segments,
            segments_by_document=segments_by_document,
        ):
            continue
        for index in matching_segments[1:]:
            union(matching_segments[0], index)

    supersedes_edges = _unambiguous_supersedes_edges(
        segments,
        segments_by_document=segments_by_document,
    )
    for successor, predecessor in supersedes_edges:
        union(successor, predecessor)

    component_members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(segments)):
        component_members[find(index)].append(index)
    successor_nodes = {successor for successor, _predecessor in supersedes_edges}
    predecessor_nodes = {predecessor for _successor, predecessor in supersedes_edges}
    for members in component_members.values():
        lineage_roots = [
            index
            for index in members
            if index in predecessor_nodes and index not in successor_nodes
        ]
        anchor_candidates = lineage_roots or members
        anchor = min(
            anchor_candidates,
            key=lambda index: _lineage_segment_sort_key(segments[index]),
        )
        canonical_unit_id = str(segments[anchor]["fallback_unit_id"])
        for index in members:
            for record_key in segments[index]["record_keys"]:
                identities[record_key] = {
                    **identities[record_key],
                    "unit_id": canonical_unit_id,
                }
    return identities


def _stable_lineage_group_is_unambiguous(
    stable_regulation_id: str,
    segment_indexes: list[int],
    *,
    segments: list[dict[str, Any]],
    segments_by_document: Mapping[tuple[str, str], list[int]],
) -> bool:
    fallback_ids = {
        str(segments[index]["fallback_unit_id"])
        for index in segment_indexes
    }
    if len(fallback_ids) <= 1:
        return True
    if _is_generic_regulation_container_id(stable_regulation_id):
        return False
    if any(
        len(
            segments_by_document.get(
                (
                    str(segments[index]["profile_key"]),
                    str(segments[index]["document_id"]),
                ),
                (),
            )
        )
        != 1
        for index in segment_indexes
    ):
        return False
    if any(bool(segments[index]["container_context"]) for index in segment_indexes):
        return False

    # Two different regulation identities with the same lifecycle date are
    # concurrent siblings, not a safely inferable rename/renumber sequence.
    fallback_ids_by_date: dict[str, set[str]] = defaultdict(set)
    for index in segment_indexes:
        lineage_date = str(segments[index]["lineage_date"] or "")
        if not lineage_date:
            return False
        fallback_ids_by_date[lineage_date].add(
            str(segments[index]["fallback_unit_id"])
        )
    return all(len(values) == 1 for values in fallback_ids_by_date.values())


def _unambiguous_supersedes_edges(
    segments: list[dict[str, Any]],
    *,
    segments_by_document: Mapping[tuple[str, str], list[int]],
) -> list[tuple[int, int]]:
    candidate_edges: list[tuple[int, int]] = []
    for successor, segment in enumerate(segments):
        predecessor_document_id = str(segment["supersedes_document_id"] or "")
        if not predecessor_document_id:
            continue
        successor_document_key = (
            str(segment["profile_key"]),
            str(segment["document_id"]),
        )
        predecessor_document_key = (
            str(segment["profile_key"]),
            predecessor_document_id,
        )
        successor_segments = segments_by_document.get(successor_document_key, ())
        predecessor_segments = segments_by_document.get(predecessor_document_key, ())
        if len(successor_segments) != 1 or len(predecessor_segments) != 1:
            continue
        predecessor = predecessor_segments[0]
        if predecessor == successor:
            continue
        successor_date = str(segment["lineage_date"] or "")
        predecessor_date = str(segments[predecessor]["lineage_date"] or "")
        if successor_date and predecessor_date and successor_date < predecessor_date:
            continue
        candidate_edges.append((successor, predecessor))

    predecessor_counts = Counter(predecessor for _successor, predecessor in candidate_edges)
    successor_counts = Counter(successor for successor, _predecessor in candidate_edges)
    candidate_edges = [
        (successor, predecessor)
        for successor, predecessor in candidate_edges
        if predecessor_counts[predecessor] == 1 and successor_counts[successor] == 1
    ]
    cycle_nodes = _supersedes_cycle_nodes(candidate_edges)
    return [
        (successor, predecessor)
        for successor, predecessor in candidate_edges
        if successor not in cycle_nodes and predecessor not in cycle_nodes
    ]


def _supersedes_cycle_nodes(edges: Iterable[tuple[int, int]]) -> set[int]:
    predecessor_by_successor = {
        successor: predecessor
        for successor, predecessor in edges
    }
    cycle_nodes: set[int] = set()
    for start in predecessor_by_successor:
        path: list[int] = []
        positions: dict[int, int] = {}
        current = start
        while current in predecessor_by_successor:
            if current in positions:
                cycle_nodes.update(path[positions[current] :])
                break
            positions[current] = len(path)
            path.append(current)
            current = predecessor_by_successor[current]
    return cycle_nodes


def _unique_normalized_metadata_value(
    metadata_values: Iterable[Mapping[str, Any]],
    field_name: str,
    *,
    normalizer: Callable[[object], str],
) -> str:
    normalized_values = {
        normalized
        for metadata in metadata_values
        if (normalized := normalizer(metadata.get(field_name)))
    }
    if len(normalized_values) != 1:
        return ""
    return next(iter(normalized_values))


def _normalize_lineage_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _normalize_document_reference(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _is_generic_regulation_container_id(value: object) -> bool:
    normalized = normalize_regulation_title(value)
    return any(
        marker in normalized
        for marker in (
            "binder",
            "catalog",
            "container",
            "regulationbook",
            "\uaddc\uc815\uc9d1",
            "\uaddc\uc815\ubaa8\uc74c",
            "\ud1b5\ud569\uaddc\uc815",
        )
    )


def _lineage_segment_sort_key(segment: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(segment.get("lineage_date") or "9999-12-31"),
        str(segment.get("fallback_unit_id") or ""),
        str(segment.get("document_id") or ""),
    )


def _canonical_group_title(
    entries: Iterable[Mapping[str, Any]],
    *,
    allow_document_name_fallback: bool = False,
) -> str:
    entry_list = list(entries)
    title_values = Counter(
        str(entry.get("metadata", {}).get("regulation_title") or "").strip()
        for entry in entry_list
        if str(entry.get("metadata", {}).get("regulation_title") or "").strip()
    )
    document_name_values = Counter(
        str(entry.get("metadata", {}).get("document_name") or "").strip()
        for entry in entry_list
        if str(entry.get("metadata", {}).get("document_name") or "").strip()
    )
    title = _consensus_value(
        title_values,
        normalizer=normalize_regulation_title,
    )
    document_name = _consensus_value(
        document_name_values,
        normalizer=normalize_regulation_title,
    )
    normalized_title = normalize_regulation_title(title)
    normalized_document_name = normalize_regulation_title(document_name)
    title_candidates = {
        normalize_regulation_title(value)
        for value in title_values
        if normalize_regulation_title(value)
    }
    if document_name and not _is_generic_regulation_container_title(document_name):
        if (
            not title
            or normalized_document_name in title_candidates
            or (
                allow_document_name_fallback
                and normalized_title
                and normalized_title in normalized_document_name
                and len(normalized_document_name) > len(normalized_title)
            )
        ):
            return document_name
    return title or document_name


def _regulation_titles_match(left: object, right: object) -> bool:
    normalized_left = normalize_regulation_title(left)
    normalized_right = normalize_regulation_title(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    shorter, longer = sorted((normalized_left, normalized_right), key=len)
    return len(shorter) >= 4 and shorter in longer


def _consensus_value(
    values: Counter[str],
    *,
    normalizer: Callable[[object], str],
) -> str:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for value, count in values.items():
        normalized = normalizer(value)
        if normalized:
            grouped[normalized][value] += count
    if not grouped:
        return ""
    selected_group = max(
        grouped,
        key=lambda normalized: (
            sum(grouped[normalized].values()),
            len(normalized),
            normalized,
        ),
    )
    representatives = grouped[selected_group]
    return max(
        representatives,
        key=lambda value: (representatives[value], len(value), value),
    )


def _is_generic_regulation_container_title(value: object) -> bool:
    normalized = normalize_regulation_title(value)
    return normalized in {
        "\uaddc\uc815",
        "\uaddc\uc815\ubaa8\uc74c",
        "\uaddc\uc815\uc9d1",
        "\ub0b4\ubd80\uaddc\uc815\uc9d1",
        "\ud1b5\ud569\uaddc\uc815\uc9d1",
    } or normalized.endswith("\uaddc\uc815\uc9d1")


def _is_plausible_regulation_number(
    value: object,
    *,
    regulation_title: object,
) -> bool:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = normalize_regulation_number(text)
    if not normalized or not re.search(r"\d", normalized) or len(normalized) > 80:
        return False
    # Parser/table metadata can leak a structural locator into regulation_no.
    # Treating "제16조" (or a chapter/paragraph locator) as a regulation number
    # splits one uploaded regulation into a phantom second catalog unit.
    if re.match(
        r"^\s*제?\s*\d+(?:\s*의\s*\d+)?\s*(?:편|장|절|관|조|항)",
        text,
    ):
        return False
    if normalize_regulation_title(text) == normalize_regulation_title(regulation_title):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}", text):
        return False
    return True


def write_vector_records_with_offsets(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Write vector JSONL and return byte offsets keyed by document/chunk."""
    record_list = records if isinstance(records, list) else list(records)
    total_records = len(record_list)
    progress_step = max(1, total_records // 100)
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    offsets: dict[tuple[str, str], tuple[int, int]] = {}
    offset = 0
    with output_path.open("wb") as handle:
        for current, record in enumerate(record_list, start=1):
            payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            document_id, chunk_id = _record_identity(record)
            if document_id and chunk_id:
                offsets[(document_id, chunk_id)] = (offset, len(payload))
            handle.write(payload)
            offset += len(payload)
            if progress_callback is not None and (current == total_records or current % progress_step == 0):
                progress_callback(current, total_records)
    return offsets


def build_hierarchical_runtime_index(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    tenant_id: str,
    profile_id: str | None,
    vector_offsets: Mapping[tuple[str, str], tuple[int, int]] | None = None,
    progress_callback: Callable[[int, str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Build a regulation catalog, TOC, version, and body-search index."""
    record_list = records if isinstance(records, list) else list(records)
    scoped_tenant_id, scoped_profile_id = _validated_runtime_record_scope(
        record_list,
        tenant_id=tenant_id,
        profile_id=profile_id,
    )
    records = canonicalize_runtime_records(record_list)
    corpus_source_content_hashes = source_content_hashes(records)
    total_records = len(records)
    record_identities = _canonical_record_regulation_identities(
        records,
        fallback_profile_id=scoped_profile_id,
    )
    progress_step = max(1, total_records // 100)
    reference_graph: dict[str, Any] = {
        "edges": [],
        "cycles": [],
        "stats": {},
    }
    _report_hierarchy_progress(progress_callback, 1, "계층 색인 준비", 0, total_records)
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    connection = sqlite3.connect(output_path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        _create_schema(connection)
        connection.executemany(
            "INSERT INTO index_metadata(key, value) VALUES(?, ?)",
            [
                ("schema_version", HIERARCHICAL_INDEX_SCHEMA_VERSION),
                ("tenant_id", scoped_tenant_id),
                ("profile_id", scoped_profile_id),
                ("record_count", str(len(records))),
                ("source_content_hashes", corpus_source_content_hashes),
            ],
        )

        version_groups: dict[tuple[str, str], dict[str, Any]] = {}
        prepared_records: list[dict[str, Any]] = []
        for fallback_order, record in enumerate(records, start=1):
            metadata = _metadata(record)
            document_id, chunk_id = _record_identity(record)
            identity = record_identities.get((document_id, chunk_id), {})
            unit_id, version_id = _add_runtime_record_to_version_groups(
                version_groups,
                record=record,
                identity=identity,
                scoped_profile_id=scoped_profile_id,
            )
            offset, length = (vector_offsets or {}).get((document_id, chunk_id), (-1, -1))
            prepared_records.append(
                {
                    "record": record,
                    "unit_id": unit_id,
                    "version_id": version_id,
                    "order_index": _integer(metadata.get("order_index"), fallback_order),
                    "vector_offset": offset,
                    "vector_length": length,
                }
            )
            if fallback_order == total_records or fallback_order % progress_step == 0:
                percent = 3 + int((fallback_order / max(total_records, 1)) * 24)
                _report_hierarchy_progress(
                    progress_callback,
                    percent,
                    "규정·개정판 분류",
                    fallback_order,
                    total_records,
                )

        finalized_versions = _finalize_versions(version_groups)
        logical_corpus_sha256 = _logical_corpus_hash(finalized_versions)
        connection.execute(
            "INSERT INTO index_metadata(key, value) VALUES(?, ?)",
            ("logical_corpus_sha256", logical_corpus_sha256),
        )
        _report_hierarchy_progress(
            progress_callback,
            30,
            "최신판과 개정 이력 확정",
            len(finalized_versions),
            len(finalized_versions),
        )
        connection.executemany(
            """
            INSERT INTO regulation_versions(
                version_id, unit_id, document_id, profile_id, institution_name,
                regulation_no, title, source_version, revision_date, effective_from,
                effective_to, repealed_at, status, is_current, is_navigation, chunk_count,
                content_hash, search_text
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["version_id"],
                    item["unit_id"],
                    item["document_id"],
                    item["profile_id"],
                    item["institution_name"],
                    item["regulation_no"],
                    item["title"],
                    item["version_label"],
                    item["revision_date"],
                    item["effective_from"],
                    item["effective_to"],
                    item["repealed_at"],
                    item["status"],
                    item["is_current"],
                    item["is_navigation"],
                    item["chunk_count"],
                    item["content_hash"],
                    item["search_text"],
                )
                for item in finalized_versions.values()
            ],
        )

        toc_rows: dict[str, tuple[Any, ...]] = {}
        for prepared_index, prepared in enumerate(prepared_records, start=1):
            record = prepared["record"]
            metadata = _metadata(record)
            document_id, chunk_id = _record_identity(record)
            cursor = connection.execute(
                """
                INSERT INTO chunks(
                    record_id, document_id, chunk_id, version_id, unit_id, chunk_type,
                    hierarchy_path, article_no, article_title, parent_id, entity_id,
                    order_index, vector_offset, vector_length, content_hash
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.get("id") or f"{document_id}:{chunk_id}"),
                    document_id,
                    chunk_id,
                    prepared["version_id"],
                    prepared["unit_id"],
                    str(metadata.get("chunk_type") or ""),
                    str(metadata.get("hierarchy_path") or ""),
                    str(metadata.get("article_no") or ""),
                    str(metadata.get("article_title") or ""),
                    str(metadata.get("parent_id") or ""),
                    str(metadata.get("entity_id") or ""),
                    prepared["order_index"],
                    prepared["vector_offset"],
                    prepared["vector_length"],
                    str(record.get("content_hash") or ""),
                ),
            )
            row_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO chunks_fts(rowid, regulation_title, hierarchy_path, article_title, body) VALUES(?, ?, ?, ?, ?)",
                (
                    row_id,
                    str(metadata.get("regulation_title") or ""),
                    str(metadata.get("hierarchy_path") or ""),
                    " ".join(
                        value
                        for value in (
                            str(metadata.get("article_no") or ""),
                            str(metadata.get("article_title") or ""),
                        )
                        if value
                    ),
                    str(record.get("text") or ""),
                ),
            )
            for toc_row in _toc_rows_for_record(
                record,
                version_id=prepared["version_id"],
                unit_id=prepared["unit_id"],
                order_index=prepared["order_index"],
            ):
                node_id = str(toc_row[0])
                existing = toc_rows.get(node_id)
                if existing is None or int(toc_row[8]) < int(existing[8]):
                    toc_rows[node_id] = toc_row
            if prepared_index == total_records or prepared_index % progress_step == 0:
                percent = 32 + int((prepared_index / max(total_records, 1)) * 55)
                _report_hierarchy_progress(
                    progress_callback,
                    percent,
                    "조문 본문·목차 색인",
                    prepared_index,
                    total_records,
                )

        _report_hierarchy_progress(progress_callback, 92, "목차 트리 저장", len(toc_rows), len(toc_rows))
        connection.executemany(
            """
            INSERT INTO toc_nodes(
                node_id, version_id, unit_id, parent_id, node_type, label,
                number, title, order_index, hierarchy_path, chunk_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            list(toc_rows.values()),
        )
        _report_hierarchy_progress(
            progress_callback,
            95,
            "규정 간 조문 참조 해석",
            0,
            len(finalized_versions),
        )
        from app.retrieval.regulation_reference_graph import (
            build_regulation_reference_graph,
        )

        reference_graph = build_regulation_reference_graph(
            _current_reference_graph_records(
                prepared_records,
                finalized_versions,
                tenant_id=scoped_tenant_id,
            )
        )
        _store_reference_graph(connection, reference_graph)
        graph_stats = (
            reference_graph.get("stats")
            if isinstance(reference_graph.get("stats"), dict)
            else {}
        )
        connection.executemany(
            "INSERT OR REPLACE INTO index_metadata(key, value) VALUES(?, ?)",
            [
                ("reference_edge_count", str(int(graph_stats.get("edge_count") or 0))),
                (
                    "resolved_reference_edge_count",
                    str(int(graph_stats.get("resolved_edge_count") or 0)),
                ),
                (
                    "unresolved_reference_edge_count",
                    str(int(graph_stats.get("unresolved_edge_count") or 0)),
                ),
                (
                    "ambiguous_reference_edge_count",
                    str(int(graph_stats.get("ambiguous_edge_count") or 0)),
                ),
                ("reference_cycle_count", str(int(graph_stats.get("cycle_count") or 0))),
            ],
        )
        connection.commit()
        connection.execute("PRAGMA optimize")
        _report_hierarchy_progress(progress_callback, 100, "계층 색인 완료", total_records, total_records)
    finally:
        connection.close()

    version_count = len(finalized_versions)
    unit_count = len({item["unit_id"] for item in finalized_versions.values() if not item["is_navigation"]})
    current_count = sum(1 for item in finalized_versions.values() if item["is_current"] and not item["is_navigation"])
    return {
        "schema_version": HIERARCHICAL_INDEX_SCHEMA_VERSION,
        "rebuild_fingerprint_schema_version": REBUILD_FINGERPRINT_SCHEMA_VERSION,
        "logical_corpus_sha256": logical_corpus_sha256,
        "source_content_hashes": corpus_source_content_hashes,
        "path": str(output_path),
        "sha256": _sha256_file(output_path),
        "record_count": len(records),
        "regulation_count": unit_count,
        "current_regulation_count": current_count,
        "regulation_version_count": version_count,
        "toc_node_count": len(toc_rows),
        "reference_edge_count": int((reference_graph.get("stats") or {}).get("edge_count") or 0),
        "resolved_reference_edge_count": int(
            (reference_graph.get("stats") or {}).get("resolved_edge_count") or 0
        ),
        "unresolved_reference_edge_count": int(
            (reference_graph.get("stats") or {}).get("unresolved_edge_count") or 0
        ),
        "ambiguous_reference_edge_count": int(
            (reference_graph.get("stats") or {}).get("ambiguous_edge_count") or 0
        ),
        "reference_cycle_count": int(
            (reference_graph.get("stats") or {}).get("cycle_count") or 0
        ),
    }


def index_summary(path: str | Path) -> dict[str, Any] | None:
    index_path = Path(path)
    if not index_path.is_file():
        return None
    with _connect_readonly(index_path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM index_metadata"))
        regulation_count = connection.execute(
            "SELECT COUNT(DISTINCT unit_id) FROM regulation_versions WHERE is_navigation=0"
        ).fetchone()[0]
        current_count = connection.execute(
            "SELECT COUNT(*) FROM regulation_versions WHERE is_current=1 AND is_navigation=0"
        ).fetchone()[0]
        version_count = connection.execute("SELECT COUNT(*) FROM regulation_versions").fetchone()[0]
        toc_count = connection.execute("SELECT COUNT(*) FROM toc_nodes").fetchone()[0]
        reference_edge_count = connection.execute(
            "SELECT COUNT(*) FROM regulation_reference_edges"
        ).fetchone()[0]
        resolved_reference_edge_count = connection.execute(
            "SELECT COUNT(*) FROM regulation_reference_edges WHERE status='resolved'"
        ).fetchone()[0]
        unresolved_reference_edge_count = connection.execute(
            "SELECT COUNT(*) FROM regulation_reference_edges WHERE status='unresolved'"
        ).fetchone()[0]
        ambiguous_reference_edge_count = connection.execute(
            "SELECT COUNT(*) FROM regulation_reference_edges WHERE status='ambiguous'"
        ).fetchone()[0]
        reference_cycle_count = connection.execute(
            "SELECT COUNT(*) FROM regulation_reference_cycles"
        ).fetchone()[0]
    logical_corpus_sha256 = metadata.get("logical_corpus_sha256")
    if (
        not isinstance(logical_corpus_sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", logical_corpus_sha256)
    ):
        logical_corpus_sha256 = None
    return {
        "schema_version": metadata.get("schema_version"),
        "tenant_id": metadata.get("tenant_id"),
        "profile_id": metadata.get("profile_id"),
        "record_count": _integer(metadata.get("record_count"), 0),
        "source_content_hashes": metadata.get("source_content_hashes"),
        "logical_corpus_sha256": logical_corpus_sha256,
        "regulation_count": int(regulation_count),
        "current_regulation_count": int(current_count),
        "regulation_version_count": int(version_count),
        "toc_node_count": int(toc_count),
        "reference_edge_count": int(reference_edge_count),
        "resolved_reference_edge_count": int(resolved_reference_edge_count),
        "unresolved_reference_edge_count": int(unresolved_reference_edge_count),
        "ambiguous_reference_edge_count": int(ambiguous_reference_edge_count),
        "reference_cycle_count": int(reference_cycle_count),
        "path": str(index_path),
    }


def search_hierarchical_records(
    index_path: str | Path,
    vector_path: str | Path,
    *,
    query: str,
    top_k: int,
    profile_id: str | None = None,
    document_id: str | None = None,
    as_of_date: str | None = None,
    allowed_unit_ids: set[str] | None = None,
    rerank_index: Bm25Index | None = None,
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    """Search allowed catalog units first, then retrieve body evidence by offset."""
    path = Path(index_path)
    terms = query_terms(query)
    with _connect_readonly(path) as connection:
        versions = _selected_version_rows(
            connection,
            profile_id=profile_id,
            document_id=document_id,
            as_of_date=as_of_date,
            allowed_unit_ids=allowed_unit_ids,
        )
        ranked_versions = _rank_versions(query, terms, versions)
        positive = [item for item in ranked_versions if item[0] > 0]
        selected = positive[: max(5, min(16, top_k * 3))] if positive else ranked_versions
        selected_version_ids = [str(item[1]["version_id"]) for item in selected]
        rows = _search_chunk_rows(
            connection,
            query=query,
            terms=terms,
            version_ids=selected_version_ids,
            limit=max(top_k * 6, 24),
        )

    version_scores = {str(row["version_id"]): float(score) for score, row in selected}
    records = _read_vector_records_at(vector_path, rows)
    results: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for row, record in zip(rows, records):
        if record is None:
            continue
        identity = _record_identity(record)
        if identity in seen:
            continue
        seen.add(identity)
        lexical_score = float(row["retrieval_score"])
        catalog_score = min(version_scores.get(str(row["version_id"]), 0.0), 100.0) / 100.0
        results.append((round(lexical_score + catalog_score, 8), record))
    if rerank_index is not None:
        results = rerank_bm25_candidates(query, results, rerank_index)
    results.sort(
        key=lambda item: (
            item[0],
            _normalize_date(_metadata(item[1]).get("revision_date")),
            _logical_text(_metadata(item[1]).get("hierarchy_path")),
        ),
        reverse=True,
    )
    results = results[:top_k]

    candidate_regulations = [
        {
            "regulation_unit_id": str(row["unit_id"]),
            "regulation_no": str(row["regulation_no"] or ""),
            "regulation_title": str(row["title"] or ""),
            "version": str(row["source_version"] or ""),
            "revision_date": str(row["revision_date"] or ""),
            "effective_from": str(row["effective_from"] or ""),
            "effective_to": str(row["effective_to"] or ""),
            "repealed_at": (
                str(row["repealed_at"] or "")
                if "repealed_at" in row.keys()
                else ""
            ),
            "status": str(row["status"] or ""),
            "catalog_score": round(float(score), 4),
        }
        for score, row in selected[:16]
    ]
    return results, {
        "retrieval_model": "institution-hierarchical-sqlite-fts-v1",
        "retrieval_strategy": "catalog_toc_body",
        "retrieval_fallback": False,
        "candidate_reranker": (
            "verified_bm25_fast_query"
            if rerank_index is not None
            else None
        ),
        "candidate_regulation_count": len(candidate_regulations),
        "candidate_regulations": candidate_regulations,
        "query_terms": terms,
    }


def list_indexed_regulations(
    path: str | Path,
    *,
    profile_id: str | None = None,
    query: str | None = None,
    include_history: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with _connect_readonly(Path(path)) as connection:
        if include_history:
            clauses = ["v.is_navigation=0"]
            params: list[Any] = []
            if profile_id:
                clauses.append("lower(v.profile_id)=lower(?)")
                params.append(profile_id)
            rows = connection.execute(
                f"""
                SELECT v.*,
                       (SELECT COUNT(*) FROM regulation_versions h WHERE h.unit_id=v.unit_id) AS version_count
                FROM regulation_versions v
                WHERE {' AND '.join(clauses)}
                ORDER BY v.regulation_no, v.title, v.revision_date DESC
                LIMIT ?
                """,
                [*params, max(1, min(int(limit), 100_000))],
            ).fetchall()
            current_version_ids = _runtime_current_version_ids(
                connection,
                profile_id=profile_id,
                unit_ids={str(row["unit_id"]) for row in rows},
            )
        else:
            rows = _selected_version_rows(
                connection,
                profile_id=profile_id,
                document_id=None,
                as_of_date=None,
                allowed_unit_ids=None,
            )
            rows = sorted(rows, key=lambda row: str(row["revision_date"] or ""), reverse=True)
            rows = sorted(
                rows,
                key=lambda row: (
                    str(row["regulation_no"] or "").casefold(),
                    str(row["title"] or "").casefold(),
                ),
            )[: max(1, min(int(limit), 100_000))]
            current_version_ids = {str(row["version_id"]) for row in rows}
    items = [
        _public_regulation_row(
            row,
            is_current=str(row["version_id"]) in current_version_ids,
        )
        for row in rows
    ]
    if not str(query or "").strip():
        return items
    terms = query_terms(str(query))
    ranked = _rank_versions(str(query), terms, rows)
    items_by_version = {item["version_id"]: item for item in items}
    return [
        dict(items_by_version[version_id], catalog_score=round(score, 4))
        for score, row in ranked
        if score > 0
        and (version_id := str(row["version_id"])) in items_by_version
    ]


def page_indexed_regulations(
    path: str | Path,
    *,
    profile_id: str | None = None,
    query: str | None = None,
    include_history: bool = False,
    page: int = 1,
    page_size: int = 50,
    allowed_unit_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return one approved row per regulation unit with SQL pagination.

    Catalog pagination must not load an arbitrary 100,000-row window merely to
    render its first page. Filtering, identity deduplication, counting, sorting,
    and slicing therefore stay inside the read-only SQLite index.
    """

    normalized_page = max(1, int(page))
    normalized_page_size = max(1, min(int(page_size), 100))
    if allowed_unit_ids is not None and not allowed_unit_ids:
        return [], 0
    lifecycle_statuses = (
        _HISTORICAL_LIFECYCLE_STATUSES
        if include_history
        else _CURRENT_LIFECYCLE_STATUSES
    )
    clauses = [
        "v.is_navigation=0",
        "lower(v.status) IN ("
        + ",".join("?" for _ in lifecycle_statuses)
        + ")",
    ]
    params: list[Any] = list(lifecycle_statuses)
    if not include_history:
        selection_date = _default_as_of_date()
        clauses.extend(
            [
                "(v.effective_from='' OR v.effective_from<=?)",
                "(v.effective_to='' OR v.effective_to>=?)",
                "NOT (lower(v.status)='superseded' AND v.effective_to='')",
            ]
        )
        params.extend([selection_date, selection_date])
    if profile_id:
        clauses.append("lower(v.profile_id)=lower(?)")
        params.append(profile_id)
    if allowed_unit_ids is not None:
        ordered_unit_ids = sorted(allowed_unit_ids)
        clauses.append(
            "v.unit_id IN (" + ",".join("?" for _ in ordered_unit_ids) + ")"
        )
        params.extend(ordered_unit_ids)
    terms = query_terms(str(query or ""))
    if terms:
        term_clauses: list[str] = []
        for term in terms:
            escaped = str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            term_clauses.append("lower(v.search_text) LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped.casefold()}%")
        clauses.append("(" + " OR ".join(term_clauses) + ")")

    offset = (normalized_page - 1) * normalized_page_size
    with _connect_readonly(Path(path)) as connection:
        if (
            not include_history
            and _regulation_versions_has_repealed_at(connection)
        ):
            clauses.append("(v.repealed_at='' OR ?<v.repealed_at)")
            params.append(selection_date)
        common_sql = f"""
            WITH filtered AS (
                SELECT v.*
                FROM regulation_versions v
                WHERE {' AND '.join(clauses)}
            ),
            ranked AS (
                SELECT filtered.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY {"version_id" if include_history else "unit_id"}
                           ORDER BY COALESCE(NULLIF(effective_from, ''), revision_date) DESC,
                                    CASE lower(status) WHEN 'approved' THEN 1 ELSE 0 END DESC,
                                    revision_date DESC, source_version DESC, version_id DESC
                       ) AS unit_rank
                FROM filtered
            )
        """
        total_row = connection.execute(
            common_sql + " SELECT COUNT(*) AS total_count FROM ranked WHERE unit_rank=1",
            params,
        ).fetchone()
        rows = connection.execute(
            common_sql
            + """
                SELECT ranked.*,
                       (
                           SELECT COUNT(*)
                           FROM regulation_versions history
                           WHERE history.unit_id=ranked.unit_id
                       ) AS version_count
                FROM ranked
                WHERE unit_rank=1
                ORDER BY title COLLATE NOCASE, regulation_no COLLATE NOCASE, unit_id
                LIMIT ? OFFSET ?
            """,
            [*params, normalized_page_size, offset],
        ).fetchall()
        current_version_ids = _runtime_current_version_ids(
            connection,
            profile_id=profile_id,
            unit_ids={str(row["unit_id"]) for row in rows},
        )
    total_count = int(total_row["total_count"] or 0) if total_row is not None else 0
    return [
        _public_regulation_row(
            row,
            is_current=str(row["version_id"]) in current_version_ids,
        )
        for row in rows
    ], total_count


def indexed_document_ids(
    path: str | Path,
    *,
    profile_id: str | None = None,
) -> set[str]:
    """Return distinct non-empty document IDs represented by indexed chunks."""

    clauses: list[str] = []
    params: list[Any] = []
    if profile_id:
        clauses.append("lower(v.profile_id)=lower(?)")
        params.append(profile_id)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect_readonly(Path(path)) as connection:
        rows = connection.execute(
            f"""
            SELECT DISTINCT c.document_id
            FROM chunks c
            JOIN regulation_versions v ON v.version_id=c.version_id
            {where_sql}
            """,
            params,
        ).fetchall()
    return {
        document_id
        for row in rows
        if (document_id := str(row["document_id"] or ""))
    }


def fully_visible_regulation_unit_ids(
    path: str | Path,
    *,
    visible_record_keys: set[tuple[str, str]] | None = None,
    visible_record_signatures: set[tuple[str, str, str]] | None = None,
    profile_id: str | None = None,
) -> set[str]:
    """Return units whose indexed chunks are all visible to the current caller.

    Callers must provide exactly one visibility representation. Signature mode
    additionally requires every indexed chunk to have non-empty identity fields
    and an exact ``content_hash`` match.
    """

    if (visible_record_keys is None) == (visible_record_signatures is None):
        raise ValueError(
            "provide exactly one of visible_record_keys or visible_record_signatures"
        )

    clauses: list[str] = []
    params: list[Any] = []
    if profile_id:
        clauses.append("lower(v.profile_id)=lower(?)")
        params.append(profile_id)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect_readonly(Path(path)) as connection:
        rows = connection.execute(
            f"""
            SELECT c.unit_id, c.document_id, c.chunk_id, c.content_hash
            FROM chunks c
            JOIN regulation_versions v ON v.version_id=c.version_id
            {where_sql}
            """,
            params,
        ).fetchall()
    if visible_record_signatures is not None:
        indexed_signatures_by_unit: dict[
            str, set[tuple[str, str, str]]
        ] = defaultdict(set)
        invalid_signature_units: set[str] = set()
        for row in rows:
            unit_id = str(row["unit_id"] or "")
            document_id = str(row["document_id"] or "")
            chunk_id = str(row["chunk_id"] or "")
            content_hash = str(row["content_hash"] or "")
            if not unit_id:
                continue
            if not document_id or not chunk_id or not content_hash:
                invalid_signature_units.add(unit_id)
                continue
            indexed_signatures_by_unit[unit_id].add(
                (document_id, chunk_id, content_hash)
            )
        return {
            unit_id
            for unit_id, indexed_signatures in indexed_signatures_by_unit.items()
            if (
                unit_id not in invalid_signature_units
                and indexed_signatures
                and indexed_signatures.issubset(visible_record_signatures)
            )
        }

    assert visible_record_keys is not None
    indexed_keys_by_unit: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        indexed_keys_by_unit[str(row["unit_id"] or "")].add(
            (str(row["document_id"] or ""), str(row["chunk_id"] or ""))
        )
    return {
        unit_id
        for unit_id, indexed_keys in indexed_keys_by_unit.items()
        if unit_id and indexed_keys and indexed_keys.issubset(visible_record_keys)
    }


def regulation_toc(
    path: str | Path,
    *,
    regulation_unit_id: str,
    as_of_date: str | None = None,
    max_nodes: int = 1000,
) -> dict[str, Any]:
    with _connect_readonly(Path(path)) as connection:
        version = _version_for_unit(connection, regulation_unit_id, as_of_date=as_of_date)
        if version is None:
            return {"regulation": None, "nodes": []}
        rows = connection.execute(
            """
            SELECT node_id, parent_id, node_type, label, number, title,
                   order_index, hierarchy_path, chunk_id
            FROM toc_nodes
            WHERE version_id=?
            ORDER BY order_index, hierarchy_path
            LIMIT ?
            """,
            (version["version_id"], max(1, min(int(max_nodes), 5000))),
        ).fetchall()
    depth_by_id: dict[str, int] = {}
    nodes: list[dict[str, Any]] = []
    for row in rows:
        parent_id = str(row["parent_id"] or "")
        depth = depth_by_id.get(parent_id, -1) + 1 if parent_id else 0
        node_id = str(row["node_id"])
        depth_by_id[node_id] = depth
        nodes.append(
            {
                "node_id": node_id,
                "parent_id": parent_id or None,
                "node_type": str(row["node_type"] or "section"),
                "label": str(row["label"] or ""),
                "number": str(row["number"] or ""),
                "title": str(row["title"] or ""),
                "depth": depth,
                "order_index": int(row["order_index"] or 0),
                "hierarchy_path": str(row["hierarchy_path"] or ""),
                "chunk_id": str(row["chunk_id"] or ""),
            }
        )
    return {
        "regulation": _public_regulation_row(version, is_current=True),
        "nodes": nodes,
    }


def regulation_references(
    path: str | Path,
    *,
    regulation_unit_id: str,
    direction: str = "both",
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    allowed_unit_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Return materialized current-corpus cross-regulation references."""

    normalized_direction = str(direction or "both").strip().casefold()
    if normalized_direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("direction must be outgoing, incoming, or both.")
    normalized_status = str(status or "").strip().casefold()
    if normalized_status and normalized_status not in {"resolved", "unresolved", "ambiguous"}:
        raise ValueError("status must be resolved, unresolved, or ambiguous.")
    normalized_page = max(1, int(page))
    normalized_page_size = max(1, min(int(page_size), 100))
    if allowed_unit_ids is not None and regulation_unit_id not in allowed_unit_ids:
        return {
            "regulation": None,
            "references": [],
            "cycles": [],
            "total_count": 0,
            "page": normalized_page,
            "page_size": normalized_page_size,
        }
    clauses: list[str] = []
    params: list[Any] = []
    if normalized_direction == "outgoing":
        clauses.append("e.source_unit_id=?")
        params.append(regulation_unit_id)
    elif normalized_direction == "incoming":
        clauses.append(
            "("
            "e.target_unit_id=? OR EXISTS("
            "SELECT 1 FROM regulation_reference_edge_candidates c "
            "WHERE c.edge_id=e.edge_id AND c.unit_id=?"
            ")"
            ")"
        )
        params.extend((regulation_unit_id, regulation_unit_id))
    else:
        clauses.append(
            "("
            "e.source_unit_id=? OR e.target_unit_id=? OR EXISTS("
            "SELECT 1 FROM regulation_reference_edge_candidates c "
            "WHERE c.edge_id=e.edge_id AND c.unit_id=?"
            ")"
            ")"
        )
        params.extend((regulation_unit_id, regulation_unit_id, regulation_unit_id))
    if normalized_status:
        clauses.append("e.status=?")
        params.append(normalized_status)
    ordered_allowed_units = (
        sorted(allowed_unit_ids)
        if allowed_unit_ids is not None
        else None
    )
    if ordered_allowed_units is not None:
        placeholders = ",".join("?" for _ in ordered_allowed_units)
        clauses.extend(
            (
                f"e.source_unit_id IN ({placeholders})",
                f"(e.target_unit_id='' OR e.target_unit_id IN ({placeholders}))",
                (
                    "NOT EXISTS("
                    "SELECT 1 FROM regulation_reference_edge_candidates denied "
                    "WHERE denied.edge_id=e.edge_id "
                    f"AND denied.unit_id NOT IN ({placeholders})"
                    ")"
                ),
            )
        )
        params.extend(ordered_allowed_units)
        params.extend(ordered_allowed_units)
        params.extend(ordered_allowed_units)
    where_sql = " AND ".join(clauses)
    offset = (normalized_page - 1) * normalized_page_size

    with _connect_readonly(Path(path)) as connection:
        version = _version_for_unit(connection, regulation_unit_id, as_of_date=None)
        if version is None:
            return {
                "regulation": None,
                "references": [],
                "cycles": [],
                "total_count": 0,
                "page": normalized_page,
                "page_size": normalized_page_size,
            }
        total_row = connection.execute(
            f"""
            SELECT COUNT(*) AS total_count
            FROM regulation_reference_edges e
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT e.source_unit_id, e.target_unit_id, e.payload_json
            FROM regulation_reference_edges e
            WHERE {where_sql}
            ORDER BY e.source_unit_id, e.target_unit_id, e.edge_type,
                     e.requested_article, e.edge_id
            LIMIT ? OFFSET ?
            """,
            [*params, normalized_page_size, offset],
        ).fetchall()
        cycle_clauses = ["u.unit_id=?"]
        cycle_params: list[Any] = [regulation_unit_id]
        if ordered_allowed_units is not None:
            placeholders = ",".join("?" for _ in ordered_allowed_units)
            cycle_clauses.append(
                "NOT EXISTS("
                "SELECT 1 FROM regulation_reference_cycle_units denied "
                "WHERE denied.cycle_id=c.cycle_id "
                f"AND denied.unit_id NOT IN ({placeholders})"
                ")"
            )
            cycle_params.extend(ordered_allowed_units)
        cycle_rows = connection.execute(
            f"""
            SELECT c.payload_json
            FROM regulation_reference_cycles c
            JOIN regulation_reference_cycle_units u ON u.cycle_id=c.cycle_id
            WHERE {' AND '.join(cycle_clauses)}
            ORDER BY c.size DESC, c.cycle_id
            """,
            cycle_params,
        ).fetchall()

    references: list[dict[str, Any]] = []
    for row in rows:
        payload = _load_json_object(row["payload_json"])
        if payload is None:
            continue
        source_unit_id = str(row["source_unit_id"] or "")
        target_unit_id = str(row["target_unit_id"] or "")
        if source_unit_id == regulation_unit_id and target_unit_id == regulation_unit_id:
            relationship = "self"
        elif source_unit_id == regulation_unit_id:
            relationship = "outgoing"
        else:
            relationship = "incoming"
        references.append({**payload, "relationship": relationship})
    cycles = [
        payload
        for row in cycle_rows
        if (payload := _load_json_object(row["payload_json"])) is not None
    ]
    return {
        "regulation": _public_regulation_row(version, is_current=True),
        "references": references,
        "cycles": cycles,
        "total_count": int(total_row["total_count"] or 0) if total_row is not None else 0,
        "page": normalized_page,
        "page_size": normalized_page_size,
    }


def page_reference_cycles(
    path: str | Path,
    *,
    profile_id: str | None = None,
    regulation_unit_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
    allowed_unit_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return deterministic pages of current-corpus circular references."""

    normalized_page = max(1, int(page))
    normalized_page_size = max(1, min(int(page_size), 100))
    if allowed_unit_ids is not None and (
        not allowed_unit_ids
        or (regulation_unit_id and regulation_unit_id not in allowed_unit_ids)
    ):
        return [], 0
    joins = ""
    clauses: list[str] = []
    params: list[Any] = []
    if regulation_unit_id:
        joins = "JOIN regulation_reference_cycle_units u ON u.cycle_id=c.cycle_id"
        clauses.append("u.unit_id=?")
        params.append(regulation_unit_id)
    if profile_id:
        clauses.append("lower(c.profile_id)=lower(?)")
        params.append(profile_id)
    if allowed_unit_ids is not None:
        ordered_unit_ids = sorted(allowed_unit_ids)
        placeholders = ",".join("?" for _ in ordered_unit_ids)
        clauses.append(
            "NOT EXISTS("
            "SELECT 1 FROM regulation_reference_cycle_units denied "
            "WHERE denied.cycle_id=c.cycle_id "
            f"AND denied.unit_id NOT IN ({placeholders})"
            ")"
        )
        params.extend(ordered_unit_ids)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    offset = (normalized_page - 1) * normalized_page_size
    with _connect_readonly(Path(path)) as connection:
        total_row = connection.execute(
            f"""
            SELECT COUNT(DISTINCT c.cycle_id) AS total_count
            FROM regulation_reference_cycles c
            {joins}
            {where_sql}
            """,
            params,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT DISTINCT c.cycle_id, c.size, c.payload_json
            FROM regulation_reference_cycles c
            {joins}
            {where_sql}
            ORDER BY c.size DESC, c.cycle_id
            LIMIT ? OFFSET ?
            """,
            [*params, normalized_page_size, offset],
        ).fetchall()
    cycles = [
        payload
        for row in rows
        if (payload := _load_json_object(row["payload_json"])) is not None
    ]
    total_count = int(total_row["total_count"] or 0) if total_row is not None else 0
    return cycles, total_count


def load_record_by_chunk(
    index_path: str | Path,
    vector_path: str | Path,
    *,
    document_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    with _connect_readonly(Path(index_path)) as connection:
        row = connection.execute(
            """
            SELECT c.*, 1.0 AS retrieval_score
            FROM chunks c
            WHERE c.document_id=? AND c.chunk_id=?
            """,
            (document_id, chunk_id),
        ).fetchone()
    return _read_vector_record_at(vector_path, row) if row is not None else None


def load_document_records(
    index_path: str | Path,
    vector_path: str | Path,
    *,
    document_id: str,
) -> list[dict[str, Any]]:
    with _connect_readonly(Path(index_path)) as connection:
        rows = connection.execute(
            """
            SELECT c.*, 1.0 AS retrieval_score
            FROM chunks c
            WHERE c.document_id=?
            ORDER BY c.order_index
            """,
            (document_id,),
        ).fetchall()
    return [
        record
        for record in _read_vector_records_at(vector_path, rows)
        if record is not None
    ]


def load_document_article_records(
    index_path: str | Path,
    vector_path: str | Path,
    *,
    document_id: str,
) -> list[dict[str, Any]]:
    """Load only records eligible to govern appendix/form references.

    Governing-article resolution ignores records without both an article
    number and title. Selecting that exhaustive candidate set in SQLite avoids
    reading unrelated forms, tables, appendices, and navigation chunks.
    """

    with _connect_readonly(Path(index_path)) as connection:
        rows = connection.execute(
            """
            SELECT c.*, 1.0 AS retrieval_score
            FROM chunks c
            WHERE c.document_id=?
              AND trim(c.article_no)<>''
              AND trim(c.article_title)<>''
            ORDER BY c.order_index
            """,
            (document_id,),
        ).fetchall()
    return [
        record
        for record in _read_vector_records_at(vector_path, rows)
        if record is not None
    ]


def load_article_records(
    index_path: str | Path,
    vector_path: str | Path,
    *,
    regulation_unit_id: str,
    article_no: str,
    as_of_date: str | None = None,
) -> list[dict[str, Any]]:
    with _connect_readonly(Path(index_path)) as connection:
        version = _version_for_unit(connection, regulation_unit_id, as_of_date=as_of_date)
        if version is None:
            return []
        rows = connection.execute(
            """
            SELECT c.*, 1.0 AS retrieval_score
            FROM chunks c
            WHERE c.version_id=? AND replace(c.article_no, ' ', '')=replace(?, ' ', '')
            ORDER BY c.order_index
            """,
            (version["version_id"], article_no),
        ).fetchall()
    return [
        record
        for record in _read_vector_records_at(vector_path, rows)
        if record is not None
    ]


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in _QUERY_TOKEN_RE.findall(unicodedata.normalize("NFKC", str(query or "")).casefold()):
        if len(token) < 2:
            continue
        candidates = [token]
        for suffix in _KOREAN_QUERY_SUFFIXES:
            if token.endswith(suffix) and len(token) >= len(suffix) + 2:
                candidates.append(token[: -len(suffix)])
                break
        for candidate in candidates:
            if candidate not in terms:
                terms.append(candidate)
    return terms[:16]


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE regulation_versions(
            version_id TEXT PRIMARY KEY,
            unit_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            institution_name TEXT NOT NULL,
            regulation_no TEXT NOT NULL,
            title TEXT NOT NULL,
            source_version TEXT NOT NULL,
            revision_date TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT NOT NULL,
            repealed_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            is_current INTEGER NOT NULL,
            is_navigation INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE INDEX idx_regulation_versions_unit ON regulation_versions(unit_id, is_current);
        CREATE INDEX idx_regulation_versions_profile ON regulation_versions(profile_id, is_current);
        CREATE TABLE chunks(
            record_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            chunk_type TEXT NOT NULL,
            hierarchy_path TEXT NOT NULL,
            article_no TEXT NOT NULL,
            article_title TEXT NOT NULL,
            parent_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            vector_offset INTEGER NOT NULL,
            vector_length INTEGER NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_chunks_identity ON chunks(document_id, chunk_id);
        CREATE INDEX idx_chunks_version_order ON chunks(version_id, order_index);
        CREATE INDEX idx_chunks_article ON chunks(version_id, article_no);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            regulation_title,
            hierarchy_path,
            article_title,
            body,
            tokenize='unicode61'
        );
        CREATE TABLE toc_nodes(
            node_id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            parent_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            label TEXT NOT NULL,
            number TEXT NOT NULL,
            title TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            hierarchy_path TEXT NOT NULL,
            chunk_id TEXT NOT NULL
        );
        CREATE INDEX idx_toc_version_order ON toc_nodes(version_id, order_index);
        CREATE TABLE regulation_reference_edges(
            edge_id TEXT PRIMARY KEY,
            edge_type TEXT NOT NULL,
            status TEXT NOT NULL,
            source_unit_id TEXT NOT NULL,
            target_unit_id TEXT NOT NULL,
            source_article TEXT NOT NULL,
            requested_article TEXT NOT NULL,
            target_article TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX idx_regulation_reference_source
            ON regulation_reference_edges(source_unit_id, status, edge_id);
        CREATE INDEX idx_regulation_reference_target
            ON regulation_reference_edges(target_unit_id, status, edge_id);
        CREATE TABLE regulation_reference_edge_candidates(
            edge_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            PRIMARY KEY(edge_id, unit_id)
        );
        CREATE INDEX idx_regulation_reference_candidate_unit
            ON regulation_reference_edge_candidates(unit_id, edge_id);
        CREATE TABLE regulation_reference_cycles(
            cycle_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            size INTEGER NOT NULL,
            self_loop INTEGER NOT NULL,
            internal_unit_edge_count INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE regulation_reference_cycle_units(
            cycle_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            PRIMARY KEY(cycle_id, unit_id)
        );
        CREATE INDEX idx_regulation_reference_cycle_unit
            ON regulation_reference_cycle_units(unit_id, cycle_id);
        """
    )


def _current_reference_graph_records(
    prepared_records: Iterable[Mapping[str, Any]],
    finalized_versions: Mapping[str, Mapping[str, Any]],
    *,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Adapt current approved runtime records to the reference-graph contract."""

    graph_records: list[dict[str, Any]] = []
    for prepared in prepared_records:
        version_id = str(prepared.get("version_id") or "")
        version = finalized_versions.get(version_id)
        if (
            not isinstance(version, Mapping)
            or not bool(version.get("is_current"))
            or bool(version.get("is_navigation"))
            or str(version.get("status") or "").strip().casefold()
            not in _CURRENT_LIFECYCLE_STATUSES
        ):
            continue
        record = prepared.get("record")
        if not isinstance(record, Mapping):
            continue
        metadata = _metadata(record)
        title = str(version.get("title") or metadata.get("regulation_title") or "").strip()
        profile_id = str(version.get("profile_id") or metadata.get("profile_id") or "").strip()
        unit_id = str(prepared.get("unit_id") or version.get("unit_id") or "").strip()
        if not title or not profile_id or not unit_id:
            continue
        article_locators = _materialized_article_locators(metadata)
        graph_record = dict(record)
        graph_record.update(
            {
                "tenant_id": str(tenant_id),
                "profile_id": profile_id,
                "unit_id": unit_id,
                "title": title,
                "regulation_no": str(version.get("regulation_no") or ""),
                "version": str(version.get("version_label") or ""),
                "effective_from": str(version.get("effective_from") or ""),
                "effective_to": str(version.get("effective_to") or ""),
                "repealed_at": str(version.get("repealed_at") or ""),
                "approval_status": "approved",
                "article_locator": article_locators[0] if article_locators else "",
                "article_locators": article_locators,
            }
        )
        graph_records.append(graph_record)
    return graph_records


def _materialized_article_locators(metadata: Mapping[str, Any]) -> list[str]:
    """Return article/paragraph/item locators actually represented by a record."""

    from app.retrieval.regulation_reference_graph import (
        canonicalize_article_locator,
    )

    base = canonicalize_article_locator(metadata.get("article_no"))
    if not isinstance(base, Mapping):
        return []
    article = str(base.get("article") or "").strip()
    if not article:
        return []

    locators: list[str] = []
    seen: set[str] = set()

    def append_locator(
        paragraph: str = "",
        item: str = "",
        subitem: str = "",
    ) -> None:
        candidate = f"{article}{paragraph}{item}{subitem}"
        canonical = canonicalize_article_locator(candidate)
        locator = str(canonical.get("locator") or "") if isinstance(canonical, Mapping) else ""
        if locator and locator not in seen:
            seen.add(locator)
            locators.append(locator)

    append_locator()
    paragraph = _structural_child_marker(metadata.get("paragraph_no"), "paragraph")
    item = _structural_child_marker(metadata.get("item_no"), "item")
    subitem = _structural_child_marker(metadata.get("subitem_no"), "subitem")
    if paragraph or item or subitem:
        append_locator(paragraph, item, subitem)

    sample = metadata.get("paragraph_item_unit_sample")
    if not isinstance(sample, (list, tuple)):
        return locators

    current_paragraph = ""
    current_item = ""
    current_subitem = ""
    for raw_child in sample:
        if not isinstance(raw_child, Mapping):
            continue
        node_type = str(raw_child.get("node_type") or "").strip().casefold()
        marker = _structural_child_marker(raw_child.get("number"), node_type)
        if not marker:
            continue
        if node_type == "paragraph":
            current_paragraph = marker
            current_item = ""
            current_subitem = ""
        elif node_type == "item":
            current_item = marker
            current_subitem = ""
        elif node_type == "subitem":
            current_subitem = marker
        else:
            continue
        append_locator(current_paragraph, current_item, current_subitem)
    return locators


def _structural_child_marker(value: object, node_type: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return ""
    normalized_type = str(node_type or "").strip().casefold()
    if normalized_type not in {"paragraph", "item", "subitem"}:
        return ""

    number_match = re.search(r"\d+", text)
    number: int | None = int(number_match.group(0)) if number_match else None
    if number is None:
        for character in text:
            try:
                numeric = unicodedata.numeric(character)
            except (TypeError, ValueError):
                continue
            if float(numeric).is_integer() and numeric > 0:
                number = int(numeric)
                break

    if normalized_type == "paragraph":
        return f"제{number}항" if number is not None else ""
    if normalized_type == "item":
        return f"제{number}호" if number is not None else ""
    if number is not None:
        return f"제{number}목"

    name_match = re.search(r"([가-힣]+)\s*(?:목|[.)])?\s*$", text)
    if name_match is None:
        return ""
    return f"{name_match.group(1)}목"


def _store_reference_graph(
    connection: sqlite3.Connection,
    graph: Mapping[str, Any],
) -> None:
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    edge_rows: list[tuple[Any, ...]] = []
    candidate_rows: list[tuple[str, str]] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source_unit = edge.get("source_unit") if isinstance(edge.get("source_unit"), Mapping) else {}
        target_unit = edge.get("target_unit") if isinstance(edge.get("target_unit"), Mapping) else {}
        source_article = edge.get("source_article") if isinstance(edge.get("source_article"), Mapping) else {}
        requested_article = (
            edge.get("requested_article")
            if isinstance(edge.get("requested_article"), Mapping)
            else {}
        )
        target_article = edge.get("target_article") if isinstance(edge.get("target_article"), Mapping) else {}
        edge_id = str(edge.get("edge_id") or "")
        edge_rows.append(
            (
                edge_id,
                str(edge.get("edge_type") or ""),
                str(edge.get("status") or ""),
                str(source_unit.get("unit_id") or ""),
                str(target_unit.get("unit_id") or ""),
                str(source_article.get("locator") or ""),
                str(requested_article.get("locator") or ""),
                str(target_article.get("locator") or ""),
                json.dumps(edge, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
        candidates = (
            edge.get("candidate_units")
            if isinstance(edge.get("candidate_units"), list)
            else []
        )
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            candidate_unit_id = str(candidate.get("unit_id") or "").strip()
            if edge_id and candidate_unit_id:
                candidate_rows.append((edge_id, candidate_unit_id))
    connection.executemany(
        """
        INSERT INTO regulation_reference_edges(
            edge_id, edge_type, status, source_unit_id, target_unit_id,
            source_article, requested_article, target_article, payload_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        edge_rows,
    )
    connection.executemany(
        """
        INSERT INTO regulation_reference_edge_candidates(edge_id, unit_id)
        VALUES(?, ?)
        """,
        candidate_rows,
    )

    cycles = graph.get("cycles") if isinstance(graph.get("cycles"), list) else []
    cycle_rows: list[tuple[Any, ...]] = []
    membership_rows: list[tuple[str, str]] = []
    for cycle in cycles:
        if not isinstance(cycle, Mapping):
            continue
        cycle_id = str(cycle.get("cycle_id") or "")
        cycle_rows.append(
            (
                cycle_id,
                str(cycle.get("profile_id") or ""),
                int(cycle.get("size") or 0),
                int(bool(cycle.get("self_loop"))),
                int(cycle.get("internal_unit_edge_count") or 0),
                json.dumps(cycle, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
        for unit_id in cycle.get("unit_ids") or []:
            normalized_unit_id = str(unit_id or "").strip()
            if normalized_unit_id:
                membership_rows.append((cycle_id, normalized_unit_id))
    connection.executemany(
        """
        INSERT INTO regulation_reference_cycles(
            cycle_id, profile_id, size, self_loop,
            internal_unit_edge_count, payload_json
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        cycle_rows,
    )
    connection.executemany(
        "INSERT INTO regulation_reference_cycle_units(cycle_id, unit_id) VALUES(?, ?)",
        membership_rows,
    )


def _add_runtime_record_to_version_groups(
    groups: dict[tuple[str, str], dict[str, Any]],
    *,
    record: Mapping[str, Any],
    identity: Mapping[str, Any],
    scoped_profile_id: str,
) -> tuple[str, str]:
    metadata = _metadata(record)
    document_id, _ = _record_identity(record)
    title = str(
        identity.get("title")
        or metadata.get("regulation_title")
        or metadata.get("document_name")
        or ""
    ).strip()
    regulation_no = str(
        identity["regulation_no"]
        if identity
        else metadata.get("regulation_no") or ""
    ).strip()
    record_profile = str(
        identity.get("profile_id")
        or metadata.get("profile_id")
        or scoped_profile_id
        or ""
    ).strip()
    unit_id = str(
        identity.get("unit_id")
        or regulation_unit_id_for(
            profile_id=record_profile,
            regulation_title=title,
            regulation_no=regulation_no,
        )
    )
    version_id = _version_id(unit_id, document_id)
    revision_date = _latest_date(
        metadata.get("revision_date"),
        metadata.get("effective_date"),
        metadata.get("valid_from"),
    )
    effective_from = _first_date(
        metadata.get("effective_from"),
        metadata.get("valid_from"),
    )
    effective_to = _first_date(
        metadata.get("effective_to"),
        metadata.get("valid_to"),
    )
    repealed_at = _first_date(metadata.get("repealed_at"))
    group = groups.setdefault(
        (unit_id, document_id),
        {
            "version_id": version_id,
            "unit_id": unit_id,
            "document_id": document_id,
            "profile_id": record_profile,
            "institution_name": str(metadata.get("institution_name") or ""),
            "regulation_no": regulation_no,
            "title": title,
            "source_version": str(metadata.get("regulation_version") or ""),
            "revision_dates": [],
            "effective_dates": [],
            "effective_to_dates": [],
            "repealed_at_dates": [],
            "status": str(metadata.get("regulation_status") or "approved"),
            "content_hashes": [],
            "logical_chunk_hashes": [],
            "search_values": [],
            "chunk_count": 0,
            "is_navigation": int(_is_navigation_unit(title, regulation_no)),
        },
    )
    if revision_date:
        group["revision_dates"].append(revision_date)
    if effective_from:
        group["effective_dates"].append(effective_from)
    if effective_to:
        group["effective_to_dates"].append(effective_to)
    if repealed_at:
        group["repealed_at_dates"].append(repealed_at)
    group["chunk_count"] += 1
    group["content_hashes"].append(str(record.get("content_hash") or ""))
    group["logical_chunk_hashes"].append(_logical_record_hash(record))
    group["search_values"].extend(
        str(value or "")
        for value in (
            metadata.get("regulation_title"),
            metadata.get("regulation_no"),
            metadata.get("part_title"),
            metadata.get("chapter_title"),
            metadata.get("section_title"),
            metadata.get("article_no"),
            metadata.get("article_title"),
            metadata.get("hierarchy_path"),
        )
        if str(value or "").strip()
    )
    return unit_id, version_id


def _finalize_versions(groups: dict[tuple[str, str], dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in groups.values():
        revision_date = max(group["revision_dates"], default="")
        # Use the actual effective date when present; fall back to the revision
        # date only when no effective date exists.  max(effective, revision)
        # would inflate a retroactive amendment's effective_from up to its later
        # revision date, hiding the version from point-in-time queries between
        # the two dates.
        effective_from = max(group["effective_dates"], default="") or revision_date
        source_version = str(group["source_version"] or "")
        version_label = f"rev-{revision_date.replace('-', '')}" if revision_date else source_version
        search_text = " ".join(dict.fromkeys(value.strip() for value in group["search_values"] if value.strip()))
        item = {
            **group,
            "revision_date": revision_date,
            "effective_from": effective_from,
            "effective_to": min(group.get("effective_to_dates") or (), default=""),
            "repealed_at": min(group.get("repealed_at_dates") or (), default=""),
            "version_label": version_label,
            "is_current": 0,
            "content_hash": _aggregate_hash(group["content_hashes"]),
            "logical_content_hash": _aggregate_hash(group["logical_chunk_hashes"]),
            "search_text": search_text[:250_000],
        }
        finalized[item["version_id"]] = item
        by_unit[item["unit_id"]].append(item)
    for versions in by_unit.values():
        versions.sort(key=_lifecycle_version_sort_key)
        lifecycle_versions = [
            item
            for item in versions
            if str(item.get("status") or "").strip().casefold()
            in _CURRENT_LIFECYCLE_STATUSES
        ]
        for item in versions:
            item["is_current"] = 0
        for index, item in enumerate(lifecycle_versions):
            if index + 1 < len(lifecycle_versions):
                next_version = lifecycle_versions[index + 1]
                next_start = _parse_date(
                    next_version["effective_from"] or next_version["revision_date"]
                )
                if next_start is not None:
                    derived_end = (next_start - timedelta(days=1)).isoformat()
                    explicit_end = str(item.get("effective_to") or "")
                    if not explicit_end or derived_end < explicit_end:
                        item["effective_to"] = derived_end
        today = date.today()
        current_candidates = [
            item
            for item in lifecycle_versions
            if _version_is_effective_on(item, today)
            and not (
                str(item.get("status") or "").strip().casefold() == "superseded"
                and not str(item.get("effective_to") or "")
            )
        ]
        if current_candidates:
            max(current_candidates, key=_lifecycle_version_sort_key)["is_current"] = 1
    return finalized


def _selected_version_rows(
    connection: sqlite3.Connection,
    *,
    profile_id: str | None,
    document_id: str | None,
    as_of_date: str | None,
    allowed_unit_ids: set[str] | None,
) -> list[sqlite3.Row]:
    if allowed_unit_ids is not None and not allowed_unit_ids:
        return []
    selection_date = as_of_date or _default_as_of_date()
    lifecycle_statuses = (
        _HISTORICAL_LIFECYCLE_STATUSES
        if as_of_date
        else _CURRENT_LIFECYCLE_STATUSES
    )
    clauses = [
        "v.is_navigation=0",
        "lower(v.status) IN ("
        + ",".join("?" for _ in lifecycle_statuses)
        + ")",
        "(v.effective_from='' OR v.effective_from<=?)",
        "(v.effective_to='' OR v.effective_to>=?)",
    ]
    params: list[Any] = [*lifecycle_statuses, selection_date, selection_date]
    if _regulation_versions_has_repealed_at(connection):
        clauses.append("(v.repealed_at='' OR ?<v.repealed_at)")
        params.append(selection_date)
    if not as_of_date:
        clauses.append(
            "NOT (lower(v.status)='superseded' AND v.effective_to='')"
        )
    if profile_id:
        clauses.append("lower(v.profile_id)=lower(?)")
        params.append(profile_id)
    if allowed_unit_ids is not None:
        ordered_unit_ids = sorted(allowed_unit_ids)
        clauses.append(
            "v.unit_id IN (" + ",".join("?" for _ in ordered_unit_ids) + ")"
        )
        params.extend(ordered_unit_ids)
    outer_clauses = ["runtime_rank=1"]
    if document_id:
        outer_clauses.append("document_id=?")
        params.append(document_id)
    return connection.execute(
        f"""
        WITH candidates AS (
            SELECT v.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY v.unit_id
                       ORDER BY COALESCE(NULLIF(v.effective_from, ''), v.revision_date) DESC,
                                CASE lower(v.status) WHEN 'approved' THEN 1 ELSE 0 END DESC,
                                v.revision_date DESC, v.source_version DESC, v.version_id DESC
                   ) AS runtime_rank
            FROM regulation_versions v
            WHERE {' AND '.join(clauses)}
        )
        SELECT candidates.*,
               (
                   SELECT COUNT(*)
                   FROM regulation_versions history
                   WHERE history.unit_id=candidates.unit_id
               ) AS version_count
        FROM candidates
        WHERE {' AND '.join(outer_clauses)}
        """,
        params,
    ).fetchall()


def _runtime_current_version_ids(
    connection: sqlite3.Connection,
    *,
    profile_id: str | None,
    unit_ids: set[str],
) -> set[str]:
    if not unit_ids:
        return set()
    return {
        str(row["version_id"])
        for row in _selected_version_rows(
            connection,
            profile_id=profile_id,
            document_id=None,
            as_of_date=None,
            allowed_unit_ids=unit_ids,
        )
    }


def _rank_versions(
    query: str,
    terms: list[str],
    versions: Iterable[sqlite3.Row],
) -> list[tuple[float, sqlite3.Row]]:
    compact_query = _compact(query)
    ranked: list[tuple[float, sqlite3.Row]] = []
    for row in versions:
        title = _compact(row["title"])
        regulation_no = _compact(row["regulation_no"])
        search_text = _compact(row["search_text"])
        score = 0.0
        if compact_query and title and (title in compact_query or compact_query in title):
            score += 100.0
        if regulation_no and regulation_no in compact_query:
            score += 80.0
        for term in terms:
            compact_term = _compact(term)
            if not compact_term:
                continue
            if compact_term in title:
                score += 24.0
            elif compact_term in regulation_no:
                score += 18.0
            elif compact_term in search_text:
                score += 4.0
        ranked.append((score, row))
    return sorted(
        ranked,
        key=lambda item: (
            item[0],
            str(item[1]["revision_date"] or ""),
            str(item[1]["title"] or ""),
        ),
        reverse=True,
    )


def _search_chunk_rows(
    connection: sqlite3.Connection,
    *,
    query: str,
    terms: list[str],
    version_ids: list[str],
    limit: int,
) -> list[sqlite3.Row]:
    if not version_ids:
        return []
    placeholders = ",".join("?" for _ in version_ids)
    fts_terms: list[str] = []
    for term in terms:
        normalized_term = unicodedata.normalize("NFKC", str(term or "")).casefold()
        if not normalized_term:
            continue
        escaped_term = normalized_term.replace('"', '""')
        if _FTS_PREFIX_ARTICLE_LOCATOR_RE.fullmatch(normalized_term):
            fts_terms.append(f'"{escaped_term}"*')
        else:
            fts_terms.append(f'"{escaped_term}"')
    if fts_terms:
        match_query = " OR ".join(fts_terms)
        rows = connection.execute(
            f"""
            SELECT c.*, (1.0 - (1.0 / (1.0 + abs(bm25(chunks_fts, 8.0, 4.0, 6.0, 1.0))))) AS retrieval_score
            FROM chunks_fts
            JOIN chunks c ON c.rowid=chunks_fts.rowid
            WHERE chunks_fts MATCH ? AND c.version_id IN ({placeholders})
            ORDER BY bm25(chunks_fts, 8.0, 4.0, 6.0, 1.0)
            LIMIT ?
            """,
            [match_query, *version_ids, limit],
        ).fetchall()
        if rows:
            return rows
    like_terms = terms or [str(query or "").strip()]
    score_parts: list[str] = []
    term_params: list[Any] = []
    for term in like_terms[:8]:
        pattern = f"%{term}%"
        score_parts.append(
            "(CASE WHEN c.article_title LIKE ? THEN 8 ELSE 0 END + "
            "CASE WHEN c.hierarchy_path LIKE ? THEN 4 ELSE 0 END + "
            "CASE WHEN f.body LIKE ? THEN 1 ELSE 0 END)"
        )
        term_params.extend([pattern, pattern, pattern])
    score_expression = " + ".join(score_parts) or "0"
    return connection.execute(
        f"""
        SELECT c.*, ({score_expression}) AS retrieval_score
        FROM chunks c
        JOIN chunks_fts f ON f.rowid=c.rowid
        WHERE c.version_id IN ({placeholders}) AND ({score_expression}) > 0
        ORDER BY retrieval_score DESC, c.order_index
        LIMIT ?
        """,
        [*term_params, *version_ids, *term_params, limit],
    ).fetchall()


def _version_for_unit(
    connection: sqlite3.Connection,
    regulation_unit_id: str,
    *,
    as_of_date: str | None,
) -> sqlite3.Row | None:
    selection_date = as_of_date or _default_as_of_date()
    lifecycle_statuses = (
        _HISTORICAL_LIFECYCLE_STATUSES
        if as_of_date
        else _CURRENT_LIFECYCLE_STATUSES
    )
    clauses = [
        "unit_id=?",
        "lower(status) IN ("
        + ",".join("?" for _ in lifecycle_statuses)
        + ")",
        "(effective_from='' OR effective_from<=?)",
        "(effective_to='' OR effective_to>=?)",
    ]
    params: list[Any] = [
        regulation_unit_id,
        *lifecycle_statuses,
        selection_date,
        selection_date,
    ]
    if _regulation_versions_has_repealed_at(connection):
        clauses.append("(repealed_at='' OR ?<repealed_at)")
        params.append(selection_date)
    if not as_of_date:
        clauses.append(
            "NOT (lower(status)='superseded' AND effective_to='')"
        )
    return connection.execute(
        f"""
        SELECT * FROM regulation_versions
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(NULLIF(effective_from, ''), revision_date) DESC,
                 CASE lower(status) WHEN 'approved' THEN 1 ELSE 0 END DESC,
                 revision_date DESC, source_version DESC, version_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _read_vector_record_at(
    vector_path: str | Path,
    row: Mapping[str, Any] | sqlite3.Row,
) -> dict[str, Any] | None:
    """Load one verified vector record while preserving the public helper contract."""

    records = _read_vector_records_at(vector_path, [row])
    return records[0] if records else None


def _read_vector_records_at(
    vector_path: str | Path,
    rows: Iterable[Mapping[str, Any] | sqlite3.Row],
) -> list[dict[str, Any] | None]:
    """Load offset-addressed records through one binary handle.

    The result list stays aligned with ``rows``. Any invalid row is rejected
    independently so one corrupt offset or payload cannot suppress valid
    neighbors.
    """

    row_list = list(rows)
    if not row_list:
        return []
    try:
        with Path(vector_path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            vector_size = handle.tell()
            return [
                _read_vector_record_from_handle(handle, row, vector_size=vector_size)
                for row in row_list
            ]
    except OSError:
        return [None] * len(row_list)


def _read_vector_record_from_handle(
    handle: BinaryIO,
    row: Mapping[str, Any] | sqlite3.Row,
    *,
    vector_size: int,
) -> dict[str, Any] | None:
    try:
        offset = int(row["vector_offset"])
        length = int(row["vector_length"])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None
    if offset < 0 or length <= 0:
        return None
    if offset > vector_size or length > vector_size - offset:
        return None
    try:
        handle.seek(offset)
        payload = handle.read(length)
        if len(payload) != length:
            return None
        record = json.loads(payload.decode("utf-8"))
    except (
        OSError,
        ValueError,
        OverflowError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    if not isinstance(record, dict):
        return None
    document_id, chunk_id = _record_identity(record)
    try:
        expected_document_id = str(row["document_id"])
        expected_chunk_id = str(row["chunk_id"])
        expected_content_hash = str(row["content_hash"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if document_id != expected_document_id or chunk_id != expected_chunk_id:
        return None
    metadata = _metadata(record)
    content_hash = str(record.get("content_hash") or "")
    if content_hash != expected_content_hash:
        return None
    if stable_content_hash(str(record.get("text") or ""), metadata) != content_hash:
        return None
    return record


def _toc_rows_for_record(
    record: dict[str, Any],
    *,
    version_id: str,
    unit_id: str,
    order_index: int,
) -> list[tuple[Any, ...]]:
    metadata = _metadata(record)
    hierarchy_path = str(metadata.get("hierarchy_path") or "")
    segments = [segment.strip() for segment in hierarchy_path.split(">") if segment.strip()]
    title = str(metadata.get("regulation_title") or "").strip()
    regulation_no = str(metadata.get("regulation_no") or "").strip()
    matched_start_index = next(
        (
            index
            for index, segment in enumerate(segments)
            if (title and normalize_regulation_title(title) in normalize_regulation_title(segment))
            or (regulation_no and _compact(regulation_no) in _compact(segment))
        ),
        None,
    )
    if matched_start_index is not None:
        selected = segments[matched_start_index:]
    else:
        # Some parsers emit only "제1장 > 제1조" and omit the regulation label
        # from hierarchy_path. Preserve every structural ancestor and synthesize
        # the regulation root instead of keeping only the leaf article.
        structural_start_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if _is_structural_toc_segment(segment)
            ),
            max(0, len(segments) - 1),
        )
        tail = segments[structural_start_index:]
        root_label = title or regulation_no or unit_id
        selected = [root_label, *tail] if root_label else tail
    article_label = " ".join(
        value
        for value in (
            str(metadata.get("article_no") or "").strip(),
            str(metadata.get("article_title") or "").strip(),
        )
        if value
    )
    if article_label and all(_compact(article_label) != _compact(segment) for segment in selected):
        selected.append(article_label)
    if not selected:
        selected = [title or regulation_no or unit_id]
    rows: list[tuple[Any, ...]] = []
    parent_id = ""
    path_parts: list[str] = []
    _, chunk_id = _record_identity(record)
    for depth, segment in enumerate(selected):
        path_parts.append(segment)
        path = " > ".join(path_parts)
        node_id = "toc-" + hashlib.sha256(f"{version_id}\n{path}".encode("utf-8")).hexdigest()[:24]
        number, node_title = _split_toc_label(segment)
        leaf_chunk_type = (
            str(metadata.get("chunk_type") or "")
            if depth == len(selected) - 1
            else ""
        )
        rows.append(
            (
                node_id,
                version_id,
                unit_id,
                parent_id,
                _toc_node_type(segment, depth, chunk_type=leaf_chunk_type),
                segment,
                number,
                node_title,
                order_index * 10 + depth,
                path,
                chunk_id if depth == len(selected) - 1 else "",
            )
        )
        parent_id = node_id
    return rows


def _is_structural_toc_segment(label: object) -> bool:
    marker = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(label or "")))
    return bool(
        re.match(r"^제?\d+(?:의\d+)?(?:편|장|절|관|조|항|호)", marker)
        or re.match(r"^(?:부칙|별표|별지|서식)", marker)
        or re.match(r"^(?:\(\d+\)|[①-⑳]|[가-힣]목)", marker)
    )


def _split_toc_label(label: str) -> tuple[str, str]:
    patterns = (
        r"^\s*((?:제\s*)?\d+(?:-\d+)*(?:조(?:의\s*\d+)?|장|절|관|편|항|호))[\s.:\-]*(.*)$",
        r"^\s*([가-힣]\s*목)[\s.:\-]*(.*)$",
        r"^\s*(\(\s*\d+\s*\)|[①-⑳])[\s.:\-]*(.*)$",
        r"^\s*(\d+[.)]|[가-힣][.)])\s*(.*)$",
        r"^\s*((?:제\s*)?\d+(?:-\d+)*(?:조(?:의\s*\d+)?)?)[\s.:\-]*(.*)$",
    )
    for pattern in patterns:
        match = re.match(pattern, label)
        if match:
            return match.group(1).strip(), match.group(2).strip()
    return "", label.strip()


def _toc_node_type(label: str, depth: int, *, chunk_type: str = "") -> str:
    compact = _compact(label)
    marker = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(label or "")))
    if depth == 0:
        return "regulation"
    if "\ubd80\uce59" in compact:
        return "supplementary"
    if "\ubcc4\ud45c" in compact:
        return "appendix"
    if "\ubcc4\uc9c0" in compact or "\uc11c\uc2dd" in compact:
        return "form"
    if re.search(r"\uc81c\d+\uc7a5", compact):
        return "chapter"
    if re.search(r"\uc81c\d+\uc808", compact):
        return "section"
    if _ARTICLE_RE.match(label):
        return "article"
    normalized_chunk_type = str(chunk_type or "").strip().casefold()
    if normalized_chunk_type == "article":
        return "article"
    if normalized_chunk_type in {"paragraph", "clause"}:
        return "paragraph"
    if normalized_chunk_type == "item":
        return "item"
    if normalized_chunk_type == "subitem":
        return "subitem"
    if re.match(r"^(?:제\d+항|\(\d+\)|[①-⑳])", marker):
        return "paragraph"
    if re.match(r"^(?:제\d+호|\d+[.)])", marker):
        return "item"
    if re.match(r"^(?:[가-힣]목|[가-힣][.)])", marker):
        return "subitem"
    return "section"


def _public_regulation_row(
    row: Mapping[str, Any] | sqlite3.Row,
    *,
    is_current: bool | None = None,
) -> dict[str, Any]:
    keys = set(row.keys()) if hasattr(row, "keys") else set(row)
    return {
        "regulation_unit_id": str(row["unit_id"]),
        "version_id": str(row["version_id"]),
        "document_id": str(row["document_id"]),
        "profile_id": str(row["profile_id"]),
        "institution_name": str(row["institution_name"]),
        "regulation_no": str(row["regulation_no"]),
        "regulation_title": str(row["title"]),
        "version": str(row["source_version"]),
        "revision_date": str(row["revision_date"]),
        "effective_from": str(row["effective_from"]),
        "effective_to": str(row["effective_to"]),
        "repealed_at": (
            str(row["repealed_at"] or "")
            if "repealed_at" in keys
            else ""
        ),
        "status": str(row["status"]),
        "is_current": bool(row["is_current"]) if is_current is None else is_current,
        "chunk_count": int(row["chunk_count"]),
        "version_count": int(row["version_count"]) if "version_count" in keys else 1,
    }


@contextmanager
def _connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _regulation_versions_has_repealed_at(connection: sqlite3.Connection) -> bool:
    """Detect the additive lifecycle column without migrating legacy indexes."""

    return any(
        str(row[1]) == "repealed_at"
        for row in connection.execute("PRAGMA table_info(regulation_versions)")
    )


def _metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("metadata")
    return value if isinstance(value, dict) else {}


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str]:
    metadata = _metadata(record)
    return (
        str(record.get("document_id") or metadata.get("document_id") or ""),
        str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
    )


def _validated_runtime_record_scope(
    records: Iterable[Mapping[str, Any]],
    *,
    tenant_id: object,
    profile_id: object,
) -> tuple[str, str]:
    """Bind every build input to one exact tenant and profile scope."""

    expected_tenant_id = str(tenant_id or "").strip()
    expected_profile_id = str(profile_id or "").strip()
    if not expected_tenant_id:
        raise ValueError("tenant_id must be a non-empty build scope")

    observed_profile_ids: set[str] = set()
    record_count = 0
    for record_index, record in enumerate(records, start=1):
        record_count += 1
        if not isinstance(record, Mapping):
            raise ValueError(f"record {record_index} must be a mapping")
        record_tenant_id = _record_scope_identifier(
            record,
            field="tenant_id",
            record_index=record_index,
        )
        if not record_tenant_id:
            raise ValueError(f"record {record_index} tenant_id is required")
        if record_tenant_id != expected_tenant_id:
            raise ValueError(
                f"record {record_index} tenant_id does not match build tenant_id"
            )

        record_profile_id = _record_scope_identifier(
            record,
            field="profile_id",
            record_index=record_index,
        )
        if not record_profile_id:
            raise ValueError(f"record {record_index} profile_id is required")
        if expected_profile_id and record_profile_id != expected_profile_id:
            raise ValueError(
                f"record {record_index} profile_id does not match build profile_id"
            )
        observed_profile_ids.add(record_profile_id)

    if expected_profile_id:
        return expected_tenant_id, expected_profile_id
    if record_count == 0 or len(observed_profile_ids) != 1:
        raise ValueError(
            "records must belong to exactly one non-empty profile_id "
            "when build profile_id is omitted"
        )
    return expected_tenant_id, next(iter(observed_profile_ids))


def _record_scope_identifier(
    record: Mapping[str, Any],
    *,
    field: str,
    record_index: int,
) -> str:
    metadata = _metadata(record)
    values = {
        normalized
        for candidate in (record.get(field), metadata.get(field))
        if (normalized := str(candidate or "").strip())
    }
    if len(values) > 1:
        raise ValueError(f"record {record_index} has conflicting {field} values")
    return next(iter(values), "")


def _version_id(unit_id: str, document_id: str) -> str:
    digest = hashlib.sha256(f"{unit_id}\n{document_id}".encode("utf-8")).hexdigest()[:20]
    return f"regver-{digest}"


def _is_navigation_unit(title: str, regulation_no: str) -> bool:
    values = {_compact(title), _compact(regulation_no)}
    return bool(values.intersection({"\ubaa9\ucc28", "\ucc28\ub840", "tableofcontents"}))


def _first_date(*values: object) -> str:
    for value in values:
        normalized = _normalize_date(value)
        if normalized:
            return normalized
    return ""


def _latest_date(*values: object) -> str:
    dates: list[str] = []
    for value in values:
        if isinstance(value, list):
            dates.extend(normalized for item in value if (normalized := _normalize_date(item)))
        elif normalized := _normalize_date(value):
            dates.append(normalized)
    return max(dates, default="")


def _normalize_date(value: object) -> str:
    text = str(value or "").strip()
    match = _DATE_RE.search(text)
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def _parse_date(value: object) -> date | None:
    normalized = _normalize_date(value)
    try:
        return date.fromisoformat(normalized) if normalized else None
    except ValueError:
        return None


def _version_sort_key(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("revision_date") or ""),
        str(item.get("effective_from") or ""),
        str(item.get("version_label") or ""),
        str(item.get("logical_content_hash") or item.get("document_id") or ""),
    )


def _lifecycle_version_sort_key(item: Mapping[str, Any]) -> tuple[str, int, str, str, str]:
    status = str(item.get("status") or "").strip().casefold()
    return (
        str(item.get("effective_from") or item.get("revision_date") or ""),
        1 if status == "approved" else 0,
        str(item.get("revision_date") or ""),
        str(item.get("version_label") or ""),
        str(item.get("logical_content_hash") or item.get("document_id") or ""),
    )


def _version_is_effective_on(item: Mapping[str, Any], reference_date: date) -> bool:
    effective_from = _parse_date(item.get("effective_from"))
    effective_to = _parse_date(item.get("effective_to"))
    repealed_at = _parse_date(item.get("repealed_at"))
    return (
        (effective_from is None or effective_from <= reference_date)
        and (effective_to is None or reference_date <= effective_to)
        and (repealed_at is None or reference_date < repealed_at)
    )


def _runtime_record_sort_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    metadata = _metadata(record)
    return (
        _compact(metadata.get("profile_id")),
        normalize_regulation_title(metadata.get("regulation_title") or metadata.get("document_name")),
        _normalize_date(metadata.get("revision_date")),
        _normalize_date(metadata.get("effective_from") or metadata.get("valid_from")),
        _logical_text(metadata.get("hierarchy_path")),
        _logical_text(metadata.get("article_no")),
        _logical_text(metadata.get("paragraph_no")),
        _logical_text(metadata.get("item_no")),
        str(_integer(metadata.get("source_page_start"), 0)).zfill(8),
        _logical_text(metadata.get("chunk_type")),
        _logical_record_hash(record),
        str(_record_identity(record)[0]),
        str(_record_identity(record)[1]),
    )


def _logical_record_hash(record: Mapping[str, Any]) -> str:
    metadata = _metadata(record)
    stable_fields = (
        "regulation_no",
        "regulation_title",
        "regulation_version",
        "revision_date",
        "effective_from",
        "effective_to",
        "repealed_at",
        "valid_from",
        "valid_to",
        "chunk_type",
        "hierarchy_path",
        "part_no",
        "part_title",
        "chapter_no",
        "chapter_title",
        "section_no",
        "section_title",
        "article_no",
        "article_title",
        "paragraph_no",
        "paragraph_label",
        "item_no",
        "subitem_no",
        "structural_child_count_source",
        "paragraph_unit_count",
        "item_unit_count",
        "subitem_unit_count",
        "paragraph_item_unit_count",
        "paragraph_item_traceable_unit_count",
        "paragraph_item_unit_sample",
        "internal_regulation_refs",
        "regulation_article_refs",
        "source_page_start",
        "source_page_end",
    )
    payload = {
        "text": _logical_text(record.get("text")),
        "metadata": {field: _logical_value(metadata.get(field)) for field in stable_fields if metadata.get(field) is not None},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _logical_corpus_hash(versions: Mapping[str, Mapping[str, Any]]) -> str:
    payload = [
        {
            "profile_id": _compact(item.get("profile_id")),
            "regulation_title": normalize_regulation_title(item.get("title")),
            "regulation_no": _compact(item.get("regulation_no")),
            "version": _logical_text(item.get("version_label")),
            "revision_date": str(item.get("revision_date") or ""),
            "effective_from": str(item.get("effective_from") or ""),
            "effective_to": str(item.get("effective_to") or ""),
            "repealed_at": str(item.get("repealed_at") or ""),
            "status": _compact(item.get("status")),
            "chunk_count": int(item.get("chunk_count") or 0),
            "logical_content_sha256": str(item.get("logical_content_hash") or ""),
        }
        for item in versions.values()
        if not item.get("is_navigation")
    ]
    payload.sort(
        key=lambda item: (
            item["profile_id"],
            item["regulation_title"],
            item["revision_date"],
            item["version"],
            item["logical_content_sha256"],
        )
    )
    encoded = json.dumps(
        {"schema_version": REBUILD_FINGERPRINT_SCHEMA_VERSION, "versions": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _logical_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _logical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, set):
        return sorted((_logical_value(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_logical_value(item) for item in value]
    return _logical_text(value)


def _logical_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")).strip()


def _load_json_object(value: object) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _report_hierarchy_progress(
    callback: Callable[[int, str, int, int], None] | None,
    percent: int,
    message: str,
    current: int,
    total: int,
) -> None:
    if callback is not None:
        callback(max(0, min(100, int(percent))), message, max(0, int(current)), max(0, int(total)))


def _aggregate_hash(values: Iterable[object]) -> str:
    canonical = "\n".join(sorted(str(value or "") for value in values))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compact(value: object) -> str:
    return re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _integer(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
