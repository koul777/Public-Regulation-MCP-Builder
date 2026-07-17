from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.backfill_temporal_metadata import backfill_temporal_metadata


class BackfillTemporalMetadataTests(unittest.TestCase):
    def test_backfills_unambiguous_scope_metadata(self) -> None:
        chunks, manifest = backfill_temporal_metadata(
            [
                _chunk(
                    "dated",
                    chunk_type="supplementary_provision",
                    metadata={
                        "document_id": "doc-1",
                        "regulation_no": "1-1",
                        "effective_date": "2026-01-01",
                        "revision_date": "2025-12-30",
                        "valid_from": "2026-01-01",
                    },
                ),
                _chunk(
                    "target",
                    metadata={
                        "document_id": "doc-1",
                        "regulation_no": "1-1",
                        "article_no": "Article2",
                        "warnings": [],
                    },
                ),
            ],
            source_label="unit",
        )

        target = next(chunk for chunk in chunks if chunk["chunk_id"] == "target")
        self.assertEqual(target["metadata"]["effective_date"], "2026-01-01")
        self.assertEqual(target["metadata"]["revision_date"], "2025-12-30")
        self.assertTrue(target["metadata"]["temporal_metadata_inherited"])
        self.assertEqual(1, manifest["delta"]["temporal_metadata_count"])
        self.assertEqual(1, manifest["after"]["inherited_chunk_count"])
        self.assertEqual(0, manifest["after"]["normalized_chunk_count"])

    def test_supplementary_date_ambiguity_does_not_count_as_conflict(self) -> None:
        chunks, manifest = backfill_temporal_metadata(
            [
                _chunk(
                    "dated-a",
                    chunk_type="supplementary_provision",
                    metadata={
                        "document_id": "doc-1",
                        "regulation_no": "1-1",
                        "effective_date": "2026-01-01",
                    },
                ),
                _chunk(
                    "dated-b",
                    chunk_type="supplementary_provision",
                    metadata={
                        "document_id": "doc-1",
                        "regulation_no": "1-1",
                        "effective_date": "2026-02-01",
                    },
                ),
                _chunk(
                    "target",
                    metadata={
                        "document_id": "doc-1",
                        "regulation_no": "1-1",
                        "article_no": "Article2",
                        "warnings": [],
                    },
                ),
            ],
            source_label="unit",
        )

        target = next(chunk for chunk in chunks if chunk["chunk_id"] == "target")
        self.assertNotIn("effective_date", target["metadata"])
        self.assertEqual(["effective_date"], target["metadata"]["temporal_metadata_ambiguous_fields"])
        self.assertEqual(0, manifest["after"]["conflict_chunk_count"])
        self.assertEqual(1, manifest["after"]["ambiguous_chunk_count"])
        self.assertEqual(1, manifest["delta"]["ambiguous_chunk_count"])

    def test_cli_writes_jsonl_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chunks_in = tmp_path / "chunks.jsonl"
            chunks_out = tmp_path / "chunks.out.jsonl"
            manifest_out = tmp_path / "manifest.json"
            chunks_in.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False)
                    for row in [
                        _chunk(
                            "dated",
                            chunk_type="supplementary_provision",
                            metadata={
                                "document_id": "doc-1",
                                "regulation_no": "1-1",
                                "effective_date": "2026-01-01",
                            },
                        ),
                        _chunk(
                            "target",
                            metadata={
                                "document_id": "doc-1",
                                "regulation_no": "1-1",
                                "warnings": [],
                            },
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/backfill_temporal_metadata.py",
                    "--chunks-in",
                    str(chunks_in),
                    "--chunks-out",
                    str(chunks_out),
                    "--manifest-out",
                    str(manifest_out),
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            rows = [json.loads(line) for line in chunks_out.read_text(encoding="utf-8").splitlines()]
            manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
            self.assertEqual("2026-01-01", rows[1]["metadata"]["effective_date"])
            self.assertEqual(1, manifest["after"]["inherited_chunk_count"])
            self.assertEqual(1, manifest["after"]["normalized_chunk_count"])


def _chunk(chunk_id: str, *, metadata: dict, chunk_type: str = "article") -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": metadata.get("document_id") or "doc-1",
        "chunk_type": chunk_type,
        "text": chunk_id,
        "normalized_text": chunk_id,
        "retrieval_text": chunk_id,
        "metadata": metadata,
    }


if __name__ == "__main__":
    unittest.main()
