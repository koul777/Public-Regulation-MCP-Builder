# MCP Tool Contract

이 문서는 PR MCP Builder가 생성한 MCP 서버가 클라이언트에 노출하는 도구와 응답 원칙을 정리한다.

## Tool Profiles

| Profile | Intended client | Exposed tools |
| --- | --- | --- |
| `full` | Claude Desktop, Claude Code, 내부 운영자용 생성형 AI | `search`, `fetch`, `list_regulations`, `get_regulation_toc`, `get_regulation_article`, `get_regulation_references`, `list_regulation_reference_cycles`, `list_documents`, `get_article`, `get_table`, `compare_versions`, `get_citation`, `get_index_status` |
| `chatgpt-data` | ChatGPT Desktop, Codex, Claude의 원격 HTTPS MCP | `list_regulations`, `get_regulation_toc`, `get_regulation_article`, `get_regulation_references`, `list_regulation_reference_cycles`, `search`, `fetch` |

서버 CLI 기본값은 `full`이다. 생성 번들은 ChatGPT Desktop·Codex·외부 모델 연결에 `--tool-profile chatgpt-data`를 명시해 내부 진단·식별자 노출을 줄인다.

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

## ChatGPT data-source 호환 계약

`chatgpt-data` 프로필은 OpenAI 데이터 소스 호환용 `search`/`fetch` 계약을 유지하면서, 규정 목록과 계층을 직접 조회하는 읽기 전용 도구를 함께 노출합니다.

- `list_regulations(query?, page?, page_size?)`는 `approved` 규정만 `regulation_title` 기준으로 중복 제거해 반환합니다. 응답에는 규정명·규정 구분·규정 번호·개정일·시행일·상태, `total_count`, 다음 페이지 정보가 포함됩니다.
- `get_regulation_toc(regulation_unit_id)`는 해당 규정의 장·절·조·별표·서식 계층을 반환합니다.
- `get_regulation_article(regulation_unit_id, article_no)`는 `제16조`와 같은 정확 조문을 승인 원문 근거로 반환합니다.
- `get_regulation_references(regulation_unit_id, direction?, status?, page?, page_size?)`는 다른 규정·조문으로 향하는 참조와 해당 규정으로 들어오는 참조를 반환합니다.
- `list_regulation_reference_cycles(regulation_unit_id?, page?, page_size?)`는 승인된 현행 규정 그래프에서 확인된 순환참조 묶음을 반환합니다.
- `search(query)`는 입력을 검색어 하나로 제한하며 결과 항목은 `id`, `title`, `url`만 반환합니다.
- `fetch(id)`는 입력을 검색 결과 ID 하나로 제한하며 `id`, `title`, `text`, `url`, 문자열 metadata를 반환합니다.
- `url`은 사용자가 열 수 있는 절대 HTTP(S) 원문 주소이거나 빈 문자열입니다.
- 로컬 전용 `govreg://` URI, tenant/profile/approval 내부 식별자와 운영 증적 경로는 공개 응답에 넣지 않습니다.
- 일곱 도구는 모두 `readOnlyHint: true`입니다. 로컬 ChatGPT Desktop·Codex는 stdio, 원격 앱은 Streamable HTTP `/mcp`로 같은 축약 계약을 노출합니다.

연결 구성은 Settings, 공식 CLI 또는 설정 파일에 직접 적용합니다. 연결 설정·로컬 경로·토큰·API 키·tunnel ID를 대화에 넣지 않습니다. 연결 후의 일반 `search`·`fetch` 질의에는 이러한 비밀값이 없어야 합니다.

`full` 프로필은 운영자용 필터와 진단 입력을 계속 제공하므로 이 축약 계약의 적용 대상이 아닙니다.

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

## Catalog and Full Profile Tools

- `list_regulations`: 통합 규정집 내부의 승인된 고유 규정 목록 확인(`chatgpt-data`, `full`)
- `get_regulation_toc`: 규정 단위 ID 기준 장·절·조·별표 목차 조회(`chatgpt-data`, `full`)
- `get_regulation_article`: 규정 단위 ID와 조문 번호로 정확 조문 즉시 조회(`chatgpt-data`, `full`)
- `get_regulation_references`: 규정·조문 간 outgoing/incoming 참조와 해결 상태 조회(`chatgpt-data`, `full`)
- `list_regulation_reference_cycles`: 규정 그래프의 순환참조 묶음 조회(`chatgpt-data`, `full`)
- `list_documents`: MCP-visible 승인 문서 목록 확인
- `get_article`: 문서 ID와 조문 번호 기준 근거 조회
- `get_table`: 표/별표 chunk 조회
- `compare_versions`: 두 문서 버전 간 조문 비교
- `get_citation`: 검색 결과 ID의 citation metadata 조회
- `get_index_status`: 승인 vector/index 상태 확인

## Response Rules

- 미승인·rejected·security-blocked chunk는 반환하지 않는다.
- superseded/repealed 개정 이력은 승인 증거와 효력 구간이 모두 확인되고 명시적 과거 기준일 조회에 해당할 때만 반환한다.
- 로컬 원본 파일 경로와 내부 artifact 경로는 응답하지 않는다.
- 모든 도구는 read-only annotation을 가진다.
- `confidential` 등급은 기본 `operator` role에서 요청할 수 없다.
- 외부 클라우드 AI로 연결할 때는 공개 가능 데이터 또는 별도 승인된 망연계 환경만 사용한다.
