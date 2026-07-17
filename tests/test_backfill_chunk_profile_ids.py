from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.structure import StructureNode
from app.storage.repository import JsonRepository
from scripts.backfill_chunk_profile_ids import backfill_chunk_profile_ids


class BackfillChunkProfileIdsTests(unittest.TestCase):
    def test_backfill_fills_missing_profile_id_from_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            repo = JsonRepository(Settings(data_dir=data_dir))
            document = Document(
                document_id="doc_profile_backfill",
                filename="sample.pdf",
                file_type="pdf",
                file_hash="hash-backfill",
                profile_id="local-default",
            )
            node = StructureNode(
                node_id="node_1",
                document_id=document.document_id,
                node_type="article",
                number="1",
                title="Purpose",
                text="Article 1 Purpose",
                order_index=0,
            )
            chunk = Chunk(
                chunk_id="chunk_1",
                document_id=document.document_id,
                source_node_ids=[node.node_id],
                chunk_type="article",
                text="Article 1 Purpose",
                metadata={"source_file": "sample.pdf"},
            )

            repo.upsert_document(document)
            repo.save_processing_result(document.document_id, [node], [chunk], [])

            before = repo.get_chunks(document.document_id)[0].metadata.get("profile_id")
            report = backfill_chunk_profile_ids(data_dir=data_dir, apply=True)
            after = JsonRepository(Settings(data_dir=data_dir)).get_chunks(document.document_id)[0].metadata.get(
                "profile_id"
            )

        self.assertIsNone(before)
        self.assertEqual("local-default", after)
        self.assertTrue(report["passed"])
        self.assertEqual(1, report["updated_chunk_files"])
        self.assertEqual(1, report["updated_chunk_count"])


if __name__ == "__main__":
    unittest.main()
