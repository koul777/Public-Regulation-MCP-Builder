from __future__ import annotations

from collections.abc import Callable, Iterator
import csv
import io
import json
from pathlib import Path

from app.schemas.chunk import Chunk
from app.schemas.validation import ValidationIssue


CSV_COLUMNS = [
    "chunk_id",
    "document_id",
    "tenant_id",
    "document_name",
    "source_file",
    "source_page_start",
    "source_page_end",
    "chapter_no",
    "chapter_title",
    "section_no",
    "section_title",
    "regulation_no",
    "regulation_title",
    "article_no",
    "article_title",
    "paragraph_no",
    "paragraph_label",
    "item_no",
    "chunk_type",
    "hierarchy_path",
    "text",
    "normalized_text",
    "effective_date",
    "revision_date",
    "valid_from",
    "valid_to",
    "revision_history",
    "revision_history_spans",
    "article_effective_overrides",
    "article_validity_windows",
    "temporal_metadata_inherited",
    "temporal_metadata_scope",
    "temporal_metadata_inherited_fields",
    "temporal_metadata_normalized_fields",
    "temporal_metadata_source_chunk_ids",
    "temporal_metadata_conflict_fields",
    "is_supplementary_provision",
    "supplementary_label",
    "supplementary_identifier_date",
    "supplementary_paragraph_label",
    "supplementary_boilerplate",
    "confidence",
    "warnings",
    "approval_status",
    "approval_id",
    "approved_by",
    "approved_at",
    "approved_content_hash",
    "security_level",
    "department_acl",
]

TABLE_CSV_COLUMNS = [
    "document_id",
    "chunk_id",
    "table_id",
    "table_title",
    "appendix_no",
    "appendix_title",
    "citation_label",
    "row_kind",
    "row_index",
    "cell_count",
    "cells",
    "header_cells",
    "record_json",
    "raw",
    "source_page_start",
    "source_page_end",
    "chunk_type",
    "hierarchy_path",
    "regulation_no",
    "regulation_title",
    "article_no",
    "article_title",
    "table_source",
    "table_geometry_source",
    "primary_parser_table_source",
    "table_classification",
    "table_confidence",
    "table_review_reason",
    "review_required",
    "review_flags",
    "row_quality_flags",
    "merged_from_row_indices",
    "kordoc_table_parser_status",
    "kordoc_table_count",
    "kordoc_table_promoted",
    "kordoc_table_promotion_review_required",
    "kordoc_table_unmatched_source",
    "table_false_positive_stability",
    "table_header_hits",
    "table_numeric_rows",
    "table_delimiter_rows",
]


class Exporter:
    def to_jsonl(self, chunks: list[Chunk]) -> str:
        return "\n".join(json.dumps(self._flat_chunk(chunk), ensure_ascii=False) for chunk in chunks) + ("\n" if chunks else "")

    def to_csv(self, chunks: list[Chunk]) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for chunk in chunks:
            writer.writerow(self._chunk_csv_row(chunk))
        return buffer.getvalue()

    def to_markdown(self, chunks: list[Chunk]) -> str:
        if not chunks:
            return ""
        document_name = chunks[0].metadata.get("document_name") or chunks[0].metadata.get("source_file") or "Document"
        lines = [f"# {document_name}", ""]
        for chunk in chunks:
            article_no = chunk.metadata.get("article_no") or chunk.metadata.get("chunk_type")
            article_title = chunk.metadata.get("article_title")
            heading = " ".join(str(value) for value in [article_no, article_title] if value)
            lines.extend([f"## {heading}".strip(), "", chunk.text.strip(), "", "메타데이터:"])
            lines.append(f"- source_file: {chunk.metadata.get('source_file')}")
            page_start = chunk.source_page_start or chunk.metadata.get("source_page_start")
            page_end = chunk.source_page_end or chunk.metadata.get("source_page_end")
            if page_start and page_end and page_start != page_end:
                page = f"{page_start}-{page_end}"
            else:
                page = str(page_start or "")
            lines.append(f"- page: {page}")
            lines.append(f"- chunk_type: {chunk.chunk_type}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def to_tables_jsonl(self, chunks: list[Chunk]) -> str:
        rows = self.table_rows(chunks)
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else "")

    def to_tables_csv(self, chunks: list[Chunk]) -> str:
        rows = self.table_rows(chunks)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=TABLE_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(self._table_csv_row(row))
        return buffer.getvalue()

    def write_jsonl(
        self,
        path: Path,
        chunks: list[Chunk],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for index, chunk in enumerate(chunks, start=1):
                handle.write(json.dumps(self._flat_chunk(chunk), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                if progress_callback is not None and self._should_report_progress(index, len(chunks)):
                    progress_callback(index, len(chunks))

    def write_csv(
        self,
        path: Path,
        chunks: list[Chunk],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for index, chunk in enumerate(chunks, start=1):
                writer.writerow(self._chunk_csv_row(chunk))
                if progress_callback is not None and self._should_report_progress(index, len(chunks)):
                    progress_callback(index, len(chunks))

    def write_markdown(
        self,
        path: Path,
        chunks: list[Chunk],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            if chunks:
                document_name = (
                    chunks[0].metadata.get("document_name")
                    or chunks[0].metadata.get("source_file")
                    or "Document"
                )
                handle.write(f"# {document_name}\n\n")
            for index, chunk in enumerate(chunks, start=1):
                article_no = chunk.metadata.get("article_no") or chunk.metadata.get("chunk_type")
                article_title = chunk.metadata.get("article_title")
                heading = " ".join(str(value) for value in [article_no, article_title] if value)
                page_start = chunk.source_page_start or chunk.metadata.get("source_page_start")
                page_end = chunk.source_page_end or chunk.metadata.get("source_page_end")
                page = f"{page_start}-{page_end}" if page_start and page_end and page_start != page_end else str(page_start or "")
                handle.write(
                    f"## {heading}\n\n{chunk.text.strip()}\n\n메타데이터:\n"
                    f"- source_file: {chunk.metadata.get('source_file')}\n"
                    f"- page: {page}\n"
                    f"- chunk_type: {chunk.chunk_type}\n\n"
                )
                if progress_callback is not None and self._should_report_progress(index, len(chunks)):
                    progress_callback(index, len(chunks))

    def write_tables_jsonl(self, path: Path, chunks: list[Chunk]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in self.iter_table_rows(chunks):
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")

    def write_tables_csv(self, path: Path, chunks: list[Chunk]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TABLE_CSV_COLUMNS)
            writer.writeheader()
            for row in self.iter_table_rows(chunks):
                writer.writerow(self._table_csv_row(row))

    def table_rows(self, chunks: list[Chunk]) -> list[dict]:
        return list(self.iter_table_rows(chunks))

    def iter_table_rows(self, chunks: list[Chunk]) -> Iterator[dict]:
        for chunk in chunks:
            metadata = chunk.metadata
            cell_rows = metadata.get("table_cell_rows") or []
            if cell_rows:
                for table_row in cell_rows:
                    cells = table_row.get("cells") or []
                    yield {
                            "document_id": chunk.document_id,
                            "chunk_id": chunk.chunk_id,
                            "row_kind": "cell",
                            "row_index": table_row.get("row_index"),
                            "cell_count": len(cells),
                            "cells": cells,
                            "header_cells": metadata.get("table_header_cells") or [],
                            "record": self._table_record_for_row(metadata, table_row),
                            "raw": table_row.get("raw"),
                            "source_page_start": chunk.source_page_start,
                            "source_page_end": chunk.source_page_end,
                            "chunk_type": chunk.chunk_type,
                            "hierarchy_path": metadata.get("hierarchy_path"),
                            "regulation_no": metadata.get("regulation_no"),
                            "regulation_title": metadata.get("regulation_title"),
                            "article_no": metadata.get("article_no"),
                            "article_title": metadata.get("article_title"),
                            **self._table_context(chunk, metadata, table_row),
                            **self._table_evidence(metadata),
                        }
                continue
            if not metadata.get("table_like"):
                continue
            for row_index, table_row in enumerate(metadata.get("table_rows") or []):
                raw = self._raw_table_row_text(table_row)
                if not raw:
                    continue
                yield {
                        "document_id": chunk.document_id,
                        "chunk_id": chunk.chunk_id,
                        "row_kind": "raw",
                        "row_index": row_index,
                        "cell_count": 1,
                        "cells": [raw],
                        "header_cells": metadata.get("table_header_cells") or [],
                        "record": {},
                        "raw": raw,
                        "source_page_start": chunk.source_page_start,
                        "source_page_end": chunk.source_page_end,
                        "chunk_type": chunk.chunk_type,
                        "hierarchy_path": metadata.get("hierarchy_path"),
                        "regulation_no": metadata.get("regulation_no"),
                        "regulation_title": metadata.get("regulation_title"),
                        "article_no": metadata.get("article_no"),
                        "article_title": metadata.get("article_title"),
                        **self._table_context(chunk, metadata, table_row),
                        **self._table_evidence(metadata),
                    }

    def manifest(self, chunks: list[Chunk], issues: list[ValidationIssue]) -> dict:
        table_row_count = 0
        structured_table_row_count = 0
        raw_table_row_count = 0
        for row in self.iter_table_rows(chunks):
            table_row_count += 1
            if row.get("row_kind") == "cell":
                structured_table_row_count += 1
            elif row.get("row_kind") == "raw":
                raw_table_row_count += 1
        return {
            "chunk_count": len(chunks),
            "issue_count": len(issues),
            "table_row_count": table_row_count,
            "structured_table_row_count": structured_table_row_count,
            "raw_table_row_count": raw_table_row_count,
            "formats": ["jsonl", "csv", "markdown", "tables_jsonl", "tables_csv"],
            "has_errors": any(issue.severity == "error" for issue in issues),
        }

    def _chunk_csv_row(self, chunk: Chunk) -> dict:
        flat = self._flat_chunk(chunk)
        row = {column: flat.get(column) for column in CSV_COLUMNS}
        for field in (
            "warnings",
            "revision_history",
            "revision_history_spans",
            "article_effective_overrides",
            "article_validity_windows",
            "temporal_metadata_inherited_fields",
            "temporal_metadata_normalized_fields",
            "temporal_metadata_source_chunk_ids",
            "temporal_metadata_conflict_fields",
        ):
            row[field] = json.dumps(row.get(field) or [], ensure_ascii=False)
        return row

    def _table_csv_row(self, row: dict) -> dict:
        flat = dict(row)
        flat["cells"] = json.dumps(flat.get("cells") or [], ensure_ascii=False)
        flat["header_cells"] = json.dumps(flat.get("header_cells") or [], ensure_ascii=False)
        flat["record_json"] = json.dumps(flat.get("record") or {}, ensure_ascii=False)
        flat["review_flags"] = json.dumps(flat.get("review_flags") or [], ensure_ascii=False)
        flat["row_quality_flags"] = json.dumps(flat.get("row_quality_flags") or [], ensure_ascii=False)
        flat["merged_from_row_indices"] = json.dumps(flat.get("merged_from_row_indices") or [], ensure_ascii=False)
        return {column: flat.get(column) for column in TABLE_CSV_COLUMNS}

    def _should_report_progress(self, current: int, total: int) -> bool:
        interval = max(1, total // 100) if total else 1
        return current == 1 or current == total or current % interval == 0

    def _raw_table_row_text(self, table_row) -> str:
        if isinstance(table_row, str):
            return table_row.strip()
        if isinstance(table_row, dict):
            raw = table_row.get("raw")
            if raw:
                return str(raw).strip()
            cells = table_row.get("cells") or []
            return " ".join(str(cell) for cell in cells).strip()
        return str(table_row).strip()

    def _table_evidence(self, metadata: dict) -> dict:
        return {
            "table_source": metadata.get("table_source"),
            "table_geometry_source": metadata.get("table_geometry_source"),
            "primary_parser_table_source": metadata.get("primary_parser_table_source"),
            "table_classification": metadata.get("table_classification"),
            "table_confidence": metadata.get("table_confidence"),
            "table_review_reason": metadata.get("table_review_reason"),
            "kordoc_table_parser_status": metadata.get("kordoc_table_parser_status"),
            "kordoc_table_count": metadata.get("kordoc_table_count"),
            "kordoc_table_promoted": bool(metadata.get("kordoc_table_promoted")),
            "kordoc_table_promotion_review_required": bool(
                metadata.get("kordoc_table_promotion_review_required")
            ),
            "kordoc_table_unmatched_source": bool(metadata.get("kordoc_table_unmatched_source")),
            "table_false_positive_stability": metadata.get("table_false_positive_stability"),
            "table_header_hits": metadata.get("table_header_hits"),
            "table_numeric_rows": metadata.get("table_numeric_rows"),
            "table_delimiter_rows": metadata.get("table_delimiter_rows"),
        }

    def _table_context(self, chunk: Chunk, metadata: dict, table_row: dict | str) -> dict:
        row_mapping = table_row if isinstance(table_row, dict) else {}
        appendix_no, appendix_title = self._appendix_context(metadata)
        table_title = (
            metadata.get("table_title")
            or metadata.get("table_appendix_title")
            or appendix_title
            or metadata.get("article_title")
            or metadata.get("hierarchy_path")
        )
        citation_label = metadata.get("table_citation_label") or self._citation_label(
            metadata,
            appendix_no,
            appendix_title,
            table_title,
            chunk.source_page_start,
            chunk.source_page_end,
        )
        return {
            "table_id": metadata.get("table_id") or f"{chunk.chunk_id}_table",
            "table_title": table_title,
            "appendix_no": metadata.get("table_appendix_no") or appendix_no,
            "appendix_title": metadata.get("table_appendix_title") or appendix_title,
            "citation_label": citation_label,
            "review_required": bool(metadata.get("table_review_required") or row_mapping.get("review_required")),
            "review_flags": metadata.get("table_review_flags") or [],
            "row_quality_flags": row_mapping.get("row_quality_flags") or [],
            "merged_from_row_indices": row_mapping.get("merged_from_row_indices") or [],
        }

    def _appendix_context(self, metadata: dict) -> tuple[str | None, str | None]:
        appendix_no = metadata.get("table_appendix_no")
        appendix_title = metadata.get("table_appendix_title")
        if appendix_no or appendix_title:
            return appendix_no, appendix_title
        hierarchy_path = str(metadata.get("hierarchy_path") or "")
        parts = [part.strip() for part in hierarchy_path.split(">") if part.strip()]
        for part in reversed(parts):
            normalized = " ".join(part.split())
            if "별표" not in normalized and "별지" not in normalized:
                continue
            tokens = normalized.split()
            if not tokens:
                continue
            if tokens[0].startswith("별표") or tokens[0].startswith("별지"):
                appendix_no = tokens[0]
                if len(tokens) > 1 and tokens[1].replace("-", "").isdigit():
                    appendix_no = f"{appendix_no}{tokens[1]}"
                    appendix_title = " ".join(tokens[2:]) or None
                else:
                    appendix_title = " ".join(tokens[1:]) or None
                return appendix_no, appendix_title
        return None, None

    def _citation_label(
        self,
        metadata: dict,
        appendix_no: str | None,
        appendix_title: str | None,
        table_title: str | None,
        page_start: int | None,
        page_end: int | None,
    ) -> str:
        parts = []
        document_name = metadata.get("document_name") or metadata.get("regulation_title") or metadata.get("source_file")
        if document_name:
            parts.append(str(document_name))
        appendix_label = " ".join(str(value) for value in [appendix_no, appendix_title] if value)
        if appendix_label:
            parts.append(appendix_label)
        elif table_title:
            parts.append(str(table_title))
        if page_start:
            page_label = f"p.{page_start}" if not page_end or page_end == page_start else f"p.{page_start}-{page_end}"
            parts.append(page_label)
        return " / ".join(parts)

    def _table_record_for_row(self, metadata: dict, table_row: dict) -> dict:
        row_index = table_row.get("row_index")
        for record in metadata.get("table_records") or []:
            if record.get("row_index") == row_index:
                return record.get("record") or {}
        return table_row.get("record") or {}

    def _flat_chunk(self, chunk: Chunk) -> dict:
        metadata = dict(chunk.metadata)
        data = {
            **metadata,
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "source_node_ids": chunk.source_node_ids,
            "chunk_type": chunk.chunk_type,
            "text": chunk.text,
            "normalized_text": chunk.normalized_text,
            "retrieval_text": chunk.retrieval_text,
            "source_page_start": chunk.source_page_start,
            "source_page_end": chunk.source_page_end,
            "confidence": chunk.confidence,
            "warnings": chunk.warnings,
            "approval_status": chunk.approval_status,
            "approval_id": chunk.approval_id,
            "approved_by": chunk.approved_by,
            "approved_at": chunk.approved_at,
            "approved_content_hash": chunk.approved_content_hash,
            "security_level": chunk.security_level,
            "department_acl": chunk.department_acl,
        }
        data["retrieval_text"] = self._retrieval_text_with_table_markdown(
            data.get("retrieval_text"),
            data.get("table_markdown"),
        )
        data.setdefault("references", [])
        data.setdefault("effective_date", None)
        data.setdefault("revision_date", None)
        data.setdefault("valid_from", None)
        data.setdefault("valid_to", None)
        data.setdefault("revision_history", [])
        data.setdefault("revision_history_spans", [])
        data.setdefault("article_effective_overrides", [])
        data.setdefault("article_validity_windows", [])
        data.setdefault("temporal_metadata_inherited", False)
        data.setdefault("temporal_metadata_scope", None)
        data.setdefault("temporal_metadata_inherited_fields", [])
        data.setdefault("temporal_metadata_normalized_fields", [])
        data.setdefault("temporal_metadata_source_chunk_ids", [])
        data.setdefault("temporal_metadata_conflict_fields", [])
        data.setdefault("is_supplementary_provision", False)
        data.setdefault("supplementary_label", None)
        data.setdefault("supplementary_identifier_date", None)
        data.setdefault("supplementary_paragraph_label", None)
        data.setdefault("supplementary_boilerplate", False)
        return data

    def _retrieval_text_with_table_markdown(self, retrieval_text: str | None, table_markdown: str | None) -> str | None:
        if not retrieval_text or not table_markdown or not table_markdown.strip():
            return retrieval_text
        if "[표]" in retrieval_text:
            return retrieval_text
        return f"{retrieval_text.rstrip()}\n[표]\n{table_markdown.strip()}"
