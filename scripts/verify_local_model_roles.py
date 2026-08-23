from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow this operational CLI to run directly from a source checkout, e.g.
# ``python scripts\\verify_local_model_roles.py --help``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.claim_auditor import ClaimAuditAgent
from app.agents.grounded_qa import GroundedQwenAnswerAgent
from app.agents.model_router import QWEN3_ANSWER_MODEL, QWEN3_QUERY_MODEL, QWEN3_REVIEW_MODEL
from app.agents.ollama_runtime import OllamaRuntime
from app.agents.query_agents import QueryAnalysisAgent, QueryRewriteAgent
from app.rag.context_builder import ContextBuilder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the three local Qwen role profiles end to end.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/image_pipeline_6hour/local_model_role_verification.json"),
    )
    return parser


def verify() -> dict:
    runtime = OllamaRuntime()
    installed = sorted(runtime.installed_models(timeout_seconds=10))
    required = [QWEN3_QUERY_MODEL, QWEN3_REVIEW_MODEL, QWEN3_ANSWER_MODEL]
    missing = [model for model in required if model not in installed]
    report: dict = {
        "schema_version": "reg-rag-local-model-role-verification-v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_scope": "loopback_only",
        "required_models": required,
        "installed_required_models": [model for model in required if model in installed],
        "missing_models": missing,
        "roles": {},
        "passed": False,
    }
    if missing:
        return report

    query = "정보보안업무규정 제2조에서 접근권한 검토 주기를 알려줘"
    analysis = QueryAnalysisAgent(runtime).analyze(query, strict_model=True)
    rewrite = QueryRewriteAgent(runtime).rewrite(analysis, strict_model=True)
    report["roles"]["query_qwen3_1_7b"] = {
        "passed": analysis.analysis_mode == "local_model" and rewrite.rewrite_mode == "local_model",
        "model": analysis.model,
        "intent": analysis.intent,
        "locators": [locator.canonical for locator in analysis.locators],
        "search_query_count": len(rewrite.search_queries),
        "analysis_duration_ms": analysis.duration_ms,
        "rewrite_duration_ms": rewrite.duration_ms,
    }

    context = ContextBuilder().build(
        [
            {
                "chunk_id": "verification-chunk-1",
                "document_id": "verification-document-1",
                "text": "제2조(접근권한 관리) 정보시스템 관리자는 모든 사용자 접근권한을 분기마다 검토하여야 한다.",
                "score": 1.0,
                "approval_status": "approved",
                "approval_id": "verification-approval-1",
                "approved_content_hash": "sha256:verification-approved-content",
                "regulation_title": "정보보안업무규정",
                "regulation_version": "검증본-1",
                "part_title": "제1편 총칙",
                "chapter_title": "제2장 접근통제",
                "article_no": "제2조",
                "article_title": "접근권한 관리",
                "paragraph_no": "제1항",
                "source_page_start": 14,
                "source_page_end": 14,
            }
        ]
    )
    answer = GroundedQwenAnswerAgent(runtime).answer(
        query=query,
        context=context,
        strict_model=True,
    )
    report["roles"]["answer_qwen3_8b"] = {
        "passed": answer.answer_mode == "grounded_local" and bool(answer.claims),
        "model": answer.model,
        "answer_mode": answer.answer_mode,
        "claim_count": len(answer.claims),
        "evidence_markers": sorted(
            {context_id for claim in answer.claims for context_id in claim.evidence_context_ids}
        ),
        "duration_ms": answer.duration_ms,
    }

    audit = ClaimAuditAgent(runtime).audit(
        draft=answer,
        context=context,
        strict_model=True,
    )
    report["roles"]["claim_audit_qwen3_4b"] = {
        "passed": audit.status == "verified" and bool(audit.citations),
        "model": audit.model,
        "status": audit.status,
        "verified_claim_ids": list(audit.verified_claim_ids),
        "citation_count": len(audit.citations),
        "citation_locators": [
            {
                "regulation_title": citation.regulation_title,
                "chapter_title": citation.chapter_title,
                "article_no": citation.article_no,
                "paragraph_no": citation.paragraph_no,
                "source_page_start": citation.source_page_start,
                "has_exact_support_quote": bool(citation.support_quote),
            }
            for citation in audit.citations
        ],
        "duration_ms": audit.duration_ms,
    }
    report["passed"] = all(item.get("passed") for item in report["roles"].values())
    return report


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = verify()
    except Exception as exc:
        # A model timeout or malformed local response must still leave a
        # machine-readable, fail-closed report for the operator and CI.
        report = {
            "schema_version": "reg-rag-local-model-role-verification-v1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "endpoint_scope": "loopback_only",
            "required_models": [QWEN3_QUERY_MODEL, QWEN3_REVIEW_MODEL, QWEN3_ANSWER_MODEL],
            "installed_required_models": [],
            "missing_models": [],
            "roles": {},
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
