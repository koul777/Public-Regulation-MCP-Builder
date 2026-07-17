from __future__ import annotations

import json
import re
import hashlib
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.schemas.chunk import Chunk
from app.schemas.quality import QualityCheck, QualityReport
from app.schemas.structure import StructureNode
from app.schemas.validation import ValidationIssue


REQUIRED_METADATA = ["document_name", "source_file", "hierarchy_path", "chunk_type"]
HWP_ARTIFACT_PATTERN = re.compile(r"(捤獥|汤捯|氠瑢|湰灧|桤灧|灳瑣|湯慴|湯湷|†普)")
PRIVATE_USE_PATTERN = re.compile(r"[\ue000-\uf8ff]")


@dataclass(frozen=True)
class QualityGateProfile:
    coverage_ratio_min: float = 0.80
    coverage_ratio_max: float = 1.30
    table_false_positive_attention_max_count: int = 6
    table_false_positive_attention_max_ratio: float = 0.15

    def __post_init__(self) -> None:
        if self.coverage_ratio_min < 0 or self.coverage_ratio_max < 0:
            raise ValueError("Coverage ratio thresholds must be non-negative.")
        if self.coverage_ratio_min > self.coverage_ratio_max:
            raise ValueError("coverage_ratio_min must be less than or equal to coverage_ratio_max.")
        if self.table_false_positive_attention_max_count < 0:
            raise ValueError("table_false_positive_attention_max_count must be non-negative.")
        if not 0 <= self.table_false_positive_attention_max_ratio <= 1:
            raise ValueError("table_false_positive_attention_max_ratio must be between 0 and 1.")

    @property
    def coverage_threshold_label(self) -> str:
        return f"{self.coverage_ratio_min:.2f}-{self.coverage_ratio_max:.2f}"

    @property
    def table_false_positive_threshold_label(self) -> str:
        return (
            f"<= {self.table_false_positive_attention_max_count} and "
            f"<= {self.table_false_positive_attention_max_ratio:.0%} of chunks"
        )


DEFAULT_QUALITY_PROFILE = QualityGateProfile()
QUALITY_PROFILE_FIELDS = set(QualityGateProfile.__dataclass_fields__)


@dataclass(frozen=True)
class QualityProfileConfig:
    default_profile: QualityGateProfile = DEFAULT_QUALITY_PROFILE
    profiles: dict[str, QualityGateProfile] | None = None
    sha256: str = ""


def load_quality_gate_profiles(path: str | Path | None) -> tuple[QualityGateProfile, dict[str, QualityGateProfile]]:
    config = load_quality_gate_profile_config(path)
    return config.default_profile, config.profiles or {}


def load_quality_gate_profile_config(path: str | Path | None) -> QualityProfileConfig:
    if not path:
        return QualityProfileConfig(default_profile=DEFAULT_QUALITY_PROFILE, profiles={}, sha256="")
    profile_path = Path(path).expanduser()
    if not profile_path.exists():
        raise FileNotFoundError(f"Quality profile config not found: {profile_path}")
    content = profile_path.read_bytes()
    return load_quality_gate_profile_config_from_bytes(content)


def load_quality_gate_profile_config_from_bytes(content: bytes) -> QualityProfileConfig:
    raw = json.loads(content.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Quality profile config must be a JSON object.")
    unknown = sorted(set(raw) - {"default", "profiles"})
    if unknown:
        raise ValueError(f"Quality profile config has unknown fields: {', '.join(unknown)}")
    default_profile = _profile_from_mapping(raw.get("default", {}), label="default")
    raw_profiles = raw.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("Quality profile config 'profiles' must be a JSON object.")
    profiles = _profiles_from_mapping(raw_profiles)
    return QualityProfileConfig(
        default_profile=default_profile,
        profiles=profiles,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def quality_gate_profile_to_dict(profile: QualityGateProfile) -> dict[str, int | float]:
    return {
        "coverage_ratio_min": profile.coverage_ratio_min,
        "coverage_ratio_max": profile.coverage_ratio_max,
        "table_false_positive_attention_max_count": profile.table_false_positive_attention_max_count,
        "table_false_positive_attention_max_ratio": profile.table_false_positive_attention_max_ratio,
    }


def quality_profile_config_to_dict(config: QualityProfileConfig) -> dict[str, Any]:
    return {
        "default": quality_gate_profile_to_dict(config.default_profile),
        "profiles": {
            profile_id: quality_gate_profile_to_dict(config.profiles[profile_id])
            for profile_id in sorted(config.profiles or {})
        },
    }


def quality_profile_config_to_bytes(config: QualityProfileConfig) -> bytes:
    content = json.dumps(
        quality_profile_config_to_dict(config),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return f"{content}\n".encode("utf-8")


def upsert_quality_profile(
    config: QualityProfileConfig,
    profile_id: str | None = None,
    *,
    coverage_ratio_min: float = DEFAULT_QUALITY_PROFILE.coverage_ratio_min,
    coverage_ratio_max: float = DEFAULT_QUALITY_PROFILE.coverage_ratio_max,
    table_false_positive_attention_max_count: int = DEFAULT_QUALITY_PROFILE.table_false_positive_attention_max_count,
    table_false_positive_attention_max_ratio: float = DEFAULT_QUALITY_PROFILE.table_false_positive_attention_max_ratio,
    update_default: bool = False,
) -> QualityProfileConfig:
    profile = QualityGateProfile(
        coverage_ratio_min=coverage_ratio_min,
        coverage_ratio_max=coverage_ratio_max,
        table_false_positive_attention_max_count=table_false_positive_attention_max_count,
        table_false_positive_attention_max_ratio=table_false_positive_attention_max_ratio,
    )
    raw = quality_profile_config_to_dict(config)
    if update_default:
        raw["default"] = quality_gate_profile_to_dict(profile)
    else:
        cleaned_profile_id = str(profile_id or "").strip()
        if not cleaned_profile_id:
            raise ValueError("Quality profile id must not be empty.")
        existing_ids = {str(key).strip().lower(): key for key in raw.get("profiles", {})}
        stored_profile_id = existing_ids.get(cleaned_profile_id.lower(), cleaned_profile_id)
        raw.setdefault("profiles", {})[stored_profile_id] = quality_gate_profile_to_dict(profile)
    validated = load_quality_gate_profile_config_from_bytes(
        json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return load_quality_gate_profile_config_from_bytes(quality_profile_config_to_bytes(validated))


def save_quality_profile_config(
    path: str | Path,
    config: QualityProfileConfig,
    *,
    backup_existing: bool = True,
) -> dict[str, Any]:
    profile_path = Path(path).expanduser()
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    content = quality_profile_config_to_bytes(config)
    saved_config = load_quality_gate_profile_config_from_bytes(content)
    backup_path: Path | None = None
    if backup_existing and profile_path.exists():
        backup_path = _next_backup_path(profile_path)
        shutil.copy2(profile_path, backup_path)
    tmp_path = profile_path.with_name(f".{profile_path.name}.{uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, profile_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return {
        "path": str(profile_path),
        "backup_path": str(backup_path) if backup_path else None,
        "sha256": saved_config.sha256,
        "profile_count": len(saved_config.profiles or {}),
    }


def _profiles_from_mapping(raw_profiles: dict) -> dict[str, QualityGateProfile]:
    profiles: dict[str, QualityGateProfile] = {}
    original_keys: dict[str, str] = {}
    for raw_profile_id, profile_data in raw_profiles.items():
        profile_id = str(raw_profile_id)
        normalized = profile_id.strip().lower()
        if not normalized:
            raise ValueError("Quality profile id must not be empty.")
        if normalized != profile_id.lower():
            raise ValueError(f"Quality profile id must not contain leading or trailing whitespace: {profile_id!r}")
        if normalized in profiles:
            raise ValueError(
                "Quality profile ids collide after normalization: "
                f"{original_keys[normalized]!r}, {profile_id!r}"
            )
        profiles[normalized] = _profile_from_mapping(profile_data, label=f"profiles.{profile_id}")
        original_keys[normalized] = profile_id
    return profiles


def _profile_from_mapping(raw: Any, *, label: str) -> QualityGateProfile:
    if raw in (None, {}):
        return DEFAULT_QUALITY_PROFILE
    if not isinstance(raw, dict):
        raise ValueError(f"Quality profile config '{label}' must be a JSON object.")
    unknown = sorted(set(raw) - QUALITY_PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Quality profile config '{label}' has unknown fields: {', '.join(unknown)}")
    return QualityGateProfile(**raw)


def _next_backup_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.bak")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak.{index}")
        if not candidate.exists():
            return candidate
        index += 1


class QualityGate:
    def __init__(
        self,
        *,
        default_profile: QualityGateProfile = DEFAULT_QUALITY_PROFILE,
        profiles: dict[str, QualityGateProfile] | None = None,
        strict_profile_ids: bool = False,
    ) -> None:
        self.default_profile = default_profile
        self.profiles = {str(key).strip().lower(): value for key, value in (profiles or {}).items()}
        self.strict_profile_ids = strict_profile_ids

    def evaluate(
        self,
        nodes: list[StructureNode],
        chunks: list[Chunk],
        issues: list[ValidationIssue],
        document_id: str,
        source_text: str | None = None,
        profile_id: str | None = None,
    ) -> QualityReport:
        profile = self._profile(profile_id)
        issue_counts = Counter(issue.severity for issue in issues)
        node_type_counts = Counter(node.node_type for node in nodes)
        chunk_type_counts = Counter(chunk.chunk_type for chunk in chunks)
        duplicate_chunk_id_count = self._duplicate_count(chunk.chunk_id for chunk in chunks)
        empty_chunk_count = sum(1 for chunk in chunks if not chunk.text.strip())
        missing_page_count = sum(
            1
            for chunk in chunks
            if chunk.source_page_start is None and not self._source_page_unavailable_is_declared(chunk)
        )
        required_metadata_missing = self._missing_required_metadata(chunks)
        missing_required_metadata_count = required_metadata_missing["chunk_count"]
        missing_required_metadata_field_count = required_metadata_missing["field_count"]
        metadata_coverage = self._metadata_coverage(chunks)
        table_metrics = self._table_metrics(chunks)
        structure_metrics = self._structure_metrics(nodes, chunks)
        text_quality_metrics = self._text_quality_metrics(chunks)
        coverage_metrics = self._coverage_metrics(source_text, chunks)
        if source_text and self._compact_text(source_text) and not nodes:
            structure_metrics["nonempty_source_without_structure"] = 1

        checks = [
            self._check("chunks_present", "error", len(chunks) > 0, len(chunks), 1, "At least one chunk must be produced."),
            self._check(
                "no_validation_errors",
                "error",
                issue_counts.get("error", 0) == 0,
                issue_counts.get("error", 0),
                0,
                "Validation errors must be zero.",
            ),
            self._check(
                "unique_chunk_ids",
                "error",
                duplicate_chunk_id_count == 0,
                duplicate_chunk_id_count,
                0,
                "Chunk IDs must be unique.",
            ),
            self._check(
                "no_empty_chunks",
                "error",
                empty_chunk_count == 0,
                empty_chunk_count,
                0,
                "Empty chunks are not usable for retrieval.",
            ),
            self._check(
                "page_metadata_present",
                "warning",
                missing_page_count == 0,
                missing_page_count,
                0,
                "Every chunk should keep source page metadata when the parser can provide it.",
            ),
            self._check(
                "required_metadata_present",
                "warning",
                missing_required_metadata_count == 0,
                missing_required_metadata_count,
                0,
                "Every chunk should include the required retrieval metadata fields.",
            ),
            self._check(
                "no_replacement_characters",
                "warning",
                text_quality_metrics["replacement_char_chunks"] == 0,
                text_quality_metrics["replacement_char_chunks"],
                0,
                "Replacement characters usually mean an encoding or parser extraction problem.",
            ),
            self._check(
                "no_hwp_mojibake_artifacts",
                "warning",
                text_quality_metrics["hwp_mojibake_artifact_chunks"] == 0
                and text_quality_metrics["suspicious_regulation_metadata_count"] == 0,
                text_quality_metrics["hwp_mojibake_artifact_chunks"],
                0,
                "HWP artifact text such as mojibake markers should be removed before chunking.",
            ),
            self._check(
                "structured_nodes_present",
                "warning",
                not structure_metrics.get("nonempty_source_without_structure", 0)
                and structure_metrics.get("structure_fallback_chunk_count", 0) == 0,
                structure_metrics.get("structure_fallback_chunk_count"),
                0,
                "Non-empty documents should produce structured nodes; fallback chunks need review.",
            ),
            self._check(
                "table_rows_when_table_like",
                "warning",
                table_metrics["table_like_without_cell_rows"] == 0,
                table_metrics["table_like_without_cell_rows"],
                0,
                "Table-like chunks without structured cell rows should be reviewed or reclassified.",
            ),
            self._check(
                "chunk_source_coverage",
                "warning",
                self._coverage_ratio_is_reasonable(
                    coverage_metrics,
                    profile,
                    source_text_provided=source_text is not None,
                ),
                coverage_metrics.get("chunk_to_source_char_ratio"),
                profile.coverage_threshold_label,
                "Chunk normalized text should preserve most source text without large duplication.",
            ),
            self._check(
                "regulation_boundary_duplication",
                "warning",
                self._regulation_duplication_is_reasonable(structure_metrics),
                structure_metrics.get("duplicate_regulation_node_count"),
                "<= 50% of unique regulation numbers",
                "Repeated regulation boundary nodes suggest running headers or appendix/supplementary headers need more precise handling.",
            ),
            self._check(
                "article_regulation_metadata_present",
                "warning",
                structure_metrics.get("article_chunks_missing_regulation_no", 0) == 0,
                structure_metrics.get("article_chunks_missing_regulation_no"),
                0,
                "Article chunks should carry regulation_no metadata for filtering and citation paths.",
            ),
            self._check(
                "detected_regulations_reach_chunks",
                "warning",
                structure_metrics.get("detected_reg_no_without_chunk_metadata_count", 0) == 0,
                structure_metrics.get("detected_reg_no_without_chunk_metadata_count"),
                0,
                "Detected regulation numbers should propagate to at least one chunk unless they are catalog-only noise.",
            ),
            self._check(
                "private_use_characters_observed",
                "info",
                text_quality_metrics["private_use_char_chunks"] == 0,
                text_quality_metrics["private_use_char_chunks"],
                0,
                "Private-use Unicode often represents HWP bullets or form glyphs; monitor conversion quality.",
            ),
            self._check(
                "table_false_positive_attention",
                "info",
                self._table_false_positive_rate_is_reasonable(table_metrics, len(chunks), profile),
                table_metrics.get("table_false_positive_attention_chunks"),
                profile.table_false_positive_threshold_label,
                "High unstable table false-positive counts should become regression fixtures.",
            ),
        ]

        failed_error_check_count = sum(1 for check in checks if check.severity == "error" and not check.passed)
        failed_warning_check_count = sum(1 for check in checks if check.severity == "warning" and not check.passed)
        error_count = issue_counts.get("error", 0) + failed_error_check_count
        warning_count = issue_counts.get("warning", 0) + failed_warning_check_count
        score_warning_count = max(0, warning_count - self._dedicated_metric_warning_issue_count(issues))
        score = self._score(
            error_count=error_count,
            warning_count=score_warning_count,
            duplicate_chunk_id_count=duplicate_chunk_id_count,
            empty_chunk_count=empty_chunk_count,
            missing_page_count=missing_page_count,
            missing_required_metadata_count=missing_required_metadata_count,
            replacement_char_chunks=int(text_quality_metrics["replacement_char_chunks"]),
            table_like_without_cell_rows=int(table_metrics["table_like_without_cell_rows"]),
        )
        passed = all(check.passed for check in checks if check.severity == "error")
        recommendations = self._recommendations(checks, metadata_coverage, table_metrics, structure_metrics)

        return QualityReport(
            document_id=document_id,
            passed=passed,
            score=score,
            node_count=len(nodes),
            chunk_count=len(chunks),
            issue_count=len(issues),
            error_count=error_count,
            warning_count=warning_count,
            validation_error_count=issue_counts.get("error", 0),
            validation_warning_count=issue_counts.get("warning", 0),
            failed_error_check_count=failed_error_check_count,
            failed_warning_check_count=failed_warning_check_count,
            duplicate_chunk_id_count=duplicate_chunk_id_count,
            empty_chunk_count=empty_chunk_count,
            missing_page_count=missing_page_count,
            missing_required_metadata_count=missing_required_metadata_count,
            missing_required_metadata_field_count=missing_required_metadata_field_count,
            node_type_counts=dict(sorted(node_type_counts.items())),
            chunk_type_counts=dict(sorted(chunk_type_counts.items())),
            metadata_coverage=metadata_coverage,
            table_metrics=table_metrics,
            structure_metrics=structure_metrics,
            text_quality_metrics=text_quality_metrics,
            coverage_metrics=coverage_metrics,
            checks=checks,
            recommendations=recommendations,
        )

    def _dedicated_metric_warning_issue_count(self, issues: list[ValidationIssue]) -> int:
        dedicated_metric_issue_types = {"page_number_missing"}
        return sum(
            1
            for issue in issues
            if issue.severity == "warning" and issue.issue_type in dedicated_metric_issue_types
        )

    def to_markdown(self, report: QualityReport) -> str:
        lines = [
            f"# Preprocessing Quality Report: {report.document_id}",
            "",
            "## Summary",
            "",
            f"- Passed: {report.passed}",
            f"- Score: {report.score:.1f}",
            f"- Nodes: {report.node_count}",
            f"- Chunks: {report.chunk_count}",
            f"- Validation issues: {report.issue_count} ({report.validation_error_count} errors, {report.validation_warning_count} warnings)",
            f"- Failed quality checks: {report.failed_error_check_count} errors, {report.failed_warning_check_count} warnings",
            "",
            "## Checks",
            "",
        ]
        for check in report.checks:
            status = "PASS" if check.passed else "FAIL"
            lines.append(f"- {status} [{check.severity}] {check.name}: {check.value} (threshold: {check.threshold})")
        lines.extend(["", "## Chunk Types", ""])
        for key, value in report.chunk_type_counts.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Metadata Coverage", ""])
        for key, value in report.metadata_coverage.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Table Metrics", ""])
        for key, value in report.table_metrics.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Text Quality Metrics", ""])
        for key, value in report.text_quality_metrics.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Structure Metrics", ""])
        for key, value in report.structure_metrics.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Coverage Metrics", ""])
        for key, value in report.coverage_metrics.items():
            lines.append(f"- {key}: {value}")
        if report.recommendations:
            lines.extend(["", "## Recommendations", ""])
            for item in report.recommendations:
                lines.append(f"- {item}")
        return "\n".join(lines).strip() + "\n"

    def _duplicate_count(self, values) -> int:
        counts = Counter(values)
        return sum(count - 1 for count in counts.values() if count > 1)

    def _missing_required_metadata(self, chunks: list[Chunk]) -> dict[str, int]:
        chunk_count = 0
        field_count = 0
        for chunk in chunks:
            missing_fields = [field for field in REQUIRED_METADATA if not chunk.metadata.get(field)]
            if missing_fields:
                chunk_count += 1
                field_count += len(missing_fields)
        return {"chunk_count": chunk_count, "field_count": field_count}

    def _metadata_coverage(self, chunks: list[Chunk]) -> dict[str, int]:
        fields = [
            "references",
            "article_refs",
            "regulation_article_refs",
            "appendix_refs",
            "form_refs",
            "external_law_refs",
            "revision_events",
            "revision_history",
            "effective_date",
            "revision_date",
            "valid_from",
            "article_effective_overrides",
            "supplementary_identifier_date",
            "supplementary_paragraph_label",
            "supplementary_boilerplate",
        ]
        coverage: dict[str, int] = {}
        for field in fields:
            coverage[f"chunks_with_{field}"] = sum(1 for chunk in chunks if self._has_metadata_value(chunk.metadata.get(field)))
        coverage["chunks_with_source_page_unavailable_reason"] = sum(
            1 for chunk in chunks if self._source_page_unavailable_is_declared(chunk)
        )
        return coverage

    def _table_metrics(self, chunks: list[Chunk]) -> dict[str, int | float]:
        table_like = [chunk for chunk in chunks if chunk.metadata.get("table_like")]
        classifications = Counter(
            chunk.metadata.get("table_classification")
            for chunk in chunks
            if chunk.metadata.get("table_classification")
        )
        false_positive_chunks = [
            chunk
            for chunk in chunks
            if str(chunk.metadata.get("table_classification") or "").startswith("probable_false_positive")
        ]
        stable_false_positive_chunks = [
            chunk for chunk in false_positive_chunks if chunk.metadata.get("table_false_positive_stability") == "stable"
        ]
        attention_false_positive_chunks = [
            chunk for chunk in false_positive_chunks if chunk.metadata.get("table_false_positive_stability") != "stable"
        ]
        row_counts = [len(chunk.metadata.get("table_cell_rows") or []) for chunk in table_like]
        raw_row_counts = [len(chunk.metadata.get("table_rows") or []) for chunk in table_like]
        column_counts = [int(chunk.metadata.get("table_column_count") or 0) for chunk in table_like]
        chunks_with_cell_rows = sum(1 for count in row_counts if count > 0)
        confidences = [float(chunk.metadata.get("table_confidence") or 0.0) for chunk in table_like]
        review_required_chunks = [
            chunk
            for chunk in table_like
            if chunk.metadata.get("table_review_required")
            or any((row or {}).get("review_required") for row in (chunk.metadata.get("table_cell_rows") or []))
        ]
        review_required_rows = sum(
            1
            for chunk in table_like
            for row in (chunk.metadata.get("table_cell_rows") or [])
            if (row or {}).get("review_required") or (row or {}).get("row_quality_flags")
        )
        citation_ready_chunks = [
            chunk
            for chunk in table_like
            if (chunk.metadata.get("table_cell_rows") or [])
            and (
                chunk.metadata.get("table_citation_label")
                or chunk.metadata.get("table_appendix_no")
                or "별표" in str(chunk.metadata.get("hierarchy_path") or "")
                or "별지" in str(chunk.metadata.get("hierarchy_path") or "")
            )
        ]
        multi_page_chunks = [
            chunk
            for chunk in table_like
            if chunk.source_page_start and chunk.source_page_end and chunk.source_page_start != chunk.source_page_end
        ]
        return {
            "table_like_chunks": len(table_like),
            "chunks_with_table_cell_rows": chunks_with_cell_rows,
            "chunks_with_table_rows": chunks_with_cell_rows,
            "table_like_without_cell_rows": len(table_like) - chunks_with_cell_rows,
            "table_cell_row_count": sum(row_counts),
            "table_row_count": sum(row_counts),
            "table_raw_row_count": sum(raw_row_counts),
            "max_table_column_count": max(column_counts) if column_counts else 0,
            "avg_table_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
            "probable_table_false_positive_chunks": sum(
                count for classification, count in classifications.items() if str(classification).startswith("probable_false_positive")
            ),
            "stable_table_false_positive_chunks": len(stable_false_positive_chunks),
            "table_false_positive_attention_chunks": len(attention_false_positive_chunks),
            "probable_table_extraction_failed_chunks": classifications.get("probable_table_extraction_failed", 0),
            "table_review_required_chunks": len(review_required_chunks),
            "table_review_required_row_count": review_required_rows,
            "table_citation_ready_chunks": len(citation_ready_chunks),
            "multi_page_table_like_chunks": len(multi_page_chunks),
            "appendix_table_like_chunks": sum(
                1
                for chunk in table_like
                if chunk.chunk_type == "appendix" or "별표" in str(chunk.metadata.get("hierarchy_path") or "")
            ),
        }

    def _structure_metrics(self, nodes: list[StructureNode], chunks: list[Chunk]) -> dict[str, int | float]:
        regulation_numbers = {
            node.number
            for node in nodes
            if node.node_type == "regulation" and node.number
        }
        regulation_nodes = [node for node in nodes if node.node_type == "regulation"]
        chunk_regulation_numbers = {
            chunk.metadata.get("regulation_no")
            for chunk in chunks
            if chunk.metadata.get("regulation_no")
        }
        chunk_owner_regulation_node_ids = self._chunk_owner_regulation_node_ids(nodes, chunks)
        parent_groups: dict[str, set[str | None]] = {}
        identity_counts = Counter(
            (node.number, self._normalized_regulation_title(node.title))
            for node in regulation_nodes
            if node.number
        )
        titles_by_number: dict[str, set[str]] = defaultdict(set)
        for node in regulation_nodes:
            if node.number:
                parent_groups.setdefault(node.number, set()).add(node.parent_id)
                titles_by_number[node.number].add(self._normalized_regulation_title(node.title))
        duplicate_identity_count = sum(count - 1 for count in identity_counts.values() if count > 1)
        title_variant_count = sum(max(0, len(titles) - 1) for titles in titles_by_number.values())
        return {
            "regulation_node_count": len(regulation_nodes),
            "unique_regulation_no_count": len(regulation_numbers),
            "unique_regulation_identity_count": len(identity_counts),
            "duplicate_regulation_node_count": duplicate_identity_count,
            "regulation_title_variant_count": title_variant_count,
            "zero_chunk_regulation_node_count": sum(
                1 for node in regulation_nodes if node.node_id not in chunk_owner_regulation_node_ids
            ),
            "detected_reg_no_without_chunk_metadata_count": len(regulation_numbers - chunk_regulation_numbers),
            "same_regulation_multiple_parent_count": sum(1 for parents in parent_groups.values() if len(parents) > 1),
            "chunks_with_regulation_metadata": sum(1 for chunk in chunks if chunk.metadata.get("regulation_no")),
            "unique_chunk_regulation_no_count": len(chunk_regulation_numbers),
            "chunks_missing_regulation_no": sum(1 for chunk in chunks if not chunk.metadata.get("regulation_no")),
            "article_chunks_missing_regulation_no": sum(
                1 for chunk in chunks if chunk.chunk_type == "article" and not chunk.metadata.get("regulation_no")
            ),
            "article_node_count": sum(1 for node in nodes if node.node_type == "article"),
            "appendix_node_count": sum(1 for node in nodes if node.node_type == "appendix"),
            "form_node_count": sum(1 for node in nodes if node.node_type == "form"),
            "supplementary_node_count": sum(1 for node in nodes if node.node_type == "supplementary"),
            "structure_fallback_chunk_count": sum(
                1
                for chunk in chunks
                if chunk.metadata.get("structure_fallback") or "structure_fallback_document_chunk" in chunk.warnings
            ),
            "chunks_without_source_nodes": sum(1 for chunk in chunks if not chunk.source_node_ids),
            "nonempty_source_without_structure": 0,
        }

    def _normalized_regulation_title(self, title: str | None) -> str:
        return re.sub(r"\s+", "", title or "")

    def _chunk_owner_regulation_node_ids(self, nodes: list[StructureNode], chunks: list[Chunk]) -> set[str]:
        lookup = {node.node_id: node for node in nodes}
        owner_ids: set[str] = set()
        for chunk in chunks:
            for source_node_id in chunk.source_node_ids:
                current_id: str | None = source_node_id
                while current_id and current_id in lookup:
                    node = lookup[current_id]
                    if node.node_type == "regulation":
                        owner_ids.add(node.node_id)
                        break
                    current_id = node.parent_id
        return owner_ids

    def _text_quality_metrics(self, chunks: list[Chunk]) -> dict[str, int | float]:
        control_pattern = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
        texts = [chunk.text for chunk in chunks]
        normalized_texts = [chunk.normalized_text or chunk.text for chunk in chunks]
        metadata_values = [
            str(chunk.metadata.get(key) or "")
            for chunk in chunks
            for key in ("regulation_no", "regulation_title", "document_name")
        ]
        return {
            "replacement_char_chunks": sum(1 for text in texts if "\ufffd" in text),
            "control_char_chunks": sum(1 for text in texts if control_pattern.search(text)),
            "context_header_chunks": sum(1 for text in texts if text.startswith("[위치]")),
            "hwp_mojibake_artifact_chunks": sum(1 for text in normalized_texts if HWP_ARTIFACT_PATTERN.search(text)),
            "hwp_mojibake_artifact_char_count": sum(len(HWP_ARTIFACT_PATTERN.findall(text)) for text in normalized_texts),
            "private_use_char_chunks": sum(1 for text in normalized_texts if PRIVATE_USE_PATTERN.search(text)),
            "private_use_char_count": sum(len(PRIVATE_USE_PATTERN.findall(text)) for text in normalized_texts),
            "suspicious_regulation_metadata_count": sum(1 for value in metadata_values if HWP_ARTIFACT_PATTERN.search(value)),
        }

    def _coverage_metrics(self, source_text: str | None, chunks: list[Chunk]) -> dict[str, int | float]:
        source_chars = len(self._compact_text(source_text or ""))
        raw_chunk_chars = len(self._compact_text("\n".join(chunk.normalized_text or chunk.text for chunk in chunks)))
        source_coverage_chunks = [chunk for chunk in chunks if not self._source_coverage_exempt(chunk)]
        source_coverage_chunk_chars = len(
            self._compact_text("\n".join(chunk.normalized_text or chunk.text for chunk in source_coverage_chunks))
        )
        exempt_chunk_chars = max(0, raw_chunk_chars - source_coverage_chunk_chars)
        ratio = round(source_coverage_chunk_chars / source_chars, 3) if source_chars else 0.0
        raw_ratio = round(raw_chunk_chars / source_chars, 3) if source_chars else 0.0
        return {
            "source_compact_chars": source_chars,
            "chunk_compact_chars": raw_chunk_chars,
            "raw_chunk_to_source_char_ratio": raw_ratio,
            "source_coverage_chunk_compact_chars": source_coverage_chunk_chars,
            "source_coverage_exempt_chunk_count": len(chunks) - len(source_coverage_chunks),
            "source_coverage_exempt_chunk_compact_chars": exempt_chunk_chars,
            "chunk_to_source_char_ratio": ratio,
        }

    @staticmethod
    def _source_coverage_exempt(chunk: Chunk) -> bool:
        metadata = chunk.metadata or {}
        return bool(metadata.get("kordoc_table_promoted"))

    def _compact_text(self, value: str) -> str:
        return re.sub(r"\s+", "", value or "")

    def _profile(self, profile_id: str | None) -> QualityGateProfile:
        if profile_id:
            normalized = str(profile_id).strip().lower()
            profile = self.profiles.get(normalized)
            if profile is not None:
                return profile
            if self.strict_profile_ids:
                raise ValueError(f"Unknown quality profile_id: {profile_id}")
        return self.default_profile

    def _coverage_ratio_is_reasonable(
        self,
        coverage_metrics: dict[str, int | float],
        profile: QualityGateProfile,
        *,
        source_text_provided: bool = False,
    ) -> bool:
        if not source_text_provided:
            return True
        source_chars = int(coverage_metrics.get("source_compact_chars") or 0)
        chunk_chars = int(
            coverage_metrics.get("source_coverage_chunk_compact_chars")
            or coverage_metrics.get("chunk_compact_chars")
            or 0
        )
        if source_chars == 0:
            return chunk_chars == 0
        ratio = coverage_metrics.get("chunk_to_source_char_ratio")
        if ratio is None:
            return True
        numeric_ratio = float(ratio)
        if profile.coverage_ratio_min <= numeric_ratio <= profile.coverage_ratio_max:
            return True
        raw_ratio = float(coverage_metrics.get("raw_chunk_to_source_char_ratio") or 0.0)
        exempt_count = int(coverage_metrics.get("source_coverage_exempt_chunk_count") or 0)
        if exempt_count and numeric_ratio < profile.coverage_ratio_min and raw_ratio >= profile.coverage_ratio_min:
            return True
        return False

    def _table_false_positive_rate_is_reasonable(
        self,
        table_metrics: dict[str, int | float],
        chunk_count: int,
        profile: QualityGateProfile,
    ) -> bool:
        false_positive_count = int(table_metrics.get("table_false_positive_attention_chunks") or 0)
        if false_positive_count == 0:
            return True
        rate = false_positive_count / chunk_count if chunk_count else 0.0
        return (
            false_positive_count <= profile.table_false_positive_attention_max_count
            and rate <= profile.table_false_positive_attention_max_ratio
        )

    def _regulation_duplication_is_reasonable(self, structure_metrics: dict[str, int | float]) -> bool:
        unique_count = int(structure_metrics.get("unique_regulation_no_count") or 0)
        duplicate_count = int(structure_metrics.get("duplicate_regulation_node_count") or 0)
        if unique_count == 0:
            return True
        return duplicate_count <= unique_count * 0.5

    def _check(
        self,
        name: str,
        severity: str,
        passed: bool,
        value: int | float | str | bool | None,
        threshold: int | float | str | bool | None,
        message: str,
    ) -> QualityCheck:
        return QualityCheck(
            name=name,
            severity=severity,  # type: ignore[arg-type]
            passed=passed,
            value=value,
            threshold=threshold,
            message=message,
        )

    def _score(
        self,
        *,
        error_count: int,
        warning_count: int,
        duplicate_chunk_id_count: int,
        empty_chunk_count: int,
        missing_page_count: int,
        missing_required_metadata_count: int,
        replacement_char_chunks: int,
        table_like_without_cell_rows: int,
    ) -> float:
        score = 100.0
        score -= min(60, error_count * 20)
        score -= min(25, warning_count * 2)
        score -= min(40, duplicate_chunk_id_count * 10)
        score -= min(40, empty_chunk_count * 10)
        score -= min(20, missing_page_count * 0.5)
        score -= min(20, missing_required_metadata_count * 0.5)
        score -= min(20, replacement_char_chunks * 2)
        score -= min(10, table_like_without_cell_rows * 0.02)
        return round(max(0.0, score), 1)

    def _recommendations(
        self,
        checks: list[QualityCheck],
        metadata_coverage: dict[str, int],
        table_metrics: dict[str, int | float],
        structure_metrics: dict[str, int | float],
    ) -> list[str]:
        recommendations = [check.message for check in checks if not check.passed]
        if metadata_coverage.get("chunks_with_source_page_unavailable_reason", 0):
            recommendations.append("Some chunks explicitly lack parser source pages; verify their source location during human review.")
        if metadata_coverage.get("chunks_with_references", 0) == 0:
            recommendations.append("Reference extraction produced no metadata; inspect article and law reference patterns.")
        if table_metrics.get("table_like_chunks", 0) and not table_metrics.get("chunks_with_table_rows", 0):
            recommendations.append("Table-like chunks need row extraction before table-aware RAG can use them reliably.")
        if table_metrics.get("table_review_required_chunks", 0):
            recommendations.append("Table/appendix rows marked review_required should be checked before citation-grade RAG use.")
        if table_metrics.get("probable_table_extraction_failed_chunks", 0):
            recommendations.append("Probable table extraction failures should become table regression fixtures with expected rows.")
        if structure_metrics.get("regulation_node_count", 0) and not structure_metrics.get("chunks_with_regulation_metadata", 0):
            recommendations.append("Regulation boundaries were detected but were not propagated to chunk metadata.")
        return self._unique(recommendations)

    def _has_metadata_value(self, value) -> bool:
        if value is None:
            return False
        if isinstance(value, (list, dict, set, tuple)):
            return bool(value)
        return bool(value)

    @staticmethod
    def _source_page_unavailable_is_declared(chunk: Chunk) -> bool:
        return bool((chunk.metadata or {}).get("source_page_unavailable_reason"))

    def _unique(self, values: list[str]) -> list[str]:
        seen = set()
        result = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


def quality_report_to_json(report: QualityReport) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
