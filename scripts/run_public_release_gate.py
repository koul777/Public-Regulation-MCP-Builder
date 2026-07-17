from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_public_release_readiness import (
    PublicFinding,
    _tracked_paths,
    _untracked_paths,
    audit_public_release,
)
from scripts.plan_public_release_cleanup import build_public_release_cleanup_plan
from scripts.report_metadata import current_repo_commit
from scripts.run_release_harness import HarnessOptions, run_harness


def build_public_release_gate_report(
    root: str | Path,
    *,
    include_untracked: bool = False,
    tracked_paths: Iterable[str] | None = None,
    run_public_harness: bool = False,
    execute_harness: bool = False,
    probe_public_url: bool = False,
    public_url: str = "https://mcp.example.invalid/mcp",
) -> dict[str, object]:
    root_path = Path(root).resolve()
    scan_paths = _scan_paths(root_path, tracked_paths=tracked_paths, include_untracked=include_untracked)
    findings = audit_public_release(root_path, tracked_paths=scan_paths)
    cleanup_plan = build_public_release_cleanup_plan(
        root_path,
        findings=findings,
        include_untracked=include_untracked,
    )
    harness_report = None
    if run_public_harness or execute_harness:
        harness_report = run_harness(
            HarnessOptions(
                project_root=root_path,
                mode="public",
                public_url=public_url,
                probe_public_url=probe_public_url,
            ),
            dry_run=not execute_harness,
            keep_going=True,
        )

    failed_required_steps = []
    if isinstance(harness_report, dict):
        failed_required_steps = list(harness_report.get("failed_required_step_names") or [])
    finding_count = len(findings)
    passed = finding_count == 0 and not failed_required_steps
    status = _status(passed=passed, finding_count=finding_count, failed_required_steps=failed_required_steps)

    return {
        "report_type": "public_release_gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(root_path),
        "status": status,
        "passed": passed,
        "include_untracked": include_untracked,
        "finding_count": finding_count,
        "severity_counts": dict(Counter(finding.severity for finding in findings)),
        "action_count": cleanup_plan.get("action_count", 0),
        "findings": [finding.to_dict() for finding in findings],
        "cleanup_plan": cleanup_plan,
        "harness": harness_report,
        "next_actions": _next_actions(findings, failed_required_steps=failed_required_steps),
    }


def _scan_paths(
    root: Path,
    *,
    tracked_paths: Iterable[str] | None,
    include_untracked: bool,
) -> list[str] | None:
    if tracked_paths is not None:
        return [str(path).replace("\\", "/") for path in tracked_paths]
    if include_untracked:
        return _tracked_paths(root) + _untracked_paths(root)
    return None


def _status(*, passed: bool, finding_count: int, failed_required_steps: list[str]) -> str:
    if passed:
        return "ready_for_public_release"
    if finding_count and failed_required_steps:
        return "blocked_by_audit_and_harness"
    if finding_count:
        return "blocked_by_public_audit"
    return "blocked_by_public_harness"


def _next_actions(findings: list[PublicFinding], *, failed_required_steps: list[str]) -> list[str]:
    codes = {finding.code for finding in findings}
    actions: list[str] = []
    if "missing-license" in codes:
        actions.append("Decide the open-source license and add LICENSE before public release.")
    if "tracked-runtime-data" in codes or "tracked-document-sample" in codes:
        actions.append("Remove runtime data and document samples, or document redistribution approval.")
    if "tracked-nonpublic-doc" in codes:
        actions.append("Remove private/internal handoff docs from the public branch or rewrite them as public-safe docs.")
    if "public-doc-nonpublic-reference" in codes:
        actions.append("Rewrite public docs so README/AGENTS/CONTRIBUTING/SECURITY do not link private/internal handoff material.")
    if "tracked-report-artifact" in codes:
        actions.append("Remove generated reports unless they are explicitly redacted and allowlisted.")
    if "institution-identifier-risk" in codes:
        actions.append("Replace institution-derived identifiers in reports or fixtures with synthetic values.")
    if failed_required_steps:
        actions.append("Fix required public release harness failures: " + ", ".join(failed_required_steps) + ".")
    if not actions:
        actions.append("Run a fresh clone rehearsal and execute reg-rag-release-harness --mode public.")
    return actions


def _to_markdown(report: dict[str, object]) -> str:
    lines = [
        "# Public Release Gate",
        "",
        f"- Status: `{report['status']}`",
        f"- Passed: `{str(report['passed']).lower()}`",
        f"- Finding count: {report['finding_count']}",
        f"- Cleanup action count: {report['action_count']}",
        "",
        "## Next Actions",
        "",
    ]
    lines.extend(f"- {action}" for action in report["next_actions"])
    lines.extend(["", "## Findings", ""])
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    if not findings:
        lines.append("- None.")
    else:
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            lines.append(
                "- `{severity}` `{code}` `{path}`: {detail}".format(
                    severity=finding.get("severity", ""),
                    code=finding.get("code", ""),
                    path=finding.get("path", ""),
                    detail=finding.get("detail", ""),
                )
            )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a non-destructive public release gate.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--include-untracked", action="store_true")
    parser.add_argument("--run-public-harness", action="store_true", help="Include the public release harness plan.")
    parser.add_argument("--execute-harness", action="store_true", help="Execute the public release harness.")
    parser.add_argument("--probe-public-url", action="store_true", help="When executing the harness, probe the live public /mcp URL.")
    parser.add_argument("--public-url", default="https://mcp.example.invalid/mcp")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-blocked", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = build_public_release_gate_report(
        args.root,
        include_untracked=args.include_untracked,
        run_public_harness=args.run_public_harness,
        execute_harness=args.execute_harness,
        probe_public_url=args.probe_public_url,
        public_url=args.public_url,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(payload + "\n", encoding="utf-8")
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    if args.json:
        stdout.write(payload + "\n")
    else:
        stdout.write(_to_markdown(report))
    return 1 if args.fail_on_blocked and not report["passed"] else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
