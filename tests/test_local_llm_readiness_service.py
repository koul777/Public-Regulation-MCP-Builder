from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

from app.core.config import Settings
from app.services.local_llm_readiness_service import check_local_llm_readiness, diagnose_local_llm_settings
from app.services.readiness_adapter import OperatorReadinessState


class LocalLlmReadinessServiceTests(unittest.TestCase):
    def test_extractive_mode_needs_no_probe_or_install(self) -> None:
        runner = Mock(side_effect=AssertionError("must not contact a model"))
        card = check_local_llm_readiness(Settings(rag_llm_backend="extractive"), probe_runner=runner)
        self.assertEqual(OperatorReadinessState.READY, card.state)
        self.assertEqual("model_free_mode", card.reason_code)
        runner.assert_not_called()

    def test_remote_or_credentialed_endpoint_is_blocked_before_probe(self) -> None:
        for endpoint in ("https://example.invalid", "http://user:secret@127.0.0.1:11434"):
            with self.subTest(endpoint=endpoint):
                runner = Mock(side_effect=AssertionError("must not send"))
                card = check_local_llm_readiness(
                    Settings(rag_llm_backend="ollama", rag_llm_endpoint=endpoint), probe_runner=runner,
                )
                self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)
                self.assertEqual("endpoint_not_allowed_or_missing", card.reason_code)
                runner.assert_not_called()
                self.assertNotIn("secret", str(card))

    def test_configuration_only_is_pending(self) -> None:
        runner = Mock()
        card = check_local_llm_readiness(Settings(rag_llm_backend="ollama"), probe=False, probe_runner=runner)
        self.assertEqual(OperatorReadinessState.CONFIGURED_PENDING, card.state)
        runner.assert_not_called()

    def test_malformed_or_failed_probe_does_not_expose_error_or_claim_success(self) -> None:
        for response in ([], {}, {"available": "yes"}, {"available": True, "checked": False},
                         {"available": True, "checked": "false"}):
            with self.subTest(response=response):
                card = check_local_llm_readiness(Settings(rag_llm_backend="ollama"), probe_runner=lambda _: response)
                self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)
        card = check_local_llm_readiness(
            Settings(rag_llm_backend="ollama"), probe_runner=Mock(side_effect=RuntimeError("synthetic-secret")),
        )
        self.assertEqual("local_probe_failed", card.reason_code)
        self.assertNotIn("synthetic-secret", str(card))

    def test_probe_receives_exact_runtime_settings_and_discards_untrusted_fields(self) -> None:
        settings = Settings(rag_llm_backend="ollama", rag_llm_timeout_seconds=3, rag_llm_model="synthetic-model")
        runner = Mock(return_value={"available": True, "model": "synthetic-secret", "error": "private detail",
                                    "endpoint_host": "private-host"})
        report = diagnose_local_llm_settings(settings, probe_runner=runner)
        runner.assert_called_once_with(settings)
        self.assertTrue(report["passed"])
        self.assertNotIn("synthetic-secret", str(report))
        self.assertNotIn("private", str(report))

    def test_real_loopback_http_probe_is_required_for_ready(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"response":"OK"}')

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                card = check_local_llm_readiness(Settings(
                    rag_llm_backend="ollama", rag_llm_model="synthetic-model",
                    rag_llm_endpoint=f"http://127.0.0.1:{server.server_port}", rag_llm_timeout_seconds=3,
                ))
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.assertEqual(OperatorReadinessState.READY, card.state)
        self.assertEqual(1, len(requests))
        self.assertEqual("synthetic-model", requests[0]["model"])
