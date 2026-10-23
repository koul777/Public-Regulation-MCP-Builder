from __future__ import annotations

import unittest

from scripts.mcp_bundle_contract import (
    MAX_BUNDLE_ARCHIVE_ENTRIES,
    MAX_BUNDLE_ARCHIVE_ENTRY_BYTES,
    MAX_BUNDLE_ARCHIVE_TOTAL_BYTES,
    validate_bundle_archive_sizes,
)


class McpBundleArchiveContractTests(unittest.TestCase):
    def test_combined_book_sized_artifacts_fit_a_bounded_contract(self) -> None:
        validate_bundle_archive_sizes([
            ("data/repository/doc_example_chunks.json", 192 * 1024 * 1024),
            ("data/vector_db/default/approved_vectors.jsonl", 112 * 1024 * 1024),
            ("data/hierarchy/regulation_hierarchy.sqlite3", 48 * 1024 * 1024),
        ])
        self.assertEqual(2048, MAX_BUNDLE_ARCHIVE_ENTRIES)
        self.assertEqual(256 * 1024 * 1024, MAX_BUNDLE_ARCHIVE_ENTRY_BYTES)
        self.assertEqual(512 * 1024 * 1024, MAX_BUNDLE_ARCHIVE_TOTAL_BYTES)

    def test_entry_total_and_count_limits_fail_closed(self) -> None:
        scenarios = [
            ([("a", 9)], {"max_entry_bytes": 8}, "too large"),
            ([("a", 8), ("b", 8)], {"max_total_bytes": 15}, "uncompressed size limit"),
            ([("a", 0), ("b", 0)], {"max_entries": 1}, "too many entries"),
            ([("a", -1)], {}, "invalid size"),
            ([("a", True)], {}, "invalid size"),
        ]
        for members, limits, message in scenarios:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_bundle_archive_sizes(members, **limits)

    def test_invalid_limits_cannot_disable_bounds(self) -> None:
        for limit in (0, -1, True, float("inf")):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "positive integers"):
                validate_bundle_archive_sizes([], max_entry_bytes=limit)


if __name__ == "__main__":
    unittest.main()
