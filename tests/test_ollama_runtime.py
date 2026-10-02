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


if __name__ == "__main__":
    unittest.main()
