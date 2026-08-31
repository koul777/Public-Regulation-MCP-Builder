from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from app.processors.mojibake import (
    MOJIBAKE_REMOVED_BLOCKS_KEY,
    MOJIBAKE_REMOVED_CHARS_KEY,
    strip_mojibake_artifacts,
)
from app.processors.quality_gate import (
    QualityGate,
    QualityGateProfile,
    has_mojibake_artifacts,
    mojibake_artifact_char_count,
    load_quality_gate_profile_config,
    load_quality_gate_profile_config_from_bytes,
    load_quality_gate_profiles,
    quality_profile_config_to_bytes,
    save_quality_profile_config,
    upsert_quality_profile,
)
from app.schemas.chunk import Chunk
from app.schemas.structure import StructureNode
from app.schemas.validation import ValidationIssue


def article_node(node_id: str = "node_1") -> StructureNode:
    return StructureNode(
        node_id=node_id,
        document_id="doc_quality",
        node_type="article",
        number="\uc81c1\uc870",
        title="\ubaa9\uc801",
        text="\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38",
        page_start=1,
        page_end=1,
        order_index=0,
    )


def regulation_node(node_id: str, order_index: int, title: str = "\uc0d8\ud50c\uaddc\uc815") -> StructureNode:
    return StructureNode(
        node_id=node_id,
        document_id="doc_quality",
        node_type="regulation",
        number="1-1-1",
        title=title,
        text=f"1-1-1. {title}",
        page_start=order_index + 1,
        page_end=order_index + 1,
        order_index=order_index,
    )


def chunk(chunk_id: str = "chunk_1", text: str = "\uc81c1\uc870(\ubaa9\uc801) \ubcf8\ubb38") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="doc_quality",
        source_node_ids=["node_1"],
        chunk_type="article",
        text=text,
        metadata={
            "document_name": "\uc0d8\ud50c\uaddc\uc815",
            "source_file": "sample.pdf",
            "hierarchy_path": "\uc0d8\ud50c\uaddc\uc815 > \uc81c1\uc870",
            "chunk_type": "article",
            "references": [{"type": "article", "value": "\uc81c2\uc870"}],
            "article_refs": ["\uc81c2\uc870"],
            "table_like": True,
            "table_rows": [{"cells": ["\uad6c\ubd84", "\ub0b4\uc6a9"]}],
            "table_cell_rows": [{"row_index": 0, "cells": ["\uad6c\ubd84", "\ub0b4\uc6a9"], "raw": "\uad6c\ubd84 \ub0b4\uc6a9"}],
            "table_confidence": 0.8,
            "regulation_no": "1-1-1",
        },
        source_page_start=1,
        source_page_end=1,
    )


class QualityGateTests(unittest.TestCase):
    def test_passes_clean_result_and_counts_metadata(self) -> None:
        report = QualityGate().evaluate([article_node()], [chunk()], [], "doc_quality")

        self.assertTrue(report.passed)
        self.assertEqual(report.duplicate_chunk_id_count, 0)
        self.assertEqual(report.metadata_coverage["chunks_with_references"], 1)
        self.assertEqual(report.table_metrics["table_like_chunks"], 1)
        self.assertEqual(report.table_metrics["table_row_count"], 1)

    def test_fails_duplicate_chunk_ids_and_validation_errors(self) -> None:
        issue = ValidationIssue(
            issue_id="issue_1",
            document_id="doc_quality",
            severity="error",
            issue_type="sample_error",
            message="sample",
        )
        report = QualityGate().evaluate([article_node()], [chunk("dup"), chunk("dup")], [issue], "doc_quality")

        self.assertFalse(report.passed)
        self.assertEqual(report.duplicate_chunk_id_count, 1)
        self.assertTrue(any(check.name == "unique_chunk_ids" and not check.passed for check in report.checks))
        self.assertLess(report.score, 100)

    def test_flags_replacement_characters(self) -> None:
        report = QualityGate().evaluate([article_node()], [chunk(text="bad\ufffdtext")], [], "doc_quality")

        self.assertEqual(report.text_quality_metrics["replacement_char_chunks"], 1)
        self.assertTrue(any(check.name == "no_replacement_characters" and not check.passed for check in report.checks))
        self.assertEqual(report.validation_warning_count, 0)
        self.assertEqual(report.failed_warning_check_count, 1)
        self.assertEqual(report.warning_count, 1)

    def test_flags_hwp_mojibake_artifacts(self) -> None:
        report = QualityGate().evaluate([article_node()], [chunk(text="汤捯 본문"), chunk("artifact", text="湯湷 본문")], [], "doc_quality")

        self.assertEqual(report.text_quality_metrics["hwp_mojibake_artifact_chunks"], 2)
        self.assertTrue(any(check.name == "no_hwp_mojibake_artifacts" and not check.passed for check in report.checks))
        self.assertEqual(report.warning_count, 1)

    def test_flags_nonempty_structure_fallback_as_warning(self) -> None:
        fallback = Chunk(
            chunk_id="fallback",
            document_id="doc_quality",
            chunk_type="document",
            text="fallback body",
            normalized_text="fallback body",
            metadata={
                "document_name": "Fallback",
                "source_file": "fallback.hwp",
                "hierarchy_path": "Fallback",
                "chunk_type": "document",
                "structure_fallback": True,
            },
            warnings=["structure_fallback_document_chunk"],
            source_page_start=1,
            source_page_end=1,
        )

        report = QualityGate().evaluate([], [fallback], [], "doc_quality", "fallback body")

        self.assertEqual(report.structure_metrics["structure_fallback_chunk_count"], 1)
        self.assertEqual(report.structure_metrics["nonempty_source_without_structure"], 1)
        self.assertTrue(any(check.name == "structured_nodes_present" and not check.passed for check in report.checks))
        self.assertEqual(report.warning_count, 1)

    def test_private_use_characters_are_info_only(self) -> None:
        report = QualityGate().evaluate([article_node()], [chunk(text="\uf0b1 항목")], [], "doc_quality")

        self.assertEqual(report.text_quality_metrics["private_use_char_chunks"], 1)
        self.assertTrue(any(check.name == "private_use_characters_observed" and not check.passed for check in report.checks))
        self.assertEqual(report.warning_count, 0)

    def test_counts_failed_warning_checks_in_summary(self) -> None:
        raw_only = chunk("raw_only")
        raw_only.metadata["table_cell_rows"] = []
        raw_only.metadata["table_rows"] = ["\uc2e0\uccad\uc790 \uc131\uba85"]

        report = QualityGate().evaluate([article_node()], [raw_only], [], "doc_quality")

        self.assertTrue(report.passed)
        self.assertEqual(report.validation_warning_count, 0)
        self.assertEqual(report.failed_warning_check_count, 1)
        self.assertEqual(report.warning_count, 1)
        self.assertTrue(any(check.name == "table_rows_when_table_like" and not check.passed for check in report.checks))

    def test_page_number_missing_warning_is_not_double_counted_in_score_penalty(self) -> None:
        item = chunk("missing_page")
        item.source_page_start = None
        item.source_page_end = None
        issue = ValidationIssue(
            issue_id="issue_page",
            document_id="doc_quality",
            target_id="missing_page",
            severity="warning",
            issue_type="page_number_missing",
            message="source_page_start is missing",
        )

        report = QualityGate().evaluate([article_node()], [item], [issue], "doc_quality")

        self.assertEqual(report.validation_warning_count, 1)
        self.assertEqual(report.failed_warning_check_count, 1)
        self.assertEqual(report.warning_count, 2)
        self.assertEqual(report.missing_page_count, 1)
        self.assertEqual(97.5, report.score)

    def test_declared_unavailable_source_page_is_not_counted_as_missing_page(self) -> None:
        item = chunk("declared_unavailable_page")
        item.source_page_start = None
        item.source_page_end = None
        item.metadata["source_page_unavailable_reason"] = "kordoc_table_source_page_missing"
        item.metadata["source_page_unavailable_parser"] = "kordoc"

        report = QualityGate().evaluate([article_node()], [item], [], "doc_quality")

        self.assertEqual(0, report.missing_page_count)
        self.assertEqual(0, report.failed_warning_check_count)
        self.assertEqual(1, report.metadata_coverage["chunks_with_source_page_unavailable_reason"])
        self.assertEqual(100.0, report.score)
        self.assertIn(
            "Some chunks explicitly lack parser source pages; verify their source location during human review.",
            report.recommendations,
        )

    def test_counts_table_review_required_and_citation_ready_metrics(self) -> None:
        item = chunk("review_required_table")
        item.chunk_type = "appendix"
        item.metadata["hierarchy_path"] = "샘플규정 > 별표 1 재산 평가표"
        item.metadata["table_citation_label"] = "별표1 재산 평가표"
        item.metadata["table_review_required"] = True
        item.metadata["table_review_flags"] = ["row_review_required"]
        item.metadata["table_cell_rows"] = [
            {
                "row_index": 1,
                "cells": ["서에제출", "법무담당부서"],
                "raw": "서에제출 법무담당부서",
                "review_required": True,
                "row_quality_flags": ["possible_truncated_cell"],
            }
        ]

        report = QualityGate().evaluate([article_node()], [item], [], "doc_quality")

        self.assertEqual(report.table_metrics["table_review_required_chunks"], 1)
        self.assertEqual(report.table_metrics["table_review_required_row_count"], 1)
        self.assertEqual(report.table_metrics["table_citation_ready_chunks"], 1)
        self.assertEqual(report.table_metrics["appendix_table_like_chunks"], 1)
        self.assertIn(
            "Table/appendix rows marked review_required should be checked before citation-grade RAG use.",
            report.recommendations,
        )

    def test_stable_table_false_positive_demotions_are_not_attention_failures(self) -> None:
        chunks = []
        for index in range(8):
            item = chunk(f"stable_false_positive_{index}", text="□ 긴 문장형 예산 지침은 표가 아니다.")
            item.metadata["table_like"] = False
            item.metadata["table_classification"] = "probable_false_positive_budget_prose"
            item.metadata["table_probable_false_positive"] = True
            item.metadata["table_false_positive_stability"] = "stable"
            item.metadata["table_cell_rows"] = []
            chunks.append(item)

        report = QualityGate().evaluate([article_node()], chunks, [], "doc_quality")

        self.assertEqual(report.table_metrics["probable_table_false_positive_chunks"], 8)
        self.assertEqual(report.table_metrics["stable_table_false_positive_chunks"], 8)
        self.assertEqual(report.table_metrics["table_false_positive_attention_chunks"], 0)
        self.assertTrue(any(check.name == "table_false_positive_attention" and check.passed for check in report.checks))
        self.assertEqual(report.failed_warning_check_count, 0)

    def test_unstable_table_false_positive_demotions_remain_attention_failures(self) -> None:
        chunks = []
        for index in range(8):
            item = chunk(f"attention_false_positive_{index}", text="표 후보로 보였지만 아직 안정화되지 않은 샘플")
            item.metadata["table_like"] = False
            item.metadata["table_classification"] = "probable_false_positive_unknown"
            item.metadata["table_probable_false_positive"] = True
            item.metadata["table_false_positive_stability"] = "attention"
            item.metadata["table_cell_rows"] = []
            chunks.append(item)

        report = QualityGate().evaluate([article_node()], chunks, [], "doc_quality")

        self.assertEqual(report.table_metrics["probable_table_false_positive_chunks"], 8)
        self.assertEqual(report.table_metrics["stable_table_false_positive_chunks"], 0)
        self.assertEqual(report.table_metrics["table_false_positive_attention_chunks"], 8)
        self.assertTrue(any(check.name == "table_false_positive_attention" and not check.passed for check in report.checks))
        self.assertEqual(report.warning_count, 0)

    def test_missing_required_metadata_counts_chunks_and_fields_separately(self) -> None:
        partial = Chunk(
            chunk_id="partial",
            document_id="doc_quality",
            chunk_type="form",
            text="\ubcf8\ubb38",
            metadata={"document_name": "\uc0d8\ud50c", "table_like": False},
            source_page_start=1,
            source_page_end=1,
        )

        report = QualityGate().evaluate([article_node()], [partial], [], "doc_quality")

        self.assertEqual(report.missing_required_metadata_count, 1)
        self.assertEqual(report.missing_required_metadata_field_count, 3)
        self.assertEqual(report.failed_warning_check_count, 1)

    def test_flags_duplicate_regulation_boundaries(self) -> None:
        report = QualityGate().evaluate(
            [regulation_node("reg_1", 0), regulation_node("reg_2", 1)],
            [chunk()],
            [],
            "doc_quality",
        )

        self.assertEqual(report.structure_metrics["duplicate_regulation_node_count"], 1)
        self.assertTrue(any(check.name == "regulation_boundary_duplication" and not check.passed for check in report.checks))

    def test_material_title_variant_is_reported_separately_from_duplicate_boundary(self) -> None:
        report = QualityGate().evaluate(
            [
                regulation_node("reg_1", 0, "\uc5f0\uad6c\uc724\ub9ac\uaddc\uc815"),
                regulation_node("reg_2", 1, "\uc0ac\uc5c5\ub2e8\uc7a5 \uc5f0\uad6c\uc724\ub9ac\uaddc\uc815"),
            ],
            [chunk()],
            [],
            "doc_quality",
        )

        self.assertEqual(report.structure_metrics["duplicate_regulation_node_count"], 0)
        self.assertEqual(report.structure_metrics["regulation_title_variant_count"], 1)
        self.assertTrue(any(check.name == "regulation_boundary_duplication" and check.passed for check in report.checks))

    def test_flags_missing_article_regulation_metadata(self) -> None:
        article_chunk = chunk("article_without_reg")
        article_chunk.metadata.pop("regulation_no", None)

        report = QualityGate().evaluate([regulation_node("reg_1", 0), article_node()], [article_chunk], [], "doc_quality")

        self.assertEqual(report.structure_metrics["article_chunks_missing_regulation_no"], 1)
        self.assertEqual(report.structure_metrics["detected_reg_no_without_chunk_metadata_count"], 1)
        self.assertTrue(any(check.name == "article_regulation_metadata_present" and not check.passed for check in report.checks))
        self.assertTrue(any(check.name == "detected_regulations_reach_chunks" and not check.passed for check in report.checks))

    def test_custom_profile_tightens_coverage_threshold(self) -> None:
        item = chunk(text="aaaaaaaaaaaa")
        default_report = QualityGate().evaluate([article_node()], [item], [], "doc_quality", source_text="aaaaaaaaaa")
        strict_report = QualityGate(
            default_profile=QualityGateProfile(coverage_ratio_min=0.95, coverage_ratio_max=1.05)
        ).evaluate([article_node()], [item], [], "doc_quality", source_text="aaaaaaaaaa")

        self.assertTrue(any(check.name == "chunk_source_coverage" and check.passed for check in default_report.checks))
        self.assertTrue(any(check.name == "chunk_source_coverage" and not check.passed for check in strict_report.checks))
        self.assertEqual(strict_report.warning_count, 1)

    def test_kordoc_unmatched_table_is_excluded_from_source_coverage_ratio(self) -> None:
        source_chunk = chunk("source_backed", text="aaaaaaaaaa")
        kordoc_only = chunk("kordoc_only", text="bbbbbbbbbbbbbbbbbbbb")
        kordoc_only.source_node_ids = []
        kordoc_only.chunk_type = "table"
        kordoc_only.source_page_start = None
        kordoc_only.source_page_end = None
        kordoc_only.metadata["chunk_type"] = "table"
        kordoc_only.metadata["table_source"] = "kordoc"
        kordoc_only.metadata["kordoc_table_promoted"] = True
        kordoc_only.metadata["kordoc_table_unmatched_source"] = True
        kordoc_only.metadata["source_page_unavailable_reason"] = "kordoc_table_source_page_missing"

        report = QualityGate().evaluate(
            [article_node()],
            [source_chunk, kordoc_only],
            [],
            "doc_quality",
            source_text="aaaaaaaaaa",
        )

        coverage_check = next(check for check in report.checks if check.name == "chunk_source_coverage")
        self.assertTrue(coverage_check.passed)
        self.assertEqual(1.0, report.coverage_metrics["chunk_to_source_char_ratio"])
        self.assertEqual(3.0, report.coverage_metrics["raw_chunk_to_source_char_ratio"])
        self.assertEqual(1, report.coverage_metrics["source_coverage_exempt_chunk_count"])

    def test_kordoc_promoted_table_can_explain_low_adjusted_coverage_ratio(self) -> None:
        source_chunk = chunk("source_backed", text="aaa")
        kordoc_table = chunk("kordoc_promoted", text="bbbbbbbbbbbbbbbbbbbb")
        kordoc_table.chunk_type = "table"
        kordoc_table.metadata["chunk_type"] = "table"
        kordoc_table.metadata["table_source"] = "kordoc"
        kordoc_table.metadata["kordoc_table_promoted"] = True

        report = QualityGate().evaluate(
            [article_node()],
            [source_chunk, kordoc_table],
            [],
            "doc_quality",
            source_text="aaaaaaaaaa",
        )

        coverage_check = next(check for check in report.checks if check.name == "chunk_source_coverage")
        self.assertTrue(coverage_check.passed)
        self.assertEqual(0.3, report.coverage_metrics["chunk_to_source_char_ratio"])
        self.assertEqual(2.3, report.coverage_metrics["raw_chunk_to_source_char_ratio"])
        self.assertEqual(1, report.coverage_metrics["source_coverage_exempt_chunk_count"])

    def test_chunk_source_coverage_fails_when_source_text_is_empty(self) -> None:
        report = QualityGate().evaluate([article_node()], [chunk()], [], "doc_quality", source_text="")

        coverage_check = next(check for check in report.checks if check.name == "chunk_source_coverage")
        self.assertFalse(coverage_check.passed)
        self.assertEqual(coverage_check.value, 0.0)
        self.assertEqual(report.coverage_metrics["source_compact_chars"], 0)
        self.assertEqual(report.warning_count, 1)

    def test_profile_id_can_select_table_false_positive_thresholds(self) -> None:
        chunks = []
        for index in range(8):
            item = chunk(f"attention_profile_{index}", text="표 후보로 보였지만 아직 안정화되지 않은 샘플")
            item.metadata["table_like"] = False
            item.metadata["table_classification"] = "probable_false_positive_unknown"
            item.metadata["table_probable_false_positive"] = True
            item.metadata["table_false_positive_stability"] = "attention"
            item.metadata["table_cell_rows"] = []
            chunks.append(item)

        gate = QualityGate(
            profiles={
                "lenient-public": QualityGateProfile(
                    table_false_positive_attention_max_count=8,
                    table_false_positive_attention_max_ratio=1.0,
                )
            }
        )
        default_report = gate.evaluate([article_node()], chunks, [], "doc_quality")
        lenient_report = gate.evaluate([article_node()], chunks, [], "doc_quality", profile_id="lenient-public")

        self.assertTrue(any(check.name == "table_false_positive_attention" and not check.passed for check in default_report.checks))
        self.assertTrue(any(check.name == "table_false_positive_attention" and check.passed for check in lenient_report.checks))

    def test_unknown_profile_id_falls_back_by_default(self) -> None:
        report = QualityGate().evaluate([article_node()], [chunk()], [], "doc_quality", profile_id="typo")

        self.assertTrue(report.passed)

    def test_strict_profile_ids_reject_unknown_profile_id(self) -> None:
        gate = QualityGate(
            profiles={"known": QualityGateProfile()},
            strict_profile_ids=True,
        )

        with self.assertRaisesRegex(ValueError, "Unknown quality profile_id"):
            gate.evaluate([article_node()], [chunk()], [], "doc_quality", profile_id="typo")

    def test_loads_quality_profiles_from_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "default": {"coverage_ratio_min": 0.7, "coverage_ratio_max": 1.4},
                        "profiles": {
                            "strict": {
                                "coverage_ratio_min": 0.95,
                                "coverage_ratio_max": 1.05,
                                "table_false_positive_attention_max_count": 2,
                                "table_false_positive_attention_max_ratio": 0.05,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            default_profile, profiles = load_quality_gate_profiles(path)

        self.assertEqual(default_profile.coverage_ratio_min, 0.7)
        self.assertEqual(default_profile.coverage_ratio_max, 1.4)
        self.assertIn("strict", profiles)
        self.assertEqual(profiles["strict"].table_false_positive_attention_max_count, 2)

    def test_loads_quality_profile_config_and_hash_from_same_file_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            content = json.dumps({"profiles": {"strict": {"coverage_ratio_min": 0.95}}})
            path.write_text(content, encoding="utf-8")

            config = load_quality_gate_profile_config(path)

        self.assertIn("strict", config.profiles)
        self.assertEqual(len(config.sha256), 64)

    def test_quality_profile_config_bytes_round_trip_and_save_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(json.dumps({"profiles": {"old": {"coverage_ratio_min": 0.5}}}), encoding="utf-8")
            config = load_quality_gate_profile_config_from_bytes(
                json.dumps(
                    {
                        "default": {"coverage_ratio_min": 0.8, "coverage_ratio_max": 1.3},
                        "profiles": {"strict": {"coverage_ratio_min": 0.95, "coverage_ratio_max": 1.05}},
                    }
                ).encode("utf-8")
            )

            content = quality_profile_config_to_bytes(config)
            reloaded = load_quality_gate_profile_config_from_bytes(content)
            result = save_quality_profile_config(path, reloaded)

            self.assertEqual(reloaded.profiles["strict"].coverage_ratio_max, 1.05)
            self.assertTrue(Path(result["backup_path"]).exists())
            self.assertIn("old", Path(result["backup_path"]).read_text(encoding="utf-8"))
            self.assertEqual(result["sha256"], load_quality_gate_profile_config(path).sha256)

    def test_upsert_quality_profile_updates_default_and_named_profile(self) -> None:
        config = load_quality_gate_profile_config_from_bytes(json.dumps({"profiles": {}}).encode("utf-8"))

        updated_default = upsert_quality_profile(
            config,
            coverage_ratio_min=0.9,
            coverage_ratio_max=1.1,
            table_false_positive_attention_max_count=3,
            table_false_positive_attention_max_ratio=0.1,
            update_default=True,
        )
        updated = upsert_quality_profile(
            updated_default,
            "strict-public",
            coverage_ratio_min=0.95,
            coverage_ratio_max=1.05,
            table_false_positive_attention_max_count=2,
            table_false_positive_attention_max_ratio=0.05,
        )

        self.assertEqual(updated.default_profile.coverage_ratio_min, 0.9)
        self.assertEqual(updated.profiles["strict-public"].table_false_positive_attention_max_count, 2)
        self.assertEqual(len(updated.sha256), 64)

    def test_upsert_quality_profile_rejects_invalid_thresholds(self) -> None:
        config = load_quality_gate_profile_config_from_bytes(json.dumps({"profiles": {}}).encode("utf-8"))

        with self.assertRaisesRegex(ValueError, "coverage_ratio_min"):
            upsert_quality_profile(config, "bad", coverage_ratio_min=1.2, coverage_ratio_max=0.8)

    def test_quality_profile_config_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(json.dumps({"default": {"unknown": 1}}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown fields"):
                load_quality_gate_profiles(path)

    def test_quality_profile_config_rejects_whitespace_profile_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(json.dumps({"profiles": {" strict ": {}}}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "leading or trailing whitespace"):
                load_quality_gate_profiles(path)

    def test_quality_profile_config_rejects_normalized_id_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(json.dumps({"profiles": {"strict": {}, "STRICT": {}}}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "collide"):
                load_quality_gate_profiles(path)

    def test_quality_profile_rejects_invalid_thresholds(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage_ratio_min"):
            QualityGateProfile(coverage_ratio_min=1.2, coverage_ratio_max=0.8)
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            QualityGateProfile(table_false_positive_attention_max_ratio=1.5)


class MojibakeDetectionTests(unittest.TestCase):
    """UTF-16 바이트쌍 깨짐과 불가능한 문자 계열만 잡고, 정상 한자는 건드리지 않는지."""

    def test_detects_utf16_byte_pair_mojibake_runs(self) -> None:
        # "edeq" 가 UTF-16 으로 잘못 읽혀 한자로 굳은 실제 사례.
        self.assertTrue(has_mojibake_artifacts("강사임용 등에 관한 규정 敤敱"))

    def test_universal_hwp_export_boilerplate_is_not_counted_as_damage(self) -> None:
        """慤桥·漠杳는 보관 문서 26건 전부에 있었다. 모두가 걸리는 신호는 신호가 아니다.

        지우기는 그대로 지우되, "이 원본은 손상됐다"는 경고에서는 뺀다.
        """
        for boilerplate in ("慤桥", "漠杳"):
            with self.subTest(boilerplate=boilerplate):
                self.assertFalse(has_mojibake_artifacts("강사임용 등에 관한 규정 " + boilerplate))
                # 지우는 대상에서는 빠지지 않는다.
                cleaned, damaged, skipped = strip_mojibake_artifacts("강사임용 등에 관한 규정 " + boilerplate)
                self.assertNotIn(boilerplate, cleaned)
                self.assertEqual(0, damaged)
                self.assertEqual(2, skipped)

    def test_real_damage_next_to_the_boilerplate_is_still_counted(self) -> None:
        self.assertTrue(has_mojibake_artifacts("강사임용 등에 관한 규정 慤桥 敤敱"))

    def test_keeps_legitimate_hanja_out_of_the_mojibake_count(self) -> None:
        # 단일 한자는 UTF-16 바이트가 ASCII 로 풀려도 정상 표기이므로 세지 않는다.
        for legitimate in (
            "학위(學位) 수여에 관한 규정",
            "사업(業) 예산",
            "인(印) 날인",
            "무(無) 기명",
            # 괄호 없이 단독으로 쓰이는 한자 표기. 정규화 단계가 이제 실제로 지우기
            # 때문에 여기서 놓치면 옛 규정문의 제목과 기관명이 사라진다.
            "제1장 總則",
            "細則",
            "本則",
            "韓國學中央研究院",
            "改正 2024. 1. 1.",
        ):
            with self.subTest(legitimate=legitimate):
                self.assertFalse(has_mojibake_artifacts(legitimate))

    def test_detects_impossible_scripts_in_regulation_titles(self) -> None:
        # 실제로 저장된 규정 제목에서 나온 깨짐. 지우는 대상에는 그대로 들어간다.
        for corrupted in ("지출예산집행 및 관리규정 杈 Ȁ", "학위수여 규정 Ā"):
            with self.subTest(corrupted=corrupted):
                cleaned, _damaged, cleaned_chars = strip_mojibake_artifacts(corrupted)
                self.assertNotIn("Ā", cleaned)
                self.assertGreater(cleaned_chars, 0)

    def test_layout_coordinate_leaks_are_cleaned_but_not_called_damage(self) -> None:
        """배치 좌표가 새어 나온 것은 군더더기다. 지우면 끝나므로 사람이 볼 일이 없다.

        실측 26건에서 이 부류가 5건이었고, 모두 서명란·양식 칸 옆이었다.
        """
        signature_line = "소속 : ྠ Ā ྠ Ā 성명 : (서명)"

        self.assertFalse(has_mojibake_artifacts(signature_line))
        cleaned, damaged, cleaned_chars = strip_mojibake_artifacts(signature_line)
        self.assertNotIn("ྠ", cleaned)
        self.assertEqual(0, damaged)
        self.assertEqual(4, cleaned_chars)

    def test_an_object_name_standing_in_for_content_is_damage(self) -> None:
        """수식 개체가 제 내용 대신 내부 이름으로 읽히면 원래 있던 것이 사라진다.

        실제 성과급 세칙에서 "기본연봉 敤敱 지급률"처럼 곱셈 기호가 없어져,
        지우고 나면 곱한다는 사실 자체가 남지 않았다.
        """
        formula = "1. 원장: 기본연봉 敤敱 경영평가 결과에 따른 지급률"

        self.assertTrue(has_mojibake_artifacts(formula))
        _cleaned, damaged, _cleaned_chars = strip_mojibake_artifacts(formula)
        self.assertEqual(2, damaged)

    def test_leaves_normal_korean_regulation_text_alone(self) -> None:
        for clean in (
            "제1조(목적) 이 규정은 인사에 관한 사항을 정함을 목적으로 한다.",
            "① 면적은 100㎡ 이하로 한다.",
            "α·β 계수를 적용한다.",
            "㈜ 상임위원회",
        ):
            with self.subTest(clean=clean):
                self.assertFalse(has_mojibake_artifacts(clean))
                self.assertEqual(0, mojibake_artifact_char_count(clean))

    def test_mojibake_lowers_the_quality_score_directly(self) -> None:
        def _report(text: str):
            node = StructureNode(
                node_id="n1",
                document_id="doc-mojibake",
                node_type="article",
                number="1",
                title="목적",
                text=text,
                order_index=0,
                page_start=1,
            )
            chunk = Chunk(
                chunk_id="c1",
                document_id="doc-mojibake",
                source_node_ids=["n1"],
                chunk_type="article",
                text=text,
                normalized_text=text,
                source_page_start=1,
                metadata={
                    "document_name": "인사규정",
                    "source_file": "인사규정.hwp",
                    "hierarchy_path": "인사규정 > 제1조",
                    "chunk_type": "article",
                },
            )
            return QualityGate().evaluate([node], [chunk], [], "doc-mojibake", source_text=text)

        base = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다."
        clean = _report(base)
        # 문서마다 다른 진짜 깨짐만 점수에 반영한다(慤桥·漠杳는 모든 문서에 있는 상용구).
        corrupted = _report(base + " 敤敱 ྠĀ")

        self.assertEqual(0, clean.text_quality_metrics["hwp_mojibake_artifact_chunks"])
        self.assertEqual(1, corrupted.text_quality_metrics["hwp_mojibake_artifact_chunks"])
        # 敤敱(대체형 2자)만 손상으로 센다. ྠĀ는 지우면 끝나는 군더더기다.
        self.assertEqual(2, corrupted.text_quality_metrics["hwp_mojibake_artifact_char_count"])
        self.assertLess(corrupted.score, clean.score)

    def test_export_boilerplate_alone_does_not_lower_the_score(self) -> None:
        def _report(text: str):
            node = StructureNode(
                node_id="n1", document_id="doc-b", node_type="article", number="1",
                title="목적", text=text, order_index=0, page_start=1,
            )
            chunk = Chunk(
                chunk_id="c1", document_id="doc-b", source_node_ids=["n1"], chunk_type="article",
                text=text, normalized_text=text, retrieval_text=text, source_page_start=1,
                metadata={"document_name": "인사규정", "source_file": "인사규정.hwp",
                          "hierarchy_path": "인사규정 > 제1조", "chunk_type": "article"},
            )
            return QualityGate().evaluate([node], [chunk], [], "doc-b", source_text=text)

        base = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다."

        self.assertEqual(_report(base).score, _report(base + " 慤桥 漠杳").score)


    def test_keeps_warning_after_the_normalizer_already_removed_the_damage(self) -> None:
        """정규화가 깨진 글자를 지운 뒤에도 손상 사실이 남아야 한다.

        정규화는 청크보다 먼저 돌기 때문에, 지우고 나면 청크를 아무리 뒤져도
        원본이 손상됐다는 근거가 없다. 표·수식처럼 내용이 통째로 빠진 자리를
        사람이 확인해야 하므로 여기서 조용히 통과시키면 안 된다.
        """
        text = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다."
        node = StructureNode(
            node_id="n1",
            document_id="doc-cleaned",
            node_type="article",
            number="1",
            title="목적",
            text=text,
            order_index=0,
            page_start=1,
        )
        chunk = Chunk(
            chunk_id="c1",
            document_id="doc-cleaned",
            source_node_ids=["n1"],
            chunk_type="article",
            text=text,
            normalized_text=text,
            source_page_start=1,
            metadata={
                "document_name": "인사규정",
                "source_file": "인사규정.hwp",
                "hierarchy_path": "인사규정 > 제1조",
                "chunk_type": "article",
            },
        )

        def _check(report):
            return next(c for c in report.checks if c.name == "no_hwp_mojibake_artifacts")

        clean = QualityGate().evaluate([node], [chunk], [], "doc-cleaned", source_text=text)
        cleaned_up = QualityGate().evaluate(
            [node],
            [chunk],
            [],
            "doc-cleaned",
            source_text=text,
            normalizer_metadata={
                MOJIBAKE_REMOVED_CHARS_KEY: 12,
                MOJIBAKE_REMOVED_BLOCKS_KEY: 3,
            },
        )

        # 본문에는 깨진 글자가 남아 있지 않다.
        self.assertEqual(0, cleaned_up.text_quality_metrics["hwp_mojibake_artifact_chunks"])
        # 그래도 지운 기록이 남아 경고가 살아 있다.
        self.assertEqual(12, cleaned_up.text_quality_metrics["mojibake_removed_char_count"])
        self.assertEqual(3, cleaned_up.text_quality_metrics["mojibake_removed_block_count"])
        self.assertTrue(_check(clean).passed)
        self.assertFalse(_check(cleaned_up).passed)
        self.assertLess(cleaned_up.score, clean.score)

    def test_reports_no_removals_when_the_normalizer_metadata_is_absent(self) -> None:
        # 예전 실행 기록에는 이 값이 없다. 없다고 해서 손상됐다고 단정하면 안 된다.
        text = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다."
        node = StructureNode(
            node_id="n1",
            document_id="doc-legacy",
            node_type="article",
            number="1",
            title="목적",
            text=text,
            order_index=0,
            page_start=1,
        )
        chunk = Chunk(
            chunk_id="c1",
            document_id="doc-legacy",
            source_node_ids=["n1"],
            chunk_type="article",
            text=text,
            normalized_text=text,
            source_page_start=1,
            metadata={
                "document_name": "인사규정",
                "source_file": "인사규정.hwp",
                "hierarchy_path": "인사규정 > 제1조",
                "chunk_type": "article",
            },
        )

        report = QualityGate().evaluate([node], [chunk], [], "doc-legacy", source_text=text)

        self.assertEqual(0, report.text_quality_metrics["mojibake_removed_char_count"])
        self.assertEqual(0, report.text_quality_metrics["mojibake_removed_block_count"])
        self.assertTrue(
            next(c for c in report.checks if c.name == "no_hwp_mojibake_artifacts").passed
        )


class QualityGateContentCoverageTests(unittest.TestCase):
    """표 서식 문자가 커버리지를 부풀려 본문 누락을 가리는 문제를 관측한다.

    실측 228건에서 청크 글자 수가 원문 대비 100~119%로 나왔는데, 그 초과분이
    ``|``·괘선 같은 표 서식이었다. 부풀린 만큼이 완충재가 되어 본문이 빠져도
    비율이 떨어지지 않는다.
    """

    def _report(self, chunk_text: str, source_text: str):
        node = StructureNode(
            node_id="n1",
            document_id="doc-cov",
            node_type="article",
            number="1",
            title="목적",
            text=source_text,
            order_index=0,
            page_start=1,
        )
        chunk = Chunk(
            chunk_id="c1",
            document_id="doc-cov",
            source_node_ids=["n1"],
            chunk_type="article",
            text=chunk_text,
            normalized_text=chunk_text,
            source_page_start=1,
            metadata={
                "document_name": "인사규정",
                "source_file": "인사규정.hwp",
                "hierarchy_path": "인사규정 > 제1조",
                "chunk_type": "article",
            },
        )
        return QualityGate().evaluate([node], [chunk], [], "doc-cov", source_text=source_text)

    def test_table_scaffolding_no_longer_counts_as_covered_content(self) -> None:
        source = "제1조(목적) 구분 내용 가 나"
        # 같은 내용을 마크다운 표로 담으면 서식 문자가 잔뜩 붙는다.
        chunked = "제1조(목적)\n| 구분 | 내용 |\n| --- | --- |\n| 가 | 나 |"
        report = self._report(chunked, source)
        metrics = report.coverage_metrics

        # 기존 지표는 서식 문자까지 세어 원문보다 많다고 본다.
        self.assertGreater(metrics["raw_chunk_to_source_char_ratio"], 1.0)
        # 새 지표는 내용만 세므로 부풀지 않는다.
        self.assertLessEqual(metrics["content_to_source_char_ratio"], 1.0)

    def test_content_ratio_drops_when_body_text_is_actually_lost(self) -> None:
        source = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다. 제2조(적용) 모든 직원에게 적용한다."
        # 제2조가 통째로 빠졌는데, 표 서식이 그 자리를 채워 글자 수만 맞춘 경우.
        chunked = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다.\n|---|---|---|---|---|---|---|---|"
        metrics = self._report(chunked, source)

        self.assertGreater(metrics.coverage_metrics["raw_chunk_to_source_char_ratio"], 0.9)
        self.assertLess(metrics.coverage_metrics["content_to_source_char_ratio"], 0.7)

    def test_plain_text_document_is_unaffected(self) -> None:
        text = "제1조(목적) 이 규정은 인사에 관한 사항을 정한다."
        metrics = self._report(text, text).coverage_metrics

        self.assertEqual(
            metrics["raw_chunk_to_source_char_ratio"], metrics["content_to_source_char_ratio"]
        )

    def test_coverage_reuses_compacted_source_and_chunk_text(self) -> None:
        gate = QualityGate()
        chunks = [
            chunk("chunk-a", text="제1조(목적) 본문"),
            chunk("chunk-b", text="제2조(적용) 본문"),
        ]
        source_line_metrics = {
            "source_lines_checked": 0,
            "source_lines_missing_count": 0,
            "source_line_coverage_ratio": 1.0,
        }

        with patch.object(
            gate,
            "_source_line_metrics",
            return_value=source_line_metrics,
        ), patch.object(gate, "_compact_text", wraps=gate._compact_text) as compact_text:
            report = gate.evaluate(
                [article_node()],
                chunks,
                [],
                "doc_quality",
                source_text="제1조(목적) 본문 제2조(적용) 본문",
            )

        self.assertEqual(2, compact_text.call_count)
        self.assertEqual(
            report.coverage_metrics["chunk_compact_chars"],
            report.coverage_metrics["source_coverage_chunk_compact_chars"],
        )

    def test_source_line_metrics_reuses_precomputed_eligible_lines(self) -> None:
        gate = QualityGate()
        chunks = [
            chunk("chunk-a", text="제1조(목적) 적용 대상과 기준 본문"),
            chunk("chunk-b", text="제2조(적용) 처리 절차와 예외 본문"),
        ]

        with patch.object(gate, "_missing_source_lines", wraps=gate._missing_source_lines) as missing_lines:
            metrics = gate._source_line_metrics(
                "제1조(목적) 적용 대상과 기준 본문\n제2조(적용) 처리 절차와 예외 본문",
                chunks,
            )

        self.assertEqual(2, metrics["source_lines_checked"])
        self.assertEqual(0, metrics["source_lines_missing_count"])
        self.assertIn("eligible_lines", missing_lines.call_args.kwargs)
        self.assertEqual(2, len(missing_lines.call_args.kwargs["eligible_lines"]))

    def test_missing_source_lines_can_use_caller_precomputed_eligible_lines(self) -> None:
        gate = QualityGate()
        source_text = "제1조(목적) 적용 대상과 기준 본문\n제2조(적용) 처리 절차와 예외 누락"
        chunks = [chunk("chunk-a", text="제1조(목적) 적용 대상과 기준 본문")]
        eligible_lines = gate._eligible_source_lines(source_text)

        with patch.object(
            gate,
            "_eligible_source_lines",
            side_effect=AssertionError("eligible source lines should be reused"),
        ):
            missing = gate._missing_source_lines(
                source_text,
                chunks,
                eligible_lines=eligible_lines,
            )

        self.assertEqual(["제2조(적용) 처리 절차와 예외 누락"], missing)


class QualitySourceLineCoverageTests(unittest.TestCase):
    """별표·별지의 산문이 통째로 빠져도 글자 수 비율은 통과시켰다.

    실제 문서에서 "※ … 평균 90점 이상이 된 후보자를 인사위원회 심의 대상자로
    선정함." 같은 심사 기준이 색인 본문에서 사라졌는데 품질 점수는 92점이었다.
    줄 단위로 대조해야 이 손실이 드러난다.
    """

    def _report(self, chunk_texts: list[str], source_text: str):
        node = StructureNode(
            node_id="n1",
            document_id="doc-line",
            node_type="article",
            number="1",
            title="목적",
            text=source_text,
            order_index=0,
            page_start=1,
        )
        chunks = [
            Chunk(
                chunk_id=f"c{index}",
                document_id="doc-line",
                source_node_ids=["n1"],
                chunk_type="appendix",
                text=text,
                normalized_text=text,
                retrieval_text=text,
                source_page_start=1,
                metadata={
                    "document_name": "표창세칙",
                    "source_file": "표창세칙.hwp",
                    "hierarchy_path": "표창세칙 > 별표1",
                    "chunk_type": "appendix",
                },
            )
            for index, text in enumerate(chunk_texts, start=1)
        ]
        return QualityGate().evaluate([node], chunks, [], "doc-line", source_text=source_text)

    def _check(self, report):
        return next(c for c in report.checks if c.name == "source_lines_reach_indexed_text")

    def test_appendix_prose_dropped_beside_the_table_is_reported(self) -> None:
        source = (
            "[별표 1]\n"
            "| 항목 | 배점 |\n"
            "※ 심사에 참여하는 심사위원 전원으로부터 80점 이상의 점수를 받고, "
            "평균 90점 이상이 된 후보자를 인사위원회 심의 대상자로 선정함.\n"
        )
        # 표만 남기고 ※ 단서를 버린 청크.
        report = self._report(["[본문]\n| 항목 | 배점 |"], source)

        self.assertFalse(self._check(report).passed)
        self.assertEqual(1, report.coverage_metrics["source_lines_missing_count"])
        self.assertLess(report.coverage_metrics["source_line_coverage_ratio"], 1.0)

    def test_a_document_that_keeps_every_line_passes(self) -> None:
        source = "제1조(목적) 이 규정은 표창에 관한 사항을 정한다.\n제2조(적용) 모든 교직원에게 적용한다."
        report = self._report([source], source)

        self.assertTrue(self._check(report).passed)
        self.assertEqual(0, report.coverage_metrics["source_lines_missing_count"])
        self.assertEqual(1.0, report.coverage_metrics["source_line_coverage_ratio"])

    def test_repeated_present_lines_keep_per_occurrence_metrics(self) -> None:
        line = "Article one purpose clause remains present in indexed chunk text."
        source = "\n".join([line, line, line])
        report = self._report([line], source)

        self.assertTrue(self._check(report).passed)
        self.assertEqual(3, report.coverage_metrics["source_lines_checked"])
        self.assertEqual(0, report.coverage_metrics["source_lines_missing_count"])
        self.assertEqual(1.0, report.coverage_metrics["source_line_coverage_ratio"])

    def test_repeated_normalized_missing_lines_keep_counts_and_samples(self) -> None:
        source_lines = [
            "Repeated missing source clause alpha beta gamma.",
            "Repeatedmissingsourceclausealphabetagamma.",
            "  Repeated missing source clause alpha beta gamma.  ",
        ]
        source = "\n".join(source_lines)
        gate = QualityGate()
        report = self._report(["Unrelated indexed content that is long enough for comparison."], source)

        self.assertFalse(self._check(report).passed)
        self.assertEqual(3, report.coverage_metrics["source_lines_checked"])
        self.assertEqual(3, report.coverage_metrics["source_lines_missing_count"])
        self.assertEqual(0.0, report.coverage_metrics["source_line_coverage_ratio"])
        self.assertEqual(
            [line.strip() for line in source_lines],
            gate._missing_source_lines(source, []),
        )

    def test_line_split_across_two_chunks_is_not_a_false_alarm(self) -> None:
        source = "제1조(목적) 이 규정은 교직원 표창에 관한 사항을 정함을 목적으로 한다."
        report = self._report(["제1조(목적) 이 규정은 교직원 표창에", "관한 사항을 정함을 목적으로 한다."], source)

        self.assertTrue(self._check(report).passed)

    def test_missing_lines_do_not_block_approval(self) -> None:
        """경고이지 오류가 아니다. 사람이 보고 판단할 일이지 자동 반려할 일이 아니다."""
        source = "제1조(목적) 이 규정은 표창에 관한 사항을 정한다.\n※ 심사 기준은 별표와 같다. 평균 90점 이상."
        report = self._report(["제1조(목적) 이 규정은 표창에 관한 사항을 정한다."], source)

        self.assertFalse(self._check(report).passed)
        self.assertEqual("warning", self._check(report).severity)
        self.assertTrue(report.passed)

    def test_a_line_split_across_chunks_by_the_context_header_is_not_a_false_alarm(self) -> None:
        """조문이 ①에서 갈리면 청크 사이에 "[위치] … [본문]" 머리말이 끼어든다.

        멀쩡히 색인된 줄을 누락으로 신고하면 운영자가 이 경고를 믿지 않게 된다.
        """
        source = "제31조(가족수당) ① 부양가족이 있는 교직원에게는 월 40,000원의 가족수당을 지급한다."
        report = self._report(
            [
                "[위치] 보수규정 > 제31조\n[본문]\n제31조(가족수당)",
                "[위치] 보수규정 > 제31조 > ①\n[본문]\n① 부양가족이 있는 교직원에게는 월 40,000원의 가족수당을 지급한다.",
            ],
            source,
        )

        self.assertTrue(self._check(report).passed)
        self.assertEqual(0, report.coverage_metrics["source_lines_missing_count"])

    def test_content_only_in_the_context_header_still_counts_as_indexed(self) -> None:
        source = "교직원 보수규정 시행세칙 전문"
        report = self._report(["[위치] 교직원 보수규정 시행세칙 전문\n[본문]\n제1조(목적) 보수를 정한다."], source)

        self.assertTrue(self._check(report).passed)

    def test_a_table_row_promoted_to_markdown_is_not_reported_missing(self) -> None:
        """원문의 표 한 줄은 맨 글자, 색인 본문은 "| 값 | 값 |" 형태로 저장된다.

        구분선을 남겨 둔 채 대조하면 멀쩡히 색인된 표 줄이 전부 누락으로 잡혀, 별표·서식이
        있는 문서마다 없는 손실을 경고하고 점수까지 깎는다. 비율 검사와 같은 기준으로
        표 서식을 걷어내고 비교해야 한다.
        """
        source = (
            "수상종류 표창대상 포상금액 비고란\n"
            "장기근속상 20년 이상 근무한 직원 금 300만원 해당 없음"
        )
        markdown = (
            "| 수상종류 | 표창대상 | 포상금액 | 비고란 |\n"
            "| --- | --- | --- | --- |\n"
            "| 장기근속상 | 20년 이상 근무한 직원 | 금 300만원 | 해당 없음 |"
        )
        report = self._report([markdown], source)

        self.assertTrue(self._check(report).passed)
        self.assertEqual(0, report.coverage_metrics["source_lines_missing_count"])

    def test_prose_lost_beside_a_markdown_table_is_still_reported(self) -> None:
        """표 서식을 걷어내느라 표 바깥의 진짜 누락까지 놓치면 안 된다."""
        source = (
            "수상종류 표창대상 포상금액 비고란\n"
            "※ 심사위원 전원의 평균 90점 이상인 자를 대상자로 선정함."
        )
        report = self._report(["| 수상종류 | 표창대상 | 포상금액 | 비고란 |"], source)

        self.assertFalse(self._check(report).passed)
        self.assertEqual(1, report.coverage_metrics["source_lines_missing_count"])

    def test_real_loss_is_still_caught_after_the_false_alarm_fix(self) -> None:
        """오탐을 줄이느라 진짜 누락까지 놓치면 검사가 무의미해진다."""
        source = (
            "제31조(가족수당) ① 부양가족이 있는 교직원에게는 월 40,000원을 지급한다.\n"
            "※ 심사위원 전원으로부터 80점 이상을 받은 후보자를 심의 대상자로 선정함."
        )
        report = self._report(
            ["[위치] 보수규정 > 제31조\n[본문]\n제31조(가족수당) ① 부양가족이 있는 교직원에게는 월 40,000원을 지급한다."],
            source,
        )

        self.assertFalse(self._check(report).passed)
        self.assertEqual(1, report.coverage_metrics["source_lines_missing_count"])

    def test_short_lines_are_not_counted(self) -> None:
        # 짧은 줄은 다른 조문에 우연히 들어 있기 쉬워 신호가 되지 않는다.
        source = "제1조(목적) 이 규정은 표창에 관한 사항을 정한다.\n20 . . .\n(인)"
        report = self._report(["제1조(목적) 이 규정은 표창에 관한 사항을 정한다."], source)

        self.assertEqual(1, report.coverage_metrics["source_lines_checked"])
        self.assertTrue(self._check(report).passed)


if __name__ == "__main__":
    unittest.main()
