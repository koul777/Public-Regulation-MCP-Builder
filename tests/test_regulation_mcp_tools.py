from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.api import routes_documents, routes_rag
from app.core.api_audit import api_audit_path
from app.core.config import Settings
from app.core.security import AuthContext
from app.mcp_server import regulation_tools
from app.mcp_server.regulation_server import create_regulation_mcp_server
from app.mcp_server.regulation_tools import (
    _FETCH_CHUNK_INDEX_CACHE,
    _mcp_relevance_guard,
    chatgpt_data_fetch_output,
    chatgpt_data_search_output,
    compare_versions,
    fetch_regulation,
    get_article,
    get_citation,
    get_document,
    get_index_status,
    get_regulation_history,
    get_table,
    list_documents,
    lookup_regulation,
    mcp_auth_context,
    search_regulations,
    settings_for_mcp_project,
    warm_mcp_runtime,
)
from app.retrieval.tokenizer import tokenize
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository


class RegulationMcpToolsTests(unittest.TestCase):
    def test_mcp_citation_uses_governing_article_for_form_chunk_without_rewriting_chunk_identity(self) -> None:
        form_result = {
            "document_id": "doc-forms",
            "chunk_id": "form-15",
            "chunk_type": "form",
            "regulation_title": "근태 관리",
            "article_no": "",
            "article_title": "",
            "governing_article_no": "제31조",
            "governing_article_title": "휴직의 운영",
            "governing_article_chunk_id": "article-31",
            "governing_article_match_ref": "별지제15호서식",
            "form_refs": ["별지제15호서식"],
            "text": "[별지 제15호서식] 휴직자 복무상황 보고서",
        }

        search_result = search_regulations.__globals__["_mcp_search_result"](form_result)
        fetch_result = search_regulations.__globals__["_mcp_fetch_result"](form_result)

        self.assertEqual(search_result["metadata"]["article_no"], "제31조")
        self.assertEqual(search_result["metadata"]["article_title"], "휴직의 운영")
        self.assertEqual(search_result["metadata"]["direct_article_no"], "")
        self.assertEqual(search_result["metadata"]["chunk_id"], "form-15")
        self.assertEqual(search_result["metadata"]["chunk_type"], "form")
        self.assertEqual(fetch_result["metadata"]["governing_article_chunk_id"], "article-31")
        self.assertIn("별지제15호서식", fetch_result["metadata"]["form_refs"])

    def test_search_metadata_is_lean_and_fetch_keeps_full_detail(self) -> None:
        result = {
            "document_id": "doc-detail",
            "chunk_id": "chunk-detail",
            "document_name": "Detail Rules",
            "source_url": "https://example.test/detail",
            "article_no": "A10",
            "article_title": "Detail",
            "answer_keywords": ["keyword-a", "keyword-b"],
            "answer_facts": [{"type": "duration", "value": "3 years", "sentence": "The period is 3 years."}],
            "answer_outline": ["Detailed answer candidate"],
            "reference_edges": [{"source": "a", "target": "b"}],
            "source_hwpx_nested_table_text_snippets": ["nested detail"],
            "source_hwp_streams": ["BodyText/Section0"],
            "table_source": "kordoc",
            "table_geometry_source": "kordoc",
            "primary_parser_table_source": "hwp_parser",
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
            "kordoc_table_promoted": True,
            "kordoc_table_promotion_review_required": True,
            "kordoc_table_unmatched_source": False,
            "kordoc_table_match": {"table_id": "kordoc-1", "score": 0.9},
            "kordoc_elapsed_ms": 12.5,
            "kordoc_input_extension": ".hwp",
            "kordoc_timeout_seconds": 120,
            "kordoc_table_inventory": {"tables": [{"title": "internal"}]},
            "parser_uncertainty_remediation_hint": "review original",
        }

        search_result = search_regulations.__globals__["_mcp_search_result"](result)
        fetch_result = search_regulations.__globals__["_mcp_fetch_result"](result)

        self.assertEqual(search_result["metadata"]["source_url"], "https://example.test/detail")
        self.assertEqual(search_result["metadata"]["article_no"], "A10")
        self.assertNotIn("answer_facts", search_result["metadata"])
        self.assertNotIn("answer_outline", search_result["metadata"])
        self.assertNotIn("answer_keywords", search_result["metadata"])
        self.assertNotIn("reference_edges", search_result["metadata"])
        self.assertNotIn("source_hwpx_nested_table_text_snippets", search_result["metadata"])
        self.assertNotIn("source_hwp_streams", search_result["metadata"])
        self.assertNotIn("kordoc_table_match", search_result["metadata"])
        self.assertNotIn("parser_uncertainty_remediation_hint", search_result["metadata"])
        for metadata in (search_result["metadata"], fetch_result["metadata"]):
            self.assertNotIn("kordoc_elapsed_ms", metadata)
            self.assertNotIn("kordoc_input_extension", metadata)
            self.assertNotIn("kordoc_timeout_seconds", metadata)
            self.assertNotIn("kordoc_table_inventory", metadata)
        self.assertEqual(search_result["metadata"]["table_source"], "kordoc")
        self.assertEqual(search_result["metadata"]["table_geometry_source"], "kordoc")
        self.assertEqual(search_result["metadata"]["primary_parser_table_source"], "hwp_parser")
        self.assertEqual(search_result["metadata"]["kordoc_table_parser_status"], "parsed")
        self.assertEqual(search_result["metadata"]["kordoc_table_count"], 1)
        self.assertTrue(search_result["metadata"]["kordoc_table_promoted"])
        self.assertTrue(search_result["metadata"]["kordoc_table_promotion_review_required"])
        self.assertFalse(search_result["metadata"]["kordoc_table_unmatched_source"])
        self.assertEqual(fetch_result["metadata"]["answer_facts"][0]["value"], "3 years")
        self.assertEqual(fetch_result["metadata"]["answer_keywords"], ["keyword-a", "keyword-b"])
        self.assertEqual(fetch_result["metadata"]["kordoc_table_match"]["table_id"], "kordoc-1")

    def test_search_and_fetch_return_only_approved_local_regulation_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )
            docs = list_documents(settings=settings, auth=mcp_auth, security_levels=["internal"])

        self.assertEqual(auth.tenant_id, "tenant-a")
        self.assertEqual(len(search["results"]), 1)
        self.assertIn("timing_ms", search["metadata"])
        self.assertEqual(
            "latest_active_version_per_regulation",
            search["metadata"]["lifecycle_selection"]["mode"],
        )
        self.assertIn("complete_lifecycle_record_count", search["metadata"]["lifecycle_selection"])
        self.assertGreaterEqual(search["metadata"]["timing_ms"]["scoring_elapsed_ms"], 0.0)
        self.assertEqual(search["results"][0]["metadata"]["chunk_id"], "approved-1")
        self.assertTrue(search["results"][0]["metadata"]["content_hash"])
        self.assertTrue(search["results"][0]["metadata"]["approved_content_hash"])
        self.assertEqual(len(search["results"][0]["metadata"]["approval_worklist_report_sha256"]), 64)
        self.assertEqual(search["results"][0]["metadata"]["approval_review_batch_manifest_path"], "reports/approval_review_batches_current.json")
        self.assertEqual(len(search["results"][0]["metadata"]["approval_review_batch_manifest_sha256"]), 64)
        self.assertTrue(search["results"][0]["metadata"]["approval_review_batch_id"].startswith("approval-"))
        self.assertEqual(len(search["results"][0]["metadata"]["approval_review_batch_chunk_fingerprint"]), 64)
        self.assertEqual(search["results"][0]["metadata"]["approval_review_strategy"], "human_bulk_review")
        self.assertEqual(search["results"][0]["metadata"]["parser_uncertainty_source"], "hwp")
        self.assertEqual(search["results"][0]["metadata"]["parser_uncertainty_risk_level"], "medium")
        self.assertEqual(
            search["results"][0]["metadata"]["parser_uncertainty_flags"],
            ["native_table_geometry_unavailable"],
        )
        self.assertNotIn("draft-1", search["results"][0]["text"])
        self.assertEqual(search["results"][0]["verbatim_text"], fetched["text"])
        self.assertTrue(search["results"][0]["verbatim"]["is_verbatim"])
        self.assertEqual(
            search["results"][0]["verbatim"]["approved_content_hash"],
            fetched["metadata"]["approved_content_hash"],
        )
        self.assertIn("Grounding rules", search["metadata"]["answer_guidance"])
        self.assertIn("Grounding rules", fetched["metadata"]["answer_guidance"])
        self.assertIn("육아휴직", fetched["text"])
        self.assertEqual(fetched["verbatim_text"], fetched["text"])
        self.assertEqual(fetched["verbatim"]["source"], "approved_local_regulation_chunk")
        self.assertEqual(fetched["metadata"]["approval_id"], "approval-mcp")
        self.assertTrue(fetched["metadata"]["content_hash"])
        self.assertTrue(fetched["metadata"]["approved_content_hash"])
        self.assertTrue(fetched["metadata"]["approval_review_batch_id"].startswith("approval-"))
        self.assertEqual(fetched["metadata"]["parser_uncertainty_recommendation"], "review_tables_and_appendices")
        self.assertEqual(search["results"][0]["metadata"]["profile_id"], "public_portal-test-profile")
        self.assertEqual(search["results"][0]["metadata"]["source_system"], "PUBLIC_PORTAL")
        self.assertEqual(search["results"][0]["metadata"]["source_url"], "https://example.test/public_portal/doc_mcp")
        self.assertEqual(fetched["metadata"]["profile_id"], "public_portal-test-profile")
        self.assertEqual(fetched["metadata"]["source_record_id"], "record-doc-mcp")
        self.assertEqual(fetched["metadata"]["source_file_id"], "file-doc-mcp")
        self.assertIn("duration", search["results"][0]["metadata"]["answer_intents"])
        self.assertIn("duration", fetched["metadata"]["answer_intents"])
        self.assertEqual(fetched["metadata"]["answer_facts"][0]["value"], "3년")
        self.assertTrue(fetched["url"].startswith("govreg://documents/"))
        self.assertEqual(docs["documents"][0]["document_id"], "doc_mcp")

    def test_lookup_prefers_direct_document_and_uses_rag_only_after_direct_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            records = routes_rag._load_local_vector_records(settings, mcp_auth)

            direct = lookup_regulation(
                settings=settings,
                auth=mcp_auth,
                query="approved regulation",
                document_id="doc_mcp",
                security_levels=["internal"],
            )
            fallback = lookup_regulation(
                settings=settings,
                auth=mcp_auth,
                query=str(records[0]["text"]),
                security_levels=["internal"],
            )

        self.assertEqual(direct["metadata"]["retrieval_mode"], "direct_lookup")
        self.assertTrue(direct["metadata"]["direct_lookup_attempted"])
        self.assertTrue(direct["metadata"]["direct_lookup_hit"])
        self.assertFalse(direct["metadata"]["fallback_used"])
        self.assertTrue(direct["results"])
        self.assertTrue(direct["results"][0]["verbatim"]["is_verbatim"])
        self.assertEqual(fallback["metadata"]["retrieval_mode"], "rag_fallback")
        self.assertFalse(fallback["metadata"]["direct_lookup_attempted"])
        self.assertTrue(fallback["metadata"]["fallback_used"])
        self.assertTrue(fallback["results"])

    def test_mcp_search_and_fetch_fail_closed_on_chunk_only_revocation_with_stale_sidecar(self) -> None:
        for revoked_status in ("rejected", "security_blocked", "superseded"):
            with self.subTest(revoked_status=revoked_status), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(data_dir=Path(tmp) / "data")
                _prepare_mcp_indexed_document(settings)
                mcp_auth = mcp_auth_context(tenant_id="tenant-a")
                vector_path = routes_rag._local_vector_path(settings, mcp_auth)
                records = [
                    json.loads(line)
                    for line in vector_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                search_query = str(records[0]["text"])
                _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id="tenant-a")
                routes_rag._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
                _FETCH_CHUNK_INDEX_CACHE.clear()

                before = search_regulations(
                    settings=settings,
                    auth=mcp_auth,
                    query=search_query,
                    security_levels=["internal"],
                )
                self.assertEqual(1, len(before["results"]))
                result_id = before["results"][0]["id"]

                # Simulate failure after the authoritative chunk write but
                # before the review journal/manifest write and vector removal.
                repository = JsonRepository(settings)
                chunks = repository.get_chunks("doc_mcp")
                chunks[0] = chunks[0].model_copy(update={"approval_status": revoked_status})
                repository.save_chunks("doc_mcp", chunks)

                after = search_regulations(
                    settings=settings,
                    auth=mcp_auth,
                    query=search_query,
                    security_levels=["internal"],
                )
                with self.assertRaisesRegex(ValueError, "not available"):
                    fetch_regulation(
                        settings=settings,
                        auth=mcp_auth,
                        result_id=result_id,
                        security_levels=["internal"],
                    )

                self.assertEqual([], after["results"])

    def test_mcp_search_and_fetch_fail_closed_on_chunk_only_acl_tightening_with_stale_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            vector_path = routes_rag._local_vector_path(settings, mcp_auth)
            records = [
                json.loads(line)
                for line in vector_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            search_query = str(records[0]["text"])
            _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id="tenant-a")
            routes_rag._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
            _FETCH_CHUNK_INDEX_CACHE.clear()

            before = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query=search_query,
                security_levels=["internal"],
            )
            self.assertEqual(1, len(before["results"]))
            result_id = before["results"][0]["id"]

            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc_mcp")
            chunks[0] = chunks[0].model_copy(update={"department_acl": ["legal"]})
            repository.save_chunks("doc_mcp", chunks)

            after = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query=search_query,
                security_levels=["internal"],
            )
            with self.assertRaisesRegex(ValueError, "not available"):
                fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual([], after["results"])

    def test_mcp_fetch_result_id_does_not_cross_tenant_vector_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            tenant_a_auth = mcp_auth_context(tenant_id="tenant-a")
            tenant_b_auth = mcp_auth_context(tenant_id="tenant-b")
            records = routes_rag._load_local_vector_records(settings, tenant_a_auth)
            search = search_regulations(
                settings=settings,
                auth=tenant_a_auth,
                query=str(records[0]["text"]),
                security_levels=["internal"],
            )

            with self.assertRaisesRegex(ValueError, "not available"):
                fetch_regulation(
                    settings=settings,
                    auth=tenant_b_auth,
                    result_id=search["results"][0]["id"],
                    security_levels=["internal"],
                )

    def test_chatgpt_data_metadata_profile_hides_internal_citation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
                metadata_profile="chatgpt-data",
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
                metadata_profile="chatgpt-data",
            )

        internal_keys = {
            "source_record_id",
            "source_file_id",
            "approval_worklist_report_sha256",
            "approval_review_batch_manifest_path",
            "approval_review_batch_manifest_sha256",
            "approval_review_batch_id",
            "approval_review_batch_chunk_fingerprint",
            "approval_review_strategy",
        }
        self.assertNotIn("tenant_id", search["metadata"])
        self.assertEqual(
            "https://example.test/public_portal/doc_mcp",
            search["results"][0]["url"],
        )
        self.assertEqual(
            "https://example.test/public_portal/doc_mcp",
            fetched["url"],
        )
        for metadata in (search["results"][0]["metadata"], fetched["metadata"]):
            for key in internal_keys:
                self.assertNotIn(key, metadata)
            self.assertEqual(metadata["approval_id"], "approval-mcp")
            self.assertTrue(metadata["approved_content_hash"])
            self.assertEqual(metadata["profile_id"], "public_portal-test-profile")
            self.assertEqual(metadata["source_system"], "PUBLIC_PORTAL")
            self.assertEqual(metadata["source_url"], "https://example.test/public_portal/doc_mcp")

    def test_mcp_invalid_metadata_profile_fails_before_success_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with self.assertRaisesRegex(ValueError, "metadata_profile must be full or chatgpt-data"):
                search_regulations(
                    settings=settings,
                    auth=mcp_auth,
                    query="no matching primary anchor",
                    security_levels=["internal"],
                    metadata_profile="external",
                )
            valid_search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )
            with self.assertRaisesRegex(ValueError, "metadata_profile must be full or chatgpt-data"):
                fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=valid_search["results"][0]["id"],
                    security_levels=["internal"],
                    metadata_profile="external",
                )
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        search_rows = [row for row in rows if row["action"] == "mcp.search"]
        fetch_rows = [row for row in rows if row["action"] == "mcp.fetch"]
        self.assertEqual(["failure", "success"], [row["outcome"] for row in search_rows])
        self.assertEqual(1, len(fetch_rows))
        self.assertEqual("failure", fetch_rows[0]["outcome"])
        self.assertEqual(400, fetch_rows[0]["status_code"])

    def test_mcp_relevance_guard_refuses_missing_primary_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658 \uaddc\uc815\uc774 \uc788\ub098\uc694?",
            [
                {
                    "score": 2.0,
                    "text": "\ud604\uae08 \uc804\ud658 \uaddc\uc815\uc740 \ud68c\uacc4 \ucc98\ub9ac \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                    "regulation_title": "\ud68c\uacc4 \uaddc\uc815",
                    "article_title": "\ud604\uae08 \uc804\ud658",
                }
            ],
        )

        self.assertTrue(guard["refused"])
        self.assertEqual("insufficient_relevance", guard["refusal_reason"])
        self.assertEqual("missing_primary_query_anchor", guard["refusal_detail"])
        self.assertEqual("\ubcf5\uc9c0\ud3ec\uc778\ud2b8", guard["primary_anchor_token"])
        self.assertFalse(guard["primary_anchor_hit"])

    def test_mcp_relevance_guard_keeps_compound_anchor_after_tokenizer_warmup(self) -> None:
        tokenize("\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658")
        guard = _mcp_relevance_guard(
            "\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658 \uaddc\uc815\uc774 \uc788\ub098\uc694?",
            [
                {
                    "score": 2.0,
                    "text": "\ud604\uae08 \uc804\ud658 \uaddc\uc815\uc740 \ud68c\uacc4 \ucc98\ub9ac \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                    "regulation_title": "\ud68c\uacc4 \uaddc\uc815",
                    "article_title": "\ud604\uae08 \uc804\ud658",
                }
            ],
        )

        self.assertTrue(guard["refused"])
        self.assertEqual("\ubcf5\uc9c0\ud3ec\uc778\ud2b8", guard["primary_anchor_token"])

    def test_mcp_relevance_guard_allows_matching_primary_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc721\uc544\ud734\uc9c1 \uc2e0\uccad \uc808\ucc28",
            [
                {
                    "score": 3.0,
                    "text": "\uc721\uc544\ud734\uc9c1 \uc2e0\uccad \uc808\ucc28\ub294 \uc2b9\uc778\ub41c \uaddc\uc815\uc5d0 \ub530\ub978\ub2e4.",
                    "regulation_title": "\uc778\uc0ac \uaddc\uc815",
                    "article_title": "\uc721\uc544\ud734\uc9c1",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_normalize_query_token_strips_stacked_particles(self) -> None:
        # The relevance guard tokenizes with the regex fallback on cold start,
        # which removes only a single trailing particle.  The normalizer must
        # strip stacked particles fully ("명부에는" -> "명부") so a malformed
        # token like "명부에" is never chosen as the primary anchor.  This is
        # kiwi-independent, so it locks the cold-start behavior deterministically.
        self.assertEqual("명부", regulation_tools._mcp_normalize_query_token("명부에는"))
        self.assertEqual("겸직자", regulation_tools._mcp_normalize_query_token("겸직자에게는"))
        # A single trailing particle is still stripped as before.
        self.assertEqual("겸직자", regulation_tools._mcp_normalize_query_token("겸직자를"))

    def test_mcp_relevance_guard_allows_spaced_table_header_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "겸직자 명부 서식에는 어떤 항목을 기록하나요?",
            [
                {
                    "score": 87.0,
                    "text": "[별지 제6호 서식]\n겸 직 자 명 부\n번호 소 속 직 위 성 명 기 간 겸직기관 겸직직위 비 고",
                    "regulation_title": "복무규정",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_allows_korean_compound_token_match(self) -> None:
        guard = _mcp_relevance_guard(
            "\uad8c\ud55c \uc704\uc784",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c7\uc870(\uc704\uc784\uc804\uacb0) \uad8c\ud55c\uc704\uc784\uc804\uacb0\uaddc\uc815\uc5d0 \ub530\ub77c \ucc98\ub9ac\ud55c\ub2e4.",
                    "regulation_title": "\uad8c\ud55c\uc704\uc784\uc804\uacb0\uaddc\uc815",
                    "article_title": "\uc704\uc784\uc804\uacb0",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_allows_hiring_query_when_result_uses_appointment_term(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc804\uc784 \uad50\uc6d0 \ucc44\uc6a9 \uc808\ucc28\ub294 \uc5b4\ub5bb\uac8c \uc9c4\ud589\ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uad50\uc6d0 \uc784\uc6a9 \uc138\uce59\uc5d0 \ub530\ub77c \uc2e0\uaddc\uc784\uc6a9 \ud6c4\ubcf4\uc790 \uc2ec\uc0ac\ub97c \uc9c4\ud589\ud55c\ub2e4.",
                    "regulation_title": "\uad50\uc6d0 \uc784\uc6a9 \uc138\uce59",
                    "article_title": "\uc2e0\uaddc\uc784\uc6a9\uc758 \uc2dc\uae30",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_allows_hiring_definition_when_domain_terms_overlap(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc804\uc784 \uad50\uc6d0 \ucc44\uc6a9 \uc808\ucc28\ub294 \uc5b4\ub5bb\uac8c \uc9c4\ud589\ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c38\uc870(\uad50\uc6d0) \uad50\uc6d0\uc740 \ucd1d\uc7a5, \uad50\uc218, \ubd80\uad50\uc218, \uc870\uad50\uc218, \uc804\uc784\uac15\uc0ac\ub85c \ud55c\ub2e4.",
                    "regulation_title": "\uc778\uc0ac\uaddc\uc815",
                    "article_title": "\uad50\uc6d0",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_uses_base_anchor_for_inflected_domain_term(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc721\uc544\ud734\uc9c1\uc758 \uc694\uac74, \uae30\uac04, \uc218\ub2f9\uc740 \uc5b4\ub5bb\uac8c \ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c30\uc870(\ud734\uc9c1 \uae30\uac04) \uc81c29\uc870 \uc81c3\ud56d\uc5d0 \ub530\ub978 \ud734\uc9c1 \uae30\uac04\uc740 \uc790\ub140 1\uba85\uc5d0 \ub300\ud558\uc5ec 3\ub144 \uc774\ub0b4\ub85c \ud55c\ub2e4.",
                    "regulation_title": "\uc778\uc0ac\uaddc\uc815",
                    "article_title": "\ud734\uc9c1 \uae30\uac04",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_ignores_question_ending_as_primary_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc131\uacfc\uc5f0\ubd09\uc740 \uc5b8\uc81c \uc5b4\ub5a4 \ubc29\uc2dd\uc73c\ub85c \uc9c0\uae09\ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c24\uc870(\uc5f0\ubd09\uc758 \uc9c0\uae09 \ubc29\ubc95) \uc131\uacfc\uc5f0\ubd09\uc740 6\uc6d4\uacfc 12\uc6d4\uc5d0 \uc774\ub4f1\ubd84\ud558\uc5ec \uc9c0\uae09\ud55c\ub2e4.",
                    "regulation_title": "\uad50\uc9c1\uc6d0\ubcf4\uc218\uaddc\uc815",
                    "article_title": "\uc5f0\ubd09\uc758 \uc9c0\uae09 \ubc29\ubc95",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_ignores_plain_instruction_word(self) -> None:
        guard = _mcp_relevance_guard(
            "\ud734\uc9c1\uc758 \uc885\ub958\uc640 \uc808\ucc28\uc5d0 \ub300\ud574\uc11c \uc54c\ub824\uc918",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c31\uc870(\ud734\uc9c1\uc758 \uc6b4\uc601) \ud734\uc9c1 \uae30\uac04 \uc911 \uc0ac\uc720\uac00 \uc18c\uba78\ub418\uba74 \ubcf5\uc9c1\uc744 \uba85\ud55c\ub2e4.",
                    "regulation_title": "\uc778\uc0ac\uaddc\uc815",
                    "article_title": "\ud734\uc9c1\uc758 \uc6b4\uc601",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_search_refuses_irrelevant_query_before_fetch_ids_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_cash_conversion",
                "\ud604\uae08 \uc804\ud658 \uaddc\uc815\uc740 \ud68c\uacc4 \ucc98\ub9ac \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                "approval-cash-conversion",
                auth,
                metadata={
                    "article_no": "\uc81c1\uc870",
                    "article_title": "\ud604\uae08 \uc804\ud658",
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658 \uaddc\uc815\uc774 \uc788\ub098\uc694?",
                security_levels=["internal"],
            )

        self.assertEqual([], search["results"])
        self.assertTrue(search["metadata"]["refused"])
        self.assertEqual("insufficient_relevance", search["metadata"]["refusal_reason"])
        self.assertEqual("missing_primary_query_anchor", search["metadata"]["refusal_detail"])
        self.assertEqual("approved_local_regulation_db", search["metadata"]["source"])
        self.assertEqual(0, search["metadata"]["result_count"])

    def test_synthetic_public_portal_chunks_are_invisible_until_approved_and_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_public_portal_visibility",
                    filename="public_portal_visibility.pdf",
                    document_name="PUBLIC_PORTAL Visibility Rule",
                    file_type="pdf",
                    file_hash="hash-public_portal-visibility",
                    tenant_id="tenant-a",
                    institution_name="Synthetic PUBLIC_PORTAL Institution",
                    apba_id="C9999",
                    source_system="PUBLIC_PORTAL",
                    source_url="https://example.test/public_portal/doc_public_portal_visibility",
                    source_record_id="record-public_portal-visibility",
                    source_file_id="file-public_portal-visibility",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_public_portal_visibility",
                [],
                [
                    Chunk(
                        chunk_id="public_portal-approved-candidate",
                        document_id="doc_public_portal_visibility",
                        chunk_type="article",
                        text="public_portal approved visible token may be shown only after approval.",
                        retrieval_text="public_portal approved visible token may be shown only after approval.",
                        metadata={
                            "source_system": "PUBLIC_PORTAL",
                            "source_record_id": "record-public_portal-visibility",
                            "source_file_id": "file-public_portal-visibility",
                            "profile_id": "public_portal-synthetic-profile",
                            "regulation_title": "PUBLIC_PORTAL Visibility Rule",
                            "article_no": "Article 1",
                            "article_title": "Approved candidate",
                        },
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="public_portal-draft-only",
                        document_id="doc_public_portal_visibility",
                        chunk_type="article",
                        text="public_portal draft hidden token must never be returned.",
                        retrieval_text="public_portal draft hidden token must never be returned.",
                        metadata={
                            "source_system": "PUBLIC_PORTAL",
                            "source_record_id": "record-public_portal-visibility",
                            "source_file_id": "file-public_portal-visibility",
                            "profile_id": "public_portal-synthetic-profile",
                            "regulation_title": "PUBLIC_PORTAL Visibility Rule",
                            "article_no": "Article 2",
                            "article_title": "Draft only",
                        },
                        security_level="internal",
                    ),
                ],
                [],
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            before = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="public_portal approved visible token",
                security_levels=["internal"],
            )

            admin_auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_public_portal_visibility",
                chunks=[chunk for chunk in repository.get_chunks("doc_public_portal_visibility") if chunk.chunk_id == "public_portal-approved-candidate"],
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_public_portal_visibility",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["public_portal-approved-candidate"],
                        approval_id="approval-public_portal-visibility",
                        security_level="internal",
                        **evidence,
                    ),
                    admin_auth,
                )
                routes_documents.index_document(
                    "doc_public_portal_visibility",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    admin_auth,
                )

            after = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="public_portal approved visible token",
                security_levels=["internal"],
            )
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([], before["results"])
        self.assertEqual(1, len(after["results"]))
        self.assertEqual("public_portal-approved-candidate", after["results"][0]["metadata"]["chunk_id"])
        self.assertEqual("PUBLIC_PORTAL", after["results"][0]["metadata"]["source_system"])
        self.assertEqual("C9999", after["results"][0]["metadata"]["apba_id"])
        self.assertEqual("record-public_portal-visibility", after["results"][0]["metadata"]["source_record_id"])
        self.assertIn("public_portal approved visible token", after["results"][0]["text"])
        self.assertNotIn("public_portal draft hidden token", after["results"][0]["text"])
        self.assertEqual(["public_portal-approved-candidate"], [record["chunk_id"] for record in vector_records])
        self.assertTrue(vector_records[0]["metadata"]["approval_id"])
        self.assertEqual("approved", vector_records[0]["metadata"]["approval_status"])
        self.assertEqual("C9999", vector_records[0]["metadata"]["apba_id"])

    def test_mcp_metadata_exposes_hwpx_complex_structure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_hwpx_evidence",
                "Nested table evidence appears in this approved regulation chunk.",
                "approval-hwpx-evidence",
                auth,
                metadata={
                    "article_no": "A1",
                    "article_title": "HWPX evidence",
                    "source_hwpx_block_types": ["table"],
                    "source_xml_files": ["Contents/header.xml"],
                    "source_xml_roles": ["metadata"],
                    "source_hwpx_parser_review_flags": ["nested_table"],
                    "source_hwpx_xml_block_indices": [312, 313, 314],
                    "source_hwpx_nested_table_text_snippets": ["Nested table evidence"],
                    "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                    "source_hwp_streams": ["BodyText/Section0"],
                    "source_hwp_section_indices": [1],
                    "source_hwp_native_table_geometry": False,
                    "pdf_embedded_image_pages": [8],
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="Nested table evidence",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )

        self.assertEqual(search["results"][0]["metadata"]["source_hwpx_block_types"], ["table"])
        self.assertEqual(search["results"][0]["metadata"]["source_hwpx_parser_review_flags"], ["nested_table"])
        self.assertEqual(search["results"][0]["metadata"]["source_xml_roles"], ["metadata"])
        self.assertEqual(fetched["metadata"]["source_xml_files"], ["Contents/header.xml"])
        self.assertEqual(fetched["metadata"]["source_hwpx_xml_block_indices"], [312, 313, 314])
        self.assertEqual(fetched["metadata"]["source_hwpx_nested_table_text_snippets"], ["Nested table evidence"])
        self.assertEqual(fetched["metadata"]["source_hwp_extraction_modes"], ["legacy_ole_para_text_only"])
        self.assertEqual(fetched["metadata"]["source_hwp_streams"], ["BodyText/Section0"])
        self.assertEqual(fetched["metadata"]["source_hwp_section_indices"], [1])
        self.assertFalse(fetched["metadata"]["source_hwp_native_table_geometry"])
        self.assertEqual(fetched["metadata"]["pdf_embedded_image_pages"], [8])

    def test_fetch_validates_only_the_requested_vector_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )

            with patch.object(
                routes_rag,
                "_record_visible_to_request",
                wraps=routes_rag._record_visible_to_request,
            ) as visible_check:
                fetched = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=search["results"][0]["id"],
                    security_levels=["internal"],
                )

        self.assertEqual(fetched["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(visible_check.call_count, 1)

    def test_fetch_reuses_chunk_index_for_repeated_result_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )
            result_id = search["results"][0]["id"]

            _FETCH_CHUNK_INDEX_CACHE.clear()
            with patch.object(
                routes_rag,
                "_load_local_vector_records",
                wraps=routes_rag._load_local_vector_records,
            ) as load_records:
                first = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )
                second = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual(first["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(second["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(0, load_records.call_count)

    def test_fetch_finds_requested_vector_record_without_full_vector_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            result_id = search_regulations.__globals__["_encode_result_id"](
                document_id="doc_mcp",
                chunk_id="approved-1",
            )
            _FETCH_CHUNK_INDEX_CACHE.clear()
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()

            with patch.object(
                routes_rag,
                "_load_local_vector_records",
                wraps=routes_rag._load_local_vector_records,
            ) as load_records:
                fetched = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual(fetched["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(0, load_records.call_count)

    def test_visible_record_by_chunk_hides_superseded_but_keeps_current(self) -> None:
        # fetch resolves a single chunk by id and must not serve a superseded or
        # repealed version as current evidence just because the chunk itself is
        # still approved.  The currency gate runs over the one fetched record so
        # the targeted lookup is preserved (no full vector load).
        def _record(document_id: str, chunk_id: str, *, status: str, effective_to: str | None) -> dict:
            return {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "text": f"{document_id} 본문: 육아휴직 3년.",
                "metadata": {
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "approval_status": "approved",
                    "regulation_id": "reg-x",
                    "regulation_version": "v1" if status != "approved" else "v2",
                    "effective_from": "2024-01-01" if status != "approved" else "2025-01-01",
                    "effective_to": effective_to,
                    "regulation_status": status,
                    "profile_id": "institution-a",
                },
            }

        superseded = _record("doc-old", "old-1", status="superseded", effective_to="2025-01-01")
        current = _record("doc-new", "new-1", status="approved", effective_to=None)

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            def resolve(*_args, document_id: str, chunk_id: str, **_kwargs):
                return superseded if document_id == "doc-old" else current

            with (
                patch.object(regulation_tools, "_indexed_vector_record_by_chunk", side_effect=resolve),
                patch.object(regulation_tools, "_validate_mcp_security_scope"),
                patch.object(
                    regulation_tools.routes_rag,
                    "get_visible_record_by_chunk",
                    side_effect=lambda *, candidate, **_kwargs: candidate,
                ),
            ):
                hidden = regulation_tools._visible_record_by_chunk(
                    settings=settings,
                    auth=mcp_auth,
                    document_id="doc-old",
                    chunk_id="old-1",
                    security_levels=["internal"],
                    department_ids=[],
                    profile_id="institution-a",
                )
                kept = regulation_tools._visible_record_by_chunk(
                    settings=settings,
                    auth=mcp_auth,
                    document_id="doc-new",
                    chunk_id="new-1",
                    security_levels=["internal"],
                    department_ids=[],
                    profile_id="institution-a",
                )

        self.assertIsNone(hidden)
        self.assertIsNotNone(kept)
        self.assertEqual("new-1", kept["chunk_id"])

    def test_vector_record_loader_streams_jsonl_without_read_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()

            with patch.object(Path, "read_text", side_effect=AssertionError("read_text should not load vector JSONL")):
                records = routes_rag._load_local_vector_records(settings, mcp_auth)

        self.assertEqual(["approved-1"], [record["chunk_id"] for record in records])

    def test_vector_record_loader_avoids_concurrent_cache_stampede(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()
            read_count = 0
            original_iter = routes_rag._iter_local_vector_lines

            def slow_iter(path: Path):
                nonlocal read_count
                read_count += 1
                time.sleep(0.05)
                yield from original_iter(path)

            with patch.object(routes_rag, "_iter_local_vector_lines", side_effect=slow_iter):
                with ThreadPoolExecutor(max_workers=5) as executor:
                    results = list(
                        executor.map(
                            lambda _: routes_rag._load_local_vector_records(settings, mcp_auth),
                            range(5),
                        )
                    )

        self.assertEqual(1, read_count)
        self.assertTrue(all([record["chunk_id"] for record in records] == ["approved-1"] for records in results))

    def test_mcp_metadata_uses_article_heading_when_stored_title_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_pay",
                (
                    "제32조 <삭제 2021.3.31.>\n"
                    "제33조(육아휴직수당) 30일 이상 휴직한 교직원의 육아휴직수당은 "
                    "기본연봉월액의 78퍼센트로 한다."
                ),
                "approval-pay",
                auth,
                metadata={
                    "article_no": "제32조",
                    "article_title": "삭제",
                    "regulation_title": "교직원보수규정",
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직수당",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )

        self.assertEqual("제33조", search["results"][0]["metadata"]["article_no"])
        self.assertEqual("육아휴직수당", search["results"][0]["metadata"]["article_title"])
        self.assertEqual("제33조", fetched["metadata"]["article_no"])
        self.assertEqual("육아휴직수당", fetched["metadata"]["article_title"])
        self.assertIn("교직원보수규정 제33조 육아휴직수당", fetched["title"])

    def test_mcp_metadata_cleans_answer_profile_spacing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_noisy_profile",
                "③원장은 지원 마감일 전까지 15일 이상 지원자격 등에 관한 사항을 공고한다.",
                "approval-noisy-profile",
                auth,
                metadata={
                    "article_no": "제7조",
                    "article_title": "신규임용의 시기",
                    "chapter_title": "신규 임용",
                    "answer_profile_version": "reg-rag-answer-profile-v1",
                    "answer_facts": [
                        {
                            "type": "duration",
                            "value": "15일이상",
                            "sentence": "③원장은 지원자격 등 에 관한 사항을 효과적인 방법 으로 공고한다.<2011.11.10.>",
                        }
                    ],
                    "answer_outline": [
                        "③원장은 지원자격 등 에 관한 사항을 효과적인 방법 으로 공고한다.",
                        "신규임용은 3년이내에 하는 것을 원칙으 로 한다.",
                        "제27조의2(성과연봉 지급대상 제외) 평가대상 기간 중 중징계 처분을 받거나 다 음과 같은 사유로 징계를 받은 경우 제외한다.",
                        "제44조(술에 취한 상태에서의 운전금지)제1항에 따른 음주운 전 또는 음주측정에 대한 불응 <2022.12.28., 2025.1 4-3-",
                        "교직원보수규정 2.22.>",
                    ],
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="지원자격 공고",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )

        rendered = json.dumps(fetched["metadata"], ensure_ascii=False)
        self.assertEqual(fetched["metadata"]["regulation_title"], "신규 임용")
        self.assertIn("③ 원장은", rendered)
        self.assertIn("15일 이상", rendered)
        self.assertIn("등에", rendered)
        self.assertIn("방법으로", rendered)
        self.assertIn("원칙으로", rendered)
        self.assertIn("다음과 같은 사유", rendered)
        self.assertIn("음주운전", rendered)
        self.assertNotIn("③원장은", rendered)
        self.assertNotIn("15일이상", rendered)
        self.assertNotIn("등 에", rendered)
        self.assertNotIn("다 음", rendered)
        self.assertNotIn("음주운 전", rendered)
        self.assertNotIn("교직원보수규정 2.22", rendered)
        self.assertNotIn("방법 으로", rendered)
        self.assertNotIn("원칙으 로", rendered)
        self.assertNotIn("<2011", rendered)

    def test_all_mcp_tools_write_success_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(settings=settings, auth=mcp_auth, query="육아휴직", security_levels=["internal"])
            result_id = search["results"][0]["id"]
            fetch_regulation(settings=settings, auth=mcp_auth, result_id=result_id, security_levels=["internal"])
            lookup_regulation(
                settings=settings,
                auth=mcp_auth,
                query="approved regulation",
                document_id="doc_mcp",
                security_levels=["internal"],
            )
            list_documents(settings=settings, auth=mcp_auth, security_levels=["internal"])
            get_article(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp",
                article_no="제10조",
                security_levels=["internal"],
            )
            get_table(settings=settings, auth=mcp_auth, table_id="missing-table", security_levels=["internal"])
            compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_mcp",
                target_document_id="doc_mcp",
                security_levels=["internal"],
            )
            get_citation(settings=settings, auth=mcp_auth, result_id=result_id)
            get_index_status(settings=settings, auth=mcp_auth, security_levels=["internal"])

            actions = {
                json.loads(line)["action"]
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            }

        self.assertLessEqual(
            {
                "mcp.search",
                "mcp.fetch",
                "mcp.lookup",
                "mcp.list_documents",
                "mcp.get_article",
                "mcp.get_table",
                "mcp.compare_versions",
                "mcp.get_citation",
                "mcp.index_status",
            },
            actions,
        )

    def test_create_regulation_mcp_server_registers_expected_tools(self) -> None:
        server = create_regulation_mcp_server(data_dir="data", tenant_id="tenant-a")

        tool_manager = getattr(server, "_tool_manager")
        tool_names = set(tool_manager._tools)

        self.assertLessEqual(
            {
                "search",
                "lookup",
                "fetch",
                "list_documents",
                "get_article",
                "get_table",
                "compare_versions",
                "get_citation",
                "get_index_status",
                "get_regulation_history",
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
            },
            tool_names,
        )
        for tool_name in tool_names:
            annotations = tool_manager._tools[tool_name].annotations
            self.assertIsNotNone(annotations, tool_name)
            self.assertTrue(annotations.readOnlyHint, tool_name)
            self.assertFalse(annotations.destructiveHint, tool_name)
            self.assertTrue(annotations.idempotentHint, tool_name)
            self.assertFalse(annotations.openWorldHint, tool_name)

        self.assertEqual("MCP", server._reg_rag_scope["protocol"])
        self.assertEqual("regulation_mcp_server", server._reg_rag_scope["server_component"])
        self.assertEqual("external_ai_or_institution_client", server._reg_rag_scope["client_component"])

    def test_chatgpt_data_tool_profile_registers_only_search_and_fetch(self) -> None:
        server = create_regulation_mcp_server(data_dir="data", tenant_id="tenant-a", tool_profile="chatgpt-data")

        tool_manager = getattr(server, "_tool_manager")

        self.assertEqual({"search", "fetch"}, set(tool_manager._tools))

    def test_chatgpt_data_tool_profile_uses_exact_openai_data_source_schemas(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="tenant-a",
            tool_profile="chatgpt-data",
            warm_cache=False,
        )
        tools = server._tool_manager._tools

        self.assertEqual({"query"}, set(tools["search"].parameters["properties"]))
        self.assertEqual(["query"], tools["search"].parameters["required"])
        self.assertEqual({"id"}, set(tools["fetch"].parameters["properties"]))
        self.assertEqual(["id"], tools["fetch"].parameters["required"])

        search_output_schema = tools["search"].output_schema
        self.assertFalse(search_output_schema["additionalProperties"])
        self.assertEqual({"results"}, set(search_output_schema["properties"]))
        search_result_schema = next(iter(search_output_schema["$defs"].values()))
        self.assertFalse(search_result_schema["additionalProperties"])
        self.assertEqual({"id", "title", "url"}, set(search_result_schema["properties"]))

        fetch_output_schema = tools["fetch"].output_schema
        self.assertFalse(fetch_output_schema["additionalProperties"])
        self.assertEqual(
            {"id", "title", "text", "url", "metadata"},
            set(fetch_output_schema["properties"]),
        )
        self.assertEqual(
            {"type": "string"},
            fetch_output_schema["properties"]["metadata"]["additionalProperties"],
        )

    def test_chatgpt_data_outputs_are_narrow_and_use_openable_http_citations(self) -> None:
        rich_result = {
            "id": "opaque-result-id",
            "title": "Approved regulation",
            "url": "https://example.test/regulations/1",
            "text": "approved evidence",
            "verbatim_text": "approved evidence",
            "metadata": {
                "document_id": "internal-document-id",
                "profile_id": "internal-profile-id",
                "approval_id": "internal-approval-id",
                "document_name": "Approved regulation",
                "article_no": "Article 1",
                "source_page_start": 3,
                "source_url": "https://example.test/regulations/1",
            },
        }

        search_output = chatgpt_data_search_output(
            {"results": [rich_result], "metadata": {"trace_id": "internal-trace"}}
        ).model_dump()
        fetch_output = chatgpt_data_fetch_output(rich_result).model_dump()

        self.assertEqual(
            {
                "results": [
                    {
                        "id": "opaque-result-id",
                        "title": "Approved regulation",
                        "url": "https://example.test/regulations/1",
                    }
                ]
            },
            search_output,
        )
        self.assertEqual(
            {"id", "title", "text", "url", "metadata"},
            set(fetch_output),
        )
        self.assertEqual("3", fetch_output["metadata"]["source_page_start"])
        self.assertNotIn("document_id", fetch_output["metadata"])
        self.assertNotIn("profile_id", fetch_output["metadata"])
        self.assertNotIn("approval_id", fetch_output["metadata"])

        invalid_url = dict(rich_result, url="govreg://documents/internal")
        self.assertEqual("", chatgpt_data_fetch_output(invalid_url).url)

    def test_historical_lookup_contract_rejects_invalid_as_of_date_for_all_content_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")

            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                search_regulations(settings=settings, auth=auth, query="regulation", as_of_date="not-a-date")
            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                get_document(settings=settings, auth=auth, document_id="doc", as_of_date="not-a-date")
            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                get_article(settings=settings, auth=auth, document_id="doc", article_no="1", as_of_date="not-a-date")
            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                get_table(settings=settings, auth=auth, table_id="table-1", as_of_date="not-a-date")

    def test_regulation_history_exposes_effective_state_and_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            auth = _prepare_mcp_indexed_document(settings)
            repository = JsonRepository(settings)
            first = repository.get_document("doc_mcp")
            self.assertIsNotNone(first)
            repository.upsert_document(
                first.model_copy(
                    update={
                        "regulation_id": "reg-history",
                        "regulation_version": "v1",
                        "regulation_status": "superseded",
                        "effective_from": "2024-01-01",
                        "effective_to": "2025-12-31",
                        "profile_id": "public_portal-test-profile",
                    }
                )
            )
            second = Document(
                document_id="doc_history_v2",
                filename="history-v2.pdf",
                document_name="MCP Regulation v2",
                file_type="pdf",
                file_hash="history-v2-hash",
                tenant_id="tenant-a",
                profile_id="public_portal-test-profile",
                regulation_id="reg-history",
                regulation_version="v2",
                regulation_status="approved",
                revision_date="2025-12-15",
                effective_from="2026-01-01",
                status="completed",
            )
            repository.upsert_document(second)
            second_chunk = Chunk(
                chunk_id="history-v2-1",
                document_id="doc_history_v2",
                chunk_type="article",
                text="History version two article.",
                retrieval_text="History version two article.",
                metadata={
                    "profile_id": "public_portal-test-profile",
                    "regulation_id": "reg-history",
                    "regulation_version": "v2",
                    "regulation_status": "approved",
                    "effective_from": "2026-01-01",
                },
                security_level="internal",
            )
            repository.save_processing_result("doc_history_v2", [], [second_chunk], [])
            _approve_and_index_test_chunks(
                root,
                settings=settings,
                repository=repository,
                document_id="doc_history_v2",
                chunks=[second_chunk],
                auth=auth,
                approval_id="approval-history-v2",
            )
            repository.append_maintenance_event(
                {
                    "event_id": "history-event-1",
                    "event_type": "regulation_lifecycle_transition",
                    "created_at": "2026-01-02T00:00:00+00:00",
                    "document_id": "doc_mcp",
                    "tenant_id": "tenant-a",
                    "profile_id": "public_portal-test-profile",
                    "regulation_id": "reg-history",
                    "regulation_version": "v1",
                    "from_status": "approved",
                    "to_status": "superseded",
                    "reason": "v2 effective",
                    "actor": "reviewer",
                }
            )

            history = get_regulation_history(
                settings=settings,
                auth=auth,
                regulation_id="reg-history",
                profile_id="public_portal-test-profile",
                as_of_date="2026-02-01",
            )

        self.assertEqual("doc_history_v2", history["current_document_id"])
        self.assertEqual(2, len(history["versions"]))
        self.assertEqual(1, len(history["lifecycle_events"]))
        self.assertFalse(history["versions"][0]["is_effective_on_as_of"])
        self.assertTrue(history["versions"][1]["is_effective_on_as_of"])

    def test_warm_mcp_runtime_uses_lightweight_status_for_large_vector_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text("{}\n", encoding="utf-8")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with (
                patch("app.mcp_server.regulation_tools._MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES", 1),
                patch.object(
                    routes_rag,
                    "_load_local_vector_records",
                    side_effect=AssertionError("large startup warmup should not parse vector JSONL"),
                ),
            ):
                status = warm_mcp_runtime(settings=settings, auth=mcp_auth)

        self.assertFalse(status["warmed"])
        self.assertTrue(status["skipped"])
        self.assertEqual("lightweight", status["warmup_mode"])
        self.assertEqual("vector_store_exceeds_startup_warmup_budget", status["skip_reason"])
        self.assertFalse(status["record_count_available"])
        self.assertGreater(status["vector_byte_count"], status["warmup_max_vector_bytes"])

    def test_warm_mcp_runtime_reports_manifest_record_count_for_large_vector_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text("{}\n", encoding="utf-8")
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest_path.write_text(
                json.dumps({"report_type": "mcp_runtime_data_bundle", "record_count": 5000}) + "\n",
                encoding="utf-8",
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with (
                patch("app.mcp_server.regulation_tools._MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES", 1),
                patch.object(
                    routes_rag,
                    "_load_local_vector_records",
                    side_effect=AssertionError("large startup warmup should not parse vector JSONL"),
                ),
            ):
                status = warm_mcp_runtime(settings=settings, auth=mcp_auth)

        self.assertFalse(status["warmed"])
        self.assertEqual(5000, status["record_count"])
        self.assertTrue(status["record_count_available"])
        self.assertEqual("mcp_runtime_manifest", status["record_count_source"])
        self.assertEqual(str(manifest_path), status["manifest_path"])

    def test_warm_mcp_runtime_reports_hierarchical_retrieval_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            bm25_path = settings.data_dir / "vector_db" / "tenant-a" / "bm25.json"
            with (
                patch.object(regulation_tools, "_verified_hierarchical_runtime_paths", return_value=(hierarchy_path, None)),
                patch.object(regulation_tools, "hierarchical_index_summary", return_value={"record_count": 3}),
                patch.object(regulation_tools.routes_rag, "bm25_index_path", return_value=bm25_path),
                patch.object(regulation_tools.routes_rag, "path_signature", return_value=None),
            ):
                status = warm_mcp_runtime(settings=settings, auth=auth)

        self.assertTrue(status["warmed"])
        self.assertTrue(status["hierarchical_index_ready"])
        self.assertTrue(status["retrieval_index_ready"])
        self.assertEqual("hierarchical_sqlite", status["retrieval_index_mode"])
        self.assertFalse(status["bm25_index_ready"])

    def test_get_index_status_reports_mcp_visible_vector_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            JsonRepository(settings).upsert_document(
                Document(
                    document_id="doc_unapproved",
                    filename="draft.pdf",
                    document_name="Draft Only",
                    file_type="pdf",
                    file_hash="draft-hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            status = get_index_status(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp",
                security_levels=["internal"],
            )
            all_status = get_index_status(settings=settings, auth=mcp_auth, security_levels=["internal"])
            draft_status = get_index_status(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_unapproved",
                security_levels=["internal"],
            )

        self.assertEqual(status["summary"]["document_count"], 1)
        self.assertEqual(status["documents"][0]["indexing_status"], "indexed")
        self.assertEqual(status["documents"][0]["approved_record_count"], 1)
        self.assertEqual(status["documents"][0]["vector_record_count"], 1)
        self.assertEqual(status["documents"][0]["latest_job"]["target_type"], "local-jsonl")
        self.assertEqual([item["document_id"] for item in all_status["documents"]], ["doc_mcp"])
        self.assertEqual(draft_status["documents"], [])

    def test_get_index_status_ignores_manifest_only_approval_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            manifest_path = settings.data_dir / "repository" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("approvals", {})["forged-manifest-only"] = {
                "approval_record_id": "forged-manifest-only",
                "approval_id": "forged-manifest-only",
                "document_id": "doc_mcp",
                "tenant_id": "tenant-a",
                "chunk_ids": ["approved-1"],
                "approved_at": "2026-07-10T00:00:00+00:00",
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            status = get_index_status(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp",
                security_levels=["internal"],
            )

        self.assertEqual(status["documents"][0]["indexing_status"], "indexed")
        self.assertEqual(status["documents"][0]["approved_record_count"], 1)

    def test_get_index_status_hides_cross_tenant_documents_in_flat_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=False)
            tenant_b_auth = AuthContext(actor="tester", tenant_id="tenant-b", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_tenant_b",
                "타기관 승인 규정은 보이면 안 된다.",
                "approval-tenant-b",
                tenant_b_auth,
                tenant_id="tenant-b",
            )
            tenant_a_mcp = mcp_auth_context(tenant_id="tenant-a")

            status = get_index_status(settings=settings, auth=tenant_a_mcp, security_levels=["internal"])

        self.assertEqual(status["documents"], [])
        self.assertEqual(status["summary"]["document_count"], 0)

    def test_mcp_settings_prefers_runtime_manifest_over_stale_tenant_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            (data_dir / "tenants" / "default").mkdir(parents=True)
            data_dir.mkdir(exist_ok=True)
            (data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_storage_isolation": False}, ensure_ascii=False),
                encoding="utf-8",
            )

            settings = settings_for_mcp_project(data_dir=data_dir, tenant_id="default")

        self.assertFalse(settings.tenant_storage_isolation)
        self.assertEqual(settings.data_dir, data_dir)

    def test_mcp_viewer_cannot_request_confidential_security_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            admin_auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_confidential",
                "비공개 규정 내용",
                "approval-confidential",
                admin_auth,
                security_level="confidential",
            )
            viewer_mcp = mcp_auth_context(tenant_id="tenant-a", role="viewer")

            with self.assertRaisesRegex(ValueError, "security level"):
                search_regulations(
                    settings=settings,
                    auth=viewer_mcp,
                    query="비공개",
                    security_levels=["confidential"],
                )
            visible_status = get_index_status(settings=settings, auth=viewer_mcp)
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(visible_status["documents"], [])
        denied_search_rows = [row for row in rows if row["action"] == "mcp.search" and row["outcome"] == "denied"]
        self.assertEqual(1, len(denied_search_rows))
        self.assertEqual(403, denied_search_rows[0]["status_code"])

    def test_mcp_fetch_invalid_result_id_writes_failure_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with self.assertRaisesRegex(ValueError, "Invalid regulation result id"):
                fetch_regulation(settings=settings, auth=mcp_auth, result_id="not-base64")
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        failure_rows = [row for row in rows if row["action"] == "mcp.fetch" and row["outcome"] == "failure"]
        self.assertEqual(1, len(failure_rows))
        self.assertEqual(400, failure_rows[0]["status_code"])

    def test_get_table_returns_only_approved_table_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_table",
                    filename="table.pdf",
                    document_name="MCP Table",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_table",
                [],
                [
                    Chunk(
                        chunk_id="table-1",
                        document_id="doc_mcp_table",
                        chunk_type="appendix",
                        text="구분 내용\n육아휴직 신청 가능",
                        retrieval_text="구분 내용\n육아휴직 신청 가능",
                        metadata={
                            "table_like": True,
                            "table_id": "leave-table",
                            "table_title": "휴직 표",
                            "table_rows": ["구분 내용", "육아휴직 신청 가능"],
                        },
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="table-draft",
                        document_id="doc_mcp_table",
                        chunk_type="appendix",
                        text="draft table",
                        retrieval_text="draft table",
                        metadata={"table_like": True, "table_id": "draft-table", "table_rows": ["draft"]},
                        security_level="internal",
                    ),
                ],
                [],
            )

            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_table",
                chunks=[chunk for chunk in repository.get_chunks("doc_mcp_table") if chunk.chunk_id == "table-1"],
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["table-1"],
                        approval_id="approval-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            table = get_table(
                settings=settings,
                auth=mcp_auth,
                table_id="leave-table",
                security_levels=["internal"],
            )
            draft = get_table(
                settings=settings,
                auth=mcp_auth,
                table_id="draft-table",
                security_levels=["internal"],
            )

        self.assertEqual(len(table["tables"]), 1)
        self.assertEqual(table["tables"][0]["chunk_id"], "table-1")
        self.assertTrue(table["tables"][0]["rows"])
        self.assertEqual(table["tables"][0]["verbatim_text"], table["tables"][0]["text"])
        self.assertTrue(table["tables"][0]["verbatim"]["is_verbatim"])
        self.assertEqual(draft["tables"], [])

    def test_get_table_validates_repository_without_snapshot_preload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_fast_table",
                    filename="table.pdf",
                    document_name="Fast MCP Table",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_fast_table",
                [],
                [
                    Chunk(
                        chunk_id="table-1",
                        document_id="doc_mcp_fast_table",
                        chunk_type="table",
                        text="approved table",
                        retrieval_text="approved table",
                        metadata={"table_like": True, "table_id": "fast-table", "table_rows": ["approved table"]},
                        security_level="internal",
                    )
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            chunks = repository.get_chunks("doc_mcp_fast_table")
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_fast_table",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_fast_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["table-1"],
                        approval_id="approval-fast-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_fast_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with patch.object(
                routes_rag,
                "_load_cached_approval_snapshot",
                side_effect=AssertionError("get_table should not preload approval snapshot"),
            ):
                result = get_table(
                    settings=settings,
                    auth=mcp_auth,
                    document_id="doc_mcp_fast_table",
                    table_id="fast-table",
                    security_levels=["internal"],
                )

        self.assertEqual(len(result["tables"]), 1)
        self.assertEqual(result["tables"][0]["chunk_id"], "table-1")

    def test_get_table_resolves_appendix_alias_to_kordoc_inventory_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_table",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_kordoc_table",
                [],
                [
                    Chunk(
                        chunk_id="doc_mcp_kordoc_table_appendix_\ubcc4\ud45c1_0001",
                        document_id="doc_mcp_kordoc_table",
                        chunk_type="appendix",
                        text="\ubcc4\ud45c1 \ubcf8\ubd80 \uc704\uc784\uc804\uacb0\uc0ac\ud56d",
                        retrieval_text="\ubcc4\ud45c1 \ubcf8\ubd80 \uc704\uc784\uc804\uacb0\uc0ac\ud56d",
                        metadata={
                            "table_like": True,
                            "hierarchy_path": "Delegation Rule > \ubcc4\ud45c1",
                            "table_cell_rows": [
                                {
                                    "row_index": 0,
                                    "cells": ["legacy", "row"],
                                    "raw": "legacy | row",
                                }
                            ],
                            "kordoc_table_inventory": {
                                "status": "parsed",
                                "parser": "kordoc",
                                "table_count": 1,
                                "stored_table_count": 1,
                                "tables": [
                                    {
                                        "table_index": 1,
                                        "row_count": 2,
                                        "column_count": 4,
                                        "cell_rows": [
                                            {
                                                "row_index": 0,
                                                "cells": ["No", "Task", "President", "Director"],
                                                "raw": "No | Task | President | Director",
                                            },
                                            {
                                                "row_index": 1,
                                                "cells": ["1", "Plan approval", "", "O"],
                                                "raw": "1 | Plan approval |  | O",
                                            },
                                        ],
                                    }
                                ],
                            },
                        },
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="doc_mcp_kordoc_table_appendix_\ubcc4\ud45c1_0002",
                        document_id="doc_mcp_kordoc_table",
                        chunk_type="appendix",
                        text="\ubcc4\ud45c1 text-only continuation",
                        retrieval_text="\ubcc4\ud45c1 text-only continuation",
                        metadata={
                            "table_like": True,
                            "table_appendix_no": "\ubcc4\ud45c1",
                            "table_rows": ["text-only row should not be returned for appendix alias"],
                        },
                        security_level="internal",
                    ),
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            chunks = repository.get_chunks("doc_mcp_kordoc_table")
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_table",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_table",
                table_id="\ubcc4\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertTrue(table["metadata"]["kordoc_table_inventory_fallback"])
        self.assertEqual(table["metadata"]["table_source"], "kordoc")
        self.assertEqual(table["metadata"]["kordoc_table_index"], 1)
        self.assertEqual(table["rows"][0]["cells"], ["No", "Task", "President", "Director"])
        self.assertEqual(table["rows"][1]["cells"], ["1", "Plan approval", "", "O"])
        self.assertNotEqual(table["rows"][0]["cells"], ["legacy", "row"])

    def test_get_table_resolves_korean_alias_when_hwp_appendix_label_is_mojibake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            mojibake_appendix_label = "\ubcc4\ud45c".encode("utf-8").decode("cp949", errors="ignore")
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_mojibake",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_kordoc_mojibake",
                [],
                [
                    Chunk(
                        chunk_id=f"doc_mcp_kordoc_mojibake_appendix_{mojibake_appendix_label}1_0001",
                        document_id="doc_mcp_kordoc_mojibake",
                        chunk_type="appendix",
                        text=f"{mojibake_appendix_label}1 legacy label",
                        retrieval_text=f"{mojibake_appendix_label}1 legacy label",
                        metadata={
                            "table_like": True,
                            "hierarchy_path": f"Delegation Rule > {mojibake_appendix_label}1",
                            "kordoc_table_inventory": {
                                "status": "parsed",
                                "parser": "kordoc",
                                "table_count": 1,
                                "stored_table_count": 1,
                                "tables": [
                                    {
                                        "table_index": 1,
                                        "row_count": 1,
                                        "column_count": 2,
                                        "cell_rows": [
                                            {
                                                "row_index": 0,
                                                "cells": ["Task", "Owner"],
                                                "raw": "Task | Owner",
                                            },
                                        ],
                                    }
                                ],
                            },
                        },
                        security_level="internal",
                    ),
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            chunks = repository.get_chunks("doc_mcp_kordoc_mojibake")
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_mojibake",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_mojibake",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-mojibake",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_mojibake",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_mojibake",
                table_id="\ubcc4\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertTrue(table["metadata"]["kordoc_table_inventory_fallback"])
        self.assertEqual(table["metadata"]["table_source"], "kordoc")
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Owner"])

    def test_get_table_deduplicates_replicated_kordoc_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_dedup",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            inventory = {
                "status": "parsed",
                "parser": "kordoc",
                "table_count": 1,
                "stored_table_count": 1,
                "tables": [
                    {
                        "table_index": 1,
                        "row_count": 2,
                        "column_count": 2,
                        "cell_rows": [
                            {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"},
                            {"row_index": 1, "cells": ["Plan", "Director"], "raw": "Plan | Director"},
                        ],
                    }
                ],
            }
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_dedup_appendix_1",
                    document_id="doc_mcp_kordoc_dedup",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 first carrier",
                    retrieval_text="\ubcc4\ud45c1 first carrier",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "kordoc_table_inventory": inventory,
                    },
                    security_level="internal",
                ),
                Chunk(
                    chunk_id="doc_mcp_kordoc_dedup_appendix_1_copy",
                    document_id="doc_mcp_kordoc_dedup",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 duplicated inventory carrier",
                    retrieval_text="\ubcc4\ud45c1 duplicated inventory carrier",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "kordoc_table_inventory": inventory,
                    },
                    security_level="internal",
                ),
            ]
            repository.save_processing_result("doc_mcp_kordoc_dedup", [], chunks, [])
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_dedup",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_dedup",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-dedup",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_dedup",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_dedup",
                table_id="\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertTrue(table["metadata"]["kordoc_table_inventory_fallback"])
        self.assertEqual(table["metadata"]["table_source"], "kordoc")
        self.assertEqual(table["metadata"]["kordoc_table_index"], 1)
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Approver"])

    def test_get_table_falls_back_when_kordoc_inventory_match_has_no_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_rowless",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_rowless_appendix_1",
                    document_id="doc_mcp_kordoc_rowless",
                    chunk_type="appendix",
                    text="별표1 fallback table",
                    retrieval_text="별표1 fallback table",
                    metadata={
                        "table_like": True,
                        "table_id": "별표 1",
                        "table_appendix_no": "별표1",
                        "table_cell_rows": [
                            {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"},
                            {"row_index": 1, "cells": ["Plan", "Director"], "raw": "Plan | Director"},
                        ],
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 0,
                                    "column_count": 0,
                                    "cell_rows": [],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                )
            ]
            repository.save_processing_result("doc_mcp_kordoc_rowless", [], chunks, [])
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_rowless",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_rowless",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-rowless",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_rowless",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_rowless",
                table_id="별표 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertEqual(table["chunk_id"], "doc_mcp_kordoc_rowless_appendix_1")
        self.assertFalse(table["metadata"].get("kordoc_table_inventory_fallback", False))
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Approver"])
        self.assertEqual(table["rows"][1]["cells"], ["Plan", "Director"])

    def test_get_table_does_not_resolve_byeolji_alias_to_byeolpyo_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_byeolji_guard",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_byeolji_guard_appendix_1",
                    document_id="doc_mcp_kordoc_byeolji_guard",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 appendix table",
                    retrieval_text="\ubcc4\ud45c1 appendix table",
                    metadata={
                        "table_like": True,
                        "hierarchy_path": "Delegation Rule > \ubcc4\ud45c1",
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 1,
                                    "column_count": 2,
                                    "cell_rows": [
                                        {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"}
                                    ],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                )
            ]
            repository.save_processing_result("doc_mcp_kordoc_byeolji_guard", [], chunks, [])
            _approve_and_index_test_chunks(
                Path(tmp),
                settings=settings,
                repository=repository,
                document_id="doc_mcp_kordoc_byeolji_guard",
                chunks=chunks,
                auth=auth,
                approval_id="approval-kordoc-byeolji-guard",
            )

            result = get_table(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                document_id="doc_mcp_kordoc_byeolji_guard",
                table_id="\ubcc4\uc9c0 1",
                security_levels=["internal"],
            )

        self.assertEqual(result["tables"], [])

    def test_get_table_falls_back_from_rowless_inventory_using_table_appendix_no_without_table_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_appendix_only",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_appendix_only_appendix_1",
                    document_id="doc_mcp_kordoc_appendix_only",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 fallback table",
                    retrieval_text="\ubcc4\ud45c1 fallback table",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "table_cell_rows": [
                            {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"},
                            {"row_index": 1, "cells": ["Plan", "Director"], "raw": "Plan | Director"},
                        ],
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 0,
                                    "column_count": 0,
                                    "cell_rows": [],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                )
            ]
            repository.save_processing_result("doc_mcp_kordoc_appendix_only", [], chunks, [])
            _approve_and_index_test_chunks(
                Path(tmp),
                settings=settings,
                repository=repository,
                document_id="doc_mcp_kordoc_appendix_only",
                chunks=chunks,
                auth=auth,
                approval_id="approval-kordoc-appendix-only",
            )

            result = get_table(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                document_id="doc_mcp_kordoc_appendix_only",
                table_id="\ubcc4\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertEqual(table["chunk_id"], "doc_mcp_kordoc_appendix_only_appendix_1")
        self.assertFalse(table["metadata"].get("kordoc_table_inventory_fallback", False))
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Approver"])

    def test_get_table_keeps_distinct_kordoc_tables_when_appendices_share_table_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_same_index",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_same_index_appendix_1",
                    document_id="doc_mcp_kordoc_same_index",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 first table",
                    retrieval_text="\ubcc4\ud45c1 first table",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 1,
                                    "column_count": 2,
                                    "cell_rows": [
                                        {"row_index": 0, "cells": ["First", "Approver"], "raw": "First | Approver"}
                                    ],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                ),
                Chunk(
                    chunk_id="doc_mcp_kordoc_same_index_appendix_2",
                    document_id="doc_mcp_kordoc_same_index",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c2 second table",
                    retrieval_text="\ubcc4\ud45c2 second table",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c2",
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 1,
                                    "column_count": 2,
                                    "cell_rows": [
                                        {"row_index": 0, "cells": ["Second", "Director"], "raw": "Second | Director"}
                                    ],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                ),
            ]
            repository.save_processing_result("doc_mcp_kordoc_same_index", [], chunks, [])
            _approve_and_index_test_chunks(
                Path(tmp),
                settings=settings,
                repository=repository,
                document_id="doc_mcp_kordoc_same_index",
                chunks=chunks,
                auth=auth,
                approval_id="approval-kordoc-same-index",
            )

            result = get_table(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                document_id="doc_mcp_kordoc_same_index",
                table_id="\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 2)
        first_cells = [table["rows"][0]["cells"] for table in result["tables"]]
        self.assertIn(["First", "Approver"], first_cells)
        self.assertIn(["Second", "Director"], first_cells)

    def test_get_table_rejects_sidecar_visible_chunk_when_repository_chunk_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_table",
                    filename="table.pdf",
                    document_name="MCP Table",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_table",
                [],
                [
                    Chunk(
                        chunk_id="table-1",
                        document_id="doc_mcp_table",
                        chunk_type="table",
                        text="approved table",
                        retrieval_text="approved table",
                        metadata={"table_like": True, "table_id": "leave-table", "table_rows": ["approved table"]},
                        security_level="internal",
                    ),
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_table",
                chunks=repository.get_chunks("doc_mcp_table"),
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["table-1"],
                        approval_id="approval-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id="tenant-a")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            before = get_table(settings=settings, auth=mcp_auth, table_id="leave-table", security_levels=["internal"])
            chunks_path = settings.data_dir / "repository" / "doc_mcp_table_chunks.json"
            chunks_payload = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks_payload[0]["text"] = "drifted unapproved table"
            chunks_payload[0]["retrieval_text"] = "drifted unapproved table"
            chunks_path.write_text(json.dumps(chunks_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            after = get_table(settings=settings, auth=mcp_auth, table_id="leave-table", security_levels=["internal"])

        self.assertEqual(1, len(before["tables"]))
        self.assertEqual([], after["tables"])

    def test_compare_versions_reports_changed_approved_articles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(settings, "doc_v1", "육아휴직은 1년 이내로 한다.", "approval-v1", auth)
            _save_document_with_one_chunk(settings, "doc_v2", "육아휴직은 2년 이내로 한다.", "approval-v2", auth)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            comparison = compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_v1",
                target_document_id="doc_v2",
                security_levels=["internal"],
            )

        self.assertEqual(comparison["summary"]["changed_count"], 1)
        self.assertEqual(comparison["changed"][0]["key"], "제10조")

    def test_compare_versions_does_not_report_same_approved_article_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(settings, "doc_same_v1", "육아휴직은 1년 이내로 한다.", "approval-same-v1", auth)
            _save_document_with_one_chunk(settings, "doc_same_v2", "육아휴직은 1년 이내로 한다.", "approval-same-v2", auth)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            comparison = compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_same_v1",
                target_document_id="doc_same_v2",
                security_levels=["internal"],
            )

        self.assertEqual(comparison["summary"]["changed_count"], 0)

    def test_compare_versions_detects_changed_material_in_multi_chunk_article(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_chunks(
                settings,
                "doc_multi_v1",
                ["article opening old", "article closing same"],
                "approval-multi-v1",
                auth,
                article_no="Article 10",
            )
            _save_document_with_chunks(
                settings,
                "doc_multi_v2",
                ["article opening new", "article closing same"],
                "approval-multi-v2",
                auth,
                article_no="Article 10",
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            comparison = compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_multi_v1",
                target_document_id="doc_multi_v2",
                security_levels=["internal"],
            )

        self.assertEqual(comparison["summary"]["base_item_count"], 1)
        self.assertEqual(comparison["summary"]["changed_count"], 1)
        self.assertEqual(comparison["changed"][0]["key"], "Article 10")
        self.assertEqual(comparison["changed"][0]["target"]["chunk_count"], 2)
        self.assertIn("article opening new", comparison["changed"][0]["target"]["text_preview"])


def _prepare_mcp_indexed_document(settings: Settings) -> AuthContext:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc_mcp",
            filename="mcp.pdf",
            document_name="MCP Regulation",
            file_type="pdf",
            file_hash="hash",
            tenant_id="tenant-a",
            status="completed",
        )
    )
    repository.save_processing_result(
        "doc_mcp",
        [],
        [
            Chunk(
                chunk_id="approved-1",
                document_id="doc_mcp",
                chunk_type="article",
                text="육아휴직은 승인된 규정에 따라 신청할 수 있다.",
                retrieval_text="육아휴직은 승인된 규정에 따라 신청할 수 있다.",
                metadata={
                    "article_no": "제10조",
                    "article_title": "육아휴직",
                    "source_system": "PUBLIC_PORTAL",
                    "source_url": "https://example.test/public_portal/doc_mcp",
                    "source_record_id": "record-doc-mcp",
                    "source_file_id": "file-doc-mcp",
                    "profile_id": "public_portal-test-profile",
                    "answer_profile_version": "reg-rag-answer-profile-v1",
                    "answer_intents": ["duration"],
                    "answer_keywords": ["육아휴직", "기간"],
                    "answer_facts": [
                        {
                            "type": "duration",
                            "value": "3년",
                            "sentence": "자녀 1명에 대하여 3년 이내로 한다.",
                        }
                    ],
                    "answer_outline": ["자녀 1명에 대하여 3년 이내로 한다."],
                },
                security_level="internal",
            ),
            Chunk(
                chunk_id="draft-1",
                document_id="doc_mcp",
                chunk_type="article",
                text="검수 전 초안은 MCP에 노출되지 않는다.",
                retrieval_text="검수 전 초안은 MCP에 노출되지 않는다.",
                security_level="internal",
            ),
        ],
        [],
    )
    chunks = repository.get_chunks("doc_mcp")
    chunks[0].metadata.update(
        {
            "parser_uncertainty_source": "hwp",
            "parser_uncertainty_risk_level": "medium",
            "parser_uncertainty_confidence": 0.72,
            "parser_uncertainty_flags": ["native_table_geometry_unavailable"],
            "parser_uncertainty_recommendation": "review_tables_and_appendices",
            "parser_uncertainty_remediation_hint": "Compare table/form geometry with source HWP.",
        }
    )
    repository.save_chunks("doc_mcp", chunks)
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    evidence = _write_approval_evidence(
        approval_settings.artifact_root,
        settings=approval_settings,
        document_id="doc_mcp",
        chunks=[chunks[0]],
    )
    auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            "doc_mcp",
            routes_documents.ApprovalRequest(
                chunk_ids=["approved-1"],
                approval_id="approval-mcp",
                security_level="internal",
                review_flags_acknowledged=True,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            "doc_mcp",
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )
    return auth


def _write_approval_evidence(
    root: Path,
    *,
    settings: Settings,
    document_id: str,
    chunks: list[Chunk],
    tenant_id: str = "tenant-a",
) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_path = reports / "approval_worklist_current.json"
    batch_manifest_path = reports / "approval_review_batches_current.json"
    chunk_ids = [chunk.chunk_id for chunk in chunks]

    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-09T00:00:00+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": False,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [
            {
                "document_id": document_id,
                "document_name": document_id,
                "filename": f"{document_id}.pdf",
                "total_chunks": len(chunks),
                "draft_chunks": len(chunks),
                "low_risk_batch_review_candidate_chunks": len(chunks),
            }
        ],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)

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
        "generated_at": "2026-07-09T00:00:01+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": False,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": "reports/approval_worklist_current.json",
            "sha256": worklist_sha256,
            "effective_data_dir": str(settings.data_dir),
            "tenant_id": tenant_id,
            "tenant_storage_isolation": False,
            "document_count": 1,
            "total_chunks": len(chunks),
            "manual_attention_chunks": 0,
            "low_risk_batch_review_candidate_chunks": len(chunks),
        },
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": batch_fingerprint,
                "review_type": review_type,
                "review_strategy": "human_bulk_review",
                "document_id": document_id,
                "document_name": document_id,
                "filename": f"{document_id}.pdf",
                "chunk_count": len(chunks),
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
            }
        ],
    }
    batch_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": _sha256_file(batch_manifest_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": batch_fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _approve_and_index_test_chunks(
    root: Path,
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    chunks: list[Chunk],
    auth: AuthContext,
    approval_id: str,
) -> None:
    approval_settings = replace(settings, artifact_root=root)
    evidence = _write_approval_evidence(
        root,
        settings=approval_settings,
        document_id=document_id,
        chunks=chunks,
        tenant_id=auth.tenant_id,
    )
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[chunk.chunk_id for chunk in chunks],
                approval_id=approval_id,
                security_level="internal",
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _save_document_with_one_chunk(
    settings: Settings,
    document_id: str,
    text: str,
    approval_id: str,
    auth: AuthContext,
    *,
    tenant_id: str = "tenant-a",
    security_level: str = "internal",
    metadata: dict | None = None,
) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            document_name=document_id,
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            tenant_id=tenant_id,
            status="completed",
        )
    )
    repository.save_processing_result(
        document_id,
        [],
        [
            Chunk(
                chunk_id=f"{document_id}-chunk-1",
                document_id=document_id,
                chunk_type="article",
                text=text,
                retrieval_text=text,
                metadata=metadata or {"article_no": "제10조", "article_title": "육아휴직"},
                security_level=security_level,
            )
        ],
        [],
    )
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    evidence = _write_approval_evidence(
        approval_settings.artifact_root,
        settings=approval_settings,
        document_id=document_id,
        chunks=[chunk for chunk in repository.get_chunks(document_id) if chunk.chunk_id == f"{document_id}-chunk-1"],
        tenant_id=auth.tenant_id,
    )
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[f"{document_id}-chunk-1"],
                approval_id=approval_id,
                security_level=security_level,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


def _write_runtime_approval_snapshot_sidecar(data_dir: Path, records: list[dict], *, tenant_id: str) -> None:
    repository_dir = data_dir / "repository"
    document_ids = sorted(
        {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "").strip()
        }
    )
    (data_dir / "mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": tenant_id,
                "document_ids": document_ids,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    repository = JsonRepository(Settings(data_dir=data_dir))
    entries = []
    for record in records:
        metadata = record.get("metadata") or {}
        entries.append(
            {
                "document_id": record.get("document_id") or metadata.get("document_id"),
                "chunk_id": record.get("chunk_id") or metadata.get("chunk_id"),
                "approval_id": metadata.get("approval_id"),
                "approved_content_hash": metadata.get("approved_content_hash"),
                "security_level": metadata.get("security_level"),
                "department_acl": metadata.get("department_acl") or [],
                "content_hash": record.get("content_hash"),
            }
        )
    (repository_dir / "approval_snapshot.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_approval_snapshot",
                "tenant_id": tenant_id,
                "document_ids": document_ids,
                "snapshot_count": len(entries),
                "file_signatures": {
                    key: (list(value) if value is not None else None)
                    for key, value in routes_rag._runtime_approval_snapshot_file_signatures(repository).items()
                },
                "entries": entries,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _save_document_with_chunks(
    settings: Settings,
    document_id: str,
    texts: list[str],
    approval_id: str,
    auth: AuthContext,
    *,
    article_no: str,
    tenant_id: str = "tenant-a",
    security_level: str = "internal",
) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            document_name=document_id,
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            tenant_id=tenant_id,
            status="completed",
        )
    )
    chunks = [
        Chunk(
            chunk_id=f"{document_id}-chunk-{index}",
            document_id=document_id,
            chunk_type="article",
            text=text,
            retrieval_text=text,
            metadata={"article_no": article_no, "article_title": "Multi chunk article"},
            security_level=security_level,
        )
        for index, text in enumerate(texts, start=1)
    ]
    repository.save_processing_result(document_id, [], chunks, [])
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    saved_chunks = repository.get_chunks(document_id)
    evidence = _write_approval_evidence(
        approval_settings.artifact_root,
        settings=approval_settings,
        document_id=document_id,
        chunks=saved_chunks,
        tenant_id=auth.tenant_id,
    )
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[chunk.chunk_id for chunk in saved_chunks],
                approval_id=approval_id,
                security_level=security_level,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


if __name__ == "__main__":
    unittest.main()
