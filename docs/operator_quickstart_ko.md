# Public Operator Quickstart

이 문서는 source-only 공개 저장소에서 합성 샘플과 로컬 개발 환경으로 파이프라인을 확인하는 공개용 quickstart다. 기관 원문, 운영 runtime, 내부 handoff 자료는 이 문서의 입력으로 사용하지 않는다.

## Scope

- Use synthetic or explicitly redistributable samples only.
- Streamlit is a local-only operator console, not a protected shared-deployment UI.
- Preprocessing output is a review preview. Official RAG/MCP indexing starts only after human review and approval.

## Local Start

```powershell
$env:APP_ENV="development"
$env:API_AUTH_REQUIRED="true"
$env:API_AUTH_TOKEN="replace-with-a-local-development-token"
$env:DATA_DIR=".\data"
uvicorn app.main:app --reload
streamlit run frontend\streamlit_app.py --server.address 127.0.0.1
```

Example authenticated request:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/documents/process `
  -H "Authorization: Bearer $env:API_AUTH_TOKEN" `
  -H "X-Tenant-Id: tenant-demo" `
  -H "X-Actor: local-operator" `
  -H "Content-Type: application/json" `
  -d '{"document_id":"synthetic-document-001","file_path":"data/uploads/synthetic-document-001.md"}'
```

The response should expose a `document_id`, `job_id`, `status=completed`, and `quality.passed=true` only for the local synthetic fixture path.

## Public Validation

```powershell
python -m unittest discover -s tests -q
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
python scripts\run_fresh_clone_rehearsal.py --mode public --dry-run --fail-on-issue
python scripts\run_release_harness.py --mode public --keep-going
python scripts\run_public_release_gate.py --include-untracked --execute-harness --fail-on-issue
```

For release evidence, use the public audit, cleanup plan, release gate, approval evidence, review-batch evidence, and MCP release evidence tools. Keep generated reports outside the tracked source tree.

## Official Chain

The official path is:

```text
source file -> preprocessing -> quality flags -> human review -> approval journal
-> approved local regulation DB/vector index -> RAG/MCP tools
```

Unreviewed results must remain `UNREVIEWED_PREVIEW` or `UNREVIEWED_POC_REVIEW`. They must not be treated as official approved vectors. Reindex approved chunks only after review flags are acknowledged, review-batch decisions are validated, and release evidence is regenerated.

## Excluded From Public Use

- institution documents and downloaded HWP/PDF originals
- runtime exports, vector databases, approval journals, and internal evidence
- real tokens, local absolute paths, and institution-derived identifiers
- claims of production deployment, SSO, ChatGPT/Claude endpoint availability, or product readiness
