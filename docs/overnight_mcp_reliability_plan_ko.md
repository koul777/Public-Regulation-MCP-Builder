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
| 5:00–6:30 | 계층형 인덱스·BM25·동시성·cold-start·Kordoc 계측 개선 | 검색 p50/p95, warmup, 외부 파서 elapsed/timeout/truncation 지표를 JSON/MD 근거로 남김 |
| 6:30–7:45 | 포커스→전체 회귀, 빌드, 위생 재실행 | `unittest`, wheel/sdist, release hygiene 모두 통과 |
| 7:45–8:30 | 운영 문서·PR 거버넌스·보안 handoff 정리 | 변경 파일, 위험, 재현 명령, Code Owner 검토 항목 정리 |
| 8:30–9:00 | 최종 체크포인트와 후속 PR 준비 | 세션 로그 `can_complete=true`, 보고서·커밋·푸시 상태 확인 |

## 현재 확인된 수용 기준

- 전체 회귀: `python -m unittest discover -s tests -q` 2119개 통과, 14개 skip (174.0초).
- 깨끗한 승인 MCP bundle: readiness/index visibility 277/277, stdio와 bearer 인증 HTTP smoke 통과.
- 계층형 SQLite 검색: BM25가 없어도 유효한 retrieval runtime으로 진단되며, 동시성 benchmark가 이를 오류로 오판하지 않음.
- 파서: HWP 잘림/UTF-16 손실, HWPML/HWPX DTD·entity, PDF 텍스트+이미지 혼합 페이지, HWPX 비본문 XML, 짧은 구조화 표를 review 신호로 보존.
- Kordoc 외부 실행: 문서 메타데이터에 입력 확장자, 실행시간, timeout, table inventory 잘림 여부를 기록해 운영 중 성능·손실 원인을 추적한다.
- Kordoc 임시 경로 생성 실패도 일반화된 오류 코드만 반환해 절대 경로가 인증 응답·JSONL로 흘러가지 않도록 한다.
- 외부 ChatGPT/Claude API·Secure Tunnel은 `chatgpt-data` 프로필로 제한하고, 원격 smoke는 `search`→`fetch`와 내부 metadata deny-list를 검증한다.
- ZIP bundle smoke는 traversal/symlink/크기 제한과 `--require-console-scripts` 실행 경로 증명을 지원한다.
- 승인 snapshot sidecar는 ZIP 해제 후에도 파일 inode/mtime에 의존하지 않고 크기+SHA-256 지문으로 재사용되며, 복사 경로 회귀 테스트를 포함한다.

## 후속 개선 후보

- `QualityReport.passed`의 기존 호환 의미는 유지하고, 승인 직전 전용 `approval_ready`/blocking-warning 정책을 additive하게 도입한다.
- Kordoc은 현재 릴리스 경로의 전 포맷 증거 계약을 깨지 않도록 유지하되, 포맷별 호출 필요성은 별도 benchmark와 명시적 설정으로 검토한다.

## 이번 9시간 세션 실행 상태

- 완료: 기준선·파서 보안·승인/테넌트·stdio/HTTP·계층형 retrieval·Kordoc telemetry/redaction 감사.
- 완료: 전체 회귀 2119/14 skip, wheel/sdist, release hygiene, dependency-complete wheel console-script 5/5, client-config stdio 3/3, focused handoff 81 tests.
- 완료: clean bundle 277/277 readiness, cold-start 5/5, concurrent/query benchmark 무결성 및 portable sidecar 성능 재측정.
- 완료: secure-profile bundle/ZIP extraction 및 pinned venv console-script provenance 검증.
- 대기: GitHub protected PR의 실제 Code Owner 검토와 `preprocessing-reviewed` 라벨. 라벨은 테스트 통과를 대신하지 않으며, 근거 확인 후에만 적용한다.

## 안전 원칙

- 원문·런타임 데이터·기관 식별자는 공개 소스와 MCP 응답에 넣지 않는다.
- 승인 snapshot/저널/콘텐츠 해시/ACL이 어긋나면 검색·fetch를 거부한다.
- 경고가 있는 파서는 자동 승인하지 않고 Code Owner가 검토할 수 있는 근거와 회귀 테스트를 남긴다.
- private real-fixture root가 없으면 fixture gate는 실패 상태를 숨기지 않고 fail-closed로 기록한다.
