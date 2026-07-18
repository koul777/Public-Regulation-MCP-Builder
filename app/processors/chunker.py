from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Callable

from app.parsers.base import PARSER_UNCERTAINTY_METADATA_FIELDS
from app.processors.answer_profile import append_answer_profile_to_retrieval_text, build_answer_profile
from app.processors.kordoc_table_matcher import (
    attach_kordoc_table_matches,
    best_kordoc_match,
    kordoc_match_summary,
    mergeable_kordoc_tables,
    prepare_kordoc_table_match_index,
)
from app.processors.metadata_extractor import MetadataExtractor
from app.processors.table_extractor import TableExtractor, disambiguate_table_headers
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.parsed import ParsedDocument
from app.schemas.structure import StructureNode


REFERENCE_SECTION_MARKER = "[\ucc38\uc870]"

HWPX_SOURCE_COUNT_METADATA_KEYS = (
    "hwpx_image_caption_count",
    "hwpx_table_row_count",
    "hwpx_table_cell_count",
    "hwpx_table_caption_count",
    "hwpx_nested_table_count",
    "hwpx_table_image_count",
    "hwpx_table_note_count",
    "hwpx_merged_cell_count",
)

HWPX_SOURCE_LIST_METADATA_KEYS = (
    "hwpx_table_direct_captions",
    "hwpx_table_image_captions",
    "hwpx_table_note_snippets",
    "hwpx_nested_table_text_snippets",
    "source_hwpx_xml_block_indices",
)

CHUNKER_VERSION = "0.1.8"
PDF_TABLE_REGION_DUPLICATE_COVERAGE = 0.8

SUPPLEMENTARY_CONTEXT_DATE = re.compile(
    r"(?P<date>\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?|\d{4}-\d{1,2}-\d{1,2})"
)
TABLE_CONTEXT_LABEL = re.compile(
    r"(?P<label>별표\s*\d*(?:\s*-\s*\d+)?|별지\s*제?\s*\d*(?:\s*-\s*\d+)?\s*호\s*서식|별지\s*제?\s*\d*(?:\s*-\s*\d+)?)"
)

TEMPORAL_SCALAR_FIELDS = (
    "effective_date",
    "revision_date",
    "valid_from",
    "valid_to",
)

INHERITABLE_TEMPORAL_SCALAR_FIELDS = (
    "effective_date",
    "revision_date",
    "valid_from",
)

TEMPORAL_LIST_FIELDS = (
    "revision_history",
    "revision_history_spans",
)

SCOPE_LEVEL_TEMPORAL_CHUNK_TYPES = {
    "document",
    "regulation",
    "supplementary",
    "supplementary_provision",
}

SUPPLEMENTARY_TEMPORAL_SOURCE_CHUNK_TYPES = {
    "supplementary",
    "supplementary_provision",
}

LOCAL_TEMPORAL_CHUNK_TYPES = {
    "article",
    "paragraph",
    "item",
    "subitem",
    "appendix",
    "form",
    "table",
}


class Chunker:
    def __init__(self, settings: Any | None = None) -> None:
        self.table_extractor = TableExtractor()
        self.metadata_extractor = MetadataExtractor()
        # Optional Settings. When absent, Kordoc-as-main promotion stays off and
        # Kordoc remains a review-only hint (backward compatible default).
        self.settings = settings

    def build_chunks(
        self,
        nodes: list[StructureNode],
        parsed: ParsedDocument,
        options: ChunkOptions | None = None,
        regulation_progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[Chunk]:
        options = options or ChunkOptions()
        lookup = {node.node_id: node for node in nodes}
        children_by_parent = self._children_by_parent(nodes)
        regulation_nodes = [node for node in nodes if node.node_type == "regulation"]
        chunks: list[Chunk] = []
        chunkable = [
            node
            for node in nodes
            if node.node_type in {"article", "appendix", "form", "supplementary", "table"}
            or (node.node_type in {"paragraph", "item", "subitem"} and not node.parent_id)
            or (node.node_type in {"paragraph", "item", "subitem"} and self._is_direct_child_of_type(node, lookup, {"supplementary"}))
            or self._container_has_orphan_body(node)
        ]

        regulation_nodes = sorted(regulation_nodes, key=lambda item: item.order_index)
        next_regulation_index = 0

        def report_regulations_through(order_index: int) -> None:
            nonlocal next_regulation_index
            if regulation_progress_callback is None or len(regulation_nodes) <= 1:
                return
            while (
                next_regulation_index < len(regulation_nodes)
                and regulation_nodes[next_regulation_index].order_index <= order_index
            ):
                current = regulation_nodes[next_regulation_index]
                next_regulation_index += 1
                label = str(current.title or current.number or f"규정 {next_regulation_index}").strip()
                regulation_progress_callback(next_regulation_index, len(regulation_nodes), label)

        for node in chunkable:
            report_regulations_through(node.order_index)
            related_nodes = self._related_nodes(node, children_by_parent)
            text = self._combined_text(related_nodes)
            working_node = node.model_copy(
                update={
                    "text": text,
                    "page_end": self._max_page_end(related_nodes),
                    "warnings": self._combined_warnings(related_nodes),
                    "confidence": min(item.confidence for item in related_nodes) if related_nodes else node.confidence,
                }
            )
            parts = self._split_node(working_node, options)
            for part_index, text in enumerate(parts, start=1):
                metadata = self._metadata_for(working_node, lookup, parsed, regulation_nodes)
                metadata["part_index"] = part_index
                metadata["part_count"] = len(parts)
                metadata.update(self._entity_trace_metadata(working_node, text))
                chunk_type = self._chunk_type(working_node)
                metadata.update(self._table_metadata(text, chunk_type))
                metadata.update(self._table_context_from_hierarchy(metadata))
                metadata.update(
                    self.metadata_extractor.extract(
                        text,
                        metadata.get("article_no"),
                        supplementary_context=self._is_supplementary_context(working_node, lookup),
                        current_regulation_no=metadata.get("regulation_no"),
                        current_regulation_title=metadata.get("regulation_title"),
                    )
                )
                metadata.update(self._supplementary_context_metadata(working_node, lookup, metadata))
                metadata.update(self._related_label_metadata(related_nodes, metadata))
                metadata.update(self._related_source_metadata(related_nodes))
                metadata.update(self._structural_child_unit_metadata(working_node, related_nodes, lookup))
                answer_profile = build_answer_profile(text, metadata)
                metadata.update(answer_profile)
                hierarchy_path = metadata.get("hierarchy_path", parsed.document_name or parsed.source_file)
                final_text = self._with_context_header(text, hierarchy_path, options)
                retrieval_text = self._retrieval_text(
                    parsed.document_name or parsed.source_file,
                    hierarchy_path,
                    text,
                    metadata.get("table_markdown"),
                )
                retrieval_text = append_answer_profile_to_retrieval_text(retrieval_text, answer_profile)
                chunk_id = self._chunk_id(parsed.document_id, working_node, part_index)
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        document_id=parsed.document_id,
                        source_node_ids=[item.node_id for item in related_nodes],
                        chunk_type=chunk_type,
                        text=text.strip(),
                        normalized_text=text.strip(),
                        retrieval_text=retrieval_text,
                        metadata=metadata,
                        source_page_start=working_node.page_start,
                        source_page_end=working_node.page_end,
                        confidence=working_node.confidence,
                        warnings=list(working_node.warnings),
                    ).model_copy(update={"text": final_text if options.include_context_header else text.strip()})
                )
        if regulation_progress_callback is not None and len(regulation_nodes) > 1:
            report_regulations_through(max((node.order_index for node in nodes), default=0) + 1)
        chunks.extend(self._pdf_table_region_chunks(parsed, len(chunks), chunks))
        if not chunks:
            chunks.extend(self._fallback_document_chunks(parsed, options))
        self._attach_pdf_document_metadata(chunks, parsed)
        self._attach_document_inventory_metadata(chunks, parsed)
        if getattr(self.settings, "kordoc_table_as_main", False):
            self._promote_kordoc_main_tables(chunks, parsed, options)
        else:
            attach_kordoc_table_matches(chunks, parsed.metadata.get("kordoc_table_inventory"))
        self._inherit_temporal_metadata_from_chunks(chunks)
        self._attach_reference_edges(chunks)
        return chunks

    _KORDOC_MATCH_RANK = {
        "weak_review_match": 1,
        "medium_review_match": 2,
        "strong_review_match": 3,
    }
    _KORDOC_UNMATCHED_TABLE_FILE_TYPES = {"hwp", "hwpx", "pdf", "docx"}
    # Primary-parser table fields that Kordoc supersedes when promoted to main.
    _PRIMARY_TABLE_FIELDS = (
        "table_markdown",
        "table_cell_rows",
        "table_cell_rows_raw",
        "table_rows",
        "table_header_cells",
        "table_column_count",
        "table_structured_row_count",
        "table_records",
        "table_record_count",
        "table_classification",
        "table_review_reason",
        "table_probable_false_positive",
        "table_probable_extraction_failed",
        "table_confidence",
        "table_header_hits",
        "table_numeric_rows",
        "table_delimiter_rows",
        "table_false_positive_stability",
    )

    def _promote_kordoc_main_tables(
        self, chunks: list[Chunk], parsed: ParsedDocument, options: ChunkOptions
    ) -> None:
        """Make a confidently matched Kordoc table the main content of a table
        chunk (structure + rendered body); demote the primary parser's table to
        a review hint. Approval/review gating is preserved — promotion never
        approves a chunk, it only changes what will be reviewed and served."""
        inventory = parsed.metadata.get("kordoc_table_inventory")
        if not isinstance(inventory, dict):
            attach_kordoc_table_matches(chunks, None)
            return
        inventory_metadata = self._kordoc_inventory_metadata(inventory)
        tables = mergeable_kordoc_tables(inventory)
        if not tables:
            # Nothing to promote; still record the review-only hints.
            attach_kordoc_table_matches(chunks, inventory)
            return
        tables = prepare_kordoc_table_match_index(tables)
        min_rank = self._KORDOC_MATCH_RANK.get(
            str(getattr(self.settings, "kordoc_table_promote_min_match", "medium_review_match")), 2
        )
        document_name = parsed.document_name or parsed.source_file
        claimed_table_keys: set[str] = set()
        for chunk in chunks:
            metadata = chunk.metadata or {}
            if not metadata.get("table_like") and chunk.chunk_type != "table":
                continue
            table, score, match_label = best_kordoc_match(chunk, tables)
            table_key = self._kordoc_table_key(table) if table else ""
            if table_key and table_key in claimed_table_keys:
                continue
            if not table or self._KORDOC_MATCH_RANK.get(match_label, 0) < min_rank:
                # Below promotion bar: keep a review-only hint on the primary
                # parser chunk, but still emit the Kordoc table as its own
                # draft table chunk below so Kordoc remains the table source.
                if table and match_label != "no_confident_match":
                    updated = dict(metadata)
                    updated["kordoc_table_match"] = kordoc_match_summary(
                        table, score=score, match_label=match_label
                    )
                    updated.update(inventory_metadata)
                    updated["kordoc_table_match_review_required"] = True
                    updated["kordoc_table_match_provisional"] = True
                    chunk.metadata = updated
                continue
            grid_rows = self._kordoc_grid_rows(table)
            markdown = self._kordoc_table_markdown(table, grid_rows)
            if not markdown or not grid_rows:
                continue
            claimed_table_keys.add(table_key)
            self._promote_chunk_to_kordoc_table(
                chunk,
                metadata,
                table,
                markdown,
                grid_rows,
                score,
                match_label,
                parsed,
                options,
                document_name,
                inventory_metadata,
            )
        chunks.extend(
            self._kordoc_unmatched_table_chunks(
                parsed=parsed,
                options=options,
                tables=tables,
                claimed_table_keys=claimed_table_keys,
                offset=len(chunks),
                inventory_metadata=inventory_metadata,
            )
        )

    @staticmethod
    def _kordoc_inventory_metadata(inventory: dict[str, Any]) -> dict[str, Any]:
        # The complete inventory can contain hundreds of table rows. Keep only
        # a compact summary on ordinary/table chunks; the full payload is
        # attached once by _attach_document_inventory_metadata so it is not
        # duplicated into every chunk's JSON record.
        return {
            "kordoc_table_inventory_summary": {
                "status": inventory.get("status"),
                "parser": inventory.get("parser"),
                "table_count": inventory.get("table_count", 0),
            },
            "kordoc_table_parser_status": inventory.get("status"),
            "kordoc_table_count": inventory.get("table_count", 0),
        }

    @staticmethod
    def _kordoc_table_key(table: dict, *, fallback: str = "") -> str:
        value = table.get("table_index")
        if value is None or str(value).strip() == "":
            value = table.get("id") or fallback
        return str(value)

    def _kordoc_unmatched_table_chunks(
        self,
        *,
        parsed: ParsedDocument,
        options: ChunkOptions,
        tables: list[dict[str, Any]],
        claimed_table_keys: set[str],
        offset: int,
        inventory_metadata: dict[str, Any],
    ) -> list[Chunk]:
        if str(parsed.file_type or "").lower() not in self._KORDOC_UNMATCHED_TABLE_FILE_TYPES:
            return []
        chunks: list[Chunk] = []
        document_name = parsed.document_name or parsed.source_file
        for position, table in enumerate(tables, start=1):
            key = self._kordoc_table_key(table, fallback=f"position_{position}")
            if key in claimed_table_keys:
                continue
            grid_rows = self._kordoc_grid_rows(table)
            markdown = self._kordoc_table_markdown(table, grid_rows)
            if not markdown or not grid_rows:
                continue
            source_page = self._kordoc_source_page(table)
            title = str(table.get("title") or f"Kordoc table {key}").strip()
            pseudo_node = StructureNode(
                node_id=f"{parsed.document_id}_kordoc_table_{key}",
                document_id=parsed.document_id,
                node_type="table",
                number=f"kordoc_{key}",
                title=title,
                text=markdown,
                page_start=source_page,
                page_end=source_page,
                order_index=offset + len(chunks),
                confidence=0.72,
                warnings=["kordoc_unmatched_table_review_required"],
                metadata={"kordoc_table_only": True},
            )
            metadata = self._metadata_for(pseudo_node, {pseudo_node.node_id: pseudo_node}, parsed, [])
            metadata.update(
                self._kordoc_table_main_metadata(
                    table=table,
                    markdown=markdown,
                    grid_rows=grid_rows,
                    score=100.0,
                    match_label="kordoc_only",
                )
            )
            metadata.update(inventory_metadata)
            metadata["table_review_reason"] = "kordoc_unmatched_main"
            metadata["kordoc_table_unmatched_source"] = True
            metadata["kordoc_table_promotion_review_required"] = True
            metadata.update(self._kordoc_source_page_metadata(source_page))
            answer_profile = build_answer_profile(markdown, metadata)
            metadata.update(answer_profile)
            hierarchy_path = metadata.get("hierarchy_path", document_name)
            retrieval_text = self._retrieval_text(document_name, hierarchy_path, markdown, markdown)
            retrieval_text = append_answer_profile_to_retrieval_text(retrieval_text, answer_profile)
            chunks.append(
                Chunk(
                    chunk_id=self._kordoc_table_chunk_id(parsed.document_id, key, source_page, len(chunks) + 1),
                    document_id=parsed.document_id,
                    source_node_ids=[],
                    chunk_type="table",
                    text=(
                        self._with_context_header(markdown, hierarchy_path, options)
                        if options.include_context_header
                        else markdown
                    ),
                    normalized_text=markdown,
                    retrieval_text=retrieval_text,
                    metadata=metadata,
                    source_page_start=source_page,
                    source_page_end=source_page,
                    confidence=pseudo_node.confidence,
                    warnings=list(pseudo_node.warnings),
                )
            )
        return chunks

    def _kordoc_table_main_metadata(
        self,
        *,
        table: dict,
        markdown: str,
        grid_rows: list,
        score: float,
        match_label: str,
    ) -> dict:
        cell_rows = self._kordoc_cell_rows(grid_rows)
        header_cells = cell_rows[0]["cells"] if cell_rows and table.get("grid_has_header") else []
        records = self._kordoc_table_records(cell_rows, header_cells)
        return {
            "table_like": True,
            "table_source": "kordoc",
            "table_geometry_source": "kordoc",
            "table_markdown": markdown,
            "table_cell_rows": cell_rows,
            "table_cell_rows_raw": [row["cells"] for row in cell_rows],
            "table_rows": [
                {"row_index": row["row_index"], "cells": row["cells"], "raw": row["raw"]}
                for row in cell_rows
            ],
            "table_header_cells": header_cells,
            "table_records": records,
            "table_column_count": table.get("grid_column_count") or table.get("column_count") or 0,
            "table_structured_row_count": len(cell_rows),
            "table_record_count": len(records),
            "table_classification": "structured_table",
            "table_review_reason": "kordoc_promoted_main",
            "table_review_required": True,
            "kordoc_table_match": kordoc_match_summary(table, score=score, match_label=match_label),
            "kordoc_table_promoted": True,
        }

    @staticmethod
    def _kordoc_source_page(table: dict) -> int | None:
        try:
            page = int(table.get("source_page"))
        except (TypeError, ValueError):
            return None
        return page if page > 0 else None

    @staticmethod
    def _kordoc_source_page_metadata(source_page: int | None) -> dict[str, Any]:
        if source_page is not None:
            return {"kordoc_source_page": source_page}
        return {
            "source_page_unavailable_reason": "kordoc_table_source_page_missing",
            "source_page_unavailable_parser": "kordoc",
        }

    @staticmethod
    def _kordoc_table_chunk_id(document_id: str, key: str, source_page: int | None, index: int) -> str:
        safe_key = re.sub(r"\W+", "_", key, flags=re.UNICODE).strip("_") or f"{index:04d}"
        page = f"p{source_page}" if source_page is not None else "p0"
        return f"{document_id}_table_kordoc_{safe_key}_{page}_{index:03d}"

    @staticmethod
    def _kordoc_cell_rows(grid_rows: list) -> list[dict]:
        return [
            {
                "row_index": index,
                "cells": [str(cell).strip() for cell in row.get("cells", [])],
                "raw": str(row.get("raw") or " ".join(str(cell).strip() for cell in row.get("cells", []))).strip(),
            }
            for index, row in enumerate(grid_rows)
            if isinstance(row, dict)
        ]

    @staticmethod
    def _kordoc_grid_rows(table: dict) -> list[dict]:
        rows = table.get("grid_rows") or table.get("cell_rows") or []
        normalized: list[dict] = []
        for row in rows:
            if isinstance(row, dict):
                cells = row.get("cells") or []
                raw = row.get("raw")
            elif isinstance(row, list):
                cells = row
                raw = None
            else:
                continue
            cells = [str(cell).strip() for cell in cells]
            normalized.append({"cells": cells, **({"raw": str(raw)} if raw is not None else {})})
        return normalized

    @staticmethod
    def _kordoc_table_markdown(table: dict, grid_rows: list[dict]) -> str:
        markdown = str(table.get("markdown") or "").strip()
        if markdown:
            return markdown
        rows = [[str(cell).strip() for cell in row.get("cells", [])] for row in grid_rows if isinstance(row, dict)]
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]

        def render_row(row: list[str]) -> str:
            cells = [cell.replace("\n", " ").replace("|", "\\|") for cell in row]
            return "| " + " | ".join(cells) + " |"

        lines = [render_row(padded[0])]
        if bool(table.get("grid_has_header") or table.get("has_header")):
            lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        lines.extend(render_row(row) for row in padded[1:])
        return "\n".join(lines)

    def _promote_chunk_to_kordoc_table(
        self,
        chunk: Chunk,
        metadata: dict,
        table: dict,
        markdown: str,
        grid_rows: list,
        score: float,
        match_label: str,
        parsed: ParsedDocument,
        options: ChunkOptions,
        document_name: str,
        inventory_metadata: dict[str, Any],
    ) -> None:
        updated = dict(metadata)
        updated.update(inventory_metadata)
        # Demote the primary parser's table into a hint snapshot for reviewers.
        hint = {key: updated[key] for key in self._PRIMARY_TABLE_FIELDS if key in updated}
        hint["table_text"] = chunk.text
        updated["primary_parser_table_hint"] = hint
        updated["primary_parser_table_source"] = self._primary_parser_table_source(parsed)
        for key in self._PRIMARY_TABLE_FIELDS:
            updated.pop(key, None)
        # Kordoc becomes the main table content.
        cell_rows = [
            {
                "row_index": index,
                "cells": [str(cell).strip() for cell in row.get("cells", [])],
                "raw": str(row.get("raw") or " ".join(str(cell).strip() for cell in row.get("cells", []))).strip(),
            }
            for index, row in enumerate(grid_rows)
            if isinstance(row, dict)
        ]
        updated["table_like"] = True
        updated["table_source"] = "kordoc"
        updated["table_geometry_source"] = "kordoc"
        updated["table_markdown"] = markdown
        updated["table_cell_rows"] = cell_rows
        updated["table_cell_rows_raw"] = [row["cells"] for row in cell_rows]
        updated["table_rows"] = [
            {"row_index": row["row_index"], "cells": row["cells"], "raw": row["raw"]}
            for row in cell_rows
        ]
        header_cells = cell_rows[0]["cells"] if cell_rows and table.get("grid_has_header") else []
        updated["table_header_cells"] = header_cells
        updated["table_records"] = self._kordoc_table_records(cell_rows, header_cells)
        updated["table_column_count"] = table.get("grid_column_count") or table.get("column_count") or 0
        updated["table_structured_row_count"] = len(cell_rows)
        updated["table_record_count"] = len(updated["table_records"])
        updated["table_classification"] = "structured_table"
        updated["table_review_reason"] = "kordoc_promoted_main"
        updated["table_review_required"] = True
        updated["kordoc_table_match"] = kordoc_match_summary(table, score=score, match_label=match_label)
        updated["kordoc_table_promoted"] = True
        updated["kordoc_table_promotion_review_required"] = True
        source_page = self._kordoc_source_page(table)
        if source_page is not None:
            updated.update(self._kordoc_source_page_metadata(source_page))
            if chunk.source_page_start is None:
                chunk.source_page_start = source_page
                chunk.source_page_end = source_page
        elif chunk.source_page_start is None:
            updated.update(self._kordoc_source_page_metadata(None))
        else:
            updated["kordoc_source_page_fallback"] = "primary_parser"
        # Rebuild the served body and retrieval text from the Kordoc table.
        hierarchy_path = updated.get("hierarchy_path", document_name)
        body = markdown
        answer_profile = build_answer_profile(body, updated)
        updated.update(answer_profile)
        retrieval_text = self._retrieval_text(document_name, hierarchy_path, body, markdown)
        retrieval_text = append_answer_profile_to_retrieval_text(retrieval_text, answer_profile)
        chunk.metadata = updated
        chunk.normalized_text = body
        chunk.retrieval_text = retrieval_text
        chunk.text = (
            self._with_context_header(body, hierarchy_path, options)
            if options.include_context_header
            else body
        )

    @staticmethod
    def _primary_parser_table_source(parsed: ParsedDocument) -> str:
        file_type = str(parsed.file_type or "").strip().lower().lstrip(".")
        if not file_type:
            file_type = Path(str(parsed.source_file or "")).suffix.lower().lstrip(".")
        if file_type in {"hwp", "hwpx", "pdf", "docx"}:
            return f"{file_type}_parser"
        return "primary_parser"

    @staticmethod
    def _kordoc_table_records(cell_rows: list[dict], header_cells: list[str]) -> list[dict]:
        if not header_cells:
            return []
        keys = disambiguate_table_headers(header_cells)
        records: list[dict] = []
        for row in cell_rows[1:]:
            cells = row.get("cells") or []
            record = {
                keys[index]: cells[index] if index < len(cells) else ""
                for index, header in enumerate(header_cells)
                if str(header).strip()
            }
            records.append({"row_index": row.get("row_index"), "record": record})
        return records

    def _table_metadata(self, text: str, chunk_type: str) -> dict:
        analysis = self.table_extractor.analyze_text(text, chunk_type)
        if not analysis.get("table_like") and chunk_type != "table":
            metadata = {
                "table_like": False,
                "table_confidence": analysis.get("table_confidence", 0.0),
            }
            if analysis.get("table_probable_false_positive"):
                metadata.update(
                    {
                        "table_classification": analysis.get("table_classification"),
                        "table_review_reason": analysis.get("table_review_reason"),
                        "table_probable_false_positive": True,
                        "table_probable_extraction_failed": False,
                        "table_false_positive_stability": analysis.get("table_false_positive_stability"),
                        "table_rows": analysis.get("table_rows", []),
                        "table_header_hits": analysis.get("table_header_hits", 0),
                        "table_numeric_rows": analysis.get("table_numeric_rows", 0),
                        "table_delimiter_rows": analysis.get("table_delimiter_rows", 0),
                    }
                )
            return metadata
        return analysis

    def _table_context_from_hierarchy(self, metadata: dict) -> dict:
        if not metadata.get("table_like"):
            return {}
        if metadata.get("table_citation_label") or metadata.get("table_appendix_no"):
            return {}
        hierarchy_path = str(metadata.get("hierarchy_path") or "")
        for segment in reversed([part.strip() for part in hierarchy_path.split(">") if part.strip()]):
            match = TABLE_CONTEXT_LABEL.search(segment)
            if not match:
                continue
            label = re.sub(r"\s+", "", match.group("label"))
            title = segment[match.end() :].strip(" -:\t")
            title = re.sub(r"\s*<[^>]*>?\s*$", "", title).strip()
            citation_label = f"{label} {title}".strip() if title else label
            updates: dict[str, Any] = {
                "table_appendix_no": label,
                "table_citation_label": citation_label,
                "table_label_inferred_from_hierarchy": True,
            }
            if title:
                updates["table_appendix_title"] = title
                updates["table_title"] = title
            return updates
        return {}

    def _related_nodes(
        self,
        node: StructureNode,
        children_by_parent: dict[str, list[StructureNode]],
    ) -> list[StructureNode]:
        if node.node_type not in {"article", "appendix", "form", "paragraph", "item", "subitem"}:
            return [node]
        related: list[StructureNode] = []
        stack = [node]
        while stack:
            current = stack.pop()
            related.append(current)
            children = children_by_parent.get(current.node_id, [])
            stack.extend(reversed(children))
        return sorted(related, key=lambda item: item.order_index)

    def _structural_child_unit_metadata(
        self,
        node: StructureNode,
        related_nodes: list[StructureNode],
        lookup: dict[str, StructureNode],
    ) -> dict[str, Any]:
        if node.node_type != "article":
            return {}
        excluded_containers = {"appendix", "form", "supplementary", "table"}
        if any(ancestor.node_type in excluded_containers for ancestor in self._ancestors(node, lookup)):
            return {}
        descendants = [item for item in related_nodes if item.node_id != node.node_id]
        paragraph_item_units = [
            item
            for item in descendants
            if item.node_type in {"paragraph", "item", "subitem"}
            and not self._has_intermediate_container(item, node, lookup, excluded_containers)
        ]
        paragraph_count = sum(1 for item in paragraph_item_units if item.node_type == "paragraph")
        item_count = sum(1 for item in paragraph_item_units if item.node_type == "item")
        subitem_count = sum(1 for item in paragraph_item_units if item.node_type == "subitem")
        total = paragraph_count + item_count + subitem_count
        if total <= 0:
            return {}
        return {
            "structural_child_count_source": "structure_detector",
            "paragraph_unit_count": paragraph_count,
            "item_unit_count": item_count,
            "subitem_unit_count": subitem_count,
            "paragraph_item_unit_count": total,
            "paragraph_item_traceable_unit_count": len(paragraph_item_units),
            "paragraph_item_unit_ids": [item.node_id for item in paragraph_item_units],
            "paragraph_item_unit_sample": [
                self._structural_child_unit_sample(item) for item in paragraph_item_units[:20]
            ],
        }

    def _has_intermediate_container(
        self,
        item: StructureNode,
        container: StructureNode,
        lookup: dict[str, StructureNode],
        excluded_types: set[str],
    ) -> bool:
        parent_id = item.parent_id
        seen: set[str] = set()
        while parent_id and parent_id != container.node_id and parent_id not in seen:
            seen.add(parent_id)
            parent = lookup.get(parent_id)
            if parent is None:
                break
            if parent.node_type in excluded_types:
                return True
            parent_id = parent.parent_id
        return False

    def _structural_child_unit_sample(self, node: StructureNode) -> dict[str, Any]:
        text = re.sub(r"\s+", " ", node.text or "").strip()
        return {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "number": node.number or "",
            "title": node.title or "",
            "page_start": node.page_start,
            "page_end": node.page_end,
            "text_preview": text[:160],
        }

    def _children_by_parent(self, nodes: list[StructureNode]) -> dict[str, list[StructureNode]]:
        children: dict[str, list[StructureNode]] = {}
        for node in nodes:
            if node.parent_id:
                children.setdefault(node.parent_id, []).append(node)
        for values in children.values():
            values.sort(key=lambda item: item.order_index)
        return children

    def _combined_text(self, nodes: list[StructureNode]) -> str:
        return "\n".join(node.text.strip() for node in nodes if node.text.strip())

    def _container_has_orphan_body(self, node: StructureNode) -> bool:
        if node.node_type not in {"part", "chapter", "section", "subsection", "regulation"}:
            return False
        lines = [line.strip() for line in node.text.splitlines() if line.strip()]
        return len(lines) > 1

    def _is_direct_child_of_type(
        self,
        node: StructureNode,
        lookup: dict[str, StructureNode],
        parent_types: set[str],
    ) -> bool:
        if not node.parent_id:
            return False
        parent = lookup.get(node.parent_id)
        return bool(parent and parent.node_type in parent_types)

    def _max_page_end(self, nodes: list[StructureNode]) -> int | None:
        page_values = [node.page_end for node in nodes if node.page_end is not None]
        return max(page_values) if page_values else None

    def _combined_warnings(self, nodes: list[StructureNode]) -> list[str]:
        warnings: list[str] = []
        for node in nodes:
            for warning in node.warnings:
                if warning not in warnings:
                    warnings.append(warning)
        return warnings

    def _inherit_temporal_metadata_from_chunks(self, chunks: list[Chunk]) -> None:
        groups: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            scope_key = self._temporal_scope_key(chunk.metadata)
            if scope_key:
                groups.setdefault(scope_key, []).append(chunk)
        for scope_key, scope_chunks in groups.items():
            context = self._temporal_context_from_scope(scope_chunks, scope_key)
            if not context["source_chunk_ids"]:
                continue
            for chunk in scope_chunks:
                inherited_fields = self._apply_temporal_context(chunk, context)
                conflict_fields = [
                    field
                    for field in context["conflict_fields"]
                    if not self._has_temporal_value(chunk.metadata.get(field))
                ]
                if conflict_fields:
                    chunk.metadata["temporal_metadata_conflict_fields"] = conflict_fields
                ambiguous_fields = [
                    field
                    for field in context["ambiguous_fields"]
                    if not self._has_temporal_value(chunk.metadata.get(field))
                ]
                if ambiguous_fields:
                    chunk.metadata["temporal_metadata_ambiguous_fields"] = ambiguous_fields
                    chunk.metadata["temporal_metadata_ambiguous_scope"] = context["scope_kind"]
                    chunk.metadata["temporal_metadata_ambiguous_source_chunk_ids"] = context["source_chunk_ids"][:20]
                if not inherited_fields:
                    continue
                chunk.metadata["temporal_metadata_inherited"] = True
                chunk.metadata["temporal_metadata_scope"] = context["scope_kind"]
                chunk.metadata["temporal_metadata_inherited_fields"] = inherited_fields
                chunk.metadata["temporal_metadata_source_chunk_ids"] = context["source_chunk_ids"][:20]

    def _temporal_context_from_scope(self, chunks: list[Chunk], scope_key: str) -> dict[str, Any]:
        scalar_values: dict[str, list[dict[str, Any]]] = {field: [] for field in INHERITABLE_TEMPORAL_SCALAR_FIELDS}
        source_chunk_ids: list[str] = []
        revision_history: list[Any] = []
        revision_history_spans: list[Any] = []
        article_effective_overrides: list[Any] = []
        article_validity_windows: list[Any] = []
        for chunk in chunks:
            metadata = chunk.metadata
            scope_level_source = self._is_scope_level_temporal_source(chunk)
            source_fields: list[str] = []
            for field in INHERITABLE_TEMPORAL_SCALAR_FIELDS:
                value = metadata.get(field)
                if scope_level_source and self._has_temporal_value(value):
                    scalar_values[field].append(
                        {
                            "value": value,
                            "source_kind": self._temporal_source_kind(chunk),
                        }
                    )
                    source_fields.append(field)
            for field, target in (("revision_history", revision_history), ("revision_history_spans", revision_history_spans)):
                value = metadata.get(field)
                if scope_level_source and isinstance(value, list) and value:
                    target.extend(value)
                    source_fields.append(field)
            overrides = self._scope_article_temporal_items(
                metadata.get("article_effective_overrides"),
                scope_level_source=scope_level_source,
            )
            if overrides:
                article_effective_overrides.extend(overrides)
                source_fields.append("article_effective_overrides")
            windows = self._scope_article_temporal_items(
                metadata.get("article_validity_windows"),
                scope_level_source=scope_level_source,
            )
            if windows:
                article_validity_windows.extend(windows)
                source_fields.append("article_validity_windows")
            if source_fields and chunk.chunk_id not in source_chunk_ids:
                source_chunk_ids.append(chunk.chunk_id)

        scalar_context: dict[str, Any] = {}
        conflict_fields: list[str] = []
        ambiguous_fields: list[str] = []
        for field, entries in scalar_values.items():
            values = [entry["value"] for entry in entries]
            unique = self._unique_temporal_values(values)
            if len(unique) == 1:
                scalar_context[field] = unique[0]
            elif len(unique) > 1:
                primary_values = self._unique_temporal_values(
                    [entry["value"] for entry in entries if entry.get("source_kind") != "supplementary"]
                )
                if len(primary_values) == 1:
                    scalar_context[field] = primary_values[0]
                    ambiguous_fields.append(field)
                elif len(primary_values) > 1:
                    conflict_fields.append(field)
                else:
                    ambiguous_fields.append(field)

        return {
            "scope_key": scope_key,
            "scope_kind": scope_key.split(":", 1)[0],
            "source_chunk_ids": source_chunk_ids,
            "scalars": scalar_context,
            "conflict_fields": conflict_fields,
            "ambiguous_fields": ambiguous_fields,
            "revision_history": self._dedupe_temporal_items(revision_history),
            "revision_history_spans": self._dedupe_temporal_items(revision_history_spans),
            "article_effective_overrides": self._dedupe_temporal_items(article_effective_overrides),
            "article_validity_windows": self._dedupe_temporal_items(article_validity_windows),
        }

    def _is_scope_level_temporal_source(self, chunk: Chunk) -> bool:
        metadata = chunk.metadata
        if metadata.get("is_supplementary_provision") or metadata.get("supplementary_label"):
            return True
        chunk_type = str(chunk.chunk_type or metadata.get("chunk_type") or "").strip()
        if chunk_type in SCOPE_LEVEL_TEMPORAL_CHUNK_TYPES:
            return True
        if chunk_type in LOCAL_TEMPORAL_CHUNK_TYPES:
            return False
        return not any(metadata.get(field) for field in ("article_no", "paragraph_no", "item_no"))

    def _temporal_source_kind(self, chunk: Chunk) -> str:
        metadata = chunk.metadata
        chunk_type = str(chunk.chunk_type or metadata.get("chunk_type") or "").strip()
        if (
            metadata.get("is_supplementary_provision")
            or metadata.get("supplementary_label")
            or chunk_type in SUPPLEMENTARY_TEMPORAL_SOURCE_CHUNK_TYPES
        ):
            return "supplementary"
        return "scope"

    def _scope_article_temporal_items(self, value: Any, *, scope_level_source: bool) -> list[Any]:
        if not isinstance(value, list):
            return []
        result: list[Any] = []
        for item in value:
            if not isinstance(item, dict):
                if scope_level_source:
                    result.append(item)
                continue
            article_ref = str(item.get("article_ref") or "").strip()
            if article_ref == "*" and not scope_level_source:
                continue
            result.append(item)
        return result

    def _apply_temporal_context(self, chunk: Chunk, context: dict[str, Any]) -> list[str]:
        metadata = chunk.metadata
        inherited_fields: list[str] = []
        normalized_fields: list[str] = []
        if self._has_temporal_value(metadata.get("effective_date")) and not self._has_temporal_value(metadata.get("valid_from")):
            metadata["valid_from"] = metadata["effective_date"]
            normalized_fields.append("valid_from")
        for field, value in context["scalars"].items():
            if self._has_temporal_value(metadata.get(field)):
                continue
            if field == "valid_from":
                article_valid_from = self._exact_article_valid_from(metadata, context["article_validity_windows"])
                if article_valid_from:
                    metadata[field] = article_valid_from
                    inherited_fields.append(field)
                    continue
            metadata[field] = value
            inherited_fields.append(field)
        for field in TEMPORAL_LIST_FIELDS:
            values = context[field]
            if values and not self._has_temporal_value(metadata.get(field)):
                metadata[field] = values
                inherited_fields.append(field)
        matching_overrides = self._matching_article_effective_overrides(
            metadata,
            context["article_effective_overrides"],
        )
        if matching_overrides and not self._has_temporal_value(metadata.get("article_effective_overrides")):
            metadata["article_effective_overrides"] = matching_overrides
            inherited_fields.append("article_effective_overrides")
        matching_windows = self._matching_article_validity_windows(
            metadata,
            context["article_validity_windows"],
        )
        if matching_windows and not self._has_temporal_value(metadata.get("article_validity_windows")):
            metadata["article_validity_windows"] = matching_windows
            inherited_fields.append("article_validity_windows")
            article_valid_from = self._exact_article_valid_from(metadata, matching_windows)
            if article_valid_from and not self._has_temporal_value(metadata.get("valid_from")):
                metadata["valid_from"] = article_valid_from
                if "valid_from" not in inherited_fields:
                    inherited_fields.append("valid_from")
        if self._has_temporal_value(metadata.get("effective_date")) and not self._has_temporal_value(metadata.get("valid_from")):
            metadata["valid_from"] = metadata["effective_date"]
            if "effective_date" in inherited_fields:
                inherited_fields.append("valid_from")
            else:
                normalized_fields.append("valid_from")
        if normalized_fields:
            metadata["temporal_metadata_normalized_fields"] = self._unique_temporal_values(normalized_fields)
        return inherited_fields

    def _temporal_scope_key(self, metadata: dict) -> str:
        regulation_key = self._primary_regulation_key(metadata)
        if regulation_key:
            return f"regulation:{regulation_key}"
        document_key = self._normalize_reference_key(
            metadata.get("document_id") or metadata.get("document_name") or metadata.get("source_file")
        )
        return f"document:{document_key}" if document_key else ""

    def _matching_article_effective_overrides(self, metadata: dict, overrides: list[Any]) -> list[Any]:
        if metadata.get("is_supplementary_provision"):
            return overrides
        article_key = self._normalize_reference_key(metadata.get("article_no"))
        if not article_key:
            return []
        return [
            item
            for item in overrides
            if isinstance(item, dict)
            and article_key == self._normalize_reference_key(item.get("article_ref"))
        ]

    def _matching_article_validity_windows(self, metadata: dict, windows: list[Any]) -> list[Any]:
        if metadata.get("is_supplementary_provision"):
            return windows
        article_key = self._normalize_reference_key(metadata.get("article_no"))
        result: list[Any] = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            article_ref = str(window.get("article_ref") or "").strip()
            if article_ref == "*":
                result.append(window)
                continue
            if article_key and article_key == self._normalize_reference_key(article_ref):
                result.append(window)
        return self._dedupe_temporal_items(result)

    def _exact_article_valid_from(self, metadata: dict, windows: list[Any]) -> str | None:
        article_key = self._normalize_reference_key(metadata.get("article_no"))
        if not article_key:
            return None
        for window in windows:
            if not isinstance(window, dict):
                continue
            if article_key != self._normalize_reference_key(window.get("article_ref")):
                continue
            valid_from = str(window.get("valid_from") or "").strip()
            if valid_from:
                return valid_from
        return None

    def _unique_temporal_values(self, values: list[Any]) -> list[Any]:
        seen: set[str] = set()
        result: list[Any] = []
        for value in values:
            key = self._temporal_value_key(value)
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    def _dedupe_temporal_items(self, values: list[Any]) -> list[Any]:
        return self._unique_temporal_values(values)

    def _temporal_value_key(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def _has_temporal_value(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return True

    def _split_node(self, node: StructureNode, options: ChunkOptions) -> list[str]:
        text = node.text.strip()
        if len(text) <= options.max_chunk_chars:
            return [text]

        split_patterns = [
            r"(?=^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚])",
            r"(?=^\(\d+\))",
            r"(?=^\d+\.\s+)",
            r"(?=^[가-힣][\.\)])",
        ]
        for pattern in split_patterns:
            parts = [part.strip() for part in re.split(pattern, text, flags=re.MULTILINE) if part.strip()]
            if len(parts) > 1 and max(len(part) for part in parts) <= options.max_chunk_chars:
                return parts

        return self._sentence_windows(text, options.max_chunk_chars, self._overlap_chars_for(node, options))

    def _overlap_chars_for(self, node: StructureNode, options: ChunkOptions) -> int:
        if node.node_type in {"paragraph", "part", "chapter", "section", "subsection", "regulation"}:
            return 0
        return options.overlap_chars

    def _sentence_windows(self, text: str, max_chars: int, overlap_chars: int) -> list[str]:
        sentences = [part.strip() for part in re.split(r"(?<=[.!?\u3002])\s+", text) if part.strip()]
        if not sentences:
            return self._hard_windows(text, max_chars, overlap_chars)
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(sentence) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(self._hard_windows(sentence, max_chars, overlap_chars))
                continue
            candidate = f"{current} {sentence}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                overlap = current[-overlap_chars:].strip() if overlap_chars else ""
                current = f"{overlap} {sentence}".strip() if overlap else sentence
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    def _hard_windows(self, text: str, max_chars: int, overlap_chars: int) -> list[str]:
        if max_chars <= 0:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + max_chars)
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
                if boundary > start + max_chars // 2:
                    end = boundary
            chunks.append(text[start:end].strip())
            if end >= len(text):
                break
            if overlap_chars <= 0:
                start = end
            else:
                next_start = max(start + 1, end - overlap_chars)
                start = next_start if next_start < end else end
        return [chunk for chunk in chunks if chunk]

    def _metadata_for(
        self,
        node: StructureNode,
        lookup: dict[str, StructureNode],
        parsed: ParsedDocument,
        regulation_nodes: list[StructureNode] | None = None,
    ) -> dict:
        ancestors = self._ancestors(node, lookup)
        metadata = {
            "document_id": parsed.document_id,
            "document_name": parsed.document_name or parsed.source_file,
            "source_file": parsed.source_file,
            "source_page_start": node.page_start,
            "source_page_end": node.page_end,
            "chunk_type": self._chunk_type(node),
            "hierarchy_path": self._hierarchy_path(parsed, ancestors, node),
            "order_index": node.order_index,
            "references": [],
            "effective_date": None,
            "revision_date": None,
            "revision_events": [],
            "revision_history": [],
            "revision_history_spans": [],
            "valid_from": None,
            "valid_to": None,
            "article_effective_overrides": [],
            "article_validity_windows": [],
            "is_supplementary_provision": False,
            "supplementary_label": None,
            "supplementary_identifier_date": None,
            "article_refs": [],
            "appendix_refs": [],
            "form_refs": [],
            "internal_regulation_refs": [],
            "external_law_refs": [],
            "reference_edges": [],
            "resolved_reference_count": 0,
            "unresolved_reference_count": 0,
            "parser_version": "0.1.0",
            "chunker_version": CHUNKER_VERSION,
            "agent_reviewed": False,
            "review_status": "not_requested",
            "warnings": list(node.warnings),
        }
        for key in (
            "institution_name",
            "apba_id",
            "source_system",
            "source_url",
            "source_record_id",
            "source_file_id",
            "source_disclosure_date",
            "source_posted_date",
            "profile_id",
            *PARSER_UNCERTAINTY_METADATA_FIELDS,
        ):
            if parsed.metadata.get(key):
                metadata[key] = parsed.metadata[key]
        for ancestor in ancestors + [node]:
            key_prefix = {
                "part": "part",
                "chapter": "chapter",
                "section": "section",
                "subsection": "subsection",
                "regulation": "regulation",
                "supplementary": "supplementary",
                "article": "article",
                "paragraph": "paragraph",
                "item": "item",
                "subitem": "subitem",
            }.get(ancestor.node_type)
            if key_prefix:
                metadata[f"{key_prefix}_no"] = ancestor.number
                metadata[f"{key_prefix}_title"] = ancestor.title
                if key_prefix == "regulation":
                    metadata["regulation_node_id"] = ancestor.node_id
                if key_prefix == "paragraph" and ancestor.metadata.get("paragraph_label"):
                    metadata["paragraph_label"] = ancestor.metadata["paragraph_label"]
        if not metadata.get("regulation_no"):
            inferred = self._nearest_preceding_regulation(node, regulation_nodes or [])
            if inferred:
                metadata["regulation_no"] = inferred.number
                metadata["regulation_title"] = inferred.title
                metadata["regulation_inferred_from_order"] = True
                metadata["regulation_source_node_id"] = inferred.node_id
                metadata["regulation_node_id"] = inferred.node_id
        if not metadata.get("regulation_no"):
            document_regulation_title = self._document_regulation_title(parsed)
            if document_regulation_title:
                metadata["regulation_no"] = document_regulation_title
                metadata["regulation_title"] = document_regulation_title
                metadata["regulation_inferred_from_document"] = True
        return metadata

    def _is_supplementary_context(self, node: StructureNode, lookup: dict[str, StructureNode]) -> bool:
        if node.node_type == "supplementary":
            return True
        return any(ancestor.node_type == "supplementary" for ancestor in self._ancestors(node, lookup))

    def _supplementary_context_metadata(
        self,
        node: StructureNode,
        lookup: dict[str, StructureNode],
        metadata: dict,
    ) -> dict:
        if not metadata.get("is_supplementary_provision"):
            return {}
        supplementary = self._nearest_supplementary_context_node(node, lookup)
        if not supplementary:
            return {}
        updates: dict[str, Any] = {}
        if supplementary.number and not metadata.get("supplementary_label"):
            updates["supplementary_label"] = supplementary.number
        if not metadata.get("supplementary_identifier_date"):
            identifier_date = self._supplementary_identifier_date_from_context(
                [
                    supplementary.number,
                    supplementary.title,
                    supplementary.text,
                    metadata.get("hierarchy_path"),
                ]
            )
            if identifier_date:
                updates["supplementary_identifier_date"] = identifier_date
        return updates

    def _nearest_supplementary_context_node(
        self,
        node: StructureNode,
        lookup: dict[str, StructureNode],
    ) -> StructureNode | None:
        if node.node_type == "supplementary":
            return node
        for ancestor in reversed(self._ancestors(node, lookup)):
            if ancestor.node_type == "supplementary":
                return ancestor
        return None

    def _supplementary_identifier_date_from_context(self, candidates: list[Any]) -> str | None:
        for candidate in candidates:
            text = str(candidate or "")
            if not text:
                continue
            match = SUPPLEMENTARY_CONTEXT_DATE.search(text)
            if not match:
                continue
            normalized = self._normalize_context_date(match.group("date"))
            if normalized:
                return normalized
        return None

    def _normalize_context_date(self, value: str) -> str | None:
        dash = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", value)
        if dash:
            year, month, day = (int(part) for part in dash.groups())
            return f"{year:04d}-{month:02d}-{day:02d}"
        return self.metadata_extractor._normalize_date(value)

    def _related_label_metadata(self, related_nodes: list[StructureNode], metadata: dict) -> dict:
        labels: list[dict[str, str | None]] = []
        for node in related_nodes:
            label = node.metadata.get("paragraph_label")
            if not label:
                continue
            labels.append(
                {
                    "node_id": node.node_id,
                    "number": node.number,
                    "label": str(label),
                }
            )
        if not labels:
            return {}
        label_metadata: dict = {"paragraph_labels": labels}
        if metadata.get("is_supplementary_provision"):
            label_metadata["supplementary_paragraph_labels"] = labels
            if len(labels) == 1:
                label_metadata["supplementary_paragraph_label"] = labels[0]["label"]
        return label_metadata

    def _related_source_metadata(self, related_nodes: list[StructureNode]) -> dict:
        hwpx_block_types: list[str] = []
        xml_files: list[str] = []
        caption_count = 0
        caption_parents: list[str] = []
        hwpx_review_flags: list[str] = []
        hwpx_counts = {key: 0 for key in HWPX_SOURCE_COUNT_METADATA_KEYS}
        hwpx_lists = {key: [] for key in HWPX_SOURCE_LIST_METADATA_KEYS}
        hwp_extraction_modes: list[str] = []
        hwp_streams: list[str] = []
        hwp_section_indices: list[int] = []
        hwp_native_table_geometry_seen = False
        hwp_native_table_geometry = False
        raw_text_parts: list[str] = []
        for node in related_nodes:
            raw_text = str(node.metadata.get("raw_text") or "").strip()
            if raw_text and raw_text not in raw_text_parts:
                raw_text_parts.append(raw_text)
            for value in node.metadata.get("source_hwpx_block_types") or []:
                if value not in hwpx_block_types:
                    hwpx_block_types.append(value)
            for value in node.metadata.get("source_xml_files") or []:
                if value not in xml_files:
                    xml_files.append(value)
            if isinstance(node.metadata.get("caption_count"), int):
                caption_count += int(node.metadata["caption_count"])
            caption_parent = node.metadata.get("caption_parent")
            if caption_parent and caption_parent not in caption_parents:
                caption_parents.append(caption_parent)
            for key in HWPX_SOURCE_COUNT_METADATA_KEYS:
                value = node.metadata.get(key)
                if isinstance(value, int):
                    hwpx_counts[key] += int(value)
            for key in HWPX_SOURCE_LIST_METADATA_KEYS:
                values = node.metadata.get(key) or []
                if not isinstance(values, list):
                    continue
                for value in values:
                    if value not in hwpx_lists[key]:
                        hwpx_lists[key].append(value)
            for value in node.metadata.get("hwpx_parser_review_flags") or []:
                if value not in hwpx_review_flags:
                    hwpx_review_flags.append(value)
            for value in node.metadata.get("source_hwp_extraction_modes") or []:
                if value not in hwp_extraction_modes:
                    hwp_extraction_modes.append(value)
            for value in node.metadata.get("source_hwp_streams") or []:
                if value not in hwp_streams:
                    hwp_streams.append(value)
            for value in node.metadata.get("source_hwp_section_indices") or []:
                if isinstance(value, int) and value not in hwp_section_indices:
                    hwp_section_indices.append(value)
            if "source_hwp_native_table_geometry" in node.metadata:
                hwp_native_table_geometry_seen = True
                hwp_native_table_geometry = hwp_native_table_geometry or bool(
                    node.metadata.get("source_hwp_native_table_geometry")
                )
        result: dict = {}
        if hwpx_block_types:
            result["source_hwpx_block_types"] = hwpx_block_types
            result["source_hwpx_block_type_count"] = len(hwpx_block_types)
        if xml_files:
            result["source_xml_files"] = xml_files
        if caption_count:
            result["source_caption_count"] = caption_count
        if caption_parents:
            result["source_caption_parents"] = caption_parents
        if hwpx_review_flags:
            result["source_hwpx_parser_review_flags"] = hwpx_review_flags
        for key, value in hwpx_counts.items():
            if value:
                result[f"source_{key}"] = value
        for key, values in hwpx_lists.items():
            if not values:
                continue
            result[key if key.startswith("source_") else f"source_{key}"] = values[:20]
        if hwp_extraction_modes:
            result["source_hwp_extraction_modes"] = hwp_extraction_modes
        if hwp_streams:
            result["source_hwp_streams"] = hwp_streams
        if hwp_section_indices:
            result["source_hwp_section_indices"] = hwp_section_indices
        if hwp_native_table_geometry_seen:
            result["source_hwp_native_table_geometry"] = hwp_native_table_geometry
        source_bboxes: list[list[float]] = []
        attachment_refs: list[dict] = []
        for node in related_nodes:
            for bbox in node.metadata.get("source_bboxes") or []:
                if bbox not in source_bboxes:
                    source_bboxes.append(bbox)
            for ref in node.metadata.get("attachment_references") or []:
                if ref not in attachment_refs:
                    attachment_refs.append(ref)
        if source_bboxes:
            result["source_bboxes"] = source_bboxes[:50]
            result["source_bbox"] = self._union_bbox(source_bboxes)
        if attachment_refs:
            result["attachment_references"] = attachment_refs[:100]
        if raw_text_parts:
            result["raw_text"] = "\n".join(raw_text_parts)
        return result

    def _entity_trace_metadata(self, node: StructureNode, normalized_text: str) -> dict:
        metadata = {
            "entity_id": node.node_id,
            "parent_id": node.parent_id,
            "source_page": node.page_start,
            "normalized_text": normalized_text.strip(),
            "confidence": node.confidence,
        }
        if node.metadata.get("source_bbox"):
            metadata["source_bbox"] = node.metadata["source_bbox"]
        raw_text = str(node.metadata.get("raw_text") or "").strip()
        if raw_text:
            metadata["raw_text"] = raw_text
        return metadata

    def _attach_document_inventory_metadata(self, chunks: list[Chunk], parsed: ParsedDocument) -> None:
        inventory = parsed.metadata.get("document_inventory")
        kordoc_table_inventory = parsed.metadata.get("kordoc_table_inventory")
        if not chunks or not isinstance(inventory, dict) and not isinstance(kordoc_table_inventory, dict):
            return
        for chunk in chunks:
            # Older code could copy the complete Kordoc inventory to every
            # chunk. Remove that amplification before attaching the document
            # level payload to one review target below.
            if "kordoc_table_inventory" in chunk.metadata:
                compacted = dict(chunk.metadata)
                compacted.pop("kordoc_table_inventory", None)
                chunk.metadata = compacted
        target = self._document_inventory_review_target(chunks)
        target_metadata = dict(target.metadata)
        if isinstance(inventory, dict):
            target_metadata["document_inventory"] = inventory
        if isinstance(kordoc_table_inventory, dict):
            target_metadata["kordoc_table_inventory"] = kordoc_table_inventory
            target_metadata.update(self._kordoc_inventory_metadata(kordoc_table_inventory))
        if parsed.file_type == "hwp" and isinstance(inventory, dict):
            target_metadata["hwp_inventory"] = inventory
            target_metadata["source_hwp_native_table_inventory"] = bool(
                parsed.metadata.get("hwp_native_table_inventory")
            )
        target.metadata = target_metadata

    def _document_inventory_review_target(self, chunks: list[Chunk]) -> Chunk:
        for chunk in chunks:
            metadata = chunk.metadata or {}
            if (
                metadata.get("table_like")
                or metadata.get("table_cell_rows")
                or metadata.get("table_rows")
                or metadata.get("table_markdown")
            ):
                return chunk
        for chunk in chunks:
            if chunk.chunk_type in {"table", "appendix", "form"}:
                return chunk
        return chunks[0]

    def _union_bbox(self, bboxes: list[list[float]]) -> list[float]:
        return [
            min(float(bbox[0]) for bbox in bboxes),
            min(float(bbox[1]) for bbox in bboxes),
            max(float(bbox[2]) for bbox in bboxes),
            max(float(bbox[3]) for bbox in bboxes),
        ]

    def _pdf_table_region_chunks(
        self,
        parsed: ParsedDocument,
        offset: int,
        existing_chunks: list[Chunk] | None = None,
    ) -> list[Chunk]:
        regions = parsed.metadata.get("pdf_table_regions") or []
        if not isinstance(regions, list):
            return []
        chunks: list[Chunk] = []
        existing_chunks = existing_chunks or []
        for index, region in enumerate(regions, start=1):
            if not isinstance(region, dict):
                continue
            text = str(region.get("text") or region.get("title") or "").strip()
            if not text:
                continue
            page_no = region.get("source_page")
            duplicate = self._pdf_table_region_duplicate_target(region, page_no, existing_chunks)
            if duplicate:
                target, coverage = duplicate
                self._attach_suppressed_pdf_table_region(target, region, coverage)
                continue
            title = str(region.get("title") or "").strip() or None
            column_count = int(region.get("column_count") or 0)
            row_count = int(region.get("row_count") or 0)
            metadata = {
                "document_id": parsed.document_id,
                "document_name": parsed.document_name or parsed.source_file,
                "source_file": parsed.source_file,
                "source_page_start": page_no,
                "source_page_end": page_no,
                "chunk_type": "table",
                "hierarchy_path": " > ".join(part for part in [parsed.document_name or parsed.source_file, title] if part),
                "references": [],
                "article_refs": [],
                "appendix_refs": [],
                "form_refs": [],
                "internal_regulation_refs": [],
                "external_law_refs": [],
                "reference_edges": [],
                "resolved_reference_count": 0,
                "unresolved_reference_count": 0,
                "parser_version": "0.1.0",
                "chunker_version": CHUNKER_VERSION,
                "agent_reviewed": False,
                "review_status": "not_requested",
                "warnings": [],
                "table_like": True,
                "table_classification": "pdf_ruling_line_table",
                "table_review_reason": "pdf_ruling_lines_present",
                "table_confidence": 0.95,
                "table_column_count": column_count,
                "table_structured_row_count": row_count,
                "table_title": title,
                "table_citation_label": title,
                "source_bbox": region.get("source_bbox"),
                "pdf_table_region": region,
                "entity_id": f"{parsed.document_id}_table_region_{index:04d}",
                "parent_id": None,
                "raw_text": text,
                "normalized_text": text,
            }
            metadata.update(self.metadata_extractor.extract(text, None))
            chunk_id = f"{parsed.document_id}_table_pdf_region_{offset + index:04d}_p{page_no or 0}_001"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    document_id=parsed.document_id,
                    source_node_ids=[],
                    chunk_type="table",
                    text=text,
                    normalized_text=text,
                    retrieval_text=self._retrieval_text(
                        parsed.document_name or parsed.source_file,
                        metadata["hierarchy_path"],
                        text,
                        None,
                    ),
                    metadata=metadata,
                    source_page_start=page_no,
                    source_page_end=page_no,
                    confidence=0.95,
                    warnings=[],
                )
            )
        return chunks

    def _pdf_table_region_duplicate_target(
        self,
        region: dict[str, Any],
        page_no: Any,
        existing_chunks: list[Chunk],
    ) -> tuple[Chunk, float] | None:
        region_bbox = self._coerce_bbox(region.get("source_bbox"))
        if not region_bbox:
            return None
        region_area = self._bbox_area(region_bbox)
        if region_area <= 0:
            return None
        try:
            page = int(page_no)
        except (TypeError, ValueError):
            page = None
        best: tuple[Chunk, float] | None = None
        for chunk in existing_chunks:
            if not self._chunk_can_cover_pdf_table_region(chunk, page):
                continue
            for bbox in self._chunk_source_bboxes(chunk):
                overlap = self._bbox_intersection_area(region_bbox, bbox)
                if overlap <= 0:
                    continue
                coverage = overlap / region_area
                if coverage < PDF_TABLE_REGION_DUPLICATE_COVERAGE:
                    continue
                if best is None or coverage > best[1]:
                    best = (chunk, coverage)
        return best

    def _chunk_can_cover_pdf_table_region(self, chunk: Chunk, page: int | None) -> bool:
        metadata = chunk.metadata or {}
        if chunk.chunk_type not in {"appendix", "form", "table"} and not metadata.get("table_like"):
            return False
        if page is None:
            return True
        start = chunk.source_page_start or metadata.get("source_page_start") or metadata.get("source_page")
        end = chunk.source_page_end or metadata.get("source_page_end") or start
        try:
            return int(start) <= page <= int(end)
        except (TypeError, ValueError):
            return False

    def _chunk_source_bboxes(self, chunk: Chunk) -> list[list[float]]:
        metadata = chunk.metadata or {}
        bboxes: list[list[float]] = []
        for value in [metadata.get("source_bbox"), *list(metadata.get("source_bboxes") or [])]:
            bbox = self._coerce_bbox(value)
            if bbox:
                bboxes.append(bbox)
        return bboxes

    def _attach_suppressed_pdf_table_region(self, chunk: Chunk, region: dict[str, Any], coverage: float) -> None:
        metadata = dict(chunk.metadata or {})
        suppressed = list(metadata.get("suppressed_pdf_table_regions") or [])
        text = str(region.get("text") or "").strip()
        column_count = self._safe_positive_int(region.get("column_count"))
        row_count = self._safe_positive_int(region.get("row_count"))
        suppressed.append(
            {
                "source_page": region.get("source_page"),
                "source_bbox": region.get("source_bbox"),
                "title": region.get("title"),
                "row_count": region.get("row_count"),
                "column_count": region.get("column_count"),
                "coverage": round(coverage, 4),
                "text_snippet": text[:240],
            }
        )
        metadata["suppressed_pdf_table_region_count"] = len(suppressed)
        metadata["suppressed_pdf_table_regions"] = suppressed[:20]
        metadata["pdf_table_region_duplicate_suppressed"] = True
        metadata["pdf_table_region_layout_evidence"] = True
        metadata["pdf_table_region_title"] = region.get("title")
        if column_count:
            previous_column_count = self._safe_positive_int(metadata.get("table_column_count"))
            if previous_column_count and previous_column_count != column_count:
                metadata["text_table_column_count"] = previous_column_count
            metadata["table_column_count"] = column_count
            metadata["pdf_table_region_column_count"] = column_count
        if row_count:
            previous_row_count = self._safe_positive_int(metadata.get("table_structured_row_count"))
            if previous_row_count and previous_row_count != row_count:
                metadata["text_table_structured_row_count"] = previous_row_count
            metadata["table_structured_row_count"] = row_count
            metadata["pdf_table_region_row_count"] = row_count
        metadata["table_classification"] = "pdf_ruling_line_table"
        metadata["table_review_reason"] = "pdf_ruling_lines_present"
        metadata["table_confidence"] = max(self._safe_float(metadata.get("table_confidence")), 0.95)
        chunk.metadata = metadata

    def _coerce_bbox(self, value: Any) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            x1, y1, x2, y2 = [float(item) for item in value]
        except (TypeError, ValueError):
            return None
        if x2 <= x1 or y2 <= y1:
            return None
        return [x1, y1, x2, y2]

    @staticmethod
    def _safe_positive_int(value: Any) -> int:
        try:
            result = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return result if result > 0 else 0

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _bbox_area(self, bbox: list[float]) -> float:
        return max(bbox[2] - bbox[0], 0.0) * max(bbox[3] - bbox[1], 0.0)

    def _bbox_intersection_area(self, left: list[float], right: list[float]) -> float:
        x1 = max(left[0], right[0])
        y1 = max(left[1], right[1])
        x2 = min(left[2], right[2])
        y2 = min(left[3], right[3])
        if x2 <= x1 or y2 <= y1:
            return 0.0
        return (x2 - x1) * (y2 - y1)

    def _attach_pdf_document_metadata(self, chunks: list[Chunk], parsed: ParsedDocument) -> None:
        blank_pages = parsed.metadata.get("blank_pages") or []
        footnote_links = parsed.metadata.get("pdf_footnote_links") or []
        footnote_marker_references = parsed.metadata.get("pdf_footnote_marker_references") or []
        if not chunks:
            return
        attached_footnote_pages: set[int] = set()
        attached_footnote_marker_pages: set[int] = set()
        for chunk in chunks:
            if blank_pages:
                chunk.metadata["blank_pages"] = blank_pages
            start = chunk.source_page_start
            end = chunk.source_page_end or start
            if footnote_links:
                page_links = [
                    link
                    for link in footnote_links
                    if isinstance(link, dict)
                    and start is not None
                    and end is not None
                    and start <= int(link.get("source_page") or -1) <= end
                    and int(link.get("source_page") or -1) not in attached_footnote_pages
                ]
                if page_links:
                    chunk.metadata["footnote_links"] = page_links
                    attached_footnote_pages.update(int(link["source_page"]) for link in page_links if link.get("source_page"))
            if footnote_marker_references:
                page_marker_references = [
                    reference
                    for reference in footnote_marker_references
                    if isinstance(reference, dict)
                    and start is not None
                    and end is not None
                    and start <= int(reference.get("source_page") or -1) <= end
                    and int(reference.get("source_page") or -1) not in attached_footnote_marker_pages
                ]
                if page_marker_references:
                    chunk.metadata["footnote_marker_references"] = page_marker_references
                    chunk.metadata["footnote_marker_reference_count"] = sum(
                        int(reference.get("marker_count") or 0) for reference in page_marker_references
                    )
                    attached_footnote_marker_pages.update(
                        int(reference["source_page"])
                        for reference in page_marker_references
                        if reference.get("source_page")
                    )

    def _ancestors(self, node: StructureNode, lookup: dict[str, StructureNode]) -> list[StructureNode]:
        ancestors: list[StructureNode] = []
        parent_id = node.parent_id
        while parent_id and parent_id in lookup:
            parent = lookup[parent_id]
            ancestors.append(parent)
            parent_id = parent.parent_id
        return list(reversed(ancestors))

    def _hierarchy_path(self, parsed: ParsedDocument, ancestors: list[StructureNode], node: StructureNode) -> str:
        parts = [parsed.document_name or parsed.source_file]
        for item in ancestors + [node]:
            label = " ".join(value for value in [item.number, item.title] if value)
            if label:
                parts.append(label)
        return " > ".join(parts)

    def _nearest_preceding_regulation(
        self,
        node: StructureNode,
        regulation_nodes: list[StructureNode],
    ) -> StructureNode | None:
        candidates = [item for item in regulation_nodes if item.order_index < node.order_index]
        return max(candidates, key=lambda item: item.order_index) if candidates else None

    def _document_regulation_title(self, parsed: ParsedDocument) -> str | None:
        for page in parsed.pages:
            for block in page.blocks:
                first_line = next((line.strip() for line in block.text.splitlines() if line.strip()), "")
                cleaned = self._clean_document_regulation_title(first_line)
                if self._looks_like_document_title(cleaned):
                    return cleaned
        for fallback in (parsed.document_name, parsed.source_file):
            cleaned = self._clean_document_regulation_title(fallback or "")
            if self._looks_like_document_title(cleaned):
                return cleaned
        return None

    def _clean_document_regulation_title(self, text: str) -> str:
        cleaned = re.sub(r"\.(?:pdf|hwp|hwpx|docx)$", "", str(text or "").strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"^\d+_\d+_", "", cleaned)
        cleaned = re.sub(r"\(\s*\d{4}\s*년도[^)]*(?:개정|제정)[^)]*\)\s*$", "", cleaned)
        cleaned = re.sub(r"[\s\(\[<〈【]*(?:제정|최종개정일|개정|일부개정|시행)\b.*$", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip(" -_()[]<>〈〉【】")

    def _looks_like_document_title(self, text: str) -> bool:
        if not text or len(text) > 120:
            return False
        if self._looks_like_hwp_mojibake_title(text):
            return False
        if re.match(r"^\[?(?:제정|일부개정|개정|시행)\b", text):
            return False
        if re.match(r"^제\s*\d+\s*(?:장|조|절|항)\b", text):
            return False
        if re.match(r"^\d+[\.\-]\d+", text):
            return False
        return True

    def _looks_like_hwp_mojibake_title(self, text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        if not compact or len(compact) > 16:
            return False
        if re.search(r"[가-힣A-Za-z0-9]", compact):
            return False
        known_markers = set("捤獥汤捯氠瑢桤灧灳")
        return any(char in known_markers for char in compact) and bool(re.fullmatch(r"[\u3400-\u9fff]+", compact))

    def _with_context_header(self, text: str, hierarchy_path: str, options: ChunkOptions) -> str:
        if not options.include_context_header:
            return text.strip()
        return f"[위치] {hierarchy_path}\n[본문]\n{text.strip()}"

    def _retrieval_text(
        self,
        document_name: str,
        hierarchy_path: str,
        text: str,
        table_markdown: str | None = None,
    ) -> str:
        lines = [f"[문서명] {document_name}", f"[위치] {hierarchy_path}", "[본문]", text.strip()]
        if table_markdown and table_markdown.strip():
            lines.extend(["[표]", table_markdown.strip()])
        return "\n".join(lines)

    def _attach_reference_edges(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        regulation_index = self._regulation_chunk_index(chunks)
        article_index = self._article_chunk_index(chunks)
        for chunk in chunks:
            metadata = chunk.metadata
            current_regulation_key = self._primary_regulation_key(metadata)
            edges: list[dict] = []
            for value in metadata.get("internal_regulation_refs") or []:
                target = self._resolve_regulation_reference(value, regulation_index)
                edges.append(self._reference_edge("regulation", value, chunk, target))
            for value in metadata.get("article_refs") or []:
                target = None
                article_key = self._normalize_reference_key(value)
                if current_regulation_key and article_key:
                    target = article_index.get((current_regulation_key, article_key))
                if target and target.chunk_id == chunk.chunk_id:
                    target = None
                edges.append(self._reference_edge("article", value, chunk, target))
            metadata["reference_edges"] = edges
            metadata["resolved_reference_count"] = sum(1 for edge in edges if edge.get("resolved"))
            metadata["unresolved_reference_count"] = sum(1 for edge in edges if not edge.get("resolved"))
            self._append_reference_retrieval_text(chunk, edges)

    def _regulation_chunk_index(self, chunks: list[Chunk]) -> dict[str, Chunk]:
        index: dict[str, Chunk] = {}
        for chunk in chunks:
            metadata = chunk.metadata
            keys = self._regulation_keys(metadata.get("regulation_no"), metadata.get("regulation_title"))
            for key in keys:
                existing = index.get(key)
                if existing is None or self._chunk_reference_rank(chunk) < self._chunk_reference_rank(existing):
                    index[key] = chunk
        return index

    def _article_chunk_index(self, chunks: list[Chunk]) -> dict[tuple[str, str], Chunk]:
        index: dict[tuple[str, str], Chunk] = {}
        for chunk in chunks:
            metadata = chunk.metadata
            regulation_key = self._primary_regulation_key(metadata)
            article_key = self._normalize_reference_key(metadata.get("article_no"))
            if not regulation_key or not article_key:
                continue
            key = (regulation_key, article_key)
            existing = index.get(key)
            if existing is None or self._chunk_reference_rank(chunk) < self._chunk_reference_rank(existing):
                index[key] = chunk
        return index

    def _chunk_reference_rank(self, chunk: Chunk) -> tuple[int, int]:
        type_rank = {"regulation": 0, "article": 1, "paragraph": 2, "item": 3, "appendix": 4}
        page = chunk.source_page_start if chunk.source_page_start is not None else 999999
        return type_rank.get(chunk.chunk_type, 9), page

    def _resolve_regulation_reference(self, value: str, regulation_index: dict[str, Chunk]) -> Chunk | None:
        for key in self._regulation_reference_keys(value):
            target = regulation_index.get(key)
            if target:
                return target
        return None

    def _reference_edge(self, ref_type: str, value: str, source: Chunk, target: Chunk | None) -> dict:
        edge = {
            "type": ref_type,
            "value": value,
            "scope": "internal",
            "source_chunk_id": source.chunk_id,
            "source_regulation_no": source.metadata.get("regulation_no"),
            "source_article_no": source.metadata.get("article_no"),
            "resolved": bool(target),
        }
        if target:
            edge.update(
                {
                    "target_document_id": target.document_id,
                    "target_chunk_id": target.chunk_id,
                    "target_chunk_type": target.chunk_type,
                    "target_regulation_no": target.metadata.get("regulation_no"),
                    "target_regulation_title": target.metadata.get("regulation_title"),
                    "target_article_no": target.metadata.get("article_no"),
                    "target_article_title": target.metadata.get("article_title"),
                }
            )
        return edge

    def _append_reference_retrieval_text(self, chunk: Chunk, edges: list[dict]) -> None:
        resolved_edges = [edge for edge in edges if edge.get("resolved")]
        if not resolved_edges or not chunk.retrieval_text or REFERENCE_SECTION_MARKER in chunk.retrieval_text:
            return
        lines = []
        for edge in resolved_edges[:20]:
            target_label = " ".join(
                str(value)
                for value in [
                    edge.get("target_regulation_no"),
                    edge.get("target_regulation_title"),
                    edge.get("target_article_no"),
                    edge.get("target_article_title"),
                ]
                if value
            )
            lines.append(f"- {edge.get('value')} -> {target_label or edge.get('target_chunk_id')}")
        chunk.retrieval_text = f"{chunk.retrieval_text.rstrip()}\n{REFERENCE_SECTION_MARKER}\n" + "\n".join(lines)

    def _primary_regulation_key(self, metadata: dict) -> str:
        for value in (metadata.get("regulation_no"), metadata.get("regulation_title")):
            key = self._normalize_reference_key(value)
            if key:
                return key
        return ""

    def _regulation_keys(self, number: object, title: object) -> set[str]:
        number_key = self._normalize_reference_key(number)
        title_key = self._normalize_reference_key(title)
        keys = {key for key in (number_key, title_key) if key}
        if number_key and title_key:
            keys.add(number_key + title_key)
        return keys

    def _regulation_reference_keys(self, value: object) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        keys = [self._normalize_reference_key(text)]
        match = re.match(r"^\s*(\d+-\d+-\d+)\.?\s*(.*)$", text)
        if match:
            number = self._normalize_reference_key(match.group(1))
            title = self._normalize_reference_key(match.group(2))
            keys.extend(key for key in (number, title, number + title if number and title else "") if key)
        result: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if key and key not in seen:
                seen.add(key)
                result.append(key)
        return result

    def _normalize_reference_key(self, value: object) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[\s「」｢｣『』\[\]【】<>()（）.,·ㆍ:：_-]+", "", text)
        return text

    def _chunk_type(self, node: StructureNode) -> str:
        return {
            "appendix": "appendix",
            "form": "form",
            "part": "part",
            "chapter": "chapter",
            "section": "section",
            "subsection": "subsection",
            "regulation": "regulation",
            "paragraph": "paragraph",
            "item": "item",
            "subitem": "subitem",
            "supplementary": "supplementary_provision",
            "table": "table",
        }.get(node.node_type, "article")

    def _chunk_id(self, document_id: str, node: StructureNode, part_index: int) -> str:
        number = re.sub(r"\W+", "_", node.number or node.node_type, flags=re.UNICODE).strip("_")
        page = f"p{node.page_start}" if node.page_start is not None else "p0"
        return f"{document_id}_{node.node_type}_{number}_{node.order_index + 1:04d}_{page}_{part_index:03d}"

    def _fallback_document_chunks(self, parsed: ParsedDocument, options: ChunkOptions) -> list[Chunk]:
        text = parsed.raw_text.strip() or "\n".join(
            block.text.strip() for page in parsed.pages for block in page.blocks if block.text.strip()
        )
        if not text:
            return []
        pseudo_node = StructureNode(
            node_id=f"{parsed.document_id}_document_fallback",
            document_id=parsed.document_id,
            node_type="article",
            number="document",
            title=parsed.document_name or parsed.source_file,
            text=text,
            page_start=parsed.pages[0].page_no if parsed.pages else 1,
            page_end=parsed.pages[-1].page_no if parsed.pages else 1,
            order_index=0,
            confidence=0.6,
            warnings=["structure_fallback_document_chunk"],
            metadata={"structure_fallback": True},
        )
        hierarchy_path = parsed.document_name or parsed.source_file
        parts = self._split_node(pseudo_node, options)
        chunks: list[Chunk] = []
        for part_index, part in enumerate(parts, start=1):
            metadata = self._metadata_for(pseudo_node, {pseudo_node.node_id: pseudo_node}, parsed, [])
            metadata.update(
                {
                    "part_index": part_index,
                    "part_count": len(parts),
                    "chunk_type": "document",
                    "hierarchy_path": hierarchy_path,
                    "structure_fallback": True,
                }
            )
            metadata.update(self._table_metadata(part, "document"))
            metadata.update(self.metadata_extractor.extract(part, None))
            answer_profile = build_answer_profile(part, metadata)
            metadata.update(answer_profile)
            final_text = self._with_context_header(part, hierarchy_path, options)
            retrieval_text = self._retrieval_text(
                parsed.document_name or parsed.source_file,
                hierarchy_path,
                part,
                metadata.get("table_markdown"),
            )
            retrieval_text = append_answer_profile_to_retrieval_text(retrieval_text, answer_profile)
            chunks.append(
                Chunk(
                    chunk_id=f"{parsed.document_id}_document_fallback_{part_index:03d}",
                    document_id=parsed.document_id,
                    source_node_ids=[],
                    chunk_type="document",
                    text=final_text if options.include_context_header else part.strip(),
                    normalized_text=part.strip(),
                    retrieval_text=retrieval_text,
                    metadata=metadata,
                    source_page_start=pseudo_node.page_start,
                    source_page_end=pseudo_node.page_end,
                    confidence=pseudo_node.confidence,
                    warnings=list(pseudo_node.warnings),
                )
            )
        return chunks
