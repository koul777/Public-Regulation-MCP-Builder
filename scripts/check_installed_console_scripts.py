from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence, TextIO


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
    "reg-rag-mcp-smoke",
    "reg-rag-mcp-transport-smoke",
    "reg-rag-mcp-client-config-smoke",
    "reg-rag-mcp-codex-app-server-check",
    "reg-rag-mcp-desktop-recognition-check",
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
    "reg-rag-mcp-concurrent-benchmark",
    "reg-rag-mcp-index-visibility",
    "reg-rag-mcp-query-benchmark",
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
) -> dict[str, object]:
    checked: list[dict[str, object]] = []
    issues: list[ConsoleScriptIssue] = []
    for command in commands:
        resolved = shutil.which(command)
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
        "passed": high_count == 0,
        "command_count": len(commands),
        "checked": checked,
        "high_count": high_count,
        "issue_count": len(issues),
        "issues": [issue.to_dict() for issue in issues],
    }


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
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    commands = tuple(args.commands or DEFAULT_COMMANDS)
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
