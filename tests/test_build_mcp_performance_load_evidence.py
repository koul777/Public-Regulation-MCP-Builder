from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path

from scripts.build_mcp_performance_load_evidence import build_mcp_performance_load_evidence, run


class BuildMcpPerformanceLoadEvidenceTests(unittest.TestCase):
    def test_composes_ready_large_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload())
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())
            out_json = root / "out.json"
            out_md = root / "out.md"

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                out_json=out_json,
                out_md=out_md,
            )
            markdown = out_md.read_text(encoding="utf-8")
            json_written = out_json.is_file()

        self.assertTrue(report["passed"])
        self.assertTrue(report["evidence_ready"])
        self.assertTrue(report["performance_release_ready"])
        self.assertTrue(report["latency_slo"]["evaluated"])
        self.assertEqual(0, report["finding_count"])
        self.assertEqual(3, report["query_benchmark_summary"]["warm_record_count"])
        self.assertEqual(3, report["file_summary"]["approved_vectors"]["record_count"])
        self.assertEqual(3, report["file_summary"]["bm25_index"]["document_count"])
        self.assertIn("MCP Performance Load Evidence", markdown)
        self.assertTrue(json_written)

    def test_record_count_mismatch_blocks_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload())
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
            )

        self.assertFalse(report["passed"])
        self.assertIn("large-runtime-record-count-mismatch", {item["code"] for item in report["findings"]})

    def test_functional_evidence_without_latency_thresholds_is_not_release_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(root / "benchmark.json", _benchmark_payload()),
                transport_smoke_report=_write_json(root / "transport.json", _transport_payload()),
                index_visibility_report=_write_json(root / "visibility.json", _visibility_payload()),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                ),
                bm25_index_json=_write_json(root / "bm25_index.json", _bm25_payload()),
                min_warm_records=3,
            )

        self.assertTrue(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertFalse(report["latency_slo"]["evaluated"])
        self.assertEqual(
            report["latency_slo"]["claim_scope"],
            "functional_evidence_only_no_latency_slo",
        )

    def test_required_latency_slo_fails_closed_when_thresholds_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(root / "benchmark.json", _benchmark_payload()),
                transport_smoke_report=_write_json(root / "transport.json", _transport_payload()),
                index_visibility_report=_write_json(root / "visibility.json", _visibility_payload()),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                ),
                bm25_index_json=_write_json(root / "bm25_index.json", _bm25_payload()),
                min_warm_records=3,
                require_latency_slo=True,
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["evidence_ready"])
        self.assertFalse(report["performance_release_ready"])
        self.assertIn(
            "latency-slo-thresholds-missing",
            {item["code"] for item in report["findings"]},
        )

    def test_threshold_failures_block_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload(total_p95=250.0))
            transport = _write_json(root / "transport.json", _transport_payload(warm_search=90.0))
            visibility = _write_json(root / "visibility.json", _visibility_payload(smoke_docs=1))
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())

            report = build_mcp_performance_load_evidence(
                query_benchmark_report=benchmark,
                transport_smoke_report=transport,
                index_visibility_report=visibility,
                approved_vectors_jsonl=vectors,
                bm25_index_json=bm25,
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_transport_warm_search_ms=80.0,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["passed"])
        self.assertIn("query-benchmark-total-p95-too-high", codes)
        self.assertIn("transport-warm-search-too-high", codes)
        self.assertIn("index-visibility-smoke-documents-present", codes)

    def test_configured_slo_blocks_missing_latency_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark_payload = _benchmark_payload()
            benchmark_payload["summary"]["total_elapsed_ms"].pop("p95")
            benchmark_payload["summary"]["warm_search_elapsed_ms"].pop("p95")
            transport_payload = _transport_payload()
            transport_payload["full_profile"].pop("warm_search_elapsed_ms")
            transport_payload["chatgpt_data_profile"].pop("warm_search_elapsed_ms")
            report = build_mcp_performance_load_evidence(
                query_benchmark_report=_write_json(root / "benchmark.json", benchmark_payload),
                transport_smoke_report=_write_json(root / "transport.json", transport_payload),
                index_visibility_report=_write_json(root / "visibility.json", _visibility_payload()),
                approved_vectors_jsonl=_write_jsonl(
                    root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}]
                ),
                bm25_index_json=_write_json(root / "bm25_index.json", _bm25_payload()),
                min_warm_records=3,
                max_total_p95_ms=200.0,
                max_warm_search_p95_ms=100.0,
                max_transport_warm_search_ms=80.0,
                require_latency_slo=True,
            )

        codes = {item["code"] for item in report["findings"]}
        self.assertFalse(report["performance_release_ready"])
        self.assertIn("query-benchmark-total-p95-missing", codes)
        self.assertIn("query-benchmark-warm-search-p95-missing", codes)
        self.assertIn("transport-warm-search-missing", codes)

    def test_cli_writes_outputs_and_can_fail_on_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = _write_json(root / "benchmark.json", _benchmark_payload(total_p95=250.0))
            transport = _write_json(root / "transport.json", _transport_payload())
            visibility = _write_json(root / "visibility.json", _visibility_payload())
            vectors = _write_jsonl(root / "approved_vectors.jsonl", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
            bm25 = _write_json(root / "bm25_index.json", _bm25_payload())
            out_json = root / "evidence.json"
            out_md = root / "evidence.md"

            stdout = io.StringIO()
            exit_code = run(
                [
                    "--query-benchmark-report",
                    str(benchmark),
                    "--transport-smoke-report",
                    str(transport),
                    "--index-visibility-report",
                    str(visibility),
                    "--approved-vectors-jsonl",
                    str(vectors),
                    "--bm25-index-json",
                    str(bm25),
                    "--max-total-p95-ms",
                    "200",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                    "--fail-on-issue",
                ],
                stdout=stdout,
            )
            json_written = out_json.is_file()
            md_written = out_md.is_file()

        self.assertEqual(2, exit_code)
        self.assertTrue(json_written)
        self.assertTrue(md_written)


def _benchmark_payload(*, total_p95: float = 120.0) -> dict:
    return {
        "report_type": "mcp_query_benchmark",
        "passed": True,
        "query_count": 2,
        "iterations": 2,
        "min_warm_records": 3,
        "finding_count": 0,
        "api_call_count": 0,
        "warmup": {"warmed": True, "record_count": 3, "bm25_index_ready": True},
        "summary": {
            "measurement_count": 4,
            "total_elapsed_ms": {"p50": 100.0, "p95": total_p95, "max": total_p95},
            "warm_search_elapsed_ms": {"p50": 50.0, "p95": 70.0, "max": 70.0},
        },
        "query_spec_sha256": "query-sha",
    }


def _transport_payload(*, warm_search: float = 40.0) -> dict:
    profile = {
        "passed": True,
        "tool_profile": "full",
        "search_result_count": 3,
        "warm_search_result_count": 3,
        "fetch_has_text": True,
        "list_tools_elapsed_ms": 5.0,
        "search_elapsed_ms": 45.0,
        "warm_search_elapsed_ms": warm_search,
        "fetch_elapsed_ms": 8.0,
        "total_elapsed_ms": 60.0,
    }
    return {
        "report_type": "mcp_transport_smoke",
        "passed": True,
        "tenant_id": "tenant-demo",
        "tenant_storage_isolation": True,
        "transport": "stdio",
        "full_profile": profile,
        "chatgpt_data_profile": dict(profile, tool_profile="chatgpt-data"),
    }


def _visibility_payload(*, smoke_docs: int = 0) -> dict:
    return {
        "report_type": "mcp_index_visibility_audit",
        "passed": True,
        "tenant_id": "tenant-demo",
        "document_count": 1,
        "total_approved_chunks": 3,
        "total_indexable_record_count": 3,
        "total_mcp_visible_records": 3,
        "total_skipped_unapproved_count": 0,
        "smoke_like_document_count": smoke_docs,
        "finding_count": 0,
    }


def _bm25_payload() -> dict:
    return {
        "index_version": "reg-rag-bm25-index-v1",
        "retrieval_model": "kiwi-bm25-v1",
        "tokenizer": "kiwi-tokenizer-v1",
        "document_count": 3,
        "document_frequencies": {"a": 3},
        "documents": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
