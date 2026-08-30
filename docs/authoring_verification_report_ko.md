# 초보자용 규정 작성 P0 구현·감리·검증 보고서

기준일: 2026-08-30

## 결론

초보자가 외부 생성형 AI 없이 로컬 화면에서 규정 초안을 시작하고, 안내 문구와 체크리스트를 따라 Markdown 또는 JSON 초안 패키지까지 내보낼 수 있는 P0를 구현했다. 이 기능은 기존 공식 승인, approved-only 인덱싱, RAG, MCP 공개 경로와 분리되어 있으며, 모든 출력물에 `공식 승인 아님` 경계를 넣는다.

현재 판정은 `CONDITIONAL GO`다. 로컬 1인 연습 용도와 내부 검토용 초안 생성까지는 근거가 충분하지만, 일반 배포 또는 보호 환경 운영 GO로 보려면 아래 3가지가 추가로 필요하다.

Claude 4차 독립 재감리의 배포 용어로는 `LOCAL TRAINING`을 포함한
`LIMITED INTERNAL DRAFT`이며, 새 High/Medium/Low 실행 가능 코드 결함은 없었다.

1. 초보자 5명 이상 대상 실제 사용성 파일럿 통과
2. 보호 배포 환경에서 작성자·확인자 계정 분리와 actor/tenant 결합 운영 증거 확보
3. POSIX 또는 symlink 가능한 Windows CI에서 symlink 방어 테스트 통과

## 분석 범위

- `app/api/routes_authoring.py`
- `app/api/authoring_request_guard.py`
- `app/services/authoring_service.py`
- `app/services/authoring_lint_service.py`
- `app/services/authoring_safety_service.py`
- `app/services/authoring_template_service.py`
- `app/storage/authoring_repository.py`
- `app/schemas/authoring.py`
- `app/schemas/authoring_integrity.py`
- `frontend/authoring_page.py`
- `frontend/streamlit_app.py`
- `docs/authoring_*`
- `pyproject.toml`, `MANIFEST.in`, `README.md`

## 실제 AI Agent 오케스트레이션

| 역할 | 맡긴 범위 | 반영 결과 |
|---|---|---|
| Backend/API agent | authoring route, service, 저장소, export/purge 경쟁 조건 | 초기 commit 복구, export와 purge의 공유 lock·tombstone·최종 sweep 보강 |
| Profile/UI agent | tenant/profile 전환, Streamlit 상태와 초보자 다음 행동 | 기관 전환 dirty guard, 상호 배타적 결정 화면, revision 충돌 복구 보강 |
| Delta review agent | 최신 변경의 회귀·공개 surface 재검토 | Markdown 여러 줄 메타데이터와 공개 template/lint route 계약 보강 |
| UX/docs agent | 로컬 1인 연습과 보호된 2인 절차의 혼동 점검 | 문서 경로 분리, 역할 중립 문구, 파일럿 자료 보강 |
| Release evidence agent | manifest, sdist 링크, build와 검증 보고서 | 새 문서 10종 패키징, 상대 링크 계약, 릴리스 근거 정리 |
| Claude independent audit | 공식 경계, 4-eyes, 무결성, 공개 위생 | Low 1건 수정, 운영·인간 검증 조건 3건을 출시 게이트로 유지 |

마지막 별도 security worker는 실행 중 응답 지연으로 중단했으며, 그 역할은 이미 완료된
보안 전담 검토, root의 보안 린트·경쟁 조건 검사와 Claude 독립 감리로 교차 확인했다.
중단된 실행을 완료된 감리로 계산하지 않았다.

## 초보자용 시스템 개선 결과

### 사용자 흐름

1. 기관, 신규 제정·일부 개정·전부 개정, 템플릿을 선택한다.
2. 규정명, 목적, 적용 범위, 근거, 담당 부서, 시행 예정일 같은 기본정보를 입력한다.
3. 서버가 조문 골격과 안내 문구를 고정한 상태로 본문 편집을 돕는다.
4. 작성 검사와 확인 목록 저장 뒤 내용 확인 요청, 수정 요청, 내용 동결, 다시 열기를 상태 전이 규칙 안에서 처리한다.
5. 최종 결과는 Markdown 또는 JSON 초안 패키지로만 내보내고 기존 승인 경로로 자동 전송하지 않는다.

### 초보자 친화성 보강

- 빠른 시작 문서 첫머리에 `처음 보는 사람은 이것만 기억하세요` 요약을 추가해 시작점과 금지 경계를 먼저 읽게 했다.
- 화면의 기술 용어를 줄였다. 예를 들어 export 확인 캡션은 `내용 식별값` 대신 `내용 확인값(SHA-256)`으로, self-freeze 흔적은 내부 코드명보다 `연습용 초안 표시` 의미가 먼저 드러나게 정리했다.
- 로컬 1인 연습과 보호된 2인 실무 절차를 빠른 시작과 MVP 계약에서 별도 경로로 나누고, 로컬 수정 요청 상태는 특정 확인자가 행동한 것처럼 보이지 않는 중립 문구로 바꿨다.
- 검증 보고서와 롤아웃 계획서에도 현재 실제 화면 흐름인 `기관 → 신규/일부/전부 개정 → 템플릿 → 기본정보 → 검토/동결/export` 순서를 반영했다.

### 구현된 핵심 제어

| 위험 | 구현 제어 | 수집 가능한 증거 |
|---|---|---|
| 공식 승인 경로와 혼선 | 별도 schema/service/repository/API 분리, import/index/MCP bridge 부재, 모든 출력물에 `공식 승인 아님` 표기 | 공식 격리 테스트, 경계 문구 검증 |
| tenant/profile 혼선 | canonical profile ID, tenant/profile/project 범위 강제, 다른 범위는 404 또는 403 | route/service/repository 범위 테스트 |
| 초보자 화면의 덮어쓰기 | `expected_revision`, section base revision, stale 상태 명시 reload | state matrix, AppTest |
| 동일 UUID 교차 오염 | 편집 버퍼와 dirty 상태를 tenant/profile/project 기준으로 분리 | 동일 UUID 교차 tenant AppTest |
| 구조 위반 초안 저장 | 템플릿 버전, 조문 구조, 체크리스트 계약 고정 | adversarial service 테스트 |
| export 변조 또는 재생 공격 | frozen revision snapshot, semantic SHA-256, byte replay 검증 | export integrity 테스트 |
| purge 중 잔존 데이터 | tombstone 기반 purge, export/commit 공유 lock, 최종 sweep | purge 테스트 |
| audit trail 누락 | 사유 원문은 보호된 snapshot에만 두고, event에는 상태 전이와 제공 여부·해시만 기록 | repository event 테스트 |
| 민감 본문 반사 | authoring 전용 content-free 422/413 핸들러, 경로·바디 redaction | request/body limit 테스트 |

## 검증 결과

### 정상 경로

- 전체 회귀: `python -m unittest discover -s tests -v`
  - 3,614 passed, 16 skipped, 795.264초
- authoring 집중 회귀 (`python -m unittest discover -s tests -p "test_authoring*.py" -v`)
  - 108 passed, 2 skipped, 34.671초
- Streamlit authoring AppTest
  - 32 passed
- route + request body limit + Streamlit 현재 작업트리 회귀
  - 70 passed, 94.681초
- authoring route + Streamlit + request limit + purge 통합
  - 83 passed
- README/sdist/public hygiene 관련 회귀
  - 32 passed

### 실패 경로

- 보호 경로에서 self-freeze는 403으로 차단된다.
- 보호 경로에서 `X-Actor` 위조는 403으로 차단된다.
- content-free 422/413 응답은 본문을 반사하지 않으며, 선택형 감사 observer 저장이 실패해도 413 거절은 유지된다.
- tampered export, appended export, boundary 제거, non-UTF-8 export는 모두 거부된다.

### 통합 경계

- export/reopen/purge 동시성 핵심 테스트 6종을 10회 반복해 총 60회 통과했다.
- `PYTHONHASHSEED` 4개 시드에서 관련 회귀 260회가 통과했다.
- Python 3.11 fresh 환경에서 FastAPI `0.141.1`, Streamlit `1.62.0`, Pydantic `2.13.5` 조합으로 authoring 집중 104건과 통합 83건이 통과했다.
- `py_compile`, authoring 대상 `mypy`, 변경 범위 `ruff`, `ruff --select S`가 통과했다.
- 413 감사 실패-폐쇄 회귀 1건과 보안 린트 설명 주석·테스트 변수명을 보강한 뒤
  route·request body limit·Streamlit 70건, request body limit 단독 10건과 위 전체
  3,614건을 모두 다시 통과했다.
- 최신 branch coverage 실행은 authoring 관련 191건 통과(2건 skip, 221.561초)였고,
  authoring·요청 제한 경로 합산 85%(2,803 statements, 888 branches)였다.
  route 96%, request body limit 91%, service 87%, UI 88%다.
- in-app browser runtime을 사용할 수 없는 환경이라 브라우저 실조작 QA는 수행하지 못했고,
  대신 Streamlit AppTest와 문서 검증으로 대체했다. 인간 파일럿 필요성은 그대로 남는다.

## 빌드와 공개 릴리스 근거

- `python -m build --sdist --wheel` 성공
- fresh Python 3.11 설치 검증
  - wheel import 성공
  - sdist install/import 성공
  - `pip check` 성공
- `python scripts/audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan`
  - 문서 상대 링크 복구와 새 초보자 문서 manifest 포함 보강 뒤 재통과
- `python -m unittest tests.test_authoring_sdist_links tests.test_authoring_public_hygiene tests.test_package_manifest tests.test_mcp_quickconnect_docs -q`
  - 32 passed

산출물 자체에 자기 해시를 기록하면 문서를 반영한 재빌드 순간 값이 달라진다. 따라서
최종 wheel과 sdist의 SHA-256은 모든 문서 반영 뒤 수행하는 마지막 재빌드에서 계산하고,
빌드 밖의 최종 인계 기록과 밤샘 세션 로그에 고정한다.

## 감사 관점의 확인 사항

- 누가 무엇을 바꿨는지: 모든 상태 전이는 이벤트로 남고, freeze와 change request 사유가 저장된다.
- 어떤 승인 아래 바뀌었는지: 보호 경로는 구조화 token과 actor 검증을 요구하며, 로컬 self-freeze는 `training_only=true`로만 허용된다.
- 어떤 데이터가 남는지: 초안 아티팩트는 tenant/profile 범위 purge 대상이며, 공개 저장소에는 runtime data가 포함되지 않도록 테스트와 hygiene audit로 확인했다.
- 어떤 경계가 아직 운영 검증이 필요한지: 실제 운영 계정 발급, 인간 사용성 파일럿, symlink 가능한 CI는 아직 코드 외부 증거가 필요하다.

## 잔여 위험과 후속 조치

### 우선순위 높음

1. [초보자 사용성 검증 계획](authoring_beginner_usability_test_plan_ko.md) 기준으로 초보자 5명 이상 파일럿을 수행하고, 오인율과 완료율을 기록한다.
2. 보호 환경에서 같은 tenant에 결합된 서로 다른 작성자·확인자 actor의 구조화 token
   발급 로그와 실제 HTTP 검증 로그를 운영 증거로 남긴다.
3. POSIX 또는 symlink 가능한 Windows CI에서 export/purge root 이탈 방어 테스트를 실행한다.

### 우선순위 중간

1. P1 이상에서 AI 제안 기능을 붙일 경우, 제안과 채택을 공식 승인 경로와 완전히 분리한 별도 통제 모델로 설계한다.
2. human pilot과 운영 증거 수집이 끝나기 전까지는 이 기능을 로컬 실습 또는 제한된 내부 초안 작성으로만 안내한다.

## 관련 문서

- [초보자용 워크스페이스 롤아웃 계획](authoring_workspace_rollout_plan_ko.md)
- [초보자 후속 개선 백로그](authoring_beginner_improvement_backlog_ko.md)
- [초보자 사용성 검증 계획](authoring_beginner_usability_test_plan_ko.md)
- [초보자 파일럿 진행 스크립트](authoring_beginner_pilot_facilitator_script_ko.md)
- [GO/NO-GO 메모 템플릿](authoring_go_nogo_memo_template_ko.md)
- [초보자 빠른 시작](authoring_quickstart_ko.md)
- [보안 모델](authoring_security_model_ko.md)
- [Claude 감리 기록](authoring_claude_audit_ko.md)
