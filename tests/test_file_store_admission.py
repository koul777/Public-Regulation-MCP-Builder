from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.storage.file_store import FileStore


class FileStoreAdmissionTests(unittest.TestCase):
    def test_stream_upload_rejects_empty_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = FileStore(settings)

            with self.assertRaisesRegex(ValueError, "empty"):
                store.save_upload_stream("doc_empty", "empty.pdf", io.BytesIO(b""))

            self.assertEqual([], list(settings.uploads_dir.glob("*.tmp")))
            self.assertEqual([], list(settings.uploads_dir.glob("*.pdf")))

    def test_stream_upload_rejects_pdf_signature_mismatch_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = FileStore(settings)

            with self.assertRaisesRegex(ValueError, r"\.pdf signature"):
                store.save_upload_stream("doc_bad", "bad.pdf", io.BytesIO(b"not a pdf"))

            self.assertEqual([], list(settings.uploads_dir.glob("*.tmp")))
            self.assertEqual([], list(settings.uploads_dir.glob("*.pdf")))

    def test_stream_upload_accepts_pdf_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = FileStore(settings)

            path, file_hash, total_size = store.save_upload_stream(
                "doc_ok",
                "ok.pdf",
                io.BytesIO(b"%PDF-1.4\nstub"),
            )

        self.assertEqual("doc_ok.pdf", path.name)
        self.assertEqual(13, total_size)
        self.assertEqual(64, len(file_hash))

    def test_stream_upload_reports_progress_by_written_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = FileStore(settings)
            progress_events: list[tuple[int, int | None]] = []

            _path, _file_hash, total_size = store.save_upload_stream(
                "doc_progress",
                "progress.pdf",
                io.BytesIO(b"%PDF-1.4\n123456"),
                chunk_size=5,
                expected_size=15,
                progress_callback=lambda written, expected: progress_events.append((written, expected)),
            )

        self.assertEqual(15, total_size)
        self.assertEqual((0, 15), progress_events[0])
        self.assertEqual((15, 15), progress_events[-1])
        self.assertGreater(len(progress_events), 2)

    def test_bytes_upload_rejects_docx_signature_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = FileStore(settings)

            with self.assertRaisesRegex(ValueError, r"\.docx signature"):
                store.save_upload("doc_bad", "bad.docx", b"not a zip")


if __name__ == "__main__":
    unittest.main()
