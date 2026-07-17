from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.schemas.chunk import Chunk
from app.services.review_workflow_service import (
    ReviewWorkflowError,
    approval_worklist_evidence,
    build_approval_record,
    build_rejection_record,
    build_security_scan_record,
    chunk_review_attention_reasons,
    normalize_evidence_artifact_path,
    normalize_evidence_identifier,
    normalize_optional_sha256,
    prepare_approval_decision,
    prepare_approval_update,
    prepare_rejection_decision,
    prepare_rejection_update,
    prepare_security_scan_update,
    require_chunk_ids,
    review_batch_chunk_fingerprint,
    review_attention_by_chunk,
    review_content_hash,
    scan_chunk,
    sha256_file,
    validate_approval_preconditions,
    verify_approval_evidence,
)


class ReviewWorkflowServiceTests(unittest.TestCase):
    def test_evidence_normalizers_raise_service_errors_without_fastapi_dependency(self) -> None:
        self.assertEqual("reports/approval.json", normalize_evidence_artifact_path("reports\\approval.json", field_name="path"))
        self.assertEqual("a" * 64, normalize_optional_sha256("A" * 64, field_name="sha"))
        self.assertEqual("approval-batch_1.2", normalize_evidence_identifier("approval-batch_1.2", field_name="id", max_length=50))

        with self.assertRaises(ReviewWorkflowError) as path_raised:
            normalize_evidence_artifact_path("../approval.json", field_name="path")
        with self.assertRaises(ReviewWorkflowError) as sha_raised:
            normalize_optional_sha256("not-a-sha", field_name="sha")
        with self.assertRaises(ReviewWorkflowError) as id_raised:
            normalize_evidence_identifier("manual review", field_name="id", max_length=50)

        self.assertEqual(400, path_raised.exception.status_code)
        self.assertIn("safe relative artifact path", path_raised.exception.detail)
        self.assertIn("SHA-256", sha_raised.exception.detail)
        self.assertIn("letters, numbers", id_raised.exception.detail)

    def test_review_content_hash_and_batch_fingerprint_are_stable_contracts(self) -> None:
        chunk = Chunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            chunk_type="article",
            text="raw text",
            normalized_text="normalized text",
            retrieval_text="retrieval text",
            metadata={"article_no": "1", "nested": {"b": 2, "a": 1}},
            warnings=["table review"],
        )
        same_chunk = chunk.model_copy(update={"metadata": {"nested": {"a": 1, "b": 2}, "article_no": "1"}})
        changed_chunk = chunk.model_copy(update={"retrieval_text": "changed retrieval text"})

        review_hash = review_content_hash(chunk)
        self.assertEqual(review_hash, review_content_hash(same_chunk))
        self.assertNotEqual(review_hash, review_content_hash(changed_chunk))

        items = [
            {
                "chunk_id": "chunk-1",
                "review_content_hash": review_hash,
                "approval_status": "draft",
                "review_priority_tier": "no_signal",
                "review_category": "low_risk_batch_review_candidate",
                "attention_reasons": ["b", "a"],
            }
        ]
        self.assertEqual(
            review_batch_chunk_fingerprint(items, "low_risk_batch"),
            review_batch_chunk_fingerprint(items, "low_risk_batch"),
        )

    def test_approval_worklist_evidence_normalizes_paths_hashes_and_identifiers(self) -> None:
        evidence = approval_worklist_evidence(
            worklist_report_path="reports\\approval_worklist_current.json",
            worklist_report_sha256="A" * 64,
            review_batch_manifest_path="reports/approval_review_batches_current.json",
            review_batch_manifest_sha256="B" * 64,
            review_batch_id="approval-237faa10d2f4-001-manual-attention-001",
            review_batch_chunk_fingerprint="C" * 64,
            review_strategy="sampled_low_risk_batch_review",
        )

        self.assertEqual("reports/approval_worklist_current.json", evidence["worklist_report_path"])
        self.assertEqual("a" * 64, evidence["worklist_report_sha256"])
        self.assertEqual("b" * 64, evidence["review_batch_manifest_sha256"])
        self.assertEqual("c" * 64, evidence["review_batch_chunk_fingerprint"])
        self.assertEqual("sampled_low_risk_batch_review", evidence["review_strategy"])

        with self.assertRaises(ReviewWorkflowError) as path_raised:
            approval_worklist_evidence(worklist_report_path="file://reports/approval_worklist_current.json")
        with self.assertRaises(ReviewWorkflowError) as strategy_raised:
            approval_worklist_evidence(review_strategy="manual review")
        with self.assertRaises(ReviewWorkflowError) as fingerprint_raised:
            approval_worklist_evidence(review_batch_chunk_fingerprint="not-a-sha")

        self.assertIn("worklist_report_path", path_raised.exception.detail)
        self.assertIn("review_strategy", strategy_raised.exception.detail)
        self.assertIn("review_batch_chunk_fingerprint", fingerprint_raised.exception.detail)

    def test_review_attention_reasons_capture_parser_and_table_review_signals(self) -> None:
        chunk = _chunk("chunk-1").model_copy(
            update={
                "metadata": {
                    "review_required": True,
                    "review_flags": ["manual", "manual", ""],
                    "parser_uncertainty": {
                        "risk_level": "High",
                        "flags": ["table_boundary", "ocr_noise"],
                        "recommendation": "human_review",
                    },
                },
                "warnings": ["table extraction warning", "minor spacing issue"],
            }
        )

        reasons = chunk_review_attention_reasons(chunk)

        self.assertEqual(
            [
                "parser_uncertainty_flags:ocr_noise",
                "parser_uncertainty_flags:table_boundary",
                "parser_uncertainty_recommendation:human_review",
                "parser_uncertainty_risk_level:high",
                "review_flags:manual",
                "review_required",
                "warning:table extraction warning",
            ],
            reasons,
        )

    def test_review_attention_by_chunk_is_scoped_to_requested_chunks(self) -> None:
        flagged = _chunk("chunk-1").model_copy(update={"metadata": {"table_review_required": True}})
        unrequested_flagged = _chunk("chunk-2").model_copy(update={"metadata": {"table_review_required": True}})
        clean = _chunk("chunk-3")

        attention = review_attention_by_chunk([flagged, unrequested_flagged, clean], {"chunk-1", "chunk-3"})

        self.assertEqual({"chunk-1": ["table_review_required"]}, attention)

    def test_require_chunk_ids_raises_service_errors_for_empty_or_missing_chunks(self) -> None:
        chunks = [_chunk("chunk-1"), _chunk("chunk-2")]

        self.assertEqual({"chunk-1"}, require_chunk_ids(chunks, ["chunk-1", "chunk-1"]))
        with self.assertRaises(ReviewWorkflowError) as empty:
            require_chunk_ids(chunks, [])
        with self.assertRaises(ReviewWorkflowError) as missing:
            require_chunk_ids(chunks, ["chunk-3"])

        self.assertEqual(400, empty.exception.status_code)
        self.assertEqual("chunk_ids is required.", empty.exception.detail)
        self.assertEqual(404, missing.exception.status_code)
        self.assertIn("chunk-3", missing.exception.detail)

    def test_validate_approval_preconditions_returns_requested_ids_and_review_attention(self) -> None:
        flagged = _chunk("chunk-1").model_copy(update={"metadata": {"table_review_required": True}})
        clean = _chunk("chunk-2")

        result = validate_approval_preconditions(
            chunks=[flagged, clean],
            chunk_ids=["chunk-1", "chunk-2", "chunk-1"],
            review_flags_acknowledged=True,
        )

        self.assertEqual({"chunk-1", "chunk-2"}, result.requested_ids)
        self.assertEqual({"chunk-1": ["table_review_required"]}, result.review_attention)

    def test_validate_approval_preconditions_blocks_non_approvable_chunks(self) -> None:
        blocked = _chunk("chunk-1").model_copy(update={"approval_status": "security_blocked"})

        with self.assertRaises(ReviewWorkflowError) as raised:
            validate_approval_preconditions(
                chunks=[blocked],
                chunk_ids=["chunk-1"],
                review_flags_acknowledged=True,
            )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("Chunks require review before approval: chunk-1:security_blocked", raised.exception.detail)

    def test_validate_approval_preconditions_requires_review_flag_acknowledgement(self) -> None:
        flagged = _chunk("chunk-1").model_copy(update={"warnings": ["table parse warning"]})

        with self.assertRaises(ReviewWorkflowError) as raised:
            validate_approval_preconditions(
                chunks=[flagged],
                chunk_ids=["chunk-1"],
                review_flags_acknowledged=False,
            )

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("Review flags must be acknowledged before approval", raised.exception.detail)
        self.assertIn("chunk-1(warning:table parse warning)", raised.exception.detail)

    def test_validate_approval_preconditions_allows_review_flag_override_with_reason(self) -> None:
        flagged = _chunk("chunk-1").model_copy(update={"warnings": ["table parse warning"]})

        result = validate_approval_preconditions(
            chunks=[flagged],
            chunk_ids=["chunk-1"],
            review_flags_acknowledged=False,
            approval_override_reason="offline director approval",
        )

        self.assertEqual({"chunk-1"}, result.requested_ids)
        self.assertEqual({"chunk-1": ["warning:table parse warning"]}, result.review_attention)

    def test_security_scan_update_blocks_high_risk_without_raw_match_storage(self) -> None:
        chunk = _chunk("chunk-1").model_copy(
            update={
                "text": "employee id 900101-1234567",
                "approval_status": "approved",
                "approval_id": "approval-old",
                "approved_by": "old-reviewer",
                "approved_at": "2026-07-01T00:00:00+00:00",
                "approved_content_hash": "a" * 64,
            }
        )

        findings = scan_chunk(chunk)
        self.assertEqual("resident_registration_number", findings[0]["rule_id"])
        self.assertEqual(64, len(findings[0]["match_hash"]))
        self.assertNotIn("900101-1234567", json.dumps(findings, ensure_ascii=False))

        update = prepare_security_scan_update(
            chunks=[chunk, _chunk("chunk-2")],
            block_high_risk=True,
            chunk_ids={"chunk-1"},
        )

        blocked = update.updated_chunks[0]
        self.assertEqual({"chunk-1"}, update.selected_ids)
        self.assertEqual({"chunk-1"}, update.blocked_chunk_ids)
        self.assertEqual("security_blocked", blocked.approval_status)
        self.assertIsNone(blocked.approval_id)
        self.assertIsNone(blocked.approved_by)
        self.assertIsNone(blocked.approved_at)
        self.assertIsNone(blocked.approved_content_hash)

        record = build_security_scan_record(
            update=update,
            scan_id="security-scan-1",
            document_id="doc-1",
            tenant_id="tenant-a",
            created_at="2026-07-10T03:04:05+00:00",
            scanned_by="reviewer@example.test",
            scan_reason="pre_approval",
            vector_sync={"status": "skipped", "reason": "test"},
        )

        self.assertEqual("security-scan-1", record["scan_id"])
        self.assertEqual(["chunk-1"], record["blocked_chunk_ids"])
        self.assertEqual(["chunk-1"], record["scanned_chunk_ids"])
        self.assertEqual(1, record["finding_count"])
        self.assertIn("resident_registration_number", record["rules"])
        self.assertNotIn("900101-1234567", json.dumps(record, ensure_ascii=False))

    def test_prepare_approval_update_attaches_evidence_and_builds_record(self) -> None:
        chunk = _chunk("chunk-1").model_copy(
            update={
                "approval_status": "needs_review",
                "security_level": "internal",
                "department_acl": ["HR", "finance"],
            }
        )
        untouched = _chunk("chunk-2")
        evidence = {
            "worklist_report_path": "reports/approval_worklist.json",
            "worklist_report_sha256": "a" * 64,
            "review_batch_manifest_path": "reports/approval_batches.json",
            "review_batch_manifest_sha256": "b" * 64,
            "review_batch_id": "approval-batch-001",
            "review_batch_chunk_fingerprint": "c" * 64,
            "review_strategy": "human_bulk_review",
        }
        update = prepare_approval_update(
            chunks=[chunk, untouched],
            requested_ids={"chunk-1"},
            approval_id="approval-1",
            approved_by="reviewer@example.test",
            approved_at="2026-07-10T01:02:03+00:00",
            requested_security_level="confidential",
            worklist_evidence=evidence,
            review_attention={"chunk-1": ["table_parse_uncertain", "parser_warning"]},
        )

        approved = update.updated_chunks[0]
        self.assertEqual("approved", approved.approval_status)
        self.assertEqual("approval-1", approved.approval_id)
        self.assertEqual("reviewer@example.test", approved.approved_by)
        self.assertEqual("confidential", approved.security_level)
        self.assertEqual(evidence["worklist_report_path"], approved.metadata["approval_worklist_report_path"])
        self.assertEqual(untouched, update.updated_chunks[1])
        self.assertEqual({"chunk-1"}, set(update.before_content_hashes))
        self.assertEqual({"chunk-1"}, set(update.approved_content_hashes))
        self.assertEqual(64, len(update.approved_content_hashes["chunk-1"]))
        self.assertEqual(1, update.review_attention_chunk_count)
        self.assertEqual(["parser_warning", "table_parse_uncertain"], update.review_attention_flags)

        snapshot = update.approved_chunks[0]
        self.assertEqual("needs_review", snapshot["previous_approval_status"])
        self.assertEqual(["finance", "hr"], snapshot["department_acl"])
        self.assertEqual(["table_parse_uncertain", "parser_warning"], snapshot["review_attention_reasons"])
        self.assertEqual(evidence, snapshot["worklist_evidence"])

        record = build_approval_record(
            update=update,
            approval_record_id="approval-record-1",
            document_id="doc-1",
            requested_ids={"chunk-1"},
            tenant_id="tenant-a",
            worklist_evidence=evidence,
            review_flags_acknowledged=True,
            preapproval_scan={"scan_id": "scan-1", "finding_count": 0},
            note="reviewed",
            snapshot="snapshots/review.json",
            artifacts={"chunks": "exports/chunks.jsonl"},
            vector_sync={"removed_vector_ids": []},
        )

        self.assertEqual("approval-record-1", record["approval_record_id"])
        self.assertEqual(["chunk-1"], record["chunk_ids"])
        self.assertTrue(record["review_flags_acknowledged"])
        self.assertEqual("scan-1", record["preapproval_security_scan_id"])
        self.assertEqual(0, record["preapproval_finding_count"])
        self.assertEqual(update.approved_chunks, record["approved_chunks"])

    def test_prepare_approval_update_drops_stale_or_forged_approval_provenance(self) -> None:
        stale_provenance_keys = [
            "approval_worklist_report_path",
            "approval_worklist_report_sha256",
            "approval_review_batch_manifest_path",
            "approval_review_batch_manifest_sha256",
            "approval_review_batch_id",
            "approval_review_batch_chunk_fingerprint",
            "approval_review_strategy",
        ]
        chunk = _chunk("chunk-1").model_copy(
            update={
                "metadata": {
                    "article_no": "1",
                    "approval_worklist_report_path": "reports/forged_worklist.json",
                    "approval_worklist_report_sha256": "0" * 64,
                    "approval_review_batch_manifest_path": "reports/forged_batches.json",
                    "approval_review_batch_manifest_sha256": "1" * 64,
                    "approval_review_batch_id": "forged-batch",
                    "approval_review_batch_chunk_fingerprint": "2" * 64,
                    "approval_review_strategy": "forged_review",
                }
            }
        )

        update = prepare_approval_update(
            chunks=[chunk],
            requested_ids={"chunk-1"},
            approval_id="approval-1",
            approved_by="reviewer@example.test",
            approved_at="2026-07-10T01:02:03+00:00",
            requested_security_level="internal",
            worklist_evidence={},
            review_attention={},
        )

        approved = update.updated_chunks[0]
        self.assertEqual("1", approved.metadata["article_no"])
        for key in stale_provenance_keys:
            self.assertNotIn(key, approved.metadata)

    def test_prepare_approval_decision_requires_clean_preapproval_scan_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            chunks = [_chunk("chunk-1").model_copy(update={"approval_status": "draft"})]
            evidence = _write_approval_evidence(root, data_dir=data_dir, document_id="doc-1", chunks=chunks)

            decision = prepare_approval_decision(
                chunks=chunks,
                chunk_ids=["chunk-1"],
                review_flags_acknowledged=True,
                preapproval_scan={
                    "scan_id": "scan-1",
                    "finding_count": 0,
                    "blocked_chunk_ids": [],
                },
                artifact_root=root,
                runtime_data_dir=data_dir,
                tenant_id="tenant-a",
                document_id="doc-1",
                approval_id="approval-1",
                approved_by="reviewer@example.test",
                approved_at="2026-07-10T02:03:04+00:00",
                requested_security_level="internal",
                worklist_evidence=evidence,
            )

            with self.assertRaises(ReviewWorkflowError) as missing_scan:
                prepare_approval_decision(
                    chunks=chunks,
                    chunk_ids=["chunk-1"],
                    review_flags_acknowledged=True,
                    preapproval_scan={},
                    artifact_root=root,
                    runtime_data_dir=data_dir,
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    approval_id="approval-1",
                    approved_by="reviewer@example.test",
                    approved_at="2026-07-10T02:03:04+00:00",
                    requested_security_level="internal",
                    worklist_evidence=evidence,
                )
            with self.assertRaises(ReviewWorkflowError) as blocked_scan:
                prepare_approval_decision(
                    chunks=chunks,
                    chunk_ids=["chunk-1"],
                    review_flags_acknowledged=True,
                    preapproval_scan={
                        "scan_id": "scan-2",
                        "finding_count": 1,
                        "blocked_chunk_ids": ["chunk-1"],
                    },
                    artifact_root=root,
                    runtime_data_dir=data_dir,
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    approval_id="approval-1",
                    approved_by="reviewer@example.test",
                    approved_at="2026-07-10T02:03:04+00:00",
                    requested_security_level="internal",
                    worklist_evidence=evidence,
                )
            with self.assertRaises(ReviewWorkflowError) as missing_evidence:
                prepare_approval_decision(
                    chunks=chunks,
                    chunk_ids=["chunk-1"],
                    review_flags_acknowledged=True,
                    preapproval_scan={
                        "scan_id": "scan-3",
                        "finding_count": 0,
                        "blocked_chunk_ids": [],
                    },
                    artifact_root=root,
                    runtime_data_dir=data_dir,
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    approval_id="approval-1",
                    approved_by="reviewer@example.test",
                    approved_at="2026-07-10T02:03:04+00:00",
                    requested_security_level="internal",
                    worklist_evidence={},
                )

        self.assertEqual({"chunk-1"}, decision.requested_ids)
        self.assertEqual(evidence, decision.worklist_evidence)
        self.assertEqual("approved", decision.approval_update.updated_chunks[0].approval_status)
        self.assertIn("preapproval_security_scan is required", missing_scan.exception.detail)
        self.assertIn("Security scan blocked chunks before approval: chunk-1", blocked_scan.exception.detail)
        self.assertIn("Official RAG/MCP approval evidence is required", missing_evidence.exception.detail)

    def test_prepare_rejection_update_clears_approval_fields_and_builds_record(self) -> None:
        chunk = _chunk("chunk-1").model_copy(
            update={
                "approval_status": "approved",
                "approval_id": "approval-old",
                "approved_by": "old-reviewer",
                "approved_at": "2026-07-01T00:00:00+00:00",
                "approved_content_hash": "a" * 64,
                "metadata": {"article_no": "1", "keep": "value"},
            }
        )
        update = prepare_rejection_update(
            chunks=[chunk, _chunk("chunk-2")],
            requested_ids={"chunk-1"},
            reason="table structure needs manual recheck",
            reviewed_by="reviewer@example.test",
            reviewed_at="2026-07-10T02:03:04+00:00",
        )

        rejected = update.updated_chunks[0]
        self.assertEqual("rejected", rejected.approval_status)
        self.assertIsNone(rejected.approval_id)
        self.assertIsNone(rejected.approved_by)
        self.assertIsNone(rejected.approved_at)
        self.assertIsNone(rejected.approved_content_hash)
        self.assertEqual("value", rejected.metadata["keep"])
        self.assertEqual("table structure needs manual recheck", rejected.metadata["review_rejection_reason"])
        self.assertEqual("reviewer@example.test", rejected.metadata["review_rejected_by"])
        self.assertNotEqual(update.before_content_hashes["chunk-1"], update.after_content_hashes["chunk-1"])

        record = build_rejection_record(
            update=update,
            review_id="review-1",
            document_id="doc-1",
            requested_ids={"chunk-1"},
            tenant_id="tenant-a",
            reason="table structure needs manual recheck",
            note="manual rejection",
            snapshot="snapshots/reject.json",
            artifacts={"chunks": "exports/chunks.jsonl"},
            vector_sync={"removed_vector_ids": ["doc-1:chunk-1"]},
        )

        self.assertEqual("reject", record["action"])
        self.assertEqual("rejected", record["status"])
        self.assertEqual(["chunk-1"], record["chunk_ids"])
        self.assertEqual(update.before_content_hashes, record["before_content_hashes"])
        self.assertEqual(update.after_content_hashes, record["after_content_hashes"])

    def test_prepare_rejection_decision_validates_ids_and_reason(self) -> None:
        chunks = [_chunk("chunk-1"), _chunk("chunk-2")]

        decision = prepare_rejection_decision(
            chunks=chunks,
            chunk_ids=["chunk-1", "chunk-1"],
            reason="  table structure needs manual recheck  ",
            reviewed_by="reviewer@example.test",
            reviewed_at="2026-07-10T02:03:04+00:00",
        )

        with self.assertRaises(ReviewWorkflowError) as missing:
            prepare_rejection_decision(
                chunks=chunks,
                chunk_ids=["chunk-3"],
                reason="manual rejection",
                reviewed_by="reviewer@example.test",
                reviewed_at="2026-07-10T02:03:04+00:00",
            )
        with self.assertRaises(ReviewWorkflowError) as blank:
            prepare_rejection_decision(
                chunks=chunks,
                chunk_ids=["chunk-1"],
                reason=" ",
                reviewed_by="reviewer@example.test",
                reviewed_at="2026-07-10T02:03:04+00:00",
            )

        self.assertEqual({"chunk-1"}, decision.requested_ids)
        self.assertEqual("table structure needs manual recheck", decision.reason)
        self.assertEqual("rejected", decision.rejection_update.updated_chunks[0].approval_status)
        self.assertEqual(404, missing.exception.status_code)
        self.assertIn("chunk-3", missing.exception.detail)
        self.assertEqual("rejection reason is required.", blank.exception.detail)

    def test_sha256_file_streams_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.txt"
            path.write_text("artifact", encoding="utf-8")

            digest = sha256_file(path)

        self.assertEqual(64, len(digest))

    def test_verify_approval_evidence_accepts_matching_worklist_and_batch_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            chunks = [_chunk("chunk-1")]
            evidence = _write_approval_evidence(root, data_dir=data_dir, document_id="doc-1", chunks=chunks)
            evidence_without_manifest_sha = dict(evidence)
            evidence_without_manifest_sha.pop("review_batch_manifest_sha256")

            verify_approval_evidence(
                artifact_root=root,
                runtime_data_dir=data_dir,
                tenant_id="tenant-a",
                document_id="doc-1",
                chunks=chunks,
                requested_ids={"chunk-1"},
                evidence=evidence_without_manifest_sha,
            )

        self.assertEqual(evidence["review_batch_manifest_sha256"], evidence_without_manifest_sha["review_batch_manifest_sha256"])

    def test_verify_approval_evidence_blocks_stale_review_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            chunks = [_chunk("chunk-1")]
            evidence = _write_approval_evidence(
                root,
                data_dir=data_dir,
                document_id="doc-1",
                chunks=chunks,
                tamper_review_content_hash=True,
            )

            with self.assertRaises(ReviewWorkflowError) as raised:
                verify_approval_evidence(
                    artifact_root=root,
                    runtime_data_dir=data_dir,
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    chunks=chunks,
                    requested_ids={"chunk-1"},
                    evidence=evidence,
                )

        self.assertIn("review_content_hash mismatch", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()


def _chunk(chunk_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        chunk_type="article",
        text=f"text for {chunk_id}",
        retrieval_text=f"text for {chunk_id}",
        approval_status="draft",
        metadata={"article_no": "1"},
    )


def _write_approval_evidence(
    root: Path,
    *,
    data_dir: Path,
    document_id: str,
    chunks: list[Chunk],
    tamper_review_content_hash: bool = False,
) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True)
    worklist_path = reports / "approval_worklist_current.json"
    manifest_path = reports / "approval_review_batches_current.json"
    worklist = {
        "report_type": "approval_worklist",
        "tenant_id": "tenant-a",
        "effective_data_dir": str(data_dir),
        "documents": [{"document_id": document_id}],
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False), encoding="utf-8")
    worklist_sha = sha256_file(worklist_path)
    batch_chunks = []
    for chunk in chunks:
        review_hash = review_content_hash(chunk)
        if tamper_review_content_hash:
            review_hash = "0" * 64
        batch_chunks.append(
            {
                "chunk_id": chunk.chunk_id,
                "review_content_hash": review_hash,
                "approval_status": chunk.approval_status,
                "review_priority_tier": "no_signal",
                "review_category": "low_risk_batch_review_candidate",
                "attention_reasons": [],
            }
        )
    fingerprint = review_batch_chunk_fingerprint(batch_chunks, "low_risk_batch")
    batch_id = f"approval-{worklist_sha[:12]}-001-low-risk-batch-001-{fingerprint[:12]}"
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "tenant_id": "tenant-a",
        "effective_data_dir": str(data_dir),
        "worklist_report": {"sha256": worklist_sha},
        "batches": [
            {
                "review_batch_id": batch_id,
                "review_batch_chunk_fingerprint": fingerprint,
                "review_type": "low_risk_batch",
                "review_strategy": "human_bulk_review",
                "document_id": document_id,
                "chunk_ids": [chunk.chunk_id for chunk in chunks],
                "chunks": batch_chunks,
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    manifest_sha = sha256_file(manifest_path)
    return {
        "worklist_report_path": "reports/approval_worklist_current.json",
        "worklist_report_sha256": worklist_sha,
        "review_batch_manifest_path": "reports/approval_review_batches_current.json",
        "review_batch_manifest_sha256": manifest_sha,
        "review_batch_id": batch_id,
        "review_batch_chunk_fingerprint": fingerprint,
        "review_strategy": "human_bulk_review",
    }
