from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.audit_public_release_readiness import PUBLIC_DOCS
from scripts.create_public_orphan_snapshot import SnapshotError, create_public_orphan_snapshot


class CreatePublicOrphanSnapshotTests(unittest.TestCase):
    def test_exports_only_current_tracked_tree_into_one_root_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "private-source"
            output = root / "public-output"
            source.mkdir()
            self._init_repo(source)

            old_secret = source / "data" / "old-secret.hwp"
            old_secret.parent.mkdir(parents=True)
            old_secret.write_bytes(b"private hwp source")
            self._commit_all(source, "private history")
            old_blob = self._git(source, "rev-parse", "HEAD:data/old-secret.hwp").strip()

            old_secret.unlink()
            self._write_required_public_files(source)
            (source / "app.py").write_text("print('public')\n", encoding="utf-8")
            self._commit_all(source, "verified public tree")
            (source / "data" / "untracked-secret.hwp").write_bytes(b"untracked private hwp")

            report = create_public_orphan_snapshot(
                source,
                source_ref="HEAD",
                output_dir=output,
            )

            self.assertEqual(1, report["commit_count"])
            self.assertEqual([], self._git(output, "remote").splitlines())
            self.assertEqual("1", self._git(output, "rev-list", "--all", "--count").strip())
            self.assertFalse((output / "data" / "old-secret.hwp").exists())
            self.assertFalse((output / "data" / "untracked-secret.hwp").exists())
            missing_blob = subprocess.run(
                ["git", "-C", str(output), "cat-file", "-e", old_blob],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(0, missing_blob.returncode)

    def test_blocks_current_tracked_document_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "private-source"
            output = root / "public-output"
            source.mkdir()
            self._init_repo(source)
            self._write_required_public_files(source)
            leaked = source / "data" / "leaked.hwp"
            leaked.parent.mkdir(exist_ok=True)
            leaked.write_bytes(b"private")
            self._commit_all(source, "unsafe tree")

            with self.assertRaisesRegex(SnapshotError, "tracked-document-sample"):
                create_public_orphan_snapshot(source, source_ref="HEAD", output_dir=output)

            self.assertFalse(output.exists())

    @staticmethod
    def _init_repo(repo: Path) -> None:
        subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)

    @staticmethod
    def _write_required_public_files(repo: Path) -> None:
        (repo / "LICENSE").write_text("test license\n", encoding="utf-8")
        for relative in PUBLIC_DOCS:
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public documentation\n", encoding="utf-8")

    @classmethod
    def _commit_all(cls, repo: Path, message: str) -> None:
        cls._git(repo, "add", "--all")
        cls._git(repo, "commit", "-m", message)

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode("utf-8", "replace")


if __name__ == "__main__":
    unittest.main()
