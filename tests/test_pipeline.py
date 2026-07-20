from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.pipeline import (
    PREPROCESSOR_PIPELINE_VERSION,
    kordoc_table_command_status,
    processing_options_payload,
    quality_profile_config_hash,
)
from app.schemas.chunk import ChunkOptions


class PipelineTests(unittest.TestCase):
    def test_processing_options_payload_includes_pipeline_version(self) -> None:
        payload = processing_options_payload(ChunkOptions(max_chunk_chars=1200, enable_agent_review=False))

        self.assertEqual(payload["max_chunk_chars"], 1200)
        self.assertEqual(payload["pipeline_version"], PREPROCESSOR_PIPELINE_VERSION)
        self.assertEqual(payload["main_ai_review_stage"], "parser_ai_review_draft")
        self.assertNotIn("enable_agent_review", payload)

    def test_processing_options_payload_includes_quality_profile_config_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text('{"profiles":{"strict":{"coverage_ratio_min":0.95}}}', encoding="utf-8")
            settings = Settings(data_dir=Path(tmp) / "data", quality_profiles_path=str(path), quality_profiles_strict=True)
            expected_hash = quality_profile_config_hash(path)

            payload = processing_options_payload(ChunkOptions(), settings=settings)

        self.assertEqual(payload["quality_profiles_sha256"], expected_hash)
        self.assertTrue(payload["quality_profiles_strict"])

    def test_processing_options_payload_changes_when_quality_profile_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text('{"profiles":{"strict":{"coverage_ratio_min":0.95}}}', encoding="utf-8")
            first = processing_options_payload(
                ChunkOptions(),
                settings=Settings(data_dir=Path(tmp) / "data", quality_profiles_path=str(path)),
            )
            path.write_text('{"profiles":{"strict":{"coverage_ratio_min":0.90}}}', encoding="utf-8")
            second = processing_options_payload(
                ChunkOptions(),
                settings=Settings(data_dir=Path(tmp) / "data", quality_profiles_path=str(path)),
            )

        self.assertNotEqual(first["quality_profiles_sha256"], second["quality_profiles_sha256"])

    def test_processing_options_payload_changes_when_agent_review_scope_changes(self) -> None:
        options = ChunkOptions()
        first = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key="configured", agent_review_model="model-a"),
        )
        second = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key="configured", agent_review_model="model-b"),
        )

        self.assertTrue(first["agent_review_cache_scope_hash"].startswith("sha256:"))
        self.assertNotEqual(first["agent_review_cache_scope_hash"], second["agent_review_cache_scope_hash"])

    def test_processing_options_payload_changes_when_agent_review_api_becomes_ready(self) -> None:
        options = ChunkOptions()
        staged = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key="", agent_review_model="model-a"),
        )
        executable = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_agent_review=True, openai_api_key="configured", agent_review_model="model-a"),
        )

        self.assertFalse(staged["agent_review_provider_execution_ready"])
        self.assertTrue(executable["agent_review_provider_execution_ready"])
        self.assertNotEqual(staged, executable)

    def test_processing_options_payload_changes_when_kordoc_table_parser_is_enabled(self) -> None:
        options = ChunkOptions()
        local_only = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_kordoc_table_parser=False),
        )
        kordoc_enabled = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_kordoc_table_parser=True, kordoc_table_command="kordoc"),
        )

        self.assertFalse(local_only["kordoc_table_parser_enabled"])
        self.assertTrue(kordoc_enabled["kordoc_table_parser_enabled"])
        self.assertTrue(kordoc_enabled["kordoc_table_as_main"])
        self.assertEqual(kordoc_enabled["kordoc_table_promote_min_match"], "medium_review_match")
        self.assertEqual(kordoc_enabled["kordoc_table_max_tables"], 500)
        self.assertEqual(kordoc_enabled["kordoc_table_command_label"], "kordoc")
        self.assertIn("kordoc_table_command_available", kordoc_enabled)
        self.assertNotEqual(local_only, kordoc_enabled)

    def test_processing_options_payload_changes_when_kordoc_main_policy_changes(self) -> None:
        options = ChunkOptions()
        promoted = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_kordoc_table_parser=True, kordoc_table_as_main=True),
        )
        hint_only = processing_options_payload(
            options,
            settings=Settings(data_dir=Path("data"), enable_kordoc_table_parser=True, kordoc_table_as_main=False),
        )

        self.assertTrue(promoted["kordoc_table_as_main"])
        self.assertFalse(hint_only["kordoc_table_as_main"])
        self.assertNotEqual(promoted, hint_only)

    def test_processing_options_payload_tracks_kordoc_command_runtime_state(self) -> None:
        options = ChunkOptions()
        kordoc_table_command_status.cache_clear()

        with patch("app.core.pipeline.resolve_kordoc_command", return_value="C:/Tools/kordoc.cmd"):
            with patch("app.core.pipeline._command_version", return_value="4.0.0"):
                payload = processing_options_payload(
                    options,
                    settings=Settings(
                        data_dir=Path("data"),
                        enable_kordoc_table_parser=True,
                        kordoc_table_command="kordoc",
                    ),
                )

        self.assertTrue(payload["kordoc_table_command_available"])
        self.assertEqual(payload["kordoc_table_command_resolved_name"], "kordoc.cmd")
        self.assertEqual(payload["kordoc_table_command_version"], "4.0.0")
        kordoc_table_command_status.cache_clear()

    def test_processing_options_payload_uses_kordoc_windows_global_fallback(self) -> None:
        options = ChunkOptions()
        kordoc_table_command_status.cache_clear()

        with patch("app.core.pipeline.resolve_kordoc_command", return_value=r"C:\Npm\kordoc.cmd"):
            with patch("app.core.pipeline._command_version", return_value="4.2.3"):
                payload = processing_options_payload(
                    options,
                    settings=Settings(
                        data_dir=Path("data"),
                        enable_kordoc_table_parser=True,
                        kordoc_table_command="kordoc",
                    ),
                )

        self.assertTrue(payload["kordoc_table_command_available"])
        self.assertEqual(payload["kordoc_table_command_resolved_name"], "kordoc.cmd")
        self.assertEqual(payload["kordoc_table_command_version"], "4.2.3")
        kordoc_table_command_status.cache_clear()

    def test_processing_options_payload_keeps_agent_review_scope_when_provider_execution_disabled(self) -> None:
        payload = processing_options_payload(
            ChunkOptions(enable_agent_review=False),
            settings=Settings(data_dir=Path("data"), enable_agent_review=False, agent_review_model="model-a"),
        )

        self.assertTrue(payload["agent_review_cache_scope_hash"].startswith("sha256:"))
        self.assertEqual(payload["main_ai_review_stage"], "parser_ai_review_draft")
        self.assertNotIn("enable_agent_review", payload)


if __name__ == "__main__":
    unittest.main()
