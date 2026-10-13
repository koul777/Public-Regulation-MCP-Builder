from __future__ import annotations

import json
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services import vercel_deployment_service as service


class VercelDeploymentServiceTests(unittest.TestCase):
    def test_publication_requires_explicit_public_approval_before_any_io(self) -> None:
        with patch.object(service, "_run") as run, patch.object(service, "deployment_identity") as identity:
            with self.assertRaises(service.DeploymentError):
                service.deploy_public_runtime(runtime=Path("missing"), server_name="demo", scope="team",
                    project="demo", public_approved=False, replace_existing=False, progress=Mock())
            run.assert_not_called()
            identity.assert_not_called()

    def test_option_injection_and_shell_characters_are_rejected(self) -> None:
        for value in ("--prod", "foo;bar", "$(whoami)", "a\nb", "abc/def", "x---y"):
            with self.subTest(value=value), self.assertRaises(service.DeploymentError):
                service._slug(value)

    def test_runner_uses_argv_and_removes_inherited_secrets_and_project(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch.object(service, "vercel_command", return_value=["node", "C:/path with spaces/vercel.js"]), \
             patch.object(service.subprocess, "run", return_value=completed) as run, \
             patch.dict(service.os.environ, {"VERCEL_TOKEN": "secret", "VERCEL_PROJECT_ID": "wrong", "MCP_AUTH_TOKEN": "secret",
                                            "GIT_DIR": "private-parent/.git", "GIT_CONFIG_COUNT": "1"}):
            service._run(["whoami"], cwd=Path.cwd())
        self.assertEqual("C:/path with spaces/vercel.js", run.call_args.args[0][1])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        for name in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID", "MCP_AUTH_TOKEN", "GIT_DIR", "GIT_CONFIG_COUNT"):
            self.assertNotIn(name, run.call_args.kwargs["env"])

    @unittest.skipUnless(shutil.which("git"), "Git is required to exercise ancestor repository discovery")
    def test_staging_commands_cannot_discover_or_link_the_parent_git_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "builder"
            repository.mkdir()
            baseline_env = {key: value for key, value in service.os.environ.items()
                            if not key.upper().startswith("GIT_")}
            subprocess.run(["git", "init", "--quiet", str(repository)], env=baseline_env,
                           check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/private.git"],
                           cwd=repository, env=baseline_env, check=True, capture_output=True)
            stage = repository / "reports" / "vercel-mcp-stage"
            stage.mkdir(parents=True)
            baseline = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=stage,
                                      env=baseline_env, capture_output=True, text=True)
            self.assertEqual(0, baseline.returncode)
            self.assertEqual(repository.resolve(), Path(baseline.stdout.strip()).resolve())
            with patch.dict(service.os.environ, {"GIT_DIR": str(repository / ".git"),
                                                "GIT_WORK_TREE": str(repository),
                                                "GIT_CEILING_DIRECTORIES": str(Path(temp).parent)}):
                isolated = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=stage,
                                          env=service._environment(stage), capture_output=True, text=True)
            self.assertNotEqual(0, isolated.returncode)
            self.assertEqual("", isolated.stdout.strip())
            self.assertFalse((stage / ".git").exists())

    def test_raw_cli_logs_and_timeout_never_reach_ui(self) -> None:
        for response in (subprocess.CompletedProcess([], 1, "private document", "secret-value"),
                         subprocess.TimeoutExpired(["command", "secret-value"], 3)):
            with patch.object(service, "vercel_command", return_value=["node"]), \
                 patch.object(service.subprocess, "run", side_effect=response if isinstance(response, Exception) else None,
                              return_value=response):
                with self.assertRaises(service.DeploymentError) as caught:
                    service._run(["whoami"], cwd=Path.cwd())
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertNotIn("private document", str(caught.exception))

    def test_old_cli_option_errors_offer_update_without_exposing_logs(self) -> None:
        for message in ("unknown option", "unknown or unexpected option", "unrecognized option", "unexpected option"):
            response = subprocess.CompletedProcess([], 1, "secret-value", f"{message}: --non-interactive; login help")
            with self.subTest(message=message), \
                 patch.object(service, "vercel_command", return_value=["node"]), \
                 patch.object(service.subprocess, "run", return_value=response):
                with self.assertRaises(service.DeploymentError) as caught:
                    service._run(["whoami"], cwd=Path.cwd())
                self.assertEqual("cli_outdated", caught.exception.code)
                self.assertNotIn("secret-value", str(caught.exception))

    def test_unsupported_json_or_project_format_offers_cli_update(self) -> None:
        for response in ("legacy human-readable secret-value", "[]"):
            with self.subTest(response=response), patch.object(service, "_run", return_value=response):
                with self.assertRaises(service.DeploymentError) as caught:
                    service._json(["whoami"], cwd=Path.cwd())
                self.assertEqual("cli_outdated", caught.exception.code)
                self.assertNotIn("secret-value", str(caught.exception))
        with patch.object(service, "_json", return_value={"projects": {}}):
            with self.assertRaises(service.DeploymentError) as caught:
                service.load_projects(Path.cwd(), "team", Mock())
            self.assertEqual("cli_outdated", caught.exception.code)

    def test_only_ready_production_alias_becomes_connection_url(self) -> None:
        good = {"readyState": "READY", "target": "production", "alias": ["demo.vercel.app"]}
        self.assertEqual("https://demo.vercel.app/mcp", service.production_mcp_url(good))
        for change in ({"readyState": "BUILDING"}, {"target": "preview"}, {"alias": []},
                       {"alias": ["http://demo.vercel.app"]}, {"alias": ["demo.vercel.app.evil.test"]},
                       {"alias": ["https://user:secret@demo.vercel.app"]}):
            with self.subTest(change=change), self.assertRaises(service.DeploymentError):
                service.production_mcp_url({**good, **change})

    def test_pagination_loads_all_projects(self) -> None:
        with patch.object(service, "_json", side_effect=[
            {"projects": [{"name": "first", "id": "p1"}], "pagination": {"next": 42}},
            {"projects": [{"name": "second", "id": "p2"}], "pagination": {"next": None}},
        ]) as request:
            result = service.load_projects(Path.cwd(), "team", Mock())
        self.assertEqual(2, len(result["projects"]))
        self.assertIn("42", request.call_args.args[0])

    def test_connect_reuses_login_and_loads_only_selected_or_single_team(self) -> None:
        account = {"username": "demo", "scopes": [{"slug": "one"}, {"slug": "two"}]}
        with patch.object(service, "load_account", return_value=account), \
             patch.object(service, "load_projects", return_value={"scope": "two"}) as projects, \
             patch.object(service, "login") as login:
            self.assertEqual({}, service.connect_account(Path.cwd(), "", Mock())["projects"])
            projects.assert_not_called()
            self.assertEqual("two", service.connect_account(Path.cwd(), "two", Mock())["projects"]["scope"])
            login.assert_not_called()

    def test_connect_opens_login_only_for_authentication_failure(self) -> None:
        for code in ("login_required", "network"):
            with self.subTest(code=code), \
                 patch.object(service, "load_account", side_effect=service.DeploymentError("failure", code=code)), \
                 patch.object(service, "login", return_value={"username": "demo", "scopes": [{"slug": "one"}]}) as login, \
                 patch.object(service, "load_projects", return_value={"scope": "one"}):
                if code == "login_required":
                    self.assertEqual("one", service.connect_account(Path.cwd(), "", Mock())["projects"]["scope"])
                    login.assert_called_once()
                else:
                    with self.assertRaises(service.DeploymentError):
                        service.connect_account(Path.cwd(), "", Mock())
                    login.assert_not_called()

    def test_changed_bundle_retains_destination_but_cannot_reuse_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            manifest = runtime / "mcp_runtime_manifest.json"
            manifest.write_text('{"tenant_id":"one","profile_id":"alpha","record_count":1}', encoding="utf-8")
            result = {"identity": service.deployment_identity(runtime), "url": "https://demo.vercel.app/mcp",
                      "scope": "team", "project": "demo", "verified": True}
            service._save_deployment(runtime, result)
            manifest.write_text('{"tenant_id":"one","profile_id":"alpha","record_count":2}', encoding="utf-8")
            self.assertEqual({}, service.read_deployment(runtime))
            self.assertEqual("demo", service.read_previous_deployment(runtime)["project"])
            with patch.object(service, "run_mcp_client_config_smoke") as smoke:
                with self.assertRaises(service.DeploymentError):
                    service.verify_deployment(runtime, result, Mock())
                smoke.assert_not_called()
            manifest.write_text('{"tenant_id":"one","profile_id":"different"}', encoding="utf-8")
            self.assertEqual({}, service.read_previous_deployment(runtime))

    def test_saved_receipt_is_bound_to_current_bundle_and_failed_probe_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            manifest = runtime / "mcp_runtime_manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            result = {"identity": service.deployment_identity(runtime), "url": "https://demo.vercel.app/mcp", "server_name": "demo"}
            with patch.object(service, "run_mcp_client_config_smoke", return_value={
                "mcp_initialized": True, "tools_discovered": True, "end_to_end_verified": False,
                "error": "secret-value", "results": [{"text": "private regulation"}],
            }):
                checked = service.verify_deployment(runtime, result, Mock())
            self.assertFalse(checked["verified"])
            self.assertEqual(result["url"], service.read_deployment(runtime)["url"])
            self.assertNotIn("secret-value", service._receipt_path(runtime).read_text(encoding="utf-8"))
            manifest.write_text('{"changed": true}', encoding="utf-8")
            self.assertEqual({}, service.read_deployment(runtime))

    def test_pipeline_stages_only_verified_runtime_and_requires_existing_project_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            (runtime / "mcp_runtime_manifest.json").write_text("{}", encoding="utf-8")
            args = dict(runtime=runtime, server_name="demo", scope="team", project="demo",
                        public_approved=True, replace_existing=False, progress=Mock())
            with patch.object(service, "validate_mcp_runtime_data_bundle_integrity", return_value={"tenant_id": "tenant"}), \
                 patch.object(service, "load_projects", return_value={"projects": [{"id": "p1", "name": "demo"}]}), \
                 patch.object(service, "prepare_vercel_mcp_deployment") as stage, \
                 patch.object(service, "_run") as run:
                with self.assertRaises(service.DeploymentError):
                    service.deploy_public_runtime(**args)
                stage.assert_not_called()
                run.assert_not_called()
            with patch.object(service, "validate_mcp_runtime_data_bundle_integrity", return_value={"tenant_id": "tenant"}), \
                 patch.object(service, "load_projects", return_value={"projects": []}), \
                 patch.object(service, "prepare_vercel_mcp_deployment") as stage, \
                 patch.object(service, "_run", side_effect=["linked", "https://build.vercel.app"]) as run, \
                 patch.object(service, "_json", return_value={"readyState": "READY", "target": "production", "alias": ["demo.vercel.app"]}), \
                 patch.object(service, "run_mcp_client_config_smoke", return_value={
                     "mcp_initialized": True, "tools_discovered": True, "end_to_end_verified": True,
                 }):
                result = service.deploy_public_runtime(**args)
            self.assertTrue(result["verified"])
            self.assertIn("--archive=tgz", run.call_args_list[-1].args[0])
            self.assertTrue(stage.call_args.kwargs["verify_sealed_runtime"])
            self.assertNotEqual(runtime, stage.call_args.kwargs["out_dir"])
            self.assertTrue(all(call.kwargs["cwd"] == stage.call_args.kwargs["out_dir"] for call in run.call_args_list))

    def test_invalid_bundle_and_duplicate_job_cannot_deploy(self) -> None:
        args = dict(runtime=Path("missing"), server_name="demo", scope="team", project="demo",
                    public_approved=True, replace_existing=False, progress=Mock())
        with patch.object(service, "deployment_identity", return_value="id"), \
             patch.object(service, "validate_mcp_runtime_data_bundle_integrity", side_effect=ValueError("private path")), \
             patch.object(service, "_run") as run:
            with self.assertRaises(service.DeploymentError):
                service.deploy_public_runtime(**args)
            run.assert_not_called()
        with service._DEPLOY_LOCK:
            with self.assertRaises(service.DeploymentError):
                service.deploy_public_runtime(**args)

    def test_inspection_failure_keeps_uploaded_deployment_for_restart_without_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            (runtime / "mcp_runtime_manifest.json").write_text("{}", encoding="utf-8")
            args = dict(runtime=runtime, server_name="demo", scope="team", project="demo",
                        public_approved=True, replace_existing=False, progress=Mock())

            def unavailable_inspection(*_args, **_kwargs):
                pending = service.read_deployment(runtime)
                self.assertEqual("https://build.vercel.app", pending["deployment_url"])
                self.assertEqual("pending", pending["status"])
                self.assertFalse(pending["verified"])
                self.assertNotIn("url", pending)
                raise service.DeploymentError("network unavailable", code="network")

            with patch.object(service, "validate_mcp_runtime_data_bundle_integrity", return_value={"tenant_id": "tenant"}), \
                 patch.object(service, "load_projects", return_value={"projects": []}), \
                 patch.object(service, "prepare_vercel_mcp_deployment"), \
                 patch.object(service, "_run", side_effect=["linked", "https://build.vercel.app"]) as upload, \
                 patch.object(service, "_json", side_effect=unavailable_inspection):
                with self.assertRaises(service.DeploymentError):
                    service.deploy_public_runtime(**args)
                self.assertEqual(2, upload.call_count)

            # A new UI session obtains its recovery state entirely from disk.
            pending = service.read_deployment(runtime)
            with patch.object(service, "_run") as upload, \
                 patch.object(service, "_json", return_value={"readyState": "READY", "target": "production", "alias": ["demo.vercel.app"]}) as inspect, \
                 patch.object(service, "run_mcp_client_config_smoke", return_value={
                     "mcp_initialized": True, "tools_discovered": True, "end_to_end_verified": True,
                 }):
                result = service.verify_deployment(runtime, pending, Mock())
                upload.assert_not_called()
                self.assertEqual(["inspect", "https://build.vercel.app", "--scope", "team"], inspect.call_args.args[0])
            self.assertEqual("https://demo.vercel.app/mcp", result["url"])
            self.assertTrue(result["verified"])
            self.assertTrue(service.read_deployment(runtime)["verified"])

    def test_pending_deployment_keeps_recovery_state_until_production_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            (runtime / "mcp_runtime_manifest.json").write_text("{}", encoding="utf-8")
            pending = {"identity": service.deployment_identity(runtime), "server_name": "demo",
                       "scope": "team", "project": "demo", "deployment_url": "https://build.vercel.app",
                       "verified": False, "status": "pending"}
            service._save_deployment(runtime, pending)
            with patch.object(service, "_json", return_value={"readyState": "BUILDING", "target": "production"}), \
                 patch.object(service, "run_mcp_client_config_smoke") as smoke:
                with self.assertRaises(service.DeploymentError):
                    service.verify_deployment(runtime, pending, Mock())
                smoke.assert_not_called()
            self.assertEqual(pending, service.read_deployment(runtime))

    def test_bundle_regenerated_during_remote_check_cannot_save_a_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            manifest = runtime / "mcp_runtime_manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            result = {"identity": service.deployment_identity(runtime), "url": "https://demo.vercel.app/mcp",
                      "server_name": "demo", "scope": "team", "project": "demo", "verified": False}
            service._save_deployment(runtime, result)

            def successful_check(**_kwargs):
                manifest.write_text('{"record_count":2}', encoding="utf-8")
                return {"mcp_initialized": True, "tools_discovered": True, "end_to_end_verified": True}

            with patch.object(service, "run_mcp_client_config_smoke", side_effect=successful_check):
                with self.assertRaises(service.DeploymentError) as caught:
                    service.verify_deployment(runtime, result, Mock())
            self.assertEqual("stale_bundle", caught.exception.code)
            self.assertEqual({}, service.read_deployment(runtime))
            self.assertFalse(service.read_previous_deployment(runtime)["verified"])

    def test_background_failure_redacts_arbitrary_exception(self) -> None:
        job = service.DeploymentJob(Mock(side_effect=RuntimeError("secret-value")))
        self.assertTrue(job.done.wait(timeout=2))
        self.assertIsNone(job.result)
        self.assertNotIn("secret-value", job.error)

    def test_login_shows_only_official_device_link_and_cleans_up_process(self) -> None:
        process = Mock(stdout=io.StringIO(
            "secret-value\nhttps://evil.test/oauth/device?user_code=ABCD\n"
            "Visit https://vercel.com/oauth/device?user_code=ABCD-EFGH\n"
        ))
        process.wait.return_value = 0
        process.poll.return_value = 0
        progress = Mock()
        with patch.object(service, "vercel_command", return_value=["node", "vercel.js"]), \
             patch.object(service.subprocess, "Popen", return_value=process), \
             patch.object(service, "load_account", return_value={"username": "demo"}):
            self.assertEqual("demo", service.login(Path.cwd(), progress)["username"])
        messages = " ".join(call.args[0] for call in progress.call_args_list)
        self.assertIn("https://vercel.com/oauth/device?user_code=ABCD-EFGH", messages)
        self.assertNotIn("evil.test", messages)
        self.assertNotIn("secret-value", messages)
        self.assertTrue(process.stdout.closed)

    def test_staging_verifies_the_copy_before_compression(self) -> None:
        from scripts import prepare_vercel_mcp_deployment as staging

        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "bundle/data"
            runtime.mkdir(parents=True)
            (runtime / "mcp_runtime_manifest.json").write_text('{"tenant_id":"demo"}', encoding="utf-8")
            with patch("scripts.generate_mcp_client_config.validate_mcp_runtime_data_bundle_integrity", side_effect=ValueError("changed copy")) as verify, \
                 patch.object(staging, "_compress_oversized_runtime_files") as compress:
                with self.assertRaisesRegex(ValueError, "changed copy"):
                    staging.prepare_vercel_mcp_deployment(runtime_data_dir=runtime, out_dir=Path(temp) / "stage", verify_sealed_runtime=True)
            self.assertEqual((Path(temp) / "stage/mcp_runtime").resolve(), verify.call_args.args[0])
            compress.assert_not_called()


if __name__ == "__main__":
    unittest.main()
