from __future__ import annotations

import io
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_mcp_windows_execution_matrix import (
    CLAUDE_CODE_STDIO,
    CLAUDE_DESKTOP_CONFIG,
    CONNECT_SCRIPT,
    INSTALL_SCRIPT,
    RUNTIME_IDENTITY_MODULES,
    RUNTIME_MARKER,
    STDIO_LAUNCHER,
    CommandResult,
    ScenarioSpec,
    run,
    run_execution_matrix,
)


INSTALL_FIXTURE = """\
param([string]$PackagePath = "")
# The injectable test runner simulates this script's verified side effects.
"""

LAUNCHER_FIXTURE = """\
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ServerArgs)
# The injectable test runner simulates runtime selection.
"""


def _write_bundle(root: Path, *, claude_code_add_stdio_contract: bool = True) -> Path:
    bundle = root / "bundle"
    bundle.mkdir()
    (bundle / INSTALL_SCRIPT).write_text(INSTALL_FIXTURE, encoding="utf-8")
    (bundle / STDIO_LAUNCHER).write_text(LAUNCHER_FIXTURE, encoding="utf-8")
    (bundle / CLAUDE_DESKTOP_CONFIG).write_text(
        json.dumps(
            {
                "mcpServers": {
                    "direct-matrix": {
                        "command": "python.exe",
                        "args": ["-m", "scripts.run_regulation_mcp"],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    contract_line = (
        "claude mcp add --transport stdio --scope user aks_mcp --no-warm-cache\n"
        if claude_code_add_stdio_contract
        else "Write-Host \"not-a-claude-code-stdio-contract\"\n"
    )
    (bundle / CLAUDE_CODE_STDIO).write_text(contract_line, encoding="utf-8")
    (bundle / CONNECT_SCRIPT).write_text(
        "param([switch]$InstallPackage)\nWrite-Host \"bundle connect helper\"\n",
        encoding="utf-8",
    )
    return bundle


class SimulatedRunner:
    def __init__(self, *, failing_scenario: str | None = None) -> None:
        self.failing_scenario = failing_scenario
        self.calls: list[ScenarioSpec] = []

    def __call__(self, spec: ScenarioSpec) -> CommandResult:
        self.calls.append(spec)
        if spec.scenario_id == self.failing_scenario:
            return CommandResult(returncode=9, stderr="simulated failure")
        if spec.scenario_id in {"py_only_install", "scripts_path_absent"}:
            marker = spec.artifacts["marker"]
            expected_python = spec.artifacts["expected_python"]
            module_sha256 = {
                name: "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()
                for name in RUNTIME_IDENTITY_MODULES
            }
            canonical = json.dumps(
                module_sha256,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "python_executable": str(expected_python.resolve()),
                        "minimum_python": "3.11",
                        "package_import": "scripts.run_regulation_mcp",
                        "identity_scope": "mcp-command-modules-v1",
                        "hash_algorithm": "sha256",
                        "module_sha256": module_sha256,
                        "build_identity_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                        "written_at": "2026-07-21T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
        elif spec.scenario_id == "runtime_marker_precedes_path_python":
            spec.artifacts["selected_log"].write_text("selected\n", encoding="utf-8")
        elif spec.scenario_id == "corrupt_runtime_marker_fails_closed":
            return CommandResult(returncode=7, stderr="invalid marker")
        elif spec.scenario_id == "missing_runtime_marker_fallback":
            spec.artifacts["fallback_log"].write_text("fallback\n", encoding="utf-8")
        return CommandResult(returncode=0, stdout="simulated")


class WindowsExecutionMatrixTests(unittest.TestCase):
    def test_missing_temp_root_is_created_and_temporary_workspace_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requested_temp_root = root / "new temp root" / "nested"

            report = run_execution_matrix(
                runner=SimulatedRunner(),
                bundle_dir=_write_bundle(root),
                powershell="powershell.exe",
                temp_root=requested_temp_root,
            )

            self.assertTrue(report["passed"])
            self.assertTrue(requested_temp_root.is_dir())
            self.assertEqual([], list(requested_temp_root.iterdir()))

    def test_direct_repo_root_script_execution_can_generate_its_bundle(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "scripts" / "run_mcp_windows_execution_matrix.py"),
                    "--powershell",
                    "",
                    "--temp-root",
                    tmp,
                ],
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual("mcp_windows_execution_matrix", json.loads(completed.stdout)["report_type"])

    def test_injected_runner_covers_all_subprocess_scenarios_without_touching_user_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _write_bundle(root)
            runner = SimulatedRunner()

            report = run_execution_matrix(
                runner=runner,
                bundle_dir=bundle,
                powershell="powershell.exe",
                temp_root=root,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(6, report["scenario_count"])
        self.assertEqual(5, len(runner.calls))
        self.assertEqual(
            {
                "py_only_install",
                "scripts_path_absent",
                "runtime_marker_precedes_path_python",
                "corrupt_runtime_marker_fails_closed",
                "missing_runtime_marker_fallback",
            },
            {call.scenario_id for call in runner.calls},
        )
        self.assertFalse(report["destructive_actions_performed"])
        self.assertFalse(report["desktop_processes_touched"])
        self.assertFalse(report["user_config_touched"])
        for call in runner.calls:
            self.assertIn("sandbox-user", call.env["USERPROFILE"])
            self.assertIn("sandbox-user", call.env["CODEX_HOME"])
        for scenario in report["scenarios"]:
            self.assertFalse(scenario["desktop_process_touched"])
            self.assertFalse(scenario["user_config_touched"])

    def test_py_only_and_scripts_path_scenarios_require_recorded_selected_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _write_bundle(root)
            report = run_execution_matrix(
                runner=SimulatedRunner(),
                bundle_dir=bundle,
                powershell="powershell.exe",
                temp_root=root,
            )

        scenarios = {item["scenario_id"]: item for item in report["scenarios"]}
        self.assertTrue(scenarios["py_only_install"]["observed"]["selected_py_launcher_runtime"])
        self.assertTrue(scenarios["py_only_install"]["observed"]["runtime_marker_schema_v2_identity_valid"])
        self.assertTrue(scenarios["scripts_path_absent"]["observed"]["selected_path_python_runtime"])
        self.assertTrue(scenarios["scripts_path_absent"]["observed"]["runtime_marker_schema_v2_identity_valid"])
        self.assertFalse(scenarios["scripts_path_absent"]["observed"]["scripts_initially_on_path"])

    def test_install_scenarios_reject_legacy_schema_one_marker(self) -> None:
        class LegacyMarkerRunner(SimulatedRunner):
            def __call__(self, spec: ScenarioSpec) -> CommandResult:
                result = super().__call__(spec)
                if spec.scenario_id in {"py_only_install", "scripts_path_absent"}:
                    marker = spec.artifacts["marker"]
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    payload["schema_version"] = 1
                    marker.write_text(json.dumps(payload), encoding="utf-8")
                return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=LegacyMarkerRunner(),
                bundle_dir=_write_bundle(root),
                powershell="powershell.exe",
                temp_root=root,
            )

        scenarios = {item["scenario_id"]: item for item in report["scenarios"]}
        self.assertFalse(report["passed"])
        self.assertFalse(scenarios["py_only_install"]["passed"])
        self.assertFalse(scenarios["scripts_path_absent"]["passed"])

    def test_install_scenarios_reject_module_set_and_aggregate_drift(self) -> None:
        class DriftedMarkerRunner(SimulatedRunner):
            def __call__(self, spec: ScenarioSpec) -> CommandResult:
                result = super().__call__(spec)
                if spec.scenario_id == "py_only_install":
                    marker = spec.artifacts["marker"]
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    payload["module_sha256"]["scripts.unexpected_module"] = "sha256:" + ("0" * 64)
                    marker.write_text(json.dumps(payload), encoding="utf-8")
                elif spec.scenario_id == "scripts_path_absent":
                    marker = spec.artifacts["marker"]
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    payload["build_identity_sha256"] = "sha256:" + ("f" * 64)
                    marker.write_text(json.dumps(payload), encoding="utf-8")
                return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=DriftedMarkerRunner(),
                bundle_dir=_write_bundle(root),
                powershell="powershell.exe",
                temp_root=root,
            )

        scenarios = {item["scenario_id"]: item for item in report["scenarios"]}
        self.assertFalse(report["passed"])
        self.assertFalse(scenarios["py_only_install"]["passed"])
        self.assertFalse(scenarios["scripts_path_absent"]["passed"])

    def test_runtime_marker_must_precede_other_path_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=SimulatedRunner(),
                bundle_dir=_write_bundle(root),
                powershell="powershell.exe",
                temp_root=root,
            )

        scenario = next(
            item for item in report["scenarios"] if item["scenario_id"] == "runtime_marker_precedes_path_python"
        )
        self.assertTrue(scenario["passed"])
        self.assertTrue(scenario["observed"]["recorded_runtime_invoked"])
        self.assertFalse(scenario["observed"]["path_python_invoked"])

    def test_corrupt_marker_fails_closed_but_missing_marker_allows_compatibility_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=SimulatedRunner(),
                bundle_dir=_write_bundle(root),
                powershell="powershell.exe",
                temp_root=root,
            )

        scenarios = {item["scenario_id"]: item for item in report["scenarios"]}
        corrupt = scenarios["corrupt_runtime_marker_fails_closed"]
        self.assertTrue(corrupt["passed"])
        self.assertTrue(corrupt["observed"]["failed_closed"])
        self.assertFalse(corrupt["observed"]["path_python_fallback_invoked"])
        missing = scenarios["missing_runtime_marker_fallback"]
        self.assertTrue(missing["passed"])
        self.assertTrue(missing["observed"]["path_python_fallback_invoked"])

    def test_direct_client_artifacts_contract_fails_if_claude_code_stdio_contract_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=SimulatedRunner(),
                bundle_dir=_write_bundle(root, claude_code_add_stdio_contract=False),
                powershell="powershell.exe",
                temp_root=root,
            )

        scenario = next(item for item in report["scenarios"] if item["scenario_id"] == "direct_client_artifacts_contract")
        self.assertFalse(report["passed"])
        self.assertFalse(scenario["passed"])
        self.assertFalse(scenario["observed"]["claude_code_add_stdio_contract"])

    def test_runner_failure_is_structured_without_subprocess_output_or_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=SimulatedRunner(failing_scenario="runtime_marker_precedes_path_python"),
                bundle_dir=_write_bundle(root),
                powershell="powershell.exe",
                temp_root=root,
            )

        serialized = json.dumps(report, ensure_ascii=False)
        self.assertFalse(report["passed"])
        self.assertEqual(1, report["failed_count"])
        self.assertNotIn("simulated failure", serialized)
        self.assertNotIn(str(root), serialized)

    def test_powershell_unavailable_skips_execution_but_still_checks_direct_client_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_execution_matrix(
                runner=SimulatedRunner(),
                bundle_dir=_write_bundle(root),
                powershell="",
                temp_root=root,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(5, report["skipped_count"])
        self.assertEqual(1, report["passed_count"])

    def test_cli_fail_gate_uses_injected_runner_and_writes_path_free_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _write_bundle(root)
            output_path = root / "matrix.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--bundle-dir",
                    str(bundle),
                    "--powershell",
                    "powershell.exe",
                    "--temp-root",
                    str(root),
                    "--out-json",
                    str(output_path),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                runner=SimulatedRunner(),
            )
            payload = json.loads(stdout.getvalue())
            written = output_path.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["passed"])
        self.assertNotIn(str(bundle), written)
        self.assertFalse(payload["user_config_touched"])

    def test_required_bundle_file_failure_does_not_call_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _write_bundle(root)
            (bundle / STDIO_LAUNCHER).unlink()
            runner = SimulatedRunner()

            report = run_execution_matrix(
                runner=runner,
                bundle_dir=bundle,
                powershell="powershell.exe",
                temp_root=root,
            )

        self.assertFalse(report["passed"])
        self.assertEqual([], runner.calls)
        self.assertEqual("required_bundle_files_missing", report["scenarios"][0]["reason_code"])


if __name__ == "__main__":
    unittest.main()
