from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.agents.ollama_runtime import OllamaRuntime, OllamaRuntimeError

from tests.test_local_http import (
    LOOPBACK_HOST,
    LOOPBACK_URL,
    PROXY_ENV,
    _FakeResponse,
    _fake_http,
    _redirect,
)


class OllamaRuntimeLocalTransportTests(unittest.TestCase):
    """Ollama 런타임 점검 호출은 프록시를 거치지 않고 리다이렉트를 따르지 않는다."""

    def test_ollama_runtime_bypasses_proxy(self) -> None:
        body = b'{"models": [{"name": "synthetic:latest"}]}'
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse(body)) as seen:
            names = OllamaRuntime(LOOPBACK_URL).installed_models(timeout_seconds=3)

        self.assertEqual({"synthetic:latest"}, names)
        self.assertEqual([LOOPBACK_HOST], [item["host"] for item in seen])
        self.assertEqual(["/api/tags"], [item["selector"] for item in seen])

    def test_ollama_runtime_reports_redirect_as_runtime_error(self) -> None:
        with _fake_http(_redirect(302)) as seen:
            with self.assertRaisesRegex(OllamaRuntimeError, "HTTP 302"):
                OllamaRuntime(LOOPBACK_URL).installed_models(timeout_seconds=3)

        self.assertEqual(1, len(seen))


class OllamaRuntimeContextWindowTests(unittest.TestCase):
    def test_window_is_explicit_and_does_not_change_default_requests(self) -> None:
        runtime = OllamaRuntime(LOOPBACK_URL)
        with patch.object(runtime, "_request_json", return_value={"response": "synthetic answer"}) as request:
            runtime.generate(model="synthetic-model", prompt="synthetic prompt", timeout_seconds=3)
            self.assertNotIn("num_ctx", request.call_args.args[2]["options"])
            runtime.generate(model="synthetic-model", prompt="synthetic prompt", timeout_seconds=3,
                             context_window=8192)
            payload = request.call_args.args[2]
        self.assertEqual(8192, payload["options"]["num_ctx"])
        self.assertEqual(0, payload["options"]["seed"])
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])

    def test_invalid_context_window_fails_before_transport(self) -> None:
        runtime = OllamaRuntime(LOOPBACK_URL)
        for value in (True, 0, 511, 32769, 8192.0, "8192"):
            with self.subTest(value=value), patch.object(runtime, "_request_json") as request:
                with self.assertRaisesRegex(ValueError, "context_window"):
                    runtime.generate(model="synthetic-model", prompt="synthetic prompt",
                                     timeout_seconds=3, context_window=value)
                request.assert_not_called()

    def test_schema_grammar_retry_preserves_explicit_window_and_prompt(self) -> None:
        runtime = OllamaRuntime(LOOPBACK_URL)
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        with patch.object(runtime, "_request_json", side_effect=[
            OllamaRuntimeError("failed to parse grammar"), {"response": '{"answer": "synthetic answer"}'},
        ]) as request:
            payload, _ = runtime.generate_json(model="synthetic-model", prompt="synthetic prompt",
                                                schema=schema, timeout_seconds=3, context_window=8192)
        self.assertEqual("synthetic answer", payload["answer"])
        first, second = [call.args[2] for call in request.call_args_list]
        self.assertEqual(schema, first["format"])
        self.assertEqual("json", second["format"])
        self.assertEqual(first["options"], second["options"])
        self.assertEqual(8192, second["options"]["num_ctx"])
        self.assertEqual(first["prompt"], second["prompt"])


if __name__ == "__main__":
    unittest.main()
