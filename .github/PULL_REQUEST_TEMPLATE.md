## 변경 요약

- 무엇을, 왜 바꾸는지 적어 주세요.

## 검증

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m build --sdist --wheel`
- [ ] `python scripts/audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan`

## 파싱·전처리·MCP 연결 변경 계약

파서·전처리, MCP 클라이언트 번들, 로컬/원격 전송, 연결 상태 판정, 회귀 기준값 또는 이 보호 장치를 바꾸는 PR은 아래 다섯 항목을 모두 실제 내용으로 채워야 합니다. 해당 변경이 없으면 기본 문구를 그대로 두어도 됩니다.

### 변경 요약

<!-- preprocessing-guard:summary -->
작성 필요
<!-- /preprocessing-guard:summary -->

### 영향받는 문서 형식·처리 단계·MCP 클라이언트

<!-- preprocessing-guard:affected-formats -->
작성 필요
<!-- /preprocessing-guard:affected-formats -->

### 유지해야 할 불변조건

승인 전 색인 금지, tenant 격리, 불확실 파싱의 검수 플래그, 원문/경로 비노출, 플러그인 등록과 대화 첨부 분리, `initialize/tools/list/get_index_status` 완료 전 연결 판정 금지 등 이번 변경에서 지킨 조건을 적어 주세요.

<!-- preprocessing-guard:invariants -->
작성 필요
<!-- /preprocessing-guard:invariants -->

### 추가·수정한 회귀 테스트와 결과

<!-- preprocessing-guard:regression-evidence -->
작성 필요
<!-- /preprocessing-guard:regression-evidence -->

### 골든/기대값 변경

기준값을 바꿨다면 변경 전후 수치, 이유, 사람이 확인한 근거를 적으세요. 기준값 변경이 없으면 `없음`이라고 적습니다.

<!-- preprocessing-guard:baseline-change -->
없음
<!-- /preprocessing-guard:baseline-change -->

## 공개·보안 영향

- [ ] 원본 기관 문서, runtime 산출물, 비밀, 로컬 경로, 기관 식별자를 추가하지 않았습니다.
- [ ] 미승인 chunk가 Vector/RAG/MCP에 노출되지 않는 것을 확인했습니다.
- [ ] 파싱·전처리·MCP 연결 보호 변경이면 Code Owner 검토 후 `preprocessing-reviewed` 라벨을 받았습니다.
