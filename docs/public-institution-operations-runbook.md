# Public Institution Operations Runbook

This public runbook covers source-only local operation and evidence contracts. It intentionally excludes institution runtime paths, original uploads, private deployment evidence, and internal handoff procedures.

## Safe Local Operation

1. Start FastAPI and the local-only Streamlit operator console.
2. Use synthetic or redistributable samples under a disposable local data directory.
3. Require authentication and `X-Tenant-Id` for API requests.
4. Inspect quality flags and review candidates before any indexing action.
5. Keep unapproved output marked as preview and outside the approved index.

## Approval and Indexing Contract

The official path is source preprocessing, quality validation, human review, approval decision, append-only approval journal, approved local regulation DB/vector index, and citation-grounded RAG/MCP retrieval.

Required evidence fields include:

- `approval_journal_coverage.eligible_record_count`
- `approval_journal_coverage.matched_record_count`
- `approval_journal_coverage.missing_record_count`
- `mcp_index_visibility_approval_journal_coverage_missing_record_count`
- source citation and tenant scope metadata

The release must fail closed when `approval-journal-vector-evidence-missing` is present or when approval coverage is incomplete.

## Public Validation Commands

```powershell
python -m unittest discover -s tests -q
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
python scripts\run_fresh_clone_rehearsal.py --mode public --dry-run --fail-on-issue
python scripts\run_public_release_gate.py --include-untracked --execute-harness --fail-on-issue
```

For authority-backed evidence, the public release process may use an artifact such as `--authoritative-artifact mcp_connection_readiness=reports/mcp_connection_readiness_current.json`, but generated reports remain untracked and must not contain local absolute paths or institution identifiers.

## Review Handoff

Review handoff must list unresolved parser uncertainty, table ambiguity, temporal ambiguity, approval coverage, and citation gaps. Do not infer dates silently and do not present a review preview as an approved vector record.
