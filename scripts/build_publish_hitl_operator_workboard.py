"""Build a publish-readiness human-in-the-loop operator workboard.

This report intentionally stays read-only. It consolidates existing evidence
reports into a single queue view for release owners and reviewers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _short_list(values: list[Any], limit: int = 5) -> list[Any]:
    return values[:limit]


def _repo_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


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


def _table_remediation_counts(remediation: dict[str, Any]) -> dict[str, Any]:
    for item in _list(remediation.get("remediation_items")):
        if _dict(item).get("item_id") == "table_preprocessing_human_review":
            return _dict(_dict(item).get("source_counts"))
    return {}


def _queue_status(open_count: int, *, blocked_by_owner: bool = False) -> str:
    if blocked_by_owner and open_count > 0:
        return "owner_decision_required"
    if open_count > 0:
        return "open"
    return "clear"


def build_publish_hitl_operator_workboard(
    *,
    parser_start_report: Path,
    table_review_summary_report: Path,
    github_publish_summary_report: Path,
    remediation_plan_report: Path | None = None,
    parser_review_batches_report: Path | None = None,
    table_review_batches_report: Path | None = None,
) -> dict[str, Any]:
    parser = _load_json(parser_start_report)
    parser_batches = _load_json(parser_review_batches_report)
    table = _load_json(table_review_summary_report)
    table_batches = _load_json(table_review_batches_report)
    github = _load_json(github_publish_summary_report)
    remediation = _load_json(remediation_plan_report)
    table_remediation = _table_remediation_counts(remediation)
    parser_artifacts = _dict(parser.get("artifacts"))
    if not parser_artifacts:
        parser_artifacts = _dict(parser.get("output_artifacts"))

    parser_open = _dict(parser.get("open_item_summary"))
    parser_completion = _dict(parser.get("completion_summary"))
    table_required_missing_total = _int(table.get("required_field_missing_total"))
    owner_decisions = [_dict(item) for item in _list(github.get("owner_decisions_required"))]
    machine_actions = [_dict(item) for item in _list(github.get("machine_cleanup_actions"))]
    product_status = _dict(github.get("product_readiness_status"))
    public_status = _dict(github.get("public_release_gate_status"))
    source_lineage_status = _dict(github.get("source_lineage_status"))
    source_lineage_blocking_count = _int(source_lineage_status.get("blocking_count"))
    github_overall_status = str(github.get("overall_status") or "")

    lineage_queue = {
        "queue_id": "source_report_lineage",
        "priority": 0,
        "status": _queue_status(source_lineage_blocking_count),
        "owner": "release engineer",
        "item_count": source_lineage_blocking_count,
        "blocks": [
            "official public release readiness signal",
            "evidence-backed publish summary",
        ],
        "evidence": {
            "github_overall_status": github_overall_status,
            "source_lineage_passed": bool(source_lineage_status.get("passed", True)),
            "source_lineage_status": source_lineage_status.get("status"),
            "source_lineage_blocking_count": source_lineage_blocking_count,
            "source_lineage_warning_count": _int(source_lineage_status.get("warning_count")),
            "source_lineage_findings": _short_list(
                _list(source_lineage_status.get("findings")),
                5,
            ),
            "source_lineage_relationships": _short_list(
                _list(source_lineage_status.get("relationships")),
                5,
            ),
        },
        "start_here": {
            "github_publish_summary_report": str(github_publish_summary_report),
        },
        "next_actions": [
            "Regenerate the stale upstream source report.",
            "Regenerate the GitHub publish readiness summary after lineage matches.",
        ],
    }

    parser_queue = {
        "queue_id": "parser_goldset_open_items",
        "priority": 1,
        "status": _queue_status(_int(parser_open.get("open_item_count"))),
        "owner": "parser reviewer",
        "item_count": _int(parser_open.get("open_item_count")),
        "blocks": [
            "overall parser F1",
            "release-grade parsing accuracy claim",
        ],
        "evidence": {
            "item_kind_counts": _dict(parser_open.get("item_kind_counts")),
            "structure_counts": _dict(parser_open.get("structure_counts")),
            "pending_document_count": _int(parser_completion.get("pending_document_count")),
            "ready_for_quality_claim": bool(parser_completion.get("ready_for_quality_claim")),
            "top_structures": _short_list(_list(parser.get("structure_review_queue")), 5),
            "top_documents": _short_list(_list(parser.get("top_documents")), 5),
            "first_review_batch_document_count": _int(
                parser_batches.get("first_batch_document_count")
            ),
            "first_review_batch_open_item_count": _int(
                parser_batches.get("first_batch_open_item_count")
            ),
            "first_review_batch_pipeline_count_total": _int(
                parser_batches.get("first_batch_pipeline_count_total")
            ),
            "first_review_batch": _short_list(_list(parser_batches.get("first_review_batch")), 6),
        },
        "start_here": {
            "label_csv": _dict(_dict(parser.get("source_artifacts")).get("labels_csv")).get("path"),
            "open_item_worklist_csv": parser_artifacts.get("open_item_worklist_csv"),
            "parser_review_batches_report": str(parser_review_batches_report)
            if parser_review_batches_report
            else None,
            "open_label_csv_command": _dict(parser.get("open_commands")).get("open_label_csv"),
        },
        "next_actions": [
            "Fill all open matched-count fields.",
            "Set every pending label_status to reviewed or approved after verification.",
            "Regenerate parser goldset score and product readiness.",
        ],
    }

    table_queue = {
        "queue_id": "table_unit_human_review",
        "priority": 2,
        "status": _queue_status(_int(table.get("pending_unit_count"))),
        "owner": "table reviewer",
        "item_count": _int(table.get("pending_unit_count")),
        "blocks": [
            "release-grade table preprocessing claim",
            "table count transfer",
        ],
        "evidence": {
            "selected_unit_count": _int(table.get("selected_unit_count")),
            "completed_unit_count": _int(table.get("completed_unit_count")),
            "pending_unit_count": _int(table.get("pending_unit_count")),
            "required_field_missing_total": table_required_missing_total,
            "required_field_missing_counts": _dict(table.get("required_field_missing_counts")),
            "review_priority_counts": _dict(table.get("review_priority_counts")),
            "label_review_flag_counts": _dict(table.get("label_review_flag_counts")),
            "ready_for_table_score_transfer": bool(table.get("ready_for_table_score_transfer")),
            "source_traceability_passed": bool(
                table_remediation.get("source_traceability_passed")
            ),
            "source_traceability_issue_count": _int(
                table_remediation.get("source_traceability_issue_count")
            ),
            "first_review_batch_count": _int(table_batches.get("first_batch_count")),
            "first_review_batch_unit_count": _int(table_batches.get("first_batch_unit_count")),
            "human_status_missing_batch_count": _int(
                table_batches.get("human_status_missing_batch_count")
            ),
            "first_review_batches": _short_list(_list(table_batches.get("first_review_batches")), 5),
            "top_documents": _short_list(_list(table.get("document_summaries")), 8),
        },
        "start_here": {
            "table_units_csv": table.get("source_table_units_csv"),
            "summary_csv": _dict(table.get("artifacts")).get("csv"),
            "summary_markdown": _dict(table.get("artifacts")).get("markdown"),
            "table_review_batches_report": str(table_review_batches_report)
            if table_review_batches_report
            else None,
        },
        "next_actions": [
            "Review source pages for each selected table unit.",
            "Fill manual and matched table counts plus row/column and parentage confirmations.",
            "Add reviewer and reviewed-at metadata, then rerun transfer validation.",
        ],
    }

    owner_queue = {
        "queue_id": "public_branch_owner_decisions",
        "priority": 3,
        "status": _queue_status(len(owner_decisions), blocked_by_owner=True),
        "owner": "release owner",
        "item_count": len(owner_decisions),
        "blocks": [
            "source-only public GitHub branch",
            "public release gate",
        ],
        "evidence": {
            "public_gate_status": public_status.get("status"),
            "public_finding_count": _int(public_status.get("finding_count")),
            "public_action_count": _int(public_status.get("action_count")),
            "decision_ids": [str(item.get("decision_id") or "") for item in owner_decisions],
            "owner_decisions": owner_decisions,
        },
        "start_here": {
            "owner_decisions_md": github.get("source_reports", {}).get(
                "owner_decisions_report"
            ),
        },
        "next_actions": [
            "Choose license.",
            "Decide sample redistribution and identifier fixture policy.",
            "Decide whether private docs are removed or rewritten for public use.",
        ],
    }

    cleanup_queue = {
        "queue_id": "approved_public_branch_machine_cleanup",
        "priority": 4,
        "status": _queue_status(len(machine_actions), blocked_by_owner=True),
        "owner": "release engineer",
        "item_count": len(machine_actions),
        "blocks": [
            "clean source-only branch after owner approval",
        ],
        "evidence": {
            "machine_cleanup_action_count": _int(github.get("machine_cleanup_action_count")),
            "cleanup_actions": machine_actions,
            "destructive_action_count": _int(
                _dict(github.get("cleanup_breakdown")).get("destructive_action_count")
            ),
            "safe_machine_action_count": _int(
                _dict(github.get("cleanup_breakdown")).get("safe_machine_action_count")
            ),
        },
        "start_here": {
            "apply_scope": "dedicated_public_release_branch",
        },
        "next_actions": [
            "Apply only after release-owner decisions are recorded.",
            "Run public release gate again on the dedicated public branch.",
        ],
    }

    queues = [lineage_queue, parser_queue, table_queue, owner_queue, cleanup_queue]
    open_queue_count = sum(1 for queue in queues if queue["status"] != "clear")
    total_open_items = sum(_int(queue.get("item_count")) for queue in queues)

    source_lineage_passed = (
        not source_lineage_status
        or (bool(source_lineage_status.get("passed")) and source_lineage_blocking_count == 0)
    )
    overall_status_allows_public = (
        not github_overall_status or github_overall_status == "ready_for_public_github_publish"
    )
    official_public_release_ready = (
        bool(product_status.get("passed"))
        and bool(public_status.get("passed"))
        and source_lineage_passed
        and overall_status_allows_public
    )
    limited_human_loop_pilot_status = (
        "conditional_human_review_required"
        if parser_queue["item_count"] or table_queue["item_count"]
        else "evidence_ready_for_release_owner_review"
    )

    return {
        "report_type": "publish_hitl_operator_workboard",
        "generated_at": _utc_now(),
        "repo_commit": _repo_commit(),
        "official_public_release_ready": official_public_release_ready,
        "limited_human_loop_pilot_status": limited_human_loop_pilot_status,
        "summary": {
            "open_queue_count": open_queue_count,
            "total_open_items": total_open_items,
            "parser_open_item_count": parser_queue["item_count"],
            "table_pending_unit_count": table_queue["item_count"],
            "table_required_field_missing_total": table_required_missing_total,
            "parser_first_review_batch_open_item_count": _int(
                parser_batches.get("first_batch_open_item_count")
            ),
            "table_first_review_batch_unit_count": _int(
                table_batches.get("first_batch_unit_count")
            ),
            "owner_decision_count": owner_queue["item_count"],
            "machine_cleanup_action_count": cleanup_queue["item_count"],
            "product_blocking_count": _int(product_status.get("blocking_count")),
            "product_warning_count": _int(product_status.get("warning_count")),
            "public_finding_count": _int(public_status.get("finding_count")),
            "public_action_count": _int(public_status.get("action_count")),
            "source_lineage_blocking_count": source_lineage_blocking_count,
            "github_overall_status": github_overall_status,
        },
        "operator_queues": queues,
        "recommended_sequence": github.get("recommended_sequence", []),
        "source_reports": {
            "parser_start_report": str(parser_start_report),
            "table_review_summary_report": str(table_review_summary_report),
            "github_publish_summary_report": str(github_publish_summary_report),
            "remediation_plan_report": str(remediation_plan_report)
            if remediation_plan_report
            else None,
            "parser_review_batches_report": str(parser_review_batches_report)
            if parser_review_batches_report
            else None,
            "table_review_batches_report": str(table_review_batches_report)
            if table_review_batches_report
            else None,
        },
        "source_report_artifacts": [
            _artifact("parser_start_report", parser_start_report),
            _artifact("parser_review_batches_report", parser_review_batches_report),
            _artifact("table_review_summary_report", table_review_summary_report),
            _artifact("table_review_batches_report", table_review_batches_report),
            _artifact("github_publish_summary_report", github_publish_summary_report),
            _artifact("remediation_plan_report", remediation_plan_report),
        ],
        "safety_note": (
            "This workboard is read-only. It does not approve chunks, fill labels, "
            "remove files, or write Vector DB records."
        ),
        "api_call_count": 0,
    }


def _compact_dict(mapping: dict[str, Any], *, limit: int = 4) -> str:
    if not mapping:
        return "-"
    pairs = list(mapping.items())
    rendered = ", ".join(f"{key}={value}" for key, value in pairs[:limit])
    if len(pairs) > limit:
        rendered += f", ... (+{len(pairs) - limit})"
    return rendered


def _md_table_cell(value: Any) -> str:
    text = str(value if value is not None else "-")
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = _dict(report.get("summary"))
    lines = [
        "# Publish HITL Operator Workboard",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Official public release ready: `{str(report.get('official_public_release_ready')).lower()}`",
        f"- Limited human-loop pilot status: `{report.get('limited_human_loop_pilot_status')}`",
        f"- Open queues/items: {summary.get('open_queue_count')} / {summary.get('total_open_items')}",
        f"- Parser open items: {summary.get('parser_open_item_count')}",
        f"- Table pending units: {summary.get('table_pending_unit_count')}",
        f"- Table required missing fields: {summary.get('table_required_field_missing_total')}",
        f"- Owner decisions / machine cleanup actions: {summary.get('owner_decision_count')} / {summary.get('machine_cleanup_action_count')}",
        f"- Source lineage blockers: {summary.get('source_lineage_blocking_count')}",
        f"- GitHub publish overall status: `{summary.get('github_overall_status') or 'unknown'}`",
        "",
        "## Operator Queues",
        "",
        "| Priority | Queue | Status | Items | Owner | Blocks | First Action |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]
    for queue in _list(report.get("operator_queues")):
        next_actions = _list(_dict(queue).get("next_actions"))
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_table_cell(_dict(queue).get("priority")),
                    _md_table_cell(_dict(queue).get("queue_id")),
                    f"`{_md_table_cell(_dict(queue).get('status'))}`",
                    _md_table_cell(_dict(queue).get("item_count")),
                    _md_table_cell(_dict(queue).get("owner")),
                    _md_table_cell(", ".join(str(item) for item in _list(_dict(queue).get("blocks")))),
                    _md_table_cell(next_actions[0] if next_actions else "-"),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Evidence Highlights", ""])
    for queue in _list(report.get("operator_queues")):
        queue = _dict(queue)
        evidence = _dict(queue.get("evidence"))
        lines.append(f"### `{queue.get('queue_id')}`")
        if queue.get("queue_id") == "source_report_lineage":
            lines.append(
                f"- Lineage passed: `{str(evidence.get('source_lineage_passed')).lower()}`; "
                f"blockers: {evidence.get('source_lineage_blocking_count')}; "
                f"warnings: {evidence.get('source_lineage_warning_count')}"
            )
            lines.append(
                f"- GitHub overall status: `{evidence.get('github_overall_status') or 'unknown'}`"
            )
        elif queue.get("queue_id") == "parser_goldset_open_items":
            lines.append(
                f"- Item kinds: {_compact_dict(_dict(evidence.get('item_kind_counts')))}"
            )
            lines.append(
                f"- Structures: {_compact_dict(_dict(evidence.get('structure_counts')))}"
            )
            if evidence.get("first_review_batch_open_item_count"):
                lines.append(
                    "- First review batch: "
                    f"{evidence.get('first_review_batch_document_count')} documents / "
                    f"{evidence.get('first_review_batch_open_item_count')} open items / "
                    f"pipeline total {evidence.get('first_review_batch_pipeline_count_total')}"
                )
        elif queue.get("queue_id") == "table_unit_human_review":
            lines.append(
                f"- Review priorities: {_compact_dict(_dict(evidence.get('review_priority_counts')))}"
            )
            lines.append(
                f"- Label flags: {_compact_dict(_dict(evidence.get('label_review_flag_counts')))}"
            )
            lines.append(
                f"- Source traceability passed: `{str(evidence.get('source_traceability_passed')).lower()}`; issues: {evidence.get('source_traceability_issue_count')}"
            )
            if evidence.get("first_review_batch_unit_count"):
                lines.append(
                    "- First review batch: "
                    f"{evidence.get('first_review_batch_count')} batches / "
                    f"{evidence.get('first_review_batch_unit_count')} units"
                )
            first_review_batches = _list(evidence.get("first_review_batches"))
            if first_review_batches:
                lines.extend(
                    [
                        "",
                        "| Batch Rank | Batch ID | Document | Units | Priority |",
                        "| --- | --- | --- | ---: | --- |",
                    ]
                )
                for batch in first_review_batches:
                    batch = _dict(batch)
                    lines.append(
                        "| "
                        + " | ".join(
                            [
                                _md_table_cell(batch.get("batch_rank")),
                                _md_table_cell(batch.get("table_review_batch_id")),
                                _md_table_cell(batch.get("document_id")),
                                _md_table_cell(batch.get("unit_count")),
                                _md_table_cell(batch.get("review_priority")),
                            ]
                        )
                        + " |"
                    )
        elif queue.get("queue_id") == "public_branch_owner_decisions":
            lines.append(
                f"- Decision IDs: {', '.join(str(item) for item in evidence.get('decision_ids', [])) or '-'}"
            )
            owner_decisions = _list(evidence.get("owner_decisions"))
            if owner_decisions:
                lines.extend(
                    [
                        "",
                        "| Decision ID | Blocking Decision |",
                        "| --- | --- |",
                    ]
                )
                for decision in owner_decisions:
                    decision = _dict(decision)
                    lines.append(
                        "| "
                        + " | ".join(
                            [
                                _md_table_cell(decision.get("decision_id")),
                                _md_table_cell(
                                    decision.get("summary")
                                    or decision.get("blocking_decision")
                                    or decision.get("decision")
                                ),
                            ]
                        )
                        + " |"
                    )
        elif queue.get("queue_id") == "approved_public_branch_machine_cleanup":
            lines.append(
                f"- Destructive actions: {evidence.get('destructive_action_count')}; safe machine actions: {evidence.get('safe_machine_action_count')}"
            )
        lines.append("")

    lines.extend(["## Recommended Sequence", ""])
    sequence = _list(report.get("recommended_sequence"))
    if sequence:
        for item in sequence:
            item = _dict(item)
            lines.append(
                f"{item.get('order')}. `{item.get('workstream')}`: {item.get('action')} Blocks public publish: `{str(item.get('blocks_public_publish')).lower()}`"
            )
    else:
        lines.append("- No recommended sequence supplied by source summary.")

    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def write_queue_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "priority",
                "queue_id",
                "status",
                "item_count",
                "owner",
                "blocks",
                "first_action",
            ],
        )
        writer.writeheader()
        for queue in _list(report.get("operator_queues")):
            queue = _dict(queue)
            next_actions = _list(queue.get("next_actions"))
            writer.writerow(
                {
                    "priority": queue.get("priority"),
                    "queue_id": queue.get("queue_id"),
                    "status": queue.get("status"),
                    "item_count": queue.get("item_count"),
                    "owner": queue.get("owner"),
                    "blocks": "; ".join(str(item) for item in _list(queue.get("blocks"))),
                    "first_action": next_actions[0] if next_actions else "",
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parser-start-report", required=True, type=Path)
    parser.add_argument("--table-review-summary-report", required=True, type=Path)
    parser.add_argument("--github-publish-summary-report", required=True, type=Path)
    parser.add_argument("--remediation-plan-report", type=Path)
    parser.add_argument("--parser-review-batches-report", type=Path)
    parser.add_argument("--table-review-batches-report", type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    parser.add_argument("--out-csv", required=True, type=Path)
    args = parser.parse_args()

    report = build_publish_hitl_operator_workboard(
        parser_start_report=args.parser_start_report,
        table_review_summary_report=args.table_review_summary_report,
        github_publish_summary_report=args.github_publish_summary_report,
        remediation_plan_report=args.remediation_plan_report,
        parser_review_batches_report=args.parser_review_batches_report,
        table_review_batches_report=args.table_review_batches_report,
    )
    _write_json(args.out_json, report)
    _write_text(args.out_md, render_markdown(report))
    write_queue_csv(args.out_csv, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
