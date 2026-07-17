from __future__ import annotations

import tempfile
import json
from pathlib import Path
import unittest

from app.ingestion.vector_adapter import stable_content_hash
from app.core.config import Settings
from app.mcp_server.regulation_tools import (
    get_regulation_article,
    get_regulation_toc,
    list_regulations,
    mcp_auth_context,
    search_regulations,
)
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    index_summary,
    list_indexed_regulations,
    load_article_records,
    regulation_toc,
    regulation_unit_id_for,
    search_hierarchical_records,
    write_vector_records_with_offsets,
)


class HierarchicalIndexTests(unittest.TestCase):
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

    def test_mcp_uses_generated_hierarchy_for_search_catalog_toc_and_article(self) -> None:
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
        self.assertEqual(2, len(catalog["regulations"]))
        self.assertTrue(toc["nodes"])
        self.assertEqual(1, len(article["articles"]))
        self.assertIn("3년", article["articles"][0]["text"])


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
        "regulation_status": "approved",
        "regulation_no": regulation_no,
        "regulation_title": regulation_title,
        "revision_date": revision_date,
        "effective_from": revision_date,
        "chunk_type": "article",
        "hierarchy_path": f"통합 규정집 > {regulation_no} {regulation_title} > {article_no} {article_title}",
        "article_no": article_no,
        "article_title": article_title,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "approved_content_hash": f"approved-{chunk_id}",
        "security_level": "internal",
        "department_acl": [],
    }
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
