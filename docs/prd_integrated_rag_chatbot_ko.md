# PRD: 내부망 규정 DB 기반 MCP 서버 생성/실행기

기준일: 2026-07-08  
대상 제품: PR MCP Builder(공공기관 규정 MCP 빌더)
문서 상태: 기존 RAG-first 기획에서 MCP-first 기획으로 전환한 개정본

## 1. 제품 정의

이 제품은 공공기관 규정, 지침, 내규, 업무편람 문서를 전처리하여 **로컬 규정 DB**를 만들고, 기관 내부망에 이미 도입된 생성형 AI가 바로 조회할 수 있는 **MCP 서버**를 생성·실행하는 도구다.

MVP의 핵심은 별도 질의 UI나 독립 챗봇을 새로 만드는 것이 아니다. 기관이 이미 승인해 운영 중인 생성형 AI가 있다면, 이 제품은 그 AI에 규정 조회 도구를 붙이는 역할을 한다.

```text
규정 파일 탑재
 → 자동 전처리
 → 사람 검토/승인
 → 로컬 규정 DB 생성
 → MCP 서버 실행
 → 내부망 생성형 AI가 MCP 도구로 조회
```

RAG, Vector DB, 임베딩은 제품의 전면 기능이 아니라 MCP 도구 내부에서 검색 품질을 높이기 위한 선택 구현이다. 즉 제품 형태는 **RAG-backed MCP**다. 외부 사용자와 기관 AI에는 MCP `search`, `fetch`, `get_article` 같은 도구 계약을 제공하고, 서버 내부에서 승인된 로컬 규정 DB와 검색 인덱스를 사용한다.

## 2. 배경과 판단

기존 기획은 전처리 결과를 Vector DB로 만들고 로컬 LLM 질의 기능까지 제공하는 방향이었다. 하지만 공공기관에는 이미 내부망 생성형 AI, 문서 질의 시스템, 또는 폐쇄망 LLM 플랫폼이 존재할 수 있다. 이 경우 별도 챗봇을 또 만드는 것보다, 승인된 규정 DB를 MCP 서버로 제공하여 기존 AI가 도구처럼 호출하게 하는 방식이 더 실용적이다.

따라서 제품 포지션은 다음과 같이 변경한다.

| 기존 방향 | 개정 방향 |
| --- | --- |
| 전처리기 + Vector DB + 별도 질의 UI | 전처리기 + 로컬 규정 DB + MCP 서버 생성/실행기 |
| 제품 안에 챗봇 포함 | 기관 내부망 AI에 MCP로 연결 |
| RAG API가 중심 | MCP `search`/`fetch`/조항조회 도구가 중심 |
| 로컬 LLM 구성까지 제품 범위 | 내부망 승인 AI는 기관 기존 시스템을 사용 |

## 3. 목표

- 규정 파일을 기관 내부망에서 전처리하고 로컬 규정 DB로 만든다.
- 사람이 검토·승인한 데이터만 MCP 조회 대상으로 공개한다.
- 기관 내부 생성형 AI가 MCP 클라이언트로 규정 검색, 조항 조회, 표 조회, 출처 확인을 수행하게 한다.
- 원문 파일 경로, 미승인 청크, 검수 중 데이터는 MCP 응답에 노출하지 않는다.
- 운영자가 프로젝트별 MCP 서버를 쉽게 실행·중지·재생성할 수 있게 한다.
- Vector 검색은 선택 기능으로 유지하되 사용자는 “MCP 규정 서버”로 인식하게 한다.

## 4. 비목표

- MVP에서 별도 범용 챗봇 UI를 제공하지 않는다.
- MVP 기본값으로 외부 ChatGPT 연결을 제공하지 않는다.
- 기관 내부망 생성형 AI의 보안성, 계정관리, 모델 운영을 대체하지 않는다.
- LLM 학습, 파인튜닝, 자동 법률판단, 인사·감사 최종 판단을 수행하지 않는다.
- 완전한 AI-DLP나 악성코드 탐지 제품을 표방하지 않는다.

## 5. 대상 사용자

### 5.1 운영자

규정 파일을 등록하고 전처리 작업, 검수 상태, 승인 상태, MCP 서버 실행 상태를 관리한다.

### 5.2 검수자

청크, 조항, 표, 별표, 출처 정보를 확인하고 MCP 공개 여부를 승인 또는 반려한다.

### 5.3 내부망 AI 사용자

기존 기관 생성형 AI에서 자연어로 질문하고, MCP 서버가 반환한 규정 근거를 바탕으로 답변을 받는다.

### 5.4 관리자

프로젝트, tenant, 보안등급, 부서 접근 범위, 감사로그, 보존·삭제 정책을 관리한다.

## 6. 시스템 아키텍처

```text
[운영자 UI/API]
      |
[문서 반입 저장소]
      |
[전처리 파이프라인]
      |  텍스트 추출, 조항/표/별표 분리, 메타데이터 생성
      v
[검수/승인 게이트]
      |  승인된 청크만 공개 가능
      v
[로컬 규정 DB]
      |  문서, 조항, 청크, 표, 승인기록, 출처, 감사로그
      v
[MCP 서버]
      |  search/fetch/get_article/get_table/compare_versions
      v
[기관 내부망 생성형 AI]
```

로컬 규정 DB는 MVP에서 기존 JSON repository와 JSONL index를 사용할 수 있다. 제품화 단계에서는 SQLite를 기본 저장소로 두고, 대량 검색이 필요한 경우 내부 Vector index를 선택적으로 생성한다.

## 7. MCP 서버 요구사항

### 7.1 실행 방식

- `stdio` 모드: Claude Desktop, Codex, 내부 MCP 클라이언트용 로컬 프로세스 실행
- `streamable-http` 모드: 내부망 AI 플랫폼에서 HTTP 기반 MCP를 호출하는 구성
- `sse` 모드: SSE 기반 MCP 클라이언트가 필요한 경우의 선택 구성
- HTTPS 종단: 별도 승인된 외부/DMZ 연결에서는 reverse proxy나 망연계 구간에서 HTTPS를 적용하는 후속 구성
- HTTP/SSE non-loopback 실행은 bearer token 또는 승인된 reverse proxy/mTLS 같은 접근통제를 요구한다.

MVP 기본값은 `stdio`와 `streamable-http`다.

### 7.2 기본 도구

| 도구 | 설명 |
| --- | --- |
| `search` | ChatGPT/Claude 호환성을 위한 기본 검색 도구. 승인된 규정 청크만 반환 |
| `fetch` | `search` 결과 ID로 전체 근거 텍스트와 citation metadata 반환 |
| `list_documents` | 승인된 문서 목록 조회 |
| `get_article` | 문서 ID와 조항 번호로 정확 조회 |
| `get_table` | 표/별표 ID로 구조화 데이터 조회 |
| `compare_versions` | 같은 규정의 버전 또는 시행일 차이 비교 |
| `get_citation` | chunk/article/table ID의 출처 정보 반환 |
| `get_index_status` | 운영자용 DB/MCP 공개 상태 확인 |

### 7.3 응답 원칙

- MCP 응답은 구조화 JSON을 우선한다.
- citation에는 문서명, 조항 번호, 페이지, 승인 ID, 내용 해시를 포함한다.
- 로컬 파일 시스템 경로는 반환하지 않는다.
- 미승인, 반려, 보안차단, superseded 데이터는 검색 대상에서 제외한다.
- 검색 결과에는 필요한 최소 본문과 출처만 포함한다.

### 7.4 RAG-backed MCP 동작 원칙

- MCP는 연결 표준과 도구 인터페이스다.
- RAG/Vector 검색은 MCP `search` 도구 내부의 검색 엔진 역할을 한다.
- `fetch`, `get_article`, `get_table`은 검색 결과 ID나 규정 식별자로 승인된 원문 근거를 반환한다.
- 내부망 생성형 AI는 반환된 근거를 바탕으로 답변을 생성하되, 근거가 없으면 추론하지 않도록 시스템 안내를 둔다.
- 운영자는 `get_index_status`와 smoke 리포트로 승인 데이터, 인덱스, MCP 공개 상태를 확인한다.

## 8. 로컬 규정 DB 요구사항

주요 엔티티는 다음과 같다.

- `Document`: 문서 ID, 파일 해시, 문서명, 기관명, 출처, 버전, 시행일
- `Chunk`: 전처리된 본문 단위, 조항/표/본문 유형, 출처 위치
- `Article`: 조항 번호, 제목, 본문, 상위 문서, 시행일
- `Table`: 표/별표 제목, 행/열 구조, 원문 위치
- `Approval`: 승인자, 승인일, 승인 ID, 승인된 내용 해시
- `McpIndex`: MCP 공개 대상 레코드와 검색 인덱스 상태
- `AuditLog`: 등록, 수정, 승인, MCP 조회 기록

MVP는 현재 저장소 구조를 유지하되, MCP 도구는 `Approval`과 `McpIndex`를 통과한 데이터만 읽는다.

## 9. 보안과 운영 경계

기관 내부망에 이미 승인된 생성형 AI가 있는 경우, 이 제품은 과도한 보안 플랫폼을 자체 구현하지 않는다. 다만 MCP 서버로 데이터를 넘기는 경계에는 최소 통제를 둔다.

- 승인된 데이터만 MCP 공개
- tenant/project 단위 DB 분리
- 부서 ACL과 보안등급 필터 선택 적용
- MCP 서버 기본 role은 `operator`이며, `admin` role은 프로젝트별 승인 후 명시적으로만 사용
- HTTP/SSE MCP 서버를 non-loopback host로 실행할 때는 `--http-bearer-token-env`를 기본으로 사용하고, 무인증 예외는 승인된 폐쇄망 통제에서만 허용
- MCP 검색, fetch, 조항/표/citation/status 조회 감사로그 기록
- 원문 파일 경로와 미승인 데이터 비노출
- DB와 인덱스 무결성 해시 검증
- 외부 ChatGPT 연결은 기본 비활성

보안성검토 관점에서는 “새로운 범용 AI”가 아니라 “기관 내부 승인 AI에 연결되는 규정 조회 MCP 서버”로 설명한다.

## 10. 사용자 흐름

1. 운영자가 규정 파일을 업로드한다.
2. 전처리기가 텍스트, 조항, 표, 별표, 메타데이터를 추출한다.
3. 검수자가 전처리 결과를 확인하고 필요한 부분을 수정한다.
4. 검수자가 MCP 공개 가능 청크를 승인한다.
5. 시스템이 로컬 규정 DB와 MCP index를 생성한다.
6. 운영자가 프로젝트 MCP 서버를 실행한다.
7. 기관 내부망 생성형 AI가 MCP 서버를 연결한다.
8. 사용자는 내부 AI에서 규정을 질의하고, AI는 MCP 도구 결과를 근거로 답변한다.

## 11. UI/API 요구사항

### 11.1 운영자 화면

- 프로젝트 목록과 MCP 서버 실행 상태 표시
- 문서 업로드, 전처리 실행, 재처리
- 승인 현황과 MCP 공개 가능 여부 표시
- MCP 연결 설정 예시 복사
- 감사로그와 증적 리포트 다운로드

### 11.2 검수 화면

- 청크별 본문, 조항 번호, 표, 페이지, 출처 확인
- 승인/반려/수정/분할/병합
- 보안등급과 부서 ACL 지정
- 승인 후 MCP 공개 대상 미리보기

### 11.3 MVP 운영 CLI

- `reg-rag-mcp-server`: 승인된 로컬 규정 DB를 MCP 서버로 실행
- `reg-rag-mcp-config`: MCP 클라이언트 등록용 JSON 스니펫 생성. `generic`, `claude-desktop`, `claude-code`, `chatgpt`, `claude-api`, `bundle` 프로필과 role, actor, department, tenant isolation 옵션 포함. `bundle.quickstart`는 Claude Code 등록 인자, ChatGPT Connector URL, Claude API 복사 필드, OpenAI Secure MCP Tunnel 템플릿, 로컬 검증 명령을 함께 제공
- `reg-rag-mcp-smoke`: 승인, 인덱싱, MCP 조회, 증적 리포트 체인 점검

다음 API는 프로젝트 서버 관리 UI를 만들 때의 후속 후보이며, 현재 MVP 구현 계약은 아니다.

- `POST /api/mcp/projects/{project_id}/start`
- `POST /api/mcp/projects/{project_id}/stop`
- `GET /api/mcp/projects/{project_id}/config`
- `GET /api/mcp/projects/{project_id}/evidence`

## 12. 배포 요구사항

### 12.1 내부망 기본 구성

- 전처리 서버, 로컬 규정 DB, MCP 서버는 기관 내부망에 배치한다.
- 내부 생성형 AI 플랫폼에서 MCP 서버 주소 또는 stdio 실행 명령을 등록한다.
- 외부망 호출은 기본값으로 사용하지 않는다.

### 12.2 로컬 단일 PC 구성

- 운영자 PC에서 전처리와 MCP 서버를 함께 실행한다.
- Claude Desktop 등 로컬 MCP 클라이언트에 stdio 명령을 등록한다.
- 파일럿과 데모에 적합하다.

### 12.3 외부 ChatGPT 연결

외부 ChatGPT Apps/Connectors 연결은 MVP 기본 범위가 아니다. 필요 시 공개 가능 데이터 또는 별도 승인된 망연계 환경에서 HTTPS MCP 서버로 구성한다. 내부망 MCP 서버를 외부에 직접 노출하지 않는 구성이 필요하면 OpenAI Secure MCP Tunnel 템플릿을 선택 경로로 제공한다.

## 13. 성공 지표

- 규정 업로드부터 MCP 서버 실행까지 30분 이내 완료
- 승인된 청크만 MCP 검색 결과에 노출
- `search`/`fetch` 호환 도구로 내부 AI 연결 성공
- 조항 번호 기반 조회 정확도 95% 이상
- MCP 응답에 로컬 경로와 미승인 데이터 미포함
- 운영자가 프로젝트별 MCP 설정을 재현 가능

## 14. MVP 범위

### 포함

- PDF/HWP/HWPX/DOCX 전처리
- 조항/표/별표/메타데이터 추출
- 사람 검토와 승인 게이트
- 로컬 규정 DB 생성
- 승인 데이터 기반 MCP `search`/`fetch`
- 조항 조회, 표 조회, citation 조회
- stdio MCP 서버 실행기
- local HTTP MCP 서버 실행기
- MCP 연결 가이드와 운영 증적 리포트

### 제외

- MVP 범위의 독립 챗봇 UI/LLM 답변 제품화
- 외부 ChatGPT 기본 연결
- LLM 모델 운영/학습/튜닝
- SSO, HA, 기관 통합 IAM
- 완전 자동 보안성검토 대체

## 15. 개발 마일스톤

### Phase 1: 문서 방향 전환

- PRD, README, AGENTS, 운영 문서에서 MCP-first 방향 반영
- “별도 질의 UI/챗봇” 표현을 “선택적 내부 검색 구현”으로 낮춤

### Phase 2: 로컬 규정 DB 계약

- 승인된 문서/청크/조항/표 조회 서비스 정리
- MCP 공개 가능 레코드와 미승인 레코드 분리
- DB/index 무결성 검증

### Phase 3: MCP 서버 MVP

- `search`/`fetch` 도구 구현
- `get_article`, `get_table`, `get_citation` 구현
- stdio 실행 스크립트 추가
- local HTTP 실행 옵션 추가

### Phase 4: 내부망 AI 연결 가이드

- Claude Desktop/Claude Code 예시 설정
- ChatGPT connector와 Claude API MCP connector용 HTTPS URL 설정 템플릿
- 내부망 AI 플랫폼용 HTTP MCP 설정 템플릿
- `bundle.quickstart` 기반 원클릭에 가까운 연결 안내 출력. Claude Desktop/Claude Code, ChatGPT HTTPS connector, OpenAI Secure MCP Tunnel, Claude API fragment를 같은 번들에서 제공
- 운영자 점검표와 장애 대응 문서

### Phase 5: 운영 고도화

- 프로젝트별 MCP 서버 관리
- 감사로그 리포트
- SQLite 저장소 전환 검토
- 대량 규정 검색 성능 개선

## 16. 수용 기준

- 전처리 후 승인하지 않은 청크는 MCP `search`와 `fetch`에서 조회되지 않는다.
- MCP `search`는 `results` 배열을 반환하고 각 결과에는 안정적인 ID와 제목, citation metadata가 있다.
- MCP `fetch`는 결과 ID로 승인된 본문과 출처를 반환한다.
- `get_article`은 조항 번호로 정확한 조항을 반환한다.
- MCP 응답에는 로컬 파일 경로가 포함되지 않는다.
- `reg-rag-mcp-config --client-profile bundle` 출력만으로 Claude Desktop, Claude Code, ChatGPT Connector, OpenAI Secure MCP Tunnel, Claude API 연결에 필요한 값과 검증 명령을 확인할 수 있다.
- 운영자는 README 또는 운영 문서만 보고 MCP 서버를 실행하고 내부망 AI에 연결할 수 있다.

## 17. 열린 질문

- MVP 저장소를 현재 JSON repository로 유지할지, SQLite를 바로 도입할지
- 내부망 AI 플랫폼이 지원하는 MCP transport 범위
- 기관별 보안등급과 부서 ACL을 필수로 둘지 선택 기능으로 둘지
- 검색은 키워드 우선으로 시작할지, deterministic local embedding을 기본 포함할지
- ChatGPT Apps 연결은 공개 규정 전용 별도 프로파일로 둘지
