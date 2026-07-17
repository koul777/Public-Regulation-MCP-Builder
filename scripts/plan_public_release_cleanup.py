from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_public_release_readiness import PublicFinding, _tracked_paths, _untracked_paths, audit_public_release
from scripts.report_metadata import current_repo_commit


@dataclass(frozen=True)
class CleanupAction:
    action: str
    path: str
    reason: str
    command: str | None = None
    action_class: str = "safe_machine_action"
    requires_owner_decision: bool = False
    destructive: bool = False
    apply_scope: str = "dedicated_public_release_branch"

    def to_dict(self) -> dict[str, str | bool | None]:
        return asdict(self)


def build_public_release_cleanup_plan(
    root: Path | str,
    *,
    findings: Iterable[PublicFinding] | None = None,
    include_untracked: bool = False,
) -> dict[str, object]:
    root_path = Path(root).resolve()
    if findings is not None:
        audit_findings = list(findings)
    else:
        tracked_paths = None
        if include_untracked:
            tracked_paths = _tracked_paths(root_path) + _untracked_paths(root_path)
        audit_findings = audit_public_release(root_path, tracked_paths=tracked_paths)
    actions = _actions_from_findings(audit_findings)
    classification_counts = _count_by(actions, "action_class")
    return {
        "report_type": "public_release_cleanup_plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(root_path),
        "include_untracked": include_untracked,
        "finding_count": len(audit_findings),
        "action_count": len(actions),
        "action_class_counts": classification_counts,
        "owner_decision_action_count": sum(1 for action in actions if action.requires_owner_decision),
        "safe_machine_action_count": classification_counts.get("safe_machine_action", 0),
        "destructive_action_count": sum(1 for action in actions if action.destructive),
        "actions": [action.to_dict() for action in actions],
        "notes": [
            "This plan does not modify files.",
            "Review license choice with the project owner before adding LICENSE.",
            "Use a dedicated public-release branch for removal or synthetic replacement work.",
            "Actions marked destructive should be applied only on a dedicated public-release branch, not on the active working branch.",
        ],
    }


def _actions_from_findings(findings: list[PublicFinding]) -> list[CleanupAction]:
    actions: dict[tuple[str, str], CleanupAction] = {}
    for finding in findings:
        if finding.code == "missing-license":
            _add_action(
                actions,
                CleanupAction(
                    "choose_and_add_license",
                    "LICENSE",
                    "Public release requires an explicit license decision.",
                    None,
                    action_class="owner_legal_decision",
                    requires_owner_decision=True,
                    apply_scope="repository_policy",
                ),
            )
        elif finding.code == "missing-public-doc":
            _add_action(
                actions,
                CleanupAction(
                    "track_public_doc",
                    finding.path,
                    "Required public documentation exists locally or should be added to the release branch.",
                    f"git add -- {finding.path}",
                    action_class="safe_machine_action",
                    apply_scope="public_release_branch",
                ),
            )
        elif finding.code in {"tracked-runtime-data", "tracked-document-sample"}:
            _add_action(
                actions,
                CleanupAction(
                    "remove_or_document_sample",
                    finding.path,
                    "Remove from public branch unless redistribution approval is documented in the sample manifest.",
                    f"git rm -- {finding.path}",
                    action_class="owner_legal_decision",
                    requires_owner_decision=True,
                    destructive=True,
                ),
            )
        elif finding.code == "tracked-report-artifact":
            _add_action(
                actions,
                CleanupAction(
                    "remove_generated_report",
                    finding.path,
                    "Generated reports should not be committed to a source-only public branch.",
                    f"git rm -- {finding.path}",
                    action_class="safe_machine_action",
                    destructive=True,
                ),
            )
        elif finding.code == "generated-artifact-path":
            _add_action(
                actions,
                CleanupAction(
                    "remove_or_ignore_generated_artifact",
                    finding.path,
                    "Generated runtime/build artifacts should be removed from the public branch and ignored locally.",
                    None,
                    action_class="safe_machine_action",
                    destructive=True,
                ),
            )
        elif finding.code == "tracked-nonpublic-doc":
            _add_action(
                actions,
                CleanupAction(
                    "remove_nonpublic_doc",
                    finding.path,
                    "Private/internal handoff docs should be removed from the public branch or rewritten as public-safe docs.",
                    f"git rm -- {finding.path}",
                    action_class="owner_policy_decision",
                    requires_owner_decision=True,
                    destructive=True,
                ),
            )
        elif finding.code == "public-doc-nonpublic-reference":
            _add_action(
                actions,
                CleanupAction(
                    "rewrite_public_doc_for_public_release",
                    finding.path,
                    "Public docs should not link private/internal handoff material in the public branch.",
                    None,
                    action_class="owner_policy_decision",
                    requires_owner_decision=True,
                    apply_scope="public_release_branch",
                ),
            )
        elif finding.code == "institution-identifier-risk":
            _add_action(
                actions,
                CleanupAction(
                    "synthesize_or_remove_identifier_fixture",
                    finding.path,
                    "Replace institution-derived identifiers with synthetic fixtures or remove the file from public release.",
                    None,
                    action_class="owner_policy_decision",
                    requires_owner_decision=True,
                ),
            )
    return sorted(actions.values(), key=lambda action: (action.action, action.path))


def _add_action(actions: dict[tuple[str, str], CleanupAction], action: CleanupAction) -> None:
    actions[(action.action, action.path)] = action


def _count_by(actions: list[CleanupAction], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in actions:
        value = str(getattr(action, field))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _to_markdown(report: dict[str, object]) -> str:
    lines = [
        "# Public Release Cleanup Plan",
        "",
        f"- Finding count: {report['finding_count']}",
        f"- Action count: {report['action_count']}",
        f"- Owner-decision actions: {report['owner_decision_action_count']}",
        f"- Safe machine actions: {report['safe_machine_action_count']}",
        f"- Destructive branch actions: {report['destructive_action_count']}",
        "",
        "| Action | Class | Path | Owner Decision | Destructive | Scope | Reason | Command |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for action in report["actions"]:
        assert isinstance(action, dict)
        lines.append(
            "| {action} | {action_class} | `{path}` | {owner} | {destructive} | {scope} | {reason} | {command} |".format(
                action=action["action"],
                action_class=action.get("action_class", ""),
                path=action["path"],
                owner="yes" if action.get("requires_owner_decision") else "no",
                destructive="yes" if action.get("destructive") else "no",
                scope=action.get("apply_scope", ""),
                reason=str(action["reason"]).replace("|", "\\|"),
                command=f"`{action['command']}`" if action.get("command") else "",
            )
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            *[f"- {note}" for note in report["notes"]],
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan safe public-release cleanup actions without modifying files.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--include-untracked", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    # include_untracked is recorded for operator intent; the audit CLI remains the source for branch-accurate scans.
    report = build_public_release_cleanup_plan(args.root, include_untracked=args.include_untracked)
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
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
