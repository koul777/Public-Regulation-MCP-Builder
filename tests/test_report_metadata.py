from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import report_metadata
from scripts.report_metadata import (
    MCP_PERFORMANCE_SOURCE_SCOPE,
    capture_mcp_performance_source_state,
    current_repo_commit,
    finalize_mcp_performance_source_state,
)


class ReportMetadataTests(unittest.TestCase):
    def test_current_repo_commit_returns_none_when_git_is_unavailable(self) -> None:
        with patch("scripts.report_metadata.subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(current_repo_commit(Path("missing-git-checkout")))

    def test_current_repo_commit_returns_valid_hash(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=("a" * 40 + "\n").encode("ascii"),
            stderr=b"",
        )
        with patch("scripts.report_metadata.subprocess.run", return_value=completed):
            self.assertEqual(current_repo_commit(Path("repo")), "a" * 40)

    def test_source_state_hash_uses_bytewise_relative_path_order_and_raw_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "scripts/zeta.py": b"zeta\r\n",
                "pyproject.toml": b"[project]\nname='demo'\n",
                "app/\u00e4.py": b"unicode-path\n",
                "app/alpha.py": b"alpha\n",
            }
            _write_source_tree(root, reversed(list(files.items())))

            state = capture_mcp_performance_source_state(root)
            expected = hashlib.sha256()
            expected.update(MCP_PERFORMANCE_SOURCE_SCOPE.encode("utf-8"))
            for relative_path in sorted(
                files,
                key=lambda value: value.encode("utf-8"),
            ):
                _hash_length_prefixed(expected, relative_path.encode("utf-8"))
                _hash_length_prefixed(expected, files[relative_path])

        self.assertEqual("available", state["status"])
        self.assertTrue(state["stable"])
        self.assertEqual(
            {
                "scope",
                "status",
                "sha256",
                "file_count",
                "byte_count",
                "stable",
            },
            set(state),
        )
        self.assertEqual(len(files), state["file_count"])
        self.assertEqual(sum(len(value) for value in files.values()), state["byte_count"])
        self.assertEqual(expected.hexdigest(), state["sha256"])

    def test_source_state_changes_for_content_add_delete_and_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"first\n"),
                    ("scripts/tool.py", b"tool\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )
            baseline = capture_mcp_performance_source_state(root)

            (root / "app" / "main.py").write_bytes(b"second\n")
            content_changed = capture_mcp_performance_source_state(root)
            (root / "app" / "main.py").write_bytes(b"first\n")

            untracked = root / "scripts" / "untracked.py"
            untracked.write_bytes(b"untracked\n")
            added = capture_mcp_performance_source_state(root)
            renamed = root / "scripts" / "renamed.py"
            untracked.rename(renamed)
            renamed_state = capture_mcp_performance_source_state(root)
            renamed.unlink()
            deleted = capture_mcp_performance_source_state(root)

        self.assertNotEqual(baseline["sha256"], content_changed["sha256"])
        self.assertNotEqual(baseline["sha256"], added["sha256"])
        self.assertNotEqual(added["sha256"], renamed_state["sha256"])
        self.assertEqual(baseline["sha256"], deleted["sha256"])

    def test_source_state_includes_gitignored_python_inside_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"app\n"),
                    ("scripts/tool.py", b"script\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )
            baseline = capture_mcp_performance_source_state(root)
            (root / ".gitignore").write_text(
                "scripts/local_override.py\n",
                encoding="utf-8",
            )
            after_gitignore = capture_mcp_performance_source_state(root)
            (root / "scripts" / "local_override.py").write_bytes(b"ignored source\n")
            with_ignored_source = capture_mcp_performance_source_state(root)

        self.assertEqual(baseline["sha256"], after_gitignore["sha256"])
        self.assertNotEqual(baseline["sha256"], with_ignored_source["sha256"])

    def test_source_state_ignores_non_scope_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"app\n"),
                    ("scripts/tool.py", b"script\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )
            baseline = capture_mcp_performance_source_state(root)
            for relative_path in (
                "tests/test_ignored.py",
                "docs/example.py",
                "frontend/operator.py",
                "reports/generated.py",
                "runtime/cache.py",
            ):
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"ignored\n")
            ignored_added = capture_mcp_performance_source_state(root)

        self.assertEqual(baseline["sha256"], ignored_added["sha256"])
        self.assertEqual(baseline["file_count"], ignored_added["file_count"])

    def test_source_state_file_set_drift_is_changed_during_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"app\n"),
                    ("scripts/tool.py", b"script\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )
            initial = report_metadata._enumerate_mcp_performance_source_files(
                root.resolve()
            )
            added = root / "scripts" / "late.py"
            added.write_bytes(b"late\n")
            final = sorted(
                [*initial, added],
                key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
            )
            with patch(
                "scripts.report_metadata._enumerate_mcp_performance_source_files",
                side_effect=[initial, final],
            ):
                state = capture_mcp_performance_source_state(root)

        self.assertEqual("changed_during_run", state["status"])
        self.assertIsNone(state["sha256"])
        self.assertFalse(state["stable"])

    def test_source_state_late_content_race_is_changed_during_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"before\n"),
                    ("scripts/tool.py", b"script\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )

            def read_with_late_change(path: Path) -> bytes:
                raw = path.read_bytes()
                if path.name == "tool.py":
                    (root / "app" / "main.py").write_bytes(b"after\n")
                return raw

            with patch(
                "scripts.report_metadata._read_source_file_bytes",
                side_effect=read_with_late_change,
            ):
                state = capture_mcp_performance_source_state(root)

        self.assertEqual("changed_during_run", state["status"])
        self.assertIsNone(state["sha256"])
        self.assertFalse(state["stable"])

    def test_source_state_unreadable_or_outside_path_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"app\n"),
                    ("scripts/tool.py", b"script\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )
            with patch(
                "scripts.report_metadata._read_source_file_bytes",
                side_effect=PermissionError("denied"),
            ):
                unreadable = capture_mcp_performance_source_state(root)
            with patch(
                "scripts.report_metadata._resolved_source_file",
                side_effect=ValueError("outside"),
            ):
                outside = capture_mcp_performance_source_state(root)

        self.assertEqual("unavailable", unreadable["status"])
        self.assertIsNone(unreadable["sha256"])
        self.assertEqual("unavailable", outside["status"])
        self.assertIsNone(outside["sha256"])

    def test_finalize_source_state_suppresses_digest_after_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_source_tree(
                root,
                [
                    ("app/main.py", b"before\n"),
                    ("scripts/tool.py", b"script\n"),
                    ("pyproject.toml", b"[project]\n"),
                ],
            )
            started = capture_mcp_performance_source_state(root)
            (root / "app" / "main.py").write_bytes(b"after\n")
            finished = finalize_mcp_performance_source_state(started, root)

        self.assertEqual("changed_during_run", finished["status"])
        self.assertIsNone(finished["sha256"])
        self.assertFalse(finished["stable"])

def _write_source_tree(
    root: Path,
    files: Iterable[tuple[str, bytes]],
) -> None:
    for relative_path, content in files:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for directory_name in ("app", "scripts"):
        (root / directory_name).mkdir(parents=True, exist_ok=True)


def _hash_length_prefixed(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


if __name__ == "__main__":
    unittest.main()
