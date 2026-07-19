# 9시간 MCP Builder 신뢰성·성능 야간 작업 계획

## 목표

병합된 `public/main`을 기준으로 문서를 파싱하고 전처리한 결과가 승인·테넌트 경계를 지키면서 MCP 런타임의 검색·조회·이력 도구로 안정적으로 전환되는지 재검증한다. 병목은 근거가 있는 범위에서만 수정하고, 불확실한 원문은 자동 승인하지 않는다.

## 작업 순서와 종료 조건

| 시간 | 작업 | 종료 조건 |
| --- | --- | --- |
| 0:00–0:45 | 기준선, 브랜치, 문서·도구 계약 확인 | `main` 커밋, 전체 테스트, 빌드, 공개 릴리스 위생 결과 기록 |
| 0:45–2:00 | PDF/HWP/HWPX/DOCX 파서·보안 감사 | 손실·XML·이미지·표 불확실성이 메타데이터와 회귀 테스트에 남음 |
| 2:00–3:30 | 승인 저널·사이드카·테넌트/프로필 격리 감사 | 승인된 레코드만 MCP-visible이며 드리프트가 fail-closed |
| 3:30–5:00 | stdio/streamable HTTP와 MCP client profile 검증 | 초기화, 도구 검색, search/fetch/history, bearer 인증 스모크 통과 |
| 5:00–6:30 | 계층형 인덱스·BM25·동시성·cold-start 개선 | 검색 p50/p95와 warmup 지표를 JSON/MD 근거로 남김 |
| 6:30–7:45 | 포커스→전체 회귀, 빌드, 위생 재실행 | `unittest`, wheel/sdist, release hygiene 모두 통과 |
| 7:45–8:30 | 운영 문서·PR 거버넌스·보안 handoff 정리 | 변경 파일, 위험, 재현 명령, Code Owner 검토 항목 정리 |
| 8:30–9:00 | 최종 체크포인트와 후속 PR 준비 | 세션 로그 `can_complete=true`, 보고서·커밋·푸시 상태 확인 |

## 현재 확인된 수용 기준

- 전체 기준선: `python -m unittest discover -s tests -v` 2100개 통과, 14개 skip.
- 깨끗한 승인 MCP bundle: readiness/index visibility 277/277, stdio와 bearer 인증 HTTP smoke 통과.
- 계층형 SQLite 검색: BM25가 없어도 유효한 retrieval runtime으로 진단되며, 동시성 benchmark가 이를 오류로 오판하지 않음.
- 파서: HWP 잘림/UTF-16 손실, HWPML/HWPX DTD·entity, PDF 텍스트+이미지 혼합 페이지, HWPX 비본문 XML, 짧은 구조화 표를 review 신호로 보존.

## 안전 원칙

- 원문·런타임 데이터·기관 식별자는 공개 소스와 MCP 응답에 넣지 않는다.
- 승인 snapshot/저널/콘텐츠 해시/ACL이 어긋나면 검색·fetch를 거부한다.
- 경고가 있는 파서는 자동 승인하지 않고 Code Owner가 검토할 수 있는 근거와 회귀 테스트를 남긴다.
- private real-fixture root가 없으면 fixture gate는 실패 상태를 숨기지 않고 fail-closed로 기록한다.
