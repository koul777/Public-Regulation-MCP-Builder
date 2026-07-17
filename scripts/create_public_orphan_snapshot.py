from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Sequence, TextIO
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_public_release_readiness import audit_public_release


class SnapshotError(RuntimeError):
    """Raised when a clean-history public snapshot cannot be created safely."""


def create_public_orphan_snapshot(
    source_root: str | Path,
    *,
    source_ref: str,
    output_dir: str | Path,
    commit_message: str = "Create clean-history public source snapshot",
) -> dict[str, object]:
    source = _repo_root(Path(source_root))
    destination = Path(output_dir).expanduser().resolve()
    _validate_destination(source, destination)

    source_commit = _git(source, "rev-parse", "--verify", f"{source_ref}^{{commit}}").strip()
    tracked_paths = _tracked_paths_at_ref(source, source_commit)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reg-rag-public-snapshot-") as tmp:
        temp_root = Path(tmp)
        archive_path = temp_root / "tracked-tree.zip"
        snapshot_root = temp_root / "snapshot"
        snapshot_root.mkdir()

        _git(source, "archive", "--format=zip", f"--output={archive_path}", source_commit)
        _extract_archive_safely(archive_path, snapshot_root)

        findings = audit_public_release(snapshot_root, tracked_paths=tracked_paths)
        if findings:
            details = "\n".join(
                f"- {finding.severity} {finding.code} {finding.path}: {finding.detail}"
                for finding in findings
            )
            raise SnapshotError(
                "The selected commit did not pass the public source audit. "
                "No snapshot was created.\n" + details
            )

        _git(snapshot_root, "init", "--initial-branch=main")
        _git(snapshot_root, "add", "--all")
        _git(
            snapshot_root,
            "-c",
            "user.name=Public Release Builder",
            "-c",
            "user.email=public-release@example.invalid",
            "commit",
            "-m",
            commit_message,
        )
        _verify_single_root_commit(snapshot_root)
        shutil.move(str(snapshot_root), destination)

    return {
        "status": "created",
        "source_ref": source_ref,
        "source_commit": source_commit,
        "output_dir": str(destination),
        "tracked_file_count": len(tracked_paths),
        "public_commit": _git(destination, "rev-parse", "HEAD").strip(),
        "commit_count": 1,
        "remote_count": 0,
        "automatic_push_performed": False,
    }


def _repo_root(start: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SnapshotError(completed.stderr.decode("utf-8", "replace").strip() or "Not a Git repository.")
    return Path(completed.stdout.decode("utf-8", "replace").strip()).resolve()


def _validate_destination(source: Path, destination: Path) -> None:
    if destination == source or source in destination.parents:
        raise SnapshotError("The output directory must be outside the private source repository.")
    if destination.exists():
        raise SnapshotError("The output directory must not already exist.")


def _tracked_paths_at_ref(source: Path, source_commit: str) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "core.quotePath=false",
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            source_commit,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SnapshotError(completed.stderr.decode("utf-8", "replace").strip() or "git ls-tree failed")
    return [path for path in completed.stdout.decode("utf-8", "replace").split("\0") if path]


def _extract_archive_safely(archive_path: Path, destination: Path) -> None:
    with ZipFile(archive_path) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise SnapshotError(f"Unsafe archive member: {member.filename}")
        archive.extractall(destination)


def _verify_single_root_commit(repo: Path) -> None:
    commit_count = int(_git(repo, "rev-list", "--all", "--count").strip())
    parent_line = _git(repo, "rev-list", "--parents", "--max-count=1", "HEAD").strip().split()
    remotes = _git(repo, "remote").splitlines()
    if commit_count != 1 or len(parent_line) != 1:
        raise SnapshotError("Snapshot history must contain exactly one parentless commit.")
    if remotes:
        raise SnapshotError("Snapshot repository unexpectedly contains a Git remote.")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise SnapshotError(detail or f"git {' '.join(args)} failed")
    return completed.stdout.decode("utf-8", "replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export one verified tracked tree into a new one-commit Git repository without private history."
    )
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--source-ref", required=True, help="Verified commit, tag, or branch to export.")
    parser.add_argument("--output-dir", required=True, help="New directory outside the private repository.")
    parser.add_argument("--commit-message", default="Create clean-history public source snapshot")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)
    try:
        report = create_public_orphan_snapshot(
            args.source_root,
            source_ref=args.source_ref,
            output_dir=args.output_dir,
            commit_message=args.commit_message,
        )
    except SnapshotError as exc:
        stderr.write(f"public snapshot creation blocked: {exc}\n")
        return 1
    stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
