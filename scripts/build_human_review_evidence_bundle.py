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

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from scripts.build_approval_review_batches import (
    DEFAULT_MAX_CHUNKS_PER_BATCH as DEFAULT_APPROVAL_MAX_CHUNKS_PER_BATCH,
    REVIEW_TYPES,
    _apply_review_batch_manifest_evidence,
    build_approval_review_batches,
    write_csv as write_approval_batches_csv,
    write_markdown as write_approval_batches_markdown,
)
from scripts.build_approval_worklist import (
    build_approval_worklist,
    write_csv as write_approval_worklist_csv,
    write_markdown as write_approval_worklist_markdown,
)
from scripts.build_reapproval_evidence_bundle import build_reapproval_evidence_bundle
from scripts.build_reapproval_review_batches import (
    DEFAULT_MAX_CHUNKS_PER_BATCH as DEFAULT_REAPPROVAL_MAX_CHUNKS_PER_BATCH,
    SUPPORTED_REVIEW_TIERS,
)
from scripts.report_metadata import current_repo_commit


def build_human_review_evidence_bundle(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool | None = None,
    reports_dir: Path = Path("reports"),
    label: str = "current",
    source_system: str | None = None,
    apba_id: str | None = None,
    approval_max_chunks_per_batch: int = DEFAULT_APPROVAL_MAX_CHUNKS_PER_BATCH,
    approval_include_review_types: Sequence[str] = REVIEW_TYPES,
    default_security_level: str = "internal",
    runtime_version_drift_report: Path | None = None,
    runtime_drift_sample_limit: int = 25,
    reapproval_review_batch_size: int = 100,
    reapproval_review_seconds_per_chunk: int = 20,
    low_risk_sample_rate: float = 0.05,
    temporal_sample_rate: float = 0.15,
    min_sample_chunks_per_tier: int = 10,
    reapproval_chunk_sample_limit: int = 10,
    reapproval_max_chunks_per_batch: int = DEFAULT_REAPPROVAL_MAX_CHUNKS_PER_BATCH,
    reapproval_include_review_tiers: Sequence[str] = SUPPORTED_REVIEW_TIERS,
    reapproval_include_suggested_actions: Sequence[str] | None = None,
    fail_on_release_gate: bool = False,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    label = _safe_label(label)
    approval_paths = _approval_paths(reports_dir, label)

    approval_worklist = build_approval_worklist(
        data_dir=data_dir,
        source_system=source_system,
        apba_id=apba_id,
        tenant_id=tenant_id,
        tenant_storage_isolation=bool(tenant_storage_isolation),
    )
    _write_json(approval_paths["approval_worklist_json"], approval_worklist)
    write_approval_worklist_csv(approval_paths["approval_worklist_csv"], approval_worklist["documents"])
    write_approval_worklist_markdown(approval_worklist, approval_paths["approval_worklist_markdown"])

    approval_batches = build_approval_review_batches(
        data_dir=data_dir,
        worklist_report=approval_paths["approval_worklist_json"],
        worklist_report_artifact_path=_safe_relative_artifact_path(approval_paths["approval_worklist_json"]),
        tenant_id=tenant_id,
        tenant_storage_isolation=bool(tenant_storage_isolation),
        max_chunks_per_batch=approval_max_chunks_per_batch,
        include_review_types=approval_include_review_types,
        default_security_level=default_security_level,
    )
    _apply_review_batch_manifest_evidence(
        approval_batches,
        _safe_relative_artifact_path(approval_paths["approval_review_batches_json"]),
    )
    _write_json(approval_paths["approval_review_batches_json"], approval_batches)
    write_approval_batches_csv(approval_paths["approval_review_batches_csv"], approval_batches["batches"])
    write_approval_batches_markdown(approval_batches, approval_paths["approval_review_batches_markdown"])

    reapproval_bundle = build_reapproval_evidence_bundle(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        reports_dir=reports_dir,
        label=label,
        source_system=source_system,
        apba_id=apba_id,
        runtime_version_drift_report=runtime_version_drift_report,
        sample_limit=runtime_drift_sample_limit,
        review_batch_size=reapproval_review_batch_size,
        review_seconds_per_chunk=reapproval_review_seconds_per_chunk,
        low_risk_sample_rate=low_risk_sample_rate,
        temporal_sample_rate=temporal_sample_rate,
        min_sample_chunks_per_tier=min_sample_chunks_per_tier,
        chunk_sample_limit=reapproval_chunk_sample_limit,
        max_chunks_per_batch=reapproval_max_chunks_per_batch,
        include_review_tiers=reapproval_include_review_tiers,
        include_suggested_actions=reapproval_include_suggested_actions,
        fail_on_release_gate=fail_on_release_gate,
    )

    technical_blockers = _technical_blockers(
        approval_batches=approval_batches,
        reapproval_bundle=reapproval_bundle,
    )
    bundle_json = out_json or reports_dir / f"human_review_evidence_bundle_{label}.json"
    bundle_md = out_md or reports_dir / f"human_review_evidence_bundle_{label}.md"
    report = {
        "report_type": "human_review_evidence_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": not technical_blockers,
        "status": _status(
            technical_blockers=technical_blockers,
            approval_chunk_count=_int(approval_batches.get("approval_chunk_count")),
            reapproval_candidate_chunks=_int((reapproval_bundle.get("summary") or {}).get("reapproval_candidate_chunks")),
        ),
        "technical_blocker_count": len(technical_blockers),
        "technical_blockers": technical_blockers,
        "release_gate_status": reapproval_bundle.get("release_gate_status"),
        "release_blocker_count": reapproval_bundle.get("release_blocker_count"),
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
            "approval_document_count": approval_worklist.get("document_count"),
            "approval_total_chunks": approval_worklist.get("total_chunks"),
            "approval_chunk_count": approval_batches.get("approval_chunk_count"),
            "approval_batch_count": approval_batches.get("batch_count"),
            "manual_attention_chunks": approval_batches.get("manual_attention_chunks"),
            "low_risk_batch_review_candidate_chunks": approval_batches.get(
                "low_risk_batch_review_candidate_chunks"
            ),
            "reapproval_candidate_chunks": (reapproval_bundle.get("summary") or {}).get(
                "reapproval_candidate_chunks"
            ),
            "reapproval_review_batch_count": (reapproval_bundle.get("summary") or {}).get(
                "review_batch_count"
            ),
            "reapproval_decision_template_blank_count": (reapproval_bundle.get("summary") or {}).get(
                "decision_template_operator_decision_blank_count"
            ),
            "reapproval_decision_validation_status": (reapproval_bundle.get("summary") or {}).get(
                "decision_validation_status"
            ),
            "reapproval_decision_validation_blocking_count": (reapproval_bundle.get("summary") or {}).get(
                "decision_validation_blocking_count"
            ),
            "runtime_version_drift_generated": (reapproval_bundle.get("summary") or {}).get(
                "runtime_version_drift_generated"
            ),
        },
        "artifacts": {
            **_artifact_summary(approval_paths),
            "reapproval_evidence_bundle_json": _artifact_path_from_bundle(
                reapproval_bundle,
                "bundle_json",
            ),
            "reapproval_evidence_bundle_markdown": _artifact_path_from_bundle(
                reapproval_bundle,
                "bundle_markdown",
            ),
            "human_review_bundle_json": {"path": str(bundle_json), "exists": None, "sha256": None, "byte_count": None},
            "human_review_bundle_markdown": {"path": str(bundle_md), "exists": None, "sha256": None, "byte_count": None},
        },
        "approval_journal_contract": _approval_journal_contract(
            data_dir=data_dir,
            tenant_id=tenant_id,
            tenant_storage_isolation=bool(tenant_storage_isolation),
        ),
        "product_readiness_inputs": {
            "approval_worklist_report": str(approval_paths["approval_worklist_json"]),
            "approval_review_batch_report": str(approval_paths["approval_review_batches_json"]),
            "reapproval_worklist_report": (reapproval_bundle.get("product_readiness_inputs") or {}).get(
                "reapproval_worklist_report"
            ),
            "reapproval_review_batch_report": (reapproval_bundle.get("product_readiness_inputs") or {}).get(
                "reapproval_review_batch_report"
            ),
            "reapproval_decision_validation_report": (
                reapproval_bundle.get("product_readiness_inputs") or {}
            ).get("reapproval_decision_validation_report"),
            "runtime_version_drift_report": (reapproval_bundle.get("product_readiness_inputs") or {}).get(
                "runtime_version_drift_report"
            ),
        },
        "next_steps": _next_steps(
            approval_chunk_count=_int(approval_batches.get("approval_chunk_count")),
            reapproval_candidate_chunks=_int((reapproval_bundle.get("summary") or {}).get("reapproval_candidate_chunks")),
            release_gate_status=str(reapproval_bundle.get("release_gate_status") or ""),
        ),
        "safety_note": (
            "This bundle prepares human review evidence only. It does not approve chunks, reapprove chunks, "
            "write append-only approval journal records, write approved Vector DB records, reindex, or publish MCP "
            "artifacts."
        ),
        "api_call_count": 0,
    }
    _write_json(bundle_json, report)
    bundle_md.parent.mkdir(parents=True, exist_ok=True)
    bundle_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _approval_paths(reports_dir: Path, label: str) -> dict[str, Path]:
    return {
        "approval_worklist_json": reports_dir / f"approval_worklist_{label}.json",
        "approval_worklist_csv": reports_dir / f"approval_worklist_{label}.csv",
        "approval_worklist_markdown": reports_dir / f"approval_worklist_{label}.md",
        "approval_review_batches_json": reports_dir / f"approval_review_batches_{label}.json",
        "approval_review_batches_csv": reports_dir / f"approval_review_batches_{label}.csv",
        "approval_review_batches_markdown": reports_dir / f"approval_review_batches_{label}.md",
    }


def _technical_blockers(
    *,
    approval_batches: dict[str, Any],
    reapproval_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if int(approval_batches.get("blocker_count") or 0) > 0:
        blockers.append(
            {
                "code": "approval-review-batch-blockers-present",
                "detail": "Approval review batch manifest has blockers.",
                "blocker_count": approval_batches.get("blocker_count"),
            }
        )
    for blocker in reapproval_bundle.get("technical_blockers") or []:
        if isinstance(blocker, dict):
            blockers.append(dict(blocker))
    return blockers


def _status(*, technical_blockers: Sequence[dict[str, Any]], approval_chunk_count: int, reapproval_candidate_chunks: int) -> str:
    if technical_blockers:
        return "blocked"
    if approval_chunk_count or reapproval_candidate_chunks:
        return "ready_for_human_review"
    return "no_human_review_candidates"


def _artifact_summary(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    return {key: _artifact(path) for key, path in paths.items()}


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


def _artifact_path_from_bundle(bundle: dict[str, Any], key: str) -> dict[str, Any]:
    artifacts = bundle.get("artifacts") if isinstance(bundle.get("artifacts"), dict) else {}
    item = artifacts.get(key) if isinstance(artifacts.get(key), dict) else {}
    path = item.get("path") if isinstance(item, dict) else None
    return _artifact(Path(path)) if path else {"path": None, "exists": False, "sha256": None, "byte_count": None}


def _approval_journal_contract(
    *,
    data_dir: Path,
    tenant_id: str,
    tenant_storage_isolation: bool,
) -> dict[str, Any]:
    effective_settings = settings_for_tenant(
        Settings(data_dir=Path(data_dir), tenant_storage_isolation=tenant_storage_isolation),
        tenant_id,
    )
    return {
        "required_for_official_rag_mcp": True,
        "bundle_writes_journal_records": False,
        "created_by": "operator_approval_ui_or_api",
        "journal_path": str(effective_settings.data_dir / "repository" / "journals" / "approvals.jsonl"),
        "vector_indexing_requirement": (
            "Official RAG/MCP indexing requires matching append-only approval journal records and matching "
            "worklist/review-batch evidence for every approved vector record."
        ),
    }


def _next_steps(*, approval_chunk_count: int, reapproval_candidate_chunks: int, release_gate_status: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if approval_chunk_count:
        steps.append(
            {
                "step": "initial_human_approval",
                "detail": (
                    "Inspect approval review batches, acknowledge parser/table flags where required, then approve "
                    "reviewed chunks through the operator UI or API so append-only approval journal records are written."
                ),
            }
        )
    if reapproval_candidate_chunks:
        steps.append(
            {
                "step": "reapproval_operator_decisions",
                "detail": (
                    "Complete the reapproval decision template, rerun decision validation, then run "
                    "reg-rag-reapproval-apply-plan before any dedicated reapproval apply or reindex step."
                ),
            }
        )
    if release_gate_status == "blocked_pending_operator_decisions":
        steps.append(
            {
                "step": "release_gate_blocked",
                "detail": "Blank reapproval decision rows are not approvals. Official RAG/MCP release remains blocked.",
            }
        )
    if not steps:
        steps.append(
            {
                "step": "attach_empty_evidence",
                "detail": "Attach the generated empty worklists to product readiness as evidence that no review candidates were found.",
            }
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
        "# Human Review Evidence Bundle",
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
        f"- Approval chunks / batches: `{summary.get('approval_chunk_count')}` / `{summary.get('approval_batch_count')}`",
        f"- Manual attention chunks: `{summary.get('manual_attention_chunks')}`",
        f"- Low-risk batch candidates: `{summary.get('low_risk_batch_review_candidate_chunks')}`",
        f"- Reapproval candidate chunks / batches: `{summary.get('reapproval_candidate_chunks')}` / `{summary.get('reapproval_review_batch_count')}`",
        f"- Reapproval decision blanks: `{summary.get('reapproval_decision_template_blank_count')}`",
        f"- Reapproval decision validation status: `{summary.get('reapproval_decision_validation_status')}`",
        "",
    ]
    blockers = [item for item in report.get("technical_blockers") or [] if isinstance(item, dict)]
    if blockers:
        lines.extend(["## Technical Blockers", ""])
        for blocker in blockers:
            lines.append(f"- `{blocker.get('code')}`: {blocker.get('detail')}")
        lines.append("")
    contract = report.get("approval_journal_contract") if isinstance(report.get("approval_journal_contract"), dict) else {}
    lines.extend(
        [
            "## Approval Journal Contract",
            "",
            f"- Required for official RAG/MCP: `{str(contract.get('required_for_official_rag_mcp')).lower()}`",
            f"- Bundle writes journal records: `{str(contract.get('bundle_writes_journal_records')).lower()}`",
            f"- Journal path: `{contract.get('journal_path')}`",
            f"- Requirement: {contract.get('vector_indexing_requirement')}",
            "",
        ]
    )
    lines.extend(["## Product Readiness Inputs", ""])
    inputs = report.get("product_readiness_inputs") if isinstance(report.get("product_readiness_inputs"), dict) else {}
    for key, value in inputs.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Safety", "", str(report.get("safety_note") or "")])
    return "\n".join(lines).rstrip() + "\n"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build read-only human review evidence for approval and reapproval in one pass."
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
    parser.add_argument("--approval-max-chunks-per-batch", type=int, default=DEFAULT_APPROVAL_MAX_CHUNKS_PER_BATCH)
    parser.add_argument("--approval-include-review-type", action="append", choices=REVIEW_TYPES)
    parser.add_argument("--default-security-level", default="internal")
    parser.add_argument("--runtime-version-drift-report", type=Path)
    parser.add_argument("--runtime-drift-sample-limit", type=int, default=25)
    parser.add_argument("--reapproval-review-batch-size", type=int, default=100)
    parser.add_argument("--reapproval-review-seconds-per-chunk", type=int, default=20)
    parser.add_argument("--low-risk-sample-rate", type=float, default=0.05)
    parser.add_argument("--temporal-sample-rate", type=float, default=0.15)
    parser.add_argument("--min-sample-chunks-per-tier", type=int, default=10)
    parser.add_argument("--reapproval-chunk-sample-limit", type=int, default=10)
    parser.add_argument("--reapproval-max-chunks-per-batch", type=int, default=DEFAULT_REAPPROVAL_MAX_CHUNKS_PER_BATCH)
    parser.add_argument("--reapproval-include-review-tier", action="append", default=[])
    parser.add_argument("--reapproval-include-suggested-action", action="append", default=[])
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
    report = build_human_review_evidence_bundle(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        reports_dir=args.reports_dir,
        label=args.label,
        source_system=args.source_system,
        apba_id=args.apba_id,
        approval_max_chunks_per_batch=args.approval_max_chunks_per_batch,
        approval_include_review_types=args.approval_include_review_type or REVIEW_TYPES,
        default_security_level=args.default_security_level,
        runtime_version_drift_report=args.runtime_version_drift_report,
        runtime_drift_sample_limit=args.runtime_drift_sample_limit,
        reapproval_review_batch_size=args.reapproval_review_batch_size,
        reapproval_review_seconds_per_chunk=args.reapproval_review_seconds_per_chunk,
        low_risk_sample_rate=args.low_risk_sample_rate,
        temporal_sample_rate=args.temporal_sample_rate,
        min_sample_chunks_per_tier=args.min_sample_chunks_per_tier,
        reapproval_chunk_sample_limit=args.reapproval_chunk_sample_limit,
        reapproval_max_chunks_per_batch=args.reapproval_max_chunks_per_batch,
        reapproval_include_review_tiers=args.reapproval_include_review_tier or SUPPORTED_REVIEW_TIERS,
        reapproval_include_suggested_actions=args.reapproval_include_suggested_action,
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
