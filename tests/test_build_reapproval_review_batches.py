from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_reapproval_review_batches import build_reapproval_review_batches, main


class BuildReapprovalReviewBatchesTests(unittest.TestCase):
    def test_builds_batches_from_chunk_candidate_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = _write_worklist(root / "reports" / "reapproval_worklist.json", candidate_count=3)
            chunks = _write_chunks_json(
                root / "reports" / "reapproval_worklist_chunks.json",
                [
                    _candidate("chunk-1", risk="high", action="reprocess_then_reapprove_and_reindex"),
                    _candidate("chunk-2", risk="high", action="reprocess_then_reapprove_and_reindex"),
                    _candidate("chunk-3", risk="medium", action="reapprove_and_reindex"),
                ],
            )
            expected_worklist_sha = hashlib.sha256(worklist.read_bytes()).hexdigest()
            expected_chunks_sha = hashlib.sha256(chunks.read_bytes()).hexdigest()

            report = build_reapproval_review_batches(
                worklist_report=worklist,
                worklist_chunks_report=chunks,
                worklist_report_artifact_path="reports/reapproval_worklist.json",
                worklist_chunks_artifact_path="reports/reapproval_worklist_chunks.json",
                max_chunks_per_batch=1,
            )

        self.assertTrue(report["passed"])
        self.assertEqual(3, report["batch_count"])
        self.assertEqual(3, report["reapproval_chunk_count"])
        self.assertEqual({"high": 2, "medium": 1}, report["risk_tier_chunk_counts"])
        self.assertEqual(expected_worklist_sha, report["worklist_report"]["sha256"])
        self.assertEqual(expected_chunks_sha, report["worklist_chunks"]["sha256"])
        for batch in report["batches"]:
            self.assertEqual(64, len(batch["reapproval_batch_chunk_fingerprint"]))
            self.assertTrue(batch["reapproval_batch_id"].endswith(batch["reapproval_batch_chunk_fingerprint"][:12]))
            template = batch["reapproval_task_template"]
            self.assertEqual(batch["chunk_ids"], template["chunk_ids"])
            self.assertEqual(batch["reapproval_batch_id"], template["reapproval_batch_id"])
            self.assertEqual(batch["review_risk_tier"], template["review_risk_tier"])
            self.assertEqual("reports/reapproval_worklist.json", template["worklist_report_path"])
            self.assertEqual("reports/reapproval_worklist_chunks.json", template["worklist_chunks_path"])

    def test_cli_writes_json_csv_and_markdown_from_csv_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = _write_worklist(root / "reapproval_worklist.json", candidate_count=2)
            chunks_csv = root / "reapproval_chunks.csv"
            _write_chunks_csv(
                chunks_csv,
                [
                    _candidate("chunk-1", risk="high", action="reprocess_then_reapprove_and_reindex"),
                    _candidate("chunk-2", risk="low", action="reapprove_and_reindex"),
                ],
            )
            out_json = root / "batches.json"
            out_csv = root / "batches.csv"
            out_decision_template = root / "batches_decision_template.csv"
            out_md = root / "batches.md"

            exit_code = main(
                [
                    "--worklist-report",
                    str(worklist),
                    "--worklist-chunks-csv",
                    str(chunks_csv),
                    "--worklist-report-artifact-path",
                    "reports/reapproval_worklist.json",
                    "--worklist-chunks-artifact-path",
                    "reports/reapproval_chunks.csv",
                    "--max-chunks-per-batch",
                    "10",
                    "--include-review-tier",
                    "high",
                    "--out-json",
                    str(out_json),
                    "--out-csv",
                    str(out_csv),
                    "--out-decision-template-csv",
                    str(out_decision_template),
                    "--out-md",
                    str(out_md),
                    "--fail-on-issue",
                ],
                stdout=io.StringIO(),
            )
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            with out_csv.open(encoding="utf-8-sig") as handle:
                csv_rows = list(csv.DictReader(handle))
            with out_decision_template.open(encoding="utf-8-sig") as handle:
                decision_rows = list(csv.DictReader(handle))
            out_md_exists = out_md.is_file()

        self.assertEqual(0, exit_code)
        self.assertTrue(out_md_exists)
        self.assertEqual(1, payload["selected_candidate_count"])
        self.assertEqual(1, payload["batch_count"])
        self.assertIn("operator_decision", payload["decision_template_fields"])
        self.assertIn("allowed_operator_decisions", payload["decision_template_fields"])
        self.assertIn("required_operator_fields", payload["decision_template_fields"])
        self.assertIn("approve_all_reviewed", payload["decision_template_operator_decisions"])
        self.assertIn("operator_decision", payload["decision_template_required_fields"])
        self.assertIn("confirmed", payload["decision_template_approval_scope_confirmations"])
        self.assertIn("reject", payload["decision_template_override_decisions"])
        self.assertIn("Blank rows", payload["decision_template_guidance"])
        self.assertEqual("high", payload["batches"][0]["review_risk_tier"])
        self.assertEqual("chunk-1", payload["batches"][0]["chunk_ids"][0])
        self.assertEqual(1, len(csv_rows))
        self.assertEqual("chunk-1", csv_rows[0]["chunk_ids"])
        self.assertEqual(1, len(decision_rows))
        self.assertEqual(payload["batches"][0]["reapproval_batch_id"], decision_rows[0]["reapproval_batch_id"])
        self.assertEqual("", decision_rows[0]["operator_decision"])
        self.assertEqual("[]", decision_rows[0]["chunk_decision_overrides_json"])
        self.assertEqual("", decision_rows[0]["approval_scope_confirmation"])
        self.assertIn("approve_all_reviewed", decision_rows[0]["allowed_operator_decisions"])
        self.assertIn("approval_scope_confirmation", decision_rows[0]["required_operator_fields"])
        self.assertIn("confirmed", decision_rows[0]["approval_scope_confirmation_options"])
        self.assertIn("needs_reprocess", decision_rows[0]["override_decision_options"])

    def test_blocks_when_chunk_candidate_count_does_not_match_worklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = _write_worklist(root / "reapproval_worklist.json", candidate_count=2)
            chunks = _write_chunks_json(
                root / "reapproval_worklist_chunks.json",
                [_candidate("chunk-1", risk="high", action="reapprove_and_reindex")],
            )

            report = build_reapproval_review_batches(
                worklist_report=worklist,
                worklist_chunks_report=chunks,
                worklist_report_artifact_path="reports/reapproval_worklist.json",
                worklist_chunks_artifact_path="reports/reapproval_worklist_chunks.json",
            )
            exit_code = main(
                [
                    "--worklist-report",
                    str(worklist),
                    "--worklist-chunks-report",
                    str(chunks),
                    "--worklist-report-artifact-path",
                    "reports/reapproval_worklist.json",
                    "--worklist-chunks-artifact-path",
                    "reports/reapproval_worklist_chunks.json",
                    "--fail-on-issue",
                ],
                stdout=io.StringIO(),
            )

        self.assertFalse(report["passed"])
        self.assertEqual(1, report["blocker_count"])
        self.assertIn("worklist-chunk-candidate-count-mismatch", {item["code"] for item in report["findings"]})
        self.assertEqual(2, exit_code)

    def test_blocks_cross_runtime_chunk_candidate_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = _write_worklist(root / "reapproval_worklist.json", candidate_count=1)
            chunks = _write_chunks_json(
                root / "reapproval_worklist_chunks.json",
                [_candidate("chunk-1", risk="high", action="reapprove_and_reindex")],
                tenant_id="tenant-b",
                effective_data_dir="data/tenant-b",
            )

            report = build_reapproval_review_batches(
                worklist_report=worklist,
                worklist_chunks_report=chunks,
                worklist_report_artifact_path="reports/reapproval_worklist.json",
                worklist_chunks_artifact_path="reports/reapproval_worklist_chunks.json",
            )

        self.assertFalse(report["passed"])
        self.assertIn("worklist-chunks-tenant-id-mismatch", {item["code"] for item in report["findings"]})
        self.assertIn("worklist-chunks-effective-data-dir-mismatch", {item["code"] for item in report["findings"]})

    def test_blocks_candidate_without_review_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = _write_worklist(root / "reapproval_worklist.json", candidate_count=1)
            candidate = _candidate("chunk-1", risk="high", action="reapprove_and_reindex")
            candidate.pop("review_content_hash")
            chunks = _write_chunks_json(root / "reapproval_worklist_chunks.json", [candidate])

            report = build_reapproval_review_batches(
                worklist_report=worklist,
                worklist_chunks_report=chunks,
                worklist_report_artifact_path="reports/reapproval_worklist.json",
                worklist_chunks_artifact_path="reports/reapproval_worklist_chunks.json",
            )

        self.assertFalse(report["passed"])
        self.assertIn(
            "chunk-review-content-hash-missing-or-invalid",
            {item["code"] for item in report["findings"]},
        )

    def test_blocks_worklist_without_tenant_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worklist = _write_worklist(root / "reapproval_worklist.json", candidate_count=1)
            payload = json.loads(worklist.read_text(encoding="utf-8"))
            payload.pop("tenant_id")
            payload.pop("effective_data_dir")
            worklist.write_text(json.dumps(payload), encoding="utf-8")
            chunks = _write_chunks_json(
                root / "reapproval_worklist_chunks.json",
                [_candidate("chunk-1", risk="high", action="reapprove_and_reindex")],
            )

            report = build_reapproval_review_batches(
                worklist_report=worklist,
                worklist_chunks_report=chunks,
                worklist_report_artifact_path="reports/reapproval_worklist.json",
                worklist_chunks_artifact_path="reports/reapproval_worklist_chunks.json",
            )

        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("worklist-tenant-id-missing", codes)
        self.assertIn("worklist-effective-data-dir-missing", codes)


def _write_worklist(path: Path, *, candidate_count: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "report_type": "reapproval_worklist",
                "generated_at": "2026-07-09T10:00:00+00:00",
                "tenant_id": "tenant-a",
                "effective_data_dir": "data/tenant-a",
                "reapproval_candidate_chunks": candidate_count,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_chunks_json(
    path: Path,
    candidates: list[dict],
    *,
    tenant_id: str = "tenant-a",
    effective_data_dir: str = "data/tenant-a",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "report_type": "reapproval_worklist_chunk_candidates",
                "generated_at": "2026-07-09T10:01:00+00:00",
                "tenant_id": tenant_id,
                "effective_data_dir": effective_data_dir,
                "candidate_count": len(candidates),
                "fields": sorted(candidates[0]) if candidates else [],
                "candidates": candidates,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_chunks_csv(path: Path, candidates: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted(candidates[0]) if candidates else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            row = dict(candidate)
            for key in ("approval_provenance_missing_fields", "reapproval_reasons"):
                row[key] = "; ".join(row.get(key) or [])
            writer.writerow(row)
    return path


def _candidate(chunk_id: str, *, risk: str, action: str) -> dict:
    return {
        "document_rank": 1,
        "suggested_action": action,
        "document_id": "doc-a",
        "document_name": "Demo",
        "filename": "demo.pdf",
        "institution_name": "Institution",
        "apba_id": "C0001",
        "profile_id": "public_portal-c0001",
        "source_system": "PUBLIC_PORTAL",
        "source_record_id": "record-a",
        "source_file_id": "file-a",
        "chunk_id": chunk_id,
        "chunk_type": "article",
        "regulation_title": "Demo",
        "article_no": "Article 1",
        "source_page_start": "",
        "approval_id": f"approval-{chunk_id}",
        "approved_content_hash_short": f"hash-{chunk_id}",
        "review_content_hash": hashlib.sha256(f"review-{chunk_id}".encode("utf-8")).hexdigest(),
        "security_level": "internal",
        "chunker_version": "0.1.0",
        "parser_version": "0.1.0",
        "vector_record_present": True,
        "vector_chunker_version": "0.1.0",
        "vector_content_hash_short": f"vector-{chunk_id}",
        "approval_provenance_missing_fields": ["approval_worklist_report_sha256"],
        "temporal_metadata_present": False,
        "review_risk_tier": risk,
        "review_strategy": "full_manual_review",
        "reapproval_reasons": ["approval_provenance_approval_worklist_report_sha256_missing"],
    }


if __name__ == "__main__":
    unittest.main()
