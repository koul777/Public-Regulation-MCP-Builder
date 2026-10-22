# 실제 규정·MCP·로컬 LLM 회귀 실행

`scripts/run_real_regulation_e2e.py`는 실제 PDF 재전처리, 기존 승인 색인의 검색, stdio MCP 조회, 로컬 API와 Ollama 응답을 하나의 비공개 실행 보고서로 수집한다. 문서를 자동 승인하지 않는다. 재전처리 결과는 검토 대기로 남기고, 조회에는 별도로 제공한 기존 승인 runtime의 복사본을 사용한다. 새 파싱 결과를 승인하거나 그 결과로 색인을 교체한 시험과는 구분한다.

## 입력 준비

원문, corpus manifest, query spec, 승인 runtime, 출력 디렉터리는 모두 공개 checkout 밖에 둔다. 절대 경로를 사용하고 링크·junction을 거치지 않는다. 이미 결과가 있는 출력 디렉터리는 다시 사용하지 않는다.

Corpus manifest의 `documents`에는 다음 항목이 필요하다.

- `document_id`: 승인 runtime 및 문항의 대상 문서 ID.
- `snapshot_pdf`: 비공개 PDF 사본의 절대 경로.
- `source_sha256`: 원본 바이트의 SHA-256.
- `human_review_complete`: 사람 검토가 실제로 완료됐을 때만 JSON `true`.

`label_provenance`에 정답 작성·검증 주체를 기록한다. 에이전트가 원문으로 작성한 문항을 사람 검증 정답이라고 표시하지 않는다. 같은 PDF가 다른 문서 ID로 존재하면 문서 ID 수와 고유 원본 해시 수를 따로 센다.

Query spec은 `queries` 배열이며 각 문항에 고유 `id`, `query`, `target_document_ids`, `target_chunk_ids`, `required_facts`, `allowed_citations`를 둔다. 근거가 없는 문항에는 `expect_no_evidence: true`와 `expected_abstain: true`를 지정한다. 승인 ID·청크 ID·원문·페이지를 가진 `allowed_citations`는 실제 응답에서 독립적으로 작성한다. 문서·조문군 단위로 개발/보류 평가셋을 나누고 최종 평가 전에는 보류셋을 개선에 사용하지 않는다.

각 실행은 tenant/profile 하나에 고정한다. 전체 `scope`, 문항 `scope`, `request_overrides.profile_id`도 이 범위와 일치해야 한다. 다른 범위를 시험하려면 별도의 실행과 보고서를 사용한다. 기존 runtime이 평면 저장인지 tenant별 저장인지 `tenant_storage_isolation`으로 명시한다.

### 개발/보류 평가셋 봉인

계획서 §4의 개발/보류 구분을 spec 라벨, 봉인 기록, 실행 검사로 강제함. 보류셋은 최종 후보를 정한 뒤 한 번만 실행함.

- query spec 최상위에 `"split": "development"` 또는 `"split": "holdout"`을 둠. 생략하면 `unspecified`로 처리함. 그 밖의 값(대소문자 차이 포함)은 `query_split_invalid`, 문항 단위 `split`은 `case_level_split_not_supported`로 거절함.
- 실행 보고서에 `query_split`이 기록됨.
- 기록 파일(ledger)은 공개 checkout 밖에 두는 추가 전용 JSONL임. 경로는 실행 설정의 `holdout_ledger`(절대 경로)로 지정하며, runtime·출력 디렉터리·입력 파일과 겹치면 `holdout_ledger_overlaps_run_paths`로 거절함.
- ledger 파일의 첫 기록이 무작위 `ledger_id`를 만들고, 이후 모든 기록이 같은 `ledger_id`를 가져야 함. 없거나 다르면 `holdout_ledger_corrupt`임. `opened` 기록, `report.json`의 `holdout_ledger_record`, 가드레일 결과의 `candidate.holdout_ledger_id`에 같은 값이 복사됨.
- 하드 링크가 둘 이상인 ledger는 `holdout_ledger_hard_linked`로 거절함. 읽을 때와 기록을 추가하기 직전에 확인함.

| ledger 이벤트 | 기록 주체 | 의미 |
| --- | --- | --- |
| `sealed` | 봉인 명령 | 개발/보류 spec이 겹치지 않음을 확인하고 두 spec의 SHA-256, 문항 수, 시각을 기록 |
| `opened` | 통합 실행기 | 보류셋 실행 시작. 첫 단계 전에 기록하므로 중단된 실행도 연 것으로 셈 |
| `rerun_authorized` | 사람이 직접 추가 | 해당 spec 해시의 가장 최근 기록일 때 한 번 더 열 수 있음 |

봉인은 두 spec이 확정된 뒤 한 번 실행함.

```powershell
python scripts/seal_real_regulation_holdout.py --development "<개발 spec 절대 경로>" --holdout "<보류 spec 절대 경로>" --ledger "<비공개 ledger 절대 경로>"
```

봉인 명령의 검사와 기록.

- 개발 spec은 `split: development`, 보류 spec은 `split: holdout`이어야 함. 아니면 `development_spec_split_mismatch`, `holdout_spec_split_mismatch`로 거절함.
- 두 spec이 같은 `id`, 같은 질문 문장, 같은 청크를 공유하면 거절함(`development_and_holdout_share_query_ids`, `development_and_holdout_share_query_text`, `development_and_holdout_share_target_chunk_ids`).
  - 질문 문장은 `query`와 `question`을 모두 보며, 유니코드 NFKC 정규화 뒤 공백·대소문자 차이를 무시하고 비교함.
  - 청크는 `target_chunk_id(s)`, `expected_chunk_id(s)`, `allowed_citations`의 `chunk_id`·`evidence_id(s)`를 비교함.
- 그 밖의 거절 코드: `seal_spec_has_no_cases`, `seal_spec_query_ids_invalid`, `development_and_holdout_must_be_different_files`, `holdout_ledger_overlaps_spec`.
- 같은 보류 spec 해시는 한 번만 봉인함(`holdout_already_sealed`). 열린 보류셋을 다시 봉인해 기록을 되돌릴 수 없음.
- ledger에는 해시, 문항 수, 시각, `ledger_id`만 씀. 질문, 답변, 문항 ID, 청크 ID, 경로는 쓰지 않음.
- 기존 ledger 줄이 손상됐으면 줄을 추가하지 않고 `holdout_ledger_corrupt`로 종료함.

`split: holdout` 실행의 규칙.

- `--check-only`를 포함해 단계 실행 전에 `holdout_ledger` 설정, 같은 spec 해시의 `sealed` 기록, `opened` 기록 없음을 확인함. 위반하면 아래 사유 코드 중 하나를 출력하고 종료 코드 2로 끝남.
- 실행기는 spec 파일을 한 번 읽어 그 바이트로 해시와 payload를 만들고 모든 단계에 같은 payload를 넘김. 검증 뒤 파일이 바뀌면 `opened` 기록 전에 `query_spec_changed_after_validation`으로 끝남. 실행 중 바뀌면 기존처럼 `input_integrity` 단계가 실패함.
- `opened` 기록 전에 설정한 Ollama 주소의 `/api/tags`가 응답하고 설정의 `model`이 목록에 있는지 확인함. 태그 없는 이름은 `:latest`로 봄. 실패하면 기록 없이 `ollama_preflight_unreachable` 또는 `ollama_preflight_model_missing`으로 끝나므로 보류셋이 소모되지 않음. 다른 역할 모델은 확인하지 않음.
- 실제 실행은 첫 단계 전에 `opened` 기록을 추가하고 같은 기록을 `report.json`의 `holdout_ledger_record`에 넣음. `--check-only`는 ledger에 쓰지 않음.
- 기록을 쓰지 못하면 `holdout_ledger_write_failed`로 단계 실행 없이 끝남. ledger 옆의 `.lock` 파일로 동시 실행을 막으며, 다른 실행이 잠금을 걸어 둔 상태면 기다리지 않고 `holdout_ledger_locked`로 거절함.
- 이미 열린 보류셋은 해당 해시의 가장 최근 기록이 `rerun_authorized`일 때만 한 번 더 열림. 새 `opened`가 뒤따르면 다시 막힘. 이 기록은 사람이 아래 형식으로 직접 한 줄 추가하며, 에이전트와 자동화는 쓰지 않음. `ledger_id`는 같은 ledger의 첫 줄(`sealed`)에서 복사함.

```json
{"event": "rerun_authorized", "ledger_id": "<ledger 첫 줄의 ledger_id>", "query_spec_sha256": "<보류 spec SHA-256>", "recorded_at": "<UTC 시각>", "reason": "<재실행 사유>"}
```

단계별 CLI는 보류 spec 파일을 받지 않음. `evaluate_mcp_retrieval_quality`, `real_regulation_mcp_probe`, `evaluate_live_local_llm`, `benchmark_mcp_queries`, `benchmark_mcp_concurrent_queries`, `benchmark_mcp_first_query`, `compare_rag_mcp_accuracy`, `export_mcp_demo_answers`, `evaluate_rag_retrieval`, `run_combined_book_e2e_regression`(`--ground-truth` 파일)은 `scripts/holdout_spec_guard.py`로 spec 파일을 검사하고, `split`이 `holdout`이면 `holdout_spec_requires_integrated_runner`로 거절함. 각 CLI는 `{"passed": false, "reason_code": "..."}`를 출력하고 종료 코드 2로 끝나며 단계는 시작하지 않음. `split`이 없거나 `development`인 spec은 그대로 처리함. 통합 실행기는 파싱된 문항을 같은 프로세스의 단계 함수에 넘기므로 이 검사를 거치지 않음.

| 사유 코드 | 발생 조건 |
| --- | --- |
| `query_split_invalid` | 최상위 `split`이 `development`·`holdout` 이외의 값 |
| `case_level_split_not_supported` | 문항 단위 `split` 키가 있음 |
| `holdout_ledger_required` | `split: holdout`인데 `holdout_ledger`가 설정되지 않음 |
| `holdout_ledger_path_invalid` | `holdout_ledger`가 비어 있거나 문자열 경로가 아님, 또는 가리키는 대상이 파일이 아님 |
| `holdout_ledger_overlaps_run_paths` | ledger 경로가 runtime·출력 디렉터리·query spec·corpus manifest와 겹침 |
| `holdout_ledger_missing` | ledger 파일이 없음 |
| `holdout_ledger_unreadable` | ledger 파일이 있으나 읽지 못함(권한, 입출력 오류) |
| `holdout_ledger_corrupt` | 줄 형식 오류, 알 수 없는 이벤트, 중복 키, 마지막 줄의 개행 누락, `ledger_id`가 없거나 서로 다름 |
| `holdout_ledger_hard_linked` | ledger 파일의 하드 링크가 둘 이상 |
| `holdout_ledger_locked` | 다른 실행이 ledger 잠금을 걸어 둔 상태 |
| `holdout_ledger_write_failed` | 잠금 파일을 열지 못했거나 기록 추가·동기화에 실패 |
| `holdout_not_sealed` | 이 spec 해시의 `sealed` 기록이 없음 |
| `holdout_already_opened` | 이미 열렸고 가장 최근 기록이 `rerun_authorized`가 아님 |
| `holdout_already_sealed` | 봉인 명령에서 같은 보류 spec 해시가 이미 ledger에 있음 |
| `query_spec_changed_after_validation` | 검증 뒤 단계 실행 전에 query spec 파일이 바뀜 |
| `ollama_preflight_unreachable` | `/api/tags`가 응답하지 않거나 상태 코드·본문이 올바르지 않음 |
| `ollama_preflight_model_missing` | 설정한 모델이 `/api/tags` 목록에 없음 |
| `holdout_spec_requires_integrated_runner` | 단계별 CLI에 `split: holdout` spec 파일을 넣음 |
| `holdout_ledger_id_unexpected` | KPI 검사기에서 고정한 `ledger_id`와 보고서의 `ledger_id`가 다르거나 없음 |
| `expected_holdout_ledger_id_invalid` | `--expected-holdout-ledger-id`가 소문자 16진수 32자가 아님 |

KPI 검사기의 `holdout_protocol`은 `query_split`이 `holdout`인 보고서에 `event`가 `opened`이고 `query_spec_sha256`이 보고서와 같으며 `ledger_id`가 있는 `holdout_ledger_record`가 있는지 확인함. 가드레일 결과의 `candidate`에 `query_split`, `holdout_ledger_id`, `authorized_rerun`이 기록되며, 재실행 승인으로 연 결과는 `authorized_rerun`이 `true`로 보임. 보류셋 보고서를 `--baseline`으로 쓰면 비교 불가 사유에 `baseline_is_holdout`이 추가되어 verdict가 `not_comparable`임.

검토자는 첫 봉인 때 봉인 명령이 출력한 `ledger_id`를 보고서와 별도로 보관하고, 최종 판정 때 `--expected-holdout-ledger-id`로 고정함. 고정값을 주면 그 ledger를 거친 `holdout` 보고서만 `holdout_protocol`을 통과하고, 다른 `ledger_id`의 보고서, `ledger_id`가 없는 보고서, 라벨이 `holdout`이 아닌 보고서는 `holdout_ledger_id_unexpected`로 실패함. 값은 소문자 16진수 32자여야 하며 아니면 `expected_holdout_ledger_id_invalid`로 종료 코드 2임. 고정값은 결과의 `candidate.expected_holdout_ledger_id`에 기록됨.

```powershell
python scripts/check_real_regulation_kpi_guardrails.py --report "$env:REG_RAG_EVAL_OUTPUT\report.json" --expected-holdout-ledger-id "<첫 봉인의 ledger_id>" --output "<새 비공개 경로>\kpi-guardrails.json"
```

한계.

- 새 ledger 파일은 새 이력으로 시작함. 같은 보류 spec을 새 ledger 파일에 다시 봉인하면 `opened` 기록이 없는 처음 상태로 보임. 보류 spec마다 ledger 하나만 유지하고, 검토자는 보고서와 가드레일 결과의 `ledger_id`가 첫 봉인의 `ledger_id`와 같은지(`--expected-holdout-ledger-id`), 해당 보류 spec 해시의 `opened` 기록이 한 번(`rerun_authorized`가 있으면 승인 횟수만큼)인지 비교해야 함.
- ledger는 사람이 편집할 수 있는 파일이라 변조 방지가 아님. 실수로 반복 실행하는 일을 막고 열린 이력을 남기는 장치임.
- 겹침 검사는 봉인 시점의 두 spec만 비교함. 봉인 뒤 개발 spec에 보류 문항을 옮겨 넣는 경우는 검사하지 않음.
- 해시는 spec 파일 바이트 기준임. 보류 spec을 한 글자라도 고치면 새 해시가 되어 다시 봉인해야 함. 보류 문항 사본의 라벨만 `development`로 바꾸는 우회는 사람이 검토해야 함.

## 통합 실행

비공개 JSON 설정에 아래 값을 채운다. 경로·tenant·profile 값은 자신의 비공개 입력으로 바꾼다.

```json
{
  "runtime_source": "<approved runtime absolute directory>",
  "corpus_manifest": "<private corpus manifest absolute path>",
  "query_spec": "<private query spec absolute path>",
  "tenant_id": "<runtime tenant>",
  "profile_id": "<runtime profile>",
  "tenant_storage_isolation": false,
  "model": "qwen3:8b",
  "ollama_endpoint": "http://127.0.0.1:11434",
  "api_role": "operator",
  "security_levels": ["internal"],
  "timeout_seconds": 600,
  "enable_kordoc_table_parser": true,
  "request_defaults": {
    "orchestration_mode": "multi_model",
    "retrieval_mode": "auto",
    "claim_audit_mode": "model"
  }
}
```

모델과 선택한 표 파서가 로컬에 설치되어 있어야 한다. 모델 다운로드·학습은 이 실행기가 수행하지 않는다. 통합 실행의 `api_role`은 모든 검색 단계와 같은 기존 `operator`로 고정한다. 별도의 viewer 거절 시험은 개별 live helper로 실행한다. 설정의 `security_levels`는 기본 `["internal"]`이며 `["public"]` 또는 `["public", "internal"]`로 지정할 수 있다. 이 필터를 직접 검색·stdio·API에 동일하게 전달한다. 단계마다 다르게 적용될 수 있는 문항별 문서·보안등급·부서·top-k override는 거부한다. 실행기는 임시 인증 토큰을 가진 loopback API를 시작하고 종료 시 자신이 시작한 프로세스를 정리한다.

설정의 `timeout_seconds`는 회귀 요청·관측기의 대기 한도다. 다중 모델 경로의 답변·감사 역할에는 별도의 모델 프로필 한도가 적용되므로, 이 값을 늘린 것만으로 역할 timeout이 늘어나지 않는다. 역할 timeout 뒤 Ollama가 생성을 완료하더라도 최종 API 답변 성공으로 계산하지 않는다.

현재 답변 역할은 컨텍스트 창 16,384토큰·대기 180초, 주장 감사 역할은 별도 프로필로 16,384토큰·45초를 사용한다. 구조·표 검토 역할의 설정은 독립적이다. 긴 근거와 출력 예산을 함께 확인하고, 대화 이력까지 추가한 입력이 항상 이 창에 들어간다고 가정하지 않는다. 컨텍스트 창을 키우면 메모리 사용과 CPU로 넘기는 연산이 증가할 수 있으므로 실제 생성의 `prompt_eval_count`, 모델 적재 상태, 대기 시간과 대체 응답 여부를 함께 기록한다.

Qwen 재순위 모델은 최종 토큰의 점수만 계산하고 KV 캐시를 만들지 않는다. AVX2 CPU이면서 네이티브 BF16과 AMX를 지원하지 않는다고 확인된 환경에서만 FP32로 불러온다. 그 밖의 장치나 기능 확인 실패 시 기존 dtype을 유지한다. FP32는 BF16보다 가중치 메모리를 약 두 배 사용하며, 점수의 미세한 차이와 후보 순위는 같은 입력으로 확인해야 한다.

API의 CPU 재순위 계산은 요청별 자식 프로세스에서 실행한다. API에는 가벼운 설정 객체만 캐시하고, 자식이 종료된 뒤 답변 모델을 호출하므로 재순위 가중치와 프레임워크 메모리를 계속 보유하지 않는다. 매번 모델을 불러오는 비용이 있으므로 속도 개선으로 가정하지 않는다. 기본 worker 한도는 300초이며, 입력·출력 크기, 점수 개수와 유한값, 종료 코드를 검사한다. 원문과 질문은 명령행 대신 표준 입력으로 전달하고 worker 오류는 고정 사유 코드로 처리한다. 재순위 실패 시 기존 검색 결과와 `reranker_status=degraded`가 유지되므로 이를 정상 재순위 완료와 구분한다. Windows의 commit 여유 감시는 해당 worker만 중단하는 최선 노력 검사이며 시스템 전체 메모리 안전을 보장하지 않는다.

저장소 루트에서 환경 변수에 비공개 설정과 새 출력 경로를 넣고 실행한다.

```powershell
python scripts/run_real_regulation_e2e.py --config "$env:REG_RAG_EVAL_CONFIG" --output-dir "$env:REG_RAG_EVAL_OUTPUT" --check-only
python scripts/run_real_regulation_e2e.py --config "$env:REG_RAG_EVAL_CONFIG" --output-dir "$env:REG_RAG_EVAL_OUTPUT"
```

`--check-only`는 설정과 원본 해시만 확인한다. 전처리·검색·MCP·모델 실행 성공을 뜻하지 않는다. 실제 실행은 문항의 대상 PDF 전체를 재전처리한다. 대형 통합 규정집은 시간이 걸릴 수 있다. 개별 corpus probe의 기본 표본 선택과 달리, 관련 없는 작은 PDF만 처리하고 문항의 원본을 검증한 것으로 집계하지 않는다.

### 대표 문항 반복 측정과 cold/warm 구분

계획서 §6의 "대표 문항 3회 반복, 결과 변동과 p50/p95 기록"을 위한 선택 설정임. 실행 설정에 `core_repeat`를 넣으면 지정한 문항을 본 실행이 끝난 뒤 추가로 실행함. 키가 없으면 반복하지 않음. 키가 있어도 `report.json`의 `configuration`은 바뀌지 않음.

```json
{
  "core_repeat": {
    "case_ids": ["<query spec 문항 ID 1>", "<query spec 문항 ID 2>"],
    "extra_runs": 2
  }
}
```

`extra_runs`는 본 실행에 더하는 횟수임. 2이면 문항마다 총 3회 실행함. `--check-only`도 같은 검사를 하며 위반하면 아래 코드로 종료 코드 2를 냄.

| 항목 | 규칙 | 오류 코드 |
| --- | --- | --- |
| `core_repeat` | JSON 객체. 설정에 `null`을 쓰면 키가 없는 것으로 처리 | `core_repeat_must_be_object` |
| 필드 | `case_ids`, `extra_runs`만 허용 | `core_repeat_unknown_field` |
| `case_ids` | 비어 있지 않은 문자열 목록, 빈 문자열·중복 없음. 기록할 때는 정렬함 | `core_repeat_case_ids_invalid` |
| `case_ids` | 모든 값이 query spec 문항 `id`에 있음 | `core_repeat_case_not_in_query_spec` |
| `extra_runs` | 1~4 정수. 불리언·문자열·소수 불가 | `core_repeat_extra_runs_invalid` |

실행 방식.

- 본 실행이 중단 없이 끝난 뒤에만 반복함. 문항을 query spec 순서로 한 번에 하나씩, 1회차 전체, 2회차 전체 순서로 실행하고 본 실행과 같은 `score_case`로 채점함.
- 반복 결과는 `live_llm` 단계 결과의 `repetitions`에만 있음. `cases`, `aggregate`, `not_run_count`, `passed`에는 들어가지 않으므로 문항 ID 집합 검사와 coverage는 본 실행 기준 그대로임. 반복 문항이 실패해도 `passed`는 바뀌지 않으며, 변동은 `repetition_summary`와 가드레일의 `stability`에서 확인함.
- 비공개 산출물 이름은 `repeat-<문항 순번>-r<회차>-<문항 ID 해시 앞 12자>.json`임. 본 실행의 `case-*.json`과 겹치지 않고, 문항 ID는 파일 이름에 들어가지 않음. 결과 폴더에 `repeat-*.json`이 이미 있으면 폴더 재사용을 거절함.
- 반복 중 전송 오류가 나거나 서버 오류(HTTP 5xx)가 연속 2회 나면 남은 반복을 멈추고 `repetitions_interrupted_reason`에 `transport_or_observer_failure` 또는 `consecutive_server_failures`를 기록함. 연속 횟수는 본 실행에서 이어서 셈. 본 실행이 중단됐으면 반복하지 않고 `main_pass_interrupted`를 기록함. 미실행 수는 `repetitions_not_run_count`임.
- 반복 단계에서 예기치 않은 예외가 나면(예: `repeat-*.json` 쓰기 실패) 사유를 `repeat_phase_error`로, 예외 형식 이름을 `repetitions_error_type`에 기록함. 예외 메시지는 기록하지 않음. 이 경우에도 본 실행 결과는 `live-local-llm-report.json`에 그대로 저장됨.
- 반복 계획은 `configuration`이 아니라 `report.json` 최상위 `core_repeat`와 `live_llm` 결과의 `repetition_plan`에 문항 ID를 정렬해 기록함. `configuration`은 기준선 비교 조건이므로 반복 요청 여부가 비교 가능성에 영향을 주지 않음. 반복이 있는 실행과 없는 실행도 비교할 수 있으며, 비교에는 양쪽의 본 실행 문항만 쓰임.

`repetition_summary`(대표 문항 단위).

| 필드 | 내용 |
| --- | --- |
| `case_ids`, `planned_extra_runs`, `planned_runs_per_case` | 요청한 반복 계획. `case_ids`는 정렬된 목록 |
| `core_run_count`, `expected_core_run_count` | 끝난 대표 문항 실행 수(본 실행 포함)와 계획 수 |
| `cases[].run_count`, `complete` | 문항별 실행 수와 계획 수 충족 여부 |
| `cases[].statuses`, `all_passed` | 실행 순서별 `passed`·`failed`와 전부 통과 여부 |
| `cases[].distinct_answer_digests` | 최종 답변 정규화 해시의 종류 수. 1이면 답변 문장이 매번 같음. 답변 본문은 보고서에 넣지 않음 |
| `cases[].runs_without_answer` | 답변이 없는 실행 수 |
| `cases[].latency_classes`, `duration_ms` | 실행 순서별 cold/warm 구분(답변 모델 기준)과 전체 대기 시간 |
| `p50_duration_ms`, `p95_duration_ms` | 모든 대표 문항 실행의 분위수 |
| `cold_run_count`, `warm_run_count`, `warm_p50_duration_ms`, `warm_p95_duration_ms` | cold/warm 구분 집계 |

분위수는 `ceil(n × q)`번째 값을 쓰는 순위 방식임. 문항당 3회 반복의 p95는 사실상 최댓값임.

cold/warm 구분. 문항 실행마다 `latency_class`가 기록되며, 판정은 답변 모델의 모델 적재 시간만 사용함. 답변 모델은 응답의 `orchestration.answer_model`이고, 없으면 최종 답변 문장과 같은 출력을 낸 단일 모델임. 알 수 없거나 둘 이상이면 `unclassified`임.

| 값 | 조건 |
| --- | --- |
| `cold` | 답변 모델의 완료된 생성 중 하나라도 Ollama `load_duration`이 1초(`COLD_LOAD_DURATION_NS` = 1,000,000,000 ns)를 넘음 |
| `warm` | 답변 모델의 완료된 생성이 1건 이상이고 모두 `load_duration`이 1초 이하임. 정확히 1초는 warm |
| `unclassified` | 답변 모델을 알 수 없음, 답변 모델의 완료된 생성이 없음(기권·차단·전송 오류·실패), 또는 적재 시간이 기록되지 않은 생성이 있음. warm으로 가정하지 않음 |

- 문항 결과의 `latency_info`에 모델별 적재 시간이 기록됨. `models`는 모델 이름마다 `completed_generation_count`, `load_reported_count`, `load_duration_ns_sum`(합), `load_duration_ns_max`(최댓값)를 가짐. 적재 시간이 보고되지 않은 모델의 합과 최댓값은 0이 아니라 `null`임. 답변 모델 이외의 적재 시간은 기록만 하고 `latency_class`를 바꾸지 않음.
- 다중 모델 경로는 문항마다 질문 분석(`qwen3:1.7b`), 답변(`qwen3:8b`), 주장 감사(`qwen3:4b`)를 호출함(`app/agents/model_router.py`의 현재 프로필). Ollama 요청의 `keep_alive`는 2분이므로(`app/agents/ollama_runtime.py`) 문항 간격이 길거나 메모리가 모자라 모델이 내려가면 다음 문항에서 다시 적재됨. 모델 세 개가 함께 상주하기 어려운 CPU 환경에서는 문항 대부분이 어떤 모델이든 다시 적재하는 것이 예상 범위임. 답변 모델 기준으로 판정해도 cold 문항이 많을 수 있으며, 그만큼 warm 문항이 적어 p95 비교는 `all_cases`로 대체됨.
- `aggregate`에 `cold_case_count`, `warm_case_count`, `unclassified_latency_case_count`(세 값의 합은 `case_count`), `warm_p50_duration_ms`, `warm_p95_duration_ms`가 추가됨. 기존 `p50_duration_ms`, `p95_duration_ms`는 모든 문항 기준 그대로임.
- 1초는 상주 모델과 디스크 적재를 구분하는 경험적 경계값이며 KPI 정책 파일의 기준이 아님. keep-alive 만료나 다른 프로그램의 메모리 압박으로 답변 모델이 다시 적재된 경우도 cold로 기록됨.
- 시간은 비스트리밍 요청의 전체 대기 시간임. 첫 토큰 시간은 측정하지 않으며 `timing_policy.first_token_latency_measured`는 `false`임. `latency_class`는 모델 적재 상태 표시이고 첫 토큰 지연이 아님.
- CPU/GPU 경합 중 측정한 값은 통제된 성능 개선 근거로 쓰지 않음. 반복은 변동 폭을 보여 주지만 경합을 없애지 못함. 같은 시간대에 다른 모델·빌드·테스트가 실행 중이었는지 실행 기록과 함께 확인한 뒤 해석함.

## 보고서 해석

검색 순위 계산에는 기존 검색용 텍스트를 사용하고, API·MCP의 답변 근거에는 검증된 청크 본문을 전달한다. `source_text_status`의 `verified_plain`은 래퍼가 없는 승인 레코드, `verified_reconstruction`은 저장된 본문과 검증된 메타데이터로 검색 문자열 전체를 정확히 재구성한 경우다. `unavailable`이면 인용 가능한 본문을 제공하지 않으며 검색용 문구로 대체하지 않는다. 이 상태는 저장 형식과 본문 사이의 일치 검사이며 PDF의 사람 검토 완료를 뜻하지 않는다. 기존 `content_hash`와 `approved_content_hash`는 해당 레코드·승인 identity를 유지하므로 응답 본문만 다시 해시한 값으로 해석하지 않는다.

주장 감사 모델은 주장의 의미가 근거에 의해 지지되는지 판정하고, 서버가 고정한 원문 구간 ID를 선택한다. 서버는 ID·근거 범위·문자열 위치를 다시 검사해 인용을 결합한다. 모델이 원문 띄어쓰기를 다시 작성할 필요를 줄이면서 정확 인용 검증을 유지하는 방식이다. 구간 선택 성공만으로 주장 지지를 통과시키지 않으며 모델의 미지원 판정, 알 수 없는 ID, 다른 근거의 ID와 합성 생략 표시는 거부한다.

표의 기존 검토 정보도 근거와 함께 전달한다. `table_review_required`가 JSON `true`이고 `table_review_flags`에 `unstable_column_count`가 있으면 해당 근거를 인용한 답변은 `table_structure_review_required` 사유로 기권한다. 빠른 검증과 추출형 fallback도 같은 제한을 지킨다. 위험 표가 검색 후보에 있다는 이유만으로 다른 정상 조문을 인용한 답변까지 막지는 않는다. 이 제한은 잘못 확정한 답변을 줄이는 조치이며 표 복원이나 정답률 개선으로 집계하지 않는다. 다른 검토 플래그도 보존하지만 모두 같은 기권 조건으로 확대하지 않는다.

native/full MCP의 표 메타데이터는 boolean·문자열 배열이다. ChatGPT data connector의 기존 `dict[str, str]` 계약에서는 boolean을 `"true"`/`"false"`, 배열을 JSON 문자열로 직렬화한다. 이 connector 출력을 내부 근거 객체에 전달할 때는 명시적으로 타입을 복원해야 한다. 일반 근거 처리기가 문자열 `"true"`를 boolean으로 암묵 변환하지 않는다.

MCP는 본문·표를 읽기 전후의 승인 저널, 색인, 범위 설정 및 관련 저장 파일 identity를 확인한다. 읽는 동안 권위 파일이 바뀌면 그 응답을 반환하지 않는다. 표 inventory와 셀 행은 벡터 텍스트 해시만으로 승인하지 않고 전체 청크 승인 권위를 추가 검증한다. 불변 runtime이 선언된 경우 그 검증 파일이 누락되거나 불일치해도 일반 저장소 조회로 우회하지 않는다. `verified_approved_content`는 이 별도 표 권위 검사 결과이며 일반 본문 재구성 상태와 구분한다.

`report.json`에는 전처리, 검색, MCP, 실제 LLM, 원본 입력 무결성과 실제 조회한 런타임 복사본 무결성의 결과가 따로 기록된다. 복사본의 승인·청크·색인·manifest·hierarchy 파일 변경도 실패로 처리한다. 정확히 선언한 API audit·RAG trace JSONL의 기존 바이트를 보존한 추가 기록과 잠금 파일 작성기가 만드는 정확한 초기 바이트만 허용하며, 폴더 전체를 예외로 두지 않는다. 필수 단계가 실패하거나 실행 중 소스·입력이 바뀌면 전체 성공으로 표시하지 않는다. 기술 검증 결과와 별개로 `product_release_ready`는 `false`이며, 사람의 원문 검토·보호 변경 검토를 대신하지 않는다.

실제 모델 호출은 Ollama 요청·응답 해시와 완료 토큰을 관측한다. 질문 분석 모델 호출만 성공한 경우, 최종 답변 모델 생성 성공과 구분한다. 최종 답변의 해시·`answer_mode`를 확인하고 모델 답변, 추출형 fallback, 기권, 전송 오류를 따로 집계한다. timeout 뒤 실행하지 못한 문항을 답변 품질 실패나 성공으로 계산하지 않는다.

API 프로세스가 연결을 끊으면 `api-process.json`의 PID와 정리 전 종료 코드를 `api-server.log` 및 운영체제 오류 기록과 대조한다. 실행기가 정상 종료를 위해 보낸 terminate/kill은 별도로 기록하므로 기존 네이티브 충돌과 혼동하지 않는다. Python fault handler의 출력도 비공개 서버 로그에만 남기며 인증 토큰이나 환경 변수 전체를 기록하지 않는다.

`required_facts` 평가는 고정 문자열·수치 앵커 비교이며 의미 정확도를 완전히 평가하지 않는다. 인용 검사는 승인 근거의 identity와 제공된 원문 인용을 확인한다. 조건·예외 누락과 주장에 대한 실제 지지는 별도 원문 검토가 필요하다. 비스트리밍 응답 전체 시간을 첫 토큰 시간으로 표시하지 않는다.

평가기의 최종 성공은 개별 문항의 `status`로 확인한다. `final_model_answer`는 관측된 생성과 최종 응답의 연결을 나타내며, 이것만으로 사실·인용 검사가 모두 통과한 것은 아니다. v4는 명시적인 감사 실패·검토 필요 상태, 인용 거절 상태, 답변의 근거 번호와 모델 감사 인용의 불일치를 실패로 처리한다. 기존 결정적 인용 계약에는 context ID와 해시가 없을 수 있으므로 해당 검사는 `not_available_by_contract`로 표시한다. 기존 응답에서 감사 상태 자체가 생략된 경우에도 모델 감사 성공을 증명하지 않는다. `verification_limitations`를 확인하고, 실행이 중단됐으면 집계에 사용한 실행 문항 수와 예정·미실행 문항 수를 함께 보고한다.

전후 비교에는 원본·승인 runtime·문항 해시, 소스 상태, 모델 digest, 생성 옵션, timeout, 검색·감사 모드를 함께 보존한다. `auto`와 `fast`, 모델 감사와 결정적 감사의 비교는 서로 다른 설정의 비교다. CPU/GPU 경합 중 측정값은 통제된 성능 개선 근거로 사용하지 않는다.

## KPI 가드레일

`scripts/check_real_regulation_kpi_guardrails.py`가 통합 실행의 `report.json`을 정책 파일 `scripts/policies/real_regulation_kpi_guardrails.v1.json`으로 판정함. `report.json`은 수정하지 않고 결과를 새 비공개 파일에 씀.

```powershell
python scripts/check_real_regulation_kpi_guardrails.py --report "$env:REG_RAG_EVAL_OUTPUT\report.json" --output "<새 비공개 경로>\kpi-guardrails.json"
python scripts/check_real_regulation_kpi_guardrails.py --report "$env:REG_RAG_EVAL_OUTPUT\report.json" --baseline "<기준선 실행 경로>\report.json" --output "<새 비공개 경로>\kpi-guardrails.json"
```

### 등급

| 등급 | 판정 | 항목 |
| --- | --- | --- |
| hard | 위반 1건 또는 근거 누락이면 실패 | 아래 10개 |
| target | 최소 문항 수를 채웠을 때 하한과 비교 | Recall@5 ≥ 0.95, MRR ≥ 0.85, 최종 모델 답변률 ≥ 0.95, 사실 앵커 충족률 ≥ 0.90, 문항 단위 사실 충족률 ≥ 0.90 |
| regression | `--baseline`을 줬을 때만 | 차단: 검색 새 누락 0건, MCP 새 실패 0건. 참고: LLM 문항 새 실패, LLM p95 시간 비율 ≤ 1.10 |

| hard ID | 확인 내용 |
| --- | --- |
| `coverage` | 필수 단계 6개 존재, 전처리 통과, LLM·검색·MCP 문항 ID가 중복 없이 `query_count`와 같고 세 단계가 같은 ID 집합, 미실행·중단·검색 오류 없음 |
| `integrity` | 입력 무결성·실행 runtime 무결성 통과, 소스 상태 안정 |
| `no_auto_approval` | `human_approval_created`, `product_release_ready` 모두 `false` |
| `mcp_contract` | MCP 단계 통과, protocol 검사 전부 통과, 모든 문항 통과, 오류 없음 |
| `request_modes` | 모든 문항이 `llm_backend=ollama`, `claim_audit_mode=model`로 실행됨 |
| `retrieval_no_evidence` | 근거 없음 문항이 1개 이상이고 모두 검색 결과 0건 |
| `negative_cases` | 기권·차단 기대 문항이 1개 이상이고 모두 통과 |
| `forbidden_facts` | 어느 문항에서도 금지 사실 앵커가 나오지 않음 |
| `citation_trace` | 답변 가능 문항의 실질 답변마다 인용 1개 이상, 인용 신원·인용문 확인, 답변의 근거 번호 일치. 계약상 context ID·해시가 없는 대체 답변 인용(`deterministic_citation_omits_context_and_hash`, `content_hash_not_available_in_gold`)은 실패로 세지 않고 `limited_trace_case_ids`에 따로 표시. 그 밖의 제한 사유는 실패 |
| `holdout_protocol` | `query_split`이 `holdout`이면 `holdout_ledger_record`가 있고 `event`가 `opened`이며 `query_spec_sha256`이 보고서와 같고 `ledger_id`가 있음. `development`·`unspecified`는 통과, 그 밖의 값은 실패 |

`retrieval_no_evidence`와 `negative_cases`는 위반이 없어도 문항 수가 coverage profile 최소치보다 적으면 `insufficient_sample`. 위반이 있으면 문항 수와 상관없이 `failed`.

### 판정 규칙

- 항목 상태: `passed`, `failed`, `unavailable`, `insufficient_sample`, `not_evaluated`, `advisory_exceeded`.
- `verdict`: hard 실패·`unavailable`, target 실패, 차단 regression 실패 중 하나라도 있으면 `failed`. 실패 없이 표본 부족만 있으면 `incomplete`. 기준선과 입력·모델이 다르면 `not_comparable`.
- 종료 코드: `passed` 0, `failed`·`incomplete` 1, `not_comparable`·입력 오류 2.
- 비율 target은 `required_successes = ceil(하한 × 분모)`를 Decimal로 계산하고 분자, 분모, 허용 실패 건수를 함께 출력함. 20문항에서 0.95는 19건 이상.
- 평균 target(Recall@5, MRR)은 반올림 전 합계로 판정함. 출력의 `value`는 소수 6자리 표시값.
- 답변 가능 문항 중 사실 앵커가 없는 문항이 있으면 두 사실 target은 `unavailable`이고 해당 문항 ID를 `unlabeled_case_ids`에 출력함. 앵커 없는 문항을 허용 실패로 세지 않음.
- 최소 문항 수(coverage profile)

  | profile | 답변 가능 | 기권·차단 | 근거 없음 검색 | 최대 문항 수 |
  | --- | ---: | ---: | ---: | ---: |
  | `full` (기본) | 20 | 8 | 4 | 제한 없음 |
  | `compact` | 10 | 4 | 2 | 30 |

  30문항 축소셋은 `--coverage-profile compact`로 명시함. 31문항 이상 실행에 `compact`를 지정하면 입력 오류(종료 코드 2).
- 권한·tenant 경계의 거절(HTTP 403) 시험은 통합 실행이 아니라 개별 live helper로 실행하므로, 이 가드레일의 `negative_cases`는 기권과 차단 문항을 합쳐 셈.
- 기준선 비교 조건: report 형식, query spec 해시, corpus, runtime fingerprint, runtime 범위, `configuration` 전체, `label_provenance`, 실제 호출된 모든 모델의 Ollama digest, 기준선 자체의 `coverage` 통과(문항 ID 중복·누락 없음). 하나라도 다르거나 모델 호출 기록이 없으면 비교하지 않음.
- 기준선과 후보는 현재 정책 하나로 다시 판정함. 두 실행의 정책 해시 일치는 요구하지 않음.
- LLM 문항 새 실패와 p95 비율은 참고로만 보고하고 차단하지 않음. 정책 파일과 판정 규칙은 그대로임.
- p95 비율(`regressions`의 `llm_p95_duration_ratio`)은 두 보고서의 본 실행 문항으로 계산함. 모든 문항 비율은 항상 계산하고, 조건을 채우면 warm 문항 비율을 더해 둘 중 큰 값을 `value`로 씀. 느리거나 실패한 문항이 warm 표본에서 빠져 회귀가 가려지는 일을 막기 위함임.

  | 비율 | 대상 | 조건 |
  | --- | --- | --- |
  | `all_cases` | 각 보고서의 모든 LLM 문항. 시간 초과·5xx로 모델 호출이 끝나지 못한 문항 포함 | 항상 계산. 이전과 같음 |
  | `warm_only` | 두 보고서에서 모두 `warm`인 문항(문항 ID로 짝지음)의 시간 | 두 보고서의 모든 LLM 문항에 `latency_class`가 있고 ID가 중복되지 않으며, 짝지은 warm 문항이 `MIN_PAIRED_WARM_CASES`(10) 이상 |

  행의 `all_cases`, `warm_only` 블록에 비율, 양쪽 p95, 표본 수가 나오고 `warm_only.paired_case_count`와 `min_paired_case_count`로 짝지은 수를 확인함. `warm_only.evaluated`가 `false`이면 warm 비율을 쓰지 않은 것임. `basis`는 `value`를 만든 비율(`all_cases` 또는 `warm_only`, 같으면 `all_cases`)이고 행 최상위의 `baseline_p95_ms`, `candidate_p95_ms`, `baseline_sample_count`, `candidate_sample_count`도 그 비율의 값임. 이전 보고서의 `generation_events`에서 라벨을 다시 만들지 않으므로, 라벨이 없는 기준선과는 `all_cases`만으로 비교함. `repetitions`는 넣지 않음. 경합 통제와 차단 전환 기준이 정해지지 않았으므로 계속 참고용임.

### 기준 완화 방지

- 정책 파일은 `scripts/check_preprocessing_change_guard.py`의 `BASELINE_FILES`에 포함됨. 바꾸는 PR에는 기준값 변경 사유와 전후 수치, 대응 테스트, Code Owner 검토가 필요함.
- 검사기에는 정책 경로를 바꾸는 옵션이 없음. 정책에서 hard 검사, target, regression 항목을 빼거나 끄면 검증 실패로 종료 코드 2.
- `tests/test_real_regulation_kpi_guardrails.py`가 계획서 §6 하한을 별도 상수로 고정함. 정책과 이 테스트를 같은 PR에서 함께 낮추는 경우는 Code Owner 검토로 막음.
- 실제 기준선 report에는 로컬 경로와 문서 ID가 들어 있으므로 저장소에 두지 않고 `--baseline`으로 비공개 경로를 넘김.

### `report.json`의 `passed`와 관계

- 기존 `passed`는 LLM 문항 하나나 검색 하한 하나만 미달해도 `false`라서 target보다 엄격함. 가드레일 결과의 `candidate.legacy_report_passed`에 그대로 기록함.
- 통합 실행기의 검색 단계 하한(Recall@5, MRR)은 같은 정책 파일에서 읽고, `report.json`의 `kpi_policy`에 정책 ID·버전·해시를 기록함.

### 실패 원인 분류 (`failure_breakdown`)

계획서 [§7 측정 결과에 따른 개발 분기](real_regulation_mcp_local_llm_10hour_plan_ko.md)는 가장 많은 실패 원인을 기준으로 다음 수정 대상을 고름. 가드레일 결과의 `failure_breakdown`이 문항별 원인과 §7 분기를 미리 모아 줌.

- 진단용 출력임. `verdict`, `passed`, hard·target·regression 행, 종료 코드에 영향을 주지 않음. 분류 계산이 실패해도 판정은 그대로이고 이 항목만 `status: "error"`가 됨.
- 검색 결과, MCP 문항, LLM 문항을 문항 ID로 합쳐 어느 단계에서든 실패한 문항마다 주원인(`primary_cause`), 해당하는 모든 원인(`causes`), 원인별 코드(`codes`), §7 분기(`plan_branch`)를 기록함. 주원인은 아래 표의 위쪽 원인이 우선함.
- 이 분류는 기록된 필드에 규칙을 적용한 결과이며 원인 확정이 아님. 근거 코드는 `scripts/check_real_regulation_kpi_guardrails.py`의 `FAILURE_CAUSES`, `EXECUTION_CODE_BRANCHES`, `LLM_ERROR_CODES`에 있음. 테스트가 `score_case`가 낼 수 있는 오류 코드와 `LLM_ERROR_CODES`의 일치, 선언한 코드와 실제로 기록되는 코드의 일치를 확인함.

| 순위 | 원인 | 판정 근거 | §7 분기 |
| ---: | --- | --- | --- |
| 1 | `security_leak` | 기권·차단 기대 문항에서만 판정. 차단 기대 문항이 답변·인용을 내보내거나 모델을 호출하거나 2xx로 응답, 기권 기대 문항이 HTTP 200에서 답변 또는 인용을 내보냄, 금지 사실이 나옴. 코드: `blocked_case_exposed_answer_or_called_model`, `forbidden_fact_exposed`, `expected_clean_abstention`, `expected_client_rejection` | 차단 기대 문항은 승인 가시성·번들 결함. 기권 기대 문항은 함께 나온 원인의 분기를 따르고 (`retrieval_false_positive`와 함께면 검색 누락), 함께 나온 원인이 없으면 근거는 맞지만 답변이 틀림 (검색은 비었는데 답변·감사 단계가 통과시킨 경우) |
| 2 | `fabricated_citation` | 실질 답변의 인용 결함. 정의는 아래 목록 | 근거는 맞지만 답변이 틀림 (인용 불일치) |
| 3 | `critical_fact` | 답변 가능 문항의 답변에 금지 사실이 나옴. 코드: `forbidden_fact_exposed` | 근거는 맞지만 답변이 틀림 (수치·단위 변형) |
| 4 | `retrieval_miss` | 답변 가능 문항의 `recall_at_5`가 1.0 미만, 또는 MCP 문항이 대상 근거를 찾지 못함. 코드: `recall_at_5_below_1`, `mcp_target_evidence_not_retrieved` | 검색 누락 |
| 5 | `retrieval_false_positive` | 근거 없음 질의가 검색 또는 MCP에서 결과를 반환. 코드: `no_evidence_query_returned_results`, `mcp_unexpected_evidence`. 요청 목록에 없던 원인이며, 검색 단계의 실패라서 같은 분기에 둠 | 검색 누락 |
| 6 | `execution_error` | HTTP 상태 불일치, 전송 오류, 감사·인용 상태 누락, 검색 오류, 질의 명세 오류, MCP 검색·조회 실패. 코드와 분기는 아래 표 | 코드에 따라 지연, 승인 가시성·번들 결함, 근거는 맞지만 답변이 틀림 |
| 7 | `generation_fallback` | 답변 가능 문항에 검증된 최종 모델 답변이 없음 (대체 응답, 기권, 감사 미수락). 코드: `final_model_generation_not_verified`, `claim_audit_not_accepted`, `citation_verification_not_accepted`, `answer_mode_fallback`, `answer_abstained`, `final_model_answer_false`, `fallback_citation_without_context_or_hash` | 근거는 맞지만 답변이 틀림 |
| 8 | `answer_incomplete` | 검증된 최종 모델 답변이지만 필수 사실 일부가 빠짐. 코드: `gold_facts_failed` | 근거는 맞지만 답변이 틀림 (조건·예외 누락) |
| 9 | `unknown` | 단계 자료 누락 또는 분류 불가. 사유는 `unknown_reasons`에 기록 | 없음 |

`generation_fallback`의 분기 근거: `claim_audit_mode=model`에서는 답변·감사 단계가 `strict_model=True`로 실행되어 모델·백엔드 실패와 시간 초과가 대체 응답이 아니라 HTTP 503이 됨. 이 경우는 `execution_error`로 분류됨. 대체 응답(`claim_audit_status`가 `fallback_from_<감사 상태>`)과 기권은 감사가 모델 답변을 거절했을 때만 나오므로 답변 분기임.

`fabricated_citation`의 정의 (실질 답변, 즉 HTTP 200이고 답변이 있고 기권이 아닌 응답에서만 판정):

- `missing_citation`: 인용 수가 1 미만.
- `citation_not_verified`: 인용은 있으나 `citation_checks.all_verified`가 true가 아님.
- `answer_citation_context_not_verified`: 답변의 근거 번호(`[E1]` 등)와 인용의 context ID가 맞지 않음.
- `invalid_citations_shape`: `citations`가 목록이 아니거나 객체가 아닌 항목을 포함.
- `citation_verification_not_accepted`: 서버의 `citation_verification_status`가 `verified`가 아님. 상태 자체가 없으면 `execution_error`의 `citation_verification_status_missing`으로 분류함.
- `unknown_verification_limitation`: `verification_limitations`가 목록이 아니거나 `deterministic_citation_omits_context_and_hash`, `content_hash_not_available_in_gold` 밖의 값을 포함.
- `citation_trace` hard 검사와의 차이는 두 가지임. 하나는 `citation_verification_not_accepted`로, 분류에만 있고 hard 검사는 보지 않음. 다른 하나는 아래 대체 응답 인용으로, hard 검사는 위반으로 판정하고 분류는 `fabricated_citation`이 아닌 `generation_fallback`으로 기록함. 그 밖의 위반은 두 쪽이 일치하며 테스트가 확인함.

대체 응답의 인용: 실제 추출형 대체 응답의 인용(`CitationVerifierAgent`)은 구조상 `context_id`, 내용 해시(`content_hash`, `approved_content_hash`), 인용문(`support_quote`)이 없음. `score_case`는 context ID·해시 누락 면제를 빠른 결정적 경로(`deterministic_identity_verified`)에만 적용하므로, `fallback_from_*` 응답은 `citation_not_verified`와 `answer_citation_context_not_verified`를 기록하고 `citation_trace` hard 검사도 위반으로 판정함 (판정은 바꾸지 않음). 분류는 `fallback`이 true이고 인용 결함이 이 두 가지뿐일 때만 `fallback_citation_without_context_or_hash`(`generation_fallback`)로 기록하고, `fabricated_citation`과 `critical_case_ids`에서 제외함. `citation_not_verified`가 있으면 실패한 인용이 모두 `content_hash_status=missing`이고 인용문 검사가 `invalid`가 아니어야 하며(인용문이 없는 `not_provided`는 허용), `answer_citation_context_not_verified`가 있으면 `citation_context_ids`가 비어 있어야 함. 신원 불일치, 잘못된 인용문, 맞지 않는 context ID는 그대로 `fabricated_citation`임. hard 검사는 바꾸지 않았고 계속 엄격함. 한계: 해시 누락과 함께 페이지 범위가 어긋난 경우, 문항이 인용문을 필수로 요구해서(`require_support_quote`) 인용문 부재가 실패 원인인 경우는 `score_case`가 기록하지 않아 구분하지 못함.

`execution_error`의 분기 (표 위쪽 코드가 우선, 해당하는 코드가 없으면 승인 가시성·번들 결함):

| 코드 | 원천 | 분기 |
| --- | --- | --- |
| `llm_backend_failed` | 서버 메시지 `Local LLM backend request failed: <예외 이름>`, 또는 `Multi-model local QA execution failed: <예외 이름>`에서 예외 이름이 `TimeoutError`, `URLError`, `OllamaRuntimeError`, `ConnectionError`, `ConnectionResetError`, `ConnectionRefusedError`, `ConnectionAbortedError`, `BrokenPipeError`, `RemoteDisconnected`, `IncompleteRead` | 지연 |
| `llm_backend_unavailable` | `Configured local LLM backend is not available.` 또는 `Local LLM backend request failed: backend unavailable` | 지연 |
| `llm_transport_timeout` | 평가기 전송 오류가 `TimeoutError`, `ReadTimeout`, `ConnectTimeout` | 지연 |
| `llm_output_invalid` | `Multi-model local QA execution failed: <예외 이름>`에서 예외 이름이 `ValueError`, `ValidationError`, `JSONDecodeError` (모델 출력을 답변·감사 검사가 거절, 예: 모델이 알 수 없는 문맥 번호를 인용) | 근거는 맞지만 답변이 틀림 |
| `llm_runtime_failed` | `Multi-model local QA execution failed: <예외 이름>`에서 예외 이름이 `RuntimeError` (필요한 모델 미설치 등), `HTTPException` (빠른 경로의 내부 HTTP 오류) | 승인 가시성·번들 결함 |
| `evidence_identity_verification_failed` | `Final evidence identity verification failed` 또는 `Fast chat evidence identity verification failed` | 승인 가시성·번들 결함 |
| `citation_verification_failed` | `Answer citation verification failed` | 근거는 맞지만 답변이 틀림 |
| `claim_audit_fallback_unverified` | `Multi-model answer claim audit did not verify: ...` (감사 거절 뒤 대체 답변의 인용 검증 실패) | 근거는 맞지만 답변이 틀림 |
| 그 밖의 코드 | `unexpected_http_status`만 있는 경우, `llm_transport_error`(시간 초과 아님), 감사·인용 상태 누락, 검색·MCP 실행 오류, 질의 명세 오류, 빈 응답, 형식 오류 | 승인 가시성·번들 결함 |

- 서버 메시지 구분: `score_case`가 `http_error_code` 필드를 기록함. 알려진 서버 메시지와 정확히 일치하거나 알려진 접두어로 시작하면 위 코드(`llm_backend_failed`, `llm_backend_unavailable`, `llm_output_invalid`, `llm_runtime_failed`, `evidence_identity_verification_failed`, `citation_verification_failed`, `claim_audit_fallback_unverified`), 그 밖의 비 2xx 응답은 `other`, 2xx는 `null`. 저장하는 값은 고정 코드뿐이고 메시지 원문은 저장하지 않음. 테스트가 `routes_rag.py`의 모든 503 메시지가 분류되는지 확인함.
- `Multi-model local QA execution failed: <예외 이름>`은 모델 호출뿐 아니라 모델 출력 검사까지 감싸므로 예외 이름으로 코드를 정함. 읽는 값은 식별자 형태의 예외 이름뿐이고, 허용 목록 밖의 이름이나 식별자가 아닌 접미어는 `other`임. 지연 분기로 분류돼도 시간 초과 확정은 아님. `OllamaRuntimeError`는 Ollama의 HTTP 오류 응답(모델 적재 실패 등)도 포함함.
- `Local LLM backend request failed: <예외 이름>`(다중 모델 경로가 아닌 기존 답변 경로)은 모델 호출만 감싸므로 예외 이름과 무관하게 `llm_backend_failed`임.
- `Answer citation verification failed`는 비승인 근거가 들어와 검증기가 예외를 낸 경우와 인용 검증이 거절된 경우를 구분하지 못함.

출력 규칙:

- 필드: `counts`와 `case_ids`는 각각 `primary`(주원인 기준)와 `any`(해당 원인이 하나라도 있는 문항 기준)를 가짐. `plan_branch_counts`는 실패 문항의 문항별 `plan_branch` 건수임. `critical_case_ids`는 1~3순위 원인이 하나라도 있는 문항 ID의 합집합임.
- `dominant_cause`는 `unknown`을 제외한 원인 중 주원인 건수가 가장 많은 원인이고, 동수이면 표에서 위쪽 원인임. 분류하지 못한 문항 수는 `unknown_case_count`에 따로 셈. `unknown` 문항만 있으면 `dominant_cause`는 `null`임.
- `dominant_plan_branch`는 `plan_branch_counts`가 가장 큰 분기임. `unknown`을 제외한 분류된 모든 실패 문항을 세므로 `dominant_cause`의 분기와 다를 수 있음. 예: 대체 응답 4건과 `execution_error` 5건(지연 2, 승인 2, 답변 1)이면 `dominant_cause`는 `execution_error`이고 `dominant_plan_branch`는 근거는 맞지만 답변이 틀림(5건)임. 동수이면 승인 가시성·번들 결함, 근거는 맞지만 답변이 틀림, 검색 누락, 지연 순임. §7이 승인 가시성·번들 결함을 먼저 해결하라고 하므로 `plan_branch_counts`의 해당 값이 0이 아니면 최빈 분기와 별개로 먼저 확인함.
- 중단된 실행: `interrupted_reason`과 `not_run_count`를 LLM 단계 결과에서 그대로 복사함. 실행하지 못한 문항은 `unknown`(`llm_case_not_run`)임. `classification_complete`는 `unknown` 문항, 중단, 미실행이 모두 없을 때만 true임.
- 결과가 없는 HTTP·전송 오류 문항에는 답변에서 파생되는 원인(인용, 대체 응답, 사실 미충족)을 기록하지 않음. 최종 모델 답변이 아닌 문항의 사실 미충족은 `answer_incomplete`가 아니라 `generation_fallback`으로만 셈.
- 단계 전체 또는 해당 문항의 자료가 없으면 `unknown`과 사유(`retrieval_stage_missing`, `mcp_case_missing`, `mcp_case_failed_unclassified`, `llm_case_not_run`, `retrieval_recall_not_recorded`, `gold_facts_unlabeled`, `unmapped_error:<코드>` 등)를 기록함. `mcp_case_failed_unclassified`는 MCP 문항이 `passed=false`이지만 `search_protocol_passed`와 `retrieval_passed`의 기록으로 원인을 판별할 수 없는 경우(필드 누락 등)임. 원인을 추정하지 않음. 다른 원인이 이미 있는 문항에서는 사유만 `unknown_reasons`에 남고 주원인은 바뀌지 않음.
- §7의 첫 분기(원문 손실·경계 오류)는 이 분류로 판별하지 못함. `retrieval_miss`와 `answer_incomplete` 문항은 원문과 청크 경계를 직접 확인해야 함.
- CLI 표준 출력에는 `failure_breakdown.dominant_cause`와 원인별 주원인 건수만 나옴. 문항 ID와 코드는 `--output` 파일에만 있음.

### 반복 안정성 (`stability`)

`core_repeat`로 실행한 보고서에 `repetition_summary`가 있으면 가드레일 결과에 `stability`가 추가됨. 반복이 없는 보고서에서는 이 항목이 없음.

- 진단용 출력임. `verdict`, `passed`, hard·target·regression 행, 종료 코드에 영향을 주지 않음. 계산이 실패해도 판정은 그대로이고 이 항목만 `status: "error"`(요약 형식이 깨졌으면 `malformed`)가 됨.
- 대표 문항별 실행들이 서로 다른 결과를 냈는지 보여 줌.

  | 필드 | 내용 |
  | --- | --- |
  | `status_disagreement_case_ids` | 실행 사이에 `passed`·`failed`가 갈린 문항 |
  | `answer_disagreement_case_ids` | 최종 답변 해시가 2종 이상인 문항 |
  | `unstable_case_ids`, `unstable_cases` | 위 두 목록의 합집합과 문항별 상태·해시 종류 수 |
  | `incomplete_case_ids` | 계획한 횟수를 다 채우지 못한 문항 |
  | `not_all_passed_case_ids` | 실행 중 하나라도 `passed`가 아닌 문항. 매번 같게 실패한 문항도 포함 |
  | `all_stable` | 대표 문항이 있고, 모두 횟수를 채웠으며, 불안정한 문항이 없음 |
  | `repetitions_interrupted_reason`, `repetitions_error_type`, `core_run_count`, `expected_core_run_count` | 반복 중단 사유(`repeat_phase_error`이면 예외 형식 이름 포함)와 실행 수 |
  | `p50_duration_ms`, `p95_duration_ms`, `warm_p95_duration_ms`, `cold_run_count`, `warm_run_count` | `repetition_summary`에서 가져온 시간 요약 |

- 답변 해시가 다르다는 것은 문장이 달랐다는 뜻이며 사실 오류를 뜻하지 않음. 상태가 같아도 해시가 다를 수 있고, 반대도 가능함. 문항 상태는 `statuses`와 개별 `repeat-*.json` 산출물로 확인함.
- CLI 표준 출력에는 `stability`의 `all_stable`, 불안정 문항 수, 미완료 문항 수만 나옴. 문항 ID는 `--output` 파일에만 있음.

### 라벨 작성 규칙

- `forbidden_facts`에는 문맥과 상관없이 답변에 나오면 안 되는 정확한 문자열만 넣음. 문자열 비교라서 "30일이 아니라 7일" 같은 부정문도 위반으로 집계됨.

### 한계와 후속 과제

- 사실 앵커는 문자열 비교라서 의미 정확도를 증명하지 않음. 인용 신원 확인은 주장 지지를 증명하지 않음.
- 후속: 주장 지지·중대 오류 분류, 반복 실행 기반 LLM regression과 cold/warm 분리 후 p95 차단, MCP search/fetch 단계별 성공 필드, report schema v2에서 실행 완전성과 품질 판정 분리.

## 단계별 재현과 공개 검증

`real_regulation_corpus_probe.py`, `real_regulation_mcp_probe.py`, `evaluate_live_local_llm.py`는 단계별 CLI도 제공한다. 인수는 각 `--help`로 확인한다. 독립 LLM CLI를 쓸 때는 전용 API의 `RAG_LLM_ENDPOINT`를 observer 포트로 미리 연결하고, 인증 토큰은 `REG_RAG_EVAL_API_TOKEN` 환경 변수로 전달한다. 원문·질문·응답·trace·runtime ZIP은 공개 소스에 추가하지 않는다.

일반 단위 테스트는 합성 자료로 수행한다. 실제 자료 시험은 별도 opt-in 실행이며, 실제 자료나 모델이 없어 건너뛴 검사를 통과로 바꾸지 않는다.

최종 배포물은 깨끗한 빌드 디렉터리에서 만들고, wheel의 Python 파일 집합과 각 파일의 SHA-256이 선택한 소스와 일치하는지도 확인한다. 과거 빌드 디렉터리에는 소스에서 삭제된 파일이 남을 수 있다. 빌드 명령 성공과 공개 소스 위생 통과는 완성된 wheel의 내용 일치를 대신하지 않는다. 실제 설치·재실행에는 검사한 정확한 wheel 해시를 사용한다.

```powershell
python -m unittest discover -s tests -v
python -m build --sdist --wheel
python scripts/audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
```

보호 범위의 구현 변경은 [변경 보호 정책](preprocessing_change_governance_ko.md)에 따라 집중 회귀 증거와 PR 필수 항목을 준비하고, 실제 Code Owner 검토와 `preprocessing-reviewed` 라벨을 받아야 한다.
