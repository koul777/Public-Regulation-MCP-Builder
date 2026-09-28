# 초보자 첫 성공 검증

이 명령은 실제 기관 원문이나 기존 운영 데이터를 건드리지 않고, 초보자가
`샘플 규정 → MCP STDIO → list_regulations/search/fetch` 경로를 완주할 수 있는지
확인한다.

## 실행

저장소 루트에서 실행한다.

처음 소스에서 실행하거나 `.venv` 폴더가 없다면 먼저 아래 준비를 한 번 실행한다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

```powershell
.\.venv\Scripts\python.exe -m scripts.run_beginner_first_success
```

설치된 Ollama와 Qwen3 8B까지 확인하려면 다음을 사용한다.

```powershell
.\.venv\Scripts\python.exe -m scripts.run_beginner_first_success --verify-local-llm
```

보고서를 저장하려면:

```powershell
.\.venv\Scripts\python.exe -m scripts.run_beginner_first_success --out-json .tmp\beginner-first-success.json
```

## 판정 범위

자동 검증은 다음 세 가지를 필수로 확인한다.

1. Python 3.11 이상과 `mcp`, `python-docx` 설치
2. 공개 배포 가능한 합성 DOCX 샘플 생성
3. 실제 MCP STDIO 서버의 초기화와 `list_regulations`, `search`, `fetch` 호출

`--verify-local-llm`을 지정하면 Ollama의 `qwen3:8b` 연결도 필수 조건이 된다.
Ollama 점검을 생략한 기본 실행은 MCP 연결 계약만 검증하며, 로컬 LLM이 준비됐다고
주장하지 않는다.

## 자동 검증 후 사람이 할 일

이 명령의 통과는 Claude Desktop이 실제로 재시작됐다는 뜻이 아니다. 다음을 직접 수행한다.

1. 승인·색인된 기관 규정으로 MCP 번들을 생성한다.
2. 생성된 `claude_desktop_config.json`의 `mcpServers` 항목을 Claude Desktop 설정에 병합한다.
3. Claude Desktop을 트레이에서 완전히 종료한 뒤 다시 실행한다.
4. Claude 대화에서 `list_regulations`, `search`, `fetch`를 차례로 호출한다.

합성 smoke 데이터의 통과 결과는 기관 규정의 정확성이나 공식 승인을 의미하지 않는다.
기관 문서는 반드시 사람 검토와 승인·색인 절차를 거쳐야 한다.
