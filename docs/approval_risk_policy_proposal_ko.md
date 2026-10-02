# 위험 등급별 승인 정책 설계 제안 (초안)

상태: **제안**이다. 코드와 동작은 바뀌지 않았다. `docs/beginner_maintenance_orchestration_plan_ko.md`
40–42행과 170–180행이 "별도 제품 결정"으로 미뤄 둔 원클릭 승인 안전 정책을 결정할 때 쓰는 자료다.
근거는 모두 `파일:행`으로 적었다. 실행이나 테스트로 확인하지 않았으므로, 런타임 동작은 코드를 읽고 추론한 것이다.

정정 이력: 독립 보안 검토와 직접 재확인 결과를 반영해 1.2, 2, 3.1, 4, 5절을 고쳤고 10절을 추가했다.
`frontend/streamlit_app.py`의 행 번호는 이 문서를 쓴 직후 `app/core/hidden_process` import가 추가되어
모두 1행씩 뒤로 밀렸다(문서의 값 +1). 행 번호는 곧 낡으므로 함수와 상수 이름으로 다시 찾는다.

## 1. 현재 상태

### 1.1 서버가 실제로 강제하는 것 (fail-closed)

- 승인 API는 `POST /{document_id}/review/approve`의 `approve_review_chunks`다(`app/api/routes_documents.py:2699`).
  쓰기 역할 확인(2713)과 문서 접근 확인(2714)을 하고, 테넌트 범위 저장소에서 청크를 읽는다(2707–2715).
- 이미 승인된 청크는 `allow_reapproval` 없이 다시 승인할 수 없다(409, 2806–2817).
- 승인 전 보안 스캔이 필수이고, 위험도 high가 발견되면 차단한다(2818–2827, `review_workflow_service.py:439–448`).
- `validate_approval_preconditions`(`app/services/review_workflow_service.py:227`)가 막는 것:
  모호한 합본 경계(235–249), `security_blocked/rejected/superseded` 상태(250–260, `review_decision_service.py:8`),
  검수 주의 사유가 있는데 `review_flags_acknowledged`도 override 사유도 없는 경우(261–268).
- 공식 증거 필드(worklist, 배치 manifest, fingerprint)는 필수다(`review_workflow_service.py:83–89`, 743–747).
  manifest의 청크 집합과 `review_content_hash`가 현재 청크와 정확히 같아야 한다(953–985).
- 색인과 내보내기 단계에서 승인 상태, 해시, 테넌트, 보안 등급, 승인 journal 레코드를 다시 대조한다
  (`app/services/approval_validation.py:43–60`, 106–125, 162–185).

### 1.2 권고만 하고 막지 않는 것

- 사람 검수 완료 여부는 승인을 막지 않는다. 확인 이벤트가 없는 청크는 서버가
  `approved_without_review` 이벤트를 만들어 기록하고 진행한다(`routes_documents.py:2943–2961`).
  사유가 비어 있으면 기본 문구가 들어간다(130–132, 2917–2920).
- override 사유가 있으면 검수 주의 플래그 확인도 생략할 수 있다(`review_workflow_service.py:263`).
  UI의 일괄 승인은 미완료 조항이 있으면 `review_flags_acknowledged=False`에 기본 사유를 넣어 보낸다
  (`frontend/streamlit_app.py:6573–6576`, 6633, 6646; 기본 사유는 1521–1523).
  그래서 파서 불확실성 high·critical(`review_workflow_service.py:117`, 183–198) 같은 신호도 기본 사유 하나로 통과한다.
- 같은 override 사유를 승인 API는 쓰기 역할이면 누구나(operator 포함) 쓸 수 있다(`routes_documents.py:2713`).
  병합 API는 같은 사유에 admin을 요구한다(2595–2596). 두 API의 정책이 서로 다르다.
- 그 사유가 journal에 남는 조건은 미검수 청크가 있을 때뿐이다(2984–2994). 모든 청크에 클라이언트가
  `human_review_confirmed`를 보냈다면, 플래그 확인을 면제받은 사유가 기록되지 않는다. 그러면
  `review_flags_acknowledged=false`인데 사유가 없는 레코드가 생긴다.
- 화면 문구도 "지금 최종 확정해도 진행"된다고 명시한다(`streamlit_app.py:11607–11615`, 12074–12083, 12272–12276).
- 초보자 집중 모드에서는 버튼 하나("내용이 맞아요 · 다음", 10740–10744)가 AI 판단 확인,
  사람 확인, 위젯 상태를 한 번에 `True`로 바꾼다(`_confirm_focused_review`, 10530–10534).

### 1.3 journal에 남는 것

- 승인 레코드에 남는 필드: `approved_by`, 해시, `review_flags_acknowledged`, `review_attention_*`, 스캔 ID
  (`review_workflow_service.py:559–597`). 청크 스냅샷에는 `review_attention_reasons`가 들어간다(514–524).
- 이벤트는 `human_review_coverage`(status, 확인 수, 미검수 수), `review_decision_event_counts`,
  `approval_override_reason`으로 집계된다(`routes_documents.py:2962–2994`).
- 이벤트 종류는 다섯 가지뿐이다(`approval_governance.py:11–17`). 허용 목록에 없는 필드는 정규화 단계에서 버려진다(178–218).
- 청크의 `approval_status`에는 `reviewed`가 없다(`app/schemas/chunk.py:9`).
  "reviewed" 단계는 이벤트로만 남는다(`approval_governance.py:67–76`).

## 2. 문제: 형식적 승인(rubber-stamping)

신뢰 모델은 "사람이 승인한 청크만 색인한다"이다. 하지만 지금의 "사람 확인"은 증명이 아니라 **클라이언트의 주장**이다.

1. `human_review_confirmed` 이벤트는 요청 본문 `review_decision_events`로 들어온다(`routes_documents.py:161–164`).
   서버는 actor만 인증된 값으로 바꾸고(2926–2930) 나머지는 그대로 믿는다.
   원문을 실제로 봤는지, 얼마나 오래 봤는지 확인할 근거가 없다.
2. 이벤트 시각은 최종 승인 시점에 일괄로 찍힌다(`approval_governance.py:105`, `streamlit_app.py:6608–6624`).
   조항별 확인 시각은 세션 상태에만 있고 기록되지 않는다(10530–10534). 그래서 지금 데이터로는 체류 시간을 알 수 없다.
3. Streamlit 로컬 승인은 actor가 고정값 `streamlit-local-operator`이고 role은 `admin`이다(`streamlit_app.py:6438–6443`).
   인증을 끈 API는 X-Actor 헤더를 그대로 쓴다(`app/core/security.py:79–84`).
   토큰 모드에서는 헤더가 아니라 토큰에 적힌 actor가 항상 쓰인다(108). `actor_is_bound`는 헤더 값이 다를 때
   403을 낼지만 정한다(106). `AuthContext`에는 이 필드가 없다(`app/core/security_primitives.py:23–29`).
   토큰에 actor를 적지 않으면 호출자가 모두 같은 자리표시 값(`configured-api-token`, 레거시 토큰은 `legacy-api-token`)이 된다.
   그러므로 **지금 구조에서는 두 사람이 승인했는지 판별할 수 없다.** 판별하려면 actor가 서로 달라야 하고 자리표시 값이 아니어야
   한다. 보호 환경은 토큰마다 actor를 요구하지만(374–378) 로컬·개발 환경은 요구하지 않는다.

이미 있는 탐지 근거:
- `human_review_coverage.approved_without_review_chunk_count`(`routes_documents.py:2978–2983`)
- 청크별 `review_attention_reasons`(`review_workflow_service.py:521`)
- AI 판단 수(`ai_action_required`, `ai_skipped`)와 해결 여부(`approval_governance.py:112–130`).
  다만 이 값들은 클라이언트가 보낸 이벤트 값이다. 서버는 형식만 정리하고(188, 195–211) 내용은 그대로 둔다.
  타임스탬프와 `source_of_truth`도 같다. 그래서 서버 판정 근거로 쓸 수 없다.
- 감사 도구 `scripts/audit_mcp_product_readiness.py:1137–1194`

이것들을 결합하면 "위험 신호가 있는데 미검수로 승인된 청크"를 사후에 셀 수 있다. 다만 "확인을 누르기는 했지만
실제로는 비교하지 않은" 경우는 지금 데이터로 구분할 수 없다.

## 3. 위험 신호

### 3.1 지금 바로 쓸 수 있는 신호 (청크 단위)

| 신호 | 근거 |
| --- | --- |
| `chunk_review_attention_reasons` 결과 전체: 대체문자, 원문 쪽 불명, `review_required`·`table_review_required`·`manual_review_required`, `review_flags`·`row_quality_flags`, 파서 불확실성 medium 이상, 표·OCR·인코딩 관련 경고 | `review_workflow_service.py:148–203`, 105–129 |
| `parser_uncertainty_risk_level` high·critical (OCR 필요 보고서의 기본값은 high) | `app/parsers/base.py:57–68`, 88–95 |
| Kordoc 표 승격·매칭 검토 플래그 | `routes_rag.py:2454–2461` |
| AI 판단 "수정 필요"(`reflect`)와 해결 여부, 본문 수정 여부. **클라이언트 보고값이라 서버 판정에는 쓸 수 없고**, 서버에 저장된 AI 검수 결과로 다시 계산할 수 있을 때만 쓴다 | `streamlit_app.py:1129–1155`, `approval_governance.py:121–124, 195–211` |
| `Chunk.confidence`(파서가 0.58–0.95 범위로 지정) | `app/schemas/chunk.py:34`, `app/parsers/hwp_parser.py:197`, `app/processors/chunker.py:482` |
| 오프라인 분류기의 등급 `blocking_review / domain_attention / informational`(시행일·부칙, 별표·서식, 표 맥락 포함) | `scripts/analyze_regulation_corpus.py:784–873`, 990–1005, 1155–1178 |

### 3.2 문서 단위로만 있거나 확인이 안 된 신호

- **OCR 단독 결과**: `ocr_review_required`는 PDF 블록 메타데이터에만 확인된다(`app/parsers/pdf_parser.py:403`, 419).
  chunker에서 이를 옮겨 받는 코드는 찾지 못했다. 문서 단위 `ocr_page_numbers`는 있다(321–323).
  청크까지 전달되는지는 **확인되지 않았으므로** 전달 경로를 추가하고 테스트해야 한다.
- **품질 점수**: `QualityReport.score`는 문서 단위다(`app/schemas/quality.py:22–26`, 계산 `quality_gate.py:995–1022`).
  청크 위험으로 바로 쓸 수 없고, 문서 전체 등급 하한으로만 쓸 수 있다.
- **배치 manifest의 `review_priority_tier`**: fingerprint에는 포함된다(`review_workflow_service.py:995`).
  하지만 manifest는 클라이언트 쪽 스크립트가 만든 산출물이라 **서버 판정 근거로 신뢰하면 안 된다.**

### 3.3 새로 만들어야 할 신호

- 숫자·날짜·금액 밀도. 현재 탐지 코드가 없고, 시행일 키워드 정규식만 있다(`analyze_regulation_corpus.py:837–839`).
- 조항별 확인 시각과 원문 보기 상호작용 기록.
- 전처리본과 최종본의 차이 크기.
- 청크 단위 OCR 출처 표시.

## 4. 등급과 요구 사항

등급은 서버가 청크 내용과 메타데이터로 **결정적으로** 계산한다. 계산 중 오류가 나면 high로 처리한다.

| 등급 | 조건(초안) | 요구 사항 |
| --- | --- | --- |
| **low** | 3.1의 신호 없음, confidence 0.9 이상 | 현행 유지. 미검수 승인을 허용하되 `approved_without_review`로 기록한다. 승인 후 **표본 재검수** 대상이 된다. |
| **elevated** | 주의 사유 있음(high 조건 제외), 시행일·부칙·별표·표 맥락, 숫자·날짜 밀도 높음, confidence 0.9 미만 | 해당 청크에 **원문 대조 확인 이벤트가 필수**다. 이벤트는 현재 `review_content_hash`에 묶여야 한다. 기본 미검수 사유로는 통과할 수 없다. 우회하려면 admin이 사람이 쓴 사유를 남겨야 하며, 이 경우 등급이 높은 표본 비율로 사후 재검수한다. |
| **high** | 파서 불확실성 high·critical, OCR 출처, 표 추출 실패·`table_review_required`, 대체문자, 서버에 저장된 AI 검수 결과 기준으로 해결되지 않은 `reflect` | **우회 불가.** 청크별 확인이 필수이고, actor가 묶이는 인증 모드에서는 **서로 다른 두 사람**의 확인이 필요하다. 로컬 단일 운영자 모드는 9절 결정 1을 따른다. |

각 요구 사항을 둔 이유:
- **원문 대조 확인을 해시에 묶는다.** 다른 내용을 보고 누른 확인이 재사용되는 것을 막는다. 화면 세션의 signature(`streamlit_app.py:1159–1172`, 1217–1220)는 AI 항목 ID와 AI 판단만 해시하고 청크 본문 해시는 포함하지 않는다. 그래서 본문이 바뀐 뒤에도 이전 확인이 유지될 수 있다. 본문 해시 결속은 서버 계약으로 새로 만드는 것이다.
- **체류 시간은 기록만 하고 차단 조건으로 쓰지 않는다.** 클라이언트가 보낸 시각은 위조할 수 있다. 차단에 쓰면 사용자가 시간만 채우고 기다리는 행동을 하게 된다. 사후 감사 지표(예: 조항당 확인 간격의 중앙값)로만 쓴다.
- **diff 화면 상호작용은 "열람 이벤트"로만 기록한다.** 열어 본 사실이 대조했다는 증명은 아니라는 점을 문서에 남긴다.
- **표본 재검수**는 자동화 편향을 측정하는 유일한 객관 지표다. low·elevated 승인 청크 중 일정 비율을 다른 사람이 다시 보게 하고, 불일치가 나오면 기존 반려 흐름(`prepare_rejection_update`, `review_workflow_service.py:600–638`)으로 처리한다. 자동으로 색인에서 빼지는 않는다.
- **2인 승인은 high 등급에만 둔다.** 전체에 적용하면 초보자 흐름이 막히고, 결국 우회 사유를 남발하게 된다.

## 5. 강제 위치 (서버)

UI 차단만으로는 부족하다. API를 직접 호출하면 우회되기 때문이다. 그래서 등급 판정과 차단은 서버에서 한다.

1. **새 순수 함수** `app/services/approval_risk_policy.py`
   - `classify_chunk_approval_risk(chunk) -> (tier, signal_keys)`를 둔다.
   - `analyze_regulation_corpus.py`의 분류 규칙 중 필요한 부분을 `app/`으로 옮기고, 스크립트는 이 함수를 호출하게 한다(역방향 의존 방지).
   - 이 작업이 전처리 회귀 기준선에 영향을 주면 `docs/preprocessing_change_governance_ko.md` 절차를 따른다.
2. **`validate_approval_preconditions`**(`review_workflow_service.py:227`)에 다음 인자를 추가한다.
   - `review_decision_events`, `actor`, `actor_is_bound`, `role`, 정책 버전
   - 등급별 요구 사항을 위반하면 `ReviewWorkflowError`를 낸다.
   - 263행의 "override 사유가 있으면 플래그 확인 생략" 규칙은 low 등급에만 적용한다.
3. **`prepare_approval_decision`**(422–480)이 이 인자를 그대로 넘긴다.
   - 지금은 라우트가 저장(`routes_documents.py:2893`) **이후**에 이벤트를 정리한다(2921–2961).
   - 이벤트 검증을 서비스 단계, 즉 저장 **이전**으로 옮겨야 차단이 원자적으로 동작한다.
   - 사전 검증은 `prepare_approval_decision` 안(`review_workflow_service.py:450`)에서 라우트의 2836행 호출로 실행되고,
     이벤트 정규화(2922–2936)는 그 뒤에 일어난다. 게이트가 이벤트를 입력으로 받으려면 정규화도 2836행 호출 이전으로 옮겨야 한다.
     이때 타임스탬프는 서버가 찍는다(지금은 클라이언트 값이 그대로 남는다).
4. **2인 승인**: 청크 상태 Literal(`chunk.py:9`)에 새 상태를 추가하지 않는다. 대신 다음처럼 한다.
   - 첫 번째 확인은 새 엔드포인트 `POST /{document_id}/review/second-review-request`가 review journal(`append_review_record`, `routes_documents.py:2673`과 같은 방식)에 `first_review_confirmed`로 남긴다.
   - 승인 요청 시 같은 테넌트, 같은 `review_content_hash`, **다른 actor**의 첫 확인이 있어야 high 청크를 승인할 수 있다.
5. **테넌트와 actor 바인딩 유지**
   - actor는 반드시 `auth.actor`로 덮어쓴다(`routes_documents.py:2928`).
   - 테넌트는 `settings_for_tenant(settings, auth.tenant_id)`(2707)로 정한 저장소에서만 읽는다.
   - "다른 사람" 판정은 두 확인의 actor가 서로 다르고 자리표시 값(`configured-api-token`, `legacy-api-token`,
     `local-anonymous`, `streamlit-local-operator`)이 아닐 때만 인정한다. `AuthContext`에는 `actor_is_bound`가 없으므로
     (`security_primitives.py:23–29`), 토큰에 actor가 명시됐는지 서버가 알 수 있게 필드를 추가하거나 자리표시 값을 거부해야 한다.
   - 서버 판정은 manifest의 `review_priority_tier`를 쓰지 않고, 현재 저장소의 청크로 다시 계산한다.
6. **색인 재검증**: `approval_validation.has_matching_approval_journal_record`(162–185)에 "journal의 등급 요구 사항이 충족되었는지"를 추가한다. 우회 경로로 들어온 레코드는 공식 색인에서 거부한다.
7. **두 번째 호출 경로**: `scripts/apply_reapproval_plan_shadow.py:825`가 `prepare_approval_decision`을 직접 호출하고
   `review_flags_acknowledged=True`를 고정으로 넘긴다. 게이트를 서비스 함수에 두면 이 경로에도 적용된다.
   재승인 도구가 high 등급을 어떻게 다룰지(별도 승인 증거를 요구할지, 차단할지)를 함께 정해야 한다.

## 6. 감사 기록과 출력 변경

**journal 변경**
- 청크 스냅샷(`review_workflow_service.py:514–524`)에 다음 필드를 추가한다.
  - `approval_risk_tier`
  - `approval_risk_signals`: 안정된 키만 넣고 본문은 넣지 않는다.
  - `review_level`: `two_person | human_confirmed | admin_override | approved_without_review | legacy_unknown`
  - `risk_policy_version`
- 레코드 단위로 `approval_risk_policy`(버전, 등급별 수)를 남긴다.

**이벤트 추가** (`approval_governance.py:11–17`의 허용 목록과 정규화 함수 178–218에 함께 등록)
- `source_comparison_acknowledged`: `review_content_hash`, 조항별 확인 시각
- `source_view_opened`
- `first_review_confirmed`, `second_review_confirmed`
- 차단 결과는 기존 `audit_api_event` 실패 기록으로 남는다(`routes_documents.py:2570–2579`와 같은 방식).

**해시 주의**
- `approved_content_hash`는 청크 메타데이터를 포함한다(`review_decision_service.py:28–44`).
- `review_level`을 메타데이터에 넣으려면 worklist 키처럼 해시 제외 목록(9–19)에 추가해야 한다.
- 대신 위변조는 journal 대조(5절의 6)로 막는다. 해시에 포함할지는 9절 결정 4에서 정한다.

**MCP·챗봇 노출**
- 현재 인용 메타데이터에 있는 것: `approval_id`, `approved_content_hash`, `approval_status`, 배치 증거
  (`app/mcp_server/regulation_tools.py:5351–5397`, 데이터 fetch 키 440–459, RAG 응답 `routes_rag.py:2425–2467`).
- 확인한 범위(5351–5418)에는 검수 수준과 `approved_by`가 없다. 다만 `approved_by`는 벡터 메타데이터 허용 목록에 있다(`app/ingestion/vector_adapter.py:192`).
- 제안: `approval_review_level` **열거값 하나만** 노출한다. 승인자 이름, override 사유, 위험 신호 원문은 노출하지 않는다.
  사유는 자유 텍스트라 실명이나 기관 식별자가 섞일 수 있기 때문이다.
- 외부 AI 지침에 "미검수 승인 조항을 인용할 때는 원문 확인을 권고한다"를 추가한다.
- 검색 대상에서 빼지는 않는다(9절 결정 5).

## 7. 하위 호환과 이전

- 이미 승인된 청크는 **재승인을 요구하지 않고, 색인에서 빼지도 않는다.** 등급도 몰래 다시 매기지 않는다.
- `review_level`이 없는 레코드는 다음처럼 읽기 시점에 파생한다.
  - `human_review_coverage.status == complete`이면 `human_confirmed(legacy)`
  - `approved_without_review` 이벤트가 있으면 `approved_without_review`
  - 둘 다 없으면 `legacy_unknown`
- 새 스크립트는 **보고서만** 만든다. "현재 기준으로 high인데 미검수 또는 legacy로 승인된 청크" 목록을 만들고, 기존 재승인 도구(`scripts/build_reapproval_worklist.py` 등)로 넘긴다. 실제 재검수는 운영자가 직접 시작한다.
- journal 보정이 필요하면 기존 backfill처럼 append-only 정정 레코드와 `supersedes_approval_record_ids`를 쓴다(`scripts/backfill_approval_review_events.py:123–143`).
  - 주의: 이 backfill은 `approved_by`를 실행 actor로 바꾼다(128–129).
  - 위험 정책 정정에서는 원래 승인자를 보존하고, 정정자를 별도 필드에 기록해야 한다.
- 정책은 `risk_policy_version`과 설정 플래그로 단계적으로 켠다. 순서는 관찰 모드(차단 없이 기록만) → elevated 강제 → high 강제다.

## 8. 테스트 계획

**기존 모듈 확장**
- `tests/test_review_workflow_service.py`: 등급별 차단, override가 low에만 적용되는지, 계산 오류 시 high 처리
- `tests/test_approval_governance.py`, `tests/test_approval_governance_invariants.py`: 새 이벤트의 정규화, 알 수 없는 필드 제거
- `tests/test_routes_documents.py`: 저장 전 차단(차단 시 청크와 journal이 바뀌지 않는지), actor 덮어쓰기
- `tests/test_api_tenant_isolation.py`: 다른 테넌트의 첫 확인이 2인 요건으로 인정되지 않는지
- `tests/test_official_rag_approval_gate_policy.py`, `tests/test_vector_upsert.py`: 요건 미충족 레코드의 색인 거부
- `tests/test_regulation_mcp_tools.py`, `tests/test_routes_rag.py`: `approval_review_level`만 노출되고 actor와 사유는 노출되지 않는지
- `tests/test_streamlit_approval_app.py`: 서버 거부 메시지 표시, 초보자 버튼이 해시에 묶인 이벤트를 만드는지
- `tests/test_backfill_approval_review_events.py`: legacy 파생값

**새 모듈**
- `tests/test_approval_risk_policy.py`: 분류 규칙, 스크립트 분류기와의 일치
- `tests/test_approval_two_person_gate.py`: 같은 actor, 바인딩 안 된 actor, 해시가 바뀐 경우
- `tests/test_approval_risk_migration_report.py`: 보고서만 만들고 상태는 바꾸지 않는지
- `tests/test_ocr_chunk_provenance.py`: OCR 출처가 청크까지 전달되는지

## 9. 사용자가 결정할 사항 (괄호 안은 권장 기본값)

1. **로컬 단일 운영자 모드에서 high 등급 처리** (권장: 차단하지 않는다. 청크별 확인을 필수로 하고, 우회는 금지하며, `review_level=human_confirmed(single)`로 표시한다. 2인 승인은 바인딩 토큰 모드에서만 강제한다.)
2. **elevated 등급 우회 허용 여부** (권장: admin이 직접 쓴 사유가 있을 때만 허용한다. 기본 문구는 거부한다.)
3. **표본 재검수 비율** (권장: low 2%, elevated 10%, 문서당 최소 1개. 파일럿 결과를 보고 조정한다.)
4. **`review_level`을 승인 해시에 포함할지** (권장: 포함하지 않는다. journal 대조로 검증한다. 포함하면 기존 승인 해시와 호환되지 않는다.)
5. **MCP·챗봇 노출 범위** (권장: 열거값만 노출하고 검색에서는 제외하지 않는다. 미검수 조항을 검색에서 빼는 안은 정책 변경이 커서 별도로 결정한다.)
6. **숫자·날짜 밀도 기준값** (권장: 관찰 모드에서 분포를 본 뒤 정한다. 그 전에는 elevated 판정에 쓰지 않는다.)
7. **시행 순서** (권장: 관찰 모드 2주 → elevated 강제 → high 강제)

**바꾸면 안 되는 것**
- 승인되지 않은 청크는 공식 vector/RAG/MCP에 들어가지 않는다. 판정 오류가 나면 실패로 닫는다(fail-closed).
- 자동 승인은 없다. 위험 등급은 차단을 **추가**하는 데만 쓰고, 승인 요건을 낮추는 데 쓰지 않는다.
- AI 제안은 최종본에 자동 반영하지 않는다(`approval_governance.py:156–175`는 메모로만 붙인다).
- 테넌트 격리, 승인 journal, 증거 해시 검증, 보안 스캔은 유지한다.
- 미검수 승인은 계속 `approved_without_review`로 정직하게 기록한다.

**남은 불확실성**
- 이 문서는 코드를 읽고 쓴 것이고 실행으로 확인하지 않았다. 특히 확인이 필요한 것:
  - OCR 메타데이터가 청크까지 전달되는지
  - 분류기를 옮겼을 때 결과가 같은지
  - 이벤트 검증을 저장 이전으로 옮겨도 재시도 경로(`routes_documents.py:2718–2784`)와 충돌하지 않는지

## 10. 정책과 별개로 먼저 고칠 기존 결함

위 정책을 도입하지 않더라도 독립 검토에서 확인된 결함이다. 등급 정책보다 변경이 작고 효과가 분명해서 먼저 별도 변경으로 처리했다.

1. **operator 역할이 override 사유로 플래그 확인을 건너뛴다. → 수정함.** 승인 API가 사유가 있으면 admin을 요구하도록
   병합 API와 맞췄다(`routes_documents.py`의 `approve_review_chunks`). 이 저장소의 호출부(Streamlit 로컬 승인, 기존 테스트)는
   모두 admin이라 영향이 없다. 외부 연동이 operator 토큰으로 사유를 쓰고 있었다면 이제 403을 받는다.
2. **면제받은 사유가 journal에 남지 않을 수 있다. → 수정함.** 모든 청크에 `human_review_confirmed`가 있어도
   `request.approval_override_reason`이 있으면 승인 레코드에 남긴다.
3. **클라이언트 이벤트의 시각과 부가 필드를 그대로 믿는다. → 아직 안 고침.** 정규화를 저장 이전으로 옮기고 타임스탬프는
   서버가 찍어야 한다(5절 3항). 이벤트 스키마와 journal 비교 테스트가 함께 바뀌어서 정책 도입 변경에서 다룬다.

1·2번 테스트(`tests/test_routes_documents.py`의 `test_approve_review_chunks_override_reason_requires_admin_role` 등 3개)는
수정 전 코드에서 실패하고 수정 후 통과하는 것을 확인했다.
