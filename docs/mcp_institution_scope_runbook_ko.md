# 기관별 MCP 운영 범위 Runbook

## 기본 원칙

- tenant와 기관 프로필은 문서, 개정 이력, 색인, MCP 번들의 공통 범위다.
- MCP는 client protocol/tool contract이고 MCP server는 실제 stdio 또는 streamable HTTP runtime이다.
- 기본 검색은 승인된 최신 개정본을 조회한다. 다만 search에 as_of_date를 지정하면 해당 날짜에 유효했던 개정본을 조회하며, get_regulation_history와 명시적 개정 비교에서도 과거 이력을 확인할 수 있다.
- tenant 전체를 묶어 내보내지 않는다. selected_institution 또는 document 범위를 명시한다.

## 기관 등록과 규정 입력

1. 기관 프로필(profile)을 생성한다.
2. 기관을 선택한 상태에서 규정 원문을 업로드한다.
3. 개정본은 새 document/version으로 입력하고 기존 문서를 덮어쓰지 않는다.
4. 개정일, 효력일, 폐지일, supersedes 관계를 확인한다.
5. parser review flag와 source metadata를 확인한다.
6. 사람이 승인하고 승인 기록에 남긴 조각만 색인과 MCP 조회에 노출한다.

## MCP bundle scope

- 기관 전체: `scope=selected_institution`, `profile_id` 필수
- 특정 규정: `scope=document`, `profile_id`와 `document_id` 필수
- `document_id`와 `profile_id`가 모두 없는 export는 실패해야 한다.

## MCP server 연결

- 로컬 프로세스 연결: stdio transport
- 내부 HTTP 연결: streamable HTTP transport
- profile-bound server는 다른 `profile_id` 요청을 거부한다.
- unbound server는 tenant에 여러 기관이 있으면 client가 `profile_id`를 명시해야 한다.

## 배포 전 점검

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

격리 smoke는 별도 하위 runtime에서 실행하며 준비된 운영 runtime과 섞지 않는다.

운영 runtime 준비:

```powershell
python scripts\run_institution_release_gate.py `
  --data-dir <prepared-runtime> `
  --tenant-id <tenant-id> `
  --profile-id <profile-id> `
  --skip-local-smoke `
  --run-dir reports\overnight_runs\<run-id>
```

운영 모드에서는 합성 smoke 문서를 거부해야 한다. 합성 PASS는 운영 승인 증거가 아니다.

Historical retrieval rule: when `search` is called with `as_of_date`, pass the same date to `fetch`. The returned fetch metadata contains the effective historical boundary.

Official publish rule: a source document must carry a concrete `profile_id`, or the operator must provide an explicit `--profile-id`. The publisher no longer falls back to a generic institution profile.

## 승인 읽기와 범위 확인

tenant와 기관 프로필은 문서, 승인 기록, 색인, 검색, fetch, 표·별표 조회, 개정 이력과 MCP 번들에서 일치해야 한다. 승인되지 않은 조각은 어떤 조회에도 나타나면 안 된다. 표와 별표에도 조문과 동일하게 승인 여부와 tenant/profile 경계를 적용한다. 계층형 색인이나 캐시는 권한을 증명하지 않는다. 조회할 때 원본 기준 문서의 tenant/profile과 최신 승인 기록을 확인하고, 캐시 내용이 현재 문서와 일치하는지 확인한다.

기본 조회는 같은 규정 계열에서 가장 최신인 유효 승인본만 반환해야 한다. 이전 개정본은 기본 조회와 document_id를 지정한 검색·fetch·조문·표 조회에서 제외한다. 예외는 기준일을 지정한 과거 조회와 개정 이력·비교 조회다. search에 as_of_date를 주면 fetch에도 같은 날짜를 전달하고 반환된 효력 기준일을 확인한다.

## 개정본 활성화와 실패 복구

새 개정본 입력이나 승인만으로 현재 개정본이 바뀌지 않는다. 초기 색인이나 재색인이 실패하면 기존 현재 개정본을 유지한다. 모의 실행, 준비 계획, 번들 생성은 승인이나 개정본 활성화가 아니다. 승인 상태와 기록을 확인하고 실제 승인 색인이 성공한 뒤 새 개정본이 현재 조회되는지 확인한다.

## 연결 완료와 증거 한계

MCP 연결을 완료하려면 올바른 범위의 최신 승인 색인, 현재 개정본으로 만든 번들, 그리고 해당 번들 개정본에 묶인 운영자 확인이 모두 필요하다. 실제 AI 앱에서 등록·재시작 또는 새 대화·연결 진단·list_regulations/search/fetch를 수행하고 반환된 조문과 출처를 직접 확인한다. 설정·문서·범위가 바뀌면 과거 체크 표시는 더 이상 현재 확인이 아니다. 번들 생성, STDIO 점검, Qwen 앱 상태만으로 실제 답변과 출처를 검증했다고 기록하지 않는다.

합성 DOCX 생성은 문서 파싱부터 외부 AI 연결까지의 검증이 아니다. MCP STDIO 계약 검증은 외부 앱 연결이나 LLM 답변 품질을 증명하지 않는다. 브라우저에서 업로드와 검수를 확인해도 최종 승인·색인까지 확인한 것은 아니다. HWP/HWPX/PDF/OCR 전체 여정은 형식별 별도 검증이 필요하다. 합성 출처 조회 검사는 LLM 답변 품질 점수가 아니다.

## 1차 범위에서 제외

기관 간 제도 비교 UI는 현재 범위에서 제외한다. 먼저 기관별 최신 retrieval, 개정 이력, approval gate, MCP scope를 안정화한 뒤 비교 기능을 별도 설계한다.
