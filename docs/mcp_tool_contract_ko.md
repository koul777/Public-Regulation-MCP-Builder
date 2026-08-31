# MCP Tool Contract

이 문서는 PR MCP Builder가 생성한 MCP 서버가 클라이언트에 노출하는 도구와 응답 원칙을 정리한다.

## Tool Profiles

| Profile | Intended client | Exposed tools |
| --- | --- | --- |
| `full` | Claude Desktop, Claude Code, 내부 운영자용 생성형 AI | `search`, `lookup`, `fetch`, `list_regulations`, `get_regulation_toc`, `get_regulation_article`, `get_regulation_references`, `list_regulation_reference_cycles`, `get_regulation_history`, `list_documents`, `get_document`, `get_article`, `get_table`, `compare_versions`, `get_citation`, `get_index_status` |
| `chatgpt-data` | ChatGPT 웹 원격 MCP, Codex, Claude의 원격 HTTPS MCP | `list_regulations`, `get_regulation_toc`, `get_regulation_article`, `get_regulation_references`, `list_regulation_reference_cycles`, `search`, `fetch` |

서버 CLI 기본값은 `full`이다. 생성 번들은 ChatGPT 웹 원격 MCP·Codex·외부 모델 연결에 `--tool-profile chatgpt-data`를 명시해 내부 진단·식별자 노출을 줄인다.

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

### 입력 파일 구성과 결과 동등성

규정별 파일 여러 개와 여러 규정을 합친 통합 규정집 한 개는 MCP 생성 시 규정 단위로 자동
정규화됩니다. 두 입력 방식은 `list_regulations` → `get_regulation_toc` →
`get_regulation_article` 및 검색에서 같은 규정·목차·조문이라는 의미 결과를 내야 합니다.
출처 추적을 위한 원본 `document_id`와 보관 파일 수는 서로 다를 수 있습니다. 계층 색인은
번들 생성 또는 갱신 중 자동 생성되므로 별도의 수동 재생성 단계는 없습니다. 단, 원문에 규정
제목이나 조문 번호가 없어 규정 단위를 구분할 수 없으면 검수가 필요합니다.

즉, 사용자는 파일을 규정별로 나누어 올리거나 규정집 한 파일로 올린 뒤 같은 순서로 검수·승인하면
됩니다. 저장된 원본 파일의 모양은 달라도 MCP에서 보이는 **규정 목록, 목차, 조문 조회와 검색 결과의
논리적 구조**는 같아야 합니다.

PDF·HWP·HWPX·DOCX의 일반 본문과 조문 구조를 읽는 빠른 전처리는 Kordoc 설치 전에도
시작할 수 있습니다. 그러나 이 네 지원 형식의 문서를 공식 MCP로 만들려면 ④ 실행 전에
Kordoc table parser 품질 증거가 있어야 합니다. 증거 없이 처리한 문서는 Kordoc 설치 후 새
초안으로 다시 전처리하고, 사람 검수 권고를 확인한 운영자가 승인·색인을 완료해야 합니다.
미검수 승인은 기본 사유와 `approved_without_review` 감사 이벤트로 구분합니다. 생성기는
Kordoc 증거·승인 journal·현재 내용 hash 조건을 충족하지 못한 번들을 만들지 않습니다.

### MCP 생성 전 승인 완료 조건

`④ Qwen 규정 챗봇·AI 연결`의 MCP 탭에 있는 **MCP로 쓸 파일 묶음 만들기**는 선택한 규정의 현재 청크가 모두
`approved` 또는 명시적으로 거부된 상태일 때만 진행됩니다. 검토가 남거나 승인되지 않은 현재
청크가 있으면 번들 생성을 차단합니다. 사용자는 `③ 검수하고 승인`에서 사람 검수 권고를 읽고
승인 또는 거부를 결정한 다음 문서 색인을 완료해야 합니다. 미검수 승인도 별도 확인 체크 없이
가능하지만 기본 사유와 `approved_without_review` 감사 이벤트를 남깁니다. 이 조건은 일부 조문만 빠진 상태로 목록·목차·검색
도구가 만들어지는 것을 막기 위한 안전장치이며, 계층 색인의 수동 재생성을 요구하지 않습니다.
현재 청크가 모두 명시적으로 거부된 규정은 terminal exclusion으로 처리하여 MCP-visible 결과에서
제외합니다. 개별 파일과 합본 내부 규정에 같은 원칙을 적용하며, 번들 전체에는 승인·색인된 규정이
최소 한 건 있어야 합니다. 거부된 청크의 본문은 runtime bundle에 복사하지 않습니다.
단순히 저장 데이터의 상태 문자열이 `rejected`인 것만으로는 명시적 거부로 인정하지 않습니다.
생성기는 append-only 검토 저널에서 담당자·결정 시각·사유와 결정 후 청크 콘텐츠 해시가 현재
청크와 일치하는지 확인하며, 증거가 없거나 해시가 달라지면 규정 누락을 허용하지 않고 번들 생성을
중단합니다.

전달 runtime에는 모든 현재 청크의 `exported`, `omitted_rejected`, `omitted_superseded` 분류와
결정 ID·결정 시각·내용 해시만 담은 봉인된 최소 누락 결정 스냅샷을 포함합니다. 반려 청크의 본문,
검토 사유·담당자·원문 경로와 원본 review journal은 복사하지 않으며 MCP 도구 응답에도 노출하지
않습니다. `omitted_rejected`는 현재 해시와 결합된 반려 결정, `omitted_superseded`는 현재 해시와
대체 청크가 결합된 split/merge 결정이 고유한 최신 결정일 때만 기록됩니다.

전달 runtime의 `approvals.jsonl`도 원본 검토 저널이 아닙니다. 승인 권한을 재검증하는 데 필요한
`approval_id`, `tenant_id`, `document_id`, `approved_at`, `chunk_ids`,
`approved_content_hashes` 여섯 필드만 담으며, 담당자 신원·메모·반려/재정의 사유·검토 이벤트·
작업 PC 경로와 중첩 스냅샷은 포함하지 않습니다.

합본 목차에서만 사용하는 배치 번호가 개별 규정 파일에는 없고 규정명이 유일하다면, 두 입력의
공개 논리 정체성을 맞추기 위해 `regulation_no`는 빈 값일 수 있습니다. 같은 제목의 규정을 구분하는
데 번호가 필요한 경우에는 번호를 유지합니다. 원본에서 읽은 번호는 내부 출처 메타데이터에 보존됩니다.

- `list_regulations(query?, page?, page_size?)`는 승인되어 MCP에 보이는 규정의 **조문 체계 카탈로그**입니다. 규정명으로 중복을 제거해 반환하고, 응답에는 규정명·규정 구분·규정 번호·개정일·시행일·상태, `total_count`, 다음 페이지 정보가 포함됩니다. 반환된 `regulation_unit_id`를 다음 목차·정확 조문 조회에 사용합니다.
- `get_regulation_toc(regulation_unit_id)`는 해당 규정의 장·절·조·별표·서식 계층을 반환합니다.
- `get_regulation_article(regulation_unit_id, article_no)`는 `제16조`와 같은 정확 조문을 승인 원문 근거로 반환합니다.
- `get_regulation_references(regulation_unit_id, direction?, status?, page?, page_size?)`는 다른 규정·조문으로 향하는 참조와 해당 규정으로 들어오는 참조를 반환합니다.
- `list_regulation_reference_cycles(regulation_unit_id?, page?, page_size?)`는 승인된 현행 규정 그래프에서 확인된 순환참조 묶음을 반환합니다.
- `search(query)`는 입력을 검색어 하나로 제한하며 결과 항목은 `id`, `title`, `url`만 반환합니다.
- `fetch(id)`는 입력을 검색 결과 ID 하나로 제한하며 `id`, `title`, `text`, `url`, 문자열 metadata를 반환합니다.
- `url`은 사용자가 열 수 있는 절대 HTTP(S) 원문 주소이거나 빈 문자열입니다.
- 로컬 전용 `govreg://` URI, tenant/profile/approval 내부 식별자와 운영 증적 경로는 공개 응답에 넣지 않습니다.
- 일곱 도구는 모두 `readOnlyHint: true`입니다. 로컬 Codex는 stdio, ChatGPT 웹과 원격 앱은 Streamable HTTP `/mcp`로 같은 축약 계약을 노출합니다. ChatGPT는 로컬 MCP에 직접 연결하지 않습니다.

연결 구성은 Settings, 공식 CLI 또는 설정 파일에 직접 적용합니다. 연결 설정·로컬 경로·토큰·API 키·tunnel ID를 대화에 넣지 않습니다. 연결 후의 일반 `search`·`fetch` 질의에는 이러한 비밀값이 없어야 합니다.

`full` 프로필은 운영자용 필터와 진단 입력을 계속 제공하므로 이 축약 계약의 적용 대상이 아닙니다.

## Search

`search`는 승인된 로컬 규정을 기관 카탈로그, 규정 목차, 본문 순서로 좁혀 검색합니다. 따라서 단순히 비슷한 문장을 찾는 방식이 아니라, 먼저 어느 규정인지와 조문 위치를 확인한 뒤 본문 근거를 찾습니다. 클라이언트는 먼저 `search`를 호출하고, 결과의 `id`를 `fetch`에 넘겨 원문 근거를 가져옵니다.

공식 Builder가 생성·갱신한 번들과 `chatgpt-data` 커넥터는 계층 색인 또는 runtime manifest의 무결성 검증에 실패하면 오류로 중단합니다. 이때 평면(flat) RAG 검색으로 바꾸거나, `list_regulations`에서 빈 목록을 정상 결과처럼 반환하지 않습니다. 사용자가 계층 색인을 따로 재생성할 필요는 없으며, Builder의 MCP 생성·업데이트 과정이 자동으로 만듭니다. 계층 색인 표식 자체가 전혀 없는 이전 버전 또는 개발용 runtime에 한해서만 호환을 위한 평면 RAG fallback을 사용할 수 있습니다.

주요 입력:

- `query`: 사용자 질문 또는 검색어
- `top_k`: 반환할 근거 수
- `security_levels`: 허용 보안 등급 필터
- `department_ids`: 부서 범위 축소 필터
- `document_id`: 특정 문서 제한

## Fetch

`fetch`는 `search`가 반환한 `id`의 승인된 본문과 citation metadata를 반환한다. 답변 생성 클라이언트는 `fetch.text`와 citation metadata를 근거로만 답해야 한다.

## Catalog and Full Profile Tools

- `search`: 승인된 규정 본문을 질문·필터로 검색하고 후속 `fetch`용 결과 ID 반환
- `lookup`: 문서 ID·조문 번호를 알 때 직접 조회하고, 정확 일치가 없을 때 승인 RAG 검색으로 보완(`full` 전용)
- `fetch`: `search`가 반환한 결과 ID로 승인 원문과 인용 메타데이터 조회
- `list_regulations`: 개별 파일·통합 규정집에서 자동 분리한 승인 규정의 조문 체계 카탈로그 확인(`chatgpt-data`, `full`)
- `get_regulation_toc`: 규정 단위 ID 기준 장·절·조·별표 목차 조회(`chatgpt-data`, `full`)
- `get_regulation_article`: 규정 단위 ID와 조문 번호로 정확 조문 즉시 조회(`chatgpt-data`, `full`)
- `get_regulation_references`: 규정·조문 간 outgoing/incoming 참조와 해결 상태 조회(`chatgpt-data`, `full`)
- `list_regulation_reference_cycles`: 규정 그래프의 순환참조 묶음 조회(`chatgpt-data`, `full`)
- `get_regulation_history`: 규정 ID 기준 개정 이력·효력 기간·선행 버전 메타데이터 조회(`full` 전용)
- `list_documents`: MCP-visible 승인 문서 목록 확인
- `get_document`: 문서 ID 기준 승인된 전체 문서와 청크 원문 조회(`full` 전용)
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
