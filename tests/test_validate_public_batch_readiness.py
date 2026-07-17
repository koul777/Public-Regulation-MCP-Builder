from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.institution_profiles import load_institution_profile_registry
from scripts.validate_public_batch_readiness import main, validate_public_batch_readiness


def write_artifacts(root: Path, document_id: str = "doc_1") -> dict[str, str]:
    paths: dict[str, str] = {}
    for field, suffix in {
        "quality_json": "quality.json",
        "quality_md": "quality.md",
        "tables_csv": "tables.csv",
        "tables_jsonl": "tables.jsonl",
    }.items():
        path = root / f"{document_id}.{suffix}"
        path.write_text("{}\n", encoding="utf-8")
        paths[field] = str(path)
    return paths


def passing_report(root: Path) -> dict:
    return {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "input_count": 1,
        "completed_count": 1,
        "skipped_unchanged_count": 0,
        "successful_count": 1,
        "failed_count": 0,
        "quality_passed_count": 1,
        "average_quality_score": 100.0,
        "failed_info_check_total": 0,
        "recommendation_total": 0,
        "table_false_positive_attention_total": 0,
        "agent_review_estimated_total_tokens_total": 0,
        "agent_review_batch_budget_exceeded": False,
        "rows": [
            {
                "filename": "sample.hwp",
                "document_id": "doc_1",
                "status": "completed",
                "quality_passed": True,
                "apba_id": "C0147",
                "profile_id": "strict-public",
                "source_system": "PUBLIC_PORTAL",
                **write_artifacts(root),
            }
        ],
    }


class ValidatePublicBatchReadinessTests(unittest.TestCase):
    def test_passes_ready_batch_with_required_fields_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))

            result = validate_public_batch_readiness(report, required_row_fields=["source_system", "profile_id"])

        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "public_batch_ready")
        self.assertIn("generated_at", result)
        self.assertEqual("2026-07-03T00:00:00+00:00", result["generated_from"])
        self.assertIn("repo_commit", result)
        self.assertEqual("strict", result["readiness_profile"])
        self.assertTrue(result["strict_release_evidence"])
        self.assertEqual(0, result["thresholds"]["max_recommendations"])

    def test_records_review_tolerance_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))
            report["failed_info_check_total"] = 2
            report["recommendation_total"] = 5

            result = validate_public_batch_readiness(
                report,
                max_failed_info=2,
                max_recommendations=5,
            )

        self.assertTrue(result["passed"])
        self.assertEqual("review_tolerance", result["readiness_profile"])
        self.assertFalse(result["strict_release_evidence"])
        self.assertEqual(2, result["thresholds"]["max_failed_info"])
        self.assertEqual(5, result["thresholds"]["max_recommendations"])

    def test_fails_when_required_fields_or_artifacts_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))
            report["rows"][0]["source_system"] = ""
            report["rows"][0]["tables_jsonl"] = str(Path(tmp) / "missing.tables.jsonl")

            result = validate_public_batch_readiness(report, required_row_fields=["source_system"])

        self.assertFalse(result["passed"])
        self.assertTrue(
            any(item["name"] == "required_row_fields_present" and not item["passed"] for item in result["checks"])
        )
        self.assertTrue(any(item["name"] == "required_artifacts_exist" and not item["passed"] for item in result["checks"]))

    def test_applies_institution_profile_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "institution_profiles.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "strict-public": {
                                "required_row_fields": [
                                    "source_system",
                                    "apba_id",
                                    "source_record_id",
                                    "source_file_id",
                                    "profile_id",
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            report = passing_report(root)
            report["rows"][0]["apba_id"] = ""
            report["rows"][0]["source_record_id"] = ""
            report["rows"][0]["source_file_id"] = "file-1"
            registry = load_institution_profile_registry(registry_path)

            result = validate_public_batch_readiness(report, institution_profile_registry=registry, strict_institution_profiles=True)

        self.assertFalse(result["passed"])
        failure = result["failures"]["missing_required_fields"][0]
        self.assertEqual(failure["required_by"], "institution_profile_registry")
        self.assertEqual(failure["missing_fields"], ["apba_id", "source_record_id"])
        self.assertEqual(failure["apba_id"], "")

    def test_strict_institution_profiles_reject_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "institution_profiles.json"
            registry_path.write_text(json.dumps({"profiles": {"known": {}}}), encoding="utf-8")
            report = passing_report(root)
            registry = load_institution_profile_registry(registry_path)

            result = validate_public_batch_readiness(report, institution_profile_registry=registry, strict_institution_profiles=True)

        self.assertFalse(result["passed"])
        self.assertIn("Unknown institution profile_id", result["failures"]["missing_required_fields"][0]["profile_error"])

    def test_fails_when_reused_row_leaks_current_ai_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))
            report.update({"completed_count": 0, "skipped_unchanged_count": 1})
            row = report["rows"][0]
            row.update(
                {
                    "status": "skipped_unchanged",
                    "agent_review_status": "skipped",
                    "agent_review_skip_reason": "reused_unchanged",
                    "agent_review_candidate_count": 0,
                    "agent_review_selected_count": 0,
                    "agent_review_estimated_input_tokens": 0,
                    "agent_review_estimated_output_tokens": 0,
                    "agent_review_estimated_total_tokens": 0,
                    "agent_review_budget_exhausted": False,
                    "agent_review_payload_hash": "sha256:leaked",
                }
            )

            result = validate_public_batch_readiness(report)

        self.assertFalse(result["passed"])
        self.assertEqual(result["failures"]["reused_ai_evidence_leaks"][0]["leaked_fields"]["agent_review_payload_hash"], "sha256:leaked")

    def test_fails_when_public_portal_source_selection_warning_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))
            report["rows"][0].update(
                {
                    "source_file_id": "98765",
                    "latest_file_no": "98766",
                    "latest_file_name": "latest-rules.zip",
                    "latest_file_ext": ".zip",
                    "selected_latest_file": "False",
                    "selection_policy": "latest_supported_fallback",
                    "selection_warning": "selected_supported_file_is_not_latest_public_portal_file",
                }
            )

            result = validate_public_batch_readiness(report)

        self.assertFalse(result["passed"])
        self.assertTrue(
            any(item["name"] == "source_selection_has_no_warnings" and not item["passed"] for item in result["checks"])
        )
        warning = result["failures"]["source_selection_warnings"][0]
        self.assertEqual(warning["source_file_id"], "98765")
        self.assertEqual(warning["latest_file_no"], "98766")
        self.assertEqual(warning["selection_policy"], "latest_supported_fallback")
        self.assertEqual(warning["selection_warning"], "selected_supported_file_is_not_latest_public_portal_file")

    def test_embedding_estimate_is_recorded_without_requiring_provider_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))

            result = validate_public_batch_readiness(
                report,
                embedding_cost_estimates=[
                    {
                        "report_type": "embedding_cost_estimate",
                        "record_count": 3,
                        "estimated_input_tokens": 42,
                        "budget_evaluation_status": "token_only",
                        "api_call_count": 0,
                        "mode": "estimate_only",
                    }
                ],
            )

        self.assertTrue(result["passed"])
        self.assertEqual(result["summary"]["embedding_estimated_input_tokens"], 42)
        self.assertEqual(result["summary"]["semantic_embedding_provider_readiness"], "estimate_only_local_validation")

    def test_requires_embedding_approval_and_budget_when_semantic_provider_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))

            result = validate_public_batch_readiness(
                report,
                embedding_cost_estimates=[
                    {
                        "report_type": "embedding_cost_estimate",
                        "record_count": 3,
                        "estimated_input_tokens": 42,
                        "provider_model": "future-semantic-embedding",
                        "price_per_1m_tokens": None,
                        "budget": None,
                        "budget_evaluation_status": "token_only",
                        "budget_exceeded": False,
                        "api_call_count": 0,
                        "mode": "estimate_only",
                    }
                ],
                require_semantic_embedding_approval=True,
            )

        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"]["semantic_embedding_provider_readiness"], "needs_attention")
        reasons = {item["reason"] for item in result["failures"]["embedding_readiness"]}
        self.assertIn("missing_embedding_approval_reference", reasons)
        self.assertIn("semantic_embedding_missing_price", reasons)

    def test_main_returns_failure_for_failed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.json"
            report = passing_report(Path(tmp))
            report["failed_count"] = 1
            path.write_text(json.dumps(report), encoding="utf-8")

            with patch("sys.argv", ["validate_public_batch_readiness.py", "--batch-report", str(path)]):
                code = main()

        self.assertEqual(code, 2)

    def test_summarizes_ocr_required_failures(self) -> None:
        report = {
            "generated_at": "2026-07-03T00:00:00+00:00",
            "input_count": 1,
            "successful_count": 0,
            "failed_count": 1,
            "failure_category_counts": {"ocr_required": 1},
            "ocr_required_count": 1,
            "ocr_required_page_count": 2,
            "retry_recommended_failed_count": 0,
            "quality_passed_count": 0,
            "average_quality_score": 0,
            "failed_info_check_total": 0,
            "recommendation_total": 0,
            "table_false_positive_attention_total": 0,
            "agent_review_estimated_total_tokens_total": 0,
            "agent_review_batch_budget_exceeded": False,
            "rows": [
                {
                    "filename": "scan.pdf",
                    "document_id": "doc_scan",
                    "status": "failed",
                    "apba_id": "C9999",
                    "failure_category": "ocr_required",
                    "ocr_required": True,
                    "ocr_page_count": 2,
                }
            ],
        }

        result = validate_public_batch_readiness(report)

        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"]["failure_category_counts"], {"ocr_required": 1})
        self.assertEqual(result["summary"]["ocr_required_count"], 1)
        self.assertEqual(result["summary"]["ocr_required_page_count"], 2)
        self.assertEqual(result["failures"]["failed_rows"][0]["filename"], "scan.pdf")
        self.assertEqual(result["failures"]["failed_rows"][0]["apba_id"], "C9999")
        self.assertEqual(result["failures"]["ocr_required_rows"][0]["ocr_page_count"], 2)
        self.assertTrue(
            any(item["name"] == "no_ocr_required_rows" and not item["passed"] for item in result["checks"])
        )

    def test_exposes_failed_info_and_recommendation_row_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = passing_report(Path(tmp))
            report["failed_info_check_total"] = 1
            report["recommendation_total"] = 2
            report["rows"][0]["failed_info_check_count"] = 1
            report["rows"][0]["recommendation_count"] = 2
            report["rows"][0]["top_failed_info_check"] = "article_title_missing"
            report["rows"][0]["top_recommendation"] = "Review table structure."

            result = validate_public_batch_readiness(report)

        self.assertFalse(result["passed"])
        self.assertEqual(
            "article_title_missing",
            result["failures"]["failed_info_check_rows"][0]["top_failed_info_check"],
        )
        self.assertEqual(
            "Review table structure.",
            result["failures"]["recommendation_rows"][0]["top_recommendation"],
        )


if __name__ == "__main__":
    unittest.main()
