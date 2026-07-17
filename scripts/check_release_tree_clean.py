from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence, TextIO


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _parse_porcelain(output: str) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        if len(raw_line) < 3:
            continue
        status = raw_line[:2]
        path = raw_line[3:]
        changes.append(
            {
                "status": status,
                "path": path,
                "index_status": status[0],
                "worktree_status": status[1],
            }
        )
    return changes


def check_release_tree_clean(project_root: Path) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    status_process = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status_process.returncode != 0:
        return {
            "report_type": "release_tree_cleanliness",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_root": str(root),
            "passed": False,
            "error": "git_status_failed",
            "git_stderr": status_process.stderr.strip(),
            "change_count": 0,
            "changes": [],
        }

    changes = _parse_porcelain(status_process.stdout)
    head_process = _git(root, "rev-parse", "HEAD")
    head = head_process.stdout.strip() if head_process.returncode == 0 else None
    staged_count = sum(1 for item in changes if item["index_status"] not in {" ", "?"})
    unstaged_count = sum(1 for item in changes if item["worktree_status"] not in {" ", "?"})
    untracked_count = sum(1 for item in changes if item["status"] == "??")
    return {
        "report_type": "release_tree_cleanliness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "head": head,
        "passed": not changes,
        "policy": "A release build must start from a Git tree with no staged, unstaged, or untracked changes.",
        "change_count": len(changes),
        "staged_count": staged_count,
        "unstaged_count": unstaged_count,
        "untracked_count": untracked_count,
        "changes": changes,
    }


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail closed when a release build tree is not Git-clean.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args(argv)
    report = check_release_tree_clean(args.project_root)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(payload + "\n", encoding="utf-8")
    (stdout or sys.stdout).write(payload + "\n")
    return 0 if report["passed"] else 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
