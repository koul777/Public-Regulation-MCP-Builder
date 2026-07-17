from __future__ import annotations

from pathlib import Path
from typing import Any

from app.parsers.archive_safety import (
    DEFAULT_ARCHIVE_MAX_COMPRESSION_RATIO,
    DEFAULT_ARCHIVE_MAX_ENTRIES,
    DEFAULT_ARCHIVE_MAX_ENTRY_UNCOMPRESSED_BYTES,
    DEFAULT_ARCHIVE_MAX_FILE_BYTES,
    DEFAULT_ARCHIVE_MAX_MEMBER_NAME_CHARS,
    DEFAULT_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES,
    OfficeArchiveLimits,
)
from app.parsers.base import BaseParser, ParserError
from app.parsers.docx_parser import DocxParser
from app.parsers.hwp_parser import HwpParser
from app.parsers.hwpx_parser import HwpxParser
from app.parsers.pdf_parser import PDFParser


def get_parser(path: Path, settings: Any | None = None) -> BaseParser:
    archive_limits = OfficeArchiveLimits(
        max_entries=getattr(settings, "office_archive_max_entries", DEFAULT_ARCHIVE_MAX_ENTRIES),
        max_archive_bytes=(
            getattr(
                settings,
                "office_archive_max_file_mb",
                DEFAULT_ARCHIVE_MAX_FILE_BYTES // (1024 * 1024),
            )
            * 1024
            * 1024
        ),
        max_total_uncompressed_bytes=(
            getattr(
                settings,
                "office_archive_max_total_uncompressed_mb",
                DEFAULT_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024),
            )
            * 1024
            * 1024
        ),
        max_entry_uncompressed_bytes=(
            getattr(
                settings,
                "office_archive_max_entry_uncompressed_mb",
                DEFAULT_ARCHIVE_MAX_ENTRY_UNCOMPRESSED_BYTES // (1024 * 1024),
            )
            * 1024
            * 1024
        ),
        max_compression_ratio=getattr(
            settings,
            "office_archive_max_compression_ratio",
            DEFAULT_ARCHIVE_MAX_COMPRESSION_RATIO,
        ),
        max_member_name_chars=getattr(
            settings,
            "office_archive_max_member_name_chars",
            DEFAULT_ARCHIVE_MAX_MEMBER_NAME_CHARS,
        ),
    )
    pdf_parser = PDFParser(
        ocr_backend=getattr(settings, "pdf_ocr_backend", None),
        ocr_language=getattr(settings, "pdf_ocr_language", None),
        ocr_render_scale=getattr(settings, "pdf_ocr_render_scale", None),
        ocr_timeout_seconds=getattr(settings, "pdf_ocr_timeout_seconds", None),
        ocr_max_pages=getattr(settings, "pdf_ocr_max_pages", None),
    )
    hwp_parser = HwpParser(
        max_decompressed_section_bytes=(
            getattr(settings, "hwp_max_decompressed_section_mb", 256) * 1024 * 1024
        ),
        max_decompressed_document_bytes=(
            getattr(settings, "hwp_max_decompressed_document_mb", 512) * 1024 * 1024
        ),
    )
    for parser in (
        pdf_parser,
        DocxParser(archive_limits=archive_limits),
        HwpxParser(archive_limits=archive_limits),
        hwp_parser,
    ):
        if parser.supports(path):
            return parser
    raise ParserError(f"Unsupported file extension: {path.suffix}")
