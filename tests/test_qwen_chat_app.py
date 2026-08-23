from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.core.institution_profiles import InstitutionProfile, InstitutionProfileRegistry
from app.core.security_primitives import AuthContext
from frontend.qwen_chat_app import (
    build_chat_request,
    completed_documents_for_profile,
    document_readiness,
    evaluate_index_gate,
    institution_registry_path,
    local_profiles,
    protected_or_shared_mode_reason,
    qwen_runtime_configuration_issue,
    safe_citation_rows,
    start_rag_chat_worker,
)
from scripts import run_qwen_chat


ROOT = Path(__file__).resolve().parents[1]


class QwenChatSecurityAndGateTests(unittest.TestCase):
    def test_protected_and_shared_modes_fail_closed(self) -> None:
        self.assertIsNotNone(
            protected_or_shared_mode_reason(
                Settings(app_env="production", api_auth_required=False, tenant_storage_isolation=False)
            )
        )
        self.assertIsNotNone(
            protected_or_shared_mode_reason(
                Settings(app_env="local", api_auth_required=True, tenant_storage_isolation=False)
            )
        )
        self.assertIsNotNone(
            protected_or_shared_mode_reason(
                Settings(app_env="local", api_auth_required=False, tenant_storage_isolation=True)
            )
        )
        self.assertIsNone(
            protected_or_shared_mode_reason(
                Settings(app_env="local", api_auth_required=False, tenant_storage_isolation=False)
            )
        )

    def test_qwen_runtime_requires_exact_model_backend_and_loopback(self) -> None:
        ready = Settings(
            rag_llm_backend="ollama",
            rag_llm_model="qwen3:8b",
            rag_llm_endpoint="http://127.0.0.1:11434",
        )
        self.assertIsNone(qwen_runtime_configuration_issue(ready))
        self.assertIn(
            "qwen3:8b",
            qwen_runtime_configuration_issue(
                Settings(
                    rag_llm_backend="ollama",
                    rag_llm_model="other-model",
                    rag_llm_endpoint="http://127.0.0.1:11434",
                )
            )
            or "",
        )
        self.assertIsNotNone(
            qwen_runtime_configuration_issue(
                Settings(
                    rag_llm_backend="ollama",
                    rag_llm_model="qwen3:8b",
                    rag_llm_endpoint="https://models.example.test",
                )
            )
        )

    def test_registry_path_prefers_configuration_and_falls_back_to_data_dir(self) -> None:
        self.assertEqual(
            Path("configured/profiles.json"),
            institution_registry_path(
                Settings(data_dir=Path("local-data"), institution_profiles_path="configured/profiles.json")
            ),
        )
        self.assertEqual(
            Path("local-data/institution_profiles.json"),
            institution_registry_path(Settings(data_dir=Path("local-data"), institution_profiles_path="")),
        )

    def test_profile_and_document_catalog_is_exactly_tenant_scoped(self) -> None:
        registry = InstitutionProfileRegistry(
            profiles={
                "local": InstitutionProfile(profile_id="local", tenant_id="tenant-a"),
                "generic": InstitutionProfile(profile_id="generic"),
                "foreign": InstitutionProfile(profile_id="foreign", tenant_id="tenant-b"),
            }
        )
        self.assertEqual({"generic", "local"}, set(local_profiles(registry, "tenant-a")))

        documents = [
            _document("visible", status="completed", tenant_id="tenant-a", profile_id="local"),
            _document("processing", status="processing", tenant_id="tenant-a", profile_id="local"),
            _document("foreign", status="completed", tenant_id="tenant-b", profile_id="local"),
            _document("other-profile", status="completed", tenant_id="tenant-a", profile_id="generic"),
            _document("legacy-unscoped", status="completed", tenant_id=None, profile_id="local"),
        ]
        visible = completed_documents_for_profile(
            documents,
            tenant_id="tenant-a",
            profile_id="local",
        )
        self.assertEqual(["visible"], [item.document_id for item in visible])

    def test_index_gate_matches_approved_visible_and_consistent_counts(self) -> None:
        status = {
            "indexing_status": "indexed",
            "vector_summary": {"record_count": 2},
            "vector_consistency": {"stale_count": 0},
            "validation_error": None,
        }
        self.assertTrue(evaluate_index_gate(status, 2)["ready"])
        self.assertEqual(
            "visible_record_count_mismatch",
            evaluate_index_gate({**status, "vector_summary": {"record_count": 1}}, 2)["reason"],
        )
        self.assertEqual(
            "stale_vector_records",
            evaluate_index_gate({**status, "vector_consistency": {"stale_count": 1}}, 2)["reason"],
        )
        self.assertFalse(
            evaluate_index_gate({**status, "vector_consistency": {"stale_count": "invalid"}}, 2)["ready"]
        )
        self.assertFalse(evaluate_index_gate(None, 2)["ready"])

    def test_document_chat_gate_requires_all_active_chunks_to_be_terminal(self) -> None:
        document = _document("doc-ready", status="completed", tenant_id="tenant-a", profile_id="local")
        status = {
            "indexing_status": "indexed",
            "vector_summary": {"record_count": 1},
            "vector_consistency": {"stale_count": 0},
            "validation_error": None,
        }
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="test")

        terminal_repository = _Repository(
            [
                SimpleNamespace(approval_status="approved"),
                SimpleNamespace(approval_status="rejected"),
                SimpleNamespace(approval_status="superseded"),
            ]
        )
        terminal = document_readiness(
            terminal_repository,
            document,
            auth,
            index_status_getter=lambda document_id, current_auth: status,
        )
        self.assertTrue(terminal.ready)
        self.assertEqual(0, terminal.pending_review_count)

        pending_repository = _Repository(
            [
                SimpleNamespace(approval_status="approved"),
                SimpleNamespace(approval_status="needs_review"),
            ]
        )
        pending = document_readiness(
            pending_repository,
            document,
            auth,
            index_status_getter=lambda document_id, current_auth: status,
        )
        self.assertFalse(pending.ready)
        self.assertEqual("pending_review", pending.gate["reason"])
        self.assertEqual(1, pending.pending_review_count)

        unavailable = document_readiness(
            _BrokenRepository(),
            document,
            auth,
            index_status_getter=lambda document_id, current_auth: status,
        )
        self.assertFalse(unavailable.ready)
        self.assertEqual("chunk_state_unavailable", unavailable.gate["reason"])

    def test_chat_request_is_exactly_document_and_profile_scoped(self) -> None:
        request = build_chat_request(
            question="적용 범위는 무엇인가요?",
            messages=[
                {"role": "user", "content": "앞 질문"},
                {"role": "assistant", "content": "실패", "error": True},
            ],
            document_id="doc-1",
            profile_id="Profile-A",
            top_k=4,
        )
        self.assertEqual("doc-1", request.document_id)
        self.assertEqual("profile-a", request.profile_id)
        self.assertEqual("ollama", request.llm_backend)
        self.assertEqual("auto", request.orchestration_mode)
        self.assertEqual(4, request.top_k)
        self.assertEqual(1, len(request.history))

    def test_worker_runs_without_blocking_caller_and_captures_result(self) -> None:
        request = build_chat_request(
            question="질문",
            messages=[],
            document_id="doc-1",
            profile_id="profile-a",
        )
        auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="test")
        worker, progress, outcome = start_rag_chat_worker(
            request,
            auth,
            chat_callable=lambda current_request, current_auth: {"answer": "답변", "citations": []},
        )
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(worker.daemon)
        self.assertTrue(progress.empty())
        self.assertEqual(("ok", {"answer": "답변", "citations": []}), outcome.get_nowait())

    def test_citation_table_drops_internal_path_fields(self) -> None:
        rows = safe_citation_rows(
            [
                {
                    "regulation_title": "샘플 복무규정",
                    "article_no": "제1조",
                    "paragraph_no": "제1항",
                    "support_quote": "이 규정은 승인된 근거만 사용합니다.",
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "approval_id": "approval-1",
                    "approval_ids": ["approval-1"],
                    "evidence_ids": ["chunk-1"],
                    "approval_review_batch_manifest_path": "C:/private/review.json",
                }
            ]
        )
        self.assertEqual("샘플 복무규정", rows[0]["규정명"])
        self.assertEqual("제1조", rows[0]["조문"])
        self.assertEqual("제1항", rows[0]["항"])
        self.assertEqual("이 규정은 승인된 근거만 사용합니다.", rows[0]["근거 인용문"])
        self.assertNotIn("document_id", rows[0])
        self.assertNotIn("approval_ids", rows[0])
        self.assertNotIn("evidence_ids", rows[0])
        self.assertNotIn("approval_review_batch_manifest_path", rows[0])
        self.assertNotIn("C:/private/review.json", repr(rows))

    def test_citation_table_accepts_fallback_document_title_and_page_range(self) -> None:
        rows = safe_citation_rows(
            [
                {
                    "document_title": "샘플 인사규정",
                    "article_no": "제2조",
                    "source_page_start": 3,
                    "source_page_end": 4,
                }
            ]
        )
        self.assertEqual(
            {"규정명": "샘플 인사규정", "조문": "제2조", "원문 쪽": "3–4"},
            rows[0],
        )


class QwenChatLauncherTests(unittest.TestCase):
    def test_loopback_validation_rejects_public_bind_addresses(self) -> None:
        self.assertEqual("127.0.0.1", run_qwen_chat.validate_loopback_host("127.0.0.1"))
        self.assertEqual("::1", run_qwen_chat.validate_loopback_host("::1"))
        with self.assertRaises(ValueError):
            run_qwen_chat.validate_loopback_host("0.0.0.0")
        with self.assertRaises(ValueError):
            run_qwen_chat.validate_loopback_host("chat.example.test")

    def test_explicit_port_is_exact_and_default_selects_available_port(self) -> None:
        with patch.object(run_qwen_chat, "port_is_available", return_value=True) as available:
            self.assertEqual(9876, run_qwen_chat.resolve_launch_port(9876))
            available.assert_called_once_with(9876, host="127.0.0.1")
        with patch.object(run_qwen_chat, "port_is_available", return_value=False):
            with self.assertRaises(RuntimeError):
                run_qwen_chat.resolve_launch_port(9876)
        with patch.object(run_qwen_chat, "select_available_port", return_value=8507) as select:
            self.assertEqual(8507, run_qwen_chat.resolve_launch_port(None))
            select.assert_called_once_with(8502, host="127.0.0.1", search_count=100)

    def test_environment_preserves_builder_paths_and_rag_values(self) -> None:
        environment = run_qwen_chat.launch_environment(
            {
                "DATA_DIR": "D:/local-data",
                "ARTIFACT_ROOT": "D:/artifacts",
                "INSTITUTION_PROFILES_PATH": "D:/profiles.json",
                "RAG_LLM_BACKEND": "ollama",
                "RAG_LLM_MODEL": "qwen3:8b",
                "RAG_LLM_ENDPOINT": "http://localhost:11434",
                "OPENAI_API_KEY": "must-not-reach-child",
                "HTTP_PROXY": "http://proxy.example.test",
                "PYTHONPATH": "C:/untrusted-python-path",
            }
        )
        self.assertEqual("D:/local-data", environment["DATA_DIR"])
        self.assertEqual("D:/artifacts", environment["ARTIFACT_ROOT"])
        self.assertEqual("D:/profiles.json", environment["INSTITUTION_PROFILES_PATH"])
        self.assertEqual("http://localhost:11434", environment["RAG_LLM_ENDPOINT"])
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertNotIn("PYTHONPATH", environment)

        forced = run_qwen_chat.launch_environment(
            {"RAG_LLM_BACKEND": "extractive", "RAG_LLM_MODEL": "other-model"}
        )
        self.assertEqual("ollama", forced["RAG_LLM_BACKEND"])
        self.assertEqual("qwen3:8b", forced["RAG_LLM_MODEL"])

    def test_launcher_does_not_start_a_process_in_protected_mode(self) -> None:
        environment = {"APP_ENV": "production", "API_AUTH_REQUIRED": "false"}
        with patch.object(run_qwen_chat, "launch_environment", return_value=environment), patch.object(
            run_qwen_chat.subprocess, "run"
        ) as run:
            self.assertEqual(2, run_qwen_chat.main(["--port", "9876", "--headless"]))
        run.assert_not_called()

    def test_main_launches_separate_streamlit_module_without_shell(self) -> None:
        completed = SimpleNamespace(returncode=0)
        environment = run_qwen_chat.launch_environment({})
        with patch.object(run_qwen_chat, "launch_environment", return_value=environment), patch.object(
            run_qwen_chat, "resolve_launch_port", return_value=9876
        ), patch.object(run_qwen_chat.subprocess, "run", return_value=completed) as run:
            result = run_qwen_chat.main(["--port", "9876", "--headless"])
        self.assertEqual(0, result)
        command = run.call_args.args[0]
        self.assertEqual([run_qwen_chat.sys.executable, "-m", "streamlit", "run"], command[:4])
        self.assertTrue(command[4].endswith("frontend\\qwen_chat_app.py"))
        self.assertIn("--server.headless", command)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(environment, run.call_args.kwargs["env"])

    def test_source_is_standalone_and_batch_uses_module_launcher(self) -> None:
        app_source = (ROOT / "frontend" / "qwen_chat_app.py").read_text(encoding="utf-8")
        batch_source = (ROOT / "RUN_QWEN_CHAT.bat").read_text(encoding="utf-8")
        self.assertNotIn("frontend.streamlit_app", app_source)
        self.assertIn("rag_chat_progress", app_source)
        self.assertIn("threading.Thread", app_source)
        self.assertIn("document_id=normalized_document_id", app_source)
        self.assertIn("profile_id=normalized_profile_id", app_source)
        self.assertIn("5. 질문하고 답변과 근거 인용을 확인하세요", app_source)
        self.assertIn("-m scripts.run_qwen_chat", batch_source)
        self.assertIn("sys.version_info >= (3, 11)", batch_source)
        self.assertNotIn("sys.version_info ^>=", batch_source)


class _Repository:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks

    def get_chunks(self, document_id: str) -> list[object]:
        return list(self.chunks)


class _BrokenRepository:
    def get_chunks(self, document_id: str) -> list[object]:
        raise RuntimeError("C:/private/path must not be displayed")


def _document(
    document_id: str,
    *,
    status: str,
    tenant_id: str | None,
    profile_id: str | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        document_id=document_id,
        status=status,
        tenant_id=tenant_id,
        profile_id=profile_id,
        processed_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
    )


if __name__ == "__main__":
    unittest.main()
