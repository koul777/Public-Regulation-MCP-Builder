from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.run_sdist_rehearsal import build_sdist_rehearsal_report, run


class SdistRehearsalTests(unittest.TestCase):
    def test_runs_tests_from_unpacked_sdist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "reg_rag_preprocessor-0.1.0"
            test_dir = source / "tests"
            test_dir.mkdir(parents=True)
            (test_dir / "__init__.py").write_text("", encoding="utf-8")
            (test_dir / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            sdist = root / "reg_rag_preprocessor-0.1.0.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                archive.add(source, arcname=source.name)

            report = build_sdist_rehearsal_report(
                project_root=root,
                sdist_path=sdist,
                tests=("tests.test_smoke",),
            )

        self.assertTrue(report["passed"])
        self.assertEqual(0, report["exit_code"])
        self.assertEqual([], report["issues"])

    def test_reports_missing_sdist_as_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = build_sdist_rehearsal_report(project_root=tmp, sdist_path="missing.tar.gz")

        self.assertFalse(report["passed"])
        self.assertEqual("sdist-rehearsal-error", report["issues"][0]["code"])

    def test_resolves_latest_sdist_from_explicit_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "reg_rag_preprocessor-0.1.0"
            test_dir = source / "tests"
            test_dir.mkdir(parents=True)
            (test_dir / "__init__.py").write_text("", encoding="utf-8")
            (test_dir / "test_smoke.py").write_text(
                "import unittest\nclass SmokeTests(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            dist = root / "reports" / "run" / "dist"
            dist.mkdir(parents=True)
            sdist = dist / "reg_rag_preprocessor-0.1.0.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                archive.add(source, arcname=source.name)

            report = build_sdist_rehearsal_report(
                project_root=root,
                sdist_dir=dist,
                tests=("tests.test_smoke",),
            )

        self.assertTrue(report["passed"])
        self.assertEqual(str(sdist.resolve()), report["sdist_path"])

    def test_cli_writes_json_and_can_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "sdist_rehearsal.json"
            stdout = io.StringIO()

            exit_code = run(
                [
                    "--project-root",
                    tmp,
                    "--sdist",
                    "missing.tar.gz",
                    "--out-json",
                    str(out_json),
                    "--json",
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(1, exit_code)
        self.assertEqual("sdist_rehearsal", payload["report_type"])
        self.assertIn('"passed": false', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
