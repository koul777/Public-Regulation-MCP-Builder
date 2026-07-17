from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_github_repo_from_remote(remote_url: str) -> str:
    value = remote_url.strip()
    if value.endswith(".git"):
        value = value[:-4]

    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
        if "/" in path:
            return path

    marker = "github.com/"
    if marker in value:
        path = value.split(marker, 1)[1].strip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"

    raise ValueError(f"Could not parse GitHub owner/repo from remote URL: {remote_url!r}")


def sanitize_remote_url(remote_url: str) -> str:
    value = remote_url.strip()
    marker = "github.com/"
    if value.startswith("https://") and "@" in value and marker in value:
        return "https://github.com/" + value.split(marker, 1)[1]
    return value


def run_command(args: list[str], cwd: Path) -> CommandResult:
    completed = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def get_remote_url(repo_root: Path, remote_name: str) -> str:
    result = run_command(["git", "remote", "get-url", remote_name], cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git remote get-url {remote_name} failed")
    return result.stdout.strip()


def get_github_visibility(repo_root: Path, repo: str) -> dict[str, Any]:
    result = run_command(
        ["gh", "repo", "view", repo, "--json", "nameWithOwner,visibility,isPrivate,url"],
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh repo view {repo} failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh repo view returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("gh repo view returned a non-object JSON payload")
    return payload


def build_visibility_report(
    *,
    repo_root: Path,
    repo: str | None,
    remote_name: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    remote_url = get_remote_url(repo_root, remote_name)
    remote_repo = parse_github_repo_from_remote(remote_url)
    explicit_repo = repo is not None
    repo = repo or remote_repo
    repo_matches_remote = repo == remote_repo

    if not repo_matches_remote:
        return {
            "report_type": "github_private_visibility",
            "generated_at": generated_at or utc_now_iso(),
            "repo_commit": get_repo_commit(repo_root),
            "repo_root_name": repo_root.name,
            "remote_name": remote_name,
            "remote_url": sanitize_remote_url(remote_url),
            "remote_github_repo": remote_repo,
            "github_repo": repo,
            "explicit_repo_override": explicit_repo,
            "passed": False,
            "failed_check_names": ["github_repo_matches_remote"],
            "checks": [
                {
                    "name": "github_repo_matches_remote",
                    "passed": False,
                    "observed": {
                        "remote_github_repo": remote_repo,
                        "github_repo": repo,
                    },
                }
            ],
        }

    visibility = get_github_visibility(repo_root, repo)
    is_private = visibility.get("isPrivate") is True or visibility.get("visibility") == "PRIVATE"
    failed_check_names = [] if is_private else ["github_repository_private"]

    return {
        "report_type": "github_private_visibility",
        "generated_at": generated_at or utc_now_iso(),
        "repo_commit": get_repo_commit(repo_root),
        "repo_root_name": repo_root.name,
        "remote_name": remote_name,
        "remote_url": sanitize_remote_url(remote_url),
        "remote_github_repo": remote_repo,
        "github_repo": repo,
        "explicit_repo_override": explicit_repo,
        "passed": is_private,
        "failed_check_names": failed_check_names,
        "checks": [
            {
                "name": "github_repo_matches_remote",
                "passed": True,
                "observed": {
                    "remote_github_repo": remote_repo,
                    "github_repo": repo,
                },
            },
            {
                "name": "github_repository_private",
                "passed": is_private,
                "observed": {
                    "nameWithOwner": visibility.get("nameWithOwner"),
                    "visibility": visibility.get("visibility"),
                    "isPrivate": visibility.get("isPrivate"),
                },
            }
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail closed unless the target GitHub repository is private."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repo", help="GitHub owner/repo. Defaults to parsing the selected remote URL.")
    parser.add_argument("--out-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        report = build_visibility_report(
            repo_root=repo_root,
            repo=args.repo,
            remote_name=args.remote,
        )
    except Exception as exc:
        report = {
            "report_type": "github_private_visibility",
            "generated_at": utc_now_iso(),
            "repo_commit": get_repo_commit(repo_root),
            "repo_root_name": repo_root.name,
            "remote_name": args.remote,
            "github_repo": args.repo,
            "passed": False,
            "failed_check_names": ["github_visibility_check_error"],
            "error": str(exc),
        }
        if args.out_json:
            write_json(args.out_json, report)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    if args.out_json:
        write_json(args.out_json, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def get_repo_commit(repo_root: Path) -> str:
    result = run_command(["git", "rev-parse", "HEAD"], cwd=repo_root)
    return result.stdout.strip() if result.returncode == 0 else ""


if __name__ == "__main__":
    sys.exit(main())
