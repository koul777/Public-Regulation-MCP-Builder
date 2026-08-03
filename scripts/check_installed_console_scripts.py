from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import __version__ as APP_VERSION  # noqa: E402


DEFAULT_COMMANDS = (
    "reg-rag-batch",
    "reg-rag-public-batch-pipeline",
    "reg-rag-ci-gate",
    "reg-rag-preprocessing-change-guard",
    "reg-rag-nightly-smoke",
    "reg-rag-audit-release",
    "reg-rag-audit-public-release",
    "reg-rag-plan-public-release-cleanup",
    "reg-rag-public-release-gate",
    "reg-rag-github-publish-readiness",
    "reg-rag-github-publish-owner-decisions",
    "reg-rag-github-publish-plan",
    "reg-rag-strict-readiness-gaps",
    "reg-rag-temporal-ambiguity-scope",
    "reg-rag-temporal-ambiguity-policy-sheet",
    "reg-rag-temporal-ambiguity-policy-check",
    "reg-rag-check-private-release",
    "reg-rag-check-github-private",
    "reg-rag-check-console-scripts",
    "reg-rag-release-harness",
    "reg-rag-hermes",
    "reg-rag-sdist-rehearsal",
    "reg-rag-fresh-clone-rehearsal",
    "reg-rag-private-release-gate",
    "reg-rag-private-release-manifest",
    "reg-rag-release-evidence-index",
    "reg-rag-verify-release-evidence",
    "reg-rag-private-release-smoke",
    "reg-rag-public-readiness",
    "reg-rag-review-queue-triage",
    "reg-rag-review-triage-summary",
    "reg-rag-human-review-evidence",
    "reg-rag-approval-evidence",
    "reg-rag-approval-worklist",
    "reg-rag-approval-review-batches",
    "reg-rag-approval-review-triage",
    "reg-rag-approval-sha-drift-plan",
    "reg-rag-table-risk-report",
    "reg-rag-table-unit-review-packet",
    "reg-rag-parsing-goldset-board",
    "reg-rag-parsing-goldset-start-here",
    "reg-rag-parsing-goldset-table-sheet",
    "reg-rag-parsing-goldset-table-units",
    "reg-rag-parsing-goldset-table-review-batches",
    "reg-rag-parsing-goldset-table-review-summary",
    "reg-rag-parsing-goldset-table-transfer-check",
    "reg-rag-parsing-goldset-table-source-check",
    "reg-rag-parsing-goldset-table-drift-check",
    "reg-rag-table-preprocessing-claim-gate",
    "reg-rag-pilot-blocker-action-board",
    "reg-rag-reapproval-evidence",
    "reg-rag-reapproval-worklist",
    "reg-rag-reapproval-review-batches",
    "reg-rag-reapproval-review-burden",
    "reg-rag-reapproval-decision-check",
    "reg-rag-reapproval-apply-plan",
    "reg-rag-reapproval-shadow-apply",
    "reg-rag-profile-registry-from-batch",
    "reg-rag-export-public-report",
    "reg-rag-export-vectordb",
    "reg-rag-export-relations",
    "reg-rag-estimate-agent-review-cost",
    "reg-rag-estimate-embedding-cost",
    "reg-rag-embed-vectors",
    "reg-rag-upsert-vectordb",
    "reg-rag-rag-security-evidence",
    "reg-rag-secure-rag-smoke",
    "reg-rag-mcp-server",
    "reg-rag-mcp-vercel-stage",
    "reg-rag-mcp-smoke",
    "reg-rag-mcp-transport-smoke",
    "reg-rag-mcp-client-config-smoke",
    "reg-rag-mcp-codex-app-server-check",
    "reg-rag-mcp-desktop-recognition-check",
    "reg-rag-mcp-claude-desktop-observation",
    "reg-rag-mcp-connection-refresh",
    "reg-rag-mcp-client-status",
    "reg-rag-mcp-windows-execution-matrix",
    "reg-rag-mcp-bundle-zip-extract-smoke",
    "reg-rag-mcp-prepare-runtime",
    "reg-rag-mcp-product-readiness",
    "reg-rag-mcp-temporal-readiness-bundle",
    "reg-rag-mcp-config",
    "reg-rag-mcp-doctor",
    "reg-rag-mcp-handoff-report",
    "reg-rag-mcp-authority",
    "reg-rag-mcp-remediation-plan",
    "reg-rag-mcp-demo-answers",
    "reg-rag-mcp-answer-evidence-bundle",
    "reg-rag-mcp-answer-blocker-map",
    "reg-rag-mcp-performance-load-evidence",
    "reg-rag-mcp-cold-start-benchmark",
    "reg-rag-mcp-first-query-benchmark",
    "reg-rag-mcp-concurrent-benchmark",
    "reg-rag-mcp-index-visibility",
    "reg-rag-mcp-query-benchmark",
    "reg-rag-mcp-retrieval-quality",
    "reg-rag-revision-impact",
    "reg-rag-real-parser-fixtures",
)


@dataclass(frozen=True)
class ConsoleScriptIssue:
    severity: str
    code: str
    command: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def check_installed_console_scripts(
    *,
    commands: Sequence[str] = DEFAULT_COMMANDS,
    run_help: bool = True,
    timeout_seconds: float = 10.0,
    search_path: str | None = None,
) -> dict[str, object]:
    checked: list[dict[str, object]] = []
    issues: list[ConsoleScriptIssue] = []
    for command in commands:
        resolved = shutil.which(command, path=search_path)
        item: dict[str, object] = {"command": command, "path": resolved, "help_checked": False}
        if not resolved:
            issues.append(
                ConsoleScriptIssue(
                    "high",
                    "console-script-missing",
                    command,
                    "Console script is not visible on PATH after package installation.",
                )
            )
            checked.append(item)
            continue
        if run_help:
            item["help_checked"] = True
            try:
                result = subprocess.run(
                    [resolved, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                issues.append(
                    ConsoleScriptIssue(
                        "high",
                        "console-script-help-timeout",
                        command,
                        f"Console script --help exceeded {timeout_seconds:g} seconds.",
                    )
                )
            except OSError as exc:
                issues.append(
                    ConsoleScriptIssue(
                        "high",
                        "console-script-help-failed",
                        command,
                        f"Console script --help could not be executed: {exc}",
                    )
                )
            else:
                item["help_exit_code"] = result.returncode
                if result.returncode != 0:
                    issues.append(
                        ConsoleScriptIssue(
                            "high",
                            "console-script-help-nonzero",
                            command,
                            f"Console script --help exited with {result.returncode}.",
                        )
                    )
        checked.append(item)

    high_count = sum(1 for issue in issues if issue.severity == "high")
    return {
        "report_type": "installed_console_scripts",
        "check_scope": "current-environment",
        "passed": high_count == 0,
        "command_count": len(commands),
        "checked": checked,
        "high_count": high_count,
        "issue_count": len(issues),
        "issues": [issue.to_dict() for issue in issues],
    }


def _normalized_wheel_version(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _select_wheel(wheel_dist_dir: Path, *, expected_version: str) -> Path:
    candidates = [
        path
        for path in wheel_dist_dir.glob("reg_rag_preprocessor-*.whl")
        if path.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No reg_rag_preprocessor wheel found in {wheel_dist_dir.resolve()}"
        )
    expected = _normalized_wheel_version(expected_version)
    matching = [
        path
        for path in candidates
        if len(path.name.split("-")) >= 5
        and _normalized_wheel_version(path.name.split("-")[1]) == expected
    ]
    if not matching:
        available = ", ".join(sorted(path.name for path in candidates))
        raise FileNotFoundError(
            f"No reg_rag_preprocessor wheel for version {expected_version!r} found in "
            f"{wheel_dist_dir.resolve()}; available: {available}"
        )
    return max(matching, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _wheel_setup_failure_report(
    *,
    commands: Sequence[str],
    code: str,
    detail: str,
    wheel_path: Path | None = None,
) -> dict[str, object]:
    issue = ConsoleScriptIssue(
        "high",
        code,
        str(wheel_path) if wheel_path is not None else "wheel-installation",
        detail,
    )
    return {
        "report_type": "installed_console_scripts",
        "check_scope": "built-wheel",
        "wheel_path": str(wheel_path.resolve()) if wheel_path is not None else None,
        "passed": False,
        "command_count": len(commands),
        "checked": [],
        "high_count": 1,
        "issue_count": 1,
        "issues": [issue.to_dict()],
    }


def check_wheel_console_scripts(
    *,
    wheel_dist_dir: Path,
    commands: Sequence[str] = DEFAULT_COMMANDS,
    run_help: bool = True,
    timeout_seconds: float = 10.0,
    setup_timeout_seconds: float = 120.0,
    expected_version: str = APP_VERSION,
) -> dict[str, object]:
    """Install the expected-version wheel in a temporary venv and check entry points."""

    try:
        wheel_path = _select_wheel(
            wheel_dist_dir,
            expected_version=expected_version,
        )
    except (FileNotFoundError, OSError) as exc:
        return _wheel_setup_failure_report(
            commands=commands,
            code="console-script-wheel-missing",
            detail=str(exc),
        )

    with tempfile.TemporaryDirectory(prefix="reg-rag-console-scripts-") as tmp:
        venv_dir = Path(tmp) / "venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=setup_timeout_seconds,
                check=True,
            )
            scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            venv_python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
            subprocess.run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    str(wheel_path.resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=setup_timeout_seconds,
                check=True,
            )
        except subprocess.TimeoutExpired:
            return _wheel_setup_failure_report(
                commands=commands,
                code="console-script-wheel-install-timeout",
                detail=f"Temporary wheel installation exceeded {setup_timeout_seconds:g} seconds.",
                wheel_path=wheel_path,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", None)
            detail = str(stderr or exc).strip()
            return _wheel_setup_failure_report(
                commands=commands,
                code="console-script-wheel-install-failed",
                detail=detail,
                wheel_path=wheel_path,
            )

        report = check_installed_console_scripts(
            commands=commands,
            run_help=run_help,
            timeout_seconds=timeout_seconds,
            search_path=str(scripts_dir),
        )
        report["check_scope"] = "built-wheel"
        report["wheel_path"] = str(wheel_path.resolve())
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify installed console scripts after package installation.")
    parser.add_argument(
        "--command",
        action="append",
        dest="commands",
        help="Console command to check. Repeat to override the default command list.",
    )
    parser.add_argument("--skip-help", action="store_true", help="Only check PATH visibility, not --help execution.")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--wheel-dist-dir",
        default=None,
        help=(
            "Install the expected-version reg_rag_preprocessor wheel from this directory in a temporary "
            "virtual environment and validate that artifact instead of the current PATH."
        ),
    )
    parser.add_argument(
        "--wheel-version",
        default=APP_VERSION,
        help="Exact application version expected in --wheel-dist-dir (default: running source version).",
    )
    parser.add_argument("--setup-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    commands = tuple(args.commands or DEFAULT_COMMANDS)
    if args.wheel_dist_dir:
        report = check_wheel_console_scripts(
            wheel_dist_dir=Path(args.wheel_dist_dir),
            commands=commands,
            run_help=not args.skip_help,
            timeout_seconds=args.timeout_seconds,
            setup_timeout_seconds=args.setup_timeout_seconds,
            expected_version=args.wheel_version,
        )
    else:
        report = check_installed_console_scripts(
            commands=commands,
            run_help=not args.skip_help,
            timeout_seconds=args.timeout_seconds,
        )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        stdout.write(payload + "\n")
    elif report["issues"]:
        for issue in report["issues"]:
            stdout.write(f"{issue['severity']} {issue['code']} {issue['command']}: {issue['detail']}\n")
    else:
        stdout.write("Installed console script check passed\n")
    if args.fail_on_issue and int(report["issue_count"]) > 0:
        return 1
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
