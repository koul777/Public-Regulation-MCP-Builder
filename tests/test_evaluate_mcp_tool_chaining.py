from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from scripts import evaluate_mcp_tool_chaining as chaining
from scripts.holdout_spec_guard import HoldoutSpecRefused


TOOLS = [
    {
        "name": "search",
        "description": "Search approved regulations; then call fetch with a returned id.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "fetch",
        "description": "Fetch approved evidence for an id returned by search.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
]
TITLE = "합성 복무규정"


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "list_regulations":
            return {"regulations": [{"regulation_unit_id": "unit-a", "regulation_title": TITLE}]}
        if name == "get_regulation_toc":
            return {"nodes": [{"node_type": "article", "number": "제1조", "title": "목적", "label": "제1조 목적"}]}
        if name == "search":
            if not str(arguments.get("query") or "").strip():
                raise ValueError("1 validation error for search query")
            return {"results": [{"id": "result-1", "title": f"{TITLE} 제1조"}]}
        if name == "fetch":
            return {"id": arguments["id"], "title": f"{TITLE} 제1조", "text": "이 규정은 합성 목적을 정한다."}
        raise AssertionError(name)


def _native_reply(*, calls=None, content="", prompt=100, generated=10) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"function": {"name": name, "arguments": args}} for name, args in calls]
    return {"message": message, "prompt_eval_count": prompt, "eval_count": generated}


class _ScriptedModel:
    """Replays model replies per scenario; keyed by whether the question names the article."""

    def __init__(self, replies_by_kind: dict[str, list[dict]]) -> None:
        self.replies_by_kind = {key: list(value) for key, value in replies_by_kind.items()}
        self.bodies: list[dict] = []

    def __call__(self, body: dict) -> dict:
        self.bodies.append(copy.deepcopy(body))
        question = body["messages"][1]["content"]
        kind = "no-evidence" if question == chaining.NO_EVIDENCE_QUESTION else (
            "with-number" if "제1조" in question else "title-only"
        )
        return self.replies_by_kind[kind].pop(0)


def _good_chain() -> list[dict]:
    return [
        _native_reply(calls=[("search", {"query": TITLE})]),
        _native_reply(calls=[("fetch", {"id": "result-1"})]),
        _native_reply(content="제1조는 이 규정의 합성 목적을 정합니다."),
    ]


class ToolChainingEvaluationTests(unittest.TestCase):
    def test_native_mode_scores_chain_success_abstention_and_tokens(self) -> None:
        runtime = _Runtime()
        model = _ScriptedModel(
            {
                "with-number": _good_chain(),
                "no-evidence": [
                    _native_reply(calls=[("search", {"query": "화성 체류 수당"})]),
                    _native_reply(content=chaining.NO_EVIDENCE_REPLY),
                ],
            }
        )
        report = chaining.evaluate_tool_chaining(
            call=runtime,
            tools=TOOLS,
            instructions="Use search, then fetch.",
            model="synthetic-e4b",
            auto_scenarios=1,
            thresholds={"min_chain_success_rate": 0.8, "max_prompt_tokens": None},
            transport=model,
        )
        self.assertTrue(report["available"])
        summary = report["summary"]
        self.assertEqual(1.0, summary["chain_success_rate"])
        self.assertEqual(0.0, summary["false_abstention_rate"])
        self.assertEqual(1.0, summary["no_evidence_abstention_rate"])
        self.assertEqual(0.0, summary["invalid_call_rate"])
        self.assertEqual(500, summary["ollama_prompt_eval_count_total"])
        self.assertGreater(summary["max_prompt_tokens"], 0)
        self.assertEqual({"min_chain_success_rate": 0.8}, report["thresholds"])
        self.assertEqual(["search", "fetch"], report["scenarios"][0]["tool_sequence"])
        first = model.bodies[0]
        self.assertEqual(["search", "fetch"], [tool["function"]["name"] for tool in first["tools"]])
        self.assertEqual({"temperature": 0, "seed": 7, "num_ctx": 8192}, first["options"])
        self.assertNotIn("think", first)
        tool_message = model.bodies[1]["messages"][-1]
        self.assertEqual(("tool", "search"), (tool_message["role"], tool_message["tool_name"]))

    def test_saying_no_evidence_when_evidence_exists_is_a_false_abstention(self) -> None:
        model = _ScriptedModel(
            {
                "with-number": [_native_reply(content="해당 자료가 없습니다.")],
                "no-evidence": [_native_reply(content=chaining.NO_EVIDENCE_REPLY)],
            }
        )
        report = chaining.evaluate_tool_chaining(
            call=_Runtime(), tools=TOOLS, instructions="", model="m", auto_scenarios=1, transport=model
        )
        self.assertEqual(0.0, report["summary"]["chain_success_rate"])
        self.assertEqual(1.0, report["summary"]["false_abstention_rate"])
        self.assertEqual(0.0, report["summary"]["evidence_reached_rate"])

    def test_unknown_tools_bad_arguments_and_validation_errors_count_as_invalid(self) -> None:
        model = _ScriptedModel(
            {
                "with-number": [
                    _native_reply(calls=[("search_regulations", {"query": TITLE})]),
                    _native_reply(calls=[("search", "not json")]),
                    _native_reply(calls=[("search", {"query": " "})]),
                    *_good_chain(),
                ],
                "no-evidence": [_native_reply(content=chaining.NO_EVIDENCE_REPLY)],
            }
        )
        report = chaining.evaluate_tool_chaining(
            call=_Runtime(), tools=TOOLS, instructions="", model="m", auto_scenarios=1, max_steps=8, transport=model
        )
        scenario = report["scenarios"][0]
        self.assertTrue(scenario["passed"])
        self.assertEqual(3, scenario["invalid_calls"])
        self.assertEqual(["<unknown>", "search", "search", "search", "fetch"], scenario["tool_sequence"])
        self.assertEqual(0.6, report["summary"]["invalid_call_rate"])

    def test_json_mode_drives_tools_without_native_tool_calling(self) -> None:
        def action(kind: str, tool: str = "", arguments=None, answer: str = "") -> dict:
            payload = {"action": kind, "tool": tool, "arguments": arguments or {}, "answer": answer}
            return {"message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}}

        model = _ScriptedModel(
            {
                "with-number": [
                    {"message": {"content": "not json"}},
                    action("call", "search", {"query": TITLE}),
                    action("call", "fetch", {"id": "result-1"}),
                    action("answer", answer="제1조는 합성 목적을 정합니다."),
                ],
                "no-evidence": [action("answer", answer=chaining.NO_EVIDENCE_REPLY)],
            }
        )
        report = chaining.evaluate_tool_chaining(
            call=_Runtime(), tools=TOOLS, instructions="", model="m", mode="json", auto_scenarios=1, transport=model
        )
        self.assertEqual(1.0, report["summary"]["chain_success_rate"])
        self.assertEqual(1, report["scenarios"][0]["invalid_calls"])
        body = model.bodies[0]
        self.assertNotIn("tools", body)
        self.assertEqual(chaining.JSON_ACTION_SCHEMA, body["format"])
        self.assertIn("- search(query: string)", body["messages"][0]["content"])
        self.assertEqual("TOOL RESULT search:", model.bodies[2]["messages"][-1]["content"].splitlines()[0])

    def test_second_auto_question_names_only_the_article_title(self) -> None:
        class _TwoRegulations(_Runtime):
            def __call__(self, name, arguments):
                if name == "list_regulations":
                    return {
                        "regulations": [
                            {"regulation_unit_id": "unit-a", "regulation_title": TITLE},
                            {"regulation_unit_id": "unit-b", "regulation_title": "합성 여비규정"},
                        ]
                    }
                return super().__call__(name, arguments)

        scenarios = chaining.build_auto_scenarios(_TwoRegulations(), count=2)
        self.assertEqual(["answerable", "answerable", "no-evidence"], [item.kind for item in scenarios])
        self.assertIn("제1조(목적)", scenarios[0].question)
        self.assertNotIn("제1조", scenarios[1].question)
        self.assertEqual("합성 여비규정", scenarios[1].expect_regulation_title)

    def test_report_keeps_question_and_answer_text_out(self) -> None:
        model = _ScriptedModel({"with-number": _good_chain(), "no-evidence": [_native_reply(content=chaining.NO_EVIDENCE_REPLY)]})
        report = chaining.evaluate_tool_chaining(
            call=_Runtime(), tools=TOOLS, instructions="", model="m", auto_scenarios=1, transport=model
        )
        rendered = json.dumps(report, ensure_ascii=False)
        for private in (TITLE, "합성 목적", "result-1", chaining.NO_EVIDENCE_QUESTION):
            self.assertNotIn(private, rendered)
        self.assertEqual(16, len(report["scenarios"][0]["scenario_key"]))

    def test_unavailable_model_or_server_is_reported(self) -> None:
        def missing_model(_body):
            raise HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}'))

        def no_tools(_body):
            raise HTTPError(
                "http://127.0.0.1:11434/api/chat", 400, "Bad Request", {}, io.BytesIO(b'{"error":"m does not support tools"}')
            )

        def unreachable(_body):
            raise URLError("refused")

        for transport, expected in ((missing_model, "model-not-found"), (no_tools, "tools-not-supported"), (unreachable, "ollama-unreachable")):
            with self.subTest(expected=expected):
                report = chaining.evaluate_tool_chaining(
                    call=_Runtime(), tools=TOOLS, instructions="", model="m", auto_scenarios=1, transport=transport
                )
                self.assertFalse(report["available"])
                self.assertEqual(expected, report["error_type"])

    def test_only_loopback_ollama_is_allowed(self) -> None:
        with self.assertRaises(ValueError):
            chaining.ollama_transport("http://192.0.2.10:11434", timeout_seconds=1)
        chaining.ollama_transport("http://127.0.0.1:11434", timeout_seconds=1)

    def test_private_spec_uses_query_spec_format_and_refuses_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "queries": [
                            {"id": "q1", "query": "합성 질문", "expect_regulation_title": TITLE},
                            {"id": "q2", "question": "없는 질문", "expect_no_evidence": True},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            scenarios = chaining.load_scenarios(spec)
            self.assertEqual(["answerable", "no-evidence"], [item.kind for item in scenarios])
            self.assertEqual(TITLE, scenarios[0].expect_regulation_title)

            spec.write_text(json.dumps({"split": "holdout", "queries": [{"query": "x"}]}), encoding="utf-8")
            with self.assertRaises(HoldoutSpecRefused):
                chaining.load_scenarios(spec)


if __name__ == "__main__":
    unittest.main()
