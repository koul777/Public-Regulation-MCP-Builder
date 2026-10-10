"""Equivalence tests for the optimized vector-record local-path leak scan.

``vector_record_path_leaks`` is a security gate that blocks local filesystem
paths from reaching the vector index.  Its implementation was optimized (one
combined regex behind a "/" or "\\" prefilter, no per-string path formatting).
These tests pin the optimized scan to a verbatim copy of the original
implementation so that every string flagged before is still flagged, with the
same field path, order and truncation, and nothing new is flagged.
"""

from __future__ import annotations

import itertools
import random
import re
import unittest
from typing import Any, Iterable

from app.ingestion import vector_adapter
from app.ingestion.vector_adapter import vector_record_path_leaks


# --- verbatim copy of the pre-optimization implementation -------------------
_REFERENCE_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:[\\/][^\s\"']+"),
    re.compile(r"(?i)\bfile://[^\s\"']+"),
    re.compile(r"(?i)(?:^|[\s\"'])(?:/app/|/data/|/home/|/mnt/|/tmp/|/usr/src/app/|/users/|/var/|/workspace/)[^\s\"']+"),
    re.compile(r"(?i)(?:^|[\s\"'])\\\\[^\\/\s]+[\\/][^\s\"']+"),
)


def _reference_looks_like_local_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in _REFERENCE_PATTERNS)


def _reference_iter_string_values(value: Any, *, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _reference_iter_string_values(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _reference_iter_string_values(item, path=f"{path}.{key}")


def _reference_path_leaks(records: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    for record in records:
        for path, value in _reference_iter_string_values(record):
            if _reference_looks_like_local_path(value):
                leaks.append({"id": str(record.get("id", "")), "field_path": path, "value": value[:240]})
    return leaks


# Fragments chosen to hit every pattern, their boundary conditions, the four
# non-ASCII letters that IGNORECASE folds onto ASCII ones (dotless/dotted i,
# long s, Kelvin sign), Korean text and URLs that must not be flagged.
_FRAGMENTS = [
    "C:\\", "c:/", "D:\\Users\\x\\a.docx", "Z:/data", "z:", "1:\\", "한C:\\", "한 C:\\x", "(C:\\x)",
    "\\\\server\\share\\f", "\\\\server", "\\\\", "\\\\\\", "\\\\a\\", "\\\\a/b", " \\\\h\\s\\f", "\"\\\\h\\s\\f",
    "//", "///", "file://", "FILE:///etc/passwd", "fıle://x", "fİle://x", "file:/", "file:///C:/x", "xfile://a",
    "/app/x", "/data/x", "/home/u/a", "/mnt/c", "/tmp/x", "/usr/src/app/x", "/users/x", "/Users/x", "/USERS/x",
    "/uſers/x", "/workſpace/x", "/worKspace/x", "/workspace/x", "/var/log", "/var", "/var/", "/appx", "/app/",
    " /home/a", "\"/home/a", "'/home/a", "\t/tmp/x", "\n/tmp/x", "a/home/b", "x/tmp/y",
    "http://example.com/a/b", "https://x.y/z?q=1", "http://localhost:8080/", "ftp://h/a", "mailto:a@b.c",
    "한글 텍스트", "제1조(목적)", "및/또는", "2024/01/01", "A/B", "n/a", "1/2", "\"", "'", " ", "\t", "\n",
    "a", "Z", "1", "한", ":", "/", "\\", ".", "-", "_", "😀", "ǅ", "İ", "ı", "ſ", "K", "é",
]


class LocalPathPredicateEquivalenceTests(unittest.TestCase):
    def test_pattern_sources_are_unchanged(self) -> None:
        # The optimized predicate is built from LOCAL_PATH_PATTERNS.  If a
        # pattern is edited on purpose, update the reference copy above too.
        self.assertEqual(
            [pattern.pattern for pattern in vector_adapter.LOCAL_PATH_PATTERNS],
            [pattern.pattern for pattern in _REFERENCE_PATTERNS],
        )
        self.assertEqual(
            [pattern.flags for pattern in vector_adapter.LOCAL_PATH_PATTERNS],
            [pattern.flags for pattern in _REFERENCE_PATTERNS],
        )

    def test_known_leaks_and_non_leaks(self) -> None:
        leaks = [
            r"C:\Users\someone\file.docx",
            "d:/data/x",
            "see file:///etc/passwd now",
            "path /home/user/a.txt here",
            "'/var/log/x'",
            r"\\server\share\file",
            "경로 /Users/홍길동/문서.hwp 입니다",
            "FıLE://x",
        ]
        clean = [
            "",
            "제1조(목적) 이 규정은 목적으로 한다.",
            "https://example.com/a/b",
            "http://localhost:8080/path",
            "2024/01/01 및/또는 A/B",
            "한C:\\x",
            "a/home/b",
            "/appx/y",
            "\\\\",
            "\\\\server",
        ]
        for value in leaks:
            self.assertTrue(vector_adapter._looks_like_local_path(value), value)
            self.assertTrue(_reference_looks_like_local_path(value), value)
        for value in clean:
            self.assertFalse(vector_adapter._looks_like_local_path(value), value)
            self.assertFalse(_reference_looks_like_local_path(value), value)

    def test_exhaustive_short_strings_match_reference(self) -> None:
        alphabet = ["C", "c", ":", "/", "\\", " ", '"', "'", "a", "한", "ſ", "K", "ı", "f", "e", "u", "s", "\n"]
        checked = 0
        for length in range(0, 5):
            for combo in itertools.product(alphabet, repeat=length):
                value = "".join(combo)
                self.assertEqual(
                    vector_adapter._looks_like_local_path(value),
                    _reference_looks_like_local_path(value),
                    repr(value),
                )
                checked += 1
        self.assertGreater(checked, 100000)

    def test_random_fragment_strings_match_reference(self) -> None:
        rng = random.Random(20261010)
        flagged = 0
        total = 60000
        for _ in range(total):
            value = "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 9)))
            expected = _reference_looks_like_local_path(value)
            self.assertEqual(vector_adapter._looks_like_local_path(value), expected, repr(value))
            flagged += expected
        # The generator must exercise both outcomes in volume.
        self.assertGreater(flagged, total // 20)
        self.assertGreater(total - flagged, total // 20)

    def test_long_text_with_late_leak_matches_reference(self) -> None:
        filler = "제" + "가나다라 " * 400
        for tail in (r"C:\x\y", "/home/u/z", r"\\h\s\f", "file://a/b", "http://a/b", "A/B 및/또는", ""):
            for head in ("", "문서 ", '"', "'"):
                value = f"{head}{filler}{tail}"
                self.assertEqual(
                    vector_adapter._looks_like_local_path(value),
                    _reference_looks_like_local_path(value),
                    tail,
                )


class _StrSubclass(str):
    pass


class _ListSubclass(list):
    pass


class VectorRecordPathLeaksEquivalenceTests(unittest.TestCase):
    def _random_value(self, rng: random.Random, depth: int) -> Any:
        roll = rng.random()
        if depth >= 4 or roll < 0.45:
            kind = rng.random()
            if kind < 0.55:
                return "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(0, 5)))
            if kind < 0.65:
                return _StrSubclass("".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 4))))
            if kind < 0.75:
                return rng.random()
            if kind < 0.85:
                return rng.randint(-5, 5)
            if kind < 0.9:
                return rng.choice([True, False, None])
            return tuple(rng.choice(_FRAGMENTS) for _ in range(2))  # tuples are never scanned
        if roll < 0.72:
            items = [self._random_value(rng, depth + 1) for _ in range(rng.randint(0, 6))]
            return _ListSubclass(items) if rng.random() < 0.1 else items
        keys = ["text", "metadata", "source_path", "files", 7, "한글키", "a.b", "x[1]"]
        return {rng.choice(keys): self._random_value(rng, depth + 1) for _ in range(rng.randint(0, 6))}

    def test_random_nested_records_match_reference(self) -> None:
        rng = random.Random(7)
        records = []
        for index in range(1500):
            record = {"id": f"doc:{index}" if index % 7 else None, "text": self._random_value(rng, 1)}
            record["metadata"] = self._random_value(rng, 1)
            record["embedding"] = [rng.random() for _ in range(rng.randint(0, 40))]
            if index % 11 == 0:
                record["embedding"].append(r"C:\leak\in\vector")
            records.append(record)
        expected = _reference_path_leaks(records)
        self.assertEqual(vector_record_path_leaks(records), expected)
        self.assertGreater(len(expected), 50)

    def test_leak_entry_shape_order_and_truncation(self) -> None:
        long_value = "/home/" + "x" * 400
        records = [
            {
                "id": "r1",
                "text": "ok",
                "metadata": {"a": [long_value, {"b": r"C:\x"}], "c": "fine", "d": "file://h/p"},
            },
            {"text": r"\\srv\share\f"},
        ]
        leaks = vector_record_path_leaks(records)
        self.assertEqual(leaks, _reference_path_leaks(records))
        self.assertEqual(
            [(leak["id"], leak["field_path"]) for leak in leaks],
            [("r1", "$.metadata.a[0]"), ("r1", "$.metadata.a[1].b"), ("r1", "$.metadata.d"), ("", "$.text")],
        )
        self.assertEqual(len(leaks[0]["value"]), 240)

    def test_generator_input_is_consumed_once(self) -> None:
        records = [{"id": "g", "text": r"C:\x"}, {"id": "h", "text": "clean"}]
        self.assertEqual(vector_record_path_leaks(iter(records)), _reference_path_leaks(records))


if __name__ == "__main__":
    unittest.main()
