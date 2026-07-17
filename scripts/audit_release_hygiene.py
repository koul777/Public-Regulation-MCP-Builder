#!/usr/bin/env python3
"""Offline release hygiene checks for files Git intends to track."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence, TextIO


DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_SCAN_BYTES = 1024 * 1024
MAX_PATH_FINDINGS_PER_FILE = 5
DEFAULT_LOCAL_PATH_SCAN_PREFIXES = ("reports/", "data/", "dist/", "build/", "release/")
DEFAULT_ALLOWLIST_FILENAME = ".release-hygiene-allowlist.json"
NON_ATTRIBUTABLE_APPROVERS = {
    "automation",
    "codex",
    "local",
    "operator",
    "release-hardening-session",
    "release-hardening-test",
    "session",
    "test",
    "unit-test",
    "unknown",
}


class AuditError(RuntimeError):
    """Raised when the audit cannot collect candidate files."""


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


LOCAL_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "windows-user-path",
        re.compile(
            r"(?<![\w.-])[A-Za-z]:[\\/](?:Users|Documents and Settings)"
            r"[\\/][A-Za-z0-9._ -]+(?:[\\/][^\s\"'<>|]+)*",
            re.IGNORECASE,
        ),
    ),
    (
        "windows-workspace-path",
        re.compile(
            r"(?<![\w.-])[A-Za-z]:[\\/](?:workspace|workspaces|repos?|src|tmp|temp)"
            r"[\\/][^\s\"'<>|]+",
            re.IGNORECASE,
        ),
    ),
    (
        "posix-user-path",
        re.compile(r"(?<![\w.-])/(?:Users|home)/[A-Za-z0-9._-]+(?:/[^\s\"'<>`]+)+"),
    ),
    (
        "wsl-path",
        re.compile(
            r"(?<![\w.-])/(?:mnt|cygdrive)/[a-z]/"
            r"(?:Users|workspace|workspaces|repos?|src|tmp|temp)(?:/[^\s\"'<>`]+)+",
            re.IGNORECASE,
        ),
    ),
    (
        "file-uri-local-path",
        re.compile(r"file:///(?:[A-Za-z]:/|(?:Users|home)/)[^\s\"'<>`]+", re.IGNORECASE),
    ),
)


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)([kmgt]?b?)?\s*", value, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid byte size: {value!r}")

    amount = int(match.group(1))
    suffix = (match.group(2) or "").lower().rstrip("b")
    multipliers = {
        "": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
        "t": 1024**4,
    }
    return amount * multipliers[suffix]


def resolve_repo_root(start: Path) -> Path:
    start = start.resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return start

    if completed.returncode != 0:
        return start

    output = completed.stdout.decode("utf-8", "replace").strip()
    return Path(output).resolve() if output else start


def collect_candidate_paths(root: Path, include_untracked: bool = False) -> list[str]:
    paths = _git_null_list(root, ["ls-files", "-z", "--cached"])
    paths.extend(_git_null_list(root, ["diff", "-z", "--name-only", "--cached", "--diff-filter=ACMRTD"]))
    paths.extend(_git_null_list(root, ["diff", "-z", "--name-only", "--diff-filter=ACMRTD"]))
    if include_untracked:
        paths.extend(_git_null_list(root, ["ls-files", "-z", "--others", "--exclude-standard"]))
    return paths


def workflow_scope_is_unavailable(mode: str, env: Mapping[str, str] | None = None) -> bool:
    if mode == "unavailable":
        return True
    if mode == "available":
        return False

    env = os.environ if env is None else env
    explicit = env.get("RELEASE_HYGIENE_WORKFLOW_SCOPE", "").strip().lower()
    if explicit in {"unavailable", "missing", "absent", "none", "false", "0", "no"}:
        return True
    if explicit in {"available", "present", "true", "1", "yes"}:
        return False

    for key in ("GITHUB_TOKEN_SCOPES", "GH_TOKEN_SCOPES", "TOKEN_SCOPES"):
        if key not in env:
            continue
        scopes = {part.strip().lower() for part in re.split(r"[,\s]+", env.get(key, "")) if part.strip()}
        return "workflow" not in scopes

    return False


def audit_paths(
    root: Path | str,
    candidate_paths: Iterable[Path | str],
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    workflow_scope_unavailable: bool = False,
    include_source_path_scan: bool = False,
) -> list[Finding]:
    root_path = Path(root).resolve()
    patterns = _local_path_patterns(root_path)
    findings: list[Finding] = []

    for rel_path in _normalised_candidate_paths(candidate_paths):
        absolute_path = root_path.joinpath(*PurePosixPath(rel_path).parts)
        if workflow_scope_unavailable and _is_workflow_file(rel_path):
            findings.append(
                Finding(
                    "workflow-file-without-scope",
                    rel_path,
                    "workflow files require a token or credential with workflow scope to push",
                )
            )

        if not absolute_path.is_file():
            continue

        size = absolute_path.stat().st_size
        if size > max_file_bytes:
            findings.append(
                Finding(
                    "oversized-file",
                    rel_path,
                    f"{size} bytes exceeds configured limit of {max_file_bytes} bytes",
                )
            )

        if include_source_path_scan or _is_artifact_path_scan_candidate(rel_path):
            findings.extend(
                _scan_file_for_local_paths(
                    absolute_path,
                    rel_path,
                    patterns,
                    max_scan_bytes=max_scan_bytes,
                )
            )

    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit tracked artifacts before release or push.")
    parser.add_argument("--root", default=".", help="repository root or any directory inside the repository")
    parser.add_argument(
        "--max-file-bytes",
        type=parse_size,
        default=DEFAULT_MAX_FILE_BYTES,
        help="maximum allowed tracked file size; accepts plain bytes or K/M/G suffixes",
    )
    parser.add_argument(
        "--max-scan-bytes",
        type=parse_size,
        default=DEFAULT_MAX_SCAN_BYTES,
        help="maximum bytes to scan per text file for local path leaks",
    )
    parser.add_argument(
        "--workflow-scope",
        choices=("auto", "available", "unavailable"),
        default="auto",
        help="whether the push credential has workflow scope",
    )
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="also audit untracked, non-ignored files as intended artifacts",
    )
    parser.add_argument(
        "--include-source-path-scan",
        action="store_true",
        help="also scan source, docs, and tests for local path literals; by default path scanning targets artifacts",
    )
    parser.add_argument(
        "--allowlist",
        default=None,
        help="JSON allowlist for intentional findings; defaults to .release-hygiene-allowlist.json when present",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument("--out-json", default=None, help="write findings JSON to this file")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env

    parser = build_parser()
    args = parser.parse_args(argv)
    root = resolve_repo_root(Path(args.root))

    try:
        candidate_paths = collect_candidate_paths(root, include_untracked=args.include_untracked)
    except AuditError as exc:
        if args.out_json:
            _write_hygiene_error_json(Path(args.out_json), exc)
        print(f"release hygiene audit error: {exc}", file=stderr)
        return 2

    raw_findings = audit_paths(
        root,
        candidate_paths,
        max_file_bytes=args.max_file_bytes,
        max_scan_bytes=args.max_scan_bytes,
        workflow_scope_unavailable=workflow_scope_is_unavailable(args.workflow_scope, env),
        include_source_path_scan=args.include_source_path_scan,
    )
    allowlist_path = Path(args.allowlist) if args.allowlist else root / DEFAULT_ALLOWLIST_FILENAME
    allowlist_rules = load_allowlist(allowlist_path)
    findings = filter_allowed_findings(raw_findings, allowlist_rules)
    if args.out_json:
        _write_hygiene_report_json(
            Path(args.out_json),
            findings,
            raw_findings=raw_findings,
            root=root,
            allowlist_path=allowlist_path,
            allowlist_rules=allowlist_rules,
        )
    _write_findings(findings, stdout, as_json=args.json)
    return 1 if findings else 0


def main() -> None:
    raise SystemExit(run())


def _git_null_list(root: Path, git_args: Sequence[str]) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *git_args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AuditError("git executable was not found") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise AuditError(stderr or "git command failed")

    return [
        item.decode("utf-8", "surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def load_allowlist(path: Path | str | None) -> list[dict[str, str]]:
    if not path:
        return []
    allowlist_path = Path(path)
    if not allowlist_path.is_file():
        return []
    data = json.loads(allowlist_path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        rules = data.get("allowed_findings") or []
    else:
        rules = data
    return [rule for rule in rules if isinstance(rule, dict)]


def filter_allowed_findings(findings: Sequence[Finding], rules: Sequence[dict[str, str]]) -> list[Finding]:
    return [finding for finding in findings if not _finding_allowed(finding, rules)]


def _finding_allowed(finding: Finding, rules: Sequence[dict[str, str]]) -> bool:
    for rule in rules:
        code = str(rule.get("code") or "")
        path = str(rule.get("path") or "")
        detail_contains = str(rule.get("detail_contains") or "")
        if code and code != finding.code:
            continue
        if path and path != finding.path:
            continue
        if detail_contains and detail_contains not in finding.detail:
            continue
        if code or path or detail_contains:
            return True
    return False


def _normalised_candidate_paths(candidate_paths: Iterable[Path | str]) -> list[str]:
    normalised: set[str] = set()
    for candidate in candidate_paths:
        text = str(candidate).replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if not text or text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
            continue
        parts = PurePosixPath(text).parts
        if ".." in parts:
            continue
        normalised.add("/".join(parts))
    return sorted(normalised)


def _local_path_patterns(root: Path) -> tuple[tuple[str, re.Pattern[str]], ...]:
    patterns = list(LOCAL_PATH_PATTERNS)
    root_candidates = {str(root), str(root).replace("\\", "/")}
    for candidate in sorted(root_candidates):
        if len(candidate) < 8 or not any(separator in candidate for separator in ("/", "\\")):
            continue
        flags = re.IGNORECASE if os.name == "nt" else 0
        patterns.append(("repository-root-path", re.compile(re.escape(candidate), flags)))
    return tuple(patterns)


def _is_workflow_file(rel_path: str) -> bool:
    lower_path = rel_path.lower()
    suffix = PurePosixPath(lower_path).suffix
    return lower_path.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}


def _is_artifact_path_scan_candidate(rel_path: str) -> bool:
    lower_path = rel_path.lower()
    return lower_path.startswith(DEFAULT_LOCAL_PATH_SCAN_PREFIXES)


def _scan_file_for_local_paths(
    absolute_path: Path,
    rel_path: str,
    patterns: Sequence[tuple[str, re.Pattern[str]]],
    *,
    max_scan_bytes: int,
) -> list[Finding]:
    with absolute_path.open("rb") as handle:
        data = handle.read(max_scan_bytes)

    if _looks_binary(data):
        return []

    text = data.decode("utf-8", "replace")
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            findings.append(
                Finding(
                    "local-path-leak",
                    rel_path,
                    f"{pattern_name} on line {line_number}: {_shorten(match.group(0))}",
                )
            )
            break
        if len(findings) >= MAX_PATH_FINDINGS_PER_FILE:
            break
    return findings


def _looks_binary(data: bytes) -> bool:
    return b"\0" in data[:4096]


def _shorten(value: str, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _write_findings(findings: Sequence[Finding], stdout: TextIO, *, as_json: bool) -> None:
    if as_json:
        json.dump([finding.to_dict() for finding in findings], stdout, indent=2)
        stdout.write("\n")
        return

    if not findings:
        stdout.write("release hygiene audit passed\n")
        return

    stdout.write("release hygiene audit failed\n")
    for finding in findings:
        stdout.write(f"- {finding.code}: {finding.path}: {finding.detail}\n")


def _write_hygiene_report_json(
    path: Path,
    findings: Sequence[Finding],
    *,
    raw_findings: Sequence[Finding] | None = None,
    root: Path | None = None,
    allowlist_path: Path | None = None,
    allowlist_rules: Sequence[dict[str, str]] = (),
) -> None:
    raw_finding_list = list(raw_findings if raw_findings is not None else findings)
    suppressed_findings = _suppressed_findings(raw_finding_list, findings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "report_type": "release_hygiene",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "repo_commit": _git_commit(root) if root is not None else "",
                "passed": not findings,
                "raw_finding_count": len(raw_finding_list),
                "suppressed_finding_count": len(suppressed_findings),
                "allowlist": _allowlist_metadata(root, allowlist_path, allowlist_rules),
                "suppressed_findings_by_code": dict(Counter(finding.code for finding in suppressed_findings)),
                "suppressed_findings_preview": [
                    {"code": finding.code, "path": finding.path} for finding in suppressed_findings[:50]
                ],
                "finding_count": len(findings),
                "findings": [finding.to_dict() for finding in findings],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _suppressed_findings(raw_findings: Sequence[Finding], findings: Sequence[Finding]) -> list[Finding]:
    remaining = Counter(_finding_key(finding) for finding in findings)
    suppressed: list[Finding] = []
    for finding in raw_findings:
        key = _finding_key(finding)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            suppressed.append(finding)
    return suppressed


def _finding_key(finding: Finding) -> tuple[str, str, str]:
    return finding.code, finding.path, finding.detail


def _allowlist_metadata(
    root: Path | None, allowlist_path: Path | None, rules: Sequence[dict[str, str]]
) -> dict[str, object]:
    rule_count = len(rules)
    missing_approval_metadata_count = sum(
        1
        for rule in rules
        if not (rule.get("approved_by") and rule.get("approved_at") and rule.get("approval_reference"))
    )
    non_attributable_approval_count = sum(
        1 for rule in rules if not _approval_is_attributable(str(rule.get("approved_by") or ""))
    )
    if allowlist_path is None:
        return {
            "configured": False,
            "rule_count": rule_count,
            "missing_approval_metadata_count": missing_approval_metadata_count,
            "non_attributable_approval_count": non_attributable_approval_count,
        }
    exists = allowlist_path.is_file()
    return {
        "configured": exists,
        "path": _display_allowlist_path(root, allowlist_path),
        "sha256": _sha256_file(allowlist_path) if exists else None,
        "rule_count": rule_count,
        "missing_approval_metadata_count": missing_approval_metadata_count,
        "non_attributable_approval_count": non_attributable_approval_count,
    }


def _approval_is_attributable(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized in NON_ATTRIBUTABLE_APPROVERS:
        return False
    if normalized.endswith("-session") or normalized.endswith("-test"):
        return False
    return True


def _display_allowlist_path(root: Path | None, allowlist_path: Path) -> str:
    resolved = allowlist_path.resolve()
    if root is not None:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return allowlist_path.name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_hygiene_error_json(path: Path, exc: AuditError) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "report_type": "release_hygiene",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "repo_commit": "",
                "passed": False,
                "error_type": "audit_error",
                "error": str(exc),
                "findings": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


if __name__ == "__main__":
    main()
