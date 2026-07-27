# PR MCP Builder

[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows11&logoColor=white)](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-STDIO%20%7C%20HTTPS-0F766E)](docs/mcp_quickconnect_ko.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![규정 문서를 사람이 검토·승인한 뒤 로컬 AI와 HTTPS MCP로 연결하는 PR MCP Builder](docs/assets/pr-mcp-builder-hero.png)

공공기관 규정 파일을 정리하고, **사람이 확인해 승인한 내용만** ChatGPT·Codex·Claude에서
검색하게 만드는 Windows용 프로그램입니다.

PDF·HWP·HWPX·DOCX 파일을 올리면 규정명, 개정판, 목차와 조문을 정리합니다. 처리 결과를
사람이 원문과 비교해 승인하면 AI 프로그램에서 사용할 MCP 검색 도구를 만듭니다.

현재 개발 중인 공개 소스 프로젝트이며 Windows 10/11 64비트 우선 지원입니다.
Streamlit 화면은 로컬 운영자용이며 완성형 공개 SaaS 화면이 아닙니다.

> [!IMPORTANT]
> 문서를 올렸다고 바로 AI 검색에 공개되지 않습니다. 승인하지 않은 내용은 MCP의
> `search`와 `fetch` 결과에 포함하지 않습니다.
> 사람 검수와 승인을 거쳐 승인된 규정만 MCP 데이터로 생성합니다.
> 사람에게 승인되지 않은 청크는 검색 색인과 MCP 번들에 포함하지 않습니다.

이 안내서는 MCP를 처음 들어 본 사람도 위에서부터 그대로 따라 할 수 있도록 작성했습니다.
화면 예시는 이해를 돕기 위한 샘플이며 앱 버전에 따라 버튼 위치나 이름이 조금 달라질 수
있습니다. **경로, 서버 이름, ID는 예시를 타이핑하지 말고 내 PC에서 생성된 값을
복사하세요.**

## 이 문서에서 할 일

1. 프로그램을 내려받고 규정 파일을 처리합니다.
2. 원문과 처리 결과를 비교한 뒤 사용할 조문을 승인합니다.
3. 아래 두 연결 중 하나를 선택합니다.
   - 같은 PC에서 쓰는 **로컬 STDIO**
   - 웹이나 다른 기기에서도 쓰는 **Vercel HTTPS**
4. AI 프로그램에 MCP를 등록합니다.
5. `search`와 `fetch`를 실제로 호출해 연결을 검증합니다.

바로 이동:

- [처음 설치하고 승인 데이터 만들기](#1-처음-설치하고-승인-데이터-만들기)
- [로컬 STDIO와 Vercel HTTPS 중 선택하기](#2-연결-방식-선택하기)
- [강의 A: 같은 PC의 Claude Desktop에 STDIO 연결하기](#강의-a-claude-desktop-로컬-stdio-연결)
- [ChatGPT/Codex Desktop과 CLI에 로컬 연결하기](#chatgptcodex-desktop과-cli에-로컬-연결하기)
- [강의 B: Vercel에 배포하고 HTTPS로 연결하기](#강의-b-vercel-https-배포와-연결)
- [search와 fetch로 최종 확인하기](#3-search와-fetch로-최종-확인하기)
- [문제 해결표](#4-문제-해결표)

## 먼저 알아둘 네 단어

| 단어 | 쉬운 뜻 |
| --- | --- |
| MCP | AI 프로그램이 이 규정 검색기에 질문할 수 있게 해 주는 연결 규칙 |
| 번들 | 설정 파일, 실행 파일, 승인 검색 데이터를 한 폴더에 모은 것 |
| STDIO | 같은 PC의 AI 프로그램이 Python 서버를 직접 켜고 대화하는 로컬 연결 |
| HTTPS | Vercel에 서버를 배포하고 `https://.../mcp` 주소로 접속하는 원격 연결 |

`search`는 승인된 규정을 찾아 결과 목록과 `id`를 돌려줍니다. `fetch`는 그 `id`를 받아
원문 내용과 출처를 돌려줍니다. 따라서 **서버 이름이 보이는 것만으로는 성공이 아니며,
두 도구가 모두 동작해야 연결 완료**입니다.

## 1. 처음 설치하고 승인 데이터 만들기

### 1-1. 프로그램 내려받기

1. [최신 Windows 실행판](https://github.com/koul777/Public-Regulation-MCP-Builder/releases/latest)을
   엽니다.
2. Windows용 ZIP 파일을 내려받습니다.
3. ZIP 파일에서 바로 실행하지 말고, 새 폴더에 **압축을 모두 풉니다**.
4. 압축을 푼 폴더에서 `PR MCP Builder.exe`를 실행합니다.
5. Windows가 실행 여부를 물으면 게시자와 내려받은 주소가 이 저장소의 Release인지 먼저
   확인합니다.

개발자가 소스에서 실행하는 방법은 [개발자용 실행과 검증](#개발자용-실행과-검증)에
있습니다.

### 1-2. 기관 선택

기관을 만들거나 기존 기관을 선택합니다. 문서와 승인 데이터는 선택한 기관 범위로
분리됩니다.

![기관을 등록하거나 선택하는 시작 화면](docs/assets/readme-guide-01-start.png)

선택 후 대시보드에서 현재 작업 상태와 다음 단계를 확인합니다.

![기관 선택 뒤 나타나는 작업 대시보드](docs/assets/readme-guide-01-dashboard.png)

### 1-3. 규정 파일 올리기

1. `① 파일 추가·전처리`로 이동합니다.
2. PDF·HWP·HWPX·DOCX 규정 파일을 선택합니다.
3. 여러 규정은 한 번에 올릴 수 있지만 처음이라면 한 파일로 연습하는 것이 쉽습니다.
4. 자동 인식된 규정명, 버전과 개정일을 원문과 비교합니다.
5. 값이 맞으면 전처리를 시작합니다.

![규정 파일을 올리는 화면](docs/assets/readme-guide-02-upload.png)

![자동 인식된 규정 정보를 확인하는 화면](docs/assets/readme-guide-02-progress.png)

처리가 끝나면 완료 표시가 나타납니다.

![규정 전처리 완료 화면](docs/assets/readme-guide-02-preprocess-complete.png)

### 1-4. 처리 결과 확인

`② 결과 확인`에서 처리할 규정을 불러옵니다. 품질 결과가 표시되어도 자동 승인된 것은
아닙니다.

![전처리 결과를 불러오는 화면](docs/assets/readme-guide-03-load.png)

여러 규정을 올렸다면 각 규정의 품질과 상태를 확인합니다.

![여러 규정의 처리 결과와 품질을 확인하는 화면](docs/assets/readme-guide-03-multi-regulation.png)

원문, 전처리 결과와 앞뒤 문맥을 비교합니다. 조문 번호, 제목, 본문, 별표와 표 내용이
원문과 다르면 승인하기 전에 수정하거나 다시 처리합니다.

![원문과 전처리 결과 및 앞뒤 문맥을 비교하는 화면](docs/assets/readme-guide-03-chunk-context.png)

### 1-5. 사람이 검수하고 승인

AI 제안은 참고용입니다. 사람이 원문을 확인하고 승인한 내용만 검색 색인과 MCP에
들어갑니다.

![AI 제안을 검토하고 사람이 승인하는 화면](docs/assets/readme-guide-04-human-review.png)

승인 화면에서 사용할 조문을 선택하고 승인 동작을 실행합니다.

![검토한 조문을 승인하는 동작 화면](docs/assets/readme-guide-04-approval-actions.png)

색인 완료 상태가 표시돼야 MCP 생성 단계로 갈 수 있습니다.

![승인 데이터의 검색 색인이 완료된 화면](docs/assets/readme-guide-04-indexed.png)

> [!WARNING]
> 원문 업로드, 미승인 데이터, API 키, 비밀번호와 기관 내부 비밀 자료를 공개 저장소나
> 공개 Vercel 배포에 넣지 마세요.

## 2. 연결 방식 선택하기

`④ MCP 생성·AI 연결`에서 사용할 앱과 연결 방식을 선택합니다.

아래 한 질문으로 선택하면 됩니다.

**AI 프로그램과 PR MCP Builder가 같은 Windows PC에 있나요?**

- 예 → **로컬 STDIO**를 선택합니다. URL은 필요 없습니다.
- 아니요, 웹이나 다른 PC에서도 써야 합니다 → **Vercel HTTPS**를 선택합니다.

| 사용하려는 곳 | 선택할 방식 | 최종 입력값 |
| --- | --- | --- |
| 같은 PC의 Claude Desktop | 로컬 STDIO | `claude_desktop_config.json`의 `command`, `args`, `env` |
| 같은 PC의 ChatGPT/Codex Desktop | 로컬 STDIO | `chatgpt_desktop_local_mcp.json`의 `ui_fields` |
| 같은 PC의 Codex CLI·IDE | 로컬 STDIO | `codex_config_snippet.toml` |
| 같은 PC의 Claude Code | 로컬 STDIO | `claude_code_add_stdio.ps1` |
| 웹이나 여러 기기의 ChatGPT·Codex·Claude | Vercel HTTPS | 고정 Production 주소 `https://<host>/mcp` |

> [!CAUTION]
> 두 화면을 섞지 마세요.
>
> - 로컬 Claude Desktop: **설정 > 개발자 > 구성 편집**
> - Vercel HTTPS: **설정 또는 Customize > Connectors**
>
> 로컬 설정에는 `command/args/env`가 필요하고, Vercel 연결에는 HTTPS URL만 필요합니다.

### 생성 완료 화면 읽는 법

`MCP 파일 묶음 생성 완료`가 나오면 아래의 **직접 MCP 연결 및 최종 확인**까지 내려갑니다.
이 영역은 두 방식을 항상 비교해 보여 주고, 이번에 선택한 방식에는 실제 다음 명령과 등록
위치를 표시합니다.

- **로컬 STDIO를 선택한 경우**: 생성된 실제 `command/args/env`, 앱별 설정 파일,
  `doctor_mcp_connection.ps1`, `validate_mcp_smoke.ps1`, 완전 재시작 순서가 보입니다.
- **Vercel HTTPS를 선택한 경우**: 입력한 Production `/mcp` URL, staging 명령,
  `vercel --prod`, Connectors 등록, 원격 smoke 순서가 보입니다.
- 번들 폴더에는 양쪽 연결 파일이 들어갈 수 있지만, **현재 선택한 방식의 절차부터**
  완료합니다.
- `MCP 파일 묶음 생성 완료`는 Vercel 배포 완료가 아닙니다. Vercel은 `Ready`와
  `Aliased` URL을 확인하고 원격 smoke까지 통과해야 합니다.

![MCP 생성 완료 화면에서 로컬 STDIO와 Vercel HTTPS의 다음 단계를 구분하는 설명용 화면](docs/assets/readme-course-00-completion-guide.png)

## 강의 A: Claude Desktop 로컬 STDIO 연결

STDIO 연결에서는 Claude Desktop이 내 PC의 Python 서버를 자식 프로세스로 실행합니다.
그래서 웹 주소가 없어도 되지만, 생성된 Python 경로·인자·환경변수가 정확해야 합니다.
Claude Desktop이 어느 폴더에서 시작되더라도 동작하도록 생성 설정은 절대경로와
`PYTHONPATH`를 사용합니다.

### A-1. 로컬 STDIO 번들 만들기

1. `④ MCP 생성·AI 연결`에서 앱으로 **Claude Desktop**을 선택합니다.
2. 연결 방식으로 **로컬 STDIO**를 선택합니다.
3. 알아보기 쉬운 MCP 이름을 입력합니다.
4. 저장 폴더를 선택합니다. 처음에는 짧고 이동하지 않을 폴더가 좋습니다.
5. `MCP로 쓸 파일 묶음 만들기`를 누릅니다.
6. 완료될 때까지 앱과 PowerShell 창을 닫지 않습니다.

![PR MCP Builder에서 승인과 색인을 마치고 Claude Desktop용 로컬 STDIO 번들을 생성하는 순서](docs/assets/readme-claude-mcp-01-bundle.svg)

생성 폴더에는 최소한 다음 항목이 있어야 합니다.

- `claude_desktop_config.json`: Claude Desktop에 합칠 설정
- `data\`: 사람이 승인한 검색 데이터
- `run_mcp_stdio_server.ps1`: 직접 Python 실행이 불가능할 때의 fallback 실행 파일
- `doctor_mcp_connection.ps1`: Python·프로젝트·import 상태 진단
- `validate_mcp_smoke.ps1`: 실제 MCP 통신 검사
- `mcp_config.bundle.json`: 번들 전체 연결 계약

![로컬 STDIO 번들 폴더에서 확인할 파일을 표시한 설명용 화면](docs/assets/readme-course-01-stdio-bundle.png)

`data`만 따로 옮기면 설정의 절대경로와 달라져 실행되지 않습니다. 폴더를 옮겨야 한다면
경로를 손으로 고치기보다 새 위치에서 번들을 다시 생성하세요.

### A-2. 생성 파일을 먼저 확인

생성 폴더의 `claude_desktop_config.json`을 메모장으로 엽니다. 정상적인 소스 프로젝트
직접 실행 설정은 다음 구조입니다.

```json
{
  "mcpServers": {
    "<생성된-서버-이름>": {
      "command": "C:\\project\\Public-Regulation-MCP-Builder\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "scripts.run_regulation_mcp",
        "--data-dir",
        "C:\\MCP-Bundles\\my-regulations\\data",
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

예시를 복사하지 말고 **내 생성 파일의 서버 이름, 전체 `command`, 모든 `args`, 전체
`env`를 그대로 사용**합니다.

- Windows 경로가 `C:\\폴더\\파일`처럼 역슬래시 두 개로 보이는 것은 정상입니다.
- `args`는 순서를 바꾸거나 일부를 지우면 안 됩니다.
- `env.PYTHONPATH`가 있어야 Claude Desktop의 시작 폴더와 관계없이 모듈을 찾습니다.
- `type: "stdio"`는 현재 Claude Desktop 로컬 설정에 필수가 아니므로 없을 수 있습니다.
- `command`가 `powershell.exe`여도 오류가 아닙니다. 소스 프로젝트나 검증된 Python을
  직접 사용할 수 없는 독립 ZIP/wheel 환경에서는 기존 래퍼가 fallback으로 생성됩니다.

### A-3. Claude Desktop 설정 파일 열기

1. Claude Desktop을 실행합니다.
2. 왼쪽 아래 프로필 또는 메뉴에서 **설정(Settings)**을 엽니다.
3. **개발자(Developer)**를 누릅니다.
4. **로컬 MCP 서버(Local MCP servers)** 영역에서 **구성 편집(Edit Config)**을 누릅니다.
5. Windows가 `%APPDATA%\Claude\claude_desktop_config.json`을 엽니다.

한글 경로는 **설정 > 개발자 > 로컬 MCP 서버 > 구성 편집**, 영문 경로는
**Settings > Developer > Edit Config**입니다.

![Claude Desktop에서 로컬 STDIO 구성 편집을 여는 설명용 화면](docs/assets/readme-course-02-claude-stdio-config.png)

이 단계에서 일반 **커넥터(Connectors)** 메뉴를 열면 안 됩니다. 그 메뉴는 강의 B의
Vercel 같은 원격 HTTPS 서버용입니다.

### A-4. 기존 설정을 지우지 않고 새 서버만 합치기

Claude 설정 파일이 완전히 비어 있으면 생성된 `claude_desktop_config.json` 전체를
복사해도 됩니다.

이미 다른 MCP 서버나 `preferences`가 있다면 파일 전체를 덮어쓰지 않습니다. 생성 파일의
`mcpServers` 안에 있는 **새 서버 한 항목만** 기존 `mcpServers` 중괄호 안에 추가합니다.

예를 들어 기존 설정이 다음과 같다고 가정합니다.

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

새 서버를 합친 뒤에는 다음처럼 기존 내용과 새 내용이 모두 남아 있어야 합니다.

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
      "command": "<생성 파일의 command>",
      "args": [
        "<생성 파일의 args 전체를 같은 순서로>"
      ],
      "env": {
        "PYTHONPATH": "<생성 파일의 프로젝트 절대경로>",
        "PYTHONSAFEPATH": "1"
      }
    }
  }
}
```

앞 항목 뒤의 쉼표 하나를 빠뜨리거나 마지막 항목 뒤에 불필요한 쉼표를 넣으면 JSON이
열리지 않습니다.

![기존 preferences와 MCP 서버를 보존하고 새 서버 항목만 병합하는 방법](docs/assets/readme-claude-mcp-02-config.svg)

### A-5. 저장하고 Claude Desktop을 완전히 다시 시작

1. 메모장에서 `Ctrl+S`를 눌러 JSON을 저장합니다.
2. Claude 창 오른쪽 위 `X`만 누르지 말고 작업 표시줄 알림 영역의 Claude 아이콘을
   우클릭해 **종료(Quit)**합니다.
3. 작업 관리자에 Claude가 남아 있지 않은지 확인합니다.
4. Claude Desktop을 다시 실행합니다.
5. 새 대화를 엽니다.
6. **파일·커넥터 추가 > Connectors**에서 방금 만든 서버 이름을 찾습니다.
7. 상태가 `running`인지 확인합니다.

`disconnected`라면 다음 단계로 넘어가지 말고 [문제 해결표](#4-문제-해결표)를
확인합니다.

### A-6. 번들 자체를 먼저 진단하는 방법

Claude 화면에서 이유를 찾기 어렵다면 생성 번들 폴더를 파일 탐색기로 열고, 주소 표시줄에
`powershell`을 입력해 Enter를 누릅니다. 열린 PowerShell에서 실행합니다.

```powershell
.\doctor_mcp_connection.ps1
.\validate_mcp_smoke.ps1
```

첫 명령은 Python 파일 존재 여부만 보지 않고 다음 원인을 구분합니다.

- Python 실행 파일이 없음
- Python 3.11 미만
- 프로젝트 루트를 찾지 못함
- `scripts.run_regulation_mcp` import 실패
- 필수 의존성 import 실패
- runtime marker 검증 실패
- 기록된 runtime Python 경로가 유효하지 않음

진단은 MCP 프로토콜 stdout을 오염시키지 않도록 stderr에 표시됩니다. `validate`는
`initialize` → `tools/list` → `search` → `fetch` 순서의 STDIO 통신을 검사합니다.

## ChatGPT/Codex Desktop과 CLI에 로컬 연결하기

### ChatGPT/Codex Desktop

생성 완료 화면의 **ChatGPT/Codex Desktop에 등록하는 방법** 또는 번들의
`chatgpt_desktop_local_mcp.json`을 엽니다. `Settings > MCP servers > Add server`에서
다음처럼 입력합니다.

| 설정 칸 | 넣을 값 |
| --- | --- |
| Name | 생성된 MCP 서버 이름 |
| Transport | `STDIO` |
| Command | `ui_fields.command` |
| Working directory | `ui_fields.cwd` |
| Arguments | `ui_fields.args`를 표시된 순서대로 한 항목씩 |
| Environment | `ui_fields.env`가 비어 있으면 비워 둠 |
| Environment passthrough | 생성 안내가 비워 두라고 하면 비워 둠 |

서버 이름은 **Name에만** 넣습니다. Command에는 서버 이름이나 폴더 이름을 넣지 않습니다.
저장 후 앱을 완전히 종료했다가 다시 실행하고 `/mcp` 또는 MCP 서버 목록을 확인합니다.

### Codex CLI·IDE

`codex_config_snippet.toml`의 `[mcp_servers.<이름>]` 블록을 사용자
`~/.codex/config.toml`에 반영합니다. 이미 같은 이름의 블록이 있다면 중복 추가하지 말고
생성된 새 블록으로 갱신합니다. CLI와 IDE 확장을 완전히 재시작한 뒤 확인합니다.

### Claude Code

번들 폴더의 PowerShell에서 실행합니다.

```powershell
.\claude_code_add_stdio.ps1
```

등록 확인:

```powershell
claude mcp list
claude mcp get <생성된-서버-이름>
```

이 스크립트는 공식 `claude mcp add --transport stdio --scope user` 형식으로 등록합니다.

## 강의 B: Vercel HTTPS 배포와 연결

Vercel HTTPS는 승인된 MCP runtime을 인터넷에서 접속 가능한 서버로 배포하는 방법입니다.
Vercel 홈페이지는 계정·환경변수·로그를 관리하고, 처음 배포할 파일 준비와 업로드는 내
PC의 PowerShell에서 진행합니다.

> [!WARNING]
> Vercel로 전송한 MCP 응답은 외부 AI 서비스로 전달될 수 있습니다. 공개 자료 또는
> 반출 승인을 받은 자료에만 사용하세요. 기관 내부 자료에는 공개 무인증 모드를 사용하지
> 말고 bearer 인증이나 OAuth를 먼저 설계하세요.

### B-1. 준비물 확인

- Vercel 계정: <https://vercel.com>
- Node.js와 npm: <https://nodejs.org>
- Python 3.11 이상
- 사람 승인과 검색 색인이 끝난 MCP 번들의 `data` 폴더
- 프로젝트 소스가 있는 이 저장소

`④ MCP 생성·AI 연결`에서 HTTPS 연결을 선택하면 앱별 안내를 볼 수 있습니다. 앱 안에
표시되는 공개 URL은 실제 배포가 끝난 Production 주소로 입력해야 합니다.

### B-2. 배포 전용 폴더 만들기

PowerShell을 프로젝트 루트에서 열고 다음 명령을 실행합니다. 두 경로는 내 실제 경로로
바꿉니다.

```powershell
python scripts\prepare_vercel_mcp_deployment.py `
  --runtime-data-dir "C:\MCP-Bundles\my-regulations\data" `
  --out-dir "C:\MCP-Deploy\vercel-mcp-stage"
```

패키지를 설치해 CLI 명령을 사용할 수 있다면 같은 작업을 다음처럼 실행할 수 있습니다.

```powershell
reg-rag-mcp-vercel-stage `
  --runtime-data-dir "C:\MCP-Bundles\my-regulations\data" `
  --out-dir "C:\MCP-Deploy\vercel-mcp-stage"
```

이 명령은 전체 프로젝트나 원본 업로드 폴더를 배포하지 않고 다음 항목만 포함한
`vercel-mcp-stage`를 만듭니다.

- MCP 실행에 필요한 공개 소스
- `api/index.py` Vercel 진입점
- `vercel.json`
- 승인된 MCP runtime

기존 출력 폴더는 자동 덮어쓰지 않습니다. 기존 폴더를 재사용하려면 내용과 비밀값을
확인하고 새 빈 폴더를 선택하는 것이 안전합니다.

> [!CAUTION]
> `.env.local`, `.vercel`, 토큰, 원본 업로드와 보고서 전체를 ZIP이나 Git으로 함께
> 올리지 마세요. `.gitignore`만 믿고 폴더 전체를 수동 업로드하지 말고, 위 staging
> 명령으로 만든 범위를 배포 입력으로 사용하세요.

### B-3. Vercel CLI 설치하고 로그인

처음 한 번만 설치합니다.

```powershell
npm install -g vercel
vercel login
```

`vercel login`이 브라우저를 열면 로그인과 승인 절차를 마치고 PowerShell로 돌아옵니다.
토큰이나 로그인 링크를 다른 사람에게 보내지 마세요.

### B-4. Vercel 프로젝트 만들고 staging 폴더 연결

프로젝트 이름은 영문 소문자, 숫자, 하이픈으로 정합니다.

```powershell
vercel project add <프로젝트-이름>
vercel link --yes `
  --project <프로젝트-이름> `
  --cwd "C:\MCP-Deploy\vercel-mcp-stage"
```

완료 후 Vercel 홈페이지 Dashboard에 해당 프로젝트가 보입니다.

### B-5. 공개 또는 비공개 방식 선택

#### 공개해도 되는 승인 규정의 read-only endpoint

공개가 허용된 규정만 포함했고 누구나 `search`·`fetch`를 호출해도 되는 경우에만 다음
값을 Production 환경에 넣습니다.

```powershell
vercel env add MCP_ALLOW_UNAUTHENTICATED_HTTP production `
  --value "true" --yes `
  --cwd "C:\MCP-Deploy\vercel-mcp-stage"

vercel env add MCP_TOOL_PROFILE production `
  --value "chatgpt-data" --yes `
  --cwd "C:\MCP-Deploy\vercel-mcp-stage"
```

이 모드는 쓰기 도구 없이 원격 공개 범위를 `search`, `fetch`로 제한하는 용도입니다.

#### 기관 내부 자료나 비공개 endpoint

`MCP_ALLOW_UNAUTHENTICATED_HTTP=true`를 사용하지 않습니다. `MCP_AUTH_TOKEN`을 Vercel
Secret으로 관리하고 bearer 인증을 지원하는 클라이언트에 환경변수 이름만 연결하거나
OAuth를 구성합니다. 토큰 값을 README, JSON, TOML, Git 커밋에 기록하지 마세요.

ChatGPT 웹 hosted connector와 Claude remote connector의 인증 지원 범위가 다를 수
있으므로 기관 운영 배포는 [Vercel HTTPS MCP 배포 안내](docs/vercel_https_mcp_ko.md)의
인증 조건을 먼저 확인합니다.

### B-6. Production 배포

미리보기 배포로 오류를 먼저 확인한 뒤 Production으로 배포합니다.

```powershell
vercel --cwd "C:\MCP-Deploy\vercel-mcp-stage"
vercel --prod --cwd "C:\MCP-Deploy\vercel-mcp-stage"
```

마지막 출력에서 다음 두 줄을 찾습니다.

```text
Ready
Aliased: https://<프로젝트-이름>.vercel.app
```

`Aliased` 주소가 고정 Production 주소입니다. 여기에 `/mcp`를 붙인 주소 하나를
클라이언트에 등록합니다.

```text
https://<프로젝트-이름>.vercel.app/mcp
```

문서에서 일반적으로 쓰는 표기는 `https://<deployment>/mcp`입니다.

![Vercel Production 배포 완료와 고정 Aliased URL을 찾는 설명용 화면](docs/assets/readme-course-03-vercel-production.png)

배포마다 생기는 긴 Preview URL 대신 고정 `Aliased` 주소를 사용하세요. 같은 Vercel
배포와 `/mcp` endpoint를 ChatGPT·Codex·Claude가 함께 사용하므로 앱마다 새 서버를
배포할 필요가 없습니다.

### B-7. 주소를 등록하기 전에 프로토콜 검증

프로젝트 루트에서 실제 Production URL로 실행합니다.

```powershell
python scripts\run_mcp_client_config_smoke.py `
  --remote-url "https://<프로젝트-이름>.vercel.app/mcp" `
  --allow-unauthenticated-remote `
  --timeout-seconds 120 `
  --fail-on-issue
```

공개 무인증 endpoint일 때만 `--allow-unauthenticated-remote`를 사용합니다. 비공개
endpoint에는 설정한 인증을 사용합니다.

결과에서 다음이 모두 확인돼야 합니다.

- `mcp_initialized`: `true`
- `tools_discovered`: `true`
- `end_to_end_verified`: `true`
- `tool_names`: `search`, `fetch` 포함

### B-8. Claude에 Vercel URL 등록

1. Claude 웹 또는 Desktop을 엽니다.
2. **설정 > 커넥터(Connectors)** 또는 **Customize > Connectors**를 엽니다.
3. **사용자 지정 커넥터 추가(Add custom connector)**를 누릅니다.
4. 이름에는 알아보기 쉬운 MCP 이름을 입력합니다.
5. URL에는 고정 Production 주소와 `/mcp`를 입력합니다.
6. 공개 read-only 배포는 별도 토큰을 입력하지 않습니다.
7. 저장한 뒤 새 대화에서 커넥터를 활성화합니다.

![Claude 사용자 지정 커넥터에 Vercel MCP URL을 등록하는 설명용 화면](docs/assets/readme-course-04-claude-remote-connector.png)

Vercel 연결 화면에는 로컬 `command`, `args`, `cwd`, `PYTHONPATH`를 입력하지 않습니다.
필요한 것은 최종 HTTPS `/mcp` URL과, 비공개 서버일 때 승인된 인증뿐입니다.

전체 흐름을 한 장으로 보면 다음과 같습니다.

![승인 번들을 Vercel에 배포하고 고정 Production URL을 Claude 커넥터에 등록한 뒤 search와 fetch로 확인하는 순서](docs/assets/readme-vercel-claude-connection.svg)

### B-9. ChatGPT·Codex·Claude Code에 같은 URL 등록

- ChatGPT/Codex Desktop: `Settings > MCP servers > Add server`에서 Streamable HTTP
  URL에 같은 `https://<host>/mcp`를 입력합니다.
- Codex CLI·IDE 공개 endpoint:

```toml
[mcp_servers.my_regulations]
url = "https://<프로젝트-이름>.vercel.app/mcp"
```

- 비공개 Codex endpoint는 토큰 값이 아니라 환경변수 이름을 설정합니다.

```toml
[mcp_servers.my_regulations]
url = "https://<프로젝트-이름>.vercel.app/mcp"
bearer_token_env_var = "MCP_AUTH_TOKEN"
```

- Claude Code: 번들에서 URL이 반영된 `claude_code_add_http.ps1`을 실행합니다.

```powershell
.\claude_code_add_http.ps1
claude mcp list
```

## 3. search와 fetch로 최종 확인하기

로컬 STDIO와 Vercel HTTPS 모두 같은 방식으로 최종 확인합니다.

1. AI 프로그램을 완전히 종료하고 다시 실행합니다.
2. 새 대화를 만듭니다.
3. 방금 등록한 MCP 서버 또는 커넥터를 활성화합니다.
4. 다음 요청을 그대로 보내되 검색어는 내 규정에 있는 말로 바꿉니다.

```text
연결한 규정 MCP의 search 도구로 인사규정을 찾아줘.
첫 번째 검색 결과의 id를 fetch 도구에 넣어 원문과 출처를 보여줘.
```

정상이라면 다음 순서가 보입니다.

1. MCP `search` 도구가 실행됩니다.
2. 한 개 이상의 검색 결과와 각 결과의 `id`가 나옵니다.
3. 그중 하나의 `id`로 MCP `fetch` 도구가 실행됩니다.
4. 승인된 규정 본문과 출처가 표시됩니다.

![MCP initialize와 search 및 fetch가 모두 성공한 설명용 검증 화면](docs/assets/readme-course-05-mcp-verification.png)

![Claude에서 running 상태와 search 및 fetch 원문 반환을 확인하는 순서](docs/assets/readme-claude-mcp-03-verify.svg)

다섯 항목을 모두 체크하면 연결 완료입니다.

- [ ] 서버 또는 커넥터가 목록에 보인다.
- [ ] 로컬 STDIO라면 상태가 `running`이다.
- [ ] 도구 목록에 `search`와 `fetch`가 보인다.
- [ ] `search`가 한 개 이상의 결과를 반환한다.
- [ ] 검색 결과의 `id`로 `fetch`가 본문과 출처를 반환한다.

## 4. 문제 해결표

| 보이는 현상 | 주된 원인 | 해결 |
| --- | --- | --- |
| 서버가 목록에 없음 | 잘못된 설정 화면, JSON 문법 오류, 재시작 안 함 | 로컬은 Developer > Edit Config, 원격은 Connectors인지 확인하고 완전히 재시작 |
| Claude Desktop가 `disconnected` | `command`, `args`, `env` 일부 누락 | 생성 JSON의 한 서버 항목을 수정 없이 다시 병합 |
| `Python was not found` | 파일 없음 또는 wrapper probe 실패 | `doctor_mcp_connection.ps1`을 실행해 버전·marker·project root·import 진단 확인 |
| Python은 있는데 import 실패 | Python 3.11 미만, 잘못된 프로젝트 Python, 의존성 누락 | 생성기가 검증한 프로젝트 Python을 사용하고 진단 stderr 확인 |
| 도구가 0개 | 서버 미활성화 또는 시작 실패 | 새 대화에서 서버를 활성화하고 `validate_mcp_smoke.ps1` 실행 |
| `search` 결과가 0개 | 검색어 불일치 또는 승인·색인된 데이터 없음 | 승인 및 색인 상태를 다시 확인하고 실제 규정 용어로 검색 |
| `fetch` 실패 | 검색 결과의 `id`가 아닌 제목을 전달 | `search` 응답의 정확한 `id` 값을 사용 |
| 폴더를 옮긴 뒤 실패 | 설정의 절대경로가 이전 위치를 가리킴 | 새 위치에서 MCP 번들을 다시 생성 |
| Vercel `404` | `/mcp`를 빼먹음 또는 잘못된 Preview URL | `https://<고정-host>/mcp` 사용 |
| Vercel `401/403` | 공개/비공개 인증 방식 불일치 | 공개 승인 데이터 여부와 환경변수·토큰/OAuth 설정을 다시 확인 |
| Vercel은 Ready지만 도구 호출 실패 | 환경변수, runtime, host 설정 오류 | Vercel Function Logs와 원격 smoke 명령 결과 확인 |
| 다른 MCP 서버가 사라짐 | Claude 설정 전체를 덮어씀 | 백업에서 복구하고 새 `mcpServers` 항목만 병합 |

## 지원 범위와 안전 원칙

| 항목 | 현재 지원 |
| --- | --- |
| 운영체제 | Windows 10/11 64비트 우선 |
| 입력 파일 | PDF, HWP, HWPX, DOCX |
| 검색 데이터 | 사람이 승인한 최신 유효 규정 |
| 로컬 연결 | ChatGPT Desktop / Codex CLI / Codex IDE, Claude Desktop, Claude Code |
| 원격 연결 | Vercel에 배포한 HTTPS `/mcp` |

- 전처리 결과는 검토용 초안이며 자동 승인이 아닙니다.
- 원문, API 키, 토큰, 기관 내부 식별자와 사용자 로컬 경로를 공개 저장소에 올리지 마세요.
- 원격 MCP의 응답은 외부 AI 서비스로 전송될 수 있습니다.
- 공개 Vercel 배포에는 공개가 허용된 승인 데이터만 포함하세요.
- Vercel Function 로그는 기관용 영속 감사 저널을 대신하지 않습니다.
- 공개 또는 기관 운영 전에는 [SECURITY.md](SECURITY.md)를 확인하세요.

## 더 자세한 안내

- [MCP 빠른 연결 안내](docs/mcp_quickconnect_ko.md)
- [MCP 클라이언트 설정 예시](docs/mcp_client_config_examples_ko.md)
- [Vercel HTTPS MCP 배포 안내](docs/vercel_https_mcp_ko.md)
- [MCP 로컬 서버 공식 문서](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
- [OpenAI MCP 공식 문서](https://learn.chatgpt.com/docs/extend/mcp)
- [Claude Code MCP 공식 문서](https://code.claude.com/docs/en/mcp)
- [Claude 원격 custom connector 공식 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Vercel CLI 배포 공식 안내](https://vercel.com/docs/projects/deploy-from-cli)

## 개발자용 실행과 검증

Python 3.11 이상에서 프로젝트 루트 기준으로 실행합니다.

Windows에서 처음 소스를 실행할 때는 `START_HERE.bat`를 사용할 수 있습니다. 수동으로
실행할 때는 가상환경을 만들고 같은 Python으로 Streamlit을 시작합니다. 로컬 runtime은
프로젝트 폴더의 `data\`에 저장되므로 공개 Git 커밋에 포함하지 않습니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m streamlit run frontend\streamlit_app.py --server.address 127.0.0.1
python -m unittest discover -s tests -v
python -m build --sdist --wheel
.\scripts\build_windows_portable.ps1
python scripts\audit_release_hygiene.py `
  --workflow-scope available `
  --include-untracked `
  --include-source-path-scan
```

기여 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md), 공개 저장소 이력 원칙은
[docs/public_repository_history_policy_ko.md](docs/public_repository_history_policy_ko.md)를
확인하세요.

## 업데이트 내역

README에는 현재 사용법만 유지합니다. 버전별 변경 내용과 다운로드 파일은
[GitHub Releases](https://github.com/koul777/Public-Regulation-MCP-Builder/releases)에서
확인할 수 있습니다.

## Kordoc 사용 고지

HWP/HWPX 문서 구조와 표 추출 교차 검증에는
[Kordoc](https://github.com/chrisryugj/kordoc)을 사용했습니다. 배포 번들에는
Kordoc 소스나 실행 파일이 포함되지 않음에 유의하세요. 라이선스는
[Kordoc LICENSE](https://github.com/chrisryugj/kordoc/blob/main/LICENSE)와
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에서 확인할 수 있습니다.
