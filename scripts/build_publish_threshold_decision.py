"""Build a publish-threshold decision report from release-readiness evidence."""

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


def _track_by_name(github_summary: dict[str, Any], track_name: str) -> dict[str, Any]:
    for track in _list(github_summary.get("progress_tracks")):
        item = _dict(track)
        if item.get("track") == track_name:
            return item
    return {}


def _blocker_codes(product_readiness: dict[str, Any]) -> list[str]:
    codes = product_readiness.get("blocking_codes")
    if codes is None:
        codes = product_readiness.get("blocker_codes")
    return [str(code) for code in _list(codes)]


def _warning_codes(product_readiness: dict[str, Any]) -> list[str]:
    return [str(code) for code in _list(product_readiness.get("warning_codes"))]


def _status(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def build_publish_threshold_decision(
    *,
    hitl_workboard_report: Path,
    github_publish_summary_report: Path,
    product_readiness_report: Path,
    table_claim_gate_report: Path | None = None,
    parser_start_report: Path | None = None,
    answer_accuracy_depth_report: Path | None = None,
) -> dict[str, Any]:
    hitl = _load_json(hitl_workboard_report)
    github = _load_json(github_publish_summary_report)
    product = _load_json(product_readiness_report)
    table_gate = _load_json(table_claim_gate_report)
    parser_start = _load_json(parser_start_report)
    answer_depth = _load_json(answer_accuracy_depth_report)

    hitl_summary = _dict(hitl.get("summary"))
    public_gate = _dict(github.get("public_release_gate_status"))
    product_status = _dict(github.get("product_readiness_status"))
    source_lineage = _dict(github.get("source_lineage_status"))
    github_overall_status = str(github.get("overall_status") or "")
    product_public_track = _track_by_name(github, "product_public_release")
    core_track = _track_by_name(github, "core_pipeline")
    human_loop_track = _track_by_name(github, "human_intervention_minimization")
    source_only_track = _track_by_name(github, "source_only_github_publish")
    parser_open = _dict(parser_start.get("open_item_summary"))
    table_gate_summary = _dict(table_gate.get("summary"))
    owner_decisions_required = _list(github.get("owner_decisions_required"))
    owner_decision_ids = [str(item.get("decision_id") or "") for item in owner_decisions_required]

    source_lineage_passed = (
        not source_lineage
        or (bool(source_lineage.get("passed")) and _int(source_lineage.get("blocking_count")) == 0)
    )
    github_status_allows_public = (
        not github_overall_status or github_overall_status == "ready_for_public_github_publish"
    )
    official_ready = (
        bool(hitl.get("official_public_release_ready"))
        and bool(product.get("passed"))
        and bool(public_gate.get("passed"))
        and source_lineage_passed
        and github_status_allows_public
    )
    limited_status = _status(hitl.get("limited_human_loop_pilot_status"))
    limited_conditional = limited_status in {
        "ready",
        "ready_with_warnings",
        "conditional_human_review_required",
    }

    owner_decision_count = _int(hitl_summary.get("owner_decision_count"), len(owner_decisions_required))
    machine_cleanup_count = _int(
        hitl_summary.get("machine_cleanup_action_count"),
        len(_list(github.get("machine_cleanup_actions"))),
    )
    table_pending_unit_count = _int(
        table_gate_summary.get("pending_unit_count"),
        _int(hitl_summary.get("table_pending_unit_count")),
    )
    table_required_field_missing_total = _int(
        table_gate_summary.get("required_field_missing_total"),
        _int(hitl_summary.get("table_required_field_missing_total")),
    )
    table_claim_ready = bool(
        table_gate
        and table_gate.get("passed") is True
        and table_gate.get("status") in {"ready", "ready_for_table_quality_claim"}
        and table_pending_unit_count == 0
        and _int(table_gate_summary.get("invalid_unit_count")) == 0
        and table_gate_summary.get("transfer_passed") is True
        and _int(table_gate_summary.get("transfer_blocker_count")) == 0
    )
    table_first_review_batch_unit_count = (
        0
        if table_claim_ready
        else _int(hitl_summary.get("table_first_review_batch_unit_count"))
    )
    product_blocking_count = _int(
        product.get("blocking_count"),
        _int(
            product.get("blocker_count"),
            _int(product_status.get("blocking_count"), _int(hitl_summary.get("product_blocking_count"))),
        ),
    )
    product_warning_count = _int(
        product.get("warning_count"),
        _int(product_status.get("warning_count"), _int(hitl_summary.get("product_warning_count"))),
    )
    public_finding_count = _int(
        public_gate.get("finding_count"),
        _int(hitl_summary.get("public_finding_count")),
    )
    public_action_count = _int(
        public_gate.get("action_count"),
        _int(hitl_summary.get("public_action_count")),
    )

    hard_blockers = [
        {
            "blocker": "parser_goldset_open_items",
            "count": _int(hitl_summary.get("parser_open_item_count"), _int(parser_open.get("open_item_count"))),
            "blocks": ["overall parser F1", "release-grade parsing accuracy claim"],
        },
        {
            "blocker": "table_unit_human_review",
            "count": table_pending_unit_count,
            "blocks": ["release-grade table preprocessing claim", "table count transfer"],
        },
        {
            "blocker": "public_branch_owner_decisions",
            "count": owner_decision_count,
            "blocks": ["source-only public GitHub branch", "public release gate"],
        },
        {
            "blocker": "approved_public_branch_machine_cleanup",
            "count": machine_cleanup_count,
            "blocks": ["clean public source branch"],
        },
    ]

    report = {
        "report_type": "publish_threshold_decision",
        "generated_at": _utc_now(),
        "current_decision": {
            "official_public_release": "ready" if official_ready else "blocked",
            "limited_human_loop_pilot": "conditional" if limited_conditional else "blocked",
            "source_only_public_github": _status(source_only_track.get("status"), "unknown"),
            "summary": (
                "85%+ is acceptable only for a limited human-loop pilot with explicit controls; "
                "90%+ evidence is still required before public release or release-grade accuracy claims."
            ),
        },
        "threshold_assessment": {
            "eighty_five_plus": {
                "decision": "conditional_limited_pilot_only",
                "sufficient_for": [
                    "private or controlled pilot",
                    "operator-supervised preprocessing",
                    "approved-only RAG/MCP retrieval",
                ],
                "not_sufficient_for": [
                    "official public release",
                    "source-only public GitHub publish",
                    "release-grade parser or table accuracy claim",
                    "autonomous indexing without human approval",
                ],
                "required_controls": [
                    "human approval remains mandatory before Vector DB indexing",
                    "unapproved chunks stay out of retrieval",
                    "parser/table uncertainty remains visible as review flags",
                    "audit logs and owner decisions remain attached to the release record",
                ],
            },
            "ninety_plus": {
                "decision": "required_for_public_release_grade_claims",
                "required_for": [
                    "official public release",
                    "public GitHub publication",
                    "release-grade parsing accuracy claim",
                    "release-grade table preprocessing claim",
                ],
                "minimum_evidence": [
                    "parser goldset overall F1 is present and meets the configured threshold",
                    "table-unit human review is complete and transferable",
                    "public release gate has zero blocking findings",
                    "fresh clone CI and release hygiene pass on the intended branch",
                ],
            },
        },
        "current_progress_bands": {
            "core_pipeline": core_track.get("progress_band"),
            "human_intervention_minimization": human_loop_track.get("progress_band"),
            "source_only_github_publish": source_only_track.get("progress_band"),
            "product_public_release": product_public_track.get("progress_band"),
        },
        "evidence_counts": {
            "open_queue_count": _int(hitl_summary.get("open_queue_count")),
            "total_open_items": _int(hitl_summary.get("total_open_items")),
            "parser_open_item_count": _int(hitl_summary.get("parser_open_item_count")),
            "parser_first_review_batch_open_item_count": _int(
                hitl_summary.get("parser_first_review_batch_open_item_count")
            ),
            "table_pending_unit_count": table_pending_unit_count,
            "table_first_review_batch_unit_count": table_first_review_batch_unit_count,
            "table_required_field_missing_total": table_required_field_missing_total,
            "owner_decision_count": owner_decision_count,
            "machine_cleanup_action_count": machine_cleanup_count,
            "source_lineage_blocking_count": _int(
                hitl_summary.get("source_lineage_blocking_count"),
                _int(source_lineage.get("blocking_count")),
            ),
            "github_overall_status": github_overall_status,
            "product_blocking_count": product_blocking_count,
            "product_warning_count": product_warning_count,
            "public_finding_count": public_finding_count,
            "public_action_count": public_action_count,
            "answer_depth_warning_count": _int(answer_depth.get("warning_count")),
            "answer_depth_blocker_count": _int(answer_depth.get("blocker_count")),
        },
        "owner_decision_ids": owner_decision_ids,
        "product_readiness": {
            "passed": bool(product.get("passed")),
            "blocking_codes": _blocker_codes(product),
            "warning_codes": _warning_codes(product),
        },
        "github_publish_summary": {
            "overall_status": github_overall_status,
            "source_lineage_passed": source_lineage_passed,
            "source_lineage_blocking_count": _int(source_lineage.get("blocking_count")),
            "source_lineage_warning_count": _int(source_lineage.get("warning_count")),
        },
        "table_claim_gate": {
            "status": table_gate.get("status"),
            "feasibility_status": table_gate.get("feasibility_status"),
            "source_traceability_passed": table_gate_summary.get("source_traceability_passed"),
            "pending_unit_count": table_gate_summary.get("pending_unit_count"),
            "required_field_missing_total": table_gate_summary.get("required_field_missing_total"),
            "claim_ready": table_claim_ready,
        },
        "answer_accuracy_depth": {
            "present": bool(answer_depth),
            "passed_for_public_release_depth": answer_depth.get(
                "passed_for_public_release_depth"
            ),
            "pilot_evidence_status": answer_depth.get("pilot_evidence_status"),
            "warning_count": _int(answer_depth.get("warning_count")),
            "blocker_count": _int(answer_depth.get("blocker_count")),
            "finding_codes": [
                str(_dict(item).get("code") or "") for item in _list(answer_depth.get("findings"))
            ],
        },
        "hard_blockers": [item for item in hard_blockers if _int(item.get("count")) > 0],
        "next_actions_to_reach_90_plus": _next_actions_to_reach_90_plus(
            parser_first_review_batch_open_item_count=_int(
                hitl_summary.get("parser_first_review_batch_open_item_count")
            ),
            table_claim_ready=table_claim_ready,
            table_first_review_batch_unit_count=table_first_review_batch_unit_count,
            owner_decision_count=owner_decision_count,
            machine_cleanup_count=machine_cleanup_count,
            answer_depth_warning_count=_int(answer_depth.get("warning_count")),
            official_ready=official_ready,
        ),
        "source_report_artifacts": [
            _artifact("hitl_workboard_report", hitl_workboard_report),
            _artifact("github_publish_summary_report", github_publish_summary_report),
            _artifact("product_readiness_report", product_readiness_report),
            _artifact("table_claim_gate_report", table_claim_gate_report),
            _artifact("parser_start_report", parser_start_report),
            _artifact("answer_accuracy_depth_report", answer_accuracy_depth_report),
        ],
        "safety_note": (
            "This report is read-only. It does not approve chunks, change review status, "
            "remove files, or write Vector DB records."
        ),
        "api_call_count": 0,
    }
    return report


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _compact_list(values: list[str], *, limit: int = 5) -> str:
    if not values:
        return "-"
    text = ", ".join(values[:limit])
    if len(values) > limit:
        text += f", ... (+{len(values) - limit})"
    return text


def _next_actions_to_reach_90_plus(
    *,
    parser_first_review_batch_open_item_count: int,
    table_claim_ready: bool,
    table_first_review_batch_unit_count: int,
    owner_decision_count: int,
    machine_cleanup_count: int,
    answer_depth_warning_count: int,
    official_ready: bool,
) -> list[dict[str, Any]]:
    candidates = [
        {
            "include": parser_first_review_batch_open_item_count > 0,
            "action": "Complete the parser first review batch, then regenerate parser goldset score.",
            "evidence_target": "parser_first_review_batch_open_item_count",
            "current_count": parser_first_review_batch_open_item_count,
        },
        {
            "include": not table_claim_ready and table_first_review_batch_unit_count > 0,
            "action": "Complete the table first review batch, then rerun table transfer validation.",
            "evidence_target": "table_first_review_batch_unit_count",
            "current_count": table_first_review_batch_unit_count,
        },
        {
            "include": owner_decision_count > 0,
            "action": "Record release-owner decisions for license, samples, private docs, and fixtures.",
            "evidence_target": "owner_decision_count",
            "current_count": owner_decision_count,
        },
        {
            "include": machine_cleanup_count > 0,
            "action": "Apply approved public-branch cleanup only on the dedicated public-release branch.",
            "evidence_target": "machine_cleanup_action_count",
            "current_count": machine_cleanup_count,
        },
        {
            "include": answer_depth_warning_count > 0,
            "action": "Expand answer-accuracy evidence beyond smoke depth before public accuracy claims.",
            "evidence_target": "answer_depth_warning_count",
            "current_count": answer_depth_warning_count,
        },
        {
            "include": not official_ready,
            "action": "Run fresh clone CI, public release gate, product readiness, and release hygiene.",
            "evidence_target": "all_release_gates_passed",
            "current_count": 1,
        },
    ]
    actions = [
        {key: value for key, value in candidate.items() if key != "include"}
        for candidate in candidates
        if candidate["include"]
    ]
    for index, action in enumerate(actions, start=1):
        action["order"] = index
    return actions


def render_markdown(report: dict[str, Any]) -> str:
    decision = _dict(report.get("current_decision"))
    counts = _dict(report.get("evidence_counts"))
    product = _dict(report.get("product_readiness"))
    answer_depth = _dict(report.get("answer_accuracy_depth"))
    bands = _dict(report.get("current_progress_bands"))
    lines = [
        "# Publish Threshold Decision",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Official public release: `{decision.get('official_public_release')}`",
        f"- Limited human-loop pilot: `{decision.get('limited_human_loop_pilot')}`",
        f"- Source-only public GitHub: `{decision.get('source_only_public_github')}`",
        f"- Decision summary: {decision.get('summary')}",
        "",
        "## Threshold Policy",
        "",
        "| Threshold | Decision | Allowed Scope | Not Allowed |",
        "| --- | --- | --- | --- |",
        (
            "| 85%+ | conditional limited pilot only | private/controlled pilot, "
            "operator-supervised preprocessing, approved-only retrieval | official public release, "
            "public GitHub publish, release-grade accuracy claims, autonomous indexing |"
        ),
        (
            "| 90%+ | required for public-release-grade claims | public release after gates pass, "
            "parser/table quality claims, cleaned source branch | bypassing owner decisions or human approval |"
        ),
        "",
        "## Current Evidence",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Open queues | {_md_cell(counts.get('open_queue_count'))} |",
        f"| Total open items | {_md_cell(counts.get('total_open_items'))} |",
        f"| Parser open items | {_md_cell(counts.get('parser_open_item_count'))} |",
        (
            "| Parser first review batch open items | "
            f"{_md_cell(counts.get('parser_first_review_batch_open_item_count'))} |"
        ),
        f"| Table pending units | {_md_cell(counts.get('table_pending_unit_count'))} |",
        (
            "| Table first review batch units | "
            f"{_md_cell(counts.get('table_first_review_batch_unit_count'))} |"
        ),
        (
            "| Table required missing fields | "
            f"{_md_cell(counts.get('table_required_field_missing_total'))} |"
        ),
        f"| Owner decisions | {_md_cell(counts.get('owner_decision_count'))} |",
        f"| Owner decision IDs | {_md_cell(_compact_list([str(item) for item in _list(report.get('owner_decision_ids'))]))} |",
        f"| Machine cleanup actions | {_md_cell(counts.get('machine_cleanup_action_count'))} |",
        f"| Source lineage blockers | {_md_cell(counts.get('source_lineage_blocking_count'))} |",
        f"| Answer-depth warnings/blockers | {_md_cell(counts.get('answer_depth_warning_count'))} / {_md_cell(counts.get('answer_depth_blocker_count'))} |",
        f"| Product blockers | {_md_cell(counts.get('product_blocking_count'))} |",
        f"| Public findings/actions | {_md_cell(counts.get('public_finding_count'))} / {_md_cell(counts.get('public_action_count'))} |",
        "",
        "## Progress Bands",
        "",
        "| Track | Band |",
        "| --- | --- |",
        f"| Core pipeline | {_md_cell(bands.get('core_pipeline'))} |",
        f"| Human intervention minimization | {_md_cell(bands.get('human_intervention_minimization'))} |",
        f"| Source-only GitHub publish | {_md_cell(bands.get('source_only_github_publish'))} |",
        f"| Product public release | {_md_cell(bands.get('product_public_release'))} |",
        "",
        "## Blocking Codes",
        "",
        f"- Product blockers: `{_compact_list([str(item) for item in _list(product.get('blocking_codes'))])}`",
        f"- Product warnings: `{_compact_list([str(item) for item in _list(product.get('warning_codes'))])}`",
        f"- Answer-depth findings: `{_compact_list([str(item) for item in _list(answer_depth.get('finding_codes'))])}`",
        "",
        "## Next Actions To Reach 90%+",
        "",
    ]
    for item in _list(report.get("next_actions_to_reach_90_plus")):
        entry = _dict(item)
        lines.append(
            f"{entry.get('order')}. {entry.get('action')} "
            f"(current {entry.get('evidence_target')}={entry.get('current_count')})"
        )
    lines.extend(["", f"> {report.get('safety_note')}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hitl-workboard-report", required=True, type=Path)
    parser.add_argument("--github-publish-summary-report", required=True, type=Path)
    parser.add_argument("--product-readiness-report", required=True, type=Path)
    parser.add_argument("--table-claim-gate-report", type=Path)
    parser.add_argument("--parser-start-report", type=Path)
    parser.add_argument("--answer-accuracy-depth-report", type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_publish_threshold_decision(
        hitl_workboard_report=args.hitl_workboard_report,
        github_publish_summary_report=args.github_publish_summary_report,
        product_readiness_report=args.product_readiness_report,
        table_claim_gate_report=args.table_claim_gate_report,
        parser_start_report=args.parser_start_report,
        answer_accuracy_depth_report=args.answer_accuracy_depth_report,
    )
    _write_json(args.out_json, report)
    _write_text(args.out_md, render_markdown(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
