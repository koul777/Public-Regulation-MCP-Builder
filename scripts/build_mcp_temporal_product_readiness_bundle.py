from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_mcp_product_readiness import build_mcp_product_readiness_audit
from scripts.audit_runtime_version_drift import build_runtime_version_drift_report
from scripts.audit_temporal_metadata_coverage import build_temporal_metadata_coverage_report
from scripts.build_temporal_ambiguity_review_scope import build_temporal_ambiguity_review_scope
from scripts.build_temporal_backfill_shadow_runtime import build_temporal_backfill_shadow_runtime
from scripts.report_metadata import current_repo_commit


def build_mcp_temporal_product_readiness_bundle(
    *,
    runtime_data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    reports_dir: Path = Path("reports"),
    shadow_data_dir: Path | None = None,
    timestamp: str | None = None,
    sample_limit: int = 25,
    text_field: str = "retrieval_text",
    fail_on_conflict: bool = False,
    max_source_report_age_hours: float | None = None,
    strict_temporal_evidence: bool = False,
    batch_reports: list[Path] | None = None,
    public_readiness_report: Path | None = None,
    parser_goldset_score_report: Path | None = None,
    rag_eval_report: Path | None = None,
    mcp_demo_answer_report: Path | None = None,
    accuracy_comparison_report: Path | None = None,
    profile_provenance_report: Path | None = None,
    mcp_readiness_report: Path | None = None,
    mcp_transport_smoke_report: Path | None = None,
    revision_impact_reports: list[Path] | None = None,
    runtime_version_drift_report: Path | None = None,
    approval_worklist_reports: list[Path] | None = None,
    approval_review_batch_reports: list[Path] | None = None,
    reapproval_worklist_reports: list[Path] | None = None,
    reapproval_review_batch_reports: list[Path] | None = None,
    reapproval_decision_validation_reports: list[Path] | None = None,
    reapproval_apply_plan_reports: list[Path] | None = None,
    min_average_quality_score: float = 98.0,
    min_parser_goldset_f1: float = 90.0,
    min_answerable_ratio: float = 0.8,
    require_full_index: bool = True,
    fail_on_product_readiness: bool = False,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    shadow_data_dir = Path(shadow_data_dir) if shadow_data_dir else reports_dir / f"temporal_shadow_runtime_{timestamp}"

    coverage_json = reports_dir / f"temporal_metadata_coverage_{timestamp}.json"
    coverage_md = reports_dir / f"temporal_metadata_coverage_{timestamp}.md"
    backfill_json = reports_dir / f"temporal_backfill_shadow_{timestamp}.json"
    ambiguity_json = reports_dir / f"temporal_ambiguity_review_scope_{timestamp}.json"
    ambiguity_md = reports_dir / f"temporal_ambiguity_review_scope_{timestamp}.md"
    generated_runtime_drift_json = reports_dir / f"runtime_version_drift_{timestamp}.json"
    generated_runtime_drift_md = reports_dir / f"runtime_version_drift_{timestamp}.md"
    product_json = reports_dir / f"mcp_product_readiness_temporal_{timestamp}.json"
    product_md = reports_dir / f"mcp_product_readiness_temporal_{timestamp}.md"

    temporal_coverage = build_temporal_metadata_coverage_report(
        data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        sample_limit=sample_limit,
        out_json=coverage_json,
        out_md=coverage_md,
    )
    temporal_backfill = build_temporal_backfill_shadow_runtime(
        source_data_dir=runtime_data_dir,
        out_data_dir=shadow_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        out_manifest=backfill_json,
        text_field=text_field,
        fail_on_conflict=fail_on_conflict,
    )
    temporal_ambiguity = build_temporal_ambiguity_review_scope(
        temporal_report=backfill_json,
        sample_limit_per_slice=sample_limit,
        out_json=ambiguity_json,
        out_md=ambiguity_md,
    )
    generated_runtime_drift = runtime_version_drift_report is None
    effective_runtime_version_drift_report = runtime_version_drift_report
    runtime_drift: dict[str, Any] | None = None
    if generated_runtime_drift:
        runtime_drift = build_runtime_version_drift_report(
            data_dir=runtime_data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            sample_limit=sample_limit,
            out_json=generated_runtime_drift_json,
            out_md=generated_runtime_drift_md,
        )
        effective_runtime_version_drift_report = generated_runtime_drift_json
    product_readiness = build_mcp_product_readiness_audit(
        runtime_data_dir=runtime_data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        batch_reports=batch_reports or [],
        public_readiness_report=public_readiness_report,
        parser_goldset_score_report=parser_goldset_score_report,
        rag_eval_report=rag_eval_report,
        mcp_demo_answer_report=mcp_demo_answer_report,
        accuracy_comparison_report=accuracy_comparison_report,
        profile_provenance_report=profile_provenance_report,
        mcp_readiness_report=mcp_readiness_report,
        mcp_transport_smoke_report=mcp_transport_smoke_report,
        temporal_coverage_report=coverage_json,
        temporal_backfill_shadow_report=backfill_json,
        temporal_ambiguity_scope_report=ambiguity_json,
        revision_impact_reports=revision_impact_reports or [],
        runtime_version_drift_report=effective_runtime_version_drift_report,
        approval_worklist_reports=approval_worklist_reports or [],
        approval_review_batch_reports=approval_review_batch_reports or [],
        reapproval_worklist_reports=reapproval_worklist_reports or [],
        reapproval_review_batch_reports=reapproval_review_batch_reports or [],
        reapproval_decision_validation_reports=reapproval_decision_validation_reports or [],
        reapproval_apply_plan_reports=reapproval_apply_plan_reports or [],
        min_average_quality_score=min_average_quality_score,
        min_parser_goldset_f1=min_parser_goldset_f1,
        min_answerable_ratio=min_answerable_ratio,
        require_full_index=require_full_index,
        max_source_report_age_hours=max_source_report_age_hours,
        strict_temporal_evidence=strict_temporal_evidence,
        out_json=product_json,
        out_md=product_md,
    )

    blockers = _bundle_blockers(
        temporal_backfill=temporal_backfill,
        product_readiness=product_readiness,
        fail_on_conflict=fail_on_conflict,
        fail_on_product_readiness=fail_on_product_readiness,
    )
    bundle_json = out_json or reports_dir / f"mcp_temporal_readiness_bundle_{timestamp}.json"
    bundle_md = out_md or reports_dir / f"mcp_temporal_readiness_bundle_{timestamp}.md"
    report = {
        "report_type": "mcp_temporal_product_readiness_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "status": "blocked" if blockers else ("completed_with_product_findings" if not product_readiness.get("passed") else "completed"),
        "passed": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "runtime_data_dir": str(runtime_data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "shadow_data_dir": str(shadow_data_dir),
        "summary": {
            "temporal_coverage_passed": bool(temporal_coverage.get("passed")),
            "temporal_coverage_record_count": temporal_coverage.get("record_count"),
            "temporal_coverage_ratio": temporal_coverage.get("temporal_metadata_ratio"),
            "shadow_conflict_chunk_count": (temporal_backfill.get("after") or {}).get("conflict_chunk_count"),
            "shadow_ambiguous_chunk_count": (temporal_backfill.get("after") or {}).get("ambiguous_chunk_count"),
            "shadow_runtime_written": bool(temporal_backfill.get("shadow_runtime_written")),
            "shadow_runtime_runnable": bool(temporal_backfill.get("shadow_runtime_runnable")),
            "shadow_write_blocked": bool(temporal_backfill.get("write_blocked")),
            "shadow_vector_record_count": int(temporal_backfill.get("vector_record_count") or 0),
            "shadow_vector_projection_ready": _shadow_vector_projection_ready(temporal_backfill),
            "temporal_ambiguity_status": temporal_ambiguity.get("status"),
            "temporal_ambiguity_passed": bool(temporal_ambiguity.get("passed")),
            "runtime_version_drift_generated": generated_runtime_drift,
            "runtime_version_drift_passed": bool((runtime_drift or {}).get("passed"))
            if generated_runtime_drift
            else None,
            "product_readiness_passed": bool(product_readiness.get("passed")),
            "product_readiness_blocking_count": product_readiness.get("blocking_count"),
            "product_readiness_warning_count": product_readiness.get("warning_count"),
            "temporal_evidence_guard_passed": (product_readiness.get("temporal_evidence_guard_summary") or {}).get("passed"),
            "reapproval_apply_plan_unsafe_contract_violation_count": (
                product_readiness.get("reapproval_apply_plan_summary") or {}
            ).get("unsafe_contract_violation_count"),
        },
        "artifacts": {
            "temporal_coverage_json": str(coverage_json),
            "temporal_coverage_markdown": str(coverage_md),
            "temporal_backfill_shadow_json": str(backfill_json),
            "temporal_ambiguity_scope_json": str(ambiguity_json),
            "temporal_ambiguity_scope_markdown": str(ambiguity_md),
            "runtime_version_drift_json": str(effective_runtime_version_drift_report)
            if effective_runtime_version_drift_report
            else None,
            "runtime_version_drift_markdown": str(generated_runtime_drift_md)
            if generated_runtime_drift
            else None,
            "product_readiness_json": str(product_json),
            "product_readiness_markdown": str(product_md),
            "bundle_json": str(bundle_json),
            "bundle_markdown": str(bundle_md),
        },
        "product_readiness_source_reports": product_readiness.get("source_reports"),
        "safety_note": (
            "This bundle generates temporal evidence and a product-readiness report. It does not approve chunks, "
            "write approved Vector DB records, publish MCP artifacts, or apply shadow backfill to the source runtime."
        ),
        "api_call_count": 0,
    }
    bundle_json.parent.mkdir(parents=True, exist_ok=True)
    bundle_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    bundle_md.parent.mkdir(parents=True, exist_ok=True)
    bundle_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _bundle_blockers(
    *,
    temporal_backfill: dict[str, Any],
    product_readiness: dict[str, Any],
    fail_on_conflict: bool,
    fail_on_product_readiness: bool,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    conflict_count = int((temporal_backfill.get("after") or {}).get("conflict_chunk_count") or 0)
    input_chunk_count = int(temporal_backfill.get("input_chunk_count") or 0)
    vector_record_count = int(temporal_backfill.get("vector_record_count") or 0)
    if input_chunk_count > 0 and vector_record_count <= 0:
        blockers.append(
            {
                "code": "temporal-backfill-vector-projection-empty",
                "detail": (
                    "Shadow temporal backfill processed repository chunks but produced no approved vector records. "
                    "Restore the repository approval/indexing contract before using this evidence for readiness."
                ),
                "input_chunk_count": input_chunk_count,
                "vector_record_count": vector_record_count,
            }
        )
    if fail_on_conflict and conflict_count:
        blockers.append(
            {
                "code": "temporal-backfill-conflict",
                "detail": "Shadow temporal backfill found conflicts and --fail-on-conflict was set.",
                "conflict_chunk_count": conflict_count,
            }
        )
    if fail_on_product_readiness and not bool(product_readiness.get("passed")):
        blockers.append(
            {
                "code": "product-readiness-blocked",
                "detail": "MCP product readiness did not pass and --fail-on-product-readiness was set.",
                "blocking_codes": list(product_readiness.get("blocking_codes") or []),
            }
        )
    return blockers


def _shadow_vector_projection_ready(temporal_backfill: dict[str, Any]) -> bool:
    return bool(
        temporal_backfill.get("passed")
        and temporal_backfill.get("shadow_runtime_written")
        and int(temporal_backfill.get("vector_record_count") or 0) > 0
    )


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    lines = [
        "# MCP Temporal Product Readiness Bundle",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Runtime: `{report.get('runtime_data_dir')}`",
        f"- Shadow runtime: `{report.get('shadow_data_dir')}`",
        f"- Tenant: `{report.get('tenant_id')}`",
        "",
        "## Summary",
        "",
        f"- Temporal coverage: `{str(summary.get('temporal_coverage_passed')).lower()}` ({summary.get('temporal_coverage_ratio')})",
        f"- Shadow conflicts / ambiguous chunks: {summary.get('shadow_conflict_chunk_count')} / {summary.get('shadow_ambiguous_chunk_count')}",
        f"- Shadow runtime written / write blocked: `{str(summary.get('shadow_runtime_written')).lower()}` / `{str(summary.get('shadow_write_blocked')).lower()}`",
        f"- Shadow runtime runnable: `{str(summary.get('shadow_runtime_runnable')).lower()}` (evidence-only copies are intentionally non-runnable)",
        f"- Shadow vector projection ready / records: `{str(summary.get('shadow_vector_projection_ready')).lower()}` / {summary.get('shadow_vector_record_count')}",
        f"- Temporal ambiguity status: `{summary.get('temporal_ambiguity_status')}`",
        f"- Runtime drift generated / passed: `{str(summary.get('runtime_version_drift_generated')).lower()}` / `{str(summary.get('runtime_version_drift_passed')).lower()}`",
        f"- Product readiness passed: `{str(summary.get('product_readiness_passed')).lower()}`",
        f"- Product readiness blockers/warnings: {summary.get('product_readiness_blocking_count')} / {summary.get('product_readiness_warning_count')}",
        f"- Temporal evidence guard passed: `{str(summary.get('temporal_evidence_guard_passed')).lower()}`",
        "",
    ]
    blockers = [item for item in report.get("blockers") or [] if isinstance(item, dict)]
    if blockers:
        lines.extend(["## Blockers", ""])
        for blocker in blockers:
            lines.append(f"- `{blocker.get('code')}`: {blocker.get('detail')}")
        lines.append("")
    lines.extend(["## Artifacts", ""])
    for key, value in artifacts.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build temporal evidence and MCP product-readiness reports in one reproducible pass."
    )
    parser.add_argument("--runtime-data-dir", default="data")
    parser.add_argument("--tenant-id", default="default")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--shadow-data-dir", default=None)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--sample-limit", type=int, default=25)
    parser.add_argument("--text-field", choices=["retrieval_text", "text", "normalized_text"], default="retrieval_text")
    parser.add_argument("--fail-on-conflict", action="store_true")
    parser.add_argument("--max-source-report-age-hours", type=float, default=None)
    parser.add_argument("--strict-temporal-evidence", action="store_true")
    parser.add_argument("--batch-report", action="append", default=[])
    parser.add_argument("--public-readiness-report", default=None)
    parser.add_argument("--parser-goldset-score-report", default=None)
    parser.add_argument("--rag-eval-report", default=None)
    parser.add_argument("--mcp-demo-answer-report", default=None)
    parser.add_argument("--accuracy-comparison-report", default=None)
    parser.add_argument("--profile-provenance-report", default=None)
    parser.add_argument("--mcp-readiness-report", default=None)
    parser.add_argument("--mcp-transport-smoke-report", default=None)
    parser.add_argument("--revision-impact-report", action="append", default=[])
    parser.add_argument("--runtime-version-drift-report", default=None)
    parser.add_argument("--approval-worklist-report", action="append", default=[])
    parser.add_argument("--approval-review-batch-report", action="append", default=[])
    parser.add_argument("--reapproval-worklist-report", action="append", default=[])
    parser.add_argument("--reapproval-review-batch-report", action="append", default=[])
    parser.add_argument("--reapproval-decision-validation-report", action="append", default=[])
    parser.add_argument("--reapproval-apply-plan-report", action="append", default=[])
    parser.add_argument("--min-average-quality-score", type=float, default=98.0)
    parser.add_argument("--min-parser-goldset-f1", type=float, default=90.0)
    parser.add_argument("--min-answerable-ratio", type=float, default=0.8)
    parser.add_argument("--allow-partial-index", action="store_true")
    parser.add_argument("--fail-on-product-readiness", action="store_true")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_mcp_temporal_product_readiness_bundle(
        runtime_data_dir=Path(args.runtime_data_dir),
        tenant_id=args.tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        reports_dir=Path(args.reports_dir),
        shadow_data_dir=Path(args.shadow_data_dir) if args.shadow_data_dir else None,
        timestamp=args.timestamp,
        sample_limit=args.sample_limit,
        text_field=args.text_field,
        fail_on_conflict=args.fail_on_conflict,
        max_source_report_age_hours=args.max_source_report_age_hours,
        strict_temporal_evidence=args.strict_temporal_evidence,
        batch_reports=[Path(value) for value in args.batch_report],
        public_readiness_report=Path(args.public_readiness_report) if args.public_readiness_report else None,
        parser_goldset_score_report=Path(args.parser_goldset_score_report) if args.parser_goldset_score_report else None,
        rag_eval_report=Path(args.rag_eval_report) if args.rag_eval_report else None,
        mcp_demo_answer_report=Path(args.mcp_demo_answer_report) if args.mcp_demo_answer_report else None,
        accuracy_comparison_report=Path(args.accuracy_comparison_report) if args.accuracy_comparison_report else None,
        profile_provenance_report=Path(args.profile_provenance_report) if args.profile_provenance_report else None,
        mcp_readiness_report=Path(args.mcp_readiness_report) if args.mcp_readiness_report else None,
        mcp_transport_smoke_report=Path(args.mcp_transport_smoke_report) if args.mcp_transport_smoke_report else None,
        revision_impact_reports=[Path(value) for value in args.revision_impact_report],
        runtime_version_drift_report=Path(args.runtime_version_drift_report) if args.runtime_version_drift_report else None,
        approval_worklist_reports=[Path(value) for value in args.approval_worklist_report],
        approval_review_batch_reports=[Path(value) for value in args.approval_review_batch_report],
        reapproval_worklist_reports=[Path(value) for value in args.reapproval_worklist_report],
        reapproval_review_batch_reports=[Path(value) for value in args.reapproval_review_batch_report],
        reapproval_decision_validation_reports=[
            Path(value) for value in args.reapproval_decision_validation_report
        ],
        reapproval_apply_plan_reports=[Path(value) for value in args.reapproval_apply_plan_report],
        min_average_quality_score=args.min_average_quality_score,
        min_parser_goldset_f1=args.min_parser_goldset_f1,
        min_answerable_ratio=args.min_answerable_ratio,
        require_full_index=not args.allow_partial_index,
        fail_on_product_readiness=args.fail_on_product_readiness,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    return 2 if report["blocker_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
