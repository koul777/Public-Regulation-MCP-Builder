from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


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


def _function_source(source: str, module: ast.Module, name: str) -> str:
    return ast.get_source_segment(source, _function(module, name)) or ""


def _literal_assignment(module: ast.Module, name: str) -> object:
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"assignment not found: {name}")


def _call_name(call: ast.Call) -> str:
    return ast.unparse(call.func)


def _keyword_name(call: ast.Call, keyword: str) -> str | None:
    value = next((item.value for item in call.keywords if item.arg == keyword), None)
    return ast.unparse(value) if value is not None else None


def _radio_for_key(tree: ast.AST, key_name: str) -> ast.Call:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node) == "st.radio"
        and _keyword_name(node, "key") == key_name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one st.radio with key={key_name}, found {len(matches)}"
        )
    return matches[0]


def _has_session_sync(tree: ast.AST, key_name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_ai_usage_path"
        ):
            continue
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and ast.unparse(target.value) == "st.session_state"
                and ast.unparse(target.slice) == key_name
            ):
                return True
    return False


class StreamlitAiUsagePathContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source, cls.module = _source_and_module()

    def test_qwen_is_the_default_and_both_usage_paths_have_beginner_labels(self) -> None:
        namespace = {
            "st": SimpleNamespace(session_state={}),
            "AI_USAGE_PATH_KEY": "ai_usage_path",
            "AI_USAGE_PATH_QWEN": "qwen",
            "AI_USAGE_PATH_MCP": "mcp",
            "AI_USAGE_PATH_OPTIONS": ("qwen", "mcp"),
        }
        exec(
            compile(
                ast.Module(
                    body=[
                        _function(self.module, "_ai_usage_path"),
                        _function(self.module, "_ai_usage_path_label"),
                    ],
                    type_ignores=[],
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )

        self.assertEqual("qwen", namespace["_ai_usage_path"]())
        namespace["st"].session_state["ai_usage_path"] = "unexpected"
        self.assertEqual("qwen", namespace["_ai_usage_path"]())
        self.assertEqual(
            "이 PC의 로컬 Qwen 챗봇으로 바로 질문",
            namespace["_ai_usage_path_label"]("qwen"),
        )
        self.assertEqual(
            "ChatGPT·Claude·Codex에 MCP로 연결",
            namespace["_ai_usage_path_label"]("mcp"),
        )

    def test_first_screen_and_sidebar_radios_synchronize_the_shared_path_state(self) -> None:
        first_screen = _function(self.module, "_render_beginner_mode_choice")
        first_radio = _radio_for_key(first_screen, "AI_USAGE_PATH_FIRST_WIDGET_KEY")
        sidebar_radio = _radio_for_key(
            self.module, "AI_USAGE_PATH_SIDEBAR_WIDGET_KEY"
        )

        for radio, widget_key in (
            (first_radio, "AI_USAGE_PATH_FIRST_WIDGET_KEY"),
            (sidebar_radio, "AI_USAGE_PATH_SIDEBAR_WIDGET_KEY"),
        ):
            self.assertEqual("AI_USAGE_PATH_OPTIONS", ast.unparse(radio.args[1]))
            self.assertEqual("_ai_usage_path_label", _keyword_name(radio, "format_func"))
            self.assertEqual("_ai_usage_path_changed", _keyword_name(radio, "on_change"))
            self.assertEqual(f"({widget_key},)", _keyword_name(radio, "args"))
            self.assertTrue(_has_session_sync(first_screen if radio is first_radio else self.module, widget_key))

        changed_source = _function_source(
            self.source, self.module, "_ai_usage_path_changed"
        )
        self.assertIn("st.session_state[AI_USAGE_PATH_KEY]", changed_source)
        self.assertIn("AI_USAGE_PATH_QWEN", changed_source)

    def test_connect_menu_label_switches_only_for_the_mcp_path(self) -> None:
        namespace = {
            "AI_USAGE_PATH_MCP": "mcp",
            "NAV_MCP": "④ Qwen 규정 챗봇·AI 연결",
        }
        selected = {"value": "qwen"}
        namespace["_ai_usage_path"] = lambda: selected["value"]
        exec(
            compile(
                ast.Module(
                    body=[_function(self.module, "_connect_nav_display_label")],
                    type_ignores=[],
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )

        self.assertEqual(
            "④ Qwen 규정 챗봇·AI 연결",
            namespace["_connect_nav_display_label"](),
        )
        selected["value"] = "mcp"
        self.assertEqual(
            "④ MCP 생성·외부 AI 연결",
            namespace["_connect_nav_display_label"](),
        )

    def test_beginner_step_four_uses_five_qwen_procedures_or_the_mcp_course(self) -> None:
        qwen_procedures = _literal_assignment(self.module, "BEGINNER_QWEN_PROCEDURES")
        mcp_procedures = _literal_assignment(self.module, "BEGINNER_GUIDE_PROCEDURES")
        selected = {"value": "qwen"}
        namespace = {
            "AI_USAGE_PATH_QWEN": "qwen",
            "BEGINNER_QWEN_PROCEDURES": qwen_procedures,
            "BEGINNER_GUIDE_PROCEDURES": mcp_procedures,
            "_ai_usage_path": lambda: selected["value"],
        }
        exec(
            compile(
                ast.Module(
                    body=[_function(self.module, "_beginner_guide_procedures")],
                    type_ignores=[],
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )

        self.assertEqual(5, len(qwen_procedures))
        self.assertEqual(qwen_procedures, namespace["_beginner_guide_procedures"](4))
        selected["value"] = "mcp"
        self.assertEqual(mcp_procedures[3], namespace["_beginner_guide_procedures"](4))
        self.assertGreater(len(mcp_procedures[3]), len(qwen_procedures))

    def test_connect_page_receives_mcp_first_from_the_selected_usage_path(self) -> None:
        calls = [
            node
            for node in ast.walk(self.module)
            if isinstance(node, ast.Call)
            and _call_name(node) == "_page_connect"
            and any(keyword.arg == "mcp_first" for keyword in node.keywords)
        ]
        self.assertEqual(1, len(calls))
        self.assertEqual(
            "_ai_usage_path() == AI_USAGE_PATH_MCP",
            _keyword_name(calls[0], "mcp_first"),
        )

        page_source = _function_source(self.source, self.module, "_page_connect")
        self.assertIn(
            "st.tabs([mcp_label, chat_label] if mcp_first else [chat_label, mcp_label])",
            page_source,
        )
        self.assertIn("if mcp_first:", page_source)

    def test_qwen_beginner_course_covers_connection_question_and_citation_checks(self) -> None:
        procedures = _literal_assignment(self.module, "BEGINNER_QWEN_PROCEDURES")
        self.assertEqual(
            (
                "승인·색인된 규정 준비 상태 확인",
                "Qwen3 8B 챗봇 켜기",
                "로컬 Qwen 연결 확인",
                "예시 질문 또는 직접 질문 입력",
                "답변과 근거 조문 함께 확인",
            ),
            procedures,
        )

        page_source = _function_source(self.source, self.module, "_page_connect")
        for contract_text in (
            '"Qwen 연결 확인"',
            "#### 예시 질문 — 하나를 누르면 바로 질문합니다",
            "이 규정의 목적과 적용 대상을 알려줘.",
            "답변 아래에 열린 `근거 조문`의 규정명·조문·인용문을 함께 확인합니다.",
        ):
            self.assertIn(contract_text, page_source)

        states = _function(self.module, "_qwen_beginner_procedure_states")
        returns = [node for node in ast.walk(states) if isinstance(node, ast.Return)]
        self.assertEqual(1, len(returns))
        self.assertEqual(
            (
                "approval_ready",
                "qwen_enabled",
                "qwen_connected",
                "question_asked",
                "grounded_answer",
            ),
            tuple(ast.unparse(item) for item in returns[0].value.elts),
        )
        states_source = ast.get_source_segment(self.source, states) or ""
        self.assertIn("_qwen_chat_probe_key(document_id)", states_source)
        self.assertIn('isinstance(message.get("citations"), list)', states_source)
        self.assertIn('bool(message.get("citations"))', states_source)

    def test_qwen_runtime_requires_the_exact_audited_qwen3_8b_model(self) -> None:
        namespace = {
            "Settings": object,
            "DEFAULT_LOCAL_LLM_MODEL": "qwen3:8b",
        }
        exec(
            compile(
                ast.Module(
                    body=[_function(self.module, "_qwen_chat_runtime_enabled")],
                    type_ignores=[],
                ),
                str(APP_PATH),
                "exec",
            ),
            namespace,
        )
        enabled = namespace["_qwen_chat_runtime_enabled"]

        self.assertTrue(
            enabled(SimpleNamespace(rag_llm_backend="ollama", rag_llm_model="qwen3:8b"))
        )
        self.assertFalse(
            enabled(SimpleNamespace(rag_llm_backend="ollama", rag_llm_model="llama3:8b"))
        )
        self.assertFalse(
            enabled(SimpleNamespace(rag_llm_backend="extractive", rag_llm_model="qwen3:8b"))
        )

    def test_profile_mismatch_disables_qwen_probe_examples_and_chat(self) -> None:
        page_source = _function_source(self.source, self.module, "_page_connect")

        self.assertIn(
            "chat_scope_ready = bool(mcp_connection_ready and not mcp_profile_scope_mismatch)",
            page_source,
        )
        self.assertIn("disabled=mcp_profile_scope_mismatch", page_source)
        self.assertIn(
            "disabled=not chat_scope_ready or not qwen_runtime_enabled",
            page_source,
        )
        self.assertIn(
            "if chat_query and chat_scope_ready and qwen_runtime_enabled:",
            page_source,
        )

    def test_qwen_path_is_isolated_from_mcp_beginner_gates(self) -> None:
        page = _function(self.module, "_page_connect")
        page_source = ast.get_source_segment(self.source, page) or ""
        self.assertIn(
            "mcp_beginner_mode = beginner_mode and not qwen_path",
            page_source,
        )
        self.assertIn(
            'st.markdown("### AI 앱에 연결하기" if mcp_beginner_mode else "### MCP client connection")',
            page_source,
        )

        guarded_mcp_course = [
            node
            for node in ast.walk(page)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "mcp_beginner_mode"
            and "MCP가 작동하고 변환되는 원리를 먼저 확인하세요"
            in (ast.get_source_segment(self.source, node) or "")
        ]
        self.assertEqual(1, len(guarded_mcp_course))
        self.assertTrue(
            any(isinstance(node, ast.Return) for node in ast.walk(guarded_mcp_course[0])),
            "the MCP-only beginner gate should remain inside mcp_beginner_mode",
        )


if __name__ == "__main__":
    unittest.main()
