from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core.config import Settings, _default_regulation_authoring_enabled


class AuthoringConfigTests(unittest.TestCase):
    def test_authoring_storage_has_a_dedicated_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")

            self.assertEqual(settings.data_dir / "authoring", settings.authoring_dir)
            self.assertNotEqual(settings.uploads_dir, settings.authoring_dir)
            self.assertNotEqual(settings.exports_dir, settings.authoring_dir)

    def test_authoring_feature_flag_can_be_disabled_fail_closed(self) -> None:
        settings = Settings(enable_regulation_authoring=False)

        self.assertFalse(settings.enable_regulation_authoring)

    def test_protected_environment_default_requires_explicit_opt_in(self) -> None:
        with patch.dict("os.environ", {"APP_ENV": "production"}):
            enabled = _default_regulation_authoring_enabled()

        self.assertFalse(enabled)

    def test_local_compose_profile_enables_beginner_authoring_separately(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        env_example = (project_root / ".env.example").read_text(encoding="utf-8")
        compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("ENABLE_REGULATION_AUTHORING=false", env_example)
        self.assertIn("STREAMLIT_ENABLE_REGULATION_AUTHORING=true", env_example)
        self.assertIn(
            "ENABLE_REGULATION_AUTHORING: ${STREAMLIT_ENABLE_REGULATION_AUTHORING:-true}",
            compose,
        )


if __name__ == "__main__":
    unittest.main()
