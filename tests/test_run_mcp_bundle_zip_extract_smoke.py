from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import scripts.run_mcp_bundle_zip_extract_smoke as bundle_zip_extract_smoke
from scripts.run_mcp_bundle_zip_extract_smoke import (
    _client_config_path_checks,
    _extract_archive_safely,
    run_mcp_bundle_zip_extract_smoke,
)


class RunMcpBundleZipExtractSmokeTests(unittest.TestCase):
    def test_relative_paths_are_resolved_before_powershell_changes_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            extracted = root / "extracted"
            _write_client_configs(
                source,
                launcher=extracted / "run_mcp_stdio_server.ps1",
                data_dir=extracted / "data",
            )
            (source / "validate_client_config_smoke.ps1").write_text("exit 0\n", encoding="utf-8")
            (source / "run_mcp_stdio_server.ps1").write_text("exit 0\n", encoding="utf-8")
            (source / "mcp_client_config_smoke.json").write_text('{"passed": true}\n', encoding="utf-8")
            bundle_zip = root / "bundle.zip"
            with zipfile.ZipFile(bundle_zip, "w") as archive:
                for path in source.rglob("*"):
                    if path.is_file():
                        archive.write(path, arcname=path.relative_to(source).as_posix())
                archive.writestr("data/.keep", "")

            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    mock.patch(
                        "scripts.run_mcp_bundle_zip_extract_smoke._powershell_command",
                        return_value="powershell.exe",
                    ),
                    mock.patch(
                        "scripts.run_mcp_bundle_zip_extract_smoke.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, "", ""),
                    ) as run_mock,
                    mock.patch(
                        "scripts.run_mcp_bundle_zip_extract_smoke.current_repo_commit",
                        return_value="test-commit",
                    ),
                    mock.patch(
                        "scripts.run_mcp_bundle_zip_extract_smoke.shutil.which",
                        return_value=None,
                    ),
                    mock.patch.dict(os.environ, {"REG_RAG_PYTHON": str(root / "stale-python.exe")}),
                ):
                    report = run_mcp_bundle_zip_extract_smoke(
                        bundle_zip="bundle.zip",
                        extract_dir="extracted",
                        server_name="govreg-local",
                    )
            finally:
                os.chdir(previous_cwd)

        command = run_mock.call_args.args[0]
        child_env = run_mock.call_args.kwargs["env"]
        self.assertTrue(Path(command[command.index("-File") + 1]).is_absolute())
        self.assertEqual(str((extracted / "validate_client_config_smoke.ps1").resolve()), command[-1])
        self.assertEqual(str(bundle_zip.resolve()), report["bundle_zip"])
        self.assertEqual(str(extracted.resolve()), report["extract_dir"])
        self.assertEqual(str(Path(os.sys.executable).resolve().parent), child_env["PATH"].split(os.pathsep)[0])
        self.assertEqual(str(Path(os.sys.executable).resolve()), child_env["REG_RAG_PYTHON"])
        self.assertEqual(
            str(bundle_zip_extract_smoke.PROJECT_ROOT.resolve()),
            child_env["REG_RAG_PYTHON_PROJECT_ROOT"],
        )
        self.assertTrue(report["passed"])

    def test_require_console_scripts_reports_environment_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            extracted = root / "nested" / "extracted"
            _write_client_configs(
                source,
                launcher=extracted / "run_mcp_stdio_server.ps1",
                data_dir=extracted / "data",
            )
            (source / "validate_client_config_smoke.ps1").write_text("exit 0\n", encoding="utf-8")
            (source / "run_mcp_stdio_server.ps1").write_text("exit 0\n", encoding="utf-8")
            (source / "mcp_client_config_smoke.json").write_text('{"passed": true}\n', encoding="utf-8")
            bundle_zip = root / "bundle.zip"
            with zipfile.ZipFile(bundle_zip, "w") as archive:
                for path in source.rglob("*"):
                    if path.is_file():
                        archive.write(path, arcname=path.relative_to(source).as_posix())
                archive.writestr("data/.keep", "")

            with (
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke._powershell_command",
                    return_value="powershell.exe",
                ),
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke.shutil.which",
                    return_value=None,
                ),
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke.current_repo_commit",
                    return_value="test-commit",
                ),
            ):
                report = run_mcp_bundle_zip_extract_smoke(
                    bundle_zip=bundle_zip,
                    extract_dir=extracted,
                    server_name="govreg-local",
                    require_console_scripts=True,
                )

        self.assertFalse(report["passed"])
        self.assertFalse(report["environment_checks_passed"])
        self.assertEqual(
            {"reg-rag-mcp-client-config-smoke", "reg-rag-mcp-server"},
            set(report["missing_console_scripts"]),
        )

    def test_path_checks_pass_when_configs_point_to_extracted_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            bundle.mkdir()
            _write_client_configs(bundle, launcher=bundle / "run_mcp_stdio_server.ps1", data_dir=bundle / "data")

            checks = _client_config_path_checks(target_dir=bundle, server_name="govreg-local")

        self.assertTrue(checks["passed"])
        self.assertTrue(checks["clients"]["codex"]["passed"])
        self.assertTrue(checks["clients"]["claude_desktop"]["passed"])
        self.assertTrue(checks["clients"]["chatgpt_desktop_local"]["passed"])
        self.assertTrue(checks["clients"]["chatgpt_desktop_local"]["guard_passed"])
        self.assertTrue(checks["clients"]["chatgpt_desktop_local"]["strict_utf8_without_bom"])
        self.assertTrue(checks["clients"]["chatgpt_desktop_local"]["config_schema_verified"])
        self.assertTrue(
            checks["clients"]["chatgpt_desktop_local"]["warning_only_artifact_verified"]
        )
        self.assertEqual(
            "unsupported",
            checks["clients"]["chatgpt_desktop_local"]["support_status"],
        )
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["direct_local_supported"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["connection_verified"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["process_started"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["runnable_config_present"])

    def test_path_checks_reject_runnable_chatgpt_desktop_ui_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            bundle.mkdir()
            _write_client_configs(bundle, launcher=bundle / "run_mcp_stdio_server.ps1", data_dir=bundle / "data")
            desktop_path = bundle / "chatgpt_desktop_local_mcp.json"
            payload = json.loads(desktop_path.read_text(encoding="utf-8"))
            payload["ui_fields"] = {
                "name": "govreg-local",
                "command": "powershell.exe",
                "args": [],
            }
            desktop_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

            checks = _client_config_path_checks(target_dir=bundle, server_name="govreg-local")

        self.assertFalse(checks["passed"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["passed"])
        self.assertTrue(checks["clients"]["chatgpt_desktop_local"]["strict_utf8_without_bom"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["config_schema_verified"])
        self.assertIn("ui_fields", checks["clients"]["chatgpt_desktop_local"]["schema_error"])

    def test_path_checks_reject_supported_or_runnable_chatgpt_desktop_artifact(self) -> None:
        mutations = {
            "supported flag": {"direct_local_supported": True},
            "runnable config": {"mcpServers": {"govreg-local": {"command": "python", "args": []}}},
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / "bundle"
                bundle.mkdir()
                _write_client_configs(
                    bundle,
                    launcher=bundle / "run_mcp_stdio_server.ps1",
                    data_dir=bundle / "data",
                )
                desktop_path = bundle / "chatgpt_desktop_local_mcp.json"
                payload = json.loads(desktop_path.read_text(encoding="utf-8"))
                payload.update(mutation)
                desktop_path.write_text(
                    json.dumps(payload, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

                checks = _client_config_path_checks(
                    target_dir=bundle,
                    server_name="govreg-local",
                )

                legacy = checks["clients"]["chatgpt_desktop_local"]
                self.assertFalse(legacy["passed"])
                self.assertFalse(legacy["guard_passed"])
                self.assertFalse(legacy["connection_verified"])
                self.assertFalse(legacy["process_started"])
                self.assertEqual(
                    label == "runnable config",
                    legacy["runnable_config_present"],
                )

    def test_path_checks_reject_chatgpt_desktop_config_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            bundle.mkdir()
            _write_client_configs(bundle, launcher=bundle / "run_mcp_stdio_server.ps1", data_dir=bundle / "data")
            desktop_path = bundle / "chatgpt_desktop_local_mcp.json"
            desktop_path.write_bytes(b"\xef\xbb\xbf" + desktop_path.read_bytes())

            checks = _client_config_path_checks(target_dir=bundle, server_name="govreg-local")

        self.assertFalse(checks["passed"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["passed"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["strict_utf8_without_bom"])
        self.assertIn("EF BB BF", checks["clients"]["chatgpt_desktop_local"]["encoding_error"])

    def test_path_checks_reject_duplicate_chatgpt_desktop_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            bundle.mkdir()
            _write_client_configs(bundle, launcher=bundle / "run_mcp_stdio_server.ps1", data_dir=bundle / "data")
            desktop_path = bundle / "chatgpt_desktop_local_mcp.json"
            desktop_path.write_text(
                '{"support_status":"unsupported","support_status":"supported",'
                '"direct_local_supported":false}',
                encoding="utf-8",
            )

            checks = _client_config_path_checks(target_dir=bundle, server_name="govreg-local")

        self.assertFalse(checks["passed"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["passed"])
        self.assertTrue(checks["clients"]["chatgpt_desktop_local"]["strict_utf8_without_bom"])
        self.assertFalse(checks["clients"]["chatgpt_desktop_local"]["config_schema_verified"])
        self.assertIn(
            "duplicate JSON key: support_status",
            checks["clients"]["chatgpt_desktop_local"]["schema_error"],
        )

    def test_path_checks_reject_stale_generated_bundle_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = root / "extracted"
            stale = root / "stale"
            extracted.mkdir()
            stale.mkdir()
            _write_client_configs(extracted, launcher=stale / "run_mcp_stdio_server.ps1", data_dir=stale / "data")

            checks = _client_config_path_checks(target_dir=extracted, server_name="govreg-local")

        self.assertFalse(checks["passed"])
        self.assertFalse(checks["clients"]["codex"]["passed"])
        self.assertFalse(checks["clients"]["claude_desktop"]["passed"])
        self.assertIn("stale", checks["clients"]["codex"]["launcher"])

    def test_safe_extract_rejects_traversal_and_symlink_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "nested" / "extract"
            destination.mkdir(parents=True)
            traversal = root / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../outside.txt", "blocked")
            with self.assertRaisesRegex(ValueError, "Unsafe bundle archive member"):
                _extract_archive_safely(traversal, destination)

            symlink = root / "symlink.zip"
            info = zipfile.ZipInfo("linked.txt")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr(info, "outside.txt")
            with self.assertRaisesRegex(ValueError, "Symlink bundle archive member"):
                _extract_archive_safely(symlink, destination)

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("same.txt", "first")
                archive.writestr("same.txt", "second")
            with self.assertRaisesRegex(ValueError, "Duplicate bundle archive member"):
                _extract_archive_safely(duplicate, destination)

    def test_safe_extract_allows_only_exact_omission_sidecar_and_rejects_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "extract"
            destination.mkdir()
            valid = root / "valid.zip"
            with zipfile.ZipFile(valid, "w") as archive:
                archive.writestr(
                    "data/repository/omission_disposition_snapshot.json",
                    "{}\n",
                )
            _extract_archive_safely(valid, destination)
            self.assertTrue(
                (
                    destination
                    / "data"
                    / "repository"
                    / "omission_disposition_snapshot.json"
                ).is_file()
            )

            forbidden_names = (
                "data/repository/journals/review_decisions.jsonl",
                "data/raw/omission_disposition_snapshot.json",
                "data/repository/raw/source-review.txt",
            )
            for index, name in enumerate(forbidden_names):
                with self.subTest(name=name):
                    archive_path = root / f"forbidden-{index}.zip"
                    with zipfile.ZipFile(archive_path, "w") as archive:
                        archive.writestr(name, "blocked")
                    with self.assertRaisesRegex(ValueError, "sealed handoff allowlist"):
                        _extract_archive_safely(archive_path, root / f"out-{index}")

    def test_extract_smoke_validates_runtime_integrity_before_client_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            extracted = root / "extracted"
            _write_client_configs(
                source,
                launcher=extracted / "run_mcp_stdio_server.ps1",
                data_dir=extracted / "data",
            )
            (source / "validate_client_config_smoke.ps1").write_text("exit 0\n", encoding="utf-8")
            (source / "run_mcp_stdio_server.ps1").write_text("exit 0\n", encoding="utf-8")
            (source / "mcp_client_config_smoke.json").write_text('{"passed": true}\n', encoding="utf-8")
            runtime_dir = source / "data"
            runtime_dir.mkdir()
            (runtime_dir / "mcp_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
            bundle_zip = root / "bundle.zip"
            with zipfile.ZipFile(bundle_zip, "w") as archive:
                for path in source.rglob("*"):
                    if path.is_file():
                        archive.write(path, arcname=path.relative_to(source).as_posix())

            with (
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke.validate_mcp_runtime_data_bundle_integrity"
                ) as integrity,
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke._powershell_command",
                    return_value="powershell.exe",
                ),
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
                mock.patch(
                    "scripts.run_mcp_bundle_zip_extract_smoke.current_repo_commit",
                    return_value="test-commit",
                ),
            ):
                report = run_mcp_bundle_zip_extract_smoke(
                    bundle_zip=bundle_zip,
                    extract_dir=extracted,
                    server_name="govreg-local",
                )

        integrity.assert_called_once_with(extracted / "data")
        self.assertTrue(report["runtime_integrity_checked"])
        self.assertTrue(report["runtime_integrity_passed"])


def _write_client_configs(bundle: Path, *, launcher: Path, data_dir: Path) -> None:
    args = [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher),
        "--data-dir",
        str(data_dir),
        "--tenant-id",
        "default",
        "--transport",
        "stdio",
        "--flat-storage",
        "--no-warm-cache",
    ]
    codex_lines = [
        "[mcp_servers.govreg-local]",
        'command = "powershell.exe"',
        "args = [",
        *[f"  {json.dumps(arg)}," for arg in args],
        "]",
    ]
    (bundle / "codex_config_snippet.toml").write_text("\n".join(codex_lines) + "\n", encoding="utf-8")
    (bundle / "claude_desktop_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "govreg-local": {
                        "type": "stdio",
                        "command": "powershell.exe",
                        "args": args,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "chatgpt_desktop_local_mcp.json").write_text(
        json.dumps(
            {
                "profile": "chatgpt-desktop-local",
                "client": "ChatGPT",
                "surface": "legacy_compatibility_artifact",
                "support_status": "unsupported",
                "direct_local_supported": False,
                "chatgpt_direct_local_mcp_supported": False,
                "server_name": "govreg-local",
                "warning": "ChatGPT does not directly connect to a local MCP server.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
