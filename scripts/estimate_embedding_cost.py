from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingestion.vector_upsert import load_vector_records_jsonl


MILLION = Decimal("1000000")


def estimate_embedding_cost(
    records: list[dict[str, Any]],
    *,
    price_per_1m_tokens: Decimal | None = None,
    currency: str = "USD",
    budget: Decimal | None = None,
    chars_per_token: int = 4,
    token_safety_margin: Decimal = Decimal("1.0"),
    provider_model: str = "",
) -> dict[str, Any]:
    normalized_chars_per_token = max(1, int(chars_per_token))
    normalized_margin = max(Decimal("1.0"), token_safety_margin)
    raw_tokens = sum(_estimate_tokens(str(record.get("text") or ""), normalized_chars_per_token) for record in records)
    estimated_tokens = int((Decimal(raw_tokens) * normalized_margin).to_integral_value(rounding=ROUND_HALF_UP))
    result: dict[str, Any] = {
        "report_type": "embedding_cost_estimate",
        "record_count": len(records),
        "document_count": len({record.get("document_id") for record in records if record.get("document_id")}),
        "text_char_count": sum(len(str(record.get("text") or "")) for record in records),
        "chars_per_token": normalized_chars_per_token,
        "token_safety_margin": _decimal_to_string(normalized_margin),
        "estimated_input_tokens_raw": raw_tokens,
        "estimated_input_tokens": estimated_tokens,
        "provider_model": provider_model,
        "currency": currency,
        "price_per_1m_tokens": _decimal_to_string(price_per_1m_tokens),
        "estimated_total_cost": None,
        "budget": _decimal_to_string(budget),
        "budget_exceeded": False,
        "budget_evaluation_status": "not_requested" if budget is None else "not_calculated",
        "api_call_count": 0,
        "mode": "estimate_only",
    }
    if estimated_tokens > 0 and price_per_1m_tokens is None:
        result["budget_evaluation_status"] = "unknown_price" if budget is not None else "token_only"
        if budget is not None:
            result["budget_exceeded"] = None
        return result

    price = price_per_1m_tokens or Decimal("0")
    total_cost = (Decimal(estimated_tokens) / MILLION * price).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    result["estimated_total_cost"] = _decimal_to_string(total_cost)
    if budget is not None:
        result["budget_exceeded"] = total_cost > budget
        result["budget_evaluation_status"] = "estimated"
    else:
        result["budget_evaluation_status"] = "estimated"
    return result


def to_markdown(estimate: dict[str, Any]) -> str:
    currency = estimate.get("currency") or ""
    lines = [
        "# Embedding Cost Estimate",
        "",
        f"- Mode: {estimate.get('mode', 'estimate_only')}",
        f"- API calls: {estimate.get('api_call_count', 0)}",
        f"- Records: {estimate.get('record_count', 0)}",
        f"- Documents: {estimate.get('document_count', 0)}",
        f"- Text chars: {estimate.get('text_char_count', 0)}",
        f"- Estimated input tokens: {estimate.get('estimated_input_tokens', 0)}",
    ]
    if estimate.get("provider_model"):
        lines.append(f"- Provider model: {estimate.get('provider_model')}")
    if estimate.get("price_per_1m_tokens") is None:
        lines.append("- Price per 1M tokens: not provided")
        lines.append("- Estimated total cost: not calculated")
    else:
        lines.append(f"- Price per 1M tokens: {estimate.get('price_per_1m_tokens')} {currency}")
        lines.append(f"- Estimated total cost: {estimate.get('estimated_total_cost')} {currency}")
    if estimate.get("budget") is not None:
        lines.append(f"- Budget: {estimate.get('budget')} {currency}")
        lines.append(f"- Budget exceeded: {estimate.get('budget_exceeded')}")
        lines.append(f"- Budget evaluation status: {estimate.get('budget_evaluation_status')}")
    lines.append("- This estimator reads local JSONL only and does not call an embedding API.")
    return "\n".join(lines) + "\n"


def _estimate_tokens(text: str, chars_per_token: int) -> int:
    text = text.strip()
    if not text:
        return 0
    return max(1, (len(text) + chars_per_token - 1) // chars_per_token)


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
        description="Estimate future embedding-provider cost from local VectorDB JSONL records without API calls."
    )
    parser.add_argument("--records-jsonl", required=True)
    parser.add_argument("--price-per-1m-tokens", type=_decimal_arg, default=None)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--budget", type=_decimal_arg, default=None)
    parser.add_argument("--chars-per-token", type=int, default=4)
    parser.add_argument("--token-safety-margin", type=_decimal_arg, default=Decimal("1.0"))
    parser.add_argument("--provider-model", default="")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--fail-over-budget", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = load_vector_records_jsonl(Path(args.records_jsonl))
    estimate = estimate_embedding_cost(
        records,
        price_per_1m_tokens=args.price_per_1m_tokens,
        currency=args.currency,
        budget=args.budget,
        chars_per_token=args.chars_per_token,
        token_safety_margin=args.token_safety_margin or Decimal("1.0"),
        provider_model=args.provider_model,
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
