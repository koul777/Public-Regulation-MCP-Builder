from __future__ import annotations

import ast
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


def setup_app_source() -> str:
    source = (Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
    names = {"_link_button", "_render_kordoc_install_feedback", "_render_kordoc_preprocess_preflight"}
    functions = "\n\n".join(
        ast.get_source_segment(source, node) for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    )
    return '''
import inspect
from typing import Any
from types import SimpleNamespace
import streamlit as st
from app.services.operator_setup_service import kordoc_installer_guidance
settings = SimpleNamespace(kordoc_table_command="kordoc")
shutil = SimpleNamespace(which=lambda name: "npm" if st.session_state.get("npm", True) else None)
def kordoc_table_command_status(command):
    return {"available": st.session_state.get("available", False), "label": "kordoc", "version": "synthetic", "reason": st.session_state.get("reason", "")}
def clear():
    st.session_state["checks"] = st.session_state.get("checks", 0) + 1
kordoc_table_command_status.cache_clear = clear
def _render_beginner_action_marker(*args, **kwargs):
    pass
def _application_restart_instruction():
    return "앱을 다시 실행하세요."
def _run_kordoc_installer():
    st.session_state["installs"] = st.session_state.get("installs", 0) + 1
    return {"ok": st.session_state["installs"] > 1, "error": "installer_timeout", "output": "synthetic-secret"}
''' + functions + '''
_render_kordoc_preprocess_preflight()
st.checkbox("다른 작업")
'''


class StreamlitSetupTests(unittest.TestCase):
    def test_custom_command_failure_and_unknown_version_have_distinct_recovery(self) -> None:
        app = AppTest.from_string(setup_app_source())
        app.session_state["reason"] = "version_probe_failed"
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("전체 명령 실행에 실패" in item.value for item in app.info))
        self.assertFalse(any("Kordoc 사용 가능" in item.value for item in app.caption))
        self.assertNotIn("installs", app.session_state)
        app.session_state["available"] = True
        app.session_state["reason"] = "version_unrecognized"
        app.button(key="preprocess-kordoc-recheck").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("버전 표기를 확인하지 못했습니다" in item.value for item in app.info))
        self.assertTrue(any("문서별 표 파싱 결과" in item.value for item in app.caption))
        self.assertNotIn("installs", app.session_state)

    def test_failure_survives_rerun_and_retry_uses_safe_action_copy(self) -> None:
        app = AppTest.from_string(setup_app_source()).run()
        self.assertFalse(app.exception)
        self.assertNotIn("installs", app.session_state)
        app.button(key="preprocess-kordoc-install-run").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("시간" in item.value for item in app.error))
        self.assertTrue(any("인터넷" in item.value for item in app.info))
        self.assertEqual(0, len(app.code))
        app.checkbox[0].check().run()
        self.assertEqual(1, app.session_state["installs"])
        self.assertTrue(any("시간" in item.value for item in app.error))
        self.assertIn("다시 시도", app.button(key="preprocess-kordoc-install-run").label)
        app.button(key="preprocess-kordoc-install-run").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(2, app.session_state["installs"])
        self.assertTrue(any("완료" in item.value for item in app.success))
        app.session_state["available"] = True
        app.button(key="preprocess-kordoc-recheck").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(2, app.session_state["installs"])
        self.assertTrue(any("Kordoc 사용 가능" in item.value for item in app.caption))
        self.assertNotIn("preprocess-kordoc-install-result", app.session_state)

    def test_missing_npm_disables_install_but_allows_recheck(self) -> None:
        app = AppTest.from_string(setup_app_source())
        app.session_state["npm"] = False
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="preprocess-kordoc-install-run").disabled)
        self.assertFalse(app.button(key="preprocess-kordoc-recheck").disabled)
        self.assertTrue(any("빠른 구조 전처리로 계속" in item.value for item in app.info))
        self.assertNotIn("installs", app.session_state)
