from __future__ import annotations

import contextlib
import io
import os
import unittest
from collections.abc import Callable, Iterator
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.agents import ollama_runtime
from app.core.config import Settings
from app.core.local_http import build_local_opener, local_urlopen, provider_urlopen
from app.rag import local_llm


LOOPBACK_HOST = "127.0.0.1:11434"
LOOPBACK_URL = f"http://{LOOPBACK_HOST}"
PROXY_HOST = "proxy.invalid:3128"
# Empty NO_PROXY values are ignored by urllib, so a developer machine's own
# bypass list cannot hide a proxied request in these tests.
PROXY_ENV = {
    "HTTP_PROXY": f"http://{PROXY_HOST}",
    "HTTPS_PROXY": f"http://{PROXY_HOST}",
    "http_proxy": f"http://{PROXY_HOST}",
    "https_proxy": f"http://{PROXY_HOST}",
    "NO_PROXY": "",
    "no_proxy": "",
}
REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for ``http.client.HTTPResponse``; no socket is involved."""

    def __init__(self, body: bytes = b"{}", *, code: int = 200, location: str = "") -> None:
        super().__init__(body)
        self.code = code
        self.msg = "synthetic"
        self._headers = Message()
        if location:
            self._headers["Location"] = location

    def info(self) -> Message:
        return self._headers


@contextlib.contextmanager
def _fake_http(responder: Callable[[Request], _FakeResponse]) -> Iterator[list[dict[str, Any]]]:
    """Replace the transport layer so no connection is ever opened.

    Everything above ``HTTPHandler.do_open`` (proxy selection, redirect handling,
    error processing) still runs for real, and each request that reaches the
    transport is recorded with the host it would have connected to.
    """
    seen: list[dict[str, Any]] = []

    def fake_do_open(handler: HTTPHandler, http_class: Any, req: Request, **http_conn_args: Any) -> _FakeResponse:
        seen.append(
            {
                "host": req.host,
                "selector": req.selector,
                "method": req.get_method(),
                "timeout": req.timeout,
            }
        )
        return responder(req)

    with patch.object(HTTPHandler, "do_open", fake_do_open):
        yield seen


def _redirect(code: int) -> Callable[[Request], _FakeResponse]:
    return lambda req: _FakeResponse(b"", code=code, location="http://redirect-target.invalid/elsewhere")


class LocalOpenerTests(unittest.TestCase):
    def test_opener_never_routes_through_a_proxy_even_when_proxy_env_is_set(self) -> None:
        with patch.dict(os.environ, PROXY_ENV):
            opener = build_local_opener()

        # An empty ProxyHandler registers no ``*_open`` methods, so urllib leaves it out
        # of ``opener.handlers``; passing a ProxyHandler instance also stops build_opener
        # from adding the default one that reads environment and system proxies. So the
        # list may hold no ProxyHandler at all, but never one that maps a scheme to a proxy.
        for handler in opener.handlers:
            if isinstance(handler, ProxyHandler):
                self.assertEqual({}, handler.proxies)
            self.assertFalse(
                isinstance(handler, ProxyHandler) and hasattr(handler, "http_open"),
                "a proxy-routing handler is present",
            )
        # The behavior tests below prove the request really goes straight to the target.

    def test_opener_replaces_default_redirect_handler(self) -> None:
        redirect_handlers = [
            handler for handler in build_local_opener().handlers if isinstance(handler, HTTPRedirectHandler)
        ]

        self.assertEqual(1, len(redirect_handlers))
        self.assertIsNot(type(redirect_handlers[0]), HTTPRedirectHandler)

    def test_default_urllib_opener_would_use_the_proxy_in_the_same_environment(self) -> None:
        # Control: proves the proxy environment above really changes where a
        # request goes, so the bypass assertions below are meaningful.
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse()) as seen:
            build_opener().open(Request(LOOPBACK_URL + "/api/tags"), timeout=1).close()

        self.assertEqual([PROXY_HOST], [item["host"] for item in seen])

    def test_local_urlopen_connects_directly_when_proxy_env_is_set(self) -> None:
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse(b'{"ok": true}')) as seen:
            with local_urlopen(Request(LOOPBACK_URL + "/api/tags"), timeout=7) as response:
                body = response.read()

        self.assertEqual(b'{"ok": true}', body)
        self.assertEqual(1, len(seen))
        self.assertEqual(LOOPBACK_HOST, seen[0]["host"])
        self.assertEqual("/api/tags", seen[0]["selector"])
        self.assertEqual(7, seen[0]["timeout"])

    def test_local_urlopen_refuses_every_redirect_status(self) -> None:
        for code in REDIRECT_STATUS_CODES:
            with self.subTest(code=code):
                request = Request(LOOPBACK_URL + "/api/generate", data=b"{}", method="POST")
                with _fake_http(_redirect(code)) as seen:
                    with self.assertRaises(HTTPError) as caught:
                        local_urlopen(request, timeout=1)

                self.assertEqual(code, caught.exception.code)
                self.assertIn("Redirects are not allowed", str(caught.exception))
                # The redirect target must never be contacted.
                self.assertEqual([LOOPBACK_HOST], [item["host"] for item in seen])

    def test_local_urlopen_refuses_non_http_schemes_before_opening(self) -> None:
        for url in ("file:///synthetic/model.json", "ftp://127.0.0.1/model", "data:text/plain,x"):
            with self.subTest(url=url):
                with _fake_http(lambda req: _FakeResponse()) as seen:
                    with self.assertRaises(ValueError):
                        local_urlopen(Request(url), timeout=1)
                    with self.assertRaises(ValueError):
                        local_urlopen(url, timeout=1)

                self.assertEqual([], seen)


class LocalCallSiteTests(unittest.TestCase):
    def _settings(self, backend: str) -> Settings:
        return Settings(
            data_dir=Path("data"),
            rag_llm_backend=backend,
            rag_llm_endpoint=LOOPBACK_URL,
            rag_llm_model="synthetic-model",
        )

    def test_call_sites_keep_the_urlopen_patch_point_bound_to_the_local_helper(self) -> None:
        self.assertIs(local_urlopen, local_llm.urlopen)
        self.assertIs(local_urlopen, ollama_runtime.urlopen)

    def test_rag_ollama_probe_bypasses_proxy(self) -> None:
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse(b'{"response": "OK"}')) as seen:
            probe = local_llm.probe_local_llm(self._settings("ollama"))

        self.assertTrue(probe["available"])
        self.assertEqual([LOOPBACK_HOST], [item["host"] for item in seen])
        self.assertEqual(["/api/generate"], [item["selector"] for item in seen])

    def test_rag_openai_compatible_answer_bypasses_proxy(self) -> None:
        body = b'{"choices": [{"message": {"content": "synthetic answer"}}]}'
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse(body)) as seen:
            answer = local_llm.generate_local_llm_answer(
                settings=self._settings("llama-cpp"),
                query="synthetic question",
                evidence=[],
            )

        self.assertEqual("synthetic answer", answer)
        self.assertEqual([LOOPBACK_HOST], [item["host"] for item in seen])
        self.assertEqual(["/v1/chat/completions"], [item["selector"] for item in seen])

    def test_rag_probe_reports_redirect_as_unavailable(self) -> None:
        with _fake_http(_redirect(302)) as seen:
            probe = local_llm.probe_local_llm(self._settings("ollama"))

        self.assertFalse(probe["available"])
        self.assertEqual("HTTPError", probe["error_type"])
        self.assertEqual(1, len(seen))

    # The OllamaRuntime transport tests live in tests/test_ollama_runtime.py so the
    # preprocessing guard maps app/agents/ollama_runtime.py to a same-named test.


class ProviderOpenerTests(unittest.TestCase):
    REMOTE_URL = "http://provider.invalid/v1/chat/completions"

    def test_loopback_provider_bypasses_the_proxy(self) -> None:
        request = Request(LOOPBACK_URL + "/v1/chat/completions", data=b"{}", method="POST")
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse(b"{}")) as seen:
            provider_urlopen(request, timeout=2).close()

        self.assertEqual([LOOPBACK_HOST], [item["host"] for item in seen])

    def test_remote_provider_keeps_the_institution_proxy(self) -> None:
        request = Request(self.REMOTE_URL, data=b"{}", method="POST")
        with patch.dict(os.environ, PROXY_ENV), _fake_http(lambda req: _FakeResponse(b"{}")) as seen:
            provider_urlopen(request, timeout=2).close()

        self.assertEqual([PROXY_HOST], [item["host"] for item in seen])

    def test_remote_provider_refuses_redirects_so_credentials_stay_put(self) -> None:
        for code in REDIRECT_STATUS_CODES:
            with self.subTest(code=code):
                request = Request(
                    self.REMOTE_URL,
                    data=b"{}",
                    headers={"Authorization": "Bearer synthetic"},
                    method="POST",
                )
                with patch.dict(os.environ, PROXY_ENV), _fake_http(_redirect(code)) as seen:
                    with self.assertRaises(HTTPError) as caught:
                        provider_urlopen(request, timeout=2)

                self.assertEqual(code, caught.exception.code)
                # Only the original request was sent; the redirect target was never contacted.
                self.assertEqual([PROXY_HOST], [item["host"] for item in seen])

    def test_provider_urlopen_refuses_non_http_schemes(self) -> None:
        with self.assertRaises(ValueError):
            provider_urlopen(Request("file:///synthetic/key.txt"), timeout=1)


if __name__ == "__main__":
    unittest.main()
