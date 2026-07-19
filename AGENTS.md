# Repository Guidelines

This public source-only repository contains the Korean public-institution regulation preprocessing and approved local RAG/MCP source code.

## Layout

- `app/`: FastAPI routes, parsers, processors, storage, ingestion, retrieval, and MCP tools.
- `frontend/`: local-only Streamlit operator UI.
- `scripts/`: operational CLIs and release evidence tools.
- `tests/`: deterministic `unittest` coverage.
- `docs/`: public architecture, security, contribution, and release documentation.

## Development

- Use Python 3.11+ and 4-space indentation.
- Keep parsing, processing, storage, ingestion, API, and security concerns separated.
- Use type hints for public functions and Pydantic models for structured API data.
- Run commands from the repository root.

```powershell
python -m unittest discover -s tests -v
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

## Security

- Preprocessing is not itself a security control.
- Only approved chunks may be indexed into the local Vector/RAG store.
- Keep raw uploads, runtime exports, secrets, local paths, and institution-specific identifiers out of public source and MCP responses.
- Use tenant isolation, approval journals, audit logs, and explicit review flags for uncertain parsing.
- The public repository must not be created by exposing the history of a private repository. Follow `docs/public_repository_history_policy_ko.md` and use a clean-history orphan snapshot for a separate public repository.

## Contributions

Add focused tests for parser behavior, security gates, tenant isolation, vector ingestion, approval-based indexing, and public-release hygiene. See `docs/harness_engineering_plan_ko.md` and `docs/hermes_engineering_plan_ko.md` for public release-harness procedures. Include the commands and results in pull requests. Do not commit generated runtime data or institution documents.

Changes to parsing/preprocessing logic, regression baselines, or their GitHub guard must also follow `docs/preprocessing_change_governance_ko.md`: complete the protected PR template fields, change a focused regression test, and obtain Code Owner review plus the `preprocessing-reviewed` label.
