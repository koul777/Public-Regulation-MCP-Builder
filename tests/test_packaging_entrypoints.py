from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class PackagingEntrypointTests(unittest.TestCase):
    def test_operational_scripts_are_included_in_package_discovery(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        includes = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]

        self.assertIn("scripts*", includes)

    def test_console_scripts_point_to_importable_main_functions(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = pyproject["project"]["scripts"]

        expected_commands = {
            "reg-rag-batch",
            "reg-rag-public-batch-pipeline",
            "reg-rag-ci-gate",
            "reg-rag-nightly-smoke",
            "reg-rag-audit-release",
            "reg-rag-audit-public-release",
            "reg-rag-plan-public-release-cleanup",
            "reg-rag-public-release-gate",
            "reg-rag-check-private-release",
            "reg-rag-check-github-private",
            "reg-rag-check-console-scripts",
            "reg-rag-release-harness",
            "reg-rag-hermes",
            "reg-rag-sdist-rehearsal",
            "reg-rag-fresh-clone-rehearsal",
            "reg-rag-private-release-gate",
            "reg-rag-private-release-manifest",
            "reg-rag-release-evidence-index",
            "reg-rag-verify-release-evidence",
            "reg-rag-private-release-smoke",
            "reg-rag-public-readiness",
            "reg-rag-review-queue-triage",
            "reg-rag-review-triage-summary",
            "reg-rag-human-review-evidence",
            "reg-rag-approval-evidence",
            "reg-rag-approval-worklist",
            "reg-rag-approval-review-batches",
            "reg-rag-approval-sha-drift-plan",
            "reg-rag-reapproval-evidence",
            "reg-rag-reapproval-worklist",
            "reg-rag-reapproval-review-batches",
            "reg-rag-reapproval-review-burden",
            "reg-rag-reapproval-decision-check",
            "reg-rag-reapproval-apply-plan",
            "reg-rag-reapproval-shadow-apply",
            "reg-rag-profile-registry-from-batch",
            "reg-rag-export-public-report",
            "reg-rag-export-vectordb",
            "reg-rag-export-relations",
            "reg-rag-estimate-agent-review-cost",
            "reg-rag-estimate-embedding-cost",
            "reg-rag-embed-vectors",
            "reg-rag-upsert-vectordb",
            "reg-rag-rag-security-evidence",
            "reg-rag-secure-rag-smoke",
            "reg-rag-mcp-server",
            "reg-rag-mcp-smoke",
            "reg-rag-mcp-transport-smoke",
            "reg-rag-mcp-client-config-smoke",
            "reg-rag-mcp-bundle-zip-extract-smoke",
            "reg-rag-mcp-prepare-runtime",
            "reg-rag-mcp-product-readiness",
            "reg-rag-mcp-temporal-readiness-bundle",
            "reg-rag-mcp-config",
            "reg-rag-mcp-doctor",
            "reg-rag-mcp-handoff-report",
            "reg-rag-mcp-authority",
            "reg-rag-mcp-remediation-plan",
            "reg-rag-mcp-demo-answers",
            "reg-rag-mcp-answer-evidence-bundle",
            "reg-rag-mcp-performance-load-evidence",
            "reg-rag-mcp-cold-start-benchmark",
            "reg-rag-mcp-first-query-benchmark",
            "reg-rag-mcp-concurrent-benchmark",
            "reg-rag-mcp-index-visibility",
            "reg-rag-mcp-query-benchmark",
            "reg-rag-mcp-retrieval-quality",
            "reg-rag-revision-impact",
            "reg-rag-real-parser-fixtures",
            "reg-rag-parsing-goldset-start-here",
            "reg-rag-parsing-goldset-table-review-batches",
            "reg-rag-parsing-goldset-table-review-summary",
            "reg-rag-parsing-goldset-table-transfer-check",
            "reg-rag-parsing-goldset-table-source-check",
            "reg-rag-parsing-goldset-table-drift-check",
            "reg-rag-table-preprocessing-claim-gate",
            "reg-rag-pilot-blocker-action-board",
        }
        self.assertLessEqual(expected_commands, set(scripts))

        for target in scripts.values():
            module_name, function_name = target.split(":", 1)
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name)))

    def test_readme_documents_current_source_entrypoint(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("START_HERE.bat", readme)
        self.assertIn("Python 3.11 이상", readme)
        self.assertIn("전달용 ZIP을 **다른 Windows PC**로 옮겨", readme)
        self.assertIn("원래 PC에서 생성 폴더를 그대로 사용하는 경우", readme)
        self.assertIn(".\\.venv\\Scripts\\python.exe -m streamlit", readme)
        self.assertIn("scripts\\build_windows_portable.ps1", readme)
        self.assertIn("--portable-python", readme)
        self.assertIn("프로젝트 폴더의 `data\\`", readme)

    def test_windows_installer_stops_when_python_or_venv_is_missing(self) -> None:
        installer = (ROOT / "INSTALL_AND_RUN.bat").read_text(encoding="utf-8")

        self.assertIn("if errorlevel 1 goto :python_missing", installer)
        self.assertIn(
            'if not exist ".venv\\Scripts\\python.exe" goto :venv_missing',
            installer,
        )
        self.assertIn("py install 3.11", installer)

    def test_windows_launchers_select_an_available_ui_port(self) -> None:
        batch_launcher = (ROOT / "RUN_APP.bat").read_text(encoding="utf-8")
        packaged_launcher = (ROOT / "packaging" / "windows_launcher.py").read_text(encoding="utf-8")

        self.assertIn("scripts\\find_available_ui_port.py", batch_launcher)
        self.assertIn("--server.port %APP_PORT%", batch_launcher)
        self.assertIn("select_available_port(preferred_ui_port)", packaged_launcher)

    def test_portable_launcher_includes_the_separate_qwen_chat_app(self) -> None:
        packaged_launcher = (ROOT / "packaging" / "windows_launcher.py").read_text(
            encoding="utf-8"
        )
        portable_spec = (ROOT / "packaging" / "PR-MCP-Builder.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn('"--qwen-chat"', packaged_launcher)
        self.assertIn('"--port"', packaged_launcher)
        self.assertIn('"qwen_chat_app.py"', packaged_launcher)
        self.assertIn("run_qwen_chat(qwen_args)", packaged_launcher)
        self.assertIn("safe_environment = launch_environment()", packaged_launcher)
        self.assertIn("validate_launch_environment(safe_environment)", packaged_launcher)
        self.assertIn('"scripts.run_qwen_chat"', portable_spec)
        self.assertIn('"qwen_chat_app.py"', portable_spec)
        build_script = (ROOT / "scripts" / "build_windows_portable.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('@("--qwen-chat", "--help")', build_script)

    def test_portable_self_check_exercises_pymupdf_and_pdf_parser_without_runtime_setup(
        self,
    ) -> None:
        launcher_path = ROOT / "packaging" / "windows_launcher.py"
        spec = importlib.util.spec_from_file_location("portable_windows_launcher", launcher_path)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            exit_code = module.portable_self_check()

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], "pr-mcp-builder-portable-self-check-v1")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["pages"], 1)
        self.assertTrue(payload["text_verified"])
        self.assertNotIn("path", payload)
        self.assertNotIn("runtime", payload)

    def test_portable_self_check_fails_closed_with_short_json_when_pymupdf_is_missing(
        self,
    ) -> None:
        launcher_path = ROOT / "packaging" / "windows_launcher.py"
        spec = importlib.util.spec_from_file_location("portable_windows_launcher_missing", launcher_path)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        stdout = io.StringIO()
        with mock.patch.object(module, "fitz", None), mock.patch("sys.stdout", stdout):
            exit_code = module.portable_self_check()

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "schema_version": "pr-mcp-builder-portable-self-check-v1",
                "status": "failed",
                "reason": "pdf_parser_self_check_failed",
            },
        )

    def test_portable_version_exits_without_configuring_runtime(self) -> None:
        from app import __version__

        launcher_path = ROOT / "packaging" / "windows_launcher.py"
        spec = importlib.util.spec_from_file_location("portable_windows_launcher_version", launcher_path)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        with mock.patch.object(sys, "argv", ["PR MCP Builder.exe", "--version"]), mock.patch.object(
            module,
            "_configure_runtime",
        ) as configure_runtime, mock.patch("builtins.print") as print_output:
            exit_code = module.main()

        self.assertEqual(0, exit_code)
        configure_runtime.assert_not_called()
        print_output.assert_called_once_with(__version__)

    def test_windows_source_launcher_repairs_incomplete_setup_and_explains_browser_fallback(
        self,
    ) -> None:
        start_launcher = (ROOT / "START_HERE.bat").read_text(encoding="utf-8")
        installer = (ROOT / "INSTALL_AND_RUN.bat").read_text(encoding="utf-8")
        run_launcher = (ROOT / "RUN_APP.bat").read_text(encoding="utf-8")
        portable_readme = (ROOT / "packaging" / "README_RUN_KO.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "from app.utils.fitz_compat import fitz; import streamlit, fastapi, pydantic, pandas, docx, olefile, mcp, kiwipiepy, app",
            start_launcher,
        )
        self.assertIn("-m pip check", start_launcher)
        self.assertIn("import sys, pip", start_launcher)
        self.assertIn("sys.version_info >= (3, 11)", start_launcher)
        self.assertIn('set "PYTHONPATH="', start_launcher)
        self.assertIn('set "PYTHONHOME="', start_launcher)
        self.assertIn('call "%~dp0INSTALL_AND_RUN.bat"', start_launcher)
        self.assertIn('set "VENV_PYTHON_WORKS="', start_launcher)
        self.assertIn('call "%~dp0INSTALL_AND_RUN.bat" --recreate-venv', start_launcher)
        self.assertIn('set "PYTHONPATH="', installer)
        self.assertIn('set "PYTHONHOME="', installer)
        self.assertIn('if /I "%~1"=="--recreate-venv"', installer)
        self.assertIn('call :verify_recreate_target', installer)
        self.assertIn('if errorlevel 1 goto :venv_recreate_refused', installer)
        self.assertIn('rmdir /s /q ".venv"', installer)
        self.assertIn(
            '%PYTHON_CMD% "%~dp0scripts\\check_windows_venv_recreate_target.py" --path "%CD%\\.venv" >nul 2>&1',
            installer,
        )
        self.assertIn("Run START_HERE.bat again to detect and repair", run_launcher)
        self.assertIn("Python 3.11 or newer with pip", run_launcher)
        self.assertIn('set "PYTHONPATH="', run_launcher)
        self.assertIn('set "PYTHONHOME="', run_launcher)
        self.assertIn("from app.utils.fitz_compat import fitz;", run_launcher)
        self.assertIn("pandas, docx, olefile, mcp, kiwipiepy", run_launcher)
        self.assertIn("-m pip check", run_launcher)
        self.assertIn("If the browser does not open", run_launcher)
        self.assertIn("http://127.0.0.1:포트", portable_readme)
        self.assertIn("전달용 MCP ZIP", portable_readme)
        self.assertIn("대상 PC에 Python 3.11 이상이 필요", portable_readme)
        self.assertIn("그 PC에서 MCP 묶음을 다시 생성", portable_readme)
        self.assertIn("https://nodejs.org", portable_readme)
        self.assertIn("Node.js LTS", portable_readme)
        self.assertIn("Kordoc 설치·검증 시작", portable_readme)
        self.assertIn("동의해 버튼을 누른 경우에만", portable_readme)

    def test_windows_source_launchers_fail_closed_on_external_venv_packages(self) -> None:
        start_launcher = (ROOT / "START_HERE.bat").read_text(encoding="utf-8")
        installer = (ROOT / "INSTALL_AND_RUN.bat").read_text(encoding="utf-8")
        run_launcher = (ROOT / "RUN_APP.bat").read_text(encoding="utf-8")

        isolation_call = (
            'scripts\\check_build_environment_isolation.py" --venv-root '
            '"%CD%\\.venv" --fail-on-issue >nul 2>&1'
        )
        self.assertIn(isolation_call, start_launcher)
        self.assertIn(isolation_call, installer)
        self.assertIn(isolation_call, run_launcher)

        run_isolation = run_launcher.index("call :check_venv_isolation")
        run_import = run_launcher.index('"%VENV_PYTHON%" -c "from app.utils.fitz_compat import fitz; import sys, pip')
        run_pip_check = run_launcher.index('"%VENV_PYTHON%" -m pip check')
        run_port = run_launcher.index("scripts\\find_available_ui_port.py")
        self.assertLess(run_isolation, run_import)
        self.assertLess(run_isolation, run_pip_check)
        self.assertLess(run_isolation, run_port)

        start_check = start_launcher.index('if /I "%~1"=="--check" goto :check')
        start_check_run = start_launcher.index('call "%~dp0RUN_APP.bat" --check')
        self.assertLess(start_check, start_check_run)
        self.assertIn('exit /b %ERRORLEVEL%', start_launcher)
        self.assertIn("VENV_ISOLATION_FAILED", start_launcher)
        self.assertIn("INSTALL_AND_RUN.bat --recreate-venv", start_launcher)

        install_dependencies = installer.index('".venv\\Scripts\\python.exe" -m pip install -e .')
        install_isolation = installer.index("call :check_venv_isolation")
        install_run = installer.index('call "%~dp0RUN_APP.bat"')
        self.assertLess(install_dependencies, install_isolation)
        self.assertLess(install_isolation, install_run)
        self.assertIn("It will not retry automatically.", installer)
        self.assertIn("If only Anaconda Python is installed", installer)

        recreate_verify = installer.index("call :verify_recreate_target")
        recreate_remove = installer.index('rmdir /s /q ".venv"')
        self.assertLess(recreate_verify, recreate_remove)

    def test_windows_portable_release_includes_readme_link_targets(self) -> None:
        build_script = (ROOT / "scripts" / "build_windows_portable.ps1").read_text(encoding="utf-8")

        self.assertIn("from app import __version__", build_script)
        self.assertNotIn("0.1.0-dev", build_script)
        for expected_path in (
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
            "docs\\mcp_quickconnect_ko.md",
            "docs\\public_repository_history_policy_ko.md",
            "packaging\\INSTALL_KORDOC_KO.ps1",
        ):
            self.assertIn(expected_path, build_script)
        self.assertIn("-m build --wheel --outdir $DistRoot", build_script)
        self.assertIn("reg_rag_preprocessor-$Version-py3-none-any.whl", build_script)
        self.assertIn("Copy-Item -LiteralPath $BundledWheelPath", build_script)
        self.assertIn(".VersionInfo", build_script)
        self.assertIn("ProductVersion", build_script)
        self.assertIn('@("--mcp-server", "--help")', build_script)
        self.assertIn('@("--portable-self-check")', build_script)
        self.assertIn('@("--version")', build_script)
        self.assertIn("WaitForExit(60000)", build_script)
        self.assertIn("$ProbeProcess.Handle", build_script)
        self.assertIn("return $StdoutText", build_script)
        self.assertNotIn('return "$StdoutText`n$StderrText"', build_script)
        self.assertIn("pr-mcp-builder-windows-artifact-v1", build_script)
        self.assertIn("pr-mcp-builder-wheel-source-v1", build_script)
        self.assertIn("Get-FileHash", build_script)
        self.assertIn("refused stale or mixed executable/wheel artifacts", build_script)
        self.assertIn("refused a stale wheel for the current source tree", build_script)
        self.assertIn("functional_pdf_parser_probe", build_script)
        self.assertIn("Invoke-PortableExecutableProbe", build_script)
        self.assertIn('Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue', build_script)
        self.assertIn('Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue', build_script)
        self.assertIn('$env:PYTHONPATH = $PreviousPythonPath', build_script)
        self.assertIn('$env:PYTHONHOME = $PreviousPythonHome', build_script)
        self.assertIn("Get-PortableWheelSourceFingerprint", build_script)
        fingerprint_block = build_script[
            build_script.index("function Get-PortableWheelSourceFingerprint") : build_script.index(
                "function Assert-PortableExecutable",
            )
        ]
        for packaged_source_root in ('"app"', '"frontend"', '"scripts"'):
            self.assertIn(packaged_source_root, fingerprint_block)
        self.assertIn("Write-PortableWheelSourceBinding", build_script)
        self.assertIn("Assert-PortableWheelSourceBinding", build_script)
        self.assertIn("Executable --version probe mismatch", build_script)
        self.assertIn('$PreviousProgressPreference = $ProgressPreference', build_script)
        self.assertIn('$ProgressPreference = "SilentlyContinue"', build_script)
        self.assertIn('$ProgressPreference = $PreviousProgressPreference', build_script)

        dependency_install = build_script.index("-e . pyinstaller build")
        isolation_check = build_script.index("$BuildPython -I $IsolationHelper")
        wheel_build = build_script.index("-m build --wheel --outdir $DistRoot")
        pyinstaller_build = build_script.index("-m PyInstaller")
        self.assertLess(dependency_install, isolation_check)
        self.assertLess(isolation_check, wheel_build)
        self.assertLess(isolation_check, pyinstaller_build)
        self.assertIn("$BuildPython -I $IsolationHelper", build_script)
        self.assertIn("--fail-on-issue", build_script)
        self.assertIn("$PreviousProcessPath = $env:Path", build_script)
        self.assertIn("$env:Path = $PreviousProcessPath", build_script)
        self.assertIn("$ArtifactAllowedRoots", build_script)
        artifact_roots = build_script[
            build_script.index("$ArtifactAllowedRoots = @(") : build_script.index(
                "$ArtifactIsolationArgs = @(",
            )
        ]
        self.assertNotIn("            $BuildRoot\n", artifact_roots)
        self.assertIn("base_library.zip", build_script)
        self.assertIn('"--analysis-toc"', build_script)
        self.assertIn('"--allowed-path"', build_script)
        self.assertIn("$BuildPython -I -m PyInstaller", build_script)
        self.assertIn("[Environment]::SystemDirectory", build_script)
        self.assertIn('"--binary-toc"', build_script)
        self.assertIn('"--binary-allowed-root"', build_script)
        self.assertIn(
            "PyInstaller artifact provenance check failed",
            build_script,
        )

        portable_spec = (ROOT / "packaging" / "PR-MCP-Builder.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn('"scripts.mcp_connection_diagnostic"', portable_spec)
        self.assertIn('"scripts.refresh_mcp_client_connection"', portable_spec)
        self.assertIn("PR_MCP_BUILDER_VERSION_FILE", portable_spec)
        self.assertIn("version=version_file", portable_spec)

    def test_build_venv_recreation_has_a_fail_closed_target_contract(self) -> None:
        build_script = (ROOT / "scripts" / "build_windows_portable.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("[switch]$RecreateBuildVenv", build_script)
        self.assertIn("$RecreateBuildVenv -and $SkipExeBuild", build_script)
        self.assertIn("$PreferredBuildVenv", build_script)
        self.assertIn("function Assert-SafeBuildVenvPath", build_script)
        self.assertIn("function Test-PathWithinRoot", build_script)
        self.assertIn("[System.StringComparison]::OrdinalIgnoreCase", build_script)
        self.assertIn("[System.IO.FileAttributes]::ReparsePoint", build_script)
        self.assertIn("Remove-Item -LiteralPath $BuildVenv -Recurse -Force -ErrorAction Stop", build_script)
        self.assertIn('Join-Path $BuildRoot (".build-venv-" + [Guid]::NewGuid().ToString("N"))', build_script)
        self.assertIn("Unable to remove the existing .build-venv cleanly", build_script)

    @unittest.skipUnless(os.name == "nt", "Windows portable script requires PowerShell")
    def test_skip_exe_build_fails_closed_without_bound_executable(self) -> None:
        from app import __version__

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist_root = root / "dist"
            dist_root.mkdir()
            wheel = dist_root / f"reg_rag_preprocessor-{__version__}-py3-none-any.whl"
            wheel.write_bytes(b"synthetic wheel")
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "build_windows_portable.ps1"),
                    "-Version",
                    __version__,
                    "-BasePython",
                    sys.executable,
                    "-DistRoot",
                    str(dist_root),
                    "-BuildRoot",
                    str(root / "build"),
                    "-SkipExeBuild",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "requires a prior executable/wheel binding manifest",
            completed.stdout + completed.stderr,
        )

    @unittest.skipUnless(os.name == "nt", "Windows portable script requires PowerShell")
    def test_skip_exe_build_rejects_mixed_wheel_hash_before_execution(self) -> None:
        from app import __version__

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist_root = root / "dist"
            app_root = dist_root / "PR MCP Builder"
            app_root.mkdir(parents=True)
            executable = app_root / "PR MCP Builder.exe"
            executable.write_bytes(b"synthetic executable")
            wheel = dist_root / f"reg_rag_preprocessor-{__version__}-py3-none-any.whl"
            wheel.write_bytes(b"new wheel bytes")
            manifest = {
                "schema_version": "pr-mcp-builder-windows-artifact-v1",
                "package_version": __version__,
                "executable_name": executable.name,
                "executable_sha256": hashlib.sha256(
                    executable.read_bytes()
                ).hexdigest(),
                "wheel_name": wheel.name,
                "wheel_sha256": "0" * 64,
                "execution_probe": ["--mcp-server", "--help"],
            }
            (app_root / "release_artifact_manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "build_windows_portable.ps1"),
                    "-Version",
                    __version__,
                    "-BasePython",
                    sys.executable,
                    "-DistRoot",
                    str(dist_root),
                    "-BuildRoot",
                    str(root / "build"),
                    "-SkipExeBuild",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "refused stale or mixed executable/wheel artifacts",
            completed.stdout + completed.stderr,
        )

    @unittest.skipUnless(os.name == "nt", "Windows portable script requires PowerShell")
    def test_skip_wheel_build_requires_matching_wheel_source_binding(self) -> None:
        from app import __version__

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist_root = root / "dist"
            app_root = dist_root / "PR MCP Builder"
            app_root.mkdir(parents=True)
            executable = app_root / "PR MCP Builder.exe"
            executable.write_bytes(b"synthetic executable")
            wheel = dist_root / f"reg_rag_preprocessor-{__version__}-py3-none-any.whl"
            wheel.write_bytes(b"synthetic wheel")
            manifest = {
                "schema_version": "pr-mcp-builder-windows-artifact-v1",
                "package_version": __version__,
                "executable_name": executable.name,
                "executable_sha256": hashlib.sha256(
                    executable.read_bytes()
                ).hexdigest(),
                "wheel_name": wheel.name,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "execution_probe": ["--mcp-server", "--help"],
                "functional_pdf_parser_probe": ["--portable-self-check"],
            }
            (app_root / "release_artifact_manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "build_windows_portable.ps1"),
                    "-Version",
                    __version__,
                    "-BasePython",
                    sys.executable,
                    "-DistRoot",
                    str(dist_root),
                    "-BuildRoot",
                    str(root / "build"),
                    "-SkipExeBuild",
                    "-SkipWheelBuild",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "requires a prior wheel/source binding manifest",
            completed.stdout + completed.stderr,
        )

    def test_kordoc_portable_installer_has_fail_closed_setup_checks(self) -> None:
        installer = (ROOT / "packaging" / "INSTALL_KORDOC_KO.ps1").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertIn("Get-Command npm", installer)
        self.assertIn("npm install -g kordoc", installer)
        self.assertIn("npm prefix -g", installer)
        self.assertIn("where.exe kordoc", installer)
        self.assertIn("kordoc --version", installer)
        self.assertIn("재처리 -> 사람 승인 -> 승인하고 색인", installer)
        self.assertIn("recursive-include packaging *.py *.spec *.txt *.ps1", manifest)
        self.assertEqual(
            pyproject["tool"]["setuptools"]["data-files"]["."],
            ["packaging/INSTALL_KORDOC_KO.ps1"],
        )

    def test_readme_discloses_kordoc_source_and_bundle_scope(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("https://github.com/chrisryugj/kordoc", readme)
        self.assertIn("https://github.com/chrisryugj/kordoc/blob/main/LICENSE", readme)
        self.assertIn("Kordoc 소스나 실행 파일이 포함되지 않음", readme)
        self.assertIn("THIRD_PARTY_NOTICES.md", readme)


if __name__ == "__main__":
    unittest.main()
