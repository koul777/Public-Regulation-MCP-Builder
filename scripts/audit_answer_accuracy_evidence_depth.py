"""Audit answer-accuracy evidence depth for publish decisions.

This complements the product readiness gate. A small passing answer eval can be
valid smoke evidence while still being too thin for public-release accuracy
claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact(role: str, path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"role": role, "path": None, "exists": False}
    return {
        "role": role,
        "path": str(path),
        "exists": path.exists(),
        "byte_count": path.stat().st_size if path.exists() and path.is_file() else None,
        "sha256": _sha256(path),
    }


def _summary_from_product(product: dict[str, Any], key: str) -> dict[str, Any]:
    value = product.get(key)
    return value if isinstance(value, dict) else {}


def _demo_answer_summary_from_report(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    items = [item for item in _list(report.get("items")) if isinstance(item, dict)]
    return {
        "passed": bool(report.get("passed")),
        "query_count": _int(report.get("query_count")) or len(items),
        "answerable_query_count": sum(1 for item in items if not item.get("expect_no_evidence")),
        "expect_no_evidence_query_count": sum(1 for item in items if item.get("expect_no_evidence")),
        "smoke_citation_count": sum(_int(item.get("smoke_citation_count")) for item in items),
        "missing_supporting_result_count": sum(
            1
            for item in items
            if not item.get("expect_no_evidence")
            and (_int(item.get("supporting_result_count")) <= 0 or not item.get("citations"))
        ),
        "quality_issue_count": _int(report.get("quality_issue_count")),
    }


def _accuracy_comparison_summary_from_report(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    summary = _dict(report.get("summary"))
    return {
        "passed": bool(report.get("passed")),
        "query_count": _int(report.get("query_count")),
        "mcp_regression_count": _int(summary.get("mcp_regression_count")),
        "mcp_avg_quality_score": _float(summary.get("mcp_avg_quality_score")),
        "baseline_avg_quality_score": _float(summary.get("baseline_avg_quality_score")),
    }


def _finding(severity: str, code: str, detail: str, **evidence: Any) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "detail": detail,
        "evidence": evidence,
    }


def build_answer_accuracy_evidence_depth(
    *,
    product_readiness_report: Path | None = None,
    rag_eval_report: Path | None = None,
    mcp_demo_answer_report: Path | None = None,
    accuracy_comparison_report: Path | None = None,
    min_public_query_count: int = 20,
    min_no_evidence_controls: int = 3,
    min_relation_supported_ratio: float = 0.5,
) -> dict[str, Any]:
    product = _load_json(product_readiness_report)
    rag_eval = _load_json(rag_eval_report)
    demo = _load_json(mcp_demo_answer_report)
    comparison = _load_json(accuracy_comparison_report)

    rag_summary = rag_eval or _summary_from_product(product, "rag_eval_summary")
    demo_summary = _demo_answer_summary_from_report(demo) or _summary_from_product(
        product, "mcp_demo_answer_summary"
    )
    comparison_summary = _accuracy_comparison_summary_from_report(
        comparison
    ) or _summary_from_product(product, "accuracy_comparison_summary")

    rag_query_count = _int(rag_summary.get("query_count"))
    demo_query_count = _int(demo_summary.get("query_count"))
    comparison_query_count = _int(comparison_summary.get("query_count"))
    total_distinct_evidence_queries = max(rag_query_count, demo_query_count, comparison_query_count)
    no_evidence_controls = max(
        _int(rag_summary.get("expect_no_evidence_query_count")),
        _int(demo_summary.get("expect_no_evidence_query_count")),
    )
    relation_supported_ratio = _float(rag_summary.get("relation_supported_ratio"))

    findings: list[dict[str, Any]] = []
    if total_distinct_evidence_queries < min_public_query_count:
        findings.append(
            _finding(
                "warning",
                "answer-evidence-query-count-thin",
                "Answer-accuracy evidence has too few distinct queries for a public-release-grade claim.",
                observed_query_count=total_distinct_evidence_queries,
                required_query_count=min_public_query_count,
            )
        )
    if no_evidence_controls < min_no_evidence_controls:
        findings.append(
            _finding(
                "warning",
                "answer-evidence-no-evidence-controls-thin",
                "No-evidence control queries are too sparse for a public-release-grade claim.",
                observed_control_count=no_evidence_controls,
                required_control_count=min_no_evidence_controls,
            )
        )
    if relation_supported_ratio < min_relation_supported_ratio:
        findings.append(
            _finding(
                "warning",
                "answer-evidence-relation-support-thin",
                "Retrieval relation-support evidence is below the public-release-depth threshold.",
                observed_ratio=relation_supported_ratio,
                required_ratio=min_relation_supported_ratio,
            )
        )
    if _int(demo_summary.get("quality_issue_count")) > 0:
        findings.append(
            _finding(
                "warning",
                "answer-evidence-demo-quality-issues",
                "Demo answer evaluation still contains quality issues that should be resolved before public-release-grade claims.",
                quality_issue_count=_int(demo_summary.get("quality_issue_count")),
            )
        )
    if _int(demo_summary.get("smoke_citation_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "answer-evidence-smoke-citations",
                "Demo answers cite smoke-test documents.",
                smoke_citation_count=_int(demo_summary.get("smoke_citation_count")),
            )
        )
    if _int(demo_summary.get("missing_supporting_result_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "answer-evidence-supporting-citations-missing",
                "Demo answerable queries are missing supporting citations.",
                missing_supporting_result_count=_int(
                    demo_summary.get("missing_supporting_result_count")
                ),
            )
        )
    if _int(comparison_summary.get("mcp_regression_count")) > 0:
        findings.append(
            _finding(
                "blocker",
                "answer-evidence-mcp-regression",
                "MCP answer comparison contains regressions.",
                mcp_regression_count=_int(comparison_summary.get("mcp_regression_count")),
            )
        )

    blocker_count = sum(1 for finding in findings if finding["severity"] == "blocker")
    warning_count = sum(1 for finding in findings if finding["severity"] == "warning")
    return {
        "report_type": "answer_accuracy_evidence_depth",
        "generated_at": _utc_now(),
        "passed_for_public_release_depth": blocker_count == 0 and warning_count == 0,
        "pilot_evidence_status": "usable_with_disclosure" if blocker_count == 0 else "blocked",
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "findings": findings,
        "thresholds": {
            "min_public_query_count": min_public_query_count,
            "min_no_evidence_controls": min_no_evidence_controls,
            "min_relation_supported_ratio": min_relation_supported_ratio,
        },
        "evidence_counts": {
            "rag_query_count": rag_query_count,
            "mcp_demo_query_count": demo_query_count,
            "accuracy_comparison_query_count": comparison_query_count,
            "distinct_evidence_query_count": total_distinct_evidence_queries,
            "no_evidence_control_count": no_evidence_controls,
            "relation_supported_ratio": relation_supported_ratio,
            "demo_smoke_citation_count": _int(demo_summary.get("smoke_citation_count")),
            "demo_missing_supporting_result_count": _int(
                demo_summary.get("missing_supporting_result_count")
            ),
            "demo_quality_issue_count": _int(demo_summary.get("quality_issue_count")),
            "mcp_regression_count": _int(comparison_summary.get("mcp_regression_count")),
        },
        "source_summaries": {
            "rag_eval_summary": rag_summary,
            "mcp_demo_answer_summary": demo_summary,
            "accuracy_comparison_summary": comparison_summary,
        },
        "source_report_artifacts": [
            _artifact("product_readiness_report", product_readiness_report),
            _artifact("rag_eval_report", rag_eval_report),
            _artifact("mcp_demo_answer_report", mcp_demo_answer_report),
            _artifact("accuracy_comparison_report", accuracy_comparison_report),
        ],
        "next_actions": [
            "Expand answer eval to at least the configured public query threshold.",
            "Add no-evidence control queries and assert that no supporting citations are returned.",
            "Include relation-support and expected citation coverage across articles, appendices, forms, and tables.",
            "Rerun product readiness after refreshing answer evidence.",
        ],
        "safety_note": (
            "This report is read-only. It does not change retrieval results, approve chunks, "
            "or write Vector DB records."
        ),
        "api_call_count": 0,
    }


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    counts = _dict(report.get("evidence_counts"))
    lines = [
        "# Answer Accuracy Evidence Depth",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Public-release-depth passed: `{str(report.get('passed_for_public_release_depth')).lower()}`",
        f"- Pilot evidence status: `{report.get('pilot_evidence_status')}`",
        f"- Blockers / warnings: {report.get('blocker_count')} / {report.get('warning_count')}",
        "",
        "## Evidence Counts",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| RAG eval queries | {_md_cell(counts.get('rag_query_count'))} |",
        f"| MCP demo queries | {_md_cell(counts.get('mcp_demo_query_count'))} |",
        f"| Accuracy comparison queries | {_md_cell(counts.get('accuracy_comparison_query_count'))} |",
        f"| Distinct evidence query count | {_md_cell(counts.get('distinct_evidence_query_count'))} |",
        f"| No-evidence controls | {_md_cell(counts.get('no_evidence_control_count'))} |",
        f"| Relation-supported ratio | {_md_cell(counts.get('relation_supported_ratio'))} |",
        f"| Demo smoke citations | {_md_cell(counts.get('demo_smoke_citation_count'))} |",
        f"| Missing supporting results | {_md_cell(counts.get('demo_missing_supporting_result_count'))} |",
        f"| MCP regressions | {_md_cell(counts.get('mcp_regression_count'))} |",
        "",
        "## Findings",
        "",
    ]
    findings = [_dict(item) for item in _list(report.get("findings"))]
    if not findings:
        lines.append("- None.")
    for finding in findings:
        lines.append(
            f"- {finding.get('severity')} `{finding.get('code')}`: {finding.get('detail')}"
        )
    lines.extend(["", "## Next Actions", ""])
    for action in _list(report.get("next_actions")):
        lines.append(f"- {action}")
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-readiness-report", type=Path)
    parser.add_argument("--rag-eval-report", type=Path)
    parser.add_argument("--mcp-demo-answer-report", type=Path)
    parser.add_argument("--accuracy-comparison-report", type=Path)
    parser.add_argument("--min-public-query-count", type=int, default=20)
    parser.add_argument("--min-no-evidence-controls", type=int, default=3)
    parser.add_argument("--min-relation-supported-ratio", type=float, default=0.5)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_answer_accuracy_evidence_depth(
        product_readiness_report=args.product_readiness_report,
        rag_eval_report=args.rag_eval_report,
        mcp_demo_answer_report=args.mcp_demo_answer_report,
        accuracy_comparison_report=args.accuracy_comparison_report,
        min_public_query_count=args.min_public_query_count,
        min_no_evidence_controls=args.min_no_evidence_controls,
        min_relation_supported_ratio=args.min_relation_supported_ratio,
    )
    _write_json(args.out_json, report)
    _write_text(args.out_md, render_markdown(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
