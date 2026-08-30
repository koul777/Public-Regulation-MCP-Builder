# 초보자용 규정 작성 워크스페이스 롤아웃 계획

기준일: 2026-08-30

## 목표

기존 제품은 문서 업로드, 전처리, 검토, 승인, approved-only 인덱싱, RAG/MCP 공개에 최적화되어 있다. 초보자가 빈 화면에서 규정 초안을 만드는 흐름은 없었다.

이번 P0의 목표는 초보자가 로컬 화면에서 질문형 입력과 템플릿 안내를 따라 초안을 만들되, 그 결과가 공식 승인이나 인덱싱 경로로 오인되지 않도록 별도 워크스페이스를 제공하는 것이다.

## 현재 판단

- 구현 상태: 완료
- 자동 검증 상태: 충분
- 운영 배포 상태: 보류
- 출시 판정: `CONDITIONAL GO`

보류 사유는 코드 품질이 아니라 운영 증거 부족이다. 특히 인간 파일럿, 운영 계정 분리 증거, symlink 가능한 CI가 아직 없다.

## 초보자 관점의 개선 포인트

| 문제 | 현재 제품의 한계 | P0 개선 | 남은 과제 |
|---|---|---|---|
| 어디서 시작해야 하는지 모름 | 업로드·전처리 중심 진입 | `새 규정 작성` 전용 시작 화면 | 실제 초보자 파일럿으로 시작 성공률 측정 |
| 조문 구조를 모름 | 구조는 검토 단계 뒤에 드러남 | 템플릿 기반 조문 구조와 안내 문구 제공 | 분야별 템플릿 확장 |
| 무엇을 빠뜨렸는지 모름 | 승인 전까지 누락 발견이 늦음 | 목적, 범위, 근거, 담당 부서, 체크리스트 선검증 | 사용성 파일럿에서 누락률 측정 |
| 공식 승인과 혼동함 | 기존 승인/RAG 흐름이 강함 | export-only, `공식 승인 아님`, training-only 경계 | 화면 문구 오인율 0% 검증 |
| 여러 창에서 작업이 꼬임 | 초안 작성 전용 상태 관리가 없음 | expected revision, base revision, section dirty 복구 | 브라우저 기반 수동 시나리오 추가 검증 |

## 단계별 추진안

### 0단계: 정책 고정

- 초안 작성은 `Document` 생성이나 approved-only 인덱싱을 직접 호출하지 않는다.
- P0 결과물은 Markdown/JSON 초안 패키지뿐이다.
- 보호 환경 self-freeze는 금지하고, 로컬 연습 경로에서만 동의와 사유를 받아 허용한다.

종료 조건:

- MVP 계약 문서 확정
- 보안 모델 문서 확정
- 공식 경계 문구와 비목표 문서화

### 1단계: P0 구현

- authoring 전용 schema, service, repository, API, Streamlit UI 구현
- 상태 전이: `planning`, `drafting`, `review_requested`, `changes_requested`, `content_frozen`, `exported`, `abandoned`
- export integrity, purge 연계, audit event, content-free 에러 처리 구현

종료 조건:

- 기능 게이트 1~8 자동 검증 통과
- 공식 경로와의 분리 테스트 통과

### 2단계: 초보자 파일럿

- 최소 5명 이상 초보자 대상 과업 수행
- 과업: 새 규정 초안 시작, 템플릿 선택, 필수 항목 작성, 검토 요청, export
- 측정: 완료율, 오인율, 200% 확대 접근성, 다음 행동 10초 내 인지율

종료 조건:

- 완료율 80% 이상
- `공식 승인` 오인 0건
- 치명 UX 이슈 0건 또는 수정 완료

### 3단계: 제한 운영

- 로컬 연습 또는 제한된 내부 초안 작성으로만 배포
- 운영 문의 대응 절차, purge 복구 절차, 중단 기준 문서화
- 보호 환경 token 발급과 actor/tenant 결합 운영 증거 수집

종료 조건:

- 운영 증거 보관
- purge 및 복구 점검표 마련
- 중단/롤백 절차 검토

### 4단계: GO/NO-GO 재판정

- 제품 책임자, 보안 책임자, UX 책임자가 증거 묶음을 검토
- human pilot, 운영 계정 증거, symlink CI 결과를 함께 판단

종료 조건:

- High/Medium 미해결 0건
- 운영 증거 3종 확보
- 일반 배포 또는 보호 환경 배포 여부 명시

## 측정 지표와 중단 기준

| 지표 | 목표 | 중단 기준 |
|---|---:|---|
| 초안 시작 성공률 | 90% 이상 | 80% 미만이면 롤아웃 중단 |
| 과업 완료율 | 80% 이상 | 동일 과업 실패가 40% 이상이면 화면 재설계 |
| 다음 행동 인지율 | 80% 이상 | 2명 이상이 핵심 다음 행동을 찾지 못하면 문구 수정 |
| 공식 승인 오인 | 0건 | 1건이라도 발생하면 기능 플래그 비활성화 |
| cross-tenant 노출 | 0건 | 1건이라도 발생하면 즉시 중단 |
| purge 누락 | 0건 | 1건이라도 발생하면 배포 중단 |
| High/Medium 보안 finding | 0건 | 1건이라도 발생하면 NO-GO |

## 역할과 책임

| 역할 | 책임 |
|---|---|
| 제품 책임자 | 범위 확정, GO/NO-GO 판정 |
| 백엔드 책임자 | 상태 전이, export integrity, purge, API 격리 |
| 프론트엔드 책임자 | 초보자 화면, 오류 문구, 안내 흐름 |
| 보안 책임자 | 공식 경계, actor/tenant 증거, 릴리스 위생 |
| UX/접근성 담당 | 파일럿 설계와 결과 정리 |

## 작업 블록별 AI Agent 오케스트레이션 운영안

초보자용 규정 작성 기능은 한 에이전트가 전부 판단하는 방식보다, 책임이 다른 에이전트가 같은 증거를 교차 검토하는 편이 안전하다. 권장 오케스트레이션은 아래와 같다.

| 작업 블록 | 주 에이전트 | 보조 에이전트 | 입력 증거 | 완료 조건 |
|---|---|---|---|---|
| 범위 고정 | Product agent | Security agent | MVP 계약, 비목표, 승인 경계 문서 | 공식 경계와 비목표가 문서로 고정됨 |
| 템플릿/가이드 설계 | UX agent | Legal structure agent | 기존 규정 예시, 초보자 체크리스트, 템플릿 계약 | 초보자가 빈칸 없이 시작 가능한 템플릿 초안 확보 |
| API/저장소 구현 | Backend agent | Security agent | 상태 전이 표, tenant/profile 규칙, export 계약 | route/service/repository 테스트 통과 |
| 화면 구현 | Frontend agent | UX agent | 상태별 문구, 버튼 정책, 오류 문안 | AppTest와 수동 흐름 검증 통과 |
| 위협 점검 | Security agent | Backend agent | event log, purge 흐름, export 무결성 테스트 | High/Medium 신규 finding 0건 |
| 사용성 파일럿 | UX agent | Product agent | 파일럿 과업표, 관찰 기록지 | 완료율·오인율 목표 충족 |
| 릴리스 위생 | Release agent | Security agent | build 산출물, hygiene audit, 패키지 manifest | 공개 저장소 위생 검사 통과 |
| GO/NO-GO 판정 | Product agent | Security/UX/Release agent | 위 단계 산출물 전부 | 운영 허용 범위와 제한 범위 명시 |

운영 규칙:

1. 에이전트 간 전달물은 문서 경로, 테스트 결과, SHA-256 같은 증거만 넘기고 원시 민감 데이터는 공유하지 않는다.
2. Product agent는 일정 관리만 하고 보안 예외를 승인하지 않는다. 보안 예외는 Security agent와 제품 책임자가 함께 서면으로 남긴다.
3. UX agent가 초보자 혼동을 보고하면 Frontend agent는 문구 수정만으로 끝내지 말고 상태 전이와 기본값도 함께 재검토한다.
4. Release agent는 최종 빌드 직전 문서 링크, 공개 경로, 절대 경로 누출 여부를 다시 검사한다.
5. GO/NO-GO 판정은 한 에이전트의 의견이 아니라 테스트, 파일럿, 운영 증거 3종이 동시에 충족될 때만 상향한다.

이 저장소의 일반 오케스트레이션 원칙은 [에이전트 역할 정의](agent_orchestration_roles_ko.md) 문서를 따른다. 다만 초보자용 규정 작성 기능은 공식 승인 경계가 강하므로 Security agent와 UX agent의 거부권을 별도로 유지한다.

## P1 이후 확장 원칙

- AI 제안 기능은 P0와 다른 통제 모델로 다룬다.
- AI 제안은 초안 제안과 provenance 기록까지만 허용한다.
- 자동 승인, 자동 인덱싱, 자동 MCP 공개는 별도 승인 없이는 금지한다.
- import bridge가 필요하면 approved-only, review flag, Code Owner 검토를 다시 통과해야 한다.

## 당장 실행할 후속 작업

1. [초보자 사용성 검증 계획](authoring_beginner_usability_test_plan_ko.md)과 [파일럿 진행 스크립트](authoring_beginner_pilot_facilitator_script_ko.md)대로 파일럿을 수행한다.
2. 보호 환경 운영자와 함께 구조화 token 발급 및 계정 분리 증거를 남긴다.
3. POSIX 또는 symlink 가능한 Windows CI 잡을 추가한다.
4. [초보자 후속 개선 백로그](authoring_beginner_improvement_backlog_ko.md)에서 P0.1 항목 1~4를 파일럿 결과에 맞춰 우선 반영한다.
5. 세 증거가 모이면 [GO/NO-GO 메모 템플릿](authoring_go_nogo_memo_template_ko.md)으로 재판정 메모를 작성한다.
