from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.check_installed_console_scripts import DEFAULT_COMMANDS, check_installed_console_scripts, run


ROOT = Path(__file__).resolve().parents[1]


class CheckInstalledConsoleScriptsTests(unittest.TestCase):
    def test_reports_missing_console_script(self) -> None:
        report = check_installed_console_scripts(
            commands=["definitely-not-installed-reg-rag-command"],
            run_help=False,
        )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["high_count"])
        self.assertEqual("console-script-missing", report["issues"][0]["code"])

    def test_accepts_visible_command_with_help(self) -> None:
        report = check_installed_console_scripts(commands=[sys.executable], run_help=True)

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["issue_count"])
        self.assertTrue(report["checked"][0]["help_checked"])

    def test_cli_writes_json_and_returns_nonzero_when_requested(self) -> None:
        stdout = io.StringIO()

        exit_code = run(
            [
                "--command",
                "definitely-not-installed-reg-rag-command",
                "--skip-help",
                "--fail-on-issue",
                "--json",
            ],
            stdout=stdout,
        )

        self.assertEqual(1, exit_code)
        self.assertIn('"console-script-missing"', stdout.getvalue())

    def test_default_commands_cover_declared_console_scripts(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(set(pyproject["project"]["scripts"]), set(DEFAULT_COMMANDS))

    @unittest.skipUnless(
        os.environ.get("REG_RAG_RUN_INSTALLED_PACKAGE_SMOKE") == "1",
        "set REG_RAG_RUN_INSTALLED_PACKAGE_SMOKE=1 to build/install the wheel and check installed console scripts",
    )
    def test_installed_wheel_exposes_declared_console_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheelhouse = root / "wheelhouse"
            venv = root / "venv"
            wheelhouse.mkdir()
            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    ".",
                    "--no-deps",
                    "--no-build-isolation",
                    "-w",
                    str(wheelhouse),
                ],
                cwd=ROOT,
                timeout=240,
            )
            wheels = sorted(wheelhouse.glob("reg_rag_preprocessor-*.whl"))
            self.assertTrue(wheels)
            _run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], timeout=120)
            venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            scripts_dir = venv_python.parent
            _run([str(venv_python), "-m", "pip", "install", "--no-deps", str(wheels[-1])], timeout=120)

            env = os.environ.copy()
            env["PATH"] = str(scripts_dir) + os.pathsep + env.get("PATH", "")
            command = shutil.which("reg-rag-check-console-scripts", path=env["PATH"])
            self.assertIsNotNone(command)
            completed = _run(
                [
                    str(command),
                    "--skip-help",
                    "--json",
                    "--fail-on-issue",
                ],
                env=env,
                timeout=120,
            )
            report = json.loads(completed.stdout)

        self.assertTrue(report["passed"])
        self.assertEqual(set(DEFAULT_COMMANDS), {item["command"] for item in report["checked"]})


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Command failed with exit code {code}: {cmd}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}".format(
                code=completed.returncode,
                cmd=" ".join(args),
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
    return completed


if __name__ == "__main__":
    unittest.main()
