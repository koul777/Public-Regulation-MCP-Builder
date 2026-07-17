from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path

from app.processors.quality_gate import (
    QualityGate,
    QualityGateProfile,
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


if __name__ == "__main__":
    unittest.main()
