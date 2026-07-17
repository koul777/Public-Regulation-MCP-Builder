# 공유 운영 배포 Runbook

이 프로젝트의 공유 운영 배포는 단순히 FastAPI 컨테이너를 띄우는 것이 아니라, 인증·테넌트 격리·감사·승인된 RAG 인덱스·MCP 서버 연결을 하나의 런타임 증거로 묶는 작업이다.

## 현재 30%로 보였던 이유

기존 점수는 코드 기능 점수가 아니라 공유 환경의 배포 차단 조건을 포함한 진단 결과였다. 로컬 셸에는 보통 `APP_ENV=local`, 빈 API token, `TENANT_STORAGE_ISOLATION=false`가 남아 있고, 실제 AKS runtime에는 승인된 레코드가 있어도 stale vector와 승인 SHA drift가 남아 있을 수 있다. 이 중 하나라도 해결되지 않으면 공유 배포 gate는 열리지 않는다.

이제 `check_private_release_readiness.py --require-shared-deployment`는 다음을 함께 출력한다.

- 통과 검사 수/전체 검사 수와 진단용 `readiness_score.percent`
- 배포를 승인하는 값이 아니라는 명시적 해석
- 실패한 검사별 category, severity, remediation

진단 점수가 80%여도 blocker가 하나 남으면 배포는 `passed=false`다. 숫자와 gate 판정은 구분한다.

## 공유 API 시작

```powershell
Copy-Item .env.shared.example .env.shared
# .env.shared의 API_AUTH_TOKEN을 secret manager 주입 값으로 교체
docker compose -f docker-compose.yml -f docker-compose.shared.yml up --build api
python scripts/check_private_release_readiness.py --require-shared-deployment --out-json reports/shared_readiness.json
```

실제 컨테이너와 같은 환경/마운트에서 다음을 실행해야 한다.

```powershell
docker compose -f docker-compose.yml -f docker-compose.shared.yml exec api `
  python scripts/check_private_release_readiness.py --require-shared-deployment
```

`API_AUTH_TOKEN`은 소스나 `.env.shared.example`에 저장하지 않는다. 모든 요청에는 인증과 `X-Tenant-Id`가 필요하고, `API_AUDIT_ENABLED=true`, `TENANT_STORAGE_ISOLATION=true`를 유지한다. Streamlit은 공유 API와 분리된 로컬 운영자 UI다.

## 규정 생명주기와 RAG

문서는 덮어쓰지 않고 같은 `profile_id`와 `regulation_id` 아래 새 `regulation_version`으로 등록한다. `revision_date`, `effective_from`, `effective_to`, `repealed_at`, `regulation_status`, `supersedes_document_id`를 문서·승인 청크·벡터 metadata에 함께 보존한다.

- 일반 RAG 검색: 승인되고 현재 효력이 있는 최신 version 1개를 선택한다.
- 과거 기준일 검색: `as_of_date`를 사용하고 search와 fetch에 같은 날짜를 전달한다.
- 전체 이력: `get_regulation_history`로 version, 개정일, 효력기간, 폐지 여부, 선행 문서를 조회한다.
- 폐지/대체 문서: 일반 RAG와 MCP 근거에서 제외하며, 이력 응답에는 남긴다.
- 승인되지 않은 청크·stale vector·검토 flag 미해결 청크: index와 MCP에서 제외한다.

검색 trace에는 `lifecycle_selection`이 기록되어 최신본 선택 기준, 기준일, 완전한 생명주기 metadata 수, 레거시 호환 레코드 수를 확인할 수 있다. 레거시 레코드는 임시 호환 대상이며 공식 handoff 전에는 temporal metadata audit을 통과해야 한다.

RAG 계층은 `approved repository -> approval/tenant/ACL visibility -> lifecycle selection -> BM25/embedding retrieval -> evidence result/trace` 순서다. MCP 서버는 이 RAG 계층을 호출하고 승인된 결과만 도구로 노출한다.

## MCP와 MCP 서버의 구분

- MCP: 클라이언트와 서버가 도구를 호출하는 프로토콜/계약
- MCP 서버: 이 저장소의 `app/mcp_server/regulation_server.py` 실행 프로세스. `stdio` 또는 `streamable-http` transport로 `search`, `fetch`, `get_regulation_history` 등을 노출한다.
- MCP 클라이언트: Claude, ChatGPT 또는 기관 AI. 클라이언트는 raw 파일이나 repository JSONL을 읽지 않고 MCP 서버 도구만 호출한다.

따라서 배포 대상은 “MCP 자체”가 아니라 승인 runtime에 연결된 MCP 서버이며, 클라이언트는 해당 서버의 transport·URL·인증·tenant/profile scope를 등록한다.

## 배포 전 필수 순서

1. 실제 기관 문서를 전처리하고 parser uncertainty를 검토한다.
2. 사람 승인과 approval journal/worklist/review-batch SHA를 확정한다.
3. 승인 청크를 index/reindex하고 runtime manifest를 생성한다.
4. `audit_temporal_metadata_coverage.py`로 version/effective/repeal metadata를 확인한다.
5. `audit_mcp_index_visibility.py --forbid-smoke-docs --require-indexed --fail-on-issue`를 실행한다.
6. `check_mcp_connection_readiness.py`와 MCP transport smoke에서 history tool 호출까지 확인한다.
7. handoff/evidence verification이 통과한 뒤에만 MCP 클라이언트를 연결한다.

기존 AKS runtime에서 승인 SHA drift가 발견되면 journal/vector JSONL을 직접 수정하지 않는다. 승인 workflow 재생성 → 승인 벡터 재색인 → evidence 재검증 순서로 복구한다.
