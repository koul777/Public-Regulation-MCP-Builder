"""Read-only helpers for the minimum regulation catalog contract.

This module reads catalog fields from the ``Document`` model, from a
document-like object's ``metadata`` mapping, or from a plain mapping so
callers can use it with documents and parsed/indexed records alike.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any, TypeAlias


DocumentLike: TypeAlias = Any
RegulationGroupKey: TypeAlias = tuple[str | None, str | None]

DEFAULT_ACTIVE_STATUSES = frozenset({"active", "approved"})


@dataclass(frozen=True, slots=True)
class RegulationMetadata:
    """Normalized, read-only catalog fields for one document."""

    profile_id: str | None = None
    regulation_id: str | None = None
    version: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    repealed_at: date | None = None
    status: str | None = None

    @property
    def effective_date_from(self) -> date | None:
        """Compatibility alias for consumers using explicit date naming."""

        return self.effective_from

    @property
    def effective_date_to(self) -> date | None:
        """Compatibility alias for consumers using explicit date naming."""

        return self.effective_to

    @property
    def group_key(self) -> RegulationGroupKey:
        return (self.profile_id, self.regulation_id)


def read_regulation_metadata(document: DocumentLike) -> RegulationMetadata:
    """Read and normalize catalog metadata without changing ``document``.

    ``Document`` has ``profile_id`` and a processing ``status`` as direct
    fields.  Regulation-specific fields are expected in ``metadata`` when
    present.  Direct fields are used as fallbacks for document-like objects;
    an explicit metadata status takes precedence over the processing status.
    Invalid dates and non-scalar values are treated as missing rather than
    raising an exception.
    """

    direct = document if isinstance(document, Mapping) else None
    metadata = _metadata_mapping(document)

    profile_id = _first_text(
        _value(document, "profile_id", direct=direct),
        _value(metadata, "profile_id"),
    )
    regulation_no = _first_text(
        _value(document, "regulation_no", direct=direct),
        _value(metadata, "regulation_no"),
    )
    regulation_id = _first_text(
        _value(document, "regulation_id", direct=direct),
        _value(metadata, "regulation_id"),
        regulation_no if _looks_like_stable_identifier(regulation_no) else None,
    )
    version = _first_text(
        _value(document, "version", direct=direct),
        _value(document, "regulation_version", direct=direct),
        _value(metadata, "version"),
        _value(metadata, "regulation_version"),
        _value(metadata, "revision"),
        _value(metadata, "revision_date"),
        _value(metadata, "valid_from"),
        _value(metadata, "effective_date"),
    )

    effective_dates = _value(metadata, "effective_dates")
    effective_from = _first_parseable_date(
        _value(document, "effective_from", direct=direct),
        _value(document, "effective_date_from", direct=direct),
        _value(metadata, "effective_from"),
        _value(metadata, "effective_date_from"),
        _value(metadata, "effective_start_date"),
        _value(metadata, "valid_from"),
        _value(metadata, "effective_date"),
        _value(metadata, "revision_date"),
        _value(effective_dates, "from"),
        _value(effective_dates, "start"),
    )
    effective_to = _parse_date(
        _first_value(
            _value(document, "effective_to", direct=direct),
            _value(document, "effective_date_to", direct=direct),
            _value(metadata, "effective_to"),
            _value(metadata, "effective_date_to"),
            _value(metadata, "effective_end_date"),
            _value(metadata, "valid_to"),
            _value(effective_dates, "to"),
            _value(effective_dates, "end"),
        )
    )
    repealed_at = _parse_date(
        _first_value(
            _value(document, "repealed_at", direct=direct),
            _value(metadata, "repealed_at"),
        )
    )

    metadata_status = _first_text(
        _value(document, "regulation_status", direct=direct),
        _value(document, "approval_status", direct=direct),
        _value(metadata, "status"),
        _value(metadata, "regulation_status"),
        _value(metadata, "approval_status"),
    )
    document_status = _first_text(
        _value(document, "status", direct=direct),
        _value(direct, "status"),
    )

    return RegulationMetadata(
        profile_id=profile_id,
        regulation_id=regulation_id,
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
        repealed_at=repealed_at,
        status=metadata_status or document_status,
    )


def group_documents_by_regulation(
    documents: Iterable[DocumentLike],
) -> dict[RegulationGroupKey, tuple[DocumentLike, ...]]:
    """Group documents by ``(profile_id, regulation_id)``.

    Documents with missing fields are retained under a key containing
    ``None``.  This preserves input records for review without inventing an
    institution or regulation identity.
    """

    grouped: dict[RegulationGroupKey, list[DocumentLike]] = {}
    for document in documents:
        key = read_regulation_metadata(document).group_key
        grouped.setdefault(key, []).append(document)
    return {key: tuple(items) for key, items in grouped.items()}


def latest_active_version(
    documents: Iterable[DocumentLike],
    *,
    as_of: date | datetime | str | None = None,
    active_statuses: Iterable[str] = DEFAULT_ACTIVE_STATUSES,
) -> DocumentLike | None:
    """Return the latest active document in a single regulation group.

    The iterable must represent one ``(profile_id, regulation_id)`` group.
    When ``as_of`` is provided, only documents effective on that date qualify.
    Ordering is deterministic: effective start date, natural version value,
    effective end date, then ``document_id``.
    """

    active = _normalized_statuses(active_statuses)
    if as_of is None or (isinstance(as_of, str) and not as_of.strip()):
        reference_date = date.today()
    else:
        reference_date = _parse_date(as_of)
        if reference_date is None:
            return None
    candidates: list[tuple[tuple[Any, ...], DocumentLike]] = []

    for document in documents:
        metadata = read_regulation_metadata(document)
        if _normalize_status(metadata.status) not in active:
            continue
        if metadata.effective_from is None:
            continue
        if not _is_effective_on(metadata, reference_date):
            continue
        candidates.append((_latest_sort_key(document, metadata), document))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def latest_history_version(
    documents: Iterable[DocumentLike],
    *,
    as_of: date | datetime | str | None = None,
) -> DocumentLike | None:
    """Return the current approved or historically effective superseded version."""
    candidates: list[DocumentLike] = []
    for document in documents:
        metadata = read_regulation_metadata(document)
        if _normalize_status(metadata.status) == "superseded" and metadata.effective_to is None:
            continue
        candidates.append(document)
    return latest_active_version(
        candidates,
        as_of=as_of,
        active_statuses={"approved", "superseded"},
    )


def filter_to_latest_active_versions(
    documents: Iterable[DocumentLike],
    *,
    as_of: date | datetime | str | None = None,
    include_legacy: bool = True,
) -> list[DocumentLike]:
    """Retain one approved active version per regulation.

    Catalog views keep legacy records by default for remediation. RAG/MCP
    callers must set ``include_legacy=False`` so records without an
    institution profile, regulation family, version, or effective start are
    never treated as current evidence.
    """
    grouped: dict[RegulationGroupKey, list[tuple[DocumentLike, RegulationMetadata]]] = {}
    for document in documents:
        metadata = read_regulation_metadata(document)
        grouped.setdefault(metadata.group_key, []).append((document, metadata))
    visible: list[DocumentLike] = []
    for (_profile_id, regulation_id), group in grouped.items():
        if not regulation_id:
            if include_legacy:
                visible.extend(document for document, _metadata in group)
            continue
        candidate_group = group
        active_statuses = DEFAULT_ACTIVE_STATUSES
        if not include_legacy:
            strict_candidates: list[tuple[DocumentLike, RegulationMetadata]] = []
            for document, metadata in group:
                if _normalize_status(metadata.status) == "superseded" and metadata.effective_to is None:
                    continue
                strict_candidates.append((document, metadata))
            candidate_group = tuple(strict_candidates)
            active_statuses = {"approved", "superseded"}
            if not candidate_group:
                continue
        latest_pair = _latest_active_version_from_pairs(
            candidate_group,
            as_of=as_of,
            active_statuses=active_statuses,
        )
        if latest_pair is None:
            if include_legacy:
                # Keep only genuine pre-catalog records (missing version or
                # effective start) visible for remediation.  A fully catalogued
                # group with no active version is inactive (e.g. repealed) and
                # must not fall open as current evidence.
                visible.extend(
                    document
                    for document, metadata in group
                    if not (metadata.version and metadata.effective_from)
                )
            continue
        latest, latest_metadata = latest_pair
        if not include_legacy and (not latest_metadata.profile_id or not latest_metadata.version):
            continue
        latest_id = _first_text(_value(latest, "document_id"), _value(latest, "id"))
        if not latest_id:
            if include_legacy:
                visible.extend(document for document, _metadata in group)
            continue
        visible.extend(
            document
            for document, _metadata in group
            if _first_text(_value(document, "document_id"), _value(document, "id")) == latest_id
        )
    return visible


def _latest_active_version_from_pairs(
    documents: Iterable[tuple[DocumentLike, RegulationMetadata]],
    *,
    as_of: date | datetime | str | None = None,
    active_statuses: Iterable[str] = DEFAULT_ACTIVE_STATUSES,
) -> tuple[DocumentLike, RegulationMetadata] | None:
    active = _normalized_statuses(active_statuses)
    if as_of is None or (isinstance(as_of, str) and not as_of.strip()):
        reference_date = date.today()
    else:
        reference_date = _parse_date(as_of)
        if reference_date is None:
            return None
    candidates: list[tuple[tuple[Any, ...], DocumentLike, RegulationMetadata]] = []

    for document, metadata in documents:
        if _normalize_status(metadata.status) not in active:
            continue
        if metadata.effective_from is None:
            continue
        if not _is_effective_on(metadata, reference_date):
            continue
        candidates.append((_latest_sort_key(document, metadata), document, metadata))

    if not candidates:
        return None
    _, document, metadata = max(candidates, key=lambda item: item[0])
    return document, metadata


def is_latest_active_version(
    document: DocumentLike,
    documents: Iterable[DocumentLike] | None = None,
    *,
    as_of: date | datetime | str | None = None,
    active_statuses: Iterable[str] = DEFAULT_ACTIVE_STATUSES,
) -> bool:
    """Return whether ``document`` is the latest active version in its group."""

    metadata = read_regulation_metadata(document)
    if documents is None:
        documents = (document,)
    else:
        documents = tuple(documents)

    group_documents = (
        candidate
        for candidate in documents
        if read_regulation_metadata(candidate).group_key == metadata.group_key
    )
    latest = latest_active_version(
        group_documents,
        as_of=as_of,
        active_statuses=active_statuses,
    )
    return latest is not None and _same_document(latest, document)


# Short aliases keep the service convenient without introducing another
# public data shape.
extract_regulation_metadata = read_regulation_metadata
group_by_regulation = group_documents_by_regulation


def _metadata_mapping(document: DocumentLike) -> Mapping[str, Any]:
    if isinstance(document, Mapping):
        value = document.get("metadata")
        if isinstance(value, Mapping):
            return value
        return document

    for attribute in ("metadata", "regulation_metadata", "catalog_metadata"):
        try:
            value = getattr(document, attribute, None)
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return value
    return {}


def _value(source: Any, name: str, *, direct: Mapping[str, Any] | None = None) -> Any:
    if direct is not None:
        return direct.get(name)
    if isinstance(source, Mapping):
        return source.get(name)
    if source is None:
        return None
    try:
        return getattr(source, name, None)
    except Exception:
        return None


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_text(*values: Any) -> str | None:
    value = _first_value(*values)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    return str(value).strip() or None


def _looks_like_stable_identifier(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and any(char.isdigit() for char in text)


def _first_parseable_date(*values: Any) -> date | None:
    for value in values:
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    text = text.replace(".", "-").replace("/", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalize_status(value: str | None) -> str | None:
    return value.strip().casefold() if value else None


def _normalized_statuses(values: Iterable[str]) -> frozenset[str]:
    return frozenset(
        normalized
        for value in values
        if (normalized := _normalize_status(_first_text(value))) is not None
    )


def _is_effective_on(metadata: RegulationMetadata, reference_date: date) -> bool:
    return (
        (metadata.effective_from is None or metadata.effective_from <= reference_date)
        and (metadata.effective_to is None or reference_date <= metadata.effective_to)
        and (metadata.repealed_at is None or reference_date < metadata.repealed_at)
    )


def _version_sort_key(version: str | None) -> tuple[tuple[tuple[int, Any], ...], str]:
    normalized = (version or "").strip().casefold()
    tokens = re.findall(r"\d+|[a-z]+", normalized)
    comparable = tuple(
        (0, int(token)) if token.isdigit() else (1, token) for token in tokens
    )
    return comparable, normalized


def _latest_sort_key(document: DocumentLike, metadata: RegulationMetadata) -> tuple[Any, ...]:
    document_id = _first_text(
        _value(document, "document_id"),
        _value(document, "id"),
    ) or ""
    return (
        metadata.effective_from or date.min,
        _version_sort_key(metadata.version),
        metadata.effective_to or date.min,
        document_id,
    )


def _same_document(left: DocumentLike, right: DocumentLike) -> bool:
    left_id = _first_text(_value(left, "document_id"), _value(left, "id"))
    right_id = _first_text(_value(right, "document_id"), _value(right, "id"))
    if left_id is not None and right_id is not None:
        return left_id == right_id
    return left is right
