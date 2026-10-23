from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app.api import routes_rag
from app.agents.query_agents import deterministic_query_rewrite
from app.core.config import Settings
from app.core.security import AuthContext
from app.retrieval.isolated_reranker import RerankerWorkerError


class RerankerStatusTests(unittest.TestCase):
    def test_public_role_preserves_actual_reranker_outcome_inside_completed_stage(self):
        for actual, expected in (("degraded", "degraded"), ("failed", "failed"),
                                 ("not_requested", "skipped"), ("completed", "completed")):
            with self.subTest(actual=actual):
                pipeline = routes_rag._qa_search_pipeline_trace(
                    retrieval={"reranker_status": actual, "reranker_reason": "reranker_worker_memory_floor"},
                    candidate_count=1, visible_count=1, result_count=1, tenant_id="tenant-test",
                )
                roles = routes_rag._qa_orchestration_role_trace(
                    search_trace={"pipeline_trace": pipeline},
                    orchestration={"claim_audit_status": "verified", "citation_verification_status": "verified"},
                )
                reranker = next(role for role in roles if role["role_id"] == "reranker")
                self.assertEqual(expected, reranker["status"])

    def test_role_override_must_belong_to_matching_stage_and_known_status(self):
        for stages in (
            [{"stage_id": "query_analysis", "status": "completed", "agent_role_statuses": [
                {"role_id": "reranker", "status": "failed"}]}],
            [{"stage_id": "rerank_filter", "status": "completed", "agent_role_statuses": [
                {"role_id": "reranker", "status": "synthetic-private-path"}]}],
        ):
            roles = routes_rag._qa_orchestration_role_trace(
                search_trace={"pipeline_trace": {"stages": stages}}, orchestration={},
            )
            self.assertEqual("completed", next(role for role in roles if role["role_id"] == "reranker")["status"])
            self.assertNotIn("synthetic-private-path", str(roles))

    def test_worker_failure_preserves_search_results_and_safe_reason(self):
        record = {"id": "doc-test:chunk-test", "document_id": "doc-test", "chunk_id": "chunk-test",
                  "text": "synthetic approved regulation", "metadata": {"document_id": "doc-test",
                  "chunk_id": "chunk-test", "approval_status": "approved", "approval_id": "approval-test"}}
        adapter = Mock()
        adapter.rerank.side_effect = RerankerWorkerError("reranker_worker_memory_floor")
        analysis = routes_rag.deterministic_query_analysis("synthetic leave procedure")
        request = routes_rag.RagSearchRequest(query="synthetic leave procedure", orchestration_mode="multi_model")
        auth = AuthContext(actor="test", tenant_id="tenant-test", auth_mode="api_token", role="admin")
        with patch.object(routes_rag, "_load_local_vector_records", return_value=[record]), \
                patch.object(routes_rag, "_load_cached_approval_snapshot", return_value={}), \
                patch.object(routes_rag, "load_visible_records", return_value=[record]), \
                patch.object(routes_rag.QueryAnalysisAgent, "analyze", return_value=analysis), \
                patch.object(routes_rag.QueryRewriteAgent, "rewrite", return_value=deterministic_query_rewrite(analysis)), \
                patch.object(routes_rag, "_score_records_for_queries", return_value=([(0.5, record)], {"retrieval_model": "test"})), \
                patch.object(routes_rag, "_cached_qwen3_reranker", return_value=adapter):
            results, trace = routes_rag.search_rag_records(request, auth, Settings(data_dir=Path("data"), rag_trace_enabled=False))
        self.assertEqual(["chunk-test"], [result["chunk_id"] for result in results])
        self.assertEqual("degraded", trace["reranker_status"])
        self.assertEqual("reranker_worker_memory_floor", trace["reranker_reason"])


if __name__ == "__main__":
    unittest.main()
