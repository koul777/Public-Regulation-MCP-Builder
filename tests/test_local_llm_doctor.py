from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.local_llm_doctor import diagnose_local_llm


class LocalLlmDoctorTests(unittest.TestCase):
    def test_extractive_mode_passes_without_local_model(self) -> None:
        report = diagnose_local_llm(backend="extractive", data_dir=Path("data"))

        self.assertTrue(report["passed"])
        self.assertEqual(report["reason"], "model_free_mode")

    def test_rejects_non_local_endpoint_without_probe(self) -> None:
        report = diagnose_local_llm(
            backend="ollama",
            endpoint="https://example.com",
            model="qwen3:8b",
            probe=False,
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "endpoint_not_allowed_or_missing")
        self.assertNotIn("example.com", str(report))

    @patch("scripts.local_llm_doctor.probe_local_llm", return_value={"available": True, "endpoint_host": "127.0.0.1", "model": "qwen3:8b"})
    @patch("scripts.local_llm_doctor.local_llm_available", return_value=True)
    def test_reports_qwen3_8b_available(self, available, probe) -> None:
        report = diagnose_local_llm(
            backend="ollama",
            endpoint="http://127.0.0.1:11434",
            model="qwen3:8b",
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["model"], "qwen3:8b")
        self.assertEqual(report["reason"], "available")
        available.assert_called_once()
        probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
