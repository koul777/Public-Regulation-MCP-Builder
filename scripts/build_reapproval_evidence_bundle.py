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

from scripts.audit_runtime_version_drift import build_runtime_version_drift_report
from scripts.build_reapproval_review_batches import (
    DEFAULT_MAX_CHUNKS_PER_BATCH,
    SUPPORTED_REVIEW_TIERS,
    build_reapproval_review_batches,
    write_csv as write_reapproval_batches_csv,
    write_decision_template_csv,
    write_markdown as write_reapproval_batches_markdown,
)
from scripts.build_reapproval_review_burden_report import build_reapproval_review_burden_report
from scripts.build_reapproval_worklist import (
    build_reapproval_worklist,
    write_chunk_csv,
    write_chunk_json,
    write_csv as write_reapproval_worklist_csv,
    write_markdown as write_reapproval_worklist_markdown,
)
from scripts.report_metadata import current_repo_commit
from scripts.validate_reapproval_decisions import validate_reapproval_decisions


def build_reapproval_evidence_bundle(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    reports_dir: Path = Path("reports"),
    label: str = "current",
    source_system: str | None = None,
    apba_id: str | None = None,
    runtime_version_drift_report: Path | None = None,
    sample_limit: int = 25,
    review_batch_size: int = 100,
    review_seconds_per_chunk: int = 20,
    low_risk_sample_rate: float = 0.05,
    temporal_sample_rate: float = 0.15,
    min_sample_chunks_per_tier: int = 10,
    chunk_sample_limit: int = 10,
    max_chunks_per_batch: int = DEFAULT_MAX_CHUNKS_PER_BATCH,
    include_review_tiers: Sequence[str] = SUPPORTED_REVIEW_TIERS,
    include_suggested_actions: Sequence[str] | None = None,
    fail_on_release_gate: bool = False,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    label = _safe_label(label)
    paths = _bundle_paths(reports_dir, label)

    generated_runtime_drift = runtime_version_drift_report is None
    effective_runtime_drift_report = runtime_version_drift_report
    runtime_drift: dict[str, Any] | None = None
    if generated_runtime_drift:
        runtime_drift = build_runtime_version_drift_report(
            data_dir=Path(data_dir),
            tenant_id=tenant_id,
            tenant_storage_isolation=tenant_storage_isolation,
            sample_limit=sample_limit,
            out_json=paths["runtime_version_drift_json"],
            out_md=paths["runtime_version_drift_markdown"],
        )
        effective_runtime_drift_report = paths["runtime_version_drift_json"]

    worklist = build_reapproval_worklist(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        source_system=source_system,
        apba_id=apba_id,
        runtime_version_drift_report=effective_runtime_drift_report,
        review_batch_size=review_batch_size,
        review_seconds_per_chunk=review_seconds_per_chunk,
        low_risk_sample_rate=low_risk_sample_rate,
        temporal_sample_rate=temporal_sample_rate,
        min_sample_chunks_per_tier=min_sample_chunks_per_tier,
        sample_limit=chunk_sample_limit,
        include_chunk_candidates=True,
    )
    chunk_candidate_rows = list(worklist.pop("_chunk_candidate_rows", []))
    _write_json(paths["reapproval_worklist_json"], worklist)
    write_reapproval_worklist_csv(paths["reapproval_worklist_csv"], worklist["documents"])
    write_chunk_csv(paths["reapproval_worklist_chunks_csv"], chunk_candidate_rows)
    write_chunk_json(paths["reapproval_worklist_chunks_json"], worklist, chunk_candidate_rows)
    write_reapproval_worklist_markdown(worklist, paths["reapproval_worklist_markdown"])

    review_batches = build_reapproval_review_batches(
        worklist_report=paths["reapproval_worklist_json"],
        worklist_chunks_report=paths["reapproval_worklist_chunks_json"],
        worklist_report_artifact_path=_safe_relative_artifact_path(paths["reapproval_worklist_json"]),
        worklist_chunks_artifact_path=_safe_relative_artifact_path(paths["reapproval_worklist_chunks_json"]),
        max_chunks_per_batch=max_chunks_per_batch,
        include_review_tiers=include_review_tiers,
        include_suggested_actions=include_suggested_actions,
    )
    _write_json(paths["reapproval_review_batches_json"], review_batches)
    write_reapproval_batches_csv(paths["reapproval_review_batches_csv"], review_batches["batches"])
    write_decision_template_csv(paths["reapproval_decision_template_csv"], review_batches["batches"])
    write_reapproval_batches_markdown(review_batches, paths["reapproval_review_batches_markdown"])

    burden = build_reapproval_review_burden_report(
        reapproval_worklist_report=paths["reapproval_worklist_json"],
        reapproval_review_batch_report=paths["reapproval_review_batches_json"],
        decision_template_csv=paths["reapproval_decision_template_csv"],
        out_json=paths["reapproval_review_burden_json"],
        out_md=paths["reapproval_review_burden_markdown"],
    )
    decision_validation = validate_reapproval_decisions(
        reapproval_review_batch_report=paths["reapproval_review_batches_json"],
        decision_template_csv=paths["reapproval_decision_template_csv"],
        out_json=paths["reapproval_decision_validation_json"],
        out_md=paths["reapproval_decision_validation_markdown"],
    )

    technical_blockers = _technical_blockers(
        runtime_drift=runtime_drift,
        worklist=worklist,
        review_batches=review_batches,
        burden=burden,
    )
    if fail_on_release_gate and burden.get("release_blocker_count"):
        technical_blockers.append(
            {
                "code": "release-gate-blocked-pending-reapproval-decisions",
                "detail": "Reapproval operator decision template is not complete and --fail-on-release-gate was set.",
                "release_gate_status": burden.get("release_gate_status"),
                "release_blocker_count": burden.get("release_blocker_count"),
            }
        )

    bundle_json = out_json or paths["bundle_json"]
    bundle_md = out_md or paths["bundle_markdown"]
    report = {
        "report_type": "reapproval_evidence_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": not technical_blockers,
        "status": _status(
            technical_blockers=technical_blockers,
            candidate_chunks=_int(worklist.get("reapproval_candidate_chunks")),
        ),
        "technical_blocker_count": len(technical_blockers),
        "technical_blockers": technical_blockers,
        "release_gate_status": burden.get("release_gate_status"),
        "release_blocker_count": burden.get("release_blocker_count"),
        "runtime_data_dir": str(data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": tenant_storage_isolation,
        "filters": {
            key: value
            for key, value in {
                "source_system": source_system,
                "apba_id": apba_id,
            }.items()
            if value
        },
        "summary": {
            "runtime_version_drift_generated": generated_runtime_drift,
            "runtime_version_drift_passed": bool((runtime_drift or {}).get("passed"))
            if generated_runtime_drift
            else None,
            "document_count": worklist.get("document_count"),
            "total_approved_chunks": worklist.get("total_approved_chunks"),
            "reapproval_candidate_chunks": worklist.get("reapproval_candidate_chunks"),
            "recommended_initial_review_chunks": worklist.get("recommended_initial_review_chunks"),
            "estimated_initial_review_minutes": worklist.get("estimated_initial_review_minutes"),
            "review_batch_count": review_batches.get("batch_count"),
            "selected_candidate_count": review_batches.get("selected_candidate_count"),
            "reapproval_chunk_count": review_batches.get("reapproval_chunk_count"),
            "decision_template_row_count": burden.get("decision_template_row_count"),
            "decision_template_operator_decision_blank_count": burden.get(
                "decision_template_operator_decision_blank_count"
            ),
            "decision_template_operator_decision_complete_count": burden.get(
                "decision_template_operator_decision_complete_count"
            ),
            "decision_validation_status": decision_validation.get("release_gate_status"),
            "decision_validation_blocking_count": decision_validation.get("blocking_count"),
            "decision_validation_complete_row_count": decision_validation.get("complete_row_count"),
            "decision_validation_blank_or_incomplete_row_count": decision_validation.get(
                "blank_or_incomplete_row_count"
            ),
            "approval_provenance_missing_chunks": worklist.get("approval_provenance_missing_chunks"),
            "source_vector_integrity_failure_count": worklist.get("source_vector_integrity_failure_count"),
        },
        "artifacts": _artifact_summary(paths, bundle_json=bundle_json, bundle_md=bundle_md),
        "product_readiness_inputs": {
            "reapproval_worklist_report": str(paths["reapproval_worklist_json"]),
            "reapproval_review_batch_report": str(paths["reapproval_review_batches_json"]),
            "reapproval_decision_validation_report": str(paths["reapproval_decision_validation_json"]),
            "runtime_version_drift_report": str(effective_runtime_drift_report)
            if effective_runtime_drift_report
            else None,
        },
        "next_steps": _next_steps(
            candidate_chunks=_int(worklist.get("reapproval_candidate_chunks")),
            release_gate_status=str(burden.get("release_gate_status") or ""),
            paths=paths,
        ),
        "safety_note": (
            "This bundle prepares reapproval evidence only. It does not reprocess files, approve chunks, "
            "set operator decisions, write approved Vector DB records, reindex, or publish MCP artifacts."
        ),
        "api_call_count": 0,
    }
    _write_json(bundle_json, report)
    bundle_md.parent.mkdir(parents=True, exist_ok=True)
    bundle_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _bundle_paths(reports_dir: Path, label: str) -> dict[str, Path]:
    return {
        "runtime_version_drift_json": reports_dir / f"runtime_version_drift_{label}.json",
        "runtime_version_drift_markdown": reports_dir / f"runtime_version_drift_{label}.md",
        "reapproval_worklist_json": reports_dir / f"reapproval_worklist_{label}.json",
        "reapproval_worklist_csv": reports_dir / f"reapproval_worklist_{label}.csv",
        "reapproval_worklist_chunks_csv": reports_dir / f"reapproval_worklist_{label}_chunks.csv",
        "reapproval_worklist_chunks_json": reports_dir / f"reapproval_worklist_{label}_chunks.json",
        "reapproval_worklist_markdown": reports_dir / f"reapproval_worklist_{label}.md",
        "reapproval_review_batches_json": reports_dir / f"reapproval_review_batches_{label}.json",
        "reapproval_review_batches_csv": reports_dir / f"reapproval_review_batches_{label}.csv",
        "reapproval_review_batches_markdown": reports_dir / f"reapproval_review_batches_{label}.md",
        "reapproval_decision_template_csv": reports_dir / f"reapproval_review_batch_decisions_{label}.csv",
        "reapproval_review_burden_json": reports_dir / f"reapproval_review_burden_{label}.json",
        "reapproval_review_burden_markdown": reports_dir / f"reapproval_review_burden_{label}.md",
        "reapproval_decision_validation_json": reports_dir / f"reapproval_decision_validation_{label}.json",
        "reapproval_decision_validation_markdown": reports_dir / f"reapproval_decision_validation_{label}.md",
        "bundle_json": reports_dir / f"reapproval_evidence_bundle_{label}.json",
        "bundle_markdown": reports_dir / f"reapproval_evidence_bundle_{label}.md",
    }


def _technical_blockers(
    *,
    runtime_drift: dict[str, Any] | None,
    worklist: dict[str, Any],
    review_batches: dict[str, Any],
    burden: dict[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if runtime_drift is not None and int(runtime_drift.get("blocker_count") or 0) > 0:
        blockers.append(
            {
                "code": "runtime-version-drift-blockers-present",
                "detail": "Resolve runtime drift blockers before reapproval planning.",
                "blocker_count": runtime_drift.get("blocker_count"),
            }
        )
    pre_reapproval_blockers = [item for item in worklist.get("pre_reapproval_blockers") or [] if isinstance(item, dict)]
    if pre_reapproval_blockers:
        blockers.append(
            {
                "code": "pre-reapproval-blockers-present",
                "detail": "Resolve vector integrity or runtime blockers before reapproval planning.",
                "blocker_count": len(pre_reapproval_blockers),
            }
        )
    if int(review_batches.get("blocker_count") or 0) > 0:
        blockers.append(
            {
                "code": "reapproval-review-batch-blockers-present",
                "detail": "Reapproval review batch manifest has blockers.",
                "blocker_count": review_batches.get("blocker_count"),
            }
        )
    if int(burden.get("blocking_count") or 0) > 0:
        blockers.append(
            {
                "code": "reapproval-review-burden-blockers-present",
                "detail": "Reapproval review burden report has technical blockers.",
                "blocking_count": burden.get("blocking_count"),
            }
        )
    return blockers


def _status(*, technical_blockers: Sequence[dict[str, Any]], candidate_chunks: int) -> str:
    if technical_blockers:
        return "blocked"
    if candidate_chunks > 0:
        return "ready_for_operator_decisions"
    return "no_reapproval_candidates"


def _artifact_summary(paths: dict[str, Path], *, bundle_json: Path, bundle_md: Path) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for key, path in paths.items():
        if key in {"bundle_json", "bundle_markdown"}:
            continue
        summary[key] = _artifact(path)
    summary["bundle_json"] = {"path": str(bundle_json), "exists": None, "sha256": None, "byte_count": None}
    summary["bundle_markdown"] = {"path": str(bundle_md), "exists": None, "sha256": None, "byte_count": None}
    return summary


def _artifact(path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "sha256": None,
        "byte_count": None,
    }
    if not path.is_file():
        return item
    data = path.read_bytes()
    item["byte_count"] = len(data)
    item["sha256"] = hashlib.sha256(data).hexdigest()
    return item


def _next_steps(*, candidate_chunks: int, release_gate_status: str, paths: dict[str, Path]) -> list[dict[str, Any]]:
    if candidate_chunks <= 0:
        return [
            {
                "step": "attach_to_product_readiness",
                "detail": "No reapproval candidates were found. Attach the empty worklist and batch manifest as evidence.",
                "reapproval_worklist_report": str(paths["reapproval_worklist_json"]),
                "reapproval_review_batch_report": str(paths["reapproval_review_batches_json"]),
                "reapproval_decision_validation_report": str(paths["reapproval_decision_validation_json"]),
            }
        ]
    steps = [
        {
            "step": "operator_review",
            "detail": (
                "Assign review batches, complete operator_decision, reviewer_id, reviewed_at, "
                "and approval_scope_confirmation in the decision template, then rerun decision validation."
            ),
            "decision_template_csv": str(paths["reapproval_decision_template_csv"]),
            "decision_validation_report": str(paths["reapproval_decision_validation_json"]),
        },
        {
            "step": "build_reapproval_apply_plan",
            "detail": (
                "After decision validation passes, run reg-rag-reapproval-apply-plan to map reviewed decisions "
                "to approve/reject/reprocess/defer chunk operations without changing runtime state."
            ),
            "reapproval_review_batch_report": str(paths["reapproval_review_batches_json"]),
            "decision_template_csv": str(paths["reapproval_decision_template_csv"]),
            "decision_validation_report": str(paths["reapproval_decision_validation_json"]),
        },
        {
            "step": "dedicated_reapproval_apply_and_reindex",
            "detail": "Only after the read-only apply plan passes, run a dedicated reapproval apply path and reindex affected approved Vector DB records.",
        },
        {
            "step": "rerun_readiness",
            "detail": "Attach the worklist and review batch manifest to MCP product readiness.",
            "reapproval_worklist_report": str(paths["reapproval_worklist_json"]),
            "reapproval_review_batch_report": str(paths["reapproval_review_batches_json"]),
            "reapproval_decision_validation_report": str(paths["reapproval_decision_validation_json"]),
        },
    ]
    if release_gate_status == "blocked_pending_operator_decisions":
        steps.insert(
            0,
            {
                "step": "release_gate_blocked",
                "detail": "Blank operator decisions are not approvals. Official release remains blocked until decisions are complete.",
            },
        )
    return steps


def _safe_relative_artifact_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        raw = str(path).replace("\\", "/")
        if raw and not path.is_absolute() and ".." not in raw.split("/") and not raw.startswith("/"):
            return raw
        return path.name


def _safe_label(value: str) -> str:
    text = str(value or "current").strip()
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in text) or "current"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# Reapproval Evidence Bundle",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Passed: `{str(report.get('passed')).lower()}`",
        f"- Release gate status: `{report.get('release_gate_status')}`",
        f"- Runtime: `{report.get('runtime_data_dir')}`",
        f"- Tenant: `{report.get('tenant_id')}`",
        "",
        "## Summary",
        "",
        f"- Runtime drift generated / passed: `{str(summary.get('runtime_version_drift_generated')).lower()}` / `{str(summary.get('runtime_version_drift_passed')).lower()}`",
        f"- Approved chunks: `{summary.get('total_approved_chunks')}`",
        f"- Reapproval candidate chunks: `{summary.get('reapproval_candidate_chunks')}`",
        f"- Recommended initial review chunks: `{summary.get('recommended_initial_review_chunks')}`",
        f"- Estimated initial review minutes: `{summary.get('estimated_initial_review_minutes')}`",
        f"- Review batches: `{summary.get('review_batch_count')}`",
        f"- Decision rows complete / blank: `{summary.get('decision_template_operator_decision_complete_count')}` / `{summary.get('decision_template_operator_decision_blank_count')}`",
        f"- Decision validation status: `{summary.get('decision_validation_status')}`",
        f"- Decision validation blockers: `{summary.get('decision_validation_blocking_count')}`",
        f"- Approval provenance missing chunks: `{summary.get('approval_provenance_missing_chunks')}`",
        f"- Source vector integrity failures: `{summary.get('source_vector_integrity_failure_count')}`",
        "",
    ]
    blockers = [item for item in report.get("technical_blockers") or [] if isinstance(item, dict)]
    if blockers:
        lines.extend(["## Technical Blockers", ""])
        for blocker in blockers:
            lines.append(f"- `{blocker.get('code')}`: {blocker.get('detail')}")
        lines.append("")
    lines.extend(["## Artifacts", ""])
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    for key, item in artifacts.items():
        path = item.get("path") if isinstance(item, dict) else item
        lines.append(f"- `{key}`: `{path}`")
    lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
    return "\n".join(lines).rstrip() + "\n"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build read-only reapproval evidence bundle in one pass."
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", default="default")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--tenant-storage-isolation", action="store_true")
    storage.add_argument("--flat-storage", action="store_true")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--label", default="current")
    parser.add_argument("--source-system")
    parser.add_argument("--apba-id")
    parser.add_argument("--runtime-version-drift-report", type=Path)
    parser.add_argument("--sample-limit", type=int, default=25)
    parser.add_argument("--review-batch-size", type=int, default=100)
    parser.add_argument("--review-seconds-per-chunk", type=int, default=20)
    parser.add_argument("--low-risk-sample-rate", type=float, default=0.05)
    parser.add_argument("--temporal-sample-rate", type=float, default=0.15)
    parser.add_argument("--min-sample-chunks-per-tier", type=int, default=10)
    parser.add_argument("--chunk-sample-limit", type=int, default=10)
    parser.add_argument("--max-chunks-per-batch", type=int, default=DEFAULT_MAX_CHUNKS_PER_BATCH)
    parser.add_argument("--include-review-tier", action="append", default=[])
    parser.add_argument("--include-suggested-action", action="append", default=[])
    parser.add_argument("--fail-on-technical-blocker", action="store_true")
    parser.add_argument("--fail-on-release-gate", action="store_true")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = parse_args(argv)
    tenant_storage_isolation = None
    if args.tenant_storage_isolation:
        tenant_storage_isolation = True
    if args.flat_storage:
        tenant_storage_isolation = False
    report = build_reapproval_evidence_bundle(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        reports_dir=args.reports_dir,
        label=args.label,
        source_system=args.source_system,
        apba_id=args.apba_id,
        runtime_version_drift_report=args.runtime_version_drift_report,
        sample_limit=args.sample_limit,
        review_batch_size=args.review_batch_size,
        review_seconds_per_chunk=args.review_seconds_per_chunk,
        low_risk_sample_rate=args.low_risk_sample_rate,
        temporal_sample_rate=args.temporal_sample_rate,
        min_sample_chunks_per_tier=args.min_sample_chunks_per_tier,
        chunk_sample_limit=args.chunk_sample_limit,
        max_chunks_per_batch=args.max_chunks_per_batch,
        include_review_tiers=args.include_review_tier or SUPPORTED_REVIEW_TIERS,
        include_suggested_actions=args.include_suggested_action,
        fail_on_release_gate=args.fail_on_release_gate,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    if args.fail_on_technical_blocker and int(report["technical_blocker_count"]) > 0:
        return 2
    if args.fail_on_release_gate and int(report["technical_blocker_count"]) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
