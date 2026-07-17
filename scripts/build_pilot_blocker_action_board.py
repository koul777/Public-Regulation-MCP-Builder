from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


def build_pilot_blocker_action_board(
    *,
    product_readiness_report: Path,
    parser_completion_board_report: Path | None = None,
    table_preprocessing_claim_gate_report: Path | None = None,
    temporal_policy_decision_validation_report: Path | None = None,
    reapproval_decision_validation_report: Path | None = None,
    reapproval_apply_plan_report: Path | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    product = _load_json(product_readiness_report)
    parser_completion = _load_optional_json(parser_completion_board_report)
    table_claim = _load_optional_json(table_preprocessing_claim_gate_report)
    temporal_validation = _load_optional_json(temporal_policy_decision_validation_report)
    reapproval_validation = _load_optional_json(reapproval_decision_validation_report)
    reapproval_apply_plan = _load_optional_json(reapproval_apply_plan_report)

    actions = _actions(
        product=product,
        parser_completion=parser_completion,
        table_claim=table_claim,
        temporal_validation=temporal_validation,
        reapproval_validation=reapproval_validation,
        reapproval_apply_plan=reapproval_apply_plan,
    )
    report = {
        "report_type": "pilot_blocker_action_board",
        "generated_at": generated_at,
        "source_reports": {
            "product_readiness_report": str(product_readiness_report),
            "parser_completion_board_report": str(parser_completion_board_report)
            if parser_completion_board_report
            else None,
            "table_preprocessing_claim_gate_report": str(table_preprocessing_claim_gate_report)
            if table_preprocessing_claim_gate_report
            else None,
            "temporal_policy_decision_validation_report": str(temporal_policy_decision_validation_report)
            if temporal_policy_decision_validation_report
            else None,
            "reapproval_decision_validation_report": str(reapproval_decision_validation_report)
            if reapproval_decision_validation_report
            else None,
            "reapproval_apply_plan_report": str(reapproval_apply_plan_report)
            if reapproval_apply_plan_report
            else None,
        },
        "source_report_artifacts": [
            _artifact("product_readiness_report", product_readiness_report, product),
            _artifact("parser_completion_board_report", parser_completion_board_report, parser_completion),
            _artifact("table_preprocessing_claim_gate_report", table_preprocessing_claim_gate_report, table_claim),
            _artifact(
                "temporal_policy_decision_validation_report",
                temporal_policy_decision_validation_report,
                temporal_validation,
            ),
            _artifact("reapproval_decision_validation_report", reapproval_decision_validation_report, reapproval_validation),
            _artifact("reapproval_apply_plan_report", reapproval_apply_plan_report, reapproval_apply_plan),
        ],
        "product_gate_summary": _product_summary(product),
        "parser_completion_summary": _parser_completion_summary(parser_completion),
        "table_claim_summary": _table_claim_summary(table_claim),
        "temporal_policy_summary": _temporal_policy_summary(temporal_validation),
        "reapproval_summary": _reapproval_summary(reapproval_validation, reapproval_apply_plan),
        "action_count": len(actions),
        "actions": actions,
        "safety_note": (
            "This action board is read-only. It does not fill labels, make policy decisions, approve chunks, "
            "apply reapproval decisions, index vectors, or publish MCP evidence."
        ),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_markdown(report), encoding="utf-8")
    return report


def _actions(
    *,
    product: dict[str, Any],
    parser_completion: dict[str, Any],
    table_claim: dict[str, Any],
    temporal_validation: dict[str, Any],
    reapproval_validation: dict[str, Any],
    reapproval_apply_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not product.get("passed"):
        actions.append(
            {
                "priority": 1,
                "action_id": "clear_product_readiness_blockers",
                "owner": "release_owner",
                "status": "blocked",
                "evidence": {
                    "blocking_count": _int(product.get("blocking_count")),
                    "blocking_codes": product.get("blocking_codes") or [],
                },
                "next_step": "Resolve the parser, temporal policy, and reapproval blockers, then rerun product readiness.",
            }
        )
    if parser_completion and not parser_completion.get("ready_for_quality_claim"):
        actions.append(
            {
                "priority": 2,
                "action_id": "complete_parser_goldset_labels",
                "owner": "human_reviewer",
                "status": str(parser_completion.get("completion_gate_status") or "blocked"),
                "evidence": {
                    "document_count": _int(parser_completion.get("document_count")),
                    "ready_document_count": _int(parser_completion.get("ready_document_count")),
                    "completed_structure_score_rows": _int(parser_completion.get("completed_structure_score_rows")),
                    "expected_structure_score_rows": _int(parser_completion.get("expected_structure_score_rows")),
                    "missing_manual_field_count": _int(parser_completion.get("missing_manual_field_count")),
                    "missing_matched_field_count": _int(parser_completion.get("missing_matched_field_count")),
                    "label_status_counts": _dict(parser_completion.get("label_status_counts")),
                    "missing_matched_field_document_count": _missing_field_document_count(
                        parser_completion,
                        "missing_matched_fields",
                    ),
                    "missing_matched_field_samples": _parser_missing_field_samples(parser_completion),
                },
                "next_step": "Fill reviewer metadata plus manual and matched counts for the current parsing goldset.",
            }
        )
    if table_claim and not table_claim.get("passed"):
        summary = table_claim.get("summary") if isinstance(table_claim.get("summary"), dict) else {}
        actions.append(
            {
                "priority": 3,
                "action_id": "complete_table_unit_human_review",
                "owner": "human_reviewer",
                "status": str(table_claim.get("status") or "blocked"),
                "evidence": {
                    "claim_level": table_claim.get("claim_level"),
                    "pending_unit_count": _int(summary.get("pending_unit_count")),
                    "completed_unit_count": _int(summary.get("completed_unit_count")),
                    "invalid_unit_count": _int(summary.get("invalid_unit_count")),
                    "transfer_blocker_count": _int(summary.get("transfer_blocker_count")),
                    "transfer_finding_code_counts": _dict(summary.get("transfer_finding_code_counts")),
                    "transfer_primary_blocker": _dict(summary.get("transfer_root_cause_summary")).get("primary_blocker"),
                    "table_answer_blocker_count": _int(summary.get("table_answer_blocker_count")),
                    "source_traceability_passed": summary.get("source_traceability_passed"),
                    "source_traceability_issue_count": _int(summary.get("source_traceability_issue_count")),
                    "drift_check_passed": summary.get("drift_check_passed"),
                    "drift_check_blocker_count": _int(summary.get("drift_check_blocker_count")),
                    "source_format_status_counts": _dict(summary.get("source_format_status_counts")),
                },
                "next_step": "Complete table-unit source review, rerun table summary, transfer check, and table claim gate.",
            }
        )
    if temporal_validation and not temporal_validation.get("passed"):
        actions.append(
            {
                "priority": 4,
                "action_id": "fill_temporal_policy_decisions",
                "owner": "policy_owner",
                "status": str(temporal_validation.get("status") or "blocked"),
                "evidence": {
                    "decision_row_count": _int(temporal_validation.get("decision_row_count")),
                    "release_blocking_row_count": _int(
                        temporal_validation.get("release_blocking_row_count")
                    ),
                    "blocking_count": _int(temporal_validation.get("blocking_count")),
                    "operator_decision_counts": _dict(temporal_validation.get("operator_decision_counts")),
                },
                "next_step": "Fill the release-blocking temporal policy decision rows and rerun validation.",
            }
        )
    if reapproval_validation and not reapproval_validation.get("passed"):
        actions.append(
            {
                "priority": 5,
                "action_id": "fill_reapproval_batch_decisions",
                "owner": "operator",
                "status": str(reapproval_validation.get("release_gate_status") or "blocked"),
                "evidence": {
                    "expected_batch_count": _int(reapproval_validation.get("expected_batch_count")),
                    "decision_row_count": _int(reapproval_validation.get("decision_row_count")),
                    "complete_row_count": _int(reapproval_validation.get("complete_row_count")),
                    "blank_or_incomplete_row_count": _int(
                        reapproval_validation.get("blank_or_incomplete_row_count")
                    ),
                    "blocking_count": _int(reapproval_validation.get("blocking_count")),
                },
                "next_step": "Complete all reapproval batch decisions, validate them, then rebuild the apply plan.",
            }
        )
    if reapproval_apply_plan and not reapproval_apply_plan.get("passed"):
        actions.append(
            {
                "priority": 6,
                "action_id": "rebuild_reapproval_apply_plan_after_decisions",
                "owner": "operator",
                "status": str(reapproval_apply_plan.get("release_gate_status") or "blocked"),
                "evidence": {
                    "blocker_count": _int(reapproval_apply_plan.get("blocker_count")),
                    "ready_plan_count": _int(reapproval_apply_plan.get("ready_plan_count")),
                    "unresolved_chunk_count": _int(reapproval_apply_plan.get("unresolved_chunk_count")),
                },
                "next_step": "After decision validation passes, rebuild the apply plan and rerun product readiness.",
            }
        )
    return sorted(actions, key=lambda item: int(item["priority"]))


def _product_summary(product: dict[str, Any]) -> dict[str, Any]:
    gates = product.get("gates") if isinstance(product.get("gates"), dict) else {}
    return {
        "passed": bool(product.get("passed")),
        "blocking_count": _int(product.get("blocking_count")),
        "warning_count": _int(product.get("warning_count")),
        "blocking_codes": product.get("blocking_codes") or [],
        "ready_gates": [key for key, value in gates.items() if isinstance(value, dict) and value.get("status") == "ready"],
        "blocked_gates": [
            key for key, value in gates.items() if isinstance(value, dict) and value.get("status") == "blocked"
        ],
    }


def _parser_completion_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "completion_gate_status": report.get("completion_gate_status"),
        "ready_for_quality_claim": bool(report.get("ready_for_quality_claim")),
        "document_count": _int(report.get("document_count")),
        "ready_document_count": _int(report.get("ready_document_count")),
        "completed_structure_score_rows": _int(report.get("completed_structure_score_rows")),
        "expected_structure_score_rows": _int(report.get("expected_structure_score_rows")),
    }


def _table_claim_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {
        "passed": bool(report.get("passed")),
        "status": report.get("status"),
        "claim_level": report.get("claim_level"),
        "pending_unit_count": _int(summary.get("pending_unit_count")),
        "transfer_blocker_count": _int(summary.get("transfer_blocker_count")),
        "source_format_status_counts": _dict(summary.get("source_format_status_counts")),
    }


def _temporal_policy_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "passed": bool(report.get("passed")),
        "status": report.get("status"),
        "decision_row_count": _int(report.get("decision_row_count")),
        "release_blocking_row_count": _int(report.get("release_blocking_row_count")),
        "blocking_count": _int(report.get("blocking_count")),
    }


def _reapproval_summary(validation: dict[str, Any], apply_plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_validation_passed": bool(validation.get("passed")) if validation else None,
        "expected_batch_count": _int(validation.get("expected_batch_count")) if validation else None,
        "complete_row_count": _int(validation.get("complete_row_count")) if validation else None,
        "blank_or_incomplete_row_count": _int(validation.get("blank_or_incomplete_row_count"))
        if validation
        else None,
        "apply_plan_passed": bool(apply_plan.get("passed")) if apply_plan else None,
        "apply_plan_blocker_count": _int(apply_plan.get("blocker_count")) if apply_plan else None,
        "ready_plan_count": _int(apply_plan.get("ready_plan_count")) if apply_plan else None,
    }


def _artifact(role: str, path: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    if path is None:
        return {"role": role, "path": None, "exists": False}
    exists = path.exists()
    artifact = {
        "role": role,
        "path": str(path),
        "exists": exists,
        "report_type": payload.get("report_type") if isinstance(payload, dict) else None,
        "generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
    }
    if exists:
        data = path.read_bytes()
        artifact["byte_count"] = len(data)
        artifact["sha256"] = hashlib.sha256(data).hexdigest()
    return artifact


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return payload


def _load_optional_json(path: Path | None) -> dict[str, Any]:
    return _load_json(path) if path else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _split_semicolon(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _missing_field_document_count(report: dict[str, Any], field: str) -> int:
    return sum(1 for row in _list(report.get("rows")) if _split_semicolon(_dict(row).get(field)))


def _parser_missing_field_samples(report: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for row in _list(report.get("rows")):
        row_dict = _dict(row)
        missing = _split_semicolon(row_dict.get("missing_matched_fields"))
        if not missing:
            continue
        samples.append(
            {
                "document_id": row_dict.get("document_id"),
                "filename": row_dict.get("filename"),
                "missing_matched_fields": missing,
                "missing_structures": _split_semicolon(row_dict.get("missing_structures")),
                "next_structure_checklist": row_dict.get("next_structure_checklist") or "",
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _evidence_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any]) -> str:
    product = report["product_gate_summary"]
    lines = [
        "# Pilot Blocker Action Board",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Product readiness passed: `{str(product.get('passed')).lower()}`",
        f"- Product blockers / warnings: {product.get('blocking_count')} / {product.get('warning_count')}",
        f"- Ready gates: {', '.join(product.get('ready_gates') or []) or '-'}",
        f"- Blocked gates: {', '.join(product.get('blocked_gates') or []) or '-'}",
        f"- Actions: {report['action_count']}",
        "",
        "## Actions",
        "",
        "| Priority | Action | Owner | Status | Evidence | Next step |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for action in report["actions"]:
        evidence = ", ".join(f"{key}={_evidence_value(value)}" for key, value in action.get("evidence", {}).items())
        lines.append(
            f"| {action['priority']} | {_md(action['action_id'])} | {_md(action['owner'])} | "
            f"{_md(action['status'])} | {_md(evidence)} | {_md(action['next_step'])} |"
        )
    lines.extend(["", "## Safety Note", "", report["safety_note"], ""])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a read-only action board for current pilot blockers.")
    parser.add_argument("--product-readiness-report", required=True, type=Path)
    parser.add_argument("--parser-completion-board-report", type=Path)
    parser.add_argument("--table-preprocessing-claim-gate-report", type=Path)
    parser.add_argument("--temporal-policy-decision-validation-report", type=Path)
    parser.add_argument("--reapproval-decision-validation-report", type=Path)
    parser.add_argument("--reapproval-apply-plan-report", type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", required=True, type=Path)
    args = parser.parse_args(argv)

    report = build_pilot_blocker_action_board(
        product_readiness_report=args.product_readiness_report,
        parser_completion_board_report=args.parser_completion_board_report,
        table_preprocessing_claim_gate_report=args.table_preprocessing_claim_gate_report,
        temporal_policy_decision_validation_report=args.temporal_policy_decision_validation_report,
        reapproval_decision_validation_report=args.reapproval_decision_validation_report,
        reapproval_apply_plan_report=args.reapproval_apply_plan_report,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "action_count": report["action_count"],
                "out_json": str(args.out_json),
                "out_md": str(args.out_md),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
