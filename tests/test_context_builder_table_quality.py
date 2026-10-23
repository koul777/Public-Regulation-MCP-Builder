from __future__ import annotations

from copy import deepcopy
import unittest

from app.rag.context_builder import ContextBuilder, ContextEvidence
from app.rag.evidence_quality import requires_table_structure_review, table_review_metadata
from app.services.regulation_rag_runtime import public_search_result


class TableQualityTests(unittest.TestCase):
    @staticmethod
    def record(chunk_id: str, **overrides) -> dict:
        return {"chunk_id": chunk_id, "document_id": "synthetic", "approval_status": "approved",
                "approval_id": "approval-" + chunk_id, "approved_content_hash": "hash-" + chunk_id,
                "regulation_title": "Synthetic Rule", "article_no": "Article 1", "chunk_type": "article",
                "text": "Approved source text.", **overrides}

    def test_gate_requires_exact_boolean_and_specific_flag(self) -> None:
        self.assertTrue(requires_table_structure_review({"table_review_required": True,
                                                        "table_review_flags": ["unstable_column_count"]}))
        for required in (False, None, 0, 1, "true", [], {}):
            with self.subTest(required=required):
                self.assertFalse(requires_table_structure_review({"table_review_required": required,
                                                                  "table_review_flags": ["unstable_column_count"]}))
        for flags in (None, "unstable_column_count", {"unstable_column_count": True}, ["possible_truncated_cell"], []):
            with self.subTest(flags=flags):
                self.assertFalse(requires_table_structure_review({"table_review_required": True,
                                                                  "table_review_flags": flags}))

    def test_nested_signals_cannot_be_hidden_by_top_level_false(self) -> None:
        item = {"table_review_required": False, "table_review_flags": [],
                "metadata": {"table_review_required": True, "table_review_flags": ["unstable_column_count"]}}
        self.assertTrue(requires_table_structure_review(item))

    def test_flags_are_only_strings_in_a_sequence(self) -> None:
        quality = table_review_metadata({"table_like": "false", "table_review_required": 1,
                                         "table_review_flags": [None, 0, {}, "row_review_required", "row_review_required"]})
        self.assertEqual({"table_like": False, "table_review_required": False,
                          "table_review_flags": ["row_review_required"]}, quality)

    def test_context_schema_does_not_coerce_string_or_integer_review_boolean(self) -> None:
        item = ContextBuilder().build([self.record("source")]).items[0].model_dump()
        for value in (1, "true", "false"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ContextEvidence.model_validate({**item, "table_review_required": value})

    def test_public_projection_preserves_only_verified_metadata_quality(self) -> None:
        record = {"text": "Approved body", "table_review_required": True,
                  "table_review_flags": ["untrusted_top_level"],
                  "metadata": {"table_like": True, "table_review_required": True,
                               "table_review_flags": ["unstable_column_count", None]}}
        original = deepcopy(record)
        result = public_search_result(record, 0.0)
        self.assertTrue(requires_table_structure_review(result))
        self.assertEqual(["unstable_column_count"], result["table_review_flags"])
        self.assertTrue(result["table_like"])
        self.assertEqual(original, record)

    def test_context_preserves_warning_separate_from_source_text(self) -> None:
        record = self.record("table", chunk_type="table", metadata={"table_like": True,
                             "table_review_required": True, "table_review_flags": ["unstable_column_count"]})
        original = deepcopy(record)
        context = ContextBuilder().build([record])
        item = context.items[0]
        self.assertTrue(requires_table_structure_review(item))
        self.assertEqual(("unstable_column_count",), item.table_review_flags)
        self.assertEqual(record["text"], item.text)
        self.assertIn("table_structure_review_required: true", context.prompt_context)
        self.assertIn("table_structure_review_required", context.review_flags)
        self.assertEqual(original, record)

    def test_legacy_and_normal_table_keep_existing_text(self) -> None:
        for extra in ({}, {"chunk_type": "table", "table_like": True, "table_review_required": False},
                      {"table_review_required": True, "table_review_flags": ["possible_truncated_cell"]}):
            with self.subTest(extra=extra):
                context = ContextBuilder().build([self.record("safe", **extra)])
                self.assertFalse(requires_table_structure_review(context.items[0]))
                self.assertNotIn("table_structure_review_required: true", context.prompt_context)
                self.assertEqual("Approved source text.", context.items[0].text)

    def test_same_scope_merge_preserves_or_and_flag_union(self) -> None:
        records = [self.record("first", table_like=True, table_review_flags=["row_review_required"]),
                   self.record("second", table_review_required=True,
                               table_review_flags=["unstable_column_count", "row_review_required"], text="Second source.")]
        context = ContextBuilder().build(records)
        self.assertEqual(1, len(context.items))
        self.assertTrue(requires_table_structure_review(context.items[0]))
        self.assertEqual(("row_review_required", "unstable_column_count"), context.items[0].table_review_flags)

    def test_duplicate_winner_cannot_erase_existing_warning(self) -> None:
        records = [self.record("first", approved_content_hash="same", score=1, table_review_required=True,
                               table_review_flags=["unstable_column_count"]),
                   self.record("second", approved_content_hash="same", score=2)]
        context = ContextBuilder().build(records)
        self.assertEqual(1, len(context.items))
        self.assertTrue(requires_table_structure_review(context.items[0]))

    def test_unrelated_article_does_not_inherit_table_warning(self) -> None:
        context = ContextBuilder().build([
            self.record("article"),
            self.record("table", chunk_type="table", table_review_required=True,
                        table_review_flags=["unstable_column_count"]),
        ])
        self.assertEqual(2, len(context.items))
        self.assertFalse(requires_table_structure_review(context.items[0]))
        self.assertTrue(requires_table_structure_review(context.items[1]))


if __name__ == "__main__":
    unittest.main()
