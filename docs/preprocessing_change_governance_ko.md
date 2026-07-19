# 파싱·전처리·MCP 연결 변경 보호 하네스

이 저장소의 파싱·전처리 결과는 승인, Vector/RAG 색인, MCP 답변의 근거가 된다. MCP 연결 생성기와 상태 판정은 사용자가 실제로 도구를 쓸 수 있는지 결정한다. 따라서 핵심 로직, 회귀 기준값, 로컬/원격 MCP 연결 로직은 일반 문서 변경처럼 바로 기본 브랜치에 반영하지 않는다. 이 하네스는 변경 자체를 금지하지 않고, 영향·테스트·사람 검토가 확인된 변경만 PR을 통과시키는 fail-closed 계약이다.

## 보호 범위

- `app/parsers/`, `app/processors/`
- `app/core/pipeline.py`, `app/services/processing_service.py`
- 파싱·구조·품질·chunk 관련 schema
- 배치 전처리와 회귀 판정에 관여하는 핵심 script
- `app/mcp_server/`의 도구·전송·HTTP 보안 경계
- MCP 번들 생성, 연결 readiness, stdio/Streamable HTTP smoke, 서버 실행 script
- `frontend/streamlit_app.py`의 MCP 프로필·연결 대상·상태 안내
- `tests/fixtures/regression/`과 품질 기준값
- 이 정책을 시행하는 workflow, guard script, CODEOWNERS, PR template

보호 파일이 바뀌면 `preprocessing-change-policy`는 다음 조건을 모두 요구한다.

1. 파서·전처리·MCP 연결 또는 기준값 변경에는 관련 `unittest` 파일도 같은 PR에서 추가하거나 수정한다. 삭제된 테스트는 증거로 인정하지 않는다.
2. PR template의 변경 요약, 영향 형식, 불변조건, 회귀 근거, 기준값 변경 항목을 실제 내용으로 채운다.
3. 골든/기대값을 바꾸면 단순히 새 결과에 맞추지 말고 변경 전후 수치와 사람 검수 근거를 기록한다.
4. Code Owner가 증거를 검토한 뒤 `preprocessing-reviewed` 라벨을 붙인다.
5. `preprocessing-regression`이 파서·구조화·표·chunk·품질·파이프라인뿐 아니라 MCP 번들, 실제 stdio/Streamable HTTP, ChatGPT/Claude 설치 경로, HTTP 보안 회귀 테스트와 package build, 공개 소스 위생 검사를 통과한다.

`pull_request_target` 정책 workflow는 PR head를 checkout하거나 실행하지 않는다. 기본 브랜치의 신뢰된 guard만 실행하고, GitHub API에서 변경 파일명만 읽는다. PR 코드는 권한이 제한된 별도 `pull_request` workflow에서 테스트한다.

## GitHub Ruleset 필수 설정

workflow 파일만 추가하면 직접 push를 막을 수 없다. 저장소 관리자 권한으로 `Settings > Rules > Rulesets`에서 `main` 대상 branch ruleset을 만들고 다음을 활성화한다.

먼저 `Settings > Issues > Labels`에서 이름이 정확히 `preprocessing-reviewed`인 라벨을 만든다. 이 라벨은 테스트 통과 표시가 아니라 Code Owner가 PR 본문과 회귀 증거를 확인했다는 명시적 승인 표시로만 사용한다.

- Require a pull request before merging
- Required approvals: 1명 이상
- Require review from Code Owners
- Dismiss stale pull request approvals when new commits are pushed
- Require approval of the most recent reviewable push
- Require conversation resolution before merging
- Require status checks to pass: `preprocessing-change-policy`, `preprocessing-regression`
- Block force pushes와 branch deletion
- 관리자와 자동화 계정의 bypass를 허용하지 않거나, 긴급 운영 계정만 명시적으로 제한

새 workflow를 기본 브랜치에 처음 넣는 bootstrap PR에서는 `pull_request_target` 검사가 아직 존재하지 않을 수 있다. 이 최초 PR은 소유자가 파일과 테스트 결과를 직접 확인해 병합하고, 즉시 위 두 status check를 required로 지정한다. 그 뒤 workflow나 guard를 바꾸는 PR도 같은 보호 범위에 들어간다.

## 로컬 사전 확인

기본 브랜치와 현재 HEAD 사이의 보호 변경을 확인하려면 다음처럼 실행한다.

```powershell
reg-rag-preprocessing-change-guard --base-ref origin/main --pr-body-file .tmp/pr-body.md --label preprocessing-reviewed
```

라벨은 로컬 검사를 위한 입력일 뿐이며 GitHub의 실제 Code Owner 승인과 라벨을 대신하지 않는다. 전체 검증은 저장소 루트에서 다음 명령으로 실행한다.

```powershell
python -m unittest discover -s tests -v
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

## 불변조건

- 전처리 성공은 보안 승인을 뜻하지 않는다.
- 불확실한 구조·표·OCR 결과는 review flag를 유지한다.
- 승인되지 않은 chunk는 로컬 Vector/RAG store에 색인하지 않는다.
- tenant, 승인 journal, audit log 경계를 약화하지 않는다.
- 원본 문서, runtime report, 비밀, 로컬 경로, 기관 식별자를 PR 증거나 공개 artifact에 넣지 않는다.
- 플러그인 등록과 현재 대화의 도구 첨부를 같은 상태로 표시하지 않는다.
- 프로세스 실행만으로 연결 완료를 표시하지 않는다.
- 실제 `initialize`, `tools/list`, `get_index_status`가 성공하기 전에는 `end_to_end_verified`를 참으로 만들지 않는다.
- ChatGPT Desktop companion JSON과 상태 JSON/TOML은 BOM 없는 UTF-8 계약을 지키며, 검증기가 `utf-8-sig`로 결함을 숨기지 않는다.
- 플러그인 설치 명령 성공만으로 `plugin_registered`를 참으로 만들지 않고 manifest 검증과 `codex plugin list --json`의 정확한 cachebuster 버전·활성 상태·공급 마켓플레이스 경로를 별도로 확인한다.
- ChatGPT 로컬 플러그인과 원격 HTTPS MCP를 서로 다른 프로필로 유지한다.
