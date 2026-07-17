from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


PUBLIC_DOCS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/operator_quickstart_ko.md",
    "docs/public_institution_pilot_plan.md",
    "docs/pilot_acceptance_and_evidence_ko.md",
    "docs/public-institution-operations-runbook.md",
)
LICENSE_FILES = ("LICENSE", "LICENSE.md", "COPYING", "NOTICE")
ALLOWED_DATA_PATHS = {"data/uploads/.gitkeep", "data/exports/.gitkeep"}
ALLOWED_PUBLIC_SAMPLE_PATHS = {"data/ocr_smoke/blank_ocr_required.pdf"}
PUBLIC_REPORT_ALLOWLIST_FILE = "docs/public_release_report_allowlist.json"
TEXT_SCAN_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".csv", ".toml", ".yml", ".yaml", ".py"}
DOCUMENT_SAMPLE_SUFFIXES = {".hwp", ".hwpx", ".pdf", ".doc", ".docx"}
RISK_TERMS = (
    "source_record_id",
    "source_file_id",
    "apbaId",
    "public_batch_quality_",
    "public_portal_",
    "public_portal:",
)
NONPUBLIC_DOC_PREFIXES = ("docs/private_", "docs/internal_")
GENERATED_ARTIFACT_PREFIXES = ("tmp/", "output/", "build/", "dist/")
NONPUBLIC_TOOLING_PATHS = {".claude/launch.json"}
PUBLIC_DOC_NONPUBLIC_PATTERNS = (
    "docs/private_",
    "docs/internal_",
    "private_release_",
    "internal_mcp_operation",
    "../codex_directives",
    "reports/next_session_handoff",
    "reports/hermes_engineering_status",
    "reports/overnight_sessions",
    "reports/overnight_runs",
)


@dataclass(frozen=True)
class PublicFinding:
    severity: str
    code: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def audit_public_release(root: Path | str, tracked_paths: Iterable[str] | None = None) -> list[PublicFinding]:
    root_path = Path(root).resolve()
    tracked = sorted({path.replace("\\", "/") for path in (tracked_paths or _tracked_paths(root_path))})
    tracked_set = set(tracked)
    allowed_public_reports = _allowed_public_report_paths(root_path, tracked_set)
    findings: list[PublicFinding] = []

    if not any(filename in tracked_set for filename in LICENSE_FILES):
        findings.append(
            PublicFinding(
                "high",
                "missing-license",
                ".",
                "Public repositories need an explicit LICENSE/COPYING/NOTICE file before release.",
            )
        )

    for filename in PUBLIC_DOCS:
        if filename not in tracked_set:
            findings.append(
                PublicFinding(
                    "medium",
                    "missing-public-doc",
                    filename,
                    f"{filename} should be included in the public release branch.",
                )
            )

    for path in tracked:
        lower_path = path.lower()
        suffix = Path(path).suffix.lower()
        if lower_path in NONPUBLIC_TOOLING_PATHS:
            findings.append(
                PublicFinding(
                    "high",
                    "tracked-local-tooling-config",
                    path,
                    "Local editor or agent launch configuration should not ship in the public source repository.",
                )
            )
        if lower_path.startswith("config/") and not lower_path.endswith(".example.json"):
            findings.append(
                PublicFinding(
                    "high",
                    "tracked-institution-config",
                    path,
                    "Public source branches should include example configuration only; institution-specific query or ID files must be removed or synthesized.",
                )
            )
        if lower_path.startswith("reports/") and path not in allowed_public_reports:
            findings.append(
                PublicFinding(
                    "high",
                    "tracked-report-artifact",
                    path,
                    "Reports are generated evidence; public branches should keep only selected redacted artifacts.",
                )
            )
        if lower_path.startswith(GENERATED_ARTIFACT_PREFIXES):
            findings.append(
                PublicFinding(
                    "high",
                    "generated-artifact-path",
                    path,
                    "Generated runtime/build artifacts should not be included in a public source branch.",
                )
            )
        if lower_path.startswith(NONPUBLIC_DOC_PREFIXES):
            findings.append(
                PublicFinding(
                    "high",
                    "tracked-nonpublic-doc",
                    path,
                    "Private/internal handoff or operations docs should not ship in a public release branch.",
                )
            )
        if lower_path.startswith("data/") and path not in ALLOWED_DATA_PATHS | ALLOWED_PUBLIC_SAMPLE_PATHS:
            findings.append(
                PublicFinding(
                    "high",
                    "tracked-runtime-data",
                    path,
                    "Data files should be removed from public source branches unless synthetic or redistributable evidence is documented.",
                )
            )
        if (
            suffix in DOCUMENT_SAMPLE_SUFFIXES
            and lower_path.startswith(("data/", "reports/", "tests/fixtures/"))
            and path not in ALLOWED_PUBLIC_SAMPLE_PATHS
        ):
            findings.append(
                PublicFinding(
                    "high",
                    "tracked-document-sample",
                    path,
                    "Binary/source document samples need synthetic origin or redistribution approval before public release.",
                )
            )
        scan_text_path = lower_path.startswith(("reports/", "tests/fixtures/", "data/")) or (
            lower_path.startswith("config/") and not lower_path.endswith(".example.json")
        )
        if suffix in TEXT_SCAN_SUFFIXES and scan_text_path:
            findings.extend(_scan_text_risk_terms(root_path, path))
        if path in PUBLIC_DOCS and suffix in TEXT_SCAN_SUFFIXES:
            findings.extend(_scan_public_doc_nonpublic_references(root_path, path))

    return findings


def _allowed_public_report_paths(root: Path, tracked_set: set[str]) -> set[str]:
    if PUBLIC_REPORT_ALLOWLIST_FILE not in tracked_set:
        return set()
    manifest_path = root.joinpath(*Path(PUBLIC_REPORT_ALLOWLIST_FILE).parts)
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    allowed = payload.get("allowed_reports") if isinstance(payload, dict) else None
    if not isinstance(allowed, list):
        return set()
    paths = set()
    for value in allowed:
        if not isinstance(value, str):
            continue
        normalized = value.strip().replace("\\", "/")
        if normalized.startswith("reports/") and ".." not in Path(normalized).parts:
            paths.add(normalized)
    return paths


def _scan_text_risk_terms(root: Path, rel_path: str) -> list[PublicFinding]:
    absolute_path = root.joinpath(*Path(rel_path).parts)
    if not absolute_path.is_file() or absolute_path.stat().st_size > 1_000_000:
        return []
    text = absolute_path.read_text(encoding="utf-8", errors="replace")
    findings: list[PublicFinding] = []
    for term in RISK_TERMS:
        if term.lower() in text.lower():
            findings.append(
                PublicFinding(
                    "medium",
                    "institution-identifier-risk",
                    rel_path,
                    f"Contains '{term}', which should be reviewed or synthesized before public release.",
                )
            )
    return findings


def _scan_public_doc_nonpublic_references(root: Path, rel_path: str) -> list[PublicFinding]:
    absolute_path = root.joinpath(*Path(rel_path).parts)
    if not absolute_path.is_file() or absolute_path.stat().st_size > 1_000_000:
        return []
    text = absolute_path.read_text(encoding="utf-8", errors="replace")
    matches = sorted(
        {
            pattern
            for pattern in PUBLIC_DOC_NONPUBLIC_PATTERNS
            if pattern.lower() in text.lower()
        }
    )
    if not matches:
        return []
    line_numbers = _matching_line_numbers(text, matches)
    return [
        PublicFinding(
            "medium",
            "public-doc-nonpublic-reference",
            rel_path,
            "Public documentation references private/internal release material at "
            + _compact_line_refs(line_numbers)
            + ": "
            + ", ".join(matches),
        )
    ]


def _matching_line_numbers(text: str, patterns: Iterable[str]) -> list[int]:
    lower_patterns = [pattern.lower() for pattern in patterns]
    line_numbers: list[int] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        lower_line = line.lower()
        if any(pattern in lower_line for pattern in lower_patterns):
            line_numbers.append(line_number)
    return line_numbers


def _compact_line_refs(line_numbers: list[int], *, limit: int = 8) -> str:
    if not line_numbers:
        return "unknown lines"
    if len(line_numbers) <= limit:
        return "lines " + ", ".join(str(line) for line in line_numbers)
    visible = ", ".join(str(line) for line in line_numbers[:limit])
    return f"lines {visible}, ... (+{len(line_numbers) - limit})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit public GitHub release readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="Include untracked, non-ignored files when previewing a local public-release branch.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-json", default=None)
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if stdout is sys.stdout and hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    root = _repo_root(Path(args.root))
    try:
        tracked_paths = None
        if args.include_untracked:
            tracked_paths = _tracked_paths(root) + _untracked_paths(root)
        findings = audit_public_release(root, tracked_paths=tracked_paths)
    except RuntimeError as exc:
        print(f"public release readiness audit error: {exc}", file=stderr)
        return 2
    report = {
        "report_type": "public_release_readiness_audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(root),
        "passed": not findings,
        "finding_count": len(findings),
        "findings": [finding.to_dict() for finding in findings],
    }
    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    elif findings:
        for finding in findings:
            stdout.write(f"{finding.severity} {finding.code} {finding.path}: {finding.detail}\n")
    else:
        stdout.write("public release readiness audit passed\n")
    return 1 if findings else 0


def main() -> None:
    raise SystemExit(run())


def _repo_root(start: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return start.resolve()
    return Path(completed.stdout.decode("utf-8", "replace").strip()).resolve()


def _tracked_paths(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip() or "git ls-files failed")
    return [item for item in completed.stdout.decode("utf-8", "replace").split("\0") if item]


def _untracked_paths(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip() or "git ls-files --others failed")
    return [item for item in completed.stdout.decode("utf-8", "replace").split("\0") if item]


if __name__ == "__main__":
    main()
