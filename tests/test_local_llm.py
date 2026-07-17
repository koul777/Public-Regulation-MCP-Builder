from __future__ import annotations

import unittest
from pathlib import Path

from app.core.config import Settings
from app.rag.local_llm import build_grounded_prompt, generate_local_llm_answer, local_llm_available, probe_local_llm


class LocalLlmTests(unittest.TestCase):
    def test_rejects_non_local_endpoint(self) -> None:
        settings = Settings(
            data_dir=Path("data"),
            rag_llm_backend="ollama",
            rag_llm_endpoint="https://example.com",
        )

        with self.assertRaisesRegex(ValueError, "localhost"):
            generate_local_llm_answer(settings=settings, query="q", evidence=[])

    def test_reports_availability_only_for_configured_local_backend(self) -> None:
        self.assertFalse(local_llm_available(Settings(data_dir=Path("data"))))
        self.assertFalse(
            local_llm_available(
                Settings(
                    data_dir=Path("data"),
                    rag_llm_backend="ollama",
                    rag_llm_endpoint="https://example.com",
                )
            )
        )
        self.assertTrue(
            local_llm_available(
                Settings(
                    data_dir=Path("data"),
                    rag_llm_backend="ollama",
                    rag_llm_endpoint="http://127.0.0.1:11434",
                )
            )
        )

    def test_probe_reports_failure_for_non_local_endpoint_without_raw_endpoint(self) -> None:
        probe = probe_local_llm(
            Settings(
                data_dir=Path("data"),
                rag_llm_backend="ollama",
                rag_llm_endpoint="https://example.com",
                rag_llm_model="model",
            )
        )

        self.assertTrue(probe["checked"])
        self.assertFalse(probe["available"])
        self.assertEqual(probe["error_type"], "ValueError")
        self.assertNotIn("example.com", str(probe))

    def test_prompt_contains_only_query_and_approved_evidence_contract(self) -> None:
        prompt = build_grounded_prompt(
            query="What is the rule?",
            evidence=[
                {
                    "document_id": "doc",
                    "chunk_id": "chunk",
                    "approval_id": "approval",
                    "text": "approved evidence",
                }
            ],
        )

        self.assertIn("approved evidence", prompt)
        self.assertIn("approval", prompt)
        self.assertNotIn("C:\\", prompt)


if __name__ == "__main__":
    unittest.main()
