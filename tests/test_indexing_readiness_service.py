from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.indexing_readiness_service import (
    IndexingPackageStatus,
    check_indexing_packages,
    indexing_preparation_guidance,
    prepare_indexing_runtime,
)


class IndexingReadinessServiceTests(unittest.TestCase):
    def _result(self, payload=None, *, code=0):
        return SimpleNamespace(
            returncode=code,
            stdout="private diagnostic\nINDEXING_READY:" + json.dumps(
                {"ready": True, "dimensions": 384} if payload is None else payload
            ),
            stderr="private diagnostic",
        )

    def test_package_check_has_no_model_import_or_process_side_effect(self) -> None:
        with patch("app.services.indexing_readiness_service.importlib.util.find_spec",
                   side_effect=lambda module: None if module == "torch" else object()) as find, \
             patch("app.services.indexing_readiness_service.subprocess.run") as run:
            status = check_indexing_packages()
        self.assertEqual(("torch",), status.missing_packages)
        self.assertFalse(status.available)
        self.assertEqual(3, find.call_count)
        run.assert_not_called()

    def test_invalid_module_spec_and_frozen_runtime_are_not_ready(self) -> None:
        with patch("app.services.indexing_readiness_service.importlib.util.find_spec", side_effect=ValueError), \
             patch("app.services.indexing_readiness_service.sys.frozen", True, create=True):
            status = check_indexing_packages()
        self.assertEqual(3, len(status.missing_packages))
        self.assertFalse(status.can_prepare)

    def test_installed_packages_still_require_a_successful_model_probe(self) -> None:
        runner = Mock(return_value=self._result())
        with patch("app.services.indexing_readiness_service.check_indexing_packages", return_value=IndexingPackageStatus()):
            result = prepare_indexing_runtime(runner=runner)
        self.assertTrue(result.ready)
        runner.assert_called_once()
        command = runner.call_args.args[0]
        self.assertEqual("-c", command[1])
        self.assertIn("Local indexing readiness check.", command[2])
        self.assertNotIn("shell", runner.call_args.kwargs)
        self.assertEqual(subprocess.DEVNULL, runner.call_args.kwargs["stdin"])
        self.assertNotIn("private", result.model_dump_json())

    def test_missing_packages_install_fixed_requirement_then_probe(self) -> None:
        runner = Mock(side_effect=[self._result(), self._result()])
        with patch("app.services.indexing_readiness_service.check_indexing_packages",
                   return_value=IndexingPackageStatus(missing_packages=("torch",))):
            result = prepare_indexing_runtime(runner=runner)
        self.assertTrue(result.ready)
        self.assertEqual(2, runner.call_count)
        self.assertEqual(["-m", "pip", "install", "--disable-pip-version-check", "sentence-transformers>=5.0,<6.0"],
                         runner.call_args_list[0].args[0][1:])

    def test_failed_install_never_probes_or_returns_captured_output(self) -> None:
        runner = Mock(return_value=self._result(code=1))
        with patch("app.services.indexing_readiness_service.check_indexing_packages",
                   return_value=IndexingPackageStatus(missing_packages=("torch",))):
            result = prepare_indexing_runtime(runner=runner)
        self.assertFalse(result.ready)
        self.assertEqual("package_install_failed", result.reason)
        runner.assert_called_once()
        self.assertNotIn("private", result.model_dump_json())

    def test_invalid_or_failed_probe_never_proves_readiness(self) -> None:
        for response in (self._result({"ready": False, "dimensions": 384}),
                         self._result({"ready": "true", "dimensions": 384}),
                         self._result({"ready": True, "dimensions": 0}),
                         self._result([]), self._result(code=1),
                         SimpleNamespace(returncode=0, stdout="private diagnostic")):
            with self.subTest(response=response), \
                 patch("app.services.indexing_readiness_service.check_indexing_packages", return_value=IndexingPackageStatus()):
                result = prepare_indexing_runtime(runner=Mock(return_value=response))
                self.assertFalse(result.ready)
                self.assertNotIn("private", result.model_dump_json())

    def test_timeout_and_launch_error_provide_safe_recovery(self) -> None:
        for error, reason in ((subprocess.TimeoutExpired("private", 900), "setup_timeout"),
                              (OSError("private"), "setup_unavailable")):
            with self.subTest(reason=reason), \
                 patch("app.services.indexing_readiness_service.check_indexing_packages", return_value=IndexingPackageStatus()):
                result = prepare_indexing_runtime(runner=Mock(side_effect=error))
                self.assertFalse(result.ready)
                self.assertEqual(reason, result.reason)
                self.assertNotIn("private", indexing_preparation_guidance(result.reason))

    def test_frozen_runtime_cannot_start_itself_as_python(self) -> None:
        runner = Mock()
        with patch("app.services.indexing_readiness_service.check_indexing_packages",
                   return_value=IndexingPackageStatus(can_prepare=False)):
            result = prepare_indexing_runtime(runner=runner)
        self.assertEqual("managed_runtime_required", result.reason)
        self.assertFalse(result.ready)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
