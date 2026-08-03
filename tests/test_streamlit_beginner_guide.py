from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

try:
    from streamlit.testing.v1 import AppTest
except Exception:  # pragma: no cover - optional in minimal environments
    AppTest = None

from app.core.config import Settings
from app.core.institution_profiles import (
    InstitutionProfile,
    InstitutionProfileRegistry,
    institution_profile_registry_to_bytes,
)
from app.services.approval_governance import approval_review_completion_state
from scripts.generate_mcp_client_config import (
    RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES,
    validate_mcp_runtime_data_bundle_integrity,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "frontend" / "streamlit_app.py"


def _source_and_module() -> tuple[str, ast.Module]:
    source = APP_PATH.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _function(module: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _write_sealed_runtime_fixture(
    runtime_data_dir: Path,
    *,
    document_ids: list[str],
) -> dict[str, Path]:
    """Write the smallest approved runtime bundle accepted by the real validator."""

    repository_dir = runtime_data_dir / "repository"
    journal_dir = repository_dir / "journals"
    vector_dir = runtime_data_dir / "vector_db" / "default"
    journal_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    repository_manifest_path = repository_dir / "manifest.json"
    approval_journal_path = journal_dir / "approvals.jsonl"
    approval_snapshot_path = repository_dir / "approval_snapshot.json"
    omission_disposition_path = (
        repository_dir / "omission_disposition_snapshot.json"
    )
    vector_path = vector_dir / "approved_vectors.jsonl"
    result_path = repository_dir / f"{document_ids[0]}_chunks.json"
    runtime_manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"

    repository_manifest_path.write_text(
        json.dumps(
            {
                "documents": {
                    document_id: {"document_id": document_id}
                    for document_id in document_ids
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    approval_journal_path.write_text(
        "".join(
            json.dumps(
                {
                    "approval_id": f"approval-{document_id}",
                    "tenant_id": "default",
                    "document_id": document_id,
                    "approved_at": "2026-08-03T00:00:00+00:00",
                    "chunk_ids": [f"{document_id}:chunk-1"],
                    "approved_content_hashes": {
                        f"{document_id}:chunk-1": f"sha256:{document_id}"
                    },
                },
                sort_keys=True,
            )
            + "\n"
            for document_id in document_ids
        ),
        encoding="utf-8",
    )
    approval_snapshot_path.write_text(
        json.dumps(
            {
                "document_ids": document_ids,
                "entries": [
                    {"document_id": document_id}
                    for document_id in document_ids
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    disposition_entries = [
        {
            "tenant_id": "default",
            "document_id": document_id,
            "chunk_id": f"{document_id}:chunk-1",
            "content_hash": f"sha256:{document_id}",
            "latest_decision_id": f"approval-{document_id}",
            "latest_decision_status": "approved",
            "latest_decision_at": "2026-08-03T00:00:00+00:00",
            "disposition": "exported",
            "exported": True,
            "requested": True,
        }
        for document_id in sorted(document_ids)
    ]
    omission_disposition_path.write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_omission_disposition_snapshot",
                "schema_version": "mcp-runtime-omission-disposition-snapshot-v1",
                "tenant_id": "default",
                "requested_document_ids": sorted(document_ids),
                "exported_document_ids": sorted(document_ids),
                "omitted_document_ids": [],
                "requested_document_count": len(document_ids),
                "exported_document_count": len(document_ids),
                "omitted_document_count": 0,
                "requested_chunk_ids": [
                    entry["chunk_id"] for entry in disposition_entries
                ],
                "exported_chunk_ids": [
                    entry["chunk_id"] for entry in disposition_entries
                ],
                "omitted_chunk_ids": [],
                "requested_chunk_count": len(disposition_entries),
                "exported_chunk_count": len(disposition_entries),
                "omitted_chunk_count": 0,
                "disposition_counts": {
                    "exported": len(disposition_entries),
                    "omitted_rejected": 0,
                    "omitted_superseded": 0,
                },
                "entry_count": len(disposition_entries),
                "entries": disposition_entries,
                "generated_at": "2026-08-03T00:00:00+00:00",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    vector_path.write_text(
        "".join(
            json.dumps(
                {
                    "id": f"{document_id}:chunk-1",
                    "document_id": document_id,
                    "chunk_id": f"{document_id}:chunk-1",
                    "metadata": {
                        "document_id": document_id,
                        "chunk_id": f"{document_id}:chunk-1",
                        "approval_id": f"approval-{document_id}",
                        "approved_content_hash": f"sha256:{document_id}",
                    },
                },
                sort_keys=True,
            )
            + "\n"
            for document_id in document_ids
        ),
        encoding="utf-8",
    )
    result_path.write_text("[]\n", encoding="utf-8")

    file_sha256 = {
        path.relative_to(runtime_data_dir).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(runtime_data_dir.rglob("*"))
        if path.is_file() and path != runtime_manifest_path
    }
    runtime_manifest_payload = {
        "report_type": "mcp_runtime_data_bundle",
        "synthetic_runtime": False,
        "provenance": "approved_runtime_bundle_export",
        "tenant_id": "default",
        "document_id": document_ids[0],
        "document_ids": document_ids,
        "record_count": len(document_ids),
        "approval_record_count": len(document_ids),
        "files": {
            "vector_jsonl": str(vector_path),
            "repository_manifest": str(repository_manifest_path),
            "approval_journal": str(approval_journal_path),
            "approval_snapshot": str(approval_snapshot_path),
            "omission_disposition_snapshot": str(omission_disposition_path),
            "result_files": [str(result_path)],
            "runtime_manifest": str(runtime_manifest_path),
        },
        "runtime_data_reuse": {
            "schema_version": "mcp-runtime-data-reuse-v1",
            "file_sha256": file_sha256,
        },
    }
    manifest_digest_payload = json.dumps(
        runtime_manifest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    runtime_manifest_payload["runtime_data_reuse"]["manifest_sha256"] = (
        hashlib.sha256(manifest_digest_payload).hexdigest()
    )
    runtime_manifest_path.write_text(
        json.dumps(runtime_manifest_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "vector": vector_path,
        "repository_manifest": repository_manifest_path,
        "approval_journal": approval_journal_path,
        "approval_snapshot": approval_snapshot_path,
        "omission_disposition_snapshot": omission_disposition_path,
        "result": result_path,
        "runtime_manifest": runtime_manifest_path,
    }


def _reseal_runtime_fixture(runtime_data_dir: Path) -> None:
    runtime_manifest_path = runtime_data_dir / "mcp_runtime_manifest.json"
    runtime_manifest_payload = json.loads(
        runtime_manifest_path.read_text(encoding="utf-8")
    )
    runtime_manifest_payload["runtime_data_reuse"]["file_sha256"] = {
        path.relative_to(runtime_data_dir).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(runtime_data_dir.rglob("*"))
        if path.is_file() and path != runtime_manifest_path
    }
    runtime_manifest_payload["runtime_data_reuse"].pop("manifest_sha256", None)
    manifest_digest_payload = json.dumps(
        runtime_manifest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    runtime_manifest_payload["runtime_data_reuse"]["manifest_sha256"] = (
        hashlib.sha256(manifest_digest_payload).hexdigest()
    )
    runtime_manifest_path.write_text(
        json.dumps(runtime_manifest_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class StreamlitBeginnerGuideTests(unittest.TestCase):
    def test_new_user_guide_continues_to_preprocess_after_institution_creation(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            app = AppTest.from_file(str(APP_PATH), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.run()

            next(
                button
                for button in app.button
                if button.label == "초보자 안내 시작"
            ).click().run()
            institution_marker = "\n".join(
                str(markdown.value) for markdown in app.markdown
            )
            self.assertIn("먼저 작업할 기관을 등록하세요", institution_marker)

            next(
                item for item in app.text_input if item.label == "기관명"
            ).input("초보자 테스트 기관")
            next(
                button for button in app.button if button.label == "기관 생성"
            ).click().run()
            preprocess_marker = "\n".join(
                str(markdown.value) for markdown in app.markdown
            )

            self.assertEqual("① 문서 올려서 전처리", app.session_state["nav_page"])
            self.assertTrue(app.session_state["beginner_guide_enabled"])
            self.assertIn("먼저 규정 파일을 선택하세요", preprocess_marker)
            self.assertNotIn("document_id", app.session_state)

        self.assertFalse(app.exception)

    def test_app_can_start_and_skip_beginner_mode_without_running_workflow_actions(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            registry = InstitutionProfileRegistry(
                profiles={
                    "test-profile": InstitutionProfile(
                        profile_id="test-profile",
                        display_name="테스트 기관",
                        institution_name="테스트 기관",
                        tenant_id="default",
                    )
                },
                default_profile_id="test-profile",
            )
            app = AppTest.from_file(str(APP_PATH), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.session_state["institution_profile_registry_bytes"] = (
                institution_profile_registry_to_bytes(registry)
            )
            app.session_state["selected_institution_profile_id"] = "test-profile"
            app.run()

            next(button for button in app.button if button.label == "초보자 안내 시작").click().run()
            marker_text = "\n".join(str(markdown.value) for markdown in app.markdown)
            self.assertTrue(app.session_state["beginner_guide_enabled"])
            self.assertIn("먼저 규정 파일을 선택하세요", marker_text)
            self.assertNotIn("document_id", app.session_state)

            next(button for button in app.button if button.label == "안내 건너뛰기").click().run()
            self.assertFalse(app.session_state["beginner_guide_enabled"])
            self.assertNotIn("document_id", app.session_state)

        self.assertFalse(app.exception)

    def test_sidebar_toggle_counts_as_an_explicit_guide_choice(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            registry = InstitutionProfileRegistry(
                profiles={
                    "test-profile": InstitutionProfile(
                        profile_id="test-profile",
                        display_name="테스트 기관",
                        institution_name="테스트 기관",
                        tenant_id="default",
                    )
                },
                default_profile_id="test-profile",
            )
            app = AppTest.from_file(str(APP_PATH), default_timeout=20)
            app.session_state["ai_connection_overrides"] = {
                "data_dir": settings.data_dir,
                "artifact_root": settings.artifact_root,
            }
            app.session_state["institution_profile_registry_bytes"] = (
                institution_profile_registry_to_bytes(registry)
            )
            app.session_state["selected_institution_profile_id"] = "test-profile"
            app.run()

            next(
                button for button in app.button if button.label == "일반 모드로 계속"
            ).click().run()
            next(
                item for item in app.toggle if item.label == "초보자 안내 모드"
            ).set_value(True).run()
            page_text = "\n".join(
                str(markdown.value) for markdown in app.markdown
            )

            self.assertTrue(app.session_state["beginner_guide_choice_made"])
            self.assertTrue(app.session_state["beginner_guide_enabled"])
            self.assertNotIn("화면 안내 방식을 선택하세요", page_text)

        self.assertFalse(app.exception)

    def test_guide_exposes_explicit_reversible_mode_controls(self) -> None:
        source, _module = _source_and_module()

        for label in (
            "초보자 안내 시작",
            "일반 모드로 계속",
            "초보자 안내 모드",
            "이전 단계",
            "다음 단계",
            "안내 건너뛰기",
            "처음부터 다시 보기",
            "선택한 AI 앱에 MCP 등록을 완료했습니다.",
            "AI 앱을 완전히 다시 시작했거나 새 대화를 열었습니다.",
            "MCP 연결 상태 새로고침 결과를 확인했습니다.",
            "list_regulations로 규정 목록이 보이는 것을 확인했습니다.",
            "search로 관련 조문이 검색되는 것을 확인했습니다.",
            "fetch로 조문 원문과 출처가 조회되는 것을 확인했습니다.",
        ):
            self.assertIn(label, source)
        self.assertIn('role="note"', source)
        self.assertIn("rr-beginner-marker-number", source)
        self.assertIn("border: 3px solid #c62828", source)
        self.assertIn('[data-testid="stCheckbox"]', source)
        self.assertIn('[data-testid="stTextInput"]', source)
        self.assertNotIn('control_key_prefix="ai-"', source)
        self.assertIn("_approval_ai_decision_control_keys(pending_ai_item_id)", source)
        self.assertIn('div[class~="st-key-{safe_control_key}"] button', source)
        self.assertIn("control_key_prefix=human_confirmed_widget_key", source)
        self.assertNotIn("'2. 사람 검증 확인' 탭을 누르세요", source)
        self.assertIn("사람 검증 결과를 직접 확인하세요", source)
        self.assertIn("청크·이슈를 확인했습니다", source)
        self.assertIn("먼저 MCP 이름을 입력하세요", source)
        self.assertIn("'정리된 내용(청크)'와 '이슈' 탭을 확인하세요", source)
        self.assertIn('control_key_prefix="institution-name"', source)
        self.assertIn('control_key_prefix=mcp_connection_target_key', source)
        self.assertIn('index=None if beginner_target_choice_required else 0', source)
        self.assertIn('[data-testid="stRadio"]', source)
        self.assertIn('[data-testid="stLinkButton"]', source)
        self.assertIn("승인·색인을 마쳤다면 MCP 생성으로 이동하세요", source)
        self.assertEqual(2, source.count("_render_beginner_connection_confirmation("))
        registration_course = source.index("_render_mcp_completion_connection_course(", 8000)
        final_confirmation = source.index(
            "_render_beginner_connection_confirmation(", registration_course
        )
        self.assertLess(registration_course, final_confirmation)

    def test_marker_css_fragment_matches_streamlit_widget_key_replacement(self) -> None:
        _source, module = _source_and_module()
        helper = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_streamlit_key_css_fragment"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                "<streamlit-key-css-fragment>",
                "exec",
            ),
            namespace,
        )

        css_fragment = namespace["_streamlit_key_css_fragment"]
        self.assertEqual(
            "approval-doc-1-chunk-1-human_confirmed_widget",
            css_fragment("approval:doc-1:chunk-1:human_confirmed_widget"),
        )

    def test_current_ai_review_item_uses_only_its_exact_decision_keys(self) -> None:
        source, module = _source_and_module()
        helper = _function(module, "_approval_ai_decision_control_keys")
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                "<approval-ai-control-keys>",
                "exec",
            ),
            namespace,
        )

        control_keys = namespace["_approval_ai_decision_control_keys"]
        self.assertEqual(
            ("ai-reflect-chunk-1:warning:1", "ai-skip-chunk-1:warning:1"),
            control_keys("chunk-1:warning:1"),
        )
        self.assertNotIn('control_key_prefix="ai-"', source)
        self.assertIn("key=reflect_button_key", source)
        self.assertIn("key=skip_button_key", source)
        self.assertIn('div[class~="st-key-{safe_control_key}"] button', source)

    def test_beginner_multi_chunk_review_moves_then_unlocks_single_approval(self) -> None:
        source, module = _source_and_module()
        helper = _function(module, "_beginner_pending_review_navigation")
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                "<beginner-review-navigation>",
                "exec",
            ),
            namespace,
        )
        navigation = namespace["_beginner_pending_review_navigation"]
        entries = [
            {"chunk_id": "chunk-1", "state": {"approve_enabled": True}},
            {"chunk_id": "chunk-2", "state": {"approve_enabled": False}},
            {"chunk_id": "chunk-3", "state": {"approve_enabled": False}},
        ]

        first_complete = navigation(
            entries,
            current_chunk_id="chunk-1",
            enabled=True,
        )
        self.assertEqual(1, first_complete["reviewed_count"])
        self.assertEqual(3, first_complete["total_count"])
        self.assertEqual("chunk-2", first_complete["next_chunk_id"])
        self.assertFalse(first_complete["all_reviewed"])

        current_incomplete = navigation(
            entries,
            current_chunk_id="chunk-2",
            enabled=True,
        )
        self.assertEqual("", current_incomplete["next_chunk_id"])

        all_complete = navigation(
            [
                {"chunk_id": entry["chunk_id"], "state": {"approve_enabled": True}}
                for entry in entries
            ],
            current_chunk_id="chunk-3",
            enabled=True,
        )
        self.assertTrue(all_complete["all_reviewed"])
        self.assertEqual("", all_complete["next_chunk_id"])

        general_mode = navigation(
            entries,
            current_chunk_id="chunk-1",
            enabled=False,
        )
        self.assertEqual("", general_mode["next_chunk_id"])

        page = _function(module, "_page_approval")
        next_button_if = next(
            node
            for node in ast.walk(page)
            if isinstance(node, ast.If)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "button"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "다음 미검수 청크"
                for call in ast.walk(node.test)
            )
        )
        next_button_calls = {
            call.func.id
            for statement in next_button_if.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        self.assertNotIn("approve_review_chunks", next_button_calls)
        self.assertNotIn("index_document", next_button_calls)
        self.assertIn("검수 완료 {reviewed_count}/{pending_count}", source)
        self.assertIn(
            "if not beginner_review_navigation[\"enabled\"]\n        or beginner_all_pending_reviewed",
            source,
        )

        approval_if = next(
            node
            for node in ast.walk(page)
            if isinstance(node, ast.If)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "button"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "승인하고 색인"
                for call in ast.walk(node.test)
            )
        )
        direct_index_calls = [
            call
            for statement in approval_if.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "index_document"
        ]
        self.assertEqual(1, len(direct_index_calls))

        approval_target_fallback = next(
            node
            for node in ast.walk(page)
            if isinstance(node, ast.If)
            and any(
                isinstance(descendant, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "approval_target_entries"
                    for target in descendant.targets
                )
                for statement in node.body
                for descendant in ast.walk(statement)
            )
            and "compare_chunk_approvable" in ast.unparse(node.test)
        )
        fallback_condition = ast.unparse(approval_target_fallback.test)
        self.assertIn(
            "not beginner_review_navigation['enabled'] or beginner_all_pending_reviewed",
            fallback_condition,
        )

        can_approve_assignment = next(
            node
            for node in ast.walk(page)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "can_approve"
                for target in node.targets
            )
        )
        self.assertIn(
            "not beginner_review_navigation['enabled'] or beginner_all_pending_reviewed",
            ast.unparse(can_approve_assignment.value),
        )
        approval_button_call = next(
            call
            for call in ast.walk(approval_if.test)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "button"
        )
        disabled_value = next(
            keyword.value
            for keyword in approval_button_call.keywords
            if keyword.arg == "disabled"
        )
        self.assertEqual(
            "not can_approve or approved_count >= total_chunks",
            ast.unparse(disabled_value),
        )

    def test_connection_confirmation_requires_each_external_step_in_order(self) -> None:
        _source, module = _source_and_module()
        helper = _function(module, "_render_beginner_connection_confirmation")

        confirmation_key_names = {
            node.targets[0].id
            for node in ast.walk(helper)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_beginner_guide_connection_confirmed_key"
        }
        marker_calls = [
            node
            for node in ast.walk(helper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_render_beginner_action_marker"
        ]
        checkbox_calls = [
            node
            for node in ast.walk(helper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
            and node.func.attr == "checkbox"
        ]

        self.assertEqual(1, len(confirmation_key_names))
        self.assertEqual(1, len(marker_calls))
        self.assertEqual(1, len(checkbox_calls))
        marker_prefix = next(
            keyword.value
            for keyword in marker_calls[0].keywords
            if keyword.arg == "control_key_prefix"
        )
        checkbox_key = next(
            keyword.value
            for keyword in checkbox_calls[0].keywords
            if keyword.arg == "key"
        )
        self.assertIsInstance(marker_prefix, ast.Subscript)
        self.assertIsInstance(checkbox_key, ast.Name)
        self.assertEqual("item_keys", ast.unparse(marker_prefix.value))
        self.assertEqual("item_key", checkbox_key.id)
        self.assertLess(marker_calls[0].lineno, checkbox_calls[0].lineno)
        helper_source = ast.unparse(helper)
        for item in (
            "registered",
            "restarted",
            "diagnostic",
            "list_regulations",
            "search",
            "fetch",
        ):
            self.assertIn(repr(item), helper_source)
        self.assertIn("disabled=not previous_complete", helper_source)
        self.assertIn("st.session_state[confirmation_key] = previous_complete", helper_source)

    def test_guide_uses_all_four_existing_workflow_pages(self) -> None:
        source, module = _source_and_module()
        assignments = {
            node.targets[0].id: node.value.value
            for node in module.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }

        guide_assignment = next(
            node
            for node in module.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "BEGINNER_GUIDE_STEPS"
        )
        guide_pages = [
            assignments[element.elts[0].id]
            for element in guide_assignment.value.elts
        ]
        self.assertEqual(
            [
                "① 문서 올려서 전처리",
                "② 결과 확인",
                "③ 검수하고 승인",
                "④ MCP 생성·AI 연결",
            ],
            guide_pages,
        )

        marker_steps = {
            int(call.args[0].value)
            for call in ast.walk(module)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_render_beginner_action_marker"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, int)
        }
        self.assertEqual({1, 2, 3, 4}, marker_steps)
        self.assertNotIn("javascript", source.lower())

    def test_guide_exposes_every_required_subprocedure(self) -> None:
        _source, module = _source_and_module()
        assignment = next(
            node
            for node in module.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "BEGINNER_GUIDE_PROCEDURES"
        )
        procedure_groups = [
            [item.value for item in group.elts]
            for group in assignment.value.elts
        ]

        self.assertEqual([6, 4, 7, 10], [len(group) for group in procedure_groups])
        flattened = [item for group in procedure_groups for item in group]
        for required in (
            "자동 인식한 규정 정보 확인",
            "AI 추가 검수 사용 여부 결정",
            "품질 경고·이슈 확인",
            "각 청크 사람 검증 결과 확인",
            "MCP에 넣을 규정 범위 확인",
            "list_regulations 목록 호출 확인",
            "search 조문 검색 확인",
            "fetch 원문·출처 조회 확인",
        ):
            self.assertIn(required, flattened)

    def test_beginner_preprocess_requires_sequential_manual_confirmations(self) -> None:
        source, module = _source_and_module()
        page_source = ast.get_source_segment(source, _function(module, "_page_preprocess")) or ""

        self.assertIn("_reset_beginner_preprocess_confirmations_for_selection(upload_sources)", page_source)
        self.assertIn("자동 인식한 규정 정보와 필요한 수정값을 확인했습니다.", page_source)
        self.assertIn("AI 추가 검수를 사용할지 여부를 결정했습니다.", page_source)
        self.assertIn("disabled=not info_confirmed", page_source)
        self.assertIn(
            "disabled=poc_review_needs_ack or not beginner_preprocess_confirmations_complete",
            page_source,
        )

    def test_preprocess_confirmation_identity_uses_file_content(self) -> None:
        _source, module = _source_and_module()
        helpers = [
            _function(module, "_beginner_upload_source_content_digest"),
            _function(module, "_beginner_preprocess_selection_identity"),
        ]
        namespace: dict[str, object] = {
            "Path": Path,
            "hashlib": hashlib,
            "json": json,
        }
        exec(
            compile(ast.Module(body=helpers, type_ignores=[]), "<upload-confirmation>", "exec"),
            namespace,
        )
        identity = namespace["_beginner_preprocess_selection_identity"]
        first = identity(
            [{"filename": "same.hwp", "size": 4, "file": SimpleNamespace(getvalue=lambda: b"AAAA")}]
        )
        second = identity(
            [{"filename": "same.hwp", "size": 4, "file": SimpleNamespace(getvalue=lambda: b"BBBB")}]
        )

        self.assertNotEqual(first, second)

    def test_results_confirmation_key_changes_with_document_revision(self) -> None:
        _source, module = _source_and_module()
        helper = _function(module, "_beginner_guide_results_confirmed_key")
        revisions = {"value": (("chunks", 1, 10),)}
        namespace: dict[str, object] = {
            "BEGINNER_GUIDE_RESULTS_CONFIRMED_PREFIX": "results-confirmed",
            "hashlib": hashlib,
            "json": json,
            "_document_context_revision": lambda _document_id: revisions["value"],
        }
        exec(
            compile(ast.Module(body=[helper], type_ignores=[]), "<results-confirmation>", "exec"),
            namespace,
        )
        confirmation_key = namespace["_beginner_guide_results_confirmed_key"]
        first = confirmation_key("doc-1")
        revisions["value"] = (("chunks", 2, 10),)
        second = confirmation_key("doc-1")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("results-confirmed:doc-1:"))

    def test_beginner_mcp_requires_scope_and_output_confirmation_before_bundle(self) -> None:
        source, module = _source_and_module()
        page_source = ast.get_source_segment(source, _function(module, "_page_connect")) or ""

        self.assertIn("MCP에 포함할 규정 범위를 확인했습니다.", page_source)
        self.assertIn("저장 위치·방식, 연결 대상과 MCP 이름을 확인했습니다.", page_source)
        self.assertIn("BEGINNER_GUIDE_MCP_SCOPE_CONFIRMED_PREFIX", page_source)
        self.assertIn("BEGINNER_GUIDE_MCP_OUTPUT_CONFIRMED_PREFIX", page_source)
        self.assertIn("mcp_target_ready = False", page_source)
        self.assertIn(
            "disabled=not mcp_bundle_ready or mcp_profile_scope_mismatch or not mcp_target_ready",
            page_source,
        )

    def test_completion_state_requires_real_approval_and_index_gate(self) -> None:
        _source, module = _source_and_module()
        helper = _function(module, "_beginner_guide_completed_steps")
        namespace: dict[str, object] = {
            "APPROVABLE_CHUNK_STATUSES": frozenset(
                {
                    "draft",
                    "needs_review",
                    "pending",
                    "pending_human_review",
                    "reviewed",
                    "human_reviewed",
                }
            ),
            "_beginner_scope_approval_ready": lambda _ctx: True,
        }
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "<guide-completion>", "exec"), namespace)
        completed_steps = namespace["_beginner_guide_completed_steps"]

        self.assertEqual(
            (False, False, False, False),
            completed_steps(
                None,
                results_confirmed=False,
                mcp_bundle_created=False,
                mcp_connection_confirmed=False,
            ),
        )
        draft_context = {
            "document_id": "doc-1",
            "document": SimpleNamespace(status="completed"),
            "chunks": [object(), object()],
            "approval_counts": {"draft": 2},
            "approved_count": 0,
            "mcp_connection_gate": {"ready": False},
        }
        self.assertEqual(
            (True, False, False, False),
            completed_steps(
                draft_context,
                results_confirmed=False,
                mcp_bundle_created=False,
                mcp_connection_confirmed=False,
            ),
        )
        self.assertEqual(
            (True, True, False, False),
            completed_steps(
                draft_context,
                results_confirmed=True,
                mcp_bundle_created=False,
                mcp_connection_confirmed=False,
            ),
        )
        approved_context = {
            "document_id": "doc-1",
            "document": SimpleNamespace(status="completed"),
            "chunks": [object(), object()],
            "approval_counts": {"approved": 2},
            "approved_count": 2,
            "mcp_connection_gate": {"ready": True},
        }
        self.assertEqual(
            (True, True, True, True),
            completed_steps(
                approved_context,
                results_confirmed=False,
                mcp_bundle_created=True,
                mcp_connection_confirmed=True,
            ),
        )

        self.assertEqual(
            (True, True, True, False),
            completed_steps(
                approved_context,
                results_confirmed=False,
                mcp_bundle_created=True,
                mcp_connection_confirmed=False,
            ),
        )

        partial_context = {
            "document_id": "doc-1",
            "document": SimpleNamespace(status="completed"),
            "chunks": [object(), object()],
            "approval_counts": {"approved": 1, "draft": 1},
            "approved_count": 1,
            "mcp_connection_gate": {"ready": True},
        }
        self.assertEqual(
            (True, True, False, False),
            completed_steps(
                partial_context,
                results_confirmed=True,
                mcp_bundle_created=True,
                mcp_connection_confirmed=True,
            ),
        )

        rejected_context = {
            "document_id": "doc-1",
            "document": SimpleNamespace(status="completed"),
            "chunks": [object(), object()],
            "approval_counts": {"approved": 1, "rejected": 1},
            "approved_count": 1,
            "mcp_connection_gate": {"ready": True},
        }
        self.assertEqual(
            (True, True, True, True),
            completed_steps(
                rejected_context,
                results_confirmed=True,
                mcp_bundle_created=True,
                mcp_connection_confirmed=True,
            ),
        )

        degraded_context = {
            "document_id": "doc-1",
            "document": SimpleNamespace(status="completed"),
            "chunks": [],
            "approval_counts": {},
            "approved_count": 0,
            "mcp_connection_gate": {"ready": False},
            "large_result_warning": "result unavailable",
        }
        self.assertEqual(
            (False, False, False, False),
            completed_steps(
                degraded_context,
                results_confirmed=True,
                mcp_bundle_created=True,
                mcp_connection_confirmed=True,
            ),
        )

        stale_processing_context = {
            **draft_context,
            "document": SimpleNamespace(status="processing"),
        }
        self.assertEqual(
            (False, False, False, False),
            completed_steps(
                stale_processing_context,
                results_confirmed=True,
                mcp_bundle_created=True,
                mcp_connection_confirmed=True,
            ),
        )

        stale_failed_context = {
            **draft_context,
            "document": SimpleNamespace(status="failed"),
        }
        self.assertEqual(
            (False, False, False, False),
            completed_steps(
                stale_failed_context,
                results_confirmed=True,
                mcp_bundle_created=True,
                mcp_connection_confirmed=True,
            ),
        )

    def test_preprocess_result_cta_requires_completed_document_evidence(self) -> None:
        source, module = _source_and_module()
        page = _function(module, "_page_preprocess")
        page_source = ast.get_source_segment(source, page) or ""

        self.assertIn("current_document_ctx", page_source)
        self.assertIn("preprocessing_complete", page_source)
        self.assertIn("_beginner_guide_completed_steps(current_document_ctx)[0]", page_source)
        self.assertIn('if preprocessing_complete:', page_source)
        self.assertNotIn('if st.session_state.get("document_id"):', page_source)
        self.assertIn("전처리가 끝날 때까지 기다리세요", page_source)

    def test_results_confirmation_checkbox_is_required_only_in_beginner_mode(self) -> None:
        source, module = _source_and_module()
        page = _function(module, "_page_results")
        page_source = ast.get_source_segment(source, page) or ""

        self.assertIn("beginner_results_confirmation_required", page_source)
        self.assertIn("results_confirmation_key = _beginner_guide_results_confirmed_key(document_id)", page_source)
        self.assertIn("청크·이슈를 확인했습니다", page_source)
        self.assertIn("structure_confirmation_key", page_source)
        self.assertIn("issues_confirmation_key", page_source)
        self.assertIn("control_key_prefix=structure_confirmation_key", page_source)
        self.assertIn("control_key_prefix=issues_confirmation_key", page_source)
        self.assertIn("disabled=not structure_confirmed", page_source)
        self.assertIn("results_confirmed = bool(structure_confirmed and issues_confirmed)", page_source)
        self.assertIn(
            "beginner_results_confirmation_required and not results_confirmed",
            page_source,
        )
        self.assertNotIn("beginner_results_document_id", page_source)
        self.assertNotIn("_mark_beginner_guide_results_confirmed", source)

    def test_human_review_marker_targets_confirmation_control_not_tab(self) -> None:
        source, module = _source_and_module()
        page = _function(module, "_page_approval")
        page_source = ast.get_source_segment(source, page) or ""

        self.assertNotIn("사람 검증 확인' 탭을 누르세요", page_source)
        self.assertIn("다음으로 바로 아래 '2. 사람 검증 확인' 탭을 여세요", page_source)
        self.assertIn("확인란이 빨간색으로 표시됩니다", page_source)
        self.assertIn("control_key_prefix=human_confirmed_widget_key", page_source)
        self.assertIn("key=human_confirmed_widget_key", page_source)

    def test_approval_guides_ai_result_human_comparison_and_index_separately(self) -> None:
        source, module = _source_and_module()
        page_source = ast.get_source_segment(source, _function(module, "_page_approval")) or ""

        for text in (
            "AI 검증 결과와 제안별 반영 여부를 확인했습니다.",
            "왼쪽: 원본 규정",
            "오른쪽: 전처리·수정 결과",
            "사람 검증 결과: 원본과 전처리 결과를 확인했습니다.",
            "승인하고 색인",
            "이미 승인된 내용 AI에 등록만 실행",
        ):
            self.assertIn(text, page_source)
        self.assertIn("not bool(review_state[\"ai_result_confirmed\"])", page_source)
        self.assertIn("disabled=not bool(review_state[\"ai_result_confirmed\"])", page_source)
        self.assertIn("control_keys=(approve_index_button_key,)", page_source)
        self.assertIn('control_key_prefix="quick-index-only-"', page_source)

    def test_beginner_approval_requires_ai_result_confirmation_signature(self) -> None:
        _source, module = _source_and_module()
        helpers = [
            _function(module, "_approval_ai_result_signature"),
            _function(module, "_approval_review_completion_with_beginner_confirmation"),
        ]
        state = {"beginner": True}
        namespace: dict[str, object] = {
            "hashlib": hashlib,
            "json": json,
            "BEGINNER_GUIDE_ENABLED_KEY": "beginner",
            "st": SimpleNamespace(session_state=state),
            "_approval_chunk_state_key": lambda document_id, chunk_id, item: (
                f"approval:{document_id}:{chunk_id}:{item}"
            ),
            "approval_review_completion_state": approval_review_completion_state,
        }
        exec(
            compile(ast.Module(body=helpers, type_ignores=[]), "<ai-result-confirmation>", "exec"),
            namespace,
        )
        completion = namespace["_approval_review_completion_with_beginner_confirmation"]
        signature = namespace["_approval_ai_result_signature"](
            ["item-1"],
            {"item-1": "reflect"},
        )

        unconfirmed = completion(
            document_id="doc-1",
            chunk_id="chunk-1",
            item_ids=["item-1"],
            ai_decisions={"item-1": "reflect"},
            human_confirmed=True,
        )
        self.assertTrue(unconfirmed["ai_confirmed"])
        self.assertFalse(unconfirmed["ai_result_confirmed"])
        self.assertFalse(unconfirmed["approve_enabled"])

        state["approval:doc-1:chunk-1:ai_result_confirmed"] = signature
        confirmed = completion(
            document_id="doc-1",
            chunk_id="chunk-1",
            item_ids=["item-1"],
            ai_decisions={"item-1": "reflect"},
            human_confirmed=True,
        )
        self.assertTrue(confirmed["ai_result_confirmed"])
        self.assertTrue(confirmed["approve_enabled"])

        changed = completion(
            document_id="doc-1",
            chunk_id="chunk-1",
            item_ids=["item-1"],
            ai_decisions={"item-1": "skip"},
            human_confirmed=True,
        )
        self.assertFalse(changed["ai_result_confirmed"])
        self.assertFalse(changed["approve_enabled"])

    def test_beginner_mcp_shows_only_the_selected_connection_path(self) -> None:
        source, module = _source_and_module()
        page_source = ast.get_source_segment(source, _function(module, "_page_connect")) or ""

        self.assertIn("beginner_target_paths", page_source)
        for path_name in (
            "Claude Code 로컬 연결",
            "Codex CLI·IDE 로컬 연결",
            "Claude Desktop 로컬 연결",
            "ChatGPT 원격 HTTPS 연결",
            "Claude 원격 HTTPS 연결",
        ):
            self.assertIn(path_name, page_source)
        self.assertIn("and not beginner_scope_confirmed", page_source)
        self.assertIn("선택한 방법:", page_source)

    def test_connection_confirmation_is_scoped_to_bundle_generation(self) -> None:
        _source, module = _source_and_module()
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {
                "_default_mcp_scope",
                "_active_mcp_scope",
                "_current_selected_document_ids",
                "_normalized_mcp_bundle_dir",
                "_mcp_request_identity",
                "_mcp_bundle_state_key",
                "_mcp_bundle_state_candidates",
                "_matching_mcp_bundle_state_candidates",
                "_beginner_guide_connection_confirmed_key",
            }
        ]
        state = {
            "mcp-data-scope-doc-1": "selected_documents",
            "mcp-bundle-dir-doc-1": "bundle",
            "mcp-server-name-doc-1": "regulations",
            "mcp-connection-target-doc-1": "claude-desktop",
            "workflow_document_ids": ["doc-1", "doc-2"],
            "workflow_selected_document_ids": ["doc-1", "doc-2"],
            "mcp_setup_bundle_written:selected_documents:doc-1": {
                "written": True,
                "generated_at": "2026-08-02T00:00:00Z",
                "document_id": "doc-1",
                "scope": "selected_documents",
                "export_document_ids": ["doc-2", "doc-1"],
                "scope_revision_signature": [
                    ["doc-1", "revision-doc-1"],
                    ["doc-2", "revision-doc-2"],
                ],
                "server_name": "regulations",
                "connection_target": "claude-desktop",
                "save_mode": "folder-only",
                "bundle_dir": "bundle",
            },
            "mcp_setup_bundle_written:selected_documents:doc-2": {
                "written": True,
                "generated_at": "2026-08-02T00:10:00Z",
                "document_id": "doc-2",
                "scope": "selected_documents",
                "export_document_ids": ["doc-1", "doc-2"],
                "scope_revision_signature": [
                    ["doc-1", "revision-doc-1"],
                    ["doc-2", "revision-doc-2"],
                ],
                "server_name": "regulations",
                "connection_target": "claude-desktop",
                "save_mode": "folder-only",
                "bundle_dir": "bundle",
            },
            "mcp_setup_bundle_written:selected_documents:doc-3": {
                "written": True,
                "generated_at": "2026-08-02T00:30:00Z",
                "document_id": "doc-3",
                "scope": "selected_documents",
                "export_document_ids": ["doc-1", "doc-2"],
                "scope_revision_signature": [
                    ["doc-1", "revision-doc-1"],
                    ["doc-2", "revision-doc-2"],
                ],
                "server_name": "stale-server",
                "connection_target": "claude-desktop",
                "save_mode": "folder-only",
                "bundle_dir": "stale-bundle",
            },
        }
        namespace: dict[str, object] = {
            "BEGINNER_GUIDE_ENABLED_KEY": "beginner_guide_enabled",
            "hashlib": hashlib,
            "json": json,
            "os": os,
            "Path": Path,
            "PROJECT_ROOT": REPO_ROOT,
            "MCP_BUNDLE_STATE_PREFIX": "mcp_setup_bundle_written",
            "BEGINNER_GUIDE_CONNECTION_CONFIRMED_PREFIX": (
                "beginner_guide_connection_confirmed"
            ),
            "WORKFLOW_DOCUMENT_IDS_KEY": "workflow_document_ids",
            "WORKFLOW_SELECTED_DOCUMENT_IDS_KEY": (
                "workflow_selected_document_ids"
            ),
            "SELECTED_INSTITUTION_PROFILE_KEY": (
                "selected_institution_profile_id"
            ),
            "_document_context_revision": lambda document_id: (
                f"revision-{document_id}"
            ),
            "_documents_for_selected_institution": lambda: [],
            "st": SimpleNamespace(session_state=state),
        }
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<guide-connection-confirmation>",
                "exec",
            ),
            namespace,
        )
        confirmation_key = namespace["_beginner_guide_connection_confirmed_key"]

        first_key = confirmation_key("doc-1")
        state["mcp_setup_bundle_written:selected_documents:doc-3"][
            "generated_at"
        ] = "2026-08-02T00:40:00Z"
        self.assertEqual(first_key, confirmation_key("doc-1"))
        state["mcp_setup_bundle_written:selected_documents:doc-1"][
            "generated_at"
        ] = "2026-08-02T00:05:00Z"
        self.assertEqual(first_key, confirmation_key("doc-1"))
        state["mcp_setup_bundle_written:selected_documents:doc-2"][
            "generated_at"
        ] = "2026-08-02T00:20:00Z"
        regenerated_key = confirmation_key("doc-1")
        state["mcp-data-scope-doc-1"] = "current_document"
        scope_key = confirmation_key("doc-1")

        self.assertNotEqual(first_key, regenerated_key)
        self.assertNotEqual(regenerated_key, scope_key)
        self.assertIn(":selected_documents:doc-1:", first_key)
        self.assertIn(":current_document:doc-1:", scope_key)

        state["mcp-data-scope-doc-1"] = "selected_documents"
        current_key = confirmation_key("doc-1")

        state["mcp-bundle-dir-doc-1"] = "other-bundle"
        self.assertNotEqual(current_key, confirmation_key("doc-1"))
        state["mcp-bundle-dir-doc-1"] = "bundle"

        state["mcp-server-name-doc-1"] = "other-server"
        self.assertNotEqual(current_key, confirmation_key("doc-1"))
        state["mcp-server-name-doc-1"] = "regulations"

        state["mcp-connection-target-doc-1"] = "chatgpt-desktop-local"
        self.assertNotEqual(current_key, confirmation_key("doc-1"))

        state["mcp-connection-target-doc-1"] = "codex"
        method_b_key = confirmation_key("doc-1")
        state["method-b-destination-doc-1-selected_documents"] = "codex"
        self.assertEqual(method_b_key, confirmation_key("doc-1"))

    def test_home_workflow_cards_complete_only_in_fail_closed_order(self) -> None:
        _source, module = _source_and_module()
        helper = _function(module, "_workflow_states")
        namespace: dict[str, object] = {
            "APPROVABLE_CHUNK_STATUSES": frozenset(
                {
                    "draft",
                    "needs_review",
                    "pending",
                    "pending_human_review",
                    "reviewed",
                    "human_reviewed",
                }
            ),
            "_mcp_bundle_created": lambda ctx: bool(ctx.get("bundle_ready")),
            "_beginner_guide_results_confirmed_key": lambda document_id: (
                f"results-confirmed:{document_id}"
            ),
            "st": SimpleNamespace(session_state={}),
        }
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                "<home-workflow-states>",
                "exec",
            ),
            namespace,
        )
        workflow_states = namespace["_workflow_states"]

        stale_failed = {
            "document": SimpleNamespace(status="failed"),
            "chunks": [object()],
            "quality_report": SimpleNamespace(passed=True),
            "approval_counts": {"approved": 1},
            "approved_count": 1,
            "mcp_connection_gate": {"ready": True},
            "bundle_ready": True,
        }
        self.assertEqual([False, False, False, False], workflow_states(stale_failed))

        completed = {
            **stale_failed,
            "document": SimpleNamespace(status="completed"),
            "quality_report": SimpleNamespace(passed=False),
            "approval_counts": {"draft": 1},
            "approved_count": 0,
            "mcp_connection_gate": {"ready": False},
            "bundle_ready": False,
        }
        self.assertEqual([True, False, False, False], workflow_states(completed))

        confirmed_warning = {
            **completed,
            "document_id": "doc-warning",
        }
        namespace["st"].session_state["results-confirmed:doc-warning"] = True
        self.assertEqual(
            [True, True, False, False],
            workflow_states(confirmed_warning),
        )

        quality_passed = {
            **completed,
            "quality_report": SimpleNamespace(passed=True),
        }
        self.assertEqual([True, True, False, False], workflow_states(quality_passed))

        pending_review = {
            **quality_passed,
            "approval_counts": {"approved": 1, "draft": 1},
            "approved_count": 1,
            "mcp_connection_gate": {"ready": True},
            "bundle_ready": True,
        }
        self.assertEqual([True, True, False, False], workflow_states(pending_review))

        approval_and_index_ready = {
            **quality_passed,
            "approval_counts": {"approved": 1},
            "approved_count": 1,
            "mcp_connection_gate": {"ready": True},
        }
        self.assertEqual(
            [True, True, True, False],
            workflow_states(approval_and_index_ready),
        )

        approved_quality_override = {
            **approval_and_index_ready,
            "quality_report": SimpleNamespace(passed=False),
        }
        self.assertEqual(
            [True, True, True, False],
            workflow_states(approved_quality_override),
        )

        actual_bundle_ready = {
            **approval_and_index_ready,
            "bundle_ready": True,
        }
        self.assertEqual(
            [True, True, True, True],
            workflow_states(actual_bundle_ready),
        )

    def test_guide_helpers_never_call_approval_or_index_actions(self) -> None:
        _source, module = _source_and_module()
        forbidden_calls = {
            "approve_review_chunks",
            "index_document",
            "index_documents_batch",
            "reindex_document",
        }
        guide_functions = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and (
                node.name.startswith("_beginner_guide")
                or node.name in {
                    "_mark_beginner_guide_results_confirmed",
                    "_render_beginner_action_marker",
                    "_render_beginner_mode_choice",
                }
            )
        ]
        called_names = {
            call.func.id
            for function in guide_functions
            for call in ast.walk(function)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        self.assertFalse(forbidden_calls & called_names)

    def test_operator_file_hash_rechecks_same_render_same_identity_bytes(
        self,
    ) -> None:
        _source, module = _source_and_module()
        helper_names = {
            "_candidate_operator_paths",
            "_operator_file_sha256",
        }
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        calls: list[object] = []
        state = {"_mcp_runtime_integrity_render_nonce": 1}

        def counted_hash(path: object) -> str:
            calls.append(path)
            return hashlib.sha256(path.read_bytes()).hexdigest()  # type: ignore[attr-defined]

        class FixedIdentityPath:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload
                self.available = True
                self.hash_error = False
                self.identity = SimpleNamespace(
                    st_size=len(payload),
                    st_mtime_ns=100,
                    st_ctime_ns=200,
                    st_dev=300,
                    st_ino=400,
                )

            def is_symlink(self) -> bool:
                return False

            def is_file(self) -> bool:
                return self.available

            def stat(self) -> object:
                return self.identity

            def resolve(self, *, strict: bool = False) -> object:
                del strict
                return self

            def read_bytes(self) -> bytes:
                if self.hash_error:
                    raise OSError("read failed")
                return self.payload

        namespace: dict[str, object] = {
            "Path": Path,
            "PROJECT_ROOT": REPO_ROOT,
            "lru_cache": lru_cache,
            "os": os,
            "st": SimpleNamespace(session_state=state),
            "MCP_RUNTIME_INTEGRITY_RENDER_NONCE_KEY": (
                "_mcp_runtime_integrity_render_nonce"
            ),
            "_sha256_file": counted_hash,
        }
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<operator-file-hash-cache>",
                "exec",
            ),
            namespace,
        )
        file_hash = namespace["_operator_file_sha256"]
        target = FixedIdentityPath(b"first")
        namespace["_candidate_operator_paths"] = lambda _raw_path: [target]

        first_hash = file_hash("bundle.zip")
        self.assertEqual(first_hash, file_hash("bundle.zip"))
        self.assertEqual(2, len(calls))

        # Simulate an in-place Windows rewrite whose complete stat identity is
        # unchanged.  Integrity checks must still observe the new bytes during
        # the same Streamlit render.
        target.payload = b"other"
        second_hash = file_hash("bundle.zip")
        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(3, len(calls))

        state["_mcp_runtime_integrity_render_nonce"] = 2
        self.assertEqual(second_hash, file_hash("bundle.zip"))
        self.assertEqual(4, len(calls))

        target.available = False
        self.assertEqual("", file_hash("bundle.zip"))
        self.assertEqual(4, len(calls))

        target.available = True
        target.hash_error = True
        self.assertEqual("", file_hash("bundle.zip"))
        self.assertEqual(5, len(calls))

    def test_bundle_detection_requires_matching_active_export_scope(self) -> None:
        _source, module = _source_and_module()
        helper_names = {
            "_default_mcp_scope",
            "_active_mcp_scope",
            "_current_selected_document_ids",
            "_normalized_mcp_bundle_dir",
            "_mcp_request_identity",
            "_candidate_operator_paths",
            "_operator_file_sha256",
            "_runtime_bundle_stat_signature",
            "_cached_mcp_runtime_bundle_integrity",
            "_mcp_runtime_bundle_ready",
            "_mcp_setup_files_ready",
            "_mcp_zip_ready",
            "_mcp_bundle_state_key",
            "_mcp_bundle_state_candidates",
            "_matching_mcp_bundle_state_candidates",
            "_mcp_bundle_created",
        }
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            bundle_dir.mkdir()
            connect_path = bundle_dir / "connect_mcp_client.ps1"
            connect_path.write_text(
                "# synthetic test fixture\n",
                encoding="utf-8",
            )
            stdio_launcher_path = bundle_dir / "run_mcp_stdio_server.ps1"
            stdio_launcher_path.write_text(
                "# synthetic stdio launcher\n",
                encoding="utf-8",
            )
            install_path = bundle_dir / "install_local_package.ps1"
            install_path.write_text(
                "# synthetic package installer\n",
                encoding="utf-8",
            )
            codex_config_path = bundle_dir / "codex_config_snippet.toml"
            codex_config_path.write_text("[mcp_servers]\n", encoding="utf-8")
            runtime_data_dir = bundle_dir / "data"
            runtime_paths = _write_sealed_runtime_fixture(
                runtime_data_dir,
                document_ids=["doc-1", "doc-2"],
            )
            vector_path = runtime_paths["vector"]
            approval_journal_path = runtime_paths["approval_journal"]
            approval_snapshot_path = runtime_paths["approval_snapshot"]
            omission_disposition_path = runtime_paths["omission_disposition_snapshot"]
            runtime_manifest_path = runtime_paths["runtime_manifest"]
            zip_path = root / "bundle.zip"
            zip_path.write_bytes(b"synthetic")
            target_path = bundle_dir / "claude_desktop_config.json"
            target_path.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            target_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
            saved_bundle_identity = {
                "generated_at": "2026-08-02T00:00:00Z",
                "profile_id": "profile-a",
                "save_mode": "folder-and-zip",
                "server_name": "regulations",
                "connection_target": "claude-desktop",
                "bundle_dir": str(bundle_dir),
                "runtime_data_dir": str(runtime_data_dir),
                "runtime_manifest": str(runtime_manifest_path),
                "connection_target_file": str(target_path),
                "connection_target_file_sha256": target_sha256,
                "zip": str(zip_path),
                "zip_sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
                "setup_file_sha256": {
                    "codex_config": hashlib.sha256(
                        codex_config_path.read_bytes()
                    ).hexdigest(),
                    "connect": hashlib.sha256(connect_path.read_bytes()).hexdigest(),
                    "install": hashlib.sha256(install_path.read_bytes()).hexdigest(),
                    "stdio_launcher": hashlib.sha256(
                        stdio_launcher_path.read_bytes()
                    ).hexdigest(),
                },
            }
            state = {
                "mcp-data-scope-doc-1": "selected_documents",
                "mcp-bundle-dir-doc-1": str(bundle_dir),
                "mcp-server-name-doc-1": "regulations",
                "mcp-connection-target-doc-1": "claude-desktop",
                "workflow_document_ids": ["doc-1", "doc-2"],
                "workflow_selected_document_ids": ["doc-1", "doc-2"],
                "workflow-document-selected-doc-1": True,
                "workflow-document-selected-doc-2": True,
                "selected_institution_profile_id": "profile-a",
                "mcp_setup_bundle_written:selected_documents:doc-1": {
                    "written": True,
                    "document_id": "doc-1",
                    "scope": "selected_documents",
                    "export_document_ids": ["doc-2", "doc-1"],
                    "scope_revision_signature": [
                        ["doc-1", "revision-doc-1"],
                        ["doc-2", "revision-doc-2"],
                    ],
                    **saved_bundle_identity,
                }
            }
            namespace: dict[str, object] = {
                "BEGINNER_GUIDE_ENABLED_KEY": "beginner_guide_enabled",
                "json": json,
                "os": os,
                "Path": Path,
                "lru_cache": lru_cache,
                "PROJECT_ROOT": root,
                "RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES": (
                    RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES
                ),
                "MCP_BUNDLE_STATE_PREFIX": "mcp_setup_bundle_written",
                "MCP_RUNTIME_INTEGRITY_RENDER_NONCE_KEY": (
                    "_mcp_runtime_integrity_render_nonce"
                ),
                "MCP_COMPLETION_SETUP_FILES": {
                    "codex_config": "codex_config_snippet.toml",
                    "connect": "connect_mcp_client.ps1",
                    "install": "install_local_package.ps1",
                    "stdio_launcher": "run_mcp_stdio_server.ps1",
                },
                "WORKFLOW_DOCUMENT_IDS_KEY": "workflow_document_ids",
                "WORKFLOW_SELECTED_DOCUMENT_IDS_KEY": (
                    "workflow_selected_document_ids"
                ),
                "SELECTED_INSTITUTION_PROFILE_KEY": (
                    "selected_institution_profile_id"
                ),
                "_document_context_revision": lambda document_id: (
                    f"revision-{document_id}"
                ),
                "_documents_for_selected_institution": lambda: [
                    SimpleNamespace(document_id="doc-1"),
                    SimpleNamespace(document_id="doc-2"),
                ],
                "_sha256_file": lambda path: hashlib.sha256(
                    Path(path).read_bytes()
                ).hexdigest(),
                "validate_mcp_runtime_data_bundle_integrity": (
                    validate_mcp_runtime_data_bundle_integrity
                ),
                "st": SimpleNamespace(session_state=state),
            }
            exec(
                compile(
                    ast.Module(body=helper_nodes, type_ignores=[]),
                    "<guide-bundle-state>",
                    "exec",
                ),
                namespace,
            )
            bundle_created = namespace["_mcp_bundle_created"]

            self.assertTrue(bundle_created({"document_id": "doc-1"}))
            self.assertTrue(bundle_created({"document_id": "doc-2"}))

            connect_bytes = connect_path.read_bytes()
            connect_path.write_bytes(b"")
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            connect_path.write_bytes(connect_bytes)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            launcher_bytes = stdio_launcher_path.read_bytes()
            stdio_launcher_path.unlink()
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            stdio_launcher_path.write_bytes(launcher_bytes)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            install_bytes = install_path.read_bytes()
            install_path.unlink()
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            install_path.write_bytes(install_bytes)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            zip_path.write_bytes(b"")
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            zip_path.write_bytes(b"synthetic")
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            state["mcp-bundle-dir-doc-1"] = str(root / "other-bundle")
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            state["mcp-bundle-dir-doc-1"] = str(bundle_dir)

            state["mcp-server-name-doc-1"] = "other-server"
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            state["mcp-server-name-doc-1"] = "regulations"

            state["mcp-connection-target-doc-1"] = "chatgpt-desktop-local"
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            state["mcp-connection-target-doc-1"] = "claude-desktop"

            runtime_manifest_text = runtime_manifest_path.read_text(encoding="utf-8")
            runtime_manifest_path.unlink()
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            runtime_manifest_path.write_text(runtime_manifest_text, encoding="utf-8")

            vector_bytes = vector_path.read_bytes()
            vector_path.unlink()
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            vector_path.write_bytes(vector_bytes)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            original_stat = vector_path.stat()
            same_size_tamper = bytearray(vector_bytes)
            same_size_tamper[0] ^= 1
            vector_path.write_bytes(bytes(same_size_tamper))
            os.utime(
                vector_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            state["_mcp_runtime_integrity_render_nonce"] = 1
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            vector_path.write_bytes(vector_bytes)
            os.utime(
                vector_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            state["_mcp_runtime_integrity_render_nonce"] = 2
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            vector_path.write_bytes(b"")
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            vector_path.write_bytes(vector_bytes + b"{\"tampered\": true}\n")
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            vector_path.write_bytes(vector_bytes)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            for approval_path in (
                approval_journal_path,
                approval_snapshot_path,
                omission_disposition_path,
            ):
                approval_bytes = approval_path.read_bytes()
                approval_path.unlink()
                self.assertFalse(bundle_created({"document_id": "doc-1"}))
                approval_path.write_bytes(approval_bytes)
                self.assertTrue(bundle_created({"document_id": "doc-1"}))

            target_bytes = target_path.read_bytes()
            target_path.unlink()
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            target_path.write_bytes(target_bytes + b"tampered")
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            target_path.write_bytes(target_bytes)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            disallowed_nodes_path = (
                runtime_data_dir / "repository" / "doc-1_nodes.json"
            )
            disallowed_nodes_path.write_text("[]\n", encoding="utf-8")
            _reseal_runtime_fixture(runtime_data_dir)
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            disallowed_nodes_path.unlink()
            _reseal_runtime_fixture(runtime_data_dir)
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            state["mcp-data-scope-doc-1"] = "current_document"
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            state["mcp_setup_bundle_written:current_document:doc-1"] = {
                "written": True,
                "document_id": "doc-1",
                "scope": "current_document",
                "export_document_id": "doc-1",
                "export_document_ids": ["doc-1"],
                "scope_revision_signature": [["doc-1", "revision-doc-1"]],
                **saved_bundle_identity,
            }
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

            zip_path.unlink()
            state["mcp_setup_bundle_written:current_document:doc-1"].update(
                {"save_mode": "folder-only", "zip": ""}
            )
            self.assertTrue(bundle_created({"document_id": "doc-1"}))
            state["mcp-save-mode-doc-1"] = "folder-and-zip"
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            state["mcp-save-mode-doc-1"] = "folder-only"
            self.assertTrue(bundle_created({"document_id": "doc-1"}))
            state["mcp_setup_bundle_written:current_document:doc-1"]["save_mode"] = (
                "folder-and-zip"
            )
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            zip_path.write_bytes(b"synthetic")
            state["mcp-save-mode-doc-1"] = "folder-and-zip"

            state["mcp-data-scope-doc-1"] = "selected_documents"
            state["workflow_selected_document_ids"] = ["doc-1"]
            state["workflow-document-selected-doc-2"] = False
            self.assertFalse(bundle_created({"document_id": "doc-1"}))

            # The widget state leads the aggregate list by one Streamlit rerun.
            state["workflow_selected_document_ids"] = ["doc-1", "doc-2"]
            self.assertFalse(bundle_created({"document_id": "doc-1"}))

            state["mcp-data-scope-doc-1"] = "selected_institution"
            state["mcp_setup_bundle_written:selected_institution:doc-1"] = {
                **saved_bundle_identity,
                "written": True,
                "document_id": "doc-1",
                "scope": "selected_institution",
                "profile_id": "profile-b",
                "export_document_ids": ["doc-1", "doc-2"],
                "scope_revision_signature": [
                    ["doc-1", "revision-doc-1"],
                    ["doc-2", "revision-doc-2"],
                ],
            }
            self.assertFalse(bundle_created({"document_id": "doc-1"}))
            state[
                "mcp_setup_bundle_written:selected_institution:doc-1"
            ]["profile_id"] = "profile-a"
            self.assertTrue(bundle_created({"document_id": "doc-1"}))

    def test_beginner_mode_defaults_mcp_scope_to_current_document(self) -> None:
        _source, module = _source_and_module()
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_default_mcp_scope", "_active_mcp_scope"}
        ]
        state = {
            "beginner_guide_enabled": True,
        }
        namespace: dict[str, object] = {
            "BEGINNER_GUIDE_ENABLED_KEY": "beginner_guide_enabled",
            "st": SimpleNamespace(session_state=state),
        }
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<beginner-default-mcp-scope>",
                "exec",
            ),
            namespace,
        )
        active_scope = namespace["_active_mcp_scope"]

        self.assertEqual("current_document", active_scope("doc-1"))

        state["mcp-data-scope-doc-1"] = "selected_institution"
        self.assertEqual("selected_institution", active_scope("doc-1"))

        state.clear()
        self.assertEqual("selected_documents", active_scope("doc-1"))

    def test_selected_documents_pending_approval_lists_only_incomplete_documents(self) -> None:
        _source, module = _source_and_module()
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {
                "_approval_status",
                "_mcp_scope_document_state",
                "_selected_documents_pending_approval",
            }
        ]
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<selected-documents-pending-approval>",
                "exec",
            ),
            namespace,
        )
        pending_documents = namespace["_selected_documents_pending_approval"]

        approved_chunk = SimpleNamespace(approval_status="approved")
        pending_chunk = SimpleNamespace(approval_status="pending")

        self.assertEqual(
            ["doc-2", "doc-3"],
            pending_documents(
                ["doc-1", "doc-2", "doc-3"],
                [
                    {"document_id": "doc-1", "chunks": [approved_chunk]},
                    {"document_id": "doc-2", "chunks": [pending_chunk]},
                ],
            ),
        )

    def test_terminal_rejected_documents_are_not_pending_and_are_not_reindex_targets(self) -> None:
        _source, module = _source_and_module()
        helper_names = {
            "_approval_status",
            "_mcp_scope_document_state",
            "_mcp_visible_scope_documents",
            "_selected_documents_pending_approval",
        }
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<terminal-rejected-streamlit-helpers>",
                "exec",
            ),
            namespace,
        )
        document_state = namespace["_mcp_scope_document_state"]
        pending_documents = namespace["_selected_documents_pending_approval"]
        visible_documents = namespace["_mcp_visible_scope_documents"]

        approved = SimpleNamespace(approval_status="approved")
        rejected = SimpleNamespace(approval_status="rejected")
        needs_review = SimpleNamespace(approval_status="needs_review")
        self.assertEqual(
            "visible-ready",
            document_state([approved], {"ready": True})["state"],
        )
        self.assertEqual(
            "terminal-excluded",
            document_state([rejected], {"ready": False})["state"],
        )
        self.assertEqual(
            "blocking",
            document_state([approved, needs_review], {"ready": True})["state"],
        )
        self.assertEqual(
            ["doc-needs-review"],
            pending_documents(
                ["doc-approved", "doc-rejected", "doc-needs-review"],
                [
                    {"document_id": "doc-approved", "chunks": [approved]},
                    {"document_id": "doc-rejected", "chunks": [rejected]},
                    {"document_id": "doc-needs-review", "chunks": [needs_review]},
                ],
            ),
        )
        approved_document = SimpleNamespace(document_id="doc-approved")
        rejected_document = SimpleNamespace(document_id="doc-rejected")
        self.assertEqual(
            [approved_document],
            visible_documents(
                [approved_document, rejected_document],
                {"visible_document_ids": ["doc-approved"]},
            ),
        )

    def test_workflow_mcp_gate_accepts_terminal_exclusion_only_with_visible_peer(self) -> None:
        _source, module = _source_and_module()
        helper_names = {
            "_approval_status",
            "_mcp_connection_gate",
            "_mcp_scope_document_state",
            "_workflow_mcp_gate_summary",
        }
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name in helper_names
        ]

        approved = SimpleNamespace(approval_status="approved")
        rejected = SimpleNamespace(approval_status="rejected")
        needs_review = SimpleNamespace(approval_status="needs_review")
        documents = {
            document_id: SimpleNamespace(document_id=document_id, tenant_id="tenant-1")
            for document_id in ("doc-approved", "doc-rejected", "doc-needs-review")
        }
        chunks_by_document_id = {
            "doc-approved": [approved],
            "doc-rejected": [rejected],
            "doc-needs-review": [needs_review],
        }

        class FakeRepository:
            def get_document(self, document_id: str):
                return documents.get(document_id)

            def get_chunks(self, document_id: str):
                return chunks_by_document_id.get(document_id, [])

        class FakeAuthContext:
            def __init__(self, **_kwargs) -> None:
                pass

        class FakeStreamlit:
            session_state: dict[str, object] = {}

        def index_status(document_id: str, _auth: object) -> dict[str, object]:
            visible_count = 1 if document_id == "doc-approved" else 0
            return {
                "indexing_status": "indexed",
                "vector_summary": {"record_count": visible_count},
                "vector_consistency": {"stale_count": 0},
            }

        namespace: dict[str, object] = {
            "APPROVABLE_CHUNK_STATUSES": frozenset({"draft", "needs_review", "pending"}),
            "AuthContext": FakeAuthContext,
            "WORKFLOW_MCP_GATE_CACHE_KEY": "workflow-gate",
            "_document_context_revision": lambda document_id: (document_id, 1),
            "_local_operator_tenant_id": lambda: "tenant-1",
            "_mcp_gate_guidance_items": lambda *_args, **_kwargs: [],
            "_workflow_document_label": lambda document: document.document_id,
            "get_index_status": index_status,
            "repository": FakeRepository(),
            "st": FakeStreamlit(),
        }
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<workflow-terminal-exclusion-gate>",
                "exec",
            ),
            namespace,
        )
        gate_summary = namespace["_workflow_mcp_gate_summary"]

        peer_summary = gate_summary(["doc-approved", "doc-rejected"], {})
        self.assertTrue(peer_summary["ready"])
        self.assertEqual(["doc-approved"], peer_summary["visible_document_ids"])
        self.assertEqual(["doc-rejected"], peer_summary["terminal_excluded_document_ids"])

        FakeStreamlit.session_state.clear()
        rejected_only_summary = gate_summary(["doc-rejected"], {})
        self.assertFalse(rejected_only_summary["ready"])
        self.assertEqual([], rejected_only_summary["visible_document_ids"])

        FakeStreamlit.session_state.clear()
        blocked_summary = gate_summary(["doc-approved", "doc-needs-review"], {})
        self.assertFalse(blocked_summary["ready"])
        self.assertEqual(["doc-needs-review"], blocked_summary["blocking_document_ids"])

    def test_selected_approval_contexts_reuse_revision_matched_documents(self) -> None:
        _source, module = _source_and_module()
        helper = _function(module, "_selected_approval_contexts")

        class FakeRepository:
            def __init__(self) -> None:
                self.document_reads = 0
                self.chunk_reads = 0
                self.run_reads = 0

            def get_document(self, document_id: str) -> SimpleNamespace:
                self.document_reads += 1
                return SimpleNamespace(document_id=document_id, tenant_id="tenant-1")

            def get_chunks(self, document_id: str) -> list[SimpleNamespace]:
                self.chunk_reads += 1
                return [
                    SimpleNamespace(
                        chunk_id=f"{document_id}-chunk-1",
                        approval_status="pending",
                    )
                ]

            def latest_completed_run(self, _document_id: str) -> SimpleNamespace:
                self.run_reads += 1
                return SimpleNamespace(stats={"agent_review": {"status": "ready"}})

        state: dict[str, object] = {}
        revisions = {"doc-other": (("chunks", 1, 10),)}
        fake_repository = FakeRepository()
        namespace: dict[str, object] = {
            "st": SimpleNamespace(session_state=state),
            "repository": fake_repository,
            "SELECTED_APPROVAL_CONTEXT_CACHE_KEY": "selected-approval-cache",
            "SELECTED_APPROVAL_CONTEXT_CACHE_MAX_ENTRIES": 4,
            "_document_context_revision": lambda document_id: revisions[document_id],
            "_local_operator_tenant_id": lambda: "tenant-1",
            "AuthContext": lambda **kwargs: kwargs,
            "_approval_status": lambda chunk: chunk.approval_status,
            "chunk_review_attention_reasons": lambda _chunk: [],
        }
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                "<selected-approval-context-cache>",
                "exec",
            ),
            namespace,
        )
        load_contexts = namespace["_selected_approval_contexts"]
        current_context = {"document_id": "doc-current", "chunks": []}

        first = load_contexts(["doc-current", "doc-other"], current_context)
        second = load_contexts(["doc-current", "doc-other"], current_context)

        self.assertEqual(2, len(first))
        self.assertEqual(2, len(second))
        self.assertEqual(
            (1, 1, 1),
            (
                fake_repository.document_reads,
                fake_repository.chunk_reads,
                fake_repository.run_reads,
            ),
        )
        self.assertEqual(1, len(state["selected-approval-cache"]))

        revisions["doc-other"] = (("chunks", 2, 10),)
        load_contexts(["doc-current", "doc-other"], current_context)
        self.assertEqual(
            (2, 2, 2),
            (
                fake_repository.document_reads,
                fake_repository.chunk_reads,
                fake_repository.run_reads,
            ),
        )

        for index in range(2, 8):
            revisions[f"doc-{index}"] = (("chunks", index, 10),)
        load_contexts(
            ["doc-current", *(f"doc-{index}" for index in range(2, 8))],
            current_context,
        )
        self.assertEqual(4, len(state["selected-approval-cache"]))

    def test_kordoc_reprocessing_requires_an_explicit_button_click(self) -> None:
        source, module = _source_and_module()
        page = _function(module, "_page_connect")
        page_source = "\n".join(source.splitlines()[page.lineno - 1 : page.end_lineno])

        self.assertNotIn("automatic_trigger", page_source)
        self.assertNotIn("_kordoc_auto_reprocess_attempt_key", source)
        self.assertIn("if retry_trigger:", page_source)
        self.assertLess(
            page_source.index("retry_trigger = st.button("),
            page_source.index("_safe_kordoc_reprocess_documents("),
        )

    def test_multi_document_results_explain_beginner_one_by_one_scope(self) -> None:
        source, module = _source_and_module()
        page = _function(module, "_page_results")
        page_source = "\n".join(source.splitlines()[page.lineno - 1 : page.end_lineno])

        self.assertIn("beginner_reviews_one_document", page_source)
        self.assertIn("현재 화면의 규정부터 1개씩 검수합니다", page_source)
        self.assertIn("현재 규정 ③ 검수·승인으로 이동", page_source)
        self.assertIn("문서 목록에서 다음 규정을 선택", page_source)

    def test_beginner_mode_disables_batch_and_advanced_approval_paths(self) -> None:
        source, module = _source_and_module()
        page = _function(module, "_page_approval")
        page_source = "\n".join(source.splitlines()[page.lineno - 1 : page.end_lineno])

        self.assertIn("일괄 검수·승인·색인 버튼을 사용할 수 없습니다", page_source)
        self.assertIn(
            "beginner_bulk_review_disabled\n                or not workflow_contexts_complete",
            page_source,
        )
        self.assertIn("disabled=beginner_mode_active", page_source)
        self.assertIn("show_advanced_approval = False", page_source)
        self.assertIn(
            "official_approval_disabled = bool(approval_evidence_missing) or beginner_mode_active",
            page_source,
        )

    def test_institution_switch_discards_selected_approval_context_cache(self) -> None:
        source, module = _source_and_module()
        select_profile = _function(module, "_select_institution_profile")
        select_profile_source = "\n".join(
            source.splitlines()[select_profile.lineno - 1 : select_profile.end_lineno]
        )
        delete_profile = _function(module, "_delete_registered_institution")
        delete_profile_source = "\n".join(
            source.splitlines()[delete_profile.lineno - 1 : delete_profile.end_lineno]
        )

        self.assertIn("SELECTED_APPROVAL_CONTEXT_CACHE_KEY", select_profile_source)
        self.assertIn("SELECTED_APPROVAL_CONTEXT_CACHE_KEY", delete_profile_source)

    def test_runtime_bundle_integrity_cache_reuses_same_bundle_across_reruns(
        self,
    ) -> None:
        _source, module = _source_and_module()
        helper_nodes = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {
                "_candidate_operator_paths",
                "_runtime_bundle_stat_signature",
                "_cached_mcp_runtime_bundle_integrity",
                "_mcp_runtime_bundle_ready",
            }
        ]
        calls: list[tuple[str, str | None]] = []

        def _validator(
            path: Path,
            *,
            expected_logical_corpus_sha256: str | None = None,
        ) -> None:
            calls.append((str(path), expected_logical_corpus_sha256))

        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / "runtime"
            runtime_dir.mkdir()
            (runtime_dir / "manifest.json").write_text(
                "{\"ok\": true}\n",
                encoding="utf-8",
            )
            state = {
                "runtime_data_dir": str(runtime_dir),
                "logical_corpus_sha256": "abc123",
                "unrelated_rerun_counter": 1,
            }
            namespace: dict[str, object] = {
                "MCP_RUNTIME_INTEGRITY_RENDER_NONCE_KEY": (
                    "_mcp_runtime_integrity_render_nonce"
                ),
                "Path": Path,
                "RUNTIME_DATA_ZIP_EXCLUDED_FILENAMES": tuple(),
                "lru_cache": lru_cache,
                "os": os,
                "st": SimpleNamespace(session_state=state),
                "_sha256_file": lambda path: hashlib.sha256(
                    Path(path).read_bytes()
                ).hexdigest(),
                "validate_mcp_runtime_data_bundle_integrity": _validator,
            }
            exec(
                compile(
                    ast.Module(body=helper_nodes, type_ignores=[]),
                    "<runtime-bundle-cache-reuse>",
                    "exec",
                ),
                namespace,
            )
            runtime_bundle_ready = namespace["_mcp_runtime_bundle_ready"]

            self.assertTrue(runtime_bundle_ready(state))
            self.assertTrue(runtime_bundle_ready(state))
            self.assertEqual(1, len(calls))

            state["unrelated_rerun_counter"] = 2
            self.assertTrue(runtime_bundle_ready(state))
            self.assertEqual(1, len(calls))

            manifest_path = runtime_dir / "manifest.json"
            original_stat = manifest_path.stat()
            original_bytes = manifest_path.read_bytes()
            replacement_bytes = original_bytes.replace(b"true", b"else")
            self.assertEqual(len(original_bytes), len(replacement_bytes))
            manifest_path.write_bytes(replacement_bytes)
            os.utime(
                manifest_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            self.assertTrue(runtime_bundle_ready(state))
            self.assertEqual(2, len(calls))

            runtime_signature = namespace["_runtime_bundle_stat_signature"]

            class RootSymlink:
                def lstat(self) -> object:
                    return SimpleNamespace(st_mtime_ns=100, st_size=0)

                def is_symlink(self) -> bool:
                    return True

                def is_dir(self) -> bool:
                    return True

            self.assertTrue(runtime_signature(RootSymlink())[0][3])

    def test_bundle_regeneration_clears_stale_proof_before_work_and_records_only_on_success(
        self,
    ) -> None:
        _source, module = _source_and_module()
        page = _function(module, "_page_connect")

        generation_if = next(
            node
            for node in ast.walk(page)
            if isinstance(node, ast.If)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "st"
                and call.func.attr == "button"
                and any(
                    keyword.arg == "key"
                    and "write-mcp-bundle-" in ast.unparse(keyword.value)
                    for keyword in call.keywords
                )
                for call in ast.walk(node.test)
            )
        )

        scoped_clears = [
            call
            for statement in generation_if.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_clear_mcp_bundle_states"
            and len(call.args) >= 2
        ]

        def is_scoped_state_key_call(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_mcp_bundle_state_key"
                and len(node.args) >= 2
            )

        scoped_assignments = [
            node
            for statement in generation_if.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and is_scoped_state_key_call(target.slice)
                for target in node.targets
            )
        ]

        self.assertEqual(1, len(scoped_clears))
        self.assertEqual(1, len(scoped_assignments))
        stale_proof_clear = scoped_clears[0]
        success_assignment = scoped_assignments[0]
        self.assertLess(stale_proof_clear.lineno, success_assignment.lineno)

        operation_calls = [
            call
            for statement in generation_if.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id
            in {
                "_run_background_operation_with_progress",
                "write_mcp_runtime_data_bundle",
                "write_mcp_setup_bundle",
                "write_mcp_setup_bundle_zip",
            }
        ]
        self.assertTrue(operation_calls)
        self.assertLess(
            stale_proof_clear.lineno,
            min(call.lineno for call in operation_calls),
        )
        self.assertGreater(
            success_assignment.lineno,
            max(call.lineno for call in operation_calls),
        )

        success_try = next(
            node
            for node in ast.walk(generation_if)
            if isinstance(node, ast.Try)
            and success_assignment
            in {
                descendant
                for statement in node.body
                for descendant in ast.walk(statement)
            }
        )
        failure_descendants = {
            descendant
            for handler in success_try.handlers
            for descendant in ast.walk(handler)
        }
        failure_descendants.update(
            descendant
            for statement in [*success_try.orelse, *success_try.finalbody]
            for descendant in ast.walk(statement)
        )
        self.assertNotIn(success_assignment, failure_descendants)

    def test_kordoc_install_is_explicit_and_shown_before_upload(self) -> None:
        source, module = _source_and_module()
        self.assertIn("_render_kordoc_preprocess_preflight()", source)
        self.assertIn("Kordoc 설치·검증 시작", source)
        self.assertIn("PDF·HWP·HWPX·DOCX를 공식 MCP로 만들려면 Kordoc을 준비하세요", source)
        self.assertIn("일반 본문의 조문·항·호 구조를 처음 읽는 파서 자체가 Kordoc인 것은 아닙니다.", source)
        self.assertIn("공식 MCP 파일 묶음에는 PDF·HWP·HWPX·DOCX 네 형식 모두 Kordoc 표 파싱 품질 증거가 필요합니다.", source)
        self.assertIn("미설치 상태에서 처리한 문서는 나중에 Kordoc 설치 후 새 초안으로", source)
        self.assertIn("사용자 환경에 전역 설치", source)
        self.assertIn("_application_restart_instruction()", source)
        self.assertIn("PR MCP Builder.exe", source)
        self.assertIn("START_HERE.bat", source)
        self.assertIn(
            'control_key_prefix="preprocess-kordoc-install-run"',
            source,
        )
        self.assertIn(
            'control_key_prefix="preprocess-nodejs-link"',
            source,
        )
        self.assertIn("Node.js LTS 설치 페이지 열기", source)
        self.assertLess(
            source.index('if npm_available:'),
            source.index('control_key_prefix="preprocess-kordoc-install-run"'),
        )
        self.assertNotIn("kordoc_auto_install_attempted", source)
        self.assertNotIn("자동 설치·검증을 시도", source)

        preprocess = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_page_preprocess"
        )
        called_names = [
            call.func.id
            for call in ast.walk(preprocess)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        ]
        self.assertLess(
            called_names.index("_render_kordoc_preprocess_preflight"),
            called_names.index("_uploaded_file_list"),
        )

    def test_beginner_preprocess_defaults_to_fast_structure_parsing_and_ai_is_opt_in(self) -> None:
        source, module = _source_and_module()

        self.assertIn("기본은 <b>빠른 구조 전처리</b>", source)
        self.assertIn("AI로 의심 구간 추가 검수 (선택)", source)
        self.assertIn('key="preprocess-enable-agent-review"', source)
        self.assertIn("외부 AI 호출 없이 조문·항·호를 정리합니다.", source)
        self.assertIn("처리 시간과 API 사용 비용이 늘 수 있습니다.", source)
        self.assertIn("다음 검토 화면에서 사람이 확인·보완", source)
        self.assertIn("공식 승인·보안 확인은 그대로 진행됩니다.", source)
        self.assertIn("사람 승인과 보안 게이트를 대신하지 않습니다.", source)

        preprocess = _function(module, "_page_preprocess")
        opt_in_checkbox = next(
            call
            for call in ast.walk(preprocess)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "st"
            and call.func.attr == "checkbox"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "AI로 의심 구간 추가 검수 (선택)"
        )
        checkbox_keywords = {keyword.arg: keyword.value for keyword in opt_in_checkbox.keywords}
        self.assertIsInstance(checkbox_keywords["value"], ast.Constant)
        self.assertIs(False, checkbox_keywords["value"].value)

        options_call = next(
            call
            for call in ast.walk(preprocess)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "ChunkOptions"
        )
        options_keywords = {keyword.arg: keyword.value for keyword in options_call.keywords}
        self.assertIsInstance(options_keywords["enable_agent_review"], ast.Name)
        self.assertEqual("ai_review_requested", options_keywords["enable_agent_review"].id)

    def test_beginner_single_chunk_rejection_requires_reason_and_confirmation(self) -> None:
        source, module = _source_and_module()
        helper = _function(module, "_chunk_rejection_ready")
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=[helper], type_ignores=[]),
                "<single-chunk-rejection-ready>",
                "exec",
            ),
            namespace,
        )
        rejection_ready = namespace["_chunk_rejection_ready"]

        self.assertFalse(rejection_ready(reason="", confirmed=True, approvable=True))
        self.assertFalse(rejection_ready(reason="   ", confirmed=True, approvable=True))
        self.assertFalse(rejection_ready(reason="잘못 합쳐짐", confirmed=False, approvable=True))
        self.assertFalse(rejection_ready(reason="잘못 합쳐짐", confirmed=True, approvable=False))
        self.assertTrue(rejection_ready(reason="잘못 합쳐짐", confirmed=True, approvable=True))

        approval_page = _function(module, "_page_approval")
        page_source = "\n".join(
            source.splitlines()[approval_page.lineno - 1 : approval_page.end_lineno]
        )
        self.assertIn("이 청크 반려", page_source)
        self.assertIn("반려 사유 (필수)", page_source)
        self.assertIn("현재 화면에서 선택한 청크 1개만 반려", page_source)
        self.assertIn("disabled=not rejection_ready", page_source)
        self.assertIn("chunk_ids=[str(compare_chunk.chunk_id)]", page_source)
        self.assertIn("reason=str(rejection_reason).strip()", page_source)
        self.assertIn("reject_review_chunks(", page_source)
        self.assertIn("승인·색인하지 않았으며 MCP 검색에서 제외됩니다", page_source)
        self.assertIn("st.rerun()", page_source)

    def test_kordoc_unready_still_points_to_fast_upload_and_preserves_official_gate(self) -> None:
        source, module = _source_and_module()
        preprocess_page = _function(module, "_page_preprocess")
        page_source = "\n".join(
            source.splitlines()[preprocess_page.lineno - 1 : preprocess_page.end_lineno]
        )
        upload_condition_start = page_source.index(
            'not _uploaded_file_list(st.session_state.get("regulation_document_upload"))'
        )
        upload_marker_start = page_source.index(
            "_render_beginner_action_marker(",
            upload_condition_start,
        )
        upload_condition = page_source[upload_condition_start:upload_marker_start]

        self.assertNotIn("kordoc_ready", upload_condition)
        self.assertIn("먼저 규정 파일을 선택하세요", page_source)
        self.assertIn("지금은 빠른 구조 전처리로 계속할 수 있습니다.", source)
        self.assertIn("아래 '파일 올리기'에서 규정 파일을 선택하세요.", source)
        self.assertIn("④ 공식 MCP 파일 묶음을 만들기 전에는 Kordoc을 설치", source)
        self.assertIn("같은 원본을 새 초안으로", source)

    def test_multi_scope_blockers_show_names_and_direct_approval_action(self) -> None:
        source, module = _source_and_module()
        connect_page = _function(module, "_page_connect")
        page_source = "\n".join(
            source.splitlines()[connect_page.lineno - 1 : connect_page.end_lineno]
        )
        render_guidance = _function(module, "_render_mcp_bundle_blocking_guidance")
        guidance_source = "\n".join(
            source.splitlines()[render_guidance.lineno - 1 : render_guidance.end_lineno]
        )
        next_button = _function(module, "_render_workflow_next_button")
        next_button_source = "\n".join(
            source.splitlines()[next_button.lineno - 1 : next_button.end_lineno]
        )

        self.assertIn('mcp_scope in {"selected_documents", "selected_institution"}', page_source)
        self.assertIn("blocking_document_ids", page_source)
        self.assertIn("blocking_document_ids[:3]", page_source)
        self.assertIn("blocking_labels=blocking_labels", page_source)
        self.assertIn("navigation_document_id=first_blocking_document_id", page_source)
        self.assertIn("if not _mcp_gate_guidance_items(", page_source)
        self.assertIn('first_blocking_gate = {"reason": "not_ready"}', page_source)
        self.assertIn("먼저 처리할 남은 규정", guidance_source)
        self.assertIn("남은 규정을 검수하고 승인하세요", guidance_source)
        self.assertIn("③ 검수하고 승인으로 이동", guidance_source)
        self.assertIn('st.session_state["document_id"]', next_button_source)
        self.assertIn('navigation_document_id: str = ""', next_button_source)
        self.assertIn("navigation_document_id", next_button_source)

    def test_beginner_copy_describes_ai_review_as_optional_everywhere(self) -> None:
        source, _module = _source_and_module()

        self.assertIn("파서 초안 → (선택) AI 추가 검수 → 사람 승인", source)
        self.assertIn("AI 추가 검수는 직접 선택한 경우에만 실행됩니다.", source)
        self.assertIn("AI 추가 검수를 직접 선택하면 이 설정으로 검수 초안을 만듭니다.", source)
        self.assertIn("AI review is optional.", source)
        self.assertNotIn("모든 문서는 항상 아래 3단계로 처리됩니다", source)
        self.assertNotIn("이어서 AI 검수가 함께 실행됩니다", source)
        self.assertNotIn("AI review draft generation is part of the main preprocessing flow", source)

    def test_mcp_block_reason_is_translated_into_korean_actions(self) -> None:
        source, module = _source_and_module()
        helper_nodes = [
            _function(module, "_mcp_gate_guidance_items"),
            _function(module, "_mcp_bundle_blocking_guidance"),
        ]
        namespace: dict[str, object] = {"NAV_APPROVAL": "③ 검수하고 승인"}
        exec(
            compile(
                ast.Module(body=helper_nodes, type_ignores=[]),
                "<mcp-gate-guidance>",
                "exec",
            ),
            namespace,
        )
        guidance = namespace["_mcp_bundle_blocking_guidance"]

        pending_guidance = guidance(
            {"reason": "no_approved_chunks"},
            pending_review_count=2,
            kordoc_ready=True,
        )
        self.assertEqual("검토가 끝나지 않은 조문이 2개 있습니다.", pending_guidance[0]["cause"])
        self.assertIn("③ 검수하고 승인 화면", pending_guidance[0]["action"])
        self.assertIn("'승인하고 색인' 버튼", pending_guidance[0]["action"])

        index_guidance = guidance(
            {"reason": "document_not_indexed"},
            pending_review_count=0,
            kordoc_ready=False,
        )
        self.assertEqual("승인된 문서가 아직 AI 검색용으로 색인되지 않았습니다.", index_guidance[0]["cause"])
        self.assertIn("'이미 승인된 내용 AI에 등록만 실행'", index_guidance[0]["action"])
        self.assertIn("PDF·HWP·HWPX·DOCX 문서의 Kordoc 표 파싱 품질 증거", index_guidance[1]["cause"])
        self.assertIn("안전 재전처리", index_guidance[1]["action"])
        self.assertNotIn("document_not_indexed", source[source.index("def _render_mcp_bundle_blocking_guidance"):])
        self.assertNotIn("Current gate:", source)
        self.assertIn("③ 검수하고 승인으로 이동", source)

    def test_beginner_progress_translates_internal_messages_and_shows_heartbeat(self) -> None:
        source, module = _source_and_module()
        helper = _function(module, "_beginner_preprocess_stage_text")
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "<guide-progress>", "exec"), namespace)
        stage_text = namespace["_beginner_preprocess_stage_text"]

        self.assertEqual("원본 파일을 안전하게 저장하는 중", stage_text("Saving uploaded file (1/2)"))
        self.assertEqual("문서 내용 분석을 시작하는 중", stage_text("Processing started"))
        self.assertIn("프로그램이 정상적으로 처리 중입니다.", source)
        self.assertIn("**현재 단계:**", source)
        self.assertIn("**경과 시간:**", source)
        self.assertIn("thread.join(timeout=0.7)", source)
        self.assertNotIn("time.sleep(0.7)", source)


if __name__ == "__main__":
    unittest.main()
