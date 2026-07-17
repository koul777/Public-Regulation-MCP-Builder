from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True)
class HarnessStep:
    name: str
    command: tuple[str, ...]
    required: bool = True
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "required": self.required,
            "env_keys": sorted(self.env),
        }


@dataclass(frozen=True)
class HarnessOptions:
    project_root: Path
    artifact_dir: Path | None = None
    build_python: Path | None = None
    source_date_epoch: int | None = None
    real_parser_fixture_root: Path | None = None
    require_real_parser_fixtures: bool = False
    mode: str = "internal"
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "https://mcp.example.invalid/mcp"
    server_name: str = "regulation_mcp"
    tenant_id: str = "default"
    bundle_dir: Path = Path("reports/mcp_connection_bundle_harness")
    bundle_zip: Path = Path("reports/mcp_connection_bundle_harness.zip")
    bundle_json: Path = Path("reports/mcp_client_bundle_harness.json")
    mcp_bundle_profile_id: str | None = None
    mcp_bundle_document_id: str | None = None
    console_json: Path = Path("reports/installed_console_scripts_harness.json")
    mcp_smoke_json: Path = Path("reports/mcp_smoke_harness.json")
    mcp_transport_smoke_json: Path = Path("reports/mcp_transport_smoke_harness.json")
    mcp_index_visibility_json: Path = Path("reports/mcp_index_visibility_harness.json")
    mcp_bundle_local_stdio_doctor_json: Path = Path("reports/mcp_connection_readiness_bundle_local_stdio_harness.json")
    mcp_bundle_client_config_smoke_json: Path = Path("reports/mcp_client_config_smoke_bundle_harness.json")
    mcp_bundle_zip_extract_smoke_json: Path = Path("reports/mcp_bundle_zip_extract_smoke_harness.json")
    mcp_bundle_zip_extract_dir: Path = Path("reports/mcp_connection_bundle_zip_extract_harness")
    mcp_bundle_transport_smoke_json: Path = Path("reports/mcp_transport_smoke_bundle_harness.json")
    mcp_runtime_data_dir: Path | None = None
    tenant_storage_isolation: bool = False
    mcp_min_visible_records: int = 1
    bundle_doctor_json: Path = Path("reports/mcp_connection_readiness_bundle_harness.json")
    chatgpt_https_json: Path = Path("reports/mcp_connection_readiness_chatgpt_https_harness.json")
    chatgpt_tunnel_json: Path = Path("reports/mcp_connection_readiness_chatgpt_tunnel_harness.json")
    public_audit_json: Path = Path("reports/public_release_readiness_audit.harness.json")
    cleanup_json: Path = Path("reports/public_release_cleanup_plan.harness.json")
    cleanup_md: Path = Path("reports/public_release_cleanup_plan.harness.md")
    sdist_rehearsal_json: Path = Path("reports/sdist_rehearsal_harness.json")
    sdist_normalization_json: Path = Path("reports/sdist_normalization_harness.json")
    release_tree_clean_json: Path = Path("reports/release_tree_clean_harness.json")
    real_parser_fixtures_json: Path = Path("reports/real_parser_fixtures_harness.json")
    real_parser_fixtures_md: Path = Path("reports/real_parser_fixtures_harness.md")
    build_dist_dir: Path = Path("dist")
    skip_tests: bool = False
    skip_build: bool = False
    skip_sdist_rehearsal: bool = False
    skip_console_check: bool = False
    skip_mcp_smoke: bool = False
    skip_mcp_transport_smoke: bool = False
    skip_public_audit: bool = False
    require_tunnel_client: bool = False
    include_wheel_in_bundle: bool = False
    probe_public_url: bool = False
    allow_dirty_build: bool = False


def _path_arg(root: Path, value: Path) -> str:
    path = value if value.is_absolute() else root / value
    return str(path)


def _source_date_epoch_arg(value: str) -> int:
    try:
        epoch = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("SOURCE_DATE_EPOCH must be an integer.") from exc
    if not 0 <= epoch <= (1 << 32) - 1:
        raise argparse.ArgumentTypeError("SOURCE_DATE_EPOCH must be between 0 and 4294967295.")
    return epoch


_SCOPED_OUTPUT_FIELDS = (
    "bundle_dir",
    "bundle_zip",
    "bundle_json",
    "console_json",
    "mcp_smoke_json",
    "mcp_transport_smoke_json",
    "mcp_index_visibility_json",
    "mcp_bundle_local_stdio_doctor_json",
    "mcp_bundle_client_config_smoke_json",
    "mcp_bundle_zip_extract_smoke_json",
    "mcp_bundle_zip_extract_dir",
    "mcp_bundle_transport_smoke_json",
    "bundle_doctor_json",
    "chatgpt_https_json",
    "chatgpt_tunnel_json",
    "public_audit_json",
    "cleanup_json",
    "cleanup_md",
    "sdist_rehearsal_json",
    "sdist_normalization_json",
    "release_tree_clean_json",
    "real_parser_fixtures_json",
    "real_parser_fixtures_md",
)


def _scope_artifact_outputs(options: HarnessOptions) -> HarnessOptions:
    artifact_dir = options.artifact_dir
    if artifact_dir is None:
        return options
    updates: dict[str, Path] = {}
    for field_name in _SCOPED_OUTPUT_FIELDS:
        value = Path(getattr(options, field_name))
        if value.is_absolute():
            continue
        try:
            relative = value.relative_to("reports")
        except ValueError:
            continue
        updates[field_name] = artifact_dir / relative
    if options.build_dist_dir == Path("dist"):
        updates["build_dist_dir"] = artifact_dir / "dist"
    return replace(options, **updates)


def build_harness_steps(options: HarnessOptions) -> list[HarnessStep]:
    options = _scope_artifact_outputs(options)
    root = options.project_root
    python = sys.executable
    build_python = _path_arg(root, options.build_python) if options.build_python is not None else python
    if options.source_date_epoch is not None and not 0 <= options.source_date_epoch <= (1 << 32) - 1:
        raise ValueError("source_date_epoch must be between 0 and 4294967295.")
    build_env = (
        {"SOURCE_DATE_EPOCH": str(options.source_date_epoch)}
        if options.source_date_epoch is not None
        else {}
    )
    steps: list[HarnessStep] = []

    if not options.skip_build and not options.allow_dirty_build:
        steps.append(
            HarnessStep(
                "release_tree_clean",
                (
                    python,
                    "scripts/check_release_tree_clean.py",
                    "--project-root",
                    str(root),
                    "--out-json",
                    _path_arg(root, options.release_tree_clean_json),
                ),
            )
        )
    # Source-only public releases must not require non-redistributable parser inputs.
    # Product/release validation can opt in explicitly with --require-real-parser-fixtures.
    if options.require_real_parser_fixtures:
        fixture_command = [
            python,
            "scripts/verify_real_parser_regression_fixtures.py",
            "--project-root",
            str(root),
            "--out-json",
            _path_arg(root, options.real_parser_fixtures_json),
            "--out-md",
            _path_arg(root, options.real_parser_fixtures_md),
        ]
        if options.real_parser_fixture_root is not None:
            fixture_command.extend(
                ["--fixture-root", _path_arg(root, options.real_parser_fixture_root)]
            )
        steps.append(HarnessStep("real_parser_fixture_gate", tuple(fixture_command)))
    if not options.skip_tests:
        steps.append(HarnessStep("unit_tests", (python, "-m", "unittest", "discover", "-q")))
    if not options.skip_build:
        steps.append(
            HarnessStep(
                "package_build",
                (
                    build_python,
                    "-m",
                    "build",
                    "--sdist",
                    "--wheel",
                    "--outdir",
                    _path_arg(root, options.build_dist_dir),
                ),
                env=build_env,
            )
        )
        if options.source_date_epoch is not None:
            steps.append(
                HarnessStep(
                    "normalize_sdist",
                    (
                        python,
                        "scripts/normalize_sdist.py",
                        "--sdist-dir",
                        _path_arg(root, options.build_dist_dir),
                        "--source-date-epoch",
                        str(options.source_date_epoch),
                        "--out-json",
                        _path_arg(root, options.sdist_normalization_json),
                        "--fail-on-issue",
                    ),
                )
            )
        if not options.skip_sdist_rehearsal:
            steps.append(
                HarnessStep(
                    "sdist_rehearsal",
                    (
                        python,
                        "scripts/run_sdist_rehearsal.py",
                        "--sdist-dir",
                        _path_arg(root, options.build_dist_dir),
                        "--out-json",
                        _path_arg(root, options.sdist_rehearsal_json),
                        "--fail-on-issue",
                    ),
                )
            )
    if not options.skip_console_check:
        steps.append(
            HarnessStep(
                "console_scripts",
                (
                    python,
                    "scripts/check_installed_console_scripts.py",
                    "--out-json",
                    _path_arg(root, options.console_json),
                    "--fail-on-issue",
                ),
            )
        )
    if not options.skip_mcp_smoke:
        steps.append(
            HarnessStep(
                "mcp_smoke",
                (
                    python,
                    "scripts/run_mcp_smoke.py",
                    "--out-json",
                    _path_arg(root, options.mcp_smoke_json),
                    "--fail-on-issue",
                ),
            )
        )
    if not options.skip_mcp_transport_smoke:
        steps.append(
            HarnessStep(
                "mcp_transport_smoke",
                (
                    python,
                    "scripts/run_mcp_transport_smoke.py",
                    "--out-json",
                    _path_arg(root, options.mcp_transport_smoke_json),
                    "--fail-on-issue",
                ),
            )
        )
    if options.mcp_runtime_data_dir is not None:
        index_visibility_command = [
            python,
            "scripts/audit_mcp_index_visibility.py",
            "--data-dir",
            _path_arg(root, options.mcp_runtime_data_dir),
            "--tenant-id",
            options.tenant_id,
            "--min-visible-records",
            str(max(1, int(options.mcp_min_visible_records))),
            "--forbid-smoke-docs",
            "--require-indexed",
            "--out-json",
            _path_arg(root, options.mcp_index_visibility_json),
            "--fail-on-issue",
        ]
        if options.tenant_storage_isolation:
            index_visibility_command.append("--tenant-storage-isolation")
        steps.append(HarnessStep("mcp_index_visibility", tuple(index_visibility_command)))

    bundle_command = [
        python,
        "scripts/generate_mcp_client_config.py",
        "--client-profile",
        "bundle",
        "--server-name",
        options.server_name,
        "--tenant-id",
        options.tenant_id,
        "--transport",
        "streamable-http",
        "--host",
        options.host,
        "--port",
        str(options.port),
        "--public-url",
        options.public_url,
        "--out-dir",
        _path_arg(root, options.bundle_dir),
        "--zip-out",
        _path_arg(root, options.bundle_zip),
        "--out-json",
        _path_arg(root, options.bundle_json),
    ]
    if options.mcp_runtime_data_dir is not None:
        bundle_command.extend(["--data-dir", _path_arg(root, options.mcp_runtime_data_dir)])
    if options.mcp_bundle_profile_id:
        bundle_command.extend(["--profile-id", options.mcp_bundle_profile_id])
    if options.mcp_bundle_document_id:
        bundle_command.extend(["--document-id", options.mcp_bundle_document_id])
    if (
        options.mcp_runtime_data_dir is None
        and options.mcp_bundle_profile_id is None
        and options.mcp_bundle_document_id is None
    ):
        bundle_command.append("--skip-runtime-data")
    if options.include_wheel_in_bundle or (options.mode in {"internal", "public"} and not options.skip_build):
        bundle_command.extend(
            [
                "--include-wheel",
                "--wheel-dist-dir",
                _path_arg(root, options.build_dist_dir),
            ]
        )

    bundle_doctor_command = [
        python,
        "scripts/check_mcp_connection_readiness.py",
        "--client-profile",
        "bundle",
        "--transport",
        "streamable-http",
        "--host",
        options.host,
        "--public-url",
        options.public_url,
        "--bundle-dir",
        _path_arg(root, options.bundle_dir),
        "--skip-data-check",
        "--out-json",
        _path_arg(root, options.bundle_doctor_json),
        "--fail-on-warning",
    ]
    chatgpt_https_command = [
        python,
        "scripts/check_mcp_connection_readiness.py",
        "--client-profile",
        "chatgpt",
        "--transport",
        "streamable-http",
        "--host",
        options.host,
        "--public-url",
        options.public_url,
        "--skip-data-check",
        "--out-json",
        _path_arg(root, options.chatgpt_https_json),
        "--fail-on-warning",
    ]
    if options.probe_public_url:
        bundle_doctor_command.append("--probe-public-url")
        chatgpt_https_command.append("--probe-public-url")

    steps.append(HarnessStep("mcp_bundle_config", tuple(bundle_command)))

    if options.mcp_runtime_data_dir is not None:
        bundle_local_stdio_doctor_command = [
            python,
            "scripts/check_mcp_connection_readiness.py",
            "--client-profile",
            "bundle",
            "--transport",
            "stdio",
            "--bundle-dir",
            _path_arg(root, options.bundle_dir),
            "--codex-config",
            _path_arg(root, options.bundle_dir / "codex_config_snippet.toml"),
            "--claude-desktop-config",
            _path_arg(root, options.bundle_dir / "claude_desktop_config.json"),
            "--allow-local-only-bundle",
            "--audit-index-visibility",
            "--tenant-id",
            options.tenant_id,
            "--min-visible-records",
            str(max(1, int(options.mcp_min_visible_records))),
            "--forbid-smoke-docs",
            "--require-indexed",
            "--out-json",
            _path_arg(root, options.mcp_bundle_local_stdio_doctor_json),
            "--fail-on-warning",
        ]
        if options.tenant_storage_isolation:
            bundle_local_stdio_doctor_command.append("--tenant-storage-isolation")
        bundle_transport_smoke_command = [
            python,
            "scripts/run_mcp_transport_smoke.py",
            "--data-dir",
            _path_arg(root, options.bundle_dir / "data"),
            "--tenant-id",
            options.tenant_id,
            "--skip-preparation",
            "--out-json",
            _path_arg(root, options.mcp_bundle_transport_smoke_json),
            "--fail-on-issue",
            "--no-warm-cache",
        ]
        if not options.tenant_storage_isolation:
            bundle_transport_smoke_command.append("--flat-storage")
        bundle_client_config_smoke_command = [
            python,
            "scripts/run_mcp_client_config_smoke.py",
            "--server-name",
            options.server_name,
            "--codex-config",
            _path_arg(root, options.bundle_dir / "codex_config_snippet.toml"),
            "--claude-desktop-config",
            _path_arg(root, options.bundle_dir / "claude_desktop_config.json"),
            "--out-json",
            _path_arg(root, options.mcp_bundle_client_config_smoke_json),
            "--fail-on-issue",
        ]
        bundle_zip_extract_smoke_command = [
            python,
            "scripts/run_mcp_bundle_zip_extract_smoke.py",
            "--bundle-zip",
            _path_arg(root, options.bundle_zip),
            "--extract-dir",
            _path_arg(root, options.mcp_bundle_zip_extract_dir),
            "--server-name",
            options.server_name,
            "--out-json",
            _path_arg(root, options.mcp_bundle_zip_extract_smoke_json),
            "--overwrite",
            "--fail-on-issue",
        ]
        steps.extend(
            [
                HarnessStep("mcp_bundle_local_stdio_doctor", tuple(bundle_local_stdio_doctor_command)),
                HarnessStep("mcp_bundle_client_config_smoke", tuple(bundle_client_config_smoke_command)),
                HarnessStep("mcp_bundle_zip_extract_smoke", tuple(bundle_zip_extract_smoke_command)),
                HarnessStep("mcp_bundle_transport_smoke", tuple(bundle_transport_smoke_command)),
            ]
        )

    steps.extend(
        [
            HarnessStep(
                "mcp_bundle_doctor",
                tuple(bundle_doctor_command),
                env={"MCP_AUTH_TOKEN": "ci-token"},
            ),
            HarnessStep(
                "chatgpt_https_doctor",
                tuple(chatgpt_https_command),
                env={"MCP_AUTH_TOKEN": "ci-token"},
            ),
        ]
    )

    tunnel_command = [
        python,
        "scripts/check_mcp_connection_readiness.py",
        "--client-profile",
        "chatgpt",
        "--connection-mode",
        "openai-tunnel",
        "--transport",
        "stdio",
        "--skip-data-check",
        "--out-json",
        _path_arg(root, options.chatgpt_tunnel_json),
        "--fail-on-warning",
    ]
    if not options.require_tunnel_client:
        tunnel_command.insert(-4, "--skip-cli-check")
    steps.append(
        HarnessStep(
            "chatgpt_tunnel_doctor",
            tuple(tunnel_command),
            env={"CONTROL_PLANE_API_KEY": "ci-control-plane-key", "OPENAI_TUNNEL_ID": "ci-tunnel-id"},
        )
    )

    if options.mode in {"internal", "public"} and not options.skip_public_audit:
        public_required = options.mode == "public"
        steps.append(
            HarnessStep(
                "public_release_audit",
                (
                    python,
                    "scripts/audit_public_release_readiness.py",
                    "--include-untracked",
                    "--out-json",
                    _path_arg(root, options.public_audit_json),
                    "--json",
                ),
                required=public_required,
            )
        )
        steps.append(
            HarnessStep(
                "public_release_cleanup_plan",
                (
                    python,
                    "scripts/plan_public_release_cleanup.py",
                    "--include-untracked",
                    "--out-json",
                    _path_arg(root, options.cleanup_json),
                    "--out-md",
                    _path_arg(root, options.cleanup_md),
                ),
                required=public_required,
            )
        )

    steps.append(HarnessStep("diff_check", ("git", "diff", "--check")))
    return steps


def run_harness(
    options: HarnessOptions,
    *,
    dry_run: bool = False,
    keep_going: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    steps = build_harness_steps(options)
    if dry_run:
        return {
            "report_type": "release_harness",
            "mode": options.mode,
            "dry_run": True,
            "passed": True,
            "step_count": len(steps),
            "steps": [step.to_dict() for step in steps],
        }

    results: list[dict[str, Any]] = []
    for step in steps:
        started = time.monotonic()
        env = os.environ.copy()
        env.update(step.env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        command_executable = Path(str(step.command[0])) if step.command else None
        if command_executable and command_executable.name.lower().startswith("python"):
            python_bin_dir = command_executable.resolve().parent
            env["PATH"] = os.pathsep.join(
                [str(python_bin_dir), env.get("PATH", "")]
            ).rstrip(os.pathsep)
        try:
            completed = subprocess.run(
                list(step.command),
                cwd=options.project_root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout_tail = _tail(completed.stdout)
            stderr_tail = _tail(completed.stderr)
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout_tail = _tail(exc.stdout if isinstance(exc.stdout, str) else "")
            stderr_tail = _tail(exc.stderr if isinstance(exc.stderr, str) else f"Timed out after {timeout_seconds:g} seconds.")
        elapsed = round(time.monotonic() - started, 3)
        passed = exit_code == 0
        results.append(
            {
                "name": step.name,
                "required": step.required,
                "passed": passed,
                "exit_code": exit_code,
                "elapsed_seconds": elapsed,
                "command": list(step.command),
                "env_keys": sorted(step.env),
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            }
        )
        if step.required and not passed and not keep_going:
            break

    failed_required = [item["name"] for item in results if item["required"] and not item["passed"]]
    failed_advisory = [item["name"] for item in results if not item["required"] and not item["passed"]]
    return {
        "report_type": "release_harness",
        "mode": options.mode,
        "dry_run": False,
        "passed": not failed_required,
        "step_count": len(steps),
        "executed_step_count": len(results),
        "failed_required_step_names": failed_required,
        "failed_advisory_step_names": failed_advisory,
        "steps": results,
    }


def _tail(text: str | bytes | None, *, line_limit: int = 40) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= line_limit:
        return "\n".join(lines)
    return "\n".join(lines[-line_limit:])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the repeatable release harness for MCP-first deployments.")
    parser.add_argument("--project-root", default=None)
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="Rebase default report, bundle, and build outputs under one directory.",
    )
    parser.add_argument(
        "--build-python",
        default=None,
        help=(
            "Python executable containing the 'build' package. Use a dedicated build-tool venv "
            "when the application environment was installed without the dev extra."
        ),
    )
    parser.add_argument(
        "--source-date-epoch",
        type=_source_date_epoch_arg,
        default=None,
        help=(
            "Set SOURCE_DATE_EPOCH for package build and deterministically normalize the built sdist. "
            "Must be between 0 and 4294967295."
        ),
    )
    parser.add_argument(
        "--real-parser-fixture-root",
        default=None,
        help="Root containing the pinned HWP/PDF release regression fixtures.",
    )
    parser.add_argument(
        "--require-real-parser-fixtures",
        action="store_true",
        help="Run the fail-closed real parser fixture gate (always enabled in public mode).",
    )
    parser.add_argument("--mode", choices=("mcp", "internal", "public"), default="internal")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--public-url", default="https://mcp.example.invalid/mcp")
    parser.add_argument("--server-name", default="regulation_mcp")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--mcp-runtime-data-dir", default=None)
    parser.add_argument(
        "--mcp-bundle-profile-id",
        default=None,
        help="Institution profile scope for an exported MCP runtime bundle.",
    )
    parser.add_argument(
        "--mcp-bundle-document-id",
        default=None,
        help="Single approved document scope for an exported MCP runtime bundle.",
    )
    parser.add_argument("--mcp-min-visible-records", type=int, default=1)
    parser.add_argument("--bundle-dir", default="reports/mcp_connection_bundle_harness")
    parser.add_argument("--bundle-zip", default="reports/mcp_connection_bundle_harness.zip")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--allow-dirty-build",
        action="store_true",
        help="Allow non-release validation builds from a dirty tree; generated artifacts must not be published.",
    )
    parser.add_argument("--skip-sdist-rehearsal", action="store_true")
    parser.add_argument("--skip-console-check", action="store_true")
    parser.add_argument("--skip-mcp-smoke", action="store_true")
    parser.add_argument("--skip-mcp-transport-smoke", action="store_true")
    parser.add_argument("--skip-public-audit", action="store_true")
    parser.add_argument("--require-tunnel-client", action="store_true")
    parser.add_argument("--probe-public-url", action="store_true", help="Make bundle and ChatGPT HTTPS doctors probe the live public /mcp URL.")
    parser.add_argument(
        "--include-wheel-in-bundle",
        action="store_true",
        help="Include the latest built wheel in the generated MCP setup bundle zip.",
    )
    return parser


def options_from_args(args: argparse.Namespace) -> HarnessOptions:
    root = Path(args.project_root) if args.project_root else Path.cwd()
    return HarnessOptions(
        project_root=root.resolve(),
        artifact_dir=Path(args.artifact_dir) if args.artifact_dir else None,
        build_python=Path(args.build_python) if args.build_python else None,
        source_date_epoch=args.source_date_epoch,
        real_parser_fixture_root=(
            Path(args.real_parser_fixture_root) if args.real_parser_fixture_root else None
        ),
        require_real_parser_fixtures=args.require_real_parser_fixtures,
        mode=args.mode,
        host=args.host,
        port=args.port,
        public_url=args.public_url,
        server_name=args.server_name,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        mcp_runtime_data_dir=Path(args.mcp_runtime_data_dir) if args.mcp_runtime_data_dir else None,
        mcp_bundle_profile_id=args.mcp_bundle_profile_id,
        mcp_bundle_document_id=args.mcp_bundle_document_id,
        mcp_min_visible_records=args.mcp_min_visible_records,
        bundle_dir=Path(args.bundle_dir),
        bundle_zip=Path(args.bundle_zip),
        skip_tests=args.skip_tests,
        skip_build=args.skip_build,
        allow_dirty_build=args.allow_dirty_build,
        skip_sdist_rehearsal=args.skip_sdist_rehearsal,
        skip_console_check=args.skip_console_check,
        skip_mcp_smoke=args.skip_mcp_smoke,
        skip_mcp_transport_smoke=args.skip_mcp_transport_smoke,
        skip_public_audit=args.skip_public_audit,
        require_tunnel_client=args.require_tunnel_client,
        include_wheel_in_bundle=args.include_wheel_in_bundle,
        probe_public_url=args.probe_public_url,
    )


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    options = options_from_args(args)
    report = run_harness(
        options,
        dry_run=args.dry_run,
        keep_going=args.keep_going,
        timeout_seconds=args.timeout_seconds,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(payload + "\n", encoding="utf-8")
    stdout.write(payload + "\n")
    return 0 if report.get("passed") else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
