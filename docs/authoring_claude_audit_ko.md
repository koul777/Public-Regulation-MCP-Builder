# 초보자용 규정 작성 기능 Claude 감리 기록

기준일: 2026-08-30

## 감리 범위

Claude Code CLI `2.1.251`를 읽기 전용 `plan` 권한, high effort로 사용해 아래 범위를 감리했다.

- authoring 전용 API, service, repository, schema
- Streamlit 초보자 화면과 상태 전이 UX
- 공식 승인, approved-only 인덱싱, RAG/MCP 경로와의 분리
- optimistic concurrency, export integrity, purge 연계
- 4-eyes, self-freeze 제한, 감사 이벤트
- 패키징과 공개 릴리스 위생

## 실행 기록

- 1차 시도: 2026-08-30 14:57 KST
  - Claude 사용량 제한으로 중단
- 2차 실행: 2026-08-30 16:50 KST
  - 정상 완료
  - Claude가 authoring 관련 테스트 144건을 직접 확인했고, Windows symlink 2건은 환경 제약으로 skip 상태였다.
- 3차 재감리: 2026-08-30 19:59 KST probe 실패
  - 결과: `You've hit your session limit · resets 9:50pm (Asia/Seoul)`
  - 후속: 사용량 재설정 뒤 아래 4차 재감리로 완료
  - 목적: 2차 실행 뒤 반영된 HTTP 4-eyes 실증, FastAPI/Streamlit 최신 호환성, 문서/패키징 마감 상태 재확인
- 4차 최종 재감리: 2026-08-30 21:50~21:55 KST
  - 정상 완료
  - 읽기 전용 `plan`, high effort로 현재 작업트리의 소스·테스트·두 감리 문서를 독립 대조했다.
  - 저장소 파일은 수정하지 않았고, 자동 테스트 수치는 재실행하지 않은 채 실제 assertion을 읽어 대표 근거를 확인했다.
  - 결론: 새 High/Medium/Low 실행 가능 코드 결함 없음

## 확인된 결과

Claude는 2차 실행 시점 기준으로 High/Medium 신규 결함을 추가 발견하지 못했다. 검토 범위 안에서 아래 경계가 유지되는 것을 확인했다.

- 초안 작성 기능은 `Document`, `Chunk`, approved-only 인덱싱, RAG/MCP 공개를 직접 호출하지 않는다.
- tenant/profile/project 범위가 API, service, repository, UI 상태에 일관되게 반영된다.
- `expected_revision`과 section base revision으로 stale write를 차단한다.
- freeze snapshot, semantic SHA-256, replay 검증으로 export 무결성을 보호한다.
- 로컬 self-freeze는 명시적 동의와 사유가 있을 때만 허용되고 `training_only` 표식을 남긴다.
- audit event가 상태 전이와 사유를 남기고, authoring 전용 422/413 경로는 본문을 반사하지 않는다.

4차 최종 재감리는 위 결과에 더해 요청한 10개 점검 영역을 모두 실제 코드와 테스트에서
재확인했다. export 경로 계산이 service와 repository에 각각 있는 점은 경로 이탈 방어와
byte-for-byte replay 검증을 겹쳐 두는 방어적 중복으로 보았고, Markdown 조문 본문의 여러
줄 보존도 경계 문구를 파일 첫 줄에서 별도 검증하므로 결함으로 판정하지 않았다.

## Finding과 처리 상태

### F1. Low: 사용되지 않는 이벤트 사유 헬퍼

- 근거: `latest_change_request_reason()`가 실제 화면 경로와 어긋난 표현을 반환할 수 있었다.
- 처리: 헬퍼와 비현실적인 원문 사유 event fixture를 제거했다. 절대 경로가 포함된
  수정 요청 문구의 redaction 테스트와 실제 화면 표시 테스트는 유지했다.
- 위험 감소: 나중에 잘못된 수정 요청 사유가 재노출될 가능성을 제거했다.

### F2. 운영 증거 필요: actor/tenant 결합과 계정 분리

- 근거: 4-eyes는 구조화 token의 actor/tenant 바인딩에 의존한다.
- 확인된 사실: 최신 코드에서는 actor/tenant 정보가 없는 token 구성이 fail closed로 500 처리되며, 보호 경로 위조 요청은 403으로 거부된다.
- 남은 검증: 실제 운영 토큰 발급 기록과 작성자·확인자 계정 분리 증거는 아직 코드 밖에서 수집해야 한다.
- 위험 감소: 코드 경계는 확인됐지만 운영 절차 증거가 없으므로 일반 배포 GO 근거로는 부족하다.

### F3. 출시 게이트 미충족: 인간 사용성 파일럿

- 근거: 자동화 AppTest는 통과했지만 초보자 5명 이상 대상 실사용 파일럿이 아직 없다.
- 처리 상태: [초보자 사용성 검증 계획](authoring_beginner_usability_test_plan_ko.md)을 작성해 과업, 기준, 관찰 양식을 고정했다.
- 남은 검증: 실제 참여자 기록과 GO/NO-GO 메모 필요.

### F4. 환경 기반 검증 공백: Windows symlink 방어 테스트 skip

- 근거: 현재 Windows 계정은 symlink 생성 권한이 없어 관련 공격 테스트 2건이 skip된다.
- 처리 상태: 코드 방어는 존재하며, POSIX 또는 symlink 가능한 Windows CI에서 재실행하도록 릴리스 게이트에 남겼다.
- 남은 검증: CI 실행 로그 필요.

## 현재 판정

현재 판정은 `CONDITIONAL GO`다.

- 허용 범위: 로컬 1인 연습, 제한된 내부 초안 작성, 검토용 export-only 사용
- 비허용 범위: 일반 사용자 출시, 보호 환경 실무 배포, 공식 승인 또는 인덱싱 경로와의 결합

## 4차 재감리 확인 결과

1. actor-bound structured token의 실제 HTTP 테스트에서 위조 actor와 작성자 self-freeze는
   403, 같은 tenant의 별도 확인자 동결은 200으로 검증된다.
2. FastAPI `0.141.1`, Streamlit `1.62.0`, Pydantic `2.13.5` 호환 보강 뒤 공개
   template/lint/OpenAPI surface가 유지된다.
3. export cache identity, 정확한 frozen revision replay, 두 종류 SHA-256, Markdown 첫 줄
   경계와 한 줄 메타데이터 정규화가 구현·테스트되어 있다.
4. staging intent recovery, manifestless cleanup, tombstone/final sweep, export/purge 공유
   lock과 변조·누락·non-UTF-8 export 거절 경로가 구현·테스트되어 있다.
5. 상대 문서 링크와 MANIFEST/sdist 계약, 공개 hygiene, clean Python 3.11 설치 근거가
   준비되어 있다.

Claude의 최종 배포 판정은 `LOCAL TRAINING`을 포함한 `LIMITED INTERNAL DRAFT`다.
`PROTECTED DEPLOYMENT`는 실제 계정 분리·actor/tenant 발급 증거와 symlink CI가,
`GENERAL RELEASE`는 여기에 초보자 5명 이상 인간 파일럿이 추가로 필요하다.
