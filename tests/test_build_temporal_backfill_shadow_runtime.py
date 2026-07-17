from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_temporal_backfill_shadow_runtime import build_temporal_backfill_shadow_runtime


class BuildTemporalBackfillShadowRuntimeTests(unittest.TestCase):
    def test_builds_shadow_repository_and_vectors_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            shadow_dir = root / "shadow"
            source_repository = source_dir / "tenants" / "tenant-demo" / "repository"
            source_repository.mkdir(parents=True)
            chunks_path = source_repository / "doc-demo_chunks.json"
            chunks_path.write_text(
                json.dumps(
                    [
                        _chunk(
                            "chunk-source",
                            chunk_type="supplementary_provision",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "effective_date": "2026-01-01",
                                "revision_date": "2025-12-31",
                                "valid_from": "2026-01-01",
                            },
                        ),
                        _chunk(
                            "chunk-target",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "article_no": "Article 1",
                            },
                        ),
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            manifest = build_temporal_backfill_shadow_runtime(
                source_data_dir=source_dir,
                out_data_dir=shadow_dir,
                tenant_id="tenant-demo",
                tenant_storage_isolation=True,
                out_manifest=root / "manifest.json",
            )

            source_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            shadow_chunks_path = shadow_dir / "tenants" / "tenant-demo" / "repository" / "doc-demo_chunks.json"
            shadow_chunks = json.loads(shadow_chunks_path.read_text(encoding="utf-8"))
            vector_path = shadow_dir / "tenants" / "tenant-demo" / "vector_db" / "tenant-demo" / "approved_vectors.jsonl"
            vector_records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(manifest["passed"])
        self.assertEqual(2, manifest["input_chunk_count"])
        self.assertEqual(2, manifest["vector_record_count"])
        self.assertEqual(1, manifest["delta"]["temporal_metadata_count"])
        self.assertFalse(source_chunks[1]["metadata"].get("effective_date"))
        self.assertEqual("2026-01-01", shadow_chunks[1]["metadata"]["effective_date"])
        self.assertTrue(shadow_chunks[1]["metadata"]["temporal_metadata_inherited"])
        target_record = next(record for record in vector_records if record["chunk_id"] == "chunk-target")
        self.assertEqual("2026-01-01", target_record["metadata"]["effective_date"])
        self.assertTrue(target_record["metadata"]["temporal_metadata_inherited"])

    def test_fail_on_conflict_writes_manifest_without_shadow_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            shadow_dir = root / "shadow"
            source_repository = source_dir / "tenants" / "tenant-demo" / "repository"
            source_repository.mkdir(parents=True)
            chunks_path = source_repository / "doc-demo_chunks.json"
            chunks_path.write_text(
                json.dumps(
                    [
                        _chunk(
                            "chunk-source-a",
                            chunk_type="regulation",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "effective_date": "2026-01-01",
                            },
                        ),
                        _chunk(
                            "chunk-source-b",
                            chunk_type="regulation",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "effective_date": "2026-02-01",
                            },
                        ),
                        _chunk(
                            "chunk-target",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "article_no": "Article 1",
                            },
                        ),
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.json"

            manifest = build_temporal_backfill_shadow_runtime(
                source_data_dir=source_dir,
                out_data_dir=shadow_dir,
                tenant_id="tenant-demo",
                tenant_storage_isolation=True,
                out_manifest=manifest_path,
                fail_on_conflict=True,
            )

            shadow_chunks_path = shadow_dir / "tenants" / "tenant-demo" / "repository" / "doc-demo_chunks.json"
            vector_path = shadow_dir / "tenants" / "tenant-demo" / "vector_db" / "tenant-demo" / "approved_vectors.jsonl"
            written_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertFalse(manifest["passed"])
        self.assertTrue(manifest["write_blocked"])
        self.assertFalse(manifest["shadow_runtime_written"])
        self.assertGreater(manifest["after"]["conflict_chunk_count"], 0)
        self.assertEqual(manifest["conflict_samples"][0]["chunk_id"], "chunk-target")
        self.assertIn("effective_date", manifest["conflict_samples"][0]["conflict_fields"])
        self.assertFalse(shadow_chunks_path.exists())
        self.assertFalse(vector_path.exists())
        self.assertTrue(written_manifest["write_blocked"])

    def test_supplementary_ambiguity_writes_shadow_runtime_without_guessing_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            shadow_dir = root / "shadow"
            source_repository = source_dir / "tenants" / "tenant-demo" / "repository"
            source_repository.mkdir(parents=True)
            chunks_path = source_repository / "doc-demo_chunks.json"
            chunks_path.write_text(
                json.dumps(
                    [
                        _chunk(
                            "chunk-source-a",
                            chunk_type="supplementary_provision",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "effective_date": "2026-01-01",
                            },
                        ),
                        _chunk(
                            "chunk-source-b",
                            chunk_type="supplementary_provision",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "effective_date": "2026-02-01",
                            },
                        ),
                        _chunk(
                            "chunk-target",
                            metadata={
                                "document_id": "doc-demo",
                                "regulation_no": "1",
                                "article_no": "Article 1",
                            },
                        ),
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            manifest = build_temporal_backfill_shadow_runtime(
                source_data_dir=source_dir,
                out_data_dir=shadow_dir,
                tenant_id="tenant-demo",
                tenant_storage_isolation=True,
                fail_on_conflict=True,
            )

            shadow_chunks_path = shadow_dir / "tenants" / "tenant-demo" / "repository" / "doc-demo_chunks.json"
            shadow_chunks = json.loads(shadow_chunks_path.read_text(encoding="utf-8"))
            target = next(chunk for chunk in shadow_chunks if chunk["chunk_id"] == "chunk-target")

        self.assertTrue(manifest["passed"])
        self.assertFalse(manifest["write_blocked"])
        self.assertTrue(manifest["shadow_runtime_written"])
        self.assertEqual(0, manifest["after"]["conflict_chunk_count"])
        self.assertEqual(1, manifest["after"]["ambiguous_chunk_count"])
        self.assertEqual("chunk-target", manifest["ambiguous_samples"][0]["chunk_id"])
        self.assertNotIn("effective_date", target["metadata"])
        self.assertEqual(["effective_date"], target["metadata"]["temporal_metadata_ambiguous_fields"])

    def test_flat_runtime_uses_document_manifest_tenant_for_persisted_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_repository = root / "source" / "repository"
            source_repository.mkdir(parents=True)
            (source_repository / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": {
                            "doc-demo": {"document_id": "doc-demo", "tenant_id": "tenant-demo"}
                        }
                    }
                ),
                encoding="utf-8",
            )
            chunk = _chunk("chunk-flat", metadata={"document_id": "doc-demo"})
            chunk.pop("tenant_id")
            chunk["metadata"].pop("tenant_id")
            (source_repository / "doc-demo_chunks.json").write_text(
                json.dumps([chunk], ensure_ascii=False),
                encoding="utf-8",
            )

            manifest = build_temporal_backfill_shadow_runtime(
                source_data_dir=root / "source",
                out_data_dir=root / "shadow",
                tenant_id="tenant-demo",
                tenant_storage_isolation=False,
            )
            shadow_chunks = json.loads(
                (root / "shadow" / "repository" / "doc-demo_chunks.json").read_text(encoding="utf-8")
            )

        self.assertTrue(manifest["passed"])
        self.assertEqual(manifest["vector_record_count"], 1)
        self.assertEqual(manifest["tenant_provenance"]["enriched_chunk_count"], 1)
        self.assertEqual(shadow_chunks[0]["metadata"]["tenant_id"], "tenant-demo")

    def test_flat_runtime_rejects_document_manifest_tenant_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_repository = root / "source" / "repository"
            source_repository.mkdir(parents=True)
            (source_repository / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": {
                            "doc-demo": {"document_id": "doc-demo", "tenant_id": "tenant-other"}
                        }
                    }
                ),
                encoding="utf-8",
            )
            chunk = _chunk("chunk-flat", metadata={"document_id": "doc-demo"})
            chunk.pop("tenant_id")
            chunk["metadata"].pop("tenant_id")
            (source_repository / "doc-demo_chunks.json").write_text(
                json.dumps([chunk], ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "tenant scope mismatch"):
                build_temporal_backfill_shadow_runtime(
                    source_data_dir=root / "source",
                    out_data_dir=root / "shadow",
                    tenant_id="tenant-demo",
                    tenant_storage_isolation=False,
                )

            self.assertFalse((root / "shadow" / "repository" / "doc-demo_chunks.json").exists())


def _chunk(chunk_id: str, *, metadata: dict, chunk_type: str = "article") -> dict:
    full_metadata = {
        "chunk_id": chunk_id,
        "document_id": metadata.get("document_id") or "doc-demo",
        "tenant_id": "tenant-demo",
        "chunk_type": chunk_type,
        "regulation_title": "Demo Regulation",
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "approved_content_hash": f"approved-hash-{chunk_id}",
        "security_level": "internal",
        **metadata,
    }
    return {
        "chunk_id": chunk_id,
        "document_id": full_metadata["document_id"],
        "tenant_id": "tenant-demo",
        "chunk_type": chunk_type,
        "text": f"Text for {chunk_id}",
        "normalized_text": f"Text for {chunk_id}",
        "retrieval_text": f"Text for {chunk_id}",
        "metadata": full_metadata,
        "approval_status": "approved",
        "approval_id": f"approval-{chunk_id}",
        "approved_content_hash": f"approved-hash-{chunk_id}",
        "security_level": "internal",
    }


if __name__ == "__main__":
    unittest.main()
