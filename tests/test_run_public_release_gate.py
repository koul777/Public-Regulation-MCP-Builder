from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_public_release_gate import build_public_release_gate_report, run


class PublicReleaseGateTests(unittest.TestCase):
    def test_ready_source_only_candidate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked_paths = _write_public_docs(root)

            report = build_public_release_gate_report(root, tracked_paths=tracked_paths)

        self.assertTrue(report["passed"])
        self.assertEqual("ready_for_public_release", report["status"])
        self.assertEqual(0, report["finding_count"])
        self.assertEqual(0, report["action_count"])
        self.assertIn("generated_at", report)
        self.assertIn("repo_commit", report)

    def test_blocked_candidate_summarizes_cleanup_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            sample = root / "data" / "sample.hwp"
            sample.parent.mkdir(parents=True)
            sample.write_bytes(b"sample")

            report = build_public_release_gate_report(
                root,
                tracked_paths=["README.md", "data/sample.hwp"],
            )

        self.assertFalse(report["passed"])
        self.assertEqual("blocked_by_public_audit", report["status"])
        self.assertGreater(report["finding_count"], 0)
        self.assertGreater(report["action_count"], 0)
        self.assertIn("Decide the open-source license", "\n".join(report["next_actions"]))

    def test_public_doc_nonpublic_references_are_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked_paths = _write_public_docs(root)
            (root / "README.md").write_text(
                "See docs/private_release_runbook.md before release.\n",
                encoding="utf-8",
            )

            report = build_public_release_gate_report(root, tracked_paths=tracked_paths)

        self.assertFalse(report["passed"])
        self.assertIn("Rewrite public docs", "\n".join(report["next_actions"]))

    def test_can_include_public_harness_dry_run_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked_paths = _write_public_docs(root)

            report = build_public_release_gate_report(
                root,
                tracked_paths=tracked_paths,
                run_public_harness=True,
            )

        self.assertTrue(report["passed"])
        self.assertIsInstance(report["harness"], dict)
        self.assertTrue(report["harness"]["dry_run"])
        self.assertEqual("public", report["harness"]["mode"])

    def test_public_harness_plan_can_require_public_url_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked_paths = _write_public_docs(root)

            report = build_public_release_gate_report(
                root,
                tracked_paths=tracked_paths,
                run_public_harness=True,
                probe_public_url=True,
            )

        commands = {
            step["name"]: step["command"]
            for step in report["harness"]["steps"]
            if isinstance(step, dict)
        }
        self.assertIn("--probe-public-url", commands["mcp_bundle_doctor"])
        self.assertIn("--probe-public-url", commands["chatgpt_https_doctor"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "gate.json"
            out_md = Path(tmp) / "gate.md"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--root",
                    ".",
                    "--include-untracked",
                    "--run-public-harness",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--json",
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("public_release_gate", payload["report_type"])
        self.assertIn("Public Release Gate", markdown)
        self.assertIn('"public_release_gate"', stdout.getvalue())


def _write_public_docs(root: Path) -> list[str]:
    paths = [
        "LICENSE",
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/operator_quickstart_ko.md",
        "docs/public_institution_pilot_plan.md",
        "docs/pilot_acceptance_and_evidence_ko.md",
        "docs/public-institution-operations-runbook.md",
    ]
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ok\n", encoding="utf-8")
    return paths


if __name__ == "__main__":
    unittest.main()
