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
from scripts.holdout_spec_guard import (
    HoldoutSpecRefused,
    effective_query_split,
    normalize_split_label,
    refuse_holdout_spec,
)
from tests.holdout_spec_fixtures import real_shaped_cases, real_shaped_spec


REFUSAL = "holdout_spec_requires_integrated_runner"
CASE = {"id": "synthetic-case", "query": "synthetic question", "target_document_ids": ["synthetic-document"]}


class HoldoutSpecGuardTests(unittest.TestCase):
    def test_holdout_in_any_spelling_is_refused_with_the_runner_code(self) -> None:
        for label in ("holdout", "HOLDOUT", " Holdout ", "ｈｏｌｄｏｕｔ"):
            for payload in ({"split": label, "queries": [CASE]}, {"split": label, "items": [CASE]}, {"split": label},
                            {"queries": [{**CASE, "split": label}]}, [{**CASE, "split": label}],
                            {"items": [{**CASE, "split": label}]}, {"query_specs": [{**CASE, "split": label}]},
                            {"split": "holdout", "queries": [{**CASE, "split": label}]}):
                with self.subTest(label=label, keys=sorted(payload) if isinstance(payload, dict) else "list"), \
                        self.assertRaisesRegex(HoldoutSpecRefused, REFUSAL):
                    refuse_holdout_spec(payload)

    def test_real_shaped_holdout_spec_is_refused_and_development_is_not(self) -> None:
        with self.assertRaisesRegex(HoldoutSpecRefused, REFUSAL):
            refuse_holdout_spec(real_shaped_spec("holdout"))
        refuse_holdout_spec(real_shaped_spec("development"))
        self.assertEqual("holdout", effective_query_split(real_shaped_spec("holdout")))
        self.assertEqual("development", effective_query_split(real_shaped_spec("development")))
        self.assertNotIn("split", real_shaped_spec("holdout"))

    def test_invalid_mixed_and_conflicting_labels_are_refused_with_their_own_codes(self) -> None:
        development, holdout, unlabeled = real_shaped_cases("development"), real_shaped_cases("holdout"), CASE
        for label in ("dev", "unspecified", "", None, ["holdout"], 1):
            with self.subTest(label=label), self.assertRaisesRegex(HoldoutSpecRefused, "query_split_invalid"):
                refuse_holdout_spec({"split": label, "queries": [CASE]})
            with self.subTest(case_label=label), self.assertRaisesRegex(HoldoutSpecRefused, "query_split_invalid"):
                refuse_holdout_spec({"queries": [{**CASE, "split": label}]})
        mixed = (
            {"queries": [{**development[0], "split": "development"}, unlabeled]},
            {"queries": [unlabeled, {**holdout[0], "split": "holdout"}]},
            {"queries": [{**development[0], "split": "development"}, {**holdout[0], "split": "holdout"}]},
            {"queries": [{**development[0], "split": "Development"}, {**development[1], "split": "holdout"}]},
            {"split": "development", "queries": [{**development[0], "split": "development"}, unlabeled]},
            [{**development[0], "split": "development"}, "plain string query"],
        )
        for payload in mixed:
            with self.subTest(payload=repr(payload)[:60]), self.assertRaisesRegex(HoldoutSpecRefused, "query_split_mixed"):
                refuse_holdout_spec(payload)
        for top, case_split in (("development", "holdout"), ("holdout", "development"), ("Holdout", "development")):
            payload = {"split": top, "queries": [{**case, "split": case_split} for case in development]}
            with self.subTest(top=top), self.assertRaisesRegex(HoldoutSpecRefused, "query_split_conflict"):
                refuse_holdout_spec(payload)

    def test_effective_split_reads_top_level_and_every_query_the_same_way(self) -> None:
        cases = real_shaped_cases("development")
        labeled = [{**case, "split": "development"} for case in cases]
        for expected, payload in (
                ("unspecified", {"queries": cases}), ("unspecified", {}), ("unspecified", [CASE]), ("unspecified", "text"),
                ("development", {"split": "development"}), ("development", {"queries": labeled}),
                ("development", {"split": " DEVELOPMENT ", "queries": labeled}),
                ("holdout", {"split": "Holdout", "queries": [{**case, "split": "ｈｏｌｄｏｕｔ"} for case in cases]})):
            with self.subTest(expected=expected, payload=repr(payload)[:50]):
                self.assertEqual(expected, effective_query_split(payload))
        self.assertEqual(("development", "holdout"), (normalize_split_label(" Development"), normalize_split_label("ＨＯＬＤＯＵＴ")))

    def test_unlabeled_and_development_specs_are_unaffected(self) -> None:
        for payload in ({"queries": [CASE]}, {"split": "development", "queries": [CASE]}, [CASE], [], {}, "text", None,
                        {"queries": ["plain string"]}, {"queries": {"not": "a list"}},
                        {"queries": [{**CASE, "split": "development"}]}, real_shaped_spec("development")):
            with self.subTest(payload=repr(payload)[:40]):
                refuse_holdout_spec(payload)


class _StageCliRefusals:
    """Each stage CLI that loads a spec file refuses a holdout spec before any query runs."""

    def holdout_payload(self) -> dict:
        raise NotImplementedError

    def development_payload(self) -> dict:
        raise NotImplementedError

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="holdout_guard_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.spec = self.write_spec(self.holdout_payload())

    def write_spec(self, payload: object, name: str = "spec.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def development_spec(self) -> Path:
        return self.write_spec(self.development_payload(), "dev.json")

    def query_count(self) -> int:
        return len(self.development_payload()["queries"])

    def run_cli(self, function, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = function(argv)
        return code, stream.getvalue()

    def assert_reported_refusal(self, code: int, output: str, reason: str = REFUSAL) -> None:
        self.assertEqual(2, code)
        self.assertEqual({"passed": False, "reason_code": reason}, json.loads(output.strip().splitlines()[-1]))

    def test_mcp_retrieval_quality_cli(self) -> None:
        stream = io.StringIO()
        with patch("scripts.evaluate_mcp_retrieval_quality.evaluate_mcp_retrieval_quality") as evaluate:
            code = evaluate_mcp_retrieval_quality.run(["--query-spec-json", str(self.spec)], stdout=stream)
        self.assert_reported_refusal(code, stream.getvalue())
        evaluate.assert_not_called()
        self.assertEqual(self.query_count(), len(evaluate_mcp_retrieval_quality.load_query_specs(self.development_spec())))
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
        self.assertEqual(self.query_count(), len(benchmark_mcp_queries.load_query_specs(self.development_spec())))

    def test_benchmark_concurrent_queries_cli(self) -> None:
        self.run_stdout_cli(benchmark_mcp_concurrent_queries.run,
                            "scripts.benchmark_mcp_concurrent_queries.benchmark_mcp_concurrent_queries")

    def test_benchmark_first_query_cli(self) -> None:
        self.run_stdout_cli(benchmark_mcp_first_query.run, "scripts.benchmark_mcp_first_query.benchmark_mcp_first_query")
        self.assertEqual(self.query_count(), len(benchmark_mcp_first_query.load_query_specs(self.development_spec())))

    def test_compare_rag_mcp_accuracy_cli(self) -> None:
        self.run_stdout_cli(compare_rag_mcp_accuracy.run, "scripts.compare_rag_mcp_accuracy.compare_rag_mcp_accuracy")
        self.assertEqual(self.query_count(), len(compare_rag_mcp_accuracy.load_query_specs(self.development_spec())))

    def test_export_mcp_demo_answers_cli(self) -> None:
        self.run_stdout_cli(export_mcp_demo_answers.run, "scripts.export_mcp_demo_answers.export_mcp_demo_answers")
        self.assertEqual(self.query_count(), len(export_mcp_demo_answers.load_query_specs(self.development_spec())))

    def test_rag_retrieval_evaluation_cli(self) -> None:
        with self.assertRaisesRegex(HoldoutSpecRefused, REFUSAL):
            evaluate_rag_retrieval.load_queries(self.spec)
        self.assertEqual(self.query_count(), len(evaluate_rag_retrieval.load_queries(self.development_spec())))
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
        truth = self.write_spec({**self.holdout_payload(), "book_title": "synthetic", "regulations": []}, "truth.json")
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
        for payload in (self.development_payload(), {"queries": [CASE]}):
            refuse_holdout_spec(payload)

    def test_mixed_and_conflicting_labels_are_refused_by_the_cli_guard(self) -> None:
        development, holdout = real_shaped_cases("development"), real_shaped_cases("holdout")
        mixed = self.write_spec({"queries": [{**development[0], "split": "development"}, {**holdout[0], "split": "holdout"}]}, "mixed.json")
        conflict = self.write_spec({**real_shaped_spec("holdout"), "split": "development"}, "conflict.json")
        for name, path, reason in (("mixed", mixed, "query_split_mixed"), ("conflict", conflict, "query_split_conflict")):
            with self.subTest(spec=name), patch("scripts.evaluate_live_local_llm.LocalOllamaObserver") as observer:
                code, output = self.run_cli(evaluate_live_local_llm.main, [
                    "--api-url", "http://127.0.0.1:1", "--query-spec", str(path),
                    "--output-dir", str(self.root / "llm"), "--observer-port", "1"])
                self.assert_reported_refusal(code, output, reason)
                observer.assert_not_called()
            stream = io.StringIO()
            with self.subTest(spec=name), patch("scripts.evaluate_mcp_retrieval_quality.evaluate_mcp_retrieval_quality") as evaluate:
                code = evaluate_mcp_retrieval_quality.run(["--query-spec-json", str(path)], stdout=stream)
                self.assert_reported_refusal(code, stream.getvalue(), reason)
                evaluate.assert_not_called()

    def test_integrated_runner_still_passes_parsed_queries_to_the_stages(self) -> None:
        # In-process stages take parsed queries, not a spec file, so the guard never sees a holdout label.
        specs = [{"id": "synthetic-case", "query": "synthetic question", "target_document_ids": ["synthetic-document"],
                  "split": "holdout"}]
        self.assertEqual(1, len(evaluate_mcp_retrieval_quality.normalize_query_specs(specs)))
        self.assertEqual(1, len(real_regulation_mcp_probe._normalize_specs(specs)))


class TopLevelLabelStageCliTests(_StageCliRefusals, unittest.TestCase):
    def holdout_payload(self) -> dict:
        return {"split": "holdout", "queries": [CASE]}

    def development_payload(self) -> dict:
        return {"split": "development", "queries": [CASE]}


class PerQueryLabelStageCliTests(_StageCliRefusals, unittest.TestCase):
    """Specs shaped like the private goldsets: no top-level split, a split on every query."""

    def holdout_payload(self) -> dict:
        return real_shaped_spec("holdout")

    def development_payload(self) -> dict:
        return real_shaped_spec("development")


if __name__ == "__main__":
    unittest.main()
