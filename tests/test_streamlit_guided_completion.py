from __future__ import annotations

import ast
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


APP_PATH = Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py"


class _Rerun(BaseException):
    pass


class StreamlitGuidedCompletionTests(unittest.TestCase):
    def _launcher(self, *, clicked: bool, healthy: bool, running: bool = True, existing: bool = False):
        module = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_render_standalone_qwen_chat_launcher")
        state = {"url": "http://127.0.0.1:8502", "_process": SimpleNamespace(poll=lambda: None if running else 1)}
        status = Mock()
        st = Mock()
        st.session_state = {"launch": state} if existing else {}
        st.button.return_value = clicked
        st.empty.return_value.container.side_effect = lambda: nullcontext()
        st.container.side_effect = lambda **kwargs: nullcontext()
        st.status.side_effect = lambda *args, **kwargs: nullcontext(status)
        st.rerun.side_effect = _Rerun

        def launch(_settings):
            st.session_state["launch"] = state
            return state

        marker = Mock()
        namespace = {"st": st, "time": SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 21]), sleep=Mock()),
                     "get_settings": lambda: None, "_launch_standalone_qwen_chat": launch,
                     "_standalone_qwen_chat_is_healthy": lambda _url: healthy,
                     "QWEN_CHAT_APP_LAUNCH_STATE_KEY": "launch",
                     "_safe_ui_error": str, "_render_beginner_action_marker": marker}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(APP_PATH), "exec"), namespace)
        return namespace[function.name], st, marker

    def test_successful_launch_refreshes_journey_without_second_click(self) -> None:
        launcher, st, _ = self._launcher(clicked=True, healthy=True)
        with self.assertRaises(_Rerun):
            launcher(key="launch-button")
        st.rerun.assert_called_once()

    def test_launch_failure_and_timeout_do_not_mark_handoff_complete(self) -> None:
        for running in (False, True):
            with self.subTest(running=running):
                launcher, st, marker = self._launcher(clicked=True, healthy=False, running=running)
                launcher(key="launch-button")
                st.rerun.assert_not_called()
                st.link_button.assert_not_called()
                self.assertEqual(("launch-button",), marker.call_args.kwargs["control_keys"])

    def test_guide_switches_from_launch_button_to_healthy_app_link(self) -> None:
        for existing in (False, True):
            launcher, st, marker = self._launcher(clicked=False, healthy=True, existing=existing)
            launcher(key="launch-button")
            target = "launch-button-open" if existing else "launch-button"
            self.assertEqual((target,), marker.call_args.kwargs["control_keys"])
            self.assertEqual(existing, st.link_button.called)
            st.rerun.assert_not_called()


if __name__ == "__main__":
    unittest.main()
