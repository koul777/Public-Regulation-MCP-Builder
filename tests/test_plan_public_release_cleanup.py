from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.audit_public_release_readiness import PublicFinding
from scripts.plan_public_release_cleanup import build_public_release_cleanup_plan, run


class PlanPublicReleaseCleanupTests(unittest.TestCase):
    def test_builds_deduplicated_actions_from_findings(self) -> None:
        findings = [
            PublicFinding("high", "missing-license", ".", "missing"),
            PublicFinding("medium", "missing-public-doc", "AGENTS.md", "missing"),
            PublicFinding("high", "tracked-runtime-data", "data/private.hwp", "runtime"),
            PublicFinding("high", "tracked-document-sample", "data/private.hwp", "sample"),
            PublicFinding("high", "tracked-nonpublic-doc", "docs/private_release_runbook.md", "private"),
            PublicFinding("medium", "public-doc-nonpublic-reference", "README.md", "reference"),
            PublicFinding("high", "tracked-report-artifact", "reports/report.json", "report"),
            PublicFinding("high", "generated-artifact-path", "tmp/runtime/vectors.jsonl", "generated"),
            PublicFinding("medium", "institution-identifier-risk", "reports/report.json", "id"),
        ]

        report = build_public_release_cleanup_plan(".", findings=findings)

        actions = {(action["action"], action["path"]) for action in report["actions"]}
        self.assertIn(("choose_and_add_license", "LICENSE"), actions)
        self.assertIn(("track_public_doc", "AGENTS.md"), actions)
        self.assertIn(("remove_or_document_sample", "data/private.hwp"), actions)
        self.assertIn(("remove_nonpublic_doc", "docs/private_release_runbook.md"), actions)
        self.assertIn(("rewrite_public_doc_for_public_release", "README.md"), actions)
        self.assertIn(("remove_generated_report", "reports/report.json"), actions)
        self.assertIn(("remove_or_ignore_generated_artifact", "tmp/runtime/vectors.jsonl"), actions)
        self.assertIn(("synthesize_or_remove_identifier_fixture", "reports/report.json"), actions)
        actions_by_name = {action["action"]: action for action in report["actions"]}
        self.assertEqual("owner_legal_decision", actions_by_name["choose_and_add_license"]["action_class"])
        self.assertTrue(actions_by_name["remove_or_document_sample"]["requires_owner_decision"])
        self.assertTrue(actions_by_name["remove_generated_report"]["destructive"])
        self.assertEqual("dedicated_public_release_branch", actions_by_name["remove_generated_report"]["apply_scope"])
        self.assertEqual(5, report["owner_decision_action_count"])
        self.assertEqual(3, report["safe_machine_action_count"])
        self.assertEqual(4, report["destructive_action_count"])
        self.assertEqual(
            {"owner_legal_decision": 2, "owner_policy_decision": 3, "safe_machine_action": 3},
            report["action_class_counts"],
        )

    def test_cli_writes_json_and_markdown_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "README.md").write_text("ok\n", encoding="utf-8")
            out_json = root / "plan.json"
            out_md = root / "plan.md"
            stdout = io.StringIO()

            exit_code = run(
                ["--root", str(root), "--out-json", str(out_json), "--out-md", str(out_md), "--json"],
                stdout=stdout,
            )

            self.assertEqual(0, exit_code)
            self.assertTrue(out_json.exists())
            self.assertTrue(out_md.exists())
            payload = stdout.getvalue()
            self.assertIn("public_release_cleanup_plan", payload)
            self.assertIn('"generated_at"', payload)
            self.assertIn('"repo_commit"', payload)
            self.assertIn("Owner-decision actions", out_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
