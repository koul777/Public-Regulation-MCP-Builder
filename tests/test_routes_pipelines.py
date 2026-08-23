import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from app.api import routes_pipelines
from app.core.config import Settings
from app.core.security import AuthContext
from fastapi import HTTPException
from pydantic import ValidationError


class PipelineManifestRouteTests(unittest.TestCase):
    def test_manifest_exposes_both_image_pipelines_without_source_paths(self) -> None:
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            with patch.object(routes_pipelines, "get_settings", return_value=settings):
                response = routes_pipelines.get_pipeline_manifest(auth)

        self.assertEqual("reg-rag-pipeline-manifest-v1", response["schema_version"])
        self.assertEqual("qwen3:8b", response["local_llm_model"])
        self.assertEqual(8, len(response["pipelines"]["regulation_preprocessing_v1"]))
        self.assertEqual(7, len(response["pipelines"]["local_regulation_qa_v1"]))
        self.assertEqual(
            "qwen3:8b",
            next(
                role["primary_model"]
                for role in response["agent_workflows"]["local_regulation_qa"]
                if role["role_id"] == "grounded_answerer"
            ),
        )
        self.assertTrue(any(profile["model"] == "qwen3:4b" for profile in response["model_profiles"]))
        query_rewriter = next(
            role
            for role in response["agent_workflows"]["local_regulation_qa"]
            if role["role_id"] == "query_rewriter"
        )
        self.assertEqual("검색어 보정 담당", query_rewriter["display_name"])
        self.assertEqual("query-qwen3-1.7b", query_rewriter["model_profile"])
        self.assertTrue(query_rewriter["purpose"])
        self.assertIn("query_plan", query_rewriter["required_inputs"])
        self.assertIn("change_tenant_scope", query_rewriter["forbidden_actions"])
        role_statuses = {
            role["role_id"]: role["implementation_status"]
            for roles in response["agent_workflows"].values()
            for role in roles
        }
        self.assertEqual("implemented_verified", role_statuses["grounded_answerer"])
        self.assertNotIn("planned", role_statuses.values())
        self.assertNotIn("source_file", str(response))
        self.assertNotIn("raw_text", str(response))

    def test_orchestration_plan_returns_next_role_and_model_without_execution(self) -> None:
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
        request = routes_pipelines.OrchestrationPlanRequest(
            workflow_id="local_regulation_qa",
            run_id="run-qa-1",
            profile_id="institution-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            with patch.object(routes_pipelines, "get_settings", return_value=settings):
                response = routes_pipelines.get_orchestration_plan(request, auth)

        self.assertEqual("agent_orchestration_plan_v1", response["report_type"])
        self.assertEqual("security_guard", response["next_role"]["role_id"])
        self.assertIsNone(response["next_role"]["model_profile"])
        self.assertTrue(response["plan_only"])
        self.assertIn("한 번에 다음 역할 하나만 실행", response["execution_rule"])
        self.assertNotIn("tenant-a", str(response))
        self.assertNotIn("institution-a", str(response))

    def test_orchestration_plan_rejects_skipped_roles(self) -> None:
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
        request = routes_pipelines.OrchestrationPlanRequest(
            workflow_id="local_regulation_qa",
            completed_roles=["orchestrator", "query_analyst"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            with patch.object(routes_pipelines, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as context:
                    routes_pipelines.get_orchestration_plan(request, auth)
        self.assertEqual(422, context.exception.status_code)

    def test_plan_request_rejects_advance_mode_in_plan_only_endpoint(self) -> None:
        with self.assertRaises(ValidationError):
            routes_pipelines.OrchestrationPlanRequest(
                workflow_id="local_regulation_qa",
                mode="advance",
            )

    def test_orchestration_plan_rejects_path_like_run_id(self) -> None:
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
        request = routes_pipelines.OrchestrationPlanRequest(
            workflow_id="local_regulation_qa",
            run_id="C:/private/source.pdf",
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", artifact_root=Path(tmp))
            with patch.object(routes_pipelines, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as context:
                    routes_pipelines.get_orchestration_plan(request, auth)

        self.assertEqual(422, context.exception.status_code)


if __name__ == "__main__":
    unittest.main()
