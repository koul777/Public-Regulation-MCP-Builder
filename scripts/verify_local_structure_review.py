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

from app.agents.local_structure_review import LocalStructureReviewAgent, apply_structure_review
from app.agents.model_router import QWEN3_REVIEW_MODEL
from app.schemas.structure import StructureNode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Qwen3 4B bounded regulation structure review.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/image_pipeline_6hour/local_structure_review_verification.json"),
    )
    return parser


def verify() -> dict[str, object]:
    node = StructureNode(
        node_id="fixture-node-3",
        document_id="fixture-document",
        node_type="article",
        number="제3조",
        title="권한관리",
        text=(
            "제3조(권한관리) 관리자는 직무에 필요한 권한만 부여하여야 한다.\n"
            "제4조(접근기록) 관리자는 접근기록을 분기마다 검토하여야 한다."
        ),
        page_start=2,
        page_end=2,
        order_index=3,
        confidence=0.61,
        warnings=["possible_merged_article_boundary"],
    )
    report = LocalStructureReviewAgent(max_nodes=4).review([node], strict_model=True)
    updated = apply_structure_review([node], report)[0]
    exact_quotes = [
        finding.source_quote in " ".join(node.text.split())
        for finding in report.findings
    ]
    passed = (
        report.model == QWEN3_REVIEW_MODEL
        and report.status == "review_required"
        and bool(report.findings)
        and all(exact_quotes)
        and any("local_structure_review:" in warning for warning in updated.warnings)
    )
    return {
        "schema_version": "reg-rag-local-structure-review-verification-v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "model": report.model,
        "status": report.status,
        "candidate_count": report.candidate_count,
        "finding_count": len(report.findings),
        "findings": [finding.model_dump(mode="json") for finding in report.findings],
        "exact_source_quote_checks": exact_quotes,
        "source_text_mutated": updated.text != node.text,
        "review_warnings": [
            warning for warning in updated.warnings if warning.startswith("local_structure_review:")
        ],
        "duration_ms": report.duration_ms,
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = verify()
    except Exception as exc:
        result = {
            "schema_version": "reg-rag-local-structure-review-verification-v1",
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
