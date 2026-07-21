from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts import mcp_client_status
from scripts.mcp_client_status import (
    CLIENT_TARGETS,
    adapt_legacy_status,
    begin_attempt,
    commit_success,
    create_bundle_status,
    fail_rolled_back,
    fail_unverified,
    initialize_status_document,
    invalidate_config_entry,
    invalidate_location,
    invalidate_runtime,
    project_legacy,
)


T0 = "2026-07-21T00:00:00Z"
T1 = "2026-07-21T00:01:00Z"
T2 = "2026-07-21T00:02:00Z"
T3 = "2026-07-21T00:03:00Z"


class McpClientStatusTests(unittest.TestCase):
    def test_create_status_contains_seven_canonical_isolated_targets(self) -> None:
        status = create_bundle_status(
            "final",
            runtime_fingerprint="runtime-v1",
            bundle_fingerprint="bundle-v1",
            generated_at=T0,
        )

        self.assertEqual(CLIENT_TARGETS, tuple(status["client_connections"]))
        self.assertEqual("mcp-bundle-status-v5", status["schema_version"])
        self.assertEqual("client-connections-v1", status["status_model"])
        for target, record in status["client_connections"].items():
            self.assertEqual(target, record["target"])
            self.assertEqual("not_started", record["last_attempt"]["state"])
            self.assertEqual("not_configured", record["effective"]["state"])
            self.assertEqual(
                {
                    "registration",
                    "loader",
                    "transport",
                    "fresh_app_server",
                    "client_reload",
                    "client_surface",
                    "conversation",
                },
                set(record["stages"]),
            )
            self.assertTrue(record["readiness"]["artifact_ready"])
            self.assertTrue(record["readiness"]["manual_action_required"])

        self.assertEqual(
            ["registration", "loader", "transport", "fresh_app_server"],
            status["client_connections"]["codex"]["configuration_required_stages"],
        )
        self.assertEqual(
            ["transport", "registration", "loader"],
            status["client_connections"]["chatgpt-remote"][
                "configuration_required_stages"
            ],
        )

    def test_begin_attempt_changes_only_selected_client_record(self) -> None:
        original = create_bundle_status("final", generated_at=T0)
        untouched = {
            target: copy.deepcopy(record)
            for target, record in original["client_connections"].items()
            if target != "codex"
        }

        started = begin_attempt(
            original,
            "codex",
            "attempt-codex-1",
            started_at=T1,
        )

        self.assertEqual("not_started", original["client_connections"]["codex"]["last_attempt"]["state"])
        self.assertEqual("in_progress", started["client_connections"]["codex"]["last_attempt"]["state"])
        self.assertEqual("codex", started["active_target"])
        for target, expected in untouched.items():
            self.assertEqual(expected, started["client_connections"][target], target)

    def test_commit_success_binds_stage_evidence_and_derives_configured(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "codex",
            "attempt-codex-1",
            started_at=T1,
        )
        committed = commit_success(
            status,
            "codex",
            "attempt-codex-1",
            verified_stages={
                "registration": {"server_name_matched": True},
                "loader": {"enabled": True},
                "transport": {"get_index_status_called": True},
                "fresh_app_server": {"tool_count": 3},
            },
            config_entry_fingerprint="config-entry-v1",
            config_container_fingerprint="config-file-v1",
            runtime_fingerprint="runtime-v1",
            bundle_fingerprint="bundle-v1",
            bundle_location_fingerprint="bundle-location-v1",
            verified_at=T2,
        )
        record = committed["client_connections"]["codex"]

        self.assertEqual("completed", record["last_attempt"]["state"])
        self.assertEqual("configured", record["effective"]["state"])
        self.assertEqual("verified", record["stages"]["transport"]["state"])
        self.assertEqual("attempt-codex-1", record["stages"]["transport"]["attempt_id"])
        self.assertTrue(committed["direct_config_registered"])
        self.assertTrue(committed["direct_config_loader_verified"])
        self.assertTrue(committed["direct_stdio_verified"])
        self.assertTrue(committed["fresh_codex_app_server_inventory_verified"])
        self.assertFalse(committed["end_to_end_verified"])

    def test_commit_success_is_isolated_for_each_of_the_seven_targets(self) -> None:
        required_stages = {
            "claude-code": ("registration", "loader", "transport"),
            "codex": ("registration", "loader", "transport", "fresh_app_server"),
            "claude-desktop": ("registration", "transport"),
            "chatgpt-desktop-local": (
                "registration",
                "loader",
                "transport",
                "fresh_app_server",
            ),
            "chatgpt-remote": ("transport", "registration", "loader"),
            "chatgpt-tunnel": ("transport", "registration", "loader"),
            "claude-api": ("transport", "registration"),
        }
        local_targets = {
            "claude-code",
            "codex",
            "claude-desktop",
            "chatgpt-desktop-local",
        }

        for target, stages in required_stages.items():
            with self.subTest(target=target):
                status = create_bundle_status("final", generated_at=T0)
                untouched = {
                    other: copy.deepcopy(record)
                    for other, record in status["client_connections"].items()
                    if other != target
                }
                attempt_id = f"attempt-{target}"
                status = begin_attempt(status, target, attempt_id, started_at=T1)
                committed = commit_success(
                    status,
                    target,
                    attempt_id,
                    verified_stages=stages,
                    config_entry_fingerprint=f"config-{target}",
                    runtime_fingerprint="runtime-v1",
                    bundle_location_fingerprint=(
                        "bundle-location-v1" if target in local_targets else None
                    ),
                    verified_at=T2,
                )

                self.assertEqual(
                    "configured",
                    committed["client_connections"][target]["effective"]["state"],
                )
                for other, expected in untouched.items():
                    self.assertEqual(
                        expected,
                        committed["client_connections"][other],
                        f"{target} success changed {other}",
                    )

    def test_new_success_attempt_cannot_reuse_stages_from_prior_attempt(self) -> None:
        configured = self._configured_codex_status()
        retrying = begin_attempt(
            configured,
            "codex",
            "attempt-codex-2",
            started_at=T2,
        )

        committed = commit_success(
            retrying,
            "codex",
            "attempt-codex-2",
            verified_stages=("registration",),
            config_entry_fingerprint="config-entry-v2",
            bundle_location_fingerprint="bundle-location-v1",
            verified_at=T3,
        )
        record = committed["client_connections"]["codex"]

        self.assertEqual("partially_verified", record["effective"]["state"])
        self.assertFalse(record["readiness"]["configuration_ready"])
        self.assertEqual("verified", record["stages"]["registration"]["state"])
        for stage_name in ("loader", "transport", "fresh_app_server"):
            self.assertEqual("not_checked", record["stages"][stage_name]["state"])
            self.assertIsNone(record["stages"][stage_name]["attempt_id"])

    def test_fail_rolled_back_preserves_effective_stages_and_other_clients(self) -> None:
        configured = self._configured_codex_status()
        prior_effective = copy.deepcopy(configured["client_connections"]["codex"]["effective"])
        prior_stages = copy.deepcopy(configured["client_connections"]["codex"]["stages"])
        prior_claude = copy.deepcopy(configured["client_connections"]["claude-desktop"])
        retrying = begin_attempt(
            configured,
            "codex",
            "attempt-codex-2",
            started_at=T2,
        )

        failed = fail_rolled_back(
            retrying,
            "codex",
            "attempt-codex-2",
            reason_code="config_write_denied",
            finished_at=T3,
        )
        record = failed["client_connections"]["codex"]

        self.assertEqual("failed_rolled_back", record["last_attempt"]["state"])
        self.assertTrue(record["last_attempt"]["rollback_complete"])
        self.assertEqual(prior_effective, record["effective"])
        self.assertEqual(prior_stages, record["stages"])
        self.assertEqual(prior_claude, failed["client_connections"]["claude-desktop"])
        self.assertEqual("failed_rolled_back", failed["installation_state"])
        self.assertEqual("configured", failed["connection_state"])

    def test_fail_unverified_terminates_attempt_and_allows_retry(self) -> None:
        configured = self._configured_codex_status()
        retrying = begin_attempt(
            configured,
            "codex",
            "attempt-codex-2",
            started_at=T2,
        )

        failed = fail_unverified(
            retrying,
            "codex",
            "attempt-codex-2",
            reason_code="status_commit_failed",
            finished_at=T3,
        )
        record = failed["client_connections"]["codex"]

        self.assertEqual("failed_unverified", record["last_attempt"]["state"])
        self.assertFalse(record["last_attempt"]["rollback_complete"])
        self.assertEqual("configured", record["effective"]["state"])
        next_attempt = begin_attempt(
            failed,
            "codex",
            "attempt-codex-3",
            started_at="2026-07-21T00:04:00Z",
        )
        self.assertEqual(
            "in_progress",
            next_attempt["client_connections"]["codex"]["last_attempt"]["state"],
        )

    def test_runtime_invalidation_stales_only_runtime_bound_local_evidence(self) -> None:
        configured = self._configured_codex_status()
        remote_before = copy.deepcopy(configured["client_connections"]["chatgpt-remote"])
        registration_before = copy.deepcopy(
            configured["client_connections"]["codex"]["stages"]["registration"]
        )

        invalidated = invalidate_runtime(
            configured,
            "runtime-v1",
            next_runtime_fingerprint="runtime-v2",
            checked_at=T3,
        )
        record = invalidated["client_connections"]["codex"]

        self.assertEqual(registration_before, record["stages"]["registration"])
        self.assertEqual("stale", record["stages"]["transport"]["state"])
        self.assertEqual("runtime_changed", record["stages"]["transport"]["reason_code"])
        self.assertEqual("stale", record["effective"]["state"])
        self.assertEqual(remote_before, invalidated["client_connections"]["chatgpt-remote"])

    def test_runtime_invalidation_isolated_across_all_local_and_remote_targets(self) -> None:
        required_stages = {
            "claude-code": ("registration", "loader", "transport"),
            "codex": ("registration", "loader", "transport", "fresh_app_server"),
            "claude-desktop": ("registration", "transport"),
            "chatgpt-desktop-local": (
                "registration",
                "loader",
                "transport",
                "fresh_app_server",
            ),
            "chatgpt-remote": ("transport", "registration", "loader"),
            "chatgpt-tunnel": ("transport", "registration", "loader"),
            "claude-api": ("transport", "registration"),
        }
        local_targets = {
            "claude-code",
            "codex",
            "claude-desktop",
            "chatgpt-desktop-local",
        }
        status = create_bundle_status("final", generated_at=T0)
        for target, stages in required_stages.items():
            attempt_id = f"attempt-{target}"
            status = begin_attempt(status, target, attempt_id, started_at=T1)
            status = commit_success(
                status,
                target,
                attempt_id,
                verified_stages=stages,
                config_entry_fingerprint=f"config-{target}",
                runtime_fingerprint="runtime-v1",
                bundle_location_fingerprint=(
                    "bundle-location-v1" if target in local_targets else None
                ),
                verified_at=T2,
            )
        remote_before = {
            target: copy.deepcopy(status["client_connections"][target])
            for target in ("chatgpt-remote", "chatgpt-tunnel", "claude-api")
        }

        invalidated = invalidate_runtime(
            status,
            "runtime-v1",
            next_runtime_fingerprint="runtime-v2",
            checked_at=T3,
        )

        for target in local_targets:
            with self.subTest(target=target):
                record = invalidated["client_connections"][target]
                self.assertEqual("verified", record["stages"]["registration"]["state"])
                self.assertEqual("stale", record["effective"]["state"])
                for stage_name in required_stages[target]:
                    if stage_name != "registration":
                        self.assertEqual("stale", record["stages"][stage_name]["state"])
        for target, expected in remote_before.items():
            self.assertEqual(expected, invalidated["client_connections"][target])

    def test_location_invalidation_does_not_touch_remote_target(self) -> None:
        configured = self._configured_codex_status()
        remote_before = copy.deepcopy(configured["client_connections"]["chatgpt-remote"])

        invalidated = invalidate_location(
            configured,
            "bundle-location-v1",
            next_location_fingerprint="bundle-location-v2",
            checked_at=T3,
        )
        record = invalidated["client_connections"]["codex"]

        self.assertEqual("stale", record["effective"]["state"])
        self.assertEqual("stale", record["stages"]["registration"]["state"])
        self.assertEqual(
            "bundle_location_changed",
            record["stages"]["registration"]["reason_code"],
        )
        self.assertEqual(remote_before, invalidated["client_connections"]["chatgpt-remote"])

    def test_config_entry_invalidation_is_target_local(self) -> None:
        configured = self._configured_codex_status()
        chatgpt_before = copy.deepcopy(
            configured["client_connections"]["chatgpt-desktop-local"]
        )

        invalidated = invalidate_config_entry(
            configured,
            "codex",
            "config-entry-v2",
            checked_at=T3,
        )

        self.assertEqual("stale", invalidated["client_connections"]["codex"]["effective"]["state"])
        self.assertEqual(
            chatgpt_before,
            invalidated["client_connections"]["chatgpt-desktop-local"],
        )

    def test_invalidation_requires_nonempty_fingerprint(self) -> None:
        status = create_bundle_status("final", generated_at=T0)

        with self.assertRaisesRegex(ValueError, "runtime fingerprint"):
            invalidate_runtime(status, "", checked_at=T1)
        with self.assertRaisesRegex(ValueError, "location fingerprint"):
            invalidate_location(status, "", checked_at=T1)
        with self.assertRaisesRegex(ValueError, "config entry fingerprint"):
            invalidate_config_entry(status, "codex", "", checked_at=T1)

    def test_legacy_projection_is_explicitly_bound_to_one_target(self) -> None:
        configured = self._configured_codex_status()
        projected = project_legacy(configured, "claude-desktop", projected_at=T3)

        self.assertEqual("claude-desktop", projected["legacy_projection_target"])
        self.assertFalse(projected["direct_config_registered"])
        self.assertFalse(projected["direct_config_loader_verified"])
        self.assertFalse(projected["claude_desktop_config_registered"])

    def test_legacy_adapter_refuses_ambiguous_direct_success(self) -> None:
        legacy = {
            "server_name": "final",
            "installation_attempt_id": "legacy-attempt",
            "runtime_fingerprint": "runtime-v1",
            "installed_config_fingerprint": "config-v1",
            "direct_config_registered": True,
            "direct_config_loader_verified": True,
            "direct_stdio_verified": True,
            "transport_end_to_end_verified": True,
            "updated_at": T1,
        }

        adapted = adapt_legacy_status(legacy, adapted_at=T2)

        self.assertEqual("target_revalidation_required", adapted["legacy_migration_state"])
        self.assertIsNone(adapted["legacy_projection_target"])
        for record in adapted["client_connections"].values():
            self.assertEqual("not_configured", record["effective"]["state"])

    def test_legacy_adapter_maps_attributed_claude_without_surface_claim(self) -> None:
        legacy = {
            "server_name": "final",
            "installation_attempt_id": "legacy-claude-attempt",
            "runtime_fingerprint": "runtime-v1",
            "claude_desktop_config_fingerprint": "config-v1",
            "claude_desktop_config_registered": True,
            "claude_desktop_config_transport_verified": True,
            "claude_desktop_loader_verified": True,
            "claude_desktop_conversation_verified": True,
            "updated_at": T1,
        }

        adapted = adapt_legacy_status(legacy, adapted_at=T2)
        record = adapted["client_connections"]["claude-desktop"]

        self.assertEqual("migrated", adapted["legacy_migration_state"])
        self.assertEqual("configured", record["effective"]["state"])
        self.assertEqual("verified", record["stages"]["registration"]["state"])
        self.assertEqual("verified", record["stages"]["transport"]["state"])
        self.assertEqual("not_checked", record["stages"]["loader"]["state"])
        self.assertEqual("not_checked", record["stages"]["conversation"]["state"])

    def test_evidence_and_identifiers_do_not_store_paths_or_secrets_verbatim(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "codex",
            r"C:\fixture\attempt secret",
            started_at=T1,
        )
        committed = commit_success(
            status,
            "codex",
            r"C:\fixture\attempt secret",
            verified_stages={
                "registration": {
                    "config_path": r"C:\fixture\.codex\config.toml",
                    "api_token": "credential-fixture-value",
                    "diagnostic": r"C:\fixture\bundle",
                }
            },
            config_entry_fingerprint=r"C:\fixture\.codex\config.toml",
            bundle_location_fingerprint=r"C:\fixture\bundle",
            verified_at=T2,
        )
        encoded = json.dumps(committed, ensure_ascii=False)

        self.assertNotIn(r"C:\fixture", encoded)
        self.assertNotIn("credential-fixture-value", encoded)
        evidence = committed["client_connections"]["codex"]["stages"]["registration"]["evidence"]
        self.assertEqual("[redacted]", evidence["config_path"])
        self.assertEqual("[redacted]", evidence["api_token"])
        self.assertTrue(str(evidence["diagnostic"]).startswith("sha256:"))

    def test_commit_requires_stage_binding_fingerprints(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "codex",
            "attempt-1",
            started_at=T1,
        )

        with self.assertRaisesRegex(ValueError, "config entry fingerprint"):
            commit_success(
                status,
                "codex",
                "attempt-1",
                verified_stages=["registration"],
                bundle_location_fingerprint="location-v1",
                verified_at=T2,
            )
        with self.assertRaisesRegex(ValueError, "bundle location fingerprint"):
            commit_success(
                status,
                "codex",
                "attempt-1",
                verified_stages=["registration"],
                config_entry_fingerprint="config-v1",
                verified_at=T2,
            )
        with self.assertRaisesRegex(ValueError, "runtime fingerprint"):
            commit_success(
                status,
                "codex",
                "attempt-1",
                verified_stages=["transport"],
                verified_at=T2,
            )

    def test_begin_attempt_rejects_second_active_attempt_for_same_target(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "codex",
            "attempt-1",
            started_at=T1,
        )

        with self.assertRaisesRegex(ValueError, "already active"):
            begin_attempt(status, "codex", "attempt-2", started_at=T2)

    def test_attempt_mismatch_fails_closed(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "codex",
            "attempt-1",
            started_at=T1,
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            commit_success(
                status,
                "codex",
                "attempt-2",
                verified_stages=[],
                verified_at=T2,
            )

    def test_conversation_stage_requires_current_tool_call_proof(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "claude-api",
            "attempt-remote-1",
            started_at=T1,
        )

        with self.assertRaisesRegex(ValueError, "tool-call proof"):
            commit_success(
                status,
                "claude-api",
                "attempt-remote-1",
                verified_stages={"conversation": {"tool_call_verified": True}},
                runtime_fingerprint="runtime-v1",
                verified_at=T2,
            )

        connected = commit_success(
            status,
            "claude-api",
            "attempt-remote-1",
            verified_stages={
                "transport": {"endpoint_verified": True},
                "registration": {"request_config_verified": True},
                "loader": {"tools_discovered": True},
                "conversation": {
                    "tool_call_verified": True,
                    "server_name": "final",
                    "tool_name": "search",
                    "conversation_id": "conversation-1",
                    "result_nonce_hash": "sha256:" + "1" * 64,
                },
            },
            config_entry_fingerprint="remote-config-v1",
            runtime_fingerprint="remote-runtime-v1",
            verified_at=T2,
        )

        self.assertEqual(
            "connected",
            connected["client_connections"]["claude-api"]["effective"]["state"],
        )
        self.assertTrue(
            connected["client_connections"]["claude-api"]["readiness"][
                "conversation_verified"
            ]
        )
        encoded = json.dumps(connected, ensure_ascii=False)
        self.assertNotIn("conversation-1", encoded)

    def test_status_reader_accepts_json_object_key_reordering(self) -> None:
        status = create_bundle_status("final", generated_at=T0)
        status["client_connections"] = dict(
            reversed(list(status["client_connections"].items()))
        )

        started = begin_attempt(status, "codex", "attempt-1", started_at=T1)

        self.assertEqual(
            "in_progress",
            started["client_connections"]["codex"]["last_attempt"]["state"],
        )

    def test_initialize_legacy_preserves_evidence_and_adds_empty_v5_records(self) -> None:
        legacy = {
            "schema_version": "mcp-bundle-status-v4",
            "status_model": "legacy-model",
            "server_name": "final",
            "installation_state": "installed_loader_verified",
            "direct_config_registered": True,
            "custom_legacy_evidence": {"count": 7},
            "generated_at": T0,
        }

        initialized = initialize_status_document(legacy, initialized_at=T1)

        self.assertEqual("mcp-bundle-status-v5", initialized["schema_version"])
        self.assertEqual("client-connections-v1", initialized["status_model"])
        self.assertEqual("mcp-bundle-status-v4", initialized["legacy_schema_version"])
        self.assertEqual("legacy-model", initialized["legacy_status_model"])
        self.assertEqual("installed_loader_verified", initialized["installation_state"])
        self.assertTrue(initialized["direct_config_registered"])
        self.assertEqual({"count": 7}, initialized["custom_legacy_evidence"])
        self.assertEqual(CLIENT_TARGETS, tuple(initialized["client_connections"]))
        for record in initialized["client_connections"].values():
            self.assertEqual("not_started", record["last_attempt"]["state"])
            self.assertEqual("not_configured", record["effective"]["state"])
            self.assertTrue(
                all(stage["state"] == "not_checked" for stage in record["stages"].values())
            )

    def test_cli_init_preserves_legacy_and_emits_only_safe_result_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "bundle_status.json"
            legacy = {
                "schema_version": "mcp-bundle-status-v4",
                "server_name": "final",
                "direct_stdio_verified": True,
                "generated_at": T0,
            }
            status_path.write_text(json.dumps(legacy), encoding="utf-8")

            exit_code, stdout, stderr = self._run_cli(
                "init",
                "--status-file",
                str(status_path),
                "--timestamp",
                T1,
            )

            self.assertEqual(0, exit_code)
            result = json.loads(stdout)
            self.assertEqual({"target", "action", "ok", "state"}, set(result))
            self.assertEqual(
                {
                    "target": None,
                    "action": "init",
                    "ok": True,
                    "state": "initialized",
                },
                result,
            )
            self.assertEqual("", stderr)
            self.assertNotIn(str(status_path), stdout)
            self.assertNotIn("final", stdout)
            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["direct_stdio_verified"])
            self.assertEqual(CLIENT_TARGETS, tuple(saved["client_connections"]))

    def test_cli_begin_and_commit_reuse_validated_state_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "bundle_status.json"
            self._write_status(status_path, create_bundle_status("final", generated_at=T0))

            begin_code, begin_stdout, begin_stderr = self._run_cli(
                "begin",
                "--status-file",
                str(status_path),
                "--target",
                "codex",
                "--attempt-id",
                "attempt-1",
                "--timestamp",
                T1,
            )
            self.assertEqual(0, begin_code)
            self.assertEqual("", begin_stderr)
            self.assertEqual("in_progress", json.loads(begin_stdout)["state"])

            commit_code, commit_stdout, commit_stderr = self._run_cli(
                "commit",
                "--status-file",
                str(status_path),
                "--target",
                "codex",
                "--attempt-id",
                "attempt-1",
                "--verified-stage",
                "registration",
                "--verified-stage",
                "loader",
                "--verified-stage",
                "transport",
                "--verified-stage",
                "fresh_app_server",
                "--config-entry-fingerprint",
                "config-v1",
                "--config-container-fingerprint",
                "container-v1",
                "--runtime-fingerprint",
                "runtime-v1",
                "--bundle-fingerprint",
                "bundle-v1",
                "--bundle-location-fingerprint",
                "location-v1",
                "--timestamp",
                T2,
            )

            self.assertEqual(0, commit_code)
            self.assertEqual("", commit_stderr)
            self.assertEqual(
                {
                    "target": "codex",
                    "action": "commit",
                    "ok": True,
                    "state": "configured",
                },
                json.loads(commit_stdout),
            )
            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "configured",
                saved["client_connections"]["codex"]["effective"]["state"],
            )

    def test_cli_attempt_mismatch_is_nonzero_sanitized_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "private-name-status.json"
            started = begin_attempt(
                create_bundle_status("secret-server", generated_at=T0),
                "codex",
                "real-attempt",
                started_at=T1,
            )
            self._write_status(status_path, started)
            before = status_path.read_bytes()

            exit_code, stdout, stderr = self._run_cli(
                "commit",
                "--status-file",
                str(status_path),
                "--target",
                "codex",
                "--attempt-id",
                "wrong-secret-attempt",
                "--timestamp",
                T2,
            )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                {"target": None, "action": "commit", "ok": False, "state": "failed"},
                json.loads(stdout),
            )
            self.assertEqual("MCP client status command failed.\n", stderr)
            self.assertNotIn(str(status_path), stdout + stderr)
            self.assertNotIn("secret-server", stdout + stderr)
            self.assertNotIn("wrong-secret-attempt", stdout + stderr)
            self.assertEqual(before, status_path.read_bytes())

    def test_cli_fail_rolled_back_keeps_previous_effective_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "bundle_status.json"
            configured = self._configured_codex_status()
            prior_effective = copy.deepcopy(
                configured["client_connections"]["codex"]["effective"]
            )
            retrying = begin_attempt(
                configured,
                "codex",
                "attempt-codex-2",
                started_at=T2,
            )
            self._write_status(status_path, retrying)

            exit_code, stdout, stderr = self._run_cli(
                "fail-rolled-back",
                "--status-file",
                str(status_path),
                "--target",
                "codex",
                "--attempt-id",
                "attempt-codex-2",
                "--reason",
                "config_write_denied",
                "--timestamp",
                T3,
            )

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual("failed_rolled_back", json.loads(stdout)["state"])
            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(
                prior_effective,
                saved["client_connections"]["codex"]["effective"],
            )

    def test_cli_invalidate_runtime_marks_bound_client_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "bundle_status.json"
            self._write_status(status_path, self._configured_codex_status())

            exit_code, stdout, stderr = self._run_cli(
                "invalidate-runtime",
                "--status-file",
                str(status_path),
                "--previous-runtime-fingerprint",
                "runtime-v1",
                "--next-runtime-fingerprint",
                "runtime-v2",
                "--timestamp",
                T3,
            )

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual(
                {
                    "target": None,
                    "action": "invalidate-runtime",
                    "ok": True,
                    "state": "stale",
                },
                json.loads(stdout),
            )
            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "stale",
                saved["client_connections"]["codex"]["effective"]["state"],
            )

    def test_atomic_writer_rejects_concurrent_digest_change_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "bundle_status.json"
            original = create_bundle_status("final", generated_at=T0)
            self._write_status(status_path, original)
            expected_digest = hashlib.sha256(status_path.read_bytes()).hexdigest()
            external_bytes = b'{"external":"winner"}\n'
            status_path.write_bytes(external_bytes)

            with self.assertRaisesRegex(RuntimeError, "concurrent status modification"):
                mcp_client_status._atomic_write_status_file(
                    status_path,
                    create_bundle_status("replacement", generated_at=T1),
                    expected_digest=expected_digest,
                )

            self.assertEqual(external_bytes, status_path.read_bytes())
            self.assertEqual([], list(Path(temp_dir).glob("*.tmp")))

    @staticmethod
    def _write_status(path: Path, status: dict) -> None:
        path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _run_cli(*args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = mcp_client_status.main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _configured_codex_status(self) -> dict:
        status = begin_attempt(
            create_bundle_status("final", generated_at=T0),
            "codex",
            "attempt-codex-1",
            started_at=T1,
        )
        return commit_success(
            status,
            "codex",
            "attempt-codex-1",
            verified_stages=(
                "registration",
                "loader",
                "transport",
                "fresh_app_server",
            ),
            config_entry_fingerprint="config-entry-v1",
            config_container_fingerprint="config-file-v1",
            runtime_fingerprint="runtime-v1",
            bundle_fingerprint="bundle-v1",
            bundle_location_fingerprint="bundle-location-v1",
            verified_at=T2,
        )


if __name__ == "__main__":
    unittest.main()
