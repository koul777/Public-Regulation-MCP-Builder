from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.compare_rag_mcp_accuracy import _expected_term_hits, compare_rag_mcp_accuracy


class CompareRagMcpAccuracyTests(unittest.TestCase):
    def test_expected_term_hits_accept_spacing_variants(self) -> None:
        hits = _expected_term_hits(
            ["\ubcc4\ud45c 2-1", "\uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9\uae30\uc900\ud45c"],
            "",
            [
                {
                    "text": (
                        "[\ubcc4\ud45c2 -1]\uc758 "
                        "\uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9 \uae30\uc900\ud45c\ub97c \ud655\uc778\ud55c\ub2e4."
                    )
                }
            ],
        )

        self.assertEqual(
            ["\ubcc4\ud45c 2-1", "\uc5f0\uad6c\uc9c1 \uc784\uc6a9\uc790\uaca9\uae30\uc900\ud45c"],
            hits,
        )

    def test_compares_search_only_baseline_to_mcp_fetch_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_json = root / "accuracy.json"
            out_md = root / "accuracy.md"

            with (
                patch("scripts.compare_rag_mcp_accuracy.settings_for_mcp_project", return_value=object()),
                patch("scripts.compare_rag_mcp_accuracy.mcp_auth_context", return_value=object()),
                patch(
                    "scripts.compare_rag_mcp_accuracy.search_regulations",
                    return_value={
                        "results": [
                            {
                                "id": "result-1",
                                "title": "Demo Regulation Article 10",
                                "text": "Short search snippet mentions childcare leave.",
                                "metadata": {
                                    "document_id": "doc-demo",
                                    "chunk_id": "chunk-10",
                                    "document_name": "Demo Regulation",
                                    "regulation_title": "Demo Regulation",
                                    "article_no": "Article 10",
                                    "article_title": "Childcare Leave",
                                },
                            }
                        ],
                        "metadata": {"trace_id": "trace-demo"},
                    },
                ),
                patch(
                    "scripts.compare_rag_mcp_accuracy.fetch_regulation",
                    return_value={
                        "id": "result-1",
                        "title": "Demo Regulation Article 10",
                        "text": "Article 10 Childcare Leave may be requested within three years with allowance support.",
                        "metadata": {
                            "document_id": "doc-demo",
                            "chunk_id": "chunk-10",
                            "document_name": "Demo Regulation",
                            "institution_name": "Demo Institution",
                            "regulation_title": "Demo Regulation",
                            "article_no": "Article 10",
                            "article_title": "Childcare Leave",
                            "source_page_start": 3,
                            "source_page_end": 3,
                            "approval_id": "approval-10",
                            "profile_id": "demo-profile",
                            "source_system": "TEST",
                            "security_level": "internal",
                        },
                    },
                ),
            ):
                report = compare_rag_mcp_accuracy(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=[
                        {
                            "query": "childcare leave period and allowance",
                            "expected_terms": ["three years", "allowance"],
                            "expected_article_nos": ["Article 10"],
                            "expected_article_titles": ["Childcare Leave"],
                        }
                    ],
                    out_json=out_json,
                    out_md=out_md,
                )
                self.assertTrue(out_json.exists())
                self.assertIn("Simple RAG vs MCP Accuracy", out_md.read_text(encoding="utf-8"))

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["summary"]["mcp_passed_count"])
        self.assertEqual(1, report["summary"]["mcp_better_count"])
        self.assertEqual(0, report["summary"]["mcp_regression_count"])
        self.assertLess(
            report["items"][0]["baseline"]["metrics"]["quality_score"],
            report["items"][0]["mcp"]["metrics"]["quality_score"],
        )
        self.assertEqual(["three years", "allowance"], report["items"][0]["mcp"]["metrics"]["expected_term_hits"])
        self.assertEqual(1.0, report["items"][0]["mcp"]["metrics"]["citation_completeness_ratio"])

    def test_report_records_query_spec_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query_specs = [{"query": "childcare leave", "expected_terms": ["leave"]}]
            query_spec_source = root / "query_specs.json"
            query_spec_source.write_text(json.dumps(query_specs), encoding="utf-8")
            expected_query_spec_size = query_spec_source.stat().st_size
            expected_query_spec_sha = hashlib.sha256(query_spec_source.read_bytes()).hexdigest()
            with (
                patch("scripts.compare_rag_mcp_accuracy.settings_for_mcp_project", return_value=object()),
                patch("scripts.compare_rag_mcp_accuracy.mcp_auth_context", return_value=object()),
                patch(
                    "scripts.compare_rag_mcp_accuracy.search_regulations",
                    return_value={
                        "results": [
                            {
                                "id": "result-1",
                                "title": "Demo Regulation Article 10",
                                "text": "childcare leave",
                                "metadata": {"article_no": "Article 10", "article_title": "Childcare Leave"},
                            }
                        ],
                        "metadata": {"trace_id": "trace-demo"},
                    },
                ),
                patch(
                    "scripts.compare_rag_mcp_accuracy.fetch_regulation",
                    return_value={
                        "id": "result-1",
                        "title": "Demo Regulation Article 10",
                        "text": "Article 10 Childcare Leave covers leave.",
                        "metadata": {
                            "document_id": "doc-demo",
                            "chunk_id": "chunk-10",
                            "document_name": "Demo Regulation",
                            "article_no": "Article 10",
                            "article_title": "Childcare Leave",
                            "source_page_start": 3,
                            "approval_id": "approval-10",
                            "profile_id": "demo-profile",
                            "source_system": "TEST",
                        },
                    },
                ),
            ):
                report = compare_rag_mcp_accuracy(
                    data_dir=root / "data",
                    tenant_id="tenant-demo",
                    query_specs=query_specs,
                    query_spec_source=query_spec_source,
                )

        self.assertEqual(str(query_spec_source), report["query_spec_path"])
        self.assertEqual(1, report["query_spec_item_count"])
        self.assertEqual(expected_query_spec_size, report["query_spec_byte_count"])
        self.assertEqual(expected_query_spec_sha, report["query_spec_sha256"])

    def test_expected_article_hits_include_multi_article_chunk_candidates(self) -> None:
        with (
            patch("scripts.compare_rag_mcp_accuracy.settings_for_mcp_project", return_value=object()),
            patch("scripts.compare_rag_mcp_accuracy.mcp_auth_context", return_value=object()),
            patch(
                "scripts.compare_rag_mcp_accuracy.search_regulations",
                return_value={
                    "results": [
                        {
                            "id": "result-1",
                            "title": "Demo",
                            "text": "\uc81c15\uc870(\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900) \uad50\uc6d0\uc744 \uc784\uc6a9\ud560 \ub54c \uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900\uc740 [\ubcc4\ud45c 2]\uc5d0 \uc758\ud55c\ub2e4.",
                            "metadata": {
                                "document_id": "doc-demo",
                                "chunk_id": "chunk-multi",
                                "document_name": "Demo Regulation",
                                "article_no": "\uc81c13\uc870",
                                "article_title": "\uc2e0\uaddc\uc784\uc6a9",
                                "article_refs": ["\uc81c15\uc870"],
                            },
                        }
                    ],
                    "metadata": {"trace_id": "trace-demo"},
                },
            ),
            patch(
                "scripts.compare_rag_mcp_accuracy.fetch_regulation",
                return_value={
                    "id": "result-1",
                    "title": "Demo",
                    "text": "\uc81c15\uc870(\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900) \uad50\uc6d0\uc744 \uc784\uc6a9\ud560 \ub54c \uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900\uc740 [\ubcc4\ud45c 2]\uc5d0 \uc758\ud55c\ub2e4.",
                    "metadata": {
                        "document_id": "doc-demo",
                        "chunk_id": "chunk-multi",
                        "document_name": "Demo Regulation",
                        "institution_name": "Demo Institution",
                        "regulation_title": "\uad50\uc6d0 \uc784\uc6a9 \uc138\uce59",
                        "article_no": "\uc81c13\uc870",
                        "article_title": "\uc2e0\uaddc\uc784\uc6a9",
                        "article_refs": ["\uc81c15\uc870"],
                        "source_page_start": 10,
                        "source_page_end": 10,
                        "approval_id": "approval-demo",
                        "profile_id": "demo-profile",
                        "source_system": "TEST",
                        "security_level": "internal",
                    },
                },
            ),
        ):
            report = compare_rag_mcp_accuracy(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                query_specs=[
                    {
                        "query": "\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900",
                        "expected_terms": ["\uc5f0\uad6c\uacbd\ub825", "\uc778\uc815\uae30\uc900"],
                        "expected_article_nos": ["\uc81c15\uc870"],
                        "expected_article_titles": ["\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900"],
                    }
                ],
            )

        item = report["items"][0]["mcp"]["metrics"]
        self.assertEqual(["\uc81c15\uc870"], item["expected_article_no_hits"])
        self.assertEqual(["\uc5f0\uad6c\uacbd\ub825 \uc778\uc815\uae30\uc900"], item["expected_article_title_hits"])
        self.assertEqual(1.0, item["expected_article_no_hit_ratio"])
        self.assertEqual(1.0, item["expected_article_title_hit_ratio"])

    def test_expected_article_hits_include_clause_refs_in_appendix_text(self) -> None:
        appendix_text = "\u003c\ubcc4\ud45c1\u003e \uc608\uc0b0\uc561\ubcc4 \ud3c9\uac00\uc704\uc6d0 \uc218(\uc81c21\uc870\uc81c3\ud56d \uad00\ub828)"
        with (
            patch("scripts.compare_rag_mcp_accuracy.settings_for_mcp_project", return_value=object()),
            patch("scripts.compare_rag_mcp_accuracy.mcp_auth_context", return_value=object()),
            patch(
                "scripts.compare_rag_mcp_accuracy.search_regulations",
                return_value={
                    "results": [
                        {
                            "id": "result-1",
                            "title": "\ubcc4\ud45c1",
                            "text": appendix_text,
                            "metadata": {
                                "document_id": "doc-demo",
                                "chunk_id": "appendix-1",
                                "document_name": "Demo Regulation",
                                "regulation_title": "\uacc4\uc57d\uc5c5\ubb34\uaddc\uc815",
                            },
                        }
                    ],
                    "metadata": {"trace_id": "trace-demo"},
                },
            ),
            patch(
                "scripts.compare_rag_mcp_accuracy.fetch_regulation",
                return_value={
                    "id": "result-1",
                    "title": "\ubcc4\ud45c1",
                    "text": appendix_text,
                    "metadata": {
                        "document_id": "doc-demo",
                        "chunk_id": "appendix-1",
                        "document_name": "Demo Regulation",
                        "institution_name": "Demo Institution",
                        "regulation_title": "\uacc4\uc57d\uc5c5\ubb34\uaddc\uc815",
                        "source_page_start": 10,
                        "source_page_end": 10,
                        "approval_id": "approval-demo",
                        "profile_id": "demo-profile",
                        "source_system": "TEST",
                        "security_level": "internal",
                    },
                },
            ),
        ):
            report = compare_rag_mcp_accuracy(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                query_specs=[
                    {
                        "query": "\uc608\uc0b0\uc561\ubcc4 \ud3c9\uac00\uc704\uc6d0 \uc218",
                        "expected_terms": ["\uc608\uc0b0\uc561\ubcc4", "\ud3c9\uac00\uc704\uc6d0"],
                        "expected_article_nos": ["\uc81c21\uc870\uc81c3\ud56d"],
                    }
                ],
            )

        item = report["items"][0]["mcp"]["metrics"]
        self.assertEqual(["\uc81c21\uc870\uc81c3\ud56d"], item["expected_article_no_hits"])
        self.assertEqual(1.0, item["expected_article_no_hit_ratio"])

    def test_expect_no_evidence_passes_when_both_modes_return_no_results(self) -> None:
        with patch("scripts.compare_rag_mcp_accuracy.settings_for_mcp_project", return_value=object()), patch(
            "scripts.compare_rag_mcp_accuracy.mcp_auth_context",
            return_value=object(),
        ), patch(
            "scripts.compare_rag_mcp_accuracy.search_regulations",
            return_value={"results": [], "metadata": {"trace_id": "trace-empty"}},
        ):
            report = compare_rag_mcp_accuracy(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                query_specs=[{"query": "nonexistent rule", "expect_no_evidence": True}],
            )

        self.assertTrue(report["passed"])
        self.assertEqual(1, report["summary"]["baseline_passed_count"])
        self.assertEqual(1, report["summary"]["mcp_passed_count"])
        self.assertEqual(0, report["summary"]["mcp_regression_count"])

    def test_mcp_expected_coverage_drop_is_regression_even_when_score_improves(self) -> None:
        with (
            patch("scripts.compare_rag_mcp_accuracy.settings_for_mcp_project", return_value=object()),
            patch("scripts.compare_rag_mcp_accuracy.mcp_auth_context", return_value=object()),
            patch(
                "scripts.compare_rag_mcp_accuracy.search_regulations",
                return_value={
                    "results": [
                        {
                            "id": "result-1",
                            "title": "Demo",
                            "text": "The benefit lasts three years and includes allowance support.",
                            "metadata": {"document_id": "doc-demo"},
                        }
                    ],
                    "metadata": {"trace_id": "trace-demo"},
                },
            ),
            patch(
                "scripts.compare_rag_mcp_accuracy.fetch_regulation",
                return_value={
                    "id": "result-1",
                    "title": "Demo",
                    "text": "The benefit lasts three years.",
                    "metadata": {
                        "document_id": "doc-demo",
                        "chunk_id": "chunk-demo",
                        "document_name": "Demo Regulation",
                        "institution_name": "Demo Institution",
                        "regulation_title": "Demo",
                        "article_no": "Article 1",
                        "article_title": "Benefit",
                        "source_page_start": 1,
                        "source_page_end": 1,
                        "approval_id": "approval-demo",
                        "profile_id": "demo-profile",
                        "source_system": "TEST",
                        "security_level": "internal",
                    },
                },
            ),
        ):
            report = compare_rag_mcp_accuracy(
                data_dir=Path("data"),
                tenant_id="tenant-demo",
                query_specs=[
                    {
                        "query": "benefit duration allowance",
                        "expected_terms": ["three years", "allowance"],
                    }
                ],
            )

        item = report["items"][0]
        self.assertGreater(item["score_delta"], 0)
        self.assertTrue(item["mcp_regression"])
        self.assertFalse(item["mcp_not_worse"])
        self.assertEqual(["expected_term_hit_ratio"], item["coverage_regression_fields"])
        self.assertEqual(1, report["summary"]["mcp_regression_count"])
        self.assertFalse(report["passed"])


if __name__ == "__main__":
    unittest.main()
