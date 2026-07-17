from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


MILLION = Decimal("1000000")


def load_batch_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def estimate_agent_review_cost(
    report: dict[str, Any],
    *,
    input_price_per_1m_tokens: Decimal | None = None,
    output_price_per_1m_tokens: Decimal | None = None,
    currency: str = "USD",
    budget: Decimal | None = None,
) -> dict[str, Any]:
    input_tokens = _estimated_tokens(report, "agent_review_estimated_input_tokens_total", "agent_review_estimated_input_tokens")
    output_tokens = _estimated_tokens(report, "agent_review_estimated_output_tokens_total", "agent_review_estimated_output_tokens")
    total_tokens = _estimated_tokens(report, "agent_review_estimated_total_tokens_total", "agent_review_estimated_total_tokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    selected_chunks = _selected_chunks(report)
    historical_selected_chunks = _estimated_tokens(
        report,
        "historical_agent_review_selected_total",
        "historical_agent_review_selected_count",
    )
    historical_input_tokens = _estimated_tokens(
        report,
        "historical_agent_review_estimated_input_tokens_total",
        "historical_agent_review_estimated_input_tokens",
    )
    historical_output_tokens = _estimated_tokens(
        report,
        "historical_agent_review_estimated_output_tokens_total",
        "historical_agent_review_estimated_output_tokens",
    )
    historical_total_tokens = _estimated_tokens(
        report,
        "historical_agent_review_estimated_total_tokens_total",
        "historical_agent_review_estimated_total_tokens",
    )
    if historical_total_tokens == 0:
        historical_total_tokens = historical_input_tokens + historical_output_tokens
    result: dict[str, Any] = {
        "generated_from": report.get("generated_at", ""),
        "input_count": int(report.get("input_count") or 0),
        "completed_count": int(report.get("completed_count") or 0),
        "skipped_unchanged_count": int(report.get("skipped_unchanged_count") or 0),
        "agent_review_selected_chunks": selected_chunks,
        "agent_review_estimated_input_tokens": input_tokens,
        "agent_review_estimated_output_tokens": output_tokens,
        "agent_review_estimated_total_tokens": total_tokens,
        "historical_agent_review_selected_chunks_on_reused_runs": historical_selected_chunks,
        "historical_agent_review_estimated_input_tokens_on_reused_runs": historical_input_tokens,
        "historical_agent_review_estimated_output_tokens_on_reused_runs": historical_output_tokens,
        "historical_agent_review_estimated_total_tokens_on_reused_runs": historical_total_tokens,
        "currency": currency,
        "input_price_per_1m_tokens": _decimal_to_string(input_price_per_1m_tokens),
        "output_price_per_1m_tokens": _decimal_to_string(output_price_per_1m_tokens),
        "estimated_input_cost": None,
        "estimated_output_cost": None,
        "estimated_total_cost": None,
        "budget": _decimal_to_string(budget),
        "budget_exceeded": False,
        "budget_evaluation_status": "not_requested" if budget is None else "not_calculated",
    }
    missing_input_price = input_tokens > 0 and input_price_per_1m_tokens is None
    missing_output_price = output_tokens > 0 and output_price_per_1m_tokens is None
    if missing_input_price or missing_output_price:
        result["budget_evaluation_status"] = "unknown_price"
        if budget is not None:
            result["budget_exceeded"] = None
        return result

    input_price = input_price_per_1m_tokens or Decimal("0")
    output_price = output_price_per_1m_tokens or Decimal("0")
    input_cost = (Decimal(input_tokens) / MILLION * input_price).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    output_cost = (Decimal(output_tokens) / MILLION * output_price).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    total_cost = (input_cost + output_cost).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    result["estimated_input_cost"] = _decimal_to_string(input_cost)
    result["estimated_output_cost"] = _decimal_to_string(output_cost)
    result["estimated_total_cost"] = _decimal_to_string(total_cost)
    if budget is not None:
        result["budget_exceeded"] = total_cost > budget
        result["budget_evaluation_status"] = "estimated"
    return result


def to_markdown(estimate: dict[str, Any]) -> str:
    price = estimate.get("input_price_per_1m_tokens")
    cost = estimate.get("estimated_input_cost")
    budget = estimate.get("budget")
    currency = estimate.get("currency") or ""
    lines = [
        "# Agent Review Cost Estimate",
        "",
        f"- Inputs: {estimate.get('input_count', 0)}",
        f"- Completed: {estimate.get('completed_count', 0)}",
        f"- Skipped unchanged: {estimate.get('skipped_unchanged_count', 0)}",
        f"- Selected review chunks: {estimate.get('agent_review_selected_chunks', 0)}",
        f"- Estimated input tokens: {estimate.get('agent_review_estimated_input_tokens', 0)}",
        f"- Estimated output tokens: {estimate.get('agent_review_estimated_output_tokens', 0)}",
        f"- Estimated total tokens: {estimate.get('agent_review_estimated_total_tokens', 0)}",
        f"- Historical selected chunks on reused runs: {estimate.get('historical_agent_review_selected_chunks_on_reused_runs', 0)}",
        f"- Historical input tokens on reused runs: {estimate.get('historical_agent_review_estimated_input_tokens_on_reused_runs', 0)}",
        f"- Historical output tokens on reused runs: {estimate.get('historical_agent_review_estimated_output_tokens_on_reused_runs', 0)}",
        f"- Historical total tokens on reused runs: {estimate.get('historical_agent_review_estimated_total_tokens_on_reused_runs', 0)}",
        "- Historical reused-run tokens are audit exposure only, not current-batch spend.",
    ]
    output_price = estimate.get("output_price_per_1m_tokens")
    if price is None and output_price is None:
        lines.append("- Input price: not provided")
        lines.append("- Output price: not provided")
        lines.append("- Estimated total cost: not calculated")
    else:
        lines.append(f"- Input price per 1M tokens: {price} {currency}")
        lines.append(f"- Output price per 1M tokens: {output_price} {currency}")
        lines.append(f"- Estimated input cost: {cost} {currency}")
        lines.append(f"- Estimated output cost: {estimate.get('estimated_output_cost')} {currency}")
        lines.append(f"- Estimated total cost: {estimate.get('estimated_total_cost')} {currency}")
    if budget is not None:
        lines.append(f"- Budget: {budget} {currency}")
        lines.append(f"- Budget exceeded: {estimate.get('budget_exceeded', False)}")
        lines.append(f"- Budget evaluation status: {estimate.get('budget_evaluation_status', '')}")
    return "\n".join(lines) + "\n"


def _estimated_tokens(report: dict[str, Any], total_key: str, row_key: str) -> int:
    total = report.get(total_key)
    if total not in (None, ""):
        return _to_int(total)
    return sum(_to_int(row.get(row_key)) for row in report.get("rows", []))


def _selected_chunks(report: dict[str, Any]) -> int:
    total = report.get("agent_review_selected_total")
    if total not in (None, ""):
        return _to_int(total)
    return sum(_to_int(row.get("agent_review_selected_count")) for row in report.get("rows", []))


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return 0
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _decimal_to_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _decimal_arg(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    decimal = Decimal(value)
    if decimal < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate optional AI agent-review input cost from a batch quality report."
    )
    parser.add_argument("--batch-report", required=True, help="Path to batch_quality_*.json.")
    parser.add_argument(
        "--input-price-per-1m-tokens",
        type=_decimal_arg,
        default=None,
        help="Operator-supplied model input price per 1M tokens. Omit for token-only reporting.",
    )
    parser.add_argument(
        "--output-price-per-1m-tokens",
        type=_decimal_arg,
        default=None,
        help="Operator-supplied model output price per 1M tokens.",
    )
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--budget", type=_decimal_arg, default=None, help="Optional budget in the selected currency.")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-over-budget", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = load_batch_report(Path(args.batch_report))
    estimate = estimate_agent_review_cost(
        report,
        input_price_per_1m_tokens=args.input_price_per_1m_tokens,
        output_price_per_1m_tokens=args.output_price_per_1m_tokens,
        currency=args.currency,
        budget=args.budget,
    )
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(estimate, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(to_markdown(estimate), encoding="utf-8")
    print(json.dumps(estimate, ensure_ascii=False, indent=2))
    budget_was_requested = estimate.get("budget") is not None
    budget_failed_or_unknown = estimate.get("budget_exceeded") is not False
    return 2 if args.fail_over_budget and budget_was_requested and budget_failed_or_unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
