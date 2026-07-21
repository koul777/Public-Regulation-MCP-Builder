from __future__ import annotations

import ast
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.mcp_connection_diagnostic import diagnostic_from_bundle_status
from scripts.mcp_client_status import begin_attempt, commit_success, create_bundle_status


REPO_ROOT = Path(__file__).resolve().parents[1]


class StreamlitOperatorModeTests(unittest.TestCase):
    def test_mcp_connection_diagnostic_reader_reloads_bundle_status_each_call(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_names = {"_read_mcp_connection_diagnostic"}
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

        def fake_diagnostic(bundle_status, **kwargs):
            calls.append((dict(bundle_status), dict(kwargs)))
            return {"marker": bundle_status.get("marker")}

        namespace = {
            "Any": Any,
            "Path": Path,
            "hashlib": hashlib,
            "json": json,
            "diagnostic_from_bundle_status": fake_diagnostic,
        }
        exec(
            compile(ast.Module(body=helper_nodes, type_ignores=[]), "<mcp-diagnostic-reader>", "exec"),
            namespace,
        )
        read_diagnostic = namespace["_read_mcp_connection_diagnostic"]

        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "bundle_status.json"
            status_path.write_text(
                json.dumps({"marker": "first", "installation_attempt_id": "attempt-1"}),
                encoding="utf-8",
            )
            first, first_error = read_diagnostic(tmp)
            status_path.write_text(
                json.dumps({"marker": "second", "installation_attempt_id": "attempt-1"}),
                encoding="utf-8",
            )
            second, second_error = read_diagnostic(tmp)

        self.assertEqual("first", first["marker"])
        self.assertEqual("second", second["marker"])
        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual("first", calls[0][0]["marker"])
        self.assertEqual("second", calls[1][0]["marker"])
        self.assertEqual("attempt-1", calls[1][1]["attempt_id"])
        self.assertIsNone(calls[1][1]["config_fingerprint"])

    def test_mcp_connection_diagnostic_reader_does_not_mix_v5_client_identities(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        reader_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_read_mcp_connection_diagnostic"
        )
        namespace = {
            "Any": Any,
            "Path": Path,
            "hashlib": hashlib,
            "json": json,
            "diagnostic_from_bundle_status": diagnostic_from_bundle_status,
        }
        exec(
            compile(ast.Module(body=[reader_node], type_ignores=[]), "<mcp-diagnostic-reader>", "exec"),
            namespace,
        )
        read_diagnostic = namespace["_read_mcp_connection_diagnostic"]
        status = begin_attempt(
            create_bundle_status("final", generated_at="2026-07-21T00:00:00Z"),
            "codex",
            "attempt-codex",
            started_at="2026-07-21T00:01:00Z",
        )
        status = commit_success(
            status,
            "codex",
            "attempt-codex",
            verified_stages=("registration", "loader", "transport", "fresh_app_server"),
            config_entry_fingerprint="codex-config",
            runtime_fingerprint="runtime-current",
            bundle_location_fingerprint="bundle-current",
            verified_at="2026-07-21T00:02:00Z",
        )

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bundle_status.json").write_text(
                json.dumps(status),
                encoding="utf-8",
            )
            for target in (
                "claude-code",
                "claude-desktop",
                "chatgpt-desktop-local",
                "chatgpt-remote",
                "chatgpt-tunnel",
                "claude-api",
            ):
                with self.subTest(target=target):
                    report, read_error = read_diagnostic(tmp, target)
                    self.assertIsNone(read_error)
                    self.assertEqual("client_connections", report.get("status_source"))
                    self.assertIsNone(report["attempt_id"])
                    self.assertIsNone(report["config_fingerprint"])
                    self.assertFalse(report["configured"])

    def test_mcp_connection_diagnostic_reader_requires_real_installed_config_fingerprint(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        reader_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_read_mcp_connection_diagnostic"
        )
        namespace = {
            "Path": Path,
            "hashlib": hashlib,
            "json": json,
            "diagnostic_from_bundle_status": diagnostic_from_bundle_status,
        }
        exec(
            compile(ast.Module(body=[reader_node], type_ignores=[]), "<mcp-diagnostic-reader>", "exec"),
            namespace,
        )
        read_diagnostic = namespace["_read_mcp_connection_diagnostic"]
        legacy_success = {
            "installation_attempt_id": "attempt-legacy",
            "runtime_fingerprint": "sha256:runtime-current",
            "direct_config_registered": True,
            "direct_config_loader_verified": True,
            "installed_config_transport_verified": True,
            "installed_config_transport_runtime_fingerprint": "sha256:runtime-current",
            "direct_stdio_verified": True,
            "transport_end_to_end_verified": True,
            "fresh_codex_app_server_inventory_verified": True,
            "fresh_codex_app_server_runtime_fingerprint": "sha256:runtime-current",
            "desktop_app_server_loader_verified": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            status_path = bundle_dir / "bundle_status.json"
            config_path = bundle_dir / "config.toml"
            config_path.write_text(
                '[mcp_servers.regulation_mcp]\ncommand = "powershell.exe"\n',
                encoding="utf-8",
            )
            installed_fingerprint = "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
            legacy_success["direct_config_path"] = str(config_path)
            status_path.write_text(json.dumps(legacy_success), encoding="utf-8")
            missing_fingerprint, missing_error = read_diagnostic(tmp)

            status_path.write_text(
                json.dumps(
                    {
                        **legacy_success,
                        "installed_config_fingerprint": installed_fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            actual_fingerprint, actual_error = read_diagnostic(tmp)

        self.assertIsNone(missing_error)
        self.assertEqual("pending", missing_fingerprint["overall_state"])
        self.assertFalse(missing_fingerprint["configured"])
        for stage_name in ("registration", "loader", "transport", "fresh_app_server"):
            with self.subTest(stage_name=stage_name):
                self.assertEqual("pending", missing_fingerprint["stages"][stage_name]["state"])
                self.assertEqual(
                    "legacy_evidence_unattributed",
                    missing_fingerprint["stages"][stage_name]["reason_code"],
                )

        self.assertIsNone(actual_error)
        self.assertEqual(installed_fingerprint, actual_fingerprint["config_fingerprint"])
        self.assertEqual("configured", actual_fingerprint["overall_state"])
        self.assertTrue(actual_fingerprint["configured"])
        self.assertFalse(actual_fingerprint["connected"])
        for stage_name in ("registration", "loader", "transport", "fresh_app_server"):
            with self.subTest(stage_name=stage_name):
                self.assertEqual("verified", actual_fingerprint["stages"][stage_name]["state"])

    def test_mcp_connection_diagnostic_reader_rehashes_current_config_file(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        reader_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_read_mcp_connection_diagnostic"
        )
        namespace = {
            "Path": Path,
            "hashlib": hashlib,
            "json": json,
            "diagnostic_from_bundle_status": diagnostic_from_bundle_status,
        }
        exec(
            compile(ast.Module(body=[reader_node], type_ignores=[]), "<mcp-diagnostic-reader>", "exec"),
            namespace,
        )
        read_diagnostic = namespace["_read_mcp_connection_diagnostic"]

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            config_path = bundle_dir / "config.toml"
            config_path.write_text(
                '[mcp_servers.other]\ncommand = "unexpected"\n',
                encoding="utf-8",
            )
            stored_fingerprint = "sha256:" + ("a" * 64)
            actual_fingerprint = "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
            self.assertNotEqual(stored_fingerprint, actual_fingerprint)
            (bundle_dir / "bundle_status.json").write_text(
                json.dumps(
                    {
                        "installation_attempt_id": "attempt-current",
                        "installed_config_fingerprint": stored_fingerprint,
                        "direct_config_path": str(config_path),
                        "direct_config_registered": True,
                        "direct_config_loader_verified": True,
                        "direct_stdio_verified": True,
                        "transport_end_to_end_verified": True,
                        "desktop_app_server_loader_verified": True,
                    }
                ),
                encoding="utf-8",
            )

            report, read_error = read_diagnostic(bundle_dir)

        self.assertIsNone(read_error)
        self.assertEqual(actual_fingerprint, report["config_fingerprint"])
        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertTrue(
            any(
                report["stages"][stage_name]["state"] != "verified"
                for stage_name in ("registration", "loader", "transport", "fresh_app_server")
            )
        )

    def test_mcp_connection_diagnostic_reader_uses_claude_desktop_config(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        reader_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_read_mcp_connection_diagnostic"
        )
        namespace = {
            "Path": Path,
            "hashlib": hashlib,
            "json": json,
            "diagnostic_from_bundle_status": diagnostic_from_bundle_status,
        }
        exec(
            compile(ast.Module(body=[reader_node], type_ignores=[]), "<mcp-diagnostic-reader>", "exec"),
            namespace,
        )
        read_diagnostic = namespace["_read_mcp_connection_diagnostic"]

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            config_path = bundle_dir / "claude_desktop_config.json"
            config_path.write_text(
                json.dumps({"mcpServers": {"regulation_mcp": {"command": "powershell.exe"}}}),
                encoding="utf-8",
            )
            actual_fingerprint = "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
            runtime_fingerprint = "sha256:runtime-current"
            (bundle_dir / "bundle_status.json").write_text(
                json.dumps(
                    {
                        "installation_attempt_id": "attempt-claude",
                        "claude_desktop_config_path": str(config_path),
                        "claude_desktop_config_fingerprint": actual_fingerprint,
                        "claude_desktop_config_registered": True,
                        "claude_desktop_config_transport_verified": True,
                        "claude_desktop_config_transport_runtime_fingerprint": runtime_fingerprint,
                        "runtime_fingerprint": runtime_fingerprint,
                    }
                ),
                encoding="utf-8",
            )

            report, read_error = read_diagnostic(bundle_dir, "claude-desktop")

        self.assertIsNone(read_error)
        self.assertEqual("claude-desktop", report["connection_target"])
        self.assertEqual(actual_fingerprint, report["config_fingerprint"])
        self.assertTrue(report["configured"])
        self.assertEqual("not_applicable", report["stages"]["fresh_app_server"]["state"])

    def test_desktop_refresh_runs_observer_without_claiming_connection(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_refresh_mcp_connection_observation"
        )
        calls: list[list[str]] = []

        def fake_refresh(argv, *, stdout):
            calls.append(list(argv))
            stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "status_updated": True,
                        "connection_verified": False,
                    }
                )
            )
            return 0

        namespace = {
            "Path": Path,
            "io": io,
            "json": json,
            "refresh_mcp_client_connection": fake_refresh,
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<mcp-refresh>", "exec"),
            namespace,
        )

        refreshed, reason = namespace["_refresh_mcp_connection_observation"](
            "fixture-bundle",
            "chatgpt-desktop-local",
            "regulation_mcp",
        )

        self.assertTrue(refreshed)
        self.assertEqual("observation_recorded_pending", reason)
        self.assertEqual("chatgpt-desktop-local", calls[0][1])
        self.assertIn("--bundle-status", calls[0])
        self.assertIn("--bundle-dir", calls[0])
        self.assertIn("--adopt-manual-registration", calls[0])
        self.assertNotIn("--fail-on-issue", calls[0])

    def test_streamlit_distinguishes_configured_from_desktop_connected(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("diagnostic_from_bundle_status", source)
        self.assertIn('status_path = Path(bundle_dir) / "bundle_status.json"', source)
        self.assertIn("MCP 연결 상태 새로고침", source)
        self.assertIn("ChatGPT Desktop 연결 진단", source)
        self.assertIn("Codex CLI 연결 진단", source)
        self.assertIn("Claude Code 연결 진단", source)
        self.assertIn("Claude Desktop 연결 진단", source)
        self.assertNotIn("ChatGPT Desktop·Codex CLI 7단계 연결 진단", source)
        self.assertIn("재시작 후 최종 확인 프롬프트", source)
        self.assertIn("MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘.", source)
        self.assertIn('if diagnostic_state == "connected":', source)
        self.assertIn('"codex": "Codex CLI",', source)
        self.assertIn('"claude-code": "Claude Code",', source)
        self.assertIn('f"{diagnostic_client_label} 연결 완료', source)
        self.assertIn('f"MCP 구성 확인 완료 · {diagnostic_client_label} 최종 확인 대기', source)
        self.assertNotIn("MCP 구성 확인 완료 · Desktop 연결 확인 대기", source)
        self.assertIn("다른 앱의 현재 대화 결과를 자동으로 읽을 수 없으므로", source)
        self.assertIn("아래 최종 도구 호출 성공은 해당 대화에서 직접 확인", source)
        self.assertIn("support_summary:", source)
        self.assertIn("next_action:", source)
        self.assertIn("st.code(agent_prompt_text, language=None)", source)
        self.assertIn("_refresh_mcp_connection_observation(", source)
        self.assertIn("이 결과만으로 현재 대화의 도구 연결 완료를 주장하지 않습니다.", source)

    def test_mcp_http_url_builder_normalizes_local_and_public_urls(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        function_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_build_mcp_http_url"
        )
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=[function_node], type_ignores=[]), "<mcp-http-url-builder>", "exec"), namespace)
        build_url = namespace["_build_mcp_http_url"]

        self.assertEqual(
            build_url(host="127.0.0.1", port=8000),
            "http://127.0.0.1:8000/mcp",
        )
        self.assertEqual(
            build_url(host="0.0.0.0", port=8876),
            "http://127.0.0.1:8876/mcp",
        )
        self.assertEqual(
            build_url(host="127.0.0.1", port=8000, public_url="mcp.example.go.kr"),
            "https://mcp.example.go.kr/mcp",
        )
        self.assertEqual(
            build_url(host="127.0.0.1", port=8000, public_url="https://mcp.example.go.kr/mcp/"),
            "https://mcp.example.go.kr/mcp",
        )
        self.assertEqual(
            build_url(host="127.0.0.1", port=8000, public_url="https://mcp.example.go.kr/base?tenant=default"),
            "",
        )
        self.assertEqual(
            build_url(host="127.0.0.1", port=8000, public_url="https://?tenant=default"),
            "",
        )

    def test_streamlit_uses_latest_four_step_navigation_and_windows_save_controls(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("ADVANCED_NAV_PAGES = [NAV_GOLDSET, NAV_ADMIN]", source)
        self.assertNotIn("elif nav_page == NAV_CONNECT:", source)
        self.assertIn("if current_nav_page == LEGACY_NAV_CONNECT:", source)
        self.assertIn("API 연결 저장하기", source)
        self.assertIn('"AI 검수 공급자·모델·API 키 설정"', source)
        self.assertIn('"AI 검수 설정"', source)
        self.assertIn("background: #c62828 !important", source)
        self.assertNotIn("home-goto-goldset", source)
        self.assertIn("streamlit_operator_project_checkpoint", source)
        self.assertIn("규정명이 아니라 사람이 작업을 구분할 프로젝트 이름", source)
        self.assertIn('"프로젝트 저장·불러오기",\n    width="large",', source)
        self.assertIn(
            'save_spacer_col, save_button_col, load_button_col = st.columns([7, 1, 1], vertical_alignment="top")',
            source,
        )
        self.assertIn('with save_button_col:', source)
        self.assertIn('with load_button_col:', source)
        self.assertIn("_render_operator_project_controls(NAV_HOME)", source)
        self.assertIn("_render_operator_project_controls(NAV_PREPROCESS)", source)
        self.assertIn("_render_operator_project_controls(NAV_RESULTS)", source)
        self.assertIn("_render_operator_project_controls(NAV_APPROVAL)", source)
        self.assertIn("_render_operator_project_controls(NAV_MCP)", source)
        self.assertIn("Windows 탐색기에서 저장 폴더 선택", source)
        self.assertIn("저장하기 — Windows 탐색기에서 산출물 폴더 열기", source)

    def test_saved_projects_are_available_only_after_institution_selection(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        institution_page = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_page_institution_select"
        )
        institution_source = ast.get_source_segment(source, institution_page) or ""

        self.assertNotIn("institution-entry-project-choice", institution_source)
        self.assertNotIn("institution-entry-project-load", institution_source)
        self.assertNotIn("저장한 프로젝트 불러오기", institution_source)
        self.assertIn("_render_institution_registration_form(registry)", institution_source)
        self.assertIn("_render_operator_project_controls(NAV_HOME)", source)

    def test_streamlit_declares_protected_deployment_guard(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("settings.api_auth_required", source)
        self.assertIn("settings.tenant_storage_isolation", source)
        self.assertTrue(
            "Streamlit is disabled for protected or tenant-isolated deployments." in source
            or "보호 모드 또는 테넌트 분리 배포에서는 Streamlit 화면을 사용할 수 없습니다." in source
        )
        self.assertIn("st.stop()", source)

    def test_streamlit_exposes_table_and_quality_exports(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("to_tables_jsonl", source)
        self.assertIn("to_tables_csv", source)
        self.assertIn("공공기관 규정 MCP 빌더", source)
        self.assertIn("승인 handoff 준비", source)
        self.assertTrue("Public-institution handoff" in source or "기관 전달용 산출물" in source)
        self.assertTrue("Upload a regulation document" in source or "문서 업로드" in source)
        self.assertTrue("Start preprocessing" in source or "전처리 시작" in source)
        self.assertIn("st.tabs", source)
        self.assertTrue("Download Quality JSON" in source or "품질 JSON 다운로드" in source)
        self.assertIn(".quality.json", source)
        self.assertIn(".quality.md", source)

    def test_streamlit_keeps_multi_regulation_batch_selected_across_workflow(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn('WORKFLOW_DOCUMENT_IDS_KEY = "workflow_document_ids"', source)
        self.assertIn('WORKFLOW_SELECTED_DOCUMENT_IDS_KEY = "workflow_selected_document_ids"', source)
        self.assertIn("completed_document_ids = [item.document_id for item in completed_documents]", source)
        self.assertIn("st.session_state[WORKFLOW_SELECTED_DOCUMENT_IDS_KEY] = completed_document_ids", source)
        self.assertIn('def _render_workflow_document_directory(*, page_key: str)', source)
        self.assertIn('_render_workflow_document_directory(page_key="results")', source)
        self.assertIn('_render_workflow_document_directory(page_key="approval")', source)
        self.assertIn('_render_workflow_document_directory(page_key="mcp")', source)
        self.assertIn("함께 처리할 규정 디렉터리", source)
        self.assertIn("선택 청크 원문·전처리 결과", source)
        self.assertIn("_render_original_source_preview(ctx[\"document\"], selected_chunk)", source)
        self.assertIn("_render_processed_result_preview(selected_chunk, selected_chunk.text)", source)
        self.assertIn("선택 청크 전후 문맥", source)
        self.assertIn('st.tabs(["직전 청크", "현재 청크", "다음 청크"])', source)
        self.assertIn("previous_chunk = chunks[selected_chunk_index - 1]", source)
        self.assertIn("next_chunk = chunks[selected_chunk_index + 1]", source)
        self.assertIn("선택한 규정 {len(selected_document_ids):,}개 일괄 처리", source)
        self.assertIn("전체 규정 자료 AI 검수 완료 (선택 {len(selected_document_ids):,}개)", source)
        self.assertIn("전체 규정 자료 사람 확인 완료 (선택 {len(selected_document_ids):,}개)", source)
        self.assertIn("나머지 부분 AI 점검 전체 완료 (선택 {len(selected_document_ids):,}개)", source)
        self.assertIn("나머지 부분 사람 점검 전체 완료 (선택 {len(selected_document_ids):,}개)", source)
        self.assertIn("_prepare_reviewed_document_approval_plan", source)
        self.assertIn("_execute_reviewed_document_approval_plan", source)
        self.assertIn("규정별 문서 ID·규정 ID·목차 계층", source)
        self.assertIn('"selected_documents": f"선택한 규정 {len(selected_document_ids):,}개"', source)
        self.assertIn("document_ids=mcp_export_document_ids", source)

    def test_streamlit_exposes_secure_rag_review_gate(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("Secure RAG review gate", source)
        self.assertIn("OFFICIAL_RAG_MCP_REVIEW_REQUIRED_KEY", source)
        self.assertIn("UNREVIEWED_PREVIEW_WARNING", source)
        self.assertIn("UNREVIEWED_POC_REVIEW_ACK_KEY", source)
        self.assertIn("휴먼리뷰 후 공식 RAG/MCP 사용", source)
        self.assertIn("UNREVIEWED_POC_REVIEW", source)
        self.assertIn("isolated PoC Review mode", source)
        self.assertIn("must not write to official approved vectors", source)
        self.assertIn("I understand this is Unreviewed PoC Review only and not official RAG/MCP.", source)
        self.assertIn("poc_review_needs_ack", source)
        self.assertIn("disabled=poc_review_needs_ack", source)
        self.assertIn("Approve all chunks for RAG", source)
        self.assertIn("Index approved chunks", source)
        self.assertIn("Reindex approved chunks", source)
        self.assertIn("indexing_disabled = approved_count <= 0", source)
        self.assertIn("아직 승인된 청크가 없어 색인할 수 없습니다.", source)
        self.assertIn("disabled=indexing_disabled", source)
        self.assertIn("get_index_status", source)
        self.assertIn("MCP-visible records", source)
        self.assertIn("Approved chunks", source)
        self.assertIn("_mcp_connection_gate", source)
        self.assertIn("approved_chunks_indexed", source)
        self.assertIn("visible_record_count_mismatch", source)
        self.assertIn("smoke-test documents", source)
        self.assertIn("same data directory and tenant", source)
        self.assertIn("Approval worklist evidence", source)
        self.assertIn("_load_approval_template_from_manifest", source)
        self.assertIn("Approval review batch manifest JSON", source)
        self.assertIn("Review batch ID to load", source)
        self.assertIn("Load approval evidence from review batch manifest", source)
        self.assertIn("Approval evidence loaded. Review the batch before approving; acknowledgement was not auto-checked.", source)
        self.assertIn("AI 검증 확인", source)
        self.assertIn("사람 검증 확인", source)
        self.assertIn("원본과 전처리 결과를 확인했습니다.", source)
        self.assertIn("승인하고 색인", source)
        self.assertIn("확인 생략 승인 사유", source)
        self.assertIn("review_decision_events", source)
        self.assertIn("approval_override_reason", source)
        self.assertIn("_build_current_document_approval_templates", source)
        self.assertIn("review_batch_chunk_fingerprint", source)
        self.assertIn("streamlit_current_document_approval_evidence", source)
        self.assertIn("이미 승인된 내용 AI에 등록만 실행", source)
        self.assertIn("전산 담당자용 고급 승인 절차 보기", source)
        self.assertIn("show_advanced_approval", source)
        self.assertIn("return", source)
        self.assertIn("st.session_state[worklist_path_key]", source)
        self.assertIn("st.session_state[batch_manifest_sha_key]", source)
        self.assertIn("st.session_state[approval_chunk_ids_key] = template[\"chunk_ids\"]", source)
        self.assertIn("st.session_state[review_ack_key] = False", source)
        self.assertIn("selected_approval_chunk_ids", source)
        self.assertIn("approval_chunk_ids = selected_approval_chunk_ids or [chunk.chunk_id for chunk in chunks]", source)
        self.assertIn("Approve selected review batch for RAG", source)
        self.assertIn("Multiple approval review batches exist for this document.", source)
        self.assertIn("Worklist report path", source)
        self.assertIn("Worklist report SHA-256", source)
        self.assertIn("Review batch manifest path", source)
        self.assertIn("Review batch manifest SHA-256", source)
        self.assertIn("Review batch ID", source)
        self.assertIn("Review batch chunk fingerprint", source)
        self.assertIn("Review strategy", source)
        self.assertIn("required_approval_evidence", source)
        self.assertIn("approval_evidence_missing", source)
        self.assertIn("Official RAG/MCP approval requires approval worklist evidence.", source)
        self.assertIn("official_approval_disabled", source)
        self.assertIn("disabled=official_approval_disabled", source)
        self.assertIn("worklist_report_path=worklist_report_path", source)
        self.assertIn("worklist_report_sha256=worklist_report_sha256", source)
        self.assertIn("review_batch_manifest_path=review_batch_manifest_path", source)
        self.assertIn("review_batch_manifest_sha256=review_batch_manifest_sha256", source)
        self.assertIn("review_batch_id=review_batch_id", source)
        self.assertIn("review_batch_chunk_fingerprint=review_batch_chunk_fingerprint", source)
        self.assertIn("review_strategy=review_strategy", source)
        self.assertIn("Run demo", source)
        self.assertIn("RagChatRequest", source)
        self.assertIn('llm_backend="extractive"', source)
        self.assertIn("Local RAG demo uses approved and indexed chunks only.", source)
        self.assertIn('st.button("시범 실행 (Run demo)", key=f"run-rag-chat-{document_id}", disabled=not mcp_connection_ready)', source)

    def test_streamlit_exposes_mcp_client_config_generator(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("MCP 연결", source)
        self.assertIn('NAV_MCP = "④ MCP 생성·AI 연결"', source)
        self.assertIn('("4단계", "MCP 생성·AI 연결"', source)
        self.assertIn("PRIMARY_NAV_PAGES", source)
        self.assertIn("ADVANCED_NAV_PAGES", source)
        self.assertIn("_mcp_bundle_created", source)
        self.assertIn("_mcp_bundle_state_key", source)
        self.assertIn("MCP_BUNDLE_STATE_PREFIX", source)
        self.assertIn("mcp_first", source)
        self.assertIn("build_mcp_client_config", source)
        self.assertIn("write_mcp_setup_bundle", source)
        self.assertIn("write_mcp_setup_bundle_zip", source)
        self.assertIn("def _normalize_mcp_server_name", source)
        self.assertIn("def _default_mcp_server_name", source)
        self.assertIn("생성할 MCP 이름 (필수 입력)", source)
        self.assertIn('placeholder=f"예: {suggested_mcp_server_name}"', source)
        self.assertIn("사용자가 입력한 이름만 AI 앱에 등록됩니다.", source)
        self.assertIn("MCP 이름을 직접 입력해야 파일 묶음과 연결 설정을 생성할 수 있습니다.", source)
        self.assertNotIn("mcp_server_name_auto_key", source)
        self.assertNotIn("automatic_mcp_server_name", source)
        self.assertIn("server_name=mcp_server_name", source)
        self.assertIn("preferred_python=sys.executable", source)
        self.assertIn("preferred_project_root=PROJECT_ROOT", source)
        self.assertNotIn('server_name="govreg-local"', source)
        self.assertIn('"저장 방식"', source)
        self.assertIn('"folder-and-zip": "폴더 + 전달용 ZIP (권장)"', source)
        self.assertIn('"folder-only": "이 PC에 폴더만 저장"', source)
        self.assertIn('if mcp_save_mode == "folder-and-zip":', source)
        self.assertIn('"save_mode": mcp_save_mode', source)
        self.assertIn("def _ensure_mcp_output_directory_writable", source)
        self.assertIn("_ensure_mcp_output_directory_writable(mcp_bundle_output_dir)", source)
        self.assertIn("def _mcp_bundle_zip_output_path", source)
        self.assertIn("def _write_operator_mcp_bundle_zip", source)
        self.assertIn('return bundle_dir / f"{bundle_name}.zip"', source)
        self.assertIn("최종 ZIP 저장 위치", source)
        self.assertIn("기존 ZIP 파일이 사용 중이어서 새 이름으로 저장했습니다", source)
        self.assertIn("write_mcp_runtime_data_bundle", source)
        self.assertIn('scope="document" if mcp_scope == "current_document" else mcp_scope', source)
        self.assertIn("MCP_REQUIRED_SOURCE_METADATA_FIELDS", source)
        self.assertIn("_missing_mcp_source_metadata", source)
        self.assertIn("missing_mcp_source_metadata", source)
        self.assertIn('document = ctx["document"]', source)
        self.assertIn("requires citation/source metadata before export", source)
        self.assertIn("_ensure_mcp_source_metadata", source)
        self.assertIn("auto-fill local provenance and reindex approved chunks", source)
        self.assertIn("출처 메타데이터 자동 보완 후 다시 색인", source)
        self.assertIn("repair-mcp-source-metadata", source)
        self.assertIn("disabled=not mcp_connection_ready", source)
        self.assertIn("transport=mcp_transport", source)
        self.assertIn("Client profile", source)
        self.assertIn("연결할 AI 앱", source)
        self.assertIn("mcp_connection_target_labels", source)
        self.assertIn("mcp-connection-target", source)
        self.assertIn("에이전트 연결 요청문과 보조 BAT", source)
        self.assertIn("mcp_target_file_keys", source)
        self.assertIn("codex_agent_prompt", source)
        self.assertIn("connect_claude_desktop_bat", source)
        self.assertIn("claude_code_agent_prompt", source)
        self.assertIn("connect_chatgpt_https_bat", source)
        self.assertIn("connect_chatgpt_tunnel_bat", source)
        self.assertIn("connect_claude_https_bat", source)
        self.assertIn("chatgpt-desktop-local", source)
        self.assertIn("chatgpt-remote", source)
        self.assertIn("chatgpt-tunnel", source)
        self.assertIn('"chatgpt-desktop-local": "chatgpt_desktop_agent_prompt"', source)
        self.assertIn('"codex": "codex_agent_prompt"', source)
        self.assertIn('"claude-code": "claude_code_agent_prompt"', source)
        self.assertIn('"chatgpt-desktop-local": "ChatGPT Desktop"', source)
        self.assertIn('"codex": "Codex CLI"', source)
        self.assertIn('"chatgpt-remote": "ChatGPT 원격 MCP (HTTPS)"', source)
        self.assertIn('"chatgpt-tunnel": "ChatGPT 웹 (보안 Tunnel MCP)"', source)
        self.assertNotIn('"all-local": "로컬 AI 앱 모두"', source)
        target_order = [
            '"claude-code"',
            '"codex"',
            '"claude-desktop"',
            '"chatgpt-desktop-local"',
            '"chatgpt-remote"',
            '"chatgpt-tunnel"',
            '"claude-api"',
        ]
        options_start = source.index("mcp_connection_target_options = [")
        options_end = source.index("]", options_start)
        options_source = source[options_start:options_end]
        self.assertEqual(
            sorted(options_source.index(target) for target in target_order),
            [options_source.index(target) for target in target_order],
        )
        self.assertIn("connection_target_file", source)
        self.assertIn("사용자용 연결 파일", source)
        self.assertIn("Path(str(bundle_state.get('connection_target_file'))).name", source)
        self.assertIn("Path(str(selected_target_file)).name", source)
        self.assertIn("Path(str(zip_path)).name", source)
        self.assertIn("BAT 보조 연결 방식", source)
        self.assertIn("Claude Desktop 기본 BAT 연결 방식", source)
        self.assertIn("로컬 프로젝트/작업공간", source)
        self.assertIn("코드 상자 오른쪽 위의 복사 아이콘", source)
        self.assertIn("현재 번들의 폴더 이름·정확한 절대경로·핵심 파일 구조", source)
        self.assertIn("render_agent_connect_prompt_for_program", source)
        self.assertIn("bundle_dir=Path(str(agent_prompt_path)).parent", source)
        self.assertIn("st.code(agent_prompt_text, language=None)", source)
        self.assertIn("Settings > MCP servers > Add server", source)
        self.assertIn("Settings > Plugins", source)
        self.assertIn("https://chatgpt.com/plugins", source)
        self.assertIn("ChatGPT Plugins 설정에서 앱을 Refresh", source)
        self.assertIn("Settings > Apps > Advanced Settings", source)
        self.assertIn("Settings > Apps > Create", source)
        self.assertIn("Settings > Apps의 custom app을 갱신", source)
        self.assertIn("Claude Desktop은 전용 BAT가 기본", source)
        self.assertIn("설치 검증이 끝날 때까지 실행", source)
        self.assertIn("MCP의 list_regulations 도구를 사용해서 등록된 규정 목록을 보여줘", source)
        self.assertIn('"usage_guide_bat": files.get("usage_guide_bat")', source)
        self.assertIn('"codex_plugin_guide": files.get("codex_plugin_guide")', source)
        self.assertIn("현재 승인된 전체 청크와 추가·개정 청크", source)
        self.assertIn("선택된 연결 방식", source)
        self.assertIn('"http": "HTTPS URL"', source)
        self.assertIn('"tunnel": "보안 Tunnel"', source)
        self.assertIn('"local": "로컬 stdio"', source)
        self.assertIn("mcp_target_ready", source)
        self.assertIn("ChatGPT (보안 Tunnel MCP)를 선택하세요", source)
        self.assertIn("def _build_mcp_http_url", source)
        self.assertIn("생성된 MCP HTTP URL", source)
        self.assertIn('mcp_public_url = mcp_http_url if mcp_mode == "http" else ""', source)
        self.assertIn("Selected MCP transport", source)
        self.assertIn("claude-desktop", source)
        self.assertIn("claude-code", source)
        self.assertIn("chatgpt", source)
        self.assertIn("claude-api", source)
        self.assertIn("MCP 설정 JSON 다운로드", source)
        self.assertIn("MCP Quickstart", source)
        self.assertIn("Claude Code add-json arguments", source)
        self.assertIn("MCP 파일 묶음을 만들 폴더", source)
        self.assertIn('value="127.0.0.1"', source)
        self.assertIn("st.warning(warning)", source)
        self.assertIn("st.info(note)", source)
        self.assertIn("Generate a copy/paste setup bundle", source)
        self.assertIn("--zip-out", source)
        self.assertIn("Fastest ChatGPT/Claude connection wizard", source)
        self.assertIn("connect_mcp_client.ps1", source)
        self.assertIn("MCP visibility precheck before client registration", source)
        self.assertIn("reg-rag-mcp-index-visibility", source)
        self.assertIn("--forbid-smoke-docs", source)
        self.assertIn("--require-indexed", source)
        self.assertIn("--fail-on-issue", source)
        self.assertIn("MCP setup bundle can be written only after approved chunks are indexed and visible.", source)
        self.assertIn("mcp_connection_ready", source)
        self.assertIn("disabled=not mcp_connection_ready", source)
        self.assertIn("_mcp_kordoc_preflight", source)
        self.assertIn("Kordoc 표 파싱 사전 점검", source)
        self.assertNotIn("현재 Kordoc 명령은 사용 가능하지만", source)
        self.assertIn("_safe_kordoc_reprocess_documents", source)
        self.assertIn("설치된 Kordoc으로 증거가 없는 원본을 새 초안에서 재전처리합니다", source)
        self.assertIn("KORDOC_AUTO_REPROCESS_ATTEMPT_PREFIX", source)
        self.assertIn("_replace_workflow_document_id", source)
        self.assertIn("kordoc_auto_install_attempted", source)
        self.assertIn("Kordoc 설치·검증 다시 실행", source)
        self.assertIn("sys.prefix", source)
        self.assertIn("Draft MCP server command; approve and index chunks before connecting a client.", source)
        self.assertIn("MCP server command is ready for connection.", source)
        self.assertIn("MCP로 쓸 파일 묶음 만들기", source)
        self.assertIn("MCP 실행 데이터와 연결 파일 묶음을 만들었습니다.", source)
        self.assertIn("runtime_data_dir", source)
        self.assertIn("runtime_record_count", source)
        self.assertIn("st.session_state[_mcp_bundle_state_key(document_id)]", source)
        self.assertIn('"install_script": files.get("install")', source)
        self.assertIn("claude_code_stdio_ps", source)
        self.assertIn("ChatGPT data-only search/fetch server", source)
        self.assertIn("OpenAI Secure MCP Tunnel for ChatGPT", source)
        self.assertIn("chatgpt_connector_url", source)
        self.assertIn("chatgpt_https_endpoint_ready", source)
        self.assertIn("--http-bearer-token-env MCP_AUTH_TOKEN", source)
        self.assertIn("MCP/AI connection guide", source)
        self.assertIn("AI review draft generation is part of the main preprocessing flow", source)
        self.assertIn("OPENAI_API_KEY", source)
        self.assertIn("Codex can connect as an MCP client", source)
        self.assertIn("not a replacement API key for this product runtime", source)
        self.assertIn("아래 버튼을 누르면 Claude Desktop/Claude Code/ChatGPT/Claude API 연결에 필요한 파일 묶음이 생성됩니다.", source)
        self.assertIn("AI review API and cost guard", source)
        self.assertIn("cached_candidate_count", source)
        self.assertIn("cost_estimate_status", source)

    def test_streamlit_reflects_parser_ai_review_human_approval_stages(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        # 파서 초안 → AI 검수 → 휴먼 승인 3단계 진행 띠가 정의되고 각 페이지에서 렌더링돼야 한다.
        self.assertIn("PIPELINE_STAGES", source)
        self.assertIn("파서 초안", source)
        self.assertIn("AI 검수", source)
        self.assertIn("휴먼 승인", source)
        self.assertIn("def _render_pipeline_stages", source)
        self.assertIn("_render_pipeline_stages(PIPELINE_STAGE_PARSER)", source)
        self.assertIn("_render_pipeline_stages(PIPELINE_STAGE_AI_REVIEW)", source)
        self.assertIn("_render_pipeline_stages(PIPELINE_STAGE_HUMAN_APPROVAL)", source)

        # AI 검수 결과가 숨은 비용 익스팬더가 아니라 결과 화면의 정식 패널로 노출돼야 한다.
        self.assertIn("AI 검수 결과", source)
        self.assertIn("def _ai_review_status_text", source)
        self.assertIn("AI가 살펴본 후보", source)
        self.assertIn("AI가 검토 대상으로 고른 청크", source)
        self.assertIn("사람이 꼭 볼 청크", source)
        # 기술 상세(비용 가드)는 유지하되 전산 담당자용으로 접어 둔다.
        self.assertIn("AI review API and cost guard", source)

    def test_connection_handoff_uses_current_chatgpt_plugin_flow(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertNotIn("비개발자는 이 파일만 더블클릭하면 됩니다", source)
        self.assertNotIn("전용 연결 버튼을 실행하면 됩니다", source)
        self.assertIn("연결 준비 버튼을 만들었습니다", source)
        self.assertIn("아래 대상별 등록·재시작·최종 확인 절차", source)
        self.assertIn("새 대화에서 + > More를 열어 앱을 선택", source)
        self.assertNotIn("앱을 선택하거나 @이름", source)

    def test_final_verification_prompts_match_the_client_tool_profile(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_mcp_final_verification_prompts"
        )
        namespace = {
            "MCP_EXTERNAL_DATA_TARGETS": frozenset(
                {"chatgpt-remote", "chatgpt-tunnel", "claude-api"}
            )
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<mcp-verification-prompts>", "exec"),
            namespace,
        )
        prompts_for = namespace["_mcp_final_verification_prompts"]

        for target in ("chatgpt-remote", "chatgpt-tunnel", "claude-api"):
            with self.subTest(target=target):
                remote_prompts = prompts_for(target, "govreg-local")
                self.assertEqual(1, len(remote_prompts))
                self.assertIn("search 도구", remote_prompts[0])
                self.assertIn("fetch 도구", remote_prompts[0])
                self.assertNotIn("get_index_status", remote_prompts[0])
                self.assertNotIn("list_regulations", remote_prompts[0])

        local_prompts = prompts_for("chatgpt-desktop-local", "govreg-local")
        self.assertEqual(2, len(local_prompts))
        self.assertIn("get_index_status", local_prompts[0])
        self.assertIn("list_regulations", local_prompts[1])

    def test_streamlit_exposes_parsing_goldset_review_gate(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("_render_parsing_goldset_review_panel", source)
        self.assertIn("Parsing goldset review gate", source)
        self.assertIn("Goldset label CSV", source)
        self.assertIn("reports/parsing_manual_goldset_labels_20260710-current.csv", source)
        self.assertIn("Open label CSV", source)
        self.assertIn("Open source file", source)
        self.assertIn("Open review packet", source)
        self.assertIn('glob("parsing_goldset_review_packets*")', source)
        self.assertIn("Save goldset review row", source)
        self.assertIn("_goldset_row_validation_issues", source)
        self.assertIn("_goldset_metric_summary", source)
        self.assertIn("_goldset_detail_text", source)
        self.assertIn("_write_goldset_label_rows", source)
        self.assertIn("matched count cannot exceed manual count", source)
        self.assertIn('"항목", "자동", "직접", "일치", "FP", "FN", "정밀도", "재현율", "상태"', source)
        self.assertIn("자동 세부값", source)
        self.assertIn('"false_positive"', source)
        self.assertIn('"false_negative"', source)
        self.assertIn("Goldset review measures parser accuracy. It does not approve operational chunks", source)


if __name__ == "__main__":
    unittest.main()
