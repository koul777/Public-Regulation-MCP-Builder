# PR MCP Builder

[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows11&logoColor=white)](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-STDIO%20%7C%20HTTPS-0F766E)](docs/mcp_quickconnect_ko.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![규정 문서를 사람이 검토·승인한 뒤 로컬 AI와 HTTPS MCP로 연결하는 PR MCP Builder](docs/assets/pr-mcp-builder-hero.png)

공공기관 규정 파일을 정리하고, **사람이 확인해 승인한 내용만** ChatGPT·Codex·Claude에서 검색하게 만드는 Windows용 프로그램입니다.

PDF·HWP·HWPX·DOCX 파일을 올리면 규정명, 개정판, 목차와 조문을 정리합니다. 처리 결과를 사람이 원문과 비교해 승인하면 AI 프로그램에서 사용할 MCP 검색 도구를 만듭니다.

> [!IMPORTANT]
> 문서를 올렸다고 바로 AI 검색에 공개되지 않습니다. 승인하지 않은 내용은 MCP의 `search`와 `fetch` 결과에 포함하지 않습니다.

## 5단계로 사용하기

1. [최신 Windows 실행판](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)을 내려받아 압축을 풉니다.
2. `PR MCP Builder.exe`를 실행하고 기관과 규정 파일을 선택합니다.
3. 정리된 조문을 원문과 비교한 뒤 사용할 내용을 승인합니다.
4. `④ MCP 생성·AI 연결`에서 MCP 이름과 연결 방식을 선택해 파일 묶음을 만듭니다.
5. 생성 완료 화면의 값을 AI 프로그램에 입력하고, 새 대화에서 `search`와 `fetch`를 실행합니다.

## 화면으로 보는 작업 순서

### 1. 기관 선택

기관을 만들거나 기존 기관을 선택합니다. 문서와 승인 데이터는 선택한 기관 범위로 분리됩니다.

![기관을 등록하거나 선택하는 시작 화면](docs/assets/readme-guide-01-start.png)

### 2. 규정 파일 올리기

PDF·HWP·HWPX·DOCX 규정 파일을 한 번에 올릴 수 있습니다.

![규정 파일을 올리는 화면](docs/assets/readme-guide-02-upload.png)

자동 인식된 규정명, 버전과 개정일을 확인한 뒤 전처리를 시작합니다.

![자동 인식된 규정 정보를 확인하는 화면](docs/assets/readme-guide-02-preprocess-complete.png)

### 3. 처리 결과 확인

품질 결과와 함께 처리할 규정을 선택합니다. 여러 규정을 하나의 작업 묶음으로 유지할 수 있습니다.

![여러 규정의 처리 결과와 품질을 확인하는 화면](docs/assets/readme-guide-03-multi-regulation.png)

원문, 전처리 결과와 앞뒤 문맥을 비교해 내용이 올바르게 정리됐는지 확인합니다.

![원문과 전처리 결과 및 앞뒤 문맥을 비교하는 화면](docs/assets/readme-guide-03-chunk-context.png)

### 4. 사람 검수와 승인

AI 제안은 참고용입니다. 사람이 원문을 확인하고 승인한 내용만 검색 색인과 MCP에 들어갑니다.

![AI 제안을 검토하고 사람이 승인하는 화면](docs/assets/readme-guide-04-human-review.png)

## 어떤 연결을 선택하나요?

| 사용하려는 곳 | 선택할 방식 | 입력할 것 |
| --- | --- | --- |
| 같은 PC의 ChatGPT/Codex Desktop | 로컬 STDIO | 생성 화면의 Name·Command·Working directory·Arguments |
| 같은 PC의 Codex CLI/IDE | 로컬 STDIO | `codex_config_snippet.toml` |
| 같은 PC의 Claude Desktop | 로컬 STDIO | `claude_desktop_config.json` |
| 같은 PC의 Claude Code | 로컬 STDIO | `claude_code_add_stdio.ps1` |
| 웹이나 여러 기기의 ChatGPT·Codex·Claude | Vercel HTTPS | 배포된 `https://<deployment>/mcp` 주소 |

- **로컬 STDIO**는 같은 PC에서 사용합니다. 인터넷에 MCP 서버를 공개할 필요가 없습니다.
- **Vercel HTTPS**는 웹이나 여러 기기에서 쓸 때 선택합니다. 로컬 폴더 대신 배포된 HTTPS 주소를 등록합니다.

## ChatGPT/Codex Desktop에 연결하기

생성 완료 화면의 **ChatGPT/Codex Desktop에 등록하는 방법**을 보면서 `Settings > MCP servers > Add server`에 입력합니다. 표시값은 방금 생성한 `chatgpt_desktop_local_mcp.json`의 `ui_fields`에서 읽으므로 예시 경로로 바꾸지 마세요.

| 설정 칸 | 넣을 값 |
| --- | --- |
| Name | 화면에 표시된 MCP 서버 이름 |
| Transport | `STDIO` |
| Command | `ui_fields.command` |
| Working directory | `ui_fields.cwd` |
| Arguments | `ui_fields.args`를 번호 순서대로 한 칸에 하나씩 |
| Environment | `입력하지 않음`이면 비워 둠 |
| Environment passthrough | `입력하지 않음`이면 비워 둠 |

> [!WARNING]
> 서버 이름은 **Name에만** 넣습니다. Command에는 서버 이름이나 폴더 이름을 넣지 마세요. Arguments는 하나라도 빠지면 실행되지 않습니다.

Command, Working directory, Arguments 전체 목록과 각 인자는 생성 완료 화면에서 복사할 수 있습니다.

## Claude Desktop에 연결하기

1. Claude Desktop에서 **설정 > 개발자 > 로컬 MCP 서버 > 구성 편집**을 누릅니다.
2. `%APPDATA%\Claude\claude_desktop_config.json`을 엽니다.
3. 생성된 `claude_desktop_config.json`의 해당 `mcpServers` 항목을 기존 설정에 합칩니다.
4. 기존에 등록한 다른 MCP 서버 항목은 삭제하지 않습니다.
5. 저장 후 Claude Desktop을 완전히 종료하고 다시 실행합니다.
6. 새 대화에서 **파일·커넥터 추가 > Connectors**를 열어 생성한 서버 이름을 확인합니다.

`command`는 실행 프로그램이고 `args`는 순서가 있는 전체 인자입니다. 서버 이름이나 폴더 이름으로 바꾸지 마세요.

## Codex CLI·IDE와 Claude Code에 연결하기

- **Codex CLI·IDE**: `codex_config_snippet.toml`의 MCP 항목을 `~/.codex/config.toml`에 반영합니다.
- **Claude Code 로컬 연결**: `claude_code_add_stdio.ps1`을 실행합니다. 공식 `claude mcp add --transport stdio --scope user` 방식으로 등록됩니다.
- **Claude Code HTTPS 연결**: Vercel 주소가 준비된 뒤 `claude_code_add_http.ps1`을 실행합니다.

서버 이름, 경로, profile ID와 tool profile은 생성할 때마다 달라질 수 있습니다. 생성 파일의 실제 값을 그대로 사용하세요.

## Vercel HTTPS로 연결하기

Vercel에 배포한 뒤 최종 `https://<deployment>/mcp` 주소를 ChatGPT·Codex·Claude의 MCP 또는 Connector 설정에 등록합니다. 이때 로컬 Command, Working directory와 Arguments는 입력하지 않습니다.

- Claude 웹: `Customize > Connectors`에서 custom connector를 추가합니다.
- 비공개 MCP: Vercel Secret, bearer token 또는 OAuth를 함께 설정합니다.
- 공개 MCP: 공개해도 되는 승인 규정만 포함했는지 먼저 확인합니다.

처음 배포하는 방법은 [Vercel HTTPS MCP 배포 안내](docs/vercel_https_mcp_ko.md)를 따라가세요.

## 연결됐는지 확인하기

설정을 저장한 뒤 AI 프로그램을 **완전히 종료하고 다시 실행**합니다. 새 대화에서 다음 두 작업이 모두 성공해야 연결 완료입니다.

1. `search`로 규정을 검색합니다.
2. 검색 결과의 첫 `id`를 `fetch`에 넣어 해당 내용을 가져옵니다.

서버 이름만 보이거나 도구가 0개이면 아직 연결된 것이 아닙니다. `Connection closed`가 나오면 생성 완료 화면과 Command, Working directory, Arguments 전체를 다시 비교하세요.

## 지원 범위와 안전 원칙

| 항목 | 현재 지원 |
| --- | --- |
| 운영체제 | Windows 10/11 64비트 우선 |
| 입력 파일 | PDF, HWP, HWPX, DOCX |
| 검색 데이터 | 사람이 승인한 최신 유효 규정 |
| 로컬 연결 | ChatGPT/Codex Desktop·CLI·IDE, Claude Desktop, Claude Code |
| 원격 연결 | Vercel에 배포한 HTTPS `/mcp` |

- 전처리 결과는 검토용 초안이며 자동 승인이 아닙니다.
- 원문, API 키, 토큰, 기관 내부 식별자와 사용자 로컬 경로를 공개 저장소에 올리지 마세요.
- 원격 MCP의 응답은 외부 AI 서비스로 전송될 수 있습니다. 공개 자료나 반출 승인을 받은 자료에만 사용하세요.
- 공개 또는 기관 운영 전에는 [SECURITY.md](SECURITY.md)를 확인하세요.

## 더 자세한 안내

- [MCP 빠른 연결 안내](docs/mcp_quickconnect_ko.md)
- [Vercel HTTPS MCP 배포 안내](docs/vercel_https_mcp_ko.md)
- [MCP 로컬 서버 공식 문서](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
- [OpenAI MCP 공식 문서](https://learn.chatgpt.com/docs/extend/mcp)
- [Claude Code MCP 공식 문서](https://code.claude.com/docs/en/mcp)

## 개발자용 실행과 검증

Python 3.11 이상에서 프로젝트 루트 기준으로 실행합니다.

```powershell
python -m streamlit run frontend\streamlit_app.py --server.address 127.0.0.1
python -m unittest discover -s tests -v
python -m build --sdist --wheel
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

기여 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md), 공개 저장소 이력 원칙은 [docs/public_repository_history_policy_ko.md](docs/public_repository_history_policy_ko.md)를 확인하세요.

## 업데이트 내역

README에는 현재 사용법만 유지합니다. 버전별 변경 내용과 다운로드 파일은 [GitHub Releases](https://github.com/koul777/Public-Regulation-MCP-Builder/releases)에서 확인할 수 있습니다.

## Kordoc 사용 고지

HWP/HWPX 문서 구조와 표 추출 교차 검증에는 [Kordoc](https://github.com/chrisryugj/kordoc)을 사용했습니다. 배포 번들에는 Kordoc 소스나 실행 파일이 포함되지 않음에 유의하세요. 라이선스는 [Kordoc LICENSE](https://github.com/chrisryugj/kordoc/blob/main/LICENSE)와 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에서 확인할 수 있습니다.
