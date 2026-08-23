# 공공기관 규정 로컬 AI 플랫폼 상세 실행 계획

이 문서는 다음 이미지의 흐름을 현재 저장소에서 실제로 구현하기 위한 상세 계획이다.

- 시리즈 1: 규정 전처리기
- 시리즈 2: 로컬 LLM 기반 지식답변 프로그램

## 1. 최종 제품 정의

최종 제품은 “MCP를 만드는 프로그램”이 아니라 다음을 수행하는 **공공기관 규정 로컬 AI 플랫폼**이다.

1차 로컬 LLM은 **Qwen3 8B**로 고정한다.

- Ollama 기본 모델명: `qwen3:8b`
- llama.cpp: Qwen3 8B GGUF 파일 또는 기관이 등록한 동일 계열 모델명
- 모델 미설치 환경의 실행 기본값: `extractive`
- 모델 자동 다운로드: 기본 비활성화

```text
규정 파일 등록
  → 파싱
  → 정규화
  → 장·조·항·호·목·별표·별지 구조 인식
  → 조문 단위 Chunk 생성
  → 품질검사
  → 사람 검수·승인
  → 승인된 최신본만 임베딩·색인
  → 로컬 규정 질문
  → 질문 분석·검색어 보정
  → 키워드 + Vector 하이브리드 검색
  → 재랭킹·보안 필터
  → Context 구성
  → 로컬 LLM 답변 생성
  → 답변 + 근거 조문 + 출처
```

MCP는 이 흐름의 본체가 아니다. 완성된 승인 RAG 엔진을 ChatGPT·Claude·Codex·기관 AI가 사용할 수 있게 하는 **선택적 연결 어댑터**다.

## 2. 현재 저장소 기준선

현재 저장소는 시리즈 1과 시리즈 2의 상당 부분을 이미 갖고 있다.

| 이미지 요소 | 현재 코드 | 현재 상태 | 발전 방향 |
|---|---|---|---|
| 문서 업로드 | `app/api/routes_documents.py`, `app/services/document_service.py` | 업로드·제한·테넌트 검사 | 업로드 manifest와 재시도 계약 강화 |
| PDF/HWP/HWPX/DOCX 파싱 | `app/parsers/*` | 형식별 parser 존재 | 페이지·표·원문 위치 provenance 통일 |
| 정규화 | `app/processors/normalizer.py`, `mojibake.py` | 인코딩·문자 정리 | 원문-정규화문 매핑 보존 |
| 구조 인식 | `structure_detector.py`, `metadata_extractor.py` | 조문·계층 metadata 생성 | 불확실성 점수와 검수 사유 표준화 |
| Chunk | `app/processors/chunker.py` | 규정 검색용 chunk 생성 | 조문 의미 단위·표 parentage 개선 |
| 품질검사 | `quality_gate.py`, `validator.py`, review services | 품질·승인 흐름이 강함 | QA 결과와 답변 차단 사유 연결 |
| Export | `app/processors/exporter.py`, `scripts/*export*` | JSON/CSV·Vector export | 공개 export와 내부 runtime export 분리 |
| 임베딩 | `app/ingestion/embedding_adapter.py` | `local-hash-embedding-v1` deterministic 방식 | 선택적 의미 임베딩 backend 추가 |
| Vector/RAG | `app/retrieval/*`, `app/services/regulation_rag_runtime.py` | BM25·구조 boost·vector 검색 | 검색 계약을 독립 서비스로 정리 |
| 로컬 LLM | `app/rag/local_llm.py` | Ollama·llama.cpp·호환 endpoint | backend protocol·모델 상태 관리 |
| 답변 API | `app/api/routes_rag.py` | `/api/rag/chat`와 extractive fallback | AnswerService로 분리 |
| 운영 UI | `frontend/streamlit_app.py` | 전처리·승인·MCP 중심 | 로컬 질문을 기본 완료 흐름으로 승격 |
| MCP | `app/mcp_server/*` | stdio/HTTP 연결 | 동일 RAG를 사용하는 외부 adapter로 유지 |

핵심 판단은 **새 프로젝트에서 다시 만들지 않고 이 저장소의 내부 경계를 재정리하는 것**이다.

## 3. 목표 아키텍처

```text
┌──────────────────────────────────────────────────────────────┐
│                    Streamlit Operator UI                     │
│  기관 선택 · 문서 등록 · 검수/승인 · 로컬 질문 · MCP 연결     │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                         Application API                       │
│ documents · review · approval · rag/search · rag/chat · mcp   │
└──────────┬──────────────────────┬──────────────────────┬──────┘
           │                      │                      │
┌──────────▼────────┐  ┌─────────▼──────────┐  ┌────────▼────────┐
│ Regulation         │  │ Approved RAG       │  │ Answer           │
│ Processing         │  │ Runtime            │  │ Service          │
│ parse/normalize/   │  │ scope/filter/      │  │ query/context/   │
│ structure/chunk/QC │  │ retrieve/rerank    │  │ llm/citation     │
└──────────┬────────┘  └─────────┬──────────┘  └────────┬────────┘
           │                      │                      │
┌──────────▼────────┐  ┌─────────▼──────────┐  ┌────────▼────────┐
│ Documents +        │  │ Approval journal + │  │ LLM backends    │
│ processing         │  │ approved vectors  │  │ extractive      │
│ artifacts          │  │ BM25/vector index │  │ Ollama          │
└────────────────────┘  └────────────────────┘  │ llama.cpp       │
                                                └────────┬────────┘
                                                         │
                                                ┌────────▼────────┐
                                                │ Citation builder │
                                                │ output filter    │
                                                │ audit trace      │
                                                └────────┬────────┘
                                                         │
                         ┌───────────────────────────────▼──────┐
                         │ Answer to operator / MCP client       │
                         └──────────────────────────────────────┘
```

## 4. 시리즈 1 — 규정 전처리기 상세 계획

### 4.1 1단계: 문서 업로드

이미지의 “문서 업로드” 박스를 다음 계약으로 고정한다.

#### 입력

- PDF
- HWP
- HWPX
- DOCX
- 기관 프로필
- 규정 식별자와 버전 정보
- 테넌트와 부서 범위
- 선택적 효력일·개정일·출처 URL

#### 처리 순서

1. 파일 확장자와 MIME 확인
2. 파일 크기·압축 해제 크기·archive entry 제한 확인
3. 악성/비정상 archive 구조 차단
4. 원본 SHA-256 계산
5. 같은 tenant·기관·규정 family·version 중복 검사
6. 원본을 `pending_uploads` 또는 격리된 runtime 경로에 저장
7. `document_id`, `tenant_id`, `profile_id`가 포함된 upload manifest 생성
8. 처리 job을 `uploaded` 상태로 등록

#### 수정/보강 대상

- `app/api/routes_documents.py`
- `app/core/input_limits.py`
- `app/parsers/archive_safety.py`
- `app/services/document_service.py`
- `app/storage/repository.py`

#### 반드시 남길 metadata

```json
{
  "document_id": "stable-id",
  "tenant_id": "tenant-a",
  "profile_id": "institution-a",
  "source_filename": "sanitized-name.pdf",
  "source_sha256": "...",
  "media_type": "application/pdf",
  "regulation_id": "regulation-family-id",
  "regulation_version": "3.2",
  "uploaded_at": "2026-08-22T00:00:00Z",
  "state": "uploaded"
}
```

#### 완료 기준

- 다른 tenant 문서와 같은 경로·manifest를 공유하지 않는다.
- 동일 버전 중복 업로드가 차단된다.
- 업로드 실패가 원본 파일 전체를 노출하지 않고 상태·이유로 기록된다.
- 원본 파일 경로는 외부 API와 MCP 응답에 절대 포함되지 않는다.

#### 테스트

- `tests/test_api_upload_admission.py`
- `tests/test_api_input_limits.py`
- `tests/test_api_tenant_isolation.py`
- archive bomb·잘못된 확장자·중복 version·빈 파일 회귀 테스트

### 4.2 2단계: 파싱

#### 목표

PDF/HWP/HWPX/DOCX를 “검색 가능한 문자열”이 아니라 **페이지·표·문단 위치를 가진 ParsedDocument**로 바꾼다.

#### 표준 출력

```text
ParsedDocument
├── document_id
├── parser_name
├── parser_version
├── pages[]
│   ├── page_no
│   ├── blocks[]
│   │   ├── block_type: paragraph/table/image/header/footer
│   │   ├── raw_text
│   │   ├── bbox (가능한 경우)
│   │   └── table_id (가능한 경우)
│   └── warnings[]
└── parser_quality
```

#### 처리 원칙

- 파싱된 텍스트가 비어 있으면 성공으로 처리하지 않는다.
- 이미지 기반 PDF는 OCR 사용 여부와 OCR engine을 metadata에 기록한다.
- 표는 문장처럼 평탄화하지 않고 행·열·header 관계를 보존한다.
- 페이지 번호를 잃지 않는다.
- HWP/HWPX의 글자 깨짐·도형·표·각주를 별도 warning으로 남긴다.

#### 수정/보강 대상

- `app/parsers/factory.py`
- `app/parsers/pdf_parser.py`
- `app/parsers/hwp_parser.py`
- `app/parsers/hwpx_parser.py`
- `app/parsers/docx_parser.py`
- `app/schemas/parsed.py`
- `app/core/failure_classification.py`

#### 완료 기준

- 지원 형식별 parser 결과가 같은 ParsedDocument 계약을 따른다.
- 페이지와 표 provenance가 downstream Chunk까지 전달된다.
- parser warning이 사람이 읽을 수 있는 검수 사유로 변환된다.

### 4.3 3단계: 정규화

#### 목표

파싱 결과의 인코딩·공백·줄바꿈·깨진 문자를 정리하되 원문 추적성을 잃지 않는다.

#### 정규화 규칙

1. Unicode NFC 정규화
2. 알려진 mojibake 패턴 복구
3. 연속 공백·불필요한 줄바꿈 정리
4. 페이지 header/footer 반복 제거 후보 표시
5. 조문 번호와 항/호 marker 주변의 비정상 공백 정리
6. 표 셀 순서 보정
7. 날짜·비율·금액·문서번호의 문자 보존

#### 출력

```text
NormalizedBlock
├── normalized_text
├── raw_text_hash
├── normalization_rules_applied[]
├── source_page
├── source_block_id
└── review_flags[]
```

#### 중요한 제한

- 정규화 과정에서 의미를 추정해 문장을 새로 만들지 않는다.
- 숫자·조문 번호·퍼센트·기간은 원문 비교가 가능해야 한다.
- 수정된 문자열은 raw hash와 함께 보존한다.

#### 수정/보강 대상

- `app/processors/normalizer.py`
- `app/processors/mojibake.py`
- `app/processors/validator.py`
- `tests/test_article_validity.py`

### 4.4 4단계: 규정 구조 인식

#### 목표

다음 계층을 표준 metadata로 인식한다.

```text
규정명
└── 편
    └── 장
        └── 절
            └── 관
                └── 조
                    └── 항
                        └── 호
                            └── 목
                                └── 별표/별지/서식/표
```

#### 표준 구조 노드

```json
{
  "node_type": "article",
  "node_id": "article-12",
  "article_no": "제12조",
  "article_title": "접근권한 관리",
  "parent_path": ["제2장", "제12조"],
  "source_pages": [14],
  "confidence": 0.98,
  "review_flags": []
}
```

#### 인식 대상

- 제1조·제1조의2·제1조의3 같은 조문 번호
- ①·② 같은 항
- 1.·2. 또는 1)·2) 같은 호
- 가.·나. 같은 목
- 별표·별지·서식·표
- 부칙
- 개정문·신구조문대비표
- 표 제목과 표 본문 parentage

#### 수정/보강 대상

- `app/processors/structure_detector.py`
- `app/processors/metadata_extractor.py`
- `app/processors/kordoc_table_parser.py`
- `app/processors/kordoc_table_matcher.py`
- `app/schemas/structure.py`
- `app/processors/article_validity.py`

#### 완료 기준

- 조문 경계가 잘못 합쳐지거나 분리된 경우 review flag가 생긴다.
- 조문 번호·조문 제목·페이지·parent path가 모두 추적된다.
- 표·별표·별지의 본문과 참조 조문 관계가 유지된다.
- 구조 인식 실패가 조용히 통과하지 않는다.

### 4.5 5단계: Chunk 생성

#### 목표

LLM이 한 번에 읽을 수 있고 검색 결과 하나만으로도 의미가 유지되는 조문 단위 Chunk를 만든다.

#### Chunk 규칙

- 가능하면 하나의 조·항·호·목을 하나의 논리 단위로 유지한다.
- 너무 긴 조문은 항/호 경계에서 분할한다.
- 분할 시 parent context를 metadata로 반복한다.
- 서로 다른 조문이 같은 Chunk에 섞이지 않게 한다.
- 표는 header·단위·행 의미가 보존되는 크기로 만든다.
- 별표·별지·서식은 참조 조문과 연결한다.
- overlap은 의미 중복이 아니라 검색 문맥 보완 목적에 한정한다.

#### 표준 Chunk metadata

```json
{
  "chunk_id": "doc-12-chunk-004",
  "document_id": "doc-12",
  "tenant_id": "tenant-a",
  "profile_id": "institution-a",
  "regulation_id": "access-control",
  "regulation_version": "3.2",
  "chunk_type": "article",
  "article_no": "제12조",
  "article_title": "접근권한 관리",
  "paragraph_no": "①",
  "parent_path": ["제2장", "제12조"],
  "source_pages": [14],
  "content_hash": "...",
  "approval_status": "pending_review",
  "review_flags": []
}
```

#### 수정/보강 대상

- `app/processors/chunker.py`
- `app/schemas/chunk.py`
- `app/processors/answer_profile.py`
- `tests/test_batch_process_regulations.py`
- 조문·표·별지 전용 regression fixture

#### 완료 기준

- Chunk 하나만 조회해도 규정명·조문 위치·본문·페이지를 알 수 있다.
- Chunk identity가 content hash와 안정적으로 연결된다.
- 같은 문장을 중복 생성하는 header/footer 문제가 차단된다.

### 4.6 6단계: 품질검사와 사람 승인

#### 목표

AI가 잘 파싱했다고 주장하는 단계가 아니라, 운영자가 원문과 비교해 승인할 수 있는 단계로 만든다.

#### 품질검사 항목

1. 빈 본문
2. 조문 번호 누락
3. parent path 불일치
4. 조문 순서 역전
5. 중복 Chunk
6. 표 header 유실
7. 별표·별지 연결 누락
8. 페이지 provenance 누락
9. 날짜·버전·효력일 누락
10. 깨진 문자·비정상 기호
11. 원문과 정규화문 hash drift
12. 승인 전 처리 결과와 이전 승인본의 충돌

#### 상태 흐름

```text
uploaded
  → processing
  → pending_review
  → approved
  → indexed

pending_review → rejected
approved → superseded
approved → repealed
```

#### 핵심 불변식

- 승인 journal 없는 `approved` Chunk는 색인할 수 없다.
- `approved`가 아닌 Chunk는 RAG와 MCP에서 보이지 않는다.
- 최신본 판정은 tenant·profile·regulation family 단위로 한다.
- 과거 버전 조회는 `as_of_date` 이력 조회로 분리한다.

#### 수정/보강 대상

- `app/services/approval_governance.py`
- `app/services/approval_validation.py`
- `app/services/review_workflow_service.py`
- `app/services/review_decision_service.py`
- `frontend/streamlit_app.py`

#### 완료 기준

- 운영자가 원문·전처리문·AI 검수 결과를 나란히 비교할 수 있다.
- 미승인·불확실·검수 차단 이유가 화면에 보인다.
- 승인 상태 변경과 재색인이 모두 감사 trace에 남는다.

### 4.7 7단계: Export

#### Export 종류를 분리한다

1. 검수용 candidate export
2. 승인 증적 export
3. Vector ingest export
4. MCP 배포용 공개 metadata export

#### 공개 export에 넣지 않을 값

- 원본 로컬 경로
- 담당자 이름과 내부 메모
- raw upload 파일
- 기관 내부 파일 서버 주소
- 테넌트 운영용 secret
- 승인 전 Chunk

#### 표준 manifest

```json
{
  "manifest_type": "approved_regulation_index",
  "schema_version": "v1",
  "tenant_id": "tenant-a",
  "profile_id": "institution-a",
  "document_ids": ["doc-12"],
  "approved_chunk_count": 128,
  "embedding_model": "local-hash-embedding-v1",
  "embedding_dimensions": 384,
  "created_at": "2026-08-22T00:00:00Z",
  "source_approval_journal_hash": "..."
}
```

### 4.8 8단계: VectorDB 입력 데이터 생성

#### 현재 방식

현재 `app/ingestion/embedding_adapter.py`의 `local-hash-embedding-v1`은 재현 가능한 fallback으로 유지한다.

#### 개선 방식

- embedding backend interface를 정의한다.
- 모델명·버전·차원·언어·정규화 방식이 vector metadata에 들어가게 한다.
- 새 embedding model은 기존 vector와 자동 혼합하지 않는다.
- tenant/profile별 index namespace를 분리한다.
- 승인 journal과 vector manifest를 hash로 연결한다.
- 색인은 임시 위치에서 생성한 뒤 atomic swap한다.
- 재색인 중에는 이전 승인 index를 계속 제공한다.

#### 수정/보강 대상

- `app/ingestion/embedding_adapter.py`
- `app/ingestion/vector_adapter.py`
- `app/ingestion/vector_upsert.py`
- `app/ingestion/vector_integrity.py`
- `app/retrieval/bm25_index.py`
- `app/retrieval/hierarchical_index.py`

#### 완료 기준

- 색인에는 승인된 최신 규정만 존재한다.
- 색인 manifest와 승인 journal이 일치한다.
- 재색인 실패 시 기존 정상 index가 손상되지 않는다.
- vector model 변경을 audit에서 감지할 수 있다.

## 5. 시리즈 2 — 로컬 RAG QA 상세 계획

### 5.1 1단계: 사용자 질문 입력

#### 입력 계약

```json
{
  "query": "접근권한은 누가 관리해야 하나요?",
  "tenant_id": "from-auth-context",
  "profile_id": "institution-a",
  "department_ids": [],
  "security_levels": ["internal"],
  "as_of_date": null,
  "top_k": 8,
  "llm_backend": "ollama"
}
```

#### 처리

1. query length와 입력 위험 패턴 확인
2. tenant는 request body가 아니라 auth context에서 확정
3. profile·department·security level 권한 확인
4. as-of date가 있으면 과거 규정 조회 모드로 분리
5. query trace id 생성

#### 완료 기준

- 권한 범위 밖의 질문은 검색 전에 거부된다.
- 지나치게 긴 질문·악성 입력·잘못된 날짜는 명확한 오류로 반환된다.
- 질문 자체가 audit에 남더라도 비밀정보 처리 정책을 따른다.

### 5.2 2단계: 질문 분석과 검색어 보정

#### 목표

LLM에 먼저 질문을 보내지 않고, 결정적인 구조 분석을 먼저 수행한다.

#### 추출할 요소

- 규정명
- 조문 번호
- 조문 제목
- 항·호·목 번호
- 별표·별지·서식 요청
- 날짜·기간·금액·비율
- “누가/언제/어떤 조건/어떻게” 같은 질문 의도
- 현행/당시/특정 날짜 기준 요청

#### 검색어 보정 규칙

- 띄어쓰기·조사·기본 형태 정규화
- 조문 번호 표기 변형 통합
- 규정명과 조문 locator 분리
- 동의어는 사전 기반으로 제한
- 의미를 바꾸는 과도한 query expansion 금지
- 구조 locator가 있으면 정확 검색을 우선

#### 수정/추가 대상

- `app/retrieval/tokenizer.py`
- `app/retrieval/searcher.py`
- 새 파일 `app/rag/query_analyzer.py`

#### 완료 기준

- “제12조”, “12조”, “제 12 조”가 같은 locator로 처리된다.
- 규정명을 명시한 질문에서 다른 규정이 우선되지 않는다.
- 질문 보정 결과가 trace에서 확인된다.

### 5.3 3단계: Hybrid Retrieval

#### 검색 경로

```text
질문
 ├─ exact locator / metadata match
 ├─ BM25 / keyword match
 ├─ local embedding similarity
 └─ hierarchy / parent-child expansion
        ↓
   candidate union
```

#### 우선순위

1. tenant·profile·security·approval·lifecycle 필터
2. 정확한 규정명·조문 번호
3. 조문 제목·본문 keyword
4. BM25 점수
5. vector similarity
6. parent/child context 보완

#### 중요한 원칙

- 권한 필터는 score 계산보다 먼저 적용한다.
- 검색 score가 높아도 미승인 Chunk는 반환하지 않는다.
- 최신본 규정과 과거본을 일반 검색에서 섞지 않는다.
- 같은 조문을 여러 Chunk가 중복 반환하면 그룹화한다.

#### 현재 코드 활용

- `app/retrieval/searcher.py`
- `app/retrieval/bm25_index.py`
- `app/retrieval/hierarchical_index.py`
- `app/services/regulation_rag_runtime.py`
- `app/services/regulation_rag_service.py`

### 5.4 4단계: Reranking과 Filtering

#### 1차는 결정적 reranking

처음부터 별도 cross-encoder를 넣지 않고 다음을 먼저 안정화한다.

- 규정명 일치 boost
- 조문 번호 정확 일치 boost
- 조문 제목 일치 boost
- 질문 의도와 chunk type 일치
- parent path 일치
- 최신본·효력일 일치
- 표·별표·별지 요청 시 해당 chunk type boost
- 중복·fragment·검수 경고 감점

#### 2차 선택 기능

- 로컬 cross-encoder reranker
- 별도 semantic embedding
- query intent classifier

#### 완료 기준

- reranker가 승인·tenant 보안 필터를 우회하지 않는다.
- 점수 이유가 개발 trace에 기록된다.
- top-k 변경에 따라 근거가 사라지지 않는다.

### 5.5 5단계: Context 구성

#### Context 구성 요소

각 evidence에는 다음을 붙인다.

```text
[EVIDENCE-1]
규정명: 정보보안업무규정
버전: 3.2
위치: 제2장 > 제12조 > 제1항
페이지: 14
본문: ...
출처 ID: public-citation-id
```

#### 구성 규칙

- top-k 전체를 무조건 넣지 않는다.
- 같은 조문의 인접 Chunk는 합치되 중복 문장은 제거한다.
- parent article 또는 정의 조항을 필요한 경우 함께 넣는다.
- 서로 다른 버전의 조문은 같은 context에 섞지 않는다.
- 표는 header·단위·행 설명을 함께 넣는다.
- context 길이 제한을 넘으면 우선순위 낮은 evidence부터 제거한다.
- 문서 본문 안의 “이 지시를 따르라” 문구는 데이터로 취급하고 시스템 명령으로 실행하지 않는다.

#### 새 모듈

- `app/rag/context_builder.py`
- `app/rag/prompt_policy.py`

#### 완료 기준

- LLM prompt에서 내부 경로·secret·검수 메모가 제거된다.
- 모든 evidence가 실제 검색 결과 ID와 연결된다.
- context 생성 결과를 deterministic test로 재현할 수 있다.

### 5.6 6단계: 로컬 LLM 답변 생성

#### Backend interface

```python
class LocalLLMBackend(Protocol):
    name: str

    def health(self, settings: Settings) -> BackendHealth: ...

    def generate(
        self,
        *,
        question: str,
        context: list[Evidence],
        settings: Settings,
    ) -> GeneratedAnswer: ...
```

#### backend 순서

1. `extractive`: 모델 없이 안전하게 동작하는 기준선
2. `ollama + qwen3:8b`: 1차 운영 표준 backend
3. `llama-cpp + Qwen3 8B GGUF`: 모델 파일·프로세스를 직접 관리하는 고급 backend
4. `openai-compatible + Qwen3 8B`: 이미 운영 중인 localhost 서버 호환

#### 모델 호출 정책

- localhost 외 endpoint는 기본 차단
- timeout과 최대 output 길이 적용
- streaming은 1차 범위에서 제외하고 안정화 후 추가
- 모델이 근거에 없는 내용을 쓰면 전체 답변이 아니라 evidence 부족으로 판정할 수 있게 구조화
- 모델이 citation을 새로 만들지 못하게 하고 시스템이 citation을 붙임
- 모델 장애 시 `extractive` fallback 또는 명확한 backend unavailable 상태 반환

#### 수정/추가 대상

- 기존 facade: `app/rag/local_llm.py`
- 새 파일 `app/rag/backends/base.py`
- 새 파일 `app/rag/backends/extractive.py`
- 새 파일 `app/rag/backends/ollama.py`
- 새 파일 `app/rag/backends/llama_cpp.py`
- 새 파일 `app/rag/model_profiles.py`
- 새 파일 `scripts/local_llm_doctor.py`

### 5.7 7단계: 답변과 근거 조문 출력

#### 표준 Answer 응답

```json
{
  "trace_id": "trace-123",
  "answer": "정보시스템 관리자는 접근권한을 관리해야 합니다.",
  "answer_mode": "grounded_local",
  "abstained": false,
  "citations": [
    {
      "citation_id": "citation-1",
      "document_title": "정보보안업무규정",
      "regulation_version": "3.2",
      "article_no": "제12조",
      "paragraph_no": "①",
      "source_page": 14,
      "evidence_id": "doc-12-chunk-004"
    }
  ],
  "limitations": []
}
```

#### 답변 정책

- evidence가 없으면 “승인된 규정 근거에서 확인할 수 없습니다.”
- evidence가 질문을 완전히 뒷받침하지 못하면 제한사항을 표시한다.
- “법률 자문”처럼 과도한 확정 표현을 피한다.
- 답변은 원문이 아니라 요약이며, 최종 판단은 원문 확인이 필요할 수 있음을 표시한다.
- citation은 검색된 승인 record와 일치해야 한다.
- 내부 파일명·경로·tenant ID·approval ID는 외부 사용자용 metadata에서 제거한다.

#### 새 모듈

- `app/rag/answer_service.py`
- `app/rag/citation_builder.py`
- `app/rag/answer_policy.py`
- 기존 `app/rag/output_filter.py` 확장

## 6. 애플리케이션 API 계획

### 기존 API 유지

- 문서 업로드 API
- 처리 job API
- 승인·검수 API
- RAG search API
- MCP tool API

### 보강할 API

#### `POST /api/rag/chat`

- 기존 request 호환 유지
- `answer_mode` 반환
- `abstained` 반환
- citation에 공개용 구조 metadata 추가
- backend health 오류와 검색 결과 없음 오류를 구분

#### `GET /api/rag/llm/status`

```json
{
  "backend": "ollama",
  "model": "configured-model",
  "available": true,
  "endpoint_host": "127.0.0.1",
  "offline_safe": true,
  "last_checked_at": "..."
}
```

#### `POST /api/rag/answer/feedback`

1차에서는 답변을 학습에 자동 반영하지 않는다. 다음만 audit한다.

- citation이 맞았는가
- 답변이 충분했는가
- 근거가 부족했는가
- 운영자가 재검토를 요청했는가

## 7. Streamlit 화면 계획

### 현재 단계 화면을 다음 순서로 재배치

```text
0. 기관/프로젝트 선택
1. 규정 파일 등록
2. 전처리 진행 상태
3. 파싱·구조·표 결과 확인
4. 검수 대기 목록
5. 원문 비교·승인
6. 승인된 규정 색인
7. 로컬 LLM 연결 진단
8. 규정 질문하기
9. 답변·근거·원문 비교
10. 선택적 MCP 연결
```

### 로컬 질문 화면 구성

#### 상단

- 기관명
- 선택된 규정 범위
- 현행/기준일
- 현재 LLM backend
- 모델 연결 상태

#### 중앙

- 질문 입력창
- 검색 중 상태
- 답변
- 답변 모드와 제한사항

#### 하단

- 근거 조문 카드
- 문서명·버전·조문·페이지
- 원문/전처리문 비교 버튼
- “이 근거가 맞지 않음” feedback
- 검색 후보와 rerank 이유의 전문가용 보기

### 초보자 모드

- 모델이 없으면 자동으로 extractive 안내
- “규정 문서가 먼저 필요합니다” 같은 다음 행동 안내
- MCP·endpoint·embedding 전문용어 숨김
- 처리 상태를 파일 → 검수 → 승인 → 질문 흐름으로 표현

### 전문가 모드

- backend/profile 선택
- top-k·as-of·security scope 확인
- retrieval trace
- embedding/index version
- approval journal 상태
- MCP transport와 bundle 진단

## 8. 데이터 계약과 버전 관리

### 필수 식별자

모든 핵심 record는 다음 범위를 가진다.

```text
tenant_id
profile_id
document_id
regulation_id
regulation_version
chunk_id
content_hash
```

### 버전 필드

- parser schema version
- chunk schema version
- vector record schema version
- embedding model/version/dimensions
- answer profile version
- prompt policy version
- citation schema version

### 변경 시 재처리 기준

| 변경 | 필요한 조치 |
|---|---|
| parser 변경 | 대상 문서 재처리 + parser regression |
| normalizer 변경 | 대상 문서 재처리 + 원문 비교 |
| chunker 변경 | 대상 문서 재chunk + retrieval regression |
| embedding 모델 변경 | 전체 대상 재임베딩 + index swap |
| reranker 변경 | 검색 회귀 + 답변 회귀 |
| prompt policy 변경 | no-evidence·citation 회귀 |
| LLM 모델 변경 | 답변 품질·지연·메모리 재측정 |

## 9. 테스트 계획

### 9.1 단위 테스트

- parser별 경계·표·페이지
- mojibake·Unicode·숫자·날짜 정규화
- 조문 번호와 parent path
- chunk 분할·중복·overlap
- query locator 분석
- context budget
- citation mapping
- answer policy
- backend timeout·빈 응답·malformed JSON

### 9.2 계약 테스트

- extractive backend와 Ollama fake backend가 같은 Answer 계약을 반환하는지
- RAG search 결과가 AnswerService가 요구하는 Evidence 계약을 만족하는지
- MCP `search`/`fetch`가 로컬 QA와 같은 승인 범위를 사용하는지

### 9.3 보안 테스트

- 미승인 Chunk 검색 차단
- 다른 tenant 검색 차단
- 다른 profile 검색 차단
- security level·department ACL 차단
- 과거/폐지 규정의 일반 검색 노출 차단
- prompt injection 텍스트가 시스템 지시로 실행되지 않음
- 로컬 경로·secret·approval 내부 메모가 답변에 노출되지 않음
- 외부 endpoint 차단

### 9.4 답변 품질 회귀셋

질의 유형별로 별도 seed를 만든다.

1. 정확한 조문 번호 질의
2. 규정명 + 조문 질의
3. 자연어 조건 질의
4. 기간·금액·비율 질의
5. 표 질의
6. 별표·별지·서식 질의
7. 개정 이력·기준일 질의
8. 답변 불가능한 질의
9. 다른 기관 규정과 혼동하기 쉬운 질의
10. 문서에 prompt injection 문장이 들어간 질의

### 9.5 성능 테스트

- 첫 query cold start
- 문서 1개/10개/100개 규모
- top-k별 latency
- embedding batch latency
- Ollama response latency
- 동시 질문 수
- 재색인 중 검색 안정성
- 메모리 사용량과 모델 context 제한

## 10. 품질 게이트

### 전처리 게이트

- 지원 파일 형식의 parser 성공
- 빈 추출 결과 0
- 구조 누락은 검수 대기
- 표 parentage 차단 수 0
- source page coverage 기준 충족

### 승인·색인 게이트

- 승인 journal과 vector manifest 일치
- 미승인 record 0
- tenant 범위 mismatch 0
- 최신본 판정 오류 0
- embedding schema drift 0

### 답변 게이트

- answerable query citation coverage 100%
- no-evidence control query에 허위 citation 0
- citation이 실제 evidence와 일치
- 조문 번호 질의 정확도 목표 95% 이상
- 내부 경로·비공개 metadata 유출 0
- LLM 실패 시 안전 fallback 확인

### MCP 회귀 게이트

- `list_regulations`
- `search`
- `fetch`
- `get_article`
- `get_table`
- `get_regulation_history`
- stdio smoke
- streamable HTTP smoke

## 11. 단계별 개발 순서

### Sprint 0 — 기준선과 보호 테스트

산출물:

- 현재 테스트 결과 report
- 로컬 QA·MCP 공통 보안 불변식 테스트
- 답변 품질 seed 초안
- 변경 대상 파일 목록

완료 조건:

- 기존 사용자 코드와 공개 문서 변경을 덮어쓰지 않는다.
- 현재 MCP와 승인 색인 흐름이 모두 통과한다.

### Sprint 1 — 공통 Evidence·Answer 계약

산출물:

- `Evidence` 내부 모델
- `Answer` 응답 모델
- citation builder
- answer policy
- fake backend

완료 조건:

- 모델 없이도 새로운 Answer 계약으로 응답한다.
- 기존 `/api/rag/chat` response 호환이 유지된다.

### Sprint 2 — AnswerService 분리

산출물:

- query analyzer
- context builder
- backend registry
- `routes_rag.py` thin route 전환

완료 조건:

- API route가 검색·prompt·backend 세부 구현을 직접 조합하지 않는다.
- 서비스 단위 테스트로 전체 흐름을 재현한다.

### Sprint 3 — Ollama 로컬 MVP

산출물:

- Ollama backend
- health/doctor
- 모델 profile
- timeout·fallback·오류 화면

완료 조건:

- Ollama가 없으면 extractive로 사용 가능하다.
- Ollama가 있으면 같은 evidence 기반 자연어 답변이 나온다.
- 외부 API가 호출되지 않는다.

### Sprint 4 — 근거 품질과 답변 회귀

산출물:

- citation/evidence 평가기
- no-evidence control set
- 조문·표·별지 답변 seed
- answer trace report

완료 조건:

- 답변 품질이 parser·retrieval 변경으로 악화되면 gate가 실패한다.
- 근거 없는 답변과 citation 조작을 차단한다.

### Sprint 5 — 로컬 QA 화면

산출물:

- 모델 상태 카드
- 질문 입력·답변·근거 카드
- 원문 비교
- 초보자 안내 흐름

완료 조건:

- 사용자가 MCP를 설정하지 않고 규정 질문까지 도달한다.
- 승인 전에는 질문 화면에서 해당 규정을 사용할 수 없다.

### Sprint 6 — 검색·임베딩 개선

산출물:

- embedding backend protocol
- 선택적 semantic embedding
- model/version-aware index manifest
- retrieval benchmark

완료 조건:

- 기존 deterministic fallback이 유지된다.
- 모델 변경 시 재색인 없이 실행되지 않는다.
- 기존 질의 회귀가 악화되지 않는다.

### Sprint 7 — llama.cpp와 폐쇄망 패키징

산출물:

- llama.cpp backend
- Windows doctor
- 모델 경로·포트·GPU 상태 진단
- optional dependency/package 분리

완료 조건:

- 인터넷 없이 문서·검색·답변이 가능하다.
- 모델 미설치·메모리 부족·프로세스 실패가 운영자에게 명확하다.

### Sprint 8 — MCP·릴리스 통합

산출물:

- 로컬 QA와 MCP 공통 RAG contract
- MCP regression bundle
- release evidence
- 사용자 문서 개편

완료 조건:

- 로컬 QA와 MCP가 같은 승인 데이터와 citation 정책을 사용한다.
- 기존 MCP 사용자는 연결 변경 없이 계속 사용할 수 있다.

## 12. 파일 구조 개선안

```text
app/
├── rag/
│   ├── answer_service.py
│   ├── answer_policy.py
│   ├── citation_builder.py
│   ├── context_builder.py
│   ├── query_analyzer.py
│   ├── model_profiles.py
│   ├── local_llm.py                 # 기존 호환 facade
│   └── backends/
│       ├── base.py
│       ├── extractive.py
│       ├── ollama.py
│       └── llama_cpp.py
├── retrieval/
│   ├── searcher.py
│   ├── bm25_index.py
│   ├── hierarchical_index.py
│   └── retrieval_trace.py
├── ingestion/
│   ├── embedding_adapter.py
│   ├── embedding_registry.py
│   └── vector_integrity.py
└── services/
    ├── regulation_rag_service.py
    └── regulation_rag_runtime.py

scripts/
├── local_llm_doctor.py
├── benchmark_local_rag.py
├── evaluate_local_answers.py
└── build_local_qa_release_evidence.py
```

## 13. 배포 프로필

### Profile A — 모델 없는 기본형

- 전처리
- 승인
- 검색
- extractive 답변
- MCP 선택 사용

용도: 개발·검수·저사양 PC·품질 기준선

### Profile B — Ollama + Qwen3 8B 로컬 AI

- Profile A 전체
- Ollama endpoint
- `qwen3:8b` 모델
- 자연어 답변

용도: 일반 기관 PC와 빠른 도입

### Profile C — llama.cpp + Qwen3 8B 폐쇄망

- Profile A 전체
- Qwen3 8B GGUF 로컬 model file
- 직접 프로세스 관리
- 외부 서버 의존 최소화

용도: 폐쇄망·보안 강화 환경

### Profile D — MCP 연결

- Profile A/B/C의 승인 RAG
- stdio 또는 streamable HTTP
- 외부 AI가 search/fetch 도구 사용

용도: ChatGPT·Claude·Codex·기관 AI 연동

## 14. 하지 않을 것

1차 전환에서는 다음을 하지 않는다.

- 규정 데이터를 사용한 자동 파인튜닝
- 모델이 승인 여부를 결정하는 자동 승인
- 원문을 외부 API로 보내는 fallback
- 무조건적인 모델 자동 다운로드
- MCP와 로컬 QA의 별도 검색 엔진 개발
- 기관 간 규정 비교
- SSO·대규모 다중 사용자 운영
- 모델 답변만으로 citation 생성

## 15. 1차 완료 정의

다음 시나리오가 한 PC에서 재현되면 이미지 방향의 1차 구현이 완료된 것으로 본다.

1. 운영자가 기관을 선택한다.
2. PDF/HWP/DOCX 규정을 등록한다.
3. 시스템이 파싱·정규화·조문·표·Chunk 결과를 만든다.
4. 운영자가 원문과 결과를 비교하고 승인한다.
5. 승인된 최신본만 Vector/BM25 색인에 들어간다.
6. 운영자가 “접근권한은 누가 관리해야 하나요?”라고 질문한다.
7. 시스템이 질문을 분석하고 키워드·구조·vector 검색을 수행한다.
8. 시스템이 승인·기관·최신본 범위를 다시 확인한다.
9. 모델이 없으면 extractive 근거 답변을 반환한다.
10. Ollama가 연결되면 같은 evidence 기반 자연어 답변을 반환한다.
11. 답변에 규정명·조문·페이지 citation이 붙는다.
12. 근거가 없는 질문에는 확인 불가라고 답한다.
13. 같은 승인 데이터를 MCP `search`/`fetch`에서도 조회한다.
14. 다른 tenant·미승인·과거 비활성 규정은 어느 경로에서도 노출되지 않는다.

## 16. 가장 먼저 착수할 5개 작업

1. `Evidence`·`Answer`·`Citation` 내부 계약 작성
2. 기존 `routes_rag.py`의 답변 조합을 `AnswerService`로 이동
3. fake backend 기반 extractive/Ollama 계약 테스트 추가
4. `local_llm_doctor`로 Ollama 상태·`qwen3:8b` 모델·localhost·timeout 점검
5. Streamlit에 Qwen3 8B 기반 “로컬 규정 질문” 최소 화면 추가

이 다섯 작업이 끝나면 이미지의 시리즈 2가 현재 코드에서 실제로 동작하는 첫 수직 슬라이스가 된다. 그 다음에야 semantic embedding, llama.cpp 패키징, UI 고도화를 진행한다.
