"""Deterministic cross-regulation reference resolution and cycle detection.

The public entry point, :func:`build_regulation_reference_graph`, accepts
JSON-like mappings rather than repository schema objects.  Only records whose
``approval_status`` (or, when that field is absent, ``regulation_status``)
normalizes to ``"approved"`` participate in the graph.

Matching is deliberately conservative.  A reference is resolved inside the
source record's exact tenant/profile scope by:

1. exact regulation number;
2. exact canonical title;
3. exact explicit alias.

Matching keys receive only Unicode NFKC, surrounding/internal whitespace, and
case normalization.  Punctuation and words are never removed, and no fuzzy
matching is performed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
import unicodedata
from typing import Any


APPROVED_STATUS = "approved"

RESOLVED = "resolved"
UNRESOLVED = "unresolved"
AMBIGUOUS = "ambiguous"

MATCH_REGULATION_NO = "regulation_no"
MATCH_CANONICAL_TITLE = "canonical_title"
MATCH_ALIAS = "alias"

REASON_RESOLVED_BY_REGULATION_NO = "resolved_by_regulation_no"
REASON_RESOLVED_BY_CANONICAL_TITLE = "resolved_by_canonical_title"
REASON_RESOLVED_BY_ALIAS = "resolved_by_alias"
REASON_AMBIGUOUS_REGULATION_NO = "ambiguous_regulation_no"
REASON_AMBIGUOUS_CANONICAL_TITLE = "ambiguous_canonical_title"
REASON_AMBIGUOUS_ALIAS = "ambiguous_alias"
REASON_TARGET_REGULATION_NUMBER_NOT_FOUND = "target_regulation_number_not_found"
REASON_TARGET_UNIT_NOT_FOUND = "target_unit_not_found"
REASON_TARGET_IDENTITY_MISSING = "target_identity_missing"
REASON_ARTICLE_LOCATOR_MISSING = "article_locator_missing"
REASON_INVALID_ARTICLE_LOCATOR = "invalid_article_locator"
REASON_TARGET_ARTICLE_NOT_FOUND = "target_article_not_found"


_ARTICLE_LOCATOR_RE = re.compile(
    r"^\s*제\s*(?P<article>[0-9]+)\s*조"
    r"(?:\s*의\s*(?P<article_sub>[0-9]+))?"
    r"(?:\s*(?:제\s*)?(?P<paragraph>[0-9]+)\s*항)?"
    r"(?:\s*(?:제\s*)?(?P<item>[0-9]+)\s*호)?"
    r"(?:\s*(?:(?:제\s*)?(?P<subitem_number>[0-9]+)|"
    r"(?P<subitem_name>[가-힣]+))\s*목)?\s*$"
)


UnitKey = tuple[str, str, str]


@dataclass(frozen=True)
class _ArticleLocator:
    article_number: int
    article_subnumber: int | None = None
    paragraph_number: int | None = None
    item_number: int | None = None
    subitem: str | None = None
    numeric_subitem: bool = False

    @property
    def article(self) -> str:
        result = f"제{self.article_number}조"
        if self.article_subnumber is not None:
            result += f"의{self.article_subnumber}"
        return result

    @property
    def locator(self) -> str:
        result = self.article
        if self.paragraph_number is not None:
            result += f"제{self.paragraph_number}항"
        if self.item_number is not None:
            result += f"제{self.item_number}호"
        if self.subitem is not None:
            prefix = "제" if self.numeric_subitem else ""
            result += f"{prefix}{self.subitem}목"
        return result

    @property
    def sort_key(self) -> tuple[int, int, int, int, int, str]:
        return (
            self.article_number,
            self.article_subnumber or 0,
            self.paragraph_number or 0,
            self.item_number or 0,
            1 if self.numeric_subitem else 0,
            self.subitem or "",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "article": self.article,
            "paragraph": (
                f"제{self.paragraph_number}항"
                if self.paragraph_number is not None
                else None
            ),
            "item": f"제{self.item_number}호" if self.item_number is not None else None,
            "subitem": (
                f"{'제' if self.numeric_subitem else ''}{self.subitem}목"
                if self.subitem is not None
                else None
            ),
        }


@dataclass
class _UnitAccumulator:
    key: UnitKey
    title_values: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    regulation_no_values: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    version_values: set[str] = field(default_factory=set)
    effective_from_values: set[str] = field(default_factory=set)
    effective_to_values: set[str] = field(default_factory=set)
    aliases: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    articles: dict[str, _ArticleLocator] = field(default_factory=dict)
    approved_record_count: int = 0

    def add(self, record: Mapping[str, Any]) -> None:
        title = _required_text(
            _lookup(record, ("title", "regulation_title")),
            field_name="title",
            unit_key=self.key,
        )
        self.title_values[_match_key(title)].add(title)

        regulation_no = _optional_text(_lookup(record, ("regulation_no",)))
        if regulation_no:
            self.regulation_no_values[_match_key(regulation_no)].add(regulation_no)

        _add_optional_value(
            self.version_values,
            _lookup(record, ("version", "regulation_version")),
        )
        _add_optional_value(
            self.effective_from_values,
            _lookup(record, ("effective_from", "effective_date", "valid_from")),
        )
        _add_optional_value(
            self.effective_to_values,
            _lookup(record, ("effective_to", "valid_to")),
        )

        for alias in _alias_values(
            _first_collection(record, ("aliases", "regulation_aliases"))
        ):
            self.aliases[_match_key(alias)].add(alias)

        article_values = _collection_items(
            _first_collection(record, ("article_locators",)),
            field_name="article_locators",
        )
        article_raw = _optional_text(
            _lookup(record, ("article_locator", "article_no"))
        )
        if article_raw:
            article_values.append(article_raw)
        for raw_locator in article_values:
            if isinstance(raw_locator, Mapping):
                raw_locator = raw_locator.get("locator")
            article = _parse_article_locator(_optional_text(raw_locator))
            if article is not None:
                self.articles[article.locator] = article
        self.approved_record_count += 1

    def finish(self) -> "_Unit":
        if len(self.title_values) != 1:
            values = sorted(
                value
                for displays in self.title_values.values()
                for value in displays
            )
            raise ValueError(
                f"Approved records for unit {self.key!r} have conflicting titles: {values!r}."
            )
        if len(self.regulation_no_values) > 1:
            values = sorted(
                value
                for displays in self.regulation_no_values.values()
                for value in displays
            )
            raise ValueError(
                f"Approved records for unit {self.key!r} have conflicting regulation_no values: "
                f"{values!r}."
            )

        title = min(next(iter(self.title_values.values())))
        regulation_no = (
            min(next(iter(self.regulation_no_values.values())))
            if self.regulation_no_values
            else None
        )
        aliases = tuple(
            min(displays)
            for _, displays in sorted(self.aliases.items())
        )
        articles = tuple(
            sorted(self.articles.values(), key=lambda item: item.sort_key)
        )
        return _Unit(
            key=self.key,
            title=title,
            regulation_no=regulation_no,
            aliases=aliases,
            version=_single_optional_value(
                self.version_values,
                field_name="version",
                unit_key=self.key,
            ),
            effective_from=_single_optional_value(
                self.effective_from_values,
                field_name="effective_from",
                unit_key=self.key,
            ),
            effective_to=_single_optional_value(
                self.effective_to_values,
                field_name="effective_to",
                unit_key=self.key,
            ),
            articles=articles,
            article_locators=frozenset(article.locator for article in articles),
            article_bases=frozenset(article.article for article in articles),
            approved_record_count=self.approved_record_count,
        )


@dataclass(frozen=True)
class _Unit:
    key: UnitKey
    title: str
    regulation_no: str | None
    aliases: tuple[str, ...]
    version: str | None
    effective_from: str | None
    effective_to: str | None
    articles: tuple[_ArticleLocator, ...]
    article_locators: frozenset[str]
    article_bases: frozenset[str]
    approved_record_count: int

    @property
    def tenant_id(self) -> str:
        return self.key[0]

    @property
    def profile_id(self) -> str:
        return self.key[1]

    @property
    def unit_id(self) -> str:
        return self.key[2]

    def locator(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "profile_id": self.profile_id,
            "unit_id": self.unit_id,
            "title": self.title,
            "regulation_no": self.regulation_no,
            "version": self.version,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
        }

    def node(self) -> dict[str, Any]:
        return {
            **self.locator(),
            "aliases": list(self.aliases),
            "articles": [article.as_dict() for article in self.articles],
            "approved_record_count": self.approved_record_count,
        }


@dataclass(frozen=True)
class _Resolution:
    status: str
    reason_code: str
    match_type: str | None = None
    target: _Unit | None = None
    candidates: tuple[_Unit, ...] = ()


@dataclass
class _EdgeAccumulator:
    semantic_key: tuple[Any, ...]
    edge_type: str
    status: str
    source_unit: _Unit
    source_article: _ArticleLocator | None
    target_unit: _Unit | None
    requested_article: _ArticleLocator | None
    target_article: _ArticleLocator | None
    candidate_units: tuple[_Unit, ...]
    requested_target_titles: set[str] = field(default_factory=set)
    reason_codes: set[str] = field(default_factory=set)
    match_types: set[str] = field(default_factory=set)
    raw_mentions: dict[str, list[Any]] = field(default_factory=dict)
    evidence: dict[str, list[Any]] = field(default_factory=dict)

    def add(
        self,
        *,
        reason_code: str,
        match_type: str | None,
        requested_target_title: str | None,
        raw_mention: Any,
        evidence: dict[str, Any],
    ) -> None:
        self.reason_codes.add(reason_code)
        if match_type:
            self.match_types.add(match_type)
        if requested_target_title:
            self.requested_target_titles.add(requested_target_title)

        raw_key = _stable_json(raw_mention)
        if raw_key not in self.raw_mentions:
            self.raw_mentions[raw_key] = [raw_mention, 0]
        self.raw_mentions[raw_key][1] += 1

        evidence_key = _stable_json(evidence)
        if evidence_key not in self.evidence:
            self.evidence[evidence_key] = [evidence, 0]
        self.evidence[evidence_key][1] += 1

    def as_dict(self) -> dict[str, Any]:
        raw_mentions = [
            {"raw": value, "count": count}
            for _, (value, count) in sorted(self.raw_mentions.items())
        ]
        evidence = [
            {**value, "mention_count": count}
            for _, (value, count) in sorted(self.evidence.items())
        ]
        requested_target_title = (
            sorted(
                self.requested_target_titles,
                key=lambda value: (value.casefold(), value),
            )[0]
            if (
                self.status == UNRESOLVED
                and self.target_unit is None
                and self.requested_target_titles
            )
            else None
        )
        return {
            "edge_id": "regref_" + _stable_hash(self.semantic_key, length=20),
            "edge_type": self.edge_type,
            "status": self.status,
            "reason_codes": sorted(self.reason_codes),
            "match_types": sorted(self.match_types),
            "source_unit": self.source_unit.locator(),
            "source_article": (
                self.source_article.as_dict()
                if self.source_article is not None
                else None
            ),
            "target_unit": (
                self.target_unit.locator()
                if self.target_unit is not None
                else None
            ),
            "requested_target_title": requested_target_title,
            "requested_article": (
                self.requested_article.as_dict()
                if self.requested_article is not None
                else None
            ),
            "target_article": (
                self.target_article.as_dict()
                if self.target_article is not None
                else None
            ),
            "candidate_units": [
                unit.locator() for unit in self.candidate_units
            ],
            "mention_count": sum(item["count"] for item in raw_mentions),
            "raw_mentions": raw_mentions,
            "evidence_count": len(evidence),
            "evidence": evidence,
        }


def build_regulation_reference_graph(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic graph from approved regulation record mappings.

    Required approved-record fields are ``tenant_id``, ``profile_id``, a stable
    ``unit_id`` (``stable_unit_id``, ``regulation_unit_id``, and
    ``regulation_id`` are accepted aliases), and ``title`` (or
    ``regulation_title``).  Article/chunk records may provide
    ``article_locator`` or ``article_no``.

    Cross-regulation mentions are read from ``regulation_article_refs`` and,
    for unit-only mentions, the first present field among ``regulation_refs``
    and ``internal_regulation_refs``.  Fields may be top-level or inside a
    ``metadata`` mapping.  The return value is fully JSON-serializable and has
    ``units``, deduplicated ``edges``, Tarjan-derived ``cycles``, and ``stats``.
    """

    input_records = list(records)
    for index, record in enumerate(input_records):
        if not isinstance(record, Mapping):
            raise TypeError(f"Record at index {index} is not a mapping.")

    approved_records = [
        record for record in input_records if _is_approved(record)
    ]
    approved_records.sort(key=_approved_record_sort_key)

    unit_accumulators: dict[UnitKey, _UnitAccumulator] = {}
    for record in approved_records:
        unit_key = _unit_key(record)
        accumulator = unit_accumulators.get(unit_key)
        if accumulator is None:
            accumulator = _UnitAccumulator(key=unit_key)
            unit_accumulators[unit_key] = accumulator
        accumulator.add(record)

    units_by_key = {
        key: accumulator.finish()
        for key, accumulator in sorted(unit_accumulators.items())
    }
    indexes = _build_indexes(units_by_key.values())

    edge_accumulators: dict[tuple[Any, ...], _EdgeAccumulator] = {}
    ignored_external_reference_count = 0
    suppressed_redundant_unit_reference_count = 0
    for record in approved_records:
        source_unit = units_by_key[_unit_key(record)]
        source_article_raw = _optional_text(
            _lookup(record, ("article_locator", "article_no"))
        )
        source_article = _parse_article_locator(source_article_raw)
        evidence = _evidence_locator(record, source_article_raw)

        article_references = _first_collection(
            record,
            ("regulation_article_refs",),
        )
        internal_article_reference_keys: set[tuple[str, str]] = set()
        for raw_reference in _collection_items(
            article_references,
            field_name="regulation_article_refs",
        ):
            if not _is_internal_reference(raw_reference):
                ignored_external_reference_count += 1
                continue
            internal_article_reference_keys.add(_reference_identity_key(raw_reference))
            _add_reference_edge(
                edge_accumulators,
                source_unit=source_unit,
                source_article=source_article,
                raw_reference=raw_reference,
                force_article=True,
                evidence=evidence,
                indexes=indexes,
            )

        unit_references = _first_collection(
            record,
            ("regulation_refs", "internal_regulation_refs"),
        )
        for raw_reference in _collection_items(
            unit_references,
            field_name="regulation_refs",
        ):
            if not _is_internal_reference(raw_reference):
                ignored_external_reference_count += 1
                continue
            if _reference_identity_key(raw_reference) in internal_article_reference_keys:
                # Metadata extraction intentionally records a regulation name
                # in both collections when an exact cross-regulation article
                # citation is present. Keep the richer article edge only.
                suppressed_redundant_unit_reference_count += 1
                continue
            _add_reference_edge(
                edge_accumulators,
                source_unit=source_unit,
                source_article=source_article,
                raw_reference=raw_reference,
                force_article=False,
                evidence=evidence,
                indexes=indexes,
            )

    edges = [
        accumulator.as_dict()
        for _, accumulator in sorted(
            edge_accumulators.items(),
            key=lambda item: _stable_json(_preserve_value(item[0])),
        )
    ]
    units = [
        unit.node()
        for _, unit in sorted(units_by_key.items())
    ]
    cycles, resolved_unit_arcs = _cycles_for_graph(
        units_by_key=units_by_key,
        edge_accumulators=edge_accumulators.values(),
    )

    status_counts = {
        status: sum(1 for edge in edges if edge["status"] == status)
        for status in (RESOLVED, UNRESOLVED, AMBIGUOUS)
    }
    return {
        "schema_version": "1.0",
        "units": units,
        "edges": edges,
        "cycles": cycles,
        "stats": {
            "input_record_count": len(input_records),
            "approved_record_count": len(approved_records),
            "excluded_unapproved_record_count": (
                len(input_records) - len(approved_records)
            ),
            "unit_count": len(units),
            "edge_count": len(edges),
            "resolved_edge_count": status_counts[RESOLVED],
            "unresolved_edge_count": status_counts[UNRESOLVED],
            "ambiguous_edge_count": status_counts[AMBIGUOUS],
            "reference_mention_count": sum(
                edge["mention_count"] for edge in edges
            ),
            "ignored_external_reference_count": ignored_external_reference_count,
            "suppressed_redundant_unit_reference_count": (
                suppressed_redundant_unit_reference_count
            ),
            "resolved_unit_arc_count": resolved_unit_arcs,
            "cycle_count": len(cycles),
        },
    }


def canonicalize_article_locator(value: Any) -> dict[str, Any] | None:
    """Return a canonical Korean article locator mapping, or ``None``."""

    locator = _parse_article_locator(_optional_text(value))
    return locator.as_dict() if locator is not None else None


def _add_reference_edge(
    edge_accumulators: dict[tuple[Any, ...], _EdgeAccumulator],
    *,
    source_unit: _Unit,
    source_article: _ArticleLocator | None,
    raw_reference: Any,
    force_article: bool,
    evidence: dict[str, Any],
    indexes: dict[str, dict[tuple[str, str], dict[str, tuple[_Unit, ...]]]],
) -> None:
    preserved_raw = _preserve_value(raw_reference)
    identity, explicit_no, article_raw = _reference_parts(raw_reference)
    requested_target_title = _safe_requested_target_title(identity)
    is_article_reference = force_article or article_raw is not None
    edge_type = (
        "regulation_article_reference"
        if is_article_reference
        else "regulation_reference"
    )
    requested_article = (
        _parse_article_locator(article_raw)
        if is_article_reference
        else None
    )

    resolution = _resolve_unit(
        source_unit=source_unit,
        identity=identity,
        explicit_no=explicit_no,
        indexes=indexes,
    )
    status = resolution.status
    reason_code = resolution.reason_code
    target_article: _ArticleLocator | None = None

    if resolution.target is not None and is_article_reference:
        if not article_raw:
            status = UNRESOLVED
            reason_code = REASON_ARTICLE_LOCATOR_MISSING
        elif requested_article is None:
            status = UNRESOLVED
            reason_code = REASON_INVALID_ARTICLE_LOCATOR
        elif requested_article.article not in resolution.target.article_bases:
            status = UNRESOLVED
            reason_code = REASON_TARGET_ARTICLE_NOT_FOUND
        elif (
            requested_article.locator != requested_article.article
            and requested_article.locator
            not in resolution.target.article_locators
        ):
            status = UNRESOLVED
            reason_code = REASON_TARGET_ARTICLE_NOT_FOUND
        elif status == RESOLVED:
            target_article = requested_article

    semantic_key = _edge_semantic_key(
        source_unit=source_unit,
        source_article=source_article,
        edge_type=edge_type,
        status=status,
        target_unit=resolution.target,
        requested_article=requested_article,
        target_article=target_article,
        candidates=resolution.candidates,
        identity=identity,
        explicit_no=explicit_no,
        article_raw=article_raw,
        reason_code=reason_code,
    )
    accumulator = edge_accumulators.get(semantic_key)
    if accumulator is None:
        accumulator = _EdgeAccumulator(
            semantic_key=semantic_key,
            edge_type=edge_type,
            status=status,
            source_unit=source_unit,
            source_article=source_article,
            target_unit=resolution.target,
            requested_article=requested_article,
            target_article=target_article,
            candidate_units=resolution.candidates,
        )
        edge_accumulators[semantic_key] = accumulator
    accumulator.add(
        reason_code=reason_code,
        match_type=resolution.match_type,
        requested_target_title=requested_target_title,
        raw_mention=preserved_raw,
        evidence=evidence,
    )


def _edge_semantic_key(
    *,
    source_unit: _Unit,
    source_article: _ArticleLocator | None,
    edge_type: str,
    status: str,
    target_unit: _Unit | None,
    requested_article: _ArticleLocator | None,
    target_article: _ArticleLocator | None,
    candidates: tuple[_Unit, ...],
    identity: str,
    explicit_no: str,
    article_raw: str | None,
    reason_code: str,
) -> tuple[Any, ...]:
    source_locator = source_article.locator if source_article is not None else ""
    requested_locator = (
        requested_article.locator if requested_article is not None else ""
    )
    target_locator = target_article.locator if target_article is not None else ""
    target_key = target_unit.key if target_unit is not None else ("", "", "")
    candidate_keys = tuple(unit.key for unit in candidates)

    if status == RESOLVED:
        outcome = ("resolved", target_key, target_locator)
    elif status == AMBIGUOUS:
        outcome = ("ambiguous", candidate_keys, requested_locator or article_raw or "")
    elif target_unit is not None:
        outcome = (
            "identified_unit_unresolved",
            target_key,
            requested_locator or article_raw or "",
            reason_code,
        )
    else:
        outcome = (
            "unresolved",
            _match_key(explicit_no or identity),
            requested_locator or _display_text(article_raw),
            reason_code,
        )
    return (
        source_unit.key,
        source_locator,
        edge_type,
        status,
        outcome,
    )


def _resolve_unit(
    *,
    source_unit: _Unit,
    identity: str,
    explicit_no: str,
    indexes: dict[str, dict[tuple[str, str], dict[str, tuple[_Unit, ...]]]],
) -> _Resolution:
    scope = (source_unit.tenant_id, source_unit.profile_id)

    if explicit_no:
        candidates = indexes["number"].get(scope, {}).get(
            _match_key(explicit_no),
            (),
        )
        if not candidates:
            return _Resolution(
                status=UNRESOLVED,
                reason_code=REASON_TARGET_REGULATION_NUMBER_NOT_FOUND,
            )
        return _resolution_for_candidates(
            candidates,
            match_type=MATCH_REGULATION_NO,
            resolved_reason=REASON_RESOLVED_BY_REGULATION_NO,
            ambiguous_reason=REASON_AMBIGUOUS_REGULATION_NO,
        )

    if not identity:
        return _Resolution(
            status=UNRESOLVED,
            reason_code=REASON_TARGET_IDENTITY_MISSING,
        )

    identity_key = _match_key(identity)
    number_candidates = indexes["number"].get(scope, {}).get(identity_key, ())
    if number_candidates:
        return _resolution_for_candidates(
            number_candidates,
            match_type=MATCH_REGULATION_NO,
            resolved_reason=REASON_RESOLVED_BY_REGULATION_NO,
            ambiguous_reason=REASON_AMBIGUOUS_REGULATION_NO,
        )

    title_candidates = indexes["title"].get(scope, {}).get(identity_key, ())
    if title_candidates:
        return _resolution_for_candidates(
            title_candidates,
            match_type=MATCH_CANONICAL_TITLE,
            resolved_reason=REASON_RESOLVED_BY_CANONICAL_TITLE,
            ambiguous_reason=REASON_AMBIGUOUS_CANONICAL_TITLE,
        )

    alias_candidates = indexes["alias"].get(scope, {}).get(identity_key, ())
    if alias_candidates:
        return _resolution_for_candidates(
            alias_candidates,
            match_type=MATCH_ALIAS,
            resolved_reason=REASON_RESOLVED_BY_ALIAS,
            ambiguous_reason=REASON_AMBIGUOUS_ALIAS,
        )

    return _Resolution(
        status=UNRESOLVED,
        reason_code=REASON_TARGET_UNIT_NOT_FOUND,
    )


def _resolution_for_candidates(
    candidates: tuple[_Unit, ...],
    *,
    match_type: str,
    resolved_reason: str,
    ambiguous_reason: str,
) -> _Resolution:
    if len(candidates) == 1:
        return _Resolution(
            status=RESOLVED,
            reason_code=resolved_reason,
            match_type=match_type,
            target=candidates[0],
        )
    return _Resolution(
        status=AMBIGUOUS,
        reason_code=ambiguous_reason,
        match_type=match_type,
        candidates=candidates,
    )


def _build_indexes(
    units: Iterable[_Unit],
) -> dict[str, dict[tuple[str, str], dict[str, tuple[_Unit, ...]]]]:
    mutable: dict[
        str,
        dict[tuple[str, str], dict[str, set[_Unit]]],
    ] = {
        "number": defaultdict(lambda: defaultdict(set)),
        "title": defaultdict(lambda: defaultdict(set)),
        "alias": defaultdict(lambda: defaultdict(set)),
    }
    for unit in units:
        scope = (unit.tenant_id, unit.profile_id)
        mutable["title"][scope][_match_key(unit.title)].add(unit)
        if unit.regulation_no:
            mutable["number"][scope][_match_key(unit.regulation_no)].add(unit)
        for alias in unit.aliases:
            mutable["alias"][scope][_match_key(alias)].add(unit)

    result: dict[
        str,
        dict[tuple[str, str], dict[str, tuple[_Unit, ...]]],
    ] = {}
    for index_name, scopes in mutable.items():
        result[index_name] = {}
        for scope, values in scopes.items():
            result[index_name][scope] = {
                key: tuple(sorted(candidates, key=lambda unit: unit.key))
                for key, candidates in sorted(values.items())
            }
    return result


def _cycles_for_graph(
    *,
    units_by_key: Mapping[UnitKey, _Unit],
    edge_accumulators: Iterable[_EdgeAccumulator],
) -> tuple[list[dict[str, Any]], int]:
    adjacency: dict[UnitKey, set[UnitKey]] = {
        key: set() for key in units_by_key
    }
    for edge in edge_accumulators:
        if (
            edge.status == RESOLVED
            and edge.target_unit is not None
        ):
            adjacency[edge.source_unit.key].add(edge.target_unit.key)

    components = _tarjan_strongly_connected_components(
        vertices=units_by_key.keys(),
        adjacency=adjacency,
    )
    cycles: list[dict[str, Any]] = []
    for component in components:
        component_set = set(component)
        is_self_loop = (
            len(component) == 1
            and component[0] in adjacency[component[0]]
        )
        if len(component) == 1 and not is_self_loop:
            continue
        unit_keys = tuple(sorted(component))
        internal_edge_count = sum(
            1
            for source in unit_keys
            for target in adjacency[source]
            if target in component_set
        )
        cycles.append(
            {
                "cycle_id": "cycle_" + _stable_hash(unit_keys, length=20),
                "tenant_id": unit_keys[0][0],
                "profile_id": unit_keys[0][1],
                "size": len(unit_keys),
                "self_loop": is_self_loop,
                "internal_unit_edge_count": internal_edge_count,
                "unit_ids": [key[2] for key in unit_keys],
                "units": [
                    units_by_key[key].locator() for key in unit_keys
                ],
            }
        )
    cycles.sort(
        key=lambda cycle: (
            cycle["tenant_id"],
            cycle["profile_id"],
            tuple(cycle["unit_ids"]),
        )
    )
    return cycles, sum(len(targets) for targets in adjacency.values())


def _tarjan_strongly_connected_components(
    *,
    vertices: Iterable[UnitKey],
    adjacency: Mapping[UnitKey, set[UnitKey]],
) -> list[tuple[UnitKey, ...]]:
    """Return SCCs using an iterative, deterministic Tarjan traversal."""

    index = 0
    indices: dict[UnitKey, int] = {}
    lowlinks: dict[UnitKey, int] = {}
    tarjan_stack: list[UnitKey] = []
    on_stack: set[UnitKey] = set()
    components: list[tuple[UnitKey, ...]] = []

    for root in sorted(vertices):
        if root in indices:
            continue

        indices[root] = index
        lowlinks[root] = index
        index += 1
        tarjan_stack.append(root)
        on_stack.add(root)
        frames: list[list[Any]] = [
            [root, None, 0, sorted(adjacency.get(root, set()))]
        ]

        while frames:
            vertex, parent, next_neighbor, neighbors = frames[-1]
            if next_neighbor < len(neighbors):
                neighbor = neighbors[next_neighbor]
                frames[-1][2] += 1
                if neighbor not in indices:
                    indices[neighbor] = index
                    lowlinks[neighbor] = index
                    index += 1
                    tarjan_stack.append(neighbor)
                    on_stack.add(neighbor)
                    frames.append(
                        [
                            neighbor,
                            vertex,
                            0,
                            sorted(adjacency.get(neighbor, set())),
                        ]
                    )
                elif neighbor in on_stack:
                    lowlinks[vertex] = min(
                        lowlinks[vertex],
                        indices[neighbor],
                    )
                continue

            frames.pop()
            if parent is not None:
                lowlinks[parent] = min(lowlinks[parent], lowlinks[vertex])
            if lowlinks[vertex] != indices[vertex]:
                continue

            component: list[UnitKey] = []
            while tarjan_stack:
                member = tarjan_stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == vertex:
                    break
            components.append(tuple(sorted(component)))

    components.sort()
    return components


def _parse_article_locator(value: str | None) -> _ArticleLocator | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    match = _ARTICLE_LOCATOR_RE.fullmatch(normalized)
    if match is None:
        return None

    numbers = {
        name: int(match.group(name))
        for name in ("article", "article_sub", "paragraph", "item")
        if match.group(name) is not None
    }
    subitem_number = match.group("subitem_number")
    if any(number <= 0 for number in numbers.values()):
        return None
    if subitem_number is not None and int(subitem_number) <= 0:
        return None

    subitem_name = match.group("subitem_name")
    return _ArticleLocator(
        article_number=numbers["article"],
        article_subnumber=numbers.get("article_sub"),
        paragraph_number=numbers.get("paragraph"),
        item_number=numbers.get("item"),
        subitem=(
            str(int(subitem_number))
            if subitem_number is not None
            else subitem_name
        ),
        numeric_subitem=subitem_number is not None,
    )


def _reference_parts(raw_reference: Any) -> tuple[str, str, str | None]:
    if isinstance(raw_reference, Mapping):
        identity = _optional_text(
            _mapping_lookup(
                raw_reference,
                ("regulation_ref", "regulation_title", "title", "name", "value"),
            )
        ) or ""
        explicit_no = _optional_text(
            _mapping_lookup(
                raw_reference,
                ("regulation_no", "target_regulation_no"),
            )
        ) or ""
        article_marker_present = any(
            key in raw_reference
            for key in ("article_ref", "article_locator", "target_article")
        )
        article_raw = _optional_text(
            _mapping_lookup(
                raw_reference,
                ("article_ref", "article_locator", "target_article"),
            )
        )
        if article_marker_present and article_raw is None:
            article_raw = ""
        return identity, explicit_no, article_raw
    return _optional_text(raw_reference) or "", "", None


def _reference_identity_key(raw_reference: Any) -> tuple[str, str]:
    identity, explicit_no, _ = _reference_parts(raw_reference)
    if explicit_no:
        return "regulation_no", _match_key(explicit_no)
    return "identity", _match_key(identity)


def _is_internal_reference(raw_reference: Any) -> bool:
    if not isinstance(raw_reference, Mapping):
        return True
    scope = _optional_text(raw_reference.get("scope"))
    return scope is None or scope.casefold() == "internal"


def _evidence_locator(
    record: Mapping[str, Any],
    source_article_raw: str | None,
) -> dict[str, Any]:
    preserved = _preserve_value(record)
    explicit_record_id = _optional_text(
        _lookup(record, ("record_id", "id"))
    )
    chunk_id = _optional_text(_lookup(record, ("chunk_id",)))
    article_id = _optional_text(_lookup(record, ("article_id",)))
    evidence_id = (
        explicit_record_id
        or chunk_id
        or article_id
        or "record_" + _stable_hash(preserved, length=20)
    )
    return {
        "evidence_id": evidence_id,
        "record_id": explicit_record_id,
        "document_id": _optional_text(_lookup(record, ("document_id",))),
        "chunk_id": chunk_id,
        "article_id": article_id,
        "source_article_raw": source_article_raw,
        "source_page_start": _preserve_value(
            _lookup(record, ("source_page_start",))
        ),
        "source_page_end": _preserve_value(
            _lookup(record, ("source_page_end",))
        ),
    }


def _approved_record_sort_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Order graph inputs deterministically without serializing full body text."""

    return (
        _display_text(_lookup(record, ("tenant_id",))),
        _display_text(_lookup(record, ("profile_id",))),
        _display_text(
            _lookup(
                record,
                ("unit_id", "stable_unit_id", "regulation_unit_id", "regulation_id"),
            )
        ),
        _display_text(_lookup(record, ("title", "regulation_title"))),
        _display_text(_lookup(record, ("regulation_no",))),
        _display_text(_lookup(record, ("article_locator", "article_no"))),
        _display_text(_lookup(record, ("document_id",))),
        _display_text(_lookup(record, ("chunk_id", "record_id", "id"))),
        _stable_json(
            _preserve_value(
                _first_collection(record, ("regulation_article_refs",))
            )
        ),
        _stable_json(
            _preserve_value(
                _first_collection(record, ("regulation_refs", "internal_regulation_refs"))
            )
        ),
    )


def _unit_key(record: Mapping[str, Any]) -> UnitKey:
    tenant_id = _required_identifier(
        _lookup(record, ("tenant_id",)),
        field_name="tenant_id",
    )
    profile_id = _required_identifier(
        _lookup(record, ("profile_id",)),
        field_name="profile_id",
    )
    unit_id = _required_identifier(
        _lookup(
            record,
            (
                "unit_id",
                "stable_unit_id",
                "regulation_unit_id",
                "regulation_id",
            ),
        ),
        field_name="unit_id",
    )
    return tenant_id, profile_id, unit_id


def _is_approved(record: Mapping[str, Any]) -> bool:
    approval_status = _lookup(record, ("approval_status",))
    if approval_status is None:
        approval_status = _lookup(record, ("regulation_status",))
    return (
        _optional_text(approval_status) or ""
    ).casefold() == APPROVED_STATUS


def _lookup(record: Mapping[str, Any], names: Sequence[str]) -> Any:
    containers: list[Mapping[str, Any]] = [record]
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    for container in containers:
        for name in names:
            if name in container and container[name] is not None:
                return container[name]
    return None


def _mapping_lookup(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _first_collection(
    record: Mapping[str, Any],
    names: Sequence[str],
) -> Any:
    containers: list[Mapping[str, Any]] = [record]
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    for container in containers:
        for name in names:
            if name in container:
                return container[name]
    return None


def _collection_items(value: Any, *, field_name: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: _stable_json(_preserve_value(item)))
    if isinstance(value, Sequence) and not isinstance(
        value,
        (bytes, bytearray),
    ):
        return list(value)
    raise TypeError(f"{field_name} must be a mapping, string, or sequence.")


def _alias_values(value: Any) -> list[str]:
    aliases = []
    for raw_alias in _collection_items(value, field_name="aliases"):
        if isinstance(raw_alias, Mapping):
            raise TypeError("aliases entries must be strings.")
        alias = _optional_text(raw_alias)
        if alias:
            aliases.append(alias)
    return aliases


def _required_identifier(value: Any, *, field_name: str) -> str:
    result = _optional_text(value)
    if not result:
        raise ValueError(
            f"Approved records require a non-empty {field_name}."
        )
    return result


def _required_text(
    value: Any,
    *,
    field_name: str,
    unit_key: UnitKey,
) -> str:
    result = _optional_text(value)
    if not result:
        raise ValueError(
            f"Approved records for unit {unit_key!r} require a non-empty {field_name}."
        )
    return result


def _add_optional_value(values: set[str], value: Any) -> None:
    normalized = _optional_text(value)
    if normalized:
        values.add(normalized)


def _single_optional_value(
    values: set[str],
    *,
    field_name: str,
    unit_key: UnitKey,
) -> str | None:
    if len(values) > 1:
        raise ValueError(
            f"Approved records for unit {unit_key!r} have conflicting {field_name} "
            f"values: {sorted(values)!r}."
        )
    return next(iter(values)) if values else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            value = value.isoformat()
        except (TypeError, ValueError):
            pass
    result = _display_text(value)
    return result or None


def _display_text(value: Any) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    return " ".join(normalized.split())


def _safe_requested_target_title(value: Any) -> str | None:
    """Keep a bounded source-visible title without carrying storage identifiers."""

    text = "".join(
        character
        for character in _display_text(value)
        if character >= " " and character != "\x7f"
    )[:300].strip()
    return text or None


def _match_key(value: Any) -> str:
    return _display_text(value).casefold()


def _preserve_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _preserve_value(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (set, frozenset)):
        preserved = [_preserve_value(item) for item in value]
        return sorted(preserved, key=_stable_json)
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_preserve_value(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _stable_json(value: Any) -> str:
    return json.dumps(
        _preserve_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_hash(value: Any, *, length: int) -> str:
    return sha256(_stable_json(value).encode("utf-8")).hexdigest()[:length]


__all__ = [
    "AMBIGUOUS",
    "RESOLVED",
    "UNRESOLVED",
    "build_regulation_reference_graph",
    "canonicalize_article_locator",
]
