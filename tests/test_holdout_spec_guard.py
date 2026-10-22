from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import (
    benchmark_mcp_concurrent_queries,
    benchmark_mcp_first_query,
    benchmark_mcp_queries,
    compare_rag_mcp_accuracy,
    evaluate_live_local_llm,
    evaluate_mcp_retrieval_quality,
    evaluate_rag_retrieval,
    export_mcp_demo_answers,
    real_regulation_mcp_probe,
    run_combined_book_e2e_regression,
)
from scripts.holdout_spec_guard import HoldoutSpecRefused, refuse_holdout_spec


REFUSAL = "holdout_spec_requires_integrated_runner"
CASE = {"id": "synthetic-case", "query": "synthetic question", "target_document_ids": ["synthetic-document"]}


class HoldoutSpecGuardTests(unittest.TestCase):
    def test_holdout_in_any_spelling_is_refused_with_the_runner_code(self) -> None:
        for label in ("holdout", "HOLDOUT", " Holdout ", "ｈｏｌｄｏｕｔ"):
            for payload in ({"split": label, "queries": [CASE]}, {"split": label, "items": [CASE]}, {"split": label}):
                with self.subTest(label=label, keys=sorted(payload)), self.assertRaisesRegex(HoldoutSpecRefused, REFUSAL):
                    refuse_holdout_spec(payload)

    def test_other_split_values_and_case_level_labels_are_refused(self) -> None:
        for label in ("dev", "unspecified", "", None, ["holdout"], 1):
            with self.subTest(label=label), self.assertRaisesRegex(HoldoutSpecRefused, "query_split_invalid"):
                refuse_holdout_spec({"split": label, "queries": [CASE]})
        hidden = {**CASE, "split": "holdout"}
        for payload in ([hidden], {"queries": [CASE, hidden]}, {"items": [hidden]}, {"query_specs": [hidden]},
                        {"split": "development", "queries": [hidden]}):
            with self.subTest(payload=type(payload).__name__), self.assertRaisesRegex(HoldoutSpecRefused, "case_level_split_not_supported"):
                refuse_holdout_spec(payload)

    def test_unlabeled_and_development_specs_are_unaffected(self) -> None:
        for payload in ({"queries": [CASE]}, {"split": "development", "queries": [CASE]}, [CASE], [], {}, "text", None,
                        {"queries": ["plain string"]}, {"queries": {"not": "a list"}}):
            with self.subTest(payload=repr(payload)[:40]):
                refuse_holdout_spec(payload)


class StageCliRefusalTests(unittest.TestCase):
    """Each stage CLI that loads a spec file refuses a holdout spec before any query runs."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="holdout_guard_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.spec = self.write_spec({"split": "holdout", "queries": [CASE]})

    def write_spec(self, payload: object, name: str = "spec.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_cli(self, function, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = function(argv)
        return code, stream.getvalue()

    def assert_reported_refusal(self, code: int, output: str) -> None:
        self.assertEqual(2, code)
        self.assertEqual({"passed": False, "reason_code": REFUSAL}, json.loads(output.strip().splitlines()[-1]))

    def test_mcp_retrieval_quality_cli(self) -> None:
        stream = io.StringIO()
        with patch("scripts.evaluate_mcp_retrieval_quality.evaluate_mcp_retrieval_quality") as evaluate:
            code = evaluate_mcp_retrieval_quality.run(["--query-spec-json", str(self.spec)], stdout=stream)
        self.assert_reported_refusal(code, stream.getvalue())
        evaluate.assert_not_called()
        self.assertEqual(1, len(evaluate_mcp_retrieval_quality.load_query_specs(self.write_spec({"split": "development", "queries": [CASE]}, "dev.json"))))
        self.assertEqual(1, len(evaluate_mcp_retrieval_quality.load_query_specs(self.write_spec([CASE], "plain.json"))))

    def test_real_regulation_mcp_probe_cli(self) -> None:
        with patch("scripts.real_regulation_mcp_probe.run_mcp_probe") as probe:
            code, output = self.run_cli(real_regulation_mcp_probe.run, [
                "--data-dir", str(self.root / "runtime"), "--query-spec", str(self.spec), "--tenant-id", "synthetic-tenant",
                "--flat-storage", "--output-path", str(self.root / "probe.json")])
        self.assert_reported_refusal(code, output)
        probe.assert_not_called()
        self.assertFalse((self.root / "probe.json").exists())

    def test_live_local_llm_cli(self) -> None:
        with patch("scripts.evaluate_live_local_llm.LocalOllamaObserver") as observer, patch(
                "scripts.evaluate_live_local_llm.evaluate_live_local_llm") as evaluate:
            code, output = self.run_cli(evaluate_live_local_llm.main, [
                "--api-url", "http://127.0.0.1:1", "--query-spec", str(self.spec),
                "--output-dir", str(self.root / "llm"), "--observer-port", "1"])
        self.assert_reported_refusal(code, output)
        observer.assert_not_called()
        evaluate.assert_not_called()

    def run_stdout_cli(self, function, patched: str) -> None:
        """A run(argv, stdout=...) CLI prints the fixed refusal as JSON, exits 2, and starts no work."""
        stream = io.StringIO()
        with patch(patched) as work:
            code = function(["--query-spec-json", str(self.spec)], stdout=stream)
        self.assert_reported_refusal(code, stream.getvalue())
        work.assert_not_called()

    def test_benchmark_queries_cli(self) -> None:
        self.run_stdout_cli(benchmark_mcp_queries.run, "scripts.benchmark_mcp_queries.benchmark_mcp_queries")
        self.assertEqual(1, len(benchmark_mcp_queries.load_query_specs(self.write_spec({"split": "development", "queries": [CASE]}, "dev.json"))))

    def test_benchmark_concurrent_queries_cli(self) -> None:
        self.run_stdout_cli(benchmark_mcp_concurrent_queries.run,
                            "scripts.benchmark_mcp_concurrent_queries.benchmark_mcp_concurrent_queries")

    def test_benchmark_first_query_cli(self) -> None:
        self.run_stdout_cli(benchmark_mcp_first_query.run, "scripts.benchmark_mcp_first_query.benchmark_mcp_first_query")
        self.assertEqual(1, len(benchmark_mcp_first_query.load_query_specs(self.write_spec({"queries": [CASE]}, "plain.json"))))

    def test_compare_rag_mcp_accuracy_cli(self) -> None:
        self.run_stdout_cli(compare_rag_mcp_accuracy.run, "scripts.compare_rag_mcp_accuracy.compare_rag_mcp_accuracy")
        self.assertEqual(1, len(compare_rag_mcp_accuracy.load_query_specs(self.write_spec({"queries": [CASE]}, "plain.json"))))

    def test_export_mcp_demo_answers_cli(self) -> None:
        self.run_stdout_cli(export_mcp_demo_answers.run, "scripts.export_mcp_demo_answers.export_mcp_demo_answers")
        self.assertEqual(1, len(export_mcp_demo_answers.load_query_specs(self.write_spec({"queries": [CASE]}, "plain.json"))))

    def test_rag_retrieval_evaluation_cli(self) -> None:
        with self.assertRaisesRegex(HoldoutSpecRefused, REFUSAL):
            evaluate_rag_retrieval.load_queries(self.spec)
        self.assertEqual(1, len(evaluate_rag_retrieval.load_queries(self.write_spec({"queries": [CASE]}, "plain.json"))))
        argv = ["evaluate_rag_retrieval", "--batch-report", str(self.root / "batch.json"),
                "--queries-json", str(self.spec), "--out-json", str(self.root / "out.json")]
        stream = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(stream), patch(
                "scripts.evaluate_rag_retrieval.load_batch_chunks_from_reports", return_value=([], [])), patch(
                "scripts.evaluate_rag_retrieval.evaluate_retrieval") as evaluate:
            code = evaluate_rag_retrieval.main()
        self.assert_reported_refusal(code, stream.getvalue())
        evaluate.assert_not_called()
        self.assertFalse((self.root / "out.json").exists())

    def test_combined_book_regression_cli_and_function(self) -> None:
        truth = self.write_spec({"split": "holdout", "book_title": "synthetic", "regulations": [], "queries": [CASE]}, "truth.json")
        for runs in ("1", "2"):
            stream = io.StringIO()
            with patch("scripts.run_combined_book_e2e_regression.run_combined_book_e2e") as run, patch(
                    "scripts.run_combined_book_e2e_regression.subprocess.run") as child, redirect_stdout(stream):
                code = run_combined_book_e2e_regression.main(
                    ["--input", str(self.root / "book.docx"), "--ground-truth", str(truth), "--runs", runs])
            with self.subTest(runs=runs):
                self.assert_reported_refusal(code, stream.getvalue())
                run.assert_not_called()
                child.assert_not_called()
        with self.assertRaisesRegex(HoldoutSpecRefused, REFUSAL):
            run_combined_book_e2e_regression.run_combined_book_e2e(
                input_path=self.root / "book.docx", ground_truth_path=truth, work_dir=self.root / "work")
        self.assertFalse((self.root / "work").exists())
        # A development or unlabeled ground truth is not refused by the guard.
        for payload in ({"split": "development", "queries": [CASE]}, {"queries": [CASE]}):
            refuse_holdout_spec(payload)

    def test_integrated_runner_still_passes_parsed_queries_to_the_stages(self) -> None:
        # In-process stages take parsed queries, not a spec file, so the guard never sees a holdout label.
        specs = [{"id": "synthetic-case", "query": "synthetic question", "target_document_ids": ["synthetic-document"]}]
        self.assertEqual(1, len(evaluate_mcp_retrieval_quality.normalize_query_specs(specs)))
        self.assertEqual(1, len(real_regulation_mcp_probe._normalize_specs(specs)))


if __name__ == "__main__":
    unittest.main()
