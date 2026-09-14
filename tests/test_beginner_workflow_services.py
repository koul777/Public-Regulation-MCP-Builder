from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.mcp_connection_service import (
    read_mcp_connection_diagnostic,
    refresh_mcp_connection_observation,
)
from app.services.readiness_adapter import (
    OperatorReadinessState,
    adapt_local_llm_probe,
    adapt_readiness_report,
)
from app.services.workflow_readiness import (
    WorkflowStage,
    next_workflow_stage,
    safe_summarize_workflow_readiness,
    summarize_workflow_readiness,
)
from scripts.mcp_connection_diagnostic import STAGE_ORDER, build_connection_diagnostic


class WorkflowReadinessServiceTests(unittest.TestCase):
    def test_returns_first_incomplete_stage_and_progress_count(self) -> None:
        readiness = summarize_workflow_readiness([True, True, False, False])

        self.assertEqual(2, readiness.completed_steps)
        self.assertEqual(4, readiness.total_steps)
        self.assertEqual(WorkflowStage.APPROVAL, readiness.current_stage)
        self.assertFalse(readiness.is_complete)
        self.assertEqual(
            WorkflowStage.APPROVAL,
            next_workflow_stage([True, True, False, False]),
        )

    def test_complete_workflow_keeps_use_as_safe_fallback_target(self) -> None:
        readiness = summarize_workflow_readiness([True, True, True, True])

        self.assertTrue(readiness.is_complete)
        self.assertIsNone(readiness.current_stage)
        self.assertEqual(WorkflowStage.USE, next_workflow_stage([True] * 4))

    def test_rejects_incomplete_state_vectors(self) -> None:
        with self.assertRaises(ValueError):
            summarize_workflow_readiness([True, False])

    def test_rejects_non_boolean_state_values(self) -> None:
        with self.assertRaises(ValueError):
            summarize_workflow_readiness([True, "false", False, False])

    def test_rejects_out_of_order_stage_completion(self) -> None:
        with self.assertRaises(ValueError):
            summarize_workflow_readiness([True, False, True, False])

    def test_ui_fallback_returns_to_preprocess_for_malformed_state(self) -> None:
        readiness = safe_summarize_workflow_readiness([True, False, True, False])

        self.assertEqual(0, readiness.completed_steps)
        self.assertEqual(WorkflowStage.PREPROCESS, readiness.current_stage)

    def test_exposes_beginner_facing_stage_label(self) -> None:
        self.assertEqual("승인·색인", WorkflowStage.APPROVAL.display_name)


class McpConnectionServiceTests(unittest.TestCase):
    def test_reader_rejects_empty_bundle_dir(self) -> None:
        calls: list[dict[str, object]] = []

        def builder(payload: dict[str, object], **kwargs: object) -> dict[str, object]:
            calls.append({"payload": payload, **kwargs})
            return {"marker": "empty-bundle"}

        diagnostic, read_error = read_mcp_connection_diagnostic(
            "",
            "codex",
            diagnostic_builder=builder,
        )

        self.assertEqual({"marker": "empty-bundle"}, diagnostic)
        self.assertEqual("bundle_dir_unavailable", read_error)
        self.assertEqual("codex", calls[0]["connection_target"])

    def test_reader_reloads_status_and_allows_builder_injection(self) -> None:
        calls: list[dict[str, object]] = []

        def builder(payload: dict[str, object], **kwargs: object) -> dict[str, object]:
            calls.append({"payload": payload, **kwargs})
            return {"marker": payload.get("marker")}

        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "bundle_status.json"
            status_path.write_text(json.dumps({"marker": "first"}), encoding="utf-8")
            first, first_error = read_mcp_connection_diagnostic(
                tmp,
                "codex",
                diagnostic_builder=builder,
            )
            status_path.write_text(json.dumps({"marker": "second"}), encoding="utf-8")
            second, second_error = read_mcp_connection_diagnostic(
                tmp,
                "codex",
                diagnostic_builder=builder,
            )

        self.assertEqual({"marker": "first"}, first)
        self.assertEqual({"marker": "second"}, second)
        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual("codex", calls[-1]["connection_target"])

    def test_reader_does_not_fingerprint_unc_config_paths(self) -> None:
        calls: list[dict[str, object]] = []

        def builder(payload: dict[str, object], **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"marker": payload.get("marker")}

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bundle_status.json").write_text(
                json.dumps(
                    {
                        "marker": "network-path",
                        "direct_config_registered": True,
                        "direct_config_path": r"\\server\share\mcp-config.toml",
                    }
                ),
                encoding="utf-8",
            )
            read_mcp_connection_diagnostic(
                tmp,
                "codex",
                diagnostic_builder=builder,
            )

        self.assertIsNone(calls[0]["config_fingerprint"])

    def test_reader_does_not_fingerprint_oversized_config_files(self) -> None:
        calls: list[dict[str, object]] = []

        def builder(payload: dict[str, object], **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"marker": payload.get("marker")}

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "large-config.toml"
            config_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
            Path(tmp, "bundle_status.json").write_text(
                json.dumps(
                    {
                        "marker": "oversized-path",
                        "direct_config_registered": True,
                        "direct_config_path": str(config_path),
                    }
                ),
                encoding="utf-8",
            )
            read_mcp_connection_diagnostic(
                tmp,
                "codex",
                diagnostic_builder=builder,
            )

        self.assertIsNone(calls[0]["config_fingerprint"])

    def test_observer_never_claims_connection_for_unobservable_target(self) -> None:
        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "codex",
            "regulation_mcp",
        )

        self.assertFalse(observed)
        self.assertEqual("target_not_observable", reason)

    def test_observer_rejects_retired_chatgpt_local_refresh_target(self) -> None:
        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "chatgpt-desktop-local",
            "regulation_mcp",
        )

        self.assertFalse(observed)
        self.assertEqual("target_not_observable", reason)

    def test_observer_records_pending_result_without_claiming_success(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str], *, stdout: object) -> int:
            calls.append(list(argv))
            stdout.write(json.dumps({"ok": False, "status_updated": True}))
            return 0

        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "claude-desktop",
            "regulation_mcp",
            refresh_runner=runner,
        )

        self.assertTrue(observed)
        self.assertEqual("observation_recorded_pending", reason)
        self.assertNotIn("--adopt-manual-registration", calls[0])

    def test_observer_surfaces_refresh_error_code_as_failure(self) -> None:
        def runner(argv: list[str], *, stdout: object) -> int:
            stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "status_updated": True,
                        "error_code": "observation_write_failed",
                    }
                )
            )
            return 1

        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "claude-desktop",
            "regulation_mcp",
            refresh_runner=runner,
        )

        self.assertFalse(observed)
        self.assertEqual("observation_write_failed", reason)

    def test_observer_treats_nonzero_exit_without_error_code_as_failure(self) -> None:
        def runner(argv: list[str], *, stdout: object) -> int:
            stdout.write(json.dumps({"ok": False, "status_updated": True}))
            return 1

        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "claude-desktop",
            "regulation_mcp",
            refresh_runner=runner,
        )

        self.assertFalse(observed)
        self.assertEqual("refresh_failed", reason)

    def test_observer_redacts_unsafe_refresh_error_code(self) -> None:
        def runner(argv: list[str], *, stdout: object) -> int:
            stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "status_updated": True,
                        "error_code": r"C:\secret\config.toml",
                    }
                )
            )
            return 1

        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "claude-desktop",
            "regulation_mcp",
            refresh_runner=runner,
        )

        self.assertFalse(observed)
        self.assertEqual("refresh_failed", reason)

    def test_observer_rejects_non_object_refresh_report(self) -> None:
        def runner(argv: list[str], *, stdout: object) -> int:
            stdout.write(json.dumps(["not", "an", "object"]))
            return 0

        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "claude-desktop",
            "regulation_mcp",
            refresh_runner=runner,
        )

        self.assertFalse(observed)
        self.assertEqual("refresh_report_invalid", reason)

    def test_observer_converts_probe_exception_to_safe_failure(self) -> None:
        def runner(argv: list[str], *, stdout: object) -> int:
            raise RuntimeError("probe unavailable")

        observed, reason = refresh_mcp_connection_observation(
            "fixture-bundle",
            "claude-desktop",
            "regulation_mcp",
            refresh_runner=runner,
        )

        self.assertFalse(observed)
        self.assertEqual("refresh_failed", reason)


class ReadinessAdapterTests(unittest.TestCase):
    def test_local_llm_probe_uses_safe_common_contract(self) -> None:
        available = adapt_local_llm_probe({"available": True})
        malformed = adapt_local_llm_probe("not-json")

        self.assertEqual(OperatorReadinessState.READY, available.state)
        self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, malformed.state)
        self.assertEqual("local_backend_unavailable", malformed.reason_code)

    def test_maps_connected_diagnostic_to_ready(self) -> None:
        card = adapt_readiness_report(
            {"overall_state": "connected"},
            component="Claude Desktop",
        )

        self.assertEqual(OperatorReadinessState.READY, card.state)
        self.assertEqual("준비 완료", card.display_name)
        self.assertEqual("ok", card.reason_code)

    def test_real_diagnostic_pending_without_registration_maps_to_registration_required(self) -> None:
        report = build_connection_diagnostic(
            {},
            attempt_id="attempt-current",
            config_fingerprint="sha256:current",
            connection_target="claude-desktop",
        )

        card = adapt_readiness_report(report, component="Claude Desktop")

        self.assertEqual(OperatorReadinessState.REGISTRATION_REQUIRED, card.state)
        self.assertIn("MCP 설정", card.next_action)

    def test_real_diagnostic_configured_without_conversation_is_pending(self) -> None:
        stages = {
            name: {
                "state": "verified",
                "attempt_id": "attempt-current",
                "checked_at": "2026-09-14T00:00:00Z",
                "reason_code": "ok",
                "evidence": {"config_fingerprint": "sha256:current"},
            }
            for name in STAGE_ORDER[:4]
        }
        report = build_connection_diagnostic(
            stages,
            attempt_id="attempt-current",
            config_fingerprint="sha256:current",
            connection_target="codex",
        )

        card = adapt_readiness_report(report, component="Codex CLI")

        self.assertEqual("configured", report["overall_state"])
        self.assertEqual(OperatorReadinessState.CONFIGURED_PENDING, card.state)

    def test_real_diagnostic_states_map_table_driven(self) -> None:
        verified_stages = {
            name: {
                "state": "verified",
                "attempt_id": "attempt-current",
                "checked_at": "2026-09-14T00:00:00Z",
                "reason_code": "ok",
                "evidence": {"config_fingerprint": "sha256:current"},
            }
            for name in STAGE_ORDER
        }
        verified_stages["conversation"]["evidence"].update(
            {
                "tool_call_verified": True,
                "server_name": "regulation_mcp",
                "tool_name": "search",
                "result_nonce": "nonce-current",
                "conversation_id": "conversation-current",
            }
        )
        cases = {
            "connected": (verified_stages, "attempt-current", OperatorReadinessState.READY),
            "stale": (
                {**verified_stages, "registration": {**verified_stages["registration"], "attempt_id": "attempt-old"}},
                "attempt-current",
                OperatorReadinessState.STALE,
            ),
            "failed": (
                {**verified_stages, "registration": {**verified_stages["registration"], "state": "failed", "reason_code": "failed"}},
                "attempt-current",
                OperatorReadinessState.ACTION_REQUIRED,
            ),
        }
        for name, (stages, attempt_id, expected_state) in cases.items():
            with self.subTest(name=name):
                report = build_connection_diagnostic(
                    stages,
                    attempt_id=attempt_id,
                    config_fingerprint="sha256:current",
                    connection_target="claude-desktop",
                )
                card = adapt_readiness_report(report, component="Claude Desktop")
                self.assertEqual(expected_state, card.state)

    def test_maps_configured_remote_without_probe_to_pending(self) -> None:
        card = adapt_readiness_report(
            {
                "passed": True,
                "public_url": "https://example.invalid/mcp",
                "remote_probe": {"performed": False},
            },
            component="ChatGPT web",
        )

        self.assertEqual(OperatorReadinessState.CONFIGURED_PENDING, card.state)
        self.assertIn("실제 도구 호출", card.next_action)

    def test_maps_high_finding_to_action_required_without_exposing_detail(self) -> None:
        card = adapt_readiness_report(
            {
                "passed": False,
                "findings": [
                    {
                        "severity": "high",
                        "code": r"C:\\private\\token.txt",
                    }
                ],
            },
            component="MCP",
        )

        self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)
        self.assertEqual("unknown", card.reason_code)
        self.assertNotIn("private", card.next_action)

    def test_maps_stale_stage_to_stale(self) -> None:
        card = adapt_readiness_report(
            {
                "overall_state": "pending",
                "stages": {"transport": {"state": "stale", "reason_code": "stale"}},
            },
            component="MCP",
        )

        self.assertEqual(OperatorReadinessState.STALE, card.state)
        self.assertEqual("stale", card.reason_code)

    def test_maps_unchecked_diagnostic_to_registration_required(self) -> None:
        card = adapt_readiness_report(
            {"overall_state": "pending"},
            component="MCP",
        )

        self.assertEqual(OperatorReadinessState.REGISTRATION_REQUIRED, card.state)
        self.assertEqual("registration_required", card.reason_code)

    def test_maps_configured_but_unchecked_diagnostic_to_pending(self) -> None:
        card = adapt_readiness_report(
            {
                "overall_state": "pending",
                "configured": True,
            },
            component="MCP",
        )

        self.assertEqual(OperatorReadinessState.CONFIGURED_PENDING, card.state)

    def test_maps_unselected_optional_dependency_to_optional_unused(self) -> None:
        card = adapt_readiness_report(
            None,
            component="Qwen",
            selected=False,
            optional=True,
        )

        self.assertEqual(OperatorReadinessState.OPTIONAL_UNUSED, card.state)
        self.assertIn("건너뛰어도", card.next_action)

    def test_invalid_report_is_unknown_and_not_ready(self) -> None:
        card = adapt_readiness_report([], component="MCP")

        self.assertEqual(OperatorReadinessState.UNKNOWN, card.state)
        self.assertEqual("report_invalid", card.reason_code)

    def test_malformed_overall_state_fails_closed_without_ui_exception(self) -> None:
        card = adapt_readiness_report(
            {"overall_state": ["connected"], "passed": False},
            component="MCP",
        )

        self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)
        self.assertEqual("readiness_failed", card.reason_code)

    def test_component_label_is_bounded_and_path_free(self) -> None:
        card = adapt_readiness_report(
            {"passed": True},
            component=r"C:\private\config.json",
        )

        self.assertEqual("component", card.component)

    def test_extractive_local_llm_mode_is_ready_without_probe(self) -> None:
        card = adapt_readiness_report(
            {
                "report_type": "local_llm_doctor_v1",
                "passed": True,
                "probe": False,
                "reason": "model_free_mode",
            },
            component="Qwen",
        )

        self.assertEqual(OperatorReadinessState.READY, card.state)

    def test_failed_model_free_signal_is_not_ready(self) -> None:
        card = adapt_readiness_report(
            {"passed": False, "reason": "model_free_mode"},
            component="Qwen",
        )

        self.assertEqual(OperatorReadinessState.ACTION_REQUIRED, card.state)

    def test_bare_pass_without_probe_is_pending(self) -> None:
        card = adapt_readiness_report({"passed": True}, component="MCP")

        self.assertEqual(OperatorReadinessState.CONFIGURED_PENDING, card.state)

if __name__ == "__main__":
    unittest.main()
