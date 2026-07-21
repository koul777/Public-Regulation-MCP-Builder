# MCP 빠른 연결 안내

> 미검수 PoC Review(`UNREVIEWED_POC_REVIEW`, legacy `UNREVIEWED_PREVIEW`)는 빠른 파싱, 검색, 품질, 연결 화면 확인용 격리 모드입니다. 공식 approved vector, release evidence, 기관 배포 handoff 산출물에는 기록하지 않으며, 공식 RAG/MCP 사용 전에는 사람 검토, 승인, index/reindex, MCP visibility audit을 다시 거칩니다.

한 줄 요약: 전처리만 끝난 자료는 연결하지 말고, 사람이 승인하고 인덱싱까지 끝난 런타임만 Claude, ChatGPT, 내부 AI에 연결합니다.

공식 연결 준비 상태는 append-only approval journal coverage까지 일치해야 합니다. `reg-rag-mcp-doctor --audit-index-visibility` 리포트의 `approval_provenance_coverage`와 `approval_journal_coverage`를 확인하고, journal coverage가 빠진 런타임은 클라이언트 연결을 막습니다.

연결 전 게이트: MCP 클라이언트 연결 산출물은 전처리, 사람 승인, approved chunk 인덱싱까지 끝난 런타임에서만 ready로 취급합니다. 승인만 있고 `Index approved chunks` 또는 `Reindex approved chunks`가 끝나지 않은 상태는 연결 전 단계입니다.

Kordoc 사전 점검: HWP/HWPX/PDF/DOCX가 선택 범위에 있으면 MCP 생성 화면이 저장된 Kordoc 증거(`status=parsed`, `parser=kordoc`)를 먼저 확인합니다. 현재 PC에 Kordoc을 설치해도 과거 `not_available` 결과 자체가 소급 변경되지는 않습니다. 대신 저장된 원본이 있는 단일 선택 문서는 기존 승인본·approval journal·vector를 보존한 새 draft로 세션당 한 번 자동 복제·재전처리합니다. 새 draft에서 `parsed/kordoc` 증거를 확인한 경우에만 선택 문서를 바꾸며, 이후 사람 검토·승인과 `Index approved chunks` 또는 `Reindex approved chunks`는 반드시 다시 실행해야 합니다. 저장된 원본이 없거나 Kordoc이 실패하면 기존 문서는 그대로 두고 bundle export를 계속 차단합니다.

Kordoc 명령이 없는 Windows 환경에서는 MCP 화면이 첫 진입 시 `INSTALL_KORDOC_KO.ps1`을 한 번 자동 실행해 설치·검증을 시도합니다. 이 스크립트는 portable ZIP과 wheel 설치본에 포함되며, 가상환경 전역 경로도 자동 탐색합니다. Node.js/npm이 없거나 설치가 실패하면 화면의 재시도 버튼 또는 portable ZIP의 설치 스크립트를 사용합니다. 설치가 확인되면 위의 새 draft 재전처리가 이어지고, 운영자는 결과 검토·승인·색인을 완료합니다.

미검수 프리뷰는 `UNREVIEWED_PREVIEW`로 분리합니다. 이 모드는 품질과 연결 UX를 빠르게 확인하기 위한 경고 상태이며, 정식 MCP handoff, 외부 AI 연결, 기관 업무 사용, release evidence에는 사용할 수 없습니다.

PR MCP Builder에서 전처리와 승인까지 끝낸 MCP를 Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop, ChatGPT 원격 MCP, ChatGPT 웹, Claude (HTTPS)에 빠르게 연결하기 위한 최소 절차입니다.

연결 상태는 다음처럼 분리합니다. 프로세스 실행이나 플러그인 등록만으로 연결 완료라고 표시하지 않습니다.

- `launcher_ready`: 생성 launcher가 존재함
- `process_started`: MCP 프로세스 또는 원격 세션이 실제로 시작됨
- `mcp_initialized`: MCP `initialize` 성공
- `tools_discovered`: MCP `tools/list` 성공 및 도구 발견
- `plugin_install_command_succeeded`: 플러그인 설치 명령 종료 코드 성공
- `plugin_manifest_validated`: companion JSON의 strict UTF-8-without-BOM 및 JSON 검증 성공
- `plugin_discoverable`: 설치된 selector가 `codex plugin list`에서 발견됨
- `plugin_registered`: manifest·설치 명령이 성공하고 `codex plugin list --json`의 활성 플러그인 버전과 로컬 마켓플레이스 경로가 현재 번들과 정확히 일치
- `direct_config_registered`: ChatGPT Desktop 로컬 direct MCP와 Codex CLI가 공유하는 사용자 설정 `~/.codex/config.toml`에 direct 항목 기록 성공. ChatGPT Desktop의 기본 사용자 경로는 여전히 `Settings > MCP servers`이며 Codex CLI 실행을 뜻하지 않음
- `direct_config_loader_verified`: `codex mcp get --json`이 현재 번들의 launcher와 data 경로를 정확히 해석함
- `direct_stdio_verified`: 생성 launcher를 통한 직접 stdio 프로토콜 검증 성공
- `desktop_tool_scan_verified`: ChatGPT Desktop 도구 scan에서 MCP 도구 노출 확인
- `conversation_attachment_verified`: 현재 대화에서 플러그인 도구 첨부 확인
- `conversation_attachment_unverified`: 제품 UI의 도구 노출 및 현재 대화의 실제 도구 호출이 아직 확인되지 않음
- `end_to_end_verified`: 해당 smoke 보고서에서 local/full은 `initialize`, `tools/list`, `get_index_status`; 외부 `chatgpt-data`는 `initialize`, `tools/list`, `search`, `fetch` 전송 계약이 모두 성공함. Desktop 도구 scan이나 현재 대화 첨부 완료를 뜻하지 않음

## 용어 정리

- MCP는 Claude Desktop 같은 생성형 AI가 로컬 규정 DB를 호출하는 연결 방식입니다.
- `search`는 질문과 관련된 승인 규정 조항을 찾는 도구입니다.
- `fetch`는 `search` 결과의 `id`로 원문 근거와 citation metadata를 가져오는 도구입니다.
- RAG/Vector DB는 MCP 안쪽에서 근거를 찾는 검색 엔진 역할을 합니다. 외부 AI에는 승인된 MCP 도구만 보입니다.

## 성능과 캐시 예열

MCP 서버는 시작할 때 승인 Vector DB, 승인 스냅샷, BM25 검색 인덱스, 대표 scoring 경로를 기본 예열할 수 있습니다. 다만 생성된 handoff 번들의 stdio/터널 스크립트는 Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop 등록이 느려지지 않도록 `--no-warm-cache`를 붙여 빠르게 시작합니다. 원인을 분리하거나 서버 상주형 HTTP 운영에서 첫 검색 속도를 더 중시할 때만 예열을 켭니다.

`python scripts\run_mcp_transport_smoke.py ... --out-json ...` 결과에서 볼 핵심 필드는 다음과 같습니다.

- `search_elapsed_ms`: MCP `search` 한 번의 실제 응답 시간
- `fetch_elapsed_ms`: 검색 결과 `id`로 원문 근거를 가져오는 시간
- `warm_search_elapsed_ms`: 같은 서버 프로세스에서 재검색했을 때의 응답 시간
- `full_profile_search_timing_ms`: vector load, approval snapshot, visibility filter, scoring, trace write 단계별 시간

Claude에서 smoke-test 문서만 보이면 연결보다 운영 데이터 상태를 먼저 봅니다. MCP 실행 `--data-dir`, `--tenant-id`, Streamlit의 `MCP-visible records`, `get_index_status` 결과가 전처리ㆍ승인ㆍ인덱싱한 런타임과 같은지 확인하고, 필요하면 `Reindex approved chunks`를 실행합니다. 연결 전에는 `reg-rag-mcp-index-visibility --data-dir <runtime> --tenant-id <tenant> --forbid-smoke-docs --require-indexed --fail-on-issue`로 로컬 런타임을 먼저 감사할 수 있습니다.

승인 worklist/review-batch SHA가 source artifact, approved vector metadata, approval journal 사이에서 어긋나면 직접 JSONL을 패치하지 말고 `reg-rag-approval-sha-drift-plan --publish-runtime-report reports\aks_mcp_publish_runtime_report.json --out-json reports\approval_sha_drift_repair_plan_current.json --out-md reports\approval_sha_drift_repair_plan_current.md`로 read-only 복구 순서를 먼저 산출합니다.

## 1. 연결 번들 생성

```powershell
reg-rag-mcp-config `
  --client-profile bundle `
  --server-name regulation_mcp `
  --tenant-id default `
  --host 0.0.0.0 `
  --public-url https://mcp.example.go.kr `
  --out-dir reports/mcp_connection_bundle `
  --zip-out reports/mcp_connection_bundle.zip `
  --include-wheel
```

`--public-url`은 ChatGPT 원격 MCP나 Claude (HTTPS)처럼 원격 HTTPS MCP가 필요한 클라이언트에만 필요합니다. Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop 로컬 direct MCP만 쓸 때는 생략합니다. Claude Code의 stdio 서버는 `--scope user`로 등록되므로 같은 사용자의 프로젝트 전체에서 보입니다. Streamlit 운영 화면에서는 MCP 영역의 `MCP로 쓸 파일 묶음 만들기` 버튼으로 같은 번들을 만들 수 있습니다.

Streamlit의 MCP 설정 JSON 다운로드와 `Write MCP setup bundle now`는 approved chunks, indexed status, MCP-visible records, stale vector count를 같은 기준으로 확인합니다. 준비 전 서버 명령은 draft command이며, 승인ㆍ인덱싱이 끝나기 전에는 클라이언트 연결용 산출물로 취급하지 않습니다.

생성되는 주요 파일:

번들의 `data/` 폴더는 설정 예시가 아니라 실제 approved runtime payload입니다. 포함 대상은 approved chunks, approved vectors, BM25 index, approval/indexing journal, `mcp_runtime_manifest.json`이며, raw `*_nodes.json`, `*_issues.json`, `*_quality.json` 전처리 산출물은 handoff zip에 포함하지 않습니다.

공급 ZIP은 생성 설정/플러그인, 승인 runtime data, 빌드된 wheel만 allowlist로 포함합니다. `.venv`, `venv`, `__pycache__`, 테스트·빌드 캐시와 로컬 runtime 부산물을 제외하며, 플러그인 companion JSON은 압축 직전에 strict UTF-8-without-BOM JSON으로 다시 검증합니다.

승인 전 단계에서는 `Approval worklist evidence`에서 `approval_review_batch_manifest`를 불러오고, 필요하면 `Review batch ID to load`로 실제 검토한 batch를 선택합니다. 선택 batch의 `approval_request_template.chunk_ids`만 `Approve selected review batch for RAG` 범위가 되며, `review_flags_acknowledged`는 자동으로 체크되지 않습니다. batch 승인 후 `Index approved chunks` 또는 `Reindex approved chunks`를 실행해야 MCP 연결 산출물이 준비 상태가 됩니다.

Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop용 로컬 stdio 설정은 `reg-rag-mcp-server`를 직접 실행하지 않고 번들 안의 `run_mcp_stdio_server.ps1` launcher를 실행합니다. `install_local_package.ps1`을 한 번 실행하면 선택한 Python과 MCP 명령 모듈 11개의 SHA-256 build identity가 `runtime_python.json` schema 2에 기록됩니다. 이후 launcher·doctor·연결 wizard는 이 marker를 PATH, `REG_RAG_PYTHON`, 저장소 checkout보다 먼저 사용하고, `PYTHONPATH`를 격리한 상태에서 현재 모듈 hash를 다시 확인합니다. 같은 Python 경로라도 wheel이나 소스가 바뀌었거나 marker가 손상되면 다른 runtime으로 조용히 fallback하지 않고 재설치를 요구합니다. marker가 아직 없는 설치 전 단계에서만 생성 당시 checkout, 명시적 `REG_RAG_PYTHON`, 마지막 호환 PATH 순서로 탐색합니다. 설치 프로세스 안에서는 선택한 Python의 `Scripts` 폴더를 PATH 맨 앞으로 재배치하고 각 console command의 출처도 확인합니다. Windows PowerShell 5.1에서 한글·공백 경로가 깨지지 않도록 `.ps1` 실행 스크립트만 UTF-8 BOM을 허용합니다. `plugin.json`, `.mcp.json`, `marketplace.json`, 상태 JSON과 TOML은 항상 BOM 없는 UTF-8이며, 선택형 ChatGPT Desktop 플러그인의 `.mcp.json`이 `EF BB BF`로 시작하면 smoke와 ZIP 추출 검증이 실패합니다.

GitHub private push 또는 배포 직전에는 release harness로 실제 runtime data를 다시 번들화하고, 생성된 번들 자체의 local stdio doctor와 transport smoke까지 확인합니다. 이 검사는 GitHub에서 받은 코드와 로컬 번들이 같은 방식으로 빠르게 연결되는지 보는 최소 회귀입니다.

```powershell
python scripts\run_release_harness.py `
  --mode mcp `
  --artifact-dir reports\overnight_runs\<run-id>\release\harness `
  --skip-tests --skip-build --skip-console-check --skip-mcp-smoke --skip-mcp-transport-smoke `
  --mcp-runtime-data-dir C:\mcp_connection_bundle\data `
  --mcp-bundle-profile-id <profile-id> `
  --tenant-id default `
  --mcp-min-visible-records 100 `
  --bundle-dir reports\mcp_connection_bundle_harness_local_check `
  --bundle-zip reports\mcp_connection_bundle_harness_local_check.zip `
  --out-json reports\overnight_runs\<run-id>\release\harness\release_harness_mcp_bundle_local_check.json `
  --keep-going
```

`--artifact-dir`를 지정하면 하네스의 기본 보고서, MCP 번들, zip, wheel/sdist 출력과 sdist rehearsal 입력이 모두 해당 디렉터리 아래로 재배치됩니다. 밤샘·장시간 검증에서는 run-id별 디렉터리를 반드시 사용하고, `--out-json`도 같은 run-id 아래에 둡니다.

현재 환경에 `python -m build`가 설치되어 있지 않으면 `--build-python <build-tool-venv>\Scripts\python.exe`로 빌드 전용 Python을 지정할 수 있습니다. 이 옵션은 package build 단계에만 적용됩니다.

동일 소스의 wheel과 sdist를 바이트 단위로 재현해야 할 때만 `--source-date-epoch <고정된 UTC 초>`를 추가합니다. 이 값은 build 환경의 `SOURCE_DATE_EPOCH`로 전달되며, build 직후 sdist 경로 안전성 검사와 gzip/tar 메타데이터 정규화를 거쳐 `sdist_normalization_harness.json`을 남깁니다. 재현성 비교에서는 두 build에 반드시 같은 epoch를 사용합니다.

하네스가 package build를 수행할 때는 기본적으로 clean Git tree 검사를 먼저 통과해야 합니다. 개발 중 dirty tree에서 구조만 검증하려면 `--allow-dirty-build`를 명시할 수 있지만, 그 결과물은 릴리스 산출물로 사용할 수 없습니다.

실 HWP/PDF parser 회귀 증적은 `reg-rag-real-parser-fixtures --fixture-root <curated-root>`로 별도 확인합니다. public 하네스에서는 이 검사가 필수이며, 데이터 없는 sdist에서 관련 unittest가 명시적으로 skip된 것만으로는 통과로 인정하지 않습니다.

- `README.md`, `README.ko.md`
- `connect_mcp_client.ps1`
- `MCP 사용 시작하기.txt`
- `CLAUDE_CODE_AGENT_CONNECT_PROMPT.md`
- `Claude Code에 연결하기.bat`
- `CODEX_AGENT_CONNECT_PROMPT.md`
- `Codex에 연결하기.bat`
- `Codex 플러그인 MCP 입력값.txt`
- `Claude Desktop에 연결하기.bat`
- `claude_desktop_config.json`
- `CHATGPT_DESKTOP_CONNECT_GUIDE.md`
- `ChatGPT Desktop에 연결하기.bat`
- `ChatGPT HTTPS에 연결하기.bat`
- `ChatGPT 보안 Tunnel에 연결하기.bat`
- `Claude HTTPS에 연결하기.bat`
- `설치 후 MCP 사용 방법 보기.bat`
- `연결 상태 확인하기.bat`
- `install_local_package.ps1`
- `codex_config_snippet.toml`
- `chatgpt_desktop_local_mcp.json`
- `chatgpt-desktop-local-plugin/.agents/plugins/marketplace.json`
- `chatgpt-desktop-local-plugin/plugins/<이름>/.codex-plugin/plugin.json`
- `chatgpt-desktop-local-plugin/plugins/<이름>/.mcp.json`
- `bundle_status.json`
- `run_mcp_stdio_server.ps1`
- `claude_code_add_stdio.ps1`
- `claude_code_add_http.ps1`
- `run_http_server.ps1`
- `run_chatgpt_data_server.ps1`
- `run_openai_secure_tunnel.ps1`
- `doctor_mcp_connection.ps1`
- `validate_client_config_smoke.ps1`
- `validate_chatgpt_remote_mcp.ps1`
- `chatgpt_connector.json`
- `claude_api_fragment.json`
- `data/mcp_runtime_manifest.json`
- `data/repository/*_chunks.json`
- `data/vector_db/<tenant>/approved_vectors.jsonl`
- `data/vector_db/<tenant>/bm25_index.json`

생성된 번들이 각 로컬 설정에서 바로 실행되는지 보려면 클라이언트 설정 파일 자체를 실행하는 smoke를 추가로 돌립니다. 이 검사는 Codex, Claude Desktop, ChatGPT Desktop 플러그인 `.mcp.json`을 읽어 실제 stdio MCP 프로세스를 띄운 뒤 `initialize`, `tools/list`, `get_index_status`, `search`, `fetch`를 호출합니다.
압축을 다른 폴더에 풀었을 때 생기는 오래된 절대경로 문제를 피하기 위해, 이 smoke 스크립트는 실행 전에 번들 안의 Codex/Claude 설정 파일을 현재 추출 폴더 기준으로 다시 씁니다.

```powershell
python scripts\run_mcp_client_config_smoke.py `
  --server-name regulation_mcp `
  --codex-config reports\mcp_connection_bundle\codex_config_snippet.toml `
  --claude-desktop-config reports\mcp_connection_bundle\claude_desktop_config.json `
  --plugin-mcp-config reports\mcp_connection_bundle\chatgpt-desktop-local-plugin\plugins\regulation-mcp\.mcp.json `
  --out-json reports\mcp_client_config_smoke_bundle.json `
  --fail-on-issue
```

## 2. 가장 빠른 연결

아래 순서는 프로그램의 연결 대상 표시 순서와 같습니다. Claude Code와 Codex CLI는 대상별 에이전트 연결 요청문을 사용하고, Claude Desktop은 전용 BAT를 사용합니다. ChatGPT Desktop은 프로그램 생성 결과의 `CHATGPT_DESKTOP_CONNECT_GUIDE.md`에 표시된 값을 `Settings > MCP servers > Add server`에 입력합니다. ZIP 원본의 `<PROGRAM_BUNDLE_DIR>`은 그대로 복사하지 않습니다.

- Claude Code: `CLAUDE_CODE_AGENT_CONNECT_PROMPT.md` 우선, `Claude Code에 연결하기.bat` 보조
- Codex CLI: `CODEX_AGENT_CONNECT_PROMPT.md` 우선, `Codex에 연결하기.bat` 보조
- Claude Desktop: `Claude Desktop에 연결하기.bat` 기본
- ChatGPT Desktop: `CHATGPT_DESKTOP_CONNECT_GUIDE.md`의 내장 MCP 서버 등록이 우선, `ChatGPT Desktop에 연결하기.bat` 보조
- ChatGPT 원격 MCP: `ChatGPT HTTPS에 연결하기.bat`로 승인된 공개 HTTPS `/mcp` 준비
- ChatGPT 웹: `ChatGPT 보안 Tunnel에 연결하기.bat`로 Secure MCP Tunnel 준비
- Claude (HTTPS MCP): Claude 앱은 HTTPS URL을 `Customize > Connectors`에 등록하고, Messages API는 `Claude HTTPS에 연결하기.bat`와 `claude_api_fragment.json` 사용

Claude Code와 Codex CLI는 설치·로더 검증 후, ChatGPT Desktop은 내장 설정 등록 후 해당 앱을 완전히 종료·재실행하고 새 대화 또는 task에서 다음처럼 호출합니다.

```text
먼저 /mcp로 aks_mcp가 연결됨으로 보이는지 확인한 뒤, aks_mcp MCP의 연결 상태와 사용 가능한 규정 도구를 보여줘.
```

Claude Desktop BAT는 `%APPDATA%\Claude\claude_desktop_config.json`을 백업·병합하고 설치된 사용자 설정으로 `initialize`·`tools/list`·`get_index_status`까지 직접 검증합니다. 이 성공은 Claude Desktop 자체 로더나 현재 대화의 도구 노출 성공을 뜻하지 않습니다. 앱을 완전히 종료·재실행한 뒤 새 대화에서 입력창의 `+` → Connectors에서 서버를 확인하고 MCP 이름을 포함해 실제 `get_index_status` 호출을 요청합니다. Claude Desktop에는 위 `/mcp` 지시를 적용하지 않습니다.

ChatGPT Desktop과 Codex CLI는 서로 다른 제품과 사용자 흐름입니다. ChatGPT Desktop의 기본 경로는 `Settings > MCP servers`이지만 현재 로컬 direct 설정은 Codex CLI와 사용자 파일 `~/.codex/config.toml`을 공유합니다. 따라서 같은 MCP 이름의 경로를 한쪽에서 바꾸면 다른 쪽에도 영향을 줄 수 있으며, 설정 기록 성공을 현재 Desktop 대화의 도구 노출 성공으로 간주하면 안 됩니다.

같은 이름으로 번들을 다시 생성하면 Claude Code와 Codex CLI는 대상별 연결 요청문을 다시 실행하고, Claude Desktop은 전용 BAT를 다시 실행합니다. ChatGPT Desktop은 새 안내 값을 기존 `Settings > MCP servers` 항목에 반영합니다. 새 번들은 현재 승인된 전체 청크를 다시 내보내므로 추가 규정과 개정판도 같은 `aks_mcp`에서 조회됩니다. 저장 폴더가 바뀌었다면 모든 로컬 대상에서 새 폴더 기준 경로로 갱신합니다.

업데이트 전 번들의 `CHATGPT_DESKTOP_AGENT_CONNECT_PROMPT.md`는 ChatGPT Desktop에 Codex CLI 설치를 요청하던 구형 형식일 수 있습니다. 운영 화면은 이 파일을 일반 에이전트 프롬프트로 복사시키지 않습니다. 검증 가능한 로컬 stdio 입력값이 있으면 현재 번들 경로로 다시 계산한 Desktop Settings 안내로 변환하고, 복구할 수 없으면 새 번들 생성을 요구합니다. 이 경우 구형 ChatGPT Desktop BAT도 실행하지 않습니다.

전산 담당자는 필요할 때만 번들 폴더에서 연결 마법사를 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1
```

`reg-rag-mcp-*` 명령이 보이지 않으면 먼저 설치 보조 스크립트를 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/install_local_package.ps1
```

비대화형 보조 경로가 필요하면 대상별 명령을 따로 사용합니다. 일반 ChatGPT Desktop 사용자는 이 블록 대신 위의 내장 설정 등록을 사용합니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1 -InstallPackage -Target claude-code
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1 -InstallPackage -Target codex -InstallCodex
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1 -InstallPackage -Target claude-desktop -InstallClaudeDesktop
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1 -InstallPackage -Target chatgpt-desktop-direct
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1 -Target chatgpt-remote
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/connect_mcp_client.ps1 -Target chatgpt-tunnel
```

## 3. Claude Code

로컬 stdio 연결은 `CLAUDE_CODE_AGENT_CONNECT_PROMPT.md`를 사용해 공식 CLI의 사용자 범위(`--scope user`)에 등록합니다. 보조 스크립트는 예전 생성기가 만든 같은 이름의 local 항목과 기존 user 항목을 정리하고 다시 등록한 뒤 `claude mcp get`으로 확인합니다. 따라서 생성 폴더 밖에서 Claude Code를 시작해도 같은 사용자에게 보입니다.

주의: [Anthropic의 Claude Code MCP 공식 한국어 문서](https://code.claude.com/docs/ko/mcp)에 따라 프로젝트 루트의 `.mcp.json`은 `project` scope이고, `~/.claude/settings.json`은 MCP 사용자 등록 파일을 대신하지 않습니다. `enabledMcpjsonServers` 같은 프로젝트 승인 설정과 서버 등록 자체를 혼동하지 마세요. 사용자 전체 연결은 설정 파일 경로를 추측해 직접 편집하는 대신 `claude mcp add --scope user ...`로 등록하고, `claude mcp get <이름>`의 User scope·`Status: Connected`·정확한 launcher/data 경로와 실제 stdio protocol smoke를 모두 검증합니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/claude_code_add_stdio.ps1
```

원격 HTTPS MCP를 Claude Code에 붙일 때는 `claude_code_add_http.ps1`을 사용하고, 먼저 `MCP_AUTH_TOKEN`을 승인된 환경변수로 설정합니다.

## 4. Codex CLI

생성된 `codex_config_snippet.toml`의 `[mcp_servers.regulation_mcp]` 블록은 번들의 실제 `data` 폴더를 가리키고, 시작 지연을 줄이기 위해 `--flat-storage`와 `--no-warm-cache`를 포함합니다. ZIP의 `<BUNDLE_DIR>`을 수동으로 치환한다면 `C:/MCP/aksmcp2`처럼 슬래시(`/`)를 쓴 절대 경로를 사용합니다. 역슬래시는 TOML 규칙에 맞게 각각 이스케이프해야 합니다. Codex CLI 시작 시간이 기본 제한에 너무 근접하지 않도록 `startup_timeout_sec = 45`도 명시합니다.

Codex CLI는 사용자 설정 `~/.codex/config.toml`을 읽습니다. 이 파일은 현재 ChatGPT Desktop 로컬 direct MCP 등록과 공유되므로 같은 서버 이름을 수정하면 두 클라이언트의 로컬 등록 경로에 함께 영향을 줄 수 있습니다.

붙여 넣은 뒤에는 실제 설치 설정이 오래된 `data-dir`를 보고 있지 않은지 doctor로 확인합니다.

```powershell
reg-rag-mcp-doctor `
  --client-profile bundle `
  --bundle-dir reports\mcp_connection_bundle `
  --allow-local-only-bundle `
  --codex-config $HOME\.codex\config.toml
```

doctor가 통과해도 Codex가 도구를 늦게 보거나 못 본다면, 설치된 Codex 설정 파일 그대로 실제 MCP를 띄워 확인합니다.

```powershell
python scripts\run_mcp_client_config_smoke.py `
  --server-name regulation_mcp `
  --codex-config $HOME\.codex\config.toml `
  --out-json reports\codex_installed_mcp_config_smoke.json `
  --fail-on-issue
```

## 5. Claude Desktop

기본 경로는 `Claude Desktop에 연결하기.bat`입니다. BAT는 `reports/mcp_connection_bundle/claude_desktop_config.json`의 `mcpServers` 값을 Claude Desktop 사용자 설정에 백업·병합합니다.

기존 설정 JSON만 먼저 검사해야 하는 전산 담당자는 `connect_mcp_client.ps1 -Target claude-desktop -ValidateClaudeDesktop`을 사용합니다. 설치할 때는 BAT가 이 검증과 `-InstallClaudeDesktop` 병합을 함께 처리합니다.

Windows 기본 위치:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

JSON 오류가 나면 쉼표, 중괄호 위치, `mcpServers` 중복 여부를 먼저 확인합니다. `Unexpected token "{", "m"... is not valid JSON` 오류는 기존 JSON 안에 새 JSON 전체를 한 번 더 붙여 넣거나, `mcpServers` 블록만 병합하지 않았을 때 자주 발생합니다. 이때는 `connect_mcp_client.ps1 -Target claude-desktop -ValidateClaudeDesktop`로 기존 설정 파일을 먼저 검증하고, 가능하면 `-InstallClaudeDesktop` 자동 병합을 사용합니다. 설정 변경 뒤에는 Claude Desktop을 완전히 종료했다가 다시 실행해야 도구가 로드됩니다.

병합 뒤에는 설치된 Claude Desktop 설정 파일도 같은 방식으로 실제 실행 검증을 합니다.

```powershell
python scripts\run_mcp_client_config_smoke.py `
  --server-name regulation_mcp `
  --claude-desktop-config "$env:APPDATA\Claude\claude_desktop_config.json" `
  --out-json reports\claude_desktop_installed_mcp_config_smoke.json `
  --fail-on-issue
```

이 smoke 성공은 Claude Desktop 로더나 현재 대화 노출 성공이 아닙니다. 재시작한 새 대화에서 입력창의 `+` → Connectors에 서버가 보이는지 확인하고 실제 `get_index_status` 호출을 요청합니다.

## 6. ChatGPT Desktop

프로그램 생성 결과 화면의 `CHATGPT_DESKTOP_CONNECT_GUIDE.md` 코드 상자에 표시된 Name·STDIO·Command·Working directory·Arguments를 ChatGPT Desktop의 `Settings > MCP servers > Add server`에 입력하고 Save한 뒤 Restart합니다. ZIP 원본의 `<PROGRAM_BUNDLE_DIR>` 자리표시자는 입력값이 아닙니다. 이 화면이 기본 사용자 경로이며 Codex CLI를 실행하라는 뜻이 아닙니다. 다만 현재 로컬 direct 설정의 저장소는 Codex CLI와 같은 `~/.codex/config.toml`입니다.

새 대화에서 `/mcp`로 서버 이름을 확인하고 실제 `get_index_status`를 호출합니다. 내장 메뉴가 없거나 수동 입력이 어려울 때만 보조 수단인 `ChatGPT Desktop에 연결하기.bat`를 사용합니다. `@MCP이름` 반복 입력은 설치나 연결 확인을 대신하지 않습니다. ChatGPT 대화 화면이 로컬 direct MCP를 노출하지 않는 제품 구성에서는 아래의 원격 HTTPS 또는 Secure MCP Tunnel 방식을 사용합니다.

Work/Codex 플러그인 배포를 명시적으로 선택한 경우에만 direct 설정과 겹치지 않는 격리 환경에서 `-InstallPackage -Target chatgpt-desktop-local -InstallChatGptDesktopPlugin`을 사용합니다. 일반 ChatGPT Desktop 연결에는 위의 내장 등록을 사용합니다.

## 7. ChatGPT 원격 HTTPS

이 절차는 위의 ChatGPT Desktop 로컬 direct MCP와 별개입니다. ChatGPT 개발자 모드 custom app은 ChatGPT에서 접근 가능한 인증된 HTTPS `/mcp` endpoint가 필요하며 localhost에 직접 연결하지 않습니다. 생성된 `chatgpt-remote` 프로필은 외부 응답 경계를 위해 `chatgpt-data` 프로필(search/fetch)만 명시적으로 노출합니다. 외부 connector 응답에는 `source_record_id`, `source_file_id`, `approval_review_batch_manifest_path` 같은 내부 운영 metadata가 포함되면 안 됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/run_chatgpt_data_server.ps1
```

ChatGPT `Settings > Apps > Advanced Settings`에서 Developer mode를 켠 뒤 `Settings > Apps > Create`에서 custom app을 만듭니다. 앱 이름은 프로그램에서 입력한 MCP 이름으로 지정하고 `chatgpt_connector.json`의 `connector_url`을 등록합니다. 생성 시 표시되는 도구 목록에서 `search`와 `fetch`를 확인합니다. `get_index_status` 같은 내부 진단은 로컬/full 프로필에서만 실행합니다. 새 ChatGPT 대화의 tools 메뉴에서 만든 앱을 선택한 뒤 실제 `search`와 `fetch` 호출로 연결을 확인합니다.

ChatGPT 웹은 ChatGPT Desktop/Codex가 공유하는 로컬 `~/.codex/config.toml`을 읽지 않으며 로컬 `/mcp` 메뉴도 노출하지 않습니다. Work mode의 Plugins는 배포된 원격 도구를 설치하는 별도 경로입니다. 이 번들의 custom app 생성 절차나 로컬 stdio 플러그인과 혼용하지 않습니다.

배포된 HTTPS endpoint 자체는 다음 스크립트로 검증합니다. 이 스크립트가 성공해도 Apps의 도구 scan 및 ChatGPT 대화 첨부 상태는 별도입니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/validate_chatgpt_remote_mcp.ps1
```

## 8. ChatGPT 웹: Secure MCP Tunnel

기관 내부 MCP 서버를 인터넷에 직접 공개하지 않으려면 OpenAI Secure MCP Tunnel 템플릿을 사용합니다.

```powershell
powershell -ExecutionPolicy Bypass -File reports/mcp_connection_bundle/run_openai_secure_tunnel.ps1
```

실행 전에 `CONTROL_PLANE_API_KEY`와 `OPENAI_TUNNEL_ID`를 승인된 환경변수로 설정합니다. 값을 스크립트 파일에 저장하지 않습니다. Secure MCP Tunnel 전용 공식 절차에 따라 ChatGPT `Settings > Security and login`에서 Developer mode를 켠 뒤 `Settings > Plugins` 또는 `https://chatgpt.com/plugins`의 `+`에서 앱을 만들고 Connection을 Tunnel로 선택합니다. 새 대화에서는 `+ > More`에서 앱을 첨부합니다. 이 경로는 공개 HTTPS용 `Settings > Apps > Create` 및 Work mode marketplace 플러그인 설치와 구분합니다.

터널 설정 점검 예시:

```powershell
reg-rag-mcp-doctor `
  --client-profile chatgpt-remote `
  --connection-mode openai-tunnel `
  --transport stdio `
  --skip-data-check
```

## 9. Claude (HTTPS)

Claude 앱과 Claude Messages API는 같은 HTTPS MCP URL을 사용할 수 있지만 등록 화면과 설정 형식은 다릅니다.

- Claude 앱: 개인 Pro/Max는 `Customize > Connectors`에서 custom connector를 추가하고, Team/Enterprise는 Owner가 `Organization settings > Connectors`에 먼저 추가합니다. 대화의 `+` > `Connectors`에서 활성화합니다.
- Claude Messages API: `claude_api_fragment.json`의 `mcp_servers`, `tools`, `betas` 값을 API 요청에 포함합니다.

Claude 앱의 Connectors 화면에 `claude_api_fragment.json`을 붙여 넣지 않고 HTTPS MCP URL만 등록합니다. 반대로 API 요청에 `claude_desktop_config.json`의 로컬 stdio 항목을 사용하지 않습니다. 두 방식 모두 URL 기반 원격 MCP가 필요하며 로컬 stdio 서버를 직접 연결하지 않습니다.

## 10. 사전 진단

```powershell
reg-rag-mcp-doctor `
  --client-profile chatgpt-remote `
  --transport streamable-http `
  --host 0.0.0.0 `
  --public-url https://mcp.example.go.kr/mcp `
  --bundle-dir reports/mcp_connection_bundle `
  --skip-data-check
```

`--bundle-dir reports/mcp_connection_bundle`는 필수 번들 파일 누락, JSON 구문 오류, Claude Desktop `mcpServers` 구조 오류, secret placeholder, 파일 안 토큰 assignment를 함께 검사합니다. 기본 검사는 설정 검증이며 실제 HTTPS 도달성은 확인하지 않습니다. 배포 직전 실제 endpoint까지 확인하려면 `--probe-public-url`을 추가하고 report의 `deploy_ready`를 확인합니다.

로컬 Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop만 사용할 번들은 public URL이 없어도 됩니다. 이 경우 원격 ChatGPT/Claude 준비 상태를 실패로 보지 않도록 다음 옵션을 추가합니다.

```powershell
reg-rag-mcp-doctor `
  --client-profile bundle `
  --bundle-dir reports/mcp_connection_bundle `
  --allow-local-only-bundle `
  --skip-data-check
```

## 11. 한국학중앙연구원 MVP 예시

현재 AKS MVP 런타임을 로컬 Claude Desktop에 붙일 때의 핵심 값은 다음과 같습니다.

```powershell
reg-rag-mcp-config `
  --client-profile claude-desktop `
  --server-name aks-regulation-mcp `
  --data-dir data\aks_mcp_publish_runtime `
  --tenant-id tenant-aks-publish `
  --tenant-storage-isolation `
  --transport stdio `
  --out-json reports\aks_claude_desktop_config.json
```

연결 전 검증:

```powershell
reg-rag-mcp-doctor `
  --client-profile bundle `
  --bundle-dir reports\aks_mcp_connection_bundle_20260708 `
  --allow-local-only-bundle `
  --skip-data-check

python scripts\run_mcp_transport_smoke.py `
  --data-dir data\aks_mcp_publish_runtime `
  --tenant-id tenant-aks-publish `
  --skip-preparation `
  --query "전임 교원 채용 절차는?" `
  --fail-on-issue
```

실제 승인ㆍ인덱싱된 runtime에는 `--skip-preparation`을 유지합니다. 준비 단계를 생략하지 않는
MCP smoke/transport smoke는 합성 승인 문서를 쓰는 scratch-only 점검이며, 명시적 `--data-dir`에
쓰려면 `--allow-persistent-smoke-data`가 필요합니다. 운영 runtime에서는 먼저
`reg-rag-mcp-index-visibility --forbid-smoke-docs`로 smoke 문서가 섞이지 않았는지 확인합니다.

현재 AKS handoff 기준은 `ready_for_local_claude_desktop_mvp`입니다. 최근 transport smoke 기준으로 예열 후 AKS 첫 `search`는 약 143ms, `fetch`는 약 30ms 수준입니다. 연결 뒤 Claude Desktop에서는 “육아휴직의 요건과 기간, 수당은?”, “전임 교원 채용 절차는?”, “성과연봉은 언제 어떻게 지급되나?” 같은 질문으로 `search`와 `fetch` 호출 여부를 확인합니다.

## 12. 공식 참고

- ChatGPT Desktop MCP: https://learn.chatgpt.com/docs/extend/mcp
- OpenAI Secure MCP Tunnel: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- ChatGPT 개발자 모드 MCP apps: https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta
- ChatGPT Work mode Plugins: https://learn.chatgpt.com/docs/plugins
- Claude API MCP connector: https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector
- Claude custom connectors: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp
- 대상별 연결 순서: Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop, ChatGPT 원격 MCP, ChatGPT 웹, Claude (HTTPS) 순서입니다. Claude Code와 Codex CLI는 대상별 요청문을 실행하고, Claude Desktop은 전용 BAT와 `%APPDATA%\Claude\claude_desktop_config.json`을 사용한 뒤 재시작한 새 대화의 Connectors에서 확인합니다. ChatGPT Desktop은 `Settings > MCP servers > Add server`가 기본이며, 로컬 direct 저장소는 Codex CLI와 `~/.codex/config.toml`을 공유합니다. ChatGPT Desktop·Codex CLI·Claude Code만 `/mcp` 공통 확인을 사용합니다. HTTPS는 공개 `/mcp` URL을 사용하고, ChatGPT 웹은 승인된 Secure MCP Tunnel을 사용합니다.
