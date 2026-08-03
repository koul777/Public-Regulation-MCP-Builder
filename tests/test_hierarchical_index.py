from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import multiprocessing
import tempfile
import json
import os
import operator
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.mcp_server import regulation_tools
from app.retrieval import hierarchical_index as hierarchical_index_module
from app.ingestion.vector_adapter import stable_content_hash
from app.core.config import Settings
from app.mcp_server.regulation_tools import (
    get_regulation_article,
    get_regulation_references,
    get_regulation_toc,
    list_regulation_reference_cycles,
    list_regulations,
    mcp_auth_context,
    search_regulations,
)
from app.retrieval.bm25_index import source_content_hashes
from app.retrieval import hierarchical_index
from app.retrieval.hierarchical_index import (
    VerifiedVectorCacheNamespace,
    build_hierarchical_runtime_index,
    canonicalize_runtime_records,
    fully_visible_regulation_unit_ids,
    index_summary,
    indexed_document_ids,
    list_indexed_regulations,
    load_article_records,
    load_document_article_records,
    load_record_by_chunk,
    logical_corpus_sha256_for_records,
    page_indexed_regulations,
    page_reference_cycles,
    regulation_references,
    regulation_toc,
    regulation_unit_id_for,
    search_hierarchical_records,
    write_vector_records_with_offsets,
)


def _hold_hierarchical_index_build_lock(
    index_path: str,
    acquired,
    release,
) -> None:
    with hierarchical_index._hierarchical_index_build_guard(
        index_path,
        timeout_seconds=10,
    ):
        acquired.set()
        if not release.wait(timeout=15):
            raise TimeoutError("hierarchical index lock holder was not released")


def _build_hierarchical_index_in_process(
    index_path: str,
    records: list[dict],
    *,
    pause_hash: bool,
    started,
    hash_started,
    release_hash,
    finished,
    results,
) -> None:
    try:
        started.set()

        def build() -> dict:
            return build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        if pause_hash:
            original_sha256 = hierarchical_index._sha256_file

            def paused_sha256(path: Path) -> str:
                hash_started.set()
                if not release_hash.wait(timeout=15):
                    raise TimeoutError("hierarchical index hash was not released")
                return original_sha256(path)

            with patch.object(
                hierarchical_index,
                "_sha256_file",
                side_effect=paused_sha256,
            ):
                result = build()
        else:
            result = build()
        results.put(("ok", result))
    except Exception:  # pragma: no cover - surfaced in the parent process
        results.put(("error", traceback.format_exc()))
    finally:
        finished.set()


class HierarchicalIndexTests(unittest.TestCase):
    def tearDown(self) -> None:
        with hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE_LOCK:
            hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE.clear()
            hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE_BYTES = 0
        with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
            hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE.clear()
            hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_BYTES = 0
            hierarchical_index._INDEXED_CHUNK_TOPOLOGY_INFLIGHT.clear()
            hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS.clear()

    def test_module_import_defers_reference_graph_builder(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import app.retrieval.hierarchical_index; "
                    "raise SystemExit("
                    "int('app.retrieval.regulation_reference_graph' in sys.modules)"
                    ")"
                ),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_build_rejects_forged_approved_ambiguous_combined_book_record(self) -> None:
        record = _record(
            "doc-ambiguous",
            "chunk-ambiguous",
            regulation_no="",
            regulation_title="통합규정집",
            article_no="document",
            article_title="통합규정집",
            text="승인 상태가 위조된 모호한 통합 규정집",
            revision_date="2026-08-03",
            chunk_type="document",
            metadata_updates={
                "ambiguous_combined_book_boundary": True,
                "warnings": ["ambiguous_combined_book_boundary_requires_reparse"],
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            with self.assertRaisesRegex(
                ValueError,
                "rejected ambiguous combined-book regulation boundaries: chunk-ambiguous",
            ):
                build_hierarchical_runtime_index(
                    index_path,
                    [record],
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )

            self.assertFalse(index_path.exists())

    def test_supersedes_cycle_nodes_distinguishes_chain_cycle_and_branches(
        self,
    ) -> None:
        self.assertEqual(
            set(),
            hierarchical_index._supersedes_cycle_nodes(
                [(3, 2), (2, 1), (1, 0)]
            ),
        )
        self.assertEqual(
            {1, 2, 3},
            hierarchical_index._supersedes_cycle_nodes(
                [(1, 2), (2, 3), (3, 1)]
            ),
        )
        self.assertEqual(
            {2, 3, 7},
            hierarchical_index._supersedes_cycle_nodes(
                [
                    (1, 2),
                    (2, 3),
                    (3, 2),
                    (4, 3),
                    (5, 4),
                    (6, 4),
                    (7, 7),
                ]
            ),
        )

    def test_supersedes_cycle_nodes_large_chain_uses_linear_hash_work(self) -> None:
        class HashCountingInt(int):
            hash_calls = 0

            def __hash__(self) -> int:
                type(self).hash_calls += 1
                return super().__hash__()

        node_count = 4_000
        nodes = [HashCountingInt(index) for index in range(node_count + 1)]
        edges = [
            (nodes[index], nodes[index - 1])
            for index in range(1, node_count + 1)
        ]
        HashCountingInt.hash_calls = 0

        cycle_nodes = hierarchical_index._supersedes_cycle_nodes(edges)

        self.assertEqual(set(), cycle_nodes)
        self.assertLess(HashCountingInt.hash_calls, node_count * 20)

    def test_index_summary_stores_private_source_content_binding(self) -> None:
        records = [
            _record(
                "doc-binding",
                "binding-a",
                regulation_no="1-1",
                regulation_title="Binding Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="first approved binding record",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-binding",
                "binding-b",
                regulation_no="1-1",
                regulation_title="Binding Regulation",
                article_no="Article 2",
                article_title="Scope",
                text="second approved binding record",
                revision_date="2026-07-01",
            ),
        ]
        expected = source_content_hashes(records)

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            built = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            summary = index_summary(index_path)
            connection = sqlite3.connect(index_path)
            try:
                stored = dict(
                    connection.execute(
                        """
                        SELECT key, value
                        FROM index_metadata
                        WHERE key IN (
                            'source_content_hashes',
                            'logical_corpus_sha256'
                        )
                        """
                    )
                )
                connection.execute(
                    """
                    UPDATE index_metadata
                    SET value=upper(value)
                    WHERE key='logical_corpus_sha256'
                    """
                )
                connection.commit()
            finally:
                connection.close()
            invalid_summary = index_summary(index_path)

        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        self.assertEqual(expected, built["source_content_hashes"])
        self.assertEqual(expected, summary["source_content_hashes"])
        self.assertEqual(expected, stored["source_content_hashes"])
        self.assertRegex(built["logical_corpus_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            built["logical_corpus_sha256"],
            stored["logical_corpus_sha256"],
        )
        self.assertEqual(
            built["logical_corpus_sha256"],
            summary["logical_corpus_sha256"],
        )
        self.assertIsNone(invalid_summary["logical_corpus_sha256"])

    def test_build_one_shot_generator_matches_list_input(self) -> None:
        records = [
            _record(
                "doc-generator",
                "generator-a",
                regulation_no="1-2",
                regulation_title="Generator Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="first generator-backed record",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-generator",
                "generator-b",
                regulation_no="1-2",
                regulation_title="Generator Regulation",
                article_no="Article 2",
                article_title="Scope",
                text="second generator-backed record",
                revision_date="2026-07-01",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            list_path = root / "list.sqlite3"
            generator_path = root / "generator.sqlite3"
            list_result = build_hierarchical_runtime_index(
                list_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            generator_result = build_hierarchical_runtime_index(
                generator_path,
                (record for record in records),
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            list_summary = index_summary(list_path)
            generator_summary = index_summary(generator_path)

        self.assertEqual(len(records), generator_result["record_count"])
        self.assertEqual(
            {key: value for key, value in list_result.items() if key != "path"},
            {
                key: value
                for key, value in generator_result.items()
                if key != "path"
            },
        )
        self.assertEqual(
            {key: value for key, value in list_summary.items() if key != "path"},
            {
                key: value
                for key, value in generator_summary.items()
                if key != "path"
            },
        )

    def test_logical_corpus_hash_accepts_one_shot_generator(self) -> None:
        records = [
            _record(
                "doc-logical-generator",
                "logical-generator-a",
                regulation_no="1-3",
                regulation_title="Logical Generator Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="first logical generator record",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-logical-generator",
                "logical-generator-b",
                regulation_no="1-3",
                regulation_title="Logical Generator Regulation",
                article_no="Article 2",
                article_title="Scope",
                text="second logical generator record",
                revision_date="2026-07-01",
            ),
        ]

        list_hash = logical_corpus_sha256_for_records(
            records,
            tenant_id="tenant-a",
            profile_id="institution-a",
        )
        generator_hash = logical_corpus_sha256_for_records(
            (record for record in records),
            tenant_id="tenant-a",
            profile_id="institution-a",
        )

        self.assertRegex(generator_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(list_hash, generator_hash)

    def test_logical_corpus_hash_does_not_precopy_list_input(self) -> None:
        records = [
            _record(
                "doc-logical-list",
                "logical-list-a",
                regulation_no="1-4",
                regulation_title="Logical List Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="logical list record",
                revision_date="2026-07-01",
            )
        ]

        with patch(
            "app.retrieval.hierarchical_index.canonicalize_runtime_records",
            wraps=canonicalize_runtime_records,
        ) as canonicalize_mock:
            logical_corpus_sha256_for_records(
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertIs(records, canonicalize_mock.call_args.args[0])

    def test_document_article_loader_excludes_non_governing_record_types(
        self,
    ) -> None:
        article = _record(
            "doc-a",
            "article-a",
            regulation_no="1-1",
            regulation_title="테스트 규정",
            article_no="제1조",
            article_title="목적",
            text="제1조 목적과 별지 제1호서식을 설명한다.",
            revision_date="2026-01-01",
        )
        form = _record(
            "doc-a",
            "form-a",
            regulation_no="1-1",
            regulation_title="테스트 규정",
            article_no="",
            article_title="",
            text="별지 제1호서식",
            revision_date="2026-01-01",
            chunk_type="form",
        )
        untitled_article = _record(
            "doc-a",
            "article-untitled",
            regulation_no="1-1",
            regulation_title="테스트 규정",
            article_no="제2조",
            article_title="",
            text="제목이 없는 조문",
            revision_date="2026-01-01",
        )

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = (
                data_dir
                / "vector_db"
                / "tenant-a"
                / "approved_vectors.jsonl"
            )
            records = [article, form, untitled_article]
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulations.sqlite"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            loaded = load_document_article_records(
                index_path,
                vector_path,
                document_id="doc-a",
            )

        self.assertEqual(["article-a"], [record["chunk_id"] for record in loaded])

    def test_search_applies_allowed_units_before_ranking_and_sanitizes_candidates(self) -> None:
        denied_records = [
            _record(
                f"doc-denied-{index:02d}",
                f"chunk-denied-{index:02d}",
                regulation_no=f"9-{index:02d}",
                regulation_title=f"극비키워드 비공개규정 {index:02d}",
                article_no="제1조",
                article_title="목적",
                text="권한이 없는 규정 본문이다.",
                revision_date="2026-01-01",
                metadata_updates={"department_acl": ["legal"]},
            )
            for index in range(12)
        ]
        allowed_record = _record(
            "doc-allowed",
            "chunk-allowed",
            regulation_no="10-1",
            regulation_title="공개 운영규정",
            article_no="제1조",
            article_title="검색",
            text="극비키워드 검색 요청에 답할 수 있는 허용 본문이다.",
            revision_date="2026-01-01",
            metadata_updates={"department_acl": ["hr"]},
        )
        records = [*denied_records, allowed_record]
        allowed_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="공개 운영규정",
            regulation_no="10-1",
        )

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            direct_results, direct_trace = search_hierarchical_records(
                index_path,
                vector_path,
                query="극비키워드",
                top_k=1,
                profile_id="institution-a",
                allowed_unit_ids={allowed_unit_id},
            )
            settings = Settings(data_dir=data_dir)
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )
            verified_token = SimpleNamespace(
                index_path=index_path,
                vector_path=vector_path,
                index_identity=regulation_tools.routes_rag.path_signature(
                    index_path
                ),
                vector_identity=regulation_tools.routes_rag.path_signature(
                    vector_path
                ),
                is_current=lambda: True,
            )
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token_for_scope",
                    return_value=verified_token,
                ),
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value={allowed_unit_id},
                ) as visible_units,
            ):
                public_search = search_regulations(
                    settings=settings,
                    auth=auth,
                    query="극비키워드",
                    top_k=1,
                    profile_id="institution-a",
                    security_levels=["internal"],
                    department_ids=["hr"],
                    metadata_profile="chatgpt-data",
                )

        self.assertEqual(
            ["chunk-allowed"],
            [record["chunk_id"] for _score, record in direct_results],
        )
        self.assertEqual(
            ["공개 운영규정"],
            [
                item["regulation_title"]
                for item in direct_trace["candidate_regulations"]
            ],
        )
        self.assertNotIn(
            "document_id",
            json.dumps(direct_trace["candidate_regulations"], ensure_ascii=False),
        )
        self.assertIn("허용 본문", public_search["results"][0]["text"])
        candidate_payload = json.dumps(
            public_search["metadata"]["candidate_regulations"],
            ensure_ascii=False,
        )
        self.assertIn("공개 운영규정", candidate_payload)
        self.assertNotIn("비공개규정", candidate_payload)
        chatgpt_payload = json.dumps(
            regulation_tools.chatgpt_data_search_output(public_search).model_dump(),
            ensure_ascii=False,
        )
        for forbidden_key in ("document_id", "chunk_id", "profile_id", "version_id"):
            self.assertNotIn(f'"{forbidden_key}"', candidate_payload)
            self.assertNotIn(f'"{forbidden_key}"', chatgpt_payload)
        visible_units.assert_called_once()

    def test_default_current_selection_transitions_on_calendar_date_without_rebuild(self) -> None:
        old = _record(
            "doc-calendar-old",
            "chunk-calendar-old",
            regulation_no="4-77",
            regulation_title="달력전환규정",
            article_no="제16조",
            article_title="전환",
            text="달력 전환 검증을 위한 구 규정 본문이다.",
            revision_date="2026-07-01",
        )
        future = _record(
            "doc-calendar-new",
            "chunk-calendar-new",
            regulation_no="4-77",
            regulation_title="달력전환규정",
            article_no="제16조",
            article_title="전환",
            text="달력 전환 검증을 위한 신 규정 본문이다.",
            revision_date="2026-08-01",
        )
        records = [old, future]
        unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="달력전환규정",
            regulation_no="4-77",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            with patch(
                "app.retrieval.hierarchical_index._default_as_of_date",
                return_value="2026-07-31",
            ):
                before_list = list_indexed_regulations(
                    index_path,
                    profile_id="institution-a",
                )
                before_page, _ = page_indexed_regulations(
                    index_path,
                    profile_id="institution-a",
                )
                before_toc = regulation_toc(
                    index_path,
                    regulation_unit_id=unit_id,
                )
                before_article = load_article_records(
                    index_path,
                    vector_path,
                    regulation_unit_id=unit_id,
                    article_no="제16조",
                )
                before_search, _ = search_hierarchical_records(
                    index_path,
                    vector_path,
                    query="달력 전환 검증",
                    top_k=1,
                    profile_id="institution-a",
                )

            with patch(
                "app.retrieval.hierarchical_index._default_as_of_date",
                return_value="2026-08-01",
            ):
                after_list = list_indexed_regulations(
                    index_path,
                    profile_id="institution-a",
                )
                after_page, _ = page_indexed_regulations(
                    index_path,
                    profile_id="institution-a",
                )
                after_toc = regulation_toc(
                    index_path,
                    regulation_unit_id=unit_id,
                )
                after_article = load_article_records(
                    index_path,
                    vector_path,
                    regulation_unit_id=unit_id,
                    article_no="제16조",
                )
                after_search, _ = search_hierarchical_records(
                    index_path,
                    vector_path,
                    query="달력 전환 검증",
                    top_k=1,
                    profile_id="institution-a",
                )
                explicit_before_toc = regulation_toc(
                    index_path,
                    regulation_unit_id=unit_id,
                    as_of_date="2026-07-31",
                )
                explicit_before_search, _ = search_hierarchical_records(
                    index_path,
                    vector_path,
                    query="달력 전환 검증",
                    top_k=1,
                    profile_id="institution-a",
                    as_of_date="2026-07-31",
                )

        self.assertEqual("doc-calendar-old", before_list[0]["document_id"])
        self.assertEqual("doc-calendar-old", before_page[0]["document_id"])
        self.assertEqual("doc-calendar-old", before_toc["regulation"]["document_id"])
        self.assertEqual(["doc-calendar-old"], [item["document_id"] for item in before_article])
        self.assertEqual("doc-calendar-old", before_search[0][1]["document_id"])

        self.assertEqual("doc-calendar-new", after_list[0]["document_id"])
        self.assertEqual("doc-calendar-new", after_page[0]["document_id"])
        self.assertEqual("doc-calendar-new", after_toc["regulation"]["document_id"])
        self.assertEqual(["doc-calendar-new"], [item["document_id"] for item in after_article])
        self.assertEqual("doc-calendar-new", after_search[0][1]["document_id"])
        self.assertTrue(after_list[0]["is_current"])
        self.assertTrue(after_toc["regulation"]["is_current"])

        self.assertEqual(
            "doc-calendar-old",
            explicit_before_toc["regulation"]["document_id"],
        )
        self.assertEqual(
            "doc-calendar-old",
            explicit_before_search[0][1]["document_id"],
        )

    def test_unresolved_target_title_is_public_but_acl_denied_resolved_target_is_absent(self) -> None:
        source = _record(
            "doc-reference-source",
            "chunk-reference-source",
            regulation_no="8-1",
            regulation_title="공개준용규정",
            article_no="제1조",
            article_title="준용",
            text="재무규정 제16조와 비밀규정 제1조를 따른다.",
            revision_date="2026-01-01",
            metadata_updates={
                "department_acl": ["hr"],
                "regulation_article_refs": [
                    {"regulation_ref": "재무규정", "article_ref": "제16조"},
                    {"regulation_ref": "비밀규정", "article_ref": "제1조"},
                ],
            },
        )
        denied_target = _record(
            "doc-reference-denied",
            "chunk-reference-denied",
            regulation_no="8-2",
            regulation_title="비밀규정",
            article_no="제1조",
            article_title="비밀",
            text="법무 부서 전용 본문이다.",
            revision_date="2026-01-01",
            metadata_updates={"department_acl": ["legal"]},
        )
        source_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="공개준용규정",
            regulation_no="8-1",
        )

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [source, denied_target],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            settings = Settings(data_dir=data_dir)
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, data_dir / "unused.jsonl"),
                ),
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value={source_unit_id},
                ),
            ):
                references = get_regulation_references(
                    settings=settings,
                    auth=auth,
                    regulation_unit_id=source_unit_id,
                    profile_id="institution-a",
                    direction="outgoing",
                )

        self.assertEqual(1, references["total_count"])
        edge = references["references"][0]
        self.assertEqual("unresolved", edge["status"])
        self.assertEqual(
            {"regulation_title": "재무규정"},
            edge["target_regulation"],
        )
        self.assertEqual("제16조", edge["requested_article"]["locator"])
        public_payload = json.dumps(references, ensure_ascii=False)
        self.assertNotIn("비밀규정", public_payload)
        for forbidden_key in ("document_id", "chunk_id", "profile_id", "version_id"):
            self.assertNotIn(f'"{forbidden_key}"', public_payload)

    def test_catalog_reference_and_cycle_scope_fail_closed_for_department_acl(self) -> None:
        records = [
            _record(
                "doc-hr",
                "chunk-hr",
                regulation_no="1-1",
                regulation_title="인사 규정",
                article_no="제1조",
                article_title="목적",
                text="법무 규정을 따른다.",
                revision_date="2026-07-01",
                metadata_updates={
                    "department_acl": ["hr"],
                    "internal_regulation_refs": ["법무 규정"],
                },
            ),
            _record(
                "doc-legal",
                "chunk-legal",
                regulation_no="1-2",
                regulation_title="법무 규정",
                article_no="제1조",
                article_title="목적",
                text="인사 규정을 따른다.",
                revision_date="2026-07-01",
                metadata_updates={
                    "department_acl": ["legal"],
                    "internal_regulation_refs": ["인사 규정"],
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            hr_auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )
            visible_records = [
                record
                for record in records
                if regulation_tools._hierarchical_record_visible_to_request(
                    record,
                    auth=hr_auth,
                    security_levels=None,
                    department_ids=None,
                    profile_id="institution-a",
                    document_id=None,
                )
            ]
            allowed_unit_ids = fully_visible_regulation_unit_ids(
                index_path,
                visible_record_keys={
                    (record["document_id"], record["chunk_id"])
                    for record in visible_records
                },
                profile_id="institution-a",
            )
            catalog, total_count = page_indexed_regulations(
                index_path,
                profile_id="institution-a",
                allowed_unit_ids=allowed_unit_ids,
            )
            hr_unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="인사 규정",
                regulation_no="1-1",
            )
            references = regulation_references(
                index_path,
                regulation_unit_id=hr_unit_id,
                allowed_unit_ids=allowed_unit_ids,
            )
            cycles, cycle_count = page_reference_cycles(
                index_path,
                profile_id="institution-a",
                allowed_unit_ids=allowed_unit_ids,
            )

        self.assertEqual(1, len(visible_records))
        self.assertEqual({hr_unit_id}, allowed_unit_ids)
        self.assertEqual(1, total_count)
        self.assertEqual(["인사 규정"], [item["regulation_title"] for item in catalog])
        self.assertEqual(0, references["total_count"])
        self.assertEqual([], references["references"])
        self.assertEqual(0, cycle_count)
        self.assertEqual([], cycles)

    def test_signature_visibility_includes_unit_when_every_hash_matches(self) -> None:
        records = [
            _record(
                "doc-signature",
                "chunk-signature-1",
                regulation_no="2-1",
                regulation_title="서명 검증 규정",
                article_no="제1조",
                article_title="목적",
                text="첫 번째 승인 본문이다.",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-signature",
                "chunk-signature-2",
                regulation_no="2-1",
                regulation_title="서명 검증 규정",
                article_no="제2조",
                article_title="범위",
                text="두 번째 승인 본문이다.",
                revision_date="2026-07-01",
            ),
        ]
        expected_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="서명 검증 규정",
            regulation_no="2-1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            visible_unit_ids = fully_visible_regulation_unit_ids(
                index_path,
                visible_record_signatures={
                    (
                        str(record["document_id"]),
                        str(record["chunk_id"]),
                        str(record["content_hash"]),
                    )
                    for record in records
                },
                profile_id="institution-a",
            )

        self.assertEqual({expected_unit_id}, visible_unit_ids)

    def test_signature_visibility_excludes_unit_when_one_hash_mismatches(self) -> None:
        records = [
            _record(
                "doc-signature",
                "chunk-signature-1",
                regulation_no="2-1",
                regulation_title="서명 검증 규정",
                article_no="제1조",
                article_title="목적",
                text="첫 번째 승인 본문이다.",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-signature",
                "chunk-signature-2",
                regulation_no="2-1",
                regulation_title="서명 검증 규정",
                article_no="제2조",
                article_title="범위",
                text="두 번째 승인 본문이다.",
                revision_date="2026-07-01",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            visible_unit_ids = fully_visible_regulation_unit_ids(
                index_path,
                visible_record_signatures={
                    (
                        str(record["document_id"]),
                        str(record["chunk_id"]),
                        (
                            "mismatched-content-hash"
                            if record["chunk_id"] == "chunk-signature-2"
                            else str(record["content_hash"])
                        ),
                    )
                    for record in records
                },
                profile_id="institution-a",
            )

        self.assertEqual(set(), visible_unit_ids)

    def test_indexed_document_ids_respects_profile_scope(self) -> None:
        records = [
            _record(
                "doc-profile-a",
                "chunk-profile-a",
                regulation_no="3-1",
                regulation_title="기관 A 규정",
                article_no="제1조",
                article_title="목적",
                text="기관 A 본문이다.",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-profile-b",
                "chunk-profile-b",
                regulation_no="3-2",
                regulation_title="기관 B 규정",
                article_no="제1조",
                article_title="목적",
                text="기관 B 본문이다.",
                revision_date="2026-07-01",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id=None,
            )
            all_document_ids = indexed_document_ids(index_path)
            profile_document_ids = indexed_document_ids(
                index_path,
                profile_id="INSTITUTION-A",
            )
            other_profile_document_ids = indexed_document_ids(
                index_path,
                profile_id="institution-b",
            )
            summary = index_summary(index_path)

        self.assertEqual({"doc-profile-a", "doc-profile-b"}, all_document_ids)
        self.assertEqual({"doc-profile-a", "doc-profile-b"}, profile_document_ids)
        self.assertEqual(set(), other_profile_document_ids)
        self.assertEqual("institution-a", summary["profile_id"])

    def test_indexed_chunk_topology_cache_reuses_sqlite_scan_across_callers(
        self,
    ) -> None:
        record = _record(
            "doc-cache-a",
            "chunk-cache-a",
            regulation_no="3-3",
            regulation_title="Cache Topology Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="cache-backed topology row",
            revision_date="2026-07-01",
        )
        visible_signature = {
            (
                str(record["document_id"]),
                str(record["chunk_id"]),
                str(record["content_hash"]),
            )
        }
        expected_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="Cache Topology Regulation",
            regulation_no="3-3",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            with patch.object(
                hierarchical_index_module,
                "_connect_readonly",
                wraps=hierarchical_index_module._connect_readonly,
            ) as readonly_connect:
                document_ids = indexed_document_ids(
                    index_path,
                    profile_id="institution-a",
                )
                visible_unit_ids = fully_visible_regulation_unit_ids(
                    index_path,
                    visible_record_signatures=visible_signature,
                    profile_id="institution-a",
                )

        self.assertEqual({"doc-cache-a"}, document_ids)
        self.assertEqual({expected_unit_id}, visible_unit_ids)
        self.assertEqual(1, readonly_connect.call_count)

    def test_indexed_chunk_topology_cache_invalidates_after_rebuild(self) -> None:
        first_records = [
            _record(
                "doc-cache-first",
                "chunk-cache-first",
                regulation_no="3-4",
                regulation_title="Cache Invalidate Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="first indexed record",
                revision_date="2026-07-01",
            )
        ]
        second_records = first_records + [
            _record(
                "doc-cache-second",
                "chunk-cache-second",
                regulation_no="3-4",
                regulation_title="Cache Invalidate Regulation",
                article_no="Article 2",
                article_title="Scope",
                text="second indexed record",
                revision_date="2026-07-01",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                first_records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            with patch.object(
                hierarchical_index_module,
                "_connect_readonly",
                wraps=hierarchical_index_module._connect_readonly,
            ) as readonly_connect:
                first_document_ids = indexed_document_ids(
                    index_path,
                    profile_id="institution-a",
                )
                build_hierarchical_runtime_index(
                    index_path,
                    second_records,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )
                second_document_ids = indexed_document_ids(
                    index_path,
                    profile_id="institution-a",
                )

        self.assertEqual({"doc-cache-first"}, first_document_ids)
        self.assertEqual(
            {"doc-cache-first", "doc-cache-second"},
            second_document_ids,
        )
        self.assertEqual(2, readonly_connect.call_count)

    def test_failed_rebuild_preserves_existing_index_and_removes_staging_file(
        self,
    ) -> None:
        old_record = _record(
            "doc-atomic-old",
            "chunk-atomic-old",
            regulation_no="3-4-1",
            regulation_title="Atomic Rebuild Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="existing index content must survive a failed rebuild",
            revision_date="2026-07-01",
        )
        new_record = _record(
            "doc-atomic-new",
            "chunk-atomic-new",
            regulation_no="3-4-2",
            regulation_title="Failed Replacement Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="this staged replacement must never become visible",
            revision_date="2026-07-02",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "regulation_hierarchy.sqlite3"
            old_result = build_hierarchical_runtime_index(
                index_path,
                [old_record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            old_bytes = index_path.read_bytes()
            old_summary = index_summary(index_path)

            with patch(
                "app.retrieval.regulation_reference_graph."
                "build_regulation_reference_graph",
                side_effect=RuntimeError("synthetic staged rebuild failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "synthetic staged rebuild failure",
                ):
                    build_hierarchical_runtime_index(
                        index_path,
                        [new_record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )

            remaining_staging_files = list(
                root.glob(f".{index_path.name}.*.tmp")
            )
            preserved_bytes = index_path.read_bytes()
            preserved_sha256 = hierarchical_index._sha256_file(index_path)
            preserved_document_ids = indexed_document_ids(
                index_path,
                profile_id="institution-a",
            )
            preserved_summary = index_summary(index_path)

        self.assertEqual(old_bytes, preserved_bytes)
        self.assertEqual(old_result["sha256"], preserved_sha256)
        self.assertEqual(old_summary, preserved_summary)
        self.assertEqual({"doc-atomic-old"}, preserved_document_ids)
        self.assertEqual([], remaining_staging_files)

    def test_generation_map_does_not_accumulate_for_distinct_built_paths(
        self,
    ) -> None:
        record = _record(
            "doc-generation-prune",
            "chunk-generation-prune",
            regulation_no="3-4-3",
            regulation_title="Generation Prune Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="completed builds must not retain unused path generations",
            revision_date="2026-07-01",
        )
        with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
            initial_paths = set(
                hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            built_paths = [
                root / f"{uuid4()}.sqlite3"
                for _ in range(32)
            ]
            for index_path in built_paths:
                build_hierarchical_runtime_index(
                    index_path,
                    [record],
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )

            resolved_built_paths = {path.resolve() for path in built_paths}
            with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
                retained_paths = set(
                    hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
                )

        self.assertEqual(initial_paths, retained_paths)
        self.assertTrue(resolved_built_paths.isdisjoint(retained_paths))

    def test_generation_map_does_not_accumulate_for_distinct_failed_builds(
        self,
    ) -> None:
        record = _record(
            "doc-generation-failed-prune",
            "chunk-generation-failed-prune",
            regulation_no="3-4-3-failed",
            regulation_title="Failed Generation Prune Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="failed builds must not retain unused path generations",
            revision_date="2026-07-01",
        )
        with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
            initial_paths = set(
                hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed_paths = [root / f"{uuid4()}.sqlite3" for _ in range(8)]
            with patch(
                "app.retrieval.regulation_reference_graph."
                "build_regulation_reference_graph",
                side_effect=RuntimeError("synthetic distinct build failure"),
            ):
                for index_path in failed_paths:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "synthetic distinct build failure",
                    ):
                        build_hierarchical_runtime_index(
                            index_path,
                            [record],
                            tenant_id="tenant-a",
                            profile_id="institution-a",
                        )

            self.assertEqual([], list(root.glob(".*.tmp")))
            with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
                retained_paths = set(
                    hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
                )

        self.assertEqual(initial_paths, retained_paths)

    def test_connect_failure_cleans_partial_staging_and_generation(self) -> None:
        record = _record(
            "doc-connect-failure",
            "chunk-connect-failure",
            regulation_no="3-4-3-connect",
            regulation_title="Connect Failure Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="a partial sqlite staging file must be removed",
            revision_date="2026-07-01",
        )
        with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
            initial_paths = set(
                hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "regulation_hierarchy.sqlite3"

            def fail_after_partial_create(path, *_args, **_kwargs):
                Path(path).write_bytes(b"partial sqlite staging")
                raise sqlite3.OperationalError("synthetic connect failure")

            with patch.object(
                hierarchical_index.sqlite3,
                "connect",
                side_effect=fail_after_partial_create,
            ):
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "synthetic connect failure",
                ):
                    build_hierarchical_runtime_index(
                        index_path,
                        [record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )

            self.assertFalse(index_path.exists())
            self.assertEqual([], list(root.glob(".*.tmp")))
            with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
                retained_paths = set(
                    hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
                )

        self.assertEqual(initial_paths, retained_paths)

    def test_close_failure_cleans_completed_staging_and_generation(self) -> None:
        record = _record(
            "doc-close-failure",
            "chunk-close-failure",
            regulation_no="3-4-3-close",
            regulation_title="Close Failure Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="a completed staging database must not publish after close fails",
            revision_date="2026-07-01",
        )
        with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
            initial_paths = set(
                hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "regulation_hierarchy.sqlite3"
            original_connect = sqlite3.connect

            class CloseFailingConnection:
                def __init__(self, connection) -> None:
                    self._connection = connection

                def __getattr__(self, name):
                    return getattr(self._connection, name)

                def close(self) -> None:
                    self._connection.close()
                    raise sqlite3.OperationalError("synthetic close failure")

            def close_failing_connect(*args, **kwargs):
                return CloseFailingConnection(original_connect(*args, **kwargs))

            with patch.object(
                hierarchical_index.sqlite3,
                "connect",
                side_effect=close_failing_connect,
            ):
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "synthetic close failure",
                ):
                    build_hierarchical_runtime_index(
                        index_path,
                        [record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )

            self.assertFalse(index_path.exists())
            self.assertEqual([], list(root.glob(".*.tmp")))
            with hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_LOCK:
                retained_paths = set(
                    hierarchical_index._INDEXED_CHUNK_TOPOLOGY_GENERATIONS
                )

        self.assertEqual(initial_paths, retained_paths)

    def test_cross_process_build_lock_times_out_and_keeps_fixed_lockfile(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            holder_acquired = context.Event()
            release_holder = context.Event()
            holder = context.Process(
                target=_hold_hierarchical_index_build_lock,
                args=(str(index_path), holder_acquired, release_holder),
            )
            holder.start()
            try:
                self.assertTrue(holder_acquired.wait(timeout=10))
                with self.assertRaisesRegex(
                    TimeoutError,
                    "hierarchical index lock",
                ):
                    with hierarchical_index._hierarchical_index_build_guard(
                        index_path,
                        timeout_seconds=0.1,
                    ):
                        self.fail("contending process unexpectedly acquired lock")
                _target, lock_path = (
                    hierarchical_index._confined_hierarchical_index_paths(
                        index_path
                    )
                )
                self.assertTrue(lock_path.is_file())
                self.assertGreaterEqual(lock_path.stat().st_size, 1)
            finally:
                release_holder.set()
                holder.join(timeout=10)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(timeout=5)
            self.assertEqual(0, holder.exitcode)

    def test_cross_process_build_lock_reacquires_after_owner_termination(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            holder_acquired = context.Event()
            never_release = context.Event()
            holder = context.Process(
                target=_hold_hierarchical_index_build_lock,
                args=(str(index_path), holder_acquired, never_release),
            )
            holder.start()
            self.assertTrue(holder_acquired.wait(timeout=10))
            holder.terminate()
            holder.join(timeout=10)
            self.assertFalse(holder.is_alive())

            with hierarchical_index._hierarchical_index_build_guard(
                index_path,
                timeout_seconds=5,
            ):
                reacquired = True
            _target, lock_path = (
                hierarchical_index._confined_hierarchical_index_paths(index_path)
            )

        self.assertTrue(reacquired)
        self.assertTrue(lock_path.name.endswith(".reg-rag.lock"))

    def test_windows_lock_retryable_errno_times_out_instead_of_masking_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".regulation_hierarchy.sqlite3.reg-rag.lock"
            file_descriptor = hierarchical_index._open_hierarchical_index_lock_file(
                lock_path
            )
            lock_attempts = 0

            def lock_contention(*_args) -> None:
                nonlocal lock_attempts
                lock_attempts += 1
                raise PermissionError(errno.EACCES, "synthetic lock contention")

            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lock_contention)
            try:
                with patch.object(hierarchical_index.os, "name", "nt"), patch.dict(
                    sys.modules, {"msvcrt": fake_msvcrt}
                ):
                    with self.assertRaises(TimeoutError) as raised:
                        hierarchical_index._acquire_hierarchical_index_file_lock(
                            file_descriptor,
                            lock_path,
                            deadline=time.monotonic(),
                        )
            finally:
                os.close(file_descriptor)

        self.assertEqual(1, lock_attempts)
        self.assertIsInstance(raised.exception.__cause__, PermissionError)
        self.assertEqual(errno.EACCES, raised.exception.__cause__.errno)

    def test_windows_lock_non_contention_oserror_propagates_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".regulation_hierarchy.sqlite3.reg-rag.lock"
            file_descriptor = hierarchical_index._open_hierarchical_index_lock_file(
                lock_path
            )
            lock_attempts = 0

            def missing_lock_file(*_args) -> None:
                nonlocal lock_attempts
                lock_attempts += 1
                raise FileNotFoundError(errno.ENOENT, "synthetic missing lock")

            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=missing_lock_file)
            try:
                with patch.object(hierarchical_index.os, "name", "nt"), patch.dict(
                    sys.modules, {"msvcrt": fake_msvcrt}
                ):
                    with self.assertRaises(FileNotFoundError) as raised:
                        hierarchical_index._acquire_hierarchical_index_file_lock(
                            file_descriptor,
                            lock_path,
                            deadline=time.monotonic() + 60.0,
                        )
            finally:
                os.close(file_descriptor)

        self.assertEqual(1, lock_attempts)
        self.assertEqual(errno.ENOENT, raised.exception.errno)

    def test_cross_process_builders_hash_their_own_committed_generation(self) -> None:
        first_records = [
            _record(
                "doc-process-writer-first",
                "chunk-process-writer-first",
                regulation_no="3-4-3-process-a",
                regulation_title="Process Writer A Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="the first process hashes its own committed index",
                revision_date="2026-07-01",
            )
        ]
        second_records = [
            *first_records,
            _record(
                "doc-process-writer-second",
                "chunk-process-writer-second",
                regulation_no="3-4-3-process-b",
                regulation_title="Process Writer B Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="the second process commits after the first returns",
                revision_date="2026-07-02",
            ),
        ]
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            first_started = context.Event()
            first_hash_started = context.Event()
            release_first_hash = context.Event()
            first_finished = context.Event()
            second_started = context.Event()
            unused_second_hash = context.Event()
            unused_second_release = context.Event()
            second_finished = context.Event()
            results = context.Queue()
            first = context.Process(
                target=_build_hierarchical_index_in_process,
                args=(str(index_path), first_records),
                kwargs={
                    "pause_hash": True,
                    "started": first_started,
                    "hash_started": first_hash_started,
                    "release_hash": release_first_hash,
                    "finished": first_finished,
                    "results": results,
                },
            )
            second = context.Process(
                target=_build_hierarchical_index_in_process,
                args=(str(index_path), second_records),
                kwargs={
                    "pause_hash": False,
                    "started": second_started,
                    "hash_started": unused_second_hash,
                    "release_hash": unused_second_release,
                    "finished": second_finished,
                    "results": results,
                },
            )
            first.start()
            try:
                self.assertTrue(first_started.wait(timeout=10))
                self.assertTrue(first_hash_started.wait(timeout=15))
                second.start()
                self.assertTrue(second_started.wait(timeout=10))
                self.assertFalse(second_finished.wait(timeout=0.35))
                release_first_hash.set()
                outcomes = [results.get(timeout=20), results.get(timeout=20)]
                first.join(timeout=10)
                second.join(timeout=10)
                self.assertEqual(0, first.exitcode)
                self.assertEqual(0, second.exitcode)
                self.assertFalse(any(outcome[0] == "error" for outcome in outcomes))
                summaries = {
                    outcome[1]["record_count"]: outcome[1]
                    for outcome in outcomes
                }
                final_sha256 = hierarchical_index._sha256_file(index_path)
                final_summary = index_summary(index_path)
            finally:
                release_first_hash.set()
                for process in (first, second):
                    if process.pid is not None and process.is_alive():
                        process.terminate()
                    if process.pid is not None:
                        process.join(timeout=5)

        self.assertEqual({1, 2}, set(summaries))
        self.assertNotEqual(summaries[1]["sha256"], summaries[2]["sha256"])
        self.assertEqual(summaries[2]["sha256"], final_sha256)
        self.assertEqual(2, final_summary["record_count"])

    def test_same_target_builders_return_their_own_committed_hashes(self) -> None:
        first_records = [
            _record(
                "doc-writer-first",
                "chunk-writer-first",
                regulation_no="3-4-3-writer-a",
                regulation_title="Writer A Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="the first writer must hash its own committed index",
                revision_date="2026-07-01",
            )
        ]
        second_records = [
            *first_records,
            _record(
                "doc-writer-second",
                "chunk-writer-second",
                regulation_no="3-4-3-writer-b",
                regulation_title="Writer B Regulation",
                article_no="Article 1",
                article_title="Purpose",
                text="the second writer commits only after the first returns",
                revision_date="2026-07-02",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            first_hash_started = threading.Event()
            release_first_hash = threading.Event()
            original_sha256 = hierarchical_index._sha256_file
            hash_call_lock = threading.Lock()
            hash_call_count = 0

            def pause_first_hash(path: Path) -> str:
                nonlocal hash_call_count
                with hash_call_lock:
                    hash_call_count += 1
                    is_first = hash_call_count == 1
                if is_first:
                    first_hash_started.set()
                    if not release_first_hash.wait(timeout=5):
                        raise TimeoutError("timed out waiting to hash first build")
                return original_sha256(path)

            with patch.object(
                hierarchical_index,
                "_sha256_file",
                side_effect=pause_first_hash,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(
                        build_hierarchical_runtime_index,
                        index_path,
                        first_records,
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )
                    try:
                        self.assertTrue(first_hash_started.wait(timeout=5))
                        second_future = executor.submit(
                            build_hierarchical_runtime_index,
                            index_path,
                            second_records,
                            tenant_id="tenant-a",
                            profile_id="institution-a",
                        )
                        self.assertFalse(second_future.done())
                    finally:
                        release_first_hash.set()
                    first_result = first_future.result(timeout=5)
                    second_result = second_future.result(timeout=5)

            final_sha256 = original_sha256(index_path)
            final_summary = index_summary(index_path)
            with hierarchical_index._HIERARCHICAL_INDEX_BUILD_LOCKS_GUARD:
                retained_build_locks = dict(
                    hierarchical_index._HIERARCHICAL_INDEX_BUILD_LOCKS
                )

        self.assertEqual(1, first_result["record_count"])
        self.assertEqual(2, second_result["record_count"])
        self.assertNotEqual(first_result["sha256"], second_result["sha256"])
        self.assertEqual(second_result["sha256"], final_sha256)
        self.assertEqual(2, final_summary["record_count"])
        self.assertNotIn(index_path.resolve(), retained_build_locks)

    def test_reader_sees_old_index_until_atomic_rebuild_commit(self) -> None:
        old_record = _record(
            "doc-reader-old",
            "chunk-reader-old",
            regulation_no="3-4-4",
            regulation_title="Reader Old Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="readers may continue using the old committed index",
            revision_date="2026-07-01",
        )
        new_record = _record(
            "doc-reader-new",
            "chunk-reader-new",
            regulation_no="3-4-5",
            regulation_title="Reader New Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="readers see this record only after atomic replacement",
            revision_date="2026-07-02",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [old_record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            replace_started = threading.Event()
            release_replace = threading.Event()
            original_replace = os.replace

            def pause_target_replace(source, destination) -> None:
                if Path(destination) == index_path:
                    replace_started.set()
                    if not release_replace.wait(timeout=5):
                        raise TimeoutError("timed out waiting to commit rebuild")
                original_replace(source, destination)

            with patch.object(
                hierarchical_index.os,
                "replace",
                side_effect=pause_target_replace,
            ):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    rebuild = executor.submit(
                        build_hierarchical_runtime_index,
                        index_path,
                        [new_record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )
                    try:
                        self.assertTrue(replace_started.wait(timeout=5))
                        during_rebuild = indexed_document_ids(
                            index_path,
                            profile_id="institution-a",
                        )
                    finally:
                        release_replace.set()
                    rebuild.result(timeout=5)

            after_commit = indexed_document_ids(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual({"doc-reader-old"}, during_rebuild)
        self.assertEqual({"doc-reader-new"}, after_commit)

    def test_atomic_rebuild_retries_transient_windows_reader_lock(self) -> None:
        old_record = _record(
            "doc-retry-old",
            "chunk-retry-old",
            regulation_no="3-4-6",
            regulation_title="Retry Old Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="old committed index",
            revision_date="2026-07-01",
        )
        new_record = _record(
            "doc-retry-new",
            "chunk-retry-new",
            regulation_no="3-4-7",
            regulation_title="Retry New Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="new index after a transient reader lock",
            revision_date="2026-07-02",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [old_record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original_replace = os.replace
            attempts = 0

            def lock_twice_then_replace(source, destination) -> None:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise PermissionError("synthetic Windows reader lock")
                original_replace(source, destination)

            with patch.object(
                hierarchical_index.os,
                "replace",
                side_effect=lock_twice_then_replace,
            ), patch.object(hierarchical_index.time, "sleep", return_value=None):
                build_hierarchical_runtime_index(
                    index_path,
                    [new_record],
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )

            document_ids = indexed_document_ids(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(3, attempts)
        self.assertEqual({"doc-retry-new"}, document_ids)

    def test_atomic_rebuild_timeout_preserves_old_index_and_cleans_staging(self) -> None:
        old_record = _record(
            "doc-timeout-old",
            "chunk-timeout-old",
            regulation_no="3-4-8",
            regulation_title="Timeout Old Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="old index survives replacement timeout",
            revision_date="2026-07-01",
        )
        new_record = _record(
            "doc-timeout-new",
            "chunk-timeout-new",
            regulation_no="3-4-9",
            regulation_title="Timeout New Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="locked replacement is never published",
            revision_date="2026-07-02",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [old_record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            old_bytes = index_path.read_bytes()

            with patch.object(
                hierarchical_index,
                "_INDEX_REPLACE_RETRY_SECONDS",
                0.0,
            ), patch.object(
                hierarchical_index.os,
                "replace",
                side_effect=PermissionError("persistent Windows reader lock"),
            ):
                with self.assertRaisesRegex(PermissionError, "persistent Windows"):
                    build_hierarchical_runtime_index(
                        index_path,
                        [new_record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )

            document_ids = indexed_document_ids(
                index_path,
                profile_id="institution-a",
            )
            preserved_bytes = index_path.read_bytes()
            staging_files = list(root.glob(f".{index_path.name}.*.tmp"))

        self.assertEqual(old_bytes, preserved_bytes)
        self.assertEqual({"doc-timeout-old"}, document_ids)
        self.assertEqual([], staging_files)

    def test_indexed_chunk_topology_cache_normalizes_profile_without_poisoning_scope(
        self,
    ) -> None:
        record = _record(
            "doc-profile-normalized",
            "chunk-profile-normalized",
            regulation_no="3-5",
            regulation_title="Normalized Profile Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="normalized profile cache row",
            revision_date="2026-07-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

            padded_result = indexed_document_ids(
                index_path,
                profile_id=" INSTITUTION-A ",
            )
            canonical_result = indexed_document_ids(
                index_path,
                profile_id="institution-a",
            )
            whitespace_only_result = indexed_document_ids(
                index_path,
                profile_id="   ",
            )
            unscoped_result = indexed_document_ids(index_path)

        self.assertEqual({"doc-profile-normalized"}, padded_result)
        self.assertEqual({"doc-profile-normalized"}, canonical_result)
        self.assertEqual(set(), whitespace_only_result)
        self.assertEqual({"doc-profile-normalized"}, unscoped_result)

    def test_indexed_chunk_topology_cache_is_immutable_and_size_bounded(self) -> None:
        record = _record(
            "doc-cache-bounded",
            "chunk-cache-bounded",
            regulation_no="3-6",
            regulation_title="Bounded Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="bounded topology cache row",
            revision_date="2026-07-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            topology = hierarchical_index._indexed_chunk_topology(
                index_path,
                profile_id="institution-a",
            )
            with self.assertRaises(TypeError):
                topology.unit_keys["mutated"] = frozenset()

            with patch.object(
                hierarchical_index,
                "_INDEXED_CHUNK_TOPOLOGY_CACHE_MAX_ENTRY_BYTES",
                1,
            ):
                hierarchical_index._evict_indexed_chunk_topology_cache(index_path)
                uncached = hierarchical_index._indexed_chunk_topology(
                    index_path,
                    profile_id="institution-a",
                )

        self.assertGreater(uncached.estimated_size_bytes, 1)
        self.assertEqual({}, hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE)
        self.assertEqual(0, hierarchical_index._INDEXED_CHUNK_TOPOLOGY_CACHE_BYTES)

    def test_indexed_chunk_topology_cache_single_flights_concurrent_cold_callers(
        self,
    ) -> None:
        record = _record(
            "doc-cache-concurrent",
            "chunk-cache-concurrent",
            regulation_no="3-7",
            regulation_title="Concurrent Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="concurrent topology cache row",
            revision_date="2026-07-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original_scan = hierarchical_index._scan_indexed_chunk_topology

            def slow_scan(*args, **kwargs):
                time.sleep(0.05)
                return original_scan(*args, **kwargs)

            with patch.object(
                hierarchical_index,
                "_scan_indexed_chunk_topology",
                side_effect=slow_scan,
            ) as scan:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(
                        executor.map(
                            lambda _index: indexed_document_ids(
                                index_path,
                                profile_id="institution-a",
                            ),
                            range(8),
                        )
                    )

        self.assertEqual([{"doc-cache-concurrent"}] * 8, results)
        self.assertEqual(1, scan.call_count)
        self.assertEqual({}, hierarchical_index._INDEXED_CHUNK_TOPOLOGY_INFLIGHT)

    def test_indexed_chunk_topology_cache_retries_after_scan_failure(self) -> None:
        record = _record(
            "doc-cache-retry",
            "chunk-cache-retry",
            regulation_no="3-8",
            regulation_title="Retry Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="retry topology cache row",
            revision_date="2026-07-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original_scan = hierarchical_index._scan_indexed_chunk_topology
            attempts = 0

            def fail_once(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("synthetic topology scan failure")
                return original_scan(*args, **kwargs)

            with patch.object(
                hierarchical_index,
                "_scan_indexed_chunk_topology",
                side_effect=fail_once,
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic topology"):
                    indexed_document_ids(
                        index_path,
                        profile_id="institution-a",
                    )
                self.assertEqual(
                    {},
                    hierarchical_index._INDEXED_CHUNK_TOPOLOGY_INFLIGHT,
                )
                retry_result = indexed_document_ids(
                    index_path,
                    profile_id="institution-a",
                )

        self.assertEqual({"doc-cache-retry"}, retry_result)
        self.assertEqual(2, attempts)

    def test_indexed_chunk_topology_cache_retries_flight_overlapping_rebuild(
        self,
    ) -> None:
        first_record = _record(
            "doc-cache-first",
            "chunk-cache-first",
            regulation_no="3-9",
            regulation_title="Rebuild Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="first topology row",
            revision_date="2026-07-01",
        )
        second_record = _record(
            "doc-cache-second",
            "chunk-cache-second",
            regulation_no="3-10",
            regulation_title="New Rebuild Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="second topology row",
            revision_date="2026-07-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [first_record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original_scan = hierarchical_index._scan_indexed_chunk_topology
            old_scan_ready = threading.Event()
            release_old_scan = threading.Event()
            attempts = 0

            def pause_first_scan(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                result = original_scan(*args, **kwargs)
                if attempts == 1:
                    old_scan_ready.set()
                    self.assertTrue(release_old_scan.wait(timeout=5))
                return result

            with patch.object(
                hierarchical_index,
                "_scan_indexed_chunk_topology",
                side_effect=pause_first_scan,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    leader = executor.submit(
                        indexed_document_ids,
                        index_path,
                        profile_id="institution-a",
                    )
                    self.assertTrue(old_scan_ready.wait(timeout=5))
                    waiter = executor.submit(
                        indexed_document_ids,
                        index_path,
                        profile_id="institution-a",
                    )
                    time.sleep(0.02)
                    build_hierarchical_runtime_index(
                        index_path,
                        [first_record, second_record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )
                    release_old_scan.set()
                    leader_result = leader.result(timeout=5)
                    waiter_result = waiter.result(timeout=5)

        expected = {"doc-cache-first", "doc-cache-second"}
        self.assertEqual(expected, leader_result)
        self.assertEqual(expected, waiter_result)
        self.assertEqual(2, attempts)
        self.assertEqual({}, hierarchical_index._INDEXED_CHUNK_TOPOLOGY_INFLIGHT)

    def test_indexed_chunk_topology_cache_retries_failed_scan_invalidated_by_rebuild(
        self,
    ) -> None:
        first_record = _record(
            "doc-cache-failed-old",
            "chunk-cache-failed-old",
            regulation_no="3-11",
            regulation_title="Failed Old Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="old topology row",
            revision_date="2026-07-01",
        )
        second_record = _record(
            "doc-cache-failed-new",
            "chunk-cache-failed-new",
            regulation_no="3-12",
            regulation_title="Failed New Cache Regulation",
            article_no="Article 1",
            article_title="Purpose",
            text="new topology row",
            revision_date="2026-07-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [first_record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            original_scan = hierarchical_index._scan_indexed_chunk_topology
            first_scan_started = threading.Event()
            release_failed_scan = threading.Event()
            attempts = 0

            def fail_invalidated_scan_once(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    first_scan_started.set()
                    self.assertTrue(release_failed_scan.wait(timeout=5))
                    raise FileNotFoundError("synthetic invalidated index scan")
                return original_scan(*args, **kwargs)

            with patch.object(
                hierarchical_index,
                "_scan_indexed_chunk_topology",
                side_effect=fail_invalidated_scan_once,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    leader = executor.submit(
                        indexed_document_ids,
                        index_path,
                        profile_id="institution-a",
                    )
                    self.assertTrue(first_scan_started.wait(timeout=5))
                    waiter = executor.submit(
                        indexed_document_ids,
                        index_path,
                        profile_id="institution-a",
                    )
                    time.sleep(0.02)
                    build_hierarchical_runtime_index(
                        index_path,
                        [first_record, second_record],
                        tenant_id="tenant-a",
                        profile_id="institution-a",
                    )
                    release_failed_scan.set()
                    leader_result = leader.result(timeout=5)
                    waiter_result = waiter.result(timeout=5)

        expected = {"doc-cache-failed-old", "doc-cache-failed-new"}
        self.assertEqual(expected, leader_result)
        self.assertEqual(expected, waiter_result)
        self.assertEqual(2, attempts)
        self.assertEqual({}, hierarchical_index._INDEXED_CHUNK_TOPOLOGY_INFLIGHT)

    def test_build_rejects_mixed_tenant_records(self) -> None:
        records = [
            _record(
                "doc-tenant-a",
                "chunk-tenant-a",
                regulation_no="3-1",
                regulation_title="기관 A 규정",
                article_no="제1조",
                article_title="목적",
                text="tenant-a 본문이다.",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-tenant-b",
                "chunk-tenant-b",
                regulation_no="3-2",
                regulation_title="기관 B 규정",
                article_no="제1조",
                article_title="목적",
                text="tenant-b 본문이다.",
                revision_date="2026-07-01",
                metadata_updates={"tenant_id": "tenant-b"},
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "tenant_id does not match"):
                build_hierarchical_runtime_index(
                    Path(tmp) / "regulation_hierarchy.sqlite3",
                    records,
                    tenant_id="tenant-a",
                    profile_id="institution-a",
                )

    def test_build_rejects_mixed_profiles_when_global_profile_is_omitted(self) -> None:
        records = [
            _record(
                "doc-profile-a",
                "chunk-profile-a",
                regulation_no="3-1",
                regulation_title="기관 A 규정",
                article_no="제1조",
                article_title="목적",
                text="기관 A 본문이다.",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-profile-b",
                "chunk-profile-b",
                regulation_no="3-2",
                regulation_title="기관 B 규정",
                article_no="제1조",
                article_title="목적",
                text="기관 B 본문이다.",
                revision_date="2026-07-01",
                metadata_updates={"profile_id": "institution-b"},
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "exactly one non-empty profile_id"):
                build_hierarchical_runtime_index(
                    Path(tmp) / "regulation_hierarchy.sqlite3",
                    records,
                    tenant_id="tenant-a",
                    profile_id=None,
                )

    def test_build_rejects_missing_record_scope_and_requires_exact_profile(self) -> None:
        cases = (
            ("tenant_id", "", "tenant_id is required"),
            ("tenant_id", "TENANT-A", "tenant_id does not match"),
            ("profile_id", "", "profile_id is required"),
            (
                "profile_id",
                "INSTITUTION-A",
                "profile_id does not match",
            ),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                record = _record(
                    f"doc-{field}-{value or 'missing'}",
                    f"chunk-{field}-{value or 'missing'}",
                    regulation_no="3-3",
                    regulation_title="범위 검증 규정",
                    article_no="제1조",
                    article_title="목적",
                    text="범위 검증 본문이다.",
                    revision_date="2026-07-01",
                    metadata_updates={field: value},
                )
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaisesRegex(ValueError, message):
                        build_hierarchical_runtime_index(
                            Path(tmp) / "regulation_hierarchy.sqlite3",
                            [record],
                            tenant_id="tenant-a",
                            profile_id="institution-a",
                        )

    def test_fully_visible_unit_ids_preserves_legacy_key_mode(self) -> None:
        record = _record(
            "doc-key-mode",
            "chunk-key-mode",
            regulation_no="4-1",
            regulation_title="기존 키 규정",
            article_no="제1조",
            article_title="목적",
            text="기존 키 기반 가시성 본문이다.",
            revision_date="2026-07-01",
        )
        expected_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="기존 키 규정",
            regulation_no="4-1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            visible_unit_ids = fully_visible_regulation_unit_ids(
                index_path,
                visible_record_keys={
                    (str(record["document_id"]), str(record["chunk_id"]))
                },
                profile_id="institution-a",
            )

        self.assertEqual({expected_unit_id}, visible_unit_ids)

    def test_document_identity_reconciles_chunk_metadata_without_number_collisions(self) -> None:
        self.assertNotEqual(
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="급여규정",
                regulation_no="4-44",
            ),
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="급여규정",
                regulation_no="44-4",
            ),
        )
        self.assertEqual(
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="급여규정",
                regulation_no="제4-44호",
            ),
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="급여규정",
                regulation_no="4.44",
            ),
        )
        records = [
            _record(
                "doc-inconsistent",
                "article-inconsistent",
                regulation_no="4-44",
                regulation_title="직원 급여 규정",
                article_no="제1조",
                article_title="목적",
                text="제1조(목적) 직원 급여의 기준을 정한다.",
                revision_date="2026-07-01",
                metadata_updates={"document_name": "직원 급여 규정"},
            ),
            _record(
                "doc-inconsistent",
                "table-inconsistent",
                regulation_no="직원 급여",
                regulation_title="직원 급여",
                article_no="제2조",
                article_title="지급",
                text="급여 지급표",
                revision_date="2026-07-01",
                chunk_type="table",
                metadata_updates={"document_name": "직원 급여 규정"},
            ),
            _record(
                "doc-inconsistent",
                "article-number-leak",
                regulation_no="제16조",
                regulation_title="직원 급여 규정",
                article_no="제16조",
                article_title="지급",
                text="제16조(지급) 급여 지급 절차를 정한다.",
                revision_date="2026-07-01",
                metadata_updates={"document_name": "직원 급여 규정"},
            ),
            _record(
                "doc-distinct-number",
                "article-distinct-number",
                regulation_no="44-4",
                regulation_title="직원 급여 규정",
                article_no="제1조",
                article_title="목적",
                text="별도 규정의 목적을 정한다.",
                revision_date="2026-07-01",
                metadata_updates={"document_name": "직원 급여 규정"},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(2, summary["regulation_count"])
        self.assertEqual({"4-44", "44-4"}, {item["regulation_no"] for item in catalog})
        by_number = {item["regulation_no"]: item for item in catalog}
        self.assertEqual(3, by_number["4-44"]["chunk_count"])

    def test_document_identity_reuses_title_match_for_repeated_unnumbered_chunks(self) -> None:
        numbered_count = 100
        repeated_count = 250
        records = [
            _record(
                "doc-many-regulations",
                f"numbered-{index}",
                regulation_no=f"4-{index}",
                regulation_title=f"테스트 규정 {index:03d}",
                article_no="제1조",
                article_title="목적",
                text=f"테스트 규정 {index:03d}의 목적을 정한다.",
                revision_date="2026-07-01",
                metadata_updates={"document_name": "기관 규정집"},
            )
            for index in range(numbered_count)
        ]
        records.extend(
            _record(
                "doc-many-regulations",
                f"unnumbered-{index}",
                regulation_no="",
                regulation_title="테스트 규정 050",
                article_no=f"제{index + 2}조",
                article_title="세부사항",
                text="동일 규정에서 번호가 누락된 표와 조문이다.",
                revision_date="2026-07-01",
                metadata_updates={"document_name": "기관 규정집"},
            )
            for index in range(repeated_count)
        )

        original_match = hierarchical_index_module._regulation_titles_match
        with patch.object(
            hierarchical_index_module,
            "_regulation_titles_match",
            wraps=original_match,
        ) as title_match:
            identities = hierarchical_index_module._canonical_record_regulation_identities(
                records,
                fallback_profile_id="institution-a",
            )

        expected_unit_id = identities[("doc-many-regulations", "numbered-50")]["unit_id"]
        self.assertEqual(
            {expected_unit_id},
            {
                identities[("doc-many-regulations", f"unnumbered-{index}")]["unit_id"]
                for index in range(repeated_count)
            },
        )
        self.assertEqual(numbered_count, title_match.call_count)

    def test_stable_regulation_id_keeps_renamed_and_renumbered_revisions_in_one_unit(self) -> None:
        records = [
            _record(
                "doc-personnel-old",
                "chunk-personnel-old",
                regulation_no="4-1",
                regulation_title="인사관리규정",
                article_no="제1조",
                article_title="목적",
                text="구 인사관리규정의 목적을 정한다.",
                revision_date="2024-01-01",
                metadata_updates={
                    "document_name": "인사관리규정",
                    "regulation_id": "reg-personnel-stable",
                },
            ),
            _record(
                "doc-personnel-new",
                "chunk-personnel-new",
                regulation_no="7-9",
                regulation_title="인사운영규정",
                article_no="제1조",
                article_title="목적",
                text="개정 인사운영규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "인사운영규정",
                    "regulation_id": "reg-personnel-stable",
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            current = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )
            history = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
                include_history=True,
            )

        self.assertEqual(1, summary["regulation_count"])
        self.assertEqual(2, summary["regulation_version_count"])
        self.assertEqual(["인사운영규정"], [item["regulation_title"] for item in current])
        self.assertEqual(
            {"인사관리규정", "인사운영규정"},
            {item["regulation_title"] for item in history},
        )
        self.assertEqual({"4-1", "7-9"}, {item["regulation_no"] for item in history})
        self.assertEqual(1, len({item["regulation_unit_id"] for item in history}))

    def test_shared_binder_regulation_id_does_not_collapse_numbered_siblings(self) -> None:
        records = [
            _record(
                "doc-shared-binder",
                "chunk-personnel",
                regulation_no="4-1",
                regulation_title="인사규정",
                article_no="제1조",
                article_title="목적",
                text="인사규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "기관 규정집",
                    "regulation_id": "shared-source-id",
                },
            ),
            _record(
                "doc-shared-binder",
                "chunk-pay",
                regulation_no="4-2",
                regulation_title="보수규정",
                article_no="제1조",
                article_title="목적",
                text="보수규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "기관 규정집",
                    "regulation_id": "shared-source-id",
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(2, summary["regulation_count"])
        self.assertEqual(
            {("4-1", "인사규정"), ("4-2", "보수규정")},
            {
                (item["regulation_no"], item["regulation_title"])
                for item in catalog
            },
        )
        self.assertEqual(2, len({item["regulation_unit_id"] for item in catalog}))

    def test_shared_stable_id_with_concurrent_distinct_identities_fails_closed(self) -> None:
        records = [
            _record(
                "doc-concurrent-personnel",
                "chunk-concurrent-personnel",
                regulation_no="4-1",
                regulation_title="인사규정",
                article_no="제1조",
                article_title="목적",
                text="인사규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "인사규정",
                    "regulation_id": "possibly-shared-source-id",
                },
            ),
            _record(
                "doc-concurrent-pay",
                "chunk-concurrent-pay",
                regulation_no="4-2",
                regulation_title="보수규정",
                article_no="제1조",
                article_title="목적",
                text="보수규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "보수규정",
                    "regulation_id": "possibly-shared-source-id",
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(2, summary["regulation_count"])
        self.assertEqual(2, len({item["regulation_unit_id"] for item in catalog}))

    def test_unambiguous_supersedes_chain_links_changed_or_missing_stable_ids(self) -> None:
        records = [
            _record(
                "doc-lineage-a",
                "chunk-lineage-a",
                regulation_no="1-1",
                regulation_title="구 인사규정",
                article_no="제1조",
                article_title="목적",
                text="최초 규정의 목적을 정한다.",
                revision_date="2022-01-01",
                metadata_updates={
                    "document_name": "구 인사규정",
                    "regulation_id": "",
                },
            ),
            _record(
                "doc-lineage-b",
                "chunk-lineage-b",
                regulation_no="2-5",
                regulation_title="인사관리규정",
                article_no="제1조",
                article_title="목적",
                text="중간 개정 규정의 목적을 정한다.",
                revision_date="2024-01-01",
                metadata_updates={
                    "document_name": "인사관리규정",
                    "regulation_id": "changed-family-id",
                    "supersedes_document_id": "doc-lineage-a",
                },
            ),
            _record(
                "doc-lineage-c",
                "chunk-lineage-c",
                regulation_no="9-3",
                regulation_title="인사운영규정",
                article_no="제1조",
                article_title="목적",
                text="최신 개정 규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "인사운영규정",
                    "regulation_id": "",
                    "supersedes_document_id": "doc-lineage-b",
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            history = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
                include_history=True,
            )

        self.assertEqual(1, summary["regulation_count"])
        self.assertEqual(3, summary["regulation_version_count"])
        self.assertEqual(3, len(history))
        self.assertEqual(1, len({item["regulation_unit_id"] for item in history}))
        self.assertEqual(
            {"구 인사규정", "인사관리규정", "인사운영규정"},
            {item["regulation_title"] for item in history},
        )

    def test_supersedes_document_link_is_ignored_when_predecessor_is_ambiguous(self) -> None:
        records = [
            _record(
                "doc-ambiguous-prior",
                "chunk-prior-personnel",
                regulation_no="4-1",
                regulation_title="인사규정",
                article_no="제1조",
                article_title="목적",
                text="인사규정의 목적을 정한다.",
                revision_date="2024-01-01",
                metadata_updates={
                    "document_name": "기관 규정집",
                    "regulation_id": "",
                },
            ),
            _record(
                "doc-ambiguous-prior",
                "chunk-prior-pay",
                regulation_no="4-2",
                regulation_title="보수규정",
                article_no="제1조",
                article_title="목적",
                text="보수규정의 목적을 정한다.",
                revision_date="2024-01-01",
                metadata_updates={
                    "document_name": "기관 규정집",
                    "regulation_id": "",
                },
            ),
            _record(
                "doc-ambiguous-successor",
                "chunk-successor",
                regulation_no="9-1",
                regulation_title="인사운영규정",
                article_no="제1조",
                article_title="목적",
                text="후속 규정의 목적을 정한다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "document_name": "인사운영규정",
                    "regulation_id": "",
                    "supersedes_document_id": "doc-ambiguous-prior",
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(3, summary["regulation_count"])
        self.assertEqual(3, len({item["regulation_unit_id"] for item in catalog}))

    def test_toc_preserves_chapter_ancestor_when_path_omits_regulation_root(self) -> None:
        record = _record(
            "doc-chapter",
            "chunk-chapter",
            regulation_no="4-1",
            regulation_title="인사규정",
            article_no="제1조",
            article_title="목적",
            text="제1조(목적) 이 규정은 인사 관리의 기준을 정한다.",
            revision_date="2026-07-01",
            hierarchy_path="제1장 총칙 > 제1조 목적",
        )
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="인사규정",
                regulation_no="4-1",
            )
            toc = regulation_toc(index_path, regulation_unit_id=unit_id)

        labels = [node["label"] for node in toc["nodes"]]
        self.assertEqual(["인사규정", "제1장 총칙", "제1조 목적"], labels)
        self.assertEqual(
            ["regulation", "chapter", "article"],
            [node["node_type"] for node in toc["nodes"]],
        )
        self.assertIsNone(toc["nodes"][0]["parent_id"])
        self.assertEqual(toc["nodes"][0]["node_id"], toc["nodes"][1]["parent_id"])
        self.assertEqual(toc["nodes"][1]["node_id"], toc["nodes"][2]["parent_id"])

    def test_canonical_title_unit_id_matches_numbered_combined_and_unnumbered_standalone(self) -> None:
        combined = _record(
            "doc-combined-unit",
            "combined-unit-article",
            regulation_no="4-1",
            regulation_title="인사규정",
            article_no="제1조",
            article_title="목적",
            text="제1조(목적) 인사 운영 기준을 정한다.",
            revision_date="2026-07-01",
            metadata_updates={
                "canonical_regulation_title": "인사규정",
                "canonical_regulation_no": "4-1",
                "canonical_hierarchy_path": "인사규정 > 제1조 목적",
            },
        )
        standalone = _record(
            "doc-standalone-unit",
            "standalone-unit-article",
            regulation_no="",
            regulation_title="인사규정",
            article_no="제1조",
            article_title="목적",
            text="제1조(목적) 인사 운영 기준을 정한다.",
            revision_date="2026-07-01",
            metadata_updates={
                "document_name": "인사규정",
                "canonical_regulation_title": "인사규정",
                "canonical_hierarchy_path": "인사규정 > 제1조 목적",
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            combined_path = Path(tmp) / "combined.sqlite3"
            standalone_path = Path(tmp) / "standalone.sqlite3"
            combined_summary = build_hierarchical_runtime_index(
                combined_path,
                [combined],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            standalone_summary = build_hierarchical_runtime_index(
                standalone_path,
                [standalone],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            combined_catalog = list_indexed_regulations(
                combined_path,
                profile_id="institution-a",
            )
            standalone_catalog = list_indexed_regulations(
                standalone_path,
                profile_id="institution-a",
            )

        self.assertEqual(1, len(combined_catalog))
        self.assertEqual(1, len(standalone_catalog))
        self.assertEqual(
            combined_catalog[0]["regulation_unit_id"],
            standalone_catalog[0]["regulation_unit_id"],
        )
        self.assertEqual("인사규정", combined_catalog[0]["regulation_title"])
        self.assertEqual("인사규정", standalone_catalog[0]["regulation_title"])
        self.assertEqual("", combined_catalog[0]["regulation_no"])
        self.assertEqual("", standalone_catalog[0]["regulation_no"])
        self.assertEqual(
            combined_summary["logical_corpus_sha256"],
            standalone_summary["logical_corpus_sha256"],
        )

    def test_legacy_combined_and_standalone_records_have_same_logical_toc_and_search_score(self) -> None:
        combined = _record(
            "doc-combined-parity",
            "combined-parity-article",
            regulation_no="1",
            regulation_title="인사규정",
            article_no="제1조",
            article_title="목적",
            text=(
                "[문서명] 기관 통합 규정집\n"
                "[위치] 기관 통합 규정집 > 제1편 일반규정 > 제1장 인사 > 1. 인사규정 > 제1장 총칙 > 제1조 목적\n"
                "[본문]\n제1조(목적) 인사 운영 기준을 정한다."
            ),
            revision_date="2026-07-01",
            hierarchy_path="기관 통합 규정집 > 제1편 일반규정 > 제1장 인사 > 1. 인사규정 > 제1장 총칙 > 제1조 목적",
            metadata_updates={
                "document_name": "기관 통합 규정집",
                "chunker_version": "0.1.8",
                "part_no": "제1편",
                "part_title": "기관 규정",
                "source_page_start": 137,
                "source_page_end": 137,
                "order_index": 910,
            },
        )
        standalone = _record(
            "doc-standalone-parity",
            "standalone-parity-article",
            regulation_no="1",
            regulation_title="인사규정",
            article_no="제1조",
            article_title="목적",
            text=(
                "[문서명] 인사규정\n"
                "[위치] 인사규정 > 제1장 총칙 > 제1조 목적\n"
                "[본문]\n제1조(목적) 인사 운영 기준을 정한다."
            ),
            revision_date="2026-07-01",
            hierarchy_path="인사규정 > 제1장 총칙 > 제1조 목적",
            metadata_updates={
                "document_name": "인사규정",
                "chunker_version": "0.1.8",
                "source_page_start": 1,
                "source_page_end": 1,
                "order_index": 1,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            combined_vector = root / "combined.jsonl"
            standalone_vector = root / "standalone.jsonl"
            combined_offsets = write_vector_records_with_offsets(combined_vector, [combined])
            standalone_offsets = write_vector_records_with_offsets(standalone_vector, [standalone])
            combined_index = root / "combined.sqlite3"
            standalone_index = root / "standalone.sqlite3"
            combined_summary = build_hierarchical_runtime_index(
                combined_index,
                [combined],
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=combined_offsets,
            )
            standalone_summary = build_hierarchical_runtime_index(
                standalone_index,
                [standalone],
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=standalone_offsets,
            )
            combined_unit_id = list_indexed_regulations(
                combined_index,
                profile_id="institution-a",
            )[0]["regulation_unit_id"]
            standalone_unit_id = list_indexed_regulations(
                standalone_index,
                profile_id="institution-a",
            )[0]["regulation_unit_id"]
            combined_toc = regulation_toc(
                combined_index,
                regulation_unit_id=combined_unit_id,
            )
            standalone_toc = regulation_toc(
                standalone_index,
                regulation_unit_id=standalone_unit_id,
            )
            combined_results, _ = search_hierarchical_records(
                combined_index,
                combined_vector,
                query="인사 운영 목적",
                top_k=1,
                profile_id="institution-a",
            )
            standalone_results, _ = search_hierarchical_records(
                standalone_index,
                standalone_vector,
                query="인사 운영 목적",
                top_k=1,
                profile_id="institution-a",
            )

        self.assertEqual(combined_unit_id, standalone_unit_id)
        self.assertEqual(
            combined_summary["logical_corpus_sha256"],
            standalone_summary["logical_corpus_sha256"],
        )
        combined_nodes = [
            {key: value for key, value in node.items() if key != "chunk_id"}
            for node in combined_toc["nodes"]
        ]
        standalone_nodes = [
            {key: value for key, value in node.items() if key != "chunk_id"}
            for node in standalone_toc["nodes"]
        ]
        self.assertEqual(combined_nodes, standalone_nodes)
        self.assertEqual(
            ["인사규정", "제1장 총칙", "제1조 목적"],
            [node["label"] for node in combined_nodes],
        )
        self.assertEqual(combined_results[0][0], standalone_results[0][0])

    def test_legacy_short_regulation_number_does_not_attach_outer_binder_segments(self) -> None:
        metadata = {
            "regulation_no": "1",
            "regulation_title": "인사규정",
            "article_no": "제1조",
            "article_title": "목적",
            "hierarchy_path": (
                "기관 통합 규정집 > 제1편 기본법령 > 제2장 인사 > "
                "1 인사규정 > 제1장 총칙 > 제1조 목적"
            ),
        }

        self.assertEqual(
            "인사규정 > 제1장 총칙 > 제1조 목적",
            hierarchical_index_module._canonical_hierarchy_path(metadata),
        )

    def test_canonical_same_title_siblings_remain_distinct_by_number(self) -> None:
        records = [
            _record(
                f"doc-canonical-sibling-{number}",
                f"chunk-canonical-sibling-{number}",
                regulation_no=number,
                regulation_title="운영규정",
                article_no="제1조",
                article_title="목적",
                text=f"{number} 운영규정 본문",
                revision_date="2026-07-01",
                metadata_updates={
                    "canonical_regulation_title": "운영규정",
                    "canonical_regulation_no": number,
                    "canonical_hierarchy_path": "운영규정 > 제1조 목적",
                },
            )
            for number in ("4-1", "7-2")
        ]

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "canonical-siblings.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(2, summary["regulation_count"])
        self.assertEqual(2, len({item["regulation_unit_id"] for item in catalog}))
        self.assertEqual({"4-1", "7-2"}, {item["regulation_no"] for item in catalog})

    def test_ambiguous_canonical_title_only_documents_fail_closed_as_distinct_units(self) -> None:
        records = [
            _record(
                f"doc-ambiguous-title-{index}",
                f"chunk-ambiguous-title-{index}",
                regulation_no="",
                regulation_title="운영규정",
                article_no="제1조",
                article_title="목적",
                text=f"서로 다른 것으로 취급해야 하는 운영규정 본문 {index}",
                revision_date="2026-07-01",
                metadata_updates={
                    "document_name": "운영규정",
                    "canonical_regulation_title": "운영규정",
                    "canonical_hierarchy_path": "운영규정 > 제1조 목적",
                    "regulation_id": "",
                },
            )
            for index in (1, 2)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "ambiguous-title-only.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )

        self.assertEqual(2, summary["regulation_count"])
        self.assertEqual(2, len({item["regulation_unit_id"] for item in catalog}))

    def test_catalog_keeps_same_title_distinct_by_number_and_hides_storage_ids(self) -> None:
        records = [
            _record(
                "doc-same-title-a",
                "chunk-same-title-a",
                regulation_no="4-1",
                regulation_title="운영규정",
                article_no="제1조",
                article_title="목적",
                text="첫 번째 운영규정의 목적을 정한다.",
                revision_date="2026-01-01",
            ),
            _record(
                "doc-same-title-b",
                "chunk-same-title-b",
                regulation_no="7-2",
                regulation_title="운영규정",
                article_no="제1조",
                article_title="목적",
                text="두 번째 운영규정의 목적을 정한다.",
                revision_date="2026-02-01",
            ),
        ]
        allowed_unit_ids = {
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title=record["metadata"]["regulation_title"],
                regulation_no=record["metadata"]["regulation_no"],
            )
            for record in records
        }

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            settings = Settings(data_dir=data_dir)
            auth = mcp_auth_context(tenant_id="tenant-a")
            with (
                patch.object(
                    regulation_tools,
                    "_require_unambiguous_profile_scope",
                    return_value="institution-a",
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, vector_path),
                ),
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value=allowed_unit_ids,
                ),
            ):
                catalog = list_regulations(
                    settings=settings,
                    auth=auth,
                    profile_id="institution-a",
                )
                toc = get_regulation_toc(
                    settings=settings,
                    auth=auth,
                    regulation_unit_id=catalog["regulations"][0]["regulation_unit_id"],
                    profile_id="institution-a",
                )

        self.assertEqual(2, summary["regulation_count"])
        self.assertEqual(2, catalog["total_count"])
        self.assertEqual({"4-1", "7-2"}, {item["regulation_no"] for item in catalog["regulations"]})
        self.assertEqual(2, len({item["regulation_unit_id"] for item in catalog["regulations"]}))
        self.assertTrue(all("document_id" not in item for item in catalog["regulations"]))
        self.assertNotIn("profile_id", catalog["metadata"])
        self.assertFalse(
            {"document_id", "profile_id", "version_id"}.intersection(toc["regulation"])
        )
        self.assertTrue(all("chunk_id" not in node for node in toc["nodes"]))

    def test_catalog_lists_140_unique_approved_regulations_with_hierarchy_pages(self) -> None:
        records = [
            _record(
                f"doc-{index:03d}",
                f"article-{index:03d}-1",
                regulation_no=f"4-{index:03d}",
                regulation_title=f"테스트규정 {index:03d}",
                article_no="제1조",
                article_title="목적",
                text=f"테스트규정 {index:03d}의 목적을 정한다.",
                revision_date="2026-07-01",
            )
            for index in range(1, 141)
        ]
        records.append(
            _record(
                "doc-001",
                "article-001-2",
                regulation_no="4-001",
                regulation_title="테스트규정 001",
                article_no="제2조",
                article_title="적용범위",
                text="테스트규정 001의 적용범위를 정한다.",
                revision_date="2026-07-01",
            )
        )
        records.append(
            _record(
                "doc-rejected",
                "article-rejected-1",
                regulation_no="9-999",
                regulation_title="승인되지 않은 규정",
                article_no="제1조",
                article_title="목적",
                text="승인되지 않은 규정은 목록에 노출되지 않아야 한다.",
                revision_date="2026-07-01",
                regulation_status="rejected",
            )
        )
        allowed_unit_ids = {
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title=record["metadata"]["regulation_title"],
                regulation_no=record["metadata"]["regulation_no"],
            )
            for record in records
        }

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            settings = Settings(data_dir=data_dir)
            auth = mcp_auth_context(tenant_id="tenant-a")
            verified_token = SimpleNamespace(is_current=lambda: True)
            with (
                patch.object(
                    regulation_tools,
                    "_require_unambiguous_profile_scope",
                    return_value="institution-a",
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, vector_path),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token",
                    return_value=verified_token,
                ),
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value=allowed_unit_ids,
                ),
            ):
                first_page = list_regulations(
                    settings=settings,
                    auth=auth,
                    profile_id="institution-a",
                    page=1,
                    page_size=100,
                )
                second_page = list_regulations(
                    settings=settings,
                    auth=auth,
                    profile_id="institution-a",
                    page=2,
                    page_size=100,
                )
                first_unit_id = first_page["regulations"][0]["regulation_unit_id"]
                toc = get_regulation_toc(
                    settings=settings,
                    auth=auth,
                    regulation_unit_id=first_unit_id,
                    profile_id="institution-a",
                )
                article = get_regulation_article(
                    settings=settings,
                    auth=auth,
                    regulation_unit_id=first_unit_id,
                    article_no="제1조",
                    profile_id="institution-a",
                    security_levels=["internal"],
                )

        self.assertEqual(141, summary["regulation_count"])
        self.assertEqual(140, first_page["total_count"])
        self.assertEqual(100, len(first_page["regulations"]))
        self.assertEqual("2", first_page["next_cursor"])
        self.assertEqual(40, len(second_page["regulations"]))
        self.assertIsNone(second_page["next_cursor"])
        self.assertEqual(
            140,
            len(
                {
                    item["regulation_title"]
                    for item in first_page["regulations"] + second_page["regulations"]
                }
            ),
        )
        self.assertTrue(all(item["status"] == "approved" for item in first_page["regulations"]))
        self.assertTrue(
            all(
                {
                    "regulation_title",
                    "regulation_category",
                    "revision_date",
                    "effective_from",
                    "status",
                }.issubset(item)
                for item in first_page["regulations"]
            )
        )
        self.assertTrue(toc["nodes"])
        self.assertEqual(1, len(article["articles"]))

    def test_as_of_uses_effective_date_not_inflated_revision_date(self) -> None:
        # A retroactive amendment is promulgated (revision_date) after it takes
        # effect (effective_from).  effective_from must stay the real effective
        # date, not be inflated up to the later revision date, or a point-in-time
        # query between the two dates wrongly finds the regulation not yet in
        # force.
        record = _record(
            "doc-a",
            "art-1",
            regulation_no="4-4-1",
            regulation_title="복무규정",
            article_no="제10조",
            article_title="육아휴직",
            text="육아휴직 기간은 3년 이내로 한다.",
            revision_date="2024-03-01",
        )
        record["metadata"]["effective_from"] = "2024-01-01"
        record["content_hash"] = stable_content_hash(record["text"], record["metadata"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, [record])
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="복무규정",
                regulation_no="4-4-1",
            )
            in_force = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=unit_id,
                article_no="제10조",
                as_of_date="2024-02-01",
            )

        self.assertEqual(1, len(in_force))

    def test_repealed_at_is_exclusive_for_approved_version(self) -> None:
        record = _record(
            "doc-approved-repealed",
            "approved-repealed-article",
            regulation_no="4-4-2",
            regulation_title="폐지 경계 규정",
            article_no="제10조",
            article_title="휴직",
            text="폐지경계 승인 본문",
            revision_date="2024-01-01",
            regulation_status="approved",
            metadata_updates={"repealed_at": "2025-06-15"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, [record])
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="폐지 경계 규정",
                regulation_no="4-4-2",
            )
            day_before = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=unit_id,
                article_no="제10조",
                as_of_date="2025-06-14",
            )
            repeal_day = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=unit_id,
                article_no="제10조",
                as_of_date="2025-06-15",
            )
            day_after = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=unit_id,
                article_no="제10조",
                as_of_date="2025-06-16",
            )
            before_search, _ = search_hierarchical_records(
                index_path,
                vector_path,
                query="폐지경계",
                top_k=5,
                profile_id="institution-a",
                as_of_date="2025-06-14",
            )
            repeal_day_search, _ = search_hierarchical_records(
                index_path,
                vector_path,
                query="폐지경계",
                top_k=5,
                profile_id="institution-a",
                as_of_date="2025-06-15",
            )
            day_after_search, _ = search_hierarchical_records(
                index_path,
                vector_path,
                query="폐지경계",
                top_k=5,
                profile_id="institution-a",
                as_of_date="2025-06-16",
            )
            current_catalog, current_count = page_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )
            history_catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
                include_history=True,
            )
            connection = sqlite3.connect(index_path)
            try:
                stored_repealed_at, stored_is_current = connection.execute(
                    "SELECT repealed_at, is_current FROM regulation_versions"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(
            ["approved-repealed-article"],
            [item["chunk_id"] for item in day_before],
        )
        self.assertEqual([], repeal_day)
        self.assertEqual([], day_after)
        self.assertEqual(
            ["approved-repealed-article"],
            [item["chunk_id"] for _score, item in before_search],
        )
        self.assertEqual([], repeal_day_search)
        self.assertEqual([], day_after_search)
        self.assertEqual(0, current_count)
        self.assertEqual([], current_catalog)
        self.assertEqual("approved", history_catalog[0]["status"])
        self.assertEqual("2025-06-15", history_catalog[0]["repealed_at"])
        self.assertFalse(history_catalog[0]["is_current"])
        self.assertEqual("2025-06-15", stored_repealed_at)
        self.assertEqual(0, stored_is_current)

    def test_legacy_index_without_repealed_at_column_remains_readable(self) -> None:
        record = _record(
            "doc-legacy-lifecycle",
            "legacy-lifecycle-article",
            regulation_no="4-4-3",
            regulation_title="구형 수명주기 규정",
            article_no="제1조",
            article_title="목적",
            text="구형호환 수명주기 본문",
            revision_date="2020-01-01",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, [record])
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                [record],
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            connection = sqlite3.connect(index_path)
            try:
                connection.execute(
                    "ALTER TABLE regulation_versions DROP COLUMN repealed_at"
                )
                connection.commit()
            finally:
                connection.close()
            unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="구형 수명주기 규정",
                regulation_no="4-4-3",
            )
            catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )
            page, total = page_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )
            toc = regulation_toc(
                index_path,
                regulation_unit_id=unit_id,
            )
            articles = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=unit_id,
                article_no="제1조",
            )
            results, trace = search_hierarchical_records(
                index_path,
                vector_path,
                query="구형호환",
                top_k=5,
                profile_id="institution-a",
            )

        self.assertEqual(1, len(catalog))
        self.assertEqual("", catalog[0]["repealed_at"])
        self.assertEqual(1, total)
        self.assertEqual("doc-legacy-lifecycle", page[0]["document_id"])
        self.assertIsNotNone(toc["regulation"])
        self.assertEqual(
            ["legacy-lifecycle-article"],
            [item["chunk_id"] for item in articles],
        )
        self.assertEqual(
            ["legacy-lifecycle-article"],
            [item["chunk_id"] for _score, item in results],
        )
        self.assertEqual(
            "",
            trace["candidate_regulations"][0]["repealed_at"],
        )

    def test_future_effective_revision_does_not_displace_current_and_history_paginates_versions(self) -> None:
        old = _record(
            "doc-old",
            "old-article",
            regulation_no="4-4-1",
            regulation_title="복무규정",
            article_no="제10조",
            article_title="휴직",
            text="구 규정 본문",
            revision_date="2024-01-01",
            metadata_updates={"effective_to": "2024-06-30"},
        )
        current = _record(
            "doc-current",
            "current-article",
            regulation_no="4-4-1",
            regulation_title="복무규정",
            article_no="제10조",
            article_title="휴직",
            text="현행 규정 본문",
            revision_date="2025-01-01",
        )
        future = _record(
            "doc-future",
            "future-article",
            regulation_no="4-4-1",
            regulation_title="복무규정",
            article_no="제10조",
            article_title="휴직",
            text="미래 시행 규정 본문",
            revision_date="2099-01-01",
        )
        records = [old, current, future]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            current_catalog, current_count = page_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )
            history_catalog, history_count = page_indexed_regulations(
                index_path,
                profile_id="institution-a",
                include_history=True,
                page_size=10,
            )
            unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="복무규정",
                regulation_no="4-4-1",
            )
            future_articles = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=unit_id,
                article_no="제10조",
                as_of_date="2099-02-01",
            )

        self.assertEqual(1, current_count)
        self.assertEqual("doc-current", current_catalog[0]["document_id"])
        self.assertEqual(3, history_count)
        history_by_document = {
            item["document_id"]: item
            for item in history_catalog
        }
        self.assertEqual("2024-06-30", history_by_document["doc-old"]["effective_to"])
        self.assertEqual("2098-12-31", history_by_document["doc-current"]["effective_to"])
        self.assertFalse(history_by_document["doc-future"]["is_current"])
        self.assertEqual(["doc-future"], [item["document_id"] for item in future_articles])

    def test_logical_corpus_fingerprint_is_stable_across_reupload_ids_and_input_order(self) -> None:
        first_records = [
            _record(
                "doc-first-old",
                "chunk-first-old",
                regulation_no="4-2-1",
                regulation_title="인사규정",
                article_no="제1조",
                article_title="목적",
                text="이 규정은 인사관리 기준을 정한다.",
                revision_date="2023-12-20",
            ),
            _record(
                "doc-first-new",
                "chunk-first-new",
                regulation_no="4-2-1",
                regulation_title="인사규정",
                article_no="제2조",
                article_title="적용범위",
                text="이 규정은 모든 직원에게 적용한다.",
                revision_date="2025-12-22",
            ),
        ]
        second_records = [
            _record(
                "doc-reupload-new",
                "chunk-reupload-new",
                regulation_no="4-2-1",
                regulation_title="인사규정",
                article_no="제2조",
                article_title="적용범위",
                text="이 규정은 모든 직원에게 적용한다.",
                revision_date="2025-12-22",
            ),
            _record(
                "doc-reupload-old",
                "chunk-reupload-old",
                regulation_no="4-2-1",
                regulation_title="인사규정",
                article_no="제1조",
                article_title="목적",
                text="이 규정은 인사관리 기준을 정한다.",
                revision_date="2023-12-20",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = build_hierarchical_runtime_index(
                root / "first.sqlite3",
                first_records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            second = build_hierarchical_runtime_index(
                root / "second.sqlite3",
                second_records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(first["logical_corpus_sha256"], second["logical_corpus_sha256"])
        self.assertEqual(first["regulation_count"], second["regulation_count"])
        self.assertEqual(first["regulation_version_count"], second["regulation_version_count"])
        self.assertEqual(first["toc_node_count"], second["toc_node_count"])

    def test_logical_corpus_fingerprint_orders_same_title_siblings_by_canonical_number(self) -> None:
        def sibling(document_id: str, chunk_id: str, number: str) -> dict:
            return _record(
                document_id,
                chunk_id,
                regulation_no=number,
                regulation_title="공통규정",
                article_no="제1조",
                article_title="목적",
                text="제1조(목적) 동일한 본문을 둔다.",
                revision_date="2026-07-01",
                metadata_updates={
                    "canonical_regulation_title": "공통규정",
                    "canonical_regulation_no": number,
                    "canonical_hierarchy_path": "공통규정 > 제1조 목적",
                },
            )

        first_records = [
            sibling("doc-a", "chunk-a", "4-1"),
            sibling("doc-z", "chunk-z", "4-2"),
        ]
        second_records = [
            sibling("doc-a", "chunk-reupload-a", "4-2"),
            sibling("doc-z", "chunk-reupload-z", "4-1"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = build_hierarchical_runtime_index(
                root / "first-siblings.sqlite3",
                first_records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            second = build_hierarchical_runtime_index(
                root / "second-siblings.sqlite3",
                second_records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )

        self.assertEqual(2, first["regulation_count"])
        self.assertEqual(
            first["logical_corpus_sha256"],
            second["logical_corpus_sha256"],
        )

    def test_institution_catalog_links_internal_regulation_revisions(self) -> None:
        records = [
            _record(
                "doc-2024",
                "old-article-1",
                regulation_no="4-4-1",
                regulation_title="복무규정",
                article_no="제10조",
                article_title="육아휴직",
                text="육아휴직 기간은 1년 이내로 한다.",
                revision_date="2024-01-01",
            ),
            _record(
                "doc-2026",
                "new-article-1",
                regulation_no="4-4-1",
                regulation_title="복무규정",
                article_no="제10조",
                article_title="육아휴직",
                text="육아휴직 기간은 3년 이내로 한다.",
                revision_date="2026-05-20",
            ),
            _record(
                "doc-2026",
                "pay-article-1",
                regulation_no="4-3-1",
                regulation_title="보수규정",
                article_no="제5조",
                article_title="보수 지급",
                text="보수는 매월 지급한다.",
                revision_date="2025-12-01",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            vector_progress: list[tuple[int, int]] = []
            offsets = write_vector_records_with_offsets(
                vector_path,
                records,
                progress_callback=lambda current, total: vector_progress.append((current, total)),
            )
            index_path = root / "regulation_hierarchy.sqlite3"
            hierarchy_progress: list[tuple[int, str, int, int]] = []
            built = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
                progress_callback=lambda percent, message, current, total: hierarchy_progress.append(
                    (percent, message, current, total)
                ),
            )

            summary = index_summary(index_path)
            current = list_indexed_regulations(index_path, profile_id="institution-a")
            history = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
                include_history=True,
            )
            filtered_catalog = list_indexed_regulations(
                index_path,
                profile_id="institution-a",
                query="복무규정",
            )
            scored, metadata = search_hierarchical_records(
                index_path,
                vector_path,
                query="육아휴직 기간",
                top_k=3,
                profile_id="institution-a",
            )

            leave_unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="복무규정",
                regulation_no="4-4-1",
            )
            toc = regulation_toc(index_path, regulation_unit_id=leave_unit_id)
            historical_articles = load_article_records(
                index_path,
                vector_path,
                regulation_unit_id=leave_unit_id,
                article_no="제10조",
                as_of_date="2024-06-01",
            )

        self.assertEqual(2, built["regulation_count"])
        self.assertEqual((len(records), len(records)), vector_progress[-1])
        self.assertEqual(100, hierarchy_progress[-1][0])
        self.assertEqual(sorted(item[0] for item in hierarchy_progress), [item[0] for item in hierarchy_progress])
        self.assertEqual(3, built["regulation_version_count"])
        self.assertEqual(2, summary["current_regulation_count"])
        self.assertEqual(2, len(current))
        self.assertEqual(3, len(history))
        self.assertEqual("복무규정", filtered_catalog[0]["regulation_title"])
        self.assertEqual("catalog_toc_body", metadata["retrieval_strategy"])
        self.assertTrue(scored)
        self.assertEqual(sorted((score for score, _record in scored), reverse=True), [score for score, _record in scored])
        self.assertEqual("doc-2026", scored[0][1]["document_id"])
        self.assertIn("3년", scored[0][1]["text"])
        self.assertEqual("복무규정", toc["regulation"]["regulation_title"])
        self.assertTrue(any(node["node_type"] == "article" for node in toc["nodes"]))
        self.assertEqual(1, len(historical_articles))
        self.assertEqual("doc-2024", historical_articles[0]["document_id"])

    @patch.object(regulation_tools, "_fully_visible_regulation_units")
    def test_mcp_uses_generated_hierarchy_for_search_catalog_toc_and_article(
        self,
        visible_units,
    ) -> None:
        records = [
            _record(
                "doc-current",
                "leave-article",
                regulation_no="4-4-1",
                regulation_title="복무규정",
                article_no="제10조",
                article_title="육아휴직",
                text="육아휴직 기간은 3년 이내로 한다.",
                revision_date="2026-05-20",
            ),
            _record(
                "doc-current",
                "pay-article",
                regulation_no="4-3-1",
                regulation_title="보수규정",
                article_no="제5조",
                article_title="보수 지급",
                text="보수는 매월 지급한다.",
                revision_date="2026-01-01",
            ),
        ]
        visible_units.return_value = {
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title=record["metadata"]["regulation_title"],
                regulation_no=record["metadata"]["regulation_no"],
            )
            for record in records
        }
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            hierarchy = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            (data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_data_bundle",
                        "tenant_id": "tenant-a",
                        "profile_id": "institution-a",
                        "files": {"hierarchical_index_sha256": hierarchy["sha256"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings = Settings(data_dir=data_dir)
            auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=auth,
                query="육아휴직 기간",
                profile_id="institution-a",
                security_levels=["internal"],
            )
            external_search = search_regulations(
                settings=settings,
                auth=auth,
                query="육아휴직 기간",
                profile_id="institution-a",
                security_levels=["internal"],
                metadata_profile="chatgpt-data",
            )
            catalog = list_regulations(
                settings=settings,
                auth=auth,
                profile_id="institution-a",
            )
            leave_unit_id = next(
                item["regulation_unit_id"]
                for item in catalog["regulations"]
                if item["regulation_title"] == "복무규정"
            )
            toc = get_regulation_toc(
                settings=settings,
                auth=auth,
                regulation_unit_id=leave_unit_id,
                profile_id="institution-a",
            )
            article = get_regulation_article(
                settings=settings,
                auth=auth,
                regulation_unit_id=leave_unit_id,
                article_no="제10조",
                profile_id="institution-a",
                security_levels=["internal"],
            )

        self.assertEqual("catalog_toc_body", search["metadata"]["retrieval_strategy"])
        self.assertEqual("leave-article", search["results"][0]["metadata"]["chunk_id"])
        self.assertTrue(external_search["metadata"]["candidate_regulations"])
        self.assertNotIn(
            "document_id",
            json.dumps(
                external_search["metadata"]["candidate_regulations"],
                ensure_ascii=False,
            ),
        )
        self.assertEqual(2, len(catalog["regulations"]))
        self.assertTrue(toc["nodes"])
        self.assertEqual(1, len(article["articles"]))
        self.assertIn("3년", article["articles"][0]["text"])


    def test_runtime_reference_graph_resolves_articles_and_reports_cycles_without_storage_ids(self) -> None:
        records = [
            _record(
                "doc-a",
                "a-article-1",
                regulation_no="1-1",
                regulation_title="규정 A",
                article_no="제1조",
                article_title="다른 규정의 적용",
                text="규정 B 제16조를 따른다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "internal_regulation_refs": ["규정 B"],
                    "regulation_article_refs": [
                        {"regulation_ref": "규정 B", "article_ref": "제16조"}
                    ],
                },
            ),
            _record(
                "doc-b",
                "b-article-16",
                regulation_no="1-2",
                regulation_title="규정 B",
                article_no="제16조",
                article_title="준용",
                text="규정 A 제1조를 따른다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "internal_regulation_refs": ["규정 A"],
                    "regulation_article_refs": [
                        {"regulation_ref": "규정 A", "article_ref": "제1조"}
                    ],
                },
            ),
        ]
        allowed_unit_ids = {
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title=record["metadata"]["regulation_title"],
                regulation_no=record["metadata"]["regulation_no"],
            )
            for record in records
        }

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )
            settings = Settings(data_dir=data_dir)
            auth = mcp_auth_context(tenant_id="tenant-a")
            with (
                patch.object(
                    regulation_tools,
                    "_require_unambiguous_profile_scope",
                    return_value="institution-a",
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, vector_path),
                ),
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value=allowed_unit_ids,
                ),
            ):
                catalog = list_regulations(
                    settings=settings,
                    auth=auth,
                    profile_id="institution-a",
                )
                units = {
                    item["regulation_title"]: item["regulation_unit_id"]
                    for item in catalog["regulations"]
                }
                references = get_regulation_references(
                    settings=settings,
                    auth=auth,
                    regulation_unit_id=units["규정 A"],
                    profile_id="institution-a",
                    direction="outgoing",
                    status="resolved",
                )
                cycles = list_regulation_reference_cycles(
                    settings=settings,
                    auth=auth,
                    profile_id="institution-a",
                )

        article_edges = [
            edge
            for edge in references["references"]
            if edge["reference_type"] == "regulation_article_reference"
        ]
        self.assertEqual(2, summary["reference_edge_count"])
        self.assertEqual(1, summary["reference_cycle_count"])
        self.assertEqual(1, len(article_edges))
        self.assertEqual("제16조", article_edges[0]["target_article"]["locator"])
        self.assertEqual("규정 B", article_edges[0]["target_regulation"]["regulation_title"])
        self.assertEqual(1, references["metadata"]["cycle_count_for_regulation"])
        self.assertEqual(1, cycles["total_count"])
        self.assertEqual(
            {"규정 A", "규정 B"},
            {
                item["regulation_title"]
                for item in cycles["cycles"][0]["regulations"]
            },
        )
        public_payload = json.dumps(
            {"references": references, "cycles": cycles},
            ensure_ascii=False,
        )
        self.assertNotIn('"document_id"', public_payload)
        self.assertNotIn('"profile_id"', public_payload)
        self.assertNotIn('"tenant_id"', public_payload)

    def test_later_unapproved_revision_does_not_hide_current_approved_catalog_or_references(self) -> None:
        records = [
            _record(
                "doc-approved",
                "approved-article",
                regulation_no="3-1",
                regulation_title="현행규정",
                article_no="제1조",
                article_title="준용",
                text="대상규정 제2조를 따른다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "internal_regulation_refs": ["대상규정"],
                    "regulation_article_refs": [
                        {"regulation_ref": "대상규정", "article_ref": "제2조"}
                    ],
                },
            ),
            _record(
                "doc-draft",
                "draft-article",
                regulation_no="3-1",
                regulation_title="현행규정",
                article_no="제1조",
                article_title="개정 초안",
                text="아직 승인되지 않은 개정 초안이다.",
                revision_date="2026-02-01",
                regulation_status="draft",
            ),
            _record(
                "doc-target",
                "target-article",
                regulation_no="3-2",
                regulation_title="대상규정",
                article_no="제2조",
                article_title="적용",
                text="적용 기준을 정한다.",
                revision_date="2026-01-01",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            summary = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            catalog, total_count = page_indexed_regulations(
                index_path,
                profile_id="institution-a",
            )
            current = next(
                item for item in catalog if item["regulation_title"] == "현행규정"
            )
            references = regulation_references(
                index_path,
                regulation_unit_id=current["regulation_unit_id"],
                direction="outgoing",
            )

        self.assertEqual(2, total_count)
        self.assertEqual("2026-01-01", current["revision_date"])
        self.assertEqual("approved", current["status"])
        self.assertEqual(2, summary["current_regulation_count"])
        self.assertEqual(1, references["total_count"])
        self.assertEqual(
            "대상규정",
            references["references"][0]["target_unit"]["title"],
        )

    def test_runtime_reference_requires_materialized_paragraph_and_item_locator(self) -> None:
        source = _record(
            "doc-source-depth",
            "source-depth",
            regulation_no="5-1",
            regulation_title="참조규정",
            article_no="제1조",
            article_title="준용",
            text="대상규정 제16조제2항제1호를 따른다.",
            revision_date="2026-01-01",
            metadata_updates={
                "regulation_article_refs": [
                    {
                        "regulation_ref": "대상규정",
                        "article_ref": "제16조제2항제1호",
                    }
                ]
            },
        )
        target_without_children = _record(
            "doc-target-depth",
            "target-depth",
            regulation_no="5-2",
            regulation_title="대상규정",
            article_no="제16조",
            article_title="절차",
            text="대상 조문의 본문이다.",
            revision_date="2026-01-01",
        )
        target_with_children = _record(
            "doc-target-depth",
            "target-depth",
            regulation_no="5-2",
            regulation_title="대상규정",
            article_no="제16조",
            article_title="절차",
            text="대상 조문의 본문이다.",
            revision_date="2026-01-01",
            metadata_updates={
                "paragraph_item_unit_sample": [
                    {"node_type": "paragraph", "number": "②"},
                    {"node_type": "item", "number": "1."},
                ],
                "paragraph_unit_count": 1,
                "item_unit_count": 1,
            },
        )
        source_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="참조규정",
            regulation_no="5-1",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unresolved_path = root / "unresolved.sqlite3"
            build_hierarchical_runtime_index(
                unresolved_path,
                [source, target_without_children],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            unresolved = regulation_references(
                unresolved_path,
                regulation_unit_id=source_unit_id,
                direction="outgoing",
            )

            resolved_path = root / "resolved.sqlite3"
            build_hierarchical_runtime_index(
                resolved_path,
                [source, target_with_children],
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            resolved = regulation_references(
                resolved_path,
                regulation_unit_id=source_unit_id,
                direction="outgoing",
            )

        self.assertEqual("unresolved", unresolved["references"][0]["status"])
        self.assertEqual(
            "target_article_not_found",
            unresolved["references"][0]["reason_codes"][0],
        )
        self.assertEqual("resolved", resolved["references"][0]["status"])
        self.assertEqual(
            "제16조제2항제1호",
            resolved["references"][0]["target_article"]["locator"],
        )

    def test_ambiguous_reference_is_visible_as_incoming_for_every_candidate(self) -> None:
        records = [
            _record(
                "doc-source-ambiguous",
                "source-ambiguous",
                regulation_no="6-1",
                regulation_title="참조규정",
                article_no="제1조",
                article_title="준용",
                text="운영규정 제1조를 따른다.",
                revision_date="2026-01-01",
                metadata_updates={
                    "regulation_article_refs": [
                        {"regulation_ref": "운영규정", "article_ref": "제1조"}
                    ]
                },
            ),
            _record(
                "doc-target-ambiguous-a",
                "target-ambiguous-a",
                regulation_no="6-2",
                regulation_title="운영규정",
                article_no="제1조",
                article_title="목적",
                text="첫 번째 운영규정이다.",
                revision_date="2026-01-01",
            ),
            _record(
                "doc-target-ambiguous-b",
                "target-ambiguous-b",
                regulation_no="6-3",
                regulation_title="운영규정",
                article_no="제1조",
                article_title="목적",
                text="두 번째 운영규정이다.",
                revision_date="2026-01-01",
            ),
        ]
        source_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="참조규정",
            regulation_no="6-1",
        )
        candidate_unit_ids = [
            regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="운영규정",
                regulation_no=regulation_no,
            )
            for regulation_no in ("6-2", "6-3")
        ]

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            outgoing = regulation_references(
                index_path,
                regulation_unit_id=source_unit_id,
                direction="outgoing",
            )
            incoming = [
                regulation_references(
                    index_path,
                    regulation_unit_id=unit_id,
                    direction="incoming",
                )
                for unit_id in candidate_unit_ids
            ]

        self.assertEqual(1, outgoing["total_count"])
        self.assertEqual("ambiguous", outgoing["references"][0]["status"])
        self.assertEqual(2, len(outgoing["references"][0]["candidate_units"]))
        for result in incoming:
            self.assertEqual(1, result["total_count"])
            self.assertEqual("incoming", result["references"][0]["relationship"])
            self.assertEqual("ambiguous", result["references"][0]["status"])

    def test_toc_preserves_paragraph_and_item_node_types(self) -> None:
        records = [
            _record(
                "doc-depth",
                "depth-article",
                regulation_no="2-1",
                regulation_title="계층규정",
                article_no="제16조",
                article_title="절차",
                text="제16조 본문",
                revision_date="2026-01-01",
            ),
            _record(
                "doc-depth",
                "depth-paragraph",
                regulation_no="2-1",
                regulation_title="계층규정",
                article_no="제16조",
                article_title="절차",
                text="제1항 본문",
                revision_date="2026-01-01",
                chunk_type="paragraph",
                hierarchy_path="통합 규정집 > 2-1 계층규정 > 제16조 절차 > 제1항",
                metadata_updates={"paragraph_no": "제1항"},
            ),
            _record(
                "doc-depth",
                "depth-item",
                regulation_no="2-1",
                regulation_title="계층규정",
                article_no="제16조",
                article_title="절차",
                text="제1호 본문",
                revision_date="2026-01-01",
                chunk_type="item",
                hierarchy_path="통합 규정집 > 2-1 계층규정 > 제16조 절차 > 제1항 > 제1호",
                metadata_updates={"paragraph_no": "제1항", "item_no": "제1호"},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
            )
            unit_id = regulation_unit_id_for(
                profile_id="institution-a",
                regulation_title="계층규정",
                regulation_no="2-1",
            )
            toc = regulation_toc(index_path, regulation_unit_id=unit_id)

        by_label = {node["label"]: node for node in toc["nodes"]}
        self.assertEqual("paragraph", by_label["제1항"]["node_type"])
        self.assertEqual("item", by_label["제1호"]["node_type"])
        self.assertGreater(by_label["제1호"]["depth"], by_label["제1항"]["depth"])


    def test_search_batch_reads_vector_candidates_with_one_open(self) -> None:
        records = [
            _record(
                "doc-batch",
                f"batch-chunk-{index}",
                regulation_no="7-1",
                regulation_title="Batch Read Regulation",
                article_no=f"Article {index}",
                article_title="Batch evidence",
                text=f"batchmarker approved evidence {index}",
                revision_date="2026-07-01",
            )
            for index in range(1, 5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            original_open = Path.open
            with patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=original_open,
            ) as open_mock:
                results, _ = search_hierarchical_records(
                    index_path,
                    vector_path,
                    query="batchmarker",
                    top_k=4,
                    profile_id="institution-a",
                )

            loaded = load_record_by_chunk(
                index_path,
                vector_path,
                document_id="doc-batch",
                chunk_id="batch-chunk-1",
            )

        self.assertEqual(1, open_mock.call_count)
        self.assertEqual(
            {record["chunk_id"] for record in records},
            {record["chunk_id"] for _score, record in results},
        )
        self.assertIsNotNone(loaded)
        self.assertEqual("batch-chunk-1", loaded["chunk_id"])

    def test_search_reranks_only_loaded_hierarchy_candidates_with_bm25(self) -> None:
        records = [
            _record(
                "doc-rerank",
                "candidate-a",
                regulation_no="7-2",
                regulation_title="Candidate Rerank Regulation",
                article_no="Article 1",
                article_title="Common evidence",
                text="common evidence first candidate",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-rerank",
                "candidate-b",
                regulation_no="7-2",
                regulation_title="Candidate Rerank Regulation",
                article_no="Article 2",
                article_title="Common evidence",
                text="common evidence target candidate",
                revision_date="2026-07-01",
            ),
            _record(
                "doc-denied",
                "candidate-denied",
                regulation_no="9-9",
                regulation_title="Denied Regulation",
                article_no="Article 1",
                article_title="Common evidence",
                text="common evidence denied candidate",
                revision_date="2026-07-01",
            ),
        ]

        class TargetIndex:
            def score_fast_query(
                self,
                _query: str,
                *,
                allowed_ids: set[str] | None = None,
            ) -> dict[str, float]:
                self.allowed_ids = allowed_ids
                return {
                    "doc-rerank:candidate-b": 100.0,
                    "doc-denied:candidate-denied": 1000.0,
                }

        rerank_index = TargetIndex()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            results, trace = search_hierarchical_records(
                index_path,
                vector_path,
                query="common evidence",
                top_k=1,
                profile_id="institution-a",
                allowed_unit_ids={
                    regulation_unit_id_for(
                        profile_id="institution-a",
                        regulation_title="Candidate Rerank Regulation",
                        regulation_no="7-2",
                    )
                },
                rerank_index=rerank_index,  # type: ignore[arg-type]
            )

        self.assertEqual("candidate-b", results[0][1]["chunk_id"])
        self.assertEqual("verified_bm25_fast_query", trace["candidate_reranker"])
        self.assertEqual(
            {"doc-rerank:candidate-a", "doc-rerank:candidate-b"},
            rerank_index.allowed_ids,
        )
        self.assertNotEqual("doc-denied", results[0][1]["document_id"])

    def test_article_locator_prefix_survives_distractors_without_acl_leak(self) -> None:
        allowed_title = "허용 채용 규정"
        allowed_records = [
            _record(
                "doc-allowed-prefix",
                f"distractor-{index:02d}",
                regulation_no="7-3",
                regulation_title=allowed_title,
                article_no=f"제{index + 20}조",
                article_title="적용 대상",
                text="적용 대상 일반 설명",
                revision_date="2026-07-01",
            )
            for index in range(61)
        ]
        allowed_records.append(
            _record(
                "doc-allowed-prefix",
                "target-prefix",
                regulation_no="7-3",
                regulation_title=allowed_title,
                article_no="제11조의3",
                article_title="제11조의3의 적용 대상",
                text="제11조의3의 적용 대상 특례",
                revision_date="2026-07-01",
                hierarchy_path="허용 채용 규정 > 제11조의3의 적용 대상",
            )
        )
        denied_record = _record(
            "doc-denied-prefix",
            "denied-prefix",
            regulation_no="9-9",
            regulation_title="비공개 채용 규정",
            article_no="제11조의3",
            article_title="제11조의3의 적용 대상",
            text="제11조의3의 적용 대상 비공개 정답",
            revision_date="2026-07-01",
            hierarchy_path="비공개 채용 규정 > 제11조의3의 적용 대상",
        )
        records = [*allowed_records, denied_record]
        allowed_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title=allowed_title,
            regulation_no="7-3",
        )
        denied_unit_id = regulation_unit_id_for(
            profile_id="institution-a",
            regulation_title="비공개 채용 규정",
            regulation_no="9-9",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            results, trace = search_hierarchical_records(
                index_path,
                vector_path,
                query="제11조의3 적용 대상",
                top_k=5,
                profile_id="institution-a",
                allowed_unit_ids={allowed_unit_id},
            )

        self.assertEqual("target-prefix", results[0][1]["chunk_id"])
        self.assertNotIn(
            "denied-prefix",
            {record["chunk_id"] for _score, record in results},
        )
        self.assertEqual(
            {allowed_unit_id},
            {
                item["regulation_unit_id"]
                for item in trace["candidate_regulations"]
            },
        )
        self.assertNotIn(
            denied_unit_id,
            {
                item["regulation_unit_id"]
                for item in trace["candidate_regulations"]
            },
        )

    def test_batch_read_drops_only_rows_with_invalid_vector_payloads(self) -> None:
        chunk_ids = (
            "valid",
            "negative-offset",
            "short-read",
            "invalid-utf8",
            "invalid-json",
        )
        records = [
            _record(
                "doc-mixed",
                chunk_id,
                regulation_no="7-2",
                regulation_title="Mixed Offset Regulation",
                article_no=f"Article {index}",
                article_title="Mixed evidence",
                text=f"mixedmarker approved evidence {index}",
                revision_date="2026-07-01",
            )
            for index, chunk_id in enumerate(chunk_ids, start=1)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            with vector_path.open("ab") as handle:
                invalid_utf8_offset = handle.tell()
                invalid_utf8_payload = b"\xff\n"
                handle.write(invalid_utf8_payload)
                invalid_json_offset = handle.tell()
                invalid_json_payload = b"{not-json}\n"
                handle.write(invalid_json_payload)
                end_offset = handle.tell()

            connection = sqlite3.connect(index_path)
            try:
                connection.execute(
                    "UPDATE chunks SET vector_offset=-1 "
                    "WHERE chunk_id='negative-offset'"
                )
                connection.execute(
                    "UPDATE chunks SET vector_offset=?, vector_length=? "
                    "WHERE chunk_id='short-read'",
                    (end_offset, 64),
                )
                connection.execute(
                    "UPDATE chunks SET vector_offset=?, vector_length=? "
                    "WHERE chunk_id='invalid-utf8'",
                    (invalid_utf8_offset, len(invalid_utf8_payload)),
                )
                connection.execute(
                    "UPDATE chunks SET vector_offset=?, vector_length=? "
                    "WHERE chunk_id='invalid-json'",
                    (invalid_json_offset, len(invalid_json_payload)),
                )
                connection.commit()
            finally:
                connection.close()

            results, _ = search_hierarchical_records(
                index_path,
                vector_path,
                query="mixedmarker",
                top_k=len(records),
                profile_id="institution-a",
            )

        self.assertEqual(
            ["valid"],
            [record["chunk_id"] for _score, record in results],
        )

    def test_batch_read_rejects_identity_and_content_hash_tampering(self) -> None:
        chunk_ids = ("valid", "identity-tampered", "content-tampered")
        records = [
            _record(
                "doc-tamper",
                chunk_id,
                regulation_no="7-3",
                regulation_title="Vector Identity Regulation",
                article_no=f"Article {index}",
                article_title="Identity evidence",
                text=f"tampermarker approved evidence {index}",
                revision_date="2026-07-01",
            )
            for index, chunk_id in enumerate(chunk_ids, start=1)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_path = root / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = root / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            identity_tampered = json.loads(json.dumps(records[1]))
            identity_tampered["chunk_id"] = "different-chunk-id"
            content_tampered = json.loads(json.dumps(records[2]))
            content_tampered["text"] = "tampermarker modified without rehashing"
            replacements: list[tuple[str, int, int]] = []
            with vector_path.open("ab") as handle:
                for chunk_id, record in (
                    ("identity-tampered", identity_tampered),
                    ("content-tampered", content_tampered),
                ):
                    payload = (
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    offset = handle.tell()
                    handle.write(payload)
                    replacements.append((chunk_id, offset, len(payload)))

            connection = sqlite3.connect(index_path)
            try:
                connection.executemany(
                    "UPDATE chunks SET vector_offset=?, vector_length=? "
                    "WHERE chunk_id=?",
                    [
                        (offset, length, chunk_id)
                        for chunk_id, offset, length in replacements
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            results, _ = search_hierarchical_records(
                index_path,
                vector_path,
                query="tampermarker",
                top_k=len(records),
                profile_id="institution-a",
            )

        self.assertEqual(
            ["valid"],
            [record["chunk_id"] for _score, record in results],
        )

    def test_verified_vector_record_cache_hits_only_with_complete_namespace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vector_path, row, namespace, record = _verified_cache_fixture(
                Path(tmp)
            )
            first = hierarchical_index._read_vector_records_at(
                vector_path,
                [row],
                verified_vector_cache_namespace=namespace,
            )
            with (
                patch.object(
                    Path,
                    "open",
                    side_effect=AssertionError(
                        "cache hit must not reopen JSONL"
                    ),
                ),
                patch.object(
                    hierarchical_index,
                    "stable_content_hash",
                    side_effect=AssertionError(
                        "cache hit must not recompute content hash"
                    ),
                ),
            ):
                second = hierarchical_index._read_vector_records_at(
                    vector_path,
                    [row],
                    verified_vector_cache_namespace=namespace,
                )
            with patch.object(Path, "open", side_effect=OSError("blocked")):
                without_namespace = hierarchical_index._read_vector_records_at(
                    vector_path,
                    [row],
                )
                other_tenant = hierarchical_index._read_vector_records_at(
                    vector_path,
                    [row],
                    verified_vector_cache_namespace=replace(
                        namespace,
                        tenant_id="tenant-b",
                    ),
                )
                other_profile = hierarchical_index._read_vector_records_at(
                    vector_path,
                    [row],
                    verified_vector_cache_namespace=replace(
                        namespace,
                        profile_id="institution-b",
                    ),
                )

        self.assertEqual([record], first)
        self.assertEqual([record], second)
        self.assertEqual([None], without_namespace)
        self.assertEqual([None], other_tenant)
        self.assertEqual([None], other_profile)

    def test_verified_vector_record_cache_freezes_cached_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vector_path, row, namespace, record = _verified_cache_fixture(
                Path(tmp)
            )
            loaded = hierarchical_index._read_vector_records_at(
                vector_path,
                [row],
                verified_vector_cache_namespace=namespace,
            )[0]

        self.assertEqual(record, loaded)
        assert loaded is not None
        self.assertEqual(
            json.dumps(record, ensure_ascii=False),
            json.dumps(loaded, ensure_ascii=False),
        )
        with self.assertRaises(TypeError):
            loaded["document_id"] = "doc-b"
        with self.assertRaises(TypeError):
            loaded["metadata"]["article_title"] = "Changed"

    def test_verified_vector_record_cache_does_not_refreeze_frozen_record(
        self,
    ) -> None:
        frozen = hierarchical_index._freeze_verified_vector_record(
            {"document_id": "doc-a", "metadata": {"tags": ["one"]}}
        )

        with patch.object(
            hierarchical_index,
            "_freeze_verified_vector_record",
            side_effect=AssertionError("already frozen record was frozen again"),
        ):
            hierarchical_index._verified_vector_record_cache_put(
                ("verified", "row"),
                frozen,
                encoded_size=32,
            )

        self.assertIs(
            frozen,
            hierarchical_index._verified_vector_record_cache_get(
                ("verified", "row")
            ),
        )

    def test_verified_vector_record_cache_key_binds_every_identity_dimension(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vector_path, row, namespace, _ = _verified_cache_fixture(Path(tmp))
            base_key = hierarchical_index._verified_vector_record_cache_key(
                vector_path,
                row,
                namespace,
            )
            self.assertIsNotNone(base_key)
            namespace_variants = (
                replace(namespace, vector_identity=("changed",)),
                replace(namespace, expected_index_sha256="c" * 64),
                replace(namespace, expected_vector_sha256="d" * 64),
                replace(namespace, tenant_id="tenant-b"),
                replace(namespace, profile_id="profile-b"),
            )
            row_variants = []
            for field, value in (
                ("vector_offset", int(row["vector_offset"]) + 1),
                ("vector_length", int(row["vector_length"]) + 1),
                ("document_id", "doc-b"),
                ("chunk_id", "chunk-b"),
                ("content_hash", "different-content-hash"),
            ):
                changed = dict(row)
                changed[field] = value
                row_variants.append(changed)
            keys = {
                hierarchical_index._verified_vector_record_cache_key(
                    vector_path,
                    row,
                    variant,
                )
                for variant in namespace_variants
            }
            keys.update(
                hierarchical_index._verified_vector_record_cache_key(
                    vector_path,
                    variant,
                    namespace,
                )
                for variant in row_variants
            )

            other_path = Path(tmp) / "other.jsonl"
            other_path.write_bytes(vector_path.read_bytes())
            other_namespace = replace(
                namespace,
                source_vector_path=os.path.normcase(
                    os.path.abspath(os.fspath(other_path))
                ),
                canonical_vector_path=os.path.normcase(
                    str(other_path.resolve(strict=True))
                ),
            )
            keys.add(
                hierarchical_index._verified_vector_record_cache_key(
                    other_path,
                    row,
                    other_namespace,
                )
            )

        self.assertNotIn(base_key, keys)
        self.assertEqual(11, len(keys))

    def test_corrupt_and_oversized_vector_records_are_never_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corrupt_path = root / "corrupt.jsonl"
            corrupt_payload = b"{not-json}\n"
            corrupt_path.write_bytes(corrupt_payload)
            corrupt_row = {
                "vector_offset": 0,
                "vector_length": len(corrupt_payload),
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "content_hash": "missing",
            }
            corrupt_namespace = _verified_cache_namespace(corrupt_path)
            for _ in range(2):
                self.assertEqual(
                    [None],
                    hierarchical_index._read_vector_records_at(
                        corrupt_path,
                        [corrupt_row],
                        verified_vector_cache_namespace=corrupt_namespace,
                    ),
                )
            self.assertEqual(0, len(hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE))

            vector_path, row, namespace, record = _verified_cache_fixture(root)
            with patch.object(
                hierarchical_index,
                "_VERIFIED_VECTOR_RECORD_CACHE_MAX_ENTRY_BYTES",
                8,
            ):
                for _ in range(2):
                    self.assertEqual(
                        [record],
                        hierarchical_index._read_vector_records_at(
                            vector_path,
                            [row],
                            verified_vector_cache_namespace=namespace,
                        ),
                    )
            self.assertEqual(0, len(hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE))

    def test_verified_vector_record_cache_concurrently_enforces_bounds(self) -> None:
        with (
            patch.object(
                hierarchical_index,
                "_VERIFIED_VECTOR_RECORD_CACHE_MAX_ENTRIES",
                32,
            ),
            patch.object(
                hierarchical_index,
                "_VERIFIED_VECTOR_RECORD_CACHE_MAX_BYTES",
                4096,
            ),
            patch.object(
                hierarchical_index,
                "_VERIFIED_VECTOR_RECORD_CACHE_MAX_ENTRY_BYTES",
                1024,
            ),
        ):
            def write_and_read(index: int) -> None:
                key = ("scope", index)
                hierarchical_index._verified_vector_record_cache_put(
                    key,
                    {"index": index},
                    encoded_size=256,
                )
                hierarchical_index._verified_vector_record_cache_get(key)

            with ThreadPoolExecutor(max_workers=16) as executor:
                list(executor.map(write_and_read, range(500)))

            with hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE_LOCK:
                entry_count = len(
                    hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE
                )
                byte_count = (
                    hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE_BYTES
                )

        self.assertLessEqual(entry_count, 32)
        self.assertLessEqual(byte_count, 4096)

    def test_verified_vector_record_cache_evicts_least_recently_used(self) -> None:
        with (
            patch.object(
                hierarchical_index,
                "_VERIFIED_VECTOR_RECORD_CACHE_MAX_ENTRIES",
                2,
            ),
            patch.object(
                hierarchical_index,
                "_VERIFIED_VECTOR_RECORD_CACHE_MAX_BYTES",
                1024,
            ),
        ):
            for key in ("a", "b"):
                hierarchical_index._verified_vector_record_cache_put(
                    (key,),
                    {"key": key},
                    encoded_size=100,
                )
            hierarchical_index._verified_vector_record_cache_get(("a",))
            hierarchical_index._verified_vector_record_cache_put(
                ("c",),
                {"key": "c"},
                encoded_size=100,
            )

            with hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE_LOCK:
                keys = list(
                    hierarchical_index._VERIFIED_VECTOR_RECORD_CACHE
                )

        self.assertEqual([("a",), ("c",)], keys)

    def test_verified_vector_record_cache_recursively_blocks_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vector_path, row, namespace, expected = _verified_cache_fixture(
                Path(tmp)
            )
            loaded = hierarchical_index._read_vector_records_at(
                vector_path,
                [row],
                verified_vector_cache_namespace=namespace,
            )[0]

        self.assertIsInstance(loaded, dict)
        assert loaded is not None
        metadata = loaded["metadata"]
        departments = metadata["department_acl"]
        nested = metadata["nested"]
        nested_items = nested["items"]
        self.assertIsInstance(metadata, dict)
        self.assertIsInstance(departments, list)
        self.assertIsInstance(nested, dict)
        self.assertIsInstance(nested_items, list)
        self.assertEqual(expected, loaded)
        self.assertEqual(
            json.dumps(expected, ensure_ascii=False, sort_keys=True),
            json.dumps(loaded, ensure_ascii=False, sort_keys=True),
        )

        dict_mutations = (
            lambda: operator.setitem(loaded, "text", "changed"),
            lambda: operator.delitem(loaded, "text"),
            loaded.clear,
            lambda: loaded.pop("text"),
            loaded.popitem,
            lambda: loaded.setdefault("new", "value"),
            lambda: loaded.update({"text": "changed"}),
            lambda: operator.ior(loaded, {"text": "changed"}),
            lambda: operator.setitem(metadata, "profile_id", "changed"),
            lambda: operator.setitem(nested, "new", True),
        )
        list_mutations = (
            lambda: operator.setitem(departments, 0, "changed"),
            lambda: operator.delitem(departments, 0),
            lambda: departments.append("changed"),
            departments.clear,
            lambda: departments.extend(["changed"]),
            lambda: departments.insert(0, "changed"),
            departments.pop,
            lambda: departments.remove("dept-a"),
            departments.reverse,
            departments.sort,
            lambda: operator.iadd(departments, ["changed"]),
            lambda: operator.imul(departments, 2),
            lambda: operator.setitem(nested_items, 0, 99),
        )
        for mutate in (*dict_mutations, *list_mutations):
            with self.assertRaisesRegex(TypeError, "immutable"):
                mutate()
        self.assertEqual(expected, loaded)

    def test_body_search_ranks_stronger_bm25_match_first(self) -> None:
        records = [
            _record(
                "doc-current",
                "leave-strong",
                regulation_no="4-4-1",
                regulation_title="복무규정",
                article_no="제10조",
                article_title="육아휴직",
                text="육아휴직 육아휴직 육아휴직 육아휴직 육아휴직 육아휴직 육아휴직 육아휴직",
                revision_date="2026-05-20",
            ),
            _record(
                "doc-current",
                "leave-weak",
                regulation_no="4-4-1",
                regulation_title="복무규정",
                article_no="제11조",
                article_title="기타 휴가",
                text="육아휴직 " + "그 밖의 사항은 따로 정한다. " * 40,
                revision_date="2026-05-20",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            vector_path = data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = data_dir / "hierarchy" / "regulation_hierarchy.sqlite3"
            build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="institution-a",
                vector_offsets=offsets,
            )

            results, _ = search_hierarchical_records(
                index_path,
                vector_path,
                query="육아휴직",
                top_k=2,
                profile_id="institution-a",
            )

        self.assertEqual(
            ["leave-strong", "leave-weak"],
            [record["chunk_id"] for _, record in results],
        )


def _verified_cache_namespace(
    vector_path: Path,
) -> VerifiedVectorCacheNamespace:
    return VerifiedVectorCacheNamespace(
        source_vector_path=os.path.normcase(
            os.path.abspath(os.fspath(vector_path))
        ),
        canonical_vector_path=os.path.normcase(
            str(vector_path.resolve(strict=True))
        ),
        vector_identity=(1, vector_path.stat().st_size, 2, 3),
        expected_index_sha256="a" * 64,
        expected_vector_sha256="b" * 64,
        tenant_id="tenant-a",
        profile_id="institution-a",
    )


def _verified_cache_fixture(
    root: Path,
) -> tuple[Path, dict[str, object], VerifiedVectorCacheNamespace, dict]:
    record = _record(
        "doc-a",
        "chunk-a",
        regulation_no="1-1",
        regulation_title="Cache Regulation",
        article_no="Article 1",
        article_title="Cache",
        text="verified vector cache evidence",
        revision_date="2026-07-01",
        metadata_updates={
            "department_acl": ["dept-a", "dept-b"],
            "nested": {"items": [1, 2]},
        },
    )
    vector_path = root / "approved_vectors.jsonl"
    offsets = write_vector_records_with_offsets(vector_path, [record])
    offset, length = offsets[("doc-a", "chunk-a")]
    row: dict[str, object] = {
        "vector_offset": offset,
        "vector_length": length,
        "document_id": "doc-a",
        "chunk_id": "chunk-a",
        "content_hash": record["content_hash"],
    }
    return vector_path, row, _verified_cache_namespace(vector_path), record


def _record(
    document_id: str,
    chunk_id: str,
    *,
    regulation_no: str,
    regulation_title: str,
    article_no: str,
    article_title: str,
    text: str,
    revision_date: str,
    regulation_status: str = "approved",
    chunk_type: str = "article",
    hierarchy_path: str | None = None,
    metadata_updates: dict | None = None,
) -> dict:
    metadata = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "tenant_id": "tenant-a",
        "profile_id": "institution-a",
        "institution_name": "테스트기관",
        "document_name": "통합 규정집",
        "regulation_id": "reg-binder",
        "regulation_version": f"rev-{revision_date.replace('-', '')}",
        "regulation_status": regulation_status,
        "regulation_no": regulation_no,
        "regulation_title": regulation_title,
        "revision_date": revision_date,
        "effective_from": revision_date,
        "chunk_type": chunk_type,
        "hierarchy_path": hierarchy_path
        or f"통합 규정집 > {regulation_no} {regulation_title} > {article_no} {article_title}",
        "article_no": article_no,
        "article_title": article_title,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "approved_content_hash": f"approved-{chunk_id}",
        "security_level": "internal",
        "department_acl": [],
    }
    metadata.update(metadata_updates or {})
    return {
        "schema_version": "reg-rag-vector-record-v1",
        "id": f"{document_id}:{chunk_id}",
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


if __name__ == "__main__":
    unittest.main()
