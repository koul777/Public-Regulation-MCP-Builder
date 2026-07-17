from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PrivateReleaseDocsTests(unittest.TestCase):
    def test_private_release_docs_are_excluded_from_source_only_release(self) -> None:
        private_paths = (
            "private_release_checklist.md",
            "private_release_operator_notes.md",
            "private_release_runbook.md",
            "private_release_incident_response.md",
        )
        for filename in private_paths:
            with self.subTest(filename=filename):
                self.assertFalse((REPO_ROOT / "docs" / filename).exists())
        self.assertTrue((REPO_ROOT / "SECURITY.md").exists())
        self.assertTrue((REPO_ROOT / "CONTRIBUTING.md").exists())

    def test_public_readme_describes_readiness_scope_and_streamlit_local_only(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("개발 중", readme)
        self.assertIn("Windows 10/11 64비트 우선 지원", readme)
        self.assertIn("Streamlit 화면은 로컬 운영자용", readme)
        self.assertIn("streamlit run frontend\\streamlit_app.py --server.address 127.0.0.1", readme)


if __name__ == "__main__":
    unittest.main()
