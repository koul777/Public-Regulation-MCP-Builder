# MCP 검색 성능·품질 게이트 운영 가이드

이 문서는 승인된 로컬 Regulation RAG/MCP 검색 경로의 첫 질의 성능과 검색 품질을 재현 가능하게 확인하는 방법을 설명한다. 대상 도구는 다음 세 개다.

- [`benchmark_mcp_first_query.py`](../scripts/benchmark_mcp_first_query.py): 새 Python 프로세스에서 실제 첫 검색까지 걸리는 시간과 같은 프로세스의 warm 검색 시간을 측정한다.
- [`evaluate_mcp_retrieval_quality.py`](../scripts/evaluate_mcp_retrieval_quality.py): 사람이 검토한 비공개 query spec을 기준으로 Recall@1/3/5, MRR, 문서 recall, no-evidence 오탐·기권 동작을 평가한다.
- [`build_mcp_performance_load_evidence.py`](../scripts/build_mcp_performance_load_evidence.py): 기존 부하·transport·가시성 증거와 두 보고서를 검증하고, 비공개 통합 보고서와 식별자가 제거된 공개용 파생 보고서를 만든다.

성능 통과는 검색 품질 통과를 뜻하지 않고, 검색 품질 통과도 시작 지연이 허용 범위라는 뜻이 아니다. 배포 또는 기준선 변경 판단에는 두 결과를 함께 사용한다. 두 도구 모두 승인된 로컬 검색 경로를 검사할 뿐이며, 전처리 성공이나 검색 결과 존재를 보안 승인으로 간주하지 않는다.

## 실행 전 불변조건

실행 전에 다음 조건을 고정해 기록한다.

- 테스트할 소스 commit과 Python 버전
- 승인된 런타임 데이터 snapshot
- tenant, profile, 보안 등급, 부서 범위
- flat 또는 tenant-isolated 저장 구조
- query spec SHA-256
- 실행 장비, 전원 정책, 동시 부하 조건
- 기준선과 threshold의 검토자 및 승인 근거

실제 데이터 루트, tenant/profile 식별자, query spec, 보고서 경로는 공개 문서나 명령 기록에 직접 쓰지 않는다. PowerShell 세션 또는 보호된 CI secret에서 다음처럼 주입한다.

    $DataRoot = $env:REG_RAG_DATA_ROOT
    $TenantId = $env:REG_RAG_TENANT_ID
    $ProfileId = $env:REG_RAG_PROFILE_ID
    $PrivateEvidenceRoot = $env:REG_RAG_PRIVATE_EVIDENCE_ROOT
    $QuerySpec = Join-Path $PrivateEvidenceRoot "retrieval-query-spec.json"
    $PerformanceReport = Join-Path $PrivateEvidenceRoot "reports\mcp-first-query.json"
    $PerformanceMarkdown = Join-Path $PrivateEvidenceRoot "reports\mcp-first-query.md"
    $QualityReport = Join-Path $PrivateEvidenceRoot "reports\mcp-retrieval-quality.json"
    $PrivateLoadEvidence = Join-Path $PrivateEvidenceRoot "reports\mcp-performance-load.json"
    $PublicLoadEvidence = Join-Path $env:REG_RAG_PUBLIC_EVIDENCE_ROOT "mcp-performance-load.public.json"
    $PublicLoadMarkdown = Join-Path $env:REG_RAG_PUBLIC_EVIDENCE_ROOT "mcp-performance-load.public.md"

`REG_RAG_PRIVATE_EVIDENCE_ROOT`는 이 공개 저장소 밖의 접근 통제된 위치를 가리켜야 한다. `.gitignore`만으로 민감정보를 보호할 수 있다고 가정하지 않는다.

`--data-dir`에는 이미 준비된 승인 런타임 루트만 사용한다. 존재하지 않는 임의 경로를 smoke 대상으로 주면 저장소 계층이 초기 manifest나 lock 같은 runtime artifact를 만들 수 있다. 이 공개 소스 checkout 자체를 데이터 루트 또는 보고서 출력 위치로 사용하지 않는다.

## Query spec

하나의 query spec을 두 도구에 함께 사용할 수 있다. 성능 도구는 `id`, `query`, `expect_no_evidence`만 사용하고 chunk/document 정답 label은 무시한다. 품질 도구는 답변 가능 질의마다 chunk 또는 document target을 요구하며, 답이 없어야 하는 질의에는 `expect_no_evidence`를 요구한다.

다음은 형식 설명만을 위한 합성 예시다. ID, 질의, target은 실제 기관 자료에서 가져온 값이 아니다.

    {
      "queries": [
        {
          "id": "synthetic-answerable-001",
          "query": "synthetic policy question alpha",
          "target_chunk_ids": [
            "synthetic-chunk-001"
          ],
          "target_document_ids": [
            "synthetic-document-001"
          ]
        },
        {
          "id": "synthetic-no-evidence-001",
          "query": "synthetic absent policy question beta",
          "expect_no_evidence": true
        }
      ]
    }

품질 label 작성 시 다음 규칙을 지킨다.

- `id`는 검토자가 추적할 수 있는 안정적인 합성 평가 ID로 둔다.
- `query` 또는 `question` 중 하나를 사용한다.
- 단일 target은 `target_chunk_id` 또는 `target_document_id`, 복수 target은 각각의 복수형 필드를 사용할 수 있다.
- chunk target이 있으면 그것이 1차 relevance 기준이고, 없으면 document target이 1차 기준이다.
- `expect_no_evidence`와 target ID를 함께 쓰지 않는다.
- no-evidence 항목을 포함하지 않으면 오탐률과 기권률 threshold를 검증할 수 없다.
- 실제 질의와 target ID가 담긴 goldset은 비공개 검토 증거이며 공개 fixture가 아니다.

두 보고서의 query spec fingerprint가 같아야 같은 평가 입력을 사용했다고 볼 수 있다. 파일 내용이나 순서가 바뀌면 새 기준선으로 취급하고 사람 검토를 다시 수행한다.

## Flat 저장과 tenant-isolated 저장

두 CLI에서 `--flat-storage`와 `--tenant-storage-isolation`은 상호 배타적이다.

- `--flat-storage`: 단일 tenant 또는 기존 flat 런타임 레이아웃을 명시한다.
- `--tenant-storage-isolation`: tenant별로 분리된 런타임 레이아웃을 명시한다.
- 둘 다 생략: 런타임 manifest와 tenant 저장 디렉터리를 이용한 자동 판정에 맡긴다.

CI와 기준선 측정에서는 자동 판정보다 실제 배포 구조와 일치하는 옵션을 명시하는 편이 재현성이 높다. 잘못된 옵션으로 빈 색인이나 다른 scope를 측정한 결과는 유효한 통과 증거가 아니다. 여러 profile이 존재하는 tenant에서는 `--profile-id`도 실제 운영 scope와 동일하게 지정한다.

tenant-isolated 실행의 기본 형태는 다음과 같다.

    python scripts\benchmark_mcp_first_query.py `
        --data-dir $DataRoot `
        --tenant-id $TenantId `
        --profile-id $ProfileId `
        --query-spec-json $QuerySpec `
        --tenant-storage-isolation `
        --iterations 3 `
        --warm-iterations 1 `
        --min-result-count 1

flat 저장을 검사할 때는 위 명령의 `--tenant-storage-isolation`만 `--flat-storage`로 바꾼다. 두 옵션을 동시에 주거나, 실패를 없애기 위해 실제 배포와 다른 저장 모드로 바꾸지 않는다.

## 실제 cold-first-query 측정

`benchmark_mcp_first_query.py`의 부모 프로세스는 애플리케이션을 미리 import하지 않는다. 각 query와 iteration마다 새 Python child를 시작하고, child 안에서 다음 순서로 실행한다.

1. 저장소 소스에서 MCP 모듈을 import한다.
2. `settings_for_mcp_project`와 `mcp_auth_context`를 구성한다.
3. `search_regulations`를 정확히 한 번 호출해 cold 검색을 측정한다.
4. `--warm-iterations`가 0보다 크면 같은 child에서 같은 검색을 반복한다.
5. 집계 가능한 안전한 payload만 부모로 반환하고 child를 종료한다.

`process_wall_elapsed_ms`는 프로세스 생성, import, 설정·인증 구성, 첫 검색, 결과 집계, 프로세스 종료를 포함한다. `cold.search_elapsed_ms`는 child 내부의 첫 `search_regulations` 호출만 측정한다. `warm.search_elapsed_ms`는 같은 프로세스에서 뒤따른 호출 시간이다. `trace_timing_ms`는 검색 응답 metadata가 제공한 로드, 가시성 필터, scoring 등의 단계 시간을 집계한다.

여기서 cold는 **새 Python 프로세스와 새 애플리케이션 런타임**을 뜻한다. 운영체제 page cache, 디스크 cache, 백신 상태까지 초기화한 물리적 cold boot를 뜻하지 않는다. 기준선 비교 시 장비와 cache 정책을 동일하게 유지하고 이 한계를 기록한다.

성능 child는 다음 설정 override를 항상 사용한다.

- `api_audit_enabled=False`
- `rag_trace_enabled=False`

따라서 벤치마크 자체가 API audit event나 RAG trace 운영 기록을 추가하지 않는다. 성능 보고서의 `trace_timing_ms`는 응답에 이미 포함된 메모리상의 timing metadata이며 운영 trace 파일이 아니다.

## 검증된 읽기 경로의 성능 불변조건

MCP 검색·조회 성능을 높일 때 승인 철회나 tenant/profile 경계 검사를 TTL cache로 늦추지 않는다. 현재 검증형 hierarchy 경로는 다음 불변조건을 유지한다.

- 요청마다 manifest, hierarchy index, vector, BM25, 승인 snapshot의 identity를 하나의 read context에 고정한다. 이 context는 다음 요청에 재사용하지 않는다.
- materialization 뒤 같은 identity를 다시 확인한다. 중간에 runtime, 승인 journal, ACL, chunk, sidecar, BM25가 바뀌면 계산한 결과를 반환하지 않는다.
- 재사용 가능한 visibility·record cache에는 runtime identity와 tenant/profile/auth scope가 포함된다. cache hit 뒤에도 현재 요청의 role, department, security 범위를 다시 적용한다.
- hierarchy postflight가 실패해 flat 검색으로 전환할 때도 profile topology를 검색 전후에 다시 확인한다. 생략된 profile이 다중 profile로 바뀌거나 단일 profile이 교체되면 fail-closed 한다.
- 승인 파일의 content signature는 프로세스 전체에서 최대 4개 worker만 사용하고, 같은 파일·같은 identity의 동시 계산은 single-flight로 합친다. 읽기 전후 file identity가 다르면 hash를 cache하지 않는다.
- 경량 길이·형식 검사는 Pydantic을 import하지 않는 `app.core.input_limits`에 둔다. MCP/API의 공개 `Annotated`·`Field` schema는 별도 모듈에 두되 기존 최소·최대값과 JSON schema 계약은 바꾸지 않는다.

이 구조에서 prevalidated identity는 같은 요청의 바깥쪽 postflight가 최종 변경 검사를 수행할 때만 전달할 수 있다. 호출자가 임의 path나 오래된 signature를 넣을 수 있는 범용 우회로를 만들거나, directory mtime·TTL만으로 승인 상태를 신뢰해서는 안 된다. 또한 runtime manifest가 strict reindex를 요구하면 성능 최적화를 이유로 해당 blocker를 완화하지 않는다.

### 2026-07-31 개발 비교 기록

다음 값은 같은 장비·승인 snapshot·28개 비공개 query spec으로 공개 `v1.2.14` clean source와 후보 source state를 번갈아 세 쌍 실행한 개발 증거다. 식별자와 원시 질의는 포함하지 않는다. 각 값은 세 clean 실행과 세 후보 실행에서 얻은 p95 또는 batch 값의 중앙값이다. 이 보고서만으로 호스트 상태의 영향을 분리할 수 없으므로 절대 지연을 릴리스 SLO로 사용하지 않고, 같은 시각에 교차 실행한 상대 차이만 개발 판단에 사용한다. 이 표는 exact-commit 릴리스 기준선이나 보호된 CI 승인을 대신하지 않는다.

| 지표 | `v1.2.14` clean | 후보 중앙값 | 변화 |
| --- | ---: | ---: | ---: |
| fresh-process wall p95 | 2,790.116 ms | 1,949.441 ms | 30.1% 감소 |
| fresh-process setup p95 | 891.809 ms | 245.151 ms | 72.5% 감소 |
| cold search p95 | 837.227 ms | 699.328 ms | 16.5% 감소 |
| warm search p95 | 152.483 ms | 155.762 ms | 2.2% 증가 |
| 순차 search p95 | 160.979 ms | 159.139 ms | 1.1% 감소 |
| 순차 fetch p95 | 157.075 ms | 114.852 ms | 26.9% 감소 |
| 순차 total p95 | 322.310 ms | 271.196 ms | 15.9% 감소 |
| 동시성 8 fetch p95 | 1,074.204 ms | 765.321 ms | 28.8% 감소 |
| 동시성 8 task total p95 | 1,978.169 ms | 1,773.685 ms | 10.3% 감소 |
| 동시성 8 batch | 15,594.634 ms | 13,499.939 ms | 13.4% 감소 |

같은 비교에서 Recall@1/3/5는 각각 `0.416667/0.583333/0.625`, MRR은 `0.496528`, document Recall@1/3/5는 모두 `1.0`으로 유지됐다. no-evidence false-positive rate는 `0`, abstention rate는 `1.0`, 검색 오류는 `0`이었다. 성능 변화 전체를 한 최적화에 귀속하지 않으며, source-state fingerprint가 같은 보고서끼리만 통합 게이트에 사용한다.

후속 후보로 cold approval-sidecar miss에서 content signature bundle을 반복 계산하는 경로가 있다. 최적화하더라도 sidecar payload 비교까지의 pre-signature만 공유하고, materialization 뒤 approval journal·repository chunk·sidecar의 fresh signature 검사는 남겨야 한다. 이 작업은 sidecar parse 직후 mutation, 동일 크기·mtime 복원 교체, 직접 파일 수정, custom loader 호환 회귀가 먼저 준비되기 전에는 적용하지 않는다.

## 반복 수 권장값

개발 smoke와 품질 게이트의 목적을 구분한다.

| 용도 | fresh child/query 권장 최소 | warm 반복/child 권장 최소 | 해석 |
| --- | ---: | ---: | --- |
| 단순 연결 확인 | 1 | 0 | 명령과 scope가 동작하는지만 확인하며 분포나 p95 근거로 사용하지 않는다. |
| 개발 smoke | 3 | 1 | 빠른 회귀 탐지용이다. p95 기준선이나 성능 주장에 사용하지 않는다. |
| PR/CI 게이트 | 20 | 3 | 고정된 runner와 query spec에서 threshold 판정에 사용한다. |
| 릴리스 기준선 | 50 | 5 | 같은 장비·snapshot에서 반복해 검토 가능한 기준선을 만든다. |

질의가 여러 개면 fresh child 수는 `query_count × iterations`다. `--min-success-count`도 전체 cold 측정 수를 기준으로 한다. 값을 생략하면 모든 cold 측정 성공이 기본 요구사항이 된다.

`--min-result-count`의 기본값은 1이다. 답변 가능 질의는 이 최소 개수 이상을 반환해야 하고, `expect_no_evidence=true`인 질의는 정확히 0개를 반환해야 qualified success가 된다. 따라서 빈 저장소나 잘못된 scope가 모든 질의에 0개를 매우 빠르게 반환해도 통과하지 않는다. 성능 세트에는 답변 가능 질의가 최소 하나 있어야 하며, no-evidence 항목만 있는 세트는 실패한다. 검증형 hierarchy runtime bundle을 측정한다면 `--required-retrieval-strategy catalog_toc_body`도 사용해 flat fallback 결과가 번들 성능으로 잘못 기록되지 않게 한다.

품질 평가 도구는 동일 질의를 반복해 평균내는 도구가 아니다. query spec의 각 항목을 한 번 검색한다. 품질 표본을 늘리려면 중복 질의를 복사하지 말고, 사람이 검토한 서로 다른 대표 질의와 no-evidence 사례를 추가한다.

## 개발 smoke

비공개 query spec과 실제 저장 모드가 준비되면 다음 명령으로 빠르게 확인한다.

    python scripts\benchmark_mcp_first_query.py `
        --data-dir $DataRoot `
        --tenant-id $TenantId `
        --profile-id $ProfileId `
        --query-spec-json $QuerySpec `
        --tenant-storage-isolation `
        --iterations 3 `
        --warm-iterations 1 `
        --min-result-count 1 `
        --out-json $PerformanceReport `
        --out-md $PerformanceMarkdown

이 실행은 threshold 없이 분포를 관찰하는 용도다. `passed`가 거짓이어도 `--fail-on-threshold`를 주지 않으면 CLI 종료 코드만으로 실패를 판정할 수 없으므로 보고서의 `passed`와 `findings`를 확인한다.

## CI 성능 게이트

latency threshold는 이번 실행 결과에 맞춰 정하지 않는다. 동일 장비, 동일 데이터 snapshot, 동일 query spec으로 만든 검토 완료 기준선에서 환경 변동 폭과 서비스 목표를 반영해 승인한다. 실제 값은 보호된 CI 변수로 주입한다.

    $ExpectedColdSuccesses = [int]$env:REG_RAG_EXPECTED_COLD_SUCCESSES
    $MaxColdP95Ms = [double]$env:REG_RAG_MAX_COLD_P95_MS
    $MaxWarmP95Ms = [double]$env:REG_RAG_MAX_WARM_P95_MS
    $RequiredRetrievalStrategy = $env:REG_RAG_REQUIRED_RETRIEVAL_STRATEGY

    python scripts\benchmark_mcp_first_query.py `
        --data-dir $DataRoot `
        --tenant-id $TenantId `
        --profile-id $ProfileId `
        --query-spec-json $QuerySpec `
        --tenant-storage-isolation `
        --iterations 20 `
        --warm-iterations 3 `
        --min-success-count $ExpectedColdSuccesses `
        --min-result-count 1 `
        --required-retrieval-strategy $RequiredRetrievalStrategy `
        --max-cold-p95-ms $MaxColdP95Ms `
        --max-warm-p95-ms $MaxWarmP95Ms `
        --out-json $PerformanceReport `
        --out-md $PerformanceMarkdown `
        --fail-on-threshold

    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

`--required-retrieval-strategy`는 `flat_rag` 또는 `catalog_toc_body`만 허용한다. `--max-cold-p95-ms`는 전체 child의 `process_wall_elapsed_ms.p95`를 검사한다. `--max-warm-p95-ms`는 성공한 warm 검색의 `search_elapsed_ms.p95`를 검사한다. 설정된 warm threshold가 있는데 warm 측정값이 없으면 통과하지 않는다. `--fail-on-threshold`를 사용하면 report가 통과하지 못했을 때 종료 코드 2를 반환한다.

## 검색 품질 게이트

품질 threshold도 현재 결과에 맞춰 사후 조정하지 않는다. 검토 완료 goldset과 승인된 기준선에서 정책값을 결정하고 보호된 CI 변수로 주입한다.

    $MinRecallAt1 = [double]$env:REG_RAG_MIN_RECALL_AT_1
    $MinRecallAt3 = [double]$env:REG_RAG_MIN_RECALL_AT_3
    $MinRecallAt5 = [double]$env:REG_RAG_MIN_RECALL_AT_5
    $MinMrr = [double]$env:REG_RAG_MIN_MRR
    $MinDocumentRecallAt5 = [double]$env:REG_RAG_MIN_DOCUMENT_RECALL_AT_5
    $MaxNoEvidenceFalsePositiveRate = [double]$env:REG_RAG_MAX_NO_EVIDENCE_FALSE_POSITIVE_RATE
    $MinNoEvidenceAbstentionRate = [double]$env:REG_RAG_MIN_NO_EVIDENCE_ABSTENTION_RATE

    python scripts\evaluate_mcp_retrieval_quality.py `
        --data-dir $DataRoot `
        --tenant-id $TenantId `
        --profile-id $ProfileId `
        --query-spec-json $QuerySpec `
        --tenant-storage-isolation `
        --top-k 5 `
        --min-recall-at-1 $MinRecallAt1 `
        --min-recall-at-3 $MinRecallAt3 `
        --min-recall-at-5 $MinRecallAt5 `
        --min-mrr $MinMrr `
        --min-document-recall-at-5 $MinDocumentRecallAt5 `
        --max-no-evidence-false-positive-rate $MaxNoEvidenceFalsePositiveRate `
        --min-no-evidence-abstention-rate $MinNoEvidenceAbstentionRate `
        --out-json $QualityReport `
        --fail-on-threshold

    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

품질 도구는 Recall@5 계산을 위해 요청값이 더 작아도 최소 5개를 검색하며, 최대 검색 깊이는 MCP 상한인 20이다. 필요하면 문서 recall의 1·3 cutoff, `--department-id`, `--security-level`, `--as-of-date`도 실제 운영 scope와 동일하게 추가한다. 검색 오류는 no-evidence 기권으로 계산되지 않고 별도 finding으로 처리된다.

품질 도구도 측정 자체가 운영 기록을 추가하지 않도록 `api_audit_enabled=False`, `rag_trace_enabled=False`를 사용한다. 이는 검색 승인·tenant·ACL 검사를 끄는 옵션이 아니며, 품질 보고서의 trace metadata와 비공개 target/result 식별자는 계속 민감정보로 취급한다.

## 동시 질의 부하 게이트

동시 질의 보고서는 동일 승인 snapshot과 query spec에서 실제 검색·fetch·답변 작업을 thread pool로 겹쳐 실행한다. 릴리스 기준은 생산자 보고서 안의 threshold만 신뢰하지 않는다. 최소 동시성, 최소 task 수, 모든 task의 최대 총시간, 전체 batch 최대시간을 보호된 CI 정책값으로 별도 주입한다.

    $ConcurrentMinConcurrency = [int]$env:REG_RAG_CONCURRENT_MIN_CONCURRENCY
    $ConcurrentMinTaskCount = [int]$env:REG_RAG_CONCURRENT_MIN_TASK_COUNT
    $ConcurrentMaxTaskTotalMs = [double]$env:REG_RAG_CONCURRENT_MAX_TASK_TOTAL_MS
    $ConcurrentMaxBatchElapsedMs = [double]$env:REG_RAG_CONCURRENT_MAX_BATCH_ELAPSED_MS
    $ConcurrentReport = $env:REG_RAG_CONCURRENT_QUERY_BENCHMARK_REPORT

    python scripts\benchmark_mcp_concurrent_queries.py `
        --data-dir $DataRoot `
        --tenant-id $TenantId `
        --profile-id $ProfileId `
        --query-spec-json $QuerySpec `
        --tenant-storage-isolation `
        --rounds 4 `
        --concurrency $ConcurrentMinConcurrency `
        --min-warm-records ([int]$env:REG_RAG_MIN_WARM_RECORDS) `
        --max-task-total-ms $ConcurrentMaxTaskTotalMs `
        --max-batch-elapsed-ms $ConcurrentMaxBatchElapsedMs `
        --out-json $ConcurrentReport `
        --fail-on-threshold

생산자는 `schema_version=1`, `repo_commit`, `mcp-performance-python-source-v1` source state, task별 측정값과 batch 집계를 기록한다. 통합 게이트는 `passed=true`만으로 통과시키지 않는다. `task_count == query_count * rounds`, measurement/success/error count, `(round, query_index)` task 집합, 유한한 timing, warm record와 index 준비 상태, 답변 가능 질의의 양수 결과, no-evidence 질의의 0개 결과를 다시 검증한다. 이어서 모든 task의 `total_elapsed_ms`와 batch elapsed를 외부 정책값에 직접 대조한다. 생산자 threshold를 완화하거나 삭제해도 이 외부 검사를 우회할 수 없다.

strict 모드의 최소 동시성은 2 이상, 최소 task 수는 1 이상 정수여야 한다. 두 최대시간은 유한한 0 이상 숫자여야 하며 `NaN`, `Infinity`, 음수는 거부한다. 네 정책값 중 하나라도 빠지거나 보고서가 없으면 `--require-concurrent-query-benchmark`가 fail-closed blocker를 만든다.

## 통합 릴리스 게이트

첫 질의, 품질, 동시 질의 보고서를 단순히 전달하는 것만으로 릴리스 준비가 되지 않는다. 첫 질의 보고서에는 최소 성공 수, 검색당 최소 결과 수, cold p95 threshold가, 품질 보고서에는 Recall@5, MRR, document Recall@5, no-evidence 오탐·기권 threshold가 모두 설정되어야 한다. 동시 질의에는 네 외부 정책값이 모두 있어야 한다. 통합 도구는 기록된 지표와 qualified-success 카운트가 threshold를 실제로 만족하는지, 동시 질의의 각 measurement와 batch가 별도 정책을 만족하는지를 다시 계산한다.

기존 query benchmark, transport smoke, index visibility 보고서와 색인 경로는 접근 통제된 환경 변수로 주입한다. strict 릴리스 증거에 사용할 visibility 보고서는 같은 runtime scope에서 반드시 `--require-indexed`로 새로 생성한다.

    python scripts\audit_mcp_index_visibility.py `
        --data-dir $DataRoot `
        --tenant-id $TenantId `
        --profile-id $ProfileId `
        --tenant-storage-isolation `
        --min-visible-records ([int]$env:REG_RAG_MIN_VISIBLE_RECORDS) `
        --forbid-smoke-docs `
        --require-indexed `
        --out-json $env:REG_RAG_INDEX_VISIBILITY_REPORT `
        --out-md $env:REG_RAG_INDEX_VISIBILITY_MARKDOWN `
        --fail-on-issue

    python scripts\build_mcp_performance_load_evidence.py `
        --query-benchmark-report $env:REG_RAG_QUERY_BENCHMARK_REPORT `
        --transport-smoke-report $env:REG_RAG_TRANSPORT_SMOKE_REPORT `
        --index-visibility-report $env:REG_RAG_INDEX_VISIBILITY_REPORT `
        --approved-vectors-jsonl $env:REG_RAG_APPROVED_VECTORS_JSONL `
        --bm25-index-json $env:REG_RAG_BM25_INDEX_JSON `
        --first-query-benchmark-report $PerformanceReport `
        --retrieval-quality-report $QualityReport `
        --concurrent-query-benchmark-report $ConcurrentReport `
        --max-total-p95-ms ([double]$env:REG_RAG_MAX_TOTAL_P95_MS) `
        --max-warm-search-p95-ms ([double]$env:REG_RAG_MAX_WARM_SEARCH_P95_MS) `
        --max-transport-warm-search-ms ([double]$env:REG_RAG_MAX_TRANSPORT_WARM_SEARCH_MS) `
        --require-latency-slo `
        --require-repo-commit-consistency `
        --require-first-query-benchmark `
        --require-retrieval-quality `
        --require-concurrent-query-benchmark `
        --min-concurrent-query-concurrency $ConcurrentMinConcurrency `
        --min-concurrent-query-task-count $ConcurrentMinTaskCount `
        --max-concurrent-query-task-total-ms $ConcurrentMaxTaskTotalMs `
        --max-concurrent-query-batch-elapsed-ms $ConcurrentMaxBatchElapsedMs `
        --expected-first-query-retrieval-strategy $RequiredRetrievalStrategy `
        --require-indexed-visibility `
        --out-json $PrivateLoadEvidence `
        --out-public-json $PublicLoadEvidence `
        --out-public-md $PublicLoadMarkdown `
        --fail-on-issue

### 소스 보고서 커밋 일관성

통합 증거에 선택된 query benchmark, transport smoke, index visibility, first-query benchmark, retrieval-quality, concurrent-query benchmark 보고서는 같은 소스 revision에서 생성되어야 한다. 현재 여섯 생산자는 모두 `repo_commit`을 기록한다. 이 필드가 추가되기 전에 생성된 기존 보고서에는 값이 없을 수 있으므로 strict 증거에는 각 보고서를 현재 생산자로 다시 생성한다.

통합 도구는 값이 확인되는 보고서 사이의 `repo_commit` 불일치를 항상 blocker로 처리한다. 40자리 16진수 형식이 아닌 값도 blocker다. `--require-repo-commit-consistency`를 사용하면 선택 보고서 중 `repo_commit`이 누락되거나 `UNKNOWN`, `null`, `unavailable`인 경우도 `source-report-repo-commit-unverifiable` blocker가 된다.

기존 보고서 호환성을 위해 required 모드가 아닐 때 누락·`UNKNOWN`만으로 `passed`나 `evidence_ready`를 실패시키지는 않는다. 다만 `repo_commit_consistency.fully_verified=false`가 되고 `performance_release_ready`는 거짓이므로 릴리스 증거가 false-green이 되지 않는다. 상태와 역할 목록만 통합 보고서에 추가하며 tenant, profile, department, 데이터 경로 같은 기관 식별자는 공개 파생본에 추가하지 않는다.

### 실제 소스 상태 지문

Git commit이 같아도 tracked 파일 수정이나 untracked Python 파일이 남아 있으면 실행 코드는 다를 수 있다. 여섯 보고서 생산자와 통합 증거 도구는 실행 시작과 종료에 실제 파일 시스템을 읽어 `source_state`를 기록한다. 지문 범위는 `mcp-performance-python-source-v1`이며 `app/**/*.py`, `scripts/**/*.py`, `pyproject.toml`만 포함한다. 따라서 관련 untracked `.py`도 포함되고 `tests`, `docs`, `frontend`, 보고서·runtime 출력은 포함되지 않는다.

지문은 저장소 상대 POSIX 경로를 UTF-8 byte 순서로 정렬한 뒤, scope label·각 경로·원본 파일 bytes를 길이 prefix와 함께 SHA-256에 입력한다. 보고서에는 절대 경로, 파일 목록, mtime을 기록하지 않는다. 범위 밖으로 향하는 symlink, 읽을 수 없는 파일, 스캔 중 변경이나 파일 집합 drift가 감지되면 digest를 내보내지 않고 `unavailable` 또는 `changed_during_run`으로 표시한다.

통합 도구는 선택된 여섯 보고서와 통합 도구 자신의 종료 지문까지 비교한다. 확인된 digest 불일치, malformed metadata, `unavailable` 또는 `changed_during_run`은 모드와 관계없이 blocker다. `--require-repo-commit-consistency`는 source-state strict 검증도 함께 활성화하므로 모든 선택 보고서가 같은 유효 지문을 제공해야 한다. strict가 아닌 경우에만 과거 보고서의 `source_state` 누락을 진단 호환으로 허용하지만 `source_state_consistency.fully_verified=false`와 `performance_release_ready=false`를 유지한다. 공개 파생본에서는 source digest를 제거하고 scope, status, 파일·byte count, 안정성 boolean 같은 집계값만 유지한다.

`performance_release_ready`는 기존 latency SLO와 threshold-bearing first-query·retrieval-quality 게이트, 외부 정책으로 다시 검증한 concurrent-query 게이트가 모두 통과할 때만 참이다. `--require-first-query-benchmark`, `--require-retrieval-quality`, `--require-concurrent-query-benchmark`는 보고서나 필수 정책 누락을 blocker로 바꾸므로 `--fail-on-issue`와 함께 사용한다. `--expected-first-query-retrieval-strategy`는 생산자 보고서 내부 설정과 별개의 릴리스 정책값이다. hierarchy runtime을 주장하는 릴리스에서는 이를 `catalog_toc_body`로 고정해 보고서 threshold 삭제나 flat fallback 전환을 차단한다.

`--require-indexed-visibility`는 visibility 보고서의 `requirements.require_indexed`가 정확히 `true`이고 `status_counts.indexed == document_count`일 때만 통과한다. 이 provenance가 없는 기존 보고서, `--require-indexed` 없이 만든 완화 보고서, `reindex_required` 등 non-indexed 상태가 하나라도 있는 보고서는 fail-closed 처리한다. latency threshold는 유한한 0 이상 숫자여야 하며 `NaN`, `Infinity`, 음수는 거부된다. threshold가 설정됐지만 해당 latency 측정값이 없으면 `latency_slo.passed`도 거짓이다.

`--out-json`과 일반 `--out-md`는 source path와 내부 식별자를 보존하는 비공개 진단 보고서다. `--out-public-json`과 `--out-public-md`는 local path, tenant/profile/department, query/result/trace 식별자, 원본·query fingerprint와 상세 finding을 제거한 공유용 파생본이다. 공개 파생본을 만들었더라도 정책상 허용된 집계만 게시하고, 원시 입력 보고서나 비공개 fingerprint manifest를 함께 게시하지 않는다.

## 보고서 해석

### 첫 질의 성능 보고서

다음 필드를 함께 본다.

- `summary.process_wall_elapsed_ms`: 모든 fresh child의 end-to-end 분포
- `summary.successful_process_wall_elapsed_ms`: cold 검색까지 성공한 child만의 분포
- `summary.cold.search_elapsed_ms`: 첫 검색 호출 분포
- `summary.warm.search_elapsed_ms`: 동일 child 후속 검색 분포
- `summary.cold.trace_timing_ms`, `summary.warm.trace_timing_ms`: 내부 단계별 분포
- `summary.cold.success_rate`, `summary.warm.success_rate`: 성공률
- `summary.cold.result_requirement_failed_count`, `summary.warm.result_requirement_failed_count`: 결과가 최소 개수에 못 미친 검색 수
- `summary.cold.retrieval_strategy_requirement_failed_count`, `summary.warm.retrieval_strategy_requirement_failed_count`: 요구 검색 전략과 불일치한 검색 수
- `summary.*.answerable_successful_count`, `summary.*.no_evidence_successful_count`: 답변 가능/기권 기대를 각각 만족한 검색 수
- `summary.timed_out_count`, `summary.invalid_protocol_count`: child 실행 문제
- `query_sha256`, `query_set_sha256`, `query_spec.sha256`: 질의 원문 없이 입력 동일성 확인
- `findings`: 최소 성공 수, cold p95, warm p95 위반

각 timing 집계에는 `p50`, `p95`, `p99`, `max`가 포함된다. process wall만 느리고 search 시간이 안정적이면 import·설정·프로세스 시작 경로를 먼저 조사한다. search와 scoring 단계가 함께 느리면 색인 크기, retrieval 전략, 가시성 필터, 저장 I/O를 확인한다. warm만 불안정하면 cache 경쟁이나 동시 부하를 확인한다. 실패 측정이 있으면 latency 수치만 보고 통과시키지 않는다.

성능 보고서에는 쿼리 원문, 검색 결과 본문, 결과 문서 ID를 넣지 않는다. 오류 메시지는 원문 대신 유형, 길이, SHA-256으로 기록하고 child stdout/stderr도 원문 대신 크기와 hash만 기록한다. 다만 보고서 metadata에는 데이터 루트와 tenant/profile 값이 포함될 수 있으므로 원시 보고서 자체는 공개 artifact가 아니다.

### 검색 품질 보고서

핵심 지표는 다음과 같다.

- `recall_at_1/3/5`: 1차 relevance target 중 cutoff 안에서 찾은 고유 target 비율의 평균
- `mrr`: 답변 가능 질의에서 첫 관련 결과 순위의 reciprocal rank 평균
- `document_recall_at_1/3/5`: document target을 제공한 질의의 문서 단위 recall
- `no_evidence_false_positive_rate`: 답이 없어야 하는 질의가 하나 이상의 결과를 반환한 비율
- `no_evidence_abstention_rate`: 답이 없어야 하는 질의가 성공적으로 0개 결과를 반환한 비율
- `search_error_count`: 검색 호출 자체가 실패한 수
- `query_spec_sha256`, `query_spec_item_count`: 평가 입력 동일성
- `threshold_failure_count`, `findings`, `passed`: 게이트 결과

품질 보고서는 검색 결과의 규정 본문이나 원문 text를 복사하지 않는다. 그러나 정답 판정을 위해 query, target ID, 결과 ID·제목·chunk/document ID, trace ID, 검색 오류 메시지와 로컬 경로 metadata를 포함할 수 있다. 따라서 “본문 미포함”은 “공개 가능”을 뜻하지 않는다.

### 동시 질의 보고서

비공개 원본에서는 `concurrency`, `task_count`, `summary.batch_elapsed_ms`, `summary.total_elapsed_ms`, `summary.successful_count`, `summary.error_count`, 답변 가능/no-evidence 측정 수와 각 measurement를 함께 확인한다. 통합 보고서의 `concurrent_query_release_gate`는 보고서 존재 여부, 네 외부 정책값 설정 여부, 독립 재검증 통과 여부를 분리해 표시한다. `concurrent_query_benchmark_summary`는 원시 measurement를 복사하지 않고 동시성·task·성공·오류·warm record·batch/task latency 집계만 보존한다.

동시 질의 원본에는 query, result/trace 식별자, fetch 제목, 오류, 데이터 경로와 tenant/profile이 들어갈 수 있다. 공개 파생본은 원시 measurement와 raw finding을 포함하지 않으며 query, 모든 ID, path, trace, query/source digest를 제거한다. 공개 JSON과 Markdown에는 집계 summary, 게이트 boolean, finding code와 일반화된 안내문만 남긴다.

## 비공개 goldset과 보고서 관리

다음 항목은 공개 저장소에 commit하거나 공개 CI artifact로 업로드하지 않는다.

- 실제 사용자 또는 기관 업무를 반영한 query spec
- 실제 chunk/document ID와 정답 label
- 결과 제목, trace ID, tenant/profile/department 식별자
- 원본 문서, 전처리 산출물, runtime data
- 데이터 루트와 비공개 증거의 로컬 경로
- 원시 성능·품질·동시 질의 JSON/Markdown 보고서
- 비공개 query spec 또는 보고서의 fingerprint를 외부와 연결할 수 있는 manifest

공개 저장소에는 합성 fixture와 정책상 허용된 집계만 둘 수 있다. 원시 보고서를 복사한 뒤 사람이 일부 필드만 지우는 방식은 누락 위험이 있으므로 사용하지 않는다. 자동화가 필요하면 통합 도구의 `--out-public-json`/`--out-public-md` 파생본을 사용하고 hygiene audit를 다시 실행한다. PR에 수치가 필요하면 비공개 증거 위치를 공개하지 말고, Code Owner가 접근 통제된 원본을 확인했다는 사실과 통과 여부만 정책이 허용하는 범위에서 기록한다.

실행 후에는 최소한 다음을 확인한다.

    git status --short
    python scripts\audit_release_hygiene.py `
        --workflow-scope available `
        --include-untracked `
        --include-source-path-scan

예상하지 않은 query spec, report, runtime 디렉터리가 저장소 아래에 생겼으면 PR을 만들기 전에 제거하고 원인을 확인한다. 보호된 CI artifact를 사용하더라도 접근 권한과 보존 기간을 최소화한다.

## Preprocessing governance 연계

파싱, 전처리, table/OCR 처리, chunk 경계, metadata schema, 승인 기반 색인, retrieval/scoring, lifecycle 선택이 바뀌면 같은 query spec에서도 성능과 품질이 달라질 수 있다. 이런 변경과 회귀 기준값·threshold·GitHub guard 변경은 [`preprocessing_change_governance_ko.md`](preprocessing_change_governance_ko.md)를 따른다.

특히 다음을 지킨다.

1. 보호 변경과 같은 PR에 집중된 회귀 테스트를 추가하거나 수정한다.
2. PR template에 변경 요약, 영향 형식, 불변조건, 변경 전후 비공개 측정 근거를 기록한다.
3. 골든 label이나 threshold를 새 결과에 맞춰 단순 갱신하지 않는다.
4. Code Owner가 비공개 근거를 검토한 뒤 `preprocessing-reviewed` 라벨을 부여한다.
5. `preprocessing-change-policy`와 `preprocessing-regression`을 모두 통과한다.
6. 승인되지 않은 chunk가 품질 수치를 높이기 위해 검색 대상에 들어가지 않았는지 확인한다.

성능·품질 보고서는 변경 검토 증거이지 승인 journal, 보안 검토, Code Owner 승인 또는 사람의 정답 검토를 대신하지 않는다.

## 재현 체크리스트

게이트 결과를 비교하기 전에 다음 항목이 모두 같거나 차이가 명시되어 있는지 확인한다.

- [ ] 동일한 source commit과 Python major/minor
- [ ] 동일한 승인 런타임 snapshot
- [ ] 동일한 query spec SHA-256과 항목 수
- [ ] 동일한 tenant/profile/department/security scope
- [ ] 동일한 flat 또는 tenant-isolated 저장 모드
- [ ] 동일한 장비 등급, 전원 정책, 동시 부하 조건
- [ ] 성능 gate에 충분한 fresh child와 warm 표본
- [ ] 성능 gate의 최소 결과 수와 예상 retrieval strategy가 실제 배포 경로와 일치
- [ ] 품질 gate에 답변 가능·no-evidence 대표 사례
- [ ] `--fail-on-threshold`와 종료 코드 검사
- [ ] 원시 goldset/report가 공개 저장소와 공개 artifact 밖에 있음
- [ ] preprocessing governance의 테스트·PR·Code Owner 조건 충족

CLI 옵션이 바뀌었는지는 저장소 루트에서 다음 명령으로 확인한다.

    python scripts\benchmark_mcp_first_query.py --help
    python scripts\benchmark_mcp_concurrent_queries.py --help
    python scripts\evaluate_mcp_retrieval_quality.py --help
    python scripts\build_mcp_performance_load_evidence.py --help
