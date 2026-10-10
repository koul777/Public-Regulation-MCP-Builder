from __future__ import annotations

import gc
import threading
import unittest
import weakref

from app.core import gc_pause
from app.core.gc_pause import gc_paused


class GcPausedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._was_enabled = gc.isenabled()
        gc.enable()

    def tearDown(self) -> None:
        if self._was_enabled:
            gc.enable()
        else:
            gc.disable()

    def test_disables_inside_and_restores_after(self) -> None:
        with gc_paused():
            self.assertFalse(gc.isenabled())
        self.assertTrue(gc.isenabled())

    def test_restores_after_an_exception(self) -> None:
        with self.assertRaises(ValueError):
            with gc_paused():
                raise ValueError("boom")
        self.assertTrue(gc.isenabled())
        self.assertEqual(0, gc_pause._pause_depth)

    def test_nested_pauses_restore_only_when_outermost_exits(self) -> None:
        with gc_paused():
            with gc_paused():
                self.assertFalse(gc.isenabled())
            self.assertFalse(gc.isenabled())
        self.assertTrue(gc.isenabled())

    def test_a_collector_disabled_by_the_caller_stays_disabled(self) -> None:
        gc.disable()
        with gc_paused():
            self.assertFalse(gc.isenabled())
        self.assertFalse(gc.isenabled())

    def test_overlapping_threads_restore_the_collector_once_all_finish(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        states: list[bool] = []

        def worker() -> None:
            with gc_paused():
                entered.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            with gc_paused():
                states.append(gc.isenabled())
            # the worker still holds a pause
            states.append(gc.isenabled())
        finally:
            release.set()
            thread.join(timeout=5)
        states.append(gc.isenabled())
        self.assertEqual([False, False, True], states)

    def test_garbage_cycles_are_still_collected_after_the_pause(self) -> None:
        # Track the cycles by weak reference instead of counting what one
        # explicit gc.collect() returns: once the pause ends, an automatic
        # young-generation collection may legitimately free some of them first,
        # which made a count-based assertion depend on allocation history.
        class Node:
            pass

        refs: list[weakref.ref] = []
        with gc_paused():
            for _ in range(100):
                node = Node()
                node.self_ref = node  # type: ignore[attr-defined]
                refs.append(weakref.ref(node))
            del node
            self.assertTrue(all(ref() is not None for ref in refs))
        gc.collect()
        self.assertTrue(all(ref() is None for ref in refs))


if __name__ == "__main__":
    unittest.main()
