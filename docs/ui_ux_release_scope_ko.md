# UI/UX 릴리즈 범위

기준일: 2026-07-07

이 문서는 `reg-rag-preprocessor`의 현재 UI/UX 상태를 릴리즈 전에 오해 없이 설명하기 위한 범위 문서입니다.

## 결론

현재 UI는 완성형 SaaS 화면이 아니라, 로컬 운영자용 Streamlit operator console입니다.

릴리즈 가능한 범위:

- 신뢰된 로컬 PC 또는 로컬 Docker profile에서 문서를 업로드하고 전처리 결과를 확인합니다.
- PDF, DOCX, HWPX, HWP 규정 문서를 업로드할 수 있습니다.
- 검토/승인 준비 상태, 구조, chunk, validation issues, handoff exports를 탭으로 확인합니다.
- JSONL, CSV, Markdown, tables JSONL/CSV, quality JSON/Markdown을 내려받습니다.
- 기관 profile과 quality profile을 로컬 세션에서 편집하고 저장할 수 있습니다.

릴리즈 범위가 아닌 것:

- 외부 사용자용 멀티테넌트 웹 포털이 아닙니다.
- 보호된 shared deployment에서 쓰는 UI가 아닙니다.
- 계정, 역할, 승인 workflow, 대시보드, 장기 job queue를 갖춘 제품형 UX가 아닙니다.
- 공공기관 최종 운영 화면이 아니라 파일럿 검증과 내부 operator handoff를 위한 화면입니다.

## 보안 경계

Streamlit은 local-only UI입니다.

- `API_AUTH_REQUIRED=true` 또는 `TENANT_STORAGE_ISOLATION=true`이면 Streamlit은 즉시 중단됩니다.
- 보호된 공유 환경에서는 authenticated FastAPI path를 사용해야 합니다.
- tenant-isolated storage, audit log, auth denial trail, private release gate는 FastAPI/release workflow 쪽에서 검증됩니다.

## 현재 UX가 주는 가치

공공기관 파일럿에서 중요한 것은 보기 좋은 화면보다 재현 가능한 전처리 증거입니다. 현재 UI는 다음 질문에 빠르게 답하도록 설계했습니다.

- 이 문서가 공식 RAG/MCP 인덱싱 전 검토/승인 후보로 통과했는가?
- 조문 구조와 chunk가 어떻게 나뉘었는가?
- 품질 점수와 validation issue는 무엇인가?
- table artifact와 quality evidence를 받을 수 있는가?
- 파일럿 담당자에게 전달할 handoff export가 만들어졌는가?

## 제품형 UI로 가려면 남은 항목

다음 항목은 private GitHub 릴리즈 이후 별도 제품화 backlog로 보는 것이 맞습니다.

- 로그인, 역할, 승인 권한 UI
- 장기 batch job progress와 재시도 queue
- 기관별 tenant dashboard
- 품질 점수 trend와 batch 비교 화면
- OCR 필요 문서와 실패 문서의 operator triage 화면
- release evidence bundle을 UI에서 직접 열람하는 화면
- 접근성, 반응형 레이아웃, 브랜딩, 한국어 microcopy 전면 정리

## 릴리즈 판단

private release 기준으로는 UI/UX가 "로컬 operator console" 범위에서 충분합니다.

공공기관 데모 기준으로는 다음 설명을 함께 붙여야 합니다.

> 이 화면은 최종 대민/대기관 포털이 아니라, 규정 문서를 공식 RAG/MCP 인덱싱에 올리기 전 전처리 품질, 검토/승인 후보, 산출물을 확인하는 preview-only 로컬 운영자 콘솔입니다.
