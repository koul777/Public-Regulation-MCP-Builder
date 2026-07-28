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
                "claude-api",
            ):
                with self.subTest(target=target):
                    report, read_error = read_diagnostic(tmp, target)
                    self.assertIsNone(read_error)
                    self.assertEqual("client_connections", report.get("status_source"))
                    self.assertIsNone(report["attempt_id"])
                    self.assertIsNone(report["config_fingerprint"])
                    self.assertFalse(report["configured"])

    def test_mcp_connection_diagnostic_reader_shows_v5_manual_registration(self):
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

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            config_path = bundle_dir / "config.toml"
            config_path.write_text(
                '[mcp_servers.final]\ncommand = "powershell.exe"\n',
                encoding="utf-8",
            )
            config_fingerprint = "sha256:" + hashlib.sha256(
                config_path.read_bytes()
            ).hexdigest()
            status = begin_attempt(
                create_bundle_status(
                    "final",
                    runtime_fingerprint="runtime-current",
                    generated_at="2026-07-21T00:00:00Z",
                ),
                "chatgpt-desktop-local",
                "manual-settings-attempt",
                started_at="2026-07-21T00:01:00Z",
            )
            status = commit_success(
                status,
                "chatgpt-desktop-local",
                "manual-settings-attempt",
                verified_stages=("registration",),
                config_entry_fingerprint=config_fingerprint,
                bundle_location_fingerprint=str(bundle_dir),
                verified_at="2026-07-21T00:02:00Z",
            )
            status["direct_config_path"] = str(config_path)
            (bundle_dir / "bundle_status.json").write_text(
                json.dumps(status),
                encoding="utf-8",
            )

            report, read_error = read_diagnostic(
                bundle_dir,
                "chatgpt-desktop-local",
            )

        self.assertIsNone(read_error)
        self.assertEqual("client_connections", report["status_source"])
        self.assertEqual("manual-settings-attempt", report["attempt_id"])
        self.assertEqual("completed", report["last_attempt_state"])
        self.assertEqual("verified", report["stages"]["registration"]["state"])
        self.assertEqual("not_checked", report["stages"]["transport"]["state"])
        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertFalse(report["connected"])

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
        self.assertNotIn("재시작 후 최종 확인 프롬프트", source)
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
        self.assertNotIn("st.code(agent_prompt_text, language=None)", source)
        self.assertNotIn("_mcp_agent_prompt_display_kind(prompt_path)", source)
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

    def test_connection_handoff_uses_direct_stdio_and_vercel_http(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertNotIn("비개발자는 이 파일만 더블클릭하면 됩니다", source)
        self.assertNotIn("전용 연결 버튼을 실행하면 됩니다", source)
        self.assertNotIn('mcp_mode == "tunnel"', source)
        self.assertNotIn('files.get("openai_tunnel")', source)
        self.assertIn("ChatGPT Desktop / Codex CLI / Codex IDE (공용 설정)", source)
        self.assertIn("ChatGPT · Vercel HTTPS MCP", source)
        self.assertIn("Claude · Vercel HTTPS MCP", source)
        self.assertIn("배포된 Vercel HTTPS `/mcp` 주소 (필수)", source)
        options_start = source.index("mcp_connection_target_options = [")
        options_end = source.index("]", options_start)
        options_source = source[options_start:options_end]
        self.assertEqual(
            [
                options_source.index(f'"{target}"')
                for target in (
                    "claude-code",
                    "codex",
                    "claude-desktop",
                    "chatgpt-remote",
                    "claude-api",
                )
            ],
            sorted(
                options_source.index(f'"{target}"')
                for target in (
                    "claude-code",
                    "codex",
                    "claude-desktop",
                    "chatgpt-remote",
                    "claude-api",
                )
            ),
        )
        self.assertNotIn('"claude-remote"', options_source)
        self.assertNotIn('"chatgpt-tunnel"', options_source)
        self.assertNotIn('"chatgpt-desktop-plugin"', options_source)
        self.assertIn('elif mcp_connection_target == "claude-api":', source)
        self.assertIn('mcp_profile = "claude-remote"', source)
        self.assertIn('"http": "Vercel HTTPS /mcp"', source)
        self.assertIn('"local": "로컬 stdio"', source)
        self.assertIn(
            '"command": desktop_registration.get("command")',
            source,
        )
        self.assertIn(
            '"args": desktop_registration.get("arguments") or []',
            source,
        )
        self.assertIn(
            '"cwd": desktop_registration.get("working_directory")',
            source,
        )
        self.assertIn(
            '"chatgpt_desktop_local_config": str(',
            source,
        )
        self.assertIn(
            "_read_chatgpt_codex_desktop_registration(",
            source,
        )
        self.assertIn(
            "_render_chatgpt_codex_desktop_registration_guide(",
            source,
        )
        self.assertIn(
            'mcp_quickstart.get("chatgpt_remote")',
            source,
        )
        self.assertIn("Streamable HTTP", source)
        self.assertIn("bearer", source)
        self.assertIn("OAuth", source)

        target_files_start = source.index("mcp_target_file_keys = {")
        target_files_end = source.index("}", target_files_start)
        target_files_source = source[target_files_start:target_files_end]
        self.assertIn('"claude-api": "claude_remote"', target_files_source)
        for retired in (
            "connect_codex_bat",
            "connect_chatgpt_desktop_bat",
            "AGENT_CONNECT_PROMPT",
            "plugin",
            "tunnel",
        ):
            self.assertNotIn(retired, target_files_source)

    def test_chatgpt_codex_desktop_registration_uses_generated_ui_fields(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        module = ast.parse(source)
        helper_names = {
            "_mcp_argument_value",
            "_chatgpt_codex_desktop_registration",
            "_read_chatgpt_codex_desktop_registration",
        }
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        namespace = {
            "Any": Any,
            "Path": Path,
            "json": json,
        }
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<desktop-registration-guide>",
                "exec",
            ),
            namespace,
        )
        read_registration = namespace[
            "_read_chatgpt_codex_desktop_registration"
        ]

        with tempfile.TemporaryDirectory() as tmp:
            config_path = (
                Path(tmp)
                / "한글 경로"
                / "chatgpt_desktop_local_mcp.json"
            )
            config_path.parent.mkdir()
            dynamic_cwd = str(config_path.parent.resolve())
            dynamic_command = str(
                (config_path.parent / "동적 런타임" / "powershell.exe").resolve()
            )
            dynamic_args = [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(
                    (
                        config_path.parent
                        / "run_mcp_stdio_server.ps1"
                    ).resolve()
                ),
                "--profile-id",
                "profile-runtime-782",
                "--tool-profile",
                "chatgpt-data-runtime",
                "--data-dir",
                str((config_path.parent / "승인 데이터").resolve()),
            ]
            payload = {
                "server_name": "ignored-fallback-name",
                "ui_fields": {
                    "name": "generated-server-782",
                    "transport": "stdio",
                    "command": dynamic_command,
                    "cwd": dynamic_cwd,
                    "args": dynamic_args,
                    "env": {},
                    "env_passthrough": [],
                },
            }
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            registration = read_registration(config_path)

            self.assertEqual("generated-server-782", registration["name"])
            self.assertEqual("STDIO", registration["transport"])
            self.assertEqual(dynamic_command, registration["command"])
            self.assertEqual(dynamic_cwd, registration["working_directory"])
            self.assertEqual(dynamic_args, registration["arguments"])
            self.assertEqual("\n".join(dynamic_args), registration["arguments_copy"])
            self.assertEqual(
                [f"{index}. {value}" for index, value in enumerate(dynamic_args, 1)],
                registration["numbered_arguments"],
            )
            self.assertEqual("입력하지 않음", registration["environment_display"])
            self.assertEqual(
                "입력하지 않음",
                registration["environment_passthrough_display"],
            )
            self.assertEqual("profile-runtime-782", registration["profile_id"])
            self.assertEqual(
                "chatgpt-data-runtime",
                registration["tool_profile"],
            )
            self.assertFalse(registration["command_matches_server_name"])

            payload["ui_fields"]["command"] = "generated-server-782"
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            collision = read_registration(config_path)
            self.assertTrue(collision["command_matches_server_name"])
            self.assertEqual(
                "generated-server-782",
                payload["ui_fields"]["command"],
            )

        helper_start = source.index(
            "def _chatgpt_codex_desktop_registration("
        )
        helper_end = source.index(
            "def _direct_python_mcp_config(",
            helper_start,
        )
        helper_source = source[helper_start:helper_end]
        self.assertIn('ui_fields.get("command")', helper_source)
        self.assertIn('ui_fields.get("cwd")', helper_source)
        self.assertIn('ui_fields.get("args")', helper_source)
        self.assertIn(
            '_mcp_argument_value(arguments, "--profile-id")',
            helper_source,
        )
        self.assertIn(
            '_mcp_argument_value(arguments, "--tool-profile")',
            helper_source,
        )
        self.assertNotIn("C:\\ttt", helper_source)
        self.assertNotIn("profile-default", helper_source)
        self.assertNotIn("\ufffd", helper_source)

    def test_chatgpt_codex_desktop_registration_guide_has_required_warnings(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        guide_start = source.index(
            "def _render_chatgpt_codex_desktop_registration_guide("
        )
        guide_end = source.index(
            "def _direct_python_mcp_config(",
            guide_start,
        )
        guide_source = source[guide_start:guide_end]

        for expected in (
            "ChatGPT Desktop에 등록하는 방법",
            "Name (MCP 서버 이름)",
            "Transport",
            "Command 복사",
            "Working directory 복사",
            "Arguments (",
            "한 줄씩 따로 복사",
            "첫 번째 인자 칸에 Argument 1",
            "`+ 인자 추가`",
            "아래 한 줄만 복사",
            "Environment (",
            "키와 값을 따로 복사",
            "왼쪽 키 칸에 복사",
            "오른쪽 값 칸에 복사",
            "Environment passthrough (",
            "Passthrough",
            "입력하지 않음",
            "MCP 서버 이름은 Name에만 입력합니다.",
            "Command에는 서버 이름을 입력하지 않습니다.",
            "각 Argument는 한 입력 칸에 하나씩 순서대로 넣어야 합니다.",
            "Arguments를 일부라도 누락하면 서버가 실행되지 않습니다.",
            "자동 수정하지 않았으므로",
            "왼쪽 아래 계정 > 설정 > 플러그인 > MCP > ",
            "+ 서버 추가 > STDIO",
            "위 Name을 ChatGPT의 이름 칸에 넣습니다.",
            "Argument 1을 첫 인자 칸에 넣고 `+ 인자 추가`",
            "Environment 첫 키·값은 이미 보이는 첫 행",
            "Environment passthrough 첫 값도 이미 보이는 첫 칸",
            "오른쪽 아래 저장을 누릅니다.",
            "ChatGPT Desktop을 완전 종료했다가 다시 실행합니다.",
            "설정 > 플러그인 > MCP에서 새 서버를 켭니다.",
            "`search`와 `fetch`를 실제로 호출해 연결을 검증합니다.",
        ):
            self.assertIn(expected, guide_source)
        self.assertNotIn("\ufffd", guide_source)

    def test_chatgpt_codex_desktop_registration_renders_each_value_separately(
        self,
    ) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        module = ast.parse(source)
        renderer_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_render_chatgpt_codex_desktop_registration_guide"
        )

        class FakeStreamlit:
            def __init__(self) -> None:
                self.events: list[tuple[str, str]] = []

            def markdown(self, value: str) -> None:
                self.events.append(("markdown", value))

            def caption(self, value: str) -> None:
                self.events.append(("caption", value))

            def info(self, value: str) -> None:
                self.events.append(("info", value))

            def code(self, value: str, *, language=None) -> None:
                self.events.append(("code", value))

            def warning(self, value: str) -> None:
                self.events.append(("warning", value))

            def error(self, value: str) -> None:
                self.events.append(("error", value))

        fake_st = FakeStreamlit()
        namespace = {"Any": Any, "st": fake_st}
        exec(
            compile(
                ast.Module(body=[renderer_node], type_ignores=[]),
                "<chatgpt-desktop-registration-guide>",
                "exec",
            ),
            namespace,
        )
        render = namespace["_render_chatgpt_codex_desktop_registration_guide"]
        arguments = [
            "-NoProfile",
            "-File",
            r"C:\MCP 번들\기관 규정\run_mcp_stdio_server.ps1",
        ]
        render(
            {
                "name": "기관 규정",
                "transport": "STDIO",
                "command": "powershell.exe",
                "arguments": arguments,
                "environment": {
                    "PYTHONPATH": r"C:\Users\테스트 사용자\Public Regulation MCP",
                    "PYTHONSAFEPATH": "1",
                },
                "environment_passthrough": ["REG_RAG_TOKEN"],
                "working_directory": r"C:\MCP 번들\기관 규정",
                "profile_id": "institution-test",
                "tool_profile": "full",
                "command_matches_server_name": False,
            }
        )

        code_values = [
            value for event, value in fake_st.events if event == "code"
        ]
        self.assertEqual(
            [
                "기관 규정",
                "powershell.exe",
                *arguments,
                "PYTHONPATH",
                r"C:\Users\테스트 사용자\Public Regulation MCP",
                "PYTHONSAFEPATH",
                "1",
                "REG_RAG_TOKEN",
                r"C:\MCP 번들\기관 규정",
            ],
            code_values,
        )
        rendered_text = "\n".join(value for _, value in fake_st.events)
        for expected in (
            "Argument 1/3",
            "Argument 2/3",
            "Argument 3/3",
            "Environment 1/2 — 왼쪽 키 칸에 복사",
            "Environment 2/2 — 오른쪽 값 칸에 복사",
            "Passthrough 1/1",
            "마지막 Argument 3까지 각각 다른 칸",
        ):
            self.assertIn(expected, rendered_text)

    def test_method_b_completion_separates_chatgpt_form_from_codex_toml(
        self,
    ) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        module = ast.parse(source)
        reader_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_read_codex_config_snippet"
        )
        renderer_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_render_codex_registration_guide"
        )

        with tempfile.TemporaryDirectory() as tmp:
            snippet_path = Path(tmp) / "코덱스 번들" / "codex_config_snippet.toml"
            snippet_path.parent.mkdir()
            snippet = (
                "[mcp_servers.기관_규정]\n"
                'command = "powershell.exe"\n'
                'args = ["-NoProfile"]\n'
            )
            snippet_path.write_text(snippet, encoding="utf-8")
            namespace = {"Path": Path}
            exec(
                compile(
                    ast.Module(body=[reader_node], type_ignores=[]),
                    "<codex-config-reader>",
                    "exec",
                ),
                namespace,
            )
            self.assertEqual(
                snippet,
                namespace["_read_codex_config_snippet"](snippet_path),
            )

        class FakeStreamlit:
            def __init__(self) -> None:
                self.events: list[tuple[str, str]] = []

            def markdown(self, value: str) -> None:
                self.events.append(("markdown", value))

            def caption(self, value: str) -> None:
                self.events.append(("caption", value))

            def info(self, value: str) -> None:
                self.events.append(("info", value))

            def code(self, value: str, *, language=None) -> None:
                self.events.append(("code", value))

            def warning(self, value: str) -> None:
                self.events.append(("warning", value))

        fake_st = FakeStreamlit()
        namespace = {"st": fake_st}
        exec(
            compile(
                ast.Module(body=[renderer_node], type_ignores=[]),
                "<codex-registration-guide>",
                "exec",
            ),
            namespace,
        )
        namespace["_render_codex_registration_guide"](
            snippet,
            generated_config_path=r"C:\MCP 번들\codex_config_snippet.toml",
        )
        rendered_text = "\n".join(value for _, value in fake_st.events)
        for expected in (
            "Codex CLI / Codex IDE에 등록하는 방법",
            "`%USERPROFILE%\\.codex\\config.toml`",
            "notepad %USERPROFILE%\\.codex\\config.toml",
            "파일 맨 아래",
            "기존 그 블록만 지운 뒤 새 블록으로 바꿉니다.",
            "`search`",
            "`fetch`",
        ):
            self.assertIn(expected, rendered_text)
        code_values = [
            value for event, value in fake_st.events if event == "code"
        ]
        self.assertEqual(
            [
                r"C:\MCP 번들\codex_config_snippet.toml",
                snippet,
            ],
            code_values,
        )

        self.assertIn(
            'if installed_target == "chatgpt-desktop-local":',
            source,
        )
        self.assertIn('elif installed_target == "codex":', source)
        for expected in (
            "방법 B에서 실제로 연결할 앱 하나 선택",
            "이번에 연결할 앱",
            "ChatGPT Desktop — 인자를 한 줄씩 서로 다른 칸에 입력",
            "Codex CLI / Codex IDE — 생성된 TOML 블록 전체 붙여 넣기",
            'if method_b_destination == "chatgpt-desktop-local":',
            "diagnostic_target =",
        ):
            self.assertIn(expected, source)
        self.assertNotIn(
            'if installed_target in {"chatgpt-desktop-local", "codex"}:',
            source,
        )

    def test_claude_desktop_final_guide_uses_generated_config(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        module = ast.parse(source)
        reader_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_read_claude_desktop_registration"
        )
        namespace = {
            "Any": Any,
            "Path": Path,
            "json": json,
        }
        exec(
            compile(
                ast.Module(body=[reader_node], type_ignores=[]),
                "<claude-desktop-registration-guide>",
                "exec",
            ),
            namespace,
        )
        read_registration = namespace["_read_claude_desktop_registration"]

        with tempfile.TemporaryDirectory() as tmp:
            config_path = (
                Path(tmp) / "클로드 번들" / "claude_desktop_config.json"
            )
            config_path.parent.mkdir()
            dynamic_args = [
                "-NoProfile",
                "-File",
                str((config_path.parent / "런처.ps1").resolve()),
                "--profile-id",
                "claude-profile-dynamic",
            ]
            payload = {
                "mcpServers": {
                    "claude-server-dynamic": {
                        "command": str(
                            (config_path.parent / "powershell.exe").resolve()
                        ),
                        "args": dynamic_args,
                        "cwd": str(config_path.parent.resolve()),
                        "env": {},
                    }
                }
            }
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            registration = read_registration(config_path)

        self.assertEqual("claude-server-dynamic", registration["name"])
        self.assertEqual(dynamic_args, registration["arguments"])
        self.assertEqual(
            payload,
            registration["merge_payload"],
        )
        self.assertEqual(
            payload,
            json.loads(registration["merge_json"]),
        )
        self.assertEqual(
            payload["mcpServers"],
            json.loads("{" + registration["server_entry_json"] + "}"),
        )

        guide_start = source.index(
            "def _render_claude_desktop_registration_guide("
        )
        guide_end = source.index(
            "def _direct_python_mcp_config(",
            guide_start,
        )
        guide_source = source[guide_start:guide_end]
        for expected in (
            "Claude Desktop에 등록하는 방법",
            "생성된 설정 파일 경로 복사",
            r"%APPDATA%\Claude\claude_desktop_config.json",
            "첫 번째 JSON 상자는 빈 `claude_desktop_config.json` 파일 전체를 덮어쓸 때",
            "두 번째 JSON 상자는 기존 서버가 이미 있을 때",
            "`mcpServers` 안에만 추가합니다.",
            "처음 연결할 때: 설정 파일 전체에 붙여 넣을 JSON 복사",
            "기존 서버가 있을 때: `mcpServers` 안에 넣을 새 서버 한 항목 복사",
            "붙여 넣을 위치 확인",
            "기존 마지막 서버 `}` 뒤에 쉼표 `,`를 하나 붙입니다.",
            "기존 `preferences` 같은 최상위 설정은 `mcpServers` 밖에 그대로 둡니다.",
            "설정 > 개발자 > 로컬 MCP 서버 > 구성 편집",
            "트레이까지 완전 종료",
            "상태가 `running`인지 확인합니다.",
            "`search`와 `fetch`를 실제로 호출해 연결을 검증합니다.",
            "원격 HTTPS MCP용",
            "로컬 STDIO MCP",
        ):
            self.assertIn(expected, guide_source)
        self.assertIn(
            '"claude_desktop_config": str(files["claude_desktop"])',
            source,
        )
        self.assertIn("_read_claude_desktop_registration(", source)
        self.assertIn("_render_claude_desktop_registration_guide(", source)
        self.assertNotIn("\ufffd", guide_source)

    def test_connection_handoff_does_not_render_copy_paste_prompts(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertNotIn("def _mcp_final_verification_prompts", source)
        self.assertNotIn("재시작 후 최종 확인 프롬프트", source)
        self.assertNotIn("새 대화 또는 새 task에 아래 문장을 그대로 입력합니다.", source)

    def test_bundle_completion_renders_mcp_connection_course(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_render_mcp_completion_connection_course"
        )
        helper_source = ast.get_source_segment(source, helper_node) or ""

        for expected in (
            "로컬 STDIO",
            "Vercel",
            "HTTPS",
            "파일 묶음 생성 완료가 Vercel 배포 완료를 뜻하지 않습니다",
            "Direct Python(프로젝트 Python 직접 실행)",
            "command/args/env",
            "PowerShell 래퍼는 fallback",
            "초보자 실수 방지",
            "Settings > Developer > Edit Config",
            "Connectors",
            "HTTPS /mcp",
            "doctor_mcp_connection.ps1",
            "validate_mcp_smoke.ps1",
            "reg-rag-mcp-vercel-stage",
            "reg-rag-mcp-client-config-smoke",
            "MCP_ALLOW_UNAUTHENTICATED_HTTP",
            "MCP_TOOL_PROFILE",
            "방법 A · Claude Code 로컬 STDIO",
            "방법 B · ChatGPT Desktop / Codex CLI / Codex IDE 로컬 STDIO",
            "방법 C · Claude Desktop 로컬 STDIO",
            "방법 D · ChatGPT · Vercel HTTPS MCP",
            "방법 E · Claude · Vercel HTTPS MCP",
            "`Claude Code`는 Claude CLI용, `Claude Desktop`은 설정 JSON 편집용",
            "search",
            "fetch",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, helper_source)

        self.assertIn('"vercel"', helper_source)
        self.assertIn('"--prod", "--cwd"', helper_source)
        self.assertIn("이 PC의 폴더·Command·", helper_source)
        self.assertIn("Arguments를 입력하지 않습니다", helper_source)
        completion_start = source.index(
            'if isinstance(bundle_state, dict) and bundle_state.get("written"):'
        )
        completion_source = source[completion_start:]
        self.assertIn("_render_mcp_completion_connection_course(", completion_source)
        for generated_file_label in (
            "ChatGPT/Codex HTTPS 설정",
            "Claude HTTPS 설정",
            "Vercel 원격 검증",
            "Claude Code HTTPS 등록",
        ):
            self.assertIn(generated_file_label, source)

    def test_bundle_completion_course_renders_local_and_remote_values(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(
            encoding="utf-8"
        )
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_render_mcp_completion_connection_course"
        )

        class StreamlitRecorder:
            def __init__(self):
                self.values: list[str] = []

            def __getattr__(self, _name):
                def record(*args, **_kwargs):
                    self.values.extend(str(value) for value in args)

                return record

        recorder = StreamlitRecorder()

        def powershell_command(command, args=None):
            return " ".join([str(command), *(str(value) for value in (args or []))])

        namespace = {
            "Path": Path,
            "st": recorder,
            "_powershell_command": powershell_command,
        }
        exec(
            compile(
                ast.Module(body=[helper_node], type_ignores=[]),
                "<mcp-completion-course>",
                "exec",
            ),
            namespace,
        )
        render = namespace["_render_mcp_completion_connection_course"]

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            runtime_dir = bundle_dir / "data"
            render(
                target="claude-desktop",
                server_name="local-demo",
                bundle_dir=str(bundle_dir),
                runtime_data_dir=str(runtime_dir),
                connection_display_value="{}",
            )
            local_output = "\n".join(recorder.values)
            self.assertIn("local-demo", local_output)
            self.assertIn("doctor_mcp_connection.ps1", local_output)
            self.assertIn("validate_mcp_smoke.ps1", local_output)
            self.assertIn("connect_mcp_client.ps1", local_output)
            self.assertIn("-InstallClaudeDesktop", local_output)
            self.assertIn("Installed-config stdio verification passed", local_output)
            self.assertIn("Settings > Developer > Edit Config", local_output)
            self.assertIn("서버 이름을 Command 칸에 직접 쓰지 않습니다", local_output)

            recorder.values.clear()
            render(
                target="claude-api",
                server_name="remote-demo",
                bundle_dir=str(bundle_dir),
                runtime_data_dir=str(runtime_dir),
                connection_display_value="https://example.test/mcp",
            )
            remote_output = "\n".join(recorder.values)
            self.assertIn("https://example.test/mcp", remote_output)
            self.assertIn("reg-rag-mcp-vercel-stage", remote_output)
            self.assertIn("vercel --prod --cwd", remote_output)
            self.assertIn("reg-rag-mcp-client-config-smoke", remote_output)
            self.assertIn("Claude/ChatGPT 원격 커넥터에는 URL만 넣습니다", remote_output)

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

    def test_streamlit_progress_uses_real_callbacks_and_separate_bundle_stages(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        helper_start = source.index("def _run_background_operation_with_progress(")
        helper_end = source.index("def _write_operator_mcp_bundle_zip(", helper_start)
        helper_source = source[helper_start:helper_end]
        self.assertIn("last_update_at", helper_source)
        self.assertIn("status_box", helper_source)
        self.assertNotIn("estimated_percent", helper_source)
        self.assertNotIn("elapsed_fraction", helper_source)

        bundle_start = source.index('"MCP로 쓸 파일 묶음 만들기"')
        bundle_end = source.index("bundle_state = st.session_state.get", bundle_start)
        bundle_source = source[bundle_start:bundle_end]
        self.assertIn('start_percent=35', bundle_source)
        self.assertIn('end_percent=78', bundle_source)
        self.assertIn('label="MCP 데이터·검색 인덱스 생성"', bundle_source)
        self.assertIn("current_bundle_regulation", bundle_source)
        self.assertIn('bundle_progress.progress(100, text="MCP 파일 묶음 생성 완료 · 100%")', bundle_source)
        self.assertIn('state="error"', bundle_source)
        self.assertIn("전체 작업을 중단했습니다", bundle_source)
        self.assertNotIn("time.sleep(0.12)", bundle_source)

        self.assertIn("progress_callback=report", source)
        self.assertIn('progress_callback(50, "검색 인덱스 생성", 0, 1)', source)


if __name__ == "__main__":
    unittest.main()
