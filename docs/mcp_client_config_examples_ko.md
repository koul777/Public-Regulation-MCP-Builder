# MCP 클라이언트 직접 설정 예시

이 저장소는 MCP 연결용 BAT 파일이나 에이전트 프롬프트를 생성하지 않습니다. 로컬은 표준 stdio 실행 계약을, 원격은 HTTPS Streamable HTTP endpoint를 직접 등록합니다.

## 로컬 stdio 계약

MCP 클라이언트가 필요한 값은 디렉터리명이 아니라 `command`, `args`, `cwd`, `env`입니다.

```json
{
  "command": "powershell.exe",
  "args": [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    "C:/MCP/aks_mcp/run_mcp_stdio_server.ps1"
  ],
  "cwd": "C:/MCP/aks_mcp",
  "env": {}
}
```

실제 생성물에서는 tenant, profile, tool profile, data 경로 인수가 추가됩니다. `chatgpt_desktop_local_mcp.json`의 `ui_fields`가 현재 번들의 완전한 입력값입니다.

### ChatGPT Desktop / Codex CLI / Codex IDE

```toml
[mcp_servers.aks_mcp]
command = "powershell.exe"
args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:/MCP/aks_mcp/run_mcp_stdio_server.ps1"]
cwd = "C:/MCP/aks_mcp"
```

세 제품이 공유하는 `~/.codex/config.toml`에는 생성된 `codex_config_snippet.toml`을 사용합니다. 수동으로 축약한 예시를 운영 설정에 복사하지 않으며, 직접 MCP 등록 전에 플러그인 목록을 검사할 필요가 없습니다.

### Claude Desktop

```json
{
  "mcpServers": {
    "aks_mcp": {
      "command": "powershell.exe",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "C:/MCP/aks_mcp/run_mcp_stdio_server.ps1"
      ],
      "cwd": "C:/MCP/aks_mcp",
      "env": {}
    }
  }
}
```

기존 설정의 다른 `mcpServers` 항목을 보존하면서 생성된 `claude_desktop_config.json`의 항목만 병합합니다.

### Claude Code

```powershell
.\claude_code_add_stdio.ps1
claude mcp get aks_mcp
```

### ChatGPT Desktop

수동 입력이 필요하면 `chatgpt_desktop_local_mcp.json`의 `ui_fields`를 `Settings > MCP servers > Add server`에 그대로 입력합니다. 일부 argument만 넣거나 폴더만 지정하면 서버가 시작되지 않습니다.

## Vercel HTTPS Streamable HTTP

배포 준비:

```powershell
reg-rag-mcp-vercel-stage `
  --runtime-data-dir .\reports\mcp_connection_bundle\data `
  --out-dir .\vercel-mcp-stage
```

Connector URL:

```text
https://<deployment>/mcp
```

ChatGPT Desktop·Codex 공용 설정:

```toml
[mcp_servers.aks_mcp]
url = "https://<deployment>/mcp"
bearer_token_env_var = "MCP_AUTH_TOKEN"
```

공개 무인증 endpoint라면 `bearer_token_env_var` 줄을 생략합니다.

Claude Code는 생성된 `claude_code_add_http.ps1`을 실행합니다. 이 파일은 공식 CLI의
`claude mcp add --transport http --scope user` 형식으로 같은 URL을 등록합니다.

승인된 공개 read-only endpoint는 `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`를 명시하고
`MCP_AUTH_TOKEN`을 비웁니다. 비공개 ChatGPT Desktop·Codex 연결은 Vercel Secret
`MCP_AUTH_TOKEN`을 두고 공용 `config.toml`의 `bearer_token_env_var`에 환경변수 이름만
등록하거나 OAuth를 사용합니다. ChatGPT Desktop에서는 `Settings > MCP servers > Add server`의
Streamable HTTP URL로 같은 endpoint를 등록할 수도 있습니다. 필요하면 manifest와 일치하는
`MCP_TENANT_ID`, `MCP_PROFILE_ID`, 사용자 도메인용 `MCP_ALLOWED_HTTP_HOSTS`를 설정합니다.
로컬 전체 `data/`, raw upload, trace, export, 비밀값은 배포하지 않습니다.

## 검증

```powershell
.\doctor_mcp_connection.ps1
.\validate_mcp_smoke.ps1
```

설정 후 클라이언트를 재시작하고 `tools/list`, `search`, `fetch`가 실제로 성공하는지 확인합니다.
