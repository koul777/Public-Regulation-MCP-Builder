from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


HASH_CHUNK_BYTES = 1024 * 1024
ROLE_REPORT_TYPES = {
    "accuracy_comparison": "simple_rag_vs_mcp_accuracy",
    "query_benchmark": "mcp_query_benchmark",
    "demo_answers": "mcp_demo_answers",
    "rag_eval": "rag_retrieval_eval",
    "product_readiness": "mcp_product_readiness",
}
QUERY_SPEC_REQUIRED_ROLES = {"accuracy_comparison", "query_benchmark", "demo_answers"}
QUERY_COUNT_REQUIRED_ROLES = {"accuracy_comparison", "query_benchmark", "demo_answers", "rag_eval"}


def build_mcp_answer_evidence_bundle(
    *,
    accuracy_comparison_report: Path | None = None,
    query_benchmark_report: Path | None = None,
    demo_answer_report: Path | None = None,
    rag_eval_report: Path | None = None,
    product_readiness_report: Path | None = None,
    require_shared_query_spec: bool = False,
    min_query_count: int = 0,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    role_paths = {
        "accuracy_comparison": accuracy_comparison_report,
        "query_benchmark": query_benchmark_report,
        "demo_answers": demo_answer_report,
        "rag_eval": rag_eval_report,
        "product_readiness": product_readiness_report,
    }
    artifacts = {
        role: _artifact_summary(role, path)
        for role, path in role_paths.items()
        if path is not None
    }
    findings = _artifact_findings(artifacts)
    findings.extend(_query_spec_findings(artifacts, require_shared_query_spec=require_shared_query_spec))
    findings.extend(_minimum_query_count_findings(artifacts, min_query_count=min_query_count))
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    report = {
        "report_type": "mcp_answer_evidence_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": blocker_count == 0,
        "bundle_ready": blocker_count == 0 and warning_count == 0,
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "finding_count": len(findings),
        "findings": findings,
        "required_shared_query_spec": require_shared_query_spec,
        "min_query_count": max(0, int(min_query_count or 0)),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "query_spec_summary": _query_spec_summary(artifacts),
        "query_count_summary": _query_count_summary(artifacts),
        "answer_accuracy_summary": _answer_accuracy_summary(artifacts),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _artifact_summary(role: str, path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "role": role,
        "path": str(path),
        "expected_report_type": ROLE_REPORT_TYPES[role],
        "exists": path.is_file(),
        "byte_count": None,
        "sha256": None,
        "parse_error": "",
        "report_type": "",
        "generated_at": "",
        "passed": None,
        "status": "",
        "query_count": None,
        "query_spec_path": "",
        "query_spec_sha256": "",
        "query_spec_byte_count": None,
        "query_spec_item_count": None,
        "quality_issue_count": None,
        "finding_count": None,
        "mcp_regression_count": None,
        "answerable_ratio": None,
        "answer_accuracy_gate": {},
    }
    if not path.is_file():
        return summary
    summary["byte_count"] = path.stat().st_size
    summary["sha256"] = _sha256_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        summary["parse_error"] = str(exc)
        return summary
    if not isinstance(payload, dict):
        summary["parse_error"] = "json_root_not_object"
        return summary
    summary.update(
        {
            "report_type": str(payload.get("report_type") or ""),
            "generated_at": str(payload.get("generated_at") or ""),
            "passed": payload.get("passed") if isinstance(payload.get("passed"), bool) else None,
            "status": str(payload.get("status") or ""),
            "query_count": _optional_int(payload.get("query_count")),
            "query_spec_path": str(payload.get("query_spec_path") or ""),
            "query_spec_sha256": str(payload.get("query_spec_sha256") or ""),
            "query_spec_byte_count": _optional_int(payload.get("query_spec_byte_count")),
            "query_spec_item_count": _optional_int(payload.get("query_spec_item_count")),
            "quality_issue_count": _optional_int(payload.get("quality_issue_count")),
            "finding_count": _optional_int(payload.get("finding_count")),
            "answerable_ratio": _optional_float(payload.get("answerable_ratio")),
        }
    )
    summary["mcp_regression_count"] = _mcp_regression_count(payload)
    if role == "product_readiness":
        summary["answer_accuracy_gate"] = _product_answer_accuracy_gate(payload)
    return summary


def _artifact_findings(artifacts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for role, artifact in artifacts.items():
        if not artifact["exists"]:
            findings.append(_finding("blocker", "artifact-missing", role, "Configured evidence artifact does not exist."))
            continue
        if artifact.get("parse_error"):
            findings.append(_finding("blocker", "artifact-invalid-json", role, str(artifact["parse_error"])))
            continue
        expected = artifact.get("expected_report_type")
        if artifact.get("report_type") != expected:
            findings.append(
                _finding(
                    "blocker",
                    "artifact-report-type-mismatch",
                    role,
                    f"Expected {expected}, got {artifact.get('report_type') or 'missing'}.",
                )
            )
        if artifact.get("passed") is False:
            if role == "product_readiness" and _product_answer_accuracy_gate_ready(artifact):
                pass
            else:
                severity = "blocker" if role in {"accuracy_comparison", "demo_answers", "product_readiness"} else "warning"
                findings.append(_finding(severity, "artifact-not-passed", role, "Evidence report did not pass."))
        if role == "accuracy_comparison" and int(artifact.get("mcp_regression_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-accuracy-regression", role, "MCP accuracy comparison reports regressions."))
        if role == "demo_answers" and int(artifact.get("quality_issue_count") or 0) > 0:
            findings.append(_finding("blocker", "mcp-demo-answer-quality-issues", role, "Demo answer report contains quality issues."))
        if role == "query_benchmark" and int(artifact.get("finding_count") or 0) > 0:
            findings.append(_finding("warning", "mcp-query-benchmark-findings", role, "Benchmark report contains threshold or result findings."))
        if role == "rag_eval" and artifact.get("answerable_ratio") == 0.0:
            findings.append(_finding("warning", "rag-eval-zero-answerable", role, "RAG retrieval eval has zero answerable queries."))
        if role == "product_readiness":
            gate = artifact.get("answer_accuracy_gate") or {}
            if gate.get("status") in {"blocked", "failed"} or int(gate.get("blocker_count") or 0) > 0:
                findings.append(_finding("blocker", "product-answer-accuracy-gate-blocked", role, "Product readiness answer accuracy gate is blocked."))
    return findings


def _query_spec_findings(
    artifacts: dict[str, dict[str, Any]],
    *,
    require_shared_query_spec: bool,
) -> list[dict[str, Any]]:
    if not require_shared_query_spec:
        return []
    findings: list[dict[str, Any]] = []
    fingerprint_by_role: dict[str, str] = {}
    missing_roles: list[str] = []
    for role, artifact in artifacts.items():
        if not _requires_query_spec(role, artifact):
            continue
        fingerprint = str(artifact.get("query_spec_sha256") or "")
        if fingerprint:
            fingerprint_by_role[role] = fingerprint
        else:
            missing_roles.append(role)
    if missing_roles:
        findings.append(
            {
                "severity": "warning",
                "code": "query-spec-fingerprint-missing",
                "role": "bundle",
                "detail": "Shared query-spec enforcement requires query-spec SHA metadata on query evidence reports.",
                "roles": sorted(missing_roles),
            }
        )
    fingerprints = set(fingerprint_by_role.values())
    if len(fingerprints) > 1:
        findings.append(
            {
                "severity": "warning",
                "code": "query-spec-fingerprint-mismatch",
                "role": "bundle",
                "detail": "Evidence reports use more than one query-spec SHA.",
                "query_spec_sha256_values": sorted(fingerprints),
            }
        )
    return findings


def _minimum_query_count_findings(
    artifacts: dict[str, dict[str, Any]],
    *,
    min_query_count: int,
) -> list[dict[str, Any]]:
    minimum = max(0, int(min_query_count or 0))
    if minimum <= 0:
        return []
    findings: list[dict[str, Any]] = []
    for role, artifact in artifacts.items():
        if not _requires_query_count(role):
            continue
        query_count = _optional_int(artifact.get("query_count"))
        if query_count is None:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "query-count-missing",
                    "role": role,
                    "detail": f"Evidence report must include query_count when minimum {minimum} is required.",
                    "min_query_count": minimum,
                }
            )
            continue
        if query_count < minimum:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "query-count-below-minimum",
                    "role": role,
                    "detail": f"Evidence report has {query_count} queries, below required minimum {minimum}.",
                    "query_count": query_count,
                    "min_query_count": minimum,
                }
            )
    return findings


def _requires_query_spec(role: str, artifact: dict[str, Any]) -> bool:
    if role in QUERY_SPEC_REQUIRED_ROLES:
        return True
    return _optional_int(artifact.get("query_count")) is not None


def _requires_query_count(role: str) -> bool:
    return role in QUERY_COUNT_REQUIRED_ROLES


def _query_spec_summary(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    roles = {
        role: {
            "query_spec_path": artifact.get("query_spec_path") or "",
            "query_spec_sha256": artifact.get("query_spec_sha256") or "",
            "query_spec_item_count": artifact.get("query_spec_item_count"),
        }
        for role, artifact in artifacts.items()
        if artifact.get("query_spec_path") or artifact.get("query_spec_sha256")
    }
    unique_sha = sorted({str(item["query_spec_sha256"]) for item in roles.values() if item["query_spec_sha256"]})
    missing_roles = sorted(
        role
        for role, artifact in artifacts.items()
        if _requires_query_spec(role, artifact) and not artifact.get("query_spec_sha256")
    )
    return {
        "report_count": len(roles),
        "unique_query_spec_sha256_count": len(unique_sha),
        "unique_query_spec_sha256": unique_sha,
        "missing_query_spec_roles": missing_roles,
        "roles": roles,
    }


def _query_count_summary(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    roles = {
        role: _optional_int(artifact.get("query_count"))
        for role, artifact in artifacts.items()
        if _optional_int(artifact.get("query_count")) is not None
    }
    values = [int(value) for value in roles.values() if value is not None]
    return {
        "report_count": len(roles),
        "min_query_count": min(values) if values else None,
        "max_query_count": max(values) if values else None,
        "roles": roles,
    }


def _answer_accuracy_summary(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    accuracy = artifacts.get("accuracy_comparison") or {}
    benchmark = artifacts.get("query_benchmark") or {}
    demo = artifacts.get("demo_answers") or {}
    rag_eval = artifacts.get("rag_eval") or {}
    product = artifacts.get("product_readiness") or {}
    return {
        "accuracy_passed": accuracy.get("passed"),
        "mcp_regression_count": _optional_int(accuracy.get("mcp_regression_count")),
        "benchmark_passed": benchmark.get("passed"),
        "benchmark_finding_count": _optional_int(benchmark.get("finding_count")),
        "demo_answers_passed": demo.get("passed"),
        "demo_quality_issue_count": _optional_int(demo.get("quality_issue_count")),
        "rag_eval_answerable_ratio": _optional_float(rag_eval.get("answerable_ratio")),
        "product_answer_accuracy_gate": product.get("answer_accuracy_gate") or {},
    }


def _mcp_regression_count(payload: dict[str, Any]) -> int | None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    value = payload.get("mcp_regression_count")
    if value is None:
        value = summary.get("mcp_regression_count")
    return _optional_int(value)


def _product_answer_accuracy_gate(payload: dict[str, Any]) -> dict[str, Any]:
    gates = payload.get("gates") if isinstance(payload.get("gates"), dict) else {}
    gate = gates.get("answer_accuracy") or gates.get("answer_accuracy_gate")
    if not isinstance(gate, dict):
        return {}
    return {
        "status": str(gate.get("status") or ""),
        "blocker_count": _optional_int(gate.get("blocker_count")) or 0,
        "warning_count": _optional_int(gate.get("warning_count")) or 0,
    }


def _product_answer_accuracy_gate_ready(artifact: dict[str, Any]) -> bool:
    gate = artifact.get("answer_accuracy_gate") if isinstance(artifact.get("answer_accuracy_gate"), dict) else {}
    status = str(gate.get("status") or "").strip().lower()
    return status in {"ready", "passed"} and int(gate.get("blocker_count") or 0) == 0


def _finding(severity: str, code: str, role: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "role": role, "detail": detail}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("answer_accuracy_summary") or {}
    lines = [
        "# MCP Answer Evidence Bundle",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Bundle ready: `{str(report.get('bundle_ready')).lower()}`",
        f"- Artifacts: {report.get('artifact_count')}",
        f"- Blockers / warnings: {report.get('blocking_count')} / {report.get('warning_count')}",
        f"- Accuracy regressions: {summary.get('mcp_regression_count')}",
        f"- Demo quality issues: {summary.get('demo_quality_issue_count')}",
        f"- Benchmark findings: {summary.get('benchmark_finding_count')}",
        f"- RAG answerable ratio: {summary.get('rag_eval_answerable_ratio')}",
        f"- Minimum query-count requirement: {report.get('min_query_count')}",
        "",
        "## Artifacts",
        "",
        "| Role | Report type | Passed | Query count | Query spec SHA | Exists |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for role, artifact in (report.get("artifacts") or {}).items():
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(role),
                    _md_cell(artifact.get("report_type")),
                    _md_cell(artifact.get("passed")),
                    _md_cell(artifact.get("query_count")),
                    _md_cell(_short(artifact.get("query_spec_sha256"))),
                    _md_cell(artifact.get("exists")),
                ]
            )
            + " |"
        )
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for finding in report["findings"]:
            lines.append(f"- `{finding.get('severity')}` `{finding.get('code')}` `{finding.get('role')}`: {finding.get('detail')}")
    return "\n".join(lines).rstrip() + "\n"


def _short(value: Any, length: int = 12) -> str:
    text = str(value or "")
    return text[:length] if text else ""


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bundle MCP answer accuracy, benchmark, demo answer, and readiness evidence.")
    parser.add_argument("--accuracy-comparison-report", type=Path)
    parser.add_argument("--query-benchmark-report", type=Path)
    parser.add_argument("--demo-answer-report", type=Path)
    parser.add_argument("--rag-eval-report", type=Path)
    parser.add_argument("--product-readiness-report", type=Path)
    parser.add_argument("--require-shared-query-spec", action="store_true")
    parser.add_argument(
        "--min-query-count",
        type=int,
        default=0,
        help="Require every query-bearing evidence artifact to have at least this many queries.",
    )
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_mcp_answer_evidence_bundle(
        accuracy_comparison_report=args.accuracy_comparison_report,
        query_benchmark_report=args.query_benchmark_report,
        demo_answer_report=args.demo_answer_report,
        rag_eval_report=args.rag_eval_report,
        product_readiness_report=args.product_readiness_report,
        require_shared_query_spec=args.require_shared_query_spec,
        min_query_count=args.min_query_count,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
