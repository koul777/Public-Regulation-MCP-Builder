# 초보자 첫 성공 검증

이 명령은 실제 기관 원문이나 기존 운영 데이터를 건드리지 않고, 합성 DOCX 샘플 생성과 별도의 합성 MCP STDIO 호출 계약을 확인한다. 두 단계의 통과는 DOCX 파싱부터 실제 AI 클라이언트까지 이어지는 전체 여정의 완료를 뜻하지 않는다.

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

이 smoke는 합성 DOCX 샘플 생성과 합성 MCP STDIO 호출 계약을 확인한다. 샘플 생성은 DOCX 파서 검증이 아니며, 이 결과는 DOCX 파싱부터 외부 AI 클라이언트 답변까지의 전체 여정이 아니다. 별도 브라우저 업로드·검수 여정도 최종 승인·색인까지 포함하지 않는다. 실제 외부 AI 답변이나 인용 확인, 정량 검색 정확도는 측정하지 않는다.

## 운영 완료 기준

운영 완료로 기록하려면 선택한 기관, 기관 프로필, 문서 범위가 맞고, 그 범위의 최신 승인본이 실제 색인되어 조회 가능해야 한다. 그 현재 개정본으로 MCP 번들을 만든 다음 운영자가 실제 AI 앱에서 등록, 재시작 또는 새 대화, 연결 진단, list_regulations, search, fetch를 확인해 연결 화면 체크리스트에 직접 기록한다. 확인 기록은 번들을 만든 개정본에 묶인다. 문서, 범위, 설정이 바뀌면 현재 승인본을 다시 색인하고 새 번들을 만들어 확인한다. 생성만 하고 확인하지 않은 번들은 완료가 아니라 연결 점검 대상이다.

연결 체크리스트는 초보자 안내를 사용하지 않는 경우에도 연결 화면에서 이용할 수 있다. 안내 상태와 관계없이 실제 승인·색인은 자동 실행되지 않는다.

AI 검수 결과는 실행 안 됨, 실패, 일부 완료, 완료 상태를 구분해 확인한다. 일부 성공이나 AI 검수 완료는 사람 승인이 아니다. 운영자는 조항을 원문과 대조하고 누락·실패 결과를 처리한 뒤 명시적으로 승인해야 한다. 승인 기록에 남은 조항만 색인 대상이다.

Qwen 앱 실행이나 상태 확인 응답은 앱이 동작한다는 뜻일 뿐 답변과 출처를 확인했다는 뜻이 아니다. 실제 Qwen 대화에서 기관·규정 선택, 질문, 답변 근거를 운영자가 확인한다.

## 자동 검증 후 사람이 할 일

이 명령의 통과는 Claude Desktop이 실제로 재시작됐다는 뜻이 아니다. 다음을 직접 수행한다.

1. 승인·색인된 기관 규정으로 MCP 번들을 생성한다.
2. 생성된 `claude_desktop_config.json`의 `mcpServers` 항목을 Claude Desktop 설정에 병합한다.
3. Claude Desktop을 트레이에서 완전히 종료한 뒤 다시 실행한다.
4. Claude 대화에서 `list_regulations`, `search`, `fetch`를 차례로 호출한다.

합성 smoke 데이터의 통과 결과는 기관 규정의 정확성이나 공식 승인을 의미하지 않는다.
기관 문서는 반드시 사람 검토와 승인·색인 절차를 거쳐야 한다. 번들 생성이나 앱 상태만으로 실제 답변·출처를 확인했다고 기록하지 않는다.
