from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessDocsTests(unittest.TestCase):
    def test_public_harness_doc_names_required_release_checks(self) -> None:
        text = (REPO_ROOT / "docs" / "harness_engineering_plan_ko.md").read_text(encoding="utf-8")

        self.assertIn("python -m unittest discover -s tests -v", text)
        self.assertIn("python -m build --sdist --wheel", text)
        self.assertIn("scripts\\audit_release_hygiene.py", text)
        self.assertIn("사람 승인 전 색인 차단", text)
        self.assertIn("실패한 검사를 우회한 산출물은 공개 릴리스로 배포하지 않습니다", text)


if __name__ == "__main__":
    unittest.main()
