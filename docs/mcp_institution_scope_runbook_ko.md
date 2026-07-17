# 기관별 MCP 운영 범위 Runbook

## 기본 원칙

- 기관 profile은 regulation retrieval, revision history, indexing, MCP bundle의 공통 scope이다.
- MCP는 client protocol/tool contract이고 MCP server는 실제 stdio 또는 streamable HTTP runtime이다.
- 현재 RAG는 승인된 최신 version만 검색한다. 과거 version은 history와 명시적 version comparison에서만 조회한다.
- tenant-wide bundle export는 금지한다. `selected_institution` 또는 `document` scope를 명시한다.

## 기관 등록과 규정 입력

1. 기관 profile을 생성한다.
2. 기관을 선택한 상태에서 규정 원문을 업로드한다.
3. 개정본은 새 document/version으로 입력하고 기존 문서를 덮어쓰지 않는다.
4. 개정일, 효력일, 폐지일, supersedes 관계를 확인한다.
5. parser review flag와 source metadata를 확인한다.
6. 사람이 승인한 chunk만 vector index와 MCP에 노출한다.

## MCP bundle scope

- 기관 전체: `scope=selected_institution`, `profile_id` 필수
- 특정 규정: `scope=document`, `profile_id`와 `document_id` 필수
- `document_id`와 `profile_id`가 모두 없는 export는 실패해야 한다.

## MCP server 연결

- 로컬 프로세스 연결: stdio transport
- 내부 HTTP 연결: streamable HTTP transport
- profile-bound server는 다른 `profile_id` 요청을 거부한다.
- unbound server는 tenant에 여러 기관이 있으면 client가 `profile_id`를 명시해야 한다.

## Release gate

Disposable synthetic integration evidence:

```powershell
python scripts\run_institution_release_gate.py `
  --data-dir data\runtime-institution-mcp `
  --tenant-id <tenant-id> `
  --profile-id <profile-id> `
  --allow-synthetic-runtime `
  --run-dir reports\overnight_runs\<run-id>
```

Optional same-tenant profile isolation evidence can be added with two distinct values:

```powershell
  --scope-profile-id <profile-a> `
  --scope-profile-id <profile-b>
```

The isolation smoke uses a separate child runtime and is not mixed into the prepared production runtime.

Prepared production runtime:

```powershell
python scripts\run_institution_release_gate.py `
  --data-dir <prepared-runtime> `
  --tenant-id <tenant-id> `
  --profile-id <profile-id> `
  --skip-local-smoke `
  --run-dir reports\overnight_runs\<run-id>
```

Production mode must reject synthetic smoke documents. A synthetic PASS is not a production approval.

Historical retrieval rule: when `search` is called with `as_of_date`, pass the same date to `fetch`. The returned fetch metadata contains the effective historical boundary.

Official publish rule: a source document must carry a concrete `profile_id`, or the operator must provide an explicit `--profile-id`. The publisher no longer falls back to a generic institution profile.

## 1차 범위에서 제외

기관 간 제도 비교 UI는 현재 범위에서 제외한다. 먼저 기관별 최신 retrieval, 개정 이력, approval gate, MCP scope를 안정화한 뒤 비교 기능을 별도 설계한다.
