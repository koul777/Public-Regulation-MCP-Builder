from __future__ import annotations

import json
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app.ingestion.vector_upsert as vector_upsert_module
from app.ingestion.embedding_adapter import embed_vector_record, local_hash_embedding, stable_embedding_hash
from app.ingestion.vector_adapter import (
    VECTOR_RECORD_SCHEMA_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION,
    VECTOR_RECORD_VERIFICATION_VERSION_V1,
    stable_content_hash,
    vector_record_verification_hash,
    with_vector_record_verification,
)
from app.ingestion.vector_upsert import (
    ChromaLocalJsonlTarget,
    LocalJsonlVectorTarget,
    PgvectorLocalJsonlTarget,
    QdrantLocalJsonlTarget,
    QdrantRestManifestTarget,
    chroma_row_from_record,
    load_vector_records_jsonl,
    pgvector_row_from_record,
    qdrant_point_from_record,
    validate_vector_record_tenant_scope,
    vector_upsert_target,
)
from app.retrieval.bm25_index import (
    BM25_INDEX_VERSION,
    BM25_STRUCTURED_METADATA_VERSION,
    Bm25Index,
    load_bm25_index,
)


class VectorUpsertTests(unittest.TestCase):
    def test_tenant_scope_rejects_mixed_records(self) -> None:
        first = _record("doc:chunk-1", "text")
        second = _record("doc:chunk-2", "text")
        second["tenant_id"] = "tenant-b"
        second["metadata"]["tenant_id"] = "tenant-b"

        with self.assertRaisesRegex(ValueError, "multiple tenant scopes"):
            validate_vector_record_tenant_scope([first, second])

    def test_tenant_scope_rejects_expected_tenant_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match expected tenant"):
            validate_vector_record_tenant_scope([_record("doc:chunk-1", "text")], expected_tenant_id="tenant-b")

    def test_tenant_scope_rejects_record_metadata_mismatch(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["tenant_id"] = "tenant-b"

        with self.assertRaisesRegex(ValueError, "inconsistent tenant_id"):
            validate_vector_record_tenant_scope([record])

    def test_local_jsonl_target_inserts_updates_and_skips_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)

            first = _record("doc:chunk-1", "text v1")
            second = _record("doc:chunk-2", "text v1")
            first_result = target.upsert([first, second])
            second_result = target.upsert([first, _record("doc:chunk-2", "text v2")])
            stored = load_vector_records_jsonl(target_path)

        self.assertEqual(first_result["inserted_count"], 2)
        self.assertEqual(second_result["unchanged_count"], 1)
        self.assertEqual(second_result["updated_count"], 1)
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[1]["text"], "text v2")
        self.assertEqual(stored[0]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(stored[0]["verification_hash"]), 64)
        self.assertEqual(second_result["verification_record_count"], 2)

    def test_local_jsonl_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"

            result = LocalJsonlVectorTarget(target_path).upsert([_record("doc:chunk-1", "text")], dry_run=True)

        self.assertEqual(result["inserted_count"], 1)
        self.assertFalse(target_path.exists())

    def test_local_jsonl_accepts_embedded_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = LocalJsonlVectorTarget(target_path).upsert([embedded])
            stored = load_vector_records_jsonl(target_path)
            bm25_index = load_bm25_index(target_path.parent / "bm25_index.json")

        self.assertEqual(result["inserted_count"], 1)
        self.assertTrue(result["bm25_index_written"])
        self.assertEqual(result["schema_versions"], [embedded["schema_version"]])
        self.assertEqual(stored[0]["embedding_dimensions"], 8)
        self.assertIsNotNone(bm25_index)
        self.assertEqual(1, bm25_index.document_count if bm25_index else 0)

    def test_local_jsonl_incrementally_updates_bm25_for_one_document(self) -> None:
        def record(document_id: str, chunk_id: str, text: str) -> dict:
            item = _record(f"{document_id}:{chunk_id}", text)
            item["document_id"] = document_id
            item["chunk_id"] = chunk_id
            item["metadata"]["document_id"] = document_id
            item["metadata"]["chunk_id"] = chunk_id
            item["content_hash"] = stable_content_hash(text, item["metadata"])
            return item

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            target.upsert(
                [
                    record("doc-a", "chunk-1", "인사 채용 기준"),
                    record("doc-a", "chunk-old", "삭제될 예전 조문"),
                    record("doc-b", "chunk-1", "복무 휴가 기준"),
                ]
            )

            result = target.upsert(
                [record("doc-a", "chunk-1", "인사 임용 기준")],
                document_id="doc-a",
            )
            stored = load_vector_records_jsonl(target_path)
            incremental = load_bm25_index(target_path.parent / "bm25_index.json")
            rebuilt = Bm25Index.build(stored)

        self.assertEqual("incremental", result["bm25_update_mode"])
        self.assertEqual(
            {"doc-a:chunk-1", "doc-b:chunk-1"},
            {item["id"] for item in stored},
        )
        self.assertIsNotNone(incremental)
        self.assertEqual(rebuilt.source_content_hashes, incremental.source_content_hashes)
        self.assertEqual(rebuilt.document_frequencies, incremental.document_frequencies)
        self.assertEqual(rebuilt.documents, incremental.documents)
        self.assertEqual(rebuilt.score("임용 휴가"), incremental.score("임용 휴가"))

    def test_local_jsonl_noop_upsert_skips_vector_and_bm25_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            record = _record("doc:chunk-1", "변경 없는 규정")
            target.upsert([record], document_id="doc")
            bm25_path = target_path.parent / "bm25_index.json"
            vector_before = (target_path.read_bytes(), target_path.stat().st_mtime_ns)
            bm25_before = (bm25_path.read_bytes(), bm25_path.stat().st_mtime_ns)

            result = target.upsert([record], document_id="doc")

            vector_after = (target_path.read_bytes(), target_path.stat().st_mtime_ns)
            bm25_after = (bm25_path.read_bytes(), bm25_path.stat().st_mtime_ns)

        self.assertEqual("skipped_unchanged", result["bm25_update_mode"])
        self.assertFalse(result["bm25_index_written"])
        self.assertEqual(0, result["full_store_write_count"])
        self.assertEqual(1, result["unchanged_count"])
        self.assertEqual(vector_before, vector_after)
        self.assertEqual(bm25_before, bm25_after)

    def test_local_jsonl_metadata_only_change_updates_vector_and_bm25_and_reuses_embedding(self) -> None:
        def embedded_record(
            *,
            title: str,
            status: str,
            effective_to: str | None,
            hierarchy_path: str,
            profile_id: str,
            department_acl: list[str],
            security_level: str,
        ) -> dict:
            item = _record("doc:chunk-1", "제16조 임용 기준 본문")
            item["metadata"].update(
                {
                    "regulation_title": title,
                    "regulation_status": status,
                    "effective_to": effective_to,
                    "hierarchy_path": hierarchy_path,
                    "profile_id": profile_id,
                    "department_acl": department_acl,
                    "security_level": security_level,
                }
            )
            item["content_hash"] = stable_content_hash(item["text"], item["metadata"])
            return embed_vector_record(item, dimensions=8)

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            first = embedded_record(
                title="인사규정",
                status="approved",
                effective_to=None,
                hierarchy_path="제2장 > 제16조",
                profile_id="profile-a",
                department_acl=["hr"],
                security_level="internal",
            )
            target.upsert([first], document_id="doc")
            stored_before = load_vector_records_jsonl(target_path)[0]
            bm25_before = load_bm25_index(target_path.parent / "bm25_index.json")

            replacement = embedded_record(
                title="개정 인사규정",
                status="superseded",
                effective_to="2026-07-31",
                hierarchy_path="제3장 > 제16조",
                profile_id="profile-b",
                department_acl=["audit", "hr"],
                security_level="sensitive",
            )
            replacement["content_hash"] = stored_before["content_hash"]
            result = target.upsert([replacement], document_id="doc")
            stored_after = load_vector_records_jsonl(target_path)[0]
            bm25_after = load_bm25_index(target_path.parent / "bm25_index.json")

        self.assertEqual(1, result["updated_count"])
        self.assertEqual(0, result["unchanged_count"])
        self.assertEqual(1, result["embedding_reused_count"])
        self.assertTrue(result["bm25_index_written"])
        self.assertEqual("incremental", result["bm25_update_mode"])
        self.assertEqual("개정 인사규정", stored_after["metadata"]["regulation_title"])
        self.assertEqual("superseded", stored_after["metadata"]["regulation_status"])
        self.assertEqual("2026-07-31", stored_after["metadata"]["effective_to"])
        self.assertEqual("profile-b", stored_after["metadata"]["profile_id"])
        self.assertEqual(["audit", "hr"], stored_after["metadata"]["department_acl"])
        self.assertEqual("sensitive", stored_after["metadata"]["security_level"])
        self.assertNotEqual(stored_before["content_hash"], stored_after["content_hash"])
        self.assertNotEqual(
            stored_before["metadata_semantic_fingerprint"],
            stored_after["metadata_semantic_fingerprint"],
        )
        self.assertNotEqual(
            stored_before["record_semantic_fingerprint"],
            stored_after["record_semantic_fingerprint"],
        )
        self.assertEqual(stored_before["embedding_hash"], stored_after["embedding_hash"])
        self.assertEqual(stored_before["embedding"], stored_after["embedding"])
        self.assertIsNotNone(bm25_before)
        self.assertIsNotNone(bm25_after)
        self.assertNotEqual(
            bm25_before.source_content_hashes if bm25_before else "",
            bm25_after.source_content_hashes if bm25_after else "",
        )
        self.assertEqual(
            stored_after["content_hash"],
            bm25_after.documents[0]["content_hash"] if bm25_after else "",
        )

    def test_local_jsonl_serializes_concurrent_thread_read_modify_write(self) -> None:
        def record(document_id: str, text: str) -> dict:
            item = _record(f"{document_id}:chunk-1", text)
            item["document_id"] = document_id
            item["metadata"]["document_id"] = document_id
            item["content_hash"] = stable_content_hash(text, item["metadata"])
            return item

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            original_read = vector_upsert_module._read_existing_records
            first_read = threading.Event()
            release_first = threading.Event()
            second_done = threading.Event()
            read_count = 0
            read_count_guard = threading.Lock()
            errors: list[BaseException] = []

            def controlled_read(path: Path) -> list[dict]:
                nonlocal read_count
                rows = original_read(path)
                with read_count_guard:
                    read_count += 1
                    is_first = read_count == 1
                if is_first:
                    first_read.set()
                    if not release_first.wait(5):
                        raise TimeoutError("test did not release the first index writer")
                return rows

            def write(document_id: str, text: str, *, done: threading.Event | None = None) -> None:
                try:
                    target.upsert([record(document_id, text)], document_id=document_id)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if done is not None:
                        done.set()

            with patch.object(vector_upsert_module, "_read_existing_records", side_effect=controlled_read):
                first_thread = threading.Thread(target=write, args=("doc-a", "인사 규정"))
                second_thread = threading.Thread(
                    target=write,
                    args=("doc-b", "복무 규정"),
                    kwargs={"done": second_done},
                )
                first_thread.start()
                self.assertTrue(first_read.wait(5))
                second_thread.start()
                self.assertFalse(second_done.wait(0.25))
                with read_count_guard:
                    self.assertEqual(1, read_count)
                release_first.set()
                first_thread.join(10)
                second_thread.join(10)
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())

            stored = load_vector_records_jsonl(target_path)

        self.assertEqual([], errors)
        self.assertEqual({"doc-a:chunk-1", "doc-b:chunk-1"}, {item["id"] for item in stored})

    def test_local_jsonl_cross_process_lock_blocks_same_index_path(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            target_path = str(Path(tmp) / "store.jsonl")
            holder_acquired = context.Event()
            release_holder = context.Event()
            waiter_acquired = context.Event()
            holder = context.Process(
                target=_hold_local_index_lock,
                args=(target_path, holder_acquired, release_holder),
            )
            waiter = context.Process(
                target=_acquire_local_index_lock,
                args=(target_path, waiter_acquired),
            )
            holder.start()
            try:
                self.assertTrue(holder_acquired.wait(10))
                waiter.start()
                self.assertFalse(waiter_acquired.wait(0.35))
                release_holder.set()
                self.assertTrue(waiter_acquired.wait(10))
                holder.join(10)
                waiter.join(10)
                self.assertEqual(0, holder.exitcode)
                self.assertEqual(0, waiter.exitcode)
            finally:
                release_holder.set()
                for process in (holder, waiter):
                    if process.pid is not None and process.is_alive():
                        process.terminate()
                    if process.pid is not None:
                        process.join(5)

    def test_local_jsonl_transaction_keeps_existing_pair_when_bm25_staging_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            target.upsert([_record("doc:chunk-1", "기존 규정")], document_id="doc")
            bm25_path = target_path.parent / "bm25_index.json"
            vector_before = target_path.read_bytes()
            bm25_before = bm25_path.read_bytes()

            def fail_bm25_stage(path: Path, **_kwargs):
                path.write_text("{partial", encoding="utf-8")
                raise RuntimeError("forced BM25 staging failure")

            with patch.object(
                vector_upsert_module,
                "update_bm25_index_for_documents",
                side_effect=fail_bm25_stage,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced BM25 staging failure"):
                    target.upsert([_record("doc:chunk-1", "개정 규정")], document_id="doc")

            self.assertEqual(vector_before, target_path.read_bytes())
            self.assertEqual(bm25_before, bm25_path.read_bytes())
            transient_names = {
                path.name
                for path in target_path.parent.iterdir()
                if path.name.endswith((".stage", ".backup"))
            }

        self.assertEqual(set(), transient_names)

    def test_local_jsonl_transaction_rolls_back_vector_when_bm25_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            target.upsert([_record("doc:chunk-1", "기존 규정")], document_id="doc")
            bm25_path = target_path.parent / "bm25_index.json"
            vector_before = target_path.read_bytes()
            bm25_before = bm25_path.read_bytes()
            real_replace = vector_upsert_module._replace_staged_path

            def fail_second_install(staged: Path, target_file: Path) -> None:
                if target_file == bm25_path:
                    raise OSError("forced BM25 install failure")
                real_replace(staged, target_file)

            with patch.object(
                vector_upsert_module,
                "_replace_staged_path",
                side_effect=fail_second_install,
            ):
                with self.assertRaisesRegex(OSError, "forced BM25 install failure"):
                    target.upsert([_record("doc:chunk-1", "개정 규정")], document_id="doc")

            self.assertEqual(vector_before, target_path.read_bytes())
            self.assertEqual(bm25_before, bm25_path.read_bytes())
            self.assertEqual(
                set(),
                {
                    path.name
                    for path in target_path.parent.iterdir()
                    if path.name.endswith((".stage", ".backup"))
                },
            )

    def test_local_jsonl_noop_repairs_missing_bm25_without_rewriting_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            record = _record("doc:chunk-1", "색인 복구 규정")
            target.upsert([record], document_id="doc")
            bm25_path = target_path.parent / "bm25_index.json"
            vector_before = (target_path.read_bytes(), target_path.stat().st_mtime_ns)
            bm25_path.unlink()

            result = target.upsert([record], document_id="doc")

            vector_after = (target_path.read_bytes(), target_path.stat().st_mtime_ns)

        self.assertEqual("full", result["bm25_update_mode"])
        self.assertTrue(result["bm25_index_written"])
        self.assertEqual(0, result["full_store_write_count"])
        self.assertEqual(vector_before, vector_after)

    def test_local_jsonl_updates_single_and_batch_rows_when_embedding_semantics_change(self) -> None:
        for use_batch in (False, True):
            with self.subTest(use_batch=use_batch), tempfile.TemporaryDirectory() as tmp:
                target_path = Path(tmp) / "store.jsonl"
                target = LocalJsonlVectorTarget(target_path)
                first = embed_vector_record(_record("doc:chunk-1", "same approved text"), dimensions=8)
                replacement = embed_vector_record(_record("doc:chunk-1", "same approved text"), dimensions=16)
                self.assertEqual(first["content_hash"], replacement["content_hash"])

                if use_batch:
                    target.upsert_documents({"doc": [first]})
                else:
                    target.upsert([first], document_id="doc")
                bm25_path = target_path.parent / "bm25_index.json"
                bm25_before = (bm25_path.read_bytes(), bm25_path.stat().st_mtime_ns)

                result = (
                    target.upsert_documents({"doc": [replacement]})
                    if use_batch
                    else target.upsert([replacement], document_id="doc")
                )
                stored = load_vector_records_jsonl(target_path)
                bm25_after = (bm25_path.read_bytes(), bm25_path.stat().st_mtime_ns)

                self.assertEqual(1, result["updated_count"])
                self.assertEqual(0, result["unchanged_count"])
                self.assertEqual(1, result["full_store_write_count"])
                self.assertEqual("skipped_unchanged", result["bm25_update_mode"])
                self.assertEqual(16, stored[0]["embedding_dimensions"])
                self.assertEqual(16, len(stored[0]["embedding"]))
                self.assertEqual(replacement["embedding_hash"], stored[0]["embedding_hash"])
                self.assertEqual(bm25_before, bm25_after)

                model_replacement = dict(replacement)
                model_replacement["embedding_model"] = "external-test-model"
                model_result = (
                    target.upsert_documents({"doc": [model_replacement]})
                    if use_batch
                    else target.upsert([model_replacement], document_id="doc")
                )
                model_stored = load_vector_records_jsonl(target_path)

                self.assertEqual(1, model_result["updated_count"])
                self.assertEqual(0, model_result["unchanged_count"])
                self.assertEqual("external-test-model", model_stored[0]["embedding_model"])

    def test_local_jsonl_noop_rebuilds_malformed_incompatible_and_stale_bm25(self) -> None:
        for invalid_state in ("malformed", "incompatible", "legacy_index_version", "stale"):
            with self.subTest(invalid_state=invalid_state), tempfile.TemporaryDirectory() as tmp:
                target_path = Path(tmp) / "store.jsonl"
                target = LocalJsonlVectorTarget(target_path)
                record = _record("doc:chunk-1", "unchanged approved regulation")
                target.upsert([record], document_id="doc")
                bm25_path = target_path.parent / "bm25_index.json"
                vector_before = (target_path.read_bytes(), target_path.stat().st_mtime_ns)

                if invalid_state == "malformed":
                    bm25_path.write_text("{not-json", encoding="utf-8")
                elif invalid_state == "incompatible":
                    payload = json.loads(bm25_path.read_text(encoding="utf-8"))
                    payload["structured_metadata_version"] = BM25_STRUCTURED_METADATA_VERSION - 1
                    bm25_path.write_text(json.dumps(payload), encoding="utf-8")
                elif invalid_state == "legacy_index_version":
                    payload = json.loads(bm25_path.read_text(encoding="utf-8"))
                    payload["index_version"] = "reg-rag-bm25-index-v2"
                    bm25_path.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    stale = Bm25Index.build([_record("stale:chunk-1", "different corpus")])
                    bm25_path.write_text(json.dumps(stale.to_dict()), encoding="utf-8")

                result = target.upsert([record], document_id="doc")
                vector_after = (target_path.read_bytes(), target_path.stat().st_mtime_ns)
                stored = load_vector_records_jsonl(target_path)
                repaired = load_bm25_index(bm25_path)

                self.assertEqual("full", result["bm25_update_mode"])
                self.assertTrue(result["bm25_index_written"])
                self.assertEqual(0, result["full_store_write_count"])
                self.assertEqual(vector_before, vector_after)
                self.assertIsNotNone(repaired)
                self.assertEqual(
                    BM25_INDEX_VERSION,
                    repaired.index_version if repaired else None,
                )
                self.assertEqual(
                    BM25_STRUCTURED_METADATA_VERSION,
                    repaired.structured_metadata_version if repaired else None,
                )
                self.assertFalse(repaired.is_stale_for(stored) if repaired else True)

    def test_local_jsonl_batch_updates_documents_with_one_store_write(self) -> None:
        def record(document_id: str, chunk_id: str, text: str) -> dict:
            item = _record(f"{document_id}:{chunk_id}", text)
            item["document_id"] = document_id
            item["chunk_id"] = chunk_id
            item["metadata"]["document_id"] = document_id
            item["metadata"]["chunk_id"] = chunk_id
            item["content_hash"] = stable_content_hash(text, item["metadata"])
            return item

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            target.upsert(
                [
                    record("doc-a", "old", "교체 전 인사 규정"),
                    record("doc-b", "old", "교체 전 복무 규정"),
                    record("doc-c", "keep", "변경 없는 회계 규정"),
                ]
            )

            result = target.upsert_documents(
                {
                    "doc-a": [record("doc-a", "new", "개정 인사 규정")],
                    "doc-b": [record("doc-b", "new", "개정 복무 규정")],
                }
            )
            stored = load_vector_records_jsonl(target_path)
            incremental = load_bm25_index(target_path.parent / "bm25_index.json")
            rebuilt = Bm25Index.build(stored)

        self.assertEqual(2, result["batch_document_count"])
        self.assertEqual(1, result["full_store_write_count"])
        self.assertEqual("incremental", result["bm25_update_mode"])
        self.assertEqual(2, result["inserted_count"])
        self.assertEqual(2, result["removed_count"])
        self.assertEqual(
            {"doc-a:new", "doc-b:new", "doc-c:keep"},
            {item["id"] for item in stored},
        )
        self.assertIsNotNone(incremental)
        self.assertEqual(rebuilt.source_content_hashes, incremental.source_content_hashes)
        self.assertEqual(rebuilt.document_frequencies, incremental.document_frequencies)
        self.assertEqual(rebuilt.documents, incremental.documents)

    def test_local_jsonl_batch_retokenizes_only_documents_that_changed(self) -> None:
        def record(document_id: str, chunk_id: str, text: str) -> dict:
            item = _record(f"{document_id}:{chunk_id}", text)
            item["document_id"] = document_id
            item["chunk_id"] = chunk_id
            item["metadata"]["document_id"] = document_id
            item["metadata"]["chunk_id"] = chunk_id
            item["content_hash"] = stable_content_hash(text, item["metadata"])
            return item

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            original = {
                "doc-a": [record("doc-a", "one", "기존 인사 규정")],
                "doc-b": [record("doc-b", "one", "변경 없는 복무 규정")],
            }
            target.upsert_documents(original)
            with patch.object(
                vector_upsert_module,
                "update_bm25_index_for_documents",
                wraps=vector_upsert_module.update_bm25_index_for_documents,
            ) as update:
                result = target.upsert_documents(
                    {
                        "doc-a": [record("doc-a", "one", "개정 인사 규정")],
                        "doc-b": original["doc-b"],
                    }
                )

        self.assertEqual("incremental", result["bm25_update_mode"])
        self.assertEqual(["doc-a"], update.call_args.kwargs["changed_document_ids"])
        self.assertEqual(1, result["updated_count"])
        self.assertEqual(1, result["unchanged_count"])

    def test_local_jsonl_batch_noop_skips_shared_store_and_bm25_writes(self) -> None:
        def record(document_id: str, chunk_id: str, text: str) -> dict:
            item = _record(f"{document_id}:{chunk_id}", text)
            item["document_id"] = document_id
            item["chunk_id"] = chunk_id
            item["metadata"]["document_id"] = document_id
            item["metadata"]["chunk_id"] = chunk_id
            item["content_hash"] = stable_content_hash(text, item["metadata"])
            return item

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            records = {
                "doc-a": [record("doc-a", "one", "인사 규정")],
                "doc-b": [record("doc-b", "one", "복무 규정")],
            }
            target.upsert_documents(records)
            bm25_path = target_path.parent / "bm25_index.json"
            vector_before = (target_path.read_bytes(), target_path.stat().st_mtime_ns)
            bm25_before = (bm25_path.read_bytes(), bm25_path.stat().st_mtime_ns)

            result = target.upsert_documents(records)

            vector_after = (target_path.read_bytes(), target_path.stat().st_mtime_ns)
            bm25_after = (bm25_path.read_bytes(), bm25_path.stat().st_mtime_ns)

        self.assertEqual("skipped_unchanged", result["bm25_update_mode"])
        self.assertEqual(0, result["full_store_write_count"])
        self.assertEqual(2, result["unchanged_count"])
        self.assertEqual(vector_before, vector_after)
        self.assertEqual(bm25_before, bm25_after)

    def test_local_jsonl_batch_rejects_cross_document_records_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            target = LocalJsonlVectorTarget(target_path)
            mismatched = _record("doc-b:chunk-1", "text")
            mismatched["document_id"] = "doc-b"
            mismatched["metadata"]["document_id"] = "doc-b"
            mismatched["content_hash"] = stable_content_hash(mismatched["text"], mismatched["metadata"])

            with self.assertRaisesRegex(ValueError, "does not match"):
                target.upsert_documents({"doc-a": [mismatched]})

        self.assertFalse(target_path.exists())

    def test_local_jsonl_rejects_malformed_embedded_records(self) -> None:
        embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)
        embedded["embedding_dimensions"] = 7

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "embedding_dimensions"):
                target.upsert([embedded])

    def test_local_jsonl_rejects_non_finite_boolean_and_oversized_embeddings(self) -> None:
        invalid_records: list[tuple[str, object, str]] = [
            ("nan", float("nan"), "embedding_non_finite"),
            ("positive-infinity", float("inf"), "embedding_non_finite"),
            ("negative-infinity", float("-inf"), "embedding_non_finite"),
            ("boolean", True, "only numbers"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            for label, invalid_value, expected_error in invalid_records:
                with self.subTest(label=label):
                    embedded = embed_vector_record(_record(f"doc:{label}", "text"), dimensions=8)
                    embedded["embedding_model"] = "external-test-model"
                    embedded["embedding"][0] = invalid_value
                    embedded["embedding_hash"] = stable_embedding_hash(embedded["embedding"])
                    with self.assertRaisesRegex(ValueError, expected_error):
                        target.upsert([embedded])

            oversized = embed_vector_record(_record("doc:oversized", "text"), dimensions=8)
            oversized["embedding_model"] = "external-test-model"
            oversized["embedding"] = [0.0] * 4097
            oversized["embedding_dimensions"] = 4097
            oversized["embedding_hash"] = stable_embedding_hash(oversized["embedding"])
            with self.assertRaisesRegex(ValueError, "between 1 and 4096"):
                target.upsert([oversized])

    def test_local_jsonl_rejects_tampered_embedded_vectors(self) -> None:
        hash_mismatch = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)
        hash_mismatch["embedding"][0] = float(hash_mismatch["embedding"][0]) + 0.25

        vector_mismatch = embed_vector_record(_record("doc:chunk-2", "text"), dimensions=8)
        vector_mismatch["embedding"] = local_hash_embedding("different text", dimensions=8)
        vector_mismatch["embedding_hash"] = stable_embedding_hash(vector_mismatch["embedding"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "embedding_hash_mismatch"):
                target.upsert([hash_mismatch])
            with self.assertRaisesRegex(ValueError, "embedding_vector_mismatch"):
                target.upsert([vector_mismatch])

    def test_upsert_rejects_invalid_existing_verification_hash(self) -> None:
        record = with_vector_record_verification(_record("doc:chunk-1", "text"))
        record["verification_hash"] = "0" * 64

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "invalid verification_hash"):
                target.upsert([record])

    def test_upsert_migrates_legacy_verification_to_semantic_fingerprints(self) -> None:
        legacy = _record("doc:chunk-1", "legacy verified text")
        legacy["verification_version"] = VECTOR_RECORD_VERIFICATION_VERSION_V1
        legacy["verification_hash"] = vector_record_verification_hash(legacy)

        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "store.jsonl"
            LocalJsonlVectorTarget(target_path).upsert([legacy], document_id="doc")
            stored = load_vector_records_jsonl(target_path)[0]

        self.assertEqual(VECTOR_RECORD_VERIFICATION_VERSION, stored["verification_version"])
        self.assertEqual(64, len(stored["metadata_semantic_fingerprint"]))
        self.assertEqual(64, len(stored["record_semantic_fingerprint"]))

    def test_upsert_rejects_tampered_semantic_fingerprint_metadata(self) -> None:
        stamped = with_vector_record_verification(_record("doc:chunk-1", "verified text"))
        stamped["metadata"]["profile_id"] = "unauthorized-profile"

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "invalid metadata_semantic_fingerprint"):
                target.upsert([stamped])

    def test_upsert_rejects_duplicate_input_ids_and_local_path_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "duplicate record ids"):
                target.upsert([_record("same", "a"), _record("same", "b")])
            with self.assertRaisesRegex(ValueError, "local path leaks"):
                record = _record("doc:chunk-1", "text")
                record["metadata"]["source_file"] = "C:" + "\\Users" + "\\dd" + "\\secret.pdf"
                target.upsert([record])
            with self.assertRaisesRegex(ValueError, "local path leaks"):
                record = _record("doc:chunk-2", "text")
                record["metadata"]["source_file"] = "/usr/src/app/secret.pdf"
                target.upsert([record])

    def test_upsert_rejects_approved_record_without_approved_content_hash(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["metadata"].pop("approved_content_hash")
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "approved_content_hash"):
                target.upsert([record])

    def test_upsert_rejects_approved_record_without_approval_provenance(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["metadata"].pop("approval_review_batch_chunk_fingerprint")
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "approval provenance"):
                target.upsert([record])

    def test_upsert_rejects_unreviewed_preview_record(self) -> None:
        record = _record("doc:chunk-1", "text")
        record["metadata"]["approval_status"] = "UNREVIEWED_PREVIEW"
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            target = LocalJsonlVectorTarget(Path(tmp) / "store.jsonl")
            with self.assertRaisesRegex(ValueError, "not approved for indexing"):
                target.upsert([record])

    def test_factory_rejects_unknown_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(vector_upsert_target("local-jsonl", target_path=Path(tmp) / "store.jsonl").target_type, "local-jsonl")
            self.assertEqual(
                vector_upsert_target("qdrant-local-jsonl", target_path=Path(tmp) / "qdrant.jsonl").target_type,
                "qdrant-local-jsonl",
            )
            self.assertEqual(
                vector_upsert_target("pgvector-local-jsonl", target_path=Path(tmp) / "pg.jsonl").target_type,
                "pgvector-local-jsonl",
            )
            self.assertEqual(
                vector_upsert_target("chroma-local-jsonl", target_path=Path(tmp) / "chroma.jsonl").target_type,
                "chroma-local-jsonl",
            )
            with self.assertRaisesRegex(ValueError, "Unsupported vector upsert"):
                vector_upsert_target("opensearch", target_path=Path(tmp) / "unused")
            with self.assertRaisesRegex(ValueError, "live network upsert is blocked"):
                vector_upsert_target("qdrant-rest", target_path=Path(tmp) / "unused")

    def test_qdrant_local_jsonl_exports_points_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "qdrant.jsonl"
            target = QdrantLocalJsonlTarget(target_path)
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = target.upsert([embedded])
            lines = target_path.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(result["mode"], "local_export_only")
        self.assertEqual(len(lines), 1)
        point = json.loads(lines[0])
        self.assertEqual(point["id"], "doc:chunk-1")
        self.assertEqual(len(point["vector"]), 8)
        self.assertEqual(point["payload"]["text"], "text")
        self.assertEqual(point["payload"]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(point["payload"]["verification_hash"]), 64)

    def test_qdrant_local_jsonl_requires_embedded_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = QdrantLocalJsonlTarget(Path(tmp) / "qdrant.jsonl")
            with self.assertRaisesRegex(ValueError, "requires embedded vector records"):
                target.upsert([_record("doc:chunk-1", "text")])

    def test_qdrant_point_from_record_merges_metadata(self) -> None:
        record = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4)
        record["metadata"]["profile_id"] = "public_portal-etc-law"
        point = qdrant_point_from_record(record)
        self.assertEqual(point["payload"]["profile_id"], "public_portal-etc-law")
        self.assertEqual(point["payload"]["embedding_model"], "local-hash-embedding-v1")

    def test_pgvector_local_jsonl_exports_rows_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "pg.jsonl"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = PgvectorLocalJsonlTarget(target_path).upsert([embedded])
            row = json.loads(target_path.read_text(encoding="utf-8").strip())

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(row["content"], "text")
        self.assertEqual(len(row["embedding"]), 8)
        self.assertEqual(row["metadata"]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(row["metadata"]["verification_hash"]), 64)

    def test_pgvector_row_from_record_merges_metadata(self) -> None:
        record = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4)
        record["metadata"]["profile_id"] = "public_portal-etc-law"
        row = pgvector_row_from_record(record)
        self.assertEqual(row["metadata"]["profile_id"], "public_portal-etc-law")
        self.assertEqual(row["embedding_dimensions"], 4)

    def test_chroma_local_jsonl_exports_rows_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "chroma.jsonl"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = ChromaLocalJsonlTarget(target_path).upsert([embedded])
            row = json.loads(target_path.read_text(encoding="utf-8").strip())

        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(row["document"], "text")
        self.assertEqual(len(row["embedding"]), 8)
        self.assertEqual(row["metadata"]["verification_version"], VECTOR_RECORD_VERIFICATION_VERSION)
        self.assertEqual(len(row["metadata"]["verification_hash"]), 64)

    def test_chroma_row_from_record_merges_metadata(self) -> None:
        record = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=4)
        record["metadata"]["profile_id"] = "public_portal-etc-law"
        row = chroma_row_from_record(record)
        self.assertEqual(row["metadata"]["profile_id"], "public_portal-etc-law")

    def test_qdrant_rest_manifest_plans_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "qdrant_rest.manifest.json"
            embedded = embed_vector_record(_record("doc:chunk-1", "text"), dimensions=8)

            result = QdrantRestManifestTarget(manifest_path, collection_name="demo").upsert([embedded])

            self.assertEqual(result["api_call_count"], 0)
            self.assertTrue(result["live_network_blocked"])
            self.assertEqual(result["planned_upsert_count"], 1)
            self.assertTrue(manifest_path.is_file())


def _hold_local_index_lock(
    target_path: str,
    acquired,
    release,
) -> None:
    with vector_upsert_module._local_index_write_lock(
        Path(target_path),
        timeout_seconds=10,
    ):
        acquired.set()
        if not release.wait(10):
            raise TimeoutError("test lock holder was not released")


def _acquire_local_index_lock(target_path: str, acquired) -> None:
    with vector_upsert_module._local_index_write_lock(
        Path(target_path),
        timeout_seconds=10,
    ):
        acquired.set()


def _record(record_id: str, text: str) -> dict:
    metadata = {
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": record_id.rsplit(":", 1)[-1],
        "profile_id": "public_portal",
        "approval_status": "approved",
        "approval_id": f"approval-{record_id.rsplit(':', 1)[-1]}",
        "approved_content_hash": f"approved-hash-{record_id.rsplit(':', 1)[-1]}",
        "security_level": "internal",
        "approval_worklist_report_path": "reports/approval_worklist_current.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "approval-batch-001",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "human_bulk_review",
    }
    return {
        "schema_version": VECTOR_RECORD_SCHEMA_VERSION,
        "id": record_id,
        "document_id": "doc",
        "tenant_id": "tenant-a",
        "chunk_id": metadata["chunk_id"],
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


if __name__ == "__main__":
    unittest.main()
