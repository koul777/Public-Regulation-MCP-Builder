from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class UiUxReleaseScopeKoTests(unittest.TestCase):
    def test_ui_ux_scope_is_honest_about_local_operator_console(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        scope = (REPO_ROOT / "docs" / "ui_ux_release_scope_ko.md").read_text(encoding="utf-8")

        self.assertIn("Streamlit 화면은 로컬 운영자용", readme)
        for phrase in [
            "로컬 운영자용 Streamlit operator console",
            "완성형 SaaS 화면이 아니라",
            "Streamlit은 local-only UI",
            "API_AUTH_REQUIRED=true",
            "TENANT_STORAGE_ISOLATION=true",
            "authenticated FastAPI path",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, scope)


if __name__ == "__main__":
    unittest.main()
