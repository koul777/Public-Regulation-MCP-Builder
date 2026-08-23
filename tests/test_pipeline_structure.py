import unittest

from app.pipelines.definitions import (
    LOCAL_QA_PIPELINE_ID,
    PREPROCESSING_PIPELINE_ID,
    PipelineStageTracker,
    get_pipeline_definition,
    pipeline_manifest,
)


class PipelineStructureTests(unittest.TestCase):
    def test_image_preprocessing_pipeline_has_eight_ordered_stages(self) -> None:
        stages = get_pipeline_definition(PREPROCESSING_PIPELINE_ID)
        self.assertEqual(8, len(stages))
        self.assertEqual(
            [
                "upload_admission",
                "parse_extract",
                "normalize",
                "structure_detect",
                "chunk_generate",
                "quality_gate",
                "export",
                "vector_index",
            ],
            [stage.stage_id for stage in stages],
        )

    def test_image_local_qa_pipeline_has_seven_ordered_stages(self) -> None:
        stages = get_pipeline_definition(LOCAL_QA_PIPELINE_ID)
        self.assertEqual(7, len(stages))
        self.assertEqual(1, stages[0].order)
        self.assertEqual("citation_verify", stages[-1].stage_id)
        self.assertTrue(stages[2].security_gate)
        self.assertTrue(stages[-1].security_gate)

    def test_tracker_records_safe_stage_transitions(self) -> None:
        tracker = PipelineStageTracker(PREPROCESSING_PIPELINE_ID, tenant_id="tenant-a")
        tracker.start("parse_extract", detail={"path": "C:\\private\\secret.pdf", "page_count": 2})
        tracker.complete("parse_extract", detail={"raw_text": "private text", "page_count": 2})
        snapshot = tracker.snapshot()
        self.assertTrue(snapshot["tenant_scoped"])
        self.assertEqual("parse_extract", snapshot["stages"][0]["stage_id"])
        self.assertEqual("completed", snapshot["stages"][0]["status"])
        self.assertTrue(snapshot["stages"][0]["purpose"])
        self.assertEqual(["parsed_document"], snapshot["stages"][0]["output_keys"])
        self.assertNotIn("path", snapshot["stages"][0]["detail"])
        self.assertNotIn("raw_text", snapshot["stages"][0]["detail"])

    def test_tracker_rejects_overlapping_stage(self) -> None:
        tracker = PipelineStageTracker(LOCAL_QA_PIPELINE_ID)
        tracker.start("query_analysis")
        with self.assertRaises(ValueError):
            tracker.start("query_correction")

    def test_tracker_records_role_statuses_without_raw_details(self) -> None:
        tracker = PipelineStageTracker(PREPROCESSING_PIPELINE_ID, tenant_id="tenant-a")
        tracker.start("quality_gate")
        tracker.set_agent_role_status(
            "quality_gate",
            "quality_gate",
            status="completed",
            detail={"quality_score": 0.91, "raw_text": "must not persist"},
        )
        tracker.set_agent_role_status(
            "quality_gate",
            "human_approval_gate",
            status="pending",
            reason_code="awaiting_human_approval",
        )
        snapshot = tracker.snapshot()
        roles = snapshot["stages"][0]["agent_role_statuses"]
        self.assertEqual("completed", roles[0]["status"])
        self.assertEqual("pending", roles[1]["status"])
        self.assertEqual("awaiting_human_approval", roles[1]["reason_code"])
        self.assertNotIn("raw_text", roles[0]["detail"])

    def test_stage_failure_closes_running_role_statuses(self) -> None:
        tracker = PipelineStageTracker(PREPROCESSING_PIPELINE_ID, tenant_id="tenant-a")
        tracker.start("parse_extract")
        tracker.set_agent_role_status(
            "parse_extract",
            "parser_extractor",
            status="running",
        )

        snapshot = tracker.fail(
            "parse_extract",
            reason_code="parser_failed",
            detail={"path": "C:\\private\\secret.pdf"},
        )
        stage = snapshot["stages"][0]
        role_statuses = {role["role_id"]: role for role in stage["agent_role_statuses"]}
        self.assertEqual("failed", stage["status"])
        self.assertEqual("failed", role_statuses["parser_extractor"]["status"])
        self.assertEqual("parser_failed", role_statuses["parser_extractor"]["reason_code"])
        self.assertEqual("pending", role_statuses["ocr_extractor"]["status"])
        self.assertNotIn("secret.pdf", str(snapshot))

    def test_manifest_exposes_machine_readable_contract(self) -> None:
        manifest = pipeline_manifest()
        self.assertEqual(8, manifest[PREPROCESSING_PIPELINE_ID][-1]["stage_total"])
        self.assertEqual(["vector_index"], manifest[PREPROCESSING_PIPELINE_ID][-1]["output_keys"])
        self.assertEqual("grounded_answerer", manifest[LOCAL_QA_PIPELINE_ID][5]["owner"])

    def test_manifest_explains_role_and_model_ownership_for_each_stage(self) -> None:
        manifest = pipeline_manifest()
        query_correction = manifest[LOCAL_QA_PIPELINE_ID][1]
        answer_stage = manifest[LOCAL_QA_PIPELINE_ID][5]

        self.assertEqual(["query_rewriter"], query_correction["agent_role_ids"])
        self.assertEqual("query-qwen3-1.7b", query_correction["agent_roles"][0]["model_profile"])
        self.assertEqual(["grounded_answerer"], answer_stage["agent_role_ids"])
        self.assertEqual("qwen3:8b", answer_stage["agent_roles"][0]["primary_model"])
        self.assertFalse(answer_stage["agent_roles"][0]["human_decision_required"])
        self.assertIn("grounded_context", answer_stage["agent_roles"][0]["required_inputs"])
        self.assertIn("approve_chunks", answer_stage["agent_roles"][0]["forbidden_actions"])


if __name__ == "__main__":
    unittest.main()
