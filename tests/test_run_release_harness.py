from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_release_harness import HarnessOptions, build_harness_steps, build_parser, run, run_harness


class ReleaseHarnessTests(unittest.TestCase):
    def test_internal_mode_marks_public_audit_as_advisory(self) -> None:
        options = HarnessOptions(project_root=Path.cwd(), mode="internal")

        steps = {step.name: step for step in build_harness_steps(options)}

        self.assertIn("unit_tests", steps)
        self.assertIn("release_tree_clean", steps)
        self.assertIn("package_build", steps)
        self.assertIn("sdist_rehearsal", steps)
        self.assertIn("console_scripts", steps)
        self.assertIn("mcp_bundle_doctor", steps)
        self.assertIn("mcp_transport_smoke", steps)
        self.assertIn("chatgpt_https_doctor", steps)
        self.assertIn("chatgpt_tunnel_doctor", steps)
        self.assertIn("public_release_audit", steps)
        self.assertFalse(steps["public_release_audit"].required)
        self.assertIn("--skip-cli-check", steps["chatgpt_tunnel_doctor"].command)
        self.assertIn("--include-wheel", steps["mcp_bundle_config"].command)

    def test_public_mode_requires_public_audit(self) -> None:
        options = HarnessOptions(project_root=Path.cwd(), mode="public")

        steps = {step.name: step for step in build_harness_steps(options)}

        self.assertTrue(steps["public_release_audit"].required)
        self.assertTrue(steps["public_release_cleanup_plan"].required)
        self.assertNotIn("real_parser_fixture_gate", steps)

    def test_real_parser_fixture_gate_accepts_explicit_curated_root(self) -> None:
        root = Path.cwd()
        fixture_root = Path("data/curated-release-fixtures")
        options = HarnessOptions(
            project_root=root,
            mode="mcp",
            require_real_parser_fixtures=True,
            real_parser_fixture_root=fixture_root,
        )

        steps = {step.name: step for step in build_harness_steps(options)}
        command = steps["real_parser_fixture_gate"].command

        self.assertIn("--fixture-root", command)
        self.assertIn(str(root / fixture_root), command)
        self.assertIn("--out-json", command)
        self.assertIn("--out-md", command)

    def test_probe_public_url_is_forwarded_to_remote_doctors(self) -> None:
        options = HarnessOptions(project_root=Path.cwd(), mode="public", probe_public_url=True)

        steps = {step.name: step for step in build_harness_steps(options)}

        self.assertIn("--probe-public-url", steps["mcp_bundle_doctor"].command)
        self.assertIn("--probe-public-url", steps["chatgpt_https_doctor"].command)
        self.assertNotIn("--probe-public-url", steps["chatgpt_tunnel_doctor"].command)

    def test_runtime_data_dir_adds_mcp_index_visibility_step(self) -> None:
        options = HarnessOptions(
            project_root=Path.cwd(),
            mode="mcp",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
            mcp_runtime_data_dir=Path("data/runtime"),
            mcp_min_visible_records=25,
        )

        steps = {step.name: step for step in build_harness_steps(options)}

        self.assertIn("mcp_index_visibility", steps)
        self.assertIn("mcp_bundle_local_stdio_doctor", steps)
        self.assertIn("mcp_bundle_client_config_smoke", steps)
        self.assertIn("mcp_bundle_zip_extract_smoke", steps)
        self.assertIn("mcp_bundle_transport_smoke", steps)
        command = steps["mcp_index_visibility"].command
        self.assertIn("scripts/audit_mcp_index_visibility.py", command)
        self.assertIn("--data-dir", command)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("data/runtime") for item in command))
        self.assertIn("--tenant-id", command)
        self.assertIn("tenant-a", command)
        self.assertIn("--tenant-storage-isolation", command)
        self.assertIn("--min-visible-records", command)
        self.assertIn("25", command)
        self.assertIn("--forbid-smoke-docs", command)
        self.assertIn("--require-indexed", command)
        self.assertIn("--fail-on-issue", command)
        bundle_command = steps["mcp_bundle_config"].command
        self.assertIn("--data-dir", bundle_command)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("data/runtime") for item in bundle_command))
        self.assertNotIn("--skip-runtime-data", bundle_command)
        local_doctor = steps["mcp_bundle_local_stdio_doctor"].command
        self.assertIn("scripts/check_mcp_connection_readiness.py", local_doctor)
        self.assertIn("--transport", local_doctor)
        self.assertIn("stdio", local_doctor)
        self.assertIn("--bundle-dir", local_doctor)
        self.assertIn("--codex-config", local_doctor)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("mcp_connection_bundle_harness/codex_config_snippet.toml") for item in local_doctor))
        self.assertIn("--claude-desktop-config", local_doctor)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("mcp_connection_bundle_harness/claude_desktop_config.json") for item in local_doctor))
        self.assertIn("--allow-local-only-bundle", local_doctor)
        self.assertIn("--audit-index-visibility", local_doctor)
        self.assertIn("--tenant-storage-isolation", local_doctor)
        self.assertIn("--forbid-smoke-docs", local_doctor)
        self.assertIn("--require-indexed", local_doctor)
        self.assertIn("--fail-on-warning", local_doctor)
        client_config_smoke = steps["mcp_bundle_client_config_smoke"].command
        self.assertIn("scripts/run_mcp_client_config_smoke.py", client_config_smoke)
        self.assertIn("--codex-config", client_config_smoke)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("mcp_connection_bundle_harness/codex_config_snippet.toml") for item in client_config_smoke))
        self.assertIn("--claude-desktop-config", client_config_smoke)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("mcp_connection_bundle_harness/claude_desktop_config.json") for item in client_config_smoke))
        self.assertIn("--fail-on-issue", client_config_smoke)
        zip_extract_smoke = steps["mcp_bundle_zip_extract_smoke"].command
        self.assertIn("scripts/run_mcp_bundle_zip_extract_smoke.py", zip_extract_smoke)
        self.assertIn("--bundle-zip", zip_extract_smoke)
        self.assertIn("--extract-dir", zip_extract_smoke)
        self.assertIn("--overwrite", zip_extract_smoke)
        self.assertIn("--fail-on-issue", zip_extract_smoke)
        transport_smoke = steps["mcp_bundle_transport_smoke"].command
        self.assertIn("scripts/run_mcp_transport_smoke.py", transport_smoke)
        self.assertIn("--skip-preparation", transport_smoke)
        self.assertIn("--no-warm-cache", transport_smoke)
        self.assertIn("--fail-on-issue", transport_smoke)
        self.assertTrue(any(str(item).replace("\\", "/").endswith("mcp_connection_bundle_harness/data") for item in transport_smoke))

    def test_source_only_harness_does_not_require_runtime_bundle_data_check(self) -> None:
        options = HarnessOptions(project_root=Path.cwd(), mode="mcp")

        steps = {step.name: step for step in build_harness_steps(options)}

        self.assertNotIn("mcp_bundle_local_stdio_doctor", steps)
        self.assertNotIn("mcp_bundle_client_config_smoke", steps)
        self.assertNotIn("mcp_bundle_zip_extract_smoke", steps)
        self.assertNotIn("mcp_bundle_transport_smoke", steps)
        self.assertNotIn("--data-dir", steps["mcp_bundle_config"].command)
        self.assertIn("--skip-runtime-data", steps["mcp_bundle_config"].command)

    def test_scoped_runtime_bundle_forwards_explicit_profile(self) -> None:
        options = HarnessOptions(
            project_root=Path.cwd(),
            mode="mcp",
            mcp_runtime_data_dir=Path("data/runtime"),
            mcp_bundle_profile_id="profile-a",
        )

        command = {step.name: step for step in build_harness_steps(options)}["mcp_bundle_config"].command

        self.assertIn("--data-dir", command)
        self.assertIn("--profile-id", command)
        self.assertIn("profile-a", command)
        self.assertNotIn("--skip-runtime-data", command)

    def test_dirty_build_requires_explicit_non_release_override(self) -> None:
        strict_steps = {
            step.name: step
            for step in build_harness_steps(HarnessOptions(project_root=Path.cwd(), mode="mcp"))
        }
        override_steps = {
            step.name: step
            for step in build_harness_steps(
                HarnessOptions(project_root=Path.cwd(), mode="mcp", allow_dirty_build=True)
            )
        }

        self.assertIn("release_tree_clean", strict_steps)
        self.assertNotIn("release_tree_clean", override_steps)
        self.assertIn("--out-json", strict_steps["release_tree_clean"].command)

    def test_dry_run_writes_reproducible_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "release_harness.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--project-root",
                    str(Path.cwd()),
                    "--mode",
                    "mcp",
                    "--dry-run",
                    "--skip-tests",
                    "--skip-build",
                    "--out-json",
                    str(out_json),
                ],
                stdout=stdout,
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(payload["dry_run"])
            self.assertTrue(payload["passed"])
            names = [step["name"] for step in payload["steps"]]
            self.assertNotIn("unit_tests", names)
            self.assertNotIn("sdist_rehearsal", names)
            self.assertIn("mcp_bundle_config", names)
            self.assertIn("diff_check", names)
            bundle_step = next(step for step in payload["steps"] if step["name"] == "mcp_bundle_config")
            self.assertNotIn("--include-wheel", bundle_step["command"])

    def test_artifact_dir_rebases_default_build_and_report_outputs(self) -> None:
        root = Path.cwd()
        artifact_dir = Path("reports/overnight_runs/run-1/release")
        options = HarnessOptions(project_root=root, mode="internal", artifact_dir=artifact_dir)

        steps = {step.name: step for step in build_harness_steps(options)}

        expected_root = str((root / artifact_dir).resolve()).replace("\\", "/")
        package_command = [str(value).replace("\\", "/") for value in steps["package_build"].command]
        self.assertIn("--outdir", package_command)
        self.assertIn(f"{expected_root}/dist", package_command)
        rehearsal_command = [str(value).replace("\\", "/") for value in steps["sdist_rehearsal"].command]
        self.assertIn("--sdist-dir", rehearsal_command)
        self.assertIn(f"{expected_root}/dist", rehearsal_command)
        bundle_command = [str(value).replace("\\", "/") for value in steps["mcp_bundle_config"].command]
        self.assertIn("--wheel-dist-dir", bundle_command)
        self.assertIn(f"{expected_root}/dist", bundle_command)
        output_flags = {
            "--out-json",
            "--out-md",
            "--out-dir",
            "--outdir",
            "--sdist-dir",
            "--wheel-dist-dir",
            "--zip-out",
        }
        output_args = []
        for step in steps.values():
            command = [str(value).replace("\\", "/") for value in step.command]
            for index, value in enumerate(command[:-1]):
                if value in output_flags and "/reports/" in command[index + 1]:
                    output_args.append(command[index + 1])
        self.assertTrue(output_args)
        self.assertTrue(all(value.startswith(expected_root) for value in output_args))

    def test_package_build_can_use_dedicated_build_tool_python(self) -> None:
        root = Path.cwd()
        build_python = Path("reports/build-tool-venv/Scripts/python.exe")
        options = HarnessOptions(
            project_root=root,
            mode="mcp",
            build_python=build_python,
        )

        steps = {step.name: step for step in build_harness_steps(options)}

        self.assertEqual(steps["package_build"].command[0], str(root / build_python))
        self.assertEqual(steps["sdist_rehearsal"].command[0], sys.executable)

    def test_source_date_epoch_normalizes_sdist_before_rehearsal(self) -> None:
        root = Path.cwd()
        artifact_dir = Path("reports/overnight_runs/run-1/release/reproducible")
        options = HarnessOptions(
            project_root=root,
            mode="mcp",
            artifact_dir=artifact_dir,
            source_date_epoch=1_783_809_530,
        )

        steps = build_harness_steps(options)
        names = [step.name for step in steps]
        package = next(step for step in steps if step.name == "package_build")
        normalizer = next(step for step in steps if step.name == "normalize_sdist")

        self.assertEqual(package.env, {"SOURCE_DATE_EPOCH": "1783809530"})
        self.assertLess(names.index("package_build"), names.index("normalize_sdist"))
        self.assertLess(names.index("normalize_sdist"), names.index("sdist_rehearsal"))
        command = [str(value).replace("\\", "/") for value in normalizer.command]
        expected_root = str((root / artifact_dir).resolve()).replace("\\", "/")
        self.assertIn(f"{expected_root}/dist", command)
        self.assertIn(f"{expected_root}/sdist_normalization_harness.json", command)
        self.assertIn("--fail-on-issue", command)

    def test_sdist_normalization_is_opt_in(self) -> None:
        steps = build_harness_steps(HarnessOptions(project_root=Path.cwd(), mode="mcp"))

        self.assertNotIn("normalize_sdist", [step.name for step in steps])
        package = next(step for step in steps if step.name == "package_build")
        self.assertNotIn("SOURCE_DATE_EPOCH", package.env)

    def test_source_date_epoch_cli_rejects_values_outside_gzip_range(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--source-date-epoch", "-1"])
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--source-date-epoch", "4294967296"])

    def test_harness_tolerates_missing_captured_output(self) -> None:
        options = HarnessOptions(
            project_root=Path.cwd(),
            mode="mcp",
            skip_console_check=True,
            skip_mcp_smoke=True,
            skip_mcp_transport_smoke=True,
        )

        with patch(
            "scripts.run_release_harness.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["ok"], returncode=0, stdout=None, stderr=None),
        ) as run_process:
            report = run_harness(options)

        self.assertTrue(report["passed"])
        self.assertEqual("", report["steps"][0]["stdout_tail"])
        self.assertEqual("", report["steps"][0]["stderr_tail"])
        _, kwargs = run_process.call_args
        self.assertEqual("utf-8", kwargs["encoding"])
        self.assertEqual("replace", kwargs["errors"])
        self.assertEqual("utf-8", kwargs["env"]["PYTHONIOENCODING"])
        python_call_kwargs = next(
            call_kwargs
            for call_args, call_kwargs in reversed(run_process.call_args_list)
            if Path(str(call_args[0][0])).name.lower().startswith("python")
        )
        self.assertEqual(
            str(Path(sys.executable).resolve().parent),
            python_call_kwargs["env"]["PATH"].split(";", 1)[0],
        )


if __name__ == "__main__":
    unittest.main()
