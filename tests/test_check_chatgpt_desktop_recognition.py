from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.check_chatgpt_desktop_recognition import (
    RegistrationObservation,
    build_support_summary,
    evaluate_recognition_observation,
    load_registration_observation,
    parse_desktop_log_text,
    parse_timestamp,
    run,
)


SUCCESS_LOG = """\
2026-07-20T14:10:00.000Z info desktop_started
2026-07-20T14:10:04.000Z info [AppServerConnection] response_routed durationMs=4000 errorCode=null method=mcpServerStatus/list
"""


class ChatGptDesktopRecognitionTests(unittest.TestCase):
    def test_parse_timestamp_normalizes_offsets_to_utc(self) -> None:
        parsed = parse_timestamp("2026-07-20T23:10:00+09:00")

        self.assertEqual(datetime(2026, 7, 20, 14, 10, tzinfo=timezone.utc), parsed)

    def test_log_parser_counts_error_free_status_response_without_exposure_claim(self) -> None:
        session = parse_desktop_log_text(SUCCESS_LOG)

        self.assertEqual("2026-07-20T14:10:00Z", session["started_at"])
        self.assertEqual(1, session["mcp_status_list_event_count"])
        self.assertEqual(1, session["mcp_status_list_success_count"])
        self.assertEqual(0, session["mcp_status_list_error_count"])

    def test_log_parser_does_not_treat_error_response_as_success(self) -> None:
        session = parse_desktop_log_text(
            "2026-07-20T14:10:04Z error response_routed "
            "errorCode=mcp_start_failed method=mcpServerStatus/list\n"
        )

        self.assertEqual(0, session["mcp_status_list_success_count"])
        self.assertEqual(1, session["mcp_status_list_error_count"])

    def test_restart_and_status_observation_never_verifies_tools_or_conversation(self) -> None:
        report = evaluate_recognition_observation(
            registration=RegistrationObservation(
                parse_timestamp("2026-07-20T14:00:00Z"),
                "explicit",
            ),
            process_start_times=["2026-07-20T14:09:59Z"],
            log_sessions=[parse_desktop_log_text(SUCCESS_LOG)],
            generated_at="2026-07-20T14:20:00Z",
        )

        self.assertTrue(report["recognition_observation_ready"])
        self.assertEqual("restart_and_mcp_status_list_observed", report["observation_status"])
        self.assertFalse(report["desktop_tool_scan_verified"])
        self.assertFalse(report["conversation_attachment_verified"])
        self.assertTrue(report["conversation_attachment_unverified"])
        self.assertFalse(report["end_to_end_verified"])

    def test_process_predating_registration_requires_restart_even_with_new_renderer(self) -> None:
        report = evaluate_recognition_observation(
            registration=RegistrationObservation(parse_timestamp("2026-07-20T14:00:00Z"), "explicit"),
            process_start_times=["2026-07-20T13:00:00Z", "2026-07-20T14:05:00Z"],
            log_sessions=[parse_desktop_log_text(SUCCESS_LOG)],
        )

        self.assertFalse(report["recognition_observation_ready"])
        self.assertTrue(report["desktop_process"]["restart_required"])
        self.assertEqual("restart_required", report["observation_status"])

    def test_pre_registration_log_session_is_not_new_desktop_evidence(self) -> None:
        old_session = parse_desktop_log_text(
            "2026-07-20T13:00:00Z info desktop_started\n"
            "2026-07-20T13:00:04Z info response_routed errorCode=null method=mcpServerStatus/list\n"
        )
        report = evaluate_recognition_observation(
            registration=RegistrationObservation(parse_timestamp("2026-07-20T14:00:00Z"), "explicit"),
            process_start_times=["2026-07-20T14:10:00Z"],
            log_sessions=[old_session],
        )

        self.assertFalse(report["recognition_observation_ready"])
        self.assertFalse(report["desktop_logs"]["post_registration_session_observed"])
        self.assertEqual("post_registration_log_session_not_observed", report["observation_status"])

    def test_new_session_without_successful_status_response_is_incomplete(self) -> None:
        session = parse_desktop_log_text("2026-07-20T14:10:00Z info desktop_started\n")
        report = evaluate_recognition_observation(
            registration=RegistrationObservation(parse_timestamp("2026-07-20T14:00:00Z"), "explicit"),
            process_start_times=["2026-07-20T14:10:00Z"],
            log_sessions=[session],
        )

        self.assertFalse(report["recognition_observation_ready"])
        self.assertEqual("mcp_status_list_success_not_observed", report["observation_status"])

    def test_not_running_is_distinct_from_restart_required(self) -> None:
        report = evaluate_recognition_observation(
            registration=RegistrationObservation(parse_timestamp("2026-07-20T14:00:00Z"), "explicit"),
            process_start_times=[],
            log_sessions=[],
        )

        self.assertFalse(report["desktop_process"]["detected"])
        self.assertFalse(report["desktop_process"]["restart_required"])
        self.assertEqual("desktop_not_running", report["observation_status"])

    def test_registration_prefers_explicit_then_bundle_status_then_config_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "bundle_status.json"
            status.write_text(
                json.dumps({"desktop_mcp_registration_updated_at": "2026-07-20T14:02:41Z"}),
                encoding="utf-8",
            )
            config = root / "config.toml"
            config.write_text("# config\n", encoding="utf-8")
            os.utime(config, (1_750_000_000, 1_750_000_000))

            explicit = load_registration_observation(
                registration_time="2026-07-20T15:00:00Z",
                bundle_status_path=status,
                config_path=config,
            )
            bundled = load_registration_observation(bundle_status_path=status, config_path=config)
            mtime = load_registration_observation(config_path=config)

        self.assertEqual("explicit", explicit.source)
        self.assertEqual("bundle_status:desktop_mcp_registration_updated_at", bundled.source)
        self.assertEqual("config_mtime", mtime.source)

    def test_support_summary_contains_no_paths_usernames_or_raw_log_text(self) -> None:
        report = {
            "observation_status": r"failed C:\fixture-private\Desktop\secret.log",
            "registration": {"observed": True, "source": r"C:\fixture-private\config.toml"},
            "desktop_process": {"detected": True, "restart_required": False},
            "desktop_logs": {
                "post_registration_session_observed": True,
                "mcp_status_list_observed_without_error": True,
            },
        }

        serialized = json.dumps(build_support_summary(report), ensure_ascii=False)

        self.assertNotIn("alice", serialized.casefold())
        self.assertNotIn("secret.log", serialized)
        self.assertNotIn("C:\\Users", serialized)
        self.assertIn("local-path-redacted", serialized)

    def test_cli_explicit_evidence_writes_path_free_report_and_fail_gate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "codex-desktop-alice.log"
            log_path.write_text(SUCCESS_LOG, encoding="utf-8")
            out_path = root / "recognition.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--registration-time",
                    "2026-07-20T14:00:00Z",
                    "--process-start-time",
                    "2026-07-20T14:09:59Z",
                    "--log-file",
                    str(log_path),
                    "--out-json",
                    str(out_path),
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            written = out_path.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["recognition_observation_ready"])
        self.assertNotIn("alice", written.casefold())
        self.assertNotIn(str(log_path), written)
        self.assertFalse(payload["desktop_tool_scan_verified"])
        self.assertFalse(payload["conversation_attachment_verified"])
        self.assertFalse(payload["end_to_end_verified"])

    def test_cli_config_observation_reports_only_content_sha256(self) -> None:
        config_content = b'[mcp_servers.private]\ncommand = "powershell.exe"\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "private-user-alice" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_bytes(config_content)
            out_path = root / "recognition.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--registration-time",
                    "2026-07-20T14:00:00Z",
                    "--config-path",
                    str(config_path),
                    "--out-json",
                    str(out_path),
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            written = out_path.read_text(encoding="utf-8")

        expected_sha256 = "sha256:" + hashlib.sha256(config_content).hexdigest()
        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"exists": True, "content_sha256": expected_sha256},
            payload["config_observation"],
        )
        for rendered in (
            stdout.getvalue(),
            written,
            json.dumps(payload["support_summary"], ensure_ascii=False),
        ):
            self.assertNotIn(str(config_path), rendered)
            self.assertNotIn("private-user-alice", rendered)

    def test_cli_missing_config_is_path_free_and_reports_not_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "missing-private-user" / "config.toml"
            out_path = root / "recognition.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--registration-time",
                    "2026-07-20T14:00:00Z",
                    "--config-path",
                    str(config_path),
                    "--out-json",
                    str(out_path),
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            written = out_path.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"exists": False, "content_sha256": None},
            payload["config_observation"],
        )
        for rendered in (
            stdout.getvalue(),
            written,
            json.dumps(payload["support_summary"], ensure_ascii=False),
        ):
            self.assertNotIn(str(config_path), rendered)
            self.assertNotIn("missing-private-user", rendered)

    def test_cli_unreadable_config_is_path_free_and_reports_not_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "unreadable-private-user" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_text("# unreadable during observation\n", encoding="utf-8")
            stdout = io.StringIO()

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=PermissionError("simulated config read denial"),
            ):
                exit_code = run(
                    [
                        "--registration-time",
                        "2026-07-20T14:00:00Z",
                        "--config-path",
                        str(config_path),
                    ],
                    stdout=stdout,
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {"exists": False, "content_sha256": None},
            payload["config_observation"],
        )
        rendered_support = json.dumps(payload["support_summary"], ensure_ascii=False)
        self.assertNotIn(str(config_path), stdout.getvalue())
        self.assertNotIn(str(config_path), rendered_support)
        self.assertNotIn("unreadable-private-user", stdout.getvalue())
        self.assertNotIn("unreadable-private-user", rendered_support)

    def test_cli_fail_gate_rejects_missing_post_registration_session(self) -> None:
        stdout = io.StringIO()
        with mock.patch(
            "scripts.check_chatgpt_desktop_recognition.discover_desktop_process_start_times",
            return_value=[parse_timestamp("2026-07-20T14:10:00Z")],
        ), mock.patch(
            "scripts.check_chatgpt_desktop_recognition.discover_log_files",
            return_value=[],
        ):
            exit_code = run(
                ["--registration-time", "2026-07-20T14:00:00Z", "--fail-on-issue"],
                stdout=stdout,
            )

        self.assertEqual(2, exit_code)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["recognition_observation_ready"])
        self.assertFalse(payload["desktop_tool_scan_verified"])


if __name__ == "__main__":
    unittest.main()
