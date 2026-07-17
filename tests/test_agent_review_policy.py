from __future__ import annotations

import unittest
from pathlib import Path

from app.agents.review_policy import AgentReviewPolicy
from app.core.config import Settings
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.quality import QualityReport


def quality_report(score: float = 100.0, warnings: int = 0) -> QualityReport:
    return QualityReport(
        document_id="doc_review",
        passed=True,
        score=score,
        node_count=1,
        chunk_count=1,
        issue_count=0,
        error_count=0,
        warning_count=warnings,
        failed_error_check_count=0,
        failed_warning_check_count=warnings,
        duplicate_chunk_id_count=0,
        empty_chunk_count=0,
        missing_page_count=0,
        missing_required_metadata_count=0,
    )


def review_chunk(**metadata) -> Chunk:
    return Chunk(
        chunk_id="chunk_review",
        document_id="doc_review",
        source_node_ids=["node_1"],
        chunk_type="appendix",
        text="broken table text",
        normalized_text="broken table text",
        retrieval_text="[본문]\nbroken table text",
        metadata=metadata,
        source_page_start=1,
        source_page_end=1,
    )


class AgentReviewPolicyTests(unittest.TestCase):
    def test_missing_api_key_still_builds_required_review_draft(self) -> None:
        policy = AgentReviewPolicy(Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key=""))

        plan = policy.plan([review_chunk(table_like=True)], quality_report(warnings=1), ChunkOptions())

        self.assertTrue(plan["enabled"])
        self.assertTrue(plan["pipeline_stage_required"])
        self.assertFalse(plan["provider_execution_enabled"])
        self.assertEqual(plan["status"], "api_configuration_needed")
        self.assertEqual(plan["skip_reason"], "openai_api_key_missing")
        self.assertEqual(plan["selected_count"], 1)
        self.assertEqual(plan["mode"], "main_pipeline_review_draft")
        self.assertEqual(plan["api_call_count"], 0)

    def test_skips_clean_quality_even_when_enabled(self) -> None:
        policy = AgentReviewPolicy(Settings(data_dir=Path("data"), enable_agent_review=True))

        plan = policy.plan(
            [review_chunk(table_like=True, table_cell_rows=[{"cells": ["a"]}])],
            quality_report(),
            ChunkOptions(enable_agent_review=True),
        )

        self.assertTrue(plan["enabled"])
        self.assertEqual(plan["status"], "skipped")
        self.assertEqual(plan["skip_reason"], "quality_gate_clean")

    def test_request_option_cannot_disable_main_review_draft(self) -> None:
        policy = AgentReviewPolicy(Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key=""))

        plan = policy.plan([review_chunk(table_like=True)], quality_report(warnings=1), ChunkOptions(enable_agent_review=False))

        self.assertTrue(plan["enabled"])
        self.assertTrue(plan["settings_enabled"])
        self.assertTrue(plan["request_enabled"])
        self.assertEqual(plan["status"], "api_configuration_needed")
        self.assertEqual(plan["skip_reason"], "openai_api_key_missing")
        self.assertEqual(plan["selected_count"], 1)

    def test_disabled_api_keeps_draft_without_provider_execution(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=False,
                llm_provider="openai",
                openai_api_key="configured-but-unused",
            )
        )

        plan = policy.plan([review_chunk(table_like=True)], quality_report(warnings=1), ChunkOptions(enable_agent_review=True))

        self.assertEqual(plan["provider"], "openai")
        self.assertEqual(plan["model"], "gpt-4.1-mini")
        self.assertEqual(plan["mode"], "main_pipeline_review_draft")
        self.assertEqual(plan["status"], "api_configuration_needed")
        self.assertEqual(plan["api_call_count"], 0)
        self.assertGreater(plan["estimated_total_tokens"], 0)
        self.assertEqual(plan["skip_reason"], "agent_review_api_disabled")

    def test_configured_openai_api_is_planned_for_execution(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                llm_provider="openai",
                openai_api_key="configured",
                agent_review_model="review-model",
            )
        )

        plan = policy.plan([review_chunk(table_like=True)], quality_report(warnings=1), ChunkOptions())

        self.assertTrue(plan["provider_execution_enabled"])
        self.assertTrue(plan["provider_execution_ready"])
        self.assertEqual(plan["status"], "planned")
        self.assertIsNone(plan["skip_reason"])
        self.assertEqual(plan["selected_count"], 1)

    def test_azure_credentials_mark_provider_execution_ready(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                llm_provider="azure-openai",
                azure_openai_endpoint="https://example.openai.azure.com",
                azure_openai_api_key="configured",
                agent_review_model="review-deployment",
            )
        )

        plan = policy.plan([review_chunk(table_like=True)], quality_report(warnings=1), ChunkOptions())

        self.assertTrue(plan["provider_execution_enabled"])
        self.assertTrue(plan["provider_execution_ready"])
        self.assertEqual(plan["status"], "planned")
        self.assertIsNone(plan["skip_reason"])
        self.assertEqual(plan["selected_count"], 1)

    def test_anthropic_credentials_mark_provider_execution_ready(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                llm_provider="anthropic",
                anthropic_api_key="configured",
                agent_review_model="claude-haiku-4-5",
            )
        )

        plan = policy.plan([review_chunk(table_like=True)], quality_report(warnings=1), ChunkOptions())

        self.assertTrue(plan["provider_execution_ready"])
        self.assertEqual(plan["status"], "planned")

    def test_selects_only_review_candidates_within_budget(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=1,
                agent_review_max_input_tokens_per_document=100,
                agent_review_max_output_tokens_per_chunk=25,
                agent_review_token_safety_margin=1.25,
            )
        )
        candidate = review_chunk(table_like=True, table_cell_rows=[])
        clean = review_chunk(table_like=False)
        clean.chunk_id = "chunk_clean"

        plan = policy.plan([candidate, clean], quality_report(score=98.0, warnings=1), ChunkOptions(enable_agent_review=True))

        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(plan["selected_count"], 1)
        self.assertEqual(plan["selected_candidates"][0]["chunk_id"], "chunk_review")
        self.assertTrue(plan["selected_candidates"][0]["content_hash"].startswith("sha256:"))
        self.assertEqual(plan["selected_candidates"][0]["cache_status"], "new")
        self.assertIn("table_like_without_cell_rows", plan["selected_candidates"][0]["reasons"])
        self.assertEqual(plan["selected_candidates"][0]["estimated_output_tokens"], 25)
        self.assertEqual(plan["estimated_output_tokens"], 25)
        self.assertEqual(plan["estimated_total_tokens"], 38)

    def test_cached_review_candidate_is_not_selected_for_api_review(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=100,
            )
        )
        candidate = review_chunk(table_like=True, table_cell_rows=[])
        initial = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions(enable_agent_review=True))
        content_hash = initial["candidates"][0]["content_hash"]

        cached = policy.plan(
            [candidate],
            quality_report(score=98.0, warnings=1),
            ChunkOptions(enable_agent_review=True),
            cached_content_hashes={content_hash},
        )

        self.assertEqual(cached["status"], "skipped")
        self.assertEqual(cached["skip_reason"], "review_candidates_cached")
        self.assertEqual(cached["candidate_count"], 1)
        self.assertEqual(cached["cached_candidate_count"], 1)
        self.assertEqual(cached["new_candidate_count"], 0)
        self.assertEqual(cached["selected_count"], 0)
        self.assertEqual(cached["candidates"][0]["cache_status"], "reused")
        self.assertTrue(cached["cache_scope_hash"].startswith("sha256:"))

    def test_review_context_changes_invalidate_content_hash_cache(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=1000,
            )
        )
        first = review_chunk(
            kordoc_table_match={
                "match_label": "medium_review_match",
                "match_score": 34.0,
                "table_index": 1,
            }
        )
        second = review_chunk(
            kordoc_table_match={
                "match_label": "medium_review_match",
                "match_score": 72.0,
                "table_index": 1,
            }
        )
        initial = policy.plan([first], quality_report(score=98.0, warnings=1), ChunkOptions())
        cached_hash = initial["candidates"][0]["content_hash"]

        changed_context = policy.plan(
            [second],
            quality_report(score=98.0, warnings=1),
            ChunkOptions(),
            cached_content_hashes={cached_hash},
        )

        self.assertNotEqual(cached_hash, changed_context["candidates"][0]["content_hash"])
        self.assertEqual(changed_context["candidates"][0]["cache_status"], "new")
        self.assertEqual(changed_context["selected_count"], 1)

    def test_records_cost_estimate_when_prices_are_configured(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=2000,
                agent_review_max_output_tokens_per_chunk=100,
                agent_review_chars_per_token=4,
                agent_review_input_price_per_1m_tokens=1,
                agent_review_output_price_per_1m_tokens=2,
                agent_review_price_version="2026-07-08",
                agent_review_price_effective_at="2026-07-08T00:00:00Z",
            )
        )
        candidate = review_chunk(table_like=True, table_cell_rows=[])
        candidate.text = "x" * 4000
        candidate.normalized_text = "x" * 4000
        candidate.retrieval_text = "[source]\n" + ("x" * 4000)

        plan = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions(enable_agent_review=True))

        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["cost_estimate_status"], "estimated")
        self.assertEqual(plan["estimated_input_tokens"], 1000)
        self.assertEqual(plan["estimated_output_tokens"], 100)
        self.assertEqual(plan["estimated_input_cost"], "0.001")
        self.assertEqual(plan["estimated_output_cost"], "0.0002")
        self.assertEqual(plan["estimated_cost"], "0.0012")
        self.assertEqual(plan["price_version"], "2026-07-08")

    def test_cache_scope_changes_when_model_changes(self) -> None:
        first = AgentReviewPolicy(
            Settings(data_dir=Path("data"), enable_agent_review=True, agent_review_model="model-a")
        )
        second = AgentReviewPolicy(
            Settings(data_dir=Path("data"), enable_agent_review=True, agent_review_model="model-b")
        )

        self.assertNotEqual(first.cache_scope_hash(), second.cache_scope_hash())

    def test_candidate_selection_is_deterministic_input_order_not_random_sampling(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=2,
                agent_review_max_input_tokens_per_document=100,
            )
        )
        first = review_chunk(table_like=True, table_cell_rows=[])
        first.chunk_id = "chunk_first"
        second = review_chunk(table_like=True, table_cell_rows=[])
        second.chunk_id = "chunk_second"
        third = review_chunk(table_like=True, table_cell_rows=[])
        third.chunk_id = "chunk_third"

        plan = policy.plan([first, second, third], quality_report(score=98.0, warnings=1), ChunkOptions(enable_agent_review=True))

        self.assertEqual([item["chunk_id"] for item in plan["candidates"]], ["chunk_first", "chunk_second", "chunk_third"])
        self.assertEqual([item["chunk_id"] for item in plan["selected_candidates"]], ["chunk_first", "chunk_second"])
        self.assertTrue(plan["budget_exhausted"])

    def test_hwpx_complex_structure_is_review_candidate(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=100,
            )
        )
        candidate = review_chunk(
            source_hwpx_parser_review_flags=["nested_table", "merged_cell"],
            source_hwpx_nested_table_count=1,
            source_hwpx_merged_cell_count=2,
        )

        plan = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions(enable_agent_review=True))

        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["candidate_count"], 1)
        self.assertIn("hwpx_parser_review_flag", plan["selected_candidates"][0]["reasons"])
        self.assertIn("hwpx_complex_structure", plan["selected_candidates"][0]["reasons"])

    def test_parser_uncertainty_is_review_candidate(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=100,
            )
        )
        candidate = review_chunk(parser_uncertainty_risk_level="medium")

        plan = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions())

        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["candidate_count"], 1)
        self.assertIn("parser_uncertainty", plan["selected_candidates"][0]["reasons"])

    def test_hwpx_medium_parser_uncertainty_does_not_add_standalone_review_load(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=100,
            )
        )
        candidate = review_chunk(parser_uncertainty_source="hwpx", parser_uncertainty_risk_level="medium")

        plan = policy.plan([candidate], quality_report(warnings=1), ChunkOptions())

        self.assertEqual(plan["status"], "skipped")
        self.assertEqual(plan["skip_reason"], "no_review_candidates")
        self.assertEqual(plan["candidate_count"], 0)
        self.assertEqual(plan["candidates"], [])

    def test_hwpx_specific_parser_flags_still_trigger_review(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=100,
            )
        )
        candidate = review_chunk(
            parser_uncertainty_source="hwpx",
            parser_uncertainty_risk_level="medium",
            source_hwpx_parser_review_flags=["nested_table"],
            source_hwpx_nested_table_count=1,
        )

        plan = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions())

        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["candidate_count"], 1)
        self.assertIn("hwpx_parser_review_flag", plan["selected_candidates"][0]["reasons"])
        self.assertIn("hwpx_complex_structure", plan["selected_candidates"][0]["reasons"])
        self.assertNotIn("parser_uncertainty", plan["selected_candidates"][0]["reasons"])

    def test_kordoc_table_inventory_is_review_candidate(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                openai_api_key="configured",
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=1000,
            )
        )
        candidate = review_chunk(
            kordoc_table_inventory={
                "status": "parsed",
                "table_count": 1,
                "tables": [
                    {
                        "row_count": 2,
                        "column_count": 3,
                        "nested_table_count": 1,
                        "cell_rows": [{"cells": ["구분", "기준", "금액"]}],
                    }
                ],
            }
        )

        plan = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions())

        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["candidate_count"], 1)
        self.assertIn("kordoc_table_structure_review", plan["selected_candidates"][0]["reasons"])
        self.assertIn("kordoc_nested_table_review", plan["selected_candidates"][0]["reasons"])

    def test_kordoc_table_match_is_review_candidate(self) -> None:
        policy = AgentReviewPolicy(Settings(data_dir=Path("data"), enable_agent_review=True))
        candidate = review_chunk(
            kordoc_table_match={
                "match_label": "medium_review_match",
                "match_score": 34.0,
                "table_index": 2,
            }
        )

        plan = policy.plan([candidate], quality_report(warnings=1), ChunkOptions())

        self.assertIn("kordoc_table_match_review", plan["selected_candidates"][0]["reasons"])

    def test_marks_budget_exhausted_when_candidate_exceeds_token_limit(self) -> None:
        policy = AgentReviewPolicy(
            Settings(
                data_dir=Path("data"),
                enable_agent_review=True,
                agent_review_max_chunks_per_document=3,
                agent_review_max_input_tokens_per_document=1,
            )
        )
        candidate = review_chunk(table_like=True, table_cell_rows=[])

        plan = policy.plan([candidate], quality_report(score=98.0, warnings=1), ChunkOptions(enable_agent_review=True))

        self.assertEqual(plan["status"], "skipped")
        self.assertEqual(plan["skip_reason"], "review_budget_exhausted")
        self.assertTrue(plan["budget_exhausted"])


if __name__ == "__main__":
    unittest.main()
