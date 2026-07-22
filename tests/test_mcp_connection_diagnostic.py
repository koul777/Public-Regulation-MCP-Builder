from __future__ import annotations

import unittest

from scripts.mcp_connection_diagnostic import (
    STAGE_ORDER,
    build_connection_diagnostic,
    diagnostic_from_bundle_status,
)
from scripts.mcp_client_status import begin_attempt, commit_success, create_bundle_status


ATTEMPT = "attempt-20260720"
FINGERPRINT = "sha256:current-config"
CHECKED_AT = "2026-07-20T14:00:00Z"


def stage(
    state: str = "verified",
    *,
    attempt_id: str = ATTEMPT,
    fingerprint: str = FINGERPRINT,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    stage_evidence = {"config_fingerprint": fingerprint}
    stage_evidence.update(evidence or {})
    return {
        "state": state,
        "attempt_id": attempt_id,
        "checked_at": CHECKED_AT,
        "reason_code": "ok" if state == "verified" else state,
        "evidence": stage_evidence,
    }


def fully_verified_stages() -> dict[str, dict[str, object]]:
    stages = {name: stage() for name in STAGE_ORDER}
    stages["conversation"] = stage(
        evidence={
            "tool_call_verified": True,
            "server_name": "regulation_mcp",
            "tool_name": "get_index_status",
            "result_nonce": "result-nonce-20260720",
            "conversation_id": "conversation-20260720",
        }
    )
    return stages


class McpConnectionDiagnosticTests(unittest.TestCase):
    def test_current_attempt_with_conversation_proof_is_connected(self) -> None:
        report = build_connection_diagnostic(
            fully_verified_stages(),
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        self.assertEqual("connected", report["overall_state"])
        self.assertTrue(report["configured"])
        self.assertTrue(report["connected"])
        self.assertFalse(report["pending"])
        self.assertIsNone(report["first_blocking_stage"])

    def test_verified_conversation_without_explicit_proof_cannot_connect(self) -> None:
        stages = fully_verified_stages()
        stages["conversation"] = stage()

        report = build_connection_diagnostic(
            stages,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        self.assertEqual("configured", report["overall_state"])
        self.assertFalse(report["connected"])
        self.assertEqual("pending", report["stages"]["conversation"]["state"])
        self.assertEqual(
            "conversation_proof_missing",
            report["stages"]["conversation"]["reason_code"],
        )

    def test_conversation_requires_complete_tool_call_contract(self) -> None:
        complete_evidence = {
            "tool_call_verified": True,
            "server_name": "regulation_mcp",
            "tool_name": "get_index_status",
            "result_nonce": "result-nonce-20260720",
            "conversation_id": "conversation-20260720",
        }
        invalid_evidence_cases = {
            "proof_id_only": {"proof_id": "made-up-proof"},
            "tool_call_not_verified": {**complete_evidence, "tool_call_verified": False},
            **{
                f"missing_{field}": {
                    key: value for key, value in complete_evidence.items() if key != field
                }
                for field in (
                    "server_name",
                    "tool_name",
                    "result_nonce",
                    "conversation_id",
                )
            },
        }

        for case_name, evidence in invalid_evidence_cases.items():
            with self.subTest(case_name=case_name):
                stages = fully_verified_stages()
                stages["conversation"] = stage(evidence=evidence)
                report = build_connection_diagnostic(
                    stages,
                    attempt_id=ATTEMPT,
                    config_fingerprint=FINGERPRINT,
                    checked_at=CHECKED_AT,
                )

                self.assertEqual("configured", report["overall_state"])
                self.assertFalse(report["connected"])
                self.assertEqual("pending", report["stages"]["conversation"]["state"])

    def test_runtime_dependent_evidence_must_match_current_runtime_fingerprint(self) -> None:
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "installed_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": "runtime-current",
                "installed_config_transport_runtime_fingerprint": "runtime-stale",
                "fresh_codex_app_server_runtime_fingerprint": "runtime-stale",
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "direct_stdio_verified": True,
                "transport_end_to_end_verified": True,
                "desktop_app_server_loader_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
            },
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
        )

        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        for stage_name in ("transport", "fresh_app_server"):
            with self.subTest(stage_name=stage_name):
                self.assertNotEqual("verified", report["stages"][stage_name]["state"])

    def test_direct_registration_cannot_be_combined_with_plugin_loader_evidence(self) -> None:
        runtime_fingerprint = "runtime-current"
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "installed_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": runtime_fingerprint,
                "installed_config_transport_runtime_fingerprint": runtime_fingerprint,
                "fresh_codex_app_server_runtime_fingerprint": runtime_fingerprint,
                "direct_config_registered": True,
                "plugin_registered": False,
                "direct_config_loader_verified": False,
                "plugin_loader_verified": True,
                "direct_stdio_verified": True,
                "transport_end_to_end_verified": True,
                "desktop_app_server_loader_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
            },
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
        )

        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertTrue(
            any(
                report["stages"][stage_name]["state"] != "verified"
                for stage_name in ("registration", "loader", "transport")
            )
        )

    def test_stale_attempt_invalidates_claimed_success(self) -> None:
        stages = fully_verified_stages()
        stages["registration"] = stage(attempt_id="older-attempt")

        report = build_connection_diagnostic(
            stages,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        registration = report["stages"]["registration"]
        self.assertEqual("stale", registration["state"])
        self.assertEqual("stale_attempt", registration["reason_code"])
        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertFalse(report["connected"])
        self.assertEqual(["registration"], report["stale_stages"])

    def test_stale_config_fingerprint_invalidates_claimed_success(self) -> None:
        stages = fully_verified_stages()
        stages["desktop_surface"] = stage(fingerprint="sha256:old-config")

        report = build_connection_diagnostic(
            stages,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        surface = report["stages"]["desktop_surface"]
        self.assertEqual("stale", surface["state"])
        self.assertEqual("stale_config_fingerprint", surface["reason_code"])
        self.assertEqual("configured", report["overall_state"])
        self.assertFalse(report["connected"])

    def test_verified_evidence_without_provenance_is_stale(self) -> None:
        stages = fully_verified_stages()
        stages["loader"] = {
            "state": "verified",
            "checked_at": CHECKED_AT,
            "evidence": {"tool_count": 14},
        }

        report = build_connection_diagnostic(
            stages,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        loader = report["stages"]["loader"]
        self.assertEqual("stale", loader["state"])
        self.assertEqual("evidence_attempt_missing", loader["reason_code"])

    def test_registration_only_remains_pending(self) -> None:
        report = build_connection_diagnostic(
            {"registration": stage()},
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertTrue(report["pending"])
        self.assertFalse(report["connected"])
        self.assertEqual("loader", report["first_blocking_stage"])

    def test_current_core_stages_are_configured_but_not_connected(self) -> None:
        stages = {name: stage() for name in STAGE_ORDER[:4]}

        report = build_connection_diagnostic(
            stages,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        self.assertEqual("configured", report["overall_state"])
        self.assertTrue(report["configured"])
        self.assertTrue(report["pending"])
        self.assertFalse(report["connected"])
        self.assertEqual("desktop_reload", report["first_blocking_stage"])

    def test_support_text_and_evidence_redact_paths_and_secrets(self) -> None:
        stages = fully_verified_stages()
        stages["transport"] = stage(
            state="failed",
            evidence={
                "config_fingerprint": FINGERPRINT,
                "launcher_path": r"C:\fixture-private\bundle\run.ps1",
                "error": r"failed at C:\fixture-private\bundle\run.ps1",
                "bearer_token": "top-secret",
            },
        )

        report = build_connection_diagnostic(
            stages,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )
        rendered = repr(
            {
                "support_summary": report["support_summary"],
                "next_action": report["next_action"],
                "evidence": report["stages"]["transport"]["evidence"],
            }
        )

        self.assertNotIn("C:\\Users", rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertIn("[redacted]", rendered)
        self.assertIn("[redacted-path]", rendered)

    def test_sequence_stage_input_is_supported(self) -> None:
        items = [dict(stage(), stage=name) for name in STAGE_ORDER]
        items[-1]["evidence"] = {
            "config_fingerprint": FINGERPRINT,
            "tool_call_verified": True,
            "server_name": "regulation_mcp",
            "tool_name": "get_index_status",
            "result_nonce": "result-nonce-20260720",
            "conversation_id": "conversation-20260720",
        }

        report = build_connection_diagnostic(
            items,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at=CHECKED_AT,
        )

        self.assertTrue(report["connected"])

    def test_checked_at_is_normalized_to_utc(self) -> None:
        report = build_connection_diagnostic(
            {"registration": stage()},
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
            checked_at="2026-07-20T23:00:00+09:00",
        )

        self.assertEqual("2026-07-20T14:00:00Z", report["checked_at"])

    def test_legacy_all_true_never_becomes_connected(self) -> None:
        legacy = {
            "attempt_id": ATTEMPT,
            "config_fingerprint": FINGERPRINT,
            "runtime_fingerprint": "sha256:runtime-current",
            "direct_config_registered": True,
            "direct_config_loader_verified": True,
            "installed_config_transport_verified": True,
            "installed_config_transport_runtime_fingerprint": "sha256:runtime-current",
            "direct_stdio_verified": True,
            "transport_end_to_end_verified": True,
            "fresh_codex_app_server_inventory_verified": True,
            "fresh_codex_app_server_runtime_fingerprint": "sha256:runtime-current",
            "desktop_app_server_loader_verified": True,
            "desktop_restart_required": False,
            "desktop_restart_status": "up_to_date",
            "desktop_tool_scan_verified": True,
            "conversation_attachment_verified": True,
            "end_to_end_verified": True,
            "updated_at": CHECKED_AT,
        }

        report = diagnostic_from_bundle_status(
            legacy,
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
        )

        self.assertEqual("configured", report["overall_state"])
        self.assertFalse(report["connected"])
        self.assertEqual("pending", report["stages"]["desktop_surface"]["state"])
        self.assertEqual("pending", report["stages"]["conversation"]["state"])
        self.assertEqual(
            "legacy_conversation_proof_not_current",
            report["stages"]["conversation"]["reason_code"],
        )

    def test_unattributed_legacy_true_values_remain_pending(self) -> None:
        report = diagnostic_from_bundle_status(
            {
                "runtime_fingerprint": "sha256:runtime-current",
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "installed_config_transport_verified": True,
                "installed_config_transport_runtime_fingerprint": "sha256:runtime-current",
                "direct_stdio_verified": True,
                "transport_end_to_end_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
                "fresh_codex_app_server_runtime_fingerprint": "sha256:runtime-current",
                "desktop_app_server_loader_verified": True,
                "updated_at": CHECKED_AT,
            }
        )

        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertEqual(
            "legacy_evidence_unattributed",
            report["stages"]["registration"]["reason_code"],
        )

    def test_explicit_binding_converts_legacy_core_without_claiming_connection(self) -> None:
        report = diagnostic_from_bundle_status(
            {
                "runtime_fingerprint": "sha256:runtime-current",
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "installed_config_transport_verified": True,
                "installed_config_transport_runtime_fingerprint": "sha256:runtime-current",
                "direct_stdio_verified": True,
                "transport_end_to_end_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
                "fresh_codex_app_server_runtime_fingerprint": "sha256:runtime-current",
                "desktop_app_server_loader_verified": True,
                "desktop_restart_required": False,
                "desktop_restart_status": "up_to_date",
                "desktop_tool_scan_verified": True,
                "conversation_attachment_verified": True,
                "end_to_end_verified": True,
                "updated_at": CHECKED_AT,
            },
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
        )

        self.assertEqual("configured", report["overall_state"])
        self.assertTrue(report["configured"])
        self.assertFalse(report["connected"])
        self.assertEqual("pending", report["stages"]["conversation"]["state"])

    def test_legacy_evidence_from_an_old_attempt_is_stale(self) -> None:
        report = diagnostic_from_bundle_status(
            {
                "attempt_id": "old-attempt",
                "config_fingerprint": "sha256:old-config",
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "direct_stdio_verified": True,
                "desktop_app_server_loader_verified": True,
                "updated_at": CHECKED_AT,
            },
            attempt_id=ATTEMPT,
            config_fingerprint=FINGERPRINT,
        )

        self.assertEqual("pending", report["overall_state"])
        self.assertEqual("stale", report["stages"]["registration"]["state"])
        self.assertEqual("stale_attempt", report["stages"]["registration"]["reason_code"])
        self.assertFalse(report["connected"])

    def test_claude_desktop_uses_its_own_configuration_and_runtime_evidence(self) -> None:
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "claude_desktop_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": "sha256:runtime-current",
                "claude_desktop_config_transport_runtime_fingerprint": (
                    "sha256:runtime-current"
                ),
                "claude_desktop_config_registered": True,
                "claude_desktop_config_transport_verified": True,
                "claude_desktop_restart_required": True,
                # These Codex-host values must not satisfy Claude Desktop stages.
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
            },
            connection_target="claude-desktop",
        )

        self.assertEqual("claude-desktop", report["connection_target"])
        self.assertEqual(
            [
                "registration",
                "transport",
                "desktop_reload",
                "loader",
                "desktop_surface",
                "conversation",
            ],
            report["stage_order"],
        )
        self.assertTrue(report["configured"])
        self.assertEqual("configured", report["overall_state"])
        self.assertEqual("desktop_reload", report["first_blocking_stage"])
        self.assertEqual("verified", report["stages"]["registration"]["state"])
        self.assertEqual("verified", report["stages"]["transport"]["state"])
        self.assertEqual("pending", report["stages"]["loader"]["state"])
        self.assertEqual("not_applicable", report["stages"]["fresh_app_server"]["state"])

    def test_claude_desktop_does_not_borrow_direct_registration(self) -> None:
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "installed_config_fingerprint": FINGERPRINT,
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "direct_stdio_verified": True,
            },
            connection_target="claude-desktop",
        )

        self.assertEqual("pending", report["overall_state"])
        self.assertFalse(report["configured"])
        self.assertEqual("registration", report["first_blocking_stage"])

    def test_codex_profile_does_not_wait_for_chatgpt_desktop_surface(self) -> None:
        runtime_fingerprint = "sha256:runtime-current"
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "installed_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": runtime_fingerprint,
                "installed_config_transport_runtime_fingerprint": runtime_fingerprint,
                "fresh_codex_app_server_runtime_fingerprint": runtime_fingerprint,
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "direct_stdio_verified": True,
                "installed_config_transport_verified": True,
                "transport_end_to_end_verified": True,
                "desktop_app_server_loader_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
            },
            connection_target="codex",
        )

        self.assertEqual(
            [
                "registration",
                "loader",
                "transport",
                "fresh_app_server",
                "conversation",
            ],
            report["stage_order"],
        )
        self.assertTrue(report["configured"])
        self.assertFalse(report["connected"])
        self.assertEqual("conversation", report["first_blocking_stage"])
        self.assertEqual("not_applicable", report["stages"]["desktop_reload"]["state"])
        self.assertEqual("not_applicable", report["stages"]["desktop_surface"]["state"])

    def test_post_restart_observer_status_satisfies_chatgpt_reload_only(self) -> None:
        runtime_fingerprint = "sha256:runtime-current"
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "installed_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": runtime_fingerprint,
                "installed_config_transport_runtime_fingerprint": runtime_fingerprint,
                "fresh_codex_app_server_runtime_fingerprint": runtime_fingerprint,
                "direct_config_registered": True,
                "direct_config_loader_verified": True,
                "installed_config_transport_verified": True,
                "transport_end_to_end_verified": True,
                "desktop_app_server_loader_verified": True,
                "fresh_codex_app_server_inventory_verified": True,
                "desktop_restart_required": False,
                "desktop_restart_status": "running_process_started_after_registration",
                "desktop_restarted_after_registration": True,
            },
            connection_target="chatgpt-desktop-local",
        )

        self.assertEqual("verified", report["stages"]["desktop_reload"]["state"])
        self.assertEqual("pending", report["stages"]["desktop_surface"]["state"])
        self.assertFalse(report["connected"])

    def test_post_restart_observer_status_satisfies_claude_reload_only(self) -> None:
        runtime_fingerprint = "sha256:runtime-current"
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "claude_desktop_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": runtime_fingerprint,
                "claude_desktop_config_transport_runtime_fingerprint": runtime_fingerprint,
                "claude_desktop_config_registered": True,
                "claude_desktop_config_transport_verified": True,
                "claude_desktop_restart_required": False,
                "claude_desktop_restart_status": (
                    "running_process_started_after_registration"
                ),
                "claude_desktop_restarted_after_registration": True,
                "claude_desktop_loader_observed": True,
            },
            connection_target="claude-desktop",
        )

        self.assertEqual("verified", report["stages"]["desktop_reload"]["state"])
        self.assertEqual("pending", report["stages"]["loader"]["state"])
        self.assertFalse(report["connected"])

    def test_claude_code_profile_uses_user_scope_loader_and_runtime_evidence(self) -> None:
        runtime_fingerprint = "sha256:runtime-current"
        report = diagnostic_from_bundle_status(
            {
                "installation_attempt_id": ATTEMPT,
                "claude_code_config_fingerprint": FINGERPRINT,
                "runtime_fingerprint": runtime_fingerprint,
                "claude_code_transport_runtime_fingerprint": runtime_fingerprint,
                "claude_code_registered": True,
                "claude_code_loader_verified": True,
                "claude_code_transport_verified": True,
                "claude_code_conversation_verified": False,
                # Desktop values must not become Claude Code requirements.
                "desktop_restart_required": True,
                "desktop_tool_scan_verified": False,
            },
            connection_target="claude-code",
        )

        self.assertEqual(
            ["registration", "loader", "transport", "conversation"],
            report["stage_order"],
        )
        self.assertTrue(report["configured"])
        self.assertEqual("conversation", report["first_blocking_stage"])
        self.assertEqual("not_applicable", report["stages"]["desktop_reload"]["state"])
        self.assertFalse(report["connected"])

    def test_v5_diagnostic_never_leaks_codex_success_into_chatgpt_desktop(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=CHECKED_AT),
            "codex",
            ATTEMPT,
            started_at=CHECKED_AT,
        )
        status = commit_success(
            status,
            "codex",
            ATTEMPT,
            verified_stages=("registration", "loader", "transport", "fresh_app_server"),
            config_entry_fingerprint="codex-config",
            runtime_fingerprint="runtime-current",
            bundle_location_fingerprint="bundle-current",
            verified_at=CHECKED_AT,
        )

        codex = diagnostic_from_bundle_status(status, connection_target="codex")
        chatgpt = diagnostic_from_bundle_status(
            status,
            connection_target="chatgpt-desktop-local",
        )

        self.assertTrue(codex["configured"])
        self.assertEqual("client_connections", codex["status_source"])
        self.assertFalse(chatgpt["configured"])
        self.assertEqual("registration", chatgpt["first_blocking_stage"])
        self.assertEqual("not_checked", chatgpt["stages"]["registration"]["state"])

    def test_v5_diagnostic_uses_each_remote_client_record(self) -> None:
        required_stages = {
            "chatgpt-remote": ("transport", "registration", "loader"),
            "chatgpt-tunnel": ("transport", "registration", "loader"),
            "claude-api": ("transport", "registration"),
        }

        for target, stages in required_stages.items():
            with self.subTest(target=target):
                attempt_id = f"attempt-{target}"
                status = begin_attempt(
                    create_bundle_status("final", generated_at=CHECKED_AT),
                    target,
                    attempt_id,
                    started_at=CHECKED_AT,
                )
                status = commit_success(
                    status,
                    target,
                    attempt_id,
                    verified_stages=stages,
                    config_entry_fingerprint=f"config-{target}",
                    runtime_fingerprint="runtime-current",
                    verified_at=CHECKED_AT,
                )

                report = diagnostic_from_bundle_status(
                    status,
                    connection_target=target,
                )

                self.assertEqual("client_connections", report.get("status_source"))
                self.assertEqual(attempt_id, report["attempt_id"])
                self.assertTrue(report["configured"])

    def test_v5_remote_diagnostic_does_not_inherit_legacy_projection_identity(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=CHECKED_AT),
            "codex",
            ATTEMPT,
            started_at=CHECKED_AT,
        )
        status = commit_success(
            status,
            "codex",
            ATTEMPT,
            verified_stages=("registration", "loader", "transport", "fresh_app_server"),
            config_entry_fingerprint="codex-config",
            runtime_fingerprint="runtime-current",
            bundle_location_fingerprint="bundle-current",
            verified_at=CHECKED_AT,
        )

        for target in ("chatgpt-remote", "chatgpt-tunnel", "claude-api"):
            with self.subTest(target=target):
                report = diagnostic_from_bundle_status(
                    status,
                    connection_target=target,
                )

                self.assertEqual("client_connections", report.get("status_source"))
                self.assertIsNone(report["attempt_id"])
                self.assertIsNone(report["config_fingerprint"])
                self.assertFalse(report["configured"])

    def test_v5_diagnostic_rejects_runtime_bound_stages_from_an_old_runtime(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=CHECKED_AT),
            "codex",
            ATTEMPT,
            started_at=CHECKED_AT,
        )
        status = commit_success(
            status,
            "codex",
            ATTEMPT,
            verified_stages=("registration", "loader", "transport", "fresh_app_server"),
            config_entry_fingerprint="codex-config",
            runtime_fingerprint="runtime-old",
            bundle_location_fingerprint="bundle-current",
            verified_at=CHECKED_AT,
        )
        status["runtime_fingerprint"] = "runtime-current"
        status["client_connections"]["codex"]["effective"][
            "runtime_fingerprint"
        ] = "runtime-current"

        report = diagnostic_from_bundle_status(status, connection_target="codex")

        self.assertFalse(report["configured"])
        for stage_name in ("loader", "transport", "fresh_app_server"):
            with self.subTest(stage_name=stage_name):
                self.assertEqual("stale", report["stages"][stage_name]["state"])
                self.assertEqual(
                    "stale_runtime_fingerprint",
                    report["stages"][stage_name]["reason_code"],
                )

    def test_v5_diagnostic_rejects_runtime_bound_stage_without_runtime_identity(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=CHECKED_AT),
            "codex",
            ATTEMPT,
            started_at=CHECKED_AT,
        )
        status = commit_success(
            status,
            "codex",
            ATTEMPT,
            verified_stages=("registration", "loader", "transport", "fresh_app_server"),
            config_entry_fingerprint="codex-config",
            runtime_fingerprint="runtime-current",
            bundle_location_fingerprint="bundle-current",
            verified_at=CHECKED_AT,
        )
        status["client_connections"]["codex"]["stages"]["transport"][
            "runtime_fingerprint"
        ] = None

        report = diagnostic_from_bundle_status(status, connection_target="codex")

        self.assertFalse(report["configured"])
        self.assertEqual("stale", report["stages"]["transport"]["state"])
        self.assertEqual(
            "evidence_runtime_fingerprint_missing",
            report["stages"]["transport"]["reason_code"],
        )

    def test_v5_diagnostic_binds_client_runtime_to_current_bundle_runtime(self) -> None:
        status = begin_attempt(
            create_bundle_status("final", generated_at=CHECKED_AT),
            "codex",
            ATTEMPT,
            started_at=CHECKED_AT,
        )
        status = commit_success(
            status,
            "codex",
            ATTEMPT,
            verified_stages=("registration", "loader", "transport", "fresh_app_server"),
            config_entry_fingerprint="codex-config",
            runtime_fingerprint="runtime-old",
            bundle_location_fingerprint="bundle-current",
            verified_at=CHECKED_AT,
        )
        status["runtime_fingerprint"] = "runtime-current"

        report = diagnostic_from_bundle_status(status, connection_target="codex")

        self.assertFalse(report["configured"])
        for stage_name in ("loader", "transport", "fresh_app_server"):
            with self.subTest(stage_name=stage_name):
                self.assertEqual("stale", report["stages"][stage_name]["state"])
                self.assertEqual(
                    "stale_runtime_fingerprint",
                    report["stages"][stage_name]["reason_code"],
                )


if __name__ == "__main__":
    unittest.main()
