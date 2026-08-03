from __future__ import annotations

from dataclasses import dataclass
import tempfile
from pathlib import Path
import unittest

from app.ingestion.vector_adapter import build_vector_records
from app.processors.chunker import Chunker
from app.processors.structure_detector import StructureDetector
from app.retrieval.bm25_index import Bm25Index
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    list_indexed_regulations,
    load_article_records,
    regulation_references,
    regulation_toc,
    search_hierarchical_records,
    write_vector_records_with_offsets,
)
from app.schemas.chunk import ChunkOptions
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


TENANT_ID = "tenant-packaging-parity"
PROFILE_ID = "profile-packaging-parity"
REVISION_DATE = "2026-07-01"


@dataclass(frozen=True)
class _RuntimeBundle:
    index_path: Path
    vector_path: Path
    bm25_index: Bm25Index
    logical_corpus_sha256: str


class InputPackagingParityTests(unittest.TestCase):
    def test_separate_files_and_combined_book_have_same_public_logical_results(self) -> None:
        separate_documents = _separate_documents()
        combined_document = _combined_document()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            separate = _build_runtime_bundle(root / "separate", separate_documents)
            combined = _build_runtime_bundle(root / "combined", [combined_document])

            separate_catalog = _catalog_projection(separate.index_path)
            combined_catalog = _catalog_projection(combined.index_path)
            self.assertEqual(separate_catalog, combined_catalog)
            self.assertEqual(
                separate.logical_corpus_sha256,
                combined.logical_corpus_sha256,
            )
            self.assertEqual({"보수규정", "인사규정"}, set(separate_catalog))

            for title in sorted(separate_catalog):
                separate_unit_id = separate_catalog[title]["regulation_unit_id"]
                combined_unit_id = combined_catalog[title]["regulation_unit_id"]
                self.assertEqual(separate_unit_id, combined_unit_id)

                separate_toc = regulation_toc(
                    separate.index_path,
                    regulation_unit_id=separate_unit_id,
                )
                combined_toc = regulation_toc(
                    combined.index_path,
                    regulation_unit_id=combined_unit_id,
                )
                self.assertEqual(
                    _public_toc_projection(separate_toc),
                    _public_toc_projection(combined_toc),
                )

                for article_no in ("제1조", "제2조"):
                    separate_articles = load_article_records(
                        separate.index_path,
                        separate.vector_path,
                        regulation_unit_id=separate_unit_id,
                        article_no=article_no,
                    )
                    combined_articles = load_article_records(
                        combined.index_path,
                        combined.vector_path,
                        regulation_unit_id=combined_unit_id,
                        article_no=article_no,
                    )
                    self.assertEqual(
                        _article_projection(separate_articles),
                        _article_projection(combined_articles),
                    )

                self.assertEqual(
                    _reference_projection(regulation_references(
                        separate.index_path,
                        regulation_unit_id=separate_unit_id,
                    )),
                    _reference_projection(regulation_references(
                        combined.index_path,
                        regulation_unit_id=combined_unit_id,
                    )),
                )

            for query in (
                "인사 운영 목적 기준",
                "보수 매월 25일 지급일",
                "직급 1급 월 보수 100",
                "인사 평가 A 90",
                "인사규정 2026년 7월 1일 시행",
                "정규직 적용 구분 기준",
                "보수 변경 신청서 홍길동 10",
                "규정 목적 기준",
            ):
                separate_results, _ = search_hierarchical_records(
                    separate.index_path,
                    separate.vector_path,
                    query=query,
                    top_k=4,
                    profile_id=PROFILE_ID,
                    rerank_index=separate.bm25_index,
                )
                combined_results, _ = search_hierarchical_records(
                    combined.index_path,
                    combined.vector_path,
                    query=query,
                    top_k=4,
                    profile_id=PROFILE_ID,
                    rerank_index=combined.bm25_index,
                )
                self.assertTrue(separate_results, query)
                self.assertEqual(
                    _search_projection(separate_results),
                    _search_projection(combined_results),
                    query,
                )

    def test_separate_files_and_title_only_book_without_contents_have_same_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            separate = _build_runtime_bundle(root / "separate", _separate_documents())
            combined = _build_runtime_bundle(
                root / "combined-without-contents",
                [_combined_document(include_contents=False)],
            )

            separate_catalog = _catalog_projection(separate.index_path)
            combined_catalog = _catalog_projection(combined.index_path)
            self.assertEqual(separate_catalog, combined_catalog)
            self.assertEqual(separate.logical_corpus_sha256, combined.logical_corpus_sha256)

            for title in sorted(separate_catalog):
                separate_unit_id = separate_catalog[title]["regulation_unit_id"]
                combined_unit_id = combined_catalog[title]["regulation_unit_id"]
                self.assertEqual(separate_unit_id, combined_unit_id)
                self.assertEqual(
                    _public_toc_projection(regulation_toc(
                        separate.index_path,
                        regulation_unit_id=separate_unit_id,
                    )),
                    _public_toc_projection(regulation_toc(
                        combined.index_path,
                        regulation_unit_id=combined_unit_id,
                    )),
                )
                self.assertEqual(
                    _reference_projection(regulation_references(
                        separate.index_path,
                        regulation_unit_id=separate_unit_id,
                    )),
                    _reference_projection(regulation_references(
                        combined.index_path,
                        regulation_unit_id=combined_unit_id,
                    )),
                )

            for query in (
                "보수규정 제2조 매월 25일",
                "정규직 적용 구분 기준",
                "보수 변경 신청서 홍길동 10",
            ):
                separate_results, _ = search_hierarchical_records(
                    separate.index_path,
                    separate.vector_path,
                    query=query,
                    top_k=4,
                    profile_id=PROFILE_ID,
                    rerank_index=separate.bm25_index,
                )
                combined_results, _ = search_hierarchical_records(
                    combined.index_path,
                    combined.vector_path,
                    query=query,
                    top_k=4,
                    profile_id=PROFILE_ID,
                    rerank_index=combined.bm25_index,
                )
                self.assertEqual(
                    _search_projection(separate_results),
                    _search_projection(combined_results),
                    query,
                )


def _separate_documents() -> list[ParsedDocument]:
    return [
        _standalone_document(
            document_id="doc-standalone-personnel",
            title="인사규정",
            source_file="인사규정.hwp",
            article_lines=(
                "제1조(목적) 인사 운영의 기준을 정한다.",
                "제2조(적용범위) 모든 직원에게 적용하며 보수는 보수규정 제2조에 따른다.",
            ),
            trailing_lines=(
                "[별표 1]",
                "인사 평가 기준",
                "등급 | 점수",
                "A | 90",
                "부칙 <2026.7.1.>",
                "제1조(시행일) 이 규정은 2026년 7월 1일부터 시행한다.",
            ),
            native_table_text="구분 | 기준\n정규직 | 적용",
        ),
        _standalone_document(
            document_id="doc-standalone-pay",
            title="보수규정",
            source_file="보수규정.pdf",
            article_lines=(
                "제1조(목적) 보수 지급의 기준을 정한다.",
                "제2조(지급일) 보수는 매월 25일 지급한다.",
            ),
            trailing_lines=(
                "【별지 제1호 서식】",
                "보수 변경 신청서",
                "성명 | 변경액",
                "홍길동 | 10",
            ),
            pdf_table_region=_pay_table_region(page_no=1),
        ),
    ]


def _standalone_document(
    *,
    document_id: str,
    title: str,
    source_file: str,
    article_lines: tuple[str, str],
    trailing_lines: tuple[str, ...] = (),
    native_table_text: str | None = None,
    pdf_table_region: dict | None = None,
) -> ParsedDocument:
    body = "\n".join(
        (
            title,
            "2026.7.1. 일부개정",
            "제1장 총칙",
            *article_lines,
            *trailing_lines,
        )
    )
    raw_text = "\n".join(value for value in (body, native_table_text) if value)
    return ParsedDocument(
        document_id=document_id,
        source_file=source_file,
        document_name=title,
        file_type=Path(source_file).suffix.lstrip(".") or "hwp",
        pages=[
            ParsedPage(
                page_no=1,
                blocks=[
                    ParsedBlock(text=body),
                    *(
                        [ParsedBlock(type="table", text=native_table_text)]
                        if native_table_text
                        else []
                    ),
                ],
            )
        ],
        raw_text=raw_text,
        metadata={
            "profile_id": PROFILE_ID,
            "institution_name": "포장 동등성 테스트기관",
            **(
                {"pdf_table_regions": [pdf_table_region]}
                if pdf_table_region is not None
                else {}
            ),
        },
    )


def _combined_document(*, include_contents: bool = True) -> ParsedDocument:
    contents = "\n".join(
        (
            "목차",
            "1-1-1. 인사규정 ........................ 10",
            "1-1-2. 보수규정 ........................ 20",
        )
    )
    personnel = "\n".join(
        (
            "제1편 일반규정",
            "제1장 인사 및 보수",
            "인사규정",
            "2026.7.1. 일부개정",
            "제1장 총칙",
            "제1조(목적) 인사 운영의 기준을 정한다.",
            "제2조(적용범위) 모든 직원에게 적용하며 보수는 보수규정 제2조에 따른다.",
            "[별표 1]",
            "인사 평가 기준",
            "등급 | 점수",
            "A | 90",
            "부칙 <2026.7.1.>",
            "제1조(시행일) 이 규정은 2026년 7월 1일부터 시행한다.",
        )
    )
    pay = "\n".join(
        (
            "보수규정",
            "2026.7.1. 일부개정",
            "제1장 총칙",
            "제1조(목적) 보수 지급의 기준을 정한다.",
            "제2조(지급일) 보수는 매월 25일 지급한다.",
            "【별지 제1호 서식】",
            "보수 변경 신청서",
            "성명 | 변경액",
            "홍길동 | 10",
        )
    )
    personnel_native_table = "구분 | 기준\n정규직 | 적용"
    raw_text = "\n".join(
        (
            *((contents,) if include_contents else ()),
            personnel,
            personnel_native_table,
            pay,
        )
    )
    pages = [
        ParsedPage(
            page_no=10,
            blocks=[
                ParsedBlock(text=personnel),
                ParsedBlock(type="table", text=personnel_native_table),
            ],
        ),
        ParsedPage(page_no=20, blocks=[ParsedBlock(text=pay)]),
    ]
    if include_contents:
        pages.insert(0, ParsedPage(page_no=1, blocks=[ParsedBlock(text=contents)]))
    return ParsedDocument(
        document_id="doc-combined-regulation-book",
        source_file="기관_통합_규정집.pdf",
        document_name="기관 통합 규정집",
        file_type="pdf",
        pages=pages,
        raw_text=raw_text,
        metadata={
            "profile_id": PROFILE_ID,
            "institution_name": "포장 동등성 테스트기관",
            "pdf_table_regions": [_pay_table_region(page_no=20)],
        },
    )


def _pay_table_region(*, page_no: int) -> dict:
    return {
        "source_page": page_no,
        "source_bbox": [40, 220, 500, 400],
        "title": "월 보수 지급기준",
        "text": "직급 | 월 보수\n1급 | 100",
        "column_count": 2,
        "row_count": 2,
    }


def _build_runtime_bundle(
    root: Path,
    documents: list[ParsedDocument],
) -> _RuntimeBundle:
    detector = StructureDetector()
    chunker = Chunker()
    approved_chunks: list[dict] = []
    for document in documents:
        nodes = detector.detect(document)
        chunks = chunker.build_chunks(
            nodes,
            document,
            ChunkOptions(include_context_header=False),
        )
        article_chunks = [chunk for chunk in chunks if chunk.chunk_type == "article"]
        regulation_count = len(
            [node for node in nodes if node.node_type == "regulation"]
        )
        expected_article_count = len(
            [node for node in nodes if node.node_type == "article"]
        )
        minimum_main_article_count = 2 * max(1, regulation_count)
        if expected_article_count < minimum_main_article_count:
            raise AssertionError(
                f"expected at least {minimum_main_article_count} main article nodes for "
                f"{document.document_id}, got {expected_article_count}"
            )
        if len(article_chunks) != expected_article_count:
            raise AssertionError(
                f"expected {expected_article_count} article chunks for "
                f"{document.document_id}, got "
                f"{[(chunk.chunk_type, chunk.chunk_id) for chunk in chunks]}"
            )
        for chunk in chunks:
            payload = chunk.model_dump(mode="json")
            payload.update(
                {
                    "tenant_id": TENANT_ID,
                    "approval_status": "approved",
                    "approval_id": f"approval-{chunk.chunk_id}",
                    "approved_content_hash": f"approved-content-{chunk.chunk_id}",
                    "security_level": "internal",
                }
            )
            payload["metadata"] = {
                **payload["metadata"],
                "tenant_id": TENANT_ID,
                "profile_id": PROFILE_ID,
                "institution_name": "포장 동등성 테스트기관",
                "revision_date": REVISION_DATE,
                "effective_from": REVISION_DATE,
                "regulation_status": "approved",
                "order_index": int(payload["metadata"].get("order_index") or 0),
            }
            approved_chunks.append(payload)

    records, summary = build_vector_records(approved_chunks)
    if summary["record_count"] != len(approved_chunks):
        raise AssertionError(summary)
    root.mkdir(parents=True, exist_ok=True)
    vector_path = root / "approved_vectors.jsonl"
    offsets = write_vector_records_with_offsets(vector_path, records)
    index_path = root / "regulation_hierarchy.sqlite3"
    hierarchy_summary = build_hierarchical_runtime_index(
        index_path,
        records,
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
        vector_offsets=offsets,
    )
    return _RuntimeBundle(
        index_path=index_path,
        vector_path=vector_path,
        bm25_index=Bm25Index.build(records),
        logical_corpus_sha256=str(hierarchy_summary["logical_corpus_sha256"]),
    )


def _catalog_projection(index_path: Path) -> dict[str, dict[str, str]]:
    return {
        item["regulation_title"]: {
            "regulation_unit_id": item["regulation_unit_id"],
            "regulation_title": item["regulation_title"],
            "regulation_no": item["regulation_no"],
        }
        for item in list_indexed_regulations(
            index_path,
            profile_id=PROFILE_ID,
        )
    }


def _public_toc_projection(payload: dict) -> dict:
    regulation = payload["regulation"] or {}
    return {
        "regulation": {
            "regulation_unit_id": regulation.get("regulation_unit_id"),
            "regulation_title": regulation.get("regulation_title"),
        },
        "nodes": [
            {
                "node_id": node["node_id"],
                "parent_id": node["parent_id"],
                "node_type": node["node_type"],
                "label": node["label"],
                "number": node["number"],
                "title": node["title"],
                "depth": node["depth"],
                "order_index": node["order_index"],
                "hierarchy_path": node["hierarchy_path"],
            }
            for node in payload["nodes"]
        ],
    }


def _article_projection(records: list[dict]) -> list[dict]:
    return [
        {
            "text": record["text"],
            "regulation_title": record["metadata"].get("regulation_title"),
            "article_no": record["metadata"].get("article_no"),
            "article_title": record["metadata"].get("article_title"),
            "hierarchy_path": record["metadata"].get("hierarchy_path"),
        }
        for record in records
    ]


def _search_projection(results: list[tuple[float, dict]]) -> list[dict]:
    return [
        {
            "score": score,
            "regulation_title": record["metadata"].get("regulation_title"),
            "article_no": record["metadata"].get("article_no"),
            "article_title": record["metadata"].get("article_title"),
            "hierarchy_path": record["metadata"].get("hierarchy_path"),
            "text": record["text"],
        }
        for score, record in results
    ]


def _reference_projection(payload: dict) -> dict:
    return {
        "regulation_unit_id": (payload.get("regulation") or {}).get("regulation_unit_id"),
        "references": [
            {
                "edge_id": item.get("edge_id"),
                "edge_type": item.get("edge_type"),
                "status": item.get("status"),
                "relationship": item.get("relationship"),
                "source_unit_id": (item.get("source_unit") or {}).get("unit_id"),
                "source_title": (item.get("source_unit") or {}).get("title"),
                "source_article": (item.get("source_article") or {}).get("locator"),
                "target_unit_id": (item.get("target_unit") or {}).get("unit_id"),
                "target_title": (item.get("target_unit") or {}).get("title"),
                "target_article": (item.get("target_article") or {}).get("locator"),
                "requested_article": (item.get("requested_article") or {}).get("locator"),
                "mention_count": item.get("mention_count"),
                "reason_codes": item.get("reason_codes"),
            }
            for item in payload.get("references") or []
        ],
        "cycles": payload.get("cycles") or [],
        "total_count": payload.get("total_count"),
    }


if __name__ == "__main__":
    unittest.main()
