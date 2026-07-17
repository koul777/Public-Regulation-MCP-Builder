from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class OfficialRagApprovalGatePolicyTests(unittest.TestCase):
    def test_runtime_code_does_not_disable_approval_required_indexing(self) -> None:
        offenders: list[str] = []
        pattern = re.compile(r"require_approval\s*=\s*False")

        for base in ["app", "frontend", "scripts"]:
            for path in (REPO_ROOT / base).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if pattern.search(text):
                    offenders.append(str(path.relative_to(REPO_ROOT)))

        self.assertEqual([], offenders)

    def test_streamlit_labels_unreviewed_mode_as_isolated_poc_review(self) -> None:
        source = (REPO_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("UNREVIEWED_POC_REVIEW", source)
        self.assertIn("legacy UNREVIEWED_PREVIEW", source)
        self.assertIn("isolated PoC Review mode", source)
        self.assertIn("must not write to official approved vectors", source)
        self.assertIn("UNREVIEWED_POC_REVIEW_ACK_KEY", source)
        self.assertIn("I understand this is Unreviewed PoC Review only and not official RAG/MCP.", source)
        self.assertIn("disabled=poc_review_needs_ack", source)
        self.assertIn("Official RAG/MCP remains blocked", source)


if __name__ == "__main__":
    unittest.main()
