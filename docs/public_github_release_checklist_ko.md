# 공개 GitHub 배포 체크리스트

이 문서는 `PR MCP Builder`를 public GitHub 저장소로 공개하기 전에 확인할 항목을 정리한다. private handoff와 public 공개는 목적이 다르므로, 공개 브랜치는 소스와 공개 가능한 문서만 포함하는 소스 전용(source-only) 브랜치로 준비한다.

비전공자용 요약: 공개 여부를 결정하기 전에 라이선스, 샘플 재배포 허용 여부, 내부 문서 제거 여부를 먼저 정한다. 그다음 원본 문서, 민감한 실행 결과, 토큰, 로컬 경로가 빠졌는지 확인하고, 깨끗한 새 폴더에서 설치와 검증을 다시 해 본다.

## 공개 경로

- 제한 공개 또는 private GitHub: 0.5~1일
- public GitHub 최소 공개: owner/legal/policy 결정 이후 3~5영업일
- 제품형 public 공개: strict parser, temporal metadata, reapproval evidence 보강 이후 1~2주

## 현재 원칙

현재 코드와 MCP 통합 테스트는 내부 PoC/파일럿에 가깝지만, 작업 폴더 전체를 그대로 공개하면 안 된다. 공개 브랜치는 다음 원칙을 따른다.

- 원본 업로드 문서, 기관 내부 규정 원문, 민감한 실행 결과는 제외한다.
- `data/`, `reports/`, `dist/`, runtime export는 공개 가능한 synthetic 또는 redacted artifact만 선별한다.
- HWP/PDF 샘플은 직접 만든 synthetic sample이거나 재배포 근거가 문서화된 경우에만 포함한다.
- private/internal handoff 문서는 공개 브랜치에서 제거하거나 공개용 문서로 다시 작성한다.
- 공개 전 LICENSE, SECURITY.md, CONTRIBUTING.md, `THIRD_PARTY_NOTICES.md`, public sample manifest를 확인한다.

## 최소 공개 차단 항목

`reg-rag-public-release-gate`와 `reg-rag-github-publish-readiness` 기준으로 다음 항목은 public GitHub 최소 공개 전 소유자 결정이 필요하다.

| Decision ID | 필요한 결정 | 기본 안전 선택 |
| --- | --- | --- |
| `license_selection` | 저장소 라이선스 선택 및 `LICENSE` 추가 | LICENSE가 없으면 공개하지 않는다. |
| `sample_redistribution_policy` | tracked HWP 샘플 제거 또는 재배포 근거 문서화 | 근거가 없으면 샘플을 공개 브랜치에서 제거한다. |
| `nonpublic_doc_policy` | private/internal 문서 제거 또는 public-safe 문서로 재작성 | 먼저 공개 브랜치에서 제거한다. |
| `identifier_fixture_policy` | 기관 유래 identifier fixture/report 합성 또는 제거 | generated report는 제거하고 테스트 fixture는 synthetic으로 교체한다. |

## 제품형 공개 차단 항목

아래 항목은 source-only GitHub 공개와 분리해서 추적한다. 최소 공개를 막는 항목이라기보다, 제품형 공개와 기관 납품급 자동화의 evidence blocker다.

- strict parser evidence: strict readiness gap이 남아 있으면 citation-grade release evidence로 인정하지 않는다.
- temporal metadata review: conflict가 0이어도 ambiguity가 크면 정책과 표시 방식이 필요하다.
- runtime version drift: stale chunk 재처리 또는 release-owner acceptance가 필요하다.
- reapproval review evidence: batch decision CSV가 채워지고 reindex evidence가 재생성되어야 한다.

## 공개 브랜치 포함/제외 기준

포함 후보:

- `app/`, `scripts/`, `tests/`, `frontend/`, `config/*.example.json`
- MCP 서버, smoke, client config 관련 공개 문서
- synthetic fixture 또는 재배포 근거가 있는 공개 샘플
- `docs/public_sample_manifest_ko.md`에 출처와 공개 조건이 기록된 샘플
- `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`

제외 후보:

- 원본 업로드 문서, 기관 내부 규정 원문, 재배포 근거 없는 HWP/PDF 샘플
- `data/repository*`, `data/vector_*`, `data/*runtime*`, `data/exports/*`
- `reports/*_current.json`, overnight session log, private handoff evidence
- `.env`, token, 로컬 경로가 담긴 설정 또는 로그
- path/secret/identifier scan을 통과하지 못한 generated report

## Fresh Clone 검증 순서

공개 후보 브랜치를 만든 뒤 깨끗한 경로에서 다음 순서로 재검증한다.

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e . build
reg-rag-check-console-scripts --out-json reports/installed_console_scripts_current.json --fail-on-issue
python -m unittest discover -v
python -m build --sdist --wheel
reg-rag-fresh-clone-rehearsal --mode public --dry-run --out-json reports/fresh_clone_rehearsal_plan_current.json --fail-on-issue
reg-rag-release-harness --mode public --out-json reports/release_harness_public_current.json --keep-going
python scripts\audit_release_hygiene.py --workflow-scope available --include-untracked --include-source-path-scan
reg-rag-audit-public-release --json --include-untracked --out-json reports/public_release_readiness_audit.preview.json
reg-rag-audit-public-release --json --out-json reports/public_release_readiness_audit.json
reg-rag-plan-public-release-cleanup --include-untracked --out-json reports/public_release_cleanup_plan.json --out-md reports/public_release_cleanup_plan.md
reg-rag-public-release-gate --include-untracked --execute-harness --out-json reports/public_release_gate_current.json --out-md reports/public_release_gate_current.md
reg-rag-strict-readiness-gaps --readiness-report reports/public_batch_readiness_strict_current.json --out-json reports/strict_public_readiness_gap_summary_current.json --out-md reports/strict_public_readiness_gap_summary_current.md
reg-rag-temporal-ambiguity-scope --temporal-report reports/aks_temporal_backfill_shadow_conflict_probe.json --out-json reports/temporal_ambiguity_review_scope_current.json --out-md reports/temporal_ambiguity_review_scope_current.md
reg-rag-mcp-product-readiness --out-json reports/mcp_product_readiness_current.json --out-md reports/mcp_product_readiness_current.md
reg-rag-mcp-doctor --audit-index-visibility --require-indexed --forbid-smoke-docs --out-json reports/mcp_connection_readiness_current.json --json
reg-rag-mcp-authority --authoritative-artifact product_readiness=reports/mcp_product_readiness_current.json --authoritative-artifact mcp_demo_answers=reports/mcp_demo_answers_current.json --authoritative-artifact mcp_transport_smoke=reports/mcp_transport_smoke_current.json --authoritative-artifact mcp_index_visibility=reports/mcp_index_visibility_current.json --authoritative-artifact mcp_connection_readiness=reports/mcp_connection_readiness_current.json --out-json reports/mcp_readiness_authority_current.json --out-md reports/mcp_readiness_authority_current.md --fail-on-issue
reg-rag-mcp-handoff-report --product-readiness-report reports/mcp_product_readiness_current.json --mcp-demo-answer-report reports/mcp_demo_answers_current.json --mcp-readiness-report reports/mcp_connection_readiness_current.json --mcp-index-visibility-report reports/mcp_index_visibility_current.json --authority-manifest reports/mcp_readiness_authority_current.json --out-json reports/mcp_handoff_current.json --out-md reports/mcp_handoff_current.md --fail-on-issue
reg-rag-release-evidence-index --profile mcp-product-readiness --repo-root . --out-json reports/mcp_product_readiness_release_evidence_index_current.json
reg-rag-verify-release-evidence --index-json reports/mcp_product_readiness_release_evidence_index_current.json --repo-root . --out-json reports/mcp_product_readiness_release_evidence_verification_current.json

# Product-public MCP release is not complete until mcp_connection_readiness_current.json,
# mcp_readiness_authority_current.json, mcp_handoff_current.json, and
# mcp_product_readiness_release_evidence_verification_current.json are regenerated
# from the same runtime. approval_journal_coverage.missing_record_count must be 0.
reg-rag-github-publish-readiness --public-release-gate-report reports/public_release_gate_current.json --product-readiness-report reports/mcp_product_readiness_current.json --remediation-plan-report reports/mcp_readiness_remediation_plan_current.json --evidence-verification-report reports/mcp_product_readiness_release_evidence_verification_current.json --strict-parser-candidate-report reports/mcp_product_readiness_strict_parser_candidate_current.json --strict-gap-summary-report reports/strict_public_readiness_gap_summary_current.json --out-json reports/github_publish_readiness_current.json --out-md reports/github_publish_readiness_current.md --out-decisions-csv reports/github_publish_owner_decisions_current.csv --out-decisions-md reports/github_publish_owner_decisions_current.md
reg-rag-github-publish-owner-decisions --decisions-csv reports/github_publish_owner_decisions_current.csv --readiness-summary-report reports/github_publish_readiness_current.json --out-json reports/github_publish_owner_decision_gate_current.json --out-md reports/github_publish_owner_decision_gate_current.md
reg-rag-github-publish-plan --readiness-summary-report reports/github_publish_readiness_current.json --owner-decision-gate-report reports/github_publish_owner_decision_gate_current.json --public-release-gate-report reports/public_release_gate_current.json --evidence-verification-report reports/mcp_product_readiness_release_evidence_verification_current.json --out-json reports/github_publish_execution_plan_current.json --out-md reports/github_publish_execution_plan_current.md
reg-rag-github-publish-owner-decisions --decisions-csv reports/github_publish_owner_decisions_current.csv --readiness-summary-report reports/github_publish_readiness_current.json --fail-on-blocker
reg-rag-mcp-smoke --out-json reports/mcp_smoke.json --fail-on-issue
reg-rag-private-release-smoke --synthetic-sample --out-json reports/private_release_smoke_dev.json
reg-rag-mcp-config --client-profile bundle --server-name regulation_mcp --tenant-id default --out-dir reports/mcp_connection_bundle --zip-out reports/mcp_connection_bundle.zip --include-wheel --skip-runtime-data
reg-rag-mcp-doctor --client-profile chatgpt --transport streamable-http --public-url https://example.invalid/mcp --skip-data-check --json
reg-rag-mcp-doctor --client-profile bundle --transport streamable-http --host 0.0.0.0 --public-url https://example.invalid/mcp --bundle-dir reports/mcp_connection_bundle --skip-data-check --json
git diff --check
```

`reg-rag-github-publish-owner-decisions --fail-on-blocker`는 `decision`, `decision_owner`, `decision_reference`가 모두 채워질 때까지 실패해야 정상이다. 이 gate가 실패하는 동안에는 license, 샘플 재배포, private 문서 제거/재작성, identifier fixture 처리 같은 owner decision이 완료되지 않은 상태로 본다.

ChatGPT HTTPS/tunnel doctor는 설정 검증용이다. 실제 공개 또는 기관 전용 HTTPS endpoint가 준비된 경우에만 `--probe-public-url`을 추가해서 네트워크 probe까지 확인한다.

## 공개 운영 주의사항

- Streamlit UI는 로컬 운영자 콘솔이며 공개 인터넷 서비스로 배포하지 않는다.
- HTTP MCP 서버를 외부망에 열 때는 bearer token, reverse proxy/mTLS, 방화벽, audit log 위치를 별도로 문서화한다.
- MCP 응답은 승인된 chunk와 citation metadata만 반환해야 하며, 원본 파일 경로, 미승인 본문, token, 내부 report 경로를 반환하면 안 된다.
- ChatGPT 또는 외부 data connector에는 MCP `chatgpt-data` tool profile을 사용하고, RAG HTTP 응답을 외부 경계로 넘길 때는 `metadata_profile=external` 또는 `metadata_profile=chatgpt-data`를 지정한다. 외부 프로필 응답에는 `source_record_id`, `source_file_id`, `approval_review_batch_manifest_path` 같은 내부 식별자와 로컬 증적 경로가 없어야 한다.
- 공개 README는 ChatGPT/Claude 연결을 기본 운영 경로처럼 표현하지 말고, 기관별 인증/네트워크 구성이 필요한 별도 경로로 설명한다.
- 공개 증적이 필요하면 path/secret/identifier scan을 통과한 redacted report만 allowlist로 선별한다.

## 권장 공개 절차

0. 기존 private 저장소의 visibility를 바로 public으로 바꾸지 않는다. 현재 트리와 Git 히스토리는 별도 검증 대상이며, 과거 private 커밋이나 branch에 원본 문서/기관 데이터가 있으면 `public-release`의 clean-history orphan snapshot을 별도 public 저장소에서 사용한다. 상세 정책은 `docs/public_repository_history_policy_ko.md`를 따른다.
1. `public-release` 브랜치를 새로 만든다.
2. owner decision CSV를 채우고 owner decision gate를 통과시킨다.
3. runtime 산출물, 내부 문서, 재배포 근거 없는 샘플을 제거한다.
4. synthetic sample과 공개 가능한 fixture만 남긴다.
5. LICENSE, SECURITY.md, CONTRIBUTING.md, public sample manifest를 확정한다.
6. 전체 테스트, package build, public release gate, fresh clone rehearsal을 실행한다.
7. GitHub 저장소 공개 직전 마지막으로 secret/path/identifier scan을 실행한다.
