# 기관 중심 규정 관리 및 MCP 운영 계약

## 1. 제품 범위

1차 제품은 기관 간 비교 기능보다 기관별 규정의 등록, 개정 이력, 승인, 최신본 RAG, MCP 제공을 우선한다.

운영자는 먼저 기관 프로필을 선택하고, 선택된 기관 범위에서 규정을 등록하거나 개정본을 추가한다. 모든 문서와 벡터 레코드는 `tenant_id`와 `profile_id`를 기준으로 범위를 제한한다.

기관 프로필은 `POST /api/institutions`로 생성하거나 수정할 수 있다. 요청에는 기관 식별자와 기관명을 포함하고, tenant는 인증된 `X-Tenant-Id` 컨텍스트에서 결정한다. 이미 다른 tenant에 배정된 프로필은 수정할 수 없다.

```json
{
  "profile_id": "institution-a",
  "display_name": "기관 A",
  "institution_name": "기관 A 공식 명칭",
  "required_row_fields": ["profile_id"]
}
```

## 2. 규정 식별 및 개정 이력

규정 가족은 `regulation_id`로 식별하고, 개정본은 `regulation_version`으로 구분한다. 다음 메타데이터를 문서와 승인된 벡터 레코드에 유지한다.

- `institution_name`, `profile_id`
- `regulation_id`, `regulation_version`
- `revision_date`, `effective_from`, `effective_to`, `repealed_at`
- `regulation_status`, `supersedes_document_id`
- `source_system`, `source_url`

개정본을 등록할 때 이전 문서가 같은 tenant, 기관 프로필, 규정 가족에 속하는지 확인한다. 이전 승인본이 있고 새 규정의 효력일이 이전보다 늦으면 이전 승인본을 `superseded`로 전환하고, 이전 문서의 `effective_to`를 새 효력일로 닫는다. 미래 효력일이거나 날짜가 불완전하면 자동 전환하지 않고 감사 이벤트와 검토 대상으로 남긴다.

허용되는 일반 흐름은 다음과 같다.

```text
uploaded -> processing -> pending_review -> approved
approved -> superseded
approved -> repealed
```

승인되지 않은 문서는 RAG와 MCP 답변 근거에 포함하지 않는다. 동일 규정 가족·동일 버전의 중복 업로드는 차단한다.

## 3. RAG 기준

RAG는 규정별 최신본만 대상으로 한다. 단, 최신본으로 인정되려면 다음 조건을 모두 만족해야 한다.

- 승인 상태일 것
- `regulation_id`, `regulation_version`, `effective_from`이 있을 것
- 기관 프로필과 tenant 범위가 요청 범위와 일치할 것
- 폐지되었거나 효력 기간이 끝난 문서가 아닐 것
- 이전 버전이 같은 활성 규정 가족에 남아 있지 않을 것

과거 시점의 규정 확인은 일반 RAG 검색과 분리된 이력 조회에서 `as_of_date`를 사용한다. 이력 응답에는 현재 문서 식별자와 각 버전의 `is_current` 표시를 포함한다.

기관별 규정 카탈로그 API도 동일하게 기관 프로필 아래에 규정 가족을 묶고, 각 가족의 `current_document_id`와 버전별 `is_current`를 제공한다. 따라서 운영 화면의 트리와 RAG 최신본 판정이 서로 다른 기준을 사용하지 않는다.

## 4. MCP와 MCP 서버의 구분

`MCP`는 클라이언트와 도구 호스트가 규칙에 따라 통신하는 프로토콜이다. `MCP 서버`는 이 프로토콜을 구현하고 규정 조회 도구를 제공하는 실행 프로세스다.

이 제품의 구성은 다음과 같다.

- RAG: 승인된 규정의 검색 및 근거 제공 계층
- MCP 서버: `search_regulations`, `fetch_regulation`, `get_regulation_history` 등 도구를 노출하는 계층
- MCP 클라이언트: Claude, ChatGPT 또는 기관 AI가 서버에 연결하는 호출 주체

따라서 설정에서 선택하는 대상은 MCP 자체가 아니라 MCP 서버의 연결 방식과 실행 대상이다.

## 5. 로컬 MCP와 HTTP MCP

- 로컬 MCP: MCP 서버를 같은 컴퓨터에서 `stdio`로 실행한다. 파일과 로컬 DB 접근이 단순하고 외부 노출이 적다.
- HTTP MCP: MCP 서버를 내부망 또는 보호된 서비스로 실행하고 `streamable-http`로 연결한다. 여러 클라이언트와 운영 관찰, 중앙 배포에 적합하다.

두 방식은 서로 다른 제품이 아니라 동일한 규정 MCP 서버의 transport 선택이다. 연결 설정에는 tenant와 기관 프로필 범위를 함께 기록해야 하며, transport가 달라도 승인된 최신본·이력·인용 정책은 동일해야 한다.

## 6. 1차 범위에서 제외하는 기능

기관 간 제도 비교는 규정 정합성, 범위, 공개 수준, 비교 기준이 먼저 정리되어야 하므로 1차 핵심 기능에서는 제외한다. 기관별 최신 규정 관리와 이력·근거 품질이 안정화된 뒤 별도 비교 인덱스와 비교 결과 모델로 확장한다.

## 7. Release 판단

기관 단위 release 판단은 다음 실행 범위를 동일하게 사용해야 한다.

```text
local smoke
stdio transport smoke
streamable-http transport smoke
temporal metadata audit
readiness audit
migration manifest
```

각 명령에는 동일한 `tenant_id`와 `profile_id`를 전달한다. readiness audit에서 `profile_id`를 생략하면 tenant 전체 감사로 동작하며, 이는 전체 tenant 운영 점검용이다. migration manifest는 차단 항목을 자동 수정하지 않고 누락 메타데이터와 권고 조치만 보고한다.

반복 실행은 `scripts/run_institution_release_gate.py`를 사용한다. 이 오케스트레이터는 모든 단계에 동일한 기관 범위를 전달하고, 단계별 로그와 종합 결과를 하나의 release evidence 디렉터리에 보존한다.
