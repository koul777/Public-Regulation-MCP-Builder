from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from app.agents.hermes_agent import HermesAgent, render_hermes_markdown
from scripts.run_hermes import run


class HermesAgentTests(unittest.TestCase):
    def test_plan_mode_returns_dry_run_plan(self) -> None:
        report = HermesAgent().run({"mode": "plan", "project_root": Path.cwd()})

        self.assertEqual(report["report_type"], "hermes_agent_run")
        self.assertEqual(report["status"], "plan_ready")
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["harness_mode"], "internal")
        self.assertIn("harness", report)
        self.assertTrue(report["next_actions"])

    def test_mcp_check_dry_run_maps_to_mcp_harness(self) -> None:
        report = HermesAgent().run({"mode": "mcp-check", "project_root": Path.cwd(), "dry_run": True})

        self.assertEqual(report["harness_mode"], "mcp")
        self.assertIn("attention_items", report)
        self.assertIn("configuration readiness", "\n".join(report["attention_items"]))
        self.assertIn("synthetic scratch chain", "\n".join(report["attention_items"]))
        self.assertIn("reg-rag-mcp-index-visibility", "\n".join(report["next_actions"]))
        names = [step["name"] for step in report["harness"]["steps"]]
        self.assertIn("mcp_transport_smoke", names)
        self.assertNotIn("public_release_audit", names)

    def test_mcp_check_with_build_includes_wheel_backed_bundle(self) -> None:
        report = HermesAgent().run(
            {"mode": "mcp-check", "project_root": Path.cwd(), "dry_run": True, "skip_build": False}
        )

        steps = {step["name"]: step for step in report["harness"]["steps"]}

        self.assertIn("package_build", steps)
        self.assertIn("sdist_rehearsal", steps)
        self.assertIn("--include-wheel", steps["mcp_bundle_config"]["command"])

    def test_mcp_runtime_data_dir_adds_index_visibility_step(self) -> None:
        report = HermesAgent().run(
            {
                "mode": "mcp-check",
                "project_root": Path.cwd(),
                "dry_run": True,
                "tenant_id": "tenant-a",
                "tenant_storage_isolation": True,
                "mcp_runtime_data_dir": "data/runtime",
                "mcp_bundle_profile_id": "profile-a",
                "mcp_min_visible_records": 25,
            }
        )

        steps = {step["name"]: step for step in report["harness"]["steps"]}

        self.assertIn("mcp_index_visibility", steps)
        command = steps["mcp_index_visibility"]["command"]
        self.assertIn("scripts/audit_mcp_index_visibility.py", command)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("data/runtime") for item in command))
        self.assertIn("tenant-a", command)
        self.assertIn("--tenant-storage-isolation", command)
        self.assertIn("25", command)
        bundle_command = steps["mcp_bundle_config"]["command"]
        self.assertIn("--profile-id", bundle_command)
        self.assertIn("profile-a", bundle_command)

    def test_probe_public_url_is_forwarded_to_remote_doctors(self) -> None:
        report = HermesAgent().run(
            {
                "mode": "mcp-check",
                "project_root": Path.cwd(),
                "dry_run": True,
                "probe_public_url": True,
            }
        )

        steps = {step["name"]: step for step in report["harness"]["steps"]}

        self.assertIn("--probe-public-url", steps["mcp_bundle_doctor"]["command"])
        self.assertIn("--probe-public-url", steps["chatgpt_https_doctor"]["command"])

    def test_public_check_plan_mentions_fresh_clone_and_executed_gate(self) -> None:
        report = HermesAgent().run({"mode": "public-check", "project_root": Path.cwd(), "dry_run": True})

        actions = "\n".join(report["next_actions"])

        self.assertIn("reg-rag-fresh-clone-rehearsal --mode public --full", actions)
        self.assertIn("reg-rag-public-release-gate --include-untracked --execute-harness", actions)

    def test_markdown_renderer_summarizes_status_and_actions(self) -> None:
        report = HermesAgent().run({"mode": "mcp-check", "project_root": Path.cwd(), "dry_run": True})

        rendered = render_hermes_markdown(report)

        self.assertIn("# Hermes Agent Report", rendered)
        self.assertIn("plan_ready", rendered)
        self.assertIn("Attention Items", rendered)
        self.assertIn("Next Actions", rendered)
        self.assertIn("Harness Steps", rendered)
        self.assertIn("Evidence Outputs", rendered)
        self.assertIn("mcp_transport_smoke", rendered)
        self.assertIn("reports/mcp_transport_smoke_hermes.json", rendered)

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "hermes.json"
            out_md = Path(tmp) / "hermes.md"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--mode",
                    "mcp-check",
                    "--dry-run",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=stdout,
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["report_type"], "hermes_agent_run")
            names = [step["name"] for step in payload["harness"]["steps"]]
            self.assertNotIn("unit_tests", names)
            self.assertNotIn("package_build", names)
            self.assertIn("mcp_transport_smoke", names)
            self.assertIn("Hermes Agent Report", out_md.read_text(encoding="utf-8"))

    def test_cli_include_build_adds_include_wheel_to_bundle_plan(self) -> None:
        stdout = io.StringIO()

        exit_code = run(["--mode", "mcp-check", "--dry-run", "--include-build"], stdout=stdout)
        payload = json.loads(stdout.getvalue())
        steps = {step["name"]: step for step in payload["harness"]["steps"]}

        self.assertEqual(0, exit_code)
        self.assertIn("package_build", steps)
        self.assertIn("--include-wheel", steps["mcp_bundle_config"]["command"])

    def test_cli_include_evidence_dry_run_prints_evidence_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "hermes.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--mode",
                    "mcp-check",
                    "--dry-run",
                    "--include-evidence",
                    "--out-json",
                    str(out_json),
                    "--evidence-index-json",
                    str(Path(tmp) / "evidence_index.json"),
                    "--evidence-verification-json",
                    str(Path(tmp) / "evidence_verification.json"),
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            written = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertEqual("hermes_agent_evidence_plan", payload["report_type"])
        self.assertEqual("hermes-mcp", payload["evidence_plan"]["profile"])
        self.assertEqual("hermes_agent_run", written["report_type"])
        self.assertTrue(payload["hermes_report"]["dry_run"])


if __name__ == "__main__":
    unittest.main()
