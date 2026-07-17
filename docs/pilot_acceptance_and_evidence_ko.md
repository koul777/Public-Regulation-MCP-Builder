# Public Pilot Acceptance and Evidence

이 문서는 source-only 공개 저장소에서 합성 또는 재배포 가능한 파일로 수행하는 pilot acceptance 기준을 정의한다.

## Evidence Matrix

| 항목 | 공개 evidence |
| --- | --- |
| preprocessing readiness | `public_batch_readiness_*.json/.md` |
| quality evidence | `public_batch_quality_*.json/.md` |
| quality threshold | `average_quality_score >= 98` |
| failure threshold | `failed_count = 0` |
| OCR handling | `ocr_required_count = 0` 또는 명시적 review flag |
| API validation | `api_call_count=0` for deterministic local checks |
| review fields | reviewer, reviewed-at, decision, citation, and approval metadata |
| tenant boundary | `X-Tenant-Id` and explicit tenant scope |
| deployment topology | 단일 FastAPI container 또는 local process로 재현 가능한 공개 예시 |

## Official Chain

Preprocessing output is preview/schema validation only. Official indexing starts only after human review, approval, append-only approval journal, and approved vector synchronization.

```text
source -> preprocess -> review flags -> reviewer decision -> approval journal
-> approved local DB/vector index -> RAG/MCP answer with citation
```

## Public Boundary

- Do not include original institution files, runtime exports, vector data, unpublished visibility evidence, or internal handoff reports.
- Use synthetic identifiers and public-safe paths in examples.
- A passing source-only pilot does not establish product-public readiness.
- Claims about provider API connectors, SSO, or external HTTPS deployment require separate environment evidence.
