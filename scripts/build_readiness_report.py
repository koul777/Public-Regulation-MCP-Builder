from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.embedding_readiness import evaluate_embedding_readiness


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_readiness_report(
    *,
    public_portal_force: dict[str, Any],
    public_portal_reuse: dict[str, Any],
    integrated_force: dict[str, Any],
    integrated_reuse: dict[str, Any],
    cost_estimate: dict[str, Any],
    snapshot_comparison: dict[str, Any],
    embedding_cost_estimates: list[dict[str, Any]] | None = None,
    require_semantic_embedding_approval: bool = False,
    embedding_approval_reference: str | None = None,
) -> dict[str, Any]:
    embedding_readiness = evaluate_embedding_readiness(
        embedding_cost_estimates,
        require_semantic_provider_approval=require_semantic_embedding_approval,
        approval_reference=embedding_approval_reference,
    )
    checks = [
        check("public_portal_all_completed", int(public_portal_force.get("completed_count") or 0) == int(public_portal_force.get("input_count") or 0)),
        check("public_portal_all_quality_passed", int(public_portal_force.get("quality_passed_count") or 0) == int(public_portal_force.get("input_count") or 0)),
        check("public_portal_no_failed_info", int(public_portal_force.get("failed_info_check_total") or 0) == 0),
        check("public_portal_no_recommendations", int(public_portal_force.get("recommendation_total") or 0) == 0),
        check("public_portal_no_table_attention", int(public_portal_force.get("table_false_positive_attention_total") or 0) == 0),
        check("public_portal_reuse_all_skipped", int(public_portal_reuse.get("skipped_unchanged_count") or 0) == int(public_portal_reuse.get("input_count") or 0)),
        check("integrated_all_completed", int(integrated_force.get("completed_count") or 0) == int(integrated_force.get("input_count") or 0)),
        check("integrated_quality_passed", int(integrated_force.get("quality_passed_count") or 0) == int(integrated_force.get("input_count") or 0)),
        check("integrated_no_failed_info", int(integrated_force.get("failed_info_check_total") or 0) == 0),
        check("integrated_no_recommendations", int(integrated_force.get("recommendation_total") or 0) == 0),
        check("integrated_no_table_attention", int(integrated_force.get("table_false_positive_attention_total") or 0) == 0),
        check("integrated_reuse_all_skipped", int(integrated_reuse.get("skipped_unchanged_count") or 0) == int(integrated_reuse.get("input_count") or 0)),
        check("current_ai_tokens_zero", current_ai_tokens(public_portal_reuse) == 0 and current_ai_tokens(integrated_reuse) == 0),
        check("cost_budget_known_and_not_exceeded", cost_budget_known_and_not_exceeded(cost_estimate)),
        check("public_portal_live_no_drift", snapshot_no_drift(snapshot_comparison)),
    ]
    checks.extend(embedding_readiness["checks"])
    passed = all(item["passed"] for item in checks)
    summary = {
        "public_portal_inputs": int(public_portal_force.get("input_count") or 0),
        "public_portal_quality_passed": int(public_portal_force.get("quality_passed_count") or 0),
        "public_portal_average_quality_score": public_portal_force.get("average_quality_score"),
        "public_portal_stable_table_false_positives": int(public_portal_force.get("stable_table_false_positive_total") or 0),
        "integrated_inputs": int(integrated_force.get("input_count") or 0),
        "integrated_quality_passed": int(integrated_force.get("quality_passed_count") or 0),
        "integrated_average_quality_score": integrated_force.get("average_quality_score"),
        "integrated_stable_table_false_positives": int(integrated_force.get("stable_table_false_positive_total") or 0),
        "integrated_failed_info_checks": int(integrated_force.get("failed_info_check_total") or 0),
        "integrated_recommendations": int(integrated_force.get("recommendation_total") or 0),
        "current_ai_estimated_total_tokens": current_ai_tokens(public_portal_reuse) + current_ai_tokens(integrated_reuse),
        "estimated_total_cost": cost_estimate.get("estimated_total_cost"),
        "cost_budget_evaluation_status": cost_estimate.get("budget_evaluation_status", ""),
        "provider_execution_path": "not_implemented",
        "public_portal_live_added": int((snapshot_comparison.get("counts") or {}).get("added") or 0),
        "public_portal_live_removed": int((snapshot_comparison.get("counts") or {}).get("removed") or 0),
        "public_portal_live_metadata_changed": int((snapshot_comparison.get("counts") or {}).get("metadata_changed") or 0),
        "public_portal_live_file_hash_changed": int((snapshot_comparison.get("counts") or {}).get("file_hash_changed") or 0),
        "public_portal_live_file_hash_coverage_before": int((snapshot_comparison.get("before") or {}).get("file_sha256_coverage_count") or 0),
        "public_portal_live_file_hash_coverage_after": int((snapshot_comparison.get("after") or {}).get("file_sha256_coverage_count") or 0),
        "public_portal_live_content_hash_checked": content_hash_checked(snapshot_comparison),
        **embedding_readiness["summary"],
    }
    guardrails = [
        "AI provider execution remains absent; this report is data-pipeline readiness, not approval to enable billing.",
        "Future calls must pass actor/approval/model/price/cost preflight reservation with an approved prompt hash and prompt token envelope.",
        "Future allowed preflight reservations must be appended to provider_budget_reservations.jsonl before any network call.",
        "Future provider payloads must use minimal normalized text without source metadata by default and must match the approved payload hash and token envelope.",
        "Future provider calls must append locked provider_execution_audit.jsonl records with payload hash and token/cost overrun validation.",
        "Non-local semantic embedding providers must provide embedding cost estimates, prices, budgets, approval references, and audit evidence before replacing local-hash-embedding-v1.",
        "Live PUBLIC_PORTAL collection should remain a smoke/nightly input, while fixed snapshots stay as CI regression fixtures.",
    ]
    if not summary["public_portal_live_content_hash_checked"]:
        guardrails.append("A metadata-only live smoke with zero file-hash coverage does not prove attachment content is unchanged.")
    return {
        "status": "data_pipeline_ready_provider_not_wired" if passed else "needs_attention",
        "passed": passed,
        "checks": checks,
        "summary": summary,
        "guardrails": guardrails,
        "failures": {
            "embedding_readiness": embedding_readiness["failures"],
        },
    }


def check(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed)}


def current_ai_tokens(report: dict[str, Any]) -> int:
    return int(report.get("agent_review_estimated_total_tokens_total") or 0)


def cost_budget_known_and_not_exceeded(estimate: dict[str, Any]) -> bool:
    if estimate.get("budget_exceeded") is not False:
        return False
    estimated_tokens = int(estimate.get("agent_review_estimated_total_tokens") or 0)
    if estimated_tokens > 0 and estimate.get("estimated_total_cost") in (None, ""):
        return False
    return True


def snapshot_no_drift(report: dict[str, Any]) -> bool:
    counts = report.get("counts") or {}
    return all(int(counts.get(key) or 0) == 0 for key in ("added", "removed", "metadata_changed", "file_hash_changed"))


def content_hash_checked(report: dict[str, Any]) -> bool:
    after = report.get("after") or {}
    return int(after.get("row_count") or 0) > 0 and int(after.get("file_sha256_coverage_count") or 0) == int(
        after.get("row_count") or 0
    )


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Public Institution Preprocessor Readiness",
        "",
        f"- Status: {report['status']}",
        f"- Passed: {report['passed']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in report["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Checks", ""])
    for item in report["checks"]:
        status = "PASS" if item["passed"] else "FAIL"
        lines.append(f"- {status}: {item['name']}")
    lines.extend(["", "## Guardrails", ""])
    for item in report["guardrails"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a consolidated public-institution readiness report.")
    parser.add_argument("--public_portal-force", required=True)
    parser.add_argument("--public_portal-reuse", required=True)
    parser.add_argument("--integrated-force", required=True)
    parser.add_argument("--integrated-reuse", required=True)
    parser.add_argument("--cost-estimate", required=True)
    parser.add_argument("--snapshot-comparison", required=True)
    parser.add_argument("--embedding-cost-estimate", action="append", default=[])
    parser.add_argument("--require-semantic-embedding-approval", action="store_true")
    parser.add_argument("--embedding-approval-reference", default=None)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_readiness_report(
        public_portal_force=load_json(Path(args.public_portal_force)),
        public_portal_reuse=load_json(Path(args.public_portal_reuse)),
        integrated_force=load_json(Path(args.integrated_force)),
        integrated_reuse=load_json(Path(args.integrated_reuse)),
        cost_estimate=load_json(Path(args.cost_estimate)),
        snapshot_comparison=load_json(Path(args.snapshot_comparison)),
        embedding_cost_estimates=[load_json(Path(path)) for path in args.embedding_cost_estimate],
        require_semantic_embedding_approval=args.require_semantic_embedding_approval,
        embedding_approval_reference=args.embedding_approval_reference,
    )
    Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.out_md).write_text(to_markdown(report), encoding="utf-8")
    print(json.dumps({"json": args.out_json, "markdown": args.out_md, "status": report["status"]}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
