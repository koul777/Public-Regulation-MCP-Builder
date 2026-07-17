"""Run the institution-scoped release evidence pipeline without mutating source data."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local MCP, transport, temporal, readiness, and migration gates for one institution profile."
    )
    parser.add_argument("--data-dir", default="data/runtime-institution-mcp")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Evidence directory. Defaults to reports/overnight_runs/release-<UTC timestamp>.",
    )
    parser.add_argument("--python", default=sys.executable, help="Python executable used for child commands.")
    parser.add_argument(
        "--allow-synthetic-runtime",
        action="store_true",
        help="Allow disposable smoke documents for local evidence only; production runs remain smoke-blocked.",
    )
    parser.add_argument(
        "--skip-local-smoke",
        action="store_true",
        help="Use an already prepared runtime and do not create synthetic smoke documents.",
    )
    parser.add_argument(
        "--scope-profile-id",
        action="append",
        default=[],
        help="Run the optional two-profile same-tenant isolation smoke; requires --allow-synthetic-runtime.",
    )
    return parser.parse_args(argv)


def _default_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return PROJECT_ROOT / "reports" / "overnight_runs" / f"release-{stamp}"


def _run_step(
    *,
    name: str,
    command: list[str],
    run_dir: Path,
) -> dict[str, object]:
    log_path = run_dir / f"{name}.log"
    launch_error = ""
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        completed = None
    stdout = completed.stdout if completed else ""
    stderr = completed.stderr if completed else launch_error
    return_code = completed.returncode if completed else 127
    log_path.write_text(
        "COMMAND\n"
        + shlex.join(command)
        + "\n\nSTDOUT\n"
        + stdout
        + "\nSTDERR\n"
        + stderr,
        encoding="utf-8",
    )
    return {
        "name": name,
        "command": command,
        "return_code": return_code,
        "passed": return_code == 0,
        "launch_error": launch_error or None,
        "log": str(log_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir()
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir
    run_dir = run_dir.resolve()
    allowed_root = (PROJECT_ROOT / "reports" / "overnight_runs").resolve()
    try:
        run_dir.relative_to(allowed_root)
    except ValueError as exc:
        raise SystemExit(f"--run-dir must be under {allowed_root}") from exc
    run_dir.mkdir(parents=True, exist_ok=True)

    data_dir = str(args.data_dir)
    scope_data_dir = str(Path(data_dir) / "profile-scope")
    tenant_id = str(args.tenant_id).strip()
    profile_id = str(args.profile_id).strip()
    python = str(args.python)
    scope_profile_ids = [str(value).strip() for value in args.scope_profile_id if str(value).strip()]
    if scope_profile_ids and (len(scope_profile_ids) != 2 or len(set(scope_profile_ids)) != 2):
        raise SystemExit("--scope-profile-id requires exactly two distinct profile ids")
    if scope_profile_ids and not args.allow_synthetic_runtime:
        raise SystemExit("--scope-profile-id requires --allow-synthetic-runtime")
    if not args.allow_synthetic_runtime and not args.skip_local_smoke:
        raise SystemExit(
            "Choose --allow-synthetic-runtime for disposable evidence or "
            "--skip-local-smoke for an existing prepared runtime."
        )
    common = ["--tenant-id", tenant_id, "--profile-id", profile_id]
    synthetic_runtime_args = ["--allow-synthetic-runtime"] if args.allow_synthetic_runtime else []
    smoke_guard_args = [] if args.allow_synthetic_runtime else ["--forbid-smoke-docs"]
    steps = [
        (
            "local-smoke",
            [
                python,
                "scripts/run_mcp_smoke.py",
                "--data-dir",
                data_dir,
                *common,
                "--allow-persistent-smoke-data",
                "--out-json",
                str(run_dir / "local-smoke.json"),
                "--fail-on-issue",
            ],
        ),
        (
            "stdio-transport-smoke",
            [
                python,
                "scripts/run_mcp_transport_smoke.py",
                "--data-dir",
                data_dir,
                *common,
                "--transport",
                "stdio",
                "--skip-preparation",
                "--out-json",
                str(run_dir / "stdio-smoke.json"),
                "--fail-on-issue",
            ],
        ),
        (
            "profile-scope-smoke",
            [
                python,
                "scripts/run_mcp_profile_scope_smoke.py",
                "--data-dir",
                scope_data_dir,
                "--tenant-id",
                tenant_id,
                "--profile-id",
                scope_profile_ids[0] if scope_profile_ids else "",
                "--profile-id",
                scope_profile_ids[1] if len(scope_profile_ids) > 1 else "",
                "--allow-persistent-smoke-data",
                "--out-json",
                str(run_dir / "profile-scope-smoke.json"),
                "--fail-on-issue",
            ],
        ),
        (
            "http-transport-smoke",
            [
                python,
                "scripts/run_mcp_transport_smoke.py",
                "--data-dir",
                data_dir,
                *common,
                "--transport",
                "streamable-http",
                "--skip-preparation",
                "--out-json",
                str(run_dir / "http-smoke.json"),
                "--fail-on-issue",
            ],
        ),
        (
            "index-visibility-audit",
            [
                python,
                "scripts/audit_mcp_index_visibility.py",
                "--data-dir",
                data_dir,
                *common,
                *smoke_guard_args,
                "--require-indexed",
                "--tenant-storage-isolation",
                "--out-json",
                str(run_dir / "index-visibility.json"),
                "--fail-on-issue",
            ],
        ),
        (
            "temporal-metadata-audit",
            [
                python,
                "scripts/audit_temporal_metadata_coverage.py",
                "--data-dir",
                data_dir,
                *common,
                "--tenant-storage-isolation",
                "--out-json",
                str(run_dir / "temporal.json"),
                "--fail-on-blocker",
            ],
        ),
        (
            "product-readiness-audit",
            [
                python,
                "scripts/audit_mcp_product_readiness.py",
                "--runtime-data-dir",
                data_dir,
                *common,
                "--tenant-storage-isolation",
                *synthetic_runtime_args,
                "--mcp-transport-smoke-report",
                str(run_dir / "http-smoke.json"),
                "--temporal-coverage-report",
                str(run_dir / "temporal.json"),
                "--out-json",
                str(run_dir / "readiness.json"),
                "--fail-on-issue",
            ],
        ),
        (
            "migration-manifest",
            [
                python,
                "scripts/build_regulation_migration_manifest.py",
                "--data-dir",
                data_dir,
                *common,
                "--tenant-storage-isolation",
                "--out-json",
                str(run_dir / "migration.json"),
                "--fail-on-blocker",
            ],
        ),
    ]
    if args.skip_local_smoke:
        steps = [step for step in steps if step[0] != "local-smoke"]
    if not scope_profile_ids:
        steps = [step for step in steps if step[0] != "profile-scope-smoke"]
    results = [_run_step(name=name, command=command, run_dir=run_dir) for name, command in steps]
    summary = {
        "report_type": "institution_release_gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": data_dir,
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "allow_synthetic_runtime": bool(args.allow_synthetic_runtime),
        "skip_local_smoke": bool(args.skip_local_smoke),
        "scope_profile_ids": scope_profile_ids,
        "run_dir": str(run_dir),
        "steps": results,
        "passed": all(bool(result["passed"]) for result in results),
    }
    summary_path = run_dir / "release_gate_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_lines = [
        "# Institution release gate summary",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Data directory: `{data_dir}`",
        f"- Tenant: `{tenant_id}`",
        f"- Institution profile: `{profile_id}`",
        f"- Synthetic runtime allowed: `{str(bool(args.allow_synthetic_runtime)).lower()}`",
        f"- Local smoke skipped: `{str(bool(args.skip_local_smoke)).lower()}`",
        f"- Overall result: **{'PASS' if summary['passed'] else 'FAIL'}**",
        "",
        "| Step | Result | Return code | Log |",
        "| --- | --- | ---: | --- |",
    ]
    for result in results:
        result_label = "PASS" if result["passed"] else "FAIL"
        markdown_lines.append(
            f"| `{result['name']}` | **{result_label}** | `{result['return_code']}` | `{result['log']}` |"
        )
    markdown_path = run_dir / "release_gate_summary.md"
    markdown_path.write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    summary["summary_markdown"] = str(markdown_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
