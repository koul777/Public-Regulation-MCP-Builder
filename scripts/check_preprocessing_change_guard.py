from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVIEW_LABEL = "preprocessing-reviewed"

LOGIC_PREFIXES = (
    "app/parsers/",
    "app/processors/",
    "app/mcp_server/",
)
LOGIC_FILES = {
    "app/core/pipeline.py",
    "app/services/processing_service.py",
    "app/schemas/chunk.py",
    "app/schemas/parsed.py",
    "app/schemas/quality.py",
    "app/schemas/structure.py",
    "frontend/streamlit_app.py",
    "scripts/analyze_regulation_corpus.py",
    "scripts/audit_table_preprocessing_claim_gate.py",
    "scripts/batch_process_regulations.py",
    "scripts/check_parsing_goldset_table_drift.py",
    "scripts/check_mcp_connection_readiness.py",
    "scripts/check_regression_expectations.py",
    "scripts/refresh_table_exports.py",
    "scripts/generate_mcp_client_config.py",
    "scripts/mcp_bundle_contract.py",
    "scripts/run_mcp_client_config_smoke.py",
    "scripts/run_mcp_transport_smoke.py",
    "scripts/run_regulation_mcp.py",
    "scripts/run_ci_regression_gate.py",
    "scripts/run_public_batch_pipeline.py",
    "scripts/verify_real_parser_regression_fixtures.py",
}
BASELINE_PREFIXES = ("tests/fixtures/regression/",)
BASELINE_FILES = {"config/quality_profiles.example.json"}
GOVERNANCE_FILES = {
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/workflows/preprocessing-change-policy.yml",
    ".github/workflows/preprocessing-regression.yml",
    "scripts/check_preprocessing_change_guard.py",
    "tests/test_github_workflow_templates.py",
    "tests/test_preprocessing_change_guard.py",
}
GUARD_IMPLEMENTATION_FILES = {
    ".github/workflows/preprocessing-change-policy.yml",
    ".github/workflows/preprocessing-regression.yml",
    "scripts/check_preprocessing_change_guard.py",
}
GUARD_TEST_FILES = {
    "tests/test_github_workflow_templates.py",
    "tests/test_preprocessing_change_guard.py",
}
FOCUSED_TEST_KEYWORDS = (
    "archive_safety",
    "article_validity",
    "chunk",
    "hwp_inventory",
    "metadata_extractor",
    "mcp",
    "normalizer",
    "parser",
    "parsing",
    "pipeline",
    "preprocess",
    "processing_service",
    "quality",
    "structure",
    "streamlit_operator",
    "table",
)
BODY_FIELDS = (
    "summary",
    "affected-formats",
    "invariants",
    "regression-evidence",
    "baseline-change",
)
PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "todo",
    "tbd",
    "작성",
    "작성 필요",
    "작성해 주세요",
    "describe",
    "required",
}
NO_BASELINE_CHANGE_VALUES = {
    "n/a",
    "none",
    "없음",
    "해당 없음",
    "변경 없음",
    "기준값 변경 없음",
}
DELETED_STATUSES = {"d", "deleted", "removed"}


class GuardInputError(RuntimeError):
    pass


def normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def parse_changed_lines(lines: Iterable[str]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        status = "modified"
        paths = parts
        first = parts[0].strip()
        if len(parts) >= 2 and (
            re.fullmatch(r"[A-Z][0-9]*", first.upper())
            or first.casefold() in {"added", "changed", "copied", "deleted", "modified", "removed", "renamed"}
        ):
            status = first
            paths = parts[1:]
        if status.upper().startswith(("R", "C")) and len(paths) >= 2:
            old_path = normalize_path(paths[-2])
            new_path = normalize_path(paths[-1])
            if old_path:
                changes.append({"path": old_path, "status": "removed"})
            if new_path:
                changes.append({"path": new_path, "status": "renamed"})
            continue
        path = normalize_path(paths[-1])
        if path:
            changes.append({"path": path, "status": status})
    deduplicated: dict[tuple[str, str], dict[str, str]] = {}
    for change in changes:
        key = (change["path"], change["status"].casefold())
        deduplicated[key] = change
    return sorted(deduplicated.values(), key=lambda item: (item["path"], item["status"]))


def git_changed_files(project_root: Path, base_ref: str, head_ref: str) -> list[dict[str, str]]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "--find-renames", f"{base_ref}...{head_ref}"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = detail[-1] if detail else "git diff failed"
        raise GuardInputError(f"Could not compare {base_ref}...{head_ref}: {suffix}")
    return parse_changed_lines(result.stdout.splitlines())


def load_event(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardInputError(f"Could not read GitHub event JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def event_pull_request_context(payload: dict[str, Any]) -> dict[str, Any]:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return {"body": "", "labels": [], "base_ref": "", "head_ref": ""}
    labels = []
    for item in pull_request.get("labels") or []:
        if isinstance(item, dict) and item.get("name"):
            labels.append(str(item["name"]))
    base = pull_request.get("base") if isinstance(pull_request.get("base"), dict) else {}
    head = pull_request.get("head") if isinstance(pull_request.get("head"), dict) else {}
    return {
        "body": str(pull_request.get("body") or ""),
        "labels": labels,
        "base_ref": str(base.get("sha") or ""),
        "head_ref": str(head.get("sha") or ""),
    }


def _matches_prefix_or_file(path: str, prefixes: tuple[str, ...], files: set[str]) -> bool:
    return path in files or any(path.startswith(prefix) for prefix in prefixes)


def _is_deleted(change: dict[str, str]) -> bool:
    status = change.get("status", "").casefold()
    return status in DELETED_STATUSES or status.startswith("d")


def _is_focused_test(path: str) -> bool:
    if not path.startswith("tests/test_") or not path.endswith(".py"):
        return False
    filename = Path(path).name.casefold()
    return any(keyword in filename for keyword in FOCUSED_TEST_KEYWORDS)


def extract_body_fields(body: str) -> dict[str, str | None]:
    fields: dict[str, str | None] = {}
    for field in BODY_FIELDS:
        pattern = re.compile(
            rf"<!--\s*preprocessing-guard:{re.escape(field)}\s*-->(.*?)"
            rf"<!--\s*/preprocessing-guard:{re.escape(field)}\s*-->",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(body)
        fields[field] = match.group(1).strip() if match else None
    return fields


def _normalized_value(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("`*_-").casefold()


def _is_placeholder(value: str | None) -> bool:
    normalized = _normalized_value(value)
    return normalized in PLACEHOLDER_VALUES or normalized.startswith(("예:", "example:", "작성해"))


def evaluate_guard(
    changes: list[dict[str, str]],
    *,
    pr_body: str,
    labels: Iterable[str],
    review_label: str = REVIEW_LABEL,
) -> dict[str, Any]:
    paths = [change["path"] for change in changes]
    logic_files = sorted(
        {
            path
            for path in paths
            if _matches_prefix_or_file(path, LOGIC_PREFIXES, LOGIC_FILES)
        }
    )
    baseline_files = sorted(
        {
            path
            for path in paths
            if _matches_prefix_or_file(path, BASELINE_PREFIXES, BASELINE_FILES)
        }
    )
    governance_files = sorted({path for path in paths if path in GOVERNANCE_FILES})
    protected_files = sorted(set(logic_files + baseline_files + governance_files))
    protected_change = bool(protected_files)

    live_paths = {change["path"] for change in changes if not _is_deleted(change)}
    changed_tests = sorted(path for path in live_paths if path.startswith("tests/test_") and path.endswith(".py"))
    focused_tests = sorted(path for path in changed_tests if _is_focused_test(path))
    guard_tests = sorted(path for path in changed_tests if path in GUARD_TEST_FILES)
    guard_implementation_files = sorted(path for path in paths if path in GUARD_IMPLEMENTATION_FILES)

    label_values = sorted({str(label).strip() for label in labels if str(label).strip()})
    label_present = review_label.casefold() in {label.casefold() for label in label_values}
    fields = extract_body_fields(pr_body) if protected_change else {field: None for field in BODY_FIELDS}
    field_status = {
        field: {
            "present": fields[field] is not None,
            "completed": fields[field] is not None and not _is_placeholder(fields[field]),
        }
        for field in BODY_FIELDS
    }

    failures: list[dict[str, Any]] = []
    if logic_files or baseline_files:
        if not focused_tests:
            failures.append(
                {
                    "code": "missing-focused-regression-test",
                    "detail": "Protected parsing/preprocessing, MCP connection, or baseline changes require a changed focused unittest module.",
                }
            )
    if guard_implementation_files and not guard_tests:
        failures.append(
            {
                "code": "missing-guard-regression-test",
                "detail": "Guard implementation changes require a changed guard/workflow test module.",
            }
        )
    if protected_change:
        for field, status in field_status.items():
            if not status["present"]:
                failures.append(
                    {
                        "code": "missing-pr-body-field",
                        "field": field,
                        "detail": f"PR body field '{field}' is required for protected changes.",
                    }
                )
            elif not status["completed"]:
                failures.append(
                    {
                        "code": "incomplete-pr-body-field",
                        "field": field,
                        "detail": f"PR body field '{field}' still contains a placeholder.",
                    }
                )
        if baseline_files:
            baseline_value = _normalized_value(fields.get("baseline-change"))
            if baseline_value in NO_BASELINE_CHANGE_VALUES:
                failures.append(
                    {
                        "code": "baseline-change-unjustified",
                        "field": "baseline-change",
                        "detail": "Regression baseline changes require a reason and before/after evidence; 'none' is not valid.",
                    }
                )
        if not label_present:
            failures.append(
                {
                    "code": "missing-review-label",
                    "detail": f"A code owner must add the '{review_label}' label after reviewing the evidence.",
                }
            )

    return {
        "report_type": "preprocessing_change_guard",
        "passed": not failures,
        "protected_change": protected_change,
        "review_label": review_label,
        "review_label_present": label_present,
        "changed_files": changes,
        "protected_files": protected_files,
        "logic_files": logic_files,
        "baseline_files": baseline_files,
        "governance_files": governance_files,
        "guard_implementation_files": guard_implementation_files,
        "changed_tests": changed_tests,
        "focused_tests": focused_tests,
        "guard_tests": guard_tests,
        "body_field_status": field_status,
        "failure_count": len(failures),
        "failures": failures,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail closed when protected parsing/preprocessing or MCP connection changes lack PR evidence and review."
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--changed-files-file", type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument("--head-ref")
    parser.add_argument("--github-event", type=Path)
    parser.add_argument("--pr-body-file", type=Path)
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--review-label", default=REVIEW_LABEL)
    parser.add_argument("--out-json", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    event_path = args.github_event
    if event_path is None and os.environ.get("GITHUB_EVENT_PATH"):
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    try:
        context = event_pull_request_context(load_event(event_path))
        if args.changed_files_file:
            changes = parse_changed_lines(args.changed_files_file.read_text(encoding="utf-8-sig").splitlines())
        else:
            base_ref = str(args.base_ref or context["base_ref"] or "")
            head_ref = str(args.head_ref or context["head_ref"] or "HEAD")
            if not base_ref:
                raise GuardInputError("Provide --changed-files-file or --base-ref.")
            changes = git_changed_files(args.project_root.resolve(), base_ref, head_ref)

        if args.pr_body_file:
            pr_body = args.pr_body_file.read_text(encoding="utf-8-sig")
        else:
            pr_body = str(context["body"] or "")
        labels = [*context["labels"], *args.label]
        report = evaluate_guard(changes, pr_body=pr_body, labels=labels, review_label=args.review_label)
    except (GuardInputError, OSError) as exc:
        report = {
            "report_type": "preprocessing_change_guard",
            "passed": False,
            "failure_count": 1,
            "failures": [{"code": "guard-input-error", "detail": str(exc)}],
        }

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
