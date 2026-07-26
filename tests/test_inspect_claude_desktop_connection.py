from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.inspect_claude_desktop_connection import (
    RegistrationObservation,
    evaluate_claude_desktop_observation,
    load_registration_observation,
    parse_claude_log_text,
    run,
)


REGISTRATION_TIME = "2026-07-20T14:00:00Z"
PROCESS_TIME = "2026-07-20T14:05:00Z"
LOG_TIME = "2026-07-20T14:06:00Z"


def installation(
    *,
    succeeded: bool = True,
    appx_detected: bool = True,
    legacy_detected: bool = False,
) -> dict[str, object]:
    return {
        "discovery_succeeded": succeeded,
        "appx_detected": appx_detected,
        "legacy_detected": legacy_detected,
        "process_start_times": [PROCESS_TIME],
    }


def config_observation() -> dict[str, object]:
    return {
        "exists": True,
        "read_succeeded": True,
        "content_sha256": "sha256:" + ("a" * 64),
    }


def registration() -> RegistrationObservation:
    return RegistrationObservation(
        occurred_at=datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        source="bundle_status",
        bundle_status_read_succeeded=True,
    )


class ClaudeDesktopConnectionObserverTests(unittest.TestCase):
    def test_log_parser_retains_only_sanitized_hit_metadata(self) -> None:
        raw = (
            "2026-07-20T14:06:00Z loading policy-mcp from "
            "C:\\Users\\private-user\\Desktop\\bundle\\run.ps1 token=secret-value\n"
        )

        parsed = parse_claude_log_text(raw, server_name="policy-mcp")
        rendered = json.dumps(parsed)

        self.assertEqual(1, parsed["server_name_hit_count"])
        self.assertEqual(0, parsed["server_name_error_hit_count"])
        self.assertNotIn("private-user", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("Desktop", rendered)
        self.assertNotIn("policy-mcp", rendered)

    def test_post_registration_name_hit_is_observation_not_loader_or_conversation_proof(self) -> None:
        session = parse_claude_log_text(
            f"{LOG_TIME} initialized policy-mcp\n",
            server_name="policy-mcp",
        )

        report = evaluate_claude_desktop_observation(
            installation=installation(),
            process_start_times=[PROCESS_TIME],
            registration=registration(),
            log_sessions=[session],
            config_observation=config_observation(),
            log_discovery_succeeded=True,
            log_reads_succeeded=True,
            generated_at="2026-07-20T14:07:00Z",
        )

        self.assertEqual("restart_and_server_name_observed", report["observation_status"])
        self.assertTrue(report["recognition_observation_ready"])
        self.assertTrue(report["claude_desktop_loader_observed"])
        self.assertFalse(report["claude_desktop_loader_verified"])
        self.assertFalse(report["claude_desktop_conversation_verified"])
        self.assertFalse(report["end_to_end_verified"])

    def test_missing_server_name_hit_fails_closed(self) -> None:
        session = parse_claude_log_text(
            f"{LOG_TIME} another server initialized\n",
            server_name="policy-mcp",
        )

        report = evaluate_claude_desktop_observation(
            installation=installation(),
            process_start_times=[PROCESS_TIME],
            registration=registration(),
            log_sessions=[session],
            config_observation=config_observation(),
            log_discovery_succeeded=True,
            log_reads_succeeded=True,
        )

        self.assertEqual(
            "post_registration_server_name_not_observed",
            report["observation_status"],
        )
        self.assertFalse(report["recognition_observation_ready"])
        self.assertFalse(report["claude_desktop_loader_observed"])
        self.assertFalse(report["claude_desktop_loader_verified"])
        self.assertFalse(report["claude_desktop_conversation_verified"])

    def test_legacy_installation_is_detected_without_appx(self) -> None:
        session = parse_claude_log_text(
            f"{LOG_TIME} initialized policy-mcp\n",
            server_name="policy-mcp",
        )

        report = evaluate_claude_desktop_observation(
            installation=installation(appx_detected=False, legacy_detected=True),
            process_start_times=[PROCESS_TIME],
            registration=registration(),
            log_sessions=[session],
            config_observation=config_observation(),
            log_discovery_succeeded=True,
            log_reads_succeeded=True,
        )

        self.assertFalse(report["installation"]["appx_detected"])
        self.assertTrue(report["installation"]["legacy_detected"])
        self.assertTrue(report["installation"]["detected"])
        self.assertTrue(report["recognition_observation_ready"])

    def test_process_predating_registration_requires_restart(self) -> None:
        session = parse_claude_log_text(
            f"{LOG_TIME} initialized policy-mcp\n",
            server_name="policy-mcp",
        )

        report = evaluate_claude_desktop_observation(
            installation=installation(),
            process_start_times=["2026-07-20T13:59:00Z"],
            registration=registration(),
            log_sessions=[session],
            config_observation=config_observation(),
            log_discovery_succeeded=True,
            log_reads_succeeded=True,
        )

        self.assertEqual("desktop_restart_required", report["observation_status"])
        self.assertTrue(report["desktop_process"]["restart_required"])
        self.assertFalse(report["recognition_observation_ready"])

    def test_error_only_name_hits_do_not_make_observation_ready(self) -> None:
        session = parse_claude_log_text(
            f"{LOG_TIME} policy-mcp failed with ENOENT\n",
            server_name="policy-mcp",
        )

        report = evaluate_claude_desktop_observation(
            installation=installation(),
            process_start_times=[PROCESS_TIME],
            registration=registration(),
            log_sessions=[session],
            config_observation=config_observation(),
            log_discovery_succeeded=True,
            log_reads_succeeded=True,
        )

        self.assertEqual("only_server_name_error_hits_observed", report["observation_status"])
        self.assertFalse(report["recognition_observation_ready"])
        self.assertFalse(report["claude_desktop_loader_observed"])

    def test_windows_discovery_failure_discards_positive_install_and_process_claims(self) -> None:
        session = parse_claude_log_text(
            f"{LOG_TIME} initialized policy-mcp\n",
            server_name="policy-mcp",
        )

        report = evaluate_claude_desktop_observation(
            installation=installation(succeeded=False),
            process_start_times=[PROCESS_TIME],
            registration=registration(),
            log_sessions=[session],
            config_observation=config_observation(),
            log_discovery_succeeded=True,
            log_reads_succeeded=True,
        )

        self.assertEqual("windows_discovery_failed", report["observation_status"])
        self.assertFalse(report["installation"]["detected"])
        self.assertFalse(report["desktop_process"]["detected"])
        self.assertFalse(report["recognition_observation_ready"])

    def test_invalid_bundle_status_prevents_ready_even_with_config_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "bundle_status.json"
            status_path.write_text("not-json", encoding="utf-8")
            observed = load_registration_observation(
                status_path,
                config_modified_at=datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
            )

        self.assertEqual("config_mtime", observed.source)
        self.assertFalse(observed.bundle_status_read_succeeded)

    def test_cli_reports_only_config_existence_and_hash_and_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "private-user" / "claude_desktop_config.json"
            config_path.parent.mkdir()
            config_bytes = b'{"token":"must-not-be-reported"}'
            config_path.write_bytes(config_bytes)
            status_path = root / "bundle_status.json"
            status_path.write_text(
                json.dumps({"claude_desktop_registration_updated_at": REGISTRATION_TIME}),
                encoding="utf-8",
            )
            log_root = root / "logs"
            log_root.mkdir()
            log_path = log_root / "session.log"
            log_path.write_text(
                f"{LOG_TIME} initialized policy-mcp from C:\\Users\\private-user\\bundle\n",
                encoding="utf-8",
            )
            output_path = root / "report.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--bundle-status",
                    str(status_path),
                    "--config-path",
                    str(config_path),
                    "--server-name",
                    "policy-mcp",
                    "--log-root",
                    str(log_root),
                    "--out-json",
                    str(output_path),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                windows_discovery=lambda: installation(),
            )

            report = json.loads(stdout.getvalue())
            saved_report = json.loads(output_path.read_text(encoding="utf-8"))
            rendered = json.dumps(report)

        self.assertEqual(0, exit_code)
        self.assertEqual(report, saved_report)
        self.assertEqual(
            "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
            report["config_observation"]["content_sha256"],
        )
        self.assertEqual(
            {"exists", "content_sha256"},
            set(report["config_observation"]),
        )
        self.assertNotIn("private-user", rendered)
        self.assertNotIn("must-not-be-reported", rendered)
        self.assertNotIn(str(root), rendered)

    def test_cli_fail_on_issue_returns_two_without_log_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "claude_desktop_config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_root = root / "logs"
            log_root.mkdir()
            (log_root / "session.log").write_text(
                f"{LOG_TIME} unrelated startup\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--config-path",
                    str(config_path),
                    "--server-name",
                    "policy-mcp",
                    "--log-root",
                    str(log_root),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                windows_discovery=lambda: installation(),
            )

        self.assertEqual(2, exit_code)
        self.assertFalse(json.loads(stdout.getvalue())["recognition_observation_ready"])

    def test_cli_windows_discovery_exception_fails_closed_without_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_root = root / "logs"
            log_root.mkdir()
            stdout = io.StringIO()

            def fail_discovery() -> dict[str, object]:
                raise RuntimeError("C:\\Users\\private-user\\secret")

            exit_code = run(
                [
                    "--config-path",
                    str(root / "missing-config.json"),
                    "--server-name",
                    "policy-mcp",
                    "--log-root",
                    str(log_root),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                windows_discovery=fail_discovery,
            )
            report = json.loads(stdout.getvalue())

        self.assertEqual(2, exit_code)
        self.assertEqual("windows_discovery_failed", report["observation_status"])
        self.assertFalse(report["installation"]["discovery_succeeded"])
        self.assertNotIn("private-user", stdout.getvalue())
        self.assertNotIn("secret", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
