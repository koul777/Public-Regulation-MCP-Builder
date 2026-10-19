from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from app.retrieval import tokenizer


def fake_kiwi_module(constructor):
    module = ModuleType("kiwipiepy")
    module.Kiwi = constructor
    return module


class RetrievalTokenizerInitializationTests(unittest.TestCase):
    def setUp(self):
        tokenizer._kiwi.cache_clear()
        self.addCleanup(tokenizer._kiwi.cache_clear)

    def test_concurrent_cold_calls_share_one_success_or_cached_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                tokenizer._kiwi.cache_clear()
                first_entered, second_entered, release, second_attempted = (threading.Event() for _ in range(4))
                instances, calls = [], []
                state_lock = threading.Lock()
                def construct():
                    instance = object()
                    with state_lock:
                        calls.append(None)
                        (first_entered if len(calls) == 1 else second_entered).set()
                        instances.append(instance)
                    if not release.wait(2):
                        raise AssertionError("test did not release constructor")
                    if fail:
                        raise RuntimeError("synthetic initialization failure")
                    return instance
                def second():
                    second_attempted.set()
                    return tokenizer._kiwi()
                with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(tokenizer._kiwi)
                        try:
                            self.assertTrue(first_entered.wait(1))
                            other = pool.submit(second)
                            self.assertTrue(second_attempted.wait(1))
                            # The first native constructor remains held while the
                            # other public call reaches the cold-cache boundary.
                            second_entered.wait(.1)
                        finally:
                            release.set()
                        values = [first.result(timeout=1), other.result(timeout=1)]
                self.assertEqual(1, len(calls))
                self.assertIs(values[0], values[1])
                self.assertIs(values[0], None if fail else instances[0])
                self.assertEqual((1, 1, 1), (tokenizer._kiwi.cache_info().hits,
                                            tokenizer._kiwi.cache_info().misses,
                                            tokenizer._kiwi.cache_info().currsize))

    def test_warm_concurrent_calls_reuse_instance_and_cache_statistics(self):
        instance = object()
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(lambda: instance)}):
            self.assertIs(instance, tokenizer._kiwi())
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: tokenizer._kiwi(), range(16)))
        self.assertTrue(all(value is instance for value in values))
        self.assertEqual((16, 1, 1), (tokenizer._kiwi.cache_info().hits,
                                     tokenizer._kiwi.cache_info().misses,
                                     tokenizer._kiwi.cache_info().currsize))

    def test_constructor_exception_is_cached_none_until_clear_and_retry(self):
        calls = []
        instance = object()
        def construct():
            calls.append(None)
            if len(calls) == 1:
                raise RuntimeError("synthetic failure")
            return instance
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
            self.assertIsNone(tokenizer._kiwi())
            self.assertIsNone(tokenizer._kiwi())
            self.assertEqual(1, len(calls))
            # Existing readiness means a cached initialization attempt, including None.
            self.assertTrue(tokenizer.kiwi_is_ready())
            self.assertEqual(tokenizer.FALLBACK_TOKENIZER_MODEL, tokenizer.tokenizer_name())
            tokenizer._kiwi.cache_clear()
            self.assertFalse(tokenizer.kiwi_is_ready())
            self.assertIs(instance, tokenizer._kiwi())
            self.assertEqual(2, len(calls))

    def test_import_failure_is_cached_none_until_clear_and_retry(self):
        with patch.dict(sys.modules, {"kiwipiepy": None}):
            self.assertIsNone(tokenizer._kiwi())
            self.assertIsNone(tokenizer._kiwi())
        instance = object()
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(lambda: instance)}):
            self.assertIsNone(tokenizer._kiwi())
            self.assertEqual(1, tokenizer._kiwi.cache_info().misses)
            tokenizer._kiwi.cache_clear()
            self.assertIs(instance, tokenizer._kiwi())

    def test_cache_clear_resets_statistics_and_next_call_reinitializes(self):
        instances = []
        def construct():
            instance = object()
            instances.append(instance)
            return instance
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
            first = tokenizer._kiwi()
            self.assertIs(first, tokenizer._kiwi())
            tokenizer._kiwi.cache_clear()
            self.assertEqual((0, 0, 0), (tokenizer._kiwi.cache_info().hits,
                                        tokenizer._kiwi.cache_info().misses,
                                        tokenizer._kiwi.cache_info().currsize))
            self.assertIsNot(first, tokenizer._kiwi())
        self.assertEqual(2, len(instances))

    def test_cache_clear_waits_for_initialization_then_returns_empty(self):
        entered, release, clear_started, clear_finished = (threading.Event() for _ in range(4))
        def construct():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release constructor")
            return object()
        def clear():
            clear_started.set()
            tokenizer._kiwi.cache_clear()
            clear_finished.set()
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                initialized = pool.submit(tokenizer._kiwi)
                try:
                    self.assertTrue(entered.wait(1))
                    cleared = pool.submit(clear)
                    self.assertTrue(clear_started.wait(1))
                    finished_while_initializing = clear_finished.wait(.1)
                finally:
                    release.set()
                initialized.result(timeout=1)
                cleared.result(timeout=1)
        self.assertFalse(finished_while_initializing)
        self.assertEqual((0, 0, 0), (tokenizer._kiwi.cache_info().hits,
                                    tokenizer._kiwi.cache_info().misses,
                                    tokenizer._kiwi.cache_info().currsize))

    def test_readiness_and_fast_regex_remain_nonblocking_during_initialization(self):
        entered, release = threading.Event(), threading.Event()
        def construct():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release constructor")
            return object()
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                initialized = pool.submit(tokenizer._kiwi)
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(pool.submit(tokenizer.kiwi_is_ready).result(timeout=1))
                    tokens = pool.submit(tokenizer.tokenize, "policy", prefer_regex_if_kiwi_cold=True).result(timeout=1)
                    self.assertEqual(["policy"], tokens)
                finally:
                    release.set()
                initialized.result(timeout=1)

    def test_initialization_gate_is_released_before_parallel_token_analysis(self):
        analysis = threading.Barrier(2)
        class FakeKiwi:
            def tokenize(self, text):
                analysis.wait(timeout=1)
                return [SimpleNamespace(form=text, tag="NNG")]
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(FakeKiwi)}):
            tokenizer._kiwi()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(tokenizer.tokenize, "policy", tokenizer_model=tokenizer.TOKENIZER_MODEL) for _ in range(2)]
                self.assertEqual([["policy"], ["policy"]], [future.result(timeout=2) for future in futures])

    def test_warm_fast_path_uses_shared_cached_instance(self):
        class FakeKiwi:
            def tokenize(self, text):
                return [SimpleNamespace(form="shared", tag="NNG")]
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(FakeKiwi)}):
            first = tokenizer._kiwi()
            self.assertEqual(["shared"], tokenizer.tokenize("policy", prefer_regex_if_kiwi_cold=True))
            self.assertIs(first, tokenizer._kiwi())
        self.assertEqual(1, tokenizer._kiwi.cache_info().misses)

    def test_base_exception_releases_gate_and_allows_retry(self):
        class InitAbort(BaseException):
            pass
        calls = []
        instance = object()
        def construct():
            calls.append(None)
            if len(calls) == 1:
                raise InitAbort()
            return instance
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
            with self.assertRaises(InitAbort):
                tokenizer._kiwi()
            self.assertEqual(0, tokenizer._kiwi.cache_info().currsize)
            self.assertIs(instance, tokenizer._kiwi())

    def test_cache_parameters_and_uncached_diagnostic_access_are_preserved(self):
        self.assertEqual({"maxsize": 1, "typed": False}, tokenizer._kiwi.cache_parameters())
        instances = []
        def construct():
            instance = object()
            instances.append(instance)
            return instance
        with patch.dict(sys.modules, {"kiwipiepy": fake_kiwi_module(construct)}):
            self.assertIsNot(tokenizer._kiwi.__wrapped__(), tokenizer._kiwi.__wrapped__())
            self.assertEqual(0, tokenizer._kiwi.cache_info().currsize)
            tokenizer._kiwi()
        self.assertEqual(3, len(instances))


if __name__ == "__main__":
    unittest.main()
