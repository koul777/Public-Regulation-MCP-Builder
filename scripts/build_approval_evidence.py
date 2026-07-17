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


from scripts.build_approval_review_batches import (
    DEFAULT_MAX_CHUNKS_PER_BATCH,
    REVIEW_TYPES,
    _apply_review_batch_manifest_evidence,
    build_approval_review_batches,
    write_csv as write_review_batches_csv,
    write_markdown as write_review_batches_markdown,
)
from scripts.build_approval_worklist import (
    build_approval_worklist,
    write_csv as write_worklist_csv,
    write_markdown as write_worklist_markdown,
)
from scripts.report_metadata import current_repo_commit


def build_approval_evidence(
    *,
    data_dir: str | Path,
    tenant_id: str = "default",
    tenant_storage_isolation: bool = False,
    source_system: str | None = None,
    apba_id: str | None = None,
    out_prefix: str | Path = "reports/approval_evidence_current",
    max_chunks_per_batch: int = DEFAULT_MAX_CHUNKS_PER_BATCH,
    include_review_types: Sequence[str] = REVIEW_TYPES,
    default_security_level: str = "internal",
) -> dict[str, Any]:
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    worklist_json = prefix.with_name(prefix.name + "_worklist.json")
    worklist_csv = prefix.with_name(prefix.name + "_worklist.csv")
    worklist_md = prefix.with_name(prefix.name + "_worklist.md")
    batches_json = prefix.with_name(prefix.name + "_review_batches.json")
    batches_csv = prefix.with_name(prefix.name + "_review_batches.csv")
    batches_md = prefix.with_name(prefix.name + "_review_batches.md")

    worklist = build_approval_worklist(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        source_system=source_system,
        apba_id=apba_id,
    )
    _write_json(worklist_json, worklist)
    write_worklist_csv(worklist_csv, worklist["documents"])
    write_worklist_markdown(worklist, worklist_md)
    worklist_sha256 = _sha256_file(worklist_json)
    worklist_artifact_path = _safe_relative_artifact_path(worklist_json)

    batches = build_approval_review_batches(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
        worklist_report=worklist_json,
        worklist_report_artifact_path=worklist_artifact_path,
        max_chunks_per_batch=max_chunks_per_batch,
        include_review_types=include_review_types,
        default_security_level=default_security_level,
    )
    _apply_review_batch_manifest_evidence(batches, _safe_relative_artifact_path(batches_json))
    _write_json(batches_json, batches)
    write_review_batches_csv(batches_csv, batches["batches"])
    write_review_batches_markdown(batches, batches_md)
    batches_sha256 = _sha256_file(batches_json)

    next_steps = _next_steps(
        batches,
        worklist_json=worklist_json,
        worklist_sha256=worklist_sha256,
        batches_json=batches_json,
        batches_sha256=batches_sha256,
    )
    blocker_count = int(batches.get("blocker_count") or 0)
    warning_count = int(batches.get("warning_count") or 0)
    return {
        "report_type": "approval_evidence_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(PROJECT_ROOT),
        "passed": blocker_count == 0,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "data_dir": str(Path(data_dir)),
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
        "worklist_summary": {
            "document_count": worklist.get("document_count", 0),
            "total_chunks": worklist.get("total_chunks", 0),
            "manual_attention_chunks": worklist.get("manual_attention_chunks", 0),
            "low_risk_batch_review_candidate_chunks": worklist.get(
                "low_risk_batch_review_candidate_chunks",
                0,
            ),
        },
        "review_batch_summary": {
            "batch_count": batches.get("batch_count", 0),
            "approval_chunk_count": batches.get("approval_chunk_count", 0),
            "manual_attention_chunks": batches.get("manual_attention_chunks", 0),
            "low_risk_batch_review_candidate_chunks": batches.get(
                "low_risk_batch_review_candidate_chunks",
                0,
            ),
            "include_review_types": batches.get("include_review_types", []),
        },
        "artifacts": {
            "worklist_json": str(worklist_json),
            "worklist_csv": str(worklist_csv),
            "worklist_markdown": str(worklist_md),
            "worklist_sha256": worklist_sha256,
            "review_batches_json": str(batches_json),
            "review_batches_csv": str(batches_csv),
            "review_batches_markdown": str(batches_md),
            "review_batches_sha256": batches_sha256,
        },
        "next_steps": next_steps,
        "findings": batches.get("findings", []),
        "safety_note": (
            "This command prepares human approval evidence only. It does not approve chunks, "
            "set review_flags_acknowledged, index Vector DB records, or publish MCP connection artifacts."
        ),
        "api_call_count": 0,
    }


def _next_steps(
    batches: dict[str, Any],
    *,
    worklist_json: Path,
    worklist_sha256: str,
    batches_json: Path,
    batches_sha256: str,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for batch in batches.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        steps.append(
            {
                "review_batch_id": batch.get("review_batch_id", ""),
                "document_id": batch.get("document_id", ""),
                "document_name": batch.get("document_name", ""),
                "review_type": batch.get("review_type", ""),
                "review_strategy": batch.get("review_strategy", ""),
                "chunk_count": batch.get("chunk_count", 0),
                "review_flags_acknowledged_required": bool(
                    batch.get("review_flags_acknowledged_required")
                ),
                "worklist_report_path": _safe_relative_artifact_path(worklist_json),
                "worklist_report_sha256": worklist_sha256,
                "review_batch_manifest_path": _safe_relative_artifact_path(batches_json),
                "review_batch_manifest_sha256": batches_sha256,
                "review_batch_chunk_fingerprint": batch.get("review_batch_chunk_fingerprint", ""),
                "streamlit_action": (
                    "Load this manifest in Approval worklist evidence, inspect the batch, "
                    "manually acknowledge parser/table flags if required, then approve the selected batch."
                ),
            }
        )
    return steps


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_relative_artifact_path(path: Path) -> str:
    text = str(path).replace("\\", "/")
    if text and not path.is_absolute() and ".." not in text.split("/") and not text.startswith("/"):
        return text
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build read-only human approval evidence worklist and review-batch manifest in one pass."
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--source-system")
    parser.add_argument("--apba-id")
    parser.add_argument("--out-prefix", default="reports/approval_evidence_current")
    parser.add_argument("--max-chunks-per-batch", type=int, default=DEFAULT_MAX_CHUNKS_PER_BATCH)
    parser.add_argument(
        "--include-review-type",
        action="append",
        choices=REVIEW_TYPES,
        help="Review type to include. Repeat to override the default of both manual_attention and low_risk_batch.",
    )
    parser.add_argument("--default-security-level", default="internal")
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    report = build_approval_evidence(
        data_dir=args.data_dir,
        tenant_id=args.tenant_id,
        tenant_storage_isolation=args.tenant_storage_isolation,
        source_system=args.source_system,
        apba_id=args.apba_id,
        out_prefix=args.out_prefix,
        max_chunks_per_batch=args.max_chunks_per_batch,
        include_review_types=args.include_review_type or REVIEW_TYPES,
        default_security_level=args.default_security_level,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout or sys.stdout)
    if args.fail_on_issue and not report["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
