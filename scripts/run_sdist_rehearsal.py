from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Sequence, TextIO


DEFAULT_TESTS = (
    "tests.test_harness_docs",
    "tests.test_packaging_entrypoints",
    "tests.test_package_manifest",
)


def build_sdist_rehearsal_report(
    *,
    project_root: str | Path = ".",
    sdist_path: str | Path | None = None,
    sdist_dir: str | Path | None = None,
    tests: Sequence[str] = DEFAULT_TESTS,
    keep_temp: bool = False,
    timeout_seconds: float | None = 120.0,
) -> dict[str, object]:
    root = Path(project_root).resolve()
    temp_dir = Path(tempfile.mkdtemp(prefix="reg-rag-sdist-rehearsal-"))
    sdist: Path | None = None
    try:
        sdist = _resolve_sdist(root, sdist_path, sdist_dir)
        extracted_root = _extract_sdist(sdist, temp_dir)
        command = [sys.executable, "-m", "unittest", *tests, "-q"]
        completed = subprocess.run(
            command,
            cwd=extracted_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        passed = completed.returncode == 0
        report = {
            "report_type": "sdist_rehearsal",
            "passed": passed,
            "sdist_path": str(sdist),
            "extracted_root": str(extracted_root) if keep_temp else None,
            "tests": list(tests),
            "command": command,
            "exit_code": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
            "issues": [] if passed else [{"code": "sdist-test-failed", "severity": "high"}],
        }
    except Exception as exc:
        report = {
            "report_type": "sdist_rehearsal",
            "passed": False,
            "sdist_path": str(sdist) if sdist is not None else None,
            "extracted_root": None,
            "tests": list(tests),
            "command": None,
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "issues": [{"code": "sdist-rehearsal-error", "severity": "high", "detail": str(exc)}],
        }
    finally:
        if not keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return report


def _resolve_sdist(
    root: Path,
    sdist_path: str | Path | None,
    sdist_dir: str | Path | None = None,
) -> Path:
    if sdist_path is not None:
        sdist = Path(sdist_path)
        if not sdist.is_absolute():
            sdist = root / sdist
        if not sdist.is_file():
            raise FileNotFoundError(f"sdist does not exist: {sdist}")
        return sdist.resolve()
    dist = Path(sdist_dir) if sdist_dir is not None else Path("dist")
    if not dist.is_absolute():
        dist = root / dist
    candidates = sorted(dist.glob("reg_rag_preprocessor-*.tar.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(
            f"No reg_rag_preprocessor-*.tar.gz found in {dist}. Run python -m build --sdist first."
        )
    return candidates[0].resolve()


def _extract_sdist(sdist: Path, temp_dir: Path) -> Path:
    destination = temp_dir.resolve()
    with tarfile.open(sdist, mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"Unsafe sdist member path: {member.name}") from exc
        archive.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise RuntimeError(f"Expected exactly one extracted source directory, found {len(roots)}.")
    return roots[0]


def _tail(text: str, *, line_limit: int = 40) -> str:
    lines = text.splitlines()
    if len(lines) <= line_limit:
        return "\n".join(lines)
    return "\n".join(lines[-line_limit:])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rehearse source distribution behavior from an unpacked sdist.")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--sdist", default=None)
    parser.add_argument("--sdist-dir", default=None)
    parser.add_argument("--test", action="append", dest="tests", help="unittest module to run after unpacking.")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = build_sdist_rehearsal_report(
        project_root=args.project_root,
        sdist_path=args.sdist,
        sdist_dir=args.sdist_dir,
        tests=tuple(args.tests or DEFAULT_TESTS),
        keep_temp=args.keep_temp,
        timeout_seconds=args.timeout_seconds,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        stdout.write(payload + "\n")
    elif report["passed"]:
        stdout.write("sdist rehearsal passed\n")
    else:
        stdout.write("sdist rehearsal failed\n")
    return 1 if args.fail_on_issue and not report["passed"] else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
