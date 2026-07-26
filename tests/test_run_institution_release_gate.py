from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts.run_institution_release_gate import main, parse_args


class RunInstitutionReleaseGateTests(unittest.TestCase):
    def test_parse_args_normalizes_required_scope_ids(self) -> None:
        args = parse_args(
            [
                "--tenant-id",
                " tenant-a ",
                "--profile-id",
                " profile-a ",
                "--skip-local-smoke",
            ]
        )

        self.assertEqual("tenant-a", args.tenant_id)
        self.assertEqual("profile-a", args.profile_id)

    def test_whitespace_scope_ids_are_rejected_before_run_dir_creation(self) -> None:
        invalid_scope_args = (
            ("--tenant-id", "   ", "--profile-id", "profile-a"),
            ("--tenant-id", "tenant-a", "--profile-id", "\t"),
        )
        for tenant_flag, tenant_id, profile_flag, profile_id in invalid_scope_args:
            with self.subTest(tenant_id=tenant_id, profile_id=profile_id):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "release-evidence"
                    stderr = io.StringIO()

                    with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        main(
                            [
                                tenant_flag,
                                tenant_id,
                                profile_flag,
                                profile_id,
                                "--skip-local-smoke",
                                "--run-dir",
                                str(run_dir),
                            ]
                        )

                    self.assertEqual(2, raised.exception.code)
                    self.assertIn("expected a non-empty scope identifier", stderr.getvalue())
                    self.assertFalse(run_dir.exists())


if __name__ == "__main__":
    unittest.main()
