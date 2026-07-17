from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key
from app.schemas.chunk import Chunk
from app.schemas.document import Document
from app.services.review_workflow_service import review_content_hash
from app.storage.repository import JsonRepository
from scripts.apply_reapproval_plan_shadow import STATE_FILENAME, apply_reapproval_plan_shadow
from scripts.build_reapproval_apply_plan import build_reapproval_apply_plan
from scripts.validate_reapproval_decisions import validate_reapproval_decisions


class ApplyReapprovalPlanShadowTests(unittest.TestCase):
    def test_dry_run_validates_without_writing_shadow_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            shadow = root / "shadow"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertTrue(report["passed"], report)
            self.assertEqual("ready_for_confirmed_shadow_apply", report["status"])
            self.assertFalse(report["shadow_runtime_written"])
            self.assertFalse(shadow.exists())

    def test_confirmed_apply_mutates_only_shadow_and_builds_one_atomic_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            shadow = root / "shadow"
            source_chunks_before = (source / "repository" / "doc-a_chunks.json").read_bytes()
            confirmation = _confirmation(plan_path)

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
                confirm_shadow_apply=True,
                **confirmation,
            )

            self.assertTrue(report["passed"], report)
            self.assertEqual("shadow_apply_completed", report["status"])
            self.assertEqual(1, report["approval_record_count"])
            self.assertEqual(1, report["rejection_record_count"])
            self.assertEqual(1, report["vector_record_count"])
            self.assertFalse(report["official_runtime_promoted"])
            self.assertEqual(source_chunks_before, (source / "repository" / "doc-a_chunks.json").read_bytes())

            shadow_repository = JsonRepository(Settings(data_dir=shadow))
            by_id = {chunk.chunk_id: chunk for chunk in shadow_repository.get_chunks("doc-a")}
            self.assertEqual("approved", by_id["chunk-approve"].approval_status)
            self.assertEqual("rejected", by_id["chunk-reject"].approval_status)
            self.assertNotEqual("approval-old-approve", by_id["chunk-approve"].approval_id)
            self.assertIsNone(by_id["chunk-reject"].approval_id)
            self.assertEqual(1, len(shadow_repository.list_approval_journal_records("doc-a")))
            completion = shadow_repository.list_maintenance_events("reapproval_shadow_apply_completed")
            self.assertEqual(1, len(completion))
            self.assertEqual("completed", completion[0]["vector_sync"]["status"])

            vector_path = shadow / "vector_db" / "default" / "approved_vectors.jsonl"
            vectors = [json.loads(line) for line in vector_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["chunk-approve"], [row["chunk_id"] for row in vectors])
            state = json.loads((shadow / STATE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual("completed", state["status"])
            self.assertFalse(state["ready_for_promotion"])
            self.assertTrue(state["ready_for_promotion_review"])

            second = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
                confirm_shadow_apply=True,
                **confirmation,
            )
            self.assertTrue(second["passed"])
            self.assertEqual("already_completed", second["status"])
            self.assertTrue(second["idempotent_noop"])
            self.assertEqual(1, len(shadow_repository.list_maintenance_events("reapproval_shadow_apply_completed")))

    def test_tenant_isolated_apply_preserves_other_tenant_runtime_and_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(
                root,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
            )
            tenant_b_settings = settings_for_tenant(
                Settings(data_dir=source, tenant_storage_isolation=True),
                "tenant-b",
            )
            tenant_b_repository = JsonRepository(tenant_b_settings)
            tenant_b_repository.upsert_document(
                Document(
                    document_id="doc-b",
                    filename="doc-b.pdf",
                    document_name="Other Tenant Regulation",
                    file_type="pdf",
                    file_hash="hash-b",
                    tenant_id="tenant-b",
                )
            )
            tenant_b_repository.save_processing_result(
                "doc-b",
                [],
                [
                    _approved_chunk(
                        "chunk-b",
                        "approval-b",
                        "c" * 64,
                        tenant_id="tenant-b",
                        document_id="doc-b",
                    )
                ],
                [],
            )
            tenant_b_vector = (
                tenant_b_settings.data_dir
                / "vector_db"
                / tenant_storage_key("tenant-b")
                / "approved_vectors.jsonl"
            )
            tenant_b_vector.parent.mkdir(parents=True, exist_ok=True)
            tenant_b_vector.write_text('{"chunk_id":"tenant-b-vector"}\n', encoding="utf-8")
            source_before = _tree_snapshot(source)
            shadow = root / "shadow"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                tenant_id="tenant-a",
                tenant_storage_isolation=True,
                operator_id="operator-a",
                confirm_shadow_apply=True,
                **_confirmation(plan_path),
            )

            self.assertTrue(report["passed"], report)
            self.assertEqual(source_before, _tree_snapshot(source))
            shadow_tenant_a = settings_for_tenant(
                Settings(data_dir=shadow, tenant_storage_isolation=True),
                "tenant-a",
            )
            shadow_tenant_b = settings_for_tenant(
                Settings(data_dir=shadow, tenant_storage_isolation=True),
                "tenant-b",
            )
            by_id = {
                chunk.chunk_id: chunk
                for chunk in JsonRepository(shadow_tenant_a).get_chunks("doc-a")
            }
            self.assertEqual("approved", by_id["chunk-approve"].approval_status)
            self.assertEqual("rejected", by_id["chunk-reject"].approval_status)
            self.assertEqual(
                '{"chunk_id":"tenant-b-vector"}\n',
                (
                    shadow_tenant_b.data_dir
                    / "vector_db"
                    / tenant_storage_key("tenant-b")
                    / "approved_vectors.jsonl"
                ).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "tenant-b",
                JsonRepository(shadow_tenant_b).get_document("doc-b").tenant_id,
            )

    def test_confirmed_apply_requires_exact_plan_id_and_payload_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            shadow = root / "shadow"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
                confirm_shadow_apply=True,
            )

            self.assertFalse(report["passed"])
            codes = {item["code"] for item in report["blockers"]}
            self.assertIn("execution-plan-confirmation-mismatch", codes)
            self.assertIn("plan-payload-confirmation-mismatch", codes)
            self.assertFalse(shadow.exists())

    def test_completed_shadow_is_not_idempotent_after_vector_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            shadow = root / "shadow"
            confirmation = _confirmation(plan_path)
            first = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
                confirm_shadow_apply=True,
                **confirmation,
            )
            self.assertTrue(first["passed"], first)
            vector_path = shadow / "vector_db" / "default" / "approved_vectors.jsonl"
            vector_path.write_text('{"chunk_id":"tampered"}\n', encoding="utf-8")

            second = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
                confirm_shadow_apply=True,
                **confirmation,
            )

            self.assertFalse(second["passed"])
            self.assertFalse(second.get("idempotent_noop", False))
            self.assertIn("shadow-runtime-already-exists", {item["code"] for item in second["blockers"]})

    def test_source_and_shadow_paths_must_be_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=source,
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertFalse(report["passed"])
            self.assertIn("source-shadow-path-equal", {item["code"] for item in report["blockers"]})

    def test_source_and_shadow_paths_must_not_be_nested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            source_before = _tree_snapshot(source)

            for shadow in (source / "nested-shadow", root):
                with self.subTest(shadow=shadow):
                    report = apply_reapproval_plan_shadow(
                        apply_plan_path=plan_path,
                        source_data_dir=source,
                        shadow_data_dir=shadow,
                        artifact_root=root,
                        operator_id="operator-a",
                    )
                    self.assertFalse(report["passed"])
                    self.assertIn("source-shadow-path-nested", {item["code"] for item in report["blockers"]})
                    self.assertEqual(source_before, _tree_snapshot(source))

    def test_missing_source_repository_is_blocked_without_creating_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _source, plan_path = _seed_source_and_plan(root)
            empty_source = root / "empty-source"
            empty_source.mkdir()
            shadow = root / "shadow"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=empty_source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertFalse(report["passed"])
            self.assertIn("source-runtime-repository-missing", {item["code"] for item in report["blockers"]})
            self.assertEqual([], list(empty_source.iterdir()))
            self.assertFalse(shadow.exists())

    def test_v1_apply_plan_contract_is_blocked_even_with_self_consistent_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["execution_contract_version"] = "reapproval-apply-plan-v1"
            payload_sha = _plan_payload_sha256(plan)
            plan["plan_payload_sha256"] = payload_sha
            plan["execution_plan_id"] = f"reapproval_apply_{payload_sha[:16]}"
            v1_plan = root / "apply-plan-v1.json"
            v1_plan.write_text(json.dumps(plan), encoding="utf-8")
            shadow = root / "shadow"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=v1_plan,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertFalse(report["passed"])
            self.assertIn("apply-plan-contract-unsupported", {item["code"] for item in report["blockers"]})
            self.assertFalse(shadow.exists())

    def test_report_output_cannot_mutate_source_or_shadow_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            unsafe_report = source / "executor-report.json"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=root / "shadow",
                artifact_root=root,
                operator_id="operator-a",
                out_json=unsafe_report,
            )

            self.assertFalse(report["passed"])
            self.assertIn("report-output-inside-runtime", {item["code"] for item in report["blockers"]})
            self.assertFalse(unsafe_report.exists())

    def test_tampered_source_review_manifest_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            manifest_path = root / "reapproval_batches.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["batches"][0]["chunks"][0]["approved_content_hash_short"] = "deadbeefdead"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=root / "shadow",
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertFalse(report["passed"])
            self.assertIn("source-artifact-sha-mismatch", {item["code"] for item in report["blockers"]})

    def test_source_chunk_content_drift_after_review_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            repository = JsonRepository(Settings(data_dir=source))
            chunks = repository.get_chunks("doc-a")
            chunks[0] = chunks[0].model_copy(
                update={
                    "text": "Content changed after the operator review",
                    "retrieval_text": "Content changed after the operator review",
                }
            )
            repository.save_chunks("doc-a", chunks)

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=root / "shadow",
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertFalse(report["passed"])
            self.assertIn("source-review-content-hash-mismatch", {item["code"] for item in report["blockers"]})

    def test_flipped_plan_gate_cannot_bypass_blocked_source_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _plan_path = _seed_source_and_plan(root)
            manifest_path = root / "reapproval_batches.json"
            decisions_path = root / "decisions.csv"
            validation_path = root / "validation.json"
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            validation.update(
                passed=False,
                release_gate_status="blocked_pending_operator_decisions",
                blocking_count=1,
            )
            validation_path.write_text(json.dumps(validation), encoding="utf-8")
            plan_path = root / "blocked-plan.json"
            build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest_path,
                decision_template_csv=decisions_path,
                decision_validation_report=validation_path,
                out_json=plan_path,
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            execution_gate = {
                "passed": True,
                "blocker_count": 0,
                "release_gate_status": "ready_for_apply_execution",
            }
            plan.update(execution_gate)
            plan["execution_gate"] = execution_gate
            payload = {
                "execution_contract_version": plan["execution_contract_version"],
                "source_report_artifacts": plan["source_report_artifacts"],
                "batch_plans": plan["batch_plans"],
                "execution_gate": execution_gate,
            }
            payload_sha = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            plan["plan_payload_sha256"] = payload_sha
            plan["execution_plan_id"] = f"reapproval_apply_{payload_sha[:16]}"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=root / "shadow",
                artifact_root=root,
                operator_id="operator-a",
            )

            self.assertFalse(report["passed"])
            self.assertIn("source-decision-validation-not-ready", {item["code"] for item in report["blockers"]})

    def test_defer_only_plan_does_not_create_or_reindex_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _plan_path = _seed_source_and_plan(root)
            manifest_path = root / "reapproval_batches.json"
            decisions_path = root / "decisions.csv"
            with decisions_path.open("w", encoding="utf-8-sig", newline="") as handle:
                fields = [
                    "reapproval_batch_id",
                    "operator_decision",
                    "reviewer_id",
                    "reviewed_at",
                    "decision_notes",
                    "chunk_decision_overrides_json",
                    "approval_scope_confirmation",
                ]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(_decision("batch-approve", "defer"))
                writer.writerow(_decision("batch-reject", "defer"))
            validation_path = root / "validation-defer.json"
            validate_reapproval_decisions(
                reapproval_review_batch_report=manifest_path,
                decision_template_csv=decisions_path,
                out_json=validation_path,
            )
            plan_path = root / "apply-plan-defer.json"
            build_reapproval_apply_plan(
                reapproval_review_batch_report=manifest_path,
                decision_template_csv=decisions_path,
                decision_validation_report=validation_path,
                out_json=plan_path,
            )
            shadow = root / "shadow"

            report = apply_reapproval_plan_shadow(
                apply_plan_path=plan_path,
                source_data_dir=source,
                shadow_data_dir=shadow,
                artifact_root=root,
                operator_id="operator-a",
                confirm_shadow_apply=True,
                **_confirmation(plan_path),
            )

            self.assertTrue(report["passed"], report)
            self.assertEqual("no_shadow_mutations_required", report["status"])
            self.assertFalse(report["shadow_runtime_written"])
            self.assertFalse(shadow.exists())

    def test_failure_after_repository_writes_leaves_no_vector_and_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, plan_path = _seed_source_and_plan(root)
            shadow = root / "shadow"
            confirmation = _confirmation(plan_path)

            with patch(
                "scripts.apply_reapproval_plan_shadow.embed_vector_records",
                side_effect=RuntimeError("embedding failed"),
            ):
                report = apply_reapproval_plan_shadow(
                    apply_plan_path=plan_path,
                    source_data_dir=source,
                    shadow_data_dir=shadow,
                    artifact_root=root,
                    operator_id="operator-a",
                    confirm_shadow_apply=True,
                    **confirmation,
                )

            self.assertFalse(report["passed"])
            self.assertEqual("shadow_apply_failed", report["status"])
            self.assertFalse((shadow / "vector_db" / "default" / "approved_vectors.jsonl").exists())
            state = json.loads((shadow / STATE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual("failed", state["status"])
            self.assertFalse(state["ready_for_promotion"])


def _confirmation(plan_path: Path) -> dict[str, str]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    return {
        "confirm_execution_plan_id": str(plan["execution_plan_id"]),
        "confirm_plan_payload_sha256": str(plan["plan_payload_sha256"]),
    }


def _seed_source_and_plan(
    root: Path,
    *,
    tenant_id: str = "default",
    tenant_storage_isolation: bool = False,
) -> tuple[Path, Path]:
    source = root / "source"
    settings = settings_for_tenant(
        Settings(data_dir=source, tenant_storage_isolation=tenant_storage_isolation),
        tenant_id,
    )
    repository = JsonRepository(settings)
    repository.upsert_document(
        Document(
            document_id="doc-a",
            filename="doc-a.pdf",
            document_name="Demo Regulation",
            file_type="pdf",
            file_hash="hash-a",
            tenant_id=tenant_id,
        )
    )
    chunks = [
        _approved_chunk("chunk-approve", "approval-old-approve", "a" * 64, tenant_id=tenant_id),
        _approved_chunk("chunk-reject", "approval-old-reject", "b" * 64, tenant_id=tenant_id),
    ]
    repository.save_processing_result("doc-a", [], chunks, [])
    old_vector_path = settings.data_dir / "vector_db" / tenant_storage_key(tenant_id) / "approved_vectors.jsonl"
    old_vector_path.parent.mkdir(parents=True, exist_ok=True)
    old_vector_path.write_text('{"chunk_id":"old-vector"}\n', encoding="utf-8")

    manifest_path = root / "reapproval_batches.json"
    manifest = {
        "report_type": "reapproval_review_batch_manifest",
        "generated_at": "2026-07-13T00:00:00+00:00",
        "passed": True,
        "blocker_count": 0,
        "batch_count": 2,
        "worklist_report": {
            "tenant_id": tenant_id,
            "effective_data_dir": str(settings.data_dir.resolve()),
        },
        "batches": [
            _batch(
                "batch-approve",
                "chunk-approve",
                "approval-old-approve",
                "a" * 12,
                review_content_hash(chunks[0]),
            ),
            _batch(
                "batch-reject",
                "chunk-reject",
                "approval-old-reject",
                "b" * 12,
                review_content_hash(chunks[1]),
            ),
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    decisions_path = root / "decisions.csv"
    with decisions_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "reapproval_batch_id",
            "operator_decision",
            "reviewer_id",
            "reviewed_at",
            "decision_notes",
            "chunk_decision_overrides_json",
            "approval_scope_confirmation",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(_decision("batch-approve", "approve_all_reviewed"))
        writer.writerow(_decision("batch-reject", "reject_all"))
    validation_path = root / "validation.json"
    validate_reapproval_decisions(
        reapproval_review_batch_report=manifest_path,
        decision_template_csv=decisions_path,
        out_json=validation_path,
    )
    plan_path = root / "apply_plan.json"
    build_reapproval_apply_plan(
        reapproval_review_batch_report=manifest_path,
        decision_template_csv=decisions_path,
        decision_validation_report=validation_path,
        out_json=plan_path,
    )
    return source, plan_path


def _approved_chunk(
    chunk_id: str,
    approval_id: str,
    approved_hash: str,
    *,
    tenant_id: str = "default",
    document_id: str = "doc-a",
) -> Chunk:
    metadata = {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "chunk_type": "article",
        "approval_status": "approved",
        "approval_id": approval_id,
        "approved_content_hash": approved_hash,
        "security_level": "internal",
    }
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        chunk_type="article",
        text=f"Public regulation text for {chunk_id}",
        retrieval_text=f"Public regulation text for {chunk_id}",
        tenant_id=tenant_id,
        approval_status="approved",
        approval_id=approval_id,
        approved_by="reviewer-old",
        approved_at="2026-07-12T00:00:00+00:00",
        approved_content_hash=approved_hash,
        security_level="internal",
        metadata=metadata,
    )


def _plan_payload_sha256(plan: dict[str, object]) -> str:
    payload = {
        "execution_contract_version": plan.get("execution_contract_version"),
        "source_report_artifacts": plan.get("source_report_artifacts") or [],
        "batch_plans": plan.get("batch_plans") or [],
        "execution_gate": plan.get("execution_gate") or {},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _batch(
    batch_id: str,
    chunk_id: str,
    approval_id: str,
    hash_short: str,
    reviewed_hash: str,
) -> dict[str, object]:
    fingerprint = hashlib.sha256(f"{batch_id}:{chunk_id}".encode()).hexdigest()
    return {
        "reapproval_batch_id": batch_id,
        "document_id": "doc-a",
        "document_name": "Demo Regulation",
        "filename": "doc-a.pdf",
        "suggested_action": "reapprove_and_reindex",
        "review_risk_tier": "medium",
        "chunk_count": 1,
        "chunk_ids": [chunk_id],
        "chunks": [
            {
                "chunk_id": chunk_id,
                "approval_id": approval_id,
                "approved_content_hash_short": hash_short,
                "review_content_hash": reviewed_hash,
                "vector_content_hash_short": hash_short,
            }
        ],
        "reapproval_batch_chunk_fingerprint": fingerprint,
        "worklist_report_path": "reports/reapproval_worklist.json",
        "worklist_report_sha256": "c" * 64,
        "worklist_chunks_path": "reports/reapproval_worklist_chunks.json",
        "worklist_chunks_sha256": "d" * 64,
    }


def _decision(batch_id: str, decision: str) -> dict[str, str]:
    return {
        "reapproval_batch_id": batch_id,
        "operator_decision": decision,
        "reviewer_id": "operator-a",
        "reviewed_at": "2026-07-13T09:00:00+09:00",
        "decision_notes": "reviewed",
        "chunk_decision_overrides_json": "[]",
        "approval_scope_confirmation": "confirmed",
    }


if __name__ == "__main__":
    unittest.main()
