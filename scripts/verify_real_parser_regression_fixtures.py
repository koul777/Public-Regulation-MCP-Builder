from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT_ENV = "REG_RAG_REAL_PARSER_FIXTURE_ROOT"
DEFAULT_FIXTURE_ROOT = Path("data/public_portal_internal_rules_expanded_l3")


@dataclass(frozen=True)
class FixtureRequirement:
    fixture_id: str
    format: str
    filename_glob: str
    expected_sha256: str
    expected_size_bytes: int
    magic: bytes


DEFAULT_REQUIREMENTS = (
    FixtureRequirement(
        fixture_id="authority_delegation_hwp_3488_237887",
        format="hwp",
        filename_glob="3488_237887_*.hwp",
        expected_sha256="af22301287404791166110a9b939168f4c97de6ef6e8d27426171a3f82d25145",
        expected_size_bytes=1_045_504,
        magic=bytes.fromhex("d0 cf 11 e0 a1 b1 1a e1"),
    ),
    FixtureRequirement(
        fixture_id="contract_regulation_hwp_27297_237280",
        format="hwp",
        filename_glob="27297_237280_*.hwp",
        expected_sha256="470322ac754a5b9bb4eec16337ba78ce66185e4320e7a0aa90f9e814a149faac",
        expected_size_bytes=425_984,
        magic=bytes.fromhex("d0 cf 11 e0 a1 b1 1a e1"),
    ),
    FixtureRequirement(
        fixture_id="public_regulation_pdf_4860_235589",
        format="pdf",
        filename_glob="4860_235589_*.pdf",
        expected_sha256="22dace428f846be7b865ff5951236cb89cae3013786b861227449cb074ef4998",
        expected_size_bytes=1_730_950,
        magic=b"%PDF-",
    ),
)


def resolve_fixture_root(
    *,
    project_root: Path = PROJECT_ROOT,
    fixture_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    configured = fixture_root
    if configured is None:
        env_value = str(env.get(FIXTURE_ROOT_ENV, "")).strip()
        configured = Path(env_value) if env_value else DEFAULT_FIXTURE_ROOT
    if not configured.is_absolute():
        configured = project_root / configured
    return configured.resolve(strict=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_string(path: Path) -> str:
    return str(path.resolve(strict=False))


def _requirement_result(fixture_root: Path, requirement: FixtureRequirement) -> dict[str, object]:
    search_pattern = fixture_root / "**" / requirement.filename_glob
    base: dict[str, object] = {
        "fixture_id": requirement.fixture_id,
        "format": requirement.format,
        "filename_glob": requirement.filename_glob,
        "search_pattern": _path_string(search_pattern),
        "expected_sha256": requirement.expected_sha256,
        "expected_size_bytes": requirement.expected_size_bytes,
        "candidate_paths": [],
        "matched_path": None,
        "content_included": False,
    }

    if not fixture_root.exists():
        return {
            **base,
            "passed": False,
            "status": "fixture_root_missing",
            "message": f"Fixture root does not exist: {_path_string(fixture_root)}",
        }
    if not fixture_root.is_dir():
        return {
            **base,
            "passed": False,
            "status": "fixture_root_not_directory",
            "message": f"Fixture root is not a directory: {_path_string(fixture_root)}",
        }

    candidates = sorted(
        (path for path in fixture_root.rglob(requirement.filename_glob) if path.is_file()),
        key=lambda path: str(path).casefold(),
    )
    candidate_paths = [_path_string(path) for path in candidates]
    base["candidate_paths"] = candidate_paths
    if not candidates:
        return {
            **base,
            "passed": False,
            "status": "fixture_missing",
            "message": f"No file matched the required path pattern: {_path_string(search_pattern)}",
        }
    if len(candidates) > 1:
        return {
            **base,
            "passed": False,
            "status": "fixture_ambiguous",
            "message": f"Expected exactly one fixture but found {len(candidates)} matching paths.",
        }

    candidate = candidates[0]
    resolved_root = fixture_root.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    if not resolved_candidate.is_relative_to(resolved_root):
        return {
            **base,
            "passed": False,
            "status": "fixture_outside_root",
            "message": f"Fixture resolves outside the configured root: {_path_string(candidate)}",
        }

    before = candidate.stat()
    with candidate.open("rb") as handle:
        leading_bytes = handle.read(len(requirement.magic))
    actual_sha256 = _sha256(candidate)
    after = candidate.stat()
    base.update(
        {
            "matched_path": _path_string(candidate),
            "actual_sha256": actual_sha256,
            "actual_size_bytes": after.st_size,
        }
    )
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return {
            **base,
            "passed": False,
            "status": "fixture_changed_during_read",
            "message": "Fixture size or modification time changed while it was being verified.",
        }
    if leading_bytes != requirement.magic:
        return {
            **base,
            "passed": False,
            "status": "fixture_signature_mismatch",
            "message": f"Fixture does not have the expected {requirement.format.upper()} file signature.",
        }
    if after.st_size != requirement.expected_size_bytes:
        return {
            **base,
            "passed": False,
            "status": "fixture_size_mismatch",
            "message": (
                f"Fixture size {after.st_size} does not equal the pinned size "
                f"{requirement.expected_size_bytes}."
            ),
        }
    if actual_sha256 != requirement.expected_sha256:
        return {
            **base,
            "passed": False,
            "status": "fixture_hash_mismatch",
            "message": "Fixture SHA-256 does not equal the pinned regression fixture hash.",
        }
    return {
        **base,
        "passed": True,
        "status": "verified",
        "message": "Exactly one named fixture matched its pinned size, signature, and SHA-256.",
    }


def audit_real_parser_fixtures(
    fixture_root: Path,
    *,
    requirements: Iterable[FixtureRequirement] = DEFAULT_REQUIREMENTS,
) -> dict[str, object]:
    resolved_root = fixture_root.resolve(strict=False)
    results = [_requirement_result(resolved_root, requirement) for requirement in requirements]
    failure_count = sum(not bool(result["passed"]) for result in results)
    return {
        "report_type": "real_parser_regression_fixture_gate",
        "schema_version": 1,
        "passed": failure_count == 0 and bool(results),
        "profile": "release-required-real-parser-fixtures",
        "fixture_root": _path_string(resolved_root),
        "required_fixture_count": len(results),
        "verified_fixture_count": len(results) - failure_count,
        "failure_count": failure_count,
        "read_only_source_verification": True,
        "source_files_copied": False,
        "source_content_included": False,
        "fixtures": results,
    }


def resolve_named_fixture_for_tests(
    fixture_id: str,
    *,
    project_root: Path = PROJECT_ROOT,
    fixture_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    requirements = [requirement for requirement in DEFAULT_REQUIREMENTS if requirement.fixture_id == fixture_id]
    if not requirements:
        raise KeyError(f"Unknown real parser fixture id: {fixture_id}")
    root = resolve_fixture_root(
        project_root=project_root,
        fixture_root=fixture_root,
        environ=environ,
    )
    return _requirement_result(root, requirements[0])


def fixture_skip_reason(result: Mapping[str, object]) -> str:
    candidates = result.get("candidate_paths") or []
    candidate_detail = f" candidates={candidates}" if candidates else ""
    return (
        f"real parser fixture unavailable: id={result.get('fixture_id')} "
        f"status={result.get('status')} search={result.get('search_pattern')}{candidate_detail}; "
        "ordinary tests skip optional real data; run "
        "`python -m scripts.verify_real_parser_regression_fixtures` for the fail-closed release gate"
    )


def render_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Real parser regression fixture gate",
        "",
        f"- Passed: `{str(bool(report.get('passed'))).lower()}`",
        f"- Profile: `{report.get('profile')}`",
        f"- Fixture root: `{report.get('fixture_root')}`",
        f"- Verified: `{report.get('verified_fixture_count')}/{report.get('required_fixture_count')}`",
        f"- Failure count: `{report.get('failure_count')}`",
        "- Source verification: read-only; no source files or source content copied into this report.",
        "",
        "| Fixture | Format | Status | Matched path / searched pattern |",
        "| --- | --- | --- | --- |",
    ]
    for row in report.get("fixtures", []):
        assert isinstance(row, Mapping)
        path = row.get("matched_path") or row.get("search_pattern") or ""
        safe_path = str(path).replace("|", "\\|")
        lines.append(
            f"| `{row.get('fixture_id')}` | `{row.get('format')}` | `{row.get('status')}` | `{safe_path}` |"
        )
        candidates = row.get("candidate_paths") or []
        if row.get("status") == "fixture_ambiguous":
            for candidate in candidates:
                safe_candidate = str(candidate).replace("|", "\\|")
                lines.append(f"|  |  | candidate | `{safe_candidate}` |")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail closed unless each named real HWP/PDF parser regression fixture exists exactly once "
            "and matches its pinned integrity metadata. Source files are read only and never copied."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument(
        "--fixture-root",
        default=None,
        help=(
            f"Fixture directory, relative to project root when not absolute. Defaults to ${FIXTURE_ROOT_ENV} "
            f"or {DEFAULT_FIXTURE_ROOT.as_posix()}."
        ),
    )
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve(strict=False)
    fixture_root = resolve_fixture_root(
        project_root=project_root,
        fixture_root=Path(args.fixture_root) if args.fixture_root else None,
    )
    report = audit_real_parser_fixtures(fixture_root)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
