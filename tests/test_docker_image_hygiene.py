from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DockerImageHygieneTests(unittest.TestCase):
    def test_dockerignore_excludes_local_release_artifacts(self):
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

        for pattern in (".git", "data", "reports", "dist", "build", "*.egg-info", ".env"):
            self.assertIn(pattern, dockerignore)

    def test_dockerfile_installs_after_source_copy(self):
        lines = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()

        copy_index = lines.index("COPY . .")
        install_index = lines.index("RUN pip install --no-cache-dir .")
        self.assertLess(copy_index, install_index)

    def test_dockerfile_exposes_api_port_only(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("EXPOSE 8000", dockerfile)
        self.assertNotIn("8501", dockerfile)


if __name__ == "__main__":
    unittest.main()
