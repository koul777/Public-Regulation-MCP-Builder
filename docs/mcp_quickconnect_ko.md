# MCP 빠른 연결

PR MCP Builder가 지원하는 공식 연결 방식은 로컬 stdio와 공개 HTTPS Streamable HTTP입니다. 생성 번들에는 BAT 파일과 에이전트 연결 프롬프트가 포함되지 않습니다.

## 1. 번들 생성

운영 화면에서 승인·색인이 끝난 범위와 MCP 이름을 선택한 뒤 `MCP로 쓸 파일 묶음 만들기`를 실행합니다. 승인되지 않은 청크, 원본 업로드, trace, export, 비밀값은 번들에 포함하지 않습니다.

핵심 산출물:

- `mcp_config.bundle.json`: 전체 연결 계약
- `run_mcp_stdio_server.ps1`: 로컬 stdio 서버
- `codex_config_snippet.toml`: ChatGPT Desktop·Codex CLI·Codex IDE 공용 설정
- `claude_code_add_stdio.ps1`: Claude Code 공식 CLI 등록
- `claude_code_add_http.ps1`: 공개 HTTPS URL이 있을 때 생성되는 Claude Code 원격 등록
- `claude_desktop_config.json`: Claude Desktop `mcpServers`
- `chatgpt_desktop_local_mcp.json`: ChatGPT Desktop 입력값
- `doctor_mcp_connection.ps1`: 연결 전 진단
- `validate_mcp_smoke.ps1`: MCP transport 검증
- `data/`: 승인 runtime data

## 2. 로컬 stdio

stdio MCP는 디렉터리를 스캔하거나 폴더명만 등록하는 방식이 아닙니다. 클라이언트에 다음 실행 계약을 모두 등록해야 합니다.

```json
{
  "command": "powershell.exe",
  "args": [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    "C:/absolute/path/to/bundle/run_mcp_stdio_server.ps1"
  ],
  "cwd": "C:/absolute/path/to/bundle",
  "env": {}
}
```

생성 화면은 현재 번들의 실제 절대경로가 반영된 `command`, `args`, `cwd`, `env`를 표시합니다. ZIP을 다른 폴더로 옮겼다면 새 위치에서 번들을 다시 생성하거나 모든 경로를 새 절대경로로 갱신합니다.

### ChatGPT Desktop / Codex CLI / Codex IDE

세 제품은 같은 `~/.codex/config.toml`을 공유합니다. `codex_config_snippet.toml`의 `[mcp_servers.<이름>]` 블록을 한 번만 반영하고 사용 중인 앱 또는 확장을 완전히 재시작한 뒤 `/mcp`와 실제 `search`·`fetch` 호출을 확인합니다. 직접 등록에는 `codex plugin list`가 필요하지 않습니다.

### Claude Code

PowerShell에서 다음 파일을 실행합니다.

```powershell
.\claude_code_add_stdio.ps1
```

이 스크립트는 공식 `claude mcp add --transport stdio --scope user` 계약을 사용합니다. 등록 후 `claude mcp get <이름>`과 `claude mcp list`를 확인하고 Claude Code를 재시작합니다.

### Claude Desktop

처음 연결한다면 아래 순서를 그대로 따릅니다. 왼쪽의 **커넥터** 메뉴는 Vercel 같은 원격
HTTPS MCP용입니다. 같은 PC에서 실행하는 로컬 stdio 서버는
**설정 > 개발자 > 로컬 MCP 서버 > 구성 편집**(영문 UI: **Settings > Developer > Edit Config**)에서 등록합니다.

1. PR MCP Builder에서 MCP 번들 생성을 완료합니다.
2. 생성 폴더의 `claude_desktop_config.json`을 메모장으로 엽니다. 이 파일에 표시된 경로를
   예시 경로로 바꾸거나 직접 다시 입력하지 않습니다.
3. Claude Desktop에서 **구성 편집**을 눌러
   `%APPDATA%\Claude\claude_desktop_config.json`을 엽니다.
4. 생성 파일의 `mcpServers` 안에 있는 새 서버 항목만 Claude 설정의 `mcpServers`에
   복사합니다. 기존 서버와 `preferences` 같은 다른 최상위 설정은 그대로 둡니다.
5. JSON을 저장합니다. Windows 경로는 JSON 안에서 `C:\\폴더\\파일`처럼 역슬래시가
   두 번 표시되는 것이 정상입니다.
6. Claude Desktop 창만 닫지 말고 작업 표시줄 알림 영역에서도 **종료**한 뒤 다시
   실행합니다.
7. 새 대화를 열고 **파일·커넥터 추가 > Connectors**에서 생성한 서버 이름이
   `running`인지 확인합니다.

예를 들어 기존 설정이 다음과 같다면:

```json
{
  "preferences": {
    "theme": "dark"
  },
  "mcpServers": {
    "already-used-server": {
      "command": "existing-command"
    }
  }
}
```

새 서버를 추가한 뒤에도 `preferences`와 `already-used-server`를 남겨야 합니다.
검증된 소스 프로젝트 Python을 직접 실행하는 항목은 다음 형태입니다.

```json
{
  "preferences": {
    "theme": "dark"
  },
  "mcpServers": {
    "already-used-server": {
      "command": "existing-command"
    },
    "<생성된-서버-이름>": {
      "command": "C:\\project\\Public-Regulation-MCP-Builder\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "scripts.run_regulation_mcp",
        "--data-dir",
        "C:\\MCP 번들\\기관 규정\\data",
        "--tenant-id",
        "<생성된-tenant-id>",
        "--transport",
        "stdio",
        "--profile-id",
        "<생성된-profile-id>",
        "--flat-storage",
        "--tool-profile",
        "<생성된-tool-profile>",
        "--no-warm-cache"
      ],
      "env": {
        "PYTHONPATH": "C:\\project\\Public-Regulation-MCP-Builder",
        "PYTHONSAFEPATH": "1"
      }
    }
  }
}
```

`command`, `args`, `env`는 하나의 실행 계약입니다. `args`의 순서를 바꾸거나
`PYTHONPATH`를 빼면 Claude Desktop의 시작 폴더에 따라 모듈을 찾지 못할 수 있습니다.
`type: "stdio"`는 현재 Claude Desktop 로컬 서버에 필수가 아니므로 생성되지 않을 수
있습니다. 소스 프로젝트와 Python 3.11+ import 검사가 통과하지 않거나 독립 배포
ZIP/wheel을 쓰는 경우에는 같은 자리에 `run_mcp_stdio_server.ps1`을 실행하는 fallback
항목이 생성됩니다. 어느 형식이든 생성 파일의 값을 그대로 복사합니다.

연결 후 새 대화에서 다음처럼 확인합니다.

```text
연결한 규정 MCP의 search 도구로 인사규정을 찾아줘.
첫 번째 검색 결과의 id를 fetch 도구에 넣어 원문과 출처를 보여줘.
```

서버 이름만 보이는 것으로는 충분하지 않습니다. `search`가 결과를 반환하고, 그 결과의
`id`를 사용한 `fetch`가 원문을 반환해야 연결이 완료된 것입니다. 실제 stdio 통합
검증에서도 생성된 `command`, `args`, `env`를 수정 없이 subprocess에 전달해 MCP
`initialize`, `tools/list`, `search`, `fetch` 순서로 성공하는 것을 확인합니다.

문제가 생기면 다음 순서로 확인합니다.

- `disconnected`: 생성 파일의 `command`, 모든 `args`, `env`가 빠짐없이 복사됐는지 확인
- `Python was not found`: 번들 폴더에서 `run_mcp_stdio_server.ps1`을 직접 실행하지 말고
  stderr에 표시된 Python 버전·marker·project root·import 진단을 확인
- 서버가 목록에 없음: JSON 쉼표와 중괄호를 확인하고 Claude Desktop을 완전히 종료·재실행
- 도구가 0개: 새 대화에서 서버를 활성화했는지 확인한 뒤 `doctor_mcp_connection.ps1`과
  `validate_mcp_smoke.ps1` 실행
- 폴더를 옮긴 뒤 실패: 이전 절대경로를 고치지 말고 새 위치에서 번들을 다시 생성

Windows 설정 경로와 재시작 절차는 공식
[MCP 로컬 서버 연결 문서](https://modelcontextprotocol.io/docs/develop/connect-local-servers)를
따릅니다.

### ChatGPT Desktop

수동 입력이 필요하면 `chatgpt_desktop_local_mcp.json`의 `ui_fields`를 `Settings > MCP servers > Add server`에 입력합니다. `command`, 모든 `args`, `cwd`, `env`가 빠짐없이 들어가야 합니다. 이 값은 위 공용 `config.toml` 항목과 같은 서버 계약입니다.

## 3. Vercel HTTPS MCP

로컬 전체 `data/`를 배포하지 말고 승인 runtime만 담은 staging 디렉터리를 만듭니다.

```powershell
reg-rag-mcp-vercel-stage `
  --runtime-data-dir .\reports\mcp_connection_bundle\data `
  --out-dir .\vercel-mcp-stage
```

staging 디렉터리를 Vercel에 배포합니다. 진입점은 `vercel_mcp.py`, 최종 endpoint는 다음과 같습니다.

```text
https://<deployment>/mcp
```

하나의 Vercel 배포와 하나의 `/mcp` endpoint를 ChatGPT Desktop·Codex와 Claude가 함께
사용합니다. 클라이언트별 서버를 따로 만들지 않습니다. ChatGPT Desktop은
`Settings > MCP servers > Add server`에서 Streamable HTTP를 선택하거나 공용
`~/.codex/config.toml`에 URL을 등록합니다. Claude는 해당 Connector 등록 화면에 같은 URL을
입력합니다.

ChatGPT Desktop·Codex 공용 설정의 원격 항목은 다음 두 값이 핵심입니다.

```toml
[mcp_servers.<이름>]
url = "https://<deployment>/mcp"
bearer_token_env_var = "MCP_AUTH_TOKEN"
```

공개 무인증 endpoint라면 `bearer_token_env_var`를 생략합니다. Claude Code는 생성된
`claude_code_add_http.ps1`을 실행해 같은 URL을 user scope로 등록할 수 있습니다.

Vercel 환경변수:

- 공개 read-only MCP: `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`, `MCP_AUTH_TOKEN`은 비움
- ChatGPT Desktop·Codex를 포함해 bearer를 지원하는 MCP 클라이언트: Vercel Secret
  `MCP_AUTH_TOKEN`과 클라이언트 설정의 `bearer_token_env_var`
- `MCP_TENANT_ID` (manifest와 다른 값을 넣으면 시작 거부)
- `MCP_PROFILE_ID` (manifest에 profile이 있으면 일치해야 함)
- `MCP_ALLOWED_HTTP_HOSTS` (사용자 도메인을 추가할 때)

배포 후 클라이언트에는 최종 HTTPS `/mcp` 주소와 승인된 인증만 등록합니다. 공개 규정
endpoint는 보안 검토 후 명시적 무인증 read-only 모드로 배포할 수 있습니다. 비공개
ChatGPT Desktop·Codex 연결은 토큰 값을 설정 파일에 직접 쓰지 않고 환경변수 이름만
`bearer_token_env_var`로 등록하거나 OAuth를 사용합니다. ChatGPT 웹의 hosted plugin 연결은
이 Codex-host MCP 설정과 별도이며 이 번들의 자동 연결 범위가 아닙니다.

자세한 배포 절차는 [Vercel HTTPS MCP 배포](vercel_https_mcp_ko.md)를 참고합니다.

## 4. 검증

로컬 진단:

```powershell
.\doctor_mcp_connection.ps1
.\validate_mcp_smoke.ps1
```

연결 완료는 설정 파일 생성만으로 판단하지 않습니다. 클라이언트를 재시작한 뒤 서버가 보이고 `tools/list`가 성공하며, 실제 `search` 결과의 id를 `fetch`로 조회할 수 있어야 합니다.

## 5. 보안

- 승인된 청크만 runtime bundle에 포함합니다.
- 원본 문서, 로컬 경로, 토큰, API 키를 공개 저장소나 대화에 넣지 않습니다.
- 토큰은 환경변수 또는 secret manager에만 둡니다.
- Vercel에는 로컬 전체 corpus가 아니라 검토된 최소 runtime만 배포합니다.
- remote MCP 응답이 외부 AI 서비스로 전달될 수 있으므로 반출 승인 범위를 확인합니다.

## 공식 참고

- OpenAI ChatGPT Desktop·Codex MCP:
  https://learn.chatgpt.com/docs/extend/mcp
- Anthropic Claude Code MCP:
  https://code.claude.com/docs/en/mcp
- Anthropic Claude remote custom connectors:
  https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Vercel MCP 배포:
  https://vercel.com/docs/mcp/deploy-mcp-servers-to-vercel

HWP/HWPX 문서 구조와 표 추출 교차 검증에는 Kordoc을 사용했습니다.
