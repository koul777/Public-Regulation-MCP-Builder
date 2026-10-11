from __future__ import annotations

import sys
import unittest

from scripts import run_combined_book_e2e_regression as harness


class CombinedBookHarnessMeasurementTests(unittest.TestCase):
    def test_process_peak_rss_is_measured_on_this_platform(self) -> None:
        peak_mb = harness._ru_maxrss_mb()

        self.assertIsInstance(peak_mb, float)
        self.assertGreater(peak_mb, 0.0)

    @unittest.skipUnless(sys.platform in {"win32", "linux"}, "current RSS comes from /proc or the Win32 API")
    def test_current_rss_is_measured_on_linux_and_windows(self) -> None:
        rss_kb = harness._current_rss_kb()

        self.assertIsInstance(rss_kb, int)
        self.assertGreater(rss_kb, 0)
        self.assertLessEqual(rss_kb / 1024, harness._ru_maxrss_mb() + 1.0)


if __name__ == "__main__":
    unittest.main()
