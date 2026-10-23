from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from app.agents.model_router import QWEN3_RERANKER_MODEL
from app.retrieval.isolated_reranker import (
    IsolatedQwen3RerankerAdapter, MAX_WORKER_INPUT_BYTES, RerankerWorkerError,
    _run_worker, _terminate_and_reap, _validated_worker_scores,
)
from app.retrieval import reranker_worker


def response(scores):
    return json.dumps({"version": 1, "status": "ok", "scores": scores}).encode()


def request():
    return {
        "version": 1, "query": "합성 규정의 기간", "documents": ["제1조 기간은 3일이다."],
        "model_name": QWEN3_RERANKER_MODEL, "model_source": QWEN3_RERANKER_MODEL,
        "max_length": 4096, "batch_size": 8,
    }


class IsolatedRerankerContractTests(unittest.TestCase):
    def test_worker_error_exposes_only_allowlisted_reason_code(self):
        for reason in (
            "reranker_worker_timeout", "reranker_worker_memory_floor",
            "reranker_worker_invalid_response", "reranker_worker_invalid_score",
            "reranker_worker_score_count_mismatch", "reranker_worker_input_too_large",
            "reranker_worker_output_too_large", "reranker_worker_stderr_too_large",
            "reranker_worker_pipe_error", "reranker_worker_pipe_not_closed",
            "reranker_worker_failed",
        ):
            with self.subTest(reason=reason):
                error = RerankerWorkerError(reason)
                self.assertEqual(error.reason_code, reason)
                self.assertEqual(str(error), reason)
                self.assertEqual(error.args, (reason,))

    def test_worker_error_does_not_publish_arbitrary_reason_or_private_text(self):
        for value in (
            "private source at C:/synthetic/secret.txt", "reranker_worker_timeout\nprivate",
            "reranker_worker_" + "x" * 10000, "", None, [], {"private": "value"},
        ):
            with self.subTest(value_type=type(value).__name__):
                error = RerankerWorkerError(value)
                self.assertEqual(error.reason_code, "reranker_worker_failed")
                self.assertEqual(str(error), "reranker_worker_failed")
                self.assertEqual(error.args, ("reranker_worker_failed",))
                with self.assertRaises(AttributeError):
                    error.reason_code = "private"

    def test_same_normalization_all_documents_and_default_batch(self):
        adapter = IsolatedQwen3RerankerAdapter()
        with patch("app.retrieval.isolated_reranker._run_worker", return_value=response([0.1, 0.9])) as run:
            scores = adapter.score("  합성\n질문  ", [" 첫째\n조문 ", "둘째 조문"])
        payload = json.loads(run.call_args.args[0])
        self.assertEqual(payload["query"], "합성 질문")
        self.assertEqual(payload["documents"], ["첫째 조문", "둘째 조문"])
        self.assertEqual(payload["batch_size"], 8)
        self.assertEqual(payload["max_length"], 4096)
        self.assertEqual(scores, [0.1, 0.9])
        self.assertIsNone(adapter._model)
        self.assertIsNone(adapter._tokenizer)

    def test_ranking_preserves_records_and_existing_top_k_semantics(self):
        adapter = IsolatedQwen3RerankerAdapter()
        candidates = [(0.7, {"id": "a", "text": "합성 첫 조문"}), (0.4, {"id": "b", "text": "합성 둘째 조문"})]
        with patch("app.retrieval.isolated_reranker._run_worker", return_value=response([0.1, 0.9])):
            ranked = adapter.rerank("합성 질문", candidates, top_k=1)
        self.assertEqual(ranked[0][1]["id"], "b")
        self.assertEqual(ranked[0][1]["retrieval_score"], 0.4)
        self.assertEqual(ranked[0][1]["reranker_score"], 0.9)
        self.assertNotIn("reranker_score", candidates[1][1])

    def test_constructor_rejects_remote_gpu_loaded_runtime_and_bad_timeouts(self):
        for kwargs in ({"device": "cuda"}, {"local_files_only": False}, {"model": object()},
                       {"tokenizer": object()}, {"timeout_seconds": True}, {"timeout_seconds": math.inf},
                       {"timeout_seconds": 0}, {"timeout_seconds": 601}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                IsolatedQwen3RerankerAdapter(**kwargs)

    def test_input_size_and_count_fail_before_process_creation(self):
        adapter = IsolatedQwen3RerankerAdapter()
        with patch("app.retrieval.isolated_reranker._run_worker") as run:
            for documents in ([], ["x"] * 51, ["x" * MAX_WORKER_INPUT_BYTES]):
                with self.assertRaises(ValueError):
                    adapter.score("synthetic", documents)
        run.assert_not_called()

    def test_finite_score_count_type_range_and_protocol_are_required(self):
        values = [response([]), response([True]), response([math.nan]), response([math.inf]),
                  response([-0.1]), response([1.1]), response(["0.5"]), b"not-json",
                  b'{"version":true,"status":"ok","scores":[0.5]}',
                  b'{"version":1,"status":"ok","scores":[0.5],"source":"private"}',
                  b'{"version":0,"version":1,"status":"ok","scores":[0.5]}']
        for value in values:
            with self.subTest(value=value), self.assertRaises(RerankerWorkerError):
                _validated_worker_scores(value, expected_count=1)
        self.assertEqual(_validated_worker_scores(response([0, 1]), expected_count=2), [0.0, 1.0])

    def test_importing_parent_does_not_import_torch(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import app.retrieval.isolated_reranker; assert 'torch' not in sys.modules"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


class IsolatedRerankerProcessTests(unittest.TestCase):
    def child(self, code, *, timeout=5):
        with patch("app.retrieval.isolated_reranker._worker_command", return_value=[sys.executable, "-c", code]):
            return _run_worker(b'{"synthetic":"private-marker"}', timeout_seconds=timeout)

    def test_bounded_stdin_round_trip_offline_flags_and_no_source_in_command(self):
        code = "import os,sys,json; value=json.load(sys.stdin); assert value['synthetic']=='private-marker'; assert os.environ['HF_HUB_OFFLINE']=='1'; assert os.environ['TRANSFORMERS_OFFLINE']=='1'; assert int(os.environ['REGULATION_RERANKER_OWNER_PID'])==os.getppid(); print('ok')"
        self.assertEqual(self.child(code).strip(), b"ok")

    def test_timeout_terminates_and_reaps_owned_child(self):
        created = []
        real_popen = subprocess.Popen
        def capture(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            created.append(child)
            return child
        with patch("app.retrieval.isolated_reranker.subprocess.Popen", side_effect=capture):
            with self.assertRaisesRegex(RerankerWorkerError, "^reranker_worker_timeout$"):
                self.child("import time; time.sleep(30)", timeout=0.1)
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].poll())

    def test_output_and_stderr_overflow_are_bounded_failures(self):
        for pipe, reason in (("stdout", "output"), ("stderr", "stderr")):
            with self.subTest(pipe=pipe), self.assertRaisesRegex(RerankerWorkerError, f"^reranker_worker_{reason}_too_large$"):
                self.child(f"import sys; sys.{pipe}.write('x'*200000); sys.{pipe}.flush()")

    def test_nonzero_and_memory_floor_do_not_leak_stderr(self):
        for code, reason in ((3, "failed"), (75, "memory_floor")):
            with self.subTest(code=code), self.assertRaisesRegex(RerankerWorkerError, f"^reranker_worker_{reason}$") as caught:
                self.child(f"import sys; sys.stderr.write('synthetic secret path'); sys.exit({code})")
            self.assertEqual(caught.exception.reason_code, f"reranker_worker_{reason}")

    def test_terminate_timeout_escalates_to_kill_then_wait(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 2), 0]
        _terminate_and_reap(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual([call.kwargs for call in process.wait.call_args_list], [{"timeout": 2}, {"timeout": 5}])


class RerankerWorkerProtocolTests(unittest.TestCase):
    def test_valid_request_uses_local_files_and_same_cpu_scorer(self):
        parsed = reranker_worker._parse_request(json.dumps(request()).encode())
        with patch.object(reranker_worker, "Qwen3RerankerAdapter") as adapter:
            adapter.return_value.score.return_value = [0.25]
            self.assertEqual(reranker_worker._execute(parsed), [0.25])
        self.assertTrue(adapter.call_args.kwargs["local_files_only"])
        self.assertEqual(adapter.call_args.kwargs["device"], "cpu")
        self.assertIsNone(adapter.call_args.kwargs["model_path"])
        adapter.return_value.score.assert_called_once_with(parsed["query"], parsed["documents"], batch_size=8)

    def test_invalid_worker_inputs_never_reach_model(self):
        variants = [{**request(), "documents": []}, {**request(), "documents": ["x"] * 51},
                    {**request(), "batch_size": True}, {**request(), "max_length": 0},
                    {**request(), "model_name": "external/model"}, {**request(), "query": ""},
                    {**request(), "extra": 1}, {**request(), "documents": [7]}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                reranker_worker._parse_request(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            reranker_worker._parse_request(b'{"version":1,"version":1}')

    def test_memory_floor_exits_only_current_worker(self):
        stop = Mock()
        stop.is_set.return_value = False
        with patch.object(reranker_worker, "_available_commit_bytes", return_value=1), \
                patch.object(reranker_worker.os, "_exit", side_effect=SystemExit(75)) as exit_process:
            with self.assertRaises(SystemExit):
                reranker_worker._memory_guard(stop)
        exit_process.assert_called_once_with(75)

    def test_unknown_memory_capacity_is_not_a_success_guarantee_or_abort(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(reranker_worker, "_available_commit_bytes", return_value=None), \
                patch.object(reranker_worker.os, "_exit") as exit_process:
            reranker_worker._memory_guard(stop)
        exit_process.assert_not_called()

    def test_owner_exit_stops_only_current_worker_even_with_available_memory(self):
        stop = Mock()
        stop.is_set.return_value = False
        parent = Mock()
        parent.is_alive.return_value = False
        with patch.object(reranker_worker, "_available_commit_bytes", return_value=2**40) as memory, \
                patch.object(reranker_worker.os, "_exit", side_effect=SystemExit(76)) as exit_process:
            with self.assertRaises(SystemExit):
                reranker_worker._memory_guard(stop, parent)
        exit_process.assert_called_once_with(76)
        memory.assert_not_called()

    def test_windows_owner_handle_observes_exit_and_closes(self):
        parent = object.__new__(reranker_worker._ParentLifetime)
        parent.pid = 123
        parent.handle = 456
        parent.kernel = Mock()
        parent.already_gone = False
        with patch.object(reranker_worker.os, "name", "nt"):
            parent.kernel.WaitForSingleObject.return_value = 258
            self.assertTrue(parent.is_alive())
            parent.kernel.WaitForSingleObject.return_value = 0
            self.assertFalse(parent.is_alive())
            parent.close()
        parent.kernel.CloseHandle.assert_called_once_with(456)
        self.assertIsNone(parent.handle)

    def test_reparenting_before_guard_initialization_rejects_new_owner(self):
        with patch.object(reranker_worker.os, "name", "posix"), \
                patch.object(reranker_worker.os, "getppid", return_value=1):
            parent = reranker_worker._ParentLifetime(expected_pid=4321)
            self.assertFalse(parent.is_alive())

    def test_expected_owner_environment_is_strict_and_optional_for_direct_cli(self):
        with patch.dict(reranker_worker.os.environ, {}, clear=True):
            self.assertIsNone(reranker_worker._expected_owner_pid())
        for value in ("", "abc", "0", "-1", "1.0", "4294967296"):
            with self.subTest(value=value), patch.dict(reranker_worker.os.environ, {"REGULATION_RERANKER_OWNER_PID": value}), \
                    self.assertRaises(ValueError):
                reranker_worker._expected_owner_pid()
        with patch.dict(reranker_worker.os.environ, {"REGULATION_RERANKER_OWNER_PID": "4321"}):
            self.assertEqual(reranker_worker._expected_owner_pid(), 4321)


if __name__ == "__main__":
    unittest.main()
