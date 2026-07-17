from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validate_quality_profiles import main, validate_quality_profiles


class ValidateQualityProfilesTests(unittest.TestCase):
    def test_validates_quality_profile_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "default": {"coverage_ratio_min": 0.8, "coverage_ratio_max": 1.3},
                        "profiles": {
                            "strict": {
                                "coverage_ratio_min": 0.95,
                                "coverage_ratio_max": 1.05,
                                "table_false_positive_attention_max_count": 2,
                                "table_false_positive_attention_max_ratio": 0.05,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = validate_quality_profiles(path)

        self.assertTrue(report["valid"])
        self.assertEqual(len(report["sha256"]), 64)
        self.assertEqual(report["profile_count"], 1)
        self.assertEqual(report["profiles"]["strict"]["coverage_threshold_label"], "0.95-1.05")

    def test_main_returns_failure_json_for_invalid_profile_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality_profiles.json"
            path.write_text(json.dumps({"default": {"unknown": 1}}), encoding="utf-8")
            with patch("sys.argv", ["validate_quality_profiles.py", str(path)]), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as stdout:
                exit_code = main()

        self.assertEqual(exit_code, 2)
        output = json.loads(stdout.getvalue())
        self.assertFalse(output["valid"])
        self.assertIn("unknown fields", output["error"])


if __name__ == "__main__":
    unittest.main()
