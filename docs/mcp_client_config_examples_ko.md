# MCP 클라이언트 설정 예시

전처리, 사람 검수, 승인, Vector DB 인덱싱이 끝난 로컬 규정 DB를 생성형 AI에 연결하는 설정 예시입니다. MCP는 연결 표준이고, 실제 규정 검색은 MCP 서버 내부의 승인된 로컬 RAG/Vector DB가 수행합니다.

## 권장 구조

```text
Claude Code / Codex CLI / Claude Desktop / ChatGPT Desktop 로컬 direct MCP
  -> local stdio MCP
  -> PR MCP Builder local MCP server

ChatGPT 원격 MCP / ChatGPT 웹 / Claude (HTTPS)
  -> authenticated HTTPS Streamable HTTP MCP
  -> PR MCP Builder MCP server

두 경로 모두
  -> approved local regulation DB and vector index
```

여기서 `search`는 관련 규정 근거를 찾는 단계이고, `fetch`는 찾은 결과의 `id`로 원문 근거를 가져오는 단계입니다. 생성형 AI가 직접 전체 규정 파일을 읽는 구조가 아니라, 승인된 로컬 RAG/Vector DB를 MCP 도구로 조회하는 구조입니다.

기본 도구:

- `search`: 승인된 규정 조항 검색
- `fetch`: `search` 결과 id로 원문 근거 조회
- `list_documents`: MCP에서 보이는 승인 문서 목록
- `get_article`: 특정 문서와 조항 번호 조회
- `get_table`: 표, 별표, 서식 근거 조회
- `compare_versions`: 두 문서 버전의 승인 조항 비교
- `get_citation`: citation metadata 조회
- `get_index_status`: 승인 벡터 인덱싱 상태 확인

도구 계약은 `docs/mcp_tool_contract_ko.md`를 기준으로 봅니다.

## 설정 번들 생성

```powershell
reg-rag-mcp-config `
  --client-profile bundle `
  --server-name regulation_mcp `
  --tenant-id default `
  --public-url https://mcp.example.go.kr `
  --out-dir reports/mcp_connection_bundle `
  --skip-runtime-data `
  --zip-out reports/mcp_connection_bundle.zip `
  --include-wheel
```

`--public-url`이 없어도 Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop 로컬 direct MCP의 stdio 설정은 생성됩니다. ChatGPT 원격 MCP, ChatGPT 웹, Claude (HTTPS) 연결만 원격 준비가 필요합니다.
로컬 Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop만 검증할 때는 doctor에 `--allow-local-only-bundle`을 붙이면 원격 프로필 not-ready를 실패로 보지 않습니다. 이 검사는 번들 내 JSON 구문과 Claude Desktop `mcpServers` 구조도 함께 확인합니다.
Claude Desktop 설정 파일 자체가 깨졌는지 확인하려면 `connect_mcp_client.ps1 -Target claude-desktop -ValidateClaudeDesktop`를 먼저 실행합니다. 통과하면 `-InstallClaudeDesktop` 자동 병합을 사용하고, 수동 편집 시에는 생성된 JSON 전체가 아니라 `mcpServers` 항목만 병합합니다. 자동 병합은 설치된 사용자 설정으로 `initialize`·`tools/list`·`get_index_status`까지 직접 검증하고 `bundle_status.json`을 `installed_pending_claude_desktop_verification`으로 기록합니다. 이는 Claude Desktop 자체 로더나 현재 대화 노출 성공이 아니므로, 완전 재시작 후 Connectors와 실제 도구 호출을 별도로 확인합니다. 생성된 `claude_desktop_config.json`은 bundle 폴더의 `data` 경로를 가리키도록 만들어지지만, zip을 다른 위치에 풀었다면 자동 병합 스크립트를 다시 실행하는 편이 안전합니다.

번들의 `data/` 폴더는 실제 approved runtime payload입니다. approved chunks, approved vectors, BM25 index, approval/indexing journal, `mcp_runtime_manifest.json`만 handoff 대상이며 raw `*_nodes.json`, `*_issues.json`, `*_quality.json`는 포함하지 않습니다.

번들 주요 파일:

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
- `claude_code_add_stdio.ps1`
- `claude_code_add_http.ps1`
- `run_http_server.ps1`
- `run_chatgpt_data_server.ps1`
- `run_openai_secure_tunnel.ps1`
- `doctor_mcp_connection.ps1`
- `chatgpt_connector.json`
- `claude_api_fragment.json`
- `data/mcp_runtime_manifest.json`
- `data/repository/*_chunks.json`
- `data/vector_db/<tenant>/approved_vectors.jsonl`
- `data/vector_db/<tenant>/bm25_index.json`

대상 안내 순서는 Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop, ChatGPT 원격 MCP, ChatGPT 웹, Claude (HTTPS)입니다. Claude Code와 Codex CLI에는 대상별 `*_AGENT_CONNECT_PROMPT.md`를 표시하고 Claude Desktop은 전용 BAT가 기본입니다. ChatGPT Desktop은 `CHATGPT_DESKTOP_CONNECT_GUIDE.md`의 값을 `Settings > MCP servers > Add server`에 입력하는 방식이 기본이며 Codex CLI를 실행하는 방식이 아닙니다. 다만 현재 로컬 direct 설정은 Codex CLI와 `~/.codex/config.toml`을 공유합니다. 수동 입력이 어렵거나 고급 설정 파일 경로가 필요할 때만 전용 BAT를 보조 수단으로 사용합니다. BAT가 공유 설정을 기록해도 Desktop에 없는 MCP 기능을 활성화하지는 않으므로, 메뉴와 `/mcp`가 계속 보이지 않으면 앱 버전·계정 정책을 확인하고 원격 HTTPS 또는 Secure MCP Tunnel로 전환합니다. Save·Restart 뒤 새 대화에서 `/mcp`와 실제 도구 호출을 확인하며, `@aksmcp` 반복 입력은 설치나 연결 확인을 대신하지 않습니다.

같은 이름으로 번들을 다시 생성하고 대상별 연결 절차를 다시 실행하면 추가·개정 청크가 같은 MCP 이름에 반영됩니다.

## stdio 방식

Claude Code, Codex CLI, Claude Desktop, ChatGPT Desktop 로컬 direct MCP에는 stdio가 가장 단순합니다.

```powershell
reg-rag-mcp-server --data-dir data --tenant-id default --transport stdio
```

서버는 기본적으로 시작 시 승인 Vector DB, 승인 스냅샷, BM25 인덱스와 대표 scoring 경로를 예열할 수 있습니다. 생성된 bundle의 stdio/터널 스크립트는 클라이언트 등록이 느려지지 않도록 `--no-warm-cache`를 붙입니다. 상주형 HTTP 운영에서 첫 검색 속도를 더 중시할 때만 예열을 켭니다.

Claude Code용 등록 명령:

```powershell
reg-rag-mcp-config `
  --client-profile claude-code `
  --server-name regulation_mcp `
  --tenant-id default `
  --transport stdio
```

Codex용 TOML:

생성된 bundle에는 `codex_config_snippet.toml`이 함께 들어갑니다. ZIP의 `<BUNDLE_DIR>`을 수동으로 바꿀 때는 `C:/MCP/aksmcp2`처럼 슬래시(`/`)를 쓴 절대 경로를 사용합니다. 역슬래시를 쓰려면 TOML 문자열 규칙에 맞게 각각 이스케이프해야 합니다. 이 파일의 `[mcp_servers.regulation_mcp]` 블록을 `$HOME\.codex\config.toml`에 붙여 넣거나 같은 이름의 기존 블록과 교체합니다. 이 사용자 파일은 현재 ChatGPT Desktop 로컬 direct MCP와 공유되지만, ChatGPT Desktop의 기본 사용자 절차는 `Settings > MCP servers`에서 등록하는 방식입니다. 화면에서 MCP 이름을 바꿨다면 해당 이름의 블록이 생성됩니다. bundle용 블록에는 실제 bundle `data` 경로, `--transport stdio`, `--flat-storage`, `--no-warm-cache`, `startup_timeout_sec = 45`가 포함되어야 합니다.

설정 후에는 실제 Codex 설정이 오래된 bundle 경로를 보고 있지 않은지 같이 검사합니다.

```powershell
reg-rag-mcp-doctor `
  --client-profile bundle `
  --bundle-dir reports\mcp_connection_bundle `
  --allow-local-only-bundle `
  --codex-config $HOME\.codex\config.toml
```

Claude Desktop용 JSON:

```powershell
reg-rag-mcp-config `
  --client-profile claude-desktop `
  --server-name regulation_mcp `
  --tenant-id default `
  --transport stdio
```

출력 예시:

```json
{
  "mcpServers": {
    "regulation_mcp": {
      "type": "stdio",
      "command": "reg-rag-mcp-server",
      "args": [
        "--data-dir",
        "data",
        "--tenant-id",
        "default",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

Claude Desktop은 생성 JSON 전체를 기존 JSON 뒤에 붙이지 않습니다. 전용 BAT로 `%APPDATA%\Claude\claude_desktop_config.json`의 `mcpServers`를 백업·병합한 뒤 앱을 완전히 재시작하고 새 대화의 Connectors에서 서버와 실제 도구 호출을 확인합니다.

## HTTP 방식

로컬 stdio를 사용할 수 없는 원격 연결에는 HTTP MCP가 필요합니다. HTTP/SSE를 외부에 열 때는 bearer token 또는 승인된 인증 프록시를 사용해야 합니다.

```powershell
$env:MCP_AUTH_TOKEN = "set-via-approved-secret-manager"
reg-rag-mcp-server `
  --data-dir data `
  --tenant-id default `
  --transport streamable-http `
  --host 0.0.0.0 `
  --port 8000 `
  --http-bearer-token-env MCP_AUTH_TOKEN `
  --auth-issuer-url https://mcp.example.go.kr
```

HTTP 설정 생성:

```powershell
reg-rag-mcp-config `
  --server-name regulation_mcp `
  --tenant-id default `
  --transport streamable-http `
  --host 0.0.0.0 `
  --port 8000 `
  --public-url https://mcp.example.go.kr
```

## ChatGPT 원격 연결

ChatGPT 개발자 모드 custom app은 HTTPS `/mcp` endpoint가 필요합니다. `Settings > Apps > Advanced Settings`에서 Developer mode를 켠 뒤 `Settings > Apps > Create`에서 등록합니다. ChatGPT 웹은 로컬 `~/.codex/config.toml`이나 로컬 stdio 서버를 읽지 않습니다. Work mode의 Plugins는 배포된 원격 도구를 설치하는 별도 경로입니다. 공개 가능 데이터 또는 별도 승인된 데이터만 연결합니다.

```powershell
reg-rag-mcp-config `
  --client-profile chatgpt-remote `
  --server-name regulation_mcp `
  --tenant-id default `
  --transport streamable-http `
  --host 0.0.0.0 `
  --port 8000 `
  --public-url https://mcp.example.go.kr
```

원격 `chatgpt-data` 프로필은 승인된 읽기 전용 `search`와 `fetch`만 노출합니다. endpoint 검증은 `initialize`, `tools/list`, `search`, `fetch`를 통과해야 하며, 이 성공은 ChatGPT Apps의 도구 scan이나 대화 첨부 성공을 뜻하지 않습니다. 내부망에서 직접 공개하지 않는 방식이 필요하면 bundle의 `run_openai_secure_tunnel.ps1`을 사용합니다.

### ChatGPT 웹: Secure MCP Tunnel

ChatGPT 웹에서 내부 MCP 서버를 직접 공개하지 않으려면 승인된 Secure MCP Tunnel을 사용합니다. 전용 공식 절차에 따라 `Settings > Security and login`에서 Developer mode를 켠 뒤 `Settings > Plugins` 또는 `https://chatgpt.com/plugins`의 `+`에서 앱을 만들고 Connection을 Tunnel로 선택합니다. 이 경로는 공개 HTTPS custom app의 `Settings > Apps > Create` 및 Work mode marketplace 플러그인 설치와 구분합니다. 로컬 stdio나 `localhost` 주소를 웹 대화에 직접 등록하는 방식은 지원 대상으로 안내하지 않습니다.

## Claude (HTTPS) 연결

Claude 앱 custom connector와 Claude Messages API는 같은 HTTPS MCP URL을 사용할 수 있지만 설정 형식은 다릅니다. Claude 앱은 URL만 `Customize > Connectors`에 등록하고 대화의 `+` > `Connectors`에서 활성화합니다. 아래 생성물은 Messages API 요청용이며 Claude 앱의 Connectors 화면에 붙여 넣지 않습니다.

Claude API는 URL 기반 MCP 서버 정의를 사용합니다.

```powershell
reg-rag-mcp-config `
  --client-profile claude-api `
  --server-name regulation_mcp `
  --tenant-id default `
  --transport streamable-http `
  --host 0.0.0.0 `
  --port 8000 `
  --public-url https://mcp.example.go.kr
```

출력의 `mcp_servers`, `tools`, `betas` 값을 Messages API 요청에 포함합니다.

## Tenant 격리

기관 또는 부서별 데이터를 한 시스템에서 운영하려면 tenant 저장소 격리를 켭니다.

```powershell
reg-rag-mcp-server `
  --data-dir data `
  --tenant-id agency-a `
  --tenant-storage-isolation `
  --transport stdio
```

## 한국학중앙연구원 MVP

AKS 전처리본을 Claude Desktop에 붙이는 설정 생성:

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

실제 MCP stdio 연결 검증:

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
  --query "육아휴직" `
  --out-json reports\mcp_transport_smoke_aks_manual_check.json `
  --fail-on-issue
```

실제 runtime 검증에서는 `--skip-preparation`을 유지합니다. 준비 단계가 켜진 MCP smoke는 합성
승인/인덱스 문서를 쓰는 scratch-only 점검이며, 명시적 `--data-dir`에는
`--allow-persistent-smoke-data`가 필요합니다. 운영 runtime에는 smoke 문서가 남아 있으면 안 됩니다.

검증 결과의 `first_result_metadata`에는 `approval_id`, `content_hash`, `approved_content_hash`, `profile_id`, `source_system`, `source_url`, `regulation_title`, `article_no`, `source_page_start`가 포함되어야 합니다.
성능 확인은 같은 JSON의 `full_profile_timing_ms`, `full_profile_search_timing_ms`, `search_elapsed_ms`, `warm_search_elapsed_ms`를 봅니다. `search_elapsed_ms`가 크고 `warm_search_elapsed_ms`가 작으면 cold cache 비용이고, 둘 다 크면 scoring 또는 저장소 크기 문제로 봅니다.

## 연결 후 확인

1. `get_index_status`로 문서가 `indexed`인지 확인합니다.
2. `search`로 실제 규정 질의를 실행합니다.
3. `fetch`로 검색 결과 id의 전체 근거와 citation metadata를 확인합니다.
4. 답변에 승인되지 않은 초안, raw file path, smoke 문서가 섞이지 않는지 확인합니다.

Claude Desktop에서 smoke-test 문서만 보이거나 실제 규정이 검색되지 않으면 MCP 연결 자체보다 먼저 운영 데이터 상태를 확인합니다.

- 연결 전에 다음 명령으로 런타임에 실제 승인 record가 보이는지 확인합니다.

```powershell
reg-rag-mcp-index-visibility `
  --data-dir data\aks_mcp_publish_runtime `
  --tenant-id tenant-aks-publish `
  --tenant-storage-isolation `
  --min-visible-records 5000 `
  --forbid-smoke-docs `
  --require-indexed `
  --fail-on-issue
```

- 실행 중인 MCP의 `--data-dir`가 승인ㆍ인덱싱을 끝낸 런타임 디렉터리인지 확인합니다.
- `--tenant-id`가 전처리, 승인, 인덱싱 때 사용한 tenant와 같은지 확인합니다.
- Streamlit의 `MCP-visible records`와 `Approved chunks`가 기대 수량인지 확인합니다.
- `get_index_status`가 `indexed`가 아니거나 `reindex_required`이면 `Reindex approved chunks`를 실행합니다.
- smoke 문서는 연결 검증용 샘플이므로 운영용 MCP 결과에 남아 있으면 잘못된 data-dir 또는 tenant를 보고 있는 상태로 봅니다.

## 보안 주의

- 토큰, API 키, 터널 ID를 파일에 저장하지 않습니다.
- 원격 HTTP MCP는 bearer token 또는 승인된 인증 프록시 없이 공개하지 않습니다.
- 외부 AI 서비스에 연결되는 MCP는 반환 데이터가 외부 서비스로 전달될 수 있으므로 공개 가능 데이터 또는 별도 승인 데이터만 사용합니다.
- 비공개 내부 규정은 로컬 stdio 또는 승인된 내부망 MCP를 우선 사용합니다.
