from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.normalize_sdist import build_sdist_normalization_report, run


class NormalizeSdistTests(unittest.TestCase):
    def test_different_archive_metadata_normalizes_to_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            self._write_archive(first, gzip_mtime=11, member_mtime=101, uid=1001)
            self._write_archive(second, gzip_mtime=22, member_mtime=202, uid=2002)

            first_report = build_sdist_normalization_report(
                source_date_epoch=1_783_809_530,
                sdist_path=first,
            )
            second_report = build_sdist_normalization_report(
                source_date_epoch=1_783_809_530,
                sdist_path=second,
            )

            self.assertTrue(first_report["passed"], first_report)
            self.assertTrue(second_report["passed"], second_report)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_report["sha256_after"], second_report["sha256_after"])
            self.assertEqual(
                first_report["content_manifest_sha256"],
                second_report["content_manifest_sha256"],
            )
            header = first.read_bytes()[:10]
            self.assertEqual(header[:3], b"\x1f\x8b\x08")
            self.assertEqual(header[3] & 0x08, 0)
            self.assertEqual(int.from_bytes(header[4:8], "little"), 1_783_809_530)
            with tarfile.open(first, "r:gz") as archive:
                members = archive.getmembers()
            self.assertEqual([member.name for member in members], sorted(member.name for member in members))
            self.assertTrue(all(member.mtime == 1_783_809_530 for member in members))
            self.assertTrue(all(member.uid == member.gid == 0 for member in members))
            self.assertTrue(all(member.uname == member.gname == "" for member in members))

    def test_traversal_member_is_rejected_without_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "unsafe.tar.gz"
            self._write_archive(archive_path, unsafe_name="../escape.txt")
            original = archive_path.read_bytes()

            report = build_sdist_normalization_report(
                source_date_epoch=123,
                sdist_path=archive_path,
            )

            self.assertFalse(report["passed"])
            self.assertIn("Unsafe member path", report["issues"][0]["detail"])
            self.assertEqual(original, archive_path.read_bytes())

    def test_traversal_symlink_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "unsafe-link.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                directory = tarfile.TarInfo("example-1.0")
                directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                link = tarfile.TarInfo("example-1.0/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../outside"
                archive.addfile(link)

            report = build_sdist_normalization_report(
                source_date_epoch=123,
                sdist_path=archive_path,
            )

            self.assertFalse(report["passed"])
            self.assertIn("Unsafe link target", report["issues"][0]["detail"])

    def test_cli_emits_json_evidence_and_rejects_multiple_sdist_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            self._write_archive(dist / "reg_rag_preprocessor-0.1.0.tar.gz")
            self._write_archive(dist / "reg_rag_preprocessor-0.2.0.tar.gz")
            report_path = root / "normalization.json"

            exit_code = run(
                [
                    "--project-root",
                    str(root),
                    "--sdist-dir",
                    "dist",
                    "--source-date-epoch",
                    "123",
                    "--out-json",
                    str(report_path),
                    "--fail-on-issue",
                ],
                stdout=io.StringIO(),
            )

            self.assertEqual(exit_code, 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["passed"])
            self.assertIn("found 2", report["issues"][0]["detail"])

    @staticmethod
    def _write_archive(
        path: Path,
        *,
        gzip_mtime: int = 1,
        member_mtime: int = 2,
        uid: int = 3,
        unsafe_name: str | None = None,
    ) -> None:
        with path.open("wb") as raw:
            with gzip.GzipFile(filename="source.tar", mode="wb", fileobj=raw, mtime=gzip_mtime) as zipped:
                with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    directory = tarfile.TarInfo("example-1.0")
                    directory.type = tarfile.DIRTYPE
                    directory.mode = 0o755
                    directory.uid = uid
                    directory.gid = uid + 1
                    directory.uname = "builder"
                    directory.gname = "builders"
                    directory.mtime = member_mtime
                    archive.addfile(directory)

                    content = b"same payload\n"
                    member = tarfile.TarInfo(unsafe_name or "example-1.0/payload.txt")
                    member.size = len(content)
                    member.mode = 0o644
                    member.uid = uid
                    member.gid = uid + 1
                    member.uname = "builder"
                    member.gname = "builders"
                    member.mtime = member_mtime + 1
                    archive.addfile(member, io.BytesIO(content))


if __name__ == "__main__":
    unittest.main()
