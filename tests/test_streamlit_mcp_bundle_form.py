from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings, clear_runtime_settings_overrides, set_runtime_settings_overrides
from app.storage.repository import JsonRepository
from tests.test_streamlit_approval_app import (
    AppTest,
    REPO_ROOT,
    _seed_app_institution_context,
    _seed_streamlit_approval_document,
)


DOCUMENT_ID = "doc_streamlit_approval"
BUNDLE_STATE_KEY = f"mcp_setup_bundle_written:current_document:{DOCUMENT_ID}"


@unittest.skipIf(AppTest is None, "streamlit.testing.v1.AppTest is not available")
class StreamlitMcpBundleFormTests(unittest.TestCase):
    """Exercise the real operator form without writing a bundle or publishing."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(data_dir=self.root / "data", artifact_root=self.root)
        _seed_streamlit_approval_document(self.settings)
        self.repository = JsonRepository(self.settings)
        chunks = self.repository.get_chunks(DOCUMENT_ID)
        chunks[0].approval_status = "approved"
        chunks[0].approval_id = "synthetic-form-approval"
        chunks[0].metadata.update({
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_inventory": {"parser": "kordoc", "status": "parsed", "table_count": 1},
        })
        self.repository.save_chunks(DOCUMENT_ID, chunks)
        set_runtime_settings_overrides(data_dir=self.settings.data_dir, artifact_root=self.settings.artifact_root)
        self.addCleanup(clear_runtime_settings_overrides)
        self.index_status = {
            "indexing_status": "indexed",
            "vector_summary": {"record_count": 1},
            "vector_consistency": {"stale_count": 0},
        }
        index_patcher = patch("app.api.routes_documents.get_index_status", side_effect=lambda *_a, **_kw: self.index_status)
        index_patcher.start()
        self.addCleanup(index_patcher.stop)
        self.writers = []
        for name in ("write_mcp_runtime_data_bundle", "write_mcp_setup_bundle", "write_mcp_setup_bundle_zip"):
            patcher = patch(
                f"scripts.generate_mcp_client_config.{name}",
                side_effect=AssertionError("A blocked form must not invoke a bundle writer"),
            )
            self.writers.append(patcher.start())
            self.addCleanup(patcher.stop)

    def _app(self, *, name: str = "synthetic-mcp", save_mode: str = "folder-only"):
        app = AppTest.from_file(str(REPO_ROOT / "frontend" / "streamlit_app.py"), default_timeout=30)
        _seed_app_institution_context(app)
        state = app.session_state
        state["document_id"] = DOCUMENT_ID
        state["nav_page"] = "④ Qwen 규정 챗봇·AI 연결"
        state["ai_usage_path"] = "mcp"
        state["beginner_guide_enabled"] = False
        state["beginner_guide_choice_made"] = True
        state["ai_connection_overrides"] = {"data_dir": self.settings.data_dir, "artifact_root": self.root}
        state[f"mcp-data-scope-{DOCUMENT_ID}"] = "current_document"
        state[f"mcp-connection-target-{DOCUMENT_ID}"] = "chatgpt-remote"
        state[f"mcp-bundle-dir-{DOCUMENT_ID}"] = str(self.root / "synthetic-bundle")
        state[f"mcp-save-mode-{DOCUMENT_ID}"] = save_mode
        state[f"mcp-server-name-{DOCUMENT_ID}"] = name
        # An existing completion record must survive an invalid submission.
        # It intentionally has no usable runtime, so the actual form is rendered.
        state[BUNDLE_STATE_KEY] = {"document_id": DOCUMENT_ID, "sentinel": "preserve-existing-proof"}
        app.run()
        self.assertFalse(app.exception, list(app.exception))
        return app

    def _assert_blocked_submission(self, app, expected_reason: str) -> None:
        submit = app.button(key=f"write-mcp-bundle-{DOCUMENT_ID}")
        self.assertFalse(submit.disabled)
        near_button = [w.value for w in app.warning if "파일 묶음을 만들기 전에 확인할 항목" in w.value]
        self.assertTrue(any(expected_reason in text for text in near_button), near_button)
        before = dict(app.session_state[BUNDLE_STATE_KEY])
        submit.click().run()
        self.assertFalse(app.exception, list(app.exception))
        self.assertTrue(any("아직 파일 묶음을 만들 수 없습니다" in error.value for error in app.error))
        self.assertEqual(before, app.session_state[BUNDLE_STATE_KEY])
        self.assertFalse((self.root / "synthetic-bundle").exists())
        for writer in self.writers:
            writer.assert_not_called()

    def test_blank_name_is_actionable_and_does_not_clear_previous_completion(self) -> None:
        app = self._app(name="")
        self._assert_blocked_submission(app, "MCP 이름")
        app.button(key=f"mcp-use-suggested-name-{DOCUMENT_ID}").click().run()
        self.assertFalse(app.exception, list(app.exception))
        self.assertTrue(app.text_input(key=f"mcp-server-name-{DOCUMENT_ID}").value)
        self.assertFalse(any("MCP 이름" in w.value for w in app.warning if "파일 묶음을 만들기 전에 확인할 항목" in w.value))
        for writer in self.writers:
            writer.assert_not_called()

    def test_invalid_name_is_reported_at_submit_without_export(self) -> None:
        app = self._app(name="잘못된 MCP 이름")
        self._assert_blocked_submission(app, "MCP 이름")

    def test_unapproved_document_cannot_export_with_clickable_button(self) -> None:
        chunks = self.repository.get_chunks(DOCUMENT_ID)
        chunks[0].approval_status = "draft"
        self.repository.save_chunks(DOCUMENT_ID, chunks)
        self._assert_blocked_submission(self._app(), "검토·승인·색인")

    def test_unindexed_document_cannot_export_with_clickable_button(self) -> None:
        self.index_status["indexing_status"] = "not_indexed"
        self._assert_blocked_submission(self._app(), "검토·승인·색인")

    def test_missing_kordoc_evidence_cannot_export_with_clickable_button(self) -> None:
        chunks = self.repository.get_chunks(DOCUMENT_ID)
        chunks[0].metadata.pop("kordoc_table_parser_status")
        chunks[0].metadata.pop("kordoc_table_inventory")
        self.repository.save_chunks(DOCUMENT_ID, chunks)
        self._assert_blocked_submission(self._app(), "표 파싱 품질")

    def test_legacy_zip_choice_has_visible_one_click_remote_recovery(self) -> None:
        original_is_file = Path.is_file

        def without_application_wheel(path: Path) -> bool:
            if path.name.startswith("reg_rag_preprocessor-") and path.suffix == ".whl":
                return False
            return original_is_file(path)

        with patch.object(Path, "is_file", without_application_wheel):
            app = self._app(save_mode="folder-and-zip")
            self._assert_blocked_submission(app, "ZIP")
            app.button(key=f"mcp-bundle-use-folder-only-{DOCUMENT_ID}").click().run()
            self.assertFalse(app.exception, list(app.exception))
            self.assertEqual("folder-only", app.radio(key=f"mcp-save-mode-{DOCUMENT_ID}").value)
            self.assertFalse(any("파일 묶음을 만들기 전에 확인할 항목" in w.value for w in app.warning))
            self.assertEqual("preserve-existing-proof", app.session_state[BUNDLE_STATE_KEY]["sentinel"])
            for writer in self.writers:
                writer.assert_not_called()


class StreamlitMcpBundleQaGateStageTests(unittest.TestCase):
    def test_final_build_qa_gate_runs_on_the_written_bundle_before_zip_and_completion(self) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
        setup = source.index("lambda _report: write_mcp_setup_bundle(")
        gate = source.index("lambda _report: run_mcp_build_qa_gate(")
        refusal = source.index('if not qa_gate_report.get("passed"):')
        zip_write = source.index("zip_path, zip_fallback_used = _write_operator_mcp_bundle_zip(")
        completion = source.index('"qa_gate": {')

        self.assertLess(setup, gate)
        self.assertLess(gate, refusal)
        self.assertLess(refusal, zip_write)
        self.assertLess(zip_write, completion)
        gate_call = source[gate:refusal]
        self.assertIn("data_dir=mcp_runtime_data_dir", gate_call)
        self.assertIn("bundle_dir=mcp_bundle_output_dir", gate_call)
        self.assertIn("profile_id=selected_profile_id", gate_call)
        self.assertIn("raise RuntimeError(", source[refusal:zip_write])
        self.assertIn("최종 빌드 QA 게이트를 통과하지 못해", source[refusal:zip_write])


if __name__ == "__main__":
    unittest.main()
