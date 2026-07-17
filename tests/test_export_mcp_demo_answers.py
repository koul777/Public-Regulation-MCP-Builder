from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from scripts.export_mcp_demo_answers import (
    _answer_text_quality_issues,
    _citation,
    _expected_article_quality_issues,
    _expected_term_quality_issues,
    export_mcp_demo_answers,
)


class ExportMcpDemoAnswersTests(unittest.TestCase):
    def test_exports_grounded_demo_answers_from_mcp_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            _seed_approved_demo_runtime(data_dir, tenant_id="tenant-mcp-demo")
            out_json = root / "demo.json"
            out_md = root / "demo.md"

            report = export_mcp_demo_answers(
                data_dir=data_dir,
                tenant_id="tenant-mcp-demo",
                tenant_storage_isolation=True,
                query_specs=[
                    {
                        "query": "육아휴직",
                        "expected_terms": ["Demo Regulation"],
                        "expected_article_nos": ["제10조"],
                        "expected_article_titles": ["육아휴직"],
                    }
                ],
                out_json=out_json,
                out_md=out_md,
            )
            self.assertTrue(out_json.exists())
            self.assertIn("MCP Demo Answers", out_md.read_text(encoding="utf-8"))

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["query_count"])
        self.assertTrue(report["items"][0]["answer"])
        self.assertTrue(report["items"][0]["citations"])
        self.assertEqual(1, report["items"][0]["supporting_result_count"])
        self.assertEqual(["Demo Regulation"], report["items"][0]["expected_term_hits"])
        self.assertEqual(1.0, report["items"][0]["expected_term_hit_ratio"])
        self.assertEqual(["제10조"], report["items"][0]["expected_article_no_hits"])
        self.assertEqual(["육아휴직"], report["items"][0]["expected_article_title_hits"])
        self.assertEqual(1.0, report["items"][0]["expected_article_no_hit_ratio"])
        self.assertEqual(1.0, report["items"][0]["expected_article_title_hit_ratio"])
        self.assertEqual(0, report["items"][0]["quality_issue_count"])
        self.assertEqual(0, report["quality_issue_count"])
        self.assertEqual(0, report["api_call_count"])

    def test_report_records_query_spec_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query_specs = [{"query": "childcare leave", "expected_terms": ["leave"]}]
            query_spec_source = root / "query_specs.json"
            query_spec_source.write_text(json.dumps(query_specs), encoding="utf-8")
            expected_query_spec_size = query_spec_source.stat().st_size
            expected_query_spec_sha = hashlib.sha256(query_spec_source.read_bytes()).hexdigest()
            with (
                patch("scripts.export_mcp_demo_answers.settings_for_mcp_project", return_value=object()),
                patch("scripts.export_mcp_demo_answers.mcp_auth_context", return_value=object()),
                patch(
                    "scripts.export_mcp_demo_answers._demo_answer",
                    return_value={
                        "query": "childcare leave",
                        "passed": True,
                        "quality_issues": [],
                        "quality_issue_count": 0,
                    },
                ),
            ):
                report = export_mcp_demo_answers(
                    data_dir=root / "data",
                    tenant_id="tenant-mcp-demo",
                    query_specs=query_specs,
                    query_spec_source=query_spec_source,
                )

        self.assertEqual(str(query_spec_source), report["query_spec_path"])
        self.assertEqual(1, report["query_spec_item_count"])
        self.assertEqual(expected_query_spec_size, report["query_spec_byte_count"])
        self.assertEqual(expected_query_spec_sha, report["query_spec_sha256"])

    def test_expect_no_evidence_passes_when_no_results_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("scripts.export_mcp_demo_answers.settings_for_mcp_project", return_value=object()),
                patch("scripts.export_mcp_demo_answers.mcp_auth_context", return_value=object()),
                patch("scripts.export_mcp_demo_answers.search_regulations", return_value={"results": []}),
            ):
                report = export_mcp_demo_answers(
                    data_dir=root / "data",
                    tenant_id="tenant-mcp-demo",
                    query_specs=[{"query": "nonexistent rule", "expect_no_evidence": True}],
                )

        self.assertTrue(report["passed"])
        self.assertTrue(report["items"][0]["expect_no_evidence"])
        self.assertEqual(0, report["items"][0]["search_result_count"])
        self.assertEqual(0, report["items"][0]["fetch_result_count"])
        self.assertEqual(0, report["items"][0]["quality_issue_count"])

    def test_expect_no_evidence_fails_when_results_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("scripts.export_mcp_demo_answers.settings_for_mcp_project", return_value=object()),
                patch("scripts.export_mcp_demo_answers.mcp_auth_context", return_value=object()),
                patch(
                    "scripts.export_mcp_demo_answers.search_regulations",
                    return_value={"results": [{"id": "result-1"}]},
                ),
                patch(
                    "scripts.export_mcp_demo_answers.fetch_regulation",
                    return_value={"id": "result-1", "text": "unrelated evidence", "metadata": {}},
                ),
            ):
                report = export_mcp_demo_answers(
                    data_dir=root / "data",
                    tenant_id="tenant-mcp-demo",
                    query_specs=[{"query": "nonexistent rule", "expect_no_evidence": True}],
                )

        self.assertFalse(report["passed"])
        self.assertEqual(
            ["expected-no-evidence-results-returned"],
            [issue["code"] for issue in report["items"][0]["quality_issues"]],
        )

    def test_answer_quality_issues_flag_metadata_and_fragments(self) -> None:
        issues = _answer_text_quality_issues(
            "\n".join(
                [
                    "승인된 규정 근거 기준입니다.",
                    "- 키워드: 육아휴직, 기간",
                    "- 의도: duration",
                    "- obligation:",
                    "- 따라 우선 활용하여야 한다.",
                    "- 제4조(경과조치) 202",
                    "- 성과연봉은 6월 및 12월에 일 시금으로 지급한다.",
                ]
            )
        )

        self.assertEqual(
            [
                "answer-metadata-label",
                "answer-metadata-label",
                "answer-metadata-label",
                "answer-fragment-line",
                "answer-fragment-line",
                "answer-bad-spacing",
            ],
            [issue["code"] for issue in issues],
        )

    def test_expected_term_quality_flags_low_coverage(self) -> None:
        issues = _expected_term_quality_issues(
            ["leave", "approval"],
            "leave is available",
            [{"document_name": "Demo Regulation", "text": "leave article"}],
            minimum_hit_ratio=1.0,
        )

        self.assertEqual("expected-term-coverage-low", issues[0]["code"])
        self.assertEqual(["approval"], issues[0]["missing_terms"])

    def test_expected_term_quality_accepts_spacing_variants(self) -> None:
        issues = _expected_term_quality_issues(
            ["\ubcc4\ud45c 2-1", "\uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9\uae30\uc900\ud45c"],
            "",
            [
                {
                    "text": (
                        "[\ubcc4\ud45c2 -1]\uc758 "
                        "\uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9 \uae30\uc900\ud45c\uc5d0 \uc758\ud55c\ub2e4."
                    )
                }
            ],
            minimum_hit_ratio=1.0,
        )

        self.assertEqual([], issues)

    def test_expected_article_quality_flags_missing_citation_article(self) -> None:
        issues = _expected_article_quality_issues(
            ["제27조의2"],
            [{"article_no": "제24조", "article_title": "연봉의 지급 방법"}],
            "article_no",
        )

        self.assertEqual("expected-article-no-missing", issues[0]["code"])
        self.assertEqual(["제27조의2"], issues[0]["missing_values"])

    def test_expected_article_quality_accepts_multi_article_chunk_candidates(self) -> None:
        citation = _citation(
            {
                "document_id": "doc-demo",
                "chunk_id": "chunk-multi",
                "text": "\uc81c15\uc870(\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900) \uad50\uc6d0\uc744 \uc784\uc6a9\ud560 \ub54c \uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900\uc740 [\ubcc4\ud45c 2]\uc5d0 \uc758\ud55c\ub2e4.",
                "article_no": "\uc81c13\uc870",
                "article_title": "\uc2e0\uaddc\uc784\uc6a9",
                "article_refs": ["\uc81c15\uc870"],
            }
        )

        self.assertEqual(
            [],
            _expected_article_quality_issues(["\uc81c15\uc870"], [citation], "article_no"),
        )
        self.assertEqual(
            [],
            _expected_article_quality_issues(["\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900"], [citation], "article_title"),
        )

    def test_expected_article_quality_accepts_clause_refs_in_appendix_text(self) -> None:
        citation = _citation(
            {
                "document_id": "doc-demo",
                "chunk_id": "appendix-1",
                "text": "\u003c\ubcc4\ud45c1\u003e \ud3c9\uac00\uc704\uc6d0 \uc218(\uc81c21\uc870\uc81c3\ud56d \uad00\ub828)",
                "article_no": "",
                "article_title": "",
            }
        )

        self.assertEqual(
            [],
            _expected_article_quality_issues(["\uc81c21\uc870\uc81c3\ud56d"], [citation], "article_no"),
        )

    def test_expected_article_quality_normalizes_rule_synonyms(self) -> None:
        citation = _citation(
            {
                "document_id": "doc-demo",
                "chunk_id": "article-1",
                "text": "\uc81c2\uc870(\uc774\uc0ac\ud68c\uc758 \uc758\uacb0\uc744 \uc694\ud558\ub294 \ub2e4\ub978 \uaddc\uc815\uc758 \uac1c\uc815)",
                "article_no": "\uc81c2\uc870",
                "article_title": "\uc774\uc0ac\ud68c\uc758 \uc758\uacb0\uc744 \uc694\ud558\ub294 \ub2e4\ub978 \uaddc\uc815\uc758 \uac1c\uc815",
            }
        )

        self.assertEqual(
            [],
            _expected_article_quality_issues(
                ["\uc774\uc0ac\ud68c\uc758 \uc758\uacb0\uc744 \uc694\ud558\ub294 \ub2e4\ub978 \uc6d0\uaddc\uc758 \uac1c\uc815"],
                [citation],
                "article_title",
            ),
        )

    def test_expected_article_quality_uses_citation_text_for_short_titles(self) -> None:
        citation = _citation(
            {
                "document_id": "doc-demo",
                "chunk_id": "appendix-2",
                "text": "\ubd80\uce59 \ubcc4\ud45c5(\uc81c20\uc870 \uad00\ub828) \uc0ad\uc81c",
                "article_no": "\uc81c20\uc870",
                "article_title": "",
            }
        )

        self.assertEqual(
            [],
            _expected_article_quality_issues(["\uc0ad\uc81c"], [citation], "article_title"),
        )


def _seed_approved_demo_runtime(data_dir: Path, *, tenant_id: str) -> None:
    settings = Settings(data_dir=data_dir, artifact_root=data_dir.parent, tenant_storage_isolation=True)
    repository_settings = settings_for_tenant(settings, tenant_id)
    repository = JsonRepository(repository_settings)
    repository.upsert_document(
        Document(
            document_id="doc_demo",
            filename="demo.pdf",
            document_name="Demo Regulation",
            file_type="pdf",
            file_hash="demo-hash",
            tenant_id=tenant_id,
            status="completed",
        )
    )
    repository.save_processing_result(
        "doc_demo",
        [],
        [
            Chunk(
                chunk_id="chunk-demo-leave",
                document_id="doc_demo",
                chunk_type="article",
                text="제10조(육아휴직) 육아휴직은 자녀 1명에 대하여 3년 이내로 신청할 수 있다.",
                retrieval_text="제10조(육아휴직) 육아휴직은 자녀 1명에 대하여 3년 이내로 신청할 수 있다.",
                source_page_start=1,
                source_page_end=1,
                metadata={
                    "document_name": "Demo Regulation",
                    "institution_name": "Demo Institution",
                    "regulation_title": "인사규정",
                    "article_no": "제10조",
                    "article_title": "육아휴직",
                    "source_page_start": 1,
                    "source_system": "TEST",
                    "source_url": "https://example.test/demo",
                    "profile_id": "demo-profile",
                    "answer_outline": ["육아휴직은 자녀 1명에 대하여 3년 이내로 신청할 수 있다."],
                },
                security_level="internal",
            )
        ],
        [],
    )
    evidence = _write_approval_evidence(data_dir.parent, repository_settings, tenant_id=tenant_id)
    auth = AuthContext(actor="tester", tenant_id=tenant_id, auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=settings):
        routes_documents.approve_review_chunks(
            "doc_demo",
            routes_documents.ApprovalRequest(
                chunk_ids=["chunk-demo-leave"],
                approval_id="approval-demo",
                security_level="internal",
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            "doc_demo",
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


def _write_approval_evidence(root: Path, settings: Settings, *, tenant_id: str) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_path = reports / "approval_worklist_current.json"
    batch_manifest_path = reports / "approval_review_batches_current.json"
    chunks = JsonRepository(settings).get_chunks("doc_demo")
    chunk_ids = [chunk.chunk_id for chunk in chunks]

    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-10T00:00:00+00:00",
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "document_count": 1,
        "total_chunks": len(chunks),
        "documents": [{"document_id": "doc_demo", "total_chunks": len(chunks)}],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = hashlib.sha256(worklist_path.read_bytes()).hexdigest()

    batch_chunks = [
        {
            "chunk_id": chunk.chunk_id,
            "review_content_hash": routes_documents._review_content_hash(chunk),
            "approval_status": chunk.approval_status,
            "review_priority_tier": "no_signal",
            "review_category": "low_risk_batch_review_candidate",
            "attention_reasons": [],
        }
        for chunk in chunks
    ]
    review_type = "low_risk_batch"
    batch_fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{batch_fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": "2026-07-10T00:00:01+00:00",
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "worklist_report": {"path": str(worklist_path), "sha256": worklist_sha256},
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": batch_fingerprint,
                "review_type": review_type,
                "review_strategy": "human_bulk_review",
                "document_id": "doc_demo",
                "chunk_count": len(chunks),
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
            }
        ],
    }
    batch_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    batch_manifest_sha256 = hashlib.sha256(batch_manifest_path.read_bytes()).hexdigest()
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": batch_manifest_sha256,
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": batch_fingerprint,
        "review_strategy": "human_bulk_review",
    }


if __name__ == "__main__":
    unittest.main()
