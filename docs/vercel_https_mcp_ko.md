# Vercel HTTPS MCP 배포

이 저장소의 MCP는 Vercel Python Function에서 HTTPS Streamable HTTP로 실행할 수 있다.
Vercel용 엔드포인트는 `/mcp`이며 서버리스 인스턴스 간 세션을 공유하지 않도록
stateless HTTP와 JSON 응답을 사용한다.

## 전제

- 전체 `data/`를 배포하지 않는다.
- 사람 검토와 승인·색인이 끝난 MCP runtime bundle만 사용한다.
- raw upload, 전처리 노드·이슈·품질 파일, 운영 보고서, 비밀값을 포함하지 않는다.
- Vercel Function의 로컬 파일 시스템은 영속 감사 저장소로 사용하지 않는다.

## 배포 디렉터리 준비

승인 runtime bundle의 `data/` 폴더를 입력으로 지정한다.

```powershell
python scripts\prepare_vercel_mcp_deployment.py `
  --runtime-data-dir reports\mcp_connection_bundle\data `
  --out-dir vercel-mcp-stage
```

출력 디렉터리에는 MCP 실행에 필요한 `app/`, ASGI entrypoint, Vercel 설정,
승인 runtime만 복사된다. 기존 출력 디렉터리는 덮어쓰지 않는다.

## 환경 변수

다음 배포 모드 중 하나를 명시해야 하며, 둘을 혼용하지 않는다.

- ChatGPT Desktop·Codex와 Claude가 함께 쓰는 승인된 공개 read-only endpoint:
  `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`, `MCP_AUTH_TOKEN`은 비움
- 비공개 ChatGPT Desktop·Codex endpoint: `MCP_AUTH_TOKEN`을 Vercel Secret으로 등록하고
  공용 `config.toml`의 `bearer_token_env_var`에 환경변수 이름만 설정하거나 OAuth 사용
- `MCP_AUTH_ISSUER_URL`: 사용자 도메인을 쓸 때 `https://mcp.example.go.kr`
- `MCP_ALLOWED_HTTP_HOSTS`: 사용자 도메인을 쓸 때 `mcp.example.go.kr`
- `MCP_ALLOWED_HTTP_ORIGINS`: 신뢰하는 Origin이 실제 전송될 때만 쉼표로 구분해 등록
- `MCP_TENANT_ID`, `MCP_PROFILE_ID`: 생략하면 runtime manifest 값을 사용
- `MCP_TOOL_PROFILE`: 기본값 `chatgpt-data`; 원격 공개 범위는 `search`, `fetch`만 권장

현재 adapter의 `MCP_AUTH_TOKEN`은 bearer 인증을 지원하는 ChatGPT Desktop·Codex와 일반 MCP
클라이언트에서 사용할 수 있다. 토큰 값은 Vercel Secret과 로컬 환경변수에만 두고
`config.toml`에는 `bearer_token_env_var = "MCP_AUTH_TOKEN"`처럼 환경변수 이름만 기록한다.
OAuth를 선택하는 경우 authorization server, protected-resource metadata, PKCE,
audience/scope 검증은 별도로 구성해야 한다. ChatGPT 웹의 hosted plugin 연결은 이
Codex-host 설정과 별도이며 이 staging 도구의 자동 연결 범위가 아니다.

## 배포와 확인

```powershell
vercel --cwd vercel-mcp-stage
vercel --prod --cwd vercel-mcp-stage
```

클라이언트에는 다음 주소를 Streamable HTTP MCP로 등록한다.

```text
https://<production-host>/mcp
```

이 endpoint 하나를 ChatGPT Desktop·Codex와 Claude에서 공통으로 사용한다. 클라이언트별
Vercel 프로젝트를 따로 만들 필요는 없다. ChatGPT Desktop은 `Settings > MCP servers >
Add server`의 Streamable HTTP URL 또는 공용 `config.toml`에 등록하고, Claude는 해당
Connector 등록 화면에 같은 URL을 등록한다.

ChatGPT Desktop·Codex 공용 설정 예시:

```toml
[mcp_servers.aks_mcp]
url = "https://<production-host>/mcp"
bearer_token_env_var = "MCP_AUTH_TOKEN"
```

공개 무인증 endpoint는 `bearer_token_env_var`를 생략한다. Claude Code는 생성 번들의
`claude_code_add_http.ps1`을 실행하면 같은 URL이 user scope에 등록된다.

배포 후 MCP Inspector 또는 클라이언트에서 차례로 확인한다.

1. `initialize`
2. `tools/list`
3. `search`
4. `fetch`

## 운영 제약

Vercel adapter는 승인 데이터 파일에 trace나 API audit을 기록하지 않는다. Vercel 로그는
진단에는 사용할 수 있지만 기관용 영속 감사 저널을 대신한다고 간주하면 안 된다. 운영
요건에 영속 감사가 포함되면 외부 감사 저장소를 연동하거나 컨테이너 기반 상주 배포를
선택한다.

Python 의존성과 승인 runtime을 합친 Function bundle 크기를 확인한다. staging 도구는
Vercel의 표준 500 MB 비압축 Python Function 한도에 애플리케이션과 의존성이 들어갈
여유를 남기기 위해 runtime을 기본 400 MiB로 제한한다. 이를 넘으면 Large Functions의
현재 적용 조건을 확인하거나 승인 runtime을 외부 읽기 전용 저장소로 분리한다.

## 공식 참고

- Vercel MCP 배포: https://vercel.com/docs/mcp/deploy-mcp-servers-to-vercel
- Vercel Python Functions:
  https://vercel.com/docs/functions/runtimes/python
- OpenAI ChatGPT Desktop·Codex MCP:
  https://learn.chatgpt.com/docs/extend/mcp
- Anthropic Claude remote custom connectors:
  https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
