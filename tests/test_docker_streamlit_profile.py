from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DockerStreamlitProfileTests(unittest.TestCase):
    def test_streamlit_compose_service_is_local_profile_only(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("profiles:", compose)
        self.assertIn("- local-ui", compose)
        self.assertIn('"127.0.0.1:8501:8501"', compose)
        self.assertIn("STREAMLIT_API_AUTH_REQUIRED:-false", compose)
        self.assertIn("STREAMLIT_TENANT_STORAGE_ISOLATION:-false", compose)


if __name__ == "__main__":
    unittest.main()
