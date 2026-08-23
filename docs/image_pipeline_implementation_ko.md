# 이미지 기준 파이프라인 구현 상태

이 문서는 `공공기관 규정 RAG 프로젝트` 이미지의 두 시리즈를 현재 소스 구조에 대응시킨다.
이미지의 흐름을 화면용 그림으로만 두지 않고, API 응답·처리 실행 통계·품질 JSON·RAG trace에서 같은 단계 식별자를 사용하도록 했다.

## Series 1 — 규정 전처리

| 이미지 단계 | 코드 단계 ID | 실제 소유 코드 | 산출물/통제 |
|---|---|---|---|
| 1. 문서 업로드 | `upload_admission` | `DocumentService`, `routes_documents` | 테넌트·파일 시그니처·SHA-256·중복 버전 admission |
| 2. 파싱 | `parse_extract` | `app/parsers/*`, `PaddleKoreanOcrAdapter` | PDF/DOCX/HWP/HWPX 파싱, 저추출 페이지만 Korean PP-OCRv5, bbox·confidence 보존 |
| 3. 정규화 | `normalize` | `TextNormalizer` | 문자·공백·페이지·출처 보존 정리 |
| 4. 조문 구조 인식 | `structure_detect` | `StructureDetector`, Qwen3 4B bounded reviewer | 규정·장·절·조·항·호 구조 노드, 불확실 구조·표 finding |
| 5. 청크 생성 | `chunk_generate` | `Chunker` | 조문 단위 검색 청크와 페이지 provenance |
| 6. 품질 검사 | `quality_gate` | `Validator`, `QualityGate` | 오류·경고·품질 점수·사람 검수 worklist |
| 7. export | `export` | `Exporter` | JSONL·CSV·Markdown·표 export |
| 8. 벡터 DB 입력 | `vector_index` | `routes_documents`, `vector_upsert`, Qwen3 Embedding 0.6B | 승인 journal·테넌트 범위 검증 후 로컬 의미 벡터 입력 |

업로드와 파싱은 의도적으로 분리되어 있다. 업로드 성공은 원본을 안전하게 보관했다는 뜻이지, 본문 추출이 완전하다는 뜻이 아니다.
파서가 반환하는 `extraction_quality`에는 페이지 커버리지, 텍스트·표·이미지 block 수, 이미지 전용 페이지, OCR/불확실성 검수 사유가 들어간다.
`blocked`는 정규화로 진행하지 않고, `review_required`는 진행은 가능하지만 사람 승인 전에 확인해야 한다.

## Series 2 — 로컬 규정 QA

| 이미지 단계 | 코드 단계 ID | 실제 구현 |
|---|---|---|
| 질문 분석 | `query_analysis` | Qwen3 1.7B 구조화 query plan + schema 실패 시 deterministic fallback |
| 검색어 보정 | `query_correction` | Qwen3 1.7B 조문·규정명·별표/서식 보수적 확장 |
| 하이브리드 검색 | `hybrid_retrieval` | 승인·테넌트·ACL 필터 뒤 BM25 + Qwen3 Embedding 0.6B RRF 융합 |
| 재순위·필터 | `rerank_filter` | Qwen3 Reranker 0.6B + 최신 버전·보안 등급·부서 ACL·승인 이중 필터 |
| 컨텍스트 구성 | `context_build` | 내부 chunk ID를 숨긴 `E1` 계열 bounded Context와 provenance |
| 로컬 LLM | `local_llm_answer` | `qwen3:8b` / Ollama, 승인 evidence ID 강제 근거 답변 |
| 인용 검증 | `citation_verify` | Qwen3 4B 주장 감사 + content hash·exact quote 결정적 검증 |

하이브리드 검색은 가시성 필터 뒤에 실행된다. 따라서 벡터 점수가 높다는 이유로 다른 테넌트, 미승인 chunk, ACL 밖의 chunk가 후보로 다시 들어올 수 없다.
Qwen3는 원문 파일 경로·비공개 저장소 경로·승인되지 않은 본문을 입력으로 받지 않고, 검색 결과로 이미 승인된 evidence만 받는다.

## 운영 확인 지점

- `ProcessingJob`: `pipeline_id`, `stage_id`, `stage_number`, `stage_status`
- `ProcessingRun.stats.pipeline_trace`: 전처리 단계별 실행 결과와 실패 reason code
- `quality.json`: `extraction_metrics`와 기존 구조·표·본문 품질 지표
- indexing job: `pipeline_stage_id=vector_index`, 승인된 청크 수, 임베딩/업서트 결과
- RAG trace: `pipeline_trace`, retrieval model, fallback 상태, evidence 수
- `GET /api/pipelines/manifest`: 원문·경로 없이 두 파이프라인의 기계 판독 가능한 계약 반환

## 현재 완료된 실제 검증

- 실제 `qwen3:1.7b`, `qwen3:4b`, `qwen3:8b` 역할별 JSON 계약과 근거 답변 검증
- 실제 Qwen3 Embedding 0.6B 1024차원 의미 분리와 Qwen3 Reranker 0.6B 관련도 분리 검증
- 실제 Korean PP-OCRv5 한국어 fixture 인식, Qwen3 4B 구조 경계·표 열 불일치 검출
- 비-loopback socket을 차단한 상태에서 전처리 8 + QA 7단계 수용시험 15/15 통과
- 사람 승인 journal, 승인 hash 기반 색인, 내부 ID 비노출, exact citation 확인
- 전체 회귀, sdist/wheel, 공개 저장소 위생 검사 통과

## 기관 상용 출시 전에 남은 외부 검증

1. 기관별 실제 PDF/HWP/HWPX/DOCX goldset을 확보하고 추출 커버리지·표 셀 보존·조문 경계 recall을 기준선화한다.
2. 목표 판매 장비에서 cold start, 동시 질문, 최대 Context, timeout/retry, CPU/RAM/VRAM 예산을 부하 검증한다.
3. 기관 담당자가 승인한 질의 세트로 Recall@k, MRR, 인용 정확도, “근거 없음” abstention을 측정하고 계약 SLA를 확정한다.
4. 상용 배포 환경에서 tenant별 저장소, 감사 이벤트, 모델 파일 배포/업데이트, 백업·복구와 dependency/model 라이선스 검토를 완료한다.
5. 본 변경은 전처리 로직을 포함하므로 보호 PR template, Code Owner review, `preprocessing-reviewed` label을 거친다.
