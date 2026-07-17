# MCP Tool Contract

이 문서는 PR MCP Builder가 생성한 MCP 서버가 클라이언트에 노출하는 도구와 응답 원칙을 정리한다.

## Tool Profiles

| Profile | Intended client | Exposed tools |
| --- | --- | --- |
| `full` | Claude Desktop, Claude Code, Claude API, 내부망 생성형 AI | `search`, `fetch`, `list_regulations`, `get_regulation_toc`, `get_regulation_article`, `list_documents`, `get_article`, `get_table`, `compare_versions`, `get_citation`, `get_index_status` |
| `chatgpt-data` | ChatGPT 데이터형 connector | `search`, `fetch` |

기본값은 `full`이다. ChatGPT connector에는 `--tool-profile chatgpt-data`를 권장한다.

```powershell
reg-rag-mcp-server `
  --data-dir data `
  --tenant-id default `
  --transport streamable-http `
  --host 0.0.0.0 `
  --port 8000 `
  --tool-profile chatgpt-data `
  --http-bearer-token-env MCP_AUTH_TOKEN `
  --auth-issuer-url https://mcp.example.go.kr
```

## Search

`search`는 승인된 로컬 규정을 기관 카탈로그, 규정 목차, 본문 순서로 좁혀 검색한다. 계층 인덱스가 없는 이전 번들은 기존 RAG 검색으로 자동 복귀한다. 클라이언트는 먼저 `search`를 호출하고, 결과의 `id`를 `fetch`에 넘겨 원문 근거를 가져온다.

주요 입력:

- `query`: 사용자 질문 또는 검색어
- `top_k`: 반환할 근거 수
- `security_levels`: 허용 보안 등급 필터
- `department_ids`: 부서 범위 축소 필터
- `document_id`: 특정 문서 제한

## Fetch

`fetch`는 `search`가 반환한 `id`의 승인된 본문과 citation metadata를 반환한다. 답변 생성 클라이언트는 `fetch.text`와 citation metadata를 근거로만 답해야 한다.

## Full Profile Tools

- `list_regulations`: 통합 규정집 내부의 개별 규정과 개정판 목록 확인
- `get_regulation_toc`: 규정 단위 ID 기준 장·절·조·별표 목차 조회
- `get_regulation_article`: 규정 단위 ID와 조문 번호로 정확 조문 즉시 조회
- `list_documents`: MCP-visible 승인 문서 목록 확인
- `get_article`: 문서 ID와 조문 번호 기준 근거 조회
- `get_table`: 표/별표 chunk 조회
- `compare_versions`: 두 문서 버전 간 조문 비교
- `get_citation`: 검색 결과 ID의 citation metadata 조회
- `get_index_status`: 승인 vector/index 상태 확인

## Response Rules

- 미승인 chunk, rejected/superseded/security-blocked chunk는 반환하지 않는다.
- 로컬 원본 파일 경로와 내부 artifact 경로는 응답하지 않는다.
- 모든 도구는 read-only annotation을 가진다.
- `confidential` 등급은 기본 `operator` role에서 요청할 수 없다.
- 외부 클라우드 AI로 연결할 때는 공개 가능 데이터 또는 별도 승인된 망연계 환경만 사용한다.
