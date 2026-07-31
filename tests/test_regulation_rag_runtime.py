from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.services import regulation_rag_runtime as runtime


class RegulationRagRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        with runtime._RUNTIME_CONTENT_SIGNATURE_LOCK:
            runtime._RUNTIME_CONTENT_SIGNATURE_CACHE.clear()
            runtime._RUNTIME_CONTENT_SIGNATURE_INFLIGHT.clear()

    def _wait_for_registered_waiter(
        self,
        path: Path,
        signature: tuple[int, int, int, int],
    ) -> None:
        flight_key = (path, signature)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with runtime._RUNTIME_CONTENT_SIGNATURE_LOCK:
                flight = runtime._RUNTIME_CONTENT_SIGNATURE_INFLIGHT.get(
                    flight_key
                )
            if flight is not None and flight.waiters_ready.wait(0.01):
                return
            time.sleep(0.005)
        self.fail("waiter did not register against the active flight")

    def test_portable_file_signature_single_flights_concurrent_cold_callers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc_chunks.json"
            payload = ("approved text\n" * 128).encode("utf-8")
            path.write_bytes(payload)
            barrier = threading.Barrier(2)
            open_entered = threading.Event()
            allow_open = threading.Event()
            open_count = 0
            open_count_lock = threading.Lock()
            original_open = Path.open
            results: list[tuple[int, str] | None] = [None, None]

            def controlled_open(
                candidate: Path,
                *args: object,
                **kwargs: object,
            ):
                nonlocal open_count
                if candidate == path:
                    with open_count_lock:
                        open_count += 1
                    open_entered.set()
                    self.assertTrue(allow_open.wait(5))
                return original_open(candidate, *args, **kwargs)

            def worker(index: int) -> None:
                barrier.wait()
                results[index] = runtime.portable_file_signature(path)

            with patch.object(Path, "open", new=controlled_open):
                threads = [
                    threading.Thread(target=worker, args=(0,)),
                    threading.Thread(target=worker, args=(1,)),
                ]
                for thread in threads:
                    thread.start()
                self.assertTrue(open_entered.wait(5))
                allow_open.set()
                for thread in threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

            expected = (len(payload), hashlib.sha256(payload).hexdigest())
            self.assertEqual([expected, expected], results)
            self.assertEqual(1, open_count)
            self.assertEqual({}, runtime._RUNTIME_CONTENT_SIGNATURE_INFLIGHT)

    def test_portable_file_signature_releases_waiters_after_failed_single_flight(
        self,
    ) -> None:
        for _iteration in range(50):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "doc_chunks.json"
                payload = "approved text".encode("utf-8")
                path.write_bytes(payload)
                open_entered = threading.Event()
                allow_failure = threading.Event()
                open_count = 0
                open_count_lock = threading.Lock()
                original_open = Path.open
                original_signature = runtime.path_signature(path)
                self.assertIsNotNone(original_signature)
                assert original_signature is not None
                results: list[tuple[int, str] | None] = [None, None]

                def failing_open(
                    candidate: Path,
                    *args: object,
                    **kwargs: object,
                ):
                    nonlocal open_count
                    if candidate == path:
                        with open_count_lock:
                            open_count += 1
                        open_entered.set()
                        self.assertTrue(allow_failure.wait(5))
                        raise OSError("simulated hash failure")
                    return original_open(candidate, *args, **kwargs)

                def worker(index: int) -> None:
                    results[index] = runtime.portable_file_signature(path)

                with patch.object(Path, "open", new=failing_open):
                    leader = threading.Thread(target=worker, args=(0,))
                    waiter = threading.Thread(target=worker, args=(1,))
                    leader.start()
                    self.assertTrue(open_entered.wait(5))
                    waiter.start()
                    self._wait_for_registered_waiter(
                        path,
                        original_signature,
                    )
                    allow_failure.set()
                    leader.join(timeout=5)
                    waiter.join(timeout=5)
                    self.assertFalse(leader.is_alive())
                    self.assertFalse(waiter.is_alive())

                self.assertEqual([None, None], results)
                self.assertEqual(1, open_count)
                self.assertNotIn(
                    (path, original_signature),
                    runtime._RUNTIME_CONTENT_SIGNATURE_INFLIGHT,
                )
                self.assertNotIn(path, runtime._RUNTIME_CONTENT_SIGNATURE_CACHE)

                def counting_open(
                    candidate: Path,
                    *args: object,
                    **kwargs: object,
                ):
                    nonlocal open_count
                    if candidate == path:
                        with open_count_lock:
                            open_count += 1
                    return original_open(candidate, *args, **kwargs)

                with patch.object(Path, "open", new=counting_open):
                    retry_result = runtime.portable_file_signature(path)
                self.assertEqual(
                    (len(payload), hashlib.sha256(payload).hexdigest()),
                    retry_result,
                )
                self.assertEqual(2, open_count)
                self.assertNotIn(
                    (path, original_signature),
                    runtime._RUNTIME_CONTENT_SIGNATURE_INFLIGHT,
                )

    def test_portable_file_signature_does_not_cache_changed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc_chunks.json"
            path.write_text("approved text", encoding="utf-8")
            original_signature = runtime.path_signature(path)
            self.assertIsNotNone(original_signature)
            assert original_signature is not None
            changed_signature = (
                original_signature[0],
                original_signature[1],
                original_signature[2] + 1,
                original_signature[3] + 1,
            )
            calls = 0
            original_path_signature = runtime.path_signature

            def changing_signature(candidate: Path):
                nonlocal calls
                if candidate == path:
                    calls += 1
                    return (
                        original_signature
                        if calls == 1
                        else changed_signature
                    )
                return original_path_signature(candidate)

            with patch.object(
                runtime,
                "path_signature",
                side_effect=changing_signature,
            ):
                self.assertIsNone(runtime.portable_file_signature(path))

            self.assertNotIn(path, runtime._RUNTIME_CONTENT_SIGNATURE_CACHE)
            self.assertEqual({}, runtime._RUNTIME_CONTENT_SIGNATURE_INFLIGHT)

    def test_repository_chunk_files_signature_is_bounded_parallel_and_deterministic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            root.mkdir(parents=True)
            names = [
                f"doc-{index:02d}_chunks.json"
                for index in range(45)
            ]
            for name in names:
                (root / name).write_text("[]", encoding="utf-8")
            repository = SimpleNamespace(root=root)
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def fake_signature(path: Path) -> tuple[int, str]:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.02)
                    return (len(path.name), f"digest:{path.name}")
                finally:
                    with active_lock:
                        active -= 1

            with patch.object(
                runtime,
                "portable_file_signature",
                side_effect=fake_signature,
            ):
                signature = runtime.repository_chunk_files_signature(repository)

            expected_signatures = [
                (name, (len(name), f"digest:{name}"))
                for name in sorted(names)
            ]
            expected_digest = hashlib.sha256(
                json.dumps(
                    expected_signatures,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                (
                    sum(len(name) for name in names),
                    expected_digest,
                ),
                signature,
            )
            self.assertGreater(max_active, 1)
            self.assertLessEqual(max_active, 4)

    def test_repository_chunk_files_signature_fails_closed_on_same_size_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            root.mkdir(parents=True)
            first = root / "doc-a_chunks.json"
            second = root / "doc-b_chunks.json"
            first.write_text("A" * 64, encoding="utf-8")
            second.write_text("B" * 64, encoding="utf-8")
            repository = SimpleNamespace(root=root)
            second_stat = second.stat()
            replaced = False

            def mutating_signature(path: Path) -> tuple[int, str]:
                nonlocal replaced
                if path == first and not replaced:
                    replaced = True
                    replacement = root / "replacement.tmp"
                    replacement.write_text("C" * 64, encoding="utf-8")
                    os.replace(replacement, second)
                    os.utime(
                        second,
                        ns=(
                            second_stat.st_atime_ns,
                            second_stat.st_mtime_ns,
                        ),
                    )
                    time.sleep(0.02)
                return (path.stat().st_size, f"digest:{path.name}")

            with patch.object(
                runtime,
                "portable_file_signature",
                side_effect=mutating_signature,
            ):
                signature = runtime.repository_chunk_files_signature(repository)

            self.assertIsNone(signature)

    def test_repository_chunk_files_signature_uses_process_wide_bounded_pool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            root.mkdir(parents=True)
            tracked_paths = [
                root / f"doc-{index:02d}_chunks.json"
                for index in range(12)
            ]
            for path in tracked_paths:
                path.write_text("approved text", encoding="utf-8")
            repository = SimpleNamespace(root=root)
            barrier = threading.Barrier(2)
            original_open = Path.open
            open_counts = {path: 0 for path in tracked_paths}
            active = 0
            max_active = 0
            lock = threading.Lock()
            results: list[tuple[int, str] | None] = [None, None]

            def controlled_open(
                candidate: Path,
                *args: object,
                **kwargs: object,
            ):
                nonlocal active, max_active
                if candidate in open_counts:
                    with lock:
                        open_counts[candidate] += 1
                        active += 1
                        max_active = max(max_active, active)
                    try:
                        time.sleep(0.02)
                        return original_open(candidate, *args, **kwargs)
                    finally:
                        with lock:
                            active -= 1
                return original_open(candidate, *args, **kwargs)

            def worker(index: int) -> None:
                barrier.wait()
                results[index] = runtime.repository_chunk_files_signature(
                    repository
                )

            with patch.object(Path, "open", new=controlled_open):
                threads = [
                    threading.Thread(target=worker, args=(0,)),
                    threading.Thread(target=worker, args=(1,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())

            self.assertIsNotNone(results[0])
            self.assertEqual(results[0], results[1])
            self.assertLessEqual(
                max_active,
                runtime._RUNTIME_CONTENT_SIGNATURE_MAX_WORKERS,
            )
            self.assertEqual(
                {path.name: 1 for path in tracked_paths},
                {path.name: count for path, count in open_counts.items()},
            )

    def test_load_cached_bm25_index_accepts_prevalidated_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bm25.json"
            path.write_text("{}", encoding="utf-8")
            signature = runtime.path_signature(path)
            self.assertIsNotNone(signature)
            sentinel = object()

            with patch.object(
                runtime,
                "path_signature",
                side_effect=AssertionError(
                    "prevalidated signature should skip path_signature"
                ),
            ), patch.object(
                runtime,
                "load_bm25_index",
                return_value=sentinel,
            ) as load_index:
                loaded_first = runtime.load_cached_bm25_index(
                    path,
                    prevalidated_signature=signature,
                )
                loaded_second = runtime.load_cached_bm25_index(
                    path,
                    prevalidated_signature=signature,
                )

            self.assertIs(sentinel, loaded_first)
            self.assertIs(sentinel, loaded_second)
            self.assertEqual(1, load_index.call_count)

if __name__ == "__main__":
    unittest.main()
