# 초보자 사용성·유지보수 오케스트레이션 계획

## 목적

이 문서는 규정 전처리부터 승인·색인·Qwen/MCP 사용까지의 흐름을 초보자가 쉽게
완료하고, 다음 담당자가 코드와 운영 절차를 쉽게 수정할 수 있도록 개선 순서와
의사결정 결과를 기록한다.

## 팀 운영 방식

| 역할 | 담당 | 책임 |
| --- | --- | --- |
| 팀장·통합자 | Codex | 요구사항 분해, 충돌 조정, 코드 반영, 테스트·문서 최종 확인 |
| 제품·코드 검토자 | Claude Opus | 초보자 흐름, 유지보수 경계, 회귀 위험에 대한 독립 검토 |
| 예비 검토자 | Luna Reserve | 대안 설계와 반대 의견 검토. 호출 가능할 때 Opus와 교차 토론 |

Luna Reserve는 이번 실행에서 호출을 시도했으나 계정 사용량 제한으로 실제 응답을
받지 못했다. 따라서 이번 구현의 독립 검토 근거는 Claude Opus 검토와 기존 회귀
테스트이며, Luna Reserve 의견을 받은 것으로 간주하지 않는다.

## 5시간 실행 배정표

| 시간 | Luna Reserve | Claude Opus | Codex 팀장·산출물 |
| --- | --- | --- | --- |
| 00:00–00:30 | 초보자 목표 여정·실패 지점 | 책임 경계·큰 위험 | 기준선·용어표·파일 지도 |
| 00:30–01:20 | 화면 문구·복구·P0/P1/P2 | facade 후보·보안·테스트 공백 | 독립 보고서 수집 |
| 01:20–02:00 | Opus 구조안에 UX 반론 | Luna 흐름안에 구조·보안 반론 | 주장·근거·반론 기록 |
| 02:00–02:45 | 수용 기준 확인 | 변경 위험·롤백 기준 확인 | 결정/보류 목록과 대상 파일 확정 |
| 02:45–03:45 | 사용자 문구 검토 기준 | 안전·회귀 검토 기준 | 작은 서비스/UI 변경만 구현 |
| 03:45–04:30 | 초보자 오인·복구 기준 | 승인·tenant·audit·MCP gate | 단위·계약·smoke·배포 검사 |
| 04:30–05:00 | 최종 사용성 반론 | 최종 구조·보안 반론 | 결과·잔여 위험·인수인계 |

각 팀원은 먼저 독립 의견을 내고, 서로의 주장에 파일 근거를 붙여 반론한다. 한쪽
의견이 없거나 정책 합의가 필요한 항목은 성공으로 표시하지 않고 보류한다. 이번
실행의 상세 위임문·토론표·검증 로그는 로컬 세션 보고서에서 관리하며 public source
distribution에는 포함하지 않는다.

## 이번 토론에서 확정한 원칙

1. 최근 추가된 원클릭 승인 기능은 의도된 사용자 경험이므로 즉시 제거하지 않는다.
   대신 위험 신호(`action_required`, OCR 전용 결과, 표 검증 실패)를 기준으로 한
   안전정책을 별도 제품 결정으로 다룬다.
2. 이미 존재하는 `local_llm_doctor`, `mcp_connection_diagnostic`를 또 하나의
   진단 체계로 복제하지 않는다.
3. 대형 Streamlit 파일을 한 번에 재작성하지 않고, 서비스 경계를 먼저 만들고
   기존 함수명은 호환 래퍼로 보존한다.
4. 공식 RAG/MCP의 승인·색인·테넌트 격리·감사 증거의 fail-closed 동작은 유지한다.

## 이번 5시간 범위에서 하지 않는 일

- AI가 사람 승인·승인 journal·색인 공개를 대신 결정하도록 바꾸지 않는다.
- 초보자 UI 개선을 이유로 인증·tenant 격리·감사 로그를 생략하지 않는다.
- 큰 Streamlit 파일 전체 재작성, JSON 저장소의 즉시 DB 전환, 신규 telemetry 도입을
  함께 진행하지 않는다.
- 실제 기관 문서·개인정보·외부 계정으로 사용성 시험을 하지 않는다.

## Sprint 1 — 완료

### 사용자 경험

- 사이드바에 현재 문서의 `완료 단계 수 / 전체 단계 수`를 표시한다.
- 현재 단계와 “지금 할 일”을 홈 화면과 같은 규칙으로 보여 준다.
- 이동 위치를 함께 표시해 초보자가 다음 메뉴를 추측하지 않도록 한다.

### 유지보수

- `app/services/mcp_connection_service.py`를 추가해 MCP 번들 상태 읽기와
  데스크톱 관찰 실행을 Streamlit 화면에서 분리했다.
- `app/services/workflow_readiness.py`를 추가해 4단계 진행 계산을 순수 함수와
  안정적인 단계 식별자로 분리했다.
- `app/services/readiness_adapter.py`를 추가해 MCP·local LLM 진단 결과를
  `확인 불가 / 준비 완료 / 연결 확인 대기 / 조치 필요 / 선택 기능 미사용 /
  등록 필요 / 오래된 확인` 상태로 보수적으로 변환한다.
- MCP 연결 화면은 내부 reason code와 doctor payload를 그대로 보여주지 않고,
  초보자가 바로 실행할 복구 행동을 안내한다.
- MCP 상태 파일 경로가 비어 있거나 관찰 프로세스가 비정상 종료되면 `pending`이나
  성공으로 낮춰 표시하지 않고 확인 불가·조치 필요로 남긴다.
- `scripts/local_llm_doctor.py`는 비정상적인 local probe 응답을 성공이나 예외로
  흘리지 않고 `local_probe_invalid` 실패로 기록한다.
- 저장된 workflow 상태가 순서를 어겨도 운영자 화면 전체가 중단되지 않고
  ① 전처리 단계로 안전하게 돌아간다. 순수 서비스의 엄격한 검증은 별도 테스트로
  유지한다.
- 순수 workflow 상태 계산기는 4개의 실제 boolean만 받아 문자열·정수 상태를
  성공으로 오인하지 않으며, 운영자 화면은 잘못된 저장값을 ① 전처리로 되돌린다.
- 기존 Streamlit 함수명과 테스트용 주입 지점을 유지해 기존 운영 화면의 변경
  위험을 낮췄다.

### 검증

```text
python -m unittest tests.test_beginner_workflow_services tests.test_local_llm_doctor tests.test_qwen_chat_app tests.test_streamlit_ai_usage_path tests.test_streamlit_operator_mode -v
```

위 검증에서 새 서비스·MCP·초보자 화면 회귀 테스트 114개가 통과했다.

이후 MCP 관찰 오류·UNC 경로·단계 순서 검증을 보강했으며, 같은 묶음은 현재
114개로 확장되어 통과한다. 전체 회귀는
`python -m unittest discover -s tests -q`가 최신 변경까지 종료 코드 0으로
완료했다. 현재 테스트 로더 기준 발견 수는 3,694개이며, 선택적 skip 정책은
16개다.
readiness adapter 추가 전 기준으로는
3,669개 통과, 16개
선택적 skip이었다. 앞선 전체 실행에서 1개 프로세스 간 잠금 테스트가 대기
시간 안에 획득되지 않았으나 단독 재실행과 최신 전체 재실행에서는 통과했으므로
코드 실패가 아닌 간헐적 자원 경합 후보로 별도 추적한다.

### 2차 Claude Opus 검토 반영

2차 read-only 검토에서는 blocker/high가 없고 이번 작은 범위는 진행 가능하다는
판정을 받았다. 다만 초보자에게 더 정확한 상태를 보여 주기 위해 다음을 추가로
반영했다.

- 실제 MCP `pending`과 미등록 상태를 `등록 필요`로 분리하고, 상태 파일이 없거나
  손상된 경우에는 `확인 불가`로 남긴다.
- stale보다 실패 finding을 먼저 보여 주고, stale의 다음 행동을 새 번들 재등록으로
  통일한다.
- `model_free_mode`는 성공 신호가 함께 있을 때만 준비 완료로 인정하며, probe 없는
  `passed=True`는 연결 확인 대기로 남긴다.
- 실제 MCP diagnostic 생성 결과를 공통 adapter에 넣는 상태별 회귀 테스트를 추가하고,
  저장된 workflow 순서가 잘못돼도 UI가 ① 전처리로 안전하게 돌아가게 했다.
- reason code 정규식을 서비스와 adapter가 공유하도록 했다.

diagnostic builder를 `app/`으로 옮기는 역방향 의존성 개선은 별도 PR 후보로
보류했다. 이번 범위에서는 facade를 유지해 대형 Streamlit 재작성 없이 회귀 범위를
제한한다.

## 유지보수 우선순위를 정한 근거

- `frontend/streamlit_app.py`는 약 15,000줄·298개 함수로, 전체 재작성보다
  서비스 facade와 순수 상태 계산을 먼저 늘리는 편이 안전하다.
- MCP 설정 생성 스크립트도 크기가 크므로, 당장은 기존 CLI를 호출하되 화면이
  직접 JSON 파싱·프로세스 인자를 조립하지 않게 한다.
- 테스트는 서비스 계약, 보안 경계, Streamlit 연결 계약의 세 층으로 나누어야
  작은 변경을 빠르게 검증할 수 있다.

## Sprint 2 — 구현 순서와 현재 상태

1. **준비 상태 어댑터 통합 — CLI·화면 연결 완료**
   - 기존 MCP doctor와 local LLM doctor의 결과를 화면용 공통 상태 모델로 변환한다.
   - 준비 완료, 사용자의 조치 필요, 선택 기능 미설치, 확인 대기 상태를 구분한다.
   - 현재 `app/services/readiness_adapter.py`와 MCP 연결 화면, 운영자·독립
     Qwen 화면에 공통 계약을 반영했다. 2026-09-28에는 local LLM doctor의
     전체 진단을 서비스로 옮겨 CLI와 두 운영자 화면을 같은 계약에 연결했다.
   - 대상 후보: `scripts/check_mcp_connection_readiness.py`,
     `scripts/local_llm_doctor.py`, `scripts/mcp_connection_diagnostic.py`.

   화면용 상태는 원본 JSON의 `passed` 하나로 줄이지 않고 아래처럼 보존한다.

   | 원본 근거 | 화면 상태 | 사용자 행동 |
   | --- | --- | --- |
   | 보고서 없음·JSON 손상 | 확인 불가 | 번들/설정 위치를 확인하고 다시 읽기 |
   | MCP 번들은 만들었지만 앱 등록이 아직 없음 | 등록 필요 | 선택한 AI 앱에 설정을 등록한 뒤 연결 확인 |
   | 설정은 유효하지만 probe 미실행 | 설정 완료·연결 확인 대기 | 해당 AI 앱에서 목록·search·fetch 확인 |
   | 실패 reason 또는 보안 gate 실패 | 조치 필요 | 원인과 재시도 방법 확인 |
   | 선택 기능 미설치·미선택 경로 | 선택 기능 사용 안 함 | 기본 로컬 경로로 진행하거나 설치 |
   | 최신 증거와 현재 설정 불일치 | 오래된 확인 | 새 번들 생성 후 다시 등록·확인 |
2. **작성 화면 facade — 템플릿 경계 통합 완료**
   - `frontend/authoring_page.py`의 저장·검증·템플릿 호출을 서비스 facade로 옮긴다.
   - Streamlit 위젯은 입력과 표시만 담당하게 한다.
   - 기존 저장소·승인 서비스의 계약을 재사용하고, 작성 초안이 공식 RAG/MCP 색인으로
     자동 연결되지 않는 경계를 테스트로 고정한다.
   - 현재 화면은 이미 `AuthoringService`를 주입받고 있으므로, 중복 facade를 만들지
     말고 실제 책임 중복이 확인되는 작은 호출부터 분리한다.
3. **시작 화면 진단 개선 — Kordoc·로컬 QA 반영**
   - 설치 실패 시 숨겨진 명령 출력 대신 원인·사용자 조치·재시도 버튼을 한곳에 표시한다.
   - Kordoc, Ollama, Node 같은 선택 의존성은 필수/선택 여부를 명확히 표시한다.
   - 화면이 직접 외부 프로세스 인자를 조립하지 않도록
     `scripts/generate_mcp_client_config.py`, `scripts/analyze_regulation_corpus.py`,
     `scripts/find_available_ui_port.py` 호출을 작은 application service 계약 뒤로 둔다.
4. **승인 위험 신호 정책 결정 — 실제 파일럿 대기**
   - 원클릭 승인 자체를 막을지, 위험 신호가 있는 문서만 추가 확인을 요구할지
     실제 초보자 파일럿 결과를 보고 결정한다.

   정책을 결정하기 전까지는 현재 보호선을 유지한다.

   - 승인되지 않은 청크를 공식 vector/RAG에 넣지 않는다.
   - AI 검수 의견은 사람 확인·해소 상태와 분리한다.
   - OCR·표 구조·`action_required` 신호는 화면에서 숨기지 않는다.
   - PoC/미검수 경로는 공식 RAG/MCP와 별도 저장·표시한다.
   - tenant/profile 범위와 approval journal을 변경하지 않는다.

### P1 작업 단위와 인수 조건

| 작업 단위 | 주 책임 | 먼저 만들 테스트 | 인수 조건 |
| --- | --- | --- | --- |
| readiness 어댑터 | Opus 검토 기준 + Codex 구현 | 상태별 매핑·실패 보수성 | MCP 연결 화면이 공통 상태 계약을 사용하고 내부 doctor JSON을 직접 노출하지 않음 |
| authoring facade | Codex | 저장·린트·템플릿 계약 | 위젯 변경 없이 서비스 테스트가 통과함 |
| 진단·복구 안내 | Luna Reserve 의견 확보 시 교차 검토 + Codex 구현 | 오류 코드·재시도·경로 비노출 | 초보자가 오류 원인과 다음 행동을 한 화면에서 확인함 |
| 승인 위험 신호 | 사용자 정책 결정 후 착수 | 승인 journal·색인 gate 회귀 | 정책 결정 문서와 4-eyes/tenant 회귀 증거가 함께 있음 |
| CI fast path | Codex | focused 서비스·UI 테스트 실행 | 새 facade 회귀가 전체 릴리스 전에 PR에서 빠르게 감지됨 |

작업 순서는 항상 `순수 상태 계산 → 서비스 계약 → UI 연결 → 실제 프로세스 smoke`로
유지한다. 마지막 단계에서 실패하면 UI 문구를 성공으로 표시하지 않고, 진단 결과를
`확인 대기` 또는 `조치 필요`로 남긴다.

## 완료 기준

- 초보자 5명 이상이 첫 문서 작성·전처리·승인·질문 흐름을 수행한다.
- 다음 행동 식별 시간이 10초 이내인 비율이 80% 이상이다.
- 승인되지 않은 청크가 RAG/MCP에 들어가지 않는 회귀 테스트가 유지된다.
- 서비스 단위 테스트만으로 진단·진행 계산을 검증할 수 있다.
- 전체 unittest와 public-release hygiene 검사가 통과한다.
- 기관별 런타임 batch evidence가 없는 public source checkout에서는
  PUBLIC_PORTAL·integrated PDF 재사용 gate를 성공으로 주장하지 않는다. 해당
  gate는 별도의 release evidence bundle이 준비된 뒤 실행한다.

## Sprint 2 개발 반영 — 2026-09-28

이 절은 개발·자동 검증 범위를 기록한다. 실제 초보자 5명의 파일럿 완료나 승인 위험
정책의 변경을 의미하지 않는다. 이번 개발은 단일 구현 작업으로 진행했으며 새로운
Opus·Luna 검토를 받은 것으로 기록하지 않는다.

### 준비 상태와 복구 안내

- `app/services/local_llm_readiness_service.py`가 CLI와 두 운영자 화면의 로컬
  모델 진단을 담당한다. 모델 없는 답변은 설치 없이 사용 가능, 주소만 확인한
  상태는 연결 확인 대기, 실제 응답 성공은 준비 완료로 구분한다.
- 진단은 현재 실행의 모델·주소·시간 제한을 그대로 사용한다. 외부 주소와 자격
  증명이 들어간 주소는 요청 전에 차단하며 예외·잘못된 응답은 성공으로 바꾸지 않는다.
- `readiness_adapter`는 성공 표시와 명시적 실패가 충돌하면 조치 필요로 판정한다.
  초보자에게는 원본 JSON이나 예외 대신 원인에 맞는 한국어 다음 행동을 표시한다.
- 설정이 달라지면 이전 연결 확인을 다시 사용하지 않는다. 설정 화면의 확인 결과와
  Kordoc 실패 안내는 다른 위젯을 조작해도 유지된다.
- `operator_setup_service`가 Kordoc 설치 위치와 프로세스 실행을 담당한다. 설치
  실패·시간 초과·설치 도구 누락·실행 불가를 구분하고, 화면에는 원인과 복구 행동을
  함께 표시한다. 설치 출력은 경로뿐 아니라 인증 정보를 포함할 수 있어 노출하지 않는다.
- 설치 버튼은 사용자가 직접 눌러야 실행된다. Node.js/npm 미설치 시 설치는
  비활성화하지만 준비 상태 재확인과 빠른 구조 전처리는 계속할 수 있다. Kordoc의
  공식 MCP 품질 게이트와 재처리 초안·사람 승인 조건은 유지한다.

### 유지보수 경계

- 독립 Qwen 챗봇의 포트 선택과 실행 인자 조립은 `local_app_service`로 옮겼다.
  Windows에서는 숨김으로 실행하며, 프로세스 시작만으로 모델 연결 완료를 표시하지 않는다.
  기존 화면 함수와 환경 필터링, 실제 앱 health 확인은 호환 경계로 남겼다.
- 작성 화면이 별도 템플릿 서비스를 만들던 부분은 기존 `AuthoringService.list_templates()`로
  연결했다. 목록 선택과 초안 생성이 같은 주입 템플릿을 사용한다. 저장·린트는 이미
  서비스에서 수행하므로 중복 facade를 추가하지 않았다.
- `generate_mcp_client_config`는 기존 Python 호출과 승인 게이트를 유지한다.
  `analyze_regulation_corpus`에서 화면이 가져오는 값은 점수 상수와 순수 변환 함수다.
  이번 조사에서 이 두 경계의 외부 프로세스 인자 조립은 확인되지 않아 포장만 추가하지 않았다.
- PR의 기존 `preprocessing-regression` 작업은 새 서비스·복구 화면·작성 격리 테스트를
  먼저 실행하고, 이후 기존 전처리 회귀·빌드·공개 소스 감사를 계속 수행한다.

### 검증 명령

```powershell
python -m unittest tests.test_readiness_adapter tests.test_local_llm_readiness_service tests.test_operator_setup_service tests.test_local_app_service tests.test_streamlit_setup tests.test_qwen_chat_app tests.test_local_llm_doctor tests.test_github_workflow_templates -v
python -m unittest discover -s tests -v
python -m build --sdist --wheel
python scripts/audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

집중 검증은 47개 실행에서 실패·오류 없이 완료했으며 선택적 CI 템플릿 2개를
건너뛰었다. 테스트는 합성 응답·임시 저장소를 사용한다. 로컬 HTTP 서버와 실제
모델 probe 코드를 연결해 응답 확인을 검사하고, Streamlit AppTest로 실패 안내 유지,
재시도, 설치 비활성화 및 재확인을 실행한다. 실제 모델 설치나 사용자 전역 설치는
자동 테스트에서 실행하지 않는다.

호환성 재검증은 작성 화면·운영자 화면·로컬 QA·설치 서비스 90개 테스트가 모두
통과했다. 기존 UI 테스트 대역에는 새 템플릿 목록 메서드를 제공하고, 실행 인자의
소유 위치를 검사하던 테스트는 새 실행 서비스 계약에 맞췄다. 승인·저널·초안
격리에 관한 기존 단언은 유지했다.

최종 전체 회귀는 `python -m unittest discover -s tests -v`로 3,753개를
590.414초 동안 실행해 실패·오류 없이 완료했고 선택적 테스트 16개를 건너뛰었다.
`python -m build --sdist --wheel`과 위의 공개 소스 위생 감사도 통과했다.
배포용 sdist·wheel에 새 서비스 3개가 포함되는지 확인했다.

`python scripts/run_beginner_first_success.py --timeout-seconds 45`는 합성 문서로
실제 stdio MCP 프로세스의 초기화·규정 목록·검색·본문/인용 조회·계층 조회를
통과했다. 별도 임시 환경에서는 새 실행 서비스로 숨김 Qwen 앱을 시작해 loopback
health의 HTTP 200을 확인하고 해당 테스트 프로세스 트리를 종료했다. 이는 앱
프로세스 시작 확인이며 실제 Qwen 모델 응답이나 기관 문서 승인 증거는 아니다.

### 파일럿 이후 결정할 승인 위험 정책

기존 원클릭 승인과 위험 표시 정책을 유지한다. 다음 기록은 실제 참여자가 수행한
결과로만 채운다. 테스트 대역·자동 클릭 결과를 사용자 파일럿 수에 합산하지 않는다.

| 익명 참여자 | 다음 행동 파악 시간 | 작성·전처리·승인·질문 완료 | 위험 경고 이해 | 막힌 단계·복구 결과 |
| --- | --- | --- | --- | --- |
| P01 | 미실시 | 미실시 | 미실시 | 미실시 |
| P02 | 미실시 | 미실시 | 미실시 | 미실시 |
| P03 | 미실시 | 미실시 | 미실시 | 미실시 |
| P04 | 미실시 | 미실시 | 미실시 | 미실시 |
| P05 | 미실시 | 미실시 | 미실시 | 미실시 |

진행자는 공개 합성 문서를 사용하고 기관명·실명·키·원본 파일은 기록하지 않는다.
각 단계의 화면 표시부터 참여자가 다음 행동을 짚을 때까지 시간을 재고, 10초 이내
비율이 80% 이상인지 계산한다. OCR·표 검증 실패·`action_required` 각각의 경고를
알아본 비율과 성급한 승인 시도를 함께 기록한다. 담당자가 증거를 검토한 후에만
현행 유지 또는 위험 문서 추가 확인 정책을 결정하며, 결정 이유와 담당자·날짜를
별도 검토 기록에 남긴다.
