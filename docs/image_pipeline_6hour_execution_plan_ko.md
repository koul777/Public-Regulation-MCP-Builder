# 이미지 구조 완성용 6시간 실행 계획

## 1. 목표와 완료 정의

대상은 이미지에 제시된 두 제품 흐름이다.

1. 규정 전처리기: 문서 업로드부터 승인된 청크의 VectorDB 입력까지 8단계
2. 로컬 RAG QA: 사용자 질문부터 Qwen3 8B 답변과 근거 조문 제시까지 7단계

이 계획에서 말하는 “완료”는 화면이나 클래스 이름만 존재하는 상태가 아니다. 아래 조건이 모두 충족돼야 완료로 판정한다.

- 모든 이미지 단계가 실제 실행 코드와 1:1로 연결된다.
- 단계마다 입력, 출력, 소유 역할, 실패 상태와 감사 trace가 존재한다.
- 원문, 승인 상태, tenant 및 보안 범위를 우회하는 경로가 없다.
- Qwen3 8B는 승인된 검색 근거만 입력받고, 근거가 없으면 답변을 거절한다.
- 응답의 규정명·조·항·호와 evidence ID가 실제 검색 결과에 존재하는지 검증한다.
- API와 Streamlit에서 동일한 실행 결과·상태·오류를 확인할 수 있다.
- 전체 단위 테스트, E2E 테스트, 공개 릴리스 위생 검사, 패키지 빌드가 통과한다.
- 실패한 검증이 하나라도 있으면 “완료”로 표시하지 않는다.

6시간은 코드와 자동 검증을 닫는 집중 구현 시간이다. 기관별 실제 문서 전체에 대한 법률적 정확성 인증, 장시간 부하 시험, 고객사별 보안 심사는 별도의 출시 승인 단계로 유지한다.

## 1.1 이미지 개념 충실도 절대 게이트

이미지의 상자 이름이나 `pipeline_trace`만 추가한 상태는 구현으로 인정하지 않는다. 아래 15개 개념은 각각 실제 입력을 받아 실제 산출물을 만들고, 다음 단계가 그 산출물을 소비해야 한다.

| 이미지 개념 | 실제 구현 계약 | 완료로 인정하지 않는 상태 |
|---|---|---|
| 문서 업로드 | PDF/HWP/HWPX/DOCX admission, signature, hash, tenant, version 생성 | 파일 선택 UI만 존재 |
| 파싱 | 본문 text, page, block, 표, 이미지/OCR 신호를 `ParsedDocument`로 생성 | 확장자만 판별하거나 원문 전체를 한 문자열로 저장 |
| 정규화 | encoding·Unicode·공백·서식 표기를 정리하면서 원문 provenance 유지 | 원문을 LLM으로 재작성 |
| 조문 구조 인식 | 장·절·조·항·호·목·별표·부칙 tree와 parent path 생성 | 정규식 결과를 평면 목록으로만 저장 |
| Chunk 생성 | 조문 의미 단위, 계층 context, source page, 시행일, 버전 생성 | 고정 글자 수로만 자름 |
| 품질 검사 | 누락·중복·고아 구조·표 손실·근거 연결을 자동 검출하고 review state 결정 | score 숫자만 반환 |
| Export | JSONL/CSV/Markdown/table export가 동일 핵심 데이터를 보존 | 화면에서 JSON을 보여주기만 함 |
| VectorDB 입력 | 승인 Chunk의 실제 embedding, metadata, atomic upsert, index manifest 생성 | 입력 예정 payload만 생성하거나 미승인 데이터 포함 |
| 질문 분석 | 의도, 규정명, 조문 locator, 날짜·버전 조건을 구조화 | trace에 `query_analysis` 이름만 기록 |
| 검색어 보정 | 조문 표기·띄어쓰기·alias를 반영한 실제 검색 query 생성 | 원 질문을 그대로 재사용하면서 보정 완료로 표시 |
| Hybrid retrieval | keyword/BM25와 semantic vector 후보를 각각 검색하고 결합 | 한 검색 결과를 두 모델처럼 표시하거나 hash fallback만 상용 완료로 표시 |
| Reranking·필터링 | 관련도 재순위와 승인·tenant·ACL·버전 필터를 실제 적용 | 점수 변경 없이 stage 이름만 추가 |
| Context 구성 | dedupe, 조문 병합, token budget, citation metadata를 포함한 Qwen 입력 생성 | 검색 결과 전체를 무제한 연결 |
| 로컬 LLM | localhost의 실제 `qwen3:8b`가 context만 사용해 답변 | mock 응답, 외부 API, 모델 미설치 fallback을 최종 완료로 간주 |
| 답변+근거 조문 | 답변 주장과 실제 규정명·장·조·항·호 evidence를 검증해 함께 반환 | 존재 여부를 확인하지 않은 인용 문자열 생성 |

시스템 전체 로컬 실행 역시 독립 게이트다. Parser, semantic Embedding Model, VectorDB, Retriever/Reranker, Qwen3 8B가 모두 내부 PC에서 실행되고 네트워크 계측상 외부 API 호출이 0건이어야 이미지의 “완전 로컬 환경”을 충족한다.

이미지 예시 질문인 “접근권한은 누가 관리해야 하나요?”를 최종 기준 시나리오로 포함한다. 합격 응답은 다음 특성을 모두 가져야 한다.

- 승인된 최신 규정에서 접근권한 관리 주체를 찾는다.
- 답변에서 관리 주체와 의무를 짧게 설명한다.
- 규정명, 장, 조, 항 또는 이에 준하는 정확한 locator를 반환한다.
- 인용 text는 저장된 evidence와 일치한다.
- 관련 조문이 없으면 규정을 만들어내지 않고 확인 불가로 답한다.

개념 충실도 판정은 `implemented`, `verified`, `blocked` 세 축으로 분리한다. 코드가 있어도 실제 Qwen 또는 semantic embedding을 실행하지 못했다면 `implemented=true`, `verified=false`이며 전체 완료는 아니다.

## 2. 현재 기준선

이미 구현된 기반은 보존하고 확장한다.

- `regulation_preprocessing_v1` 8단계와 `local_regulation_qa_v1` 7단계 계약
- PDF/HWP/HWPX/DOCX 파서, Windows OCR 경로, Kordoc 표 추출 경로
- extraction quality, 구조·청크·품질·승인·export·VectorDB 코드
- 승인된 청크만 로컬 벡터 저장소에 넣는 보안 게이트
- BM25와 로컬 hash embedding의 하이브리드 검색 골격
- Qwen3 8B Ollama 기본 모델명 `qwen3:8b`
- grounded answer 및 citation verifier 에이전트 골격
- 단계별 `pipeline_trace`와 `/api/pipelines/manifest`

6시간 동안 우선 닫아야 할 차이는 다음과 같다.

- 일부 에이전트 역할이 `planned` 또는 `active_partial`로 남아 있다.
- 질의분석·검색어보정·재순위·context 구성이 trace상 단계와 실제 독립 실행 단위로 완전히 분리되지 않았다.
- hash embedding은 결정론적 fallback에는 유용하지만 의미 검색 품질을 대표하는 상용 로컬 embedding 프로필은 아니다.
- 문장별 주장과 인용 근거의 연결 검증이 더 엄격해야 한다.
- 실제 Ollama/Qwen3 8B E2E, 성능 예산, 설치 패키지 검증 증거가 부족하다.

현재 local hash embedding은 테스트와 장애 fallback 용도로만 인정한다. 이미지의 “Embedding Model + VectorDB”와 “Hybrid Search”를 완성하려면 한국어 의미 검색이 가능한 실제 로컬 semantic embedding backend의 실행 증거가 필수다.

## 3. 360분 시간표

| 시간 | 누적 | 작업 블록 | 반드시 남길 산출물 |
|---|---:|---|---|
| 00:00~00:30 | 30분 | 기준선·계약·테스트 고정 | gap matrix, 변경 대상 목록, baseline 결과 |
| 00:30~01:30 | 90분 | 전처리 1~3단계 완성 | 업로드/파싱/OCR/표/정규화 회귀 테스트 |
| 01:30~02:20 | 140분 | 전처리 4~7단계 완성 | 계층 구조·청크·품질·export fixture |
| 02:20~03:10 | 190분 | 승인·Embedding·VectorDB 완성 | 승인 게이트, 로컬 embedding adapter, index manifest |
| 03:10~04:15 | 255분 | Retrieval·Rerank·Context 완성 | 실제 5단계 검색 파이프라인, 품질평가 결과 |
| 04:15~05:10 | 310분 | Qwen3 8B·근거답변·인용검증 완성 | 로컬 LLM E2E, abstention·citation 테스트 |
| 05:10~05:40 | 340분 | 운영 UI·관측성·복구 완성 | Streamlit 상태화면, doctor, 오류 복구 테스트 |
| 05:40~06:00 | 360분 | 전체 릴리스 게이트 | 전체 테스트·빌드·위생검사·최종 보고서 |

### 3.1 분 단위 실행 보드

| 시간 | Orchestrator가 발행할 작업 | 담당 에이전트/모델 | 코드 초점 | 종료 게이트 |
|---|---|---|---|---|
| 00:00~00:10 | 저장소·변경·테스트 기준선 수집 | workflow_orchestrator D0 | `git status`, 기존 report, test inventory | baseline artifact 생성 |
| 00:10~00:20 | 이미지 15단계와 코드 gap 계산 | workflow_orchestrator D0 | pipeline manifest, role registry | 단계별 ready/partial/missing 확정 |
| 00:20~00:30 | task/result schema와 모델 profile 고정 | workflow_orchestrator D0 | pipeline/agent schema | 계약 테스트 통과 |
| 00:30~00:45 | 업로드 admission과 tenant 경계 검증 | intake_guard D0 | document service, file store | 위조·빈 파일·타 tenant fixture 통과 |
| 00:45~01:00 | native parser 결과 통합 | parser_dispatcher + native extractor D0 | PDF/DOCX/HWP/HWPX parser | page/block/table provenance 확인 |
| 01:00~01:15 | image-only OCR와 표 추출 연결 | OCR S1 + table extractor | PDF OCR adapter, Kordoc | OCR confidence·표 geometry 생성 |
| 01:15~01:30 | extraction quality와 정규화 | quality D0 + normalizer D0 | extraction report, normalizer | 누락 page 차단·원문 hash 보존 |
| 01:30~01:45 | 규정 계층 tree 생성 | structure detector D0 | structure detector | 장·절·조·항·호·목 fixture 일치 |
| 01:45~02:00 | 불확실 구조·표만 모델 검수 | structure reviewer L2 4B | review adapter | source span 없는 제안 거부 |
| 02:00~02:10 | 조문 의미 Chunk 생성 | chunk builder D0 | chunker | parent path·source page 포함 |
| 02:10~02:20 | 품질 게이트와 export parity | quality/export D0 | quality gate, exporter | worklist·JSONL·CSV parity 통과 |
| 02:20~02:35 | 사람 승인 journal과 hash 검증 | human gate + integrity D0 | approval service | 미승인 index 시도 차단 |
| 02:35~02:50 | 승인 Chunk semantic embedding | semantic embedder S2-E 0.6B | embedding adapter | 1024차원·model ID 기록 |
| 02:50~03:05 | atomic VectorDB/BM25 index 생성 | index writer D0 | vector upsert, BM25 index | manifest snapshot 일치 |
| 03:05~03:10 | 인덱스 보안 smoke | index integrity D0 | index audit | stale·cross-tenant 0건 |
| 03:10~03:25 | 질문 분석·검색어 보정 JSON | query agents L1 1.7B | query analyzer/rewriter | schema·locator 보존 통과 |
| 03:25~03:40 | query embedding과 양쪽 후보 검색 | embedder S2-E + retrieval D0 | BM25/vector retrieval | 독립 candidate set 증거 생성 |
| 03:40~03:55 | 전용 reranker와 이중 ACL 필터 | reranker S2-R + guard D0 | reranker/filter | 타 tenant·미승인 후보 0건 |
| 03:55~04:05 | context dedupe·병합·budget | context builder D0 | context builder | token·evidence contract 통과 |
| 04:05~04:15 | retrieval benchmark와 실패 query 분석 | evaluator D0 | retrieval eval | Recall@5·MRR 기록 |
| 04:15~04:30 | Qwen doctor·prompt·모델 lease 확인 | model runtime guard D0 | local LLM adapter | localhost·qwen3:8b 확인 |
| 04:30~04:50 | 최종 근거답변 생성 | grounded answerer L3 8B | answer agent | context 밖 조문 생성 0건 |
| 04:50~05:00 | 주장 단위 semantic 근거 감사 | claim auditor L2 4B | claim audit agent | unsupported claim 표식 |
| 05:00~05:10 | exact citation·locator·hash 검증 | citation verifier D0 | citation verifier | citation precision 100% |
| 05:10~05:20 | UI 단계·모델·fallback 상태 연결 | UI presenter D0 | Streamlit/API | UI와 trace 상태 일치 |
| 05:20~05:30 | timeout·재시도·중단 복구 | workflow orchestrator D0 | durable workflow | 중복 승인·index 0건 |
| 05:30~05:40 | 이미지 예시 질문 E2E | 전체 agent graph | API/UI smoke | 답변+정확 근거 조문 출력 |
| 05:40~05:50 | focused·security·syntax gate | test runner D0 | 신규/보안 테스트 | 실패 0건 |
| 05:50~05:58 | 전체 회귀·build·hygiene 병렬 검증 | release orchestrator D0 | unittest/build/audit | 필수 gate exit 0 |
| 05:58~06:00 | acceptance matrix 봉인 | workflow orchestrator D0 | final report | 완료/차단 근거 기록 |

마지막 20분의 병렬 검증은 서로 같은 runtime data를 쓰지 않는 독립 작업만 병렬화한다. 전체 테스트 시간이 예산을 넘으면 테스트를 중단하지 않고 6시간 종료 상태를 `verification_running`으로 보고하며, 실제 종료 코드를 받은 뒤에만 release status를 확정한다.

## 4. 블록별 상세 실행안

### 블록 A — 00:00~00:30: 기준선과 변경 계약 고정

목적은 6시간 동안 “완료 기준”이 흔들리지 않게 만드는 것이다.

실행 작업:

1. `AGENTS.md`, 이미지 구현 문서, 파이프라인 정의, 역할 registry를 기준으로 이미지 단계별 코드 소유자를 확정한다.
2. 현재 변경 파일을 보존하고 unrelated 변경과 구현 대상 변경을 구분한다.
3. 다음 baseline을 기록한다.
   - 전체 `unittest` 결과
   - `local_llm_doctor` 결과
   - release hygiene 결과
   - package build 가능 여부
   - Ollama와 `qwen3:8b` 설치 여부
4. `reports/image_pipeline_6hour/` 아래에 실행 보고서와 machine-readable JSON의 스키마를 고정한다.
5. 이미지의 각 단계에 대해 `ready`, `partial`, `missing`, `blocked_external` 중 하나를 부여한다.

완료 기준:

- 15개 단계가 모두 코드 파일·테스트·산출물과 매핑된다.
- 외부 설치가 필요한 항목과 코드만으로 완료 가능한 항목이 분리된다.
- baseline 실패 목록이 0건이거나, 기존 실패와 이번 변경 실패가 구분된다.

체크포인트 00:30:

- `gap_matrix.json`
- `baseline_report.md`
- 변경 파일 목록과 rollback 가능한 단위

### 블록 B — 00:30~01:30: 전처리 1~3단계 완성

대상 단계:

1. 문서 업로드
2. 파싱 및 텍스트·이미지·표 인식
3. 인코딩·공백·서식·용어 정규화

구현 작업:

1. 업로드 admission
   - 확장자와 실제 signature 불일치 차단
   - 크기 제한, SHA-256, 중복, tenant, 보안등급 기록
   - 원본 경로나 기관 식별자가 API·trace에 노출되지 않는지 검증
2. 파싱·추출
   - PDF 텍스트 block, 이미지 포함 페이지, 이미지 전용 페이지를 구분
   - 텍스트가 없는 스캔 PDF는 OCR 성공 전까지 `blocked`
   - OCR 텍스트는 자동 승인하지 않고 `review_required`
   - DOCX/HWP/HWPX의 표·머리글·쪽 번호 provenance 보존
   - Kordoc 표 결과와 기본 parser 결과의 불일치를 검수 사유로 승격
3. extraction quality
   - 페이지 커버리지, 문자 수, block 수, 표 수, 이미지 페이지 수
   - OCR 페이지와 parser uncertainty flag
   - `pass`, `review_required`, `blocked`의 결정 규칙 고정
4. 정규화
   - Unicode, 개행, 공백, 목록 기호, 조문 표기 정규화
   - 원문 text와 normalized text의 출처 연결 유지
   - 정규화가 법률 문구를 추가·삭제하지 않는 property test 추가

테스트 fixture:

- 정상 text PDF
- image-only PDF
- text+image 혼합 PDF
- 다중 표 DOCX
- HWP/HWPX 표본
- 손상 signature 및 빈 파일

완료 기준:

- 추출되지 않은 페이지가 조용히 누락되지 않는다.
- OCR·표 불확실성이 quality JSON과 review queue에 동일하게 나타난다.
- parser 실패 시 현재 단계가 `failed` 또는 `blocked`로 남는다.

체크포인트 01:15 및 01:30:

- parser fixture 테스트 결과
- extraction coverage JSON
- 실패·검수 사유 코드 목록

### 블록 C — 01:30~02:20: 전처리 4~7단계 완성

대상 단계:

4. 조문 구조 인식
5. Chunk 생성
6. 품질 검사
7. Export

구현 작업:

1. 계층 구조
   - 문서 > 편/장 > 절 > 조 > 항 > 호 > 목
   - 부칙, 별표, 별지, 서식의 독립 노드와 parent path
   - 규정명, 조문 제목, 시행일, 버전, source page 보존
2. Chunk 계약
   - 기본 단위는 한 조문 또는 의미상 분리 가능한 하위 단위
   - 장·절·규정명 context header 포함
   - 표는 행·셀 구조와 사람이 읽는 Markdown을 함께 보존
   - 지나치게 긴 조문은 항 경계에서 나누고 부모 조문 ID를 유지
3. 품질 게이트
   - 누락, 중복, 고아 노드, 역전된 계층, 빈 조문, 잘린 표 검출
   - 구조 품질과 추출 품질을 하나의 review worklist로 합침
   - 사람이 승인하기 전 상태는 `pending_review`
4. Export
   - JSONL, CSV, Markdown, table JSONL/CSV의 필드 parity 검증
   - 이미지 예시 필드 `document`, `chapter`, `article`, `article_title`, `paragraph`, `text`, `source_page`, `effective_date`, `version`, `parent_path`를 필수 계약으로 고정
   - export에 raw local path·비밀·원본 파일명이 섞이지 않게 검사

완료 기준:

- 계층 fixture의 expected tree와 실제 tree가 일치한다.
- 같은 조문이 중복 Chunk로 생성되지 않는다.
- 모든 Chunk가 document ID, source page 또는 명시적 page unknown 사유를 가진다.
- export 형식 간 핵심 필드와 row count가 일치한다.

체크포인트 02:00 및 02:20:

- 구조 tree snapshot
- chunk contract test
- export parity report

### 블록 D — 02:20~03:10: 승인·Embedding·VectorDB 완성

대상 단계:

8. VectorDB 입력 데이터 생성과 저장

구현 작업:

1. 승인 경계
   - 승인 journal과 승인 당시 content hash가 일치하는 Chunk만 인덱싱
   - 수정·반려·보안등급 변경 시 기존 vector를 stale로 판정
   - tenant·부서 ACL·보안등급을 embedding 전에 검증
2. embedding adapter
   - 현재 local hash embedding을 결정론적 테스트 fallback으로 유지
   - 상용 프로필은 localhost의 로컬 semantic embedding backend를 adapter 뒤에 연결
   - 모델 ID, 차원, 문서 content hash, 생성 시간을 index metadata에 저장
   - semantic backend가 없을 때 자동으로 품질을 가장하지 않고 명시적 fallback 상태 기록
3. VectorDB와 BM25
   - 문서 단위 atomic upsert
   - 중복 ID, 차원 불일치, 다른 tenant record 차단
   - BM25 index와 vector manifest가 같은 승인 snapshot을 가리키는지 검증
4. 단계 trace
   - `vector_index` 시작·완료·실패·dry-run 상태
   - record count, embedding model, index version을 path-free 형태로 기록

완료 기준:

- 미승인·반려·stale·타 tenant Chunk 유입 0건
- 동일 승인 snapshot 재인덱싱은 멱등적
- 인덱스 변경 중 실패해도 이전 정상 인덱스 보존
- indexing job과 실제 저장 record 수 일치

체크포인트 02:45 및 03:10:

- approval-to-index E2E 결과
- vector integrity report
- embedding backend/fallback 상태

### 블록 E — 03:10~04:15: 검색 5단계 실제 실행 경로 완성

대상 단계:

1. 질문 분석
2. 검색어 보정
3. Hybrid retrieval
4. Reranking 및 필터링
5. Context 구성

구현 작업:

1. 질문 분석기
   - 규정명, 조/항/호, 날짜, 버전, 기관, 정의어, 별표/서식 의도 추출
   - 자유 질문과 정확 조문 조회를 구분
2. 검색어 보정
   - 조문 표기 변형, 띄어쓰기, Unicode, 규정명 alias를 결정론적으로 확장
   - 원 질문은 보존하고 확장 검색어를 trace에 원문 없이 code/count 형태로 기록
3. Hybrid retrieval
   - ACL과 승인 filter를 후보 생성보다 먼저 적용
   - BM25와 local vector 후보를 각각 생성
   - RRF 또는 검증된 score normalization으로 결합
4. Reranker
   - 메타데이터 일치, 조문 locator, 버전·시행일, 표/본문 유형을 반영
   - 로컬 semantic reranker가 없을 때 deterministic rerank로 명시적 degrade
5. Context builder
   - 중복 Chunk 제거
   - 같은 조문의 연속 Chunk 병합
   - token budget 안에서 규정명·조문·source page·effective date 포함
   - prompt injection 문구를 데이터로 취급하고 시스템 지시로 실행하지 않음

품질평가 세트:

- 정확 조문 조회
- 자연어 정책 질문
- 정의어 질문
- 별표·표 질문
- 현재/과거 버전 질문
- 근거 없음 질문
- 타 tenant 자료 유도 질문
- prompt injection 포함 문서 질문

완료 기준:

- fixture 기준 Recall@5 목표 0.90 이상
- 정확 조문 locator 질의 Top-1 목표 0.95 이상
- 타 tenant·미승인 후보 노출 0건
- context 내 중복 evidence ID 0건
- 검색 fallback 여부가 API와 trace에 표시됨

체크포인트 03:30, 04:00 및 04:15:

- retrieval benchmark JSON
- 실패 query 목록
- context budget·중복 제거 결과

### 블록 F — 04:15~05:10: Qwen3 8B와 근거답변 완성

모델 계약:

- 기본 answer model: Ollama `qwen3:8b`
- endpoint: loopback/localhost만 허용
- 외부 API 호출: 0
- 입력: 사용자 질문과 승인된 grounding context만 허용

구현 작업:

1. local LLM doctor
   - Ollama 연결, 모델 설치, endpoint, timeout, model ID 확인
   - 실제 모델이 없으면 해당 검증을 `blocked_external`로 표시하고 성공으로 위장하지 않음
2. prompt contract
   - 근거 밖 추론 금지
   - 모르면 명시적으로 답변 거절
   - 답변 문장마다 evidence ID 또는 조문 locator 연결
   - 원문에 없는 규정명·조문 번호 생성 금지
3. 답변 agent
   - timeout, 연결 실패, 빈 응답, 비정상 JSON 처리
   - 운영 모드에서는 안전한 extractive fallback 또는 명시적 503 정책 중 설정된 계약 준수
4. citation verifier
   - 모든 인용 ID가 검색 evidence에 존재하는지 확인
   - 인용 규정명·조·항·호가 evidence metadata와 일치하는지 확인
   - 검증 실패 문장은 제거하거나 전체 답변을 abstain 처리
5. 출력 filter
   - 로컬 경로, prompt, 보안 metadata, 다른 tenant 식별자 제거
   - 답변과 별도로 구조화된 citations 배열 반환

완료 기준:

- 근거 없는 질문의 hallucinated answer 0건
- citation precision 100%
- 인용 없는 핵심 주장 0건
- Qwen 실패가 승인·인덱스 상태를 변경하지 않음
- Qwen에 raw file path와 미승인 Chunk가 전달되지 않음

체크포인트 04:45 및 05:10:

- Qwen doctor 결과
- grounded QA fixture 결과
- hallucination·citation failure 사례와 차단 증거

### 블록 G — 05:10~05:40: 운영 UI·관측성·복구

구현 작업:

1. Streamlit에 8단계 전처리와 7단계 QA 진행 상태를 동일 ID로 표시한다.
2. 사용자가 볼 수 있는 상태를 `대기`, `실행`, `검수 필요`, `차단`, `완료`, `실패`로 통일한다.
3. 추출 품질, 승인 대기 수, 인덱스 상태, 검색 fallback, Qwen 연결 상태를 한 화면에서 확인하게 한다.
4. 재시도는 실패 단계부터 시작하되 승인 journal과 이전 정상 인덱스를 훼손하지 않는다.
5. 감사 로그에는 actor, tenant scoped 여부, stage, outcome, reason code, duration만 남기고 원문과 로컬 경로는 남기지 않는다.

완료 기준:

- UI 상태와 API/job trace가 불일치하지 않는다.
- 새로고침 후에도 마지막 durable 상태가 복구된다.
- 실패 원인과 운영자 조치가 한국어로 표시된다.
- 재시도·취소가 승인 또는 vector를 중복 생성하지 않는다.

체크포인트 05:30 및 05:40:

- UI contract 테스트
- 실패 복구 시나리오 결과
- path-free audit 샘플

### 블록 H — 05:40~06:00: 최종 릴리스 게이트

순서대로 실행한다.

1. 변경 파일 문법 및 `git diff --check`
2. 신규 focused tests
3. 전체 `python -m unittest discover -s tests -v`
4. source distribution 및 wheel build
5. release hygiene와 source path scan
6. Qwen3 8B 실제 E2E smoke
7. 외부 네트워크 호출 0건 검증
8. 이미지 15단계 acceptance matrix 재평가

완료 판정:

- 실제 semantic embedding, 실제 reranking, 실제 Qwen3 8B를 포함한 모든 필수 게이트 통과: `release_candidate`
- 코드 게이트는 통과했지만 실제 모델·기관 문서가 없음: `code_complete_external_validation_pending`
- 보안·승인·tenant·인용 게이트 실패: `blocked`, 출시 금지
- 단위 테스트나 package build 실패: `failed`, 완료 금지

최종 산출물:

- `reports/image_pipeline_6hour/final_report.md`
- `reports/image_pipeline_6hour/acceptance_matrix.json`
- `reports/image_pipeline_6hour/test_results.json`
- `reports/image_pipeline_6hour/retrieval_benchmark.json`
- `reports/image_pipeline_6hour/qwen_smoke.json`
- 변경 파일과 미해결 위험 목록

## 5. 다중 모델 에이전트 오케스트레이션 계약

### 5.1 모델 수준과 라우팅 원칙

모델은 크기가 아니라 역할 적합성으로 배치한다. 권한이 큰 작업일수록 큰 LLM을 쓰는 것이 아니라 deterministic 검증을 강화한다.

| 수준 | 모델 프로필 | 담당 작업 | 기본 실행 방식 | 선택 이유 |
|---|---|---|---|---|
| D0 | 모델 없음 | 오케스트레이션, 파일·tenant·ACL·승인·저장·품질 hard gate | Python deterministic service | 재현성과 fail-closed가 우선 |
| S1 | `korean_PP-OCRv5_mobile_rec` | 스캔 이미지의 한국어·영문·숫자 OCR | 로컬 PaddleOCR, 필요한 페이지만 호출 | 생성 LLM이 아닌 문자 인식 전용 모델 |
| S2-E | `Qwen3-Embedding-0.6B` | 문서·질의 semantic embedding | 로컬 sentence-transformers/TEI adapter, 1024차원 기본 | 100개 이상 언어와 instruction-aware retrieval 지원 |
| S2-R | `Qwen3-Reranker-0.6B` | query-passage 관련도 재순위 | 로컬 CrossEncoder, 후보 50개 이하 | embedding과 다른 ranking 전용 목적 모델 |
| L1 | `qwen3:1.7b` | 질문 의도 분석, locator·필터·검색어 JSON 생성 | Ollama, non-thinking, temperature 0 | 짧은 구조화 작업에 8B를 사용하지 않음 |
| L2 | `qwen3:4b` | 불확실 구조·표 검수, 답변 주장-근거 semantic 감사 | Ollama, non-thinking, temperature 0 | 규정 구조 판단과 모순 검출에 중간 수준 추론 사용 |
| L3 | `qwen3:8b` | 최종 근거 기반 한국어 답변 | Ollama, context 제한, 낮은 temperature | 가장 높은 언어·추론 품질이 필요한 사용자 응답 전용 |

현재 hash embedding은 테스트와 semantic backend 장애 시 fallback이다. S2-E가 실제로 실행되지 않은 상태는 이미지의 Hybrid Search 완성으로 인정하지 않는다. Qwen3 Embedding 계열은 0.6B·4B·8B embedding/reranker를 제공하지만, 6시간 구현의 기본은 속도와 메모리 균형을 위해 0.6B 전용 모델로 고정한다.

모델 승격 규칙:

1. D0 규칙으로 정확히 처리할 수 있는 작업은 LLM에 보내지 않는다.
2. 질의 분석은 L1에서 끝낸다. JSON schema를 두 번 위반하면 더 큰 모델로 자동 승격하지 않고 deterministic fallback을 사용한다.
3. 구조·표 uncertainty가 임계치 이상인 항목만 L2에 전달한다. L2도 확신하지 못하면 사람이 검수한다.
4. L3는 승인된 context를 받은 최종 답변에서만 사용한다.
5. Citation 검증은 L2의 semantic 판단을 참고할 수 있지만, 최종 통과 여부는 D0의 ID·locator·hash 일치 검사가 결정한다.
6. 어떤 모델도 자신보다 큰 모델을 직접 호출할 수 없다. 모든 라우팅은 Orchestrator 정책이 결정한다.

### 5.2 공통 작업 봉투와 결과 계약

Orchestrator가 각 에이전트에 전달하는 `AgentTaskEnvelope`는 다음 필드로 고정한다.

```json
{
  "workflow_id": "wf_...",
  "run_id": "run_...",
  "pipeline_id": "regulation_preprocessing_v1",
  "stage_id": "parse_extract",
  "agent_id": "ocr_extractor",
  "tenant_scope_hash": "sha256:...",
  "input_artifact_refs": ["artifact:..."],
  "input_content_hashes": ["sha256:..."],
  "model_profile": "paddleocr-korean-v5",
  "deadline_ms": 60000,
  "attempt": 1,
  "idempotency_key": "..."
}
```

봉투에는 원문 text, 로컬 경로, 비밀값을 직접 넣지 않는다. 에이전트는 tenant가 검증된 artifact reference만 읽는다.

모든 에이전트는 같은 `AgentResult` 형태를 반환한다.

```json
{
  "status": "completed",
  "output_artifact_refs": ["artifact:..."],
  "output_content_hashes": ["sha256:..."],
  "evidence_ids": [],
  "confidence": 1.0,
  "review_flags": [],
  "reason_code": null,
  "metrics": {
    "duration_ms": 0,
    "input_units": 0,
    "output_units": 0
  }
}
```

허용 상태 전이는 다음과 같다.

```text
pending -> running -> completed
                  -> review_required -> human_approved -> completed
                                     -> human_rejected -> blocked
                  -> degraded
                  -> blocked
                  -> failed
```

- `completed`: 다음 단계가 output hash를 검증한 뒤 진행
- `review_required`: 자동 진행 금지, 사람 검수 queue로 이동
- `degraded`: 안전한 fallback 결과이며 운영은 가능하지만 release acceptance는 실패
- `blocked`: 보안·승인·근거·필수 모델 조건 불충족
- `failed`: 일시적 또는 코드 오류, 정책에 따라 제한 재시도

재시도 정책:

- 읽기 전용 D0 단계: 같은 idempotency key로 최대 2회
- L1/L2/L3 추론: timeout·일시 연결 오류만 최대 1회 재시도
- VectorDB write: 새 호출이 아니라 동일 idempotency key의 상태 조회 후 재개
- 승인 journal write: 자동 재시도 전 기존 event ID 확인
- schema 위반·근거 위반·ACL 위반: 재시도하지 않고 차단

### 5.3 에이전트별 역할·모델·권한

아래 표의 세부 이름은 이미지 계획을 설명하기 위한 서비스 별칭이다. 실제 API·UI·실행기가
사용하는 canonical role ID는 `app/agents/role_registry.py`에 고정되어 있다. 예를 들어
`workflow_orchestrator`는 `orchestrator`, `text_normalizer`는 `normalizer`,
`query_analysis_agent`는 `query_analyst`, `grounded_answer_agent`는 `grounded_answerer`,
`claim_audit_agent`는 `claim_auditor`로 정규화된다. 파서·표 추출·출력 보안처럼 기존
서비스 안의 하위 작업은 독립 모델 역할로 가장하지 않고 `parser_extractor`,
`table_reviewer`, `security_guard`의 입력·출력 계약 안에서 추적한다.

| 에이전트 ID | 이미지 단계 | 모델 | 입력 | 출력 | 허용 권한 | 실패 시 전환 |
|---|---|---|---|---|---|---|
| `workflow_orchestrator` | 전체 | D0 | workflow state, stage result refs | 다음 task, durable trace | 상태 전환만 | 현재 stage 실패 기록 후 중단 |
| `intake_guard` | 1 업로드 | D0 | upload stream, auth scope | admission decision, source hash | 격리 저장 | 거부 또는 quarantine |
| `parser_dispatcher` | 2 파싱 | D0 | file signature, artifact ref | parser route | parser 선택 | unsupported/blocked |
| `native_text_extractor` | 2 파싱 | D0 parser | PDF/DOCX/HWP/HWPX | pages, blocks, tables | parsed artifact 생성 | OCR 후보 또는 failed |
| `ocr_extractor` | 2 파싱 | S1 PP-OCRv5 | image-only page refs | OCR blocks, bbox, confidence | OCR artifact 생성 | review_required/blocked |
| `table_extractor` | 2 파싱 | Kordoc/구조 parser | page/table refs | cell rows, geometry, header | table artifact 생성 | review_required |
| `text_normalizer` | 3 정규화 | D0 | parsed blocks | normalized blocks, provenance map | 파생 artifact 생성 | failed |
| `structure_detector` | 4 구조 인식 | D0 | normalized blocks | hierarchy nodes, confidence | node artifact 생성 | uncertainty route |
| `structure_review_agent` | 4·6 | L2 Qwen3 4B | uncertain nodes와 최소 주변 문맥 | findings, suggested label, evidence refs | findings만 생성 | human review |
| `chunk_builder` | 5 Chunk | D0 | hierarchy nodes, provenance | article chunks | chunk draft 생성 | failed |
| `quality_gate` | 6 품질 | D0 | parsed/normalized/nodes/chunks | quality report, worklist | 상태를 review로 전환 | blocked/review_required |
| `human_approval_gate` | 6과 8 사이 | 사람 | worklist와 source evidence | append-only decision | 승인·반려 journal | 승인 전 index 금지 |
| `export_agent` | 7 Export | D0 | approved/draft artifacts | JSONL/CSV/Markdown/table files | export 디렉터리 write | failed |
| `semantic_embedder` | 8 VectorDB | S2-E Qwen3 Embedding 0.6B | approved retrieval text | 1024-d vector, model metadata | vector 생성만 | degraded/blocked |
| `index_integrity_guard` | 8 VectorDB | D0 | approval journal, vector records | upsert decision | index write 승인 | blocked |
| `vector_index_writer` | 8 VectorDB | D0 | verified records | atomic index, manifest | local VectorDB write | 이전 index 보존 |
| `query_input_guard` | QA 입력 | D0 | user query, auth scope | bounded query | 길이·정책 검증 | request rejected |
| `query_analysis_agent` | QA 1 | L1 Qwen3 1.7B | bounded query | intent/locator/filter JSON | query plan 생성 | deterministic fallback |
| `query_rewrite_agent` | QA 2 | L1 Qwen3 1.7B | query plan, alias dictionary | search queries | query variants 생성 | 원 질문+규칙 확장 |
| `query_embedder` | QA 3 | S2-E Qwen3 Embedding 0.6B | semantic query | query vector | vector 생성 | hybrid degraded |
| `retrieval_guard` | QA 3 | D0 | auth scope, query variants/vector | ACL-filtered BM25/vector candidates | index read | no evidence |
| `reranker_agent` | QA 4 | S2-R Qwen3 Reranker 0.6B | query와 상위 50 candidate | relevance scores, top 10 | score 생성 | deterministic rerank |
| `metadata_filter_guard` | QA 4 | D0 | reranked candidates | approved current evidence | 후보 제거 | no evidence |
| `context_builder` | QA 5 | D0 | top evidence | deduped bounded context | context artifact 생성 | abstain |
| `grounded_answer_agent` | QA 6 | L3 Qwen3 8B | query와 bounded context | draft answer, cited evidence IDs | draft 생성 | extractive degraded/503 |
| `claim_audit_agent` | QA 7 | L2 Qwen3 4B | draft claims와 cited snippets | supported/unsupported findings | findings만 생성 | citation block |
| `citation_verifier` | QA 7 | D0 | draft, findings, evidence metadata | verified answer와 citations | 문장 제거·전체 차단 | abstain |
| `output_security_filter` | QA 출력 | D0 | verified answer | redacted API response | 민감정보 제거 | response blocked |

LLM은 승인, tenant 판정, 보안등급 변경, 원문 수정, VectorDB 쓰기 권한을 갖지 않는다. 모델 출력은 언제나 제안 또는 draft이며 deterministic gate 또는 사람이 최종 권한을 가진다.

### 5.4 전처리 파이프라인 오케스트레이션 순서

```text
Upload
  -> intake_guard[D0]
  -> parser_dispatcher[D0]
       -> native_text_extractor[D0]
       -> (image-only/low coverage) ocr_extractor[PP-OCRv5]
       -> table_extractor[Kordoc]
  -> text_normalizer[D0]
  -> structure_detector[D0]
       -> (uncertain only) structure_review_agent[Qwen3 4B]
  -> chunk_builder[D0]
  -> quality_gate[D0]
       -> review_required -> human_approval_gate
  -> export_agent[D0]
  -> semantic_embedder[Qwen3 Embedding 0.6B]
  -> index_integrity_guard[D0]
  -> vector_index_writer[D0]
```

단계별 hand-off 조건:

1. `intake_guard`가 tenant와 signature를 확정하기 전 parser를 실행하지 않는다.
2. native parse coverage가 기준 미만일 때만 OCR을 호출한다.
3. OCR 결과는 원문 대체가 아니라 별도 provenance를 가진 block으로 병합한다.
4. structure reviewer의 제안은 source span이 있어야 반영 후보가 된다.
5. quality gate의 blocking flag가 하나라도 있으면 export는 draft로만 허용하고 VectorDB는 금지한다.
6. 사람 승인 journal, approved content hash, 현재 Chunk hash가 모두 일치해야 embedding을 실행한다.
7. embedding model ID와 dimension이 index manifest와 다르면 같은 collection에 upsert하지 않는다.

### 5.5 질의응답 파이프라인 오케스트레이션 순서

```text
User Query
  -> query_input_guard[D0]
  -> query_analysis_agent[Qwen3 1.7B]
  -> query_rewrite_agent[Qwen3 1.7B]
  -> query_embedder[Qwen3 Embedding 0.6B]
  -> retrieval_guard[D0: BM25 + Vector]
  -> reranker_agent[Qwen3 Reranker 0.6B]
  -> metadata_filter_guard[D0]
  -> context_builder[D0]
  -> grounded_answer_agent[Qwen3 8B]
  -> claim_audit_agent[Qwen3 4B]
  -> citation_verifier[D0]
  -> output_security_filter[D0]
  -> Answer + Regulation/Chapter/Article/Paragraph Evidence
```

단계별 hand-off 조건:

1. query agent가 생성한 날짜·tenant·보안 필터는 요청자의 권한을 넓힐 수 없다.
2. ACL filter는 candidate generation 전과 rerank 후 두 번 적용한다.
3. BM25와 vector 결과는 서로 독립적으로 생성된 뒤 fusion한다.
4. reranker는 후보를 재정렬할 수 있지만 승인·tenant filter를 되돌릴 수 없다.
5. context builder는 evidence ID, 규정명, 조·항·호, source page, effective date를 함께 넣는다.
6. Qwen3 8B에는 최대 context budget을 넘긴 원문이나 검색되지 않은 문서를 전달하지 않는다.
7. Qwen draft의 각 핵심 주장에는 evidence ID가 있어야 한다.
8. Qwen3 4B semantic audit와 D0 exact verifier 중 하나라도 실패하면 해당 문장을 제거한다.
9. 남은 핵심 주장이 없으면 “승인된 규정 근거에서 확인할 수 없습니다”로 abstain한다.

### 5.6 모델 실행과 자원 스케줄링

모든 모델을 동시에 GPU에 상주시킬 필요는 없다. Orchestrator가 model pool lease를 관리한다.

| 자원 프로필 | 실행 전략 |
|---|---|
| CPU only | OCR·Embedding·Reranker는 CPU, 1.7B/4B/8B는 순차 Ollama 실행; 기능 검증 가능하나 응답시간은 별도 표기 |
| VRAM 8GB | 8B 양자화 모델을 답변 시점에 단독 적재; 0.6B embedding/reranker는 CPU 또는 답변 전에 GPU lease 반납 |
| VRAM 12~16GB | 0.6B embedding/reranker 상주 가능, 8B 답변 모델 우선; 4B reviewer는 batch 시점에만 적재 |
| VRAM 24GB+ | 모델별 worker 분리 가능하지만 tenant·artifact 보안 경계는 동일하게 유지 |

운영 제한:

- 한 workflow가 모델 lease를 무기한 점유하지 않도록 deadline을 둔다.
- 전처리 batch의 4B review와 실시간 8B QA가 충돌하면 사용자 QA가 우선이다.
- 8B 답변 중 OCR batch는 CPU queue로 이동하거나 일시 정지한다.
- 모델 warm/cold latency, peak RAM/VRAM, tokens/sec를 모델별로 기록한다.
- model download는 운영 중 자동 수행하지 않는다. 설치·hash·license 확인 후 명시적으로 등록한다.

### 5.7 모델별 합격 기준

| 모델/서비스 | 합격 기준 |
|---|---|
| PP-OCRv5 Korean | OCR fixture character accuracy와 page coverage 기록, 저신뢰 block 자동 검수 전환 |
| Qwen3 Embedding 0.6B | 한국어 규정 질의 Recall@5가 hash fallback보다 개선되고 dimension·normalization이 고정됨 |
| Qwen3 Reranker 0.6B | 후보 50개에서 정답 조문 MRR/Top-3가 fusion-only보다 저하되지 않음 |
| Qwen3 1.7B | query JSON schema 99% 이상, locator 보존 100%, 권한 필터 생성·확장 금지 |
| Qwen3 4B | 구조·인용 fixture에서 unsupported 항목을 통과시키지 않으며 판단 근거 evidence ID 필수 |
| Qwen3 8B | 근거 없음 abstention 100%, citation precision 100%, 외부 지식으로 규정 생성 0건 |

모델 평가를 통과하지 못하면 해당 모델만 교체할 수 있도록 agent contract와 model adapter를 분리한다. 모델 교체가 승인·보안·storage 코드를 변경하게 만들지 않는다.

### 5.8 구현 파일 배치와 테스트 소유권

기존 파일을 우선 확장하고 같은 책임의 중복 모듈을 만들지 않는다.

| 책임 | 우선 수정 파일 | 필요할 때만 추가할 파일 | 집중 테스트 |
|---|---|---|---|
| 공통 task/result 계약 | `app/agents/orchestrator.py`, `app/agents/base.py` | `app/agents/contracts.py` | `tests/test_agent_orchestrator.py`, schema/transition test |
| 모델 라우팅·lease | `app/agents/role_registry.py`, `app/core/config.py` | `app/agents/model_router.py` | model permission, localhost, timeout test |
| 파싱·OCR | `app/parsers/pdf_parser.py`, parser factory | `app/parsers/ocr_adapter.py` | PDF/image/table fixture test |
| 추출 품질 | `app/parsers/extraction_quality.py`, `app/services/processing_service.py` | 없음 | `tests/test_extraction_quality.py`, processing regression |
| 구조·Chunk·품질 | 기존 processors와 quality gate | review adapter가 필요할 때만 `app/agents/structure_review_agent.py` | structure/chunker/quality fixture |
| 승인·VectorDB | `app/api/routes_documents.py`, ingestion adapters | `app/ingestion/semantic_embedding_adapter.py` | approval, tenant, vector integrity test |
| 질문 분석·보정 | `app/api/routes_rag.py`, searcher | `app/rag/query_analysis.py` | query schema, locator, fallback test |
| semantic rerank | `app/retrieval/searcher.py` | `app/retrieval/local_reranker.py` | relevance, ACL-before/after test |
| Context | `app/api/routes_rag.py` | `app/rag/context_builder.py` | dedupe, token budget, injection test |
| 답변·인용 | `app/agents/grounded_answer_agent.py`, `app/agents/citation_verifier.py` | `app/agents/claim_audit_agent.py` | hallucination, abstention, citation test |
| 운영 화면 | `frontend/streamlit_app.py` | 없음 | Streamlit source/contract test |
| 공개 계약 | `app/api/routes_pipelines.py`, docs | 없음 | route auth, path redaction test |

모든 새 public 함수에는 type hint를 사용하고, API·agent payload는 Pydantic 모델로 검증한다. 전처리 로직을 변경하는 각 작업은 focused regression test와 preprocessing change governance 증거를 함께 남긴다.

## 6. 우선순위와 시간 초과 규칙

6시간 안에 예상보다 오래 걸리는 작업이 발생하면 다음 순서로 보호한다.

1. 승인·tenant·보안·인용 fail-closed
2. 전처리 8단계 E2E와 QA 7단계 E2E
3. Qwen3 8B 실제 smoke
4. retrieval 품질과 context 최적화
5. UI 장식과 비핵심 문서 정리

시간이 부족해도 아래 항목은 생략하지 않는다.

- 미승인 Chunk 차단
- cross-tenant 검색 차단
- 근거 없는 답변 거절
- 인용 검증
- 전체 회귀 테스트
- 실패·미검증 항목의 명시적 보고

## 7. 6시간 종료 시 사용자에게 보고할 내용

1. 요청 6시간 대비 실제 경과 시간
2. 이미지 15단계별 `완료/부분/차단` 상태
3. 실행한 명령과 통과·실패 결과
4. 생성·수정한 파일
5. Qwen3 8B 실제 연결·응답 여부
6. parser·retrieval·citation 정량 결과
7. 출시를 막는 위험과 다음 작업

“코드가 존재한다”와 “제품이 검증되었다”를 구분한다. 실제 모델이나 기관 문서가 없는 항목은 추정으로 통과시키지 않고 외부 검증 대기로 남긴다.

## 8. 모델 선정 근거 자료

- [Qwen3 8B 공식 모델 카드](https://huggingface.co/Qwen/Qwen3-8B)
- [Qwen3 4B 공식 모델 카드](https://huggingface.co/Qwen/Qwen3-4B)
- [Qwen3 1.7B 공식 모델 카드](https://huggingface.co/Qwen/Qwen3-1.7B)
- [Qwen3 Embedding 0.6B 공식 모델 카드](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [Qwen3 Reranker 0.6B 공식 모델 카드](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)
- [Ollama Qwen3 공식 라이브러리](https://ollama.com/library/qwen3)
- [PaddleOCR 한국어 PP-OCRv5 공식 문서](https://www.paddleocr.ai/main/en/version3.x/module_usage/text_recognition.html)
