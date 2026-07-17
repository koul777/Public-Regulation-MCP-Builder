from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.report_metadata import current_repo_commit


@dataclass(frozen=True)
class CommitSliceRule:
    slice_id: str
    title: str
    dependency_order: int
    patterns: tuple[str, ...]
    verification_commands: tuple[str, ...]
    rationale: str


COMMIT_SLICE_RULES: tuple[CommitSliceRule, ...] = (
    CommitSliceRule(
        "parsing_accuracy_evidence",
        "Parsing Accuracy Evidence",
        10,
        (
            "app/parsers/**",
            "app/processors/chunker.py",
            "app/processors/exporter.py",
            "app/processors/metadata_extractor.py",
            "app/processors/normalizer.py",
            "app/processors/quality_gate.py",
            "app/processors/structure_detector.py",
            "app/processors/table_extractor.py",
            "scripts/*parsing_goldset*",
            "scripts/*table*",
            "scripts/analyze_regulation_corpus.py",
            "tests/test_hwp_parser.py",
            "tests/test_hwpx_parser.py",
            "tests/test_pdf_parser.py",
            "tests/test_chunker.py",
            "tests/test_exporter.py",
            "tests/test_metadata_extractor.py",
            "tests/test_normalizer.py",
            "tests/test_quality_gate.py",
            "tests/test_structure_detector.py",
            "tests/test_*parsing_goldset*",
            "tests/test_*table*",
            "tests/test_analyze_regulation_corpus_parsing_accuracy.py",
            "docs/*parsing*",
            "docs/*table*",
            "config/*goldset*",
        ),
        (
            "python -m unittest tests.test_build_parsing_goldset_completion_board tests.test_analyze_regulation_corpus_parsing_accuracy tests.test_audit_table_preprocessing_claim_gate -v",
            "python scripts\\build_parsing_goldset_completion_board.py --labels-csv reports\\parsing_manual_goldset_labels_20260710-current.csv --packet-dir reports\\parsing_goldset_review_packets_current_20260710 --out-json reports\\parsing_goldset_completion_board_current.json --out-md reports\\parsing_goldset_completion_board_current.md",
        ),
        "Parser/table evidence should be reviewed before making product readiness claims.",
    ),
    CommitSliceRule(
        "approval_reapproval_gates",
        "Approval And Reapproval Gates",
        20,
        (
            "app/agents/execution_audit.py",
            "app/agents/execution_guard.py",
            "app/agents/review_policy.py",
            "app/core/pipeline.py",
            "app/schemas/**",
            "app/services/document_service.py",
            "app/services/processing_service.py",
            "app/services/review_*",
            "app/api/routes_documents.py",
            "scripts/*approval*",
            "scripts/*reapproval*",
            "scripts/build_revision_impact_report.py",
            "scripts/validate_temporal_ambiguity_policy_decisions.py",
            "tests/test_agent_review_*",
            "tests/test_pipeline.py",
            "tests/test_processing_service.py",
            "tests/test_build_revision_impact_report.py",
            "tests/test_validate_temporal_ambiguity_policy_decisions.py",
            "tests/test_*approval*",
            "tests/test_routes_documents.py",
            "docs/*approval*",
        ),
        (
            "python -m unittest tests.test_review_workflow_service tests.test_review_decision_service tests.test_routes_documents tests.test_validate_reapproval_decisions tests.test_build_reapproval_apply_plan -v",
            "python scripts\\validate_reapproval_decisions.py --reapproval-review-batch-report reports\\reapproval_review_batches_current.json --decision-template-csv reports\\reapproval_review_batch_decisions_current.csv --fail-on-issue",
        ),
        "Approval evidence and reapproval decisions control what reaches Vector DB and MCP.",
    ),
    CommitSliceRule(
        "rag_mcp_answer_accuracy",
        "RAG MCP Answer Accuracy",
        30,
        (
            "app/api/routes_rag.py",
            "app/mcp_server/**",
            "app/rag/**",
            "app/retrieval/**",
            "app/processors/answer_profile.py",
            "scripts/*mcp*",
            "scripts/*rag*",
            "scripts/*answer*",
            "scripts/compare_rag_mcp_accuracy.py",
            "scripts/evaluate_rag_retrieval.py",
            "scripts/benchmark_mcp*",
            "tests/test_*mcp*",
            "tests/test_*rag*",
            "tests/test_extractive_answer.py",
            "tests/test_compare_rag_mcp_accuracy.py",
            "tests/test_evaluate_rag_retrieval.py",
            "config/*mcp*_query_specs*.json",
            "docs/*mcp*",
            "docs/*rag*",
        ),
        (
            "python -m unittest tests.test_routes_rag tests.test_regulation_mcp_tools tests.test_extractive_answer tests.test_compare_rag_mcp_accuracy tests.test_export_mcp_demo_answers -v",
            "python scripts\\build_mcp_answer_evidence_bundle.py --accuracy-comparison-report reports\\simple_rag_vs_mcp_accuracy_pilot20_20260710.json --query-benchmark-report reports\\mcp_query_benchmark_pilot20_20260710.json --demo-answer-report reports\\mcp_demo_answers_pilot20_20260710.json --rag-eval-report reports\\rag_retrieval_eval_pilot20_20260710.json --product-readiness-report reports\\mcp_product_readiness_pilot_gate_current_20260711.json --out-json reports\\mcp_answer_evidence_current.json",
        ),
        "Answer accuracy changes depend on approved retrieval behavior and citation evidence.",
    ),
    CommitSliceRule(
        "institution_ingestion_runtime",
        "Institution Ingestion Runtime",
        35,
        (
            "app/core/batch_failure_alerting.py",
            "app/core/institution_profiles.py",
            "app/ingestion/__init__.py",
            "config/public_portal_*",
            "scripts/batch_process_regulations.py",
            "scripts/fetch_public_portal_laws.py",
            "scripts/fetch_public_portal_internal_rules.py",
            "scripts/export_vectordb_ingestion.py",
            "scripts/upsert_vectordb_ingestion.py",
            "scripts/validate_public_batch_readiness.py",
            "scripts/build_institution_profile_registry_from_batch.py",
            "tests/test_api_sample_e2e.py",
            "tests/test_batch_process_regulations.py",
            "tests/test_emit_batch_failure_alert.py",
            "tests/test_embed_vectordb_records.py",
            "tests/test_embedding_adapter.py",
            "tests/test_estimate_embedding_cost.py",
            "tests/test_export_vectordb_ingestion.py",
            "tests/test_fetch_public_portal_laws.py",
            "tests/test_fetch_public_portal_internal_rules.py",
            "tests/test_institution_profiles.py",
            "tests/test_validate_public_batch_readiness.py",
            "tests/test_build_institution_profile_registry_from_batch.py",
            "docs/codex-guidance-supplement-public-institution-real-sample.md",
            "docs/pilot_acceptance_and_evidence_ko.md",
            "docs/pilot_overview_ko.md",
        ),
        (
            "python -m unittest tests.test_batch_process_regulations tests.test_fetch_public_portal_laws tests.test_fetch_public_portal_internal_rules tests.test_validate_public_batch_readiness tests.test_institution_profiles -v",
            "python -m unittest tests.test_export_vectordb_ingestion tests.test_upsert_vectordb_ingestion tests.test_embedding_adapter tests.test_embed_vectordb_records -v",
        ),
        "PUBLIC_PORTAL/profile ingestion and runtime export mechanics are separate from parser accuracy claims.",
    ),
    CommitSliceRule(
        "release_handoff_safety",
        "Release And Handoff Safety",
        40,
        (
            "app/main.py",
            "app/api/routes_exports.py",
            "scripts/run_private_release_gate.py",
            "scripts/run_private_release_smoke.py",
            "scripts/check_private_release_readiness.py",
            "scripts/build_private_release_manifest.py",
            "scripts/build_release_evidence_index.py",
            "scripts/verify_release_evidence_bundle.py",
            "scripts/build_worktree_commit_plan.py",
            "scripts/run_nightly_smoke.py",
            "scripts/run_release_harness.py",
            "scripts/run_fresh_clone_rehearsal.py",
            "scripts/run_sdist_rehearsal.py",
            "scripts/check_mcp_connection_readiness.py",
            "scripts/audit_mcp_index_visibility.py",
            "scripts/build_mcp_handoff_report.py",
            "tests/test_private_release*",
            "tests/test_build_worktree_commit_plan.py",
            "tests/test_run_nightly_smoke.py",
            "tests/test_run_release_harness.py",
            "tests/test_run_fresh_clone_rehearsal.py",
            "tests/test_run_sdist_rehearsal.py",
            "tests/test_build_release_evidence_index.py",
            "tests/test_verify_release_evidence_bundle.py",
            "tests/test_check_mcp_connection_readiness.py",
            "tests/test_audit_mcp_index_visibility.py",
            "tests/test_build_mcp_handoff_report.py",
            "docs/private_release*",
            "docs/mcp_quickconnect_ko.md",
            "docs/mcp_client_config_examples_ko.md",
            "docs/internal_mcp_operation_ko.md",
            "docs/operator_quickstart_ko.md",
        ),
        (
            "python -m unittest tests.test_private_release_gate tests.test_private_release_script_execution tests.test_private_release_docs tests.test_private_release_runbook tests.test_operator_quickstart_ko -v",
            "python -m unittest tests.test_build_release_evidence_index tests.test_verify_release_evidence_bundle tests.test_build_mcp_handoff_report tests.test_check_mcp_connection_readiness tests.test_audit_mcp_index_visibility -v",
        ),
        "Release and MCP handoff gates should be committed together with their operator docs and tests.",
    ),
    CommitSliceRule(
        "temporal_revision_evidence",
        "Temporal Revision Evidence",
        45,
        (
            "scripts/audit_runtime_version_drift.py",
            "scripts/backfill_public_portal_runtime_identity.py",
            "scripts/backfill_temporal_metadata.py",
            "scripts/build_pilot_blocker_action_board.py",
            "scripts/build_profile_provenance_report.py",
            "scripts/build_review_queue_triage.py",
            "scripts/build_temporal_ambiguity_policy_decision_sheet.py",
            "scripts/build_temporal_ambiguity_review_scope.py",
            "scripts/build_temporal_backfill_shadow_runtime.py",
            "scripts/export_relation_graph.py",
            "scripts/summarize_relation_bridges.py",
            "scripts/summarize_review_triage_labels.py",
            "scripts/summarize_strict_public_readiness_gaps.py",
            "tests/test_audit_runtime_version_drift.py",
            "tests/test_backfill_public_portal_runtime_identity.py",
            "tests/test_backfill_temporal_metadata.py",
            "tests/test_build_pilot_blocker_action_board.py",
            "tests/test_build_profile_provenance_report.py",
            "tests/test_build_review_queue_triage.py",
            "tests/test_build_temporal_ambiguity_policy_decision_sheet.py",
            "tests/test_build_temporal_ambiguity_review_scope.py",
            "tests/test_build_temporal_backfill_shadow_runtime.py",
            "tests/test_export_relation_graph.py",
            "tests/test_summarize_relation_bridges.py",
            "tests/test_summarize_review_triage_labels.py",
            "tests/test_summarize_strict_public_readiness_gaps.py",
        ),
        (
            "python -m unittest tests.test_backfill_temporal_metadata tests.test_build_temporal_ambiguity_review_scope tests.test_validate_temporal_ambiguity_policy_decisions tests.test_build_pilot_blocker_action_board -v",
        ),
        "Temporal ambiguity, revision impact, and blocker boards are evidence-generation workstreams.",
    ),
    CommitSliceRule(
        "security_tenant_infrastructure",
        "Security Tenant Infrastructure",
        50,
        (
            "app/core/security.py",
            "app/core/tenant_access.py",
            "app/core/config.py",
            "app/core/api_audit.py",
            "app/ingestion/vector_*",
            "app/storage/repository.py",
            "docker-compose.yml",
            ".env.example",
            ".github/**",
            "tests/test_api_security.py",
            "tests/test_api_tenant_isolation.py",
            "tests/test_vector*",
            "tests/test_upsert_vectordb_ingestion.py",
            "SECURITY.md",
            "README.md",
        ),
        (
            "python -m unittest tests.test_api_security tests.test_api_tenant_isolation tests.test_vector_upsert tests.test_upsert_vectordb_ingestion -v",
            "python -m unittest tests.test_deployment_defaults tests.test_readiness -v",
        ),
        "Security and tenant isolation changes are a separate review surface from parser quality.",
    ),
    CommitSliceRule(
        "operator_docs_and_local_ui",
        "Operator Docs And Local UI",
        60,
        (
            "frontend/**",
            "docs/operator_*",
            "docs/ui_ux_*",
            "tests/test_streamlit*",
            "tests/test_frontend*",
            "tests/test_ui_ux*",
            "tests/test_operator*",
        ),
        (
            "python -m unittest tests.test_streamlit_operator_mode tests.test_frontend_upload_types tests.test_ui_ux_release_scope_ko tests.test_operator_quickstart_ko -v",
        ),
        "Local Streamlit and operator guidance should not be mixed with protected API/MCP gate commits.",
    ),
    CommitSliceRule(
        "packaging_public_release",
        "Packaging And Public Release",
        70,
        (
            "scripts/report_metadata.py",
            "scripts/check_installed_console_scripts.py",
            "pyproject.toml",
            "MANIFEST.in",
            "CONTRIBUTING.md",
            "LICENSE",
            ".gitignore",
            ".gitattributes",
            "scripts/audit_public_release_readiness.py",
            "scripts/plan_public_release_cleanup.py",
            "scripts/build_github_publish_*",
            "scripts/check_github_publish_*",
            "tests/test_package_manifest.py",
            "tests/test_packaging_entrypoints.py",
            "tests/test_check_installed_console_scripts.py",
            "tests/test_audit_public_release_readiness.py",
            "tests/test_plan_public_release_cleanup.py",
            "tests/test_build_github_publish_*",
            "tests/test_check_github_publish_*",
            "docs/public*",
            "docs/open_source*",
        ),
        (
            "python -m unittest tests.test_package_manifest tests.test_packaging_entrypoints tests.test_audit_public_release_readiness tests.test_plan_public_release_cleanup -v",
        ),
        "Public release mechanics should stay separate from private pilot runtime evidence.",
    ),
    CommitSliceRule(
        "hermes_harness_parallel_track",
        "Hermes Harness Parallel Track",
        80,
        (
            "app/agents/hermes_agent.py",
            "scripts/run_hermes.py",
            "scripts/run_public_batch_pipeline.py",
            "docs/harness_engineering_plan_ko.md",
            "docs/hermes_engineering_plan_ko.md",
            "docs/benchmark_absorption_ko.md",
            "tests/test_hermes_agent.py",
            "tests/test_harness_docs.py",
            "tests/test_public_batch_pipeline_docs.py",
            "tests/test_run_public_batch_pipeline.py",
        ),
        (
            "python -m unittest tests.test_hermes_agent tests.test_harness_docs tests.test_public_batch_pipeline_docs tests.test_run_public_batch_pipeline -v",
        ),
        "Hermes and harness work should remain a parallel track until pilot readiness gates are stable.",
    ),
    CommitSliceRule(
        "generated_runtime_reports",
        "Generated Runtime Data And Reports",
        90,
        (
            "data/**",
            "reports/**",
            "dist/**",
            "*.egg-info/**",
        ),
        (
            "Do not commit generated runtime data or *_current.json reports unless explicitly approved for handoff evidence.",
        ),
        "Generated data and current reports usually belong in regenerated evidence, not source commits.",
    ),
)


def build_worktree_commit_plan(
    *,
    repo_root: Path | str | None = None,
    status_lines: Sequence[str] | None = None,
    out_json: Path | None = None,
    out_md: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or PROJECT_ROOT)
    raw_status_lines = list(status_lines) if status_lines is not None else _git_status_short(root)
    changes = [_parse_status_line(line) for line in raw_status_lines if line.strip()]
    changes = [change for change in changes if change]
    slice_rows = _slice_rows(changes)
    report = {
        "report_type": "worktree_commit_plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": current_repo_commit(root),
        "repo_root": str(root),
        "total_change_count": len(changes),
        "tracked_change_count": sum(1 for change in changes if not change["untracked"]),
        "untracked_change_count": sum(1 for change in changes if change["untracked"]),
        "slice_count": len(slice_rows),
        "slices": slice_rows,
        "recommended_sequence": [
            {
                "slice_id": row["slice_id"],
                "title": row["title"],
                "dependency_order": row["dependency_order"],
                "path_count": row["path_count"],
            }
            for row in slice_rows
            if row["path_count"] > 0
        ],
        "safety_notes": [
            "Review each slice against actual diffs before committing; this report classifies paths, not semantics.",
            "Do not commit generated data, reports, or runtime artifacts unless they are deliberate release evidence.",
            "Regenerate final handoff evidence from a clean committed tree.",
        ],
        "api_call_count": 0,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(_to_markdown(report), encoding="utf-8")
    return report


def _git_status_short(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status --short failed")
    return result.stdout.splitlines()


def _parse_status_line(line: str) -> dict[str, Any] | None:
    if len(line) < 4:
        return None
    status = line[:2]
    raw_path = line[3:].strip()
    if " -> " in raw_path:
        raw_path = raw_path.split(" -> ", 1)[1].strip()
    path = raw_path.replace("\\", "/")
    return {
        "status": status,
        "path": path,
        "untracked": status == "??",
    }


def _slice_rows(changes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    assigned: dict[str, list[dict[str, Any]]] = {rule.slice_id: [] for rule in COMMIT_SLICE_RULES}
    assigned["other_changes"] = []
    for change in changes:
        rule = _matching_rule(change["path"])
        assigned[rule.slice_id if rule else "other_changes"].append(change)
    rows: list[dict[str, Any]] = []
    for rule in COMMIT_SLICE_RULES:
        rows.append(_slice_row(rule, assigned[rule.slice_id]))
    rows.append(
        {
            "slice_id": "other_changes",
            "title": "Other Changes",
            "dependency_order": 100,
            "path_count": len(assigned["other_changes"]),
            "tracked_change_count": sum(1 for change in assigned["other_changes"] if not change["untracked"]),
            "untracked_change_count": sum(1 for change in assigned["other_changes"] if change["untracked"]),
            "representative_paths": [change["path"] for change in assigned["other_changes"][:25]],
            "paths": [change["path"] for change in assigned["other_changes"]],
            "verification_commands": ["Inspect manually and move into a named slice before committing."],
            "rationale": "Paths that do not match the known pilot-readiness slices.",
        }
    )
    return sorted(rows, key=lambda row: (row["dependency_order"], row["slice_id"]))


def _matching_rule(path: str) -> CommitSliceRule | None:
    for rule in COMMIT_SLICE_RULES:
        if any(_match_path(path, pattern) for pattern in rule.patterns):
            return rule
    return None


def _match_path(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    return fnmatch.fnmatch(path, normalized)


def _slice_row(rule: CommitSliceRule, changes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "slice_id": rule.slice_id,
        "title": rule.title,
        "dependency_order": rule.dependency_order,
        "path_count": len(changes),
        "tracked_change_count": sum(1 for change in changes if not change["untracked"]),
        "untracked_change_count": sum(1 for change in changes if change["untracked"]),
        "representative_paths": [change["path"] for change in changes[:25]],
        "paths": [change["path"] for change in changes],
        "verification_commands": list(rule.verification_commands),
        "rationale": rule.rationale,
    }


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Worktree Commit Plan",
        "",
        f"- Report type: `{report.get('report_type')}`",
        f"- Repo commit: `{report.get('repo_commit')}`",
        f"- Total changes: `{report.get('total_change_count')}`",
        f"- Tracked / untracked: `{report.get('tracked_change_count')}` / `{report.get('untracked_change_count')}`",
        "",
        "## Recommended Sequence",
        "",
        "| Order | Slice | Paths |",
        "| --- | --- | ---: |",
    ]
    for row in report.get("recommended_sequence") or []:
        lines.append(f"| {row['dependency_order']} | {row['title']} | {row['path_count']} |")
    lines.extend(["", "## Slices", ""])
    for row in report.get("slices") or []:
        if row.get("path_count", 0) <= 0:
            continue
        lines.extend(
            [
                f"### {row['title']}",
                "",
                f"- Slice ID: `{row['slice_id']}`",
                f"- Paths: `{row['path_count']}`",
                f"- Tracked / untracked: `{row['tracked_change_count']}` / `{row['untracked_change_count']}`",
                f"- Rationale: {row['rationale']}",
                "- Verification:",
            ]
        )
        for command in row.get("verification_commands") or []:
            lines.append(f"  - `{command}`")
        lines.append("- Representative paths:")
        for path in row.get("representative_paths") or []:
            lines.append(f"  - `{path}`")
        lines.append("")
    lines.extend(["## Safety Notes", ""])
    for note in report.get("safety_notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only commit slicing plan from git status.")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    report = build_worktree_commit_plan(
        repo_root=Path(args.repo_root) if args.repo_root else PROJECT_ROOT,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
