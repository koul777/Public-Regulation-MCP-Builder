# Public Operator Quickstart

이 문서는 source-only 공개 저장소에서 합성 샘플과 로컬 개발 환경으로 파이프라인을 확인하는 공개용 quickstart다. 기관 원문, 운영 runtime, 내부 handoff 자료는 이 문서의 입력으로 사용하지 않는다.

> 이 안내의 초보자 첫 선택 화면은 현재 소스 기준이다. `releases/latest` Windows 실행판에는
> 다음 portable 릴리스가 게시되기 전까지 보이지 않을 수 있으므로, 화면이 다르면 소스로
> 실행하거나 새 portable 릴리스와 fresh-Windows 검증이 끝난 뒤 다운로드한다.

## Scope

- Use synthetic or explicitly redistributable samples only.
- Streamlit is a local-only operator console, not a protected shared-deployment UI.
- Preprocessing output is a review preview. Official RAG/MCP indexing starts only after human review and approval.

## 처음 사용자 화면 흐름

아래 `Local Start`로 Streamlit 운영 화면을 연 뒤, 첫 화면에서 먼저 **최종 사용 방법**을
고른다. **이 PC의 로컬 Qwen 챗봇으로 바로 질문**은 빌더와 별도로 실행되는 localhost
챗봇에서 규정에 묻는 경로이고, **ChatGPT·Claude·Codex에 MCP로 연결**은 외부 AI 앱에 같은 규정 도구를
등록하는 경로다. 둘은 같은 승인된 로컬 RAG 색인을 읽으므로 별도 RAG를 만들 필요가 없다.
선택은 왼쪽 메뉴의 **Qwen 또는 MCP 선택**에서 언제든 바꿀 수 있으며, 어느 쪽을 골라도
다른 쪽 기능이나 승인 데이터는 삭제되지 않는다.

그 뒤 공통 ①~③을 완료하고, 선택에 맞는 ④를 진행한다. 이 절은 약 5분 안에 읽을 수 있는
화면 안내이며 실제 전처리 시간은 파일 크기와 형식에 따라 더 걸릴 수 있다.

1. `① 문서 올려서 전처리`에서 **문서 업로드**로 재배포 가능한 합성 규정 파일 하나를
   선택한 뒤 **전처리 시작**을 누르고 **전처리 완료**를 확인한다. 기본은 빠른 구조
   전처리이며, 외부 AI 검수는 왼쪽 사이드바 **AI 검수**에서 켜고 API 키를 저장해 둔
   경우에만 실행된다. 켜 두면 이후 전처리에 자동으로 함께 실행되므로 이 화면에서
   따로 고르지 않는다.
2. `② 결과 확인`의 **정리된 내용(청크)** 탭에서 원문과 전처리 결과를 좌우로 비교하고,
   이어서 **이슈** 탭의 확인 필요 항목을 읽는다. 품질 통과 표시는 자동 승인이 아니며,
   결과가 원문과 다르면 승인 단계로 넘기지 않는다.
3. `③ 검수하고 승인`은 세 단계뿐이다. **1단계** 규정 디렉터리에서 **규정 열기**를 눌러
   검수할 규정을 연다. **2단계** 규정을 위에서 아래로 스크롤하며 조항마다 **원본·전처리본·AI
   검수본**을 비교한다. AI 추가 검수를 켜고 전처리했으면 AI 검수본이, 켜지 않았으면
   전처리본이 ✅ 최종본이며, 제안이 틀리면 그 칸을 직접 고치면 된다. 확인 버튼을 따로
   누를 필요는 없다. **3단계** **이 규정 최종 확정 · 승인하고 색인**을 누르면 고친 내용이
   자동으로 반영되고 승인·색인까지 끝난다. 제외할 조항만 목록에서 고르고 사유와 확인을
   마친 뒤 **선택한 조항 반려**를 누른다. 규정을 여러 개 선택했다면 3단계 아래
   **전체 규정 확인**을 켜서 선택한 규정의 미승인 조항을 규정 순서대로 한 화면에서
   비교·수정하고 **전체 규정 최종 확정**으로 한 번에 승인·색인할 수 있다.
   **승인·색인 완료**가 함께 표시되거나 명시 반려로 처리 방향이 결정돼야 다음 단계로
   이동한다.
4. ④는 첫 선택에 따라 나뉜다. **로컬 Qwen**을 골랐다면 `④ Qwen 규정 챗봇·AI 연결`에서
   **독립 Qwen 챗봇 실행**을 누른다. 새 브라우저 창에서 아래 여섯 단계를 따른다.

   1. 독립 localhost 챗봇이 열렸는지 확인한다.
   2. 기관을 선택하고 규정별 승인·색인 준비 상태를 읽는다.
   3. **질문 가능** 규정 하나를 선택한다. 전처리만 끝난 규정은 목록에 보여도 질문할 수 없다.
   4. **Ollama · qwen3:8b 연결 확인**을 누른다. 첫 확인은 모델 적재로 수십 초 걸릴 수 있다.
   5. 질문을 입력하고 검색·문맥 구성·Qwen3 8B 답변·인용 검증의 진행률과 경과 시간을 본다.
   6. 답변 아래 근거 조문의 규정명·조문 번호·페이지·승인 인용을 원문과 함께 확인한다.

   기본값에서는 **Qwen3 4B 정밀 근거 감사가 꺼져 있다.** 기본 빠른 경로는 승인
   BM25/lexical 검색 → Qwen3 8B 짧은 답변 → 결정론적 근거 ID 검증을 실행한다. 의미 단위의
   추가 검토가 필요할 때만 토글을 켠다. 켜면 8B 뒤 4B 감사와 모델 교체 시간이 추가된다.
   빌더를 닫고 챗봇만 다시 열려면 저장소 루트에서 `RUN_QWEN_CHAT.bat`를 실행한다.

   **MCP**를 골랐다면 메뉴가 `④ MCP 생성·외부 AI 연결`로 보인다. 사용할 외부 AI 앱과
   연결 방식(같은 PC 또는 원격)을 고르고 **생성할 MCP 이름 (필수 입력)**에 이름을
   입력한 뒤 **MCP로 쓸 파일 묶음 만들기**를 누른다. 화면 아래의 앱별 등록·연결 진단을
   마치고 실제 AI 대화에서 `list_regulations` → `search` → `fetch`가 승인 원문과 출처를
   반환하는지 확인한다. MCP를 골라도 독립 로컬 Qwen 챗봇은 그대로 실행할 수 있다.

규정별 파일을 여러 개 올려도 되고 여러 규정을 합친 규정집 한 개를 올려도 된다. 규정 제목과
조문 경계가 분명하면 두 방식은 MCP에서 같은 규정 목록·목차·조문을 만든다. 계층 색인은 ④에서
묶음을 만들 때 자동 생성된다. 합본에서 별표·붙임 다음의 같은 페이지에 새 규정을 이어 붙일
때는 번호가 있는 목차 또는 새 페이지의 편·장 제목처럼 경계를 확인할 수 있는 표지를 둔다.
경계가 불명확하면 일부만 잘못 내보내지 않고 생성이 멈추므로 `② 결과 확인`에서 원문 경계를
확인한 뒤 파일을 나누거나 표지를 보완해 다시 전처리한다.

현재 소스 또는 해당 기능이 포함된 portable 릴리스의 첫 화면에서 **초보자 안내 시작**을 선택한 뒤 기관을 만들거나 선택하면
현재 눌러야 할 곳에 빨간 테두리·화살표·단계 번호가 나타난다. 번호는 사이드바의
**세부 확인 절차** 목록과 같은 `3-2` 형식이며, 목록에서는 끝난 절차가 ✅, 지금 할 차례가
👉로 표시된다. 아직 준비가 안 된 화면에서는 번호 대신 `!` 표시로 먼저 끝내야 할 준비
작업을 알려 준다. 이 빨간 테두리는 오류가
아니라 현재 안내 대상 표시다. **이전 단계**·**다음 단계**는 설명 위치만 바꾸며,
승인이나 색인을 자동 실행하지 않는다. **안내 건너뛰기**로 언제든 끌 수 있고
**처음부터 다시 보기**로 다시 시작할 수 있다. 안내가 필요 없으면 첫 선택 화면에서
**일반 모드로 계속**을 누른다. 기관을 이미 선택했다면 사이드바에서 **초보자 안내 모드**를 켤 수 있다.

첫 업로드 전에 `① 문서 올려서 전처리`의 **공식 MCP 품질 준비 확인**을 본다. 준비되면
**Kordoc 사용 가능**, 없으면 **Kordoc 설치·검증 시작**이 표시된다. MCP 묶음 생성에
필요한 Kordoc이 없으면 화면은 Node.js/npm을 이용한 사용자 전역 설치임을 먼저 설명한다.
설치에 동의할 때만 **Kordoc 설치·검증 시작**을 누른다. 설치 없이 전처리할 수는 있지만,
설치 후에는 앱을 완전히 종료하고 `START_HERE.bat`으로 다시 실행해 **Kordoc 사용 가능**을
확인한다. 설치 없이 먼저 처리했다면 나중에 새 초안을 전처리하고 다시 검수·승인해야 한다.
재전처리는 MCP 화면에 들어가기만 해서는 시작되지 않고, 사용자가 **안전 재전처리** 버튼을
직접 눌렀을 때만 시작된다.

마지막 단계의 **AI 앱에서 search와 fetch 도구 호출이 성공한 것을 확인했습니다**는
MCP 이름 입력이나 파일 생성만으로 선택하지 않는다. 화면 아래의 앱별 등록·연결 진단을 먼저 마치고,
실제 AI 대화에서 `list_regulations`로 승인된 목록을 확인한 뒤 두 도구가 승인 원문과 출처를
반환한 것을 사람이 확인한 뒤 선택한다. 문서 범위·저장 방식이 다른 묶음을 새로 만들면
새 묶음에서 다시 확인해야 완료로 표시된다.

MCP 경로에서 Codex·Claude는 화면이 추천하는 같은 PC용 연결을 사용할 수 있다. ChatGPT는 로컬 MCP에
직접 연결하지 않으므로 ChatGPT 웹의 원격 HTTPS MCP 또는 OpenAI Secure MCP Tunnel을
사용한다. STDIO·HTTPS·JSON·TOML·Vercel 설정은 기본 흐름에 필요하지 않으며, 원격
배포나 앱별 수동 등록이 필요할 때만
[README의 방법 A~E](../README.md#2-다섯-방법-중-하나-선택하기)를 확인한다.

## Local Start

처음 사용하는 운영 화면은 저장소 루트에서 다음 한 줄로 시작한다. 이 실행기는 Python
환경을 준비하고 사용할 수 있는 로컬 포트를 찾아 Streamlit을 연다.

```powershell
.\START_HERE.bat
```

API 검증이 필요할 때만 운영 화면과 **별도의 PowerShell 창**에서 다음 명령을 실행한다.
`API_AUTH_REQUIRED=true`는 FastAPI 예시용이며 로컬 Streamlit 운영 화면과 함께 설정하지
않는다.

```powershell
$env:APP_ENV="development"
$env:API_AUTH_REQUIRED="true"
$env:API_AUTH_TOKEN="replace-with-a-local-development-token"
$env:DATA_DIR=".\data"
uvicorn app.main:app --reload
```

Example authenticated request:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/documents/process `
  -H "Authorization: Bearer $env:API_AUTH_TOKEN" `
  -H "X-Tenant-Id: tenant-demo" `
  -H "X-Actor: local-operator" `
  -H "Content-Type: application/json" `
  -d '{"document_id":"synthetic-document-001","file_path":"data/uploads/synthetic-document-001.md"}'
```

The response should expose a `document_id`, `job_id`, `status=completed`, and `quality.passed=true` only for the local synthetic fixture path.

## Public Validation

```powershell
python -m unittest discover -s tests -q
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
python scripts\run_fresh_clone_rehearsal.py --mode public --dry-run --fail-on-issue
python scripts\run_release_harness.py --mode public --keep-going
python scripts\run_public_release_gate.py --include-untracked --execute-harness --fail-on-blocked
```

For release evidence, use the public audit, cleanup plan, release gate, approval evidence, review-batch evidence, and MCP release evidence tools. Keep generated reports outside the tracked source tree.

## Official Chain

The official path is:

```text
source file -> preprocessing -> quality flags -> human review -> approval journal
-> approved local regulation DB/vector index -> RAG/MCP tools
```

Unreviewed results must remain `UNREVIEWED_PREVIEW` or `UNREVIEWED_POC_REVIEW`. They must not be treated as official approved vectors. Reindex approved chunks only after review flags are acknowledged, review-batch decisions are validated, and release evidence is regenerated.

## Excluded From Public Use

- institution documents and downloaded HWP/PDF originals
- runtime exports, vector databases, approval journals, and internal evidence
- real tokens, local absolute paths, and institution-derived identifiers
- claims of production deployment, SSO, ChatGPT/Claude endpoint availability, or product readiness
