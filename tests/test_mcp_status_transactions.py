from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import scripts.generate_mcp_client_config as generator
from scripts.generate_mcp_client_config import (
    _write_bundle_status,
    build_mcp_client_config,
    write_mcp_runtime_data_bundle,
    write_mcp_setup_bundle,
    write_mcp_setup_bundle_zip,
)


def _runtime_fingerprint(runtime_manifest: dict[str, Any]) -> str:
    serialized = json.dumps(
        runtime_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _mark_bundle_status_connected(status_path: Path) -> None:
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.update(
        {
            "installation_attempt_id": "completed-attempt",
            "installation_state": "installed_loader_verified",
            "connection_state": "connected",
            "runtime_data_ready": True,
            "runtime_fingerprint": "a" * 64,
            "installed_config_fingerprint": "sha256:" + ("b" * 64),
            "direct_config_registered": True,
            "direct_config_loader_verified": True,
            "loader_verification_state": "verified",
            "process_started": True,
            "mcp_initialized": True,
            "tools_discovered": True,
            "installed_config_transport_verified": True,
            "direct_stdio_verified": True,
            "transport_end_to_end_verified": True,
            "claude_desktop_config_transport_verified": True,
            "claude_desktop_loader_verified": True,
            "claude_desktop_conversation_verified": True,
            "fresh_codex_app_server_inventory_verified": True,
            "desktop_app_server_loader_verified": True,
            "desktop_tool_scan_verified": True,
            "conversation_attachment_verified": True,
            "conversation_attachment_unverified": False,
            "tool_scan_unverified": False,
            "end_to_end_verified": True,
        }
    )
    status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class McpStatusTransactionTests(unittest.TestCase):
    def test_failed_zip_refresh_preserves_existing_archive_bytes(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            zip_path = root / "bundle.zip"
            write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="status-transaction-mcp",
            )
            write_mcp_setup_bundle_zip(bundle_dir, zip_path)
            before = zip_path.read_bytes()

            def fail_during_zip(current: int, _total: int, _name: str) -> None:
                if current > 0:
                    raise RuntimeError("forced-mid-zip-failure")

            with self.assertRaisesRegex(RuntimeError, "forced-mid-zip-failure"):
                write_mcp_setup_bundle_zip(
                    bundle_dir,
                    zip_path,
                    progress_callback=fail_during_zip,
                )

            after = zip_path.read_bytes()

        self.assertEqual(
            before,
            after,
            "A failed ZIP refresh must preserve the existing valid archive byte-for-byte.",
        )

    def test_failed_setup_refresh_preserves_existing_bundle_bytes(self) -> None:
        original_config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        replacement_config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-b",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            write_mcp_setup_bundle(
                original_config,
                bundle_dir,
                server_name="status-transaction-mcp",
            )
            (bundle_dir / "runtime_python.json").write_bytes(b'{"schema_version":1,"legacy":true}\r\n')
            _mark_bundle_status_connected(bundle_dir / "bundle_status.json")
            before = _tree_bytes(bundle_dir)
            original_write = generator._write_utf8_no_bom
            write_count = 0

            def fail_during_setup(path: Path, value: str) -> None:
                nonlocal write_count
                write_count += 1
                if write_count == 4:
                    raise RuntimeError("forced-mid-setup-failure")
                original_write(path, value)

            with (
                patch(
                    "scripts.generate_mcp_client_config._write_utf8_no_bom",
                    side_effect=fail_during_setup,
                ),
                self.assertRaisesRegex(RuntimeError, "forced-mid-setup-failure"),
            ):
                write_mcp_setup_bundle(
                    replacement_config,
                    bundle_dir,
                    server_name="status-transaction-mcp",
                )

            after = _tree_bytes(bundle_dir)

        changed_paths = sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        self.assertEqual(
            [],
            changed_paths,
            "A failed setup refresh must preserve every existing bundle file byte-for-byte.",
        )
        for path, expected in before.items():
            with self.subTest(path=path):
                self.assertEqual(expected, after[path])

    def test_successful_setup_refresh_discards_stale_runtime_marker(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="status-transaction-mcp")
            marker = bundle_dir / "runtime_python.json"
            marker.write_bytes(b'{"schema_version":1,"legacy":true}\r\n')

            write_mcp_setup_bundle(config, bundle_dir, server_name="status-transaction-mcp")

            self.assertFalse(marker.exists())

    def test_setup_refresh_invalidates_claude_evidence_before_bundle_replacement(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="status-transaction-mcp")
            status_path = bundle_dir / "bundle_status.json"
            _mark_bundle_status_connected(status_path)
            observed_status: dict[str, Any] = {}

            def inspect_transition_then_fail(*args: Any, **kwargs: Any) -> dict[str, str]:
                del args, kwargs
                observed_status.update(json.loads(status_path.read_text(encoding="utf-8")))
                raise RuntimeError("stop-after-setup-transition")

            with (
                patch(
                    "scripts.generate_mcp_client_config._write_mcp_setup_bundle_untransactional",
                    side_effect=inspect_transition_then_fail,
                ),
                self.assertRaisesRegex(RuntimeError, "stop-after-setup-transition"),
            ):
                write_mcp_setup_bundle(config, bundle_dir, server_name="status-transaction-mcp")

        self.assertEqual("setup_refresh_in_progress", observed_status["installation_state"])
        for field in (
            "claude_desktop_config_transport_verified",
            "claude_desktop_loader_verified",
            "claude_desktop_conversation_verified",
        ):
            with self.subTest(field=field):
                self.assertFalse(observed_status[field])

    def test_failed_setup_backup_preserves_unbacked_original_bytes(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="status-transaction-mcp",
            )
            _mark_bundle_status_connected(bundle_dir / "bundle_status.json")
            before = _tree_bytes(bundle_dir)
            original_copy = generator.shutil.copy2
            copy_count = 0

            def fail_during_backup(source: Path, destination: Path, *args: Any, **kwargs: Any) -> Path:
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    raise OSError("forced-mid-backup-failure")
                return original_copy(source, destination, *args, **kwargs)

            with (
                patch(
                    "scripts.generate_mcp_client_config.shutil.copy2",
                    side_effect=fail_during_backup,
                ),
                self.assertRaisesRegex(OSError, "forced-mid-backup-failure"),
            ):
                write_mcp_setup_bundle(
                    config,
                    bundle_dir,
                    server_name="status-transaction-mcp",
                )

            after = _tree_bytes(bundle_dir)

        changed_paths = sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        self.assertEqual(
            [],
            changed_paths,
            "A failed backup must not delete originals that were never copied safely.",
        )
        for path, expected in before.items():
            with self.subTest(path=path):
                self.assertEqual(expected, after[path])

    def test_failed_runtime_refresh_preserves_existing_data_and_status_bytes(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        records = [
            {
                "id": "document-a:chunk-a",
                "document_id": "document-a",
                "chunk_id": "chunk-a",
                "text": "approved regulation text",
                "metadata": {
                    "document_id": "document-a",
                    "chunk_id": "chunk-a",
                    "tenant_id": "tenant-a",
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            source_data_dir = root / "source-data"
            source_data_dir.mkdir()
            write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="status-transaction-mcp",
            )
            runtime_data_dir = bundle_dir / "data"
            (runtime_data_dir / "repository").mkdir(parents=True)
            (runtime_data_dir / "repository" / "legacy.json").write_bytes(b"legacy-repository\r\n")
            (runtime_data_dir / "vector_db").mkdir()
            (runtime_data_dir / "vector_db" / "legacy.bin").write_bytes(b"legacy-vector\x00\x01")
            (runtime_data_dir / "mcp_runtime_manifest.json").write_bytes(b'{"record_count":1}\r\n')
            _mark_bundle_status_connected(bundle_dir / "bundle_status.json")
            before = _tree_bytes(bundle_dir)

            with (
                patch(
                    "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                    return_value=records,
                ),
                patch(
                    "scripts.generate_mcp_client_config.canonicalize_runtime_records",
                    return_value=records,
                ),
                patch(
                    "scripts.generate_mcp_client_config._runtime_source_metadata_summary",
                    return_value={},
                ),
                patch("scripts.generate_mcp_client_config._require_runtime_source_metadata"),
                patch(
                    "scripts.generate_mcp_client_config._require_kordoc_table_parser_evidence",
                    return_value={},
                ),
                patch(
                    "scripts.generate_mcp_client_config.write_vector_records_with_offsets",
                    side_effect=RuntimeError("forced-mid-runtime-failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "forced-mid-runtime-failure"),
            ):
                write_mcp_runtime_data_bundle(
                    source_data_dir=source_data_dir,
                    out_dir=bundle_dir,
                    tenant_id="tenant-a",
                    document_id="document-a",
                )

            after = _tree_bytes(bundle_dir)

        changed_paths = sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        self.assertEqual(
            [],
            changed_paths,
            "A failed runtime refresh must preserve data and bundle status byte-for-byte.",
        )
        for path, expected in before.items():
            with self.subTest(path=path):
                self.assertEqual(expected, after[path])

    def test_runtime_refresh_invalidates_runtime_dependent_connection_evidence(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        original_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-a",
            "document_ids": ["document-a"],
            "record_count": 1,
            "chunk_count": 2,
            "recommended_smoke_query": "original query",
        }
        refreshed_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-a",
            "document_ids": ["document-a", "document-b"],
            "record_count": 2,
            "chunk_count": 5,
            "recommended_smoke_query": "refreshed query",
        }

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="status-transaction-mcp",
            )
            status_path = bundle_dir / "bundle_status.json"
            _write_bundle_status(
                bundle_dir,
                config=config,
                runtime_manifest=original_runtime,
            )

            completed_status = json.loads(status_path.read_text(encoding="utf-8"))
            completed_status.update(
                {
                    "installation_attempt_id": "completed-attempt",
                    "installation_state": "installed_loader_verified",
                    "connection_state": "connected",
                    "direct_config_registered": True,
                    "direct_config_loader_verified": True,
                    "loader_verification_state": "verified",
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "installed_config_transport_verified": True,
                    "generated_client_configs_transport_verified": True,
                    "plugin_stdio_verified": True,
                    "direct_stdio_verified": True,
                    "transport_end_to_end_verified": True,
                    "fresh_codex_app_server_inventory_verified": True,
                    "desktop_app_server_loader_verified": True,
                    "desktop_app_server_tool_count": 3,
                    "desktop_app_server_tool_names": ["fetch", "get_index_status", "search"],
                    "desktop_app_server_server_info": {"name": "status-transaction-mcp"},
                    "desktop_app_server_error": None,
                    "desktop_tool_scan_verified": True,
                    "conversation_attachment_verified": True,
                    "conversation_attachment_unverified": False,
                    "tool_scan_unverified": False,
                    "end_to_end_verified": True,
                }
            )
            status_path.write_text(
                json.dumps(completed_status, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            _write_bundle_status(
                bundle_dir,
                config=config,
                runtime_manifest=refreshed_runtime,
            )

            refreshed_status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual("completed-attempt", refreshed_status["installation_attempt_id"])
        self.assertTrue(refreshed_status["direct_config_registered"])
        self.assertTrue(refreshed_status["direct_config_loader_verified"])
        self.assertEqual("verified", refreshed_status["loader_verification_state"])
        self.assertEqual(
            "installed_loader_verified_runtime_changed",
            refreshed_status["installation_state"],
        )
        self.assertEqual("pending_runtime_revalidation", refreshed_status["connection_state"])
        self.assertEqual(2, refreshed_status["record_count"])
        self.assertEqual(5, refreshed_status["chunk_count"])
        self.assertEqual("refreshed query", refreshed_status["recommended_smoke_query"])
        self.assertEqual(
            _runtime_fingerprint(refreshed_runtime),
            refreshed_status["runtime_fingerprint"],
        )
        self.assertNotEqual(
            _runtime_fingerprint(original_runtime),
            refreshed_status["runtime_fingerprint"],
        )
        for field in (
            "process_started",
            "mcp_initialized",
            "tools_discovered",
            "installed_config_transport_verified",
            "generated_client_configs_transport_verified",
            "plugin_stdio_verified",
            "direct_stdio_verified",
            "transport_end_to_end_verified",
            "fresh_codex_app_server_inventory_verified",
            "desktop_app_server_loader_verified",
            "desktop_tool_scan_verified",
            "conversation_attachment_verified",
            "end_to_end_verified",
        ):
            with self.subTest(field=field):
                self.assertFalse(refreshed_status[field])
        self.assertTrue(refreshed_status["conversation_attachment_unverified"])
        self.assertTrue(refreshed_status["tool_scan_unverified"])
        self.assertEqual(0, refreshed_status["desktop_app_server_tool_count"])
        self.assertEqual([], refreshed_status["desktop_app_server_tool_names"])
        self.assertIsNone(refreshed_status["desktop_app_server_server_info"])
        self.assertEqual(
            "runtime_changed_revalidation_required",
            refreshed_status["desktop_app_server_error"],
        )

    def test_runtime_refresh_invalidates_claude_evidence_before_data_swap(self) -> None:
        config = build_mcp_client_config(
            server_name="status-transaction-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        refreshed_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-b",
            "record_count": 2,
            "chunk_count": 4,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            source_data_dir = root / "source-data"
            source_data_dir.mkdir()
            write_mcp_setup_bundle(config, bundle_dir, server_name="status-transaction-mcp")
            status_path = bundle_dir / "bundle_status.json"
            _mark_bundle_status_connected(status_path)
            observed_status: dict[str, Any] = {}

            def stage_runtime(*args: Any, **kwargs: Any) -> dict[str, Any]:
                del args
                staging_dir = Path(kwargs["_runtime_data_dir"])
                staging_dir.mkdir(parents=True, exist_ok=True)
                (staging_dir / "mcp_runtime_manifest.json").write_text(
                    json.dumps(refreshed_runtime, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                return refreshed_runtime

            def inspect_transition(_output_dir: Path) -> list[str]:
                observed_status.update(json.loads(status_path.read_text(encoding="utf-8")))
                return []

            with (
                patch(
                    "scripts.generate_mcp_client_config._write_mcp_runtime_data_bundle_uncommitted",
                    side_effect=stage_runtime,
                ),
                patch(
                    "scripts.generate_mcp_client_config._clear_stale_bundle_status_reports",
                    side_effect=inspect_transition,
                ),
            ):
                write_mcp_runtime_data_bundle(
                    source_data_dir=source_data_dir,
                    out_dir=bundle_dir,
                    tenant_id="tenant-a",
                    document_id="document-b",
                )

        self.assertEqual("runtime_refresh_in_progress", observed_status["installation_state"])
        for field in (
            "claude_desktop_config_transport_verified",
            "claude_desktop_loader_verified",
            "claude_desktop_conversation_verified",
        ):
            with self.subTest(field=field):
                self.assertFalse(observed_status[field])

    def test_runtime_refresh_invalidates_claude_desktop_verification_evidence(self) -> None:
        config = build_mcp_client_config(
            server_name="claude-status-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        original_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-a",
            "record_count": 1,
            "chunk_count": 2,
        }
        refreshed_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-b",
            "record_count": 2,
            "chunk_count": 4,
        }

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="claude-status-mcp",
            )
            status_path = bundle_dir / "bundle_status.json"
            _write_bundle_status(
                bundle_dir,
                config=config,
                runtime_manifest=original_runtime,
            )

            completed_status = json.loads(status_path.read_text(encoding="utf-8"))
            completed_status.update(
                {
                    "installation_attempt_id": "claude-completed-attempt",
                    "installation_state": "installed_pending_claude_desktop_verification",
                    "connection_state": "pending_claude_desktop_restart",
                    "claude_desktop_config_registered": True,
                    "claude_desktop_config_path": "C:/fixture/AppData/Roaming/Claude/claude_desktop_config.json",
                    "claude_desktop_config_fingerprint": "sha256:" + ("c" * 64),
                    "claude_desktop_config_transport_verified": True,
                    "claude_desktop_config_transport_runtime_fingerprint": completed_status[
                        "runtime_fingerprint"
                    ],
                    "claude_desktop_loader_observed": True,
                    "claude_desktop_loader_verified": True,
                    "claude_desktop_conversation_verified": True,
                    "direct_stdio_verified": True,
                    "transport_end_to_end_verified": True,
                    "end_to_end_verified": True,
                }
            )
            status_path.write_text(
                json.dumps(completed_status, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            _write_bundle_status(
                bundle_dir,
                config=config,
                runtime_manifest=refreshed_runtime,
            )

            refreshed_status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(
            "claude-completed-attempt",
            refreshed_status["installation_attempt_id"],
        )
        self.assertTrue(refreshed_status["claude_desktop_config_registered"])
        self.assertEqual(
            "C:/fixture/AppData/Roaming/Claude/claude_desktop_config.json",
            refreshed_status["claude_desktop_config_path"],
        )
        self.assertEqual(
            "sha256:" + ("c" * 64),
            refreshed_status["claude_desktop_config_fingerprint"],
        )
        self.assertEqual(
            "installed_pending_claude_desktop_verification_runtime_changed",
            refreshed_status["installation_state"],
        )
        self.assertEqual(
            "pending_runtime_revalidation",
            refreshed_status["connection_state"],
        )
        for field in (
            "claude_desktop_config_transport_verified",
            "claude_desktop_loader_observed",
            "claude_desktop_loader_verified",
            "claude_desktop_conversation_verified",
            "direct_stdio_verified",
            "transport_end_to_end_verified",
            "end_to_end_verified",
        ):
            with self.subTest(field=field):
                self.assertFalse(refreshed_status[field])
        self.assertIsNone(
            refreshed_status["claude_desktop_config_transport_runtime_fingerprint"]
        )
        for target, record in refreshed_status["client_connections"].items():
            with self.subTest(target=target):
                self.assertTrue(record["readiness"]["runtime_ready"])

    def test_runtime_refresh_rejects_every_active_installation_state(self) -> None:
        config = build_mcp_client_config(
            server_name="active-status-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        original_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-a",
            "record_count": 1,
            "chunk_count": 1,
        }
        refreshed_runtime = {
            "tenant_id": "tenant-a",
            "document_id": "document-b",
            "record_count": 2,
            "chunk_count": 3,
        }
        active_states = (
            "preflight_direct",
            "preflight_plugin",
            "preflight_claude_desktop",
            "installing",
            "installing_plugin",
            "plugin_installed_pending_loader_verification",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for active_state in active_states:
                with self.subTest(active_state=active_state):
                    bundle_dir = root / active_state
                    write_mcp_setup_bundle(
                        config,
                        bundle_dir,
                        server_name="active-status-mcp",
                    )
                    status_path = bundle_dir / "bundle_status.json"
                    _write_bundle_status(
                        bundle_dir,
                        config=config,
                        runtime_manifest=original_runtime,
                    )
                    active_status = json.loads(status_path.read_text(encoding="utf-8"))
                    active_status.update(
                        {
                            "installation_attempt_id": f"attempt-{active_state}",
                            "installation_state": active_state,
                            "connection_state": "not_connected",
                        }
                    )
                    status_path.write_text(
                        json.dumps(active_status, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    status_before_refresh = status_path.read_bytes()

                    with self.assertRaisesRegex(
                        RuntimeError,
                        "cannot replace bundle status during an active connection attempt",
                    ):
                        _write_bundle_status(
                            bundle_dir,
                            config=config,
                            runtime_manifest=refreshed_runtime,
                        )

                    self.assertEqual(status_before_refresh, status_path.read_bytes())
                    unchanged_status = json.loads(status_path.read_text(encoding="utf-8"))
                    self.assertEqual(active_state, unchanged_status["installation_state"])
                    self.assertEqual(
                        f"attempt-{active_state}",
                        unchanged_status["installation_attempt_id"],
                    )
                    self.assertEqual(1, unchanged_status["record_count"])
                    self.assertEqual(
                        _runtime_fingerprint(original_runtime),
                        unchanged_status["runtime_fingerprint"],
                    )


if __name__ == "__main__":
    unittest.main()
