from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import mcp_client_status
from scripts.mcp_connection_diagnostic import diagnostic_from_bundle_status
from scripts import refresh_mcp_client_connection as refresh


OBSERVED_AT = "2026-07-21T00:00:03Z"
_FAKE_WINDOWS_HOME = Path("C:/") / "Users" / "private-user"
_FAKE_CODEX_CONFIG = _FAKE_WINDOWS_HOME / ".codex" / "config.toml"
_FAKE_CLAUDE_CONFIG = (
    _FAKE_WINDOWS_HOME
    / "AppData"
    / "Roaming"
    / "Claude"
    / "claude_desktop_config.json"
)


def _status(*, server_name: str = "sample_mcp") -> dict[str, object]:
    return {
        "report_type": "mcp_bundle_status",
        "server_name": server_name,
        "installation_attempt_id": "attempt-existing-001",
        "installation_state": "installed_loader_verified",
        "connection_state": "configured_pending_conversation_verification",
        "direct_config_path": str(_FAKE_CODEX_CONFIG),
        "claude_desktop_config_path": str(_FAKE_CLAUDE_CONFIG),
        "desktop_tool_scan_verified": True,
        "conversation_attachment_verified": True,
        "end_to_end_verified": True,
        "claude_desktop_loader_verified": True,
        "claude_desktop_conversation_verified": True,
        "unrelated_secret": "do-not-copy-this-value",
    }


def _chatgpt_report(*, ready: bool = True) -> dict[str, object]:
    return {
        "generated_at": OBSERVED_AT,
        "observation_status": (
            "restart_and_mcp_status_list_observed"
            if ready
            else "desktop_log_files_not_found"
        ),
        "recognition_observation_ready": ready,
        "registration": {
            "observed": True,
            "source": r"C:\secret\bundle_status.json",
        },
        "desktop_process": {
            "detected": True,
            "count": 2,
            "post_registration_process_count": 1,
            "restart_required": False,
            "restart_status": "running_process_started_after_registration",
            "executable_path": r"C:\secret\ChatGPT.exe",
        },
        "desktop_logs": {
            "discovery_status": "logs_loaded",
            "log_file_count": 7,
            "post_registration_session_observed": ready,
            "mcp_status_list_observed_without_error": ready,
            "mcp_status_list_error_count": 0,
            "raw_log": "secret log line",
        },
        "server_name": "sample_mcp",
        "token": "super-secret-token",
    }


def _claude_report(*, ready: bool = True) -> dict[str, object]:
    return {
        "generated_at": OBSERVED_AT,
        "observation_status": (
            "restart_and_server_name_observed"
            if ready
            else "post_registration_server_name_not_observed"
        ),
        "recognition_observation_ready": ready,
        "installation": {"detected": True, "install_path": r"C:\secret\Claude.exe"},
        "registration": {"observed": True},
        "config_observation": {
            "exists": True,
            "content_sha256": "sha256:private-config-fingerprint",
        },
        "desktop_process": {
            "detected": True,
            "count": 1,
            "post_registration_process_count": 1,
            "restart_required": False,
            "restart_status": "running_process_started_after_registration",
        },
        "desktop_logs": {
            "discovery_succeeded": True,
            "reads_succeeded": True,
            "post_registration_session_observed": ready,
            "post_registration_server_name_observed": ready,
            "raw_log": "sample_mcp bearer private-value",
        },
        "claude_desktop_loader_observed": ready,
        "claude_desktop_loader_verified": True,
        "claude_desktop_conversation_verified": True,
    }


class RefreshMcpClientConnectionTests(unittest.TestCase):
    def _write_status(self, root: Path, payload: dict[str, object] | None = None) -> Path:
        path = root / "bundle_status.json"
        path.write_text(
            json.dumps(payload or _status(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_manual_registration_files(
        self,
        root: Path,
        *,
        config_args_suffix: str | None = None,
    ) -> tuple[Path, Path]:
        bundle_status = _status()
        bundle_status["installation_attempt_id"] = None
        bundle_status["direct_config_registered"] = False
        status_path = self._write_status(root, bundle_status)
        launcher = root / "run_mcp_stdio_server.ps1"
        data_dir = root / "data"
        args = ["-NoProfile", "-File", str(launcher), "--data-dir", str(data_dir)]

        def toml_string(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)

        snippet_lines = [
            "[mcp_servers.sample_mcp]",
            'command = "powershell.exe"',
            "startup_timeout_sec = 45",
            f"cwd = {toml_string(str(root))}",
            "args = [",
            *[f"  {toml_string(value)}," for value in args],
            "]",
        ]
        (root / "codex_config_snippet.toml").write_text(
            "\n".join(snippet_lines) + "\n",
            encoding="utf-8",
        )
        config_args = list(args)
        if config_args_suffix is not None:
            config_args.append(config_args_suffix)
        config_lines = [
            "[mcp_servers.sample_mcp]",
            'command = "powershell.exe"',
            f"cwd = {toml_string(str(root))}",
            "enabled = true",
            "startup_timeout_sec = 120",
            "args = [",
            *[f"  {toml_string(value)}," for value in config_args],
            "]",
            "",
            "[unrelated]",
            'private_token = "must-not-be-reported"',
        ]
        config_path = root / "current_codex_config.toml"
        config_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
        return status_path, config_path

    def test_chatgpt_refresh_preserves_attempt_and_verification_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path = self._write_status(root)
            report_path = root / "refresh.json"
            stdout = io.StringIO()
            observed: dict[str, object] = {}

            def probe(target: str, bundle: Path, config: Path | None, server: str):
                observed.update(target=target, bundle=bundle, config=config, server=server)
                return _chatgpt_report()

            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--out-json",
                    str(report_path),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                probe=probe,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(observed["target"], "chatgpt-desktop-local")
            self.assertEqual(observed["bundle"], status_path)
            self.assertEqual(
                observed["config"], _FAKE_CODEX_CONFIG
            )
            self.assertEqual(observed["server"], "sample_mcp")

            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["installation_attempt_id"], "attempt-existing-001")
            self.assertEqual(updated["installation_state"], "installed_loader_verified")
            self.assertEqual(
                updated["connection_state"], "configured_pending_conversation_verification"
            )
            self.assertTrue(updated["desktop_tool_scan_verified"])
            self.assertTrue(updated["conversation_attachment_verified"])
            self.assertTrue(updated["end_to_end_verified"])
            self.assertEqual(
                updated["desktop_recognition_observation_status"],
                "restart_and_mcp_status_list_observed",
            )
            self.assertTrue(updated["desktop_status_scan_request_observed"])

            observation = updated["chatgpt_desktop_connection_observation"]
            observation_text = json.dumps(observation, ensure_ascii=False)
            self.assertNotIn("sample_mcp", observation_text)
            self.assertNotIn("private-user", observation_text)
            self.assertNotIn("secret", observation_text)
            self.assertFalse(observation["tool_exposure_verified"])
            self.assertFalse(observation["conversation_attachment_verified"])
            self.assertFalse(observation["end_to_end_verified"])

            rendered = stdout.getvalue()
            self.assertEqual(json.loads(rendered), json.loads(report_path.read_text(encoding="utf-8")))
            self.assertNotIn("sample_mcp", rendered)
            self.assertNotIn("private-user", rendered)
            self.assertNotIn("secret", rendered)
            self.assertFalse(json.loads(rendered)["connection_verified"])

    def test_claude_refresh_uses_only_sanitized_observer_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path = self._write_status(root)
            stdout = io.StringIO()

            exit_code = refresh.run(
                [
                    "--target",
                    "claude-desktop",
                    "--server-name",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                probe=lambda *_args: _claude_report(),
            )

            self.assertEqual(exit_code, 0)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["installation_attempt_id"], "attempt-existing-001")
            self.assertTrue(updated["claude_desktop_loader_observed"])
            self.assertTrue(updated["claude_desktop_loader_verified"])
            self.assertTrue(updated["claude_desktop_conversation_verified"])
            observation = updated["claude_desktop_connection_observation"]
            self.assertTrue(observation["loader_observed"])
            self.assertFalse(observation["loader_verified"])
            self.assertFalse(observation["conversation_attachment_verified"])
            observation_text = json.dumps(observation, ensure_ascii=False)
            self.assertNotIn("sample_mcp", observation_text)
            self.assertNotIn("fingerprint", observation_text)
            self.assertNotIn("secret", observation_text)
            self.assertNotIn("sample_mcp", stdout.getvalue())

    def test_fail_on_issue_returns_two_after_recording_negative_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = self._write_status(Path(temp_dir))
            stdout = io.StringIO()
            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--fail-on-issue",
                ],
                stdout=stdout,
                probe=lambda *_args: _chatgpt_report(ready=False),
            )

            self.assertEqual(exit_code, 2)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(
                updated["desktop_recognition_observation_status"],
                "desktop_log_files_not_found",
            )
            self.assertFalse(
                updated["chatgpt_desktop_connection_observation"][
                    "recognition_observation_ready"
                ]
            )

    def test_missing_attempt_is_rejected_without_running_probe_or_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status = _status()
            status["installation_attempt_id"] = None
            status_path = self._write_status(Path(temp_dir), status)
            before = status_path.read_bytes()
            stdout = io.StringIO()
            called = False

            def probe(*_args):
                nonlocal called
                called = True
                return _chatgpt_report()

            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                ],
                stdout=stdout,
                probe=probe,
            )

            self.assertEqual(exit_code, 1)
            self.assertFalse(called)
            self.assertEqual(status_path.read_bytes(), before)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["error_code"], "installation_attempt_id_missing")
            self.assertNotIn(str(status_path), stdout.getvalue())

    def test_server_mismatch_is_rejected_without_identity_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = self._write_status(Path(temp_dir), _status(server_name="recorded-private"))
            before = status_path.read_bytes()
            stdout = io.StringIO()
            exit_code = refresh.run(
                [
                    "--target",
                    "claude-desktop",
                    "--server",
                    "supplied-private",
                    "--bundle-status",
                    str(status_path),
                ],
                stdout=stdout,
                probe=lambda *_args: self.fail("probe must not run"),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(status_path.read_bytes(), before)
            self.assertEqual(
                json.loads(stdout.getvalue())["error_code"],
                "bundle_status_server_identity_mismatch",
            )
            self.assertNotIn("recorded-private", stdout.getvalue())
            self.assertNotIn("supplied-private", stdout.getvalue())

    def test_concurrent_status_change_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = self._write_status(Path(temp_dir))
            stdout = io.StringIO()

            def probe(*_args):
                changed = json.loads(status_path.read_text(encoding="utf-8"))
                changed["external_writer_value"] = "preserve-me"
                status_path.write_text(json.dumps(changed), encoding="utf-8")
                return _chatgpt_report()

            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                ],
                stdout=stdout,
                probe=probe,
            )

            self.assertEqual(exit_code, 1)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["external_writer_value"], "preserve-me")
            self.assertNotIn("chatgpt_desktop_connection_observation", updated)
            self.assertEqual(
                json.loads(stdout.getvalue())["error_code"],
                "bundle_status_changed_during_observation",
            )

    def test_out_json_cannot_replace_bundle_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = self._write_status(Path(temp_dir))
            before = status_path.read_bytes()
            stdout = io.StringIO()
            exit_code = refresh.run(
                [
                    "--target",
                    "claude-desktop",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--out-json",
                    str(status_path),
                ],
                stdout=stdout,
                probe=lambda *_args: _claude_report(),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(status_path.read_bytes(), before)
            self.assertEqual(
                json.loads(stdout.getvalue())["error_code"],
                "out_json_must_not_replace_bundle_status",
            )

    def test_sanitizer_rejects_free_form_status_and_invalid_timestamp(self) -> None:
        report = _chatgpt_report()
        report["observation_status"] = str(_FAKE_WINDOWS_HOME / "secret.log")
        report["generated_at"] = "bearer private-token"
        observation = refresh.sanitize_observation("chatgpt-desktop-local", report)
        self.assertEqual(observation["observation_status"], "unknown")
        self.assertIsNone(observation["observed_at"])
        self.assertNotIn("private", json.dumps(observation))

    def test_explicit_manual_registration_adoption_requires_exact_effective_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(root)
            config_before = config_path.read_bytes()
            snippet_before = (root / "codex_config_snippet.toml").read_bytes()
            config_fingerprint = "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
            stdout = io.StringIO()
            probe_saw_adoption = False

            def probe(target: str, bundle: Path, config: Path | None, server: str):
                nonlocal probe_saw_adoption
                adopted = json.loads(bundle.read_text(encoding="utf-8"))
                probe_saw_adoption = (
                    adopted["installation_attempt_id"] == "manual-test-attempt"
                    and adopted["direct_config_registered"] is True
                    and config == config_path.resolve()
                )
                return _chatgpt_report(ready=False)

            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--bundle-dir",
                    str(root),
                    "--codex-config",
                    str(config_path),
                    "--adopt-manual-registration",
                ],
                stdout=stdout,
                probe=probe,
                attempt_id_factory=lambda: "manual-test-attempt",
                clock=lambda: datetime(2026, 7, 21, 1, 2, 3, tzinfo=timezone.utc),
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(probe_saw_adoption)
            self.assertEqual(config_path.read_bytes(), config_before)
            self.assertEqual((root / "codex_config_snippet.toml").read_bytes(), snippet_before)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["installation_attempt_id"], "manual-test-attempt")
            self.assertEqual(
                updated["installation_state"],
                "installed_pending_desktop_verification",
            )
            self.assertEqual(updated["connection_state"], "pending_desktop_verification")
            self.assertTrue(updated["direct_config_registered"])
            self.assertEqual(updated["direct_config_path"], str(config_path.resolve()))
            self.assertEqual(updated["installed_config_fingerprint"], config_fingerprint)
            self.assertEqual(
                updated["desktop_mcp_registration_updated_at"],
                "2026-07-21T01:02:03Z",
            )
            for key in (
                "direct_config_loader_verified",
                "installed_config_transport_verified",
                "direct_stdio_verified",
                "transport_end_to_end_verified",
                "fresh_codex_app_server_inventory_verified",
                "desktop_app_server_loader_verified",
                "desktop_tool_scan_verified",
                "conversation_attachment_verified",
                "end_to_end_verified",
            ):
                self.assertFalse(updated[key], key)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["manual_registration_adopted"])
            self.assertTrue(result["installation_attempt_created_by_explicit_adoption"])
            self.assertFalse(result["installation_attempt_preserved"])
            self.assertFalse(result["connection_verified"])
            rendered = stdout.getvalue()
            self.assertNotIn("sample_mcp", rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("must-not-be-reported", rendered)
            self.assertNotIn(config_fingerprint, rendered)

    def test_v5_manual_registration_updates_selected_client_registration_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(root)
            initial = mcp_client_status.create_bundle_status(
                "sample_mcp",
                runtime_fingerprint="runtime-current",
                bundle_fingerprint="bundle-current",
                generated_at="2026-07-21T00:00:00Z",
            )
            initial = mcp_client_status.begin_attempt(
                initial,
                "codex",
                "prior-codex-attempt",
                started_at="2026-07-21T00:00:01Z",
            )
            initial = mcp_client_status.commit_success(
                initial,
                "codex",
                "prior-codex-attempt",
                verified_stages=(
                    "registration",
                    "loader",
                    "transport",
                    "fresh_app_server",
                ),
                config_entry_fingerprint="prior-codex-config",
                runtime_fingerprint=initial["runtime_fingerprint"],
                bundle_fingerprint=initial["bundle_fingerprint"],
                bundle_location_fingerprint="prior-codex-location",
                verified_at="2026-07-21T00:00:02Z",
            )
            self._write_status(root, initial)
            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--bundle-dir",
                    str(root),
                    "--codex-config",
                    str(config_path),
                    "--adopt-manual-registration",
                ],
                stdout=io.StringIO(),
                probe=lambda *_args: _chatgpt_report(ready=False),
                attempt_id_factory=lambda: "manual-v5-attempt",
                clock=lambda: datetime(2026, 7, 21, 1, 2, 3, tzinfo=timezone.utc),
            )

            self.assertEqual(0, exit_code)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            record = updated["client_connections"]["chatgpt-desktop-local"]
            codex_record = updated["client_connections"]["codex"]
            self.assertEqual("stale", codex_record["effective"]["state"])
            self.assertEqual(
                "shared_config_replaced",
                codex_record["stages"]["registration"]["reason_code"],
            )
            self.assertEqual("completed", record["last_attempt"]["state"])
            self.assertEqual("manual-v5-attempt", record["last_attempt"]["id"])
            self.assertEqual("partially_verified", record["effective"]["state"])
            self.assertEqual(
                "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest(),
                record["effective"]["config_entry_fingerprint"],
            )
            self.assertEqual("verified", record["stages"]["registration"]["state"])
            for stage_name in (
                "loader",
                "transport",
                "fresh_app_server",
                "client_reload",
                "client_surface",
                "conversation",
            ):
                with self.subTest(stage_name=stage_name):
                    self.assertEqual(
                        "not_checked",
                        record["stages"][stage_name]["state"],
                    )
            self.assertTrue(updated["direct_config_registered"])
            self.assertFalse(updated["direct_config_loader_verified"])
            self.assertFalse(updated["desktop_tool_scan_verified"])
            self.assertFalse(updated["conversation_attachment_verified"])
            self.assertFalse(updated["end_to_end_verified"])
            diagnostic = diagnostic_from_bundle_status(
                updated,
                connection_target="chatgpt-desktop-local",
            )
            self.assertEqual("client_connections", diagnostic["status_source"])
            self.assertEqual("completed", diagnostic["last_attempt_state"])
            self.assertEqual("verified", diagnostic["stages"]["registration"]["state"])
            self.assertEqual("pending", diagnostic["overall_state"])
            self.assertFalse(diagnostic["configured"])
            self.assertFalse(diagnostic["connected"])

    def test_v5_later_transport_observation_stays_within_registration_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(root)
            initial = mcp_client_status.create_bundle_status(
                "sample_mcp",
                runtime_fingerprint="runtime-current",
                bundle_fingerprint="bundle-current",
                generated_at="2026-07-21T00:00:00Z",
            )
            self._write_status(root, initial)
            common_args = [
                "--target",
                "chatgpt-desktop-local",
                "--server",
                "sample_mcp",
                "--bundle-status",
                str(status_path),
                "--bundle-dir",
                str(root),
                "--codex-config",
                str(config_path),
                "--adopt-manual-registration",
            ]
            first_exit = refresh.run(
                common_args,
                stdout=io.StringIO(),
                probe=lambda *_args: _chatgpt_report(ready=False),
                attempt_id_factory=lambda: "manual-v5-later-transport",
                clock=lambda: datetime(2026, 7, 21, 1, 2, 3, tzinfo=timezone.utc),
            )
            second_exit = refresh.run(
                common_args,
                stdout=io.StringIO(),
                probe=lambda *_args: _chatgpt_report(ready=True),
            )

            self.assertEqual(0, first_exit)
            self.assertEqual(0, second_exit)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            record = updated["client_connections"]["chatgpt-desktop-local"]
            self.assertEqual("manual-v5-later-transport", record["last_attempt"]["id"])
            self.assertEqual("completed", record["last_attempt"]["state"])
            self.assertEqual("partially_verified", record["effective"]["state"])
            self.assertEqual("verified", record["stages"]["registration"]["state"])
            self.assertEqual("verified", record["stages"]["transport"]["state"])
            self.assertEqual(
                updated["runtime_fingerprint"],
                record["stages"]["transport"]["runtime_fingerprint"],
            )
            for stage_name in (
                "loader",
                "fresh_app_server",
                "client_reload",
                "client_surface",
                "conversation",
            ):
                with self.subTest(stage_name=stage_name):
                    self.assertEqual(
                        "not_checked",
                        record["stages"][stage_name]["state"],
                    )
            self.assertFalse(updated["desktop_tool_scan_verified"])
            self.assertFalse(updated["conversation_attachment_verified"])
            self.assertFalse(updated["end_to_end_verified"])
            diagnostic = diagnostic_from_bundle_status(
                updated,
                connection_target="chatgpt-desktop-local",
            )
            self.assertEqual("verified", diagnostic["stages"]["registration"]["state"])
            self.assertEqual("verified", diagnostic["stages"]["transport"]["state"])
            self.assertEqual("pending", diagnostic["overall_state"])
            self.assertFalse(diagnostic["configured"])
            self.assertFalse(diagnostic["connected"])

    def test_manual_registration_mismatch_fails_closed_without_status_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(
                root,
                config_args_suffix="--different",
            )
            before = status_path.read_bytes()
            stdout = io.StringIO()
            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--bundle-dir",
                    str(root),
                    "--codex-config",
                    str(config_path),
                    "--adopt-manual-registration",
                ],
                stdout=stdout,
                probe=lambda *_args: self.fail("probe must not run"),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(status_path.read_bytes(), before)
            self.assertEqual(
                json.loads(stdout.getvalue())["error_code"],
                "manual_registration_entry_mismatch",
            )
            self.assertNotIn("sample_mcp", stdout.getvalue())
            self.assertNotIn(str(root), stdout.getvalue())

    def test_manual_registration_is_never_adopted_without_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(root)
            before = status_path.read_bytes()
            stdout = io.StringIO()
            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--bundle-dir",
                    str(root),
                    "--codex-config",
                    str(config_path),
                ],
                stdout=stdout,
                probe=lambda *_args: self.fail("probe must not run"),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(status_path.read_bytes(), before)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["error_code"], "installation_attempt_id_missing")
            self.assertFalse(result["manual_registration_adopted"])

    def test_manual_registration_source_change_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(root)
            before = status_path.read_bytes()
            stdout = io.StringIO()

            def mutate_config_before_commit() -> str:
                config_path.write_text(
                    config_path.read_text(encoding="utf-8") + "# concurrent change\n",
                    encoding="utf-8",
                )
                return "manual-racing-attempt"

            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--bundle-dir",
                    str(root),
                    "--codex-config",
                    str(config_path),
                    "--adopt-manual-registration",
                ],
                stdout=stdout,
                probe=lambda *_args: self.fail("probe must not run"),
                attempt_id_factory=mutate_config_before_commit,
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(status_path.read_bytes(), before)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["error_code"], "manual_registration_source_changed")
            self.assertFalse(result["manual_registration_adopted"])

    def test_manual_registration_does_not_overwrite_concurrent_status_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_path, config_path = self._write_manual_registration_files(root)
            stdout = io.StringIO()

            def mutate_status_before_commit() -> str:
                changed = json.loads(status_path.read_text(encoding="utf-8"))
                changed["external_writer_value"] = "preserve-me"
                status_path.write_text(json.dumps(changed), encoding="utf-8")
                return "manual-racing-attempt"

            exit_code = refresh.run(
                [
                    "--target",
                    "chatgpt-desktop-local",
                    "--server",
                    "sample_mcp",
                    "--bundle-status",
                    str(status_path),
                    "--bundle-dir",
                    str(root),
                    "--codex-config",
                    str(config_path),
                    "--adopt-manual-registration",
                ],
                stdout=stdout,
                probe=lambda *_args: self.fail("probe must not run"),
                attempt_id_factory=mutate_status_before_commit,
            )

            self.assertEqual(exit_code, 1)
            updated = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["external_writer_value"], "preserve-me")
            self.assertIsNone(updated["installation_attempt_id"])
            self.assertFalse(updated["direct_config_registered"])
            self.assertEqual(
                json.loads(stdout.getvalue())["error_code"],
                "bundle_status_changed_during_manual_adoption",
            )

    def test_manual_registration_ambiguous_or_missing_entry_fails_closed(self) -> None:
        cases = (
            (
                "ambiguous",
                '\n[mcp_servers.SAMPLE_MCP]\ncommand = "powershell.exe"\nargs = []\n',
                "manual_registration_config_entry_ambiguous",
            ),
            (
                "missing",
                None,
                "manual_registration_config_entry_missing",
            ),
        )
        for label, suffix, error_code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                status_path, config_path = self._write_manual_registration_files(root)
                if suffix is None:
                    config_path.write_text(
                        config_path.read_text(encoding="utf-8").replace(
                            "[mcp_servers.sample_mcp]",
                            "[mcp_servers.another_server]",
                        ),
                        encoding="utf-8",
                    )
                else:
                    config_path.write_text(
                        config_path.read_text(encoding="utf-8") + suffix,
                        encoding="utf-8",
                    )
                before = status_path.read_bytes()
                stdout = io.StringIO()
                exit_code = refresh.run(
                    [
                        "--target",
                        "chatgpt-desktop-local",
                        "--server",
                        "sample_mcp",
                        "--bundle-status",
                        str(status_path),
                        "--bundle-dir",
                        str(root),
                        "--codex-config",
                        str(config_path),
                        "--adopt-manual-registration",
                    ],
                    stdout=stdout,
                    probe=lambda *_args: self.fail("probe must not run"),
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(status_path.read_bytes(), before)
                self.assertEqual(json.loads(stdout.getvalue())["error_code"], error_code)

    def test_default_codex_config_path_prefers_codex_home_then_user_profile(self) -> None:
        with mock.patch.dict(
            refresh.os.environ,
            {"CODEX_HOME": r"C:\isolated-codex", "USERPROFILE": r"C:\profile"},
            clear=True,
        ):
            self.assertEqual(
                refresh._default_codex_config_path(),
                Path(r"C:\isolated-codex") / "config.toml",
            )
        with mock.patch.dict(
            refresh.os.environ,
            {"CODEX_HOME": "", "USERPROFILE": r"C:\profile"},
            clear=True,
        ):
            self.assertEqual(
                refresh._default_codex_config_path(),
                Path(r"C:\profile") / ".codex" / "config.toml",
            )


if __name__ == "__main__":
    unittest.main()
