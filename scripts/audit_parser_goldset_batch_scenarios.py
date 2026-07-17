from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analyze_regulation_corpus import (  # noqa: E402
    GOLDSET_SCORE_SPECS,
    build_goldset_score_payload,
    load_goldset_label_rows,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall <= 0:
        return 0.0
    return round((2 * precision * recall) / (precision + recall), 2)


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _aggregate_counts(counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    manual_total = sum(item["manual"] for item in counts.values())
    pipeline_total = sum(item["pipeline"] for item in counts.values())
    matched_total = sum(item["matched"] for item in counts.values())
    precision = _percent(matched_total, pipeline_total)
    recall = _percent(matched_total, manual_total)
    return {
        "manual_total": manual_total,
        "pipeline_total": pipeline_total,
        "matched_total": matched_total,
        "false_positive_total": max(pipeline_total - matched_total, 0),
        "false_negative_total": max(manual_total - matched_total, 0),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


def count_upper_bound(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute a count-only upper bound from current pipeline totals.

    This is deliberately not a release-grade score: it assumes every current
    pipeline unit can match a manual unit up to min(manual, pipeline). The value
    is useful for deciding whether parser work or evidence-transfer work is the
    current bottleneck.
    """

    by_structure: dict[str, dict[str, int]] = {
        structure: {"manual": 0, "pipeline": 0, "matched": 0}
        for structure in GOLDSET_SCORE_SPECS
    }
    skipped_structure_count = 0
    for document in payload.get("documents") or []:
        if not isinstance(document, dict) or document.get("excluded_from_quality_claim"):
            continue
        scores = document.get("scores") if isinstance(document.get("scores"), dict) else {}
        for structure_type in GOLDSET_SCORE_SPECS:
            score = scores.get(structure_type) if isinstance(scores, dict) else None
            if not isinstance(score, dict):
                skipped_structure_count += 1
                continue
            manual = score.get("manual_count")
            pipeline = score.get("pipeline_count")
            if not isinstance(manual, int) or not isinstance(pipeline, int):
                skipped_structure_count += 1
                continue
            by_structure[structure_type]["manual"] += manual
            by_structure[structure_type]["pipeline"] += pipeline
            by_structure[structure_type]["matched"] += min(manual, pipeline)

    structure_summary = {
        structure_type: _aggregate_counts({structure_type: counts})
        for structure_type, counts in by_structure.items()
    }
    return {
        "measurement_kind": "count_only_upper_bound_not_release_claim",
        "overall": _aggregate_counts(by_structure),
        "by_structure": structure_summary,
        "skipped_structure_count": skipped_structure_count,
        "claim_safety_note": (
            "This upper bound assumes count-level matches and does not prove unit-level identity. "
            "Use it to prioritize parser work; do not use it as a public accuracy claim."
        ),
    }


def _issue_summary(payload: dict[str, Any]) -> dict[str, Any]:
    issues = [issue for issue in payload.get("issues") or [] if isinstance(issue, dict)]
    by_code = Counter(str(issue.get("code") or "unknown") for issue in issues)
    by_structure = Counter(str(issue.get("structure_type") or "unknown") for issue in issues)
    stale_count = sum(count for code, count in by_code.items() if "stale-after-reprocess" in code)
    return {
        "issue_count": len(issues),
        "by_code": dict(sorted(by_code.items())),
        "by_structure": dict(sorted(by_structure.items())),
        "stale_after_reprocess_count": stale_count,
    }


def _scenario_status(payload: dict[str, Any], upper_bound: dict[str, Any], *, min_f1: float) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    strict_f1 = (payload.get("overall") or {}).get("f1") if isinstance(payload.get("overall"), dict) else None
    upper_f1 = (upper_bound.get("overall") or {}).get("f1") if isinstance(upper_bound.get("overall"), dict) else None
    if summary.get("ready_for_quality_claim") is True and isinstance(strict_f1, (int, float)) and strict_f1 >= min_f1:
        return "ready_for_quality_claim"
    if isinstance(upper_f1, (int, float)) and upper_f1 < min_f1:
        return "parser_improvement_required"
    if _issue_summary(payload)["stale_after_reprocess_count"] > 0:
        return "evidence_transfer_or_recheck_required"
    return "score_evidence_incomplete"


def _next_action(status: str) -> str:
    if status == "ready_for_quality_claim":
        return "Use this score report as parser goldset evidence, then rerun product readiness."
    if status == "parser_improvement_required":
        return "Improve parser/counting logic first; count-level evidence cannot reach the configured F1 threshold."
    if status == "evidence_transfer_or_recheck_required":
        return "Validate whether stale matched counts can be transferred with unit-level evidence before using this as a claim."
    return "Complete missing score evidence, then regenerate the parser goldset score."


def build_parser_goldset_batch_scenario_audit(
    *,
    labels_csv: Path,
    batch_reports: list[Path],
    workspace: Path,
    reports_dir: Path,
    min_f1: float = 90.0,
    out_json: Path | None = None,
    out_md: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    label_rows = load_goldset_label_rows(labels_csv)
    scenarios: list[dict[str, Any]] = []
    for batch_report in batch_reports:
        score_payload = build_goldset_score_payload(
            workspace,
            label_rows,
            [batch_report],
            reports_dir,
            generated_at=generated_at,
        )
        upper_bound = count_upper_bound(score_payload)
        status = _scenario_status(score_payload, upper_bound, min_f1=min_f1)
        scenarios.append(
            {
                "batch_report": str(batch_report),
                "status": status,
                "next_action": _next_action(status),
                "strict_score": {
                    "ready_for_quality_claim": score_payload.get("summary", {}).get("ready_for_quality_claim"),
                    "issue_count": score_payload.get("summary", {}).get("issue_count"),
                    "scorable_structure_count": score_payload.get("overall", {}).get("scorable_count"),
                    "overall_f1": score_payload.get("overall", {}).get("f1"),
                    "by_structure": score_payload.get("by_structure", {}),
                },
                "issue_summary": _issue_summary(score_payload),
                "count_upper_bound": upper_bound,
            }
        )

    best_strict = max(
        scenarios,
        key=lambda item: item["strict_score"].get("overall_f1")
        if isinstance(item["strict_score"].get("overall_f1"), (int, float))
        else -1,
        default=None,
    )
    best_upper = max(
        scenarios,
        key=lambda item: item["count_upper_bound"]["overall"].get("f1")
        if isinstance(item["count_upper_bound"]["overall"].get("f1"), (int, float))
        else -1,
        default=None,
    )
    report = {
        "report_type": "parser_goldset_batch_scenario_audit",
        "generated_at": generated_at or _utc_now(),
        "labels_csv": str(labels_csv),
        "min_f1": min_f1,
        "scenario_count": len(scenarios),
        "best_strict_batch_report": best_strict.get("batch_report") if best_strict else None,
        "best_strict_overall_f1": best_strict["strict_score"].get("overall_f1") if best_strict else None,
        "best_upper_bound_batch_report": best_upper.get("batch_report") if best_upper else None,
        "best_upper_bound_f1": best_upper["count_upper_bound"]["overall"].get("f1") if best_upper else None,
        "all_upper_bounds_below_threshold": not any(
            isinstance(item["count_upper_bound"]["overall"].get("f1"), (int, float))
            and item["count_upper_bound"]["overall"]["f1"] >= min_f1
            for item in scenarios
        ),
        "scenarios": scenarios,
        "safety_note": (
            "The count_upper_bound section is not a quality-claim score. It is an upper-bound diagnostic "
            "for deciding whether parser implementation work is still required."
        ),
        "api_call_count": 0,
    }
    if out_json:
        _write_json(out_json, report)
    if out_md:
        _write_text(out_md, make_markdown(report))
    return report


def make_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Parser Goldset Batch Scenario Audit",
        "",
        f"- Min F1 threshold: {report.get('min_f1')}",
        f"- Scenario count: {report.get('scenario_count')}",
        f"- Best strict F1: {report.get('best_strict_overall_f1')} ({report.get('best_strict_batch_report')})",
        f"- Best count-only upper bound F1: {report.get('best_upper_bound_f1')} ({report.get('best_upper_bound_batch_report')})",
        f"- All upper bounds below threshold: {str(report.get('all_upper_bounds_below_threshold')).lower()}",
        "",
        "## Safety Note",
        "",
        str(report.get("safety_note") or ""),
        "",
        "## Scenarios",
        "",
    ]
    for scenario in report.get("scenarios") or []:
        strict = scenario.get("strict_score", {})
        upper = scenario.get("count_upper_bound", {}).get("overall", {})
        issues = scenario.get("issue_summary", {})
        lines.extend(
            [
                f"### {scenario.get('batch_report')}",
                "",
                f"- Status: `{scenario.get('status')}`",
                f"- Strict F1: {strict.get('overall_f1')} / ready: {strict.get('ready_for_quality_claim')}",
                f"- Count-only upper bound F1: {upper.get('f1')}",
                f"- Issues: {issues.get('issue_count')} / stale-after-reprocess: {issues.get('stale_after_reprocess_count')}",
                f"- Next action: {scenario.get('next_action')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare parser goldset score scenarios across batch reports.")
    parser.add_argument("--labels-csv", required=True, type=Path)
    parser.add_argument("--batch-report", action="append", required=True, type=Path)
    parser.add_argument("--workspace", default=".", type=Path)
    parser.add_argument("--reports-dir", default="reports", type=Path)
    parser.add_argument("--min-f1", default=90.0, type=float)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--fail-if-upper-bound-below-threshold", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_parser_goldset_batch_scenario_audit(
        labels_csv=args.labels_csv,
        batch_reports=args.batch_report,
        workspace=args.workspace.resolve(),
        reports_dir=args.reports_dir,
        min_f1=args.min_f1,
        out_json=args.out_json,
        out_md=args.out_md,
    )
    print(json.dumps({"ok": True, "out_json": str(args.out_json or ""), "out_md": str(args.out_md or "")}, ensure_ascii=False))
    if args.fail_if_upper_bound_below_threshold and report.get("all_upper_bounds_below_threshold"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
