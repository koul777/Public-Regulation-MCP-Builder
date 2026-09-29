from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.core.institution_profiles import InstitutionProfile, InstitutionProfileRegistry
from app.core.security_primitives import AuthContext
from app.services.readiness_adapter import OperatorReadinessState
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
    _qwen_probe_readiness,
    _qwen_tour_step,
)
from scripts import run_qwen_chat


ROOT = Path(__file__).resolve().parents[1]


def _synthetic_visible_progress() -> None:
    from unittest.mock import patch
    from app.core.security_primitives import AuthContext
    from frontend import qwen_chat_app as chat

    request = chat.build_chat_request(question="공개 합성 질문", messages=[], document_id="doc", profile_id="demo")
    auth = AuthContext(actor="synthetic", tenant_id="demo", auth_mode="test")
    with patch.object(chat, "rag_chat", return_value={"answer": "합성 답변", "citations": []}):
        result = chat.run_rag_chat_with_visible_progress(request, auth)
    chat._render_assistant_message({"content": result["answer"], "citations": result["citations"]})


class QwenChatSecurityAndGateTests(unittest.TestCase):
    def test_duplicate_public_citations_preserve_distinct_scope_pages_and_quotes(self) -> None:
        citation = {"document_id": "doc-a", "regulation_version": "1", "article_no": "제5조",
                    "article_title": "기록 보관", "source_page_start": 1}
        variants = [dict(citation, chunk_id="article"), dict(citation, chunk_id="table"),
                    dict(citation, document_id="doc-b"), dict(citation, regulation_version="2"),
                    dict(citation, source_page_start=2), dict(citation, support_quote="2년간 보관한다.")]
        rows = safe_citation_rows(variants)
        self.assertEqual(5, len(rows))
        self.assertEqual("2", rows[-2]["원문 쪽"])
        self.assertEqual("2년간 보관한다.", rows[-1]["근거 인용문"])

    def setUp(self) -> None:
        import sys

        self.addCleanup(sys.modules.__setitem__, "__main__", sys.modules["__main__"])

    def test_malformed_or_blank_citations_never_become_evidence(self) -> None:
        for value in ([], {}, 42, True, "   ", "\n\t"):
            with self.subTest(value=value):
                citations = [{"article_no": value, "support_quote": value, "source_page_start": 3}]
                self.assertEqual([], safe_citation_rows(citations))
                step = _qwen_tour_step(True, [{"role": "assistant", "citations": citations}])
                self.assertIn("근거 인용이 없는", step[1])
        self.assertEqual(
            [{"조문": "제1조", "근거 인용문": "공개 합성 근거"}],
            safe_citation_rows([{"article_no": "  제1조 ", "support_quote": "공개\n합성 근거", "source_page_start": {"private": "value"}}]),
        )

    def test_citationless_worker_completion_does_not_claim_evidence_review(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_function(_synthetic_visible_progress).run()
        self.assertFalse(app.exception)
        self.assertEqual(1, len(app.status))
        self.assertEqual("답변 생성 완료 · 근거 인용을 아래에서 확인하세요", app.status[0].label)
        self.assertTrue(any("규정 근거로 사용하지 마세요" in item.value for item in app.warning))

    def test_guided_chat_requires_selection_and_probe_then_accepts_two_questions(self) -> None:
        import sys
        from streamlit.testing.v1 import AppTest

        # AppTest replaces __main__; restore it before later multiprocessing tests.
        self.addCleanup(sys.modules.__setitem__, "__main__", sys.modules["__main__"])
        app = AppTest.from_function(_synthetic_guided_chat, default_timeout=20).run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.selectbox(key="qwen-profile").value)
        self.assertEqual(0, len(app.chat_input))
        app.selectbox(key="qwen-profile").select("demo").run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.selectbox(key="qwen-document-demo").value)
        app.selectbox(key="qwen-document-demo").select("doc").run()
        self.assertTrue(app.chat_input[0].disabled)
        app.button(key="qwen-probe").click().run()
        self.assertFalse(app.chat_input[0].disabled)
        for question in ("휴가는 며칠인가요?", "신청은 어떻게 하나요?"):
            app.chat_input[0].set_value(question).run()
            self.assertFalse(app.exception)
        markup = "\n".join(str(item.value) for item in app.markdown)
        self.assertIn("답변 아래 근거 인용을 확인하세요", markup)
        self.assertIn("qwen-answer-3", markup)

    def test_click_guide_requires_live_probe_and_grounded_answer(self) -> None:
        answered = [{"role": "assistant", "citations": [{"article_no": "제1조"}]}]
        self.assertEqual(3, _qwen_tour_step(False, answered)[0])
        self.assertEqual(4, _qwen_tour_step(True, [])[0])
        self.assertEqual(4, _qwen_tour_step(True, [{"role": "assistant", "error": True}])[0])
        uncited = _qwen_tour_step(True, [{"role": "assistant", "citations": []}])
        self.assertEqual(5, uncited[0])
        self.assertIn("근거 인용이 없는", uncited[1])
        self.assertEqual("div.st-key-qwen-answer-0", uncited[3])
        malformed = _qwen_tour_step(True, [{"role": "assistant", "citations": [{"chunk_id": "private"}]}])
        self.assertIn("근거 인용이 없는", malformed[1])
        page_only = _qwen_tour_step(True, [{"role": "assistant", "citations": [{"source_page_start": 3}]}])
        self.assertIn("근거 인용이 없는", page_only[1])
        self.assertEqual(5, _qwen_tour_step(True, answered)[0])
        self.assertEqual("div.st-key-qwen-answer-0", _qwen_tour_step(True, answered)[3])
        self.assertEqual([{"role": "assistant", "citations": [{"article_no": "제1조"}]}], answered)

    def test_citation_free_answer_warns_without_exposing_unusable_citation(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_function(
            _synthetic_answer_render,
            args=([{"chunk_id": "private"}, {"source_page_start": 3}],),
        ).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("규정 근거로 사용하지 마세요" in item.value for item in app.warning))
        self.assertEqual(0, len(app.expander))

    def test_blocked_states_show_next_action_and_never_open_chat(self) -> None:
        import sys
        from streamlit.testing.v1 import AppTest

        self.addCleanup(sys.modules.__setitem__, "__main__", sys.modules["__main__"])
        for scenario, expected in (
            ("missing_profile", "기관을 등록"),
            ("no_profiles", "현재 기관을 등록"),
            ("no_documents", "전처리를 완료"),
            ("not_ready", "직접 승인 또는 반려"),
        ):
            with self.subTest(scenario=scenario):
                app = AppTest.from_function(
                    _synthetic_blocked_chat, args=(scenario,), default_timeout=20,
                ).run()
                if scenario in {"no_documents", "not_ready"}:
                    app.selectbox(key="qwen-profile").select("demo").run()
                self.assertFalse(app.exception)
                self.assertEqual(0, len(app.chat_input))
                self.assertTrue(any(expected in str(item.value) for item in app.info))
                markup = "\n".join(str(item.value) for item in app.markdown)
                self.assertIn("st-key-qwen-recovery", markup)
                self.assertFalse(app.button(key="qwen-recovery-recheck").disabled)
                app.button(key="qwen-recovery-recheck").click().run()
                self.assertFalse(app.exception)
                self.assertEqual(0, len(app.chat_input))

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

    def test_qwen_probe_readiness_uses_common_beginner_state_contract(self) -> None:
        ready = _qwen_probe_readiness(
            {"signature": "current", "available": True},
            signature="current",
        )
        unavailable = _qwen_probe_readiness(
            {"signature": "current", "available": False},
            signature="current",
        )
        stale = _qwen_probe_readiness(
            {"signature": "old", "available": True},
            signature="current",
        )

        self.assertEqual(OperatorReadinessState.READY, ready.state)
        self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, unavailable.state)
        self.assertEqual(OperatorReadinessState.UNKNOWN, stale.state)

    def test_safe_diagnostic_card_keeps_recovery_reason_and_invalidates_on_settings_change(self) -> None:
        from app.services.local_llm_readiness_service import check_local_llm_readiness

        card = check_local_llm_readiness(
            Settings(rag_llm_backend="ollama"), probe_runner=lambda _: {"available": "invalid"},
        )
        state = {"signature": "current", "card": card, "available": False}
        current = _qwen_probe_readiness(state, signature="current")
        changed = _qwen_probe_readiness(state, signature="changed")
        self.assertEqual("local_probe_invalid", current.reason_code)
        self.assertIn("해석하지 못했습니다", current.next_action)
        self.assertEqual(OperatorReadinessState.UNKNOWN, changed.state)

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
        self.assertEqual("fast", request.retrieval_mode)
        self.assertEqual("deterministic", request.claim_audit_mode)
        self.assertEqual(4, request.top_k)
        self.assertEqual(1, len(request.history))

        precise = build_chat_request(
            question="적용 범위는 무엇인가요?",
            messages=[],
            document_id="doc-1",
            profile_id="Profile-A",
            precise_claim_audit=True,
        )
        self.assertEqual("model", precise.claim_audit_mode)

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
    def test_english_windows_output_keeps_korean_launch_and_error_messages(self) -> None:
        for protected in (False, True):
            with self.subTest(protected=protected):
                stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
                stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252")
                stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252")
                environment = {"APP_ENV": "production" if protected else "local"}
                try:
                    with patch.object(run_qwen_chat.sys, "stdout", stdout), patch.object(
                        run_qwen_chat.sys, "stderr", stderr
                    ), patch.object(run_qwen_chat, "launch_environment", return_value=environment), patch.object(
                        run_qwen_chat, "resolve_launch_port", return_value=9876
                    ), patch.object(run_qwen_chat.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                        result = run_qwen_chat.main(["--port", "9876", "--headless"])
                    stdout.flush()
                    stderr.flush()
                    if protected:
                        self.assertEqual(2, result)
                        self.assertIn("[실행 중단]", stderr_bytes.getvalue().decode("utf-8"))
                        run.assert_not_called()
                    else:
                        self.assertEqual(0, result)
                        self.assertIn("로컬 Qwen 규정 챗봇", stdout_bytes.getvalue().decode("utf-8"))
                        run.assert_called_once()
                finally:
                    stdout.close()
                    stderr.close()

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


def _synthetic_guided_chat() -> None:
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.core.config import Settings
    from app.core.institution_profiles import InstitutionProfile, InstitutionProfileRegistry
    from app.services.readiness_adapter import adapt_local_llm_probe
    from frontend import qwen_chat_app as chat

    settings = Settings(app_env="test", api_auth_required=False, tenant_storage_isolation=False,
                        api_default_tenant_id="default", rag_llm_backend="ollama", rag_llm_model="qwen3:8b",
                        rag_llm_endpoint="http://127.0.0.1:11434")
    registry = InstitutionProfileRegistry(profiles={"demo": InstitutionProfile(
        profile_id="demo", display_name="공개 합성 기관", institution_name="공개 합성 기관", tenant_id="default")})
    document = SimpleNamespace(document_id="doc", title="합성 규정", original_filename="sample.docx",
                               status="completed", tenant_id="default", profile_id="demo", processed_at="2026-01-01")
    ready = chat.DocumentReadiness(document, 1, 1, 0, 0, {"ready": True, "indexing_status": "indexed"}, {})
    response = {"answer": "합성 답변입니다.", "citations": [{"regulation_title": "합성 규정", "article_no": "제1조"}]}
    with patch.object(chat, "get_settings", return_value=settings), \
         patch.object(chat, "load_local_institution_registry", return_value=registry), \
         patch.object(chat, "JsonRepository", return_value=SimpleNamespace(list_documents=lambda: [document])), \
         patch.object(chat, "document_readiness", return_value=ready), \
         patch.object(chat, "check_local_llm_readiness", return_value=adapt_local_llm_probe({"available": True}, component="Qwen3 8B")), \
         patch.object(chat, "run_rag_chat_with_visible_progress", return_value=response):
        chat.main()


def _synthetic_answer_render(citations: list[dict[str, str]]) -> None:
    from frontend.qwen_chat_app import _render_assistant_message

    _render_assistant_message({"role": "assistant", "content": "합성 답변", "citations": citations})


def _synthetic_blocked_chat(scenario: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.core.config import Settings
    from app.core.institution_profiles import InstitutionProfile, InstitutionProfileRegistry
    from frontend import qwen_chat_app as chat

    settings = Settings(app_env="test", api_auth_required=False, tenant_storage_isolation=False,
                        api_default_tenant_id="default", rag_llm_backend="ollama", rag_llm_model="qwen3:8b",
                        rag_llm_endpoint="http://127.0.0.1:11434")
    profile = InstitutionProfile(
        profile_id="demo", display_name="공개 합성 기관", institution_name="공개 합성 기관",
        tenant_id="default",
    )
    registry = InstitutionProfileRegistry(profiles={} if scenario == "no_profiles" else {"demo": profile})
    document = SimpleNamespace(document_id="doc", status="completed", tenant_id="default", profile_id="demo")
    readiness = chat.DocumentReadiness(
        document, 1, 0, 0, 1, {"ready": False, "reason": "pending_review"}, None,
    )
    registry_result = FileNotFoundError() if scenario == "missing_profile" else registry
    documents = [] if scenario == "no_documents" else [document]
    with patch.object(chat, "get_settings", return_value=settings), \
         patch.object(chat, "qwen_runtime_configuration_issue", return_value=None), \
         patch.object(chat, "load_local_institution_registry", side_effect=registry_result if isinstance(registry_result, Exception) else None, return_value=registry), \
         patch.object(chat, "JsonRepository", return_value=SimpleNamespace(list_documents=lambda: documents)), \
         patch.object(chat, "document_readiness", return_value=readiness), \
         patch.object(chat, "rag_chat") as rag_chat:
        chat.main()
        rag_chat.assert_not_called()


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
