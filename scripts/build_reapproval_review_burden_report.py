from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit
from scripts.reapproval_decision_contract import (
    REAPPROVAL_DECISION_OPERATOR_DECISIONS,
    is_allowed_operator_decision,
    normalize_operator_decision,
    row_missing_required_decision_fields,
)


HASH_CHUNK_BYTES = 1024 * 1024


def build_reapproval_review_burden_report(
    *,
    reapproval_worklist_report: Path,
    reapproval_review_batch_report: Path | None = None,
    decision_template_csv: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    worklist = _load_json(reapproval_worklist_report)
    batch = _load_json(reapproval_review_batch_report) if reapproval_review_batch_report else {}
    decision_template = _decision_template_summary(decision_template_csv) if decision_template_csv else {}
    workload = _workload_summary(worklist)
    tier_breakdown = _tier_breakdown(worklist)
    batch_summary = _batch_summary(batch)
    findings = _findings(workload, tier_breakdown, batch_summary, worklist)
    release_blockers = _release_gate_blockers(workload, decision_template, bool(decision_template_csv))
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    source_report_artifacts = [
        _source_artifact("reapproval_worklist_report", reapproval_worklist_report, expected_report_type="reapproval_worklist"),
        *(
            [
                _source_artifact(
                    "reapproval_review_batch_manifest_report",
                    reapproval_review_batch_report,
                    expected_report_type="reapproval_review_batch_manifest",
                )
            ]
            if reapproval_review_batch_report
            else []
        ),
        *(
            [_source_artifact("reapproval_decision_template_csv", decision_template_csv)]
            if decision_template_csv
            else []
        ),
    ]
    report = {
        "report_type": "reapproval_review_burden",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": blocker_count == 0,
        "status": _status(blocker_count, workload["reapproval_candidate_chunks"]),
        "blocking_count": blocker_count,
        "warning_count": warning_count,
        "findings": findings,
        "release_gate_status": "blocked_pending_operator_decisions" if release_blockers else "ready_for_release_gate",
        "release_blocker_count": len(release_blockers),
        "release_blockers": release_blockers,
        "source_reports": {
            "reapproval_worklist_report": str(reapproval_worklist_report),
            "reapproval_review_batch_report": (
                str(reapproval_review_batch_report) if reapproval_review_batch_report else None
            ),
            "decision_template_csv": str(decision_template_csv) if decision_template_csv else None,
        },
        "source_report_artifacts": source_report_artifacts,
        "reapproval_candidate_chunks": workload["reapproval_candidate_chunks"],
        "baseline_full_review_minutes": workload["baseline_full_review_minutes"],
        "recommended_initial_review_chunks": workload["recommended_initial_review_chunks"],
        "estimated_initial_review_minutes": workload["estimated_initial_review_minutes"],
        "initial_review_reduction_ratio": workload["initial_review_reduction_ratio"],
        "decision_template_row_count": _int(decision_template.get("row_count")),
        "decision_template_operator_decision_complete_count": _int(decision_template.get("complete_row_count")),
        "decision_template_operator_decision_blank_count": _int(
            decision_template.get("operator_decision_blank_count")
        ),
        "workload_summary": workload,
        "tier_initial_review_breakdown": tier_breakdown,
        "batch_summary": batch_summary,
        "decision_template_summary": decision_template,
        "operator_controls": {
            "auto_approval": False,
            "auto_reindex": False,
            "sample_review_is_release_gate": False,
            "operator_decisions_required": workload["reapproval_candidate_chunks"] > 0,
            "escalation_rule": (
                "If sampled medium or low risk chunks fail review, escalate the affected tier, "
                "document, or batch to full manual review before reapproval or reindexing."
            ),
        },
        "safety_note": (
            "This report is read-only. It summarizes initial human-review workload and does not "
            "approve chunks, reprocess files, or write Vector DB records."
        ),
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _workload_summary(worklist: dict[str, Any]) -> dict[str, Any]:
    candidate_chunks = _int(worklist.get("reapproval_candidate_chunks"))
    initial_chunks = _int(worklist.get("recommended_initial_review_chunks"))
    full_minutes = _int(worklist.get("estimated_review_minutes"))
    initial_minutes = _int(worklist.get("estimated_initial_review_minutes"))
    return {
        "document_count": _int(worklist.get("document_count")),
        "total_approved_chunks": _int(worklist.get("total_approved_chunks")),
        "reapproval_candidate_chunks": candidate_chunks,
        "candidate_ratio": _float(worklist.get("reapproval_candidate_ratio")),
        "baseline_full_review_chunks": candidate_chunks,
        "baseline_full_review_minutes": full_minutes,
        "recommended_initial_review_chunks": initial_chunks,
        "estimated_initial_review_minutes": initial_minutes,
        "deferred_after_initial_review_chunks": max(candidate_chunks - initial_chunks, 0),
        "deferred_after_initial_review_minutes": max(full_minutes - initial_minutes, 0),
        "initial_review_reduction_ratio": _float4(
            worklist.get("initial_review_reduction_ratio")
            if worklist.get("initial_review_reduction_ratio") is not None
            else _ratio(max(candidate_chunks - initial_chunks, 0), candidate_chunks)
        ),
        "baseline_full_review_hours": _hours(full_minutes),
        "estimated_initial_review_hours": _hours(initial_minutes),
        "deferred_after_initial_review_hours": _hours(max(full_minutes - initial_minutes, 0)),
        "review_seconds_per_chunk": _int(worklist.get("review_seconds_per_chunk")),
        "low_risk_sample_rate": _float(worklist.get("low_risk_sample_rate")),
        "temporal_sample_rate": _float(worklist.get("temporal_sample_rate")),
        "min_sample_chunks_per_tier": _int(worklist.get("min_sample_chunks_per_tier")),
        "source_vector_integrity_failure_count": _int(worklist.get("source_vector_integrity_failure_count")),
        "pre_reapproval_blocker_count": len(worklist.get("pre_reapproval_blockers") or []),
        "approval_provenance_missing_chunks": _int(worklist.get("approval_provenance_missing_chunks")),
        "approval_provenance_only_chunks": _int(worklist.get("approval_provenance_only_chunks")),
        "approval_provenance_missing_field_counts": _dict(
            worklist.get("approval_provenance_missing_field_counts")
        ),
    }


def _tier_breakdown(worklist: dict[str, Any]) -> dict[str, Any]:
    low_rate = _float(worklist.get("low_risk_sample_rate"))
    temporal_rate = _float(worklist.get("temporal_sample_rate"))
    minimum = _int(worklist.get("min_sample_chunks_per_tier"))
    documents = [item for item in worklist.get("documents") or [] if isinstance(item, dict)]
    tiers = {
        "high": {
            "candidate_chunks": 0,
            "initial_review_chunks": 0,
            "policy": "full_manual_review",
        },
        "medium": {
            "candidate_chunks": 0,
            "initial_review_chunks": 0,
            "sample_rate": temporal_rate,
            "policy": "temporal_metadata_sample_then_operator_reapproval",
        },
        "low": {
            "candidate_chunks": 0,
            "initial_review_chunks": 0,
            "sample_rate": low_rate,
            "policy": "version_only_sample_then_operator_reapproval",
        },
    }
    if documents:
        for document in documents:
            high = _int(document.get("high_risk_candidate_chunks"))
            medium = _int(document.get("temporal_sample_candidate_chunks"))
            low = _int(document.get("low_risk_candidate_chunks"))
            tiers["high"]["candidate_chunks"] += high
            tiers["high"]["initial_review_chunks"] += high
            tiers["medium"]["candidate_chunks"] += medium
            tiers["medium"]["initial_review_chunks"] += _sample_count(medium, rate=temporal_rate, minimum=minimum)
            tiers["low"]["candidate_chunks"] += low
            tiers["low"]["initial_review_chunks"] += _sample_count(low, rate=low_rate, minimum=minimum)
    else:
        counts = _dict(worklist.get("review_triage_counts"))
        high = _int(counts.get("high") or worklist.get("high_risk_candidate_chunks"))
        medium = _int(counts.get("medium") or worklist.get("temporal_sample_candidate_chunks"))
        low = _int(counts.get("low") or worklist.get("low_risk_candidate_chunks"))
        tiers["high"]["candidate_chunks"] = high
        tiers["high"]["initial_review_chunks"] = high
        tiers["medium"]["candidate_chunks"] = medium
        tiers["medium"]["initial_review_chunks"] = _sample_count(medium, rate=temporal_rate, minimum=minimum)
        tiers["low"]["candidate_chunks"] = low
        tiers["low"]["initial_review_chunks"] = _sample_count(low, rate=low_rate, minimum=minimum)
    reported_initial = _int(worklist.get("recommended_initial_review_chunks"))
    computed_initial = sum(_int(value.get("initial_review_chunks")) for value in tiers.values())
    return {
        "tiers": tiers,
        "computed_initial_review_chunks": computed_initial,
        "reported_initial_review_chunks": reported_initial,
        "matches_reported_initial_review_chunks": computed_initial == reported_initial,
    }


def _batch_summary(batch: dict[str, Any]) -> dict[str, Any]:
    if not batch:
        return {}
    return {
        "report_type": str(batch.get("report_type") or ""),
        "passed": bool(batch.get("passed")),
        "candidate_count": _int(batch.get("candidate_count")),
        "selected_candidate_count": _int(batch.get("selected_candidate_count")),
        "batch_count": _int(batch.get("batch_count")),
        "reapproval_chunk_count": _int(batch.get("reapproval_chunk_count")),
        "max_chunks_per_batch": _int(batch.get("max_chunks_per_batch")),
        "blocker_count": _int(batch.get("blocker_count")),
        "warning_count": _int(batch.get("warning_count")),
        "risk_tier_chunk_counts": _dict(batch.get("risk_tier_chunk_counts")),
        "action_chunk_counts": _dict(batch.get("action_chunk_counts")),
        "decision_template": _dict(batch.get("decision_template")),
    }


def _decision_template_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    summary: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "row_count": 0,
        "fieldnames": [],
        "operator_decision_blank_count": 0,
        "reviewer_id_blank_count": 0,
        "reviewed_at_blank_count": 0,
        "approval_scope_confirmation_blank_count": 0,
        "invalid_operator_decision_count": 0,
        "invalid_operator_decisions": [],
        "complete_row_count": 0,
        "allowed_operator_decisions": list(REAPPROVAL_DECISION_OPERATOR_DECISIONS),
    }
    if not path.is_file():
        return summary
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        summary["fieldnames"] = list(reader.fieldnames or [])
        invalid_decisions: set[str] = set()
        for row in reader:
            summary["row_count"] += 1
            decision = normalize_operator_decision(row.get("operator_decision"))
            missing_required = row_missing_required_decision_fields(row)
            if "operator_decision" in missing_required:
                summary["operator_decision_blank_count"] += 1
            elif not is_allowed_operator_decision(decision):
                summary["invalid_operator_decision_count"] += 1
                invalid_decisions.add(decision)
            if "reviewer_id" in missing_required:
                summary["reviewer_id_blank_count"] += 1
            if "reviewed_at" in missing_required:
                summary["reviewed_at_blank_count"] += 1
            if "approval_scope_confirmation" in missing_required:
                summary["approval_scope_confirmation_blank_count"] += 1
            if not missing_required and is_allowed_operator_decision(decision):
                summary["complete_row_count"] += 1
        summary["invalid_operator_decisions"] = sorted(invalid_decisions)
    return summary


def _findings(
    workload: dict[str, Any],
    tier_breakdown: dict[str, Any],
    batch_summary: dict[str, Any],
    worklist: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if workload["source_vector_integrity_failure_count"] > 0 or workload["pre_reapproval_blocker_count"] > 0:
        findings.append(
            {
                "severity": "blocker",
                "code": "pre-reapproval-blockers-present",
                "detail": "Resolve vector integrity or runtime blockers before reapproval planning.",
                "source_vector_integrity_failure_count": workload["source_vector_integrity_failure_count"],
                "pre_reapproval_blocker_count": workload["pre_reapproval_blocker_count"],
            }
        )
    if workload["recommended_initial_review_chunks"] > workload["reapproval_candidate_chunks"]:
        findings.append(
            {
                "severity": "blocker",
                "code": "initial-review-exceeds-candidates",
                "recommended_initial_review_chunks": workload["recommended_initial_review_chunks"],
                "reapproval_candidate_chunks": workload["reapproval_candidate_chunks"],
            }
        )
    if not tier_breakdown["matches_reported_initial_review_chunks"]:
        findings.append(
            {
                "severity": "warning",
                "code": "tier-breakdown-initial-review-mismatch",
                "computed_initial_review_chunks": tier_breakdown["computed_initial_review_chunks"],
                "reported_initial_review_chunks": tier_breakdown["reported_initial_review_chunks"],
            }
        )
    if batch_summary:
        candidates = workload["reapproval_candidate_chunks"]
        mismatched = {
            key: batch_summary.get(key)
            for key in ("candidate_count", "selected_candidate_count", "reapproval_chunk_count")
            if _int(batch_summary.get(key)) != candidates
        }
        if mismatched:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "reapproval-batch-coverage-mismatch",
                    "reapproval_candidate_chunks": candidates,
                    "mismatched_fields": mismatched,
                }
            )
        if batch_summary["blocker_count"] > 0:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "reapproval-batch-blockers-present",
                    "blocker_count": batch_summary["blocker_count"],
                }
            )
    elif workload["reapproval_candidate_chunks"] > 0:
        findings.append(
            {
                "severity": "warning",
                "code": "reapproval-batch-manifest-not-provided",
                "detail": "Attach the batch manifest before operator handoff.",
            }
        )
    if _int(worklist.get("approval_provenance_missing_chunks")) > 0:
        findings.append(
            {
                "severity": "warning",
                "code": "approval-provenance-evidence-requires-operator-decisions",
                "approval_provenance_missing_chunks": _int(worklist.get("approval_provenance_missing_chunks")),
                "approval_provenance_only_chunks": _int(worklist.get("approval_provenance_only_chunks")),
            }
        )
    return findings


def _release_gate_blockers(
    workload: dict[str, Any],
    decision_template: dict[str, Any],
    decision_template_requested: bool,
) -> list[dict[str, Any]]:
    if workload["reapproval_candidate_chunks"] <= 0:
        return []
    if not decision_template_requested:
        return [
            {
                "code": "reapproval-decision-template-not-provided",
                "severity": "blocker",
                "detail": "Reapproval candidates require a reviewed decision template before private release.",
            }
        ]
    if not decision_template.get("exists"):
        return [
            {
                "code": "reapproval-decision-template-missing",
                "severity": "blocker",
                "path": decision_template.get("path"),
            }
        ]
    row_count = _int(decision_template.get("row_count"))
    blank_count = _int(decision_template.get("operator_decision_blank_count"))
    invalid_count = _int(decision_template.get("invalid_operator_decision_count"))
    reviewer_blank_count = _int(decision_template.get("reviewer_id_blank_count"))
    reviewed_at_blank_count = _int(decision_template.get("reviewed_at_blank_count"))
    scope_blank_count = _int(decision_template.get("approval_scope_confirmation_blank_count"))
    if row_count <= 0:
        return [
            {
                "code": "reapproval-decision-template-empty",
                "severity": "blocker",
                "path": decision_template.get("path"),
            }
        ]
    blockers: list[dict[str, Any]] = []
    if blank_count > 0:
        blockers.append(
            {
                "code": "reapproval-decisions-not-complete",
                "severity": "blocker",
                "row_count": row_count,
                "operator_decision_blank_count": blank_count,
                "detail": "Blank operator decisions are not approvals and must block private release.",
            }
        )
    if invalid_count > 0:
        blockers.append(
            {
                "code": "reapproval-decisions-invalid",
                "severity": "blocker",
                "row_count": row_count,
                "invalid_operator_decision_count": invalid_count,
                "invalid_operator_decisions": decision_template.get("invalid_operator_decisions") or [],
                "allowed_operator_decisions": decision_template.get("allowed_operator_decisions") or [],
                "detail": "Operator decisions must match the decision template contract.",
            }
        )
    if reviewer_blank_count > 0 or reviewed_at_blank_count > 0 or scope_blank_count > 0:
        blockers.append(
            {
                "code": "reapproval-decision-evidence-incomplete",
                "severity": "blocker",
                "row_count": row_count,
                "reviewer_id_blank_count": reviewer_blank_count,
                "reviewed_at_blank_count": reviewed_at_blank_count,
                "approval_scope_confirmation_blank_count": scope_blank_count,
                "detail": "Completed reapproval decisions require reviewer, review timestamp, and approval scope confirmation.",
            }
        )
    return blockers


def _source_artifact(role: str, path: Path | None, *, expected_report_type: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "role": role,
        "path": str(path) if path else None,
        "exists": bool(path and path.is_file()),
        "byte_count": None,
        "sha256": None,
    }
    if not path or not path.is_file():
        return item
    data = path.read_bytes()
    item["byte_count"] = len(data)
    item["sha256"] = hashlib.sha256(data).hexdigest()
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            item["parse_error"] = "invalid_json"
        else:
            if isinstance(payload, dict):
                item["report_type"] = payload.get("report_type")
                item["generated_at"] = payload.get("generated_at")
                item["repo_commit"] = payload.get("repo_commit")
                item["passed"] = payload.get("passed")
                if expected_report_type and payload.get("report_type") != expected_report_type:
                    item["expected_report_type"] = expected_report_type
    return item


def _status(blocker_count: int, candidate_chunks: int) -> str:
    if blocker_count:
        return "blocked"
    if candidate_chunks:
        return "ready_for_operator_review"
    return "no_reapproval_candidates"


def _to_markdown(report: dict[str, Any]) -> str:
    workload = report["workload_summary"]
    tiers = report["tier_initial_review_breakdown"]["tiers"]
    lines = [
        "# Reapproval Review Burden",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Status: `{report.get('status')}`",
        f"- Release gate status: `{report.get('release_gate_status')}`",
        f"- Passed: `{report.get('passed')}`",
        f"- Candidate chunks: `{workload.get('reapproval_candidate_chunks')}`",
        f"- Baseline full review: `{workload.get('baseline_full_review_chunks')}` chunks / `{workload.get('baseline_full_review_minutes')}` minutes",
        f"- Recommended initial review: `{workload.get('recommended_initial_review_chunks')}` chunks / `{workload.get('estimated_initial_review_minutes')}` minutes",
        f"- Deferred after initial review: `{workload.get('deferred_after_initial_review_chunks')}` chunks / `{workload.get('deferred_after_initial_review_minutes')}` minutes",
        f"- Initial review reduction ratio: `{workload.get('initial_review_reduction_ratio')}`",
        f"- Approval provenance missing chunks: `{workload.get('approval_provenance_missing_chunks')}`",
        "",
        f"Safety: {report.get('safety_note')}",
        "",
        "## Tier Breakdown",
        "",
        "| Tier | Candidates | Initial review | Policy |",
        "| --- | ---: | ---: | --- |",
    ]
    for tier in ("high", "medium", "low"):
        item = tiers.get(tier) or {}
        lines.append(
            "| {tier} | {candidates} | {initial} | {policy} |".format(
                tier=tier,
                candidates=item.get("candidate_chunks"),
                initial=item.get("initial_review_chunks"),
                policy=_md_cell(item.get("policy")),
            )
        )
    lines.extend(["", "## Findings", ""])
    findings = report.get("findings") or []
    if not findings:
        lines.append("- None")
    for finding in findings:
        lines.append(f"- `{finding.get('severity')}` `{finding.get('code')}`")
    lines.extend(["", "## Release Blockers", ""])
    release_blockers = report.get("release_blockers") or []
    if not release_blockers:
        lines.append("- None")
    for blocker in release_blockers:
        lines.append(f"- `{blocker.get('severity')}` `{blocker.get('code')}`")
    lines.extend(
        [
            "",
            "## Operator Controls",
            "",
            f"- Auto approval: `{report['operator_controls']['auto_approval']}`",
            f"- Auto reindex: `{report['operator_controls']['auto_reindex']}`",
            f"- Operator decisions required: `{report['operator_controls']['operator_decisions_required']}`",
            f"- Escalation rule: {report['operator_controls']['escalation_rule']}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _sample_count(count: int, *, rate: float, minimum: int) -> int:
    if count <= 0:
        return 0
    bounded_rate = min(max(rate, 0.0), 1.0)
    target = int(math.ceil(count * bounded_rate))
    return min(count, max(max(minimum, 1), target))


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _hours(minutes: int) -> float:
    return round(minutes / 60, 2) if minutes else 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _float4(value: Any) -> float:
    return round(_float(value), 4)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a read-only report explaining reapproval human-review burden and sampling controls."
    )
    parser.add_argument("--reapproval-worklist-report", type=Path, required=True)
    parser.add_argument("--reapproval-review-batch-report", type=Path)
    parser.add_argument("--decision-template-csv", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_reapproval_review_burden_report(
        reapproval_worklist_report=args.reapproval_worklist_report,
        reapproval_review_batch_report=args.reapproval_review_batch_report,
        decision_template_csv=args.decision_template_csv,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
