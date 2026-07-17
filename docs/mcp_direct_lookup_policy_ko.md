# MCP 규정 직접조회 우선 정책

법령·규정 질의는 자유 생성형 RAG 답변을 기본으로 하지 않는다.

## 호출 정책

1. MCP 클라이언트가 `document_id`를 알고 있으면 `lookup`을 호출한다.
2. `lookup`은 승인된 문서 또는 조문을 직접 조회한다.
3. 직접조회 결과가 있으면 `retrieval_mode=direct_lookup`으로 반환한다.
4. 직접조회 결과가 없으면 승인된 로컬 인덱스의 `search` 경로로 전환하고 `retrieval_mode=rag_fallback`으로 표시한다.
5. 직접조회와 RAG 모두 근거가 없으면 추정하지 않는다.

## 원문 근거

`lookup`, `search`, `fetch`, 문서·조문·표 조회 응답에는 `verbatim_text` 또는 `verbatim.text`가 포함된다. 최종 답변은 이 원문과 문서 ID, 조문, 버전, 효력일, 페이지, 승인 콘텐츠 해시를 근거로 삼아야 한다.

`verbatim`은 모델이 생성한 요약이 아니라 승인된 로컬 규정 chunk/table에서 반환된 텍스트다. 따라서 외부 AI는 원문을 인용할 때 요약문을 따옴표로 표시하지 말고 `verbatim.text`를 사용한다.

## 범위

이 정책은 현재 full MCP tool profile의 `lookup`에 적용된다. ChatGPT data profile은 기존 `search`/`fetch` 계약을 유지하므로, 해당 클라이언트에서는 `search` 후 `fetch`를 사용한다.
