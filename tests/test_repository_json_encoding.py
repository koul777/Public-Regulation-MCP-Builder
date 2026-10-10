from __future__ import annotations

import gc
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.config import Settings
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.storage import repository as repository_module
from app.storage.repository import JsonRepository


# Small thresholds make every branch of the size heuristic reachable with tiny values.
SMALL_LIMITS = {
    "_JSON_BUFFERED_ENCODE_MAX_STRING_CHARS": 20,
    "_JSON_BUFFERED_ENCODE_MAX_TOTAL_STRING_CHARS": 60,
    "_JSON_BUFFERED_ENCODE_MAX_CONTAINER_ITEMS": 6,
    "_JSON_BUFFERED_ENCODE_MAX_INSPECTED_VALUES": 25,
}


def _reference_needs_buffered_encoding(value: object, limits: dict[str, int]) -> bool:
    """Verbatim copy of the pre-optimization heuristic, parameterized on its limits."""

    pending: list[object] = [value]
    seen_containers: set[int] = set()
    inspected_values = 0
    total_string_chars = 0
    while pending:
        current = pending.pop()
        inspected_values += 1
        if inspected_values > limits["_JSON_BUFFERED_ENCODE_MAX_INSPECTED_VALUES"]:
            return True
        if isinstance(current, str):
            string_chars = len(current)
            total_string_chars += string_chars
            if (
                string_chars > limits["_JSON_BUFFERED_ENCODE_MAX_STRING_CHARS"]
                or total_string_chars > limits["_JSON_BUFFERED_ENCODE_MAX_TOTAL_STRING_CHARS"]
            ):
                return True
        elif isinstance(current, dict):
            if len(current) > limits["_JSON_BUFFERED_ENCODE_MAX_CONTAINER_ITEMS"]:
                return True
            container_id = id(current)
            if container_id in seen_containers:
                continue
            seen_containers.add(container_id)
            pending.extend(child for pair in current.items() for child in pair)
        elif isinstance(current, (list, tuple)):
            if len(current) > limits["_JSON_BUFFERED_ENCODE_MAX_CONTAINER_ITEMS"]:
                return True
            container_id = id(current)
            if container_id in seen_containers:
                continue
            seen_containers.add(container_id)
            pending.extend(current)
    return False


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.random()
    if depth >= 3 or kind < 0.45:
        scalar = rng.random()
        if scalar < 0.4:
            return rng.choice(["", "a", "x" * 10, "한글" * 8, "y" * 19, "y" * 20, "y" * 21, "z" * 35])
        if scalar < 0.55:
            return rng.randint(-5, 5)
        if scalar < 0.7:
            return rng.random()
        if scalar < 0.8:
            return None
        return rng.choice([True, False])
    if kind < 0.7:
        size = rng.choice([0, 1, 2, 5, 6, 7, 12])
        return [_random_value(rng, depth + 1) for _ in range(size)]
    if kind < 0.8:
        size = rng.choice([0, 1, 3, 6, 7])
        return tuple(_random_value(rng, depth + 1) for _ in range(size))
    size = rng.choice([0, 1, 2, 5, 6, 7])
    mapping: dict = {}
    for index in range(size):
        key_kind = rng.random()
        if key_kind < 0.9:
            key = f"k{index}" if rng.random() < 0.8 else "q" * rng.choice([5, 20, 21, 30])
        elif key_kind < 0.95:
            key = index
        else:
            key = (index, "t")
        mapping[key] = _random_value(rng, depth + 1)
    return mapping


class BufferedEncodingDecisionTests(unittest.TestCase):
    def test_decision_matches_the_reference_for_random_values(self) -> None:
        rng = random.Random(20260110)
        trues = 0
        with mock.patch.multiple(repository_module, **SMALL_LIMITS):
            for index in range(4000):
                value = _random_value(rng)
                if index % 9 == 0:
                    shared = [1, 2, 3]
                    value = {"a": shared, "b": shared, "c": [shared] * rng.choice([1, 3, 9]), "d": value}
                expected = _reference_needs_buffered_encoding(value, SMALL_LIMITS)
                trues += expected
                with self.subTest(index=index):
                    self.assertEqual(expected, repository_module._json_value_needs_buffered_encoding(value))
        self.assertGreater(trues, 100)
        self.assertLess(trues, 3900)

    def test_decision_matches_the_reference_at_every_threshold_boundary(self) -> None:
        cases = {
            "string at limit": "s" * 20,
            "string over limit": "s" * 21,
            "root string at total limit": "s" * 60,
            "list items at limit": list(range(6)),
            "list items over limit": list(range(7)),
            "dict items at limit": {f"k{i}": 0 for i in range(6)},
            "dict items over limit": {f"k{i}": 0 for i in range(7)},
            "total chars at limit": ["a" * 15] * 4,
            "total chars over limit": ["a" * 15] * 4 + ["b"],
            "values at limit": [[0, 0, 0, 0, 0]] * 4 + [[0, 0, 0]],
            "values over limit": [[0, 0, 0, 0, 0]] * 4 + [[0, 0, 0, 0]],
            "dict keys count as values": {f"k{i}": 0 for i in range(6)} | {"x": [0] * 5},
            "dict key over string limit": {"k" * 21: 1},
            "tuple container": tuple(range(7)),
            "scalar root": 12345,
            "none root": None,
            "empty containers": [[], {}, (), []],
        }
        with mock.patch.multiple(repository_module, **SMALL_LIMITS):
            for label, value in cases.items():
                with self.subTest(label=label):
                    self.assertEqual(
                        _reference_needs_buffered_encoding(value, SMALL_LIMITS),
                        repository_module._json_value_needs_buffered_encoding(value),
                    )

    def test_decision_terminates_for_cyclic_values_like_the_reference(self) -> None:
        cyclic: dict = {"a": []}
        cyclic["a"].append(cyclic)
        with mock.patch.multiple(repository_module, **SMALL_LIMITS):
            self.assertEqual(
                _reference_needs_buffered_encoding(cyclic, SMALL_LIMITS),
                repository_module._json_value_needs_buffered_encoding(cyclic),
            )

    def test_production_limits_still_flag_exceptional_records(self) -> None:
        big_string = {"text": "x" * (256 * 1024 + 1)}
        many_items = {"rows": list(range(4097))}
        many_values = {"rows": [[0, 1, 2] for _ in range(1400)]}
        small = {"chunk_id": "c", "text": "short", "metadata": {"k": ["v", 1, None]}}
        self.assertTrue(repository_module._json_value_needs_buffered_encoding(big_string))
        self.assertTrue(repository_module._json_value_needs_buffered_encoding(many_items))
        self.assertTrue(repository_module._json_value_needs_buffered_encoding(many_values))
        self.assertFalse(repository_module._json_value_needs_buffered_encoding(small))


class WriteJsonArrayByteIdentityTests(unittest.TestCase):
    def _write(self, records: list[object], *, limits: dict[str, int] | None = None) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            repository = JsonRepository(Settings(data_dir=Path(tmp) / "data"))
            path = Path(tmp) / "out.json"
            progress: list[tuple[str, int, int]] = []
            if limits is None:
                repository._write_json_array(
                    path,
                    iter(records),
                    total=len(records),
                    phase="p",
                    progress_callback=lambda phase, done, total: progress.append((phase, done, total)),
                )
            else:
                with mock.patch.multiple(repository_module, **limits):
                    repository._write_json_array(
                        path,
                        iter(records),
                        total=len(records),
                        phase="p",
                        progress_callback=lambda phase, done, total: progress.append((phase, done, total)),
                    )
            if records:
                self.assertEqual(("p", len(records), len(records)), progress[-1])
            return path.read_bytes()

    def test_both_encoding_strategies_write_identical_bytes(self) -> None:
        records = [
            {"chunk_id": f"c{i}", "text": "한글 본문 " * (i % 5), "metadata": {"n": i, "rows": [[i, "a"]] * (i % 4)}}
            for i in range(40)
        ] + [{"huge": ["z" * 30] * 8}]
        expected = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        one_shot = self._write(records)
        buffered = self._write(records, limits={name: 1 for name in SMALL_LIMITS})
        self.assertEqual(expected, one_shot)
        self.assertEqual(expected, buffered)

    def test_empty_array_is_written(self) -> None:
        self.assertEqual(b"[]", self._write([]))


class ResultReadersPauseGarbageCollectionTests(unittest.TestCase):
    def _repository_with_chunks(self, tmp: str) -> JsonRepository:
        repository = JsonRepository(Settings(data_dir=Path(tmp) / "data"))
        repository.upsert_document(
            Document(
                document_id="doc-gc",
                filename="a.pdf",
                document_name="A",
                file_type="pdf",
                file_hash="hash-gc",
            )
        )
        repository.save_processing_result(
            "doc-gc",
            [],
            [
                Chunk(chunk_id=f"c{i}", document_id="doc-gc", chunk_type="article", text=f"t{i}", metadata={"i": i})
                for i in range(5)
            ],
            [],
        )
        return repository

    def test_get_chunks_reads_with_collector_paused_and_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = self._repository_with_chunks(tmp)
            seen: list[bool] = []
            original = repository._read_result

            def spy(document_id: str, result_type: str):
                seen.append(gc.isenabled())
                return original(document_id, result_type)

            was_enabled = gc.isenabled()
            gc.enable()
            try:
                with mock.patch.object(repository, "_read_result", spy):
                    chunks = repository.get_chunks("doc-gc")
                    records = repository.get_chunk_records("doc-gc")
                self.assertEqual([False, False], seen)
                self.assertTrue(gc.isenabled())
                self.assertEqual(["c0", "c1", "c2", "c3", "c4"], [chunk.chunk_id for chunk in chunks])
                self.assertEqual(5, len(records))
            finally:
                if not was_enabled:
                    gc.disable()

    def test_reader_errors_restore_the_collector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = self._repository_with_chunks(tmp)
            was_enabled = gc.isenabled()
            gc.enable()
            try:
                with mock.patch.object(repository, "_read_result", side_effect=RuntimeError("boom")):
                    with self.assertRaises(RuntimeError):
                        repository.get_chunks("doc-gc")
                self.assertTrue(gc.isenabled())
            finally:
                if not was_enabled:
                    gc.disable()

    def test_caller_disabled_collector_stays_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = self._repository_with_chunks(tmp)
            was_enabled = gc.isenabled()
            gc.disable()
            try:
                repository.get_chunks("doc-gc")
                self.assertFalse(gc.isenabled())
            finally:
                if was_enabled:
                    gc.enable()


if __name__ == "__main__":
    unittest.main()
