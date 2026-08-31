# Vercel HTTPS MCP 배포

이 저장소의 MCP는 Vercel Python Function에서 HTTPS Streamable HTTP로 실행할 수 있다.
Vercel용 엔드포인트는 `/mcp`이며 서버리스 인스턴스 간 세션을 공유하지 않도록
stateless HTTP와 JSON 응답을 사용한다.

## 전제

- 전체 `data/`를 배포하지 않는다.
- 운영자의 명시적 승인·색인이 끝난 MCP runtime bundle만 사용한다. 사람 검토는 권고하며, 미검수 승인은 사유와 감사 이벤트가 포함된 경우에만 승인 데이터로 취급한다.
- raw upload, 전처리 노드·이슈·품질 파일, 운영 보고서, 비밀값을 포함하지 않는다.
- Vercel Function의 로컬 파일 시스템은 영속 감사 저장소로 사용하지 않는다.

## 배포 디렉터리 준비

승인 runtime bundle의 `data/` 폴더를 입력으로 지정한다.

```powershell
python scripts\prepare_vercel_mcp_deployment.py `
  --runtime-data-dir reports\mcp_connection_bundle\data `
  --out-dir vercel-mcp-stage
```

출력 디렉터리에는 MCP 실행에 필요한 `app/`, `api/index.py` ASGI entrypoint, Vercel 설정,
승인 runtime만 복사된다. 기존 출력 디렉터리는 덮어쓰지 않는다.

## 처음 배포하는 사람: 홈페이지와 명령창의 역할

Vercel 홈페이지에서는 계정 가입, 프로젝트 확인, 환경 변수와 로그 확인을 한다. 로컬 PC에
있는 승인 runtime을 처음 올릴 때는 PowerShell에서 Vercel CLI를 사용하는 것이 가장
단순하다. GitHub 저장소 전체나 원본 `data/` 폴더를 Vercel 홈페이지에 직접 업로드하지
않는다.

![승인 번들을 Vercel에 배포하고 Claude 커넥터에 등록한 뒤 search와 fetch로 검증하는 순서](assets/readme-vercel-claude-connection.svg)

1. https://vercel.com 에 가입하고 로그인한다.
2. PowerShell에서 CLI를 한 번 설치하고 로그인한다.

```powershell
npm install -g vercel
vercel login
```

3. 위 절차로 `vercel-mcp-stage`를 만든 뒤 프로젝트를 만들고 연결한다. 프로젝트 이름은
   영문 소문자, 숫자와 하이픈으로 정한다.

```powershell
vercel project add <프로젝트-이름>
vercel link --yes --project <프로젝트-이름> --cwd .\vercel-mcp-stage
```

4. 공개해도 되는 승인 규정만 담긴 read-only 배포라면 다음 값을 Production에 넣는다.

```powershell
vercel env add MCP_ALLOW_UNAUTHENTICATED_HTTP production --value "true" --yes --cwd .\vercel-mcp-stage
vercel env add MCP_TOOL_PROFILE production --value "chatgpt-data" --yes --cwd .\vercel-mcp-stage
```

기관 내부 자료처럼 공개하면 안 되는 데이터에는 이 공개 모드를 사용하지 않는다. 그런
경우 bearer 인증이나 OAuth를 먼저 구성한다.

## 환경 변수

다음 배포 모드 중 하나를 명시해야 하며, 둘을 혼용하지 않는다.

- ChatGPT 웹·Codex와 Claude가 함께 쓰는 승인된 공개 read-only endpoint:
  `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`, `MCP_AUTH_TOKEN`은 비움
- 비공개 Codex endpoint: `MCP_AUTH_TOKEN`을 Vercel Secret으로 등록하고
  `config.toml`의 `bearer_token_env_var`에 환경변수 이름만 설정. ChatGPT 웹은
  워크스페이스가 승인한 OAuth 정책 사용
- `MCP_AUTH_ISSUER_URL`: 사용자 도메인을 쓸 때 `https://mcp.example.go.kr`
- `MCP_ALLOWED_HTTP_HOSTS`: 사용자 도메인을 쓸 때 `mcp.example.go.kr`
- `MCP_ALLOWED_HTTP_ORIGINS`: 신뢰하는 Origin이 실제 전송될 때만 쉼표로 구분해 등록
- `MCP_TENANT_ID`, `MCP_PROFILE_ID`: 생략하면 runtime manifest 값을 사용
- `MCP_TOOL_PROFILE`: 기본값 `chatgpt-data`; 원격 공개 범위는 읽기 전용 `list_regulations`, `get_regulation_toc`, `get_regulation_article`, `get_regulation_references`, `list_regulation_reference_cycles`, `search`, `fetch`

현재 adapter의 `MCP_AUTH_TOKEN`은 bearer 인증을 지원하는 Codex와 일반 MCP
클라이언트에서 사용할 수 있다. 토큰 값은 Vercel Secret과 로컬 환경변수에만 두고
`config.toml`에는 `bearer_token_env_var = "MCP_AUTH_TOKEN"`처럼 환경변수 이름만 기록한다.
OAuth를 선택하는 경우 authorization server, protected-resource metadata, PKCE,
audience/scope 검증은 별도로 구성해야 한다. ChatGPT 웹의 원격 MCP 앱 등록은 이
staging 도구가 자동 수행하지 않으며, Developer mode·플랜·관리자 권한을 따로 확인한다.

## 배포와 확인

```powershell
vercel --cwd vercel-mcp-stage
vercel --prod --cwd vercel-mcp-stage
```

명령 마지막의 `Aliased https://<프로젝트-이름>.vercel.app`이 고정 Production 주소다.
배포마다 생기는 긴 미리보기 주소보다 이 고정 주소 뒤에 `/mcp`를 붙여 등록한다.

클라이언트에는 다음 주소를 Streamable HTTP MCP로 등록한다.

```text
https://<production-host>/mcp
```

이 endpoint 하나를 ChatGPT 웹·Codex와 Claude에서 공통으로 사용할 수 있다. 클라이언트별
Vercel 프로젝트를 따로 만들 필요는 없다. ChatGPT 웹은 Developer mode의 Apps 설정에서
원격 MCP 앱을 만들고, Codex는 `config.toml`, Claude는 해당 Connector 등록 화면에 같은
URL을 등록한다. ChatGPT는 로컬 MCP에 직접 연결하지 않는다.

Codex 원격 설정 예시:

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

승인된 공개 read-only endpoint는 저장소의 검증 명령으로 네 단계를 한 번에 확인할 수
있다.

```powershell
python scripts\run_mcp_client_config_smoke.py `
  --remote-url "https://<프로젝트-이름>.vercel.app/mcp" `
  --allow-unauthenticated-remote `
  --timeout-seconds 120 `
  --fail-on-issue
```

결과에서 `mcp_initialized`, `tools_discovered`, `end_to_end_verified`가 모두 `true`이고
`tool_names`에 `list_regulations`, `get_regulation_toc`, `get_regulation_article`,
`get_regulation_references`, `list_regulation_reference_cycles`, `search`, `fetch`가 있어야 한다.
이어서 목록 1페이지, 첫 규정의 목차·조문·참조, 순환참조 목록, 검색 결과의 `fetch`
원문까지 실제 호출로 확인한다.

공개 read-only 주소가 정해진 뒤 Claude용 복사 파일을 만들려면 다음처럼 명시적으로 공개
모드를 선택한다.

```powershell
python scripts\generate_mcp_client_config.py `
  --server-name "<MCP-이름>" `
  --client-profile claude-remote `
  --transport streamable-http `
  --public-url "https://<프로젝트-이름>.vercel.app/mcp" `
  --approved-public-unauthenticated `
  --out-json claude_https_mcp.json
```

## Claude에 주소 등록

1. Claude 웹 또는 Desktop에서 **설정 > 커넥터(Connectors)**를 연다.
2. **사용자 지정 커넥터 추가(Add custom connector)**를 누른다.
3. 이름에는 알아보기 쉬운 MCP 이름을, URL에는 고정 Production 주소와 `/mcp`를 입력한다.
4. 공개 read-only 배포는 별도 토큰을 입력하지 않는다. 비공개 배포는 구성한 OAuth 흐름을
   따른다.
5. 저장한 뒤 새 대화를 열고 커넥터를 활성화한다.
6. `규정 MCP의 search로 인사규정을 찾고 첫 결과 id를 fetch로 조회해 줘`라고 요청한다.

로컬 Claude Desktop의 **개발자 > 구성 편집**은 PowerShell/Python STDIO 연결용이다.
Vercel HTTPS 주소는 그 JSON 파일이 아니라 **커넥터** 화면에 등록한다.

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
- OpenAI ChatGPT Developer mode·MCP:
  https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta
- OpenAI Secure MCP Tunnel:
  https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- Anthropic Claude remote custom connectors:
  https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
