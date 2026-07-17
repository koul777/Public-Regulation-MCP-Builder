from __future__ import annotations

import subprocess
from pathlib import Path


def current_repo_commit(repo_root: Path | None = None) -> str | None:
    root = repo_root or Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return None
    commit = completed.stdout.decode("utf-8", "replace").strip()
    return commit if len(commit) == 40 else None
