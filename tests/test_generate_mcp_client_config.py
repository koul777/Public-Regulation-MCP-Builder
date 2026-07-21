from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import hashlib
import importlib.util
import json
import unittest
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.ingestion.vector_adapter import stable_content_hash
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.schemas.structure import StructureNode
from app.storage.repository import JsonRepository
from scripts.generate_mcp_client_config import (
    AGENT_CONNECT_BUNDLE_NAME_MARKER,
    AGENT_CONNECT_BUNDLE_DIR_MARKER,
    AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER,
    RUNTIME_IDENTITY_MODULES,
    _install_local_package_script,
    _powershell_runtime_identity_validator_lines,
    _recommended_runtime_smoke_query,
    _powershell_stdio_launcher_script,
    _runtime_identity_builder_base64,
    _runtime_identity_verifier_base64,
    build_mcp_client_config,
    main,
    parse_args,
    render_agent_connect_prompt_for_program,
    write_mcp_runtime_data_bundle,
    write_mcp_setup_bundle,
    write_mcp_setup_bundle_zip,
)
from scripts.mcp_bundle_contract import ALL_SETUP_BUNDLE_FILES, REQUIRED_SETUP_BUNDLE_FILES


def _assert_same_existing_path(
    test_case: unittest.TestCase,
    expected: str | Path,
    actual: str | Path,
) -> None:
    """Compare file identity so Windows 8.3 and long paths are equivalent."""
    expected_path = Path(expected)
    actual_path = Path(actual)
    test_case.assertTrue(expected_path.exists(), f"Expected path does not exist: {expected_path}")
    test_case.assertTrue(actual_path.exists(), f"Actual path does not exist: {actual_path}")
    test_case.assertTrue(
        os.path.samefile(expected_path, actual_path),
        f"Paths do not identify the same filesystem entry: {expected_path!s} != {actual_path!s}",
    )


def _test_runtime_marker_payload(python_executable: str | Path) -> dict[str, object]:
    module_sha256 = {
        module_name: "sha256:" + hashlib.sha256(module_name.encode("utf-8")).hexdigest()
        for module_name in RUNTIME_IDENTITY_MODULES
    }
    canonical = json.dumps(
        module_sha256,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 2,
        "python_executable": str(Path(python_executable).resolve()),
        "minimum_python": "3.11",
        "package_import": "scripts.run_regulation_mcp",
        "identity_scope": "mcp-command-modules-v1",
        "hash_algorithm": "sha256",
        "module_sha256": module_sha256,
        "build_identity_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "written_at": "2026-07-20T00:00:00+00:00",
    }


def _test_runtime_identity_json() -> str:
    marker = _test_runtime_marker_payload(sys.executable)
    return json.dumps(
        {
            "module_sha256": marker["module_sha256"],
            "build_identity_sha256": marker["build_identity_sha256"],
        },
        separators=(",", ":"),
    )


def _actual_runtime_marker_payload(python_executable: str | Path) -> dict[str, object]:
    module_sha256: dict[str, str] = {}
    for module_name in RUNTIME_IDENTITY_MODULES:
        spec = importlib.util.find_spec(module_name)
        if spec is None or not spec.origin:
            raise AssertionError(f"Missing test runtime module: {module_name}")
        module_sha256[module_name] = "sha256:" + hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
    canonical = json.dumps(
        module_sha256,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = _test_runtime_marker_payload(python_executable)
    payload["module_sha256"] = module_sha256
    payload["build_identity_sha256"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return payload


def _fake_client_config_smoke_source(message: str = "client-smoke-ok") -> str:
    return (
        "import json, sys\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--out-json') + 1])\n"
        "server_name = args[args.index('--server-name') + 1]\n"
        "targets = []\n"
        "for flag, label in (('--codex-config', 'codex'), ('--claude-desktop-config', 'claude_desktop'), ('--plugin-mcp-config', 'chatgpt_desktop_local')):\n"
        "    if flag in args: targets.append({'label': label, 'config_path': str(Path(args[args.index(flag) + 1])), 'passed': True, 'contract_verified': True})\n"
        "payload = {'report_type': 'mcp_client_config_smoke', 'generated_at': datetime.now(timezone.utc).isoformat(), 'server_name': server_name, 'passed': bool(targets), 'launcher_ready': bool(targets), 'process_started': bool(targets), 'mcp_initialized': bool(targets), 'tools_discovered': bool(targets), 'end_to_end_verified': bool(targets), 'results': targets}\n"
        "out.write_text(json.dumps(payload), encoding='utf-8')\n"
        f"print({message!r})\n"
    )


class GenerateMcpClientConfigTests(unittest.TestCase):
    def test_setup_bundle_embeds_separate_chatgpt_and_claude_fallback_configs(self) -> None:
        config = build_mcp_client_config(
            server_name="product-source-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        config["claude_desktop"]["mcpServers"]["product-source-mcp"]["env"] = {
            "PRODUCT_SOURCE": "claude-desktop"
        }
        config["chatgpt_desktop_local"]["mcpServers"]["product-source-mcp"]["env"] = {
            "PRODUCT_SOURCE": "chatgpt-desktop"
        }

        with tempfile.TemporaryDirectory() as tmp:
            files = write_mcp_setup_bundle(config, tmp, server_name="product-source-mcp")
            wizard = Path(files["connect"]).read_text(encoding="utf-8-sig")

        embedded: dict[str, dict[str, object]] = {}
        for variable_name in (
            "EmbeddedClaudeDesktopConfigBase64",
            "EmbeddedChatGptDesktopConfigBase64",
        ):
            match = re.search(rf'^\${variable_name} = "([A-Za-z0-9+/=]+)"$', wizard, re.MULTILINE)
            self.assertIsNotNone(match, variable_name)
            embedded[variable_name] = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))

        claude_server = embedded["EmbeddedClaudeDesktopConfigBase64"]["mcpServers"]["product-source-mcp"]
        chatgpt_server = embedded["EmbeddedChatGptDesktopConfigBase64"]["mcpServers"]["product-source-mcp"]
        self.assertEqual("claude-desktop", claude_server["env"]["PRODUCT_SOURCE"])
        self.assertEqual("chatgpt-desktop", chatgpt_server["env"]["PRODUCT_SOURCE"])

    def test_setup_bundle_replaces_stale_plugin_tree_when_server_name_changes(self) -> None:
        first_config = build_mcp_client_config(
            server_name="first-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        second_config = build_mcp_client_config(
            server_name="second-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "renamed-server-bundle"
            write_mcp_setup_bundle(first_config, bundle_dir, server_name="first-mcp")
            plugin_marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            self.assertTrue(
                (plugin_marketplace_root / "plugins" / "first-mcp" / ".codex-plugin" / "plugin.json").is_file()
            )

            write_mcp_setup_bundle(second_config, bundle_dir, server_name="second-mcp")
            zip_path = Path(tmp) / "renamed-server-bundle.zip"
            write_mcp_setup_bundle_zip(bundle_dir, zip_path)

            plugin_directories = {
                path.name
                for path in (plugin_marketplace_root / "plugins").iterdir()
                if path.is_dir()
            }
            marketplace = json.loads(
                (plugin_marketplace_root / ".agents" / "plugins" / "marketplace.json").read_text(
                    encoding="utf-8"
                )
            )
            second_mcp = json.loads(
                (plugin_marketplace_root / "plugins" / "second-mcp" / ".mcp.json").read_text(
                    encoding="utf-8"
                )
            )
            zip_created = zip_path.is_file()

        self.assertEqual({"second-mcp"}, plugin_directories)
        self.assertEqual(["second-mcp"], [plugin["name"] for plugin in marketplace["plugins"]])
        self.assertEqual({"second-mcp"}, set(second_mcp["mcpServers"]))
        self.assertTrue(zip_created)

    def test_setup_bundle_restores_prior_plugin_tree_when_replacement_fails(self) -> None:
        config = build_mcp_client_config(
            server_name="first-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "rollback-plugin-bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="first-mcp")
            plugin_marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            prior_files = {
                path.relative_to(plugin_marketplace_root).as_posix(): path.read_bytes()
                for path in plugin_marketplace_root.rglob("*")
                if path.is_file()
            }

            def fail_after_partial_plugin_write(*args: object, **kwargs: object) -> dict[str, str]:
                del args, kwargs
                partial_plugin = (
                    plugin_marketplace_root
                    / "plugins"
                    / "second-mcp"
                    / ".codex-plugin"
                    / "plugin.json"
                )
                partial_plugin.parent.mkdir(parents=True, exist_ok=True)
                partial_plugin.write_text('{"name":"second-mcp"}\n', encoding="utf-8")
                raise RuntimeError("forced setup replacement failure")

            with patch(
                "scripts.generate_mcp_client_config._write_mcp_setup_bundle_untransactional",
                side_effect=fail_after_partial_plugin_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced setup replacement failure"):
                    write_mcp_setup_bundle(config, bundle_dir, server_name="first-mcp")

            restored_files = {
                path.relative_to(plugin_marketplace_root).as_posix(): path.read_bytes()
                for path in plugin_marketplace_root.rglob("*")
                if path.is_file()
            }

        self.assertEqual(prior_files, restored_files)
        self.assertNotIn(
            "plugins/second-mcp/.codex-plugin/plugin.json",
            restored_files,
        )

    def test_setup_bundle_preserves_backup_and_restores_other_files_when_plugin_rollback_is_locked(self) -> None:
        config = build_mcp_client_config(
            server_name="first-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "locked-rollback-bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="first-mcp")
            readme_path = bundle_dir / "README.md"
            prior_readme = readme_path.read_bytes()
            plugin_marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            partial_marker = (
                plugin_marketplace_root
                / "plugins"
                / "second-mcp"
                / ".codex-plugin"
                / "plugin.json"
            )

            def fail_after_overwriting_readme(*args: object, **kwargs: object) -> dict[str, str]:
                del args, kwargs
                readme_path.write_text("partial replacement\n", encoding="utf-8")
                partial_marker.parent.mkdir(parents=True, exist_ok=True)
                partial_marker.write_text('{"name":"second-mcp"}\n', encoding="utf-8")
                raise RuntimeError("forced setup replacement failure")

            original_rmtree = shutil.rmtree

            def fail_locked_plugin_cleanup(path: object, *args: object, **kwargs: object) -> None:
                target = Path(path)
                if target == plugin_marketplace_root and partial_marker.exists():
                    raise PermissionError("forced locked plugin file")
                original_rmtree(path, *args, **kwargs)

            with (
                patch(
                    "scripts.generate_mcp_client_config._write_mcp_setup_bundle_untransactional",
                    side_effect=fail_after_overwriting_readme,
                ),
                patch(
                    "scripts.generate_mcp_client_config.shutil.rmtree",
                    side_effect=fail_locked_plugin_cleanup,
                ),
                self.assertRaisesRegex(RuntimeError, "rollback was incomplete") as raised,
            ):
                write_mcp_setup_bundle(config, bundle_dir, server_name="first-mcp")

            backup_dirs = list(Path(tmp).glob(".locked-rollback-bundle.setup-backup-*"))
            backup_file_bytes = {
                path.read_bytes()
                for backup_dir in backup_dirs
                for path in backup_dir.rglob("*")
                if path.is_file()
            }

            self.assertEqual(prior_readme, readme_path.read_bytes())
            self.assertEqual(1, len(backup_dirs))
            self.assertIn(prior_readme, backup_file_bytes)
            self.assertIn(str(backup_dirs[0]), str(raised.exception))
            self.assertIn("PermissionError", str(raised.exception))

    def test_program_copy_prompt_materializes_exact_bundle_path_without_changing_portable_source(self) -> None:
        portable_prompt = (
            f"Bundle name: {AGENT_CONNECT_BUNDLE_NAME_MARKER}\n"
            f"Bundle: {AGENT_CONNECT_BUNDLE_DIR_MARKER}\n"
            f"$BundleDir = {AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER}\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "MCP's 한글 번들"
            bundle_dir.mkdir()
            rendered = render_agent_connect_prompt_for_program(
                portable_prompt,
                bundle_dir=bundle_dir,
            )

        resolved = str(bundle_dir.resolve())
        self.assertIn(f"Bundle name: {bundle_dir.name}", rendered)
        self.assertIn(f"Bundle: {resolved}", rendered)
        self.assertIn("$BundleDir = '" + resolved.replace("'", "''") + "'", rendered)
        self.assertNotIn(AGENT_CONNECT_BUNDLE_DIR_MARKER, rendered)
        self.assertNotIn(AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER, rendered)
        self.assertNotIn(AGENT_CONNECT_BUNDLE_NAME_MARKER, rendered)
        self.assertIn(AGENT_CONNECT_BUNDLE_DIR_MARKER, portable_prompt)

    def test_program_copy_prompt_upgrades_existing_workspace_search_prompt(self) -> None:
        legacy_prompt = """# ChatGPT Desktop 에이전트 MCP 연결 요청

1. 현재 작업공간에서 `CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md`를 정확히 하나 찾아 그 부모 폴더를 `$BundleDir`로 삼고 `Set-Location -LiteralPath $BundleDir`을 실행해. 0개 또는 여러 개면 임의 경로를 선택하지 말고 중단해.
2. `manifest.json`을 확인해.
"""

        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "기존 번들"
            bundle_dir.mkdir()
            rendered = render_agent_connect_prompt_for_program(
                legacy_prompt,
                bundle_dir=bundle_dir,
            )

        self.assertIn("생성 프로그램이 지정한 현재 번들 폴더", rendered)
        self.assertIn(f"생성 프로그램이 지정한 번들 폴더 이름: `{bundle_dir.name}`", rendered)
        self.assertIn("├─ manifest.json", rendered)
        self.assertIn("├─ data\\", rendered)
        self.assertIn("reg_rag_preprocessor-*.whl  (독립 배포용 wheel을 포함한 경우)", rendered)
        self.assertIn("runtime_python.json", rendered)
        self.assertIn("$BundleDir = '" + str(bundle_dir.resolve()) + "'", rendered)
        self.assertIn("CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md", rendered)
        self.assertIn("그 정확한 폴더를 작업공간으로 열거나 추가", rendered)
        self.assertNotIn("현재 작업공간에서 `CHATGPT_DESKTOP", rendered)
        self.assertIn("2. `manifest.json`을 확인해.", rendered)

    def test_runtime_identity_payloads_are_encoded_for_powershell_native_argv(self) -> None:
        builder_source = base64.b64decode(_runtime_identity_builder_base64()).decode("utf-8")
        verifier_source = base64.b64decode(_runtime_identity_verifier_base64()).decode("utf-8")
        install_script = _install_local_package_script()
        validator_script = "\n".join(_powershell_runtime_identity_validator_lines())

        self.assertIn("base64.b64decode(sys.argv[1])", builder_source)
        self.assertIn("base64.b64decode(sys.argv[1])", verifier_source)
        self.assertIn("base64.b64decode(sys.argv[2])", verifier_source)
        self.assertIn("[System.Convert]::ToBase64String", install_script)
        self.assertGreaterEqual(validator_script.count("[System.Convert]::ToBase64String"), 2)
        self.assertNotIn("$IdentityBuilderBase64 ($RuntimeModules | ConvertTo-Json", install_script)
        self.assertNotIn("$IdentityVerifierBase64 $RuntimeModulesJson $ExpectedHashesJson", validator_script)

    def test_runtime_identity_helpers_execute_with_generated_python_argv_layout(self) -> None:
        install_script = _install_local_package_script()
        validator_script = "\n".join(_powershell_runtime_identity_validator_lines())
        builder_match = re.search(
            r'& \$ResolvedPython -c "([^"]+)" \$IdentityBuilderBase64',
            install_script,
        )
        verifier_match = re.search(
            r'\$VerifierStartInfo\.Arguments = "-c ([^"]+) \$IdentityVerifierBase64',
            validator_script,
        )
        self.assertIsNotNone(builder_match)
        self.assertIsNotNone(verifier_match)
        assert builder_match is not None and verifier_match is not None

        modules_json = json.dumps(
            list(RUNTIME_IDENTITY_MODULES),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        modules_base64 = base64.b64encode(modules_json).decode("ascii")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        built = subprocess.run(
            [
                sys.executable,
                "-c",
                builder_match.group(1),
                _runtime_identity_builder_base64(),
                modules_base64,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        identity = json.loads(built.stdout)
        expected_hashes_base64 = base64.b64encode(
            json.dumps(
                identity["module_sha256"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        verified = subprocess.run(
            [
                sys.executable,
                "-c",
                verifier_match.group(1),
                _runtime_identity_verifier_base64(),
                modules_base64,
                expected_hashes_base64,
                identity["build_identity_sha256"],
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(0, verified.returncode, verified.stdout + verified.stderr)

    @unittest.skipUnless(os.name == "nt", "Generated standalone validation scripts are Windows-specific.")
    def test_standalone_validators_reject_exit_zero_without_fresh_report(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        cases = (
            (
                "doctor_mcp_connection.ps1",
                "scripts.check_mcp_connection_readiness",
                "mcp_connection_readiness.json",
                {
                    "report_type": "mcp_connection_readiness",
                    "passed": True,
                    "findings": [],
                },
                "MCP doctor did not produce a fresh passing readiness report.",
            ),
            (
                "validate_mcp_smoke.ps1",
                "scripts.run_mcp_transport_smoke",
                "mcp_transport_smoke.json",
                {
                    "report_type": "mcp_transport_smoke",
                    "passed": True,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "end_to_end_verified": True,
                    "full_profile": {
                        "passed": True,
                        "search_result_count": 1,
                        "fetch_has_text": True,
                    },
                    "chatgpt_data_profile": {
                        "passed": True,
                        "search_result_count": 1,
                        "fetch_has_text": True,
                    },
                },
                "Runtime MCP smoke did not produce a fresh passing search/fetch report.",
            ),
            (
                "validate_client_config_smoke.ps1",
                "scripts.run_mcp_client_config_smoke",
                "mcp_client_config_smoke.json",
                {
                    "report_type": "mcp_client_config_smoke",
                    "passed": True,
                    "launcher_ready": True,
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "end_to_end_verified": True,
                    "results": [{}, {}, {}],
                },
                "Client config smoke did not produce a fresh passing three-client report.",
            ),
        )

        for script_name, module_name, report_name, stale_payload, expected_error in cases:
            with self.subTest(script_name=script_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle_dir = root / "bundle with spaces"
                fake_python_root = root / "fake modules"
                fake_scripts = fake_python_root / "scripts"
                fake_scripts.mkdir(parents=True)
                (fake_scripts / "__init__.py").write_text("", encoding="utf-8")
                module_path = fake_python_root / (module_name.replace(".", "/") + ".py")
                module_path.write_text("raise SystemExit(0)\n", encoding="utf-8")

                config = build_mcp_client_config(
                    server_name="fresh-report-mcp",
                    client_profile="bundle",
                    tenant_id="tenant-a",
                )
                write_mcp_setup_bundle(
                    config,
                    bundle_dir,
                    server_name="fresh-report-mcp",
                    preferred_python=sys.executable,
                    preferred_project_root=fake_python_root,
                )
                (bundle_dir / "data").mkdir(exist_ok=True)
                report_path = bundle_dir / report_name
                report_path.write_text(json.dumps(stale_payload), encoding="utf-8")

                env = os.environ.copy()
                env["PYTHONPATH"] = str(fake_python_root)
                env.pop("REG_RAG_PYTHON", None)
                completed = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(bundle_dir / script_name),
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )

                self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertFalse(report_path.exists(), "A stale positive report survived the validator run.")
                self.assertIn(expected_error, completed.stdout + completed.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell installer behavior is Windows-specific.")
    def test_install_script_discovers_active_python_scripts_directory_when_missing_from_path(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_dir = tmp_path / "bundle user's copy"
            write_mcp_setup_bundle(config, bundle_dir, server_name="govreg-local")

            fake_bin = tmp_path / "fake-bin"
            fake_scripts = tmp_path / "Fake Python" / "Scripts"
            fake_bin.mkdir()
            fake_scripts.mkdir(parents=True)
            fake_python = fake_bin / "python.cmd"
            fake_python.write_text(
                "@echo off\n"
                "if \"%1\"==\"-m\" goto pip_ok\n"
                "if \"%1\"==\"-c\" if not \"%~4\"==\"\" goto identity\n"
                "if \"%1\"==\"-c\" goto scripts_dir\n"
                "exit /b 1\n"
                ":pip_ok\n"
                "exit /b 0\n"
                ":scripts_dir\n"
                f"echo {base64.b64encode(str(fake_scripts).encode('utf-8')).decode('ascii')}\n"
                "exit /b 0\n"
                ":identity\n"
                f"echo {_test_runtime_identity_json()}\n"
                "exit /b 0\n",
                encoding="utf-8",
            )
            for command in (
                "reg-rag-mcp-server",
                "reg-rag-mcp-config",
                "reg-rag-mcp-doctor",
                "reg-rag-mcp-smoke",
                "reg-rag-mcp-codex-app-server-check",
                "reg-rag-mcp-desktop-recognition-check",
                "reg-rag-mcp-client-config-smoke",
                "reg-rag-mcp-index-visibility",
            ):
                (fake_scripts / f"{command}.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            package_path = tmp_path / "dummy package.whl"
            package_path.write_bytes(b"test-only")

            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env.pop("REG_RAG_PYTHON", None)
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "install_local_package.ps1"),
                    "-PackagePath",
                    str(package_path),
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("installed and visible on PATH", completed.stdout)
            marker = json.loads((bundle_dir / "runtime_python.json").read_text(encoding="utf-8"))
            self.assertEqual(2, marker["schema_version"])
            _assert_same_existing_path(self, fake_python, marker["python_executable"])
            self.assertEqual("scripts.run_regulation_mcp", marker["package_import"])
            self.assertEqual(set(RUNTIME_IDENTITY_MODULES), set(marker["module_sha256"]))

    @unittest.skipUnless(os.name == "nt", "PowerShell installer behavior is Windows-specific.")
    def test_install_script_prefers_bundled_wheel_over_ancestor_editable_checkout(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="wheel-first-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "developer checkout"
            bundle_dir = project_root / "reports" / "generated bundle"
            bundle_dir.mkdir(parents=True)
            (project_root / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            write_mcp_setup_bundle(config, bundle_dir, server_name="wheel-first-mcp")
            bundled_wheel = bundle_dir / "reg_rag_preprocessor-1.2.12-py3-none-any.whl"
            bundled_wheel.write_bytes(b"test-only-wheel")

            fake_bin = root / "selected runtime"
            fake_scripts = fake_bin / "Scripts"
            fake_scripts.mkdir(parents=True)
            pip_log = root / "pip-invocation.txt"
            fake_python = fake_bin / "python.cmd"
            fake_python.write_text(
                "@echo off\n"
                "if \"%1\"==\"-m\" echo %*>>\"%FAKE_PIP_LOG%\"& exit /b 0\n"
                "if \"%1\"==\"-c\" if not \"%~4\"==\"\" goto identity\n"
                f"if \"%1\"==\"-c\" echo {base64.b64encode(str(fake_scripts).encode('utf-8')).decode('ascii')}& exit /b 0\n"
                "exit /b 1\n"
                ":identity\n"
                f"echo {_test_runtime_identity_json()}\n"
                "exit /b 0\n",
                encoding="utf-8",
            )
            for command in (
                "reg-rag-mcp-server",
                "reg-rag-mcp-config",
                "reg-rag-mcp-doctor",
                "reg-rag-mcp-smoke",
                "reg-rag-mcp-codex-app-server-check",
                "reg-rag-mcp-desktop-recognition-check",
                "reg-rag-mcp-client-config-smoke",
                "reg-rag-mcp-index-visibility",
            ):
                (fake_scripts / f"{command}.cmd").write_text("@exit /b 0\n", encoding="utf-8")

            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(fake_scripts), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["REG_RAG_PYTHON"] = str(fake_python)
            env["FAKE_PIP_LOG"] = str(pip_log)
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "install_local_package.ps1"),
                ],
                cwd=bundle_dir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            pip_invocation = pip_log.read_text(encoding="utf-8")
            invoked_wheels = re.findall(r'"([^"\r\n]+\.whl)"', pip_invocation)
            self.assertTrue(invoked_wheels, pip_invocation)
            for invoked_wheel in invoked_wheels:
                _assert_same_existing_path(self, bundled_wheel, invoked_wheel)
            self.assertNotIn(" pip install -e ", pip_invocation)
            self.assertIn(
                "pip install --force-reinstall --no-deps",
                pip_invocation,
            )

    @unittest.skipUnless(os.name == "nt", "PowerShell installer behavior is Windows-specific.")
    def test_install_script_rejects_ambiguous_bundled_wheels_before_pip(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="ambiguous-wheel-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="ambiguous-wheel-mcp")
            for version in ("1.2.12", "1.2.13"):
                bundle_dir.joinpath(
                    f"reg_rag_preprocessor-{version}-py3-none-any.whl"
                ).write_bytes(b"test-only-wheel")

            fake_runtime = root / "runtime"
            fake_scripts = fake_runtime / "Scripts"
            fake_scripts.mkdir(parents=True)
            pip_log = root / "pip-was-called.txt"
            fake_python = fake_runtime / "python.cmd"
            encoded_scripts = base64.b64encode(str(fake_scripts).encode("utf-8")).decode("ascii")
            fake_python.write_text(
                "@echo off\n"
                'if "%1"=="-m" echo %*>>"%FAKE_PIP_LOG%"& exit /b 0\n'
                f'if "%1"=="-c" echo {encoded_scripts}& exit /b 0\n'
                "exit /b 1\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_runtime), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["REG_RAG_PYTHON"] = str(fake_python)
            env["FAKE_PIP_LOG"] = str(pip_log)

            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "install_local_package.ps1"),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("Multiple bundled reg_rag_preprocessor wheels", completed.stdout + completed.stderr)
            self.assertFalse(pip_log.exists())
            self.assertFalse((bundle_dir / "runtime_python.json").exists())

    @unittest.skipUnless(os.name == "nt", "Secure tunnel PowerShell behavior is Windows-specific.")
    def test_secure_tunnel_expands_unicode_bundle_data_dir_as_one_command_argument(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="tunnel-path-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "한글 경로 bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="tunnel-path-mcp")
            data_dir = bundle_dir / "data"
            data_dir.mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            tunnel_log = root / "tunnel-command.txt"
            server_args_log = root / "server-args.txt"
            (fake_bin / "reg-rag-mcp-doctor.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            (fake_bin / "reg-rag-mcp-server.cmd").write_text(
                "@echo off\n"
                "chcp 65001 >nul\n"
                '>"%MCP_SERVER_ARGS_LOG%" echo ARG1=%~1\n'
                '>>"%MCP_SERVER_ARGS_LOG%" echo ARG2=%~2\n'
                '>>"%MCP_SERVER_ARGS_LOG%" echo ARG3=%~3\n'
                '>>"%MCP_SERVER_ARGS_LOG%" echo ARG4=%~4\n'
                '>>"%MCP_SERVER_ARGS_LOG%" echo ARG7=%~7\n'
                '>>"%MCP_SERVER_ARGS_LOG%" echo ARG8=%~8\n'
                '>>"%MCP_SERVER_ARGS_LOG%" echo ARG9=%~9\n'
                "exit /b 0\n",
                encoding="utf-8",
            )
            (fake_bin / "tunnel-client.cmd").write_text(
                "@echo off\n"
                "chcp 65001 >nul\n"
                'if /I "%1"=="init" (\n'
                '  >"%TUNNEL_COMMAND_LOG%" echo %~9\n'
                '  >>"%TUNNEL_COMMAND_LOG%" echo DATA=%PRMCPBUILDER_TUNNEL_DATA_DIR%\n'
                '  call %~9\n'
                '  if errorlevel 1 exit /b %ERRORLEVEL%\n'
                ')\n'
                "exit /b 0\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CONTROL_PLANE_API_KEY"] = "test-control-plane-key"
            env["OPENAI_TUNNEL_ID"] = "test-tunnel-id"
            env["TUNNEL_COMMAND_LOG"] = str(tunnel_log)
            env["MCP_SERVER_ARGS_LOG"] = str(server_args_log)

            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "run_openai_secure_tunnel.ps1"),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            command_value, inherited_data_dir = tunnel_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("powershell.exe", command_value)
            self.assertIn("-EncodedCommand", command_value)
            self.assertNotIn("$BundleDataDir", command_value)
            encoded_launcher = command_value.split("-EncodedCommand ", 1)[1]
            launcher_source = base64.b64decode(encoded_launcher).decode("utf-16-le")
            self.assertIn("$env:PRMCPBUILDER_TUNNEL_DATA_DIR", launcher_source)
            self.assertIn("'--tool-profile', 'chatgpt-data'", launcher_source)
            self.assertTrue(inherited_data_dir.startswith("DATA="), inherited_data_dir)
            _assert_same_existing_path(self, data_dir, inherited_data_dir.removeprefix("DATA="))
            server_args = server_args_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual("ARG1=--data-dir", server_args[0])
            self.assertTrue(server_args[1].startswith("ARG2="), server_args[1])
            _assert_same_existing_path(self, data_dir, server_args[1].removeprefix("ARG2="))
            self.assertEqual("ARG3=--tenant-id", server_args[2])
            self.assertEqual("ARG4=tenant-a", server_args[3])
            self.assertEqual("ARG7=--flat-storage", server_args[4])
            self.assertEqual("ARG8=--tool-profile", server_args[5])
            self.assertEqual("ARG9=chatgpt-data", server_args[6])

    @unittest.skipUnless(os.name == "nt", "PowerShell connection flow locking is Windows-specific.")
    def test_two_local_connection_flows_serialize_install_through_registration_boundary(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="serialized-flow-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="serialized-flow-mcp",
            )
            flow_log = root / "flow-order.txt"
            (bundle_dir / "install_local_package.ps1").write_text(
                "param([switch]$ConnectionFlowLockHeld)\n"
                '$ErrorActionPreference = "Stop"\n'
                'Add-Content -LiteralPath $env:MCP_FLOW_LOG -Value ("start:" + $PID)\n'
                "Start-Sleep -Milliseconds 700\n"
                'Add-Content -LiteralPath $env:MCP_FLOW_LOG -Value ("end:" + $PID)\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["MCP_FLOW_LOG"] = str(flow_log)

            def run_flow() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        files["connect"],
                        "-Target",
                        "install",
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                completed_runs = list(executor.map(lambda _: run_flow(), range(2)))

            for completed in completed_runs:
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            events = flow_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(4, len(events))
            self.assertTrue(events[0].startswith("start:"), events)
            self.assertTrue(events[1].startswith("end:"), events)
            self.assertTrue(events[2].startswith("start:"), events)
            self.assertTrue(events[3].startswith("end:"), events)
            self.assertEqual(events[0].split(":", 1)[1], events[1].split(":", 1)[1])
            self.assertEqual(events[2].split(":", 1)[1], events[3].split(":", 1)[1])
            self.assertNotEqual(events[0].split(":", 1)[1], events[2].split(":", 1)[1])

    @unittest.skipUnless(os.name == "nt", "PowerShell native-output decoding is Windows-specific.")
    def test_install_script_preserves_korean_python_scripts_path_across_native_stdout(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="unicode-runtime-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "한글 번들 경로"
            write_mcp_setup_bundle(config, bundle_dir, server_name="unicode-runtime-mcp")

            runtime_dir = root / "선택한 파이썬 환경"
            scripts_dir = runtime_dir / "Scripts"
            scripts_dir.mkdir(parents=True)
            driver = root / "fake_python_driver.py"
            driver.write_text(
                "\n".join(
                    [
                        "import base64, os, sys",
                        "args = sys.argv[1:]",
                        "if args and args[0] == '-m': raise SystemExit(0)",
                        "if args and args[0] == '-c':",
                        "    if len(args) >= 4: print(os.environ['FAKE_IDENTITY_JSON'])",
                        "    elif 'base64.b64encode' in args[1]: print(base64.b64encode(os.environ['FAKE_SCRIPTS_DIR'].encode('utf-8')).decode('ascii'))",
                        "    elif 'sysconfig.get_path' in args[1]: print(os.environ['FAKE_SCRIPTS_DIR'])",
                        "    raise SystemExit(0)",
                        "raise SystemExit(1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_python = runtime_dir / "python.cmd"
            fake_python.write_text(
                f'@"{sys.executable}" "{driver}" %*\n@exit /b %ERRORLEVEL%\n',
                encoding="utf-8",
            )
            for command in (
                "reg-rag-mcp-server",
                "reg-rag-mcp-config",
                "reg-rag-mcp-doctor",
                "reg-rag-mcp-smoke",
                "reg-rag-mcp-codex-app-server-check",
                "reg-rag-mcp-desktop-recognition-check",
                "reg-rag-mcp-client-config-smoke",
                "reg-rag-mcp-index-visibility",
            ):
                (scripts_dir / f"{command}.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            package_path = root / "dummy package.whl"
            package_path.write_bytes(b"test-only")

            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(runtime_dir), str(scripts_dir), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["REG_RAG_PYTHON"] = str(fake_python)
            env["FAKE_SCRIPTS_DIR"] = str(scripts_dir)
            env["FAKE_IDENTITY_JSON"] = _test_runtime_identity_json()
            env["PYTHONIOENCODING"] = "cp949"
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "install_local_package.ps1"),
                    "-PackagePath",
                    str(package_path),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            marker = json.loads((bundle_dir / "runtime_python.json").read_text(encoding="utf-8"))
            _assert_same_existing_path(self, fake_python, marker["python_executable"])
            self.assertEqual(set(RUNTIME_IDENTITY_MODULES), set(marker["module_sha256"]))

    @unittest.skipUnless(os.name == "nt", "PowerShell native-output decoding is Windows-specific.")
    def test_generated_codex_and_claude_cli_captures_preserve_utf8_and_restore_ps51_encoding(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="unicode-cli-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        wizard = config["quickstart"]["copy_paste"]["connect_wizard_ps"]
        codex_start = wizard.index("function Invoke-CodexCli")
        codex_end = wizard.index("\nfunction Test-CodexCliExecutable", codex_start)
        codex_function = wizard[codex_start:codex_end]
        claude_script = config["quickstart"]["copy_paste"]["claude_code_stdio_ps"]
        claude_start = claude_script.index("function Invoke-ClaudeMcpCli")
        claude_end = claude_script.index("\nfunction Get-ClaudeUserConfigPath", claude_start)
        claude_function = claude_script[claude_start:claude_end]

        # The direct loader and the nested plugin adapter must share the tested
        # UTF-8-aware Codex wrapper instead of introducing raw native captures.
        self.assertIn('$LoaderResult = Invoke-CodexCli @("mcp", "get", $ServerName, "--json")', wizard)
        self.assertIn("return Invoke-CodexCli -Arguments $Arguments", wizard)
        self.assertNotIn("$LoaderOutput = @(& codex", wizard)
        self.assertEqual(1, wizard.count("$CommandOutput = @(& codex @Arguments 2>&1)"))
        self.assertEqual(1, claude_script.count("$CommandOutput = @(& claude @Arguments 2>&1)"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected_path = str(root / "한글 경로" / "MCP 번들" / "data")
            native_payload = base64.b64encode(
                (json.dumps({"path": expected_path}, ensure_ascii=False) + "\n").encode("utf-8")
            ).decode("ascii")
            env = os.environ.copy()
            env["TEST_NATIVE_PYTHON"] = sys.executable
            env["TEST_NATIVE_JSON_BASE64"] = native_payload
            env["TEST_EXPECTED_UNICODE_PATH"] = expected_path

            probe_template = r'''
function __COMMAND_NAME__ {
  & $env:TEST_NATIVE_PYTHON -c 'import base64,sys;sys.stdout.buffer.write(base64.b64decode(sys.argv[1]))' $env:TEST_NATIVE_JSON_BASE64
}
[Console]::OutputEncoding = [System.Text.Encoding]::GetEncoding(949)
$OutputEncoding = [System.Text.Encoding]::ASCII
$BeforeConsole = [Console]::OutputEncoding.CodePage
$BeforePowerShell = $OutputEncoding.CodePage
$Capture = __INVOKE_NAME__ @("probe", "--json")
$Parsed = (($Capture.Output | Out-String) | ConvertFrom-Json -ErrorAction Stop)
[pscustomobject]@{
  exit_code = $Capture.ExitCode
  exact_path = ([string]$Parsed.path -ceq $env:TEST_EXPECTED_UNICODE_PATH)
  before_console = $BeforeConsole
  after_console = [Console]::OutputEncoding.CodePage
  before_powershell = $BeforePowerShell
  after_powershell = $OutputEncoding.CodePage
} | ConvertTo-Json -Compress
'''
            cases = (
                ("codex", "Invoke-CodexCli", codex_function),
                ("claude", "Invoke-ClaudeMcpCli", claude_function),
            )
            for command_name, invoke_name, function_source in cases:
                with self.subTest(command_name=command_name):
                    probe = (
                        probe_template.replace("__COMMAND_NAME__", command_name)
                        .replace("__INVOKE_NAME__", invoke_name)
                    )
                    script_path = root / f"{command_name}-utf8-capture.ps1"
                    script_path.write_text(function_source + "\n" + probe, encoding="utf-8-sig")
                    completed = subprocess.run(
                        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )

                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    result = json.loads(completed.stdout)
                    self.assertEqual(0, result["exit_code"])
                    self.assertTrue(result["exact_path"], completed.stdout + completed.stderr)
                    self.assertEqual(949, result["before_console"])
                    self.assertEqual(949, result["after_console"])
                    self.assertEqual(20127, result["before_powershell"])
                    self.assertEqual(20127, result["after_powershell"])

    @unittest.skipUnless(os.name == "nt", "Windows console-script precedence is Windows-specific.")
    def test_install_script_moves_selected_python_scripts_ahead_of_stale_path_commands(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="runtime-precedence-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="runtime-precedence-mcp")
            selected_bin = root / "selected-bin"
            selected_scripts = root / "selected-runtime" / "Scripts"
            stale_scripts = root / "stale-runtime" / "Scripts"
            selected_bin.mkdir()
            selected_scripts.mkdir(parents=True)
            stale_scripts.mkdir(parents=True)
            selected_python = selected_bin / "python.cmd"
            selected_python.write_text(
                "@echo off\n"
                "if \"%1\"==\"-m\" exit /b 0\n"
                "if \"%1\"==\"-c\" if not \"%~4\"==\"\" goto identity\n"
                f"if \"%1\"==\"-c\" echo {base64.b64encode(str(selected_scripts).encode('utf-8')).decode('ascii')}& exit /b 0\n"
                "exit /b 1\n"
                ":identity\n"
                f"echo {_test_runtime_identity_json()}\n"
                "exit /b 0\n",
                encoding="utf-8",
            )
            command_names = (
                "reg-rag-mcp-server",
                "reg-rag-mcp-config",
                "reg-rag-mcp-doctor",
                "reg-rag-mcp-smoke",
                "reg-rag-mcp-codex-app-server-check",
                "reg-rag-mcp-desktop-recognition-check",
                "reg-rag-mcp-client-config-smoke",
                "reg-rag-mcp-index-visibility",
            )
            for command_name in command_names:
                (selected_scripts / f"{command_name}.cmd").write_text("@exit /b 0\n", encoding="utf-8")
                (stale_scripts / f"{command_name}.cmd").write_text("@exit /b 23\n", encoding="utf-8")
            package_path = root / "dummy.whl"
            package_path.write_bytes(b"test-only")
            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(stale_scripts), str(selected_bin), str(selected_scripts), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env.pop("REG_RAG_PYTHON", None)
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "install_local_package.ps1"),
                    "-PackagePath",
                    str(package_path),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            marker = json.loads((bundle_dir / "runtime_python.json").read_text(encoding="utf-8"))
            _assert_same_existing_path(self, selected_python, marker["python_executable"])

    @unittest.skipUnless(os.name == "nt", "Windows py launcher behavior is Windows-specific.")
    def test_install_script_uses_py_311_when_python_command_is_absent(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_dir = tmp_path / "py launcher bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="govreg-local")

            fake_bin = tmp_path / "py-only-bin"
            fake_runtime_dir = tmp_path / "selected Python 311"
            fake_scripts = fake_runtime_dir / "Scripts"
            fake_bin.mkdir()
            fake_scripts.mkdir(parents=True)
            fake_python = fake_runtime_dir / "python.cmd"
            fake_python.write_text(
                "@echo off\n"
                "if \"%1\"==\"-m\" exit /b 0\n"
                "if \"%1\"==\"-c\" if not \"%~4\"==\"\" goto identity\n"
                f"if \"%1\"==\"-c\" echo {base64.b64encode(str(fake_scripts).encode('utf-8')).decode('ascii')}& exit /b 0\n"
                "exit /b 1\n"
                ":identity\n"
                f"echo {_test_runtime_identity_json()}\n"
                "exit /b 0\n",
                encoding="utf-8",
            )
            (fake_bin / "py.cmd").write_text(
                "@echo off\n"
                "if not \"%1\"==\"-3.11\" exit /b 1\n"
                f"echo {base64.b64encode(str(fake_python.resolve()).encode('utf-8')).decode('ascii')}\n"
                "exit /b 0\n",
                encoding="utf-8",
            )
            for command in (
                "reg-rag-mcp-server",
                "reg-rag-mcp-config",
                "reg-rag-mcp-doctor",
                "reg-rag-mcp-smoke",
                "reg-rag-mcp-codex-app-server-check",
                "reg-rag-mcp-desktop-recognition-check",
                "reg-rag-mcp-client-config-smoke",
                "reg-rag-mcp-index-visibility",
            ):
                (fake_scripts / f"{command}.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            package_path = tmp_path / "dummy py package.whl"
            package_path.write_bytes(b"test-only")

            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env.pop("REG_RAG_PYTHON", None)
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "install_local_package.ps1"),
                    "-PackagePath",
                    str(package_path),
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            marker = json.loads((bundle_dir / "runtime_python.json").read_text(encoding="utf-8"))
            _assert_same_existing_path(self, fake_python, marker["python_executable"])

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher behavior is Windows-specific.")
    def test_stdio_launcher_uses_path_python_when_console_scripts_are_missing(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_dir = tmp_path / "restarted desktop bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="govreg-local")
            (bundle_dir / "data").mkdir()

            fake_bin = tmp_path / "fake-bin"
            fake_bin.mkdir()
            invocation_log = tmp_path / "python-invocation.txt"
            (fake_bin / "python.cmd").write_text(
                "@echo off\n"
                "if \"%1\"==\"-c\" exit /b 0\n"
                "if \"%1\"==\"-m\" echo %*>\"%FAKE_PYTHON_LOG%\"& exit /b 0\n"
                "exit /b 9\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["FAKE_PYTHON_LOG"] = str(invocation_log)
            env.pop("REG_RAG_PYTHON", None)
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "run_mcp_stdio_server.ps1"),
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertTrue(invocation_log.is_file(), completed.stdout + completed.stderr)
            invocation = invocation_log.read_text(encoding="utf-8")
            self.assertIn("-m scripts.run_regulation_mcp", invocation)
            self.assertIn("--transport stdio", invocation)

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher behavior is Windows-specific.")
    def test_installed_runtime_survives_desktop_restart_with_different_path_python(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_dir = tmp_path / "desktop restart user's bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="govreg-local")
            (bundle_dir / "data").mkdir()

            installed_runtime = tmp_path / "installed runtime" / "python.cmd"
            installed_runtime.parent.mkdir(parents=True)
            invocation_log = tmp_path / "recorded-runtime-invocation.txt"
            installed_runtime.write_text(
                "@echo off\n"
                "if \"%1\"==\"-c\" exit /b 0\n"
                "if \"%1\"==\"-m\" echo %*>\"%RECORDED_PYTHON_LOG%\"& exit /b 0\n"
                "exit /b 9\n",
                encoding="utf-8",
            )
            (bundle_dir / "runtime_python.json").write_text(
                json.dumps(_test_runtime_marker_payload(installed_runtime), ensure_ascii=False),
                encoding="utf-8",
            )

            different_python_dir = tmp_path / "different global python"
            different_python_dir.mkdir()
            (different_python_dir / "python.cmd").write_text("@exit /b 27\n", encoding="utf-8")

            env = os.environ.copy()
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(different_python_dir), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["RECORDED_PYTHON_LOG"] = str(invocation_log)
            env.pop("REG_RAG_PYTHON", None)
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle_dir / "run_mcp_stdio_server.ps1"),
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            invocation = invocation_log.read_text(encoding="utf-8")
            self.assertIn("-m scripts.run_regulation_mcp", invocation)
            self.assertIn("--transport stdio", invocation)

    @unittest.skipUnless(os.name == "nt", "PowerShell marker isolation behavior is Windows-specific.")
    def test_runtime_marker_wins_over_polluted_pythonpath_and_preferred_project_root(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="marker-isolation-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "marker A bundle"
            polluted_project_root = root / "preferred project B"
            polluted_scripts = polluted_project_root / "scripts"
            polluted_scripts.mkdir(parents=True)
            (polluted_scripts / "__init__.py").write_text("", encoding="utf-8")
            polluted_sentinel = root / "project-b-executed.txt"
            polluted_module = (
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['POLLUTED_PROJECT_SENTINEL']).write_text('B', encoding='utf-8')\n"
                "raise SystemExit(0)\n"
            )
            for module_name in ("run_regulation_mcp.py", "check_mcp_connection_readiness.py"):
                (polluted_scripts / module_name).write_text(polluted_module, encoding="utf-8")

            write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="marker-isolation-mcp",
                preferred_python=sys.executable,
                preferred_project_root=polluted_project_root,
            )
            (bundle_dir / "data").mkdir()

            recorded_runtime_dir = root / "recorded runtime A"
            recorded_runtime_dir.mkdir()
            recorded_runtime_log = root / "recorded-runtime-a.jsonl"
            runtime_driver = recorded_runtime_dir / "runtime_driver.py"
            runtime_driver.write_text(
                "\n".join(
                    [
                        "import json, os, sys",
                        "from pathlib import Path",
                        "args = sys.argv[1:]",
                        "log_path = Path(os.environ['RECORDED_RUNTIME_LOG'])",
                        "entry = {'args': args, 'pythonpath': os.environ.get('PYTHONPATH'), 'safepath': os.environ.get('PYTHONSAFEPATH')}",
                        "with log_path.open('a', encoding='utf-8') as stream: stream.write(json.dumps(entry) + '\\n')",
                        "if os.environ.get('PYTHONPATH'): raise SystemExit(71)",
                        "if os.environ.get('PYTHONSAFEPATH') != '1': raise SystemExit(72)",
                        "if args and args[0] == '-c': raise SystemExit(0)",
                        "if len(args) >= 2 and args[0] == '-m':",
                        "    module_name = args[1]",
                        "    if module_name == 'scripts.check_mcp_connection_readiness' and '--out-json' in args:",
                        "        report_path = Path(args[args.index('--out-json') + 1])",
                        "        report_path.write_text(json.dumps({'report_type': 'mcp_connection_readiness', 'passed': True, 'findings': []}), encoding='utf-8')",
                        "    if module_name in {'scripts.run_regulation_mcp', 'scripts.check_mcp_connection_readiness'}: raise SystemExit(0)",
                        "raise SystemExit(73)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            recorded_python = recorded_runtime_dir / "python.cmd"
            recorded_python.write_text(
                f'@"{sys.executable}" "{runtime_driver}" %*\n@exit /b %ERRORLEVEL%\n',
                encoding="utf-8",
            )
            (bundle_dir / "runtime_python.json").write_text(
                json.dumps(_test_runtime_marker_payload(recorded_python), ensure_ascii=False),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["PYTHONPATH"] = str(polluted_project_root)
            env["POLLUTED_PROJECT_SENTINEL"] = str(polluted_sentinel)
            env["RECORDED_RUNTIME_LOG"] = str(recorded_runtime_log)
            env.pop("REG_RAG_PYTHON", None)
            commands = (
                [str(bundle_dir / "run_mcp_stdio_server.ps1")],
                [str(bundle_dir / "doctor_mcp_connection.ps1")],
                [str(bundle_dir / "connect_mcp_client.ps1"), "-Target", "doctor"],
            )
            for command in commands:
                with self.subTest(script=Path(command[0]).name):
                    completed = subprocess.run(
                        [
                            powershell,
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            *command,
                        ],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

            self.assertFalse(polluted_sentinel.exists(), "PreferredProjectRoot/PYTHONPATH project B executed.")
            invocations = [
                json.loads(line)
                for line in recorded_runtime_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(invocations)
            self.assertTrue(all(entry["pythonpath"] is None for entry in invocations), invocations)
            self.assertTrue(all(entry["safepath"] == "1" for entry in invocations), invocations)
            invoked_modules = [
                entry["args"][1]
                for entry in invocations
                if len(entry["args"]) >= 2 and entry["args"][0] == "-m"
            ]
            self.assertEqual(1, invoked_modules.count("scripts.run_regulation_mcp"))
            self.assertEqual(2, invoked_modules.count("scripts.check_mcp_connection_readiness"))

    @unittest.skipUnless(os.name == "nt", "PowerShell stdio streaming behavior is Windows-specific.")
    def test_recorded_runtime_launcher_streams_stdout_before_server_exit(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="marker-stream-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "marker stream bundle"
            files = write_mcp_setup_bundle(config, bundle_dir, server_name="marker-stream-mcp")
            (bundle_dir / "data").mkdir()

            runtime_dir = root / "recorded runtime"
            runtime_dir.mkdir()
            runtime_driver = runtime_dir / "runtime_driver.py"
            runtime_driver.write_text(
                "\n".join(
                    [
                        "import os, sys",
                        "args = sys.argv[1:]",
                        "if args and args[0] == '-c': raise SystemExit(0)",
                        "if len(args) < 2 or args[0] != '-m' or args[1] != 'scripts.run_regulation_mcp': raise SystemExit(81)",
                        "if os.environ.get('PYTHONPATH'): raise SystemExit(83)",
                        "if os.environ.get('PYTHONSAFEPATH') != '1': raise SystemExit(84)",
                        "line = sys.stdin.buffer.readline()",
                        "sys.stdout.buffer.write(line)",
                        "sys.stdout.buffer.flush()",
                        "sys.stdin.buffer.read()",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            recorded_python = runtime_dir / "python.cmd"
            recorded_python.write_text(
                f'@"{sys.executable}" "{runtime_driver}" %*\n@exit /b %ERRORLEVEL%\n',
                encoding="utf-8",
            )
            (bundle_dir / "runtime_python.json").write_text(
                json.dumps(_test_runtime_marker_payload(recorded_python), ensure_ascii=False),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.pop("REG_RAG_PYTHON", None)
            process = subprocess.Popen(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["stdio_launcher"],
                ],
                cwd=root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None and process.stdout is not None
            probe = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            try:
                process.stdin.write(probe)
                process.stdin.flush()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending_line = executor.submit(process.stdout.readline)
                    try:
                        streamed = pending_line.result(timeout=5)
                    except FutureTimeoutError:
                        process.stdin.close()
                        process.terminate()
                        raise AssertionError(
                            "The recorded-runtime branch buffered native stdout instead of streaming MCP stdio."
                        )
                self.assertEqual(probe, streamed)
                process.stdin.close()
                self.assertEqual(0, process.wait(timeout=10), process.stderr.read().decode("utf-8", "replace"))
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    @unittest.skipUnless(os.name == "nt", "PowerShell marker isolation behavior is Windows-specific.")
    def test_http_and_guarded_stdio_doctors_do_not_inherit_hostile_pythonpath(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="guarded-marker-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "guarded 한글 bundle"
            write_mcp_setup_bundle(config, bundle_dir, server_name="guarded-marker-mcp")
            (bundle_dir / "data").mkdir()

            hostile_root = root / "hostile pythonpath"
            hostile_scripts = hostile_root / "scripts"
            hostile_scripts.mkdir(parents=True)
            (hostile_scripts / "__init__.py").write_text("", encoding="utf-8")
            hostile_sentinel = root / "hostile-module-executed.txt"
            hostile_source = (
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['HOSTILE_MODULE_SENTINEL']).write_text('executed', encoding='utf-8')\n"
                "raise SystemExit(0)\n"
            )
            for module_name in ("check_mcp_connection_readiness.py", "run_regulation_mcp.py"):
                (hostile_scripts / module_name).write_text(hostile_source, encoding="utf-8")

            runtime_dir = root / "recorded runtime"
            runtime_dir.mkdir()
            runtime_log = root / "recorded-runtime.jsonl"
            runtime_driver = runtime_dir / "runtime_driver.py"
            runtime_driver.write_text(
                "\n".join(
                    [
                        "import json, os, sys",
                        "from pathlib import Path",
                        "args = sys.argv[1:]",
                        "entry = {'args': args, 'pythonpath': os.environ.get('PYTHONPATH'), 'safepath': os.environ.get('PYTHONSAFEPATH')}",
                        "with Path(os.environ['GUARDED_RUNTIME_LOG']).open('a', encoding='utf-8') as stream: stream.write(json.dumps(entry) + '\\n')",
                        "if args and args[0] == '-c': raise SystemExit(0)",
                        "if len(args) >= 2 and args[0] == '-m':",
                        "    if os.environ.get('PYTHONPATH'): raise SystemExit(71)",
                        "    if os.environ.get('PYTHONSAFEPATH') != '1': raise SystemExit(72)",
                        "    if args[1] in {'scripts.check_mcp_connection_readiness', 'scripts.run_regulation_mcp'}: raise SystemExit(0)",
                        "raise SystemExit(73)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            recorded_python = runtime_dir / "python.cmd"
            recorded_python.write_text(
                f'@"{sys.executable}" "{runtime_driver}" %*\n@exit /b %ERRORLEVEL%\n',
                encoding="utf-8",
            )
            (bundle_dir / "runtime_python.json").write_text(
                json.dumps(_test_runtime_marker_payload(recorded_python), ensure_ascii=False),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["PYTHONPATH"] = str(hostile_root)
            env["PYTHONSAFEPATH"] = "0"
            env["HOSTILE_MODULE_SENTINEL"] = str(hostile_sentinel)
            env["GUARDED_RUNTIME_LOG"] = str(runtime_log)
            env["MCP_AUTH_TOKEN"] = "test-only-strong-mcp-auth-token"
            env.pop("REG_RAG_PYTHON", None)

            for script_name in ("run_http_server.ps1", "run_local_stdio_server.ps1"):
                with self.subTest(script_name=script_name):
                    runtime_log.write_text("", encoding="utf-8")
                    completed = subprocess.run(
                        [
                            powershell,
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(bundle_dir / script_name),
                        ],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )

                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    invocations = [
                        json.loads(line)
                        for line in runtime_log.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    module_invocations = [
                        entry
                        for entry in invocations
                        if len(entry["args"]) >= 2 and entry["args"][0] == "-m"
                    ]
                    self.assertEqual(
                        ["scripts.check_mcp_connection_readiness", "scripts.run_regulation_mcp"],
                        [entry["args"][1] for entry in module_invocations],
                    )
                    self.assertTrue(all(entry["pythonpath"] is None for entry in module_invocations))
                    self.assertTrue(all(entry["safepath"] == "1" for entry in module_invocations))
                    self.assertFalse(hostile_sentinel.exists())

    @unittest.skipUnless(os.name == "nt", "Runtime marker identity enforcement is Windows-specific.")
    def test_runtime_marker_module_hash_mismatch_fails_closed_across_entrypoints(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="identity-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback_root = root / "stale preferred source"
            fallback_scripts = fallback_root / "scripts"
            fallback_scripts.mkdir(parents=True)
            fallback_sentinel = root / "fallback-used.txt"
            fallback_source = (
                "import os, pathlib\n"
                "pathlib.Path(os.environ['FALLBACK_SENTINEL']).write_text('used', encoding='utf-8')\n"
            )
            for name in ("run_regulation_mcp.py", "check_mcp_connection_readiness.py"):
                (fallback_scripts / name).write_text(fallback_source, encoding="utf-8")

            bundle_dir = root / "bundle 한글 spaces"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="identity-mcp",
                preferred_python=sys.executable,
                preferred_project_root=fallback_root,
            )
            (bundle_dir / "data").mkdir()
            marker = _actual_runtime_marker_payload(sys.executable)
            marker_hashes = marker["module_sha256"]
            assert isinstance(marker_hashes, dict)
            marker_hashes["scripts.run_regulation_mcp"] = "sha256:" + ("f" * 64)
            (bundle_dir / "runtime_python.json").write_text(
                json.dumps(marker, ensure_ascii=False),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["PYTHONPATH"] = str(fallback_root)
            env["FALLBACK_SENTINEL"] = str(fallback_sentinel)
            env.pop("REG_RAG_PYTHON", None)
            commands = (
                [files["stdio_launcher"]],
                [files["doctor"]],
                [files["connect"], "-Target", "doctor"],
            )
            for command in commands:
                completed = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        *command,
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("identity", (completed.stdout + completed.stderr).lower())
            self.assertFalse(fallback_sentinel.exists())

    @unittest.skipUnless(os.name == "nt", "Runtime marker v2 contract is Windows-specific.")
    def test_runtime_marker_v2_rejects_legacy_missing_extra_and_aggregate_drift(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="identity-contract-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "identity contract bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="identity-contract-mcp",
            )
            (bundle_dir / "data").mkdir()
            valid = _actual_runtime_marker_payload(sys.executable)
            cases: dict[str, dict[str, object]] = {}

            legacy = {
                "schema_version": 1,
                "python_executable": str(Path(sys.executable).resolve()),
                "minimum_python": "3.11",
                "package_import": "scripts.run_regulation_mcp",
                "written_at": "2026-07-20T00:00:00+00:00",
            }
            cases["legacy_schema"] = legacy

            missing = json.loads(json.dumps(valid))
            missing["module_sha256"].pop(RUNTIME_IDENTITY_MODULES[-1])
            cases["missing_module"] = missing

            extra = json.loads(json.dumps(valid))
            extra["module_sha256"]["scripts.unexpected_module"] = "sha256:" + ("0" * 64)
            cases["extra_module"] = extra

            malformed = json.loads(json.dumps(valid))
            malformed["module_sha256"][RUNTIME_IDENTITY_MODULES[0]] = "sha256:not-a-hash"
            cases["malformed_hash"] = malformed

            aggregate = json.loads(json.dumps(valid))
            aggregate["build_identity_sha256"] = "sha256:" + ("f" * 64)
            cases["aggregate_drift"] = aggregate

            env = os.environ.copy()
            env.pop("REG_RAG_PYTHON", None)
            for case_name, marker in cases.items():
                with self.subTest(case=case_name):
                    (bundle_dir / "runtime_python.json").write_text(
                        json.dumps(marker, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    completed = subprocess.run(
                        [
                            powershell,
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            files["stdio_launcher"],
                        ],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                    self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    self.assertIn("identity", (completed.stdout + completed.stderr).lower())

    def test_stdio_launcher_can_use_explicit_packaged_python_after_bundle_move(self) -> None:
        launcher = _powershell_stdio_launcher_script(
            ["--data-dir", "C:/bundle/data", "--transport", "stdio"],
        )

        self.assertIn("$env:REG_RAG_PYTHON", launcher)
        self.assertIn("-m scripts.run_regulation_mcp", launcher)
        self.assertIn('import scripts.run_regulation_mcp', launcher)
        self.assertIn('$ConsoleProbe = Start-Process', launcher)
        self.assertIn("stale console script", launcher)

    def test_cli_accepts_explicit_wheel_dist_directory(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["generate_mcp_client_config.py", "--include-wheel", "--wheel-dist-dir", "reports/run/dist"],
        ):
            args = parse_args()

        self.assertTrue(args.include_wheel)
        self.assertEqual("reports/run/dist", args.wheel_dist_dir)

    def test_cli_accepts_repeated_document_ids_for_selected_regulations(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "generate_mcp_client_config.py",
                "--document-id",
                "doc-a",
                "--document-id",
                "doc-b",
            ],
        ):
            args = parse_args()

        self.assertEqual(["doc-a", "doc-b"], args.document_id)

    def test_cli_accepts_canonical_chatgpt_local_and_remote_profiles(self) -> None:
        for profile in ("chatgpt-desktop-local", "chatgpt-remote"):
            with self.subTest(profile=profile), patch.object(
                sys,
                "argv",
                ["generate_mcp_client_config.py", "--client-profile", profile],
            ):
                args = parse_args()

            self.assertEqual(profile, args.client_profile)

    def test_committed_bundle_readmes_require_real_runtime_visibility_audit(self) -> None:
        reports_dir = Path(__file__).resolve().parents[1] / "reports"
        readmes = sorted(reports_dir.glob("mcp_connection_bundle*/README*.md"))
        if not readmes:
            self.skipTest("No generated MCP connection bundle README artifacts present.")

        missing = []
        for path in readmes:
            text = path.read_text(encoding="utf-8")
            if (
                "reg-rag-mcp-index-visibility" not in text
                or "--forbid-smoke-docs" not in text
                or "--require-indexed" not in text
            ):
                missing.append(path.relative_to(reports_dir.parent).as_posix())

        self.assertEqual([], missing)

    def test_builds_stdio_config_with_tenant_isolation(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-test",
            data_dir="data",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
            transport="stdio",
        )

        server = config["mcpServers"]["govreg-test"]
        self.assertEqual(server["command"], "reg-rag-mcp-server")
        self.assertIn("--tenant-storage-isolation", server["args"])
        self.assertNotIn("--flat-storage", server["args"])
        self.assertIn("tenant-a", server["args"])

    def test_builds_streamable_http_config_with_client_url(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-http",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
            transport="streamable-http",
            host="0.0.0.0",
            port=9000,
            actor="mcp-tenant-a",
            role="operator",
            department_ids=["hr", "legal"],
        )

        server = config["mcpServers"]["govreg-http"]
        self.assertEqual(server["url"], "http://127.0.0.1:9000/mcp")
        self.assertEqual(server["transport"], "streamable-http")
        self.assertIn("--host", server["serverCommand"]["args"])
        self.assertIn("0.0.0.0", server["serverCommand"]["args"])
        self.assertIn("--tenant-storage-isolation", server["serverCommand"]["args"])
        self.assertIn("mcp-tenant-a", server["serverCommand"]["args"])
        self.assertIn("operator", server["serverCommand"]["args"])
        self.assertEqual(server["serverCommand"]["args"].count("--department-id"), 2)

    def test_builds_claude_code_stdio_config(self) -> None:
        config = build_mcp_client_config(
            client_profile="claude-code",
            transport="stdio",
            tenant_id="tenant-a",
            role="operator",
        )

        self.assertEqual(config["type"], "stdio")
        self.assertEqual(config["command"], "reg-rag-mcp-server")
        self.assertIn("--tenant-id", config["args"])
        self.assertIn("--flat-storage", config["args"])
        self.assertIn("tenant-a", config["args"])
        self.assertIn("operator", config["args"])

    def test_builds_chatgpt_connector_config_with_public_url(self) -> None:
        config = build_mcp_client_config(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            public_url="https://mcp.example.go.kr/govreg",
            tenant_id="tenant-a",
            tenant_storage_isolation=True,
        )

        self.assertEqual(config["connector_url"], "https://mcp.example.go.kr/govreg/mcp")
        self.assertTrue(config["chatgpt_setup"]["requires_reachable_https"])
        self.assertTrue(config["chatgpt_setup"]["https_endpoint_ready"])
        self.assertIn("Settings > Plugins", config["chatgpt_setup"]["location"])
        self.assertIn("https://chatgpt.com/plugins", config["chatgpt_setup"]["location"])
        self.assertIn("Developer mode", " ".join(config["connection_steps"]))
        self.assertNotIn("Apps/Connectors", json.dumps(config, ensure_ascii=False))
        self.assertNotIn("@", json.dumps(config, ensure_ascii=False))
        self.assertIn("search", config["compatible_tools"])
        self.assertIn("fetch", config["compatible_tools"])
        self.assertEqual(["search", "fetch"], config["compatible_tools"])
        self.assertIn("--tenant-storage-isolation", config["server_start"]["args"])
        self.assertIn("--tool-profile", config["server_start"]["args"])
        self.assertIn("chatgpt-data", config["server_start"]["args"])
        self.assertIn("--allowed-http-host", config["server_start"]["args"])
        self.assertIn("--allowed-http-origin", config["server_start"]["args"])
        self.assertIn("--no-warm-cache", config["server_start"]["args"])
        self.assertIn("--http-bearer-token-env", config["server_start"]["args"])
        self.assertIn("--auth-issuer-url", config["server_start"]["args"])
        self.assertIn("https://mcp.example.go.kr/govreg", config["server_start"]["args"])
        self.assertEqual(config["server_auth"]["token_env"], "MCP_AUTH_TOKEN")
        self.assertNotIn("$BundleDataDir", config["openai_secure_tunnel"]["copy_paste_ps"])

    def test_chatgpt_connector_requires_https_public_url(self) -> None:
        config = build_mcp_client_config(
            client_profile="chatgpt-remote",
            transport="streamable-http",
            public_url="http://mcp.example.go.kr",
        )

        self.assertFalse(config["ready"])
        self.assertEqual(config["connector_url"], "http://mcp.example.go.kr/mcp")
        self.assertIn("public_url_must_use_https", config["missing"])
        self.assertTrue(config["chatgpt_setup"]["requires_reachable_https"])
        self.assertFalse(config["chatgpt_setup"]["https_endpoint_ready"])

    def test_chatgpt_connector_rejects_hostless_or_query_public_url(self) -> None:
        for public_url in ("https://", "https://?tenant=default", "https://mcp.example.go.kr/mcp?tenant=default"):
            with self.subTest(public_url=public_url):
                config = build_mcp_client_config(
                    client_profile="chatgpt-remote",
                    transport="streamable-http",
                    public_url=public_url,
                )

                self.assertFalse(config["ready"])
                self.assertIsNone(config["connector_url"])
                self.assertIn("public_url_https_mcp_endpoint", config["missing"])
                self.assertFalse(config["chatgpt_setup"]["https_endpoint_ready"])

    def test_builds_bundle_for_common_clients(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr/mcp",
        )

        self.assertIn("quickstart", config)
        self.assertIn("claude_desktop", config)
        self.assertIn("claude_code", config)
        self.assertIn("chatgpt_desktop_local", config)
        self.assertIn("chatgpt_remote", config)
        self.assertIn("claude_api", config)
        self.assertEqual(config["chatgpt_remote"]["connector_url"], "https://mcp.example.go.kr/mcp")
        self.assertEqual(config["claude_api"]["mcp_servers"][0]["url"], "https://mcp.example.go.kr/mcp")
        self.assertEqual(config["quickstart"]["claude_code"]["command"], "claude")
        self.assertEqual(
            config["quickstart"]["claude_code"]["args"][:7],
            ["mcp", "add", "--transport", "stdio", "--scope", "user", "aks_mcp"],
        )
        claude_desktop_args = config["claude_desktop"]["mcpServers"]["aks_mcp"]["args"]
        claude_code_args = config["claude_code"]["args"]
        self.assertIn("--no-warm-cache", claude_desktop_args)
        self.assertIn("--no-warm-cache", claude_code_args)
        self.assertEqual(config["quickstart"]["chatgpt_remote"]["verification_tools"], ["search", "fetch"])
        self.assertTrue(config["quickstart"]["chatgpt_remote"]["requires_reachable_https"])
        self.assertTrue(config["quickstart"]["chatgpt_remote"]["https_endpoint_ready"])
        self.assertIn("openai_secure_tunnel", config["quickstart"]["chatgpt_remote"]["connection_options"])
        self.assertEqual(config["quickstart"]["chatgpt_desktop_local"]["profile"], "chatgpt-desktop-local")
        self.assertTrue(config["quickstart"]["chatgpt_desktop_local"]["conversation_attachment_unverified"])
        self.assertEqual(config["quickstart"]["openai_secure_tunnel"]["tunnel_id_env"], "OPENAI_TUNNEL_ID")
        self.assertEqual(config["quickstart"]["openai_secure_tunnel"]["setup_state"], "manual_setup_required")
        self.assertIn("--tool-profile chatgpt-data", config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
        self.assertIn("--no-warm-cache", config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
        self.assertIn("--flat-storage", config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
        self.assertIn("--fail-on-warning", config["quickstart"]["openai_secure_tunnel"]["commands"]["readiness"]["args"])
        self.assertIn(
            "--audit-index-visibility",
            config["quickstart"]["openai_secure_tunnel"]["commands"]["readiness"]["args"],
        )
        self.assertEqual(config["quickstart"]["claude_api"]["copy_fields"], ["mcp_servers", "tools", "betas"])
        self.assertIn("--http-bearer-token-env", config["quickstart"]["run_http_server"]["args"])
        self.assertIn("--auth-issuer-url", config["quickstart"]["run_http_server"]["args"])
        self.assertIn("--no-warm-cache", config["quickstart"]["run_http_server"]["args"])
        self.assertIn("https://mcp.example.go.kr", config["quickstart"]["run_http_server"]["args"])
        self.assertEqual("chatgpt-data", config["quickstart"]["run_chatgpt_data_server"]["tool_profile"])
        self.assertIn("--tool-profile", config["chatgpt_remote"]["server_start"]["args"])
        self.assertIn("chatgpt-data", config["chatgpt_remote"]["server_start"]["args"])
        self.assertIn("chatgpt-data", config["claude_api"]["server_start"]["args"])
        self.assertEqual(["search", "fetch"], config["quickstart"]["chatgpt_remote"]["verification_tools"])
        self.assertIn("--no-warm-cache", config["quickstart"]["run_chatgpt_data_server"]["args"])
        self.assertIn("copy_paste", config["quickstart"])
        self.assertIn(
            '$ClaudeAddArgs = @("mcp", "add", "--transport", "stdio", "--scope", "user"',
            config["quickstart"]["copy_paste"]["claude_code_stdio_ps"],
        )
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["claude_code_stdio_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["run_local_stdio_server_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn('Assert-EnvVar "MCP_AUTH_TOKEN"', config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("Invoke-BundlePythonModule $DoctorPython", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("'--client-profile', 'bundle'", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("--audit-index-visibility", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn('Resolve-BundleModulePython "scripts.check_mcp_connection_readiness"', config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("if ($DoctorExitCode -ne 0)", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn("'--tool-profile'", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("'chatgpt-data'", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("--no-warm-cache", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("Invoke-BundlePythonModule $DoctorPython", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("'--client-profile', 'chatgpt-remote'", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        self.assertIn("--audit-index-visibility", config["quickstart"]["copy_paste"]["run_chatgpt_data_server_ps"])
        tunnel_script = config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"]
        self.assertIn("tunnel-client init", tunnel_script)
        encoded_match = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)'", tunnel_script)
        self.assertIsNotNone(encoded_match)
        assert encoded_match is not None
        decoded_tunnel_launcher = base64.b64decode(encoded_match.group(1)).decode("utf-16-le")
        self.assertIn("'--no-warm-cache'", decoded_tunnel_launcher)
        self.assertIn('$ErrorActionPreference = "Stop"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Assert-Command "tunnel-client"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Assert-EnvVar "CONTROL_PLANE_API_KEY"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Assert-EnvVar "OPENAI_TUNNEL_ID"', config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn("if ($LASTEXITCODE -ne 0)", config["quickstart"]["copy_paste"]["openai_secure_tunnel_ps"])
        self.assertIn('Resolve-BundleModulePython "scripts.check_mcp_connection_readiness"', config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("'--bundle-dir'", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("= $BundleDir", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("--audit-index-visibility", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("'--tenant-id', 'tenant-a'", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("mcp_connection_readiness.json", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("'--out-json'", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("= $DoctorReport", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertEqual("reg-rag-mcp-index-visibility", config["quickstart"]["audit_index_visibility"]["command"])
        self.assertIn("reg-rag-mcp-index-visibility", config["quickstart"]["copy_paste"]["audit_index_visibility_ps"])
        self.assertNotIn("--allow-local-only-bundle", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("chatgpt-tunnel", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertIn("Claude Desktop config updated", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertIn("Claude Code CLI was not found", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertIn("No ChatGPT remote connector_url is ready", config["quickstart"]["copy_paste"]["connect_wizard_ps"])
        self.assertTrue(config["quickstart"]["warnings"])

    def test_builds_claude_api_connector_config(self) -> None:
        config = build_mcp_client_config(
            client_profile="claude-api",
            transport="streamable-http",
            public_url="https://mcp.example.go.kr",
            server_name="govreg-local",
        )

        self.assertEqual(config["mcp_servers"][0]["type"], "url")
        self.assertEqual(config["mcp_servers"][0]["url"], "https://mcp.example.go.kr/mcp")
        self.assertNotIn("authorization_token_env", config["mcp_servers"][0])
        self.assertEqual(config["server_auth"]["token_env"], "MCP_AUTH_TOKEN")
        self.assertIn("--no-warm-cache", config["server_start"]["args"])
        self.assertEqual(config["tools"][0]["type"], "mcp_toolset")
        self.assertEqual(config["tools"][0]["mcp_server_name"], "govreg-local")
        self.assertIn("mcp-client-2025-11-20", config["betas"])
        self.assertIn("--auth-issuer-url", config["server_start"]["args"])
        self.assertIn("https://mcp.example.go.kr", config["server_start"]["args"])
        self.assertIn("connection_steps", config)

    def test_claude_api_rejects_non_https_public_url(self) -> None:
        config = build_mcp_client_config(
            client_profile="claude-api",
            transport="streamable-http",
            public_url="http://mcp.example.go.kr/mcp",
            server_name="govreg-local",
        )

        self.assertFalse(config["ready"])
        self.assertEqual([], config["mcp_servers"])
        self.assertEqual([], config["tools"])
        self.assertIn("public_url_must_use_https", config["missing"])

    def test_remote_auth_token_env_rejects_powershell_metacharacters(self) -> None:
        for token_env in ("TOKEN;Write-Output-INJECT", "TOKEN|whoami", "$(whoami)"):
            with self.subTest(token_env=token_env):
                with self.assertRaisesRegex(ValueError, "environment variable name"):
                    build_mcp_client_config(
                        client_profile="bundle",
                        public_url="https://mcp.example.go.kr/mcp",
                        remote_auth_token_env=token_env,
                    )

    def test_claude_code_http_preserves_literal_environment_reference(self) -> None:
        config = build_mcp_client_config(
            client_profile="bundle",
            public_url="https://mcp.example.go.kr/mcp",
            remote_auth_token_env="MCP_AUTH_TOKEN",
        )

        script = config["quickstart"]["copy_paste"]["claude_code_http_ps"]
        self.assertIn("'Authorization: Bearer ${MCP_AUTH_TOKEN}'", script)
        self.assertNotIn("$env:MCP_AUTH_TOKEN", script)

    def test_bundle_without_public_url_marks_remote_profiles_not_ready(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        self.assertFalse(config["chatgpt"]["ready"])
        self.assertIn("public_url_https_mcp_endpoint", config["chatgpt"]["missing"])
        self.assertFalse(config["chatgpt"]["chatgpt_setup"]["https_endpoint_ready"])
        self.assertFalse(config["quickstart"]["chatgpt_remote"]["https_endpoint_ready"])
        self.assertFalse(config["claude_api"]["ready"])
        self.assertEqual([], config["claude_api"]["mcp_servers"])
        self.assertIsNone(config["quickstart"]["chatgpt_remote"]["connector_url"])
        self.assertIsNone(config["quickstart"]["claude_api"]["mcp_server_url"])
        self.assertIsNone(config["quickstart"]["copy_paste"]["claude_code_http_ps"])
        self.assertIn("--allow-local-only-bundle", config["quickstart"]["copy_paste"]["doctor_ps"])

    def test_bundle_can_enforce_min_visible_records_in_doctor_commands(self) -> None:
        config = build_mcp_client_config(
            client_profile="bundle",
            tenant_id="tenant-a",
            min_visible_records=5000,
        )

        self.assertIn("5000", config["quickstart"]["audit_index_visibility"]["args"])
        self.assertIn("'--min-visible-records'", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("'5000'", config["quickstart"]["copy_paste"]["doctor_ps"])
        self.assertIn("'--min-visible-records', '5000'", config["quickstart"]["copy_paste"]["run_http_server_ps"])
        self.assertIn(
            "5000",
            config["quickstart"]["openai_secure_tunnel"]["commands"]["readiness"]["args"],
        )

    def test_writes_copy_paste_setup_bundle(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr",
        )

        with tempfile.TemporaryDirectory() as tmp:
            files = write_mcp_setup_bundle(
                config,
                tmp,
                server_name="govreg-local",
                preferred_python=sys.executable,
                preferred_project_root=Path(__file__).resolve().parents[1],
            )
            output_dir = Path(tmp)
            generated_names = {path.name for path in output_dir.iterdir() if path.is_file()}

            self.assertTrue(REQUIRED_SETUP_BUNDLE_FILES.issubset(generated_names))
            self.assertEqual(ALL_SETUP_BUNDLE_FILES, generated_names)
            self.assertTrue((output_dir / "README.md").exists())
            self.assertTrue((output_dir / "README.ko.md").exists())
            self.assertTrue((output_dir / "bundle_status.json").exists())
            self.assertTrue((output_dir / "codex_config_snippet.toml").exists())
            self.assertTrue((output_dir / "claude_desktop_config.json").exists())
            self.assertTrue((output_dir / "chatgpt_connector.json").exists())
            self.assertTrue((output_dir / "chatgpt_desktop_local_mcp.json").exists())
            self.assertTrue((output_dir / "claude_api_fragment.json").exists())
            self.assertTrue((output_dir / "run_http_server.ps1").exists())
            self.assertTrue((output_dir / "run_chatgpt_data_server.ps1").exists())
            self.assertTrue((output_dir / "run_openai_secure_tunnel.ps1").exists())
            self.assertTrue((output_dir / "doctor_mcp_connection.ps1").exists())
            self.assertTrue((output_dir / "validate_client_config_smoke.ps1").exists())
            self.assertTrue((output_dir / "connect_mcp_client.ps1").exists())
            self.assertTrue((output_dir / "MCP 사용 시작하기.txt").exists())
            self.assertTrue((output_dir / "설치 후 MCP 사용 방법 보기.bat").exists())
            self.assertTrue((output_dir / "Codex 플러그인 MCP 입력값.txt").exists())
            self.assertTrue((output_dir / "Codex에 연결하기.bat").exists())
            self.assertTrue((output_dir / "ChatGPT Desktop에 연결하기.bat").exists())
            self.assertTrue((output_dir / "Claude Desktop에 연결하기.bat").exists())
            self.assertTrue((output_dir / "Claude Code에 연결하기.bat").exists())
            self.assertTrue((output_dir / "ChatGPT HTTPS에 연결하기.bat").exists())
            self.assertTrue((output_dir / "ChatGPT 보안 Tunnel에 연결하기.bat").exists())
            self.assertTrue((output_dir / "Claude HTTPS에 연결하기.bat").exists())
            self.assertTrue((output_dir / "연결 상태 확인하기.bat").exists())
            self.assertTrue((output_dir / "install_local_package.ps1").exists())
            self.assertTrue((output_dir / "claude_code_add_stdio.ps1").exists())
            self.assertIn("claude_code_stdio", files)
            self.assertIn("codex_config", files)
            self.assertIn("client_config_smoke", files)
            self.assertIn("chatgpt_desktop_local", files)
            self.assertIn("connect", files)
            self.assertIn("usage_guide", files)
            self.assertIn("usage_guide_bat", files)
            self.assertIn("chatgpt_desktop_agent_prompt", files)
            self.assertIn("codex_agent_prompt", files)
            self.assertIn("claude_code_agent_prompt", files)
            self.assertIn("codex_plugin_guide", files)
            self.assertIn("connect_codex_bat", files)
            self.assertIn("connect_chatgpt_desktop_bat", files)
            self.assertIn("connect_claude_desktop_bat", files)
            self.assertIn("connect_claude_code_bat", files)
            self.assertIn("connect_chatgpt_https_bat", files)
            self.assertIn("connect_chatgpt_tunnel_bat", files)
            self.assertIn("connect_claude_https_bat", files)
            self.assertIn("doctor_bat", files)
            self.assertIn("install", files)
            manifest = (output_dir / "manifest.json").read_text(encoding="utf-8")
            manifest_payload = json.loads(manifest)
            self.assertEqual(
                [
                    "Claude Code",
                    "Codex CLI",
                    "Claude Desktop",
                    "ChatGPT Desktop",
                    "ChatGPT 원격 MCP",
                    "ChatGPT 웹",
                    "Claude (HTTPS MCP)",
                ],
                [connection["client"] for connection in manifest_payload["connections"]],
            )
            bundle_status = json.loads((output_dir / "bundle_status.json").read_text(encoding="utf-8"))
            plugin_root = output_dir / "chatgpt-desktop-local-plugin"
            plugin_manifest_path = plugin_root / "plugins" / "govreg-local" / ".codex-plugin" / "plugin.json"
            plugin_mcp_path = plugin_root / "plugins" / "govreg-local" / ".mcp.json"
            marketplace_path = plugin_root / ".agents" / "plugins" / "marketplace.json"
            for machine_path in [plugin_manifest_path, plugin_mcp_path, marketplace_path]:
                self.assertFalse(machine_path.read_bytes().startswith(b"\xef\xbb\xbf"), machine_path)
                json.loads(machine_path.read_text(encoding="utf-8"))
            plugin_manifest = json.loads(plugin_manifest_path.read_text(encoding="utf-8"))
            plugin_mcp = json.loads(plugin_mcp_path.read_text(encoding="utf-8"))
            self.assertRegex(plugin_manifest["version"], r"^0\.1\.0\+codex\.[0-9a-f]{12}$")
            self.assertEqual("./.mcp.json", plugin_manifest["mcpServers"])
            self.assertEqual({"govreg-local"}, set(plugin_mcp["mcpServers"]))
            self.assertNotIn("mcp_servers", plugin_mcp)
            agent_prompt = Path(files["chatgpt_desktop_agent_prompt"]).read_text(encoding="utf-8")
            self.assertIn(AGENT_CONNECT_BUNDLE_NAME_MARKER, agent_prompt)
            self.assertIn(AGENT_CONNECT_BUNDLE_DIR_MARKER, agent_prompt)
            self.assertNotIn(AGENT_CONNECT_BUNDLE_DIR_PS_LITERAL_MARKER, agent_prompt)
            self.assertNotIn(str(output_dir.resolve()), agent_prompt)
            self.assertIn("ChatGPT Desktop 전용", agent_prompt)
            self.assertIn("Settings > MCP servers", agent_prompt)
            self.assertIn("Add server", agent_prompt)
            self.assertIn("Name: govreg-local", agent_prompt)
            self.assertIn("Type: STDIO", agent_prompt)
            self.assertIn("Command: powershell.exe", agent_prompt)
            self.assertIn("Working directory:", agent_prompt)
            self.assertIn("Arguments", agent_prompt)
            self.assertIn("Save", agent_prompt)
            self.assertIn("Restart", agent_prompt)
            self.assertIn("├─ CHATGPT_DESKTOP_CONNECT_GUIDE.md", agent_prompt)
            self.assertIn("├─ ChatGPT Desktop에 연결하기.bat", agent_prompt)
            self.assertIn("├─ data\\", agent_prompt)
            self.assertIn("reg_rag_preprocessor-*.whl", agent_prompt)
            self.assertIn("runtime_python.json", agent_prompt)
            self.assertNotIn("codex mcp get", agent_prompt)
            self.assertNotIn("-Target codex", agent_prompt)
            self.assertNotIn("Codex", agent_prompt)
            self.assertIn("/mcp", agent_prompt)
            final_tool_prompt = "govreg-local MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘."
            self.assertIn(final_tool_prompt, agent_prompt)
            rendered_desktop_guide = render_agent_connect_prompt_for_program(
                agent_prompt,
                bundle_dir=output_dir,
            )
            self.assertIn(str(output_dir.resolve()), rendered_desktop_guide)
            self.assertIn(f"생성 프로그램이 지정한 번들 폴더 이름: `{output_dir.name}`", rendered_desktop_guide)
            self.assertNotIn(AGENT_CONNECT_BUNDLE_DIR_MARKER, rendered_desktop_guide)
            self.assertNotIn(AGENT_CONNECT_BUNDLE_NAME_MARKER, rendered_desktop_guide)
            self.assertIn("Name: govreg-local", rendered_desktop_guide)
            self.assertIn("Command: powershell.exe", rendered_desktop_guide)
            self.assertIn("Arguments", rendered_desktop_guide)
            self.assertNotIn("-Target codex", rendered_desktop_guide)
            codex_agent_prompt = Path(files["codex_agent_prompt"]).read_text(encoding="utf-8")
            self.assertIn(AGENT_CONNECT_BUNDLE_DIR_MARKER, codex_agent_prompt)
            self.assertNotIn(str(output_dir.resolve()), codex_agent_prompt)
            self.assertIn("codex mcp get govreg-local --json", codex_agent_prompt)
            self.assertIn("direct_stdio_verified", codex_agent_prompt)
            self.assertIn("desktop_app_server_loader_verified", codex_agent_prompt)
            self.assertIn(final_tool_prompt, codex_agent_prompt)
            self.assertIn("Codex를 완전히 종료하고 다시 실행", codex_agent_prompt)
            claude_agent_prompt = Path(files["claude_code_agent_prompt"]).read_text(encoding="utf-8")
            self.assertIn(AGENT_CONNECT_BUNDLE_DIR_MARKER, claude_agent_prompt)
            self.assertNotIn(str(output_dir.resolve()), claude_agent_prompt)
            self.assertIn("claude mcp get govreg-local", claude_agent_prompt)
            self.assertIn(final_tool_prompt, claude_agent_prompt)
            self.assertIn("Claude Code를 완전히 종료하고 다시 실행", claude_agent_prompt)
            readme = (output_dir / "README.md").read_text(encoding="utf-8")
            readme_ko = (output_dir / "README.ko.md").read_text(encoding="utf-8")
            codex_snippet = tomllib.loads((output_dir / "codex_config_snippet.toml").read_text(encoding="utf-8"))
            codex_server = codex_snippet["mcp_servers"]["govreg-local"]
            codex_args = codex_server["args"]
            self.assertTrue((output_dir / "run_mcp_stdio_server.ps1").exists())
            self.assertEqual("powershell.exe", codex_server["command"])
            self.assertEqual(45, codex_server["startup_timeout_sec"])
            self.assertEqual(str(output_dir.resolve()), codex_server["cwd"])
            self.assertIn("-NoProfile", codex_args)
            self.assertIn("-File", codex_args)
            self.assertIn(str((output_dir / "run_mcp_stdio_server.ps1").resolve()), codex_args)
            self.assertIn(str((output_dir / "data").resolve()), codex_args)
            self.assertIn("--transport", codex_args)
            self.assertIn("stdio", codex_args)
            self.assertIn("--no-warm-cache", codex_args)
            self.assertIn("--flat-storage", codex_args)
            stdio_launcher = (output_dir / "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")
            self.assertIn('Get-Command "reg-rag-mcp-server"', stdio_launcher)
            self.assertIn("scripts\\run_regulation_mcp.py", stdio_launcher)
            self.assertIn(f"$PreferredPython = '{sys.executable}'", stdio_launcher)
            self.assertIn(
                f"$PreferredProjectRoot = '{Path(__file__).resolve().parents[1]}'",
                stdio_launcher,
            )
            self.assertIn("if (-not $ProjectRoot -and $PreferredProjectRoot)", stdio_launcher)
            self.assertLess(
                stdio_launcher.index("$ProjectRoot = Find-ProjectRoot"),
                stdio_launcher.index('$ConsoleCommand = Get-Command "reg-rag-mcp-server"'),
            )
            self.assertIn("install_local_package.ps1 once", stdio_launcher)
            self.assertIn("$VerifierStartInfo.RedirectStandardInput = $true", stdio_launcher)
            self.assertIn("$VerifierStartInfo.RedirectStandardOutput = $true", stdio_launcher)
            self.assertIn("$VerifierProcess.StandardInput.Close()", stdio_launcher)
            self.assertIn(
                'if (-not (Test-RuntimeMarkerIdentity $Resolved $Marker))',
                stdio_launcher,
            )
            self.assertIn(
                'Invoke-RecordedRuntimeServer $RecordedRuntimePython $ServerArgs',
                stdio_launcher,
            )
            self.assertNotIn(
                '$RecordedRuntimeExitCode = Invoke-RecordedRuntimeServer',
                stdio_launcher,
            )
            self.assertIn('exit [int]$ServerExitCode', stdio_launcher)
            self.assertIn(
                '$ClaudeAddArgs = @("mcp", "add", "--transport", "stdio", "--scope", "user", "govreg-local"',
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Invoke-ClaudeMcpCli @("mcp", "remove", "govreg-local", "--scope", "local")',
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Invoke-ClaudeMcpCli @("mcp", "remove", "govreg-local", "--scope", "user")',
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Invoke-ClaudeMcpCli @("mcp", "get", "govreg-local")',
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "reg-rag-mcp-doctor --client-profile bundle --transport stdio",
                (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "--forbid-smoke-docs",
                (output_dir / "run_local_stdio_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '$DoctorExitCode = Invoke-BundlePythonModule $DoctorPython "scripts.check_mcp_connection_readiness" $DoctorArgs',
                (output_dir / "run_local_stdio_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn('"manifest": "manifest.json"', manifest)
            self.assertIn('"bundle_status": "bundle_status.json"', manifest)
            self.assertIn('"readme": "README.md"', manifest)
            self.assertIn('"readme_ko": "README.ko.md"', manifest)
            self.assertIn('"codex_config": "codex_config_snippet.toml"', manifest)
            self.assertIn('"chatgpt_desktop_local": "chatgpt_desktop_local_mcp.json"', manifest)
            self.assertIn('"stdio_launcher": "run_mcp_stdio_server.ps1"', manifest)
            self.assertIn('"client_config_smoke": "validate_client_config_smoke.ps1"', manifest)
            self.assertIn('"mode": "secure_mcp_tunnel"', manifest)
            self.assertIn('"ready": "manual_setup_required"', manifest)
            self.assertIn('"primary_file": "ChatGPT 보안 Tunnel에 연결하기.bat"', manifest)
            self.assertIn('"config_file": "run_openai_secure_tunnel.ps1"', manifest)
            self.assertIn('"connect": "connect_mcp_client.ps1"', manifest)
            self.assertIn('"usage_guide": "MCP 사용 시작하기.txt"', manifest)
            self.assertIn('"usage_guide_bat": "설치 후 MCP 사용 방법 보기.bat"', manifest)
            self.assertIn('"codex_plugin_guide": "Codex 플러그인 MCP 입력값.txt"', manifest)
            self.assertIn('"connect_codex_bat": "Codex에 연결하기.bat"', manifest)
            self.assertIn('"connect_chatgpt_desktop_bat": "ChatGPT Desktop에 연결하기.bat"', manifest)
            chatgpt_desktop_bat = Path(files["connect_chatgpt_desktop_bat"]).read_text(encoding="utf-8-sig")
            codex_bat = Path(files["connect_codex_bat"]).read_text(encoding="utf-8")
            claude_code_bat = Path(files["connect_claude_code_bat"]).read_text(encoding="utf-8")
            self.assertIn("-InstallPackage -Target chatgpt-desktop-direct", chatgpt_desktop_bat)
            self.assertNotIn("-Target codex", chatgpt_desktop_bat)
            self.assertNotIn("-InstallChatGptDesktopPlugin", chatgpt_desktop_bat)
            self.assertIn('"connect_claude_desktop_bat": "Claude Desktop에 연결하기.bat"', manifest)
            self.assertIn('"connect_claude_code_bat": "Claude Code에 연결하기.bat"', manifest)
            self.assertIn('"connect_chatgpt_https_bat": "ChatGPT HTTPS에 연결하기.bat"', manifest)
            self.assertIn('"connect_chatgpt_tunnel_bat": "ChatGPT 보안 Tunnel에 연결하기.bat"', manifest)
            self.assertIn('"connect_claude_https_bat": "Claude HTTPS에 연결하기.bat"', manifest)
            self.assertIn('"doctor_bat": "연결 상태 확인하기.bat"', manifest)
            self.assertIn('"primary_file": "CODEX_AGENT_CONNECT_PROMPT.md"', manifest)
            self.assertIn('"fallback_file": "Codex에 연결하기.bat"', manifest)
            self.assertIn('"primary_file": "CLAUDE_CODE_AGENT_CONNECT_PROMPT.md"', manifest)
            self.assertIn('"fallback_file": "Claude Code에 연결하기.bat"', manifest)
            self.assertIn('"config_file": "codex_config_snippet.toml"', manifest)
            self.assertIn('"client": "ChatGPT Desktop"', manifest)
            self.assertIn('"profile": "chatgpt-desktop-local"', manifest)
            self.assertIn('"profile": "chatgpt-remote"', manifest)
            self.assertIn('"primary_file": "CHATGPT_DESKTOP_CONNECT_GUIDE.md"', manifest)
            self.assertIn('"fallback_file": "ChatGPT Desktop에 연결하기.bat"', manifest)
            self.assertIn('"primary_file": "Claude Desktop에 연결하기.bat"', manifest)
            self.assertIn('"config_file": "claude_desktop_config.json"', manifest)
            self.assertIn('"install": "install_local_package.ps1"', manifest)
            self.assertIn("Run `connect_mcp_client.ps1`", readme)
            self.assertIn("For direct Codex CLI compatibility, paste `CODEX_AGENT_CONNECT_PROMPT.md`", readme)
            self.assertIn("double-click `Claude Desktop에 연결하기.bat`", readme)
            self.assertIn("run `/mcp` and verify", readme)
            self.assertIn("Do not apply the `/mcp` step above to Claude Desktop", readme)
            self.assertLess(
                readme.index("Open this extracted bundle as that app's local workspace"),
                readme.index("Only after verification completes, fully quit and restart that client"),
            )
            self.assertLess(
                readme.index("Only after verification completes, fully quit and restart that client"),
                readme.index("run `/mcp` and verify"),
            )
            self.assertIn("get_index_status", readme)
            self.assertIn("bundle_status.json", readme)
            self.assertIn("codex_config_snippet.toml", readme)
            self.assertIn("ChatGPT Desktop에 연결하기.bat", readme)
            self.assertIn("validate_client_config_smoke.ps1", readme)
            self.assertIn("recommended_smoke_query", readme)
            self.assertIn(
                "uses it before any source checkout, environment override, or PATH",
                readme,
            )
            self.assertIn("-ValidateClaudeDesktop", readme)
            self.assertIn("--codex-config", readme)
            self.assertIn("--claude-desktop-config", readme)
            self.assertIn("Get-Content -Encoding UTF8", readme)
            self.assertIn("approval journals and vector IDs are keyed", readme)
            self.assertIn("reg-rag-mcp-index-visibility", readme)
            self.assertIn("reg-rag-mcp-index-visibility", readme_ko)
            self.assertIn("pip install -e .", readme)
            self.assertIn("bundled `reg_rag_preprocessor-*.whl`", readme)
            self.assertIn("--include-wheel", readme)
            self.assertIn("https://help.openai.com/en/articles/20001256-plugins-in-codex", readme)
            self.assertIn("Settings > Plugins", readme)
            self.assertIn("https://chatgpt.com/plugins", readme)
            self.assertIn("Settings > MCP servers > Add server", readme_ko)
            self.assertNotIn("Apps/Connectors", readme)
            self.assertNotIn("Apps/Connectors", readme_ko)
            self.assertIn("https://modelcontextprotocol.io/specification/2025-11-25/basic/transports", readme)
            self.assertIn("https://docs.anthropic.com/en/docs/claude-code/mcp", readme)
            self.assertIn("MCP 연결 번들", readme_ko)
            self.assertNotIn("| Client | Mode | Ready | Primary file |", readme)
            self.assertIn("| Client | Mode | Setup artifact ready | Primary file |", readme)
            self.assertNotIn("| 클라이언트 | 방식 | 준비 상태 | 주요 파일 |", readme_ko)
            self.assertIn("| 클라이언트 | 방식 | 설정 산출물 준비 | 주요 파일 |", readme_ko)
            self.assertIn("does not mean client registration, loader recognition, or a conversation tool call succeeded", readme)
            self.assertIn("클라이언트 등록·로더 인식·현재 대화 도구 호출 성공을 뜻하지 않습니다", readme_ko)
            self.assertIn("CHATGPT_DESKTOP_CONNECT_GUIDE.md", readme_ko)
            self.assertIn("CODEX_AGENT_CONNECT_PROMPT.md", readme_ko)
            self.assertIn("CLAUDE_CODE_AGENT_CONNECT_PROMPT.md", readme_ko)
            self.assertIn("Claude Desktop에는 위 `/mcp` 공통 절차를 적용하지 않습니다", readme_ko)
            self.assertIn("bundle_status.json", readme_ko)
            self.assertIn("codex_config_snippet.toml", readme_ko)
            self.assertIn("validate_client_config_smoke.ps1", readme_ko)
            self.assertIn("`list_tools`, `get_index_status`, `search`, `fetch`", readme_ko)
            self.assertIn("recommended_smoke_query", readme_ko)
            self.assertIn(
                "저장소 checkout, `REG_RAG_PYTHON`, PATH보다 먼저 사용",
                readme_ko,
            )
            self.assertIn("connect_mcp_client.ps1", readme_ko)
            self.assertNotIn("연결 상태만 확인할 때", readme_ko)
            self.assertIn("번들·설정 사전 진단", readme_ko)
            self.assertIn("Codex에 연결하기.bat", readme_ko)
            self.assertIn("ChatGPT Desktop에 연결하기.bat", readme_ko)
            self.assertIn("Claude Desktop에 연결하기.bat", readme_ko)
            self.assertIn("Claude Code에 연결하기.bat", readme_ko)
            self.assertIn("ChatGPT HTTPS에 연결하기.bat", readme_ko)
            self.assertIn("ChatGPT 보안 Tunnel에 연결하기.bat", readme_ko)
            self.assertIn("Claude HTTPS에 연결하기.bat", readme_ko)
            self.assertIn("연결 상태 확인하기.bat", readme_ko)
            self.assertIn("Settings > MCP servers > Add server", readme_ko)
            self.assertIn("Codex CLI와 Claude Code", readme_ko)
            self.assertNotIn("For ChatGPT Desktop, Codex, and Claude Code", readme)
            self.assertNotIn("ChatGPT Desktop, Codex, Claude Code는 다음 순서", readme_ko)
            self.assertNotIn("local Work/Codex workspace", readme)
            self.assertNotIn("config.toml shared by ChatGPT Desktop and Codex", readme)
            self.assertIn("get_index_status", readme_ko)
            self.assertIn("-ValidateClaudeDesktop", readme_ko)
            self.assertIn("--codex-config", readme_ko)
            self.assertIn("--claude-desktop-config", readme_ko)
            self.assertIn("Get-Content -Encoding UTF8", readme_ko)
            self.assertIn("승인 저널과 벡터 ID", readme_ko)
            self.assertIn("install_local_package.ps1", readme_ko)
            self.assertIn("wheel", readme_ko)
            self.assertIn("pyproject.toml", readme_ko)
            self.assertIn("https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta", readme_ko)
            self.assertIn("https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector", readme_ko)
            self.assertIn("ChatGPT", readme_ko)
            self.assertIn("| Codex CLI | local_stdio | true | `CODEX_AGENT_CONNECT_PROMPT.md` |", readme)
            self.assertIn("| Claude Desktop | local_stdio | true | `Claude Desktop에 연결하기.bat` |", readme)
            self.assertIn("| Claude Code | local_stdio | true | `CLAUDE_CODE_AGENT_CONNECT_PROMPT.md` |", readme)
            self.assertIn("| ChatGPT 웹 | secure_mcp_tunnel | manual_setup_required | `ChatGPT 보안 Tunnel에 연결하기.bat` |", readme)
            self.assertIn("| ChatGPT Desktop | local_stdio | true | `CHATGPT_DESKTOP_CONNECT_GUIDE.md` |", readme)
            chatgpt_desktop_local = json.loads((output_dir / "chatgpt_desktop_local_mcp.json").read_text(encoding="utf-8"))
            self.assertEqual("chatgpt-desktop-local", chatgpt_desktop_local["profile"])
            self.assertEqual("ChatGPT Desktop", chatgpt_desktop_local["client"])
            self.assertTrue(chatgpt_desktop_local["chatgpt_direct_local_mcp_supported"])
            self.assertEqual("chatgpt_desktop_settings_mcp_servers", chatgpt_desktop_local["primary_registration"])
            self.assertEqual("chatgpt_desktop_mcp_settings", chatgpt_desktop_local["surface"])
            self.assertTrue(chatgpt_desktop_local["conversation_attachment_unverified"])
            self.assertFalse(chatgpt_desktop_local["plugin_install_command_succeeded"])
            self.assertFalse(chatgpt_desktop_local["plugin_manifest_validated"])
            self.assertFalse(chatgpt_desktop_local["plugin_discoverable"])
            self.assertFalse(chatgpt_desktop_local["plugin_loader_verified"])
            self.assertFalse(chatgpt_desktop_local["desktop_tool_scan_verified"])
            self.assertFalse(chatgpt_desktop_local["conversation_attachment_verified"])
            self.assertEqual("local_stdio", chatgpt_desktop_local["mode"])
            self.assertEqual("powershell.exe", chatgpt_desktop_local["ui_fields"]["command"])
            self.assertEqual(str(output_dir.resolve()), chatgpt_desktop_local["ui_fields"]["cwd"])
            self.assertIn(
                str((output_dir / "run_mcp_stdio_server.ps1").resolve()),
                chatgpt_desktop_local["ui_fields"]["args"],
            )
            self.assertIn(str((output_dir / "data").resolve()), chatgpt_desktop_local["ui_fields"]["args"])
            chatgpt_desktop_bat = (output_dir / "ChatGPT Desktop에 연결하기.bat").read_text(encoding="utf-8")
            self.assertIn("-InstallPackage -Target chatgpt-desktop-direct", chatgpt_desktop_bat)
            self.assertNotIn("-Target codex", chatgpt_desktop_bat)
            self.assertNotIn("-InstallChatGptDesktopPlugin", chatgpt_desktop_bat)
            self.assertIn("/mcp를 입력해 govreg-local이 연결됨으로 보이는지", chatgpt_desktop_bat)
            self.assertIn("[다음 단계]", chatgpt_desktop_bat)
            self.assertIn("govreg-local MCP의 get_index_status를 실행하고", chatgpt_desktop_bat)
            self.assertIn("govreg-local MCP의 get_index_status를 실행하고", codex_bat)
            self.assertIn("govreg-local MCP의 get_index_status를 실행하고", claude_code_bat)
            usage_guide = (output_dir / "MCP 사용 시작하기.txt").read_text(encoding="utf-8")
            self.assertIn("등록된 MCP 이름: govreg-local", usage_guide)
            self.assertIn("ChatGPT Desktop은 GUIDE 값을", usage_guide)
            self.assertIn("Claude Code와 Codex CLI는", usage_guide)
            self.assertIn("로컬 full 프로필의 설치 후 도구 확인", usage_guide)
            self.assertIn("원격 ChatGPT/보안 Tunnel/Claude API의 chatgpt-data 프로필 확인", usage_guide)
            self.assertIn("search 도구로 인사규정을 찾고", usage_guide)
            self.assertIn("`@` 멘션은 연결 확인 수단이 아닙니다", usage_guide)
            self.assertIn("Claude Desktop에는 위 에이전트 프롬프트 및 /mcp 공통 절차를 적용하지 않음", usage_guide)
            self.assertIn("codex mcp list", usage_guide)
            self.assertIn("claude mcp list", usage_guide)
            self.assertIn("같은 MCP 업데이트", usage_guide)
            for remote_launcher_name in (
                "ChatGPT HTTPS에 연결하기.bat",
                "ChatGPT 보안 Tunnel에 연결하기.bat",
                "Claude HTTPS에 연결하기.bat",
            ):
                remote_launcher = (output_dir / remote_launcher_name).read_text(encoding="utf-8")
                self.assertIn("search", remote_launcher, remote_launcher_name)
                self.assertIn("fetch", remote_launcher, remote_launcher_name)
                self.assertNotIn("등록된 규정 목록을 보여줘", remote_launcher, remote_launcher_name)
            plugin_guide = (output_dir / "Codex 플러그인 MCP 입력값.txt").read_text(encoding="utf-8")
            self.assertIn("MCP 이름: govreg-local", plugin_guide)
            self.assertIn("실행 명령: powershell.exe", plugin_guide)
            self.assertIn("작업 중인 디렉터리: <BUNDLE_DIR>", plugin_guide)
            self.assertIn("<BUNDLE_DIR>\\run_mcp_stdio_server.ps1", plugin_guide)
            self.assertNotIn(str(output_dir.resolve()), plugin_guide)
            self.assertNotIn("기본 방법: Codex에 연결하기.bat", plugin_guide)
            for generated_doc in [*output_dir.glob("*.md"), *output_dir.glob("*.txt")]:
                self.assertNotIn(
                    str(output_dir.resolve()),
                    generated_doc.read_text(encoding="utf-8"),
                    generated_doc.name,
                )

            self.assertIn('start "" notepad.exe "%~dp0MCP 사용 시작하기.txt"', (output_dir / "설치 후 MCP 사용 방법 보기.bat").read_text(encoding="utf-8"))
            self.assertIn(
                'Assert-EnvVar "MCP_AUTH_TOKEN"',
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "$script:McpPreferredPython",
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "function reg-rag-mcp-server",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertNotIn("<strong-token>", (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"))
            self.assertIn(
                '$ErrorActionPreference = "Stop"',
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Resolve-BundleModulePython "scripts.run_regulation_mcp"',
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "'--client-profile', 'bundle'",
                (output_dir / "run_http_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "'--tool-profile'",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "'chatgpt-data'",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "'--client-profile', 'chatgpt-remote'",
                (output_dir / "run_chatgpt_data_server.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '$ErrorActionPreference = "Stop"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-Command "tunnel-client"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-EnvVar "CONTROL_PLANE_API_KEY"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'Assert-EnvVar "OPENAI_TUNNEL_ID"',
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "<runtime-api-key>",
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "tunnel-client run",
                (output_dir / "run_openai_secure_tunnel.ps1").read_text(encoding="utf-8"),
            )
            self.assertIn("scripts.check_mcp_connection_readiness", (output_dir / "doctor_mcp_connection.ps1").read_text(encoding="utf-8"))
            self.assertIn("'--bundle-dir'", (output_dir / "doctor_mcp_connection.ps1").read_text(encoding="utf-8"))
            wizard = (output_dir / "connect_mcp_client.ps1").read_text(encoding="utf-8")
            self.assertIn(
                '[ValidateSet("menu", "install", "claude-desktop", "claude-code", "codex", "chatgpt-desktop-direct", "chatgpt-desktop-local", "chatgpt-remote"',
                wizard,
            )
            self.assertIn("function Show-ChatGptDesktop", wizard)
            self.assertIn("Open a new ChatGPT conversation, then select + > More > $ServerName.", wizard)
            self.assertIn('Start-Process "https://chatgpt.com/plugins"', wizard)
            self.assertNotIn("After creating the app, fully restart ChatGPT Desktop", wizard)
            self.assertNotIn("or mention @$ServerName", wizard)
            self.assertIn("function Write-Utf8NoBom", wizard)
            self.assertIn("function Read-StrictUtf8Json", wizard)
            self.assertIn('Invoke-CodexPluginCli @("plugin", "list", "--json")', wizard)
            self.assertIn(
                'Invoke-CodexPluginCli @("plugin", "marketplace", "remove", $PluginMarketplaceName, "--json")',
                wizard,
            )
            self.assertIn("Plugin discovery returned stale version", wizard)
            self.assertIn("Plugin discovery returned a stale marketplace source", wizard)
            self.assertIn("ChatGPT Secure MCP Tunnel", wizard)
            self.assertIn(
                "Verification prompt: $ServerName MCP의 search 도구로 인사규정을 찾고, 반환된 첫 번째 id를 fetch 도구로 조회해 조문 원문과 출처를 보여줘.",
                wizard,
            )
            self.assertNotIn(
                "Verification prompt: $ServerName MCP의 연결 상태와 사용 가능한 규정 도구를 보여줘.",
                wizard,
            )
            self.assertIn("Claude API MCP server URL", wizard)
            self.assertIn("Get-ClaudeDesktopConfigPath", wizard)
            self.assertIn("Get-CodexConfigPath", wizard)
            self.assertIn("[switch]$ValidateClaudeDesktop", wizard)
            self.assertIn("[switch]$InstallCodex", wizard)
            self.assertIn("Test-ClaudeDesktopConfig", wizard)
            self.assertIn("Install-CodexConfig", wizard)
            self.assertIn("Build-CodexConfigSnippet", wizard)
            self.assertIn("Get-BundleServerEntry", wizard)
            self.assertIn("function Read-ClaudeDesktopBundleServerConfig", wizard)
            self.assertIn("function Read-ChatGptDesktopBundleServerConfig", wizard)
            self.assertIn("$Source = Read-ChatGptDesktopBundleServerConfig", wizard)
            self.assertIn("$Source = Read-ClaudeDesktopBundleServerConfig", wizard)
            self.assertNotIn("function Read-BundleServerConfig", wizard)
            self.assertIn("Claude Desktop config is not valid JSON", wizard)
            self.assertIn("pasting the whole generated JSON as a second top-level object", wizard)
            self.assertIn("Claude Code CLI was not found", wizard)
            self.assertIn("reg-rag-mcp-index-visibility", wizard)
            self.assertIn("Get-McpCommandInvocation", wizard)
            self.assertIn("Invoke-McpCommand", wizard)
            self.assertIn("Get-RecordedRuntimePython", wizard)
            self.assertIn('return @($RecordedPython, "-m", $ModuleName)', wizard)
            self.assertIn(str(sys.executable), wizard)
            self.assertIn(str(Path(__file__).resolve().parents[1]), wizard)
            self.assertIn("Run-LocalStdioDoctor", wizard)
            self.assertIn("Run-InstalledClaudeDesktopConfigSmoke", wizard)
            self.assertIn("installed_pending_claude_desktop_verification", wizard)
            self.assertLess(wizard.index("$env:CODEX_HOME"), wizard.index("$env:USERPROFILE"))
            self.assertIn('throw "Local MCP doctor failed; $ConsumerName configuration was not changed."', wizard)
            self.assertIn("$LocalStdioDoctorArgs", wizard)
            self.assertIn("--allow-local-only-bundle", wizard)
            self.assertIn("--transport', 'stdio", wizard)
            self.assertIn('[string]$CodexConfigPath = ""', wizard)
            self.assertIn("$LegacyDefaultForSameProfile", wizard)
            self.assertIn("Removed duplicate entries for this bundle", wizard)
            self.assertIn("$ConsumerName MCP config verification failed after writing", wizard)
            codex_bat = (output_dir / "Codex에 연결하기.bat").read_text(encoding="utf-8")
            claude_desktop_bat = (output_dir / "Claude Desktop에 연결하기.bat").read_text(encoding="utf-8")
            claude_code_bat = (output_dir / "Claude Code에 연결하기.bat").read_text(encoding="utf-8")
            doctor_bat = (output_dir / "연결 상태 확인하기.bat").read_text(encoding="utf-8")
            for launcher in [codex_bat, claude_desktop_bat, claude_code_bat, doctor_bat]:
                self.assertIn("@echo off", launcher)
                self.assertIn("chcp 65001 >nul", launcher)
                self.assertIn('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0', launcher)
                self.assertIn("pause", launcher)
            self.assertIn("-Target doctor", doctor_bat)
            self.assertIn('connect_mcp_client.ps1" -InstallPackage -Target codex -InstallCodex', codex_bat)
            self.assertIn(
                'connect_mcp_client.ps1" -InstallPackage -Target claude-desktop -InstallClaudeDesktop',
                claude_desktop_bat,
            )
            self.assertIn('connect_mcp_client.ps1" -InstallPackage -Target claude-code', claude_code_bat)
            self.assertIn('connect_mcp_client.ps1" -Target doctor', doctor_bat)
            self.assertIn(
                "https://mcp.example.go.kr/mcp",
                (output_dir / "chatgpt_connector.json").read_text(encoding="utf-8"),
            )
            for script_name in [
                "claude_code_add_stdio.ps1",
                "run_local_stdio_server.ps1",
                "run_http_server.ps1",
                "run_chatgpt_data_server.ps1",
                "run_openai_secure_tunnel.ps1",
                "doctor_mcp_connection.ps1",
            ]:
                script = (output_dir / script_name).read_text(encoding="utf-8")
                self.assertIn('$BundleDataDir = Join-Path $BundleDir "data"', script)
                self.assertIn("--data-dir", script)
                self.assertGreaterEqual(script.count("$BundleDataDir"), 2)
            claude_code_stdio = (output_dir / "claude_code_add_stdio.ps1").read_text(encoding="utf-8")
            self.assertIn("$ClaudeCodeArgs = @(", claude_code_stdio)
            self.assertIn('$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"', claude_code_stdio)
            self.assertIn('"--", "powershell.exe") + $LauncherArgs', claude_code_stdio)
            self.assertIn('$ClaudeAdd = Invoke-ClaudeMcpCli $ClaudeAddArgs', claude_code_stdio)
            self.assertIn("--no-warm-cache", claude_code_stdio)
            self.assertIn(
                "--no-warm-cache",
                (output_dir / "run_local_stdio_server.ps1").read_text(encoding="utf-8"),
            )
            validate_script = (output_dir / "validate_mcp_smoke.ps1").read_text(encoding="utf-8")
            self.assertIn("reg-rag-mcp-transport-smoke", validate_script)
            self.assertIn('Resolve-BundleModulePython "scripts.run_mcp_transport_smoke"', validate_script)
            self.assertIn(
                'Invoke-BundlePythonModule $McpPython "scripts.run_mcp_transport_smoke" $SmokeArgs',
                validate_script,
            )
            self.assertNotIn('& reg-rag-mcp-transport-smoke @SmokeArgs', validate_script)
            self.assertIn("mcp_runtime_manifest.json", validate_script)
            self.assertIn("recommended_smoke_query", validate_script)
            self.assertIn("-Encoding UTF8", validate_script)
            self.assertIn("--no-warm-cache", validate_script)
            client_config_smoke_script = (output_dir / "validate_client_config_smoke.ps1").read_text(encoding="utf-8")
            self.assertIn("reg-rag-mcp-client-config-smoke", client_config_smoke_script)
            self.assertIn('Resolve-BundleModulePython "scripts.run_mcp_client_config_smoke"', client_config_smoke_script)
            self.assertIn(
                'Invoke-BundlePythonModule $McpPython "scripts.run_mcp_client_config_smoke" $SmokeArgs',
                client_config_smoke_script,
            )
            self.assertNotIn('& reg-rag-mcp-client-config-smoke @SmokeArgs', client_config_smoke_script)
            self.assertIn("mcp_client_config_smoke.json", client_config_smoke_script)
            self.assertIn("--codex-config", client_config_smoke_script)
            self.assertIn("--claude-desktop-config", client_config_smoke_script)
            self.assertIn("Set-McpBundlePaths", client_config_smoke_script)
            self.assertIn("Write-CodexBundleConfig", client_config_smoke_script)
            self.assertIn("Update-PluginBundleConfig", client_config_smoke_script)
            self.assertIn("Write-CodexBundleConfig $PluginArgs", client_config_smoke_script)
            self.assertNotIn("Write-CodexBundleConfig $ClaudeDesktopArgs", client_config_smoke_script)
            self.assertNotIn("plugin and Claude Desktop MCP args diverged", client_config_smoke_script)
            self.assertIn("Write-JsonUtf8NoBom $PluginMcpConfig", client_config_smoke_script)
            self.assertIn('$StdioLauncher = Join-Path $BundleDir "run_mcp_stdio_server.ps1"', client_config_smoke_script)
            self.assertIn("Set-McpBundlePaths", wizard)
            self.assertIn(
                '$Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")',
                wizard,
            )
            self.assertIn("-Encoding UTF8 | ConvertFrom-Json", wizard)
            self.assertEqual("mcp_bundle_status", bundle_status["report_type"])
            self.assertFalse(bundle_status["runtime_data_ready"])
            self.assertEqual(0, bundle_status["record_count"])
            self.assertEqual("validate_client_config_smoke.ps1", bundle_status["first_use"]["client_config_smoke_script"])
            self.assertIn("mcp_connection_readiness.json", bundle_status["stale_status_reports_cleared_on_generation"])
            self.assertFalse(bundle_status["claude_desktop_config_registered"])
            self.assertFalse(bundle_status["claude_desktop_config_transport_verified"])
            self.assertFalse(bundle_status["claude_desktop_loader_verified"])
            self.assertFalse(bundle_status["claude_desktop_conversation_verified"])

    def test_server_name_rejects_script_injection_and_ambiguous_toml_keys(self) -> None:
        invalid_names = [
            "",
            "Uppercase",
            "with.dot",
            'bad\"; Write-Output injected; #',
            "line\nbreak",
            "a" * 65,
        ]
        for server_name in invalid_names:
            with self.subTest(server_name=server_name):
                with self.assertRaisesRegex(ValueError, "server_name"):
                    build_mcp_client_config(server_name=server_name, client_profile="bundle")

        safe_config = build_mcp_client_config(server_name="safe-name", client_profile="bundle")
        with tempfile.TemporaryDirectory() as tmp:
            unsafe_out = Path(tmp) / "unsafe-output"
            with self.assertRaisesRegex(ValueError, "server_name"):
                write_mcp_setup_bundle(
                    safe_config,
                    unsafe_out,
                    server_name='bad\"; Write-Output injected; #',
                )
            self.assertFalse(unsafe_out.exists())

    @unittest.skipUnless(os.name == "nt", "Codex Windows installer script test")
    def test_installed_codex_smoke_rejects_misattributed_fresh_reports(self) -> None:
        self._assert_installed_smoke_report_guards(
            function_name="Run-InstalledCodexConfigSmoke",
            expected_label="codex",
        )

    @unittest.skipUnless(os.name == "nt", "Codex Windows installer script test")
    def test_installed_plugin_smoke_rejects_misattributed_fresh_reports(self) -> None:
        self._assert_installed_smoke_report_guards(
            function_name="Run-InstalledPluginConfigSmoke",
            expected_label="chatgpt_desktop_local",
        )

    @unittest.skipUnless(os.name == "nt", "Claude Desktop Windows installer script test")
    def test_installed_claude_desktop_smoke_rejects_misattributed_fresh_reports(self) -> None:
        self._assert_installed_smoke_report_guards(
            function_name="Run-InstalledClaudeDesktopConfigSmoke",
            expected_label="claude_desktop",
        )

    def _assert_installed_smoke_report_guards(
        self,
        *,
        function_name: str,
        expected_label: str,
    ) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not available.")

        config = build_mcp_client_config(
            server_name="expected-report-server",
            client_profile="bundle",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "report guard bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="expected-report-server",
            )
            connect_script = Path(files["connect"]).read_text(encoding="utf-8-sig")
            function_start = connect_script.index(f"function {function_name}")
            function_end = connect_script.find("\nfunction ", function_start + 1)
            self.assertGreater(function_end, function_start)
            function_text = connect_script[function_start:function_end]
            expected_config = str((root / "installed config" / "mcp-config.json").resolve())
            baseline = {
                "report_type": "mcp_client_config_smoke",
                "generated_at": "__NOW__",
                "server_name": "expected-report-server",
                "passed": True,
                "launcher_ready": True,
                "process_started": True,
                "mcp_initialized": True,
                "tools_discovered": True,
                "end_to_end_verified": True,
                "results": [
                    {
                        "label": expected_label,
                        "config_path": expected_config,
                        "passed": True,
                        "contract_verified": True,
                    }
                ],
            }
            invalid_cases = (
                "wrong_report_type",
                "top_level_not_passed",
                "wrong_server_name",
                "wrong_result_label",
                "wrong_result_config_path",
                "wrong_result_count",
                "stale_generated_at",
            )
            unexpectedly_accepted: list[str] = []
            for case_name in invalid_cases:
                payload = json.loads(json.dumps(baseline))
                if case_name == "wrong_report_type":
                    payload["report_type"] = "unrelated_report"
                elif case_name == "top_level_not_passed":
                    payload["passed"] = False
                elif case_name == "wrong_server_name":
                    payload["server_name"] = "different-server"
                elif case_name == "wrong_result_label":
                    payload["results"][0]["label"] = "different-client"
                elif case_name == "wrong_result_config_path":
                    payload["results"][0]["config_path"] = str(root / "different-config.json")
                elif case_name == "wrong_result_count":
                    payload["results"].append(dict(payload["results"][0]))
                elif case_name == "stale_generated_at":
                    payload["generated_at"] = "2020-01-01T00:00:00+00:00"

                payload_json = json.dumps(payload, ensure_ascii=False).replace("'", "''")
                expected_config_ps = expected_config.replace("'", "''")
                bundle_dir_ps = str(bundle_dir).replace("'", "''")
                harness = "\n".join(
                    [
                        '$ErrorActionPreference = "Stop"',
                        "$InstallationAttemptId = 'focused-red-attempt'",
                        "$ServerName = 'expected-report-server'",
                        f"$BundleDir = '{bundle_dir_ps}'",
                        "function BundlePath([string]$Name) { return Join-Path $BundleDir $Name }",
                        "function Read-JsonFile([string]$Name) {",
                        "  return [pscustomobject]@{ installation_attempt_id = $InstallationAttemptId; runtime_fingerprint = 'runtime-current' }",
                        "}",
                        "function Invoke-McpCommand([string]$Name, [object[]]$Arguments) {",
                        "  $OutIndex = [Array]::IndexOf($Arguments, '--out-json')",
                        "  if ($OutIndex -lt 0) { throw 'missing --out-json in focused RED harness' }",
                        f"  $Payload = '{payload_json}' | ConvertFrom-Json -ErrorAction Stop",
                        "  if ([string]$Payload.generated_at -eq '__NOW__') { $Payload.generated_at = [DateTimeOffset]::UtcNow.ToString('o') }",
                        "  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)",
                        "  [IO.File]::WriteAllText([string]$Arguments[$OutIndex + 1], (($Payload | ConvertTo-Json -Depth 30) + [Environment]::NewLine), $Utf8NoBom)",
                        "  return 0",
                        "}",
                        "function Update-BundleStatus([hashtable]$Changes) { }",
                        function_text,
                        f"$Accepted = {function_name} '{expected_config_ps}'",
                        "if ($Accepted) { Write-Output 'accepted'; exit 0 }",
                        "Write-Output 'rejected'; exit 23",
                    ]
                )
                harness_path = root / f"{function_name}-{case_name}.ps1"
                harness_path.write_text(harness + "\n", encoding="utf-8")
                completed = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(harness_path),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                self.assertIn(
                    completed.returncode,
                    {0, 23},
                    completed.stdout + completed.stderr,
                )
                if completed.returncode == 0:
                    unexpectedly_accepted.append(case_name)

        self.assertEqual(
            [],
            unexpectedly_accepted,
            (
                f"{function_name} must require a fresh mcp_client_config_smoke report for the "
                "requested server, exactly one expected client result, and the exact installed config path."
            ),
        )

    @unittest.skipUnless(os.name == "nt", "Codex Windows installer script test")
    def test_codex_installer_replaces_same_profile_legacy_default_and_verifies_paths(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
            profile_id="institution-test",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_dir = root / "AKS_MCP"
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-output-ok")\nraise SystemExit(0)\n', encoding="utf-8")
            fake_client_smoke = fake_project_root / "scripts" / "run_mcp_client_config_smoke.py"
            fake_client_smoke.write_text(
                _fake_client_config_smoke_source(),
                encoding="utf-8",
            )
            fake_app_server_check = fake_project_root / "scripts" / "check_codex_app_server_mcp.py"
            fake_app_server_check.write_text(
                "\n".join(
                    [
                        "import hashlib, json, os, sys",
                        "from datetime import datetime, timezone",
                        "from pathlib import Path",
                        "args = sys.argv[1:]",
                        "out = Path(args[args.index('--out-json') + 1])",
                        "config_path = str((Path(os.environ['CODEX_HOME']) / 'config.toml').resolve())",
                        "config_fingerprint = hashlib.sha256(os.path.normcase(config_path).encode('utf-8')).hexdigest()",
                        "config_content_fingerprint = 'sha256:' + hashlib.sha256(Path(config_path).read_bytes()).hexdigest()",
                        "payload = {",
                        "    'report_type': 'codex_app_server_mcp_status',",
                        "    'probe_scope': 'fresh_codex_app_server_process',",
                        "    'probe_id': 'fixture-fresh-probe',",
                        "    'generated_at': datetime.now(timezone.utc).isoformat(),",
                        "    'provenance': {",
                        "        'executable_path': sys.executable,",
                        "        'process_id': os.getpid(),",
                        "        'config_scope': {",
                        "            'config_exists': True,",
                        "            'config_path_sha256': config_fingerprint,",
                        "            'config_content_stable_during_probe': True,",
                        "            'config_content_sha256': config_content_fingerprint,",
                        "        },",
                        "    },",
                        "    'passed': True,",
                        "    'app_server_initialized': True,",
                        "    'status_list_received': True,",
                        "    'server_name': 'aks_mcp',",
                        "    'server_found': True,",
                        "    'tool_count': 3,",
                        "    'tool_names': ['fetch', 'get_index_status', 'search'],",
                        "    'server_info': {'name': 'regulation-mcp', 'version': 'test'},",
                        "    'error': None,",
                        "}",
                        "out.write_text(json.dumps(payload), encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aks_mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            moved_bundle_dir = root / "다른 위치" / "Renamed Bundle"
            moved_bundle_dir.parent.mkdir(parents=True)
            bundle_dir.rename(moved_bundle_dir)
            moved_connect_script = moved_bundle_dir / Path(files["connect"]).name
            codex_config = root / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True, exist_ok=True)
            codex_config.write_text(
                "\n".join(
                    [
                        "[mcp_servers.govreg-local]",
                        'command = "powershell.exe"',
                        "args = ['--profile-id', 'institution-test', 'C:\\old-bundle']",
                        "",
                        "[mcp_servers.aks_mcp]",
                        'command = "aksmcp"',
                        "",
                        "[mcp_servers.aks_mcp.env]",
                        'STALE = "must-be-removed"',
                        "",
                        "[mcp_servers.other]",
                        'command = "other-server"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            original_codex_config = codex_config.read_text(encoding="utf-8")
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            legacy_plugin_marker = root / "legacy-plugin.marker"
            legacy_plugin_marker.write_text("installed\n", encoding="utf-8")
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "if \"%1\"==\"--version\" echo codex-cli fixture&& exit /b 0\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  if exist \"%CODEX_PLUGIN_MARKER%\" (\r\n"
                "    echo {\"installed\":[{\"pluginId\":\"aks-mcp@aks-mcp-local\",\"installed\":true,\"enabled\":true}]}\r\n"
                "  ) else (\r\n"
                "    echo {\"installed\":[]}\r\n"
                "  )\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin remove\" (\r\n"
                "  del /q \"%CODEX_PLUGIN_MARKER%\" 2^>nul\r\n"
                "  echo {\"removed\":true}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin add\" (\r\n"
                "  type nul > \"%CODEX_PLUGIN_MARKER%\"\r\n"
                "  echo {\"added\":true}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace list\" (\r\n"
                "  echo {\"marketplaces\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" (\r\n"
                "  echo {\"name\":\"aks_mcp\",\"enabled\":%CODEX_EXPECTED_ENABLED%,\"startup_timeout_sec\":45,\"transport\":{\"type\":\"stdio\",\"command\":\"powershell.exe\",\"args\":[\"-NoProfile\",\"-ExecutionPolicy\",\"Bypass\",\"-File\",\"%CODEX_EXPECTED_LAUNCHER_JSON%\",\"--data-dir\",\"%CODEX_EXPECTED_DATA_JSON%\",\"--tenant-id\",\"tenant-a\",\"--transport\",\"stdio\",\"--profile-id\",\"institution-test\",\"--flat-storage\",\"--tool-profile\",\"%CODEX_EXPECTED_TOOL_PROFILE%\",\"--no-warm-cache\"],\"cwd\":\"%CODEX_EXPECTED_CWD_JSON%\"}}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 9\r\n",
                encoding="utf-8",
            )
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_exe.parent)]
            )
            env["CODEX_EXPECTED_LAUNCHER_JSON"] = json.dumps(
                str((moved_bundle_dir / "run_mcp_stdio_server.ps1").resolve())
            )[1:-1]
            env["CODEX_EXPECTED_DATA_JSON"] = json.dumps(str((moved_bundle_dir / "data").resolve()))[1:-1]
            env["CODEX_EXPECTED_CWD_JSON"] = json.dumps(str(moved_bundle_dir.resolve()))[1:-1]
            env["CODEX_EXPECTED_ENABLED"] = "false"
            env["CODEX_EXPECTED_TOOL_PROFILE"] = "full"
            env["CODEX_PLUGIN_MARKER"] = str(legacy_plugin_marker)
            env["CODEX_HOME"] = str(codex_config.parent)

            disabled = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(moved_connect_script),
                    "-Target",
                    "chatgpt-desktop-direct",
                    "-CodexConfigPath",
                    str(codex_config),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertNotEqual(0, disabled.returncode, disabled.stdout + disabled.stderr)
            self.assertIn("disabled, stale, or contract-mismatched", disabled.stdout + disabled.stderr)
            disabled_status = json.loads((moved_bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertFalse(disabled_status["direct_config_loader_verified"])
            self.assertFalse(disabled_status["direct_stdio_verified"])
            self.assertTrue(disabled_status["direct_config_rollback_performed"])
            self.assertTrue(
                disabled_status["legacy_plugin_restored_after_direct_failure"],
                disabled.stdout + disabled.stderr,
            )
            self.assertFalse(disabled_status["legacy_plugin_removed_for_direct_config"])
            self.assertTrue(legacy_plugin_marker.exists())
            self.assertEqual(original_codex_config, codex_config.read_text(encoding="utf-8"))

            env["CODEX_EXPECTED_ENABLED"] = "true"
            env["CODEX_EXPECTED_TOOL_PROFILE"] = "minimal"
            mismatched_args = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(moved_connect_script),
                    "-Target",
                    "codex",
                    "-InstallCodex",
                    "-CodexConfigPath",
                    str(codex_config),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertNotEqual(0, mismatched_args.returncode, mismatched_args.stdout + mismatched_args.stderr)
            self.assertIn("contract-mismatched", mismatched_args.stdout + mismatched_args.stderr)
            mismatched_status = json.loads((moved_bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertTrue(mismatched_status["direct_config_rollback_performed"])
            self.assertTrue(mismatched_status["legacy_plugin_restored_after_direct_failure"])
            self.assertTrue(legacy_plugin_marker.exists())
            self.assertEqual(original_codex_config, codex_config.read_text(encoding="utf-8"))

            env["CODEX_EXPECTED_TOOL_PROFILE"] = "full"

            completed = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(moved_connect_script),
                    "-Target",
                    "codex",
                    "-InstallCodex",
                    "-CodexConfigPath",
                    str(codex_config),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            installed = tomllib.loads(codex_config.read_text(encoding="utf-8-sig"))
            self.assertEqual({"aks_mcp", "other"}, set(installed["mcp_servers"]))
            self.assertEqual("other-server", installed["mcp_servers"]["other"]["command"])
            aks_entry = installed["mcp_servers"]["aks_mcp"]
            self.assertEqual("powershell.exe", aks_entry["command"])
            self.assertNotIn("env", aks_entry)
            self.assertIn(str((moved_bundle_dir / "run_mcp_stdio_server.ps1").resolve()), aks_entry["args"])
            self.assertIn(str((moved_bundle_dir / "data").resolve()), aks_entry["args"])
            self.assertNotIn(str(bundle_dir.resolve()), json.dumps(aks_entry, ensure_ascii=False))
            self.assertTrue(list(codex_config.parent.glob("config.toml.bak-*")))
            self.assertIn("doctor-output-ok", completed.stdout)
            self.assertIn("Verified MCP server name and bundle paths: aks_mcp", completed.stdout)
            self.assertIn("Direct MCP protocol initialize/tools smoke passed.", completed.stdout)
            self.assertIn("Codex app-server loaded aks_mcp with 3 tools.", completed.stdout)
            status = json.loads((moved_bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["direct_config_registered"])
            self.assertTrue(status["direct_config_loader_verified"])
            self.assertFalse(status["direct_config_rollback_performed"])
            self.assertTrue(status["direct_stdio_verified"])
            self.assertTrue(status["transport_end_to_end_verified"])
            self.assertTrue(status["desktop_app_server_loader_verified"])
            self.assertTrue(status["fresh_codex_app_server_inventory_verified"])
            self.assertTrue(status["legacy_plugin_conflict_detected"])
            self.assertTrue(status["legacy_plugin_removed_for_direct_config"])
            self.assertFalse(status["legacy_plugin_restored_after_direct_failure"])
            self.assertFalse(status["legacy_plugin_marketplace_removed"])
            self.assertFalse(legacy_plugin_marker.exists())
            self.assertEqual(3, status["desktop_app_server_tool_count"])
            self.assertEqual(["fetch", "get_index_status", "search"], status["desktop_app_server_tool_names"])
            _assert_same_existing_path(self, codex_config, status["direct_config_path"])

    @unittest.skipUnless(os.name == "nt", "Codex fresh app-server evidence is Windows-specific.")
    def test_codex_installer_rejects_stale_app_server_report_when_checker_writes_nothing(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            tenant_id="tenant-a",
            data_dir="data",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_scripts = fake_project_root / "scripts"
            fake_scripts.mkdir(parents=True)
            (fake_scripts / "check_mcp_connection_readiness.py").write_text(
                'print("stale-app-server-doctor-ok")\n',
                encoding="utf-8",
            )
            (fake_scripts / "run_mcp_client_config_smoke.py").write_text(
                _fake_client_config_smoke_source(),
                encoding="utf-8",
            )
            (fake_scripts / "check_codex_app_server_mcp.py").write_text(
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            bundle_dir = root / "stale-app-server-bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            stale_report_path = bundle_dir / "codex_app_server_mcp_status.json"
            stale_report_path.write_text(
                json.dumps(
                    {
                        "report_type": "codex_app_server_mcp_status",
                        "probe_scope": "fresh_codex_app_server_process",
                        "probe_id": "stale-positive-probe",
                        "generated_at": "2020-01-01T00:00:00+00:00",
                        "provenance": {
                            "executable_path": "C:/stale/codex.exe",
                            "process_id": 999,
                            "config_scope": {
                                "config_exists": True,
                                "config_path_sha256": "stale",
                            },
                        },
                        "passed": True,
                        "app_server_initialized": True,
                        "status_list_received": True,
                        "server_name": "aksmcp",
                        "server_found": True,
                        "tool_count": 3,
                        "tool_names": ["fetch", "get_index_status", "search"],
                    }
                ),
                encoding="utf-8",
            )

            generated_entry = json.loads(
                (bundle_dir / "claude_desktop_config.json").read_text(encoding="utf-8")
            )["mcpServers"]["aksmcp"]
            loader_payload = {
                "name": "aksmcp",
                "enabled": True,
                "startup_timeout_sec": 45,
                "transport": {
                    "type": "stdio",
                    "command": generated_entry["command"],
                    "args": generated_entry["args"],
                    "cwd": str(bundle_dir.resolve()),
                },
            }
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "if \"%1\"==\"--version\" echo codex-cli fixture&& exit /b 0\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  echo {\"installed\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" (\r\n"
                "  echo %CODEX_GET_JSON%\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            codex_config = root / ".codex" / "config.toml"
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CODEX_HOME"] = str(codex_config.parent)
            env["CODEX_GET_JSON"] = json.dumps(loader_payload, separators=(",", ":"))

            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "codex",
                    "-InstallCodex",
                    "-CodexConfigPath",
                    str(codex_config),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("Codex app-server did not initialize", completed.stdout + completed.stderr)
            self.assertFalse(stale_report_path.exists())
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["direct_config_registered"])
            self.assertTrue(status["direct_config_loader_verified"])
            self.assertTrue(status["direct_stdio_verified"])
            self.assertFalse(status["fresh_codex_app_server_inventory_verified"])
            self.assertFalse(status["desktop_app_server_loader_verified"])
            self.assertEqual("installed_loader_verified_pending_fresh_inventory", status["installation_state"])

    @unittest.skipUnless(os.name == "nt", "Desktop-only Codex config behavior is Windows-specific.")
    def test_chatgpt_desktop_keeps_verified_config_pending_when_codex_cli_is_absent(self) -> None:
        config = build_mcp_client_config(
            server_name="desktop-only-mcp",
            client_profile="bundle",
            tenant_id="tenant-a",
            data_dir="data",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            scripts_dir = fake_project_root / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "check_mcp_connection_readiness.py").write_text(
                'print("doctor-ok")\n',
                encoding="utf-8",
            )
            (scripts_dir / "run_mcp_client_config_smoke.py").write_text(
                _fake_client_config_smoke_source(),
                encoding="utf-8",
            )
            bundle_dir = root / "desktop only bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="desktop-only-mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir()
            plugin_config_path = (
                bundle_dir
                / "chatgpt-desktop-local-plugin"
                / "plugins"
                / "desktop-only-mcp"
                / ".mcp.json"
            )
            plugin_config = json.loads(plugin_config_path.read_text(encoding="utf-8"))
            plugin_config["mcpServers"]["desktop-only-mcp"]["args"].append("--chatgpt-source-marker")
            plugin_config_path.write_text(
                json.dumps(plugin_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            claude_bundle_config_path = bundle_dir / "claude_desktop_config.json"
            claude_bundle_config = json.loads(claude_bundle_config_path.read_text(encoding="utf-8"))
            claude_bundle_config["mcpServers"]["desktop-only-mcp"]["args"].append("--claude-source-marker")
            claude_bundle_config_path.write_text(
                json.dumps(claude_bundle_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            codex_config = root / "user config" / "config.toml"
            codex_config.parent.mkdir()
            codex_config.write_text(
                '[mcp_servers.other]\ncommand = "other-server"\n',
                encoding="utf-8",
            )

            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(windows_dir / "System32"), str(powershell_dir)]
            )
            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "codex",
                    "-InstallCodex",
                    "-CodexConfigPath",
                    str(codex_config),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("CONFIGURED - DESKTOP VERIFICATION REQUIRED", completed.stdout)
            installed = codex_config.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.other]", installed)
            self.assertIn("[mcp_servers.desktop-only-mcp]", installed)
            self.assertIn("--chatgpt-source-marker", installed)
            self.assertNotIn("--claude-source-marker", installed)
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertEqual("installed_pending_desktop_verification", status["installation_state"])
            self.assertTrue(status["direct_config_registered"])
            self.assertTrue(status["installed_config_transport_verified"])
            self.assertTrue(status["direct_stdio_verified"])
            self.assertFalse(status["direct_config_loader_verified"])
            self.assertEqual("blocked", status["loader_verification_state"])
            self.assertEqual("codex_cli_unavailable", status["loader_verification_reason"])
            self.assertFalse(status["desktop_tool_scan_verified"])
            self.assertFalse(status["conversation_attachment_verified"])
            self.assertFalse(status["end_to_end_verified"])

    @unittest.skipUnless(os.name == "nt", "Claude Desktop Windows installer script test")
    def test_claude_desktop_installer_replaces_legacy_profile_and_verifies_paths(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
            profile_id="institution-test",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("claude-doctor-ok")\n', encoding="utf-8")
            fake_client_smoke = fake_project_root / "scripts" / "run_mcp_client_config_smoke.py"
            fake_client_smoke.write_text(
                _fake_client_config_smoke_source("claude-desktop-installed-smoke-ok"),
                encoding="utf-8",
            )
            bundle_dir = root / "AKS_MCP"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aks_mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            claude_bundle_config_path = bundle_dir / "claude_desktop_config.json"
            claude_bundle_config = json.loads(claude_bundle_config_path.read_text(encoding="utf-8"))
            claude_bundle_config["mcpServers"]["aks_mcp"]["args"].append("--claude-source-marker")
            claude_bundle_config_path.write_text(
                json.dumps(claude_bundle_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            plugin_config_path = Path(files["chatgpt_desktop_plugin_mcp"])
            plugin_config = json.loads(plugin_config_path.read_text(encoding="utf-8"))
            plugin_config["mcpServers"]["aks_mcp"]["args"].append("--chatgpt-source-marker")
            plugin_config_path.write_text(
                json.dumps(plugin_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            appdata_dir = root / "AppData" / "Roaming"
            claude_config = appdata_dir / "Claude" / "claude_desktop_config.json"
            claude_config.parent.mkdir(parents=True)
            claude_config.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "mcpServers": {
                            "govreg-local": {
                                "command": "powershell.exe",
                                "args": ["--profile-id", "institution-test", r"C:\old-bundle"],
                            },
                            "aks_mcp": {"command": "aksmcp", "args": []},
                            "other": {"command": "other-server", "args": []},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            env["APPDATA"] = str(appdata_dir)
            env["PATH"] = os.pathsep.join(
                [str(windows_dir / "System32"), str(powershell_exe.parent)]
            )

            completed = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "claude-desktop",
                    "-InstallClaudeDesktop",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            installed = json.loads(claude_config.read_text(encoding="utf-8-sig"))
            self.assertEqual("dark", installed["theme"])
            self.assertEqual({"aks_mcp", "other"}, set(installed["mcpServers"]))
            aks_args = installed["mcpServers"]["aks_mcp"]["args"]
            self.assertIn(str((bundle_dir / "run_mcp_stdio_server.ps1").resolve()), aks_args)
            self.assertIn(str((bundle_dir / "data").resolve()), aks_args)
            self.assertIn("--claude-source-marker", aks_args)
            self.assertNotIn("--chatgpt-source-marker", aks_args)
            self.assertTrue(list(claude_config.parent.glob("claude_desktop_config.json.bak-*")))
            self.assertIn("claude-doctor-ok", completed.stdout)
            self.assertIn(
                "Removed duplicate Claude Desktop entries for this bundle: govreg-local",
                completed.stdout,
            )
            self.assertIn("Verified MCP server name and bundle paths: aks_mcp", completed.stdout)
            self.assertIn("Installed-config stdio verification passed", completed.stdout)
            self.assertIn("CLAUDE DESKTOP VERIFICATION REQUIRED", completed.stdout)
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertEqual("installed_pending_claude_desktop_verification", status["installation_state"])
            self.assertEqual("pending_claude_desktop_restart", status["connection_state"])
            self.assertTrue(status["claude_desktop_config_registered"])
            self.assertTrue(status["claude_desktop_config_transport_verified"])
            self.assertFalse(status["claude_desktop_loader_verified"])
            self.assertFalse(status["claude_desktop_conversation_verified"])

    @unittest.skipUnless(os.name == "nt", "Claude Code BAT automation test")
    def test_claude_code_bat_ignores_missing_legacy_entries_and_verifies_user_scope(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "가짜 프로젝트"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("claude-code-doctor-ok")\n', encoding="utf-8")
            bundle_dir = root / "한글 경로" / "Claude Code 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            claude_log = root / "claude-calls.txt"
            (fake_bin / "claude.cmd").write_text(
                "@echo off\r\n"
                "echo %*>>\"%CLAUDE_TEST_LOG%\"\r\n"
                "if \"%1 %2\"==\"mcp remove\" (\r\n"
                "  echo No MCP server named aksmcp in requested scope 1>&2\r\n"
                "  exit /b 1\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp add\" (\r\n"
                "  echo Added MCP server aksmcp with user scope\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" (\r\n"
                "  echo aksmcp: Scope: User Command: powershell.exe -File %CLAUDE_EXPECTED_LAUNCHER% --data-dir %CLAUDE_EXPECTED_DATA%\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 2\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CLAUDE_TEST_LOG"] = str(claude_log)
            claude_user_profile = root / "claude-user"
            claude_user_profile.mkdir()
            env["USERPROFILE"] = str(claude_user_profile)
            env["CLAUDE_EXPECTED_LAUNCHER"] = str((bundle_dir / "run_mcp_stdio_server.ps1").resolve())
            env["CLAUDE_EXPECTED_DATA"] = str((bundle_dir / "data").resolve())
            completed = subprocess.run(
                [
                    str(windows_dir / "System32" / "cmd.exe"),
                    "/d",
                    "/c",
                    files["connect_claude_code_bat"],
                ],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("claude-code-doctor-ok", completed.stdout)
            self.assertIn("wrong-scope, or wrong-path", Path(files["claude_code_stdio"]).read_text(encoding="utf-8-sig"))
            self.assertIn("Added MCP server aksmcp with user scope", completed.stdout)
            self.assertIn("aksmcp: Scope: User Command: powershell.exe", completed.stdout)
            calls = claude_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("mcp remove aksmcp --scope local" in call for call in calls))
            self.assertTrue(any("mcp remove aksmcp --scope user" in call for call in calls))
            self.assertTrue(
                any("mcp add --transport stdio --scope user aksmcp -- powershell.exe" in call for call in calls)
            )
            self.assertTrue(any("mcp get aksmcp" in call for call in calls))

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop BAT automation test")
    def test_chatgpt_desktop_bat_registers_plugin_idempotently_from_korean_space_path(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "가짜 프로젝트"
            scripts_dir = fake_project_root / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
            (scripts_dir / "check_mcp_connection_readiness.py").write_text(
                'print("bat-doctor-ok")\nraise SystemExit(0)\n',
                encoding="utf-8",
            )
            (scripts_dir / "run_mcp_client_config_smoke.py").write_text(
                _fake_client_config_smoke_source("bat-client-smoke-ok"),
                encoding="utf-8",
            )
            (scripts_dir / "check_codex_app_server_mcp.py").write_text(
                "\n".join(
                    [
                        "import hashlib, json, os, sys",
                        "from datetime import datetime, timezone",
                        "from pathlib import Path",
                        "args = sys.argv[1:]",
                        "out = Path(args[args.index('--out-json') + 1])",
                        "config_path = str((Path(os.environ['CODEX_HOME']) / 'config.toml').resolve())",
                        "config_fingerprint = hashlib.sha256(os.path.normcase(config_path).encode('utf-8')).hexdigest()",
                        "payload = {",
                        "    'report_type': 'codex_app_server_mcp_status',",
                        "    'probe_scope': 'fresh_codex_app_server_process',",
                        "    'probe_id': 'fixture-plugin-fresh-probe',",
                        "    'generated_at': datetime.now(timezone.utc).isoformat(),",
                        "    'provenance': {",
                        "        'executable_path': sys.executable,",
                        "        'process_id': os.getpid(),",
                        "        'config_scope': {",
                        "            'config_exists': True,",
                        "            'config_path_sha256': config_fingerprint,",
                        "        },",
                        "    },",
                        "    'passed': True,",
                        "    'app_server_initialized': True,",
                        "    'status_list_received': True,",
                        "    'server_name': 'aksmcp',",
                        "    'server_found': True,",
                        "    'tool_count': 3,",
                        "    'tool_names': ['fetch', 'get_index_status', 'search'],",
                        "    'server_info': {'name': 'regulation-mcp', 'version': 'test'},",
                        "    'error': None,",
                        "}",
                        "out.write_text(json.dumps(payload), encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            bundle_dir = root / "한글 경로" / "MCP 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            plugin_root = marketplace_root / "plugins" / "aksmcp"
            plugin_manifest = json.loads(
                (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            installed_cache_root = root / "codex-cache" / plugin_manifest["version"]
            installed_cache_root.mkdir(parents=True)
            shutil.copy2(plugin_root / ".mcp.json", installed_cache_root / ".mcp.json")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            codex_log = root / "codex-calls.txt"
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "if \"%1\"==\"--version\" echo codex-cli fixture&& exit /b 0\r\n"
                "echo %*>>\"%CODEX_TEST_LOG%\"\r\n"
                "if \"%1 %2\"==\"plugin remove\" (\r\n"
                "  if exist \"%CODEX_INSTALLED_MARKER%\" del /q \"%CODEX_INSTALLED_MARKER%\"\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace remove\" (\r\n"
                "  if exist \"%CODEX_INSTALLED_MARKER%\" del /f /q \"%CODEX_INSTALLED_MARKER%\"\r\n"
                "  echo Error: marketplace is not configured or installed 1>&2\r\n"
                "  exit /b 9\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin add\" if not exist \"%CODEX_RETRY_MARKER%\" (\r\n"
                "  type nul >\"%CODEX_RETRY_MARKER%\"\r\n"
                "  exit /b 9\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin add\" (\r\n"
                "  type nul >\"%CODEX_INSTALLED_MARKER%\"\r\n"
                "  echo {\"pluginId\":\"aksmcp@aksmcp-local\",\"name\":\"aksmcp\",\"marketplaceName\":\"aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installedPath\":\"%CODEX_EXPECTED_INSTALLED_PATH%\"}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  echo {\"installed\":[{\"pluginId\":\"aksmcp@aksmcp-local\",\"name\":\"aksmcp\",\"marketplaceName\":\"aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installed\":true,\"enabled\":true,\"source\":{\"source\":\"local\",\"path\":\"%CODEX_EXPECTED_PLUGIN_ROOT%\"},\"marketplaceSource\":{\"sourceType\":\"local\",\"source\":\"%CODEX_EXPECTED_MARKETPLACE_ROOT%\"}}],\"available\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" (\r\n"
                "  if not exist \"%CODEX_INSTALLED_MARKER%\" exit /b 9\r\n"
                "  echo {\"name\":\"aksmcp\",\"enabled\":true,\"transport\":{\"type\":\"stdio\",\"command\":\"powershell.exe\",\"args\":%CODEX_EXPECTED_ARGS_JSON%}}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CODEX_TEST_LOG"] = str(codex_log)
            env["CODEX_EXPECTED_VERSION"] = plugin_manifest["version"]
            env["CODEX_EXPECTED_PLUGIN_ROOT"] = plugin_root.as_posix()
            env["CODEX_EXPECTED_MARKETPLACE_ROOT"] = marketplace_root.as_posix()
            env["CODEX_EXPECTED_INSTALLED_PATH"] = installed_cache_root.as_posix()
            expected_plugin = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
            env["CODEX_EXPECTED_ARGS_JSON"] = json.dumps(
                expected_plugin["mcpServers"]["aksmcp"]["args"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            env["CODEX_EXPECTED_LAUNCHER_JSON"] = json.dumps(
                str((bundle_dir / "run_mcp_stdio_server.ps1").resolve())
            )[1:-1]
            env["CODEX_EXPECTED_DATA_JSON"] = json.dumps(str((bundle_dir / "data").resolve()))[1:-1]
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("# fixture\n", encoding="utf-8")
            env["CODEX_HOME"] = str(codex_home)
            env["CODEX_RETRY_MARKER"] = str(root / "codex-plugin-retried")
            env["CODEX_INSTALLED_MARKER"] = str(root / "codex-plugin-installed")
            powershell_exe = powershell_dir / "powershell.exe"
            connect_script = Path(files["connect"])

            completed_runs = []
            for _ in range(2):
                completed_runs.append(
                    subprocess.run(
                        [
                            str(powershell_exe),
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(connect_script),
                            "-Target",
                            "chatgpt-desktop-local",
                            "-InstallChatGptDesktopPlugin",
                        ],
                        cwd=root,
                        env=env,
                        input="\n",
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                )

            for completed in completed_runs:
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("bat-doctor-ok", completed.stdout)
                self.assertIn("bat-client-smoke-ok", completed.stdout)
            calls = codex_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(3, sum("plugin marketplace add" in call for call in calls))
            self.assertEqual(2, sum("plugin marketplace remove aksmcp-local" in call for call in calls))
            self.assertEqual(3, sum("plugin add aksmcp@aksmcp-local" in call for call in calls))
            self.assertEqual(4, sum("plugin list --json" in call for call in calls))
            self.assertEqual(4, sum("mcp get aksmcp --json" in call for call in calls))
            connect_script_source = Path(files["connect"]).read_text(encoding="utf-8")
            self.assertIn("System.Threading.Mutex", connect_script_source)
            self.assertIn("Local\\PRMCPBuilder-LocalMcpConnectionFlow", connect_script_source)
            self.assertLess(
                connect_script_source.index("Local\\PRMCPBuilder-LocalMcpConnectionFlow"),
                connect_script_source.index("if ($InstallPackage)"),
            )
            self.assertIn(
                "Invoke-WithLocalConnectionFlow {\n    Install-PackageIfRequested\n    Invoke-SelectedTarget\n  }",
                connect_script_source,
            )
            self.assertIn("if ($InstallPackage) {\n    Invoke-WithLocalConnectionFlow", connect_script_source)
            local_target_line = next(
                line for line in connect_script_source.splitlines() if line.startswith("$LocalConnectionTargets =")
            )
            self.assertNotIn('"menu"', local_target_line)
            self.assertNotIn('"chatgpt-tunnel"', local_target_line)
            status_path = bundle_dir / "bundle_status.json"
            self.assertFalse(status_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status["launcher_ready"])
            self.assertTrue(status["process_started"])
            self.assertTrue(status["mcp_initialized"])
            self.assertTrue(status["tools_discovered"])
            self.assertTrue(status["plugin_install_command_succeeded"])
            self.assertTrue(status["plugin_manifest_validated"])
            self.assertTrue(status["plugin_discoverable"])
            self.assertTrue(status["plugin_loader_verified"])
            self.assertTrue(status["plugin_registered"])
            self.assertFalse(status["direct_stdio_verified"])
            self.assertTrue(status["plugin_stdio_verified"])
            self.assertTrue(status["generated_client_configs_transport_verified"])
            self.assertTrue(status["fresh_codex_app_server_inventory_verified"])
            self.assertTrue(status["desktop_app_server_loader_verified"])
            self.assertFalse(status["desktop_tool_scan_verified"])
            self.assertFalse(status["conversation_attachment_verified"])
            self.assertTrue(status["conversation_attachment_unverified"])
            self.assertTrue(status["transport_end_to_end_verified"])
            self.assertFalse(status["end_to_end_verified"])
            plugin_config_path = bundle_dir / "chatgpt-desktop-local-plugin" / "plugins" / "aksmcp" / ".mcp.json"
            self.assertFalse(plugin_config_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            plugin_config = json.loads(plugin_config_path.read_text(encoding="utf-8"))
            plugin_args = plugin_config["mcpServers"]["aksmcp"]["args"]
            self.assertIn(str((bundle_dir / "run_mcp_stdio_server.ps1").resolve()), plugin_args)
            self.assertIn(str((bundle_dir / "data").resolve()), plugin_args)

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop fresh inventory failure test")
    def test_chatgpt_desktop_plugin_keeps_loader_verified_pending_when_fresh_inventory_fails(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            scripts_dir = fake_project_root / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
            (scripts_dir / "check_mcp_connection_readiness.py").write_text(
                'print("plugin-pending-doctor-ok")\nraise SystemExit(0)\n',
                encoding="utf-8",
            )
            (scripts_dir / "run_mcp_client_config_smoke.py").write_text(
                _fake_client_config_smoke_source(),
                encoding="utf-8",
            )
            (scripts_dir / "check_codex_app_server_mcp.py").write_text(
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            bundle_dir = root / "plugin-pending-bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            stale_report = bundle_dir / "codex_app_server_mcp_status.json"
            stale_report.write_text(
                json.dumps(
                    {
                        "report_type": "codex_app_server_mcp_status",
                        "probe_scope": "fresh_codex_app_server_process",
                        "probe_id": "stale-positive",
                        "generated_at": "2099-01-01T00:00:00+00:00",
                        "passed": True,
                        "app_server_initialized": True,
                        "status_list_received": True,
                        "server_name": "aksmcp",
                        "server_found": True,
                        "tool_names": ["fetch", "get_index_status", "search"],
                    }
                ),
                encoding="utf-8",
            )

            marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            plugin_root = marketplace_root / "plugins" / "aksmcp"
            plugin_manifest = json.loads(
                (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            installed_cache_root = root / "codex-cache" / plugin_manifest["version"]
            installed_cache_root.mkdir(parents=True)
            shutil.copy2(plugin_root / ".mcp.json", installed_cache_root / ".mcp.json")

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "if \"%1\"==\"--version\" echo codex-cli fixture&& exit /b 0\r\n"
                "if \"%1 %2\"==\"plugin remove\" (\r\n"
                "  if exist \"%CODEX_INSTALLED_MARKER%\" del /q \"%CODEX_INSTALLED_MARKER%\"\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace remove\" (\r\n"
                "  echo Error: marketplace is not configured or installed 1>&2\r\n"
                "  exit /b 9\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin add\" (\r\n"
                "  type nul >\"%CODEX_INSTALLED_MARKER%\"\r\n"
                "  echo {\"pluginId\":\"aksmcp@aksmcp-local\",\"name\":\"aksmcp\",\"marketplaceName\":\"aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installedPath\":\"%CODEX_EXPECTED_INSTALLED_PATH%\"}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  echo {\"installed\":[{\"pluginId\":\"aksmcp@aksmcp-local\",\"name\":\"aksmcp\",\"marketplaceName\":\"aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installed\":true,\"enabled\":true,\"source\":{\"source\":\"local\",\"path\":\"%CODEX_EXPECTED_PLUGIN_ROOT%\"},\"marketplaceSource\":{\"sourceType\":\"local\",\"source\":\"%CODEX_EXPECTED_MARKETPLACE_ROOT%\"}}],\"available\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" (\r\n"
                "  if not exist \"%CODEX_INSTALLED_MARKER%\" exit /b 9\r\n"
                "  echo {\"name\":\"aksmcp\",\"enabled\":true,\"transport\":{\"type\":\"stdio\",\"command\":\"powershell.exe\",\"args\":%CODEX_EXPECTED_ARGS_JSON%}}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )

            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            expected_plugin = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
            env["CODEX_EXPECTED_VERSION"] = plugin_manifest["version"]
            env["CODEX_EXPECTED_PLUGIN_ROOT"] = plugin_root.as_posix()
            env["CODEX_EXPECTED_MARKETPLACE_ROOT"] = marketplace_root.as_posix()
            env["CODEX_EXPECTED_INSTALLED_PATH"] = installed_cache_root.as_posix()
            env["CODEX_EXPECTED_ARGS_JSON"] = json.dumps(
                expected_plugin["mcpServers"]["aksmcp"]["args"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            env["CODEX_INSTALLED_MARKER"] = str(root / "codex-plugin-installed")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("# fixture\n", encoding="utf-8")
            env["CODEX_HOME"] = str(codex_home)

            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "chatgpt-desktop-local",
                    "-InstallChatGptDesktopPlugin",
                ],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertFalse(stale_report.exists())
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "plugin_installed_loader_verified_pending_fresh_inventory",
                status["installation_state"],
            )
            self.assertEqual("pending_fresh_loader_inventory", status["connection_state"])
            self.assertTrue(status["plugin_install_command_succeeded"])
            self.assertTrue(status["plugin_discoverable"])
            self.assertTrue(status["plugin_loader_verified"])
            self.assertTrue(status["plugin_registered"])
            self.assertTrue(status["plugin_stdio_verified"])
            self.assertFalse(status["fresh_codex_app_server_inventory_verified"])
            self.assertFalse(status["desktop_app_server_loader_verified"])
            self.assertFalse(status["desktop_tool_scan_verified"])
            self.assertFalse(status["conversation_attachment_verified"])
            self.assertFalse(status["end_to_end_verified"])

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop MCP name-conflict test")
    def test_chatgpt_desktop_installer_rejects_existing_direct_mcp_with_same_name(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-before-name-conflict")\n', encoding="utf-8")
            bundle_dir = root / "name-conflict-bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            codex_log = root / "conflict-codex-calls.txt"
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "echo %*>>\"%CODEX_TEST_LOG%\"\r\n"
                "if \"%1 %2\"==\"plugin list\" (echo {\"installed\":[],\"available\":[]}& exit /b 0)\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace list\" (echo {\"marketplaces\":[]}& exit /b 0)\r\n"
                "if \"%1 %2\"==\"mcp get\" (\r\n"
                "  echo {\"name\":\"aksmcp\",\"enabled\":true,\"transport\":{\"type\":\"stdio\",\"command\":\"other.exe\",\"args\":[]}}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CODEX_TEST_LOG"] = str(codex_log)
            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "chatgpt-desktop-local",
                    "-InstallChatGptDesktopPlugin",
                ],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("existing direct or unrelated MCP entry", completed.stdout + completed.stderr)
            calls = codex_log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("plugin add aksmcp@aksmcp-local" in call for call in calls))
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["plugin_name_conflict_detected"])
            self.assertFalse(status["plugin_install_command_succeeded"])
            self.assertFalse(status["plugin_loader_verified"])
            self.assertFalse(status["plugin_registered"])

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop restart-state PowerShell test")
    def test_chatgpt_desktop_restart_state_distinguishes_required_current_and_unknown(self) -> None:
        config = build_mcp_client_config(server_name="aksmcp", client_profile="bundle")
        wizard = config["quickstart"]["copy_paste"]["connect_wizard_ps"]
        start = wizard.index("function Get-ChatGptDesktopRestartState")
        end = wizard.index("\nfunction Get-ChatGptDesktopPluginRoot", start)
        function_source = wizard[start:end]
        probe = r'''
$Registration = [DateTimeOffset]::Parse("2026-07-20T09:00:00Z")
$Cases = [ordered]@{
  before = @([pscustomobject]@{ StartTime = [DateTimeOffset]::Parse("2026-07-20T08:00:00Z") })
  after = @([pscustomobject]@{ StartTime = [DateTimeOffset]::Parse("2026-07-20T10:00:00Z") })
  none = @()
  unknown = @([pscustomobject]@{ Name = "missing-start-time" })
}
$Result = [ordered]@{}
foreach ($Name in $Cases.Keys) {
  $Result[$Name] = Get-ChatGptDesktopRestartState -RegistrationUpdatedAtUtc $Registration -Processes $Cases[$Name]
}
$Result | ConvertTo-Json -Depth 6
'''
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "restart-state.ps1"
            script_path.write_text(function_source + "\n" + probe, encoding="utf-8-sig")
            windows_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            completed = subprocess.run(
                [str(powershell_exe), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("required", result["before"]["desktop_restart_status"])
        self.assertTrue(result["before"]["desktop_restart_required"])
        self.assertEqual("process_predates_mcp_registration", result["before"]["desktop_restart_reason_code"])
        self.assertEqual("up_to_date", result["after"]["desktop_restart_status"])
        self.assertFalse(result["after"]["desktop_restart_required"])
        self.assertEqual("process_started_after_mcp_registration", result["after"]["desktop_restart_reason_code"])
        self.assertEqual("not_running", result["none"]["desktop_restart_status"])
        self.assertEqual("unknown", result["unknown"]["desktop_restart_status"])
        self.assertIsNone(result["unknown"]["desktop_restart_required"])

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop stale discovery test")
    def test_chatgpt_desktop_installer_rejects_stale_discovered_plugin_version(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-before-stale-discovery")\n', encoding="utf-8")
            bundle_dir = root / "stale-plugin-bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            plugin_root = marketplace_root / "plugins" / "aksmcp"
            plugin_manifest = json.loads(
                (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            installed_cache_root = root / "stale-codex-cache" / plugin_manifest["version"]
            installed_cache_root.mkdir(parents=True)
            shutil.copy2(plugin_root / ".mcp.json", installed_cache_root / ".mcp.json")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "if \"%1 %2\"==\"mcp get\" exit /b 9\r\n"
                "if \"%1 %2\"==\"plugin add\" (\r\n"
                "  echo {\"pluginId\":\"aksmcp@aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installedPath\":\"%CODEX_EXPECTED_INSTALLED_PATH%\"}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  echo {\"installed\":[{\"pluginId\":\"aksmcp@aksmcp-local\",\"name\":\"aksmcp\",\"marketplaceName\":\"aksmcp-local\",\"version\":\"0.1.0\",\"installed\":true,\"enabled\":true,\"marketplaceSource\":{\"sourceType\":\"local\",\"source\":\"%CODEX_EXPECTED_MARKETPLACE_ROOT%\"}}],\"available\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CODEX_EXPECTED_MARKETPLACE_ROOT"] = marketplace_root.as_posix()
            env["CODEX_EXPECTED_VERSION"] = plugin_manifest["version"]
            env["CODEX_EXPECTED_INSTALLED_PATH"] = installed_cache_root.as_posix()
            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "chatgpt-desktop-local",
                    "-InstallChatGptDesktopPlugin",
                ],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("stale version 0.1.0", completed.stdout + completed.stderr)
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["plugin_install_command_succeeded"])
            self.assertFalse(status["plugin_discoverable"])
            self.assertFalse(status["plugin_loader_verified"])
            self.assertFalse(status["plugin_registered"])
            self.assertEqual("failed_rolled_back", status["installation_state"])
            self.assertTrue(status["plugin_rollback_performed"])
            self.assertTrue(status["plugin_rollback_complete"])

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop MCP loader verification test")
    def test_chatgpt_desktop_installer_rejects_plugin_missing_from_mcp_loader(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-before-loader-check")\n', encoding="utf-8")
            bundle_dir = root / "loader-missing-bundle"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            marketplace_root = bundle_dir / "chatgpt-desktop-local-plugin"
            plugin_manifest = json.loads(
                (
                    marketplace_root
                    / "plugins"
                    / "aksmcp"
                    / ".codex-plugin"
                    / "plugin.json"
                ).read_text(encoding="utf-8")
            )
            plugin_root = marketplace_root / "plugins" / "aksmcp"
            installed_cache_root = root / "loader-codex-cache" / plugin_manifest["version"]
            installed_cache_root.mkdir(parents=True)
            shutil.copy2(plugin_root / ".mcp.json", installed_cache_root / ".mcp.json")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "if \"%1 %2\"==\"plugin add\" (\r\n"
                "  echo {\"pluginId\":\"aksmcp@aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installedPath\":\"%CODEX_EXPECTED_INSTALLED_PATH%\"}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  echo {\"installed\":[{\"pluginId\":\"aksmcp@aksmcp-local\",\"version\":\"%CODEX_EXPECTED_VERSION%\",\"installed\":true,\"enabled\":true,\"marketplaceSource\":{\"sourceType\":\"local\",\"source\":\"%CODEX_EXPECTED_MARKETPLACE_ROOT%\"}}],\"available\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" exit /b 9\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(Path(sys.executable).parent), str(windows_dir / "System32"), str(powershell_dir)]
            )
            env["CODEX_EXPECTED_VERSION"] = plugin_manifest["version"]
            env["CODEX_EXPECTED_MARKETPLACE_ROOT"] = marketplace_root.as_posix()
            env["CODEX_EXPECTED_INSTALLED_PATH"] = installed_cache_root.as_posix()
            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "chatgpt-desktop-local",
                    "-InstallChatGptDesktopPlugin",
                ],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("codex mcp get could not resolve", completed.stdout + completed.stderr)
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["plugin_install_command_succeeded"])
            self.assertFalse(status["plugin_discoverable"])
            self.assertFalse(status["plugin_loader_verified"])
            self.assertFalse(status["plugin_registered"])
            self.assertEqual("failed_rolled_back", status["installation_state"])
            self.assertTrue(status["plugin_rollback_performed"])
            self.assertTrue(status["plugin_rollback_complete"])

    @unittest.skipUnless(os.name == "nt", "ChatGPT Desktop BAT failure-state test")
    def test_chatgpt_desktop_plugin_installer_does_not_mark_failed_registration_connected(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("doctor-before-plugin-failure")\n', encoding="utf-8")
            bundle_dir = root / "한글 실패 경로" / "MCP 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            status_path = bundle_dir / "bundle_status.json"
            previous_status = json.loads(status_path.read_text(encoding="utf-8"))
            previous_status.update(
                {
                    "installation_attempt_id": "previous-success-attempt",
                    "installation_state": "plugin_installed_loader_verified",
                    "process_started": True,
                    "mcp_initialized": True,
                    "tools_discovered": True,
                    "plugin_install_command_succeeded": True,
                    "plugin_discoverable": True,
                    "plugin_loader_verified": True,
                    "plugin_registered": True,
                    "plugin_stdio_verified": True,
                    "generated_client_configs_transport_verified": True,
                    "transport_end_to_end_verified": True,
                }
            )
            status_path.write_text(json.dumps(previous_status), encoding="utf-8")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "codex.cmd").write_text(
                '@echo off\r\nif "%1 %2"=="mcp get" exit /b 9\r\nif "%1 %2"=="plugin add" exit /b 9\r\nexit /b 0\r\n',
                encoding="utf-8",
            )
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["PATH"] = os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            )
            completed = subprocess.run(
                [
                    str(powershell_dir / "powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["connect"],
                    "-Target",
                    "chatgpt-desktop-local",
                    "-InstallChatGptDesktopPlugin",
                ],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            self.assertNotEqual("previous-success-attempt", status["installation_attempt_id"])
            self.assertEqual("failed_no_external_change", status["installation_state"])
            self.assertFalse(status["plugin_rollback_performed"])
            self.assertIsNone(status["plugin_rollback_complete"])
            self.assertTrue(status["plugin_manifest_validated"])
            self.assertFalse(status["plugin_install_command_succeeded"])
            self.assertFalse(status["plugin_discoverable"])
            self.assertFalse(status["plugin_loader_verified"])
            self.assertFalse(status["plugin_registered"])
            self.assertFalse(status["plugin_stdio_verified"])
            self.assertFalse(status["generated_client_configs_transport_verified"])
            self.assertFalse(status["transport_end_to_end_verified"])
            self.assertFalse(status["mcp_initialized"])
            self.assertFalse(status["tools_discovered"])
            self.assertFalse(status["end_to_end_verified"])
            self.assertTrue(status["conversation_attachment_unverified"])

    @unittest.skipUnless(os.name == "nt", "Claude Desktop damaged bundle JSON recovery test")
    def test_claude_desktop_bat_recovers_damaged_bundle_json_in_korean_path(self) -> None:
        config = build_mcp_client_config(
            server_name="aksmcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.parent.mkdir(parents=True)
            fake_doctor.write_text('print("claude-recovery-doctor-ok")\n', encoding="utf-8")
            (fake_project_root / "scripts" / "run_mcp_client_config_smoke.py").write_text(
                _fake_client_config_smoke_source("claude-recovery-installed-smoke-ok"),
                encoding="utf-8",
            )
            bundle_dir = root / "한글 경로" / "Claude MCP 번들"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aksmcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            (bundle_dir / "claude_desktop_config.json").write_text(
                r'{"mcpServers":{"aksmcp":{"command":"powershell.exe","args":["C:\bad\data"]}}}',
                encoding="utf-8",
            )
            appdata_dir = root / "사용자 AppData" / "Roaming"
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
            env["APPDATA"] = str(appdata_dir)
            env["PATH"] = os.pathsep.join(
                [str(windows_dir / "System32"), str(powershell_dir)]
            )
            completed = subprocess.run(
                [str(windows_dir / "System32" / "cmd.exe"), "/d", "/c", files["connect_claude_desktop_bat"]],
                cwd=root,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("recovering the MCP entry", completed.stdout)
            recovered_source = json.loads(
                (bundle_dir / "claude_desktop_config.json").read_text(encoding="utf-8-sig")
            )
            installed_path = appdata_dir / "Claude" / "claude_desktop_config.json"
            installed = json.loads(installed_path.read_text(encoding="utf-8-sig"))
            for payload in (recovered_source, installed):
                args = payload["mcpServers"]["aksmcp"]["args"]
                self.assertIn(str((bundle_dir / "run_mcp_stdio_server.ps1").resolve()), args)
                self.assertIn(str((bundle_dir / "data").resolve()), args)

    @unittest.skipUnless(os.name == "nt", "Windows stdio launcher fallback test")
    def test_stdio_launcher_uses_generated_project_runtime_without_path_command(self) -> None:
        config = build_mcp_client_config(
            server_name="aks_mcp",
            client_profile="bundle",
            data_dir="data",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_project_root = root / "project"
            fake_server = fake_project_root / "scripts" / "run_regulation_mcp.py"
            fake_server.parent.mkdir(parents=True)
            (fake_server.parent / "__init__.py").write_text("", encoding="utf-8")
            fake_server.write_text('print("stdio-fallback-ok")\n', encoding="utf-8")
            fake_doctor = fake_project_root / "scripts" / "check_mcp_connection_readiness.py"
            fake_doctor.write_text(
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "out = pathlib.Path(args[args.index('--out-json') + 1])\n"
                "out.write_text(json.dumps({'report_type':'mcp_connection_readiness','passed':True,'findings':[]}), encoding='utf-8')\n"
                "print('doctor-fallback-ok')\n",
                encoding="utf-8",
            )
            bundle_dir = root / "standalone" / "AKS_MCP"
            files = write_mcp_setup_bundle(
                config,
                bundle_dir,
                server_name="aks_mcp",
                preferred_python=sys.executable,
                preferred_project_root=fake_project_root,
            )
            (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            windows_dir = Path(env.get("SystemRoot", r"C:\Windows"))
            powershell_exe = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            env["PATH"] = os.pathsep.join([str(windows_dir / "System32"), str(powershell_exe.parent)])

            completed = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["stdio_launcher"],
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            completed_doctor = subprocess.run(
                [
                    str(powershell_exe),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    files["doctor"],
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("stdio-fallback-ok", completed.stdout)
        self.assertEqual(0, completed_doctor.returncode, completed_doctor.stdout + completed_doctor.stderr)
        self.assertIn("doctor-fallback-ok", completed_doctor.stdout)

    def test_setup_bundle_scripts_use_bundle_local_data_dir(self) -> None:
        stale_data_dir = r"C:\stale\mcp_connection_bundle\data"
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            data_dir=stale_data_dir,
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr",
        )

        with tempfile.TemporaryDirectory() as tmp:
            write_mcp_setup_bundle(
                config,
                tmp,
                server_name="govreg-local",
                preferred_python=sys.executable,
                preferred_project_root=Path(__file__).resolve().parents[1],
            )
            output_dir = Path(tmp)

            for script_name in [
                "claude_code_add_stdio.ps1",
                "run_local_stdio_server.ps1",
                "run_http_server.ps1",
                "run_chatgpt_data_server.ps1",
                "run_openai_secure_tunnel.ps1",
                "doctor_mcp_connection.ps1",
            ]:
                script = (output_dir / script_name).read_text(encoding="utf-8")
                self.assertIn('$BundleDataDir = Join-Path $BundleDir "data"', script)
                self.assertTrue(
                    "--data-dir $BundleDataDir" in script
                    or ("'--data-dir'" in script and "$BundleDataDir" in script),
                    script_name,
                )
                self.assertNotIn(stale_data_dir, script)

            claude_desktop = json.loads((output_dir / "claude_desktop_config.json").read_text(encoding="utf-8"))
            claude_args = claude_desktop["mcpServers"]["govreg-local"]["args"]
            stdio_launcher = (output_dir / "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")
            self.assertIn(str((output_dir / "data").resolve()), claude_args)
            self.assertNotIn(stale_data_dir, claude_args)
            bundle_config = json.loads((output_dir / "mcp_config.bundle.json").read_text(encoding="utf-8"))
            bundle_data_dir = str((output_dir / "data").resolve())
            claude_code_cli_args = bundle_config["quickstart"]["claude_code"]["args"]
            self.assertIn("--scope", claude_code_cli_args)
            self.assertIn("user", claude_code_cli_args)
            self.assertIn(bundle_data_dir, claude_code_cli_args)
            self.assertNotIn(stale_data_dir, bundle_config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"])
            self.assertIn(
                f"--data-dir {bundle_data_dir}",
                bundle_config["quickstart"]["openai_secure_tunnel"]["stdio_mcp_command"],
            )
            self.assertNotIn(stale_data_dir, json.dumps(bundle_config, ensure_ascii=False))
            for profile_name in ("chatgpt_remote", "chatgpt"):
                relocated_tunnel_script = bundle_config[profile_name]["openai_secure_tunnel"]["copy_paste_ps"]
                self.assertIn(
                    "$env:PRMCPBUILDER_TUNNEL_DATA_DIR = $BundleDataDir",
                    relocated_tunnel_script,
                )
                self.assertIn('$BundleDataDir = Join-Path $BundleDir "data"', relocated_tunnel_script)
                self.assertNotIn(stale_data_dir, relocated_tunnel_script)
                self.assertNotIn(str(output_dir), relocated_tunnel_script)
            for config_name in ("mcp_config.bundle.json", "chatgpt_connector.json"):
                config_text = (output_dir / config_name).read_text(encoding="utf-8")
                encoded_launchers = re.findall(
                    r"-EncodedCommand ([A-Za-z0-9+/=]+)",
                    config_text,
                )
                self.assertTrue(encoded_launchers, config_name)
                for encoded_launcher in encoded_launchers:
                    launcher_source = base64.b64decode(encoded_launcher).decode("utf-16-le")
                    self.assertIn("$env:PRMCPBUILDER_TUNNEL_DATA_DIR", launcher_source)
                    self.assertNotIn(stale_data_dir, launcher_source)
                    self.assertNotIn(str(output_dir), launcher_source)

            wizard = (output_dir / "connect_mcp_client.ps1").read_text(encoding="utf-8")
            self.assertIn("Get-BundleDataDir", wizard)
            self.assertIn("Set-McpBundlePaths", wizard)
            self.assertIn(
                '$Source = Set-McpBundlePaths $Source (Get-BundleDataDir) (BundlePath "run_mcp_stdio_server.ps1")',
                wizard,
            )

    def test_setup_bundle_normalizes_python_stdio_client_configs(self) -> None:
        stale_data_dir = r"C:\stale\mcp_connection_bundle\data"
        stale_script = r"C:\stale\source\scripts\run_regulation_mcp.py"
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            data_dir=stale_data_dir,
            tenant_id="tenant-a",
        )
        server = config["claude_desktop"]["mcpServers"]["govreg-local"]
        server["command"] = sys.executable
        server["args"] = [
            stale_script,
            "--data-dir",
            stale_data_dir,
            "--tenant-id",
            "tenant-a",
            "--transport",
            "stdio",
            "--flat-storage",
            "--no-warm-cache",
        ]
        config["claude_code"]["command"] = sys.executable
        config["claude_code"]["args"] = list(server["args"])

        with tempfile.TemporaryDirectory() as tmp:
            write_mcp_setup_bundle(
                config,
                tmp,
                server_name="govreg-local",
                preferred_python=sys.executable,
                preferred_project_root=Path(__file__).resolve().parents[1],
            )
            output_dir = Path(tmp)
            launcher = str((output_dir / "run_mcp_stdio_server.ps1").resolve())
            bundle_data_dir = str((output_dir / "data").resolve())
            codex_snippet = tomllib.loads((output_dir / "codex_config_snippet.toml").read_text(encoding="utf-8"))
            codex_args = codex_snippet["mcp_servers"]["govreg-local"]["args"]
            claude_desktop = json.loads((output_dir / "claude_desktop_config.json").read_text(encoding="utf-8"))
            claude_args = claude_desktop["mcpServers"]["govreg-local"]["args"]
            stdio_launcher = (output_dir / "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")

        for args in (codex_args, claude_args):
            self.assertEqual("-File", args[3])
            self.assertEqual(launcher, args[4])
            self.assertIn(bundle_data_dir, args)
            self.assertNotIn(stale_script, args)
            self.assertNotIn(stale_data_dir, args)
        self.assertNotIn(stale_script, stdio_launcher)
        self.assertIn(f"$PreferredPython = '{sys.executable}'", stdio_launcher)

    def test_setup_bundle_clears_stale_status_reports(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            stale_readiness = output_dir / "mcp_connection_readiness.json"
            stale_smoke = output_dir / "mcp_transport_smoke.json"
            stale_client_smoke = output_dir / "mcp_client_config_smoke.json"
            stale_remote_smoke = output_dir / "mcp_chatgpt_remote_smoke.json"
            stale_app_server = output_dir / "codex_app_server_mcp_status.json"
            stale_claude_desktop_smoke = output_dir / "claude_desktop_installed_mcp_config_smoke.json"
            stale_readiness.write_text('{"effective_data_dir":"C:/old"}\n', encoding="utf-8")
            stale_smoke.write_text('{"passed":false}\n', encoding="utf-8")
            stale_client_smoke.write_text('{"passed":false}\n', encoding="utf-8")
            stale_remote_smoke.write_text('{"passed":false}\n', encoding="utf-8")
            stale_app_server.write_text('{"passed":false}\n', encoding="utf-8")
            stale_claude_desktop_smoke.write_text('{"passed":false}\n', encoding="utf-8")

            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            self.assertFalse(stale_readiness.exists())
            self.assertFalse(stale_smoke.exists())
            self.assertFalse(stale_client_smoke.exists())
            self.assertFalse(stale_remote_smoke.exists())
            self.assertFalse(stale_app_server.exists())
            self.assertFalse(stale_claude_desktop_smoke.exists())
            status = json.loads((output_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["runtime_data_ready"])
            self.assertEqual(
                [
                    "mcp_connection_readiness.json",
                    "mcp_transport_smoke.json",
                    "mcp_client_config_smoke.json",
                    "mcp_chatgpt_remote_smoke.json",
                    "codex_app_server_mcp_status.json",
                    "claude_desktop_installed_mcp_config_smoke.json",
                ],
                status["stale_status_reports_cleared_on_generation"],
            )

    def test_zips_setup_bundle_for_operator_handoff(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
            public_url="https://mcp.example.go.kr",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")
            (output_dir / "operator_notes.tmp").write_text("do not ship", encoding="utf-8")
            (output_dir / ".venv" / "Scripts").mkdir(parents=True)
            (output_dir / ".venv" / "Scripts" / "python.exe").write_bytes(b"not-a-runtime")
            (output_dir / "__pycache__").mkdir()
            (output_dir / "__pycache__" / "module.pyc").write_bytes(b"cache")
            (output_dir / ".pytest_cache").mkdir()
            (output_dir / ".pytest_cache" / "README.md").write_text("cache", encoding="utf-8")
            (output_dir / "data" / ".venv" / "vector_db").mkdir(parents=True)
            (output_dir / "data" / ".venv" / "vector_db" / "approved_vectors.jsonl").write_text(
                "{}\n",
                encoding="utf-8",
            )
            zip_progress: list[tuple[int, int, str]] = []
            result = write_mcp_setup_bundle_zip(
                output_dir,
                zip_path,
                progress_callback=lambda current, total, name: zip_progress.append((current, total, name)),
            )

            self.assertEqual(str(zip_path), result)
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
                portable_docs = {
                    name: archive.read(name).decode("utf-8")
                    for name in names
                    if name.lower().endswith((".md", ".txt"))
                }
                portable_config_names = {
                    "bundle_status.json",
                    "chatgpt_connector.json",
                    "chatgpt_desktop_local_mcp.json",
                    "claude_api_fragment.json",
                    "claude_desktop_config.json",
                    "codex_config_snippet.toml",
                    "mcp_config.bundle.json",
                    "chatgpt-desktop-local-plugin/plugins/govreg-local/.mcp.json",
                }
                portable_configs = {
                    name: archive.read(name).decode("utf-8")
                    for name in portable_config_names
                }
                bundled_config = json.loads(portable_configs["mcp_config.bundle.json"])

            self.assertIn("connect_mcp_client.ps1", names)
            self.assertIn("Codex에 연결하기.bat", names)
            self.assertIn("ChatGPT Desktop에 연결하기.bat", names)
            self.assertIn("Claude Desktop에 연결하기.bat", names)
            self.assertIn("Claude Code에 연결하기.bat", names)
            self.assertIn("연결 상태 확인하기.bat", names)
            self.assertIn("install_local_package.ps1", names)
            self.assertIn("README.ko.md", names)
            self.assertIn("CHATGPT_DESKTOP_CONNECT_GUIDE.md", names)
            self.assertIn("CODEX_AGENT_CONNECT_PROMPT.md", names)
            self.assertIn("CLAUDE_CODE_AGENT_CONNECT_PROMPT.md", names)
            self.assertIn("codex_config_snippet.toml", names)
            self.assertIn("chatgpt_desktop_local_mcp.json", names)
            self.assertIn("chatgpt_connector.json", names)
            self.assertIn("claude_api_fragment.json", names)
            self.assertIn("run_openai_secure_tunnel.ps1", names)
            self.assertIn(
                "chatgpt-desktop-local-plugin/plugins/govreg-local/.codex-plugin/plugin.json",
                names,
            )
            self.assertIn(
                "chatgpt-desktop-local-plugin/plugins/govreg-local/.mcp.json",
                names,
            )
            self.assertIn(
                "chatgpt-desktop-local-plugin/.agents/plugins/marketplace.json",
                names,
            )
            self.assertNotIn("operator_notes.tmp", names)
            self.assertFalse(any(".venv" in name.split("/") for name in names))
            self.assertFalse(any("__pycache__" in name.split("/") for name in names))
            self.assertFalse(any(".pytest_cache" in name.split("/") for name in names))
            for name, content in portable_docs.items():
                self.assertNotIn(str(output_dir.resolve()), content, name)
            for name, content in portable_configs.items():
                self.assertNotIn(str(output_dir.resolve()), content, name)
                self.assertNotIn(str(output_dir.resolve()).replace("\\", "\\\\"), content, name)
            self.assertIn(
                "$BundleDataDir",
                bundled_config["quickstart"]["copy_paste"]["audit_index_visibility_ps"],
            )
            self.assertIn(
                "$BundleDataDir",
                bundled_config["quickstart"]["copy_paste"]["claude_code_stdio_ps"],
            )
            for name in (
                "claude_desktop_config.json",
                "codex_config_snippet.toml",
                "chatgpt_desktop_local_mcp.json",
                "chatgpt-desktop-local-plugin/plugins/govreg-local/.mcp.json",
            ):
                self.assertIn("<BUNDLE_DIR>", portable_configs[name], name)
            portable_codex_toml = portable_configs["codex_config_snippet.toml"]
            self.assertIn("forward-slash absolute path", portable_codex_toml)
            materialized_codex = tomllib.loads(
                portable_codex_toml.replace("<BUNDLE_DIR>", "C:/MCP/규정")
            )
            self.assertIn("govreg-local", materialized_codex["mcp_servers"])
            self.assertIn("C:/MCP/aksmcp2", portable_docs["README.md"])
            self.assertIn("슬래시(`/`)", portable_docs["README.ko.md"])
            self.assertTrue(zip_progress)
            self.assertEqual(zip_progress[-1][0], zip_progress[-1][1])
            self.assertEqual(sorted(item[0] for item in zip_progress), [item[0] for item in zip_progress])

    def test_zip_includes_bundled_runtime_data_directory(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / "approval_snapshot.json").write_text(
                json.dumps(
                    {
                        "report_type": "mcp_runtime_approval_snapshot",
                        "document_ids": ["doc-current"],
                        "entries": [{"document_id": "doc-current", "chunk_id": "chunk-1"}],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            write_mcp_setup_bundle_zip(output_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("data/vector_db/tenant-a/approved_vectors.jsonl", names)
            self.assertIn("data/repository/doc-current_chunks.json", names)
            self.assertIn("data/repository/approval_snapshot.json", names)

    def test_zip_rejects_cross_tenant_runtime_vector_store_files(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            tenant_a_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            tenant_b_vector_dir = output_dir / "data" / "vector_db" / "tenant-b"
            runtime_repository_dir.mkdir(parents=True)
            tenant_a_vector_dir.mkdir(parents=True)
            tenant_b_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"], tenant_id="tenant-a")
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            vector_record = json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n"
            (tenant_a_vector_dir / "approved_vectors.jsonl").write_text(vector_record, encoding="utf-8")
            (tenant_b_vector_dir / "approved_vectors.jsonl").write_text(vector_record, encoding="utf-8")
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("outside the manifest tenant", str(raised.exception))
        self.assertIn("tenant-b/approved_vectors.jsonl", str(raised.exception))

    def test_zip_rejects_runtime_data_without_manifest(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("missing a valid mcp_runtime_manifest.json", str(raised.exception))

    def test_zip_rejects_stale_runtime_document_artifacts(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / "doc-stale_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("stale document artifacts", str(raised.exception))

    def test_zip_excludes_runtime_locks_and_trace_logs(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / ".write.lock").write_text("", encoding="utf-8")
            (runtime_repository_dir / "api_audit.jsonl").write_text("{}\n", encoding="utf-8")
            (runtime_repository_dir / "rag_traces.jsonl").write_text("{}\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            write_mcp_setup_bundle_zip(output_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

        self.assertIn("data/repository/doc-current_chunks.json", names)
        self.assertNotIn("data/repository/.write.lock", names)
        self.assertNotIn("data/repository/api_audit.jsonl", names)
        self.assertNotIn("data/repository/rag_traces.jsonl", names)

    def test_zip_rejects_raw_runtime_repository_results_even_for_manifest_document(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = Path(tmp) / "bundle.zip"
            runtime_repository_dir = output_dir / "data" / "repository"
            runtime_vector_dir = output_dir / "data" / "vector_db" / "tenant-a"
            runtime_repository_dir.mkdir(parents=True)
            runtime_vector_dir.mkdir(parents=True)
            _write_runtime_data_manifest(output_dir / "data", ["doc-current"])
            _write_runtime_repository_manifest(runtime_repository_dir, ["doc-current"])
            (runtime_repository_dir / "doc-current_chunks.json").write_text("[]\n", encoding="utf-8")
            (runtime_repository_dir / "doc-current_nodes.json").write_text("[]\n", encoding="utf-8")
            (runtime_vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps({"document_id": "doc-current"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            with self.assertRaises(ValueError) as raised:
                write_mcp_setup_bundle_zip(output_dir, zip_path)

        self.assertIn("raw preprocessing artifacts", str(raised.exception))
        self.assertIn("doc-current_nodes.json", str(raised.exception))

    def test_runtime_bundle_requires_kordoc_table_parser_evidence_for_hwp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata={})
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn("requires Kordoc table parsing", str(raised.exception))
        self.assertIn("rerun preprocessing", str(raised.exception))

    def test_runtime_bundle_exports_only_selected_document_set(self) -> None:
        records = [
            _runtime_export_record(
                "doc-a",
                "chunk-a",
                metadata={"regulation_id": "reg-personnel", "hierarchy_path": "인사규정 > 제1조"},
            ),
            _runtime_export_record("doc-b", "chunk-b"),
            _runtime_export_record(
                "doc-c",
                "chunk-c",
                metadata={"regulation_id": "reg-service", "hierarchy_path": "복무규정 > 제1조"},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            with patch(
                "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                return_value=records,
            ):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=Path(tmp) / "source",
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    profile_id="public_portal-test-profile",
                    document_ids=["doc-a", "doc-c"],
                    scope="selected_documents",
                    require_kordoc_table_parser=False,
                    require_source_metadata=False,
                )

            exported_records = [
                json.loads(line)
                for line in (output_dir / "data" / "vector_db" / "tenant-a" / "approved_vectors.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]

        self.assertEqual("selected_documents", runtime_manifest["scope"])
        self.assertEqual(["doc-a", "doc-c"], runtime_manifest["document_ids"])
        self.assertEqual({"doc-a", "doc-c"}, {record["document_id"] for record in exported_records})
        metadata_by_document = {record["document_id"]: record["metadata"] for record in exported_records}
        self.assertEqual("reg-personnel", metadata_by_document["doc-a"]["regulation_id"])
        self.assertEqual("인사규정 > 제1조", metadata_by_document["doc-a"]["hierarchy_path"])
        self.assertEqual("reg-service", metadata_by_document["doc-c"]["regulation_id"])
        self.assertEqual("복무규정 > 제1조", metadata_by_document["doc-c"]["hierarchy_path"])

    def test_runtime_bundle_rejects_selected_document_missing_visible_records(self) -> None:
        records = [_runtime_export_record("doc-a", "chunk-a")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                return_value=records,
            ):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=Path(tmp) / "source",
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        profile_id="public_portal-test-profile",
                        document_ids=["doc-a", "doc-missing"],
                        scope="selected_documents",
                        require_kordoc_table_parser=False,
                        require_source_metadata=False,
                    )

        self.assertIn("not all MCP-visible", str(raised.exception))
        self.assertIn("doc-missing", str(raised.exception))

    def test_runtime_bundle_exports_kordoc_table_parser_summary(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 2, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]
            runtime_progress: list[tuple[int, str, int | None, int | None]] = []

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                    progress_callback=lambda percent, message, current, total: runtime_progress.append(
                        (percent, message, current, total)
                    ),
                )

            saved_manifest = json.loads(
                (output_dir / "data" / "mcp_runtime_manifest.json").read_text(encoding="utf-8")
            )
            approval_snapshot = json.loads(
                (output_dir / "data" / "repository" / "approval_snapshot.json").read_text(encoding="utf-8")
            )

        summary = runtime_manifest["kordoc_table_parser_summary"]
        self.assertTrue(runtime_manifest["kordoc_table_parser_required"])
        self.assertEqual(summary["parsed_document_count"], 1)
        self.assertEqual(summary["documents"][0]["parser"], "kordoc")
        self.assertEqual(summary["documents"][0]["table_count"], 2)
        self.assertTrue(runtime_manifest["source_metadata_required"])
        self.assertTrue(runtime_manifest["source_metadata_summary"]["complete"])
        self.assertEqual("규정", runtime_manifest["recommended_smoke_query"])
        self.assertEqual(saved_manifest["kordoc_table_parser_summary"]["parsed_document_count"], 1)
        self.assertTrue(saved_manifest["source_metadata_summary"]["complete"])
        self.assertEqual("규정", saved_manifest["recommended_smoke_query"])
        self.assertEqual("mcp_runtime_approval_snapshot", approval_snapshot["report_type"])
        self.assertEqual(1, approval_snapshot["snapshot_count"])
        self.assertEqual("doc-kordoc", approval_snapshot["entries"][0]["document_id"])
        self.assertEqual("chunk-1", approval_snapshot["entries"][0]["chunk_id"])
        self.assertTrue(runtime_progress)
        self.assertEqual(100, runtime_progress[-1][0])
        self.assertEqual(sorted(item[0] for item in runtime_progress), [item[0] for item in runtime_progress])

    def test_runtime_bundle_preserves_bulk_approval_review_events(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 2, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 2,
        }
        bulk_events = [
            {
                "event": "human_review_confirmed",
                "timestamp": "2026-07-10T00:00:00+00:00",
                "actor": "operator",
                "chunk_id": "chunk-1",
                "sequence": index,
            }
            for index in range(150)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            JsonRepository(settings).append_approval_record(
                {
                    "approval_record_id": "approval-record-bulk-review-events",
                    "approval_id": "approval-bulk-review-events",
                    "document_id": "doc-kordoc",
                    "tenant_id": "tenant-a",
                    "chunk_ids": ["chunk-1"],
                    "approved_content_hashes": {"chunk-1": "approved-hash"},
                    "approved_chunks": [
                        {
                            "chunk_id": "chunk-1",
                            "approved_content_hash": "approved-hash",
                        }
                    ],
                    "approved_by": "operator",
                    "approved_at": "2026-07-10T00:00:01+00:00",
                    "human_review_confirmed": True,
                    "review_decision_events": bulk_events,
                    "worklist_evidence": _approval_worklist_evidence(),
                }
            )
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            journal_records = [
                json.loads(line)
                for line in (output_dir / "data" / "repository" / "journals" / "approvals.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]

        exported_bulk = next(
            record for record in journal_records if record.get("approval_id") == "approval-bulk-review-events"
        )
        self.assertEqual(2, runtime_manifest["approval_record_count"])
        self.assertEqual(150, len(exported_bulk["review_decision_events"]))

    def test_runtime_bundle_exports_recommended_smoke_query_from_article_metadata(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 2, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 2,
        }
        record_metadata = {
            "chunk_type": "article",
            "article_no": "제7조",
            "article_title": "위임전결",
            "appendix_refs": ["별표1"],
            "regulation_title": "권한위임전결규정",
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1", metadata=record_metadata)]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            saved_manifest = json.loads(
                (output_dir / "data" / "mcp_runtime_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual("제7조 위임전결", runtime_manifest["recommended_smoke_query"])
        self.assertEqual("제7조 위임전결", saved_manifest["recommended_smoke_query"])

    def test_recommended_smoke_query_recovers_from_broken_article_metadata(self) -> None:
        records = [
            _runtime_export_record(
                "doc-kordoc",
                "chunk-broken-metadata",
                metadata={
                    "chunk_type": "article",
                    "article_no": "??",
                    "article_title": "??",
                    "regulation_title": "권한위임전결규정",
                },
            )
        ]
        records[0]["text"] = "제7조(위임전결) 권한의 위임전결 기준은 별표에 따른다."

        self.assertEqual("제7조 위임전결", _recommended_runtime_smoke_query(records))

    def test_recommended_smoke_query_prefers_substantive_article_over_supplementary_transition(self) -> None:
        records = [
            _runtime_export_record(
                "doc-kordoc",
                "chunk-transition",
                metadata={
                    "chunk_type": "article",
                    "article_no": "제2조",
                    "article_title": "장해심사 권역별 통합심사 등 시행에 따른 경과조치",
                    "appendix_refs": ["별표1"],
                },
            ),
            _runtime_export_record(
                "doc-kordoc",
                "chunk-delegation",
                metadata={
                    "chunk_type": "article",
                    "article_no": "제7조",
                    "article_title": "위임전결",
                    "appendix_refs": ["별표1"],
                },
            ),
        ]

        self.assertEqual("제7조 위임전결", _recommended_runtime_smoke_query(records))

    def test_cli_out_dir_writes_clean_runtime_data_bundle(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _seed_runtime_bundle_document(root, file_type="hwp", metadata=metadata)
            output_dir = root / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with (
                patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records),
                patch.object(
                    sys,
                    "argv",
                    [
                        "generate_mcp_client_config.py",
                        "--client-profile",
                        "bundle",
                        "--server-name",
                        "govreg-local",
                        "--data-dir",
                        str(settings.data_dir),
                        "--tenant-id",
                        "tenant-a",
                        "--document-id",
                        "doc-kordoc",
                        "--out-dir",
                        str(output_dir),
                    ],
                ),
                patch("builtins.print"),
            ):
                exit_code = main()

            runtime_data_dir = output_dir / "data"
            runtime_manifest = json.loads(
                runtime_data_dir.joinpath("mcp_runtime_manifest.json").read_text(encoding="utf-8")
            )
            raw_results = sorted(
                path.name
                for path in runtime_data_dir.joinpath("repository").glob("*.json")
                if path.name.endswith(("_nodes.json", "_issues.json", "_quality.json"))
            )

            self.assertEqual(0, exit_code)
            self.assertTrue((output_dir / "mcp_config.bundle.json").is_file())
            self.assertTrue((runtime_data_dir / "mcp_runtime_manifest.json").is_file())
            self.assertTrue((output_dir / "bundle_status.json").is_file())
            self.assertEqual("doc-kordoc", runtime_manifest["document_id"])
            self.assertIsNone(runtime_manifest["source_data_dir"])
            self.assertEqual("approved_local_export", runtime_manifest["source_data_provenance"])
            self.assertNotIn(str(settings.data_dir), json.dumps(runtime_manifest, ensure_ascii=False))
            self.assertTrue((runtime_data_dir / "repository" / "doc-kordoc_chunks.json").is_file())
            self.assertEqual([], raw_results)
            bundle_status = json.loads((output_dir / "bundle_status.json").read_text(encoding="utf-8"))
            self.assertTrue(bundle_status["runtime_data_ready"])
            self.assertIsNone(bundle_status["installation_attempt_id"])
            self.assertEqual("not_installed", bundle_status["installation_state"])
            self.assertEqual("not_configured", bundle_status["connection_state"])
            self.assertEqual("doc-kordoc", bundle_status["document_id"])
            self.assertEqual(1, bundle_status["record_count"])
            self.assertEqual(runtime_manifest["recommended_smoke_query"], bundle_status["recommended_smoke_query"])

    def test_cli_can_write_source_only_setup_bundle_without_runtime_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "generate_mcp_client_config.py",
                        "--client-profile",
                        "bundle",
                        "--out-dir",
                        str(output_dir),
                        "--skip-runtime-data",
                    ],
                ),
                patch("builtins.print"),
            ):
                exit_code = main()

            self.assertEqual(0, exit_code)
            self.assertTrue((output_dir / "mcp_config.bundle.json").is_file())
            self.assertFalse((output_dir / "data").exists())

    def test_cli_source_only_bundle_removes_stale_runtime_data_before_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            stale_data_dir = output_dir / "data"
            stale_data_dir.mkdir(parents=True)
            (stale_data_dir / "mcp_runtime_manifest.json").write_text(
                json.dumps({"document_ids": ["stale-document"]}),
                encoding="utf-8",
            )
            zip_out = Path(tmp) / "bundle.zip"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "generate_mcp_client_config.py",
                        "--client-profile",
                        "bundle",
                        "--out-dir",
                        str(output_dir),
                        "--zip-out",
                        str(zip_out),
                        "--skip-runtime-data",
                    ],
                ),
                patch("builtins.print"),
            ):
                exit_code = main()

            with zipfile.ZipFile(zip_out) as archive:
                names = archive.namelist()

            self.assertEqual(0, exit_code)
            self.assertFalse(stale_data_dir.exists())
            self.assertIn("data/", names)
            self.assertEqual(["data/"], [name for name in names if name.startswith("data/")])

    def test_runtime_bundle_requires_source_metadata_on_approved_records(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]
            records[0]["metadata"].pop("source_url")

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn("requires citation/source metadata", str(raised.exception))
        self.assertIn("source_url", str(raised.exception))

    def test_runtime_bundle_does_not_fall_back_to_flat_source_when_auto_isolated_source_is_empty(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            isolated_dir = settings.data_dir / "tenants" / "tenant-a"
            isolated_dir.mkdir(parents=True)
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]
            seen_data_dirs: list[Path] = []

            def visible_records(*, settings, auth, document_id, profile_id=None):
                seen_data_dirs.append(Path(settings.data_dir))
                return [] if Path(settings.data_dir) == isolated_dir else records

            with patch(
                "scripts.generate_mcp_client_config._runtime_visible_records_for_export",
                side_effect=visible_records,
            ):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=output_dir,
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn(isolated_dir, seen_data_dirs)
        self.assertNotIn(settings.data_dir, seen_data_dirs)
        self.assertIn("No MCP-visible approved records", str(raised.exception))

    def test_runtime_bundle_rejects_stale_vector_when_current_chunk_is_draft(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc-kordoc")
            draft_chunk = chunks[0].model_copy(update={"approval_status": "draft"})
            repository.save_processing_result("doc-kordoc", [], [draft_chunk], [])
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                with self.assertRaises(ValueError) as raised:
                    write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=Path(tmp) / "bundle",
                        tenant_id="tenant-a",
                        document_id="doc-kordoc",
                    )

        self.assertIn("current repository chunks no longer match approved vector records", str(raised.exception))
        self.assertIn("chunk_not_approved", str(raised.exception))

    def test_runtime_bundle_does_not_export_raw_nodes_issues_or_quality_reports(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            repository = JsonRepository(settings)
            chunks = repository.get_chunks("doc-kordoc")
            repository.save_processing_result(
                "doc-kordoc",
                [
                    StructureNode(
                        node_id="node-draft",
                        document_id="doc-kordoc",
                        node_type="article",
                        text="DRAFT ONLY RAW NODE TEXT",
                        order_index=1,
                    )
                ],
                chunks,
                [],
            )
            output_dir = Path(tmp) / "bundle"
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            repository_dir = output_dir / "data" / "repository"
            result_files = set(runtime_manifest["files"]["result_files"])

            self.assertTrue((repository_dir / "doc-kordoc_chunks.json").exists())
            self.assertFalse((repository_dir / "doc-kordoc_nodes.json").exists())
            self.assertFalse((repository_dir / "doc-kordoc_issues.json").exists())
            self.assertFalse((repository_dir / "doc-kordoc_quality.json").exists())
            self.assertNotIn(str(repository_dir / "doc-kordoc_nodes.json"), result_files)

    def test_runtime_bundle_export_clears_stale_runtime_files(self) -> None:
        metadata = {
            "kordoc_table_inventory": {"status": "parsed", "parser": "kordoc", "table_count": 1, "tables": []},
            "kordoc_table_parser_status": "parsed",
            "kordoc_table_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = _seed_runtime_bundle_document(Path(tmp), file_type="hwp", metadata=metadata)
            output_dir = Path(tmp) / "bundle"
            stale_repository = output_dir / "data" / "repository"
            stale_vector = output_dir / "data" / "vector_db" / "tenant-a"
            stale_repository.mkdir(parents=True)
            stale_vector.mkdir(parents=True)
            stale_chunk = stale_repository / "doc-stale_chunks.json"
            stale_manifest = output_dir / "data" / "mcp_runtime_manifest.json"
            stale_vector_file = stale_vector / "approved_vectors.jsonl"
            stale_top_level_file = output_dir / "data" / "legacy_export.json"
            stale_chunk.write_text("[]\n", encoding="utf-8")
            stale_manifest.write_text("{}\n", encoding="utf-8")
            stale_vector_file.write_text("{}\n", encoding="utf-8")
            stale_top_level_file.write_text("{}\n", encoding="utf-8")
            records = [_runtime_export_record("doc-kordoc", "chunk-1")]

            with patch("scripts.generate_mcp_client_config._runtime_visible_records_for_export", return_value=records):
                runtime_manifest = write_mcp_runtime_data_bundle(
                    source_data_dir=settings.data_dir,
                    out_dir=output_dir,
                    tenant_id="tenant-a",
                    document_id="doc-kordoc",
                )

            self.assertFalse(stale_chunk.exists())
            self.assertFalse(stale_top_level_file.exists())
            self.assertTrue((output_dir / "data" / "repository" / "doc-kordoc_chunks.json").exists())
            self.assertEqual(runtime_manifest["bm25_index_status"], "ready")
            self.assertEqual(runtime_manifest["bm25_document_count"], 1)
            self.assertTrue((output_dir / "data" / "vector_db" / "tenant-a" / "bm25_index.json").exists())
            self.assertEqual("ready", runtime_manifest["hierarchical_index_status"])
            self.assertEqual(1, runtime_manifest["regulation_count"])
            self.assertEqual(1, runtime_manifest["regulation_version_count"])
            self.assertGreaterEqual(runtime_manifest["toc_node_count"], 1)
            self.assertEqual("reg-rag-logical-corpus-v1", runtime_manifest["rebuild_fingerprint_schema_version"])
            self.assertRegex(runtime_manifest["logical_corpus_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(runtime_manifest["rebuild_contract"]["input_order_independent"])
            hierarchy_path = output_dir / "data" / "hierarchy" / "regulation_hierarchy.sqlite3"
            self.assertTrue(hierarchy_path.exists())
            self.assertEqual(
                runtime_manifest["files"]["hierarchical_index_sha256"],
                hashlib.sha256(hierarchy_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                1,
                len(list((output_dir / "data" / "repository").glob("doc-*_chunks.json"))),
            )

    def test_local_only_setup_bundle_writes_required_contract_files(self) -> None:
        config = build_mcp_client_config(
            server_name="govreg-local",
            client_profile="bundle",
            tenant_id="tenant-a",
        )

        with tempfile.TemporaryDirectory() as tmp:
            write_mcp_setup_bundle(config, tmp, server_name="govreg-local")
            output_dir = Path(tmp)
            generated_names = {path.name for path in output_dir.iterdir() if path.is_file()}

        self.assertTrue(REQUIRED_SETUP_BUNDLE_FILES.issubset(generated_names))
        self.assertNotIn("claude_code_add_http.ps1", generated_names)

    def test_packaged_app_writes_executable_first_stdio_launcher(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")
        packaged_exe = r"C:\Program Files\PR MCP Builder\PR MCP Builder.exe"

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"REG_RAG_PACKAGED_EXE": packaged_exe}):
                write_mcp_setup_bundle(config, tmp, server_name="govreg-local")
            launcher = Path(tmp, "run_mcp_stdio_server.ps1").read_text(encoding="utf-8")

        self.assertIn(packaged_exe, launcher)
        self.assertIn("& $PackagedExe --mcp-server @ServerArgs", launcher)
        self.assertLess(launcher.index("$PackagedExe"), launcher.index("function Find-ProjectRoot"))

    def test_zip_can_include_built_wheel_for_self_contained_handoff(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "bundle"
            dist_dir = root / "dist"
            zip_path = root / "bundle.zip"
            wheel = dist_dir / "reg_rag_preprocessor-0.1.0-py3-none-any.whl"
            dist_dir.mkdir()
            wheel.write_bytes(b"fake wheel")
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            write_mcp_setup_bundle_zip(output_dir, zip_path, include_wheel=True, dist_dir=dist_dir)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("reg_rag_preprocessor-0.1.0-py3-none-any.whl", names)
            self.assertIn("install_local_package.ps1", names)

    def test_zip_include_wheel_can_run_outside_project_cwd(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "bundle"
            dist_dir = root / "dist"
            other_cwd = root / "other"
            zip_path = root / "bundle.zip"
            wheel = dist_dir / "reg_rag_preprocessor-0.1.0-py3-none-any.whl"
            dist_dir.mkdir()
            other_cwd.mkdir()
            wheel.write_bytes(b"fake wheel")
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")

            previous_cwd = Path.cwd()
            try:
                os.chdir(other_cwd)
                write_mcp_setup_bundle_zip(output_dir, zip_path, include_wheel=True)
            finally:
                os.chdir(previous_cwd)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("reg_rag_preprocessor-0.1.0-py3-none-any.whl", names)

    def test_zip_out_inside_bundle_does_not_include_itself(self) -> None:
        config = build_mcp_client_config(client_profile="bundle", tenant_id="tenant-a")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "bundle"
            zip_path = output_dir / "bundle.zip"
            write_mcp_setup_bundle(config, output_dir, server_name="govreg-local")
            write_mcp_setup_bundle_zip(output_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertNotIn("bundle.zip", names)
            self.assertIn("connect_mcp_client.ps1", names)

    def test_rejects_unknown_transport(self) -> None:
        with self.assertRaises(ValueError):
            build_mcp_client_config(transport="websocket")

    def test_rejects_unknown_client_profile(self) -> None:
        with self.assertRaises(ValueError):
            build_mcp_client_config(client_profile="unknown-client")


def _seed_runtime_bundle_document(root: Path, *, file_type: str, metadata: dict) -> Settings:
    settings = Settings(data_dir=root / "data")
    repository = JsonRepository(settings)
    document = Document(
        document_id="doc-kordoc",
        filename=f"rules.{file_type}",
        document_name="Rules",
        institution_name="Test Institution",
        source_system="PUBLIC_PORTAL",
        source_url="https://example.test/rules",
        profile_id="public_portal-test-profile",
        file_type=file_type,
        file_hash="hash",
        tenant_id="tenant-a",
        status="completed",
        regulation_id="reg-kordoc",
        regulation_version="v1",
        effective_from="2026-01-01",
        regulation_status="approved",
    )
    chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc-kordoc",
        chunk_type="article",
        text="approved text",
        normalized_text="approved text",
        retrieval_text="approved text",
        metadata={
            "chunk_id": "chunk-1",
            "document_id": "doc-kordoc",
            "tenant_id": "tenant-a",
            "approval_status": "approved",
            "approval_id": "approval-kordoc",
            "approved_content_hash": "approved-hash",
            "security_level": "internal",
            "institution_name": "Test Institution",
            "source_system": "PUBLIC_PORTAL",
            "source_url": "https://example.test/rules",
            "profile_id": "public_portal-test-profile",
            "regulation_id": "reg-kordoc",
            "regulation_version": "v1",
            "effective_from": "2026-01-01",
            "regulation_status": "approved",
            **metadata,
        },
        approval_status="approved",
        approval_id="approval-kordoc",
        approved_content_hash="approved-hash",
        security_level="internal",
    )
    repository.upsert_document(document)
    repository.save_processing_result(document.document_id, [], [chunk], [])
    repository.append_approval_record(
        {
            "approval_record_id": "approval-record-kordoc",
            "approval_id": "approval-kordoc",
            "document_id": "doc-kordoc",
            "tenant_id": "tenant-a",
            "chunk_ids": ["chunk-1"],
            "approved_content_hashes": {"chunk-1": "approved-hash"},
            "approved_chunks": [
                {
                    "chunk_id": "chunk-1",
                    "approved_content_hash": "approved-hash",
                }
            ],
            "approved_by": "operator",
            "approved_at": "2026-07-10T00:00:00+00:00",
            "worklist_evidence": _approval_worklist_evidence(),
        }
    )
    return settings


def _write_runtime_data_manifest(
    runtime_data_dir: Path,
    document_ids: list[str],
    *,
    tenant_id: str = "tenant-a",
) -> None:
    runtime_data_dir.mkdir(parents=True, exist_ok=True)
    runtime_data_dir.joinpath("mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": tenant_id,
                "document_id": document_ids[0] if document_ids else None,
                "document_ids": document_ids,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_runtime_repository_manifest(repository_dir: Path, document_ids: list[str]) -> None:
    repository_dir.mkdir(parents=True, exist_ok=True)
    repository_dir.joinpath("manifest.json").write_text(
        json.dumps({"documents": {document_id: {"document_id": document_id} for document_id in document_ids}}),
        encoding="utf-8",
    )


def _runtime_export_record(document_id: str, chunk_id: str, *, metadata: dict | None = None) -> dict:
    metadata = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "tenant_id": "tenant-a",
        "approval_status": "approved",
        "approval_id": "approval-kordoc",
        "approved_content_hash": "approved-hash",
        "security_level": "internal",
        "approval_worklist_report_path": "reports/worklist.json",
        "approval_worklist_report_sha256": "a" * 64,
        "approval_review_batch_manifest_path": "reports/batches.json",
        "approval_review_batch_manifest_sha256": "b" * 64,
        "approval_review_batch_id": "batch-kordoc",
        "approval_review_batch_chunk_fingerprint": "c" * 64,
        "approval_review_strategy": "operator_manual_review",
        "institution_name": "Test Institution",
        "source_system": "PUBLIC_PORTAL",
        "source_url": "https://example.test/rules",
        "profile_id": "public_portal-test-profile",
        "regulation_id": "reg-kordoc",
        "regulation_version": "v1",
        "effective_from": "2026-01-01",
        "regulation_status": "approved",
        **(metadata or {}),
    }
    text = "approved text"
    return {
        "id": f"{document_id}:{chunk_id}",
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


def _approval_worklist_evidence() -> dict[str, str]:
    return {
        "worklist_report_path": "reports/worklist.json",
        "worklist_report_sha256": "a" * 64,
        "review_batch_manifest_path": "reports/batches.json",
        "review_batch_manifest_sha256": "b" * 64,
        "review_batch_id": "batch-kordoc",
        "review_batch_chunk_fingerprint": "c" * 64,
        "review_strategy": "operator_manual_review",
    }


if __name__ == "__main__":
    unittest.main()
