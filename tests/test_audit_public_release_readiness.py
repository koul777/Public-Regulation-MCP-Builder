from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.audit_public_release_readiness import audit_public_release, run


REQUIRED_PUBLIC_FILES = [
    "LICENSE",
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/operator_quickstart_ko.md",
    "docs/public_institution_pilot_plan.md",
    "docs/pilot_acceptance_and_evidence_ko.md",
    "docs/public-institution-operations-runbook.md",
]


def _write_public_file(root: Path, filename: str) -> None:
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


class AuditPublicReleaseReadinessTests(unittest.TestCase):
    def test_flags_missing_license_and_tracked_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "sample.hwp").write_bytes(b"sample")
            (root / "reports").mkdir()
            (root / "reports" / "public_batch_quality_20260703.json").write_text(
                '{"source_record_id":"3050658"}\n',
                encoding="utf-8",
            )

            findings = audit_public_release(
                root,
                tracked_paths=[
                    "README.md",
                    "data/sample.hwp",
                    "reports/public_batch_quality_20260703.json",
                ],
            )

        codes = {finding.code for finding in findings}
        self.assertIn("missing-license", codes)
        self.assertIn("tracked-runtime-data", codes)
        self.assertIn("tracked-document-sample", codes)
        self.assertIn("tracked-report-artifact", codes)
        self.assertIn("institution-identifier-risk", codes)

    def test_passes_source_only_public_file_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)

            findings = audit_public_release(
                root,
                tracked_paths=REQUIRED_PUBLIC_FILES,
            )

        self.assertEqual([], findings)

    def test_flags_local_claude_launch_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            launch = root / ".claude" / "launch.json"
            launch.parent.mkdir(parents=True)
            launch.write_text("{}\n", encoding="utf-8")

            findings = audit_public_release(
                root,
                tracked_paths=[*REQUIRED_PUBLIC_FILES, ".claude/launch.json"],
            )

        self.assertTrue(
            any(finding.code == "tracked-local-tooling-config" for finding in findings)
        )

    def test_flags_missing_third_party_notices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked_paths = [path for path in REQUIRED_PUBLIC_FILES if path != "THIRD_PARTY_NOTICES.md"]
            for filename in tracked_paths:
                _write_public_file(root, filename)

            findings = audit_public_release(root, tracked_paths=tracked_paths)

        self.assertTrue(
            any(
                finding.code == "missing-public-doc" and finding.path == "THIRD_PARTY_NOTICES.md"
                for finding in findings
            )
        )

    def test_allows_documented_synthetic_ocr_smoke_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            sample = root / "data" / "ocr_smoke" / "blank_ocr_required.pdf"
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(b"%PDF synthetic blank smoke")

            findings = audit_public_release(
                root,
                tracked_paths=[*REQUIRED_PUBLIC_FILES, "data/ocr_smoke/blank_ocr_required.pdf"],
            )

        self.assertEqual([], findings)

    def test_flags_private_or_internal_docs_as_nonpublic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            private_doc = root / "docs" / "private_release_runbook.md"
            private_doc.parent.mkdir(parents=True, exist_ok=True)
            private_doc.write_text("internal handoff\n", encoding="utf-8")

            findings = audit_public_release(
                root,
                tracked_paths=[
                    "LICENSE",
                    "README.md",
                    "AGENTS.md",
                    "CONTRIBUTING.md",
                    "SECURITY.md",
                    "THIRD_PARTY_NOTICES.md",
                    "docs/private_release_runbook.md",
                ],
            )

        codes = {finding.code for finding in findings}
        self.assertIn("tracked-nonpublic-doc", codes)

    def test_flags_public_doc_references_to_nonpublic_release_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "LICENSE").write_text("ok\n", encoding="utf-8")
            (root / "README.md").write_text(
                "See docs/private_release_runbook.md and docs/internal_mcp_operation_ko.md.\n",
                encoding="utf-8",
            )
            for filename in ["AGENTS.md", "CONTRIBUTING.md", "SECURITY.md"]:
                _write_public_file(root, filename)
            (root / "THIRD_PARTY_NOTICES.md").write_text("ok\n", encoding="utf-8")

            findings = audit_public_release(
                root,
                tracked_paths=REQUIRED_PUBLIC_FILES,
            )

        codes = {finding.code for finding in findings}
        self.assertIn("public-doc-nonpublic-reference", codes)
        details = "\n".join(finding.detail for finding in findings)
        self.assertIn("lines 1", details)

    def test_flags_external_directives_and_internal_handoff_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            (root / "AGENTS.md").write_text(
                "Read ../CODEX_DIRECTIVES.md and reports/overnight_sessions/session.jsonl.\n",
                encoding="utf-8",
            )

            findings = audit_public_release(root, tracked_paths=REQUIRED_PUBLIC_FILES)

        self.assertTrue(
            any(
                finding.code == "public-doc-nonpublic-reference"
                and finding.path == "AGENTS.md"
                for finding in findings
            )
        )

    def test_flags_generated_artifact_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            artifact = root / "tmp" / "runtime" / "vectors.jsonl"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}\n", encoding="utf-8")

            findings = audit_public_release(
                root,
                tracked_paths=[
                    "LICENSE",
                    "README.md",
                    "AGENTS.md",
                    "CONTRIBUTING.md",
                    "SECURITY.md",
                    "THIRD_PARTY_NOTICES.md",
                    "tmp/runtime/vectors.jsonl",
                ],
            )

        codes = {finding.code for finding in findings}
        self.assertIn("generated-artifact-path", codes)

    def test_flags_institution_specific_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            (root / "config").mkdir()
            (root / "config" / "institution_ids.txt").write_text("C0001\n", encoding="utf-8")
            (root / "config" / "institution.example.json").write_text("{}\n", encoding="utf-8")

            findings = audit_public_release(
                root,
                tracked_paths=[
                    *REQUIRED_PUBLIC_FILES,
                    "config/institution_ids.txt",
                    "config/institution.example.json",
                ],
            )

        self.assertIn("tracked-institution-config", {finding.code for finding in findings})

    def test_allows_manifest_listed_sanitized_public_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            report = root / "reports" / "public_readiness_redacted.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# Public readiness\nNo institution identifiers.\n", encoding="utf-8")
            manifest = root / "docs" / "public_release_report_allowlist.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                '{"allowed_reports":["reports/public_readiness_redacted.md"]}\n',
                encoding="utf-8",
            )

            findings = audit_public_release(
                root,
                tracked_paths=[
                    *REQUIRED_PUBLIC_FILES,
                    "docs/public_release_report_allowlist.json",
                    "reports/public_readiness_redacted.md",
                ],
            )

        self.assertEqual([], findings)

    def test_allowed_public_report_still_scans_risk_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            report = root / "reports" / "public_readiness_redacted.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('{"source_record_id":"3050658"}\n', encoding="utf-8")
            manifest = root / "docs" / "public_release_report_allowlist.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                '{"allowed_reports":["reports/public_readiness_redacted.md"]}\n',
                encoding="utf-8",
            )

            findings = audit_public_release(
                root,
                tracked_paths=[
                    "LICENSE",
                    "README.md",
                    "AGENTS.md",
                    "CONTRIBUTING.md",
                    "SECURITY.md",
                    "THIRD_PARTY_NOTICES.md",
                    "docs/public_release_report_allowlist.json",
                    "reports/public_readiness_redacted.md",
                ],
            )

        codes = {finding.code for finding in findings}
        self.assertNotIn("tracked-report-artifact", codes)
        self.assertIn("institution-identifier-risk", codes)

    def test_include_untracked_preview_counts_local_public_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)

            stdout = io.StringIO()
            exit_code = run(["--root", str(root), "--include-untracked", "--json"], stdout=stdout)

        self.assertEqual(0, exit_code)
        self.assertIn('"passed": true', stdout.getvalue())
        self.assertIn('"generated_at"', stdout.getvalue())
        self.assertIn('"repo_commit"', stdout.getvalue())

    def test_include_untracked_preview_respects_gitignore_for_generated_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text("reports/*\n", encoding="utf-8")
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            report = root / "reports" / "public_release_gate_ci.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('{"source_record_id":"should-be-ignored"}\n', encoding="utf-8")

            stdout = io.StringIO()
            exit_code = run(["--root", str(root), "--include-untracked", "--json"], stdout=stdout)

        self.assertEqual(0, exit_code)
        payload = stdout.getvalue()
        self.assertIn('"passed": true', payload)
        self.assertNotIn("public_release_gate_ci.json", payload)

    def test_include_untracked_preview_flags_unicode_data_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            for filename in REQUIRED_PUBLIC_FILES:
                _write_public_file(root, filename)
            sample = root / "data" / "기관" / "규정.pdf"
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(b"%PDF public-risk sample")

            stdout = io.StringIO()
            exit_code = run(["--root", str(root), "--include-untracked", "--json"], stdout=stdout)

        self.assertEqual(1, exit_code)
        payload = stdout.getvalue()
        self.assertIn("tracked-runtime-data", payload)
        self.assertIn("data/기관/규정.pdf", payload)


if __name__ == "__main__":
    unittest.main()
