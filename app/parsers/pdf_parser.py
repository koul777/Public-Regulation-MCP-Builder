from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from statistics import median

from app.parsers.base import (
    BaseParser,
    OCRRequiredError,
    ParserError,
    document_name_from_path,
    parser_uncertainty_metadata,
)
from app.schemas.parsed import ParsedBlock, ParsedDocument, ParsedPage


ROMAN_FOOTNOTE_MARKERS = {chr(code) for code in range(0x2170, 0x2178)}


class PDFParser(BaseParser):
    supported_extensions = {".pdf"}

    def __init__(
        self,
        *,
        ocr_backend: str | None = None,
        ocr_language: str | None = None,
        ocr_render_scale: float | None = None,
        ocr_timeout_seconds: int | None = None,
        ocr_max_pages: int | None = None,
    ):
        self.ocr_backend = (ocr_backend if ocr_backend is not None else os.getenv("PDF_OCR_BACKEND", "")).strip().lower()
        self.ocr_language = (ocr_language if ocr_language is not None else os.getenv("PDF_OCR_LANGUAGE", "ko")).strip() or "ko"
        self.ocr_render_scale = ocr_render_scale if ocr_render_scale is not None else float(os.getenv("PDF_OCR_RENDER_SCALE", "2"))
        self.ocr_timeout_seconds = (
            ocr_timeout_seconds if ocr_timeout_seconds is not None else int(os.getenv("PDF_OCR_TIMEOUT_SECONDS", "300"))
        )
        if ocr_max_pages is None:
            env_max_pages = int(os.getenv("PDF_OCR_MAX_PAGES", "0"))
            self.ocr_max_pages = env_max_pages if env_max_pages > 0 else None
        else:
            self.ocr_max_pages = ocr_max_pages if ocr_max_pages > 0 else None

    def parse(self, path: Path, document_id: str) -> ParsedDocument:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise ParserError("PDF parsing requires PyMuPDF. Install package 'pymupdf'.") from exc

        pages: list[ParsedPage] = []
        raw_parts: list[str] = []
        blank_pages: list[int] = []
        table_regions: list[dict] = []
        footnote_links: list[dict] = []
        footnote_marker_references: list[dict] = []
        ambiguous_two_column_pages: list[int] = []
        try:
            with fitz.open(path) as doc:
                for page_index, page in enumerate(doc, start=1):
                    blocks = self._layout_line_blocks(page, page_index)
                    if not blocks:
                        blocks = self._text_block_fallback(page)
                    if not blocks:
                        blank_pages.append(page_index)
                    table_regions.extend(self._table_regions(page, page_index, blocks))
                    footnote_links.extend(self._footnote_links(page, page_index))
                    footnote_marker_references.extend(self._footnote_marker_references(page, page_index))
                    if any(block.metadata.get("pdf_layout_reading_order_review") for block in blocks):
                        ambiguous_two_column_pages.append(page_index)
                    for block in blocks:
                        raw_parts.append(str(block.metadata.get("raw_text") or block.text).strip())
                    pages.append(ParsedPage(page_no=page_index, blocks=blocks))
        except Exception as exc:
            raise ParserError(f"Failed to parse PDF file: {exc}") from exc

        if not raw_parts:
            if self.ocr_backend == "windows":
                try:
                    ocr_document = self._parse_with_windows_ocr(path, document_id)
                    if ocr_document.raw_text.strip():
                        return ocr_document
                except ParserError as exc:
                    raise OCRRequiredError(
                        "No text blocks were extracted from the PDF file. "
                        f"Windows OCR fallback failed: {exc}",
                        page_count=len(pages),
                        file_type="pdf",
                        uncertainty_report=parser_uncertainty_metadata(
                            source="pdf",
                            risk_level="high",
                            flags=["no_text_extracted", "ocr_required", "windows_ocr_failed"],
                            confidence=0.0,
                            recommendation="run_ocr",
                            remediation_hint="Run OCR outside the parser or provide a text-embedded PDF before approval.",
                        )["parser_uncertainty"],
                    ) from exc
            raise OCRRequiredError(
                "No text blocks were extracted from the PDF file. OCR may be required.",
                page_count=len(pages),
                file_type="pdf",
                uncertainty_report=parser_uncertainty_metadata(
                    source="pdf",
                    risk_level="high",
                    flags=["no_text_extracted", "ocr_required"],
                    confidence=0.0,
                    recommendation="run_ocr",
                    remediation_hint="Run OCR and review extracted text before approval.",
                )["parser_uncertainty"],
            )

        if ambiguous_two_column_pages:
            uncertainty = parser_uncertainty_metadata(
                source="pdf",
                risk_level="medium",
                flags=["embedded_text_extracted", "pdf_two_column_reading_order_ambiguous"],
                confidence=0.7,
                recommendation="manual_review",
                remediation_hint=(
                    "Review two-column reading order before approval on PDF page(s): "
                    + ", ".join(str(page_no) for page_no in ambiguous_two_column_pages)
                    + "."
                ),
            )
        else:
            uncertainty = parser_uncertainty_metadata(
                source="pdf",
                risk_level="low",
                flags=["embedded_text_extracted"],
                confidence=0.95,
                recommendation="none",
            )

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_name_from_path(path),
            file_type="pdf",
            pages=pages,
            raw_text="\n".join(raw_parts),
            metadata={
                **uncertainty,
                "pdf_parser_version": "layout-lines-v1",
                "blank_pages": blank_pages,
                "missing_content_pages": [],
                "pdf_two_column_reading_order_ambiguous_pages": ambiguous_two_column_pages,
                "pdf_table_regions": table_regions,
                "pdf_footnote_links": footnote_links,
                "pdf_footnote_marker_references": footnote_marker_references,
            },
        )

    def _layout_line_blocks(self, page, page_no: int) -> list[ParsedBlock]:
        groups = self._visual_char_groups(page)
        blocks: list[ParsedBlock] = []
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        column_boundary = self._two_column_boundary(page, groups, page_width=page_width)
        for line_index, chars in enumerate(groups, start=1):
            segments = self._visual_line_segments(
                chars,
                page_width=page_width,
                column_boundary=column_boundary,
            )
            for segment_index, segment_chars in enumerate(segments, start=1):
                text = self._text_from_chars(segment_chars)
                if not text:
                    continue
                x0 = min(item["bbox"][0] for item in segment_chars)
                y0 = min(item["bbox"][1] for item in segment_chars)
                x1 = max(item["bbox"][2] for item in segment_chars)
                y1 = max(item["bbox"][3] for item in segment_chars)
                sizes = [float(item.get("size") or 0) for item in segment_chars if item.get("size")]
                metadata = {
                    "pdf_layout": True,
                    "pdf_layout_line_index": line_index,
                    "pdf_layout_column_segment_index": segment_index,
                    "pdf_layout_column_segment_count": len(segments),
                    "raw_text": text,
                    "source_bbox": [x0, y0, x1, y1],
                    "source_page": page_no,
                    "page_width": page_width,
                    "page_height": page_height,
                    "font_size_median": round(median(sizes), 3) if sizes else None,
                }
                blocks.append(ParsedBlock(text=text, bbox=(x0, y0, x1, y1), metadata=metadata))
        if column_boundary is not None:
            blocks = self._column_major_blocks(blocks, column_boundary=column_boundary)
        return blocks

    def _text_block_fallback(self, page) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text, *_ = block
            clean = (text or "").strip()
            if not clean:
                continue
            blocks.append(
                ParsedBlock(
                    text=clean,
                    bbox=(x0, y0, x1, y1),
                    metadata={"raw_text": clean, "source_bbox": [x0, y0, x1, y1]},
                )
            )
        return blocks

    def _visual_char_groups(self, page) -> list[list[dict]]:
        raw = page.get_text("rawdict")
        chars: list[dict] = []
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        value = str(char.get("c") or "")
                        if not value or not value.strip():
                            continue
                        bbox = tuple(float(part) for part in char.get("bbox", (0, 0, 0, 0)))
                        chars.append(
                            {
                                "c": value,
                                "bbox": bbox,
                                "size": float(span.get("size") or 0),
                                "font": span.get("font"),
                            }
                        )
        chars.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
        groups: list[list[dict]] = []
        for char in chars:
            y0 = char["bbox"][1]
            size = float(char.get("size") or 10)
            tolerance = max(2.2, size * 0.32)
            target: list[dict] | None = None
            for group in reversed(groups[-8:]):
                group_y = median(item["bbox"][1] for item in group)
                if abs(y0 - group_y) <= tolerance:
                    target = group
                    break
            if target is None:
                groups.append([char])
            else:
                target.append(char)
        for group in groups:
            group.sort(key=lambda item: item["bbox"][0])
        groups.sort(key=lambda group: (median(item["bbox"][1] for item in group), min(item["bbox"][0] for item in group)))
        return groups

    def _visual_line_segments(
        self,
        chars: list[dict],
        *,
        page_width: float,
        column_boundary: float | None = None,
    ) -> list[list[dict]]:
        ordered = sorted(chars, key=lambda item: item["bbox"][0])
        if len(ordered) < 8:
            return [ordered]
        widths = [max(0.1, item["bbox"][2] - item["bbox"][0]) for item in ordered]
        typical_width = median(widths) if widths else 10.0
        if column_boundary is not None:
            central_gap_threshold = max(8.0, typical_width * 1.5, page_width * 0.015)
            for index in range(1, len(ordered)):
                previous_x1 = ordered[index - 1]["bbox"][2]
                next_x0 = ordered[index]["bbox"][0]
                if (
                    previous_x1 <= column_boundary <= next_x0
                    and next_x0 - previous_x1 >= central_gap_threshold
                ):
                    return [ordered[:index], ordered[index:]]
        gap_threshold = max(24.0, typical_width * 8.0, page_width * 0.09)
        segments: list[list[dict]] = []
        current: list[dict] = []
        previous_x1: float | None = None
        for item in ordered:
            x0, _, x1, _ = item["bbox"]
            if previous_x1 is not None and x0 - previous_x1 > gap_threshold and current:
                segments.append(current)
                current = []
            current.append(item)
            previous_x1 = x1
        if current:
            segments.append(current)
        if len(segments) <= 1:
            return [ordered]
        if any(len(self._text_from_chars(segment).replace(" ", "")) < 6 for segment in segments):
            return [ordered]
        return segments

    def _two_column_boundary(self, page, groups: list[list[dict]], *, page_width: float) -> float | None:
        get_drawings = getattr(page, "get_drawings", None)
        if callable(get_drawings):
            try:
                if len(get_drawings()) >= 16:
                    return None
            except (RuntimeError, TypeError, ValueError):
                pass

        boundary = page_width / 2.0
        margin = page_width * 0.015
        left_segments: list[list[dict]] = []
        right_segments: list[list[dict]] = []
        split_group_count = 0
        for chars in groups:
            segments = self._visual_line_segments(chars, page_width=page_width)
            if len(segments) > 1:
                split_group_count += 1
            for segment in segments:
                if not segment:
                    continue
                x0 = min(item["bbox"][0] for item in segment)
                x1 = max(item["bbox"][2] for item in segment)
                if x1 <= boundary + margin:
                    left_segments.append(segment)
                elif x0 >= boundary - margin:
                    right_segments.append(segment)

        if len(left_segments) < 6 or len(right_segments) < 6:
            return None
        side_coverage = (len(left_segments) + len(right_segments)) / max(1, len(groups))
        if split_group_count < 2 and side_coverage < 0.7:
            return None

        left_y0 = min(item["bbox"][1] for segment in left_segments for item in segment)
        left_y1 = max(item["bbox"][3] for segment in left_segments for item in segment)
        right_y0 = min(item["bbox"][1] for segment in right_segments for item in segment)
        right_y1 = max(item["bbox"][3] for segment in right_segments for item in segment)
        overlap = max(0.0, min(left_y1, right_y1) - max(left_y0, right_y0))
        shorter_span = max(1.0, min(left_y1 - left_y0, right_y1 - right_y0))
        if overlap / shorter_span < 0.6:
            return None
        return boundary

    def _column_major_blocks(
        self,
        blocks: list[ParsedBlock],
        *,
        column_boundary: float,
    ) -> list[ParsedBlock]:
        def column(block: ParsedBlock) -> int:
            if not block.bbox:
                return 0
            center = (float(block.bbox[0]) + float(block.bbox[2])) / 2.0
            return 0 if center <= column_boundary else 1

        def visual_key(block: ParsedBlock) -> tuple[float, float]:
            if not block.bbox:
                return (0.0, 0.0)
            return (float(block.bbox[1]), float(block.bbox[0]))

        page_width = max(
            (float(block.metadata.get("page_width") or 0.0) for block in blocks),
            default=0.0,
        )
        gutter_margin = max(4.0, page_width * 0.015)

        def spans_gutter(block: ParsedBlock) -> bool:
            if not block.bbox:
                return False
            return (
                float(block.bbox[0]) < column_boundary - gutter_margin
                and float(block.bbox[2]) > column_boundary + gutter_margin
            )

        line_columns: dict[int, set[int]] = {}
        for block in blocks:
            if spans_gutter(block):
                continue
            line_index = int(block.metadata.get("pdf_layout_line_index") or 0)
            line_columns.setdefault(line_index, set()).add(column(block))
        paired_line_indexes = sorted(
            line_index for line_index, columns in line_columns.items() if columns == {0, 1}
        )
        if not paired_line_indexes:
            for block in blocks:
                block.metadata["pdf_layout_reading_order"] = "visual_review_required_two_column"
                block.metadata["pdf_layout_reading_order_review"] = True
            return blocks

        first_body_line_index = paired_line_indexes[0]
        side_body_blocks = [
            block
            for block in blocks
            if not spans_gutter(block)
            and int(block.metadata.get("pdf_layout_line_index") or 0) >= first_body_line_index
        ]
        body_bottom = max(
            (float(block.bbox[3]) for block in side_body_blocks if block.bbox),
            default=0.0,
        )
        header_blocks = [
            block
            for block in blocks
            if int(block.metadata.get("pdf_layout_line_index") or 0) < first_body_line_index
        ]
        footer_blocks = [
            block
            for block in blocks
            if spans_gutter(block) and block.bbox and float(block.bbox[1]) >= body_bottom
        ]
        band_block_ids = {id(block) for block in [*header_blocks, *footer_blocks]}
        body_blocks = [block for block in blocks if id(block) not in band_block_ids]
        ambiguous_spanning_blocks = [block for block in body_blocks if spans_gutter(block)]
        if ambiguous_spanning_blocks:
            for block in blocks:
                block.metadata["pdf_layout_reading_order"] = "visual_review_required_two_column"
                block.metadata["pdf_layout_reading_order_review"] = True
            return blocks

        for block in header_blocks:
            block.metadata["pdf_layout_reading_order"] = "column_major_two_column"
            block.metadata["pdf_layout_reading_order_band"] = "header"
            block.metadata["pdf_layout_column_index"] = 0
        for block in body_blocks:
            block.metadata["pdf_layout_reading_order"] = "column_major_two_column"
            block.metadata["pdf_layout_reading_order_band"] = "body"
            block.metadata["pdf_layout_column_index"] = column(block) + 1
        for block in footer_blocks:
            block.metadata["pdf_layout_reading_order"] = "column_major_two_column"
            block.metadata["pdf_layout_reading_order_band"] = "footer"
            block.metadata["pdf_layout_column_index"] = 0

        return [
            *sorted(header_blocks, key=visual_key),
            *sorted(body_blocks, key=lambda block: (column(block), *visual_key(block))),
            *sorted(footer_blocks, key=visual_key),
        ]

    def _text_from_chars(self, chars: list[dict]) -> str:
        if not chars:
            return ""
        widths = [max(0.1, item["bbox"][2] - item["bbox"][0]) for item in chars]
        typical_width = median(widths) if widths else 10.0
        space_threshold = max(2.8, typical_width * 0.35)
        parts: list[str] = []
        previous_x1: float | None = None
        previous_char = ""
        for item in sorted(chars, key=lambda value: value["bbox"][0]):
            char = str(item.get("c") or "")
            if not char.strip():
                continue
            x0, _, x1, _ = item["bbox"]
            if previous_x1 is not None:
                gap = x0 - previous_x1
                if gap > space_threshold and not self._suppress_layout_space(previous_char, char):
                    parts.append(" ")
            parts.append(char)
            previous_x1 = x1
            previous_char = char
        text = "".join(parts)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s+([)\]\}>.,:;])", r"\1", text)
        text = re.sub(r"([(\[<])\s+", r"\1", text)
        return text

    def _suppress_layout_space(self, previous_char: str, char: str) -> bool:
        if not previous_char:
            return False
        if previous_char in "([<" or char in ")]}>.,:;":
            return True
        if previous_char.isdigit() and char.isdigit():
            return True
        return False

    def _table_regions(self, page, page_no: int, blocks: list[ParsedBlock]) -> list[dict]:
        horizontal: list[tuple[float, float, float, float]] = []
        vertical: list[tuple[float, float, float, float]] = []
        for drawing in page.get_drawings():
            for item in drawing.get("items", []):
                kind = item[0]
                if kind == "l":
                    p1, p2 = item[1], item[2]
                    x0, y0, x1, y1 = float(p1.x), float(p1.y), float(p2.x), float(p2.y)
                    if abs(y0 - y1) < 1:
                        horizontal.append((min(x0, x1), y0, max(x0, x1), y1))
                    elif abs(x0 - x1) < 1:
                        vertical.append((x0, min(y0, y1), x1, max(y0, y1)))
                elif kind == "re":
                    rect = item[1]
                    horizontal.extend(
                        [
                            (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y0)),
                            (float(rect.x0), float(rect.y1), float(rect.x1), float(rect.y1)),
                        ]
                    )
                    vertical.extend(
                        [
                            (float(rect.x0), float(rect.y0), float(rect.x0), float(rect.y1)),
                            (float(rect.x1), float(rect.y0), float(rect.x1), float(rect.y1)),
                        ]
                    )
        page_width = float(page.rect.width)
        long_horizontal = [line for line in horizontal if (line[2] - line[0]) >= page_width * 0.25]
        long_vertical = [line for line in vertical if (line[3] - line[1]) >= 28]
        if len(long_horizontal) < 2 or len(long_vertical) < 3:
            return []

        x_lines = self._cluster_positions([line[0] for line in long_vertical] + [line[2] for line in long_vertical])
        y_lines = self._cluster_positions([line[1] for line in long_horizontal] + [line[3] for line in long_horizontal])
        if len(x_lines) < 3 or len(y_lines) < 2:
            return []
        x0, x1 = min(x_lines), max(x_lines)
        y0, y1 = min(y_lines), max(y_lines)
        if (x1 - x0) < page_width * 0.25 or (y1 - y0) < 25:
            return []

        region_blocks = [
            block
            for block in blocks
            if block.bbox
            and block.bbox[0] >= x0 - 2
            and block.bbox[2] <= x1 + 2
            and block.bbox[1] >= y0 - 2
            and block.bbox[3] <= y1 + 2
        ]
        title = self._table_title_above(blocks, x0, y0, x1)
        text = "\n".join(block.text for block in region_blocks if block.text.strip())
        return [
            {
                "source_page": page_no,
                "source_bbox": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                "column_lines": [round(value, 3) for value in x_lines],
                "row_lines": [round(value, 3) for value in y_lines],
                "column_count": max(0, len(x_lines) - 1),
                "row_count": max(0, len(y_lines) - 1),
                "title": title,
                "text": text,
                "evidence": "pdf_ruling_lines",
            }
        ]

    def _cluster_positions(self, values: list[float], tolerance: float = 2.0) -> list[float]:
        if not values:
            return []
        clusters: list[list[float]] = []
        for value in sorted(values):
            if clusters and abs(value - median(clusters[-1])) <= tolerance:
                clusters[-1].append(value)
            else:
                clusters.append([value])
        return [round(float(median(cluster)), 3) for cluster in clusters]

    def _table_title_above(self, blocks: list[ParsedBlock], x0: float, y0: float, x1: float) -> str | None:
        candidates: list[tuple[float, float, str]] = []
        center = (x0 + x1) / 2
        for block in blocks:
            if not block.bbox:
                continue
            bx0, by0, bx1, by1 = block.bbox
            if by1 >= y0 or y0 - by1 > 90:
                continue
            text = block.text.strip()
            if not text or text.startswith("["):
                continue
            block_center = (bx0 + bx1) / 2
            distance = abs(block_center - center)
            size = float(block.metadata.get("font_size_median") or 0)
            candidates.append((distance - size * 3, by0, text))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], -item[1]))
        return candidates[0][2]

    def _roman_footnote_markers(self, page) -> list[dict]:
        raw = page.get_text("rawdict")
        markers: list[dict] = []
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        value = str(char.get("c") or "")
                        if value not in ROMAN_FOOTNOTE_MARKERS:
                            continue
                        bbox = [float(part) for part in char.get("bbox", (0, 0, 0, 0))]
                        markers.append({"marker": value, "bbox": bbox, "size": float(span.get("size") or 0)})
        return markers

    def _footnote_links(self, page, page_no: int) -> list[dict]:
        markers = self._roman_footnote_markers(page)
        if not markers:
            return []
        page_height = float(page.rect.height)
        bottom_markers = [item for item in markers if item["bbox"][1] >= page_height * 0.86]
        body_markers = [item for item in markers if item["bbox"][1] < page_height * 0.86]
        result: list[dict] = []
        seen: set[str] = set()
        for bottom in sorted(bottom_markers, key=lambda item: (item["marker"], item["bbox"][0], item["bbox"][1])):
            marker = bottom["marker"]
            if marker in seen:
                continue
            seen.add(marker)
            source = next((item for item in body_markers if item["marker"] == marker), bottom)
            result.append(
                {
                    "marker": marker,
                    "source_page": page_no,
                    "marker_bbox": [round(part, 3) for part in source["bbox"]],
                    "footnote_bbox": [round(part, 3) for part in bottom["bbox"]],
                }
            )
        return result

    def _footnote_marker_references(self, page, page_no: int) -> list[dict]:
        markers = self._roman_footnote_markers(page)
        if not markers:
            return []
        page_height = float(page.rect.height)
        bottom_markers = [item for item in markers if item["bbox"][1] >= page_height * 0.86]
        body_markers = [item for item in markers if item["bbox"][1] < page_height * 0.86]
        unique_markers = sorted({str(item["marker"]) for item in markers})
        if not unique_markers:
            return []
        return [
            {
                "source_page": page_no,
                "marker_count": len(unique_markers),
                "markers": unique_markers,
                "bottom_marker_count": len({str(item["marker"]) for item in bottom_markers}),
                "body_marker_count": len({str(item["marker"]) for item in body_markers}),
                "source": "pdf_roman_footnote_markers",
            }
        ]

    def _parse_with_windows_ocr(self, path: Path, document_id: str) -> ParsedDocument:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise ParserError("PDF OCR rendering requires PyMuPDF. Install package 'pymupdf'.") from exc

        with tempfile.TemporaryDirectory(prefix="reg_rag_pdf_ocr_") as tmp:
            tmp_path = Path(tmp)
            image_paths: list[Path] = []
            try:
                with fitz.open(path) as doc:
                    for page_index, page in enumerate(doc, start=1):
                        if self.ocr_max_pages is not None and page_index > self.ocr_max_pages:
                            break
                        image_path = tmp_path / f"page_{page_index:04d}.png"
                        pixmap = page.get_pixmap(
                            matrix=fitz.Matrix(self.ocr_render_scale, self.ocr_render_scale),
                            alpha=False,
                        )
                        pixmap.save(image_path)
                        image_paths.append(image_path)
            except Exception as exc:
                raise ParserError(f"Failed to render PDF pages for OCR: {exc}") from exc

            page_texts = self._extract_windows_ocr_pages(image_paths)

        pages: list[ParsedPage] = []
        raw_parts: list[str] = []
        for page_no, text in enumerate(page_texts, start=1):
            clean = (text or "").strip()
            blocks: list[ParsedBlock] = []
            if clean:
                blocks.append(
                    ParsedBlock(
                        text=clean,
                        metadata={
                            "ocr_backend": "windows",
                            "ocr_language": self.ocr_language,
                        },
                    )
                )
                raw_parts.append(clean)
            pages.append(ParsedPage(page_no=page_no, blocks=blocks))

        if not raw_parts:
            raise ParserError("Windows OCR completed but produced no text.")

        return ParsedDocument(
            document_id=document_id,
            source_file=path.name,
            document_name=document_name_from_path(path),
            file_type="pdf",
            pages=pages,
            raw_text="\n".join(raw_parts),
            metadata={
                "ocr_backend": "windows",
                "ocr_language": self.ocr_language,
                "ocr_page_count": len(pages),
                **parser_uncertainty_metadata(
                    source="pdf",
                    risk_level="medium",
                    flags=["ocr_text_extracted", "windows_ocr"],
                    confidence=0.72,
                    recommendation="review_ocr_text",
                    remediation_hint="Review OCR text for recognition errors before approval.",
                ),
            },
        )

    def _extract_windows_ocr_pages(self, image_paths: list[Path]) -> list[str]:
        if not image_paths:
            return []
        powershell = shutil.which("powershell") or shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("pwsh.exe")
        if not powershell:
            raise ParserError("PowerShell is required for Windows OCR fallback.")

        script = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$languageTag = $args[0]
$imagePaths = @()
if ($args.Length -gt 1) {
    $imagePaths = $args[1..($args.Length - 1)]
}
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
function Await-WinRt($operation, [type]$resultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
        Select-Object -First 1
    $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait()
    $task.Result
}
$language = [Windows.Globalization.Language]::new($languageTag)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    throw "Windows OCR language unavailable: $languageTag"
}
$pages = @()
foreach ($imagePath in $imagePaths) {
    $stream = $null
    try {
        $file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imagePath)) ([Windows.Storage.StorageFile])
        $stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @()
        foreach ($line in $result.Lines) {
            $lines += $line.Text
        }
        $text = [string]::Join("`n", [string[]]$lines)
        if ([string]::IsNullOrWhiteSpace($text)) {
            $text = $result.Text
        }
        $pages += [PSCustomObject]@{
            path = $imagePath
            text = $text
        }
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}
[PSCustomObject]@{ pages = $pages } | ConvertTo-Json -Depth 4 -Compress
"""
        script_path = _write_temporary_powershell_script(script)
        try:
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    self.ocr_language,
                    *map(str, image_paths),
                ],
                capture_output=True,
                encoding="utf-8",
                timeout=self.ocr_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                raise ParserError(stderr or "Windows OCR PowerShell process failed.")
            try:
                payload = json.loads((completed.stdout or "").strip())
            except json.JSONDecodeError as exc:
                raise ParserError(f"Windows OCR returned invalid JSON: {exc}") from exc
        finally:
            script_path.unlink(missing_ok=True)
        pages = payload.get("pages") or []
        if isinstance(pages, dict):
            pages = [pages]
        return [str(page.get("text") or "") for page in pages]


def _write_temporary_powershell_script(script: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        suffix=".ps1",
        prefix="reg_rag_windows_ocr_",
        encoding="utf-8-sig",
        delete=False,
    )
    try:
        handle.write(script)
        return Path(handle.name)
    finally:
        handle.close()
