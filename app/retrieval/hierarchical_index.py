from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, Callable, Iterable, Iterator, Mapping

from app.ingestion.vector_adapter import stable_content_hash


HIERARCHICAL_INDEX_SCHEMA_VERSION = "reg-rag-hierarchical-index-v1"
REBUILD_FINGERPRINT_SCHEMA_VERSION = "reg-rag-logical-corpus-v1"
HIERARCHICAL_INDEX_RELATIVE_PATH = Path("hierarchy") / "regulation_hierarchy.sqlite3"
_DATE_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
_QUERY_TOKEN_RE = re.compile(r"[0-9A-Za-z\uac00-\ud7a3]+")
_ARTICLE_RE = re.compile(r"^\s*(\uc81c\s*\d+\s*\uc870(?:\uc758\s*\d+)?)")
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


def hierarchical_index_path(data_dir: str | Path) -> Path:
    """Return the conventional institution hierarchy index path."""
    return Path(data_dir) / HIERARCHICAL_INDEX_RELATIVE_PATH


def normalize_regulation_title(value: object) -> str:
    """Normalize a regulation title for stable institution-local identity."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", text)
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
    normalized_no = _compact(regulation_no)
    identity = normalized_title or normalized_no or "unknown-regulation"
    digest = hashlib.sha256(f"{normalized_profile}\n{identity}".encode("utf-8")).hexdigest()[:20]
    return f"regunit-{digest}"


def canonicalize_runtime_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return records in a stable logical order independent of upload order."""
    return sorted(records, key=_runtime_record_sort_key)


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
    records: list[dict[str, Any]],
    *,
    tenant_id: str,
    profile_id: str | None,
    vector_offsets: Mapping[tuple[str, str], tuple[int, int]] | None = None,
    progress_callback: Callable[[int, str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Build a regulation catalog, TOC, version, and body-search index."""
    records = canonicalize_runtime_records(records)
    total_records = len(records)
    progress_step = max(1, total_records // 100)
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
                ("tenant_id", str(tenant_id)),
                ("profile_id", str(profile_id or "")),
                ("record_count", str(len(records))),
            ],
        )

        version_groups: dict[tuple[str, str], dict[str, Any]] = {}
        prepared_records: list[dict[str, Any]] = []
        for fallback_order, record in enumerate(records, start=1):
            metadata = _metadata(record)
            document_id, chunk_id = _record_identity(record)
            title = str(metadata.get("regulation_title") or metadata.get("document_name") or "").strip()
            regulation_no = str(metadata.get("regulation_no") or "").strip()
            record_profile = str(metadata.get("profile_id") or profile_id or "").strip()
            unit_id = regulation_unit_id_for(
                profile_id=record_profile,
                regulation_title=title,
                regulation_no=regulation_no,
            )
            version_id = _version_id(unit_id, document_id)
            revision_date = _latest_date(
                metadata.get("revision_date"),
                metadata.get("effective_date"),
                metadata.get("valid_from"),
            )
            effective_from = _first_date(metadata.get("effective_from"), metadata.get("valid_from"))
            group = version_groups.setdefault(
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
                effective_to, status, is_current, is_navigation, chunk_count,
                content_hash, search_text
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        "logical_corpus_sha256": _logical_corpus_hash(finalized_versions),
        "path": str(output_path),
        "sha256": _sha256_file(output_path),
        "record_count": len(records),
        "regulation_count": unit_count,
        "current_regulation_count": current_count,
        "regulation_version_count": version_count,
        "toc_node_count": len(toc_rows),
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
    return {
        "schema_version": metadata.get("schema_version"),
        "tenant_id": metadata.get("tenant_id"),
        "profile_id": metadata.get("profile_id"),
        "record_count": _integer(metadata.get("record_count"), 0),
        "regulation_count": int(regulation_count),
        "current_regulation_count": int(current_count),
        "regulation_version_count": int(version_count),
        "toc_node_count": int(toc_count),
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
) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    """Search catalog and TOC first, then retrieve body evidence by offset."""
    path = Path(index_path)
    terms = query_terms(query)
    with _connect_readonly(path) as connection:
        versions = _selected_version_rows(
            connection,
            profile_id=profile_id,
            document_id=document_id,
            as_of_date=as_of_date,
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
    results: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        record = _read_vector_record_at(vector_path, row)
        if record is None:
            continue
        identity = _record_identity(record)
        if identity in seen:
            continue
        seen.add(identity)
        lexical_score = float(row["retrieval_score"])
        catalog_score = min(version_scores.get(str(row["version_id"]), 0.0), 100.0) / 100.0
        results.append((round(lexical_score + catalog_score, 8), record))
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
            "document_id": str(row["document_id"] or ""),
            "catalog_score": round(float(score), 4),
        }
        for score, row in selected[:16]
    ]
    return results, {
        "retrieval_model": "institution-hierarchical-sqlite-fts-v1",
        "retrieval_strategy": "catalog_toc_body",
        "retrieval_fallback": False,
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
        clauses = ["v.is_navigation=0"]
        params: list[Any] = []
        if not include_history:
            clauses.append("v.is_current=1")
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
            [*params, max(1, min(int(limit), 1000))],
        ).fetchall()
    items = [_public_regulation_row(row) for row in rows]
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
    return {"regulation": _public_regulation_row(version), "nodes": nodes}


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
    return [record for row in rows if (record := _read_vector_record_at(vector_path, row)) is not None]


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
        """
    )


def _finalize_versions(groups: dict[tuple[str, str], dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in groups.values():
        revision_date = max(group["revision_dates"], default="")
        effective_from = max(
            value
            for value in (max(group["effective_dates"], default=""), revision_date)
            if value
        ) if (group["effective_dates"] or revision_date) else ""
        source_version = str(group["source_version"] or "")
        version_label = f"rev-{revision_date.replace('-', '')}" if revision_date else source_version
        search_text = " ".join(dict.fromkeys(value.strip() for value in group["search_values"] if value.strip()))
        item = {
            **group,
            "revision_date": revision_date,
            "effective_from": effective_from,
            "effective_to": "",
            "version_label": version_label,
            "is_current": 0,
            "content_hash": _aggregate_hash(group["content_hashes"]),
            "logical_content_hash": _aggregate_hash(group["logical_chunk_hashes"]),
            "search_text": search_text[:250_000],
        }
        finalized[item["version_id"]] = item
        by_unit[item["unit_id"]].append(item)
    for versions in by_unit.values():
        versions.sort(key=_version_sort_key)
        for index, item in enumerate(versions):
            item["is_current"] = int(index == len(versions) - 1)
            if index + 1 < len(versions):
                next_start = _parse_date(versions[index + 1]["effective_from"] or versions[index + 1]["revision_date"])
                if next_start is not None:
                    item["effective_to"] = (next_start - timedelta(days=1)).isoformat()
    return finalized


def _selected_version_rows(
    connection: sqlite3.Connection,
    *,
    profile_id: str | None,
    document_id: str | None,
    as_of_date: str | None,
) -> list[sqlite3.Row]:
    clauses = ["is_navigation=0"]
    params: list[Any] = []
    if profile_id:
        clauses.append("lower(profile_id)=lower(?)")
        params.append(profile_id)
    if document_id:
        clauses.append("document_id=?")
        params.append(document_id)
    if as_of_date:
        clauses.extend(
            [
                "(effective_from='' OR effective_from<=?)",
                "(effective_to='' OR effective_to>=?)",
            ]
        )
        params.extend([as_of_date, as_of_date])
    else:
        clauses.append("is_current=1")
    return connection.execute(
        f"SELECT * FROM regulation_versions WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()


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
    fts_terms = [term.replace('"', '""') for term in terms if term]
    if fts_terms:
        match_query = " OR ".join(f'"{term}"' for term in fts_terms)
        rows = connection.execute(
            f"""
            SELECT c.*, (1.0 / (1.0 + abs(bm25(chunks_fts, 8.0, 4.0, 6.0, 1.0)))) AS retrieval_score
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
    if as_of_date:
        return connection.execute(
            """
            SELECT * FROM regulation_versions
            WHERE unit_id=? AND (effective_from='' OR effective_from<=?)
              AND (effective_to='' OR effective_to>=?)
            ORDER BY effective_from DESC, revision_date DESC
            LIMIT 1
            """,
            (regulation_unit_id, as_of_date, as_of_date),
        ).fetchone()
    return connection.execute(
        "SELECT * FROM regulation_versions WHERE unit_id=? AND is_current=1 LIMIT 1",
        (regulation_unit_id,),
    ).fetchone()


def _read_vector_record_at(vector_path: str | Path, row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any] | None:
    offset = int(row["vector_offset"])
    length = int(row["vector_length"])
    if offset < 0 or length <= 0:
        return None
    try:
        with Path(vector_path).open("rb") as handle:
            handle.seek(offset)
            payload = handle.read(length)
        record = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    document_id, chunk_id = _record_identity(record)
    if document_id != str(row["document_id"]) or chunk_id != str(row["chunk_id"]):
        return None
    metadata = _metadata(record)
    content_hash = str(record.get("content_hash") or "")
    if content_hash != str(row["content_hash"]):
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
    start_index = next(
        (
            index
            for index, segment in enumerate(segments)
            if (title and normalize_regulation_title(title) in normalize_regulation_title(segment))
            or (regulation_no and _compact(regulation_no) in _compact(segment))
        ),
        max(0, len(segments) - 1),
    )
    selected = segments[start_index:]
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
        rows.append(
            (
                node_id,
                version_id,
                unit_id,
                parent_id,
                _toc_node_type(segment, depth),
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


def _split_toc_label(label: str) -> tuple[str, str]:
    match = re.match(r"^\s*((?:\uc81c\s*)?\d+(?:-\d+)*(?:\uc870(?:\uc758\d+)?|\uc7a5|\uc808|\uad00|\ud3b8)?)[\s.:\-]*(.*)$", label)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", label.strip()


def _toc_node_type(label: str, depth: int) -> str:
    compact = _compact(label)
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
    return "section"


def _public_regulation_row(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
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
        "status": str(row["status"]),
        "is_current": bool(row["is_current"]),
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


def _metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("metadata")
    return value if isinstance(value, dict) else {}


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str]:
    metadata = _metadata(record)
    return (
        str(record.get("document_id") or metadata.get("document_id") or ""),
        str(record.get("chunk_id") or metadata.get("chunk_id") or ""),
    )


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
