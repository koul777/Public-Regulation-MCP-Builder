from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.generate_mcp_client_config import (
    RUNTIME_IDENTITY_MODULES as GENERATED_RUNTIME_IDENTITY_MODULES,
)

INSTALL_SCRIPT = "install_local_package.ps1"
STDIO_LAUNCHER = "run_mcp_stdio_server.ps1"
CLAUDE_DESKTOP_BAT = "Claude Desktop에 연결하기.bat"
CLAUDE_CODE_BAT = "Claude Code에 연결하기.bat"
RUNTIME_MARKER = "runtime_python.json"
RUNTIME_IDENTITY_MODULES = tuple(GENERATED_RUNTIME_IDENTITY_MODULES)
MCP_COMMANDS = (
    "reg-rag-mcp-server",
    "reg-rag-mcp-config",
    "reg-rag-mcp-doctor",
    "reg-rag-mcp-smoke",
    "reg-rag-mcp-codex-app-server-check",
    "reg-rag-mcp-desktop-recognition-check",
    "reg-rag-mcp-client-config-smoke",
    "reg-rag-mcp-index-visibility",
)


def _fake_runtime_marker_payload(python_executable: Path) -> dict[str, Any]:
    placeholder_hash = "sha256:" + ("0" * 64)
    module_sha256 = {name: placeholder_hash for name in RUNTIME_IDENTITY_MODULES}
    canonical = json.dumps(
        module_sha256,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 2,
        "python_executable": str(python_executable.resolve()),
        "minimum_python": "3.11",
        "package_import": "scripts.run_regulation_mcp",
        "identity_scope": "mcp-command-modules-v1",
        "hash_algorithm": "sha256",
        "module_sha256": module_sha256,
        "build_identity_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "written_at": "2026-01-01T00:00:00+00:00",
    }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    artifacts: Mapping[str, Path] = field(default_factory=dict)


Runner = Callable[[ScenarioSpec], CommandResult]


def subprocess_runner(spec: ScenarioSpec) -> CommandResult:
    """Run one isolated scenario; injectable in tests and other harnesses."""

    try:
        completed = subprocess.run(
            list(spec.command),
            cwd=spec.cwd,
            env=dict(spec.env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=124,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(returncode=127, stderr=exc.__class__.__name__)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def run_execution_matrix(
    *,
    runner: Runner = subprocess_runner,
    bundle_dir: Path | None = None,
    powershell: str | None = None,
    temp_root: Path | None = None,
) -> dict[str, Any]:
    """Run destructive-free Windows MCP launcher scenarios in one temp tree.

    No connection BAT is executed. The Claude BAT scenario only inspects the
    staged text, so neither Desktop processes nor user MCP configuration can be
    changed by this harness.
    """

    powershell_command = (
        shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None
        else str(powershell).strip() or None
    )
    if temp_root is not None:
        temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcp-windows-matrix-", dir=temp_root) as tmp:
        root = Path(tmp)
        source_bundle = bundle_dir or _generate_bundle(root / "generated-source")
        staged_bundle = _stage_bundle(source_bundle, root / "staged-bundle")
        missing_files = [
            name
            for name in (INSTALL_SCRIPT, STDIO_LAUNCHER, CLAUDE_DESKTOP_BAT, CLAUDE_CODE_BAT)
            if not (staged_bundle / name).is_file()
        ]
        if missing_files:
            scenarios = [
                _scenario_result(
                    "bundle_contract",
                    passed=False,
                    reason_code="required_bundle_files_missing",
                    observed={"missing_file_count": len(missing_files)},
                )
            ]
            return _matrix_report(scenarios, windows_supported=bool(powershell_command))

        scenarios: list[dict[str, Any]] = []
        if powershell_command:
            scenarios.extend(
                [
                    _run_py_only_install(staged_bundle, root, powershell_command, runner),
                    _run_scripts_path_absent(staged_bundle, root, powershell_command, runner),
                    _run_runtime_marker_priority(staged_bundle, root, powershell_command, runner),
                    _run_marker_fallback(
                        staged_bundle,
                        root,
                        powershell_command,
                        runner,
                        corrupt=True,
                    ),
                    _run_marker_fallback(
                        staged_bundle,
                        root,
                        powershell_command,
                        runner,
                        corrupt=False,
                    ),
                ]
            )
        else:
            for scenario_id in (
                "py_only_install",
                "scripts_path_absent",
                "runtime_marker_precedes_path_python",
                "corrupt_runtime_marker_fails_closed",
                "missing_runtime_marker_fallback",
            ):
                scenarios.append(
                    _scenario_result(
                        scenario_id,
                        passed=False,
                        status="skipped",
                        reason_code="powershell_unavailable",
                    )
                )
        scenarios.append(_check_claude_bat_install_package(staged_bundle))
        return _matrix_report(scenarios, windows_supported=bool(powershell_command))


def _generate_bundle(output_dir: Path) -> Path:
    from scripts.generate_mcp_client_config import build_mcp_client_config, write_mcp_setup_bundle

    config = build_mcp_client_config(
        server_name="windows-matrix",
        client_profile="bundle",
        tenant_id="matrix-tenant",
    )
    write_mcp_setup_bundle(config, output_dir, server_name="windows-matrix")
    return output_dir


def _stage_bundle(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for name in (INSTALL_SCRIPT, STDIO_LAUNCHER, CLAUDE_DESKTOP_BAT, CLAUDE_CODE_BAT):
        source_path = source / name
        if source_path.is_file():
            shutil.copy2(source_path, destination / name)
    (destination / "data").mkdir(exist_ok=True)
    return destination


def _base_environment(root: Path, *, path_entries: Sequence[Path]) -> dict[str, str]:
    windows_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
    sandbox_home = root / "sandbox-user"
    sandbox_temp = root / "temp"
    for directory in (
        sandbox_home,
        sandbox_temp,
        sandbox_home / "AppData" / "Roaming",
        sandbox_home / "AppData" / "Local",
        sandbox_home / ".codex",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    environment = {
        "SystemRoot": str(windows_dir),
        "WINDIR": str(windows_dir),
        "COMSPEC": str(windows_dir / "System32" / "cmd.exe"),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PATH": os.pathsep.join(
            [
                *(str(path) for path in path_entries),
                str(windows_dir / "System32"),
                str(powershell_dir),
            ]
        ),
        "USERPROFILE": str(sandbox_home),
        "HOME": str(sandbox_home),
        "APPDATA": str(sandbox_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(sandbox_home / "AppData" / "Local"),
        "CODEX_HOME": str(sandbox_home / ".codex"),
        "TEMP": str(sandbox_temp),
        "TMP": str(sandbox_temp),
    }
    return environment


def _powershell_command(powershell: str, script: Path, *arguments: str) -> tuple[str, ...]:
    return (
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
    )


def _write_fake_runtime(runtime: Path, scripts_dir: Path, invocation_log: Path | None = None) -> None:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    log_line = ""
    if invocation_log is not None:
        log_name = invocation_log.name
        log_line = (
            f'if "%1"=="-m" echo %*>"{log_name}"& exit /b 0\n'
            f'if not "%1"=="-c" echo %*>"{log_name}"& exit /b 0\n'
        )
    marker_payload = _fake_runtime_marker_payload(runtime)
    identity_json = json.dumps(
        {
            "module_sha256": marker_payload["module_sha256"],
            "build_identity_sha256": marker_payload["build_identity_sha256"],
        },
        separators=(",", ":"),
    )
    scripts_dir_base64 = base64.b64encode(str(scripts_dir).encode("utf-8")).decode("ascii")
    runtime.write_text(
        "@echo off\n"
        + log_line
        + "if \"%1\"==\"-m\" exit /b 0\n"
        + "if \"%1\"==\"-c\" if not \"%~4\"==\"\" goto identity\n"
        + f"if \"%1\"==\"-c\" echo {scripts_dir_base64}& exit /b 0\n"
        + "exit /b 1\n"
        + ":identity\n"
        + f"echo {identity_json}\n"
        + "exit /b 0\n",
        encoding="utf-8",
    )


def _write_fake_console_commands(scripts_dir: Path) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for command in MCP_COMMANDS:
        (scripts_dir / f"{command}.cmd").write_text("@exit /b 0\n", encoding="utf-8")


def _runtime_marker_v2_matches(marker_path: Path, expected_python: Path) -> bool:
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in (
            ("schema_version", 2),
            ("minimum_python", "3.11"),
            ("package_import", "scripts.run_regulation_mcp"),
            ("identity_scope", "mcp-command-modules-v1"),
            ("hash_algorithm", "sha256"),
        )
    ):
        return False
    python_executable = payload.get("python_executable")
    if not isinstance(python_executable, str) or not python_executable:
        return False
    try:
        if Path(python_executable).resolve() != expected_python.resolve():
            return False
    except (OSError, RuntimeError):
        return False
    module_sha256 = payload.get("module_sha256")
    if not isinstance(module_sha256, dict) or set(module_sha256) != set(RUNTIME_IDENTITY_MODULES):
        return False
    hash_pattern = re.compile(r"sha256:[0-9a-f]{64}\Z")
    if any(not isinstance(value, str) or not hash_pattern.fullmatch(value) for value in module_sha256.values()):
        return False
    build_identity = payload.get("build_identity_sha256")
    if not isinstance(build_identity, str) or not hash_pattern.fullmatch(build_identity):
        return False
    canonical = json.dumps(
        module_sha256,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_build_identity = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return build_identity == expected_build_identity


def _run_py_only_install(bundle: Path, root: Path, powershell: str, runner: Runner) -> dict[str, Any]:
    scenario_root = root / "py-only"
    fake_bin = scenario_root / "bin"
    runtime = scenario_root / "selected-python" / "python.cmd"
    scripts_dir = runtime.parent / "Scripts"
    fake_bin.mkdir(parents=True)
    _write_fake_runtime(runtime, scripts_dir)
    _write_fake_console_commands(scripts_dir)
    (fake_bin / "py.cmd").write_text(
        "@echo off\n"
        "if not \"%1\"==\"-3.11\" exit /b 1\n"
        + f"echo {base64.b64encode(str(runtime.resolve()).encode('utf-8')).decode('ascii')}\n"
        + "exit /b 0\n",
        encoding="utf-8",
    )
    package_path = scenario_root / "dummy package.whl"
    package_path.write_bytes(b"matrix-fixture")
    marker = bundle / RUNTIME_MARKER
    marker.unlink(missing_ok=True)
    spec = ScenarioSpec(
        "py_only_install",
        _powershell_command(
            powershell,
            bundle / INSTALL_SCRIPT,
            "-PackagePath",
            str(package_path),
        ),
        scenario_root,
        _base_environment(scenario_root, path_entries=[fake_bin]),
        {"marker": marker, "expected_python": runtime},
    )
    result = runner(spec)
    marker_matches = _runtime_marker_v2_matches(marker, runtime)
    return _scenario_result(
        spec.scenario_id,
        passed=result.returncode == 0 and marker_matches,
        reason_code=(
            "py_launcher_selected_and_recorded"
            if result.returncode == 0 and marker_matches
            else "py_only_install_failed"
        ),
        observed={
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "runtime_marker_written": marker.is_file(),
            "selected_py_launcher_runtime": marker_matches,
            "runtime_marker_schema_v2_identity_valid": marker_matches,
        },
    )


def _run_scripts_path_absent(bundle: Path, root: Path, powershell: str, runner: Runner) -> dict[str, Any]:
    scenario_root = root / "scripts-path-absent"
    fake_bin = scenario_root / "bin"
    runtime = fake_bin / "python.cmd"
    scripts_dir = scenario_root / "runtime" / "Scripts"
    fake_bin.mkdir(parents=True)
    _write_fake_runtime(runtime, scripts_dir)
    _write_fake_console_commands(scripts_dir)
    package_path = scenario_root / "dummy package.whl"
    package_path.write_bytes(b"matrix-fixture")
    marker = bundle / RUNTIME_MARKER
    marker.unlink(missing_ok=True)
    environment = _base_environment(scenario_root, path_entries=[fake_bin])
    scripts_initially_absent = str(scripts_dir).casefold() not in {
        entry.casefold() for entry in environment["PATH"].split(os.pathsep)
    }
    spec = ScenarioSpec(
        "scripts_path_absent",
        _powershell_command(
            powershell,
            bundle / INSTALL_SCRIPT,
            "-PackagePath",
            str(package_path),
        ),
        scenario_root,
        environment,
        {"marker": marker, "expected_python": runtime},
    )
    result = runner(spec)
    marker_matches = _runtime_marker_v2_matches(marker, runtime)
    passed = result.returncode == 0 and scripts_initially_absent and marker_matches
    return _scenario_result(
        spec.scenario_id,
        passed=passed,
        reason_code="scripts_path_discovered" if passed else "scripts_path_discovery_failed",
        observed={
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "scripts_initially_on_path": not scripts_initially_absent,
            "runtime_marker_written": marker.is_file(),
            "selected_path_python_runtime": marker_matches,
            "runtime_marker_schema_v2_identity_valid": marker_matches,
        },
    )


def _run_runtime_marker_priority(bundle: Path, root: Path, powershell: str, runner: Runner) -> dict[str, Any]:
    scenario_root = root / "marker-priority"
    recorded_log = scenario_root / "recorded.log"
    path_log = scenario_root / "path.log"
    recorded_runtime = scenario_root / "recorded" / "python.cmd"
    path_bin = scenario_root / "path-bin"
    path_runtime = path_bin / "python.cmd"
    _write_fake_runtime(recorded_runtime, recorded_runtime.parent / "Scripts", recorded_log)
    _write_fake_runtime(path_runtime, path_runtime.parent / "Scripts", path_log)
    marker = bundle / RUNTIME_MARKER
    marker.write_text(
        json.dumps(_fake_runtime_marker_payload(recorded_runtime)),
        encoding="utf-8",
    )
    spec = ScenarioSpec(
        "runtime_marker_precedes_path_python",
        _powershell_command(powershell, bundle / STDIO_LAUNCHER),
        scenario_root,
        _base_environment(scenario_root, path_entries=[path_bin]),
        {"selected_log": recorded_log, "rejected_log": path_log},
    )
    result = runner(spec)
    selected = recorded_log.is_file()
    path_python_used = path_log.is_file()
    passed = result.returncode == 0 and selected and not path_python_used
    return _scenario_result(
        spec.scenario_id,
        passed=passed,
        reason_code="runtime_marker_precedence_verified" if passed else "runtime_marker_precedence_failed",
        observed={
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "recorded_runtime_invoked": selected,
            "path_python_invoked": path_python_used,
        },
    )


def _run_marker_fallback(
    bundle: Path,
    root: Path,
    powershell: str,
    runner: Runner,
    *,
    corrupt: bool,
) -> dict[str, Any]:
    suffix = "corrupt" if corrupt else "missing"
    scenario_root = root / f"marker-{suffix}"
    invocation_log = scenario_root / "path-python.log"
    path_bin = scenario_root / "path-bin"
    path_runtime = path_bin / "python.cmd"
    _write_fake_runtime(path_runtime, path_runtime.parent / "Scripts", invocation_log)
    marker = bundle / RUNTIME_MARKER
    if corrupt:
        marker.write_text('{"schema_version":1,"python_executable":', encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)
    scenario_id = "corrupt_runtime_marker_fails_closed" if corrupt else "missing_runtime_marker_fallback"
    environment = _base_environment(scenario_root, path_entries=[path_bin])
    if not corrupt:
        # Make the pre-install compatibility path deterministic even when the
        # caller places the temporary workspace inside a source checkout.
        environment["REG_RAG_PYTHON"] = str(path_runtime)
    spec = ScenarioSpec(
        scenario_id,
        _powershell_command(powershell, bundle / STDIO_LAUNCHER),
        scenario_root,
        environment,
        {"fallback_log": invocation_log},
    )
    result = runner(spec)
    fallback_used = invocation_log.is_file()
    passed = (
        result.returncode != 0 and not fallback_used
        if corrupt
        else result.returncode == 0 and fallback_used
    )
    return _scenario_result(
        scenario_id,
        passed=passed,
        reason_code=(
            "corrupt_marker_failed_closed"
            if corrupt and passed
            else "missing_marker_fell_back_to_compatible_python"
            if passed
            else f"{suffix}_marker_contract_failed"
        ),
        observed={
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "path_python_fallback_invoked": fallback_used,
            "compatible_python_fallback_invoked": fallback_used,
            "failed_closed": bool(corrupt and result.returncode != 0 and not fallback_used),
        },
    )


def _check_claude_bat_install_package(bundle: Path) -> dict[str, Any]:
    results: dict[str, bool] = {}
    for label, filename, target in (
        ("claude_desktop", CLAUDE_DESKTOP_BAT, "-Target claude-desktop"),
        ("claude_code", CLAUDE_CODE_BAT, "-Target claude-code"),
    ):
        try:
            text = (bundle / filename).read_text(encoding="utf-8-sig")
        except OSError:
            results[label] = False
            continue
        install_index = text.find("-InstallPackage")
        target_index = text.find(target)
        results[label] = install_index >= 0 and target_index >= 0 and install_index < target_index
    passed = all(results.values())
    return _scenario_result(
        "claude_bat_installs_package",
        passed=passed,
        reason_code="claude_bats_install_before_registration" if passed else "claude_bat_missing_install_package",
        observed={
            "claude_desktop_install_package": results.get("claude_desktop", False),
            "claude_code_install_package": results.get("claude_code", False),
            "bat_executed": False,
        },
    )


def _scenario_result(
    scenario_id: str,
    *,
    passed: bool,
    reason_code: str,
    observed: Mapping[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "status": status or ("passed" if passed else "failed"),
        "passed": bool(passed),
        "reason_code": reason_code,
        "observed": dict(observed or {}),
        "destructive": False,
        "desktop_process_touched": False,
        "user_config_touched": False,
    }


def _matrix_report(scenarios: Sequence[dict[str, Any]], *, windows_supported: bool) -> dict[str, Any]:
    passed_count = sum(item.get("status") == "passed" for item in scenarios)
    failed_count = sum(item.get("status") == "failed" for item in scenarios)
    skipped_count = sum(item.get("status") == "skipped" for item in scenarios)
    return {
        "report_type": "mcp_windows_execution_matrix",
        "passed": failed_count == 0 and skipped_count == 0,
        "windows_execution_supported": windows_supported,
        "temporary_workspace_used": True,
        "temporary_workspace_removed_after_run": True,
        "destructive_actions_performed": False,
        "desktop_processes_touched": False,
        "user_config_touched": False,
        "path_details_redacted": True,
        "scenario_count": len(scenarios),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "scenarios": list(scenarios),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an isolated, non-destructive Windows MCP execution matrix."
    )
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--powershell")
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, runner: Runner = subprocess_runner) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    report = run_execution_matrix(
        runner=runner,
        bundle_dir=args.bundle_dir,
        powershell=args.powershell,
        temp_root=args.temp_root,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    output.write(rendered)
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
