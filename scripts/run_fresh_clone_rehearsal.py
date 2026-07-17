from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence, TextIO


SOURCE_ONLY_TESTS = (
    "tests.test_harness_docs",
    "tests.test_packaging_entrypoints",
    "tests.test_package_manifest",
)


@dataclass(frozen=True)
class FreshCloneStep:
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


def build_fresh_clone_steps(
    *,
    clone_root: Path,
    mode: str = "public",
    full: bool = False,
    python_executable: str | None = None,
    harness_timeout_seconds: float | None = 600.0,
) -> list[FreshCloneStep]:
    python = python_executable or sys.executable
    report_dir = clone_root / "reports"
    steps: list[FreshCloneStep] = []

    if full:
        venv_dir = clone_root / ".venv-rehearsal"
        venv_python = _venv_python(venv_dir)
        venv_env = _venv_env(venv_python)
        steps.append(
            FreshCloneStep(
                "create_venv",
                (python, "-m", "venv", str(venv_dir)),
            )
        )
        steps.append(
            FreshCloneStep(
                "install_package",
                (str(venv_python), "-m", "pip", "install", ".[dev]"),
                env=venv_env,
            )
        )
        python = str(venv_python)
    else:
        venv_env = {}

    steps.append(
        FreshCloneStep(
            "source_only_tests",
            (python, "-m", "unittest", *SOURCE_ONLY_TESTS, "-q"),
            env=venv_env,
        )
    )

    harness_command = [
        python,
        "scripts/run_release_harness.py",
        "--mode",
        mode,
        "--out-json",
        str(report_dir / ("fresh_clone_release_harness.json" if full else "fresh_clone_release_harness_plan.json")),
    ]
    if full:
        harness_command.append("--keep-going")
        if harness_timeout_seconds is not None:
            harness_command.extend(["--timeout-seconds", f"{harness_timeout_seconds:g}"])
    else:
        harness_command.append("--dry-run")
    steps.append(FreshCloneStep("release_harness_full" if full else "release_harness_plan", tuple(harness_command), env=venv_env))
    return steps


def build_fresh_clone_rehearsal_report(
    *,
    source_root: str | Path = ".",
    mode: str = "public",
    dry_run: bool = False,
    full: bool = False,
    work_dir: str | Path | None = None,
    keep_clone: bool = False,
    require_clean_source: bool = False,
    timeout_seconds: float | None = 120.0,
    harness_timeout_seconds: float | None = 600.0,
) -> dict[str, Any]:
    source = Path(source_root).resolve()
    issues: list[dict[str, str]] = []
    if not source.exists():
        issues.append(
            {
                "severity": "high",
                "code": "source-root-missing",
                "detail": f"Source root does not exist: {source}",
            }
        )
        return _report(
            mode=mode,
            dry_run=dry_run,
            full=full,
            source_root=source,
            clone_root=None,
            source_git={},
            issues=issues,
            steps=[],
        )

    source_git, git_issues = _read_source_git_state(source)
    issues.extend(git_issues)
    status_short = str(source_git.get("status_short", ""))
    if status_short:
        dirty_issue = {
            "severity": "high" if require_clean_source else "medium",
            "code": "source-worktree-dirty",
            "detail": "Fresh clone rehearsal uses committed files only; uncommitted or untracked files are not included.",
        }
        issues.append(dirty_issue)

    if dry_run:
        clone_root = Path("<fresh-clone-root>")
        steps = [step.to_dict() for step in build_fresh_clone_steps(clone_root=clone_root, mode=mode, full=full, python_executable="python")]
        return _report(
            mode=mode,
            dry_run=True,
            full=full,
            source_root=source,
            clone_root=None,
            source_git=source_git,
            issues=issues,
            steps=steps,
        )

    if any(issue["severity"] == "high" for issue in issues):
        return _report(
            mode=mode,
            dry_run=False,
            full=full,
            source_root=source,
            clone_root=None,
            source_git=source_git,
            issues=issues,
            steps=[],
        )

    temp_parent: Path | None = None
    clone_root: Path | None = None
    step_results: list[dict[str, Any]] = []
    try:
        if work_dir is None:
            temp_parent = Path(tempfile.mkdtemp(prefix="reg-rag-fresh-clone-"))
            clone_root = temp_parent / "repo"
        else:
            clone_root = Path(work_dir).resolve()
            if clone_root.exists() and any(clone_root.iterdir()):
                issues.append(
                    {
                        "severity": "high",
                        "code": "work-dir-not-empty",
                        "detail": f"Refusing to clone into non-empty directory: {clone_root}",
                    }
                )
                return _report(
                    mode=mode,
                    dry_run=False,
                    full=full,
                    source_root=source,
                    clone_root=clone_root,
                    source_git=source_git,
                    issues=issues,
                    steps=[],
                )

        git_root = str(source_git.get("root") or source)
        clone_result = _run_step(
            FreshCloneStep("git_clone", ("git", "clone", "--no-hardlinks", git_root, str(clone_root))),
            cwd=source,
            timeout_seconds=timeout_seconds,
        )
        step_results.append(clone_result)
        if not clone_result["passed"]:
            issues.append({"severity": "high", "code": "git-clone-failed", "detail": "git clone failed."})
            return _report(
                mode=mode,
                dry_run=False,
                full=full,
                source_root=source,
                clone_root=clone_root if keep_clone else None,
                source_git=source_git,
                issues=issues,
                steps=step_results,
            )

        clone_commit = _git_output(["rev-parse", "HEAD"], cwd=clone_root)
        if source_git.get("commit") and clone_commit and source_git["commit"] != clone_commit:
            issues.append(
                {
                    "severity": "high",
                    "code": "clone-commit-mismatch",
                    "detail": "Fresh clone HEAD does not match source HEAD.",
                }
            )

        for step in build_fresh_clone_steps(
            clone_root=clone_root,
            mode=mode,
            full=full,
            harness_timeout_seconds=harness_timeout_seconds,
        ):
            result = _run_step(step, cwd=clone_root, timeout_seconds=timeout_seconds)
            step_results.append(result)
            if step.required and not result["passed"]:
                issues.append(
                    {
                        "severity": "high",
                        "code": f"{step.name}-failed",
                        "detail": f"Fresh clone step failed: {step.name}",
                    }
                )
                break

        return _report(
            mode=mode,
            dry_run=False,
            full=full,
            source_root=source,
            clone_root=clone_root if keep_clone else None,
            source_git={**source_git, "clone_commit": clone_commit},
            issues=issues,
            steps=step_results,
        )
    finally:
        if temp_parent is not None and not keep_clone:
            shutil.rmtree(temp_parent, ignore_errors=True)


def _read_source_git_state(source: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    root = _git_output(["rev-parse", "--show-toplevel"], cwd=source)
    if not root:
        return {}, [
            {
                "severity": "high",
                "code": "source-not-git-repository",
                "detail": f"Source root is not inside a Git repository: {source}",
            }
        ]
    commit = _git_output(["rev-parse", "HEAD"], cwd=source)
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=source)
    status = _git_output(["status", "--short"], cwd=source)
    if not commit:
        issues.append({"severity": "high", "code": "source-commit-unavailable", "detail": "Could not resolve source HEAD."})
    return {"root": root, "commit": commit, "branch": branch, "status_short": status}, issues


def _git_output(args: Sequence[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _run_step(step: FreshCloneStep, *, cwd: Path, timeout_seconds: float | None) -> dict[str, Any]:
    started = time.monotonic()
    env = os.environ.copy()
    env.update(step.env)
    try:
        completed = subprocess.run(
            list(step.command),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
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
    return {
        "name": step.name,
        "required": step.required,
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "command": list(step.command),
        "env_keys": sorted(step.env),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _report(
    *,
    mode: str,
    dry_run: bool,
    full: bool,
    source_root: Path,
    clone_root: Path | None,
    source_git: dict[str, Any],
    issues: list[dict[str, str]],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    high_count = sum(1 for issue in issues if issue["severity"] == "high")
    failed_required = [step["name"] for step in steps if step.get("required") and not step.get("passed", True)]
    return {
        "report_type": "fresh_clone_rehearsal",
        "mode": mode,
        "dry_run": dry_run,
        "profile": "full" if full else "quick",
        "passed": high_count == 0 and not failed_required,
        "source_root": str(source_root),
        "clone_root": str(clone_root) if clone_root is not None else None,
        "source_git": source_git,
        "step_count": len(steps),
        "failed_required_step_names": failed_required,
        "high_count": high_count,
        "issue_count": len(issues),
        "issues": issues,
        "steps": steps,
    }


def _tail(text: str, *, line_limit: int = 40) -> str:
    lines = text.splitlines()
    if len(lines) <= line_limit:
        return "\n".join(lines)
    return "\n".join(lines[-line_limit:])


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_env(venv_python: Path) -> dict[str, str]:
    scripts_dir = str(venv_python.parent)
    return {"PATH": scripts_dir + os.pathsep + os.environ.get("PATH", "")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clone the current branch into a fresh worktree and rehearse release checks.")
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--mode", choices=("mcp", "internal", "public"), default="public")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full", action="store_true", help="Create a venv, install the package, and run the release harness.")
    parser.add_argument("--work-dir", default=None, help="Optional empty clone target. Defaults to a temporary directory.")
    parser.add_argument("--keep-clone", action="store_true")
    parser.add_argument("--require-clean-source", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--harness-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = build_fresh_clone_rehearsal_report(
        source_root=args.source_root,
        mode=args.mode,
        dry_run=args.dry_run,
        full=args.full,
        work_dir=args.work_dir,
        keep_clone=args.keep_clone,
        require_clean_source=args.require_clean_source,
        timeout_seconds=args.timeout_seconds,
        harness_timeout_seconds=args.harness_timeout_seconds,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        stdout.write(payload + "\n")
    elif report["passed"]:
        stdout.write("fresh clone rehearsal passed\n")
    else:
        stdout.write("fresh clone rehearsal failed\n")
    return 1 if args.fail_on_issue and not report["passed"] else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
