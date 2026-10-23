from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from app.ingestion.vector_adapter import stable_content_hash
from app.processors.answer_profile import append_answer_profile_to_retrieval_text
from app.processors.chunker import Chunker
from app.services.approved_source_text import SourceText, source_text_from_verified_record
from app.services import regulation_rag_runtime as runtime


class ApprovedSourceTextTests(unittest.TestCase):
    @staticmethod
    def record(body: str = "제7조 지급 기준\n8시간 이내에는 100원을 지급한다.", *, table: str = "", plain: bool = False) -> dict:
        metadata = {
            "tenant_id": "tenant-a", "profile_id": "profile-a",
            "document_id": "synthetic-book", "chunk_id": "synthetic-article",
            "approval_status": "approved", "approval_id": "approval-a",
            "approved_content_hash": "approved-identity", "security_level": "internal",
            "document_name": "규정집", "regulation_title": "예시규정",
            "canonical_regulation_title": "예시규정",
            "hierarchy_path": "예시규정 > 제7조", "canonical_hierarchy_path": "예시규정 > 제7조",
            "source_hierarchy_path": "규정집 > 예시규정 > 제7조",
            "answer_intents": ["payment"], "answer_keywords": ["지급"],
            "answer_facts": [{"type": "payment", "value": "100원"}],
            "reference_edges": [{"type": "article", "value": "제8조"}],
        }
        chunker = Chunker()
        text = body if plain else chunker._retrieval_text("예시규정", metadata["canonical_hierarchy_path"], body, table)
        if not plain:
            text = append_answer_profile_to_retrieval_text(text, metadata)
            holder = SimpleNamespace(retrieval_text=text)
            chunker._append_reference_retrieval_text(holder, metadata["reference_edges"])
            text = holder.retrieval_text
        return {"document_id": "synthetic-book", "chunk_id": "synthetic-article", "text": text,
                "metadata": metadata, "content_hash": stable_content_hash(text, metadata)}

    def test_removes_only_augmentation_by_exact_body_witness(self) -> None:
        body = "제7조 지급 기준\n8시간 이내에는 100원을 지급한다."
        record = self.record(body)
        original = deepcopy(record)
        result = source_text_from_verified_record(record, body_witness=body, expected_tenant_id="tenant-a")
        self.assertEqual(SourceText(body, "verified_reconstruction"), result)
        self.assertEqual(original, record)
        self.assertNotIn("[답변분류]", result.text)

    def test_plain_approved_text_needs_no_repository_witness(self) -> None:
        body = "원문 가운데 [본문] 표기를 그대로 둔다."
        self.assertEqual(SourceText(body, "verified_plain"), source_text_from_verified_record(self.record(body, plain=True)))

    def test_tampered_body_and_whitespace_are_unavailable(self) -> None:
        body = "제7조\n100원을 지급한다."
        record = self.record(body)
        for witness in (body.replace("100", "200"), body.replace("\n", " "), body + " ", "100원을 지급한다."):
            with self.subTest(witness=witness):
                self.assertEqual("unavailable", source_text_from_verified_record(record, body_witness=witness).status)

    def test_tampered_vector_text_or_metadata_is_unavailable(self) -> None:
        for field in ("text", "metadata"):
            record = self.record()
            if field == "text":
                record["text"] += "변조"
            else:
                record["metadata"]["canonical_regulation_title"] = "다른규정"
            self.assertEqual("unavailable", source_text_from_verified_record(record, body_witness="본문").status)

    def test_wrong_tenant_and_unapproved_record_are_unavailable(self) -> None:
        record = self.record("본문", plain=True)
        self.assertEqual("unavailable", source_text_from_verified_record(record, expected_tenant_id="tenant-b").status)
        for field in ("approval_status", "approval_id", "approved_content_hash"):
            changed = deepcopy(record)
            changed["metadata"][field] = ""
            changed["content_hash"] = stable_content_hash(changed["text"], changed["metadata"])
            self.assertEqual("unavailable", source_text_from_verified_record(changed).status)

    def test_repository_title_is_not_a_reconstruction_input(self) -> None:
        record = self.record("본문")
        record["text"] = record["text"].replace("[문서명] 예시규정", "[문서명] 임의규정")
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])
        self.assertEqual("unavailable", source_text_from_verified_record(record, body_witness="본문").status)

    def test_table_duplication_preserves_the_complete_body(self) -> None:
        table = "| 항목 | 금액 |\n| --- | --- |\n| 기본 | 100 |"
        body = "별표 지급표\n" + table
        record = self.record(body, table=table)
        result = source_text_from_verified_record(record, body_witness=body, table_witness=table)
        self.assertEqual(body, result.text)
        self.assertEqual("unavailable", source_text_from_verified_record(record, body_witness=body, table_witness="잘못된 표").status)

    def test_literal_body_markers_and_terminal_suffix_are_not_stripped(self) -> None:
        for body in (
            "제7조\n[표]\n이 표기는 원문이다.\n[본문]\n계속되는 본문",
            "제7조\n[답변분류]\n의도: payment\n키워드: 지급\n- payment: 100원",
            "제7조\n[참조]\n- 조문: 제8조",
        ):
            with self.subTest(body=body):
                record = self.record(body)
                self.assertEqual(body, source_text_from_verified_record(record, body_witness=body).text)

    def test_unknown_augmented_format_and_missing_witness_fail_closed(self) -> None:
        self.assertEqual("unavailable", source_text_from_verified_record(self.record()).status)
        record = self.record("[위치] 미지원 형식\n본문", plain=True)
        self.assertEqual("unavailable", source_text_from_verified_record(record, body_witness="본문").status)

    def test_malformed_augmentation_metadata_is_unavailable(self) -> None:
        for field in ("answer_facts", "answer_intents", "reference_edges"):
            with self.subTest(field=field):
                record = self.record("본문")
                record["metadata"][field] = 42
                record["content_hash"] = stable_content_hash(record["text"], record["metadata"])
                self.assertEqual("unavailable", source_text_from_verified_record(record, body_witness="본문").status)

    def test_public_formatter_ignores_store_source_text_flags(self) -> None:
        record = self.record()
        record.update({"source_text": "임의 내용", "source_text_status": "verified_plain"})
        public = runtime.public_search_result(record, 1.0)
        self.assertEqual("", public["text"])
        self.assertEqual("unavailable", public["source_text_status"])

    def test_public_formatter_requires_explicit_typed_boundary_result(self) -> None:
        record = self.record()
        public = runtime.public_search_result(record, 1.0, source_text_result={"text": "임의 내용", "status": "verified_plain"})
        self.assertEqual("", public["text"])
        verified = source_text_from_verified_record(record, body_witness="제7조 지급 기준\n8시간 이내에는 100원을 지급한다.")
        public = runtime.public_search_result(record, 1.0, source_text_result=verified)
        self.assertEqual(verified.text, public["text"])
        self.assertEqual(record["content_hash"], public["content_hash"])

    def test_public_formatter_does_not_expose_unknown_or_unavailable_status_text(self) -> None:
        for status in ("unavailable", "arbitrary-available"):
            with self.subTest(status=status):
                public = runtime.public_search_result(self.record(), 1.0,
                                                      source_text_result=SourceText("임의 내용", status))
                self.assertEqual("", public["text"])
                self.assertEqual("unavailable", public["source_text_status"])

    def test_runtime_witness_rejects_wrong_current_identity(self) -> None:
        record = self.record("본문")
        chunk = {"document_id": "synthetic-book", "chunk_id": "synthetic-article", "approval_status": "approved",
                 "approval_id": "wrong-approval", "approved_content_hash": "approved-identity",
                 "normalized_text": "본문", "metadata": record["metadata"]}
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            (Path(tmp) / "repository").mkdir()
            (Path(tmp) / "repository/synthetic-book_chunks.json").write_text(json.dumps([chunk]), encoding="utf-8")
            self.assertEqual("unavailable", runtime.approved_source_text_for_record(
                record, settings=settings, auth=SimpleNamespace(tenant_id="tenant-a")).status)

    def test_runtime_witness_rejects_wrong_document_tenant_and_profile(self) -> None:
        record = self.record("본문")
        chunk_data = {"document_id": "synthetic-book", "chunk_id": "synthetic-article", "normalized_text": "본문",
                      "approval_status": "approved", "approval_id": "approval-a",
                      "approved_content_hash": "approved-identity", "metadata": deepcopy(record["metadata"])}
        auth = SimpleNamespace(tenant_id="tenant-a")
        for tenant_id, profile_id in (("tenant-b", "profile-a"), ("tenant-a", "profile-b")):
            with self.subTest(tenant_id=tenant_id, profile_id=profile_id):
                chunk_data["metadata"].update(tenant_id=tenant_id, profile_id=profile_id)
                with tempfile.TemporaryDirectory() as tmp:
                    settings = SimpleNamespace(data_dir=Path(tmp))
                    (Path(tmp) / "repository").mkdir()
                    (Path(tmp) / "repository/synthetic-book_chunks.json").write_text(json.dumps([chunk_data]), encoding="utf-8")
                    self.assertEqual("unavailable", runtime.approved_source_text_for_record(record, settings=settings, auth=auth).status)

    def test_legacy_chat_does_not_call_model_for_unavailable_source(self) -> None:
        from app.api import routes_rag

        result = {"chunk_id": "synthetic", "approval_status": "approved", "text": "",
                  "source_text_status": "unavailable", "answer_facts": [{"type": "payment", "value": "999원"}]}
        with patch.object(routes_rag, "GroundedAnswerAgent") as agent:
            answer, evidence_ids = routes_rag._chat_answer("ollama", SimpleNamespace(), "지급액", [result])
            agent.assert_not_called()
        self.assertEqual(routes_rag.NO_EVIDENCE_ANSWER, answer)
        self.assertEqual([], evidence_ids)
        answer, evidence_ids = routes_rag._chat_answer("extractive", SimpleNamespace(), "지급액", [result])
        self.assertEqual(routes_rag.NO_EVIDENCE_ANSWER, answer)
        self.assertEqual([], evidence_ids)

    def test_legacy_chat_blocks_falsey_and_conflicting_source_status(self) -> None:
        from app.api import routes_rag

        for status in (False, 0, [], {}):
            with self.subTest(status=status), patch.object(routes_rag, "GroundedAnswerAgent") as agent:
                result = {"source_text_status": status, "text": "본문", "metadata": {"source_text_status": "verified_plain"}}
                answer, evidence_ids = routes_rag._chat_answer("ollama", SimpleNamespace(), "질문", [result])
                agent.assert_not_called()
                self.assertEqual(routes_rag.NO_EVIDENCE_ANSWER, answer)
                self.assertEqual([], evidence_ids)

    def test_api_metadata_profiles_keep_source_unavailable_status(self) -> None:
        from app.api import routes_rag
        from app.rag.context_builder import ContextBuilder

        record = self.record()
        public = runtime.public_search_result(record, 1.0)
        for profile in ("full", "external", "chatgpt-data"):
            with self.subTest(profile=profile):
                projected = routes_rag._rag_result_for_metadata_profile(public, profile)
                self.assertEqual("unavailable", projected["source_text_status"])
                self.assertFalse(ContextBuilder().build([projected]).items)

    def test_internal_retrieval_observer_does_not_put_augmentation_in_public_results_or_trace(self) -> None:
        from app.services import regulation_rag_service as service

        body = "승인된 원문"
        record = self.record(body)
        excluded = self.record("선택되지 않은 원문")
        records = [record, excluded]
        request = SimpleNamespace(query="지급 기준", top_k=1, as_of_date=None)
        routes = SimpleNamespace(
            _validate_query_policy=lambda _: None,
            JsonRepository=lambda _: SimpleNamespace(),
            _perf_elapsed_ms=lambda _: 0,
            load_visible_records=lambda **_: records,
            _rag_trace=lambda **kwargs: {"results": kwargs["results"], "extra": kwargs["extra"]},
        )
        observed = []
        with ExitStack() as stack:
            for target, name, value in (
                (service, "_load_routes_rag", routes),
                (service, "validate_query_security_scope", None),
                (service, "to_rag_search_request", request),
                (service, "requested_department_ids", frozenset()),
                (service, "load_local_vector_records", records),
                (service, "approval_snapshot_for_records", {}),
                (service, "score_records", ([(1.0, record), (0.9, excluded)], {"retrieval_model": "synthetic"})),
                (runtime, "approved_source_text_for_record", SourceText(body, "verified_reconstruction")),
            ):
                stack.enter_context(patch.object(target, name, return_value=value))
            results, trace = service.search_records(
                query=SimpleNamespace(), auth=SimpleNamespace(tenant_id="tenant-a"),
                settings=SimpleNamespace(rag_trace_enabled=False), retrieval_text_observer=observed.append,
            )
        self.assertEqual([[record["text"]]], observed)
        self.assertEqual([body], [result["text"] for result in results])
        self.assertNotIn("[문서명]", json.dumps({"results": results, "trace": trace}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
