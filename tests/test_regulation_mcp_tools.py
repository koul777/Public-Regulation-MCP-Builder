from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from app.api import routes_documents, routes_rag
from app.core.api_audit import api_audit_path
from app.core.config import Settings
from app.core.security import AuthContext
from app.ingestion.vector_adapter import stable_content_hash
from app.mcp_server import regulation_tools
from app.mcp_server.regulation_server import create_regulation_mcp_server
from app.mcp_server.regulation_tools import (
    _FETCH_CHUNK_INDEX_CACHE,
    _mcp_relevance_guard,
    chatgpt_data_fetch_output,
    chatgpt_data_search_output,
    compare_versions,
    fetch_regulation,
    get_article,
    get_citation,
    get_document,
    get_index_status,
    list_regulations,
    get_regulation_history,
    get_table,
    list_documents,
    lookup_regulation,
    mcp_auth_context,
    search_regulations,
    settings_for_mcp_project,
    warm_mcp_runtime,
)
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    hierarchical_index_path,
    write_vector_records_with_offsets,
)
from app.retrieval.bm25_index import (
    BM25_INDEX_VERSION,
    BM25_STRUCTURED_METADATA_VERSION,
    Bm25Index,
)
from app.retrieval.tokenizer import FALLBACK_TOKENIZER_MODEL
from app.retrieval.tokenizer import tokenize
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.services import regulation_rag_runtime
from app.storage.repository import JsonRepository


@unittest.skipUnless(
    os.name == "nt",
    "Windows directory ChangeTime enumeration is required",
)
class WindowsBulkChunkEnumerationTests(unittest.TestCase):
    @staticmethod
    def _descriptor(repository_root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            root=repository_root,
            manifest_path=repository_root / "manifest.json",
            legacy_path=repository_root.parent / "repository.json",
        )

    def test_repository_wide_bulk_matches_scoped_signatures_and_call_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            paths = []
            scoped_signatures = {}
            for index in range(4):
                document_id = f"doc_{index}"
                path = repository_root / f"{document_id}_chunks.json"
                path.write_text(f"[{index}]", encoding="utf-8")
                paths.append(path)
                scoped = (
                    regulation_rag_runtime
                    ._runtime_approval_identity_chunk_entries(
                        self._descriptor(repository_root),
                        document_ids=[document_id],
                    )
                )
                self.assertIsNotNone(scoped)
                assert scoped is not None
                scoped_signatures[path.name] = scoped[0][1]
            (repository_root / "not-a-chunk.json").write_text(
                "{}",
                encoding="utf-8",
            )

            original_lstat = Path.lstat
            chunk_lstat_count = 0

            def track_chunk_lstat(candidate: Path) -> object:
                nonlocal chunk_lstat_count
                if candidate in paths:
                    chunk_lstat_count += 1
                return original_lstat(candidate)

            with (
                patch.object(Path, "lstat", track_chunk_lstat),
                patch.object(
                    regulation_rag_runtime,
                    "_windows_file_change_time_ns",
                    side_effect=AssertionError(
                        "unscoped chunks must not open per-file handles"
                    ),
                ),
                patch.object(
                    regulation_rag_runtime,
                    "_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX",
                    wraps=(
                        regulation_rag_runtime
                        ._WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX
                    ),
                ) as directory_information,
            ):
                bulk = (
                    regulation_rag_runtime
                    ._runtime_approval_identity_chunk_entries(
                        self._descriptor(repository_root),
                        document_ids=None,
                    )
                )

        self.assertIsNotNone(bulk)
        assert bulk is not None
        self.assertEqual(
            scoped_signatures,
            {path.name: signature for path, signature in bulk},
        )
        self.assertEqual(4, chunk_lstat_count)
        # One successful 64 KiB batch followed by ERROR_NO_MORE_FILES.
        self.assertEqual(2, directory_information.call_count)

    def test_document_scoped_identity_keeps_per_file_change_time_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            chunk_path = repository_root / "doc_chunks.json"
            chunk_path.write_text("[]", encoding="utf-8")
            with (
                patch.object(
                    regulation_rag_runtime,
                    "_windows_enumerate_directory_identities",
                    side_effect=AssertionError(
                        "a scoped identity must not enumerate the directory"
                    ),
                ),
                patch.object(
                    regulation_rag_runtime,
                    "_windows_file_change_time_ns",
                    wraps=(
                        regulation_rag_runtime
                        ._windows_file_change_time_ns
                    ),
                ) as per_file_change_time,
            ):
                entries = (
                    regulation_rag_runtime
                    ._runtime_approval_identity_chunk_entries(
                        self._descriptor(repository_root),
                        document_ids=["doc"],
                    )
                )

        self.assertIsNotNone(entries)
        self.assertEqual(1, per_file_change_time.call_count)

    def test_bulk_open_and_non_eof_errors_return_no_partial_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            with patch.object(
                regulation_rag_runtime,
                "_WINDOWS_CREATE_FILE",
                return_value=(
                    regulation_rag_runtime
                    ._WINDOWS_INVALID_HANDLE_VALUE
                ),
            ):
                self.assertIsNone(
                    regulation_rag_runtime
                    ._windows_enumerate_directory_identities(
                        repository_root
                    )
                )

            sample = (
                regulation_rag_runtime._WindowsDirectoryIdentity(
                    name="doc_chunks.json",
                    last_write_time_ns=1,
                    size=2,
                    change_time_ns=3,
                    attributes=0x20,
                    file_id=4,
                )
            )
            information_call_count = 0

            def one_batch_then_access_denied(
                *_args: object,
            ) -> bool:
                nonlocal information_call_count
                information_call_count += 1
                if information_call_count == 1:
                    return True
                ctypes.set_last_error(5)
                return False

            with (
                patch.object(
                    regulation_rag_runtime,
                    "_WINDOWS_CREATE_FILE",
                    return_value=123,
                ),
                patch.object(
                    regulation_rag_runtime,
                    "_WINDOWS_CLOSE_HANDLE",
                    return_value=True,
                ) as close_handle,
                patch.object(
                    regulation_rag_runtime,
                    "_WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX",
                    side_effect=one_batch_then_access_denied,
                ),
                patch.object(
                    regulation_rag_runtime,
                    "_windows_parse_directory_identity_buffer",
                    return_value=[sample],
                ),
            ):
                partial = (
                    regulation_rag_runtime
                    ._windows_enumerate_directory_identities(
                        repository_root
                    )
                )

        self.assertIsNone(partial)
        self.assertEqual(2, information_call_count)
        close_handle.assert_called_once_with(123)

    def test_bulk_parser_rejects_malformed_lengths_and_offsets(
        self,
    ) -> None:
        buffer_size = 256
        buffer = ctypes.create_string_buffer(buffer_size)
        record = (
            regulation_rag_runtime
            ._WindowsFileIdBothDirectoryInfo
            .from_buffer(buffer)
        )
        record.LastWriteTime = 1
        record.ChangeTime = 1
        record.EndOfFile = 0
        record.FileNameLength = 3
        self.assertIsNone(
            regulation_rag_runtime
            ._windows_parse_directory_identity_buffer(
                buffer,
                buffer_size,
            )
        )

        ctypes.memset(buffer, 0, buffer_size)
        record = (
            regulation_rag_runtime
            ._WindowsFileIdBothDirectoryInfo
            .from_buffer(buffer)
        )
        record.LastWriteTime = 1
        record.ChangeTime = 1
        record.EndOfFile = 0
        record.FileNameLength = 2
        record.NextEntryOffset = 106
        ctypes.memmove(
            ctypes.addressof(buffer)
            + regulation_rag_runtime
            ._WINDOWS_DIRECTORY_FILE_NAME_OFFSET,
            "a".encode("utf-16-le"),
            2,
        )
        self.assertIsNone(
            regulation_rag_runtime
            ._windows_parse_directory_identity_buffer(
                buffer,
                buffer_size,
            )
        )

    def test_bulk_rejects_missing_and_duplicate_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            path = repository_root / "doc_chunks.json"
            path.write_text("[]", encoding="utf-8")
            repository_root_resolved = repository_root.resolve(
                strict=True
            )

            with patch.object(
                regulation_rag_runtime,
                "_windows_enumerate_directory_identities",
                side_effect=AssertionError(
                    "duplicate requests must fail before enumeration"
                ),
            ):
                duplicate_requested = (
                    regulation_rag_runtime
                    ._windows_repository_chunk_entries(
                        repository_directory=repository_root,
                        repository_root=repository_root_resolved,
                        chunk_paths=[path, path],
                    )
                )

            with patch.object(
                regulation_rag_runtime,
                "_windows_enumerate_directory_identities",
                return_value={},
            ):
                missing_enumerated = (
                    regulation_rag_runtime
                    ._windows_repository_chunk_entries(
                        repository_directory=repository_root,
                        repository_root=repository_root_resolved,
                        chunk_paths=[path],
                    )
                )

            entry = regulation_rag_runtime._WindowsDirectoryIdentity(
                name=path.name,
                last_write_time_ns=1,
                size=2,
                change_time_ns=3,
                attributes=0x20,
                file_id=4,
            )
            duplicate_enumerated = (
                regulation_rag_runtime
                ._windows_index_directory_identities(
                    [
                        entry,
                        replace(entry, name=path.name.upper()),
                    ]
                )
            )

        self.assertIsNone(duplicate_requested)
        self.assertIsNone(missing_enumerated)
        self.assertIsNone(duplicate_enumerated)

    def test_bulk_lstat_metadata_mismatch_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            chunk_path = repository_root / "doc_chunks.json"
            chunk_path.write_text("[]", encoding="utf-8")
            actual = chunk_path.lstat()
            original_lstat = Path.lstat

            def mismatched_file_id(candidate: Path) -> object:
                if candidate == chunk_path:
                    return SimpleNamespace(
                        st_mode=actual.st_mode,
                        st_file_attributes=(
                            actual.st_file_attributes
                        ),
                        st_mtime_ns=actual.st_mtime_ns,
                        st_size=actual.st_size,
                        st_ino=actual.st_ino + 1,
                    )
                return original_lstat(candidate)

            with patch.object(
                Path,
                "lstat",
                mismatched_file_id,
            ):
                entries = (
                    regulation_rag_runtime
                    ._runtime_approval_identity_chunk_entries(
                        self._descriptor(repository_root),
                        document_ids=None,
                    )
                )

        self.assertIsNone(entries)

    def test_bulk_change_time_detects_same_size_restored_mtime_rewrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            chunk_path = repository_root / "doc_chunks.json"
            chunk_path.write_bytes(b"AAAA")
            original_stat = chunk_path.stat()
            descriptor = self._descriptor(repository_root)
            before = (
                regulation_rag_runtime
                ._runtime_approval_identity_chunk_entries(
                    descriptor,
                    document_ids=None,
                )
            )

            time.sleep(0.02)
            chunk_path.write_bytes(b"BBBB")
            os.utime(
                chunk_path,
                ns=(
                    original_stat.st_atime_ns,
                    original_stat.st_mtime_ns,
                ),
            )
            after_stat = chunk_path.stat()
            after = (
                regulation_rag_runtime
                ._runtime_approval_identity_chunk_entries(
                    descriptor,
                    document_ids=None,
                )
            )

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        assert before is not None
        assert after is not None
        self.assertEqual(original_stat.st_size, after_stat.st_size)
        self.assertEqual(
            original_stat.st_mtime_ns,
            after_stat.st_mtime_ns,
        )
        self.assertEqual(before[0][1][:2], after[0][1][:2])
        self.assertNotEqual(before[0][1][2], after[0][1][2])

    def test_two_snapshot_checks_perform_two_independent_bulk_scans(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_root = data_dir / "repository"
            repository_root.mkdir(parents=True)
            (data_dir / "mcp_runtime_manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (repository_root / "approval_snapshot.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (repository_root / "doc_chunks.json").write_text(
                "[]",
                encoding="utf-8",
            )
            descriptor = self._descriptor(repository_root)
            with patch.object(
                regulation_rag_runtime,
                "_windows_enumerate_directory_identities",
                wraps=(
                    regulation_rag_runtime
                    ._windows_enumerate_directory_identities
                ),
            ) as enumerate_directory:
                before = (
                    regulation_rag_runtime
                    .runtime_approval_snapshot_identity(descriptor)
                )
                after = (
                    regulation_rag_runtime
                    .runtime_approval_snapshot_identity(descriptor)
                )

        self.assertIsNotNone(before)
        self.assertEqual(before, after)
        self.assertEqual(2, enumerate_directory.call_count)


class RegulationMcpToolsTests(unittest.TestCase):
    def test_verified_read_context_builds_manifest_bound_vector_cache_namespace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            vector_path = (
                settings.data_dir
                / "vector_db"
                / "tenant-a"
                / "approved_vectors.jsonl"
            )
            vector_path.parent.mkdir(parents=True)
            vector_path.write_bytes(b"{}\n")
            relative_vector_path = vector_path.relative_to(
                settings.data_dir
            ).as_posix()
            token = regulation_tools._VerifiedHierarchicalRuntimeToken(
                manifest_path=settings.data_dir / "mcp_runtime_manifest.json",
                index_path=settings.data_dir / "hierarchy" / "index.sqlite3",
                vector_path=vector_path,
                manifest_identity=("manifest",),
                index_identity=("index",),
                vector_identity=("vector",),
                expected_index_sha256="a" * 64,
                tenant_id="tenant-a",
                profile_id="profile-a",
                manifest_record_count=1,
                manifest_file_sha256=((relative_vector_path, "b" * 64),),
                hierarchy_record_count=1,
                hierarchy_source_content_hashes="c" * 64,
            )
            context = regulation_tools._VerifiedHierarchicalReadContext(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                profile_id="profile-a",
                runtime_token=token,
                authorization_identity=("approval",),
                authorization_document_ids=None,
            )

            namespace = context.verified_vector_cache_namespace
            missing_hash = replace(
                context,
                runtime_token=replace(token, manifest_file_sha256=()),
            ).verified_vector_cache_namespace

        self.assertIsNotNone(namespace)
        assert namespace is not None
        self.assertEqual("tenant-a", namespace.tenant_id)
        self.assertEqual("profile-a", namespace.profile_id)
        self.assertEqual(("vector",), namespace.vector_identity)
        self.assertEqual("a" * 64, namespace.expected_index_sha256)
        self.assertEqual("b" * 64, namespace.expected_vector_sha256)
        self.assertIsNone(missing_hash)

    def setUp(self) -> None:
        regulation_tools._HIERARCHICAL_INDEX_VERIFICATION_CACHE.clear()
        regulation_tools._HIERARCHICAL_VERIFIED_RUNTIME_TOKENS.clear()
        regulation_tools._HIERARCHICAL_BM25_VERIFICATION_CACHE.clear()
        regulation_tools._HIERARCHICAL_PROFILE_VERIFICATION_CACHE.clear()
        regulation_tools._HIERARCHICAL_VISIBILITY_CACHE.clear()
        regulation_tools._HIERARCHICAL_FETCH_RECORD_CACHE.clear()
        regulation_tools._VISIBLE_DOCUMENT_RECORD_CACHE.clear()

    def test_hierarchy_visibility_uses_runtime_sidecar_without_vector_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )
            snapshot = {
                ("doc-a", "chunk-visible"): {
                    "approval_id": "approval-visible",
                    "approved_content_hash": "approved-hash-visible",
                    "content_hash": "content-hash-visible",
                    "security_level": "internal",
                    "department_acl": {"hr"},
                },
                ("doc-a", "chunk-denied"): {
                    "approval_id": "approval-denied",
                    "approved_content_hash": "approved-hash-denied",
                    "content_hash": "content-hash-denied",
                    "security_level": "internal",
                    "department_acl": {"legal"},
                },
            }

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("index-identity",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-identity",),
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ) as indexed_documents,
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=snapshot,
                ) as load_snapshot,
                patch.object(
                    regulation_tools,
                    "fully_visible_regulation_unit_ids",
                    return_value={"unit-a"},
                ) as visible_units,
                patch.object(
                    regulation_tools,
                    "_visible_records",
                    side_effect=AssertionError("vector scan must not run"),
                ),
            ):
                result = regulation_tools._fully_visible_regulation_units(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                    security_levels=["internal"],
                    department_ids=["hr"],
                )
                cached_result = regulation_tools._fully_visible_regulation_units(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                    security_levels=["internal"],
                    department_ids=["hr"],
                )

        self.assertEqual({"unit-a"}, result)
        self.assertEqual(result, cached_result)
        indexed_documents.assert_called_once()
        load_snapshot.assert_called_once()
        visible_units.assert_called_once()
        self.assertEqual(
            {("doc-a", "chunk-visible", "content-hash-visible")},
            visible_units.call_args.kwargs["visible_record_signatures"],
        )
        self.assertEqual(
            "profile-a",
            visible_units.call_args.kwargs["profile_id"],
        )

    def test_hierarchy_visibility_falls_back_when_runtime_sidecar_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            records = [
                {
                    "document_id": "doc-a",
                    "chunk_id": "chunk-a",
                    "metadata": {},
                }
            ]

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("index-identity",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-identity",),
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools,
                    "_visible_records",
                    return_value=records,
                ) as visible_records,
                patch.object(
                    regulation_tools,
                    "fully_visible_regulation_unit_ids",
                    return_value={"unit-a"},
                ) as visible_units,
            ):
                result = regulation_tools._fully_visible_regulation_units(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                )

        self.assertEqual({"unit-a"}, result)
        visible_records.assert_called_once()
        self.assertEqual(
            {("doc-a", "chunk-a")},
            visible_units.call_args.kwargs["visible_record_keys"],
        )

    def test_hierarchy_authorization_lazily_opens_live_repository_only_for_fallback(
        self,
    ) -> None:
        settings = Settings(data_dir=Path("synthetic-runtime"))
        descriptor = regulation_tools.repository_path_descriptor(settings)
        live_repository = object()
        with (
            patch.object(
                regulation_tools.routes_rag,
                "runtime_approval_snapshot_identity",
                side_effect=[("sidecar-identity",), None],
            ),
            patch.object(
                regulation_tools,
                "indexed_document_ids",
                return_value={"doc-a"},
            ),
            patch.object(
                regulation_tools,
                "_json_repository",
                return_value=live_repository,
            ) as open_repository,
            patch.object(
                regulation_tools.routes_rag,
                "approval_snapshot_signature",
                return_value=("live-identity",),
            ) as live_signature,
        ):
            sidecar_identity = (
                regulation_tools._hierarchical_authorization_source_identity(
                    settings=settings,
                    repository_paths=descriptor,
                    index_path=Path("hierarchy.sqlite3"),
                    profile_id="profile-a",
                )
            )
            open_repository.assert_not_called()
            live_identity = (
                regulation_tools._hierarchical_authorization_source_identity(
                    settings=settings,
                    repository_paths=descriptor,
                    index_path=Path("hierarchy.sqlite3"),
                    profile_id="profile-a",
                )
            )

        self.assertEqual(
            ("runtime_sidecar", ("sidecar-identity",)),
            sidecar_identity,
        )
        self.assertEqual(
            ("live_repository", ("live-identity",)),
            live_identity,
        )
        open_repository.assert_called_once_with(settings)
        live_signature.assert_called_once_with(live_repository, ["doc-a"])

    def test_runtime_sidecar_visibility_rejects_unauthorized_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )

            with self.assertRaisesRegex(ValueError, "department"):
                regulation_tools._runtime_sidecar_visible_regulation_units(
                    settings=settings,
                    auth=auth,
                    profile_id=None,
                    index_path=Path("synthetic.sqlite3"),
                    security_levels=["internal"],
                    department_ids=["legal"],
                )

    def test_runtime_sidecar_empty_security_levels_use_role_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )
            snapshot = {
                ("doc-a", "chunk-a"): {
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "content_hash": "content-hash-a",
                    "security_level": "internal",
                    "department_acl": {"hr"},
                }
            }

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("index-identity",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-identity",),
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=snapshot,
                ),
                patch.object(
                    regulation_tools,
                    "fully_visible_regulation_unit_ids",
                    return_value={"unit-a"},
                ) as visible_units,
            ):
                omitted_levels = (
                    regulation_tools._runtime_sidecar_visible_regulation_units(
                        settings=settings,
                        auth=auth,
                        profile_id="profile-a",
                        index_path=Path("synthetic.sqlite3"),
                        security_levels=None,
                        department_ids=["hr"],
                    )
                )
                regulation_tools._HIERARCHICAL_VISIBILITY_CACHE.clear()
                empty_levels = (
                    regulation_tools._runtime_sidecar_visible_regulation_units(
                        settings=settings,
                        auth=auth,
                        profile_id="profile-a",
                        index_path=Path("synthetic.sqlite3"),
                        security_levels=[],
                        department_ids=["hr"],
                    )
                )

        self.assertEqual({"unit-a"}, omitted_levels)
        self.assertEqual(omitted_levels, empty_levels)
        self.assertEqual(2, visible_units.call_count)
        self.assertEqual(
            visible_units.call_args_list[0].kwargs["visible_record_signatures"],
            visible_units.call_args_list[1].kwargs["visible_record_signatures"],
        )

    def test_hierarchy_visibility_cache_isolated_by_authorized_departments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            snapshot = {
                ("doc-a", "chunk-hr"): {
                    "approval_id": "approval-hr",
                    "approved_content_hash": "approved-hash-hr",
                    "content_hash": "content-hash-hr",
                    "security_level": "internal",
                    "department_acl": {"hr"},
                },
                ("doc-a", "chunk-legal"): {
                    "approval_id": "approval-legal",
                    "approved_content_hash": "approved-hash-legal",
                    "content_hash": "content-hash-legal",
                    "security_level": "internal",
                    "department_acl": {"legal"},
                },
            }

            def units_for_scope(*_args, **kwargs):
                signatures = kwargs["visible_record_signatures"]
                return {
                    "unit-hr"
                    if any(item[1] == "chunk-hr" for item in signatures)
                    else "unit-legal"
                }

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("index-identity",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-identity",),
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=snapshot,
                ),
                patch.object(
                    regulation_tools,
                    "fully_visible_regulation_unit_ids",
                    side_effect=units_for_scope,
                ) as visible_units,
            ):
                hr_units = regulation_tools._runtime_sidecar_visible_regulation_units(
                    settings=settings,
                    auth=mcp_auth_context(
                        tenant_id="tenant-a",
                        role="operator",
                        department_ids=["hr"],
                    ),
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                    department_ids=["hr"],
                )
                legal_units = regulation_tools._runtime_sidecar_visible_regulation_units(
                    settings=settings,
                    auth=mcp_auth_context(
                        tenant_id="tenant-a",
                        role="operator",
                        department_ids=["legal"],
                    ),
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                    department_ids=["legal"],
                )

        self.assertEqual({"unit-hr"}, hr_units)
        self.assertEqual({"unit-legal"}, legal_units)
        self.assertEqual(2, visible_units.call_count)

    def test_hierarchy_visibility_does_not_cache_files_changed_during_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            snapshot = {
                ("doc-a", "chunk-a"): {
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "content_hash": "content-hash-a",
                    "security_level": "internal",
                    "department_acl": set(),
                }
            }

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("index-identity",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    side_effect=[
                        ("approval-before",),
                        ("approval-after",),
                    ],
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=snapshot,
                ),
                patch.object(
                    regulation_tools,
                    "fully_visible_regulation_unit_ids",
                    return_value={"unit-a"},
                ),
            ):
                result = regulation_tools._runtime_sidecar_visible_regulation_units(
                    settings=settings,
                    auth=mcp_auth_context(tenant_id="tenant-a"),
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                )

        self.assertIsNone(result)
        self.assertEqual(0, len(regulation_tools._HIERARCHICAL_VISIBILITY_CACHE))

    def test_hierarchy_visibility_cache_hit_revalidates_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            snapshot = {
                ("doc-a", "chunk-a"): {
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "content_hash": "content-hash-a",
                    "security_level": "internal",
                    "department_acl": set(),
                }
            }

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("index-identity",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    side_effect=[
                        ("approval-stable",),
                        ("approval-stable",),
                        ("approval-stable",),
                        ("approval-changed",),
                    ],
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=snapshot,
                ) as load_snapshot,
                patch.object(
                    regulation_tools,
                    "fully_visible_regulation_unit_ids",
                    return_value={"unit-a"},
                ),
            ):
                first = regulation_tools._runtime_sidecar_visible_regulation_units(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                )
                second = regulation_tools._runtime_sidecar_visible_regulation_units(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    index_path=Path("synthetic.sqlite3"),
                )

        self.assertEqual({"unit-a"}, first)
        self.assertIsNone(second)
        load_snapshot.assert_called_once()

    def test_verified_hierarchy_search_uses_sidecar_without_full_vector_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            auth = mcp_auth_context(tenant_id="tenant-a")
            vector_path = routes_rag._local_vector_path(settings, auth)
            records = [
                json.loads(line)
                for line in vector_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = hierarchical_index_path(settings.data_dir)
            hierarchy = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="public_portal-test-profile",
                vector_offsets=offsets,
            )
            _write_runtime_approval_snapshot_sidecar(
                settings.data_dir,
                records,
                tenant_id="tenant-a",
            )
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "profile_id": "public_portal-test-profile",
                    "hierarchical_index_status": "ready",
                    "files": {
                        "hierarchical_index_sha256": hierarchy["sha256"],
                    },
                }
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            routes_rag._RAG_APPROVAL_SNAPSHOT_CACHE.clear()

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "load_local_vector_records",
                    side_effect=AssertionError("full vector load must not run"),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "search_records",
                    side_effect=AssertionError("flat search must not run"),
                ),
                patch.object(
                    JsonRepository,
                    "list_documents",
                    side_effect=AssertionError(
                        "repository profile scan must not run"
                    ),
                ),
            ):
                response = search_regulations(
                    settings=settings,
                    auth=auth,
                    query=str(records[0]["text"]),
                    top_k=1,
                    profile_id=None,
                    security_levels=["internal"],
                )

        self.assertEqual(1, len(response["results"]))
        self.assertEqual(
            "public_portal-test-profile",
            response["metadata"]["profile_id"],
        )
        self.assertEqual(
            "catalog_toc_body",
            response["metadata"]["retrieval_strategy"],
        )

    def test_external_chunk_symlink_invalidates_sidecar_and_verified_search(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data")
            _prepare_mcp_indexed_document(settings)
            auth = mcp_auth_context(tenant_id="tenant-a")
            vector_path = routes_rag._local_vector_path(settings, auth)
            records = [
                json.loads(line)
                for line in vector_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = hierarchical_index_path(settings.data_dir)
            hierarchy = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="public_portal-test-profile",
                vector_offsets=offsets,
            )
            _write_runtime_approval_snapshot_sidecar(
                settings.data_dir,
                records,
                tenant_id="tenant-a",
            )
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "profile_id": "public_portal-test-profile",
                    "hierarchical_index_status": "ready",
                    "files": {
                        "hierarchical_index_sha256": hierarchy["sha256"],
                    },
                }
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            chunk_path = (
                settings.data_dir
                / "repository"
                / "doc_mcp_chunks.json"
            )
            external_chunk_path = root / "external_doc_mcp_chunks.json"
            chunk_path.replace(external_chunk_path)
            simulate_symlink_metadata = False
            try:
                chunk_path.symlink_to(external_chunk_path)
            except (NotImplementedError, OSError):
                # Windows without Developer Mode cannot create file symlinks.
                # Restore the fixture and simulate only the link metadata so
                # the fail-closed search regression still executes there.
                external_chunk_path.replace(chunk_path)
                simulate_symlink_metadata = True
            original_lstat = Path.lstat

            def reports_external_chunk_symlink(candidate: Path) -> object:
                if simulate_symlink_metadata and candidate == chunk_path:
                    return SimpleNamespace(
                        st_mode=stat.S_IFLNK,
                        st_file_attributes=0,
                    )
                return original_lstat(candidate)

            with patch.object(
                Path,
                "lstat",
                reports_external_chunk_symlink,
            ):
                descriptor = regulation_tools.repository_path_descriptor(
                    settings
                )
                regulation_rag_runtime._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
                regulation_rag_runtime._RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.clear()

                self.assertIsNone(
                    regulation_rag_runtime.runtime_approval_snapshot_identity(
                        descriptor
                    )
                )
                self.assertIsNone(
                    regulation_rag_runtime.repository_chunk_files_signature(
                        descriptor
                    )
                )
                self.assertIsNone(
                    regulation_rag_runtime.load_runtime_approval_snapshot_sidecar(
                        descriptor,
                        ["doc_mcp"],
                        auth,
                    )
                )
                self.assertIsNone(
                    regulation_rag_runtime.load_cached_runtime_approval_snapshot(
                        descriptor,
                        ["doc_mcp"],
                        auth,
                    )
                )
                with patch.object(
                    regulation_tools.routes_rag,
                    "search_records",
                    side_effect=AssertionError(
                        "unsafe hierarchy authorization must not fall back open"
                    ),
                ):
                    response = search_regulations(
                        settings=settings,
                        auth=auth,
                        query=str(records[0]["text"]),
                        top_k=1,
                        profile_id="public_portal-test-profile",
                        security_levels=["internal"],
                    )

        self.assertEqual([], response["results"])

    def test_runtime_approval_chunk_paths_reject_escaped_and_non_file_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_root = data_dir / "repository"
            repository_root.mkdir(parents=True)
            escaped_path = data_dir / "escaped_chunks.json"
            escaped_path.write_text("[]", encoding="utf-8")
            (repository_root / "directory_chunks.json").mkdir()
            descriptor = SimpleNamespace(
                root=repository_root,
                manifest_path=repository_root / "manifest.json",
                legacy_path=data_dir / "repository.json",
            )

            escaped = (
                regulation_rag_runtime.runtime_approval_identity_chunk_paths(
                    descriptor,
                    document_ids=["../escaped"],
                )
            )
            non_file_scoped = (
                regulation_rag_runtime.runtime_approval_identity_chunk_paths(
                    descriptor,
                    document_ids=["directory"],
                )
            )
            non_file_unscoped = (
                regulation_rag_runtime.runtime_approval_identity_chunk_paths(
                    descriptor,
                    document_ids=None,
                )
            )

        self.assertIsNone(escaped)
        self.assertIsNone(non_file_scoped)
        self.assertIsNone(non_file_unscoped)

    def test_runtime_approval_identity_keeps_missing_scoped_chunk_sentinel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_root = data_dir / "repository"
            repository_root.mkdir(parents=True)
            (data_dir / "mcp_runtime_manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (repository_root / "approval_snapshot.json").write_text(
                "{}",
                encoding="utf-8",
            )
            descriptor = SimpleNamespace(
                root=repository_root,
                manifest_path=repository_root / "manifest.json",
                legacy_path=data_dir / "repository.json",
            )

            identity = (
                regulation_rag_runtime.runtime_approval_snapshot_identity(
                    descriptor,
                    document_ids=["missing-document"],
                )
            )

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(
            (
                (
                    "missing-document_chunks.json",
                    ("missing",),
                ),
            ),
            identity[5],
        )

    def test_runtime_approval_identity_reuses_normal_chunk_lstat(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_root = data_dir / "repository"
            repository_root.mkdir(parents=True)
            manifest_path = data_dir / "mcp_runtime_manifest.json"
            sidecar_path = repository_root / "approval_snapshot.json"
            chunk_path = repository_root / "doc_chunks.json"
            manifest_path.write_text("{}", encoding="utf-8")
            sidecar_path.write_text("{}", encoding="utf-8")
            chunk_path.write_text("[]", encoding="utf-8")
            descriptor = SimpleNamespace(
                root=repository_root,
                manifest_path=repository_root / "manifest.json",
                legacy_path=data_dir / "repository.json",
            )
            original_lstat = Path.lstat
            chunk_lstat_calls = 0

            def track_chunk_lstat(candidate: Path) -> object:
                nonlocal chunk_lstat_calls
                if candidate == chunk_path:
                    chunk_lstat_calls += 1
                return original_lstat(candidate)

            with (
                patch.object(Path, "lstat", track_chunk_lstat),
                patch.object(
                    regulation_rag_runtime,
                    "path_signature",
                    wraps=regulation_rag_runtime.path_signature,
                ) as path_signatures,
            ):
                identity = (
                    regulation_rag_runtime.runtime_approval_snapshot_identity(
                        descriptor,
                        document_ids=["doc"],
                    )
                )

        self.assertIsNotNone(identity)
        self.assertEqual(1, chunk_lstat_calls)
        self.assertNotIn(
            chunk_path,
            [
                call.args[0]
                for call in path_signatures.call_args_list
            ],
        )

    def test_same_size_rewrite_with_restored_mtime_invalidates_approval_caches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            repository_root = data_dir / "repository"
            repository_root.mkdir(parents=True)
            (data_dir / "mcp_runtime_manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (repository_root / "approval_snapshot.json").write_text(
                "{}",
                encoding="utf-8",
            )
            chunk_path = repository_root / "doc_chunks.json"
            chunk_path.write_bytes(b"AAAA")
            original_stat = chunk_path.stat()
            descriptor = SimpleNamespace(
                root=repository_root,
                manifest_path=repository_root / "manifest.json",
                legacy_path=data_dir / "repository.json",
            )
            auth = mcp_auth_context(tenant_id="tenant-a")
            snapshots = [
                {
                    ("doc", "chunk"): {
                        "content_hash": "before",
                    }
                },
                {
                    ("doc", "chunk"): {
                        "content_hash": "after",
                    }
                },
            ]
            sidecar_load_count = 0

            def load_current_snapshot(
                _repository: object,
                _document_ids: list[str],
                _auth: AuthContext,
            ) -> dict[tuple[str, str], dict[str, str]]:
                nonlocal sidecar_load_count
                snapshot = snapshots[sidecar_load_count]
                sidecar_load_count += 1
                return snapshot

            regulation_rag_runtime._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
            regulation_rag_runtime._RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.clear()
            regulation_rag_runtime._RUNTIME_CONTENT_SIGNATURE_CACHE.clear()
            before_signature = regulation_rag_runtime.path_signature(
                chunk_path
            )
            before_portable = (
                regulation_rag_runtime.portable_file_signature(chunk_path)
            )
            before_snapshot = (
                regulation_rag_runtime.load_cached_runtime_approval_snapshot(
                    descriptor,
                    ["doc"],
                    auth,
                    sidecar_loader=load_current_snapshot,
                )
            )

            time.sleep(0.01)
            chunk_path.write_bytes(b"BBBB")
            os.utime(
                chunk_path,
                ns=(
                    original_stat.st_atime_ns,
                    original_stat.st_mtime_ns,
                ),
            )

            after_signature = regulation_rag_runtime.path_signature(
                chunk_path
            )
            after_portable = (
                regulation_rag_runtime.portable_file_signature(chunk_path)
            )
            after_snapshot = (
                regulation_rag_runtime.load_cached_runtime_approval_snapshot(
                    descriptor,
                    ["doc"],
                    auth,
                    sidecar_loader=load_current_snapshot,
                )
            )
            regulation_rag_runtime._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
            regulation_rag_runtime._RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.clear()
            regulation_rag_runtime._RUNTIME_CONTENT_SIGNATURE_CACHE.clear()

        self.assertIsNotNone(before_signature)
        self.assertIsNotNone(after_signature)
        assert before_signature is not None
        assert after_signature is not None
        self.assertEqual(before_signature[:2], after_signature[:2])
        self.assertNotEqual(before_signature, after_signature)
        self.assertIsNotNone(before_portable)
        self.assertIsNotNone(after_portable)
        assert before_portable is not None
        assert after_portable is not None
        self.assertEqual(before_portable[0], after_portable[0])
        self.assertNotEqual(before_portable[1], after_portable[1])
        self.assertEqual(snapshots[0], before_snapshot)
        self.assertEqual(snapshots[1], after_snapshot)
        self.assertEqual(2, sidecar_load_count)

    @unittest.skipUnless(os.name == "nt", "Windows ChangeTime is required")
    def test_windows_change_time_failure_fails_closed_for_chunk_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            chunk_path = repository_root / "doc_chunks.json"
            chunk_path.write_text("[]", encoding="utf-8")
            descriptor = SimpleNamespace(
                root=repository_root,
                manifest_path=repository_root / "manifest.json",
                legacy_path=repository_root.parent / "repository.json",
            )

            with patch.object(
                regulation_rag_runtime,
                "_windows_file_change_time_ns",
                return_value=None,
            ):
                signature = regulation_rag_runtime.path_signature(chunk_path)
                chunk_paths = (
                    regulation_rag_runtime.runtime_approval_identity_chunk_paths(
                        descriptor,
                        document_ids=["doc"],
                    )
                )

        self.assertIsNone(signature)
        self.assertIsNone(chunk_paths)

    def test_runtime_approval_chunk_paths_reject_reparse_file_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repository"
            repository_root.mkdir()
            chunk_path = repository_root / "doc_chunks.json"
            chunk_path.write_text("[]", encoding="utf-8")
            descriptor = SimpleNamespace(
                root=repository_root,
                manifest_path=repository_root / "manifest.json",
                legacy_path=repository_root.parent / "repository.json",
            )
            original_lstat = Path.lstat

            def reports_reparse_file(candidate: Path) -> object:
                if candidate == chunk_path:
                    return SimpleNamespace(
                        st_mode=stat.S_IFREG,
                        st_file_attributes=0x400,
                    )
                return original_lstat(candidate)

            with patch.object(Path, "lstat", reports_reparse_file):
                chunk_paths = (
                    regulation_rag_runtime.runtime_approval_identity_chunk_paths(
                        descriptor,
                        document_ids=["doc"],
                    )
                )

        self.assertIsNone(chunk_paths)

    def test_fresh_hierarchy_search_avoids_repository_and_fastapi_routes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            auth = mcp_auth_context(tenant_id="tenant-a")
            vector_path = routes_rag._local_vector_path(settings, auth)
            records = [
                json.loads(line)
                for line in vector_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = hierarchical_index_path(settings.data_dir)
            hierarchy = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="public_portal-test-profile",
                vector_offsets=offsets,
            )
            _write_runtime_approval_snapshot_sidecar(
                settings.data_dir,
                records,
                tenant_id="tenant-a",
            )
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "profile_id": "public_portal-test-profile",
                    "hierarchical_index_status": "ready",
                    "files": {
                        "hierarchical_index_sha256": hierarchy["sha256"],
                    },
                }
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            child_source = """
import json
from pathlib import Path
import sys
from app.core.config import Settings
from app.mcp_server.regulation_tools import (
    mcp_auth_context,
    search_regulations,
)
from app.services.regulation_rag_service import local_vector_path
from app.services import regulation_rag_service

settings = Settings(data_dir=Path(sys.argv[1]), api_audit_enabled=False)
auth = mcp_auth_context(tenant_id="tenant-a")
def reject_route_import():
    raise AssertionError("hierarchy search attempted to import routes_rag")
regulation_rag_service._load_routes_rag = reject_route_import
class RejectRoutesRagImport:
    def find_spec(self, fullname, path, target=None):
        if fullname in {"app.api.routes_rag", "app.storage.repository"}:
            raise AssertionError(f"forbidden cold-path import: {fullname}")
        return None
sys.meta_path.insert(0, RejectRoutesRagImport())
vector_path = local_vector_path(settings, auth)
first_record = json.loads(
    next(
        line
        for line in vector_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
)
repository_root = settings.data_dir / "repository"
repository_state_before = {
    str(path.relative_to(repository_root)): (
        path.stat().st_mtime_ns,
        path.stat().st_size,
    )
    for path in repository_root.rglob("*")
    if path.is_file()
}
response = search_regulations(
    settings=settings,
    auth=auth,
    query=str(first_record["text"]),
    top_k=1,
    security_levels=["internal"],
)
repository_state_after = {
    str(path.relative_to(repository_root)): (
        path.stat().st_mtime_ns,
        path.stat().st_size,
    )
    for path in repository_root.rglob("*")
    if path.is_file()
}
print(json.dumps({
    "result_count": len(response["results"]),
    "retrieval_strategy": response["metadata"]["retrieval_strategy"],
    "routes_rag_loaded": "app.api.routes_rag" in sys.modules,
    "repository_loaded": "app.storage.repository" in sys.modules,
    "repository_state_unchanged": repository_state_before == repository_state_after,
}))
"""
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child_source,
                    str(settings.data_dir),
                ],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

        child_result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(1, child_result["result_count"])
        self.assertEqual(
            "catalog_toc_body",
            child_result["retrieval_strategy"],
        )
        self.assertFalse(child_result["routes_rag_loaded"])
        self.assertFalse(child_result["repository_loaded"])
        self.assertTrue(child_result["repository_state_unchanged"])

    def test_cached_hierarchy_verifier_rejects_index_replaced_before_return(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            auth = mcp_auth_context(tenant_id="tenant-a")
            vector_path = routes_rag._local_vector_path(settings, auth)
            records = [
                json.loads(line)
                for line in vector_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            offsets = write_vector_records_with_offsets(vector_path, records)
            index_path = hierarchical_index_path(settings.data_dir)
            hierarchy = build_hierarchical_runtime_index(
                index_path,
                records,
                tenant_id="tenant-a",
                profile_id="public_portal-test-profile",
                vector_offsets=offsets,
            )
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest = {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": "tenant-a",
                "profile_id": "public_portal-test-profile",
                "files": {
                    "hierarchical_index_sha256": hierarchy["sha256"],
                },
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            first = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="public_portal-test-profile",
            )
            original_signature = regulation_tools.routes_rag.path_signature
            index_signature_calls = 0

            def replace_before_cached_return(path: Path):
                nonlocal index_signature_calls
                if Path(path) == index_path:
                    index_signature_calls += 1
                    if index_signature_calls == 2:
                        index_path.write_bytes(b"replaced-index")
                return original_signature(path)

            with patch.object(
                regulation_tools.routes_rag,
                "path_signature",
                side_effect=replace_before_cached_return,
            ):
                second = (
                    regulation_tools._verified_hierarchical_runtime_paths(
                        settings=settings,
                        auth=auth,
                        profile_id="public_portal-test-profile",
                    )
                )

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_get_regulation_article_rejects_mid_request_approval_change(
        self,
    ) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        settings = Settings(
            data_dir=Path(temporary_directory.name) / "data",
            api_audit_enabled=False,
        )
        auth = mcp_auth_context(tenant_id="tenant-a")
        paths = (
            Path("synthetic-index.sqlite3"),
            Path("synthetic-vectors.jsonl"),
        )
        token = SimpleNamespace(is_current=lambda: True)
        record = {
            "document_id": "doc-a",
            "chunk_id": "chunk-a",
            "text": "approved article",
            "content_hash": "content-hash-a",
            "metadata": {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "approval_status": "approved",
                "approval_id": "approval-a",
                "approved_content_hash": "approved-hash-a",
                "security_level": "internal",
                "regulation_unit_id": "unit-a",
                "article_no": "1",
                "article_title": "Article",
            },
        }
        with (
            patch.object(
                regulation_tools,
                "_resolve_mcp_profile_scope",
                return_value=None,
            ),
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_paths",
                return_value=paths,
            ),
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_token",
                return_value=token,
            ),
            patch.object(
                regulation_tools,
                "_hierarchical_authorization_source_identity",
                side_effect=[
                    ("runtime_sidecar", "before"),
                    ("runtime_sidecar", "after"),
                ],
            ),
            patch.object(
                regulation_tools,
                "_fully_visible_regulation_units",
                return_value={"unit-a"},
            ),
            patch.object(
                regulation_tools,
                "load_hierarchical_article_records",
                return_value=[record],
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "authorization source changed",
            ):
                regulation_tools.get_regulation_article(
                    settings=settings,
                    auth=auth,
                    regulation_unit_id="unit-a",
                    article_no="1",
                    security_levels=["internal"],
                )

    def test_get_regulation_article_rejects_paths_without_verification_token(
        self,
    ) -> None:
        with (
            patch.object(
                regulation_tools,
                "_resolve_mcp_profile_scope",
                return_value=None,
            ),
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_paths",
                return_value=(
                    Path("index.sqlite3"),
                    Path("vector.jsonl"),
                ),
            ),
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_token",
                return_value=None,
            ),
            patch.object(
                regulation_tools,
                "_fully_visible_regulation_units",
                side_effect=AssertionError(
                    "unverified article index must not be authorized"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "index verification changed",
            ):
                regulation_tools.get_regulation_article(
                    settings=Settings(
                        data_dir=Path("synthetic-runtime"),
                        api_audit_enabled=False,
                    ),
                    auth=mcp_auth_context(tenant_id="tenant-a"),
                    regulation_unit_id="unit-a",
                    article_no="1",
                    security_levels=["internal"],
                )

    def test_hierarchy_search_fails_closed_when_runtime_files_change_mid_query(self) -> None:
        query = SimpleNamespace(
            profile_id="profile-a",
            query="test query",
            top_k=1,
            document_id=None,
            as_of_date=None,
            security_levels=["internal"],
            department_ids=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            token_is_current_calls = 0

            def final_token_is_stale() -> bool:
                nonlocal token_is_current_calls
                token_is_current_calls += 1
                return False

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token_for_scope",
                    return_value=SimpleNamespace(
                        index_path=Path("index.sqlite3"),
                        vector_path=Path("vector.jsonl"),
                        index_identity=("index-stable",),
                        vector_identity=("vector-stable",),
                        is_current=final_token_is_stale,
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_bm25",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-stable",),
                ) as approval_identity,
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value={"unit-a"},
                ),
                patch.object(
                    regulation_tools,
                    "search_hierarchical_records",
                    return_value=([], {"retrieval_model": "hier", "retrieval_strategy": "catalog_toc_body"}),
                ),
            ):
                result = regulation_tools._search_hierarchical_runtime(
                    settings=settings,
                    auth=auth,
                    query=query,
                )

        self.assertIsNone(result)
        self.assertEqual(1, token_is_current_calls)
        self.assertEqual(2, approval_identity.call_count)

    def test_hierarchy_search_rejects_paths_without_verification_token(
        self,
    ) -> None:
        query = SimpleNamespace(
            profile_id=None,
            query="test query",
            top_k=1,
            document_id=None,
            as_of_date=None,
            security_levels=["internal"],
            department_ids=[],
        )
        with (
            patch.object(
                regulation_tools,
                "_verified_hierarchical_runtime_token_for_scope",
                return_value=None,
            ),
            patch.object(
                regulation_tools,
                "search_hierarchical_records",
                side_effect=AssertionError(
                    "unverified hierarchy must not be searched"
                ),
            ),
        ):
            result = regulation_tools._search_hierarchical_runtime(
                settings=Settings(data_dir=Path("synthetic-runtime")),
                auth=mcp_auth_context(tenant_id="tenant-a"),
                query=query,
            )

        self.assertIsNone(result)

    def test_hierarchy_search_reuses_prevalidated_visibility_identity(self) -> None:
        query = SimpleNamespace(
            profile_id="profile-a",
            query="test query",
            top_k=1,
            document_id=None,
            as_of_date=None,
            security_levels=["internal"],
            department_ids=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token_for_scope",
                    return_value=SimpleNamespace(
                        index_path=Path("index.sqlite3"),
                        vector_path=Path("vector.jsonl"),
                        index_identity=("index-stable",),
                        vector_identity=("vector-stable",),
                        is_current=lambda: True,
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_bm25",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    side_effect=[
                        ("approval-stable",),
                        ("approval-stable",),
                    ],
                ) as approval_identity,
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value={"unit-a"},
                ) as visible_units,
                patch.object(
                    regulation_tools,
                    "search_hierarchical_records",
                    return_value=(
                        [],
                        {
                            "retrieval_model": "hier",
                            "retrieval_strategy": "catalog_toc_body",
                        },
                    ),
                ),
            ):
                result = regulation_tools._search_hierarchical_runtime(
                    settings=settings,
                    auth=auth,
                    query=query,
                )

        self.assertIsNotNone(result)
        self.assertEqual(2, approval_identity.call_count)
        self.assertEqual(
            ("index-stable",),
            visible_units.call_args.kwargs["prevalidated_index_signature"],
        )
        self.assertEqual(
            ("approval-stable",),
            visible_units.call_args.kwargs["prevalidated_source_identity"],
        )

    def test_search_read_context_checks_each_identity_twice_per_call(self) -> None:
        query = SimpleNamespace(
            profile_id="profile-a",
            query="approved policy",
            top_k=1,
            document_id=None,
            as_of_date=None,
            security_levels=["internal"],
            department_ids=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path, index_path, bm25_path = (
                _write_bound_hierarchy_bm25_fixture(
                    settings,
                    profile_id="profile-a",
                )
            )
            vector_path = regulation_tools.routes_rag.local_vector_path(
                settings,
                auth,
            )
            self.assertIsNotNone(
                regulation_tools._verified_hierarchical_runtime_paths(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                )
            )
            self.assertIsNotNone(
                regulation_tools._verified_hierarchical_runtime_bm25(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                )
            )
            original_path_signature = (
                regulation_tools.routes_rag.path_signature
            )

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    wraps=original_path_signature,
                ) as path_signature,
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-stable",),
                ) as approval_identity,
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value={"unit-a"},
                ),
                patch.object(
                    regulation_tools,
                    "search_hierarchical_records",
                    return_value=(
                        [],
                        {
                            "retrieval_model": "hier",
                            "retrieval_strategy": "catalog_toc_body",
                        },
                    ),
                ),
            ):
                first = regulation_tools._search_hierarchical_runtime(
                    settings=settings,
                    auth=auth,
                    query=query,
                )
                second = regulation_tools._search_hierarchical_runtime(
                    settings=settings,
                    auth=auth,
                    query=query,
                )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(4, approval_identity.call_count)
        self.assertEqual(
            Counter(
                {
                    manifest_path: 4,
                    index_path: 4,
                    vector_path: 4,
                    bm25_path: 4,
                }
            ),
            Counter(
                current.args[0]
                for current in path_signature.call_args_list
            ),
        )

    def test_search_flat_fallback_rejects_newly_ambiguous_profile_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            with (
                patch.object(
                    regulation_tools,
                    "_resolve_mcp_profile_scope_with_runtime_token",
                    return_value=(None, object()),
                ),
                patch.object(
                    regulation_tools,
                    "_search_hierarchical_runtime",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools,
                    "_mcp_profile_scope_ids",
                    return_value={"profile-a", "profile-b"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "search_records",
                ) as flat_search,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "profile_id is required",
                ):
                    regulation_tools.search_regulations(
                        settings=settings,
                        auth=auth,
                        query="approved policy",
                    )

        flat_search.assert_not_called()

    def test_search_flat_fallback_rejects_profile_change_during_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            with (
                patch.object(
                    regulation_tools,
                    "_resolve_mcp_profile_scope_with_runtime_token",
                    return_value=(None, object()),
                ),
                patch.object(
                    regulation_tools,
                    "_search_hierarchical_runtime",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools,
                    "_mcp_profile_scope_ids",
                    side_effect=[
                        {"profile-a"},
                        {"profile-a", "profile-b"},
                    ],
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "search_records",
                    return_value=([], {"trace_id": "flat-trace"}),
                ) as flat_search,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "profile_id is required",
                ):
                    regulation_tools.search_regulations(
                        settings=settings,
                        auth=auth,
                        query="approved policy",
                    )

        flat_search.assert_called_once()
        self.assertEqual(
            "profile-a",
            flat_search.call_args.kwargs["query"].profile_id,
        )

    def test_hierarchy_search_rejects_bm25_changed_after_scoring(self) -> None:
        query = SimpleNamespace(
            profile_id="profile-a",
            query="test query",
            top_k=1,
            document_id=None,
            as_of_date=None,
            security_levels=["internal"],
            department_ids=[],
        )
        token = SimpleNamespace(
            index_path=Path("index.sqlite3"),
            vector_path=Path("vector.jsonl"),
            index_identity=("index-stable",),
            vector_identity=("vector-stable",),
            is_current=lambda: True,
        )
        bm25_index = object()
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            bm25_path = regulation_tools.routes_rag.bm25_index_path(
                settings=settings,
                auth=auth,
            )
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token_for_scope",
                    return_value=token,
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_bm25",
                    return_value=(
                        bm25_index,
                        bm25_path,
                        ("bm25-before",),
                    ),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-stable",),
                ) as approval_identity,
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    side_effect=[
                        ("bm25-before",),
                        ("bm25-after",),
                    ],
                ) as path_signature,
                patch.object(
                    regulation_tools,
                    "_fully_visible_regulation_units",
                    return_value={"unit-a"},
                ),
                patch.object(
                    regulation_tools,
                    "search_hierarchical_records",
                    return_value=(
                        [],
                        {
                            "retrieval_model": "hier",
                            "retrieval_strategy": "catalog_toc_body",
                        },
                    ),
                ) as search_records,
            ):
                result = regulation_tools._search_hierarchical_runtime(
                    settings=settings,
                    auth=auth,
                    query=query,
                )

        self.assertIsNone(result)
        self.assertIs(
            bm25_index,
            search_records.call_args.kwargs["rerank_index"],
        )
        self.assertEqual(2, approval_identity.call_count)
        self.assertEqual(
            [call(bm25_path), call(bm25_path)],
            path_signature.call_args_list,
        )

    def test_verified_runtime_profile_requires_matching_stable_manifest_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_data_bundle",
                        "tenant_id": "tenant-a",
                        "profile_id": "profile-a",
                    }
                ),
                encoding="utf-8",
            )
            index_path = hierarchical_index_path(settings.data_dir)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_bytes(b"synthetic-index")
            summary = {
                "schema_version": regulation_tools.HIERARCHICAL_INDEX_SCHEMA_VERSION,
                "tenant_id": "tenant-a",
                "profile_id": "profile-a",
            }

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, Path("synthetic-vector")),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value=summary,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("stable",),
                ),
            ):
                resolved = (
                    regulation_tools._verified_hierarchical_runtime_profile_id(
                        settings=settings,
                        auth=auth,
                    )
                )

            regulation_tools._HIERARCHICAL_PROFILE_VERIFICATION_CACHE.clear()
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, Path("synthetic-vector")),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value={**summary, "profile_id": "profile-b"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("stable",),
                ),
            ):
                mismatched = (
                    regulation_tools._verified_hierarchical_runtime_profile_id(
                        settings=settings,
                        auth=auth,
                    )
                )

            regulation_tools._HIERARCHICAL_PROFILE_VERIFICATION_CACHE.clear()
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, Path("synthetic-vector")),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value=summary,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    side_effect=[
                        ("manifest-before",),
                        ("index-stable",),
                        ("vector-stable",),
                        ("manifest-after",),
                        ("index-stable",),
                        ("vector-stable",),
                    ],
                ),
            ):
                unstable = (
                    regulation_tools._verified_hierarchical_runtime_profile_id(
                        settings=settings,
                        auth=auth,
                    )
                )

        self.assertEqual("profile-a", resolved)
        self.assertIsNone(mismatched)
        self.assertIsNone(unstable)

    def test_verified_runtime_profile_reuses_stable_positive_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_data_bundle",
                        "tenant_id": "tenant-a",
                        "profile_id": "profile-a",
                    }
                ),
                encoding="utf-8",
            )
            index_path = hierarchical_index_path(settings.data_dir)
            vector_path = routes_rag._local_vector_path(settings, auth)
            summary = {
                "schema_version": regulation_tools.HIERARCHICAL_INDEX_SCHEMA_VERSION,
                "tenant_id": "tenant-a",
                "profile_id": "profile-a",
            }

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, vector_path),
                ) as verified_paths,
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value=summary,
                ) as load_summary,
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("stable",),
                ),
            ):
                first = (
                    regulation_tools._verified_hierarchical_runtime_profile_id(
                        settings=settings,
                        auth=auth,
                    )
                )
                second = (
                    regulation_tools._verified_hierarchical_runtime_profile_id(
                        settings=settings,
                        auth=auth,
                    )
                )

        self.assertEqual("profile-a", first)
        self.assertEqual(first, second)
        verified_paths.assert_called_once()
        load_summary.assert_called_once()

    def test_list_regulations_uses_verified_runtime_profile_without_scope_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_profile_id",
                    return_value="profile-a",
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=None,
                ) as runtime_paths,
                patch.object(
                    JsonRepository,
                    "list_documents",
                    side_effect=AssertionError("repository scope scan must not run"),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_local_vector_records",
                    side_effect=AssertionError("vector scope scan must not run"),
                ),
            ):
                result = list_regulations(
                    settings=settings,
                    auth=auth,
                    profile_id=None,
                )

        self.assertFalse(result["metadata"]["hierarchical_index_ready"])
        self.assertEqual("profile-a", runtime_paths.call_args.kwargs["profile_id"])

    def test_visible_records_uses_verified_runtime_profile_without_scope_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_profile_id",
                    return_value="profile-a",
                ),
                patch.object(
                    JsonRepository,
                    "list_documents",
                    side_effect=AssertionError("repository scope scan must not run"),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_local_vector_records",
                    side_effect=AssertionError("vector scope scan must not run"),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "get_visible_records",
                    return_value=[],
                ) as get_visible_records,
            ):
                result = regulation_tools._visible_records(
                    settings=settings,
                    auth=auth,
                    profile_id=None,
                    security_levels=["internal"],
                )

        self.assertEqual([], result)
        query_request = get_visible_records.call_args.kwargs["query"]
        self.assertEqual("profile-a", query_request.profile_id)

    def test_hierarchy_verification_cache_is_scoped_to_manifest_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            index_path = hierarchical_index_path(settings.data_dir)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_bytes(b"stable-hierarchy-index")
            vector_path = routes_rag._local_vector_path(settings, auth)
            vector_path.parent.mkdir(parents=True, exist_ok=True)
            vector_path.write_text("", encoding="utf-8")
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest = {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": "tenant-a",
                "profile_id": "profile-a",
                "files": {
                    "hierarchical_index_sha256": hashlib.sha256(
                        index_path.read_bytes()
                    ).hexdigest(),
                },
            }
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            summary = {
                "schema_version": regulation_tools.HIERARCHICAL_INDEX_SCHEMA_VERSION,
                "tenant_id": "tenant-a",
                "profile_id": "profile-a",
            }

            with patch.object(
                regulation_tools,
                "hierarchical_index_summary",
                return_value=summary,
            ):
                accepted = regulation_tools._verified_hierarchical_runtime_paths(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                )
                manifest["profile_id"] = "profile-b"
                manifest_path.write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )
                rejected = regulation_tools._verified_hierarchical_runtime_paths(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-b",
                )

        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)

    def test_verified_hierarchy_warm_token_skips_manifest_and_summary_reads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            first = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )

            with (
                patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError(
                        "warm verification must not reread the manifest"
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    side_effect=AssertionError(
                        "warm verification must not reopen SQLite"
                    ),
                ),
            ):
                second = (
                    regulation_tools._verified_hierarchical_runtime_paths(
                        settings=settings,
                        auth=auth,
                        profile_id="profile-a",
                    )
                )

        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_search_scope_token_uses_three_fresh_identities_before_final_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            paths = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            self.assertIsNotNone(paths)
            token = regulation_tools._verified_hierarchical_runtime_token(
                paths,
            )
            self.assertIsNotNone(token)
            original_path_signature = (
                regulation_tools.routes_rag.path_signature
            )

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    wraps=original_path_signature,
                ) as path_signature,
                patch.object(
                    regulation_tools._VerifiedHierarchicalRuntimeToken,
                    "is_current",
                    side_effect=AssertionError(
                        "the search-only warm helper defers the final token check"
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    side_effect=AssertionError(
                        "matching warm identities must not rerun path verification"
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token",
                    side_effect=AssertionError(
                        "matching warm identities must reuse the cached token"
                    ),
                ),
            ):
                reused = (
                    regulation_tools._verified_hierarchical_runtime_token_for_scope(
                        settings=settings,
                        auth=auth,
                        profile_id="profile-a",
                    )
                )

        self.assertIs(token, reused)
        self.assertEqual(3, path_signature.call_count)

    def test_verified_hierarchy_rejects_replaced_manifest_from_warm_token(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path, _, _ = _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            first = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["hierarchical_index_sha256"] = "f" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            replaced = (
                regulation_tools._verified_hierarchical_runtime_paths(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                )
            )

        self.assertIsNotNone(first)
        self.assertIsNone(replaced)

    def test_verified_hierarchy_warm_token_is_scope_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            first = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            vector_path = routes_rag._local_vector_path(settings, auth)

            wrong_profile = (
                regulation_tools._verified_hierarchical_runtime_paths(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-b",
                )
            )
            with patch.object(
                regulation_tools.routes_rag,
                "local_vector_path",
                return_value=vector_path,
            ):
                wrong_tenant = (
                    regulation_tools._verified_hierarchical_runtime_paths(
                        settings=settings,
                        auth=mcp_auth_context(tenant_id="tenant-b"),
                        profile_id="profile-a",
                    )
                )

        self.assertIsNotNone(first)
        self.assertIsNone(wrong_profile)
        self.assertIsNone(wrong_tenant)

    def test_hierarchy_bm25_warm_token_reuses_manifest_and_summary_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            first = regulation_tools._verified_hierarchical_runtime_bm25(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )

            with (
                patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError(
                        "warm BM25 verification must not reread the manifest"
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    side_effect=AssertionError(
                        "warm BM25 verification must reuse the corpus binding"
                    ),
                ),
            ):
                second = (
                    regulation_tools._verified_hierarchical_runtime_bm25(
                        settings=settings,
                        auth=auth,
                        profile_id="profile-a",
                    )
                )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first[1:], second[1:])

    def test_hierarchy_bm25_reuses_injected_paths_and_checks_token_pre_post(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            paths = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            self.assertIsNotNone(paths)
            token = regulation_tools._verified_hierarchical_runtime_token(
                paths,
            )
            self.assertIsNotNone(token)

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    side_effect=AssertionError(
                        "injected verified paths must be reused"
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token",
                    side_effect=AssertionError(
                        "the injected verified token must be reused"
                    ),
                ),
                patch.object(
                    regulation_tools._VerifiedHierarchicalRuntimeToken,
                    "is_current",
                    side_effect=[True, True],
                ) as token_is_current,
            ):
                verified = regulation_tools._verified_hierarchical_runtime_bm25(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    hierarchy_paths=paths,
                    runtime_token=token,
                )

        self.assertIsNotNone(verified)
        self.assertEqual(2, token_is_current.call_count)

    def test_hierarchy_bm25_rejects_final_runtime_token_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            paths = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            self.assertIsNotNone(paths)
            token = regulation_tools._verified_hierarchical_runtime_token(
                paths,
            )
            self.assertIsNotNone(token)

            with patch.object(
                regulation_tools._VerifiedHierarchicalRuntimeToken,
                "is_current",
                side_effect=[True, False],
            ) as token_is_current:
                rejected = regulation_tools._verified_hierarchical_runtime_bm25(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                    hierarchy_paths=paths,
                    runtime_token=token,
                )

        self.assertIsNone(rejected)
        self.assertEqual(2, token_is_current.call_count)

    def test_hierarchy_bm25_rejects_injected_scope_and_path_mismatches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            paths = regulation_tools._verified_hierarchical_runtime_paths(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            self.assertIsNotNone(paths)
            token = regulation_tools._verified_hierarchical_runtime_token(
                paths,
            )
            self.assertIsNotNone(token)
            mismatches = (
                (
                    paths,
                    replace(token, tenant_id="tenant-b"),
                ),
                (
                    paths,
                    replace(token, profile_id="profile-b"),
                ),
                (
                    (Path("other-index.sqlite3"), paths[1]),
                    token,
                ),
                (
                    paths,
                    replace(
                        token,
                        manifest_path=Path("other-manifest.json"),
                    ),
                ),
            )

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    side_effect=AssertionError(
                        "invalid injected values must fail closed"
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_token",
                    side_effect=AssertionError(
                        "invalid injected values must not select another token"
                    ),
                ),
            ):
                rejected = [
                    regulation_tools._verified_hierarchical_runtime_bm25(
                        settings=settings,
                        auth=auth,
                        profile_id="profile-a",
                        hierarchy_paths=injected_paths,
                        runtime_token=injected_token,
                    )
                    for injected_paths, injected_token in mismatches
                ]

        self.assertEqual([None] * len(mismatches), rejected)

    def test_hierarchy_candidate_reranker_requires_manifest_pinned_bm25(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            _, _, bm25_path = _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )

            accepted = regulation_tools._verified_hierarchical_runtime_bm25(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )
            bm25_path.write_text(
                bm25_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            tampered = regulation_tools._verified_hierarchical_runtime_bm25(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )

        self.assertIsNotNone(accepted)
        self.assertIsNone(tampered)

    def test_hierarchy_candidate_reranker_rejects_same_count_stale_source_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path, _, bm25_path = _write_bound_hierarchy_bm25_fixture(
                settings,
                profile_id="profile-a",
            )
            bm25_payload = json.loads(bm25_path.read_text(encoding="utf-8"))
            bm25_payload["source_content_hashes"] = "f" * 64
            bm25_path.write_text(
                json.dumps(bm25_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            relative_path = bm25_path.relative_to(settings.data_dir).as_posix()
            manifest["runtime_data_reuse"]["file_sha256"][relative_path] = (
                hashlib.sha256(bm25_path.read_bytes()).hexdigest()
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            rejected = regulation_tools._verified_hierarchical_runtime_bm25(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )

        self.assertIsNone(rejected)

    def test_hierarchy_candidate_reranker_rejects_legacy_missing_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path, hierarchy_path, _ = (
                _write_bound_hierarchy_bm25_fixture(
                    settings,
                    profile_id="profile-a",
                )
            )
            connection = sqlite3.connect(hierarchy_path)
            try:
                connection.execute(
                    "DELETE FROM index_metadata WHERE key='source_content_hashes'"
                )
                connection.commit()
            finally:
                connection.close()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["hierarchical_index_sha256"] = hashlib.sha256(
                hierarchy_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            rejected = regulation_tools._verified_hierarchical_runtime_bm25(
                settings=settings,
                auth=auth,
                profile_id="profile-a",
            )

        self.assertIsNone(rejected)

    def test_hierarchy_candidate_reranker_rejects_scope_or_count_mismatch(self) -> None:
        settings = Settings(data_dir=Path("synthetic-data"))
        auth = mcp_auth_context(tenant_id="tenant-a")
        manifest = {
            "report_type": "mcp_runtime_data_bundle",
            "tenant_id": "tenant-a",
            "profile_id": "profile-a",
            "record_count": 2,
            "runtime_data_reuse": {
                "file_sha256": {
                    "vector_db/tenant-a/bm25_index.json": "a" * 64,
                }
            },
        }
        index = Bm25Index(
            index_version=BM25_INDEX_VERSION,
            structured_metadata_version=BM25_STRUCTURED_METADATA_VERSION,
            generated_at="2026-07-30T00:00:00+00:00",
            tokenizer=FALLBACK_TOKENIZER_MODEL,
            k1=1.5,
            b=0.75,
            source_content_hashes="b" * 64,
            document_count=1,
            average_document_length=1.0,
            document_frequencies={"policy": 1},
            documents=[],
        )
        with (
            patch.object(
                Path,
                "read_text",
                return_value=json.dumps(manifest),
            ),
            patch.object(
                regulation_tools.routes_rag,
                "bm25_index_path",
                return_value=(
                    settings.data_dir
                    / "vector_db"
                    / "tenant-a"
                    / "bm25_index.json"
                ),
            ),
            patch.object(
                regulation_tools.routes_rag,
                "path_signature",
                return_value=("stable",),
            ),
            patch.object(
                regulation_tools,
                "_file_sha256",
                return_value="a" * 64,
            ),
            patch.object(
                regulation_tools.routes_rag,
                "load_cached_bm25_index",
                return_value=index,
            ),
        ):
            wrong_profile = (
                regulation_tools._verified_hierarchical_runtime_bm25(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-b",
                )
            )
            wrong_count = (
                regulation_tools._verified_hierarchical_runtime_bm25(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                )
            )

        self.assertIsNone(wrong_profile)
        self.assertIsNone(wrong_count)

    def test_mcp_citation_uses_governing_article_for_form_chunk_without_rewriting_chunk_identity(self) -> None:
        form_result = {
            "document_id": "doc-forms",
            "chunk_id": "form-15",
            "chunk_type": "form",
            "regulation_title": "근태 관리",
            "article_no": "",
            "article_title": "",
            "governing_article_no": "제31조",
            "governing_article_title": "휴직의 운영",
            "governing_article_chunk_id": "article-31",
            "governing_article_match_ref": "별지제15호서식",
            "form_refs": ["별지제15호서식"],
            "text": "[별지 제15호서식] 휴직자 복무상황 보고서",
        }

        search_result = search_regulations.__globals__["_mcp_search_result"](form_result)
        fetch_result = search_regulations.__globals__["_mcp_fetch_result"](form_result)

        self.assertEqual(search_result["metadata"]["article_no"], "제31조")
        self.assertEqual(search_result["metadata"]["article_title"], "휴직의 운영")
        self.assertEqual(search_result["metadata"]["direct_article_no"], "")
        self.assertEqual(search_result["metadata"]["chunk_id"], "form-15")
        self.assertEqual(search_result["metadata"]["chunk_type"], "form")
        self.assertEqual(fetch_result["metadata"]["governing_article_chunk_id"], "article-31")
        self.assertIn("별지제15호서식", fetch_result["metadata"]["form_refs"])

    def test_search_metadata_is_lean_and_fetch_keeps_full_detail(self) -> None:
        result = {
            "document_id": "doc-detail",
            "chunk_id": "chunk-detail",
            "document_name": "Detail Rules",
            "source_url": "https://example.test/detail",
            "article_no": "A10",
            "article_title": "Detail",
            "answer_keywords": ["keyword-a", "keyword-b"],
            "answer_facts": [{"type": "duration", "value": "3 years", "sentence": "The period is 3 years."}],
            "answer_outline": ["Detailed answer candidate"],
            "reference_edges": [{"source": "a", "target": "b"}],
            "source_hwpx_nested_table_text_snippets": ["nested detail"],
            "source_hwp_streams": ["BodyText/Section0"],
            "table_source": "kordoc",
            "table_geometry_source": "kordoc",
            "primary_parser_table_source": "hwp_parser",
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
            "kordoc_table_promoted": True,
            "kordoc_table_promotion_review_required": True,
            "kordoc_table_unmatched_source": False,
            "kordoc_table_match": {"table_id": "kordoc-1", "score": 0.9},
            "kordoc_elapsed_ms": 12.5,
            "kordoc_input_extension": ".hwp",
            "kordoc_timeout_seconds": 120,
            "kordoc_table_inventory": {"tables": [{"title": "internal"}]},
            "parser_uncertainty_remediation_hint": "review original",
        }

        search_result = search_regulations.__globals__["_mcp_search_result"](result)
        fetch_result = search_regulations.__globals__["_mcp_fetch_result"](result)

        self.assertEqual(search_result["metadata"]["source_url"], "https://example.test/detail")
        self.assertEqual(search_result["metadata"]["article_no"], "A10")
        self.assertNotIn("answer_facts", search_result["metadata"])
        self.assertNotIn("answer_outline", search_result["metadata"])
        self.assertNotIn("answer_keywords", search_result["metadata"])
        self.assertNotIn("reference_edges", search_result["metadata"])
        self.assertNotIn("source_hwpx_nested_table_text_snippets", search_result["metadata"])
        self.assertNotIn("source_hwp_streams", search_result["metadata"])
        self.assertNotIn("kordoc_table_match", search_result["metadata"])
        self.assertNotIn("parser_uncertainty_remediation_hint", search_result["metadata"])
        for metadata in (search_result["metadata"], fetch_result["metadata"]):
            self.assertNotIn("kordoc_elapsed_ms", metadata)
            self.assertNotIn("kordoc_input_extension", metadata)
            self.assertNotIn("kordoc_timeout_seconds", metadata)
            self.assertNotIn("kordoc_table_inventory", metadata)
        self.assertEqual(search_result["metadata"]["table_source"], "kordoc")
        self.assertEqual(search_result["metadata"]["table_geometry_source"], "kordoc")
        self.assertEqual(search_result["metadata"]["primary_parser_table_source"], "hwp_parser")
        self.assertEqual(search_result["metadata"]["kordoc_table_parser_status"], "parsed")
        self.assertEqual(search_result["metadata"]["kordoc_table_count"], 1)
        self.assertTrue(search_result["metadata"]["kordoc_table_promoted"])
        self.assertTrue(search_result["metadata"]["kordoc_table_promotion_review_required"])
        self.assertFalse(search_result["metadata"]["kordoc_table_unmatched_source"])
        self.assertEqual(fetch_result["metadata"]["answer_facts"][0]["value"], "3 years")
        self.assertEqual(fetch_result["metadata"]["answer_keywords"], ["keyword-a", "keyword-b"])
        self.assertEqual(fetch_result["metadata"]["kordoc_table_match"]["table_id"], "kordoc-1")

    def test_search_and_fetch_return_only_approved_local_regulation_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )
            docs = list_documents(settings=settings, auth=mcp_auth, security_levels=["internal"])

        self.assertEqual(auth.tenant_id, "tenant-a")
        self.assertEqual(len(search["results"]), 1)
        self.assertIn("timing_ms", search["metadata"])
        self.assertEqual(
            "latest_active_version_per_regulation",
            search["metadata"]["lifecycle_selection"]["mode"],
        )
        self.assertIn("complete_lifecycle_record_count", search["metadata"]["lifecycle_selection"])
        self.assertGreaterEqual(search["metadata"]["timing_ms"]["scoring_elapsed_ms"], 0.0)
        self.assertEqual(search["results"][0]["metadata"]["chunk_id"], "approved-1")
        self.assertTrue(search["results"][0]["metadata"]["content_hash"])
        self.assertTrue(search["results"][0]["metadata"]["approved_content_hash"])
        self.assertEqual(len(search["results"][0]["metadata"]["approval_worklist_report_sha256"]), 64)
        self.assertEqual(search["results"][0]["metadata"]["approval_review_batch_manifest_path"], "reports/approval_review_batches_current.json")
        self.assertEqual(len(search["results"][0]["metadata"]["approval_review_batch_manifest_sha256"]), 64)
        self.assertTrue(search["results"][0]["metadata"]["approval_review_batch_id"].startswith("approval-"))
        self.assertEqual(len(search["results"][0]["metadata"]["approval_review_batch_chunk_fingerprint"]), 64)
        self.assertEqual(search["results"][0]["metadata"]["approval_review_strategy"], "human_bulk_review")
        self.assertEqual(search["results"][0]["metadata"]["parser_uncertainty_source"], "hwp")
        self.assertEqual(search["results"][0]["metadata"]["parser_uncertainty_risk_level"], "medium")
        self.assertEqual(
            search["results"][0]["metadata"]["parser_uncertainty_flags"],
            ["native_table_geometry_unavailable"],
        )
        self.assertNotIn("draft-1", search["results"][0]["text"])
        self.assertEqual(search["results"][0]["verbatim_text"], fetched["text"])
        self.assertTrue(search["results"][0]["verbatim"]["is_verbatim"])
        self.assertEqual(
            search["results"][0]["verbatim"]["approved_content_hash"],
            fetched["metadata"]["approved_content_hash"],
        )
        self.assertIn("Grounding rules", search["metadata"]["answer_guidance"])
        self.assertIn("Grounding rules", fetched["metadata"]["answer_guidance"])
        self.assertIn("육아휴직", fetched["text"])
        self.assertEqual(fetched["verbatim_text"], fetched["text"])
        self.assertEqual(fetched["verbatim"]["source"], "approved_local_regulation_chunk")
        self.assertEqual(fetched["metadata"]["approval_id"], "approval-mcp")
        self.assertTrue(fetched["metadata"]["content_hash"])
        self.assertTrue(fetched["metadata"]["approved_content_hash"])
        self.assertTrue(fetched["metadata"]["approval_review_batch_id"].startswith("approval-"))
        self.assertEqual(fetched["metadata"]["parser_uncertainty_recommendation"], "review_tables_and_appendices")
        self.assertEqual(search["results"][0]["metadata"]["profile_id"], "public_portal-test-profile")
        self.assertEqual(search["results"][0]["metadata"]["source_system"], "PUBLIC_PORTAL")
        self.assertEqual(search["results"][0]["metadata"]["source_url"], "https://example.test/public_portal/doc_mcp")
        self.assertEqual(fetched["metadata"]["profile_id"], "public_portal-test-profile")
        self.assertEqual(fetched["metadata"]["source_record_id"], "record-doc-mcp")
        self.assertEqual(fetched["metadata"]["source_file_id"], "file-doc-mcp")
        self.assertIn("duration", search["results"][0]["metadata"]["answer_intents"])
        self.assertIn("duration", fetched["metadata"]["answer_intents"])
        self.assertEqual(fetched["metadata"]["answer_facts"][0]["value"], "3년")
        self.assertTrue(fetched["url"].startswith("govreg://documents/"))
        self.assertEqual(docs["documents"][0]["document_id"], "doc_mcp")

    def test_lookup_prefers_direct_document_and_uses_rag_only_after_direct_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            records = routes_rag._load_local_vector_records(settings, mcp_auth)

            direct = lookup_regulation(
                settings=settings,
                auth=mcp_auth,
                query="approved regulation",
                document_id="doc_mcp",
                security_levels=["internal"],
            )
            fallback = lookup_regulation(
                settings=settings,
                auth=mcp_auth,
                query=str(records[0]["text"]),
                security_levels=["internal"],
            )

        self.assertEqual(direct["metadata"]["retrieval_mode"], "direct_lookup")
        self.assertTrue(direct["metadata"]["direct_lookup_attempted"])
        self.assertTrue(direct["metadata"]["direct_lookup_hit"])
        self.assertFalse(direct["metadata"]["fallback_used"])
        self.assertTrue(direct["results"])
        self.assertTrue(direct["results"][0]["verbatim"]["is_verbatim"])
        self.assertEqual(fallback["metadata"]["retrieval_mode"], "rag_fallback")
        self.assertFalse(fallback["metadata"]["direct_lookup_attempted"])
        self.assertTrue(fallback["metadata"]["fallback_used"])
        self.assertTrue(fallback["results"])

    def test_mcp_search_and_fetch_fail_closed_on_chunk_only_revocation_with_stale_sidecar(self) -> None:
        for revoked_status in ("rejected", "security_blocked", "superseded"):
            with self.subTest(revoked_status=revoked_status), tempfile.TemporaryDirectory() as tmp:
                settings = Settings(data_dir=Path(tmp) / "data")
                _prepare_mcp_indexed_document(settings)
                mcp_auth = mcp_auth_context(tenant_id="tenant-a")
                vector_path = routes_rag._local_vector_path(settings, mcp_auth)
                records = [
                    json.loads(line)
                    for line in vector_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                search_query = str(records[0]["text"])
                _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id="tenant-a")
                routes_rag._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
                _FETCH_CHUNK_INDEX_CACHE.clear()

                before = search_regulations(
                    settings=settings,
                    auth=mcp_auth,
                    query=search_query,
                    security_levels=["internal"],
                )
                self.assertEqual(1, len(before["results"]))
                result_id = before["results"][0]["id"]

                # Simulate failure after the authoritative chunk write but
                # before the review journal/manifest write and vector removal.
                repository = JsonRepository(settings)
                chunks = repository.get_chunks("doc_mcp")
                chunks[0] = chunks[0].model_copy(update={"approval_status": revoked_status})
                repository.save_chunks("doc_mcp", chunks)

                after = search_regulations(
                    settings=settings,
                    auth=mcp_auth,
                    query=search_query,
                    security_levels=["internal"],
                )
                with self.assertRaisesRegex(ValueError, "not available"):
                    fetch_regulation(
                        settings=settings,
                        auth=mcp_auth,
                        result_id=result_id,
                        security_levels=["internal"],
                    )

                self.assertEqual([], after["results"])

    def test_mcp_search_and_fetch_fail_closed_on_chunk_only_acl_tightening_with_stale_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            vector_path = routes_rag._local_vector_path(settings, mcp_auth)
            records = [
                json.loads(line)
                for line in vector_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            search_query = str(records[0]["text"])
            _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id="tenant-a")
            routes_rag._RAG_APPROVAL_SNAPSHOT_CACHE.clear()
            _FETCH_CHUNK_INDEX_CACHE.clear()

            before = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query=search_query,
                security_levels=["internal"],
            )
            self.assertEqual(1, len(before["results"]))
            result_id = before["results"][0]["id"]

            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc_mcp")
            chunks[0] = chunks[0].model_copy(update={"department_acl": ["legal"]})
            repository.save_chunks("doc_mcp", chunks)

            after = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query=search_query,
                security_levels=["internal"],
            )
            with self.assertRaisesRegex(ValueError, "not available"):
                fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual([], after["results"])

    def test_mcp_fetch_result_id_does_not_cross_tenant_vector_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            tenant_a_auth = mcp_auth_context(tenant_id="tenant-a")
            tenant_b_auth = mcp_auth_context(tenant_id="tenant-b")
            records = routes_rag._load_local_vector_records(settings, tenant_a_auth)
            search = search_regulations(
                settings=settings,
                auth=tenant_a_auth,
                query=str(records[0]["text"]),
                security_levels=["internal"],
            )

            with self.assertRaisesRegex(ValueError, "not available"):
                fetch_regulation(
                    settings=settings,
                    auth=tenant_b_auth,
                    result_id=search["results"][0]["id"],
                    security_levels=["internal"],
                )

    def test_chatgpt_data_metadata_profile_hides_internal_citation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
                metadata_profile="chatgpt-data",
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
                metadata_profile="chatgpt-data",
            )

        internal_keys = {
            "source_record_id",
            "source_file_id",
            "approval_worklist_report_sha256",
            "approval_review_batch_manifest_path",
            "approval_review_batch_manifest_sha256",
            "approval_review_batch_id",
            "approval_review_batch_chunk_fingerprint",
            "approval_review_strategy",
        }
        self.assertNotIn("tenant_id", search["metadata"])
        self.assertEqual(
            "https://example.test/public_portal/doc_mcp",
            search["results"][0]["url"],
        )
        self.assertEqual(
            "https://example.test/public_portal/doc_mcp",
            fetched["url"],
        )
        for metadata in (search["results"][0]["metadata"], fetched["metadata"]):
            for key in internal_keys:
                self.assertNotIn(key, metadata)
            self.assertEqual(metadata["approval_id"], "approval-mcp")
            self.assertTrue(metadata["approved_content_hash"])
            self.assertEqual(metadata["profile_id"], "public_portal-test-profile")
            self.assertEqual(metadata["source_system"], "PUBLIC_PORTAL")
            self.assertEqual(metadata["source_url"], "https://example.test/public_portal/doc_mcp")

    def test_mcp_invalid_metadata_profile_fails_before_success_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with self.assertRaisesRegex(ValueError, "metadata_profile must be full or chatgpt-data"):
                search_regulations(
                    settings=settings,
                    auth=mcp_auth,
                    query="no matching primary anchor",
                    security_levels=["internal"],
                    metadata_profile="external",
                )
            valid_search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )
            with self.assertRaisesRegex(ValueError, "metadata_profile must be full or chatgpt-data"):
                fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=valid_search["results"][0]["id"],
                    security_levels=["internal"],
                    metadata_profile="external",
                )
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        search_rows = [row for row in rows if row["action"] == "mcp.search"]
        fetch_rows = [row for row in rows if row["action"] == "mcp.fetch"]
        self.assertEqual(["failure", "success"], [row["outcome"] for row in search_rows])
        self.assertEqual(1, len(fetch_rows))
        self.assertEqual("failure", fetch_rows[0]["outcome"])
        self.assertEqual(400, fetch_rows[0]["status_code"])

    def test_mcp_relevance_guard_refuses_missing_primary_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658 \uaddc\uc815\uc774 \uc788\ub098\uc694?",
            [
                {
                    "score": 2.0,
                    "text": "\ud604\uae08 \uc804\ud658 \uaddc\uc815\uc740 \ud68c\uacc4 \ucc98\ub9ac \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                    "regulation_title": "\ud68c\uacc4 \uaddc\uc815",
                    "article_title": "\ud604\uae08 \uc804\ud658",
                }
            ],
        )

        self.assertTrue(guard["refused"])
        self.assertEqual("insufficient_relevance", guard["refusal_reason"])
        self.assertEqual("missing_primary_query_anchor", guard["refusal_detail"])
        self.assertEqual("\ubcf5\uc9c0\ud3ec\uc778\ud2b8", guard["primary_anchor_token"])
        self.assertFalse(guard["primary_anchor_hit"])

    def test_mcp_relevance_guard_keeps_compound_anchor_after_tokenizer_warmup(self) -> None:
        tokenize("\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658")
        guard = _mcp_relevance_guard(
            "\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658 \uaddc\uc815\uc774 \uc788\ub098\uc694?",
            [
                {
                    "score": 2.0,
                    "text": "\ud604\uae08 \uc804\ud658 \uaddc\uc815\uc740 \ud68c\uacc4 \ucc98\ub9ac \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                    "regulation_title": "\ud68c\uacc4 \uaddc\uc815",
                    "article_title": "\ud604\uae08 \uc804\ud658",
                }
            ],
        )

        self.assertTrue(guard["refused"])
        self.assertEqual("\ubcf5\uc9c0\ud3ec\uc778\ud2b8", guard["primary_anchor_token"])

    def test_mcp_relevance_guard_allows_matching_primary_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc721\uc544\ud734\uc9c1 \uc2e0\uccad \uc808\ucc28",
            [
                {
                    "score": 3.0,
                    "text": "\uc721\uc544\ud734\uc9c1 \uc2e0\uccad \uc808\ucc28\ub294 \uc2b9\uc778\ub41c \uaddc\uc815\uc5d0 \ub530\ub978\ub2e4.",
                    "regulation_title": "\uc778\uc0ac \uaddc\uc815",
                    "article_title": "\uc721\uc544\ud734\uc9c1",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_normalize_query_token_strips_stacked_particles(self) -> None:
        # The relevance guard tokenizes with the regex fallback on cold start,
        # which removes only a single trailing particle.  The normalizer must
        # strip stacked particles fully ("명부에는" -> "명부") so a malformed
        # token like "명부에" is never chosen as the primary anchor.  This is
        # kiwi-independent, so it locks the cold-start behavior deterministically.
        self.assertEqual("명부", regulation_tools._mcp_normalize_query_token("명부에는"))
        self.assertEqual("겸직자", regulation_tools._mcp_normalize_query_token("겸직자에게는"))
        # A single trailing particle is still stripped as before.
        self.assertEqual("겸직자", regulation_tools._mcp_normalize_query_token("겸직자를"))

    def test_mcp_relevance_guard_allows_spaced_table_header_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "겸직자 명부 서식에는 어떤 항목을 기록하나요?",
            [
                {
                    "score": 87.0,
                    "text": "[별지 제6호 서식]\n겸 직 자 명 부\n번호 소 속 직 위 성 명 기 간 겸직기관 겸직직위 비 고",
                    "regulation_title": "복무규정",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_allows_korean_compound_token_match(self) -> None:
        guard = _mcp_relevance_guard(
            "\uad8c\ud55c \uc704\uc784",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c7\uc870(\uc704\uc784\uc804\uacb0) \uad8c\ud55c\uc704\uc784\uc804\uacb0\uaddc\uc815\uc5d0 \ub530\ub77c \ucc98\ub9ac\ud55c\ub2e4.",
                    "regulation_title": "\uad8c\ud55c\uc704\uc784\uc804\uacb0\uaddc\uc815",
                    "article_title": "\uc704\uc784\uc804\uacb0",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_allows_hiring_query_when_result_uses_appointment_term(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc804\uc784 \uad50\uc6d0 \ucc44\uc6a9 \uc808\ucc28\ub294 \uc5b4\ub5bb\uac8c \uc9c4\ud589\ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uad50\uc6d0 \uc784\uc6a9 \uc138\uce59\uc5d0 \ub530\ub77c \uc2e0\uaddc\uc784\uc6a9 \ud6c4\ubcf4\uc790 \uc2ec\uc0ac\ub97c \uc9c4\ud589\ud55c\ub2e4.",
                    "regulation_title": "\uad50\uc6d0 \uc784\uc6a9 \uc138\uce59",
                    "article_title": "\uc2e0\uaddc\uc784\uc6a9\uc758 \uc2dc\uae30",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_allows_hiring_definition_when_domain_terms_overlap(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc804\uc784 \uad50\uc6d0 \ucc44\uc6a9 \uc808\ucc28\ub294 \uc5b4\ub5bb\uac8c \uc9c4\ud589\ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c38\uc870(\uad50\uc6d0) \uad50\uc6d0\uc740 \ucd1d\uc7a5, \uad50\uc218, \ubd80\uad50\uc218, \uc870\uad50\uc218, \uc804\uc784\uac15\uc0ac\ub85c \ud55c\ub2e4.",
                    "regulation_title": "\uc778\uc0ac\uaddc\uc815",
                    "article_title": "\uad50\uc6d0",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_uses_base_anchor_for_inflected_domain_term(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc721\uc544\ud734\uc9c1\uc758 \uc694\uac74, \uae30\uac04, \uc218\ub2f9\uc740 \uc5b4\ub5bb\uac8c \ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c30\uc870(\ud734\uc9c1 \uae30\uac04) \uc81c29\uc870 \uc81c3\ud56d\uc5d0 \ub530\ub978 \ud734\uc9c1 \uae30\uac04\uc740 \uc790\ub140 1\uba85\uc5d0 \ub300\ud558\uc5ec 3\ub144 \uc774\ub0b4\ub85c \ud55c\ub2e4.",
                    "regulation_title": "\uc778\uc0ac\uaddc\uc815",
                    "article_title": "\ud734\uc9c1 \uae30\uac04",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_ignores_question_ending_as_primary_anchor(self) -> None:
        guard = _mcp_relevance_guard(
            "\uc131\uacfc\uc5f0\ubd09\uc740 \uc5b8\uc81c \uc5b4\ub5a4 \ubc29\uc2dd\uc73c\ub85c \uc9c0\uae09\ub418\ub098\uc694?",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c24\uc870(\uc5f0\ubd09\uc758 \uc9c0\uae09 \ubc29\ubc95) \uc131\uacfc\uc5f0\ubd09\uc740 6\uc6d4\uacfc 12\uc6d4\uc5d0 \uc774\ub4f1\ubd84\ud558\uc5ec \uc9c0\uae09\ud55c\ub2e4.",
                    "regulation_title": "\uad50\uc9c1\uc6d0\ubcf4\uc218\uaddc\uc815",
                    "article_title": "\uc5f0\ubd09\uc758 \uc9c0\uae09 \ubc29\ubc95",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_mcp_relevance_guard_ignores_plain_instruction_word(self) -> None:
        guard = _mcp_relevance_guard(
            "\ud734\uc9c1\uc758 \uc885\ub958\uc640 \uc808\ucc28\uc5d0 \ub300\ud574\uc11c \uc54c\ub824\uc918",
            [
                {
                    "score": 3.0,
                    "text": "\uc81c31\uc870(\ud734\uc9c1\uc758 \uc6b4\uc601) \ud734\uc9c1 \uae30\uac04 \uc911 \uc0ac\uc720\uac00 \uc18c\uba78\ub418\uba74 \ubcf5\uc9c1\uc744 \uba85\ud55c\ub2e4.",
                    "regulation_title": "\uc778\uc0ac\uaddc\uc815",
                    "article_title": "\ud734\uc9c1\uc758 \uc6b4\uc601",
                }
            ],
        )

        self.assertFalse(guard["refused"])

    def test_search_refuses_irrelevant_query_before_fetch_ids_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_cash_conversion",
                "\ud604\uae08 \uc804\ud658 \uaddc\uc815\uc740 \ud68c\uacc4 \ucc98\ub9ac \uae30\uc900\uc5d0 \ub530\ub978\ub2e4.",
                "approval-cash-conversion",
                auth,
                metadata={
                    "article_no": "\uc81c1\uc870",
                    "article_title": "\ud604\uae08 \uc804\ud658",
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="\ubcf5\uc9c0\ud3ec\uc778\ud2b8 \ud604\uae08 \uc804\ud658 \uaddc\uc815\uc774 \uc788\ub098\uc694?",
                security_levels=["internal"],
            )

        self.assertEqual([], search["results"])
        self.assertTrue(search["metadata"]["refused"])
        self.assertEqual("insufficient_relevance", search["metadata"]["refusal_reason"])
        self.assertEqual("missing_primary_query_anchor", search["metadata"]["refusal_detail"])
        self.assertEqual("approved_local_regulation_db", search["metadata"]["source"])
        self.assertEqual(0, search["metadata"]["result_count"])

    def test_synthetic_public_portal_chunks_are_invisible_until_approved_and_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_public_portal_visibility",
                    filename="public_portal_visibility.pdf",
                    document_name="PUBLIC_PORTAL Visibility Rule",
                    file_type="pdf",
                    file_hash="hash-public_portal-visibility",
                    tenant_id="tenant-a",
                    institution_name="Synthetic PUBLIC_PORTAL Institution",
                    apba_id="C9999",
                    source_system="PUBLIC_PORTAL",
                    source_url="https://example.test/public_portal/doc_public_portal_visibility",
                    source_record_id="record-public_portal-visibility",
                    source_file_id="file-public_portal-visibility",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_public_portal_visibility",
                [],
                [
                    Chunk(
                        chunk_id="public_portal-approved-candidate",
                        document_id="doc_public_portal_visibility",
                        chunk_type="article",
                        text="public_portal approved visible token may be shown only after approval.",
                        retrieval_text="public_portal approved visible token may be shown only after approval.",
                        metadata={
                            "source_system": "PUBLIC_PORTAL",
                            "source_record_id": "record-public_portal-visibility",
                            "source_file_id": "file-public_portal-visibility",
                            "profile_id": "public_portal-synthetic-profile",
                            "regulation_title": "PUBLIC_PORTAL Visibility Rule",
                            "article_no": "Article 1",
                            "article_title": "Approved candidate",
                        },
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="public_portal-draft-only",
                        document_id="doc_public_portal_visibility",
                        chunk_type="article",
                        text="public_portal draft hidden token must never be returned.",
                        retrieval_text="public_portal draft hidden token must never be returned.",
                        metadata={
                            "source_system": "PUBLIC_PORTAL",
                            "source_record_id": "record-public_portal-visibility",
                            "source_file_id": "file-public_portal-visibility",
                            "profile_id": "public_portal-synthetic-profile",
                            "regulation_title": "PUBLIC_PORTAL Visibility Rule",
                            "article_no": "Article 2",
                            "article_title": "Draft only",
                        },
                        security_level="internal",
                    ),
                ],
                [],
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            before = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="public_portal approved visible token",
                security_levels=["internal"],
            )

            admin_auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_public_portal_visibility",
                chunks=[chunk for chunk in repository.get_chunks("doc_public_portal_visibility") if chunk.chunk_id == "public_portal-approved-candidate"],
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_public_portal_visibility",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["public_portal-approved-candidate"],
                        approval_id="approval-public_portal-visibility",
                        security_level="internal",
                        **evidence,
                    ),
                    admin_auth,
                )
                routes_documents.index_document(
                    "doc_public_portal_visibility",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    admin_auth,
                )

            after = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="public_portal approved visible token",
                security_levels=["internal"],
            )
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([], before["results"])
        self.assertEqual(1, len(after["results"]))
        self.assertEqual("public_portal-approved-candidate", after["results"][0]["metadata"]["chunk_id"])
        self.assertEqual("PUBLIC_PORTAL", after["results"][0]["metadata"]["source_system"])
        self.assertEqual("C9999", after["results"][0]["metadata"]["apba_id"])
        self.assertEqual("record-public_portal-visibility", after["results"][0]["metadata"]["source_record_id"])
        self.assertIn("public_portal approved visible token", after["results"][0]["text"])
        self.assertNotIn("public_portal draft hidden token", after["results"][0]["text"])
        self.assertEqual(["public_portal-approved-candidate"], [record["chunk_id"] for record in vector_records])
        self.assertTrue(vector_records[0]["metadata"]["approval_id"])
        self.assertEqual("approved", vector_records[0]["metadata"]["approval_status"])
        self.assertEqual("C9999", vector_records[0]["metadata"]["apba_id"])

    def test_mcp_metadata_exposes_hwpx_complex_structure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_hwpx_evidence",
                "Nested table evidence appears in this approved regulation chunk.",
                "approval-hwpx-evidence",
                auth,
                metadata={
                    "article_no": "A1",
                    "article_title": "HWPX evidence",
                    "source_hwpx_block_types": ["table"],
                    "source_xml_files": ["Contents/header.xml"],
                    "source_xml_roles": ["metadata"],
                    "source_hwpx_parser_review_flags": ["nested_table"],
                    "source_hwpx_xml_block_indices": [312, 313, 314],
                    "source_hwpx_nested_table_text_snippets": ["Nested table evidence"],
                    "source_hwp_extraction_modes": ["legacy_ole_para_text_only"],
                    "source_hwp_streams": ["BodyText/Section0"],
                    "source_hwp_section_indices": [1],
                    "source_hwp_native_table_geometry": False,
                    "pdf_embedded_image_pages": [8],
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="Nested table evidence",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )

        self.assertEqual(search["results"][0]["metadata"]["source_hwpx_block_types"], ["table"])
        self.assertEqual(search["results"][0]["metadata"]["source_hwpx_parser_review_flags"], ["nested_table"])
        self.assertEqual(search["results"][0]["metadata"]["source_xml_roles"], ["metadata"])
        self.assertEqual(fetched["metadata"]["source_xml_files"], ["Contents/header.xml"])
        self.assertEqual(fetched["metadata"]["source_hwpx_xml_block_indices"], [312, 313, 314])
        self.assertEqual(fetched["metadata"]["source_hwpx_nested_table_text_snippets"], ["Nested table evidence"])
        self.assertEqual(fetched["metadata"]["source_hwp_extraction_modes"], ["legacy_ole_para_text_only"])
        self.assertEqual(fetched["metadata"]["source_hwp_streams"], ["BodyText/Section0"])
        self.assertEqual(fetched["metadata"]["source_hwp_section_indices"], [1])
        self.assertFalse(fetched["metadata"]["source_hwp_native_table_geometry"])
        self.assertEqual(fetched["metadata"]["pdf_embedded_image_pages"], [8])

    def test_fetch_validates_only_the_requested_vector_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )

            with patch.object(
                regulation_tools.routes_rag,
                "record_visible_to_request",
                wraps=regulation_tools.routes_rag.record_visible_to_request,
            ) as visible_check:
                fetched = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=search["results"][0]["id"],
                    security_levels=["internal"],
                )

        self.assertEqual(fetched["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(visible_check.call_count, 1)

    def test_fetch_limits_governing_enrichment_to_article_candidates(
        self,
    ) -> None:
        record = {
            "document_id": "doc-a",
            "chunk_id": "form-a",
            "text": "별지 제1호서식",
            "content_hash": "hash-a",
            "metadata": {
                "document_id": "doc-a",
                "chunk_id": "form-a",
                "approval_status": "approved",
                "approval_id": "approval-a",
                "approved_content_hash": "approved-hash-a",
                "security_level": "internal",
                "profile_id": "profile-a",
                "form_refs": ["별지제1호서식"],
            },
        }
        result_id = search_regulations.__globals__["_encode_result_id"](
            document_id="doc-a",
            chunk_id="form-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                Settings(data_dir=Path(tmp) / "data"),
                api_audit_enabled=False,
            )
            auth = mcp_auth_context(tenant_id="tenant-a")
            with (
                patch.object(
                    regulation_tools,
                    "_visible_record_by_chunk",
                    return_value=record,
                ),
                patch.object(
                    regulation_tools,
                    "_visible_records",
                    return_value=[],
                ) as visible_records,
            ):
                fetched = fetch_regulation(
                    settings=settings,
                    auth=auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual("form-a", fetched["metadata"]["chunk_id"])
        self.assertTrue(
            visible_records.call_args.kwargs["article_candidates_only"]
        )

    def test_fetch_governing_enrichment_fails_closed_on_approval_revocation_during_fast_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                Settings(data_dir=Path(tmp) / "data"),
                api_audit_enabled=False,
            )
            auth = mcp_auth_context(
                tenant_id="tenant-a",
                role="operator",
                department_ids=["hr"],
            )
            target_record = {
                "document_id": "doc_governing",
                "chunk_id": "form-15",
                "text": "[별지제15호서식] 휴직원",
                "content_hash": "hash-form",
                "metadata": {
                    "document_id": "doc_governing",
                    "chunk_id": "form-15",
                    "approval_status": "approved",
                    "approval_id": "approval-governing",
                    "approved_content_hash": "approved-form",
                    "security_level": "internal",
                    "department_acl": [],
                    "profile_id": "profile-a",
                    "article_no": "",
                    "article_title": "",
                    "form_refs": ["별지제15호서식"],
                    "regulation_title": "휴직 규정",
                },
            }
            article_record = {
                "document_id": "doc_governing",
                "chunk_id": "article-31",
                "text": "제31조 휴직의 운영은 별지제15호서식에 따른다.",
                "content_hash": "hash-article",
                "metadata": {
                    "document_id": "doc_governing",
                    "chunk_id": "article-31",
                    "approval_status": "approved",
                    "approval_id": "approval-governing",
                    "approved_content_hash": "approved-article",
                    "security_level": "internal",
                    "department_acl": [],
                    "profile_id": "profile-a",
                    "article_no": "제31조",
                    "article_title": "휴직의 운영",
                    "form_refs": ["별지제15호서식"],
                    "regulation_title": "휴직 규정",
                },
            }
            approval_snapshot = {
                ("doc_governing", "form-15"): {
                    "approval_id": "approval-governing",
                    "approved_content_hash": "approved-form",
                    "content_hash": "hash-form",
                    "security_level": "internal",
                    "department_acl": set(),
                },
                ("doc_governing", "article-31"): {
                    "approval_id": "approval-governing",
                    "approved_content_hash": "approved-article",
                    "content_hash": "hash-article",
                    "security_level": "internal",
                    "department_acl": set(),
                },
            }
            approval_revoked = threading.Event()
            runtime_token = SimpleNamespace(
                index_path=Path("hierarchy.sqlite"),
                vector_path=Path("vectors.jsonl"),
                index_identity=("index-stable",),
                vector_identity=("vector-stable",),
                profile_id="profile-a",
                matches_scope=lambda **_kwargs: True,
                is_current=lambda: True,
            )

            def load_related_and_revoke(*_args, **_kwargs):
                approval_revoked.set()
                return [article_record]

            def current_approval_identity(*_args, **_kwargs):
                return (
                    ("approval-source-after",)
                    if approval_revoked.is_set()
                    else ("approval-source-before",)
                )

            with (
                patch.object(
                    regulation_tools,
                    "_resolve_mcp_profile_scope_with_runtime_token",
                    return_value=("profile-a", runtime_token),
                ),
                patch.object(
                    regulation_tools,
                    "_load_cached_hierarchical_record_by_chunk",
                    return_value=target_record,
                ),
                patch.object(
                    regulation_tools,
                    "load_hierarchical_document_article_records",
                    side_effect=load_related_and_revoke,
                ) as related_loader,
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    side_effect=current_approval_identity,
                ) as approval_identity,
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=approval_snapshot,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "repository_cache",
                    return_value=object(),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "is_record_visible",
                    return_value=True,
                ),
                patch.object(
                    regulation_tools,
                    "filter_to_latest_active_versions",
                    side_effect=lambda records, **_kwargs: list(records),
                ),
                patch.object(
                    regulation_tools,
                    "_visible_record_by_chunk",
                    return_value=None,
                ) as fallback_lookup,
                patch.object(
                    regulation_tools,
                    "_visible_records",
                    side_effect=AssertionError(
                        "no related records should be returned after TOCTOU fail-closed"
                    ),
                ),
            ):
                record, related_records = (
                    regulation_tools._visible_record_with_related_by_chunk(
                        settings=settings,
                        auth=auth,
                        document_id="doc_governing",
                        chunk_id="form-15",
                        security_levels=["internal"],
                        department_ids=["hr"],
                        profile_id="profile-a",
                        as_of_date="2026-07-01",
                    )
                )

        self.assertEqual(1, related_loader.call_count)
        self.assertIsNone(record)
        self.assertEqual([], related_records)
        self.assertEqual(1, fallback_lookup.call_count)
        self.assertEqual(2, approval_identity.call_count)

    def test_fetch_read_context_checks_each_identity_once_before_and_after(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            manifest_path, index_path, _bm25_path = (
                _write_bound_hierarchy_bm25_fixture(
                    settings,
                    profile_id="profile-a",
                )
            )
            self.assertIsNotNone(
                regulation_tools._verified_hierarchical_runtime_paths(
                    settings=settings,
                    auth=auth,
                    profile_id="profile-a",
                )
            )
            vector_path = regulation_tools.routes_rag.local_vector_path(
                settings,
                auth,
            )
            target_record = {
                "document_id": "doc-binding",
                "chunk_id": "chunk-binding",
                "text": "approved policy binding evidence",
                "content_hash": "content-hash",
                "metadata": {
                    "document_id": "doc-binding",
                    "chunk_id": "chunk-binding",
                    "profile_id": "profile-a",
                    "approval_status": "approved",
                    "approval_id": "approval-binding",
                    "approved_content_hash": "approved-binding",
                    "security_level": "internal",
                    "department_acl": [],
                    "article_no": "Article 1",
                    "article_title": "Purpose",
                },
            }
            approval_snapshot = {
                ("doc-binding", "chunk-binding"): {
                    "approval_id": "approval-binding",
                    "approved_content_hash": "approved-binding",
                    "content_hash": "content-hash",
                    "security_level": "internal",
                    "department_acl": set(),
                }
            }
            original_path_signature = (
                regulation_tools.routes_rag.path_signature
            )

            with (
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    wraps=original_path_signature,
                ) as path_signature,
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-stable",),
                ) as approval_identity,
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=approval_snapshot,
                ),
                patch.object(
                    regulation_tools,
                    "_load_cached_hierarchical_record_by_chunk",
                    return_value=target_record,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "repository_cache",
                    return_value=object(),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "is_record_visible",
                    return_value=True,
                ),
                patch.object(
                    regulation_tools,
                    "filter_to_latest_active_versions",
                    side_effect=lambda records, **_kwargs: list(records),
                ),
            ):
                fetched, related_records = (
                    regulation_tools._visible_record_with_related_by_chunk(
                        settings=settings,
                        auth=auth,
                        document_id="doc-binding",
                        chunk_id="chunk-binding",
                        security_levels=["internal"],
                        department_ids=[],
                        profile_id="profile-a",
                        as_of_date="2026-07-01",
                    )
                )

        self.assertEqual("chunk-binding", fetched["chunk_id"])
        self.assertEqual([], related_records)
        self.assertEqual(2, approval_identity.call_count)
        self.assertEqual(
            Counter(
                {
                    manifest_path: 2,
                    index_path: 2,
                    vector_path: 2,
                }
            ),
            Counter(
                current.args[0]
                for current in path_signature.call_args_list
            ),
        )

    def test_fetch_governing_enrichment_fast_path_matches_fallback_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, auth, result_id = _prepare_mcp_governing_article_document(root)

            fast = fetch_regulation(
                settings=settings,
                auth=auth,
                result_id=result_id,
                security_levels=["internal"],
                department_ids=["hr"],
                profile_id="profile-a",
                as_of_date="2026-07-01",
            )
            with patch.object(
                regulation_tools.routes_rag,
                "runtime_approval_snapshot_identity",
                return_value=None,
            ):
                fallback = fetch_regulation(
                    settings=settings,
                    auth=auth,
                    result_id=result_id,
                    security_levels=["internal"],
                    department_ids=["hr"],
                    profile_id="profile-a",
                    as_of_date="2026-07-01",
                )

        self.assertEqual(fast["id"], fallback["id"])
        self.assertEqual(fast["title"], fallback["title"])
        self.assertEqual(fast["text"], fallback["text"])
        self.assertEqual(fast["metadata"]["chunk_id"], "form-15")
        self.assertEqual("article-31", fast["metadata"]["governing_article_chunk_id"])
        self.assertEqual("제31조", fast["metadata"]["governing_article_no"])
        self.assertEqual("휴직의 운영", fast["metadata"]["governing_article_title"])
        self.assertEqual("별지제15호서식", fast["metadata"]["governing_article_match_ref"])
        self.assertEqual(
            fast["metadata"]["governing_article_chunk_id"],
            fallback["metadata"]["governing_article_chunk_id"],
        )
        self.assertEqual(
            fast["metadata"]["governing_article_no"],
            fallback["metadata"]["governing_article_no"],
        )
        self.assertEqual(
            fast["metadata"]["governing_article_title"],
            fallback["metadata"]["governing_article_title"],
        )
        self.assertEqual(
            fast["metadata"]["governing_article_match_ref"],
            fallback["metadata"]["governing_article_match_ref"],
        )
        self.assertEqual(
            fast["metadata"]["form_refs"],
            fallback["metadata"]["form_refs"],
        )

    def test_fetch_reuses_chunk_index_for_repeated_result_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직",
                security_levels=["internal"],
            )
            result_id = search["results"][0]["id"]

            _FETCH_CHUNK_INDEX_CACHE.clear()
            with patch.object(
                routes_rag,
                "_load_local_vector_records",
                wraps=routes_rag._load_local_vector_records,
            ) as load_records:
                first = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )
                second = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual(first["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(second["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(0, load_records.call_count)

    def test_hierarchical_fetch_record_cache_tracks_runtime_file_signatures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            index_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            vector_path = (
                settings.data_dir
                / "vector_db"
                / "tenant-a"
                / "approved_vectors.jsonl"
            )
            record = {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "content_hash": "hash-a",
            }
            signature_generation = {"value": 1}

            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(index_path, vector_path),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    side_effect=lambda path: (
                        signature_generation["value"],
                        str(path),
                    ),
                ),
                patch.object(
                    regulation_tools,
                    "load_hierarchical_record_by_chunk",
                    return_value=record,
                ) as load_record,
            ):
                first = regulation_tools._indexed_vector_record_by_chunk(
                    settings=settings,
                    auth=auth,
                    document_id="doc-a",
                    chunk_id="chunk-a",
                )
                cached = regulation_tools._indexed_vector_record_by_chunk(
                    settings=settings,
                    auth=auth,
                    document_id="doc-a",
                    chunk_id="chunk-a",
                )
                signature_generation["value"] = 2
                changed = regulation_tools._indexed_vector_record_by_chunk(
                    settings=settings,
                    auth=auth,
                    document_id="doc-a",
                    chunk_id="chunk-a",
                )

        self.assertEqual(record, first)
        self.assertEqual(record, cached)
        self.assertEqual(record, changed)
        self.assertEqual(2, load_record.call_count)

    def test_fetch_finds_requested_vector_record_without_full_vector_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            result_id = search_regulations.__globals__["_encode_result_id"](
                document_id="doc_mcp",
                chunk_id="approved-1",
            )
            _FETCH_CHUNK_INDEX_CACHE.clear()
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()

            with patch.object(
                routes_rag,
                "_load_local_vector_records",
                wraps=routes_rag._load_local_vector_records,
            ) as load_records:
                fetched = fetch_regulation(
                    settings=settings,
                    auth=mcp_auth,
                    result_id=result_id,
                    security_levels=["internal"],
                )

        self.assertEqual(fetched["metadata"]["chunk_id"], "approved-1")
        self.assertEqual(0, load_records.call_count)

    def test_visible_record_by_chunk_hides_superseded_but_keeps_current(self) -> None:
        # fetch resolves a single chunk by id and must not serve a superseded or
        # repealed version as current evidence just because the chunk itself is
        # still approved.  The currency gate runs over the one fetched record so
        # the targeted lookup is preserved (no full vector load).
        def _record(document_id: str, chunk_id: str, *, status: str, effective_to: str | None) -> dict:
            return {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "text": f"{document_id} 본문: 육아휴직 3년.",
                "metadata": {
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "approval_status": "approved",
                    "regulation_id": "reg-x",
                    "regulation_version": "v1" if status != "approved" else "v2",
                    "effective_from": "2024-01-01" if status != "approved" else "2025-01-01",
                    "effective_to": effective_to,
                    "regulation_status": status,
                    "profile_id": "institution-a",
                },
            }

        superseded = _record("doc-old", "old-1", status="superseded", effective_to="2025-01-01")
        current = _record("doc-new", "new-1", status="approved", effective_to=None)

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            def resolve(*_args, document_id: str, chunk_id: str, **_kwargs):
                return superseded if document_id == "doc-old" else current

            with (
                patch.object(regulation_tools, "_indexed_vector_record_by_chunk", side_effect=resolve),
                patch.object(regulation_tools, "_validate_mcp_security_scope"),
                patch.object(
                    regulation_tools.routes_rag,
                    "get_visible_record_by_chunk",
                    side_effect=lambda *, candidate, **_kwargs: candidate,
                ),
            ):
                hidden = regulation_tools._visible_record_by_chunk(
                    settings=settings,
                    auth=mcp_auth,
                    document_id="doc-old",
                    chunk_id="old-1",
                    security_levels=["internal"],
                    department_ids=[],
                    profile_id="institution-a",
                )
                kept = regulation_tools._visible_record_by_chunk(
                    settings=settings,
                    auth=mcp_auth,
                    document_id="doc-new",
                    chunk_id="new-1",
                    security_levels=["internal"],
                    department_ids=[],
                    profile_id="institution-a",
                )

        self.assertIsNone(hidden)
        self.assertIsNotNone(kept)
        self.assertEqual("new-1", kept["chunk_id"])

    def test_vector_record_loader_streams_jsonl_without_read_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()

            with patch.object(Path, "read_text", side_effect=AssertionError("read_text should not load vector JSONL")):
                records = routes_rag._load_local_vector_records(settings, mcp_auth)

        self.assertEqual(["approved-1"], [record["chunk_id"] for record in records])

    def test_vector_record_loader_avoids_concurrent_cache_stampede(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")
            routes_rag._RAG_VECTOR_RECORD_CACHE.clear()
            read_count = 0
            original_iter = routes_rag._iter_local_vector_lines

            def slow_iter(path: Path):
                nonlocal read_count
                read_count += 1
                time.sleep(0.05)
                yield from original_iter(path)

            with patch.object(routes_rag, "_iter_local_vector_lines", side_effect=slow_iter):
                with ThreadPoolExecutor(max_workers=5) as executor:
                    results = list(
                        executor.map(
                            lambda _: routes_rag._load_local_vector_records(settings, mcp_auth),
                            range(5),
                        )
                    )

        self.assertEqual(1, read_count)
        self.assertTrue(all([record["chunk_id"] for record in records] == ["approved-1"] for records in results))

    def test_mcp_metadata_uses_article_heading_when_stored_title_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_pay",
                (
                    "제32조 <삭제 2021.3.31.>\n"
                    "제33조(육아휴직수당) 30일 이상 휴직한 교직원의 육아휴직수당은 "
                    "기본연봉월액의 78퍼센트로 한다."
                ),
                "approval-pay",
                auth,
                metadata={
                    "article_no": "제32조",
                    "article_title": "삭제",
                    "regulation_title": "교직원보수규정",
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="육아휴직수당",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )

        self.assertEqual("제33조", search["results"][0]["metadata"]["article_no"])
        self.assertEqual("육아휴직수당", search["results"][0]["metadata"]["article_title"])
        self.assertEqual("제33조", fetched["metadata"]["article_no"])
        self.assertEqual("육아휴직수당", fetched["metadata"]["article_title"])
        self.assertIn("교직원보수규정 제33조 육아휴직수당", fetched["title"])

    def test_mcp_metadata_cleans_answer_profile_spacing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_noisy_profile",
                "③원장은 지원 마감일 전까지 15일 이상 지원자격 등에 관한 사항을 공고한다.",
                "approval-noisy-profile",
                auth,
                metadata={
                    "article_no": "제7조",
                    "article_title": "신규임용의 시기",
                    "chapter_title": "신규 임용",
                    "answer_profile_version": "reg-rag-answer-profile-v1",
                    "answer_facts": [
                        {
                            "type": "duration",
                            "value": "15일이상",
                            "sentence": "③원장은 지원자격 등 에 관한 사항을 효과적인 방법 으로 공고한다.<2011.11.10.>",
                        }
                    ],
                    "answer_outline": [
                        "③원장은 지원자격 등 에 관한 사항을 효과적인 방법 으로 공고한다.",
                        "신규임용은 3년이내에 하는 것을 원칙으 로 한다.",
                        "제27조의2(성과연봉 지급대상 제외) 평가대상 기간 중 중징계 처분을 받거나 다 음과 같은 사유로 징계를 받은 경우 제외한다.",
                        "제44조(술에 취한 상태에서의 운전금지)제1항에 따른 음주운 전 또는 음주측정에 대한 불응 <2022.12.28., 2025.1 4-3-",
                        "교직원보수규정 2.22.>",
                    ],
                },
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(
                settings=settings,
                auth=mcp_auth,
                query="지원자격 공고",
                security_levels=["internal"],
            )
            fetched = fetch_regulation(
                settings=settings,
                auth=mcp_auth,
                result_id=search["results"][0]["id"],
                security_levels=["internal"],
            )

        rendered = json.dumps(fetched["metadata"], ensure_ascii=False)
        self.assertEqual(fetched["metadata"]["regulation_title"], "신규 임용")
        self.assertIn("③ 원장은", rendered)
        self.assertIn("15일 이상", rendered)
        self.assertIn("등에", rendered)
        self.assertIn("방법으로", rendered)
        self.assertIn("원칙으로", rendered)
        self.assertIn("다음과 같은 사유", rendered)
        self.assertIn("음주운전", rendered)
        self.assertNotIn("③원장은", rendered)
        self.assertNotIn("15일이상", rendered)
        self.assertNotIn("등 에", rendered)
        self.assertNotIn("다 음", rendered)
        self.assertNotIn("음주운 전", rendered)
        self.assertNotIn("교직원보수규정 2.22", rendered)
        self.assertNotIn("방법 으로", rendered)
        self.assertNotIn("원칙으 로", rendered)
        self.assertNotIn("<2011", rendered)

    def test_all_mcp_tools_write_success_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            search = search_regulations(settings=settings, auth=mcp_auth, query="육아휴직", security_levels=["internal"])
            result_id = search["results"][0]["id"]
            fetch_regulation(settings=settings, auth=mcp_auth, result_id=result_id, security_levels=["internal"])
            lookup_regulation(
                settings=settings,
                auth=mcp_auth,
                query="approved regulation",
                document_id="doc_mcp",
                security_levels=["internal"],
            )
            list_documents(settings=settings, auth=mcp_auth, security_levels=["internal"])
            get_article(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp",
                article_no="제10조",
                security_levels=["internal"],
            )
            get_table(settings=settings, auth=mcp_auth, table_id="missing-table", security_levels=["internal"])
            compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_mcp",
                target_document_id="doc_mcp",
                security_levels=["internal"],
            )
            get_citation(settings=settings, auth=mcp_auth, result_id=result_id)
            get_index_status(settings=settings, auth=mcp_auth, security_levels=["internal"])

            actions = {
                json.loads(line)["action"]
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
            }

        self.assertLessEqual(
            {
                "mcp.search",
                "mcp.fetch",
                "mcp.lookup",
                "mcp.list_documents",
                "mcp.get_article",
                "mcp.get_table",
                "mcp.compare_versions",
                "mcp.get_citation",
                "mcp.index_status",
            },
            actions,
        )

    def test_create_regulation_mcp_server_registers_expected_tools(self) -> None:
        server = create_regulation_mcp_server(data_dir="data", tenant_id="tenant-a")

        tool_manager = getattr(server, "_tool_manager")
        tool_names = set(tool_manager._tools)

        self.assertLessEqual(
            {
                "search",
                "lookup",
                "fetch",
                "list_documents",
                "get_article",
                "get_table",
                "compare_versions",
                "get_citation",
                "get_index_status",
                "get_regulation_history",
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
                "get_regulation_references",
                "list_regulation_reference_cycles",
            },
            tool_names,
        )
        for tool_name in tool_names:
            annotations = tool_manager._tools[tool_name].annotations
            self.assertIsNotNone(annotations, tool_name)
            self.assertTrue(annotations.readOnlyHint, tool_name)
            self.assertFalse(annotations.destructiveHint, tool_name)
            self.assertTrue(annotations.idempotentHint, tool_name)
            self.assertFalse(annotations.openWorldHint, tool_name)

        self.assertEqual("MCP", server._reg_rag_scope["protocol"])
        self.assertEqual("regulation_mcp_server", server._reg_rag_scope["server_component"])
        self.assertEqual("external_ai_or_institution_client", server._reg_rag_scope["client_component"])

    def test_chatgpt_data_tool_profile_registers_catalog_search_and_fetch(self) -> None:
        server = create_regulation_mcp_server(data_dir="data", tenant_id="tenant-a", tool_profile="chatgpt-data")

        tool_manager = getattr(server, "_tool_manager")

        self.assertEqual(
            {
                "list_regulations",
                "get_regulation_toc",
                "get_regulation_article",
                "get_regulation_references",
                "list_regulation_reference_cycles",
                "search",
                "fetch",
            },
            set(tool_manager._tools),
        )

    def test_chatgpt_data_tool_profile_uses_exact_openai_data_source_schemas(self) -> None:
        server = create_regulation_mcp_server(
            data_dir="data",
            tenant_id="tenant-a",
            tool_profile="chatgpt-data",
            warm_cache=False,
        )
        tools = server._tool_manager._tools

        self.assertEqual({"query"}, set(tools["search"].parameters["properties"]))
        self.assertEqual(["query"], tools["search"].parameters["required"])
        self.assertEqual({"id"}, set(tools["fetch"].parameters["properties"]))
        self.assertEqual(["id"], tools["fetch"].parameters["required"])

        catalog_parameters = tools["list_regulations"].parameters
        self.assertEqual({"query", "page", "page_size"}, set(catalog_parameters["properties"]))
        self.assertEqual([], catalog_parameters.get("required", []))
        catalog_output_schema = tools["list_regulations"].output_schema
        self.assertFalse(catalog_output_schema["additionalProperties"])
        self.assertEqual(
            {"regulations", "total_count", "page", "page_size", "next_cursor"},
            set(catalog_output_schema["properties"]),
        )
        article_parameters = tools["get_regulation_article"].parameters
        self.assertEqual({"regulation_unit_id", "article_no"}, set(article_parameters["properties"]))
        self.assertEqual(
            {"regulation_unit_id", "article_no"},
            set(article_parameters["required"]),
        )
        reference_parameters = tools["get_regulation_references"].parameters
        self.assertEqual(
            {"regulation_unit_id", "direction", "status", "page", "page_size"},
            set(reference_parameters["properties"]),
        )
        self.assertEqual(
            ["regulation_unit_id"],
            reference_parameters["required"],
        )
        cycle_parameters = tools["list_regulation_reference_cycles"].parameters
        self.assertEqual(
            {"regulation_unit_id", "page", "page_size"},
            set(cycle_parameters["properties"]),
        )
        self.assertEqual([], cycle_parameters.get("required", []))

        search_output_schema = tools["search"].output_schema
        self.assertFalse(search_output_schema["additionalProperties"])
        self.assertEqual({"results"}, set(search_output_schema["properties"]))
        search_result_schema = next(iter(search_output_schema["$defs"].values()))
        self.assertFalse(search_result_schema["additionalProperties"])
        self.assertEqual({"id", "title", "url"}, set(search_result_schema["properties"]))

        fetch_output_schema = tools["fetch"].output_schema
        self.assertFalse(fetch_output_schema["additionalProperties"])
        self.assertEqual(
            {"id", "title", "text", "url", "metadata"},
            set(fetch_output_schema["properties"]),
        )
        self.assertEqual(
            {"type": "string"},
            fetch_output_schema["properties"]["metadata"]["additionalProperties"],
        )

    def test_chatgpt_data_outputs_are_narrow_and_use_openable_http_citations(self) -> None:
        rich_result = {
            "id": "opaque-result-id",
            "title": "Approved regulation",
            "url": "https://example.test/regulations/1",
            "text": "approved evidence",
            "verbatim_text": "approved evidence",
            "metadata": {
                "document_id": "internal-document-id",
                "profile_id": "internal-profile-id",
                "approval_id": "internal-approval-id",
                "document_name": "Approved regulation",
                "article_no": "Article 1",
                "source_page_start": 3,
                "source_url": "https://example.test/regulations/1",
            },
        }

        search_output = chatgpt_data_search_output(
            {"results": [rich_result], "metadata": {"trace_id": "internal-trace"}}
        ).model_dump()
        fetch_output = chatgpt_data_fetch_output(rich_result).model_dump()

        self.assertEqual(
            {
                "results": [
                    {
                        "id": "opaque-result-id",
                        "title": "Approved regulation",
                        "url": "https://example.test/regulations/1",
                    }
                ]
            },
            search_output,
        )
        self.assertEqual(
            {"id", "title", "text", "url", "metadata"},
            set(fetch_output),
        )
        self.assertEqual("3", fetch_output["metadata"]["source_page_start"])
        self.assertNotIn("document_id", fetch_output["metadata"])
        self.assertNotIn("profile_id", fetch_output["metadata"])
        self.assertNotIn("approval_id", fetch_output["metadata"])

        invalid_url = dict(rich_result, url="govreg://documents/internal")
        self.assertEqual("", chatgpt_data_fetch_output(invalid_url).url)

    def test_public_candidate_and_unresolved_reference_payloads_hide_storage_ids(self) -> None:
        candidate = regulation_tools._public_search_candidate_regulation(
            {
                "regulation_unit_id": "regunit-public",
                "regulation_title": "공개규정",
                "regulation_no": "4-1",
                "version": "rev-20260729",
                "document_id": "doc-secret",
                "chunk_id": "chunk-secret",
                "profile_id": "profile-secret",
                "version_id": "regver-secret",
            }
        )
        unresolved = regulation_tools._public_regulation_reference(
            {
                "edge_id": "edge-public",
                "edge_type": "regulation_article_reference",
                "status": "unresolved",
                "source_unit": {"title": "공개규정", "unit_id": "regunit-public"},
                "target_unit": None,
                "candidate_units": [],
                "requested_target_title": "재무규정",
                "requested_article": {"locator": "제16조", "article": "제16조"},
                "document_id": "doc-secret",
                "chunk_id": "chunk-secret",
                "profile_id": "profile-secret",
                "version_id": "regver-secret",
            }
        )
        stripped_resolved = regulation_tools._public_regulation_reference(
            {
                "edge_id": "edge-denied",
                "edge_type": "regulation_article_reference",
                "status": "resolved",
                "target_unit": None,
                "candidate_units": [],
                "requested_target_title": "권한밖 비밀규정",
                "requested_article": {"locator": "제1조", "article": "제1조"},
            }
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(
            {"regulation_title": "재무규정"},
            unresolved["target_regulation"],
        )
        self.assertEqual("제16조", unresolved["requested_article"]["locator"])
        self.assertIsNone(stripped_resolved["target_regulation"])
        public_json = json.dumps(
            {
                "candidate": candidate,
                "unresolved": unresolved,
                "stripped_resolved": stripped_resolved,
            },
            ensure_ascii=False,
        )
        self.assertNotIn("권한밖 비밀규정", public_json)
        for forbidden_key in ("document_id", "chunk_id", "profile_id", "version_id"):
            self.assertNotIn(f'"{forbidden_key}"', public_json)

    def test_historical_lookup_contract_rejects_invalid_as_of_date_for_all_content_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")

            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                search_regulations(settings=settings, auth=auth, query="regulation", as_of_date="not-a-date")
            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                get_document(settings=settings, auth=auth, document_id="doc", as_of_date="not-a-date")
            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                get_article(settings=settings, auth=auth, document_id="doc", article_no="1", as_of_date="not-a-date")
            with self.assertRaisesRegex(ValueError, "as_of_date must be an ISO date"):
                get_table(settings=settings, auth=auth, table_id="table-1", as_of_date="not-a-date")

    def test_regulation_history_exposes_effective_state_and_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root / "data", artifact_root=root)
            auth = _prepare_mcp_indexed_document(settings)
            repository = JsonRepository(settings)
            first = repository.get_document("doc_mcp")
            self.assertIsNotNone(first)
            repository.upsert_document(
                first.model_copy(
                    update={
                        "regulation_id": "reg-history",
                        "regulation_version": "v1",
                        "regulation_status": "superseded",
                        "effective_from": "2024-01-01",
                        "effective_to": "2025-12-31",
                        "profile_id": "public_portal-test-profile",
                    }
                )
            )
            second = Document(
                document_id="doc_history_v2",
                filename="history-v2.pdf",
                document_name="MCP Regulation v2",
                file_type="pdf",
                file_hash="history-v2-hash",
                tenant_id="tenant-a",
                profile_id="public_portal-test-profile",
                regulation_id="reg-history",
                regulation_version="v2",
                regulation_status="approved",
                revision_date="2025-12-15",
                effective_from="2026-01-01",
                status="completed",
            )
            repository.upsert_document(second)
            second_chunk = Chunk(
                chunk_id="history-v2-1",
                document_id="doc_history_v2",
                chunk_type="article",
                text="History version two article.",
                retrieval_text="History version two article.",
                metadata={
                    "profile_id": "public_portal-test-profile",
                    "regulation_id": "reg-history",
                    "regulation_version": "v2",
                    "regulation_status": "approved",
                    "effective_from": "2026-01-01",
                },
                security_level="internal",
            )
            repository.save_processing_result("doc_history_v2", [], [second_chunk], [])
            _approve_and_index_test_chunks(
                root,
                settings=settings,
                repository=repository,
                document_id="doc_history_v2",
                chunks=[second_chunk],
                auth=auth,
                approval_id="approval-history-v2",
            )
            repository.append_maintenance_event(
                {
                    "event_id": "history-event-1",
                    "event_type": "regulation_lifecycle_transition",
                    "created_at": "2026-01-02T00:00:00+00:00",
                    "document_id": "doc_mcp",
                    "tenant_id": "tenant-a",
                    "profile_id": "public_portal-test-profile",
                    "regulation_id": "reg-history",
                    "regulation_version": "v1",
                    "from_status": "approved",
                    "to_status": "superseded",
                    "reason": "v2 effective",
                    "actor": "reviewer",
                }
            )

            history = get_regulation_history(
                settings=settings,
                auth=auth,
                regulation_id="reg-history",
                profile_id="public_portal-test-profile",
                as_of_date="2026-02-01",
            )

        self.assertEqual("doc_history_v2", history["current_document_id"])
        self.assertEqual(2, len(history["versions"]))
        self.assertEqual(1, len(history["lifecycle_events"]))
        self.assertFalse(history["versions"][0]["is_effective_on_as_of"])
        self.assertTrue(history["versions"][1]["is_effective_on_as_of"])

    def test_warm_mcp_runtime_uses_lightweight_status_for_large_vector_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text("{}\n", encoding="utf-8")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with (
                patch("app.mcp_server.regulation_tools._MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES", 1),
                patch.object(
                    routes_rag,
                    "_load_local_vector_records",
                    side_effect=AssertionError("large startup warmup should not parse vector JSONL"),
                ),
            ):
                status = warm_mcp_runtime(settings=settings, auth=mcp_auth)

        self.assertFalse(status["warmed"])
        self.assertTrue(status["skipped"])
        self.assertEqual("lightweight", status["warmup_mode"])
        self.assertEqual("vector_store_exceeds_startup_warmup_budget", status["skip_reason"])
        self.assertFalse(status["record_count_available"])
        self.assertGreater(status["vector_byte_count"], status["warmup_max_vector_bytes"])

    def test_warm_mcp_runtime_reports_manifest_record_count_for_large_vector_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            vector_path.parent.mkdir(parents=True)
            vector_path.write_text("{}\n", encoding="utf-8")
            manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
            manifest_path.write_text(
                json.dumps({"report_type": "mcp_runtime_data_bundle", "record_count": 5000}) + "\n",
                encoding="utf-8",
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with (
                patch("app.mcp_server.regulation_tools._MCP_HEAVY_WARMUP_MAX_VECTOR_BYTES", 1),
                patch.object(
                    routes_rag,
                    "_load_local_vector_records",
                    side_effect=AssertionError("large startup warmup should not parse vector JSONL"),
                ),
            ):
                status = warm_mcp_runtime(settings=settings, auth=mcp_auth)

        self.assertFalse(status["warmed"])
        self.assertEqual(5000, status["record_count"])
        self.assertTrue(status["record_count_available"])
        self.assertEqual("mcp_runtime_manifest", status["record_count_source"])
        self.assertEqual(str(manifest_path), status["manifest_path"])

    def test_warm_mcp_runtime_reports_hierarchical_retrieval_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            bm25_path = settings.data_dir / "vector_db" / "tenant-a" / "bm25.json"
            with (
                patch.object(regulation_tools, "_verified_hierarchical_runtime_paths", return_value=(hierarchy_path, None)),
                patch.object(regulation_tools, "hierarchical_index_summary", return_value={"record_count": 3}),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value={
                        ("doc-a", "chunk-a"): {
                            "approval_id": "approval-a"
                        }
                    },
                ) as load_snapshot,
                patch.object(
                    regulation_tools.routes_rag,
                    "load_local_vector_records",
                    side_effect=AssertionError(
                        "hierarchy warmup must not load the full vector"
                    ),
                ),
                patch.object(regulation_tools.routes_rag, "bm25_index_path", return_value=bm25_path),
                patch.object(regulation_tools.routes_rag, "path_signature", return_value=None),
            ):
                status = warm_mcp_runtime(settings=settings, auth=auth)

        self.assertTrue(status["warmed"])
        self.assertTrue(status["hierarchical_index_ready"])
        self.assertTrue(status["retrieval_index_ready"])
        self.assertEqual("hierarchical_sqlite", status["retrieval_index_mode"])
        self.assertFalse(status["bm25_index_ready"])
        self.assertTrue(status["approval_snapshot_ready"])
        self.assertEqual(1, status["approval_snapshot_document_count"])
        self.assertEqual(1, status["approval_snapshot_entry_count"])
        load_snapshot.assert_called_once()

    def test_warm_mcp_runtime_primes_verified_candidate_reranker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            bm25_path = settings.data_dir / "vector_db" / "tenant-a" / "bm25.json"
            verified_runtime = (
                SimpleNamespace(document_count=3),
                bm25_path,
                ("bm25-signature",),
            )
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(hierarchy_path, Path("vector.jsonl")),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value={
                        "record_count": 3,
                        "profile_id": "profile-a",
                    },
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value={
                        ("doc-a", "chunk-a"): {
                            "approval_id": "approval-a"
                        }
                    },
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_bm25",
                    return_value=verified_runtime,
                ) as verify_bm25,
            ):
                status = warm_mcp_runtime(settings=settings, auth=auth)

        self.assertTrue(status["bm25_index_ready"])
        self.assertTrue(status["candidate_reranker_ready"])
        self.assertEqual(
            "verified_bm25_fast_query",
            status["candidate_reranker_mode"],
        )
        verify_bm25.assert_called_once_with(
            settings=settings,
            auth=auth,
            profile_id="profile-a",
        )

    def test_warm_mcp_runtime_primes_search_and_fetch_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(hierarchy_path, vector_path),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value={
                        "record_count": 3,
                        "profile_id": "profile-a",
                    },
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value={("doc-a", "chunk-a"): {"approval_id": "approval-a"}},
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_bm25",
                    return_value=("bm25-index", Path("bm25.json"), ("bm25-signature",)),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "local_vector_signature",
                    return_value=("vector-signature", 123, 456),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    side_effect=lambda path: ("sig", str(path)),
                ),
                patch.object(
                    regulation_tools,
                    "list_indexed_regulations",
                    return_value=[{"regulation_title": "직원 채용 세칙", "regulation_no": ""}],
                ),
                patch.object(
                    regulation_tools,
                    "search_regulations",
                    return_value={"results": [{"id": "result-1"}]},
                ) as search_mock,
                patch.object(
                    regulation_tools,
                    "fetch_regulation",
                    return_value={"id": "result-1"},
                ) as fetch_mock,
            ):
                status = warm_mcp_runtime(settings=settings, auth=auth)

        self.assertTrue(status["search_probe_ready"])
        self.assertTrue(status["fetch_probe_ready"])
        self.assertIn("probe_query_select_elapsed_ms", status["timing_ms"])
        self.assertIn("search_probe_elapsed_ms", status["timing_ms"])
        self.assertIn("fetch_probe_elapsed_ms", status["timing_ms"])
        self.assertEqual("profile-a", search_mock.call_args.kwargs["profile_id"])
        self.assertEqual(["internal"], search_mock.call_args.kwargs["security_levels"])
        self.assertFalse(search_mock.call_args.kwargs["settings"].api_audit_enabled)
        self.assertFalse(search_mock.call_args.kwargs["settings"].rag_trace_enabled)
        self.assertEqual("result-1", fetch_mock.call_args.kwargs["result_id"])
        self.assertFalse(fetch_mock.call_args.kwargs["settings"].api_audit_enabled)
        self.assertFalse(fetch_mock.call_args.kwargs["settings"].rag_trace_enabled)

    def test_hierarchy_warmup_probe_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(hierarchy_path, Path("vector.jsonl")),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value={
                        "record_count": 3,
                        "profile_id": "profile-a",
                    },
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value={("doc-a", "chunk-a"): {"approval_id": "approval-a"}},
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_bm25",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "local_vector_signature",
                    return_value=("vector-signature", 123, 456),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("hierarchy-signature",),
                ),
                patch.object(
                    regulation_tools,
                    "list_indexed_regulations",
                    return_value=[{"regulation_title": "직원 채용 세칙"}],
                ),
                patch.object(
                    regulation_tools,
                    "search_regulations",
                    side_effect=RuntimeError("probe failure"),
                ),
            ):
                status = warm_mcp_runtime(settings=settings, auth=auth)

        self.assertTrue(status["warmed"])
        self.assertFalse(status["search_probe_ready"])
        self.assertFalse(status["fetch_probe_ready"])

    def test_visible_records_uses_hierarchical_document_fast_path_for_document_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            record = {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "text": "approved text",
                "content_hash": "hash-a",
                "metadata": {
                    "document_id": "doc-a",
                    "chunk_id": "chunk-a",
                    "approval_status": "approved",
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "security_level": "internal",
                    "department_acl": [],
                    "profile_id": "profile-a",
                },
            }
            snapshot = {
                ("doc-a", "chunk-a"): {
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "content_hash": "hash-a",
                    "security_level": "internal",
                    "department_acl": set(),
                }
            }
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_profile_id",
                    return_value="profile-a",
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(hierarchy_path, vector_path),
                ),
                patch.object(
                    regulation_tools,
                    "load_hierarchical_document_records",
                    return_value=[record],
                ) as load_document_records,
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=("stable-signature",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    return_value=("approval-source",),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=snapshot,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "get_visible_records",
                    side_effect=AssertionError("document-scoped hierarchy fetch must not scan all visible records"),
                ),
            ):
                result = regulation_tools._visible_records(
                    settings=settings,
                    auth=auth,
                    document_id="doc-a",
                    security_levels=["internal"],
                )

        self.assertEqual([record], result)
        self.assertEqual("doc-a", load_document_records.call_args.kwargs["document_id"])

    def test_visible_records_reuses_hierarchical_document_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            record = {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "text": "approved text",
                "content_hash": "hash-a",
                "metadata": {
                    "document_id": "doc-a",
                    "chunk_id": "chunk-a",
                    "approval_status": "approved",
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "security_level": "internal",
                    "department_acl": [],
                    "profile_id": "profile-a",
                },
            }
            snapshot = {
                ("doc-a", "chunk-a"): {
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "content_hash": "hash-a",
                    "security_level": "internal",
                    "department_acl": set(),
                }
            }
            regulation_tools._VISIBLE_DOCUMENT_RECORD_CACHE.clear()
            try:
                with (
                    patch.object(
                        regulation_tools,
                        "_verified_hierarchical_runtime_profile_id",
                        return_value="profile-a",
                    ),
                    patch.object(
                        regulation_tools,
                        "_verified_hierarchical_runtime_paths",
                        return_value=(hierarchy_path, vector_path),
                    ),
                    patch.object(
                        regulation_tools.routes_rag,
                        "path_signature",
                        side_effect=lambda path: ("sig", str(path)),
                    ),
                    patch.object(
                        regulation_tools.routes_rag,
                        "runtime_approval_snapshot_identity",
                        return_value=("approval-source",),
                    ),
                    patch.object(
                        regulation_tools.routes_rag,
                        "load_cached_runtime_approval_snapshot",
                        return_value=snapshot,
                    ),
                    patch.object(
                        regulation_tools,
                        "load_hierarchical_document_records",
                        return_value=[record],
                    ) as load_document_records,
                ):
                    first = regulation_tools._visible_records(
                        settings=settings,
                        auth=auth,
                        document_id="doc-a",
                        security_levels=["internal"],
                    )
                    second = regulation_tools._visible_records(
                        settings=settings,
                        auth=auth,
                        document_id="doc-a",
                        security_levels=["internal"],
                    )
            finally:
                regulation_tools._VISIBLE_DOCUMENT_RECORD_CACHE.clear()

        self.assertEqual([record], first)
        self.assertEqual([record], second)
        self.assertEqual(1, load_document_records.call_count)

    def test_visible_records_rechecks_approval_after_runtime_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            vector_path = (
                settings.data_dir
                / "vector_db"
                / "tenant-a"
                / "approved_vectors.jsonl"
            )
            record = {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "text": "approved text",
                "content_hash": "hash-a",
                "metadata": {
                    "document_id": "doc-a",
                    "chunk_id": "chunk-a",
                    "approval_status": "approved",
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "security_level": "internal",
                    "department_acl": [],
                    "profile_id": "profile-a",
                },
            }
            approved_snapshot = {
                ("doc-a", "chunk-a"): {
                    "approval_id": "approval-a",
                    "approved_content_hash": "approved-hash-a",
                    "content_hash": "hash-a",
                    "security_level": "internal",
                    "department_acl": set(),
                }
            }
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_profile_id",
                    return_value="profile-a",
                ),
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(hierarchy_path, vector_path),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    side_effect=lambda path: ("sig", str(path)),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "runtime_approval_snapshot_identity",
                    side_effect=[
                        ("approval-source-before",),
                        ("approval-source-before",),
                        ("approval-source-after",),
                        ("approval-source-after",),
                    ],
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    side_effect=[approved_snapshot, {}],
                ),
                patch.object(
                    regulation_tools,
                    "load_hierarchical_document_records",
                    return_value=[record],
                ) as load_document_records,
                patch.object(
                    regulation_tools.routes_rag,
                    "get_visible_records",
                    side_effect=AssertionError(
                        "a verified revoked snapshot must fail closed in the fast path"
                    ),
                ),
            ):
                before_revocation = regulation_tools._visible_records(
                    settings=settings,
                    auth=auth,
                    document_id="doc-a",
                    security_levels=["internal"],
                )
                after_revocation = regulation_tools._visible_records(
                    settings=settings,
                    auth=auth,
                    document_id="doc-a",
                    security_levels=["internal"],
                )

        self.assertEqual([record], before_revocation)
        self.assertEqual([], after_revocation)
        self.assertEqual(2, load_document_records.call_count)

    def test_hierarchy_warmup_reports_stale_approval_sidecar_without_vector_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = mcp_auth_context(tenant_id="tenant-a")
            hierarchy_path = settings.data_dir / "hierarchy" / "regulations.sqlite"
            with (
                patch.object(
                    regulation_tools,
                    "_verified_hierarchical_runtime_paths",
                    return_value=(hierarchy_path, Path("vector.jsonl")),
                ),
                patch.object(
                    regulation_tools,
                    "hierarchical_index_summary",
                    return_value={
                        "record_count": 3,
                        "profile_id": "profile-a",
                    },
                ),
                patch.object(
                    regulation_tools,
                    "indexed_document_ids",
                    return_value={"doc-a"},
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_cached_runtime_approval_snapshot",
                    return_value=None,
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "load_local_vector_records",
                    side_effect=AssertionError(
                        "stale sidecar warmup must not scan the vector"
                    ),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "bm25_index_path",
                    return_value=Path("bm25.json"),
                ),
                patch.object(
                    regulation_tools.routes_rag,
                    "path_signature",
                    return_value=None,
                ),
            ):
                status = warm_mcp_runtime(settings=settings, auth=auth)

        self.assertTrue(status["warmed"])
        self.assertFalse(status["approval_snapshot_ready"])
        self.assertEqual(1, status["approval_snapshot_document_count"])
        self.assertEqual(0, status["approval_snapshot_entry_count"])

    def test_get_index_status_reports_mcp_visible_vector_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            JsonRepository(settings).upsert_document(
                Document(
                    document_id="doc_unapproved",
                    filename="draft.pdf",
                    document_name="Draft Only",
                    file_type="pdf",
                    file_hash="draft-hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            status = get_index_status(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp",
                security_levels=["internal"],
            )
            all_status = get_index_status(settings=settings, auth=mcp_auth, security_levels=["internal"])
            draft_status = get_index_status(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_unapproved",
                security_levels=["internal"],
            )

        self.assertEqual(status["summary"]["document_count"], 1)
        self.assertEqual(status["documents"][0]["indexing_status"], "indexed")
        self.assertEqual(status["documents"][0]["approved_record_count"], 1)
        self.assertEqual(status["documents"][0]["vector_record_count"], 1)
        self.assertEqual(status["documents"][0]["latest_job"]["target_type"], "local-jsonl")
        self.assertEqual([item["document_id"] for item in all_status["documents"]], ["doc_mcp"])
        self.assertEqual(draft_status["documents"], [])

    def test_get_index_status_ignores_manifest_only_approval_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            _prepare_mcp_indexed_document(settings)
            manifest_path = settings.data_dir / "repository" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("approvals", {})["forged-manifest-only"] = {
                "approval_record_id": "forged-manifest-only",
                "approval_id": "forged-manifest-only",
                "document_id": "doc_mcp",
                "tenant_id": "tenant-a",
                "chunk_ids": ["approved-1"],
                "approved_at": "2026-07-10T00:00:00+00:00",
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            status = get_index_status(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp",
                security_levels=["internal"],
            )

        self.assertEqual(status["documents"][0]["indexing_status"], "indexed")
        self.assertEqual(status["documents"][0]["approved_record_count"], 1)

    def test_get_index_status_hides_cross_tenant_documents_in_flat_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data", tenant_storage_isolation=False)
            tenant_b_auth = AuthContext(actor="tester", tenant_id="tenant-b", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_tenant_b",
                "타기관 승인 규정은 보이면 안 된다.",
                "approval-tenant-b",
                tenant_b_auth,
                tenant_id="tenant-b",
            )
            tenant_a_mcp = mcp_auth_context(tenant_id="tenant-a")

            status = get_index_status(settings=settings, auth=tenant_a_mcp, security_levels=["internal"])

        self.assertEqual(status["documents"], [])
        self.assertEqual(status["summary"]["document_count"], 0)

    def test_mcp_settings_prefers_runtime_manifest_over_stale_tenant_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            (data_dir / "tenants" / "default").mkdir(parents=True)
            data_dir.mkdir(exist_ok=True)
            (data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"tenant_storage_isolation": False}, ensure_ascii=False),
                encoding="utf-8",
            )

            settings = settings_for_mcp_project(data_dir=data_dir, tenant_id="default")

        self.assertFalse(settings.tenant_storage_isolation)
        self.assertEqual(settings.data_dir, data_dir)

    def test_mcp_viewer_cannot_request_confidential_security_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            admin_auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(
                settings,
                "doc_confidential",
                "비공개 규정 내용",
                "approval-confidential",
                admin_auth,
                security_level="confidential",
            )
            viewer_mcp = mcp_auth_context(tenant_id="tenant-a", role="viewer")

            with self.assertRaisesRegex(ValueError, "security level"):
                search_regulations(
                    settings=settings,
                    auth=viewer_mcp,
                    query="비공개",
                    security_levels=["confidential"],
                )
            visible_status = get_index_status(settings=settings, auth=viewer_mcp)
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(visible_status["documents"], [])
        denied_search_rows = [row for row in rows if row["action"] == "mcp.search" and row["outcome"] == "denied"]
        self.assertEqual(1, len(denied_search_rows))
        self.assertEqual(403, denied_search_rows[0]["status_code"])

    def test_mcp_fetch_invalid_result_id_writes_failure_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with self.assertRaisesRegex(ValueError, "Invalid regulation result id"):
                fetch_regulation(settings=settings, auth=mcp_auth, result_id="not-base64")
            rows = [
                json.loads(line)
                for line in api_audit_path(settings).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        failure_rows = [row for row in rows if row["action"] == "mcp.fetch" and row["outcome"] == "failure"]
        self.assertEqual(1, len(failure_rows))
        self.assertEqual(400, failure_rows[0]["status_code"])

    def test_get_table_returns_only_approved_table_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_table",
                    filename="table.pdf",
                    document_name="MCP Table",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_table",
                [],
                [
                    Chunk(
                        chunk_id="table-1",
                        document_id="doc_mcp_table",
                        chunk_type="appendix",
                        text="구분 내용\n육아휴직 신청 가능",
                        retrieval_text="구분 내용\n육아휴직 신청 가능",
                        metadata={
                            "table_like": True,
                            "table_id": "leave-table",
                            "table_title": "휴직 표",
                            "table_rows": ["구분 내용", "육아휴직 신청 가능"],
                        },
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="table-draft",
                        document_id="doc_mcp_table",
                        chunk_type="appendix",
                        text="draft table",
                        retrieval_text="draft table",
                        metadata={"table_like": True, "table_id": "draft-table", "table_rows": ["draft"]},
                        security_level="internal",
                    ),
                ],
                [],
            )

            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_table",
                chunks=[chunk for chunk in repository.get_chunks("doc_mcp_table") if chunk.chunk_id == "table-1"],
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["table-1"],
                        approval_id="approval-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            table = get_table(
                settings=settings,
                auth=mcp_auth,
                table_id="leave-table",
                security_levels=["internal"],
            )
            draft = get_table(
                settings=settings,
                auth=mcp_auth,
                table_id="draft-table",
                security_levels=["internal"],
            )

        self.assertEqual(len(table["tables"]), 1)
        self.assertEqual(table["tables"][0]["chunk_id"], "table-1")
        self.assertTrue(table["tables"][0]["rows"])
        self.assertEqual(table["tables"][0]["verbatim_text"], table["tables"][0]["text"])
        self.assertTrue(table["tables"][0]["verbatim"]["is_verbatim"])
        self.assertEqual(draft["tables"], [])

    def test_get_table_validates_repository_without_snapshot_preload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_fast_table",
                    filename="table.pdf",
                    document_name="Fast MCP Table",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_fast_table",
                [],
                [
                    Chunk(
                        chunk_id="table-1",
                        document_id="doc_mcp_fast_table",
                        chunk_type="table",
                        text="approved table",
                        retrieval_text="approved table",
                        metadata={"table_like": True, "table_id": "fast-table", "table_rows": ["approved table"]},
                        security_level="internal",
                    )
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            chunks = repository.get_chunks("doc_mcp_fast_table")
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_fast_table",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_fast_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["table-1"],
                        approval_id="approval-fast-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_fast_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            with patch.object(
                routes_rag,
                "_load_cached_approval_snapshot",
                side_effect=AssertionError("get_table should not preload approval snapshot"),
            ):
                result = get_table(
                    settings=settings,
                    auth=mcp_auth,
                    document_id="doc_mcp_fast_table",
                    table_id="fast-table",
                    security_levels=["internal"],
                )

        self.assertEqual(len(result["tables"]), 1)
        self.assertEqual(result["tables"][0]["chunk_id"], "table-1")

    def test_get_table_resolves_appendix_alias_to_kordoc_inventory_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_table",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_kordoc_table",
                [],
                [
                    Chunk(
                        chunk_id="doc_mcp_kordoc_table_appendix_\ubcc4\ud45c1_0001",
                        document_id="doc_mcp_kordoc_table",
                        chunk_type="appendix",
                        text="\ubcc4\ud45c1 \ubcf8\ubd80 \uc704\uc784\uc804\uacb0\uc0ac\ud56d",
                        retrieval_text="\ubcc4\ud45c1 \ubcf8\ubd80 \uc704\uc784\uc804\uacb0\uc0ac\ud56d",
                        metadata={
                            "table_like": True,
                            "hierarchy_path": "Delegation Rule > \ubcc4\ud45c1",
                            "table_cell_rows": [
                                {
                                    "row_index": 0,
                                    "cells": ["legacy", "row"],
                                    "raw": "legacy | row",
                                }
                            ],
                            "kordoc_table_inventory": {
                                "status": "parsed",
                                "parser": "kordoc",
                                "table_count": 1,
                                "stored_table_count": 1,
                                "tables": [
                                    {
                                        "table_index": 1,
                                        "row_count": 2,
                                        "column_count": 4,
                                        "cell_rows": [
                                            {
                                                "row_index": 0,
                                                "cells": ["No", "Task", "President", "Director"],
                                                "raw": "No | Task | President | Director",
                                            },
                                            {
                                                "row_index": 1,
                                                "cells": ["1", "Plan approval", "", "O"],
                                                "raw": "1 | Plan approval |  | O",
                                            },
                                        ],
                                    }
                                ],
                            },
                        },
                        security_level="internal",
                    ),
                    Chunk(
                        chunk_id="doc_mcp_kordoc_table_appendix_\ubcc4\ud45c1_0002",
                        document_id="doc_mcp_kordoc_table",
                        chunk_type="appendix",
                        text="\ubcc4\ud45c1 text-only continuation",
                        retrieval_text="\ubcc4\ud45c1 text-only continuation",
                        metadata={
                            "table_like": True,
                            "table_appendix_no": "\ubcc4\ud45c1",
                            "table_rows": ["text-only row should not be returned for appendix alias"],
                        },
                        security_level="internal",
                    ),
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            chunks = repository.get_chunks("doc_mcp_kordoc_table")
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_table",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_table",
                table_id="\ubcc4\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertTrue(table["metadata"]["kordoc_table_inventory_fallback"])
        self.assertEqual(table["metadata"]["table_source"], "kordoc")
        self.assertEqual(table["metadata"]["kordoc_table_index"], 1)
        self.assertEqual(table["rows"][0]["cells"], ["No", "Task", "President", "Director"])
        self.assertEqual(table["rows"][1]["cells"], ["1", "Plan approval", "", "O"])
        self.assertNotEqual(table["rows"][0]["cells"], ["legacy", "row"])

    def test_get_table_resolves_korean_alias_when_hwp_appendix_label_is_mojibake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            mojibake_appendix_label = "\ubcc4\ud45c".encode("utf-8").decode("cp949", errors="ignore")
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_mojibake",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_kordoc_mojibake",
                [],
                [
                    Chunk(
                        chunk_id=f"doc_mcp_kordoc_mojibake_appendix_{mojibake_appendix_label}1_0001",
                        document_id="doc_mcp_kordoc_mojibake",
                        chunk_type="appendix",
                        text=f"{mojibake_appendix_label}1 legacy label",
                        retrieval_text=f"{mojibake_appendix_label}1 legacy label",
                        metadata={
                            "table_like": True,
                            "hierarchy_path": f"Delegation Rule > {mojibake_appendix_label}1",
                            "kordoc_table_inventory": {
                                "status": "parsed",
                                "parser": "kordoc",
                                "table_count": 1,
                                "stored_table_count": 1,
                                "tables": [
                                    {
                                        "table_index": 1,
                                        "row_count": 1,
                                        "column_count": 2,
                                        "cell_rows": [
                                            {
                                                "row_index": 0,
                                                "cells": ["Task", "Owner"],
                                                "raw": "Task | Owner",
                                            },
                                        ],
                                    }
                                ],
                            },
                        },
                        security_level="internal",
                    ),
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            chunks = repository.get_chunks("doc_mcp_kordoc_mojibake")
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_mojibake",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_mojibake",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-mojibake",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_mojibake",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_mojibake",
                table_id="\ubcc4\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertTrue(table["metadata"]["kordoc_table_inventory_fallback"])
        self.assertEqual(table["metadata"]["table_source"], "kordoc")
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Owner"])

    def test_get_table_deduplicates_replicated_kordoc_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_dedup",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            inventory = {
                "status": "parsed",
                "parser": "kordoc",
                "table_count": 1,
                "stored_table_count": 1,
                "tables": [
                    {
                        "table_index": 1,
                        "row_count": 2,
                        "column_count": 2,
                        "cell_rows": [
                            {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"},
                            {"row_index": 1, "cells": ["Plan", "Director"], "raw": "Plan | Director"},
                        ],
                    }
                ],
            }
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_dedup_appendix_1",
                    document_id="doc_mcp_kordoc_dedup",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 first carrier",
                    retrieval_text="\ubcc4\ud45c1 first carrier",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "kordoc_table_inventory": inventory,
                    },
                    security_level="internal",
                ),
                Chunk(
                    chunk_id="doc_mcp_kordoc_dedup_appendix_1_copy",
                    document_id="doc_mcp_kordoc_dedup",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 duplicated inventory carrier",
                    retrieval_text="\ubcc4\ud45c1 duplicated inventory carrier",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "kordoc_table_inventory": inventory,
                    },
                    security_level="internal",
                ),
            ]
            repository.save_processing_result("doc_mcp_kordoc_dedup", [], chunks, [])
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_dedup",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_dedup",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-dedup",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_dedup",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_dedup",
                table_id="\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertTrue(table["metadata"]["kordoc_table_inventory_fallback"])
        self.assertEqual(table["metadata"]["table_source"], "kordoc")
        self.assertEqual(table["metadata"]["kordoc_table_index"], 1)
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Approver"])

    def test_get_table_falls_back_when_kordoc_inventory_match_has_no_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_rowless",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_rowless_appendix_1",
                    document_id="doc_mcp_kordoc_rowless",
                    chunk_type="appendix",
                    text="별표1 fallback table",
                    retrieval_text="별표1 fallback table",
                    metadata={
                        "table_like": True,
                        "table_id": "별표 1",
                        "table_appendix_no": "별표1",
                        "table_cell_rows": [
                            {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"},
                            {"row_index": 1, "cells": ["Plan", "Director"], "raw": "Plan | Director"},
                        ],
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 0,
                                    "column_count": 0,
                                    "cell_rows": [],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                )
            ]
            repository.save_processing_result("doc_mcp_kordoc_rowless", [], chunks, [])
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_kordoc_rowless",
                chunks=chunks,
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_kordoc_rowless",
                    routes_documents.ApprovalRequest(
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        approval_id="approval-kordoc-rowless",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_kordoc_rowless",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            result = get_table(
                settings=settings,
                auth=mcp_auth,
                document_id="doc_mcp_kordoc_rowless",
                table_id="별표 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertEqual(table["chunk_id"], "doc_mcp_kordoc_rowless_appendix_1")
        self.assertFalse(table["metadata"].get("kordoc_table_inventory_fallback", False))
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Approver"])
        self.assertEqual(table["rows"][1]["cells"], ["Plan", "Director"])

    def test_get_table_does_not_resolve_byeolji_alias_to_byeolpyo_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_byeolji_guard",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_byeolji_guard_appendix_1",
                    document_id="doc_mcp_kordoc_byeolji_guard",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 appendix table",
                    retrieval_text="\ubcc4\ud45c1 appendix table",
                    metadata={
                        "table_like": True,
                        "hierarchy_path": "Delegation Rule > \ubcc4\ud45c1",
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 1,
                                    "column_count": 2,
                                    "cell_rows": [
                                        {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"}
                                    ],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                )
            ]
            repository.save_processing_result("doc_mcp_kordoc_byeolji_guard", [], chunks, [])
            _approve_and_index_test_chunks(
                Path(tmp),
                settings=settings,
                repository=repository,
                document_id="doc_mcp_kordoc_byeolji_guard",
                chunks=chunks,
                auth=auth,
                approval_id="approval-kordoc-byeolji-guard",
            )

            result = get_table(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                document_id="doc_mcp_kordoc_byeolji_guard",
                table_id="\ubcc4\uc9c0 1",
                security_levels=["internal"],
            )

        self.assertEqual(result["tables"], [])

    def test_get_table_falls_back_from_rowless_inventory_using_table_appendix_no_without_table_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_appendix_only",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_appendix_only_appendix_1",
                    document_id="doc_mcp_kordoc_appendix_only",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 fallback table",
                    retrieval_text="\ubcc4\ud45c1 fallback table",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "table_cell_rows": [
                            {"row_index": 0, "cells": ["Task", "Approver"], "raw": "Task | Approver"},
                            {"row_index": 1, "cells": ["Plan", "Director"], "raw": "Plan | Director"},
                        ],
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 0,
                                    "column_count": 0,
                                    "cell_rows": [],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                )
            ]
            repository.save_processing_result("doc_mcp_kordoc_appendix_only", [], chunks, [])
            _approve_and_index_test_chunks(
                Path(tmp),
                settings=settings,
                repository=repository,
                document_id="doc_mcp_kordoc_appendix_only",
                chunks=chunks,
                auth=auth,
                approval_id="approval-kordoc-appendix-only",
            )

            result = get_table(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                document_id="doc_mcp_kordoc_appendix_only",
                table_id="\ubcc4\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertEqual(table["chunk_id"], "doc_mcp_kordoc_appendix_only_appendix_1")
        self.assertFalse(table["metadata"].get("kordoc_table_inventory_fallback", False))
        self.assertEqual(table["rows"][0]["cells"], ["Task", "Approver"])

    def test_get_table_keeps_distinct_kordoc_tables_when_appendices_share_table_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_kordoc_same_index",
                    filename="delegation.hwp",
                    document_name="Delegation Rule",
                    file_type="hwp",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            chunks = [
                Chunk(
                    chunk_id="doc_mcp_kordoc_same_index_appendix_1",
                    document_id="doc_mcp_kordoc_same_index",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c1 first table",
                    retrieval_text="\ubcc4\ud45c1 first table",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c1",
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 1,
                                    "column_count": 2,
                                    "cell_rows": [
                                        {"row_index": 0, "cells": ["First", "Approver"], "raw": "First | Approver"}
                                    ],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                ),
                Chunk(
                    chunk_id="doc_mcp_kordoc_same_index_appendix_2",
                    document_id="doc_mcp_kordoc_same_index",
                    chunk_type="appendix",
                    text="\ubcc4\ud45c2 second table",
                    retrieval_text="\ubcc4\ud45c2 second table",
                    metadata={
                        "table_like": True,
                        "table_appendix_no": "\ubcc4\ud45c2",
                        "kordoc_table_inventory": {
                            "status": "parsed",
                            "parser": "kordoc",
                            "table_count": 1,
                            "stored_table_count": 1,
                            "tables": [
                                {
                                    "table_index": 1,
                                    "row_count": 1,
                                    "column_count": 2,
                                    "cell_rows": [
                                        {"row_index": 0, "cells": ["Second", "Director"], "raw": "Second | Director"}
                                    ],
                                }
                            ],
                        },
                    },
                    security_level="internal",
                ),
            ]
            repository.save_processing_result("doc_mcp_kordoc_same_index", [], chunks, [])
            _approve_and_index_test_chunks(
                Path(tmp),
                settings=settings,
                repository=repository,
                document_id="doc_mcp_kordoc_same_index",
                chunks=chunks,
                auth=auth,
                approval_id="approval-kordoc-same-index",
            )

            result = get_table(
                settings=settings,
                auth=mcp_auth_context(tenant_id="tenant-a"),
                document_id="doc_mcp_kordoc_same_index",
                table_id="\ud45c 1",
                security_levels=["internal"],
            )

        self.assertEqual(len(result["tables"]), 2)
        first_cells = [table["rows"][0]["cells"] for table in result["tables"]]
        self.assertIn(["First", "Approver"], first_cells)
        self.assertIn(["Second", "Director"], first_cells)

    def test_get_table_rejects_sidecar_visible_chunk_when_repository_chunk_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            repository = JsonRepository(settings)
            repository.upsert_document(
                Document(
                    document_id="doc_mcp_table",
                    filename="table.pdf",
                    document_name="MCP Table",
                    file_type="pdf",
                    file_hash="hash",
                    tenant_id="tenant-a",
                    status="completed",
                )
            )
            repository.save_processing_result(
                "doc_mcp_table",
                [],
                [
                    Chunk(
                        chunk_id="table-1",
                        document_id="doc_mcp_table",
                        chunk_type="table",
                        text="approved table",
                        retrieval_text="approved table",
                        metadata={"table_like": True, "table_id": "leave-table", "table_rows": ["approved table"]},
                        security_level="internal",
                    ),
                ],
                [],
            )
            approval_settings = replace(settings, artifact_root=Path(tmp))
            evidence = _write_approval_evidence(
                Path(tmp),
                settings=approval_settings,
                document_id="doc_mcp_table",
                chunks=repository.get_chunks("doc_mcp_table"),
            )
            with patch.object(routes_documents, "get_settings", return_value=approval_settings):
                routes_documents.approve_review_chunks(
                    "doc_mcp_table",
                    routes_documents.ApprovalRequest(
                        chunk_ids=["table-1"],
                        approval_id="approval-table",
                        security_level="internal",
                        **evidence,
                    ),
                    auth,
                )
                routes_documents.index_document(
                    "doc_mcp_table",
                    routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                    auth,
                )
            vector_path = settings.data_dir / "vector_db" / "tenant-a" / "approved_vectors.jsonl"
            records = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            _write_runtime_approval_snapshot_sidecar(settings.data_dir, records, tenant_id="tenant-a")
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            before = get_table(settings=settings, auth=mcp_auth, table_id="leave-table", security_levels=["internal"])
            chunks_path = settings.data_dir / "repository" / "doc_mcp_table_chunks.json"
            chunks_payload = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks_payload[0]["text"] = "drifted unapproved table"
            chunks_payload[0]["retrieval_text"] = "drifted unapproved table"
            chunks_path.write_text(json.dumps(chunks_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            after = get_table(settings=settings, auth=mcp_auth, table_id="leave-table", security_levels=["internal"])

        self.assertEqual(1, len(before["tables"]))
        self.assertEqual([], after["tables"])

    def test_compare_versions_reports_changed_approved_articles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(settings, "doc_v1", "육아휴직은 1년 이내로 한다.", "approval-v1", auth)
            _save_document_with_one_chunk(settings, "doc_v2", "육아휴직은 2년 이내로 한다.", "approval-v2", auth)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            comparison = compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_v1",
                target_document_id="doc_v2",
                security_levels=["internal"],
            )

        self.assertEqual(comparison["summary"]["changed_count"], 1)
        self.assertEqual(comparison["changed"][0]["key"], "제10조")

    def test_compare_versions_does_not_report_same_approved_article_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_one_chunk(settings, "doc_same_v1", "육아휴직은 1년 이내로 한다.", "approval-same-v1", auth)
            _save_document_with_one_chunk(settings, "doc_same_v2", "육아휴직은 1년 이내로 한다.", "approval-same-v2", auth)
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            comparison = compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_same_v1",
                target_document_id="doc_same_v2",
                security_levels=["internal"],
            )

        self.assertEqual(comparison["summary"]["changed_count"], 0)

    def test_compare_versions_detects_changed_material_in_multi_chunk_article(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp) / "data")
            auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
            _save_document_with_chunks(
                settings,
                "doc_multi_v1",
                ["article opening old", "article closing same"],
                "approval-multi-v1",
                auth,
                article_no="Article 10",
            )
            _save_document_with_chunks(
                settings,
                "doc_multi_v2",
                ["article opening new", "article closing same"],
                "approval-multi-v2",
                auth,
                article_no="Article 10",
            )
            mcp_auth = mcp_auth_context(tenant_id="tenant-a")

            comparison = compare_versions(
                settings=settings,
                auth=mcp_auth,
                base_document_id="doc_multi_v1",
                target_document_id="doc_multi_v2",
                security_levels=["internal"],
            )

        self.assertEqual(comparison["summary"]["base_item_count"], 1)
        self.assertEqual(comparison["summary"]["changed_count"], 1)
        self.assertEqual(comparison["changed"][0]["key"], "Article 10")
        self.assertEqual(comparison["changed"][0]["target"]["chunk_count"], 2)
        self.assertIn("article opening new", comparison["changed"][0]["target"]["text_preview"])


def _write_bound_hierarchy_bm25_fixture(
    settings: Settings,
    *,
    profile_id: str,
) -> tuple[Path, Path, Path]:
    auth = mcp_auth_context(tenant_id="tenant-a")
    metadata = {
        "document_id": "doc-binding",
        "chunk_id": "chunk-binding",
        "tenant_id": "tenant-a",
        "profile_id": profile_id,
        "institution_name": "Test Institution",
        "document_name": "Binding Regulation",
        "regulation_no": "1-1",
        "regulation_title": "Binding Regulation",
        "regulation_status": "approved",
        "regulation_version": "v1",
        "revision_date": "2026-07-01",
        "effective_from": "2026-07-01",
        "chunk_type": "article",
        "hierarchy_path": "Binding Regulation > Article 1",
        "article_no": "Article 1",
        "article_title": "Purpose",
        "approval_status": "approved",
        "approval_id": "approval-binding",
        "approved_content_hash": "approved-binding",
        "security_level": "internal",
        "department_acl": [],
    }
    text = "approved policy binding evidence"
    record = {
        "schema_version": "reg-rag-vector-record-v1",
        "id": "doc-binding:chunk-binding",
        "document_id": "doc-binding",
        "chunk_id": "chunk-binding",
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }
    vector_path = regulation_tools.routes_rag.local_vector_path(settings, auth)
    offsets = write_vector_records_with_offsets(vector_path, [record])
    hierarchy_path = hierarchical_index_path(settings.data_dir)
    hierarchy = build_hierarchical_runtime_index(
        hierarchy_path,
        [record],
        tenant_id="tenant-a",
        profile_id=profile_id,
        vector_offsets=offsets,
    )
    bm25_path = regulation_tools.routes_rag.bm25_index_path(
        settings=settings,
        auth=auth,
    )
    bm25_path.parent.mkdir(parents=True, exist_ok=True)
    bm25_index = Bm25Index.build([record])
    bm25_path.write_text(
        json.dumps(bm25_index.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    relative_bm25_path = bm25_path.relative_to(settings.data_dir).as_posix()
    manifest_path = settings.data_dir / "mcp_runtime_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": "tenant-a",
                "profile_id": profile_id,
                "record_count": 1,
                "files": {
                    "hierarchical_index_sha256": hierarchy["sha256"],
                },
                "runtime_data_reuse": {
                    "file_sha256": {
                        relative_bm25_path: hashlib.sha256(
                            bm25_path.read_bytes()
                        ).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, hierarchy_path, bm25_path


def _prepare_mcp_indexed_document(settings: Settings) -> AuthContext:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc_mcp",
            filename="mcp.pdf",
            document_name="MCP Regulation",
            file_type="pdf",
            file_hash="hash",
            tenant_id="tenant-a",
            status="completed",
        )
    )
    repository.save_processing_result(
        "doc_mcp",
        [],
        [
            Chunk(
                chunk_id="approved-1",
                document_id="doc_mcp",
                chunk_type="article",
                text="육아휴직은 승인된 규정에 따라 신청할 수 있다.",
                retrieval_text="육아휴직은 승인된 규정에 따라 신청할 수 있다.",
                metadata={
                    "article_no": "제10조",
                    "article_title": "육아휴직",
                    "source_system": "PUBLIC_PORTAL",
                    "source_url": "https://example.test/public_portal/doc_mcp",
                    "source_record_id": "record-doc-mcp",
                    "source_file_id": "file-doc-mcp",
                    "profile_id": "public_portal-test-profile",
                    "answer_profile_version": "reg-rag-answer-profile-v1",
                    "answer_intents": ["duration"],
                    "answer_keywords": ["육아휴직", "기간"],
                    "answer_facts": [
                        {
                            "type": "duration",
                            "value": "3년",
                            "sentence": "자녀 1명에 대하여 3년 이내로 한다.",
                        }
                    ],
                    "answer_outline": ["자녀 1명에 대하여 3년 이내로 한다."],
                },
                security_level="internal",
            ),
            Chunk(
                chunk_id="draft-1",
                document_id="doc_mcp",
                chunk_type="article",
                text="검수 전 초안은 MCP에 노출되지 않는다.",
                retrieval_text="검수 전 초안은 MCP에 노출되지 않는다.",
                security_level="internal",
            ),
        ],
        [],
    )
    chunks = repository.get_chunks("doc_mcp")
    chunks[0].metadata.update(
        {
            "parser_uncertainty_source": "hwp",
            "parser_uncertainty_risk_level": "medium",
            "parser_uncertainty_confidence": 0.72,
            "parser_uncertainty_flags": ["native_table_geometry_unavailable"],
            "parser_uncertainty_recommendation": "review_tables_and_appendices",
            "parser_uncertainty_remediation_hint": "Compare table/form geometry with source HWP.",
        }
    )
    repository.save_chunks("doc_mcp", chunks)
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    evidence = _write_approval_evidence(
        approval_settings.artifact_root,
        settings=approval_settings,
        document_id="doc_mcp",
        chunks=[chunks[0]],
    )
    auth = AuthContext(actor="tester", tenant_id="tenant-a", auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            "doc_mcp",
            routes_documents.ApprovalRequest(
                chunk_ids=["approved-1"],
                approval_id="approval-mcp",
                security_level="internal",
                review_flags_acknowledged=True,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            "doc_mcp",
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )
    return auth


def _write_approval_evidence(
    root: Path,
    *,
    settings: Settings,
    document_id: str,
    chunks: list[Chunk],
    tenant_id: str = "tenant-a",
) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    worklist_path = reports / "approval_worklist_current.json"
    batch_manifest_path = reports / "approval_review_batches_current.json"
    chunk_ids = [chunk.chunk_id for chunk in chunks]

    worklist = {
        "report_type": "approval_worklist",
        "generated_at": "2026-07-09T00:00:00+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": False,
        "document_count": 1,
        "total_chunks": len(chunks),
        "manual_attention_chunks": 0,
        "low_risk_batch_review_candidate_chunks": len(chunks),
        "documents": [
            {
                "document_id": document_id,
                "document_name": document_id,
                "filename": f"{document_id}.pdf",
                "total_chunks": len(chunks),
                "draft_chunks": len(chunks),
                "low_risk_batch_review_candidate_chunks": len(chunks),
            }
        ],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2), encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)

    batch_chunks = [
        {
            "chunk_id": chunk.chunk_id,
            "review_content_hash": routes_documents._review_content_hash(chunk),
            "approval_status": chunk.approval_status,
            "review_priority_tier": "no_signal",
            "review_category": "low_risk_batch_review_candidate",
            "attention_reasons": [],
        }
        for chunk in chunks
    ]
    review_type = "low_risk_batch"
    batch_fingerprint = routes_documents._review_batch_chunk_fingerprint(batch_chunks, review_type)
    batch_id = f"approval-{worklist_sha256[:12]}-001-low-risk-batch-001-{batch_fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": "2026-07-09T00:00:01+00:00",
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": False,
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": "reports/approval_worklist_current.json",
            "sha256": worklist_sha256,
            "effective_data_dir": str(settings.data_dir),
            "tenant_id": tenant_id,
            "tenant_storage_isolation": False,
            "document_count": 1,
            "total_chunks": len(chunks),
            "manual_attention_chunks": 0,
            "low_risk_batch_review_candidate_chunks": len(chunks),
        },
        "batch_count": 1,
        "approval_chunk_count": len(chunks),
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": batch_fingerprint,
                "review_type": review_type,
                "review_strategy": "human_bulk_review",
                "document_id": document_id,
                "document_name": document_id,
                "filename": f"{document_id}.pdf",
                "chunk_count": len(chunks),
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
                "review_flags_acknowledged_required": False,
            }
        ],
    }
    batch_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": worklist_sha256,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": _sha256_file(batch_manifest_path),
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": batch_fingerprint,
        "review_strategy": "human_bulk_review",
    }


def _approve_and_index_test_chunks(
    root: Path,
    *,
    settings: Settings,
    repository: JsonRepository,
    document_id: str,
    chunks: list[Chunk],
    auth: AuthContext,
    approval_id: str,
) -> None:
    approval_settings = replace(settings, artifact_root=root)
    evidence = _write_approval_evidence(
        root,
        settings=approval_settings,
        document_id=document_id,
        chunks=chunks,
        tenant_id=auth.tenant_id,
    )
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[chunk.chunk_id for chunk in chunks],
                approval_id=approval_id,
                security_level="internal",
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


def _prepare_mcp_governing_article_document(
    root: Path,
) -> tuple[Settings, AuthContext, str]:
    settings = Settings(data_dir=root / "data", artifact_root=root)
    repository = JsonRepository(settings)
    document_id = "doc_governing"
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename="governing.pdf",
            document_name="Governing Regulation",
            file_type="pdf",
            file_hash="governing-hash",
            tenant_id="tenant-a",
            profile_id="profile-a",
            regulation_id="reg-governing",
            regulation_version="v1",
            regulation_status="approved",
            effective_from="2026-01-01",
            status="completed",
        )
    )
    chunks = [
        Chunk(
            chunk_id="article-31",
            document_id=document_id,
            chunk_type="article",
            text="제31조 휴직의 운영은 별지제15호서식에 따른다.",
            retrieval_text="제31조 휴직의 운영은 별지제15호서식에 따른다.",
            metadata={
                "article_no": "제31조",
                "article_title": "휴직의 운영",
                "form_refs": ["별지제15호서식"],
                "regulation_title": "휴직 규정",
                "profile_id": "profile-a",
                "regulation_id": "reg-governing",
                "regulation_version": "v1",
                "regulation_status": "approved",
                "effective_from": "2026-01-01",
            },
            security_level="internal",
        ),
        Chunk(
            chunk_id="form-15",
            document_id=document_id,
            chunk_type="form",
            text="[별지제15호서식] 휴직원",
            retrieval_text="[별지제15호서식] 휴직원",
            metadata={
                "article_no": "",
                "article_title": "",
                "form_refs": ["별지제15호서식"],
                "regulation_title": "휴직 규정",
                "profile_id": "profile-a",
                "regulation_id": "reg-governing",
                "regulation_version": "v1",
                "regulation_status": "approved",
                "effective_from": "2026-01-01",
            },
            security_level="internal",
        ),
    ]
    repository.save_processing_result(document_id, [], chunks, [])
    admin_auth = AuthContext(
        actor="reviewer",
        tenant_id="tenant-a",
        auth_mode="api_token",
        role="admin",
        department_ids=["hr"],
    )
    _approve_and_index_test_chunks(
        root,
        settings=settings,
        repository=repository,
        document_id=document_id,
        chunks=chunks,
        auth=admin_auth,
        approval_id="approval-governing",
    )
    fetch_auth = mcp_auth_context(
        tenant_id="tenant-a",
        role="operator",
        department_ids=["hr"],
    )
    records = routes_rag._load_local_vector_records(settings, fetch_auth)
    _write_runtime_approval_snapshot_sidecar(
        settings.data_dir,
        records,
        tenant_id="tenant-a",
    )
    result_id = search_regulations.__globals__["_encode_result_id"](
        document_id=document_id,
        chunk_id="form-15",
    )
    return settings, fetch_auth, result_id


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _save_document_with_one_chunk(
    settings: Settings,
    document_id: str,
    text: str,
    approval_id: str,
    auth: AuthContext,
    *,
    tenant_id: str = "tenant-a",
    security_level: str = "internal",
    metadata: dict | None = None,
) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            document_name=document_id,
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            tenant_id=tenant_id,
            status="completed",
        )
    )
    repository.save_processing_result(
        document_id,
        [],
        [
            Chunk(
                chunk_id=f"{document_id}-chunk-1",
                document_id=document_id,
                chunk_type="article",
                text=text,
                retrieval_text=text,
                metadata=metadata or {"article_no": "제10조", "article_title": "육아휴직"},
                security_level=security_level,
            )
        ],
        [],
    )
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    evidence = _write_approval_evidence(
        approval_settings.artifact_root,
        settings=approval_settings,
        document_id=document_id,
        chunks=[chunk for chunk in repository.get_chunks(document_id) if chunk.chunk_id == f"{document_id}-chunk-1"],
        tenant_id=auth.tenant_id,
    )
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[f"{document_id}-chunk-1"],
                approval_id=approval_id,
                security_level=security_level,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


def _write_runtime_approval_snapshot_sidecar(data_dir: Path, records: list[dict], *, tenant_id: str) -> None:
    repository_dir = data_dir / "repository"
    document_ids = sorted(
        {
            str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "")
            for record in records
            if str(record.get("document_id") or (record.get("metadata") or {}).get("document_id") or "").strip()
        }
    )
    (data_dir / "mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": tenant_id,
                "document_ids": document_ids,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    repository = JsonRepository(Settings(data_dir=data_dir))
    entries = []
    for record in records:
        metadata = record.get("metadata") or {}
        entries.append(
            {
                "document_id": record.get("document_id") or metadata.get("document_id"),
                "chunk_id": record.get("chunk_id") or metadata.get("chunk_id"),
                "approval_id": metadata.get("approval_id"),
                "approved_content_hash": metadata.get("approved_content_hash"),
                "security_level": metadata.get("security_level"),
                "department_acl": metadata.get("department_acl") or [],
                "content_hash": record.get("content_hash"),
            }
        )
    (repository_dir / "approval_snapshot.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_approval_snapshot",
                "schema_version": "mcp-runtime-approval-snapshot-v1",
                "tenant_id": tenant_id,
                "document_ids": document_ids,
                "record_count": len(entries),
                "snapshot_count": len(entries),
                "file_signatures": {
                    key: (list(value) if value is not None else None)
                    for key, value in routes_rag._runtime_approval_snapshot_file_signatures(repository).items()
                },
                "entries": entries,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    sidecar_path = repository_dir / "approval_snapshot.json"
    manifest_path = data_dir / "mcp_runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_data_reuse"] = {
        "file_sha256": {
            "repository/approval_snapshot.json": hashlib.sha256(
                sidecar_path.read_bytes()
            ).hexdigest()
        }
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _save_document_with_chunks(
    settings: Settings,
    document_id: str,
    texts: list[str],
    approval_id: str,
    auth: AuthContext,
    *,
    article_no: str,
    tenant_id: str = "tenant-a",
    security_level: str = "internal",
) -> None:
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            document_name=document_id,
            file_type="pdf",
            file_hash=f"hash-{document_id}",
            tenant_id=tenant_id,
            status="completed",
        )
    )
    chunks = [
        Chunk(
            chunk_id=f"{document_id}-chunk-{index}",
            document_id=document_id,
            chunk_type="article",
            text=text,
            retrieval_text=text,
            metadata={"article_no": article_no, "article_title": "Multi chunk article"},
            security_level=security_level,
        )
        for index, text in enumerate(texts, start=1)
    ]
    repository.save_processing_result(document_id, [], chunks, [])
    approval_settings = replace(settings, artifact_root=settings.data_dir.parent)
    saved_chunks = repository.get_chunks(document_id)
    evidence = _write_approval_evidence(
        approval_settings.artifact_root,
        settings=approval_settings,
        document_id=document_id,
        chunks=saved_chunks,
        tenant_id=auth.tenant_id,
    )
    with patch.object(routes_documents, "get_settings", return_value=approval_settings):
        routes_documents.approve_review_chunks(
            document_id,
            routes_documents.ApprovalRequest(
                chunk_ids=[chunk.chunk_id for chunk in saved_chunks],
                approval_id=approval_id,
                security_level=security_level,
                **evidence,
            ),
            auth,
        )
        routes_documents.index_document(
            document_id,
            routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
            auth,
        )


if __name__ == "__main__":
    unittest.main()
