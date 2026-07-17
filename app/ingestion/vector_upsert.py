from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from uuid import uuid4

from app.ingestion.vector_adapter import (
    ALLOWED_SECURITY_LEVELS,
    APPROVED_CHUNK_STATUS,
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    approval_provenance_issue_fields,
    vector_record_verification_hash,
    vector_record_path_leaks,
    with_vector_record_verification,
)
from app.ingestion.embedding_adapter import EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
from app.ingestion.embedding_adapter import MAX_EMBEDDING_DIMENSIONS
from app.ingestion.vector_integrity import embedded_vector_integrity_reason
from app.core.tenant_access import tenant_storage_key
from app.retrieval.bm25_index import default_bm25_index_path, write_bm25_index


SUPPORTED_UPSERT_SCHEMA_VERSIONS = {VECTOR_RECORD_SCHEMA_VERSION, EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION}
SUPPORTED_QDRANT_TARGET_TYPES = {"qdrant-local-jsonl"}
_LOCAL_TARGET_FILENAMES = {
    "local-jsonl": "approved_vectors.jsonl",
    "qdrant-local-jsonl": "approved_qdrant_points.jsonl",
    "pgvector-local-jsonl": "approved_pgvector_rows.jsonl",
    "chroma-local-jsonl": "approved_chroma_rows.jsonl",
}


class VectorUpsertTarget(Protocol):
    target_type: str

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LocalJsonlVectorTarget:
    path: Path
    target_type: str = "local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing = _read_existing_records(self.path)
        existing_by_id = {record["id"]: record for record in existing}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={record["id"] for record in validated},
            document_id_getter=_local_record_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            previous = existing_by_id.get(record["id"])
            if previous is None:
                inserted += 1
            elif previous.get("content_hash") == record.get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[record["id"]] = record
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            final_records = [existing_by_id[key] for key in sorted(existing_by_id)]
            _write_jsonl_atomic(self.path, final_records)
            bm25_index = write_bm25_index(default_bm25_index_path(self.path), final_records)
        else:
            bm25_index = None
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "bm25_index_path": str(default_bm25_index_path(self.path)),
            "bm25_index_written": bm25_index is not None,
            "dry_run": dry_run,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class QdrantLocalJsonlTarget:
    path: Path
    target_type: str = "qdrant-local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing_points = _read_existing_qdrant_points(self.path)
        existing_by_id = {str(point["id"]): point for point in existing_points}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={str(record["id"]) for record in validated},
            document_id_getter=_qdrant_point_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            point = qdrant_point_from_record(record)
            previous = existing_by_id.get(point["id"])
            if previous is None:
                inserted += 1
            elif previous.get("payload", {}).get("content_hash") == point.get("payload", {}).get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[point["id"]] = point
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl_atomic(self.path, [existing_by_id[key] for key in sorted(existing_by_id)])
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "dry_run": dry_run,
            "mode": "local_export_only",
            "api_call_count": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing_points),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class ChromaLocalJsonlTarget:
    path: Path
    target_type: str = "chroma-local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing_rows = _read_existing_chroma_rows(self.path)
        existing_by_id = {str(row["id"]): row for row in existing_rows}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={str(record["id"]) for record in validated},
            document_id_getter=_metadata_row_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            row = chroma_row_from_record(record)
            previous = existing_by_id.get(row["id"])
            if previous is None:
                inserted += 1
            elif previous.get("metadata", {}).get("content_hash") == row.get("metadata", {}).get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[row["id"]] = row
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl_atomic(self.path, [existing_by_id[key] for key in sorted(existing_by_id)])
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "dry_run": dry_run,
            "mode": "local_export_only",
            "api_call_count": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing_rows),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class PgvectorLocalJsonlTarget:
    path: Path
    target_type: str = "pgvector-local-jsonl"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        existing_rows = _read_existing_pgvector_rows(self.path)
        existing_by_id = {str(row["id"]): row for row in existing_rows}
        removed = _remove_inactive_document_items(
            existing_by_id,
            document_id=document_id,
            active_ids={str(record["id"]) for record in validated},
            document_id_getter=_metadata_row_document_id,
        )
        inserted = 0
        updated = 0
        unchanged = 0
        for record in validated:
            row = pgvector_row_from_record(record)
            previous = existing_by_id.get(row["id"])
            if previous is None:
                inserted += 1
            elif previous.get("content_hash") == row.get("content_hash"):
                unchanged += 1
            else:
                updated += 1
            existing_by_id[row["id"]] = row
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl_atomic(self.path, [existing_by_id[key] for key in sorted(existing_by_id)])
        return {
            "target_type": self.target_type,
            "target_path": str(self.path),
            "dry_run": dry_run,
            "mode": "local_export_only",
            "api_call_count": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "input_record_count": len(validated),
            "existing_record_count": len(existing_rows),
            "final_record_count": len(existing_by_id),
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "removed_count": removed,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
        }


@dataclass(frozen=True)
class QdrantRestManifestTarget:
    path: Path
    target_type: str = "qdrant-rest-manifest"
    collection_name: str = "reg-rag-collection"

    def upsert(
        self,
        records: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        fail_on_leak: bool = True,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        validated = _with_upsert_verification(validate_embedded_vector_records(records))
        leaks = vector_record_path_leaks(validated)
        if fail_on_leak and leaks:
            raise ValueError(f"Vector upsert records contain local path leaks: {len(leaks)}")
        points = [qdrant_point_from_record(record) for record in validated]
        dimensions = sorted({len(point.get("vector") or []) for point in points})
        manifest_body = {
            "target_type": self.target_type,
            "collection_name": self.collection_name,
            "mode": "manifest_only",
            "live_network_blocked": True,
            "api_call_count": 0,
            "dry_run": dry_run,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "verification_version": VECTOR_RECORD_VERIFICATION_VERSION,
            "verification_record_count": len(validated),
            "schema_versions": sorted({str(record.get("schema_version") or "") for record in validated}),
            "embedding_models": sorted({str(record.get("embedding_model") or "") for record in validated}),
            "embedding_dimensions": dimensions,
            "input_record_count": len(validated),
            "planned_upsert_count": len(points),
            "removed_count": 0,
            "local_path_leak_count": len(leaks),
            "local_path_leak_samples": leaks[:20],
            "approval_required_fields": [
                "budget_reference",
                "approval_reference",
                "audit_log_id",
            ],
            "sample_point_ids": [str(point.get("id") or "") for point in points[:5]],
        }
        if not dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(manifest_body, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            **manifest_body,
            "target_path": str(self.path),
        }


def vector_upsert_target(
    target_type: str,
    *,
    target_path: str | Path | None = None,
    collection_name: str | None = None,
) -> VectorUpsertTarget:
    normalized = str(target_type or "").strip().lower()
    if normalized == "local-jsonl":
        if not target_path:
            raise ValueError("local-jsonl target requires target_path.")
        return LocalJsonlVectorTarget(Path(target_path))
    if normalized == "qdrant-local-jsonl":
        if not target_path:
            raise ValueError("qdrant-local-jsonl target requires target_path.")
        return QdrantLocalJsonlTarget(Path(target_path))
    if normalized == "pgvector-local-jsonl":
        if not target_path:
            raise ValueError("pgvector-local-jsonl target requires target_path.")
        return PgvectorLocalJsonlTarget(Path(target_path))
    if normalized == "chroma-local-jsonl":
        if not target_path:
            raise ValueError("chroma-local-jsonl target requires target_path.")
        return ChromaLocalJsonlTarget(Path(target_path))
    if normalized == "qdrant-rest-manifest":
        if not target_path:
            raise ValueError("qdrant-rest-manifest target requires target_path.")
        return QdrantRestManifestTarget(Path(target_path), collection_name=collection_name or "reg-rag-collection")
    if normalized == "qdrant-rest":
        raise ValueError(
            "qdrant-rest live network upsert is blocked by default. "
            "Use qdrant-rest-manifest for manifest-only planning or qdrant-local-jsonl for offline export."
        )
    raise ValueError(
        "Unsupported vector upsert target_type. Supported: local-jsonl, qdrant-local-jsonl, "
        "pgvector-local-jsonl, chroma-local-jsonl, qdrant-rest-manifest."
    )


def load_vector_records_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid vector JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Invalid vector JSONL at {path}:{line_no}: expected object")
        records.append(record)
    return validate_vector_records(records)


def validate_vector_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Vector record {index} must be an object.")
        schema_version = record.get("schema_version")
        if schema_version not in SUPPORTED_UPSERT_SCHEMA_VERSIONS:
            raise ValueError(f"Vector record {index} has unsupported schema_version: {record.get('schema_version')}")
        record_id = str(record.get("id") or "").strip()
        if not record_id:
            raise ValueError(f"Vector record {index} is missing id.")
        if record_id in seen:
            duplicates.append(record_id)
        seen.add(record_id)
        if not str(record.get("text") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing text.")
        metadata = record.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError(f"Vector record {record_id} metadata must be an object.")
        metadata = metadata or {}
        if metadata.get("approval_status") != APPROVED_CHUNK_STATUS or not metadata.get("approval_id"):
            raise ValueError(f"Vector record {record_id} is not approved for indexing.")
        if not str(metadata.get("approved_content_hash") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing approved_content_hash.")
        provenance_issues = approval_provenance_issue_fields(record)
        if provenance_issues:
            raise ValueError(
                f"Vector record {record_id} is missing or has invalid approval provenance: "
                f"{', '.join(provenance_issues)}."
            )
        if not str(metadata.get("tenant_id") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing tenant_id.")
        record_tenant_id = str(record.get("tenant_id") or "").strip()
        metadata_tenant_id = str(metadata.get("tenant_id") or "").strip()
        if record_tenant_id and record_tenant_id != metadata_tenant_id:
            raise ValueError(
                f"Vector record {record_id} has inconsistent tenant_id between record and metadata."
            )
        security_level = str(metadata.get("security_level") or "").strip().lower()
        if security_level not in ALLOWED_SECURITY_LEVELS:
            raise ValueError(f"Vector record {record_id} has invalid or missing security_level.")
        if not str(record.get("content_hash") or "").strip():
            raise ValueError(f"Vector record {record_id} is missing content_hash.")
        verification_version = str(record.get("verification_version") or "")
        verification_hash = str(record.get("verification_hash") or "")
        if verification_version and verification_version != VECTOR_RECORD_VERIFICATION_VERSION:
            raise ValueError(f"Vector record {record_id} has unsupported verification_version: {verification_version}")
        if verification_version and not verification_hash:
            raise ValueError(f"Vector record {record_id} is missing verification_hash.")
        if verification_hash and verification_hash != vector_record_verification_hash(record):
            raise ValueError(f"Vector record {record_id} has invalid verification_hash.")
        if schema_version == EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION:
            embedding = record.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError(f"Embedded vector record {record_id} is missing embedding.")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in embedding):
                raise ValueError(f"Embedded vector record {record_id} embedding must contain only numbers.")
            dimensions = record.get("embedding_dimensions")
            if (
                not isinstance(dimensions, int)
                or isinstance(dimensions, bool)
                or dimensions != len(embedding)
                or not 1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
            ):
                raise ValueError(
                    f"Embedded vector record {record_id} embedding_dimensions must match embedding length "
                    f"and be between 1 and {MAX_EMBEDDING_DIMENSIONS}."
                )
            if not str(record.get("embedding_model") or "").strip():
                raise ValueError(f"Embedded vector record {record_id} is missing embedding_model.")
            if not str(record.get("embedding_hash") or "").strip():
                raise ValueError(f"Embedded vector record {record_id} is missing embedding_hash.")
            integrity_reason = embedded_vector_integrity_reason(record)
            if integrity_reason:
                raise ValueError(f"Embedded vector record {record_id} failed integrity check: {integrity_reason}.")
        validated.append(record)
    if duplicates:
        sample = ", ".join(sorted(set(duplicates))[:20])
        raise ValueError(f"Vector upsert input has duplicate record ids: {sample}")
    return validated


def validate_vector_record_tenant_scope(
    records: Iterable[dict[str, Any]],
    *,
    expected_tenant_id: str | None = None,
) -> str:
    """Require vector input to represent one tenant and, when supplied, that tenant."""
    expected = str(expected_tenant_id or "").strip()
    observed: set[str] = set()
    for index, record in enumerate(records, start=1):
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        record_tenant_id = str(record.get("tenant_id") or "").strip()
        metadata_tenant_id = str(metadata.get("tenant_id") or "").strip()
        tenant_id = metadata_tenant_id or record_tenant_id
        if not tenant_id:
            raise ValueError(f"Vector record {index} is missing tenant_id.")
        if record_tenant_id and metadata_tenant_id and record_tenant_id != metadata_tenant_id:
            raise ValueError(f"Vector record {index} has inconsistent tenant_id fields.")
        observed.add(tenant_id)
        if expected and tenant_id != expected:
            raise ValueError(
                f"Vector record {index} tenant_id does not match expected tenant: {tenant_id!r} != {expected!r}."
            )
    if len(observed) > 1:
        raise ValueError(f"Vector upsert input contains multiple tenant scopes: {', '.join(sorted(observed))}")
    if expected:
        return expected
    return next(iter(observed), "")


def validate_vector_target_tenant_scope(
    target_type: str,
    target_path: str | Path,
    *,
    expected_tenant_id: str | None = None,
) -> None:
    """Reject existing target rows that would mix tenants during an upsert."""
    expected = str(expected_tenant_id or "").strip()
    if not expected or str(target_type or "").strip().lower() == "qdrant-rest-manifest":
        return
    path = Path(target_path)
    if not path.is_file():
        return
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid existing vector target JSON at {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid existing vector target row at {path}:{line_no}: expected object")
        normalized_type = str(target_type or "").strip().lower()
        if normalized_type == "qdrant-local-jsonl":
            container = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        else:
            container = row.get("metadata") if isinstance(row.get("metadata"), dict) else row
        tenant_id = str(container.get("tenant_id") or row.get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError(f"Existing vector target row at {path}:{line_no} is missing tenant_id.")
        if tenant_id != expected:
            raise ValueError(
                f"Existing vector target row at {path}:{line_no} belongs to tenant {tenant_id!r}, "
                f"not expected tenant {expected!r}."
            )


def canonical_vector_target_path(
    data_dir: str | Path,
    tenant_id: str,
    *,
    target_type: str,
    tenant_storage_isolation: bool,
) -> Path | None:
    """Return the official local target path for a tenant-scoped runtime."""
    filename = _LOCAL_TARGET_FILENAMES.get(str(target_type or "").strip().lower())
    if filename is None:
        return None
    base = Path(data_dir)
    if tenant_storage_isolation:
        base = base / "tenants" / tenant_storage_key(tenant_id)
    tenant_key = tenant_storage_key(tenant_id)
    return base / "vector_db" / tenant_key / filename


def _with_upsert_verification(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verified_at = datetime.now(timezone.utc).isoformat()
    return [with_vector_record_verification(record, verified_at=verified_at) for record in records]


def validate_embedded_vector_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = validate_vector_records(records)
    non_embedded = [
        str(record.get("id") or "")
        for record in validated
        if record.get("schema_version") != EMBEDDED_VECTOR_RECORD_SCHEMA_VERSION
    ]
    if non_embedded:
        sample = ", ".join(non_embedded[:20])
        raise ValueError(
            "qdrant-local-jsonl requires embedded vector records. "
            f"Non-embedded record ids: {sample}"
        )
    return validated


def qdrant_point_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    payload = {
        "content_hash": record.get("content_hash"),
        "text": record.get("text"),
        "document_id": record.get("document_id"),
        "chunk_id": record.get("chunk_id"),
        "embedding_model": record.get("embedding_model"),
        "embedding_hash": record.get("embedding_hash"),
        "verification_version": record.get("verification_version"),
        "verification_hash": record.get("verification_hash"),
        "verified_at": record.get("verified_at"),
        **metadata,
    }
    return {
        "id": str(record.get("id") or ""),
        "vector": list(record.get("embedding") or []),
        "payload": payload,
    }


def chroma_row_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    payload = {
        "content_hash": record.get("content_hash"),
        "document_id": record.get("document_id"),
        "chunk_id": record.get("chunk_id"),
        "embedding_model": record.get("embedding_model"),
        "embedding_hash": record.get("embedding_hash"),
        "verification_version": record.get("verification_version"),
        "verification_hash": record.get("verification_hash"),
        "verified_at": record.get("verified_at"),
        **metadata,
    }
    return {
        "id": str(record.get("id") or ""),
        "document": record.get("text"),
        "embedding": list(record.get("embedding") or []),
        "metadata": payload,
    }


def pgvector_row_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    payload = {
        "content_hash": record.get("content_hash"),
        "document_id": record.get("document_id"),
        "chunk_id": record.get("chunk_id"),
        "embedding_model": record.get("embedding_model"),
        "embedding_hash": record.get("embedding_hash"),
        "verification_version": record.get("verification_version"),
        "verification_hash": record.get("verification_hash"),
        "verified_at": record.get("verified_at"),
        **metadata,
    }
    return {
        "id": str(record.get("id") or ""),
        "content": record.get("text"),
        "embedding": list(record.get("embedding") or []),
        "embedding_dimensions": record.get("embedding_dimensions"),
        "content_hash": record.get("content_hash"),
        "metadata": payload,
    }


def _remove_inactive_document_items(
    existing_by_id: dict[str, dict[str, Any]],
    *,
    document_id: str | None,
    active_ids: set[str],
    document_id_getter,
) -> int:
    if not document_id:
        return 0
    removed = 0
    for item_id, item in list(existing_by_id.items()):
        if document_id_getter(item) == document_id and item_id not in active_ids:
            del existing_by_id[item_id]
            removed += 1
    return removed


def _local_record_document_id(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") or {}
    return str(record.get("document_id") or metadata.get("document_id") or "")


def _qdrant_point_document_id(point: dict[str, Any]) -> str:
    payload = point.get("payload") or {}
    return str(payload.get("document_id") or "")


def _metadata_row_document_id(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") or {}
    return str(metadata.get("document_id") or "")


def _read_existing_qdrant_points(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    points: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            point = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(point, dict):
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: expected object")
        if not str(point.get("id") or "").strip():
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: missing id")
        if not isinstance(point.get("vector"), list) or not point.get("vector"):
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: missing vector")
        if not isinstance(point.get("payload"), dict):
            raise ValueError(f"Invalid Qdrant JSONL at {path}:{line_no}: payload must be an object")
        points.append(point)
    return points


def _read_existing_chroma_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: expected object")
        if not str(row.get("id") or "").strip():
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: missing id")
        if not isinstance(row.get("embedding"), list) or not row.get("embedding"):
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: missing embedding")
        if not str(row.get("document") or "").strip():
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: missing document")
        if not isinstance(row.get("metadata"), dict):
            raise ValueError(f"Invalid Chroma JSONL at {path}:{line_no}: metadata must be an object")
        rows.append(row)
    return rows


def _read_existing_pgvector_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: expected object")
        if not str(row.get("id") or "").strip():
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: missing id")
        if not isinstance(row.get("embedding"), list) or not row.get("embedding"):
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: missing embedding")
        if not str(row.get("content") or "").strip():
            raise ValueError(f"Invalid pgvector JSONL at {path}:{line_no}: missing content")
        rows.append(row)
    return rows


def _read_existing_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return load_vector_records_jsonl(path)


def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        tmp_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + ("\n" if records else ""),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
