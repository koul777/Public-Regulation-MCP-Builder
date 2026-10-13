from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.mcp_server.vercel_runtime import materialize_vercel_runtime


class VercelRuntimeTests(unittest.TestCase):
    def _runtime(self, root: Path, *, expected: bytes = b"approved\n") -> Path:
        source = root / "source"
        (source / "repository").mkdir(parents=True)
        (source / "repository/approvals.jsonl.gz").write_bytes(gzip.compress(b"approved\n"))
        (source / "mcp_runtime_manifest.json").write_text(json.dumps({
            "runtime_data_reuse": {"file_sha256": {
                "repository/approvals.jsonl": hashlib.sha256(expected).hexdigest(),
            }},
        }), encoding="utf-8")
        return source

    def test_restores_original_bytes_and_names_and_reuses_completed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._runtime(root)
            restored = materialize_vercel_runtime(source, cache_root=root / "cache")
            self.assertEqual(b"approved\n", (restored / "repository/approvals.jsonl").read_bytes())
            self.assertFalse((restored / "repository/approvals.jsonl.gz").exists())
            self.assertEqual((source / "mcp_runtime_manifest.json").read_bytes(),
                             (restored / "mcp_runtime_manifest.json").read_bytes())
            self.assertTrue((source / "repository/approvals.jsonl.gz").is_file())
            with patch("app.mcp_server.vercel_runtime.gzip.open", side_effect=AssertionError):
                self.assertEqual(restored, materialize_vercel_runtime(source, cache_root=root / "cache"))

    def test_corrupt_payload_never_marks_cache_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._runtime(root, expected=b"different")
            with self.assertRaisesRegex(ValueError, "hash check"):
                materialize_vercel_runtime(source, cache_root=root / "cache")
            self.assertEqual([], list((root / "cache").rglob(".ready")))

    def test_expansion_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._runtime(root)
            with patch("app.mcp_server.vercel_runtime.MAX_RUNTIME_BYTES", 2):
                with self.assertRaisesRegex(ValueError, "allowed size"):
                    materialize_vercel_runtime(source, cache_root=root / "cache")
            self.assertEqual([], list((root / "cache").rglob(".ready")))

    def test_manifest_paths_cannot_escape_cache(self) -> None:
        for name in ("../outside", "/absolute", "C:/absolute", "repository\\outside"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = self._runtime(root)
                manifest = {"runtime_data_reuse": {"file_sha256": {name: "a" * 64}}}
                (source / "mcp_runtime_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Invalid sealed"):
                    materialize_vercel_runtime(source, cache_root=root / "cache")
                self.assertFalse((root / "cache").exists())
