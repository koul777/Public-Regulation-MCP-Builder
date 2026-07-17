from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.check_regression_expectations import check_expectations, load_json
from scripts import audit_release_hygiene


def _latest_file(root: Path, pattern: str) -> Path | None:
    candidates = sorted(root.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


DEFAULT_GATES = (
    {
        "name": "public_portal_reuse_batch",
        "batch_report": "reports/public_batch_quality_20260703-221536.json",
        "batch_report_fallback_pattern": "reports/public_batch_quality_*.json",
        "expectations": "tests/fixtures/regression/public_portal_quality_expectations_20260703.json",
        "expectations_fallback_pattern": "tests/fixtures/regression/public_portal_quality_expectations_*.json",
        "required_fixture_count": 26,
    },
    {
        "name": "integrated_pdf_reuse_batch",
        "batch_report": "reports/public_batch_quality_20260703-221530.json",
        "batch_report_fallback_pattern": "reports/public_batch_quality_*.json",
        "expectations": "tests/fixtures/regression/integrated_pdf_quality_expectations_20260703.json",
        "expectations_fallback_pattern": "tests/fixtures/regression/integrated_pdf_quality_expectations_*.json",
        "required_fixture_count": 1,
    },
)


def _resolve_artifact_path(
    root: Path,
    configured_path: str,
    fallback_pattern: str | None,
    *,
    artifact_type: str,
    gate_name: str,
) -> tuple[Path | None, bool, str | None]:
    configured = root / configured_path
    if configured.is_file():
        return configured, False, None
    if fallback_pattern:
        fallback = _latest_file(root, fallback_pattern)
        if fallback is not None:
            details = (
                f"{gate_name}:{artifact_type}:configured_missing:{configured_path}:fallback_to:{fallback.relative_to(root).as_posix()}"
            )
            return fallback, True, details
    return configured, False, f"{gate_name}:{artifact_type}:configured_missing:{configured_path}:fallback_failed"


def run_regression_gate(
    *,
    project_root: Path | None = None,
    gates: tuple[dict[str, Any], ...] = DEFAULT_GATES,
    include_release_hygiene: bool = False,
    release_hygiene_workflow_scope: str = "auto",
    release_hygiene_include_source_path_scan: bool = False,
) -> dict[str, Any]:
    root = audit_release_hygiene.resolve_repo_root(project_root or Path.cwd())
    results: list[dict[str, Any]] = []
    for gate in gates:
        batch_report_path, used_batch_fallback, batch_fallback_detail = _resolve_artifact_path(
            root,
            gate["batch_report"],
            gate.get("batch_report_fallback_pattern"),
            artifact_type="batch_report",
            gate_name=gate["name"],
        )
        expectations_path, used_expectations_fallback, expectations_fallback_detail = _resolve_artifact_path(
            root,
            gate["expectations"],
            gate.get("expectations_fallback_pattern"),
            artifact_type="expectations",
            gate_name=gate["name"],
        )
        fallback_details: list[str] = []
        if batch_fallback_detail:
            fallback_details.append(batch_fallback_detail)
        if expectations_fallback_detail:
            fallback_details.append(expectations_fallback_detail)
        if not batch_report_path.is_file():
            results.append(
                {
                    "name": gate["name"],
                    "passed": False,
                    "reason": "missing_batch_report",
                    "batch_report": str(batch_report_path),
                    "fallback_details": fallback_details,
                }
            )
            continue
        if not expectations_path.is_file():
            results.append(
                {
                    "name": gate["name"],
                    "passed": False,
                    "reason": "missing_expectations",
                    "expectations": str(expectations_path),
                    "fallback_details": fallback_details,
                }
            )
            continue
        batch_report = load_json(batch_report_path)
        expectations = load_json(expectations_path)
        result = check_expectations(batch_report, expectations)
        required_fixture_count = int(gate.get("required_fixture_count", 0) or 0)
        passed = bool(result.get("passed")) and result.get("checked_count", 0) >= required_fixture_count
        results.append(
            {
                "name": gate["name"],
                "passed": passed,
                "batch_report": str(batch_report_path.relative_to(root)).replace("\\", "/"),
                "batch_report_was_fallback": bool(used_batch_fallback),
                "expectations": str(expectations_path.relative_to(root)).replace("\\", "/"),
                "expectations_was_fallback": bool(used_expectations_fallback),
                "checked_count": result.get("checked_count", 0),
                "required_fixture_count": required_fixture_count,
                "failure_count": result.get("failure_count", 0),
                "failures": result.get("failures", [])[:20],
                "fallback_details": fallback_details,
            }
        )
    release_hygiene = None
    if include_release_hygiene:
        release_hygiene = run_release_hygiene_gate(
            project_root=root,
            workflow_scope=release_hygiene_workflow_scope,
            include_source_path_scan=release_hygiene_include_source_path_scan,
        )

    passed = all(item.get("passed") for item in results)
    if release_hygiene is not None:
        passed = passed and bool(release_hygiene.get("passed"))
    return {
        "report_type": "ci_regression_gate",
        "passed": passed,
        "gate_count": len(results),
        "gates": results,
        "release_hygiene": release_hygiene,
    }


def run_release_hygiene_gate(
    *,
    project_root: Path,
    workflow_scope: str = "auto",
    include_source_path_scan: bool = False,
) -> dict[str, Any]:
    try:
        candidate_paths = audit_release_hygiene.collect_candidate_paths(project_root)
        findings = audit_release_hygiene.audit_paths(
            project_root,
            candidate_paths,
            workflow_scope_unavailable=audit_release_hygiene.workflow_scope_is_unavailable(workflow_scope),
            include_source_path_scan=include_source_path_scan,
        )
        allowlist_path = project_root / audit_release_hygiene.DEFAULT_ALLOWLIST_FILENAME
        filtered_findings = audit_release_hygiene.filter_allowed_findings(
            findings,
            audit_release_hygiene.load_allowlist(allowlist_path),
        )
    except audit_release_hygiene.AuditError as exc:
        return {
            "name": "release_hygiene",
            "passed": False,
            "reason": "audit_error",
            "error": str(exc),
        }

    return {
        "name": "release_hygiene",
        "passed": not filtered_findings,
        "finding_count": len(filtered_findings),
        "suppressed_finding_count": len(findings) - len(filtered_findings),
        "findings": [finding.to_dict() for finding in filtered_findings[:50]],
        "workflow_scope": workflow_scope,
        "include_source_path_scan": include_source_path_scan,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run pinned PUBLIC_PORTAL and integrated PDF regression gates for CI.")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--skip-release-hygiene", action="store_true")
    parser.add_argument("--workflow-scope", choices=("auto", "available", "unavailable"), default="auto")
    parser.add_argument("--include-source-path-scan", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = audit_release_hygiene.resolve_repo_root(Path(args.project_root) if args.project_root else Path.cwd())
    report = run_regression_gate(
        project_root=root,
        include_release_hygiene=not args.skip_release_hygiene,
        release_hygiene_workflow_scope=args.workflow_scope,
        release_hygiene_include_source_path_scan=args.include_source_path_scan,
    )
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["out_json"] = str(out_json)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
