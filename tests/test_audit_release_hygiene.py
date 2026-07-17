from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import audit_release_hygiene as audit


class AuditReleaseHygieneTests(unittest.TestCase):
    def test_detects_local_path_leak_in_text_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path("dist/report.json")
            artifact = root / rel_path
            artifact.parent.mkdir(parents=True)
            leaked_path = "/" + "home/alice/project/private-cache.db"
            artifact.write_text(f'{{"source": "{leaked_path}"}}\n', encoding="utf-8")

            findings = audit.audit_paths(root, [rel_path], max_file_bytes=1024)

        self.assertEqual(["local-path-leak"], [finding.code for finding in findings])
        self.assertEqual("dist/report.json", findings[0].path)
        self.assertIn("posix-user-path", findings[0].detail)

    def test_detects_oversized_checked_in_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path("release/blob.bin")
            artifact = root / rel_path
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"abcdef")

            findings = audit.audit_paths(root, [rel_path], max_file_bytes=5)

        self.assertEqual(["oversized-file"], [finding.code for finding in findings])
        self.assertIn("exceeds configured limit", findings[0].detail)

    def test_detects_workflow_file_only_when_scope_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path(".github/workflows/release.yml")
            workflow = root / rel_path
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: release\n", encoding="utf-8")

            available_findings = audit.audit_paths(
                root,
                [rel_path],
                workflow_scope_unavailable=False,
            )
            unavailable_findings = audit.audit_paths(
                root,
                [rel_path],
                workflow_scope_unavailable=True,
            )

        self.assertEqual([], available_findings)
        self.assertEqual(
            ["workflow-file-without-scope"],
            [finding.code for finding in unavailable_findings],
        )

    def test_detects_staged_workflow_deletion_when_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path(".github/workflows/release.yml")

            findings = audit.audit_paths(
                root,
                [rel_path],
                workflow_scope_unavailable=True,
            )

        self.assertEqual(
            ["workflow-file-without-scope"],
            [finding.code for finding in findings],
        )
        self.assertEqual(".github/workflows/release.yml", findings[0].path)

    def test_collect_candidate_paths_includes_staged_and_worktree_diffs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls: list[tuple[str, ...]] = []

            def fake_git_null_list(_root: Path, args: list[str]) -> list[str]:
                calls.append(tuple(args))
                if args == ["ls-files", "-z", "--cached"]:
                    return ["README.md"]
                if args == ["diff", "-z", "--name-only", "--cached", "--diff-filter=ACMRTD"]:
                    return [".github/workflows/ci.yml"]
                if args == ["diff", "-z", "--name-only", "--diff-filter=ACMRTD"]:
                    return ["docs/private_release_checklist.md"]
                return []

            with mock.patch.object(audit, "_git_null_list", side_effect=fake_git_null_list):
                paths = audit.collect_candidate_paths(root)

        self.assertEqual(
            [
                ("ls-files", "-z", "--cached"),
                ("diff", "-z", "--name-only", "--cached", "--diff-filter=ACMRTD"),
                ("diff", "-z", "--name-only", "--diff-filter=ACMRTD"),
            ],
            calls,
        )
        self.assertEqual(
            ["README.md", ".github/workflows/ci.yml", "docs/private_release_checklist.md"],
            paths,
        )

    def test_source_path_literals_are_ignored_by_default_path_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path("tests/test_fixture.py")
            artifact = root / rel_path
            artifact.parent.mkdir(parents=True)
            artifact.write_text('path = r"C:\\Users\\dd\\Desktop\\secret.pdf"\n', encoding="utf-8")

            default_findings = audit.audit_paths(root, [rel_path], max_file_bytes=1024)
            strict_findings = audit.audit_paths(
                root,
                [rel_path],
                max_file_bytes=1024,
                include_source_path_scan=True,
            )

        self.assertEqual([], default_findings)
        self.assertEqual(["local-path-leak"], [finding.code for finding in strict_findings])

    def test_allowlist_suppresses_documented_intentional_findings(self):
        finding = audit.Finding(
            code="local-path-leak",
            path="tests/test_fixture.py",
            detail="windows-user-path on line 1: C:\\Users\\dd\\secret.pdf",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            allowlist_path = Path(temp_dir) / ".release-hygiene-allowlist.json"
            allowlist_path.write_text(
                json.dumps(
                    {
                        "allowed_findings": [
                            {
                                "code": "local-path-leak",
                                "path": "tests/test_fixture.py",
                                "reason": "intentional redaction fixture",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            filtered = audit.filter_allowed_findings([finding], audit.load_allowlist(allowlist_path))

        self.assertEqual([], filtered)

    def test_allowlist_loader_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            allowlist_path = Path(temp_dir) / ".release-hygiene-allowlist.json"
            allowlist_path.write_text(
                json.dumps(
                    {
                        "allowed_findings": [
                            {
                                "code": "local-path-leak",
                                "path": "tests/test_fixture.py",
                                "reason": "intentional redaction fixture",
                            }
                        ]
                    }
                ),
                encoding="utf-8-sig",
            )

            rules = audit.load_allowlist(allowlist_path)

        self.assertEqual("local-path-leak", rules[0]["code"])
        self.assertEqual("tests/test_fixture.py", rules[0]["path"])

    def test_auto_workflow_scope_uses_token_scope_environment(self):
        self.assertTrue(audit.workflow_scope_is_unavailable("auto", {"GH_TOKEN_SCOPES": "repo"}))
        self.assertFalse(
            audit.workflow_scope_is_unavailable("auto", {"GH_TOKEN_SCOPES": "repo workflow"})
        )

    def test_cli_reports_findings_from_collected_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path("dist/report.txt")
            artifact = root / rel_path
            artifact.parent.mkdir(parents=True)
            artifact.write_text("cached at " + "/" + "home/alice/project/out\n", encoding="utf-8")
            out_json = root / "reports" / "release_hygiene_current.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(audit, "collect_candidate_paths", return_value=[rel_path]):
                exit_code = audit.run(
                    ["--root", str(root), "--workflow-scope", "available", "--out-json", str(out_json)],
                    stdout=stdout,
                    stderr=stderr,
                    env={},
                )
            written = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())
        self.assertIn("local-path-leak", stdout.getvalue())
        self.assertFalse(written["passed"])
        self.assertEqual(1, written["raw_finding_count"])
        self.assertEqual(0, written["suppressed_finding_count"])
        self.assertEqual(1, written["finding_count"])
        self.assertEqual("local-path-leak", written["findings"][0]["code"])

    def test_cli_out_json_discloses_allowlist_suppressions_without_raw_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rel_path = Path("tests/test_fixture.py")
            artifact = root / rel_path
            artifact.parent.mkdir(parents=True)
            artifact.write_text('path = r"C:\\Users\\dd\\Desktop\\secret.pdf"\n', encoding="utf-8")
            allowlist_path = root / ".release-hygiene-allowlist.json"
            allowlist_path.write_text(
                json.dumps(
                    {
                        "allowed_findings": [
                            {
                                "code": "local-path-leak",
                                "path": "tests/test_fixture.py",
                                "reason": "intentional test fixture",
                                "approved_by": "koul777",
                                "approved_at": "2026-07-07",
                                "approval_reference": "TEST-ALLOWLIST",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            out_json = root / "reports" / "release_hygiene_current.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(audit, "collect_candidate_paths", return_value=[rel_path]):
                exit_code = audit.run(
                    [
                        "--root",
                        str(root),
                        "--include-source-path-scan",
                        "--workflow-scope",
                        "available",
                        "--out-json",
                        str(out_json),
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    env={},
                )
            written = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertTrue(written["passed"])
        self.assertEqual(1, written["raw_finding_count"])
        self.assertEqual(1, written["suppressed_finding_count"])
        self.assertEqual({"local-path-leak": 1}, written["suppressed_findings_by_code"])
        self.assertEqual(".release-hygiene-allowlist.json", written["allowlist"]["path"])
        self.assertRegex(written["allowlist"]["sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(0, written["allowlist"]["missing_approval_metadata_count"])
        self.assertEqual(0, written["allowlist"]["non_attributable_approval_count"])
        serialized = json.dumps(written, ensure_ascii=False)
        self.assertNotIn("Desktop", serialized)
        self.assertNotIn("secret.pdf", serialized)

    def test_cli_writes_out_json_on_audit_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out_json = root / "reports" / "release_hygiene_current.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(audit, "collect_candidate_paths", side_effect=audit.AuditError("git failed")):
                exit_code = audit.run(
                    ["--root", str(root), "--out-json", str(out_json)],
                    stdout=stdout,
                    stderr=stderr,
                    env={},
                )
            written = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertEqual(2, exit_code)
        self.assertIn("release hygiene audit error", stderr.getvalue())
        self.assertFalse(written["passed"])
        self.assertEqual("audit_error", written["error_type"])


if __name__ == "__main__":
    unittest.main()
