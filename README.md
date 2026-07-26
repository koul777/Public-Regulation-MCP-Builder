# PR MCP Builder

[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows11&logoColor=white)](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-local%20stdio%20%7C%20HTTPS-0F766E)](docs/mcp_quickconnect_ko.md)
[![Latest release](https://img.shields.io/github/v/release/koul777/Public-Regulation-MCP-Builder?display_name=tag&sort=semver)](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

PDF, HWP, HWPX, DOCX 규정을 읽어 구조화하고, 승인된 규정만 MCP 데이터로 생성해서 ChatGPT, Codex, Claude가 `search`와 `fetch`로 조회할 수 있게 준비하는 Windows 중심 도구입니다.

> [!IMPORTANT]
> 현재 개발 중인 공개 소스 기반 개발판입니다. Windows 10/11 64비트 우선 지원입니다. Streamlit 화면은 로컬 운영자용이며 완성형 SaaS 화면이 아닙니다. 사람에게 승인되지 않은 청크는 검색 결과와 MCP 응답에 포함하지 않습니다.

## 한눈에 보기

이 프로그램은 아래 순서로 동작합니다.

1. 규정 파일을 읽어 기관, 규정명, 개정 이력, 목차, 조문 구조를 정리합니다.
2. 운영자가 결과를 검토하고 사람 검수 단계를 거칩니다.
3. 승인된 규정만 MCP 데이터로 생성합니다.
4. ChatGPT Desktop / Codex Desktop·CLI·IDE / Claude Desktop / Claude Code 또는 `https://<deployment>/mcp` 원격 주소에 연결할 수 있는 파일을 만듭니다.

핵심은 “문서를 넣으면 바로 공개 검색용 데이터가 된다”가 아닙니다. 전처리 결과는 검토용 미리보기이고, 최종 MCP에는 승인 완료 데이터만 반영됩니다.

## 어떤 연결을 만들 수 있나

연결 방식은 두 가지뿐입니다.

| 사용 위치 | 방식 | 사용 값 |
| --- | --- | --- |
| ChatGPT Desktop / Codex Desktop / Codex CLI / Codex IDE | 로컬 stdio | `chatgpt_desktop_local_mcp.json`의 `ui_fields` 또는 `codex_config_snippet.toml` |
| Claude Desktop | 로컬 stdio | `claude_desktop_config.json` |
| Claude Code | 로컬 stdio | `claude_code_add_stdio.ps1` |
| ChatGPT / Codex / Claude 원격 연결 | Vercel HTTPS | 배포 후 최종 `https://<deployment>/mcp` |

- 로컬 stdio: 같은 PC에서 MCP 서버를 직접 실행합니다.
- Vercel HTTPS: Vercel에 배포한 뒤 `https://<deployment>/mcp` 주소로 연결합니다.

## 제일 쉬운 사용 순서

비전공자 기준으로는 아래 5단계만 보면 됩니다.

1. Windows 실행판의 압축을 풀고 `PR MCP Builder.exe`를 실행합니다.
2. 기관을 선택하고 규정 파일을 올린 뒤 원문과 처리 결과를 비교합니다.
3. 사용할 규정을 승인하고 `MCP로 쓸 파일 묶음 만들기`를 누릅니다.
4. 생성 완료 화면에 나온 값을 AI 프로그램 설정에 그대로 입력합니다.
5. 저장 후 AI 프로그램을 완전히 종료·재실행하고, 새 대화에서 `search`와 `fetch`를 호출합니다.

잘 안 붙는 대부분의 원인은 아래 둘입니다.

- 서버 이름을 `Command` 칸에 넣은 경우
- `Arguments`를 일부 빼먹은 경우

로컬 stdio는 “폴더만 지정”해서 끝나는 방식이 아닙니다. `command`, `args`, `cwd`, `env`를 생성 화면에 나온 그대로 넣어야 합니다.

## 생성 완료 화면 그대로 입력하기

경로나 서버 이름을 예시에서 가져올 필요가 없습니다. 생성 완료 화면은 방금 만든 `chatgpt_desktop_local_mcp.json`의 `ui_fields`를 읽어 실제 값을 보여 줍니다.

> [!WARNING]
> MCP 서버 이름은 **Name에만** 입력합니다. Command에는 서버 이름을 넣지 마세요. Arguments는 한 입력 칸에 하나씩 순서대로 모두 넣어야 하며, 하나라도 빠지면 서버가 실행되지 않습니다.

### ChatGPT Desktop / Codex Desktop

`Settings > MCP servers > Add server`에서 생성 완료 화면의 **ChatGPT/Codex Desktop에 등록하는 방법**을 보며 입력합니다.

| 설정 칸 | 넣을 값 |
| --- | --- |
| Name | 화면에 표시된 MCP 서버 이름 |
| Transport | `STDIO` |
| Command | `ui_fields.command` |
| Working directory | `ui_fields.cwd` |
| Arguments | `ui_fields.args`를 번호 순서대로 한 칸에 하나씩 |
| Environment | 화면에 `입력하지 않음`이 보이면 비워 둠 |
| Environment passthrough | 화면에 `입력하지 않음`이 보이면 비워 둠 |

Command, Working directory와 Arguments 전체 목록은 화면의 복사 영역을 사용하면 됩니다. 각 인자도 번호가 붙은 목록에서 하나씩 복사할 수 있습니다.

### Codex CLI / Codex IDE

생성된 `codex_config_snippet.toml`의 MCP 항목을 공용 `~/.codex/config.toml`에 반영합니다. 파일 안의 서버 이름과 경로는 방금 만든 번들 기준이므로 예시값으로 바꾸지 않습니다.

### Claude Desktop

- 위치: `설정 > 개발자 > 로컬 MCP 서버 > 구성 편집`
- 영문 UI: `Settings > Developer > Edit Config`
- 실제 파일: `%APPDATA%\Claude\claude_desktop_config.json`
- 번들에 들어 있는 `claude_desktop_config.json`의 `mcpServers` 항목만 기존 설정에 병합합니다.
- 등록 후에는 Claude Desktop을 완전히 종료한 뒤 다시 실행합니다.
- 새 대화에서 `파일·커넥터 추가 > Connectors`를 열어 서버가 보이는지 확인합니다.

### Claude Code

- 사용 파일: `claude_code_add_stdio.ps1`
- 역할: 공식 `claude mcp add --transport stdio --scope user` 등록을 대신 실행합니다.

### ChatGPT / Codex / Claude 원격 HTTPS

- 사용 값: Vercel 배포 후 최종 `https://<deployment>/mcp`
- 로컬 폴더 경로나 stdio 인자는 넣지 않습니다.
- ChatGPT는 MCP 설정 화면에 URL을 넣고, Claude 웹은 `Customize > Connectors`에서 custom connector를 추가합니다.
- 인증이 필요한 경우 `bearer_token_env_var` 또는 서버 측 인증 구성을 함께 사용합니다.

자세한 예시는 [MCP 빠른 연결 안내](docs/mcp_quickconnect_ko.md), [Vercel HTTPS MCP 배포 안내](docs/vercel_https_mcp_ko.md), 공식 [MCP 로컬 서버 문서](https://modelcontextprotocol.io/docs/develop/connect-local-servers)를 참고하세요.

## 필요할 때 확인하는 생성 파일

대부분은 생성 완료 화면의 안내만 따르면 됩니다. 설정을 직접 확인해야 할 때는 아래 파일을 사용합니다.

| 파일 | 쓰는 곳 |
| --- | --- |
| `chatgpt_desktop_local_mcp.json` | ChatGPT Desktop 수동 입력 기준 |
| `codex_config_snippet.toml` | Codex Desktop / Codex CLI / Codex IDE |
| `claude_desktop_config.json` | Claude Desktop 설정 병합 |
| `claude_code_add_stdio.ps1` | Claude Code 로컬 stdio 등록 |
| `claude_code_add_http.ps1` | Claude Code 원격 HTTPS 등록 |
| `chatgpt_connector.json`, `claude_https_mcp.json` | 원격 HTTPS 연결 참고 |
| `connect_mcp_client.ps1` | 번들 설치/연결 보조 |
| `doctor_mcp_connection.ps1`, `validate_client_config_smoke.ps1` | 연결 점검 |

## 로컬 실행

### Windows 실행판

1. [최신 GitHub Release](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)에서 Windows ZIP을 받습니다.
2. 압축을 풉니다.
3. `PR MCP Builder.exe`를 실행합니다.

작업 데이터는 기본적으로 `%LOCALAPPDATA%\PR MCP Builder\data`에 저장됩니다.

### 소스 코드 실행

- `Python 3.11 이상`이 필요합니다.
- 가장 쉬운 시작 방법은 `START_HERE.bat`입니다.
- 직접 실행하려면 아래 명령을 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m streamlit run frontend\streamlit_app.py --server.address 127.0.0.1
```

소스 코드 실행 시 작업 데이터는 프로젝트 폴더의 `data\` 아래에 저장됩니다.

실행 조건만 확인하려면 아래 명령을 사용할 수 있습니다.

```powershell
.\START_HERE.bat --check
```

## 문서 처리와 검토 원칙

- 전처리 결과는 운영자 검토용입니다.
- 사람 검수와 승인 없이 바로 공식 검색 데이터가 되지 않습니다.
- 사람에게 승인되지 않은 청크는 MCP 응답과 검색 결과에서 제외됩니다.
- 승인된 규정만 MCP 데이터로 생성되며, 최신 유효본과 개정 이력은 분리해서 관리합니다.
- 동일 기관 안에서도 규정명, 버전, 조문 구조를 기준으로 이력을 묶습니다.

## Vercel HTTPS 배포

원격 연결이 필요하면 로컬 번들에서 만든 승인 데이터만 별도 staging 폴더로 내보내 Vercel에 배포합니다.

```powershell
reg-rag-mcp-vercel-stage --runtime-data-dir .\reports\mcp_connection_bundle\data --out-dir .\vercel-mcp-stage
```

- 최종 연결 주소는 `https://<deployment>/mcp` 입니다.
- 공개 read-only MCP라면 서버 측에서 `MCP_ALLOW_UNAUTHENTICATED_HTTP=true`를 명시할 수 있습니다.
- 비공개 운영이면 bearer token 또는 별도 인증 구성을 사용합니다.
- 원격 MCP는 승인된 규정 응답이 외부 AI 서비스로 전달될 수 있으므로, 공개 가능 범위인지 먼저 결정해야 합니다.

## 선택 사항: Kordoc

HWP 보강이 필요하면 [Kordoc 프로젝트](https://github.com/chrisryugj/kordoc)를 별도로 설치해 함께 사용할 수 있습니다.

- Kordoc 설치·실행 파일은 포함하지 않음
- Kordoc 소스나 실행 파일이 포함되지 않음
- 라이선스: <https://github.com/chrisryugj/kordoc/blob/main/LICENSE>
- 제3자 고지: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

## 보안과 공개 저장소 원칙

- 전처리 자체를 보안 통제로 간주하지 않습니다.
- 원문 문서, 비밀값, 기관 내부 식별자, 로컬 경로는 공개 저장소와 MCP 응답에 넣지 않습니다.
- 공개 배포 전에는 [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [docs/public_repository_history_policy_ko.md](docs/public_repository_history_policy_ko.md)를 확인해야 합니다.

## 개발자용 검증과 빌드

```powershell
python -m unittest discover -s tests -v
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

Windows 실행 ZIP은 아래 명령으로 빌드합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows_portable.ps1
```

최신 배포와 다운로드는 [releases/latest](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)에서 확인할 수 있습니다.
