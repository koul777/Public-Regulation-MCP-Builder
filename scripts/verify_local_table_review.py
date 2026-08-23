from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow this operational CLI to run directly from a source checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.local_table_review import LocalTableReviewAgent, apply_table_review
from app.agents.model_router import QWEN3_REVIEW_MODEL
from app.schemas.structure import StructureNode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Qwen3 4B bounded regulation table review.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/image_pipeline_6hour/local_table_review_verification.json"),
    )
    return parser


def verify() -> dict[str, object]:
    table = StructureNode(
        node_id="fixture-table-1",
        document_id="fixture-document",
        node_type="table",
        title="접근권한 검토표",
        text=(
            "권한등급 | 검토주기\n"
            "일반 | 분기\n"
            "중요 | 월 | 정보보안담당자"
        ),
        page_start=4,
        page_end=4,
        order_index=8,
        confidence=0.72,
        warnings=["table_column_count_mismatch"],
    )
    report = LocalTableReviewAgent(max_tables=2).review([table], strict_model=True)
    updated = apply_table_review([table], report)[0]
    exact_quotes = [
        finding.source_quote in " ".join(table.text.split())
        for finding in report.findings
    ]
    passed = (
        report.model == QWEN3_REVIEW_MODEL
        and report.status in {"verified", "review_required"}
        and all(exact_quotes)
        and updated.text == table.text
    )
    return {
        "schema_version": "reg-rag-local-table-review-verification-v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "model": report.model,
        "status": report.status,
        "candidate_count": report.candidate_count,
        "finding_count": len(report.findings),
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
        "exact_source_quote_checks": exact_quotes,
        "source_text_mutated": updated.text != table.text,
        "duration_ms": report.duration_ms,
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = verify()
    except Exception as exc:
        result = {
            "schema_version": "reg-rag-local-table-review-verification-v1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "passed": False,
            "model": QWEN3_REVIEW_MODEL,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
