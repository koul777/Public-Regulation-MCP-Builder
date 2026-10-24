<!-- slop-gate: off -->
<!-- 이 문서는 윤문 규칙의 금지 표현을 예시로 적기 때문에 자동 윤문 검사를 끔. -->

# MCP 최종 빌드 QA 게이트

MCP 묶음을 만들 때 도구 설계 규칙을 코드로 검사함. 문서에만 적은 규칙은 작업 중 건너뛰거나 바뀌는 일이 있어서 빌드 단계에서 강제함.

검사 대상은 네 가지임.

- 도구 표면: 도구 수, 도구 설명 토큰, 입력 수, 입력 경계, 읽기 전용 표시
- 도구 체이닝: ID 입력의 출처 도구 안내, 실제 런타임의 목록·목차·조문·검색·원문 조회
- 공개 응답 안전성: 응답 속 로컬 경로와 비밀값 형태
- 윤문: 묶음 안내문과 도구 설명의 한국어 표현

e4b급 로컬 모델로 실제 도구 호출을 확인하는 검사는 선택 항목임.

## 실행 위치

| 위치 | 실행 시점 | 실패 시 처리 |
| --- | --- | --- |
| 빌더 ④ MCP 탭의 **MCP로 쓸 파일 묶음 만들기** | 연결 파일을 만든 뒤, ZIP 압축 전(88%) | 묶음을 완료로 표시하지 않고 차단 코드를 화면에 표시 |
| `python scripts\mcp_build_qa_gate.py --bundle-dir <묶음 폴더>` | 릴리스 전 확인, CI | 종료 코드 2 |
| `python scripts\mcp_build_qa_gate.py --source-only` | MCP 도구 코드를 바꾼 PR | 런타임 없이 도구 표면만 검사 |

빌더 화면은 통과 시 `도구 N개 · 도구 설명 약 N토큰 · 설계 목표 미달 N건`을 표시함.

결과 등급은 두 가지임.

- `blocker`: 빌드 실패. 묶음 완료 기록을 만들지 않음.
- `target`: 설계 목표 미달. 보고서에만 기록. `--require-targets`를 주면 차단으로 바뀜.

보고서에는 도구 이름, 개수, 토큰 추정치, 결과 코드만 기록함. 데이터 경로, tenant·profile ID, 질문, 규정 본문은 넣지 않음.

## 1. 도구 표면

full, chatgpt-data 두 프로필을 각각 검사함.

| 항목 | 차단 기준 | 설계 목표 |
| --- | --- | --- |
| 도구 수 | 래칫 초과(full 16개, chatgpt-data 7개) | 12개 이하 |
| 도구 설명 토큰(이름, 설명, 입력 스키마, 서버 안내문) | 래칫 초과(full 4,650, chatgpt-data 1,300) | 4,000 이하 |
| 도구 설명 길이 | 40자 미만 또는 1,200자 초과 | 400자 이하 |
| 필수 입력 수 | 3개 초과 | 없음 |
| 입력 수 | 없음 | 6개 이하 |
| 입력 경계 | 문자열 `maxLength`, 숫자 `maximum`, 배열 `maxItems` 누락 | 없음 |
| 입력 구조 | `$ref`, `$defs`, 중첩 객체 입력 | 없음 |
| 읽기 전용 | `readOnlyHint=true`, `destructiveHint=false`가 아님 | 없음 |
| 도구 이름 | 영문 소문자·숫자·밑줄 64자 형식이 아님 | 없음 |

- 토큰 추정: ASCII 4글자를 1토큰, 그 밖의 글자는 1글자를 1토큰으로 계산함. 클라이언트 토크나이저마다 실제 값은 다름. 실제 값은 소형 모델 검사에서 Ollama `prompt_eval_count`로 기록함.
- 설명이 너무 짧으면 모델이 다음 도구를 고르지 못하고, 너무 길면 토큰 소모가 늘어남. 하한 40자와 목표 400자는 이 균형을 확인하는 출발값이며 소형 모델 검사 결과로 조정함.
- 래칫: 현재 출시 상태의 상한. 도구를 합쳐 줄이면 같은 변경에서 `scripts/mcp_build_qa_gate.py`의 `RATCHET`과 `tests/test_mcp_build_qa_gate.py`의 고정값을 함께 낮춤. 빌드를 통과시키려고 값을 올리지 않음.
- 토큰 래칫에는 기관 범위 안내 한 줄(바인딩된 profile_id 길이에 따라 달라짐)을 위한 약 70토큰 여유가 있음.

## 2. 도구 체이닝

### 정적 검사

- `id` 또는 `*_id` 필수 입력이 있는 도구는 설명에 그 값을 주는 다른 도구 이름을 적어야 함. 예: `get_regulation_toc`의 "returned by list_regulations".
- 모든 도구 이름이 서버 안내문(instructions)에 있어야 함.
- ID 없이 시작할 수 있는 도구(`search`, `list_regulations` 등)가 하나 이상 있어야 함.

### 런타임 검사

묶음의 `data` 폴더로 서버를 만들되 프로세스는 띄우지 않고, 등록된 도구를 FastMCP 입력 검증 그대로 호출함. 감사 로그와 RAG trace는 끄고 실행하므로 런타임 파일을 바꾸지 않음.

1. `list_regulations`로 규정을 받아 최대 3개를 고르게 고름.
2. 각 규정을 `get_regulation_toc`로 조회하고 첫 조문을 `get_regulation_article`로 조회함. 조문 번호는 네 형태로 모두 결과가 나와야 함: `제1조`, 목차 label `제1조 목적`, 인용형 `제1조(목적)`, 숫자 `1`.
3. 규정명으로 `search`를 호출하고 첫 결과 id로 `fetch`를 호출함. 본문이 비어 있으면 차단.
4. full 프로필에 기관이 바인딩되어 있으면 다른 `profile_id`로 한 검색이 거부되어야 함.
5. 도구별 최대 응답 토큰 추정치를 기록함. 3,000을 넘으면 설계 목표 미달.

목록이 비었거나, 목차에 조문이 없거나, 조문·검색·원문 조회가 0건이면 차단함. 근거가 있는데 0건이 나오는 경로는 모델이 "자료가 없다"고 답하는 원인이 됨.

## 3. 공개 응답 안전성

런타임 검사에서 받은 모든 응답의 문자열 값을 검사함.

| 패턴 | 대상 프로필 |
| --- | --- |
| Windows 드라이브 경로, UNC 경로, `/home`·`/Users`·`/root` 경로, `file://` | 전체 |
| Bearer 토큰, `sk-` 키, GitHub 토큰, AWS 접근 키, 개인 키 블록 | 전체 |
| 로컬 전용 `govreg://` 주소 | chatgpt-data |

보고서에는 패턴 이름과 건수만 기록하고 일치한 값은 기록하지 않음.

## 4. 윤문 게이트

| 대상 | 내용 |
| --- | --- |
| 묶음 안내문 | `README.md`, `README.ko.md`, `MCP 사용 시작하기.txt`, 묶음 루트의 다른 `.md`·`.txt` |
| MCP 문구 | 두 프로필의 서버 안내문과 도구 설명 |

| 규칙 코드 | 내용 |
| --- | --- |
| `dash` | 줄표(—) |
| `arrow` | 문장 속 화살표(→) |
| `ellipsis` | 말줄임표(…, 한글 뒤 `...`) |
| `double-passive` | 되어지다 |
| `chatbot-phrase` | 좋은 질문, 살펴보겠습니다, 다음과 같습니다 |
| `hype-word` | 획기적, 혁신적, 패러다임, 시너지, 인사이트, 시사하는 바, 주목할 만, 본질적으로 |
| `slogan-ending` | 결론적으로, 할 때입니다, 지금이야말로 |
| `metaphor-verb` | 꽂다, 박아·박힌, 녹여, 겨냥, 걷어 |

- 한글이 있는 줄만 검사함. 코드 블록과 인라인 코드는 제외함.
- 생성 안내문은 `slop-gate: off` 표시로 검사를 끌 수 없음.
- 안내문 파일이 없으면 차단함.
- 결과에는 파일 이름, 줄 번호, 규칙, 일치한 표현만 기록함.
- 다른 문서 단독 검사: `python scripts\korean_polish_gate.py <파일 또는 폴더>`. 이때는 `slop-gate: off` 표시를 인정함.

## 5. 소형 모델 체이닝 검사(선택)

e4b급 로컬 모델이 도구를 골라 이어서 호출하는지 확인함. 작은 모델을 기준으로 두는 이유는 도구 호출 능력이 약한 환경에서도 체이닝이 되는지 보기 위함임. MCP 서버를 띄우지 않고 등록된 도구를 직접 호출함.

| 모드 | 방식 | 대상 |
| --- | --- | --- |
| `native` | Ollama `/api/chat`의 `tools` | 도구 템플릿이 있는 모델 |
| `json` | 도구 목록을 시스템 프롬프트에 넣고 JSON 동작 한 개씩 응답 | 도구 호출 기능이 없거나 약한 모델·API |

`native`를 지원하지 않는 모델은 `tools-not-supported`로 표시됨. 이 경우 `--tool-call-mode json`으로 검사함.

### 질문

- 기본: 묶음 규정 목록에서 자동 생성. 절반은 조문 번호를 포함하고 절반은 조문 제목만 포함함. 근거 없는 질문 1개를 추가함.
- 비공개 질문: `--scenario-json`에 기존 검색 품질 게이트의 query spec 형식(`{"queries": [...]}`, `query` 또는 `question`, `expect_no_evidence`)을 그대로 사용함. 답이 나와야 하는 규정명은 `expect_regulation_title`로 지정함. holdout 표시가 있는 spec은 거부함.
- 실제 사용자 질문 파일과 보고서는 공개 저장소 밖에 둠.

### 지표와 기본 기준

| 지표 | 뜻 | 기본 기준 |
| --- | --- | --- |
| `chain_success_rate` | 답할 수 있는 질문에서 근거 도구(`fetch`, `get_regulation_article` 등)의 본문을 받고 기권하지 않은 비율 | 0.8 이상 |
| `false_abstention_rate` | 근거가 있는데 "없다"고 답한 비율 | 0.1 이하 |
| `invalid_call_rate` | 없는 도구, JSON이 아닌 입력, 입력 검증 실패의 비율 | 0.2 이하 |
| `no_evidence_abstention_rate` | 근거 없는 질문에 기권한 비율 | 1.0 |
| `max_prompt_tokens` | 요청 한 번의 최대 문맥 토큰 추정치 | `--max-prompt-tokens` 지정 시 |

보고서에는 질문, 답, 도구 응답을 복사하지 않음. 질문은 SHA-256 앞 16자리로만 구분함.

### 실행 예

    $BundleDir = $env:REG_RAG_MCP_BUNDLE_DIR
    $PrivateReport = Join-Path $env:REG_RAG_PRIVATE_EVIDENCE_ROOT "reports\mcp-build-qa-gate.json"

    python scripts\mcp_build_qa_gate.py `
        --bundle-dir $BundleDir `
        --model gemma3n:e4b `
        --tool-call-mode json `
        --out-json $PrivateReport

    python scripts\mcp_build_qa_gate.py `
        --bundle-dir $BundleDir `
        --model qwen3:4b `
        --no-think `
        --model-tool-profile chatgpt-data

- Ollama 주소는 loopback(`127.0.0.1`, `localhost`, `::1`)만 허용함. 프록시와 redirect를 따르지 않음.
- `--no-think`는 qwen3처럼 생각 단계가 있는 모델에서 토큰을 줄일 때 사용함. 생각 단계가 없는 모델에 보내면 오류가 날 수 있음.
- CPU만 있는 PC에서는 질문 하나에 몇 분이 걸릴 수 있음. 빌더 화면의 묶음 생성 단계에서는 실행하지 않음.

## 2026-10-24 첫 적용 결과

변경 전 소스에 같은 게이트를 실행하면 차단 25건이었음.

| 차단 코드 | 건수 | 조치 |
| --- | ---: | --- |
| `tool-input-unbounded`(`as_of_date` 길이 제한 없음) | 9 | `McpAsOfDate`(최대 32자) 적용 |
| `tool-chain-producer-missing` | 9 | 도구 설명에 ID를 주는 도구 이름 추가 |
| `tool-missing-from-instructions`(`list_documents`) | 1 | full 서버 안내문에 추가 |
| `chain-article-empty`(목차 label, 인용형, 숫자) | 6 | 정확 일치가 없을 때 `제N조`·`제N조의M`으로 바꿔 다시 조회 |

현재 결과는 차단 0건, 설계 목표 미달 5건임.

| 목표 미달 | 현재 값 | 목표 |
| --- | ---: | ---: |
| full 도구 수 | 16 | 12 |
| full 도구 설명 토큰 | 약 4,580 | 4,000 |
| `search` 설명 길이(full) | 566자 | 400자 |
| `search` 입력 수(full) | 7 | 6 |
| `lookup` 입력 수(full) | 8 | 6 |

full 도구 설명 토큰은 4,464에서 4,580으로 늘었음. ID 출처 도구 이름과 입력 길이 제한을 추가한 결과임. chatgpt-data는 1,211에서 1,218임. 도구 수와 토큰을 목표까지 줄이는 작업은 도구 통합 단계에서 진행하고, 그때 래칫 값을 낮춤.

## 관련 문서

- [MCP 도구 계약](mcp_tool_contract_ko.md)
- [MCP 검색 성능·품질 게이트](mcp_performance_quality_gate_ko.md)
- [전처리 변경 보호 절차](preprocessing_change_governance_ko.md)
