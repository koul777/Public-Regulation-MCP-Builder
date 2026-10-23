from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.core.security import AuthContext
from app.mcp_server import regulation_tools as mcp
from app.services import regulation_rag_runtime
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage.repository import JsonRepository
from tests.test_regulation_mcp_tools import (
    _approve_and_index_test_chunks, _build_verified_hierarchy_runtime,
    _write_runtime_approval_snapshot_sidecar,
)


class McpProjectionSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = AuthContext(actor="synthetic-reviewer", tenant_id="tenant-a", auth_mode="api_token", role="admin")
        self.document_id = "synthetic-projection-document"
        self.chunk_id = "synthetic-projection-chunk"
        self.profile_id = "synthetic-profile"
        self.body = "제1조 검증용 본문. 지급액은 100원이다."

    def _fixture(self, name: str, *, hierarchy: bool, table_kind: str | None = None,
                 table_review: bool = False, lifecycle: bool = False, immutable_flat: bool = False):
        folder = self.root / name
        settings = replace(Settings(data_dir=folder / "data", artifact_root=folder),
                           rag_trace_enabled=False, api_audit_enabled=False)
        repository = JsonRepository(settings)
        repository.upsert_document(Document(
            document_id=self.document_id, filename="synthetic.pdf", document_name="Synthetic Policy",
            file_type="pdf", file_hash="synthetic-hash", tenant_id=self.auth.tenant_id,
            profile_id=self.profile_id, status="completed",
            regulation_status="pending_approval" if lifecycle else "draft",
            regulation_id="synthetic-regulation" if lifecycle else None,
            regulation_version="v1" if lifecycle else None,
            effective_from="2020-01-01" if lifecycle else None,
        ))
        metadata = {
            "tenant_id": self.auth.tenant_id, "profile_id": self.profile_id,
            "document_name": "Synthetic Policy", "regulation_title": "Synthetic Policy",
            "canonical_regulation_title": "Synthetic Policy", "article_no": "제1조",
            "article_title": "검증", "hierarchy_path": "Synthetic Policy > 제1조",
            "canonical_hierarchy_path": "Synthetic Policy > 제1조",
        }
        if lifecycle:
            metadata.update(regulation_status="pending_approval", regulation_id="synthetic-regulation",
                            regulation_version="v1", effective_from="2020-01-01")
        if table_kind:
            metadata.update(table_like=True, table_id="synthetic-table", table_title="Synthetic table",
                            table_cell_rows=[{"row_index": 0, "cells": ["item", "100"], "raw": "item | 100"}])
            if table_review:
                metadata.update(table_review_required=True, table_review_flags=["unstable_column_count"])
            if table_kind == "inventory":
                metadata["kordoc_table_inventory"] = {
                    "status": "parsed", "table_count": 1, "tables": [{
                        "table_id": "synthetic-table", "table_index": 1,
                        "cell_rows": [{"row_index": 0, "cells": ["item", "100"], "raw": "item | 100"}],
                    }],
                }
        chunk = Chunk(
            chunk_id=self.chunk_id, document_id=self.document_id,
            chunk_type="table" if table_kind else "article", text=self.body, normalized_text=self.body,
            retrieval_text="[문서명] Synthetic Policy\n[위치] Synthetic Policy > 제1조\n[본문]\n" + self.body,
            metadata=metadata, security_level="internal",
        )
        repository.save_processing_result(self.document_id, [], [chunk], [])
        _approve_and_index_test_chunks(folder, settings=settings, repository=repository,
                                       document_id=self.document_id, chunks=[chunk], auth=self.auth,
                                       approval_id="synthetic-approval", review_flags_acknowledged=table_review)
        if hierarchy:
            _build_verified_hierarchy_runtime(settings, self.auth, profile_id=self.profile_id)
        elif immutable_flat:
            records = mcp.routes_rag.load_local_vector_records(settings, self.auth)
            _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id=self.auth.tenant_id)
        return settings, repository

    def _call(self, operation: str, settings):
        kwargs = dict(settings=settings, auth=self.auth, document_id=self.document_id,
                      profile_id=self.profile_id, security_levels=["internal"])
        if operation == "fetch":
            kwargs.pop("document_id")
            return mcp.fetch_regulation(**kwargs, result_id=mcp._encode_result_id(
                document_id=self.document_id, chunk_id=self.chunk_id,
            ))
        if operation == "document":
            return mcp.get_document(**kwargs)
        if operation == "article":
            return mcp.get_article(**kwargs, article_no="제1조")
        return mcp.get_table(**kwargs, table_id="synthetic-table")

    def _mutate(self, repository, mutation):
        if mutation == "review_journal":
            approved_at = repository.list_approval_journal_records(self.document_id)[-1]["approved_at"]
            at = datetime.fromisoformat(approved_at.replace("Z", "+00:00")) + timedelta(seconds=1)
            repository.append_review_record({
                "review_id": "synthetic-review-after-approval", "tenant_id": self.auth.tenant_id,
                "document_id": self.document_id, "chunk_ids": [self.chunk_id], "reviewed_at": at.isoformat(),
            })
        else:
            repository.upsert_document(repository.get_document(self.document_id).model_copy(
                update={"profile_id": "other-synthetic-profile"},
            ))

    def test_source_projection_rechecks_authority_for_flat_and_hierarchy(self):
        original = mcp.source_text_from_current_witness
        for hierarchy in (False, True):
            for operation in ("fetch", "document", "article"):
                for mutation in (("review_journal", "manifest") if operation == "fetch" else ("review_journal",)):
                    with self.subTest(hierarchy=hierarchy, operation=operation, mutation=mutation):
                        settings, repository = self._fixture(
                            f"race-{hierarchy}-{operation}-{mutation}", hierarchy=hierarchy,
                        )
                        self.assertIn(self.body, json.dumps(self._call(operation, settings), ensure_ascii=False))
                        events = []
                        def mutate_during_source(**kwargs):
                            if not events:
                                events.append(mutation)
                                self._mutate(repository, mutation)
                            return original(**kwargs)
                        with patch.object(mcp, "source_text_from_current_witness", side_effect=mutate_during_source):
                            with self.assertRaisesRegex(ValueError, "changed|unavailable"):
                                self._call(operation, settings)
                        self.assertEqual([mutation], events)

    def test_table_rows_require_full_approved_content_for_flat_and_hierarchy(self):
        for hierarchy in (False, True):
            for kind in ("inventory", "rows"):
                with self.subTest(hierarchy=hierarchy, kind=kind):
                    settings, repository = self._fixture(f"table-{hierarchy}-{kind}", hierarchy=hierarchy, table_kind=kind)
                    baseline = self._call("table", settings)
                    self.assertEqual(1, len(baseline["tables"]))
                    self.assertIn("100", json.dumps(baseline))
                    chunk_path = repository.root / f"{self.document_id}_chunks.json"
                    rows = json.loads(chunk_path.read_text(encoding="utf-8"))
                    selected = (rows[0]["metadata"]["kordoc_table_inventory"]["tables"][0]["cell_rows"]
                                if kind == "inventory" else rows[0]["metadata"]["table_cell_rows"])
                    selected[0].update(cells=["INJECTED", "999"], raw="INJECTED | 999")
                    chunk_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                    result = self._call("table", settings)
                    self.assertEqual([], result["tables"])
                    self.assertNotIn("INJECTED", json.dumps(result))

    def test_hierarchy_search_rechecks_authority_after_source_projection(self):
        settings, repository = self._fixture("hierarchy-search-race", hierarchy=True)
        kwargs = dict(settings=settings, auth=self.auth, query="제1조 검증용 본문 지급액 100원",
                      profile_id=self.profile_id, security_levels=["internal"], top_k=1)
        baseline = mcp.search_regulations(**kwargs)
        self.assertIn(self.body, json.dumps(baseline, ensure_ascii=False))
        original = mcp.source_text_from_current_witness
        events = []
        def mutate_during_source(**source_kwargs):
            if not events:
                events.append("review_journal")
                self._mutate(repository, "review_journal")
            return original(**source_kwargs)
        with patch.object(mcp, "source_text_from_current_witness", side_effect=mutate_during_source):
            with self.assertRaisesRegex(ValueError, "changed|unavailable"):
                mcp.search_regulations(**kwargs)
        self.assertEqual(["review_journal"], events)

    def test_flat_search_rechecks_authority_after_source_projection(self):
        settings, repository = self._fixture("flat-search-race", hierarchy=False)
        kwargs = dict(settings=settings, auth=self.auth, query="제1조 검증용 본문 지급액 100원",
                      profile_id=self.profile_id, security_levels=["internal"], top_k=1)
        baseline = mcp.search_regulations(**kwargs)
        self.assertIn(self.body, json.dumps(baseline, ensure_ascii=False))
        original = regulation_rag_runtime.approved_source_text_for_record
        events = []
        def mutate_during_source(*args, **source_kwargs):
            if not events:
                events.append("review_journal")
                self._mutate(repository, "review_journal")
            return original(*args, **source_kwargs)
        with patch.object(regulation_rag_runtime, "approved_source_text_for_record", side_effect=mutate_during_source):
            with self.assertRaisesRegex(ValueError, "changed|unavailable"):
                mcp.search_regulations(**kwargs)
        self.assertEqual(["review_journal"], events)

    def test_version_comparison_rechecks_after_source_projection(self):
        settings, repository = self._fixture("comparison-race", hierarchy=False)
        kwargs = dict(settings=settings, auth=self.auth, base_document_id=self.document_id,
                      target_document_id=self.document_id, profile_id=self.profile_id, security_levels=["internal"])
        baseline = mcp.compare_versions(**kwargs)
        self.assertEqual(1, baseline["summary"]["base_item_count"])
        original = mcp.source_text_from_current_witness
        events = []
        def mutate_during_source(**source_kwargs):
            if not events:
                events.append("review_journal")
                self._mutate(repository, "review_journal")
            return original(**source_kwargs)
        with patch.object(mcp, "source_text_from_current_witness", side_effect=mutate_during_source):
            with self.assertRaisesRegex(ValueError, "changed|unavailable"):
                mcp.compare_versions(**kwargs)
        self.assertEqual(["review_journal"], events)

    def test_table_inventory_rechecks_authority_after_materialization(self):
        original = mcp._kordoc_inventory_table_result
        for hierarchy in (False, True):
            with self.subTest(hierarchy=hierarchy):
                settings, repository = self._fixture(f"inventory-race-{hierarchy}", hierarchy=hierarchy, table_kind="inventory")
                self.assertEqual(1, len(self._call("table", settings)["tables"]))
                events = []
                def mutate_during_inventory(**kwargs):
                    result = original(**kwargs)
                    if not events:
                        events.append("review_journal")
                        self._mutate(repository, "review_journal")
                    return result
                with patch.object(mcp, "_kordoc_inventory_table_result", side_effect=mutate_during_inventory):
                    with self.assertRaisesRegex(ValueError, "changed|unavailable"):
                        self._call("table", settings)
                self.assertEqual(["review_journal"], events)

    def test_generated_table_runtime_cannot_fall_back_when_authority_files_disappear(self):
        for missing in ("manifest", "sidecar", "both"):
            with self.subTest(missing=missing):
                settings, repository = self._fixture(f"missing-{missing}", hierarchy=True, table_kind="rows")
                self.assertEqual(1, len(self._call("table", settings)["tables"]))
                if missing in {"manifest", "both"}:
                    (repository.root.parent / "mcp_runtime_manifest.json").unlink()
                if missing in {"sidecar", "both"}:
                    (repository.root / "approval_snapshot.json").unlink()
                self.assertEqual([], self._call("table", settings)["tables"])

    def test_projection_guard_does_not_expose_private_os_error_paths(self):
        settings, _repository = self._fixture("private-errors", hierarchy=False)
        guard = mcp._McpProjectionReadGuard.capture(settings=settings, auth=self.auth, document_id=self.document_id)
        with patch.object(mcp, "_mcp_projection_source_identity", side_effect=PermissionError("synthetic-private-path")):
            for action in (guard.require_current, lambda: mcp._McpProjectionReadGuard.capture(
                settings=settings, auth=self.auth, document_id=self.document_id,
            )):
                with self.assertRaisesRegex(ValueError, "changed|unavailable") as captured:
                    action()
                self.assertNotIn("synthetic-private-path", str(captured.exception))

    def test_get_table_keeps_verified_structure_review_metadata(self):
        for hierarchy in (False, True):
            for kind in ("inventory", "rows"):
                with self.subTest(hierarchy=hierarchy, kind=kind):
                    settings, _repository = self._fixture(
                        f"warnings-{hierarchy}-{kind}", hierarchy=hierarchy, table_kind=kind, table_review=True,
                    )
                    tables = self._call("table", settings)["tables"]
                    self.assertEqual(1, len(tables))
                    metadata = tables[0]["metadata"]
                    self.assertIs(True, metadata["table_like"])
                    self.assertIs(True, metadata["table_review_required"])
                    self.assertEqual(["unstable_column_count"], metadata["table_review_flags"])
                    self.assertEqual("verified_approved_content", metadata["table_content_status"])

    def test_exported_table_runtime_uses_immutable_authority_after_lifecycle_transition(self):
        from app.services.review_decision_service import approved_content_hash

        for hierarchy, kind in ((h, k) for h in (False, True) for k in ("inventory", "rows")):
            for scoped in (False, True):
                with self.subTest(hierarchy=hierarchy, kind=kind, scoped=scoped):
                    settings, repository = self._fixture(
                        f"exported-{hierarchy}-{kind}-{scoped}", hierarchy=hierarchy,
                        table_kind=kind, lifecycle=True, immutable_flat=not hierarchy,
                    )
                    chunk = repository.get_chunks(self.document_id)[0]
                    self.assertEqual("approved", chunk.metadata["regulation_status"])
                    self.assertNotEqual(chunk.approved_content_hash, approved_content_hash(chunk))
                    snapshot_root = repository.root / "review_snapshots"
                    snapshot_paths = list(snapshot_root.glob("*.jsonl"))
                    self.assertTrue(snapshot_paths)
                    for path in snapshot_paths:
                        path.unlink()
                    snapshot_root.rmdir()
                    kwargs = dict(settings=settings, auth=self.auth, table_id="synthetic-table",
                                  profile_id=self.profile_id, security_levels=["internal"])
                    if scoped:
                        kwargs["document_id"] = self.document_id
                    with patch.object(mcp.routes_rag, "get_visible_records",
                                      side_effect=AssertionError("Generated runtime must not use live visibility")):
                        tables = mcp.get_table(**kwargs)["tables"]
                    self.assertEqual(1, len(tables))
                    self.assertIn("100", json.dumps(tables))
                    self.assertEqual("verified_approved_content", tables[0]["metadata"]["table_content_status"])

    def test_invalid_exported_table_runtime_never_uses_live_visibility(self):
        cases = [(True, kind) for kind in ("manifest", "sidecar", "hierarchy")]
        cases += [(False, kind) for kind in ("manifest", "sidecar", "missing_manifest", "missing_sidecar")]
        for hierarchy, invalid in cases:
            with self.subTest(hierarchy=hierarchy, invalid=invalid):
                settings, repository = self._fixture(
                    f"invalid-exported-{hierarchy}-{invalid}", hierarchy=hierarchy,
                    table_kind="rows", immutable_flat=not hierarchy,
                )
                paths = {"manifest": repository.root.parent / "mcp_runtime_manifest.json",
                         "sidecar": repository.root / "approval_snapshot.json",
                         "hierarchy": mcp.hierarchical_index_path(settings.data_dir)}
                if invalid.startswith("missing_"):
                    paths[invalid.removeprefix("missing_")].unlink()
                else:
                    paths[invalid].write_bytes(b"invalid synthetic authority")
                with patch.object(mcp.routes_rag, "get_visible_records",
                                  side_effect=AssertionError("Invalid generated runtime must not use live visibility")):
                    self.assertEqual([], self._call("table", settings)["tables"])

    def test_plain_flat_table_keeps_live_approval_validation(self):
        settings, _repository = self._fixture("pure-flat-table", hierarchy=False, table_kind="rows")
        with patch.object(mcp.routes_rag, "get_visible_records", wraps=mcp.routes_rag.get_visible_records) as visible:
            self.assertEqual(1, len(self._call("table", settings)["tables"]))
        self.assertEqual(1, visible.call_count)
        self.assertIs(False, visible.call_args.kwargs["use_cached_approval_snapshot"])


if __name__ == "__main__":
    unittest.main()

