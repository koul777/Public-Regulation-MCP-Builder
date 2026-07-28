from __future__ import annotations

import tempfile
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.mcp_server import regulation_tools
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
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    fully_visible_regulation_unit_ids,
    index_summary,
    list_indexed_regulations,
    load_article_records,
    page_indexed_regulations,
    page_reference_cycles,
    regulation_references,
    regulation_toc,
    regulation_unit_id_for,
    search_hierarchical_records,
    write_vector_records_with_offsets,
)


class HierarchicalIndexTests(unittest.TestCase):
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
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, vector_path),
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
