from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.processors.quality_gate import QualityProfileConfig
from app.core.pipeline import processing_options_payload
from app.schemas.chunk import ChunkOptions
from app.services.processing_service import ProcessingService
from app.storage.repository import JsonRepository


class ProcessingServiceQualityProfilesTests(unittest.TestCase):
    def test_supplied_quality_profile_config_bypasses_missing_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                quality_profiles_path=str(Path(tmp) / "missing-quality-profiles.json"),
            )
            service = ProcessingService(
                settings,
                JsonRepository(settings),
                quality_profile_config=QualityProfileConfig(),
            )

        self.assertIsNotNone(service.quality_gate)
        self.assertRegex(service.quality_profiles_sha256, r"^[0-9a-f]{64}$")

    def test_supplied_quality_profile_config_sha_is_used_in_processing_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                quality_profiles_path=str(Path(tmp) / "different-quality-profiles.json"),
            )
            service = ProcessingService(
                settings,
                JsonRepository(settings),
                quality_profile_config=QualityProfileConfig(),
            )

            payload = processing_options_payload(
                ChunkOptions(),
                settings=settings,
                quality_profiles_sha256=service.quality_profiles_sha256,
            )

        self.assertEqual(payload["quality_profiles_sha256"], service.quality_profiles_sha256)


if __name__ == "__main__":
    unittest.main()
