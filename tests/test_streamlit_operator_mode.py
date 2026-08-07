from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from types import SimpleNamespace
from unittest.mock import patch

from scripts.mcp_connection_diagnostic import diagnostic_from_bundle_status
from scripts.mcp_client_status import begin_attempt, commit_success, create_bundle_status


REPO_ROOT = Path(__file__).resolve().parents[1]


class StreamlitOperatorModeTests(unittest.TestCase):
    def test_reviewed_document_plan_reuses_index_created_during_revision_approval(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_execute_reviewed_document_approval_plan"
        )
        index_calls: list[str] = []
        progress: list[tuple[int, str, int | None, int | None]] = []

        class Approval:
            chunk_ids = ["chunk-1", "chunk-2"]

            def model_copy(self, *, update):
                raise AssertionError("revision activation must not defer its first index")

        class RevisionDocument:
            supersedes_document_id = "doc-prior"

        def approve(document_id, request, auth):
            return {
                "vector_sync": {
                    "status": "indexed",
                    "record_count": 2,
                }
            }

        def index(document_id, request, auth):
            index_calls.append(document_id)
            return {"record_count": 2}

        namespace = {
            "Callable": Callable,
            "approve_review_chunks": approve,
            "index_document": index,
            "IndexRequest": object,
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<approval-plan>", "exec"),
            namespace,
        )
        result = namespace["_execute_reviewed_document_approval_plan"](
            {
                "document_id": "doc-revision",
                "document": RevisionDocument(),
                "local_auth": object(),
                "approval_requests": [Approval()],
                "edited_chunk_count": 0,
            },
            progress_callback=lambda *values: progress.append(values),
            defer_index=True,
        )

        self.assertEqual([], index_calls)
        self.assertEqual(2, result["indexed_record_count"])
        self.assertEqual((100, "승인·색인 완료", 1, 1), progress[-1])

    def test_reviewed_document_plan_can_defer_first_index_for_batch_write(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_execute_reviewed_document_approval_plan"
        )
        index_calls: list[str] = []
        progress: list[tuple[int, str, int | None, int | None]] = []
        approval_requests: list[object] = []

        class Approval:
            chunk_ids = ["chunk-1"]
            review_batch_id = "review-batch-1"

            def model_copy(self, *, update):
                for key, value in update.items():
                    setattr(self, key, value)
                return self

        def approve(document_id, request, auth):
            approval_requests.append(request)
            return {"vector_sync": {"status": "skipped", "reason": "no_prior_indexed_job"}}

        def index(document_id, request, auth):
            index_calls.append(document_id)
            return {"record_count": 1}

        namespace = {
            "Callable": Callable,
            "approve_review_chunks": approve,
            "index_document": index,
            "IndexRequest": object,
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<approval-plan>", "exec"),
            namespace,
        )
        result = namespace["_execute_reviewed_document_approval_plan"](
            {
                "document_id": "doc-new",
                "local_auth": object(),
                "approval_requests": [Approval()],
                "edited_chunk_count": 0,
            },
            progress_callback=lambda *values: progress.append(values),
            defer_index=True,
        )

        self.assertEqual([], index_calls)
        self.assertTrue(result["index_deferred"])
        self.assertEqual(0, result["indexed_record_count"])
        self.assertEqual(approval_requests[0].vector_sync_batch_id, result["vector_sync_batch_id"])
        self.assertTrue(approval_requests[0].defer_vector_sync)
        self.assertIn("review-batch-1", approval_requests[0].vector_sync_batch_id)
        self.assertEqual((100, "승인 완료·일괄 색인 대기", 1, 1), progress[-1])

    def test_reviewed_document_plan_resumes_a_durable_deferred_sync_batch(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_execute_reviewed_document_approval_plan"
        )
        index_calls: list[str] = []
        namespace = {
            "Callable": Callable,
            "approve_review_chunks": lambda *_args, **_kwargs: {},
            "index_document": lambda document_id, *_args, **_kwargs: index_calls.append(document_id),
            "IndexRequest": object,
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<approval-plan>", "exec"),
            namespace,
        )

        result = namespace["_execute_reviewed_document_approval_plan"](
            {
                "document_id": "doc-recover",
                "local_auth": object(),
                "approval_requests": [],
                "pending_vector_sync_batch_ids": ["durable-batch-1"],
                "edited_chunk_count": 0,
            },
            defer_index=True,
        )

        self.assertEqual([], index_calls)
        self.assertTrue(result["index_deferred"])
        self.assertEqual("durable-batch-1", result["vector_sync_batch_id"])

    def test_reviewed_document_plan_with_no_work_never_calls_single_document_index(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_execute_reviewed_document_approval_plan"
        )
        index_calls: list[str] = []
        progress: list[tuple[int, str, int | None, int | None]] = []
        namespace = {
            "Callable": Callable,
            "approve_review_chunks": lambda *_args, **_kwargs: {},
            "index_document": lambda document_id, *_args, **_kwargs: index_calls.append(document_id),
            "IndexRequest": object,
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<approval-plan>", "exec"),
            namespace,
        )

        result = namespace["_execute_reviewed_document_approval_plan"](
            {
                "document_id": "doc-unchanged",
                "local_auth": object(),
                "approval_requests": [],
                "pending_vector_sync_batch_ids": [],
                "edited_chunk_count": 0,
            },
            progress_callback=lambda *values: progress.append(values),
            defer_index=True,
        )

        self.assertEqual([], index_calls)
        self.assertTrue(result["index_skipped"])
        self.assertFalse(result["index_deferred"])
        self.assertEqual((100, "변경 없음·색인 생략", 0, 0), progress[-1])

    def test_batch_plan_filter_keeps_only_changed_or_durable_recovery_documents(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_approval_plan_requires_work"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<approval-plan-filter>", "exec"),
            namespace,
        )
        requires_work = namespace["_approval_plan_requires_work"]
        plans = [
            {
                "document_id": f"doc-{index:02d}",
                "approval_requests": [],
                "pending_vector_sync_batch_ids": [],
            }
            for index in range(45)
        ]
        plans[17]["approval_requests"] = [object()]

        actual_work = [plan for plan in plans if requires_work(plan)]

        self.assertEqual(["doc-17"], [plan["document_id"] for plan in actual_work])
        self.assertTrue(
            requires_work(
                {
                    "approval_requests": [],
                    "pending_vector_sync_batch_ids": ["durable-recovery-batch"],
                }
            )
        )
        self.assertFalse(
            requires_work(
                {
                    "approval_requests": [],
                    "pending_vector_sync_batch_ids": ["", "   "],
                }
            )
        )
        batch_start = source.index('batch_status = st.status("선택한 규정별 승인·색인 중…')
        batch_end = source.index("if workflow_ready_count < workflow_pending_count:", batch_start)
        batch_source = source[batch_start:batch_end]
        self.assertIn("if _approval_plan_requires_work(plan)", batch_source)
        self.assertIn("변경 없는 규정 {skipped_plan_count:,}개는 색인을 생략했습니다", batch_source)

    def test_streamlit_batch_approval_and_metadata_patch_use_batch_indexing(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count("index_documents_batch("), 2)
        self.assertIn("defer_index=True", source)
        self.assertIn('"defer_vector_sync": True', source)
        self.assertIn("vector_sync_batch_ids=deferred_batch_ids", source)
        self.assertIn("보상 색인을 완료했습니다", source)
        self.assertIn("pending_deferred_vector_sync_batch_ids", source)
        self.assertIn("색인 복구", source)
        self.assertIn("patched_document_ids", source)

    def test_all_long_operation_status_cards_leave_running_state_on_error(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_names = {
            "_brief_long_operation_error",
            "_long_operation_context_label",
            "_update_long_operation_error",
            "_long_operation_status",
        }
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]

        class FakeStatus:
            def __init__(self) -> None:
                self.updates: list[dict[str, object]] = []
                self.errors: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update(self, **kwargs) -> None:
                self.updates.append(dict(kwargs))

            def error(self, message: str) -> None:
                self.errors.append(message)

        class FakeStreamlit:
            def __init__(self) -> None:
                self.card = FakeStatus()

            def status(self, *_args, **_kwargs):
                return self.card

        fake_st = FakeStreamlit()
        namespace = {
            "Callable": Callable,
            "Iterator": Iterator,
            "contextmanager": contextmanager,
            "redact_sensitive_paths": lambda value: value,
            "st": fake_st,
        }
        exec(
            compile(ast.Module(body=helper_nodes, type_ignores=[]), "<long-operation-status>", "exec"),
            namespace,
        )

        with self.assertRaisesRegex(RuntimeError, "backend stopped"):
            with namespace["_long_operation_status"](
                "처리 중",
                failure_stage="검색 인덱스 생성",
                failure_regulation="인사규정",
                failure_policy="전체 작업을 중단하고 복구 배치를 보존합니다.",
            ):
                raise RuntimeError("backend stopped\nwith details")

        self.assertEqual("error", fake_st.card.updates[-1]["state"])
        rendered_error = fake_st.card.errors[-1]
        self.assertIn("실패 단계: 검색 인덱스 생성", rendered_error)
        self.assertIn("실패 규정: 인사규정", rendered_error)
        self.assertIn("오류: backend stopped with details", rendered_error)
        self.assertIn("전체 작업을 중단하고 복구 배치를 보존합니다", rendered_error)

        direct_status_calls = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
            and node.func.attr == "status"
        ]
        self.assertEqual(3, len(direct_status_calls))
        self.assertGreaterEqual(source.count("_long_operation_status("), 7)
        batch_start = source.index('batch_status = st.status("선택한 규정별 승인·색인 중…')
        batch_end = source.index("if workflow_ready_count < workflow_pending_count:", batch_start)
        self.assertIn("_update_long_operation_error(", source[batch_start:batch_end])
        bundle_start = source.index('bundle_status = st.status("MCP 파일 묶음 생성 중…')
        bundle_end = source.index(
            "bundle_candidates = _matching_mcp_bundle_state_candidates", bundle_start
        )
        self.assertIn('state="error"', source[bundle_start:bundle_end])

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

    def test_mcp_connection_diagnostic_reader_rejects_historical_chatgpt_local_success(self):
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
            status = create_bundle_status(
                "final",
                runtime_fingerprint="runtime-current",
                generated_at="2026-07-21T00:00:00Z",
            )
            historical = status["client_connections"]["chatgpt-desktop-local"]
            historical.update(
                {
                    "support_status": "supported",
                    "supported": True,
                    "configured": True,
                    "connected": True,
                }
            )
            historical["last_attempt"].update(
                {"id": "manual-settings-attempt", "state": "completed"}
            )
            historical["effective"].update(
                {
                    "state": "connected",
                    "attempt_id": "manual-settings-attempt",
                    "config_entry_fingerprint": config_fingerprint,
                }
            )
            historical["stages"]["registration"].update(
                {
                    "state": "verified",
                    "attempt_id": "manual-settings-attempt",
                    "verified": True,
                }
            )
            status["legacy_projection_target"] = "chatgpt-desktop-local"
            status["active_target"] = "chatgpt-desktop-local"
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
        self.assertEqual("unsupported", report["overall_state"])
        self.assertEqual("chatgpt_local_unsupported", report["reason_code"])
        self.assertFalse(report["configured"])
        self.assertFalse(report["connected"])
        self.assertNotEqual("verified", report["stages"]["registration"]["state"])

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
            missing_fingerprint, missing_error = read_diagnostic(tmp, "codex")

            status_path.write_text(
                json.dumps(
                    {
                        **legacy_success,
                        "installed_config_fingerprint": installed_fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            actual_fingerprint, actual_error = read_diagnostic(tmp, "codex")

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
        self.assertNotIn('"chatgpt-desktop-local": "ChatGPT Desktop 연결 진단"', source)
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
        self.assertIn("System.Windows.Forms.FolderBrowserDialog", source)
        self.assertIn("_default_mcp_bundle_directory()", source)

    def test_portable_folder_picker_fallback_uses_initial_path_without_interpolation(self) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_select_windows_output_directory_via_powershell"
        )
        namespace = {"Path": Path, "os": os, "subprocess": subprocess}
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<folder-picker>", "exec"),
            namespace,
        )
        completed = subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout="\ufeffC:\\사용자\\MCP",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "subprocess.run", return_value=completed
        ) as run_process:
            selected = namespace["_select_windows_output_directory_via_powershell"](
                Path(tmp)
            )

        self.assertEqual("C:\\사용자\\MCP", selected)
        args, kwargs = run_process.call_args
        self.assertEqual("powershell.exe", args[0][0])
        self.assertNotIn(str(Path(tmp).resolve()), args[0][-1])
        self.assertEqual(
            str(Path(tmp).resolve()),
            kwargs["env"]["PR_MCP_FOLDER_PICKER_INITIAL"],
        )

    def test_portable_mcp_config_uses_executable_server_mode(self) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        helper_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_direct_python_mcp_config"
        )
        namespace = {
            "json": json,
            "os": SimpleNamespace(getenv=lambda name: "C:\\Portable\\PR MCP Builder.exe" if name == "REG_RAG_PACKAGED_EXE" else None),
            "sys": SimpleNamespace(executable="C:\\Portable\\PR MCP Builder.exe", frozen=True),
            "Path": Path,
            "PROJECT_ROOT": REPO_ROOT,
            "_powershell_command": lambda command, args: " ".join([command, *args]),
        }
        exec(
            compile(ast.Module(body=[helper_node], type_ignores=[]), "<portable-mcp-config>", "exec"),
            namespace,
        )
        payload = {
            "quickstart": {
                "run_local_stdio_server": {
                    "command": "reg-rag-mcp-server",
                    "args": ["--data-dir", ".\\data"],
                },
                "copy_paste": {},
            }
        }

        result = namespace["_direct_python_mcp_config"](payload)
        server = result["quickstart"]["run_local_stdio_server"]
        self.assertEqual("C:\\Portable\\PR MCP Builder.exe", server["command"])
        self.assertEqual("--mcp-server", server["args"][0])
        self.assertNotIn("run_regulation_mcp.py", server["args"])
        self.assertIn("--flat-storage", server["args"])
        self.assertIn("--no-warm-cache", server["args"])

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
        self.assertIn("전체 규정 확인 (선택 {len(selected_document_ids):,}개)", source)
        self.assertIn("전체 규정 확인 열기 · 선택한 {len(selected_document_ids):,}개를 한꺼번에 검수·확정", source)
        # 나머지 규정의 청크는 '전체 규정 승인'을 실제로 쓸 때만 읽는다.
        self.assertIn("선택한 규정 {len(selected_document_ids):,}개 상태 불러오기", source)
        self.assertIn("bulk_review_requested and not batch_loaded", source)
        self.assertIn("bulk_review_requested and batch_loaded", source)
        # Bulk "AI 검수 완료" / "사람 확인 완료" buttons are gone — the multi-doc
        # summary auto-confirms every pending chunk instead.
        self.assertIn("AI·사람 확인은 자동으로 완료 표시됩니다", source)
        # 초보자 모드에서도 전체 규정 승인을 쓸 수 있되, 확인란을 거쳐야 버튼이 열린다.
        self.assertIn("규정 {len(selected_document_ids):,}개를 한 번에 승인·색인하는 것에 동의합니다.", source)
        self.assertIn(
            "beginner_bulk_review_disabled = beginner_bulk_mode_active and not beginner_bulk_confirmed",
            source,
        )
        self.assertGreaterEqual(source.count("beginner_bulk_review_disabled"), 2)
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
        # 승인·색인되는 본문은 언제나 가운데 전처리본 칸이다. AI는 오른쪽에서
        # 볼 곳을 짚어 줄 뿐 본문을 쓰지 않으므로 최종본 배지가 옮겨 다니지 않는다.
        self.assertIn('header_cols[1].markdown("**전처리본 · ✅ 최종본**")', source)
        self.assertIn('header_cols[2].markdown("**AI 검수 의견**")', source)
        self.assertIn("_render_agent_review_findings(", source)
        self.assertIn("AI는 어디를 봐야 하는지 짚어 줄 뿐 본문을 고치지 않습니다.", source)
        self.assertIn("✅ 최종본 칸의 내용이 승인·색인되어 MCP에 들어갑니다.", source)
        self.assertIn("_approval_auto_confirm_pending_chunks(", source)
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

        # 파서 초안 → (선택) AI 추가 검수 → 사람 승인 3단계 진행 띠가 정의되고 각 페이지에서 렌더링돼야 한다.
        self.assertIn("PIPELINE_STAGES", source)
        self.assertIn("파서 초안", source)
        self.assertIn("(선택) AI 추가 검수", source)
        self.assertIn("사람 승인", source)
        self.assertIn("def _render_pipeline_stages", source)
        self.assertIn("_render_pipeline_stages(PIPELINE_STAGE_PARSER)", source)
        self.assertIn("_render_pipeline_stages(PIPELINE_STAGE_AI_REVIEW)", source)
        self.assertIn("_render_pipeline_stages(PIPELINE_STAGE_HUMAN_APPROVAL)", source)

        # 색은 지금 서 있는 칸 하나에만 준다. 세 칸이 모두 칠해지면 색으로는 현재 단계를 못 읽는다.
        self.assertIn('state, badge = "active", "▶ 지금 단계"', source)
        self.assertIn('state, badge = "done", "✓ 완료"', source)
        self.assertIn('state, badge = "upcoming", "예정"', source)
        self.assertIn(".rr-stage.active {", source)
        self.assertIn(".rr-stage.upcoming {", source)

        # AI 검수 결과가 숨은 비용 익스팬더가 아니라 결과 화면의 정식 패널로 노출돼야 한다.
        self.assertIn("AI 검수 결과", source)
        self.assertIn("def _ai_review_status_text", source)
        self.assertIn("AI가 살펴본 후보", source)
        self.assertIn("AI가 검토 대상으로 고른 청크", source)
        self.assertIn("사람이 꼭 볼 청크", source)
        # 기술 상세(비용 가드)는 유지하되 전산 담당자용으로 접어 둔다.
        self.assertIn("AI review API and cost guard", source)

    def test_results_hides_ai_metrics_when_ai_review_was_not_requested(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        # AI를 켜지 않았는데 후보 0 / 선정 0 지표를 보여주면 "AI가 보고 아무것도 못 찾았다"로 읽힌다.
        self.assertIn("def _agent_review_requested", source)
        self.assertIn("ai_review_requested = _agent_review_requested(agent_review_summary)", source)
        self.assertIn("elif not ai_review_requested:", source)
        self.assertIn("agent_review_not_requested", source)
        # AI를 켰을 때는 왜 사람이 볼 조항이 남는지 범위·한도를 함께 설명한다.
        self.assertIn("def _ai_review_scope_caption", source)
        self.assertIn("st.caption(_ai_review_scope_caption(agent_review_summary))", source)

    def test_preprocess_offers_bulk_pending_selection_and_filename_based_naming(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        # 대기 규정을 한 번에 골라 일괄 전처리할 수 있어야 한다.
        self.assertIn('key="pending-upload-select-all"', source)
        self.assertIn('key="pending-upload-clear-all"', source)
        self.assertIn("전체 규정 선택", source)
        # 규정 이름은 올린 파일 이름을 기본으로 쓰고, 파일마다 자기 이름을 적용한다.
        self.assertIn("PREPROCESS_DOCUMENT_NAME_MODE_KEY", source)
        self.assertIn('"올린 파일 이름 그대로 사용"', source)
        self.assertIn(
            'if document_name_mode == "filename" and not file_upload_metadata.get("document_name"):',
            source,
        )
        self.assertIn('file_upload_metadata["document_name"] = Path(filename).stem', source)

    def test_preprocess_ai_review_is_opt_in_while_official_gates_remain(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        # 켜고 끄는 곳은 사이드바 하나뿐이고, ① 화면은 그 상태를 그대로 따른다.
        self.assertNotIn('key="preprocess-enable-agent-review"', source)
        self.assertIn('"AI 검수 사용"', source)
        self.assertIn(
            "ai_review_requested = bool(settings.enable_agent_review)"
            " and not _ai_review_setup_blocker(settings)",
            source,
        )
        self.assertIn("enable_agent_review=ai_review_requested", source)
        self.assertNotIn("enable_agent_review=True", source)
        self.assertIn("휴먼리뷰 후 공식 RAG/MCP 사용", source)
        self.assertIn("공식 승인·보안 확인은 그대로 진행됩니다.", source)
        self.assertIn("사람 승인과 보안 게이트를 대신하지 않습니다.", source)

    def test_mcp_export_failure_guidance_is_not_nested_in_folder_open_error(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        dialog_start = source.index("def _render_operator_project_dialog")
        dialog_end = source.index("\ndef ", dialog_start + 1)
        folder_dialog_source = source[dialog_start:dialog_end]
        self.assertNotIn("MCP runtime export would be incomplete", folder_dialog_source)

        mcp_page_start = source.index("def _page_connect")
        export_reason_index = source.index("MCP runtime export would be incomplete", mcp_page_start)
        export_exception_start = source.rfind("except Exception as exc:", mcp_page_start, export_reason_index)
        self.assertGreater(export_exception_start, mcp_page_start)
        self.assertIn(
            "③ 검수하고 승인에서 남은 항목을 처리하고 색인한 뒤 'MCP로 쓸 파일 묶음 만들기'를 다시 누르세요.",
            source[
                export_exception_start : source.index(
                    "bundle_candidates = _matching_mcp_bundle_state_candidates",
                    export_exception_start,
                )
            ],
        )
        export_exception_source = source[
            export_exception_start : source.index(
                "bundle_candidates = _matching_mcp_bundle_state_candidates",
                export_exception_start,
            )
        ]
        self.assertIn("beginner_error_message", export_exception_source)
        self.assertIn(
            "검토가 끝나지 않은 조문이 있어 MCP 파일 묶음을 만들지 않았습니다.",
            export_exception_source,
        )
        self.assertNotIn("st.error(str(exc))", export_exception_source)

    def test_kordoc_guidance_keeps_fast_preprocess_separate_from_official_mcp_gate(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("Kordoc 없이도 일반 조문·항·호의 빠른 구조 전처리는 할 수 있습니다.", source)
        self.assertIn("PDF·HWP·HWPX·DOCX를 공식 MCP로 만들려면 Kordoc을 준비하세요", source)
        self.assertIn("공식 MCP 파일 묶음에는 PDF·HWP·HWPX·DOCX 네 형식 모두 Kordoc 표 파싱 품질 증거가 필요합니다.", source)
        self.assertIn("미설치 상태에서 처리한 문서는 나중에 Kordoc 설치 후 새 초안으로", source)
        self.assertIn("다시 전처리·검수·승인해야 합니다.", source)
        self.assertNotIn("HWP/HWPX의 표·별표가 중요한 문서", source)

    def test_single_chunk_rejection_reuses_guarded_backend_action(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("RejectRequest", source)
        self.assertIn("reject_review_chunks", source)
        self.assertIn("_chunk_rejection_ready", source)
        self.assertIn("반려 사유 (필수)", source)
        self.assertIn("선택한 조항만 반려하여 MCP에서 제외", source)
        self.assertIn("chunk_ids=list(reject_targets)", source)
        self.assertIn("disabled=not rejection_ready", source)
        self.assertIn("최종 제외(terminal exclusion)", source)
        self.assertIn("_invalidate_document_context_cache(document_id)", source)

    def test_connection_handoff_uses_direct_stdio_and_vercel_http(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertNotIn("비개발자는 이 파일만 더블클릭하면 됩니다", source)
        self.assertNotIn("전용 연결 버튼을 실행하면 됩니다", source)
        self.assertNotIn('mcp_mode == "tunnel"', source)
        self.assertNotIn('files.get("openai_tunnel")', source)
        self.assertIn('"codex": "Codex CLI / Codex IDE"', source)
        self.assertIn(
            "ChatGPT는 로컬 STDIO에 직접 연결하지 않으므로 원격 HTTPS 대상을 선택",
            source,
        )
        self.assertIn("ChatGPT · Vercel HTTPS MCP", source)
        self.assertIn("Claude · Vercel HTTPS MCP", source)
        self.assertIn("배포된 Vercel HTTPS `/mcp` 주소 (첫 배포 전에는 비워도 됨)", source)
        self.assertIn("배포 준비용 MCP 묶음", source)
        self.assertIn("아직 없음 — 배포 준비 묶음부터 생성하세요", source)
        self.assertIn('mcp_http_url.startswith("https://")', source)
        self.assertIn("Settings > Apps > Advanced settings > Developer mode", source)
        self.assertIn("Secure MCP Tunnel", source)
        self.assertIn("다른 Windows PC에서 로컬 STDIO로 실행하려면 대상 PC에", source)
        self.assertIn("Python 3.11 이상을 설치해야 합니다", source)
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
        self.assertNotIn('if mcp_mode == "local":\n                    connection_display_value = json.dumps', source)

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
            "def _read_claude_desktop_registration(",
            guide_start,
        )
        guide_source = source[guide_start:guide_end]

        for expected in (
            "로컬 STDIO MCP 서버에 직접 연결하는 공개 지원 경로가 아닙니다",
            "이전 번들의 로컬 ChatGPT 설정값은 사용하지 마세요",
            "ChatGPT · Vercel HTTPS MCP",
            "OpenAI Secure MCP Tunnel",
            "OpenAI의 ChatGPT MCP 지원 범위 확인",
        ):
            self.assertIn(expected, guide_source)
        self.assertNotIn("Command 복사", guide_source)
        self.assertNotIn("+ 서버 추가 > STDIO", guide_source)
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

            def link_button(self, label: str, url: str) -> None:
                self.events.append(("link_button", f"{label} {url}"))

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

        self.assertFalse([value for event, value in fake_st.events if event == "code"])
        rendered_text = "\n".join(value for _, value in fake_st.events)
        for expected in (
            "로컬 STDIO MCP 서버에 직접 연결하는 공개 지원 경로가 아닙니다",
            "이전 번들의 로컬 ChatGPT 설정값은 사용하지 마세요",
            "ChatGPT · Vercel HTTPS MCP",
            "OpenAI Secure MCP Tunnel",
            "help.openai.com",
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
            "Codex CLI / Codex IDE에 등록하는 방법",
            "이전 번들의 로컬 ChatGPT 설정값은 사용하지 마세요",
            "diagnostic_target = installed_target",
        ):
            self.assertIn(expected, source)
        self.assertNotIn("method_b_destination", source)
        self.assertNotIn("ChatGPT Desktop — 인자를", source)
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
            "방법 B · Codex CLI / Codex IDE 로컬 STDIO",
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
            "sys": SimpleNamespace(frozen=False),
            "os": SimpleNamespace(getenv=lambda _name: ""),
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

            namespace["sys"].frozen = True
            recorder.values.clear()
            render(
                target="claude-desktop",
                server_name="portable-demo",
                bundle_dir=str(bundle_dir),
                runtime_data_dir=str(runtime_dir),
                connection_display_value="{}",
            )
            packaged_output = "\n".join(recorder.values)
            self.assertIn("PR MCP Builder.exe --mcp-server", packaged_output)
            self.assertIn("Python을 따로 설치할 필요가 없습니다", packaged_output)
            self.assertNotIn("doctor_mcp_connection.ps1", packaged_output)
            self.assertNotIn("validate_mcp_smoke.ps1", packaged_output)
            self.assertNotIn("connect_mcp_client.ps1", packaged_output)

            namespace["sys"].frozen = False
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

    def _exec_streamlit_functions(self, names: tuple[str, ...], extra_namespace: dict) -> dict:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        self.assertEqual(len(names), len(nodes))
        namespace = dict(extra_namespace)
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), "<streamlit-helpers>", "exec"),
            namespace,
        )
        return namespace

    def test_only_the_current_pipeline_stage_is_coloured(self):
        """세 칸이 다 칠해져 있으면 색으로는 지금 단계를 읽을 수 없다."""
        rendered: list[str] = []

        class Recorder:
            def __getattr__(self, _name):
                def record(*args, **_kwargs):
                    rendered.extend(str(value) for value in args)

                return record

        namespace = self._exec_streamlit_functions(
            ("_render_pipeline_stages",),
            {
                "st": Recorder(),
                "PIPELINE_STAGES": [
                    ("파서 초안", "1차 정리"),
                    ("(선택) AI 추가 검수", "AI 검토 초안"),
                    ("사람 승인", "최종 확인"),
                ],
            },
        )
        render = namespace["_render_pipeline_stages"]

        render(2)
        strip = "\n".join(rendered)

        self.assertEqual(1, strip.count('class="rr-stage active"'))
        self.assertEqual(1, strip.count('class="rr-stage done"'))
        self.assertEqual(1, strip.count('class="rr-stage upcoming"'))
        self.assertIn("1단계 · ✓ 완료", strip)
        self.assertIn("2단계 · ▶ 지금 단계", strip)
        self.assertIn("3단계 · 예정", strip)

        rendered.clear()
        render(3)
        last_strip = "\n".join(rendered)
        self.assertEqual(1, last_strip.count('class="rr-stage active"'))
        self.assertEqual(2, last_strip.count('class="rr-stage done"'))
        self.assertNotIn("upcoming", last_strip)

        # 흐름 설명용 호출(현재 단계 없음)은 어떤 칸도 현재로 표시하지 않는다.
        rendered.clear()
        render(0)
        preview_strip = "\n".join(rendered)
        self.assertNotIn("rr-stage active", preview_strip)
        self.assertNotIn("지금 단계", preview_strip)

    def test_results_step_can_be_left_without_opening_a_regulation(self):
        """규정을 하나 눌러야만 다음 단계로 갈 수 있는 것은 확인이 아니라 통행세다."""
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        results_start = source.index("def _page_results(")
        results_source = source[results_start : source.index("\ndef ", results_start + 10)]
        self.assertIn(
            "_render_workflow_directory_open_prompt(document_id, blocking=False)",
            results_source,
        )
        self.assertIn("_render_results_step_exit_without_open(selected_document_ids)", results_source)

        next_button_calls: list[dict] = []

        class Recorder:
            def __getattr__(self, _name):
                def record(*args, **_kwargs):
                    return None

                return record

        namespace = self._exec_streamlit_functions(
            ("_render_results_step_exit_without_open",),
            {
                "st": Recorder(),
                "NAV_APPROVAL": "③ 검수하고 승인",
                "_render_workflow_next_button": lambda label, target, **kwargs: next_button_calls.append(
                    {"label": label, "target": target, **kwargs}
                ),
            },
        )
        render = namespace["_render_results_step_exit_without_open"]

        render(["doc-a", "doc-b"])

        self.assertEqual(1, len(next_button_calls))
        self.assertEqual("③ 검수하고 승인", next_button_calls[0]["target"])
        self.assertFalse(next_button_calls[0]["disabled"])
        self.assertIn("2개 규정", next_button_calls[0]["label"])

        # 선택한 규정이 하나도 없으면 넘길 것이 없으므로 그때만 막는다.
        next_button_calls.clear()
        render([])
        self.assertTrue(next_button_calls[0]["disabled"])

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
        self.assertIn("status_box.update(", helper_source)
        self.assertNotIn("status_box.write(", helper_source)
        self.assertIn("min(measured_percent, 99) if thread.is_alive()", helper_source)
        self.assertIn("thread.join(timeout=0.5)", helper_source)
        self.assertNotIn("time.sleep(0.5)", helper_source)
        self.assertNotIn("estimated_percent", helper_source)
        self.assertNotIn("elapsed_fraction", helper_source)

        bundle_start = source.index('"MCP로 쓸 파일 묶음 만들기"')
        bundle_end = source.index(
            "bundle_candidates = _matching_mcp_bundle_state_candidates", bundle_start
        )
        bundle_source = source[bundle_start:bundle_end]
        self.assertIn('start_percent=35', bundle_source)
        self.assertIn('end_percent=78', bundle_source)
        self.assertIn('label="MCP 데이터·검색 인덱스 생성"', bundle_source)
        self.assertIn("④ MCP 생성·업데이트 한 번으로 선택 범위의 규정 목록·목차·조문 계층 색인", source)
        self.assertIn("개별 규정 파일 여러 개와 통합 규정집 모두 동일하게 처리됩니다.", source)
        self.assertIn("목차 노드 {runtime_data.get('toc_node_count', 0)}개와 조문 계층 색인을 자동 생성했습니다.", source)
        self.assertIn("current_bundle_regulation", bundle_source)
        self.assertIn('bundle_progress.progress(100, text="MCP 파일 묶음 생성 완료 · 100%")', bundle_source)
        self.assertIn('state="error"', bundle_source)
        self.assertIn("전체 작업을 중단했습니다", bundle_source)
        self.assertIn("아직 검수·승인 또는 반려가 끝나지 않은 청크가 있어 MCP 생성을 중단했습니다.", source)
        self.assertNotIn("time.sleep(0.12)", bundle_source)

        self.assertIn("progress_callback=report", source)
        self.assertIn('progress_callback(50, "검색 인덱스 생성", 0, 1)', source)

    def test_streamlit_does_not_offer_inactive_chunk_mode_choices(self):
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn('chunk_mode = "article"', source)
        self.assertIn("청크 방식: 규정의 조문·항목 구조에 맞춰 자동 적용", source)
        self.assertNotIn('"paragraph": "문단 중심"', source)
        self.assertNotIn('"hybrid": "혼합"', source)


if __name__ == "__main__":
    unittest.main()
