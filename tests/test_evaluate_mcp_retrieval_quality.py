from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ingestion.vector_adapter import stable_content_hash
from app.retrieval.hierarchical_index import (
    build_hierarchical_runtime_index,
    hierarchical_index_path,
    write_vector_records_with_offsets,
)
from scripts.evaluate_mcp_retrieval_quality import (
    evaluate_mcp_retrieval_quality,
    normalize_query_specs,
    run,
)


def _result(chunk_id: str, document_id: str) -> dict[str, object]:
    return {
        "id": f"result-{chunk_id}",
        "title": f"Synthetic {chunk_id}",
        "metadata": {
            "chunk_id": chunk_id,
            "document_id": document_id,
        },
    }


TEST_SOURCE_STATE = {
    "scope": "mcp-performance-python-source-v1",
    "status": "available",
    "sha256": "b" * 64,
    "file_count": 3,
    "byte_count": 101,
    "stable": True,
}


def _runtime_record(
    *,
    tenant_id: str,
    profile_id: str,
    document_id: str,
    chunk_id: str,
    effective_from: str = "2025-01-01",
    effective_to: str = "",
) -> dict[str, object]:
    text = f"Approved runtime evidence for {chunk_id}."
    metadata = {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "institution_name": "Synthetic Institution",
        "document_name": f"Synthetic Regulation {document_id}",
        "regulation_no": document_id,
        "regulation_title": f"Synthetic Regulation {document_id}",
        "regulation_status": "approved",
        "regulation_version": "v1",
        "revision_date": effective_from,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "chunk_type": "article",
        "hierarchy_path": f"Synthetic Regulation {document_id} > Article 1",
        "article_no": "Article 1",
        "article_title": "Purpose",
        "approval_status": "approved",
        "security_level": "internal",
        "department_acl": [],
    }
    return {
        "schema_version": "reg-rag-vector-record-v1",
        "id": f"{document_id}:{chunk_id}",
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "text": text,
        "metadata": metadata,
        "content_hash": stable_content_hash(text, metadata),
    }


def _write_verified_runtime(
    data_dir: Path,
    *,
    tenant_id: str,
    profile_id: str,
    vector_records: list[dict[str, object]],
    index_records: list[dict[str, object]] | None = None,
) -> None:
    vector_path = (
        data_dir
        / "vector_db"
        / tenant_id
        / "approved_vectors.jsonl"
    )
    offsets = write_vector_records_with_offsets(vector_path, vector_records)
    hierarchy = build_hierarchical_runtime_index(
        hierarchical_index_path(data_dir),
        index_records if index_records is not None else vector_records,
        tenant_id=tenant_id,
        profile_id=profile_id,
        vector_offsets=offsets,
    )
    (data_dir / "mcp_runtime_manifest.json").write_text(
        json.dumps(
            {
                "report_type": "mcp_runtime_data_bundle",
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "record_count": len(
                    index_records if index_records is not None else vector_records
                ),
                "files": {
                    "hierarchical_index_sha256": hierarchy["sha256"],
                },
            }
        ),
        encoding="utf-8",
    )


def _all_requested_runtime_targets(
    **kwargs: object,
) -> dict[str | None, tuple[set[str], set[str]]]:
    chunk_ids = set(kwargs["requested_chunk_ids"])
    document_ids = set(kwargs["requested_document_ids"])
    return {
        as_of_date: (set(chunk_ids), set(document_ids))
        for as_of_date in set(kwargs["as_of_dates"])
    }


class EvaluateMcpRetrievalQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture_source_state = patch(
            "scripts.evaluate_mcp_retrieval_quality.capture_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.finalize_source_state = patch(
            "scripts.evaluate_mcp_retrieval_quality.finalize_mcp_performance_source_state",
            return_value=dict(TEST_SOURCE_STATE),
        ).start()
        self.addCleanup(patch.stopall)

    def test_evaluates_ranked_targets_no_evidence_and_trace_strategy(self) -> None:
        query_specs = [
            {
                "id": "single",
                "query": "synthetic single target",
                "target_chunk_id": "chunk-a",
                "target_document_id": "doc-a",
            },
            {
                "id": "multiple",
                "query": "synthetic multiple targets",
                "target_chunk_ids": ["chunk-b", "chunk-c"],
                "target_document_ids": ["doc-b", "doc-c"],
            },
            {
                "id": "document-only",
                "query": "synthetic document target",
                "target_document_id": "doc-d",
            },
            {
                "id": "abstain",
                "query": "synthetic absent evidence",
                "expect_no_evidence": True,
            },
            {
                "id": "false-positive",
                "query": "synthetic false positive",
                "expect_no_evidence": True,
            },
        ]
        responses = {
            "synthetic single target": {
                "results": [_result("noise-1", "noise-doc"), _result("chunk-a", "doc-a")],
                "metadata": {
                    "trace_id": "trace-single",
                    "retrieval_strategy": "flat_rag",
                    "result_count": 2,
                    "timing_ms": {"total_elapsed_ms": 1.25},
                },
            },
            "synthetic multiple targets": {
                "results": [
                    _result("chunk-b", "doc-b"),
                    _result("noise-2", "noise-doc"),
                    _result("noise-3", "noise-doc"),
                    _result("chunk-c", "doc-c"),
                ],
                "metadata": {
                    "trace_id": "trace-multiple",
                    "retrieval_strategy": "hierarchical",
                    "result_count": 4,
                },
            },
            "synthetic document target": {
                "results": [
                    _result("noise-4", "noise-doc"),
                    _result("noise-5", "noise-doc"),
                    _result("chunk-d", "doc-d"),
                ],
                "metadata": {
                    "trace_id": "trace-document",
                    "retrieval_strategy": "flat_rag",
                    "result_count": 3,
                },
            },
            "synthetic absent evidence": {
                "results": [],
                "metadata": {
                    "trace_id": "trace-abstain",
                    "retrieval_strategy": "flat_rag",
                    "result_count": 0,
                    "refused": True,
                    "refusal_reason": "low_relevance",
                },
            },
            "synthetic false positive": {
                "results": [_result("noise-6", "noise-doc")],
                "metadata": {
                    "trace_id": "trace-false-positive",
                    "retrieval_strategy": "hierarchical",
                    "result_count": 1,
                },
            },
        }

        def search_side_effect(**kwargs: object) -> dict[str, object]:
            return responses[str(kwargs["query"])]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=root / "data"),
                ) as settings_mock,
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                    return_value=object(),
                ) as auth_mock,
                patch(
                    "scripts.evaluate_mcp_retrieval_quality._verified_runtime_target_ids",
                    side_effect=_all_requested_runtime_targets,
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.search_regulations",
                    side_effect=search_side_effect,
                ) as search_mock,
            ):
                report = evaluate_mcp_retrieval_quality(
                    data_dir=root / "data",
                    tenant_id="tenant-synthetic",
                    profile_id="profile-synthetic",
                    query_specs=query_specs,
                    security_levels=["internal"],
                    department_ids=["dept-synthetic"],
                )

        summary = report["summary"]
        self.assertEqual(TEST_SOURCE_STATE, report["source_state"])
        self.capture_source_state.assert_called_once()
        self.finalize_source_state.assert_called_once()
        self.assertEqual(5, search_mock.call_count)
        settings_mock.assert_called_once_with(
            data_dir=root / "data",
            tenant_id="tenant-synthetic",
            tenant_storage_isolation=None,
            api_audit_enabled=False,
            rag_trace_enabled=False,
        )
        self.assertEqual(
            {
                "api_audit_enabled": False,
                "rag_trace_enabled": False,
            },
            report["settings_overrides"],
        )
        self.assertTrue(all(call.kwargs["top_k"] == 5 for call in search_mock.call_args_list))
        self.assertTrue(
            all(call.kwargs["profile_id"] == "profile-synthetic" for call in search_mock.call_args_list)
        )
        auth_mock.assert_called_once_with(
            tenant_id="tenant-synthetic",
            department_ids=["dept-synthetic"],
        )
        self.assertTrue(
            all(
                call.kwargs["department_ids"] == ["dept-synthetic"]
                for call in search_mock.call_args_list
            )
        )
        self.assertEqual(0.166667, summary["recall_at_1"])
        self.assertEqual(0.833333, summary["recall_at_3"])
        self.assertEqual(1.0, summary["recall_at_5"])
        self.assertEqual(0.611111, summary["mrr"])
        self.assertEqual(0.166667, summary["document_recall_at_1"])
        self.assertEqual(0.833333, summary["document_recall_at_3"])
        self.assertEqual(1.0, summary["document_recall_at_5"])
        self.assertEqual(1, summary["no_evidence_false_positive_count"])
        self.assertEqual(0.5, summary["no_evidence_false_positive_rate"])
        self.assertEqual(1, summary["no_evidence_abstention_count"])
        self.assertEqual(0.5, summary["no_evidence_abstention_rate"])
        self.assertEqual("hierarchical", report["results"][1]["retrieval_strategy"])
        self.assertEqual("trace-multiple", report["results"][1]["trace"]["trace_id"])
        self.assertEqual("chunk-c", report["results"][1]["results"][3]["chunk_id"])
        self.assertTrue(report["results"][1]["results"][3]["primary_target_match"])
        self.assertTrue(report["results"][3]["trace"]["refused"])
        self.assertEqual("canonical_json", report["query_spec_fingerprint_basis"])

    def test_plural_targets_are_merged_deduplicated_and_validated(self) -> None:
        normalized = normalize_query_specs(
            [
                {
                    "question": "synthetic targets",
                    "target_chunk_id": ["chunk-a", "chunk-b"],
                    "target_chunk_ids": ["chunk-b", "chunk-c"],
                    "target_document_id": "doc-a",
                    "target_document_ids": ["doc-a", "doc-b"],
                }
            ]
        )

        self.assertEqual(["chunk-a", "chunk-b", "chunk-c"], normalized[0]["target_chunk_ids"])
        self.assertEqual(["doc-a", "doc-b"], normalized[0]["target_document_ids"])
        with self.assertRaisesRegex(ValueError, "target chunk/document"):
            normalize_query_specs([{"query": "missing labels"}])
        with self.assertRaisesRegex(ValueError, "cannot combine"):
            normalize_query_specs(
                [
                    {
                        "query": "contradictory labels",
                        "expect_no_evidence": True,
                        "target_chunk_id": "chunk-a",
                    }
                ]
            )

    def test_thresholds_create_findings_and_missing_metric_is_not_silently_passed(self) -> None:
        with (
            patch(
                "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                return_value=SimpleNamespace(data_dir=Path("data")),
            ),
            patch(
                "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                return_value=object(),
            ),
            patch(
                "scripts.evaluate_mcp_retrieval_quality._verified_runtime_target_ids",
                side_effect=_all_requested_runtime_targets,
            ),
            patch(
                "scripts.evaluate_mcp_retrieval_quality.search_regulations",
                return_value={
                    "results": [_result("noise", "noise-doc")],
                    "metadata": {"trace_id": "trace-threshold", "retrieval_strategy": "flat_rag"},
                },
            ),
        ):
            report = evaluate_mcp_retrieval_quality(
                data_dir=Path("data"),
                tenant_id="tenant-synthetic",
                query_specs=[
                    {
                        "query": "synthetic missed target",
                        "target_chunk_id": "chunk-target",
                    }
                ],
                min_recall_at_5=0.8,
                min_document_recall_at_5=0.8,
            )

        self.assertFalse(report["passed"])
        self.assertEqual(
            [
                "retrieval-quality-threshold-not-met",
                "retrieval-quality-metric-unavailable",
            ],
            [finding["code"] for finding in report["findings"]],
        )
        self.assertEqual("recall_at_5", report["findings"][0]["metric"])
        self.assertEqual("document_recall_at_5", report["findings"][1]["metric"])

    def test_search_failure_is_reported_and_does_not_count_as_abstention(self) -> None:
        with (
            patch(
                "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                return_value=object(),
            ),
            patch(
                "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                return_value=object(),
            ),
            patch(
                "scripts.evaluate_mcp_retrieval_quality.search_regulations",
                side_effect=ValueError("synthetic search failure"),
            ),
        ):
            report = evaluate_mcp_retrieval_quality(
                data_dir=Path("data"),
                tenant_id="tenant-synthetic",
                query_specs=[
                    {
                        "query": "synthetic error control",
                        "expect_no_evidence": True,
                    }
                ],
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["summary"]["search_error_count"])
        self.assertEqual(0, report["summary"]["no_evidence_abstention_count"])
        self.assertEqual("mcp-search-error", report["findings"][0]["code"])
        self.assertEqual("ValueError", report["results"][0]["search_error"]["type"])

    def test_missing_runtime_targets_are_reported_and_excluded_from_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector_dir = root / "data" / "vector_db" / "tenant-synthetic"
            vector_dir.mkdir(parents=True, exist_ok=True)
            (vector_dir / "approved_vectors.jsonl").write_text(
                json.dumps(
                    {
                        "chunk_id": "missing-chunk",
                        "document_id": "missing-doc",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=root / "data"),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                    return_value=SimpleNamespace(tenant_id="tenant-synthetic"),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.search_regulations",
                    return_value={
                        "results": [_result("expired-chunk", "expired-doc")],
                        "metadata": {},
                    },
                ) as search_mock,
            ):
                report = evaluate_mcp_retrieval_quality(
                    data_dir=root / "data",
                    tenant_id="tenant-synthetic",
                    query_specs=[
                        {
                            "id": "stale-target",
                            "query": "synthetic stale target",
                            "target_chunk_id": "missing-chunk",
                            "target_document_id": "missing-doc",
                        }
                    ],
                )

        self.assertFalse(report["passed"])
        self.assertEqual(0, search_mock.call_count)
        self.assertEqual(0, report["search_call_count"])
        self.assertEqual(1, report["query_spec_validation_finding_count"])
        self.assertEqual(
            ["query-spec-target-missing-from-runtime"],
            [finding["code"] for finding in report["findings"]],
        )
        self.assertEqual(1, report["summary"]["invalid_query_spec_count"])
        self.assertEqual(0, report["summary"]["answerable_query_count"])
        self.assertFalse(report["results"][0]["query_spec_valid"])
        self.assertEqual(["missing-chunk"], report["results"][0]["missing_target_chunk_ids"])
        self.assertEqual(["missing-doc"], report["results"][0]["missing_target_document_ids"])

    def test_runtime_target_validation_honors_profile_and_effective_date_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            wrong_profile_record = _runtime_record(
                tenant_id="tenant-synthetic",
                profile_id="profile-other",
                document_id="wrong-profile-doc",
                chunk_id="wrong-profile-chunk",
            )
            expired_record = _runtime_record(
                tenant_id="tenant-synthetic",
                profile_id="profile-selected",
                document_id="expired-doc",
                chunk_id="expired-chunk",
                effective_from="2024-01-01",
                effective_to="2024-12-31",
            )
            _write_verified_runtime(
                data_dir,
                tenant_id="tenant-synthetic",
                profile_id="profile-selected",
                vector_records=[wrong_profile_record, expired_record],
                index_records=[expired_record],
            )
            with (
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=data_dir),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                    return_value=SimpleNamespace(tenant_id="tenant-synthetic"),
                ),
                patch("scripts.evaluate_mcp_retrieval_quality.search_regulations") as search_mock,
            ):
                report = evaluate_mcp_retrieval_quality(
                    data_dir=data_dir,
                    tenant_id="tenant-synthetic",
                    profile_id="profile-selected",
                    as_of_date="2025-06-01",
                    query_specs=[
                        {
                            "id": "wrong-profile",
                            "query": "profile scoped target",
                            "target_chunk_id": "wrong-profile-chunk",
                            "target_document_id": "wrong-profile-doc",
                        },
                        {
                            "id": "expired",
                            "query": "historically expired target",
                            "target_chunk_id": "expired-chunk",
                            "target_document_id": "expired-doc",
                        },
                        {
                            "id": "historical",
                            "query": "historically active target",
                            "target_chunk_id": "expired-chunk",
                            "target_document_id": "expired-doc",
                            "as_of_date": "2024-06-01",
                        },
                    ],
                )

        self.assertFalse(report["passed"])
        self.assertEqual(1, search_mock.call_count)
        self.assertEqual(2, report["summary"]["invalid_query_spec_count"])
        self.assertEqual(
            ["wrong-profile-chunk"],
            report["results"][0]["missing_target_chunk_ids"],
        )
        self.assertEqual(
            ["expired-chunk"],
            report["results"][1]["missing_target_chunk_ids"],
        )
        self.assertTrue(report["results"][2]["query_spec_valid"])
        self.assertEqual(
            "2024-06-01",
            search_mock.call_args.kwargs["as_of_date"],
        )

    def test_token_profile_rejects_same_length_top_level_profile_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            canonical = _runtime_record(
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                document_id="profile-bound-doc",
                chunk_id="profile-bound-chunk",
            )
            tampered = dict(canonical)
            tampered["profile_id"] = "profile-evil"
            self.assertEqual(
                len(str(canonical["profile_id"])),
                len(str(tampered["profile_id"])),
            )
            _write_verified_runtime(
                data_dir,
                tenant_id="tenant-demo",
                profile_id="profile-demo",
                vector_records=[tampered],
                index_records=[canonical],
            )
            with (
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=data_dir),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                    return_value=SimpleNamespace(tenant_id="tenant-demo"),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.search_regulations"
                ) as search_mock,
            ):
                report = evaluate_mcp_retrieval_quality(
                    data_dir=data_dir,
                    tenant_id="tenant-demo",
                    profile_id=None,
                    query_specs=[
                        {
                            "id": "profile-drift",
                            "query": "profile-bound target",
                            "target_chunk_id": "profile-bound-chunk",
                            "target_document_id": "profile-bound-doc",
                        }
                    ],
                )

        self.assertFalse(report["passed"])
        self.assertEqual(0, search_mock.call_count)
        self.assertFalse(report["results"][0]["query_spec_valid"])
        self.assertEqual(
            ["profile-bound-chunk"],
            report["results"][0]["missing_target_chunk_ids"],
        )
        self.assertEqual(
            ["profile-bound-doc"],
            report["results"][0]["missing_target_document_ids"],
        )

    def test_cli_writes_fingerprint_and_fail_on_threshold_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query_path = root / "queries.json"
            out_path = root / "quality.json"
            query_specs = [
                {
                    "id": "cli-query",
                    "query": "synthetic CLI target",
                    "target_chunk_id": "chunk-target",
                    "target_document_id": "doc-target",
                }
            ]
            query_path.write_text(json.dumps({"queries": query_specs}), encoding="utf-8")
            expected_sha256 = hashlib.sha256(query_path.read_bytes()).hexdigest()
            stdout = io.StringIO()
            with (
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.settings_for_mcp_project",
                    return_value=SimpleNamespace(data_dir=root / "data"),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.mcp_auth_context",
                    return_value=object(),
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality._verified_runtime_target_ids",
                    side_effect=_all_requested_runtime_targets,
                ),
                patch(
                    "scripts.evaluate_mcp_retrieval_quality.search_regulations",
                    return_value={
                        "results": [_result("noise", "noise-doc")],
                        "metadata": {
                            "trace_id": "trace-cli",
                            "retrieval_strategy": "flat_rag",
                        },
                    },
                ),
            ):
                exit_code = run(
                    [
                        "--data-dir",
                        str(root / "data"),
                        "--tenant-id",
                        "tenant-synthetic",
                        "--query-spec-json",
                        str(query_path),
                        "--min-recall-at-5",
                        "1.0",
                        "--out-json",
                        str(out_path),
                        "--fail-on-threshold",
                    ],
                    stdout=stdout,
                )

            written = json.loads(out_path.read_text(encoding="utf-8"))
            emitted = json.loads(stdout.getvalue())

        self.assertEqual(2, exit_code)
        self.assertEqual(expected_sha256, written["query_spec_sha256"])
        self.assertEqual("source_file", written["query_spec_fingerprint_basis"])
        self.assertEqual(str(query_path), written["query_spec_path"])
        self.assertEqual(1, written["query_spec_item_count"])
        self.assertEqual(written["query_spec_sha256"], emitted["query_spec_sha256"])


if __name__ == "__main__":
    unittest.main()
