from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.generate_mcp_client_config import build_mcp_client_config, write_mcp_setup_bundle


def _windows_environment(root: Path, fake_bin: Path) -> dict[str, str]:
    environment = dict(os.environ)
    windows_dir = Path(environment.get("SystemRoot", r"C:\Windows"))
    powershell_dir = windows_dir / "System32" / "WindowsPowerShell" / "v1.0"
    user_profile = root / "user"
    appdata = user_profile / "AppData" / "Roaming"
    localappdata = user_profile / "AppData" / "Local"
    codex_home = user_profile / ".codex"
    for directory in (appdata, localappdata, codex_home):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "PATH": os.pathsep.join(
                [str(fake_bin), str(windows_dir / "System32"), str(powershell_dir)]
            ),
            "USERPROFILE": str(user_profile),
            "HOME": str(user_profile),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "CODEX_HOME": str(codex_home),
        }
    )
    return environment


def _write_bundle(root: Path) -> tuple[dict[str, str], Path]:
    config = build_mcp_client_config(
        server_name="aksmcp",
        client_profile="bundle",
        data_dir="data",
        tenant_id="tenant-a",
    )
    fake_project_root = root / "fake-project"
    fake_scripts = fake_project_root / "scripts"
    fake_scripts.mkdir(parents=True)
    (fake_scripts / "check_mcp_connection_readiness.py").write_text(
        'print("atomicity-doctor-ok")\nraise SystemExit(0)\n',
        encoding="utf-8",
    )
    bundle_dir = root / "bundle"
    files = write_mcp_setup_bundle(
        config,
        bundle_dir,
        server_name="aksmcp",
        preferred_python=sys.executable,
        preferred_project_root=fake_project_root,
    )
    (bundle_dir / "data").mkdir(parents=True, exist_ok=True)
    (fake_scripts / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "scripts" / "mcp_client_status.py",
        fake_scripts / "mcp_client_status.py",
    )
    status_path = bundle_dir / "bundle_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["runtime_fingerprint"] = "sha256:" + hashlib.sha256(
        b"runtime-current"
    ).hexdigest()
    status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return files, bundle_dir


def _run_powershell(script: str | Path, *, root: Path, environment: dict[str, str], args: list[str]) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise unittest.SkipTest("PowerShell is not available.")
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


@unittest.skipUnless(os.name == "nt", "PowerShell CLI atomicity tests are Windows-specific.")
class McpWindowsAtomicityBoundaryTests(unittest.TestCase):
    def test_claude_code_failure_restores_previous_user_scope_entry(self) -> None:
        for failure_stage in ("add", "get"):
            with self.subTest(failure_stage=failure_stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                files, bundle_dir = _write_bundle(root)
                fake_bin = root / "fake-bin"
                fake_bin.mkdir()
                state_path = root / "user" / ".claude.json"
                state_path.parent.mkdir(parents=True, exist_ok=True)
                prior_state = (
                    b'{"mcpServers":{"aksmcp":{"command":"legacy-python.exe",'
                    b'"args":["--legacy-config"]}},"theme":"dark"}\r\n'
                )
                state_path.write_bytes(prior_state)
                call_log = root / "claude-calls.txt"
                (fake_bin / "reg-rag-mcp-doctor.cmd").write_text(
                    "@echo atomicity-doctor-ok\r\n@exit /b 0\r\n",
                    encoding="utf-8",
                )
                (fake_bin / "claude.cmd").write_text(
                    "@echo off\r\n"
                    "echo %*>>\"%CLAUDE_TEST_LOG%\"\r\n"
                    "if \"%1 %2\"==\"mcp remove\" (\r\n"
                    "  if /I \"%5\"==\"local\" exit /b 1\r\n"
                    "  if /I \"%5\"==\"user\" (>\"%CLAUDE_STATE%\" echo {\"mcpServers\":{},\"theme\":\"dark\"}& exit /b 0)\r\n"
                    ")\r\n"
                    "if \"%1 %2\"==\"mcp add\" (\r\n"
                    "  echo %*| %SystemRoot%\\System32\\findstr.exe /C:\"legacy-python.exe\" >nul\r\n"
                    "  if not errorlevel 1 goto restore_prior\r\n"
                    "  if /I \"%CLAUDE_FAIL_STAGE%\"==\"add\" goto fail_add\r\n"
                    "  goto add_replacement\r\n"
                    ")\r\n"
                    "if \"%1 %2\"==\"mcp get\" (\r\n"
                    "  %SystemRoot%\\System32\\findstr.exe /C:\"legacy-python.exe\" \"%CLAUDE_STATE%\" >nul\r\n"
                    "  if not errorlevel 1 (echo aksmcp: Scope: User Command: legacy-python.exe --legacy-config& exit /b 0)\r\n"
                    "  if /I \"%CLAUDE_FAIL_STAGE%\"==\"get\" (echo forced get failure 1>&2& exit /b 8)\r\n"
                    "  echo aksmcp: Scope: User Command: powershell.exe -File %CLAUDE_EXPECTED_LAUNCHER% --data-dir %CLAUDE_EXPECTED_DATA%\r\n"
                    "  exit /b 0\r\n"
                    ")\r\n"
                    "exit /b 2\r\n"
                    ":restore_prior\r\n"
                    ">\"%CLAUDE_STATE%\" echo {\"mcpServers\":{\"aksmcp\":{\"command\":\"legacy-python.exe\",\"args\":[\"--legacy-config\"]}},\"theme\":\"dark\"}\r\n"
                    "exit /b 0\r\n"
                    ":fail_add\r\n"
                    "echo forced add failure 1>&2\r\n"
                    "exit /b 9\r\n"
                    ":add_replacement\r\n"
                    ">\"%CLAUDE_STATE%\" echo {\"mcpServers\":{\"aksmcp\":{\"command\":\"powershell.exe\",\"args\":[\"replacement\"]}},\"theme\":\"dark\"}\r\n"
                    "echo Added replacement user entry\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
                environment = _windows_environment(root, fake_bin)
                environment.update(
                    {
                        "CLAUDE_STATE": str(state_path),
                        "CLAUDE_TEST_LOG": str(call_log),
                        "CLAUDE_FAIL_STAGE": failure_stage,
                        "CLAUDE_EXPECTED_LAUNCHER": str(
                            (bundle_dir / "run_mcp_stdio_server.ps1").resolve()
                        ),
                        "CLAUDE_EXPECTED_DATA": str((bundle_dir / "data").resolve()),
                    }
                )

                completed = _run_powershell(
                    files["claude_code_stdio"],
                    root=root,
                    environment=environment,
                    args=[],
                )

                self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertEqual(
                    prior_state,
                    state_path.read_bytes(),
                    "A failed Claude Code replacement must restore the exact prior .claude.json bytes.\n"
                    + completed.stdout
                    + completed.stderr,
                )

    def test_plugin_failure_restores_prior_marketplace_without_installed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, bundle_dir = _write_bundle(root)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            call_log = root / "codex-calls.txt"
            marketplace_state = root / "marketplace-state.txt"
            marketplace_state.write_text("prior\n", encoding="utf-8")
            prior_marketplace = root / "prior-marketplace"
            prior_marketplace.mkdir()
            current_marketplace = bundle_dir / "chatgpt-desktop-local-plugin"
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "echo %*>>\"%CODEX_TEST_LOG%\"\r\n"
                "if \"%1\"==\"--version\" (echo codex-cli fixture& exit /b 0)\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace list\" (\r\n"
                "  echo {\"marketplaces\":[{\"name\":\"aksmcp-local\",\"source\":\"%CODEX_PRIOR_MARKETPLACE_JSON%\"}]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin list\" (echo {\"installed\":[],\"available\":[]}& exit /b 0)\r\n"
                "if \"%1 %2\"==\"plugin remove\" exit /b 0\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace remove\" (>%CODEX_MARKETPLACE_STATE% echo absent& exit /b 0)\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace add\" (\r\n"
                "  if /I \"%~4\"==\"%CODEX_PRIOR_MARKETPLACE%\" (>%CODEX_MARKETPLACE_STATE% echo prior& exit /b 0)\r\n"
                "  echo forced current marketplace add failure 1>&2\r\n"
                "  exit /b 9\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" exit /b 1\r\n"
                "exit /b 2\r\n",
                encoding="utf-8",
            )
            environment = _windows_environment(root, fake_bin)
            environment.update(
                {
                    "CODEX_TEST_LOG": str(call_log),
                    "CODEX_MARKETPLACE_STATE": str(marketplace_state),
                    "CODEX_PRIOR_MARKETPLACE": prior_marketplace.as_posix(),
                    "CODEX_PRIOR_MARKETPLACE_JSON": prior_marketplace.as_posix(),
                    "CODEX_CURRENT_MARKETPLACE": str(current_marketplace),
                }
            )

            completed = _run_powershell(
                files["connect"],
                root=root,
                environment=environment,
                args=["-Target", "chatgpt-desktop-local", "-InstallChatGptDesktopPlugin"],
            )
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertEqual(
                ("prior", True),
                (
                    marketplace_state.read_text(encoding="utf-8").strip(),
                    status["plugin_rollback_complete"],
                ),
                "A marketplace that existed without an installed plugin must be restored after failure.\n"
                + completed.stdout
                + completed.stderr,
            )

    def test_plugin_failure_restores_prior_disabled_state_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, bundle_dir = _write_bundle(root)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            call_log = root / "codex-calls.txt"
            plugin_state = root / "plugin-state.txt"
            marketplace_state = root / "marketplace-state.txt"
            plugin_state.write_text("disabled\n", encoding="utf-8")
            marketplace_state.write_text("prior\n", encoding="utf-8")
            prior_marketplace = root / "prior-marketplace"
            prior_marketplace.mkdir()
            prior_version = "0.1.0+codex.prior0000001"
            (fake_bin / "codex.cmd").write_text(
                "@echo off\r\n"
                "echo %*>>\"%CODEX_TEST_LOG%\"\r\n"
                "set \"PLUGIN_STATE=\"\r\n"
                "if exist \"%CODEX_PLUGIN_STATE%\" set /p PLUGIN_STATE=<\"%CODEX_PLUGIN_STATE%\"\r\n"
                "if \"%1\"==\"--version\" (echo codex-cli fixture& exit /b 0)\r\n"
                "if \"%1 %2\"==\"plugin list\" (\r\n"
                "  if /I \"%PLUGIN_STATE%\"==\"disabled\" (echo {\"installed\":[{\"pluginId\":\"aksmcp@aksmcp-local\",\"installed\":true,\"enabled\":false,\"version\":\"%CODEX_PRIOR_VERSION%\",\"marketplaceSource\":{\"source\":\"%CODEX_PRIOR_MARKETPLACE_JSON%\"}}],\"available\":[]}& exit /b 0)\r\n"
                "  if /I \"%PLUGIN_STATE%\"==\"enabled\" (echo {\"installed\":[{\"pluginId\":\"aksmcp@aksmcp-local\",\"installed\":true,\"enabled\":true,\"version\":\"%CODEX_PRIOR_VERSION%\",\"marketplaceSource\":{\"source\":\"%CODEX_PRIOR_MARKETPLACE_JSON%\"}}],\"available\":[]}& exit /b 0)\r\n"
                "  echo {\"installed\":[],\"available\":[]}\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"plugin remove\" (>%CODEX_PLUGIN_STATE% echo absent& exit /b 0)\r\n"
                "if \"%1 %2\"==\"plugin disable\" (>%CODEX_PLUGIN_STATE% echo disabled& exit /b 0)\r\n"
                "if \"%1 %2\"==\"plugin add\" (>%CODEX_PLUGIN_STATE% echo enabled& exit /b 0)\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace remove\" (>%CODEX_MARKETPLACE_STATE% echo absent& exit /b 0)\r\n"
                "if \"%1 %2 %3\"==\"plugin marketplace add\" (\r\n"
                "  if /I \"%~4\"==\"%CODEX_PRIOR_MARKETPLACE%\" (>%CODEX_MARKETPLACE_STATE% echo prior& exit /b 0)\r\n"
                "  echo forced current marketplace add failure 1>&2\r\n"
                "  exit /b 9\r\n"
                ")\r\n"
                "if \"%1 %2\"==\"mcp get\" exit /b 1\r\n"
                "exit /b 2\r\n",
                encoding="utf-8",
            )
            environment = _windows_environment(root, fake_bin)
            environment.update(
                {
                    "CODEX_TEST_LOG": str(call_log),
                    "CODEX_PLUGIN_STATE": str(plugin_state),
                    "CODEX_MARKETPLACE_STATE": str(marketplace_state),
                    "CODEX_PRIOR_MARKETPLACE": prior_marketplace.as_posix(),
                    "CODEX_PRIOR_MARKETPLACE_JSON": prior_marketplace.as_posix(),
                    "CODEX_PRIOR_VERSION": prior_version,
                }
            )

            completed = _run_powershell(
                files["connect"],
                root=root,
                environment=environment,
                args=["-Target", "chatgpt-desktop-local", "-InstallChatGptDesktopPlugin"],
            )
            status = json.loads((bundle_dir / "bundle_status.json").read_text(encoding="utf-8"))

            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertEqual(
                ("disabled", "prior", True),
                (
                    plugin_state.read_text(encoding="utf-8").strip(),
                    marketplace_state.read_text(encoding="utf-8").strip(),
                    status["plugin_rollback_complete"],
                ),
                "Rollback must preserve the prior plugin's disabled state, version, and marketplace source.\n"
                + completed.stdout
                + completed.stderr,
            )


if __name__ == "__main__":
    unittest.main()
