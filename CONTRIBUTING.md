# Contributing

## Development Setup

Use Python 3.11 or newer.

```powershell
pip install -e .
python -m unittest discover -v
```

For MCP-specific changes, also run:

```powershell
reg-rag-mcp-smoke --fail-on-issue
reg-rag-mcp-config --client-profile bundle --server-name sample-institution-regulations --tenant-id default --out-dir reports/mcp_connection_bundle --zip-out reports/mcp_connection_bundle.zip
reg-rag-mcp-doctor --client-profile chatgpt --transport streamable-http --public-url https://example.invalid/mcp --skip-data-check
reg-rag-mcp-doctor --client-profile chatgpt --connection-mode openai-tunnel --transport stdio --skip-data-check
```

For public/source-only workflow changes, run `reg-rag-private-release-smoke --synthetic-sample`.

## Contribution Rules

- Keep changes scoped to preprocessing, approval, local regulation DB, MCP tools, or documented operator workflows.
- Add focused tests for parser changes, approval/indexing gates, tenant isolation, MCP visibility, audit evidence, and security filtering.
- Do not commit private documents, uploaded files, generated runtime data, vector stores, secrets, local paths, or institution-specific reports.
- Prefer synthetic fixtures unless a public sample is redistributable and documented.
- Keep MCP responses citation-backed and approved-data-only.

## Before Pull Request

Run:

```powershell
python -m unittest discover -v
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
reg-rag-audit-public-release --json
reg-rag-audit-public-release --json --include-untracked
reg-rag-plan-public-release-cleanup --include-untracked --out-md reports/public_release_cleanup_plan.md
git diff --check
```

PRs should include a summary, test commands, affected workflows, and any security or public-release implications.
