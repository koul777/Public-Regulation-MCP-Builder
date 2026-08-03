from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import check_build_environment_isolation as isolation


class BuildEnvironmentIsolationTests(unittest.TestCase):
    def test_artifact_sources_pass_only_under_explicit_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            report = isolation.evaluate_artifact_source_provenance(
                source_paths=(allowed / "module.py", allowed / "native.dll"),
                allowed_roots=(allowed,),
            )

        self.assertTrue(report["artifact_sources_within_allowed_roots"])
        self.assertEqual(2, report["checked_artifact_source_count"])
        self.assertEqual(0, report["external_artifact_source_count"])

    def test_artifact_sources_fail_without_exposing_external_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            external = root / "private-python" / "native.dll"
            report = isolation.evaluate_artifact_source_provenance(
                source_paths=(allowed / "module.py", external),
                allowed_roots=(allowed,),
            )

        self.assertFalse(report["artifact_sources_within_allowed_roots"])
        self.assertEqual(1, report["external_artifact_source_count"])
        self.assertNotIn(str(external), json.dumps(report))

    def test_artifact_source_allows_an_exact_path_without_allowing_children(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = isolation.evaluate_artifact_source_provenance(
                source_paths=(root, root / ".venv" / "package.py"),
                allowed_roots=(),
                allowed_paths=(root,),
            )

        self.assertEqual(2, report["checked_artifact_source_count"])
        self.assertEqual(1, report["external_artifact_source_count"])

    def test_analysis_toc_is_parsed_as_data_and_checks_nested_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            toc = root / "Analysis-00.toc"
            toc.write_text(
                repr(([str(allowed / "module.py")], [("dll", str(allowed / "x.dll"), "BINARY")])),
                encoding="utf-8",
            )
            report = isolation.inspect_pyinstaller_analysis_toc(
                toc,
                allowed_roots=(allowed,),
            )

        self.assertTrue(report["artifact_sources_within_allowed_roots"])
        self.assertEqual(2, report["checked_artifact_source_count"])

    def test_binary_sources_require_absolute_allowed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            outside = root / "outside"
            report = isolation.evaluate_binary_source_provenance(
                source_paths=(
                    allowed / "good.dll",
                    outside / "bad.pyd",
                    "relative.dll",
                ),
                allowed_roots=(allowed,),
            )

        self.assertFalse(report["binary_sources_within_allowed_roots"])
        self.assertEqual(1, report["external_binary_source_count"])
        self.assertEqual(1, report["relative_binary_source_count"])
        self.assertNotIn(str(outside), json.dumps(report))

    def test_binary_toc_checks_only_binary_and_extension_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            toc = root / "COLLECT-00.toc"
            toc.write_text(
                repr(
                    ([
                        ("native.dll", str(allowed / "native.dll"), "BINARY"),
                        ("module.pyd", str(allowed / "module.pyd"), "EXTENSION"),
                        ("docs.txt", str(root / "outside" / "docs.txt"), "DATA"),
                    ],)
                ),
                encoding="utf-8",
            )
            report = isolation.inspect_pyinstaller_binary_tocs(
                (toc,),
                allowed_roots=(allowed,),
            )

        self.assertTrue(report["binary_sources_within_allowed_roots"])
        self.assertEqual(2, report["checked_binary_source_count"])

    def test_passes_when_distributions_and_required_modules_are_venv_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            venv = Path(temp_dir) / ".build-venv"
            site_packages = venv / "Lib" / "site-packages"
            report = isolation.evaluate_build_environment_isolation(
                venv_root=venv,
                python_prefix=venv,
                distribution_roots=(site_packages, site_packages / "example"),
                module_files={
                    "fitz": None,
                    "pymupdf": site_packages / "pymupdf" / "__init__.py",
                },
            )

        self.assertTrue(report["passed"])
        self.assertEqual("ok", report["reason_code"])
        self.assertEqual(0, report["external_distribution_count"])
        self.assertEqual(0, report["external_module_count"])

    def test_fails_when_any_distribution_is_outside_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            venv = root / ".build-venv"
            site_packages = venv / "Lib" / "site-packages"
            report = isolation.evaluate_build_environment_isolation(
                venv_root=venv,
                python_prefix=venv,
                distribution_roots=(site_packages, root / "developer-python"),
                module_files={
                    "fitz": None,
                    "pymupdf": site_packages / "pymupdf" / "__init__.py",
                },
            )

        self.assertFalse(report["passed"])
        self.assertEqual("build_dependency_outside_venv", report["reason_code"])
        self.assertEqual(1, report["external_distribution_count"])

    def test_fails_when_required_backend_file_is_outside_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            venv = root / ".build-venv"
            site_packages = venv / "Lib" / "site-packages"
            report = isolation.evaluate_build_environment_isolation(
                venv_root=venv,
                python_prefix=venv,
                distribution_roots=(site_packages,),
                module_files={
                    "fitz": None,
                    "pymupdf": root / "developer-python" / "pymupdf" / "__init__.py",
                },
            )

        self.assertFalse(report["passed"])
        self.assertEqual("build_dependency_outside_venv", report["reason_code"])
        self.assertEqual(1, report["external_module_count"])

    def test_cli_exit_is_fail_closed_and_does_not_expose_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sensitive_root = root / "private-python"
            venv = root / "project" / ".build-venv"
            report = isolation.evaluate_build_environment_isolation(
                venv_root=venv,
                python_prefix=venv,
                distribution_roots=(sensitive_root,),
                module_files={
                    "fitz": None,
                    "pymupdf": sensitive_root / "pymupdf" / "__init__.py",
                },
            )
            stdout = io.StringIO()
            with (
                patch.object(
                    isolation,
                    "inspect_current_environment",
                    return_value=report,
                ),
                redirect_stdout(stdout),
            ):
                exit_code = isolation.main(
                    ["--venv-root", str(venv), "--fail-on-issue"]
                )

            output = stdout.getvalue()
        payload = json.loads(output)
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["passed"])
        self.assertEqual("build_dependency_outside_venv", payload["reason_code"])
        self.assertNotIn(str(sensitive_root), output)
        self.assertNotIn(temp_dir, output)


if __name__ == "__main__":
    unittest.main()
