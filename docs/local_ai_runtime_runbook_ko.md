# Qwen3 다중 모델 로컬 AI 설치·운영 런북

## 1. 운영 목표

이 런북은 공공기관 규정 파일을 외부 AI API로 보내지 않고 다음 두 파이프라인을 한 PC 또는 폐쇄망 서버에서 운용하는 절차다.

- 전처리 8단계: 업로드 검증 → 파싱/OCR → 정규화 → 구조 탐지 → 조문 Chunk → 품질 게이트 → 구조화 Export → 사람 승인 기반 Vector 색인
- 정밀 질의응답 7단계(API 기본 모드·전체 수용시험): 질의 분석 → 질의 보정 → Hybrid Search → 재랭킹/ACL 필터 → Context 구성 → Qwen3 8B 답변 → Qwen3 4B 주장 감사와 결정적 인용 검증

`MCP`는 이 파이프라인의 지식 처리 엔진이 아니다. 승인된 로컬 RAG를 Codex·ChatGPT·Claude Desktop 같은 클라이언트에 노출하는 선택적 인터페이스다. 전처리 빌더, 로컬 Qwen 챗봇, MCP 서버는 서로 별도 프로세스로 실행된다. 첫 시작 화면에서는 **로컬 Qwen 챗봇** 또는 **MCP 연결** 중 최종 사용 방법을 고른다. 이는 ④의 첫 안내와 초보자 마지막 절차를 정하는 되돌릴 수 있는 화면 선택일 뿐, MCP·Qwen·승인 색인 가운데 무엇도 삭제하거나 복제하지 않는다.

별도 로컬 RAG를 다시 구축하지 않는다. 전처리 파이프라인이 만든 승인 Vector/BM25 색인을 독립 로컬 Qwen 챗봇과 MCP가 함께 읽는다. 빌더의 `④ Qwen 규정 챗봇·AI 연결`에서 **독립 Qwen 챗봇 실행**을 누르면 localhost 전용 앱이 새 프로세스와 브라우저 창으로 열린다. 이 앱은 기관과 규정을 선택하게 하고, 선택한 규정 ID 하나와 기관 프로필을 검색 범위로 고정한다. 대화 이력은 기관·규정별 세션에 유지하며 후속 질문 검색에는 최근 사용자 질문을 문맥으로 함께 사용한다. 이전 답변은 대화 이해에만 쓰고 규정 근거로 취급하지 않는다.

로컬 Qwen 경로의 초보자 절차는 여섯 단계다: (1) 독립 localhost 챗봇 열기, (2) 기관과 규정별 승인·색인 준비 상태 확인, (3) `질문 가능` 규정 선택, (4) `Ollama · qwen3:8b 연결 확인`과 답변 모드 선택, (5) 질문 입력 후 진행률·경과 시간 확인, (6) 답변과 근거 조문·인용 확인. 질문을 보내면 검색 준비부터 인용 검증까지 현재 단계가 계속 표시된다. MCP 경로를 고르면 ④가 `MCP 생성·외부 AI 연결`로 표시되어 기존 MCP 묶음 생성과 `list_regulations`·`search`·`fetch` 연결 확인 절차를 따른다. 어느 경로에서도 독립 Qwen 앱과 MCP 서버를 별도로 실행할 수 있다.

독립 Qwen 챗봇은 기본적으로 MCP와 같은 저지연 승인 BM25 검색을 사용한다. 이 모드에서는
질문마다 1.7B 분석·질의 임베딩·CPU 재랭커를 실행하지 않고 Qwen3 8B가 짧은 답변과 근거
ID를 만든 뒤 결정론적 인용 검증을 수행한다. 화면의 `Qwen3 4B 정밀 근거 감사`는 의미 감사가
추가로 필요한 경우에만 켠다. 기본값은 꺼짐이며, 켜면 GPU에서 8B·4B 모델을 교체하느라
응답 시간이 크게 늘 수 있다.

따라서 위 7단계는 **독립 Qwen 챗봇의 기본 실행 순서가 아니다.** 모든 모델 역할을 확인하는
정밀 수용시험 또는 `retrieval_mode=auto`, `claim_audit_mode=model`인 API 경로의 순서다.
독립 챗봇 기본값은 `retrieval_mode=fast`, `claim_audit_mode=deterministic`이며
`승인 범위 확인 → BM25/lexical 검색 → Context 구성 → Qwen3 8B 답변 → 결정론적 인용 검증`
순서로 실행한다. 어느 경로도 tenant·기관 프로필·문서·승인·ACL·최신본 필터를 생략하지 않는다.

## 2. 모델 배치

| 수준 | 모델 | 역할 | 장애 시 정책 |
|---|---|---|---|
| D0 | 모델 없음 | 보안, tenant, 승인, 상태 전이, 품질, 인용 exact match | fail-closed |
| S1 | `korean_PP-OCRv5_mobile_rec` | 저추출 스캔 페이지 한국어 OCR | 사람 검수 또는 처리 차단 |
| S2-E | `Qwen/Qwen3-Embedding-0.6B` | 승인 Chunk와 질의의 의미 벡터 | BM25 degraded, semantic 필수 gate 실패 |
| S2-R | `Qwen/Qwen3-Reranker-0.6B` | ACL 통과 후보 재순위 | deterministic rank degraded |
| L1 | `qwen3:1.7b` | 질의 의도·locator 분석과 보수적 검색어 재작성 | 원 질문 + 규칙 기반 확장 |
| L2 | `qwen3:4b` | 불확실 구조 검수와 선택형 정밀 답변 주장-근거 감사 | 사람 검수 또는 답변 제한; 독립 챗봇 기본 대화에서는 OFF |
| L3 | `qwen3:8b` | 승인된 bounded Context 기반 최종 한국어 답변 | 근거 발췌형 답변 또는 unavailable |

생성 모델은 승인, ACL 변경, 색인 공개, source text 변경 권한이 없다. 최종 Qwen3 8B가 만든 인용도 신뢰하지 않고 시스템이 승인 journal과 content hash를 다시 대조해 공개 citation을 만든다.

## 3. 권장 실행 자원

정확한 처리량은 문서 크기, 양자화, CPU·GPU와 Context 길이에 따라 달라지므로 설치 후 기관 데이터로 benchmark한다.

- 최소 기능 확인: Python 3.11+, 32GB RAM, 충분한 SSD, Ollama CPU 실행 가능 환경
- 권장 운영: 32~64GB RAM, 12GB 이상 VRAM, 모델·cache·원문을 담을 암호화 SSD
- VRAM 8GB 전후: 8B를 답변 시점에 단독 적재하고 embedding/reranker는 CPU 사용
- VRAM 12~16GB 이상: 0.6B semantic 모델 상주, 4B reviewer는 전처리 batch 시점, 8B는 QA 시점에 우선 적재
- 폐쇄망: 인터넷 연결 환경에서 Python wheel과 모델 snapshot을 사전 검증한 뒤 hash manifest와 함께 반입

## 4. Python 환경 설치

저장소 루트의 PowerShell에서 실행한다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[local-ai,dev]"
```

`local-ai` extra는 PaddleOCR/PaddlePaddle과 sentence-transformers를 설치한다. GPU용 Paddle 또는 PyTorch가 필요한 환경은 기관이 승인한 CUDA 버전에 맞춰 해당 공식 wheel을 별도로 고정한다. 운영 배포에서는 검증된 lock 또는 내부 wheelhouse를 사용하고 임의 최신 버전으로 자동 갱신하지 않는다.

## 5. Ollama 모델 준비

독립 Qwen 챗봇의 기본 빠른 대화만 사용할 때는 `qwen3:8b`가 필수다.

```powershell
ollama pull qwen3:8b
ollama list
```

API 정밀 경로, 4B 정밀 감사와 전체 수용시험까지 실행할 때만 나머지 역할 모델도 받는다.

```powershell
ollama pull qwen3:1.7b
ollama pull qwen3:4b
ollama list
```

`ollama list`에 `qwen3:8b`가 보이고 Ollama가 실행 중이어야 독립 앱의 **Ollama · qwen3:8b
연결 확인**이 성공한다. 첫 확인은 8B 모델을 RAM/GPU에 적재하므로 수십 초 걸릴 수 있다.
진행 중에는 연결 버튼을 반복해서 누르지 않는다.

Ollama endpoint는 `http://127.0.0.1:11434` 또는 `localhost`만 허용된다. 사설 LAN 주소나 외부 URL을 넣으면 model router가 거부한다.

Qwen3 Embedding/Reranker와 PaddleOCR 모델은 최초 검증 시 로컬 cache에 준비된다. 인터넷 없는 운영 서버에서는 연결 환경에서 아래 검증을 먼저 수행하고, 모델 cache를 hash 검증 후 동일 경로로 반입한다. 런타임 adapter는 `local_files_only=True`를 사용하므로 운영 질의 중 Hugging Face 다운로드를 시도하지 않는다.

## 6. 안전한 환경 설정

로컬 개발 예시는 다음과 같다. 운영에서는 토큰 값을 셸 기록에 직접 남기지 말고 secret manager 또는 OS 보안 저장소로 주입한다.

```powershell
$env:APP_ENV = "local"
$env:DATA_DIR = ".\data"
$env:RAG_LLM_BACKEND = "ollama"
$env:RAG_LLM_ENDPOINT = "http://127.0.0.1:11434"
$env:RAG_LLM_MODEL = "qwen3:8b"
$env:RAG_LLM_TIMEOUT_SECONDS = "60"
$env:LOCAL_STRUCTURE_REVIEW_ENABLED = "true"
$env:LOCAL_STRUCTURE_REVIEW_MAX_NODES = "12"
$env:ENABLE_AGENT_REVIEW = "false"
```

보호 환경에서는 최소한 다음 항목도 명시한다.

```powershell
$env:APP_ENV = "production"
$env:API_AUTH_REQUIRED = "true"
$env:TENANT_STORAGE_ISOLATION = "true"
$env:API_AUDIT_ENABLED = "true"
```

- `ENABLE_AGENT_REVIEW=false`는 기존 외부 provider 검수 경로를 끈다. 이번 로컬 구조·표 검수는 별도 `LOCAL_STRUCTURE_REVIEW_ENABLED`로 함께 제어되며, `LOCAL_STRUCTURE_REVIEW_MAX_NODES`는 구조 후보와 표 후보 각각의 상한으로 사용된다.
- API 토큰, 원문 경로, 기관 식별자, raw upload를 Git·공개 report·MCP 응답에 넣지 않는다.
- tenant storage isolation을 켠 운영에서는 승인 증적과 Vector index도 같은 tenant scope에 있어야 한다.

## 7. 모델별 설치 검증

다음 명령은 mock이 아니라 실제 로컬 모델을 실행한다.

```powershell
.\.venv\Scripts\python.exe scripts\local_llm_doctor.py --backend ollama --model qwen3:8b
.\.venv\Scripts\python.exe scripts\verify_local_model_roles.py
.\.venv\Scripts\python.exe scripts\verify_local_semantic_models.py
.\.venv\Scripts\python.exe scripts\verify_paddle_ocr_runtime.py
.\.venv\Scripts\python.exe scripts\verify_local_structure_review.py
.\.venv\Scripts\python.exe scripts\verify_local_table_review.py
```

통과 기준은 다음과 같다.

- 1.7B·4B·8B 응답이 각각 자신의 JSON 계약을 만족한다.
- semantic embedding의 관련 문장 유사도가 무관 문장보다 높다.
- reranker의 관련 passage 점수가 무관 passage보다 높다.
- PaddleOCR가 생성된 한국어 fixture의 필수 용어를 confidence와 bbox와 함께 복원한다.
- 구조 reviewer는 허용된 node ID와 exact source quote만 사용하며 원문을 바꾸지 않는다.

검증 보고서는 `reports/image_pipeline_6hour/` 아래에 생성되며 공개 소스에 포함하지 않는다.

## 8. 서버와 운영 UI 시작

API와 UI를 각각 다른 PowerShell에서 실행한다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
.\.venv\Scripts\python.exe -m streamlit run frontend\streamlit_app.py --server.address 127.0.0.1
```

Qwen 챗봇은 빌더와 별도 프로세스로 실행한다. 빌더의 **독립 Qwen 챗봇 실행** 버튼을 한 번
누르거나, 빌더 없이 다음 명령을 실행한다. 두 방법 모두 `127.0.0.1`에만 바인딩한다.

```powershell
.\RUN_QWEN_CHAT.bat
# 또는 설치된 콘솔 명령
reg-rag-qwen-chat
```

선택한 규정에서 `제1조에 대해서 알려줘`처럼 시간 조건이 없는 단일 조문 번호를 물으면,
tenant·기관 프로필·문서·승인·ACL·최신본 필터를 적용한 결과 안에서 조문 번호를 정확히
조회한다. 이 경로는 질문 분석 모델, 질의 임베딩, 재랭커를 실행하지 않는다. 존재하지 않는
조문은 의미가 비슷한 다른 조문으로 대체하지 않고 근거 없음으로 처리한다. 독립 Qwen의 복수
조문·항/호·기준일·일반 자연어 질문도 승인 BM25/lexical 빠른 검색을 사용한다. Qwen3 1.7B,
Embedding, Reranker를 포함한 전체 하이브리드 경로는 API 기본 모드와 정밀 수용시험에서 유지된다.

진행 화면에서 `2/5 승인된 규정 조항을 찾고 있습니다`가 수 초 이상 계속되면 구버전 챗봇
프로세스인지 먼저 확인하고 독립 Qwen 앱만 다시 실행한다. 빌더와 MCP 서버는 재시작할 필요가
없다. 정확 조문 검색이 끝난 뒤의 `4/5 Qwen3 8B` 답변 생성 시간은 로컬 CPU·GPU와 모델
적재 상태에 따라 달라진다. `Qwen3 4B 정밀 근거 감사`를 켠 경우에만 4B 감사와 모델 교체
시간이 추가된다.

이전 대화에 다른 조문 번호가 있어도 현재 질문이 `제5조 내용은 뭐야`처럼 스스로 완결된
단일 조문 질문이면 현재 질문만 검색한다. `그 내용은?`처럼 앞 질문 없이는 뜻이 불분명한
후속 질문만 최근 사용자 질문을 결합한다. 이전 사용자 질문도 현재 질문과 같은 입력 보안
정책을 통과해야 하며, assistant 답변은 검색 근거로 사용하지 않는다.

운영자는 다음 순서를 지킨다.

1. 문서를 업로드하고 signature·크기·tenant admission 결과를 확인한다.
2. 파싱/OCR coverage와 `review_required` 사유를 확인한다.
3. 조·항·호·목 구조, 표·별표·별지, source page provenance를 원문과 비교한다.
4. 품질 blocker를 해소한 Chunk만 사람 이름과 승인 근거를 남겨 승인한다.
5. 승인 journal과 content hash가 일치하는 Chunk만 `Qwen/Qwen3-Embedding-0.6B`로 색인한다.
6. 전체 정밀 수용시험의 QA trace에서 query 1.7B, hybrid retrieval, reranker, context, answer 8B, audit 4B, citation 검증 상태를 확인한다.
7. 운영 UI에서 **독립 Qwen 챗봇 실행**을 누른 뒤 새 창에서 기관과 `질문 가능` 규정을 선택한다. 연결 확인 후 정확 조문 질문과 지시형 후속 질문을 각각 실행한다. 정확 조문은 `contextualized_search=false`, `그 내용은?` 같은 지시형 질문은 `true`인지 확인한다.

`GET /api/pipelines/manifest`는 원문이나 로컬 경로 없이 15단계 정의, 역할, 구현 상태, 모델 profile과 실패 정책을 반환한다.

## 9. 15단계 실제 수용시험

Ollama와 모든 semantic/OCR cache가 준비된 후 실행한다.

```powershell
.\.venv\Scripts\python.exe scripts\run_image_pipeline_acceptance.py
```

이 시험은 합성 한국어 규정을 실제 업로드·전처리하고, 사람이 승인했다는 journal을 API로 기록한 뒤, Qwen3 Embedding 색인과 실제 1.7B → Reranker → 8B → 4B QA를 수행한다. 시험 중 socket은 loopback만 허용되며 비-loopback 연결 시도는 즉시 실패한다.

완료 판정은 모두 만족해야 한다.

- `stage_count=15`, `passed_stage_count=15`, `passed=true`
- `external_api_call_count=0`, `local_only=true`
- 승인 actor와 approval journal 존재
- 답변의 모든 공개 인용이 `E1` 형식이며 exact support quote와 승인 hash에 결합됨
- 임시 원문·index·approval 파일이 종료 후 삭제되고 Git 상태에 남지 않음

## 10. 릴리스 게이트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m build --sdist --wheel
.\.venv\Scripts\python.exe scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

파싱·전처리 로직을 바꿨다면 `docs/preprocessing_change_governance_ko.md`도 적용한다. focused regression test, 보호된 PR template 필드, Code Owner review, `preprocessing-reviewed` label 없이는 병합하지 않는다.

## 11. 장애 분리표

| 증상 | 우선 확인 | 안전한 처리 |
|---|---|---|
| 독립 챗봇에 선택 가능한 규정 없음 | 활성 청크 검수 결정, 승인 journal, 색인 수, stale 레코드 | 빌더 ③에서 승인·색인을 완료하고 필요하면 문서 색인 복구; 미승인 규정으로 우회 금지 |
| `qwen3:8b` 연결 확인 실패 | `ollama list`, Ollama 프로세스, `127.0.0.1:11434` | `ollama pull qwen3:8b` 후 Ollama 재시작; 외부 endpoint로 우회 금지 |
| 첫 8B 연결 확인이 오래 걸림 | RAM/VRAM 사용량, Ollama model load | 경과 시간을 보며 한 번만 기다리고 중복 실행 금지; 연결 후 동일 세션 재질문으로 warm 상태 확인 |
| 기본 대화가 4B 단계에서 오래 걸림 | `Qwen3 4B 정밀 근거 감사` 토글 | 일반 대화는 기본 OFF로 복귀; 정밀 의미 감사가 필요한 경우에만 ON |
| 근거 부족 고정 답변 | 선택 문서, 조문 존재 여부, 승인·색인 상태 | 다른 조문을 임의 대체하지 말고 원문·승인 범위를 확인한 뒤 필요한 규정을 승인·재색인 |
| OCR 결과 없음 | Paddle model cache, 렌더링 페이지, 추출 coverage | normalize 진행 금지, review/blocked |
| `qwen3:1.7b` JSON 오류 | Ollama 상태, timeout, schema error trace | 원 질문과 deterministic rewrite 사용 |
| Embedding load 실패 | local cache, 모델명, RAM | 기존 승인 색인을 유지하고 BM25 degraded로 답변 계속; semantic 릴리스 차단 |
| Reranker OOM | 후보 수, CPU/GPU profile | deterministic rank로 복귀, trace에 degraded |
| 8B timeout | Ollama lease, Context budget, timeout | extractive 또는 unavailable; 승인 상태 불변 |
| 4B 감사 실패 | cited snippet, evidence ID, schema | 검증되지 않은 Qwen 주장은 반환하지 않고 승인 근거 발췌 답변+인용으로 제한 |
| citation hash 불일치 | 재처리·재승인 여부 | 답변 차단 후 재승인·재색인 |
| 다른 tenant 결과 노출 | auth scope, 저장소 격리, index manifest | 즉시 검색 중단·보안 사건 처리 |

## 12. 운영 승격 체크리스트

- [ ] 기관별 실제 PDF/DOCX/HWP/HWPX와 스캔 표본 goldset 통과
- [ ] 표·별표·별지 fixture를 포함한 reviewer 실데이터 검증 완료
- [ ] 역할별 latency, CPU/RAM/VRAM, 동시 질의, cold-start 예산 측정
- [ ] tenant 간 교차 검색·미승인 Chunk·과거 버전 노출 0건
- [ ] 근거 없음 abstention 100%, citation precision 목표 충족
- [ ] backup/restore, index 원자 교체, 중단 후 재개 훈련 완료
- [ ] 개인정보·기관 보안 검토와 모델·dependency SBOM 승인
- [ ] 전체 unittest, build, release hygiene, 15단계 실제 수용시험 통과
- [ ] 전처리 변경 governance와 Code Owner 승인 완료

코드 수용시험 통과는 제품 후보가 되기 위한 기술 조건이다. 실제 판매·기관 배포 승격은 해당 기관의 문서 goldset, 성능 예산, 보안 심사, 사용자 검수 절차까지 통과한 뒤 결정한다.
