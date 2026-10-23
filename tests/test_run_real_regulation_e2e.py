from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts.run_real_regulation_e2e import (
    EvidenceError,
    RunOptions,
    active_runtime_integrity,
    managed_local_api,
    _record_stage,
    options_from_config,
    require_private_path,
    runtime_fingerprint,
    run_real_regulation_e2e,
    validate_options,
    verify_corpus,
)


class RealRegulationE2eEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="regression_evidence_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "source-runtime"
        self.runtime.mkdir()
        (self.runtime / "manifest.json").write_text('{"approved": true}', encoding="utf-8")
        self.pdf = self.root / "synthetic.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nsynthetic test document\n")
        self.manifest = self.root / "corpus.json"
        self.documents = [{
            "document_id": "synthetic-document",
            "snapshot_pdf": str(self.pdf),
            "source_sha256": hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
            "human_review_complete": False,
        }]
        self._save_manifest()
        self.query_spec = self.root / "queries.json"
        self.query_spec.write_text(json.dumps({"queries": [{"id": "synthetic-case", "query": "synthetic question", "target_document_ids": ["synthetic-document"]}]}), encoding="utf-8")
        self.options = RunOptions(runtime_source=self.runtime, corpus_manifest=self.manifest,
            query_spec=self.query_spec, output_dir=self.root / "new-run", tenant_id="synthetic-tenant")

    def _save_manifest(self) -> None:
        self.manifest.write_text(json.dumps({"documents": self.documents, "label_provenance": "synthetic"}), encoding="utf-8")

    def _run_managed_process(self, process: Mock) -> dict:
        self.options.output_dir.mkdir()
        with patch("scripts.run_real_regulation_e2e.subprocess.Popen", return_value=process) as popen, patch(
            "app.core.local_http.local_urlopen", return_value=nullcontext(Mock(status=200))
        ), patch("scripts.run_real_regulation_e2e.secrets.token_urlsafe", return_value="synthetic-secret-not-for-logs"):
            with managed_local_api(self.options, runtime=self.runtime, endpoint="http://127.0.0.1:11434"):
                pass
        report_text = (self.options.output_dir / "api-process.json").read_text(encoding="utf-8")
        self.assertNotIn("synthetic-secret-not-for-logs", report_text)
        self.assertEqual("1", popen.call_args.kwargs["env"]["PYTHONFAULTHANDLER"])
        return json.loads(report_text)

    def test_native_process_exit_is_preserved_before_owner_cleanup(self) -> None:
        process = Mock(pid=1234)
        process.poll.side_effect = [None, 0xC0000005, 0xC0000005]
        report = self._run_managed_process(process)
        self.assertEqual(1234, report["pid"])
        self.assertEqual(0xC0000005, report["exit_code_before_cleanup"])
        self.assertEqual(0xC0000005, report["exit_code_after_cleanup"])
        self.assertEqual("exited_before_cleanup", report["cleanup_action"])
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_owned_termination_is_not_reported_as_a_preexisting_crash(self) -> None:
        process = Mock(pid=2345)
        process.poll.side_effect = [None, None, -15]
        report = self._run_managed_process(process)
        self.assertIsNone(report["exit_code_before_cleanup"])
        self.assertEqual("terminated_by_owner", report["cleanup_action"])
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_owned_process_that_ignores_termination_is_killed_and_recorded(self) -> None:
        process = Mock(pid=3456)
        process.poll.side_effect = [None, None, -9]
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 15), -9]
        report = self._run_managed_process(process)
        self.assertIsNone(report["exit_code_before_cleanup"])
        self.assertEqual("killed_by_owner_after_timeout", report["cleanup_action"])
        process.kill.assert_called_once()

    def test_valid_configuration_does_not_create_output_or_approve(self) -> None:
        before = runtime_fingerprint(self.runtime)
        result = validate_options(self.options)
        self.assertEqual(1, result["query_count"])
        self.assertFalse(result["corpus"]["human_review_complete"])
        self.assertEqual(0, result["corpus"]["new_approval_records_created"])
        self.assertFalse(self.options.output_dir.exists())
        self.assertEqual(before, runtime_fingerprint(self.runtime))

    def test_modified_raw_source_fails_even_with_approval_metadata(self) -> None:
        self.pdf.write_bytes(b"%PDF-1.7\nchanged contents\n")
        with self.assertRaisesRegex(EvidenceError, "corpus_source_hash_mismatch"):
            verify_corpus(self.manifest)

    def test_human_review_text_true_is_not_review_attestation(self) -> None:
        self.documents[0]["human_review_complete"] = "true"
        self._save_manifest()
        self.assertFalse(verify_corpus(self.manifest)["human_review_complete"])

    def test_duplicate_document_ids_are_rejected(self) -> None:
        self.documents.append(dict(self.documents[0]))
        self._save_manifest()
        with self.assertRaisesRegex(EvidenceError, "corpus_document_identity_invalid"):
            verify_corpus(self.manifest)

    def test_same_raw_document_under_two_ids_counts_once(self) -> None:
        self.documents.append({**self.documents[0], "document_id": "synthetic-copy"})
        self._save_manifest()
        result = verify_corpus(self.manifest)
        self.assertEqual(2, result["document_count"])
        self.assertEqual(1, result["unique_source_count"])

    def test_pdf_extension_without_pdf_signature_fails(self) -> None:
        self.pdf.write_bytes(b"not a PDF")
        self.documents[0]["source_sha256"] = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self._save_manifest()
        with self.assertRaisesRegex(EvidenceError, "corpus_pdf_signature_invalid"):
            verify_corpus(self.manifest)

    def test_output_cannot_overwrite_or_contain_runtime(self) -> None:
        for destination in (self.runtime, self.runtime / "child", self.root):
            with self.subTest(destination=destination.name):
                with self.assertRaisesRegex(EvidenceError, "output_overlaps_runtime_source"):
                    validate_options(replace(self.options, output_dir=destination))

    def test_existing_evidence_is_not_overwritten(self) -> None:
        self.options.output_dir.mkdir()
        evidence = self.options.output_dir / "existing.json"
        evidence.write_text("preserve me", encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "output_must_be_new_or_empty"):
            validate_options(self.options)
        self.assertEqual("preserve me", evidence.read_text(encoding="utf-8"))

    def test_public_paths_are_rejected_before_creating_them(self) -> None:
        public = self.root / "public"
        public.mkdir()
        with self.assertRaisesRegex(EvidenceError, "private_path_inside_public_checkout"):
            require_private_path(public / "reports" / "private.json", project_root=public)
        self.assertFalse((public / "reports").exists())

    def test_relative_private_path_rejected(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "private_path_must_be_absolute"):
            require_private_path(Path("reports/private.json"))

    def test_private_link_is_rejected_before_resolving_away_identity(self) -> None:
        linked = self.root / "linked-directory" / "corpus.json"
        with patch("scripts.run_real_regulation_e2e._is_link", side_effect=lambda part: part == linked.parent):
            with self.assertRaisesRegex(EvidenceError, "private_path_links_not_allowed"):
                require_private_path(linked)

    def test_external_model_endpoint_and_credentials_rejected(self) -> None:
        for endpoint in ("https://example.invalid", "http://secret@127.0.0.1:11434", "http://127.0.0.1:11434/?key=secret",
                         "http://127.0.0.1:11434/other-api", "http://127.0.0.1:0"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(EvidenceError, "ollama_endpoint_must_be_loopback"):
                    validate_options(replace(self.options, ollama_endpoint=endpoint))

    def test_ambiguous_storage_mode_rejected(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "storage_mode_must_be_boolean"):
            validate_options(replace(self.options, tenant_storage_isolation="false"))  # type: ignore[arg-type]

    def test_admin_clearance_is_not_used_for_regression(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "unsupported_regression_api_role"):
            validate_options(replace(self.options, api_role="admin"))

    def test_mixed_viewer_api_and_operator_mcp_run_is_rejected(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "unsupported_regression_api_role"):
            validate_options(replace(self.options, api_role="viewer"))

    def test_nonshared_search_overrides_are_rejected_before_any_stage(self) -> None:
        for key, value in (("document_id", "synthetic-document"), ("security_levels", ["public"]),
                           ("department_ids", ["synthetic-department"]), ("top_k", 8),
                           ("as_of_date", "2030-01-01")):
            case = {"id": "case", "query": "question", "request_overrides": {key: value}}
            self.query_spec.write_text(json.dumps({"queries": [case]}), encoding="utf-8")
            with self.subTest(key=key), self.assertRaisesRegex(EvidenceError, "retrieval_overrides_not_shared_by_all_stages"):
                validate_options(self.options)

    def test_shared_security_filter_requires_unique_operator_levels(self) -> None:
        for levels in ((), ("secret",), ("public", "public"), (["public"],), "internal"):
            with self.subTest(levels=levels), self.assertRaisesRegex(EvidenceError, "security_levels_must_be_unique_operator_levels"):
                validate_options(replace(self.options, security_levels=levels))

    def test_global_defaults_cannot_silently_change_only_the_api_search_scope(self) -> None:
        for override in ({"security_levels": ["public"]}, {"document_id": "synthetic-document"},
                         {"as_of_date": "2030-01-01"}, {"top_k": 8}):
            with self.subTest(override=override), self.assertRaisesRegex(EvidenceError, "retrieval_overrides_not_shared_by_all_stages"):
                validate_options(replace(self.options, request_defaults=override))

    def test_mixed_scope_is_not_silently_evaluated_in_one_profile(self) -> None:
        self.query_spec.write_text(json.dumps({"queries": [{"id": "case", "query": "question", "profile_id": "another-profile"}]}), encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "mixed_profile_query_spec_not_supported"):
            validate_options(self.options)

    def test_nested_profile_override_cannot_change_declared_runtime_scope(self) -> None:
        for override in ({"profile_id": "another-profile"}, {"tenant_id": "another-tenant"}):
            self.query_spec.write_text(json.dumps({"queries": [{"id": "case", "query": "question", "request_overrides": override}]}), encoding="utf-8")
            with self.subTest(override=override), self.assertRaises(EvidenceError):
                validate_options(self.options)

    def test_global_and_case_scope_must_match_authenticated_runtime(self) -> None:
        for scope in ({"tenant_id": "different-tenant"}, {"profile_id": "different-profile"},
                      {"tenant_storage_isolation": "false"}):
            for location in ("global", "case"):
                case = {"id": "case", "query": "question"}
                payload = {"queries": [case]}
                (payload if location == "global" else case)["scope"] = scope
                self.query_spec.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(scope=scope, location=location), self.assertRaisesRegex(EvidenceError, "query_scope_does_not_match_runtime"):
                    validate_options(self.options)

    def test_live_request_cannot_select_unobserved_backend(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "live_regression_requires_ollama"):
            validate_options(replace(self.options, request_defaults={"llm_backend": "other-backend"}))

    def test_timeout_and_selected_ids_are_not_coerced_from_ambiguous_types(self) -> None:
        for timeout in (True, "600", 0, 601):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(EvidenceError, "runtime_configuration_invalid"):
                validate_options(replace(self.options, timeout_seconds=timeout))  # type: ignore[arg-type]
        config = {"runtime_source": str(self.runtime), "corpus_manifest": str(self.manifest),
                  "query_spec": str(self.query_spec), "tenant_id": "synthetic-tenant"}
        with self.assertRaisesRegex(EvidenceError, "preprocessing_document_ids_must_be_string_sequence"):
            options_from_config({**config, "preprocessing_document_ids": "synthetic-document"}, self.options.output_dir)

    def test_query_source_must_be_present_in_real_corpus(self) -> None:
        self.query_spec.write_text(json.dumps({"queries": [{"id": "case", "query": "question", "target_document_ids": ["unrelated-document"]}]}), encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "query_or_preprocessing_document_missing_from_corpus"):
            validate_options(self.options)

    def test_empty_and_duplicate_query_ids_rejected(self) -> None:
        for queries in ([], [{"id": "same"}, {"id": "same"}], [{"id": ""}]):
            self.query_spec.write_text(json.dumps({"queries": queries}), encoding="utf-8")
            with self.subTest(queries=queries):
                with self.assertRaises(EvidenceError):
                    validate_options(self.options)

    def test_runtime_tamper_changes_fingerprint(self) -> None:
        first = runtime_fingerprint(self.runtime)
        (self.runtime / "manifest.json").write_text('{"approved":false}', encoding="utf-8")
        self.assertNotEqual(first["sha256"], runtime_fingerprint(self.runtime)["sha256"])

    def _active_integrity(self, before):
        return active_runtime_integrity(self.runtime, before, tenant_id="synthetic-tenant", tenant_storage_isolation=False)

    def test_active_runtime_allows_only_declared_append_only_audit(self) -> None:
        audit = self.runtime / "repository" / "api_audit.jsonl"
        audit.parent.mkdir()
        audit.write_bytes(b'{"record_id":"old"}\n')
        before = runtime_fingerprint(self.runtime)
        with audit.open("ab") as stream:
            stream.write(b'{"record_id":"new"}\n')
        (audit.parent / ".api_audit.lock").touch()
        self.assertTrue(self._active_integrity(before)["passed"])
        audit.write_bytes(b'{"record_id":"bad"}\n')
        self.assertFalse(self._active_integrity(before)["passed"])

    def test_active_runtime_rejects_unexpected_files_and_malformed_appends(self) -> None:
        before = runtime_fingerprint(self.runtime)
        audit = self.runtime / "repository" / "api_audit.jsonl"
        audit.parent.mkdir()
        for content in (b'not JSON\n', b'{"id":1,"id":2}\n', b'{"id":1}', b'[]\n'):
            audit.write_bytes(content)
            with self.subTest(content=content):
                self.assertFalse(self._active_integrity(before)["passed"])
        audit.write_bytes(b'{"record_id":"new"}\n')
        (audit.parent / "new_chunks.json").write_text("[]", encoding="utf-8")
        self.assertFalse(self._active_integrity(before)["passed"])

    def test_hierarchy_lock_accepts_only_the_actual_single_null_byte_initialization(self) -> None:
        before = runtime_fingerprint(self.runtime)
        lock = self.runtime / "hierarchy" / ".regulation_hierarchy.sqlite3.reg-rag.lock"
        lock.parent.mkdir()
        lock.write_bytes(b"\0")
        self.assertTrue(self._active_integrity(before)["passed"])
        for content in (b"x", b"\0\0"):
            lock.write_bytes(content)
            with self.subTest(content=content):
                self.assertFalse(self._active_integrity(before)["passed"])

    def test_other_tenant_audit_is_not_an_allowed_runtime_output(self) -> None:
        before = runtime_fingerprint(self.runtime)
        audit = self.runtime / "tenants" / "another-tenant" / "repository" / "api_audit.jsonl"
        audit.parent.mkdir(parents=True)
        audit.write_bytes(b'{"record_id":"new"}\n')
        result = active_runtime_integrity(self.runtime, before, tenant_id="synthetic-tenant", tenant_storage_isolation=True)
        self.assertFalse(result["passed"])

    def test_existing_repository_lock_bytes_cannot_change(self) -> None:
        lock = self.runtime / "repository" / ".write.lock"
        lock.parent.mkdir()
        lock.touch()
        before = runtime_fingerprint(self.runtime)
        lock.write_bytes(b"unexpected contents")
        self.assertFalse(self._active_integrity(before)["passed"])

    def _run_synthetic_stages(self, *, mcp_effect=None):
        with ExitStack() as stack:
            stack.enter_context(patch("scripts.report_metadata.capture_mcp_performance_source_state", return_value={}))
            stack.enter_context(patch("scripts.report_metadata.finalize_mcp_performance_source_state", return_value={"stable": True}))
            stack.enter_context(patch("scripts.report_metadata.current_repo_commit", return_value="synthetic"))
            stack.enter_context(patch("scripts.real_regulation_corpus_probe.run_corpus_probe", return_value={
                "passed": True, "results": [{"source_document_id": "synthetic-document", "passed": True}]}))
            retrieval = stack.enter_context(patch("scripts.evaluate_mcp_retrieval_quality.evaluate_mcp_retrieval_quality", return_value={"passed": True}))
            mcp = stack.enter_context(patch("scripts.real_regulation_mcp_probe.run_mcp_probe", side_effect=mcp_effect, return_value={"passed": True}))
            stack.enter_context(patch("scripts.evaluate_live_local_llm.LocalOllamaObserver"))
            live = stack.enter_context(patch("scripts.evaluate_live_local_llm.evaluate_live_local_llm", return_value={"passed": True}))
            stack.enter_context(patch("scripts.run_real_regulation_e2e.managed_local_api", return_value=nullcontext(("http://127.0.0.1:1", {}))))
            report = run_real_regulation_e2e(self.options)
        return report, retrieval, mcp, live

    def test_copied_runtime_mutation_cannot_pass_even_when_all_stages_pass(self) -> None:
        def tamper(**kwargs):
            (kwargs["data_dir"] / "manifest.json").write_text('{"approved":false}', encoding="utf-8")
            return {"passed": True}

        report, *_ = self._run_synthetic_stages(mcp_effect=tamper)
        self.assertEqual("passed", report["stages"]["input_integrity"]["status"])
        self.assertEqual("failed", report["stages"]["active_runtime_integrity"]["status"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["product_release_ready"])

    def test_all_search_stages_receive_the_same_declared_security_filter(self) -> None:
        self.options = replace(self.options, security_levels=("public", "internal"))
        report, retrieval, mcp, live = self._run_synthetic_stages()
        self.assertTrue(report["passed"])
        expected = ["public", "internal"]
        self.assertEqual(expected, retrieval.call_args.kwargs["security_levels"])
        self.assertEqual(expected, mcp.call_args.kwargs["query_specs"][0]["security_levels"])
        self.assertEqual(expected, live.call_args.kwargs["request_defaults"]["security_levels"])

    def test_retrieval_thresholds_come_from_the_kpi_policy(self) -> None:
        from scripts.check_real_regulation_kpi_guardrails import load_policy

        policy = load_policy()
        report, retrieval, *_ = self._run_synthetic_stages()
        kwargs = retrieval.call_args.kwargs
        self.assertEqual(policy["targets"]["retrieval_recall_at_5"]["minimum"], kwargs["min_recall_at_5"])
        self.assertEqual(policy["targets"]["retrieval_mrr"]["minimum"], kwargs["min_mrr"])
        self.assertEqual((0.0, 1.0), (kwargs["max_no_evidence_false_positive_rate"],
                                      kwargs["min_no_evidence_abstention_rate"]))
        self.assertEqual({"policy_id": policy["policy_id"], "policy_version": policy["policy_version"],
                          "sha256": policy["sha256"]}, report["kpi_policy"])

    def test_runtime_change_during_hash_is_rejected(self) -> None:
        from scripts import run_real_regulation_e2e as module
        original = module._sha256

        def changing_hash(path: Path) -> str:
            result = original(path)
            path.write_bytes(path.read_bytes() + b" ")
            return result

        with patch.object(module, "_sha256", side_effect=changing_hash):
            with self.assertRaisesRegex(EvidenceError, "runtime_changed_during_fingerprint"):
                runtime_fingerprint(self.runtime)

    def test_failed_stage_cannot_pass_on_truthy_report_or_exception(self) -> None:
        report = {"stages": {}}
        for value in ("true", 1, None):
            result = _record_stage(report, "stage", lambda: {"passed": value})
            self.assertEqual("failed", result["status"])

        def failure() -> dict:
            raise RuntimeError("raw provider response and private path must not escape")

        result = _record_stage(report, "stage", failure)
        self.assertEqual("failed", result["status"])
        self.assertEqual("RuntimeError", result["error_type"])
        self.assertNotIn("raw provider", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
