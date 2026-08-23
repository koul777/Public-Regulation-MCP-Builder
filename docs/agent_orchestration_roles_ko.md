# 공공기관 규정 로컬 AI 에이전트 역할 체계

## 1. 목적

Qwen3 계열 기반 로컬 규정 AI를 만들 때 여러 모델 호출을 자유롭게 연결하지 않고, 역할·권한·입출력·실패 정책이 고정된 오케스트레이션으로 운영한다. 최종 답변은 Qwen3 8B가 맡고, 더 단순하거나 전문화된 작업은 더 작거나 목적 전용인 모델에 배정한다.

이 시스템에서 “에이전트”라는 이름을 쓰더라도 모두 LLM인 것은 아니다.

- 결정적 서비스: 파싱, 보안, 승인 확인, 색인, 검색, citation 검증
- 제한된 생성 LLM 역할: Qwen3 1.7B 질의 분석·재작성, Qwen3 4B 구조·표·주장 검수, Qwen3 8B 근거 답변
- 전문 모델 역할: PaddleOCR 한국어 인식, Qwen3 Embedding 0.6B 의미 벡터, Qwen3 Reranker 0.6B 재순위
- 사람 역할: 최종 승인
- 운영 역할: 릴리스·MCP 검증

AI는 제안과 답변을 만들 수 있지만, 승인·권한·색인 공개·릴리스 결정을 독점하지 않는다.

## 2. 역할 분류

| 수준 | 역할 | 실행 모델/방식 | 데이터 변경 권한 |
|---|---|---|---|
| D0 | `orchestrator`, `security_guard`, `intake_guard` | 결정적 Python | 허용된 상태·감사 기록만 |
| S1 | `ocr_extractor` | `korean_PP-OCRv5_mobile_rec` | OCR artifact·confidence만 |
| D0 | `parser_extractor`, `normalizer`, `structure_detector`, `chunk_builder` | 결정적 parser/processor | 단계별 artifact·trace만 |
| L2 | `structure_reviewer`, `table_reviewer` | `qwen3:4b` | review finding만 |
| D0/H | `quality_gate`, `human_approval_gate`, `exporter` | 결정적 gate/사람 | 품질 결과·승인 journal·export |
| S2-E | `semantic_embedder` | `Qwen/Qwen3-Embedding-0.6B` | 승인된 임시 vector만 |
| D0 | `index_builder` | 결정적 writer | 승인 색인만 |
| L1 | `query_analyst`, `query_rewriter` | `qwen3:1.7b` | 검색 plan·trace만 |
| D0 | `retrieval_guard` | BM25 + vector fusion | ACL 적용 retrieval trace만 |
| S2-R | `reranker` | `Qwen/Qwen3-Reranker-0.6B` | 후보 순서·score만 |
| D0 | `context_builder` | 결정적 budget builder | context trace만 |
| L3 | `grounded_answerer` | `qwen3:8b` | answer draft·model trace만 |
| L2 | `claim_auditor` | `qwen3:4b` | claim finding만 |
| D0 | `citation_verifier` | exact evidence verifier | 검증된 answer trace만 |
| D0 | `evaluation_agent`, `release_operator` | 결정적 harness | 평가·릴리스 evidence만 |

## 3. 핵심 권한 원칙

### AI 역할이 할 수 없는 일

- Chunk를 자동 승인하기
- 승인 journal 없이 색인 공개하기
- tenant·기관·부서 범위를 변경하기
- 검색되지 않은 사실을 답변에 추가하기
- citation을 임의로 만들어내기
- 외부 API로 기관 문서를 전송하기
- 릴리스 gate를 우회하기

### 오케스트레이터가 할 수 없는 일

- 직접 조문을 판단해 답변하기
- 품질 차단을 무시하고 다음 단계로 진행하기
- 사람 승인 없이 `index_builder` 호출하기
- 실패한 결과를 성공으로 변환하기

오케스트레이터는 “무엇을 답할지”가 아니라 “현재 상태에서 어떤 역할을 호출할 수 있는지”만 결정한다.

## 4. 워크플로우 A — 문서 등록·승인·색인

```text
orchestrator
  → security_guard
  → intake_guard
  → parser_extractor
  → (저추출 페이지만) ocr_extractor [PaddleOCR]
  → normalizer → structure_detector
  → (불확실 후보만) structure_reviewer [Qwen3 4B]
  → (표 후보만) table_reviewer [Qwen3 4B]
  → chunk_builder
  → quality_gate
  → human_approval_gate
  → exporter
  → semantic_embedder [Qwen3 Embedding 0.6B]
  → index_builder
  → evaluation_agent
```

### 단계별 책임

1. `security_guard`: 인증·tenant·파일 정책을 먼저 확인한다.
2. `intake_guard`: 파일을 접수하고 source hash와 document id를 만든다.
3. 기존 parser/processor: 문서를 구조화하고 Chunk를 만든다.
4. `structure_reviewer`: 불확실한 구조를 finding으로 제안한다.
5. `table_reviewer`: 표·별표·별지의 위험을 finding으로 제안한다.
6. `quality_gate`: 차단 여부와 검수 worklist를 만든다.
7. `human_approval_gate`: 운영자가 원문과 비교해 최종 결정한다.
8. `index_builder`: 승인 journal과 hash가 맞을 때만 색인한다.
9. `evaluation_agent`: 색인과 검색 회귀를 검사한다.

### 중단 조건

- 보안 scope 없음
- parser 결과가 비어 있음
- 구조·표 provenance 없음
- 품질 blocker 미해결
- 사람 승인 없음
- approval journal과 Chunk hash 불일치
- 색인 manifest 검증 실패

## 5. 워크플로우 B — 로컬 규정 질의응답

```text
orchestrator
  → security_guard (입력)
  → query_analyst [Qwen3 1.7B]
  → query_rewriter [Qwen3 1.7B]
  → retrieval_guard [BM25 + Qwen3 Embedding 0.6B]
  → reranker [Qwen3 Reranker 0.6B]
  → context_builder
  → grounded_answerer (Qwen3 8B)
  → claim_auditor [Qwen3 4B]
  → citation_verifier
  → security_guard (출력)
```

### 단계별 책임

1. 입력 보안 검사가 끝나기 전에는 질의 분석을 시작하지 않는다.
2. `query_analyst`는 Qwen3 1.7B의 구조화 JSON으로 규정명·조문·기간·기준일·질의 의도를 추출한다. schema 실패 시 규칙 기반 분석으로 복귀한다.
3. `query_rewriter`는 Qwen3 1.7B로 locator를 보존한 검색 변형만 만든다. tenant·ACL 조건은 생성할 수도 바꿀 수도 없다.
4. `retrieval_guard`는 승인·tenant·기관·보안등급·최신본 필터를 검색보다 먼저 적용하고 BM25와 Qwen3 Embedding 결과를 결합한다.
5. `reranker`는 ACL 통과 후보 안에서만 Qwen3 Reranker 0.6B 점수로 순서를 바꾼다.
6. `context_builder`는 내부 chunk ID를 prompt에서 숨기고 `E1`, `E2` 형식의 bounded Context만 만든다.
7. `grounded_answerer`는 Qwen3 8B로 각 주장에 허용된 evidence ID를 붙인 답변 초안을 만든다.
8. `claim_auditor`는 Qwen3 4B로 각 주장과 인용 snippet의 의미 지지를 검사한다.
9. `citation_verifier`는 원본 승인 hash와 exact quote를 다시 대조하며, 하나라도 실패하면 문장을 제거하거나 abstain한다.
10. 출력 `security_guard`는 내부 경로·secret·운영 metadata를 제거한다.

### 답변 실패 처리

| 상황 | 처리 |
|---|---|
| 검색 결과 없음 | 확인 불가 답변, LLM 호출 생략 |
| Qwen3 8B timeout | extractive fallback 또는 unavailable |
| 모델이 citation을 생성 | citation 폐기 후 시스템 citation 재생성 |
| evidence와 주장 불일치 | 답변 제한·abstain |
| tenant scope 오류 | 검색·답변 모두 거부 |
| 출력 필터 오류 | 답변 외부 반환 금지 |

## 6. 워크플로우 C — 릴리스·MCP handoff

```text
orchestrator
  → evaluation_agent
  → release_operator(Hermes)
```

`HermesAgent`는 새 답변을 만드는 역할이 아니라 테스트·빌드·MCP smoke·공개 릴리스 증적을 조정하는 운영 역할로 유지한다.

MCP는 별도 지식베이스를 만들지 않는다. 로컬 QA가 사용하는 승인 RAG와 같은 `retrieval_guard` 정책을 사용한다.

## 7. 난이도별 모델 라우팅

| 등급 | 배치 | 선택 이유 | 최대 권한 |
|---|---|---|---|
| L1 | Qwen3 1.7B | 짧고 형식이 고정된 질의 분석·재작성에 충분하며 지연과 메모리를 줄임 | query plan 생성 |
| L2 | Qwen3 4B | 구조 경계와 주장-근거처럼 문맥 판단이 필요하지만 최종 문장 생성은 아닌 검수 | finding 생성 |
| L3 | Qwen3 8B | 여러 근거를 종합해 자연스러운 한국어 최종 답변을 작성 | 답변 초안 생성 |
| S1 | PaddleOCR Korean PP-OCRv5 | 이미지 문자 인식에 특화 | OCR block 생성 |
| S2-E | Qwen3 Embedding 0.6B | 다국어 의미 검색 전용 | vector 생성 |
| S2-R | Qwen3 Reranker 0.6B | query-passage 관련도 판별 전용 | 후보 재정렬 |
| D0 | 결정적 Python | 승인·보안·권한·인용처럼 재현성과 fail-closed가 필요한 결정 | 제한된 상태 변경 |

### 생성 모델을 사용하지 않는 역할

- `security_guard`
- `quality_gate`
- `human_approval_gate`
- `index_builder`
- `retrieval_guard`
- `context_builder`
- `citation_verifier`
- `release_operator`

이 분리는 모델이 틀려도 승인·보안·색인 공개가 자동으로 오염되지 않게 하기 위한 것이다.

## 8. 역할 호출 계약

각 호출은 다음 envelope를 사용한다.

```json
{
  "workflow_id": "qa-2026-0001",
  "role_id": "grounded_answerer",
  "tenant_scope_hash": "sha256:<opaque-scope-hash>",
  "profile_scope": "configured-profile",
  "input_schema_version": "agent-input-v1",
  "payload_reference": "trace-local-reference",
  "allowed_mutations": ["model_trace"],
  "approval_scope": "approved-current-only",
  "model_profile": {
    "backend": "ollama",
    "model": "qwen3:8b",
    "endpoint_host": "127.0.0.1"
  },
  "trace_id": "trace-123"
}
```

모델 prompt에 원문 파일 경로·비공개 운영 메모·secret을 넣지 않는다. 실제 payload는 reference로 관리하고, 역할별로 필요한 최소 field만 전달한다.

## 9. 현재 코드와의 연결

| 현재 코드 | 새 역할 |
|---|---|
| `HermesAgent` | `release_operator` |
| `MetadataAgent` | parser/metadata 전문 서비스 |
| `PaddleKoreanOcrAdapter` | `ocr_extractor`의 로컬 PaddleOCR 실행기 |
| `LocalStructureReviewAgent` | Qwen3 4B `structure_reviewer` |
| `StructureReviewAgent`, `TableNarrationAgent` | 검수 역할의 deterministic fallback |
| `AgentReviewPolicy` | LLM review 후보·예산 정책 |
| `AgentReviewExecutor` | 제한된 review role의 provider 실행기 |
| `execution_guard.py` | review 실행 사전 보안 gate |
| `Qwen3EmbeddingAdapter`, `Qwen3RerankerAdapter` | semantic·reranker 전용 실행기 |
| `QueryAnalysisAgent`, `QueryRewriteAgent` | Qwen3 1.7B 질의 역할 |
| `GroundedQwenAnswerAgent` | Qwen3 8B 답변 역할 |
| `ClaimAuditAgent`, `CitationVerifier` | Qwen3 4B 감사 + 결정적 인용 검증 |
| `local_llm.py`, `ollama_runtime.py` | localhost 전용 Qwen runtime facade |
| `output_filter.py` | 출력 `security_guard` 보조 |

`role_registry.py`와 `model_router.py`가 역할·모델의 단일 기준이며 `/api/pipelines/manifest`가 이를 경로 정보 없이 운영 UI에 공개한다.
인증된 운영자는 `POST /api/pipelines/orchestration/plan`에 workflow와 완료된 역할의
정확한 prefix를 보내 다음 역할·모델·실패 정책을 조회할 수 있다. 이 endpoint는 계획만
반환하며 모델 호출, 승인, 색인, 파일 쓰기를 하지 않는다.
계획 응답에는 원시 `tenant_id`·기관 `profile_id`를 넣지 않고 `tenant_scoped`와
`profile_scoped` 여부만 남긴다. 따라서 운영자가 다음 역할을 확인해도 기관 식별자가
계획 trace나 공개 API 응답으로 새어 나가지 않는다.

계획 endpoint의 `mode`는 `plan`만 허용한다. 실제 한 역할 실행은 저장 가능한 executor 상태와
artifact 참조를 가진 내부 `advance()` 경계에서만 수행한다.

## 10. 실제 실행·복구 순서

1. `role_registry.py`와 `model_router.py`를 검증하고 등록되지 않은 역할·외부 endpoint를 거부한다.
2. tenant·권한·승인 scope를 확정한 뒤에만 workflow를 시작한다.
3. 각 역할은 Pydantic 계약으로 입력과 출력을 검증하며, 원문 경로 대신 제한된 payload만 받는다.
4. 결정적 단계 실패는 즉시 중단하고 마지막 정상 checkpoint를 남긴다.
5. Qwen3 1.7B/4B의 schema·timeout 실패는 deterministic fallback 또는 사람 검수로 내린다.
6. Embedding/Reranker 장애는 degraded trace를 남기고 BM25 경로를 사용할 수 있지만, semantic 필수 릴리스 판정은 실패시킨다.
7. Qwen3 8B 답변 실패는 근거 발췌형 답변 또는 명확한 unavailable로 내린다.
8. Qwen3 4B 주장 감사와 결정적 citation 검증을 모두 통과한 문장만 반환한다.
9. 승인·색인 write는 idempotency key, approval hash, tenant scope를 재확인한다.
10. 실제 모델 수용시험과 전체 회귀·build·hygiene 증적을 함께 릴리스 판정에 사용한다.

## 11. 1차 완료 조건

- 모든 workflow가 등록된 role id와 고정 모델 profile만 호출한다.
- `human_approval_gate` 이전에는 `index_builder`가 호출되지 않는다.
- `grounded_answerer` 이전에는 `context_builder`가 만든 evidence만 전달된다.
- `citation_verifier` 실패 답변은 외부에 그대로 노출되지 않는다.
- Qwen3 1.7B/4B가 실패해도 권한·승인 상태는 변하지 않는다.
- Qwen3 8B가 없어도 extractive 경로가 동작하고 이를 degraded로 표시한다.
- 실제 로컬 수용시험에서 8개 전처리 + 7개 QA 단계가 모두 통과하고 외부 API 호출이 0건이어야 한다.
- 모든 역할 호출에 workflow·tenant·trace·입출력 schema version이 남는다.
- Hermes 릴리스 역할은 기존 MCP/public gate를 계속 사용한다.

## 12. 현재 실행기와 초보자 설명모드

역할 계획만 만들고 끝나지 않도록 `app/agents/executor.py`의
`advance_workflow()`가 한 번에 역할 하나를 실행하고 다음 작업을 준비한다.

- 현재 역할의 입력은 `artifact:` 참조와 SHA-256으로만 전달한다.
- 입력·출력 artifact 참조와 SHA-256 hash는 항상 1:1로 묶어야 하며, 한쪽만 있으면 거부한다.
- 역할 결과가 성공하면 출력 artifact 참조와 hash를 다음 역할에 넘긴다.
- `run_id`는 경로가 될 수 없는 불투명 식별자만 허용한다.
- `failed`, `blocked`, `review_required` 상태에서는 다음 역할을 자동 실행하지 않는다.
- `human_approval_gate`는 `artifact:` 형식의 사람 결정 참조가 있어야만 재개된다.
- 상태는 매 단계 저장할 수 있으므로 중단 뒤 마지막 정상 checkpoint에서 재개할 수 있다.

`app/pipelines/definitions.py`의 manifest는 각 화면 단계에 연결된 `agent_role_ids`,
담당 역할 설명, 입력·출력, 변경 가능한 범위, 금지된 행동, 모델 profile, 실패 정책,
사람 결정 필요 여부를 함께 제공한다.
초보자 모드의 `전체 과정과 담당 모델을 한눈에 보기` 패널은 이 manifest를 그대로 읽는다.
따라서 다음 화면에서 무엇을 눌러야 하는지와 그 뒤 어떤 역할·모델이 실행되는지를 한 번에
확인할 수 있다. 설명 패널은 초보자 모드에서 펼쳐진 상태로 시작하고, 실제 실행 trace에는
각 단계의 목적과 역할별 다음 행동도 표시한다. 실제 QA 답변이 다중 모델 경로로 실행되면
응답에도 역할별 실행 trace가 포함되며, 내부 경로·원문·tenant 식별자는 포함하지 않는다.
각 단계에는 `받는 것`과 `만드는 것`도 함께 표시한다. 예를 들어 전처리의 구조 인식은
정리된 문서를 받아 규정·장·절·조·항·호 구조를 만들고, QA의 컨텍스트 구성은 검색된
근거 조문을 받아 답변 모델에 전달할 근거 본문과 인용 정보를 만든다. 화면에는 이 값을
초보자가 읽을 수 있는 한국어 이름으로 보여 주며, 내부 키·파일 경로·원문 내용은 노출하지 않는다.

문서 처리와 질의·답변이 끝난 뒤의 MCP 연결 준비는 별도 `release_and_mcp_handoff`
흐름으로 설명한다. 이 흐름은 `evaluation_agent`가 테스트·품질 evidence를 확인한 뒤
`release_operator`가 공개 릴리스와 MCP handoff 조건을 점검한다. 필수 gate가 실패하면
배포하지 않고 복구·재검증 조치만 안내한다.

## 13. 문서 처리에서 실제 역할 상태를 읽는 법

전처리 실행 기록의 `stats.pipeline_trace.stages[*]`에는 정적 역할 목록과 별도로
`agent_role_statuses`가 남는다. 이 목록은 다음 상태를 사용한다.

- `completed`: 해당 역할이 정상적으로 끝났다.
- `skipped`: 조건상 실행할 필요가 없었다. 예를 들어 OCR 후보가 없는 문서다.
- `degraded`: 로컬 모델을 사용할 수 없어 제한된 경로로 진행했다.
- `review_required`: 결과를 자동으로 넘기지 않고 사람 검토를 기다린다.
- `pending`: 아직 사람이 승인하거나 다음 외부 단계를 실행하지 않았다.
- `blocked` / `failed`: 오류·보안·입력 문제로 이후 역할을 진행하지 않는다.

초보자 화면은 `pipeline_manifest`의 전체 설계도와 현재 문서의
`agent_role_statuses` 실행 기록을 분리해 보여 준다. 따라서 “담당 모델이 무엇인지”와
“이번 문서에서 실제로 실행됐는지”를 혼동하지 않는다. 상태 기록에는 역할명, 모델명,
상태, 제한된 사유 코드만 저장하며 원문·파일 경로·secret은 저장하지 않는다. 단계가
실패하거나 차단되면 그 안에서 실행 중이던 역할도 같은 실패 상태로 닫히므로, 화면에
실행 중으로 남는 오해를 만들지 않는다.

## 14. 실제 로컬 모델 검증 상태

현재 source checkout에서 수행한 역할 검증 결과는 다음과 같다.

- `qwen3:1.7b`: 질의 분석·검색어 보정 실제 로컬 실행 통과
- `qwen3:4b`: 구조·표 검수, 주장 감사와 citation 보조 판정 실제 로컬 실행 통과
- `qwen3:8b`: 승인된 근거 Context 기반 답변 실제 로컬 실행 통과
- `Qwen3-Embedding-0.6B`: 1024차원 의미 검색과 관련도 분리 검증 통과
- `Qwen3-Reranker-0.6B`: 관련 passage 점수 분리 검증 통과
- `korean_PP-OCRv5_mobile_rec`: 한국어 fixture OCR과 필수 용어·confidence·bbox 검증 통과

위 결과는 저장소의 `.venv\Scripts\python.exe`와 로컬 모델 cache를 사용한 것이다.
기본 `python`이 다른 전역 환경을 가리키면 optional dependency가 없다는 오류가 날 수
있으므로, 운영 검증은 반드시 `.venv`의 Python으로 실행한다. 15단계 acceptance는
`.venv`에서 15/15 단계, 외부 API 호출 0건, 승인 actor 기록, 근거 marker 포함으로 통과했다.
모델 timeout이나 로컬 응답 형식 오류가 나도 `verify_local_model_roles.py`는 traceback만
남기지 않고 `passed: false`, 오류 종류, 제한된 오류 메시지를 JSON으로 저장한다.

따라서 코드 계약·역할 라우팅·보안 게이트·Qwen 생성 역할·임베딩·재순위·OCR이 모두
저장소 `.venv`의 실제 로컬 런타임에서 검증됐다. 15단계 acceptance도 15/15로 통과했으므로
현재 상태는 모델 수용 완료로 표시할 수 있다. 운영자는 전역 Python이 아니라 동일한 `.venv`와
로컬 cache를 사용해야 하며, 환경이 바뀌면 acceptance를 다시 실행한다.
### Executor resume invariant

When a role pauses with `review_required`, the executor preserves only its opaque output artifact references and SHA-256 content hashes. After an approved `artifact:` decision, the next role receives that exact reference/hash pair; raw source content and local paths are never copied into workflow state.
