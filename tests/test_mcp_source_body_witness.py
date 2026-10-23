from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.core.security_primitives import AuthContext
from app.ingestion.vector_adapter import stable_content_hash
from app.mcp_server import regulation_tools as mcp
from app.services import approved_source_witness as witnesses


class McpSourceBodyWitnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root)
        self.auth = AuthContext(role="operator", actor="synthetic-reader", tenant_id="synthetic-tenant", auth_mode="token")
        (self.root / "repository").mkdir()
        self.path = self.root / "repository" / "synthetic-document_chunks.json"
        self.body = "Synthetic staff work thirty hours weekly."
        self.metadata = {
            "tenant_id": "synthetic-tenant", "profile_id": "synthetic-profile",
            "document_id": "synthetic-document", "chunk_id": "synthetic-chunk",
            "approval_status": "approved", "approval_id": "synthetic-approval",
            "approved_content_hash": "a" * 64, "document_name": "Synthetic rule",
            "hierarchy_path": "Synthetic chapter", "regulation_title": "Synthetic rule",
        }
        self.record = self._record(self.body)
        self.row = {
            "document_id": "synthetic-document", "chunk_id": "synthetic-chunk",
            "normalized_text": self.body, "approval_status": "approved",
            "approval_id": "synthetic-approval", "approved_content_hash": "a" * 64,
            "metadata": {"tenant_id": "synthetic-tenant", "profile_id": "synthetic-profile"},
        }
        self._write([self.row])
        witnesses._SOURCE_WITNESS_CACHE.clear()
        self.addCleanup(witnesses._SOURCE_WITNESS_CACHE.clear)

    def _record(self, body: str) -> dict:
        text = f"[문서명] Synthetic rule\n[위치] Synthetic chapter\n[본문]\n{body}"
        return {"document_id": "synthetic-document", "chunk_id": "synthetic-chunk", "text": text,
                "metadata": dict(self.metadata), "content_hash": stable_content_hash(text, self.metadata)}

    def _write(self, rows: list[dict]) -> None:
        self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def _resolve(self, record=None):
        return witnesses.source_text_from_current_witness(settings=self.settings, auth=self.auth, record=record or self.record)

    def test_current_witness_streams_once_and_public_outputs_use_body(self) -> None:
        with patch.object(witnesses, "_load_source_body_witnesses", wraps=witnesses._load_source_body_witnesses) as loader, patch.object(
            mcp, "_json_repository", side_effect=AssertionError("No repository initialization is permitted")
        ):
            for _ in range(2):
                public = mcp._public_mcp_record(self.record, 1.0, settings=self.settings, auth=self.auth)
                for output in (mcp._mcp_search_result(public), mcp._mcp_fetch_result(public)):
                    self.assertEqual(self.body, output["text"])
                    self.assertEqual(self.body, output["verbatim_text"])
                    self.assertEqual(self.body, output["verbatim"]["text"])
                    self.assertEqual("verified_reconstruction", output["metadata"]["source_text_status"])
            self.assertEqual(1, loader.call_count)

    def test_unavailable_never_falls_back_to_retrieval_text(self) -> None:
        self.path.unlink()
        injected = {**self.record, "source_text": self.body, "source_text_status": "verified_reconstruction"}
        public = mcp._public_mcp_record(injected, 1.0, settings=self.settings, auth=self.auth)
        for output in (mcp._mcp_search_result(public), mcp._mcp_fetch_result(public)):
            self.assertEqual("", output["text"])
            self.assertEqual("", output["verbatim_text"])
            self.assertEqual("", output["verbatim"]["text"])
            self.assertFalse(output["verbatim"]["is_verbatim"])
            self.assertEqual("unavailable", output["metadata"]["source_text_status"])
        connector = mcp.chatgpt_data_fetch_output(mcp._mcp_fetch_result(public))
        self.assertEqual("", connector.text)
        self.assertEqual("unavailable", connector.metadata["source_text_status"])

    def test_table_review_flags_keep_native_types_and_connector_string_contract(self) -> None:
        metadata = {**self.record["metadata"], "table_like": True, "table_review_required": True,
                    "table_review_flags": ["unstable_column_count", "synthetic_review_flag"]}
        record = {**self.record, "metadata": metadata,
                  "content_hash": stable_content_hash(self.record["text"], metadata)}
        public = mcp._public_mcp_record(record, 1.0, settings=self.settings, auth=self.auth)
        fetched = mcp._mcp_fetch_result(public)
        self.assertIs(True, fetched["metadata"]["table_like"])
        self.assertIs(True, fetched["metadata"]["table_review_required"])
        self.assertEqual(metadata["table_review_flags"], fetched["metadata"]["table_review_flags"])
        connector = mcp.chatgpt_data_fetch_output(fetched)
        self.assertEqual("true", connector.metadata["table_like"])
        self.assertEqual("true", connector.metadata["table_review_required"])
        self.assertEqual(metadata["table_review_flags"], json.loads(connector.metadata["table_review_flags"]))
        self.assertTrue(all(isinstance(value, str) for value in connector.metadata.values()))

    def test_same_size_restored_mtime_tamper_invalidates_warm_cache(self) -> None:
        self.assertEqual(self.body, self._resolve().text)
        original = self.path.stat()
        self.row["normalized_text"] = self.body.replace("thirty", "ninety")
        self._write([self.row])
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(original.st_size, self.path.stat().st_size)
        self.assertEqual("unavailable", self._resolve().status)

    def test_mutation_during_stream_read_is_not_cached(self) -> None:
        original = witnesses._load_source_body_witnesses

        def changing(path, document_id):
            result = original(path, document_id)
            path.write_bytes(path.read_bytes() + b" ")
            return result

        with patch.object(witnesses, "_load_source_body_witnesses", side_effect=changing):
            self.assertEqual("unavailable", self._resolve().status)
        self.assertEqual({}, witnesses._SOURCE_WITNESS_CACHE)

    def test_current_approval_tenant_profile_and_witness_must_match(self) -> None:
        for field, value in (("approval_status", "draft"), ("approval_id", "other-approval"),
                             ("approved_content_hash", "b" * 64), ("normalized_text", "Unrelated source text")):
            with self.subTest(field=field):
                self._write([{**self.row, field: value}])
                self.assertEqual("unavailable", self._resolve().status)
        for field in ("tenant_id", "profile_id"):
            with self.subTest(field=field):
                self._write([{**self.row, "metadata": {**self.row["metadata"], field: "other-scope"}}])
                self.assertEqual("unavailable", self._resolve().status)

    def test_duplicate_identity_json_keys_and_malformed_arrays_fail_closed(self) -> None:
        valid = json.dumps(self.row)
        for raw in (f"[{valid},{valid}]", f'[{valid[:-1]},"chunk_id":"other"}}]',
                    f"[{valid},]", f"[{valid}] trailing", "{}", "["):
            with self.subTest(raw=raw[:40]):
                self.path.write_text(raw, encoding="utf-8")
                self.assertEqual("unavailable", self._resolve().status)
        self._write([{**self.row, "document_id": "foreign-document"}])
        self.assertEqual("unavailable", self._resolve().status)

    def test_streaming_record_across_read_boundaries_retains_only_witness(self) -> None:
        large_body = "Synthetic body " * 6000
        self._write([{**self.row, "normalized_text": large_body.strip(), "ignored_large_field": "x" * 90000}])
        result = self._resolve(self._record(large_body.strip()))
        self.assertEqual(large_body.strip(), result.text)
        witness = next(iter(witnesses._SOURCE_WITNESS_CACHE.values()))[1]["synthetic-chunk"]
        self.assertFalse(hasattr(witness, "ignored_large_field"))

    def test_bounded_cache_and_unsafe_document_path_fail_closed(self) -> None:
        with patch.object(witnesses, "_SOURCE_WITNESS_CACHE_MAX_BYTES", 1):
            self.assertEqual("unavailable", self._resolve().status)
        for value in ("../outside", "..\\outside", "C:outside"):
            with self.subTest(value=value), patch.object(witnesses, "_load_source_body_witnesses") as loader:
                self.assertEqual("unavailable", self._resolve({**self.record, "document_id": value}).status)
                loader.assert_not_called()

    def test_plain_verified_record_needs_no_repository_read(self) -> None:
        record = {**self.record, "text": self.body, "content_hash": stable_content_hash(self.body, self.metadata)}
        self.path.unlink()
        with patch.object(witnesses, "_load_source_body_witnesses") as loader:
            self.assertEqual(self.body, self._resolve(record).text)
            loader.assert_not_called()

    def test_unknown_chunk_or_corrupt_vector_hash_is_unavailable(self) -> None:
        for change in ({"chunk_id": "unknown-chunk"}, {"content_hash": "b" * 64}):
            with self.subTest(change=change):
                self.assertEqual("unavailable", self._resolve({**self.record, **change}).status)

    def test_linked_chunk_file_is_not_read(self) -> None:
        destination = self.root / "outside-chunks.json"
        self.path.rename(destination)
        try:
            self.path.symlink_to(destination)
        except OSError:
            # Windows developer mode or symlink privilege is optional; exercise
            # the same pre-read guard without granting any additional privilege.
            with patch.object(Path, "is_symlink", return_value=True), patch.object(
                witnesses, "_load_source_body_witnesses"
            ) as loader:
                self.assertEqual("unavailable", self._resolve().status)
                loader.assert_not_called()
            return
        with patch.object(witnesses, "_load_source_body_witnesses") as loader:
            self.assertEqual("unavailable", self._resolve().status)
            loader.assert_not_called()

    def test_generic_approved_rule_reference_is_not_a_regulation_title(self) -> None:
        query = "육아휴직은 승인된 규정에 따라 신청할 수 있다."
        self.assertEqual({}, mcp._mcp_named_regulation_scope(query))
        self.assertFalse(mcp._mcp_relevance_guard(query, [{"text": query, "document_name": "Synthetic rule"}])["refused"])

    def test_mcp_guard_uses_retrieval_text_without_exposing_it(self) -> None:
        raw_text = "Synthetic retrieval-only search phrase."
        public = {"document_id": "synthetic-document", "chunk_id": "synthetic-chunk",
                  "text": "", "source_text_status": "unavailable"}

        def fake_search(*, retrieval_text_observer, **_kwargs):
            retrieval_text_observer([raw_text])
            return [public], {"trace_id": "synthetic-trace"}

        with patch.object(mcp, "_mcp_search_requires_verified_hierarchy", return_value=False), patch.object(
            mcp, "_resolve_mcp_profile_scope_with_runtime_token", return_value=(None, None)
        ), patch.object(mcp, "_resolve_mcp_flat_fallback_profile_scope", return_value=None), patch.object(
            mcp.routes_rag, "search_records", side_effect=fake_search
        ), patch.object(mcp, "_mcp_relevance_guard", return_value={"refused": False}) as guard, patch.object(
            mcp, "audit_api_event"
        ):
            result = mcp.search_regulations(settings=self.settings, auth=self.auth, query="Synthetic query")
        self.assertEqual(raw_text, guard.call_args.args[1][0]["text"])
        self.assertEqual("", result["results"][0]["text"])
        self.assertNotIn(raw_text, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
