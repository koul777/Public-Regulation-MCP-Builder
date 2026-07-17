from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_rag_retrieval import (
    evaluate_retrieval,
    load_batch_chunks_from_reports,
    load_queries,
    load_runtime_chunks,
    looks_garbled_text,
    query_spec_metadata,
    single_or_list,
    tokenize,
)


class EvaluateRagRetrievalTests(unittest.TestCase):
    def test_tokenize_keeps_korean_terms(self) -> None:
        self.assertIn("채용", tokenize("공공기관 직원 채용 절차"))
        self.assertIn("public", tokenize("Public RAG"))

    def test_evaluates_top_chunks_and_relation_support(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_hiring",
                "document_name": "채용시행세칙",
                "institution_name": "기관",
                "retrieval_text": "직원 채용은 공개경쟁시험을 원칙으로 하며 채용 절차와 방법을 공고한다.",
                "normalized_text": "직원 채용은 공개경쟁시험을 원칙으로 하며 채용 절차와 방법을 공고한다.",
            },
            {
                "document_id": "doc_b",
                "chunk_id": "chunk_other",
                "document_name": "계약규정",
                "retrieval_text": "계약 체결 절차를 정한다.",
                "normalized_text": "계약 체결 절차를 정한다.",
            },
        ]
        edges = [
            {
                "chunk_id": "chunk_hiring",
                "relation_type": "article_cites_regulation_article",
                "target_label": "인사규정 제15조",
                "evidence_type": "regulation_article_ref",
                "evidence_text": "인사규정 제15조",
                "confidence": 0.9,
            }
        ]
        queries = [
            {
                "id": "hiring",
                "question": "공공기관 직원 채용 절차와 공개경쟁 원칙은?",
                "expected_terms": ["채용", "공개경쟁", "공고"],
            }
        ]

        report = evaluate_retrieval(chunks, edges, queries, top_k=1)

        self.assertEqual(report["query_count"], 1)
        self.assertEqual(report["answerable_count"], 1)
        self.assertEqual(report["relation_supported_count"], 1)
        result = report["results"][0]
        self.assertEqual(result["top_chunks"][0]["chunk_id"], "chunk_hiring")
        self.assertEqual(result["expected_term_hit_ratio"], 1.0)
        self.assertEqual(result["relation_type_counts"], {"article_cites_regulation_article": 1})

    def test_expect_no_evidence_queries_are_counted_separately(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_hiring",
                "document_name": "hiring",
                "retrieval_text": "public hiring notice principle",
                "normalized_text": "public hiring notice principle",
            }
        ]
        queries = [
            {
                "id": "hiring",
                "query": "public hiring",
                "expected_terms": ["public", "hiring"],
            },
            {
                "id": "missing",
                "query": "library seat booking",
                "expect_no_evidence": True,
                "expected_terms": ["library", "seat"],
            },
        ]

        report = evaluate_retrieval(chunks, [], queries, top_k=1)

        self.assertEqual(2, report["query_count"])
        self.assertEqual(1, report["answerable_query_count"])
        self.assertEqual(1, report["answerable_count"])
        self.assertEqual(1.0, report["answerable_ratio"])
        self.assertEqual(1, report["expect_no_evidence_query_count"])
        self.assertEqual(1, report["no_evidence_passed_count"])
        self.assertEqual(0, report["no_evidence_failed_count"])
        self.assertTrue(report["results"][1]["expect_no_evidence"])
        self.assertFalse(report["results"][1]["answerable"])
        self.assertTrue(report["results"][1]["no_evidence_passed"])

    def test_expect_no_evidence_query_passes_when_retrieved_chunks_lack_expected_evidence(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_library",
                "document_name": "library",
                "retrieval_text": "library procedure manual",
                "normalized_text": "library procedure manual",
            }
        ]
        queries = [
            {
                "id": "missing",
                "query": "library seat booking",
                "expect_no_evidence": True,
                "expected_terms": ["seat", "booking"],
            }
        ]

        report = evaluate_retrieval(chunks, [], queries, top_k=1)

        self.assertEqual(0, report["answerable_query_count"])
        self.assertEqual(1, report["expect_no_evidence_query_count"])
        self.assertEqual(1, report["no_evidence_passed_count"])
        self.assertEqual(0, report["no_evidence_failed_count"])
        self.assertEqual(["chunk_library"], [item["chunk_id"] for item in report["results"][0]["top_chunks"]])
        self.assertFalse(report["results"][0]["answerable"])
        self.assertTrue(report["results"][0]["no_evidence_passed"])

    def test_expect_no_evidence_query_passes_when_expected_terms_are_split_across_chunks(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_benefits",
                "document_name": "benefits",
                "retrieval_text": "benefit point allowance policy",
                "normalized_text": "benefit point allowance policy",
            },
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_cards",
                "document_name": "cards",
                "retrieval_text": "cash conversion policy for card incentives",
                "normalized_text": "cash conversion policy for card incentives",
            },
        ]
        queries = [
            {
                "id": "missing",
                "query": "benefit point cash conversion policy",
                "expect_no_evidence": True,
                "expected_terms": ["benefit point", "cash conversion", "policy"],
            }
        ]

        report = evaluate_retrieval(chunks, [], queries, top_k=2)

        result = report["results"][0]
        self.assertEqual(["benefit point", "policy", "cash conversion"], result["expected_term_hits"])
        self.assertEqual(0.667, result["max_chunk_expected_term_hit_ratio"])
        self.assertEqual(1, report["no_evidence_passed_count"])
        self.assertTrue(result["no_evidence_passed"])

    def test_expect_no_evidence_query_fails_when_retrieved_chunks_match_expected_evidence(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_library",
                "document_name": "library",
                "retrieval_text": "library seat booking policy",
                "normalized_text": "library seat booking policy",
            }
        ]
        queries = [
            {
                "id": "missing",
                "query": "library seat booking",
                "expect_no_evidence": True,
                "expected_terms": ["library", "seat"],
            }
        ]

        report = evaluate_retrieval(chunks, [], queries, top_k=1)

        self.assertEqual(0, report["answerable_query_count"])
        self.assertEqual(0.0, report["answerable_ratio"])
        self.assertEqual(1, report["expect_no_evidence_query_count"])
        self.assertEqual(0, report["no_evidence_passed_count"])
        self.assertEqual(1, report["no_evidence_failed_count"])
        self.assertFalse(report["results"][0]["answerable"])
        self.assertFalse(report["results"][0]["no_evidence_passed"])
        self.assertEqual(1.0, report["results"][0]["max_chunk_expected_term_hit_ratio"])

    def test_body_and_article_title_matches_outrank_document_name_only_matches(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_scope",
                "document_name": "채용업무지침",
                "article_title": "적용범위",
                "retrieval_text": "이 지침은 직원에게 적용한다.",
                "normalized_text": "이 지침은 직원에게 적용한다.",
            },
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_hiring_process",
                "document_name": "채용업무지침",
                "article_title": "채용 절차",
                "retrieval_text": "직원 채용은 공개경쟁을 원칙으로 하며 채용 공고 후 절차를 진행한다.",
                "normalized_text": "직원 채용은 공개경쟁을 원칙으로 하며 채용 공고 후 절차를 진행한다.",
            },
        ]
        queries = [
            {
                "id": "hiring",
                "question": "직원 채용 절차와 공개경쟁 공고 원칙은?",
                "expected_terms": ["채용", "절차", "공개경쟁", "공고"],
            }
        ]

        report = evaluate_retrieval(chunks, [], queries, top_k=1)

        self.assertEqual(report["results"][0]["top_chunks"][0]["chunk_id"], "chunk_hiring_process")

    def test_flags_form_table_and_ocr_noise_candidates(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_form",
                "document_name": "수의계약 사유서",
                "chunk_type": "form",
                "retrieval_text": "수의계약 사유서 법적 근거 요청사유를 기재한다.",
                "normalized_text": "수의계약 사유서 법적 근거 요청사유를 기재한다.",
            },
            {
                "document_id": "doc_b",
                "chunk_id": "chunk_noisy",
                "document_name": "수의계약 집행기준",
                "chunk_type": "paragraph",
                "retrieval_text": "수의계약 사유서 법적 근거 업才// 兀/경구",
                "normalized_text": "수의계약 사유서 법적 근거 업才// 兀/경구",
            },
        ]
        edges = [
            {
                "chunk_id": "chunk_form",
                "relation_type": "table_has_row",
                "target_label": "법적 근거 요청사유",
                "evidence_text": "법적 근거 요청사유",
            }
        ]
        queries = [
            {
                "id": "sole_source",
                "question": "수의계약 사유서의 법적 근거와 요청사유는?",
                "expected_terms": ["수의계약", "사유서", "법적 근거", "요청사유"],
            }
        ]

        report = evaluate_retrieval(chunks, edges, queries, top_k=2)
        flag_counts = report["quality_flag_counts"]

        self.assertTrue(looks_garbled_text("업才// 兀/경구"))
        self.assertTrue(looks_garbled_text("공PI관을 감사 또는 조A昏는 지뼼회의"))
        self.assertEqual(flag_counts["form_or_appendix_candidate"], 1)
        self.assertEqual(flag_counts["table_context_candidate"], 1)
        self.assertEqual(flag_counts["ocr_or_encoding_noise"], 1)
        self.assertEqual(report["quality_warning_query_count"], 1)

    def test_garbled_chunks_are_penalized_when_clean_evidence_matches(self) -> None:
        chunks = [
            {
                "document_id": "doc_clean",
                "chunk_id": "chunk_clean",
                "document_name": "수의계약 사유서",
                "chunk_type": "form",
                "retrieval_text": "수의계약 사유서에는 법적 근거와 수의계약 요청사유를 기재한다.",
                "normalized_text": "수의계약 사유서에는 법적 근거와 수의계약 요청사유를 기재한다.",
            },
            {
                "document_id": "doc_noisy",
                "chunk_id": "chunk_noisy",
                "document_name": "수의계약 사유서",
                "chunk_type": "form",
                "retrieval_text": "수의계약 사유서에는 법적 근거와 수의계약 요청사유 업才// 兀/경구를 기재한다.",
                "normalized_text": "수의계약 사유서에는 법적 근거와 수의계약 요청사유 업才// 兀/경구를 기재한다.",
            },
        ]
        queries = [
            {
                "id": "sole_source",
                "question": "수의계약 사유서의 법적 근거와 요청사유는?",
                "expected_terms": ["수의계약", "사유서", "법적 근거", "요청사유"],
            }
        ]

        report = evaluate_retrieval(chunks, [], queries, top_k=1)

        self.assertEqual("chunk_clean", report["results"][0]["top_chunks"][0]["chunk_id"])
        self.assertEqual(0, report["quality_warning_chunk_count"])

    def test_loads_chunks_from_multiple_batch_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_paths = []
            for index in range(2):
                document_id = f"doc_{index}"
                (root / f"{document_id}.jsonl").write_text(
                    json.dumps({"document_id": document_id, "chunk_id": f"chunk_{index}"}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                report_path = root / f"batch_{index}.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "generated_at": f"2026-07-07T13:0{index}:00+00:00",
                            "rows": [
                                {
                                    "status": "completed",
                                    "document_id": document_id,
                                    "quality_json": f"{document_id}.quality.json",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                report_paths.append(report_path)

            chunks, reports = load_batch_chunks_from_reports(report_paths)

        self.assertEqual([chunk["chunk_id"] for chunk in chunks], ["chunk_0", "chunk_1"])
        self.assertEqual(len(reports), 2)
        self.assertEqual(single_or_list(["one"]), "one")
        self.assertEqual(single_or_list(["one", "two"]), ["one", "two"])

    def test_loads_approved_chunks_from_tenant_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository_dir = root / "tenants" / "tenant-a" / "repository"
            repository_dir.mkdir(parents=True)
            (repository_dir / "doc_a_chunks.json").write_text(
                json.dumps(
                    [
                        {
                            "chunk_id": "chunk-approved",
                            "metadata": {"tenant_id": "tenant-a"},
                            "approval_status": "approved",
                        },
                        {
                            "chunk_id": "chunk-rejected",
                            "metadata": {"tenant_id": "tenant-a"},
                            "approval_status": "rejected",
                        },
                        {
                            "chunk_id": "chunk-other-tenant",
                            "metadata": {"tenant_id": "tenant-b"},
                            "approval_status": "approved",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            chunks, effective_dir, isolation = load_runtime_chunks(
                root,
                tenant_id="tenant-a",
                approved_only=True,
            )

        self.assertTrue(isolation)
        self.assertEqual(root / "tenants" / "tenant-a", effective_dir)
        self.assertEqual(["chunk-approved"], [chunk["chunk_id"] for chunk in chunks])

    def test_load_queries_accepts_mcp_query_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            path.write_text(
                json.dumps(
                    [
                        {"id": "mcp_spec", "query": "MCP style question", "expected_terms": ["MCP"]},
                        {"id": "empty", "query": ""},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            queries = load_queries(path)
            metadata = query_spec_metadata(path, queries)
            byte_count = path.stat().st_size

        self.assertEqual(1, len(queries))
        self.assertEqual("MCP style question", queries[0]["query"])
        self.assertEqual(str(path), metadata["query_spec_path"])
        self.assertEqual(1, metadata["query_spec_item_count"])
        self.assertEqual(byte_count, metadata["query_spec_byte_count"])
        self.assertEqual(64, len(metadata["query_spec_sha256"]))


if __name__ == "__main__":
    unittest.main()
