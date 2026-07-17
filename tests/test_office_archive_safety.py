from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core.config import Settings
from app.parsers.archive_safety import (
    OfficeArchiveLimits,
    read_archive_member_bounded,
    validate_office_archive,
)
from app.parsers.base import ParserError
from app.parsers.docx_parser import DocxParser
from app.parsers.factory import get_parser
from app.parsers.hwpx_parser import HwpxParser


DOCX_AVAILABLE = importlib.util.find_spec("docx") is not None


class OfficeArchiveSafetyTests(unittest.TestCase):
    def test_factory_wires_office_archive_limits_from_settings(self) -> None:
        settings = Settings(
            office_archive_max_entries=17,
            office_archive_max_file_mb=29,
            office_archive_max_total_uncompressed_mb=23,
            office_archive_max_entry_uncompressed_mb=5,
            office_archive_max_compression_ratio=31.5,
            office_archive_max_member_name_chars=211,
        )

        hwpx = get_parser(Path("sample.hwpx"), settings=settings)
        docx = get_parser(Path("sample.docx"), settings=settings)

        for parser in (hwpx, docx):
            self.assertEqual(17, parser.archive_limits.max_entries)
            self.assertEqual(29 * 1024 * 1024, parser.archive_limits.max_archive_bytes)
            self.assertEqual(23 * 1024 * 1024, parser.archive_limits.max_total_uncompressed_bytes)
            self.assertEqual(5 * 1024 * 1024, parser.archive_limits.max_entry_uncompressed_bytes)
            self.assertEqual(31.5, parser.archive_limits.max_compression_ratio)
            self.assertEqual(211, parser.archive_limits.max_member_name_chars)

    def test_invalid_limit_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            OfficeArchiveLimits(max_entries=0)
        with self.assertRaisesRegex(ValueError, "greater than 1.0"):
            OfficeArchiveLimits(max_compression_ratio=1.0)
        with self.assertRaisesRegex(ValueError, "greater than 1.0"):
            OfficeArchiveLimits(max_compression_ratio=float("nan"))

    def test_hwpx_rejects_excessive_entry_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entries.hwpx"
            self._write_zip(
                path,
                {
                    "Contents/section0.xml": b"<root><p>safe</p></root>",
                    "Contents/section1.xml": b"<root><p>extra</p></root>",
                },
            )
            parser = HwpxParser(archive_limits=self._limits(max_entries=1))

            with self.assertRaisesRegex(ParserError, "entry count 2 exceeds limit 1"):
                parser.parse(path, "doc_entries")

    def test_rejects_oversized_archive_before_zip_directory_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oversized.hwpx"
            self._write_zip(path, {"Contents/section0.xml": b"<root><p>text</p></root>"})
            parser = HwpxParser(archive_limits=self._limits(max_archive_bytes=16))

            with self.assertRaisesRegex(ParserError, "file size .* exceeds limit 16"):
                parser.parse(path, "doc_oversized")

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_docx_rejects_excessive_entry_count_before_package_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entries.docx"
            self._write_zip(path, {"word/document.xml": b"x", "word/styles.xml": b"y"})
            parser = DocxParser(archive_limits=self._limits(max_entries=1))

            with self.assertRaisesRegex(ParserError, "entry count 2 exceeds limit 1"):
                parser.parse(path, "doc_entries")

    def test_rejects_oversized_individual_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "entry.hwpx"
            self._write_zip(path, {"Contents/section0.xml": b"x" * 65})
            parser = HwpxParser(
                archive_limits=self._limits(
                    max_entry_uncompressed_bytes=64,
                    max_total_uncompressed_bytes=256,
                )
            )

            with self.assertRaisesRegex(ParserError, "entry size 65 exceeds limit 64"):
                parser.parse(path, "doc_entry")

    def test_rejects_excessive_total_uncompressed_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "total.hwpx"
            self._write_zip(
                path,
                {
                    "Contents/section0.xml": b"x" * 60,
                    "Contents/section1.xml": b"y" * 60,
                },
            )
            parser = HwpxParser(
                archive_limits=self._limits(
                    max_entry_uncompressed_bytes=64,
                    max_total_uncompressed_bytes=100,
                )
            )

            with self.assertRaisesRegex(ParserError, "total uncompressed size 120 exceeds limit 100"):
                parser.parse(path, "doc_total")

    def test_rejects_excessive_compression_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratio.hwpx"
            self._write_zip(
                path,
                {"Contents/section0.xml": b"A" * 10_000},
                compression=zipfile.ZIP_DEFLATED,
            )
            parser = HwpxParser(
                archive_limits=self._limits(
                    max_entry_uncompressed_bytes=20_000,
                    max_total_uncompressed_bytes=20_000,
                    max_compression_ratio=2.0,
                )
            )

            with self.assertRaisesRegex(ParserError, "compression ratio .* exceeds limit 2.0"):
                parser.parse(path, "doc_ratio")

    def test_rejects_path_traversal_and_absolute_member_names(self) -> None:
        unsafe_names = (
            "../Contents/section0.xml",
            "/Contents/section0.xml",
            "C:/Contents/section0.xml",
            "..\\Contents\\section0.xml",
        )
        for unsafe_name in unsafe_names:
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "unsafe.hwpx"
                if "\\" in unsafe_name:
                    canonical_name = unsafe_name.replace("\\", "/")
                    self._write_zip(path, {canonical_name: b"<root><p>unsafe</p></root>"})
                    path.write_bytes(
                        path.read_bytes().replace(
                            canonical_name.encode("ascii"),
                            unsafe_name.encode("ascii"),
                        )
                    )
                else:
                    self._write_zip(path, {unsafe_name: b"<root><p>unsafe</p></root>"})

                with self.assertRaisesRegex(ParserError, "Unsafe HWPX archive"):
                    HwpxParser().parse(path, "doc_unsafe")

    def test_rejects_case_colliding_member_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicates.hwpx"
            self._write_zip(
                path,
                {
                    "Contents/section0.xml": b"<root><p>first</p></root>",
                    "contents/SECTION0.XML": b"<root><p>second</p></root>",
                },
            )

            with self.assertRaisesRegex(ParserError, "duplicate or ambiguous member path"):
                HwpxParser().parse(path, "doc_duplicates")

    def test_rejects_symbolic_link_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "symlink.hwpx"
            info = zipfile.ZipInfo("Contents/section0.xml")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(info, "../../outside")

            with self.assertRaisesRegex(ParserError, "symbolic-link entries are not supported"):
                HwpxParser().parse(path, "doc_symlink")

    def test_rejects_non_office_compression_method(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lzma.hwpx"
            self._write_zip(
                path,
                {"Contents/section0.xml": b"<root><p>text</p></root>"},
                compression=zipfile.ZIP_LZMA,
            )

            with self.assertRaisesRegex(ParserError, "unsupported compression method"):
                HwpxParser().parse(path, "doc_lzma")

    def test_runtime_member_read_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.hwpx"
            self._write_zip(path, {"Contents/section0.xml": b"x" * 65})
            with zipfile.ZipFile(path) as archive:
                info = validate_office_archive(
                    archive,
                    format_name="HWPX",
                    limits=self._limits(max_entry_uncompressed_bytes=128),
                )[0]
                with self.assertRaisesRegex(ParserError, "decompressed entry exceeded runtime limit 64"):
                    read_archive_member_bounded(
                        archive,
                        info,
                        format_name="HWPX",
                        max_bytes=64,
                    )

    def test_default_hwpx_limits_preserve_normal_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "normal.hwpx"
            self._write_zip(
                path,
                {"Contents/section0.xml": b"<root><p>Normal regulation text</p></root>"},
                compression=zipfile.ZIP_DEFLATED,
            )

            parsed = HwpxParser().parse(path, "doc_normal")

        self.assertEqual("Normal regulation text", parsed.raw_text)

    @unittest.skipUnless(DOCX_AVAILABLE, "python-docx is not installed")
    def test_default_docx_limits_preserve_normal_fixture(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "normal.docx"
            document = Document()
            document.add_paragraph("Normal DOCX regulation text")
            document.save(path)

            parsed = DocxParser().parse(path, "doc_normal")

        self.assertEqual("Normal DOCX regulation text", parsed.raw_text)

    def _limits(self, **overrides: object) -> OfficeArchiveLimits:
        values = {
            "max_entries": 10,
            "max_archive_bytes": 2048,
            "max_total_uncompressed_bytes": 1024,
            "max_entry_uncompressed_bytes": 512,
            "max_compression_ratio": 200.0,
            "max_member_name_chars": 128,
            **overrides,
        }
        return OfficeArchiveLimits(**values)

    def _write_zip(
        self,
        path: Path,
        entries: dict[str, bytes],
        *,
        compression: int = zipfile.ZIP_STORED,
    ) -> None:
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)


if __name__ == "__main__":
    unittest.main()
