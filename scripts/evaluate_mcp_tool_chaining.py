from __future__ import annotations

"""Measure whether a small local model can drive the MCP tools in-process.

No MCP server is started. Tool calls go through the registered FastMCP tools
with the same argument validation, so a weak function-calling model sees the
tools the way an MCP client would show them. Two calling modes exist:

- ``native``: Ollama ``/api/chat`` with ``tools`` (models with a tool template);
- ``json``: the tool list goes into the system prompt and the model replies
  with one JSON action per turn. This is how a model or API with poor native
  tool calling can still drive the tools.

The report records outcomes, tool-name sequences, counts and token figures.
Questions, answers and tool results are not copied; a question appears only as
a short SHA-256 key. Use a private scenario file for real user questions.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.holdout_spec_guard import refuse_holdout_spec


SCHEMA_VERSION = "mcp-tool-chaining-eval-v1"
EVIDENCE_TOOLS = frozenset({"fetch", "get_regulation_article", "get_article", "get_document", "get_table", "lookup"})
NO_EVIDENCE_REPLY = "승인된 규정에서 근거를 찾지 못했습니다."
NO_EVIDENCE_QUESTION = "우주정거장에 파견된 직원의 화성 체류 수당은 하루 얼마인가요?"
ABSTAIN_MARKERS = (
    "근거를 찾지 못",
    "찾을 수 없",
    "확인되지 않",
    "확인할 수 없",
    "근거가 없",
    "포함되어 있지 않",
    "자료가 없",
    "no evidence",
    "not found",
)
ANSWER_RULES = (
    "Answer in Korean using only text returned by the tools. Call tools until you have the article text. "
    f"If the tools return no matching evidence, reply exactly: {NO_EVIDENCE_REPLY}"
)
JSON_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["call", "answer"]},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "answer": {"type": "string"},
    },
    "required": ["action", "tool", "arguments", "answer"],
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_ERROR_FEEDBACK_CHARS = 300

ToolCaller = Callable[[str, dict[str, Any]], Any]
ChatTransport = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Scenario:
    key: str
    question: str
    kind: str
    expect_regulation_title: str = ""

    @property
    def expect_no_evidence(self) -> bool:
        return self.kind == "no-evidence"


class ModelUnavailable(RuntimeError):
    """The model server or model could not serve the first request."""


def estimate_tokens(text: str) -> int:
    value = str(text or "")
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    return math.ceil(ascii_chars / 4 + (len(value) - ascii_chars))


def _question_key(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ollama_transport(base_url: str, *, timeout_seconds: float) -> ChatTransport:
    """POST to a loopback Ollama /api/chat without proxies or redirects."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The tool-chaining check only talks to a loopback Ollama server.")
    from app.core.local_http import local_urlopen

    endpoint = base_url.rstrip("/") + "/api/chat"

    def send(body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with local_urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    return send


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Read a query spec ({"queries": [...]}) used by the retrieval gates."""
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    refuse_holdout_spec(payload)
    items = payload.get("queries") if isinstance(payload, Mapping) else payload
    scenarios: list[Scenario] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        question = str(item.get("question") or item.get("query") or "").strip()
        if not question:
            continue
        scenarios.append(
            Scenario(
                key=_question_key(question),
                question=question,
                kind="no-evidence" if item.get("expect_no_evidence") is True else "answerable",
                expect_regulation_title=str(item.get("expect_regulation_title") or "").strip(),
            )
        )
    return scenarios


def build_auto_scenarios(call: ToolCaller, *, count: int) -> list[Scenario]:
    """Questions from the built catalog, half naming the article number.

    The other half names only the article title, so the model has to find the
    article through the table of contents or search.
    """
    if count <= 0:
        return []
    catalog = call("list_regulations", {"page": 1, "page_size": 100}) or {}
    regulations = [item for item in catalog.get("regulations") or [] if isinstance(item, Mapping)]
    if len(regulations) > count:
        step = len(regulations) / count
        regulations = [regulations[int(index * step)] for index in range(count)]
    scenarios: list[Scenario] = []
    for index, regulation in enumerate(regulations):
        title = str(regulation.get("regulation_title") or "").strip()
        unit_id = str(regulation.get("regulation_unit_id") or "")
        if not title or not unit_id:
            continue
        toc = call("get_regulation_toc", {"regulation_unit_id": unit_id}) or {}
        node = next(
            (
                item
                for item in toc.get("nodes") or []
                if isinstance(item, Mapping)
                and item.get("node_type") == "article"
                and str(item.get("title") or "").strip()
                and str(item.get("number") or "").strip()
            ),
            None,
        )
        if node is None:
            continue
        number = str(node["number"]).strip()
        article_title = str(node["title"]).strip()
        if index % 2 == 0:
            question = f"{title} {number}({article_title})에는 어떤 내용이 규정되어 있나요?"
        else:
            question = f"{title}에서 {article_title}에 관한 조문 내용을 알려 주세요."
        scenarios.append(
            Scenario(key=_question_key(question), question=question, kind="answerable", expect_regulation_title=title)
        )
    scenarios.append(Scenario(key=_question_key(NO_EVIDENCE_QUESTION), question=NO_EVIDENCE_QUESTION, kind="no-evidence"))
    return scenarios


def _ollama_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def _json_mode_catalog(tools: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "Tools (call one per reply):",
    ]
    for tool in tools:
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required") or [])
        params = []
        for name, param in (schema.get("properties") or {}).items():
            branches = param.get("anyOf") if isinstance(param, Mapping) and param.get("anyOf") else [param]
            kinds = sorted({str(item.get("type")) for item in branches if isinstance(item, Mapping) and item.get("type") not in {None, "null"}})
            params.append(f"{name}{'' if name in required else '?'}: {'|'.join(kinds) or 'any'}")
        lines.append(f"- {tool['name']}({', '.join(params)}): {tool.get('description') or ''}")
    lines.append(
        'Reply with JSON only: {"action":"call","tool":"<name>","arguments":{...},"answer":""} '
        'or {"action":"answer","tool":"","arguments":{},"answer":"<Korean answer>"}.'
    )
    return "\n".join(lines)


def _has_evidence_text(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"text", "verbatim_text"} and isinstance(item, str) and item.strip():
                return True
            if _has_evidence_text(item):
                return True
    elif isinstance(value, list):
        return any(_has_evidence_text(item) for item in value)
    return False


def _is_abstention(answer: str) -> bool:
    text = " ".join(str(answer or "").split()).casefold()
    return not text or any(marker.casefold() in text for marker in ABSTAIN_MARKERS)


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None
    if raw is None:
        return {}
    return None


class _ScenarioRun:
    def __init__(
        self,
        *,
        scenario: Scenario,
        call: ToolCaller,
        tool_names: frozenset[str],
        response_char_budget: int,
    ) -> None:
        self.scenario = scenario
        self.call = call
        self.tool_names = tool_names
        self.response_char_budget = response_char_budget
        self.tool_sequence: list[str] = []
        self.invalid_calls = 0
        self.call_errors = 0
        self.truncated_results = 0
        self.evidence_reached = False

    def execute(self, name: str, raw_arguments: Any) -> str:
        self.tool_sequence.append(name if name in self.tool_names else "<unknown>")
        if name not in self.tool_names:
            self.invalid_calls += 1
            return f"ERROR: unknown tool {name!r}. Use one of: {', '.join(sorted(self.tool_names))}."
        arguments = _parse_arguments(raw_arguments)
        if arguments is None:
            self.invalid_calls += 1
            return "ERROR: arguments must be a JSON object."
        try:
            result = self.call(name, arguments)
        except Exception as exc:  # noqa: BLE001 - the model sees the error, the report counts it
            message = " ".join(str(exc).split())
            if "validation error" in message.casefold():
                self.invalid_calls += 1
            else:
                self.call_errors += 1
            return f"ERROR: {type(exc).__name__}: {message[:_MAX_ERROR_FEEDBACK_CHARS]}"
        text = _compact(result)
        if name in EVIDENCE_TOOLS and _has_evidence_text(result):
            expected = self.scenario.expect_regulation_title
            if not expected or expected in text:
                self.evidence_reached = True
        if len(text) > self.response_char_budget:
            self.truncated_results += 1
            text = text[: self.response_char_budget] + " ...[truncated]"
        return text


def run_scenario(
    *,
    scenario: Scenario,
    send: ChatTransport,
    call: ToolCaller,
    tools: Sequence[Mapping[str, Any]],
    instructions: str,
    model: str,
    mode: str,
    max_steps: int,
    options: Mapping[str, Any],
    think: bool | None,
    response_char_budget: int,
    first_request: bool = False,
) -> dict[str, Any]:
    run = _ScenarioRun(
        scenario=scenario,
        call=call,
        tool_names=frozenset(str(tool["name"]) for tool in tools),
        response_char_budget=response_char_budget,
    )
    system = f"{instructions}\n\n{ANSWER_RULES}"
    if mode == "json":
        system = f"{system}\n\n{_json_mode_catalog(tools)}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": scenario.question},
    ]
    native_tools = _ollama_tools(tools) if mode == "native" else None
    tools_tokens = estimate_tokens(_compact(native_tools)) if native_tools else 0
    prompt_eval_total = 0
    eval_total = 0
    peak_context = 0
    model_requests = 0
    answer = ""
    outcome = "max-steps"
    started = time.perf_counter()
    for step in range(1, max_steps + 1):
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "options": dict(options)}
        if native_tools is not None:
            body["tools"] = native_tools
        else:
            body["format"] = JSON_ACTION_SCHEMA
        if think is not None:
            body["think"] = think
        peak_context = max(peak_context, tools_tokens + estimate_tokens(_compact(messages)))
        model_requests += 1
        try:
            response = send(body)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            if first_request and step == 1:
                raise ModelUnavailable(_unavailable_reason(exc)) from exc
            outcome = "model-error"
            break
        prompt_eval_total += int(response.get("prompt_eval_count") or 0)
        eval_total += int(response.get("eval_count") or 0)
        message = response.get("message") if isinstance(response.get("message"), Mapping) else {}
        if mode == "native":
            tool_calls = [item for item in message.get("tool_calls") or [] if isinstance(item, Mapping)]
            if not tool_calls:
                answer = str(message.get("content") or "")
                outcome = "answered"
                break
            messages.append(
                {"role": "assistant", "content": str(message.get("content") or ""), "tool_calls": tool_calls}
            )
            for tool_call in tool_calls:
                function = tool_call.get("function") if isinstance(tool_call.get("function"), Mapping) else {}
                name = str(function.get("name") or "")
                result = run.execute(name, function.get("arguments"))
                messages.append({"role": "tool", "content": result, "tool_name": name})
        else:
            content = str(message.get("content") or "")
            try:
                action = json.loads(content)
            except json.JSONDecodeError:
                action = None
            if not isinstance(action, Mapping) or action.get("action") not in {"call", "answer"}:
                run.invalid_calls += 1
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "ERROR: reply with one JSON action object."})
                continue
            if action["action"] == "answer":
                answer = str(action.get("answer") or "")
                outcome = "answered"
                break
            name = str(action.get("tool") or "")
            result = run.execute(name, action.get("arguments"))
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"TOOL RESULT {name}:\n{result}"})
    abstained = outcome == "answered" and _is_abstention(answer)
    if scenario.expect_no_evidence:
        passed = abstained
    else:
        passed = outcome == "answered" and run.evidence_reached and not abstained
    return {
        "scenario_key": scenario.key,
        "kind": scenario.kind,
        "outcome": outcome,
        "passed": passed,
        "abstained": abstained,
        "evidence_reached": run.evidence_reached,
        "model_requests": model_requests,
        "tool_calls": len(run.tool_sequence),
        "tool_sequence": run.tool_sequence,
        "invalid_calls": run.invalid_calls,
        "call_errors": run.call_errors,
        "truncated_results": run.truncated_results,
        "answer_chars": len(answer),
        "peak_context_tokens_estimate": peak_context,
        "ollama_prompt_eval_count": prompt_eval_total,
        "ollama_eval_count": eval_total,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _unavailable_reason(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace").casefold()
        except OSError:
            detail = ""
        if "does not support tools" in detail:
            return "tools-not-supported"
        if exc.code == 404 or "not found" in detail:
            return "model-not-found"
        return f"http-{exc.code}"
    if isinstance(exc, URLError):
        return "ollama-unreachable"
    return type(exc).__name__


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    answerable = [item for item in results if item["kind"] == "answerable"]
    no_evidence = [item for item in results if item["kind"] == "no-evidence"]
    total_calls = sum(int(item["tool_calls"]) for item in results)
    invalid = sum(int(item["invalid_calls"]) for item in results)
    return {
        "scenario_count": len(results),
        "answerable_count": len(answerable),
        "no_evidence_count": len(no_evidence),
        "chain_success_rate": _rate(sum(1 for item in answerable if item["passed"]), len(answerable)),
        "false_abstention_rate": _rate(sum(1 for item in answerable if item["abstained"]), len(answerable)),
        "evidence_reached_rate": _rate(sum(1 for item in answerable if item["evidence_reached"]), len(answerable)),
        "no_evidence_abstention_rate": _rate(sum(1 for item in no_evidence if item["abstained"]), len(no_evidence)),
        "invalid_call_rate": _rate(invalid, total_calls) if total_calls else 0.0,
        "call_error_count": sum(int(item["call_errors"]) for item in results),
        "mean_tool_calls": round(total_calls / len(results), 2) if results else None,
        "max_prompt_tokens": max((int(item["peak_context_tokens_estimate"]) for item in results), default=0),
        "ollama_prompt_eval_count_total": sum(int(item["ollama_prompt_eval_count"]) for item in results),
        "ollama_eval_count_total": sum(int(item["ollama_eval_count"]) for item in results),
        "outcomes": {
            outcome: sum(1 for item in results if item["outcome"] == outcome)
            for outcome in sorted({str(item["outcome"]) for item in results})
        },
    }


def evaluate_tool_chaining(
    *,
    call: ToolCaller,
    tools: Sequence[Mapping[str, Any]],
    instructions: str,
    model: str,
    ollama_url: str = "http://127.0.0.1:11434",
    mode: str = "native",
    scenario_path: str | Path | None = None,
    auto_scenarios: int = 6,
    max_steps: int = 6,
    num_ctx: int = 8192,
    think: bool | None = None,
    timeout_seconds: float = 180.0,
    response_char_budget: int = 8_000,
    thresholds: Mapping[str, Any] | None = None,
    transport: ChatTransport | None = None,
) -> dict[str, Any]:
    if mode not in {"native", "json"}:
        raise ValueError("mode must be native or json.")
    send = transport or ollama_transport(ollama_url, timeout_seconds=timeout_seconds)
    scenarios = load_scenarios(scenario_path) if scenario_path else []
    if not scenarios:
        scenarios = build_auto_scenarios(call, count=auto_scenarios)
    options = {"temperature": 0, "seed": 7, "num_ctx": int(num_ctx)}
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "mode": mode,
        "scenario_source": "private-spec" if scenario_path else "auto-catalog",
        "max_steps": max_steps,
        "num_ctx": num_ctx,
        "thresholds": {key: value for key, value in dict(thresholds or {}).items() if value is not None},
    }
    results: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios):
        try:
            results.append(
                run_scenario(
                    scenario=scenario,
                    send=send,
                    call=call,
                    tools=tools,
                    instructions=instructions,
                    model=model,
                    mode=mode,
                    max_steps=max_steps,
                    options=options,
                    think=think,
                    response_char_budget=response_char_budget,
                    first_request=index == 0,
                )
            )
        except ModelUnavailable as exc:
            return {**report, "available": False, "error_type": str(exc), "summary": {}, "scenarios": []}
    return {**report, "available": True, "summary": summarize(results), "scenarios": results}
