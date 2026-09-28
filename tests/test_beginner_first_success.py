from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_beginner_first_success import (
    _check_environment,
    _check_synthetic_sample,
    _run_stdio_contract,
    run_beginner_first_success,
)


class BeginnerFirstSuccessTests(unittest.TestCase):
    def test_environment_and_sample_checks_are_path_free(self) -> None:
        environment = _check_environment()
        sample = _check_synthetic_sample()

        self.assertTrue(environment["passed"])
        self.assertTrue(sample["passed"])
        self.assertTrue(sample["redistributable"])
        self.assertNotIn("C:\\", json.dumps(environment, ensure_ascii=False))
        self.assertNotIn("C:\\", json.dumps(sample, ensure_ascii=False))

    def test_stdio_contract_maps_beginner_actions(self) -> None:
        transport_report = {
            "passed": True,
            "preparation": {"synthetic_runtime": True},
            "full_profile": {
                "mcp_initialized": True,
                "fetch_has_text": True,
            },
            "chatgpt_data_profile": {
                "catalog_verified": True,
                "search_result_count": 1,
                "fetch_has_text": True,
                "hierarchy_verified": True,
            },
        }
        with patch(
            "scripts.run_beginner_first_success.run_mcp_transport_smoke",
            return_value=transport_report,
        ):
            result = _run_stdio_contract(timeout_seconds=1)

        self.assertTrue(result["passed"])
        self.assertTrue(result["list_regulations"])
        self.assertTrue(result["search"])
        self.assertTrue(result["fetch"])
        self.assertTrue(result["tenant_isolation"])

    def test_runner_can_write_a_small_report_and_optional_llm_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "first-success.json"
            with patch(
                "scripts.run_beginner_first_success.run_mcp_transport_smoke",
                return_value={
                    "passed": True,
                    "preparation": {"synthetic_runtime": True},
                    "full_profile": {"mcp_initialized": True, "fetch_has_text": True},
                    "chatgpt_data_profile": {
                        "catalog_verified": True,
                        "search_result_count": 1,
                        "fetch_has_text": True,
                        "hierarchy_verified": True,
                    },
                },
            ), patch(
                "scripts.run_beginner_first_success.diagnose_local_llm",
                return_value={"passed": False, "reason": "local_backend_unavailable"},
            ):
                report = run_beginner_first_success(
                    verify_local_llm=True,
                    out_json=output,
                )

            self.assertFalse(report["passed"])
            self.assertTrue(output.is_file())
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(saved["passed"])
            self.assertEqual("local_backend_unavailable", saved["stages"][-1]["reason"])
            self.assertNotIn(str(Path(tmp)), output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
